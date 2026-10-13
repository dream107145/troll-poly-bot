"""Fair P(UP wins) model per asset, fitted on TRAIN only (logistic, L2, standardised features).

    nice -n 19 python scripts/paired_exit/fit_model.py > results/paired_exit_model.md

Reports, per asset and split: log loss of (a) the token mid alone (calibrated logit on TRAIN), (b) the full
model; reliability on VALIDATION; coefficients. Writes data/replay/paired_exit/fair_<asset>.json.
"""
import json
import math
import sys

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L  # noqa: E402


def samples(wins, split, step=5, lo=5.0, hi=295.0):
    X, y, mid = [], [], []
    for w in wins:
        if w.split != split:
            continue
        idx = np.arange(0, w.n, step)
        idx = idx[(w.left[idx] >= lo) & (w.left[idx] <= hi) & ~np.isnan(w.F["mid"][idx])]
        if not len(idx):
            continue
        X.append(L.design(w.F, idx)); mid.append(w.F["mid"][idx])
        y.append(np.full(len(idx), 1.0 if w.outcome == "UP" else 0.0))
    return np.vstack(X), np.concatenate(y), np.concatenate(mid)


def ll(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def main():
    print("# Fair-probability model P(UP wins) per asset (fitted on TRAIN only)\n")
    print("Log loss per sample (every 1 s, 5-295 s left). `mid` = the token mid as the probability; "
          "`mid-fit` = logistic on logit(mid) alone (TRAIN); `model` = all features. Lower is better.\n")
    print("| asset | split | samples | mid | mid-fit | model | model - mid-fit |")
    print("|---|---|---:|---:|---:|---:|---:|")
    coefs = {}
    rel = {}
    for asset in L.ASSETS:
        wins = L.prepare(L.load_asset(asset))
        Xtr, ytr, _ = samples(wins, "train")
        model = L.fit_logistic(Xtr, ytr, l2=50.0)
        base = L.fit_logistic(Xtr[:, [0]], ytr, l2=1.0)
        model["asset"] = asset
        model["train_windows"] = sum(1 for w in wins if w.split == "train")
        model["train_to"] = max(w.open for w in wins if w.split == "train")
        json.dump(model, open(f"{L.CACHE}/fair_{asset.lower()}.json", "w"), indent=1)
        coefs[asset] = model["w"]
        for split in ("train", "validation", "test", "forward"):
            X, y, mid = samples(wins, split)
            p = L.predict(model, X)
            pb = L.predict(base, X[:, [0]])
            a, b, c = ll(mid, y), ll(pb, y), ll(p, y)
            print(f"| {asset} | {split} | {len(y)} | {a:.4f} | {b:.4f} | {c:.4f} | {c - b:+.4f} |")
            if split == "validation":
                bins = np.linspace(0, 1, 11)
                k = np.clip(np.digitize(p, bins) - 1, 0, 9)
                rel[asset] = [(float(p[k == i].mean()), float(y[k == i].mean()), int((k == i).sum())) for i in range(10) if (k == i).sum() > 50]
    print("\n## Standardised coefficients (TRAIN)\n")
    print("| feature | " + " | ".join(L.ASSETS) + " |")
    print("|---|" + "---:|" * len(L.ASSETS))
    for i, f in enumerate(("intercept",) + L.FEATS):
        print(f"| {f} | " + " | ".join(f"{coefs[a][i]:+.3f}" for a in L.ASSETS) + " |")
    print("\n## Reliability on VALIDATION (mean predicted -> observed UP rate, n)\n")
    for a, r in rel.items():
        print(f"- {a}: " + ", ".join(f"{p:.2f}->{o:.2f} ({n})" for p, o, n in r))


if __name__ == "__main__":
    main()
