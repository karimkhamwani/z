"""Typed configuration loaded from config.toml."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class Account:
    starting_equity: float = 200.0


@dataclass
class Markets:
    assets: list[str] = field(default_factory=lambda: ["btc"])
    timeframe: str = "5m"


@dataclass
class Feeds:
    coinbase: bool = True
    ca_bundle: str = ""
    relax_x509_strict: bool = True


@dataclass
class Model:
    twap_lookback_s: int = 60
    vol_halflife_s: float = 600
    vol_floor_bp: float = 0.2
    vol_change_s: int = 30
    vol_prior_bp: float = 0.5
    basis_sigma_usd: float = 4.0


@dataclass
class Strategy:
    momentum_window_s: float = 3
    min_momentum_bp: float = 0.5
    min_edge: float = 0.02
    min_price: float = 0.05
    max_price: float = 0.95
    min_seconds_left: float = 5
    min_seconds_elapsed: float = 3
    max_book_age_s: float = 2.0
    side_cooldown_s: float = 2.0
    max_orders_per_market: int = 30


@dataclass
class Execution:
    latency_ms: float = 300
    liquidity_haircut: float = 0.5
    max_slippage: float = 0.02
    fee_rate: float = 0.07


@dataclass
class Risk:
    clip_pct_equity: float = 0.05
    min_shares: float = 5
    max_clip_usd: float = 250
    max_shares_per_order: float = 5      # 0 = no cap
    max_market_usd: float = 30           # 0 = no cap; includes fees and in-flight orders
    max_market_exposure_pct: float = 0.25
    daily_loss_stop_pct: float = 0.50
    daily_loss_stop_basis: str = "initial"   # "initial" = % of starting capital, "day_start" = % of equity at 00:00 UTC
    max_drawdown_kill_pct: float = 0.35


@dataclass
class Rebates:
    estimate_taker_rebates: bool = True


@dataclass
class Logging:
    data_dir: str = "data"
    snapshot_every_s: float = 1.0
    status_every_s: float = 15


@dataclass
class Config:
    mode: str = "paper"
    account: Account = field(default_factory=Account)
    markets: Markets = field(default_factory=Markets)
    feeds: Feeds = field(default_factory=Feeds)
    model: Model = field(default_factory=Model)
    strategy: Strategy = field(default_factory=Strategy)
    execution: Execution = field(default_factory=Execution)
    risk: Risk = field(default_factory=Risk)
    rebates: Rebates = field(default_factory=Rebates)
    logging: Logging = field(default_factory=Logging)


def _build(cls, data: dict):
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"Unknown config keys in [{cls.__name__.lower()}]: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = Config(mode=raw.pop("mode", "paper"))
    sections = {"account": Account, "markets": Markets, "feeds": Feeds, "model": Model, "strategy": Strategy,
                "execution": Execution, "risk": Risk, "rebates": Rebates, "logging": Logging}
    for name, cls in sections.items():
        if name in raw:
            setattr(cfg, name, _build(cls, raw.pop(name)))
    if raw:
        raise ValueError(f"Unknown config sections: {sorted(raw)}")
    # relative paths are relative to the config file, so the bot works from any working directory on any OS
    base = path.parent
    if not Path(cfg.logging.data_dir).is_absolute():
        cfg.logging.data_dir = str(base / cfg.logging.data_dir)
    if cfg.feeds.ca_bundle and not Path(cfg.feeds.ca_bundle).is_absolute():
        cfg.feeds.ca_bundle = str(base / cfg.feeds.ca_bundle)
    r = cfg.risk
    if r.max_shares_per_order and r.max_shares_per_order < r.min_shares:
        raise ValueError(f"risk.max_shares_per_order ({r.max_shares_per_order}) is below risk.min_shares ({r.min_shares}); "
                         "Polymarket's minimum order is 5 shares, so no order could ever be placed")
    if r.daily_loss_stop_basis not in ("initial", "day_start"):
        raise ValueError('risk.daily_loss_stop_basis must be "initial" or "day_start"')
    if r.max_market_usd and r.max_market_usd < r.min_shares * 1.0:
        raise ValueError(f"risk.max_market_usd ({r.max_market_usd}) is too small for a {r.min_shares}-share order")
    return cfg
