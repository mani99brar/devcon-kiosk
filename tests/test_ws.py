"""End to end over the WebSocket (docs/websocket-design.md): the real relay (server/app.py) in a
thread, fake Pis built from the real pi/kiosk.py running the real uplink against it, and phones
on /ws/phone through the websockets client, as the page talks to the relay. Each fake Pi's
Screens drive a fake Chromium: its DevTools HTTP endpoints are patched in, and a DevTools
WebSocket records every command and answers previews. A second server on the same relay
state answers GET /pi/ws with 404 like the relay before the WebSocket, for the fallback to
HTTP sync. Short timings are patched in; nothing real (Chromium, mpv, grim) is started, and
nothing outside a temp folder is written. Each side alone, in more detail: tests/test_ws_server.py
(the framing, coalescing, want merging) and tests/test_ws_pi.py (browsing, previews, fallback).

  <venv>/bin/python -m unittest tests.test_ws -v      (from the repo root; about 15 s)

Needs the websockets package (python3-websockets on the Pis); skipped without it.
KIOSK_TEST_LOGS=1 also prints both sides' log lines as they happen.
"""
import base64
import http.client
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

try:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect
    from websockets.sync.server import serve
except ImportError:
    connect = None

sys.dont_write_bytecode = True  # no __pycache__ in pi/ or server/
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pi"))
import kiosk  # noqa: E402

_spec = importlib.util.spec_from_file_location("relay_app_e2e", REPO / "server" / "app.py")
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
TIMINGS = {"SYNC_HOLD": 0.5, "OFFLINE_AFTER": 2, "REPLY_TIMEOUT": 3, "WRONG_AUTH_DELAY": 0.2, "WANT_FOR": 2,
           "PING_EVERY": 0.5, "SILENT_AFTER": 3, "FIRST_WITHIN": 2, "WS_TICK": 0.05, "CLOSE_WAIT": 1}
SITES = ["https://example.org", "video:clip.mp4", "https://example.net/page"]
VIEWPORT = [1422, 800]  # 1920x1080 at zoom 1.35, in CSS pixels
GRIM_JPEG = b"\xff\xd8 grim \xff\xd9"
SCREENCAST = ("Page.startScreencast", "Page.stopScreencast")  # previews, not browsing

lines, lines_lock = [], threading.Lock()  # both sides' log lines, "relay: ..." and "pi: ..."
port = old_port = None
tab_ids = itertools.count(1)  # tab ids unique across every fake Chromium


def capture(side):
    def log(*args):
        line = f"{side}: " + " ".join(map(str, args))
        with lines_lock:
            lines.append(line)
        if os.environ.get("KIOSK_TEST_LOGS"):
            print(line, flush=True)
    return log


def wait_until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while not (result := check()) and time.monotonic() < deadline:
        time.sleep(0.02)
    return result


class NoWebSocket(app.Relay):
    """The relay as it was before the WebSocket: GET /pi/ws is a 404 (HTTP/1.0)."""

    def websocket(self, path):
        if path == "/pi/ws":
            return self.reply(404, {"error": "not found"})
        return super().websocket(path)


def setUpModule():
    global port, old_port
    if connect is None:
        raise unittest.SkipTest("the websockets package is not installed (on the Pis it is python3-websockets; "
                                "run this with a python that has it, e.g. a venv with websockets 15)")
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    patches = [
        mock.patch.object(kiosk.Screen, "devtools", lambda s, path, method="GET": s.chromium.http(s, path, method)),
        mock.patch.object(kiosk.Screen, "play", play),
        mock.patch.object(kiosk, "subprocess", types.SimpleNamespace(run=grim, TimeoutExpired=subprocess.TimeoutExpired)),
        mock.patch.object(kiosk, "SITES_FILE", Path(tmp.name) / "sites.json"),
        mock.patch.object(kiosk, "CDP_TIMEOUT", 1),
        mock.patch.object(kiosk, "log", capture("pi")),
        mock.patch.object(app, "log", capture("relay")),
        *(mock.patch.object(app, name, value) for name, value in TIMINGS.items()),
    ]
    for p in patches:
        p.start()
        unittest.addModuleCleanup(p.stop)
    app.configure(CODE, PI_KEY, PIS)
    server = app.make_server(0, "127.0.0.1")
    old = app.ThreadingHTTPServer(("127.0.0.1", 0), NoWebSocket)
    port, old_port = server.server_address[1], old.server_address[1]
    for s in (server, old):
        threading.Thread(target=s.serve_forever, daemon=True).start()
        unittest.addModuleCleanup(s.server_close)
        unittest.addModuleCleanup(s.shutdown)
    threading.Thread(target=app.reaper, daemon=True).start()


