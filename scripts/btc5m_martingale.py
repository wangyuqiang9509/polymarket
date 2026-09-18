#!/usr/bin/env python3
"""5 → 10 → 30 → 5 stake ladder from the live journal.

Loss = economic PnL <= -1 USDC (full-stake dumps, not scratches).
After the $30 ticket, reset to $5 whether it wins or loses.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

LOSS_USD = -1.0
WIN_USD = 0.30
STAKE_BY_STREAK = {0: 5.0, 1: 10.0, 2: 30.0}


def fnum(v: Any) -> Optional[float]:
    try:
        if v is None or v == '':
            return None
        return float(v)
    except Exception:
        return None


def classify(trade: dict[str, Any]) -> tuple[str, Optional[float]]:
    pnl = fnum(trade.get('realized_pnl_usdc'))
    if pnl is None:
        pnl = fnum(trade.get('hold_to_settlement_pnl_usdc'))
    if pnl is None:
        return 'unknown', None
    if pnl <= LOSS_USD:
        return 'loss', pnl
    if pnl >= WIN_USD:
        return 'win', pnl
    return 'scratch', pnl


def stake_for(streak: int) -> float:
    return STAKE_BY_STREAK[max(0, min(int(streak), 2))]


def next_streak(prev_streak: int, outcome: str, played_stake: Optional[float] = None) -> int:
    """Size up only after the ticket that was actually staked.

    $5 loss → $10; $10 win → $5; $10 loss → $30; $30 either way → $5.
    A scratch keeps the pending step. Extra $5 losses do not skip to $30.
    """
    if outcome in ('scratch', 'unknown'):
        return max(0, min(int(prev_streak), 2))
    played = float(played_stake or 0)
    if outcome == 'win':
        # Only the $10/$30 recovery ticket resets to $5.
        # A later $5 win does not cancel a pending $10 after a real loss.
        if played >= 9.0 or int(prev_streak) <= 0:
            return 0
        return max(0, min(int(prev_streak), 2))
    # loss below
    if played >= 29.0:
        return 0
    if played >= 9.0:
        return 2
    return 1


def load_trades(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        trades = data.get('trades')
        if isinstance(trades, list):
            return trades
    return []


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            'loss_streak': 0,
            'next_stake': 5.0,
            'last_opened_at': '',
            'last_outcome': '',
        }
    try:
        obj = json.loads(path.read_text())
    except Exception:
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    streak = int(obj.get('loss_streak') or 0)
    streak = max(0, min(streak, 2))
    return {
        'loss_streak': streak,
        'next_stake': float(obj.get('next_stake') or stake_for(streak)),
        'last_opened_at': str(obj.get('last_opened_at') or ''),
        'last_outcome': str(obj.get('last_outcome') or ''),
    }


def apply_new_trades(state: dict[str, Any], trades: list[dict[str, Any]]) -> dict[str, Any]:
    last_seen = str(state.get('last_opened_at') or '')
    streak = int(state.get('loss_streak') or 0)
    applied = 0
    last_outcome = str(state.get('last_outcome') or '')
    last_opened = last_seen
    for trade in trades:
        opened = str(trade.get('opened_at') or '')
        if not opened:
            continue
        if last_seen and opened <= last_seen:
            continue
        outcome, _pnl = classify(trade)
        played = fnum(trade.get('stake_usd'))
        streak = next_streak(streak, outcome, played)
        last_outcome = outcome
        last_opened = opened
        applied += 1
    out = {
        'loss_streak': streak,
        'next_stake': stake_for(streak),
        'last_opened_at': last_opened,
        'last_outcome': last_outcome,
        'applied': applied,
    }
    return out


def replay_all(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk the journal so a scratch after a loss keeps the $10/$30 step."""
    streak = 0
    last_outcome = ''
    last_opened = ''
    applied = 0
    for trade in trades:
        opened = str(trade.get('opened_at') or '')
        if not opened:
            continue
        outcome, _pnl = classify(trade)
        played = fnum(trade.get('stake_usd'))
        streak = next_streak(streak, outcome, played)
        last_outcome = outcome
        last_opened = opened
        applied += 1
    return {
        'loss_streak': streak,
        'next_stake': stake_for(streak),
        'last_opened_at': last_opened,
        'last_outcome': last_outcome,
        'applied': applied,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--journal', required=True)
    ap.add_argument('--state', required=True)
    args = ap.parse_args()

    journal = Path(args.journal)
    state_path = Path(args.state)
    trades = load_trades(journal)
    if state_path.is_file():
        state = apply_new_trades(load_state(state_path), trades)
    else:
        state = replay_all(trades)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    writable = {
        'loss_streak': state['loss_streak'],
        'next_stake': state['next_stake'],
        'last_opened_at': state['last_opened_at'],
        'last_outcome': state['last_outcome'],
    }
    state_path.write_text(json.dumps(writable, indent=2) + '\n')
    print(
        json.dumps(
            {
                'martingale': True,
                'next_stake': state['next_stake'],
                'loss_streak': state['loss_streak'],
                'last_outcome': state['last_outcome'],
                'last_opened_at': state['last_opened_at'],
                'applied': state.get('applied', 0),
            }
        ),
        flush=True,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
