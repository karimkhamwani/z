"""Performance report from the paper ledger.

    python manage.py report            # summary to the terminal
    python manage.py report --csv      # also write data/report_*.csv
"""
import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

from bot.analytics import load


def show(title, rows):
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    cols = list(rows[0])
    fmt = lambda v: f"{v:.4f}" if isinstance(v, float) and abs(v) < 1 else (f"{v:.2f}" if isinstance(v, float) else str(v))
    w = {c: max(len(c), *(len(fmt(r[c])) for r in rows)) for c in cols}
    print("  " + "  ".join(c.rjust(w[c]) for c in cols))
    for r in rows:
        print("  " + "  ".join(fmt(r[c]).rjust(w[c]) for c in cols))


def main():
    for stream in (sys.stdout, sys.stderr):   # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(Path(__file__).resolve().parent / "data"))
    ap.add_argument("--csv", action="store_true")
    a = ap.parse_args()
    d = load(Path(a.data), recent=10**9)
    if d.get("empty"):
        print("No paper data yet — run the bot first.")
        return
    s = d["summary"]
    first = dt.datetime.fromtimestamp(s["first_fill"]).strftime("%Y-%m-%d %H:%M") if s["first_fill"] else "-"
    agree = s["model_settlement_agreement"]
    print("=" * 76)
    print(f"PAPER REPORT  (first fill {first})")
    print("=" * 76)
    print(f"equity {s['equity']:.2f}  (start {s['starting_equity']:.2f}, return {s['return_pct']:+.2f}%)   "
          f"peak {s['peak']:.2f}   max drawdown {s['max_drawdown']:.1%}   halted: {s['halted'] or 'no'}")
    print(f"markets settled {s['markets_settled']}   fills {s['fills']} ({s['fills_settled']} settled)   "
          f"open positions {s['open_positions']}")
    print(f"notional {s['notional']:.2f}   pre-fee pnl {s['gross_pnl']:+.2f}   fees {s['fees']:.2f}   "
          f"net trading pnl {s['net_trading_pnl']:+.2f} ({s['net_pct']:+.2f}% of notional)   rebates {s['rebates']:.2f}")
    if s["market_win_rate"] is not None:
        print(f"market win rate {s['market_win_rate']:.1%}   avg pnl/market {s['avg_pnl_per_market']:+.2f}   "
              f"model-vs-official settlement agreement {agree[0]}/{agree[1]}")
    for t in d["tables"].values():
        show(t["title"], t["rows"])
    show("Rejected signals (momentum passed, trade skipped)", d["rejections"])
    show("Daily (UTC)", d["daily"])
    if a.csv:
        out = {**{k: v["rows"] for k, v in d["tables"].items()}, "daily": d["daily"], "fills": d["recent_fills"][::-1]}
        for name, rows in out.items():
            if rows:
                with open(Path(a.data) / f"report_{name}.csv", "w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        print(f"\nCSV written to {a.data}/report_*.csv")


if __name__ == "__main__":
    main()
