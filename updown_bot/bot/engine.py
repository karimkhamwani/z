"""Orchestrator: market rollover, signal loop, execution (paper / shadow / live), settlement, account sync,
snapshots, status."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path

from .book import BookFeed
from .config import Config
from .feeds import AssetPrices, run_chainlink, run_coinbase
from .ledger import Ledger
from .markets import Market, TF_SECONDS, fetch_market, fetch_winner, window_start
from .model import fair_value
from .net import atomic_write_text
from .paper import PaperExecutor, Portfolio, credit_rebates, rebate_rate, settle, utc_day
from .risk import RiskManager
from .secrets import mask
from .strategy import evaluate

log = logging.getLogger("engine")
RESOLVE_GIVE_UP_S = 20 * 60
MAX_CHAINLINK_AGE_S = 10


class Engine:
    def __init__(self, cfg: Config, fresh: bool = False, run_seconds: float | None = None, account=None):
        self.cfg = cfg
        self.mode = cfg.mode                          # paper | shadow | live
        self.account = account                        # LiveAccount for shadow/live, None for paper
        self.run_seconds = run_seconds
        self.data = Path(cfg.logging.data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data / "portfolio.json"
        if self.state_path.exists() and not fresh:
            self.pf = Portfolio.from_json(self.state_path.read_text(encoding="utf-8"))
            log.info("resumed portfolio: equity %.2f, %d open positions", self.pf.equity, len(self.pf.positions))
        else:
            e = cfg.account.starting_equity
            self.pf = Portfolio(cash=e, starting_equity=e, peak_equity=e)
        # live/shadow: starting capital = the real portfolio balance at the first sync (set in account_loop)
        self.needs_initial_equity = self.mode != "paper" and (fresh or not self.state_path.exists())
        self.ledger = Ledger(self.data / "paper.db")
        self.status: dict[str, str] = {"coinbase": "off", "chainlink": "connecting", "clob": "connecting"}
        self.prices = {a: AssetPrices(a, cfg.model.vol_halflife_s, cfg.model.vol_floor_bp, cfg.model.vol_change_s,
                                  cfg.model.vol_prior_bp) for a in cfg.markets.assets}
        self._restore_vol()
        self.books = BookFeed(self.status)
        self.risk = RiskManager(cfg.risk, self.pf)
        if self.mode == "paper":
            self.exec = PaperExecutor(self.pf, cfg.execution.latency_ms, cfg.execution.liquidity_haircut,
                                      cfg.execution.fee_rate, cfg.risk.min_shares)
        elif self.mode == "shadow":
            from .live import ShadowExecutor
            self.exec = ShadowExecutor(self.ledger)
        else:
            from .live import LiveExecutor
            self.exec = LiveExecutor(account, self.pf, self.ledger, order_timeout_s=cfg.live.order_timeout_s,
                                     max_consecutive_errors=cfg.live.max_consecutive_errors, kill_switch=self.kill_switch)
        self.order_times: deque[float] = deque()      # for the orders-per-minute limit
        self.account_info: dict = {}
        self.markets: dict[str, Market] = {}          # slug -> market (current + next per asset)
        self.refs: dict[str, float] = {}              # slug -> TWAP reference at start
        self.inflight: set[tuple[str, str]] = set()   # (slug, outcome) orders being executed
        self.last_order: dict[tuple[str, str], float] = {}
        self.pending: dict[str, dict] = {}            # condition_id -> {slug, end, first_try}
        self.stats = {"signals": 0, "fills": 0, "rejects": 0, "settled": 0}
        self.started = time.time()
        for cid, pos in self.pf.positions.items():   # positions restored from disk still need settlement
            self.pending[cid] = {"slug": pos.slug, "end": pos.end, "model_winner": None}

    # ---------- volatility state across restarts ----------
    def _restore_vol(self) -> None:
        if self.cfg.model.vol_restore_max_age_s <= 0:
            log.info("measuring volatility fresh from Chainlink (~5.5 min); no trading until it's ready")
            return
        path = self.data / "vol_state.json"
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        age = time.time() - saved.get("ts", 0)
        if age > self.cfg.model.vol_restore_max_age_s:
            log.info("saved volatility is %.0f min old: measuring afresh (~5.5 min before trading)", age / 60)
            return
        for a, st in saved.get("assets", {}).items():
            if a in self.prices:
                self.prices[a].vol.restore(st)
        log.info("restored volatility from %.0f s ago: %s", age,
                 {a: round(p.vol.sigma(1.0) if p.vol.var else 0, 2) for a, p in self.prices.items()})

    def _save_vol(self, now: float) -> None:
        state = {a: p.vol.state() for a, p in self.prices.items() if p.vol.ready}
        if state:
            atomic_write_text(self.data / "vol_state.json", json.dumps({"ts": now, "assets": state}))

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
                                if self.mode == "live" and self.account is not None:
                                    asyncio.create_task(self.account.prewarm([m.up_token, m.down_token]))
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
        cl = p.chainlink.v[p.chainlink.last_sec]
        sigma = p.vol.sigma(cl)
        lag = max(0.0, now - p.chainlink.last_sec)
        cap = self.cfg.model.max_spot_adjust_sigma * sigma * math.sqrt(lag + 1) if self.cfg.model.max_spot_adjust_sigma else None
        x = p.estimate_now(now, max_adjust=cap)
        if ref is None or x is None:
            return None
        return fair_value(reference=ref, x_now=x, now_sec=int(now), end_sec=m.end, lookback=m.twap_lookback,
                          realized=p.chainlink, sigma=sigma, basis_sigma=self.cfg.model.basis_sigma_usd)

    # ---------- trading ----------
    async def _next_tick(self) -> None:
        """Wake on the next Coinbase tick (the momentum source) or after 100 ms, whichever comes first.
        Polling every 100 ms added ~50 ms of average reaction delay before an order could be sent."""
        events = [p.tick for p in self.prices.values()]
        waiters = [asyncio.create_task(e.wait()) for e in events]
        try:
            await asyncio.wait(waiters, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            for e in events:
                e.clear()

    async def signal_loop(self) -> None:
        sc = self.cfg.strategy
        while True:
            await self._next_tick()
            now = time.time()
            if self.risk.check_breakers() or self._live_blocked(now):
                continue
            for m in list(self.markets.values()):
                if not (m.start <= now < m.end):
                    continue
                if m.seconds_delay > 0:   # delayed matching: a 3-second momentum edge can't survive it
                    continue
                if not self.prices[m.asset].vol.ready:   # only trade on volatility measured from Chainlink itself
                    continue
                fv = self._fair(m, now)
                if fv is None:
                    continue
                p = self.prices[m.asset]
                mom = p.momentum_bp(sc.momentum_window_s, now)
                books = {"Up": self.books.book(m.up_token), "Down": self.books.book(m.down_token)}
                trend = p.spot.move_bp(sc.trend_window_s, now) if sc.max_counter_trend_bp else None
                intent, rejs = evaluate(sc, p_up=fv.p_up, momentum_bp=mom, books=books, now=now,
                                        seconds_left=m.end - now, seconds_elapsed=now - m.start,
                                        fee_rate=m.fee_rate, tick=m.tick, max_slippage=self.cfg.execution.max_slippage,
                                        trend_bp=trend)
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
                if self.mode != "paper":
                    while self.order_times and now - self.order_times[0] > 60:
                        self.order_times.popleft()
                    if len(self.order_times) >= self.cfg.live.max_orders_per_minute:
                        continue
                    self.order_times.append(now)
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
                self.ledger.fill({**asdict(fill), "mode": self.mode})
                log.info("FILL %s %s %.2f sh @ %.3f (fair %.3f, edge %+.3f, mom %+.2fbp, %ds left) cash %.2f",
                         m.slug, fill.outcome, fill.shares, fill.avg_price, fill.fair, fill.edge, fill.momentum_bp,
                         fill.seconds_left, fill.cash)
            else:
                self.ledger.rejection({"ts": time.time(), "slug": m.slug, "outcome": intent.outcome,
                                       "reason": "no_fill_after_latency", "fair": intent.fair, "ask": intent.ask,
                                       "edge": intent.edge, "momentum_bp": intent.momentum_bp,
                                       "seconds_left": m.end - time.time()})
        except Exception as e:
            from .live import HaltTrading
            if isinstance(e, HaltTrading):
                self.pf.halted = f"live halt: {e}"
                self.ledger.event(time.time(), "halt", str(e))
                log.error("HALTED — no new orders until restart: %s", e)
            else:
                log.exception("unexpected execution error on %s", m.slug)
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
                res = settle(self.pf, cid, winner, credit_cash=self.mode == "paper")
                del self.pending[cid]
                if res:
                    self.stats["settled"] += 1
                    self.ledger.settlement({"ts": now, **res, "source": source, "model_winner": info.get("model_winner"),
                                            "equity_after": self.pf.equity})
                    log.info("SETTLED %s winner=%s (model %s) pnl %+.2f → equity %.2f", res["slug"], winner,
                             info.get("model_winner"), res["pnl"], self.pf.equity)
            reb = credit_rebates(self.pf, now, self.cfg.rebates.estimate_taker_rebates and self.mode == "paper")
            if reb:
                self.ledger.rebate(reb)
                log.info("REBATE %s: %.2f (tier %.0f%% on fees %.2f)", reb["day"], reb["rebate"], reb["rate"] * 100, reb["fees"])
            if self.risk.roll_day(now):
                self.ledger.event(now, "new_day", f"equity {self.pf.equity:.2f}")
            self.pf.save(self.state_path)
            self._save_vol(now)
            self.ledger.commit()

    # ---------- live account ----------
    def kill_switch(self) -> bool:
        """Create a file named STOP in the data folder to stop opening positions immediately."""
        return (self.data / "STOP").exists()

    def _live_blocked(self, now: float) -> bool:
        if self.mode == "paper":
            return False
        if self.kill_switch():
            return True
        if now - self.pf.last_sync > 3 * self.cfg.live.sync_every_s:   # never trade on a stale balance
            return True
        return self.pf.cash < self.cfg.live.min_cash_usd

    async def account_loop(self) -> None:
        failures = 0
        while True:
            try:
                snap = await self.account.snapshot()
                failures = 0
                self.pf.cash = snap.cash
                self.pf.positions_value = snap.positions_value
                self.pf.claimable_value = snap.claimable_value
                self.pf.last_sync = snap.ts
                if self.needs_initial_equity:
                    eq = self.pf.equity
                    self.pf.starting_equity = self.pf.day_start_equity = eq
                    self.pf.peak_equity = self.pf.equity_low
                    self.needs_initial_equity = False
                    log.info("starting capital set from portfolio balance: %.2f (cash %.2f + positions %.2f)",
                             eq, snap.cash, snap.positions_value)
                self.pf.peak_equity = max(self.pf.peak_equity, self.pf.equity_low)
                self.account_info = {"cash": snap.cash, "positions_value": snap.positions_value + snap.claimable_value,
                                     "claimable_value": snap.claimable_value,
                                     "open_positions": snap.open_positions, "redeemable": len(snap.redeemable),
                                     "last_sync": snap.ts, "error": ""}
                self.ledger.account({"ts": snap.ts, "cash": snap.cash, "positions_value": snap.positions_value,
                                     "equity": self.pf.equity, "open_positions": snap.open_positions,
                                     "redeemable": len(snap.redeemable), "raw_balance": snap.raw_balance})
                if self.mode == "live" and self.cfg.live.redeem_winnings and self.account.creds.can_redeem:
                    for cid in snap.redeemable:
                        try:
                            tx = await self.account.redeem(cid)
                            if tx:
                                log.info("CLAIMED winnings for %s… (tx %s)", cid[:10], tx)
                                self.ledger.event(time.time(), "redeem", f"{cid} {tx}")
                        except Exception as e:
                            log.warning("claiming %s… failed (will retry): %s", cid[:10], e)
            except Exception as e:
                failures += 1
                self.account_info["error"] = f"{type(e).__name__}: {e}"[:200]
                log.warning("account sync failed (%d in a row): %s", failures, e)
            await asyncio.sleep(self.cfg.live.sync_every_s)

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
                "vol_ready": p.vol.ready, "vol_obs": p.vol.n, "vol_warmup": p.vol.warmup,
                "position": {"up_shares": pos.up_shares, "down_shares": pos.down_shares, "cost": pos.cost,
                             "orders": pos.orders} if pos else None})
        live = None
        if self.mode != "paper" and self.account is not None:
            live = {"wallet": mask(self.account.wallet), "wallet_type": self.account.wallet_type,
                    "kill_switch": self.kill_switch(), "sync_age_s": now - self.pf.last_sync if self.pf.last_sync else None,
                    "can_redeem": bool(self.account.creds.can_redeem and self.cfg.live.redeem_winnings),
                    "min_cash_usd": self.cfg.live.min_cash_usd, **self.account_info}
        st = {"ts": now, "mode": self.cfg.mode, "live": live, "started": self.started, "feeds": dict(self.status),
              "halted": pf.halted, "stats": dict(self.stats), "pending_settlement": len(self.pending),
              "portfolio": {"equity": pf.equity, "cash": pf.cash, "open_cost": pf.open_cost, "peak": pf.peak_equity,
                            "positions_value": pf.positions_value,
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
        atomic_write_text(self.data / "status.json", json.dumps(st))

    async def status_loop(self) -> None:
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
                vol = self.prices[m.asset].vol
                tag = "" if vol.ready else f" (measuring vol {min(vol.n, vol.warmup)}/{vol.warmup}, not trading)"
                fvs.append((f"{m.asset}:{int(m.end - now)}s p_up={fv.p_up:.2f}" if fv else f"{m.asset}:warming") + tag)
                if ub and ub.best_ask():
                    fvs[-1] += f" ask_up={ub.best_ask():.2f}"
            log.info("equity %.2f (cash %.2f, open %.2f) | realized %+.2f | rebates %.2f | tier %.0f%% | "
                     "fills %d, settled %d, pending %d | feeds cb=%s cl=%s clob=%s | %s%s",
                     pf.equity, pf.cash, pf.open_cost, pf.realized_pnl, pf.rebates_total, tier * 100,
                     self.stats["fills"], self.stats["settled"], len(self.pending), self.status["coinbase"],
                     self.status["chainlink"], self.status["clob"], " ".join(fvs) or "no active market",
                     f" | HALTED: {pf.halted}" if pf.halted else "")

    async def run(self) -> None:
        if self.account is not None:
            if self.mode == "live" and self.cfg.live.cancel_all_on_start:
                await self.account.cancel_all()
                log.info("cancelled any resting orders")
        self.risk.roll_day(time.time())
        coros = [run_chainlink(self.prices, self.status), self.books.run(), self.market_loop(), self.signal_loop(),
                 self.settle_loop(), self.snapshot_loop(), self.status_loop()]
        if self.cfg.feeds.coinbase:
            coros.append(run_coinbase(self.prices, self.status))
        if self.account is not None:
            coros.append(self.account_loop())
        tasks = [asyncio.create_task(c) for c in coros]
        try:
            # run until a task crashes (re-raised below) or, with --minutes, until the time is up
            done, _ = await asyncio.wait(tasks, timeout=self.run_seconds, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                t.result()
            if self.run_seconds:
                log.info("--minutes reached; stopping")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.account is not None:
                try:
                    if self.mode == "live" and self.cfg.live.cancel_all_on_start:
                        await self.account.cancel_all()
                    await self.account.close()
                except Exception as e:
                    log.warning("shutdown: %s", e)
            self.pf.save(self.state_path)
            self.ledger.commit()
