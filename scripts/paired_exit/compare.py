"""Strategies A-F + B on one asset with every metric of the brief (item 16): IN-SAMPLE (train) and
OUT-OF-SAMPLE (test + forward), the validation split shown for the selection.

    nice -n 19 python scripts/paired_exit/compare.py BTC > results/paired_exit_comparison.md
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L       # noqa: E402
import run as R          # noqa: E402
import sim as S          # noqa: E402
import pe_select as SEL  # noqa: E402
from stage2 import entry_of  # noqa: E402

METRICS = [("windows", "markets", "{:d}"), ("entries", "orders (entries)", "{:d}"), ("fills", "fills", "{:d}"),
           ("fill_rate", "fill rate", "{:.0%}"), ("net", "total P&L $", "{:+.2f}"), ("per_window", "P&L per market $", "{:+.4f}"),
           ("per_hour", "P&L per hour $", "{:+.3f}"), ("t", "t-stat (per window)", "{:+.2f}"),
           ("sharpe_day", "Sharpe-like (daily)", "{:+.2f}"), ("ev_per_entry", "EXPECTED_VALUE_PER_ENTRY $", "{:+.4f}"),
           ("median_trade", "median trade $", "{:+.3f}"), ("win_rate", "win rate", "{:.0%}"),
           ("avg_win", "average winner $", "{:+.3f}"), ("avg_loss", "average loser $", "{:+.3f}"),
           ("small_win_large_loss", "SMALL_WIN / LARGE_LOSS ratio", "{:.3f}"),
           ("wins_per_loss", "wins needed per average loss", "{:.1f}"),
           ("largest_win", "largest winner $", "{:+.2f}"), ("largest_loss", "largest loser $", "{:+.2f}"),
           ("max_dd", "max drawdown $", "{:+.2f}"), ("worst_window", "worst market $", "{:+.2f}"),
           ("cvar5_window", "avg of 5 % worst markets $", "{:+.2f}"), ("profit_factor", "profit factor", "{:.2f}"),
           ("fees", "fees $", "{:.2f}"), ("slippage", "avg slippage (fill - ask) $", "{:+.4f}"),
           ("avg_hold_s", "avg holding time s", "{:.0f}"), ("max_exposure", "max position $", "{:.2f}"),
           ("unpaired_positions", "unpaired positions", "{:d}"), ("unpaired_losses", "unpaired losers", "{:d}"),
           ("unpaired_loss_avg", "avg unpaired loser $", "{:+.3f}"), ("pairs_completed", "completed pairs", "{:d}"),
           ("pair_avg", "avg completed pair $", "{:+.3f}"), ("pct_settle", "% reaching settlement", "{:.0%}"),
           ("pct_stop", "% stopped", "{:.0%}"), ("pct_thesis", "% thesis exits", "{:.0%}"),
           ("pct_time", "% time exits", "{:.0%}"), ("pct_tp", "% take-profit", "{:.0%}"), ("pct_pair", "% pair-locked", "{:.0%}")]


def merge(res, splits):
    pn = np.concatenate([res[s][0] for s in splits])
    legs = [r for s in splits for r in res[s][1]]
    return R.metrics(pn, legs, sum(res[s][2] for s in splits))


def main(asset="BTC"):
    wins = R.load(asset)
    costs = json.load(open(f"{L.CACHE}/costs_train.json"))
    ent = entry_of(SEL.choose(SEL.by_asset(SEL.load("stage1"))[asset])[0])
    base = replace(S.Params(**{**ent, "name": "x", "structure": "C", "stop": None}),
                   cost_c=round(costs[asset]["entry_cost"], 4))
    fair_tp = dict(tp_offsets=(0.02,), tp_fracs=(1.0,), tp_fair=0.03)
    rules = {
        "A equal UP/DOWN shares, hold": replace(base, structure="A", max_window_usdc=20.0, max_unpaired_usdc=20.0, max_dir_usdc=20.0),
        "B today: pair < 0.95, hold": S.Params(name="B", structure="B", entry="old", lo=0.20, hi=0.93, limit_off=0.03,
                                               pair_target=0.95, stop=None),
        "C first token + stop 0.20 + time 30 s": replace(base, stop=0.20, time_exit_s=30.0),
        "D first token + fair TP + time 30 s (CHOSEN)": replace(base, structure="D", time_exit_s=30.0, **fair_tp),
        "D' first token + 2-level static ladder (10 sh) + stop 0.20": replace(base, structure="D", shares=10.0, tp_offsets=(0.05, 0.10),
                                                                             tp_fracs=(0.5, 0.5), stop=0.20, max_window_usdc=10.0,
                                                                             max_unpaired_usdc=10.0, max_dir_usdc=10.0),
        "E D + optional pair < 0.95": replace(base, structure="E", pair_target=0.95, time_exit_s=30.0),
        "F equal pair + inventory + TP + stop": replace(base, structure="F", shares=10.0, stop=0.20, tp_offsets=(0.10,), tp_fracs=(1.0,),
                                                         max_window_usdc=20.0, max_unpaired_usdc=20.0, max_dir_usdc=20.0),
        "C0 first token held to settlement (reference)": base,
    }
    out = {}
    for name, prm in rules.items():
        res = R.simulate(wins, prm, L.Exec())
        out[name] = {"IN-SAMPLE (train)": merge(res, ("train",)), "validation": merge(res, ("validation",)),
                     "OUT-OF-SAMPLE (test+forward)": merge(res, ("test", "forward"))}
    print(f"# Strategies A-F on {asset}: every metric\n")
    print(f"Entry (C-F): {asset}'s Stage-1 rule {ent['k_sig']}, edge {ent['min_edge']}, agree {ent['min_agree']}, band "
          f"{ent['lo']}-{ent['hi']}, offset {ent['limit_off']}, late +{ent['late_edge_add']}; entry cost {base.cost_c}. "
          f"Exposure: 5 shares (10 where marked), 1 open leg, 3 buys, 10 USDC / window (20 for A / F).\n")
    for period in ("IN-SAMPLE (train)", "validation", "OUT-OF-SAMPLE (test+forward)"):
        print(f"## {period}\n")
        names = list(rules)
        print("| metric | " + " | ".join(names) + " |")
        print("|---|" + "---:|" * len(names))
        for key, label, fmt in METRICS:
            cells = []
            for n in names:
                v = out[n][period][key]
                try:
                    cells.append(fmt.format(int(v)) if "d}" in fmt else fmt.format(v))
                except (ValueError, TypeError):
                    cells.append("—")
            print(f"| {label} | " + " | ".join(c.replace("nan", "—") for c in cells) + " |")
        print()
    json.dump(out, open(f"{L.CACHE}/compare_{asset.lower()}.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "BTC")
