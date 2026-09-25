"""Live price feeds: Coinbase ticker and Binance trades (fast, leading) and Polymarket RTDS Chainlink (the
settlement series). `feeds.momentum_source` picks which exchange drives the signal."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from .model import EwmaVol, PriceSeries, SecondBars
from .net import get_ctx

log = logging.getLogger("feeds")

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
RTDS_WS = "wss://ws-live-data.polymarket.com"
BINANCE_WS = ["wss://stream.binance.com:9443/stream?streams=",       # main site (blocked in some countries)
              "wss://data-stream.binance.vision/stream?streams="]   # Binance's market-data-only host (works there)
STALE_S = 10  # reconnect a feed that has been silent this long
FRESH_S = 5   # an exchange price older than this isn't used for the signal or the estimate
SOURCES = ("coinbase", "binance", "both")


class AssetPrices:
    """Everything the model needs for one underlying."""

    def __init__(self, asset: str, vol_halflife_s: float, vol_floor_bp: float, vol_change_s: int = 30,
                 vol_prior_bp: float = 0.5):
        self.asset = asset
        self.spot = PriceSeries()                 # Coinbase trades/ticker, keyed by local receive time
        self.chainlink = SecondBars()             # Chainlink prints keyed by their own second
        self.chainlink_local = PriceSeries()      # Chainlink prints keyed by local receive time (lag diagnostics)
        self.binance = PriceSeries()              # Binance BTC/USDT trades, keyed by local receive time
        self.vol = EwmaVol(vol_halflife_s, vol_floor_bp, vol_change_s, vol_prior_bp)
        self.tick = asyncio.Event()               # set on every signal-source tick: the signal loop reacts immediately
        self.source = "coinbase"                  # which exchange drives momentum, trend and the estimate

    def fast(self, now: float) -> list[PriceSeries]:
        """The exchange series behind the signal. If the chosen one has gone quiet, the other takes over, so a
        dropped feed doesn't stop trading; with neither fresh the caller falls back to Chainlink."""
        chosen = {"coinbase": [self.spot], "binance": [self.binance], "both": [self.spot, self.binance]}[self.source]
        fresh = lambda s: (last := s.last()) is not None and now - last[0] <= FRESH_S
        live = [s for s in chosen if fresh(s)]
        return live or [s for s in (self.spot, self.binance) if s not in chosen and fresh(s)]

    def move_bp(self, window_s: float, now: float) -> float | None:
        """Price move over the window (bp) on the signal source. With "both", the two exchanges must agree on the
        direction; the smaller move counts, and disagreement counts as no move."""
        moves = [m for s in self.fast(now) if (m := s.move_bp(window_s, now)) is not None]
        if not moves:
            return None
        if len(moves) == 1:
            return moves[0]
        a, b = moves
        return min(a, b, key=abs) if a * b > 0 else 0.0

    def estimate_now(self, now: float, max_adjust: float | None = None) -> float | None:
        """Best estimate of the Chainlink price right now: last print + the Coinbase move since that print.
        `max_adjust` caps that move (USD): Chainlink aggregates many venues, so a sudden Coinbase-only spike
        shouldn't be extrapolated in full."""
        if self.chainlink.last_sec is None:
            return None
        cl_sec = self.chainlink.last_sec
        cl_val = self.chainlink.v[cl_sec]
        moves = []
        for s in self.fast(now):                  # USDT and USD prices differ by a basis; their moves don't
            then = s.at(cl_sec + 0.999)
            if then is not None:
                moves.append(s.last()[1] - then)
        if not moves:
            return cl_val
        move = sum(moves) / len(moves)
        if max_adjust is not None:
            move = max(-max_adjust, min(max_adjust, move))
        return cl_val + move

    def momentum_bp(self, window_s: float, now: float) -> float | None:
        m = self.move_bp(window_s, now)
        if m is None:  # fall back to the (slower) Chainlink series
            m = self.chainlink_local.move_bp(window_s, now)
        return m


async def _pinger(ws, text: str, every: float) -> None:
    try:
        while True:
            await asyncio.sleep(every)
            await ws.send(text)
    except Exception:
        return


