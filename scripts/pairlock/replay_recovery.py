"""Backtest of the DYNAMIC LOSS-RECOVERY engine (strategy/pairlock_recovery.py) against the current pair-lock strategy
(B as it runs, the frozen v2 code), on the same 1,649 BTC windows, the same entries (the crypto trigger and B's entry
gate), the same execution model (replay2.fok: arrival +143 ms, the size walked through the ask levels with 80 % taken
by faster traders, the book 1 s later must still fill at or under the limit, the worse VWAP of the two; a FOK that
cannot fill leaves nothing).

    A  current pair-lock (strategy B)        replay3.run_window_v2 unchanged
    R  dynamic recovery                      one portfolio per market; after the first fill every buy is a recovery
                                             decision (pairlock_recovery.evaluate_recovery) at every 200 ms sample

The archive carries each token's touch and the depth within two ticks (modelled as three levels, ask / +1c / +2c, a
third each): a recovery that needs more than the 20 % of that depth the execution model leaves is "insufficient depth"
here even if the live book is deeper -- the limitation is reported, not hidden.

    PAIRLOCK_OUT=<v2 cache dir> OUT3=<dir> python scripts/pairlock/replay_recovery.py
"""
from __future__ import annotations

import itertools
import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import replace

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
sys.path.insert(0, "/root/troll-poly-bot/src")
import replay3 as R3                                                  # noqa: E402
from troll_poly_bot.strategy import pairlock_recovery as rc           # noqa: E402
from troll_poly_bot.strategy import pairlock_v2 as plv2               # noqa: E402
from troll_poly_bot.strategy import pairlock_book as pb               # noqa: E402
from troll_poly_bot.strategy.pairlock import MarketView               # noqa: E402

R, R2 = R3.R, R3.R2
OUT3 = os.environ.get("OUT3", "/tmp/pairlock_recovery")
BANKROLL = 100.0
SPLITS = R3.SPLITS
ENTRY_PRM = replace(plv2.PairLockParams(depth_haircut=R2.HAIRCUT), **R3.ENTRY)
ENTRY_LIM = plv2.RiskLimits()


def _x(v):
    v = float(v)
    return None if v != v else v


def view_at(w, i) -> MarketView:
    ua, ub, lu = R2.levels(w, "UP", i)
    da, db, ld = R2.levels(w, "DOWN", i)
    return MarketView(bid_up=ub, ask_up=ua, bid_down=db, ask_down=da, asks_up=lu, asks_down=ld,
                      bids_up=[(round(1.0 - p, 2), s) for p, s in ld], bids_down=[(round(1.0 - p, 2), s) for p, s in lu])


