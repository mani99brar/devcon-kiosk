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

Pis and phones may instead keep a WebSocket open (GET /pi/ws with X-Pi-Key,
GET /ws/phone with the access code in the first message), so commands, state
and previews go both ways at once. Both transports share one state per Pi and
polling stays as the fallback. RFC 6455 is hand-rolled below (class WebSocket).
"""
import base64
import binascii
import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import select
import signal
import socket
import struct
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
# s a stored preview is still shown at once to a phone that starts watching: an older one means
# nobody was watching, so the TV may have moved on since (a fresh one comes within a second)
REPLAY_MAX_AGE = 2
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
WS_COMMAND_PATH = re.compile(r"/api/screen/([0-9]{1,4})/([a-z]{1,20})")
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PING_EVERY = 20  # s between the server's pings on each WebSocket
SILENT_AFTER = 50  # s without a frame from the other side before a WebSocket is closed
FIRST_WITHIN = 10  # s for a WebSocket's first message (the Pi's hello, the phone's auth)
WS_SEND_TIMEOUT = 10  # s a write may stall before a WebSocket counts as dead
WS_TICK = 1  # s between ping and silence checks
CLOSE_WAIT = 2  # s the other side gets to finish a close we started
MAX_TEXT = 256 * 1024
MAX_BINARY = 1024 * 1024
MAX_OUTBOX = 500  # messages waiting for a slow WebSocket client before it is dropped
MAX_WATCH = 64  # screens one phone may watch
CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG = 0, 1, 2, 8, 9, 10

ACCESS_CODE = b""  # set by configure()
PI_KEY = b""
PIS = ()
hosts = {}  # host name -> Host
hosts_lock = threading.Lock()
phones = set()  # WebSockets of phones that gave the right code
phones_lock = threading.Lock()  # never wait for a Host's cond while holding this


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
        self.ws = None  # the Pi's WebSocket, after its hello
        self.ws_want = None  # the screens last sent to it in a want message
        self.watchers = {}  # screen -> phone WebSockets watching it
        self.pushed = None  # (online, state) last pushed to the phone WebSockets

    def online(self):
        if self.ws is not None:
            return True
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
                push_preview(h, int(i), jpeg)
        h.shots = {i: s for i, s in h.shots.items() if now - s[1] <= SHOT_MAX_AGE}
        results = data.get("results")
        for r in results if isinstance(results, list) else ():
            rid = r.get("id") if isinstance(r, dict) else None
            if isinstance(rid, str) and rid in h.delivered:
                h.delivered.discard(rid)
                h.results[rid] = (r.get("status"), r.get("body"))
        h.cond.notify_all()
        push_state(h)
        h.cond.wait_for(lambda: h.queue, timeout=SYNC_HOLD)
        requests, h.queue = h.queue, []
        h.delivered.update(r["id"] for r in requests)
        return {"requests": requests, "want_shots": wanted_screens(h)}


def wanted_screens(h):
    """Screens a phone wants previews of: shot.jpg in the last WANT_FOR s or a watching
    WebSocket. Call holding h.cond."""
    now = time.monotonic()
    h.wanted = {i: t for i, t in h.wanted.items() if now - t <= WANT_FOR}
    return sorted(set(h.wanted) | {i for i, viewers in h.watchers.items() if viewers})


def relay(h, req):
    """Send a phone command to a Pi (its WebSocket if open, else the sync queue) and wait
    for its answer: (status, body)."""
    with h.cond:
        if not h.online():
            return 503, {"error": f"{h.name} is not connected"}
        if h.ws is not None:
            if len(h.delivered) >= MAX_QUEUE:
                return 503, {"error": f"{h.name} is busy"}
            h.delivered.add(req["id"])
            h.ws.put({"type": "command", **req})
        elif len(h.queue) >= MAX_QUEUE:
            return 503, {"error": f"{h.name} is busy"}
        else:
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
            push_state(h)
        return status, body


def relay_logged(h, i, action, path, query, body, client, via=""):
    """relay() a phone's command for screen i, with the usual log lines: (status, body)."""
    # the phone's lab token is for the proxy only; the Pi never needs it
    query = "&".join(p for p in query.split("&") if p and p.split("=")[0] != "lab_token")
    log(f"{h.name} screen {i}: {action} (from {client}){via}")
    status, answer = relay(h, {"id": secrets.token_hex(8), "method": "POST", "path": path,
                               "query": query, "body": body})
    if status >= 500 and isinstance(answer, dict) and "error" in answer:
        log(f"{h.name} screen {i}: {action} failed: {answer['error']}{via}")
    return status, answer


