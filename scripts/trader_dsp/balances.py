"""Daily pUSD balance of the wallet (archive eth_call) and the external flow it implies.

Usage: balances.py PROCESSED_DIR
For each UTC midnight (and the cutoff) take the block of the wallet's last fill before it, read the pUSD balance at
that block, and compare the change with the trading cash flow from the ledgers over the same span:
    external flow = change in balance - (redemptions + rebates + rewards - buys)
Positive = deposit / transfer in, negative = withdrawal / transfer out.  Writes daily_balance_flows.csv.
"""
import json, sys, time, urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

P = Path(sys.argv[1])
RPCS = ["https://rpc-mainnet.matic.quiknode.pro", "https://polygon.drpc.org"]
W = "c53375ff94e96100f2b30a4b5775db35218d69a9"
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"


def balance(block):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": PUSD, "data": "0x70a08231" + "0" * 24 + W}, hex(block)]}).encode()
    for k in range(12):
        try:
            req = urllib.request.Request(RPCS[k % 2], body, {"Content-Type": "application/json", "User-Agent": "tpb-research"})
            d = json.loads(urllib.request.urlopen(req, timeout=60).read())
            if "result" in d:
                return int(d["result"], 16) / 1e6
        except Exception:                              # noqa: BLE001
            pass
        time.sleep(2 + k)
    raise RuntimeError(f"balance at {block}")


tr = pd.read_csv(P / "trades.csv.gz", usecols=["ts", "block", "cost"])
red = pd.read_csv(P / "redemptions.csv", usecols=["ts", "usdc"])
rr = pd.read_csv(P / "rebates_rewards.csv", usecols=["ts", "usdc"])
cash = pd.concat([pd.DataFrame({"ts": tr["ts"], "x": -tr["cost"]}), red.rename(columns={"usdc": "x"}),
                  rr.rename(columns={"usdc": "x"})]).sort_values("ts", kind="stable")
cts, ccum = cash["ts"].values, cash["x"].cumsum().values
fb = tr.dropna(subset=["block"]).sort_values("ts")
anchors = [int(x.timestamp()) for x in pd.date_range("2026-07-18", "2026-10-12", freq="D", tz="UTC")] + [1791766500]
rows = []
for a in anchors:
    x = fb[fb["ts"] < a].iloc[-1]
    b, t = int(x["block"]), int(x["ts"])
    i = np.searchsorted(cts, t, side="right") - 1
    rows.append({"anchor_utc": pd.to_datetime(a, unit="s", utc=True), "block": b, "last_fill_ts": t,
                 "pusd_balance": balance(b), "trading_cash_cum": float(ccum[i])})
    print(rows[-1], flush=True)
D = pd.DataFrame(rows)
D["external_flow"] = D["pusd_balance"].diff() - D["trading_cash_cum"].diff()
D.loc[0, "external_flow"] = D.loc[0, "pusd_balance"] - D.loc[0, "trading_cash_cum"]     # everything before the 1st anchor
D.to_csv(P / "daily_balance_flows.csv", index=False)
print(D[["anchor_utc", "pusd_balance", "trading_cash_cum", "external_flow"]].round(2).to_string())
print("sum external flow", round(D["external_flow"].sum(), 2), "deposits", round(D.loc[D["external_flow"] > 1, "external_flow"].sum(), 2),
      "withdrawals", round(D.loc[D["external_flow"] < -1, "external_flow"].sum(), 2))
