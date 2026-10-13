"""What the wallet's BTC fills look like against this repo's recorded order books and spot feed.

Usage: analyze_micro.py PROCESSED_DIR BOOKS.pkl
Writes PROCESSED_DIR/micro/*.csv and metrics_micro.json.

Times: the data-api / block timestamp is a whole second and the order was matched before the block, so each fill
is placed at an estimated match time  ts + 0.5 - LAG  where LAG is the shift that best lines taker prices up with
the recorded best ask.  Book values are taken from the last recorded point at or before a time.
"""
import json, pickle, sys
from pathlib import Path

import numpy as np
import pandas as pd

P = Path(sys.argv[1])
BK = pickle.load(open(sys.argv[2], "rb"))
OUT = P / "micro"
OUT.mkdir(exist_ok=True)
M = {}
J = lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else str(o)
rng = np.random.default_rng(7)

tr = pd.read_csv(P / "trades.csv.gz")
tr = tr[(tr["asset"] == "BTC") & tr["slug"].isin(BK.keys())].copy()
print(f"BTC fills with a recorded book: {len(tr)} in {tr['slug'].nunique()} markets", flush=True)
M["coverage"] = {"fills": len(tr), "markets": int(tr["slug"].nunique()),
                 "first": str(pd.to_datetime(tr["ts"].min(), unit="s", utc=True)),
                 "last": str(pd.to_datetime(tr["ts"].max(), unit="s", utc=True))}


def arrs(b, side):
    if side == "Up":
        return b["ua"], b["ub"], b["uad"], b["ubd"], 1.0
    return b["da"], b["db"], b["dad"], b["dbd"], -1.0


def at(t, x, when):
    i = np.searchsorted(t, when, side="right") - 1
    return np.where(i >= 0, x[np.clip(i, 0, len(x) - 1)], np.nan)


# ------------------------------------------------------------------ 1. alignment
tk = tr[(tr["role"] == "taker") & ((tr["price"] * 100 - (tr["price"] * 100).round()).abs() < 1e-6)]
samp = tk.sample(min(len(tk), 60000), random_state=1)
res = []
for lag in np.arange(0.0, 5.01, 0.25):
    hit = []
    for (slug, side), g in samp.groupby(["slug", "outcome"]):
        b = BK[slug]
        a, *_ = arrs(b, side)
        ask = at(b["t"], a, (g["ts"].values + 0.5 - lag) * 1000)
        hit.append(np.abs(g["price"].values - ask) < 0.005)
    hit = np.concatenate(hit)
    res.append((lag, float(np.mean(hit))))
LAG = max(res, key=lambda r: r[1])[0]
M["alignment"] = {"lag_s": float(LAG), "taker_price_equals_recorded_ask_share_by_lag": {f"{l:.2f}": round(v, 4) for l, v in res}}
print("alignment", M["alignment"], flush=True)
tr["tm"] = (tr["ts"] + 0.5 - LAG) * 1000.0        # estimated match time, ms

# ------------------------------------------------------------------ 2. per-fill book context
rows = []
for (slug, side), g in tr.groupby(["slug", "outcome"]):
    b = BK[slug]
    t = b["t"]
    a, bi, ad, bd, sg = arrs(b, side)
    mid = (a + bi) / 2
    tm = g["tm"].values
    r = {"fill_id": g["fill_id"].values, "ask": at(t, a, tm), "bid": at(t, bi, tm), "ask_depth": at(t, ad, tm),
         "bid_depth": at(t, bd, tm), "mid0": at(t, mid, tm)}
    for h in (-30, -10, -5, -3, -2, -1, 1, 2, 3, 5, 10, 30, 60):
        r[f"mid_{h:+d}"] = at(t, mid, tm + h * 1000)
    s0 = at(t, b["spot"], tm)
    for h in (-10, -3, -1, 1, 3, 10, 60):
        r[f"spot_{h:+d}"] = sg * (at(t, b["spot"], tm + h * 1000) / s0 - 1) * 1e4 * (1 if h > 0 else -1)
    rows.append(pd.DataFrame(r))
ctx = pd.concat(rows)
f = tr.merge(ctx, on="fill_id")
for h in (-30, -10, -5, -3, -2, -1):
    f[f"dmid_{h:+d}"] = f["mid0"] - f[f"mid_{h:+d}"]          # move INTO the fill (positive = token rose)
