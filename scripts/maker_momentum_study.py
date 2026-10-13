"""Maker bids on a rising token (the one part of dangerously-skip-permissions' record that is not speed): its maker
fills on BTC 5m tokens that had risen in the prior 3 s earned +3.1c a share before rebates (scripts/
momentum_inventory_study.py, part A follow-up). This replays that rule on our recorded books with two fill models,
because a recorded book cannot show our place in the queue:

    CROSS   filled only when the ask comes down to our bid (a seller crossing us) -- a lower bound on fills, and the
            fills it does count are the adverse ones
    QUEUE   we join the back of the bid level (queue ahead = the level's depth when placed) and fill once the level's
            depth has fallen by queue + our size while it is still the best bid (every fall counted as a trade:
            cancels make this optimistic), or on a CROSS

Rule: when the token's mid rose >= theta over `win` s (30..285 s left, spread <= 2c, price band), post 5 shares at the
best bid (1c better when the spread is 2c); cancel after `rest` s, when the mid falls 1c below where it was posted
(if `rev`), or at 30 s left; one bid per token per 5 s; filled shares held to settlement; no fee, NO rebate.
A BASELINE posts the same bids every 10 s with no momentum condition. Chronological TRAIN / VALIDATION / TEST
(50 / 25 / 25 % of markets); the configuration is chosen on VALIDATION (QUEUE model) and TEST is reported once.

    .venv/bin/python scripts/maker_momentum_study.py
"""
from __future__ import annotations

import glob
import itertools
import json
import math
import pickle
import time
from pathlib import Path

import numpy as np

from momentum_inventory_study import at, split, stats, fmt

ROOT = Path(__file__).resolve().parents[1]
SIZE = 5.0


def load_bid_depth(cache: Path) -> dict:
    if cache.exists():
        return pickle.load(cache.open("rb"))
    out = {}
    for f in sorted(glob.glob(str(ROOT / "data" / "charts" / "btc-updown-5m-*.json"))):
        try:
            d = json.load(open(f))
        except (OSError, ValueError):
            continue
        P = [p for p in d.get("points") or [] if p.get("t")]
        if len(P) >= 200:
            out[d["slug"]] = {k: np.array([np.nan if p.get(k) is None else p[k] for p in P], float) for k in ("ubd", "dbd")}
    pickle.dump(out, cache.open("wb"))
    return out


def simulate(b, bd, side, up, theta, win, rest, rev, band, momentum=True, every_ms=10_000.0):
    t = b["t"]
    a, bid = (b["ua"], b["ub"]) if side == "UP" else (b["da"], b["db"])
    dep = bd["ubd"] if side == "UP" else bd["dbd"]
    mid = (a + bid) / 2.0
    left = (b["close"] - t) / 1000.0
    ok = ((a - bid) <= 0.02 + 1e-9) & (left >= 30.0) & (left <= 285.0) & (mid >= band[0]) & (mid <= band[1]) & ~np.isnan(a) & ~np.isnan(bid)
    if momentum:
        ok &= (mid - at(t, mid, t - win * 1000.0)) >= theta - 1e-9
    won = float((up == 1) if side == "UP" else (up == 0))
    res, nxt = [], -1e18
    for i in np.flatnonzero(ok):
        if t[i] < nxt:
            continue
        nxt = t[i] + (5000.0 if momentum else every_ms)
        j0 = np.searchsorted(t, t[i] + 250.0)                 # the order lands 250 ms on
        if j0 >= len(t) or math.isnan(bid[j0]) or math.isnan(a[j0]):
            continue
        lvl = bid[j0]                                             # the recorded level whose depth we watch
        px = lvl + (0.01 if a[j0] - lvl >= 0.02 - 1e-9 else 0.0)  # 1c better on a 2c spread: alone at the front
        queue = 0.0 if px > lvl + 1e-9 else float(np.nan_to_num(dep[j0]))
        end = min(t[i] + rest * 1000.0, b["close"] - 30_000.0)
        m0, fell, cross, qfill = mid[j0], 0.0, False, False
        last_dep, away = float(np.nan_to_num(dep[j0])), False
        for k in range(j0 + 1, len(t)):
            if t[k] > end or (rev and mid[k] <= m0 - 0.01 + 1e-9):
                break
            if not math.isnan(a[k]) and a[k] <= px + 1e-9:
                cross = qfill = True
                break
            if math.isnan(bid[k]):
                continue
            if abs(bid[k] - lvl) < 1e-9:
                d = float(np.nan_to_num(dep[k]))
                if not away and d < last_dep:                     # sells into the level (cancels counted too)
                    fell += last_dep - d
                last_dep, away = d, False
                if fell >= queue + SIZE:
                    qfill = True            # filled in the QUEUE world; keep watching for the CROSS world
            else:
                away = True                 # a better bid in front (its sells are not ours) or the level gone below
        res.append({"open": b["open"], "slug": None, "px": px, "won": won, "cross": cross, "queue": qfill})
    return res


