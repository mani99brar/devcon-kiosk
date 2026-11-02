#!/usr/bin/env bash
# Print the team control link and the access code, to send to the team separately.
#   ./links.sh
set -euo pipefail
dir="$(cd "$(dirname "$0")" && pwd)"
python3 - "$dir/config.toml" "$dir/secret.toml" <<'PY'
import sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
cfg.update(tomllib.load(open(sys.argv[2], "rb")))
server = cfg.get("server_team_link", "")
urls = [u.rstrip("/") for u in cfg.get("public_urls", [])]
if not server and not urls:
    sys.exit("public_urls in secret.toml is empty: fill it in after the Tailscale setup")
print("Team link (controls both screens; fine to post in the team chat):")
if server:
    print(f"  {server}")
if urls:
    print(f"  {'backup if the server is down: ' if server else ''}{urls[0]}")
for u in urls[1:]:
    print(f"  backup if that Pi is off: {u}")
print("Access code (send separately, e.g. a DM; each phone remembers it):")
print(f"  {cfg['access_code']}")
PY
