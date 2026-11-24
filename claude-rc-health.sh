#!/bin/bash
# Gentle auto-heal for the `claude remote-control` daemon.
#
# Root failure it fixes: the "zombie bridge" (#41024): the process stays alive
# but its relay connection dies, so the app shows the session disconnected.
# launchd/systemd cannot see this (the process is not dead). It is detected by
# the absence of an ESTABLISHED TCP connection to the relay, then healed.
#
# Safety: network-gated, sustained failure (3 checks, about 6 min), 20 min
# cooldown, never restarts while the daemon still has live sessions (unless the
# bridge has been dead long enough that they are lost anyway).

# launchd may give this job no environment: without HOME every path below
# would turn relative.
HOME="${HOME:-$(eval echo ~$(id -un))}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=/usr/bin/python3; [ -x "$PY" ] || PY=python3
LABEL_PREFIX="${RC_LABEL_PREFIX:-com.vibe-wars}"
UNIT_PREFIX="${RC_UNIT_PREFIX:-vibe-wars}"
# Same daemon log location as rc_guardian.py; ps only our own processes on
# Linux (another user's claude on a shared box is not ours).
OS=$(uname -s)
if [ "$OS" = Darwin ]; then
  LOGDIR="$HOME/Library/Logs"; PS="ps -Ao"
else
  LOGDIR="$HOME/.claude/logs"; PS="ps -u $(id -u) -o"
fi
STATE_ROOT="${RC_STATE_DIR:-$(cd "$HERE" && "$PY" -c 'import rc_config as c; print(c.STATE_DIR)' 2>/dev/null)}"
STATE_ROOT="${STATE_ROOT:-$HOME/.vibe-wars}"
ANTH=https://api.anthropic.com/v1/messages
STATE_DIR="$STATE_ROOT/health"
LOG="$STATE_ROOT/logs/claude-rc-health.log"
UID_=$(id -u)
BAD_THRESHOLD=3
COOLDOWN=1200
now=$(date +%s)
mkdir -p "$STATE_DIR" "$STATE_ROOT/logs"

alert() { "$PY" "$HERE/rc_notify.py" "$1" >/dev/null 2>&1; }

# --- log rotation: capped in-place truncate (O_APPEND => no sparse holes) ---
# claude-rc.log grows without bound; keep the last ~8 MB of the noisy logs.
cap_log() {
  local f="$1" max="${2:-12000000}" keep="${3:-8000000}"
  [ -f "$f" ] || return 0
  local sz
  if [ "$OS" = Darwin ]; then sz=$(stat -f%z "$f" 2>/dev/null) || return 0
  else sz=$(stat -c%s "$f" 2>/dev/null) || return 0; fi
  if [ "$sz" -gt "$max" ]; then
    tail -c "$keep" "$f" > "$f.rot" 2>/dev/null && cat "$f.rot" > "$f" && rm -f "$f.rot"
  fi
}
cap_log "$LOGDIR"/claude-rc.log
cap_log "$LOGDIR"/claude-rc.err 4000000 2000000
cap_log "$LOG" 4000000 2000000

ts() { date '+%Y-%m-%d %H:%M:%S'; }
check_net() { local c; c=$(curl -s --max-time 8 -o /dev/null -w "%{http_code}" "$ANTH" 2>/dev/null); case "$c" in 200|400|401|403|404|405) return 0;; *) return 1;; esac; }
relayconn() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -a -p "$1" -iTCP -sTCP:ESTABLISHED 2>/dev/null | grep -vE "127\.0\.0\.1|\[::1\]|fe80:" | grep -c ESTABLISHED
  else
    ss -Htnp state established 2>/dev/null | grep -- "pid=$1," | grep -vE '^\s*[0-9]+\s+[0-9]+\s+(127\.|\[::1\])' | grep -c .
  fi
}
read_state() { BAD=0; LAST=0; [ -f "$STATE_DIR/$1" ] && read -r BAD LAST < "$STATE_DIR/$1"; [ -z "$BAD" ] && BAD=0; [ -z "$LAST" ] && LAST=0; }
write_state() { echo "$2 $3" > "$STATE_DIR/$1"; }

