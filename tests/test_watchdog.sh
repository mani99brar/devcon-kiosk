#!/usr/bin/env bash
# Runs pi/tailscale-watchdog.sh against stub tailscale, curl, systemctl, timeout and sudo
# commands (nothing real is called). Prints PASS/FAIL per case; exits 1 if any case fails.
#   bash tests/test_watchdog.sh
set -u
script="$(cd "$(dirname "$0")/.." && pwd)/pi/tailscale-watchdog.sh"
T=$(mktemp -d "${TMPDIR:-/tmp}/watchdog-test.XXXXXX")
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/empty"
export STUB_CALLS="$T/calls" STUB_DIR="$T"

# Scenario variables read by the stubs:
#   TS_BACKEND, TS_ONLINE (true/false), TS_FUNNEL (on/off), TS_DOWN=1 (tailscaled not answering)
#   NET (up/down), SELF_CODES (HTTP codes for the self-check, one per call; 000 = no answer)
#   RESTART_RC (exit code of systemctl)
cat > "$T/bin/tailscale" <<'EOF'
#!/usr/bin/env bash
echo "tailscale $*" >> "$STUB_CALLS"
case "$*" in
  "status --json")
    if [ "$TS_DOWN" = 1 ]; then
      echo "failed to connect to local tailscaled; it doesn't appear to be running" >&2
      exit 1
    fi
    cat <<JSON
{
  "Version": "1.90.0",
  "BackendState": "$TS_BACKEND",
  "Self": {
    "HostName": "kleros1",
    "DNSName": "kleros1.tail0000.ts.net.",
    "Online": $TS_ONLINE
  },
  "Peer": {
    "nodekey:0000": {"HostName": "kleros2", "DNSName": "kleros2.tail0000.ts.net.", "Online": true}
  }
}
JSON
    ;;
  "funnel status")
    if [ "$TS_FUNNEL" = on ]; then
      printf '# Funnel on:\n#     - https://kleros1.tail0000.ts.net\n\n'
      printf 'https://kleros1.tail0000.ts.net (Funnel on)\n|-- / proxy http://127.0.0.1:8080\n'
    else
      echo "No serve config"
    fi ;;
  *) echo "stub: unexpected: tailscale $*" >&2; exit 2 ;;
