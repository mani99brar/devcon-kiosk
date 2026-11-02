# Devcon booth kiosk runbook

Two Raspberry Pi 5s (`kleros1`, `kleros2`), one TV each. Each one boots with nobody
touching it, shows its videos and sites full screen, rotates them on a timer and is controlled
from a phone browser, from anywhere, through a relay server.

| File | What it is |
|---|---|
| `config.toml` | Sites, videos, times, zoom and screens for each Pi. Add a site with one line (section 2a). |
| `secret.toml` | The team's access code, the team links and the relay's keys. **Not in git**: copy `secret.example.toml` and get the values from the booth lead. |
| `deploy.sh <host>` | Sets up or updates a Pi over SSH. Safe to rerun. Stops if a video or page that `config.toml` uses isn't on your computer. |
| `check.sh <host>` | Board, RAM, throttling, temperature, Tailscale, watchdog and the last kiosk log lines. |
| `shot.sh <host>` | Screenshot of what the TV shows, saved to `shots/`. |
| `links.sh` | Prints the team link (the relay first, then the Tailscale backups) and the access code, to send separately (section 3). |
| `pi/` | Files copied to `~/kiosk` on the Pi: `kiosk.py` (rotation, phone API, link to the relay), `control.html` (the phone page), `install.sh`, `tailscale-watchdog.sh`. |
| `videos/`, `pages/` | Media, **not in git**: videos (too big for git), and local HTML pages shown like a site. `deploy.sh` mirrors both to the Pi. |
| `server/` | The relay server (`app.py`, `Dockerfile`, and `README.md` for whoever hosts it). Francisco runs it on his host (lab2). |
| `docker-compose.yml`, `.env.example`, `start.sh` / `stop.sh` / `destroy.sh` | For the relay's host only: build and start it (also after `git pull`), stop it, remove it. See `server/README.md`. |
| `docs/` | The designs: `server-design.md` (relay, watchdog) and `websocket-design.md` (WebSocket, phone browsing, the phone page). |
| `tests/` | Tests that run on a laptop, with no Pi or Docker (see "Working on this together"). |

Stack: Raspberry Pi OS **Lite** (64-bit, Debian 13 Trixie), autologin on tty1 →
labwc (Wayland) → Chromium `--kiosk`, with one browser per screen. All sites are
preloaded as tabs, and `kiosk.py` switches between them. Videos play in mpv on top of the browser.
There's no desktop, so there are no panels, notifications or update prompts.

## How it connects

- The Pis only make **outgoing** connections, so nothing at the venue needs opening, and venue Wi-Fi that
  hides devices from each other is fine.
- Each Pi keeps a WebSocket open to the relay server on Francisco's host. Phones open the team link
  (the same relay) and get their own WebSocket. Commands, TV state and previews go over these straight away.
- If a WebSocket can't connect, both sides fall back to HTTP polling by themselves: the Pi syncs about every
  2 s and tries the WebSocket again every 5 min; the phone page refreshes every 3 s and says so at the top.
- Each Pi also runs Tailscale: for SSH and `deploy.sh` from machines on our tailnet, and a backup control
  link (Tailscale Funnel). A watchdog restarts Tailscale if it gets stuck.
- The access code, the Pis' key and the proxy tokens are never in git: they live in `secret.toml` (Pis) and on the
  relay's host (`.env`, and the proxy's own settings). Details: `server/README.md` and `docs/`.

## 1. Flash a card (Raspberry Pi Imager)

1. Put the microSD card in the USB-C reader and plug the reader into the Mac.
2. Open Raspberry Pi Imager.
   - **Device:** Raspberry Pi 5
   - **OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**
   - **Storage:** the card reader. Check that the size is about 64 GB.
