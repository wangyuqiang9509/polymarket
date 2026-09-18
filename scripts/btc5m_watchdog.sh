#!/usr/bin/env bash
set -euo pipefail

# Keep the BTC 5m live loop running while runtime/btc5m.keepalive exists.
# Stop via: btc5m_ctl.sh stop (clears keepalive and kills this watchdog).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$SKILL_ROOT/runtime"
CTL="$SCRIPT_DIR/btc5m_ctl.sh"
KEEPALIVE="$RUNTIME_DIR/btc5m.keepalive"
PIDFILE="$RUNTIME_DIR/btc5m_watchdog.pid"
LOG="$RUNTIME_DIR/btc5m_watchdog.log"
REPO="${BTC5M_REPO:-$(cd "$SKILL_ROOT/../.." && pwd)/pm-hl-conservative-plus-repo}"

mkdir -p "$RUNTIME_DIR"
echo $$ >"$PIDFILE"
touch "$KEEPALIVE"

export BTC5M_REPO="$REPO"
export BTC5M_MARTINGALE="${BTC5M_MARTINGALE:-0}"

SLEEP_SEC="${BTC5M_WATCHDOG_SEC:-30}"

log() {
  echo "[$(date -u +%FT%TZ)] $*" >>"$LOG"
}

log "watchdog_start sleep=${SLEEP_SEC}s keepalive=$KEEPALIVE"

while true; do
  if [[ ! -f "$KEEPALIVE" ]]; then
    log "keepalive_cleared exit"
    rm -f "$PIDFILE"
    exit 0
  fi

  trader_pid=""
  if [[ -f "$RUNTIME_DIR/btc5m.pid" ]]; then
    trader_pid="$(cat "$RUNTIME_DIR/btc5m.pid" 2>/dev/null || true)"
  fi
  if [[ -n "$trader_pid" ]] && ps -p "$trader_pid" >/dev/null 2>&1; then
    :
  else
    log "trader_stopped restarting"
    "$CTL" start --loop --profile conservative \
      --stake-usd 5 --stop-loss-pct 0.90 \
      --max-trades 36 --daily-loss-pct 80 \
      --entry-timeout-min 12 --poll-sec 5 \
      --close-retry-max 30 --close-retry-delay-sec 2 \
      >>"$LOG" 2>&1 || log "restart_failed"
  fi
  sleep "$SLEEP_SEC"
done
