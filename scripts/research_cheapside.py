"""CHEAPSIDE research: is the cheaper token of a 5-minute market ever worth buying and
holding to settlement?

    ionice -c3 nice -n 19 python scripts/research_cheapside.py [--draws 3] [--edge 0.05] \
        > results/cheapside_backtest.md

RESEARCH ONLY, read-only on the archive. Reads the research feature cache
(scripts/research_repriced_momentum.py cache: every archive point, ~220 ms apart, with
both tokens' touch and depth, the composite spot, the TwoScaleVol sigma and the
analytic TWAP fair value -- all causal), the strike per window (data/charts_index.json)
and each window's venue outcome. Writes data/replay/cheapside_model.json (the fitted
model the paper engine loads) and the report to stdout.

Rules simulated (strategy/cheapside.py mirrors them):
  candidate    at every point where the cheap side's book changed: cheap = the side with
               the lower ask, 0.05 <= ask <= 0.35, spread <= 3c, ask depth >= size
  model        P(cheap side settles as the winner) from a logistic regression fitted on
               TRAIN windows only, Platt-recalibrated on VALIDATION; inputs are the ask,
               time left, the analytic fair value (spot distance from the strike vs the
               settling TWAP's remaining variance), the spot's 250 ms / 500 ms / 1 s / 3 s
               returns in sigma (signed to the cheap side), their sign consistency and
               acceleration, realised vol, spot order flow, spread and touch depth
  support      the 1 s spot return points the cheap side's way and 2 of 3 short horizons agree
  edge         model_p - limit - taker fee(limit) - 0.5c slippage >= 0.05   (limit = ask + 1c)
  order        FOK, 10 shares (<= 3.5 USDC), 1 position per market, 3 attempts >= 2 s apart
  exit         settlement only: +1 or 0 per share, entry fee only
The archive cannot resolve 50/100 ms returns (points are ~220 ms apart); the live engine
reads those from the fast feed, this study uses 250 ms as the shortest horizon.
Look-ahead: every input at point i is stamped <= t_i; the outcome is the label only;
thresholds are fixed a priori, nothing is chosen on test or forward.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.backtest.spot_shock_replay import SETTLE_GUARD_MS, Execution, load_asset, windows  # noqa: E402

CACHE = ROOT / "data/replay/rm_research_cache"
INDEX = ROOT / "data/charts_index.json"
MODEL_OUT = ROOT / "data/replay/cheapside_model.json"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
PERIOD = {"train": "train", "validation": "validation", "unseen_1": "test", "unseen_2": "test", "unseen_3": "forward"}
PERIODS = ("train", "validation", "test", "forward")

MIN_ASK, MAX_ASK = 0.05, 0.35
MAX_SPREAD = 0.03
SIZE = 10.0
MAX_MARKET_USDC = 5.0
LIMIT_OFFSET = 0.01
SLIPPAGE = 0.005
MIN_LEFT, MAX_LEFT = 15.0, 295.0
ATTEMPTS, RETRY_MS = 3, 2000.0
MAX_QUOTE_AGE_MS = 1000.0
PRICE_BUCKETS = ((0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.25), (0.25, 0.30), (0.30, 0.35))
LEFT_BUCKETS = ((0, 60), (60, 120), (120, 180), (180, 240), (240, 300))
MOM_BUCKETS = (("against (z1 < -0.5)", -1e9, -0.5), ("flat", -0.5, 0.5), ("with (0.5-2)", 0.5, 2.0), ("strong with (> 2)", 2.0, 1e9))
FEATURES = ("logit_ask", "logit_fair", "z250", "z500", "z1000", "z3000", "consist", "accel", "log_sigma",
            "sqrt_left", "dist", "tfi", "obi", "spread_c", "log_depth")


def logit(p):
    p = np.clip(p, 0.005, 0.995)
    return np.log(p / (1.0 - p))


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def ret(t, lp, lag_ms):
    j = np.searchsorted(t, t - lag_ms, side="right") - 1
    ok = (j >= 0) & (t - t[np.maximum(j, 0)] <= lag_ms + 1500.0)
    return np.where(ok, lp - lp[np.maximum(j, 0)], np.nan)


# ─────────────────────────────── candidates ───────────────────────────────

def candidates(w, strike: float) -> dict | None:
    """Every point of one window where the cheap side's book just changed and its ask
    is in the band; features are causal (stamped <= t)."""
    ua, da = w.side["UP"]["ask"], w.side["DOWN"]["ask"]
    both = ~np.isnan(ua) & ~np.isnan(da)
    if not both.any():
        return None
    up_cheap = ua < da
    ask = np.where(up_cheap, ua, da)
    bid = np.where(up_cheap, w.side["UP"]["bid"], w.side["DOWN"]["bid"])
    adep = np.where(up_cheap, w.side["UP"]["adep"], w.side["DOWN"]["adep"])
    band = both & (ua != da) & (ask >= MIN_ASK - 1e-9) & (ask <= MAX_ASK + 1e-9) & (w.left >= MIN_LEFT) & (w.left <= MAX_LEFT)
    # "fresh book update": the cheap side's touch or its depth differs from the previous point
    prev_ask = np.r_[np.nan, ask[:-1]]
    prev_bid = np.r_[np.nan, bid[:-1]]
    prev_dep = np.r_[np.nan, adep[:-1]]
    changed = (ask != prev_ask) | (bid != prev_bid) | (adep != prev_dep)
    idx = np.nonzero(band & changed)[0]
    if not len(idx):
        return None
    d = np.where(up_cheap, 1.0, -1.0)
    sig = np.maximum(w.sigma, 1e-7)
    z = {}
    for h in (250.0, 500.0, 1000.0, 3000.0):
        z[int(h)] = ret(w.t, w.lp, h) / (sig * math.sqrt(h / 1000.0)) * d
    consist = sum((z[h] > 0).astype(float) for h in (250, 500, 1000))
    accel = z[500] - z[1000] / math.sqrt(2.0)                  # the last 500 ms faster than the second's average
    fair_up = w.extra["p"]                                      # analytic P(up): strike distance vs remaining variance
    fair = np.where(up_cheap, fair_up, 1.0 - fair_up)
    dist = np.log(np.exp(w.lp) / strike) / (sig * np.sqrt(np.maximum(w.left, 1.0))) * d if strike > 0 else np.zeros(len(w))
    spread = ask - bid
    feats = {
        "logit_ask": logit(ask), "logit_fair": logit(fair), "z250": z[250], "z500": z[500], "z1000": z[1000],
        "z3000": z[3000], "consist": consist, "accel": accel, "log_sigma": np.log(sig * 1e4),
        "sqrt_left": np.sqrt(w.left / 300.0), "dist": np.clip(dist, -6, 6),
        "tfi": w.extra["tfi"] * d, "obi": w.extra["obi"] * d, "spread_c": spread * 100.0,
        "log_depth": np.log1p(np.nan_to_num(adep, nan=0.0)),
    }
    X = np.column_stack([np.nan_to_num(feats[k][idx], nan=0.0) for k in FEATURES])
    won = None if w.won_up is None else np.where(up_cheap[idx], w.won_up, not w.won_up).astype(float)
    return {"idx": idx, "X": X, "ask": ask[idx], "bid": bid[idx], "adep": np.nan_to_num(adep[idx], nan=0.0),
            "up_cheap": up_cheap[idx], "left": w.left[idx], "t": w.t[idx], "z1000": z[1000][idx],
            "consist": consist[idx], "support": (z[1000][idx] > 0) & (consist[idx] >= 2), "won": won}


# ─────────────────────────────── the model ───────────────────────────────

def fit_logistic(X, y, l2=1.0, iters=30):
    """L2-regularised logistic regression by IRLS on standardised features."""
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = np.column_stack([np.ones(len(X)), (X - mu) / sd])
    beta = np.zeros(Z.shape[1])
    R = np.eye(Z.shape[1]) * l2
    R[0, 0] = 0.0
    for _ in range(iters):
        p = sigmoid(Z @ beta)
        W = p * (1 - p) + 1e-9
        g = Z.T @ (y - p) - R @ beta
        H = (Z * W[:, None]).T @ Z + R
        step = np.linalg.solve(H, g)
        beta += step
        if np.abs(step).max() < 1e-7:
            break
    return {"beta": beta.tolist(), "mu": mu.tolist(), "sd": sd.tolist()}


def predict_raw(model, X):
    Z = np.column_stack([np.ones(len(X)), (X - np.array(model["mu"])) / np.array(model["sd"])])
    return Z @ np.array(model["beta"])


def platt(raw, y):
    """a + b * logit, fitted by IRLS (a 2-parameter recalibration)."""
    m = fit_logistic(raw[:, None], y, l2=0.0)
    b = m["beta"][1] / m["sd"][0]
    a = m["beta"][0] - b * m["mu"][0]
    return {"a": float(a), "b": float(b)}


def predict(model, X):
    r = predict_raw(model, X)
    return sigmoid(model["platt"]["a"] + model["platt"]["b"] * r)


def brier(p, y):
    return float(np.mean((p - y) ** 2))


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


# ─────────────────────────────── simulation ───────────────────────────────

def simulate(w, c, p_model, ex: Execution, min_edge: float):
    """Time-ordered pass over one window's candidates: one position, FOK entries."""
    fee_rate = w.rate
    out = {"eligible": 0, "orders": [], "supported": 0}
    attempts, last_send = 0, -1e18
    for k in range(len(c["idx"])):
        ask, left, t = float(c["ask"][k]), float(c["left"][k]), float(c["t"][k])
        limit = round(ask + LIMIT_OFFSET, 2)
        if c["ask"][k] - c["bid"][k] > MAX_SPREAD + 1e-9 or c["adep"][k] < SIZE:
            continue
        if not c["support"][k]:
            continue
        out["supported"] += 1
        fee = fee_rate * limit * (1.0 - limit)
        edge = float(p_model[k]) - limit - fee - SLIPPAGE
        if edge < min_edge:
            continue
        out["eligible"] += 1
        if attempts >= ATTEMPTS or t - last_send < RETRY_MS:
            continue
        side = "UP" if c["up_cheap"][k] else "DOWN"
        sv = w.side[side]
        # quote age: the update is fresh by construction; add the book latency draw
        pre, half = ex.latency()
        if half * 2.0 > MAX_QUOTE_AGE_MS:
            continue
        r = ex.buy(w, sv, t, limit)
        attempts += 1
        last_send = t
        order = {"t": t, "left": left, "side": side, "ask": ask, "limit": limit, "p": float(p_model[k]),
                 "edge": edge, "z1000": float(c["z1000"][k]), "filled": r["filled"], "reason": r["reason"],
                 "arrival_ask": r["arrival_ask"], "entry": None, "gross": 0.0, "fee": 0.0, "net": 0.0, "won": None}
        if r["filled"]:
            entry = r["entry"]
            won = None if w.won_up is None else (w.won_up == (side == "UP"))
            fee_paid = fee_rate * entry * (1.0 - entry) * SIZE
            gross = ((1.0 if won else 0.0) - entry) * SIZE if won is not None else 0.0
            order.update(entry=entry, won=won, gross=gross, fee=fee_paid, net=gross - fee_paid,
                         t_settle=w.close, cost=entry * SIZE)
            out["orders"].append(order)
            break                                   # one position per market
        out["orders"].append(order)
    return out


