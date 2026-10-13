"""Part 2 of the book-signal study: the triangle bot's paired-exit strategy (structure D: fair-value EV gate, maker
take-profit at max(fill + 0.02, fair + 0.03), 30 s time exit, 5 shares, 1 open leg, 3 buys) with the TRIGGER
swapped: crypto (today) | book | both (crypto confirmed by the book) | either. Same execution model (calibrated
paper fills), same fair model (paired-exit TRAIN fit), the book model fitted on TRAIN (part 1).

  A. controlled swap: today's live settings, only the trigger changes;
  B. per mode, a TRAIN grid -> top 10 by t (>= 40 fills) -> best on VALIDATION -> TEST / FORWARD / FORWARD2 untouched;
  C. daily walk-forward of the mode choice itself (each day's mode + parameters picked on the days before).

    nice -n 19 .venv/bin/python scripts/book_signal/strategy_study.py > results/book_signal_part2.md
"""
from __future__ import annotations

import itertools
import json
import math
import sys
import time
from dataclasses import replace
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/book_signal")
import bs_lib as B   # noqa: E402

L = B.L
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import sim as S      # noqa: E402

SPLITS = ("train", "validation", "test", "forward", "forward2")
OOS = ("test", "forward", "forward2")
LIVE = S.Params(name="live", structure="D", entry="fair", k_sig=2.0, min_agree=0.34, min_edge=0.02, cost_c=0.0367,
                lo=0.15, hi=0.85, min_left=60.0, max_left=290.0, late_left=180.0, late_edge_add=0.02, limit_off=0.03,
                shares=5.0, max_window_usdc=10.0, max_unpaired_usdc=5.0, max_dir_usdc=5.0, max_open=1, max_buys=3,
                stop=None, time_exit_s=30.0, settle_keep=0.95, tp_offsets=(0.02,), tp_fracs=(1.0,), tp_mode="maker",
                tp_fair=0.03)
WINS = None


def load():
    wins = B.load_btc()
    fair = json.load(open(f"{L.CACHE}/fair_btc.json"))
    book = json.load(open(f"{B.OUT}/book_model_train.json"))
    for w in wins:
        w.fair_up = L.predict(fair, L.design(w.F))
        w.F["book"] = B.predict(book, B.design(B.book_features(w, w.F["mid"])))
    return wins


def run(args):
    prm, splits = args
    ex = L.Exec()
    out = {}
    for s in splits:
        pn, nf, wins_, worst = [], 0, 0, 0.0
        legs_all = []
        for w in WINS:
            if w.split != s:
                continue
            p, legs = S.run_window(w, w.fair_up, prm, ex)
            pn.append(p)
            fl = [r for r in legs if r.get("filled", True)]
            nf += len(fl)
            wins_ += sum(1 for r in fl if r["pnl"] > 0)
            legs_all.extend((r["pnl"], r["kind"]) for r in fl)
        pn = np.array(pn)
        out[s] = {"pn": pn, "fills": nf, "wins": wins_, "legs": legs_all}
    return prm, out


def summ(res, splits):
    pn = np.concatenate([res[s]["pn"] for s in splits])
    nf = sum(res[s]["fills"] for s in splits)
    wn = sum(res[s]["wins"] for s in splits)
    sd = pn.std()
    t = pn.mean() / (sd / math.sqrt(len(pn))) if sd > 0 else float("nan")
    legs = [x for s in splits for x in res[s]["legs"]]
    tp = sum(1 for _, k in legs if k == "TAKE_PROFIT")
    return {"n": len(pn), "net": float(pn.sum()), "pw": float(pn.mean()), "t": t, "fills": nf,
            "win": wn / nf if nf else float("nan"), "worst": float(pn.min()) if len(pn) else 0.0,
            "tp": tp / nf if nf else float("nan"), "per_fill": float(pn.sum()) / nf if nf else float("nan")}


def row(name, r):
    return (f"| {name} | {r['fills']} | {r['win']:.0%} | {r['tp']:.0%} | {r['net']:+.2f} | {r['per_fill']:+.3f} | "
            f"{r['pw']:+.4f} | {r['t']:+.2f} | {r['worst']:+.2f} |")


HDR = ("| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |\n"
       "|---|---:|---:|---:|---:|---:|---:|---:|---:|")


def label(p):
    if p.signal == "crypto":
        trig = f"crypto z>={p.k_sig:g}"
    elif p.signal == "book":
        trig = f"book>={p.k_book:g}c"
    elif p.signal == "both":
        trig = f"crypto z>={p.k_sig:g} + book>={p.book_confirm:g}c"
    else:
        trig = f"crypto z>={p.k_sig:g} or book>={p.k_book:g}c"
    return (f"{trig}, edge {p.min_edge:g}, agree {p.min_agree:g}, band {p.lo:g}-{p.hi:g}, offset {p.limit_off:g}")


