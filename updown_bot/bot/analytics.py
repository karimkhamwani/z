"""Performance analytics over the paper ledger (shared by report.py and the dashboard)."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

BUCKETS = {
    "dir_momentum_bp": ([(-1e9, 0.5), (0.5, 1), (1, 2), (2, 4), (4, 1e9)], ["<0.5bp", "0.5–1", "1–2", "2–4", "≥4bp"]),
    "edge": ([(-1e9, 0), (0, 0.02), (0.02, 0.05), (0.05, 0.1), (0.1, 1e9)], ["<0", "0–2¢", "2–5¢", "5–10¢", "≥10¢"]),
    "seconds_left": ([(0, 30), (30, 60), (60, 120), (120, 200), (200, 1e9)], ["<30s", "30–60s", "60–120s", "120–200s", "≥200s"]),
    "avg_price": ([(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)], ["<0.2", "0.2–0.4", "0.4–0.6", "0.6–0.8", "≥0.8"]),
    "fair": ([(0, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)], ["<0.3", "0.3–0.5", "0.5–0.7", "0.7–0.9", "≥0.9"]),
}
TITLES = {"dir_momentum_bp": "By momentum toward the side bought", "edge": "By edge at fill (after fee)",
          "seconds_left": "By time left in window", "avg_price": "By fill price",
          "fair": "Calibration: model fair value vs actual win rate"}


def connect(data: Path) -> sqlite3.Connection | None:
    p = data / "paper.db"
    if not p.exists():
        return None
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def bucket_table(rows: list[dict], key: str) -> list[dict]:
    edges, labels = BUCKETS[key]
    out = []
    for (lo, hi), lab in zip(edges, labels):
        sel = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
        n = sum(r["notional"] for r in sel)
        pnl = sum(r["pnl"] for r in sel)
        wins = sum(1 for r in sel if r["won"])
        avg_fair = sum(r["fair"] for r in sel) / len(sel) if sel else None
        out.append({"bucket": lab, "fills": len(sel), "notional": round(n, 2), "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / n * 100, 2) if n else None,
                    "win_rate": round(wins / len(sel), 3) if sel else None,
                    "avg_fair": round(avg_fair, 3) if avg_fair is not None else None})
    return out


def load(data: Path, recent: int = 30) -> dict:
    data = Path(data)
    try:
        pf = json.loads((data / "portfolio.json").read_text(encoding="utf-8")) if (data / "portfolio.json").exists() else {}
    except (OSError, ValueError):   # file being replaced right now — try again on the next poll
        pf = {}
    db = connect(data)
    if db is None:
        return {"portfolio": pf, "empty": True}
    settled = {r["condition_id"]: dict(r) for r in db.execute("SELECT * FROM settlements ORDER BY ts")}
    fills = [dict(r) for r in db.execute("SELECT * FROM fills ORDER BY ts")]
    for f in fills:
        s = settled.get(f["condition_id"])
        f["won"] = None if s is None else (s["winner"] == f["outcome"])
        f["pnl"] = 0.0 if s is None else (f["shares"] if f["won"] else 0.0) - f["cash"]
        f.pop("levels", None)
        m = f.get("momentum_bp")
        f["dir_momentum_bp"] = None if m is None else (m if f["outcome"] == "Up" else -m)
    done = [f for f in fills if f["won"] is not None]
    notional = sum(f["notional"] for f in done)
    fees = sum(f["fee"] for f in done)
    gross = sum((f["shares"] if f["won"] else 0) - f["notional"] for f in done)
    net = sum(f["pnl"] for f in done)
    curve = [dict(r) for r in db.execute("SELECT ts, equity, cash, open_cost FROM equity ORDER BY ts")]
    peak, mdd = 0.0, 0.0
    for r in curve:
        peak = max(peak, r["equity"])
        mdd = max(mdd, (peak - r["equity"]) / peak if peak else 0)
    sl = list(settled.values())
    agree = [s for s in sl if s["model_winner"]]
    start_eq = pf.get("starting_equity", 0) or 0
    equity = pf.get("cash", 0) + sum(p["cost"] for p in pf.get("positions", {}).values())
    daily: dict[str, dict] = {}
    for f in done:
        d = dt.datetime.fromtimestamp(f["ts"], dt.UTC).strftime("%Y-%m-%d")
        x = daily.setdefault(d, {"day": d, "fills": 0, "notional": 0.0, "fees": 0.0, "pnl": 0.0})
        x["fills"] += 1; x["notional"] += f["notional"]; x["fees"] += f["fee"]; x["pnl"] += f["pnl"]
    rejections = [dict(r) for r in db.execute(
        "SELECT reason, COUNT(*) n, AVG(edge) avg_edge FROM rejections GROUP BY reason ORDER BY n DESC")]
    rebates = [dict(r) for r in db.execute("SELECT * FROM rebates ORDER BY day")]
    return {
        "empty": False, "portfolio": pf,
        "summary": {"equity": equity, "starting_equity": start_eq,
                    "return_pct": (equity / start_eq - 1) * 100 if start_eq else 0.0,
                    "peak": pf.get("peak_equity", 0), "halted": pf.get("halted", ""), "max_drawdown": mdd,
                    "markets_settled": len(sl), "fills": len(fills), "fills_settled": len(done),
                    "open_positions": len(pf.get("positions", {})), "notional": notional, "gross_pnl": gross,
                    "fees": fees, "net_trading_pnl": net, "net_pct": net / notional * 100 if notional else 0.0,
                    "rebates": pf.get("rebates_total", 0.0),
                    "market_win_rate": sum(s["pnl"] > 0 for s in sl) / len(sl) if sl else None,
                    "avg_pnl_per_market": sum(s["pnl"] for s in sl) / len(sl) if sl else None,
                    "fill_win_rate": sum(1 for f in done if f["won"]) / len(done) if done else None,
                    "model_settlement_agreement": [sum(1 for s in agree if s["model_winner"] == s["winner"]), len(agree)],
                    "first_fill": fills[0]["ts"] if fills else None},
        "tables": {k: {"title": TITLES[k], "rows": bucket_table(done, k)} for k in BUCKETS},
        "equity_curve": curve,
        "recent_fills": fills[-recent:][::-1],
        "recent_settlements": sl[-recent:][::-1],
        "daily": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in daily.values()],
        "rejections": rejections,
        "rebates": rebates,
    }


def market_snapshots(data: Path, slug: str, limit: int = 400) -> list[dict]:
    db = connect(Path(data))
    if db is None:
        return []
    rows = db.execute("SELECT ts, seconds_left, reference, x_now, chainlink, spot, p_up, up_bid, up_ask, down_bid, "
                      "down_ask, momentum_bp FROM snapshots WHERE slug=? ORDER BY ts DESC LIMIT ?", (slug, limit))
    return [dict(r) for r in rows][::-1]


# ---------- paginated tables (all rows, filtered server-side) ----------
FILL_SQL = """
SELECT f.ts, f.slug, f.condition_id, f.outcome, f.shares, f.avg_price, f.notional, f.fee, f.cash, f.fair, f.edge,
       f.momentum_bp, f.seconds_left, f.signal_ask, s.winner,
       CASE WHEN s.winner IS NULL THEN NULL
            WHEN s.winner = 'Split' THEN 0.5 * f.shares - f.cash
            WHEN s.winner = f.outcome THEN f.shares - f.cash ELSE -f.cash END AS pnl
