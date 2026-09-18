#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
from typing import Any, Optional
from pathlib import Path

import requests

from py_clob_client_v2 import ApiCreds, ClobClient, OrderMarketCancelParams

from btc5m_martingale import load_trades
from btc5m_rules import (
    DEFAULT_CLOB_CUT_BID,
    DEFAULT_CLOB_SL_MIN_BID,
    DEFAULT_CONFIRM_POLLS,
    DEFAULT_FAST_POLL_SEC,
    DEFAULT_FAST_POLL_WINDOW_SEC,
    DEFAULT_HEDGE_MAX_ASK,
    DEFAULT_EARLY_FLATTEN_BID,
    DEFAULT_EARLY_FLATTEN_SEC,
    DEFAULT_EXPENSIVE_AFTER_WINS,
    DEFAULT_EXPENSIVE_ASK,
    DEFAULT_MAX_ASK,
    DEFAULT_MAX_ENTRY_SECONDS_LEFT,
    DEFAULT_MIN_ENTRY_SECONDS_LEFT,
    choose_side,
    confirm_cut,
    consecutive_wins,
    fak_unmatched,
    hedge_notional,
    in_entry_window,
    salvage_limit_price,
    should_cut_on_clob_bid,
    should_cut_on_clob_stop,
    should_cut_on_gamma_stop,
    should_flatten_before_dead_book,
    should_hold_to_settlement,
)

POLYGON = 137

UTC = dt.timezone.utc


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def ts_utc() -> str:
    return now_utc().isoformat().replace('+00:00', 'Z')


def parse_json_objects(text: str) -> list[dict[str, Any]]:
    out = []
    cur = []
    depth = 0
    for ch in text:
        if ch == '{':
            depth += 1
        if depth > 0:
            cur.append(ch)
        if ch == '}' and depth > 0:
            depth -= 1
            if depth == 0:
                s = ''.join(cur)
                cur = []
                try:
                    out.append(json.loads(s))
                except Exception:
                    pass
    return out


def bucket_5m(ts: int) -> int:
    return ts - (ts % 300)


def fetch_event(slug: str) -> Optional[dict[str, Any]]:
    r = requests.get('https://gamma-api.polymarket.com/events', params={'slug': slug}, timeout=12)
    r.raise_for_status()
    arr = r.json()
    return arr[0] if arr else None


def resolve_active_current_5m_market() -> Optional[dict[str, Any]]:
    """Return active BTC 5m market for the current slot only."""
    now = int(time.time())
    cur = bucket_5m(now)
    slug = f'btc-updown-5m-{cur}'

    try:
        ev = fetch_event(slug)
    except Exception:
        return None
    if not ev:
        return None

    mkts = ev.get('markets') or []
    if not mkts:
        return None

    m = mkts[0]
    if m.get('closed') is True:
        return None
    if m.get('active') is False:
        return None

    end_iso = str(m.get('endDate') or m.get('endDateIso') or '')
    try:
        end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None

    sec_left = end_ts - time.time()
    if sec_left <= 5:
        return None

    mm = dict(m)
    mm['_event_slug'] = slug
    mm['_seconds_left'] = sec_left
    return mm


