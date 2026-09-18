#!/usr/bin/env bash
set -euo pipefail

# Continuous conservative sessions. Stop via btc5m_ctl.sh stop.
LOG="${1:?log path required}"
shift

MAX_TRADES="${BTC5M_MAX_TRADES_PER_DAY:-12}"
MAX_SESSIONS="${BTC5M_MAX_SESSIONS:-36}"
DAILY_LOSS_PCT="${BTC5M_DAILY_LOSS_PCT:-10}"
STOP_USD="${BTC5M_STOP_USD:-}"
SLEEP_SEC="${BTC5M_LOOP_SLEEP_SEC:-8}"
VENV_PY="${BTC5M_VENV_PY:?}"
RUNTIME_DIR="$(cd "$(dirname "$LOG")" && pwd)"
SKILL_ROOT="$(cd "$RUNTIME_DIR/.." && pwd)"
JOURNAL_JSON="$SKILL_ROOT/data/live_trades.json"
MARTINGALE_STATE="$RUNTIME_DIR/btc5m.martingale.json"
MARTINGALE_ON="${BTC5M_MARTINGALE:-0}"

runner_with_stake() {
  local stake="$1"
  shift
  local -a out=()
  local skip=0
  local arg
  for arg in "$@"; do
    if [[ "$skip" == "1" ]]; then
      skip=0
      continue
    fi
    if [[ "$arg" == "--stake-usd" ]]; then
      skip=1
      continue
    fi
    out+=("$arg")
  done
  out+=("--stake-usd" "$stake")
  "${out[@]}"
}

refresh_stake() {
  local cash="${1:-0}"
  local stake="${BTC5M_STAKE_USD:-5}"
  if [[ "$MARTINGALE_ON" != "1" ]]; then
    echo "$stake"
    return 0
  fi
  if [[ ! -f "$SKILL_ROOT/scripts/btc5m_martingale.py" ]]; then
    echo "$stake"
    return 0
  fi
  local mg
  mg="$("$VENV_PY" "$SKILL_ROOT/scripts/btc5m_martingale.py" --journal "$JOURNAL_JSON" --state "$MARTINGALE_STATE" 2>/dev/null || true)"
  if [[ -n "$mg" ]]; then
    echo "$mg" >>"$LOG"
    stake="$("$VENV_PY" -c "import json,sys; print(json.loads(sys.argv[1]).get('next_stake',5))" "$mg" 2>/dev/null || echo 5)"
  fi
  stake="${stake:-5}"
  if awk -v c="$cash" -v s="$stake" 'BEGIN { exit !((c+0) > 0 && (s+0) > (c-2)) }'; then
    if awk -v c="$cash" 'BEGIN { exit !((c+0) >= 32) }'; then
      stake="30"
    elif awk -v c="$cash" 'BEGIN { exit !((c+0) >= 12) }'; then
      stake="10"
    else
      stake="5"
    fi
    echo "[$(date -u +%FT%TZ)] martingale_clamp cash=$cash stake=$stake" >>"$LOG"
  fi
  echo "$stake"
}

cash_now() {
  "$VENV_PY" - <<'PY'
import os
from py_clob_client_v2 import AssetType, BalanceAllowanceParams, ClobClient
host = os.getenv("PM_CLOB_HOST") or "https://clob.polymarket.com"
key = os.environ["PM_PRIVATE_KEY"]
funder = os.getenv("PM_FUNDER") or os.getenv("PM_ADDRESS")
sig = int(os.getenv("PM_SIGNATURE_TYPE", "3"))
l1 = ClobClient(host=host, chain_id=137, key=key)
creds = l1.create_or_derive_api_key()
c = ClobClient(host=host, chain_id=137, key=key, creds=creds, signature_type=sig, funder=funder)
raw = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)) or {}
bal = float(raw.get("balance") or 0)
print(bal / 1e6 if bal >= 1000 else bal)
PY
}

journal_n() {
  "$VENV_PY" - "$JOURNAL_JSON" <<'PY' 2>/dev/null || echo 0
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_file():
    print(0)
    raise SystemExit
d = json.loads(p.read_text())
print(len((d.get("trades") if isinstance(d, dict) else None) or []))
PY
}