def run_window_recovery(w, prm: rc.RecoveryParams, model, trig, keep_events=False):
    tl = w.t.tolist()
    cyc: rc.RecoveryCycle | None = None
    pend = None
    seq = itertools.count(1)
    cnt = defaultdict(float)
    rej = defaultdict(int)
    attempts = 0
    max_inv = max_cost = 0.0

    def resolve(p):
        nonlocal cyc
        c = p["cyc"]
        if p["status"] == "filled":
            f = pb.FillRecord(w.slug, p["t_result"], p["side"].lower(), p["side"], p["q"], p["px"], p["q"] * p["fee_ps"],
                              p["oid"], f"{p['oid']}-1", "BUY", p["expected"], p["kind"])
            rc.on_fill(c, f, p["t_result"])
            rc.on_order_done(c, p["oid"], True, p["t_result"])
            cnt[p["kind"] + "_fills"] += 1
            if p["kind"] == "recovery":
                cnt["recovery_" + p["side"].lower() + "_fills"] += 1
                cnt["recovery_qty"] += p["q"]
                cnt["recovery_cost"] += p["q"] * (p["px"] + p["fee_ps"])
        else:
            rc.on_order_done(c, p["oid"], False, p["t_result"], p["status"])
            cnt[p["kind"] + "_kills"] += 1
            if p["kind"] == "entry" and not c.holds:
                cyc = None

    for i in range(w.i0, w.i1):
        t = tl[i]
        if pend is not None and t >= pend["t_result"]:
            p, pend = pend, None
            resolve(p)
        if pend is not None:
            continue
        left = (w.close - t) / 1000.0
        f = R.feat_at(w, i)
        if cyc is not None and cyc.holds:
            b = cyc.book
            max_inv = max(max_inv, b.up_qty + b.down_qty)
            max_cost = max(max_cost, b.total_cost)
            if f is None:
                continue
            p_up = model.p_up(f)
            dec, rec = rc.evaluate_recovery(cyc, p_up, f, view_at(w, i), t, left, model, prm, BANKROLL - b.cash_spent)
            if rec.get("confirmations", 0) >= prm.min_confirmations and rec.get("losing_side_move", -1.0) >= prm.min_price_move:
                cnt["recovery_opportunities"] += 1
            if dec is None:
                if "confirmations" in rec or rec["decision"] == "NONE":
                    rej[_cat(rec["why"])] += 1
                if not keep_events:
                    cyc.events.pop()                                  # keep memory flat: BUY / fill rows stay
                continue
            oid = f"r{next(seq)}"
            rc.on_recovery_sent(cyc, dec, oid, t)
            st, px, fps, tr = R2.fok(w, i, dec.side, dec.shares, dec.limit)
            pend = {"kind": "recovery", "cyc": cyc, "side": dec.side, "q": dec.shares, "status": st, "px": px,
                    "fee_ps": fps, "t_result": tr, "oid": oid, "expected": dec.vwap}
            cnt["recovery_sent"] += 1
            cnt["recovery_" + dec.side.lower() + "_sent"] += 1
            continue
        d = int(trig[i - w.i0])
        if d == 0 or f is None or attempts >= 2:
            continue
        side = "UP" if d > 0 else "DOWN"
        ask, bid, lv = R2.levels(w, side, i)
        dec, why = plv2.entry_decision(f, side, ask, bid, lv, model, ENTRY_PRM, ENTRY_LIM, plv2.MarketExposure(), left, None)
        if dec is None:
            cnt["entry_refused"] += 1
            continue
        attempts += 1
        cyc = rc.RecoveryCycle(book=pb.PairBook(w.slug), a_side=side, started_t=t, target=prm.target_profit)
        oid = f"e{next(seq)}"
        rc.on_entry_sent(cyc, oid, dec.shares, dec.limit, t)
        st, px, fps, tr = R2.fok(w, i, side, dec.shares, dec.limit)
        pend = {"kind": "entry", "cyc": cyc, "side": side, "q": dec.shares, "status": st, "px": px, "fee_ps": fps,
                "t_result": tr, "oid": oid, "expected": ask}
        cnt["entries"] += 1
    if pend is not None:
        resolve(pend if pend["status"] == "filled" else {**pend, "status": "market_closed"})
    out = {"slug": w.slug, "day": w.day, "outcome": w.outcome, "traded": 0, "recon_err": 0.0, "rej": dict(rej),
           **{k: v for k, v in cnt.items()}}
    if cyc is None or not cyc.holds:
        return out
    rc.on_close(cyc, w.close)
    r = rc.on_settled(cyc, w.outcome, w.close + 40_000.0)
    b = cyc.book
    out.update({"traded": 1, "total": r["pnl"], "first_leg": r["first_leg_pnl"], "recovery_effect": r["recovery_effect"],
                "recovered": int(r["recovered"]), "reached_target": int(r["reached_target"]), "fees": b.fees,
                "slippage": b.slippage, "capital": b.cash_spent, "max_cost": max_cost, "max_inventory": max_inv,
                "min_settlement": min(r["pnl_up_win"], r["pnl_down_win"]), "n_recoveries": len(cyc.recoveries),
                "recon_err": max(abs(r["pnl"] - r["pnl_from_cash"]), abs(r["pnl"] - r["pnl_reconstructed"])),
                "sequence": {"side": cyc.a_side, "entry": [(f.qty, f.price) for f in b.fills if f.kind == "entry"],
                             "fills": [(x["t"], x["side"], x["qty"], round(x["price"], 4), round(x["pnl_up_win"], 3),
                                        round(x["pnl_down_win"], 3)) for x in cyc.recoveries],
                             "up_qty": b.up_qty, "down_qty": b.down_qty, "up_avg": b.up_avg_cost,
                             "down_avg": b.down_avg_cost, "total_cost": round(b.total_cost, 4),
                             "pnl_up_win": round(r["pnl_up_win"], 4), "pnl_down_win": round(r["pnl_down_win"], 4),
                             "winner": w.outcome, "pnl": round(r["pnl"], 4), "first_leg": round(r["first_leg_pnl"], 4)},
                "events": [e for e in cyc.events if e.get("decision") == "BUY" or "event" in e] if keep_events else None})
    return out


