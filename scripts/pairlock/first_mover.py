"""Item 3: which exchange gives the earliest reliable signal? On the tick recordings (data/ticks), every flow
signal (strategy/flowsignal.py, the live settings) records its first mover; for each first mover: how often,
and whether the BTC price kept moving the signal's way 0.5 / 1 / 2 s later. Also each exchange's delivery
lag (receipt - exchange time) where the exchange stamps its messages."""
from __future__ import annotations

import bisect
import gzip
import json
import math
import statistics
import sys
from collections import defaultdict

sys.path.insert(0, "/root/troll-poly-bot/src")
from troll_poly_bot.strategy.flowsignal import FlowParams, FlowSignal   # noqa: E402

NAMES = {"cb": "coinbase", "by": "bybit", "bn": "binance"}
P = FlowParams(ref_bp=0.6, accel_ref_bp=0.6, min_bp=0.2, gate_bp=0.15)


def run(path):
    ev, lag = [], defaultdict(list)
    with gzip.open(path, "rt") as f:
        for line in f:
            if '"BTC"' not in line:
                continue
            r = json.loads(line)
            if r.get("s") != "BTC" or r.get("x") not in NAMES or r.get("k") not in ("q", "t"):
                continue
            ev.append(r)
            if r.get("et"):
                lag[(r["x"], r["k"])].append(r["rt"] - r["et"])
    ev.sort(key=lambda r: r["rt"])
    tl = {x: statistics.median(v) for (x, k), v in lag.items() if k == "t"}
    fs = FlowSignal(P)
    series = {x: ([], []) for x in NAMES}
    sigs = []
    for r in ev:
        x, rt = r["x"], r["rt"]
        ti = r["et"] if r.get("et") and abs(rt - r["et"]) < 30_000 else rt - tl.get(x, 0.0)
        if r["k"] == "q":
            if not r.get("b") or not r.get("a"):
                continue
            series[x][0].append(rt)
            series[x][1].append(0.5 * (r["b"] + r["a"]))
            res = fs.on_quote("BTC", NAMES[x], r["b"], r["a"], ti, rt)
        else:
            res = fs.on_trade("BTC", NAMES[x], r["p"] * r["q"], r.get("side") == 1, ti, rt)
        if res is not None:
            sigs.append((rt, res))

    def mid(x, t):
        ts, ms = series[x]
        i = bisect.bisect_right(ts, t) - 1
        return ms[i] if i >= 0 else None

    by = defaultdict(lambda: defaultdict(list))
    for rt, res in sigs:
        for h in (500, 1000, 2000):
            v = [(mid(x, rt + h) / mid(x, rt) - 1) * 1e4 for x in NAMES if mid(x, rt) and mid(x, rt + h)]
            if v:
                by[res.first_mover or "none"][h].append(res.dir * statistics.mean(v))
    print(f"{path.split('/')[-1]}: {len(sigs)} flow signals")
    print("  delivery lag p50 (receipt - exchange stamp), ms:",
          {f"{x}:{k}": round(statistics.median(v)) for (x, k), v in sorted(lag.items())})
    for fm, d in sorted(by.items(), key=lambda kv: -len(kv[1][1000])):
        n = len(d[1000])
        cells = []
        for h in (500, 1000, 2000):
            v = d[h]
            m = statistics.mean(v) if v else float("nan")
            se = statistics.pstdev(v) / math.sqrt(len(v)) if len(v) > 1 else float("nan")
            hit = sum(1 for z in v if z > 0) / len(v) if v else float("nan")
            cells.append(f"+{h / 1000:g}s {m:+.2f}bp(+/-{se:.2f}) hit {hit:.0%}")
        print(f"  first mover {fm:9} {n:4} signals ({n / max(1, len(sigs)):.0%}) | " + " | ".join(cells))


for p in sys.argv[1:]:
    run(p)
