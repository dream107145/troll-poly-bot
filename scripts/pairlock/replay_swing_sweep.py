"""SWING RECOVERY PARAMETER SWEEP (the user's 2026-10-07 brief): is there a robust positive-EV parameter region for the
recovery paper bot's swing strategy (strategy/pairlock_swing.py) under REALISTIC historical execution?

Nothing here changes the bot. Every candidate runs through:

  * the bot's own decision code -- pairlock_swing.entry_decision / evaluate / observe, the state machine INITIAL_ENTRY ->
    SWING_0 -> SWING_1_RECOVERY -> SWING_2_RECOVERY -> HEDGE_MODE -> RESOLUTION, unchanged, with the candidate's
    RecoveryParams (entry window / band, hysteresis, trigger, swing sizes, capital cap);
  * the existing realistic fill model, scripts/pairlock/replay_swing.execute("realistic"): the order arrives 143 ms after
    the decision and walks the recorded asks THEN (80 % of the touch taken first), fill-and-kill up to its limit; the
    book 1 s later must still fill it at or under the limit (else killed); charged at the worse VWAP; the venue fee;
  * three policies the bot does NOT have, applied here as a filter on the bot's decisions (the brief's items 11-14):
      - RECOVERY PRICE LIMIT (rec_max): a swing-1 / swing-2 buy is judged on its decision-time EXECUTABLE VWAP over
        the book: "reduce" buys the largest size whose VWAP <= the limit, "reject" waits unless the whole remaining
        size fits under it. rec_dynamic: the limit is min(rec_max, the break-even price) -- the per-share price at which
        the recovery side winning leaves the market at 0 after every cost, from the inventory held (no model).
      - HEDGE RATIO: the swing-3+ hedge buys ratio x the inventory imbalance (100 % = the bot's UP = DOWN);
        "once" = of the imbalance when hedging starts, "each" = again at every later swing (of the imbalance then).
      - HEDGE PRICE LIMIT (hedge_max): hedge orders never pay above it (the order's limit): hedge partly, wait or skip.
    Swing 2 may be given as a multiple of swing 1 (s2_mult).

Speed: a held market whose state cannot act (no remainder to buy, hedge done, too late to trade) jumps to the next
sample where the swing condition holds -- the only thing that can change its state -- instead of calling evaluate on
every sample. Decision-identical to the sample-by-sample replay (tests/test_pairlock_swing_sweep.py checks it market by
market against replay_swing.run_window).

Selection never looks at the test period: candidates are ranked by a score from TRAIN and VALIDATION only (formula in
selection_score); the test period is reported for the ranked candidates as out-of-sample. Stages: baselines, one
dimension at a time (entry, trigger x hysteresis, sizes, hedge, recovery price, capital), a factorial of each stage's
best values, a neighbourhood around the leaders, stress tests of the top 20.

    nice -n 10 .venv/bin/python scripts/pairlock/replay_swing_sweep.py --workers 5
    ... --max-markets 300 --stages baselines            (a smoke run)
    ... --start-date 2026-09-22 --end-date 2026-10-07 --output results/pairlock_swing_sweep.json
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import itertools
import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from multiprocessing import Pool

import numpy as np

ROOT = "/root/troll-poly-bot"
sys.path.insert(0, f"{ROOT}/scripts/pairlock")
sys.path.insert(0, f"{ROOT}/src")
import replay_swing as RS                                          # noqa: E402  (the existing replay: data, fill model)
from troll_poly_bot.strategy import pairlock_swing as sw            # noqa: E402
from troll_poly_bot.strategy.pairlock_book import FillRecord, PairBook, fee_per_share   # noqa: E402

OTHER = {"UP": "DOWN", "DOWN": "UP"}
DAY = 86_400_000.0
#: the brief's periods (UTC dates of the market's open); data outside them -> a chronological 60 / 20 / 20 split
PERIODS = (("train", "2026-09-22", "2026-10-01"), ("validation", "2026-10-02", "2026-10-04"), ("test", "2026-10-05", "2026-10-07"))


# ───────────────────────────────── candidates ─────────────────────────────────


@dataclass(frozen=True)
class Cand:
    entry_start: float = 20.0
    entry_end: float = 30.0
    entry_min: float = 0.55
    entry_max: float = 0.60
    hysteresis: float = 0.04
    trigger: float = 0.70
    s1: float = 24.0
    s2: float = 80.0
    s2_mult: float = 0.0             # > 0: swing 2 = round(s2_mult x s1) (s2 ignored)
    hedge_ratio: float = 1.0
    hedge_mode: str = "once"         # "once" | "each"
    hedge_max: float = 0.0           # 0 = no hedge price limit
    rec_max: float = 0.0             # 0 = no recovery price limit
    rec_policy: str = "reduce"       # "reduce" | "reject"
    rec_dynamic: bool = False        # limit = min(rec_max or 1, the break-even price)
    capital: float = 125.0

    @property
    def swing2(self) -> float:
        return float(round(self.s2_mult * self.s1)) if self.s2_mult > 0 else self.s2

    def prm(self):
        return replace(RS.BASE, swing_entry_delay_s=self.entry_start, swing_entry_window_s=self.entry_end,
                       swing_entry_min=self.entry_min, swing_entry_max=self.entry_max, swing_leader_hysteresis=self.hysteresis,
                       swing_recovery_price=self.trigger, swing_sizing="fixed", swing1_shares=self.s1, swing2_shares=self.swing2,
                       swing_two_sided_hedge=self.hedge_ratio > 0.0, swing_max_market_capital=self.capital)

    def key(self) -> str:
        d = asdict(self)
        return "|".join(f"{k}={d[k]}" for k in sorted(d))

    def label(self) -> str:
        s2 = f"{self.s2_mult:g}x" if self.s2_mult > 0 else f"{self.s2:g}"
        h = f"h{self.hedge_ratio:.0%}" + (f"/{self.hedge_mode}" if 0 < self.hedge_ratio < 1 else "") + \
            (f"<={self.hedge_max:.2f}" if self.hedge_max else "")
        r = ("rec<=" + (f"{self.rec_max:.2f}" if self.rec_max else "") + ("BE" if self.rec_dynamic else "") + f"/{self.rec_policy}"
             if self.rec_max or self.rec_dynamic else "")
        return (f"e{self.entry_start:g}-{self.entry_end:g}s {self.entry_min:.2f}-{self.entry_max:.2f} | 10/{self.s1:g}/{s2} | "
                f"trig {self.trigger:.2f} hyst {self.hysteresis:.2f} | {h} {r} | cap {self.capital:g}").replace("  ", " ")


@dataclass(frozen=True)
class Stress:
    price_add: float = 0.0           # added to every fill's VWAP (fill-price stress)
    kill: float = 0.0                # share of otherwise filled orders killed (fill-rate stress; deterministic hash)
    ask_shift: float = 0.0           # the asks met at arrival / 1 s later shifted up (slippage stress)
    fee_mult: float = 1.0            # the venue fee x this (fee stress), in sizing and fills

    def name(self) -> str:
        if self.price_add:
            return f"fill price +{self.price_add:.2f}"
        if self.kill:
            return f"fill rate -{self.kill:.0%}"
        if self.ask_shift:
            return f"slippage +{self.ask_shift:.3f}"
        if self.fee_mult != 1.0:
            return f"fees +{self.fee_mult - 1:.0%}"
        return "none"


NO_STRESS = Stress()
STRESSES = [Stress(price_add=x) for x in (0.01, 0.02, 0.03)] + [Stress(kill=x) for x in (0.10, 0.20, 0.30)] + \
           [Stress(ask_shift=x) for x in (0.005, 0.01, 0.02)] + [Stress(fee_mult=x) for x in (1.10, 1.25, 1.50)]


# ───────────────────────────────── policies (not in the bot) ─────────────────────────────────


def breakeven_price(book: PairBook, side: str, q: float, fees) -> float:
    """The highest price per share at which buying ``q`` of ``side`` leaves the side-wins outcome at >= 0 after every
    cost (fee in): p + fee(p) <= (held side + q - total cost + realised) / q. Inventory only, no model."""
    if q <= 0.0:
        return 0.0
    allin = (book.qty(side) + q - book.total_cost + book.realised_exit_pnl) / q
    if allin <= 0.0:
        return 0.0
    lo, hi = 0.0, 0.999
    if hi + fees(hi) <= allin:
        return hi
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if mid + fees(mid) <= allin:
            lo = mid
        else:
            hi = mid
    return lo


def _vwap_capped_qty(levels, want: float, cap: float) -> float:
    """The largest size up to ``want`` whose VWAP over ``levels`` (ascending) stays <= ``cap``."""
    q = cost = 0.0
    for p, s in sorted(levels):
        take = min(s, want - q)
        if take <= 1e-12:
            break
        if (cost + take * p) <= cap * (q + take) + 1e-12:
            q += take
            cost += take * p
            continue
        if p > cap:                                   # part of this level: (cost + x p) / (q + x) = cap
            x = (cap * q - cost) / (p - cap)
            q += max(0.0, min(take, x))
        break
    return math.floor(q * 100.0 + 1e-6) / 100.0


def apply_policy(dec, st, book, mv, prm, c: Cand, ps: dict, free: float, fees):
    """The candidate's policies on the bot's decision: (order or None, why it was changed)."""
    if dec is None or dec.kind == "entry":
        return dec, ""
    if dec.kind == "recovery":
        cap = c.rec_max if c.rec_max > 0 else None
        if c.rec_dynamic:
            be = breakeven_price(book, dec.side, dec.plan_qty, fees)
            cap = be if cap is None else min(cap, be)
        if cap is None:
            return dec, ""
        if c.rec_policy == "reject":                  # the order the bot would send: its executable VWAP over the book
            return (None, "rec_reject") if dec.vwap > cap + 1e-9 else (dec, "")
        levels = [(float(p), float(s)) for p, s in (mv.asks(dec.side) or []) if float(s) > 0 and float(p) <= dec.limit + 1e-9]
        q = min(dec.shares, _vwap_capped_qty(levels, dec.shares, cap))
        if q < float(prm.min_size) - 1e-9:
            return None, "rec_cap"
        if q >= dec.shares - 1e-9:
            return dec, ""
        vw, used = sw.walk(levels, q)
        fps = sum(n * fees(p) for p, n in used) / q
        return sw.SwingOrder("recovery", dec.side, q, dec.limit, vw, fps, dec.plan_qty, dec.swing, dec.reason, dec.record), "rec_cap"
    if dec.kind == "hedge":
        filled = float((st.hedge or {}).get("filled", 0.0))
        if ps.get("target_swing") is None or (c.hedge_mode == "each" and ps["target_swing"] != st.swing_number):
            ps["target"] = filled + c.hedge_ratio * dec.plan_qty
            ps["target_swing"] = st.swing_number
        want = math.floor(max(0.0, min(dec.plan_qty, ps["target"] - filled)) * 100.0 + 1e-6) / 100.0
        if want < float(prm.min_size) - 1e-9:
            return None, "hedge_ratio"
        best = min((float(p) for p, s in (mv.asks(dec.side) or []) if float(s) > 0), default=None)
        top = (best or 0.0) + float(prm.max_slippage)
        if c.hedge_max > 0:
            top = min(top, c.hedge_max)
        if want >= dec.shares - 1e-9 and top >= dec.limit - 1e-9:
            return dec, ""
        s = sw.size_buy(dec.side, want, mv, book, prm, free, top, fees)
        if not s["ok"]:
            return None, "hedge_max" if c.hedge_max > 0 else "hedge_ratio"
        return sw.SwingOrder("hedge", dec.side, s["q"], s["limit"], s["vwap"], s["fee_ps"], dec.plan_qty, dec.swing, dec.reason,
                             dec.record), "hedge_policy"
    return dec, ""


def needs_eval(st, book, prm, c: Cand, ps: dict, left: float, elapsed: float) -> bool:
    """Can this held market act at this sample (other than through a new swing)? False -> jump to the next swing."""
    if left < float(prm.min_time_left_s) or st.phase in (sw.Phase.RESOLUTION, sw.Phase.INITIAL_ENTRY):
        return False
    mn = float(prm.min_size)
    if st.phase is sw.Phase.SWING_0:
        return (st.swing_number == 0 and elapsed <= float(prm.swing_entry_window_s)
                and float(st.entry.get("filled", 0.0)) < float(st.plan["initial_shares"]) - mn + 1e-9)
    if st.phase in sw.RECOVERY_PHASES:
        cur = st.current_swing
        return float(cur["target"]) - float(cur["filled"]) >= mn - 1e-9
    if not st.plan["two_sided_hedge"] or c.hedge_ratio <= 0.0:
        return False
    if abs(book.up_qty - book.down_qty) < mn - 1e-9:
        return False
    if ps.get("target_swing") is None or (c.hedge_mode == "each" and ps["target_swing"] != st.swing_number):
        return True
    return ps["target"] - float((st.hedge or {}).get("filled", 0.0)) >= mn - 1e-9


# ───────────────────────────────── one market ─────────────────────────────────


def _prep(w: dict) -> None:
    if "_valid" not in w:
        w["_mu"] = 0.5 * (w["ub"] + w["ua"])
        w["_md"] = 0.5 * (w["db"] + w["da"])
        w["_valid"] = ~(np.isnan(w["ub"]) | np.isnan(w["ua"]) | np.isnan(w["db"]) | np.isnan(w["da"]))


def swing_index(w: dict, h: float, trig: float, ch: str) -> np.ndarray:
    """Samples where ``ch`` (the challenger) leads the other token by the hysteresis with its ask at the trigger:
    pairlock_swing.observe's swing condition, vectorised (same float operations)."""
    mc, mo = (w["_mu"], w["_md"]) if ch == "UP" else (w["_md"], w["_mu"])
    ask = w["ua"] if ch == "UP" else w["da"]
    with np.errstate(invalid="ignore"):
        lead = mc - mo
        ok = (lead >= h - 1e-12) if h > 0.0 else (lead > 1e-12)
        return np.flatnonzero(w["_valid"] & ok & (ask >= trig - 1e-9))


