"""Momentum-Following UP/DOWN Inventory strategy (the user's spec, 2026-10-09) tested against the wallet
dangerously-skip-permissions (0xc53375ff...) and on this repo's recorded BTC 5-minute books (data/charts).

PART A -- the wallet's own fills (09-22 -> 10-08, the markets we recorded), matched to our book at block time - 2 s:
    does the spec's taker trigger select the wallet's profitable buys, and what would the same buys earn at OUR
    execution (the ask our book shows L ms after the trigger) instead of the wallet's price?
PART B -- the spec on its own, every recorded BTC market, chronological TRAIN / VALIDATION / TEST (50 / 25 / 25 %):
    trigger    token mid rose >= theta over a 1 s or 3 s lookback, optional "last two mid changes up", optional
               exhaustion cap (3 s rise <= cap), spread <= smax, ask depth >= 5 sh, 30..295 s left; 3 s cooldown/token
    q-hat      a logistic model fitted on TRAIN only (market logit, 1/3/10 s rises, time left, spread, spot returns
               3/30 s toward the token, spot-vs-strike, order-flow imbalance) -- calibrated, never "mid + 1.5c"
    taker      marketable limit at the decision ask + 1c, filled at the ask our book shows L ms later (else missed);
               EV = q-hat - ask - fee(ask), traded when EV >= margin; held to settlement
    maker      post-only bid at the bid L ms later, gated on q-hat - bid >= margin, resting 5 s; filled when our level
               is traded through (the ask comes down to it); no fee, NO rebate; held to settlement
    portfolio  the spec's section-7 limits on TEST (bankroll 200, $1-2 orders vs the 5-share venue minimum, open
               cost 20 %, projected loss 5 %, daily stop 3 %, one market)
Thresholds are chosen on TRAIN, the configuration on VALIDATION, and TEST is reported once.

    .venv/bin/python scripts/momentum_inventory_study.py --wallet <dir with pm.pkl + outcomes.json>
"""
from __future__ import annotations

import argparse
import bisect
import collections
import glob
import itertools
import json
import math
import os
import pickle
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CH = ROOT / "data" / "charts"
FEE = 0.07
LAT = (250.0, 500.0, 1000.0)


def fee(p):
    return FEE * p * (1.0 - p)


# ---------------------------------------------------------------------------------------------- data

def outcomes(wallet_dir: Path, slugs: list[str]) -> dict[str, int]:
    """1 = UP won. The wallet's gamma outcomes, then gamma for the rest (closed + resolved 1/0 only)."""
    cache = ROOT / "data" / "replay" / "btc5m_outcomes.json"
    out = json.loads(cache.read_text()) if cache.exists() else {}
    w = json.loads((wallet_dir / "outcomes.json").read_text())
    for s, o in w.items():
        if o.get("winner") in ("Up", "Down"):
            out[s] = 1 if o["winner"] == "Up" else 0
    todo = [s for s in slugs if s not in out]
    for i in range(0, len(todo), 40):
        url = "https://gamma-api.polymarket.com/markets?closed=true&limit=100&" + "&".join("slug=" + s for s in todo[i:i + 40])
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        for m in json.load(urllib.request.urlopen(req, timeout=30)):
            px, names = json.loads(m.get("outcomePrices") or "[]"), json.loads(m.get("outcomes") or "[]")
            if m.get("closed") and px and set(px) <= {"0", "1"}:
                out[m["slug"]] = int(dict(zip(names, map(float, px))).get("Up", 0) == 1.0)
    cache.write_text(json.dumps(out))
    return out


