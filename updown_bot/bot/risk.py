"""Position sizing and circuit breakers.

Orders are a share of equity (so size grows with equity), capped by `max_shares_per_order` and by a per-market
budget = min(max_market_exposure_pct × equity, max_market_usd) that counts filled cost, fees and in-flight orders."""
from __future__ import annotations

import math

from .config import Risk
from .paper import Portfolio, utc_day


class RiskManager:
    def __init__(self, cfg: Risk, pf: Portfolio):
        self.cfg = cfg
        self.pf = pf
        self.reserved: dict[str, float] = {}   # condition_id -> worst-case cost of orders in flight

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
        if pf.halted.startswith(("drawdown", "live")):
            return pf.halted
        if eq <= pf.peak_equity * (1 - self.cfg.max_drawdown_kill_pct):
            pf.halted = f"drawdown kill: equity {eq:.2f} is {self.cfg.max_drawdown_kill_pct:.0%} below peak {pf.peak_equity:.2f}"
        elif pf.day_start_equity and pf.day_start_equity - eq >= self.daily_loss_limit():
            pf.halted = (f"daily stop: down {pf.day_start_equity - eq:.2f} today (limit {self.daily_loss_limit():.2f} = "
                         f"{self.cfg.daily_loss_stop_pct:.0%} of {self._daily_basis_label()}); resumes at 00:00 UTC")
        return pf.halted

    def daily_loss_limit(self) -> float:
        """Max USD loss allowed in one UTC day before new positions stop."""
        base = self.pf.starting_equity if self.cfg.daily_loss_stop_basis == "initial" else self.pf.day_start_equity
        return self.cfg.daily_loss_stop_pct * base

    def _daily_basis_label(self) -> str:
        return "starting capital" if self.cfg.daily_loss_stop_basis == "initial" else "today's starting equity"

    def market_cap(self) -> float:
        """Max total cost (incl. fees) in one market: the lower of the % of equity and the fixed USD cap."""
        cap = self.pf.equity * self.cfg.max_market_exposure_pct
        if self.cfg.max_market_usd:
            cap = min(cap, self.cfg.max_market_usd)
        return cap

    def market_budget(self, condition_id: str) -> float:
        """What's left under the market cap after filled cost and orders still in flight."""
        pos = self.pf.positions.get(condition_id)
        used = (pos.cost if pos else 0.0) + self.reserved.get(condition_id, 0.0)
        return max(0.0, self.market_cap() - used)

    def reserve(self, condition_id: str, amount: float) -> None:
        self.reserved[condition_id] = self.reserved.get(condition_id, 0.0) + amount

    def release(self, condition_id: str, amount: float) -> None:
        left = self.reserved.get(condition_id, 0.0) - amount
        if left <= 1e-9:
            self.reserved.pop(condition_id, None)
        else:
            self.reserved[condition_id] = left

    def clip_shares(self, condition_id: str, price: float, min_size: float, fee_rate: float = 0.07) -> float:
        """Shares for one order with limit `price` (0 if it can't fit). Worst-case cost per share is the limit
        plus the taker fee at the limit (cost rises with price, so a better fill only costs less)."""
        if price <= 0:
            return 0.0
        unit = price + fee_rate * price * (1 - price)
        budget = min(self.market_budget(condition_id), self.pf.cash)
        usd = min(self.pf.equity * self.cfg.clip_pct_equity, self.cfg.max_clip_usd, budget)
        shares = math.floor(usd / unit * 100) / 100
        if self.cfg.max_shares_per_order:
            shares = min(shares, self.cfg.max_shares_per_order)
        need = max(self.cfg.min_shares, min_size)
        if shares >= need:
            return shares
        # at small equity the 5-share exchange minimum can exceed the % clip: allow exactly the minimum
        # only if it still fits under the per-order share cap, the market cap and cash
        if (not self.cfg.max_shares_per_order or need <= self.cfg.max_shares_per_order) and need * unit <= budget:
            return need
        return 0.0
