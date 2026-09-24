"""Orchestrator: market rollover, signal loop, paper execution, settlement, rebates, snapshots, status."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from .book import BookFeed
from .config import Config
from .feeds import AssetPrices, run_chainlink, run_coinbase
from .ledger import Ledger
from .markets import Market, TF_SECONDS, fetch_market, fetch_winner, window_start
from .model import fair_value
from .paper import PaperExecutor, Portfolio, credit_rebates, rebate_rate, settle, utc_day
from .risk import RiskManager
from .strategy import evaluate

log = logging.getLogger("engine")
RESOLVE_GIVE_UP_S = 20 * 60
MAX_CHAINLINK_AGE_S = 10


class Engine:
    def __init__(self, cfg: Config, fresh: bool = False, run_seconds: float | None = None):
        self.cfg = cfg
        self.run_seconds = run_seconds
        self.data = Path(cfg.logging.data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data / "portfolio.json"
        if self.state_path.exists() and not fresh:
            self.pf = Portfolio.from_json(self.state_path.read_text())
            log.info("resumed portfolio: equity %.2f, %d open positions", self.pf.equity, len(self.pf.positions))
        else:
            e = cfg.account.starting_equity
            self.pf = Portfolio(cash=e, starting_equity=e, peak_equity=e)
        self.ledger = Ledger(self.data / "paper.db")
        self.status: dict[str, str] = {"coinbase": "off", "chainlink": "connecting", "clob": "connecting"}
        self.prices = {a: AssetPrices(a, cfg.model.vol_halflife_s, cfg.model.vol_floor_bp, cfg.model.vol_change_s,
                                  cfg.model.vol_prior_bp) for a in cfg.markets.assets}
        self.books = BookFeed(self.status)
        self.risk = RiskManager(cfg.risk, self.pf)
        self.exec = PaperExecutor(self.pf, cfg.execution.latency_ms, cfg.execution.liquidity_haircut,
                                  cfg.execution.fee_rate, cfg.risk.min_shares)
        self.markets: dict[str, Market] = {}          # slug -> market (current + next per asset)
        self.refs: dict[str, float] = {}              # slug -> TWAP reference at start
        self.inflight: set[tuple[str, str]] = set()   # (slug, outcome) orders being executed
        self.last_order: dict[tuple[str, str], float] = {}
        self.pending: dict[str, dict] = {}            # condition_id -> {slug, end, first_try}
        self.stats = {"signals": 0, "fills": 0, "rejects": 0, "settled": 0}
        self.started = time.time()
        for cid, pos in self.pf.positions.items():   # positions restored from disk still need settlement
            self.pending[cid] = {"slug": pos.slug, "end": pos.end, "model_winner": None}

    # ---------- market management ----------
    async def market_loop(self) -> None:
        tf = self.cfg.markets.timeframe
        dur = TF_SECONDS[tf]
        while True:
            now = time.time()
            want = set()
            for asset in self.cfg.markets.assets:
                s = window_start(now, tf)
                for start in (s, s + dur):
                    slug = f"{asset}-updown-{tf}-{start}"
                    want.add(slug)
                    if slug not in self.markets:
                        try:
                            m = await fetch_market(asset, tf, start)
                            if m:
                                self.markets[slug] = m
                        except Exception as e:
                            log.debug("market fetch %s failed: %s", slug, e)
            for slug in list(self.markets):
                m = self.markets[slug]
                if slug not in want and now > m.end:
                    self._close_market(m)
                    del self.markets[slug]
                    self.refs.pop(slug, None)
            self.books.set_tokens({t for m in self.markets.values() for t in (m.up_token, m.down_token)})
            await asyncio.sleep(1.0)

    def _close_market(self, m: Market) -> None:
        if m.condition_id in self.pf.positions and m.condition_id not in self.pending:
            self.pending[m.condition_id] = {"slug": m.slug, "end": m.end, "asset": m.asset,
                                            "lookback": m.twap_lookback, "ref": self.refs.get(m.slug),
                                            "model_winner": None}

    def _model_winner(self, info: dict) -> str | None:
        """Our own settlement estimate from the Chainlink series (logged next to the official result)."""
        if info.get("model_winner") or info.get("ref") is None or info.get("asset") not in self.prices:
            return info.get("model_winner")
        final = self.prices[info["asset"]].chainlink.twap(info["end"], info["lookback"])
        if final is not None:
            info["model_winner"] = "Up" if final >= info["ref"] else "Down"
        return info.get("model_winner")

    def _reference(self, m: Market) -> float | None:
        if m.slug not in self.refs:
            r = self.prices[m.asset].chainlink.twap(m.start, m.twap_lookback)
            if r is not None:
                self.refs[m.slug] = r
        return self.refs.get(m.slug)

    def _fair(self, m: Market, now: float):
        p = self.prices[m.asset]
        if p.chainlink.last_sec is None or now - p.chainlink.last_sec > MAX_CHAINLINK_AGE_S:
            return None  # never price off a stale settlement feed
        ref = self._reference(m)
        x = p.estimate_now(now)
        if ref is None or x is None:
            return None
        return fair_value(reference=ref, x_now=x, now_sec=int(now), end_sec=m.end, lookback=m.twap_lookback,
                          realized=p.chainlink, sigma=p.vol.sigma(x), basis_sigma=self.cfg.model.basis_sigma_usd)

    # ---------- trading ----------
    async def signal_loop(self) -> None:
        sc = self.cfg.strategy
        while True:
            await asyncio.sleep(0.1)
            now = time.time()
            if self.risk.check_breakers():
                continue
            for m in list(self.markets.values()):
                if not (m.start <= now < m.end):
                    continue
                fv = self._fair(m, now)
                if fv is None:
                    continue
                p = self.prices[m.asset]
                mom = p.momentum_bp(sc.momentum_window_s, now)
                books = {"Up": self.books.book(m.up_token), "Down": self.books.book(m.down_token)}
                intent, rejs = evaluate(sc, p_up=fv.p_up, momentum_bp=mom, books=books, now=now,
                                        seconds_left=m.end - now, seconds_elapsed=now - m.start,
                                        fee_rate=m.fee_rate, tick=m.tick, max_slippage=self.cfg.execution.max_slippage)
                for r in rejs:
                    key = (m.slug, r.outcome, r.reason)
                    if now - self.last_order.get(key, 0) > 1.0:   # log each reason at most 1/s per side
                        self.last_order[key] = now
                        self.stats["rejects"] += 1
                        self.ledger.rejection({"ts": now, "slug": m.slug, "outcome": r.outcome, "reason": r.reason,
                                               "fair": r.fair, "ask": r.ask, "edge": r.edge, "momentum_bp": r.momentum_bp,
                                               "seconds_left": m.end - now})
                if intent is None:
                    continue
                key = (m.slug, intent.outcome)
                pos = self.pf.positions.get(m.condition_id)
                if key in self.inflight or now - self.last_order.get(key, 0) < sc.side_cooldown_s:
                    continue
                if pos and pos.orders >= sc.max_orders_per_market:
                    continue
                shares = self.risk.clip_shares(m.condition_id, intent.limit, m.min_size, m.fee_rate)
                if shares <= 0:
                    continue
                reserve = shares * (intent.limit + m.fee_rate * intent.limit * (1 - intent.limit))
                self.risk.reserve(m.condition_id, reserve)
                self.stats["signals"] += 1
                self.inflight.add(key)
                self.last_order[key] = now
                ctx = {"now": now, "fair": intent.fair, "momentum_bp": intent.momentum_bp,
                       "seconds_left": m.end - now, "ask": intent.ask}
                asyncio.create_task(self._execute(m, intent, shares, ctx, reserve))

    async def _execute(self, m: Market, intent, shares: float, ctx: dict, reserve: float) -> None:
        key = (m.slug, intent.outcome)
        try:
            fill = await self.exec.buy(market=m, outcome=intent.outcome, limit=intent.limit, max_shares=shares,
                                       get_book=self.books.book, context=ctx)
            if fill:
                self.stats["fills"] += 1
                self.ledger.fill({**asdict(fill), "mode": "paper"})
                log.info("FILL %s %s %.2f sh @ %.3f (fair %.3f, edge %+.3f, mom %+.2fbp, %ds left) cash %.2f",
                         m.slug, fill.outcome, fill.shares, fill.avg_price, fill.fair, fill.edge, fill.momentum_bp,
                         fill.seconds_left, fill.cash)
            else:
                self.ledger.rejection({"ts": time.time(), "slug": m.slug, "outcome": intent.outcome,
                                       "reason": "no_fill_after_latency", "fair": intent.fair, "ask": intent.ask,
                                       "edge": intent.edge, "momentum_bp": intent.momentum_bp,
                                       "seconds_left": m.end - time.time()})
        finally:
            self.inflight.discard(key)
            self.risk.release(m.condition_id, reserve)

    # ---------- settlement & rebates ----------
    async def settle_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            now = time.time()
            for cid, info in list(self.pending.items()):
                if now < info["end"] + 45:   # on-chain resolution lands ~60-100 s after the end
                    continue
                self._model_winner(info)
                winner, source = await fetch_winner(cid)
                if winner is None and now > info["end"] + RESOLVE_GIVE_UP_S and info.get("model_winner"):
                    winner, source = info["model_winner"], "model_twap_fallback"
                if winner is None:
                    continue
                res = settle(self.pf, cid, winner)
                del self.pending[cid]
                if res:
                    self.stats["settled"] += 1
                    self.ledger.settlement({"ts": now, **res, "source": source, "model_winner": info.get("model_winner"),
                                            "equity_after": self.pf.equity})
                    log.info("SETTLED %s winner=%s (model %s) pnl %+.2f → equity %.2f", res["slug"], winner,
                             info.get("model_winner"), res["pnl"], self.pf.equity)
            reb = credit_rebates(self.pf, now, self.cfg.rebates.estimate_taker_rebates)
            if reb:
                self.ledger.rebate(reb)
                log.info("REBATE %s: %.2f (tier %.0f%% on fees %.2f)", reb["day"], reb["rebate"], reb["rate"] * 100, reb["fees"])
            if self.risk.roll_day(now):
                self.ledger.event(now, "new_day", f"equity {self.pf.equity:.2f}")
            self.pf.save(self.state_path)
            self.ledger.commit()

    # ---------- recording & status ----------
    async def snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.logging.snapshot_every_s)
            now = time.time()
            for m in list(self.markets.values()):
                if not (m.start <= now < m.end):
                    continue
                p = self.prices[m.asset]
                fv = self._fair(m, now)
                ub, db_ = self.books.book(m.up_token), self.books.book(m.down_token)
                spot = p.spot.last()
                cl_last = p.chainlink.last_sec
                self.ledger.snapshot({
                    "ts": now, "slug": m.slug, "seconds_left": m.end - now, "reference": self.refs.get(m.slug),
                    "x_now": p.estimate_now(now), "chainlink": p.chainlink.v.get(cl_last) if cl_last else None,
                    "spot": spot[1] if spot else None, "sigma": p.vol.sigma(spot[1]) if spot else None,
                    "p_up": fv.p_up if fv else None,
                    "up_bid": ub.best_bid() if ub else None, "up_ask": ub.best_ask() if ub else None,
                    "down_bid": db_.best_bid() if db_ else None, "down_ask": db_.best_ask() if db_ else None,
                    "momentum_bp": p.momentum_bp(self.cfg.strategy.momentum_window_s, now),
                    "chainlink_lag_s": now - cl_last if cl_last else None})
            self.ledger.commit()
            self.write_status(now)

    def write_status(self, now: float) -> None:
        """Live state for the dashboard (data/status.json, rewritten every second)."""
        pf = self.pf
        today = utc_day(now)
        mkts = []
        for m in sorted(self.markets.values(), key=lambda x: (x.asset, x.start)):
            if not (m.start <= now < m.end):
                continue
            p = self.prices[m.asset]
            fv = self._fair(m, now)
            ub, db_ = self.books.book(m.up_token), self.books.book(m.down_token)
            spot = p.spot.last()
            cl = p.chainlink.last_sec
            ua, da = (ub.best_ask() if ub else None), (db_.best_ask() if db_ else None)
            fee = lambda a: m.fee_rate * a * (1 - a)
            pos = pf.positions.get(m.condition_id)
            mkts.append({
                "slug": m.slug, "asset": m.asset, "timeframe": m.timeframe, "start": m.start, "end": m.end,
                "seconds_left": m.end - now, "reference": self.refs.get(m.slug), "x_now": p.estimate_now(now),
                "chainlink": p.chainlink.v.get(cl) if cl else None, "chainlink_age_s": now - cl if cl else None,
                "spot": spot[1] if spot else None, "sigma": p.vol.sigma(spot[1]) if spot else None,
                "p_up": fv.p_up if fv else None, "sd_final": fv.sd_final if fv else None,
                "up_bid": ub.best_bid() if ub else None, "up_ask": ua,
                "down_bid": db_.best_bid() if db_ else None, "down_ask": da,
                "edge_up": fv.p_up - ua - fee(ua) if fv and ua else None,
                "edge_down": (1 - fv.p_up) - da - fee(da) if fv and da else None,
                "momentum_bp": p.momentum_bp(self.cfg.strategy.momentum_window_s, now),
                "position": {"up_shares": pos.up_shares, "down_shares": pos.down_shares, "cost": pos.cost,
                             "orders": pos.orders} if pos else None})
        st = {"ts": now, "mode": self.cfg.mode, "started": self.started, "feeds": dict(self.status),
              "halted": pf.halted, "stats": dict(self.stats), "pending_settlement": len(self.pending),
              "portfolio": {"equity": pf.equity, "cash": pf.cash, "open_cost": pf.open_cost, "peak": pf.peak_equity,
                            "starting_equity": pf.starting_equity, "realized": pf.realized_pnl,
                            "rebates": pf.rebates_total, "fees_today": pf.fees_by_day.get(today, 0.0),
                            "wv_30d": pf.wv_30d(today), "tier": rebate_rate(pf.wv_30d(today)),
                            "day_start_equity": pf.day_start_equity},
              "markets": mkts,
              "config": {"assets": self.cfg.markets.assets, "timeframe": self.cfg.markets.timeframe,
                         "min_momentum_bp": self.cfg.strategy.min_momentum_bp, "min_edge": self.cfg.strategy.min_edge,
                         "latency_ms": self.cfg.execution.latency_ms, "haircut": self.cfg.execution.liquidity_haircut,
                         "clip_pct_equity": self.cfg.risk.clip_pct_equity,
                         "max_shares_per_order": self.cfg.risk.max_shares_per_order,
                         "max_market_usd": self.cfg.risk.max_market_usd,
                         "max_market_exposure_pct": self.cfg.risk.max_market_exposure_pct,
                         "daily_loss_stop_pct": self.cfg.risk.daily_loss_stop_pct,
                         "daily_loss_stop_basis": self.cfg.risk.daily_loss_stop_basis,
                         "daily_loss_limit_usd": self.risk.daily_loss_limit(),
                         "max_drawdown_kill_pct": self.cfg.risk.max_drawdown_kill_pct}}
        tmp = self.data / "status.tmp"
        tmp.write_text(json.dumps(st))
        tmp.replace(self.data / "status.json")

    async def status_loop(self) -> None:
        start = time.time()
        while True:
            await asyncio.sleep(self.cfg.logging.status_every_s)
            now = time.time()
            pf = self.pf
            tier = rebate_rate(pf.wv_30d(utc_day(now)))
            self.ledger.equity({"ts": now, "cash": pf.cash, "open_cost": pf.open_cost, "equity": pf.equity,
                                "peak": pf.peak_equity, "realized": pf.realized_pnl, "rebates": pf.rebates_total,
                                "halted": pf.halted})
            active = [m for m in self.markets.values() if m.start <= now < m.end]
            fvs = []
            for m in active:
                fv = self._fair(m, now)
                ub = self.books.book(m.up_token)
                fvs.append(f"{m.asset}:{int(m.end - now)}s p_up={fv.p_up:.2f}" if fv else f"{m.asset}:warming")
                if ub and ub.best_ask():
                    fvs[-1] += f" ask_up={ub.best_ask():.2f}"
            log.info("equity %.2f (cash %.2f, open %.2f) | realized %+.2f | rebates %.2f | tier %.0f%% | "
                     "fills %d, settled %d, pending %d | feeds cb=%s cl=%s clob=%s | %s%s",
                     pf.equity, pf.cash, pf.open_cost, pf.realized_pnl, pf.rebates_total, tier * 100,
                     self.stats["fills"], self.stats["settled"], len(self.pending), self.status["coinbase"],
                     self.status["chainlink"], self.status["clob"], " ".join(fvs) or "no active market",
                     f" | HALTED: {pf.halted}" if pf.halted else "")
            if self.run_seconds and now - start > self.run_seconds:
                raise SystemExit(0)

    async def run(self) -> None:
        self.risk.roll_day(time.time())
        tasks = [run_chainlink(self.prices, self.status), self.books.run(), self.market_loop(), self.signal_loop(),
                 self.settle_loop(), self.snapshot_loop(), self.status_loop()]
        if self.cfg.feeds.coinbase:
            tasks.append(run_coinbase(self.prices, self.status))
        try:
            await asyncio.gather(*tasks)
        finally:
            self.pf.save(self.state_path)
            self.ledger.commit()
