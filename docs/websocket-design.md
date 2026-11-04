# WebSocket transport and phone browsing: design

Status: requested by Manmeet on 2 Nov 2026 after the relay went live ("there's a delay which makes it very
hard to use"); paths and limits agreed with Francisco's setup (edge1 + lab2 already forward Upgrade/Connection,
idle timeout 60 s). Builds on docs/server-design.md, which stays valid: **the HTTP polling path stays as the
automatic fallback** until the WebSocket path has been tested at the booth.

## Why

Measured on 2 Nov through the live relay:
- A command took ~0.9 s: each Pi sync opens a new TLS connection to edge1 (~0.3 s; RTT 119 ms), and a command
  needs two syncs (deliver, then answer).
- The preview refreshed only every 3–8 s: the page polls every 3 s and the Pi uploads at most every 2 s.
- One grim + ffmpeg capture on the Pi takes ~0.5 s.

Target: tap → TV reacts in ~0.3 s; preview shows it ~0.5–1 s later; status updates instantly.

## Connections

| Who | URL | Auth |
|---|---|---|
| Pi | `wss://kleros-devcon-screens.labs.accesoprohibido.com/pi/ws` | handshake headers `X-Lab-Token` (edge1) and `X-Pi-Key` (app; checked before the 101, else 403) |
| Phone | `wss://kleros-devcon-screens-manager.labs.accesoprohibido.com/ws/phone?lab_token=<phone token>` | `lab_token` is for edge1 only (the app ignores it); the access code goes in the **first message**, never in the URL (keeps it out of proxy logs) |

Locally (no edge1) the same paths work over `ws://127.0.0.1:18080` / `:18081`.

Pings: the server sends a ping frame every **20 s** on every WebSocket and closes a connection that has sent
nothing (no pong, no message) for **50 s**. The Pi client also pings every 20 s. This keeps both proxies'
60 s idle timeout happy.

Sizes: text frames at most 256 KB (state JSON is ~2–5 KB); binary preview frames at most 1 MB (a preview is
~20–40 KB). The server rejects bigger frames by closing with 1009. Fragmented messages may be accepted
(continuation frames) but nobody sends them.

## Messages

All text frames are JSON objects with a `type`. Binary frames are previews.

### Pi ↔ server (`/pi/ws`)

Pi → server:
- `{"type": "hello", "host": "kleros1", "state": {...}}`: must be the first message (within 10 s, else close).
  `host` matches `^[a-z0-9-]{1,63}$`. `state` is exactly what the Pi's own `GET /api/state` returns. A second
  connection for the same host replaces the first (the old one is closed).
- `{"type": "state", "state": {...}}`: whenever the state changes (the Pi compares every 250 ms) and at least
  every 10 s.
- `{"type": "result", "id": "…", "status": 200, "body": {...}}`: the answer to a command.
- binary: `[screen index: 1 byte][JPEG bytes]`: a preview of that screen.

Server → Pi:
- `{"type": "command", "id": "…", "method": "POST", "path": "/api/screen/0/next", "query": "site=2", "body": {...} | null}`
- `{"type": "want", "screens": [0]}`: the screens a phone is watching right now; sent on every change and right
  after `hello`. `[]` means stop sending previews.

### Phone ↔ server (`/ws/phone`)

Phone → server:
- `{"type": "auth", "code": "…"}`: first message, within 10 s. Wrong → (after 1 s) `{"type": "error", "error":
  "wrong access code"}` then close code **4003**. The page treats 4003 like the HTTP 403 JSON answer (forget the
  saved code, show the login box).
