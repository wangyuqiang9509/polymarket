#!/usr/bin/env python3
"""Live-sample rules: flat $5, skip expensive asks, salvage unmatched closes."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import btc5m_rules as rules  # noqa: E402


class ChooseSideTests(unittest.TestCase):
    def test_picks_stronger_side_in_070_075_zone(self):
        side, px, reason = rules.choose_side(0.74, 0.71, threshold=0.70, max_ask=0.85, win_streak=0)
        self.assertEqual((side, px, reason), ('UP', 0.74, None))

    def test_skips_when_both_below_threshold(self):
        side, px, reason = rules.choose_side(0.65, 0.62, threshold=0.70, max_ask=0.85, win_streak=0)
        self.assertEqual(reason, 'skip_price_below_threshold')
        self.assertIsNone(side)

    def test_skips_ask_at_or_above_max(self):
        side, px, reason = rules.choose_side(0.86, 0.40, threshold=0.70, max_ask=0.85, win_streak=0)
        self.assertEqual(reason, 'skip_ask_too_expensive')
        self.assertEqual(px, 0.86)
        self.assertIsNone(side)

    def test_does_not_fade_into_weaker_side_when_stronger_is_expensive(self):
        # UP 0.90 / DOWN 0.72: do not buy DOWN (that would reverse momentum).
        side, px, reason = rules.choose_side(0.90, 0.72, threshold=0.70, max_ask=0.85, win_streak=0)
        self.assertEqual(reason, 'skip_ask_too_expensive')
        self.assertIsNone(side)

    def test_skips_expensive_after_three_wins(self):
        side, px, reason = rules.choose_side(
            0.82, 0.20, threshold=0.70, max_ask=0.85, win_streak=3,
            expensive_after_wins=3, expensive_ask=0.80,
        )
        self.assertEqual(reason, 'skip_expensive_after_wins')
        self.assertIsNone(side)

    def test_allows_070_zone_even_after_win_streak(self):
        side, px, reason = rules.choose_side(
            0.74, 0.20, threshold=0.70, max_ask=0.85, win_streak=5,
            expensive_after_wins=3, expensive_ask=0.80,
        )
        self.assertEqual((side, px, reason), ('UP', 0.74, None))

    def test_allows_082_when_streak_below_cap(self):
        side, px, reason = rules.choose_side(
            0.82, 0.20, threshold=0.70, max_ask=0.85, win_streak=2,
            expensive_after_wins=3, expensive_ask=0.80,
        )
        self.assertEqual((side, px, reason), ('UP', 0.82, None))


class EntryWindowTests(unittest.TestCase):
    def test_accepts_about_two_minutes_left(self):
        ok, reason = rules.in_entry_window(120)
        self.assertEqual((ok, reason), (True, None))
        ok, reason = rules.in_entry_window(90)
        self.assertEqual((ok, reason), (True, None))
        ok, reason = rules.in_entry_window(150)
        self.assertEqual((ok, reason), (True, None))

    def test_skips_too_early(self):
        ok, reason = rules.in_entry_window(180)
        self.assertEqual((ok, reason), (False, 'skip_too_early_to_enter'))

    def test_skips_too_late(self):
        ok, reason = rules.in_entry_window(60)
        self.assertEqual((ok, reason), (False, 'skip_too_late_to_enter'))

    def test_skips_missing_clock(self):
        ok, reason = rules.in_entry_window(None)
        self.assertEqual((ok, reason), (False, 'heartbeat_bad_market_end'))


class WinStreakTests(unittest.TestCase):
    def test_counts_trailing_wins_and_ignores_scratch(self):
        trades = [
            {'realized_pnl_usdc': 1.5, 'stake_usd': 5},
            {'realized_pnl_usdc': 0.1, 'stake_usd': 5},
            {'realized_pnl_usdc': 1.2, 'stake_usd': 5},
        ]
        self.assertEqual(rules.consecutive_wins(trades), 2)

    def test_resets_after_loss(self):
        trades = [
            {'realized_pnl_usdc': 1.5, 'stake_usd': 5},
            {'hold_to_settlement_pnl_usdc': -5.0, 'stake_usd': 5},
            {'realized_pnl_usdc': 1.2, 'stake_usd': 5},
        ]
        self.assertEqual(rules.consecutive_wins(trades), 1)

    def test_empty_is_zero(self):
        self.assertEqual(rules.consecutive_wins([]), 0)


class CloseSalvageTests(unittest.TestCase):
    def test_404_no_book_is_unmatched(self):
        txt = "PolyApiException[status_code=404, error_message={'error': 'No orderbook exists for the requested token id'}]"
        self.assertTrue(rules.fak_unmatched(txt))

    def test_no_match_fak_is_unmatched(self):
        self.assertTrue(rules.fak_unmatched('No orders found to match with FAK order'))

    def test_matched_is_not_unmatched(self):
        self.assertFalse(rules.fak_unmatched('status matched success true'))

    def test_salvage_uses_clob_bid_when_alive(self):
        px = rules.salvage_limit_price(best_bid=0.62, gamma_last=0.40, min_bid=0.05)
        self.assertAlmostEqual(px, 0.61)

    def test_salvage_falls_back_to_gamma_last(self):
        px = rules.salvage_limit_price(best_bid=None, gamma_last=0.55, min_bid=0.05)
        self.assertAlmostEqual(px, 0.54)

    def test_no_salvage_when_book_is_dead(self):
        self.assertIsNone(rules.salvage_limit_price(best_bid=0.01, gamma_last=0.02, min_bid=0.05))


class HoldToSettlementTests(unittest.TestCase):
    def test_hold_when_bid_still_winning(self):
        self.assertTrue(rules.should_hold_to_settlement(best_bid=0.99, gamma_last=0.50))

    def test_hold_uses_gamma_if_no_clob_bid(self):
        self.assertTrue(rules.should_hold_to_settlement(best_bid=None, gamma_last=0.96))

    def test_sell_attempt_when_losing_book(self):
        self.assertFalse(rules.should_hold_to_settlement(best_bid=None, gamma_last=0.40))

    def test_dead_book_does_not_hold(self):
        self.assertFalse(rules.should_hold_to_settlement(best_bid=None, gamma_last=None))


class ClobBidFloorTests(unittest.TestCase):
    def test_does_not_cut_clob_wick_while_gamma_still_winning(self):
        # Live 2026-09-17 floor cuts: CLOB 0.28–0.37, Gamma still 0.73–0.96, settlement won.
        self.assertFalse(rules.should_cut_on_clob_bid(0.37, gamma_last=0.735))
        self.assertFalse(rules.should_cut_on_clob_bid(0.33, gamma_last=0.965))
        self.assertFalse(rules.should_cut_on_clob_bid(0.28, gamma_last=0.845))

    def test_cuts_only_when_clob_and_gamma_both_confirm_loss(self):
        self.assertTrue(rules.should_cut_on_clob_bid(0.35, gamma_last=0.40))
        self.assertTrue(rules.should_cut_on_clob_bid(0.40, gamma_last=0.50))

    def test_does_not_cut_winning_book(self):
        self.assertFalse(rules.should_cut_on_clob_bid(0.99, gamma_last=0.40))
        self.assertFalse(rules.should_cut_on_clob_bid(0.41, gamma_last=0.40))

    def test_does_not_cut_dead_penny_book(self):
        self.assertFalse(rules.should_cut_on_clob_bid(0.01, gamma_last=0.20))
        self.assertFalse(rules.should_cut_on_clob_bid(0.04, gamma_last=0.20))

    def test_does_not_cut_on_missing_clob_or_missing_gamma(self):
        self.assertFalse(rules.should_cut_on_clob_bid(None, gamma_last=0.20))
        self.assertFalse(rules.should_cut_on_clob_bid(0.35, gamma_last=None))


class ClobStopTests(unittest.TestCase):
    def test_cuts_when_bid_fails_25pct_but_book_still_liquid(self):
        # entry 0.80 → SL 0.60; bid 0.55 is sellable and below SL.
        self.assertTrue(rules.should_cut_on_clob_stop(0.55, 0.60))

    def test_does_not_cut_while_clob_still_strong(self):
        # 04:42: CLOB bid 0.77, SL 0.615 — CLOB stop stays off; Gamma did the work.
        self.assertFalse(rules.should_cut_on_clob_stop(0.77, 0.615))

    def test_ignores_wick_zone_below_045(self):
        self.assertFalse(rules.should_cut_on_clob_stop(0.37, 0.60))
        self.assertFalse(rules.should_cut_on_clob_stop(0.28, 0.60))

    def test_ignores_dead_penny_and_missing_bid(self):
        self.assertFalse(rules.should_cut_on_clob_stop(0.01, 0.60))
        self.assertFalse(rules.should_cut_on_clob_stop(None, 0.60))


class GammaStopTests(unittest.TestCase):
    def test_04_42_gamma_leads_while_clob_still_has_bid(self):
        self.assertTrue(rules.should_cut_on_gamma_stop(0.495, 0.615, best_bid=0.77))

    def test_does_not_fire_when_book_is_dead(self):
        self.assertFalse(rules.should_cut_on_gamma_stop(0.495, 0.615, best_bid=None))
        self.assertFalse(rules.should_cut_on_gamma_stop(0.495, 0.615, best_bid=0.01))

    def test_05_40_gamma_never_crossed_sl(self):
        self.assertFalse(rules.should_cut_on_gamma_stop(0.855, 0.51, best_bid=0.50))


class EarlyFlattenTests(unittest.TestCase):
    def test_flattens_live_loser_before_dead_zone(self):
        self.assertTrue(rules.should_flatten_before_dead_book(0.40))
        self.assertTrue(rules.should_flatten_before_dead_book(0.50))

    def test_does_not_flatten_developing_or_winning_book(self):
        self.assertFalse(rules.should_flatten_before_dead_book(0.70))
        self.assertFalse(rules.should_flatten_before_dead_book(0.99))

    def test_does_not_flatten_unsellable_book(self):
        self.assertFalse(rules.should_flatten_before_dead_book(0.01))
        self.assertFalse(rules.should_flatten_before_dead_book(None))


if __name__ == '__main__':
    unittest.main()
