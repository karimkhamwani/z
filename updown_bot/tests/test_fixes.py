"""Regression tests for the fixes that came out of the first live session (Sep 24, 19:40–19:55)."""
import asyncio
import json
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


class TestVolBiasCorrection(unittest.TestCase):
    def test_one_large_first_change_does_not_dominate(self):
        """Sep 24 session 2: the connection snapshot's first 30 s change was large; the old estimator still
        reported $6-8/s after warm-up while Chainlink moved ~$3.5/s."""
        import random
        rng = random.Random(5)
        v = EwmaVol(halflife_s=600, floor_bp=0.0, change_s=30, prior_bp=0.0, warmup=300)
        alpha, old_var, hist = v.alpha, None, {}      # the previous estimator, replayed on the same data

        def feed(sec, price):
            nonlocal old_var
            v.update(sec, price)
            hist[sec] = price
            past = next((hist[sec - 30 - d] for d in range(3) if sec - 30 - d in hist), None)
            if past is not None:
                d2 = (price - past) ** 2 / 30
                old_var = d2 if old_var is None else (1 - alpha) * old_var + alpha * d2

        x = 84400.0
        feed(0, x)
        x += 250.0
        feed(30, x)                         # one huge first change (≈ $45/s over 30 s)
        for s in range(31, 31 + 330):       # then a steady $3.5/s random walk
            x += rng.gauss(0, 3.5)
            feed(s, x)
        self.assertTrue(v.ready)
        old_sigma = old_var ** 0.5
        self.assertGreater(old_sigma, 25)            # the old estimator is still dominated by the first sample
        self.assertLess(v.sigma(x), 6.5)             # now: one outlier counts ~1/300, close to the true $3.5
        self.assertGreater(v.sigma(x), 2.5)


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


class TestFalseDrawdownHalt(unittest.TestCase):
    """Sep 24 22:28: 'drawdown kill: equity 39.62 is 35% below peak 61.78'. The 61.78 peak came from 21:36:07,
    when a $10 payout was already in cash (51.78) and the same claim was still listed as a position (10.00)."""

    def test_claimed_payout_cannot_inflate_the_peak(self):
        from bot.config import Risk
        from bot.risk import RiskManager
        pf = Portfolio(cash=51.78, starting_equity=53.67, peak_equity=53.67, day_start_equity=53.67)
        pf.positions_value, pf.claimable_value = 0.0, 10.00      # the double-counted moment
        rm = RiskManager(Risk(daily_loss_stop_pct=0.5, max_drawdown_kill_pct=0.35), pf)
        rm.check_breakers()
        self.assertAlmostEqual(pf.peak_equity, 53.67)             # not 61.78
        pf.cash, pf.positions_value, pf.claimable_value = 38.69, 0.93, 0.0
        self.assertEqual(rm.check_breakers(), "")                 # 39.62 > 0.65 × 57.89 real peak → no halt

    def test_claim_in_transit_cannot_trigger_a_stop(self):
        from bot.config import Risk
        from bot.risk import RiskManager
        pf = Portfolio(cash=30.0, starting_equity=53.67, peak_equity=50.0, day_start_equity=53.67)
        pf.positions_value, pf.claimable_value = 0.0, 20.0        # payout resolved, not yet in cash
        self.assertEqual(RiskManager(Risk(daily_loss_stop_pct=0.5, max_drawdown_kill_pct=0.35), pf).check_breakers(), "")


class TestCounterTrend(unittest.TestCase):
    def kw(self, trend):
        now = 1000.0
        return dict(books={"Up": book(0.40, now), "Down": book(0.61, now)}, now=now, seconds_left=150,
                    seconds_elapsed=150, fee_rate=0.07, tick=0.01, max_slippage=0.05, trend_bp=trend)

    def test_uptick_inside_a_downtrend_is_skipped(self):
        intent, rej = evaluate(Strategy(), p_up=0.47, momentum_bp=0.8, **self.kw(-2.0))   # 60 s: −2 bp
        self.assertIsNone(intent)
        self.assertEqual(rej[0].reason, "counter_trend")

    def test_with_trend_or_flat_still_trades(self):
        for trend in (+2.0, 0.0, -1.0):
            intent, _ = evaluate(Strategy(), p_up=0.47, momentum_bp=0.8, **self.kw(trend))
            self.assertEqual(intent.outcome, "Up", trend)


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


