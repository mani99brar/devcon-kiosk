#!/usr/bin/env python3
"""Devcon booth kiosk: one Chromium per screen, timed site rotation, phone control.

  kiosk.py check          validate config.toml for this hostname
  kiosk.py write-configs  write the labwc and kanshi config for this hostname
  kiosk.py run            start the browsers, the rotation and the control server

Sites are preloaded as tabs; rotation switches tabs over Chromium's local
DevTools HTTP endpoint (127.0.0.1 only). A `video:<file>` entry plays
~/kiosk/videos/<file> once in mpv on top of the browser, then moves on.
Links added or removed from the phone page are kept in sites.json, on top of
config.toml, so they survive reboots and deploys. With server_url in
secret.toml, an uplink thread also connects out to the relay server
(server/app.py) and runs the phone commands sent there. Stdlib only.
"""
import base64
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
STATUS_SECONDS = 15  # how long the boot status page (control URL) stays up
SITES_FILE = HERE / "sites.json"
sites_lock = threading.Lock()
MIN_CODE_LEN = 16  # the team link is public, so the code must not be guessable

CHROMIUM_FLAGS = [
    "--kiosk",
    "--ozone-platform=wayland",
    "--noerrdialogs",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--deny-permission-prompts",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--disable-features=Translate,TranslateUI,MediaRouter",
    "--simulate-outdated-no-au=Tue, 31 Dec 2099 23:59:59 GMT",
    "--overscroll-history-navigation=0",
    "--disable-pinch",
    "--autoplay-policy=no-user-gesture-required",
    "--enable-features=OverlayScrollbar",
]

MPV_FLAGS = [
    "--fs",
    "--no-osc",
    "--osd-level=0",
    "--no-input-default-bindings",
    "--cursor-autohide=always",
    "--keep-open=no",
    "--no-terminal",
]
# Pi 5 decodes only HEVC in hardware, and that needs the zero-copy output;
# everything else (H.264 up to 1080p) is decoded on the CPU.
MPV_HEVC = ["--hwdec=drm", "--vo=dmabuf-wayland"]
MPV_OTHER = ["--hwdec=no", "--vo=gpu-next"]

AUTOSTART = """\
# Written by kiosk.py write-configs. Edit config.toml and rerun deploy.sh instead.
session=$(pgrep -n -u "$(id -u)" -x labwc)  # everything below stops with this labwc
kanshi > "$HOME/kiosk/kanshi.log" 2>&1 &
: > "$HOME/kiosk/kiosk.log"
(while kill -0 "$session" 2>/dev/null; do
  KIOSK_SESSION_PID=$session python3 "$HOME/kiosk/kiosk.py" run; sleep 3
done) >> "$HOME/kiosk/kiosk.log" 2>&1 &
"""

RC_XML = """\
<?xml version="1.0"?>
<!-- Written by kiosk.py write-configs. Edit config.toml and rerun deploy.sh instead. -->
<labwc_config>
  <windowRules>
    <windowRule identifier="*">
      <action name="HideCursor"/>
    </windowRule>
{rules}  </windowRules>
</labwc_config>
"""


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


def load_settings():
    cfg = tomllib.loads((HERE / "config.toml").read_text())
    # the access code and team links are kept out of git in secret.toml
    # (deploys before it existed had them in config.toml)
    secret = HERE / "secret.toml"
    if secret.exists():
        cfg.update(tomllib.loads(secret.read_text()))
    return cfg


def load_config():
    cfg = load_settings()
    host = socket.gethostname()
    blocks = cfg.get("pi", {}).get(host, {}).get("screen", [])
    if not blocks:
        sys.exit(f"config.toml has no [[pi.{host}.screen]] block for this Pi")
    screens = []
    changes = load_site_changes()
    for i, s in enumerate(blocks):
        # a site is a URL, or { url = "...", seconds = 241 } to give it its own time on screen
        entries = [e if isinstance(e, dict) else {"url": e} for e in s.get("sites", [])]
        base = [str(e.get("url", "")).strip() for e in entries if str(e.get("url", "")).strip()]
        seconds = {str(e["url"]).strip(): float(e["seconds"]) for e in entries
                   if "seconds" in e and str(e.get("url", "")).strip()}
        if not base:
            sys.exit(f"screen {i} on {host} has no sites")
        c = changes.get(str(i), {})
        sites = ([u for u in base if u not in c.get("removed", [])]
                 + [u for u in c.get("added", []) if u not in base])
        screens.append(Screen(
            index=i,
            output=s.get("output", f"HDMI-A-{i + 1}"),
            interval=float(s.get("interval", cfg.get("interval", 25))),
            zoom=float(s.get("zoom", cfg.get("zoom", 1.35))),
            sites=sites or base,
            base=base,
            seconds=seconds,
            control_port=int(cfg.get("control_port", 8080)),
            video_audio=bool(cfg.get("video_audio", False)),
        ))
    return cfg, host, screens