def hub_state():
    with hosts_lock:
        found = sorted(hosts.items())
    pis = []
    for name, h in found:
        with h.cond:
            pis.append({"host": name, "base": f"/api/pi/{name}", "online": h.online()})
    return {"hub": True, "host": "server", "peers": [], "pis": pis}


def reap():
    """Log the Pis that stopped syncing, tell the phone WebSockets, and tell Pi WebSockets
    about previews no longer wanted over HTTP."""
    with hosts_lock:
        found = list(hosts.values())
    for h in found:
        with h.cond:
            if h.announced and not h.online():
                h.announced = False
                log(f"{h.name} went offline (no sync for {OFFLINE_AFTER:g} s)")
            push_state(h)
            send_want(h)


def reaper():
    while True:
        time.sleep(1)
        reap()


class Closed(Exception):
    """A WebSocket ends. code: the close frame to send (None when the line is gone)."""

    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def unmask(data, key):
    n = len(data)
    if not n:
        return data
    pad = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(n, "big")


class WebSocket:
    """The server end of one RFC 6455 connection, on the socket of an upgraded request.

    The request's thread reads (next_message), answers pings, sends ours and notices
    silence. Other threads queue messages (put, put_preview) for a writer thread, so nobody
    waits on a slow client; a newer preview replaces an unsent one with the same key.
    Every socket write holds self.lock."""

    def __init__(self, sock, client, pending=b""):
        self.sock, self.client = sock, client
        self.buf = bytearray(pending)  # read from the socket, not yet parsed
        self.lock = threading.Lock()
        self.out = threading.Condition()  # guards queue, previews, done
        self.queue = collections.deque()  # JSON text (bytes) or a preview key, in order
        self.previews = {}  # preview key -> newest frame not yet sent
        self.done = False  # nothing more gets written but a close frame
        self.close_sent = self.peer_closed = False
        self.kind, self.parts = None, []  # a fragmented message being received
        self.heard = self.pinged = time.monotonic()
        self.reason = ""  # why another thread closed it
        threading.Thread(target=self.writer, daemon=True).start()

    def send(self, opcode, payload=b""):
        """One unmasked frame; False if the connection is closing or gone."""
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 1 << 16:
            head = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        with self.lock:
            if self.close_sent:
                return False
            self.close_sent = opcode == CLOSE
            try:
                self.sock.sendall(head + payload)
                return True
            except OSError:  # a stalled write may have left half a frame: drop the line
                self.close_sent = True
                self.kill()
                return False

    def put(self, message):
        """Queue a JSON message (a dict, or text already encoded) for the writer thread."""
        if isinstance(message, dict):
            message = json.dumps(message).encode()
        with self.out:
            if self.done:
                return
            if len(self.queue) >= MAX_OUTBOX:
                self.done, self.reason = True, "too slow to keep up"
                self.kill()
            else:
                self.queue.append(message)
            self.out.notify()

    def put_preview(self, key, frame):
        with self.out:
            if self.done:
                return
            if key not in self.previews:
                self.queue.append(key)
            self.previews[key] = frame
            self.out.notify()

    def writer(self):
        while True:
            with self.out:
                self.out.wait_for(lambda: self.queue or self.done)
                if self.done:
                    return
                item = self.queue.popleft()
                if isinstance(item, bytes):
                    opcode, payload = TEXT, item
                else:
                    opcode, payload = BINARY, self.previews.pop(item)
            if not self.send(opcode, payload):
                return

    def kill(self):
        """Shut the socket, which wakes the reading thread."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def abort(self, reason):
        """Close it from another thread; the reading thread then ends its session."""
        self.reason = reason
        with self.out:
            self.done = True
            self.out.notify()
        self.send(CLOSE, struct.pack("!H", 1000) + reason.encode()[:123])
        self.kill()

    def recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(n - len(self.buf), 1 << 16))
            if not chunk:
                raise Closed(None, "connection closed")
            self.buf += chunk
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def read_frame(self):
        """Read one frame: a whole message comes back as str (text) or bytes (binary), else None."""
        b0, b1 = self.recv_exact(2)
        fin, opcode, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
        if b0 & 0x70 or opcode not in (CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG):
            raise Closed(1002, "bad frame")
        if not b1 & 0x80:
            raise Closed(1002, "client frames must be masked")
        if opcode >= CLOSE and (n > 125 or not fin):
            raise Closed(1002, "bad control frame")
        if n == 126:
            n = struct.unpack("!H", self.recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.recv_exact(8))[0]
        kind = self.kind or opcode
        if opcode < CLOSE:
            if (opcode == CONTINUATION) != (self.kind is not None):
                raise Closed(1002, "bad fragmentation")
            if sum(map(len, self.parts)) + n > (MAX_TEXT if kind == TEXT else MAX_BINARY):
                raise Closed(1009, "message too big")
        key = self.recv_exact(4)
        payload = unmask(self.recv_exact(n), key)
        self.heard = time.monotonic()
        if opcode == PING:
            self.send(PONG, payload)
        elif opcode == CLOSE:
            self.peer_closed = True
            code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 0 if payload else 1000
            valid = 1000 <= code <= 1011 and code not in (1004, 1005, 1006) or 3000 <= code <= 4999
            raise Closed(code if valid else 1002, "closed by the other side")
        elif opcode != PONG:
            self.kind = kind
            self.parts.append(payload)
            if fin:
                message, self.kind, self.parts = b"".join(self.parts), None, []
                if kind == BINARY:
                    return message
                try:
                    return message.decode()
                except UnicodeDecodeError:
                    raise Closed(1007, "text that isn't UTF-8") from None
        return None

    def next_message(self, deadline=None):
        """The next text (str) or binary (bytes) message. Meanwhile pings every PING_EVERY s;
        raises Closed when the other side closes, goes silent, misses the deadline or breaks
        the protocol."""
        try:
            while True:
                now = time.monotonic()
                if now - self.heard >= SILENT_AFTER:
                    raise Closed(1011, f"silent for {SILENT_AFTER:g} s")
                if deadline is not None and now >= deadline:
                    raise Closed(1008, "no first message in time")
                if now - self.pinged >= PING_EVERY:
                    self.pinged = now
                    self.send(PING)
                if self.buf or select.select([self.sock], [], [], WS_TICK)[0]:
                    message = self.read_frame()
                    if message is not None:
                        return message
        except (OSError, ValueError):
            raise Closed(None, "connection lost") from None

    def end(self, code, reason=""):
        """Finish from the reading thread: stop the writer, send our close frame if the line
        is up and, unless that answers the other side's close, give it CLOSE_WAIT to close."""
        with self.out:
            self.done = True
            self.out.notify()
        if code is None:
            return
        payload = struct.pack("!H", code) + (b"" if self.peer_closed else reason.encode()[:123])
        if not self.send(CLOSE, payload) or self.peer_closed:
            return
        try:
            self.sock.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + CLOSE_WAIT
            while (left := deadline - time.monotonic()) > 0 and select.select([self.sock], [], [], left)[0]:
                if not self.sock.recv(1 << 16):
                    break
        except (OSError, ValueError):
            pass


