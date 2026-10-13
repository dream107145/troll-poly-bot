"""Stability check of the sweep's leaders (scripts/pairlock/replay_swing_sweep.py): every one-step neighbour of the
top N candidates in results/pairlock_swing_sweep.json is evaluated (those the sweep did not run are run now, same data,
same model, same score), and each leader gets its neighbourhood figures. Writes
results/pairlock_swing_sweep_neighbourhood.json.

    nice -n 10 .venv/bin/python scripts/pairlock/swing_sweep_neighbourhood.py --top 5 --workers 5
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import statistics
import sys
from dataclasses import asdict
from multiprocessing import Pool

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
import replay_swing_sweep as SW                                  # noqa: E402

ROOT = "/root/troll-poly-bot"


def _job(c):
    _, _, per, _ = SW._job((c, SW.NO_STRESS, False))
    return c, per


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default=f"{ROOT}/results/pairlock_swing_sweep.json")
    ap.add_argument("--out", default=f"{ROOT}/results/pairlock_swing_sweep_neighbourhood.json")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--cache", default="/tmp/claude-0/-root-troll-poly-bot/53e010f8-a35d-42fb-b329-fe76e0f4a11d/scratchpad/swing")
    a = ap.parse_args()
    D = json.load(open(a.sweep))
    known = {SW.Cand(**c["cand"]).key(): c for c in D["candidates"] if c["stress"] == "none"}
    with open(os.path.join(a.cache, "swing_windows.pkl"), "rb") as f:
        ws = pickle.load(f)
    day = lambda w: __import__("time").strftime("%Y-%m-%d", __import__("time").gmtime(w["open"] / 1000.0))   # noqa: E731
    ws = [w for w in ws if "2026-09-22" <= day(w) <= "2026-10-07"]
    assert ws[-1]["slug"] == D["last_market"] and len(ws) == D["markets"], "not the sweep's data snapshot"
    SW._WS, SW._SPLIT = ws, SW.split_of(ws)
    leaders = [SW.Cand(**t["cand"]) for t in D["top20"][:a.top]]
    todo = list(dict.fromkeys(n for c in leaders for n in SW.neighbours(c) if n.key() not in known))
    print(f"{len(todo)} neighbours to run", flush=True)
    new = {}
    with Pool(a.workers) as pool:
        for c, per in pool.imap_unordered(_job, todo):
            new[c.key()] = {"cand": asdict(c), "score": SW.selection_score(per),
                            "per": {p: per[p] for p in ("train", "validation", "test", "all")}}
    allc = {**known, **new}
    out = []
    for rank, c in enumerate(leaders, start=1):
        ns = [allc[n.key()] for n in SW.neighbours(c)]
        tv = [min(n["per"]["train"]["pnl_per_market"], n["per"]["validation"]["pnl_per_market"]) for n in ns]
        te = [n["per"]["test"]["pnl_per_market"] for n in ns]
        out.append({"rank": rank, "label": c.label(), "score": allc[c.key()]["score"], "neighbours": len(ns),
                    "positive_trainval_share": sum(1 for x in tv if x > 0) / len(tv), "positive_test_share": sum(1 for x in te if x > 0) / len(te),
                    "median_score": statistics.median(n["score"] for n in ns), "min_score": min(n["score"] for n in ns),
                    "median_all_pnl_per_market": statistics.median(n["per"]["all"]["pnl_per_market"] for n in ns),
                    "rows": [{"label": SW.Cand(**n["cand"]).label(), "score": n["score"],
                              **{p: n["per"][p]["pnl_per_market"] for p in ("train", "validation", "test", "all")}} for n in ns]})
        o = out[-1]
        print(f"#{rank} {o['neighbours']} neighbours: train&val > 0 {o['positive_trainval_share']:.0%}, test > 0 "
              f"{o['positive_test_share']:.0%}, median score {o['median_score']:+.3f} (own {o['score']:+.3f}), median all "
              f"{o['median_all_pnl_per_market']:+.3f}/mkt | {c.label()}", flush=True)
    json.dump({"leaders": out, "new_candidates": list(new.values())}, open(a.out, "w"), default=str)
    print("->", a.out)


if __name__ == "__main__":
    main()
