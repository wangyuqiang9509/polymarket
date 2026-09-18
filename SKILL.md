---
name: btc-5m-live
description: Run and monitor BTC 5-minute Up/Down trading on Polymarket using momentum-near-close logic (time-left, BTC move, market skew), fixed/controlled sizing, optional micro-hedge, and one-shot or loop execution.
---

# BTC 5m Live

## Paths
- Main trading repo: `<your-workspace>/pm-hl-conservative-plus-repo` (or set `BTC5M_REPO`)
- Core runner: `src/live/pm_live_trade_runner.py`
- Canonical skill runner: `scripts/test_btc_5m_session_exit_sl.py`
- Skill control entrypoint: `scripts/btc5m_ctl.sh`
- Compatibility wrapper (deprecated): `scripts/run_btc_5m_threshold_test.py`

## Strategy Alignment
Use this skill when the operator wants to execute a BTC 5m momentum strategy:
- Entry focus near event close (around 2 minutes left, 90-150s window).
- Confirm meaningful BTC move in the interval (about $70-$100).
- Prefer direction supported by market skew.
- Enter with momentum, not against it.
- Optional small opposite hedge when skew becomes extreme.

## Operational Rules
- Default is dry-run unless `--execute` is set.
- Fixed `$5` stake. Ladder / martingale is off unless `BTC5M_MARTINGALE=1`.
- Enter only when about **120 seconds** remain (**90-150s**). Skip earlier and later. Stronger-side CLOB ask must be `>= 0.70`.
- If both UP and DOWN satisfy threshold logic, choose the stronger side.
- Skip if that ask is `>= 0.85`. After 3 consecutive wins, also skip ask `>= 0.80`. Do not buy the cheap side instead.
- On close, do not dump at 1 cent when the book is dead; GTC only if a bid `>= 0.05` is still there.
- Primary stop: **CLOB best bid** vs entry × 0.75, from the salvage floor (`0.05`) up. Bid `>= 0.45` fires alone; below that the same book's best ask (or Gamma) must be `<= 0.50`. A real loser jumps 0.6 → 0.3 between polls, so the band reaches the floor.
- Every CLOB-driven cut must be seen on **2 consecutive polls** (`--confirm-polls`), unless the same book's best ask is already `<= 0.50`: then the whole book says lost and the cut fires on the first poll. Trade prints show winner wicks last 0–2s; losers take 15–45s to collapse.
- Poll every **1s** inside the last **90s** (`--fast-poll-sec`, `--fast-poll-window-sec`).
- Gamma 25% stop is **off** by default (`--gamma-sl` enables it). Live it sold 16 winners at 0.70–0.95 and saved 5 losers: net negative.
- If our book is dead on close (FAK unmatched / 404), **sell through the opposite book**: buy the same share count of the other token when its ask is `<= 0.95` (`--hedge-max-ask`, `--no-hedge`). Same payoff as selling ours at 1 − ask; the winner's book always has depth. Extra collateral is tied up until settlement; the journal nets it.
- CLOB wick floor stays: FAK-sell if bid is `0.05–0.40` **and** the same book's best ask is `<= 0.50` (Gamma `<= 0.50` is the fallback confirmation). A pulled bid with the ask still high is a fakeout; Gamma alone lags and left losers unsold.
- Once a GTC salvage order has been posted, cancel resting orders on the token before every FAK retry. A resting GTC reserves the shares and every later FAK reports `zero_effective_shares`.
- At **45s** left, sell if CLOB bid is live but `< 0.55` (`time_exit_45s_not_winning`). Do not flatten 0.70–0.89 names that can still run.
- At **20s** left, hold if bid/Gamma `>= 0.90`; otherwise try to sell. Last 20s is often a 404 on the loser.

## One-shot real test
From trading repo root:

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --execute
```

Aggressive profile:

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile aggressive --execute
```

Override profile params manually (example):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --stake-usd 5 --entry-timeout-min 90 --execute
```

## Strategy Profiles
- File: `config/btc_5m_profiles.yaml`
- Presets: `conservative`, `aggressive`
- Includes entry/exit timing, quote staleness checks, spread/liquidity guards, hedge triggers, and risk caps.

## Hot Commands (chat-friendly)
Examples:
- `btc5m conservative start`
- `btc5m aggressive start`

Handlers:
- `scripts/btc5m_hot.sh [conservative|aggressive]`
- `scripts/btc5m_ctl.sh start --profile [conservative|aggressive]`
- `scripts/btc5m_ctl.sh status|stop|report|logs`
- completion summary utility: `scripts/btc5m_latest_report.py --mark`

Output:
- isolated skill runtime logs: `skills/btc-5m-live/runtime/btc5m_<profile>_<UTCSTAMP>.log`

## Notes
- Canonical runner resolves current BTC 5m market slug (`btc-updown-5m-<bucket>`).
- Real order placement is delegated to `pm_live_trade_runner.py` with `--force-side` and `--max-notional-usd`.
- Keep BTC5m automation scoped to this skill contour (`btc5m_ctl.sh` + `skills/btc-5m-live/runtime`) to avoid cross-skill interference.
- Keep all GitHub-facing docs and metadata in English.
