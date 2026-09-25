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
    vol_restore_max_age_s: float = 0       # 0 = always re-measure volatility on restart; >0 = reuse a saved estimate this fresh
    max_spot_adjust_sigma: float = 3.0     # cap Coinbase's pull on the Chainlink estimate at this many sigma·√lag
    basis_sigma_usd: float = 4.0


@dataclass
class Strategy:
    momentum_window_s: float = 3
    min_momentum_bp: float = 0.5
    min_edge: float = 0.02
    max_edge: float = 0.12                 # skip "edges" bigger than this (0 = off)
    max_momentum_bp: float = 5.0           # skip 3-second moves bigger than this: single-venue spikes (0 = off)
    trend_window_s: float = 60
    max_counter_trend_bp: float = 1.2      # skip buys against a 60 s move bigger than this (0 = off)
    min_price: float = 0.20
    max_price: float = 0.95
    min_seconds_left: float = 5
    min_seconds_elapsed: float = 3
    max_book_age_s: float = 2.0
    max_feed_lag_s: float = 0.5          # don't trade while the order-book feed runs this far behind Polymarket
    side_cooldown_s: float = 2.0
    max_orders_per_market: int = 30


@dataclass
class Execution:
    latency_ms: float = 700
    liquidity_haircut: float = 0.5
    max_slippage: float = 0.05
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
class Live:
    data_dir: str = "data_live"          # live/shadow keep their own ledger, separate from paper
    sync_every_s: float = 15             # read the portfolio balance (cash + positions) this often
    redeem_winnings: bool = True         # claim resolved winning positions (needs a Relayer API key)
    cancel_all_on_start: bool = True     # cancel any resting orders at start and on shutdown
    order_timeout_s: float = 10
    max_consecutive_errors: int = 5      # halt new orders after this many errors in a row
    max_orders_per_minute: int = 20
    min_cash_usd: float = 5              # don't open positions when pUSD cash is below this
    require_confirmation: bool = False   # true = also type LIVE at startup (skip with --yes)


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
    live: Live = field(default_factory=Live)
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
                "execution": Execution, "risk": Risk, "rebates": Rebates, "live": Live, "logging": Logging}
    for name, cls in sections.items():
        if name in raw:
            setattr(cfg, name, _build(cls, raw.pop(name)))
    if raw:
        raise ValueError(f"Unknown config sections: {sorted(raw)}")
    # relative paths are relative to the config file, so the bot works from any working directory on any OS
    base = path.parent
    if cfg.mode not in ("paper", "shadow", "live"):
        raise ValueError('mode must be "paper", "shadow" or "live"')
    if cfg.mode in ("shadow", "live"):   # real-account modes write to their own folder
        cfg.logging.data_dir = cfg.live.data_dir
    if not Path(cfg.logging.data_dir).is_absolute():
        cfg.logging.data_dir = str(base / cfg.logging.data_dir)
    if cfg.feeds.ca_bundle and not Path(cfg.feeds.ca_bundle).is_absolute():
        cfg.feeds.ca_bundle = str(base / cfg.feeds.ca_bundle)
    r = cfg.risk
    if r.max_shares_per_order and r.max_shares_per_order < r.min_shares:
        raise ValueError(f"risk.max_shares_per_order ({r.max_shares_per_order}) is below risk.min_shares ({r.min_shares}); "
                         "Polymarket's minimum order is 5 shares, so no order could ever be placed")
    if cfg.live.sync_every_s >= 30:
        raise ValueError("live.sync_every_s must be under 30: the balance sync keeps the order connection warm, "
                         "and the SDK closes connections idle for 30 s (a cold connection adds ~100 ms per order)")
    if r.daily_loss_stop_basis not in ("initial", "day_start"):
        raise ValueError('risk.daily_loss_stop_basis must be "initial" or "day_start"')
    if r.max_market_usd and r.max_market_usd < r.min_shares * 1.0:
        raise ValueError(f"risk.max_market_usd ({r.max_market_usd}) is too small for a {r.min_shares}-share order")
    return cfg
