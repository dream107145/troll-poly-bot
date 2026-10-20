"""Backtest LEADER_PAIR (strategy/leader_pair/) on the recorded BTC 5-minute books.

    nice -n 19 .venv/bin/python scripts/leader_pair_backtest.py                      # the defaults, every market
    ... --set name KEY=VALUE ...  [--set ...]                                         # parameter sets (sensitivity)
    ... --limit 300 --workers 2 --slippage 0.01 --haircut 0.5 --stride 2

Writes results/leader_pair_backtest_<stamp>.jsonl (one record per set: the split summaries, attribution, the
per-market rows of the LAST set to results/leader_pair_markets_<stamp>.jsonl). TRAIN / VAL / TEST are chronological
50 / 25 / 25 % of the markets; the sets are compared on TRAIN+VAL, and only the defaults are reported on TEST.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from troll_poly_bot.strategy.leader_pair.backtest import (   # noqa: E402
    BacktestConfig, engine_for, load_cache, report_splits, result_row, run_market, split_cuts)

BOOKS = UPS = None


def _init(books_path, ups_path):
    global BOOKS, UPS
    BOOKS, UPS, _ = load_cache(books_path, ups_path)


def _chunk(args):
    overrides, bcfg, slugs = args
    e = engine_for(**overrides)
    return [run_market(e, s, BOOKS[s], bool(UPS[s]), bcfg) for s in slugs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", nargs="+", action="append", default=[], metavar=("NAME", "KEY=VALUE"))
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--books", type=Path, default=ROOT / "data/replay/btc5m_books.pkl")
    ap.add_argument("--outcomes", type=Path, default=ROOT / "data/replay/btc5m_outcomes.json")
    ap.add_argument("--latency-ms", type=float, default=250.0)
    ap.add_argument("--slippage", type=float, default=0.0, help="extra USDC a share on every fill")
    ap.add_argument("--haircut", type=float, default=1.0, help="fraction of the displayed ask depth available")
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--tag", default="")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "results")
    a = ap.parse_args()
    stamp = time.strftime("%Y-%m-%d_%H%M") + (f"_{a.tag}" if a.tag else "")
    bcfg = BacktestConfig(latency_ms=a.latency_ms, extra_slippage=a.slippage, depth_haircut=a.haircut, stride=a.stride)
    _init(a.books, a.outcomes)
    slugs = sorted((s for s in BOOKS if s in UPS), key=lambda s: BOOKS[s]["open"])
    if a.limit:
        slugs = slugs[:a.limit]
    cuts = split_cuts([BOOKS[s]["open"] for s in slugs])
    sets = a.set or [["defaults"]]
    n = max(1, a.workers)
    chunks = [slugs[i::n] for i in range(n)]
    out = a.out_dir / f"leader_pair_backtest_{stamp}.jsonl"
    rows_path = a.out_dir / f"leader_pair_markets_{stamp}.jsonl"
    print(f"{len(slugs)} markets, cuts {cuts}, {n} workers, {bcfg} -> {out}", flush=True)
    ctx = mp.get_context("fork")
    with ctx.Pool(n) as pool:
        for spec in sets:
            name, kv = spec[0], dict(x.split("=", 1) for x in spec[1:])
            t0 = time.time()
            rows = [r for part in pool.map(_chunk, [(kv, bcfg, c) for c in chunks]) for r in part]
            rows.sort(key=lambda r: r.open)
            rep = report_splits(rows, cuts)
            rec = {"name": name, "overrides": kv, "backtest": bcfg.__dict__ if hasattr(bcfg, "__dict__") else str(bcfg),
                   "latency_ms": bcfg.latency_ms, "extra_slippage": bcfg.extra_slippage, "depth_haircut": bcfg.depth_haircut,
                   "stride": bcfg.stride, "markets": len(rows), "cuts": cuts, "splits": rep, "seconds": round(time.time() - t0)}
            with out.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            with rows_path.open("w") as f:
                for r in rows:
                    f.write(json.dumps(result_row(r)) + "\n")
            print(f"{name:24s} " + " | ".join(f"{k} {v['per_market']:+.3f}/mkt t {v['t']} win {v['win_rate']} ({v['traded']}/{v['markets']}, "
                                              f"net {v['net_pnl']:+.0f}, fees {v['fees']:.0f})" for k, v in rep.items() if v.get("markets"))
                  + f" [{rec['seconds']} s]", flush=True)


if __name__ == "__main__":
    main()