def _u(slug: str, n: int) -> float:
    return int(hashlib.blake2b(f"{slug}:{n}".encode(), digest_size=8).hexdigest(), 16) / 2.0 ** 64


def run_window(w: dict, c: Cand, stress: Stress = NO_STRESS) -> dict:
    """One market under candidate ``c``: the bot's decisions, the policies, the realistic fill model (+ stress)."""
    _prep(w)
    prm = c.prm()
    fees = fee_per_share if stress.fee_mult == 1.0 else (lambda p, k=stress.fee_mult: fee_per_share(p) * k)
    ts, valid = w["t"], w["_valid"]
    n, close = len(ts), w["close"]
    book = PairBook(w["slug"])
    st: sw.SwingState | None = None
    pend = None
    tries = seq = nsent = 0
    ps: dict = {}
    idx = {}
    m = defaultdict(float)
    for k in ("entry", "s1", "s2", "hedge"):                    # every counter present in every market's row
        for f in ("sent", "fills", "kills", "partial", "qty", "gross"):
            m[f"{k}_{f}"] = 0.0
    m["entry_opps"] = 0.0
    m["cap_bound"] = 0

    def nxt(i: int):
        ch = OTHER[st.direction]
        a = idx.get(ch)
        if a is None:
            a = idx[ch] = swing_index(w, c.hysteresis, c.trigger, ch)
        k = int(np.searchsorted(a, i))
        return int(a[k]) if k < len(a) else None

    def cls(dec):
        return "entry" if dec.kind == "entry" else "hedge" if dec.kind == "hedge" else f"s{min(dec.swing, 2)}"

    def resolve(p):
        nonlocal st, seq
        dec, r = p["dec"], p["res"]
        k = cls(dec)
        if r["status"] == "filled" and stress.kill and _u(w["slug"], p["n"]) < stress.kill:
            r = {**r, "status": "killed_stress"}
        if r["status"] != "filled":
            m[f"{k}_kills"] += 1
            if dec.kind == "entry" and not book.fills:
                st = None
            return
        seq += 1
        px = r["vwap"] + stress.price_add
        m[f"{k}_fills"] += 1
        m[f"{k}_partial"] += int(r["qty"] < dec.shares - 1e-9)
        m[f"{k}_qty"] += r["qty"]
        m[f"{k}_gross"] += r["qty"] * px
        book.add_fill(FillRecord(w["slug"], r["t_result"], dec.side.lower(), dec.side, r["qty"], px, r["fee"],
                                 f"o{seq}", f"o{seq}-1", "BUY", dec.vwap, dec.kind))
        sw.on_fill(st, dec.kind, dec.side, r["qty"], px, r["fee"], r["t_result"], dec.swing)

    i0 = int(np.searchsorted(ts, w["open"] + 1000.0 * c.entry_start - 1.0, side="left"))
    last_entry_t = w["open"] + 1000.0 * c.entry_end
    i = i0
    while i < n:
        t = float(ts[i])
        if t >= close:
            break
        if pend is not None and t >= pend["res"]["t_result"]:
            resolve(pend)
            pend = None
        if st is None and (t > last_entry_t or tries >= prm.open_max_tries) and pend is None:
            break
        if not valid[i]:
            i += 1
            continue
        if pend is not None:
            j_res = int(np.searchsorted(ts, pend["res"]["t_result"], side="left"))
            if st is not None and book.fills and st.phase not in (sw.Phase.INITIAL_ENTRY, sw.Phase.RESOLUTION):
                j = nxt(i)
                if j is not None and j == i and i < j_res:
                    sw.observe(st, RS.view_at(w, i), t)
                    i += 1
                    continue
                i = max(i + 1, min(j_res, j if j is not None else n))
            else:
                i = max(i + 1, j_res)
            continue
        left = (close - t) / 1000.0
        elapsed = (t - w["open"]) / 1000.0
        free = RS.BANKROLL
        if st is None or not book.fills:
            mv = RS.view_at(w, i)
            dec, _ = sw.entry_decision(mv, elapsed, left, prm, free, now=t, fees=fees)
            if dec is None:
                i += 1
                continue
            m["entry_opps"] += 1
            tries += 1
            if st is None:
                st = sw.SwingState.start(dec.side, t, prm)
        else:
            if not needs_eval(st, book, prm, c, ps, left, elapsed):
                j = nxt(i)
                if j is None:
                    break
                if j > i:
                    i = j
                    continue
            mv = RS.view_at(w, i)
            dec, rec = sw.evaluate(st, book, mv, t, left, elapsed, prm, free, fees)
            if "MAX_MARKET_CAPITAL" in str(rec.get("why")) or (rec.get("sizing") or {}).get("bound") == "MAX_MARKET_CAPITAL":
                m["cap_bound"] = 1
            dec, why = apply_policy(dec, st, book, mv, prm, c, ps, free, fees)
            if why:
                m[f"policy_{why}"] = 1
            if dec is None:
                i += 1
                continue
        sw.on_sent(st, dec.kind, dec.swing, t)
        m[f"{cls(dec)}_sent"] += 1
        nsent += 1
        pend = {"dec": dec, "n": nsent, "res": RS.execute("realistic", w, i, dec, prm, fees, stress.ask_shift)}
        i += 1
    if pend is not None and pend["res"]["status"] == "filled":
        resolve(pend)
    out = {"slug": w["slug"], "open": w["open"], "outcome": w["outcome"], "traded": 0, **m}
    if st is None or not book.fills:
        return out
    fin = sw.on_settled(st, book, w["outcome"], close)
    rows = {s["n"]: s for s in st.swings}
    out.update({"traded": 1, "bucket": fin["bucket"], "swings": st.swing_number, "pnl": fin["pnl"], "capital": book.cash_spent,
                "inventory": book.up_qty + book.down_qty, "fees": book.fees, "a_won": int(w["outcome"] == st.initial_token),
                "balanced": int(fin["balanced"]), "hedged": int(fin["hedged"]),
                "first_leg_pnl": float(st.entry.get("filled", 0.0)) * (w["outcome"] == st.initial_token) - float(st.entry.get("cost", 0.0)),
                **{f"s{k}_trig": rows[k]["ask"] for k in (1, 2, 3) if k in rows}})
    return out


