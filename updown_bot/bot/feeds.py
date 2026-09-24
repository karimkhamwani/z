"""Live price feeds: Coinbase ticker (fast, leading) and Polymarket RTDS Chainlink (the settlement series)."""
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
STALE_S = 10  # reconnect a feed that has been silent this long


class AssetPrices:
    """Everything the model needs for one underlying."""

    def __init__(self, asset: str, vol_halflife_s: float, vol_floor_bp: float, vol_change_s: int = 30,
                 vol_prior_bp: float = 0.5):
        self.asset = asset
        self.spot = PriceSeries()                 # Coinbase trades/ticker, keyed by local receive time
        self.chainlink = SecondBars()             # Chainlink prints keyed by their own second
        self.chainlink_local = PriceSeries()      # Chainlink prints keyed by local receive time (lag diagnostics)
        self.vol = EwmaVol(vol_halflife_s, vol_floor_bp, vol_change_s, vol_prior_bp)

    def estimate_now(self, now: float) -> float | None:
        """Best estimate of the Chainlink price right now: last print + the Coinbase move since that print."""
        if self.chainlink.last_sec is None:
            return None
        cl_sec = self.chainlink.last_sec
        cl_val = self.chainlink.v[cl_sec]
        spot_last = self.spot.last()
        if spot_last is None or now - spot_last[0] > 5:
            return cl_val
        spot_then = self.spot.at(cl_sec + 0.999)
        if spot_then is None:
            return cl_val
        return cl_val + (spot_last[1] - spot_then)

    def momentum_bp(self, window_s: float, now: float) -> float | None:
        m = self.spot.move_bp(window_s, now)
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
                        products[m["product_id"]].spot.add(time.time(), float(m["price"]))
        except Exception as e:
            status["coinbase"] = f"down ({type(e).__name__})"
            log.warning("coinbase feed error: %s; reconnecting in %.0fs", e, backoff)
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