def json_message(message):
    """A WebSocket text message as a dict; {} for binary, bad JSON or anything but an object."""
    if not isinstance(message, str):
        return {}
    try:
        data = json.loads(message)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def preview_frame(name, i, jpeg):
    """A preview as phones get it: [len(host)][host][screen][JPEG]."""
    return bytes([len(name)]) + name.encode() + bytes([i]) + jpeg


def push_preview(h, i, jpeg):
    """Hand a new preview to the phones watching that screen. Call holding h.cond."""
    if h.watchers.get(i) and i < 256:
        frame = preview_frame(h.name, i, jpeg)
        for ws in h.watchers[i]:
            ws.put_preview((h.name, i), frame)


def push_state(h):
    """Tell every phone WebSocket if h's state or online status changed. Call holding h.cond."""
    online = h.online()
    now = (online, h.state if online else None)
    if now == h.pushed:
        return
    h.pushed = now
    text = json.dumps({"type": "state", "host": h.name, "online": online, "state": now[1]}).encode()
    with phones_lock:
        found = list(phones)
    for ws in found:
        ws.put(text)


def send_want(h):
    """Tell the Pi's WebSocket which previews are wanted, if that changed. Call holding h.cond."""
    if h.ws is not None and (screens := wanted_screens(h)) != h.ws_want:
        h.ws_want = screens
        h.ws.put({"type": "want", "screens": screens})


def attach(h, ws, state):
    """Make ws the Pi's WebSocket after its hello; returns the one it replaces, if any."""
    with h.cond:
        if not h.announced or not h.online():
            log(f"{h.name} is online (from {ws.client})")
        old, h.announced, h.ws, h.ws_want = h.ws, True, ws, None
        if isinstance(state, dict):
            h.state = {**state, "peers": []}
        for req in h.queue:  # commands that were waiting for an HTTP sync
            h.delivered.add(req["id"])
            ws.put({"type": "command", **req})
        h.queue = []
        push_state(h)
        send_want(h)
        return old


