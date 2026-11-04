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
(server/app.py), over its WebSocket when it can and HTTP polling otherwise,
and runs the phone commands sent there. Stdlib only, plus Debian's
python3-websockets for the WebSocket, the phone's browsing commands and fast
previews; without it the kiosk runs as before (HTTP only).
"""
import base64
import hmac
import itertools
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

try:
    from websockets.exceptions import InvalidStatus, WebSocketException
    from websockets.sync.client import connect as ws_connect
except ImportError:
    ws_connect = None

HERE = Path(__file__).resolve().parent
HOME = Path.home()
STATUS_SECONDS = 15  # how long the boot status page (control URL) stays up
SITES_FILE = HERE / "sites.json"
sites_lock = threading.Lock()
MIN_CODE_LEN = 16  # the team link is public, so the code must not be guessable
WS_RETRY = 300  # seconds of HTTP sync before trying the relay's WebSocket again
STATE_EVERY = 10  # seconds between state messages over the WebSocket when nothing changed
PREVIEW_GAP = 0.25  # at most 4 previews a second per screen over the WebSocket
PREVIEW_WIDTH = 768  # pixels, for DevTools previews (grim's -s 0.4 of 1920)
CDP_TIMEOUT = 3  # seconds for each step of a DevTools WebSocket operation
# seconds a video stays up under the next one, so the TV never shows the browser between two
# videos while the next mpv starts (it closed at its end before: the page behind flashed up)
HANDOVER = 3
MPV_START = 1  # seconds mpv may take to show a video: the switch waits this long past its length
BROWSE = ("click", "scroll", "type", "key", "back", "forward", "reload")  # phone browsing
KEYS = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "ArrowLeft": 37, "ArrowUp": 38,
        "ArrowRight": 39, "ArrowDown": 40}  # the keys the phone can press: windowsVirtualKeyCode

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


def same_site(url, site):
    """Whether a tab showing url is still on site: the same host, ignoring a leading www.
    (for a file:// page, the same file)."""
    a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(site)
    if b.scheme == "file":
        return a.scheme == "file" and urllib.parse.unquote(a.path) == urllib.parse.unquote(b.path)
    return (a.hostname or "").removeprefix("www.") == (b.hostname or "").removeprefix("www.")


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


def video_seconds(path):
    """The video's length in seconds, or None when ffprobe can't tell."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True)
        seconds = float(out.stdout.strip().split(",")[0])
    except (FileNotFoundError, ValueError):  # not installed yet, or "N/A"
        return None
    return seconds if 0 < seconds < float("inf") else None


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


class DevToolsError(OSError):
    """A DevTools WebSocket operation on a tab failed."""


class Page:
    """One short DevTools WebSocket connection to a Chromium tab (Screen.cdp opens it)."""

    def __init__(self, ws):
        self.ws, self.ids, self.events = ws, itertools.count(1), []

    def read(self):
        msg = json.loads(self.ws.recv(CDP_TIMEOUT))
        return msg if isinstance(msg, dict) else {}

    def call(self, method, **params):
        """Send one command and wait for its answer; returns its result."""
        n = next(self.ids)
        self.ws.send(json.dumps({"id": n, "method": method, "params": params}))
        while (msg := self.read()).get("id") != n:
            self.events.append(msg)
        if "error" in msg:
            error = msg["error"]
            raise DevToolsError(f"{method}: {error.get('message') if isinstance(error, dict) else error}")
        return msg.get("result") or {}

    def wait(self, method):
        """The params of the first `method` event (e.g. Page.screencastFrame)."""
        while not (found := [m for m in self.events if m.get("method") == method]):
            self.events.append(self.read())
        return found[0].get("params") or {}


def browse_error(action, data):
    """What is wrong with the body of a phone browsing command (BROWSE), or None."""
    d = data if isinstance(data, dict) else {}

    def between(key, low, high):
        return type(d.get(key)) in (int, float) and low <= d[key] <= high

    if action == "click" and not (between("x", 0, 1) and between("y", 0, 1)):
        return "click needs x and y between 0 and 1"
    if action == "scroll" and not between("dy", -2, 2):
        return "scroll needs dy between -2 and 2"
    if action == "type" and not (isinstance(d.get("text"), str) and 0 < len(d["text"]) <= 500):
        return "type needs a text of at most 500 characters"
    if action == "key" and not (isinstance(d.get("key"), str) and d["key"] in KEYS):
        return "key must be one of " + ", ".join(KEYS)
    return None


def page_action(page, action, data):
    """Do a phone browsing command (BROWSE, body checked by browse_error) on a Page."""
    if action in ("click", "scroll"):
        w, h = page.call("Runtime.evaluate", expression="[innerWidth, innerHeight]",
                         returnByValue=True)["result"]["value"]
        if action == "click":  # x and y are fractions of the screen
            x, y = data["x"] * w, data["y"] * h
            page.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
            for kind in ("mousePressed", "mouseReleased"):
                page.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)
        else:  # dy is a fraction of the screen's height, positive = down
            page.call("Input.dispatchMouseEvent", type="mouseWheel", x=w / 2, y=h / 2,
                      deltaX=0, deltaY=data["dy"] * h)
    elif action == "type":
        page.call("Input.insertText", text=data["text"])
    elif action == "key":
        # (no nativeVirtualKeyCode: Chromium on a Mac took 27 for its own "-" key and kept repeating it)
        key = {"key": data["key"], "code": data["key"], "windowsVirtualKeyCode": KEYS[data["key"]]}
        page.call("Input.dispatchKeyEvent", type="keyDown", **key,
                  **({"text": "\r"} if data["key"] == "Enter" else {}))
        page.call("Input.dispatchKeyEvent", type="keyUp", **key)
    elif action in ("back", "forward"):
        page.call("Runtime.evaluate", expression=f"history.{action}()")
    elif action == "reload":
        page.call("Page.reload")


def page_picture(page):
    """A preview-sized JPEG of a Page, from a one-frame screencast. (A Page.captureScreenshot
    scaled with a clip resizes the page on the TV while it captures.)"""
    page.call("Page.startScreencast", format="jpeg", quality=60,
              maxWidth=PREVIEW_WIDTH, maxHeight=PREVIEW_WIDTH)
    frame = page.wait("Page.screencastFrame")
    page.call("Page.stopScreencast")
    return base64.b64decode(frame["data"])


def end_process(p):
    p.terminate()
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        p.kill()


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
        self.leaving = []  # (mpv, when to stop it): videos kept up under the next one (HANDOVER)
        self.tabs = []
        self.temp = None  # (tab id, url) put up with "Show now"; closed on the next switch
        # tabs a page opened on top of ours (a target=_blank link, window.open), newest last;
        # closed by the phone's Close tab, the next switch, Show now or Resume (see follow_tabs)
        self.popups = []
        self.closed = set()  # tabs this kiosk closed that Chromium may still list for a moment
        self.on_screen = (None, "")  # (tab id, its URL now), from Chromium's tab list
        self.current = 0
        self.paused = False
        self.next_switch = 0.0
        self.errors = 0
        self.lock = threading.Lock()
        self.shot, self.shot_time, self.shot_lock = b"", 0.0, threading.Lock()
        self.capture_errors = set()  # why DevTools previews failed (each logged once)
        self.devtools_off_until = 0.0  # previews with grim only until then, after a failure

    def devtools(self, path, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read()
        return json.loads(body) if body[:1] in (b"{", b"[") else body.decode()

    def new_tab(self, url):
        return self.devtools("/json/new?" + urllib.parse.quote(url, safe=":/"), "PUT")["id"]

    def active_tab(self):
        """The tab on screen: the newest tab a page opened, else the "Show now" tab, else the
        current site's (None for a video)."""
        popups, temp, tabs, current = self.popups, self.temp, self.tabs, self.current
        if popups:
            return popups[-1]
        if temp is not None:
            return temp[0]
        return tabs[current] if current < len(tabs) else None

    def follow_tabs(self):
        """Keep up with what Chromium shows. A link with target=_blank (or window.open, or the
        mouse) opens a tab this kiosk didn't, in front of ours: it becomes the tab on screen
        (previews, browsing) until the next switch, Show now or Resume closes it. Also notes
        the URL of the tab on screen for state(). Caller holds self.lock."""
        if not self.tabs:  # starting: the boot status page is the only tab
            return
        try:
            pages = {t["id"]: t.get("url", "") for t in self.devtools("/json/list")
                     if isinstance(t, dict) and t.get("type") == "page" and "id" in t}
        except (OSError, ValueError, TypeError, AttributeError):
            return  # as before; the next call tries again
        self.closed = {t for t in self.closed if t in pages}
        ours = set(self.tabs) | ({self.temp[0]} if self.temp else set()) | self.closed
        kept = [t for t in self.popups if t in pages]
        # (Chromium lists the most recently active tab first: that one ends up on top)
        new = [t for t in reversed(pages) if t not in ours and t not in kept]
        if new:
            log(f"screen {self.index}: a page opened {label(pages[new[-1]])} in a new tab, following it")
        self.popups = kept + new
        tab = self.active_tab()
        self.on_screen = (tab, pages.get(tab, ""))

    def close_tab(self, tab):
        """Close a tab. Chromium can still list it for a few ms (seen with Chrome 156), so
        follow_tabs mustn't take it for a new one. Caller holds self.lock."""
        self.closed.add(tab)
        try:
            self.devtools(f"/json/close/{tab}")
        except OSError:
            pass

    def close_popups(self):
        """Close the tabs pages opened (follow_tabs). Returns whether there were any.
        Caller holds self.lock."""
        popups, self.popups = self.popups, []
        for tab in popups:
            self.close_tab(tab)
        return bool(popups)

    def cdp(self, run):
        """run(page) with page a Page: a short DevTools WebSocket connection to the tab on
        screen. Returns what run returned; any failure comes out as DevToolsError."""
        tab = self.active_tab()
        if ws_connect is None:
            raise DevToolsError("python3-websockets isn't installed")
        if tab is None:
            raise DevToolsError("no tab on screen")
        try:
            target = next((t for t in self.devtools("/json/list") if t.get("id") == tab), None)
            if target is None:
                raise DevToolsError("the tab is gone")
            url = target.get("webSocketDebuggerUrl") or f"ws://127.0.0.1:{self.port}/devtools/page/{tab}"
            with ws_connect(url, open_timeout=CDP_TIMEOUT, close_timeout=1, ping_interval=None,
                            proxy=None, compression=None, max_size=2**24) as ws:
                return run(Page(ws))
        except DevToolsError:
            raise
        except (OSError, ValueError, LookupError, TypeError, AttributeError, WebSocketException) as e:
            raise DevToolsError(str(e) or type(e).__name__) from e

    def reset_tab(self, i):
        """Reopen site i's tab if it has wandered off the site (phone browsing, the mouse).
        Returns whether it did. Caller holds self.lock."""
        try:
            url = next((t.get("url", "") for t in self.devtools("/json/list")
                        if t.get("id") == self.tabs[i]), None)
            if url is None or same_site(url, self.sites[i]):
                return False
        except (OSError, ValueError, TypeError, AttributeError):
            return False  # the show goes on as before
        log(f"screen {self.index}: tab for {label(self.sites[i])} was on {label(url)}, reopening")
        self.close_tab(self.tabs[i])
        self.tabs[i] = self.new_tab(self.sites[i])
        return True

    def play(self, site):
        """Start a video on top. One already up stays under it for HANDOVER s, until the new
        window is surely up. Sets next_switch: the video's end (with MPV_START to spare, so its
        end isn't cut; mpv stays on its last frame meanwhile) when ffprobe knows its length,
        else never (loop() moves on when mpv exits)."""
        path = video_path(site)
        hevc = video_codec(path) == "hevc"
        seconds = video_seconds(path)
        args = ["mpv", f"--wayland-app-id=kiosk-video-{self.index}", *MPV_FLAGS,
                f"--keep-open={'yes' if seconds else 'no'}", *(MPV_HEVC if hevc else MPV_OTHER)]
        if not self.video_audio:
            args.append("--no-audio")
        with open(HERE / f"mpv{self.index}.log", "w") as mpv_log:  # (mpv keeps its own copy)
            player = subprocess.Popen([*args, str(path)], stdout=mpv_log, stderr=mpv_log)
        if self.player is not None:
            self.leaving.append((self.player, time.monotonic() + HANDOVER))
        self.player = player
        self.next_switch = time.monotonic() + MPV_START + seconds if seconds else float("inf")
        log(f"screen {self.index}: playing {path.name} ({'HEVC, hardware' if hevc else 'CPU'} decode"
            + (f", {seconds:.0f} s)" if seconds else ", length unknown)"))

    def close_temp(self):
        if self.temp is None:
            return
        tab, self.temp = self.temp[0], None
        self.close_tab(tab)

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
        """Stop the video on screen, and any still kept up under it."""
        players = [p for p, _ in self.leaving] + ([self.player] if self.player is not None else [])
        self.player, self.leaving = None, []
        for p in players:
            end_process(p)

    def retire(self):
        """Stop the videos kept up under a newer one, once their HANDOVER is over."""
        now = time.monotonic()
        for p, until in self.leaving:
            if until <= now:
                end_process(p)
        self.leaving = [(p, until) for p, until in self.leaving if until > now]

    def kill(self):
        self.stop_video()
        subprocess.run(["pkill", "-f", "--", f"--user-data-dir={self.profile}"], check=False)
        self.proc = None
        self.tabs = []
        self.temp = None
        self.popups = []
        self.closed = set()

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
        with self.lock:
            for t in status:
                self.close_tab(t)
            self.tabs = tabs
            self.show(0)
        log(f"screen {self.index}: rotating {len(tabs)} sites every {self.interval:g} s")

    def show(self, i):
        """Bring site i to the front. Caller holds self.lock."""
        i %= len(self.tabs)
        self.close_temp()
        self.close_popups()
        self.current = i
        if is_video(self.sites[i]):
            self.next_switch = float("inf")  # play() sets the video's end when it knows it
            self.play(self.sites[i])  # over the video before, if any (see play)
            return
        self.reset_tab(i)
        try:
            self.devtools(f"/json/activate/{self.tabs[i]}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            log(f"screen {self.index}: tab for {label(self.sites[i])} was gone, reopening")
            self.tabs[i] = self.new_tab(self.sites[i])
            self.devtools(f"/json/activate/{self.tabs[i]}")
        self.stop_video()  # only now: the page is already in front behind the video
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
                    self.tick()
                self.errors = 0
            except Exception as e:
                self.errors += 1
                log(f"screen {self.index}: {e!r} ({self.errors} in a row)")
                if self.errors >= 3 or not self.tabs:
                    log(f"screen {self.index}: restarting chromium")
                    self.kill()
                time.sleep(5)
            time.sleep(0.5)

    def tick(self):
        """One turn of loop(): move on when the time is up or mpv exited, stop videos whose
        handover is over, and keep up with Chromium's tabs. Caller holds self.lock."""
        self.retire()
        if self.player is not None and self.player.poll() is not None:  # no known length, or it failed
            self.player = None
            if self.paused:  # keep the paused video looping
                self.play(self.sites[self.current])
            else:
                self.show(self.current + 1)
        elif self.tabs and time.monotonic() >= self.next_switch:
            if not self.paused:
                self.show(self.current + 1)
            elif self.player is not None:  # a paused video starts again, over its last frame
                self.play(self.sites[self.current])
        self.follow_tabs()

    def command(self, action, site=None, url=None):
        """Run a phone command. Returns an error message, or None."""
        with self.lock:
            if not self.tabs:
                return "screen is still starting"
            if action in ("show", "add", "remove") and not url:
                return "missing link"
            if action == "show":  # put a link up now, paused, without keeping it
                self.close_temp()
                self.close_popups()
                tab = self.new_tab(url)
                self.devtools(f"/json/activate/{tab}")
                self.temp = (tab, url)
                self.paused = True
                self.stop_video()  # after: the new tab is in front behind the video
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
                    self.close_tab(tab)
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
                    # close the tabs pages opened, and put a tab that was browsed off its site back on it
                    popups = self.close_popups()
                    if not is_video(self.sites[self.current]) and (self.reset_tab(self.current) or popups):
                        self.devtools(f"/json/activate/{self.tabs[self.current]}")
                    self.next_switch = time.monotonic() + self.slot(self.current)
            elif action == "closetab":  # the tab on screen, one at a time; never a rotation site's
                self.follow_tabs()  # a tap may have opened a tab since the loop last looked
                if self.popups:  # back to the tab underneath: the one that opened it, Show now's or the site's
                    self.close_tab(self.popups.pop())
                    if (tab := self.active_tab()) is not None:
                        self.devtools(f"/json/activate/{tab}")
                elif self.temp is not None:
                    self.show(self.current)  # closes it: back to the rotation's site (still paused)
                else:
                    return "this tab is a rotation site's, so it stays open"
                self.follow_tabs()  # so the state names the page on screen now
            else:
                return "unknown command"

    def browse(self, action, data):
        """Run a phone browsing command (BROWSE, body checked by browse_error) on the tab on
        screen, and pause the rotation there. Returns an error message, or None."""
        with self.lock:
            if not self.tabs:
                return "screen is still starting"
            self.follow_tabs()  # a tap may have opened a tab since the loop last looked
            if self.player is not None or self.active_tab() is None:
                return "a video is playing"
            self.cdp(lambda page: page_action(page, action, data))
            self.paused = True

    def capture(self):
        """A small JPEG of what this screen shows: the tab on screen over DevTools when no
        video is on top (fast), else (or when that fails) grim + ffmpeg (~0.5 s on the Pi)."""
        if (ws_connect is not None and self.player is None and self.active_tab() is not None
                and time.monotonic() >= self.devtools_off_until):
            try:
                return self.cdp(page_picture)
            except OSError as e:
                self.devtools_off_until = time.monotonic() + 30  # grim only for a while
                if str(e) not in self.capture_errors:
                    self.capture_errors.add(str(e))
                    log(f"screen {self.index}: DevTools preview failed ({e}), using grim")
        try:  # Debian's grim has no JPEG, so ffmpeg compresses it (~30 KB)
            ppm = subprocess.run(["grim", "-o", self.output, "-s", "0.4", "-t", "ppm", "-"],
                                 capture_output=True, timeout=10).stdout
            return subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "ppm_pipe", "-i", "-",
                 "-q:v", "6", "-f", "mjpeg", "-"],
                input=ppm, capture_output=True, timeout=10).stdout if ppm else b""
        except (OSError, subprocess.TimeoutExpired):
            return b""

    def snapshot(self):
        """A small JPEG of what this screen shows, for the phone page; reused for 2 s."""
        with self.shot_lock:
            if time.monotonic() - self.shot_time > 2:
                self.shot = self.capture()
                self.shot_time = time.monotonic()
            return self.shot

    def preview(self):
        """A fresh snapshot(), for the WebSocket previews (which keep their own pace)."""
        self.shot_time = 0.0
        return self.snapshot()

    def state(self):
        tab, url = self.on_screen
        on_top = tab is not None and tab == self.active_tab() and self.player is None
        return {
            "index": self.index,
            "output": self.output,
            "ready": bool(self.tabs),
            "paused": self.paused,
            "current": self.current,
            "temp": label(self.temp[1]) if self.temp else None,
            # the page on screen now, wherever links took it (None: a video, or not known yet)
            "page": label(url) if url and on_top else None,
            # whether Close tab (closetab) has a tab to close: one a link or Show now opened
            "closable": bool(self.popups or self.temp) and self.player is None,
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
    if action in BROWSE:  # on the tab on screen: {"x", "y"}, {"dy"}, {"text"}, {"key"} or nothing
        error = browse_error(action, data)
        if error:
            return 400, {"error": error}
        d = data if isinstance(data, dict) else {}
        what = {"click": f"{d.get('x')},{d.get('y')}", "scroll": d.get("dy"),
                "type": f"{len(d.get('text') or '')} characters", "key": d.get("key")}.get(action, "")
        log(f"control: {action} {what} on screen {i}" + (f" (via {via})" if via else ""))
        try:
            error = screens[i].browse(action, data)
        except OSError as e:
            error = f"browser didn't respond ({e})"
        return (409, {"error": error}) if error else (200, None)
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


class Uplink:
    """Keeps this Pi connected to the relay server (server_url) and runs the phone commands
    it passes on. The Pi only connects out: to the relay's WebSocket (/pi/ws) when it can,
    else with HTTP syncs (/pi/sync), each one carrying the answers to the last one."""

    def __init__(self, cfg, host, screens, public_urls, stop):
        self.host, self.screens, self.public_urls, self.stop = host, screens, public_urls, stop
        self.server_url = cfg["server_url"].rstrip("/")
        self.ws_url = re.sub(r"^http", "ws", self.server_url) + "/pi/ws"  # https -> wss, http -> ws
        self.headers = {"X-Pi-Key": str(cfg.get("server_pi_key", ""))}
        if cfg.get("server_lab_token"):
            self.headers["X-Lab-Token"] = str(cfg["server_lab_token"])
        # the HTTP sync loop's, kept across its turns between WebSocket tries
        self.results, self.wanted, self.shot_sent = [], [], {}
        self.delay, self.down, self.reasons = 1, True, set()

    def state(self):
        return pi_state(self.host, self.public_urls, self.screens)

    def answer(self, q):
        """Run one command the server passed on; returns its result for the server."""
        m = re.fullmatch(r"/api/screen/(\d+)/([a-z]{1,20})", str(q.get("path")))
        status, payload = 404, {"error": "not found"}
        if q.get("method") == "POST" and m:
            try:
                status, payload = run_command(self.screens, int(m[1]), m[2], str(q.get("query") or ""),
                                              q.get("body"), via="server")
            except Exception as e:  # the control server would drop the connection
                log(f"uplink: {m[2]} on screen {m[1]} failed: {e!r}")
                status, payload = 500, {"error": f"the Pi couldn't do that ({e})"}
        return {"id": q.get("id"), "status": status, "body": payload or self.state()}

    def run(self):
        """The WebSocket, falling back to HTTP syncs after 3 failed tries in a row or at once
        when the server has no WebSocket (an older relay); from there the WebSocket is tried
        again every WS_RETRY s, or as soon as the server answers again after being down."""
        if ws_connect is None:
            log("uplink: python3-websockets isn't installed, using HTTP sync only")
            self.sync_over_http(float("inf"), back_when_up=False)
            return
        fails, fallback, said = 0, False, set()  # said: WebSocket problems logged since it last worked
        while not self.stop.is_set():
            started = time.monotonic()
            status, reason = self.websocket()
            if status == 101 and time.monotonic() - started >= 10:  # it worked, then dropped
                fails, fallback, said, self.delay = 0, False, set(), 1
                continue
            if status == 403:
                if reason not in said:
                    log("uplink: the server refused this Pi's key")
                said.add(reason)
                self.stop.wait(60)
                continue
            old = status in (400, 404, 426)
            if status != 101 and reason not in said:  # (101: it closed right away, logged already)
                log(f"uplink: {self.server_url} has no WebSocket ({reason}), using HTTP sync" if old
                    else f"uplink: can't reach {self.server_url} ({reason}), retrying")
                self.reasons.add(reason)  # the HTTP loop needn't say it again
            said.add(reason)
            fails += 1
            if old or fallback or fails >= 3:
                if not (old or fallback):
                    log(f"uplink: using HTTP sync, trying the WebSocket again every {WS_RETRY:g} s")
                fails, fallback = 0, True
                self.sync_over_http(time.monotonic() + WS_RETRY)
            else:
                self.stop.wait(self.delay)
                self.delay = min(self.delay * 2, 60)

    def websocket(self):
        """One connection to the relay's WebSocket, until it closes. Returns (101, why it
        closed), or (the HTTP status or None, why it couldn't connect)."""
        try:  # this client never follows a redirect: the key only goes to server_url
            ws = ws_connect(self.ws_url, additional_headers=self.headers, open_timeout=15,
                            ping_interval=20, close_timeout=2)
        except Exception as e:  # refused, can't connect, timed out, or not a WebSocket server
            if isinstance(e, InvalidStatus):
                status = e.response.status_code
            else:  # websockets takes only HTTP/1.1 answers; the relay's own errors are HTTP/1.0
                status = (m := re.search(r"HTTP/1\.0 (\d{3})", str(e.__cause__))) and int(m[1])
            return status, f"HTTP {status}" if status else str(e) or type(e).__name__
        log(f"uplink: connected to {self.server_url} over WebSocket")
        lock, closed, wake = threading.Lock(), threading.Event(), threading.Event()
        want, why = [], []

        def send(msg):
            with lock:
                ws.send(msg if isinstance(msg, bytes) else json.dumps(msg))

        def end(e):
            why.append(e)
            closed.set()
            wake.set()

        def reader():  # runs the commands, one at a time, and takes in what the phones watch
            nonlocal want
            try:
                for raw in ws:
                    try:
                        msg = json.loads(raw) if isinstance(raw, str) else None
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") == "command":
                        send({"type": "result", **self.answer(msg)})
                        wake.set()  # a fresh preview now
                    elif msg.get("type") == "want" and isinstance(msg.get("screens"), list):
                        want = [i for i in msg["screens"] if type(i) is int and 0 <= i < len(self.screens)]
                        wake.set()
                end(None)
            except Exception as e:
                end(e)

        def previews():  # binary [screen index][JPEG], each wanted screen at most every PREVIEW_GAP s
            taken = {}
            try:
                while not closed.is_set():
                    wake.clear()
                    now, wanted = time.monotonic(), want
                    due = [i for i in wanted if now - taken.get(i, -PREVIEW_GAP) >= PREVIEW_GAP]
                    for i in due:
                        taken[i] = time.monotonic()
                        shot = self.screens[i].preview()
                        if shot and len(shot) < 2**20:
                            send(bytes([i]) + shot)
                    if not due:
                        wake.wait(min((taken[i] + PREVIEW_GAP - now for i in wanted), default=None))
            except Exception as e:
                end(e)

        try:
            state = self.state()
            send({"type": "hello", "host": self.host, "state": state})
            last, last_sent = json.dumps(state), time.monotonic()
            threading.Thread(target=reader, daemon=True).start()
            threading.Thread(target=previews, daemon=True).start()
            while not self.stop.is_set() and not closed.wait(0.25):
                state = self.state()
                if (text := json.dumps(state)) != last or time.monotonic() - last_sent >= STATE_EVERY:
                    send({"type": "state", "state": state})
                    last, last_sent = text, time.monotonic()
        except Exception as e:
            end(e)
        finally:
            ws.close()
            end(None)
            self.down = True
        error = next((e for e in why if e is not None), None)
        reason = ((str(error) or type(error).__name__) if error
                  else f"closed with {ws.close_code}" if ws.close_code else "closed")
        if not self.stop.is_set():
            log(f"uplink: lost the WebSocket to {self.server_url} ({reason}), reconnecting")
        return 101, reason

    def sync_over_http(self, until, back_when_up=True):
        """The HTTP sync loop, until time.monotonic() reaches `until` or (back_when_up) the
        server answers again after failing, with no results left to deliver."""
        headers = {"Content-Type": "application/json", **self.headers}
        failed = False
        while not self.stop.is_set():
            if (time.monotonic() >= until or (failed and back_when_up and not self.down)) and not self.results:
                return
            shots = {}
            for i in self.wanted:  # previews a phone is looking at, each at most every 2 s
                if ((i not in self.shot_sent or time.monotonic() - self.shot_sent[i] >= 2)
                        and (shot := self.screens[i].snapshot())):
                    shots[str(i)] = base64.b64encode(shot).decode()
                    self.shot_sent[i] = time.monotonic()
            body = json.dumps({"host": self.host, "state": self.state(),
                               "results": self.results, "shots": shots}).encode()
            started = time.monotonic()
            try:
                req = urllib.request.Request(self.server_url + "/pi/sync", body, headers, method="POST")
                with NO_REDIRECTS.open(req, timeout=30) as r:
                    reply = json.loads(r.read())
                requests = [q for q in reply.get("requests") or [] if isinstance(q, dict)]
                self.wanted = [i for i in reply.get("want_shots") or []
                               if type(i) is int and 0 <= i < len(self.screens)]
            except Exception as e:  # can't connect, timed out, an HTTP error, or not a sync reply
                refused = isinstance(e, urllib.error.HTTPError) and e.code == 403
                reason = (str(e) if isinstance(e, urllib.error.HTTPError)
                          else str(getattr(e, "reason", "") or e) or type(e).__name__)
                if reason not in self.reasons:
                    self.reasons.add(reason)
                    log("uplink: the server refused this Pi's key" if refused
                        else f"uplink: can't reach {self.server_url} ({reason}), retrying")
                self.down, self.wanted, failed = True, [], True  # results are kept for the next sync
                self.delay = 60 if refused else self.delay
                self.stop.wait(self.delay)
                self.delay = min(self.delay * 2, 60)
                continue
            if self.down:
                log(f"uplink: connected to {self.server_url}")
            self.down, self.delay, self.reasons, self.results = False, 1, set(), []
            for q in requests:
                self.results.append(self.answer(q))
            del self.results[:-20]
            if not requests:  # the server holds each sync ~2 s; one that doesn't mustn't make this spin
                self.stop.wait(max(0.0, 0.5 - (time.monotonic() - started)))


def uplink(cfg, host, screens, public_urls, stop=None):
    """Keep this Pi connected to the relay server (server_url) and run the phone commands
    it passes on (see Uplink). Setting `stop` (a threading.Event; only the tests use it)
    ends it."""
    Uplink(cfg, host, screens, public_urls, stop or threading.Event()).run()


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