def tearDownModule():
    app.configure(CODE, PI_KEY, ())  # nothing left for the reaper


class FakeChromium:
    """One screen's Chromium: the DevTools HTTP endpoints (patched in as Screen.devtools) and a
    DevTools WebSocket per tab that records each command as (tab, method, params) and answers it."""

    def __init__(self):
        self.tabs, self.calls, self.lock = {}, [], threading.Lock()  # tabs: id -> its URL now
        self.server = serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def http(self, screen, path, method="GET"):
        with self.lock:
            if path.startswith("/json/new?"):
                tab = f"T{next(tab_ids)}"
                self.tabs[tab] = urllib.parse.unquote(path.removeprefix("/json/new?"))
                return {"id": tab}
            if path.startswith("/json/close/"):
                self.tabs.pop(path.removeprefix("/json/close/"), None)
                return "Target is closing"
            if path.startswith("/json/activate/"):
                tab = path.removeprefix("/json/activate/")
                if tab not in self.tabs:
                    raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
                screen.showing = self.tabs[tab]
                return "Target activated"
            if path == "/json/list":
                return [{"id": t, "type": "page", "url": u,
                         "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/{t}"}
                        for t, u in self.tabs.items()]
        raise OSError(f"fake Chromium has no {path}")

    def handler(self, ws):
        tab = ws.request.path.rsplit("/", 1)[-1]
        try:
            for raw in ws:
                msg = json.loads(raw)
                method, params = msg["method"], msg.get("params", {})
                self.calls.append((tab, method, params))
                result = {}
                if method == "Runtime.evaluate" and params.get("expression") == "[innerWidth, innerHeight]":
                    result = {"result": {"type": "object", "value": VIEWPORT}}
                ws.send(json.dumps({"id": msg["id"], "result": result}))
                if method == "Page.startScreencast":
                    ws.send(json.dumps({"method": "Page.screencastFrame", "params": {
                        "data": base64.b64encode(picture(tab)).decode(), "metadata": {}, "sessionId": 1}}))
        except ConnectionClosed:
            pass

    def browsing(self):
        """The commands sent so far that aren't previews: (tab, method, params)."""
        return [c for c in self.calls if c[1] not in SCREENCAST]


def picture(tab):
    """What the fake DevTools sends as a preview of a tab."""
    return b"\xff\xd8 devtools " + tab.encode() + b" \xff\xd9"


class FakePlayer:
    """mpv, still playing."""

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def play(self, site):
    self.showing, self.player = site, FakePlayer()


def grim(args, **kwargs):
    """grim and ffmpeg for previews of a video; nothing else is run."""
    if args[0] not in ("grim", "ffmpeg"):
        raise OSError(f"this test doesn't run {args[0]}")
    return subprocess.CompletedProcess(args, 0, stdout=b"P6 fake ppm" if args[0] == "grim" else GRIM_JPEG,
                                       stderr=b"")


def make_screen(index, sites):
    s = kiosk.Screen(index, f"HDMI-A-{index + 1}", 30, 1.35, list(sites), 8080, base=list(sites))
    s.chromium = FakeChromium()
    s.tabs = [None if kiosk.is_video(u) else s.new_tab(u) for u in s.sites]  # as launch() does
    s.show(0)
    return s


