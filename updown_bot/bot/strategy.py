"""Momentum-taker signal: buy a side only when the coin just moved toward it AND the ask is below fair value
by more than the taker fee plus a margin. This is the only slice that was profitable before rebates for both
analysed bots (+13–27% net for x-MoneyForWhiskas, +6.7–9.3% for 0xb55fa129); flat and adverse fills lost."""
from __future__ import annotations

import math
from dataclasses import dataclass

from .book import Book
from .config import Strategy
from .model import taker_fee_per_share


@dataclass
class Intent:
    outcome: str
    limit: float
    ask: float
    fair: float
    edge: float        # fair − ask − fee, per share, at the signal
    momentum_bp: float


@dataclass
class Rejection:
    outcome: str
    reason: str
    fair: float
    ask: float | None
    edge: float | None
    momentum_bp: float


def max_limit_for_edge(fair: float, min_edge: float, fee_rate: float, tick: float) -> float:
    """Highest price on the tick grid where fair − p − fee(p) >= min_edge."""
    p = math.floor((fair - min_edge) / tick) * tick
    while p > 0 and fair - p - taker_fee_per_share(p, fee_rate) < min_edge - 1e-12:
        p -= tick
    return round(max(p, 0.0), 4)


def evaluate(cfg: Strategy, *, p_up: float, momentum_bp: float | None, books: dict[str, Book | None], now: float,
             seconds_left: float, seconds_elapsed: float, fee_rate: float, tick: float, max_slippage: float,
             trend_bp: float | None = None) -> tuple[Intent | None, list[Rejection]]:
    rejections: list[Rejection] = []
    if momentum_bp is None or seconds_left < cfg.min_seconds_left or seconds_elapsed < cfg.min_seconds_elapsed:
        return None, rejections
    best: Intent | None = None
    for outcome, fair in (("Up", p_up), ("Down", 1.0 - p_up)):
        directional = momentum_bp if outcome == "Up" else -momentum_bp
        if directional < cfg.min_momentum_bp:
            continue  # no signal for this side — not logged (the common case)
        if cfg.max_momentum_bp and directional > cfg.max_momentum_bp:
            rejections.append(Rejection(outcome, "momentum_spike", fair, None, None, momentum_bp))
            continue
        if cfg.max_counter_trend_bp and trend_bp is not None:
            with_trend = trend_bp if outcome == "Up" else -trend_bp
            if with_trend < -cfg.max_counter_trend_bp:   # a small uptick inside a bigger move the other way
                rejections.append(Rejection(outcome, "counter_trend", fair, None, None, momentum_bp))
                continue
        book = books.get(outcome)
        if book is None or book.age(now) > cfg.max_book_age_s:
            rejections.append(Rejection(outcome, "stale_book", fair, None, None, momentum_bp))
            continue
        ask = book.best_ask()
        if ask is None:
            rejections.append(Rejection(outcome, "no_ask", fair, None, None, momentum_bp))
            continue
        edge = fair - ask - taker_fee_per_share(ask, fee_rate)
        if not (cfg.min_price <= ask <= cfg.max_price):
            rejections.append(Rejection(outcome, "price_out_of_range", fair, ask, edge, momentum_bp))
            continue
        if edge < cfg.min_edge:
            rejections.append(Rejection(outcome, "edge_below_min", fair, ask, edge, momentum_bp))
            continue
        if cfg.max_edge and edge > cfg.max_edge:   # the market rarely misprices by this much; the model more often errs
            rejections.append(Rejection(outcome, "edge_too_large", fair, ask, edge, momentum_bp))
            continue
        limit = min(round(ask + max_slippage, 4), max_limit_for_edge(fair, cfg.min_edge, fee_rate, tick))
        if limit < ask:
            rejections.append(Rejection(outcome, "limit_below_ask", fair, ask, edge, momentum_bp))
            continue
        cand = Intent(outcome, limit, ask, fair, edge, momentum_bp)
        if best is None or cand.edge > best.edge:
            best = cand
    return best, rejections
