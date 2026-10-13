"""Part 1 of the book-signal study: does the Polymarket book tell the token's next move, and does it say it before
crypto spot does?

  1. the ridge book model (bs_lib) fitted on TRAIN; its correlation / sign hit with the next-1 s UP mid move on
     every split (validation / test / forward / forward2 untouched);
  2. timing: every token move of >= 2c within 1 s -- when did the crypto trigger (|1 s spot z| >= 2, the bot's
     current signal) and the book trigger first fire in the move's direction, relative to the move's start;
  3. triggers as trades: for each trigger, the taker cost of acting on it (the calibrated paper fill: lands
     250 ms later, worse of two books) against the token mid 1 s / 5 s / 30 s later and at settlement.

    nice -n 19 .venv/bin/python scripts/book_signal/signal_study.py > results/book_signal_part1.md
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/book_signal")
import bs_lib as B   # noqa: E402

L = B.L
SPLITS = ("train", "validation", "test", "forward", "forward2")


def attach(wins, model):
    for w in wins:
        bf = B.book_features(w, w.F["mid"])
        w.F["book"] = B.predict(model, B.design(bf))
        w.F["yfut"] = B.future_move(w, w.F["mid"])
        for k in ("acons1", "acons2", "vel1", "vel2", "imb"):
            w.F["b_" + k] = bf[k]


def triggers(w, kind, thr, min_left=60.0, max_left=290.0, gap_ms=1000.0):
    """(index, direction) of every trigger moment, one per direction per gap_ms."""
    x = w.F["z1"] if kind == "crypto" else w.F["book"]
    m = (np.abs(np.nan_to_num(x)) >= thr) & (w.left >= min_left) & (w.left <= max_left)
    out, last = [], {1: -1e18, -1: -1e18}
    for i in np.flatnonzero(m):
        d = 1 if x[i] > 0 else -1
        if w.T[i] - last[d] >= gap_ms:
            last[d] = w.T[i]
            out.append((int(i), d))
    return out


def main():
    wins = B.load_btc()
    by = defaultdict(list)
    for w in wins:
        by[w.split].append(w)
    print("# Polymarket book direction signal vs crypto spot (BTC 5-minute markets)\n")
    print("Windows per split: " + ", ".join(f"{s} {len(by[s])}" for s in SPLITS) + ". forward2 = the windows archived "
          "after the paired-exit study's cache (10-01 21:25 UTC onward), never used by any study.\n")
    # ---- 1. fit on TRAIN
    X, y = [], []
    for w in by["train"]:
        bf = B.book_features(w, w.F["mid"])
        yy = B.future_move(w, w.F["mid"])
        idx = np.flatnonzero(~np.isnan(yy) & (w.left >= 5) & (w.left <= 295) & ~np.isnan(bf["vel5"]))
        X.append(B.design(bf, idx)); y.append(yy[idx])
    X = np.vstack(X); y = np.concatenate(y)
    model = B.fit_ridge(X, y)
    json.dump(model, open(f"{B.OUT}/book_model_train.json", "w"), indent=1)
    print("## 1. The book model (ridge, fitted on TRAIN only): next-1 s UP mid move in cents\n")
    print("| feature | weight per sd |")
    print("|---|---:|")
    for f, wt in zip(model["features"], model["w"][1:]):
        print(f"| {f} | {wt:+.3f} |")
    attach(wins, model)
    print("\n| split | samples | corr(book, next 1 s) | corr(crypto z1, next 1 s) | book sign hit, strongest 10 % | crypto sign hit, strongest 10 % |")
    print("|---|---:|---:|---:|---:|---:|")
    q90 = None
    for s in SPLITS:
        bb, zz, yy = [], [], []
        for w in by[s]:
            m = ~np.isnan(w.F["yfut"]) & ~np.isnan(w.F["book"]) & ~np.isnan(w.F["z1"]) & (w.left >= 5) & (w.left <= 295)
            bb.append(w.F["book"][m]); zz.append(w.F["z1"][m]); yy.append(w.F["yfut"][m])
        bb, zz, yy = np.concatenate(bb), np.concatenate(zz), np.concatenate(yy)
        if s == "train":
            q90 = (np.quantile(np.abs(bb), 0.9), np.quantile(np.abs(zz), 0.9))

        def hit(x, q):
            m = (np.abs(x) >= q) & (yy != 0)
            return float(np.mean(np.sign(x[m]) == np.sign(yy[m]))), int(m.sum())
        hb, nb = hit(bb, q90[0]); hz, nz = hit(zz, q90[1])
        print(f"| {s} | {len(yy)} | {np.corrcoef(bb, yy)[0, 1]:+.3f} | {np.corrcoef(zz, yy)[0, 1]:+.3f} | "
              f"{hb:.0%} (n {nb}) | {hz:.0%} (n {nz}) |")
    print(f"\nStrongest-10 % thresholds from TRAIN: |book| >= {q90[0]:.3f} c, |z1| >= {q90[1]:.2f}.\n")

    # ---- 2. timing on token moves
    print("## 2. Who fires first on a real token move\n")
    print("A move = the UP-equivalent mid changes >= 2c within the next 1 s from a sample (the first such sample "
          "starts it; moves >= 3 s apart). For each, the first trigger in the move's direction from 3 s before the "
          "start to 1 s after it. Crypto = |1 s spot z| >= 2 (the bot today). Book = |book model| >= the threshold.\n")
    thr_list = (0.5, 0.75, 1.0)
    for s in ("train", "validation", "test", "forward", "forward2"):
        res = defaultdict(list)
        n_moves = 0
        for w in by[s]:
            mid = w.F["mid"]
            j5 = np.minimum(np.arange(w.n) + 5, w.n - 1)
            okg = (w.T[j5] - w.T) <= 1450.0
            fut_max = np.full(w.n, np.nan); fut_min = np.full(w.n, np.nan)
            for h in range(1, 6):
                jh = np.minimum(np.arange(w.n) + h, w.n - 1)
                fut_max = np.fmax(fut_max, mid[jh]); fut_min = np.fmin(fut_min, mid[jh])
            up = okg & (fut_max - mid >= 0.02 - 1e-9)
            dn = okg & (mid - fut_min >= 0.02 - 1e-9)
            last = -1e18
            for i in range(1, w.n):
                d = 1 if (up[i] and not up[i - 1]) else -1 if (dn[i] and not dn[i - 1]) else 0
                if d == 0 or not (30.0 <= w.left[i] <= 290.0) or w.T[i] - last < 3000.0:
                    continue
                last = w.T[i]
                n_moves += 1
                lo = int(np.searchsorted(w.T, w.T[i] - 3000.0)); hi = int(np.searchsorted(w.T, w.T[i] + 1000.0, side="right"))
                z = w.F["z1"][lo:hi] * d
                k = np.flatnonzero(np.nan_to_num(z) >= 2.0)
                tc = (w.T[lo + k[0]] - w.T[i]) if len(k) else None
                res["crypto"].append(tc)
                bk = w.F["book"][lo:hi] * d
                for thr in thr_list:
                    k = np.flatnonzero(np.nan_to_num(bk) >= thr)
                    tb = (w.T[lo + k[0]] - w.T[i]) if len(k) else None
                    res[f"book {thr}"].append(tb)
                    if tc is not None and tb is not None:
                        res[f"lead {thr}"].append(tc - tb)
        print(f"**{s}**: {n_moves} moves\n")
        print("| trigger | found | median ms vs start | before the start | book earlier than crypto (both found) |")
        print("|---|---:|---:|---:|---:|")
        for key in ["crypto"] + [f"book {t}" for t in thr_list]:
            v = [x for x in res[key] if x is not None]
            if not v:
                print(f"| {key} | 0 | - | - | - |"); continue
            lead = ""
            if key.startswith("book"):
                ld = res["lead " + key.split()[1]]
                lead = f"{np.mean(np.array(ld) > 0):.0%} earlier, {np.mean(np.array(ld) == 0):.0%} same sample, median lead {np.median(ld):+.0f} ms (n {len(ld)})" if ld else "-"
            print(f"| {key} | {len(v) / max(1, n_moves):.0%} | {np.median(v):+.0f} | {np.mean(np.array(v) < 0):.0%} | {lead} |")
        print()

    # ---- 3. triggers as trades
    print("## 3. Every trigger as a taker trade\n")
    print("Bought at the calibrated paper fill (lands 250 ms later, must survive 1 s, worse of two books, limit "
          "ask + 0.03, band 0.15-0.85, 60-290 s left), fee 0.07 p(1-p). Markout = the token's mid h later minus the "
          "fill price minus the fee (cents per share). Rate = triggers per hour of markets. The fair-value gate is NOT "
          "applied here (that is part 2).\n")
    ex = L.Exec()
    configs = [("crypto", 2.0), ("crypto", 3.0)] + [("book", t) for t in (0.5, 0.75, 1.0, 1.5)]
    for s in SPLITS:
        print(f"**{s}** ({len(by[s])} markets)\n")
        print("| trigger | per hour | fill rate | +1 s | +5 s | +30 s | settlement | settlement win rate |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|")
        for kind, thr in configs:
            n, nf, m1, m5, m30, mset, wins_ = 0, 0, [], [], [], [], []
            for w in by[s]:
                for i, d in triggers(w, kind, thr):
                    side = "UP" if d > 0 else "DOWN"
                    ask = (w.ua if d > 0 else w.da)[i]
                    if math.isnan(ask) or not (0.15 <= ask <= 0.85):
                        continue
                    n += 1
                    px, known = ex.taker_buy(w, side, w.T[i], min(round(ask + 0.03, 2), 0.99))
                    if px is None:
                        continue
                    nf += 1
                    fee = L.fee(px)
                    for h, acc in ((1000.0, m1), (5000.0, m5), (30000.0, m30)):
                        j = w.ix(w.T[i] + 250.0 + h)
                        mu = w.F["mid"][j]
                        tok = mu if d > 0 else 1.0 - mu
                        if not math.isnan(tok):
                            acc.append((tok - px - fee) * 100.0)
                    won = 1.0 if w.outcome == side else 0.0
                    mset.append((won - px - fee) * 100.0); wins_.append(won)
            hours = len(by[s]) * 5.0 / 60.0
            f = lambda a: f"{np.mean(a):+.2f}" if a else "-"   # noqa: E731
            print(f"| {kind} {thr:g} | {n / hours:.0f} | {nf / max(n, 1):.0%} | {f(m1)} | {f(m5)} | {f(m30)} | {f(mset)} "
                  f"| {np.mean(wins_) if wins_ else float('nan'):.0%} |")
        print()


if __name__ == "__main__":
    main()
