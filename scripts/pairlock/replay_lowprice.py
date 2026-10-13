"""OLD PAIR-LOCK vs the LOW-PRICE FIRST LEG + DYNAMIC RECOVERY mode (the user's spec, 2026-10-07, items 20-21), on
the SAME recorded BTC windows, the same features and probability model (data/replay/pairlock2_model.json), the same
execution model (replay2.fok: arrival +143 ms, the size walked through the ask levels with 80 % taken by faster
traders, the book 1 s later must still fill at or under the limit, the worse VWAP; all-or-nothing -- the replay
cannot show partial fills, the paper bot does) and the same four splits (replay.py SPLITS by time).

    A          strategy B (the v2 pair-lock as it ran): replay3.run_window_v2, unchanged
    R-open     the recovery paper bot's OPEN RULE (10-06): the higher token 2..20 s after the open, leader 0.50..0.85,
               recover above 0.70 toward +0.50, two recoveries, loss limits 35 / 60 s
    LOW-hold   the low-price first leg alone, held to settlement (the first leg's DIRECTIONAL edge, item 21 A)
    LOW        the low-price first leg + break-even dynamic recovery (the spec's mode, ecosystem.pairlocklow.config.js)
    LOW-p50    ... with partial_recovery 0.5
    LOW-ev     ... with expected_value

No lookahead: every decision at sample i sees the book, features and model at i; fills come from the book 143 ms
and 1 s later through the execution model; the outcome is used only at settlement.

    PAIRLOCK_OUT=<cache dir with windows.pkl> PROCS=2 .venv/bin/python scripts/pairlock/replay_lowprice.py
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

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
sys.path.insert(0, "/root/troll-poly-bot/src")
import replay as R                                                    # noqa: E402
import replay2 as R2                                                  # noqa: E402
import replay3 as R3                                                  # noqa: E402
from troll_poly_bot.strategy import pairlock_book as pb               # noqa: E402
from troll_poly_bot.strategy import pairlock_recovery as rc           # noqa: E402
from troll_poly_bot.strategy import pairlock_v2 as plv2               # noqa: E402
from troll_poly_bot.strategy.pairlock import MarketView               # noqa: E402

OUT = os.environ.get("OUT_LOW", "/root/troll-poly-bot/results")
BANKROLL = 100.0
SPLITS = R3.SPLITS
ENTRY_PRM = replace(plv2.PairLockParams(depth_haircut=R2.HAIRCUT), **R3.ENTRY)

CAPS = dict(direction="price", pair_bid=False, edge_gate=False, step_fill=True, max_order_size=60.0, max_capital=90.0,
            max_total_position=300.0, max_actions=30, max_market_loss=35.0, recovery_max_price=1.0, lock_left_s=60.0,
            late_enabled=False, profit_exit=0.0, recovery_sell=False, depth_haircut=R2.HAIRCUT)
OLD = rc.RecoveryParams(entry_mode="open", open_delay_s=2.0, open_window_s=20.0, open_min_price=0.50, open_max_price=0.85,
                        open_shares=10.0, open_limit_offset=0.02, open_max_tries=3, recovery_min_price=0.70, max_recoveries=2,
                        **CAPS)
LOW = rc.RecoveryParams(entry_mode="low", open_shares=10.0, open_limit_offset=0.02, recovery_target_mode="break_even",
                        recovery_min_price=0.55, min_price_move=0.01, min_pnl_improvement=0.05, max_recoveries=0, **CAPS)
CONFIGS = [("A", None), ("R-open", OLD), ("LOW-hold", replace(LOW, recovery_enabled=False)), ("LOW", LOW),
           ("LOW-p50", replace(LOW, recovery_target_mode="partial_recovery", partial_target=0.5)),
           ("LOW-ev", replace(LOW, recovery_target_mode="expected_value"))]


def view_at(w, i) -> MarketView:
    ua, ub, lu = R2.levels(w, "UP", i)
    da, db, ld = R2.levels(w, "DOWN", i)
    return MarketView(bid_up=ub, ask_up=ua, bid_down=db, ask_down=da, asks_up=lu, asks_down=ld,
                      bids_up=[(round(1.0 - p, 2), s) for p, s in ld], bids_down=[(round(1.0 - p, 2), s) for p, s in lu])


def run_window(w, prm: rc.RecoveryParams, model):
    """One market under the recovery engine's rules (entry mode open or low), the replay's execution model."""
    tl = w.t.tolist()
    cyc: rc.RecoveryCycle | None = None
    pend = None
    seq = itertools.count(1)
    cnt = defaultdict(float)
    rej = defaultdict(int)
    tries = 0
    first_t = None
    first_rec = None                       # the first recovery BUY record (worst / expected before and after)
    max_cost = 0.0

    def resolve(p):
        nonlocal cyc, first_t
        c = p["cyc"]
        if p["status"] == "filled":
            f = pb.FillRecord(w.slug, p["t_result"], p["side"].lower(), p["side"], p["q"], p["px"], p["q"] * p["fee_ps"],
                              p["oid"], f"{p['oid']}-1", "BUY", p["expected"], p["kind"])
            rc.on_fill(c, f, p["t_result"])
            rc.on_order_done(c, p["oid"], True, p["t_result"])
            cnt[p["kind"] + "_fills"] += 1
            if p["kind"] == "entry":
                first_t = p["t_result"]
                cnt["first_qty"] += p["q"]
                cnt["first_cost"] += p["q"] * (p["px"] + p["fee_ps"])
            elif p["kind"] == "recovery":
                cnt["recovery_qty"] += p["q"]
                cnt["recovery_cost"] += p["q"] * (p["px"] + p["fee_ps"])
            elif p["kind"] == "lock":
                cnt["lock_qty"] += p["q"]
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
        mv = view_at(w, i)
        if cyc is not None and cyc.holds:
            b = cyc.book
            max_cost = max(max_cost, b.total_cost)
            if not prm.recovery_enabled or f is None:
                continue
            p_up = model.p_up(f)
            dec, rec = rc.evaluate_recovery(cyc, p_up, f, mv, t, left, model, prm, BANKROLL - b.cash_spent)
            if "opposite_vwap" in rec or rec.get("decision") in ("LOCK", "LOCKED"):
                cnt["recovery_evaluations"] += 1
                cnt["verdict_" + rec.get("verdict", "REJECT")] += 1
            if dec is None:
                if rec.get("verdict") == "REJECT":
                    rej[rec["why"].split("(")[0][:50]] += 1
                cyc.events.clear()
                continue
            if first_rec is None and dec.kind == "recovery":
                first_rec = {k: rec.get(k) for k in ("worst_case_before", "worst_case_after", "expected_before", "expected_after",
                                                     "required_opposite_qty", "opposite_vwap", "time_left")}
            oid = f"r{next(seq)}"
            rc.on_recovery_sent(cyc, dec, oid, t)
            st, px, fps, tr = R2.fok(w, i, dec.side, dec.shares, dec.limit)
            pend = {"kind": dec.kind, "cyc": cyc, "side": dec.side, "q": dec.shares, "status": st, "px": px,
                    "fee_ps": fps, "t_result": tr, "oid": oid, "expected": dec.vwap}
            cnt[dec.kind + "_sent"] += 1
            cyc.events.clear()
            continue
        if cyc is not None or tries >= (prm.low_max_tries if prm.entry_mode == "low" else prm.open_max_tries):
            continue
        if prm.entry_mode == "low":
            if not prm.low_min_left_s <= left <= prm.low_max_left_s:
                continue
            side, lrec = rc.low_entry_side(mv, prm, left, f, model, ENTRY_PRM)
            cnt["first_evaluations"] += 1
            if side is None:
                rej["first: " + lrec["why"][:40]] += 1
                continue
        else:
            el = (t - w.open) / 1000.0
            if not prm.open_delay_s <= el <= prm.open_window_s:
                continue
            side, why = rc.open_entry_side(mv, prm)
            cnt["first_evaluations"] += 1
            if side is None:
                continue
        cnt["first_opportunities"] += 1
        tries += 1
        ask = mv.ask(side)
        limit = min(0.99, round(ask + prm.open_limit_offset, 2))
        cyc = rc.RecoveryCycle(book=pb.PairBook(w.slug), a_side=side, started_t=t, target=prm.target_profit, open_entry=True)
        oid = f"e{next(seq)}"
        rc.on_entry_sent(cyc, oid, prm.open_shares, limit, t)
        st, px, fps, tr = R2.fok(w, i, side, prm.open_shares, limit)
        pend = {"kind": "entry", "cyc": cyc, "side": side, "q": prm.open_shares, "status": st, "px": px, "fee_ps": fps,
                "t_result": tr, "oid": oid, "expected": ask}
        cnt["entry_sent"] += 1
    if pend is not None:
        resolve(pend if pend["status"] == "filled" else {**pend, "status": "market_closed"})
    out = {"slug": w.slug, "day": w.day, "outcome": w.outcome, "traded": 0, "rej": dict(rej), **cnt}
    if cyc is None or not cyc.holds:
        return out
    rc.on_close(cyc, w.close)
    r = rc.on_settled(cyc, w.outcome, w.close + 40_000.0)
    b = cyc.book
    worst_first = -cnt["first_cost"]
    out.update({"traded": 1, "pnl": r["pnl"], "first_leg": r["first_leg_pnl"], "recovery_effect": r["recovery_effect"],
                "recovered": int(bool(cyc.recoveries)), "fees": b.fees, "slippage": b.slippage, "capital": max_cost,
                "worst_after": min(r["pnl_up_win"], r["pnl_down_win"]), "worst_first": worst_first,
                "pair_complete": int(min(r["pnl_up_win"], r["pnl_down_win"]) >= -1e-9),
                "locked": int(cyc.locked), "hold_s": (w.close - first_t) / 1000.0 if first_t else 0.0,
                "first_rec": first_rec, "side": cyc.a_side, "won": int(w.outcome == cyc.a_side),
                "recon_err": max(abs(r["pnl"] - r["pnl_from_cash"]), abs(r["pnl"] - r["pnl_reconstructed"]))})
    return out


def summarize(recs):
    tr = [r for r in recs if r["traded"]]
    pnl = [r["pnl"] for r in tr]
    n = len(tr)
    g = lambda k: sum(float(r.get(k) or 0.0) for r in recs)       # noqa: E731
    eq = peak = dd = 0.0
    for r in recs:
        eq += r.get("pnl", 0.0)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    rec_tr = [r for r in tr if r["recovered"]]
    one = [r for r in tr if not r["recovered"]]
    fr = [r["first_rec"] for r in rec_tr if r.get("first_rec")]
    avg = lambda xs: statistics.mean(xs) if xs else None                        # noqa: E731
    col = lambda k: avg([x[k] for x in fr if x.get(k) is not None])            # noqa: E731
    se = statistics.pstdev(pnl) / math.sqrt(n) if n > 1 else 0.0
    rej = defaultdict(int)
    for r in recs:
        for k, v in r.get("rej", {}).items():
            rej[k] += v
    return {"markets": len(recs), "first_evaluations": g("first_evaluations"), "first_opportunities": g("first_opportunities"),
            "first_orders": g("entry_sent"), "first_fills": g("entry_fills"), "positions": n,
            "recovery_evaluations": g("recovery_evaluations"), "recovery_buy": g("verdict_BUY"), "recovery_wait": g("verdict_WAIT"),
            "recovery_reject": g("verdict_REJECT"), "recovery_orders": g("recovery_sent"), "recovery_fills": g("recovery_fills"),
            "lock_orders": g("lock_sent"), "lock_fills": g("lock_fills"),
            "recovered_positions": len(rec_tr), "unrecovered_positions": len(one),
            "avg_first_price": (g("first_cost") / g("first_qty")) if g("first_qty") else None,
            "avg_recovery_price": (g("recovery_cost") / g("recovery_qty")) if g("recovery_qty") else None,
            "recovery_qty_over_first_qty": (g("recovery_qty") / sum(r["first_qty"] for r in rec_tr)) if rec_tr and sum(r["first_qty"] for r in rec_tr) else None,
            "worst_before_recovery": col("worst_case_before"), "worst_after_recovery": col("worst_case_after"),
            "expected_before_recovery": col("expected_before"), "expected_after_recovery": col("expected_after"),
            "worst_final_avg": avg([r["worst_after"] for r in tr]),
            "pair_completions": sum(r["pair_complete"] for r in tr), "locked": sum(r["locked"] for r in tr),
            "one_sided_wins": sum(r["won"] for r in one), "one_sided_losses": sum(1 - r["won"] for r in one),
            "settlement_wins": sum(1 for x in pnl if x > 0), "settlement_losses": sum(1 for x in pnl if x <= 0),
            "first_leg_pnl": g("first_leg"), "recovery_effect_pnl": g("recovery_effect"),
            "fees": g("fees"), "slippage": g("slippage"), "gross_pnl": sum(pnl) + g("fees"), "net_pnl": sum(pnl),
            "pnl_per_position": statistics.mean(pnl) if pnl else 0.0, "se": se,
            "t": (statistics.mean(pnl) / se) if se > 0 else 0.0, "worst_position": min(pnl) if pnl else 0.0,
            "max_drawdown": dd, "max_capital": max((r["capital"] for r in tr), default=0.0),
            "avg_capital": statistics.mean(r["capital"] for r in tr) if tr else 0.0,
            "avg_hold_s": statistics.mean(r["hold_s"] for r in tr) if tr else 0.0,
            "rejections": dict(sorted(rej.items(), key=lambda kv: -kv[1])[:12]),
            "recon_max_err": max((r.get("recon_err", 0.0) for r in recs), default=0.0)}


def summarize_a(recs):
    s = R3.summarize(recs)
    tr = [r for r in recs if r["traded"]]
    pnl = [r["total"] for r in tr]
    n = len(pnl)
    se = statistics.pstdev(pnl) / math.sqrt(n) if n > 1 else 0.0
    return {"markets": s["windows"], "first_orders": s["entries"], "first_fills": s["entry_fills"], "positions": s["markets"],
            "recovery_orders": s["hedges_sent"], "recovery_fills": s["hedges_filled"], "net_pnl": s["net"],
            "pnl_per_position": s["per_market"], "se": se, "t": s["t"], "worst_position": s["worst"], "max_drawdown": s["max_dd"],
            "settlement_wins": sum(1 for x in pnl if x > 0), "settlement_losses": sum(1 for x in pnl if x <= 0),
            "first_leg_pnl": s["baseline_first_legs"], "recovery_effect_pnl": s["hedging_pnl"],
            "max_capital": max((r.get("capital", 0.0) for r in tr), default=0.0), "recon_max_err": s["recon_max_err"]}


_WS = None
_M = None


def _job(arg):
    name, split = arg
    prm = dict(CONFIGS)[name]
    recs = []
    for w in R.split(_WS)[split]:
        if name == "A":
            if w.trig is None or w.trig[0] != 2.5:
                w.trig = (2.5, R.triggers(w, 2.5))
            recs.append(R3.run_window_v2(w, _M, w.trig[1]))
        else:
            recs.append(run_window(w, prm, _M))
    return name, split, recs


def main():
    global _WS, _M
    import multiprocessing as mp
    t0 = time.time()
    if "--summarize" in sys.argv:                       # re-summarize a saved raw run
        import pickle
        by = pickle.load(open(f"{OUT}/pairlock_lowprice_replay_raw_2026-10-07.pkl", "rb"))
        _WS = R.load_cache()
        _M = plv2.ProbModel.load(R3.MODEL_PATH)
        return finish(by, t0)
    _WS = R.load_cache()
    _M = plv2.ProbModel.load(R3.MODEL_PATH)
    args = [(n, s) for n, _ in CONFIGS for s in SPLITS]
    with mp.get_context("fork").Pool(int(os.environ.get("PROCS", "2"))) as pool:
        res = pool.map(_job, args, chunksize=1)
    by = defaultdict(dict)
    for name, split, recs in res:
        by[name][split] = recs
    os.makedirs(OUT, exist_ok=True)
    import pickle
    with open(f"{OUT}/pairlock_lowprice_replay_raw_2026-10-07.pkl", "wb") as fh:    # the pool's output, before any summary
        pickle.dump(dict(by), fh)
    return finish(by, t0)


def finish(by, t0):
    out = {"windows": len(_WS), "first": _WS[0].slug, "last": _WS[-1].slug,
           "splits": {s: [v[0].slug, v[-1].slug, len(v)] for s, v in R.split(_WS).items()}, "model": _M.meta.get("fitted_on")}
    for name, d in by.items():
        f = summarize_a if name == "A" else summarize
        out[name] = {s: f(d[s]) for s in SPLITS}
        out[name]["all"] = f([r for s in SPLITS for r in d[s]])
        out[name]["test+forward"] = f(d["test"] + d["forward"])
    json.dump(out, open(f"{OUT}/pairlock_lowprice_replay_2026-10-07.json", "w"), indent=1, default=float)
    json.dump({n: {s: [{k: v for k, v in r.items() if k != "rej"} for r in d[s] if r.get("traded")] for s in SPLITS}
               for n, d in by.items() if n != "A"},
              open(f"{OUT}/pairlock_lowprice_replay_positions_2026-10-07.json", "w"), default=float)
    for name in out:
        if name in ("windows", "first", "last", "splits", "model"):
            continue
        a = out[name]["all"]
        print(name, json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in a.items() if k != "rejections"}, default=str))
    print(f"done in {time.time() - t0:.0f} s -> {OUT}")


if __name__ == "__main__":
    main()