3. Customisation:
   - **Hostname:** `kleros1` (and `kleros2` for the second card)
   - **Username:** `kiosk`. Pick any password.
   - **Wi-Fi:** your **phone hotspot**, plus the Wi-Fi country. Type the password into Imager only.
     Don't use hotel or venue Wi-Fi that has a sign-in page: a Pi with no keyboard can't get past it.
   - **Locale / timezone:** your own
   - **SSH:** enabled, public key only. Paste the contents of `~/.ssh/kleros_kiosk.pub`, the key made for these Pis (same key on both cards).
4. Write the card. Then unplug and replug the reader, and check `/Volumes/bootfs/user-data`: Imager broke the SSH key
   on both cards (two spaces in the middle, `E2VMTO  TLsLgx`). Fix that with `perl -pi -e 's/E2VMTO  TLsLgx/E2VMTOTLsLgx/'`, and set
   `sudo: "ALL=(ALL) NOPASSWD:ALL"` in place of `sudo: null`. Then eject.

## 2. First boot and deploy

1. Put the card in the Pi, plug the micro-HDMI adapter into **HDMI0** (the port next to
   the USB-C power port), connect the TV, then plug in power. The kiosk only draws on this port
   (`HDMI-A-1` in `config.toml`); the other port stays black.
2. Wait about 2 minutes, then from the Mac:
   ```
   ./deploy.sh kleros1.local
   ```
   It prints the board model, RAM, OS version and the config summary, installs the packages and restarts into the kiosk.
   It needs `secret.toml` and every video and page that `config.toml` uses (section 2a).
   During setup, keep the Mac on the same hotspot as the Pi.
   On iPhone, turn on **Maximize Compatibility** in the hotspot settings (2.4 GHz).
   Once the Pi and your Mac are on Tailscale (section 3), `./deploy.sh kleros1` works from anywhere.
3. Optional: add a second network, such as home Wi-Fi with no sign-in page. Run this yourself, because you type the password:
   ```
   ! ssh -i ~/.ssh/kleros_kiosk -t kiosk@kleros1.local sudo nmtui
   ```
   Choose *Edit a connection* → *Add* → *Wi-Fi*, then enter the network name and password.

To recover a dead card, reflash it (step 1) and run `deploy.sh` again.

## 2a. What the TVs show (`config.toml`)

Each Pi has a `[[pi.<hostname>.screen]]` block with its `output` and its `sites`, shown in order. A site is one of:

| Entry | What it shows |
|---|---|
| `"https://devcon-foresight.netlify.app/"` | A web page, for the screen's `interval` (60 s on both TVs now). |
| `{ url = "https://...", seconds = 241 }` | A web page with its own time on screen. |
| `"video:<file>"` | `videos/<file>`, played once to the end; then the rotation moves on. |
| `"file:///home/kiosk/kiosk/pages/<file>.html"` | A local HTML page from `pages/`, shown like a web page (no internet needed). |

To change what's shown, edit `config.toml` and rerun `./deploy.sh <host>` for that Pi.
`deploy.sh` mirrors `videos/` and `pages/` to the Pi (a file removed here is removed there). They aren't in git,
so it **refuses to deploy** if a video or page that `config.toml` uses is missing on your computer: a fresh
clone without the media can't wipe the Pis. Get the files from the team (e.g. Drive) first.

Rotation in `config.toml` now (the two TVs start at different points, so they don't show the same thing;
kleros1 plays the 4K loop until it is redeployed, see section 6):

| Pi | Order |
|---|---|
| `kleros1` | Booth video 1 (Enterprise, Agent Court, Escrow) → Agentic Court loop video → Booth video 2 (Curate, Scout, Oracle) → Foresight (60 s) |
| `kleros2` | Booth video 2 → Foresight (60 s) → Booth video 1 → Agentic Court loop video |

All three videos are 1080p H.264 (see Video below for why not 4K). `pages/` holds the Agentic Court HTML page;
it isn't in the rotation now (its loop video is).

## 3. Team control (from any phone, anywhere)

