"""The Pi side of docs/websocket-design.md, with the real pi/kiosk.py: the phone's browsing
commands and the DevTools previews against a fake Chromium (its HTTP endpoints, patched in
as in test_relay.py, plus a real DevTools-like WebSocket server that records each command),
tabs that wandered off their site, and the uplink's WebSocket session and its fallback to
HTTP sync against fake relays. Nothing real (Chromium, mpv, grim) is started.

  <venv>/bin/python -m unittest tests.test_ws_pi -v      (from the repo root; about 25 s)

Needs the websockets package (the Pis have Debian's python3-websockets); skipped without it.
KIOSK_TEST_LOGS=1 also prints kiosk.py's log lines as they happen.
"""
import base64
import http.client
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # no __pycache__ in pi/
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pi"))
import kiosk  # noqa: E402

try:
    import websockets
    from websockets.sync.server import serve
except ImportError:
    websockets = None

# placeholders, not the real secrets
CODE = "test-access-code-0000"
PI_KEY = "test-pi-key-" + "0" * 24
LAB_TOKEN = "test-pi-lab-token"
SECRETS = (CODE, PI_KEY, LAB_TOKEN)
SITES = ["https://example.org", "video:clip.mp4", "https://example.net/page"]
PUBLIC = ["https://kleros1.tail0000.ts.net"]
VIEWPORT = [1422, 800]  # 1920x1080 at zoom 1.35, in CSS pixels
JPEG = b"\xff\xd8\xff\xe0 fake preview \xff\xd9"


def wait_until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while not (result := check()) and time.monotonic() < deadline:
        time.sleep(0.02)
    return result


class FakeChromium:
    """One Chromium's DevTools: the HTTP endpoints (patched in as Screen.devtools) and a
    WebSocket per tab that records each command as (tab, method, params) and answers it."""

    def __init__(self):
        self.ids, self.tabs = itertools.count(1), {}  # tab id -> its URL now
        self.calls, self.connections = [], []
        self.errors, self.silent = {}, set()  # methods answered with an error / never answered
        self.closed = False
        self.server = serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if not self.closed:
            self.closed = True
            self.server.shutdown()

    def http(self, screen, path, method="GET"):
        if path.startswith("/json/new?"):
            tab = f"T{next(self.ids)}"
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
        self.connections.append(tab)
        try:
            for raw in ws:
                msg = json.loads(raw)
                method, params = msg["method"], msg.get("params", {})
                self.calls.append((tab, method, params))
                if method in self.silent:
                    continue
                ws.send(json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}))  # noise
                if method in self.errors:
                    ws.send(json.dumps({"id": msg["id"], "error": {"code": -32000,
                                                                   "message": self.errors[method]}}))
                    continue
                result = {}
                if method == "Runtime.evaluate" and params.get("expression") == "[innerWidth, innerHeight]":
                    result = {"result": {"type": "object", "value": VIEWPORT}}
                ws.send(json.dumps({"id": msg["id"], "result": result}))
                if method == "Page.startScreencast":
                    frame = base64.b64encode(self.picture(tab)).decode()
                    ws.send(json.dumps({"method": "Page.screencastFrame",
                                        "params": {"data": frame, "metadata": {}, "sessionId": 1}}))
        except websockets.exceptions.ConnectionClosed:
            pass

    @staticmethod
    def picture(tab):
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


def make_screen(index=0, sites=SITES):
    s = kiosk.Screen(index, f"HDMI-A-{index + 1}", 30, 1.35, list(sites), 8080, base=list(sites))
    s.tabs = [None if kiosk.is_video(u) else s.new_tab(u) for u in s.sites]  # as launch() does
    s.show(0)
    return s


