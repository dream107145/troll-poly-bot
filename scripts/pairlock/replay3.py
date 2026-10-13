"""Recovery-hedge / profit-lock replay (2026-10-02 spec item 20): strategies A-E on the SAME chronological BTC windows
(the v2 cache, 1,649 windows 09-22 -> 10-01), the same entry trigger, gate, model and execution model.

    A  INITIAL ENTRY ONLY         new code, hedge_mode=none
    B  CURRENT IMPLEMENTATION     the FROZEN v2 code (strategy/pairlock_v2.py), its deployed EV-based conditional hedge
                                  (rev 1.5 / 400 ms, MIN_HEDGE_EV 0.05, MIN_PAIR_EDGE 0.05), including its re-entry
    C  CORRECT RECOVERY HEDGE     new code, hedge_mode=recovery, every default (MAX_ACCEPTABLE_PAIR_COST 1.00)
    D  PROFIT LOCK                new code, hedge_mode=profit_lock
    E  COMBINED                   new code, hedge_mode=combined
    DIAGNOSTIC (not a parameter choice): C and E with MAX_ACCEPTABLE_PAIR_COST 1.05 / 1.10 / 1.15 / 1.20, to show what
    the recovery logic does when the cap lets it act.

Nothing is optimised: the first replay verifies behaviour. The daily-loss stop is OFF so every strategy takes exactly
the same entries (the comparison is then the position management alone).

Execution = the paper venue's CALIBRATED scenario as in replay2 (arrival + 143 ms, the size walked through the ask
levels with 80 % taken by faster traders, the book 1 s later must still fill at or under the limit, the worse VWAP of
the two, per-level taker fee; a FOK that cannot fill leaves nothing).

    PAIRLOCK_OUT=<v2 cache dir> OUT3=<output dir> python scripts/pairlock/replay3.py run
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

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("pairlock_replay2", "/root/troll-poly-bot/scripts/pairlock/replay2.py")
R2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R2)                            # the v2 replay on the FROZEN v2 modules (strategy B)
R = R2.R                                                # data, cached features, triggers
from troll_poly_bot.strategy import pairlock as pl          # noqa: E402  (the new code)
from troll_poly_bot.strategy import pairlock_book as pb     # noqa: E402

ROOT = "/root/troll-poly-bot"
OUT3 = os.environ.get("OUT3", "/tmp/pairlock3")
MODEL_PATH = f"{ROOT}/data/replay/pairlock2_model.json"
HAIRCUT = R2.HAIRCUT
SPLITS = ("train", "validation", "test", "forward")
ENTRY = {"entry_edge": 0.02, "entry_mode": "both", "short_min": 0.55, "band_lo": 0.20, "band_hi": 0.96}
V2_C = {**ENTRY, "hedge_mode": "conditional", "rev_thr": 1.5, "confirm_ms": 400.0, "min_hedge_ev": 0.05,
        "min_pair_edge": 0.05}


def view_at(w, i) -> pl.MarketView:
    """Both books at sample i. The archive keeps each token's ask levels; a token's bids are the other's asks
    mirrored (DOWN ask = 1 - UP bid on every recorded sample)."""
    ua, ub, lu = R2.levels(w, "UP", i)
    da, db, ld = R2.levels(w, "DOWN", i)
    return pl.MarketView(bid_up=ub, ask_up=ua, bid_down=db, ask_down=da, asks_up=lu, asks_down=ld,
                         bids_up=[(round(1.0 - p, 2), s) for p, s in ld], bids_down=[(round(1.0 - p, 2), s) for p, s in lu])


def _cat(why: str) -> str:
    for k, lab in (("MAX_ACCEPTABLE_PAIR_COST", "pair cost cap"), ("the size needs", "pair cost cap"),
                   ("DOWNSIDE_REDUCTION", "downside minimum"), ("utility", "utility not improved"),
                   ("depth", "depth"), ("venue minimum", "venue minimum"), ("market", "risk limits"),
                   ("hedge position", "risk limits"), ("total position", "risk limits")):
        if k in why:
            return lab
    return "other"


def run_window(w, prm, lim, model, trig, keep=False):
    tl = w.t.tolist()
    cycles: list[pl.Cycle] = []
    pend = None
    seq = itertools.count(1)
    cnt = defaultdict(float)
    rej = defaultdict(int)
    audit = []

    def exposure():
        cap = sum(c.book.cash_spent for c in cycles)
        sh = sum(c.book.up_qty + c.book.down_qty for c in cycles)
        if pend is not None:
            cap += pend["q"] * (pend["limit"] + pb.fee_per_share(pend["limit"]))
            sh += pend["q"]
        held = [c for c in cycles if c.holds]
        return pl.MarketExposure(capital=cap, shares=sh, worst_case=sum(c.book.worst_case_pnl for c in cycles if c.book.fills),
                                 cycles=len(cycles), held=bool(held), held_state=held[-1].state.value if held else "")

    def resolve(p):
        cyc = p["cyc"]
        if p["status"] == "filled":
            fill = pb.FillRecord(w.slug, p["t_result"], p["side"].lower(), p["side"], p["q"], p["px"], p["q"] * p["fee_ps"],
                                 p["oid"], f"{p['oid']}-1", "BUY", p["expected"], p["kind"])
            w0 = cyc.book.worst_case_pnl
            pl.on_fill(cyc, fill, prm, p["t_result"])
            pl.on_order_done(cyc, p["oid"], True, p["t_result"], prm=prm)
            cnt[p["kind"] + "_fills"] += 1
            if p["kind"] != "entry":
                cnt[p["kind"] + "_qty"] += p["q"]
                cnt[p["kind"] + "_downside"] += cyc.book.worst_case_pnl - w0
        else:
            pl.on_order_done(cyc, p["oid"], False, p["t_result"], p["status"], prm=prm)
            cnt[p["kind"] + "_kills"] += 1
            if p["kind"] == "entry" and not cyc.book.fills and cyc in cycles:
                cycles.remove(cyc)

    for i in range(w.i0, w.i1):
        t = tl[i]
        if pend is not None and t >= pend["t_result"]:
            p, pend = pend, None
            resolve(p)
        if pend is not None:
            continue
        cur = cycles[-1] if cycles else None
        left = (w.close - t) / 1000.0
        watching = cur is not None and cur.state in pb.WATCH_STATES
        d = int(trig[i - w.i0])
        if not watching and d == 0:
            continue
        f = R.feat_at(w, i)
        if watching:
            ep0, adv0 = cur.episode, cur.adverse_now
            dec, why, rec = pl.manage_position(cur, f, view_at(w, i), t, left, model, prm, lim, exposure())
            if cur.adverse_now and not adv0:
                cnt["adverse_episodes"] += 1
            if cur.episode != ep0:
                cnt["recovery_opportunities"] += 1
            if rec is not None:
                if rec["kind"] == "recovery":
                    cnt["recovery_evaluations"] += 1
                    cnt["recovery_candidates"] += 1 if rec.get("acceptable") else 0
                    if rec.get("decision") == "REJECTED":
                        cnt["recovery_rejected"] += 1
                        rej[_cat(rec.get("why", ""))] += 1
                else:
                    cnt["profit_lock_opportunities"] += 1
                    if rec.get("decision") == "REJECTED":
                        cnt["profit_lock_rejected"] += 1
            if dec is not None:
                oid = f"h{next(seq)}"
                pl.on_hedge_sent(cur, oid, dec.side, dec.shares, dec.limit, t, dec.kind, dec.info)
                st, px, fps, tr = R2.fok(w, i, dec.side, dec.shares, dec.limit)
                pend = {"kind": dec.kind, "cyc": cur, "side": dec.side, "q": dec.shares, "limit": dec.limit,
                        "expected": dec.info.get("vwap"), "status": st, "px": px, "fee_ps": fps, "t_result": tr,
                        "oid": oid}
                cnt[dec.kind + "_sent"] += 1
                cnt[dec.kind + "_pair_cost_decided"] += dec.info.get("pair_cost") or 0.0
                continue
        if d == 0 or pend is not None or f is None:
            continue
        cur = cycles[-1] if cycles else None
        side = "UP" if d > 0 else "DOWN"
        ask, bid, lv = R2.levels(w, side, i)
        mkt = exposure()
        dec, why = pl.entry_decision(f, side, ask, bid, lv, model, prm, lim, mkt, left, cur)
        if dec is None:
            cnt["reentry_blocked" if why.startswith("REENTRY_BLOCKED") else "entry_refused"] += 1
            continue
        cyc = pl.Cycle(book=pb.PairBook(w.slug), a_side=side, started_t=t)
        cyc.entry_info = {"decision_t": t, "model_p": dec.p, "ev": dec.ev, "executable_vwap": dec.info["vwap"]}
        cycles.append(cyc)
        oid = f"e{next(seq)}"
        pl.on_entry_sent(cyc, oid, dec.shares, dec.limit, t)
        st, px, fps, tr = R2.fok(w, i, side, dec.shares, dec.limit)
        pend = {"kind": "entry", "cyc": cyc, "side": side, "q": dec.shares, "limit": dec.limit, "expected": ask,
                "status": st, "px": px, "fee_ps": fps, "t_result": tr, "oid": oid}
        cnt["entries"] += 1
    if pend is not None:
        resolve(pend if pend["status"] == "filled" else {**pend, "status": "market_closed"})
    # ---- settlement, three-way reconciliation, what each cycle ended as
    tot = defaultdict(float)
    recon, states = 0.0, defaultdict(int)
    held_cycles = [c for c in cycles if c.book.fills]
    for c in held_cycles:
        final = c.state.value
        pl.on_close(c, w.close, prm)
        r = pl.on_settled(c, w.outcome, w.close + 40_000.0)
        recon = max(recon, abs(r["pnl"] - r["pnl_from_cash"]), abs(r["pnl"] - r["pnl_reconstructed"]))
        for k in ("total", "prediction", "recovery_leg", "profit_lock_leg", "pairs", "directional"):
            tot[k] += r[k]
        tot["fees"] += c.book.fees
        tot["slippage"] += c.book.slippage
        tot["capital"] += c.book.cash_spent
        states["final:" + final] += 1
        if c.recovery_qty > 0:
            tot["recovery_market_pnl"] += r["total"]
            tot["recovery_market_baseline"] += r["prediction"]
            states["recovery_markets"] += 1
        if c.profit_lock_qty > 0:
            tot["profit_lock_market_pnl"] += r["total"]
            tot["profit_lock_market_baseline"] += r["prediction"]
            tot["profit_lock_locked"] += c.profit_lock_locked
            states["profit_lock_markets"] += 1
        for q, e in c.recovery_edges:
            tot["recovery_edge_qty"] += q
            tot["recovery_paircost_x_qty"] += q * (1.0 - e)
        if c.book.matched_qty > 0 and c.book.pair_cost_per_share is not None:
            tot["matched_markets"] += 1
            tot["pair_cost_sum"] += c.book.pair_cost_per_share
            if c.book.pair_cost_per_share > 1.0 + 1e-9:
                states["pairs_above_1"] += 1
        if keep:
            audit.append({"slug": w.slug, "outcome": w.outcome, "transitions": [(x.frm, x.to, x.reason) for x in c.transitions],
                          "events": [(x.name, x.state, x.reason) for x in c.events], "pnl": r["pnl"]})
    for k in ("reentry_blocked", "recovery_opportunities", "recovery_evaluations", "recovery_sent", "recovery_fills",
              "profit_lock_opportunities", "profit_lock_sent", "profit_lock_fills", "adverse_episodes"):
        if cnt.get(k):
            cnt[k + "_mkts"] = 1
    out = {"slug": w.slug, "day": w.day, "outcome": w.outcome, "traded": int(bool(held_cycles)),
           "cycles": len(held_cycles), "recon_err": recon, "rej": dict(rej), "states": dict(states),
           **{k: v for k, v in cnt.items()}, **{k: round(v, 6) for k, v in tot.items()}}
    if keep:
        out["audit"] = audit
    return out


def run_window_v2(w, model_v2, trig):
    """Strategy B: replay2.run_window on the frozen v2 modules, the deployed conditional settings."""
    prm, lim = R2.base_params()
    prm = replace(prm, **V2_C)
    r = R2.run_window(w, "C", prm, lim, model_v2, trig)
    return {"slug": w.slug, "day": w.day, "outcome": w.outcome, "traded": r["traded"], "cycles": r["cycles"],
            "total": r.get("total", 0.0), "prediction": r.get("prediction", 0.0), "hedging": r.get("hedging", 0.0),
            "pairs": r.get("pairs", 0.0), "hedges": r.get("hedges", 0), "hedge_fills": r.get("hedge_fills", 0),
            "entries": r.get("entries", 0), "entry_fills": r.get("entry_fills", 0), "recon_err": r["recon_err"],
            "capital": r.get("capital", 0.0), "pair_costs": r["pair_costs"], "states": r["states"],
            "hedge_log": [h for h in r["hedge_log"] if h.get("px") is not None]}


# ───────────────────────────── batches ─────────────────────────────

_WS = None
_M = None
_M2 = None


def _job(arg):
    name, kind, kw, split = arg
    sp = R.split(_WS)
    recs = []
    for w in sp[split]:
        if w.trig is None or w.trig[0] != 2.5:
            w.trig = (2.5, R.triggers(w, 2.5))
        if kind == "B":
            recs.append(run_window_v2(w, _M2, w.trig[1]))
        else:
            prm = replace(pl.PairLockParams(depth_haircut=HAIRCUT), **{**ENTRY, **kw})
            recs.append(run_window(w, prm, pl.RiskLimits(), _M, w.trig[1], keep=(name == "E" and split == "forward")))
    return name, split, recs


JOBS = [("A", "new", {"hedge_mode": "none"}), ("B", "B", {}), ("C", "new", {"hedge_mode": "recovery"}),
        ("D", "new", {"hedge_mode": "profit_lock"}), ("E", "new", {"hedge_mode": "combined"})]
DIAG = [(f"{s}@{cap:.2f}", "new", {"hedge_mode": m, "max_acceptable_pair_cost": cap})
        for s, m in (("C", "recovery"), ("E", "combined")) for cap in (1.05, 1.10, 1.15, 1.20)]


def cmd_run():
    global _WS, _M, _M2
    import multiprocessing as mp
    t0 = time.time()
    _WS = R.load_cache()
    _M = pl.ProbModel.load(MODEL_PATH)
    from troll_poly_bot.strategy import pairlock_v2 as plv2
    _M2 = plv2.ProbModel.load(MODEL_PATH)
    os.makedirs(OUT3, exist_ok=True)
    args = [(n, k, kw, s) for n, k, kw in JOBS + DIAG for s in SPLITS]
    procs = int(os.environ.get("PROCS", "3"))
    with mp.get_context("fork").Pool(procs) as pool:
        res = pool.map(_job, args, chunksize=1)
    by = defaultdict(dict)
    for name, split, recs in res:
        by[name][split] = recs
    with open(f"{OUT3}/replay3_records.json", "w") as f:
        json.dump({n: {s: [{k: v for k, v in r.items() if k != "audit"} for r in rs] for s, rs in d.items()}
                   for n, d in by.items()}, f, default=float)
    aud = [a for r in by["E"]["forward"] for a in r.get("audit", [])]
    with open(f"{OUT3}/replay3_audit_E_forward.json", "w") as f:
        json.dump(aud, f, default=float)
    summ = {n: {s: summarize(by[n][s]) for s in SPLITS} | {"all": summarize([r for s in SPLITS for r in by[n][s]]),
                                                         "test+forward": summarize(by[n]["test"] + by[n]["forward"])}
            for n in by}
    with open(f"{OUT3}/replay3_summary.json", "w") as f:
        json.dump(summ, f, indent=1, default=float)
    print(f"done in {time.time() - t0:.0f} s -> {OUT3}")


def summarize(recs):
    tr = [r for r in recs if r["traded"]]
    pnl = [r.get("total", 0.0) for r in tr]
    n = len(tr)
    mean = statistics.mean(pnl) if pnl else 0.0
    sd = statistics.pstdev(pnl) if n > 1 else 0.0
    eq = peak = dd = 0.0
    for r in recs:
        eq += r.get("total", 0.0)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    g = lambda k: sum(r.get(k, 0.0) for r in recs)   # noqa: E731
    st = defaultdict(int)
    rej = defaultdict(int)
    for r in recs:
        for k, v in (r.get("states") or {}).items():
            st[k] += v
        for k, v in (r.get("rej") or {}).items():
            rej[k] += v
    out = {"windows": len(recs), "markets": n, "cycles": g("cycles"), "net": sum(pnl), "per_market": mean,
           "t": mean / (sd / math.sqrt(n)) if n > 1 and sd > 0 else 0.0, "max_dd": dd,
           "worst": min(pnl) if pnl else 0.0, "win_rate": sum(1 for x in pnl if x > 0) / n if n else 0.0,
           "baseline_first_legs": g("prediction"), "entries": g("entries"), "entry_fills": g("entry_fills"),
           "reentry_blocked": g("reentry_blocked"), "fees": g("fees"), "slippage": g("slippage"),
           "capital": g("capital"), "recon_max_err": max((r["recon_err"] for r in recs), default=0.0),
           "states": dict(st)}
    if any("hedges" in r for r in recs):                         # strategy B (v2)
        pcs = [x for r in recs for x in r.get("pair_costs", [])]
        out.update({"hedges_sent": g("hedges"), "hedges_filled": g("hedge_fills"), "hedging_pnl": g("hedging"),
                    "pairs_pnl": g("pairs"), "markets_with_2_cycles": sum(1 for r in recs if r["cycles"] >= 2),
                    "avg_pair_cost": statistics.mean(pcs) if pcs else None,
                    "pairs_above_1": sum(1 for x in pcs if x > 1.0)})
        return out
    eq_ = g("recovery_edge_qty")
    out.update({
        "markets_adverse": g("adverse_episodes_mkts"), "markets_reversal_confirmed": g("recovery_opportunities_mkts"),
        "markets_recovery_evaluated": g("recovery_evaluations_mkts"), "markets_recovery_sent": g("recovery_sent_mkts"),
        "markets_recovery_filled": g("recovery_fills_mkts"), "markets_profit_lock_opportunity":
            g("profit_lock_opportunities_mkts"), "markets_profit_lock_filled": g("profit_lock_fills_mkts"),
        "markets_reentry_blocked": g("reentry_blocked_mkts"),
        "adverse_episodes": g("adverse_episodes"), "recovery_opportunities": g("recovery_opportunities"),
        "recovery_evaluations": g("recovery_evaluations"), "recovery_candidates": g("recovery_candidates"),
        "recovery_rejected": g("recovery_rejected"), "recovery_rejected_by": dict(rej),
        "recovery_sent": g("recovery_sent"), "recovery_filled": g("recovery_fills"), "recovery_kills": g("recovery_kills"),
        "recovery_fill_rate": g("recovery_fills") / g("recovery_sent") if g("recovery_sent") else None,
        "recovery_avg_qty": g("recovery_qty") / g("recovery_fills") if g("recovery_fills") else None,
        "recovery_avg_pair_cost": g("recovery_paircost_x_qty") / eq_ if eq_ else None,
        "recovery_downside_total": g("recovery_downside"),
        "recovery_avg_downside": g("recovery_downside") / g("recovery_fills") if g("recovery_fills") else None,
        "recovery_markets": st.get("recovery_markets", 0), "recovery_settlement_pnl": g("recovery_market_pnl"),
        "recovery_markets_baseline": g("recovery_market_baseline"), "recovery_leg_pnl": g("recovery_leg"),
        "profit_lock_opportunities": g("profit_lock_opportunities"), "profit_lock_rejected": g("profit_lock_rejected"),
        "profit_lock_sent": g("profit_lock_sent"), "profit_lock_filled": g("profit_lock_fills"),
        "profit_lock_kills": g("profit_lock_kills"),
        "profit_lock_fill_rate": g("profit_lock_fills") / g("profit_lock_sent") if g("profit_lock_sent") else None,
        "profit_lock_avg_qty": g("profit_lock_qty") / g("profit_lock_fills") if g("profit_lock_fills") else None,
        "profit_lock_markets": st.get("profit_lock_markets", 0), "profit_lock_settlement_pnl": g("profit_lock_market_pnl"),
        "profit_lock_markets_baseline": g("profit_lock_market_baseline"), "profit_lock_leg_pnl": g("profit_lock_leg"),
        "profit_lock_locked": g("profit_lock_locked"),
        "avg_pair_cost": g("pair_cost_sum") / g("matched_markets") if g("matched_markets") else None,
        "pairs_above_1": st.get("pairs_above_1", 0), "markets_with_2_cycles": sum(1 for r in recs if r["cycles"] >= 2)})
    return out


def cmd_smoke():
    """A few windows of every strategy: the code paths run and reconcile (no conclusions)."""
    global _WS, _M, _M2
    _WS = R.load_cache()[-40:]
    _M = pl.ProbModel.load(MODEL_PATH)
    from troll_poly_bot.strategy import pairlock_v2 as plv2
    _M2 = plv2.ProbModel.load(MODEL_PATH)
    for n, k, kw in JOBS + DIAG[3:4] + DIAG[7:8]:
        recs = [r for s in SPLITS for r in _job((n, k, kw, s))[2]]
        sm = summarize(recs)
        print(n, {x: sm.get(x) for x in ("markets", "cycles", "net", "baseline_first_legs", "recon_max_err",
                                          "recovery_opportunities", "recovery_evaluations", "recovery_sent",
                                          "recovery_filled", "profit_lock_opportunities", "profit_lock_filled",
                                          "reentry_blocked", "hedges_sent", "markets_with_2_cycles")})


if __name__ == "__main__":
    {"run": cmd_run, "smoke": cmd_smoke}[sys.argv[1]]()