Run `./links.sh`. It prints:
```
Team link (controls both screens; fine to post in the team chat):
  https://kleros-devcon-screens-manager.labs.accesoprohibido.com/?lab_token=<phone token>
  backup if the server is down: https://kleros1.<tailnet>.ts.net
  backup if that Pi is off: https://kleros2.<tailnet>.ts.net
Access code (send separately, e.g. a DM; each phone remembers it):
  <the code>
```

Send the team:
- **The team link**: the first line, the relay server. Post it in the team chat as it is: the `?lab_token=` part
  gets phones past Francisco's proxy, and without the access code the page only shows a login box.
- **The access code**, separately (a DM, or say it at the booth). Each phone remembers it.
  The code is the lock: 16 random characters, with a 1 s wait after each wrong try. If it leaks, change
  `access_code` in `secret.toml`, deploy both Pis, have Francisco put the same value in the relay's `.env`
  (`ACCESS_CODE`) and rerun `./start.sh`, then send everyone the new code.
- The two Tailscale links are a **backup** only. Their public path was broken on 2 Nov (section 5).

The page controls both TVs. Pick the Pi at the top (the phone remembers the pick); its dot is green when rotating,
amber when paused or showing a link, and red when not connected. Top right, **Live** means the WebSocket is up
(a command reaches the TV in about 0.3 s, about 2 previews a second); **Updating every 3 s** means it is on the
slower polling fallback, which still works.

| Control | What it does |
|---|---|
| Preview | What the TV shows now. **Tap it** to click at that spot on the TV (a ring shows where). |
| Full screen | The preview fills the phone (turn it sideways for a bigger picture). Tap = click, swipe = scroll; Exit, Back, Up, Down (and Close tab) beside or below it. |
| Prev / Pause / Next | Previous or next item. **Pause** holds the rotation on what is showing (a playing video keeps repeating) until **Resume** or a tap on the orange bar. |
| Back / Forward / Reload, Scroll up / down | On the page on the TV. |
| Text box, Type, Enter | Tap a field in the preview first, then Type puts the text in it; Enter presses Enter. |
| Close tab | Shown only while the TV is on a tab that a link (or Show now) opened: closes it and goes back to the tab underneath. The rotation's own tabs never close (use Back). |
| Show now | Puts the link on the TV at once, paused, without saving it (orange bar; tap it to go back to the rotation). |
| Add to rotation | Adds the link to that TV's rotation and shows it. |
| Site list | Tap a site to jump to it; **✕** removes it from the rotation (asks first). |

- Clicking, scrolling, typing and Back / Forward / Reload pause the rotation, so it doesn't switch away while someone browses. Resume
  (or the next switch) puts a tab that was browsed off its site back on it. They don't work while a video plays.
- Added and removed sites are saved in `~/kiosk/sites.json` on that Pi, on top of `config.toml`, and survive
  reboots and deploys. To undo all of them: `ssh ... rm ~/kiosk/sites.json`, then redeploy.
- Every command is logged in `~/kiosk/kiosk.log` on the Pi, and in the relay's log.