def load_site_changes():
    try:
        return json.loads(SITES_FILE.read_text())
    except (OSError, ValueError):
        return {}


def clean_url(url):
    """A link typed on the phone, or None if it isn't a plain web address."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme not in ("http", "https") or "." not in (parts.hostname or "")
            or len(url) > 2000):
        return None
    return url


def is_video(site):
    return site.startswith("video:")


def video_path(site):
    return HERE / "videos" / site.removeprefix("video:")


def video_codec(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True)
    except FileNotFoundError:  # ffprobe not installed yet (first deploy)
        return ""
    return out.stdout.strip().split(",")[0]  # some files add empty csv columns ("h264,")


def label(url):
    if is_video(url):
        return "▶ " + url.removeprefix("video:")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "file":  # a page from pages/: its file name
        return parts.path.rsplit("/", 1)[-1] or url
    name = (parts.hostname or url).removeprefix("www.")
    if parts.path not in ("", "/"):
        name += parts.path
    return name if len(name) <= 40 else name[:39] + "…"


def local_ips():
    out = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout
    return [ip for ip in out.split() if ":" not in ip]


def wait_for_network(urls, timeout=90):
    hosts = [h for h in (urllib.parse.urlsplit(u).hostname for u in urls)
             if h and h not in ("localhost", "127.0.0.1")]
    deadline = time.time() + timeout
    while hosts and time.time() < deadline:
        try:
            socket.getaddrinfo(hosts[0], 443)
            return
        except OSError:
            time.sleep(2)
    if hosts:
        log("network not up after", timeout, "s; loading sites anyway")


def mark_clean_exit(profile):
    """Stop Chromium offering to restore tabs after a power cut or crash."""
    prefs = profile / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text())
    except (OSError, ValueError):
        return
    data.setdefault("profile", {}).update(exit_type="Normal", exited_cleanly=True)
    prefs.write_text(json.dumps(data))


class Screen:
    def __init__(self, index, output, interval, zoom, sites, control_port, video_audio=False,
                 base=None, seconds=None):
        self.index, self.output, self.interval, self.zoom = index, output, interval, zoom
        self.sites, self.control_port, self.video_audio = sites, control_port, video_audio
        self.base = base or list(sites)  # the sites in config.toml
        self.seconds = seconds or {}  # url -> its own time on screen, instead of interval
        self.port = 9222 + index
        self.profile = HOME / ".cache" / "kiosk" / f"screen{index}"
        self.proc = None
        self.player = None
        self.tabs = []
        self.temp = None  # (tab id, url) put up with "Show now"; closed on the next switch
        self.current = 0
        self.paused = False
        self.next_switch = 0.0
        self.errors = 0
        self.lock = threading.Lock()
        self.shot, self.shot_time, self.shot_lock = b"", 0.0, threading.Lock()

    def devtools(self, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read()
        return json.loads(body) if body[:1] in (b"{", b"[") else body.decode()

    def new_tab(self, url):
        return self.devtools("/json/new?" + urllib.parse.quote(url, safe=":/"), "PUT")["id"]

    def play(self, site):
        path = video_path(site)
        hevc = video_codec(path) == "hevc"
        args = ["mpv", f"--wayland-app-id=kiosk-video-{self.index}", *MPV_FLAGS,
                *(MPV_HEVC if hevc else MPV_OTHER)]
        if not self.video_audio:
            args.append("--no-audio")
        mpv_log = open(HERE / f"mpv{self.index}.log", "w")
        self.player = subprocess.Popen([*args, str(path)], stdout=mpv_log, stderr=mpv_log)
        log(f"screen {self.index}: playing {path.name} ({'HEVC, hardware' if hevc else 'CPU'} decode)")

    def close_temp(self):
        if self.temp is None:
            return
        tab, self.temp = self.temp[0], None
        try:
            self.devtools(f"/json/close/{tab}")
        except OSError:
            pass

    def save_sites(self):
        """Record how the phone page changed this screen's sites (sites.json)."""
        with sites_lock:
            changes = load_site_changes()
            changes[str(self.index)] = {
                "added": [u for u in self.sites if u not in self.base],
                "removed": [u for u in self.base if u not in self.sites],
            }
            SITES_FILE.write_text(json.dumps(changes, indent=2) + "\n")

    def stop_video(self):
        if self.player is None:
            return
        self.player.terminate()
        try:
            self.player.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.player.kill()
        self.player = None

    def kill(self):
        self.stop_video()
        subprocess.run(["pkill", "-f", "--", f"--user-data-dir={self.profile}"], check=False)
        self.proc = None
        self.tabs = []
        self.temp = None

    def launch(self):
        self.kill()
        time.sleep(1)
        self.profile.mkdir(parents=True, exist_ok=True)
        mark_clean_exit(self.profile)
        args = [
            "chromium",
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={self.port}",
            f"--class=kiosk-{self.index}",
            f"--force-device-scale-factor={self.zoom}",
            *CHROMIUM_FLAGS,
            f"http://127.0.0.1:{self.control_port}/status",
        ]
        chromium_log = open(HERE / f"chromium{self.index}.log", "w")
        self.proc = subprocess.Popen(args, stdout=chromium_log, stderr=chromium_log,
                                     start_new_session=True)
        started = time.monotonic()
        log(f"screen {self.index}: chromium started (pid {self.proc.pid})")

        for _ in range(60):
            try:
                self.devtools("/json/version")
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("chromium DevTools endpoint did not come up")

        wait_for_network(self.sites)
        status = [t["id"] for t in self.devtools("/json/list") if t.get("type") == "page"]
        # loads every site in the background; videos have no tab
        tabs = [None if is_video(u) else self.new_tab(u) for u in self.sites]
        for t in status:
            self.devtools(f"/json/activate/{t}")
        time.sleep(max(0.0, STATUS_SECONDS - (time.monotonic() - started)))
        for t in status:
            self.devtools(f"/json/close/{t}")
        with self.lock:
            self.tabs = tabs
            self.show(0)
        log(f"screen {self.index}: rotating {len(tabs)} sites every {self.interval:g} s")

    def show(self, i):
        """Bring site i to the front. Caller holds self.lock."""
        i %= len(self.tabs)
        self.stop_video()
        self.close_temp()
        self.current = i
        if is_video(self.sites[i]):
            self.play(self.sites[i])
            self.next_switch = float("inf")  # loop() moves on when the video ends
            return
        try:
            self.devtools(f"/json/activate/{self.tabs[i]}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            log(f"screen {self.index}: tab for {label(self.sites[i])} was gone, reopening")
            self.tabs[i] = self.new_tab(self.sites[i])
            self.devtools(f"/json/activate/{self.tabs[i]}")
        self.next_switch = time.monotonic() + self.slot(i)

    def slot(self, i):
        """Seconds site i stays up: its own `seconds`, else the screen's interval."""
        return self.seconds.get(self.sites[i], self.interval)

    def loop(self):
        while True:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    if self.proc is not None:
                        log(f"screen {self.index}: chromium exited ({self.proc.returncode}), restarting")
                    self.launch()
                with self.lock:
                    if self.player is not None and self.player.poll() is not None:
                        self.player = None
                        if self.paused:  # keep the paused video looping
                            self.play(self.sites[self.current])
                        else:
                            self.show(self.current + 1)
                    elif self.tabs and not self.paused and time.monotonic() >= self.next_switch:
                        self.show(self.current + 1)
                self.errors = 0
            except Exception as e:
                self.errors += 1
                log(f"screen {self.index}: {e!r} ({self.errors} in a row)")
                if self.errors >= 3 or not self.tabs:
                    log(f"screen {self.index}: restarting chromium")
                    self.kill()
                time.sleep(5)
            time.sleep(0.5)

    def command(self, action, site=None, url=None):
        """Run a phone command. Returns an error message, or None."""
        with self.lock:
            if not self.tabs:
                return "screen is still starting"
            if action in ("show", "add", "remove") and not url:
                return "missing link"
            if action == "show":  # put a link up now, paused, without keeping it
                self.stop_video()
                self.close_temp()
                tab = self.new_tab(url)
                self.devtools(f"/json/activate/{tab}")
                self.temp = (tab, url)
                self.paused = True
            elif action == "add":
                if url not in self.sites:
                    self.sites.append(url)
                    self.tabs.append(None if is_video(url) else self.new_tab(url))
                    self.save_sites()
                self.show(self.sites.index(url))
            elif action == "remove":
                if url not in self.sites:
                    return "that site is not in the rotation"
                if len(self.sites) == 1:
                    return "can't remove the last site"
                i = self.sites.index(url)
                self.sites.pop(i)
                tab = self.tabs.pop(i)
                self.save_sites()
                if tab is not None:
                    try:
                        self.devtools(f"/json/close/{tab}")
                    except OSError:
                        pass
                if i < self.current:
                    self.current -= 1
                elif i == self.current:
                    self.show(self.current)
            elif action == "next":
                self.show(self.current + 1)
            elif action == "prev":
                self.show(self.current - 1)
            elif action == "jump" and site is not None:
                self.show(site)
            elif action == "pause":
                self.paused = True
            elif action == "resume":
                self.paused = False
                if self.temp is not None:
                    self.show(self.current)
                elif self.player is None:
                    self.next_switch = time.monotonic() + self.slot(self.current)
            else:
                return "unknown command"

    def snapshot(self):
        """A small JPEG of what this screen shows, for the phone page; reused for 2 s."""
        with self.shot_lock:
            if time.monotonic() - self.shot_time > 2:
                try:  # Debian's grim has no JPEG, so ffmpeg compresses it (~30 KB)
                    ppm = subprocess.run(["grim", "-o", self.output, "-s", "0.3", "-t", "ppm", "-"],
                                         capture_output=True, timeout=10).stdout
                    self.shot = subprocess.run(
                        ["ffmpeg", "-loglevel", "error", "-f", "ppm_pipe", "-i", "-",
                         "-q:v", "6", "-f", "mjpeg", "-"],
                        input=ppm, capture_output=True, timeout=10).stdout if ppm else b""
                except (OSError, subprocess.TimeoutExpired):
                    self.shot = b""
                self.shot_time = time.monotonic()
            return self.shot

    def state(self):
        return {
            "index": self.index,
            "output": self.output,
            "ready": bool(self.tabs),
            "paused": self.paused,
            "current": self.current,
            "temp": label(self.temp[1]) if self.temp else None,
            "interval": self.interval,
            "sites": [label(u) for u in self.sites],
            "urls": self.sites,
        }


def write_configs(screens):
    labwc = HOME / ".config" / "labwc"
    labwc.mkdir(parents=True, exist_ok=True)
    (labwc / "autostart").write_text(AUTOSTART)
    rules = ""
    if len(screens) > 1:  # pin each screen's browser and video player to its own output
        rules = "".join(
            f'    <windowRule identifier="{app}-{s.index}">\n'
            f'      <action name="MoveToOutput" output="{s.output}"/>\n'
            f"    </windowRule>\n" for s in screens for app in ("kiosk", "kiosk-video"))
    (labwc / "rc.xml").write_text(RC_XML.format(rules=rules))

    lines = [f"  output {s.output} mode 1920x1080@60Hz position {1920 * s.index},0"
             for s in screens]
    profiles = ["profile all {\n" + "\n".join(lines) + "\n}"]
    if len(screens) > 1:  # still force 1080p when only one of the TVs is on
        profiles += [f"profile only{s.index} {{\n{line}\n}}" for s, line in zip(screens, lines)]
    kanshi = HOME / ".config" / "kanshi"
    kanshi.mkdir(parents=True, exist_ok=True)
    (kanshi / "config").write_text(
        "# Written by kiosk.py write-configs. 1080p on every kiosk output.\n"
        + "\n".join(profiles) + "\n")


def load_code(cfg):
    code = str(cfg.get("access_code", "")).encode()
    if not code:
        sys.exit("secret.toml needs an access_code")
    return code


def pi_state(host, public_urls, screens):
    return {"host": host, "peers": public_urls, "screens": [s.state() for s in screens]}


def run_command(screens, i, action, query, data, via=None):
    """Run /api/screen/<i>/<action>[?site=<n>] with JSON body `data` ({"url": ...} for
    show/add/remove), from the control server or the relay server. Returns (status,
    error payload), or (200, None) when it worked: the caller replies with pi_state()."""
    if i >= len(screens):
        return 404, {"error": "no such screen"}
    site = urllib.parse.parse_qs(query).get("site", [None])[0]
    url = data.get("url") if isinstance(data, dict) else None
    if url is not None and action != "remove":  # remove names an existing entry as-is
        url = clean_url(url)
        if url is None:
            return 400, {"error": "that isn't a web address"}
    log(f"control: {action} {url or site or ''} on screen {i}" + (f" (via {via})" if via else ""))
    try:
        error = screens[i].command(action, int(site) if site and site.isdigit() else None, url)
    except OSError as e:
        error = f"browser didn't respond ({e})"
    if error:
        return 409, {"error": error}
    return 200, None


def make_handler(cfg, host, screens):
    code = load_code(cfg)
    public_urls = [u.rstrip("/") for u in cfg.get("public_urls", [])]
    control_html = (HERE / "control.html").read_bytes()

    class Control(BaseHTTPRequestHandler):
        def reply(self, status, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype if ctype.startswith("image/") else f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            # the page on one Pi also controls the others; the code header is the auth
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            if hmac.compare_digest(self.headers.get("X-Code", "").encode(), code):
                return True
            time.sleep(1)  # slow down guessing
            self.reply(403, {"error": "wrong access code"})
            return False

        def do_OPTIONS(self):  # CORS preflight from the page on the other Pi
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "X-Code, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/":
                self.reply(200, control_html, "text/html")
            elif path == "/status":
                self.reply(200, status_html(host, screens[0].control_port, public_urls), "text/html")
            elif path == "/api/state":
                if self.authorized():
                    self.reply(200, pi_state(host, public_urls, screens))
            elif (m := re.fullmatch(r"/api/screen/(\d+)/shot\.jpg", path)) and int(m[1]) < len(screens):
                if self.authorized():
                    shot = screens[int(m[1])].snapshot()
                    self.reply(200, shot, "image/jpeg") if shot else self.reply(503, {"error": "no picture"})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            # /api/screen/<i>/<action>[?site=<n>], with {"url": ...} for show/add/remove
            parts = urllib.parse.urlsplit(self.path)
            p = parts.path.strip("/").split("/")
            if len(p) != 4 or p[:2] != ["api", "screen"] or not p[2].isdigit():
                return self.reply(404, {"error": "not found"})
            if not self.authorized():
                return
            data = None
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= 4096:
                try:
                    data = json.loads(self.rfile.read(length))
                except ValueError:
                    pass
            status, payload = run_command(screens, int(p[2]), p[3], parts.query, data)
            self.reply(status, payload or pi_state(host, public_urls, screens))

        def log_message(self, *args):
            pass

    return Control


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):  # a redirect would carry the Pi key elsewhere: fail instead
        return None


NO_REDIRECTS = urllib.request.build_opener(_NoRedirect)


def uplink(cfg, host, screens, public_urls, stop=None):
    """Keep this Pi connected to the relay server (server_url) and run the phone commands
    it passes on. The Pi only connects out; each sync carries the answers to the last one.
    Setting `stop` (a threading.Event; only the tests use it) ends the loop."""
    stop = stop or threading.Event()
    server_url = cfg["server_url"].rstrip("/")
    headers = {"Content-Type": "application/json", "X-Pi-Key": str(cfg.get("server_pi_key", ""))}
    if cfg.get("server_lab_token"):
        headers["X-Lab-Token"] = str(cfg["server_lab_token"])
    results, wanted, shot_sent = [], [], {}
    delay, down, reasons = 1, True, set()
    while not stop.is_set():
        shots = {}
        for i in wanted:  # previews a phone is looking at, each at most every 2 s
            if ((i not in shot_sent or time.monotonic() - shot_sent[i] >= 2)
                    and (shot := screens[i].snapshot())):
                shots[str(i)] = base64.b64encode(shot).decode()
                shot_sent[i] = time.monotonic()
        body = json.dumps({"host": host, "state": pi_state(host, public_urls, screens),
                           "results": results, "shots": shots}).encode()
        started = time.monotonic()
        try:
            req = urllib.request.Request(server_url + "/pi/sync", body, headers, method="POST")
            with NO_REDIRECTS.open(req, timeout=30) as r:
                reply = json.loads(r.read())
            requests = [q for q in reply.get("requests") or [] if isinstance(q, dict)]
            wanted = [i for i in reply.get("want_shots") or []
                      if type(i) is int and 0 <= i < len(screens)]
        except Exception as e:  # can't connect, timed out, an HTTP error, or not a sync reply
            refused = isinstance(e, urllib.error.HTTPError) and e.code == 403
            reason = (str(e) if isinstance(e, urllib.error.HTTPError)
                      else str(getattr(e, "reason", "") or e) or type(e).__name__)
            if reason not in reasons:
                reasons.add(reason)
                log("uplink: the server refused this Pi's key" if refused
                    else f"uplink: can't reach {server_url} ({reason}), retrying")
            down, wanted = True, []  # results are kept for the next sync
            delay = 60 if refused else delay
            stop.wait(delay)
            delay = min(delay * 2, 60)
            continue
        if down:
            log(f"uplink: connected to {server_url}")
        down, delay, reasons, results = False, 1, set(), []
        for q in requests:
            m = re.fullmatch(r"/api/screen/(\d+)/([a-z]{1,20})", str(q.get("path")))
            status, payload = 404, {"error": "not found"}
            if q.get("method") == "POST" and m:
                try:
                    status, payload = run_command(screens, int(m[1]), m[2], str(q.get("query") or ""),
                                                  q.get("body"), via="server")
                except Exception as e:  # the control server would drop the connection
                    log(f"uplink: {m[2]} on screen {m[1]} failed: {e!r}")
                    status, payload = 500, {"error": f"the Pi couldn't do that ({e})"}
            results.append({"id": q.get("id"), "status": status,
                            "body": payload or pi_state(host, public_urls, screens)})
        del results[:-20]
        if not requests:  # the server holds each sync ~2 s; one that doesn't mustn't make this spin
            stop.wait(max(0.0, 0.5 - (time.monotonic() - started)))


def status_html(host, port, public_urls=()):
    urls = [u for u in public_urls if urllib.parse.urlsplit(u).hostname.startswith(host + ".")]
    urls += [f"http://{host}.local:{port}"] + [f"http://{ip}:{port}" for ip in local_ips()]
    items = "".join(f"<li>{u}</li>" for u in urls) or "<li>no network yet</li>"
    return (
        "<!doctype html><meta charset=utf-8><title>Starting</title>"
        "<style>body{margin:0;height:100vh;display:grid;place-content:center;"
        "background:#111;color:#eee;font:28px system-ui}h1{font-size:48px}"
        "ul{list-style:none;padding:0}li{margin:8px 0;font-family:monospace}</style>"
        f"<h1>{host} is starting</h1><p>Phone control:</p><ul>{items}</ul>")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    cfg, host, screens = load_config()
    if cmd == "check":
        if len(load_code(cfg)) < MIN_CODE_LEN:
            print(f"WARNING: access_code is shorter than {MIN_CODE_LEN} characters; "
                  "too easy to guess on the public team link")
        for s in screens:
            print(f"{host} screen {s.index}: {s.output}, {len(s.sites)} sites, "
                  f"every {s.interval:g} s, zoom {s.zoom:g}")
            for url, sec in s.seconds.items():
                print(f"  {label(url)}: {sec:g} s")
            if s.sites != s.base:
                print(f"  changed from the phone page (sites.json): {len(s.sites)} sites now")
            for site in filter(is_video, s.sites):
                path = video_path(site)
                print(f"  {label(site)}: " + (video_codec(path) or "unreadable") if path.exists()
                      else f"  WARNING: {path} is missing; it will be skipped")
        if cfg.get("server_url"):
            print(f"server: {cfg['server_url']} (Pi key {'set' if cfg.get('server_pi_key') else 'NOT set'}, "
                  f"lab token {'set' if cfg.get('server_lab_token') else 'not set'})")
        else:
            print("server: off")
        return
    if cmd == "write-configs":
        write_configs(screens)
        return
    if cmd != "run":
        sys.exit(__doc__)

    server = ThreadingHTTPServer(("0.0.0.0", screens[0].control_port),
                                 make_handler(cfg, host, screens))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"control server on port {screens[0].control_port}")
    for s in screens:
        threading.Thread(target=s.loop, daemon=True).start()
    if cfg.get("server_url"):
        public_urls = [u.rstrip("/") for u in cfg.get("public_urls", [])]
        threading.Thread(target=uplink, args=(cfg, host, screens, public_urls), daemon=True).start()
    session = int(os.environ.get("KIOSK_SESSION_PID", "0"))
    while True:
        time.sleep(2)
        if session and not Path(f"/proc/{session}").exists():
            log("labwc session ended, stopping")
            for s in screens:
                s.kill()
            return


if __name__ == "__main__":
    main()
