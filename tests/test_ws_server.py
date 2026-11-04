"""The relay's WebSocket side (server/app.py): its hand-rolled RFC 6455 framing against raw
sockets and against the real websockets client (the library the Pis use), and the one
state per Pi that WebSocket and HTTP Pis and phones share. Short timings are patched in;
nothing real is started and nothing is written to disk.

  python3 -m unittest tests.test_ws_server -v      (from the repo root; about 30 s)

It needs the websockets package (Debian's python3-websockets on the Pis) and skips without
it. KIOSK_TEST_LOGS=1 also prints the relay's log lines as they happen.
"""
import base64
import http.client
import importlib.util
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from websockets.sync.client import connect
except ImportError:
    connect = None

sys.dont_write_bytecode = True  # no __pycache__ in server/
REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("relay_app_ws", REPO / "server" / "app.py")
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

# placeholders, not the real secrets
CODE = "test-access-code-0000"
PI_KEY = "test-pi-key-" + "0" * 24
WRONG_KEY = "test-wrong-pi-key-" + "1" * 18
LAB_TOKEN = "test-pi-lab-token"
PHONE_TOKEN = "test-phone-lab-token"
SECRETS = (CODE, PI_KEY, WRONG_KEY, LAB_TOKEN, PHONE_TOKEN)
PIS = ["kleros1", "kleros2"]
TIMINGS = {"SYNC_HOLD": 0.3, "OFFLINE_AFTER": 2, "REPLY_TIMEOUT": 2, "WRONG_AUTH_DELAY": 0.2,
           "WANT_FOR": 1, "PING_EVERY": 0.4, "SILENT_AFTER": 1.5, "FIRST_WITHIN": 1,
           "WS_TICK": 0.05, "CLOSE_WAIT": 1}
SAMPLE_KEY, SAMPLE_ACCEPT = "dGhlIHNhbXBsZSBub25jZQ==", "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="  # RFC 6455 1.3
JPEG = b"\xff\xd8\xff\xe0 fake preview \xff\xd9"
TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 1, 2, 8, 9, 10, 0

lines, lines_lock = [], threading.Lock()
port = None
server = None
reaping = threading.Event()  # app.reap() runs only while set, so pushes can't wait for it


def capture(*args):
    line = " ".join(map(str, args))
    with lines_lock:
        lines.append(line)
    if os.environ.get("KIOSK_TEST_LOGS"):
        print("relay:", line, flush=True)


def setUpModule():
    global port, server
    if connect is None:
        raise unittest.SkipTest("the websockets package is not installed (on the Pis it is python3-websockets; "
                                "run this with a python that has it, e.g. a venv with websockets 15)")
    patches = [mock.patch.object(app, "log", capture),
               *(mock.patch.object(app, name, value) for name, value in TIMINGS.items())]
    for p in patches:
        p.start()
        unittest.addModuleCleanup(p.stop)
    app.configure(CODE, PI_KEY, PIS)
    server = app.make_server(0, "127.0.0.1")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()


def reaper():
    while True:
        reaping.wait()
        app.reap()
        time.sleep(0.1)


def tearDownModule():
    server.shutdown()
    server.server_close()
    app.configure(CODE, PI_KEY, ())


def call(method, path, body=None, code=CODE, headers=None):
    """One HTTP request to the relay: (status, body, headers); a JSON body comes back parsed."""
    headers = {**({} if code is None else {"X-Code": code}), **(headers or {})}
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path, body, headers)
        r = conn.getresponse()
        raw = r.read()
    finally:
        conn.close()
    if r.getheader("Content-Type", "").startswith("application/json"):
        raw = json.loads(raw)
    return r.status, raw, r.headers


def sync(payload, key=PI_KEY):
    return call("POST", "/pi/sync", payload, code=None, headers={"X-Pi-Key": key})


def online(host):
    return {p["host"]: p["online"] for p in call("GET", "/api/state")[1]["pis"]}.get(host)


def raw_state(host, current=0):
    return {"host": host, "peers": [f"https://{host}.tail0000.ts.net"],
            "screens": [{"index": 0, "output": "HDMI-A-1", "ready": True, "paused": False,
                         "current": current, "temp": None, "interval": 30,
                         "sites": ["example.org", "example.net"],
                         "urls": ["https://example.org", "https://example.net"]}]}


def seen(state):
    """A Pi's state as the relay passes it on: peers removed."""
    return {**state, "peers": []}


def wait_until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while not (result := check()) and time.monotonic() < deadline:
        time.sleep(0.02)
    return result


