"""Are the BTC markets the wallet skipped different from the ones it traded?  (recorded books, 09-22 -> 10-11)

Usage: skipped_markets.py PROCESSED_DIR BOOKS.pkl  ->  PROCESSED_DIR/skipped_markets.json
"Isolated" = both neighbouring 5-minute slots were traded, so the skip is not an outage.
"""
import json, pickle, sys
from pathlib import Path

import numpy as np
import pandas as pd

P = Path(sys.argv[1])
BK = pickle.load(open(sys.argv[2], "rb"))
mk = pd.read_csv(P / "markets.csv")
traded = set(mk["slug"])
bset = set(mk.loc[mk["asset"] == "BTC", "market_open"])
rows = []
for s, x in BK.items():
    o = int(s.rsplit("-", 1)[1])
    if o < 1790091600 or o > 1791700000:
        continue
    t = x["t"]
    g = np.arange(o * 1000 + 3000, o * 1000 + 297000, 1000.0)
    i = np.clip(np.searchsorted(t, g, side="right") - 1, 0, len(t) - 1)
    mid = ((x["ua"] + x["ub"]) / 2)[i]
    d = np.abs(np.diff(mid))
    d = d[~np.isnan(d)]
    rows.append({"traded": s in traded, "isolated": (o - 300 in bset) and (o + 300 in bset), "path": float(d.sum()),
                 "jumps2c": int((d >= 0.02).sum()), "inband": float(np.nanmean((mid > 0.1) & (mid < 0.9)))})
R = pd.DataFrame(rows)
iso = R[R["isolated"]]
out = {"recorded_btc_markets": len(R), "traded": int(R["traded"].sum()), "untraded": int((~R["traded"]).sum()),
       "isolated_untraded": int((~iso["traded"]).sum()),
       "isolated_comparison_traded_vs_untraded_medians": iso.groupby("traded")[["path", "jumps2c", "inband"]].median().round(3).to_dict(orient="index")}
json.dump(out, open(P / "skipped_markets.json", "w"), indent=1, default=str)
print(json.dumps(out, default=str))