for h in (1, 2, 3, 5, 10, 30, 60):
    f[f"dmid_{h:+d}"] = f[f"mid_{h:+d}"] - f["mid0"]          # move AFTER the fill
f["spread"] = f["ask"] - f["bid"]
f["px_vs_ask_c"] = (f["price"] - f["ask"]) * 100
f["px_vs_bid_c"] = (f["price"] - f["bid"]) * 100
f.to_csv(OUT / "fills_with_book.csv.gz", index=False, compression="gzip")

# baseline: random moments in the same markets, both tokens
base = []
for slug in tr["slug"].unique():
    b = BK[slug]
    t = b["t"]
    for side in ("Up", "Down"):
        a, bi, ad, bd, sg = arrs(b, side)
        mid = (a + bi) / 2
        tm = rng.uniform(b["open"] * 1000 + 10000, b["close"] * 1000 - 10000, 20)
        m0 = at(t, mid, tm)
        d = {"mid0": m0}
        for h in (-10, -3, -1):
            d[f"dmid_{h:+d}"] = m0 - at(t, mid, tm + h * 1000)
        for h in (1, 3, 10, 60):
            d[f"dmid_{h:+d}"] = at(t, mid, tm + h * 1000) - m0
        s0 = at(t, b["spot"], tm)
        for h in (-3, -1, 1, 3):
            d[f"spot_{h:+d}"] = sg * (at(t, b["spot"], tm + h * 1000) / s0 - 1) * 1e4 * (1 if h > 0 else -1)
        base.append(pd.DataFrame(d))
base = pd.concat(base)
base = base[base["mid0"].between(0.1, 0.9)]


def es(df):
    o = {}
    for c in ("dmid_-10", "dmid_-3", "dmid_-1", "dmid_+1", "dmid_+3", "dmid_+10", "dmid_+60"):
        if c in df:
            v = df[c].dropna() * 100
            o[c + "_c"] = {"mean": round(v.mean(), 3), "se": round(v.std() / np.sqrt(len(v)), 3), "median": round(v.median(), 2),
                           "share_pos": round((v > 0.25).mean(), 3), "share_neg": round((v < -0.25).mean(), 3)}
    for c in ("spot_-3", "spot_-1", "spot_+1", "spot_+3"):
        if c in df:
            v = df[c].dropna()
            o[c + "_bps"] = {"mean": round(v.mean(), 3), "se": round(v.std() / np.sqrt(len(v)), 3), "share_pos": round((v > 0).mean(), 3)}
    return o


one = lambda d: d.drop_duplicates("tx")
M["event_study"] = {"taker_orders": es(one(f[f["role"] == "taker"])), "maker_fills": es(f[f["role"] == "maker"]),
                    "random_moments_same_markets_price_0.1_0.9": es(base),
                    "n": {"taker_orders": int(one(f[f["role"] == "taker"]).shape[0]), "maker_fills": int((f["role"] == "maker").sum()), "random": len(base)}}
print(json.dumps(M["event_study"], indent=0, default=J)[:2500], flush=True)

# ------------------------------------------------------------------ 3. execution vs the recorded book
tko = f[f["role"] == "taker"]
mko = f[f["role"] == "maker"]
M["execution_vs_book"] = {
    "taker_fill_minus_ask_c": {"p10": round(tko["px_vs_ask_c"].quantile(.1), 2), "p50": round(tko["px_vs_ask_c"].median(), 2),
                               "p90": round(tko["px_vs_ask_c"].quantile(.9), 2),
                               "below_ask": round((tko["px_vs_ask_c"] < -0.5).mean(), 3), "at_ask": round((tko["px_vs_ask_c"].abs() <= 0.5).mean(), 3),
                               "above_ask": round((tko["px_vs_ask_c"] > 0.5).mean(), 3)},
    "maker_fill_minus_bid_c": {"p10": round(mko["px_vs_bid_c"].quantile(.1), 2), "p50": round(mko["px_vs_bid_c"].median(), 2),
                               "p90": round(mko["px_vs_bid_c"].quantile(.9), 2),
                               "below_bid": round((mko["px_vs_bid_c"] < -0.5).mean(), 3), "at_bid": round((mko["px_vs_bid_c"].abs() <= 0.5).mean(), 3),
                               "above_bid": round((mko["px_vs_bid_c"] > 0.5).mean(), 3)},
    "spread_at_taker_fill_c": {"p50": round(tko["spread"].median() * 100, 2), "p90": round(tko["spread"].quantile(.9) * 100, 2)},
    "taker_shares_vs_top_ask_depth": {"median_ratio": round((tko["shares"] / tko["ask_depth"]).median(), 3),
                                      "share_order_ge_depth": round((tko["shares"] >= tko["ask_depth"] * 0.98).mean(), 3)},
}
print(json.dumps(M["execution_vs_book"], default=J), flush=True)

