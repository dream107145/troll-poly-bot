import json, sys
sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
import replay as rp

NG = {"entry_edge": -1.0}
WIDE = {"band_lo": 0.20, "band_hi": 0.96}
jobs = [("BASE", {}), ("A_nogate", {}), ("A", {}), ("B", {}), ("C", {}),
        ("B", NG), ("C", NG),
        ("A_nogate", WIDE), ("A", WIDE), ("B", {**NG, **WIDE}), ("C", {**NG, **WIDE}), ("BASE", WIDE)]
res = rp.batch(jobs, procs=5)
rows = []
for name, ov, out in res:
    tag = name + ("" if not ov else " " + ",".join(f"{k}={v}" for k, v in ov.items()))
    print(f"\n### {tag}")
    for s, summ in out.items():
        print(rp.fmt(summ))
        rows.append({"name": name, "overrides": ov, "split": s, **{k: v for k, v in summ.items() if k != "_recs"}})
json.dump(rows, open(f"{rp.OUT}/compare.json", "w"), indent=1, default=str)