# Return 1 if a fill since loop start is a full wipe or cash loss worse than STOP_USD.
hard_loss_hit() {
  [[ -z "${STOP_USD}" ]] && return 0
  "$VENV_PY" - "$JOURNAL_JSON" "$start_fills" "$STOP_USD" <<'PY'
import json, sys
from pathlib import Path

p = Path(sys.argv[1])
start_n = int(sys.argv[2])
limit = float(sys.argv[3])
if not p.is_file():
    raise SystemExit(0)
d = json.loads(p.read_text())
trades = (d.get("trades") if isinstance(d, dict) else None) or []
for trade in trades[start_n:]:
    realized = trade.get("realized_pnl_usdc")
    hold = trade.get("hold_to_settlement_pnl_usdc")
    if realized is not None and float(realized) <= -limit:
        print(
            "stop_realized opened=%s side=%s realized=%s reason=%s"
            % (trade.get("opened_at"), trade.get("side"), realized, trade.get("close_reason"))
        )
        raise SystemExit(1)
    if realized is None and hold is not None and float(hold) <= -limit:
        print(
            "stop_unsold_wipe opened=%s side=%s hold=%s winner=%s reason=%s"
            % (
                trade.get("opened_at"),
                trade.get("side"),
                hold,
                trade.get("settlement_winner"),
                trade.get("close_reason"),
            )
        )
        raise SystemExit(1)
raise SystemExit(0)
PY
}

echo "[$(date -u +%FT%TZ)] loop_start max_fills=$MAX_TRADES max_sessions=$MAX_SESSIONS daily_loss_pct=$DAILY_LOSS_PCT stop_usd=${STOP_USD:-off} martingale=$MARTINGALE_ON" >>"$LOG"
start_cash="$(cash_now || echo 0)"
echo "[$(date -u +%FT%TZ)] start_cash=$start_cash" >>"$LOG"
sessions=0
start_fills="$(journal_n)"
now_cash="$start_cash"

while true; do
  stake_usd="$(refresh_stake "$now_cash")"
  echo "[$(date -u +%FT%TZ)] session_begin sessions=$sessions fills=$(( $(journal_n) - start_fills )) stake_usd=$stake_usd" >>"$LOG"
  set +e
  runner_with_stake "$stake_usd" "$@" >>"$LOG" 2>&1
  rc=$?
  set -e
  sessions=$((sessions + 1))
  echo "[$(date -u +%FT%TZ)] session_end rc=$rc sessions=$sessions stake_usd=$stake_usd" >>"$LOG"

  # Refresh local live-trade journal after every session.
  if [[ -f "$SKILL_ROOT/scripts/btc5m_live_journal.py" ]]; then
    "$VENV_PY" "$SKILL_ROOT/scripts/btc5m_live_journal.py" \
      --runtime-dir "$RUNTIME_DIR" \
      --out-md "$SKILL_ROOT/data/live_trades.md" \
      --out-json "$SKILL_ROOT/data/live_trades.json" \
      >>"$LOG" 2>&1 || true
  fi

  fills=$(( $(journal_n) - start_fills ))
  echo "[$(date -u +%FT%TZ)] fills=$fills/$MAX_TRADES sessions=$sessions/$MAX_SESSIONS" >>"$LOG"
  if ! hard_loss_hit >>"$LOG"; then
    echo "[$(date -u +%FT%TZ)] stop_hard_loss_usd=${STOP_USD}" >>"$LOG"
    break
  fi
  if [[ "$fills" -ge "$MAX_TRADES" ]]; then
    echo "[$(date -u +%FT%TZ)] stop_max_fills=$MAX_TRADES" >>"$LOG"
    break
  fi
  if [[ "$sessions" -ge "$MAX_SESSIONS" ]]; then
    echo "[$(date -u +%FT%TZ)] stop_max_sessions=$MAX_SESSIONS fills=$fills" >>"$LOG"
    break
  fi

  # CLOB cash lags a few seconds after close; retry so we don't false-trip daily loss.
  sleep 15
  now_cash="$(cash_now || echo 0)"
  echo "[$(date -u +%FT%TZ)] cash=$now_cash" >>"$LOG"
  if awk -v s="$start_cash" -v n="$now_cash" 'BEGIN { if (s>0 && (s-n)>=4.5) exit 1; exit 0 }'; then
    :
  else
    sleep 15
    now_cash="$(cash_now || echo 0)"
    echo "[$(date -u +%FT%TZ)] cash_retry=$now_cash" >>"$LOG"
  fi
  if ! awk -v s="$start_cash" -v n="$now_cash" -v p="$DAILY_LOSS_PCT" 'BEGIN {
    if (s <= 0) exit 0
    if ((s - n) / s * 100 >= p) exit 1
    exit 0
  }'; then
    echo "[$(date -u +%FT%TZ)] stop_daily_loss start=$start_cash now=$now_cash pct=$DAILY_LOSS_PCT" >>"$LOG"
    break
  fi

  sleep "$SLEEP_SEC"
done

echo "[$(date -u +%FT%TZ)] loop_exit" >>"$LOG"