def _cat(why: str) -> str:
    for k, lab in (("MIN_PRICE_MOVE", "no adverse move"), ("no confirmed reversal", "no reversal"),
                   ("no {O} inventory".replace("{O}", "UP"), "nothing to recover from"), ("no DOWN inventory", "nothing to recover from"),
                   ("insufficient depth", "depth"),
                   ("MAX_SLIPPAGE", "slippage"), ("MAX_TOTAL_POSITION", "position limit"), ("MAX_RECOVERY_CAPITAL", "capital limit"),
                   ("free bankroll", "bankroll"), ("MIN_RECOVERY_EDGE", "edge"), ("MAX_ACTIONS", "max actions"),
                   ("cooldown", "cooldown"), ("already satisfied", "target satisfied"), ("s left", "time left"),
                   ("no profit per share", "no profit per share"), ("no ", "no quote")):
        if k in why:
            return lab
    return "other"


_WS = None
_M2 = None


def _job(arg):
    name, kw, split = arg
    recs = []
    for w in R.split(_WS)[split]:
        if w.trig is None or w.trig[0] != 2.5:
            w.trig = (2.5, R.triggers(w, 2.5))
        if name == "A":
            recs.append(R3.run_window_v2(w, _M2, w.trig[1]))
        else:
            recs.append(run_window_recovery(w, rc.RecoveryParams(depth_haircut=R2.HAIRCUT, **kw), _M2, w.trig[1]))
    return name, split, recs


def summarize_recovery(recs):
    tr = [r for r in recs if r["traded"]]
    pnl = [r["total"] for r in tr]
    n = len(tr)
    g = lambda k: sum(r.get(k, 0.0) for r in recs)   # noqa: E731
    rej = defaultdict(int)
    for r in recs:
        for k, v in r.get("rej", {}).items():
            rej[k] += v
    eq = peak = dd = 0.0
    for r in recs:
        eq += r.get("total", 0.0)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    rf = g("recovery_fills")
    return {"windows": len(recs), "initial_positions": n, "entries_sent": g("entries"), "entry_fills": g("entry_fills"),
            "recovery_opportunities": g("recovery_opportunities"), "recovery_orders_sent": g("recovery_sent"),
            "recovery_orders_filled": rf, "recovery_fill_rate": rf / g("recovery_sent") if g("recovery_sent") else None,
            "up_recovery_orders": g("recovery_up_sent"), "down_recovery_orders": g("recovery_down_sent"),
            "up_recovery_fills": g("recovery_up_fills"), "down_recovery_fills": g("recovery_down_fills"),
            "avg_recovery_size": g("recovery_qty") / rf if rf else None,
            "avg_recovery_price": (g("recovery_cost") / g("recovery_qty")) if g("recovery_qty") else None,
            "total_fees": g("fees"), "total_slippage": g("slippage"), "total_pnl": sum(pnl),
            "pnl_per_position": statistics.mean(pnl) if pnl else 0.0,
            "t": (statistics.mean(pnl) / (statistics.pstdev(pnl) / math.sqrt(n))) if n > 1 and statistics.pstdev(pnl) > 0 else 0.0,
            "worst_position": min(pnl) if pnl else 0.0, "best_position": max(pnl) if pnl else 0.0, "max_dd": dd,
            "max_capital_exposure": max((r.get("max_cost", 0.0) for r in tr), default=0.0),
            "max_inventory": max((r.get("max_inventory", 0.0) for r in tr), default=0.0),
            "pct_positions_recovered": 100.0 * g("recovered") / n if n else 0.0,
            "pct_reaching_target": 100.0 * g("reached_target") / n if n else 0.0,
            "pct_ending_negative": 100.0 * sum(1 for x in pnl if x < 0) / n if n else 0.0,
            "avg_min_settlement_pnl": statistics.mean(r["min_settlement"] for r in tr) if tr else 0.0,
            "first_legs_pnl": g("first_leg"), "recovery_effect_pnl": g("recovery_effect"),
            "recovered_markets_pnl": sum(r["total"] for r in tr if r.get("recovered")),
            "recovered_markets_first_leg": sum(r["first_leg"] for r in tr if r.get("recovered")),
            "rejections": dict(rej), "recon_max_err": max((r["recon_err"] for r in recs), default=0.0)}