def frame(opcode, payload=b"", fin=True, mask=True):
    """A client frame, masked unless told otherwise."""
    b0, n, bit = (0x80 if fin else 0) | opcode, len(payload), 0x80 if mask else 0
    if n < 126:
        head = bytes([b0, bit | n])
    elif n < 1 << 16:
        head = bytes([b0, bit | 126]) + struct.pack("!H", n)
    else:
        head = bytes([b0, bit | 127]) + struct.pack("!Q", n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + app.unmask(payload, key)


def auth(code=CODE, **extra):
    return json.dumps({"type": "auth", "code": code, **extra}).encode()


def parse_preview(data):
    n = data[0]
    return data[1:1 + n].decode(), data[1 + n], data[2 + n:]


class Raw:
    """A WebSocket client on a plain socket, for the frame rules the library won't break."""

    def __init__(self, path="/ws/phone", headers=None, key=SAMPLE_KEY, early=b"", rcvbuf=None):
        self.sock = socket.socket()
        if rcvbuf:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        self.sock.settimeout(5)
        self.sock.connect(("127.0.0.1", port))
        request = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket",
                   "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                   *(f"{k}: {v}" for k, v in (headers or {}).items())]
        self.sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode() + early)
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        self.head = head.decode()
        self.status = int(self.head.split()[1])

    def header(self, name):
        for line in self.head.split("\r\n")[1:]:
            k, _, v = line.partition(":")
            if k.strip().lower() == name.lower():
                return v.strip()
        return None

    def send(self, opcode, payload=b"", **kwargs):
        self.sock.sendall(frame(opcode, payload, **kwargs))

    def recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("the server closed the connection")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def frame(self):
        """(fin, opcode, payload, length marker 0..127) of the server's next frame."""
        b0, b1 = self.recv_exact(2)
        assert not b1 & 0x80, "a server frame was masked"
        n = marker = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self.recv_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.recv_exact(8))[0]
        return b0 & 0x80, b0 & 0x0F, self.recv_exact(n), marker

    def recv(self):
        """The next data or close frame: (opcode, payload, marker); answers pings."""
        while True:
            fin, opcode, payload, marker = self.frame()
            if opcode == PING:
                self.send(PONG, payload)
            elif opcode != PONG:
                return opcode, payload, marker

    def json(self):
        opcode, payload, _ = self.recv()
        assert opcode == TEXT, (opcode, payload)
        return json.loads(payload)

    def close_code(self):
        """Read on until the server's close frame; its code."""
        while True:
            opcode, payload, _ = self.recv()
            if opcode == CLOSE:
                return struct.unpack("!H", payload[:2])[0] if payload else None

    def eof(self, timeout=3):
        """True when the server closes the TCP connection within timeout."""
        self.sock.settimeout(timeout)
        try:
            while self.sock.recv(65536):
                pass
            return True
        except (socket.timeout, ConnectionResetError):
            return False

    def login(self):
        self.send(TEXT, auth())
        hello = self.json()
        assert hello["type"] == "hello", hello
        for _ in hello["pis"]:
            self.json()  # one state per Pi
        return hello

    def close(self):
        self.sock.close()


class WsPi:
    """A Pi on /pi/ws through the real websockets client. It answers `next` by moving on a
    site (as kiosk.py would) unless answer is False, and records commands and want messages."""

    def __init__(self, host="kleros1", key=PI_KEY, answer=True, hello=True):
        self.host, self.answer, self.state = host, answer, raw_state(host)
        self.commands, self.wants, self.closed, self.cond = [], [], None, threading.Condition()
        self.ws = connect(f"ws://127.0.0.1:{port}/pi/ws", open_timeout=5, close_timeout=2,
                          additional_headers={"X-Pi-Key": key, "X-Lab-Token": LAB_TOKEN})
        if hello:
            self.ws.send(json.dumps({"type": "hello", "host": host, "state": self.state}))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while True:
                msg = json.loads(self.ws.recv())
                with self.cond:
                    (self.commands if msg["type"] == "command" else self.wants).append(msg)
                    self.cond.notify_all()
                if msg["type"] == "command" and self.answer:
                    if msg["path"] == "/api/screen/0/next":
                        self.state = raw_state(self.host, self.state["screens"][0]["current"] + 1)
                        status, body = 200, self.state
                    else:
                        status, body = 409, {"error": "unknown command"}
                    self.ws.send(json.dumps({"type": "result", "id": msg["id"], "status": status, "body": body}))
        except ConnectionClosed as e:
            with self.cond:
                self.closed = e.rcvd.code if e.rcvd else "no close frame"
                self.cond.notify_all()

    def wait_want(self, screens, timeout=5):
        with self.cond:
            ok = self.cond.wait_for(lambda: self.wants and self.wants[-1]["screens"] == screens, timeout)
        assert ok, f"{self.host} wanted {self.wants[-1:] or 'nothing'}, not {screens}"

    def wait_closed(self, timeout=5):
        with self.cond:
            self.cond.wait_for(lambda: self.closed is not None, timeout)
        return self.closed

    def preview(self, i, jpeg):
        self.ws.send(bytes([i]) + jpeg)

    def close(self):
        self.ws.close()
        self.thread.join(5)


class HttpPi(threading.Thread):
    """A Pi on the old HTTP sync, raw from the test: answers `next`, uploads previews it is asked for."""

    def __init__(self, host="kleros2"):
        super().__init__(daemon=True)
        self.host, self.state, self.stop, self.seen, self.wants = host, raw_state(host), threading.Event(), [], []
        self.start()

    def run(self):
        results, want = [], []
        while not self.stop.is_set():
            shots = {str(i): base64.b64encode(JPEG + self.host.encode()).decode() for i in want}
            status, reply, _ = sync({"host": self.host, "state": self.state, "results": results, "shots": shots})
            results = []
            if status != 200:
                time.sleep(0.2)
                continue
            want = reply["want_shots"]
            self.wants.append(want)
            for req in reply["requests"]:
                self.seen.append(req)
                self.state = raw_state(self.host, self.state["screens"][0]["current"] + 1)
                results.append({"id": req["id"], "status": 200, "body": self.state})

    def close(self):
        self.stop.set()
        self.join(5)


