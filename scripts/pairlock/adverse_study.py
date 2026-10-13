"""Item 19: at every adverse (and favourable) event after a first-leg fill, is buying the opposite token better
than holding? The B leg is simulated with the realistic fill model; its value per share is reported marked at
B's bid 1 / 5 / 10 s after the fill is known and at settlement. NO_HEDGE = 0, PARTIAL = 0.5 x, FULL = 1 x per
unmatched share (P&L is linear in the hedge quantity). First legs: every momentum trigger, band 0.20-0.96."""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import replace

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
import replay as rp   # noqa: E402
from troll_poly_bot.strategy import pairlock as pl   # noqa: E402

HS = (1000.0, 5000.0, 10000.0)


def b_leg(w, i, side_b, q=10.0):
    ask_b, _, _ = rp.book(w, side_b, i)
    if ask_b is None:
        return None
    st, px, tk = rp.fok(w, i, side_b, q, min(0.99, round(ask_b + 0.03, 2)))
    if st != "filled":
        return {"filled": False, "status": st}
    fee = pl.fee_per_share(px)
    out = {"filled": True, "px": px, "settle": (1.0 if w.outcome == side_b else 0.0) - px - fee}
    for h in HS:
        j = int(np.searchsorted(w.t, tk + h))
        if j >= w.i1:
            out[h] = out["settle"] if j >= len(w.t) or w.t[min(j, len(w.t) - 1)] >= w.close else None
            continue
        _, bid, _ = rp.book(w, side_b, j)
        out[h] = None if bid is None else bid - px - fee
    return out


def main():
    ws = rp.load_cache()
    sp = rp.split(ws)
    model = pl.ProbModel.load(rp.MODEL_PATH)
    prm = replace(pl.PairLockParams(), entry_edge=-1.0, band_lo=0.20, band_hi=0.96, max_cycles_per_market=1)
    lim = pl.RiskLimits()
    res = {}
    for sname, wss in sp.items():
        rows = []
        for w in wss:
            if w.trig is None or w.trig[0] != 2.5:
                w.trig = (2.5, rp.triggers(w, 2.5))
            r = rp.run_window(w, "A", prm, lim, model, w.trig[1])
            for e in r["entry_log"]:
                side_a = e["side"]
                side_b = "DOWN" if side_a == "UP" else "UP"
                i_from = int(np.searchsorted(w.t, e["t_known"]))
                fee_a = pl.fee_per_share(e["px"])
                for kind, X in (("adverse", 0.03), ("adverse", 0.05), ("adverse", 0.10), ("favourable", 0.96),
                                ("favourable", 0.98)):
                    for i in range(i_from, w.i1):
                        left = (w.close - w.t[i]) / 1000.0
                        if left < 10.0:
                            break
                        if kind == "adverse":
                            _, bid_a, _ = rp.book(w, side_a, i)
                            hit = bid_a is not None and bid_a <= e["px"] - X + 1e-9
                        else:
                            ask_b, _, _ = rp.book(w, side_b, i)
                            hit = ask_b is not None and e["px"] + fee_a + ask_b + pl.fee_per_share(ask_b) <= X + 1e-9
                        if not hit:
                            continue
                        f = rp.feat_at(w, i)
                        score = pl.reversal_score(f, side_a, model.rev_scales) if f else None
                        leg = b_leg(w, i, side_b)
                        if leg is None:
                            break
                        rows.append({"kind": kind, "X": X, "left": left, "score": score,
                                     "a_settle": e["win"] - e["px"] - fee_a,
                                     "p_b": None if f is None else 1.0 - model.p_side(f, side_a), **leg})
                        break
        res[sname] = rows

    def stat(v):
        v = [x for x in v if x is not None]
        if len(v) < 2:
            return f"n {len(v):4}"
        m, se = statistics.mean(v), statistics.pstdev(v) / math.sqrt(len(v))
        return f"{m:+.3f}+/-{se:.3f} (n {len(v)})"

    print("Per share of the opposite token B bought at the event (FULL hedge = x1 per unmatched share, PARTIAL 50 % = x0.5,"
          " NO_HEDGE = 0). Positive = the hedge adds value.\n")
    for kind, X in (("adverse", 0.03), ("adverse", 0.05), ("adverse", 0.10), ("favourable", 0.96), ("favourable", 0.98)):
        lab = f"{kind} {'bid fell ' + format(X, '.2f') if kind == 'adverse' else 'pair cost <= ' + format(X, '.2f')}"
        print(f"== {lab}")
        for sname, rows in res.items():
            rr = [r for r in rows if r["kind"] == kind and r["X"] == X]
            fl = [r for r in rr if r["filled"]]
            print(f"  {sname:10} events {len(rr):4} hedge fills {len(fl):4} ({len(fl) / max(1, len(rr)):.0%}) | "
                  + " | ".join(f"+{int(h / 1000)}s {stat([r.get(h) for r in fl])}" for h in HS)
                  + f" | settle {stat([r['settle'] for r in fl])}")
        # by time left and reversal score, all out-of-sample splits pooled
        oos = [r for s in ("validation", "test", "forward") for r in res[s] if r["kind"] == kind and r["X"] == X and r["filled"]]
        for lo, hi in ((240, 300), (120, 240), (60, 120), (30, 60), (10, 30)):
            v = [r["settle"] for r in oos if lo <= r["left"] < hi]
            if v:
                print(f"     OOS {lo:3}-{hi:3} s left: settle {stat(v)}")
        if kind == "adverse":
            for lo, hi in ((-9, 0), (0, 1), (1, 9)):
                v = [r["settle"] for r in oos if r["score"] is not None and lo <= r["score"] < hi]
                if v:
                    print(f"     OOS reversal score {lo:+d}..{hi:+d}: settle {stat(v)}")
            v = [r["settle"] for r in oos if r["p_b"] is not None and r["p_b"] - r["px"] - pl.fee_per_share(r["px"]) > 0]
            print(f"     OOS where the model said B underpriced: settle {stat(v)}")
    json.dump(res, open(f"{rp.OUT}/adverse_study.json", "w"), default=str)


main()