# ------------------------------------------------------------------ 4. entry hazard on a 1-s grid
grid = []
torders = tr[tr["role"] == "taker"].drop_duplicates("tx")
morders = tr[tr["role"] == "maker"]
for slug in tr["slug"].unique():
    b = BK[slug]
    t = b["t"]
    g0 = np.arange(b["open"] * 1000 + 3000, b["close"] * 1000 - 2000, 1000.0)
    for side in ("Up", "Down"):
        a, bi, ad, bd, sg = arrs(b, side)
        mid = (a + bi) / 2
        m0 = at(t, mid, g0)
        own = torders[(torders["slug"] == slug) & (torders["outcome"] == side)]["tm"].values
        y = np.zeros(len(g0), int)
        if len(own):
            k = np.floor((own - g0[0]) / 1000).astype(int)
            k = k[(k >= 0) & (k < len(g0))]
            np.add.at(y, k, 1)
        allown = torders[torders["slug"] == slug]["tm"].values
        idx = np.searchsorted(np.sort(allown), g0, side="left") - 1
        last_own = np.where(idx >= 0, g0 - np.sort(allown)[np.clip(idx, 0, None)] if len(allown) else np.inf, np.inf) / 1000 if len(allown) else np.full(len(g0), np.inf)
        s0 = at(t, b["spot"], g0)
        d = pd.DataFrame({"slug": slug, "side": side, "t_open": (g0 / 1000 - b["open"]), "mid": m0,
                          "spread": at(t, a, g0) - at(t, bi, g0), "ask": at(t, a, g0), "ask_depth": at(t, ad, g0),
                          "r1": m0 - at(t, mid, g0 - 1000), "r3": m0 - at(t, mid, g0 - 3000), "r10": m0 - at(t, mid, g0 - 10000),
                          "ask1": at(t, a, g0) - at(t, a, g0 - 1000),
                          "s1": sg * (s0 / at(t, b["spot"], g0 - 1000) - 1) * 1e4, "s3": sg * (s0 / at(t, b["spot"], g0 - 3000) - 1) * 1e4,
                          "s10": sg * (s0 / at(t, b["spot"], g0 - 10000) - 1) * 1e4,
                          "since_own_s": last_own, "y": (y > 0).astype(int)})
        grid.append(d)
G = pd.concat(grid, ignore_index=True)
G = G.dropna(subset=["mid", "r1", "r3", "s1", "s3", "ask"])
base_rate = G["y"].mean()
print(f"grid rows {len(G)}, entry seconds {G['y'].sum()}, base rate {base_rate:.4f}", flush=True)


def lift(col, edges):
    c = pd.cut(G[col], edges)
    t = G.groupby(c, observed=False)["y"].agg(["size", "mean"])
    t["lift"] = t["mean"] / base_rate
    t.index = t.index.astype(str)
    return {k: {"n": int(v["size"]), "entry_rate": round(v["mean"], 4), "lift": round(v["lift"], 2)} for k, v in t.iterrows()}


c_edges = [-1, -0.05, -0.03, -0.02, -0.01, -0.005, 0.005, 0.01, 0.02, 0.03, 0.05, 1]
s_edges = [-1e3, -5, -2, -1, -0.5, -0.2, 0.2, 0.5, 1, 2, 5, 1e3]
M["entry_hazard"] = {"base_rate_per_token_second": round(base_rate, 5), "rows": len(G),
                     "by_r1_token_mid_change_1s": lift("r1", c_edges), "by_r3_token_mid_change_3s": lift("r3", c_edges),
                     "by_r10": lift("r10", c_edges),
                     "by_spot_1s_toward_token_bps": lift("s1", s_edges), "by_spot_3s_toward_token_bps": lift("s3", s_edges),
                     "by_mid": lift("mid", [0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1]),
                     "by_t_open": lift("t_open", [0, 5, 10, 20, 30, 60, 120, 180, 240, 270, 290, 300]),
                     "by_spread_c": lift("spread", [-1, 0.0101, 0.0201, 0.0301, 0.0501, 1]),
                     "by_seconds_since_own_taker_order": lift("since_own_s", [-1, 1, 2, 3, 5, 10, 30, 1e9])}
