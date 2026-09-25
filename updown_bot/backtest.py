"""Backtest the strategy on the most recent BTC 5-minute markets, from public data only (no keys, no orders).

    python manage.py backtest                  # last 20 minutes = the 4 most recent resolved markets
    python manage.py backtest --minutes 120    # longer = more markets = a more reliable answer
    python manage.py backtest --minutes 480 --set strategy.min_edge=0.04   # try a setting without editing config
    python manage.py backtest --minutes 480 --compare-sources   # Coinbase vs Binance vs both as the signal source

It runs the bot's own model (bot/model.py) and signal rules (bot/strategy.py) with the settings in config.toml.

Data
  • Coinbase BTC-USD: every trade, millisecond timestamps → momentum, trend, volatility, the price estimate.
  • Binance BTC/USDT: every aggregated trade (when feeds.momentum_source uses Binance, or with --compare-sources).
    Both are keyed by the exchange's own clock, so this compares how good each signal is, not how fast each price
    reaches your PC — measure that with `python manage.py leadlag`.
  • Polymarket: each market's official start price ("price to beat") and final price → reference and winner,
    exact. Chainlink itself has no public history, so the Chainlink series is rebuilt as Coinbase + the offset
    between the two at the market's start, lagging 2 s (the median lag measured live).
  • Polymarket's trade tape (1-second timestamps) → the book. Someone buying Up at 0.55 means an Up ask of 0.55
    existed that second; someone selling Down at 0.40 means an Up ask of 0.60 (the two books mirror each other).

Fills
  An order sent at t arrives at t + latency. It fills if trades in that second show an ask at or below our limit
  (at that price); if every trade then was above the limit, it misses — someone faster took the cheap shares. With
  no trades in that second, the last ask seen is assumed unchanged. "instant" fills at the ask the signal saw,
  which is roughly what paper mode assumes. Trades are timestamped to the second, so a cheap ask traded earlier in
  the arrival second still counts for us: short latencies look better than they are. Live Sep 25 filled 29% of
  orders; if a row here fills far more than your live runs do, trust the live number.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import copy
import datetime as dt
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bot.book import Book  # noqa: E402
from bot.config import load_config  # noqa: E402
from bot.feeds import SOURCES, AssetPrices  # noqa: E402
from bot.model import PriceSeries, SecondBars, fair_value, taker_fee_per_share  # noqa: E402
from bot.net import get_json, ssl_context  # noqa: E402
from bot.strategy import evaluate  # noqa: E402

CHAINLINK_LAG_S = 2
WARMUP_S = 900            # Coinbase history before the first market, so volatility is measured (≥ 330 s needed)
STEP_S = 0.1              # the live bot re-evaluates on every Coinbase tick, at most ~10×/s
LATENCIES = [("instant", None), ("0.3 s", 0.3), ("0.7 s", 0.7), ("1.5 s", 1.5)]
CACHE = HERE / "data" / "backtest_cache"   # resolved markets never change: each is downloaded once
PARALLEL = 4                               # Polymarket answers 429 "Too Many Requests" to bigger bursts


@dataclass
class Mkt:
    slug: str
    start: int
    end: int
    condition_id: str
    ref: float
    final: float
    winner: str
    asks: dict = field(default_factory=lambda: {"Up": {}, "Down": {}})   # second → lowest ask seen
    n_trades: int = 0
    cached: bool = False


# ---------------------------------------------------------------- data
async def polite_get(url: str) -> object:
    """GET with patient retries: rate limits (429) and hiccups get a growing pause instead of a crash."""
    last: Exception | None = None
    for wait in (0, 2, 5, 10, 20, 40):
        if wait:
            await asyncio.sleep(wait)
        try:
            return await get_json(url, retries=1)
        except ConnectionError as e:
            last = e
    raise ConnectionError(f"gave up after several retries (network or rate limit): {last}")


def _cache_path(slug: str) -> Path:
    return CACHE / f"{slug}.json"


def load_cached(slug: str) -> Mkt | None:
    try:
        d = json.loads(_cache_path(slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    m = Mkt(**{k: v for k, v in d.items() if k != "asks"})
    m.asks = {side: {int(k): v for k, v in book.items()} for side, book in d["asks"].items()}
    return m


def save_cached(m: Mkt) -> None:
    if time.time() - m.end < 600:            # the trade feed can lag a few minutes behind the market
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    _cache_path(m.slug).write_text(json.dumps({**m.__dict__}), encoding="utf-8")


async def fetch_markets(n: int) -> list[Mkt]:
    now = int(time.time())
    start = now - now % 300 - 300           # the most recent market that has ended
    out: list[Mkt] = []
    tries = 0
    while len(out) < n and tries < n + 4:
        tries += 1
        start_i = start
        start -= 300
        cached = load_cached(f"btc-updown-5m-{start_i}")
        if cached is not None:
            cached.cached = True
            out.append(cached)
            continue
        ev = await polite_get(f"https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{start_i}")
        if not ev:
            continue
        meta = ev[0].get("eventMetadata") or {}
        if meta.get("finalPrice") is None or meta.get("priceToBeat") is None:
            continue                        # not resolved yet
        m = ev[0]["markets"][0]
        ref, fin = float(meta["priceToBeat"]), float(meta["finalPrice"])
        out.append(Mkt(f"btc-updown-5m-{start_i}", start_i, start_i + 300, m["conditionId"], ref, fin,
                       "Up" if fin >= ref else "Down"))
    return sorted(out, key=lambda m: m.start)


async def fetch_trades(m: Mkt) -> None:
    """Polymarket taker trades → lowest ask per second for each side."""
    offset = 0
    while True:
        page = await polite_get(f"https://data-api.polymarket.com/trades?market={m.condition_id}&limit=500&offset={offset}")
        if not page:
            break
        for t in page:
            sec, px, out = int(t["timestamp"]), float(t["price"]), t.get("outcome")
            if out not in ("Up", "Down") or not 0 < px < 1:
                continue
            other = "Down" if out == "Up" else "Up"
            side, p = (out, px) if t.get("side") == "BUY" else (other, round(1 - px, 4))
            book = m.asks[side]
            book[sec] = min(book.get(sec, 9.0), p)
            m.n_trades += 1
        if len(page) < 500 or offset >= 20000:
            break
        offset += 500
    save_cached(m)


async def fetch_coinbase(since: float) -> list[tuple[float, float]]:
    trades: list[tuple[float, float]] = []
    url = "https://api.exchange.coinbase.com/products/BTC-USD/trades?limit=1000"
    page = await polite_get(url)
    while page:
        for t in page:
            ts = dt.datetime.fromisoformat(t["time"].replace("Z", "+00:00")).timestamp()
            trades.append((ts, float(t["price"])))
        oldest = min(int(t["trade_id"]) for t in page)
        if trades and min(x[0] for x in trades[-len(page):]) < since:
            break
        page = await polite_get(f"{url}&after={oldest}")
    trades.sort()
    return [x for x in trades if x[0] >= since]


BINANCE_REST = "https://data-api.binance.vision"   # Binance's market-data host: works where binance.com is blocked


async def _binance_hour(h: int) -> list[tuple[float, float]]:
    """All BTC/USDT aggregated trades in the hour starting at unix time h (cached once the hour is over)."""
    path = CACHE / f"binance_BTCUSDT_{h}.json"
    try:
        return [tuple(x) for x in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError):
        pass
    out: list[tuple[float, float]] = []
    url = f"{BINANCE_REST}/api/v3/aggTrades?symbol=BTCUSDT&limit=1000"
    page = await polite_get(f"{url}&startTime={h * 1000}&endTime={(h + 3600) * 1000 - 1}")
    while page:
        out += [(t["T"] / 1000, float(t["p"])) for t in page if t["T"] < (h + 3600) * 1000]
        if len(page) < 1000 or page[-1]["T"] >= (h + 3600) * 1000:
            break
        page = await polite_get(f"{url}&fromId={page[-1]['a'] + 1}")
    if time.time() > h + 3600 + 600:
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out), encoding="utf-8")
    return out


async def fetch_binance(since: float, until: float) -> list[tuple[float, float]]:
    hours = range(int(since) // 3600 * 3600, int(until) + 1, 3600)
    sem = asyncio.Semaphore(PARALLEL)

    async def one(h):
        async with sem:
            return await _binance_hour(h)
    parts = await asyncio.gather(*(one(h) for h in hours))
    return sorted(x for part in parts for x in part if since <= x[0] <= until)


# ---------------------------------------------------------------- simulation
def second_closes(trades: list[tuple[float, float]]) -> dict[int, float]:
    closes: dict[int, float] = {}
    for ts, px in trades:
        closes[int(ts)] = px
    lo, hi = min(closes), max(closes)
    last = closes[lo]
    for s in range(lo, hi + 1):            # forward-fill quiet seconds
        last = closes.get(s, last)
        closes[s] = last
    return closes


def ask_at(m: Mkt, side: str, sec: int, max_back: int) -> tuple[float | None, int | None]:
    for d in range(max_back + 1):
        p = m.asks[side].get(sec - d)
        if p is not None:
            return p, sec - d
    return None, None


def simulate(cfg, markets: list[Mkt], trades, closes, latency: float | None, binance=None) -> dict:
    sc, rk = cfg.strategy, cfg.risk
    fee_rate, tick, slip = cfg.execution.fee_rate, 0.01, cfg.execution.max_slippage
    res = {"signals": 0, "fills": [], "missed": 0, "unknown_book": 0}
    equity = cfg.account.starting_equity
    for m in markets:
        # the bot's own price object: the same source choice, momentum, trend and estimate code as live
        ap = AssetPrices("btc", cfg.model.vol_halflife_s, cfg.model.vol_floor_bp, cfg.model.vol_change_s,
                         cfg.model.vol_prior_bp)
        ap.source = cfg.feeds.momentum_source
        ap.spot, ap.binance = PriceSeries(max_age_s=3600), PriceSeries(max_age_s=3600)
        bars = ap.chainlink = SecondBars()
        vol = ap.vol
        basis = m.ref - sum(closes[s] for s in range(m.start - 59, m.start + 1)) / 60   # Chainlink − Coinbase
        vol_sec = min(closes)
        bn = binance or []
        i = bisect.bisect_left(trades, (m.start - 120,))
        j = bisect.bisect_left(bn, (m.start - 120,))
        spent = 0.0
        last_sent: dict[str, float] = {}
        busy_until: dict[str, float] = {}
        n_fills = 0
        t = m.start + sc.min_seconds_elapsed
        while t < m.end - sc.min_seconds_left:
            while i < len(trades) and trades[i][0] <= t:
                ap.spot.add(*trades[i])
                i += 1
            while j < len(bn) and bn[j][0] <= t:
                ap.binance.add(*bn[j])
                j += 1
            cl_sec = int(t) - CHAINLINK_LAG_S
            while vol_sec <= cl_sec:
                vol.update(vol_sec, closes[vol_sec])      # 30 s changes: the offset doesn't matter
                bars.add(vol_sec, closes[vol_sec] + basis)
                vol_sec += 1
            t_next = round(t + STEP_S, 3)
            if not vol.ready:
                t = t_next
                continue
            cl_val = bars.v[cl_sec]
            sigma = vol.sigma(cl_val)
            cap = cfg.model.max_spot_adjust_sigma * sigma * math.sqrt(t - cl_sec + 1)
            x = ap.estimate_now(t, max_adjust=cap)
            fv = fair_value(reference=m.ref, x_now=x, now_sec=int(t), end_sec=m.end, lookback=60, realized=bars,
                            sigma=sigma, basis_sigma=cfg.model.basis_sigma_usd)
            books = {}
            for side in ("Up", "Down"):
                p, sec = ask_at(m, side, int(t) - 1, max_back=int(sc.max_book_age_s))   # only completed seconds
                b = None
                if p is not None:
                    b = Book()
                    b.asks = {p: 1e9}
                    b.updated = sec + 1.0
                books[side] = b
            intent, _ = evaluate(sc, p_up=fv.p_up, momentum_bp=ap.momentum_bp(sc.momentum_window_s, t), books=books,
                                 now=t, seconds_left=m.end - t, seconds_elapsed=t - m.start, fee_rate=fee_rate,
                                 tick=tick, max_slippage=slip, trend_bp=ap.move_bp(sc.trend_window_s, t))
            if intent is None or t < busy_until.get(intent.outcome, 0) or \
                    t - last_sent.get(intent.outcome, -99) < sc.side_cooldown_s or n_fills >= sc.max_orders_per_market:
                t = t_next
                continue
            cap_usd = min(equity * rk.max_market_exposure_pct, rk.max_market_usd or 1e9)
            unit = intent.limit + taker_fee_per_share(intent.limit, fee_rate)
            shares = min(rk.max_shares_per_order or 1e9, math.floor(equity * rk.clip_pct_equity / unit * 100) / 100)
            shares = max(shares, rk.min_shares)
            if spent + shares * unit > cap_usd:
                t = t_next
                continue
            res["signals"] += 1
            last_sent[intent.outcome] = t
            # --- where does the order meet the book?
            if latency is None:
                price = intent.ask
            else:
                arrive = t + latency
                busy_until[intent.outcome] = arrive
                price = m.asks[intent.outcome].get(int(arrive))
                if price is None:
                    res["unknown_book"] += 1
                    price, _ = ask_at(m, intent.outcome, int(arrive), max_back=3)
                if price is None or price > intent.limit + 1e-9:
                    res["missed"] += 1
                    t = t_next
                    continue
            amount = shares * intent.limit                 # FAK market BUY: spend up to shares × limit
            got = amount / price
            fee = taker_fee_per_share(price, fee_rate) * got
            cost = amount + fee
            won = intent.outcome == m.winner
            pnl = (got if won else 0.0) - cost
            spent += cost
            n_fills += 1
            res["fills"].append({"slug": m.slug, "t": t, "side": intent.outcome, "price": price, "shares": got,
                                 "fair": intent.fair, "edge": intent.edge, "won": won, "pnl": pnl, "cost": cost})
            t = t_next
        equity += sum(f["pnl"] for f in res["fills"] if f["slug"] == m.slug)
    return res


# ---------------------------------------------------------------- report
def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, default=20, help="how far back (rounded to whole 5-minute markets)")
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--fills", action="store_true", help="list every simulated fill")
    ap.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                    help="try a setting without editing config.toml, e.g. --set strategy.min_edge=0.04 (repeatable)")
    ap.add_argument("--compare-sources", action="store_true",
                    help="run Coinbase, Binance and both (must agree) as the signal source, side by side")
    args = ap.parse_args()
    cfg = load_config(args.config)
    for item in args.set:
        key, _, value = item.partition("=")
        section, _, name = key.partition(".")
        obj = getattr(cfg, section, None)
        if obj is None or not hasattr(obj, name) or not value:
            sys.exit(f"--set {item}: expected SECTION.KEY=VALUE with a key from config.toml, e.g. strategy.min_edge=0.04")
        old = getattr(obj, name)
        setattr(obj, name, type(old)(value) if not isinstance(old, bool) else value.lower() in ("1", "true", "yes"))
        print(f"override: {section}.{name} = {getattr(obj, name)} (config.toml: {old})")
    ssl_context(cfg.feeds.ca_bundle, cfg.feeds.relax_x509_strict)
    n = max(1, args.minutes // 5)

    async def load():
        print(f"Loading the last {n} resolved BTC 5-minute markets …")
        markets = await fetch_markets(n)
        todo = [m for m in markets if not m.cached]
        sem = asyncio.Semaphore(PARALLEL)

        async def one(m):
            async with sem:
                await fetch_trades(m)
        if todo:
            print(f"  downloading trades for {len(todo)} markets ({len(markets) - len(todo)} already saved) …")
        await asyncio.gather(*(one(m) for m in todo))
        print(f"Loading Coinbase BTC trades since {WARMUP_S // 60} min before the first market …")
        trades = await fetch_coinbase(markets[0].start - WARMUP_S)
        binance = None
        if args.compare_sources or cfg.feeds.momentum_source != "coinbase":
            print("Loading Binance BTC/USDT trades …")
            binance = await fetch_binance(markets[0].start - WARMUP_S, markets[-1].end)
        return markets, trades, binance
    markets, trades, binance = asyncio.run(load())
    closes = second_closes(trades)

    print(f"\n{'market (UTC)':14} {'start price':>12} {'final':>12}  winner  Polymarket trades")
    for m in markets:
        print(f"{time.strftime('%H:%M', time.gmtime(m.start))}-{time.strftime('%H:%M', time.gmtime(m.end))}    "
              f"{m.ref:12,.2f} {m.final:12,.2f}  {m.winner:6}  {m.n_trades:,}")
    print(f"Coinbase: {len(trades):,} trades" + (f"   Binance: {len(binance):,} trades" if binance else ""))
    print(f"Settings: {cfg.risk.max_shares_per_order:g} shares/order, ${cfg.risk.max_market_usd:g}/market cap, "
          f"min edge {cfg.strategy.min_edge * 100:.0f}c, max edge {cfg.strategy.max_edge * 100:.0f}c, "
          f"momentum ≥ {cfg.strategy.min_momentum_bp} bp over {cfg.strategy.momentum_window_s:g} s, "
          f"signal source {'compared' if args.compare_sources else cfg.feeds.momentum_source}")

    if args.compare_sources:
        print(f"\n{'signal source':14} {'latency':8} {'orders':>6} {'filled':>7} {'fill %':>7} {'won':>6} {'P&L':>9} "
              f"{'per share':>10}")
        for src in SOURCES:
            c = copy.deepcopy(cfg)
            c.feeds.momentum_source = src
            for label, lat in LATENCIES:
                r = simulate(c, markets, trades, closes, lat, binance)
                f = r["fills"]
                sh = sum(x["shares"] for x in f)
                pnl = sum(x["pnl"] for x in f)
                print(f"{src:14} {label:8} {r['signals']:6d} {len(f):7d} {len(f) / max(r['signals'], 1):7.0%} "
                      f"{sum(x['won'] for x in f) / max(len(f), 1):6.0%} {pnl:+9.2f} {pnl / sh * 100 if sh else 0:+9.1f}c")
            print()
        print("Prices are keyed by each exchange's own clock: this compares signal quality. Which price reaches "
              "your PC first\nis a separate question — measure it there with `python manage.py leadlag`.")
        return

    print(f"\n{'order latency':14} {'orders':>6} {'filled':>7} {'fill %':>7} {'won':>7} {'spent':>8} {'P&L':>8} "
          f"{'per share':>10}  {'model expected':>14}")
    results = {}
    for label, lat in LATENCIES:
        r = simulate(cfg, markets, trades, closes, lat, binance)
        results[label] = r
        f = r["fills"]
        sh = sum(x["shares"] for x in f)
        pnl = sum(x["pnl"] for x in f)
        spent = sum(x["cost"] for x in f)
        exp = sum(x["shares"] * (x["fair"] - x["price"] - taker_fee_per_share(x["price"], cfg.execution.fee_rate))
                  for x in f)
        wins = sum(x["won"] for x in f)
        print(f"{label:14} {r['signals']:6d} {len(f):7d} {len(f) / max(r['signals'], 1):7.0%} "
              f"{f'{wins}/{len(f)}':>7} {spent:8.2f} {pnl:+8.2f} {pnl / sh * 100 if sh else 0:+9.1f}c  {exp:+14.2f}")
    print("\nPer market (0.3 s latency):")
    by = defaultdict(list)
    for x in results["0.3 s"]["fills"]:
        by[x["slug"]].append(x)
    for m in markets:
        f = by.get(m.slug, [])
        ups, downs = sum(x["side"] == "Up" for x in f), sum(x["side"] == "Down" for x in f)
        print(f"  {time.strftime('%H:%M', time.gmtime(m.start))}  winner {m.winner:4}  fills {len(f):2d} "
              f"(Up {ups}, Down {downs})  P&L {sum(x['pnl'] for x in f):+.2f}")
    if args.fills:
        print("\nFills (0.3 s):")
        for x in results["0.3 s"]["fills"]:
            print(f"  {time.strftime('%H:%M:%S', time.gmtime(x['t']))} {x['side']:4} {x['shares']:.2f} sh @ {x['price']:.2f} "
                  f"fair {x['fair']:.3f} edge {x['edge'] * 100:+.1f}c  {'WON' if x['won'] else 'lost'} {x['pnl']:+.2f}")
    if len(markets) < 24:
        print(f"\n{len(markets)} markets is a small sample: one market can swing the total by several dollars. "
              "Use --minutes 120 or more before drawing conclusions.")
    print("Fills are credited if trades in the arrival second show a low enough ask; trades are timestamped to the "
          "second,\nso the 0.3-0.7 s rows are optimistic and the 1.5 s row is closer to a strict reading.")


if __name__ == "__main__":
    main()
