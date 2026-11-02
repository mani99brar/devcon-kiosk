#!/usr/bin/env bash
# Health check for one kiosk Pi: board, RAM, throttling, temperature, kiosk state.
#   ./check.sh kleros1.local
set -euo pipefail
host="${1:?usage: ./check.sh <host>}"
key="${KIOSK_KEY:-$HOME/.ssh/kleros_kiosk}"
sshopts=(-i "$key" -o IdentitiesOnly=yes)
ssh "${sshopts[@]}" "${KIOSK_USER:-kiosk}@$host" '
  tr -d "\0" < /proc/device-tree/model; echo
  free -h | head -2
  echo "throttled: $(vcgencmd get_throttled)   (want throttled=0x0)"
  vcgencmd measure_temp
  uptime
  echo "labwc: $(pgrep -x labwc >/dev/null && echo running || echo NOT running)"
  echo "chromium processes: $(pgrep -c chromium || true)"
  echo "--- last kiosk log lines"; tail -n 8 ~/kiosk/kiosk.log 2>/dev/null || echo "(no log yet)"
'
