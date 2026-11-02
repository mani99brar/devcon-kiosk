# Relay server and Tailscale watchdog: design

Status: approved by Manmeet on 2 Nov 2026 (venue day). Event starts 4 Nov.

## Why

The team control links run on Tailscale Funnel. On 2 Nov they were dead for about 20 minutes:
Tailscale got stuck after the Pis booted with yesterday's clock (no RTC battery; `fake-hwclock`
restores the last shutdown time), and Funnel itself misbehaved. Two fixes:

1. **Relay server** on Francisco's host, so the team has a link that doesn't depend on Tailscale.
   The Pis connect *out* to it over plain HTTPS (it can never reach them). Funnel stays as backup.
2. **Tailscale watchdog** on each Pi, so a stuck Tailscale restarts itself after a power cut.

Hosting rules are Francisco's brief (not in the repo; the summary below is binding):
TLS and a token check happen on his proxy edge1; our app speaks plain HTTP; ports are published
on `127.0.0.1` only; no host changes; everything inside the repo folder; `start.sh`, `stop.sh`,
`destroy.sh`; `GET /healthz`; `.env.example`; tokens never committed or hard-coded.

## Names

| Who | URL | edge1 checks |
|---|---|---|
| Pis | `https://kleros-devcon-screens.labs.accesoprohibido.com` | header `X-Lab-Token` (Pi token) on every request |
| Phones | `https://kleros-devcon-screens-manager.labs.accesoprohibido.com` | query parameter `lab_token` (phone token) on every request |

edge1 routes the Pi host's every path to `127.0.0.1:18080`, and the phone host's `/api/` and
`/ws/` to 18080 and everything else to 18081. **We publish the same container port on both**, so
any split works and there is no separate web service.

## Pieces

| File | What |
|---|---|
| `server/app.py` | The relay. Python 3 standard library only, one file. |
| `server/Dockerfile` | `python:3.13-slim`, copies `server/app.py` and `pi/control.html`, numeric non-root `USER`. |
| `docker-compose.yml`, `.dockerignore`, `.env.example` | At the repo root, so Francisco runs everything from there. |
| `start.sh`, `stop.sh`, `destroy.sh` | At the repo root. bash + git + docker compose only, no sudo, no `$HOME`/`$UID`. |
| `server/README.md` | For Francisco: services, every variable, what to fill, how to run locally. |
| `pi/kiosk.py` | New uplink thread; POST handling refactored into one shared function. |
| `pi/control.html` | New "hub" mode when served by the relay; Pi mode unchanged. |
| `pi/tailscale-watchdog.sh` + section in `pi/install.sh` | The watchdog and its systemd timer. |
| `tests/` | stdlib `unittest` for the relay end to end; a bash test for the watchdog with stubs. |

## Secrets

- Pi side, in `secret.toml` (git-ignored, copied by `deploy.sh`):
  ```toml
  server_url = "https://kleros-devcon-screens.labs.accesoprohibido.com"  # leave out to turn the uplink off
  server_lab_token = "..."   # edge1's Pi token, sent as X-Lab-Token
  server_pi_key = "..."      # our own key, sent as X-Pi-Key; same value as PI_KEY in the server's .env
  server_team_link = "https://kleros-devcon-screens-manager.labs.accesoprohibido.com/?lab_token=..."  # printed by links.sh
  ```
- Server side, in `.env` (git-ignored; Francisco fills it from `.env.example`):
  `ACCESS_CODE` (the team code, same as `access_code` in `secret.toml`, at least 16 characters),
  `PI_KEY` (at least 32 characters), `PIS` (optional, comma-separated host names shown even before
  they connect, e.g. `kleros1,kleros2`), `APP_UID=1000`, `APP_GID=1000`.
- The app never logs a token, key or code. The page never sends `lab_token` to any URL that isn't
  relative to its own origin.

## Relay API (`server/app.py`)

