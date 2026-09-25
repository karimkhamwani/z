"""Regression tests for the fixes that came out of the first live session (Sep 24, 19:40–19:55)."""
import asyncio
import time
import unittest
from types import SimpleNamespace as NS

from bot.book import Book
from bot.config import Strategy
from bot.feeds import AssetPrices
from bot.live import LiveAccount, LiveExecutor, is_no_match
from bot.model import EwmaVol, SecondBars, fair_value, taker_fee_per_share
from bot.paper import Portfolio
from bot.secrets import LiveCreds
from bot.strategy import evaluate
from tests.test_live import CTX, KEY, MARKET, FakeClient, FakeLedger


class RequestRejectedError(Exception):   # same class name the SDK raises
    pass


def book(ask, now):
    b = Book(); b.asks = {ask: 100.0}; b.bids = {round(ask - 0.01, 2): 100.0}; b.updated = now
    return b


class TestVolatilityGate(unittest.TestCase):
    def test_not_ready_until_measured(self):
        v = EwmaVol(halflife_s=600, floor_bp=0.2, change_s=30, prior_bp=0.5, warmup=300)
        x = 84400.0
        for s in range(200):
            v.update(s, x + (s % 7))
        self.assertFalse(v.ready)                                    # 170 samples < 300
        for s in range(200, 400):
            v.update(s, x + (s % 7))
        self.assertTrue(v.ready)

    def test_state_survives_restart(self):
        v = EwmaVol(600, 0.2, 30, 0.5, 300)
        for s in range(400):
            v.update(s, 84400.0 + (s % 11))
        w = EwmaVol(600, 0.2, 30, 0.5, 300)
        w.restore(v.state())
        self.assertTrue(w.ready)
        self.assertAlmostEqual(w.sigma(84400.0), v.sigma(84400.0))

    def test_first_live_loss_is_no_longer_a_trade(self):
        """19:40:37 Up @0.28 ask: start price 84429.31, Chainlink ~84410.5, 262 s left. With the default
        sigma ($4.22) the model said 0.407 (edge +12c) and bought; Down won. At the measured $1.69 it isn't a trade."""
        ref, x, now_sec, end = 84429.31, 84410.48, 1790293238, 1790293500
        p_default = fair_value(reference=ref, x_now=x, now_sec=now_sec, end_sec=end, lookback=60, realized=SecondBars(),
                               sigma=4.22, basis_sigma=4.0).p_up
        p_measured = fair_value(reference=ref, x_now=x, now_sec=now_sec, end_sec=end, lookback=60, realized=SecondBars(),
                                sigma=1.69, basis_sigma=4.0).p_up
        self.assertGreater(p_default, 0.37)
        self.assertLess(p_measured, 0.26)                            # market had it at ~0.255
        books = {"Up": book(0.28, 1000.0), "Down": book(0.74, 1000.0)}
        intent, _ = evaluate(Strategy(), p_up=p_measured, momentum_bp=0.55, books=books, now=1000.0, seconds_left=262,
                             seconds_elapsed=38, fee_rate=0.07, tick=0.01, max_slippage=0.02)
        self.assertIsNone(intent)


class TestSpikeCap(unittest.TestCase):
    def test_coinbase_spike_is_capped(self):
        """19:54:01: Coinbase fell ~$23 in 3 s while Chainlink (84393.65) barely moved; start price 84373.96.
        Uncapped, the estimate crossed below the start price and the bot bought Down @0.46 (Up won)."""
        p = AssetPrices("btc", 600, 0.2)
        now = 1790294041.0
        cl_sec = int(now) - 2
        p.chainlink.add(cl_sec, 84393.65)
        p.spot.add(cl_sec + 0.5, 84402.5)
        p.spot.add(now - 0.1, 84379.4)
        uncapped = p.estimate_now(now)
        cap = 3.0 * 1.69 * (2 + 1) ** 0.5
        capped = p.estimate_now(now, max_adjust=cap)
        self.assertLess(uncapped, 84373.96 + 1)
        self.assertGreater(capped, 84373.96 + 5)


class TestStrategyGuards(unittest.TestCase):
    def kw(self, up_ask):
        now = 1000.0
        return dict(books={"Up": book(up_ask, now), "Down": book(round(1 - up_ask + 0.01, 2), now)}, now=now,
                    seconds_left=120, seconds_elapsed=180, fee_rate=0.07, tick=0.01, max_slippage=0.02)

    def test_edge_too_large_is_skipped(self):
        intent, rej = evaluate(Strategy(max_edge=0.12), p_up=0.60, momentum_bp=1.0, **self.kw(0.40))
        self.assertIsNone(intent)
        self.assertEqual(rej[0].reason, "edge_too_large")

    def test_momentum_spike_is_skipped(self):
        intent, rej = evaluate(Strategy(max_momentum_bp=5.0), p_up=0.60, momentum_bp=7.0, **self.kw(0.52))
        self.assertIsNone(intent)
        self.assertEqual(rej[0].reason, "momentum_spike")

    def test_normal_signal_still_trades(self):
        intent, _ = evaluate(Strategy(), p_up=0.60, momentum_bp=1.0, **self.kw(0.52))
        self.assertEqual(intent.outcome, "Up")


class TestNoMatchIsNotAnError(unittest.TestCase):
    def make(self, exc):
        client = FakeClient([exc] * 6, activity=["should-not-be-read"])
        pf = Portfolio(cash=100, starting_equity=100, peak_equity=100)
        acct = LiveAccount(LiveCreds(KEY, "0x" + "22" * 20, 1), client=client, wallet="0x" + "22" * 20)
        led = FakeLedger()
        ex = LiveExecutor(acct, pf, led, order_timeout_s=1, max_consecutive_errors=5, kill_switch=lambda: False)
        return ex, led

    def buy(self, ex):
        t0 = time.time()
        r = asyncio.run(ex.buy(market=MARKET, outcome="Up", limit=0.4, max_shares=5, get_book=None, context=CTX))
        return r, time.time() - t0

    def test_fak_no_match_exception(self):
        e = RequestRejectedError("no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.")
        self.assertTrue(is_no_match(e))
        ex, led = self.make(e)
        for _ in range(6):                                            # six in a row used to halt the bot
            fill, took = self.buy(ex)
            self.assertIsNone(fill)
            self.assertLess(took, 1.0)                                 # no 3 s recovery check any more
        self.assertEqual(ex.errors, 0)
        self.assertEqual(led.orders[-1]["code"], "fak_not_filled")

    def test_other_rejection_counts_but_skips_recovery(self):
        ex, led = self.make(RequestRejectedError("invalid order: price out of range"))
        fill, took = self.buy(ex)
        self.assertIsNone(fill)
        self.assertEqual(ex.errors, 1)
        self.assertLess(took, 1.0)


class TestPrewarm(unittest.TestCase):
    def test_prewarm_loads_each_token(self):
        seen = []

        async def resolve_market(ctx, token_id):
            seen.append(token_id)
        client = NS(_ctx=NS(order_metadata=NS(resolve_market=resolve_market)))
        acct = LiveAccount(LiveCreds(KEY, "0x" + "22" * 20, 1), client=client)
        asyncio.run(acct.prewarm(["UP", "DOWN"]))
        self.assertEqual(seen, ["UP", "DOWN"])

    def test_prewarm_never_raises(self):
        acct = LiveAccount(LiveCreds(KEY, "0x" + "22" * 20, 1), client=NS())   # SDK internals missing
        asyncio.run(acct.prewarm(["UP"]))


if __name__ == "__main__":
    unittest.main()