def max_drawdown(nets):
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def max_exposure(fills):
    """Largest simultaneous cost of open (unsettled) positions."""
    ev = sorted([(f["t"], f["cost"]) for f in fills] + [(f["t_settle"], -f["cost"]) for f in fills])
    cur = best = 0.0
    for _, dx in ev:
        cur += dx
        best = max(best, cur)
    return best


# ─────────────────────────────── reporting ───────────────────────────────

def table(rows, cols):
    print("| " + " | ".join(cols) + " |")
    print("|" + "|".join("---:" if i else "---" for i in range(len(cols))) + "|")
    for r in rows:
        print("| " + " | ".join(str(x) for x in r) + " |")
    print()


def calib_rows(p, y, price, label):
    rows = []
    for lo, hi in PRICE_BUCKETS:
        m = (price >= lo - 1e-9) & (price < hi)
        n = int(m.sum())
        if n == 0:
            rows.append((f"${lo:.2f}–{hi:.2f}", 0, "—", "—", "—", "—"))
            continue
        rows.append((f"${lo:.2f}–{hi:.2f}", n, f"{price[m].mean():.3f}", f"{p[m].mean():.3f}", f"{y[m].mean():.3f}",
                     f"{(p[m].mean() - y[m].mean()) * 100:+.1f}c"))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=3)
    ap.add_argument("--edge", type=float, default=0.05)
    ap.add_argument("--assets", default=",".join(ASSETS))
    args = ap.parse_args()
    strikes = {s: float(r.get("strike") or 0.0) for s, r in json.loads(INDEX.read_text())["rows"].items()}

    cands = defaultdict(list)                      # period -> list of (asset, window, cand)
    n_markets = defaultdict(int)
    for asset in [a.strip().upper() for a in args.assets.split(",") if a.strip()]:
        try:
            d, meta = load_asset(CACHE, asset)
        except FileNotFoundError:
            continue
        span = {str(row[0]): (int(row[6]), int(row[7])) for row in meta}
        for w in windows(d, meta):
            s, e = span[w.slug]                     # the extra per-point columns the Window does not carry
            w.extra = {"p": d["p"][s:e], "tfi": np.nan_to_num(d["tfi"][s:e]), "obi": np.nan_to_num(d["obi"][s:e])}
            per = PERIOD.get(w.split)
            if per is None or w.won_up is None:
                continue
            n_markets[per] += 1
            c = candidates(w, strikes.get(w.slug, 0.0))
            if c is not None:
                cands[per].append((asset, w, c))
        del d
        print(f"… {asset}", file=sys.stderr)

    # ── fit on TRAIN, recalibrate on VALIDATION
    def stack(per, step=1):
        Xs, ys, asks = [], [], []
        for _, _, c in cands[per]:
            Xs.append(c["X"][::step]); ys.append(c["won"][::step]); asks.append(c["ask"][::step])
        return np.vstack(Xs), np.concatenate(ys), np.concatenate(asks)

    Xtr, ytr, _ = stack("train", step=4)          # ~1 s apart: less duplication of the same book state
    model = fit_logistic(Xtr, ytr, l2=2.0)
    Xva, yva, ava = stack("validation")
    model["platt"] = platt(predict_raw(model, Xva), yva)
    model["features"] = list(FEATURES)
    model["rules"] = {"min_ask": MIN_ASK, "max_ask": MAX_ASK, "max_spread": MAX_SPREAD, "size": SIZE,
                      "limit_offset": LIMIT_OFFSET, "slippage": SLIPPAGE, "min_edge": args.edge,
                      "min_left": MIN_LEFT, "max_left": MAX_LEFT}
    MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    MODEL_OUT.write_text(json.dumps(model, indent=1))

    print("# CHEAPSIDE backtest: buying the cheap token and holding to settlement\n")
    print(f"Archive windows with a venue outcome: " + ", ".join(f"{p} {n_markets[p]:,}" for p in PERIODS) + ". "
          f"Candidates are book updates with the cheaper ask in ${MIN_ASK:.2f}–{MAX_ASK:.2f}. "
          f"Model fitted on train, Platt-recalibrated on validation; test and forward untouched.\n")
    print("## 1. Does the model know more than the price? (all candidates, every period)\n")
    print("Brier / log loss of the model's P(cheap side wins) against the market's own price (the ask "
          "is the market's probability). Lower is better; a model that cannot beat the ask has no edge to sell.\n")
    rows = []
    per_p = {}
    for per in PERIODS:
        if not cands[per]:
            continue
        X, y, a = stack(per)
        p = predict(model, X)
        per_p[per] = (p, y, a)
        rows.append((per, f"{len(y):,}", f"{y.mean():.3f}", f"{a.mean():.3f}", f"{p.mean():.3f}",
                     f"{brier(a, y):.4f}", f"{brier(p, y):.4f}", f"{logloss(a, y):.4f}", f"{logloss(p, y):.4f}"))
    table(rows, ["period", "candidates", "actual win rate", "mean ask", "mean model p", "Brier ask", "Brier model",
                 "logloss ask", "logloss model"])

    print("## 2. Calibration by price bucket (all candidates)\n")
    for per in PERIODS:
        if per not in per_p:
            continue
        p, y, a = per_p[per]
        print(f"**{per}**\n")
        table(calib_rows(p, y, a, per), ["ask bucket", "n", "mean ask", "mean model p", "actual win rate", "model − actual"])

    coef = sorted(zip(FEATURES, model["beta"][1:]), key=lambda x: -abs(x[1]))
    print("Model coefficients (standardised inputs, largest first): " +
          ", ".join(f"{k} {v:+.2f}" for k, v in coef) + f"; Platt a {model['platt']['a']:+.2f} b {model['platt']['b']:.2f}.\n")

    # ── simulate the strategy
    print(f"## 3. The strategy, realistic execution (FOK 10 shares at ask + 1c, edge >= {args.edge:.2f}, hold to settlement)\n")
    summary = {}
    detail = {}
    for per in PERIODS:
        stats = defaultdict(float)
        fills_all, orders_all = [], []
        for draw in range(args.draws):
            ex = Execution(seed=draw)
            fills, orders = [], []
            cand_n = supp = elig = 0
            for asset, w, c in sorted(cands[per], key=lambda x: x[1].t[0]):
                p = predict(model, c["X"])
                r = simulate(w, c, p, ex, args.edge)
                cand_n += len(c["idx"]); supp += r["supported"]; elig += r["eligible"]
                for o in r["orders"]:
                    o["asset"] = asset
                    orders.append(o)
                    if o["filled"]:
                        fills.append(o)
            fills.sort(key=lambda o: o["t_settle"])
            stats["cand"] += cand_n; stats["supp"] += supp; stats["elig"] += elig
            stats["orders"] += len(orders); stats["fills"] += len(fills)
            stats["gross"] += sum(f["gross"] for f in fills); stats["fee"] += sum(f["fee"] for f in fills)
            stats["net"] += sum(f["net"] for f in fills); stats["wins"] += sum(1 for f in fills if f["won"])
            stats["dd"] += max_drawdown([f["net"] for f in fills]); stats["expo"] += max_exposure(fills)
            fills_all += fills; orders_all += orders
        n = args.draws
        for k in list(stats):
            stats[k] /= n
        summary[per] = stats
        detail[per] = (fills_all, orders_all)

    rows = []
    for per in PERIODS:
        s = summary[per]
        f = s["fills"]
        rows.append((per, f"{n_markets[per]:,}", f"{s['cand']:,.0f}", f"{s['supp']:,.0f}", f"{s['elig']:,.0f}",
                     f"{s['orders']:.0f}", f"{f:.0f}", f"{100 * f / s['orders']:.0f}%" if s["orders"] else "—",
                     f"{s['gross']:+.2f}", f"{s['fee']:.2f}", f"**{s['net']:+.2f}**",
                     f"{s['net'] / f:+.3f}" if f else "—", f"{100 * s['wins'] / f:.0f}%" if f else "—",
                     f"{s['dd']:.2f}", f"{s['expo']:.2f}"))
    table(rows, ["period", "markets", "candidates", "+support", "eligible", "orders", "fills", "fill rate", "gross $",
                 "fees $", "net $", "net/trade $", "win rate", "max DD $", "max exposure $"])
    print(f"Per-draw averages over {args.draws} latency draws. Candidates = cheap-side book updates in the band; "
          "+support = of those, the spot points the cheap side's way; eligible = also >= the edge bar; orders = "
          "sent under the 1-position / 3-attempt / 2 s budget.\n")

    for per in PERIODS:
        fills, orders = detail[per]
        if not fills:
            continue
        print(f"### {per}: the fills, by regime (all {args.draws} draws pooled; net $ per 10-share trade)\n")
        e = np.array([f["entry"] for f in fills]); pm = np.array([f["p"] for f in fills])
        y = np.array([1.0 if f["won"] else 0.0 for f in fills]); net = np.array([f["net"] for f in fills])
        print(f"Entry price mean {e.mean():.3f}, model p mean {pm.mean():.3f}, actual win rate {y.mean():.3f}, "
              f"net/trade {net.mean():+.3f} (t = {net.mean() / (net.std() / math.sqrt(len(net)) + 1e-9):.1f}, n = {len(net)}).\n")
        print("**by entry price**\n")
        rows = []
        for lo, hi in PRICE_BUCKETS:
            m = (e >= lo - 1e-9) & (e < hi)
            if m.sum():
                rows.append((f"${lo:.2f}–{hi:.2f}", int(m.sum()), f"{pm[m].mean():.3f}", f"{y[m].mean():.3f}", f"{net[m].sum():+.2f}", f"{net[m].mean():+.3f}"))
        table(rows, ["bucket", "fills", "model p", "actual", "net $", "net/trade"])
        print("**by time left at entry**\n")
        left = np.array([f["left"] for f in fills]); rows = []
        for lo, hi in LEFT_BUCKETS:
            m = (left >= lo) & (left < hi)
            if m.sum():
                rows.append((f"{lo}–{hi} s", int(m.sum()), f"{pm[m].mean():.3f}", f"{y[m].mean():.3f}", f"{net[m].sum():+.2f}", f"{net[m].mean():+.3f}"))
        table(rows, ["bucket", "fills", "model p", "actual", "net $", "net/trade"])
        print("**by momentum (1 s spot return in sigma, signed to the side)**\n")
        z = np.array([f["z1000"] for f in fills]); rows = []
        for lab, lo, hi in MOM_BUCKETS:
            m = (z >= lo) & (z < hi)
            if m.sum():
                rows.append((lab, int(m.sum()), f"{pm[m].mean():.3f}", f"{y[m].mean():.3f}", f"{net[m].sum():+.2f}", f"{net[m].mean():+.3f}"))
        table(rows, ["bucket", "fills", "model p", "actual", "net $", "net/trade"])
        print("**by asset**\n")
        rows = []
        for a in ASSETS:
            m = np.array([f["asset"] == a for f in fills])
            if m.sum():
                rows.append((a, int(m.sum()), f"{pm[m].mean():.3f}", f"{y[m].mean():.3f}", f"{net[m].sum():+.2f}", f"{net[m].mean():+.3f}"))
        table(rows, ["asset", "fills", "model p", "actual", "net $", "net/trade"])
        kills = defaultdict(int)
        for o in orders:
            if not o["filled"]:
                kills[o["reason"]] += 1
        print("Unfilled orders: " + ", ".join(f"{k} {v}" for k, v in sorted(kills.items(), key=lambda x: -x[1])) + ".\n")


if __name__ == "__main__":
    main()
