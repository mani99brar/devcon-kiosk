export const meta = {
  name: 'build-websocket-browsing-ui',
  description: 'Build the WebSocket transport, phone browsing and the new phone page UI from docs/websocket-design.md, then test end to end',
  phases: [
    { title: 'Build', detail: 'server, Pi side and phone page in parallel, disjoint files' },
    { title: 'Integrate', detail: 'end-to-end WebSocket tests across real server + real kiosk.py + page, fix mismatches' },
  ],
}

// Paths default to the vea-vps layout; pass args {repo, venv, scratch} to override.
const REPO = (args && args.repo) || '~/devcon-kiosk'
const VENV_PY = (args && args.venv) || '~/.venvs/kiosk-test/bin/python'
const SCRATCH = (args && args.scratch) || '/tmp/kiosk-wf'

const CONTEXT = `
Repo: ${REPO} (public GitHub repo; never git commit or push, the main session commits).
FIRST read docs/websocket-design.md (the approved spec for THIS change, binding), then docs/server-design.md (the existing relay design, still valid) and skim README.md.
Project: two Raspberry Pi 5 booth kiosks (kleros1, kleros2) run pi/kiosk.py (Python 3.13 stdlib + Debian python3-websockets 15.0.1) under labwc; Chromium is driven over DevTools; pi/control.html is the phone control page, served by each Pi (port 8080, Tailscale "Pi mode") and by the relay server/app.py ("hub mode") which runs on Francisco's host behind his proxy edge1.
The Pis are LIVE at a venue and the relay is LIVE: anything that changes behaviour of the existing HTTP paths is a regression. The HTTP polling path must keep working exactly as today and remains the fallback.
Rules:
- Match the surrounding code style (plain, few comments only where non-obvious, existing naming, kiosk.py's log() format). server/app.py stays standard library only. kiosk.py may import websockets (Debian package on the Pi) but must still run without it (HTTP only).
- Never read, print or copy secret.toml or any .env. Never write real tokens or codes; use placeholders.
- Do not ssh to the Pis, do not run deploy.sh, do not use docker, do not git commit/push, do not pip install into the system python.
- A test venv with websockets 15.0.1 exists: ${VENV_PY} (use it for anything needing websockets). Node is available (check `node -e "console.log(typeof WebSocket)"`: Node 18 has no global WebSocket; then use the venv python client wherever a WebSocket client is needed, and stub WebSocket in tests/test_page.js).
- Only edit the files assigned to you; other agents edit other files at the same time.
- Scratch files: ${SCRATCH}/ (create it).
- Existing tests must keep passing: python3 -m unittest tests.test_relay (21 tests), node tests/test_page.js, bash tests/test_watchdog.sh.
- Report honestly: list what you ran and the result; anything not run is "not tested".`

const RESULT = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    files: { type: 'array', items: { type: 'string' } },
    tests: { type: 'array', items: { type: 'object', properties: { what: { type: 'string' }, result: { type: 'string' } }, required: ['what', 'result'] } },
    deviations: { type: 'array', items: { type: 'string' } },
    concerns: { type: 'array', items: { type: 'string' } },
  },
  required: ['summary', 'files', 'tests', 'deviations', 'concerns'],
}

