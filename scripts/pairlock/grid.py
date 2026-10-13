"""Validation grid for strategy C (and the entry gate), then the chosen settings and their neighbours on TEST and
FORWARD. Hedge variants use max_cycles_per_market = 1 so they share their no-hedge twin's entries exactly."""
from __future__ import annotations

import itertools
import json
import sys

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
import replay as rp   # noqa: E402

STREAMS = {"wide": {"entry_edge": -1.0, "band_lo": 0.20, "band_hi": 0.96, "max_cycles_per_market": 1},
           "narrow": {"entry_edge": -1.0, "band_lo": 0.45, "band_hi": 0.55, "max_cycles_per_market": 1}}


def main():
    out = {}
    jobs = []
    for sname, base in STREAMS.items():
        jobs.append(("A", {**base, "_tag": f"{sname}|A"}))
        jobs.append(("B", {**base, "unconditional": True, "_tag": f"{sname}|B"}))
        for lam, edge, mpc in itertools.product((0.0, 0.005, 0.02, 0.1), (0.0, 0.01, 0.02),
                                                (0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99)):
            jobs.append(("C", {**base, "lam": lam, "min_hedge_edge": edge, "max_pair_cost": mpc,
                               "_tag": f"{sname}|econ|lam={lam}|edge={edge}|mpc={mpc}"}))
        for thr, conf, adv in itertools.product((0.5, 1.0, 1.5), (0.0, 400.0, 1000.0), (0.03, 0.05)):
            jobs.append(("C", {**base, "lam": 0.02, "rev_thr": thr, "confirm_ms": conf, "adverse_min": adv,
                               "_tag": f"{sname}|rev|thr={thr}|conf={conf}|adv={adv}"}))
        for acts, sh in itertools.product((1, 3), (5.0, 10.0)):
            jobs.append(("C", {**base, "lam": 0.02, "lim_max_hedge_actions": acts, "entry_shares": sh,
                               "_tag": f"{sname}|size|acts={acts}|shares={sh}"}))
            jobs.append(("A", {**base, "entry_shares": sh, "_tag": f"{sname}|Asize|shares={sh}"}))
    # the entry gate (strategy A), both bands
    for sname, (lo, hi) in (("wide", (0.20, 0.96)), ("narrow", (0.45, 0.55))):
        for edge in (-1.0, -0.06, -0.04, -0.02, 0.0, 0.02):
            jobs.append(("A", {"entry_edge": edge, "band_lo": lo, "band_hi": hi, "_tag": f"{sname}|gate|edge={edge}"}))
    clean = [(n, {k: v for k, v in o.items() if k != "_tag"}) for n, o in jobs]
    tags = [o["_tag"] for _, o in jobs]
    res = rp.batch(clean, splits_=["validation"], procs=5)
    for tag, (_, ov, r) in zip(tags, res):
        s = r["validation"]
        out[tag] = {k: v for k, v in s.items() if k != "_recs"}
    json.dump(out, open(f"{rp.OUT}/grid_validation.json", "w"), indent=1, default=str)
    for tag, s in out.items():
        print(f"{tag:58} mkts {s['markets_traded']:4} hedges {s['hedge_fills']:3}/{s['hedge_attempts']:3} "
              f"locks {s['pair_locks']:3} full {s['full_hedges']:3} | net {s['net']:+8.2f} {s['per_market']:+.3f}/mkt "
              f"se {s['se_market']:.3f} sharpe {s['sharpe_like']:+.3f} DD {s['max_drawdown']:+.2f} p5 {s['tail_p05']:+.2f}")


main()
