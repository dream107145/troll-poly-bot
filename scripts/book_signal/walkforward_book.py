"""Part 3 of the book-signal study: daily walk-forward. For each day d (from the 4th day), the fair model AND the book
model are re-fitted on every window before d, and FIXED rules -- today's live settings with each trigger -- trade day
d untouched. Every rule tried in part 2's controlled swap is shown, so nothing is picked after the fact.

    nice -n 19 .venv/bin/python scripts/book_signal/walkforward_book.py > results/book_signal_part3.md
"""
from __future__ import annotations

import math
import sys
import time
from dataclasses import replace
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/book_signal")
import bs_lib as B            # noqa: E402
import strategy_study as SS   # noqa: E402

L = B.L
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import sim as S                # noqa: E402
import walkforward as WF       # noqa: E402

LIVE = SS.LIVE
RULES = [("crypto (today)", LIVE)]
RULES += [(f"book >= {kb:g}", replace(LIVE, signal="book", k_book=kb, min_agree=-1.0)) for kb in (1.0, 1.5, 2.0)]
RULES += [(f"both + book >= {c:g}", replace(LIVE, signal="both", book_confirm=c)) for c in (0.0, 1.0)]
RULES += [(f"either book >= {kb:g}", replace(LIVE, signal="either", k_book=kb)) for kb in (1.5, 2.0, 3.0)]
DAY = {}


def fit_book(wins):
    X, y = [], []
    for w in wins:
        bf = B.book_features(w, w.F["mid"])
        yy = B.future_move(w, w.F["mid"])
        idx = np.flatnonzero(~np.isnan(yy) & (w.left >= 5) & (w.left <= 295) & ~np.isnan(bf["vel5"]))
        idx = idx[::2]
        X.append(B.design(bf, idx)); y.append(yy[idx])
    return B.fit_ridge(np.vstack(X), np.concatenate(y))


def run_rule(k):
    ex = L.Exec()
    pn, fills = [], 0
    for w in DAY["test"]:
        p, legs = S.run_window(w, w.fair_up, RULES[k][1], ex)
        pn.append(p)
        fills += sum(1 for r in legs if r.get("filled", True))
    return k, np.array(pn), fills


def main():
    t0 = time.time()
    wins = B.load_btc()
    days = sorted({w.day for w in wins})
    print("# Daily walk-forward: models re-fitted on everything before the day, fixed rules, the day traded untouched\n")
    print("Rules = today's live settings (edge 0.02 after cost 0.0367, agreement 0.34 where crypto triggers, band "
          "0.15-0.85, offset 0.03, late +0.02, exits as live) with each trigger. $ per market; fills in brackets.\n")
    names = [n for n, _ in RULES]
    print("| day | markets | " + " | ".join(names) + " |")
    print("|---|---:|" + "---:|" * len(names))
    allp = {k: [] for k in range(len(RULES))}
    allf = {k: 0 for k in range(len(RULES))}
    days_pos = {k: 0 for k in range(len(RULES))}
    ndays = 0
    for d in days[3:]:
        train = [w for w in wins if w.day < d]
        test = [w for w in wins if w.day == d]
        if len(train) < 200 or len(test) < 20:
            continue
        fair = WF.fit(train)
        book = fit_book(train)
        for w in test:
            w.fair_up = L.predict(fair, L.design(w.F))
            w.F["book"] = B.predict(book, B.design(B.book_features(w, w.F["mid"])))
        DAY["test"] = test
        with Pool(3) as pool:
            out = pool.map(run_rule, range(len(RULES)))
        ndays += 1
        cells = []
        for k, pn, fills in sorted(out):
            allp[k].extend(pn.tolist()); allf[k] += fills
            days_pos[k] += int(pn.sum() > 0)
            cells.append(f"{pn.mean():+.3f} ({fills})")
        print(f"| {time.strftime('%m-%d', time.gmtime(d * 86400))} | {len(test)} | " + " | ".join(cells) + " |", flush=True)
    cells, cells_t, cells_d, cells_f = [], [], [], []
    for k in range(len(RULES)):
        a = np.array(allp[k])
        se = a.std() / math.sqrt(len(a))
        cells.append(f"{a.mean():+.4f}")
        cells_t.append(f"{a.mean() / se:+.2f}" if se > 0 else "-")
        cells_d.append(f"{days_pos[k]}/{ndays}")
        cells_f.append(f"{a.sum() / max(allf[k], 1):+.3f}")
    print("| **all days, $ per market** | " + str(len(allp[0])) + " | " + " | ".join(cells) + " |")
    print("| t | | " + " | ".join(cells_t) + " |")
    print("| $ per fill | | " + " | ".join(cells_f) + " |")
    print("| days positive | | " + " | ".join(cells_d) + " |")
    print(f"\n({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
