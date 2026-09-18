# Agent instructions

This is a **standalone Polymarket BTC 5m Up/Down bot**. Global memories, other repos, and other knowledge graphs do not apply here.

Read `SKILL.md` and `CONTOUR.md` before changing strategy, runners, or risk. The always-on isolation rule is `.cursor/rules/isolate-global-memory.mdc`.

## Learned User Preferences

- Isolate this workspace from global/user memories; treat only this repo and the current chat as context.

## Learned Workspace Facts

- Skill name: `btc-5m-live`. Strategy is momentum into close (~2 minutes left), not mean reversion.
- Canonical runner: `scripts/test_btc_5m_session_exit_sl.py`. Control entry: `scripts/btc5m_ctl.sh`.
- Live execution is delegated to `pm-hl-conservative-plus-repo`; this repo owns skill contour, profiles, and wrappers.
- Sizing is fixed $5. Martingale/ladder is off by default.