def parse_json_field(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def market_side_prices(market: dict[str, Any]) -> tuple[float, float, str, str, str, str]:
    outcomes = parse_json_field(market.get('outcomes')) or []
    prices = parse_json_field(market.get('outcomePrices')) or []
    token_ids = parse_json_field(market.get('clobTokenIds')) or []
    if len(prices) < 2 or len(token_ids) < 2:
        raise RuntimeError('missing outcomePrices/clobTokenIds')

    up_i, down_i = 0, 1
    labs = [str(x).lower() for x in outcomes[:2]] if isinstance(outcomes, list) else []
    if len(labs) >= 2 and ('up' in labs[1] or 'yes' in labs[1]):
        up_i, down_i = 1, 0

    up_p = float(prices[up_i])
    dn_p = float(prices[down_i])
    up_t = str(token_ids[up_i])
    dn_t = str(token_ids[down_i])
    return up_p, dn_p, up_t, dn_t, str(market.get('slug') or market.get('_event_slug') or ''), str(market.get('endDate') or market.get('endDateIso') or '')


def _book_levels(book, attr: str):
    if isinstance(book, dict):
        return book.get(attr) or []
    return getattr(book, attr, []) or []


def _level_price(row) -> float:
    if isinstance(row, dict):
        return float(row.get('price') or 0)
    return float(getattr(row, 'price', 0) or 0)


def _best_bid_ask(book) -> tuple[Optional[float], Optional[float]]:
    bids = _book_levels(book, 'bids')
    asks = _book_levels(book, 'asks')
    best_bid = None
    best_ask = None
    for b in bids:
        p = _level_price(b)
        if p > 0 and (best_bid is None or p > best_bid):
            best_bid = p
    for a in asks:
        p = _level_price(a)
        if p > 0 and (best_ask is None or p < best_ask):
            best_ask = p
    return best_bid, best_ask


def clob_side_prices(up_token: str, down_token: str, clob_base: str = 'https://clob.polymarket.com') -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return trigger prices from CLOB orderbooks: UP ask, DOWN ask, spread of picked side when available."""
    pub = ClobClient(host=clob_base, chain_id=POLYGON)
    up_book = pub.get_order_book(str(up_token))
    dn_book = pub.get_order_book(str(down_token))
    up_bid, up_ask = _best_bid_ask(up_book)
    dn_bid, dn_ask = _best_bid_ask(dn_book)

    picked_spread = None
    # Side picked later by max ask; keep a generic sanity spread estimate
    if up_ask is not None and up_bid is not None:
        picked_spread = max(0.0, up_ask - up_bid)
    if dn_ask is not None and dn_bid is not None:
        s = max(0.0, dn_ask - dn_bid)
        picked_spread = s if picked_spread is None else min(picked_spread, s)

    return up_ask, dn_ask, picked_spread


def clob_best_bid_ask(token_id: str, clob_base: str = 'https://clob.polymarket.com') -> tuple[Optional[float], Optional[float]]:
    """Best bid and best ask of one token from a single book fetch.

    The ask is the non-lagging loss confirmation: Gamma stayed 0.49–0.86 on
    2026-09-17/18 losers whose bid was already 0.01–0.02.
    """
    pub = ClobClient(host=clob_base, chain_id=POLYGON)
    book = pub.get_order_book(str(token_id))
    return _best_bid_ask(book)


def clob_best_bid(token_id: str, clob_base: str = 'https://clob.polymarket.com') -> Optional[float]:
    best_bid, _ = clob_best_bid_ask(token_id, clob_base)
    return best_bid


def auth_clob_client(clob_base: str = 'https://clob.polymarket.com') -> Optional[ClobClient]:
    try:
        key = os.getenv('PM_PRIVATE_KEY') or ''
        funder = os.getenv('PM_FUNDER') or os.getenv('PM_ADDRESS') or None
        sig = int(os.getenv('PM_SIGNATURE_TYPE', '2'))
        v1 = os.getenv('PM_API_KEY') or ''
        v2 = os.getenv('PM_API_SECRET') or ''
        v3 = os.getenv('PM_API_PASSPHRASE') or ''
        if not key:
            return None
        l1 = ClobClient(host=clob_base, chain_id=POLYGON, key=key)
        if v1 and v2 and v3:
            creds = ApiCreds(api_key=v1, api_secret=v2, api_passphrase=v3)
        else:
            creds = l1.create_or_derive_api_key()
        return ClobClient(
            host=clob_base,
            chain_id=POLYGON,
            key=key,
            creds=creds,
            signature_type=sig,
            funder=funder,
        )
    except Exception:
        return None


def poll_order_status(client: Optional[ClobClient], order_id: str, wait_sec: float = 6.0, step_sec: float = 1.0) -> tuple[str, Optional[dict[str, Any]]]:
    if client is None or not order_id:
        return '', None
    deadline = time.time() + max(0.0, float(wait_sec))
    last = None
    while time.time() <= deadline:
        try:
            last = client.get_order(order_id)
            st = str((last or {}).get('status') or '').upper()
            if st and st not in ('LIVE', 'OPEN'):
                return st, last
        except Exception:
            pass
        time.sleep(max(0.2, float(step_sec)))
    try:
        last = client.get_order(order_id)
    except Exception:
        pass
    st = str((last or {}).get('status') or '').upper()
    return st, last


def cancel_token_orders(client: Optional[ClobClient], token_id: str) -> Optional[dict[str, Any]]:
    if client is None:
        return None
    try:
        return client.cancel_market_orders(OrderMarketCancelParams(asset_id=str(token_id)))
    except Exception as e:
        return {'error': str(e)}


def run_open(repo: str, slug: str, side: str, stake: float, execute: bool) -> tuple[str, list[dict[str, Any]]]:
    cmd = [
        '.venv/bin/python',
        'src/live/pm_live_trade_runner.py',
        '--market-slug', slug,
        '--force-side', side,
        '--start-equity', '100',
        '--risk-frac', str(stake / 100.0),
        '--max-notional-usd', str(stake),
    ]
    if execute:
        cmd.append('--execute')
    env = os.environ.copy()
    env.setdefault('PM_MAX_SPREAD', '1')
    env.setdefault('PM_MIN_TOP_ASK_NOTIONAL_USD', '0')
    env.setdefault('PM_ORDER_TYPE', 'FAK')
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or '') + '\n' + (p.stderr or '')
    return out, parse_json_objects(out)


def run_close(
    repo: str,
    slug: str,
    token_id: str,
    shares: float,
    execute: bool,
    close_order_type: str = 'FAK',
    close_limit_price: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    cmd = [
        '.venv/bin/python',
        'src/live/pm_live_trade_runner.py',
        '--market-slug', slug,
        '--close-token-id', token_id,
        '--close-shares', f'{shares:.8f}',
    ]
    if close_limit_price is not None and close_limit_price > 0:
        cmd += ['--close-limit-price', f'{close_limit_price:.6f}']
    if execute:
        cmd.append('--execute')
    env = os.environ.copy()
    env['PM_CLOSE_ORDER_TYPE'] = str(close_order_type or 'FAK').upper()
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or '') + '\n' + (p.stderr or '')
    return out, parse_json_objects(out)


def get_side_price_from_slug(slug: str, side: str) -> Optional[float]:
    try:
        ev = fetch_event(slug)
        if not ev:
            return None
        mkts = ev.get('markets') or []
        if not mkts:
            return None
        up, dn, *_ = market_side_prices(mkts[0])
        return up if side == 'UP' else dn
    except Exception:
        return None


def try_hedge_close(args: argparse.Namespace, opened: dict[str, Any], report: dict[str, Any], close_debug: list[dict[str, Any]], attempt: int) -> bool:
    """Sell through the opposite book: buy `shares` of the other token.

    Buying the other side at ask a pays exactly what selling ours at 1-a
    would. The loser's book 404s in the last minute; the winner's book never
    does. Costs shares*a of extra collateral until settlement.
    """
    opp_token = str(opened.get('opp_token_id') or '')
    opp_side = str(opened.get('opp_side') or '')
    if not opp_token or not opp_side:
        return False
    try:
        _opp_bid, opp_ask = clob_best_bid_ask(opp_token)
    except Exception:
        opp_ask = None
    notional = hedge_notional(float(opened['shares']), opp_ask, max_ask=float(args.hedge_max_ask))
    close_debug.append({
        'ts': ts_utc(),
        'attempt': attempt,
        'order_type': 'HEDGE_OPPOSITE',
        'status': 'try' if notional else 'skip_opposite_too_expensive',
        'opp_ask': opp_ask,
        'notional_usd': notional,
    })
    if not notional:
        return False
    out, objs = run_open(args.repo, opened['market_slug'], opp_side, float(notional), args.execute)
    post = {}
    runner = {}
    for o in objs:
        if isinstance(o, dict) and 'order_post_result' in o:
            runner = o
            post = o.get('order_post_result') or {}
    ok = bool(post and post.get('success') is True and str(post.get('status', '')).lower() == 'matched')
    close_debug.append({
        'ts': ts_utc(),
        'attempt': attempt,
        'order_type': 'HEDGE_OPPOSITE',
        'status': str(post.get('status') or 'error').lower(),
    })
    report['hedge_raw'] = out[-2000:]
    if not ok:
        return False
    report['hedge'] = {
        'side': opp_side,
        'token_id': str(runner.get('token_id') or opp_token),
        'shares': float(post.get('takingAmount') or 0),
        'cost_usdc': float(post.get('makingAmount') or 0),
        'entry_price': float(runner.get('entry_price') or (opp_ask or 0)),
        'order_id': post.get('orderID'),
        'tx': (post.get('transactionsHashes') or [None])[0],
        'at': ts_utc(),
    }
    return True


PROFILES: dict[str, dict[str, Any]] = {
    'conservative': {
        'threshold': 0.70,
        'stake_usd': 5.0,
        'stop_loss_pct': 0.25,
        'exit_before_sec': 20,
        'min_entry_seconds_left': DEFAULT_MIN_ENTRY_SECONDS_LEFT,
        'max_entry_seconds_left': DEFAULT_MAX_ENTRY_SECONDS_LEFT,
        'entry_timeout_min': 60,
        'poll_sec': 5.0,
        'max_ask': DEFAULT_MAX_ASK,
        'expensive_ask': DEFAULT_EXPENSIVE_ASK,
        'expensive_after_wins': DEFAULT_EXPENSIVE_AFTER_WINS,
        'clob_cut_bid': DEFAULT_CLOB_CUT_BID,
        'clob_sl_min_bid': DEFAULT_CLOB_SL_MIN_BID,
        'early_flatten_sec': DEFAULT_EARLY_FLATTEN_SEC,
        'early_flatten_bid': DEFAULT_EARLY_FLATTEN_BID,
    },
    'aggressive': {
        'threshold': 0.70,
        'stake_usd': 5.0,
        'stop_loss_pct': 0.30,
        'exit_before_sec': 20,
        'min_entry_seconds_left': DEFAULT_MIN_ENTRY_SECONDS_LEFT,
        'max_entry_seconds_left': DEFAULT_MAX_ENTRY_SECONDS_LEFT,
        'entry_timeout_min': 60,
        'poll_sec': 5.0,
        'max_ask': DEFAULT_MAX_ASK,
        'expensive_ask': DEFAULT_EXPENSIVE_ASK,
        'expensive_after_wins': DEFAULT_EXPENSIVE_AFTER_WINS,
        'clob_cut_bid': DEFAULT_CLOB_CUT_BID,
        'clob_sl_min_bid': DEFAULT_CLOB_SL_MIN_BID,
        'early_flatten_sec': DEFAULT_EARLY_FLATTEN_SEC,
        'early_flatten_bid': DEFAULT_EARLY_FLATTEN_BID,
    },
}


def apply_profile(args: argparse.Namespace) -> argparse.Namespace:
    prof = PROFILES.get(args.profile or 'conservative', PROFILES['conservative'])
    if args.threshold is None:
        args.threshold = float(prof['threshold'])
    if args.stake_usd is None:
        args.stake_usd = float(prof['stake_usd'])
    if args.stop_loss_pct is None:
        args.stop_loss_pct = float(prof['stop_loss_pct'])
    if args.exit_before_sec is None:
        args.exit_before_sec = int(prof['exit_before_sec'])
    if args.min_entry_seconds_left is None:
        args.min_entry_seconds_left = int(prof['min_entry_seconds_left'])
    if args.max_entry_seconds_left is None:
        args.max_entry_seconds_left = int(prof['max_entry_seconds_left'])
    if args.entry_timeout_min is None:
        args.entry_timeout_min = int(prof['entry_timeout_min'])
    if args.poll_sec is None:
        args.poll_sec = float(prof['poll_sec'])
    if args.max_ask is None:
        args.max_ask = float(prof['max_ask'])
    if args.expensive_ask is None:
        args.expensive_ask = float(prof['expensive_ask'])
    if args.expensive_after_wins is None:
        args.expensive_after_wins = int(prof['expensive_after_wins'])
    if args.clob_cut_bid is None:
        args.clob_cut_bid = float(prof['clob_cut_bid'])
    if args.clob_sl_min_bid is None:
        args.clob_sl_min_bid = float(prof['clob_sl_min_bid'])
    if args.early_flatten_sec is None:
        args.early_flatten_sec = int(prof['early_flatten_sec'])
    if args.early_flatten_bid is None:
        args.early_flatten_bid = float(prof['early_flatten_bid'])
    return args


def default_journal_path() -> Path:
    env_journal = os.environ.get('BTC5M_JOURNAL')
    if env_journal:
        return Path(env_journal)
    return Path(__file__).resolve().parents[1] / 'data' / 'live_trades.json'


def default_repo_path() -> str:
    env_repo = os.environ.get('BTC5M_REPO')
    if env_repo:
        return env_repo
    return str(Path(__file__).resolve().parents[3] / 'pm-hl-conservative-plus-repo')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default=default_repo_path())
    ap.add_argument('--profile', choices=['conservative', 'aggressive'], default='conservative')
    ap.add_argument('--threshold', type=float, default=None)
    ap.add_argument('--stake-usd', type=float, default=None)
    ap.add_argument('--stop-loss-pct', type=float, default=None, help='0.30 means -30%% from entry price')
    ap.add_argument('--exit-before-sec', type=int, default=None)
    ap.add_argument('--min-entry-seconds-left', type=int, default=None, help='Do not open if fewer seconds remain (default 90, ~120s-30s)')
    ap.add_argument('--max-entry-seconds-left', type=int, default=None, help='Do not open if more seconds remain (default 150, ~120s+30s)')
    ap.add_argument('--entry-timeout-min', type=int, default=None)
    ap.add_argument('--poll-sec', type=float, default=None)
    ap.add_argument('--max-ask', type=float, default=None, help='Skip if stronger-side ask is at or above this (do not fade the cheap side)')
    ap.add_argument('--expensive-ask', type=float, default=None, help='After a win streak, skip asks at or above this')
    ap.add_argument('--expensive-after-wins', type=int, default=None, help='Win streak that arms the expensive-ask skip')
    ap.add_argument('--clob-cut-bid', type=float, default=None, help='FAK-sell if current CLOB bid is between salvage min and this floor')
    ap.add_argument('--clob-sl-min-bid', type=float, default=None, help='CLOB 25%% SL only if bid is at or above this (wick/dead zone is below)')
    ap.add_argument('--early-flatten-sec', type=int, default=None, help='Seconds before end to flatten a live non-winner (default 45)')
    ap.add_argument('--early-flatten-bid', type=float, default=None, help='At early-flatten-sec, sell if CLOB bid is below this and still salvageable')
    ap.add_argument('--close-retry-max', type=int, default=18, help='Max close retries when position is not yet visible / not immediately closable')
    ap.add_argument('--close-retry-delay-sec', type=float, default=2.0, help='Delay between close retries')
    ap.add_argument('--gamma-sl', action='store_true', help='Also stop-loss on Gamma last vs entry (off by default: it sold 16 live winners at 0.70-0.95)')
    ap.add_argument('--no-gamma-sl', action='store_true', help='Deprecated no-op: Gamma stop is already off unless --gamma-sl')
    ap.add_argument('--confirm-polls', type=int, default=DEFAULT_CONFIRM_POLLS, help='A CLOB cut must be seen on this many consecutive polls (wicks last 0-2s)')
    ap.add_argument('--fast-poll-sec', type=float, default=DEFAULT_FAST_POLL_SEC, help='Poll interval inside the fast window before expiry')
    ap.add_argument('--fast-poll-window-sec', type=float, default=DEFAULT_FAST_POLL_WINDOW_SEC, help='Seconds before expiry from which the fast poll applies')
    ap.add_argument('--hedge-max-ask', type=float, default=DEFAULT_HEDGE_MAX_ASK, help='When our book is dead, buy the opposite token if its ask is at or below this')
    ap.add_argument('--no-hedge', action='store_true', help='Never sell through the opposite book')
    ap.add_argument('--execute', action='store_true')
    args = apply_profile(ap.parse_args())

    report: dict[str, Any] = {
        'started_at': ts_utc(),
        'params': {
            'profile': args.profile,
            'threshold': args.threshold,
            'stake_usd': args.stake_usd,
            'stop_loss_pct': args.stop_loss_pct,
            'exit_before_sec': args.exit_before_sec,
            'min_entry_seconds_left': args.min_entry_seconds_left,
            'max_entry_seconds_left': args.max_entry_seconds_left,
            'entry_timeout_min': args.entry_timeout_min,
            'poll_sec': args.poll_sec,
            'max_ask': args.max_ask,
            'expensive_ask': args.expensive_ask,
            'expensive_after_wins': args.expensive_after_wins,
            'clob_cut_bid': args.clob_cut_bid,
            'clob_sl_min_bid': args.clob_sl_min_bid,
            'early_flatten_sec': args.early_flatten_sec,
            'early_flatten_bid': args.early_flatten_bid,
            'close_retry_max': args.close_retry_max,
            'close_retry_delay_sec': args.close_retry_delay_sec,
            'gamma_sl': bool(args.gamma_sl),
            'no_gamma_sl': not bool(args.gamma_sl),
            'confirm_polls': args.confirm_polls,
            'fast_poll_sec': args.fast_poll_sec,
            'fast_poll_window_sec': args.fast_poll_window_sec,
            'hedge_max_ask': args.hedge_max_ask,
            'no_hedge': bool(args.no_hedge),
            'execute': args.execute,
        },
        'attempts': [],
    }

    win_streak = consecutive_wins(load_trades(default_journal_path()))
    report['params']['win_streak'] = win_streak

    deadline = time.time() + args.entry_timeout_min * 60
    opened = None

    while time.time() < deadline:
        try:
            m = resolve_active_current_5m_market()
            if not m:
                report['attempts'].append({'ts': ts_utc(), 'status': 'heartbeat_no_current_market'})
                time.sleep(args.poll_sec)
                continue

            g_up, g_dn, up_t, dn_t, slug, end_iso = market_side_prices(m)

            end_ts = None
            sec_left = None
            try:
                end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
                sec_left = max(0.0, end_ts - time.time())
            except Exception:
                pass

            if sec_left is None:
                report['attempts'].append({'ts': ts_utc(), 'slug': slug, 'status': 'heartbeat_bad_market_end'})
                time.sleep(args.poll_sec)
                continue

            # Only open around ~120s left (default 90-150). Too early is noise; too late is expiry risk.
            in_window, window_skip = in_entry_window(
                sec_left,
                min_left=args.min_entry_seconds_left,
                max_left=args.max_entry_seconds_left,
            )
            if not in_window:
                report['attempts'].append({
                    'ts': ts_utc(),
                    'slug': slug,
                    'status': window_skip or 'skip_outside_entry_window',
                    'seconds_left': sec_left,
                    'min_entry_seconds_left': args.min_entry_seconds_left,
                    'max_entry_seconds_left': args.max_entry_seconds_left,
                })
                time.sleep(args.poll_sec)
                continue

            # CLOB-based trigger price (best ask of selected side), not Gamma outcomePrices.
            try:
                up_ask, dn_ask, min_spread = clob_side_prices(up_t, dn_t)
            except Exception as e:
                report['attempts'].append({'ts': ts_utc(), 'slug': slug, 'status': 'skip_clob_unavailable', 'error': str(e)})
                time.sleep(args.poll_sec)
                continue

            report['attempts'].append({
                'ts': ts_utc(),
                'slug': slug,
                'status': 'heartbeat',
                'gamma_up': g_up,
                'gamma_down': g_dn,
                'clob_up_ask': up_ask,
                'clob_down_ask': dn_ask,
                'seconds_left': sec_left,
                'min_spread': min_spread,
            })

            side, trigger_price, skip_reason = choose_side(
                up_ask,
                dn_ask,
                threshold=args.threshold,
                max_ask=args.max_ask,
                win_streak=win_streak,
                expensive_after_wins=args.expensive_after_wins,
                expensive_ask=args.expensive_ask,
            )
            if skip_reason or side is None or trigger_price is None:
                report['attempts'].append({
                    'ts': ts_utc(),
                    'slug': slug,
                    'status': skip_reason or 'skip_price_below_threshold',
                    'threshold': args.threshold,
                    'max_ask': args.max_ask,
                    'win_streak': win_streak,
                    'clob_up_ask': up_ask,
                    'clob_down_ask': dn_ask,
                    'seconds_left': sec_left,
                    'trigger_price': trigger_price,
                })
                time.sleep(args.poll_sec)
                continue

            out, objs = run_open(args.repo, slug, side, args.stake_usd, args.execute)
            post = None
            runner = None
            for o in objs:
                if isinstance(o, dict) and 'order_post_result' in o:
                    runner = o
                    post = o.get('order_post_result') or {}
            if post and post.get('success') is True and str(post.get('status', '')).lower() == 'matched':
                token_id = str(runner.get('token_id') or (up_t if side == 'UP' else dn_t))
                shares = float(post.get('takingAmount') or 0)
                cost = float(post.get('makingAmount') or 0)
                entry_price = float(runner.get('entry_price') or trigger_price)
                opened = {
                    'opened_at': ts_utc(),
                    'market_slug': slug,
                    'market_end_iso': end_iso,
                    'side': side,
                    'token_id': token_id,
                    'opp_side': 'DOWN' if side == 'UP' else 'UP',
                    'opp_token_id': dn_t if side == 'UP' else up_t,
                    'entry_price': entry_price,
                    'shares': shares,
                    'cost_usdc': cost,
                    'open_order_id': post.get('orderID'),
                    'open_tx': (post.get('transactionsHashes') or [None])[0],
                }
                report['open_raw'] = out[-4000:]
                break
            else:
                report['last_open_try'] = out[-2000:]
        except Exception as e:
            report['attempts'].append({'ts': ts_utc(), 'status': 'error', 'error': str(e)})
        time.sleep(args.poll_sec)

    if not opened:
        report['finished_at'] = ts_utc()
        report['result'] = 'no_entry_timeout'
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    report['opened'] = opened

    # monitor after open: stop-loss or time exit
    end_ts = None
    try:
        end_ts = dt.datetime.fromisoformat(opened['market_end_iso'].replace('Z', '+00:00')).timestamp()
    except Exception:
        end_ts = time.time() + 300

    sl_price = opened['entry_price'] * (1.0 - args.stop_loss_pct)
    report['stop_loss_price'] = sl_price

    close_reason = None
    early_flatten_sec = int(args.early_flatten_sec)
    pending_reason = None
    pending_count = 0
    while True:
        now = time.time()
        if now >= (end_ts - args.exit_before_sec):
            close_reason = f'time_exit_{args.exit_before_sec}s_before_end'
            break

        side_px = get_side_price_from_slug(opened['market_slug'], opened['side'])
        report['last_side_price'] = side_px
        live_ask = None
        try:
            live_bid, live_ask = clob_best_bid_ask(opened['token_id'])
        except Exception:
            live_bid = None
        if live_bid is not None:
            report['last_clob_bid'] = live_bid
        if live_ask is not None:
            report['last_clob_ask'] = live_ask
        report['last_check_at'] = ts_utc()

        reason = None
        # Wide-band CLOB stop: bid <= entry*0.75 down to the salvage floor. Below 0.45 the
        # same book's ask (or Gamma) must confirm — a pulled bid with the ask still high is a wick.
        if should_cut_on_clob_stop(
            live_bid,
            sl_price,
            best_ask=live_ask,
            gamma_last=side_px,
            min_live_bid=args.clob_sl_min_bid,
        ):
            reason = f"stop_loss_clob_{int(args.stop_loss_pct * 100)}pct"
        elif should_cut_on_clob_bid(live_bid, side_px, best_ask=live_ask, cut_bid=args.clob_cut_bid):
            reason = 'clob_bid_floor'
        elif args.gamma_sl and should_cut_on_gamma_stop(side_px, sl_price, live_bid):
            reason = f"stop_loss_{int(args.stop_loss_pct * 100)}pct"
        elif now >= (end_ts - early_flatten_sec) and should_flatten_before_dead_book(
            live_bid,
            flatten_below=args.early_flatten_bid,
        ):
            # Last 20s is often a 404 on the loser. Flatten a live non-winner at ~45s.
            reason = f'time_exit_{early_flatten_sec}s_not_winning'

        # Wicks in the trade prints lasted 0-2s; a real collapse takes 15-45s. Ask for two polls.
        fire, pending_reason, pending_count = confirm_cut(pending_reason, reason, pending_count, polls=args.confirm_polls)
        if reason and not fire:
            report['pending_cut'] = {'reason': reason, 'count': pending_count, 'bid': live_bid, 'ask': live_ask, 'gamma': side_px, 'ts': ts_utc()}
        if fire:
            close_reason = fire
            report['cut_snapshot'] = {'reason': fire, 'bid': live_bid, 'ask': live_ask, 'gamma': side_px, 'seconds_left': end_ts - time.time()}
            break
        sec_left_now = end_ts - time.time()
        time.sleep(args.fast_poll_sec if sec_left_now <= args.fast_poll_window_sec else args.poll_sec)

    # Last 20s: winning book can still be sold (~0.99), losing book is usually 404.
    # Hold the winning book to settlement instead of clipping $0.01–0.08.
    if close_reason and close_reason.startswith('time_exit'):
        live_bid = report.get('last_clob_bid')
        try:
            bb = clob_best_bid(opened['token_id'])
            if bb is not None:
                live_bid = bb
                report['last_clob_bid'] = bb
        except Exception:
            pass
        gamma_last = report.get('last_side_price')
        if gamma_last is None:
            gamma_last = get_side_price_from_slug(opened['market_slug'], opened['side'])
            report['last_side_price'] = gamma_last
        if should_hold_to_settlement(live_bid, gamma_last):
            close_reason = 'hold_to_settlement_winning_book'
            report['closed'] = {
                'close_reason': close_reason,
                'closed_at': ts_utc(),
                'close_success': False,
                'close_status': 'held',
                'close_order_id': None,
                'close_tx': None,
                'close_shares': 0.0,
                'close_usdc': 0.0,
                'close_skipped': 'hold_to_settlement',
            }
            report['realized_cashflow_pnl_usdc'] = None
            report['finished_at'] = ts_utc()
            report['result'] = 'done'
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

    close_debug: list[dict[str, Any]] = []
    close_obj: dict[str, Any] = {}
    out = ''
    fallback_used = None
    force_close_used = None
    client = auth_clob_client()

    resting_gtc = False
    for i in range(max(1, int(args.close_retry_max))):
        if resting_gtc and args.execute:
            # A resting GTC reserves the shares; every FAK after it reports
            # zero_effective_shares (2026-09-18 02:17, 30 retries). Free them first.
            cancel_info = cancel_token_orders(client, opened['token_id'])
            close_debug.append({
                'ts': ts_utc(),
                'attempt': i + 1,
                'order_type': 'CANCEL_RESTING',
                'status': 'no_client' if client is None else ('error' if (cancel_info or {}).get('error') else 'ok'),
            })
            resting_gtc = False
        out, objs = run_close(
            args.repo,
            opened['market_slug'],
            opened['token_id'],
            opened['shares'],
            args.execute,
            close_order_type='FAK',
        )
        close_obj = {}
        post = {}
        for o in reversed(objs):
            if isinstance(o, dict) and 'order_post_result' in o:
                close_obj = o
                post = o.get('order_post_result') or {}
                break
        status = str(post.get('status') or '').lower()
        skipped = str(close_obj.get('close_skipped') or '')
        close_debug.append({
            'ts': ts_utc(),
            'attempt': i + 1,
            'order_type': 'FAK',
            'status': status,
            'close_skipped': skipped,
        })
        try:
            filled_amt = float(post.get('takingAmount') or 0)
        except Exception:
            filled_amt = 0.0
        if post.get('success') is True and (status == 'matched' or filled_amt > 0):
            break

        # common transient path right after open: token balance not yet visible
        if skipped == 'zero_effective_shares':
            time.sleep(float(args.close_retry_delay_sec))
            continue

        # FAK unmatched / dead book: GTC only if a real bid is still alive. Never 1-cent dump.
        txt = ((out or '') + '\n' + json.dumps(close_obj, ensure_ascii=False))
        if fak_unmatched(txt):
            bb = None
            try:
                bb = clob_best_bid(opened['token_id'])
            except Exception:
                bb = None
            if bb is None:
                bb = report.get('last_clob_bid')
            gamma_last = get_side_price_from_slug(opened['market_slug'], opened['side'])
            if gamma_last is None:
                gamma_last = report.get('last_side_price')
            # Our book is dead: sell through the other side's book (same payoff, live liquidity).
            if not args.no_hedge and not report.get('hedge'):
                if try_hedge_close(args, opened, report, close_debug, i + 1):
                    close_reason = f'{close_reason}+hedge_opposite'
                    break
            limit_px = salvage_limit_price(bb, gamma_last)
            if limit_px is None:
                close_debug.append({
                    'ts': ts_utc(),
                    'attempt': i + 1,
                    'order_type': 'SKIP_DEAD_BOOK',
                    'status': 'no_salvage_bid',
                    'close_skipped': skipped,
                })
                time.sleep(float(args.close_retry_delay_sec))
                continue
            fallback_used = {'type': 'GTC_LIMIT', 'price': limit_px}
            out2, objs2 = run_close(
                args.repo,
                opened['market_slug'],
                opened['token_id'],
                opened['shares'],
                args.execute,
                close_order_type='GTC',
                close_limit_price=limit_px,
            )
            close_obj2 = objs2[-1] if objs2 else {}
            post2 = close_obj2.get('order_post_result') or {}
            status2 = str(post2.get('status') or '').lower()
            close_debug.append({
                'ts': ts_utc(),
                'attempt': i + 1,
                'order_type': 'GTC',
                'status': status2,
                'close_skipped': str(close_obj2.get('close_skipped') or ''),
                'limit_price': limit_px,
            })
            close_obj = close_obj2
            out = out2
            if post2.get('success') is True and status2 == 'matched':
                break
            # Anything other than a confirmed match may have left a resting order.
            resting_gtc = True

            # If GTC is accepted but still live, force-close flow: poll status, cancel, repost aggressive.
            if post2.get('success') is True and status2 == 'live':
                oid2 = str(post2.get('orderID') or '')
                st_upd, ord_upd = poll_order_status(client, oid2, wait_sec=min(8.0, max(2.0, float(args.close_retry_delay_sec) * 2)), step_sec=1.0)
                close_debug.append({
                    'ts': ts_utc(),
                    'attempt': i + 1,
                    'order_type': 'GTC_POLL',
                    'status': st_upd.lower() if st_upd else '',
                    'order_id': oid2,
                })
                if st_upd == 'MATCHED':
                    post2['status'] = 'matched'
                    close_obj['order_post_result'] = post2
                    break

                cancel_info = cancel_token_orders(client, opened['token_id'])
                bb2 = None
                try:
                    bb2 = clob_best_bid(opened['token_id'])
                except Exception:
                    bb2 = None
                if bb2 is None:
                    bb2 = report.get('last_clob_bid')
                force_px = salvage_limit_price(bb2, gamma_last)
                if force_px is None:
                    close_debug.append({
                        'ts': ts_utc(),
                        'attempt': i + 1,
                        'order_type': 'SKIP_FORCE_DEAD_BOOK',
                        'status': 'no_salvage_bid',
                    })
                    time.sleep(float(args.close_retry_delay_sec))
                    continue
                force_close_used = {
                    'type': 'FORCE_GTC_LIMIT',
                    'price': force_px,
                    'cancel_info': cancel_info,
                }
                out3, objs3 = run_close(
                    args.repo,
                    opened['market_slug'],
                    opened['token_id'],
                    opened['shares'],
                    args.execute,
                    close_order_type='GTC',
                    close_limit_price=force_px,
                )
                close_obj3 = objs3[-1] if objs3 else {}
                post3 = close_obj3.get('order_post_result') or {}
                status3 = str(post3.get('status') or '').lower()
                close_debug.append({
                    'ts': ts_utc(),
                    'attempt': i + 1,
                    'order_type': 'FORCE_GTC',
                    'status': status3,
                    'close_skipped': str(close_obj3.get('close_skipped') or ''),
                    'limit_price': force_px,
                })
                close_obj = close_obj3
                out = out3
                if post3.get('success') is True and status3 == 'matched':
                    break
                resting_gtc = True

        time.sleep(float(args.close_retry_delay_sec))

    post = close_obj.get('order_post_result') or {}
    post_status = str(post.get('status') or '').lower()
    close_usdc = float(post.get('takingAmount') or 0)
    if resting_gtc and args.execute and not (post.get('success') is True and (post_status == 'matched' or close_usdc > 0)):
        report['close_cancel_final'] = cancel_token_orders(client, opened['token_id'])
    closed = {
        'close_reason': close_reason,
        'closed_at': ts_utc(),
        'close_success': bool(post.get('success') is True and (post_status == 'matched' or close_usdc > 0)),
        'close_status': post.get('status'),
        'close_order_id': post.get('orderID'),
        'close_tx': (post.get('transactionsHashes') or [None])[0],
        'close_shares': float(post.get('makingAmount') or 0),
        'close_usdc': close_usdc,
        'close_skipped': close_obj.get('close_skipped'),
        'hedge': report.get('hedge'),
    }
    report['close_debug'] = close_debug
    if fallback_used:
        report['close_fallback'] = fallback_used
    if force_close_used:
        report['close_force'] = force_close_used
    report['close_raw'] = out[-4000:]
    report['closed'] = closed

    pnl = None
    if closed['close_usdc']:
        pnl = round(closed['close_usdc'] - opened['cost_usdc'], 6)
    report['realized_cashflow_pnl_usdc'] = pnl
    report['finished_at'] = ts_utc()
    report['result'] = 'done'

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