def load_books(cache: Path) -> dict[str, dict]:
    if cache.exists():
        return pickle.load(cache.open("rb"))
    books = {}
    for f in sorted(glob.glob(str(CH / "btc-updown-5m-*.json"))):
        try:
            d = json.load(open(f))
        except (OSError, ValueError):
            continue
        P = [p for p in d.get("points") or [] if p.get("t")]
        if len(P) < 200:
            continue
        g = lambda k: np.array([np.nan if p.get(k) is None else p[k] for p in P], float)
        of = lambda k: np.array([np.nan if (p.get("of") or {}).get(k) is None else p["of"][k] for p in P], float)
        books[d["slug"]] = {"open": float(d["open_ts"]), "close": float(d["close_ts"]), "strike": d.get("strike"),
                            "t": g("t"), "ua": g("ua"), "ub": g("ub"), "da": g("da"), "db": g("db"),
                            "uad": g("uad"), "dad": g("dad"), "spot": g("spot"), "tfi": of("tfi15"), "obi": of("obi")}
    pickle.dump(books, cache.open("wb"))
    return books


def token_arrays(b: dict, side: str):
    a, bid, dep = (b["ua"], b["ub"], b["uad"]) if side == "UP" else (b["da"], b["db"], b["dad"])
    return a, bid, dep, (a + bid) / 2.0


def at(t: np.ndarray, x: np.ndarray, when: np.ndarray) -> np.ndarray:
    """x at the last point at or before ``when``."""
    i = np.searchsorted(t, when, side="right") - 1
    v = np.where(i >= 0, x[np.clip(i, 0, len(x) - 1)], np.nan)
    return v


def after(t: np.ndarray, x: np.ndarray, when: np.ndarray) -> np.ndarray:
    """x at the first point at or after ``when`` (the book an order arriving then meets)."""
    i = np.searchsorted(t, when, side="left")
    return np.where(i < len(x), x[np.clip(i, 0, len(x) - 1)], np.nan)


def features(b: dict, side: str, idx: np.ndarray | None = None, when: np.ndarray | None = None) -> dict:
    t = b["t"]
    a, bid, dep, mid = token_arrays(b, side)
    sg = 1.0 if side == "UP" else -1.0
    tt = t[idx] if idx is not None else when
    m0 = at(t, mid, tt)
    f = {"t": tt, "mid": m0, "ask": at(t, a, tt), "bid": at(t, bid, tt), "dep": at(t, dep, tt),
         "left": (b["close"] - tt) / 1000.0}
    for w in (1, 3, 10):
        f[f"r{w}"] = m0 - at(t, mid, tt - w * 1000.0)
    s0 = at(t, b["spot"], tt)
    for w in (3, 30):
        f[f"s{w}"] = sg * (s0 / at(t, b["spot"], tt - w * 1000.0) - 1.0) * 1e4
    k = b.get("strike")
    f["dist"] = sg * (np.log(s0 / k) * 1e4 if k else np.zeros_like(s0)) / np.sqrt(np.maximum(f["left"], 1.0))
    f["tfi"] = sg * at(t, b["tfi"], tt)
    f["obi"] = sg * at(t, b["obi"], tt)
    return f


XCOLS = ("lm", "r1", "r3", "r10", "left", "spread", "s3", "s30", "dist", "tfi", "obi", "lm_left")


def design(f: dict) -> np.ndarray:
    m = np.clip(f["mid"], 0.01, 0.99)
    lm = np.log(m / (1 - m))
    cols = {"lm": lm, "r1": f["r1"] * 100, "r3": f["r3"] * 100, "r10": f["r10"] * 100, "left": f["left"] / 300.0,
            "spread": (f["ask"] - f["bid"]) * 100, "s3": np.clip(f["s3"], -50, 50), "s30": np.clip(f["s30"], -100, 100),
            "dist": np.clip(f["dist"], -20, 20), "tfi": np.nan_to_num(f["tfi"]), "obi": np.nan_to_num(f["obi"]),
            "lm_left": lm * f["left"] / 300.0}
    X = np.column_stack([np.nan_to_num(cols[c]) for c in XCOLS])
    return np.column_stack([np.ones(len(X)), X])


def fit_logit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 30) -> np.ndarray:
    mu, sd = X[:, 1:].mean(0), X[:, 1:].std(0) + 1e-9
    Z = X.copy()
    Z[:, 1:] = (Z[:, 1:] - mu) / sd
    w = np.zeros(Z.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        g = Z.T @ (p - y) + l2 * np.r_[0, w[1:]]
        H = (Z * (p * (1 - p))[:, None]).T @ Z + l2 * np.diag(np.r_[0, np.ones(len(w) - 1)])
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return np.r_[w[0] - (w[1:] * mu / sd).sum(), w[1:] / sd]


def predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))