# --- network gate: do nothing during a real outage ---
if ! check_net; then
  echo "$(ts) NET down — skip" >> "$LOG"
  for f in "$STATE_DIR"/*; do [ -e "$f" ] || continue; l=$(cut -d' ' -f2 "$f" 2>/dev/null); echo "0 ${l:-0}" > "$f"; done
  exit 0
fi

# heal one target: $1=statekey  $2=pid(empty if missing)  $3=restart-cmd-desc  $4=restart-fn-name
heal() {
  local key="$1" pid="$2" desc="$3" fn="$4" bad
  if [ -z "$pid" ]; then bad=1; else [ "$(relayconn "$pid")" -ge 1 ] && bad=0 || bad=1; fi
  read_state "$key"
  if [ "$bad" = 0 ]; then write_state "$key" 0 "$LAST"; return; fi
  BAD=$((BAD+1))
  if [ "$BAD" -ge "$BAD_THRESHOLD" ] && [ $((now-LAST)) -gt "$COOLDOWN" ]; then
    echo "$(ts) $key RESTART — $desc" >> "$LOG"
    "$fn"; rc=$?
    # 2026-08-06: a deferred restart (rc=2) must NOT reset the counter. It used to,
    # so BAD could never climb past ~10 and the bad>=15 override was unreachable —
    # the daemon stayed dead 55 min while this logged "RESTART SKIPPED" every 20 min.
    if [ "$rc" = 2 ]; then write_state "$key" "$BAD" "$LAST"; else write_state "$key" 0 "$now"; fi
  else
    write_state "$key" "$BAD" "$LAST"
    echo "$(ts) $key bad $BAD/$BAD_THRESHOLD ($desc)" >> "$LOG"
  fi
}

# --- main server ---
# A restart KILLS every session the daemon spawned. So: never restart while it
# has live children, unless the bridge has been dead so long (bad >= 15) that
# those sessions are lost anyway; rc-guardian restores what dies. ps, not
# pgrep -f: pgrep sometimes cannot read the argv of a launchd-spawned process.
live_sessions() {
  [ -z "$SRV_PID" ] && { echo 0; return; }
  pgrep -P "$SRV_PID" 2>/dev/null | wc -l | tr -d ' '
}
# A signed-out daemon exits at once with "You must be logged in"; a restart
# cannot fix it. Then: no restart, one alert a day asking for a sign-in.
signed_out() {
  local f="$LOGDIR/claude-rc.err" m
  [ -f "$f" ] || return 1
  if [ "$OS" = Darwin ]; then m=$(stat -f%m "$f"); else m=$(stat -c%Y "$f"); fi
  [ $((now-m)) -lt 600 ] && tail -c 2000 "$f" | grep -q "must be logged in"
}
restart_server() {
  if signed_out; then
    local mark="$STATE_DIR/signed-out-paged" last=0
    [ -f "$mark" ] && last=$(cat "$mark" 2>/dev/null)
    echo "$(ts) server RESTART SKIPPED — signed out of claude.ai, restart cannot help" >> "$LOG"
    if [ $((now-${last:-0})) -gt 86400 ]; then
      echo "$now" > "$mark"
      alert "$(hostname -s): the remote-control daemon is signed out of claude.ai and cannot start. Sign in (claude auth login, or the panel). Next reminder in 24 h."
    fi
    return 0
  fi
  local live; live=$(live_sessions)
  if [ "$live" -gt 0 ] && [ "${BAD:-0}" -lt 15 ]; then
    echo "$(ts) server RESTART SKIPPED — $live live session(s), kickstart would kill them (bad=$BAD/15)" >> "$LOG"
    return 2
  fi
  [ "$live" -gt 0 ] && "$PY" "$HERE/rc_guardian.py" >> "$LOG" 2>&1
  if [ "$OS" = Darwin ]; then
    launchctl kickstart -k "gui/$UID_/$LABEL_PREFIX.remote-control" >> "$LOG" 2>&1
  else
    systemctl --user restart "$UNIT_PREFIX-remote-control.service" >> "$LOG" 2>&1
  fi
  alert "$(hostname -s): the remote-control daemon was dead (zombie bridge), restarted it. Live sessions at the time: $live. rc-guardian restores them within a few minutes."
}
# Match any prefix: RC_NAME_PREFIX differs per machine.
SRV_PID=$($PS pid=,args= | grep -- "remote-control --remote-control-session-name-prefix " | grep -v grep | awk '{print $1}' | head -1)
heal "server" "$SRV_PID" "zombie/missing main server" restart_server
