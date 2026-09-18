#!/usr/bin/env python3
"""Entry and close rules from the Sep 16 live sample.

Keep 0.70 momentum. Do not fade the expensive side into the cheap side.
Skip asks that historically paid too little vs a full dump.
"""
from __future__ import annotations

from typing import Any, Optional

from btc5m_martingale import classify

DEFAULT_MAX_ASK = 0.85
DEFAULT_EXPENSIVE_ASK = 0.80
DEFAULT_EXPENSIVE_AFTER_WINS = 3
DEFAULT_MIN_SALVAGE_BID = 0.05
DEFAULT_HOLD_BID = 0.90
DEFAULT_CLOB_CUT_BID = 0.40
DEFAULT_CLOB_CUT_MAX_GAMMA = 0.50
# Live CLOB 25% SL only while the book is still liquid. Below this is the wick/dead zone.
DEFAULT_CLOB_SL_MIN_BID = 0.45
# Last ~20s is often untradeable on the loser. Flatten earlier if not a winner.
DEFAULT_EARLY_FLATTEN_SEC = 45
DEFAULT_EARLY_FLATTEN_BID = 0.55
# ~120s left, ±30s. Earlier in the candle is noise; later is too close to expiry.
DEFAULT_MIN_ENTRY_SECONDS_LEFT = 90
DEFAULT_MAX_ENTRY_SECONDS_LEFT = 150
# Any CLOB-driven cut must be seen on this many consecutive polls. Real losers
# take 15-45s to collapse; winner wicks in the trade prints lasted 0-2s (2/72).
DEFAULT_CONFIRM_POLLS = 2
# Poll every second once this close to expiry: losers go 0.6 -> 0.3 in seconds.
DEFAULT_FAST_POLL_WINDOW_SEC = 90
DEFAULT_FAST_POLL_SEC = 1.0
# Sell through the opposite book when ours is dead: buying the other side at
# ask a is the same trade as selling ours at 1-a. Skip if it recovers < 5c.
DEFAULT_HEDGE_MAX_ASK = 0.95


def in_entry_window(
    sec_left: Optional[float],
    *,
    min_left: float = DEFAULT_MIN_ENTRY_SECONDS_LEFT,
    max_left: float = DEFAULT_MAX_ENTRY_SECONDS_LEFT,
) -> tuple[bool, Optional[str]]:
    if sec_left is None:
        return False, 'heartbeat_bad_market_end'
    if float(sec_left) > float(max_left):
        return False, 'skip_too_early_to_enter'
    if float(sec_left) < float(min_left):
        return False, 'skip_too_late_to_enter'
    return True, None


def choose_side(
    up_ask: Optional[float],
    dn_ask: Optional[float],
    *,
    threshold: float = 0.70,
    max_ask: float = DEFAULT_MAX_ASK,
    win_streak: int = 0,
    expensive_after_wins: int = DEFAULT_EXPENSIVE_AFTER_WINS,
    expensive_ask: float = DEFAULT_EXPENSIVE_ASK,
) -> tuple[Optional[str], Optional[float], Optional[str]]:
    candidates: list[tuple[str, float]] = []
    if up_ask is not None and float(up_ask) >= threshold:
        candidates.append(('UP', float(up_ask)))
    if dn_ask is not None and float(dn_ask) >= threshold:
        candidates.append(('DOWN', float(dn_ask)))
    if not candidates:
        return None, None, 'skip_price_below_threshold'

    side, px = sorted(candidates, key=lambda x: x[1], reverse=True)[0]
    if px >= max_ask:
        return None, px, 'skip_ask_too_expensive'
    if int(win_streak) >= int(expensive_after_wins) and px >= expensive_ask:
        return None, px, 'skip_expensive_after_wins'
    return side, px, None


def consecutive_wins(trades: list[dict[str, Any]]) -> int:
    n = 0
    for trade in reversed(trades):
        outcome, _pnl = classify(trade)
        if outcome == 'win':
            n += 1
            continue
        if outcome == 'scratch':
            continue
        break
    return n


def fak_unmatched(text: str) -> bool:
    t = (text or '').lower()
    return (
        'no orders found to match with fak order' in t
        or 'no orderbook exists' in t
        or 'status_code=404' in t
    )


def salvage_limit_price(
    best_bid: Optional[float],
    gamma_last: Optional[float],
    min_bid: float = DEFAULT_MIN_SALVAGE_BID,
) -> Optional[float]:
    px = None
    if best_bid is not None and float(best_bid) >= min_bid:
        px = float(best_bid)
    elif gamma_last is not None and float(gamma_last) >= min_bid:
        px = float(gamma_last)
    if px is None:
        return None
    return max(0.01, min(0.99, round(px - 0.01, 6)))


def should_hold_to_settlement(
    best_bid: Optional[float],
    gamma_last: Optional[float],
    hold_bid: float = DEFAULT_HOLD_BID,
) -> bool:
    """At 20s the winning book is still live (~0.99) and the losing book is dead.

    Selling then only clips winners. Hold if the live bid still looks like the winner.
    """
    px = None
    if best_bid is not None:
        px = float(best_bid)
    elif gamma_last is not None:
        px = float(gamma_last)
    return px is not None and px >= hold_bid