- `{"type": "command", "id": "<client id>", "host": "kleros1", "path": "/api/screen/0/next", "query": "site=2", "body": {...} | null}`:
  same validation as the HTTP relay (`path` `^/api/screen/\d{1,4}/[a-z]{1,20}$`, body at most 4096 bytes when
  encoded, the phone's `lab_token` never forwarded). Answer: `{"type": "result", "id": …, "status": …, "body": …}`
  (503 offline, 504 after 15 s, else the Pi's answer).
- `{"type": "watch", "host": "kleros1", "screen": 0, "on": true|false}`: start or stop previews of a screen.
  Watching ends when the phone disconnects.

Server → phone (after a good `auth`):
- `{"type": "hello", "pis": [{"host": "kleros1", "online": true}, ...]}`, then one `state` message per known Pi.
- `{"type": "state", "host": "kleros1", "online": true, "state": {...}}` on every Pi state change, and
  `{"type": "state", "host": …, "online": false, "state": null}` when a Pi goes offline.
- `{"type": "result", …}` as above.
- binary: `[n: 1 byte][host: n ASCII bytes][screen index: 1 byte][JPEG bytes]`.

Only the latest preview per host/screen is kept for each phone: a slow phone drops frames, never blocks the Pi.
Each phone connection has its own writer thread and an outbound queue in which a new preview replaces an unsent
older one for the same host/screen; JSON messages keep their order.

## Server (`server/app.py`)

- Still Python standard library only. RFC 6455 is hand-rolled in `app.py` (handshake with
  `Sec-WebSocket-Accept`, masked client frames, unmasked server frames, lengths 7/16/64-bit, ping/pong, close,
  continuation), with its own tests. `ThreadingHTTPServer` keeps one thread per connection; a WebSocket upgrade
  hands the socket to the connection's thread for its lifetime. Fine for ~2 Pis and ~10–20 phones.
- One shared per-host state, used by both transports: a host is online if its WebSocket is open **or** it synced
  over HTTP in the last 10 s. A phone command (from either the HTTP API or a phone WebSocket) goes over the Pi's
  WebSocket when it is open, else into the HTTP sync queue (as today). Previews from either path update the stored
  preview (so `GET …/shot.jpg` keeps working) and are pushed to watching phone WebSockets. `want` merges HTTP
  watchers (`shot.jpg` in the last 10 s) and WebSocket watchers.
- Every existing HTTP path, status and test stays as it is.
- Logs: WebSocket connect/disconnect per Pi and per phone (with the client address), wrong code / wrong key, and
  the same command lines as today (`… (ws)` suffix for commands that arrived over a phone WebSocket). Never a code,
  key or token.

## Pi (`pi/kiosk.py`, `pi/install.sh`)

- `install.sh` installs `python3-websockets` explicitly (today it is only there because yt-dlp pulls it in).
- The uplink prefers the WebSocket (`server_url` with https→wss / http→ws, path `/pi/ws`, headers `X-Pi-Key` and
  `X-Lab-Token`, `websockets.sync.client.connect`, ping 20 s, open timeout 15 s, no redirects). It falls back to
  the existing HTTP sync loop when the WebSocket can't connect: after 3 failed attempts in a row, or at once when
  the handshake is refused with 400/404/426 (an older server). In fallback it keeps syncing over HTTP and retries
  the WebSocket every 5 minutes. A 403 is the key being refused: log it and back off 60 s, as today. If
  `websockets` can't be imported, it uses HTTP only and logs that once.
- Over the WebSocket: send `hello`; one reader loop runs commands through `run_command` (same validation as today)
  and answers with `result`; the state is compared every 250 ms and sent on change (and every 10 s); while the
  server `want`s screens, a preview thread sends frames: a fresh one right after any command, then as fast as
  capture allows but at most 4 per second per screen. All sends go through one lock.
- Faster previews for web pages: when no video is playing, capture the active tab with Chrome DevTools
  `Page.captureScreenshot` (JPEG, quality 60, clipped to the viewport with a scale giving a ~768 px wide image).
  When a video is playing (mpv on top), or if the DevTools capture fails, use grim + ffmpeg as today (`-s 0.4`).
- DevTools WebSocket: `websockets.sync.client.connect(tab["webSocketDebuggerUrl"])` to the active tab (the "Show
  now" tab if any, else the current site's tab), short timeouts, one connection per operation (simple, ~10 ms
  locally).

## Phone browsing (Pi commands, any transport)

New `run_command` actions; each acts on the active tab, pauses the rotation (so it doesn't switch away while
someone browses), and answers 409 `a video is playing` if a video is on top:

| Action | Body | DevTools |
|---|---|---|
| `click` | `{"x": 0..1, "y": 0..1}` (fraction of the screen) | `Runtime.evaluate` innerWidth/innerHeight, then `Input.dispatchMouseEvent` moved/pressed/released (left, clickCount 1) |
| `scroll` | `{"dy": -2..2}` (fraction of the viewport height; positive = down) | `Input.dispatchMouseEvent` type `mouseWheel` at the centre |
| `type` | `{"text": "…"}` (at most 500 characters) | `Input.insertText` |
| `key` | `{"key": "Enter" \| "Backspace" \| "Tab" \| "Escape" \| "ArrowUp" \| "ArrowDown" \| "ArrowLeft" \| "ArrowRight"}` | `Input.dispatchKeyEvent` keyDown + keyUp with the right `code`/`windowsVirtualKeyCode` |
| `back` / `forward` | none | `Runtime.evaluate` `history.back()` / `history.forward()` |
| `reload` | none | `Page.reload` |

Bad bodies → 400. `resume` additionally puts a rotation tab that has wandered off its site back on it. The same
check runs whenever the rotation shows a tab: if the tab's current URL (from `/json/list`) is on a different host
than the site's URL (ignoring a leading `www.`; for `file://` the path), close it and open the site again. This also
tidies up after the physical mouse.

## Phone page (`pi/control.html`)

- Hub mode (served by the relay): open the phone WebSocket (`/ws/phone` + `?lab_token=…`, `wss:` on https),
  `auth` with the saved code, render from `hello`/`state` pushes, send commands and `watch` over it, show binary
  previews via object URLs (revoke the old one). Watch the screens that are on the page while the page is visible;
  stop when hidden. Reconnect with backoff 1 → 30 s; **while it isn't connected, use today's HTTP polling** (so a
  network that blocks WebSockets still works). Close code 4003 → wrong code (login box).
- Browsing controls under each preview, in both modes (hub over the WebSocket, Pi mode over HTTP POST): tap the
  preview to click at that spot (x/y as fractions of the image), Scroll ▲ / ▼, Back, Forward, Reload, a text box
  with Type and Enter. While browsing, the existing orange bar style says the rotation is paused; Resume continues.
- Everything else keeps working as now (login, Prev/Pause/Next, Show now, Add, Remove, sites list, the Pi-mode
  Funnel peers flow, `lab_token` only on relative URLs, the JSON-vs-HTML error handling).

## Phone page UI/UX (requested with this change)

The page is used one-handed on a phone at a busy booth, by ~10 teammates. Goals:
- **One card per TV**: host name + a status pill (Rotating / Paused / Showing a link / Offline) at the top, the live
  preview large and edge to edge (16:9), then the controls. Both TVs on one scrolling page; on a wide screen, side by side.
- **Big, obvious controls**: Prev / Pause-Resume / Next as the primary row (at least 48 px tall, clear icons + labels);
  browsing (Back, Forward, Reload, Scroll ▲/▼, Type + Enter) in a compact second group; "Show now" / "Add to rotation"
  with the link box; the site list as tappable rows showing which one is on now, with a remove button that asks first.
- **Instant feedback**: a tapped button shows a pressed/busy state until the answer comes back; errors appear as a short
  toast near the bottom (not a line of red text at the top); a tap on the preview shows a brief ripple where it was
  tapped (the spot that will be clicked on the TV).
- **Connection status** always visible in a slim header: "Live" (WebSocket), "Updating every 3 s" (polling fallback),
  "Reconnecting…", with the Kleros wordmark/colour and the access-code logout.
- **Clear empty/error states**: "kleros2 is not connected" card instead of a blank; the login screen explains where the
  code comes from.
- Accessible: real `<button>`s with labels, sufficient contrast on the dark background, focus styles, `aria-live` for
  the toast and status pill, respects `prefers-reduced-motion`.
- Still one self-contained HTML file with inline CSS/JS, **no external fonts, scripts or images** (it must work on the
  Pi's own page with no internet), system font stack, dark theme (it's used next to bright TVs).

## After the first try (Manmeet, 2 Nov evening)

- **One TV at a time, with a picker.** The header has a button per Pi (status dot: green rotating, amber paused or
  showing a link, red offline); only the picked Pi's card is on the page, and only it is watched (previews). No
  pick yet: the first Pi that is up. The phone remembers a tap (`localStorage` `kioskPi`); the team link is
  unchanged. Switching away clears that card's picture, so coming back never shows an old one.
- **Previews and the card follow links.** A link with `target=_blank` (or `window.open`) opens a new tab in front
  of the rotation tab; kiosk.py only knew its own tabs, so it kept capturing the hidden tab underneath (Chromium
  still hands out its last frame), browsing went to that hidden tab (3 s timeout), and Resume left the new tab up.
  Now `Screen.follow_tabs()` (every loop turn, ~0.5 s, and before each browsing command) takes a page tab the
  kiosk didn't open as the tab on screen until the next switch, Show now or Resume closes it. Each screen's state
  also has `page`: the label of the URL on screen now (None for a video or not known yet), so the state push
  follows navigation; the card says "Showing <page>" and falls back to the site for a Pi without it.
- **Full screen.** A "Full screen" button on the preview fills the phone with the picture (the browser's own full
  screen and a landscape lock where it has them; CSS only on an iPhone). Tap = click, a vertical swipe = scroll
  (the page follows the finger), and Exit / Back / Up / Down beside it (landscape) or below it (portrait). Exit,
  Escape, leaving the browser's full screen or the TV going offline end it.
- **Close tab** (DM 3287). New command `closetab` (no body) closes the tab on screen when a link (`follow_tabs`)
  or Show now opened it, one per tap: the newest link tab first, back to the tab underneath (the one that opened
  it, Show now's, or the site's); Show now's tab goes back to the rotation's site, as Next would, but stays paused.
  The rotation's own tabs are never closed: 409 `this tab is a rotation site's, so it stays open` (a link in the
  same tab: Back). Each screen's state has `closable` (true while there is such a tab and no video on top); the
  page shows "Close tab" with Back / Forward / Reload and in the full-screen keys only then, so a Pi that doesn't
  send `closable` never shows it. Tabs kiosk.py closes are remembered until Chromium stops listing them (it can
  for a few ms after "Target is closing"), so `follow_tabs` never takes one for a new tab.
- **No wrong picture after a switch** (DM 3327). The relay shows a new watcher its stored preview at once only
  if it is at most 2 s old (`REPLAY_MAX_AGE`; older means nobody was watching and the TV may have moved on).
  The page drops a `shot.jpg` answer once the WebSocket took over or a newer request went out, and clears the
  picture while it is hidden. (The flash on the TVs themselves, between two videos, is fixed in kiosk.py's
  video handover: see CLAUDE.md's gotchas.)
- **Time on screen** (DM 3336). New command `length?site=<n>` with body `{"seconds": n}` (5 to 3600; 0 = a
  video to its end; `null` or no body = back to `config.toml`'s), kept per URL in `sites.json` (`"seconds"`,
  written only when there is one, and dropped when the site is removed). `show` takes `"seconds"` too: the
  rotation goes on by itself then, unless Pause or browsing holds it. Out-of-range or non-number seconds: 400;
  0 for a page or a bad `site`: 409. Each screen's state has `lengths` (per site, `{"auto", "set"}`: `auto` from
  `config.toml`, `null` = a video to its end; `set` from a phone) and `temp_ends_at` (Unix time, or null). The
  page shows the pickers only when a Pi sends `lengths`, so an older Pi gets no length it would ignore. The relay
  needs no change (`length` matches its `[a-z]{1,20}` action pattern), but it serves its own copy of the page.

## Tests

- `tests/test_relay.py` (existing, 21 tests) keeps passing unchanged.
- New `tests/test_ws.py`: the hand-rolled server frames against the real `websockets` client (the same library the
  Pis use): handshake, masking, 16/64-bit lengths, ping/pong, close, oversize 1009, wrong Pi key 403 before 101,
  phone auth/4003, commands over both WebSockets, previews pushed and coalesced, fallback (an old-style HTTP-only
  server makes the uplink fall back to `/pi/sync`; a WebSocket drop makes it reconnect), browsing actions against a
  fake DevTools endpoint. Skips cleanly with a message if `websockets` isn't installed.
- `tests/test_page.js` grows WebSocket cases with a stub WebSocket (hub over WS, fallback to polling, 4003) and the
  browsing controls (tap → click with the right fractions).
- Real hardware: the new uplink on kleros1 against the new relay run on the Mac, before Francisco redeploys.
