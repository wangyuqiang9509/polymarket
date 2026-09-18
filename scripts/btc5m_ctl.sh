#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="$SKILL_ROOT/runtime"
WORKSPACE_ROOT="$(cd "$SKILL_ROOT/../.." && pwd)"
REPO_DEFAULT="$WORKSPACE_ROOT/pm-hl-conservative-plus-repo"
REPO="${BTC5M_REPO:-$REPO_DEFAULT}"
RUNNER="${BTC5M_RUNNER:-$SKILL_ROOT/scripts/test_btc_5m_session_exit_sl.py}"
VENV_PY="$REPO/.venv/bin/python"
ENV_FILE="${BTC5M_ENV_FILE:-$REPO/.env}"

PIDFILE="$RUNTIME_DIR/btc5m.pid"
METAFILE="$RUNTIME_DIR/btc5m.meta.json"
LATEST_LINK="$RUNTIME_DIR/latest.log"

mkdir -p "$RUNTIME_DIR"

usage() {
  cat <<'EOF'
Usage:
  btc5m_ctl.sh start [--loop] [--profile conservative|aggressive] [--entry-timeout-min N] [--stake-usd N] [--threshold N] [--stop-loss-pct N] [--no-gamma-sl] [--min-entry-seconds-left N] [--max-entry-seconds-left N] [--max-trades N] [--daily-loss-pct N] [--poll-sec N] [--close-retry-max N] [--close-retry-delay-sec N]
  btc5m_ctl.sh status
  btc5m_ctl.sh stop
  btc5m_ctl.sh report [--limit N]
  btc5m_ctl.sh journal
  btc5m_ctl.sh logs

Notes:
- Runs in isolated skill runtime: skills/btc-5m-live/runtime
- Uses auth/env from pm-hl-conservative-plus-repo/.env
EOF
}

is_running() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1
  else
    return 1
  fi
}

cmd_start() {
  local profile="conservative"
  local entry_timeout_min="35"
  local stake_usd=""
  local threshold=""
  local poll_sec="2"
  local close_retry_max="30"
  local close_retry_delay_sec="2"
  local stop_loss_pct=""
  local min_entry_seconds_left=""
  local max_entry_seconds_left=""
  local max_trades=""
  local daily_loss_pct=""
  local loop="0"
  local no_gamma_sl="0"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --profile) profile="$2"; shift 2;;
      --entry-timeout-min) entry_timeout_min="$2"; shift 2;;
      --stake-usd) stake_usd="$2"; shift 2;;
      --threshold) threshold="$2"; shift 2;;
      --stop-loss-pct) stop_loss_pct="$2"; shift 2;;
      --min-entry-seconds-left) min_entry_seconds_left="$2"; shift 2;;
      --max-entry-seconds-left) max_entry_seconds_left="$2"; shift 2;;
      --max-trades) max_trades="$2"; shift 2;;
      --daily-loss-pct) daily_loss_pct="$2"; shift 2;;
      --poll-sec) poll_sec="$2"; shift 2;;
      --close-retry-max) close_retry_max="$2"; shift 2;;
      --close-retry-delay-sec) close_retry_delay_sec="$2"; shift 2;;
      --no-gamma-sl) no_gamma_sl="1"; shift;;
      --loop) loop="1"; shift;;
      *) echo "Unknown arg: $1"; usage; exit 2;;
    esac
  done

  if is_running; then
    echo "already_running pid=$(cat "$PIDFILE")"
    return 0
  fi

  local ts log
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$RUNTIME_DIR/btc5m_${profile}_${ts}.log"

  local -a runner_cmd
  runner_cmd=("$VENV_PY" "$RUNNER" "--profile" "$profile" "--entry-timeout-min" "$entry_timeout_min" "--poll-sec" "$poll_sec" "--close-retry-max" "$close_retry_max" "--close-retry-delay-sec" "$close_retry_delay_sec" "--execute")
  [[ -n "$stake_usd" ]] && runner_cmd+=("--stake-usd" "$stake_usd")
  [[ -n "$threshold" ]] && runner_cmd+=("--threshold" "$threshold")
  [[ -n "$stop_loss_pct" ]] && runner_cmd+=("--stop-loss-pct" "$stop_loss_pct")
  [[ -n "$min_entry_seconds_left" ]] && runner_cmd+=("--min-entry-seconds-left" "$min_entry_seconds_left")
  [[ -n "$max_entry_seconds_left" ]] && runner_cmd+=("--max-entry-seconds-left" "$max_entry_seconds_left")
  [[ "$no_gamma_sl" == "1" ]] && runner_cmd+=("--no-gamma-sl")

  (
    if [[ -f "$ENV_FILE" ]]; then
      set -a
      # shellcheck disable=SC1090
      source "$ENV_FILE"
      set +a
    fi
    cd "$REPO"
    export PYTHONUNBUFFERED=1
    if [[ "$loop" == "1" ]]; then
      export BTC5M_VENV_PY="$VENV_PY"
      export BTC5M_MARTINGALE="${BTC5M_MARTINGALE:-0}"
      [[ -n "$stake_usd" ]] && export BTC5M_STAKE_USD="$stake_usd"
      [[ -n "$max_trades" ]] && export BTC5M_MAX_TRADES_PER_DAY="$max_trades"
      [[ -n "$daily_loss_pct" ]] && export BTC5M_DAILY_LOSS_PCT="$daily_loss_pct"
      [[ -n "${BTC5M_STOP_USD:-}" ]] && export BTC5M_STOP_USD
      [[ -n "${BTC5M_MAX_SESSIONS:-}" ]] && export BTC5M_MAX_SESSIONS
      nohup bash "$SCRIPT_DIR/btc5m_loop.sh" "$log" "${runner_cmd[@]}" >"$log" 2>&1 &
    else
      nohup "${runner_cmd[@]}" >"$log" 2>&1 &
    fi
    echo $! >"$PIDFILE"
  )

  ln -sfn "$log" "$LATEST_LINK"
  local pid
  pid="$(cat "$PIDFILE")"

  cat >"$METAFILE" <<JSON
{
  "startedAt": "$(date -u +%FT%TZ)",
  "pid": $pid,
  "profile": "$profile",
  "entryTimeoutMin": $entry_timeout_min,
  "pollSec": $poll_sec,
  "closeRetryMax": $close_retry_max,
  "closeRetryDelaySec": $close_retry_delay_sec,
  "log": "$log",
  "repo": "$REPO",
  "loop": $loop,
  "stopLossPct": "${stop_loss_pct:-}",
  "maxTrades": "${max_trades:-}",
  "dailyLossPct": "${daily_loss_pct:-}",
  "minEntrySecondsLeft": "${min_entry_seconds_left:-}",
  "maxEntrySecondsLeft": "${max_entry_seconds_left:-}",
  "noGammaSl": $no_gamma_sl
}
JSON

  sleep 1
  if ps -p "$pid" >/dev/null 2>&1; then
    echo "started pid=$pid log=$log"
  else
    echo "failed_to_start (check $log)"
    exit 1
  fi
}

