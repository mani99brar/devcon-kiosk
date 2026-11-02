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
# videos/ and pages/ aren't in git and are mirrored with --delete, so a copy of the repo without
# them would wipe them on the Pi: stop unless every file config.toml uses is here
python3 - "$dir" <<'PY'
import pathlib, sys, tomllib, urllib.parse
d = pathlib.Path(sys.argv[1])
cfg = tomllib.loads((d / "config.toml").read_text())
need = set()
for pi in cfg.get("pi", {}).values():
    for s in pi.get("screen", []):
        for u in s.get("sites", []):
            u = u.get("url", "") if isinstance(u, dict) else u  # { url = ..., seconds = ... } entries
            if u.startswith("video:"):
                need.add(d / "videos" / u.removeprefix("video:"))
            elif u.startswith("file://") and "/kiosk/pages/" in u:
                need.add(d / "pages" / urllib.parse.unquote(urllib.parse.urlsplit(u).path.split("/kiosk/pages/", 1)[1]))
missing = sorted(str(p.relative_to(d)) for p in need if not p.is_file())
if missing:
    sys.exit("not deploying: config.toml uses files that aren't on this computer (get them from the team first):\n  "
             + "\n  ".join(missing))
PY
ssh "${sshopts[@]}" "$user@$host" 'mkdir -p ~/kiosk/videos ~/kiosk/pages'
scp "${sshopts[@]}" -q "$dir/config.toml" "$dir/secret.toml" "$dir"/pi/*.py "$dir"/pi/*.sh "$dir"/pi/*.html "$user@$host:kiosk/"
# videos/ and pages/ are mirrored to ~/kiosk (files removed here are removed there)
mkdir -p "$dir/videos" "$dir/pages"
rsync -a --size-only --delete --exclude '.*' -e "ssh ${sshopts[*]}" "$dir/videos/" "$user@$host:kiosk/videos/"
rsync -a --size-only --delete --exclude '.*' -e "ssh ${sshopts[*]}" "$dir/pages/" "$user@$host:kiosk/pages/"
ssh "${sshopts[@]}" -t "$user@$host" 'bash ~/kiosk/install.sh'
