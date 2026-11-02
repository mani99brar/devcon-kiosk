#!/usr/bin/env python3
"""Relay between the booth kiosks and the team's phones, independent of Tailscale.

  ACCESS_CODE=... PI_KEY=... [PIS=kleros1,kleros2] python3 app.py

Each Pi POSTs /pi/sync (X-Pi-Key) every couple of seconds with its state, the
answers to earlier commands and any previews a phone asked for. The server
holds that request up to SYNC_HOLD seconds and answers with the phone commands
queued for the Pi. Phones load the same control.html as on a Pi; it sees
"hub" in /api/state and reaches each Pi under /api/pi/<host>/ (X-Code).
Plain HTTP on port 8000: TLS and the lab tokens are checked by the proxy in
front. Everything is kept in memory. Stdlib only.
"""
import base64
import binascii
import hmac
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# next to app.py in the image; pi/control.html when run from the repo
CONTROL_PAGES = (HERE / "control.html", HERE.parent / "pi" / "control.html")
PORT = 8000
SYNC_HOLD = 2  # s a Pi's sync waits for phone commands before answering
OFFLINE_AFTER = 10  # s without a sync before a Pi counts as offline
REPLY_TIMEOUT = 15  # s a phone command waits for the Pi's answer
WANT_FOR = 10  # s a preview counts as wanted after a phone asked for it
SHOT_MAX_AGE = 15  # s a stored preview is still served
WRONG_AUTH_DELAY = 1  # s, slows down guessing
MAX_SYNC_BODY = 4 * 1024 * 1024
MAX_COMMAND_BODY = 4096
MAX_SHOT = 1024 * 1024
MAX_QUEUE = 20  # phone commands waiting for one Pi
MAX_HOSTS = 32
MIN_CODE_LEN = 16
MIN_KEY_LEN = 32
HOST_RE = re.compile(r"[a-z0-9-]{1,63}")
STATE_PATH = re.compile(r"/api/pi/([a-z0-9-]{1,63})/api/state")
SHOT_PATH = re.compile(r"/api/pi/([a-z0-9-]{1,63})/api/screen/([0-9]{1,4})/shot\.jpg")
COMMAND_PATH = re.compile(r"/api/pi/([a-z0-9-]{1,63})(/api/screen/([0-9]{1,4})/([a-z]{1,20}))")

ACCESS_CODE = b""  # set by configure()
PI_KEY = b""
PIS = ()
hosts = {}  # host name -> Host
hosts_lock = threading.Lock()


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, flush=True)


class Host:
    """What the relay knows about one Pi. Everything here is guarded by cond."""

    def __init__(self, name):
        self.name = name
        self.cond = threading.Condition()
        self.state = None  # the Pi's own /api/state, with peers = []
        self.last_seen = None  # monotonic time of the last sync
        self.announced = False  # logged as online
        self.shots = {}  # screen -> (JPEG, time)
        self.wanted = {}  # screen -> when a phone last asked for its preview
        self.queue = []  # phone commands not yet handed to the Pi
        self.delivered = set()  # ids handed to the Pi, waiting for its answer
        self.results = {}  # id -> (status, body)

    def online(self):
        return self.last_seen is not None and time.monotonic() - self.last_seen < OFFLINE_AFTER


def configure(access_code, pi_key, pis=()):
    """Set the secrets and the Pis listed before they connect. Forgets every Pi."""
    global ACCESS_CODE, PI_KEY, PIS
    ACCESS_CODE, PI_KEY, PIS = access_code.encode(), pi_key.encode(), tuple(pis)
    with hosts_lock:
        hosts.clear()
        hosts.update((name, Host(name)) for name in PIS)


def find_host(name, create=False):
    with hosts_lock:
        if create and name not in hosts and len(hosts) < MAX_HOSTS:
            hosts[name] = Host(name)
        return hosts.get(name)


def control_page():
    for path in CONTROL_PAGES:
        try:
            return path.read_bytes()
        except OSError:
            pass
    return None


def decode_shot(text):
    """A preview JPEG sent by a Pi, or None if it isn't base64 or is too big."""
    if not isinstance(text, str) or len(text) > MAX_SHOT * 4 // 3 + 4:
        return None
    try:
        jpeg = base64.b64decode(text)
    except (binascii.Error, ValueError):
        return None
    return jpeg if 0 < len(jpeg) <= MAX_SHOT else None


