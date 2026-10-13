"""Execution-cost components on TRAIN per asset (every |z1|>=2 candidate in 0.15-0.85, FOK ask+2c, 5 sh, hold):
   slippage      fill - decision ask  (latency part: ask at landing - decision ask; survival part: the rest)
   adverse sel.  fair at decision - win rate of FILLED orders, minus the same for ALL candidates
   exit cost     bid-side round trip: (ask - bid)/2 + taker fee at the bid (what an early exit gives up)"""
import json, sys
import numpy as np
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L, run as R, sim as S

def main():
    out = {}
    print("| asset | candidates | fill % | slippage c | latency part c | adverse selection c | fee at fill c | half spread + exit fee c | total entry cost c |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for a in L.ASSETS:
        wins = R.load(a)
        ex = L.Exec()
        sl, lat, fair_f, won_f, fair_a, won_a, fees, exitc = [], [], [], [], [], [], [], []
        n = 0
        for w in wins:
            if w.split != "train":
                continue
            for i, d in S.candidates(w, 2.0, 60, 290):
                side = "UP" if d > 0 else "DOWN"
                ask = (w.ua if d > 0 else w.da)[i]; bid = (w.ub if d > 0 else w.db)[i]
                if not (0.15 <= ask <= 0.85) or np.isnan(bid):
                    continue
                n += 1
                fair = w.fair_up[i] if d > 0 else 1 - w.fair_up[i]
                won = 1.0 if w.outcome == side else 0.0
                fair_a.append(fair); won_a.append(won)
                px, known = ex.taker_buy(w, side, w.T[i], round(ask + 0.02, 2))
                exitc.append((ask - bid) / 2 + L.fee(bid))
                if px is None:
                    continue
                j = w.ix(w.T[i] + ex.lat)
                a_land = (w.ua if d > 0 else w.da)[j]
                sl.append(px - ask); lat.append(a_land - ask); fair_f.append(fair); won_f.append(won); fees.append(L.fee(px))
        adv = (np.mean(fair_f) - np.mean(won_f)) - (np.mean(fair_a) - np.mean(won_a))
        rec = {"candidates": n, "fill_rate": len(sl) / n, "slippage": float(np.mean(sl)), "latency": float(np.nanmean(lat)),
               "adverse": float(adv), "fee": float(np.mean(fees)), "exit": float(np.mean(exitc))}
        rec["entry_cost"] = rec["slippage"] + max(rec["adverse"], 0.0)
        out[a] = rec
        print(f"| {a} | {n} | {rec['fill_rate']:.0%} | {100*rec['slippage']:+.2f} | {100*rec['latency']:+.2f} | {100*adv:+.2f} | "
              f"{100*rec['fee']:.2f} | {100*rec['exit']:.2f} | {100*rec['entry_cost']:+.2f} |")
    json.dump(out, open(f"{L.CACHE}/costs_train.json", "w"), indent=1)

main()
