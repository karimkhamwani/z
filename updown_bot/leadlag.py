"""Which BTC price feed gives THIS computer the earliest warning before Polymarket reprices? (no keys, no orders)

    python manage.py leadlag              # records 5 minutes, then reports
    python manage.py leadlag --minutes 10

It records Coinbase and Binance prices as they arrive here, plus the Polymarket order book for the current BTC
5-minute market (by Polymarket's own timestamps), then measures:
  1. which exchange's price reaches this computer first, and
  2. how long after each exchange's move Polymarket's price follows, and how closely.
Run it on the machine that runs the bot: the answer depends on where that machine is. Absolute delays include
this computer's clock error; the difference between Coinbase and Binance doesn't.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import statistics as st
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import websockets  # noqa: E402

from bot.book import CLOB_WS  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.feeds import BINANCE_WS, COINBASE_WS  # noqa: E402
from bot.markets import fetch_market  # noqa: E402
from bot.net import get_ctx, ssl_context  # noqa: E402

GRID = 0.05        # seconds
HORIZON = 0.5      # compare 0.5-second moves


async def record(seconds: float) -> dict:
    data = {"coinbase": [], "binance": [], "poly": [], "binance_host": None, "errors": {}}
    stop = time.time() + seconds

    async def coinbase():
        async with websockets.connect(COINBASE_WS, ssl=get_ctx(), max_size=2**22) as ws:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}))
            while time.time() < stop:
                m = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if m.get("type") == "ticker":
                    data["coinbase"].append((time.time(), float(m["price"])))

    async def binance():
        last: Exception | None = None
        for host in BINANCE_WS:                                   # main site first, market-data host if blocked
            try:
                async with websockets.connect(host + "btcusdt@aggTrade", ssl=get_ctx(), max_size=2**22) as ws:
                    data["binance_host"] = host.split("/")[2]
                    while time.time() < stop:
                        d = json.loads(await asyncio.wait_for(ws.recv(), 15))
                        d = d.get("data") or d
                        if "p" in d:
                            data["binance"].append((time.time(), float(d["p"])))
                    return
            except Exception as e:
                last = e
                if data["binance_host"]:
                    raise
        raise ConnectionError(f"Binance unreachable on every host: {last}")

    async def poly():
        while time.time() < stop:
            now = int(time.time())
            m = await fetch_market("btc", "5m", now - now % 300)
            end = min(m.start + 300, stop)
            bids: dict[float, float] = {}
            asks: dict[float, float] = {}
            async with websockets.connect(CLOB_WS, ssl=get_ctx(), max_size=2**24, max_queue=4096, ping_interval=None) as ws:
                await ws.send(json.dumps({"assets_ids": [m.up_token], "type": "market"}))
                while time.time() < end:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), 2)
                    except asyncio.TimeoutError:
                        continue
                    if raw == "PONG":
                        continue
                    msg = json.loads(raw)
                    for e in (msg if isinstance(msg, list) else [msg]):
                        et = e.get("event_type")
                        if et == "book" and e.get("asset_id") == m.up_token:
                            bids = {float(x["price"]): float(x["size"]) for x in e["bids"] if float(x["size"]) > 0}
                            asks = {float(x["price"]): float(x["size"]) for x in e["asks"] if float(x["size"]) > 0}
                        elif et == "price_change":
                            for c in e["price_changes"]:
                                if c["asset_id"] != m.up_token:
                                    continue
                                side = bids if c["side"] == "BUY" else asks
                                p, sz = float(c["price"]), float(c["size"])
                                if sz <= 0:
                                    side.pop(p, None)
                                else:
                                    side[p] = sz
                        else:
                            continue
                        if bids and asks and e.get("timestamp"):
                            data["poly"].append((int(e["timestamp"]) / 1000, (max(bids) + min(asks)) / 2, m.start))

    async def guard(name, coro):
        try:
            await coro
        except Exception as e:
            data["errors"][name] = f"{type(e).__name__}: {e}"[:160]

    await asyncio.gather(guard("coinbase", coinbase()), guard("binance", binance()), guard("polymarket", poly()))
    return data


def analyse(data: dict) -> None:
    feeds = ("coinbase", "binance")
    for k in feeds + ("poly",):
        if len(data[k]) < 100:
            print(f"Not enough {k} data ({len(data[k])} updates){': ' + data['errors'].get(k, '') if data['errors'].get(k) else ''}")
            return
    t0 = max(data[k][0][0] for k in feeds + ("poly",)) + 5
    t1 = min(data[k][-1][0] for k in feeds + ("poly",)) - 5
    grid = [t0 + i * GRID for i in range(int((t1 - t0) / GRID))]

    def on_grid(rows, col):
        ts = [r[0] for r in rows]
        return [rows[i][col] if (i := bisect.bisect_right(ts, g) - 1) >= 0 else None for g in grid]
    k = int(HORIZON / GRID)
    rets = {f: [math.log(x[i] / x[i - k]) if i >= k and x[i] and x[i - k] else None
                for i in range(len(x))] for f, x in ((f, on_grid(data[f], 1)) for f in feeds)}
    mid, mkt = on_grid(data["poly"], 1), on_grid(data["poly"], 2)
    dmid = [mid[i] - mid[i - k] if i >= k and mid[i] is not None and mid[i - k] is not None and mkt[i] == mkt[i - k]
            else None for i in range(len(mid))]

    def corr(a, b):
        p = [(u, v) for u, v in zip(a, b) if u is not None and v is not None]
        if len(p) < 100:
            return float("nan")
        ma, mb = st.mean(u for u, _ in p), st.mean(v for _, v in p)
        va, vb = sum((u - ma) ** 2 for u, _ in p), sum((v - mb) ** 2 for _, v in p)
        return sum((u - ma) * (v - mb) for u, v in p) / math.sqrt(va * vb) if va and vb else float("nan")

    def shifted(x, n):   # x as it was n grid steps earlier
        return [x[i - n] if 0 <= i - n < len(x) else None for i in range(len(x))]

    print(f"\nRecorded {t1 - t0:.0f} s: Coinbase {len(data['coinbase']):,} updates, Binance {len(data['binance']):,} "
          f"(via {data['binance_host']}), Polymarket {len(data['poly']):,}")
    lead = max((corr(rets["coinbase"], shifted(rets["binance"], n)), n * GRID) for n in range(-30, 31))
    who = "Binance" if lead[1] > 0 else "Coinbase"
    print("\n1. Which price arrives here first?")
    print(f"   {f'{who} arrives {abs(lead[1]) * 1000:.0f} ms earlier' if lead[1] else 'Both arrive at about the same time'} "
          f"(match between the two: {lead[0]:.2f})")
    print("\n2. How long after each exchange moves does Polymarket's Up price follow?")
    res = {}
    for f in feeds:
        best = max((corr(dmid, shifted(rets[f], n)), n * GRID) for n in range(0, 41))
        res[f] = best
        print(f"   {f.capitalize():9} Polymarket follows {best[1] * 1000:4.0f} ms later   (match {best[0]:.2f})")
    gain = (res["binance"][1] - res["coinbase"][1]) * 1000
    print("\nOn this computer:")
    print(f"   Binance gives {abs(gain):.0f} ms {'MORE' if gain > 0 else 'LESS'} warning than Coinbase, and Polymarket "
          f"follows it {'more' if res['binance'][0] > res['coinbase'][0] else 'less'} closely "
          f"({res['binance'][0]:.2f} vs {res['coinbase'][0]:.2f}).")
    if gain >= 0 and res["binance"][0] >= res["coinbase"][0]:
        print('   → Binance looks better here: try momentum_source = "binance" (or "both") in shadow mode.')
    elif gain < 0 and res["binance"][0] > res["coinbase"][0]:
        print('   → Mixed: Binance is the better signal but reaches you later. "both" is the cautious choice.')
    else:
        print('   → Coinbase looks better here: keep momentum_source = "coinbase".')
    print("   Five minutes is a small sample: run it two or three times, at different hours, before switching.")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    args = ap.parse_args()
    cfg = load_config(args.config)
    ssl_context(cfg.feeds.ca_bundle, cfg.feeds.relax_x509_strict)
    print(f"Recording Coinbase, Binance and the Polymarket book for {args.minutes:g} minutes …")
    data = asyncio.run(record(args.minutes * 60))
    for name, err in data["errors"].items():
        print(f"  {name}: {err}")
    analyse(data)


if __name__ == "__main__":
    main()