def sync(h, data, client):
    """Take in one sync from a Pi, wait up to SYNC_HOLD for commands, return the answer."""
    now = time.monotonic()
    with h.cond:
        if not h.announced or not h.online():
            log(f"{h.name} is online (from {client})")
        h.announced, h.last_seen = True, now
        if isinstance(data.get("state"), dict):
            h.state = {**data["state"], "peers": []}
        shots = data.get("shots")
        for i, text in shots.items() if isinstance(shots, dict) else ():
            if re.fullmatch(r"[0-9]{1,4}", i) and (jpeg := decode_shot(text)):
                h.shots[int(i)] = (jpeg, now)
        h.shots = {i: s for i, s in h.shots.items() if now - s[1] <= SHOT_MAX_AGE}
        results = data.get("results")
        for r in results if isinstance(results, list) else ():
            rid = r.get("id") if isinstance(r, dict) else None
            if isinstance(rid, str) and rid in h.delivered:
                h.delivered.discard(rid)
                h.results[rid] = (r.get("status"), r.get("body"))
        h.cond.notify_all()
        h.cond.wait_for(lambda: h.queue, timeout=SYNC_HOLD)
        requests, h.queue = h.queue, []
        h.delivered.update(r["id"] for r in requests)
        now = time.monotonic()
        h.wanted = {i: t for i, t in h.wanted.items() if now - t <= WANT_FOR}
        return {"requests": requests, "want_shots": sorted(h.wanted)}


def relay(h, req):
    """Queue a phone command for a Pi and wait for its answer: (status, body)."""
    with h.cond:
        if not h.online():
            return 503, {"error": f"{h.name} is not connected"}
        if len(h.queue) >= MAX_QUEUE:
            return 503, {"error": f"{h.name} is busy"}
        h.queue.append(req)
        h.cond.notify_all()
        if not h.cond.wait_for(lambda: req["id"] in h.results, timeout=REPLY_TIMEOUT):
            h.queue = [r for r in h.queue if r is not req]
            h.delivered.discard(req["id"])
            return 504, {"error": f"{h.name} didn't answer"}
        status, body = h.results.pop(req["id"])
        if not isinstance(status, int) or not 200 <= status <= 599:
            return 502, {"error": f"{h.name} sent a bad answer"}
        if status == 200 and isinstance(body, dict) and "screens" in body:
            body = h.state = {**body, "peers": []}
        return status, body


def hub_state():
    with hosts_lock:
        found = sorted(hosts.items())
    pis = []
    for name, h in found:
        with h.cond:
            pis.append({"host": name, "base": f"/api/pi/{name}", "online": h.online()})
    return {"hub": True, "host": "server", "peers": [], "pis": pis}


def reap():
    """Log the Pis that stopped syncing."""
    with hosts_lock:
        found = list(hosts.values())
    for h in found:
        with h.cond:
            if h.announced and not h.online():
                h.announced = False
                log(f"{h.name} went offline (no sync for {OFFLINE_AFTER:g} s)")


def reaper():
    while True:
        time.sleep(1)
        reap()


