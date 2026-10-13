"""Two follow-up tests of the inferred entry logic.

Usage: analyze_micro2.py PROCESSED_DIR BOOKS.pkl LAG_S
A. fair value: is the wallet's taker entry explained by a spot-vs-strike fair value above the ask (a spot-lag rule)?
   fair(Up) = Phi(ln(S/K) / (sigma_1s * sqrt(seconds left))), S = this repo's composite spot, K = recorded strike,
   sigma_1s = the market's own 1-s log-return std.  Hazard of a taker order by edge = fair - ask.
B. order remainders: are maker fills the resting rest of the same limit orders that first filled as taker?
   (same market, same token, same price, shortly after a taker fill; and do taker + maker parts add up to the
   order-size menu)
Writes PROCESSED_DIR/metrics_micro2.json.
"""
import json, sys
from math import erf, sqrt
from pathlib import Path

import numpy as np
import pandas as pd

P, BK, LAG = Path(sys.argv[1]), __import__("pickle").load(open(sys.argv[2], "rb")), float(sys.argv[3])
M = {}
J = lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else str(o)
Phi = np.vectorize(lambda z: 0.5 * (1 + erf(z / sqrt(2))))


def at(t, x, when):
    i = np.searchsorted(t, when, side="right") - 1
    return np.where(i >= 0, x[np.clip(i, 0, len(x) - 1)], np.nan)


tr = pd.read_csv(P / "trades.csv.gz")

# ------------------------------------------------------------------ A. fair value
bt = tr[(tr["asset"] == "BTC") & tr["slug"].isin(BK.keys())].copy()
bt["tm"] = (bt["ts"] + 0.5 - LAG) * 1000.0
tko = bt[bt["role"] == "taker"].drop_duplicates("tx")
rows, calib = [], []
for slug in bt["slug"].unique():
    b = BK[slug]
    K = b.get("strike")
    if not K:
        continue
    t = b["t"]
    g = np.arange(b["open"] * 1000 + 3000, b["close"] * 1000 - 2000, 1000.0)
    s1 = at(t, b["spot"], g).astype(float)
    lr = np.diff(np.log(s1))
    sig = np.nanstd(lr)
    if not np.isfinite(sig) or sig <= 0:
        continue
    left = b["close"] - g / 1000.0
    fu = Phi(np.log(s1 / K) / (sig * np.sqrt(np.maximum(left, 1.0))))
    if b.get("outcome") in ("UP", "DOWN"):
        calib.append(pd.DataFrame({"fair_up": fu, "won_up": float(b["outcome"] == "UP")}))
    for side in ("Up", "Down"):
        a, bi = (b["ua"], b["ub"]) if side == "Up" else (b["da"], b["db"])
        ask, bid = at(t, a, g), at(t, bi, g)
        fair = fu if side == "Up" else 1 - fu
        own = tko[(tko["slug"] == slug) & (tko["outcome"] == side)]["tm"].values
        y = np.zeros(len(g), int)
        k = np.floor((own - g[0]) / 1000).astype(int)
        k = k[(k >= 0) & (k < len(g))]
        np.add.at(y, k, 1)
        mid = (ask + bid) / 2
        rows.append(pd.DataFrame({"edge": fair - ask, "fair_minus_mid": fair - mid, "mid": mid,
                                  "r1": mid - at(t, ((a + bi) / 2), g - 1000), "y": (y > 0).astype(int)}))
G = pd.concat(rows, ignore_index=True).dropna()
G = G[G["mid"].between(0.05, 0.95)]
br = G["y"].mean()


def lift(col, edges):
    c = pd.cut(G[col], edges)
    t = G.groupby(c, observed=False)["y"].agg(["size", "mean"])
    t.index = t.index.astype(str)
    return {k: {"n": int(v["size"]), "lift": round(v["mean"] / br, 2)} for k, v in t.iterrows()}


ed = [-1, -0.15, -0.10, -0.05, -0.02, 0, 0.02, 0.05, 0.10, 0.15, 1]
M["fair_value"] = {"rows": len(G), "base_rate": round(br, 5),
                   "lift_by_edge_fair_minus_ask": lift("edge", ed),
                   "lift_by_fair_minus_mid": lift("fair_minus_mid", ed)}
