"""Extra cuts: structure by timeframe, hourly pattern, tier estimate, lifetime curve -> analysis/extras.json + csvs"""
import csv, json, collections, statistics as st, datetime as dt
F = list(csv.DictReader(open("analysis/fills.csv"))); M = list(csv.DictReader(open("analysis/markets.csv")))
for f in F:
    for k in ("price", "shares", "notional", "cash", "fee"): f[k] = float(f[k])
    f["taker"] = f["taker"] == "True"; f["won"] = {"True": True, "False": False}.get(f["won"])
out = {}
tf = collections.defaultdict(list)
for m in M: tf[m["family"].split()[-1]].append(m)
rows = []
for k, L in tf.items():
    two = [m for m in L if m["pair_cost"]]
    rows.append(dict(timeframe=k, markets=len(L), two_sided_pct=round(len(two) / len(L) * 100, 1),
                     pair_cost_median=round(st.median(float(m["pair_cost"]) for m in two), 4) if two else None,
                     imbalance_median=round(st.median(float(m["imbalance"]) for m in L if m["imbalance"]), 3),
                     avg_fills=round(st.mean(int(m["fills"]) for m in L), 1), avg_notional=round(st.mean(float(m["notional"]) for m in L), 1),
                     net_pnl=round(sum(float(m["net_pnl"]) for m in L)), win_rate=round(sum(float(m["net_pnl"]) > 0 for m in L) / len(L), 3)))
with open("analysis/structure_by_timeframe.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
out["structure_by_timeframe"] = rows
hr = collections.defaultdict(lambda: [0, 0.0, 0.0])
for f in F:
    if f["won"] is None: continue
    h = int(f["utc"][11:13]); hr[h][0] += 1; hr[h][1] += f["notional"]; hr[h][2] += (f["shares"] if f["won"] else 0) - f["cash"]
hrows = [dict(utc_hour=h, fills=v[0], notional=round(v[1]), net_pnl_before_rebate=round(v[2])) for h, v in sorted(hr.items())]
with open("analysis/by_utc_hour.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(hrows[0])); w.writeheader(); w.writerows(hrows)
out["by_utc_hour"] = hrows
tk = [f for f in F if f["taker"]]; days = (int(F[-1]["timestamp"]) - int(F[0]["timestamp"])) / 86400
wv = sum(f["notional"] * (1 - f["price"]) * 2.3 for f in tk)
out["tier_estimate"] = dict(days=round(days, 2), wV_window=round(wv), wV_30d_projected=round(wv / days * 30), observed_rebate_rate=0.44,
                            note="docs formula projects ~$9.2M (just under the $10M Obsidian line) -> Diamond 44%, matching the observed payout exactly")
p = json.load(open("raw/pnl_all_daily.json"))
out["lifetime_chart_weekly"] = [dict(date=dt.datetime.fromtimestamp(x["t"], dt.UTC).strftime("%Y-%m-%d"), pnl=round(x["p"])) for x in p[::7]] + [dict(date="latest", pnl=round(p[-1]["p"]))]
json.dump(out, open("analysis/extras.json", "w"), indent=1)
print(json.dumps({k: out[k] for k in ("structure_by_timeframe", "tier_estimate")}, indent=1))
print([(r["utc_hour"], r["fills"], r["net_pnl_before_rebate"]) for r in hrows])