def detach(h, ws):
    with h.cond:
        if h.ws is not ws:  # replaced by a newer connection
            return
        h.ws = h.ws_want = None
        if h.announced and not h.online():
            h.announced = False
            log(f"{h.name} went offline (WebSocket closed)")
        push_state(h)


def pi_message(h, message):
    """One message from a Pi's WebSocket: a preview (binary), its state or a command's result."""
    if isinstance(message, bytes):
        if len(message) >= 2:
            i, jpeg, now = message[0], message[1:], time.monotonic()
            with h.cond:
                h.shots[i] = (jpeg, now)
                h.shots = {i: s for i, s in h.shots.items() if now - s[1] <= SHOT_MAX_AGE}
                push_preview(h, i, jpeg)
        return
    msg = json_message(message)
    with h.cond:
        if msg.get("type") == "state" and isinstance(msg.get("state"), dict):
            h.state = {**msg["state"], "peers": []}
            push_state(h)
        elif msg.get("type") == "result" and isinstance(rid := msg.get("id"), str) and rid in h.delivered:
            h.delivered.discard(rid)
            h.results[rid] = (msg.get("status"), msg.get("body"))
            h.cond.notify_all()


def add_phone(ws):
    """Greet a phone that gave the right code (hello, then each Pi's state) and start pushing."""
    with hosts_lock:
        found = sorted(hosts.items())
    pis = []
    for name, h in found:
        with h.cond:
            pis.append({"host": name, "online": h.online()})
    ws.put({"type": "hello", "pis": pis})
    with phones_lock:
        phones.add(ws)
    with hosts_lock:  # again: a Pi that appeared meanwhile gets a state message too
        found = sorted(hosts.items())
    for name, h in found:
        with h.cond:
            online = h.online()
            ws.put({"type": "state", "host": name, "online": online, "state": h.state if online else None})


def remove_phone(ws, watching):
    with phones_lock:
        phones.discard(ws)
    for h, i in watching:
        with h.cond:
            viewers = h.watchers.get(i, set())
            viewers.discard(ws)
            if not viewers:
                h.watchers.pop(i, None)
            send_want(h)


def phone_watch(ws, msg, watching):
    """{"type": "watch", "host", "screen", "on"}: start or stop previews of a screen."""
    name, i = msg.get("host"), msg.get("screen")
    h = find_host(name) if isinstance(name, str) else None
    if h is None or type(i) is not int or not 0 <= i <= 255:
        return
    on = msg.get("on") is True and ((h, i) in watching or len(watching) < MAX_WATCH)
    with h.cond:
        viewers = h.watchers.setdefault(i, set())
        if on:
            watching.add((h, i))
            viewers.add(ws)
            shot = h.shots.get(i)
            if shot and time.monotonic() - shot[1] <= REPLAY_MAX_AGE:  # something to show at once
                ws.put_preview((h.name, i), preview_frame(h.name, i, shot[0]))
        else:
            watching.discard((h, i))
            viewers.discard(ws)
        if not viewers:
            del h.watchers[i]
        send_want(h)


def phone_command(ws, msg):
    """{"type": "command", "id", "host", "path", "query", "body"} from a phone's WebSocket,
    validated like the HTTP relay and answered with a result message. Runs in its own thread."""
    name, path, query, body = msg.get("host"), msg.get("path"), msg.get("query"), msg.get("body")
    h = find_host(name) if isinstance(name, str) else None
    m = WS_COMMAND_PATH.fullmatch(path) if isinstance(path, str) else None
    if h is None or m is None:
        status, answer = 404, {"error": "not found"}
    elif len(json.dumps(body).encode()) > MAX_COMMAND_BODY:
        status, answer = 413, {"error": "request too large"}
    else:
        status, answer = relay_logged(h, m[1], m[2], path, query if isinstance(query, str) else "",
                                      body, ws.client, " (ws)")
    ws.put({"type": "result", "id": msg.get("id"), "status": status, "body": answer})