class FakePi:
    """A Pi from the real kiosk.py: two Screens, each on its own fake Chromium, and the real uplink."""

    def __init__(self, host, key=PI_KEY, server_url=None, stop=None):
        self.host, self.public_urls = host, [f"https://{host}.tail0000.ts.net"]
        self.screens = [make_screen(0, SITES), make_screen(1, ["https://example.com/one"])]
        cfg = {"server_url": server_url or f"http://127.0.0.1:{port}/", "server_pi_key": key,
               "server_lab_token": LAB_TOKEN}
        self.stop = stop or threading.Event()
        self.thread = threading.Thread(target=kiosk.uplink, daemon=True,
                                       args=(cfg, host, self.screens, self.public_urls, self.stop))
        self.thread.start()

    def expected(self):
        """What the relay passes on for this Pi: its own /api/state with peers = []."""
        return {**kiosk.pi_state(self.host, self.public_urls, self.screens), "peers": []}

    def close(self):
        self.stop.set()
        self.thread.join(5)
        for s in self.screens:
            s.chromium.close()
        if self.thread.is_alive():
            raise AssertionError(f"{self.host}'s uplink didn't stop")
        # the relay lets go of the WebSocket too, so its "offline" can't reach a later test's phone
        h = app.find_host(self.host)
        if h is not None and not wait_until(lambda: h.ws is None, 3):
            raise AssertionError(f"the relay still has {self.host}'s WebSocket")


class RecordingStop(threading.Event):
    """An uplink stop event that records how long the uplink waits each time."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return super().wait(timeout)


class Phone:
    """The hub page on /ws/phone, through the websockets client."""

    def __init__(self, code=CODE):
        self.ws = connect(f"ws://127.0.0.1:{port}/ws/phone?lab_token={PHONE_TOKEN}", open_timeout=5,
                          close_timeout=2)
        self.messages, self.previews, self.closed, self.cond = [], [], None, threading.Condition()
        self.ids = itertools.count(1)
        self.ws.send(json.dumps({"type": "auth", "code": code}))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while True:
                data = self.ws.recv()
                with self.cond:
                    if isinstance(data, bytes):  # [n][host][screen][JPEG]
                        n = data[0]
                        self.previews.append((data[1:1 + n].decode(), data[1 + n], data[2 + n:]))
                    else:
                        self.messages.append(json.loads(data))
                    self.cond.notify_all()
        except ConnectionClosed as e:
            with self.cond:
                self.closed = e.rcvd.code if e.rcvd else "no close frame"
                self.cond.notify_all()

    def send(self, **msg):
        self.ws.send(json.dumps(msg))

    def wait(self, check, timeout=5, after=0):
        """The first message from index `after` on for which check() is true, and its index."""
        def find():
            return next(((i, m) for i, m in enumerate(self.messages) if i >= after and check(m)), None)
        with self.cond:
            found = self.cond.wait_for(find, timeout)
        if not found:
            raise AssertionError(f"no matching message; got {self.messages[after:]}")
        return found

    def state_of(self, host, online=True, after=0, timeout=5):
        return self.wait(lambda m: m["type"] == "state" and m["host"] == host and m["online"] is online,
                         timeout, after)

    def command(self, host, action, i=0, query="", body=None, timeout=5):
        """What the page sends for a button: the result message (status, body)."""
        rid = f"p{next(self.ids)}"
        self.send(type="command", id=rid, host=host, path=f"/api/screen/{i}/{action}", query=query, body=body)
        _, m = self.wait(lambda m: m["type"] == "result" and m["id"] == rid, timeout)
        return m["status"], m["body"]

    def watch(self, host, screen, on=True):
        self.send(type="watch", host=host, screen=screen, on=on)

    def frames(self, host, screen):
        with self.cond:
            return [jpeg for h, i, jpeg in self.previews if (h, i) == (host, screen)]

    def wait_closed(self, timeout=5):
        with self.cond:
            self.cond.wait_for(lambda: self.closed is not None, timeout)
        return self.closed

    def close(self):
        self.ws.close()
        self.thread.join(5)


def call(method, path, body=None, code=CODE):
    """One HTTP request to the relay, as the page polls it: (status, body); JSON comes back parsed."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path, None if body is None else json.dumps(body).encode(), {"X-Code": code})
        r = conn.getresponse()
        raw = r.read()
    finally:
        conn.close()
    return r.status, json.loads(raw) if r.getheader("Content-Type", "").startswith("application/json") else raw


