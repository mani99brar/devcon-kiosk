"""End-to-end test of the relay server (server/app.py) with fake Pis built from the real
pi/kiosk.py: their Screens drive a fake Chromium, and the real uplink thread syncs with
the relay over HTTP. Short timings are patched in; nothing real (Chromium, mpv, grim,
Docker) is started, and nothing outside a temp folder is written.

  python3 -m unittest tests.test_relay -v      (from the repo root; about 20 s)

KIOSK_TEST_LOGS=1 also prints both sides' log lines as they happen.
"""
import http.client
import importlib.util
import itertools
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True  # no __pycache__ in pi/ or server/
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pi"))
import kiosk  # noqa: E402

_spec = importlib.util.spec_from_file_location("relay_app", REPO / "server" / "app.py")
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
TIMINGS = {"SYNC_HOLD": 0.5, "OFFLINE_AFTER": 2, "REPLY_TIMEOUT": 3, "WRONG_AUTH_DELAY": 0.2}
JPEG = b"\xff\xd8\xff\xe0 fake preview \xff\xd9"
SITES = ["https://example.org", "video:clip.mp4", "https://example.net/page"]

lines, lines_lock = [], threading.Lock()  # both sides' log lines, "relay: ..." and "pi: ..."
port = None
server = None


def capture(side):
    def log(*args):
        line = f"{side}: " + " ".join(map(str, args))
        with lines_lock:
            lines.append(line)
        if os.environ.get("KIOSK_TEST_LOGS"):
            print(line, flush=True)
    return log


# a fake Chromium DevTools endpoint and mpv, like fake_pi.py; `showing` is what the TV shows
tab_ids, open_tabs = itertools.count(1), {}


def devtools(self, path, method="GET"):
    if path.startswith("/json/new?"):
        tab = f"T{next(tab_ids)}"
        open_tabs[tab] = urllib.parse.unquote(path.removeprefix("/json/new?"))
        return {"id": tab}
    if path.startswith("/json/close/"):
        open_tabs.pop(path.removeprefix("/json/close/"), None)
        return "Target is closing"
    if path.startswith("/json/activate/"):
        self.showing = open_tabs[path.removeprefix("/json/activate/")]
        return "Target activated"
    raise OSError(f"fake Chromium has no {path}")


def play(self, site):
    self.showing = site


def setUpModule():
    global port, server
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    patches = [
        mock.patch.object(kiosk.Screen, "devtools", devtools),
        mock.patch.object(kiosk.Screen, "play", play),
        mock.patch.object(kiosk.Screen, "snapshot", lambda self: JPEG),
        mock.patch.object(kiosk, "SITES_FILE", Path(tmp.name) / "sites.json"),
        mock.patch.object(kiosk, "log", capture("pi")),
        mock.patch.object(app, "log", capture("relay")),
        *(mock.patch.object(app, name, value) for name, value in TIMINGS.items()),
    ]
    for p in patches:
        p.start()
        unittest.addModuleCleanup(p.stop)
    app.configure(CODE, PI_KEY, PIS)
    server = app.make_server(0, "127.0.0.1")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=app.reaper, daemon=True).start()  # logs Pis going offline


def tearDownModule():
    server.shutdown()
    server.server_close()
    app.configure(CODE, PI_KEY, ())  # nothing left for the reaper to log


def call(method, path, body=None, code=CODE, headers=None):
    """One request to the relay: (status, body, headers); a JSON body comes back parsed."""
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


def command(action, i=0, query="", body=None, host="kleros1", headers=None):
    """What the hub page sends for a button: POST /api/pi/<host>/api/screen/<i>/<action>."""
    path = f"/api/pi/{host}/api/screen/{i}/{action}" + (f"?{query}" if query else "")
    return call("POST", path, body, headers=headers)


def pi_state(host="kleros1"):
    return call("GET", f"/api/pi/{host}/api/state")


def sync(payload, key=PI_KEY):
    """One raw POST /pi/sync, as a Pi sends it."""
    return call("POST", "/pi/sync", payload, code=None, headers=None if key is None else {"X-Pi-Key": key})


def online(host):
    """The host's "online" in the hub's /api/state, or None if it isn't listed."""
    status, hub, _ = call("GET", "/api/state")
    return {p["host"]: p["online"] for p in hub["pis"]}.get(host)


def hub_list(*up):
    return [{"host": h, "base": f"/api/pi/{h}", "online": h in up} for h in PIS]


