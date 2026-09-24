"""Market discovery (Gamma API) and official resolution (CLOB API)."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .net import get_json

TF_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


@dataclass
class Market:
    asset: str
    timeframe: str
    start: int
    end: int
    slug: str
    condition_id: str
    up_token: str
    down_token: str
    tick: float
    min_size: float
    fee_rate: float
    twap_lookback: int

    def token(self, outcome: str) -> str:
        return self.up_token if outcome == "Up" else self.down_token


def slug_for(asset: str, timeframe: str, start: int) -> str:
    return f"{asset}-updown-{timeframe}-{start}"


def window_start(now: float, timeframe: str) -> int:
    d = TF_SECONDS[timeframe]
    return int(now) // d * d


async def fetch_market(asset: str, timeframe: str, start: int) -> Market | None:
    slug = slug_for(asset, timeframe, start)
    data = await get_json(f"https://gamma-api.polymarket.com/markets?slug={slug}")
    if not data:
        return None
    m = data[0]
    outcomes = json.loads(m["outcomes"])
    tokens = json.loads(m["clobTokenIds"])
    tok = dict(zip(outcomes, tokens))
    fee = (m.get("feeSchedule") or {}).get("rate", 0.07)
    lookback = int((m.get("cryptoMarketConfig") or {}).get("twapLookbackSeconds") or 60)
    return Market(asset=asset, timeframe=timeframe, start=start, end=start + TF_SECONDS[timeframe], slug=slug,
                  condition_id=m["conditionId"], up_token=tok["Up"], down_token=tok["Down"],
                  tick=float(m.get("orderPriceMinTickSize") or 0.01), min_size=float(m.get("orderMinSize") or 5),
                  fee_rate=float(fee), twap_lookback=lookback)


async def fetch_winner(condition_id: str) -> str | None:
    """'Up' / 'Down' once Polymarket has resolved the market, else None."""
    d = await get_json(f"https://clob.polymarket.com/markets/{condition_id}")
    for t in d.get("tokens", []):
        if t.get("winner"):
            return t["outcome"]
    return None