def online(host):
    return {p["host"]: p["online"] for p in call("GET", "/api/state")[1]["pis"]}.get(host)


class EndToEnd(unittest.TestCase):
    """Each class starts with a relay that knows only the PIS names, and its own fake Pis."""
    pis = ()  # host names started for the class
    server_url = None

    @classmethod
    def setUpClass(cls):
        app.configure(CODE, PI_KEY, PIS)
        with lines_lock:
            cls.class_start = len(lines)
        cls.syncs = mock.patch.object(app, "sync", wraps=app.sync)  # counts HTTP syncs
        cls.sync = cls.syncs.start()
        cls.fake = {}
        try:
            for host in cls.pis:
                cls.fake[host] = FakePi(host, server_url=cls.server_url)
            if not wait_until(lambda: all(online(h) for h in cls.pis), 5):
                raise AssertionError(f"the fake Pis never came online: {[online(h) for h in cls.pis]}")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for pi in cls.fake.values():
            pi.close()
        cls.syncs.stop()

    def setUp(self):
        with lines_lock:
            self.test_start = len(lines)

    def tearDown(self):
        with lines_lock:
            text = "\n".join(lines)
        for secret in SECRETS:
            self.assertNotIn(secret, text, "a secret was logged")

    def logged(self, text, since_class=False):
        with lines_lock:
            return [line for line in lines[self.class_start if since_class else self.test_start:] if text in line]

    def phone(self, code=CODE):
        p = Phone(code)
        self.addCleanup(p.close)
        return p

    def ok(self, phone, host, action, i=0, query="", body=None):
        """A command from the phone that must work: the Pi's state after it, as the relay passes it on."""
        status, state = phone.command(host, action, i, query, body)
        self.assertEqual(status, 200, state)
        self.assertEqual(state, self.fake[host].expected())
        return state


