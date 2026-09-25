"""Real-money execution through Polymarket's official SDK (`polymarket-client`), plus a no-orders shadow mode.

- LiveAccount   connects the account, reads the portfolio balance (pUSD cash + position value), claims winnings.
- LiveExecutor  sends FAK market BUYs capped by price, shares and all-in spend; records actual fills.
- ShadowExecutor  same decisions, but only logs the order it would have sent.

Both executors expose the same `buy(...)` coroutine as PaperExecutor, so the engine and strategy are identical
across modes.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from .model import taker_fee_per_share
from .paper import CRYPTO_WEIGHT, Fill, Portfolio, Position, utc_day
from .secrets import LiveCreds, mask

log = logging.getLogger("live")

NO_FILL_CODES = {"unmatched", "fak_not_filled", "fok_not_filled", "market_not_ready"}
HALT_CODES = {"not_enough_balance"}


class HaltTrading(RuntimeError):
    """Stop opening new positions until a human looks (auth, balance or repeated errors)."""


@dataclass
class AccountSnapshot:
    ts: float
    cash: float                 # pUSD available to trade
    positions_value: float      # current value of open (unresolved) positions
    redeemable: list[str]       # condition ids with a winning (value > 0) redeemable position
    open_positions: int
    raw_balance: int
    claimable_value: float = 0.0   # value of resolved winning positions not yet claimed


@dataclass
class LiveAccount:
    creds: LiveCreds
    client: Any = None
    wallet: str = ""
    wallet_type: str = ""
    redeemed: dict[str, float] = field(default_factory=dict)   # condition_id -> last attempt time

    async def connect(self) -> None:
        from polymarket import AsyncSecureClient, RelayerApiKey   # imported lazily: paper mode doesn't need the SDK
        api_key = RelayerApiKey(key=self.creds.relayer_key, address=self.creds.relayer_address) if self.creds.relayer else None
        self.client = await AsyncSecureClient.create(private_key=self.creds.private_key, wallet=self.creds.wallet,
                                                     api_key=api_key)
        self.wallet = str(self.client.wallet)
        self.wallet_type = str(self.client.wallet_type)
        if self.wallet.lower() != self.creds.wallet.lower():
            raise HaltTrading(f"SDK resolved wallet {mask(self.wallet)} but POLY_FUNDER_ADDRESS is {mask(self.creds.wallet)}")
        if self.wallet_type != self.creds.expected_wallet_type:
            raise HaltTrading(f"POLY_SIGNATURE_TYPE={self.creds.signature_type} means {self.creds.expected_wallet_type}, "
                              f"but Polymarket reports this wallet as {self.wallet_type}. Fix POLY_SIGNATURE_TYPE "
                              f"(0 EOA, 1 proxy, 2 Safe, 3 deposit wallet) before trading.")

    async def snapshot(self) -> AccountSnapshot:
        bal = await self.client.get_balance_allowance(asset_type="COLLATERAL")
        cash = bal.balance / 1e6                                   # pUSD has 6 decimals
        value = claimable = 0.0
        redeemable: list[str] = []
        n_open = 0
        async for p in self.client.list_positions(user=self.wallet).iter_items():
            size = float(p.current_size or 0)
            if size <= 0:
                continue
            v = float(p.current_value or 0)
            n_open += 1
            if p.redeemable:
                if v > 0:
                    redeemable.append(str(p.condition_id))
                    claimable += v
            else:
                value += v
        return AccountSnapshot(time.time(), cash, value, redeemable, n_open, int(bal.balance), claimable)

    async def redeem(self, condition_id: str) -> str | None:
        if not self.creds.can_redeem:
            return None
        last = self.redeemed.get(condition_id, 0)
        if time.time() - last < 300:        # don't hammer the relayer; retry a failed claim every 5 min
            return None
        self.redeemed[condition_id] = time.time()
        handle = await self.client.redeem_positions(condition_id=condition_id)
        outcome = await asyncio.wait_for(handle.wait(), timeout=120)
        return str(outcome.transaction_hash)

    async def recent_buys(self, condition_id: str, token_id: str, since: float) -> list[Any]:
        """Trades on this token since `since` — used to resolve orders whose outcome is unknown after an error."""
        out = []
        async for a in self.client.list_activity(user=self.wallet, condition_id=condition_id,
                                                 activity_types=["TRADE"]).iter_items():
            ts = a.timestamp.timestamp() if hasattr(a.timestamp, "timestamp") else float(a.timestamp)
            if ts < since - 5:
                break
            if str(getattr(a, "asset_id", "")) == str(token_id) and getattr(a, "side", "") == "BUY":
                out.append(a)
        return out

    async def cancel_all(self) -> None:
        await self.client.cancel_all()

    async def prewarm(self, token_ids: list[str]) -> None:
        """Load a market's order metadata into the SDK's cache before the first order. Measured live: the first
        order in each market took 2.0-2.4 s (metadata lookup) versus ~0.6 s afterwards."""
        try:
            ctx = self.client._ctx
            for tok in token_ids:
                await ctx.order_metadata.resolve_market(ctx, token_id=tok)
        except Exception as e:   # private SDK internals: if they change, orders still work, just slower
            log.debug("prewarm skipped: %s", e)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()


def is_no_match(e: Exception) -> bool:
    """The SDK raises RequestRejectedError('no orders found to match with FAK order ...') when a FAK finds
    nothing to fill; the response-code path (`fak_not_filled`) is handled separately."""
    msg = str(e).lower()
    return type(e).__name__ == "RequestRejectedError" and ("no orders found to match" in msg or "fak" in msg and "killed" in msg)


def order_params(shares: float, limit: float, fee_rate: float) -> tuple[float, float]:
    """(amount, max_spend) for a BUY of `shares` at worst price `limit`.
    amount = shares × limit (pre-fee USD, cents); max_spend adds the taker fee at the limit, rounded up to cents.
    Fills at better prices spend the same dollars for slightly more shares, never more money."""
    amount = round(shares * limit, 2)
    max_spend = math.ceil((amount + shares * taker_fee_per_share(limit, fee_rate)) * 100 - 1e-9) / 100
    return amount, max_spend


class LiveExecutor:
    def __init__(self, account: LiveAccount, portfolio: Portfolio, ledger, *, order_timeout_s: float,
                 max_consecutive_errors: int, kill_switch):
        self.acct = account
        self.pf = portfolio
        self.ledger = ledger
        self.timeout = order_timeout_s
        self.max_errors = max_consecutive_errors
        self.kill_switch = kill_switch
        self.errors = 0
        self.timing: dict = {}

    def _order_row(self, market, outcome, amount, max_spend, limit, **kw) -> None:
        self.ledger.order({"ts": time.time(), "slug": market.slug, "condition_id": market.condition_id,
                           "outcome": outcome, "amount": amount, "max_spend": max_spend, "max_price": limit, **kw})

    def _error(self, why: str) -> None:
        self.errors += 1
        log.error("order error %d/%d: %s", self.errors, self.max_errors, why)
        if self.errors >= self.max_errors:
            raise HaltTrading(f"{self.errors} consecutive order errors (last: {why})")

    async def buy(self, *, market, outcome: str, limit: float, max_shares: float, get_book, context: dict) -> Fill | None:
        if self.kill_switch():
            return None
        amount, max_spend = order_params(max_shares, limit, market.fee_rate)
        if amount < 1.0:
            return None
        token = market.token(outcome)
        t0 = time.time()
        self.timing = {}
        try:
            resp = await asyncio.wait_for(self._send(token, amount, max_spend, limit), timeout=self.timeout)
        except Exception as e:  # timeout / network: the order may or may not have reached the exchange
            latency = time.time() - t0
            if is_no_match(e):   # a definitive "nothing to fill against" — not an error, nothing to recover
                self._order_row(market, outcome, amount, max_spend, limit, ok=0, status="rejected",
                                code="fak_not_filled", message=str(e)[:300], latency_s=latency)
                self.errors = 0
                return None
            if type(e).__name__ == "RequestRejectedError":   # server refused it: no order exists, skip the check
                self._order_row(market, outcome, amount, max_spend, limit, ok=0, status="rejected",
                                code=type(e).__name__, message=str(e)[:300], latency_s=latency)
                if "balance" in str(e).lower() or "allowance" in str(e).lower():
                    raise HaltTrading(f"exchange rejected order: {e}")
                self._error(f"rejected: {e}")
                return None
            self._order_row(market, outcome, amount, max_spend, limit, ok=0, status="error", code=type(e).__name__,
                            message=str(e)[:300], latency_s=latency)
            fill = await self._recover(market, outcome, token, t0, context)
            if fill is None:
                self._error(f"{type(e).__name__}: {e}")
            return fill
        latency = time.time() - t0
        if not resp.ok:
            self._order_row(market, outcome, amount, max_spend, limit, ok=0, status="rejected", code=resp.code,
                            message=resp.message[:300], latency_s=latency)
            if resp.code in HALT_CODES:
                raise HaltTrading(f"exchange rejected order: {resp.code} ({resp.message})")
            if resp.code in NO_FILL_CODES:
                self.errors = 0
                return None
            self._error(f"rejected {resp.code}: {resp.message}")
            return None
        self.errors = 0
        making, taking = float(resp.making_amount), float(resp.taking_amount)
        latency = self.timing.get("total_s", latency)
        usd, shares = (making, taking) if taking >= making else (taking, making)  # BUY below $1: shares > dollars
        if resp.status != "matched" and shares <= 0:
            usd, shares = await self._await_delayed(str(resp.order_id), limit)
        self._order_row(market, outcome, amount, max_spend, limit, ok=1, status=resp.status, code="",
                        message=f"order {resp.order_id} trades {len(resp.trade_ids)}", latency_s=latency,
                        order_id=str(resp.order_id), filled_usd=usd, filled_shares=shares,
                        sign_ms=self.timing.get("sign_ms"), post_ms=self.timing.get("post_ms"))
        if shares <= 0:
            return None
        return self._record(market, outcome, shares, usd, context, latency)

    async def _send(self, token: str, amount: float, max_spend: float, limit: float):
        """Sign locally, then post — timed separately so the Orders panel shows where latency goes.
        If the exchange reports a balance/allowance problem, retry once through place_market_order, which
        lets the SDK repair a missing allowance."""
        c = self.acct.client
        kw = dict(token_id=token, side="BUY", amount=f"{amount:.2f}", max_price=f"{limit:.4f}",
                  max_spend=f"{max_spend:.2f}", order_type="FAK")
        t0 = time.perf_counter()
        signed = await c.create_market_order(**kw)
        t1 = time.perf_counter()
        resp = await c.post_order(signed)
        t2 = time.perf_counter()
        self.timing = {"sign_ms": (t1 - t0) * 1000, "post_ms": (t2 - t1) * 1000, "total_s": t2 - t0}
        if not resp.ok and resp.code == "not_enough_balance":
            resp = await c.place_market_order(**kw)
        return resp

    async def _await_delayed(self, order_id: str, limit: float, wait_s: float = 8.0) -> tuple[float, float]:
        """A `delayed`/`live` FAK hasn't matched yet: poll it, then cancel whatever is left."""
        deadline = time.time() + wait_s
        matched = 0.0
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                o = await self.acct.client.get_order(order_id=order_id)
                matched = float(o.size_matched)
                if o.status.upper() not in ("LIVE", "DELAYED"):
                    break
            except Exception as e:
                log.warning("get_order %s failed: %s", order_id, e)
        try:
            await self.acct.client.cancel_order(order_id=order_id)   # only this order, never your manual ones
        except Exception as e:
            log.warning("cancelling delayed order %s failed: %s", order_id, e)
        return matched * limit, matched   # conservative: assume the worst allowed price

    async def _recover(self, market, outcome, token, t0, context) -> Fill | None:
        try:
            await asyncio.sleep(3)
            trades = await self.acct.recent_buys(market.condition_id, token, t0)
        except Exception as e:
            log.error("could not check for fills after an order error: %s — the next account sync will catch them", e)
            return None
        if not trades:
            return None
        shares = sum(float(t.shares) for t in trades)
        usd = sum(float(t.amount) for t in trades)
        log.warning("order error, but %d trade(s) found on %s — recording the fill", len(trades), market.slug)
        return self._record(market, outcome, shares, usd, context, time.time() - t0)

    def _record(self, market, outcome, shares, usd, context, latency) -> Fill:
        avg = usd / shares
        fee = shares * taker_fee_per_share(avg, market.fee_rate)   # estimate; the account sync uses real cash
        cash = usd + fee
        pos = self.pf.positions.setdefault(market.condition_id, Position(market.slug, market.condition_id, market.end))
        if outcome == "Up":
            pos.up_shares += shares
        else:
            pos.down_shares += shares
        pos.cost += cash
        pos.fees += fee
        pos.orders += 1
        self.pf.cash -= cash   # provisional until the next balance sync
        day = utc_day(context["now"])
        self.pf.fees_by_day[day] = self.pf.fees_by_day.get(day, 0.0) + fee
        self.pf.wv_by_day[day] = self.pf.wv_by_day.get(day, 0.0) + usd * (1 - avg) * CRYPTO_WEIGHT
        return Fill(ts=context["now"] + latency, slug=market.slug, condition_id=market.condition_id, outcome=outcome,
                    shares=shares, avg_price=avg, notional=usd, fee=fee, cash=cash, fair=context["fair"],
                    edge=context["fair"] - avg - fee / shares, momentum_bp=context["momentum_bp"],
                    seconds_left=context["seconds_left"] - latency, signal_ask=context["ask"], levels=[])


class ShadowExecutor:
    """Connected to the real account, but never sends an order: logs what the live executor would have sent."""

    def __init__(self, ledger):
        self.ledger = ledger

    async def buy(self, *, market, outcome: str, limit: float, max_shares: float, get_book, context: dict) -> Fill | None:
        amount, max_spend = order_params(max_shares, limit, market.fee_rate)
        self.ledger.order({"ts": time.time(), "slug": market.slug, "condition_id": market.condition_id,
                           "outcome": outcome, "amount": amount, "max_spend": max_spend, "max_price": limit, "ok": 1,
                           "status": "shadow", "code": "", "message": "not sent (shadow mode)", "latency_s": 0.0})
        log.info("SHADOW would BUY %s %s: $%.2f (max spend $%.2f) up to %.2f — fair %.3f, mom %+.2fbp",
                 market.slug, outcome, amount, max_spend, limit, context["fair"], context["momentum_bp"])
        return None
