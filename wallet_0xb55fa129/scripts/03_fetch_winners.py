"""Official resolution (winning outcome) for every market in the activity file -> raw/winners.json"""
import json, urllib.request, time
from concurrent.futures import ThreadPoolExecutor
act = json.load(open("raw/activity_7d.json"))
cids = sorted({x["conditionId"] for x in act if x["type"] == "TRADE"})
try: W = json.load(open("raw/winners.json"))
except Exception: W = {}
def get(cid):
    for i in range(5):
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(f"https://clob.polymarket.com/markets/{cid}", headers={"User-Agent": "Mozilla/5.0"}), timeout=10))
            w = [t["outcome"] for t in d["tokens"] if t.get("winner")]
            return cid, {"winner": w[0] if w else None, "closed": d.get("closed"), "fee_bps": [d.get("maker_base_fee"), d.get("taker_base_fee")], "question": d.get("question")}
        except Exception: time.sleep(1.5 * (i + 1))
    return cid, None
todo = [c for c in cids if not (W.get(c) and W[c].get("winner"))]
print("markets", len(cids), "to fetch", len(todo), flush=True)
with ThreadPoolExecutor(10) as ex:
    for cid, r in ex.map(get, todo):
        if r: W[cid] = r
json.dump(W, open("raw/winners.json", "w"))
print("resolved", sum(1 for c in cids if W.get(c, {}) and W[c].get("winner")), "/", len(cids))