async def run_coinbase(assets: dict[str, AssetPrices], status: dict) -> None:
    products = {f"{a.upper()}-USD": p for a, p in assets.items()}
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(COINBASE_WS, ssl=get_ctx(), open_timeout=10, ping_interval=20) as ws:
                await ws.send(json.dumps({"type": "subscribe", "product_ids": list(products), "channels": ["ticker"]}))
                status["coinbase"] = "up"
                backoff = 1.0
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=STALE_S)  # TimeoutError → reconnect
                    m = json.loads(raw)
                    if m.get("type") == "ticker" and m.get("product_id") in products:
                        ap = products[m["product_id"]]
                        ap.spot.add(time.time(), float(m["price"]))
                        ap.tick.set()
        except Exception as e:
            status["coinbase"] = f"down ({type(e).__name__})"
            log.warning("coinbase feed error: %s; reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def apply_binance(symbols: dict[str, AssetPrices], raw: str | bytes, now: float) -> AssetPrices | None:
    """One combined-stream aggTrade message → the asset's Binance series. Returns the asset it updated."""
    m = json.loads(raw)
    d = m.get("data") or m
    ap = symbols.get(str(d.get("s", "")).lower())
    if ap is None or "p" not in d:
        return None
    ap.binance.add(now, float(d["p"]))
    return ap


async def run_binance(assets: dict[str, AssetPrices], status: dict) -> None:
    symbols = {f"{a}usdt": p for a, p in assets.items()}
    streams = "/".join(f"{s}@aggTrade" for s in symbols)
    backoff, host = 1.0, 0
    while True:
        url = BINANCE_WS[host % len(BINANCE_WS)] + streams
        connected = False
        try:
            async with websockets.connect(url, ssl=get_ctx(), open_timeout=10, ping_interval=20, max_size=2**22) as ws:
                connected = True
                status["binance"] = "up"
                backoff = 1.0
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=STALE_S)  # TimeoutError → reconnect
                    ap = apply_binance(symbols, raw, time.time())
                    if ap is not None and ap.source != "coinbase":
                        ap.tick.set()
        except Exception as e:
            status["binance"] = f"down ({type(e).__name__})"
            if not connected:
                host += 1                        # e.g. HTTP 451 where binance.com is blocked: try the other host
            log.warning("binance feed error: %s; reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def run_chainlink(assets: dict[str, AssetPrices], status: dict) -> None:
    by_symbol = {f"{a}/usd": p for a, p in assets.items()}
    subs = [{"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": s}, separators=(",", ":"))} for s in by_symbol]
    single = next(iter(by_symbol.values())) if len(by_symbol) == 1 else None
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(RTDS_WS, ssl=get_ctx(), open_timeout=10, ping_interval=None) as ws:
                await ws.send(json.dumps({"action": "subscribe", "subscriptions": subs}))
                pinger = asyncio.create_task(_pinger(ws, "PING", 5))
                status["chainlink"] = "up"
                backoff = 1.0
                last_data = time.time()
                try:
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            raw = None
                        if time.time() - last_data > STALE_S:
                            # the socket can stay open while the stream silently stops — force a reconnect
                            raise TimeoutError(f"no Chainlink print for {STALE_S}s")
                        if not raw or raw in ("PONG", "pong"):
                            continue
                        try:
                            m = json.loads(raw)
                        except ValueError:
                            continue
                        pl = m.get("payload") or {}
                        target = by_symbol.get(str(pl.get("symbol", "")).lower(), single)
                        if target is None:
                            continue
                        points = pl.get("data") if isinstance(pl.get("data"), list) else (
                            [pl] if "value" in pl and "timestamp" in pl else [])
                        now = time.time()
                        for pt in points:
                            sec = int(pt["timestamp"]) // 1000
                            val = float(pt["value"])
                            target.chainlink.add(sec, val)
                            target.vol.update(sec, val)
                        if points:
                            last_data = now
                        if points and len(points) == 1:
                            target.chainlink_local.add(now, float(points[-1]["value"]))
                finally:
                    pinger.cancel()
        except Exception as e:
            status["chainlink"] = f"down ({type(e).__name__})"
            log.warning("chainlink feed error: %s; reconnecting in %.0fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
