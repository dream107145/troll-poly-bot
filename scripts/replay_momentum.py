"""Realistic-paper evidence for MOMENTUM_TAKER from the recorded windows.

    python scripts/replay_momentum.py [--charts data/charts] [--profiles strict,easy]
                                      [--exec realistic,optimistic,conservative] [--grid]
                                      [--write-calibration data/momentum_calibration.json]
                                      [--out data/replay] [--workers 3]

Runs the LIVE decision and exit code (strategy/momentum.py) over every archived
window with the execution model of backtest/momentum_replay.py, split
chronologically by 5-minute epoch into TRAINING (50 %), VALIDATION (25 %) and
OUT-OF-SAMPLE (25 %). Prints markdown tables and writes the full result as
JSON. ``optimistic`` is the old paper engine and is shown only to measure how
much of a result is the simulator.

``--grid`` searches a SMALL grid (z threshold x minimum net edge) on the
TRAINING period only, picks the best expectancy with at least 20 trades, and
reports what that choice did on validation and out-of-sample. It never
reports a train-period number as evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from troll_poly_bot.backtest.momentum_replay import (   # noqa: E402
    EXEC_MODELS, MomentumReplay, fit_calibration, group, load_windows, split_by_time, summarize, time_bucket,
    z_bucket,
)
from troll_poly_bot.strategy.momentum import MomentumConfig   # noqa: E402

WINDOWS: list = []          # loaded once before the pool forks
SPLITS: dict = {}


def _profile(name: str) -> MomentumConfig:
    if name == "nogate":
        # DIAGNOSTIC, never deployable: the edge gate off (no exit-cost or
        # uncertainty charge, any net edge), every other gate on. PnL is still
        # charged both real fees. It answers "what do the momentum signals
        # themselves do under realistic execution?" -- the question the edge
        # gate is supposed to have got right.
        return replace(MomentumConfig.strict(), include_exit_costs=False, uncertainty_base=0.0,
                       uncertainty_frac=0.0, uncertainty_vol=0.0, min_net_edge=-1.0, profile="nogate")
    return MomentumConfig.easy() if name == "easy" else MomentumConfig.strict()


def _job(args):
    label, mcfg, exec_name, split = args
    ws = WINDOWS if split == "all" else SPLITS[split]
    r = MomentumReplay(mcfg, EXEC_MODELS[exec_name]).run(ws)
    return label, exec_name, split, r.trades, r.orders, r.skips


def _row(name, s):
    def f(k, fmt="{:+.4f}"):
        v = s.get(k)
        return "" if v is None else (fmt.format(v) if isinstance(v, float) else str(v))
    return (f"| {name} | {s.get('orders', 0)} | {s.get('fills', s.get('trades', 0))} | {f('fill_rate', '{:.0%}')} | "
            f"{s.get('trades', 0)} | {f('win_rate', '{:.0%}')} | {f('avg_entry', '{:.3f}')} | {f('avg_exit', '{:.3f}')} | "
            f"{f('avg_slippage')} | {f('fees', '{:.2f}')} | {f('avg_win')} | {f('avg_loss')} | {f('expectancy')} | "
            f"{f('pnl', '{:+.2f}')} | {f('max_drawdown', '{:.2f}')} | {f('markout_1s')} | {f('t_epoch', '{:.2f}')} |")


HEADER = ("| | orders | fills | fill rate | closed | win | avg entry | avg exit | slippage | fees | avg win | "
          "avg loss | expectancy/trade | PnL | max DD | markout 1s | t (epochs) |\n" + "|---" * 17 + "|")


def main() -> None:
    global WINDOWS, SPLITS
    ap = argparse.ArgumentParser()
    ap.add_argument("--charts", default="data/charts")
    ap.add_argument("--profiles", default="strict,easy")
    ap.add_argument("--exec", default="realistic,optimistic,conservative")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--write-calibration", default="")
    ap.add_argument("--out", default="data/replay")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    t0 = time.time()
    WINDOWS = load_windows(args.charts)
    SPLITS = split_by_time(WINDOWS)
    span = lambda ws: (time.strftime("%m-%d %H:%M", time.gmtime(ws[0].open_ts / 1000)) + " -> " +   # noqa: E731
                       time.strftime("%m-%d %H:%M", time.gmtime(ws[-1].close_ts / 1000))) if ws else "-"
    print(f"# MOMENTUM_TAKER realistic replay\n\n{len(WINDOWS)} windows loaded in {time.time() - t0:.0f}s; "
          f"assets {sorted({w.asset for w in WINDOWS})}")
    for k in ("train", "validation", "test"):
        print(f"- {k}: {len(SPLITS[k])} windows, {len({w.epoch for w in SPLITS[k]})} epochs, {span(SPLITS[k])} UTC")

    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    execs = [e.strip() for e in args.exec.split(",") if e.strip()]
    jobs = [(p, _profile(p), e, sp) for p in profiles for e in execs for sp in ("train", "validation", "test")]
    grid_cfgs = []
    if args.grid:
        for z in (2.5, 3.0, 3.5, 4.0):
            for edge in (0.0, 0.01, 0.02):
                grid_cfgs.append((f"grid z{z} e{edge}", replace(MomentumConfig.strict(), z_threshold=z, min_net_edge=edge)))
        jobs += [(lbl, cfg, "realistic", "train") for lbl, cfg in grid_cfgs]
    results: dict = {}
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for label, exec_name, split, trades, orders, skips in pool.map(_job, jobs):
            results[(label, exec_name, split)] = (trades, orders, skips)
    print(f"\n{len(jobs)} replays in {time.time() - t0:.0f}s total\n")

    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "windows": len(WINDOWS),
           "splits": {k: {"windows": len(v), "epochs": len({w.epoch for w in v}), "span": span(v)} for k, v in SPLITS.items()},
           "runs": {}}
    for p in profiles:
        print(f"\n## Profile `{p}`\n")
        print(HEADER)
        for e in execs:
            for sp in ("train", "validation", "test"):
                trades, orders, _ = results[(p, e, sp)]
                s = summarize(trades, orders)
                out["runs"][f"{p}/{e}/{sp}"] = s
                print(_row(f"{e} / {sp}", s))
        # breakdowns on the realistic model, all periods pooled (descriptive only)
        if "realistic" in execs:
            allt = [t for sp in ("train", "validation", "test") for t in results[(p, "realistic", sp)][0]]
            allo = [o for sp in ("train", "validation", "test") for o in results[(p, "realistic", sp)][1]]
            alls = [x for sp in ("train", "validation", "test") for x in results[(p, "realistic", sp)][2]]
            for title, key in (("asset", lambda t: t["asset"]),
                               ("time to expiry", lambda t: time_bucket(t["secs_left"])),
                               ("momentum z", lambda t: z_bucket(t.get("z"))),
                               ("entry price", lambda t: f"{int((t.get('fill_px') or 0) * 10) / 10:.1f}-")):
                g = group(allt, key)
                if g:
                    print(f"\n### `{p}` realistic, by {title} (all periods, descriptive)\n\n{HEADER}")
                    for k, s in g.items():
                        print(_row(k, s))
            out["runs"][f"{p}/realistic/by_asset"] = group(allt, lambda t: t["asset"])
            oc: dict = {}
            for o in allo:
                oc[o["outcome"]] = oc.get(o["outcome"], 0) + 1
            print(f"\norder outcomes (realistic, all periods): {oc or 'no orders'}")
            # missed opportunities
            cls: dict = {}
            for x in alls:
                cls.setdefault(x["reason"], {}).setdefault(x["class"], 0)
                cls[x["reason"]][x["class"]] += 1
            if cls:
                print(f"\n### `{p}` skipped signals: was the price there at arrival, and did a taker round "
                      f"trip at 20 s make money after both fees? (sampled every 2 s per reason per window)\n")
                print("| reason | skips | unfillable | good miss (would lose) | bad miss (would win) | unknown | "
                      "mean hypothetical net |\n|---|---:|---:|---:|---:|---:|---:|")
                for reason, c in sorted(cls.items(), key=lambda kv: -sum(kv[1].values())):
                    nets = [x["hypothetical_net"] for x in alls if x["reason"] == reason and "hypothetical_net" in x]
                    mean = f"{sum(nets) / len(nets):+.4f}" if nets else ""
                    print(f"| {reason} | {sum(c.values())} | {c.get('UNFILLABLE_SKIP', 0)} | {c.get('GOOD_MISS', 0)} | "
                          f"{c.get('BAD_MISS', 0)} | {c.get('UNKNOWN', 0)} | {mean} |")
                out["runs"][f"{p}/realistic/skips"] = cls
    if grid_cfgs:
        print("\n## Grid on TRAINING only (realistic)\n")
        print(HEADER)
        best = None
        for lbl, cfg in grid_cfgs:
            trades, orders, _ = results[(lbl, "realistic", "train")]
            s = summarize(trades, orders)
            out["runs"][f"{lbl}/realistic/train"] = s
            print(_row(lbl, s))
            if s.get("trades", 0) >= 20 and s.get("expectancy") is not None and \
                    (best is None or s["expectancy"] > best[2]["expectancy"]):
                best = (lbl, cfg, s)
        if best is None or best[2]["expectancy"] <= 0:
            print("\nNo grid point had >= 20 closed trades AND positive expectancy on the training period. "
                  "Nothing is selected; nothing is validated.")
            out["grid_selected"] = None
        else:
            lbl, cfg, _ = best
            print(f"\nSelected on TRAINING: **{lbl}**. Its validation and out-of-sample results:\n\n{HEADER}")
            for sp in ("validation", "test"):
                r = MomentumReplay(cfg, EXEC_MODELS["realistic"]).run(SPLITS[sp])
                s = summarize(r.trades, r.orders)
                out["runs"][f"{lbl}/realistic/{sp}"] = s
                print(_row(f"{lbl} / {sp}", s))
            out["grid_selected"] = lbl
    if args.write_calibration:
        cal = fit_calibration(SPLITS["train"], MomentumConfig.strict())
        cal["fitted_on"] = out["splits"]["train"]
        Path(args.write_calibration).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write_calibration).write_text(json.dumps(cal, indent=1))
        print(f"\ncalibration (TRAINING period only) written to {args.write_calibration}")
        print("| key | windows | mean continuation @20s | se |\n|---|---:|---:|---:|")
        for k, v in sorted(cal["rows"].items()):
            print(f"| {k} | {v['n_windows']} | {v['mean']:+.4f} | {v['se'] if v['se'] is not None else ''} |")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    dest = Path(args.out) / f"momentum_replay_{time.strftime('%Y%m%d_%H%M%S')}.json"
    dest.write_text(json.dumps(out, indent=1, default=str))
    print(f"\nwritten {dest}")


if __name__ == "__main__":
    main()