def should_cut_on_clob_bid(
    best_bid: Optional[float],
    gamma_last: Optional[float] = None,
    *,
    best_ask: Optional[float] = None,
    cut_bid: float = DEFAULT_CLOB_CUT_BID,
    min_bid: float = DEFAULT_MIN_SALVAGE_BID,
    max_gamma: float = DEFAULT_CLOB_CUT_MAX_GAMMA,
) -> bool:
    """Sell when the CLOB bid is in the floor zone and a second source confirms.

    A lone CLOB wick to 0.28–0.40 with Gamma still 0.73–0.96 sold winners
    on 2026-09-17, so a bid alone is not enough. But Gamma lags: on
    2026-09-17/18 six losers went 0.70 → 0.01 with Gamma still 0.49–0.86 and
    were never sold. The same book's best ask does not lag — a pulled bid
    leaves the ask high, a real loss drags the ask down too. Confirm with
    the ask first, Gamma second. Dead 1-cent books cannot be salvaged.
    """
    if best_bid is None:
        return False
    px = float(best_bid)
    if not (float(min_bid) <= px <= float(cut_bid)):
        return False
    if best_ask is not None and float(best_ask) <= float(max_gamma):
        return True
    return gamma_last is not None and float(gamma_last) <= float(max_gamma)


def should_cut_on_clob_stop(
    best_bid: Optional[float],
    sl_price: Optional[float],
    *,
    best_ask: Optional[float] = None,
    gamma_last: Optional[float] = None,
    min_live_bid: float = DEFAULT_CLOB_SL_MIN_BID,
    min_salvage: float = DEFAULT_MIN_SALVAGE_BID,
    confirm_px: float = DEFAULT_CLOB_CUT_MAX_GAMMA,
) -> bool:
    """Cut on CLOB bid vs entry*0.75 while the book is still sellable.

    Bid >= min_live_bid (0.45) fires on its own. Below that (the 0.05-0.45
    wick/dead zone, Sep 17 winners printed 0.28-0.37) it needs the same
    book's ask or Gamma <= confirm_px. A real loser jumps 0.6 -> 0.3 between
    two polls, so the band must reach down to the salvage floor.
    """
    if best_bid is None or sl_price is None:
        return False
    px = float(best_bid)
    if not (float(min_salvage) <= px <= float(sl_price)):
        return False
    if px >= float(min_live_bid):
        return True
    if best_ask is not None and float(best_ask) <= float(confirm_px):
        return True
    return gamma_last is not None and float(gamma_last) <= float(confirm_px)


def confirm_cut(pending: Optional[str], reason: Optional[str], count: int, *, polls: int = DEFAULT_CONFIRM_POLLS) -> tuple[Optional[str], Optional[str], int]:
    """Require the same cut reason on `polls` consecutive polls.

    Returns (fire_reason, pending, count). A wick that shows on one poll and
    is gone on the next never fires; a real collapse fires one poll late.
    """
    if not reason:
        return None, None, 0
    if reason == pending:
        count += 1
    else:
        pending, count = reason, 1
    if count >= max(1, int(polls)):
        return reason, pending, count
    return None, pending, count


def hedge_notional(shares: float, opp_ask: Optional[float], *, max_ask: float = DEFAULT_HEDGE_MAX_ASK) -> Optional[float]:
    """USDC to spend on the opposite token to neutralise `shares` of ours.

    None when the other side is too expensive to be worth it (recovers < 1-max_ask).
    """
    if opp_ask is None or shares <= 0:
        return None
    a = float(opp_ask)
    if a <= 0 or a > float(max_ask):
        return None
    return round(float(shares) * a, 4)


def should_cut_on_gamma_stop(
    gamma_last: Optional[float],
    sl_price: Optional[float],
    best_bid: Optional[float],
    *,
    min_salvage: float = DEFAULT_MIN_SALVAGE_BID,
) -> bool:
    """Gamma 25% SL only if a CLOB bid is still there to hit.

    04:42: Gamma 0.495 with CLOB bid 0.77 sold. 05:40: Gamma stayed 0.855 and
    the Down book 404'd — Gamma-only would fire too late or not at all.
    """
    if gamma_last is None or sl_price is None:
        return False
    if float(gamma_last) > float(sl_price):
        return False
    if best_bid is None or float(best_bid) < float(min_salvage):
        return False
    return True


def should_flatten_before_dead_book(
    best_bid: Optional[float],
    *,
    flatten_below: float = DEFAULT_EARLY_FLATTEN_BID,
    min_salvage: float = DEFAULT_MIN_SALVAGE_BID,
) -> bool:
    """At ~45s left, sell if the book is live but no longer a winner.

    Do not flatten 0.70-0.89 names (those can still run to 0.99).
    Do not flatten penny/404 books (unsellable).
    """
    if best_bid is None:
        return False
    px = float(best_bid)
    return float(min_salvage) <= px < float(flatten_below)
