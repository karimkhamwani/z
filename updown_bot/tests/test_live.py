"""Live/shadow execution against a fake Polymarket SDK client — no network, no keys, no real orders."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from bot.config import Risk
from bot.live import HaltTrading, LiveAccount, LiveExecutor, ShadowExecutor, order_params
from bot.markets import Market
from bot.paper import Portfolio, Position, settle
from bot.risk import RiskManager
from bot.secrets import CredentialError, LiveCreds, get_live_creds, load_dotenv

MARKET = Market(asset="btc", timeframe="5m", start=1000, end=1300, slug="btc-updown-5m-1000", condition_id="0xc1",
                up_token="UP", down_token="DOWN", tick=0.01, min_size=5, fee_rate=0.07, twap_lookback=60)
CTX = {"now": 1100.0, "fair": 0.70, "momentum_bp": 1.2, "seconds_left": 200.0, "ask": 0.60}
KEY = "0x" + "11" * 32   # throwaway test key
REAL_SLEEP = asyncio.sleep  # tests patch asyncio.sleep; fakes and the fast sleep must use the real one


class FakeLedger:
    def __init__(self):
        self.orders = []

    def order(self, row):
        self.orders.append(row)


class FakeClient:
    def __init__(self, responses=None, activity=(), balance=0, positions=()):
        self.responses = list(responses or [])
        self.calls = []
        self.activity = list(activity)
        self.balance = balance
        self.positions = list(positions)
        self.cancelled = 0

    async def _next(self):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if r == "hang":
            await REAL_SLEEP(10)
        return r

    async def create_market_order(self, **kw):      # sign (local)
        self.calls.append(kw)
        return {"signed": kw}

    async def post_order(self, signed):             # send
        return await self._next()

    async def place_market_order(self, **kw):       # fallback path (allowance recovery)
        self.fallback_calls = getattr(self, "fallback_calls", 0) + 1
        return await self._next()

    async def get_order(self, order_id):
        return NS(size_matched="5", status="MATCHED")

    async def cancel_all(self):
        self.cancelled += 1

    async def cancel_order(self, order_id):
        self.cancelled += 1

    async def get_balance_allowance(self, asset_type):
        return NS(balance=self.balance, allowances={})

    def _pages(self, items):
        async def gen():
            for i in items:
                yield i
        return NS(iter_items=gen)

    def list_positions(self, user):
        return self._pages(self.positions)

    def list_activity(self, **kw):
        return self._pages(self.activity)


def accepted(making, taking, status="matched"):
    return NS(ok=True, order_id="0xord", status=status, making_amount=making, taking_amount=taking,
              trade_ids=("t1",), transactions_hashes=())


def rejected(code, msg="x"):
    return NS(ok=False, code=code, message=msg)


def make(client, kill=False, max_errors=3):
    pf = Portfolio(cash=100, starting_equity=100, peak_equity=100)
    acct = LiveAccount(LiveCreds(KEY, "0x" + "22" * 20, 1), client=client, wallet="0x" + "22" * 20)
    led = FakeLedger()
    ex = LiveExecutor(acct, pf, led, order_timeout_s=0.5, max_consecutive_errors=max_errors, kill_switch=lambda: kill)
    return ex, pf, led


def buy(ex):
    return asyncio.run(ex.buy(market=MARKET, outcome="Up", limit=0.62, max_shares=5, get_book=None, context=CTX))


class TestOrderParams(unittest.TestCase):
    def test_amount_and_all_in_cap(self):
        amount, max_spend = order_params(5, 0.62, 0.07)
        self.assertEqual(amount, 3.10)                           # 5 shares × $0.62
        self.assertEqual(max_spend, 3.19)                        # + fee 5×0.07×0.62×0.38 = $0.0825, rounded up


class TestLiveExecutor(unittest.TestCase):
    def test_matched_fill_is_recorded(self):
        client = FakeClient([accepted("3.00", "5")])
        ex, pf, led = make(client)
        fill = buy(ex)
        self.assertAlmostEqual(fill.shares, 5)
        self.assertAlmostEqual(fill.avg_price, 0.60)
        self.assertAlmostEqual(pf.positions["0xc1"].up_shares, 5)
        self.assertLess(pf.cash, 97.0)                           # $3 + estimated fee, provisional until sync
        kw = client.calls[0]
        self.assertEqual((kw["side"], kw["order_type"], kw["amount"], kw["max_price"], kw["max_spend"]),
                         ("BUY", "FAK", "3.10", "0.6200", "3.19"))
        self.assertEqual(led.orders[-1]["status"], "matched")

    def test_amount_fields_in_either_order(self):
        ex, pf, _ = make(FakeClient([accepted("5", "3.00")]))
        self.assertAlmostEqual(buy(ex).avg_price, 0.60)

    def test_no_fill_is_not_an_error(self):
        ex, pf, led = make(FakeClient([rejected("fak_not_filled"), rejected("unmatched")]))
        self.assertIsNone(buy(ex))
        self.assertIsNone(buy(ex))
        self.assertEqual(ex.errors, 0)
        self.assertEqual(pf.positions, {})

    def test_insufficient_balance_retries_once_then_halts(self):
        client = FakeClient([rejected("not_enough_balance", "x"), rejected("not_enough_balance", "still")])
        ex, *_ = make(client)
        with self.assertRaises(HaltTrading):
            buy(ex)
        self.assertEqual(client.fallback_calls, 1)                  # one retry via the SDK's allowance repair

    def test_allowance_repaired_on_retry(self):
        client = FakeClient([rejected("not_enough_balance", "x"), accepted("3.00", "5")])
        ex, pf, _ = make(client)
        self.assertAlmostEqual(buy(ex).shares, 5)

    def test_sign_and_send_times_are_recorded(self):
        ex, pf, led = make(FakeClient([accepted("3.00", "5")]))
        buy(ex)
        row = led.orders[-1]
        self.assertIsNotNone(row["sign_ms"]); self.assertIsNotNone(row["post_ms"])
        self.assertGreaterEqual(row["latency_s"] * 1000, row["post_ms"])

    def test_repeated_errors_halt(self):
        ex, *_ = make(FakeClient([rejected("unknown")] * 3), max_errors=3)
        buy(ex)
        buy(ex)
        with self.assertRaises(HaltTrading):
            buy(ex)

    def test_timeout_then_trade_found_records_fill(self):
        trade = NS(timestamp=datetime.now(timezone.utc), asset_id="UP", side="BUY", shares="5", amount="3.05")
        ex, pf, led = make(FakeClient(["hang"], activity=[trade]))
        with mock.patch("bot.live.asyncio.sleep", new=_fast_sleep):
            fill = buy(ex)
        self.assertIsNotNone(fill)
        self.assertAlmostEqual(fill.shares, 5)
        self.assertEqual(led.orders[0]["status"], "error")

    def test_timeout_without_trade_counts_as_error(self):
        ex, pf, _ = make(FakeClient([ConnectionError("reset")]))
        with mock.patch("bot.live.asyncio.sleep", new=_fast_sleep):
            self.assertIsNone(buy(ex))
        self.assertEqual(ex.errors, 1)
        self.assertEqual(pf.positions, {})

    def test_kill_switch_sends_nothing(self):
        client = FakeClient([accepted("3", "5")])
        ex, *_ = make(client, kill=True)
        self.assertIsNone(buy(ex))
        self.assertEqual(client.calls, [])

    def test_delayed_order_is_polled_and_remainder_cancelled(self):
        client = FakeClient([accepted("0", "0", status="delayed")])
        ex, pf, _ = make(client)
        with mock.patch("bot.live.asyncio.sleep", new=_fast_sleep):
            fill = buy(ex)
        self.assertAlmostEqual(fill.shares, 5)
        self.assertAlmostEqual(fill.avg_price, 0.62)             # conservative: worst allowed price
        self.assertEqual(client.cancelled, 1)


class TestLedgerMigration(unittest.TestCase):
    def test_old_ledger_gets_new_columns(self):
        import sqlite3
        from bot.ledger import Ledger
        d = Path(tempfile.mkdtemp())
        db = sqlite3.connect(d / "paper.db")        # an orders table from before sign_ms/post_ms existed
        db.execute("CREATE TABLE orders (ts REAL, slug TEXT, condition_id TEXT, outcome TEXT, amount REAL, max_spend REAL, "
                   "max_price REAL, ok INTEGER, status TEXT, code TEXT, message TEXT, latency_s REAL, order_id TEXT, "
                   "filled_usd REAL, filled_shares REAL)")
        db.commit(); db.close()
        led = Ledger(d / "paper.db")
        led.order({"ts": 1.0, "slug": "s", "sign_ms": 0.3, "post_ms": 480.0})
        led.commit()
        self.assertEqual(led.db.execute("select post_ms from orders").fetchone()[0], 480.0)


class TestShadow(unittest.TestCase):
    def test_shadow_never_sends(self):
        led = FakeLedger()
        fill = asyncio.run(ShadowExecutor(led).buy(market=MARKET, outcome="Down", limit=0.4, max_shares=5,
                                                   get_book=None, context=CTX))
        self.assertIsNone(fill)
        self.assertEqual(led.orders[0]["status"], "shadow")


class TestAccount(unittest.TestCase):
    def test_snapshot_balance_and_positions(self):
        pos = [NS(current_size="5", current_value="3.2", redeemable=False, condition_id="0xa"),
               NS(current_size="10", current_value="10", redeemable=True, condition_id="0xb"),   # won, claimable
               NS(current_size="7", current_value="0", redeemable=True, condition_id="0xc"),     # lost: nothing to claim
               NS(current_size="0", current_value="0", redeemable=False, condition_id="0xd")]
        acct = LiveAccount(LiveCreds(KEY, "0x" + "22" * 20, 1), client=FakeClient(balance=12_345_678, positions=pos),
                           wallet="0x" + "22" * 20)
        snap = asyncio.run(acct.snapshot())
        self.assertAlmostEqual(snap.cash, 12.345678)
        self.assertAlmostEqual(snap.positions_value, 13.2)
        self.assertEqual(snap.redeemable, ["0xb"])
        self.assertEqual(snap.open_positions, 3)

    def test_wallet_type_mismatch_refuses_to_trade(self):
        fake = NS(wallet="0x" + "22" * 20, wallet_type="EOA")

        async def create(**kw):
            return fake
        with mock.patch("polymarket.AsyncSecureClient.create", new=create):
            acct = LiveAccount(LiveCreds(KEY, "0x" + "22" * 20, 1))          # declared proxy (1), actually EOA
            with self.assertRaises(HaltTrading):
                asyncio.run(acct.connect())


class TestPortfolioLive(unittest.TestCase):
    def test_equity_is_portfolio_balance(self):
        pf = Portfolio(cash=50, starting_equity=100, peak_equity=100)
        pf.positions["c"] = Position("s", "c", 0, up_shares=10, cost=6)
        self.assertEqual(pf.equity, 56)                          # paper: cash + cost
        pf.positions_value = 9.5
        self.assertEqual(pf.equity, 59.5)                        # live: cash + market value from the exchange

    def test_live_settlement_waits_for_redemption_cash(self):
        pf = Portfolio(cash=50, starting_equity=100, peak_equity=100)
        pf.positions["c"] = Position("s", "c", 0, up_shares=10, cost=6)
        r = settle(pf, "c", "Up", credit_cash=False)
        self.assertEqual(r["pnl"], 4)
        self.assertEqual(pf.cash, 50)

    def test_live_halt_is_sticky(self):
        pf = Portfolio(cash=100, starting_equity=100, peak_equity=100, day_start_equity=100, halted="live halt: x")
        self.assertTrue(RiskManager(Risk(), pf).check_breakers().startswith("live halt"))


class TestSecrets(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(("POLY_", "POLYMARKET_"))}

    def tearDown(self):
        for k in [k for k in os.environ if k.startswith(("POLY_", "POLYMARKET_"))]:
            del os.environ[k]
        os.environ.update(self.saved)

    def write_env(self, text):
        d = Path(tempfile.mkdtemp())
        (d / ".env").write_text(text)
        return d

    def test_user_variable_names_and_inline_comments(self):
        d = self.write_env(f"POLY_PRIVATE_KEY={KEY[2:]}   # signer\nPOLY_FUNDER_ADDRESS=0x{'ab' * 20}  # proxy\n"
                           "POLY_SIGNATURE_TYPE=1           # 1 = proxy wallet\n")
        creds, warnings = get_live_creds(d)
        self.assertEqual(creds.private_key, KEY)
        self.assertEqual(creds.wallet, "0x" + "ab" * 20)
        self.assertEqual(creds.expected_wallet_type, "POLY_PROXY")
        self.assertTrue(any("Relayer" in w for w in warnings))
        self.assertNotIn(KEY[2:10], repr(creds))                  # key never shown

    def test_eoa_derives_funder_from_key(self):
        from eth_account import Account
        d = self.write_env(f"POLY_PRIVATE_KEY={KEY}\nPOLY_SIGNATURE_TYPE=0\n")
        creds, _ = get_live_creds(d)
        self.assertEqual(creds.wallet, Account.from_key(KEY).address)
        self.assertTrue(creds.can_redeem)

    def test_validation(self):
        for text in ("POLY_PRIVATE_KEY=nothex\nPOLY_FUNDER_ADDRESS=0x" + "ab" * 20,
                     f"POLY_PRIVATE_KEY={KEY}\nPOLY_SIGNATURE_TYPE=1\n",                         # proxy needs funder
                     f"POLY_PRIVATE_KEY={KEY}\nPOLY_FUNDER_ADDRESS=0x{'ab' * 20}\nPOLY_SIGNATURE_TYPE=7",
                     f"POLY_PRIVATE_KEY={KEY}\nPOLY_FUNDER_ADDRESS=0x{'ab' * 20}\nPOLY_SIGNATURE_TYPE=0"):  # EOA ≠ signer
            for k in [k for k in os.environ if k.startswith("POLY_")]:
                del os.environ[k]
            with self.assertRaises(CredentialError, msg=text):
                get_live_creds(self.write_env(text))


async def _fast_sleep(*_a, **_k):
    await REAL_SLEEP(0)


if __name__ == "__main__":
    unittest.main()
