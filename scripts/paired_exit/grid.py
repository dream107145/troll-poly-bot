"""Run a list of Params on every asset in parallel; save metrics per (asset, config, split).

    from grid import run_grid
    run_grid("stage1", configs, assets)
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L   # noqa: E402
import run as R      # noqa: E402

OUT = f"{L.CACHE}/grids"


def _work(args):
    asset, configs, exec_kw, keep_legs = args
    os.nice(19) if os.getpid() else None
    wins = R.load(asset)
    out = []
    ex_proto = exec_kw or {}
    costs = json.load(open(f"{L.CACHE}/costs_train.json"))
    from dataclasses import replace
    for p in configs:
        if p.cost_c < 0:            # sentinel: this asset's measured TRAIN entry cost
            p = replace(p, cost_c=round(costs[asset]["entry_cost"], 4))
        ex = L.Exec(**ex_proto)
        res = R.simulate(wins, p, ex)
        rec = {"asset": asset, "name": p.name, "params": asdict(p), "splits": {}}
        for s, (pn, legs, n) in res.items():
            rec["splits"][s] = R.metrics(pn, legs, n)
            if keep_legs:
                rec.setdefault("legs", {})[s] = [{k: v for k, v in r.items() if k != "exits"} | {
                    "exits": [list(e) for e in r.get("exits", [])]} for r in legs]
                rec.setdefault("pn", {})[s] = pn.tolist()
        out.append(rec)
    return out


def run_grid(stage, configs, assets=L.ASSETS, exec_kw=None, workers=4, keep_legs=False):
    os.makedirs(OUT, exist_ok=True)
    t = time.time()
    jobs = [(a, configs[a] if isinstance(configs, dict) else configs, exec_kw, keep_legs) for a in assets
            if not isinstance(configs, dict) or a in configs]
    res = []
    with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as ex:
        for part in ex.map(_work, jobs):
            res.extend(part)
    json.dump(res, open(f"{OUT}/{stage}.json", "w"))
    print(f"{stage}: {sum(len(j[1]) for j in jobs)} runs over {len(jobs)} assets in {time.time() - t:.0f} s", file=sys.stderr)
    return res