phase('Build')
const [server, pi, page] = await parallel([
  () => agent(`${CONTEXT}

YOUR JOB: the server side (spec sections "Connections", "Messages", "Server").
Your files ONLY: server/app.py, tests/test_ws_server.py (new), server/README.md (add a short WebSocket section for Francisco: paths, auth, ping 20 s, close after 50 s silent, frame limits, and that polling stays).
- Hand-roll RFC 6455 in app.py inside the existing ThreadingHTTPServer handler: GET /pi/ws and GET /ws/phone with Upgrade: websocket (case-insensitive header checks, Sec-WebSocket-Version 13, Sec-WebSocket-Key -> Accept). Check X-Pi-Key BEFORE answering 101 on /pi/ws (403 JSON otherwise, after the usual delay). A non-upgrade GET to those paths -> 426 or 400 with JSON.
- Frames: read with exact-length reads (handle partial recv), require masking from clients (close 1002 if unmasked), support 7/16/64-bit lengths, text/binary/continuation/ping/pong/close, enforce the size limits (close 1009), reply to ping with pong, echo close. Server sends unmasked frames. Each socket write goes through a per-connection lock.
- Server ping every 20 s; close a connection silent for 50 s. Use socket timeouts so threads don't hang forever.
- Integrate with the existing per-host state exactly as the spec says (one state shared by both transports; commands from the HTTP API and from phone WebSockets go to the Pi's WebSocket when open, else to the HTTP sync queue; previews from either path update the stored shot AND are pushed to watching phones; want = HTTP watchers + WS watchers; offline/online transitions pushed to phones).
- Phone writer: one writer thread + outbound queue per phone connection, coalescing previews per host/screen, JSON in order.
- Keep every existing HTTP path, status and log line unchanged; tests/test_relay.py must still pass untouched.
- tests/test_ws_server.py (stdlib unittest, run with the venv python; skip with a clear message if websockets is missing): handshake, masking rule, 16- and 64-bit lengths, ping/pong, close, oversize 1009, wrong Pi key 403 before 101, phone auth ok / wrong code 4003, Pi hello -> phone sees state, phone command -> Pi WS gets it -> result back to phone, HTTP command routed over the Pi WS, preview pushed to a watching phone and coalesced for a slow one, want messages, Pi disconnect -> phone gets online:false, an HTTP-sync Pi and a WS Pi at once. Use websockets.sync.client as the Pi/phone client where it helps (it is the real library the Pis use) and raw sockets for frame edge cases.
Run all your tests plus tests/test_relay.py and report.`, { label: 'build:server-ws', phase: 'Build', schema: RESULT }),

  () => agent(`${CONTEXT}

YOUR JOB: the Pi side (spec sections "Pi", "Phone browsing").
Your files ONLY: pi/kiosk.py, pi/install.sh (add python3-websockets to the package list only), tests/test_ws_pi.py (new).
- Uplink: prefer the WebSocket exactly as the spec says (url scheme mapping, /pi/ws, headers, websockets.sync.client.connect with ping_interval=20, open_timeout=15, and no redirect following; check what websockets 15 does on a 3xx and make sure the key is never sent elsewhere), hello, reader loop running commands via run_command, state compared every 250 ms and sent on change and every 10 s, preview thread honouring 'want' (fresh frame right after a command, at most 4/s/screen), one send lock. Fall back to the EXISTING HTTP sync loop per the spec rules (3 failures, or 400/404/426 at once; retry WS every 5 min while in fallback; 403 -> 60 s backoff + one log line). Without the websockets module: HTTP only, one log line. Keep the existing uplink() signature usable (main() calls it; tests pass stop=Event) and keep the HTTP loop's behaviour identical when used.
- DevTools helper: connect to the active tab's webSocketDebuggerUrl with websockets.sync.client (short timeouts), send one CDP command, wait for its id, close. Active tab = the 'Show now' temp tab if set, else tabs[current] (None for a video entry).
- Previews: Page.captureScreenshot (jpeg, quality 60, clip to the viewport with a scale giving ~768 px width) when no video player is running; grim+ffmpeg at -s 0.4 otherwise or on any CDP error. Keep Screen.snapshot() working for the HTTP path (it may use the same capture function).
- Browsing actions in run_command per the spec table (click/scroll/type/key/back/forward/reload) with body validation (400), pausing the rotation, 409 when a video is on top; resume puts a wandered rotation tab back; show() checks the tab's URL host via /json/list and reopens the site if it wandered (ignore a leading www.; for file:// compare the path). Keep existing actions byte-identical.
- tests/test_ws_pi.py (unittest, venv python): fake Chromium (HTTP endpoints as in tests/test_relay.py) PLUS a fake DevTools WebSocket server (websockets.sync.server) that records CDP messages and answers them; cover each browsing action's CDP calls and validation, the 409 video case, pause-on-browse, tab reset on show/resume, CDP screenshot used for web pages and grim fallback (patch subprocess), and uplink fallback/backoff logic with a fake relay (WS refused 404 -> HTTP sync used; WS works -> hello/state/result/preview frames; drop -> reconnect).
Run your tests plus tests/test_relay.py (system python3) and report.`, { label: 'build:pi-ws', phase: 'Build', schema: RESULT }),

  () => agent(`${CONTEXT}

YOUR JOB: the phone page (spec sections "Phone page", "Phone page UI/UX", and the phone side of "Messages").
Your files ONLY: pi/control.html, tests/test_page.js.
If a Skill tool is available to you, invoke the skill "frontend-design:frontend-design" first and follow its guidance within the spec's constraints.
- Redesign the UI/UX per the spec: header with Kleros wordmark + connection status + logout; one card per TV with status pill, large 16:9 live preview (tap = click at that spot with a ripple), primary Prev/Pause-Resume/Next row, a browsing group (Back, Forward, Reload, Scroll up/down, text box + Type + Enter), Show now / Add to rotation with the link box, the site list (current one highlighted, jump on tap, remove with confirm), toasts for errors, pressed/busy button states, offline card, good login screen. Dark theme, system fonts, no external resources, accessible (labels, contrast, focus, aria-live, reduced motion). Mobile-first; two columns on wide screens.
- Hub mode: WebSocket client per the spec (/ws/phone + ?lab_token=, wss on https, auth first message, hello/state/result/binary previews, watch on/off with visibility, reconnect backoff 1->30 s, HTTP polling while not connected, close 4003 = wrong code -> forget code + login). Pi mode: unchanged HTTP flows (direct Pi API + Funnel peers), with the browsing controls sent as HTTP POSTs.
- Keep these behaviours (tests/test_page.js covers them; keep those cases passing, update selectors only if the markup changes): lab_token only on relative URLs; #c= stripped while keeping ?lab_token; JSON-vs-HTML error handling (HTML 403 keeps the code and says the link was rejected; JSON 403 logs out; HTML 5xx shows status); half-typed link inputs and preview images survive refreshes; no Funnel peers in hub mode.
- Extend tests/test_page.js with a stub WebSocket: hub mode connects to the right URL with the token, sends auth first, renders from pushes, commands go over WS with ids and results update the UI, 4003 logs out, a failed WS falls back to polling, previews from binary frames set the image, watch messages follow visibility; and browsing: a tap at a known spot on the preview sends click with the right x/y fractions, scroll/type/key/back/forward/reload send the right actions/bodies in both modes.
Run node tests/test_page.js and node --check on the extracted script; report.`, { label: 'build:page', phase: 'Build', schema: RESULT }),
])

