# Devcon booth kiosk runbook

Two Raspberry Pi 5s (`kleros1`, `kleros2`), one TV each. Each one boots with nobody
touching it, shows its sites full screen, rotates them on a timer and is controlled
from a phone browser.

| File | What it is |
|---|---|
| `config.toml` | Sites, interval, zoom and screens for each Pi. Add a site with one line. |
| `secret.toml` | The team's access code and team links. **Not in git**: copy `secret.example.toml` and get the code from the booth lead. |
| `deploy.sh <host>` | Sets up or updates a Pi over SSH. Safe to rerun. |
| `check.sh <host>` | Board, RAM, throttling, temperature and kiosk state. |
| `shot.sh <host>` | Screenshot of what the TV shows, saved to `shots/`. |
| `links.sh` | Prints the team link and the access code to send to the team. |
| `pi/` | Files copied to `~/kiosk` on the Pi (`kiosk.py`, `control.html`, `install.sh`). |

Stack: Raspberry Pi OS **Lite** (64-bit, Debian 13 Trixie), autologin on tty1 →
labwc (Wayland) → Chromium `--kiosk`, with one browser per screen. All sites are
preloaded as tabs, and `kiosk.py` switches between them.
There's no desktop, so there are no panels, notifications or update prompts.

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
   the USB-C power port), connect the TV, then plug in power.
2. Wait about 2 minutes, then from the Mac:
   ```
   ./deploy.sh kleros1.local
   ```
   It prints the board model, RAM and OS version, installs the packages and restarts into the kiosk.
   During setup, keep the Mac on the same hotspot as the Pi.
   On iPhone, turn on **Maximize Compatibility** in the hotspot settings (2.4 GHz).
3. Optional: add a second network, such as home Wi-Fi with no sign-in page. Run this yourself, because you type the password:
   ```
   ! ssh -i ~/.ssh/kleros_kiosk -t kiosk@kleros1.local sudo nmtui
   ```
   Choose *Edit a connection* → *Add* → *Wi-Fi*, then enter the network name and password.

To change sites, edit `config.toml` and rerun `./deploy.sh kleros1.local`.
To recover a dead card, reflash it (step 1) and run `deploy.sh` again.

## 3. Team control (from any phone, anywhere)

Each Pi runs Tailscale, and Tailscale Funnel publishes its control page at a public HTTPS address.
The Pis only make outgoing connections, so this works on venue Wi-Fi that blocks devices from
seeing each other, and from mobile data. SSH is **not** public; only the control page is.

