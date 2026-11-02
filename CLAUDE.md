# Devcon booth kiosks: notes for Claude

Two Raspberry Pi 5s (`kleros1`, `kleros2`, user `kiosk`), one TV each at the booth (Vu 55UT, 4K, fed 1080p), at Manmeet's
Devcon booth. **The event starts 4 Nov 2026.** README.md is the runbook; `docs/server-design.md` and
`docs/websocket-design.md` are the designs. Read them before changing code.

## Rules from the brief (binding)

- Never run dd, format or partition a disk unless you have shown the disk list and Manmeet confirmed the exact device.
- Wi-Fi passwords only go into Raspberry Pi Imager or are typed by Manmeet in `nmtui`. Never in chat, files or scripts.
- Don't reopen the hardware choice or quote prices. Keep it simple; Manmeet pushes back on over-building.
- When you need Manmeet's hands, give one physical step at a time.
- DONE means it was actually run (by you or by Manmeet) and you show the output or say who confirmed it. Anything not
  run stays marked as not tested. Report outcomes faithfully.
- Secrets: `secret.toml` (access code, Pi key, Francisco's lab tokens, team links) and the server's `.env` are never
  committed, printed into files in git, or hard-coded. The repo is **public**.
- Commit or push only when Manmeet asks (standing permission: commit and push finished, tested work on `main`).

## Layout

| Path | What |
|---|---|
| `config.toml` | Sites/videos per Pi; `{ url = ..., seconds = ... }` entries can set their own time |
| `secret.toml` (git-ignored) | access_code, public_urls, server_url, server_lab_token, server_pi_key, server_team_link |
| `pi/kiosk.py` | Runs on the Pi: Chromium tabs via DevTools, mpv for videos, phone API on :8080, uplink to the relay |
| `pi/control.html` | The phone page (served by each Pi and by the relay) |
| `pi/install.sh`, `pi/tailscale-watchdog.sh` | Pi setup (rerunnable) and the tailscaled watchdog |
| `server/app.py` + Docker files + `start.sh`/`stop.sh`/`destroy.sh` | The relay, hosted by Francisco (fcanela) on lab2 behind his proxy edge1 |
| `deploy.sh <host>` | Deploy to a Pi over SSH (refuses if media used by config.toml is missing locally) |
| `videos/`, `pages/` (git-ignored contents) | Media; mirrored to the Pis with --delete by deploy.sh |
| `tests/` | `python3 -m unittest tests.test_relay`, `<venv>/bin/python -m unittest tests.test_ws tests.test_ws_pi tests.test_ws_server` (needs websockets 15.0.1, as on the Pis; skipped without it), `node tests/test_page.js`, `bash tests/test_watchdog.sh` |

## How things connect

- Team control (main): `https://kleros-devcon-screens-manager.labs.accesoprohibido.com/?lab_token=…` (Francisco's relay,
  hub mode, live since 2 Nov 16:25; the token value is `server_team_link` in `secret.toml`: never write it anywhere).
  Pis keep a WebSocket to `wss://kleros-devcon-screens.labs.accesoprohibido.com/pi/ws`; phones use `/ws/phone`.
  Both fall back to HTTP polling by themselves (Pi: `/pi/sync`, ~2 s holds, WebSocket retried every 5 min;
  page: every 3 s, "Updating every 3 s" instead of "Live").
  edge1 checks `X-Lab-Token` (Pis) / `?lab_token=` (phones); idle timeout 60 s; Upgrade/Connection forwarded.
- The relay's image bakes in `pi/control.html`: a page change reaches the team link only after Francisco runs
  `git pull && ./start.sh` on lab2 (the Pis' own pages update with `deploy.sh`).
- Tailscale: management (SSH/deploy) and a backup control link via Funnel. **The public Funnel links were broken on
  2 Nov** (Tailscale's relay reset connections, the private tailnet worked); the relay is the team link.
- To deploy to a Pi from a machine on the tailnet: `./deploy.sh kleros1` (the key is `~/.ssh/kleros_kiosk` by
  default; override with `KIOSK_KEY`).

## Gotchas learned the hard way

- **Testing the public Funnel path from a tailnet machine is misleading**: MagicDNS sends `*.ts.net` to the 100.x
  address. Test with public DNS + `curl --resolve host:443:<public ip>`.
- `pkill -f "<pattern>"` inside `ssh host '…'` matches its own command line and kills the session. Use the
  `[x]yz` trick or a script file.
- macOS ships openrsync (no `--append-verify`) and zsh (no word splitting of `$VAR` holding options).
- `cmd | grep -m1` under `set -o pipefail` exits 141; capture into a variable first (install.sh had this).
- ffprobe can print `hevc,` (empty csv columns); `video_codec()` takes the first field. Pi 5 only decodes HEVC in
  hardware (`--hwdec=drm --vo=dmabuf-wayland`); H.264 up to 1080p plays on the CPU; 4K H.264 doesn't play.
- The Pis have no RTC battery: they boot with the last shutdown time; tailscaled got stuck after that once.
  The watchdog only trusts `tailscale status` (a Pi fetching its own Funnel URL gave false TLS errors).
- A preview capture (grim + ffmpeg) takes ~0.5 s on the Pi; a new TLS connection to edge1 ~0.3 s (RTT 119 ms).
- The kiosk only draws on `HDMI-A-1` = the micro-HDMI port next to the USB-C power port; the other port shows black.
- The 4K HEVC Agentic Court loop flashed on a Mi TV (Manmeet's hotel room test, kleros2) with hardware decode (drm + dmabuf-wayland), so the rotation
  uses the 1080p H.264 loop (~9% CPU); the Pis output 1080p anyway.
- `deploy.sh` mirrors `videos/` and `pages/` with `--delete` and refuses to run if a file `config.toml` uses is
  missing locally, so a clone without media can't wipe the Pis. Pages are `file:///home/kiosk/kiosk/pages/<file>`.

## Status (2 Nov, 23:30 IST) and next steps

- Team link = Francisco's relay (hub mode), the manager URL with `?lab_token=`, access code sent separately. Live
  since 2 Nov 16:25. The WebSocket version (`ca1f2bd`) has been live on lab2 and the Pis since 18:11, measured:
  commands ~280 ms, previews ~1.8/s (polling was ~900 ms and one preview per 3–8 s). Polling stays as the fallback.
- Phone page (`control.html` at `c10fb49`): Pi picker, live preview, full-screen preview, tap = click on the TV,
  scroll, type + Enter, back/forward/reload, Close tab (only for tabs a link or Show now opened), Show now / Add /
  Remove, Prev / Pause / Next (Pause holds the rotation; a playing video keeps repeating), "Live" vs polling.
  lab2 was rebuilt from `c10fb49` (the team link has served that page since 2 Nov 23:36 IST; checked by Claude).
- Rotation (`config.toml`): two booth videos (1080p H.264), the 1080p Agentic Court loop video, Foresight (60 s).
- kleros2: runs `c10fb49` + the 1080p loop config (`3aaf993`). Mouse works (left click, scroll, side back button;
  confirmed by Manmeet).
- kleros1: offline overnight (venue); still runs `ca1f2bd`'s code with the 4K loop config (`f42e195`).
- Watchdog: restarts tailscaled when `tailscale status` says the Pi is offline/not running for 3 checks in a row
  (at most once per 10 min); it no longer fetches the Pi's own Funnel URL.
- Next:
  1. Deploy kleros1 when it is back (`./deploy.sh kleros1`); check it plays the 1080p loop and logs
     `uplink: connected ... over WebSocket`.
  2. Power-cut test: pull a Pi's power; the rotation comes back, the uplink reconnects, and the watchdog fixes a
     stuck tailscaled (`journalctl -t kiosk-tailscale-watchdog`).
  3. The "Pause rotation" / "Resume rotation" relabel is on the local branch `pause-rotation-label` (`6cb27de`, not
     pushed or deployed): it waits for Manmeet's go.
  4. The Agentic Court HTML page is being optimised so it doesn't lag (same content). At 23:47 `pages/` still held
     the original (identical copy in `test-clips/originals/`). Measure the new one before/after on a Pi; the page
     isn't in the rotation now (its loop video is).
- Not tested: power-cut cold boot with the watchdog fixing a stuck tailscaled; a multi-hour run; TV picture
  settings (Manmeet to do). Full list: README section 6.