class Phone:
    """The hub page on /ws/phone through the real websockets client."""

    def __init__(self, code=CODE):
        self.ws = connect(f"ws://127.0.0.1:{port}/ws/phone?lab_token={PHONE_TOKEN}", open_timeout=5,
                          close_timeout=2)
        self.messages, self.previews, self.closed, self.cond = [], [], None, threading.Condition()
        self.ws.send(json.dumps({"type": "auth", "code": code}))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while True:
                data = self.ws.recv()
                with self.cond:
                    if isinstance(data, bytes):
                        self.previews.append(parse_preview(data))
                    else:
                        self.messages.append(json.loads(data))
                    self.cond.notify_all()
        except ConnectionClosed as e:
            with self.cond:
                self.closed = e.rcvd.code if e.rcvd else "no close frame"
                self.cond.notify_all()

    def send(self, **msg):
        self.ws.send(json.dumps(msg))

    def wait(self, check, timeout=5, what="message"):
        """The first message (or preview, or close code) for which check() is true."""
        def find():
            for m in self.messages if what == "message" else self.previews:
                if check(m):
                    return m
        with self.cond:
            found = self.cond.wait_for(find, timeout)
        assert found, f"no matching {what}; got {self.messages if what == 'message' else self.previews}"
        return found

    def state_of(self, host, online=True, timeout=5):
        return self.wait(lambda m: m["type"] == "state" and m["host"] == host and m["online"] is online,
                         timeout)

    def wait_closed(self, timeout=5):
        with self.cond:
            self.cond.wait_for(lambda: self.closed is not None, timeout)
        return self.closed

    def command(self, rid, host="kleros1", path="/api/screen/0/next", query="", body=None):
        self.send(type="command", id=rid, host=host, path=path, query=query, body=body)
        return self.wait(lambda m: m["type"] == "result" and m["id"] == rid)

    def watch(self, host, screen, on=True):
        self.send(type="watch", host=host, screen=screen, on=on)

    def close(self):
        self.ws.close()
        self.thread.join(5)


class WsTest(unittest.TestCase):

    def setUp(self):
        app.configure(CODE, PI_KEY, PIS)
        with lines_lock:
            self.start = len(lines)
        self.addCleanup(reaping.clear)

    def reap(self):
        """Let the server's once-a-second reaper run from now on (offline Pis, expired wants)."""
        reaping.set()

    def tearDown(self):
        with lines_lock:
            text = "\n".join(lines)
        for secret in SECRETS:
            self.assertNotIn(secret, text, "a secret was logged")

    def logged(self, text):
        with lines_lock:
            return [line for line in lines[self.start:] if text in line]

    def raw(self, *args, **kwargs):
        r = Raw(*args, **kwargs)
        self.addCleanup(r.close)
        return r

    def pi(self, *args, **kwargs):
        p = WsPi(*args, **kwargs)
        self.addCleanup(p.close)
        return p

    def phone(self, *args, **kwargs):
        p = Phone(*args, **kwargs)
        self.addCleanup(p.close)
        return p


