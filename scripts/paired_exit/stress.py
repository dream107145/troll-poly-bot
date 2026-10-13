"""Stress tests (brief item 21) on the candidate and the comparison rules, TEST + FORWARD windows (untouched).

    nice -n 19 python scripts/paired_exit/stress.py [ASSET] > results/paired_exit_stress.md
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import replace

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L       # noqa: E402
import run as R          # noqa: E402
import sim as S          # noqa: E402
import pe_select as SEL  # noqa: E402
from stage2 import entry_of  # noqa: E402

SCEN = [("base (calibrated)", {}, None),
        ("latency +50 %", {"lat_mult": 1.5}, None), ("latency +100 %", {"lat_mult": 2.0}, None),
        ("latency +200 %", {"lat_mult": 3.0}, None),
        ("fill probability -10 %", {"fill_haircut": 0.10}, None), ("fill probability -25 %", {"fill_haircut": 0.25}, None),
        ("fill probability -50 %", {"fill_haircut": 0.50}, None),
        ("spread +1c", {"spread_add": 0.01}, None), ("spread +2c", {"spread_add": 0.02}, None),
        ("slippage +1c", {"slip_add": 0.01}, None), ("slippage +2c", {"slip_add": 0.02}, None),
        ("survival window 2 s (stricter fills)", {"survive_ms": 2000.0}, None),
        ("resting orders fill on touch (optimistic)", {"touch_fill": True}, None),
        ("Polymarket book 1 s stale", {"stale_book_ms": 1000.0}, None),
        ("spot feed 200 ms late", {}, ("shift", 200.0)), ("spot feed 500 ms late", {}, ("shift", 500.0)),
        ("binance missing", {}, ("drop", "binance")), ("coinbase missing", {}, ("drop", "coinbase")),
        ("bybit missing", {}, ("drop", "bybit"))]


def main(asset="BTC"):
    wins = R.load(asset)
    model = json.load(open(f"{L.CACHE}/fair_{asset.lower()}.json"))
    costs = json.load(open(f"{L.CACHE}/costs_train.json"))
    ent = entry_of(SEL.choose(SEL.by_asset(SEL.load("stage1"))[asset])[0])
    base = replace(S.Params(**{**ent, "name": "x", "structure": "C", "stop": None}),
                   cost_c=round(costs[asset]["entry_cost"], 4))
    rules = {"candidate: fairTP + time30": replace(base, structure="D", tp_offsets=(0.02,), tp_fracs=(1.0,), tp_fair=0.03,
                                                  time_exit_s=30.0),
             "C hold to settlement": base,
             "C + stop 0.20": replace(base, stop=0.20),
             "B today (pair < 0.95)": S.Params(name="B", structure="B", entry="old", lo=0.20, hi=0.93, limit_off=0.03,
                                              pair_target=0.95, stop=None)}
    oos = [w for w in wins if w.split in ("test", "forward")]
    print(f"# Stress tests: {asset}, TEST + FORWARD ({len(oos)} windows)\n")
    print("$ per window (fills, worst window). Disagreement: share of candidate signals whose exchanges' 1 s returns "
          "disperse by more than 3 sigma (the kill condition) is printed at the end.\n")
    print("| scenario | " + " | ".join(rules) + " |")
    print("|---|" + "---:|" * len(rules))
    saved = {w.slug: (w.F, w.fair_up) for w in oos}
    for name, kw, feat in SCEN:
        if feat is not None:
            for w in oos:
                F = L.compute_features(w, w.sigma, spot_shift_ms=feat[1]) if feat[0] == "shift" else \
                    L.compute_features(w, w.sigma, drop_exchange=feat[1])
                w.F, w.fair_up = F, L.predict(model, L.design(F))
        cells = []
        for rname, prm in rules.items():
            ex = L.Exec(**kw)
            pn, legs = [], []
            for w in oos:
                p, lg = S.run_window(w, w.fair_up, prm, ex, R.OLD_LOCK)
                pn.append(p); legs.extend(lg)
            pn = np.array(pn)
            nf = sum(1 for r in legs if r.get("filled", True))
            cells.append(f"{pn.mean():+.3f} ({nf}, {pn.min():+.1f})")
        print(f"| {name} | " + " | ".join(cells) + " |")
        if feat is not None:
            for w in oos:
                w.F, w.fair_up = saved[w.slug]
    n = big = 0
    for w in oos:
        for i, d in S.candidates(w, base.k_sig, 60, 290):
            n += 1
            big += int(w.F["disp"][i] > 3.0)
    print(f"\nExchange disagreement: {big} of {n} candidate signals ({big / max(n, 1):.1%}) have a 1 s cross-exchange "
          f"dispersion above 3 sigma (refused by the kill condition).")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "BTC")