Container port 8000. `ThreadingHTTPServer`, daemon threads, HTTP/1.0 responses. Every response
has `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.
No CORS headers (the hub page is same-origin). Unknown paths: 404 `{"error": "not found"}`.

Unauthenticated:
- `GET /healthz` → 200 `ok` (text/plain).
- `GET /` → `control.html` (text/html).

Pi side, header `X-Pi-Key` must equal `PI_KEY` (`hmac.compare_digest`; wrong or missing → sleep
1 s, 403 `{"error": "wrong Pi key"}`):
- `POST /pi/sync`, JSON body at most 4 MB:
  ```json
  {"host": "kleros1",
   "state": { ...exactly what the Pi's own GET /api/state returns... },
   "results": [{"id": "ab12...", "status": 200, "body": { ... }}],
   "shots": {"0": "<base64 JPEG>"}}
  ```
  `host` must match `^[a-z0-9-]{1,63}$` (else 400). The server records `last_seen`, stores the
  state (with `peers` replaced by `[]`), stores each shot (decoded, at most 1 MB, else ignored),
  and completes waiting phone requests by `id`. Then it waits up to `SYNC_HOLD` = 2 s for phone
  requests for this host, returning as soon as one is queued:
  ```json
  {"requests": [{"id": "ab12...", "method": "POST", "path": "/api/screen/0/next", "query": "site=2", "body": {"url": "..."}}],
   "want_shots": [0]}
  ```
  `want_shots` lists screens whose preview a phone asked for in the last 10 s.
  A request is handed out once (moved from queued to delivered).
- A host is **online** if it synced in the last `OFFLINE_AFTER` = 10 s.

Phone side, header `X-Code` must equal `ACCESS_CODE` (`hmac.compare_digest`; wrong or missing →
sleep 1 s, 403 `{"error": "wrong access code"}`):
- `GET /api/state` →
  `{"hub": true, "host": "server", "peers": [], "pis": [{"host": "kleros1", "base": "/api/pi/kleros1", "online": true}, ...]}`
  sorted by host: the `PIS` names plus every host that has synced since the server started.
- `GET /api/pi/<host>/api/state` → the stored state (200) if online, else 503
  `{"error": "<host> is not connected"}`.
- `GET /api/pi/<host>/api/screen/<i>/shot.jpg` → records that screen `i` of `<host>` is wanted;
  the stored JPEG (200, `image/jpeg`) if it is at most 15 s old, else 503 `{"error": "no picture yet"}`.
- `POST /api/pi/<host>/api/screen/<i>/<action>[?site=n]`, body at most 4096 bytes (JSON or empty;
  bad JSON → `null`): `i` digits, `action` `^[a-z]{1,20}$`, host known (else 404). Offline → 503
  `{"error": "<host> is not connected"}`. Otherwise queue `{id: secrets.token_hex(8), method: "POST",
  path, query (raw), body}` (at most 20 queued per host, else 503 `{"error": "<host> is busy"}`),
  wake that host's held sync, and wait up to `REPLY_TIMEOUT` = 15 s. On a result, reply with its
  status and body; if the status is 200 and the body has `screens`, also store it as the host's state
  (with `peers` = `[]`). On timeout drop the request if not yet delivered and reply 504
  `{"error": "<host> didn't answer"}`.

State: one dict per host (`state`, `last_seen`, `shots {i: (bytes, time)}`, `wanted {i: time}`,
`queue`, `delivered {id}`, `results {id: (status, body)}`) guarded by one `threading.Condition`.

Logs (stdout, one line each, time-stamped like `kiosk.py`'s `log`): start-up (port, `PIS`),
a host coming online / going offline (checked lazily or by a small reaper thread),
each relayed command (`kleros1 screen 0: next (from <first X-Forwarded-For address or client IP>)`),
wrong code / wrong Pi key (with the client address), and start-up errors. Missing or too-short
`ACCESS_CODE` / `PI_KEY` → print why and exit 1.

Constants (`SYNC_HOLD`, `OFFLINE_AFTER`, `REPLY_TIMEOUT`, ...) are module-level so tests can patch them.

## Pi uplink (`pi/kiosk.py`)

- Refactor: move the body of `do_POST` after the path/auth checks into a module-level
  `run_command(screens, i, action, query, data)` returning `(status, payload)`, used by both the
  HTTP handler and the uplink, with identical behaviour (including the `clean_url` rule, the
  `control: ...` log line, 400/404/409/200 statuses). Move the state dict into
  `pi_state(host, public_urls, screens)`.
- `uplink(cfg, host, screens, public_urls)`: daemon thread started in `main()` (run) only when
  `server_url` is set. Loop:
  1. POST `{server_url}/pi/sync` with `Content-Type: application/json`, `X-Pi-Key`, and
     `X-Lab-Token` when set; urllib; timeout 30 s. Body: host, `pi_state(...)`, pending results,
     shots.
  2. For each request: run it only if `method == "POST"` and `path` matches
     `^/api/screen/(\d+)/([a-z]{1,20})$`, via `run_command` (with `query` parsed for `site`, `body`
     as `data`; log the command with `(via server)`); otherwise result 404. Append
     `{"id", "status", "body"}` to the results sent with the next sync. Results not yet delivered
     survive a failed sync (keep at most 20).
  3. For each valid index in `want_shots`, include `screens[i].snapshot()` base64-encoded in the
     next sync, at most once per 2 s per screen, only if non-empty.
  4. Success → if it was down before, log `uplink: connected to <server_url>`; retry delay back
     to 1 s. Failure (URLError, HTTPError, timeout, bad JSON) → log once per distinct reason
     `uplink: can't reach <server_url> (<reason>), retrying`; sleep the delay; double it up to
     60 s. 403 → log `uplink: the server refused this Pi's key` and use the 60 s delay.
- `kiosk.py check` prints `server: <server_url>` (and whether the Pi key and lab token are set) or
  `server: off`.
- Must not change anything for a Pi without `server_url`.

## Phone page (`pi/control.html`)

- `labToken` from `?lab_token=` in the page URL. Every fetch to a **relative** URL (starts with
  `/`) gets `lab_token=<labToken>` appended (respecting an existing `?`) when `labToken` is set.
  Absolute URLs (the Funnel peers) never get it.
- `#c=` handling keeps the query string: `history.replaceState(null, "", location.pathname + location.search)`.
- After `api("", "/api/state")`: if `own.hub`, render each entry of `own.pis` by fetching
  `api(p.base, "/api/state")` in parallel; a failure renders `p.host` + "Not reachable right now".
  No Funnel peers are fetched in hub mode. Otherwise (served by a Pi) the existing peers flow is
  unchanged.
- Everything else (preview, buttons, inputs kept across refreshes, 403 logout only for base `""`)
  stays as it is.

## `links.sh`

Prints the server team link (`server_team_link`) first when set, then the Funnel links as backup,
then the access code (to send separately).

## Tailscale watchdog

`pi/tailscale-watchdog.sh`, installed by `install.sh` as root-owned
`/usr/local/sbin/kiosk-tailscale-watchdog`, run by `kiosk-tailscale-watchdog.timer`
(`OnBootSec=2min`, `OnUnitActiveSec=1min`) through a oneshot service. `install.sh` writes the units
only when they change, then `daemon-reload` and `enable --now` the timer (safe to rerun).

Each run:
1. `tailscale status --json`. If Tailscale isn't installed, or `BackendState` is `NeedsLogin`,
   `NeedsMachineAuth`, `Stopped` or `NoState`: leave it alone (exit 0, no counting).
2. Internet check: any HTTP answer from `https://controlplane.tailscale.com/` within 10 s. None →
   reset the failure count, exit 0 (restarting can't help).
3. Healthy = `BackendState == Running` and `Self.Online` is true, and, if Funnel is on
   (`tailscale funnel status` lists an `https://` URL), `GET https://<Self.DNSName>/status` returns
   200 within 15 s (two tries).
4. Healthy → reset the count. Unhealthy → count + 1, log the reason. At 3 in a row, and if the last
   restart was at least 10 minutes ago: `systemctl restart tailscaled`, log it, reset the count.

State in `/run/kiosk-tailscale-watchdog/` (cleared at boot). Environment overrides for tests only:
`WATCHDOG_STATE_DIR`, `WATCHDOG_CHECK_URL`, `WATCHDOG_FAILS` (3), `WATCHDOG_MIN_GAP` (600).
Logs go to stdout (the journal). `check.sh` shows the last watchdog lines.

## Tests

- `tests/test_relay.py` (`python3 -m unittest`, runs on the Mac without Docker): starts `server/app.py`
  in a thread on a free port with short timings; a fake Pi built from the real `kiosk.py` (`Screen`
  with `devtools`, `play` and `snapshot` patched, like `fake_pi`) runs the real uplink. Covers:
  healthz, page, wrong code 403, wrong Pi key 403 (and the uplink backs off), host listed and
  online, relayed next/pause/resume/show/add/remove (state changes, 409 passed through), preview
  503 then 200, offline after the fake Pi stops, 504 when a Pi syncs but never answers, `lab_token`
  not required, bad host name 400, unknown host 404.
- `tests/test_watchdog.sh`: stubs for `tailscale`, `curl`, `systemctl` on `PATH`; covers
  not-signed-in, no internet, healthy, three failures → one restart, no second restart within the gap.
- Real hardware (by Claude, then the user): the uplink on kleros1 against the relay running on the Mac
  over Tailscale; the watchdog's restart path on kleros1 by pointing `WATCHDOG_CHECK_URL` at a dead URL;
  a power-pull cold boot. Then the real server once Francisco deploys it.
