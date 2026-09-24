"""Pricing model for Polymarket crypto Up/Down markets.

Settlement rule (verified against 1,832 official BTC-5m resolutions, 97.8% match using Binance as a proxy):
    Up  if  TWAP_L(end)  >=  TWAP_L(start)        (Chainlink BTC/USD, L = 60 s)
where TWAP_L(t) is the mean of the 1-second Chainlink prints in (t − L, t].

So the "price to beat" R is known a moment after the window opens, and the final value A is an *average*
over the last L seconds. Inside that last minute the outcome locks in progressively, which a spot-vs-start
model misprices.

Model: over one-second steps the price is a driftless random walk with per-second std σ (USD).
If the current (estimated) price is x, the seconds of the final window that are already known sum to K,
n_f of them are still in the future, and the final window starts a ≥ 0 seconds from now:

    E[A]   = (K + (L − n_known) · x) / L          (unknown seconds are expected at x)
    Var[A] = σ² · (n_f² · a + n_f (n_f + 1)(2 n_f + 1) / 6) / L²  +  basis_sigma² · (share of window not yet known)²

    P(Up) = Φ((E[A] − R) / sqrt(Var[A]))
"""
from __future__ import annotations

import bisect
import math
from collections import deque
from dataclasses import dataclass


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def taker_fee_per_share(price: float, rate: float = 0.07) -> float:
    """Polymarket crypto taker fee: rate × shares × p × (1 − p)."""
    return rate * price * (1.0 - price)


class PriceSeries:
    """Time-ordered prices with O(log n) lookup of the value at-or-before a timestamp."""

    def __init__(self, max_age_s: float = 1800):
        self.ts: deque[float] = deque()
        self.px: deque[float] = deque()
        self.max_age_s = max_age_s

    def add(self, t: float, p: float) -> None:
        if self.ts and t < self.ts[-1]:
            return  # ignore out-of-order ticks
        self.ts.append(t)
        self.px.append(p)
        while self.ts and self.ts[0] < t - self.max_age_s:
            self.ts.popleft()
            self.px.popleft()

    def last(self) -> tuple[float, float] | None:
        return (self.ts[-1], self.px[-1]) if self.ts else None

    def at(self, t: float) -> float | None:
        if not self.ts:
            return None
        i = bisect.bisect_right(self.ts, t) - 1
        return self.px[i] if i >= 0 else None

    def move_bp(self, window_s: float, now: float) -> float | None:
        """Log move over the last `window_s` seconds, in basis points."""
        last = self.last()
        past = self.at(now - window_s)
        if last is None or past is None or past <= 0:
            return None
        return math.log(last[1] / past) * 1e4


class SecondBars:
    """Chainlink prints keyed by whole second (the settlement series)."""

    def __init__(self, max_age_s: int = 3600):
        self.v: dict[int, float] = {}
        self.max_age_s = max_age_s
        self.last_sec: int | None = None

    def add(self, sec: int, value: float) -> None:
        self.v[sec] = value
        if self.last_sec is None or sec > self.last_sec:
            self.last_sec = sec
            cutoff = sec - self.max_age_s
            if len(self.v) > self.max_age_s + 120:
                for k in [k for k in self.v if k < cutoff]:
                    del self.v[k]

    def get_ffill(self, sec: int, max_back: int = 5) -> float | None:
        for d in range(max_back + 1):
            if sec - d in self.v:
                return self.v[sec - d]
        return None

    def twap(self, end_sec: int, lookback: int) -> float | None:
        """Mean of prints in (end − lookback, end]; None if the window isn't covered."""
        if self.last_sec is None or self.last_sec < end_sec:
            return None
        vals = [self.get_ffill(s) for s in range(end_sec - lookback + 1, end_sec + 1)]
        if any(x is None for x in vals):
            return None
        return sum(vals) / len(vals)


class EwmaVol:
    """EWMA of squared one-second changes (USD) of the settlement series."""

    def __init__(self, halflife_s: float, floor_bp: float):
        self.alpha = 1 - 0.5 ** (1 / halflife_s)
        self.var: float | None = None
        self.floor_bp = floor_bp
        self._prev: tuple[int, float] | None = None

    def update(self, sec: int, value: float) -> None:
        if self._prev is not None and sec > self._prev[0]:
            dt = sec - self._prev[0]
            if dt <= 5:
                d2 = (value - self._prev[1]) ** 2 / dt
                self.var = d2 if self.var is None else (1 - self.alpha) * self.var + self.alpha * d2
        self._prev = (sec, value)

    def sigma(self, price: float) -> float:
        floor = self.floor_bp * 1e-4 * price
        return max(math.sqrt(self.var) if self.var is not None else 0.0, floor)


@dataclass
class FairValue:
    p_up: float
    mean_final: float
    sd_final: float
    reference: float
    known_seconds: int
    future_seconds: int


def fair_value(*, reference: float, x_now: float, now_sec: int, end_sec: int, lookback: int,
               realized: SecondBars, sigma: float, basis_sigma: float) -> FairValue:
    """P(Up) for a market settling on TWAP_lookback(end) >= reference. See module docstring."""
    win_start = end_sec - lookback + 1          # first second inside the final averaging window
    last_real = realized.last_sec if realized.last_sec is not None else -10**12
    known_sum = 0.0
    n_real = 0
    n_est = 0                                   # seconds that have passed but Chainlink hasn't printed yet
    for s in range(win_start, min(now_sec, end_sec) + 1):
        if s <= last_real:
            v = realized.get_ffill(s)
            if v is not None:
                known_sum += v
                n_real += 1
                continue
        n_est += 1
    n_future = end_sec - max(now_sec, win_start - 1)
    n_future = max(0, min(lookback, n_future))
    gap = max(0, win_start - 1 - now_sec)       # seconds until the final window begins
    unknown = lookback - n_real
    mean = (known_sum + unknown * x_now) / lookback
    rw_var = sigma ** 2 * (n_future ** 2 * gap + n_future * (n_future + 1) * (2 * n_future + 1) / 6) / lookback ** 2
    basis_var = (basis_sigma * unknown / lookback) ** 2
    sd = math.sqrt(rw_var + basis_var)
    if sd < 1e-9:
        p = 1.0 if mean >= reference else 0.0
    else:
        p = phi((mean - reference) / sd)
    return FairValue(p_up=min(max(p, 0.0), 1.0), mean_final=mean, sd_final=sd, reference=reference,
                     known_seconds=n_real, future_seconds=n_future)
