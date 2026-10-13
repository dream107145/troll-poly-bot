"""Sanity checks on the CHEAPSIDE backtest (scripts/research_cheapside.py).

    ionice -c3 nice -n 19 python scripts/research_cheapside_checks.py > results/cheapside_checks.md

1. Where is the edge? The actual win rate of the cheap side against its ask, by time
   left x ask bucket, on EVERY candidate (no model, no execution). If the cheap side
   wins far more than its price says only in the first minute, the source is the
   market's pricing at the open, not the model.
2. Is the model needed? The same execution simulation with the model replaced by
   "always eligible" (buy the first supported cheap ask in the band), and with a
   time-only rule (>= 240 s left). Selection on train / validation; test and forward
   reported untouched.
3. Are the fills believable? For each simulated fill, how long the cheap ask stayed at
   or under the limit after the order's arrival (a quote that vanishes at once was a
   ghost of the local book, not a fill).
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import research_cheapside as R                                            # noqa: E402
from troll_poly_bot.backtest.spot_shock_replay import Execution, load_asset, windows   # noqa: E402

LEFT_B = ((240, 300), (180, 240), (120, 180), (60, 120), (15, 60))


def main() -> None:
    import json
    strikes = {s: float(r.get("strike") or 0.0) for s, r in json.loads(R.INDEX.read_text())["rows"].items()}
    model = json.loads(R.MODEL_OUT.read_text())
    cands = defaultdict(list)
    for asset in R.ASSETS:
        d, meta = load_asset(R.CACHE, asset)
        span = {str(row[0]): (int(row[6]), int(row[7])) for row in meta}
        for w in windows(d, meta):
            s, e = span[w.slug]
            w.extra = {"p": d["p"][s:e], "tfi": np.nan_to_num(d["tfi"][s:e]), "obi": np.nan_to_num(d["obi"][s:e])}
            per = R.PERIOD.get(w.split)
            if per is None or w.won_up is None:
                continue
            c = R.candidates(w, strikes.get(w.slug, 0.0))
            if c is not None:
                cands[per].append((asset, w, c))
        del d
        print(f"… {asset}", file=sys.stderr)

    print("# CHEAPSIDE sanity checks\n")
    print("## 1. Cheap side's actual win rate vs its ask, by time left and ask (all candidates, one row per book update)\n")
    print("Cells: actual win rate − mean ask, in cents (positive = the cheap side wins more often than its price says); n in brackets.\n")
    for per in R.PERIODS:
        rows = cands[per]
        if not rows:
            continue
        left = np.concatenate([c["left"] for _, _, c in rows]); ask = np.concatenate([c["ask"] for _, _, c in rows])
        won = np.concatenate([c["won"] for _, _, c in rows]); sup = np.concatenate([c["support"] for _, _, c in rows])
        print(f"**{per}** ({len(won):,} candidates)\n")
        print("| time left | " + " | ".join(f"${lo:.2f}–{hi:.2f}" for lo, hi in R.PRICE_BUCKETS) + " | all |")
        print("|---|" + "---:|" * (len(R.PRICE_BUCKETS) + 1))
        for lo, hi in LEFT_B:
            m0 = (left >= lo) & (left < hi)
            cells = []
            for plo, phi in R.PRICE_BUCKETS + ((0.05, 0.35),):
                m = m0 & (ask >= plo - 1e-9) & (ask < phi + (1e-9 if phi == 0.35 else 0))
                cells.append(f"{(won[m].mean() - ask[m].mean()) * 100:+.1f}c ({m.sum():,})" if m.sum() else "—")
            print(f"| {lo}–{hi} s | " + " | ".join(cells) + " |")
        m0 = (left >= 240) & sup
        print(f"\nFirst minute, spot-supported only: {(won[m0].mean() - ask[m0].mean()) * 100:+.1f}c over {m0.sum():,} "
              f"updates (win {won[m0].mean():.3f} vs ask {ask[m0].mean():.3f}).\n")

    print("## 2. The same execution with and without the model (3 latency draws, net $ per period)\n")
    print("| rule | " + " | ".join(R.PERIODS) + " |")
    print("|---|" + "---:|" * len(R.PERIODS))
    rules = {
        "model, edge >= 5c (the strategy)": lambda c: R.predict(model, c["X"]),
        "no model: every supported cheap ask": lambda c: np.ones(len(c["idx"])),
        "no model, first minute only (>= 240 s left)": lambda c: np.where(c["left"] >= 240, 1.0, 0.0),
        "no model, ask >= 0.20, first minute": lambda c: np.where((c["left"] >= 240) & (c["ask"] >= 0.20), 1.0, 0.0),
        "model, edge >= 5c, first minute only": lambda c: np.where(c["left"] >= 240, R.predict(model, c["X"]), 0.0),
    }
    persist = defaultdict(list)
    for name, pf in rules.items():
        cells = []
        for per in R.PERIODS:
            nets, fills_n, orders_n = [], 0, 0
            for draw in range(3):
                ex = Execution(seed=draw)
                for asset, w, c in cands[per]:
                    # a model-free rule marks its candidates with p = 1 (eligible at any bar) or 0
                    r = R.simulate(w, c, pf(c), ex, 0.05 if name.startswith("model") else 0.0)
                    for o in r["orders"]:
                        orders_n += 1
                        if o["filled"]:
                            fills_n += 1
                            nets.append(o["net"])
                            if name.startswith("model, edge >= 5c (the"):
                                # how long the ask stayed at or under the limit after arrival
                                sv = w.side[o["side"]]
                                j = w.first_at(o["t"] + 500.0)      # ~the arrival read
                                k = j
                                while k < len(w) and sv["ask"][k] == sv["ask"][k] and sv["ask"][k] <= o["limit"] + 1e-9:
                                    k += 1
                                persist[per].append((w.t[min(k, len(w) - 1)] - w.t[j]) / 1000.0)
            n = len(nets)
            cells.append(f"{sum(nets) / 3:+.0f} ({fills_n // 3} fills, {100 * fills_n / max(orders_n, 1):.0f}%)" if n else "—")
        print(f"| {name} | " + " | ".join(cells) + " |")

    print("\n## 2b. The strategy on the forward period, by UTC day (3 draws pooled; net $ and fills)\n")
    from datetime import datetime, timezone
    byday = defaultdict(lambda: [0.0, 0, 0])
    for draw in range(3):
        ex = Execution(seed=draw)
        for asset, w, c in cands["forward"]:
            r = R.simulate(w, c, R.predict(model, c["X"]), ex, 0.05)
            for o in r["orders"]:
                day = datetime.fromtimestamp(o["t"] / 1000, timezone.utc).strftime("%m-%d")
                byday[day][1] += 1
                if o["filled"]:
                    byday[day][0] += o["net"]; byday[day][2] += 1
    print("| day (UTC) | orders | fills | net $ | net/fill |")
    print("|---|---:|---:|---:|---:|")
    for day in sorted(byday):
        net, n_o, n_f = byday[day]
        print(f"| {day} | {n_o // 3} | {n_f // 3} | {net / 3:+.0f} | {net / n_f:+.2f} |" if n_f else f"| {day} | {n_o // 3} | 0 | — | — |")

    print("\n## 3. How long the filled ask persisted after the order's arrival read (strategy fills)\n")
    print("| period | fills | median s | p25 s | share gone within 1 s | share still there after 5 s |")
    print("|---|---:|---:|---:|---:|---:|")
    for per in R.PERIODS:
        v = np.array(persist[per])
        if len(v):
            print(f"| {per} | {len(v)} | {np.median(v):.1f} | {np.percentile(v, 25):.1f} | {100 * np.mean(v < 1.0):.0f}% | {100 * np.mean(v >= 5.0):.0f}% |")


if __name__ == "__main__":
    main()
