"""Items 20 and 24: results by market regime and by time left at entry, out of sample (validation + test +
forward pooled; each split also shown for the main rows). Regimes are DESCRIPTIVE labels of each window computed
from its own path after the fact (they slice the results; no decision reads them):
  strong trend |net| >= 1.5 sigma_window, weak 0.5-1.5, sideways < 0.5; high / low volatility (1 s realised vol
  >= 1.5x / <= 0.67x the median); rapid reversal (an excursion of >= 1 sigma_window that retraced >= 1);
  large shock (a 1 s move >= 5 sigma); noisy (10 s sign changes in the top quartile and |net| < 0.5).
usage: regimes.py '<json list of [name, overrides, label]>'"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
import replay as rp   # noqa: E402

M1 = rp.FN.index("mom1")


def labels(ws):
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


def summ(recs):
    traded = [r for r in recs if r["traded"]]
    pnl = [r.get("total", 0.0) for r in traded]
    if not pnl:
        return "n    0"
    m = statistics.mean(pnl)
    se = statistics.pstdev(pnl) / math.sqrt(len(pnl)) if len(pnl) > 1 else float("nan")
    hed = sum(r.get("hedging", 0.0) for r in traded)
    return f"n {len(pnl):4} {m:+.3f}/mkt (se {se:.3f}) net {sum(pnl):+8.2f} hedging {hed:+7.2f}"


def main():
    cfgs = json.loads(sys.argv[1])
    ws = rp.load_cache()
    labs = labels(ws)
    jobs = [(n, o) for n, o, _ in cfgs]
    res = rp.batch(jobs, splits_=["validation", "test", "forward"], keep=True, procs=len(jobs))
    names = [lab for _, _, lab in cfgs]
    print("Out of sample (validation + test + forward), per traded market\n")
    print(f"{'regime':20} " + " | ".join(f"{n:^44}" for n in names))
    for reg in ("strong trend", "weak trend", "sideways", "high volatility", "low volatility", "rapid reversal",
                "large crypto shock", "noisy", "ALL"):
        cells = []
        for _, _, out in res:
            recs = [r for s in ("validation", "test", "forward") for r in out[s]["_recs"]
                    if reg == "ALL" or r["slug"] in labs[reg]]
            cells.append(f"{summ(recs):44}")
        print(f"{reg:20} " + " | ".join(cells))
    print("\nBy seconds left at the first entry of the market")
    for lo, hi in ((240, 300), (180, 240), (120, 180), (60, 120), (10, 60)):
        cells = []
        for _, _, out in res:
            recs = [r for s in ("validation", "test", "forward") for r in out[s]["_recs"]
                    if r["traded"] and r["entry_log"] and lo <= r["entry_log"][0]["left"] < hi]
            cells.append(f"{summ(recs):44}")
        print(f"{lo:3}-{hi:3} s left       " + " | ".join(cells))
    print("\nHedges by seconds left at the decision (count, filled, kinds)")
    for (_, _, out), n in zip(res, names):
        hl = [h for s in ("validation", "test", "forward") for r in out[s]["_recs"] for h in r["hedge_log"]]
        if not hl:
            continue
        line = []
        for lo, hi in ((240, 300), (120, 240), (60, 120), (30, 60), (10, 30)):
            hh = [h for h in hl if lo <= h["left"] < hi]
            line.append(f"{lo}-{hi}s {sum(1 for h in hh if h.get('px') is not None)}/{len(hh)}")
        print(f"  {n:28} " + "  ".join(line))


main()
