"""Tail-risk anatomy of today's rule (B) and the candidates, plus the market-regime split (brief items 17, 19).

    nice -n 19 python scripts/paired_exit/tail.py > results/paired_exit_tail.md

B's legs: PAIRED (the resting pair filled), NAKED WIN, NAKED LOSS. EV per fill =
    P(pair) * avg pair + P(naked win) * avg naked win + P(naked loss) * avg naked loss
by asset, seconds left, entry price, momentum strength, volatility tercile and the entry model's EV.
Regimes are HINDSIGHT labels of each window (trend strength, realised volatility tercile, reversals); they
describe where a rule makes / loses money, they are not a tradable filter.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import replace

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L      # noqa: E402
import run as R         # noqa: E402
import sim as S         # noqa: E402
import pe_select as SEL  # noqa: E402
from stage2 import entry_of  # noqa: E402

B = S.Params(name="B today", structure="B", entry="old", lo=0.20, hi=0.93, limit_off=0.03, pair_target=0.95, stop=None)


def bucket(v, edges, labels):
    for (lo, hi), lab in zip(edges, labels):
        if lo <= v < hi:
            return lab
    return None


def decompose(legs):
    f = [r for r in legs if r.get("filled", True)]
    n = len(f)
    if not n:
        return None
    pair = [r["pnl"] for r in f if r.get("paired")]
    nw = [r["pnl"] for r in f if not r.get("paired") and r["pnl"] > 0]
    nl = [r["pnl"] for r in f if not r.get("paired") and r["pnl"] <= 0]
    g = lambda x: float(np.mean(x)) if x else 0.0   # noqa: E731
    ev = len(pair) / n * g(pair) + len(nw) / n * g(nw) + len(nl) / n * g(nl)
    return {"n": n, "p_pair": len(pair) / n, "avg_pair": g(pair), "p_nwin": len(nw) / n, "avg_nwin": g(nw),
            "p_nloss": len(nl) / n, "avg_nloss": g(nl), "ev": ev,
            "pairs_per_loss": abs(g(nl)) / g(pair) if pair and g(pair) > 0 else float("nan")}


def dline(label, d):
    if d is None:
        return f"| {label} | 0 |" + " |" * 8
    return (f"| {label} | {d['n']} | {d['p_pair']:.0%} | {d['avg_pair']:+.3f} | {d['p_nwin']:.0%} | {d['avg_nwin']:+.2f} | "
            f"{d['p_nloss']:.0%} | {d['avg_nloss']:+.2f} | {d['ev']:+.3f} | {d['pairs_per_loss']:.1f} |")


DHEAD = ("| bucket | fills | P(pair) | avg pair | P(naked win) | avg naked win | P(naked loss) | avg naked loss | EV / fill | "
         "pairs to repay 1 naked loss |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")


def main():
    stage1 = SEL.by_asset(SEL.load("stage1"))
    print("# Tail risk and regimes\n")
    allB = []
    regime_rows = defaultdict(lambda: defaultdict(list))   # (rule) -> (regime label) -> window pnl
    for asset in L.ASSETS:
        wins = R.load(asset)
        ent = entry_of(SEL.choose(stage1[asset])[0])
        base = S.Params(**{**ent, "name": "x", "structure": "C", "stop": None})
        costs = json.load(open(f"{L.CACHE}/costs_train.json"))
        base = replace(base, cost_c=round(costs[asset]["entry_cost"], 4))
        cands = {"B today": B, "C hold": base,
                 "D fairTP+time30": replace(base, structure="D", tp_offsets=(0.02,), tp_fracs=(1.0,), tp_fair=0.03,
                                            time_exit_s=30.0)}
        sig = np.array([w.sigma for w in wins])
        t1, t2 = np.quantile(sig, [1 / 3, 2 / 3])
        for w in wins:
            w.regime = L.classify_regime(w)
            w.regime["vol"] = "low vol" if w.sigma < t1 else "high vol" if w.sigma >= t2 else "mid vol"
        fl = np.array([w.regime["flips"] for w in wins])
        f_hi = np.quantile(fl, 0.8)
        ex = L.Exec()
        for name, prm in cands.items():
            for w in wins:
                pnl, legs = S.run_window(w, w.fair_up, prm, ex, R.OLD_LOCK)
                labs = [w.regime["trend"], w.regime["vol"], "rapid reversals" if w.regime["flips"] >= f_hi else "few reversals"]
                for lab in labs:
                    regime_rows[(asset, name)][lab].append(pnl)
                    regime_rows[("ALL", name)][lab].append(pnl)
                if name == "B today":
                    for r in legs:
                        if r.get("filled", True):
                            r["vol"] = w.regime["vol"]
                            allB.append(r)
    # ---- B anatomy
    print("## 1. Today's rule (B): where the money goes\n")
    print("Every fill of today's rule (entry model, resting pair < 0.95, legs held to settlement) on all 7 assets, all "
          "splits.\n")
    print(DHEAD)
    print(dline("ALL", decompose(allB)))
    for a in L.ASSETS:
        print(dline(a, decompose([r for r in allB if r["asset"] == a])))
    for lab, key, edges, labels in (
            ("seconds left", "left", ((60, 120), (120, 180), (180, 240), (240, 301)), ("60-120", "120-180", "180-240", "240-300")),
            ("entry price", "px", ((0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 1.0)), ("<0.50", "0.50-0.70", "0.70-0.80", "0.80-0.93")),
            ("momentum |z1|", "z1", ((0, 2), (2, 3), (3, 99)), ("1-2", "2-3", ">=3")),
            ("entry model EV", "edge", ((-9, 0.04), (0.04, 0.08), (0.08, 9)), ("0.02-0.04", "0.04-0.08", ">=0.08"))):
        print(f"\n**by {lab}**\n")
        print(DHEAD)
        for lb in labels:
            sub = [r for r in allB if bucket(abs(r[key]) if key == "z1" else r[key], edges, labels) == lb]
            print(dline(lb, decompose(sub)))
    print("\n**by volatility (window sigma tercile, causal)**\n")
    print(DHEAD)
    for lb in ("low vol", "mid vol", "high vol"):
        print(dline(lb, decompose([r for r in allB if r.get("vol") == lb])))
    d = decompose(allB)
    print(f"\nRepaying one naked loss of {d['avg_nloss']:+.2f} takes {abs(d['avg_nloss']) / d['avg_pair']:.1f} completed pairs of "
          f"{d['avg_pair']:+.3f}. With P(pair) {d['p_pair']:.0%} and P(naked loss) {d['p_nloss']:.0%} there are "
          f"{d['p_pair'] / max(d['p_nloss'], 1e-9):.1f} pairs per naked loss: the rule needs ~{abs(d['avg_nloss']) / d['avg_pair']:.0f}.\n")
    # ---- regimes
    print("## 2. Market regimes (hindsight labels): $ per window\n")
    labs = ("strong trend", "weak trend", "sideways", "low vol", "mid vol", "high vol", "rapid reversals", "few reversals")
    print("| asset | rule | " + " | ".join(labs) + " |")
    print("|---|---|" + "---:|" * len(labs))
    for (asset, name), d in regime_rows.items():
        cells = []
        for lb in labs:
            v = d.get(lb, [])
            cells.append(f"{np.mean(v):+.3f} ({len(v)})" if v else "—")
        print(f"| {asset} | {name} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