esac
EOF
cat > "$T/bin/curl" <<'EOF'
#!/usr/bin/env bash
url=
for a in "$@"; do case $a in http://*|https://*) url=$a ;; esac; done
echo "curl $url" >> "$STUB_CALLS"
if [ "$url" = https://controlplane.tailscale.com/ ]; then
  [ "$NET" = up ] || exit 28  # curl's timeout exit code
  exit 0
fi
n=$(cat "$STUB_DIR/self_n" 2>/dev/null || echo 0)
echo $((n + 1)) > "$STUB_DIR/self_n"
codes=($SELF_CODES)
code=${codes[$((n < ${#codes[@]} ? n : ${#codes[@]} - 1))]}
printf '%s' "$code"  # what -w '%{http_code}' prints
[ "$code" = 000 ] && exit 28
exit 0
EOF
cat > "$T/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB_CALLS"
exit "$RESTART_RC"
EOF
cat > "$T/bin/timeout" <<'EOF'
#!/usr/bin/env bash
shift  # never times out: just runs the command
exec "$@"
EOF
cat > "$T/bin/sudo" <<'EOF'
#!/usr/bin/env bash
echo "sudo $*" >> "$STUB_CALLS"
exit 1
EOF
chmod +x "$T"/bin/*

for c in tailscale curl systemctl timeout sudo; do
  [ "$(PATH="$T/bin:$PATH" command -v "$c")" = "$T/bin/$c" ] || { echo "stub $c isn't first on PATH"; exit 1; }
done

# fresh: new scenario, empty state.  run [VAR=value ...]: one watchdog run ($rc, $T/out, $T/calls)
fresh() { rm -rf "$T/state" "$T/all_calls"; mkdir -p "$T/state"; touch "$T/all_calls"; }
run() {
  : > "$STUB_CALLS"
  rm -f "$T/self_n"
  env PATH="$T/bin:$PATH" WATCHDOG_STATE_DIR="$T/state" TS_BACKEND=Running TS_ONLINE=true \
    TS_FUNNEL=on TS_DOWN=0 NET=up SELF_CODES=200 RESTART_RC=0 "$@" "$BASH" "$script" > "$T/out" 2>&1
  rc=$?
  cat "$STUB_CALLS" >> "$T/all_calls"
}

rc_is() { [ "$rc" = "$1" ] || { echo "      exit code $rc, want $1"; return 1; }; }
fails_is() {
  local got
  got=$(cat "$T/state/fails" 2>/dev/null || echo none)
  [ "$got" = "$1" ] || { echo "      failure count $got, want $1"; return 1; }
}
called() { grep -qxF "$1" "$T/calls" || { echo "      not called: $1"; return 1; }; }
not_called() { ! grep -q "^$1" "$T/calls" || { echo "      called: $(grep "^$1" "$T/calls" | head -1)"; return 1; }; }
said() { grep -qF "$1" "$T/out" || { echo "      output lacks: $1"; return 1; }; }
self_checks_are() {
  local got
  got=$(grep -c '^curl https://kleros1' "$T/calls")
  [ "$got" = "$1" ] || { echo "      $got self-checks, want $1"; return 1; }
}
restarts_are() {
  local got
  got=$(grep -c '^systemctl restart tailscaled$' "$T/all_calls")
  [ "$got" = "$1" ] || { echo "      $got restarts, want $1"; return 1; }
}

failed=0
t() {  # t <name> <function>
  local why
  if why=$("$2"); then
    echo "PASS  $1"
  else
    echo "FAIL  $1"
    echo "$why"
    echo "      last run's output:"
    sed 's/^/        /' "$T/out"
    failed=$((failed + 1))
  fi
}

not_installed() {
  fresh
  run PATH="$T/empty"
  rc_is 0 && said "isn't installed" && fails_is none && [ ! -s "$T/calls" ]
}
not_signed_in() {
  local s
  for s in NeedsLogin NeedsMachineAuth Stopped NoState; do
    fresh
    echo 2 > "$T/state/fails"
    run TS_BACKEND=$s TS_ONLINE=false NET=down
    rc_is 0 && said "$s" && fails_is 2 && not_called curl && not_called systemctl || return 1
  done
}
no_internet() {
  fresh
  echo 2 > "$T/state/fails"
  run TS_ONLINE=false NET=down
  rc_is 0 && said "no internet" && fails_is 0 && called "curl https://controlplane.tailscale.com/" \
    && self_checks_are 0 && not_called systemctl
}
healthy_funnel_on() {
  fresh
  echo 2 > "$T/state/fails"
  run
  rc_is 0 && said "ok again" && fails_is 0 && called "curl https://kleros1.tail0000.ts.net/status" \
    && self_checks_are 1 && not_called systemctl
}
healthy_funnel_off() {
  fresh
  run TS_FUNNEL=off
  rc_is 0 && said "ok: online" && fails_is 0 && called "tailscale funnel status" && self_checks_are 0
}
self_check_retried() {
  fresh
  run SELF_CODES="000 200"
  rc_is 0 && fails_is 0 && self_checks_are 2
}
funnel_502() {
  fresh
  run SELF_CODES=502
  rc_is 0 && fails_is 1 && self_checks_are 2 && said "answered 502" && not_called systemctl
}
tailscaled_not_answering() {
  fresh
  run TS_DOWN=1
  rc_is 0 && fails_is 1 && said "didn't answer" && not_called systemctl
}
stuck_starting() {
  fresh
  echo junk > "$T/state/fails"
  run TS_BACKEND=Starting TS_ONLINE=false
  rc_is 0 && fails_is 1 && said "Starting" && self_checks_are 0
}
check_url_override() {
  fresh
  run TS_FUNNEL=off WATCHDOG_CHECK_URL=http://127.0.0.1:9/dead SELF_CODES=000
  rc_is 0 && fails_is 1 && called "curl http://127.0.0.1:9/dead" && said "no answer from http://127.0.0.1:9/dead"
}
three_failures_one_restart() {
  fresh
  run TS_ONLINE=false && rc_is 0 && fails_is 1 && restarts_are 0 || return 1
  run TS_ONLINE=false && rc_is 0 && fails_is 2 && restarts_are 0 || return 1
  run TS_ONLINE=false
  rc_is 0 && said "restarting tailscaled" && called "systemctl restart tailscaled" && restarts_are 1 \
    && fails_is 0 && [ -s "$T/state/last_restart" ] && not_called sudo
}
no_restart_within_gap() {  # continues from three_failures_one_restart
  local i
  for i in 1 2 3 4; do
    run TS_ONLINE=false
    rc_is 0 && fails_is "$i" && restarts_are 1 || return 1
  done
  said "not restarting yet"
}
healthy_resets_after_restart() {  # continues: a healthy run resets the count, no restart
  run
  rc_is 0 && fails_is 0 && restarts_are 1
}
restart_after_gap() {  # continues: once the gap has passed, the next failures restart again
  sleep 2
  run TS_ONLINE=false WATCHDOG_MIN_GAP=1 && fails_is 1 || return 1
  run TS_ONLINE=false WATCHDOG_MIN_GAP=1 && fails_is 2 || return 1
  run TS_ONLINE=false WATCHDOG_MIN_GAP=1
  rc_is 0 && restarts_are 2 && fails_is 0
}
failed_restart() {
  fresh
  run TS_ONLINE=false WATCHDOG_FAILS=1 RESTART_RC=1
  rc_is 1 && said "restart tailscaled failed" && restarts_are 1 || return 1
  run TS_ONLINE=false WATCHDOG_FAILS=1
  rc_is 0 && said "not restarting yet" && restarts_are 1
}

t "tailscale not installed: exits 0, touches nothing" not_installed
t "not signed in / stopped: left alone, not counted" not_signed_in
t "no internet: count reset, no self-check, no restart" no_internet
t "healthy, Funnel on: count reset, checks https://<DNSName>/status" healthy_funnel_on
t "healthy, Funnel off: no self-check" healthy_funnel_off
t "self-check gets a second try" self_check_retried
t "Funnel answers 502 twice: counted" funnel_502
t "tailscaled not answering: counted" tailscaled_not_answering
t "stuck in Starting: counted (bad count file read as 0)" stuck_starting
t "WATCHDOG_CHECK_URL is checked even with Funnel off" check_url_override
t "three failures in a row: exactly one restart" three_failures_one_restart
t "no second restart within the gap" no_restart_within_gap
t "healthy run resets the count" healthy_resets_after_restart
t "restart again once the gap has passed" restart_after_gap
t "failed restart: exit 1, no retry within the gap" failed_restart

if ((failed)); then echo "$failed failed"; exit 1; fi
echo "all passed"
