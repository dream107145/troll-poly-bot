"""Pair-lock v2 replay (2026-10-02 brief): strategies A / B / C on identical chronological BTC windows, with the
v2 decision code and accounting. Since the 2026-10-02 recovery-hedge rewrite those are the FROZEN copies
strategy/pairlock_v2.py + pairlock_book_v2.py (strategy B, which the paper bot runs through pairlock_engine_v2.py);
the A / C / D / E replay is scripts/pairlock/replay3.py.

    PAIRLOCK_OUT=<cache dir> python scripts/pairlock/replay2.py prepare     model + short model + execution constants
    PAIRLOCK_OUT=<cache dir> python scripts/pairlock/replay2.py grid        the structured grid (TRAIN -> VALIDATION)
    PAIRLOCK_OUT=<cache dir> python scripts/pairlock/replay2.py final       A / B / C / baseline + every analysis

Execution (the paper venue's CALIBRATED scenario, as the bot runs): decision -> arrival after LAT_MS -> the order's
size is walked through the ask levels at arrival (the 200 ms archive carries the touch and the depth within two
ticks: modelled as three levels ask, ask+1c, ask+2c, a third each), with 80 % of every level taken by faster
traders; the book 1 s later must still fill the size at or under the limit; the fill is charged the worse VWAP of
the two books; taker fee 7 % x p(1-p) per level; the result is known after the survival window + one ack. A FOK
that cannot fill is killed -- no partial inventory, nothing assumed.

Splits: chronological TRAIN 50 / VALIDATION 20 / TEST 15 / FORWARD 15 % of windows; the models and the execution
constants are fitted on TRAIN only; parameters are chosen on VALIDATION; TEST / FORWARD are reported untouched.
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
from dataclasses import asdict, replace

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
sys.path.insert(0, "/root/troll-poly-bot/src")


def _load_v1():
    """scripts/pairlock/replay.py under its own module name: a bare ``import replay`` can pick up
    scripts/replay.py (another tool with the same file name) when both are on sys.path."""
    import importlib.util
    name = "pairlock_replay_v1"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, "/root/troll-poly-bot/scripts/pairlock/replay.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load_v1()                                         # data, features cache, triggers, logistic fit
from troll_poly_bot.strategy import pairlock_v2 as pl          # noqa: E402  (FROZEN v2 = strategy B)
from troll_poly_bot.strategy import pairlock_book_v2 as pb     # noqa: E402

ROOT = "/root/troll-poly-bot"
OUT = R.OUT
MODEL_PATH = os.environ.get("PAIRLOCK2_MODEL", f"{ROOT}/data/replay/pairlock2_model.json")
RES = f"{ROOT}/results"
LAT_MS, ACK_MS, SURVIVAL_MS = R.LAT_MS, R.ACK_MS, 1000.0
HAIRCUT = R.CONTENTION
SHORT_N_S = 10.0
SPLIT_NAMES = ("train", "validation", "test", "forward")
LEFT_BUCKETS = ((240, 301, "240-300"), (180, 240, "180-240"), (120, 180, "120-180"), (90, 120, "90-120"),
                (60, 90, "60-90"), (30, 60, "30-60"), (10, 30, "10-30"))


def left_bucket(x):
    return next((lab for lo, hi, lab in LEFT_BUCKETS if lo <= x < hi), "<10")


# ───────────────────────────── executable books and the FOK ─────────────────────────────


def levels(w, side, i):
    ask, bid, d = R.book(w, side, i)
    if ask is None:
        return None, bid, []
    d = max(d, 0.0)
    lv = [(round(min(ask + k * 0.01, 0.99), 2), d / 3.0) for k in range(3)]
    return ask, bid, lv


def fok(w, i_dec, side, q, limit):
    """(status, fill VWAP, fee per share, time the result is known)."""
    ts = w.t
    t_arr = float(ts[i_dec]) + LAT_MS
    j = int(np.searchsorted(ts, t_arr, side="left"))
    if j >= len(ts) or ts[j] >= w.close:
        return "market_closed", None, 0.0, t_arr + ACK_MS
    _, _, lv = levels(w, side, j)
    vw, fps, used = pb.vwap_buy(lv, q, HAIRCUT)
    if vw is None or max(p for p, _ in used) > limit + 1e-9:
        return "fok_unfillable", None, 0.0, float(ts[j]) + ACK_MS
    k = int(np.searchsorted(ts, t_arr + SURVIVAL_MS, side="left"))
    if k >= len(ts) or ts[k] >= w.close:
        return "quote_pulled", None, 0.0, t_arr + SURVIVAL_MS + ACK_MS
    _, _, lv2 = levels(w, side, k)
    vw2, fps2, used2 = pb.vwap_buy(lv2, q, 0.0)
    if vw2 is None or max(p for p, _ in used2) > limit + 1e-9:
        return "quote_pulled", None, 0.0, t_arr + SURVIVAL_MS + ACK_MS
    if vw2 > vw:
        vw, fps = vw2, fps2
    return "filled", vw, fps, t_arr + SURVIVAL_MS + ACK_MS


# ───────────────────────────── one window ─────────────────────────────


def _bid(w, side, i):
    x = w.ub[i] if side == "UP" else w.db[i]
    return None if x != x else float(x)


def _mtm_at(book: pb.PairBook, w, t):
    i = int(np.searchsorted(w.t, t, side="right")) - 1
    if i < w.i0 or t >= w.close:
        return None
    return book.mark_to_market_pnl(_bid(w, "UP", i), _bid(w, "DOWN", i))


def _counterfactual(w, cyc: pl.Cycle, t, cands, outcome):
    """NO / PARTIAL (the best partial, else 50 %) / FULL hedge at the decision's VWAP: P&L at +1 / +5 / +10 s
    (executable bids) and at settlement. Fill risk ignored on purpose: this asks whether the HEDGE has value."""
    out = {}
    b = cyc.book
    by = {round(c.frac, 2): c for c in cands}
    for name, frac in (("NO", 0.0), ("PARTIAL", 0.5), ("FULL", 1.0)):
        c = by.get(frac)
        if c is None or (frac > 0 and (c.vwap is None or c.q <= 0)):
            continue
        hb = pb.PairBook.from_json(b.to_json())
        hb.pending.clear()
        if c.q > 0:
            light = "DOWN" if b.heavy_side == "UP" else "UP"
            hb.add_fill(pb.FillRecord(w.slug, t, light.lower(), light, c.q, c.vwap, c.q * c.fee_per_share, "cf",
                                      f"cf-{name}-{t}", kind="hedge"))
        out[name] = {h: _mtm_at(hb, w, t + h * 1000.0) for h in (1, 5, 10)}
        out[name]["settle"] = hb.resolution_pnl(outcome)
    return out


def run_window(w, strat, prm, lim, model, trig, allow_entries=True, keep=False):
    tl = w.t.tolist()
    cycles: list[pl.Cycle] = []
    pend = None
    seq = itertools.count(1)
    audit, evals, hedge_log, entry_log = [], [], [], []
    cnt = defaultdict(int)

    def exposure():
        cap = sum(c.book.cash_spent for c in cycles)
        sh = sum(c.book.up_qty + c.book.down_qty for c in cycles)
        if pend is not None:
            cap += pend["q"] * (pend["limit"] + pb.fee_per_share(pend["limit"]))
            sh += pend["q"]
        worst = sum(c.book.worst_case_pnl for c in cycles if c.book.fills)
        return pl.MarketExposure(capital=cap, shares=sh, worst_case=worst, cycles=len(cycles))

    def audit_row(cyc, t, i):
        if not keep:
            return
        b = cyc.book
        f = R.feat_at(w, i) if i is not None else None
        pu = model.p_up(f) if f else None
        audit.append({"t": t, "state": cyc.state.value, **b.summary(_bid(w, "UP", i), _bid(w, "DOWN", i), pu)})

    def resolve(p, i):
        cyc = p["cyc"]
        if p["status"] == "filled":
            fill = pb.FillRecord(w.slug, p["t_result"], p["side"].lower(), p["side"], p["q"], p["px"],
                                 p["q"] * p["fee_ps"], p["oid"], f"{p['oid']}-1", "BUY", p["ask"], p["kind"])
            pl.on_fill(cyc, fill, prm, p["t_result"])
            pl.on_order_done(cyc, p["oid"], True, p["t_result"], prm=prm)
            cnt[p["kind"] + "_fills"] += 1
            log = entry_log if p["kind"] == "entry" else hedge_log
            log.append({"left": p["left"], "q": p["q"], "px": p["px"], "ask": p["ask"], "label": p.get("label"),
                        "ev": p.get("ev"), "win": 1.0 if w.outcome == p["side"] else 0.0, "status": "filled"})
            audit_row(cyc, p["t_result"], i)
        else:
            pl.on_order_done(cyc, p["oid"], False, p["t_result"], p["status"])
            cnt[p["kind"] + "_kills"] += 1
            if p["kind"] == "hedge":
                hedge_log.append({"left": p["left"], "q": p["q"], "px": None, "ask": p["ask"], "label": p.get("label"),
                                  "ev": p.get("ev"), "status": p["status"]})
            elif not cyc.book.fills and cyc in cycles:
                cycles.remove(cyc)

    for i in range(w.i0, w.i1):
        t = tl[i]
        if pend is not None and t >= pend["t_result"]:
            p, pend = pend, None
            resolve(p, i)
        if pend is not None:
            continue
        cur = cycles[-1] if cycles else None
        left = (w.close - t) / 1000.0
        watching = cur is not None and cur.state in pb.HEDGE_WATCH_STATES and strat in ("B", "C")
        d = int(trig[i - w.i0]) if allow_entries else 0
        if not watching and d == 0:
            continue
        f = R.feat_at(w, i)
        if f is None:
            continue
        if watching:
            light = "DOWN" if cur.book.heavy_side == "UP" else "UP"
            ask_b, bid_b, lv = levels(w, light, i)
            dec, why, rec = pl.hedge_step(cur, f, _bid(w, "UP", i), _bid(w, "DOWN", i), ask_b, bid_b, lv, t, left,
                                          model, prm, lim, exposure())
            if rec is not None:
                cnt["evaluations"] += 1
                if rec.get("decision") == "NO_HEDGE":
                    cnt["no_hedge"] += 1
                if keep:
                    cands = pb.evaluate_hedges(cur.book, rec["p_up"], lv, fractions=(0.0, 0.5, 1.0),
                                               min_size=prm.min_size, haircut=prm.depth_haircut)
                    evals.append({"left": left, "decision": rec.get("decision"), "confirmed": rec["confirmed"],
                                  "lockable": rec["lockable"], "adverse": rec["adverse"],
                                  "cf": _counterfactual(w, cur, t, cands, w.outcome)})
            if dec is not None:
                oid = f"h{next(seq)}"
                pl.on_hedge_sent(cur, oid, dec.side, dec.shares, dec.limit, t)
                st, px, fps, tr = fok(w, i, dec.side, dec.shares, dec.limit)
                pend = {"kind": "hedge", "cyc": cur, "side": dec.side, "q": dec.shares, "limit": dec.limit,
                        "ask": ask_b, "status": st, "px": px, "fee_ps": fps, "t_result": tr, "oid": oid, "left": left,
                        "label": dec.kind, "ev": dec.ev}
                cnt["hedges"] += 1
                continue
        if d == 0 or pend is not None:
            continue
        cur = cycles[-1] if cycles else None
        if cur is not None and not cur.closed:
            continue
        side = "UP" if d > 0 else "DOWN"
        ask, bid, lv = levels(w, side, i)
        dec, why = pl.entry_decision(f, side, ask, bid, lv, model, prm, lim, exposure(), left, cur)
        if dec is None:
            cnt["entry_refused"] += 1
            continue
        cyc = pl.Cycle(book=pb.PairBook(w.slug), a_side=side, started_t=t)
        cyc.to(pb.State.INITIAL_ENTRY_CANDIDATE, t, f"signal {side}, EV {dec.ev:+.3f}")
        cycles.append(cyc)
        oid = f"e{next(seq)}"
        pl.on_entry_sent(cyc, oid, dec.shares, dec.limit, t)
        st, px, fps, tr = fok(w, i, side, dec.shares, dec.limit)
        pend = {"kind": "entry", "cyc": cyc, "side": side, "q": dec.shares, "limit": dec.limit, "ask": ask,
                "status": st, "px": px, "fee_ps": fps, "t_result": tr, "oid": oid, "left": left, "ev": dec.ev}
        cnt["entries"] += 1
    if pend is not None:
        p = pend
        if p["status"] == "filled":
            resolve(p, w.i1 - 1)              # matched before the close; its result lands after the last sample
        else:
            resolve({**p, "status": "market_closed"}, w.i1 - 1)
    # ---- settlement and reconciliation
    tot = defaultdict(float)
    pcs, recon_err, states = [], 0.0, defaultdict(int)
    for c in cycles:
        if not c.book.fills:
            continue
        pl.on_close(c, w.close)
        r = pl.on_settled(c, w.outcome, w.close + 40_000.0)
        # independent reconstruction from the raw fills (not through PairBook)
        cash = sum(-(f.qty * f.price + f.fee) if f.action == "BUY" else (f.qty * f.price - f.fee) for f in c.book.fills)
        settle = sum(f.qty * (1 if f.action == "BUY" else -1) for f in c.book.fills if f.side == w.outcome)
        recon_err = max(recon_err, abs((settle + cash) - r["pnl"]), abs(r["pnl_from_cash"] - r["pnl"]))
        for k in ("total", "prediction", "hedging", "pairs", "directional"):
            tot[k] += r[k]
        tot["fees"] += c.book.fees
        tot["slippage"] += c.book.slippage
        tot["capital"] += c.book.cash_spent
        tot["worst_case"] += c.book.worst_case_pnl
        if c.book.matched_qty > 0:
            pcs.append(c.book.pair_cost_per_share)
        reached = {x.to for x in c.transitions}
        for s in ("PAIR_FORMED", "PAIR_LOCKED", "ADVERSE_MOVE", "HEDGE_EVALUATION", "NO_HEDGE"):
            if s in reached:
                states[s] += 1
        if c.book.unmatched >= prm.min_size - 1e-9:
            states["naked_at_settlement"] += 1
        if c.book.matched_qty > 0 and c.book.unmatched > 1e-9:
            states["incomplete_pair"] += 1
            tot["incomplete_pnl"] += r["directional"]
        if keep:
            audit.append({"t": w.close + 40_000.0, "state": c.state.value, "settled": w.outcome,
                          "pnl": r["pnl"], "pnl_from_cash": settle + cash, **c.book.summary()})
    out = {"slug": w.slug, "day": w.day, "outcome": w.outcome, "cycles": sum(1 for c in cycles if c.book.fills),
           **dict(cnt), **{k: round(v, 6) for k, v in tot.items()}, "pair_costs": pcs, "states": dict(states),
           "recon_err": recon_err, "traded": int(any(c.book.fills for c in cycles)),
           "hedge_log": hedge_log, "entry_log": entry_log}
    if keep:
        out.update({"audit": audit, "evals": evals})
    return out


def run_split(ws, strat, prm, lim, model, trig_k=2.5, daily_loss=40.0, keep=False):
    recs, day, day_pnl = [], None, 0.0
    for w in ws:
        if w.trig is None or w.trig[0] != trig_k:
            w.trig = (trig_k, R.triggers(w, trig_k))
        if w.day != day:
            day, day_pnl = w.day, 0.0
        r = run_window(w, strat, prm, lim, model, w.trig[1], allow_entries=day_pnl > -daily_loss, keep=keep)
        day_pnl += r.get("total", 0.0)
        recs.append(r)
    return recs


def summarize(recs, label=""):
    tr = [r for r in recs if r["traded"]]
    pnl = [r.get("total", 0.0) for r in tr]
    n = len(tr)
    cap = sum(r.get("capital", 0.0) for r in tr)
    eq = peak = dd = 0.0
    for r in recs:
        eq += r.get("total", 0.0)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    pcs = [x for r in recs for x in r["pair_costs"]]
    st = defaultdict(int)
    for r in recs:
        for k, v in r["states"].items():
            st[k] += v
    cyc = sum(r["cycles"] for r in recs)
    he, hf = sum(r.get("hedges", 0) for r in recs), sum(r.get("hedge_fills", 0) for r in recs)
    mean = statistics.mean(pnl) if pnl else 0.0
    sd = statistics.pstdev(pnl) if n > 1 else 0.0
    return {"label": label, "windows": len(recs), "markets": n, "cycles": cyc,
            "entries": sum(r.get("entries", 0) for r in recs), "entry_fills": sum(r.get("entry_fills", 0) for r in recs),
            "hedges": he, "hedge_fills": hf, "hedge_success": hf / he if he else None,
            "evaluations": sum(r.get("evaluations", 0) for r in recs), "no_hedge": sum(r.get("no_hedge", 0) for r in recs),
            "pair_formed": st["PAIR_FORMED"], "pair_locked": st["PAIR_LOCKED"],
            "lock_rate": st["PAIR_LOCKED"] / cyc if cyc else 0.0,
            "incomplete": st["incomplete_pair"], "incomplete_rate": (st["incomplete_pair"] + sum(r.get("hedge_kills", 0) for r in recs)) / he if he else None,
            "naked": st["naked_at_settlement"], "pair_cost_avg": statistics.mean(pcs) if pcs else None,
            "net": sum(pnl), "per_market": mean, "se": sd / math.sqrt(n) if n > 1 else 0.0,
            "t": mean / (sd / math.sqrt(n)) if n > 1 and sd > 0 else 0.0,
            "per_100": 100.0 * sum(pnl) / cap if cap > 0 else 0.0, "capital_per_market": cap / n if n else 0.0,
            "max_capital": max((r.get("capital", 0.0) for r in recs), default=0.0), "max_dd": dd,
            "p05": float(np.percentile(pnl, 5)) if pnl else 0.0, "worst": min(pnl) if pnl else 0.0,
            "fees": sum(r.get("fees", 0.0) for r in recs), "slippage": sum(r.get("slippage", 0.0) for r in recs),
            "prediction": sum(r.get("prediction", 0.0) for r in recs), "hedging": sum(r.get("hedging", 0.0) for r in recs),
            "pairs": sum(r.get("pairs", 0.0) for r in recs), "directional": sum(r.get("directional", 0.0) for r in recs),
            "recon_max_err": max((r["recon_err"] for r in recs), default=0.0),
            "win_rate": sum(1 for x in pnl if x > 0) / n if n else 0.0}


# ───────────────────────────── prepare: models + execution constants (TRAIN only) ─────────────────────────────


def fit_short(train, trig_k=2.5):
    """P(the traded token's bid N s after the decision >= its ask at the decision) on the side-signed features,
    at every trigger moment of the TRAIN windows."""
    X, y = [], []
    for w in train:
        if w.trig is None or w.trig[0] != trig_k:
            w.trig = (trig_k, R.triggers(w, trig_k))
        for k, i in enumerate(range(w.i0, w.i1)):
            d = int(w.trig[1][k])
            if d == 0:
                continue
            f = R.feat_at(w, i)
            if f is None:
                continue
            side = "UP" if d > 0 else "DOWN"
            ask, _, _ = R.book(w, side, i)
            j = int(np.searchsorted(w.t, w.t[i] + SHORT_N_S * 1000.0, side="right")) - 1
            if ask is None or j >= w.i1:
                continue
            b = _bid(w, side, j)
            if b is None:
                continue
            sf = pl.side_features(f, side)
            X.append([sf[k_] for k_ in pl.SHORT])
            y.append(1.0 if b >= ask else 0.0)
    X, y = np.array(X), np.array(y)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    beta = R.fit_logistic((X - mu) / sd, y, l2=1.0)
    # back to raw units: b0' = b0 - sum(b_k mu_k / sd_k), b_k' = b_k / sd_k
    b_raw = [float(beta[0] - np.sum(beta[1:] * mu / sd))] + [float(x) for x in beta[1:] / sd]
    return {"features": list(pl.SHORT), "beta": b_raw, "horizon_s": SHORT_N_S, "n": int(len(y)),
            "base_rate": float(y.mean())}


def calibrate_exec(train, model, prm, lim, trig_k=2.5):
    m0 = pl.ProbModel.from_json(model.to_json())
    m0.exec = {"entry_fill_rate": 1.0, "entry_slip": 0.0, "entry_adv": 0.0, "hedge_fill_rate": 1.0, "hedge_slip": 0.0}
    prm0 = replace(prm, entry_edge=-1.0)
    fills, adv, slip, sent = 0, [], [], 0
    for w in train:
        if w.trig is None or w.trig[0] != trig_k:
            w.trig = (trig_k, R.triggers(w, trig_k))
        for k, i in enumerate(range(w.i0, w.i1)):
            d = int(w.trig[1][k])
            if d == 0:
                continue
            f = R.feat_at(w, i)
            if f is None:
                continue
            side = "UP" if d > 0 else "DOWN"
            ask, bid, lv = levels(w, side, i)
            left = (w.close - float(w.t[i])) / 1000.0
            dec, _ = pl.entry_decision(f, side, ask, bid, lv, m0, prm0, lim, pl.MarketExposure(), left, None)
            if dec is None:
                continue
            sent += 1
            st, px, _fps, _ = fok(w, i, side, dec.shares, dec.limit)
            if st != "filled":
                continue
            fills += 1
            slip.append(px - dec.info["vwap"])
            adv.append(dec.p - (1.0 if w.outcome == side else 0.0))
    hs, hf, hslip = 0, 0, []
    prmB = replace(prm, hedge_mode="unconditional", entry_edge=-1.0)
    for w in train:
        r = run_window(w, "B", prmB, lim, m0, w.trig[1])
        hs += r.get("hedges", 0)
        hf += r.get("hedge_fills", 0)
        hslip += [x["px"] - x["ask"] for x in r["hedge_log"] if x.get("px") is not None and x.get("ask")]
    return {"entry_fill_rate": fills / max(1, sent), "entry_slip": statistics.mean(slip) if slip else 0.0,
            "entry_adv": statistics.mean(adv) if adv else 0.0, "hedge_fill_rate": hf / max(1, hs),
            "hedge_slip": statistics.mean(hslip) if hslip else 0.0, "n_entries": sent, "n_entry_fills": fills,
            "n_hedges": hs, "n_hedge_fills": hf}


def base_params():
    return pl.PairLockParams(depth_haircut=HAIRCUT), pl.RiskLimits()


def cmd_prepare():
    ws = R.load_cache()
    sp = R.split(ws)
    model, rep = R.fit_model(sp)                    # P(UP) on the v2 features, TRAIN only
    model = pl.ProbModel(features=pl.FEATURES, beta=model.beta, rev_scales=model.rev_scales, meta=model.meta)
    # the reversal score's new components get their TRAIN scales too
    X, _, _, _ = R.samples(sp["train"])
    for k in pl.REVERSAL:
        model.rev_scales[k] = float(np.std(X[:, pl.FEATURES.index(k)])) or 1.0
    model.short = fit_short(sp["train"])
    prm, lim = base_params()
    ex = calibrate_exec(sp["train"], model, prm, lim)
    model.exec = {k: float(v) for k, v in ex.items() if not k.startswith("n_")}
    model.meta.update({"exec_counts": {k: v for k, v in ex.items() if k.startswith("n_")}, "fit_report": rep,
                       "version": 2, "splits": {k: [v[0].slug, v[-1].slug, len(v)] for k, v in sp.items()}})
    model.save(MODEL_PATH)
    print(json.dumps({"fit": rep, "short": {k: v for k, v in model.short.items() if k != "beta"}, "exec": ex},
                     indent=1, default=str))


# ───────────────────────────── batches ─────────────────────────────

_WS = None
_MODEL = None


def _job(arg):
    name, strat, kw, lim_kw, splits_, keep = arg
    prm0, lim0 = base_params()
    prm = replace(prm0, **kw)
    lim = replace(lim0, **lim_kw)
    sp = R.split(_WS)
    out = {}
    for s in splits_:
        recs = run_split(sp[s], strat, prm, lim, _MODEL, keep=keep)
        out[s] = summarize(recs, f"{name} {s}")
        if keep:
            out[s]["_recs"] = recs
    return name, kw, lim_kw, out


def batch(jobs, splits_=SPLIT_NAMES, keep=False, procs=4):
    global _WS, _MODEL
    import multiprocessing as mp
    if _WS is None:
        _WS = R.load_cache()
        _MODEL = pl.ProbModel.load(MODEL_PATH)
    args = [(n, s, kw, lk, list(splits_), keep) for n, s, kw, lk in jobs]
    if procs <= 1 or len(args) == 1:
        return [_job(a) for a in args]
    with mp.get_context("fork").Pool(procs) as pool:
        return pool.map(_job, args, chunksize=1)


STRATS = {"BASELINE": ("A", {"hedge_mode": "none", "entry_edge": -1.0}),
          "A": ("A", {"hedge_mode": "none"}),
          "B": ("B", {"hedge_mode": "unconditional"}),
          "C": ("C", {"hedge_mode": "conditional"})}


def cmd_grid():
    """Stage 1: the entry (A) on TRAIN, chosen on VALIDATION. Stage 2: C's hedge / lock settings on the chosen
    entry. Stage 3: B's trigger. A small structured grid, neighbours reported for stability."""
    t0 = time.time()
    jobs = []
    for e, mode, band in itertools.product((0.0, 0.02, 0.04), ("settlement", "both"), ((0.20, 0.96), (0.30, 0.85))):
        jobs.append((f"A e{e} {mode} {band[0]}-{band[1]}", "A",
                     {"hedge_mode": "none", "entry_edge": e, "entry_mode": mode, "short_min": 0.55,
                      "band_lo": band[0], "band_hi": band[1]}, {}))
    res = batch(jobs, splits_=("train", "validation"))
    stage1 = [(n, kw, out) for n, kw, _, out in res]
    ok = [x for x in stage1 if x[2]["train"]["markets"] >= 60]
    top = sorted(ok, key=lambda x: -x[2]["train"]["t"])[:4]
    best = max(top, key=lambda x: x[2]["validation"]["per_market"])
    entry = {k: v for k, v in best[1].items() if k != "hedge_mode"}
    jobs = []
    for rev, conf, mev, mpe in itertools.product((0.5, 1.0, 1.5), (200.0, 400.0, 1000.0), (-0.05, 0.0, 0.05),
                                                 (0.02, 0.05)):
        jobs.append((f"C rev{rev} conf{conf:.0f} mev{mev} mpe{mpe}", "C",
                     {**entry, "hedge_mode": "conditional", "rev_thr": rev, "confirm_ms": conf, "min_hedge_ev": mev,
                      "min_pair_edge": mpe}, {}))
    for trig in (0.03, 0.05, 0.10):
        jobs.append((f"B trig{trig}", "B", {**entry, "hedge_mode": "unconditional", "unconditional_trigger": trig}, {}))
    for mpe in (0.08, 0.06, 0.05, 0.04, 0.03, 0.02, 0.01):            # lock thresholds = pair cost 0.92 .. 0.99
        jobs.append((f"C lock-only mpe{mpe}", "C", {**entry, "hedge_mode": "conditional", "rev_thr": 99.0,
                                                    "min_pair_edge": mpe, "min_hedge_ev": -1.0}, {}))
    res2 = batch(jobs, splits_=("train", "validation"))
    out = {"stage1": [(n, kw, {s: {k: v for k, v in o.items()} for s, o in out_.items()}) for n, kw, out_ in stage1],
           "entry": entry, "stage2": [(n, kw, out_) for n, kw, _, out_ in res2]}
    json.dump(out, open(f"{OUT}/grid2.json", "w"), indent=1, default=float)
    print(f"grid: {len(jobs) + len(stage1)} settings in {time.time() - t0:.0f} s; entry chosen {best[0]}")


def cmd_final():
    """Every strategy on every split with the chosen settings; the analyses; the audit trail; reconciliation."""
    g = json.load(open(f"{OUT}/grid2.json"))
    entry = g["entry"]
    c_rows = [r for r in g["stage2"] if r[0].startswith("C rev")]
    ok = [r for r in c_rows if r[2]["train"]["markets"] >= 60]
    top = sorted(ok, key=lambda r: -r[2]["train"]["t"])[:6]
    cbest = max(top, key=lambda r: r[2]["validation"]["per_market"])
    b_rows = [r for r in g["stage2"] if r[0].startswith("B trig")]
    bbest = max(b_rows, key=lambda r: r[2]["validation"]["per_market"])
    jobs = [("BASELINE (every trigger, no gate, no hedge)", "A", {"hedge_mode": "none", "entry_edge": -1.0,
                                                                   "band_lo": entry["band_lo"], "band_hi": entry["band_hi"]}, {}),
            ("A directional only", "A", {**entry, "hedge_mode": "none"}, {}),
            (f"B unconditional ({bbest[0][2:]})", "B", bbest[1], {}),
            (f"C conditional ({cbest[0][2:]})", "C", cbest[1], {})]
    res = batch(jobs, keep=True)
    final = {"entry": entry, "c": cbest[1], "b": bbest[1], "runs": {}}
    audit_path = f"{RES}/pairlock2_audit_trail.jsonl"
    with open(audit_path, "w") as fa:
        for name, kw, _, out in res:
            final["runs"][name] = {s: {k: v for k, v in o.items() if k != "_recs"} for s, o in out.items()}
            final["runs"][name]["_analysis"] = analyse({s: o["_recs"] for s, o in out.items()})
            if name.startswith("C "):
                for s, o in out.items():
                    for r in o["_recs"]:
                        if r.get("audit"):
                            fa.write(json.dumps({"slug": r["slug"], "split": s, "rows": r["audit"]}, default=float) + "\n")
    json.dump(final, open(f"{OUT}/final2.json", "w"), indent=1, default=float)
    print("final written", audit_path)


def analyse(recs_by_split):
    """Time-to-expiry, the hedge counterfactuals, regimes, failure buckets (validation + test + forward)."""
    oos = [r for s in ("validation", "test", "forward") for r in recs_by_split.get(s, [])]
    out = {}
    hl = [h for r in oos for h in r["hedge_log"]]
    tb = defaultdict(lambda: {"sent": 0, "filled": 0, "ev": 0.0})
    for h in hl:
        b = tb[left_bucket(h["left"])]
        b["sent"] += 1
        b["filled"] += 1 if h.get("px") is not None else 0
        b["ev"] += h.get("ev") or 0.0
    out["hedges_by_left"] = dict(tb)
    el = defaultdict(list)
    for r in oos:
        for e in r["entry_log"]:
            el[left_bucket(e["left"])].append(r.get("total", 0.0))
    out["entries_by_left"] = {k: (len(v), float(np.mean(v))) for k, v in el.items()}
    cf = defaultdict(lambda: defaultdict(list))
    for r in oos:
        for e in r.get("evals", []):
            key = "confirmed reversal" if e["confirmed"] else ("lock available" if e["lockable"] else "other")
            for lab in ("PARTIAL", "FULL"):
                if lab in e["cf"] and "NO" in e["cf"]:
                    for h in (1, 5, 10, "settle"):
                        a, b = e["cf"][lab].get(h), e["cf"]["NO"].get(h)
                        if a is not None and b is not None:
                            cf[(key, lab)][h].append(a - b)
                            cf[(key + " @" + left_bucket(e["left"]), lab)][h].append(a - b)
    out["counterfactual"] = {f"{k[0]}|{k[1]}": {str(h): (len(v), float(np.mean(v))) for h, v in d.items()}
                             for k, d in cf.items()}
    return out


if __name__ == "__main__":
    {"prepare": cmd_prepare, "grid": cmd_grid, "final": cmd_final}[sys.argv[1]]()
