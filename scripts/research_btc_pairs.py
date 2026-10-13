"""BTC momentum-then-reversal PAIRS (2026-10-01 brief): a first leg on a strong short-term BTC
signal, the opposite side after a confirmed reversal when the pair costs well under $1 after fees,
repeated while the market is open, everything held to settlement.

    ionice -c3 nice -n 19 python scripts/research_btc_pairs.py > results/btc_pairs_backtest.md

RESEARCH ONLY (reads the research cache; same venue_lag execution model as research_btc_hedge.py:
measured presend / half round trip, the venue book 500 ms after arrival, FOK at ask + 1c with
5 shares behind it, taker fee on every fill, settlement free). Chronological TRAIN / VALIDATION /
TEST / FORWARD; the grid (trend_z x confirm_s x max_pair_cost x min_gap) is chosen on
TRAIN + VALIDATION only, locked to data/replay/btc_pairs_locked.json, and TEST / FORWARD are
reported untouched. Differences from the hedge study: NO mandatory trade (no cheapest-side
fallback), up to ``max_pairs`` pairs per market, a minimum gap between legs, a minimum pair edge.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from research_btc_hedge import CACHE, LIMIT_OFFSET, MIN_LEFT_S, PERIOD, PERIODS, RETRY_MS, SIZE, SLIPPAGE, LATENCY_ALLOWANCE, zret   # noqa: E402
from troll_poly_bot.backtest.spot_shock_replay import Execution, load_asset, windows   # noqa: E402

LOCK = ROOT / "data/replay/btc_pairs_locked.json"
OBS_S = 5.0
MAX_FIRST_PRICE = 0.65
FIRST_ATTEMPTS = 3
GRID = {"trend_z": (1.0, 1.5, 2.0), "confirm_s": (2.0, 3.0, 5.0), "max_pair": (0.95, 0.97, 0.98), "min_gap_s": (0.0, 10.0)}
MAX_PAIRS = 3
MIN_EDGE = 0.0            # the pair-cost cap already carries the edge: 1 - max_pair


def simulate(w, ex: Execution, P: dict) -> dict:
    t, left = w.t, w.left
    z1, z5 = zret(t, w.lp, w.sigma, 1000.0), zret(t, w.lp, w.sigma, 5000.0)
    open_ts = w.close - 300_000.0
    n = len(t)
    rec = {"slug": w.slug, "pairs": [], "legs": 0, "fills": 0, "orders": 0, "fees": 0.0, "up_qty": 0.0, "up_cost": 0.0,
           "dn_qty": 0.0, "dn_cost": 0.0, "signals": 0, "reversals": 0, "rejected_pair_cost": 0}
    i = int(np.searchsorted(t, open_ts + OBS_S * 1000.0))
    pairs_done = 0
    last_pair_done = -1e18
    cap = min(P["max_pair"], 1.0 - MIN_EDGE)
    while i < n and left[i] > MIN_LEFT_S and pairs_done < MAX_PAIRS:
        # ── first leg: a strong signal only (|z5| >= trend_z, 1 s agrees), after the leg gap
        if t[i] - last_pair_done < P["min_gap_s"] * 1000.0 or not (z5[i] == z5[i] and z1[i] == z1[i]) \
                or abs(z5[i]) < P["trend_z"] or z1[i] * z5[i] <= 0:
            i += 1
            continue
        side = "UP" if z5[i] > 0 else "DOWN"
        rec["signals"] += 1
        sv = w.side[side]
        first_px, first_qty, attempts, last_try = None, 0.0, 0, -1e18
        while i < n and attempts < FIRST_ATTEMPTS and left[i] > MIN_LEFT_S:
            if t[i] - last_try < RETRY_MS:
                i += 1
                continue
            ask = sv["ask"][i]
            if ask != ask or ask > MAX_FIRST_PRICE + 1e-9:
                break
            attempts += 1; last_try = t[i]; rec["orders"] += 1
            r = ex.buy(w, sv, t[i], round(ask + LIMIT_OFFSET, 2))
            i = w.first_at(r["t_known"])
            if r["filled"]:
                first_px, first_qty = r["entry"], SIZE
                rec["fills"] += 1; rec["legs"] += 1; rec["fees"] += w.fee(r["entry"]) * SIZE
                break
        if first_qty <= 0:
            i += 1
            continue
        if side == "UP":
            rec["up_qty"] += first_qty; rec["up_cost"] += first_px * first_qty
        else:
            rec["dn_qty"] += first_qty; rec["dn_cost"] += first_px * first_qty
        t_first = t[max(i - 1, 0)]
        pair = {"side": side, "first_px": first_px, "hedge_px": None, "pair_cost": None, "best_pair": None, "reversal": False}
        # ── hedge after a confirmed reversal
        other = "DOWN" if side == "UP" else "UP"
        want = -1 if side == "UP" else 1
        against_since, last_hedge_try, hedged = None, -1e18, 0.0
        while i < n and left[i] > MIN_LEFT_S and hedged <= 0:
            if z5[i] == z5[i] and z5[i] * want >= P["trend_z"]:
                against_since = t[i] if against_since is None else against_since
            else:
                against_since = None
            if against_since is not None and t[i] - against_since >= P["confirm_s"] * 1000.0 and not pair["reversal"]:
                pair["reversal"] = True; rec["reversals"] += 1
            if pair["reversal"] and t[i] - t_first >= P["min_gap_s"] * 1000.0 and t[i] - last_hedge_try >= RETRY_MS:
                osv = w.side[other]; ask = osv["ask"][i]
                if ask == ask:
                    pc = first_px + ask + w.fee(ask) + SLIPPAGE + LATENCY_ALLOWANCE
                    pair["best_pair"] = pc if pair["best_pair"] is None else min(pair["best_pair"], pc)
                    if pc <= cap + 1e-9:
                        last_hedge_try = t[i]; rec["orders"] += 1
                        r = ex.buy(w, osv, t[i], round(ask + LIMIT_OFFSET, 2))
                        i = w.first_at(r["t_known"])
                        if r["filled"]:
                            hedged = SIZE; rec["fills"] += 1; rec["legs"] += 1; rec["fees"] += w.fee(r["entry"]) * SIZE
                            pair["hedge_px"] = r["entry"]; pair["pair_cost"] = first_px + r["entry"]
                            if other == "UP":
                                rec["up_qty"] += SIZE; rec["up_cost"] += r["entry"] * SIZE
                            else:
                                rec["dn_qty"] += SIZE; rec["dn_cost"] += r["entry"] * SIZE
                        continue
                    rec["rejected_pair_cost"] += 1
            i += 1
        rec["pairs"].append(pair)
        if hedged > 0:
            pairs_done += 1; last_pair_done = t[i - 1] if i > 0 else t[0]
        else:
            break                                     # an unhedged first leg holds to settlement; no new pair on top of it
    payout = rec["up_qty"] if w.won_up else rec["dn_qty"]
    rec["cost"] = rec["up_cost"] + rec["dn_cost"]
    rec["net"] = payout - rec["cost"] - rec["fees"]
    rec["won_up"] = bool(w.won_up)
    return rec


def run(wins, P, draws):
    out = []
    for d in range(draws):
        ex = Execution(seed=d, size=SIZE)
        for w in wins:
            r = simulate(w, ex, P); r["draw"] = d; out.append(r)
    return out


def summary(recs, draws):
    traded = [r for r in recs if r["legs"] > 0]
    pairs = [p for r in traded for p in r["pairs"]]
    done = [p for p in pairs if p["hedge_px"] is not None]
    nets = [r["net"] for r in traded]
    dd = 0.0
    for d in range(draws):
        cum = peak = worst = 0.0
        for r in [x for x in recs if x["draw"] == d and x["legs"] > 0]:
            cum += r["net"]; peak = max(peak, cum); worst = max(worst, peak - cum)
        dd += worst / draws
    mean = (sum(nets) / len(nets)) if nets else float("nan")
    sd = (sum((x - mean) ** 2 for x in nets) / max(len(nets) - 1, 1)) ** 0.5 if len(nets) > 1 else float("nan")
    return {
        "markets": len(recs) / draws, "traded": len(traded) / draws, "orders": sum(r["orders"] for r in recs) / draws,
        "fills": sum(r["fills"] for r in recs) / draws,
        "fill_rate": sum(r["fills"] for r in recs) / max(1, sum(r["orders"] for r in recs)),
        "pairs": len(done) / draws, "incomplete": (len(pairs) - len(done)) / draws,
        "avg_first": (sum(p["first_px"] for p in pairs) / len(pairs)) if pairs else float("nan"),
        "avg_second": (sum(p["hedge_px"] for p in done) / len(done)) if done else float("nan"),
        "avg_pair_cost": (sum(p["pair_cost"] for p in done) / len(done)) if done else float("nan"),
        "fees": sum(r["fees"] for r in recs) / draws, "net": sum(nets) / draws,
        "per_pair": (sum(nets) / len(done)) if done else float("nan"),
        "per_market": mean, "win_rate": (sum(1 for x in nets if x > 0) / len(nets)) if nets else float("nan"),
        "dd": dd, "t": (mean / (sd / math.sqrt(len(nets)))) if len(nets) > 1 and sd > 0 else float("nan"), "n": len(nets),
        "rejected_pair_cost": sum(r["rejected_pair_cost"] for r in recs) / draws,
    }


def f2(x):
    return "—" if x != x else f"{x:+.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=2)
    args = ap.parse_args()
    d, meta = load_asset(CACHE, "BTC")
    by_period = defaultdict(list)
    for w in windows(d, meta):
        per = PERIOD.get(w.split)
        if per is not None and w.won_up is not None:
            by_period[per].append(w)
    for per in by_period:
        by_period[per].sort(key=lambda w: w.close)
    del d
    print("# BTC momentum-reversal PAIRS backtest\n")
    print("Windows: " + ", ".join(f"{p} {len(by_period[p])}" for p in PERIODS) + f". First leg: |z5| >= trend_z with the 1 s "
          f"return agreeing, {SIZE:g} shares FOK at ask + 1c (ask <= {MAX_FIRST_PRICE}), up to {FIRST_ATTEMPTS} attempts; hedge "
          f"after the reversal is confirmed for confirm_s, only when first + ask + fee + slippage <= max_pair_cost; up to "
          f"{MAX_PAIRS} pairs per market; an unhedged first leg holds to settlement. {args.draws} latency draws.\n")
    dev = by_period["train"] + by_period["validation"]
    rows = []
    for tz, cs, mp, gap in itertools.product(*GRID.values()):
        P = {"trend_z": tz, "confirm_s": cs, "max_pair": mp, "min_gap_s": gap}
        s_tr = summary(run(by_period["train"], P, args.draws), args.draws)
        s_va = summary(run(by_period["validation"], P, args.draws), args.draws)
        rows.append((P, s_tr, s_va))
    print("## 1. Grid on TRAIN + VALIDATION (net $, pairs completed); the choice is made here only\n")
    print("| trend_z | confirm_s | max_pair | min_gap | train net | pairs | validation net | pairs |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for P, a, b in sorted(rows, key=lambda r: -(r[1]["net"] + r[2]["net"])):
        print(f"| {P['trend_z']} | {P['confirm_s']} | {P['max_pair']} | {P['min_gap_s']} | {f2(a['net'])} | {a['pairs']:.0f} | {f2(b['net'])} | {b['pairs']:.0f} |")
    ok = [r for r in rows if r[1]["pairs"] >= 10 and r[2]["pairs"] >= 3]
    best = max(ok or rows, key=lambda r: r[1]["net"] + r[2]["net"])
    P = best[0]
    LOCK.write_text(json.dumps({"chosen": P, "rule": "max train+validation net with >= 10 / 3 completed pairs"}, indent=1))
    print(f"\nChosen (locked to {LOCK.name}): {P}\n")
    print("## 2. The chosen configuration, every period (TEST and FORWARD untouched by the choice)\n")
    keys = [("markets", "BTC markets"), ("traded", "markets traded"), ("orders", "orders"), ("fills", "fills"), ("fill_rate", "fill rate"),
            ("pairs", "completed pairs"), ("incomplete", "incomplete first legs"), ("avg_first", "avg first-leg price"),
            ("avg_second", "avg second-leg price"), ("avg_pair_cost", "avg pair cost (prices)"), ("fees", "fees $"),
            ("net", "net P&L $"), ("per_pair", "P&L per completed pair $"), ("per_market", "P&L per traded market $"),
            ("win_rate", "win rate (markets)"), ("dd", "max drawdown $"), ("t", "t-stat (per market)"), ("rejected_pair_cost", "hedge checks over the cap")]
    per = {p: summary(run(by_period[p], P, args.draws), args.draws) for p in PERIODS}
    print("| metric | " + " | ".join(PERIODS) + " |")
    print("|---|" + "---:|" * len(PERIODS))
    for k, lab in keys:
        cells = []
        for p in PERIODS:
            v = per[p][k]
            cells.append(f"{v:.1%}" if k in ("fill_rate", "win_rate") else (f"{v:.3f}" if k in ("avg_first", "avg_second", "avg_pair_cost") else (f"{v:.0f}" if k in ("markets", "traded", "orders", "fills", "pairs", "incomplete", "rejected_pair_cost") else f2(v))))
        print(f"| {lab} | " + " | ".join(cells) + " |")
    print("\n## 3. By window (FORWARD, chosen configuration, draw 0): the 15 best and 15 worst markets\n")
    recs = [r for r in run(by_period["forward"], P, 1) if r["legs"] > 0]
    recs.sort(key=lambda r: r["net"])
    print("| market | legs | pairs | first px | second px | pair cost | net $ |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for r in recs[:15] + recs[-15:]:
        done = [p for p in r["pairs"] if p["hedge_px"] is not None]
        print(f"| {r['slug']} | {r['legs']} | {len(done)} | {r['pairs'][0]['first_px']:.2f} | "
              f"{(sum(p['hedge_px'] for p in done) / len(done)):.2f} | {(sum(p['pair_cost'] for p in done) / len(done)):.3f} | {f2(r['net'])} |"
              if done else f"| {r['slug']} | {r['legs']} | 0 | {r['pairs'][0]['first_px']:.2f} | — | — | {f2(r['net'])} |")
    hours = defaultdict(list)
    for r in recs:
        hours[int((r["slug"].split("-")[-1]) ) // 3600 % 24].append(r["net"])
    print("\nFORWARD net by UTC hour: " + ", ".join(f"{h:02d}h {sum(v):+.1f} ({len(v)})" for h, v in sorted(hours.items())))


if __name__ == "__main__":
    main()
