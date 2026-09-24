"""Market discovery (Gamma API) and official resolution (on-chain Conditional Tokens, CLOB API fallback)."""
from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass

from .net import UA, get_ctx, get_json

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
    seconds_delay: float = 0.0   # >0 means the exchange delays matching; the momentum edge doesn't survive that

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
    delay = 0.0
    try:
        clob = await get_json(f"https://clob.polymarket.com/markets/{m['conditionId']}")
        delay = float(clob.get("seconds_delay") or 0)
    except Exception:
        delay = 0.0 if m.get("secondsDelay") in (None, 0) else float(m["secondsDelay"])
    return Market(asset=asset, timeframe=timeframe, start=start, end=start + TF_SECONDS[timeframe], slug=slug,
                  condition_id=m["conditionId"], up_token=tok["Up"], down_token=tok["Down"],
                  tick=float(m.get("orderPriceMinTickSize") or 0.01), min_size=float(m.get("orderMinSize") or 5),
                  fee_rate=float(fee), twap_lookback=lookback, seconds_delay=delay)


POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"   # Polymarket Conditional Tokens (Polygon)
SEL_DENOMINATOR = "0xdd34de67"                        # payoutDenominator(bytes32)
SEL_NUMERATORS = "0x0504c814"                         # payoutNumerators(bytes32,uint256)


def _eth_call_sync(data: str) -> int:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": CTF, "data": data}, "latest"]}).encode()
    req = urllib.request.Request(POLYGON_RPC, data=body, headers={"content-type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=10, context=get_ctx()) as r:
        return int(json.load(r)["result"], 16)


async def fetch_winner_onchain(condition_id: str) -> str | None:
    """Read the payout vector from the Conditional Tokens contract. Up/Down markets list outcomes as
    ["Up", "Down"], so index 0 = Up. Markets resolve on-chain ~60-100 s after they end — much sooner than the
    CLOB API's `winner` flag updates."""
    c = condition_id[2:].rjust(64, "0")
    den = await asyncio.to_thread(_eth_call_sync, SEL_DENOMINATOR + c)
    if den == 0:
        return None
    n_up = await asyncio.to_thread(_eth_call_sync, SEL_NUMERATORS + c + "0" * 64)
    n_down = await asyncio.to_thread(_eth_call_sync, SEL_NUMERATORS + c + "0" * 63 + "1")
    if n_up == n_down:
        return "Split"
    return "Up" if n_up > n_down else "Down"


async def fetch_winner_clob(condition_id: str) -> str | None:
    d = await get_json(f"https://clob.polymarket.com/markets/{condition_id}")
    for t in d.get("tokens", []):
        if t.get("winner"):
            return t["outcome"]
    return None


async def fetch_winner(condition_id: str) -> tuple[str | None, str]:
    """('Up' | 'Down' | 'Split' | None, source). On-chain first, CLOB API as a fallback."""
    try:
        w = await fetch_winner_onchain(condition_id)
        if w:
            return w, "onchain"
    except Exception:
        pass
    try:
        w = await fetch_winner_clob(condition_id)
        if w:
            return w, "clob"
    except Exception:
        pass
    return None, ""
