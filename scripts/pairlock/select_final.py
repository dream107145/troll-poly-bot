"""Parameter choice on VALIDATION only (grid_validation.json, from grid.py), with a stability check on the
neighbours, then the chosen configurations once on TEST and FORWARD (and the train split for reference).

Selection rules (written before looking at test/forward):
  * entry gate: the band and entry_edge with the best validation Sharpe-like (mean / SD per traded market) among
    settings with >= 50 traded markets; its neighbours (adjacent edge values) must not flip sign.
  * hedge layer: on the hedge grid's shared entries (max_cycles = 1), the economic setting (lam, min_hedge_edge,
    max_pair_cost) whose validation per-market P&L beats its no-hedge twin by the most, if any beats it at all;
    then the reversal settings and the action/size settings the same way. If nothing beats the twin, the layer's
    most conservative setting (no hedge pays) is kept: the strategy then trades like A, which IS the result.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
import replay as rp   # noqa: E402

G = json.load(open(f"{rp.OUT}/grid_validation.json"))


def pick_gate():
    rows = []
    for k, s in G.items():
        if "|gate|" in k and s["markets_traded"] >= 50:
            band, _, e = k.split("|")
            rows.append((s["sharpe_like"], band, float(e.split("=")[1]), s))
    rows.sort(key=lambda r: r[0], reverse=True)
    print("entry gate on validation (Sharpe-like, per market, markets):")
    for sh, band, e, s in sorted(rows, key=lambda r: (r[1], r[2])):
        print(f"  {band:6} edge {e:+.2f}: sharpe {sh:+.3f}  {s['per_market']:+.3f}/mkt (se {s['se_market']:.3f})  n {s['markets_traded']}")
    best = rows[0]
    return best[1], best[2]


def pick_hedge(stream):
    twin = G[f"{stream}|A"]["per_market"]
    econ = [(G[k]["per_market"] - twin, k) for k in G if k.startswith(f"{stream}|econ|")]
    econ.sort(reverse=True)
    print(f"\nhedge layer, {stream} stream: no-hedge twin {twin:+.3f}/mkt; B (hedge every adverse move) "
          f"{G[f'{stream}|B']['per_market'] - twin:+.3f} vs the twin")
    print("  best 8 economic settings (validation, minus the twin):")
    for d, k in econ[:8]:
        s = G[k]
        print(f"    {d:+.3f}/mkt  {k.split('|', 2)[2]:34} hedges {s['hedge_fills']}/{s['hedge_attempts']} locks {s['pair_locks']}")
    print(f"  worst: {econ[-1][0]:+.3f} ({econ[-1][1].split('|', 2)[2]}); settings beating the twin: "
          f"{sum(1 for d, _ in econ if d > 0)} of {len(econ)}")
    by_mpc = {}
    for d, k in econ:
        mpc = float(k.split("mpc=")[1])
        by_mpc.setdefault(mpc, []).append(d)
    print("  by max_pair_cost (mean over lam x edge):  " + "  ".join(f"{m:.2f}: {sum(v) / len(v):+.3f}" for m, v in sorted(by_mpc.items())))
    rev = sorted(((G[k]["per_market"] - twin, k) for k in G if k.startswith(f"{stream}|rev|")), reverse=True)
    print("  reversal settings (lam 0.02): best " + ", ".join(f"{d:+.3f} {k.split('|', 2)[2]}" for d, k in rev[:3])
          + f"; worst {rev[-1][0]:+.3f}")
    size = [(k.split("|", 2)[2], G[k]["per_market"], G[k.replace("|size|acts=1|", "|Asize|").replace("|size|acts=3|", "|Asize|")]["per_market"])
            for k in G if k.startswith(f"{stream}|size|")]
    print("  actions x size (C vs its A twin): " + "; ".join(f"{n}: {c:+.3f} vs {a:+.3f}" for n, c, a in size))
    return econ, rev


def main():
    band, edge = pick_gate()
    lo, hi = (0.20, 0.96) if band == "wide" else (0.45, 0.55)
    print(f"\nchosen entry gate: band {lo:.2f}-{hi:.2f}, entry_edge {edge:+.2f}")
    econ_w, rev_w = pick_hedge("wide")
    econ_n, rev_n = pick_hedge("narrow")
    stream_econ = econ_w if band == "wide" else econ_n
    best_d, best_k = stream_econ[0]
    parts = dict(p.split("=") for p in best_k.split("|")[2:])
    hedge = {"lam": float(parts["lam"]), "min_hedge_edge": float(parts["edge"]), "max_pair_cost": float(parts["mpc"])}
    if best_d <= 0:
        print("\nNO hedge setting beats holding on validation: the layer keeps its most conservative setting "
              "(pure EV, min_hedge_edge 0.02): it hedges only when the model prices B above its ask by 2c a share")
        hedge = {"lam": 0.0, "min_hedge_edge": 0.02, "max_pair_cost": 0.95}
    print(f"chosen hedge setting: {hedge}")
    base = {"entry_edge": edge, "band_lo": lo, "band_hi": hi}
    jobs = [("BASE", {"band_lo": lo, "band_hi": hi}), ("A", base), ("B", {**base, "unconditional": True}),
            ("C", {**base, **hedge})]
    # neighbours of the chosen gate and hedge, for stability on the held-out splits
    for de in (-0.02, 0.02):
        jobs.append(("A", {**base, "entry_edge": round(edge + de, 2)}))
    for mpc in (hedge["max_pair_cost"] - 0.01, hedge["max_pair_cost"] + 0.01):
        jobs.append(("C", {**base, **hedge, "max_pair_cost": round(mpc, 2)}))
    res = rp.batch(jobs, procs=4)
    out = []
    for name, ov, r in res:
        tag = name + " " + ",".join(f"{k}={v}" for k, v in ov.items())
        print(f"\n### {tag}")
        for s, summ in r.items():
            print(rp.fmt(summ))
            out.append({"name": name, "overrides": ov, "split": s, **summ})
    json.dump({"gate": {"band": band, "edge": edge}, "hedge": hedge, "rows": out},
              open(f"{rp.OUT}/final.json", "w"), indent=1, default=str)


main()
