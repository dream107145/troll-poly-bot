"""Realistic-paper evidence for EARLY_WINDOW_LOW_ASK from the recorded windows.

    python scripts/replay_early_window.py [--charts data/charts] [--out data/replay] [--workers 4]
                                          [--threshold 0.40] [--limit N]

Buys a token whose executable ask is below the threshold, only while
0 <= seconds since the market start <= 60, with the realistic execution model
of the momentum replay, and reports the first minute in six 10-second slices:
opportunities, fills, fill rate, ask / bid / spread / spread % / depth,
target-hit rate, MFE, MAE, holding time, average and total net PnL, max
drawdown.

Two views of the slices:

* STRATEGY -- the rule as specified, one run, trades grouped by the slice
  their decision fell in. Later slices only see windows the earlier slices
  did not already trade (one entry per window), so they are not comparable
  samples on their own.
* PER-SLICE -- six runs, each allowed to enter ONLY inside its own slice.
  Every slice gets an independent look at every window; this is the
  like-for-like answer to "does the start of the minute behave differently
  from its end?".

Nothing is tuned. The defaults in strategy/early_window.py were fixed before
this was first run; the threshold / exit / model-gate variants are shown as
sensitivity, never used to pick a configuration. ``optimistic`` execution is
the old paper engine and exists only to show how much of a result is the
simulator.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from troll_poly_bot.backtest.early_window_replay import (   # noqa: E402
    BUCKETS, EarlyWindowReplay, by_bucket, summarize,
)
from troll_poly_bot.backtest.momentum_replay import EXEC_MODELS, load_windows, split_by_time   # noqa: E402
from troll_poly_bot.strategy.early_window import TARGET_OR_TIME, EarlyWindowConfig   # noqa: E402

WINDOWS: list = []


def _job(args):
    label, cfg, exec_name = args
    r = EarlyWindowReplay(cfg, EXEC_MODELS[exec_name]).run(WINDOWS)
    return label, exec_name, {"trades": r.trades, "orders": r.orders, "opportunities": r.opportunities,
                              "funnel": {b: dict(c) for b, c in r.funnel.items()},
                              "market": {b: dict(m) for b, m in r.market.items()}}


def _fmt(v, f="{:+.4f}"):
    if v is None:
        return ""
    if isinstance(v, (tuple, list)):
        return "[" + ", ".join(f.format(x) for x in v) + "]"
    return f.format(v) if isinstance(v, float) else str(v)


def _pct(v):
    return "" if v is None else f"{v:.0%}"


SLICE_HEADER = ("| slice | opps | orders | fills | fill rate (opps) | avg ask | avg bid | spread | spread % | "
                "depth | target hit | MFE held | MAE held | MFE to close | MAE to close | hold s | avg net | total net | "
                "max DD | t (epochs) |\n" + "|---" * 20 + "|")


def _slice_row(name, s):
    return (f"| {name} | {s['opportunities']} | {s['orders']} | {s['fills']} | {_pct(s.get('fill_rate_opps'))} | "
            f"{_fmt(s.get('avg_ask'), '{:.3f}')} | {_fmt(s.get('avg_bid'), '{:.3f}')} | "
            f"{_fmt(s.get('avg_spread'), '{:.3f}')} | {_pct(s.get('avg_spread_pct'))} | "
            f"{_fmt(s.get('avg_depth'), '{:.0f}')} | {_pct(s.get('target_hit_rate'))} | "
            f"{_fmt(s.get('avg_mfe'), '{:+.3f}')} | {_fmt(s.get('avg_mae'), '{:+.3f}')} | "
            f"{_fmt(s.get('avg_mfe_to_close'), '{:+.3f}')} | {_fmt(s.get('avg_mae_to_close'), '{:+.3f}')} | "
            f"{_fmt(s.get('avg_hold_s'), '{:.0f}')} | {_fmt(s.get('avg_pnl'))} | {_fmt(s.get('total_pnl'), '{:+.2f}')} | "
            f"{_fmt(s.get('max_drawdown'), '{:.2f}')} | {_fmt(s.get('t_epoch'), '{:+.2f}')} |")


SUM_HEADER = ("| run | opps | orders | fills | fill rate (orders) | win | target hit | TP exits | avg fill | "
              "slippage | avg net/trade | net/share | total net | 95% CI net/trade (epochs) | max DD | t (epochs) |\n"
              + "|---" * 16 + "|")


def _sum_row(name, s):
    return (f"| {name} | {s['opportunities']} | {s['orders']} | {s['fills']} | {_pct(s.get('fill_rate_orders'))} | "
            f"{_pct(s.get('win_rate'))} | {_pct(s.get('target_hit_rate'))} | {_pct(s.get('take_profit_rate'))} | "
            f"{_fmt(s.get('avg_fill'), '{:.3f}')} | {_fmt(s.get('avg_slippage'))} | {_fmt(s.get('avg_pnl'))} | "
            f"{_fmt(s.get('avg_pnl_per_share'))} | {_fmt(s.get('total_pnl'), '{:+.2f}')} | "
            f"{_fmt(s.get('ci95_pnl'), '{:+.3f}')} | {_fmt(s.get('max_drawdown'), '{:.2f}')} | "
            f"{_fmt(s.get('t_epoch'), '{:+.2f}')} |")


def main() -> None:
    global WINDOWS
    ap = argparse.ArgumentParser()
    ap.add_argument("--charts", default="data/charts")
    ap.add_argument("--out", default="data/replay")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--threshold", type=float, default=None, help="override the low-price threshold")
    ap.add_argument("--limit", type=int, default=0, help="only the first N windows (smoke runs)")
    args = ap.parse_args()

    t0 = time.time()
    WINDOWS = load_windows(args.charts)
    if args.limit:
        WINDOWS = WINDOWS[:args.limit]
    base = EarlyWindowConfig() if args.threshold is None else EarlyWindowConfig(low_price_threshold=args.threshold)
    print(f"loaded {len(WINDOWS)} windows in {time.time() - t0:.0f}s", file=sys.stderr)

    jobs = [("default", base, "realistic"), ("default", base, "optimistic"), ("default", base, "conservative"),
            ("no model gate (diagnostic)", replace(base, min_model_edge=-1.0, min_expected_net=-1.0), "realistic"),
            ("hold to settlement (no target)", replace(base, take_profit=0.0), "realistic"),
            ("target or sell at bid after 60 s", replace(base, exit_mode=TARGET_OR_TIME), "realistic"),
            ("no model gate, sell at bid after 60 s (diagnostic)",
             replace(base, min_model_edge=-1.0, min_expected_net=-1.0, exit_mode=TARGET_OR_TIME), "realistic")]
    for th in (0.30, 0.35, 0.45):
        if abs(th - base.low_price_threshold) > 1e-9:
            jobs.append((f"threshold {th:.2f}", replace(base, low_price_threshold=th), "realistic"))
    for lo, hi in BUCKETS:
        jobs.append((f"slice {lo}-{hi}s", replace(base, entry_window_start_s=float(lo), entry_window_end_s=float(hi)),
                     "realistic"))
        jobs.append((f"slice {lo}-{hi}s nogate",
                     replace(base, entry_window_start_s=float(lo), entry_window_end_s=float(hi),
                             min_model_edge=-1.0, min_expected_net=-1.0), "realistic"))

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = {(lab, ex): res for lab, ex, res in pool.map(_job, jobs)}
    print(f"replayed {len(jobs)} runs in {time.time() - t0:.0f}s", file=sys.stderr)

    splits = split_by_time(WINDOWS)
    period = {w.epoch: name for name, ws in splits.items() for w in ws}
    first, last = WINDOWS[0], WINDOWS[-1]
    fmt_ts = lambda ms: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))  # noqa: E731
    L: list[str] = []
    L.append(f"# EARLY_WINDOW_LOW_ASK replay, {time.strftime('%Y-%m-%d')}\n")
    L.append(f"{len(WINDOWS)} windows, {len({w.epoch for w in WINDOWS})} epochs, "
             f"{fmt_ts(first.open_ts)} -> {fmt_ts(last.open_ts)} UTC. Config: `{json.dumps(asdict(base))}`\n")

    main_r = results[("default", "realistic")]
    tr, od, op = main_r["trades"], main_r["orders"], main_r["opportunities"]

    L.append("## 1. Headline (the rule as specified, all periods)\n")
    L.append(SUM_HEADER)
    for (lab, ex), res in results.items():
        if lab.startswith("slice"):
            continue
        L.append(_sum_row(f"{lab} / {ex}", summarize(res["trades"], res["orders"], res["opportunities"])))
    L.append("")

    L.append("## 2. First minute by 10-second slice: STRATEGY view (one run, grouped by decision time)\n")
    L.append(SLICE_HEADER)
    for b, s in by_bucket(tr, od, op).items():
        L.append(_slice_row(b, s))
    L.append("\nLater slices only contain windows the earlier ones did not trade; compare them with section 3.\n")

    L.append("## 3. First minute by 10-second slice: PER-SLICE view (entry allowed only inside the slice)\n")
    for gate in ("", " nogate"):
        L.append(f"### {'model gate ON (the rule)' if not gate else 'model gate OFF (diagnostic: every cheap ask)'}\n")
        L.append(SLICE_HEADER)
        for lo, hi in BUCKETS:
            res = results[(f"slice {lo}-{hi}s{gate}", "realistic")]
            L.append(_slice_row(f"{lo}-{hi}s", summarize(res["trades"], res["orders"], res["opportunities"])))
        L.append("")

    L.append("## 4. Market conditions: every side-point with ask < threshold in the first minute\n")
    L.append("| slice | side-points | avg ask | avg bid | avg spread | avg spread % | avg depth |\n|---|---|---|---|---|---|---|")
    for lo, hi in BUCKETS:
        m = main_r["market"].get(f"{lo}-{hi}s") or {}
        n, nb = m.get("n", 0), m.get("n_bid", 0)
        if not n:
            continue
        L.append(f"| {lo}-{hi}s | {int(n)} | {m['ask'] / n:.3f} | {m['bid'] / nb:.3f} | {m['spread'] / nb:.3f} | "
                 f"{m['spread_pct'] / nb:.0%} | {m['depth'] / n:.0f} |" if nb else f"| {lo}-{hi}s | {int(n)} | | | | | |")
    L.append("")

    L.append("## 5. Why not: decision points by reason (default run)\n")
    reasons = sorted({r for c in main_r["funnel"].values() for r in c})
    cols = ["pre-open"] + [f"{lo}-{hi}s" for lo, hi in BUCKETS] + ["after 60s"]
    L.append("| reason | " + " | ".join(cols) + " |\n" + "|---" * (len(cols) + 1) + "|")
    for r in reasons:
        L.append(f"| {r} | " + " | ".join(str(main_r["funnel"].get(c, {}).get(r, 0)) for c in cols) + " |")
    pre_entries = sum(1 for t in tr if t["s_decision"] < 0) + sum(1 for o in od if o["s_decision"] < 0)
    late = sum(1 for o in od if o["s_decision"] > base.entry_window_end_s)
    L.append(f"\nOrders decided before the market start: {pre_entries}. Orders decided after "
             f"{base.entry_window_end_s:.0f} s: {late}.\n")

    L.append("## 6. Stability: chronological periods (nothing is fitted, so all three are out-of-sample)\n")
    L.append(SUM_HEADER)
    for p in ("train", "validation", "test"):
        L.append(_sum_row(p, summarize([t for t in tr if period.get(t["epoch"]) == p],
                                       [o for o in od if period.get(o["epoch"]) == p],
                                       [o for o in op if period.get(o["epoch"]) == p])))
    L.append("")
    L.append("## 7. By asset (default, realistic)\n")
    L.append(SUM_HEADER)
    for a in sorted({w.asset for w in WINDOWS}):
        L.append(_sum_row(a, summarize([t for t in tr if t["asset"] == a], [o for o in od if o["asset"] == a],
                                       [o for o in op if o["asset"] == a])))
    L.append("")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    md = out / f"early_window_replay_{stamp}.md"
    md.write_text("\n".join(L) + "\n", encoding="utf-8")
    (out / f"early_window_replay_{stamp}.json").write_text(json.dumps(
        {"config": asdict(base), "windows": len(WINDOWS),
         "runs": {f"{lab} / {ex}": {"summary": summarize(res["trades"], res["orders"], res["opportunities"]),
                                     "trades": res["trades"], "funnel": res["funnel"]}
                  for (lab, ex), res in results.items()}}, default=str), encoding="utf-8")
    print("\n".join(L))
    print(f"\nwrote {md}", file=sys.stderr)


if __name__ == "__main__":
    main()
