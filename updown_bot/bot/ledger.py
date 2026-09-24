"""SQLite ledger: every fill, settlement, rejected signal, 1 Hz market snapshot, equity point and rebate."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
  ts REAL, slug TEXT, condition_id TEXT, outcome TEXT, shares REAL, avg_price REAL, notional REAL, fee REAL,
  cash REAL, fair REAL, edge REAL, momentum_bp REAL, seconds_left REAL, signal_ask REAL, levels TEXT, mode TEXT);
CREATE TABLE IF NOT EXISTS settlements (
  ts REAL, slug TEXT, condition_id TEXT, winner TEXT, source TEXT, model_winner TEXT, up_shares REAL,
  down_shares REAL, cost REAL, fees REAL, payout REAL, pnl REAL, orders INTEGER, equity_after REAL);
CREATE TABLE IF NOT EXISTS rejections (
  ts REAL, slug TEXT, outcome TEXT, reason TEXT, fair REAL, ask REAL, edge REAL, momentum_bp REAL, seconds_left REAL);
CREATE TABLE IF NOT EXISTS snapshots (
  ts REAL, slug TEXT, seconds_left REAL, reference REAL, x_now REAL, chainlink REAL, spot REAL, sigma REAL,
  p_up REAL, up_bid REAL, up_ask REAL, down_bid REAL, down_ask REAL, momentum_bp REAL, chainlink_lag_s REAL);
CREATE TABLE IF NOT EXISTS equity (
  ts REAL, cash REAL, open_cost REAL, equity REAL, peak REAL, realized REAL, rebates REAL, halted TEXT);
CREATE TABLE IF NOT EXISTS rebates (day TEXT, fees REAL, wv_30d REAL, rate REAL, rebate REAL);
CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS orders (
  ts REAL, slug TEXT, condition_id TEXT, outcome TEXT, amount REAL, max_spend REAL, max_price REAL, ok INTEGER,
  status TEXT, code TEXT, message TEXT, latency_s REAL, order_id TEXT, filled_usd REAL, filled_shares REAL);
CREATE TABLE IF NOT EXISTS account (
  ts REAL, cash REAL, positions_value REAL, equity REAL, open_positions INTEGER, redeemable INTEGER, raw_balance INTEGER);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(slug, ts);
"""


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._dirty = False

    def _ins(self, table: str, row: dict) -> None:
        cols = ",".join(row)
        self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})",
                        [json.dumps(v) if isinstance(v, (list, dict)) else v for v in row.values()])
        self._dirty = True

    def fill(self, row: dict) -> None: self._ins("fills", row)
    def settlement(self, row: dict) -> None: self._ins("settlements", row)
    def rejection(self, row: dict) -> None: self._ins("rejections", row)
    def snapshot(self, row: dict) -> None: self._ins("snapshots", row)
    def equity(self, row: dict) -> None: self._ins("equity", row)
    def rebate(self, row: dict) -> None: self._ins("rebates", row)
    def order(self, row: dict) -> None: self._ins("orders", row)
    def account(self, row: dict) -> None: self._ins("account", row)
    def event(self, ts: float, kind: str, detail: str) -> None: self._ins("events", {"ts": ts, "kind": kind, "detail": detail})

    def commit(self) -> None:
        if self._dirty:
            self.db.commit()
            self._dirty = False