class Handshake(WsTest):

    def test_101_with_the_right_accept(self):
        r = self.raw()
        self.assertTrue(r.head.startswith("HTTP/1.1 101 "), r.head)
        self.assertEqual(r.header("Sec-WebSocket-Accept"), SAMPLE_ACCEPT)
        self.assertEqual((r.header("Upgrade").lower(), r.header("Connection").lower()), ("websocket", "upgrade"))
        self.assertIsNone(r.header("Sec-WebSocket-Extensions"))  # no compression offered back
        self.assertEqual(r.login()["type"], "hello")

    def test_header_case_and_lists(self):
        r = self.raw(headers={"X-Pi-Key": PI_KEY})  # extra headers are fine
        r.close()
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(f"GET /ws/phone HTTP/1.1\r\nHost: x\r\nupgrade: WebSocket\r\n"
                     f"CONNECTION: keep-alive, Upgrade\r\nsec-websocket-key: {SAMPLE_KEY}\r\n"
                     f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
        self.assertTrue(sock.recv(4096).startswith(b"HTTP/1.1 101 "))

    def test_not_an_upgrade_426_and_bad_handshakes(self):
        for path in ("/pi/ws", "/ws/phone", f"/ws/phone?lab_token={PHONE_TOKEN}"):
            status, body, headers = call("GET", path, code=None, headers={"X-Pi-Key": PI_KEY})
            self.assertEqual((status, body), (426, {"error": "this address only takes WebSocket connections"}))
            self.assertEqual(headers["Upgrade"], "websocket")
            self.assertEqual(headers["Cache-Control"], "no-store")
        upgrade = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Key": SAMPLE_KEY}
        status, body, headers = call("GET", "/ws/phone", code=None, headers={**upgrade, "Sec-WebSocket-Version": "8"})
        self.assertEqual((status, body, headers["Sec-WebSocket-Version"]),
                         (426, {"error": "unsupported WebSocket version"}, "13"))
        for key in ("", "not base64!", base64.b64encode(b"short").decode()):
            status, body, _ = call("GET", "/ws/phone", code=None,
                                   headers={**upgrade, "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13"})
            self.assertEqual((status, body), (400, {"error": "bad WebSocket handshake"}), key)
        self.assertEqual(call("POST", "/pi/ws", code=None)[:2], (404, {"error": "not found"}))

    def test_wrong_pi_key_403_before_101(self):
        for key in ("", WRONG_KEY):
            started = time.monotonic()
            with self.assertRaises(InvalidStatus) as caught:
                connect(f"ws://127.0.0.1:{port}/pi/ws", additional_headers={"X-Pi-Key": key}, open_timeout=5)
            self.assertEqual(caught.exception.response.status_code, 403)
            self.assertEqual(json.loads(caught.exception.response.body), {"error": "wrong Pi key"})
            self.assertGreaterEqual(time.monotonic() - started, app.WRONG_AUTH_DELAY)
        r = self.raw("/pi/ws")  # no key at all
        self.assertEqual(r.status, 403)
        self.assertNotIn("101", r.head.split("\r\n")[0])
        self.assertEqual(len(self.logged("wrong Pi key from 127.0.0.1")), 3)
        self.assertIs(online("kleros1"), False)


class Frames(WsTest):
    """Raw clients on the phone path."""

    def test_unmasked_client_frame_closed_1002(self):
        r = self.raw()
        r.send(TEXT, auth(), mask=False)
        self.assertEqual(r.close_code(), 1002)
        self.assertTrue(r.eof())

    def test_reserved_bits_and_unknown_opcode_1002(self):
        for bad in (bytes([0xC1, 0x80]) + b"\0\0\0\0", bytes([0x83, 0x80]) + b"\0\0\0\0"):
            r = self.raw()
            r.sock.sendall(bad)
            self.assertEqual(r.close_code(), 1002)

    def test_16_and_64_bit_lengths(self):
        r = self.raw()
        long_auth = auth(pad="x" * 300)  # 126 + 16-bit length from the client
        self.assertGreater(len(long_auth), 125)
        r.send(TEXT, long_auth)
        hello = r.json()
        self.assertEqual(hello["type"], "hello")
        _, payload, marker = r.recv()  # kleros1's state, offline: short
        r.json()
        pi = self.pi()
        opcode, payload, marker = r.recv()  # kleros1 online with its state: > 125 bytes
        self.assertEqual((opcode, json.loads(payload)["online"], marker), (TEXT, True, 126))
        r.send(TEXT, json.dumps({"type": "watch", "host": "kleros1", "screen": 0, "on": True}).encode())
        pi.wait_want([0])
        big = JPEG + bytes(70000)  # > 65535: 127 + 64-bit length both ways
        raw_pi = self.raw("/pi/ws", headers={"X-Pi-Key": PI_KEY})
        raw_pi.send(TEXT, json.dumps({"type": "hello", "host": "kleros1", "state": raw_state("kleros1")}).encode())
        self.assertEqual(json.loads(raw_pi.recv()[1])["type"], "want")
        raw_pi.send(BINARY, bytes([0]) + big)
        while (got := r.recv())[0] != BINARY:
            pass
        self.assertEqual((parse_preview(got[1]), got[2]), (("kleros1", 0, big), 127))
        status, body, _ = call("GET", "/api/pi/kleros1/api/screen/0/shot.jpg")
        self.assertEqual((status, body), (200, big))

    def test_partial_reads(self):
        r = self.raw()
        for byte in frame(TEXT, auth()):  # one byte per TCP segment
            r.sock.sendall(bytes([byte]))
            time.sleep(0.002)
        self.assertEqual(r.json()["type"], "hello")

    def test_frames_right_behind_the_handshake(self):
        r = self.raw(early=frame(TEXT, auth()) + frame(PING, b"early"))
        self.assertEqual(r.status, 101)
        got = {}  # the pong is written at once, the hello by the writer thread: either may come first
        while len(got) < 2:
            fin, opcode, payload, _ = r.frame()
            if opcode == PONG or opcode == TEXT and json.loads(payload)["type"] == "hello":
                got[opcode] = payload
        self.assertEqual(got[PONG], b"early")

    def test_fragmented_message_with_a_ping_inside(self):
        r = self.raw()
        text = auth()
        r.send(TEXT, text[:5], fin=False)
        r.send(CONTINUATION, text[5:10], fin=False)
        r.send(PING, b"mid")
        r.send(CONTINUATION, text[10:])
        fin, opcode, payload, _ = r.frame()
        self.assertEqual((opcode, payload), (PONG, b"mid"))
        self.assertEqual(r.json()["type"], "hello")
        r.send(CONTINUATION, b"{}")  # a continuation with nothing to continue
        self.assertEqual(r.close_code(), 1002)

    def test_ping_pong_both_ways(self):
        r = self.raw()
        r.login()
        r.send(PING, b"are you there")
        while (got := r.frame())[1] != PONG:
            pass
        self.assertEqual(got[2], b"are you there")
        started = time.monotonic()
        while (got := r.frame())[1] != PING:  # the server's own ping
            pass
        self.assertLess(time.monotonic() - started, app.PING_EVERY + 0.5)
        phone = self.phone()  # the library answers the server's pings by itself
        phone.wait(lambda m: m["type"] == "hello")
        time.sleep(app.SILENT_AFTER + 0.5)
        self.assertIsNone(phone.closed)
        self.assertTrue(phone.ws.ping(b"lib").wait(2), "no pong for the library's ping")

    def test_silent_client_closed(self):
        r = self.raw()
        r.login()
        started = time.monotonic()
        while (got := r.frame())[1] != CLOSE:  # never answer the pings
            self.assertLess(time.monotonic() - started, app.SILENT_AFTER + 2, "never closed")
        self.assertEqual(struct.unpack("!H", got[2][:2])[0], 1011)
        self.assertGreaterEqual(time.monotonic() - started, app.SILENT_AFTER - 0.3)
        self.assertTrue(r.eof())
        self.assertTrue(wait_until(lambda: self.logged("phone WebSocket disconnected (from 127.0.0.1): silent for")))

    def test_close_is_echoed(self):
        r = self.raw()
        r.login()
        r.send(CLOSE, struct.pack("!H", 1000) + b"bye")
        fin, opcode, payload, _ = r.frame()
        self.assertEqual((opcode, payload), (CLOSE, struct.pack("!H", 1000)))
        self.assertTrue(r.eof())
        phone = self.phone()
        phone.wait(lambda m: m["type"] == "hello")
        phone.close()  # the library's close handshake completes cleanly
        self.assertEqual(phone.closed, 1000)

    def test_oversize_1009(self):
        r = self.raw()
        r.login()
        r.sock.sendall(bytes([0x82, 0x80 | 127]) + struct.pack("!Q", app.MAX_BINARY + 1) + os.urandom(4) + bytes(1000))
        self.assertEqual(r.close_code(), 1009)
        self.assertTrue(r.eof())
        r = self.raw()
        r.login()
        r.send(TEXT, b"[" * 100, fin=False)
        r.send(CONTINUATION, b" " * app.MAX_TEXT)  # the parts add up to too much
        self.assertEqual(r.close_code(), 1009)
        phone = self.phone()
        phone.wait(lambda m: m["type"] == "hello")
        phone.ws.send(" " * (app.MAX_TEXT + 1))
        self.assertEqual(phone.wait_closed(), 1009)
        pi = self.pi()
        pi.preview(0, bytes(app.MAX_BINARY - 1))  # exactly 1 MB with the screen byte: allowed
        pi.ws.send(json.dumps({"type": "state", "state": raw_state("kleros1", current=1)}))
        self.assertTrue(wait_until(lambda: call("GET", "/api/pi/kleros1/api/state")[1]["screens"][0]["current"] == 1))
        self.assertIsNone(pi.closed)

    def test_text_must_be_utf8(self):
        r = self.raw()
        r.send(TEXT, b"\xff\xfe")
        self.assertEqual(r.close_code(), 1007)

    def test_first_message_in_time(self):
        r = self.raw()
        started = time.monotonic()
        self.assertEqual(r.close_code(), 1008)
        self.assertGreaterEqual(time.monotonic() - started, app.FIRST_WITHIN - 0.2)
        pi = self.pi(hello=False)
        self.assertEqual(pi.wait_closed(), 1008)
        self.assertTrue(self.logged("Pi WebSocket from 127.0.0.1 closed before its hello"))
        self.assertTrue(self.logged("phone WebSocket from 127.0.0.1 closed before auth"))

    def test_server_frames_are_unmasked_and_written_in_order(self):
        """Many threads queue at once; every frame arrives whole (Raw asserts unmasked)."""
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        ws = app.WebSocket(a, "test")
        self.addCleanup(ws.end, None)
        sent = [{"type": "n", "i": i, "pad": "x" * (i * 97 % 70000)} for i in range(60)]
        threads = [threading.Thread(target=ws.put, args=(m,)) for m in sent]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        reader = Raw.__new__(Raw)
        reader.sock, reader.buf = b, b""
        b.settimeout(5)
        got = [json.loads(reader.frame()[2]) for _ in sent]  # each one whole, never interleaved
        self.assertEqual(sorted(got, key=lambda m: m["i"]), sent)


class PhoneAuth(WsTest):

    def test_right_code(self):
        phone = self.phone()
        hello = phone.wait(lambda m: m["type"] == "hello")
        self.assertEqual(hello, {"type": "hello", "pis": [{"host": "kleros1", "online": False},
                                                          {"host": "kleros2", "online": False}]})
        for host in PIS:
            self.assertEqual(phone.state_of(host, online=False),
                             {"type": "state", "host": host, "online": False, "state": None})
        self.assertTrue(self.logged("phone WebSocket connected (from 127.0.0.1)"))

    def test_wrong_code_4003(self):
        for code in ("not-the-code", ""):
            started = time.monotonic()
            phone = self.phone(code=code)
            self.assertEqual(phone.wait(lambda m: True), {"type": "error", "error": "wrong access code"})
            self.assertGreaterEqual(time.monotonic() - started, app.WRONG_AUTH_DELAY)
            self.assertEqual(phone.wait_closed(), 4003)
            self.assertEqual(len(phone.messages), 1)  # nothing about the Pis
        self.assertEqual(len(self.logged("wrong access code from 127.0.0.1")), 2)
        r = self.raw()
        r.send(TEXT, json.dumps({"type": "watch", "host": "kleros1", "screen": 0, "on": True}).encode())
        self.assertEqual(r.close_code(), 1008)  # auth has to come first

    def test_forwarded_for_is_logged(self):
        r = self.raw(headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
        r.login()
        self.assertTrue(wait_until(lambda: self.logged("phone WebSocket connected (from 203.0.113.9)")))


class Together(WsTest):
    """Phones and Pis on WebSockets, and with HTTP Pis and phones."""

    def test_pi_hello_reaches_phone(self):
        phone = self.phone()
        phone.state_of("kleros1", online=False)
        pi = self.pi()
        self.assertEqual(phone.state_of("kleros1")["state"], seen(pi.state))
        pi.wait_want([])  # right after the hello
        self.assertEqual(online("kleros1"), True)
        self.assertEqual(call("GET", "/api/pi/kleros1/api/state")[:2], (200, seen(pi.state)))
        self.assertTrue(self.logged("kleros1 is online (from 127.0.0.1)"))
        self.assertTrue(self.logged("kleros1 WebSocket connected (from 127.0.0.1)"))
        later = self.phone()  # a phone joining later gets the state at once
        self.assertEqual(later.wait(lambda m: m["type"] == "hello")["pis"][0], {"host": "kleros1", "online": True})
        self.assertEqual(later.state_of("kleros1")["state"], seen(pi.state))
        changed = raw_state("kleros1", current=1)
        pi.ws.send(json.dumps({"type": "state", "state": changed}))
        for p in (phone, later):
            p.wait(lambda m: m["type"] == "state" and m["state"] == seen(changed))

    def test_phone_command_over_both_websockets(self):
        pi = self.pi()
        phone = self.phone()
        phone.state_of("kleros1")
        started = time.monotonic()
        result = phone.command("c1", query=f"site=2&lab_token={PHONE_TOKEN}", body={"url": "https://kleros.io"})
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result, {"type": "result", "id": "c1", "status": 200,
                                  "body": seen(raw_state("kleros1", current=1))})
        [cmd] = pi.commands
        self.assertEqual({k: cmd[k] for k in ("type", "method", "path", "query", "body")},
                         {"type": "command", "method": "POST", "path": "/api/screen/0/next", "query": "site=2",
                          "body": {"url": "https://kleros.io"}})
        self.assertNotEqual(cmd["id"], "c1")  # the relay's own id
        phone.wait(lambda m: m["type"] == "state" and m["state"] == seen(raw_state("kleros1", current=1)))
        self.assertEqual(call("GET", "/api/pi/kleros1/api/state")[1], seen(raw_state("kleros1", current=1)))
        self.assertTrue(self.logged("kleros1 screen 0: next (from 127.0.0.1) (ws)"))
        self.assertEqual(phone.command("c2", path="/api/screen/0/dance")["body"], {"error": "unknown command"})

    def test_phone_command_errors(self):
        self.pi()
        phone = self.phone()
        phone.state_of("kleros1")
        not_found = {"status": 404, "body": {"error": "not found"}}
        for rid, kwargs in (("a", {"host": "kleros9"}), ("b", {"path": "/api/screen/0/NEXT"}),
                            ("c", {"path": "/api/pi/kleros1/api/screen/0/next"}), ("d", {"path": None}),
                            ("e", {"host": ["kleros1"]})):
            result = phone.command(rid, **kwargs)
            self.assertEqual({k: result[k] for k in ("status", "body")}, not_found, kwargs)
        result = phone.command("f", body={"url": "x" * 5000})
        self.assertEqual((result["status"], result["body"]), (413, {"error": "request too large"}))
        result = phone.command("g", host="kleros2")
        self.assertEqual((result["status"], result["body"]), (503, {"error": "kleros2 is not connected"}))
        self.assertTrue(self.logged("kleros2 screen 0: next failed: kleros2 is not connected (ws)"))

    def test_504_when_the_ws_pi_never_answers(self):
        pi = self.pi(answer=False)
        phone = self.phone()
        phone.state_of("kleros1")
        with mock.patch.object(app, "REPLY_TIMEOUT", 0.5):
            result = phone.command("slow")
        self.assertEqual((result["status"], result["body"]), (504, {"error": "kleros1 didn't answer"}))
        self.assertEqual(len(pi.commands), 1)
        h = app.find_host("kleros1")
        with h.cond:
            self.assertEqual((h.queue, h.delivered, h.results), ([], set(), {}))

    def test_http_command_goes_over_the_pi_websocket(self):
        pi = self.pi()
        self.assertTrue(wait_until(lambda: online("kleros1")))
        started = time.monotonic()
        status, body, _ = call("POST", f"/api/pi/kleros1/api/screen/0/next?lab_token={PHONE_TOKEN}")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual((status, body), (200, seen(raw_state("kleros1", current=1))))
        self.assertEqual([(c["path"], c["query"], c["body"]) for c in pi.commands], [("/api/screen/0/next", "", None)])
        self.assertEqual(self.logged("screen 0: next"), ["kleros1 screen 0: next (from 127.0.0.1)"])

    def test_previews_pushed_to_watching_phones(self):
        pi = self.pi()
        watcher, other = self.phone(), self.phone()
        watcher.state_of("kleros1")
        other.state_of("kleros1")
        watcher.watch("kleros1", 0)
        pi.wait_want([0])
        pi.preview(0, JPEG)
        self.assertEqual(watcher.wait(lambda p: True, what="preview"), ("kleros1", 0, JPEG))
        self.assertEqual(call("GET", "/api/pi/kleros1/api/screen/0/shot.jpg")[:2], (200, JPEG))
        pi.preview(1, JPEG + b"1")  # nobody watches screen 1: stored, not pushed
        self.assertTrue(wait_until(lambda: call("GET", "/api/pi/kleros1/api/screen/1/shot.jpg")[0] == 200))
        late = self.phone()
        late.state_of("kleros1")
        late.watch("kleros1", 0)  # the stored preview comes at once
        self.assertEqual(late.wait(lambda p: True, what="preview"), ("kleros1", 0, JPEG))
        watcher.watch("kleros1", 0, on=False)
        late.watch("kleros1", 0, on=False)
        self.reap()
        pi.wait_want([], timeout=app.WANT_FOR + 2)  # once the shot.jpg requests above expire
        pi.preview(0, JPEG + b"again")
        time.sleep(0.3)
        self.assertEqual((len(watcher.previews), len(late.previews)), (1, 1))
        self.assertEqual(other.previews, [])

    def test_a_stale_stored_preview_is_not_shown_to_a_new_watcher(self):
        """3 Nov (DM 3327): a phone that started watching got the stored preview at once, even
        one from up to 15 s before: the TV could have moved on, so a wrong picture flashed."""
        pi = self.pi()
        first = self.phone()
        first.state_of("kleros1")
        first.watch("kleros1", 0)
        pi.wait_want([0])
        pi.preview(0, JPEG + b"old")
        first.wait(lambda p: True, what="preview")
        first.watch("kleros1", 0, on=False)
        h = app.find_host("kleros1")
        with h.cond:
            jpeg, at = h.shots[0]
            h.shots[0] = (jpeg, at - app.REPLAY_MAX_AGE - 1)  # nobody watched since
        late = self.phone()
        late.state_of("kleros1")
        late.watch("kleros1", 0)
        pi.wait_want([0])
        time.sleep(0.3)
        self.assertEqual(late.previews, [])  # not the old one
        self.assertEqual(call("GET", "/api/pi/kleros1/api/screen/0/shot.jpg")[:2], (200, JPEG + b"old"))  # HTTP: as before
        pi.preview(0, JPEG + b"new")
        self.assertEqual(late.wait(lambda p: True, what="preview"), ("kleros1", 0, JPEG + b"new"))

    def test_slow_phone_gets_only_the_latest_preview(self):
        pi = self.pi()
        r = self.raw(rcvbuf=4096)
        r.login()
        r.send(TEXT, json.dumps({"type": "watch", "host": "kleros1", "screen": 0, "on": True}).encode())
        pi.wait_want([0])
        count = 25
        with mock.patch.object(app, "SILENT_AFTER", 10):
            started = time.monotonic()
            for n in range(count):  # the raw phone reads nothing meanwhile
                pi.preview(0, n.to_bytes(2, "big") + bytes(200_000))
            pi.ws.send(json.dumps({"type": "state", "state": raw_state("kleros1", current=1)}))
            pi_took = time.monotonic() - started
            time.sleep(0.3)
            got, states = [], []
            while not got or got[-1] != count - 1:
                opcode, payload, _ = r.recv()
                if opcode == BINARY:
                    got.append(int.from_bytes(parse_preview(payload)[2][:2], "big"))
                else:
                    states.append(json.loads(payload))
        self.assertLess(pi_took, 2, "a slow phone held up the Pi")
        self.assertLess(len(got), count, "nothing was dropped")
        self.assertEqual(got, sorted(got))
        self.assertEqual(got[-1], count - 1)
        h = app.find_host("kleros1")
        self.assertEqual(h.shots[0][0][:2], (count - 1).to_bytes(2, "big"))  # the stored one is the newest too

    def test_coalescing_keeps_json_order(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        ws = app.WebSocket(a, "test")
        self.addCleanup(ws.end, None)
        with ws.lock:  # the writer is stuck, as on a slow phone
            ws.put({"n": 1})
            ws.put_preview(("kleros1", 0), b"old")
            ws.put({"n": 2})
            ws.put_preview(("kleros2", 0), b"other")
            ws.put_preview(("kleros1", 0), b"newer")
            ws.put_preview(("kleros1", 0), b"newest")
            ws.put({"n": 3})
        reader = Raw.__new__(Raw)
        reader.sock, reader.buf = b, b""
        b.settimeout(5)
        got = [(op, json.loads(p) if op == TEXT else p) for _, op, p, _ in (reader.frame() for _ in range(5))]
        self.assertEqual(got, [(TEXT, {"n": 1}), (BINARY, b"newest"), (TEXT, {"n": 2}), (BINARY, b"other"),
                               (TEXT, {"n": 3})])

    def test_want_merges_http_and_websocket_watchers(self):
        pi = self.pi()
        pi.wait_want([])
        a, b = self.phone(), self.phone()
        a.state_of("kleros1")
        b.state_of("kleros1")
        a.watch("kleros1", 0)
        pi.wait_want([0])
        b.watch("kleros1", 0)
        b.watch("kleros1", 2)
        pi.wait_want([0, 2])
        call("GET", "/api/pi/kleros1/api/screen/1/shot.jpg")  # an HTTP phone
        pi.wait_want([0, 1, 2])
        self.reap()
        pi.wait_want([0, 2], timeout=app.WANT_FOR + 2)  # that one expired
        reaping.clear()
        time.sleep(0.2)  # a reap() still running
        b.close()
        pi.wait_want([0])
        a.watch("kleros1", 0, on=False)
        pi.wait_want([])
        for bad in ({"host": "kleros9", "screen": 0}, {"host": "kleros1", "screen": "0"},
                    {"host": "kleros1", "screen": 256}, {"host": "kleros1", "screen": True}):
            a.send(type="watch", on=True, **bad)
        a.watch("kleros1", 3)
        pi.wait_want([3])  # only the good one counted
        self.assertEqual(pi.wants[-2]["screens"], [])

    def test_pi_disconnect_tells_the_phones(self):
        pi = self.pi()
        phone = self.phone()
        phone.state_of("kleros1")
        pi.close()
        self.assertEqual(phone.state_of("kleros1", online=False),
                         {"type": "state", "host": "kleros1", "online": False, "state": None})
        self.assertIs(online("kleros1"), False)
        self.assertEqual(call("GET", "/api/pi/kleros1/api/state")[:2], (503, {"error": "kleros1 is not connected"}))
        self.assertTrue(self.logged("kleros1 WebSocket disconnected (from 127.0.0.1)"))
        self.assertTrue(self.logged("kleros1 went offline (WebSocket closed)"))
        result = phone.command("after")
        self.assertEqual((result["status"], result["body"]), (503, {"error": "kleros1 is not connected"}))

    def test_a_second_connection_replaces_the_first(self):
        first = self.pi()
        self.assertTrue(wait_until(lambda: online("kleros1")))
        second = self.pi()
        self.assertEqual(first.wait_closed(), 1000)
        self.assertTrue(wait_until(lambda: self.logged("disconnected (from 127.0.0.1): replaced by a newer connection")))
        self.assertIs(online("kleros1"), True)
        self.assertFalse(self.logged("went offline"))
        self.assertEqual(call("POST", "/api/pi/kleros1/api/screen/0/next")[0], 200)
        self.assertEqual((len(first.commands), len(second.commands)), (0, 1))

    def test_bad_hello_closed_1008(self):
        for hello in ({"type": "state", "host": "kleros1"}, {"type": "hello", "host": "Kleros1"},
                      {"type": "hello"}, ["hello"]):
            pi = self.pi(hello=False)
            pi.ws.send(json.dumps(hello))
            self.assertEqual(pi.wait_closed(), 1008, hello)
        self.assertEqual(call("GET", "/api/state")[1]["pis"],
                         [{"host": h, "base": f"/api/pi/{h}", "online": False} for h in PIS])

    def test_an_http_pi_and_a_websocket_pi_at_once(self):
        ws_pi = self.pi("kleros1")
        http_pi = HttpPi("kleros2")
        self.addCleanup(http_pi.close)
        phone = self.phone()
        self.assertEqual(phone.state_of("kleros1")["state"], seen(raw_state("kleros1")))
        self.assertEqual(phone.state_of("kleros2")["state"], seen(raw_state("kleros2")))
        for host in PIS:  # commands from a phone WebSocket and over HTTP, to both
            self.assertEqual(phone.command(f"ws-{host}", host=host)["status"], 200)
            self.assertEqual(call("POST", f"/api/pi/{host}/api/screen/0/next")[:2],
                             (200, seen(raw_state(host, current=2))))
        self.assertEqual((len(ws_pi.commands), len(http_pi.seen)), (2, 2))
        phone.wait(lambda m: m["type"] == "state" and m["host"] == "kleros2" and m["state"]["screens"][0]["current"] == 2)
        phone.watch("kleros2", 0)  # an HTTP Pi gets WebSocket watchers in want_shots
        self.assertTrue(wait_until(lambda: [0] in http_pi.wants))
        self.assertEqual(phone.wait(lambda p: p[0] == "kleros2", what="preview"), ("kleros2", 0, JPEG + b"kleros2"))
        self.assertEqual(ws_pi.wants[-1]["screens"], [])  # kleros1 isn't asked for kleros2's screen
        http_pi.close()
        self.reap()  # an HTTP Pi goes offline by not syncing, which only the reaper notices
        phone.state_of("kleros2", online=False, timeout=app.OFFLINE_AFTER + 3)
        self.assertTrue(self.logged("kleros2 went offline (no sync for 2 s)"))
        self.assertIs(online("kleros1"), True)
        self.assertEqual(phone.command("still", host="kleros1")["status"], 200)


if __name__ == "__main__":
    unittest.main()