def grid(mode):
    base = LIVE
    out = []
    for e, band, off in itertools.product((0.0, 0.02, 0.04), ((0.15, 0.85), (0.30, 0.70)), (0.02, 0.03)):
        common = dict(min_edge=e, lo=band[0], hi=band[1], limit_off=off, signal=mode)
        if mode == "crypto":
            for k, ag in itertools.product((1.5, 2.0, 3.0), (-1.0, 0.34)):
                out.append(replace(base, k_sig=k, min_agree=ag, **common))
        elif mode == "book":
            for kb in (0.75, 1.0, 1.5, 2.0, 3.0):
                out.append(replace(base, k_book=kb, min_agree=-1.0, **common))
        elif mode == "both":
            for k, c, ag in itertools.product((1.5, 2.0, 3.0), (0.0, 0.5, 1.0), (-1.0, 0.34)):
                out.append(replace(base, k_sig=k, book_confirm=c, min_agree=ag, **common))
        else:
            for k, kb in itertools.product((2.0, 3.0), (1.0, 2.0, 3.0)):
                out.append(replace(base, k_sig=k, k_book=kb, min_agree=-1.0, **common))
    return out


def main():
    global WINS
    t0 = time.time()
    WINS = load()
    pool = Pool(3)
    print("# The triangle paired-exit strategy with a Polymarket-book trigger (BTC)\n")
    print("Execution: calibrated paper fills (lands 250 ms later, survives 1 s, worse of two books, fee 0.07 p(1-p)); "
          "exits as live (maker take-profit max(fill + 0.02, fair + 0.03), sell at 30 s left unless bid >= 0.95, else "
          "settle). Book model and fair model fitted on TRAIN only. forward2 = 10-01 21:25 UTC onward, never seen.\n")
    # ---- A. controlled swap
    swaps = [("today: crypto z>=2 (live settings)", LIVE)]
    for kb in (1.0, 1.5, 2.0, 3.0):
        swaps.append((f"book >= {kb:g} c (agreement off)", replace(LIVE, signal="book", k_book=kb, min_agree=-1.0)))
    for c in (0.0, 0.5, 1.0):
        swaps.append((f"both: crypto z>=2 + book >= {c:g} c in the last 1 s", replace(LIVE, signal="both", book_confirm=c)))
    for kb in (1.5, 2.0, 3.0):
        swaps.append((f"either: crypto z>=2 or book >= {kb:g} c", replace(LIVE, signal="either", k_book=kb)))
    res = dict(pool.map(run, [(p, SPLITS) for _, p in swaps]))
    print("## A. Today's settings, only the trigger changed\n")
    for period, sp in (("TRAIN", ("train",)), ("VALIDATION", ("validation",)), ("TEST + FORWARD", ("test", "forward")),
                       ("FORWARD2 (newest 4.6 h, untouched)", ("forward2",))):
        print(f"### {period}\n")
        print(HDR)
        for name, p in swaps:
            print(row(name, summ(res[p], sp)))
        print()
    # ---- B. per-mode selection
    print("## B. Each mode with its own parameters (TRAIN grid -> VALIDATION pick -> untouched OOS)\n")
    chosen = {}
    for mode in ("crypto", "book", "both", "either"):
        g = grid(mode)
        tr = pool.map(run, [(p, ("train",)) for p in g])
        scored = []
        for p, r in tr:
            s = summ(r, ("train",))
            scored.append((s["t"] if s["t"] == s["t"] else -9, s["fills"], p))
        top = sorted([x for x in scored if x[1] >= 40], key=lambda x: -x[0])[:10] or sorted(scored, key=lambda x: -x[0])[:10]
        vr = dict(pool.map(run, [(x[2], ("validation",)) for x in top]))
        best = max(top, key=lambda x: vr[x[2]]["validation"]["pn"].mean())[2]
        full = dict(pool.map(run, [(best, SPLITS)]))[best]
        chosen[mode] = (best, full)
        print(f"- **{mode}** ({len(g)} rules): chosen {label(best)}")
    print()
    for period, sp in (("TRAIN (in-sample)", ("train",)), ("VALIDATION (selection)", ("validation",)),
                       ("TEST + FORWARD (untouched)", ("test", "forward")), ("FORWARD2 (untouched, newest)", ("forward2",)),
                       ("ALL OUT-OF-SAMPLE (test + forward + forward2)", OOS)):
        print(f"### {period}\n")
        print(HDR)
        for mode, (p, r) in chosen.items():
            print(row(f"{mode}: {label(p)}", summ(r, sp)))
        print()
    pool.close()
    print(f"({time.time() - t0:.0f} s)")


if __name__ == "__main__":
    main()
