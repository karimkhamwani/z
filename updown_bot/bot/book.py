"""Live order books from the Polymarket CLOB market websocket."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from .net import get_ctx

log = logging.getLogger("book")
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class Book:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.updated = 0.0

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def asks_ascending(self) -> list[tuple[float, float]]:
        return sorted(self.asks.items())

    def age(self, now: float) -> float:
        return now - self.updated if self.updated else float("inf")


class BookFeed:
    """Keeps one websocket subscribed to `desired` token ids; reconnects whenever the set changes."""

    def __init__(self, status: dict):
        self.books: dict[str, Book] = {}
        self.desired: set[str] = set()
        self.trades: list[dict] = []  # recent prints (kept short; used for diagnostics)
        self.status = status
        self._changed = asyncio.Event()

    def set_tokens(self, tokens: set[str]) -> None:
        if tokens != self.desired:
            self.desired = set(tokens)
            for t in tokens:
                self.books.setdefault(t, Book())
            for t in list(self.books):
                if t not in tokens:
                    del self.books[t]
            self._changed.set()

    def book(self, token: str) -> Book | None:
        return self.books.get(token)

    def _apply(self, e: dict, now: float) -> None:
        et = e.get("event_type")
        if et == "book":
            b = self.books.get(e.get("asset_id"))
            if b is None:
                return
            b.bids = {float(l["price"]): float(l["size"]) for l in e.get("bids", []) if float(l["size"]) > 0}
            b.asks = {float(l["price"]): float(l["size"]) for l in e.get("asks", []) if float(l["size"]) > 0}
            b.updated = now
        elif et == "price_change":
            for c in e.get("price_changes", []):
                b = self.books.get(c.get("asset_id"))
                if b is None:
                    continue
                side = b.bids if c.get("side") == "BUY" else b.asks
                p, s = float(c["price"]), float(c["size"])
                if s <= 0:
                    side.pop(p, None)
                else:
                    side[p] = s
                b.updated = now
        elif et == "last_trade_price":
            self.trades.append({"t": now, "asset": e.get("asset_id"), "price": float(e.get("price", 0)),
                                "size": float(e.get("size", 0)), "side": e.get("side")})
            if len(self.trades) > 2000:
                del self.trades[:1000]

    async def run(self) -> None:
        backoff = 1.0
        while True:
            if not self.desired:
                await asyncio.sleep(0.2)
                continue
            tokens = sorted(self.desired)
            self._changed.clear()
            try:
                async with websockets.connect(CLOB_WS, ssl=get_ctx(), open_timeout=10, max_size=2**24,
                                              ping_interval=None) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    self.status["clob"] = "up"
                    backoff = 1.0
                    last_ping = time.time()
                    while not self._changed.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            raw = None
                        now = time.time()
                        if now - last_ping > 10:
                            await ws.send("PING")
                            last_ping = now
                        if not raw or raw == "PONG":
                            continue
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        for e in msg if isinstance(msg, list) else [msg]:
                            self._apply(e, now)
            except Exception as e:
                self.status["clob"] = f"down ({type(e).__name__})"
                log.warning("clob feed error: %s; reconnecting in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
