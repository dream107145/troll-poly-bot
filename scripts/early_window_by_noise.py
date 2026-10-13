"""EARLY_WINDOW_LOW_ASK: does selecting assets or noise regimes rescue it?

    python scripts/early_window_by_noise.py [--charts data/charts] [--out data/replay] [--workers 2]

Replays the broad trade (every cheap first-minute ask, no model gate) and the
rule itself, then splits the fills by what was KNOWN AT ENTRY:

* asset
* spot volatility (the live two-scale estimator, bps/s), absolute and
  relative to the asset's own median
* the token's own noise: range of its mid over the previous 30 s, sd of its
  1-second mid changes, and its move over the last 10 s (falling into the buy)

Every split is reported by tercile. Then the selection is tested the only way
that means anything: tercile cut points and the chosen bucket / asset / cell
come from the TRAINING half (chronological, by epoch) and are scored on the
second half they never saw.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from troll_poly_bot.backtest.early_window_replay import EarlyWindowReplay   # noqa: E402
from troll_poly_bot.backtest.momentum_replay import REALISTIC, load_windows, split_by_time   # noqa: E402
from troll_poly_bot.strategy.early_window import EarlyWindowConfig   # noqa: E402

WINDOWS: list = []
MIN_TRAIN = 30          # a bucket needs this many training fills to be selectable


def _job(args):
    label, cfg = args
    return label, EarlyWindowReplay(cfg, REALISTIC, record_market=False).run(WINDOWS).trades


def stats(rows: list[dict]) -> dict:
    rows = [r for r in rows if r.get("pnl") is not None]
    if not rows:
        return {"n": 0}
    ep: dict[int, float] = defaultdict(float)
    for r in rows:
        ep[r["epoch"]] += r["pnl"]
    e = list(ep.values())
    t = st.fmean(e) / (st.stdev(e) / math.sqrt(len(e))) if len(e) > 2 and st.stdev(e) > 0 else None
    return {"n": len(rows), "hit": st.fmean([r["target_hit"] for r in rows]),
            "settle": st.fmean([1.0 if r["won_at_settle"] else 0.0 for r in rows]),
            "fill": st.fmean([r["fill_px"] for r in rows]),
            "net": st.fmean([r["pnl_per_share"] for r in rows]), "t": t}


def row(name, s) -> str:
    if not s["n"]:
        return f"| {name} | 0 | | | | | |"
    t = "" if s["t"] is None else f"{s['t']:+.1f}"
    return (f"| {name} | {s['n']} | {s['hit']:.0%} | {s['settle']:.0%} | {s['fill']:.3f} | "
            f"{s['net'] * 100:+.1f} c | {t} |")


HEAD = "| bucket | fills | target hit | settle win | avg fill | net / share | t (epochs) |\n|---|---:|---:|---:|---:|---:|---:|"


def cuts(values: list[float]) -> tuple[float, float]:
    v = sorted(values)
    return v[len(v) // 3], v[2 * len(v) // 3]


def tercile(x, c) -> str:
    return "low" if x < c[0] else "mid" if x < c[1] else "high"


def features(rows: list[dict], asset_median: dict[str, float]) -> None:
    for r in rows:
        s = r.get("sigma_bps_s")
        r["sigma_rel"] = s / asset_median[r["asset"]] if s and asset_median.get(r["asset"]) else None


DIMS = {
    "spot vol, bps/s": "sigma_bps_s",
    "spot vol vs the asset's median": "sigma_rel",
    "token mid range, last 30 s": "token_range_30s",
    "token 1 s mid change sd": "token_sd_1s",
    "token move, last 10 s": "token_move_10s",
}


def report(name: str, trades: list[dict], period: dict[int, str]) -> list[str]:
    L = [f"## {name}\n"]
    med = {a: st.median([r["sigma_bps_s"] for r in trades if r["asset"] == a and r.get("sigma_bps_s")])
           for a in {r["asset"] for r in trades}}
    features(trades, med)
    L.append("### By asset (all periods)\n")
    L.append("| asset | fills | target hit | settle win | avg fill | net / share | t (epochs) | median spot vol bps/s | "
             "median token range 30 s |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for a in sorted(med):
        rs = [r for r in trades if r["asset"] == a]
        rng = [r["token_range_30s"] for r in rs if r.get("token_range_30s") is not None]
        L.append(row(a, stats(rs))[:-1] + f"| {med[a]:.2f} | {st.median(rng) * 100 if rng else 0:.1f} c |")
    L.append("")
    for title, key in DIMS.items():
        vals = [r[key] for r in trades if r.get(key) is not None]
        if len(vals) < 30:
            continue
        c = cuts(vals)
        L.append(f"### {title} (terciles: < {c[0]:.4g} / < {c[1]:.4g} / above)\n")
        L.append(HEAD)
        for b in ("low", "mid", "high"):
            L.append(row(b, stats([r for r in trades if r.get(key) is not None and tercile(r[key], c) == b])))
        L.append("")

    # ---- selection on the training half, scored on the half it never saw ----
    train = [r for r in trades if period[r["epoch"]] == "train"]
    hold = [r for r in trades if period[r["epoch"]] != "train"]
    cands: list[tuple[str, callable]] = []
    for a in sorted(med):
        cands.append((f"asset {a}", lambda r, a=a: r["asset"] == a))
    for title, key in DIMS.items():
        vals = [r[key] for r in train if r.get(key) is not None]
        if len(vals) < 30:
            continue
        c = cuts(vals)
        for b in ("low", "mid", "high"):
            cands.append((f"{title}: {b}", lambda r, key=key, c=c, b=b: r.get(key) is not None
                          and tercile(r[key], c) == b))
    # asset x own-noise cells
    for a in sorted(med):
        for title in ("spot vol vs the asset's median", "token mid range, last 30 s"):
            key = DIMS[title]
            vals = [r[key] for r in train if r["asset"] == a and r.get(key) is not None]
            if len(vals) < 30:
                continue
            c = cuts(vals)
            for b in ("low", "mid", "high"):
                cands.append((f"{a} x {title}: {b}", lambda r, a=a, key=key, c=c, b=b: r["asset"] == a
                              and r.get(key) is not None and tercile(r[key], c) == b))
    scored = []
    for lab, f in cands:
        s_tr = stats([r for r in train if f(r)])
        if s_tr["n"] >= MIN_TRAIN:
            scored.append((s_tr["net"], lab, f, s_tr))
    scored.sort(key=lambda x: -x[0])
    L.append(f"### Selection: chosen on TRAINING ({len(train)} fills), scored on the held-out half ({len(hold)} fills)\n")
    L.append(f"{len(scored)} candidate buckets with >= {MIN_TRAIN} training fills "
             f"({sum(1 for x in scored if x[0] > 0)} positive in training).\n")
    L.append("| candidate (top 10 by training net) | train fills | train net / share | train t | held-out fills | "
             "held-out target hit | held-out net / share | held-out t |\n|---|---:|---:|---:|---:|---:|---:|---:|")
    for net, lab, f, s_tr in scored[:10]:
        s_ho = stats([r for r in hold if f(r)])
        fmt_t = lambda t: "" if t is None else f"{t:+.1f}"  # noqa: E731
        L.append(f"| {lab} | {s_tr['n']} | {net * 100:+.1f} c | {fmt_t(s_tr['t'])} | {s_ho['n']} | "
                 + (f"{s_ho['hit']:.0%} | {s_ho['net'] * 100:+.1f} c | {fmt_t(s_ho['t'])} |" if s_ho["n"] else " | | |"))
    pos = [f for net, _, f, _ in scored if net > 0]
    if pos:
        s_ho = stats([r for r in hold if any(f(r) for f in pos)])
        L.append(f"\nEvery bucket positive in training, pooled, on the held-out half: {row('pooled', s_ho)}\n")
    L.append("")
    return L


def main() -> None:
    global WINDOWS
    ap = argparse.ArgumentParser()
    ap.add_argument("--charts", default="data/charts")
    ap.add_argument("--out", default="data/replay")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    t0 = time.time()
    WINDOWS = load_windows(args.charts)
    base = EarlyWindowConfig()
    nogate = replace(base, min_model_edge=-1.0, min_expected_net=-1.0)
    jobs = [("every cheap ask (no model gate), target or settle", nogate),
            ("every cheap ask (no model gate), hold to settlement", replace(nogate, take_profit=0.0)),
            ("the rule (model gate on)", base)]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = dict(pool.map(_job, jobs))
    print(f"{len(WINDOWS)} windows, {len(jobs)} runs in {time.time() - t0:.0f}s", file=sys.stderr)
    period = {w.epoch: p for p, ws in split_by_time(WINDOWS, (0.5, 0.25, 0.25)).items() for w in ws}
    L = [f"# EARLY_WINDOW_LOW_ASK by asset and noise, {time.strftime('%Y-%m-%d')}\n",
         f"{len(WINDOWS)} windows, realistic execution, 5-share orders, both taker fees. "
         "Training = first half of the epochs; held-out = the second half.\n"]
    for lab, _ in jobs:
        L += report(lab, results[lab], period)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md = out / f"early_window_by_noise_{time.strftime('%Y-%m-%d')}.md"
    md.write_text("\n".join(L) + "\n", encoding="utf-8")
    (md.with_suffix(".json")).write_text(json.dumps(results, default=str), encoding="utf-8")
    print("\n".join(L))
    print(f"\nwrote {md}", file=sys.stderr)


if __name__ == "__main__":
    main()