class TwoPisOverWebSocket(EndToEnd):
    pis = ("kleros1", "kleros2")

    def setUp(self):
        for host, pi in self.fake.items():  # every test starts on the first site, rotating
            for i in range(len(pi.screens)):
                for action in ("resume", "jump?site=0"):
                    self.assertEqual(call("POST", f"/api/pi/{host}/api/screen/{i}/{action}")[0], 200)
        super().setUp()

    def test_both_pis_connect_over_the_websocket(self):
        phone = self.phone()
        phone.wait(lambda m: m["type"] == "hello")
        # longer than SILENT_AFTER with nothing to say: the relay's pings and the clients' pongs keep
        # every WebSocket up
        time.sleep(app.SILENT_AFTER + 0.5)
        self.assertIsNone(phone.closed)
        self.assertEqual(call("GET", "/api/state"), (200, {"hub": True, "host": "server", "peers": [], "pis": [
            {"host": h, "base": f"/api/pi/{h}", "online": True} for h in PIS]}))
        for host in PIS:
            self.assertIsNotNone(app.find_host(host).ws, host)
            self.assertTrue(self.logged(f"relay: {host} WebSocket connected (from 127.0.0.1)", since_class=True))
            self.assertEqual(call("GET", f"/api/pi/{host}/api/state"), (200, self.fake[host].expected()))
        self.assertEqual(len(self.logged(f"pi: uplink: connected to http://127.0.0.1:{port} over WebSocket",
                                         since_class=True)), 2)
        self.assertFalse(self.logged("has no WebSocket", since_class=True))
        self.assertFalse(self.logged("WebSocket disconnected", since_class=True))
        self.assertEqual(self.sync.call_count, 0)  # no HTTP sync at all

    def test_phone_auth_hello_and_states(self):
        phone = self.phone()
        _, hello = phone.wait(lambda m: m["type"] == "hello")
        self.assertEqual(hello, {"type": "hello", "pis": [{"host": h, "online": True} for h in PIS]})
        for host in PIS:
            _, m = phone.state_of(host)
            self.assertEqual(m, {"type": "state", "host": host, "online": True, "state": self.fake[host].expected()})
        self.assertTrue(self.logged("relay: phone WebSocket connected (from 127.0.0.1)"))

    def test_commands_reach_the_pi_and_results_come_back(self):
        phone, other = self.phone(), self.phone()
        other.wait(lambda m: m["type"] == "hello")
        s = self.fake["kleros1"].screens[0]
        self.ok(phone, "kleros1", "jump", query="site=2")
        self.assertEqual((s.current, s.showing), (2, SITES[2]))
        state = self.ok(phone, "kleros1", "next")
        self.assertEqual((state["screens"][0]["current"], s.showing), (0, SITES[0]))
        self.ok(phone, "kleros1", "next")
        self.assertEqual((s.current, s.showing, s.player is not None), (1, "video:clip.mp4", True))
        self.ok(phone, "kleros1", "prev")
        self.assertEqual(s.current, 0)
        self.assertTrue(self.ok(phone, "kleros1", "pause")["screens"][0]["paused"] and s.paused)
        self.assertFalse(self.ok(phone, "kleros1", "resume")["screens"][0]["paused"] or s.paused)

        state = self.ok(phone, "kleros1", "show", body={"url": "kleros.io"})
        self.assertEqual((state["screens"][0]["temp"], state["screens"][0]["paused"]), ("kleros.io", True))
        self.assertEqual(s.showing, "https://kleros.io")
        state = self.ok(phone, "kleros1", "resume")
        self.assertEqual((state["screens"][0]["temp"], s.showing), (None, SITES[0]))

        url = "https://example.com/added"
        state = self.ok(phone, "kleros1", "add", body={"url": url})
        self.assertEqual((state["screens"][0]["urls"], s.showing), (SITES + [url], url))
        self.assertEqual(json.loads(kiosk.SITES_FILE.read_text())["0"], {"added": [url], "removed": []})
        self.assertEqual(self.ok(phone, "kleros1", "remove", body={"url": url})["screens"][0]["urls"], SITES)

        # a time on screen from the phone (3 Nov, DM 3336): through the relay, kept in sites.json
        state = self.ok(phone, "kleros1", "length", query="site=0", body={"seconds": 45})
        self.assertEqual(state["screens"][0]["lengths"][:2], [{"auto": 30, "set": 45}, {"auto": None, "set": None}])
        self.assertEqual(json.loads(kiosk.SITES_FILE.read_text())["0"]["seconds"], {SITES[0]: 45})
        self.assertEqual(phone.command("kleros1", "length", 0, "site=0", {"seconds": 2}),
                         (400, {"error": "seconds must be from 5 to 3600 (0: to its end)"}))
        self.assertEqual(self.ok(phone, "kleros1", "length", query="site=0")["screens"][0]["lengths"][0],
                         {"auto": 30, "set": None})  # no body: back to config.toml's

        t = self.fake["kleros2"].screens[1]
        self.assertTrue(self.ok(phone, "kleros2", "pause", i=1)["screens"][1]["paused"] and t.paused)
        self.assertFalse(self.fake["kleros1"].screens[1].paused)
        # the other phone got each change pushed, without asking
        other.wait(lambda m: m["type"] == "state" and m["host"] == "kleros1" and m["online"]
                   and m["state"]["screens"][0]["urls"] == SITES + [url])
        other.wait(lambda m: m["type"] == "state" and m["host"] == "kleros2" and m["online"]
                   and m["state"]["screens"][1]["paused"])
        self.assertTrue(self.logged("relay: kleros1 screen 0: next (from 127.0.0.1) (ws)"))
        self.assertTrue(self.logged("pi: control: jump 2 on screen 0 (via server)"))
        self.assertEqual(self.sync.call_count, 0)

    def test_pi_errors_come_back(self):
        phone = self.phone()
        for host, action, i, body, expect in (
                ("kleros1", "remove", 0, {"url": "https://not.in.rotation.example"},
                 (409, {"error": "that site is not in the rotation"})),
                ("kleros1", "remove", 1, {"url": "https://example.com/one"},
                 (409, {"error": "can't remove the last site"})),
                ("kleros1", "show", 0, {"url": "not a link"}, (400, {"error": "that isn't a web address"})),
                ("kleros1", "dance", 0, None, (409, {"error": "unknown command"})),
                ("kleros1", "next", 2, None, (404, {"error": "no such screen"})),
                ("kleros1", "click", 0, {"x": 2, "y": 0}, (400, {"error": "click needs x and y between 0 and 1"})),
                ("kleros9", "next", 0, None, (404, {"error": "not found"}))):
            self.assertEqual(phone.command(host, action, i, body=body), expect, (host, action))
        self.ok(phone, "kleros1", "jump", query="site=1")  # the video
        self.assertEqual(phone.command("kleros1", "click", body={"x": 0.5, "y": 0.5}),
                         (409, {"error": "a video is playing"}))

    def test_browsing_reaches_devtools(self):
        phone = self.phone()
        s = self.fake["kleros1"].screens[0]
        tab, chromium = s.tabs[0], s.chromium
        chromium.calls.clear()
        self.assertTrue(self.ok(phone, "kleros1", "click", body={"x": 0.5, "y": 0.25})["screens"][0]["paused"])
        at = {"x": 711.0, "y": 200.0}
        self.assertEqual(chromium.browsing(), [
            (tab, "Runtime.evaluate", {"expression": "[innerWidth, innerHeight]", "returnByValue": True}),
            (tab, "Input.dispatchMouseEvent", {"type": "mouseMoved", **at}),
            (tab, "Input.dispatchMouseEvent", {"type": "mousePressed", **at, "button": "left", "clickCount": 1}),
            (tab, "Input.dispatchMouseEvent", {"type": "mouseReleased", **at, "button": "left", "clickCount": 1})])
        for action, body, calls in (
                ("scroll", {"dy": 0.5}, [("Runtime.evaluate", {"expression": "[innerWidth, innerHeight]",
                                                               "returnByValue": True}),
                                         ("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 711.0, "y": 400.0,
                                                                       "deltaX": 0, "deltaY": 400.0})]),
                ("type", {"text": "hello wörld"}, [("Input.insertText", {"text": "hello wörld"})]),
                ("key", {"key": "Enter"}, [
                    ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter",
                                                "windowsVirtualKeyCode": 13, "text": "\r"}),
                    ("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                                                "windowsVirtualKeyCode": 13})]),
                ("back", None, [("Runtime.evaluate", {"expression": "history.back()"})]),
                ("forward", None, [("Runtime.evaluate", {"expression": "history.forward()"})]),
                ("reload", None, [("Page.reload", {})])):
            chromium.calls.clear()
            self.ok(phone, "kleros1", action, body=body)
            self.assertEqual(chromium.browsing(), [(tab, *c) for c in calls], action)
        self.assertTrue(self.logged("pi: control: type 11 characters on screen 0 (via server)"))
        self.assertFalse(self.logged("hello"), "typed text was logged")
        # the "Show now" tab is the one browsed while it is up
        self.ok(phone, "kleros1", "show", body={"url": "kleros.io"})
        chromium.calls.clear()
        self.ok(phone, "kleros1", "reload")
        self.assertEqual(chromium.browsing(), [(s.temp[0], "Page.reload", {})])

    def test_previews_stream_while_watched(self):
        phone = self.phone()
        phone.wait(lambda m: m["type"] == "hello")
        one, two = self.fake["kleros1"].screens[0], self.fake["kleros2"].screens[1]
        phone.watch("kleros1", 0)
        phone.watch("kleros2", 1)
        self.assertTrue(wait_until(lambda: len(phone.frames("kleros1", 0)) >= 2 and phone.frames("kleros2", 1)))
        self.assertEqual(set(phone.frames("kleros1", 0)), {picture(one.tabs[0])})  # from DevTools
        self.assertEqual(set(phone.frames("kleros2", 1)), {picture(two.tabs[0])})
        self.assertEqual((app.find_host("kleros1").ws_want, app.find_host("kleros2").ws_want), ([0], [1]))
        before, started = len(phone.frames("kleros1", 0)), time.monotonic()
        time.sleep(1)
        rate = (len(phone.frames("kleros1", 0)) - before) / (time.monotonic() - started)
        self.assertTrue(2 <= rate <= 4.5, f"{rate:.1f} previews a second")
        self.assertEqual(phone.frames("kleros1", 1) + phone.frames("kleros2", 0), [])  # only what is watched
        # a command brings a fresh picture: a video now, so grim's
        self.ok(phone, "kleros1", "next")
        self.assertTrue(wait_until(lambda: GRIM_JPEG in phone.frames("kleros1", 0)))
        phone.watch("kleros1", 0, on=False)
        phone.watch("kleros2", 1, on=False)
        self.assertTrue(wait_until(lambda: app.find_host("kleros1").ws_want == [] == app.find_host("kleros2").ws_want))
        time.sleep(0.4)
        stopped = len(phone.previews)
        time.sleep(0.8)
        self.assertEqual(len(phone.previews), stopped)  # nobody watches, the Pis stop sending

    def test_http_api_alongside(self):
        phone = self.phone()
        phone.wait(lambda m: m["type"] == "hello")
        pi = self.fake["kleros1"]
        status, state = call("POST", "/api/pi/kleros1/api/screen/0/next")  # the page's polling fallback
        self.assertEqual((status, state), (200, pi.expected()))
        self.assertEqual(pi.screens[0].current, 1)
        self.assertEqual(self.sync.call_count, 0)  # it went over the Pi's WebSocket
        phone.wait(lambda m: m["type"] == "state" and m["host"] == "kleros1" and m["state"] == state)
        self.assertTrue(self.logged("relay: kleros1 screen 0: next (from 127.0.0.1)"))
        self.assertEqual(call("GET", "/api/pi/kleros1/api/state"), (200, pi.expected()))
        # an HTTP preview request makes the Pi stream that screen over its WebSocket for a while
        path = "/api/pi/kleros2/api/screen/1/shot.jpg"
        tab = self.fake["kleros2"].screens[1].tabs[0]
        got = wait_until(lambda: (r := call("GET", path))[0] == 200 and r)
        self.assertEqual(got, (200, picture(tab)))
        self.assertEqual(app.find_host("kleros2").ws_want, [1])
        self.assertTrue(wait_until(lambda: app.find_host("kleros2").ws_want == [], app.WANT_FOR + 2))


