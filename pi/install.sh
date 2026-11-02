#!/usr/bin/env bash
# Runs on the Pi (deploy.sh copies it to ~/kiosk and runs it). Safe to rerun.
set -euo pipefail
K="$HOME/kiosk"

echo "== Board"
tr -d '\0' < /proc/device-tree/model; echo
free -h | head -2
. /etc/os-release
echo "OS: $PRETTY_NAME, $(uname -m), kernel $(uname -r)"
[ "$(uname -m)" = aarch64 ] || echo "WARNING: this is not the 64-bit OS"

echo "== Config for $(hostname)"
python3 "$K/kiosk.py" check

echo "== Packages"
pkgs=(labwc chromium kanshi wlr-randr grim mpv ffmpeg python3-websockets)
missing=()
for p in "${pkgs[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if ((${#missing[@]})); then
  echo "installing: ${missing[*]}"
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
else
  echo "all installed"
fi
echo "labwc $(labwc --version 2>/dev/null | head -1 || true)"
chromium --version 2>/dev/null || true

echo "== Tailscale (team control link)"
if ! command -v tailscale >/dev/null; then
  sudo mkdir -p --mode=0755 /usr/share/keyrings
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.noarmor.gpg" \
    | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.tailscale-keyring.list" \
    | sudo tee /etc/apt/sources.list.d/tailscale.list >/dev/null
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y tailscale
fi
port=$(cd "$K" && python3 -c 'import kiosk; print(kiosk.load_config()[2][0].control_port)')
if ! tailscale status >/dev/null 2>&1; then
  echo "not signed in yet: run  sudo tailscale up --accept-dns=false  on this Pi and open the link it prints"
# read the status once: grep stopping early on a pipe would fail the script (pipefail)
elif funnel=$(sudo tailscale funnel status 2>/dev/null) && grep -q "127.0.0.1:$port" <<<"$funnel"; then
  grep -m1 https:// <<<"$funnel"
else
  # --bg keeps the public link up across reboots; first time it may print a link to enable Funnel
  sudo timeout 90 tailscale funnel --bg "$port" || echo "funnel not on yet: open the link above, then rerun deploy.sh"
fi

echo "== Tailscale watchdog (restarts tailscaled when it gets stuck)"
if ! command -v tailscale >/dev/null; then
  echo "skipped: tailscale isn't installed"
else
  wd=/usr/local/sbin/kiosk-tailscale-watchdog
  units=/etc/systemd/system/kiosk-tailscale-watchdog
  changed=0
  if ! cmp -s "$K/tailscale-watchdog.sh" "$wd"; then
    sudo install -o root -g root -m 0755 "$K/tailscale-watchdog.sh" "$wd"
    changed=1
  fi
  want="[Unit]
Description=Restart tailscaled if it is stuck (kiosk)

[Service]
Type=oneshot
ExecStart=$wd
SyslogIdentifier=kiosk-tailscale-watchdog
TimeoutStartSec=5min"
  reload=0
  if [ "$(cat "$units.service" 2>/dev/null)" != "$want" ]; then
    printf '%s\n' "$want" | sudo tee "$units.service" >/dev/null
    reload=1
  fi
  want="[Unit]
Description=Check Tailscale every minute (kiosk)

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=5s

[Install]
WantedBy=timers.target"
  if [ "$(cat "$units.timer" 2>/dev/null)" != "$want" ]; then
    printf '%s\n' "$want" | sudo tee "$units.timer" >/dev/null
    reload=1
  fi
  if ((reload)); then sudo systemctl daemon-reload; fi
  sudo systemctl enable --now kiosk-tailscale-watchdog.timer
  if ((changed || reload)); then echo "written"; else echo "already set"; fi
fi

echo "== Force 1080p on kiosk outputs (works with the TV off or unplugged)"
reboot_needed=0
cmdline=/boot/firmware/cmdline.txt
want=$(cd "$K" && python3 -c 'import kiosk; print(" ".join(f"video={s.output}:1920x1080@60D" for s in kiosk.load_config()[2]))')
new="$(sed -E 's/ ?video=HDMI-A-[0-9]:[^ ]*//g' "$cmdline") $want"
if [ "$(cat "$cmdline")" != "$new" ]; then
  printf '%s\n' "$new" | sudo tee "$cmdline" >/dev/null
  reboot_needed=1
  echo "written: $want (takes effect after reboot)"
else
  echo "already set"
fi

echo "== Console autologin on tty1"
dropin=/etc/systemd/system/getty@tty1.service.d/autologin.conf
want="[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $USER --noclear %I \$TERM"
if [ "$(cat "$dropin" 2>/dev/null)" != "$want" ]; then
  sudo mkdir -p "$(dirname "$dropin")"
  printf '%s\n' "$want" | sudo tee "$dropin" >/dev/null
  sudo systemctl daemon-reload
  echo "written"
else
  echo "already set"
fi

echo "== Start labwc on tty1 login"
start='# >>> kiosk >>>'
end='# <<< kiosk <<<'
touch "$HOME/.profile"
sed -i "/^$start\$/,/^$end\$/d" "$HOME/.profile"
cat >> "$HOME/.profile" <<EOF
$start
if [ -z "\$WAYLAND_DISPLAY" ] && [ "\$(tty)" = /dev/tty1 ]; then
  exec labwc > "\$HOME/kiosk/labwc.log" 2>&1
fi
$end
EOF

echo "== labwc and kanshi config"
python3 "$K/kiosk.py" write-configs

echo "== (Re)starting the kiosk session"
if ((reboot_needed)); then
  echo "rebooting to apply the boot settings"
  sudo systemctl reboot
elif pgrep -u "$USER" -x labwc >/dev/null; then
  # stop the old session and anything it left behind; tty1 autologin starts a fresh one
  pkill -u "$USER" -f '\.config/labwc/autostart' || true
  pkill -u "$USER" -f 'kiosk/kiosk\.py run' || true
  pkill -u "$USER" -f 'user-data-dir=.*/\.cache/kiosk/' || true
  pkill -u "$USER" -x kanshi || true
  pkill -u "$USER" -x mpv || true
  pkill -u "$USER" -x labwc
else
  sudo systemctl restart getty@tty1
fi
echo "done: the screen should show the status page, then start rotating"