def wait_until(check, timeout=5):
    deadline = time.monotonic() + timeout
    while not (result := check()) and time.monotonic() < deadline:
        time.sleep(0.05)
    return result


def dead_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_screen(index, sites):
    s = kiosk.Screen(index, f"HDMI-A-{index + 1}", 30, 1.35, list(sites), 8080, base=list(sites))
    s.tabs = [None if kiosk.is_video(u) else s.new_tab(u) for u in s.sites]  # as launch() does
    s.show(0)
    return s


class FakePi:
    """A Pi from the real kiosk.py: two Screens on the fake Chromium and the real uplink."""

    def __init__(self, host, key=PI_KEY, lab_token=LAB_TOKEN, server_url=None, stop=None):
        self.host, self.public_urls = host, [f"https://{host}.tail0000.ts.net"]
        self.screens = [fake_screen(0, SITES), fake_screen(1, ["https://example.com/one"])]
        cfg = {"server_url": server_url or f"http://127.0.0.1:{port}/", "server_pi_key": key}
        if lab_token:
            cfg["server_lab_token"] = lab_token
        self.stop = stop or threading.Event()
        self.thread = threading.Thread(target=kiosk.uplink, daemon=True,
                                       args=(cfg, host, self.screens, self.public_urls, self.stop))
        self.thread.start()

    def expected(self):
        """What the relay should show for this Pi: its own /api/state with peers = []."""
        return {**kiosk.pi_state(self.host, self.public_urls, self.screens), "peers": []}

    def close(self):
        self.stop.set()
        self.thread.join(5)
        if self.thread.is_alive():
            raise AssertionError(f"{self.host}'s uplink didn't stop")


class MutePi:
    """A Pi that keeps syncing but never runs what it is sent."""

    def __init__(self, host):
        self.host, self.seen, self.stop = host, [], threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            status, reply, _ = sync({"host": self.host, "state": raw_state(self.host), "results": []})
            if status == 200:
                self.seen += reply["requests"]
            else:
                time.sleep(0.2)

    def close(self):
        self.stop.set()
        self.thread.join(5)


def raw_state(host, current=0):
    return {"host": host, "peers": [f"https://{host}.tail0000.ts.net"],
            "screens": [{"index": 0, "output": "HDMI-A-1", "ready": True, "paused": False,
                         "current": current, "temp": None, "interval": 30,
                         "sites": ["example.org", "example.net"],
                         "urls": ["https://example.org", "https://example.net"]}]}