class PiReconnects(EndToEnd):
    pis = ("kleros1",)

    def test_killed_websocket_offline_then_back(self):
        phone = self.phone()
        phone.state_of("kleros1")
        h = app.find_host("kleros1")
        first = h.ws
        phone.watch("kleros1", 0)
        self.assertTrue(wait_until(lambda: phone.frames("kleros1", 0)))
        first.kill()  # the line goes dead under the Pi's WebSocket
        offline, _ = phone.state_of("kleros1", online=False)
        self.assertTrue(self.logged("relay: kleros1 went offline (WebSocket closed)"))
        back, m = phone.state_of("kleros1", after=offline, timeout=8)
        self.assertEqual(m["state"], self.fake["kleros1"].expected())
        self.assertIsNot(h.ws, first)
        self.assertTrue(self.logged(f"pi: uplink: lost the WebSocket to http://127.0.0.1:{port}"))
        self.assertEqual(len(self.logged("relay: kleros1 WebSocket connected", since_class=True)), 2)
        self.assertTrue(wait_until(lambda: h.ws_want == [0]))  # the phone still watches: previews start again
        seen = len(phone.frames("kleros1", 0))
        self.assertTrue(wait_until(lambda: len(phone.frames("kleros1", 0)) > seen))
        self.assertTrue(self.ok(phone, "kleros1", "pause")["screens"][0]["paused"])
        self.assertEqual(self.sync.call_count, 0)