class TestFillNotYetListed(unittest.TestCase):
    """Sep 24 23:40:20: a $4.20 fill left cash at $24.40 while Polymarket's positions list didn't show the new
    position yet, so equity read $24.40 (real: $28.60) and tripped the 35% drawdown kill from the $38.89 peak."""

    def engine(self, pos_end):
        from bot.engine import Engine
        from bot.paper import Position
        eng = Engine.__new__(Engine)
        eng.pf = Portfolio(cash=24.40, starting_equity=38.69, peak_equity=38.89)
        eng.pf.positions["C1"] = Position("btc-updown-5m-1", "C1", end=pos_end, up_shares=5.0, cost=4.20)
        return eng

    def snap(self, listed):
        from bot.live import AccountSnapshot
        return AccountSnapshot(time.time(), 24.40, 4.15 if listed else 0.0, [], int(listed), 24_400_000, 0.0,
                               {"C1": 5.0} if listed else {})

    def test_unlisted_fill_counts_at_cost(self):
        from bot.config import Risk
        from bot.risk import RiskManager
        eng = self.engine(pos_end=time.time() + 200)
        snap = self.snap(listed=False)
        eng.pf.positions_value = snap.positions_value + eng._unlisted_value(snap, time.time())
        self.assertAlmostEqual(eng.pf.equity, 28.60)
        self.assertEqual(RiskManager(Risk(max_drawdown_kill_pct=0.35), eng.pf).check_breakers(), "")

    def test_listed_fill_is_not_double_counted(self):
        eng = self.engine(pos_end=time.time() + 200)
        self.assertEqual(eng._unlisted_value(self.snap(listed=True), time.time()), 0.0)

    def test_ended_market_trusts_the_api(self):
        eng = self.engine(pos_end=time.time() - 5)          # a lost position drops to 0: don't prop it up
        self.assertEqual(eng._unlisted_value(self.snap(listed=False), time.time()), 0.0)


class TestNoBlockingStatusWrite(unittest.TestCase):
    def test_locked_status_file_is_skipped_without_sleeping(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from bot.net import atomic_write_text
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "status.json"
            with mock.patch("pathlib.Path.replace", side_effect=PermissionError), \
                    mock.patch("time.sleep") as slept:
                atomic_write_text(target, "{}", retries=1)
            slept.assert_not_called()


class TestBinanceSource(unittest.TestCase):
    def prices(self, source, now):
        p = AssetPrices("btc", 600, 0.2)
        p.source = source
        p.spot.add(now - 3.0, 84000.0)                 # Coinbase: +2 bp over 3 s
        p.spot.add(now - 0.1, 84016.8)
        p.binance.add(now - 3.0, 84040.0)              # Binance (USDT, different level): +4 bp over 3 s
        p.binance.add(now - 0.1, 84073.6)
        return p

    def test_binance_message_updates_its_series(self):
        from bot.feeds import apply_binance
        p = AssetPrices("btc", 600, 0.2)
        raw = json.dumps({"stream": "btcusdt@aggTrade", "data": {"e": "aggTrade", "s": "BTCUSDT", "p": "84012.5", "T": 1}})
        self.assertIs(apply_binance({"btcusdt": p}, raw, 1000.0), p)
        self.assertEqual(p.binance.last(), (1000.0, 84012.5))
        self.assertIsNone(apply_binance({"btcusdt": p}, json.dumps({"result": None, "id": 1}), 1000.0))

    def test_source_picks_the_exchange(self):
        now = 1000.0
        self.assertAlmostEqual(self.prices("coinbase", now).momentum_bp(3, now), 2.0, places=1)
        self.assertAlmostEqual(self.prices("binance", now).momentum_bp(3, now), 4.0, places=1)
        self.assertAlmostEqual(self.prices("both", now).momentum_bp(3, now), 2.0, places=1)   # the smaller agreeing move

    def test_both_needs_agreement(self):
        now = 1000.0
        p = self.prices("both", now)
        p.binance.add(now - 0.05, 83990.0)             # Binance now down over 3 s, Coinbase up
        self.assertEqual(p.momentum_bp(3, now), 0.0)

    def test_quiet_source_falls_back_to_the_other(self):
        now = 1000.0
        p = AssetPrices("btc", 600, 0.2)
        p.source = "binance"
        p.binance.add(now - 30, 84000.0)               # Binance silent for 30 s
        p.spot.add(now - 3.0, 84000.0)
        p.spot.add(now - 0.1, 84016.8)
        self.assertAlmostEqual(p.momentum_bp(3, now), 2.0, places=1)

    def test_estimate_uses_the_source_move_not_its_price_level(self):
        now = 1000.0
        p = AssetPrices("btc", 600, 0.2)
        p.source = "binance"
        p.chainlink.add(int(now) - 2, 84005.0)
        p.binance.add(now - 1.5, 84050.0)              # Binance price during the Chainlink second (USDT level)
        p.binance.add(now - 0.1, 84073.6)
        p.spot.add(now - 0.1, 84500.0)                 # Coinbase is ignored with source = "binance"
        self.assertAlmostEqual(p.estimate_now(now), 84005.0 + (84073.6 - 84050.0), places=6)

    def test_config_rejects_a_source_without_its_feed(self):
        import tempfile
        from pathlib import Path
        from bot.config import load_config
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.toml"
            cfg.write_text('[feeds]\nbinance = false\nmomentum_source = "binance"\n')
            with self.assertRaises(ValueError):
                load_config(cfg)
            cfg.write_text('[feeds]\nmomentum_source = "kraken"\n')
            with self.assertRaises(ValueError):
                load_config(cfg)


if __name__ == "__main__":
    unittest.main()
