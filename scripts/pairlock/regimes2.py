"""Item 35 (v2): baseline / A / B / C per market regime, held-out (validation + test + forward pooled), from the same
runs as replay2.py final. Regimes are DESCRIPTIVE labels of each window's own path (regimes.labels: strong / weak
trend, sideways, high / low volatility, rapid reversal, large crypto shock, noisy) plus Polymarket liquidity
(the window's median near-touch depth: bottom / top third = low / high liquidity). They slice results only.

    PAIRLOCK_OUT=<cache> python scripts/pairlock/regimes2.py > results/pairlock2_regimes.md
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import replace

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
import replay2 as P   # noqa: E402

R = P.R


class RG:
    """regimes.labels, copied: regimes.py runs its whole study at import (no __main__ guard)."""
    @staticmethod
    def labels(ws):
        return _labels(ws)


M1 = R.FN.index("mom1")


def _labels(ws):
    raw = {}
    for w in ws:
        r1, last = [], -1e18
        for k, i in enumerate(range(w.i0, w.i1)):
            row = w.F[k]
            if np.isnan(row[0]) or w.t[i] - last < 1000.0:
                continue
            last = w.t[i]
            r1.append(row[M1] * w.sigma)
        if len(r1) < 100:
            continue
        x = np.cumsum(r1)
        scale = w.sigma * math.sqrt(300.0)
        net = x[-1] / scale
        vol = math.sqrt(float(np.mean(np.square(r1))))
        hi, lo = float(np.max(x)), float(np.min(x))
        rev = (hi / scale >= 1.0 and (hi - x[-1]) / scale >= 1.0) or (-lo / scale >= 1.0 and (x[-1] - lo) / scale >= 1.0)
        x10 = x[::10]
        d10 = np.diff(x10)
        flips = int(np.sum(np.sign(d10[1:]) != np.sign(d10[:-1])))
        shock = float(np.max(np.abs(np.array(r1) / w.sigma))) >= 5.0
        raw[w.slug] = (net, vol, rev, flips, shock)
    vmed = statistics.median(v[1] for v in raw.values())
    f75 = float(np.percentile([v[3] for v in raw.values()], 75))
    out = defaultdict(set)
    for slug, (net, vol, rev, flips, shock) in raw.items():
        a = abs(net)
        out["strong trend" if a >= 1.5 else "weak trend" if a >= 0.5 else "sideways"].add(slug)
        if vol >= 1.5 * vmed:
            out["high volatility"].add(slug)
        elif vol <= 0.67 * vmed:
            out["low volatility"].add(slug)
        if rev:
            out["rapid reversal"].add(slug)
        if shock:
            out["large crypto shock"].add(slug)
        if flips >= f75 and a < 0.5:
            out["noisy"].add(slug)
    return out

HELD = ("validation", "test", "forward")


def main():
    fin = json.load(open(f"{P.OUT}/final2.json"))
    ws = R.load_cache()
    model = P.pl.ProbModel.load(P.MODEL_PATH)
    sp = R.split(ws)
    held = [w for s in HELD for w in sp[s]]
    labels = RG.labels(held)
    depth = {w.slug: float(np.nanmedian(np.nan_to_num(w.uad[w.i0:w.i1]) + np.nan_to_num(w.ubd[w.i0:w.i1]))) for w in held}
    lo, hi = np.percentile(list(depth.values()), [33.3, 66.7])
    for slug, d in depth.items():
        if d <= lo:
            labels["low liquidity"].add(slug)
        elif d >= hi:
            labels["high liquidity"].add(slug)
    runs = {"baseline": ("A", {"hedge_mode": "none", "entry_edge": -1.0, "band_lo": fin["entry"]["band_lo"],
                                "band_hi": fin["entry"]["band_hi"]}),
            "A": ("A", {**fin["entry"], "hedge_mode": "none"}), "B": ("B", fin["b"]), "C": ("C", fin["c"])}
    per = {}
    for name, (strat, kw) in runs.items():
        prm0, lim = P.base_params()
        prm = replace(prm0, **kw)
        recs = []
        for s in HELD:
            recs += P.run_split(sp[s], strat, prm, lim, model)
        per[name] = {r["slug"]: r for r in recs if r["traded"]}
    order = ("strong trend", "weak trend", "sideways", "high volatility", "low volatility", "rapid reversal",
             "large crypto shock", "noisy", "low liquidity", "high liquidity")
    print("# Regimes (v2): $ per traded market, held-out (validation + test + forward), +- 1 standard error (n)\n")
    print("| regime | windows | " + " | ".join(runs) + " |")
    print("|---|---:|" + "---:|" * len(runs))
    for lab in order:
        slugs = labels.get(lab, set())
        cells = []
        for name in runs:
            v = [per[name][s]["total"] for s in slugs if s in per[name]]
            if len(v) >= 2:
                cells.append(f"{statistics.mean(v):+.3f} +- {statistics.pstdev(v) / math.sqrt(len(v)):.3f} ({len(v)})")
            elif v:
                cells.append(f"{v[0]:+.3f} (1)")
            else:
                cells.append("-")
        print(f"| {lab} | {len(slugs)} | " + " | ".join(cells) + " |")
    print("\nHedging P&L (the opposite leg) per regime, C vs B (sum over held-out markets):\n")
    print("| regime | B hedging | C hedging | C pairs |")
    print("|---|---:|---:|---:|")
    for lab in order:
        slugs = labels.get(lab, set())
        bh = sum(per["B"][s].get("hedging", 0.0) for s in slugs if s in per["B"])
        ch = sum(per["C"][s].get("hedging", 0.0) for s in slugs if s in per["C"])
        cp = sum(per["C"][s].get("pairs", 0.0) for s in slugs if s in per["C"])
        print(f"| {lab} | {bh:+.2f} | {ch:+.2f} | {cp:+.2f} |")


if __name__ == "__main__":
    main()