C = pd.concat(calib)
cb = C.groupby(pd.cut(C["fair_up"], np.linspace(0, 1, 11)), observed=False)["won_up"].agg(["size", "mean"])
cb.index = cb.index.astype(str)
M["fair_value"]["calibration_fair_up_vs_outcome"] = {k: [int(v["size"]), round(v["mean"], 3)] for k, v in cb.iterrows()}
# edge at the wallet's taker entries vs all token-seconds
e_in = G.loc[G["y"] == 1, "edge"] * 100
e_all = G["edge"] * 100
M["fair_value"]["edge_c_at_entries"] = {q: round(float(e_in.quantile(q)), 2) for q in (0.1, 0.25, 0.5, 0.75, 0.9)}
M["fair_value"]["edge_c_all_seconds"] = {q: round(float(e_all.quantile(q)), 2) for q in (0.1, 0.25, 0.5, 0.75, 0.9)}
M["fair_value"]["share_entries_edge_gt_0"] = round(float((e_in > 0).mean()), 3)
M["fair_value"]["share_all_seconds_edge_gt_0"] = round(float((e_all > 0).mean()), 3)
print(json.dumps(M["fair_value"], indent=0, default=J)[:4000], flush=True)

# ------------------------------------------------------------------ B. maker fills as order remainders
o = tr.sort_values(["slug", "outcome", "ts", "fill_id"]).reset_index(drop=True)
res = []
for (slug, side), g in o.groupby(["slug", "outcome"], sort=False):
    ts, px, role, q = g["ts"].values, g["price"].values, g["role"].values, g["shares"].values
    last_taker = {}                                  # price (rounded to 1e-4) -> last taker fill time
    for i in range(len(g)):
        key = round(px[i], 4)
        if role[i] == "maker":
            lt = last_taker.get(key)
            any_t = [v for v in last_taker.values()]
            res.append((ts[i] - lt if lt is not None else np.nan, ts[i] - max(any_t) if any_t else np.nan, q[i]))
        else:
            last_taker[key] = ts[i]
R = pd.DataFrame(res, columns=["gap_same_price", "gap_any_taker", "shares"])
M["maker_remainder"] = {
    "maker_fills": len(R),
    "share_with_earlier_taker_fill_same_token_same_price": round(float(R["gap_same_price"].notna().mean()), 3),
    "share_same_price_taker_within_5s": round(float((R["gap_same_price"] <= 5).mean()), 3),
    "share_same_price_taker_within_30s": round(float((R["gap_same_price"] <= 30).mean()), 3),
    "gap_same_price_s_quantiles": {q: round(float(R["gap_same_price"].quantile(q)), 1) for q in (0.1, 0.25, 0.5, 0.75, 0.9)},
    "share_with_no_earlier_taker_fill_on_token": round(float(R["gap_any_taker"].isna().mean()), 3),
}
# do a non-integer taker fill plus the next same-price maker fills complete a round order size?
menu = np.array([5, 10, 15, 20, 25, 30, 40, 45, 50, 60, 70, 75, 80, 90, 100, 120, 150, 200])
hit, tot = 0, 0
for (slug, side), g in o.groupby(["slug", "outcome"], sort=False):
    ts, px, role, q = g["ts"].values, g["price"].values, g["role"].values, g["shares"].values
    for i in np.where((role == "taker") & (np.abs(q - np.round(q)) > 1e-6))[0]:
        tot += 1
        s = q[i]
        for j in range(i + 1, min(i + 40, len(g))):
            if ts[j] - ts[i] > 120:
                break
            if role[j] == "maker" and abs(px[j] - px[i]) < 1e-4:
                s += q[j]
                if np.any(np.abs(menu - s) < 0.011):
                    hit += 1
                    break
M["maker_remainder"]["partial_taker_fills_completed_to_menu_size_by_later_same_price_maker_fills"] = {
    "partial_taker_fills": tot, "completed": hit, "share": round(hit / max(tot, 1), 3)}
print(json.dumps(M["maker_remainder"], indent=0, default=J), flush=True)
json.dump(M, open(P / "metrics_micro2.json", "w"), indent=1, default=J)
print("written")
