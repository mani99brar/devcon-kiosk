#!/usr/bin/env bash
# Restarts tailscaled when it is stuck: after a boot with a stale clock it can show this Pi as
# offline and the Funnel link stays dead until a restart. install.sh installs it as
# /usr/local/sbin/kiosk-tailscale-watchdog; kiosk-tailscale-watchdog.timer runs it as root
# every minute. Output goes to the journal:  journalctl -t kiosk-tailscale-watchdog
# Overrides, for tests only: WATCHDOG_STATE_DIR, WATCHDOG_CHECK_URL, WATCHDOG_FAILS, WATCHDOG_MIN_GAP
# No set -e: failing checks are the normal way to find out something is wrong.
set -u

state="${WATCHDOG_STATE_DIR:-/run/kiosk-tailscale-watchdog}"  # /run is cleared at boot
check_url="${WATCHDOG_CHECK_URL:-}"  # when set, checked instead of this Pi's Funnel link
internet=https://controlplane.tailscale.com/

log() { echo "$*"; }
num() { if [[ $1 =~ ^[0-9]+$ ]]; then echo "$1"; else echo "$2"; fi; }

max_fails=$(num "${WATCHDOG_FAILS:-}" 3)
min_gap=$(num "${WATCHDOG_MIN_GAP:-}" 600)

if ! command -v tailscale >/dev/null; then
  log "tailscale isn't installed: nothing to do"
  exit 0
fi

status=$(timeout 20 tailscale status --json 2>/dev/null)
read -r backend online dns <<<"$(python3 -I -c '
import json, sys
try:
    s = json.load(sys.stdin)
    me = s.get("Self") or {}
    print(s.get("BackendState") or "-", me.get("Online") is True, (me.get("DNSName") or "").rstrip(".") or "-")
except Exception:
    print("- False -")
' <<<"$status")"
backend=${backend:--}

case $backend in
  NeedsLogin|NeedsMachineAuth|Stopped|NoState)
    log "tailscale is $backend: leaving it alone"
    exit 0 ;;
esac

mkdir -p "$state" || exit 1
fails=$(num "$(cat "$state/fails" 2>/dev/null)" 0)

# any HTTP answer will do; without one, restarting tailscaled can't help
if ! curl -s -o /dev/null --max-time 10 "$internet"; then
  echo 0 > "$state/fails"
  log "no internet (no answer from $internet): not counting"
  exit 0
fi

reason=
if [ "$backend" = - ]; then
  reason="tailscale status didn't answer"
elif [ "$backend" != Running ]; then
  reason="tailscale is $backend, not Running"
elif [ "$online" != True ]; then
  reason="tailscale shows this Pi as offline"
else
  if [ -z "$check_url" ] && [ "$dns" != - ]; then
    funnel=$(timeout 20 tailscale funnel status 2>/dev/null)
    if [[ $funnel == *https://* ]]; then check_url="https://$dns/status"; fi
  fi
  if [ -n "$check_url" ]; then
    for _ in 1 2; do
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$check_url")
      [ "$code" = 200 ] && break
    done
    case $code in
      200) ;;
      ''|000) reason="no answer from $check_url" ;;
      *) reason="$check_url answered $code" ;;
    esac
  fi
fi

if [ -z "$reason" ]; then
  ok="online${check_url:+, $check_url answered 200}"
  if ((fails)); then log "ok again after $fails failed checks: $ok"; else log "ok: $ok"; fi
  echo 0 > "$state/fails"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$state/fails"
log "unhealthy ($fails in a row): $reason"
if ((fails < max_fails)); then exit 0; fi

# seconds since boot: unlike the wall clock it doesn't jump when NTP fixes a stale clock
now=$(num "$(python3 -I -c 'import time; print(int(time.monotonic()))')" "")
last=$(num "$(cat "$state/last_restart" 2>/dev/null)" "")
if [ -z "$now" ]; then
  log "can't read the time: not restarting"
  exit 1
fi
if [ -n "$last" ] && ((now - last < min_gap)); then
  log "not restarting yet: the last restart was $((now - last)) s ago (at most one per $min_gap s)"
  exit 0
fi
# record the restart first, so even a run killed halfway can't cause a second one too soon
if ! echo "$now" > "$state/last_restart"; then
  log "can't write $state/last_restart: not restarting"
  exit 1
fi
echo 0 > "$state/fails"
log "restarting tailscaled after $fails failed checks in a row"
if systemctl restart tailscaled; then
  log "tailscaled restarted"
else
  log "systemctl restart tailscaled failed"
  exit 1
fi