class RecordingStop(threading.Event):
    """An uplink stop event that records how long the uplink waits each time."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return super().wait(timeout)


@unittest.skipIf(websockets is None, "needs the websockets package (python3-websockets on the Pi)")
class PiTest(unittest.TestCase):
    """A Screen on a fresh fake Chromium, with sites.json in a temp folder and the log captured."""

    def setUp(self):
        self.chromium = FakeChromium()
        self.addCleanup(self.chromium.close)
        self.lines = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (mock.patch.object(kiosk.Screen, "devtools",
                                    lambda s, path, method="GET": self.chromium.http(s, path, method)),
                  mock.patch.object(kiosk.Screen, "play", play),
                  mock.patch.object(kiosk, "SITES_FILE", Path(tmp.name) / "sites.json"),
                  mock.patch.object(kiosk, "log", self.log),
                  mock.patch.object(kiosk, "CDP_TIMEOUT", 1)):
            p.start()
            self.addCleanup(p.stop)
        self.screen = make_screen()

    def tearDown(self):
        text = "\n".join(self.lines)
        for secret in SECRETS:
            self.assertNotIn(secret, text, "a secret was logged")

    def log(self, *args):
        line = " ".join(map(str, args))
        self.lines.append(line)
        if os.environ.get("KIOSK_TEST_LOGS"):
            print("pi:", line, flush=True)

    def logged(self, text):
        return [line for line in self.lines if text in line]

    def run_command(self, action, body=None, query=""):
        return kiosk.run_command([self.screen], 0, action, query, body)

    def calls(self):
        """The DevTools commands sent so far, without the tab: (method, params)."""
        return [(method, params) for tab, method, params in self.chromium.calls]


class Browsing(PiTest):

    def test_click(self):
        tab = self.screen.tabs[0]
        self.assertEqual(self.run_command("click", {"x": 0.5, "y": 0.25}), (200, None))
        at = {"x": 711.0, "y": 200.0}
        self.assertEqual(self.chromium.calls, [
            (tab, "Runtime.evaluate", {"expression": "[innerWidth, innerHeight]", "returnByValue": True}),
            (tab, "Input.dispatchMouseEvent", {"type": "mouseMoved", **at}),
            (tab, "Input.dispatchMouseEvent", {"type": "mousePressed", **at, "button": "left", "clickCount": 1}),
            (tab, "Input.dispatchMouseEvent", {"type": "mouseReleased", **at, "button": "left", "clickCount": 1}),
        ])
        self.assertEqual(self.chromium.connections, [tab])  # one connection for the whole click
        self.assertTrue(self.screen.paused)
        self.assertTrue(self.logged("control: click 0.5,0.25 on screen 0"))

    def test_scroll(self):
        self.assertEqual(self.run_command("scroll", {"dy": -1}), (200, None))
        self.assertEqual(self.calls()[1:], [("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": 711.0, "y": 400.0, "deltaX": 0, "deltaY": -800})])
        self.assertEqual(self.run_command("scroll", {"dy": 0.5})[0], 200)
        self.assertEqual(self.calls()[-1][1]["deltaY"], 400.0)  # positive = down

    def test_type(self):
        self.assertEqual(self.run_command("type", {"text": "hello wörld"}), (200, None))
        self.assertEqual(self.calls(), [("Input.insertText", {"text": "hello wörld"})])
        self.assertTrue(self.logged("control: type 11 characters on screen 0"))
        self.assertFalse(self.logged("hello"), "typed text was logged")

    def test_keys(self):
        for key, code in kiosk.KEYS.items():
            self.chromium.calls.clear()
            self.assertEqual(self.run_command("key", {"key": key}), (200, None), key)
            event = {"key": key, "code": key, "windowsVirtualKeyCode": code}
            self.assertEqual(self.calls(), [
                ("Input.dispatchKeyEvent", {"type": "keyDown", **event, **({"text": "\r"} if key == "Enter" else {})}),
                ("Input.dispatchKeyEvent", {"type": "keyUp", **event})], key)
        self.assertEqual(kiosk.KEYS, {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "ArrowLeft": 37,
                                      "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40})

    def test_back_forward_reload(self):
        for action in ("back", "forward", "reload"):
            self.assertEqual(self.run_command(action, {"ignored": True}), (200, None), action)
        self.assertEqual(self.calls(), [("Runtime.evaluate", {"expression": "history.back()"}),
                                        ("Runtime.evaluate", {"expression": "history.forward()"}),
                                        ("Page.reload", {})])

    def test_bad_bodies_400(self):
        for action, body, error in (
                ("click", None, "click needs x and y between 0 and 1"),
                ("click", {"x": 0.5}, "click needs x and y between 0 and 1"),
                ("click", {"x": 1.01, "y": 0}, "click needs x and y between 0 and 1"),
                ("click", {"x": -0.1, "y": 0}, "click needs x and y between 0 and 1"),
                ("click", {"x": "0.5", "y": 0.5}, "click needs x and y between 0 and 1"),
                ("click", {"x": True, "y": 0.5}, "click needs x and y between 0 and 1"),
                ("click", {"x": float("nan"), "y": 0.5}, "click needs x and y between 0 and 1"),
                ("click", [0.5, 0.5], "click needs x and y between 0 and 1"),
                ("scroll", {"dy": 2.5}, "scroll needs dy between -2 and 2"),
                ("scroll", {"dy": "1"}, "scroll needs dy between -2 and 2"),
                ("scroll", None, "scroll needs dy between -2 and 2"),
                ("type", {"text": ""}, "type needs a text of at most 500 characters"),
                ("type", {"text": "x" * 501}, "type needs a text of at most 500 characters"),
                ("type", {"text": 5}, "type needs a text of at most 500 characters"),
                ("key", {"key": "F5"}, "key must be one of Enter, Backspace, Tab, Escape, ArrowLeft, "
                                       "ArrowUp, ArrowRight, ArrowDown"),
                ("key", {"key": ["Enter"]}, None),
                ("key", {"key": "enter"}, None),
                ("key", None, None)):
            status, payload = self.run_command(action, body)
            self.assertEqual(status, 400, (action, body))
            if error:
                self.assertEqual(payload, {"error": error})
        self.assertEqual(self.chromium.calls, [])
        self.assertFalse(self.screen.paused)
        for action, body in (("click", {"x": 0, "y": 1}), ("scroll", {"dy": 2}), ("scroll", {"dy": -2}),
                             ("type", {"text": "x" * 500})):
            self.assertEqual(self.run_command(action, body), (200, None), (action, body))

    def test_video_on_top_409(self):
        self.assertEqual(self.run_command("jump", query="site=1"), (200, None))  # the video
        self.assertIsNotNone(self.screen.player)
        for action, body in (("click", {"x": 0.5, "y": 0.5}), ("reload", None), ("type", {"text": "a"})):
            self.assertEqual(self.run_command(action, body), (409, {"error": "a video is playing"}))
        self.assertEqual(self.chromium.calls, [])
        self.assertFalse(self.screen.paused)

    def test_still_starting_409(self):
        self.screen.tabs = []
        self.assertEqual(self.run_command("reload"), (409, {"error": "screen is still starting"}))

    def test_show_now_tab_is_the_one_browsed(self):
        self.assertEqual(self.run_command("show", {"url": "kleros.io"}), (200, None))
        temp = self.screen.temp[0]
        self.assertEqual(self.chromium.tabs[temp], "https://kleros.io")
        self.assertEqual(self.run_command("reload"), (200, None))
        self.assertEqual(self.chromium.calls, [(temp, "Page.reload", {})])

    def test_pauses_the_rotation_until_resume(self):
        self.screen.next_switch = 0  # due now
        self.assertEqual(self.run_command("scroll", {"dy": 1}), (200, None))
        self.assertTrue(self.screen.state()["paused"])
        self.assertEqual(self.run_command("resume"), (200, None))
        self.assertFalse(self.screen.paused)
        self.assertGreater(self.screen.next_switch, time.monotonic() + 20)  # a full slot again

    def test_devtools_errors_409(self):
        self.chromium.errors["Input.insertText"] = "no focused element"
        self.assertEqual(self.run_command("type", {"text": "a"}), (409, {
            "error": "browser didn't respond (Input.insertText: no focused element)"}))
        self.assertFalse(self.screen.paused)
        self.chromium.silent.add("Page.reload")
        started = time.monotonic()
        status, payload = self.run_command("reload")
        self.assertEqual(status, 409)
        self.assertTrue(payload["error"].startswith("browser didn't respond ("), payload)
        self.assertLess(time.monotonic() - started, kiosk.CDP_TIMEOUT + 1)
        self.chromium.tabs.pop(self.screen.tabs[0])
        self.assertEqual(self.run_command("reload"), (409, {"error": "browser didn't respond (the tab is gone)"}))

    def test_devtools_down_409(self):
        self.chromium.close()
        status, payload = self.run_command("click", {"x": 0.1, "y": 0.1})
        self.assertEqual(status, 409)
        self.assertTrue(payload["error"].startswith("browser didn't respond ("), payload)

    def test_without_websockets_409(self):
        with mock.patch.object(kiosk, "ws_connect", None):
            self.assertEqual(self.run_command("reload"), (409, {
                "error": "browser didn't respond (python3-websockets isn't installed)"}))

    def test_over_the_pis_own_http_api(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     kiosk.make_handler({"access_code": CODE}, "kleros1", [self.screen]))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(path, body, code=CODE):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", path, json.dumps(body), {"X-Code": code, "Content-Type": "application/json"})
            r = conn.getresponse()
            reply = (r.status, json.loads(r.read()))
            conn.close()
            return reply

        status, reply = post("/api/screen/0/click", {"x": 0.25, "y": 0.5})
        self.assertEqual(status, 200)
        self.assertEqual(reply, kiosk.pi_state("kleros1", [], [self.screen]))
        self.assertTrue(reply["screens"][0]["paused"])
        self.assertEqual(self.calls()[2], ("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": 355.5, "y": 400.0, "button": "left", "clickCount": 1}))
        self.assertEqual(post("/api/screen/0/key", {"key": "F1"})[0], 400)
        self.assertEqual(post("/api/screen/0/reload", None, code="wrong"), (403, {"error": "wrong access code"}))


class WanderedTabs(PiTest):

    def test_same_site(self):
        for url, site, same in (
                ("https://example.org/a/b?c", "https://example.org", True),
                ("https://www.example.org/", "https://example.org", True),
                ("http://example.org:8080/", "https://www.example.org/x", True),
                ("https://EXAMPLE.org/", "https://example.org", True),
                ("https://other.example/", "https://example.org", False),
                ("https://sub.example.org/", "https://example.org", False),
                ("chrome-error://chromewebdata/", "https://example.org", False),
                ("about:blank", "https://example.org", False),
                ("file:///home/kiosk/kiosk/pages/a%20b.html", "file:///home/kiosk/kiosk/pages/a b.html", True),
                ("file:///home/kiosk/kiosk/pages/a.html#x", "file:///home/kiosk/kiosk/pages/a.html", True),
                ("file:///home/kiosk/kiosk/pages/b.html", "file:///home/kiosk/kiosk/pages/a.html", False),
                ("https://example.org/home/a.html", "file:///home/a.html", False)):
            self.assertIs(kiosk.same_site(url, site), same, (url, site))

    def test_show_reopens_a_tab_that_wandered_off(self):
        s, old = self.screen, self.screen.tabs[2]
        self.chromium.tabs[old] = "https://elsewhere.example/found/this"
        self.assertEqual(self.run_command("jump", query="site=2"), (200, None))
        self.assertNotEqual(s.tabs[2], old)
        self.assertNotIn(old, self.chromium.tabs)  # closed
        self.assertEqual((self.chromium.tabs[s.tabs[2]], s.showing), (SITES[2], SITES[2]))
        self.assertTrue(self.logged("screen 0: tab for example.net/page was on "
                                    "elsewhere.example/found/this, reopening"))

    def test_show_keeps_a_tab_on_its_own_site(self):
        s, tab = self.screen, self.screen.tabs[2]
        self.chromium.tabs[tab] = "https://www.example.net/another/page?x=1"
        self.assertEqual(self.run_command("jump", query="site=2"), (200, None))
        self.assertEqual(s.tabs[2], tab)
        self.assertFalse(self.logged("reopening"))

    def test_rotation_reopens_it_too(self):
        s, old = self.screen, self.screen.tabs[0]
        self.chromium.tabs[old] = "https://elsewhere.example/"
        with s.lock:  # what loop() does when the time is up
            s.show(s.current + 2)
        self.assertEqual((s.current, s.showing), (2, SITES[2]))
        with s.lock:
            s.show(s.current + 1)
        self.assertEqual((s.current, s.showing), (0, SITES[0]))
        self.assertNotEqual(s.tabs[0], old)

    def test_resume_puts_a_browsed_tab_back(self):
        s, old = self.screen, self.screen.tabs[0]
        self.assertEqual(self.run_command("click", {"x": 0.5, "y": 0.5}), (200, None))
        self.chromium.tabs[old] = "https://linked.example/from/the/click"  # the click followed a link
        s.showing = self.chromium.tabs[old]
        self.assertEqual(self.run_command("resume"), (200, None))
        self.assertFalse(s.paused)
        self.assertNotEqual(s.tabs[0], old)
        self.assertEqual((s.current, s.showing), (0, SITES[0]))  # the new tab is on screen

    def test_resume_leaves_a_tab_on_its_site(self):
        s, tab = self.screen, self.screen.tabs[0]
        self.assertEqual(self.run_command("scroll", {"dy": 1}), (200, None))
        self.chromium.tabs[tab] = "https://example.org/further/down"
        self.assertEqual(self.run_command("resume"), (200, None))
        self.assertEqual(s.tabs[0], tab)

    def test_a_failing_tab_list_changes_nothing(self):
        def no_list(s, path, method="GET"):
            if path == "/json/list":
                raise OSError("no list")
            return self.chromium.http(s, path, method)
        with mock.patch.object(kiosk.Screen, "devtools", no_list):
            self.assertEqual(self.run_command("jump", query="site=2"), (200, None))
            self.assertEqual(self.screen.showing, SITES[2])


class NewTabs(PiTest):
    """Issue 2 (2 Nov): a link that opens a new tab (target=_blank) put that tab in front on the
    TV, but previews, browsing and the state stayed on the hidden tab underneath."""

    def open_tab(self, url, tab="P1"):
        """A page opens a tab, as Chromium does for a target=_blank link: it is in front."""
        self.chromium.tabs[tab] = url
        self.screen.showing = url
        return tab

    def follow(self):
        with self.screen.lock:  # what loop() does every 0.5 s
            self.screen.follow_tabs()

    def test_a_tab_a_link_opened_is_followed(self):
        s = self.screen
        self.assertEqual(self.run_command("click", {"x": 0.5, "y": 0.5}), (200, None))
        popup = self.open_tab("https://court.example/cases/12?from=link")
        self.assertEqual(s.capture(), FakeChromium.picture(s.tabs[0]))  # before: the hidden tab (the bug)
        self.follow()
        self.follow()
        self.assertEqual(s.active_tab(), popup)
        self.assertEqual(s.capture(), FakeChromium.picture(popup))  # the preview is what the TV shows
        self.assertEqual(s.state()["page"], "court.example/cases/12")
        self.assertEqual(s.state()["current"], 0)  # still the same place in the rotation
        self.assertEqual(self.logged("in a new tab"),
                         ["screen 0: a page opened court.example/cases/12 in a new tab, following it"])
        self.assertFalse(self.logged("from=link"), "a query string was logged")

    def test_browsing_goes_to_the_new_tab_at_once(self):
        self.assertEqual(self.run_command("click", {"x": 0.5, "y": 0.5}), (200, None))
        popup = self.open_tab("https://court.example/")
        self.chromium.calls.clear()  # no loop turn in between: browse() looks for itself
        self.assertEqual(self.run_command("scroll", {"dy": 1}), (200, None))
        self.assertEqual({tab for tab, method, params in self.chromium.calls}, {popup})

    def test_resume_closes_it_and_shows_the_site_again(self):
        s = self.screen
        self.run_command("click", {"x": 0.5, "y": 0.5})
        popup = self.open_tab("https://court.example/")
        self.follow()
        self.assertEqual(self.run_command("resume"), (200, None))
        self.assertNotIn(popup, self.chromium.tabs)  # closed, so tabs don't pile up
        self.assertEqual((s.popups, s.active_tab(), s.showing, s.paused), ([], s.tabs[0], SITES[0], False))
        self.follow()
        self.assertEqual(s.state()["page"], "example.org")

    def test_next_and_show_now_close_it(self):
        s = self.screen
        popup = self.open_tab("https://court.example/")
        self.follow()
        self.assertEqual(self.run_command("next"), (200, None))
        self.assertNotIn(popup, self.chromium.tabs)
        self.assertEqual(s.popups, [])
        self.run_command("jump", query="site=0")
        popup = self.open_tab("https://court.example/", "P2")
        self.follow()
        self.assertEqual(self.run_command("show", {"url": "kleros.io"}), (200, None))
        self.assertNotIn(popup, self.chromium.tabs)
        self.assertEqual(s.active_tab(), s.temp[0])

    def test_tabs_that_close_themselves_are_dropped(self):
        s = self.screen
        first = self.open_tab("https://one.example/", "P1")
        self.follow()
        second = self.open_tab("https://two.example/", "P2")  # a link in the first one
        self.follow()
        self.assertEqual((s.popups, s.active_tab()), ([first, second], second))
        del self.chromium.tabs[second]  # window.close()
        self.follow()
        self.assertEqual((s.active_tab(), s.state()["page"]), (first, "one.example"))
        del self.chromium.tabs[first]
        self.follow()
        self.assertEqual((s.popups, s.active_tab(), s.state()["page"]), ([], s.tabs[0], "example.org"))

    def test_page_follows_navigation_in_the_same_tab(self):
        s, tab = self.screen, self.screen.tabs[0]
        self.assertIsNone(s.state()["page"])  # not looked yet
        self.follow()
        self.assertEqual(s.state()["page"], "example.org")
        self.chromium.tabs[tab] = "https://example.org/cases/12"  # a link in the same tab
        self.follow()
        self.assertEqual(s.state()["page"], "example.org/cases/12")
        with s.lock:
            s.show(2)  # the rotation moves on: no stale page until the next look
        self.assertIsNone(s.state()["page"])
        self.follow()
        self.assertEqual(s.state()["page"], "example.net/page")
        self.run_command("show", {"url": "kleros.io"})
        self.follow()
        self.assertEqual((s.state()["page"], s.state()["temp"]), ("kleros.io", "kleros.io"))
        self.run_command("jump", query="site=1")  # a video on top: no page
        self.follow()
        self.assertIsNone(s.state()["page"])

    def test_starting_or_a_failing_tab_list_changes_nothing(self):
        s = self.screen
        tabs, s.tabs = s.tabs, []
        self.open_tab("http://127.0.0.1:8080/status")  # the boot status page, while starting
        self.follow()
        self.assertEqual(s.popups, [])
        s.tabs = tabs
        self.follow()
        popup = s.popups[-1]

        def no_list(screen, path, method="GET"):
            if path == "/json/list":
                raise OSError("no list")
            return self.chromium.http(screen, path, method)
        with mock.patch.object(kiosk.Screen, "devtools", no_list):
            self.follow()  # no exception: loop() would count it towards restarting Chromium
        self.assertEqual(s.active_tab(), popup)


class Previews(PiTest):

    def setUp(self):
        super().setUp()
        self.ran = []

        def run(args, **kwargs):
            self.ran.append(args[0])
            out = b"P6 fake ppm" if args[0] == "grim" else b"\xff\xd8 grim \xff\xd9"
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr=b"")
        p = mock.patch.object(kiosk.subprocess, "run", run)
        p.start()
        self.addCleanup(p.stop)

    def test_devtools_for_web_pages(self):
        self.assertEqual(self.screen.capture(), FakeChromium.picture(self.screen.tabs[0]))
        self.assertEqual(self.calls(), [
            ("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": 768, "maxHeight": 768}),
            ("Page.stopScreencast", {})])
        self.assertEqual(self.ran, [])
        self.run_command("show", {"url": "kleros.io"})  # the "Show now" tab when there is one
        self.assertEqual(self.screen.capture(), FakeChromium.picture(self.screen.temp[0]))

    def test_grim_for_videos(self):
        self.run_command("jump", query="site=1")
        self.assertEqual(self.screen.capture(), b"\xff\xd8 grim \xff\xd9")
        self.assertEqual(self.ran, ["grim", "ffmpeg"])
        self.assertEqual(self.chromium.calls, [])

    def test_grim_command(self):
        self.run_command("jump", query="site=1")
        with mock.patch.object(kiosk.subprocess, "run", wraps=kiosk.subprocess.run) as run:
            self.screen.capture()
        self.assertEqual(run.call_args_list[0].args[0], ["grim", "-o", "HDMI-A-1", "-s", "0.4", "-t", "ppm", "-"])
        self.assertEqual(run.call_args_list[1].args[0][:5], ["ffmpeg", "-loglevel", "error", "-f", "ppm_pipe"])
        self.assertEqual(run.call_args_list[1].kwargs["input"], b"P6 fake ppm")

    def test_grim_when_devtools_fails(self):
        self.chromium.errors["Page.startScreencast"] = "not now"
        for _ in range(3):
            self.assertEqual(self.screen.capture(), b"\xff\xd8 grim \xff\xd9")
            self.screen.devtools_off_until = 0
        self.assertEqual(len(self.logged("screen 0: DevTools preview failed "
                                         "(Page.startScreencast: not now), using grim")), 1)
        self.chromium.errors.clear()
        self.chromium.silent.add("Page.startScreencast")  # no frame ever comes
        started = time.monotonic()
        self.assertEqual(self.screen.capture(), b"\xff\xd8 grim \xff\xd9")
        self.assertLess(time.monotonic() - started, kiosk.CDP_TIMEOUT + 1)
        # after a failure, grim only for 30 s: a stuck DevTools doesn't slow every preview down
        self.chromium.silent.clear()
        tried = len(self.chromium.connections)
        self.assertEqual(self.screen.capture(), b"\xff\xd8 grim \xff\xd9")
        self.assertEqual(len(self.chromium.connections), tried)
        self.screen.devtools_off_until = time.monotonic() - 1
        self.assertEqual(self.screen.capture(), FakeChromium.picture(self.screen.tabs[0]))
        with mock.patch.object(kiosk, "ws_connect", None):
            self.ran.clear()
            self.assertEqual(self.screen.capture(), b"\xff\xd8 grim \xff\xd9")
            self.assertEqual(self.ran, ["grim", "ffmpeg"])

    def test_snapshot_reuses_and_preview_refreshes(self):
        with mock.patch.object(kiosk.Screen, "capture", side_effect=[b"one", b"two", b"three"]) as capture:
            self.assertEqual(self.screen.snapshot(), b"one")
            self.assertEqual(self.screen.snapshot(), b"one")  # reused for 2 s (the HTTP path)
            self.assertEqual(self.screen.preview(), b"two")  # the WebSocket stream: always fresh
            self.assertEqual(self.screen.snapshot(), b"two")
            self.assertEqual(capture.call_count, 2)


class FakeRelay:
    """A relay with the WebSocket, from websockets' own server: records each Pi connection
    (the handshake and every message); the test talks back through Conn.send."""

    def __init__(self, refuse=None):
        self.refuse, self.handshakes, self.conns = refuse, [], []
        self.server = serve(self.handler, "127.0.0.1", 0, process_request=self.check)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def check(self, connection, request):
        self.handshakes.append((request.path, request.headers))
        if self.refuse:
            return connection.respond(self.refuse, "refused\n")
        return None

    def handler(self, ws):
        conn = Conn(ws)
        self.conns.append(conn)
        try:
            for msg in ws:
                conn.got.append(json.loads(msg) if isinstance(msg, str) else msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        conn.closed.set()


class Conn:
    def __init__(self, ws):
        self.ws, self.got, self.closed = ws, [], threading.Event()

    def send(self, msg):
        self.ws.send(json.dumps(msg))

    def of_type(self, kind):
        return [m for m in self.got if isinstance(m, dict) and m.get("type") == kind]

    def frames(self):
        return [m for m in self.got if isinstance(m, bytes)]

    def result(self, id):
        return next((m for m in self.of_type("result") if m.get("id") == id), None)


class OldRelayHandler(BaseHTTPRequestHandler):
    """The relay as it was before the WebSocket (HTTP/1.0 answers, like server/app.py)."""

    def reply(self, status, body, headers=()):
        raw = json.dumps(body).encode()
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        relay = self.server
        relay.gets.append((self.path, self.headers))
        self.reply(relay.ws_status, {"error": "not found"},
                   [("Location", relay.location)] if relay.location else [])

    def do_POST(self):
        relay = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        relay.syncs.append((self.path, self.headers, body))
        if relay.fail_syncs:
            relay.fail_syncs -= 1
            return self.reply(503, {"error": "restarting"})
        requests, relay.requests = relay.requests, []
        self.reply(200, {"requests": requests, "want_shots": []})

    def log_message(self, *args):
        pass


class Http11Handler(OldRelayHandler):
    protocol_version = "HTTP/1.1"


class OldRelay(ThreadingHTTPServer):
    """GET /pi/ws answers ws_status (404: no WebSocket here); POST /pi/sync hands out
    `requests` at once (no hold) and records each sync."""
    daemon_threads = True

    def __init__(self, ws_status=404, location=None, http11=False):
        super().__init__(("127.0.0.1", 0), Http11Handler if http11 else OldRelayHandler)
        self.ws_status, self.location = ws_status, location
        self.gets, self.syncs, self.requests, self.fail_syncs = [], [], [], 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self):
        self.shutdown()
        self.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class UplinkTest(PiTest):
    """Two Screens on the fake Chromium (previews patched to a fixed JPEG) and the real uplink."""

    def setUp(self):
        super().setUp()
        self.screens = [self.screen, make_screen(1, ["https://example.com/one"])]
        p = mock.patch.object(kiosk.Screen, "capture", lambda s: JPEG + bytes([s.index]))
        p.start()
        self.addCleanup(p.stop)

    def start_uplink(self, url, stop=None):
        stop = stop or threading.Event()
        cfg = {"server_url": url, "server_pi_key": PI_KEY, "server_lab_token": LAB_TOKEN}
        thread = threading.Thread(target=kiosk.uplink, args=(cfg, "kleros1", self.screens, PUBLIC, stop),
                                  daemon=True)
        thread.start()

        def close():
            stop.set()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "the uplink didn't stop")
        self.addCleanup(close)
        return stop, thread

    def state(self):
        return kiosk.pi_state("kleros1", PUBLIC, self.screens)

    def relay(self, kind, **kwargs):
        relay = kind(**kwargs)
        self.addCleanup(relay.close)  # after the uplink has stopped (cleanups run last first)
        return relay


class UplinkOverWebSocket(UplinkTest):

    def connect(self, relay, n=1):
        self.assertTrue(wait_until(lambda: len(relay.conns) >= n and relay.conns[n - 1].got),
                        "the uplink didn't connect and say hello")
        return relay.conns[n - 1]

    def test_ws_url(self):
        def ws_url(url):
            return kiosk.Uplink({"server_url": url}, "kleros1", [], [], threading.Event()).ws_url
        self.assertEqual(ws_url("https://kleros-devcon-screens.labs.example/"),
                         "wss://kleros-devcon-screens.labs.example/pi/ws")
        self.assertEqual(ws_url("http://127.0.0.1:18080"), "ws://127.0.0.1:18080/pi/ws")
        self.assertEqual(ws_url("https://host.example/prefix"), "wss://host.example/prefix/pi/ws")

    def test_hello_previews_commands_state(self):
        relay = self.relay(FakeRelay)
        with mock.patch.object(kiosk, "STATE_EVERY", 60):
            self.start_uplink(f"http://127.0.0.1:{relay.port}/")
            conn = self.connect(relay)
            path, headers = relay.handshakes[0]
            self.assertEqual((path, headers["X-Pi-Key"], headers["X-Lab-Token"]), ("/pi/ws", PI_KEY, LAB_TOKEN))
            self.assertEqual(conn.got[0], {"type": "hello", "host": "kleros1", "state": self.state()})
            self.assertTrue(self.logged(f"uplink: connected to http://127.0.0.1:{relay.port} over WebSocket"))

            time.sleep(0.5)
            self.assertEqual(conn.frames(), [])  # no previews until a phone watches
            conn.send({"type": "want", "screens": [1, 7, "0", -1]})
            self.assertTrue(wait_until(lambda: len(conn.frames()) >= 2))
            started, before = time.monotonic(), len(conn.frames())
            time.sleep(1)
            rate = (len(conn.frames()) - before) / (time.monotonic() - started)
            self.assertTrue(2.5 <= rate <= 4.5, f"{rate:.1f} previews a second")
            self.assertEqual(set(conn.frames()), {b"\x01" + JPEG + b"\x01"})
            conn.send({"type": "want", "screens": [0, 1]})
            self.assertTrue(wait_until(lambda: b"\x00" + JPEG + b"\x00" in conn.frames()))
            conn.send({"type": "want", "screens": []})
            time.sleep(0.4)
            stopped = len(conn.frames())
            time.sleep(0.6)
            self.assertEqual(len(conn.frames()), stopped)  # no more once nobody watches

            conn.send({"type": "command", "id": "c1", "method": "POST", "path": "/api/screen/0/next",
                       "query": "", "body": None})
            self.assertTrue(wait_until(lambda: conn.result("c1")))
            result = conn.result("c1")
            self.assertEqual((result["status"], result["body"]["screens"][0]["current"]), (200, 1))
            self.assertEqual(result["body"], self.state())
            # the state follows by itself (compared every 250 ms)
            self.assertTrue(wait_until(lambda: any(m["state"]["screens"][0]["current"] == 1
                                                   for m in conn.of_type("state")), 2))
            self.assertTrue(self.logged("control: next  on screen 0 (via server)"))

            for q in ({"id": "c2", "method": "GET", "path": "/api/screen/0/next"},
                      {"id": "c3", "method": "POST", "path": "/etc/passwd"},
                      {"id": "c4", "method": "POST", "path": "/api/screen/0/prev", "query": "", "body": None},
                      {"id": "c5", "method": "POST", "path": "/api/screen/0/click", "query": "",
                       "body": {"x": 0.5, "y": 0.5}},
                      {"id": "c6", "method": "POST", "path": "/api/screen/0/show", "query": "",
                       "body": {"url": "not a link"}}):
                conn.send({"type": "command", **q})
            self.assertTrue(wait_until(lambda: conn.result("c6")))
            self.assertEqual([(conn.result(i)["status"], conn.result(i)["body"].get("error"))
                              for i in ("c2", "c3", "c4", "c5", "c6")],
                             [(404, "not found"), (404, "not found"), (200, None), (200, None),
                              (400, "that isn't a web address")])
            self.assertTrue(conn.result("c5")["body"]["screens"][0]["paused"])
            self.assertIn(("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 711.0, "y": 400.0,
                                                        "button": "left", "clickCount": 1}), self.calls())

    def test_navigation_is_pushed_as_state(self):
        relay = self.relay(FakeRelay)
        # one screen: the fake Chromium is shared, and screen 1's tab would look opened by a page
        # (on a Pi each screen has its own Chromium)
        for tab in self.screens.pop(1).tabs:
            self.chromium.tabs.pop(tab, None)
        with mock.patch.object(kiosk, "STATE_EVERY", 60):
            self.start_uplink(f"http://127.0.0.1:{relay.port}")
            conn = self.connect(relay)
            self.chromium.tabs["P1"] = "https://court.example/cases/12"  # a link opened a tab
            with self.screen.lock:
                self.screen.follow_tabs()  # what loop() does every 0.5 s
            self.assertTrue(wait_until(lambda: any(m["state"]["screens"][0]["page"] == "court.example/cases/12"
                                                   for m in conn.of_type("state")), 2))

    def test_state_at_least_every_10_s(self):
        relay = self.relay(FakeRelay)
        with mock.patch.object(kiosk, "STATE_EVERY", 0.5):
            self.start_uplink(f"http://127.0.0.1:{relay.port}")
            conn = self.connect(relay)
            time.sleep(1.4)
        self.assertGreaterEqual(len(conn.of_type("state")), 2)
        self.assertTrue(all(m["state"] == self.state() for m in conn.of_type("state")))

    def test_drop_reconnects(self):
        relay = self.relay(FakeRelay)
        self.start_uplink(f"http://127.0.0.1:{relay.port}")
        first = self.connect(relay)
        first.ws.close()
        second = self.connect(relay, 2)
        self.assertEqual(second.got[0]["type"], "hello")
        self.assertTrue(self.logged(f"uplink: lost the WebSocket to http://127.0.0.1:{relay.port}"))
        second.send({"type": "command", "id": "after", "method": "POST", "path": "/api/screen/1/pause"})
        self.assertTrue(wait_until(lambda: second.result("after")))
        self.assertEqual(second.result("after")["status"], 200)

    def test_stop_closes_the_connection(self):
        relay = self.relay(FakeRelay)
        stop, thread = self.start_uplink(f"http://127.0.0.1:{relay.port}")
        conn = self.connect(relay)
        stop.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(conn.closed.wait(3))
        self.assertFalse(self.logged("lost the WebSocket"))

    def test_wrong_key_403_backs_off(self):
        relay = self.relay(FakeRelay, refuse=403)
        stop = RecordingStop()
        self.start_uplink(f"http://127.0.0.1:{relay.port}", stop)
        self.assertTrue(wait_until(lambda: stop.waits, 3))
        time.sleep(1.2)
        self.assertEqual((stop.waits, len(relay.handshakes)), ([60], 1))
        self.assertEqual(len(self.logged("uplink: the server refused this Pi's key")), 1)


class UplinkFallback(UplinkTest):

    def test_old_relay_404_uses_http_sync_at_once(self):
        relay = self.relay(OldRelay)
        stop = RecordingStop()
        with mock.patch.object(kiosk, "WS_RETRY", 1):
            self.start_uplink(relay.url, stop)
            self.assertTrue(wait_until(lambda: relay.syncs, 2))
            path, headers = relay.gets[0]
            self.assertEqual((path, headers["X-Pi-Key"], headers["X-Lab-Token"], headers["Upgrade"]),
                             ("/pi/ws", PI_KEY, LAB_TOKEN, "websocket"))
            path, headers, body = relay.syncs[0]
            self.assertEqual((path, headers["X-Pi-Key"], headers["Content-Type"]),
                             ("/pi/sync", PI_KEY, "application/json"))
            self.assertEqual(body, {"host": "kleros1", "state": self.state(), "results": [], "shots": {}})
            relay.requests.append({"id": "r1", "method": "POST", "path": "/api/screen/0/next", "query": "",
                                   "body": None})
            answered = wait_until(lambda: [r for _, _, b in relay.syncs for r in b["results"] if r["id"] == "r1"])
            self.assertTrue(answered)
            self.assertEqual((answered[0]["status"], answered[0]["body"]["screens"][0]["current"]), (200, 1))
            # tries the WebSocket again after WS_RETRY, then goes on syncing
            self.assertTrue(wait_until(lambda: len(relay.gets) >= 2, 3))
            synced = len(relay.syncs)
            self.assertTrue(wait_until(lambda: len(relay.syncs) > synced, 2))
        self.assertTrue(all(w <= 0.5 for w in stop.waits), stop.waits)  # never a back-off
        self.assertEqual(len(self.logged(f"uplink: {relay.url} has no WebSocket (HTTP 404), using HTTP sync")), 1)
        self.assertEqual(len(self.logged(f"uplink: connected to {relay.url}")), 1)

    def test_http11_404_too(self):
        relay = self.relay(OldRelay, http11=True)
        self.start_uplink(relay.url)
        self.assertTrue(wait_until(lambda: relay.syncs, 2))
        self.assertEqual(len(relay.gets), 1)
        self.assertTrue(self.logged("has no WebSocket (HTTP 404)"))

    def test_three_failures_then_http_sync(self):
        relay = self.relay(OldRelay, ws_status=502)
        stop = RecordingStop()
        with mock.patch.object(kiosk, "WS_RETRY", 0.5):
            self.start_uplink(relay.url, stop)
            self.assertTrue(wait_until(lambda: relay.syncs, 6))
            self.assertEqual((len(relay.gets), stop.waits[:2]), (3, [1, 2]))
            # in fallback, a failed retry goes straight back to HTTP (no back-off)
            self.assertTrue(wait_until(lambda: len(relay.gets) >= 5, 3))
        self.assertNotIn(4, stop.waits)
        self.assertEqual(len(self.logged(f"uplink: can't reach {relay.url} (HTTP 502), retrying")), 1)
        self.assertEqual(len(self.logged("uplink: using HTTP sync, trying the WebSocket again every 0.5 s")), 1)

    def test_redirects_not_followed(self):
        target = FakeRelay()
        self.addCleanup(target.close)
        relay = self.relay(OldRelay, ws_status=302, location=f"ws://127.0.0.1:{target.port}/pi/ws", http11=True)
        self.start_uplink(relay.url)
        self.assertTrue(wait_until(lambda: relay.syncs, 6))
        self.assertEqual((len(relay.gets), target.handshakes), (3, []))  # the key never went there
        self.assertTrue(self.logged(f"uplink: can't reach {relay.url} (HTTP 302), retrying"))

    def test_back_on_the_websocket_when_the_server_answers_again(self):
        relay = self.relay(OldRelay)
        relay.fail_syncs = 1  # e.g. the relay restarting
        self.start_uplink(relay.url)
        self.assertTrue(wait_until(lambda: len(relay.gets) >= 2, 4), "no WebSocket try after the restart")
        self.assertGreaterEqual(len(relay.syncs), 2)
        self.assertEqual(len(self.logged(f"uplink: can't reach {relay.url} (HTTP Error 503")), 1)

    def test_without_websockets_http_only(self):
        relay = self.relay(OldRelay)
        with mock.patch.object(kiosk, "ws_connect", None):
            self.start_uplink(relay.url)
            self.assertTrue(wait_until(lambda: len(relay.syncs) >= 2, 3))
        self.assertEqual(relay.gets, [])
        self.assertEqual(len(self.logged("uplink: python3-websockets isn't installed, using HTTP sync only")), 1)


if __name__ == "__main__":
    unittest.main()