Pages that need a wallet or a login (for example Kleros Court's home page) show empty boxes. Use pages that show content without one.

On the same Wi-Fi as the Pi, `http://kleros1.local:8080` (or the IP shown on the TV at boot) also works without the relay
(it polls every 3 s, like the Tailscale links).
**On the Android phone, use Firefox** for that plain-HTTP address: Chrome failed with `ERR_TOO_MANY_REQUESTS` (31 Oct).

**Tailscale setup** (done for both Pis on 1 Nov; repeat only for a reflashed card): `deploy.sh` installs Tailscale. Then run
`ssh -i ~/.ssh/kleros_kiosk -t kiosk@<host>.local sudo tailscale up --accept-dns=false`, open the printed link and click Connect.
Rerun `deploy.sh`, which turns on the Funnel link. Finally, in the [admin page](https://login.tailscale.com/admin/machines),
use ⋯ → **Disable key expiry** for that Pi. Optional: install Tailscale on the Mac (same account), so `./deploy.sh kleros1`
and `./check.sh kleros1` work from anywhere.

**Relay settings** (already done): `server_url` in `secret.toml` turns on the Pi's link to the relay, with
`server_pi_key` and `server_lab_token` (`server_team_link` is only the link `./links.sh` prints);
`deploy.sh` prints `server: ... (Pi key set, lab token set)`, and the
kiosk log (last lines in `./check.sh`) says `uplink: connected to ... over WebSocket`.
The relay's side is in `server/README.md`.

## 3a. Venue Wi-Fi

The Pis need a network with internet, a plain password and **no sign-in page**. A wired network cable needs no setup.
Add the venue Wi-Fi while the Pi is on the hotspot (or over Tailscale). You type the password:
```
! ssh -i ~/.ssh/kleros_kiosk -t kiosk@kleros1.local sudo nmtui
```
*Edit a connection* → *Add* → *Wi-Fi*, then enter the network name and password. The hotspot stays saved as a backup.

## 4. At the TV (do this at the booth)

- [ ] Set picture mode to **PC** or **Game**.
- [ ] Turn overscan **off** (*Just Scan*, *Screen Fit* or *Full Pixel*). Check that the page edges aren't cut off.
- [ ] Turn off the TV's **auto power off / eco timer**. Many TVs switch off after a few hours with no remote input.
- [ ] Wireless HDMI kit (if one booth's TV is far away): the transmitter connects to the Pi's
      HDMI0 port through the micro-HDMI adapter and an HDMI cable, and the receiver connects to the TV.
      Test the pair before the event, because it can add lag or only connect at 1080p (the kiosk already outputs 1080p).

A mouse plugged into the Pi works at the booth: left click, scroll and the side back button (confirmed on kleros2).

## 5. If something goes wrong at the booth

Built-in recovery: if Chromium crashes, it restarts within seconds. After a power cut, the Pi boots back into the rotation in about 30 s,
and keeps retrying the relay until it answers. If `kiosk.py` itself fails, it restarts within 3 s. The sites stay loaded as tabs,
so a short network drop doesn't blank the screen. If `tailscale status` says the Pi is offline or not running (or doesn't answer) for 3 checks in a row
(one a minute, only while the internet works), the watchdog restarts Tailscale, at most once every 10 min (`check.sh` shows its last lines).

| What you see | Do this |
|---|---|
| TV stays black, but the Pi has power | The micro-HDMI is in the wrong port. Use **HDMI0**, the port next to the USB-C power port; the other port shows black. |
| Screen frozen or blank | Open the phone page and tap **Next**. If the phone page doesn't load either, pull the Pi's power for 10 s and plug it back in. It's back in about 30 s. |
| A Pi's button is red, or its card says "… is not connected" | That Pi isn't reaching the relay: power or internet. Check the venue Wi-Fi or cable (sign-in page? password changed?). Turn on the phone hotspot: the Pis know it and connect to it by themselves. Then pull that Pi's power for 10 s. The card comes back by itself once the Pi is online. |
| Team link says "the link was rejected, open the team link again" | Francisco's proxy refused the link: the `?lab_token=` part is missing or old. Open the full team link from `./links.sh` again. This is not the access code. |
| Team link says "Wrong access code" (the login box comes back) | The code was mistyped or changed. Ask for the current one. |
| Team link doesn't load at all, for anyone | The relay is down. Ask Francisco to check it (`docker compose ps`, `docker compose logs -f server`). Meanwhile, try a Tailscale backup link from `./links.sh`. |
| Tailscale backup link: "connection was reset" | On 2 Nov this was Tailscale's public relay, not the Pis: the private tailnet worked. Test the public path as below. |
| Team link: top of the page says "Updating every 3 s" (normal on the Tailscale and `.local` links) | The WebSocket isn't getting through (e.g. a network that blocks it). The page still works, just slower; it switches back to Live by itself. |
| Lightning bolt on screen or slowdown | `./check.sh kleros1.local`. If `throttled` is not `0x0`: `0x50005` means low voltage, so plug the official 27 W supply straight into a wall socket, not a long power-strip chain. A high temperature (over 80 °C) means the case needs air, so move it away from the back of the TV. |
| TV turned itself off | Turn off the TV's eco or auto-power-off timer (see section 4). |
| Card dead | Reflash it with Imager (section 1) and run `deploy.sh`. This downloads about 300 MB of packages over the hotspot and takes about 15 min. |
| Sites slow down after many hours | Power-cycle before opening each day. |

**Testing the public Funnel path.** A computer on our tailnet can't test it in a browser: Tailscale's MagicDNS answers
`*.ts.net` with the private address, so it looks fine even when the public link is dead. Ask public DNS for the address
and connect to that:
```
h=kleros1.<tailnet>.ts.net
ip=$(dig +short "$h" @dns.google | tail -1)
curl -sS -o /dev/null -w '%{http_code}\n' --max-time 15 --resolve "$h:443:$ip" "https://$h/"
```
`200` means the public link works. Or try it from a phone on mobile data, with Tailscale off.

The Pixel hotspot can switch itself off when no devices are connected. The Pis stay connected, but if the hotspot ever disappears, check *Settings → Hotspot → Turn off hotspot automatically*.

## 6. Done checklist

Mark each item only after it has actually been run, and note who ran it. Anything else stays **not tested**.

| Check | kleros1 | kleros2 |
|---|---|---|
| Cold boot with no keyboard reaches the rotation | Manmeet pulled power for 10 s, then plugged it back in; rotation began about 30 s after power-on with no input, no restore bubble (log and screenshot checked by Claude), 31 Oct | Manmeet pulled power for 10 s, then plugged it back in; rotation began 29 s after the kernel started with no input, no restore bubble; the earlier Pause was cleared by the reboot (log, uptime and screenshot checked by Claude), 1 Nov |
| Power cut with today's code: rotation, relay reconnect, watchdog fixing a stuck Tailscale | not tested | not tested |
| Board model and RAM confirmed (`check.sh`) | Pi 5 Model B Rev 1.1, 8 GB (7.9 GiB); checked over SSH by Claude, 31 Oct | Pi 5 Model B Rev 1.1, 8 GB (7.9 GiB); checked over SSH by Claude, 1 Nov |
| Phone control over the hotspot (old 6-digit code, before Tailscale) | Next and Pause from Manmeet's Android/Firefox were logged on the Pi; it stayed paused on one site for 35 s (screenshots identical). Manmeet tapped, Claude checked, 31 Oct | Next and Pause from Manmeet's Android/Firefox were logged; `/api/state` showed paused on time.is and it stayed there 36 s past the 30 s interval (screenshots). Manmeet tapped, Claude checked, 1 Nov |
| Team link through the relay (HTTP polling): page 200, wrong code 403, pause/resume round trip under 1 s; Claude, 2 Nov 16:25 | Connected to the relay by itself at 16:25; online, preview through the relay 200. Claude, 2 Nov | Connected to the relay by itself at 16:25; online, preview through the relay 200. Claude, 2 Nov |
| Relay over WebSocket (`ca1f2bd`): commands ~280 ms, previews ~1.8 a second (polling: ~900 ms, one preview every 3–8 s); measured by Claude through the relay, 2 Nov 18:11 | Connected over the WebSocket. Claude, 2 Nov 18:11 | Connected over the WebSocket. Claude, 2 Nov 18:11 |
| Pi picker, full screen, preview following new tabs, Close tab (`3bdd500`, `c10fb49`) | Not deployed yet (runs `ca1f2bd`) | Deployed (`c10fb49`). Using them on this Pi: not tested |
| Mouse at the booth: left click, scroll, side back button | not tested | Works, confirmed by Manmeet, 2 Nov |
| Team link over Tailscale Funnel (now the backup) | 1 Nov: Manmeet opened it on a phone and saw kleros1 plus "kleros2: Not reachable" (kleros2 was off). **2 Nov: broken** from Manmeet's phone on venue Wi-Fi ("connection was reset") and in Claude's public-path test, even after restarting tailscaled | 1 Nov: page, preview and Next from Manmeet's phone (Next logged at 21:43:29); without the code: 403. Manmeet tapped, Claude checked. **2 Nov: broken**, same as kleros1 |
| Show now / Add / Remove, live preview | API: the link answers 403 without the code, 200 with it; preview works on pages and a 4K video. Claude, 1 Nov | Show now, Add and Remove work (screenshots `kleros2-shownow.png`, `kleros2-added.png`). Manmeet saw the preview and the orange bar on the phone. 1 Nov |
| Boots with no network, recovers when it comes back | not tested | Booted with the hotspot off; once it was back, all 3 sites showed correctly (screenshots `kleros2-net1..3.png`). Claude, 1 Nov |
| Real videos and Foresight in the rotation | Booth videos and Foresight on screen (screenshots by Claude: `kleros1-booth-video.png`, `kleros1-foresight.png`), 2 Nov. Still has the 4K loop in its config: deploy when it's back | Booth videos and Foresight on screen (screenshots by Claude: `kleros2-booth-video.png`, `kleros2-foresight.png`), 2 Nov. Runs the 1080p loop (`3aaf993`) |
| Smooth playback, watched for a full rotation at the booth | not tested | not tested |
| Multi-hour run: `throttled=0x0`, sane temperature | **Skipped by Manmeet, so not tested.** Only 10 min were logged (3 readings: 0x0 at 50–51 °C), 31 Oct | not tested |
| Real TV shows the full page, nothing cut off (TV picture settings, section 4) | not tested (Manmeet to do) | not tested (Manmeet to do) |
| Wireless kit connects (if used) | not tested | not tested |

The 4K HEVC Agentic Court loop flashed on a Mi TV (hotel room test on kleros2, hardware decode), so the rotation went back to the 1080p loop on 2 Nov.
kleros1 is offline at the venue overnight, so it still runs `ca1f2bd` with the 4K loop: **deploy it when it's back.**
The team link serves the relay's own copy of the phone page: lab2 was rebuilt from `c10fb49` and has served that
page (picker, full screen, Close tab) since 2 Nov 23:36 IST (checked by Claude).

Laptop tests, run by Claude on 2 Nov (23:34, again at 23:47): 117 Python tests (relay, WebSocket, Pi side), 28 phone page cases and
15 watchdog cases, all passed. They use fakes for Chromium, mpv, Tailscale and the network, so they don't replace the checks above.

## Later (after the core works)

1. Offline copies of each site, and skipping a site that fails to load instead of showing an error page.
2. Attract mode: slow auto-scroll or a scripted click-through for each site, or a looping 1080p recording.
3. A QR code in a corner of each site.

## Video (tested on kleros1, 31 Oct 2026)

**Adding a video:** put the file in `videos/` on the Mac, add `"video:<file>"` to a screen's `sites` in
`config.toml`, and run `deploy.sh`. It mirrors `videos/` to the Pi (removing a file here removes it there),
and the video plays once in its turn before the rotation moves on. Next, Prev and jump work on videos.
Pause keeps the current video looping. Sound is off unless `video_audio = true`.
The kiosk picks the decoder from the file: HEVC uses `--hwdec=drm --vo=dmabuf-wayland` (hardware),
and anything else is decoded on the CPU (`--vo=gpu-next`), which is fine for H.264 up to 1080p.
4K H.264 is unusable: the Pi 5 has no hardware decoder for it (the booth videos' 4K masters are H.264, so the 1080p versions are used).
`ffprobe` prints `hevc,` for some files; the kiosk handles that.

**Use 1080p H.264 at the booth.** The Pis output 1080p anyway, and the 4K HEVC Agentic Court loop flashed on a Mi TV
with the hardware decoder (2 Nov). The 1080p H.264 loop uses about 9% CPU.

Tested on kleros1 with the test clips in the rotation: the H.264 clip played, then advanced to the 4K clip by itself.
Next skipped to the first site, and Pause looped the H.264 clip until Resume (log and screenshots checked by Claude, 31 Oct).
The test clips are no longer in `config.toml`.

| Test clip (generated, 30 s, ~35 Mbit/s) | Output mode | Dropped frames |
|---|---|---|
| 3840×2160 HEVC Main, 60 fps | `--hwdec=drm --vo=dmabuf-wayland` | **0** (load 0.8, 53 °C, `0x0`) |
| 3840×2160 HEVC Main, 30 fps | `--hwdec=drm --vo=dmabuf-wayland` | **0** |
| 3840×2160 HEVC Main, 60 fps | `--hwdec=drm-copy --vo=gpu-next` | 1,329 in 27 s (unusable) |
| 3840×2160 HEVC Main, 30 fps and 60 fps | `--hwdec=auto --vo=gpu-next` (mpv default) | nothing shown: "Mapping hardware decoded surface failed" |
| 1920×1080 H.264 High, 60 fps | `--hwdec=no --vo=gpu-next` (CPU) | **1** in 20 s (load 0.64) |
| 1920×1080 H.264 High, 60 fps | `--hwdec=drm --vo=dmabuf-wayland` | nothing shown: "failed to upload frame" |

The output was 1080p, so the decoder handled full 4K and the picture was scaled down to the 1080p screen.
True 4K output needs a 4K TV and a 4K mode for that screen. A generated test pattern is easier to decode than real footage.
Convert on the Mac: `ffmpeg -i in.mov -c:v libx265 -crf 22 -preset medium -tag:v hvc1 -an out-4k.mp4`

## Working on this together (git)

The repo is **public**. It holds the code, config and this runbook. These stay **out** of git (see `.gitignore`):
- `secret.toml` (the access code, team links and relay keys) and the relay's `.env`. Get `secret.toml` from the booth lead and keep your own copy.
- `videos/`, `pages/`, `test-clips/` and `shots/`. Media is too big for git: share it another way (e.g. Drive)
  and put it in `videos/` and `pages/` before running `deploy.sh`.

To deploy, a teammate also needs SSH access to the Pis. Don't copy the `kleros_kiosk` key around:
make your own (`ssh-keygen -t ed25519 -f ~/.ssh/kleros_kiosk`) and send the `.pub` file to the booth lead,
who appends it to `~/.ssh/authorized_keys` on both Pis. Both of you can then run `deploy.sh` and `check.sh`.

Pull before you deploy, so you don't overwrite each other's site changes on the Pis.

The relay in `server/` runs on Francisco's host. Its image includes a copy of `pi/control.html`, so a change to the
phone page reaches the team link only after Francisco runs `git pull && ./start.sh` there (the Pis get it with `deploy.sh`).

Tests (from the repo root, no Pi, Docker or browser needed):
```
python3 -m unittest tests.test_relay -v        # the relay over HTTP, with fake Pis (~15 s)
python3 -m unittest tests.test_ws tests.test_ws_pi tests.test_ws_server -v   # WebSocket side (~1 min)
node tests/test_page.js                        # the phone page, with a stub DOM
bash tests/test_watchdog.sh                    # the Tailscale watchdog, with stub commands
```
The WebSocket tests need the `websockets` package (`pip install websockets==15.0.1` in a venv, the version on the Pis);
without it they are skipped.