FROM fills f LEFT JOIN settlements s ON s.condition_id = f.condition_id
"""


def _fill_where(result: str, side: str) -> tuple[str, list]:
    conds, args = [], []
    if result == "open":
        conds.append("s.winner IS NULL")
    elif result == "won":
        conds.append("s.winner IS NOT NULL AND (s.winner = f.outcome OR s.winner = 'Split')")
    elif result == "lost":
        conds.append("s.winner IS NOT NULL AND s.winner <> f.outcome AND s.winner <> 'Split'")
    if side in ("Up", "Down"):
        conds.append("f.outcome = ?")
        args.append(side)
    return (" WHERE " + " AND ".join(conds)) if conds else "", args


def fills_page(data: Path, offset: int = 0, limit: int = 50, result: str = "all", side: str = "all") -> dict:
    """Newest-first page of fills with each fill's settlement result, plus totals for the filtered set."""
    db = connect(Path(data))
    if db is None:
        return {"total": 0, "rows": [], "totals": {}}
    where, args = _fill_where(result, side)
    tot = db.execute(f"SELECT COUNT(*) n, COALESCE(SUM(notional),0) notional, COALESCE(SUM(fee),0) fees, "
                     f"COALESCE(SUM(pnl),0) pnl, SUM(pnl IS NOT NULL) settled, SUM(pnl > 0) won "
                     f"FROM ({FILL_SQL}{where})", args).fetchone()
    rows = db.execute(f"{FILL_SQL}{where} ORDER BY f.ts DESC LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return {"total": tot["n"], "offset": offset, "limit": limit, "rows": [dict(r) for r in rows],
            "totals": {"notional": tot["notional"], "fees": tot["fees"], "pnl": tot["pnl"],
                       "settled": tot["settled"] or 0, "won": tot["won"] or 0}}


def settlements_page(data: Path, offset: int = 0, limit: int = 50, result: str = "all") -> dict:
    db = connect(Path(data))
    if db is None:
        return {"total": 0, "rows": []}
    where = {"won": " WHERE pnl > 0", "lost": " WHERE pnl <= 0"}.get(result, "")
    n = db.execute(f"SELECT COUNT(*) FROM settlements{where}").fetchone()[0]
    rows = db.execute(f"SELECT * FROM settlements{where} ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {"total": n, "offset": offset, "limit": limit, "rows": [dict(r) for r in rows]}


def fills_csv(data: Path) -> str:
    import csv
    import io
    db = connect(Path(data))
    buf = io.StringIO()
    if db is None:
        return ""
    cur = db.execute(f"{FILL_SQL} ORDER BY f.ts")
    w = csv.writer(buf)
    cols = [d[0] for d in cur.description]
    w.writerow(["utc_time", *cols])
    for r in cur:
        w.writerow([dt.datetime.fromtimestamp(r["ts"], dt.UTC).strftime("%Y-%m-%d %H:%M:%S"), *r])
    return buf.getvalue()


def orders_page(data: Path, offset: int = 0, limit: int = 50) -> dict:
    """Live/shadow order log: every order sent (or that shadow mode would have sent) and the exchange's answer."""
    db = connect(Path(data))
    if db is None:
        return {"total": 0, "rows": [], "by_status": []}
    try:
        n = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    except sqlite3.OperationalError:          # paper ledgers created before live mode existed
        return {"total": 0, "rows": [], "by_status": []}
    rows = db.execute("SELECT * FROM orders ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    by = db.execute("SELECT COALESCE(NULLIF(code,''), status) k, COUNT(*) n FROM orders GROUP BY k ORDER BY n DESC").fetchall()
    return {"total": n, "offset": offset, "limit": limit, "rows": [dict(r) for r in rows],
            "by_status": [dict(r) for r in by]}
