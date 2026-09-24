import math
import random
import unittest

from bot.book import Book
from bot.config import Risk, Strategy
from bot.model import EwmaVol, SecondBars, fair_value, taker_fee_per_share
from bot.paper import Portfolio, credit_rebates, rebate_rate, settle, simulate_taker_fill, Position
from bot.risk import RiskManager
from bot.strategy import evaluate, max_limit_for_edge


class TestFees(unittest.TestCase):
    def test_fee_formula(self):
        self.assertAlmostEqual(taker_fee_per_share(0.5) * 100, 1.75)
        self.assertAlmostEqual(taker_fee_per_share(0.2) * 100, 1.12)

    def test_rebate_tiers(self):
        self.assertEqual(rebate_rate(1_000), 0.0)
        self.assertEqual(rebate_rate(2_000), 0.03)
        self.assertEqual(rebate_rate(9_190_000), 0.44)   # wallet 0xb55fa129 — observed 44%
        self.assertEqual(rebate_rate(12_000_000), 0.50)


class TestFairValue(unittest.TestCase):
    def bars(self, secs, value):
        b = SecondBars()
        for s in secs:
            b.add(s, value)
        return b

    def test_at_the_money_is_half(self):
        b = self.bars(range(900, 1001), 100.0)
        fv = fair_value(reference=100.0, x_now=100.0, now_sec=1000, end_sec=1300, lookback=60, realized=b, sigma=0.5, basis_sigma=0)
        self.assertAlmostEqual(fv.p_up, 0.5, places=6)

    def test_locked_in_late(self):
        # 55 of 60 final seconds already printed well above the reference → near-certain Up
        b = self.bars(range(1000, 1296), 101.0)
        fv = fair_value(reference=100.0, x_now=101.0, now_sec=1295, end_sec=1300, lookback=60, realized=b, sigma=0.5, basis_sigma=0.5)
        self.assertGreater(fv.p_up, 0.999)

    def test_variance_matches_monte_carlo(self):
        # 150 s left: 90 s gap then the full 60 s averaging window; compare with simulation
        sigma, L, gap = 0.4, 60, 90
        b = self.bars(range(1000, 1151), 100.0)
        fv = fair_value(reference=100.3, x_now=100.0, now_sec=1150, end_sec=1300, lookback=L, realized=b, sigma=sigma, basis_sigma=0)
        rng = random.Random(7)
        n, ups = 20000, 0
        for _ in range(n):
            x = 100.0
            for _ in range(gap):
                x += rng.gauss(0, sigma)
            tot = 0.0
            for _ in range(L):
                x += rng.gauss(0, sigma)
                tot += x
            ups += (tot / L) >= 100.3
        self.assertAlmostEqual(fv.p_up, ups / n, delta=0.015)


class TestVol(unittest.TestCase):
    def test_thirty_second_changes_recover_random_walk_sigma(self):
        rng = random.Random(3)
        v = EwmaVol(halflife_s=600, floor_bp=0.0, change_s=30, prior_bp=0.0, warmup=0)
        x = 80000.0
        for s in range(20000):
            x += rng.gauss(0, 4.0)
            v.update(s, x)
        self.assertAlmostEqual(v.sigma(x), 4.0, delta=0.6)

    def test_prior_during_warmup(self):
        v = EwmaVol(halflife_s=600, floor_bp=0.1, change_s=30, prior_bp=0.5, warmup=300)
        for s in range(100):
            v.update(s, 80000.0)                                  # flat → tiny realized vol
        self.assertAlmostEqual(v.sigma(80000.0), 4.0)             # 0.5 bp × 80k