def summarize_a(recs):
    s = R3.summarize(recs)
    tr = [r for r in recs if r["traded"]]
    pnl = [r["total"] for r in tr]
    return {"windows": s["windows"], "initial_positions": s["markets"], "entries_sent": s["entries"],
            "entry_fills": s["entry_fills"], "hedges_sent": s["hedges_sent"], "hedges_filled": s["hedges_filled"],
            "total_fees": None, "total_pnl": s["net"], "pnl_per_position": s["per_market"], "t": s["t"],
            "worst_position": s["worst"], "max_dd": s["max_dd"],
            "max_capital_exposure": max((r.get("capital", 0.0) for r in tr), default=0.0),
            "pct_ending_negative": 100.0 * sum(1 for x in pnl if x < 0) / len(pnl) if pnl else 0.0,
            "first_legs_pnl": s["baseline_first_legs"], "hedging_pnl": s["hedging_pnl"], "pairs_pnl": s["pairs_pnl"],
            "markets_with_2_cycles": s["markets_with_2_cycles"], "recon_max_err": s["recon_max_err"]}


CONFIGS = [("A", {}), ("R", {}), ("R edge0.02", {"min_edge": 0.02}), ("R target1", {"target_profit": 1.0}),
           ("R slip0.05", {"max_slippage": 0.05})]


def main():
    global _WS, _M2
    import multiprocessing as mp
    t0 = time.time()
    _WS = R.load_cache()
    _M2 = plv2.ProbModel.load(R3.MODEL_PATH)
    args = [(n, kw, s) for n, kw in CONFIGS for s in SPLITS]
    with mp.get_context("fork").Pool(int(os.environ.get("PROCS", "2"))) as pool:
        res = pool.map(_job, args, chunksize=1)
    by = defaultdict(dict)
    for name, split, recs in res:
        by[name][split] = recs
    os.makedirs(OUT3, exist_ok=True)
    out = {}
    for name, d in by.items():
        f = summarize_a if name == "A" else summarize_recovery
        out[name] = {s: f(d[s]) for s in SPLITS}
        out[name]["all"] = f([r for s in SPLITS for r in d[s]])
        out[name]["test+forward"] = f(d["test"] + d["forward"])
    json.dump(out, open(f"{OUT3}/replay_recovery_summary.json", "w"), indent=1, default=float)
    seqs = [{"split": s, **r["sequence"]} for s in SPLITS for r in by["R"][s] if r.get("traded") and r.get("n_recoveries")]
    json.dump(seqs, open(f"{OUT3}/replay_recovery_sequences.json", "w"), indent=1, default=float)
    for name in out:
        a = out[name]["all"]
        print(name, json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in a.items()}, default=str))
    print(f"{len(seqs)} markets with a recovery fill; done in {time.time() - t0:.0f} s -> {OUT3}")


if __name__ == "__main__":
    main()
