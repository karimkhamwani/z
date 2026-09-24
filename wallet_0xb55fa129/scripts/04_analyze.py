"""Core analysis. Inputs: raw/activity_7d.json, raw/winners.json, raw/prices_1s_*.json
Outputs (analysis/): fills.csv, markets.csv, daily_ledger.csv, by_family.csv, by_momentum.csv,
by_price.csv, by_time_in_window.csv, summary.json"""
import json, csv, math, re, os, collections, statistics as st, datetime as dt
act = json.load(open("raw/activity_7d.json")); WIN = json.load(open("raw/winners.json"))
SYM = {"btc": "BTCUSDT", "bitcoin": "BTCUSDT", "eth": "ETHUSDT", "ethereum": "ETHUSDT", "sol": "SOLUSDT", "solana": "SOLUSDT",
       "xrp": "XRPUSDT", "bnb": "BNBUSDT", "doge": "DOGEUSDT", "dogecoin": "DOGEUSDT", "hype": None, "hyperliquid": None}
DUR = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
MONTHS = {m: i + 1 for i, m in enumerate(["january","february","march","april","may","june","july","august","september","october","november","december"])}
def parse(slug):
    m = re.match(r"([a-z]+)-updown-(\d+[mh])-(\d+)$", slug)
    if m: return m.group(1), m.group(2), int(m.group(3))
    m = re.match(r"([a-z]+)-up-or-down-([a-z]+)-(\d+)-(\d{4})-(\d+)(am|pm)-et$", slug)
    if m:
        a, mon, d, y, h, ap = m.groups(); h = int(h) % 12 + (12 if ap == "pm" else 0)
        start = int(dt.datetime(int(y), MONTHS[mon], int(d), h, tzinfo=dt.timezone(dt.timedelta(hours=-4))).timestamp())  # EDT
        return a, "1h", start
    m = re.match(r"([a-z]+)-above-", slug)
    if m: return m.group(1), "strike", None
    return "other", "other", None
PX = {}
for s in set(v for v in SYM.values() if v):
    fn = f"raw/prices_1s_{s}.json"
    if os.path.exists(fn): PX[s] = {int(k): v for k, v in json.load(open(fn)).items()}
def P(sym, t):
    px = PX.get(sym)
    if not px: return None
    for d in range(10):
        if t - d in px: return px[t - d]
SIG = {}
for s, px in PX.items():
    ks = sorted(px); r = [math.log(px[ks[i]] / px[ks[i-1]]) for i in range(1, len(ks)) if ks[i] - ks[i-1] == 1]
    SIG[s] = st.pstdev(r)
Phi = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
os.makedirs("analysis", exist_ok=True)
fills = []
for x in act:
    if x["type"] != "TRADE": continue
    w = (WIN.get(x["conditionId"]) or {}).get("winner")
    asset, tf, start = parse(x["slug"]); sym = SYM.get(asset)
    fee = x["usdcSize"] - x["size"] * x["price"]
    f = dict(timestamp=x["timestamp"], utc=dt.datetime.fromtimestamp(x["timestamp"], dt.UTC).strftime("%Y-%m-%d %H:%M:%S"),
             family=f"{asset} {tf}", asset=asset, tf=tf, slug=x["slug"], conditionId=x["conditionId"], outcome=x["outcome"],
             price=x["price"], shares=x["size"], notional=x["size"] * x["price"], cash=x["usdcSize"], fee=fee, taker=fee > 1e-4,
             winner=w, won=(w == x["outcome"]) if w else None, sec_into_window=None, sec_left=None,
             mom3_bp=None, mom10_bp=None, next5_bp=None, fair=None)
    if start and sym and tf in DUR and x["outcome"] in ("Up", "Down"):
        t = x["timestamp"]; o = P(sym, start); pt = P(sym, t); sg = 1 if x["outcome"] == "Up" else -1
        f["sec_into_window"] = t - start; f["sec_left"] = start + DUR[tf] - t
        if o and pt:
            p3, p10, n5 = P(sym, t - 3), P(sym, t - 10), P(sym, t + 5)
            f["mom3_bp"] = sg * math.log(pt / p3) * 1e4 if p3 else None
            f["mom10_bp"] = sg * math.log(pt / p10) * 1e4 if p10 else None
            f["next5_bp"] = sg * math.log(n5 / pt) * 1e4 if n5 else None
            fu = Phi(math.log(pt / o) / (SIG[sym] * math.sqrt(max(f["sec_left"], 1))))
            f["fair"] = fu if sg == 1 else 1 - fu
    fills.append(f)