# ---------------------------------------------------------------------------------------------- events

def events(books: dict, up: dict, theta: float, win: int, confirm: bool, cap: float | None, smax: float,
           cool_ms: float = 3000.0) -> list[dict]:
    """Every trigger of the spec's taker rule, one per token per cooldown, with what an order meets at arrival."""
    out = []
    for slug, b in books.items():
        if slug not in up:
            continue
        t = b["t"]
        for side in ("UP", "DOWN"):
            a, bid, dep, mid = token_arrays(b, side)
            rise = mid - at(t, mid, t - win * 1000.0)
            left = (b["close"] - t) / 1000.0
            ok = (rise >= theta - 1e-9) & ((a - bid) <= smax + 1e-9) & (dep >= 5.0) & (left >= 30.0) & (left <= 295.0)
            if cap is not None:
                ok &= (mid - at(t, mid, t - 3000.0)) <= cap + 1e-9
            if confirm:
                d = np.diff(mid, prepend=mid[0])
                nz = np.flatnonzero(np.nan_to_num(d) != 0)
                k = np.searchsorted(nz, np.arange(len(mid)), side="right")
                last1 = np.where(k >= 1, d[nz[np.clip(k - 1, 0, len(nz) - 1)]], 0) if len(nz) else np.zeros(len(mid))
                last2 = np.where(k >= 2, d[nz[np.clip(k - 2, 0, len(nz) - 1)]], 0) if len(nz) else np.zeros(len(mid))
                ok &= (last1 > 0) & (last2 > 0)
            ok &= ~np.isnan(rise) & ~np.isnan(a)
            nxt = -1e18
            keep = []
            for i in np.flatnonzero(ok):
                if t[i] >= nxt:
                    keep.append(i)
                    nxt = t[i] + cool_ms
            if not keep:
                continue
            idx = np.array(keep)
            f = features(b, side, idx=idx)
            won = (up[slug] == 1) if side == "UP" else (up[slug] == 0)
            for L in LAT:
                f[f"ask{int(L)}"] = after(t, a, t[idx] + L)
                f[f"bid{int(L)}"] = after(t, bid, t[idx] + L)
            # maker: the bid placed 250 ms on is filled when the ask comes down to it within the next 5 s
            j0, j1 = np.searchsorted(t, t[idx] + 250.0), np.searchsorted(t, t[idx] + 5250.0)
            a9 = np.nan_to_num(a, nan=9.0)
            f["mk_fill"] = np.array([float(not math.isnan(bp) and bool((a9[x0:x1] <= bp + 1e-9).any()))
                                     for x0, x1, bp in zip(j0, j1, f["bid250"])])
            for j in range(len(idx)):
                e = {k: float(v[j]) for k, v in f.items()}
                e.update(slug=slug, side=side, open=b["open"], won=float(won))
                out.append(e)
    return out


