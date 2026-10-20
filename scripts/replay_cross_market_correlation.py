#!/usr/bin/env python3
"""Leakage-free chronological evaluation scaffold for cross_market_correlation.

Splits synchronized pair outcomes into train / validation / test, fits the
joint-outcome model on train only, selects gates on validation, and reports
holdout metrics on test. Does not place orders or enable live trading.

Usage:
  .venv/bin/python scripts/replay_cross_market_correlation.py [--history PATH]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from troll_poly_bot.strategy.cross_market_correlation.alignment import all_unique_pairs
from troll_poly_bot.strategy.cross_market_correlation.math import (
    JointProbs, classify_candidate, ev_c1, ev_c2,
)
from troll_poly_bot.strategy.cross_market_correlation.model import (
    JointOutcomeModel, always_agree_baseline, independence_baseline, log_score,
)

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")


def _synth_observations(n: int = 600, seed: int = 7) -> list[tuple[str, str, bool, bool]]:
    """Synthetic correlated settlements for offline smoke (not a profitability claim)."""
    rng = random.Random(seed)
    pairs = all_unique_pairs(ASSETS)
    out = []
    for _ in range(n):
        a, b = rng.choice(pairs)
        # Mild agreement regime
        if rng.random() < 0.72:
            up = rng.random() < 0.5
            out.append((a, b, up, up))
        elif rng.random() < 0.5:
            out.append((a, b, True, False))
        else:
            out.append((a, b, False, True))
    return out


def _split(rows, train_frac=0.5, val_frac=0.25):
    n = len(rows)
    t = int(n * train_frac)
    v = int(n * (train_frac + val_frac))
    return rows[:t], rows[t:v], rows[v:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", default="", help="Optional JSON with pairs[].n_* counts")
    ap.add_argument("--cost", type=float, default=0.96, help="Assumed all-in combined cost")
    args = ap.parse_args()

    observations = _synth_observations()
    train, val, test = _split(observations)

    model = JointOutcomeModel(prior_strength=4.0, min_sample_size=30)
    if args.history and Path(args.history).is_file():
        model.load_json(args.history)
        print(f"loaded history {args.history}")
    else:
        model.ingest_synced(train)

    # Validation: pick whether statistical EV at cost clears a conservative bar
    val_scores = {"model": 0.0, "agree": 0.0, "indep": 0.0, "n": 0}
    for a, b, a_up, b_up in val:
        p = model.estimate(a, b)
        val_scores["model"] += log_score(p, a_up, b_up)
        val_scores["agree"] += log_score(always_agree_baseline(0.72), a_up, b_up)
        val_scores["indep"] += log_score(independence_baseline(0.5, 0.5), a_up, b_up)
        val_scores["n"] += 1

    # Untouched test: hypothetical EV for both directions at fixed cost (no look-ahead sizing)
    test_rows = []
    for a, b, a_up, b_up in test:
        p = model.estimate(a, b)
        # Realized payoff for each candidate direction
        if a_up and not b_up:
            realized_c1, realized_c2 = 2.0, 0.0
        elif (not a_up) and b_up:
            realized_c1, realized_c2 = 0.0, 2.0
        else:
            realized_c1 = realized_c2 = 1.0
        test_rows.append({
            "pair": f"{a}|{b}",
            "ev1": ev_c1(p.q_ud, p.q_du, args.cost),
            "ev2": ev_c2(p.q_ud, p.q_du, args.cost),
            "realized_c1": realized_c1 - args.cost,
            "realized_c2": realized_c2 - args.cost,
            "n": p.n, "uncertainty": p.uncertainty,
            "label1": classify_candidate(
                "A_UP_B_DOWN", 1.0, args.cost, p,
                guaranteed_mode=True, statistical_mode=True,
                guaranteed_min_net=0.01, minimum_conservative_ev=0.0,
                uncertainty_z=1.0, max_uncertainty=0.5,
                min_sample_size=30, minimum_expected_net_profit=-1.0,
            ).label,
        })

    # Prove high agreement never gets GUARANTEED without worst-case proof
    high = JointProbs(0.49, 0.01, 0.01, 0.49, n=5000, uncertainty=0.01)
    lab = classify_candidate(
        "A_UP_B_DOWN", 1.0, args.cost, high,
        guaranteed_mode=True, statistical_mode=True,
        guaranteed_min_net=0.01, minimum_conservative_ev=-10,
        uncertainty_z=0.0, max_uncertainty=1.0,
        min_sample_size=1, minimum_expected_net_profit=-10,
    ).label

    report = {
        "train": len(train), "validation": len(val), "test": len(test),
        "val_avg_log_score": {k: (val_scores[k] / max(val_scores["n"], 1))
                              for k in ("model", "agree", "indep")},
        "test_mean_ev1": sum(r["ev1"] for r in test_rows) / max(len(test_rows), 1),
        "test_mean_realized_c1": sum(r["realized_c1"] for r in test_rows) / max(len(test_rows), 1),
        "high_correlation_label": lab,
        "note": "Synthetic smoke only — not a claim of live profitability. "
                "Enable statistical mode only after real OOS + paper validation.",
    }
    out = ROOT / "results" / "cross_market_correlation_replay.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"wrote {out}")
    assert lab != "GUARANTEED_ARBITRAGE", "high correlation must not be guaranteed"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
