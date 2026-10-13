"""TARGET-PROFIT RECOVERY SIZING (the user, 2026-10-08) on the recorded BTC 5-minute markets: TARGET_RECOVERY_PROFIT
1..5 USDC against the 2026-10-07 fixed 24 / 80, with the bot's own decisions (pairlock_swing, sample by sample:
replay_swing.run_window) and the realistic fill model (replay_swing.execute "realistic": arrival +143 ms, walked asks,
fill-and-kill, the 1 s survival check, fees). Same frozen data snapshot and periods as docs/pairlock_swing_sweep_2026-10-07.md.

    nice -n 10 .venv/bin/python scripts/pairlock/replay_swing_targets.py [--workers 5]
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import statistics
import sys
import time
from dataclasses import replace
from multiprocessing import Pool

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
import replay_swing as RS                                         # noqa: E402
import replay_swing_sweep as SW                                   # noqa: E402

ROOT = "/root/troll-poly-bot"
CACHE = "/tmp/claude-0/-root-troll-poly-bot/53e010f8-a35d-42fb-b329-fe76e0f4a11d/scratchpad/swing/swing_windows.pkl"
TARGET = replace(RS.BASE, swing_sizing="target", swing_max_recovery_shares=150.0, swing_max_recovery_price=0.85)
RUNS = [("fixed 24 / 80", RS.BASE)] + [(f"target {t:g}", replace(TARGET, swing_target_profit=float(t))) for t in (1, 2, 3, 4, 5)]
_WS: list = []


def _job(arg):
    label, prm, lo, hi = arg
    return label, [RS.run_window(w, "realistic", prm) for w in _WS[lo:hi]]


def summary(recs, split):
    out = {}
    for p in ("train", "validation", "test", "all"):
        rs = [r for r in recs if p == "all" or split[r["slug"]] == p]
        tr = [r for r in rs if r["traded"]]
        pnl = [r["pnl"] for r in tr]
        by = {b: [r["pnl"] for r in tr if r["bucket"] == b] for b in ("0", "1", "2", "3+")}
        eq = peak = dd = 0.0
        for x in sorted(tr, key=lambda r: r["open"]):
            eq += x["pnl"]
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        s1 = [r["s1_qty"] for r in tr if r["swings"] >= 1 and r["s1_qty"] > 0]
        s2 = [r["s2_qty"] for r in tr if r["swings"] >= 2 and r["s2_qty"] > 0]
        out[p] = {"markets": len(rs), "traded": len(tr), "total": sum(pnl), "per_market": sum(pnl) / len(rs) if rs else 0.0,
                  "win_rate": sum(1 for x in pnl if x > 0) / len(tr) if tr else 0.0, "max_dd": dd, "worst": min(pnl, default=0.0),
                  "by_swing": {b: (len(v), sum(v)) for b, v in by.items()},
                  "swing1_bought": len(s1), "swing1_reached": sum(1 for r in tr if r["swings"] >= 1), "avg_s1_qty": statistics.mean(s1) if s1 else None,
                  "avg_s1_vwap": statistics.mean(r["s1_vwap"] for r in tr if r.get("s1_vwap")) if s1 else None,
                  "swing2_bought": len(s2), "swing2_reached": sum(1 for r in tr if r["swings"] >= 2), "avg_s2_qty": statistics.mean(s2) if s2 else None,
                  "avg_s2_vwap": statistics.mean(r["s2_vwap"] for r in tr if r.get("s2_vwap")) if s2 else None,
                  "max_capital": max((r["capital"] for r in tr), default=0.0),
                  "avg_capital": statistics.mean(r["capital"] for r in tr) if tr else 0.0,
                  "hedged": sum(1 for r in tr if r["hedge_needed"])}
    return out


def main():
    global _WS
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--out", default=f"{ROOT}/results/pairlock_swing_targets_2026-10-08.json")
    a = ap.parse_args()
    with open(CACHE, "rb") as f:
        ws = pickle.load(f)
    day = lambda w: time.strftime("%Y-%m-%d", time.gmtime(w["open"] / 1000.0))      # noqa: E731
    _WS = [w for w in ws if "2026-09-22" <= day(w) <= "2026-10-07"]
    split = SW.split_of(_WS)
    chunk = max(1, len(_WS) // (a.workers * 3))
    jobs = [(lab, prm, lo, min(lo + chunk, len(_WS))) for lab, prm in RUNS for lo in range(0, len(_WS), chunk)]
    res: dict = {lab: [] for lab, _ in RUNS}
    t0 = time.time()
    with Pool(a.workers) as pool:
        for lab, recs in pool.imap_unordered(_job, jobs):
            res[lab].extend(recs)
    out = {"markets": len(_WS), "last": _WS[-1]["slug"], "seconds": round(time.time() - t0),
           "runs": {lab: summary(res[lab], split) for lab, _ in RUNS}}
    json.dump(out, open(a.out, "w"), indent=1, default=str)
    print(f"{len(_WS)} markets, {out['seconds']} s -> {a.out}")
    for lab, r in out["runs"].items():
        al = r["all"]
        print(f"{lab:14s} /mkt train {r['train']['per_market']:+.3f} val {r['validation']['per_market']:+.3f} test {r['test']['per_market']:+.3f} "
              f"all {al['per_market']:+.3f} (total {al['total']:+.0f}) win {al['win_rate']:.0%} dd {al['max_dd']:+.0f} worst {al['worst']:+.0f} | "
              f"S1 bought {al['swing1_bought']}/{al['swing1_reached']} avg {al['avg_s1_qty'] or 0:.0f} sh @ {al['avg_s1_vwap'] or 0:.3f} | "
              f"S2 bought {al['swing2_bought']}/{al['swing2_reached']} avg {al['avg_s2_qty'] or 0:.0f} sh @ {al['avg_s2_vwap'] or 0:.3f} | "
              f"cap max {al['max_capital']:.0f} avg {al['avg_capital']:.0f} | "
              + " ".join(f"S{b} {n}:{v:+.0f}" for b, (n, v) in al["by_swing"].items()), flush=True)


if __name__ == "__main__":
    main()
