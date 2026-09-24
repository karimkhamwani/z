"""Fetch full activity for a wallet over N days (30-min windows to dodge the offset cap),
plus positions, closed positions, P&L series. Writes raw JSON + trades CSV."""
import json, urllib.request, time, sys, csv
from concurrent.futures import ThreadPoolExecutor
W = "0xb55fa1296e6ec55d0ce53d93b9237389f11764d4"
DAYS = 7
UA = {"User-Agent": "Mozilla/5.0"}
def get(url):
    for i in range(6):
        try: return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))
        except Exception as e: err = e; time.sleep(2 * (i + 1))
    print("ERR", url, err, file=sys.stderr); return None
def window(s, span=1800):
    out = []; off = 0
    while True:
        d = get(f"https://data-api.polymarket.com/activity?user={W}&limit=500&offset={off}&start={s}&end={s+span-1}&sortDirection=ASC")
        if d is None: print("FAILED WINDOW", s, file=sys.stderr); break
        out += d; off += 500
        if len(d) < 500: break
        if off > 3000:  # split window if dense
            print("dense window, splitting", s, file=sys.stderr)
            return window(s, span // 2) + window(s + span // 2, span - span // 2)
    return out
now = int(time.time()); end = now - now % 1800 + 1800; start = end - DAYS * 86400
with ThreadPoolExecutor(6) as ex: res = list(ex.map(window, range(start, end, 1800)))
seen = set(); act = []
for w in res:
    for r in w:
        k = (r["transactionHash"], r["asset"], r["type"], r["size"], r["price"], r.get("side"), r["timestamp"])
        if k in seen: continue
        seen.add(k); act.append(r)
act.sort(key=lambda r: r["timestamp"])
json.dump(act, open("raw/activity_7d.json", "w"))
print("activity events", len(act), "span", start, end)
cols = ["timestamp", "type", "side", "outcome", "price", "size", "usdcSize", "slug", "title", "conditionId", "asset", "transactionHash"]
with open("raw/activity_7d.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(act)
def pull(kind, extra=""):
    out = []; off = 0
    while True:
        d = get(f"https://data-api.polymarket.com/{kind}?user={W}&limit=500&offset={off}{extra}")
        if not isinstance(d, list): break
        out += d; off += 500
        if len(d) < 500: break
    return out
json.dump(pull("positions", "&sizeThreshold=0"), open("raw/positions.json", "w"))
json.dump(get(f"https://user-pnl-api.polymarket.com/user-pnl?user_address={W}&interval=all&fidelity=1d"), open("raw/pnl_all_daily.json", "w"))
json.dump(get(f"https://user-pnl-api.polymarket.com/user-pnl?user_address={W}&interval=1w&fidelity=1h"), open("raw/pnl_1w_hourly.json", "w"))
json.dump({"value": get(f"https://data-api.polymarket.com/value?user={W}"),
           "traded": get(f"https://data-api.polymarket.com/traded?user={W}"),
           "leaderboard": get(f"https://data-api.polymarket.com/v1/leaderboard?user={W}&timePeriod=ALL")}, open("raw/profile_stats.json", "w"), indent=1)
print("done")
