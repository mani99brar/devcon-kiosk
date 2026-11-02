# Devcon booth kiosks: notes for Claude

Two Raspberry Pi 5s (`kleros1`, `kleros2`, user `kiosk`), one TV each (Vu 55UT, 4K, fed 1080p), at Manmeet's
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
| `tests/` | `python3 -m unittest tests.test_relay`, `node tests/test_page.js`, `bash tests/test_watchdog.sh` |

## How things connect

- Team control (main): `https://kleros-devcon-screens-manager.labs.accesoprohibido.com/?lab_token=…` (Francisco's relay,
  live since 2 Nov 16:25). Pis poll `https://kleros-devcon-screens.labs.accesoprohibido.com/pi/sync` every ~2 s.
  edge1 checks `X-Lab-Token` (Pis) / `?lab_token=` (phones); idle timeout 60 s; Upgrade/Connection forwarded.
- Tailscale: management (SSH/deploy) and a backup control link via Funnel. **The public Funnel links were broken on
  2 Nov** (Tailscale's relay resets connections); the relay is the team link.
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

## Status (2 Nov evening) and next step

- Both Pis on the venue cable; rotation per `config.toml` (two booth videos, the 4K HEVC Agentic Court loop,
  Foresight 60 s); team relay link works (polling: ~0.9 s per command, preview every 3–8 s, "very hard to use").
- **Next: build `docs/websocket-design.md`** (WebSocket transport with polling fallback, phone browsing, page UI/UX).
  Run the saved workflow: `.claude/workflows/build-websocket-browsing-ui.js` (it uses the test venv at
  `~/.venvs/kiosk-test`, websockets 15.0.1 like the Pis). Then test against real Pis, ask Francisco to
  `git pull && ./start.sh` on lab2, deploy both Pis, and have Manmeet try the page on a phone.
- Not tested yet: power-cut cold boot with the watchdog fixing a stuck tailscaled; multi-hour run; TV settings.
