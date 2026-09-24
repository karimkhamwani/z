"""Portfolio accounting, paper fills against the live book, settlement, and taker-rebate estimates."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .book import Book
from .model import taker_fee_per_share

# Polymarket Taker Rebate Program (30-day weighted volume → share of taker fees refunded)
REBATE_TIERS = [(10_000_000, 0.50), (4_000_000, 0.44), (1_000_000, 0.32), (200_000, 0.18), (20_000, 0.08), (2_000, 0.03)]
CRYPTO_WEIGHT = 2.3


def rebate_rate(wv_30d: float) -> float:
    for threshold, rate in REBATE_TIERS:
        if wv_30d >= threshold:
            return rate
    return 0.0


def utc_day(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d")


@dataclass
class Position:
    slug: str
    condition_id: str
    end: int
    up_shares: float = 0.0
    down_shares: float = 0.0
    cost: float = 0.0          # cash paid including fees
    fees: float = 0.0
    orders: int = 0

    def shares(self, outcome: str) -> float:
        return self.up_shares if outcome == "Up" else self.down_shares


@dataclass
class Fill:
    ts: float
    slug: str
    condition_id: str
    outcome: str
    shares: float
    avg_price: float
    notional: float
    fee: float
    cash: float
    fair: float
    edge: float
    momentum_bp: float
    seconds_left: float
    signal_ask: float
    levels: list


@dataclass
class Portfolio:
    cash: float
    starting_equity: float
    peak_equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    day: str = ""
    day_start_equity: float = 0.0
    fees_by_day: dict[str, float] = field(default_factory=dict)
    wv_by_day: dict[str, float] = field(default_factory=dict)
    rebated_days: list[str] = field(default_factory=list)
    realized_pnl: float = 0.0
    rebates_total: float = 0.0
    halted: str = ""

    @property
    def open_cost(self) -> float:
        return sum(p.cost for p in self.positions.values())

    @property
    def equity(self) -> float:
        """Cash plus open positions at cost (conservative; used for sizing and stops)."""
        return self.cash + self.open_cost

    def wv_30d(self, today: str) -> float:
        d0 = (dt.date.fromisoformat(today) - dt.timedelta(days=30)).isoformat()
        return sum(v for d, v in self.wv_by_day.items() if d0 <= d <= today)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Portfolio":
        d = json.loads(text)
        d["positions"] = {k: Position(**v) for k, v in d.get("positions", {}).items()}
        return cls(**d)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.to_json())
        tmp.replace(path)


def simulate_taker_fill(book_asks: list[tuple[float, float]], *, limit: float, max_shares: float, haircut: float,
                        fee_rate: float, cash_available: float, min_shares: float) -> tuple[float, float, float, list]:
    """Walk the asks up to `limit`, taking `haircut` of each level's size. Returns (shares, notional, fee, levels).
    Shares are rounded down to 2 decimals; returns zeros if the fill would be below `min_shares`."""
    shares = notional = fee = 0.0
    levels = []
    for price, size in book_asks:
        if price > limit + 1e-9 or shares >= max_shares:
            break
        avail = size * haircut
        take = min(avail, max_shares - shares)
        unit_cost = price + taker_fee_per_share(price, fee_rate)
        affordable = (cash_available - notional - fee) / unit_cost
        take = min(take, affordable)
        take = math.floor(take * 100) / 100
        if take <= 0:
            break
        shares += take
        notional += take * price
        fee += take * taker_fee_per_share(price, fee_rate)
        levels.append([price, take])
    if shares < min_shares:
        return 0.0, 0.0, 0.0, []
    return shares, notional, fee, levels


class PaperExecutor:
    """Taker orders filled against the live book after a simulated latency (FAK semantics: partial fills allowed)."""

    def __init__(self, portfolio: Portfolio, latency_ms: float, haircut: float, fee_rate: float, min_shares: float):
        self.pf = portfolio
        self.latency = latency_ms / 1000
        self.haircut = haircut
        self.fee_rate = fee_rate
        self.min_shares = min_shares

    async def buy(self, *, market, outcome: str, limit: float, max_shares: float, get_book, context: dict) -> Fill | None:
        await asyncio.sleep(self.latency)
        book: Book | None = get_book(market.token(outcome))
        if book is None:
            return None
        shares, notional, fee, levels = simulate_taker_fill(
            book.asks_ascending(), limit=limit, max_shares=max_shares, haircut=self.haircut,
            fee_rate=market.fee_rate, cash_available=self.pf.cash, min_shares=max(self.min_shares, market.min_size))
        if shares <= 0 or notional < 1.0:  # Polymarket marketable orders need >= $1
            return None
        cash = notional + fee
        pos = self.pf.positions.setdefault(market.condition_id, Position(market.slug, market.condition_id, market.end))
        if outcome == "Up":
            pos.up_shares += shares
        else:
            pos.down_shares += shares
        pos.cost += cash
        pos.fees += fee
        pos.orders += 1
        self.pf.cash -= cash
        day = utc_day(context["now"])
        self.pf.fees_by_day[day] = self.pf.fees_by_day.get(day, 0.0) + fee
        avg = notional / shares
        self.pf.wv_by_day[day] = self.pf.wv_by_day.get(day, 0.0) + notional * (1 - avg) * CRYPTO_WEIGHT
        return Fill(ts=context["now"] + self.latency, slug=market.slug, condition_id=market.condition_id, outcome=outcome,
                    shares=shares, avg_price=avg, notional=notional, fee=fee, cash=cash, fair=context["fair"],
                    edge=context["fair"] - avg - fee / shares, momentum_bp=context["momentum_bp"],
                    seconds_left=context["seconds_left"] - self.latency, signal_ask=context["ask"], levels=levels)


def settle(pf: Portfolio, condition_id: str, winner: str) -> dict | None:
    pos = pf.positions.pop(condition_id, None)
    if pos is None:
        return None
    payout = pos.shares(winner)
    pnl = payout - pos.cost
    pf.cash += payout
    pf.realized_pnl += pnl
    pf.peak_equity = max(pf.peak_equity, pf.equity)
    return {"slug": pos.slug, "condition_id": condition_id, "winner": winner, "up_shares": pos.up_shares,
            "down_shares": pos.down_shares, "cost": pos.cost, "fees": pos.fees, "payout": payout, "pnl": pnl,
            "orders": pos.orders}


def credit_rebates(pf: Portfolio, now: float, enabled: bool) -> dict | None:
    """At the first check after UTC midnight, refund yesterday's taker fees at yesterday's tier (paid next day)."""
    if not enabled:
        return None
    today = utc_day(now)
    yesterday = (dt.date.fromisoformat(today) - dt.timedelta(days=1)).isoformat()
    if yesterday in pf.rebated_days or yesterday not in pf.fees_by_day:
        return None
    rate = rebate_rate(pf.wv_30d(yesterday))
    amount = pf.fees_by_day[yesterday] * rate
    pf.cash += amount
    pf.rebates_total += amount
    pf.rebated_days.append(yesterday)
    return {"day": yesterday, "fees": pf.fees_by_day[yesterday], "wv_30d": pf.wv_30d(yesterday), "rate": rate,
            "rebate": amount}