class Relay(BaseHTTPRequestHandler):
    timeout = 30  # s for reading a request or writing an answer; holds don't count

    def version_string(self):
        return "kiosk-relay"

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def reply(self, status, body, ctype="application/json"):
        if ctype == "application/json":
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype if ctype.startswith("image/") else f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:  # the client gave up waiting
            pass

    def client(self):
        """The first X-Forwarded-For address (set by the proxy), else the peer's."""
        fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return re.sub(r"[^0-9A-Za-z.:\[\]-]", "", fwd)[:64] or self.client_address[0]

    def allowed(self, header, secret, error):
        if secret and hmac.compare_digest(self.headers.get(header, "").encode(), secret):
            return True
        log(f"{error} from {self.client()}")
        time.sleep(WRONG_AUTH_DELAY)  # slow down guessing
        self.reply(403, {"error": error})
        return False

    def phone(self):
        return self.allowed("X-Code", ACCESS_CODE, "wrong access code")

    def known(self, name):
        h = find_host(name)
        if h is None:
            self.reply(404, {"error": "not found"})
        return h

    def read_body(self, limit):
        """The request body, or None after answering 413. A missing or bad length means empty."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > limit:
            self.reply(413, {"error": "request too large"})
            return None
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            self.reply(200, "ok", "text/plain")
        elif path == "/":
            page = control_page()
            if page is None:
                self.reply(500, {"error": "control.html is missing next to app.py"})
            else:
                self.reply(200, page, "text/html")
        elif path == "/api/state":
            if self.phone():
                self.reply(200, hub_state())
        elif m := STATE_PATH.fullmatch(path):
            if self.phone() and (h := self.known(m[1])):
                with h.cond:
                    state = h.state if h.online() else None
                if state is None:
                    self.reply(503, {"error": f"{h.name} is not connected"})
                else:
                    self.reply(200, state)
        elif m := SHOT_PATH.fullmatch(path):
            if self.phone() and (h := self.known(m[1])):
                with h.cond:
                    h.wanted[int(m[2])] = time.monotonic()
                    shot = h.shots.get(int(m[2]))
                if shot and time.monotonic() - shot[1] <= SHOT_MAX_AGE:
                    self.reply(200, shot[0], "image/jpeg")
                else:
                    self.reply(503, {"error": "no picture yet"})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/pi/sync":
            self.pi_sync()
        elif m := COMMAND_PATH.fullmatch(parts.path):
            self.relay_command(m[1], m[2], m[3], m[4], parts.query)
        else:
            self.reply(404, {"error": "not found"})

    def pi_sync(self):
        if not self.allowed("X-Pi-Key", PI_KEY, "wrong Pi key"):
            return
        raw = self.read_body(MAX_SYNC_BODY)
        if raw is None:
            return
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):
            return self.reply(400, {"error": "bad JSON"})
        name = data.get("host") if isinstance(data, dict) else None
        if not isinstance(name, str) or not HOST_RE.fullmatch(name):
            return self.reply(400, {"error": "bad host name"})
        h = find_host(name, create=True)
        if h is None:
            return self.reply(503, {"error": "too many Pis"})
        self.reply(200, sync(h, data, self.client()))

    def relay_command(self, name, path, i, action, query):  # self.command is the HTTP method
        # /api/pi/<host>/api/screen/<i>/<action>[?site=<n>], relayed to that Pi
        if not self.phone() or not (h := self.known(name)):
            return
        raw = self.read_body(MAX_COMMAND_BODY)
        if raw is None:
            return
        try:
            body = json.loads(raw) if raw else None
        except (ValueError, RecursionError):
            body = None
        # the phone's lab token is for the proxy only; the Pi never needs it
        query = "&".join(p for p in query.split("&") if p and p.split("=")[0] != "lab_token")
        log(f"{name} screen {i}: {action} (from {self.client()})")
        status, answer = relay(h, {"id": secrets.token_hex(8), "method": "POST", "path": path,
                                   "query": query, "body": body})
        if status >= 500 and isinstance(answer, dict) and "error" in answer:
            log(f"{name} screen {i}: {action} failed: {answer['error']}")
        self.reply(status, answer)

    def log_message(self, *args):  # request lines carry the lab token; never log them
        pass


def make_server(port, bind="0.0.0.0"):
    return ThreadingHTTPServer((bind, port), Relay)


def main():
    code = os.environ.get("ACCESS_CODE", "").strip()
    key = os.environ.get("PI_KEY", "").strip()
    pis = [p.strip() for p in os.environ.get("PIS", "").split(",") if p.strip()]
    problems = []
    if len(code) < MIN_CODE_LEN:
        problems.append(f"ACCESS_CODE is {'too short' if code else 'missing'}: "
                        f"it needs at least {MIN_CODE_LEN} characters (the team's code from secret.toml)")
    if len(key) < MIN_KEY_LEN:
        problems.append(f"PI_KEY is {'too short' if key else 'missing'}: "
                        f"it needs at least {MIN_KEY_LEN} characters (server_pi_key in secret.toml)")
    problems += [f"PIS: {p!r} is not a host name (lowercase letters, digits and -)"
                 for p in pis if not HOST_RE.fullmatch(p)]
    if problems:
        for p in problems:
            log("can't start:", p)
        log("fill these in .env (see .env.example), then run ./start.sh again")
        sys.exit(1)
    configure(code, key, pis)
    try:
        server = make_server(PORT)
    except OSError as e:
        log(f"can't start: port {PORT}: {e}")
        sys.exit(1)
    if control_page() is None:
        log("WARNING: control.html is missing next to app.py; / will answer 500")
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # docker stop
    threading.Thread(target=reaper, daemon=True).start()
    log(f"relay server on port {PORT}, PIS: {', '.join(pis) or '(none)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")


if __name__ == "__main__":
    main()