# joint: token move x spot move (3 s)
G["r3b"] = pd.cut(G["r3"], [-1, -0.01, 0.01, 1], labels=["token fell >=1c", "token flat", "token rose >=1c"])
G["s3b"] = pd.cut(G["s3"], [-1e3, -1, 1, 1e3], labels=["spot against >=1bp", "spot flat", "spot toward >=1bp"])
j = G.groupby(["r3b", "s3b"], observed=False)["y"].agg(["size", "mean"])
M["entry_hazard"]["joint_r3_s3"] = {f"{a} | {b}": {"n": int(v["size"]), "lift": round(v["mean"] / base_rate, 2)} for (a, b), v in j.iterrows()}

# logistic regression (standardised features) and univariate AUCs
feats = ["r1", "r3", "r10", "ask1", "s1", "s3", "s10", "mid", "spread", "t_open"]
H = G[(G["mid"].between(0.05, 0.95))].dropna(subset=feats).copy()
H["mid_dist"] = (H["mid"] - 0.5).abs()
for k in ("r1", "r3", "s1", "s3"):
    H[f"abs_{k}"] = H[k].abs()                     # a symmetric trigger (moves either way) needs |move| terms
feats2 = ["r1", "r3", "r10", "ask1", "s1", "s3", "s10", "abs_r1", "abs_r3", "abs_s1", "abs_s3", "mid_dist", "spread", "t_open"]
X = H[feats2].clip(H[feats2].quantile(0.001), H[feats2].quantile(0.999), axis=1)
mu, sd = X.mean(), X.std()
Z = ((X - mu) / sd).values
Z = np.c_[np.ones(len(Z)), Z]
yv = H["y"].values.astype(float)
w = np.zeros(Z.shape[1])
for _ in range(25):
    p = 1 / (1 + np.exp(-Z @ w))
    g = Z.T @ (yv - p) - 1e-3 * np.r_[0, w[1:]]
    Hs = (Z * (p * (1 - p))[:, None]).T @ Z + 1e-3 * np.eye(len(w))
    step = np.linalg.solve(Hs, g)
    w += step
    if np.abs(step).max() < 1e-7:
        break
se = np.sqrt(np.diag(np.linalg.inv(Hs)))


def auc(s, y):
    o = np.argsort(s)
    r = np.empty(len(s))
    r[o] = np.arange(1, len(s) + 1)
    n1 = y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))


p = 1 / (1 + np.exp(-Z @ w))
M["entry_model"] = {"rows": len(H), "positives": int(yv.sum()),
                    "std_coef": {k: {"coef": round(float(c), 3), "z": round(float(c / s), 1)} for k, c, s in zip(["const"] + feats2, w, se)},
                    "auc_full_model": round(float(auc(p, yv)), 4),
                    "auc_single_feature": {k: round(float(auc(H[k].values, yv)), 4) for k in feats2}}
print(json.dumps(M["entry_hazard"]["by_r1_token_mid_change_1s"], default=J), json.dumps(M["entry_model"], default=J), flush=True)

# ------------------------------------------------------------------ 5. direction at entry: rising token?
to = one(f[f["role"] == "taker"]).dropna(subset=["dmid_-3", "spot_-3"])
M["direction_at_taker_entry"] = {
    "bought_token_rose_ge_1c_prior_3s": round((to["dmid_-3"] >= 0.01).mean(), 3),
    "bought_token_fell_ge_1c_prior_3s": round((to["dmid_-3"] <= -0.01).mean(), 3),
    "bought_token_rose_ge_1c_prior_1s": round((to["dmid_-1"] >= 0.01).mean(), 3),
    "spot_moved_toward_bought_side_prior_3s(>0.5bp)": round((to["spot_-3"] > 0.5).mean(), 3),
    "spot_moved_against_prior_3s(<-0.5bp)": round((to["spot_-3"] < -0.5).mean(), 3),
    "random_token_rose_ge_1c_3s": round((base["dmid_-3"] >= 0.01).mean(), 3),
    "random_token_fell_ge_1c_3s": round((base["dmid_-3"] <= -0.01).mean(), 3)}
