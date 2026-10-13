"""Leg-1 performance by seconds left and by entry price, per chronological split (chart_replay's data)."""
from __future__ import annotations

import sys
from collections import defaultdict

sys.path.insert(0, "/root/troll-poly-bot/scripts/triangle_audit")
import chart_replay as cr   # noqa: E402

wins = [cr.W(*x) for x in cr.load()]
n = len(wins)
for trig_name, trig in (("HIGH ZONE", cr.high_zone), ("SPOT SHOCK proxy", cr.spot_shock)):
    print(f"\n===== {trig_name}: leg 1 only, by seconds left at the decision =====")
    for sname, lo, hi in cr.SPLITS:
        ws = wins[int(lo * n):int(hi * n)]
        by = defaultdict(list)
        byp = defaultdict(list)
        for w in ws:
            for r in cr.simulate(w, trig(w), 1.0, "none"):
                if not r["filled"]:
                    continue
                left = (w.close - r["t"]) / 1000.0
                b = "240-300" if left >= 240 else "180-240" if left >= 180 else "120-180" if left >= 120 else "60-120" if left >= 60 else "10-60"
                by[b].append(r["leg1"])
                p = r["fill"]
                byp["<0.5" if p < 0.5 else "0.5-0.8" if p < 0.8 else "0.8-0.9" if p < 0.9 else "0.9-0.96"].append(r["leg1"])
        line = f"  {sname:11}"
        for b in ("240-300", "180-240", "120-180", "60-120", "10-60"):
            v = by.get(b, [])
            line += f" | {b}s n{len(v):5} {sum(v) / len(v) if v else 0:+.3f}" if v else f" | {b}s     -       "
        print(line)
        line = f"  {'':11} by fill price:"
        for b in ("<0.5", "0.5-0.8", "0.8-0.9", "0.9-0.96"):
            v = byp.get(b, [])
            line += f" | {b} n{len(v):5} {sum(v) / len(v) if v else 0:+.3f} win {sum(1 for x in v if x > 0) / len(v) * 100 if v else 0:.0f}%" if v else f" | {b} -"
        print(line)