with open("analysis/fills.csv", "w", newline="") as fh:
    wr = csv.DictWriter(fh, fieldnames=list(fills[0])); wr.writeheader(); wr.writerows(fills)
R = [f for f in fills if f["won"] is not None]
def agg(L):
    n = sum(f["notional"] for f in L); cash = sum(f["cash"] for f in L); pay = sum(f["shares"] for f in L if f["won"])
    tk = [f for f in L if f["taker"]]; tn = sum(f["notional"] for f in tk)
    return dict(fills=len(L), notional=round(n, 2), cash=round(cash, 2), fees=round(cash - n, 2), payout=round(pay, 2),
                prefee_pnl=round(pay - n, 2), net_pnl=round(pay - cash, 2), prefee_pct=round((pay - n) / n * 100, 3) if n else None,
                net_pct=round((pay - cash) / n * 100, 3) if n else None, taker_share_notional=round(tn / n * 100, 1) if n else None,
                avg_price=round(n / sum(f["shares"] for f in L), 4) if L else None,
                taker_fee_pct=round(sum(f["fee"] for f in tk) / tn * 100, 3) if tn else None)
def write(name, rows):
    with open(f"analysis/{name}.csv", "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0])); wr.writeheader(); wr.writerows(rows)
# rebates, matched to the day that earned them (paid just after 00:00 UTC for previous day)
reb = collections.defaultdict(lambda: {"taker_rebate": 0, "maker_rebate": 0})
for x in act:
    if x["type"] in ("TAKER_REBATE", "MAKER_REBATE"):
        reb[x["timestamp"] // 86400 - 1][x["type"].lower()] += x["usdcSize"]
# daily ledger
byday = collections.defaultdict(list)
for f in R: byday[f["timestamp"] // 86400].append(f)
daily = []
for d in sorted(byday):
    L = byday[d]; tk = [f for f in L if f["taker"]]; mk = [f for f in L if not f["taker"]]
    tpre = sum((f["shares"] if f["won"] else 0) - f["notional"] for f in tk); mpre = sum((f["shares"] if f["won"] else 0) - f["notional"] for f in mk)
    fees = sum(f["fee"] for f in L); tr, mr = reb[d]["taker_rebate"], reb[d]["maker_rebate"]
    daily.append(dict(day=dt.datetime.fromtimestamp(d * 86400, dt.UTC).strftime("%Y-%m-%d"), fills=len(L), notional=round(sum(f["notional"] for f in L)),
                      taker_edge=round(tpre), fees=round(fees), maker_edge=round(mpre), taker_rebate=round(tr), maker_rebate=round(mr),
                      net=round(tpre + mpre - fees + tr + mr), rebate_to_prevday_fee=round(tr / fees, 3) if fees else None))
write("daily_ledger", daily)
fam = collections.defaultdict(list)
for f in R: fam[f["family"]].append(f)
write("by_family", sorted([dict(family=k, **agg(v)) for k, v in fam.items()], key=lambda r: -r["notional"]))
tfm = collections.defaultdict(list)
for f in R: tfm[f["tf"]].append(f)
write("by_timeframe", sorted([dict(timeframe=k, **agg(v)) for k, v in tfm.items()], key=lambda r: -r["notional"]))
am = collections.defaultdict(list)
for f in R: am[f["asset"]].append(f)
write("by_asset", sorted([dict(asset=k, **agg(v)) for k, v in am.items()], key=lambda r: -r["notional"]))
T = [f for f in R if f["taker"] and f["mom3_bp"] is not None]
B = [(-99, -2, "<= -2bp"), (-2, -0.5, "-2..-0.5"), (-0.5, 0.5, "flat"), (0.5, 2, "+0.5..+2"), (2, 99, ">= +2bp")]
write("by_momentum_taker", [dict(bucket=lab, **agg([f for f in T if lo <= f["mom3_bp"] < hi])) for lo, hi, lab in B])
M = [f for f in R if not f["taker"] and f["mom3_bp"] is not None]
write("by_momentum_maker", [dict(bucket=lab, **agg([f for f in M if lo <= f["mom3_bp"] < hi])) for lo, hi, lab in B])
write("by_price", [dict(price_bucket=f"{lo:.1f}-{lo+0.1:.1f}", **agg([f for f in R if lo <= f["price"] < lo + 0.1 + (0.01 if lo >= 0.9 else 0)])) for lo in [i / 10 for i in range(10)]])
tw = [f for f in R if f["sec_left"] is not None]
def frac_bucket(f):
    tot = f["sec_into_window"] + f["sec_left"]; return min(int(f["sec_into_window"] / tot * 10), 9) if tot else None
write("by_time_in_window", [dict(decile=f"{i*10}-{i*10+10}% elapsed", **agg([f for f in tw if frac_bucket(f) == i])) for i in range(10)])
# per market
mk = collections.defaultdict(list)
for f in R: mk[f["conditionId"]].append(f)
mrows = []
for cid, L in mk.items():
    up = sum(f["shares"] for f in L if f["outcome"] in ("Up", "Yes")); dn = sum(f["shares"] for f in L if f["outcome"] in ("Down", "No"))
    upc = sum(f["notional"] for f in L if f["outcome"] in ("Up", "Yes")); dnc = sum(f["notional"] for f in L if f["outcome"] in ("Down", "No"))
    mrows.append(dict(conditionId=cid, slug=L[0]["slug"], family=L[0]["family"], winner=L[0]["winner"], fills=len(L),
                      shares_up_yes=round(up, 3), shares_down_no=round(dn, 3), avg_up=round(upc / up, 4) if up else None, avg_down=round(dnc / dn, 4) if dn else None,
                      pair_cost=round(upc / up + dnc / dn, 4) if up and dn else None, imbalance=round(abs(up - dn) / (up + dn), 4) if up + dn else None,
                      notional=round(upc + dnc, 2), cash=round(sum(f["cash"] for f in L), 2), payout=round(sum(f["shares"] for f in L if f["won"]), 2),
                      net_pnl=round(sum(f["shares"] for f in L if f["won"]) - sum(f["cash"] for f in L), 2)))
write("markets", sorted(mrows, key=lambda r: r["slug"]))
two = [m for m in mrows if m["pair_cost"]]
pn = [m["net_pnl"] for m in mrows]; q = lambda L, p: sorted(L)[int(p * (len(L) - 1))]
full = [r for r in daily if r["fills"] > 0][1:-1] if len(daily) > 2 else daily
S = dict(all=agg(R), taker=agg([f for f in R if f["taker"]]), maker=agg([f for f in R if not f["taker"]]),
         markets=len(mrows), two_sided_share=round(len(two) / len(mrows), 4), pair_cost_median=st.median(m["pair_cost"] for m in two),
         imbalance_median=st.median(m["imbalance"] for m in mrows if m["imbalance"] is not None),
         market_pnl=dict(mean=round(st.mean(pn), 2), median=round(st.median(pn), 2), sd=round(st.pstdev(pn), 2), p5=round(q(pn, .05), 2), p95=round(q(pn, .95), 2), win_rate=round(sum(p > 0 for p in pn) / len(pn), 4)),
         fill_cash_median=st.median(f["cash"] for f in R), fill_cash_p90=q([f["cash"] for f in R], .9),
         taker_fill_share=round(sum(f["taker"] for f in R) / len(R), 4),
         rebates_total=dict(taker=round(sum(x["usdcSize"] for x in act if x["type"] == "TAKER_REBATE"), 2), maker=round(sum(x["usdcSize"] for x in act if x["type"] == "MAKER_REBATE"), 2)),
         full_days=[r["day"] for r in full], full_days_net=sum(r["net"] for r in full),
         momentum_taker_mean=dict(m3=st.mean(f["mom3_bp"] for f in T), next5=st.mean(f["next5_bp"] for f in T if f["next5_bp"] is not None)) if T else None,
         model_edge_taker=st.mean(f["fair"] - f["price"] for f in T if f["fair"] is not None) if T else None,
         sigma_per_sec=SIG)
json.dump(S, open("analysis/summary.json", "w"), indent=1, default=str)
print(json.dumps(S, indent=1, default=str)[:3500])
for r in daily: print(r)