phase('Integrate')
const integrate = await agent(`${CONTEXT}

YOUR JOB: end-to-end integration of the three parts just built, and fixing mismatches (spec section "Tests").
Reports from the builders:
SERVER: ${JSON.stringify(server)}
PI: ${JSON.stringify(pi)}
PAGE: ${JSON.stringify(page)}
Write tests/test_ws.py (unittest, venv python, skips cleanly without websockets; must finish in well under 2 minutes): start the REAL server/app.py in a thread on a free port with short timings; build fake Pis from the REAL pi/kiosk.py (fake Chromium HTTP endpoints + a fake DevTools WebSocket, as in the builders' tests) running the REAL uplink against ws://127.0.0.1:<port>; drive it like a phone with websockets.sync.client on /ws/phone. Cover: both Pis connect over WS and show online; phone auth + hello/state; next/pause/resume/show/add/remove over the phone WS reach the Pi and results come back; a click/scroll/type over the phone WS reaches the fake DevTools as the right CDP calls; previews stream to a watching phone and stop when unwatched; the HTTP API still works alongside (an HTTP phone command is delivered over the Pi WS); kill the Pi WS -> phone sees offline -> uplink reconnects; a relay that refuses /pi/ws (404) -> the uplink falls back to HTTP sync and commands still work; wrong code 4003; wrong Pi key 403.
Also run the page against the real server: start server/app.py, then (Node 22 with a global WebSocket if available, else the venv python websockets client) connect to /ws/phone exactly like the page does (auth, watch, command) to prove the browser-side protocol matches the server (the page logic itself is covered by tests/test_page.js).
Run everything: the venv for tests/test_ws*.py, system python3 for tests/test_relay.py, node tests/test_page.js, bash tests/test_watchdog.sh, py_compile on server/app.py and pi/kiosk.py. Fix failures by changing whichever side is off-spec (minimal changes; you may edit server/app.py, pi/kiosk.py, pi/control.html and any tests). Rerun until all green, and run the whole set 3 times to catch flaky timing.`, { label: 'integrate', phase: 'Integrate', schema: RESULT })

return { server, pi, page, integrate }