class RecordingStop(threading.Event):
    """An uplink stop event that records how long the uplink waits each time."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return super().wait(timeout)


class Background(threading.Thread):
    """fn(*args) in a thread; result() waits for what it returned."""

    def __init__(self, fn, *args, **kwargs):
        super().__init__(daemon=True)
        self.fn, self.args, self.kwargs, self.value, self.error = fn, args, kwargs, None, None
        self.start()

    def run(self):
        try:
            self.value = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            self.error = e

    def result(self, timeout=10):
        self.join(timeout)
        if self.is_alive():
            raise AssertionError(f"{self.fn.__name__} didn't return within {timeout} s")
        if self.error:
            raise self.error
        return self.value


class RelayTest(unittest.TestCase):
    """Each class starts with a relay that knows only the PIS names."""

    @classmethod
    def setUpClass(cls):
        app.configure(CODE, PI_KEY, PIS)
        with lines_lock:
            cls.class_start = len(lines)

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
            return [line for line in lines[self.class_start if since_class else self.test_start:]
                    if text in line]

    def check_headers(self, headers):
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIsNone(headers["Access-Control-Allow-Origin"])


class RelayAlone(RelayTest):
    """No Pi connected."""

    def test_healthz(self):
        status, body, headers = call("GET", "/healthz", code=None)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.check_headers(headers)

    def test_page_is_control_html(self):
        page = (REPO / "pi" / "control.html").read_bytes()
        for path in ("/", f"/?lab_token={PHONE_TOKEN}"):
            status, body, headers = call("GET", path, code=None)
            self.assertEqual((status, body), (200, page))
            self.assertTrue(headers["Content-Type"].startswith("text/html"))
            self.check_headers(headers)

    def test_hub_state_lab_token_not_required(self):
        for path in ("/api/state", f"/api/state?lab_token={PHONE_TOKEN}"):
            status, body, headers = call("GET", path)
            self.assertEqual((status, body), (200, {"hub": True, "host": "server", "peers": [],
                                                    "pis": hub_list()}))
            self.check_headers(headers)

    def test_wrong_code_403(self):
        wrong = (403, {"error": "wrong access code"})
        for code in (None, "", "not-the-code"):
            started = time.monotonic()
            status, body, headers = call("GET", "/api/state", code=code)
            self.assertEqual((status, body), wrong)
            self.assertGreaterEqual(time.monotonic() - started, app.WRONG_AUTH_DELAY)
            self.check_headers(headers)
        for method, path in (("GET", "/api/pi/kleros1/api/state"),
                             ("GET", "/api/pi/kleros1/api/screen/0/shot.jpg"),
                             ("POST", "/api/pi/kleros1/api/screen/0/next")):
            self.assertEqual(call(method, path, code="not-the-code")[:2], wrong, path)
        self.assertEqual(len(self.logged("relay: wrong access code from 127.0.0.1")), 6)

    def test_wrong_pi_key_403(self):
        for key in (None, "", WRONG_KEY):
            status, body, headers = sync({"host": "kleros1", "state": raw_state("kleros1"),
                                          "results": []}, key=key)
            self.assertEqual((status, body), (403, {"error": "wrong Pi key"}))
            self.check_headers(headers)
        self.assertIs(online("kleros1"), False)
        self.assertEqual(len(self.logged("relay: wrong Pi key from 127.0.0.1")), 3)

    def test_bad_host_name_400(self):
        for host in ("Kleros1", "kleros_1", "kleros1.local", "../x", "", "a" * 64, None, 7):
            status, body, _ = sync({"host": host, "state": raw_state("x"), "results": []})
            self.assertEqual((status, body), (400, {"error": "bad host name"}), host)
        self.assertEqual(sync(b"{not json")[:2], (400, {"error": "bad JSON"}))
        self.assertEqual(call("GET", "/api/state")[1]["pis"], hub_list())

    def test_unknown_host_and_path_404(self):
        for method, path in (("POST", "/api/pi/kleros9/api/screen/0/next"),
                             ("GET", "/api/pi/kleros9/api/state"),
                             ("GET", "/api/pi/kleros9/api/screen/0/shot.jpg"),
                             ("POST", "/api/pi/kleros1/api/screen/0/NEXT"),
                             ("POST", "/api/screen/0/next"),
                             ("GET", "/nope")):
            status, body, headers = call(method, path)
            self.assertEqual((status, body), (404, {"error": "not found"}), path)
            self.check_headers(headers)

    def test_pi_not_connected_yet_503(self):
        self.assertEqual(pi_state("kleros2")[:2], (503, {"error": "kleros2 is not connected"}))
        self.assertEqual(command("next", host="kleros2")[:2],
                         (503, {"error": "kleros2 is not connected"}))
        self.assertEqual(call("GET", "/api/pi/kleros2/api/screen/0/shot.jpg")[:2],
                         (503, {"error": "no picture yet"}))


class RelayedCommands(RelayTest):
    """kleros1 is a fake Pi running the real uplink (with a lab token, which the relay ignores)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pi = FakePi("kleros1")
        if not wait_until(lambda: online("kleros1")):
            cls.pi.close()
            raise AssertionError("the fake Pi never came online")

    @classmethod
    def tearDownClass(cls):
        cls.pi.close()

    def send(self, action, i=0, query="", body=None, expect=200, headers=None):
        status, reply, headers = command(action, i, query, body, headers=headers)
        self.assertEqual(status, expect, reply)
        self.check_headers(headers)
        if status == 200:  # the Pi's state right after the command, peers removed
            self.assertEqual(reply, self.pi.expected())
            self.assertEqual(pi_state()[:2], (200, reply))
        return reply

    def test_host_listed_and_online(self):
        self.assertEqual(call("GET", "/api/state")[:2],
                         (200, {"hub": True, "host": "server", "peers": [], "pis": hub_list("kleros1")}))
        time.sleep(app.SYNC_HOLD * 2.5)  # so the state below came with a sync, not a command answer
        self.assertEqual(pi_state()[:2], (200, self.pi.expected()))
        self.assertTrue(self.logged("relay: kleros1 is online (from 127.0.0.1)", since_class=True))
        self.assertTrue(self.logged(f"pi: uplink: connected to http://127.0.0.1:{port}", since_class=True))

    def test_next_prev_jump(self):
        s = self.pi.screens[0]
        self.send("jump", query="site=0")
        self.assertEqual((s.current, s.showing), (0, SITES[0]))
        self.assertEqual(self.send("next")["screens"][0]["current"], 1)
        self.assertEqual(s.showing, SITES[1])  # a video: the (fake) mpv plays it
        self.send("next")
        self.assertEqual((s.current, s.showing), (2, SITES[2]))
        self.send("prev")
        self.assertEqual(s.current, 1)
        self.send("jump", query="site=2")
        self.assertEqual((s.current, s.showing), (2, SITES[2]))
        self.assertEqual(self.pi.screens[1].current, 0)  # only screen 0 was told
        self.assertTrue(self.logged("pi: control: jump 2 on screen 0 (via server)"))
        self.assertTrue(self.logged("relay: kleros1 screen 0: next (from 127.0.0.1)"))

    def test_pause_resume(self):
        s = self.pi.screens[0]
        reply = self.send("pause", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.2"})
        self.assertTrue(reply["screens"][0]["paused"] and s.paused)
        self.assertTrue(self.logged("relay: kleros1 screen 0: pause (from 203.0.113.7)"))
        reply = self.send("resume")
        self.assertFalse(reply["screens"][0]["paused"] or s.paused)

    def test_show_now(self):
        s = self.pi.screens[0]
        reply = self.send("show", body={"url": "kleros.io"})
        self.assertEqual((reply["screens"][0]["temp"], reply["screens"][0]["paused"]), ("kleros.io", True))
        self.assertEqual(s.showing, "https://kleros.io")
        reply = self.send("resume")
        self.assertEqual((reply["screens"][0]["temp"], reply["screens"][0]["paused"]), (None, False))
        self.assertEqual(s.showing, s.sites[s.current])

    def test_add_remove(self):
        url = "https://example.com/added"
        reply = self.send("add", body={"url": url})
        self.assertEqual(reply["screens"][0]["urls"], SITES + [url])
        self.assertEqual(reply["screens"][0]["current"], len(SITES))
        self.assertEqual(self.pi.screens[0].showing, url)
        self.assertEqual(json.loads(kiosk.SITES_FILE.read_text())["0"], {"added": [url], "removed": []})
        reply = self.send("remove", body={"url": url})
        self.assertEqual(reply["screens"][0]["urls"], SITES)
        self.assertEqual(json.loads(kiosk.SITES_FILE.read_text())["0"], {"added": [], "removed": []})

    def test_pi_errors_passed_through(self):
        for args, expect, error in (
                (("remove", 0, "", {"url": "https://not.in.rotation.example"}), 409,
                 "that site is not in the rotation"),
                (("remove", 1, "", {"url": "https://example.com/one"}), 409, "can't remove the last site"),
                (("show", 0, "", None), 409, "missing link"),
                (("dance",), 409, "unknown command"),
                (("show", 0, "", {"url": "not a link"}), 400, "that isn't a web address"),
                (("next", 2), 404, "no such screen")):
            self.assertEqual(self.send(*args, expect=expect), {"error": error}, args)
        self.assertEqual(self.pi.screens[1].sites, ["https://example.com/one"])
        self.send("next", body=b"{not json")  # bad JSON reaches the Pi as null

    def test_lab_token_not_passed_to_the_pi(self):
        with mock.patch.object(kiosk, "run_command", wraps=kiosk.run_command) as run:
            self.send("jump", query=f"site=1&lab_token={PHONE_TOKEN}")
        self.assertEqual(run.call_args.args[3], "site=1")
        self.assertEqual(self.pi.screens[0].current, 1)

    def test_preview_503_then_200(self):
        path = "/api/pi/kleros1/api/screen/1/shot.jpg"
        self.assertEqual(call("GET", path)[:2], (503, {"error": "no picture yet"}))
        # that request told the relay to want screen 1; the uplink sends it with a later sync
        got = wait_until(lambda: (r := call("GET", path))[0] == 200 and r)
        self.assertTrue(got, "the preview never arrived")
        status, body, headers = got
        self.assertEqual((status, body, headers["Content-Type"]), (200, JPEG, "image/jpeg"))
        self.check_headers(headers)


class PiNeverAnswers(RelayTest):
    """Raw syncs from the test, no kiosk.py."""

    def test_504_when_the_pi_syncs_but_never_answers(self):
        pi = MutePi("kleros2")
        self.addCleanup(pi.close)
        self.assertTrue(wait_until(lambda: online("kleros2")))
        started = time.monotonic()
        status, body, headers = command("next", host="kleros2")
        took = time.monotonic() - started
        self.assertEqual((status, body), (504, {"error": "kleros2 didn't answer"}))
        self.check_headers(headers)
        self.assertGreaterEqual(took, app.REPLY_TIMEOUT - 0.1)
        self.assertLess(took, app.REPLY_TIMEOUT + 2)
        pi.close()
        self.assertEqual([(r["method"], r["path"], r["query"], r["body"]) for r in pi.seen],
                         [("POST", "/api/screen/0/next", "", None)])  # handed out once
        self.assertTrue(self.logged("relay: kleros2 screen 0: next failed: kleros2 didn't answer"))
        late = {"id": pi.seen[0]["id"], "status": 200, "body": raw_state("kleros2", current=1)}
        self.assertEqual(sync({"host": "kleros2", "state": raw_state("kleros2"), "results": [late]})[0], 200)
        h = app.find_host("kleros2")
        with h.cond:  # the late answer was dropped, nothing left behind
            self.assertEqual((h.queue, h.delivered, h.results), ([], set(), {}))
            self.assertEqual(h.state["screens"][0]["current"], 0)

    def test_held_sync_returns_when_a_command_comes(self):
        with mock.patch.object(app, "SYNC_HOLD", 3), mock.patch.object(app, "OFFLINE_AFTER", 10):
            held = Background(sync, {"host": "kleros2", "state": raw_state("kleros2"), "results": []})
            self.assertTrue(wait_until(lambda: online("kleros2")))
            time.sleep(0.2)
            queued = time.monotonic()
            phone = Background(command, "next", host="kleros2")
            status, reply, _ = held.result()
            self.assertLess(time.monotonic() - queued, 1.5)  # not the 3 s hold
        self.assertEqual(status, 200)
        [req] = reply["requests"]
        self.assertEqual((req["method"], req["path"]), ("POST", "/api/screen/0/next"))
        answer = raw_state("kleros2", current=1)
        sync({"host": "kleros2", "state": raw_state("kleros2"),
              "results": [{"id": req["id"], "status": 200, "body": answer}]})
        self.assertEqual(phone.result()[:2], (200, {**answer, "peers": []}))
        self.assertEqual(pi_state("kleros2")[:2], (200, {**answer, "peers": []}))


class PiGoesOffline(RelayTest):

    def test_offline_after_the_pi_stops(self):
        pi = FakePi("kleros1", lab_token=None)
        self.addCleanup(pi.close)
        self.assertTrue(wait_until(lambda: online("kleros1")))
        self.assertEqual(pi_state()[:2], (200, pi.expected()))  # as sent with the syncs
        self.assertEqual(command("next")[0], 200)
        pi.close()
        stopped = time.monotonic()
        self.assertTrue(wait_until(lambda: online("kleros1") is False, app.OFFLINE_AFTER + 2))
        self.assertLess(time.monotonic() - stopped, app.OFFLINE_AFTER + 1)
        self.assertEqual(pi_state()[:2], (503, {"error": "kleros1 is not connected"}))
        self.assertEqual(command("next")[:2], (503, {"error": "kleros1 is not connected"}))
        self.assertTrue(wait_until(lambda: self.logged("relay: kleros1 went offline"), 3))


class UplinkBacksOff(RelayTest):

    def test_wrong_pi_key(self):
        stop = RecordingStop()
        pi = FakePi("kleros1", key=WRONG_KEY, stop=stop)
        self.addCleanup(pi.close)
        self.assertTrue(wait_until(lambda: stop.waits, 3))
        time.sleep(1.5)  # a retry after the usual 1 s would show up now
        self.assertEqual(stop.waits, [60])
        self.assertEqual(len(self.logged("relay: wrong Pi key from 127.0.0.1")), 1)
        self.assertEqual(len(self.logged("pi: uplink: the server refused this Pi's key")), 1)
        self.assertIs(online("kleros1"), False)

    def test_server_unreachable(self):
        url = f"http://127.0.0.1:{dead_port()}"
        stop = RecordingStop()
        pi = FakePi("kleros1", server_url=url, stop=stop)
        self.addCleanup(pi.close)
        self.assertTrue(wait_until(lambda: len(stop.waits) >= 2, 4))
        self.assertEqual(stop.waits[:2], [1, 2])
        self.assertEqual(len(self.logged(f"pi: uplink: can't reach {url} (")), 1)


if __name__ == "__main__":
    unittest.main()
