#!/usr/bin/env bash
# Screenshot what a kiosk Pi is showing right now.
#   ./shot.sh kleros1.local [out.png]
set -euo pipefail
host="${1:?usage: ./shot.sh <host> [out.png]}"
out="${2:-shots/${host%%.*}-$(date +%H%M%S).png}"
key="${KIOSK_KEY:-$HOME/.ssh/kleros_kiosk}"
mkdir -p "$(dirname "$out")"
ssh -i "$key" -o IdentitiesOnly=yes "${KIOSK_USER:-kiosk}@$host" \
  'export XDG_RUNTIME_DIR=/run/user/$(id -u); export WAYLAND_DISPLAY=$(basename "$(ls -d $XDG_RUNTIME_DIR/wayland-? | head -1)"); grim -' > "$out"
echo "$out"
