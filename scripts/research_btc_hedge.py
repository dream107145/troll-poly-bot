"""BTC_TREND_REVERSAL_HEDGE research: one mandatory entry per BTC 5-minute market on the
short-term BTC direction, then a price-gated hedge of the other side after a confirmed reversal;
everything held to settlement.

    ionice -c3 nice -n 19 python scripts/research_btc_hedge.py > results/btc_hedge_backtest.md

RESEARCH ONLY, read-only on the archive (research cache: every archive point ~220 ms apart,
both tokens' touch and depth, composite spot, TwoScaleVol sigma). Execution is the validated
venue_lag model (backtest.spot_shock_replay.Execution): presend and half the venue round trip
drawn from the measured priors, the venue book read 500 ms after arrival, FOK at the limit with
5 shares behind it, taker fee on every fill, settlement free.

Per BTC market:
  observe    ``obs_s`` after the open (default 10 s); the 1 s and 5 s spot returns in sigma
  initial    side = the 5 s direction when |z5| >= ``trend_z`` and the 1 s return agrees, else
             the cheaper executable ask (CHEAPEST_SIDE_FALLBACK); FOK 5 shares at ask + 1c,
             ask <= ``max_initial_price``; retried every 2 s, up to ``initial_attempts`` times,
             until filled -- the one mandatory trade
  reversal   the 5 s return points AGAINST the held side by >= ``trend_z`` for ``confirm_s``
             consecutive seconds (several observations, never one tick)
  hedge      A) never; B) at the first confirmed reversal, whatever the price; C) only when
             pair_cost = avg first-leg price + opposite ask + fee(ask) + slippage + latency
             allowance <= ``max_pair_cost``, re-checked on every later tick while unmatched.
             Size = the unmatched quantity; FOK at ask + 1c; then hold to settlement
  settle     matched pairs pay 1; the unmatched side pays 1 / 0; entry fees only

``max_pair_cost`` is chosen on train + validation from {0.94, 0.95, 0.96, 0.97, 0.98}; test and
forward are reported untouched. Nothing else is tuned.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.backtest.spot_shock_replay import Execution, load_asset, windows   # noqa: E402

CACHE = ROOT / "data/replay/rm_research_cache"
LOCK = ROOT / "data/replay/btc_hedge_locked.json"
PERIOD = {"train": "train", "validation": "validation", "unseen_1": "test", "unseen_2": "test", "unseen_3": "forward"}
PERIODS = ("train", "validation", "test", "forward")

OBS_S = 10.0
TREND_Z = 1.0
CONFIRM_S = 3.0
SIZE = 5.0
LIMIT_OFFSET = 0.01
MAX_INITIAL_PRICE = 0.65
INITIAL_ATTEMPTS = 8
RETRY_MS = 2000.0
SLIPPAGE = 0.005
LATENCY_ALLOWANCE = 0.005
MIN_LEFT_S = 15.0
PAIR_COSTS = (0.94, 0.95, 0.96, 0.97, 0.98)


def zret(t, lp, sigma, lag_ms):
    j = np.searchsorted(t, t - lag_ms, side="right") - 1
    ok = (j >= 0) & (t - t[np.maximum(j, 0)] <= lag_ms + 1500.0)
    r = np.where(ok, lp - lp[np.maximum(j, 0)], np.nan)
    return r / (np.maximum(sigma, 1e-7) * math.sqrt(lag_ms / 1000.0))


def simulate(w, ex: Execution, variant: str, max_pair: float) -> dict:
    """One market. variant: A (no hedge), B (unconditional), C (price-gated)."""
    t, left = w.t, w.left
    z1, z5 = zret(t, w.lp, w.sigma, 1000.0), zret(t, w.lp, w.sigma, 5000.0)
    close = w.close
    open_ts = close - 300_000.0
    rec = {"slug": w.slug, "initial": None, "side": None, "reason": None, "attempts": 0, "first_qty": 0.0,
           "first_cost": 0.0, "hedge_qty": 0.0, "hedge_cost": 0.0, "fees": 0.0, "reversal": False,
           "hedge_checks": 0, "hedge_attempts": 0, "pair_cost": None, "won": None, "net": 0.0,
           "z1": None, "z5": None, "first_px": None, "hedge_px": None}
    i = int(np.searchsorted(t, open_ts + OBS_S * 1000.0))
    n = len(t)
    # ── the initial entry
    attempts, last_try, side = 0, -1e18, None
    while i < n and attempts < INITIAL_ATTEMPTS and left[i] > MIN_LEFT_S:
        if t[i] - last_try < RETRY_MS:
            i += 1
            continue
        a_up, a_dn = w.side["UP"]["ask"][i], w.side["DOWN"]["ask"][i]
        if a_up != a_up and a_dn != a_dn:
            i += 1
            continue
        if side is None:
            if z5[i] == z5[i] and abs(z5[i]) >= TREND_Z and z1[i] == z1[i] and z1[i] * z5[i] > 0:
                side, reason = ("UP" if z5[i] > 0 else "DOWN"), ("TREND_UP" if z5[i] > 0 else "TREND_DOWN")
            else:
                if a_up != a_up or a_dn != a_dn:
                    i += 1
                    continue
                side, reason = ("UP" if a_up <= a_dn else "DOWN"), "CHEAPEST_SIDE_FALLBACK"
            rec.update(side=side, reason=reason, z1=None if z1[i] != z1[i] else float(z1[i]),
                       z5=None if z5[i] != z5[i] else float(z5[i]))
        sv = w.side[side]
        ask = sv["ask"][i]
        if ask != ask or ask > MAX_INITIAL_PRICE + 1e-9:
            i += 1
            continue
        attempts += 1
        last_try = t[i]
        r = ex.buy(w, sv, t[i], round(ask + LIMIT_OFFSET, 2))
        if r["filled"]:
            rec.update(initial=True, first_qty=SIZE, first_cost=r["entry"] * SIZE, first_px=r["entry"],
                       fees=w.fee(r["entry"]) * SIZE)
            i = w.first_at(r["t_known"])
            break
        i = w.first_at(r["t_known"])
    rec["attempts"] = attempts
    if not rec["initial"]:
        rec["initial"] = False
        return rec
    # ── after the entry: reversal, hedge
    other = "DOWN" if side == "UP" else "UP"
    want = -1 if side == "UP" else 1                    # the sign of z5 that is AGAINST the held side
    against_since = None
    hedged = 0.0
    last_hedge_try = -1e18
    while i < n and left[i] > MIN_LEFT_S and hedged + 1e-9 < rec["first_qty"]:
        if z5[i] == z5[i] and z5[i] * want >= TREND_Z:
            against_since = t[i] if against_since is None else against_since
        else:
            against_since = None
        confirmed = against_since is not None and t[i] - against_since >= CONFIRM_S * 1000.0
        if confirmed and not rec["reversal"]:
            rec["reversal"] = True
        if rec["reversal"] and variant != "A" and t[i] - last_hedge_try >= RETRY_MS:
            osv = w.side[other]
            ask = osv["ask"][i]
            if ask == ask:
                avg_first = rec["first_cost"] / rec["first_qty"]
                pair = avg_first + ask + w.fee(ask) + SLIPPAGE + LATENCY_ALLOWANCE
                rec["hedge_checks"] += 1
                if rec["pair_cost"] is None or pair < rec["pair_cost"]:
                    rec["pair_cost"] = pair
                if variant == "B" or pair <= max_pair + 1e-9:
                    rec["hedge_attempts"] += 1
                    last_hedge_try = t[i]
                    r = ex.buy(w, osv, t[i], round(ask + LIMIT_OFFSET, 2))
                    if r["filled"]:
                        hedged += SIZE
                        rec.update(hedge_qty=hedged, hedge_cost=rec["hedge_cost"] + r["entry"] * SIZE,
                                   hedge_px=r["entry"], fees=rec["fees"] + w.fee(r["entry"]) * SIZE)
                    i = w.first_at(r["t_known"])
                    continue
        i += 1
    # ── settlement
    won = w.won_up == (side == "UP")
    rec["won"] = won
    matched = min(rec["first_qty"], rec["hedge_qty"])
    payout = matched + (rec["first_qty"] - matched) * (1.0 if won else 0.0) + (rec["hedge_qty"] - matched) * (0.0 if won else 1.0)
    rec["matched"], rec["unmatched"] = matched, rec["first_qty"] - matched
    rec["payout"] = payout
    rec["net"] = payout - rec["first_cost"] - rec["hedge_cost"] - rec["fees"]
    return rec


def run(wins, variant, max_pair, draws=3):
    recs = []
    for d in range(draws):
        ex = Execution(seed=d, size=SIZE)
        for w in wins:
            r = simulate(w, ex, variant, max_pair)
            r["draw"] = d
            recs.append(r)
    return recs


def summary(recs, draws):
    n = len(recs) / draws
    init = [r for r in recs if r["initial"]]
    hed = [r for r in init if r["hedge_qty"] > 0]
    rev = [r for r in init if r["reversal"]]
    nets = [r["net"] for r in init]
    # drawdown and losing streak in time order, per draw, averaged
    dd = streak = 0.0
    for d in range(draws):
        cum = peak = worst = 0.0
        run_l = best_l = 0
        for r in [x for x in recs if x["draw"] == d and x["initial"]]:
            cum += r["net"]; peak = max(peak, cum); worst = max(worst, peak - cum)
            run_l = run_l + 1 if r["net"] < 0 else 0
            best_l = max(best_l, run_l)
        dd += worst / draws; streak += best_l / draws
    s = {
        "markets": n, "with_initial": len(init) / draws, "initial_rate": len(init) / max(len(recs), 1),
        "fills": (len(init) + len(hed)) / draws, "hedge_rate": len(hed) / max(len(rev), 1),
        "reversal_rate": len(rev) / max(len(init), 1),
        "hedge_completion": (sum(1 for r in hed if r["hedge_qty"] >= r["first_qty"] - 1e-9) / len(hed)) if hed else float("nan"),
        "avg_initial_px": (sum(r["first_px"] for r in init) / len(init)) if init else float("nan"),
        "avg_hedge_px": (sum(r["hedge_px"] for r in hed) / len(hed)) if hed else float("nan"),
        "avg_pair_cost": (sum(r["first_px"] + r["hedge_px"] for r in hed) / len(hed)) if hed else float("nan"),
        "fees": sum(r["fees"] for r in init) / draws, "slippage": (len(init) + len(hed)) * SLIPPAGE * SIZE / draws,
        "net": sum(nets) / draws, "per_market": (sum(nets) / len(nets)) if nets else float("nan"),
        "win_rate": (sum(1 for x in nets if x > 0) / len(nets)) if nets else float("nan"),
        "dd": dd, "streak": streak,
        "unmatched_usdc": (sum(r["unmatched"] * r["first_px"] for r in init) / len(init)) if init else float("nan"),
        "by_side": {s_: (sum(r["net"] for r in init if r["side"] == s_) / draws) for s_ in ("UP", "DOWN")},
        "by_reason": {k: (sum(r["net"] for r in init if r["reason"] == k) / draws, sum(1 for r in init if r["reason"] == k) / draws)
                      for k in ("TREND_UP", "TREND_DOWN", "CHEAPEST_SIDE_FALLBACK")},
        "rev_net": sum(r["net"] for r in rev) / draws, "norev_net": sum(r["net"] for r in init if not r["reversal"]) / draws,
        "rev_n": len(rev) / draws, "norev_n": (len(init) - len(rev)) / draws,
    }
    return s


def table(rows, head):
    print("| " + " | ".join(head) + " |")
    print("|" + "|".join("---:" if i else "---" for i in range(len(head))) + "|")
    for r in rows:
        print("| " + " | ".join(str(x) for x in r) + " |")
    print()


def f2(x):
    return "—" if x != x else f"{x:+.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=3)
    args = ap.parse_args()
    d, meta = load_asset(CACHE, "BTC")
    per = defaultdict(list)
    for w in windows(d, meta):
        p = PERIOD.get(w.split)
        if p is not None and w.won_up is not None:
            per[p].append(w)
    for p in per:
        per[p].sort(key=lambda w: w.close)
    del d
    print("# BTC trend-reversal-hedge backtest\n")
    print("BTC 5-minute windows with a venue outcome: " + ", ".join(f"{p} {len(per[p]):,}" for p in PERIODS)
          + f". Observation {OBS_S:g} s, trend {TREND_Z:g} σ over 5 s, reversal confirmed over {CONFIRM_S:g} s, "
          f"5-share FOK at ask + 1c, initial ask ≤ {MAX_INITIAL_PRICE}, {INITIAL_ATTEMPTS} attempts; "
          f"{args.draws} latency draws.\n")

    print("## 1. max_pair_cost (variant C), chosen on train + validation\n")
    rows, best = [], None
    for mp in PAIR_COSTS:
        res = {p: summary(run(per[p], "C", mp, args.draws), args.draws) for p in PERIODS}
        score = res["train"]["net"] + res["validation"]["net"]
        if best is None or score > best[0]:
            best = (score, mp)
        rows.append((f"{mp:.2f}", *(f"{res[p]['net']:+.0f} (hedged {100 * res[p]['hedge_rate']:.0f}% of reversals)" for p in PERIODS)))
    table(rows, ["max_pair_cost", *PERIODS])
    chosen = best[1]
    LOCK.write_text(json.dumps({"max_pair_cost": chosen, "rule": "max train+validation net", "grid": PAIR_COSTS}, indent=1))
    print(f"Chosen: **{chosen:.2f}** (`data/replay/btc_hedge_locked.json`). Test and forward were not read for this choice.\n")

    print("## 2. The three variants\n")
    keys = [("markets", "markets", "{:.0f}"), ("with_initial", "with initial trade", "{:.0f}"),
            ("initial_rate", "initial trade rate", "{:.1%}"), ("fills", "fills", "{:.0f}"),
            ("reversal_rate", "reversal rate", "{:.1%}"), ("hedge_rate", "hedge rate (of reversals)", "{:.1%}"),
            ("hedge_completion", "hedge completion", "{:.1%}"), ("avg_initial_px", "avg initial price", "{:.3f}"),
            ("avg_hedge_px", "avg hedge price", "{:.3f}"), ("avg_pair_cost", "avg pair cost (prices)", "{:.3f}"),
            ("fees", "fees $", "{:.2f}"), ("slippage", "slippage allowance $", "{:.2f}"),
            ("net", "**net P&L $**", "**{:+.2f}**"), ("per_market", "expectancy / market $", "{:+.3f}"),
            ("win_rate", "win rate (markets)", "{:.1%}"), ("dd", "max drawdown $", "{:.2f}"),
            ("unmatched_usdc", "avg unmatched exposure $", "{:.2f}"), ("streak", "longest losing streak", "{:.1f}")]
    allres = {}
    for variant, label in (("A", "A) trend-only"), ("B", "B) trend + unconditional hedge"), ("C", f"C) trend + hedge if pair ≤ {chosen:.2f}")):
        res = {p: summary(run(per[p], variant, chosen, args.draws), args.draws) for p in PERIODS}
        allres[variant] = res
        print(f"### {label}\n")
        rows = []
        for k, lab, fmt in keys:
            cells = []
            for p in PERIODS:
                v = res[p][k]
                cells.append("—" if isinstance(v, float) and v != v else fmt.format(v))
            rows.append((lab, *cells))
        table(rows, ["metric", *PERIODS])
        rows = []
        for p in PERIODS:
            s = res[p]
            rows.append((p, f2(s["by_side"]["UP"]), f2(s["by_side"]["DOWN"]),
                         *(f"{f2(s['by_reason'][k][0])} ({s['by_reason'][k][1]:.0f})" for k in ("TREND_UP", "TREND_DOWN", "CHEAPEST_SIDE_FALLBACK")),
                         f"{f2(s['rev_net'])} ({s['rev_n']:.0f})", f"{f2(s['norev_net'])} ({s['norev_n']:.0f})"))
        table(rows, ["period", "net: initial UP", "net: initial DOWN", "net: TREND_UP (n)", "net: TREND_DOWN (n)",
                     "net: CHEAPEST fallback (n)", "net: reversal markets (n)", "net: no-reversal markets (n)"])

    print("## 3. Does the mandatory initial trade have negative expectancy?\n")
    a = allres["A"]
    print("Variant A is exactly that trade alone (held to settlement, one fee): expectancy per market "
          + ", ".join(f"{p} {a[p]['per_market']:+.3f} $" for p in PERIODS) + ". By how the side was chosen, "
          "see the TREND / CHEAPEST columns above.\n")


if __name__ == "__main__":
    main()
