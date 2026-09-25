"""Live order books from the Polymarket CLOB market websocket."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from .net import get_ctx

try:                       # C JSON parser: several times faster on the ~1,000 msg/s market channel
    import orjson
    loads = orjson.loads
except ImportError:        # pragma: no cover
    loads = json.loads

log = logging.getLogger("book")
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class Book:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.updated = 0.0        # local receive time of the last update
        self.server_ts = 0.0      # Polymarket's timestamp on that update (seconds)

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def asks_ascending(self) -> list[tuple[float, float]]:
        return sorted(self.asks.items())

    def age(self, now: float) -> float:
        return now - self.updated if self.updated else float("inf")


class BookFeed:
    """Keeps one websocket subscribed to `desired` token ids; reconnects whenever the set changes.

    Efficiency matters: the market channel sends ~1,000+ messages/s. If the bot reads too slowly, Polymarket
    disconnects it ("1013 slow consumer: send buffer full") and the book it trades on falls seconds behind
    (Sep 24, Ireland server). So: plain `async for` reads (no per-message timeout wrapper), orjson when available,
    and a measured feed lag (Polymarket's message timestamps vs now) that the strategy uses to stop trading on a
    lagging book."""

    def __init__(self, status: dict):
        self.books: dict[str, Book] = {}
        self.desired: set[str] = set()
        self.trades: list[dict] = []  # recent prints (kept short; used for diagnostics)
        self.status = status
        self._changed = asyncio.Event()
        self.lag = 0.0            # EWMA of (receive time − Polymarket timestamp), seconds
        self._lag_floor: list[tuple[float, float]] = []   # (time, lag) samples for a rolling minimum
        self.msgs = 0

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

    def feed_lag(self) -> float:
        """How far behind real time the book is, net of clock offset: current lag minus the best lag seen in the
        last 2 minutes (that minimum is network latency + clock skew, which doesn't make the book stale)."""
        if not self._lag_floor:
            return 0.0
        return max(0.0, self.lag - min(l for _, l in self._lag_floor))

    def _note_lag(self, server_ms, now: float) -> None:
        try:
            ts = float(server_ms) / 1000.0
        except (TypeError, ValueError):
            return
        lag = now - ts
        self.lag = lag if self.msgs < 5 else 0.9 * self.lag + 0.1 * lag
        if not self._lag_floor or now - self._lag_floor[-1][0] > 1.0:
            self._lag_floor.append((now, lag))
            cutoff = now - 120
            while self._lag_floor and self._lag_floor[0][0] < cutoff:
                self._lag_floor.pop(0)
        elif lag < self._lag_floor[-1][1]:
            self._lag_floor[-1] = (now, lag)
        return ts

    def _apply(self, e: dict, now: float) -> None:
        et = e.get("event_type")
        server_ts = self._note_lag(e.get("timestamp"), now) or 0.0
        if et == "book":
            b = self.books.get(e.get("asset_id"))
            if b is None:
                return
            b.bids = {float(l["price"]): sz for l in e.get("bids", ()) if (sz := float(l["size"])) > 0}
            b.asks = {float(l["price"]): sz for l in e.get("asks", ()) if (sz := float(l["size"])) > 0}
            b.updated, b.server_ts = now, server_ts
        elif et == "price_change":
            for c in e.get("price_changes", ()):
                b = self.books.get(c.get("asset_id"))
                if b is None:
                    continue
                side = b.bids if c.get("side") == "BUY" else b.asks
                p, sz = float(c["price"]), float(c["size"])
                if sz <= 0:
                    side.pop(p, None)
                else:
                    side[p] = sz
                b.updated, b.server_ts = now, server_ts
        elif et == "last_trade_price":
            self.trades.append({"t": now, "asset": e.get("asset_id"), "price": float(e.get("price", 0)),
                                "size": float(e.get("size", 0)), "side": e.get("side")})
            if len(self.trades) > 2000:
                del self.trades[:1000]

    async def _housekeeping(self, ws, state: dict) -> None:
        """Keep-alive pings, a silence watchdog, and closing the socket when the token set changes."""
        while True:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=10)
                await ws.close()          # token set changed: the reader loop ends and we resubscribe
                return
            except asyncio.TimeoutError:
                pass
            if time.time() - state["last_msg"] > 30:
                log.warning("order book stream silent for 30s; reconnecting")
                await ws.close()
                return
            try:
                await ws.send("PING")
            except Exception:
                return

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
                                              max_queue=4096, ping_interval=None) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    self.status["clob"] = "up"
                    backoff = 1.0
                    state = {"last_msg": time.time()}
                    keeper = asyncio.create_task(self._housekeeping(ws, state))
                    try:
                        async for raw in ws:
                            now = time.time()
                            state["last_msg"] = now
                            if raw == "PONG" or not raw:
                                continue
                            try:
                                msg = loads(raw)
                            except ValueError:
                                continue
                            self.msgs += 1
                            if isinstance(msg, list):
                                for e in msg:
                                    self._apply(e, now)
                            else:
                                self._apply(msg, now)
                    finally:
                        keeper.cancel()
                if self._changed.is_set():
                    continue
            except Exception as e:
                self.status["clob"] = f"down ({type(e).__name__})"
                log.warning("clob feed error: %s; reconnecting in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