# ───────────────────────────────── statistics ─────────────────────────────────


def _pct(xs, q):
    return float(np.percentile(xs, q)) if xs else None


def stats(recs: list[dict]) -> dict:
    """Every figure the brief asks for (item 16-17) over a set of markets (chronological)."""
    recs = sorted(recs, key=lambda r: r["open"])
    tr = [r for r in recs if r["traded"]]
    pnl = [r["pnl"] for r in tr]
    n, k = len(recs), len(tr)
    eq = peak = dd = 0.0
    dds, streak, worst_streak = [], 0, 0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        dds.append(eq - peak)
        streak = streak + 1 if x < -1e-9 else 0
        worst_streak = max(worst_streak, streak)
    g = lambda key: sum(float(r.get(key) or 0.0) for r in recs)      # noqa: E731
    by = {}
    for b in ("0", "1", "2", "3+"):
        xs = [r["pnl"] for r in tr if r["bucket"] == b]
        by[b] = {"markets": len(xs), "share": len(xs) / k if k else None, "pnl": sum(xs), "avg": statistics.mean(xs) if xs else None}
    days = defaultdict(float)
    for r in recs:
        days[int(r["open"] // DAY)] += float(r.get("pnl") or 0.0)
    cap = [r["capital"] for r in tr]
    out = {"markets": n, "traded": k, "total_pnl": sum(pnl), "pnl_per_market": sum(pnl) / n if n else 0.0,
           "pnl_per_traded": statistics.mean(pnl) if pnl else 0.0, "median_pnl": statistics.median(pnl) if pnl else 0.0,
           "std_pnl": statistics.pstdev(pnl) if k > 1 else 0.0,
           "t": (statistics.mean(pnl) / (statistics.pstdev(pnl) / math.sqrt(k))) if k > 1 and statistics.pstdev(pnl) > 0 else 0.0,
           "win_rate": sum(1 for x in pnl if x > 1e-9) / k if k else 0.0, "loss_rate": sum(1 for x in pnl if x < -1e-9) / k if k else 0.0,
           "max_drawdown": dd, "avg_drawdown": statistics.mean(dds) if dds else 0.0, "worst_market": min(pnl, default=0.0),
           "best_market": max(pnl, default=0.0), "p01_pnl": _pct(pnl, 1), "p05_pnl": _pct(pnl, 5), "max_losing_streak": worst_streak,
           "by_swing": by, "first_leg_alone_pnl": g("first_leg_pnl"),
           "entry_opportunity_markets": sum(1 for r in recs if r.get("entry_opps")), "entry_opportunities": g("entry_opps"),
           "missed_entries": sum(1 for r in recs if r.get("entry_opps") and not r["traded"]),
           "max_capital": max(cap, default=0.0), "avg_capital": statistics.mean(cap) if cap else 0.0, "p95_capital": _pct(cap, 95),
           "max_inventory": max((r["inventory"] for r in tr), default=0.0),
           "avg_inventory": statistics.mean(r["inventory"] for r in tr) if tr else 0.0,
           "capital_bound_markets": sum(int(r.get("cap_bound") or 0) for r in tr),
           "pnl_per_dollar": sum(pnl) / sum(cap) if cap and sum(cap) > 0 else 0.0, "fees": sum(r["fees"] for r in tr),
           "days": len(days), "losing_days": sum(1 for v in days.values() if v < -1e-9),
           "daily_std": statistics.pstdev(days.values()) if len(days) > 1 else 0.0}
    for c in ("entry", "s1", "s2", "hedge"):
        sent, fills = g(f"{c}_sent"), g(f"{c}_fills")
        out[f"{c}_orders"], out[f"{c}_fills"], out[f"{c}_partial"], out[f"{c}_kills"] = sent, fills, g(f"{c}_partial"), g(f"{c}_kills")
        out[f"{c}_fill_rate"] = fills / sent if sent else None
        out[f"avg_{c}_vwap"] = g(f"{c}_gross") / g(f"{c}_qty") if g(f"{c}_qty") else None
        out[f"{c}_qty"] = g(f"{c}_qty")
    for k2 in (1, 2, 3):
        xs = [r[f"s{k2}_trig"] for r in tr if r.get(f"s{k2}_trig") is not None]
        out[f"avg_s{k2}_trigger_ask"] = statistics.mean(xs) if xs else None
    out["policy_markets"] = {p: sum(int(r.get(f"policy_{p}") or 0) for r in tr)
                             for p in ("rec_cap", "rec_reject", "hedge_ratio", "hedge_max", "hedge_policy")}
    return out


def split_of(ws: list[dict]) -> dict:
    """slug -> period: the brief's dates if the data covers them, else a chronological 60 / 20 / 20 split."""
    day = lambda w: time.strftime("%Y-%m-%d", time.gmtime(w["open"] / 1000.0))      # noqa: E731
    by = {}
    for w in ws:
        d = day(w)
        p = next((name for name, a, b in PERIODS if a <= d <= b), None)
        by[w["slug"]] = p
    names = [p for p in by.values()]
    if all(p is not None for p in names) and all(names.count(nm) > 0 for nm, _, _ in PERIODS):
        return by
    srt = sorted(ws, key=lambda w: w["open"])
    n = len(srt)
    return {w["slug"]: ("train" if k < 0.6 * n else "validation" if k < 0.8 * n else "test") for k, w in enumerate(srt)}


def selection_score(per: dict) -> float:
    """The ranking score, from TRAIN and VALIDATION only (the test period never ranks a candidate):

        score = min(P&L per market in train, in validation)          (the worse period's EV per market offered)
                - 0.001 x |max drawdown, train + validation|        (100 USDC of drawdown costs 0.10)
                - 0.002 x |1st-percentile market, train + validation| (a -50 tail costs 0.10)
                - 0.0005 x 95th-percentile capital                   (125 USDC costs 0.06)
                - 0.20 x share of losing days, train + validation    (all days losing costs 0.20)
    """
    tr, va, tv = per["train"], per["validation"], per["trainval"]
    return (min(tr["pnl_per_market"], va["pnl_per_market"]) - 0.001 * abs(tv["max_drawdown"]) - 0.002 * abs(tv["p01_pnl"] or 0.0)
            - 0.0005 * (tv["p95_capital"] or 0.0) - 0.20 * (tv["losing_days"] / tv["days"] if tv["days"] else 0.0))


# ───────────────────────────────── running ─────────────────────────────────


_WS: list[dict] = []
_SPLIT: dict = {}


def _job(arg):
    c, stress, keep = arg
    recs = [run_window(w, c, stress) for w in _WS]
    per = {}
    for p in ("train", "validation", "test"):
        per[p] = stats([r for r in recs if _SPLIT[r["slug"]] == p])
    per["trainval"] = stats([r for r in recs if _SPLIT[r["slug"]] in ("train", "validation")])
    per["all"] = stats(recs)
    return c, stress, per, (recs if keep else None)


class Sweep:
    def __init__(self, workers: int):
        self.workers = workers
        self.done: dict[tuple, dict] = {}
        self.markets: dict[str, list] = {}
        self.stage_of: dict[str, list] = defaultdict(list)
        self.equiv: dict[str, set] = {}

    def run(self, cands, stage: str, stress: Stress = NO_STRESS, keep: bool = False) -> list[Cand]:
        todo, seen = [], set()
        for c in cands:
            if stress.name() == "none":
                self.stage_of[c.key()].append(stage)
            k = (c.key(), stress.name())
            if (k not in self.done or (keep and f"{c.key()}|stress={stress.name()}" not in self.markets)) and k not in seen:
                seen.add(k)
                todo.append((c, stress, keep))
        t0 = time.time()
        if todo:
            if self.workers > 1:
                with Pool(self.workers) as pool:
                    for c, s, per, recs in pool.imap_unordered(_job, todo):
                        self._store(c, s, per, recs)
            else:
                for a in todo:
                    self._store(*_job(a))
        print(f"[{stage}] {len(todo)} runs ({len(cands)} asked) in {time.time() - t0:.0f} s", flush=True)
        return list(dict.fromkeys(cands))

    def _store(self, c, s, per, recs):
        rec = {"cand": c, "stress": s.name(), "per": per}
        if s is NO_STRESS or s.name() == "none":
            rec["score"] = selection_score(per)
        self.done[(c.key(), s.name())] = rec
        if recs is not None:
            self.markets[f"{c.key()}|stress={s.name()}"] = recs

    def get(self, c: Cand, stress: str = "none") -> dict:
        return self.done[(c.key(), stress)]

    def fingerprint(self, c: Cand) -> tuple:
        """Candidates with this in common behave identically on the data (a policy that never bound, a cap that
        never bound, ...): they are one result, ranked once."""
        per = self.get(c)["per"]
        return tuple((round(per[p]["total_pnl"], 6), per[p]["traded"], round(per[p]["avg_capital"], 6), round(per[p]["fees"], 6))
                     for p in ("train", "validation", "test"))

    def best(self, cands, k: int) -> list[Cand]:
        """The top ``k`` by the selection score, one per behaviour (the variant closest to the baseline stands for its
        equivalents, listed in self.equiv)."""
        out, seen = [], {}
        for c in sorted(dict.fromkeys(cands), key=lambda c: (-self.get(c)["score"], _distance(c))):
            fp = self.fingerprint(c)
            if fp in seen:
                self.equiv.setdefault(seen[fp].key(), set()).add(c.label())
                continue
            seen[fp] = c
            if len(out) < k:
                out.append(c)
        return out


BASE = Cand()


def _distance(c: Cand) -> int:
    """How many parameters differ from the current strategy (ties in the ranking prefer the smaller change)."""
    a, b = asdict(c), asdict(BASE)
    return sum(1 for k in a if a[k] != b[k])


BASELINES = {"A current (10/24/80, hedge 100%, cap 125)": BASE, "B no hedge": replace(BASE, hedge_ratio=0.0),
             "C 10/24/40": replace(BASE, s2=40.0), "D 10/15/30": replace(BASE, s1=15.0, s2=30.0),
             "E 10/15/20": replace(BASE, s1=15.0, s2=20.0)}
GRID = {"entry_start": [15.0, 20.0, 25.0, 30.0, 35.0, 40.0], "entry_min": [0.50, 0.52, 0.55], "entry_max": [0.58, 0.60, 0.62, 0.65],
        "hysteresis": [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10],
        "trigger": [0.60, 0.62, 0.64, 0.66, 0.68, 0.70, 0.72, 0.74, 0.76, 0.78],
        "s1": [8.0, 10.0, 12.0, 15.0, 18.0, 20.0, 24.0, 30.0], "s2": [20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 100.0],
        "s2_mult": [1.5, 2.0, 2.5, 3.0, 3.5], "hedge_ratio": [0.0, 0.25, 0.5, 0.75, 1.0],
        "hedge_max": [0.0, 0.70, 0.72, 0.74, 0.76, 0.78, 0.80],
        "rec_max": [0.65, 0.67, 0.69, 0.70, 0.71, 0.72, 0.73, 0.74, 0.75, 0.77],
        "capital": [100.0, 110.0, 125.0, 140.0, 150.0, 175.0, 200.0]}


def stage_cands(name: str, base: Cand = BASE) -> list[Cand]:
    G = GRID
    if name == "baselines":
        return list(BASELINES.values())
    if name == "entry":
        return [replace(base, entry_start=s, entry_end=s + 10.0, entry_min=a, entry_max=b)
                for s in G["entry_start"] for a in G["entry_min"] for b in G["entry_max"] if a < b]
    if name == "trigger":
        return [replace(base, trigger=t, hysteresis=h) for t in G["trigger"] for h in G["hysteresis"]]
    if name == "sizes":
        return ([replace(base, s1=a, s2=b) for a in G["s1"] for b in G["s2"]]
                + [replace(base, s1=a, s2_mult=k) for a in G["s1"] for k in G["s2_mult"]])
    if name == "sizes_nohedge":
        return [replace(base, s1=a, s2=b, hedge_ratio=0.0) for a in G["s1"] for b in G["s2"]]
    if name == "hedge":
        out = []
        for r in G["hedge_ratio"]:
            for hm in G["hedge_max"]:
                for mode in (("once", "each") if 0.0 < r < 1.0 else ("once",)):
                    if r == 0.0 and hm:
                        continue
                    out.append(replace(base, hedge_ratio=r, hedge_max=hm, hedge_mode=mode))
        return out
    if name == "recovery_price":
        out = [replace(base, rec_max=x, rec_policy=p) for x in G["rec_max"] for p in ("reduce", "reject")]
        out += [replace(base, rec_max=x, rec_dynamic=True, rec_policy=p) for x in (0.0, 0.68, 0.70, 0.72) for p in ("reduce", "reject")]
        return out
    if name == "capital":
        return [replace(base, capital=x) for x in G["capital"]]
    raise ValueError(name)


def neighbours(c: Cand) -> list[Cand]:
    """One grid step up / down in every swept dimension (the stability analysis)."""
    out = []

    def step(field, values, cur):
        if cur not in values:
            return
        k = values.index(cur)
        for j in (k - 1, k + 1):
            if 0 <= j < len(values):
                yield values[j]

    for f in ("entry_min", "entry_max", "hysteresis", "trigger", "s1", "hedge_ratio", "capital"):
        for v in step(f, GRID[f], getattr(c, f)):
            n = replace(c, **{f: v})
            if n.entry_min < n.entry_max:
                out.append(n)
    for v in step("entry_start", GRID["entry_start"], c.entry_start):
        out.append(replace(c, entry_start=v, entry_end=v + (c.entry_end - c.entry_start)))
    if c.s2_mult > 0:
        out += [replace(c, s2_mult=v) for v in step("s2_mult", GRID["s2_mult"], c.s2_mult)]
    else:
        out += [replace(c, s2=v) for v in step("s2", GRID["s2"], c.s2)]
    if c.hedge_ratio > 0:
        out += [replace(c, hedge_max=v) for v in step("hedge_max", GRID["hedge_max"], c.hedge_max)]
    if c.rec_max:
        out += [replace(c, rec_max=v) for v in step("rec_max", GRID["rec_max"], c.rec_max)]
    return list(dict.fromkeys(out))


def _distinct(values, k):
    out = []
    for v in values:
        if v not in out:
            out.append(v)
        if len(out) == k:
            break
    return out


def main():
    global _WS, _SPLIT
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start-date", default="2026-09-22")
    ap.add_argument("--end-date", default="2026-10-07")
    ap.add_argument("--output", default=f"{ROOT}/results/pairlock_swing_sweep.json")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--markets-out", default=None)
    ap.add_argument("--max-markets", type=int, default=0, help="an evenly spaced subset (chronology kept), for development")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--stages", default="all", help="all | comma list of baselines,entry,trigger,sizes,sizes_nohedge,hedge,"
                                                     "recovery_price,capital,combined,neighbourhood,stress")
    ap.add_argument("--cache", default=os.environ.get("SWING_CACHE", "/tmp/claude-0/-root-troll-poly-bot/53e010f8-a35d-42fb-b329-fe76e0f4a11d/scratchpad/swing"))
    ap.add_argument("--refresh", action="store_true", help="rebuild the data cache from data/charts (else the cached snapshot)")
    a = ap.parse_args()
    cache = os.path.join(a.cache, "swing_windows.pkl")
    if a.refresh or not os.path.exists(cache):
        ws = RS.load(a.cache)
    else:
        import pickle
        with open(cache, "rb") as f:
            ws = pickle.load(f)                                       # a frozen snapshot: the sweep is reproducible
    day = lambda w: time.strftime("%Y-%m-%d", time.gmtime(w["open"] / 1000.0))      # noqa: E731
    ws = [w for w in ws if a.start_date <= day(w) <= a.end_date]
    if a.max_markets and len(ws) > a.max_markets:
        stp = len(ws) / a.max_markets
        ws = [ws[int(k * stp)] for k in range(a.max_markets)]
    _WS, _SPLIT = ws, split_of(ws)
    stages = ("baselines,entry,trigger,sizes,sizes_nohedge,hedge,recovery_price,capital,combined,neighbourhood,stress"
              if a.stages == "all" else a.stages).split(",")
    print(f"{len(ws)} markets {day(ws[0])} .. {day(ws[-1])}; periods "
          + ", ".join(f"{p} {sum(1 for v in _SPLIT.values() if v == p)}" for p in ("train", "validation", "test")), flush=True)
    S = Sweep(a.workers)
    t_start = time.time()
    S.run(list(BASELINES.values()), "baselines", keep=True)
    picks = {}
    one_dim = [s for s in ("entry", "trigger", "sizes", "sizes_nohedge", "hedge", "recovery_price", "capital") if s in stages]
    for st in one_dim:
        cs = S.run(stage_cands(st), st)
        picks[st] = S.best(cs, 4)
    if "combined" in stages and picks:
        # the factorial of each dimension's best values (chosen on train + validation)
        E = _distinct([(c.entry_start, c.entry_end, c.entry_min, c.entry_max) for c in picks.get("entry", [BASE])], 2)
        T = _distinct([(c.trigger, c.hysteresis) for c in picks.get("trigger", [BASE])], 3)
        Z = _distinct([(c.s1, c.s2, c.s2_mult) for c in picks.get("sizes", [BASE]) + picks.get("sizes_nohedge", [])], 3)
        H = _distinct([(c.hedge_ratio, c.hedge_max, c.hedge_mode) for c in picks.get("hedge", [BASE])], 3)
        R = _distinct([(c.rec_max, c.rec_policy, c.rec_dynamic) for c in picks.get("recovery_price", [BASE])] + [(0.0, "reduce", False)], 3)
        K = _distinct([c.capital for c in picks.get("capital", [BASE])] + [125.0], 2)
        combo = [Cand(entry_start=e[0], entry_end=e[1], entry_min=e[2], entry_max=e[3], trigger=t[0], hysteresis=t[1], s1=z[0],
                      s2=z[1], s2_mult=z[2], hedge_ratio=h[0], hedge_max=h[1], hedge_mode=h[2], rec_max=r[0], rec_policy=r[1],
                      rec_dynamic=r[2], capital=k) for e, t, z, h, r, k in itertools.product(E, T, Z, H, R, K)]
        S.run(combo, "combined")
    pool = [S.done[k]["cand"] for k in S.done if S.done[k]["stress"] == "none"]
    if "neighbourhood" in stages:
        # local search around the leaders (train + validation score only): expand every top-10 candidate's neighbours,
        # re-rank, and repeat while new candidates enter the top 10 (at most 4 rounds)
        expanded: set = set()
        for rnd in range(4):
            lead = [c for c in S.best(pool, 10) if c.key() not in expanded]
            if not lead:
                break
            expanded |= {c.key() for c in lead}
            S.run([n for c in lead for n in neighbours(c)], f"neighbourhood round {rnd + 1}")
            pool = [S.done[k]["cand"] for k in S.done if S.done[k]["stress"] == "none"]
    top = S.best(pool, 20)
    neigh = {}
    for c in top:
        ns = [n for n in neighbours(c) if (n.key(), "none") in S.done]
        sc = [S.get(n)["score"] for n in ns]
        ev = [min(S.get(n)["per"]["train"]["pnl_per_market"], S.get(n)["per"]["validation"]["pnl_per_market"]) for n in ns]
        neigh[c.key()] = {"neighbours": len(ns), "median_score": statistics.median(sc) if sc else None,
                          "positive_share": sum(1 for x in ev if x > 0) / len(ev) if ev else None,
                          "min_score": min(sc) if sc else None}
    if "stress" in stages:
        for s in STRESSES:
            S.run(top, f"stress {s.name()}", stress=s)
    keep = list(dict.fromkeys(top + list(BASELINES.values())))
    S.run(keep, "per-market rows", keep=True)                       # (already computed: re-run only to keep the rows)
    write(a, S, top, neigh, picks, ws, time.time() - t_start)


def _row(S: Sweep, c: Cand, stress: str = "none") -> dict:
    d = S.get(c, stress)
    al, tr, va, te = (d["per"][p] for p in ("all", "train", "validation", "test"))
    b = al["by_swing"]
    return {"label": c.label(), "entry_start": c.entry_start, "entry_end": c.entry_end, "entry_min": c.entry_min,
            "entry_max": c.entry_max, "hysteresis": c.hysteresis, "swing_trigger": c.trigger, "swing1_size": c.s1,
            "swing2_size": c.swing2, "swing2_mult": c.s2_mult or "", "hedge_ratio": c.hedge_ratio, "hedge_mode": c.hedge_mode,
            "hedge_max_price": c.hedge_max or "", "recovery_max_price": c.rec_max or "", "recovery_policy": c.rec_policy,
            "recovery_dynamic_be": int(c.rec_dynamic), "capital_cap": c.capital, "stress": stress,
            "stages": ";".join(S.stage_of.get(c.key(), [])),
            "markets": al["markets"], "traded_markets": al["traded"],
            "swing0_markets": b["0"]["markets"], "swing1_markets": b["1"]["markets"], "swing2_markets": b["2"]["markets"],
            "swing3plus_markets": b["3+"]["markets"], "total_pnl": round(al["total_pnl"], 2),
            "pnl_per_market": round(al["pnl_per_market"], 4), "pnl_per_traded": round(al["pnl_per_traded"], 4),
            "median_pnl": round(al["median_pnl"], 3), "std_pnl": round(al["std_pnl"], 3), "win_rate": round(al["win_rate"], 4),
            "max_drawdown": round(al["max_drawdown"], 2), "avg_drawdown": round(al["avg_drawdown"], 2),
            "worst_market": round(al["worst_market"], 2), "p01_pnl": al["p01_pnl"] and round(al["p01_pnl"], 2),
            "p05_pnl": al["p05_pnl"] and round(al["p05_pnl"], 2), "max_losing_streak": al["max_losing_streak"],
            "avg_capital": round(al["avg_capital"], 2), "p95_capital": al["p95_capital"] and round(al["p95_capital"], 2),
            "max_capital": round(al["max_capital"], 2), "pnl_per_dollar": round(al["pnl_per_dollar"], 4),
            "capital_bound_markets": al["capital_bound_markets"],
            **{f"swing{k.replace('+', 'plus')}_pnl": round(b[k]["pnl"], 2) for k in b},
            **{f"swing{k.replace('+', 'plus')}_avg": b[k]["avg"] and round(b[k]["avg"], 3) for k in b},
            "entry_fill_rate": al["entry_fill_rate"] and round(al["entry_fill_rate"], 3),
            "swing1_fill_rate": al["s1_fill_rate"] and round(al["s1_fill_rate"], 3),
            "swing2_fill_rate": al["s2_fill_rate"] and round(al["s2_fill_rate"], 3),
            "hedge_fill_rate": al["hedge_fill_rate"] and round(al["hedge_fill_rate"], 3),
            "avg_entry_vwap": al["avg_entry_vwap"] and round(al["avg_entry_vwap"], 4),
            "avg_swing1_vwap": al["avg_s1_vwap"] and round(al["avg_s1_vwap"], 4),
            "avg_swing2_vwap": al["avg_s2_vwap"] and round(al["avg_s2_vwap"], 4),
            "avg_hedge_vwap": al["avg_hedge_vwap"] and round(al["avg_hedge_vwap"], 4),
            "avg_swing1_trigger_ask": al["avg_s1_trigger_ask"] and round(al["avg_s1_trigger_ask"], 4),
            "avg_swing2_trigger_ask": al["avg_s2_trigger_ask"] and round(al["avg_s2_trigger_ask"], 4),
            "training_pnl": round(tr["total_pnl"], 2), "validation_pnl": round(va["total_pnl"], 2), "test_pnl": round(te["total_pnl"], 2),
            "training_pnl_per_market": round(tr["pnl_per_market"], 4), "validation_pnl_per_market": round(va["pnl_per_market"], 4),
            "test_pnl_per_market": round(te["pnl_per_market"], 4),
            "robustness_score": round(d["score"], 4) if "score" in d else ""}


def write(a, S: Sweep, top, neigh, picks, ws, secs):
    rows = [_row(S, d["cand"], d["stress"]) for d in S.done.values()]
    rows.sort(key=lambda r: (r["stress"] != "none", -(r["robustness_score"] if r["robustness_score"] != "" else -1e9)))
    csv_path = a.csv or a.output.replace(".json", ".csv")
    with open(csv_path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    doc = {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "seconds": round(secs),
           "markets": len(ws), "last_market": ws[-1]["slug"], "first": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ws[0]["open"] / 1000)),
           "last": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ws[-1]["open"] / 1000)),
           "periods": {p: sum(1 for v in _SPLIT.values() if v == p) for p in ("train", "validation", "test")},
           "period_dates": PERIODS, "selection_score": selection_score.__doc__, "grid": GRID,
           "execution": {"model": "replay_swing.execute('realistic')", "latency_ms": RS.LAT_MS, "survival_ms": RS.SURVIVAL_MS,
                         "touch_contention": RS.CONTENTION},
           "baselines": {name: {"cand": asdict(c), "per": S.get(c)["per"], "score": S.get(c)["score"]} for name, c in BASELINES.items()},
           "top20": [{"rank": k + 1, "cand": asdict(c), "label": c.label(), "score": S.get(c)["score"], "per": S.get(c)["per"],
                      "neighbourhood": neigh.get(c.key()), "equivalent": sorted(S.equiv.get(c.key(), [])),
                      "stress": {s.name(): S.done[(c.key(), s.name())]["per"]["all"] for s in STRESSES if (c.key(), s.name()) in S.done}}
                     for k, c in enumerate(top)],
           "stage_picks": {k: [c.label() for c in v] for k, v in picks.items()},
           "candidates": [{"cand": asdict(d["cand"]), "stress": d["stress"], "score": d.get("score"),
                           "per": {p: {k: v for k, v in d["per"][p].items()} for p in ("train", "validation", "test", "all")}}
                          for d in S.done.values()]}
    with open(a.output, "w") as f:
        json.dump(doc, f, default=str)
    mpath = a.markets_out or a.output.replace(".json", "_markets.json.gz")
    with gzip.open(mpath, "wt") as f:
        json.dump(S.markets, f, default=str)
    print(f"-> {a.output}\n-> {csv_path}\n-> {mpath}", flush=True)
    for k, c in enumerate(top[:20]):
        r = _row(S, c)
        print(f"{k + 1:2d} {r['robustness_score']:+.3f}  tr {r['training_pnl_per_market']:+.3f} va {r['validation_pnl_per_market']:+.3f} "
              f"te {r['test_pnl_per_market']:+.3f}  dd {r['max_drawdown']:+.0f}  {c.label()}", flush=True)
    for name, c in BASELINES.items():
        r = _row(S, c)
        print(f"   {name:42s} all {r['pnl_per_market']:+.3f}/mkt  tr {r['training_pnl_per_market']:+.3f} va "
              f"{r['validation_pnl_per_market']:+.3f} te {r['test_pnl_per_market']:+.3f}  score {r['robustness_score']:+.3f}", flush=True)


if __name__ == "__main__":
    main()
