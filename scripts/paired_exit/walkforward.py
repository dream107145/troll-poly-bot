"""Walk-forward (brief item 20): for each test day d, the fair model is re-fitted on every window before day d-1,
the entry costs are re-measured on those windows, the entry parameters are optimised on them (top 10 by t),
chosen on day d-1 (validation) and traded on day d, untouched. The exit structure and the caps are fixed
(the study's candidate: fair-value take-profit + time exit 30 s, 10 USDC / window, 1 open leg, 3 buys).

    nice -n 19 python scripts/paired_exit/walkforward.py [ASSET ...] > results/paired_exit_walkforward.md
"""
from __future__ import annotations

import itertools
import json
import math
import sys
import time
from dataclasses import replace

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L   # noqa: E402
import run as R      # noqa: E402
import sim as S      # noqa: E402

STRUCT = dict(structure="D", tp_offsets=(0.02,), tp_fracs=(1.0,), tp_fair=0.03, time_exit_s=30.0, stop=None,
              max_window_usdc=10.0, max_unpaired_usdc=10.0, max_dir_usdc=10.0, max_open=1, max_buys=3)
GRID = list(itertools.product((1.5, 2.0, 3.0), (0.0, 0.02, 0.04), (-1.0, 0.34), ((0.15, 0.85), (0.30, 0.70)), (0.02, 0.03)))


def fit(wins):
    X, y = [], []
    for w in wins:
        idx = np.arange(0, w.n, 5)
        idx = idx[(w.left[idx] >= 5) & (w.left[idx] <= 295) & ~np.isnan(w.F["mid"][idx])]
        if len(idx):
            X.append(L.design(w.F, idx)); y.append(np.full(len(idx), 1.0 if w.outcome == "UP" else 0.0))
    return L.fit_logistic(np.vstack(X), np.concatenate(y), l2=50.0)


def entry_cost(wins, ex):
    sl, ff, wf, fa, wa = [], [], [], [], []
    for w in wins:
        for i, d in S.candidates(w, 2.0, 60, 290):
            side = "UP" if d > 0 else "DOWN"
            ask = (w.ua if d > 0 else w.da)[i]
            if not (0.15 <= ask <= 0.85):
                continue
            fair = w.fair_up[i] if d > 0 else 1 - w.fair_up[i]
            won = 1.0 if w.outcome == side else 0.0
            fa.append(fair); wa.append(won)
            px, _ = ex.taker_buy(w, side, w.T[i], round(ask + 0.02, 2))
            if px is not None:
                sl.append(px - ask); ff.append(fair); wf.append(won)
    if not ff:
        return 0.02
    adv = (np.mean(ff) - np.mean(wf)) - (np.mean(fa) - np.mean(wa))
    return float(np.mean(sl) + max(adv, 0.0))


def run_set(wins, prm, ex):
    pn, legs = [], []
    for w in wins:
        p, lg = S.run_window(w, w.fair_up, prm, ex, R.OLD_LOCK)
        pn.append(p); legs.extend(lg)
    return np.array(pn), legs


def main(assets):
    print("# Walk-forward: re-fit, re-cost, re-select every day; trade the next day untouched\n")
    print(f"Fixed structure: {STRUCT}. Entry grid: k_sig x min_edge x agreement x band x limit offset = {len(GRID)}.\n")
    for asset in assets:
        t0 = time.time()
        wins = L.prepare(L.load_asset(asset))
        days = sorted({w.day for w in wins})
        rows, oos_all, oos_hold = [], [], []
        for k in range(3, len(days)):
            d_val, d_test = days[k - 1], days[k]
            train = [w for w in wins if w.day < d_val]
            val = [w for w in wins if w.day == d_val]
            test = [w for w in wins if w.day == d_test]
            if len(train) < 200 or not val or not test:
                continue
            model = fit(train)
            for w in train + val + test:
                w.fair_up = L.predict(model, L.design(w.F))
            ex = L.Exec()
            cost = round(entry_cost(train, ex), 4)
            scored = []
            for kk, e, ag, band, off in GRID:
                prm = S.Params(name="wf", k_sig=kk, min_edge=e, min_agree=ag, lo=band[0], hi=band[1], limit_off=off,
                               cost_c=cost, **STRUCT)
                pn, legs = run_set(train, prm, ex)
                fills = sum(1 for r in legs if r.get("filled", True))
                sd = pn.std()
                tt = pn.mean() / (sd / math.sqrt(len(pn))) if sd > 0 else -9
                scored.append((tt, fills, prm))
            top = sorted([s for s in scored if s[1] >= 40], key=lambda s: -s[0])[:10] or sorted(scored, key=lambda s: -s[0])[:10]
            best = max(top, key=lambda s: run_set(val, s[2], ex)[0].mean())
            prm = best[2]
            pn_is = run_set(train, prm, ex)[0]
            pn_v = run_set(val, prm, ex)[0]
            pn_t, legs_t = run_set(test, prm, ex)
            pn_h = run_set(test, replace(prm, structure="C", tp_offsets=(), tp_fracs=(), tp_fair=None, time_exit_s=None), ex)[0]
            oos_all.extend(pn_t.tolist()); oos_hold.extend(pn_h.tolist())
            fl = [r for r in legs_t if r.get("filled", True)]
            rows.append((d_test, len(train), cost, prm, pn_is.mean(), pn_v.mean(), pn_t.mean(), pn_t.sum(), len(fl),
                         pn_t.min(), pn_h.mean()))
        print(f"## {asset}\n")
        print("| test day | train windows | entry cost | chosen entry (k, edge, agree, band, offset) | IS $/win | val $/win | "
              "OOS $/win | OOS net $ | OOS fills | OOS worst window | OOS hold-only $/win |")
        print("|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|")
        for d, ntr, cost, prm, a, b, c, net, nf, worst, h in rows:
            day = time.strftime("%m-%d", time.gmtime(d * 86400))
            print(f"| {day} | {ntr} | {cost:.3f} | {prm.k_sig:g}, {prm.min_edge:g}, {prm.min_agree:g}, {prm.lo:g}-{prm.hi:g}, "
                  f"{prm.limit_off:g} | {a:+.3f} | {b:+.3f} | {c:+.3f} | {net:+.2f} | {nf} | {worst:+.2f} | {h:+.3f} |")
        o = np.array(oos_all)
        h = np.array(oos_hold)
        if len(o):
            se = o.std() / math.sqrt(len(o))
            print(f"\nOut-of-sample, all test days: {len(o)} windows, net {o.sum():+.2f} USDC, {o.mean():+.4f} per window "
                  f"(t {o.mean() / se if se > 0 else float('nan'):+.2f}), worst window {o.min():+.2f}; the same choices held "
                  f"to settlement instead: {h.mean():+.4f} per window, net {h.sum():+.2f}. ({time.time() - t0:.0f} s)\n")


if __name__ == "__main__":
    main(sys.argv[1:] or ["BTC"])
