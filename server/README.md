# Kiosk relay server

A small relay between our two booth kiosks (Raspberry Pis `kleros1` and `kleros2`) and the team's
phones, so the team can control the booth screens without Tailscale.

- The Pis connect **out** to the server over HTTPS and ask "anything for me?" about every 2 seconds
  (`POST /pi/sync`; the server holds each of these up to 2 s and answers as soon as a command is
  waiting). The server can never reach the Pis.
- The phones open the control page from the server and send their commands to it; the server hands
  each command to the right Pi on its next sync and returns the Pi's answer (or 504 after 15 s).
- No WebSocket: plain HTTPS polling, about one request every 2 s per Pi.
- One Python file, standard library only. Everything is kept in memory and nothing needs keeping:
  after a restart the Pis reconnect within seconds.

## Services and ports

| Service | Container port | Published on the host | What |
|---|---|---|---|
| `server` | 8000 (plain HTTP) | `127.0.0.1:18080` and `127.0.0.1:18081` | The whole app: page, phone API and Pi API |

Both host ports go to the **same** container port, so there is no separate web service and any
split of paths between 18080 and 18081 works.

| Path | Who | Auth (checked by the app) |
|---|---|---|
| `GET /healthz` | health checks | none; answers `200 ok` when ready |
| `GET /` | phones: the control page | none (the page asks for the access code) |
| `GET /api/state`, `/api/pi/<host>/...` | phones: state, previews, commands | header `X-Code` = `ACCESS_CODE` |
| `POST /pi/sync` | the Pis | header `X-Pi-Key` = `PI_KEY` |

The app does not check the lab tokens; edge1 does (below). It never logs a code, key or token.
Wrong codes and keys are answered with 403 after a 1 s delay, and logged with the client address.

## Settings in `.env`

`.env` is git-ignored. Create it from `.env.example`, which lists the same variables:

| Variable | Required | What to put |
|---|---|---|
| `ACCESS_CODE` | yes | The team's access code, at least 16 characters. **Get it from Manmeet.** |
| `PI_KEY` | yes | The Pis' key, at least 32 characters. **Get it from Manmeet.** |
| `PIS` | no | Pi host names shown before they first connect. Keep `kleros1,kleros2`. |
| `APP_UID`, `APP_GID` | yes | The numeric user and group the container runs as. Keep `1000`. |

If `ACCESS_CODE` or `PI_KEY` is missing or too short, the app prints why and exits
(`docker compose logs server` shows the reason).

## edge1 settings

| Host | Route | Token check |
|---|---|---|
| `kleros-devcon-screens.labs.accesoprohibido.com` (the Pis) | every path to port 18080 | header `X-Lab-Token` (the Pi token) on every request |
| `kleros-devcon-screens-manager.labs.accesoprohibido.com` (the phones) | your default split: `/api/` and `/ws/` to 18080, everything else to 18081 (both are the same app) | query parameter `lab_token` (the phone token) on every request |

- Please pass the headers `X-Pi-Key` and `X-Code` through unchanged, and set `X-Forwarded-For`
  (used only for the log lines).
- The phone page adds `lab_token` to every request it makes to its own host, so the team link is
  `https://kleros-devcon-screens-manager.labs.accesoprohibido.com/?lab_token=<phone token>`.
- Upstream read timeout: at least 20 s (a phone command can wait up to 15 s for a Pi's answer).
- Request bodies: a Pi sync is at most 4 MB (normally under 100 KB); a phone command at most 4 KB.
- `/ws/` is not used (no WebSocket).

## First start

From the repo folder:

```
cp .env.example .env
# edit .env: fill ACCESS_CODE and PI_KEY (from Manmeet)
./start.sh
curl -fsS http://127.0.0.1:18080/healthz      # prints: ok
```

`./start.sh` checks that `.env` exists, creates `./data` and runs `docker compose up -d --build`.
The app keeps nothing on disk, so `./data` stays empty.

| Task | Command |
|---|---|
| Update after `git pull` | `./start.sh` |
| Stop (keeps `.env`, `./data` and the image) | `./stop.sh` |
| Remove containers, networks, volumes and images | `./destroy.sh` |
| Status and health | `docker compose ps` |
| Logs | `docker compose logs -f server` |

The container runs as `APP_UID:APP_GID` with a read-only file system, all capabilities dropped and
`no-new-privileges`. Logs go to stdout. The log shows start-up (port and `PIS`), each Pi coming
online or going offline, each relayed command (`kleros1 screen 0: next (from <address>)`) and
wrong codes or keys.

## Run it locally (laptop)

Same commands, no edge1 and no lab token:

```
cp .env.example .env
# put any test values in .env, e.g. from: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
./start.sh
```

Then open http://127.0.0.1:18081 and type the `ACCESS_CODE`. Without Docker:
`ACCESS_CODE=... PI_KEY=... python3 server/app.py` serves the same app on port 8000.
