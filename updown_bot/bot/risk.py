"""Position sizing and circuit breakers. Sizing is a share of equity, so the bot scales as equity grows."""
from __future__ import annotations

import math

from .config import Risk
from .paper import Portfolio, utc_day


class RiskManager:
    def __init__(self, cfg: Risk, pf: Portfolio):
        self.cfg = cfg
        self.pf = pf

    def roll_day(self, now: float) -> bool:
        """Reset the daily loss stop at UTC midnight. Returns True on a new day."""
        day = utc_day(now)
        if day != self.pf.day:
            self.pf.day = day
            self.pf.day_start_equity = self.pf.equity
            if self.pf.halted.startswith("daily"):
                self.pf.halted = ""
            return True
        return False

    def check_breakers(self) -> str:
        pf = self.pf
        eq = pf.equity
        pf.peak_equity = max(pf.peak_equity, eq)
        if pf.halted.startswith("drawdown"):
            return pf.halted
        if eq <= pf.peak_equity * (1 - self.cfg.max_drawdown_kill_pct):
            pf.halted = f"drawdown kill: equity {eq:.2f} is {self.cfg.max_drawdown_kill_pct:.0%} below peak {pf.peak_equity:.2f}"
        elif pf.day_start_equity and eq <= pf.day_start_equity * (1 - self.cfg.daily_loss_stop_pct):
            pf.halted = f"daily stop: equity {eq:.2f} is {self.cfg.daily_loss_stop_pct:.0%} below today's start {pf.day_start_equity:.2f}"
        return pf.halted

    def market_budget(self, condition_id: str) -> float:
        pos = self.pf.positions.get(condition_id)
        used = pos.cost if pos else 0.0
        return max(0.0, self.pf.equity * self.cfg.max_market_exposure_pct - used)

    def clip_shares(self, condition_id: str, price: float, min_size: float) -> float:
        """Shares for one order at `price` (0 if below the exchange minimum or out of budget)."""
        usd = min(self.pf.equity * self.cfg.clip_pct_equity, self.cfg.max_clip_usd,
                  self.market_budget(condition_id), self.pf.cash)
        if price <= 0 or usd <= 0:
            return 0.0
        shares = math.floor(usd / price * 100) / 100
        if shares < max(self.cfg.min_shares, min_size):
            # at small equity the 5-share minimum can exceed the % clip; allow it only if the market budget covers it
            need = max(self.cfg.min_shares, min_size)
            if need * price <= min(self.market_budget(condition_id), self.pf.cash):
                return need
            return 0.0
        return shares