print(M["direction_at_taker_entry"], flush=True)

# ------------------------------------------------------------------ 6. P&L of fills by what preceded them (settlement value - cost)
f["pre3"] = pd.cut(f["dmid_-3"], [-1, -0.01, 0.01, 1], labels=["fell>=1c", "flat", "rose>=1c"])
pp = f.groupby(["role", "pre3"], observed=False).agg(fills=("fill_id", "size"), cost=("cost", "sum"), pnl=("pnl_at_settlement", "sum"),
                                                     post1=("dmid_+1", "mean"), post10=("dmid_+10", "mean"))
pp["pnl_pct"] = pp["pnl"] / pp["cost"] * 100
M["pnl_by_prior_move"] = {f"{a} {b}": {k: round(float(v), 3) for k, v in r.items()} for (a, b), r in pp.iterrows()}
# markout: mid 10 s / 60 s after vs price paid (per share, cents), by role
for r in ("taker", "maker"):
    x = f[f["role"] == r]
    M[f"markout_{r}_c"] = {h: round(float(((x[f"mid_{h}"] - x["price"]) * 100).mean()), 3) for h in ("+1", "+3", "+10", "+30", "+60")}
print(M["pnl_by_prior_move"], M["markout_taker_c"], M["markout_maker_c"], flush=True)

# ------------------------------------------------------------------ 7. inferred-rule fidelity (category C: our test of the inferred rule)
# rule: token mid rose >= X c over the last k s, mid within [0.10, 0.90]  ->  "the wallet takes this token within 1 s"
fid = {}
for k in ("r1", "r3"):
    for x in (0.005, 0.01, 0.02, 0.03):
        trig = (G[k] >= x) & G["mid"].between(0.10, 0.90)
        fid[f"{k}>={x}"] = {"trigger_seconds": int(trig.sum()), "precision": round(float(G.loc[trig, "y"].mean()), 4),
                            "recall": round(float(G.loc[trig, "y"].sum() / G["y"].sum()), 4)}
M["inferred_rule_fidelity"] = fid
print(fid, flush=True)

# ------------------------------------------------------------------ 8. representative markets
mk = pd.read_csv(P / "markets.csv")
rec = mk[mk["slug"].isin(set(f["slug"]))].copy()
pick = {}
med = rec[(rec["both_sides"]) & rec["fills"].between(12, 18)]
pick["typical"] = med.iloc[(med["cost"] - rec["cost"].median()).abs().argsort().iloc[0]]["slug"]
pick["largest_gain"] = rec.sort_values("pnl").iloc[-1]["slug"]
pick["largest_loss"] = rec.sort_values("pnl").iloc[0]["slug"]
hz = rec[(rec["pair_cost"] < 0.93) & (rec["fills"].between(15, 40)) & (rec["lean_frac"] < 0.25)]
pick["two_sided_below_1"] = hz.sort_values("pnl").iloc[len(hz) // 2]["slug"] if len(hz) else None
one_side = rec[(~rec["both_sides"]) & rec["fills"].between(5, 25)]
pick["one_sided"] = one_side.sort_values("cost").iloc[len(one_side) // 2]["slug"] if len(one_side) else None
seqs = {}
for k, s in pick.items():
    if s is None:
        continue
    x = f[f["slug"] == s].sort_values(["ts", "fill_id"])
    cols = ["t_from_open", "outcome", "role", "price", "shares", "cost", "bid", "ask", "dmid_-3", "dmid_+3", "spot_-3"]
    x[cols].round(4).to_csv(OUT / f"sequence_{k}_{s}.csv", index=False)
    seqs[k] = {"slug": s, **{c: (float(v) if isinstance(v, (int, float, np.floating, np.integer)) else v)
                             for c, v in mk[mk["slug"] == s][["fills", "cost", "shares_up", "shares_down", "avg_cost_up", "avg_cost_down", "winner", "pnl"]].iloc[0].items()}}
M["representative_markets"] = seqs
json.dump(M, open(P / "metrics_micro.json", "w"), indent=1, default=J)
print("written", flush=True)