def split(evs: list[dict], opens: list[float]) -> dict[str, list[dict]]:
    o = sorted(opens)
    c1, c2 = o[len(o) // 2], o[(3 * len(o)) // 4]
    return {"train": [e for e in evs if e["open"] < c1], "val": [e for e in evs if c1 <= e["open"] < c2],
            "test": [e for e in evs if e["open"] >= c2]}, (c1, c2)


def stats(v: list[float], mk: list[str]) -> dict:
    """Mean per share and a market-clustered standard error."""
    if len(v) < 5:
        return {"n": len(v), "mean": float("nan"), "se": float("nan"), "t": float("nan"), "sum": float(sum(v))}
    by = collections.defaultdict(list)
    for x, m in zip(v, mk):
        by[m].append(x)
    tot = np.array([sum(x) for x in by.values()])
    n = len(v)
    mean = tot.sum() / n
    se = math.sqrt(len(tot) / max(len(tot) - 1, 1) * ((tot - mean * np.array([len(x) for x in by.values()])) ** 2).sum()) / n
    return {"n": n, "mean": mean, "se": se, "t": mean / se if se > 0 else float("nan"), "sum": float(sum(v))}


def taker_pnl(evs, q, margin, L=250.0, slip=0.0):
    v, mk, ev = [], [], []
    for e, qq in zip(evs, q):
        a = e[f"ask{int(L)}"]
        if math.isnan(a) or a > e["ask"] + 0.01 + 1e-9:          # the marketable limit: missed
            continue
        a = min(0.99, a + slip)
        x = qq - a - fee(a)
        if x >= margin:
            v.append(e["won"] - a - fee(a)); mk.append(e["slug"]); ev.append(x)
    s = stats(v, mk)
    s["exp_edge"] = float(np.mean(ev)) if ev else float("nan")
    return s, v, mk


def maker_pnl(evs, q, margin):
    v, mk, placed = [], [], 0
    for e, qq in zip(evs, q):
        b = e["bid250"]
        if math.isnan(b) or qq - b < margin:
            continue
        placed += 1
        if e["mk_fill"]:
            v.append(e["won"] - b); mk.append(e["slug"])
    s = stats(v, mk)
    s["placed"], s["fill_rate"] = placed, (len(v) / placed if placed else float("nan"))
    return s


# ---------------------------------------------------------------------------------------------- part A

def part_a(books: dict, up: dict, wallet_dir: Path) -> list[str]:
    tr, _ = pickle.load((wallet_dir / "pm.pkl").open("rb"))
    rows = collections.defaultdict(list)
    for x in tr:
        if x["type"] == "TRADE" and x["slug"] in books and x["slug"] in up:
            rows[x["slug"]].append(x)
    agg = collections.defaultdict(lambda: [0.0, 0.0, 0.0])            # key -> shares, cost, payout
    ours = {int(L): [0.0, 0.0, 0.0, 0] for L in LAT}
    n_fills = 0
    for slug, xs in rows.items():
        b = books[slug]
        for side in ("UP", "DOWN"):
            ys = [x for x in xs if x["side"] == side]
            if not ys:
                continue
            tb = np.array([x["timestamp"] * 1000.0 - 2000.0 for x in ys])
            f = features(b, side, when=tb)
            a, bid, dep, mid = token_arrays(b, side)
            for j, x in enumerate(ys):
                n_fills += 1
                q, px = float(x["size"]), float(x["price"])
                cost = float(x["usdcSize"]) if x.get("usdcSize") else q * px
                won = float(x["won"])
                kind = "taker" if x["taker"] else "maker"
                trig = (max(f["r1"][j], f["r3"][j]) >= 0.01 - 1e-9 and (f["ask"][j] - f["bid"][j]) <= 0.02 + 1e-9
                        and 30.0 <= f["left"][j] <= 295.0)
                gap = px - f["ask"][j] if kind == "taker" else px - f["bid"][j]
                gk = "below" if gap < -0.005 else "above" if gap > 0.005 else "at"
                for key in (kind, f"{kind} trigger={'yes' if trig else 'no'}", f"{kind} vs book: {gk}"):
                    r = agg[key]
                    r[0] += q; r[1] += cost; r[2] += q * won
                if kind == "taker" and trig:
                    for L in LAT:
                        ak = float(after(b["t"], a, np.array([tb[j] + L]))[0])
                        if math.isnan(ak) or ak > f["ask"][j] + 0.01 + 1e-9:
                            ours[int(L)][3] += 1
                            continue
                        o = ours[int(L)]
                        o[0] += q; o[1] += q * (ak + fee(ak)); o[2] += q * won
    out = ["## Part A -- the wallet's own fills against the spec", "",
           f"{n_fills:,} wallet fills in {len(rows):,} BTC markets we recorded (09-22 -> 10-08), matched to our book at "
           "block time - 2 s (block times are whole seconds: +-1 s).", "",
           "| fills | shares | cost | edge per share | edge % of cost |", "|---|---:|---:|---:|---:|"]
    for key in ("taker", "taker trigger=yes", "taker trigger=no", "taker vs book: below", "taker vs book: at",
                "taker vs book: above", "maker", "maker trigger=yes", "maker trigger=no"):
        q, c, p = agg[key]
        if q:
            out.append(f"| {key} | {q:,.0f} | {c:,.0f} | {100 * (p - c) / q:+.2f}c | {100 * (p - c) / c:+.2f} % |")
    out += ["", "The same trigger-accepted taker buys at OUR execution (the ask our book shows L ms after the wallet's "
            "match time, limit = that ask + 1c, fee added):", "", "| latency | shares filled | missed buys | edge per share | edge % of cost |",
            "|---|---:|---:|---:|---:|"]
    for L, (q, c, p, miss) in ours.items():
        if q:
            out.append(f"| {L} ms | {q:,.0f} | {miss:,} | {100 * (p - c) / q:+.2f}c | {100 * (p - c) / c:+.2f} % |")
    return out


# ---------------------------------------------------------------------------------------------- part B

def part_b(books: dict, up: dict) -> tuple[list[str], dict]:
    opens = [b["open"] for s, b in books.items() if s in up]
    grid = list(itertools.product((0.01, 0.02, 0.03), (1, 3), (False, True), (None, 0.05), (0.01, 0.02)))
    margins = (0.005, 0.01, 0.02, 0.03)
    res = []
    cuts = None
    t0 = time.time()
    for theta, win, conf, cap, smax in grid:
        evs = events(books, up, theta, win, conf, cap, smax)
        sp, cuts = split(evs, opens)
        if len(sp["train"]) < 500:
            continue
        Xtr = design({k: np.array([e[k] for e in sp["train"]]) for k in sp["train"][0] if k not in ("slug", "side")})
        ytr = np.array([e["won"] for e in sp["train"]])
        w = fit_logit(Xtr, ytr)
        q = {k: predict(w, design({c: np.array([e[c] for e in v]) for c in v[0] if c not in ("slug", "side")})) if v else np.array([])
             for k, v in sp.items()}
        base = {k: stats([e["won"] - e["ask250"] - fee(e["ask250"]) for e in v if not math.isnan(e["ask250"])
                          and e["ask250"] <= e["ask"] + 0.01 + 1e-9],
                         [e["slug"] for e in v if not math.isnan(e["ask250"]) and e["ask250"] <= e["ask"] + 0.01 + 1e-9])
                for k, v in sp.items()}
        for mg in margins:
            r = {"theta": theta, "win": win, "confirm": conf, "cap": cap, "smax": smax, "margin": mg, "w": w.tolist(),
                 "events": {k: len(v) for k, v in sp.items()}, "all_triggers": base}
            for k, v in sp.items():
                r[k] = taker_pnl(v, q[k], mg)[0]
                r["mk_" + k] = maker_pnl(v, q[k], mg)
            if r is not None:
                for L in LAT[1:]:
                    r[f"test_L{int(L)}"] = taker_pnl(sp["test"], q["test"], mg, L=L)[0]
                r["test_slip1c"] = taker_pnl(sp["test"], q["test"], mg, slip=0.01)[0]
                # calibration on validation
                qv, yv = q["val"], np.array([e["won"] for e in sp["val"]])
                r["val_brier_model"] = float(np.mean((qv - yv) ** 2)) if len(yv) else None
                r["val_brier_mid"] = float(np.mean((np.array([e["mid"] for e in sp["val"]]) - yv) ** 2)) if len(yv) else None
            res.append(r)
        print(f"  config theta {theta} win {win} confirm {conf} cap {cap} smax {smax}: {len(evs)} events "
              f"({time.time() - t0:.0f} s)", flush=True)
    return res, cuts


def pick(res, key, n_min=150):
    pre = "mk_" if key.startswith("mk_") else ""
    ok = [r for r in res if r[pre + "train"]["n"] >= n_min and r[pre + "val"]["n"] >= n_min // 2]
    return max(ok, key=lambda r: (r[key]["mean"] if not math.isnan(r[key]["mean"]) else -9)) if ok else None


def portfolio(books, up, cfg, cuts, order_usd=2.0, min_shares=5.0, bank=200.0, strict=True):
    """The spec's section-7 limits on TEST for the chosen taker configuration."""
    evs = [e for e in events(books, up, cfg["theta"], cfg["win"], cfg["confirm"], cfg["cap"], cfg["smax"]) if e["open"] >= cuts[1]]
    w = np.array(cfg["w"])
    q = predict(w, design({c: np.array([e[c] for e in evs]) for c in evs[0] if c not in ("slug", "side")}))
    order = np.argsort([e["t"] for e in evs])
    eq, peak, mdd, day, day_pnl, n, infeasible = bank, bank, 0.0, None, 0.0, 0, 0
    inv = {}                                        # slug -> [U, D, C]
    pnl_by_mkt = {}
    for j in order:
        e, qq = evs[j], q[j]
        a = e["ask250"]
        if math.isnan(a) or a > e["ask"] + 0.01 + 1e-9 or qq - a - fee(a) < cfg["margin"]:
            continue
        # settle markets that closed before this event
        for s in [s for s in inv if books[s]["close"] <= e["t"]]:
            U, D, C = inv.pop(s)
            p = (U if up[s] == 1 else D) - C
            eq += p; pnl_by_mkt[s] = p
            d = time.strftime("%F", time.gmtime(books[s]["close"] / 1000))
            day_pnl = day_pnl + p if d == day else p
            day = d
            peak = max(peak, eq); mdd = max(mdd, peak - eq)
        today = time.strftime("%F", time.gmtime(e["t"] / 1000))
        if today == day and day_pnl <= -0.03 * bank:
            continue
        shares = math.floor(order_usd / (a + fee(a)) * 100) / 100
        if shares < min_shares:
            if strict:
                infeasible += 1
                continue
            shares = min_shares
        cost = shares * (a + fee(a))
        U, D, C = inv.get(e["slug"], [0.0, 0.0, 0.0])
        U2, D2, C2 = (U + shares, D, C + cost) if e["side"] == "UP" else (U, D + shares, C + cost)
        if sum(v[2] for v in inv.values()) - C + C2 > 0.20 * bank or min(U2, D2) - C2 < -0.05 * bank:
            continue
        if any(s != e["slug"] for s in inv):            # one market at a time
            continue
        inv[e["slug"]] = [U2, D2, C2]
        n += 1
    for s, (U, D, C) in inv.items():
        p = (U if up[s] == 1 else D) - C
        eq += p; pnl_by_mkt[s] = p
    return {"orders": n, "infeasible_5sh_min": infeasible, "markets": len(pnl_by_mkt), "pnl": eq - bank, "max_dd": mdd,
            "pos_markets": sum(p > 0 for p in pnl_by_mkt.values())}


def fmt(s):
    if not s or s.get("n", 0) < 5 or math.isnan(s.get("mean", float("nan"))):
        return f"n {s.get('n', 0) if s else 0}"
    return f"{100 * s['mean']:+.2f}c ±{100 * s['se']:.2f} (n {s['n']:,}, t {s['t']:+.1f})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", type=Path, required=True)
    ap.add_argument("--cache", type=Path, default=ROOT / "data" / "replay" / "btc5m_books.pkl")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "momentum_inventory_study_2026-10-09")
    a = ap.parse_args()
    books = load_books(a.cache)
    up = outcomes(a.wallet, sorted(books))
    books = {s: b for s, b in books.items() if s in up}
    print(f"{len(books)} recorded BTC markets with an outcome", flush=True)
    lines = [f"# Momentum-following inventory strategy: test (2026-10-09)", "",
             f"{len(books):,} recorded BTC 5-minute markets ({time.strftime('%m-%d %H:%M', time.gmtime(min(b['open'] for b in books.values()) / 1000))} -> "
             f"{time.strftime('%m-%d %H:%M', time.gmtime(max(b['open'] for b in books.values()) / 1000))} UTC), outcomes from gamma "
             "(closed, resolved 1/0). Edge per share = payout - price - taker fee; maker fills pay no fee and get NO rebate.", ""]
    lines += part_a(books, up, a.wallet)
    print("part A done", flush=True)
    res, cuts = part_b(books, up)
    tc = lambda c: time.strftime("%m-%d %H:%M", time.gmtime(c / 1000))
    lines += ["", "## Part B -- the spec on its own (recorded books, chronological split)", "",
              f"TRAIN < {tc(cuts[0])} <= VALIDATION < {tc(cuts[1])} <= TEST (UTC). {len(res)} configurations "
              "(trigger 1/2/3c over 1 s or 3 s, confirmation on/off, exhaustion cap off/5c, spread <= 1c/2c, margin 0.5/1/2/3c).", ""]
    first = res[0]
    lines += ["Every trigger, no model (bought at the ask 250 ms later, held): "
              f"train {fmt(first['all_triggers']['train'])}, validation {fmt(first['all_triggers']['val'])}, "
              f"test {fmt(first['all_triggers']['test'])} (trigger 1c / 1 s, spread <= 1c).", ""]
    tr_best = pick(res, "train")
    va_best = pick(res, "val")
    for name, r in (("best on TRAIN", tr_best), ("chosen on VALIDATION", va_best)):
        if r is None:
            continue
        lines += [f"**Taker, {name}:** trigger {r['theta'] * 100:g}c over {r['win']} s, confirm {r['confirm']}, cap {r['cap']}, "
                  f"spread <= {r['smax'] * 100:g}c, margin {r['margin'] * 100:g}c", "",
                  "| split | realised per share | model's expected edge |", "|---|---|---:|"]
        for k in ("train", "val", "test"):
            lines.append(f"| {k} | {fmt(r[k])} | {100 * r[k]['exp_edge']:+.2f}c |")
        lines += ["", f"TEST robustness: latency 500 ms {fmt(r['test_L500'])}; 1 s {fmt(r['test_L1000'])}; "
                  f"+1c slippage {fmt(r['test_slip1c'])}. Validation Brier: model {r['val_brier_model']:.4f} vs market mid "
                  f"{r['val_brier_mid']:.4f}.", ""]
    pos_val = [r for r in res if r["val"]["n"] >= 75 and r["val"]["mean"] > 0]
    pos_both = [r for r in pos_val if r["test"]["n"] >= 30 and r["test"]["mean"] > 0]
    lines += [f"Configurations positive on VALIDATION: {len(pos_val)} of {len(res)}; of those, positive on TEST too: {len(pos_both)}.", ""]
    mk_best = pick(res, "mk_val", n_min=50)
    if mk_best:
        lines += [f"**Maker bids, chosen on VALIDATION** (trigger {mk_best['theta'] * 100:g}c / {mk_best['win']} s, margin "
                  f"{mk_best['margin'] * 100:g}c): fill rate {mk_best['mk_test']['fill_rate']:.0%} of {mk_best['mk_test']['placed']:,} bids; "
                  f"train {fmt(mk_best['mk_train'])}, validation {fmt(mk_best['mk_val'])}, test {fmt(mk_best['mk_test'])}.", ""]
    if va_best:
        p1 = portfolio(books, up, va_best, cuts, strict=True)
        p2 = portfolio(books, up, va_best, cuts, strict=False)
        lines += ["**Section-7 portfolio on TEST** (bankroll 200, open cost <= 40, projected loss <= 10, daily stop 6, one market):", "",
                  f"- $2 orders as written: {p1['orders']} orders, {p1['infeasible_5sh_min']} signals impossible (5 shares cost more than $2 above ~0.40), "
                  f"P&L {p1['pnl']:+.2f}, max drawdown {p1['max_dd']:.2f}, {p1['pos_markets']}/{p1['markets']} markets positive",
                  f"- 5-share minimum orders: {p2['orders']} orders, P&L {p2['pnl']:+.2f}, max drawdown {p2['max_dd']:.2f}, "
                  f"{p2['pos_markets']}/{p2['markets']} markets positive", ""]
    a.out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    json.dump(res, a.out.with_suffix(".json").open("w"), default=lambda o: None)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