class TestPaperFill(unittest.TestCase):
    ASKS = [(0.50, 20.0), (0.51, 40.0), (0.53, 100.0)]

    def test_walks_book_with_haircut_and_limit(self):
        sh, notional, fee, lv = simulate_taker_fill(self.ASKS, limit=0.51, max_shares=100, haircut=0.5,
                                                    fee_rate=0.07, cash_available=1000, min_shares=5)
        self.assertAlmostEqual(sh, 30.0)                     # 10 @0.50 + 20 @0.51, 0.53 is above limit
        self.assertAlmostEqual(notional, 10 * 0.50 + 20 * 0.51)
        self.assertAlmostEqual(fee, 10 * taker_fee_per_share(0.50) + 20 * taker_fee_per_share(0.51))

    def test_below_minimum_is_rejected(self):
        sh, *_ = simulate_taker_fill([(0.5, 6.0)], limit=0.5, max_shares=100, haircut=0.5, fee_rate=0.07,
                                     cash_available=1000, min_shares=5)
        self.assertEqual(sh, 0.0)

    def test_cash_limits_size(self):
        sh, notional, fee, _ = simulate_taker_fill(self.ASKS, limit=0.6, max_shares=1000, haircut=1.0, fee_rate=0.07,
                                                   cash_available=10.0, min_shares=5)
        self.assertLessEqual(notional + fee, 10.0 + 1e-9)


class TestSettlementAndRebates(unittest.TestCase):
    def test_settle_pays_winning_shares(self):
        pf = Portfolio(cash=100, starting_equity=200, peak_equity=200)
        pf.positions["c"] = Position("s", "c", 0, up_shares=100, down_shares=40, cost=100)
        r = settle(pf, "c", "Up")
        self.assertAlmostEqual(r["pnl"], 0.0)
        self.assertAlmostEqual(pf.cash, 200.0)

    def test_rebate_paid_next_day(self):
        pf = Portfolio(cash=0, starting_equity=200, peak_equity=200)
        pf.fees_by_day["2026-09-22"] = 10.0
        pf.wv_by_day["2026-09-22"] = 250_000        # Gold tier (18%)
        r = credit_rebates(pf, 1790121600 + 60, True)  # 2026-09-23 00:01 UTC
        self.assertAlmostEqual(r["rebate"], 1.8)
        self.assertIsNone(credit_rebates(pf, 1790121600 + 120, True))  # only once


class TestRiskAndStrategy(unittest.TestCase):
    def test_clip_scales_with_equity(self):
        pf = Portfolio(cash=200, starting_equity=200, peak_equity=200)
        rm = RiskManager(Risk(), pf)
        self.assertAlmostEqual(rm.clip_shares("c", 0.5, 5), 20.0)          # 5% of 200 = $10 → 20 shares
        pf.cash = 2000
        self.assertAlmostEqual(rm.clip_shares("c", 0.5, 5), 200.0)

    def test_drawdown_kill(self):
        pf = Portfolio(cash=200, starting_equity=200, peak_equity=200, day_start_equity=200)
        rm = RiskManager(Risk(), pf)
        pf.cash = 120
        self.assertIn("drawdown", rm.check_breakers())

    def test_limit_respects_edge(self):
        lim = max_limit_for_edge(0.70, 0.02, 0.07, 0.01)
        self.assertGreaterEqual(0.70 - lim - taker_fee_per_share(lim), 0.02 - 1e-12)

    def _book(self, ask, now):
        b = Book(); b.asks = {ask: 100.0}; b.bids = {round(ask - 0.01, 2): 100.0}; b.updated = now
        return b

    def test_needs_momentum_and_edge(self):
        now = 1000.0
        books = {"Up": self._book(0.55, now), "Down": self._book(0.47, now)}
        kw = dict(books=books, now=now, seconds_left=120, seconds_elapsed=180, fee_rate=0.07, tick=0.01, max_slippage=0.02)
        i, _ = evaluate(Strategy(), p_up=0.65, momentum_bp=1.0, **kw)
        self.assertEqual(i.outcome, "Up")                                   # momentum up + 6.3c edge
        i, _ = evaluate(Strategy(), p_up=0.65, momentum_bp=0.1, **kw)
        self.assertIsNone(i)                                                # no momentum → no trade
        i, rej = evaluate(Strategy(), p_up=0.56, momentum_bp=1.0, **kw)
        self.assertIsNone(i)                                                # momentum but no edge after fee
        self.assertEqual(rej[0].reason, "edge_below_min")


if __name__ == "__main__":
    unittest.main()