class Relay(BaseHTTPRequestHandler):
    timeout = 30  # s for reading a request or writing an answer; holds don't count

    def version_string(self):
        return "kiosk-relay"

    def end_headers(self):
        if self.protocol_version == "HTTP/1.1":  # only a refused WebSocket handshake answers in 1.1
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def reply(self, status, body, ctype="application/json", headers=()):
        if ctype == "application/json":
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype if ctype.startswith("image/") else f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
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
                    send_want(h)
                if shot and time.monotonic() - shot[1] <= SHOT_MAX_AGE:
                    self.reply(200, shot[0], "image/jpeg")
                else:
                    self.reply(503, {"error": "no picture yet"})
        elif path in ("/pi/ws", "/ws/phone"):
            self.websocket(path)
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
        self.reply(*relay_logged(h, i, action, path, query, body, self.client()))

    def websocket(self, path):
        """GET /pi/ws or /ws/phone: the RFC 6455 handshake, then this thread serves the socket."""
        self.protocol_version = "HTTP/1.1"  # websockets clients can't read the status of a 1.0 answer

        def tokens(name):
            return {t.strip().lower() for v in self.headers.get_all(name, ()) for t in v.split(",")}
        if "websocket" not in tokens("Upgrade") or "upgrade" not in tokens("Connection"):
            return self.reply(426, {"error": "this address only takes WebSocket connections"},
                              headers=(("Upgrade", "websocket"),))
        if self.headers.get("Sec-WebSocket-Version", "").strip() != "13":
            return self.reply(426, {"error": "unsupported WebSocket version"},
                              headers=(("Sec-WebSocket-Version", "13"),))
        key = self.headers.get("Sec-WebSocket-Key", "").strip()
        try:
            good_key = len(base64.b64decode(key, validate=True)) == 16
        except (binascii.Error, ValueError):
            good_key = False
        if not good_key:
            return self.reply(400, {"error": "bad WebSocket handshake"})
        if path == "/pi/ws" and not self.allowed("X-Pi-Key", PI_KEY, "wrong Pi key"):
            return
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        self.close_connection = True
        try:
            self.wfile.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        except OSError:
            return
        ws = WebSocket(self.connection, self.client(), self.early_frames())
        (self.pi_socket if path == "/pi/ws" else self.phone_socket)(ws)

    def early_frames(self):
        """Bytes the client sent right behind its handshake, already in rfile's buffer."""
        try:
            self.connection.setblocking(False)
            return self.rfile.read1(1 << 20) or b""
        except OSError:
            return b""
        finally:
            self.connection.settimeout(WS_SEND_TIMEOUT)

    def pi_socket(self, ws):
        h, code, reason = None, None, ""
        try:
            hello = json_message(ws.next_message(time.monotonic() + FIRST_WITHIN))
            name = hello.get("host")
            if hello.get("type") != "hello" or not isinstance(name, str) or not HOST_RE.fullmatch(name):
                raise Closed(1008, "the first message must be a hello with a good host name")
            if (h := find_host(name, create=True)) is None:
                raise Closed(1008, "too many Pis")
            old = attach(h, ws, hello.get("state"))
            log(f"{name} WebSocket connected (from {ws.client})")
            if old is not None:
                old.abort("replaced by a newer connection")
            while True:
                pi_message(h, ws.next_message())
        except Closed as c:
            code, reason = c.code, c.reason
        finally:
            if h is None:
                log(f"Pi WebSocket from {ws.client} closed before its hello: {reason}")
            else:
                log(f"{h.name} WebSocket disconnected (from {ws.client}): {ws.reason or reason}")
                detach(h, ws)
            ws.end(code, reason)

    def phone_socket(self, ws):
        watching, joined, code, reason = set(), False, None, ""
        try:
            auth = json_message(ws.next_message(time.monotonic() + FIRST_WITHIN))
            if auth.get("type") != "auth":
                raise Closed(1008, "the first message must be auth")
            given = auth.get("code")
            if not (ACCESS_CODE and isinstance(given, str)
                    and hmac.compare_digest(given.encode(errors="replace"), ACCESS_CODE)):
                log(f"wrong access code from {ws.client}")
                time.sleep(WRONG_AUTH_DELAY)  # slow down guessing
                ws.send(TEXT, json.dumps({"type": "error", "error": "wrong access code"}).encode())
                raise Closed(4003, "wrong access code")
            log(f"phone WebSocket connected (from {ws.client})")
            joined = True
            add_phone(ws)
            while True:
                msg = json_message(ws.next_message())
                if msg.get("type") == "command":  # waits for the Pi, so not on this thread
                    threading.Thread(target=phone_command, args=(ws, msg), daemon=True).start()
                elif msg.get("type") == "watch":
                    phone_watch(ws, msg, watching)
        except Closed as c:
            code, reason = c.code, c.reason
        finally:
            if joined:
                log(f"phone WebSocket disconnected (from {ws.client}): {ws.reason or reason}")
                remove_phone(ws, watching)
            elif code != 4003:
                log(f"phone WebSocket from {ws.client} closed before auth: {reason}")
            ws.end(code, reason)

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
