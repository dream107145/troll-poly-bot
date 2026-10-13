"""Find the transactions behind the external flows that daily_balance_flows.csv shows.

Usage: external_flows.py PROCESSED_DIR RAW_DIR
1. inflow days: pUSD transfers into the wallet that are not trades / redemptions / rebates / rewards
2. the first withdrawal day: every pUSD transfer out of the wallet that is not a trade (small 2k-block chunks)
   -> the withdrawal destination(s)
3. all pUSD transfers from the wallet to those destinations over the whole period (cheap: both topics filtered)
Writes RAW_DIR/chain/external_flows.json and checks the per-day sums against the balance identity.
"""
import gzip, json, os, sys, time, urllib.request
from pathlib import Path

import pandas as pd

P, RAW = Path(sys.argv[1]), Path(sys.argv[2])
W = "0x" + "0" * 24 + "c53375ff94e96100f2b30a4b5775db35218d69a9"
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
EX = "e111180000d2663c0091e4f400237545b87b996b"
RPCS = os.environ.get("RPCS", "https://rpc-mainnet.matic.quiknode.pro,https://polygon.api.onfinality.io/public").split(",")
SMALL, DAY, BIG = (int(os.environ.get(k, v)) for k, v in (("STEP_SMALL", "9000"), ("STEP_DAY", "2000"), ("STEP_BIG", "9000")))


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for k in range(16):
        try:
            req = urllib.request.Request(RPCS[k % len(RPCS)], body, {"Content-Type": "application/json", "User-Agent": "tpb-research"})
            d = json.loads(urllib.request.urlopen(req, timeout=60).read())
            if "result" in d:
                return d["result"]
        except Exception:                              # noqa: BLE001
            pass
        time.sleep(1 + k)
    raise RuntimeError((method, params))


def logs(topics, a, b, step):
    out = []
    for s in range(a, b + 1, step):
        out += rpc("eth_getLogs", [{"address": PUSD, "fromBlock": hex(s), "toBlock": hex(min(s + step - 1, b)), "topics": topics}])
    return out


def row(x, direction):
    blk = int(x["blockNumber"], 16)
    t = int(rpc("eth_getBlockByNumber", [hex(blk), False])["timestamp"], 16)
    return {"direction": direction, "block": blk, "time_utc": str(pd.to_datetime(t, unit="s", utc=True)), "tx": x["transactionHash"],
            "counterparty": "0x" + x["topics"][1 if direction == "in" else 2][-40:], "usdc": int(x["data"], 16) / 1e6}


D = pd.read_csv(P / "daily_balance_flows.csv")
known = {json.loads(l)["transactionHash"] for l in gzip.open(RAW / "activity.jsonl.gz", "rt")}
span = lambda i: (int(D.loc[i - 1, "block"]) + 1 if i > 0 else int(D.loc[i, "block"]) - 60000, int(D.loc[i, "block"]))
res = []
flow_days = list(D.index[D["external_flow"].abs() > 1])
# 1. inflows
for i in [i for i in flow_days if D.loc[i, "external_flow"] > 0]:
    a, b = span(i)
    res += [row(x, "in") for x in logs([T, None, W], a, b, SMALL) if x["transactionHash"] not in known]
    print("inflow day", D.loc[i, "anchor_utc"], "found", [r["usdc"] for r in res if r["direction"] == "in"], flush=True)
# 2. destinations from the first withdrawal day
first_out = [i for i in flow_days if D.loc[i, "external_flow"] < 0][0]
a, b = span(first_out)
dest = {}
for x in logs([T, W], a, b, DAY):
    to = x["topics"][2][-40:]
    if to != EX and x["transactionHash"] not in known:      # maker fills pay their counterparty directly: skip trades
        dest["0x" + to] = dest.get("0x" + to, 0) + int(x["data"], 16) / 1e6
print("destinations on the first withdrawal day:", dest, flush=True)
# 3. every transfer to those destinations, whole period
B0, B1 = int(D["block"].iloc[0]) - 60000, int(D["block"].iloc[-1])
for d in dest:
    res += [row(x, "out") for x in logs([T, W, "0x" + "0" * 24 + d[2:]], B0, B1, BIG) if x["transactionHash"] not in known]
json.dump(res, open(RAW / "chain" / "external_flows.json", "w"), indent=1)
# check against the balance identity per day
F = pd.DataFrame(res)
F["signed"] = F["usdc"].where(F["direction"] == "in", -F["usdc"])
F["t"] = pd.to_datetime(F["time_utc"])
D["anchor"] = pd.to_datetime(D["anchor_utc"])
for i in flow_days:
    lo = D.loc[i - 1, "anchor"] if i > 0 else D.loc[i, "anchor"] - pd.Timedelta(days=2)
    got = F[(F["t"] > lo) & (F["t"] <= D.loc[i, "anchor"])]["signed"].sum()
    print(str(D.loc[i, "anchor"])[:10], "balance-implied", round(D.loc[i, "external_flow"], 2), "transactions", round(got, 2), flush=True)
for r in res:
    print(r)