def run(books, depth, up, cfg, momentum=True):
    out = []
    for slug, b in books.items():
        if slug not in up or slug not in depth:
            continue
        for side in ("UP", "DOWN"):
            for r in simulate(b, depth[slug], side, up[slug], *cfg, momentum=momentum):
                r["slug"] = slug
                out.append(r)
    return out


def summarise(rows):
    s = {}
    for model in ("cross", "queue"):
        f = [r for r in rows if r[model]]
        s[model] = stats([r["won"] - r["px"] for r in f], [r["slug"] for r in f])
        s[model]["fill_rate"] = len(f) / len(rows) if rows else float("nan")
    s["placed"] = len(rows)
    return s


def main():
    books = pickle.load((ROOT / "data" / "replay" / "btc5m_books.pkl").open("rb"))
    up = json.loads((ROOT / "data" / "replay" / "btc5m_outcomes.json").read_text())
    depth = load_bid_depth(ROOT / "data" / "replay" / "btc5m_biddepth.pkl")
    books = {s: b for s, b in books.items() if s in up and s in depth}
    opens = [b["open"] for b in books.values()]
    grid = list(itertools.product((0.01, 0.02), (1, 3), (5, 15, 30), (False, True), ((0.05, 0.95), (0.30, 0.85))))
    res = []
    t0 = time.time()
    for cfg in grid:
        sp, cuts = split(run(books, depth, up, cfg), opens)
        res.append({"cfg": cfg, **{k: summarise(v) for k, v in sp.items()}})
        print(f"  {cfg}: {sum(len(v) for v in sp.values())} bids ({time.time() - t0:.0f} s)", flush=True)
    base, _ = split(run(books, depth, up, (0, 0, 15, True, (0.30, 0.85)), momentum=False), opens)
    base = {k: summarise(v) for k, v in base.items()}
    ok = [r for r in res if r["val"]["queue"]["n"] >= 200]
    best = max(ok, key=lambda r: r["val"]["queue"]["mean"])
    tc = lambda c: time.strftime("%m-%d %H:%M", time.gmtime(c / 1000))
    L = ["## Part C -- maker bids on a rising token (the wallet's maker side)", "",
         f"TRAIN < {tc(cuts[0])} <= VALIDATION < {tc(cuts[1])} <= TEST. {len(res)} configurations "
         "(rise 1/2c over 1/3 s, rest 5/15/30 s, cancel on a 1c reversal or not, band 0.05-0.95 or 0.30-0.85). Per share "
         "filled = payout - bid price (no fee, no rebate).", "",
         "| | split | placed | CROSS fills: rate, edge | QUEUE fills: rate, edge |", "|---|---|---:|---|---|"]
    for name, r in (("chosen on VALIDATION " + str(best["cfg"]), best), ("BASELINE every 10 s, no momentum", base)):
        for k in ("train", "val", "test"):
            s = r[k]
            L.append(f"| {name if k == 'train' else ''} | {k} | {s['placed']:,} | {s['cross']['fill_rate']:.0%}, {fmt(s['cross'])} | "
                     f"{s['queue']['fill_rate']:.0%}, {fmt(s['queue'])} |")
    pv = [r for r in ok if r["val"]["queue"]["mean"] > 0]
    pt = [r for r in pv if r["test"]["queue"]["mean"] > 0]
    pc = [r for r in res if r["test"]["cross"]["n"] >= 100 and r["test"]["cross"]["mean"] > 0]
    L += ["", f"QUEUE model: {len(pv)} of {len(ok)} configurations positive on VALIDATION, {len(pt)} of those also on TEST. "
          f"CROSS model: {len(pc)} of {len(res)} positive on TEST.", ""]
    text = "\n".join(L)
    print(text)
    out = ROOT / "results" / "momentum_inventory_study_2026-10-09.md"
    out.write_text(out.read_text() + "\n" + text + "\n")
    json.dump(res, (ROOT / "results" / "maker_momentum_study_2026-10-09.json").open("w"), default=str)


if __name__ == "__main__":
    main()