cmd_status() {
  if is_running; then
    local pid
    pid="$(cat "$PIDFILE")"
    echo "running pid=$pid"
    ps -p "$pid" -o pid=,etime=,command=
  else
    echo "stopped"
  fi
  if [[ -f "$METAFILE" ]]; then
    echo "meta=$METAFILE"
  fi
  if [[ -L "$LATEST_LINK" ]]; then
    echo "latest_log=$(readlink "$LATEST_LINK")"
  fi
  if [[ -f "$RUNTIME_DIR/btc5m.keepalive" ]]; then
    echo "keepalive=on"
  else
    echo "keepalive=off"
  fi
  if [[ -f "$RUNTIME_DIR/btc5m_watchdog.pid" ]]; then
    local wpid
    wpid="$(cat "$RUNTIME_DIR/btc5m_watchdog.pid" 2>/dev/null || true)"
    if [[ -n "$wpid" ]] && ps -p "$wpid" >/dev/null 2>&1; then
      echo "watchdog=running pid=$wpid"
    else
      echo "watchdog=dead"
    fi
  else
    echo "watchdog=off"
  fi
}

cmd_stop() {
  rm -f "$RUNTIME_DIR/btc5m.keepalive"
  local wpid=""
  if [[ -f "$RUNTIME_DIR/btc5m_watchdog.pid" ]]; then
    wpid="$(cat "$RUNTIME_DIR/btc5m_watchdog.pid" 2>/dev/null || true)"
  fi
  if [[ -n "$wpid" ]] && ps -p "$wpid" >/dev/null 2>&1; then
    kill "$wpid" 2>/dev/null || true
    sleep 1
    if ps -p "$wpid" >/dev/null 2>&1; then
      kill -9 "$wpid" 2>/dev/null || true
    fi
    echo "watchdog_stopped pid=$wpid"
  fi
  rm -f "$RUNTIME_DIR/btc5m_watchdog.pid"

  if ! is_running; then
    echo "already_stopped"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  pkill -P "$pid" || true
  kill "$pid" || true
  sleep 1
  if ps -p "$pid" >/dev/null 2>&1; then
    pkill -9 -P "$pid" || true
    kill -9 "$pid" || true
  fi
  rm -f "$PIDFILE"
  echo "stopped pid=$pid"
}

cmd_report() {
  local limit="20"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --limit) limit="$2"; shift 2;;
      *) echo "Unknown arg: $1"; usage; exit 2;;
    esac
  done
  "$VENV_PY" "$SKILL_ROOT/scripts/btc5m_report.py" --runtime-dir "$RUNTIME_DIR" --limit "$limit"
}

cmd_journal() {
  "$VENV_PY" "$SKILL_ROOT/scripts/btc5m_live_journal.py" \
    --runtime-dir "$RUNTIME_DIR" \
    --out-md "$SKILL_ROOT/data/live_trades.md" \
    --out-json "$SKILL_ROOT/data/live_trades.json"
}

cmd_logs() {
  if [[ -L "$LATEST_LINK" ]]; then
    tail -n 120 "$(readlink "$LATEST_LINK")"
  else
    echo "no_logs"
  fi
}

main() {
  local cmd="${1:-}"
  [[ -z "$cmd" ]] && { usage; exit 2; }
  shift || true
  case "$cmd" in
    start) cmd_start "$@" ;;
    status) cmd_status ;;
    stop) cmd_stop ;;
    report) cmd_report "$@" ;;
    journal) cmd_journal ;;
    logs) cmd_logs ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
