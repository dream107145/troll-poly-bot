"""SWING RECOVERY backtest (the user's 2026-10-07 spec: strategy/pairlock_swing.py, the recovery paper bot's swing mode)
on every recorded BTC 5-minute window (data/charts/btc-updown-5m-*.json: best bid / ask and the depth within 2 cents of
the touch, ~5 samples a second, the venue's outcome).

The DECISIONS are the bot's own code -- pairlock_swing.entry_decision / evaluate / observe on every recorded sample,
the same RecoveryParams the paper bot runs -- only the FILLS are modelled, four ways:

    theoretical   every order fills in full at once: the entry at the decision's best ask, every swing and hedge buy at
                  exactly SWING_RECOVERY_PRICE (0.70); no fees, no depth or capital limit. The spec's arithmetic on each
                  market's real swing path -- NOT a realistic result.
    vwap          every order fills at once at the VWAP of its size walked through the recorded ask levels at the
                  decision (the best ask and two more cents, the recorded depth within 2 c split evenly over them);
                  no fees; the strategy's depth / capital / position bounds apply.
    vwap_fees     ... plus the venue's taker fee, 0.07 p (1 - p) per share.
    realistic     the paper bot's execution model (scenario "calibrated", PaperExchange): the order arrives 143 ms after
                  the decision and takes the book THEN (80 % of the touch level taken by faster traders, deeper levels as
                  shown), fill-and-kill up to its limit; the book 1 s after arrival must still hold the filled size at or
                  under the limit, else the order is killed, and the fill is charged at the worse of the two VWAPs; fees
                  in; the result is known 1.1 s after arrival (no new order meanwhile; swings are still tracked).

Depth beyond 2 cents of the touch is not recorded and is taken as ZERO: a large order (80 shares) is limited by what
the recording shows. No parameter is fitted, so there is no train/test split; the chronological quarters and the days
show whether the result is stable.

    .venv/bin/python scripts/pairlock/replay_swing.py [--procs 3] [--cache DIR] [--out results/...json]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import replace
from multiprocessing import Pool

import numpy as np

ROOT = "/root/troll-poly-bot"
sys.path.insert(0, f"{ROOT}/src")
from troll_poly_bot.strategy import pairlock_recovery as rc     # noqa: E402
from troll_poly_bot.strategy import pairlock_swing as sw        # noqa: E402
from troll_poly_bot.strategy.pairlock import MarketView         # noqa: E402
from troll_poly_bot.strategy.pairlock_book import FillRecord, PairBook, fee_per_share   # noqa: E402

LAT_MS, ACK_MS, SURVIVAL_MS = 143.0, 112.0, 1000.0     # the pair-lock replays' measured latency; scenario "calibrated"
CONTENTION = 0.80                                       # share of the touch level taken first (the latency profile's)
BANKROLL = 1_000.0                                      # per market: the bankroll never binds here (capital is reported)
NO_FEE = lambda p: 0.0                                  # noqa: E731
COLS = ("t", "ub", "ua", "db", "da", "ubd", "uad", "dbd", "dad")

#: the paper bot's parameters (ecosystem.pairlockrecovery.config.js; the swing spec's defaults)
#: (2026-10-08: the bot sizes recoveries to a target profit now; this replay keeps the 2026-10-07 spec's FIXED 24 / 80, so
#: docs/pairlock_swing_backtest_2026-10-07.md stays reproducible -- target sizing: scripts/pairlock/replay_swing_targets.py)
BASE = rc.RecoveryParams(swing_mode=True, swing_sizing="fixed", max_total_position=300.0, max_slippage=0.03, min_time_left_s=15.0, cooldown_ms=2000.0,
                         depth_haircut=0.0, min_size=5.0, open_max_tries=3)


# ───────────────────────────────── data ─────────────────────────────────


def load(cache_dir: str) -> list[dict]:
    path = os.path.join(cache_dir, "swing_windows.pkl")
    files = sorted(glob.glob(f"{ROOT}/data/charts/btc-updown-5m-*.json"))
    if os.path.exists(path):
        with open(path, "rb") as f:
            ws = pickle.load(f)
        if len(ws) and ws[-1]["slug"] == os.path.basename(files[-1])[:-5]:
            return ws
    out = []
    t0 = time.time()
    for fn in files:
        w = window_from_file(fn)
        if w is not None:
            out.append(w)
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(out, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"{len(out)} windows loaded in {time.time() - t0:.0f} s", flush=True)
    return out


def window_from_file(fn: str) -> dict | None:
    """One recorded market as arrays (NaN = missing); None when it has no venue outcome or too few samples."""
    with open(fn) as f:
        d = json.load(f)
    oc = (d.get("outcome") or "").upper()
    pts = d.get("points") or []
    if oc not in ("UP", "DOWN") or len(pts) < 300 or not d.get("open_ts"):
        return None
    g = lambda p, k, alt=None: p.get(k, p.get(alt) if alt else None)      # noqa: E731
    col = {"t": [p["t"] for p in pts], "ub": [p.get("ub") for p in pts], "ua": [p.get("ua") for p in pts],
           "db": [p.get("db") for p in pts], "da": [p.get("da") for p in pts], "ubd": [p.get("ubd") for p in pts],
           "uad": [p.get("uad") for p in pts], "dbd": [g(p, "dbd", "uad") for p in pts], "dad": [g(p, "dad", "ubd") for p in pts]}
    arr = {k: np.array([np.nan if x is None else float(x) for x in v], dtype=np.float64) for k, v in col.items()}
    return {"slug": d["slug"], "open": float(d["open_ts"]), "close": float(d["close_ts"]), "outcome": oc, **arr}


def _f(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def ask_levels(ask, depth, touch_share: float = 1.0) -> list[tuple[float, float]]:
    """The best ask and two more cents, the recorded depth within 2 c split evenly; ``touch_share`` of the touch level."""
    if ask is None:
        return []
    d = max(0.0, depth or 0.0) / 3.0
    lv: dict[float, float] = {}
    for k in range(3):
        p = round(min(ask + 0.01 * k, 0.99), 2)
        lv[p] = lv.get(p, 0.0) + d * (touch_share if k == 0 else 1.0)
    return sorted(lv.items())


def view_at(w: dict, i: int, theoretical: bool = False) -> MarketView:
    ua, ub, da, db = _f(w["ua"][i]), _f(w["ub"][i]), _f(w["da"][i]), _f(w["db"][i])
    if theoretical:
        return MarketView(bid_up=ub, ask_up=ua, bid_down=db, ask_down=da, asks_up=[(ua, 1e9)] if ua else [],
                          asks_down=[(da, 1e9)] if da else [], bids_up=[(ub, 1e9)] if ub else [], bids_down=[(db, 1e9)] if db else [])
    return MarketView(bid_up=ub, ask_up=ua, bid_down=db, ask_down=da, asks_up=ask_levels(ua, _f(w["uad"][i])),
                      asks_down=ask_levels(da, _f(w["dad"][i])), bids_up=[(ub, _f(w["ubd"][i]) or 0.0)] if ub else [],
                      bids_down=[(db, _f(w["dbd"][i]) or 0.0)] if db else [])


# ───────────────────────────────── execution ─────────────────────────────────


def _take(levels, q, limit):
    out, left = [], q
    for p, s in levels:
        if p > limit + 1e-9 or left <= 1e-9:
            break
        x = min(left, s)
        if x > 1e-9:
            out.append((p, x))
            left -= x
    return out


def execute(mode: str, w: dict, i: int, dec: sw.SwingOrder, prm, fees, ask_shift: float = 0.0) -> dict:
    """The order's fate: {status, qty, vwap, fee, t_result}. ``ask_shift`` (stress tests, scripts/pairlock/replay_swing_sweep.py):
    the asks the order meets at arrival and 1 s later are that much higher (0 = the recorded book)."""
    t = float(w["t"][i])
    if mode == "theoretical":
        px = dec.vwap if dec.kind == "entry" else float(prm.swing_recovery_price)
        return {"status": "filled", "qty": dec.shares, "vwap": px, "fee": 0.0, "t_result": t}
    if mode in ("vwap", "vwap_fees"):
        return {"status": "filled", "qty": dec.shares, "vwap": dec.vwap, "fee": dec.fee_ps * dec.shares, "t_result": t}
    ts = w["t"]
    t_arr = t + LAT_MS
    j = int(np.searchsorted(ts, t_arr, side="left"))
    if j >= len(ts) or ts[j] >= w["close"]:
        return {"status": "market_closed", "t_result": t_arr + ACK_MS}
    k_ask, k_dep = ("ua", "uad") if dec.side == "UP" else ("da", "dad")
    shift = lambda a: a if a is None or not ask_shift else a + ask_shift     # noqa: E731
    matched = _take(ask_levels(shift(_f(w[k_ask][j])), _f(w[k_dep][j]), 1.0 - CONTENTION), dec.shares, dec.limit)
    if not matched:
        return {"status": "no_liquidity", "t_result": float(ts[j]) + ACK_MS}
    q = sum(x for _, x in matched)
    k = int(np.searchsorted(ts, t_arr + SURVIVAL_MS, side="left"))
    if k >= len(ts) or ts[k] >= w["close"]:
        return {"status": "quote_pulled", "t_result": t_arr + SURVIVAL_MS + ACK_MS}
    later = _take(ask_levels(shift(_f(w[k_ask][k])), _f(w[k_dep][k])), q, dec.limit)
    if sum(x for _, x in later) + 1e-9 < q:
        return {"status": "quote_pulled", "t_result": t_arr + SURVIVAL_MS + ACK_MS}
    vw = lambda m: sum(p * x for p, x in m) / sum(x for _, x in m)      # noqa: E731
    use = later if vw(later) > vw(matched) else matched
    return {"status": "filled", "qty": q, "vwap": vw(use), "fee": sum(x * fees(p) for p, x in use),
            "t_result": t_arr + SURVIVAL_MS + ACK_MS}


# ───────────────────────────────── one market ─────────────────────────────────


def run_window(w: dict, mode: str, prm) -> dict:
    fees = NO_FEE if mode in ("theoretical", "vwap") else fee_per_share
    if mode == "theoretical":
        prm = replace(prm, swing_max_market_capital=1e9, max_total_position=1e9)
    theo = mode == "theoretical"
    ts = w["t"]
    n = len(ts)
    book = PairBook(w["slug"])
    st: sw.SwingState | None = None
    pend = None
    tries = 0
    cnt = defaultdict(float)
    seq = 0
    i0 = int(np.searchsorted(ts, w["open"] + 1000.0 * float(prm.swing_entry_delay_s) - 1.0, side="left"))
    last_entry_t = w["open"] + 1000.0 * float(prm.swing_entry_window_s)

    def resolve(p):
        nonlocal st, seq
        dec, r = p["dec"], p["res"]
        cnt[f"{dec.kind}_sent"] += 1
        if r["status"] != "filled":
            cnt[f"{dec.kind}_killed"] += 1
            if dec.kind == "entry" and not book.fills:
                st = None                                   # the engine drops a cycle whose first buy filled nothing
            return
        seq += 1
        cnt[f"{dec.kind}_filled"] += 1
        cnt[f"{dec.kind}_partial"] += int(r["qty"] < dec.shares - 1e-9)
        book.add_fill(FillRecord(w["slug"], r["t_result"], dec.side.lower(), dec.side, r["qty"], r["vwap"], r["fee"],
                                 f"o{seq}", f"o{seq}-1", "BUY", dec.vwap, dec.kind))
        sw.on_fill(st, dec.kind, dec.side, r["qty"], r["vwap"], r["fee"], r["t_result"], dec.swing)

    for i in range(i0, n):
        t = float(ts[i])
        if t >= w["close"]:
            break
        if pend is not None and t >= pend["res"]["t_result"]:
            resolve(pend)
            pend = None
        if st is None and (t > last_entry_t or tries >= prm.open_max_tries) and pend is None:
            break                                           # no entry: no trade in this market
        mv = view_at(w, i, theo)
        if mv.ask_up is None or mv.ask_down is None or mv.bid_up is None or mv.bid_down is None:
            continue
        if pend is not None:
            if st is not None and book.fills:
                sw.observe(st, mv, t)
            continue
        left = (w["close"] - t) / 1000.0
        elapsed = (t - w["open"]) / 1000.0
        if st is None or not book.fills:
            dec, _ = sw.entry_decision(mv, elapsed, left, prm, BANKROLL, now=t, fees=fees)
            if dec is None:
                continue
            tries += 1
            if st is None:
                st = sw.SwingState.start(dec.side, t, prm)
        else:
            dec, _ = sw.evaluate(st, book, mv, t, left, elapsed, prm, BANKROLL, fees)
            if dec is None:
                continue
        sw.on_sent(st, dec.kind, dec.swing, t)
        pend = {"dec": dec, "res": execute(mode, w, i, dec, prm, fees)}
        if pend["res"]["t_result"] <= t:
            resolve(pend)
            pend = None
    if pend is not None and pend["res"]["status"] == "filled":
        resolve(pend)                                       # arrived before the close: the fill stands
    out = {"slug": w["slug"], "open": w["open"], "outcome": w["outcome"], "traded": 0, **cnt}
    if st is None or not book.fills:
        return out
    fin = sw.on_settled(st, book, w["outcome"], w["close"])
    A = st.initial_token
    e_cost = float(st.entry.get("cost", 0.0))
    rows = {s["n"]: s for s in st.swings}
    out.update({"traded": 1, "bucket": fin["bucket"], "swings": st.swing_number, "pnl": fin["pnl"], "capital": book.cash_spent,
                "fees": book.fees, "initial": A, "a_won": int(w["outcome"] == A), "entry_qty": float(st.entry.get("filled", 0.0)),
                "entry_vwap": st.entry.get("vwap"), "first_leg_pnl": float(st.entry.get("filled", 0.0)) * (w["outcome"] == A) - e_cost,
                "up": book.up_qty, "down": book.down_qty, "balanced": int(fin["balanced"]),
                "hedge_qty": float((st.hedge or {}).get("filled", 0.0)), "hedge_vwap": (st.hedge or {}).get("vwap"),
                "hedge_needed": int(st.hedge is not None and st.plan["two_sided_hedge"]),
                "s1_qty": float(rows.get(1, {}).get("filled", 0.0)), "s1_vwap": rows.get(1, {}).get("vwap"),
                "s1_ask": rows.get(1, {}).get("ask"), "s2_qty": float(rows.get(2, {}).get("filled", 0.0)),
                "s2_vwap": rows.get(2, {}).get("vwap"), "s2_ask": rows.get(2, {}).get("ask"),
                "s3_left": None if 3 not in rows else (w["close"] - rows[3]["t"]) / 1000.0,
                "pnl_if_up": fin["pnl_if_up_wins"], "pnl_if_down": fin["pnl_if_down_wins"]})
    return out


# ───────────────────────────────── summary ─────────────────────────────────


def summarize(recs: list[dict]) -> dict:
    recs = sorted(recs, key=lambda r: r["open"])
    tr = [r for r in recs if r["traded"]]
    pnl = [r["pnl"] for r in tr]
    n = len(tr)
    eq = peak = dd = 0.0
    for r in tr:
        eq += r["pnl"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    mean = lambda xs: statistics.mean(xs) if xs else None      # noqa: E731
    by = {}
    for b in ("0", "1", "2", "3+"):
        g = [r for r in tr if r["bucket"] == b]
        by[b] = {"markets": len(g), "share": len(g) / n if n else None, "pnl": sum(r["pnl"] for r in g),
                 "avg_pnl": mean([r["pnl"] for r in g]), "wins": sum(1 for r in g if r["pnl"] > 1e-9),
                 "losses": sum(1 for r in g if r["pnl"] < -1e-9), "a_won": sum(r["a_won"] for r in g),
                 "worst": min((r["pnl"] for r in g), default=None), "avg_capital": mean([r["capital"] for r in g])}
    h = [r for r in tr if r["hedge_needed"]]
    se = statistics.pstdev(pnl) / math.sqrt(n) if n > 1 else 0.0
    s = lambda k: sum(float(r.get(k) or 0.0) for r in recs)       # noqa: E731
    return {"markets_processed": len(recs), "markets_entered": n, "entry_orders": s("entry_sent"), "entry_kills": s("entry_killed"),
            "by_swing": by, "total_pnl": sum(pnl), "avg_pnl_per_market": mean(pnl), "se": se,
            "t": (mean(pnl) / se) if n > 1 and se > 0 else None,
            "win_rate": sum(1 for x in pnl if x > 1e-9) / n if n else None,
            "loss_rate": sum(1 for x in pnl if x < -1e-9) / n if n else None, "max_drawdown": dd,
            "worst_market": min(pnl, default=None), "best_market": max(pnl, default=None),
            "max_capital": max((r["capital"] for r in tr), default=0.0), "avg_capital": mean([r["capital"] for r in tr]),
            "fees": sum(r["fees"] for r in tr), "first_leg_alone_pnl": sum(r["first_leg_pnl"] for r in tr),
            "avg_entry_vwap": mean([r["entry_vwap"] for r in tr if r.get("entry_vwap")]),
            "avg_swing1_vwap": mean([r["s1_vwap"] for r in tr if r.get("s1_vwap")]),
            "avg_swing2_vwap": mean([r["s2_vwap"] for r in tr if r.get("s2_vwap")]),
            "swing1_full": sum(1 for r in tr if r["swings"] >= 1 and r["s1_qty"] >= float(BASE.swing1_shares) - 5 + 1e-9),
            "swing1_short": sum(1 for r in tr if r["swings"] >= 1 and r["s1_qty"] < float(BASE.swing1_shares) - 5 + 1e-9),
            "swing2_full": sum(1 for r in tr if r["swings"] >= 2 and r["s2_qty"] >= float(BASE.swing2_shares) - 5 + 1e-9),
            "swing2_short": sum(1 for r in tr if r["swings"] >= 2 and r["s2_qty"] < float(BASE.swing2_shares) - 5 + 1e-9),
            "hedge_markets": len(h), "hedges_balanced": sum(r["balanced"] for r in h),
            "hedges_uneven": sum(1 - r["balanced"] for r in h),
            "avg_hedge_vwap": mean([r["hedge_vwap"] for r in h if r.get("hedge_vwap")]),
            "avg_hedge_qty": mean([r["hedge_qty"] for r in h]),
            "recovery_orders": s("recovery_sent"), "recovery_kills": s("recovery_killed"),
            "recovery_partial": s("recovery_partial"), "hedge_orders": s("hedge_sent"), "hedge_kills": s("hedge_killed"),
            "hedge_partial": s("hedge_partial")}


def splits(recs: list[dict]) -> dict:
    recs = sorted(recs, key=lambda r: r["open"])
    q = len(recs)
    out = {}
    for k, (a, b) in enumerate(((0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0)), start=1):
        part = recs[int(a * q):int(b * q)]
        s = summarize(part)
        out[f"Q{k}"] = {"from": time.strftime("%m-%d %H:%M", time.gmtime(part[0]["open"] / 1000)),
                        "to": time.strftime("%m-%d %H:%M", time.gmtime(part[-1]["open"] / 1000)),
                        "entered": s["markets_entered"], "pnl": s["total_pnl"], "avg": s["avg_pnl_per_market"],
                        "swing3+": s["by_swing"]["3+"]["markets"]}
    days = defaultdict(list)
    for r in recs:
        days[time.strftime("%Y-%m-%d", time.gmtime(r["open"] / 1000))].append(r)
    out["days"] = {d: {"entered": sum(r["traded"] for r in v), "pnl": round(sum(r.get("pnl", 0.0) for r in v), 2)}
                   for d, v in sorted(days.items())}
    return out


_WS = None


def _job(arg):
    mode, label, overrides, lo, hi = arg
    prm = replace(BASE, **overrides)
    return label, [run_window(w, mode, prm) for w in _WS[lo:hi]]


RUNS = [  # (mode, label, RecoveryParams overrides)
    ("theoretical", "theoretical", {}),
    ("vwap", "vwap (no fees)", {}),
    ("vwap_fees", "vwap + fees", {}),
    ("realistic", "realistic (cap 125)", {}),
    ("realistic", "realistic (cap 150)", {"swing_max_market_capital": 150.0}),
    ("vwap_fees", "vwap + fees (cap 150)", {"swing_max_market_capital": 150.0}),
    ("realistic", "realistic, hedge off (cap 150)", {"swing_max_market_capital": 150.0, "swing_two_sided_hedge": False}),
]


def main():
    global _WS
    ap = argparse.ArgumentParser()
    ap.add_argument("--procs", type=int, default=3)
    ap.add_argument("--cache", default=os.environ.get("SWING_CACHE", "/tmp/claude-0/-root-troll-poly-bot/53e010f8-a35d-42fb-b329-fe76e0f4a11d/scratchpad/swing"))
    ap.add_argument("--out", default=f"{ROOT}/results/pairlock_swing_backtest_2026-10-07.json")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    _WS = load(a.cache)
    if a.limit:
        _WS = _WS[-a.limit:]
    t0 = time.time()
    chunk = max(1, len(_WS) // (a.procs * 4))
    jobs = [(m, lab, ov, lo, min(lo + chunk, len(_WS))) for m, lab, ov in RUNS for lo in range(0, len(_WS), chunk)]
    res = defaultdict(list)
    with Pool(a.procs) as pool:
        for label, recs in pool.imap_unordered(_job, jobs):
            res[label].extend(recs)
    print(f"{len(_WS)} windows x {len(RUNS)} runs in {time.time() - t0:.0f} s", flush=True)
    out = {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "windows": len(_WS),
           "first": time.strftime("%Y-%m-%d %H:%M", time.gmtime(_WS[0]["open"] / 1000)),
           "last": time.strftime("%Y-%m-%d %H:%M", time.gmtime(_WS[-1]["open"] / 1000)),
           "params": {k: v for k, v in vars(BASE).items() if k.startswith("swing") or k in
                      ("max_slippage", "min_time_left_s", "cooldown_ms", "max_total_position", "min_size", "open_max_tries")},
           "execution": {"latency_ms": LAT_MS, "ack_ms": ACK_MS, "survival_ms": SURVIVAL_MS, "touch_contention": CONTENTION},
           "runs": {}}
    for _m, label, ov in RUNS:
        recs = res[label]
        out["runs"][label] = {"overrides": ov, "summary": summarize(recs), "splits": splits(recs),
                              "worst": sorted((r for r in recs if r["traded"]), key=lambda r: r["pnl"])[:5]}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=1, default=str)
    for label, r in out["runs"].items():
        s = r["summary"]
        b = s["by_swing"]
        print(f"{label:32s} entered {s['markets_entered']:5d}  total {s['total_pnl']:+9.2f}  avg {s['avg_pnl_per_market'] or 0:+.3f}  "
              f"win {100 * (s['win_rate'] or 0):.1f}%  dd {s['max_drawdown']:+.1f}  maxcap {s['max_capital']:.1f}  | "
              + "  ".join(f"S{k} {v['markets']} {v['pnl']:+.1f}" for k, v in b.items()), flush=True)
    print("->", a.out)


if __name__ == "__main__":
    main()