Run `./links.sh` and send the team:
- **The link** (`https://kleros1.<tailnet>.ts.net`, with kleros2's as backup; `./links.sh` prints both). Fine to post in the team chat. It's findable anyway, and on its own it only shows a login box.
- **The access code**, separately (a DM, or say it at the booth). Each phone remembers it.
  The code is the lock: 16 random characters, with a 1 s wait after each wrong try. If it leaks, change
  `access_code` in `secret.toml`, deploy both Pis and send everyone the new code.

The page on either link controls **both** Pis (the other one shows "Not reachable" if it's off). Each screen has:
- **a live preview** of the TV, updated every few seconds (web pages and videos)
- **Prev / Pause / Next** buttons, plus a button for each site to jump to it
- **Show now:** puts any link on the TV straight away, paused, without saving it (orange bar; Resume goes back)
- **Add to rotation / ✕:** keeps or removes a site. Phone changes are saved in `~/kiosk/sites.json` on that Pi,
  on top of `config.toml`, and survive reboots and deploys. To undo all of them: `ssh ... rm ~/kiosk/sites.json`, then redeploy.
- Every command is logged in `~/kiosk/kiosk.log`.

Pages that need a wallet or a login (for example Kleros Court's home page) show empty boxes. Use pages that show content without one.

On the same Wi-Fi as the Pi, `http://kleros1.local:8080` (or the IP shown on the TV at boot) also works without Tailscale.
**On the Android phone, use Firefox** for that plain-HTTP address: Chrome failed with `ERR_TOO_MANY_REQUESTS` (31 Oct).

**Tailscale setup** (done for both Pis on 1 Nov; repeat only for a reflashed card): `deploy.sh` installs Tailscale. Then run
`ssh -i ~/.ssh/kleros_kiosk -t kiosk@<host>.local sudo tailscale up --accept-dns=false`, open the printed link and click Connect.
Rerun `deploy.sh`, which turns on the public link. Finally, in the [admin page](https://login.tailscale.com/admin/machines),
use ⋯ → **Disable key expiry** for that Pi. Optional: install Tailscale on the Mac (same account), so `./deploy.sh kleros1`
and `./check.sh kleros1` work from anywhere.

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
      HDMI port through the micro-HDMI adapter and an HDMI cable, and the receiver connects to the TV.
      Test the pair before the event, because it can add lag or only connect at 1080p (the kiosk already outputs 1080p).

## 5. If something goes wrong at the booth

Built-in recovery: if Chromium crashes, it restarts within seconds. After a power cut, the Pi boots back into the rotation in about 30 s.
If `kiosk.py` itself fails, it restarts within 3 s. The sites stay loaded as tabs, so a short network drop doesn't blank the screen.

| What you see | Do this |
|---|---|
| Screen frozen or blank | Open the phone page and tap **Next**. If the phone page doesn't load either, pull the Pi's power for 10 s and plug it back in. It's back in about 30 s. |
| Team link shows "Not reachable" or won't load | That Pi has no internet. Check the venue Wi-Fi (sign-in page? password changed?). Turn on the phone hotspot: the Pis know it and connect to it by themselves. Then pull that Pi's power for 10 s. |
| Team link says "Wrong access code" | The code was changed. Ask for the new one. |
| Lightning bolt on screen or slowdown | `./check.sh kleros1.local`. If `throttled` is not `0x0`: `0x50005` means low voltage, so plug the official 27 W supply straight into a wall socket, not a long power-strip chain. A high temperature (over 80 °C) means the case needs air, so move it away from the back of the TV. |
| TV turned itself off | Turn off the TV's eco or auto-power-off timer (see section 4). |
| Card dead | Reflash it with Imager (section 1) and run `deploy.sh`. This downloads about 300 MB of packages over the hotspot and takes about 15 min. |
| Sites slow down after many hours | Power-cycle before opening each day. |

The Pixel hotspot can switch itself off when no devices are connected. The Pis stay connected, but if the hotspot ever disappears, check *Settings → Hotspot → Turn off hotspot automatically*.

## 6. Done checklist

Mark each item only after it has actually been run, and note who ran it.

| Check | kleros1 | kleros2 |
|---|---|---|
| Cold boot with no keyboard reaches the rotation | User pulled power for 10 s, then plugged it back in; rotation began about 30 s after power-on with no input, no restore bubble (log and screenshot checked by Claude), 31 Oct | User pulled power for 10 s, then plugged it back in; rotation began 29 s after the kernel started with no input, no restore bubble; the earlier Pause was cleared by the reboot (log, uptime and screenshot checked by Claude), 1 Nov |
| Board model and RAM confirmed (`check.sh`) | Pi 5 Model B Rev 1.1, 8 GB (7.9 GiB); checked over SSH by Claude, 31 Oct | Pi 5 Model B Rev 1.1, 8 GB (7.9 GiB); checked over SSH by Claude, 1 Nov |
| Phone control works over the hotspot (old 6-digit code, before Tailscale) | Next and Pause from the user's Android/Firefox at 10.244.232.69 were logged on the Pi; it stayed paused on one site for 35 s (screenshots identical). User tapped, Claude checked, 31 Oct | Next and Pause from the user's Android/Firefox at 10.244.232.69 were logged; `/api/state` showed paused on time.is and it stayed there 36 s past the 30 s interval (screenshots). User tapped, Claude checked, 1 Nov |
| Team link over the internet (Tailscale Funnel), from the user's phone on mobile data | The user opened kleros1's team link on their phone and saw kleros1 plus "kleros2: Not reachable" (kleros2 was off). Both real Pis on one page: **not tested yet** (tomorrow at the venue). 1 Nov | Page, preview and Next from the user's phone via kleros2's team link (Next logged at 21:43:29). Without the code: 403. Key expiry off. User tapped, Claude checked, 1 Nov |
| Show now / Add / Remove, live preview | API: public link answers 403 without the code, 200 with it; preview works on pages and the 4K video. Key expiry off. Claude, 1 Nov | Show now, Add and Remove work (screenshots `kleros2-shownow.png`, `kleros2-added.png`). The user saw the preview and the orange bar on their phone. 1 Nov |
| Boots with no network, recovers when it comes back | not tested | Booted with the hotspot off; once it was back, all 3 sites showed correctly (screenshots `kleros2-net1..3.png`). Claude, 1 Nov |
| Multi-hour run: `throttled=0x0`, sane temperature | **Skipped by user, so not tested.** Only 10 min were logged (3 readings: 0x0 at 50–51 °C), 31 Oct | not tested |
| Real video files play smoothly | **Not tested: no files yet.** A generated 4K HEVC test clip played with 0 dropped frames at 30 and 60 fps (see Video below), Claude, 31 Oct | not tested |
| Real TV shows the full page, nothing cut off | not tested | not tested |
| Wireless kit connects (if used) | not tested | not tested |

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

Tested on kleros1 with the test clips in the rotation: the H.264 clip played, then advanced to the 4K clip by itself.
Next skipped to the first site, and Pause looped the H.264 clip until Resume (log and screenshots checked by Claude, 31 Oct).
The two `test-*` clips are still in kleros1's config. **Remove them before the event.**

| Test clip (generated, 30 s, ~35 Mbit/s) | Output mode | Dropped frames |
|---|---|---|
| 3840×2160 HEVC Main, 60 fps | `--hwdec=drm --vo=dmabuf-wayland` | **0** (load 0.8, 53 °C, `0x0`) |
| 3840×2160 HEVC Main, 30 fps | `--hwdec=drm --vo=dmabuf-wayland` | **0** |
| 3840×2160 HEVC Main, 60 fps | `--hwdec=drm-copy --vo=gpu-next` | 1,329 in 27 s (unusable) |
| 3840×2160 HEVC Main, 30 fps and 60 fps | `--hwdec=auto --vo=gpu-next` (mpv default) | nothing shown: "Mapping hardware decoded surface failed" |
| 1920×1080 H.264 High, 60 fps | `--hwdec=no --vo=gpu-next` (CPU) | **1** in 20 s (load 0.64) |
| 1920×1080 H.264 High, 60 fps | `--hwdec=drm --vo=dmabuf-wayland` | nothing shown: "failed to upload frame" |

The output was 1080p, so the decoder handled full 4K and the picture was scaled down to the 1080p screen.
True 4K output needs a 4K TV and a 4K mode for that screen. A generated test pattern is easier to decode than real footage, so test real files once they exist.
Convert on the Mac: `ffmpeg -i in.mov -c:v libx265 -crf 22 -preset medium -tag:v hvc1 -an out-4k.mp4`

## Working on this together (git)

The repo holds the code, config and this runbook. These stay **out** of git (see `.gitignore`):
- `secret.toml`, the access code and team links. Get it from the booth lead and keep your own copy.
- `videos/`, `test-clips/` and `shots/`. Videos are too big for git: share them another way (e.g. Drive)
  and put them in `videos/` before running `deploy.sh`.

To deploy, a teammate also needs SSH access to the Pis. Don't copy the `kleros_kiosk` key around:
make your own (`ssh-keygen -t ed25519 -f ~/.ssh/kleros_kiosk`) and send the `.pub` file to the booth lead,
who appends it to `~/.ssh/authorized_keys` on both Pis. Both of you can then run `deploy.sh` and `check.sh`.

Pull before you deploy, so you don't overwrite each other's site changes on the Pis.
A server can go in a `server/` folder later. It should use the same HTTP API the phone page uses
(`/api/state`, `/api/screen/<i>/<action>`, with the `X-Code` header), and never Chromium's port 9222.
