"""Binance 1-second closes for each underlying over the last 7 days -> raw/prices_1s_<SYM>.json"""
import json, urllib.request, time, sys
from concurrent.futures import ThreadPoolExecutor
SYMS = sys.argv[1:] or ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT","DOGEUSDT","HYPEUSDT"]
now = int(time.time()); end = now - now % 1800 + 1800; start = end - 7 * 86400 - 3600
def get(sym, s):
    u = f"https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1s&limit=1000&startTime={s*1000}"
    for i in range(6):
        try: return json.load(urllib.request.urlopen(u, timeout=30))
        except Exception: time.sleep(2 * (i + 1))
    return []
for sym in SYMS:
    with ThreadPoolExecutor(8) as ex: res = list(ex.map(lambda s: get(sym, s), range(start, end, 1000)))
    px = {k[0] // 1000: float(k[4]) for r in res for k in r}
    json.dump(px, open(f"raw/prices_1s_{sym}.json", "w")); print(sym, len(px), flush=True)
