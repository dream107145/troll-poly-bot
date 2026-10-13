"""Token markouts after spot-momentum candidates, executed with the calibrated fill model.

For every candidate (|z1| >= K, one per direction per second, 30-290 s left; side = the move's direction):
FOK at ask + 2c; if filled, the round trip's net per share if sold at the BID (taker, both fees) at
fill-known + h seconds, and held to settlement. Per asset and split; plus by the model's edge decile.
"""
import json
import math
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L  # noqa: E402

H = (0, 2, 5, 10, 30, 60)


def main(K=2.0):
    ex = L.Exec()
    print(f"# Markouts after |z1| >= {K} spot moves (calibrated fills, FOK ask+2c, taker both ways)\n")
    print("Net USDC per share after both fees; `settle` = held to settlement. fill% = fills / candidates.\n")
    print("| asset | split | cands | fill% | " + " | ".join(f"+{h}s" for h in H) + " | settle | win% |")
    print("|---|---|---:|---:|" + "---:|" * (len(H) + 2))
    edge_rows = []
    for asset in L.ASSETS:
        wins = L.prepare(L.load_asset(asset))
        model = json.load(open(f"{L.CACHE}/fair_{asset.lower()}.json"))
        acc = defaultdict(lambda: defaultdict(list))
        for w in wins:
            P = L.predict(model, L.design(w.F))
            last = {1: -1e18, -1: -1e18}
            z1 = w.F["z1"]
            for i in range(w.n):
                z = z1[i]
                if not (abs(z) >= K) or not (30 <= w.left[i] <= 290):
                    continue
                d = 1 if z > 0 else -1
                t = w.T[i]
                if t - last[d] < 1000:
                    continue
                last[d] = t
                side = "UP" if d > 0 else "DOWN"
                ask = (w.ua if d > 0 else w.da)[i]
                if math.isnan(ask) or not (0.05 <= ask <= 0.95):
                    continue
                fair = P[i] if d > 0 else 1 - P[i]
                r = acc[w.split]
                r["n"].append(1)
                px, known = ex.taker_buy(w, side, t, round(ask + 0.02, 2))
                if px is None:
                    continue
                r["fill"].append(1)
                bid = w.ub if d > 0 else w.db
                vals = []
                for h in H:
                    j = w.ix(known + h * 1000)
                    b = bid[j] if j >= 0 else np.nan
                    if w.T[j] >= w.close - 1000 or math.isnan(b):
                        b = 1.0 if w.outcome == side else 0.0
                        v = b - px - L.fee(px)
                    else:
                        v = b - px - L.fee(px) - L.fee(b)
                    r[h].append(v)
                st = (1.0 if w.outcome == side else 0.0) - px - L.fee(px)
                r["settle"].append(st)
                r["win"].append(1.0 if w.outcome == side else 0.0)
                edge_rows.append((asset, w.split, fair - ask - L.fee(ask), st, r[5][-1]))
        for split in ("train", "validation", "test", "forward"):
            r = acc[split]
            n, f = len(r["n"]), len(r["fill"])
            if not f:
                continue
            print(f"| {asset} | {split} | {n} | {f / n:.0%} | " + " | ".join(f"{np.mean(r[h]):+.4f}" for h in H)
                  + f" | {np.mean(r['settle']):+.4f} | {np.mean(r['win']):.0%} |")
    print("\n## Settlement net per share by the model's edge (fair - ask - fee) at the decision, all assets\n")
    print("| split | edge bucket | fills | settle net/share | +5 s net/share |")
    print("|---|---|---:|---:|---:|")
    E = np.array([(r[2], r[3], r[4]) for r in edge_rows])
    S = np.array([r[1] for r in edge_rows])
    for split in ("train", "validation", "test", "forward"):
        m = S == split
        for lo, hi in ((-1, -0.05), (-0.05, -0.02), (-0.02, 0.0), (0.0, 0.02), (0.02, 0.05), (0.05, 1)):
            k = m & (E[:, 0] >= lo) & (E[:, 0] < hi)
            if k.sum():
                print(f"| {split} | {lo:+.2f}..{hi:+.2f} | {k.sum()} | {E[k, 1].mean():+.4f} | {E[k, 2].mean():+.4f} |")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 2.0)