class OldRelayFallback(EndToEnd):
    """The Pi's server_url is a relay with no WebSocket for Pis; the phone is on the WebSocket."""
    pis = ("kleros1",)

    @classmethod
    def setUpClass(cls):
        cls.server_url = f"http://127.0.0.1:{old_port}"
        super().setUpClass()

    def test_uplink_uses_http_sync_and_commands_work(self):
        url = f"http://127.0.0.1:{old_port}"
        self.assertEqual(len(self.logged(f"pi: uplink: {url} has no WebSocket (HTTP 404), using HTTP sync",
                                         since_class=True)), 1)
        # (after the relay's first hold: the relay has it online a moment before that)
        self.assertTrue(wait_until(lambda: self.logged(f"pi: uplink: connected to {url}", since_class=True), 2))
        self.assertIsNone(app.find_host("kleros1").ws)
        self.assertGreater(self.sync.call_count, 0)
        pi, phone = self.fake["kleros1"], self.phone()
        _, hello = phone.wait(lambda m: m["type"] == "hello")
        self.assertIn({"host": "kleros1", "online": True}, hello["pis"])
        _, m = phone.state_of("kleros1")
        self.assertEqual(m["state"], pi.expected())

        state = self.ok(phone, "kleros1", "next")  # over the phone's WebSocket, then the Pi's HTTP sync
        self.assertEqual((state["screens"][0]["current"], pi.screens[0].current), (1, 1))
        self.ok(phone, "kleros1", "prev")
        chromium = pi.screens[0].chromium
        chromium.calls.clear()
        self.ok(phone, "kleros1", "scroll", body={"dy": -1})
        self.assertEqual(chromium.browsing()[-1][1:], ("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": 711.0, "y": 400.0, "deltaX": 0, "deltaY": -800}))
        self.assertEqual(call("POST", "/api/pi/kleros1/api/screen/0/resume"), (200, pi.expected()))
        # previews come with the syncs and are pushed to the watching phone
        phone.watch("kleros1", 0)
        self.assertTrue(wait_until(lambda: phone.frames("kleros1", 0), 6))
        self.assertEqual(phone.frames("kleros1", 0)[0], picture(pi.screens[0].tabs[0]))
        self.assertIsNone(app.find_host("kleros1").ws)
        self.assertTrue(self.logged("relay: kleros1 screen 0: next (from 127.0.0.1) (ws)"))


class Refused(EndToEnd):

    def test_wrong_code_4003(self):
        started = time.monotonic()
        phone = self.phone("not-the-code")
        self.assertEqual(phone.wait_closed(), 4003)
        self.assertGreaterEqual(time.monotonic() - started, app.WRONG_AUTH_DELAY)
        self.assertEqual(phone.messages, [{"type": "error", "error": "wrong access code"}])
        self.assertEqual(len(self.logged("relay: wrong access code from 127.0.0.1")), 1)

    def test_wrong_pi_key_403(self):
        stop = RecordingStop()
        pi = FakePi("kleros1", key=WRONG_KEY, stop=stop)
        self.addCleanup(pi.close)
        self.assertTrue(wait_until(lambda: stop.waits, 3))
        time.sleep(1.2)  # a retry after the usual 1 s would show up now
        self.assertEqual(stop.waits, [60])
        self.assertEqual(len(self.logged("relay: wrong Pi key from 127.0.0.1")), 1)
        self.assertEqual(len(self.logged("pi: uplink: the server refused this Pi's key")), 1)
        self.assertIs(online("kleros1"), False)
        self.assertEqual(self.sync.call_count, 0)  # no fallback to HTTP with a refused key


if __name__ == "__main__":
    unittest.main()
