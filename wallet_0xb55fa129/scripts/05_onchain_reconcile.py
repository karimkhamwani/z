"""Reconcile on-chain pUSD transfers with activity for the last ~N hours (public RPC keeps ~1.5 days of logs)."""
import json, urllib.request, collections, sys
RPC = "https://polygon-bor-rpc.publicnode.com"; HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 12
Wl = "0xb55fa1296e6ec55d0ce53d93b9237389f11764d4"; W = "0x" + "0" * 24 + Wl[2:]
TOK = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"; TR = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
UA = {"content-type": "application/json", "User-Agent": "Mozilla/5.0"}
def call(m, p): return json.load(urllib.request.urlopen(urllib.request.Request(RPC, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode(), headers=UA), timeout=60))
latest = int(call("eth_blockNumber", [])["result"], 16); start = latest - int(HOURS * 3600 / 1.5)
bt = lambda b: int(call("eth_getBlockByNumber", [hex(b), False])["result"]["timestamp"], 16)
logs = []
for b in range(start, latest, 2000):
    for tp in ([TR, W], [TR, None, W]):
        r = call("eth_getLogs", [{"address": TOK, "fromBlock": hex(b), "toBlock": hex(min(b + 1999, latest)), "topics": tp}])
        logs += r.get("result", [])
act = json.load(open("raw/activity_7d.json")); tend = act[-1]["timestamp"]
tl = bt(latest); tsb = lambda b: tl - (latest - b) * RATE
RATE = (tl - bt(latest - 20000)) / 20000
logs = [l for l in logs if tsb(int(l["blockNumber"], 16)) <= tend - 60]   # 60s safety margin
bmin = min(int(l["blockNumber"], 16) for l in logs); bmax = max(int(l["blockNumber"], 16) for l in logs); t0, t1 = bt(bmin), bt(bmax)
inflow = collections.defaultdict(float); out = 0
for l in logs:
    v = int(l["data"], 16) / 1e6; fr = "0x" + l["topics"][1][-40:]
    if fr == Wl: out += v
    else: inflow[fr] += v
seg = [x for x in act if t0 <= x["timestamp"] <= t1]
c = collections.defaultdict(float)
for x in seg: c[x["type"]] += x["usdcSize"]
res = dict(window_utc=[t0, t1], hours=round((t1 - t0) / 3600, 2), onchain_out=round(out, 2), onchain_in=round(sum(inflow.values()), 2),
           onchain_in_by_sender={k: round(v, 2) for k, v in inflow.items()}, activity_by_type={k: round(v, 2) for k, v in c.items()},
           activity_net=round(c["REDEEM"] + c["TAKER_REBATE"] + c["MAKER_REBATE"] - c["TRADE"], 2), onchain_net=round(sum(inflow.values()) - out, 2))
json.dump(res, open("analysis/onchain_reconciliation.json", "w"), indent=1); print(json.dumps(res, indent=1))
