#!/usr/bin/env bash
# Set up or update one kiosk Pi over SSH. Safe to rerun.
#   ./deploy.sh kleros1.local
set -euo pipefail
host="${1:?usage: ./deploy.sh <host>   e.g. kleros1.local}"
user="${KIOSK_USER:-kiosk}"
key="${KIOSK_KEY:-$HOME/.ssh/kleros_kiosk}"
sshopts=(-i "$key" -o IdentitiesOnly=yes)
dir="$(cd "$(dirname "$0")" && pwd)"

[ -f "$dir/secret.toml" ] || { echo "secret.toml is missing: copy secret.example.toml and put the team's code in it"; exit 1; }
ssh "${sshopts[@]}" "$user@$host" 'mkdir -p ~/kiosk/videos'
scp "${sshopts[@]}" -q "$dir/config.toml" "$dir/secret.toml" "$dir"/pi/*.py "$dir"/pi/*.sh "$dir"/pi/*.html "$user@$host:kiosk/"
# videos/ is mirrored to ~/kiosk/videos (files removed here are removed there)
rsync -a --size-only --delete --exclude '.*' -e "ssh ${sshopts[*]}" "$dir/videos/" "$user@$host:kiosk/videos/"
ssh "${sshopts[@]}" -t "$user@$host" 'bash ~/kiosk/install.sh'
