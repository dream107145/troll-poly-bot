"""Selection on a grid: TRAIN optimises (top-K by TRAIN t-stat of window P&L, >= min fills), VALIDATION
selects (best validation $/window among them), TEST / FORWARD are reported untouched."""
from __future__ import annotations

import json
import math
import sys

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L   # noqa: E402
import run as R      # noqa: E402


def load(stage):
    return json.load(open(f"{L.CACHE}/grids/{stage}.json"))


def tval(m):
    v = m.get("t")
    return -1e9 if v is None or (isinstance(v, float) and math.isnan(v)) else v


def choose(recs, k=10, min_fills=80, key_train=tval, key_val=lambda m: m["per_window"]):
    ok = [r for r in recs if r["splits"]["train"]["fills"] >= min_fills]
    if not ok:
        return None, []
    top = sorted(ok, key=lambda r: key_train(r["splits"]["train"]), reverse=True)[:k]
    best = max(top, key=lambda r: key_val(r["splits"]["validation"]))
    return best, top


def table(rows, label_of=lambda r: r["name"]):
    out = [R.head()]
    for r in rows:
        for s in R.SPLIT_NAMES:
            out.append(R.row(label_of(r), s, r["splits"][s]))
    return "\n".join(out)


def by_asset(recs):
    d = {}
    for r in recs:
        d.setdefault(r["asset"], []).append(r)
    return d


if __name__ == "__main__":
    stage = sys.argv[1]
    recs = load(stage)
    for asset, rs in by_asset(recs).items():
        best, top = choose(rs)
        print(f"\n### {asset}: chosen {best['name'] if best else None}\n")
        if best:
            print(table([best], lambda r: f"{asset} {r['name']}"))
