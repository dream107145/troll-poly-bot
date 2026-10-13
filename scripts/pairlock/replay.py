"""[v1, 2026-10-01 -- HISTORICAL. Its strategy functions call the v1 pairlock API (PairPosition, State.MONITOR ...),
replaced on 2026-10-02 by strategy/pairlock_book.py + the v2 pairlock.py; the v1 module is archived as
data/replay/pairlock.py.before_v2. scripts/pairlock/replay2.py reuses only this file's data loading, features cache,
triggers and logistic fit.]

Replay of the conditional dynamic hedge / pair-lock strategy on data/charts (BTC 5-minute windows, ~213 ms
samples of the composite spot and both books), with the paper venue's `realistic` execution model:

  decision -> arrival after LAT_MS (measured: pre-send 31 ms + half the 224 ms round trip) -> FOK matched against
  the book at the first sample at or after arrival, 80 % of the touch taken by faster traders (contention) ->
  the fill stands only if the matched level is still on the book 2 s later (quote survival) -> the result is
  known one ack later. Taker fee 7 % x p(1-p). Settlement at the window's venue outcome.

The decisions are made by src/troll_poly_bot/strategy/pairlock.py -- the same functions the live engine calls.
Chronological splits by window: TRAIN 50 % / VALIDATION 20 % / TEST 15 % / FORWARD 15 %. The probability model,
its scales and the execution constants are fitted on TRAIN only; parameters are chosen on VALIDATION only.

Entry trigger: the live bot's trigger is the millisecond flow signal, which the 213 ms charts cannot reproduce;
the replay uses the 1 s spot shock (|1 s return| >= trig_k sigma with the 3 s return agreeing) as its proxy.

usage:  replay.py prepare          (features cache + model fit + execution calibration)
        replay.py run <name> [json overrides]   one configuration on every split
        replay.py grid                           the validation grid
        replay.py report                         everything the audit document needs
"""
from __future__ import annotations

import bisect
import glob
import json
import math
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, replace

import numpy as np

ROOT = "/root/troll-poly-bot"
sys.path.insert(0, f"{ROOT}/src")
from troll_poly_bot.strategy import pairlock as pl   # noqa: E402

OUT = os.environ.get("PAIRLOCK_OUT", "/tmp/claude-0/-root-troll-poly-bot/0b5739ea-ef03-4944-92af-cebb397ef559/scratchpad/pairlock")
os.makedirs(OUT, exist_ok=True)
MODEL_PATH = os.environ.get("PAIRLOCK_MODEL", f"{ROOT}/data/replay/pairlock_model.json")
LAT_MS = 143.0          # measured: pre-send p50 31 ms + order round trip p50 224 ms / 2
ACK_MS = 112.0
CONTENTION = 0.80       # the measured latency profile's contention (HOME_BROADBAND); both scenarios leave it on
#: the paper venue's fill model (execution/scenarios.py). "calibrated" (the live bot's since 19:49 UTC, fitted
#: on 193 real orders): the later book 1 s after arrival must still fill the size at or under the LIMIT, charged
#: at the worse of the two books. "realistic": every matched level must still be there 2 s later (too strict
#: against real orders: it voids fills whenever the ask ticks up inside the limit). Kept as a sensitivity.
SCENARIO = os.environ.get("PAIRLOCK_SCENARIO", "calibrated")
SURVIVAL_MS = {"calibrated": 1000.0, "realistic": 2000.0}[SCENARIO]
SPLITS = (("train", 0.0, 0.5), ("validation", 0.5, 0.7), ("test", 0.7, 0.85), ("forward", 0.85, 1.0))
FN = pl.FEATURES + ("mid_up", "left")


# ───────────────────────────────── data ─────────────────────────────────


class Win:
    """One window as compact arrays (NaN = missing). The features are computed ONCE, by the live function
    pairlock.features() on a pairlock.History, then kept as an array."""
    __slots__ = ("slug", "open", "close", "strike", "outcome", "t", "ub", "ua", "db", "da", "ubd", "uad", "i0", "i1",
                 "sigma", "F", "ok", "trig", "day")

    def __init__(self, d, prev_pts):
        self.slug, self.open, self.close = d["slug"], d["open_ts"], d["close_ts"]
        self.strike, self.outcome = float(d["strike"]), d["outcome"].upper()
        pts = d["points"]
        # sigma from the 300 s before the open (the previous window when contiguous + this one's pre-open points)
        hs = pl.History.from_points((prev_pts or []) + pts)
        self.sigma = pl.realized_sigma(hs, self.open, 300.0)
        h = pl.History.from_points(pts)
        self.i0 = bisect.bisect_left(h.t, self.open)
        self.i1 = bisect.bisect_left(h.t, self.close)
        self.ok = self.sigma is not None and self.i1 - self.i0 > 600
        self.F = None
        self.trig = None
        self.day = time.strftime("%Y-%m-%d", time.gmtime(self.open / 1000.0))
        if self.ok:
            rows = np.full((self.i1 - self.i0, len(FN)), np.nan)
            for k, i in enumerate(range(self.i0, self.i1)):
                f = pl.features(h, i, self.strike, self.sigma, self.close)
                if f is not None:
                    rows[k] = [f[n] for n in FN]
            self.F = rows
        nan = lambda xs: np.array([np.nan if x is None else x for x in xs], dtype=np.float64)   # noqa: E731
        self.t = np.array(h.t, dtype=np.float64)
        self.ub, self.ua, self.db, self.da = nan(h.ub), nan(h.ua), nan(h.db), nan(h.da)
        self.ubd, self.uad = nan(h.ubd), nan(h.uad)


def load_windows():
    fs = sorted(glob.glob(f"{ROOT}/data/charts/btc-updown-5m-*.json"))
    out, prev = [], None
    for f in fs:
        d = json.load(open(f))
        if not d.get("points") or (d.get("outcome") or "").upper() not in ("UP", "DOWN") or not d.get("strike"):
            prev = None
            continue
        prev_pts = prev["points"] if prev is not None and abs(prev["close_ts"] - d["open_ts"]) < 1.0 else None
        w = Win(d, prev_pts)
        prev = d
        if w.ok:
            out.append(w)
    return out


def feat_at(w: Win, i: int):
    r = w.F[i - w.i0]
    if np.isnan(r[0]):
        return None
    return dict(zip(FN, r.tolist()))


def triggers(w: Win, k: float):
    """The replay's entry trigger (proxy for the flow signal): 1 s spot move >= k sigma, 3 s agreeing,
    at most one per direction per second."""
    out = np.zeros(w.i1 - w.i0, dtype=np.int8)
    last = {1: -1e18, -1: -1e18}
    m1, m3 = FN.index("mom1"), FN.index("mom3")
    for k_, i in enumerate(range(w.i0, w.i1)):
        r = w.F[k_]
        if np.isnan(r[0]):
            continue
        d = 1 if r[m1] >= k and r[m3] > 0 else -1 if r[m1] <= -k and r[m3] < 0 else 0
        if d and w.t[i] - last[d] >= 1000.0:
            last[d] = w.t[i]
            out[k_] = d
    return out


def prepare_cache():
    t0 = time.time()
    ws = load_windows()
    with open(f"{OUT}/windows.pkl", "wb") as f:
        pickle.dump(ws, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"{len(ws)} windows with features cached in {time.time() - t0:.0f} s")
    return ws


class _Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if name == "Win":
            return Win                       # cached from `replay.py prepare` (module __main__)
        return super().find_class(module, name)


def load_cache():
    with open(f"{OUT}/windows.pkl", "rb") as f:
        return _Unpickler(f).load()


def split(ws):
    n = len(ws)
    return {name: ws[int(lo * n):int(hi * n)] for name, lo, hi in SPLITS}


# ───────────────────────────────── the probability model ─────────────────────────────────


def fit_logistic(X, y, l2=1e-3, iters=50):
    Xb = np.hstack([np.ones((len(X), 1)), X])
    b = np.zeros(Xb.shape[1])
    for _ in range(iters):
        z = np.clip(Xb @ b, -30, 30)
        p = 1 / (1 + np.exp(-z))
        g = Xb.T @ (p - y) + l2 * np.r_[0, b[1:]]
        W = p * (1 - p)
        H = (Xb * W[:, None]).T @ Xb + l2 * np.diag(np.r_[0, np.ones(len(b) - 1)])
        step = np.linalg.solve(H, g)
        b -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return b


def samples(ws, every_ms=2000.0, lo=10.0, hi=295.0):
    X, y, mids, wid = [], [], [], []
    for n, w in enumerate(ws):
        last = -1e18
        for k, i in enumerate(range(w.i0, w.i1)):
            r = w.F[k]
            if np.isnan(r[0]) or not (lo <= r[-1] <= hi) or w.t[i] - last < every_ms:
                continue
            last = w.t[i]
            X.append(r[:len(pl.FEATURES)])
            y.append(1.0 if w.outcome == "UP" else 0.0)
            mids.append(r[FN.index("mid_up")])
            wid.append(n)
    return np.array(X), np.array(y), np.clip(np.array(mids), 0.01, 0.99), np.array(wid)


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def fit_model(sp):
    X, y, mids, _ = samples(sp["train"])
    b = fit_logistic(X, y)
    # reversal score scales: the train SD of each component
    scales = {k: float(np.std(X[:, pl.FEATURES.index(k)])) or 1.0 for k in pl.REVERSAL}
    model = pl.ProbModel(beta=b.tolist(), rev_scales=scales,
                         meta={"fitted_on": [sp["train"][0].slug, sp["train"][-1].slug], "n_samples": int(len(y)),
                               "n_windows": len(sp["train"]), "kind": "logistic"})
    rep = {"beta": dict(zip(("intercept",) + pl.FEATURES, [round(x, 4) for x in b]))}
    for name, ws in sp.items():
        Xs, ys, ms, _ = samples(ws)
        pm = 1 / (1 + np.exp(-np.clip(np.hstack([np.ones((len(Xs), 1)), Xs]) @ b, -30, 30)))
        rep[name] = {"n": int(len(ys)), "logloss_model": logloss(pm, ys), "logloss_market": logloss(ms, ys),
                     "brier_model": float(np.mean((pm - ys) ** 2)), "brier_market": float(np.mean((ms - ys) ** 2))}
    return model, rep


# ───────────────────────────────── execution (paper venue, realistic) ─────────────────────────────────


def _f(x):
    x = float(x)
    return None if x != x else x


def book(w: Win, side: str, i: int):
    """(ask, bid, ask depth near the touch). The books are mirrored: DOWN ask depth = UP bid depth."""
    if side == "UP":
        return _f(w.ua[i]), _f(w.ub[i]), float(np.nan_to_num(w.uad[i]))
    return _f(w.da[i]), _f(w.db[i]), float(np.nan_to_num(w.ubd[i]))


def fok(w: Win, i_dec: int, side: str, q: float, limit: float):
    """(status, fill price, time the result is known). Mirrors PaperExchange with scenarios.REALISTIC."""
    ts = w.t
    t_arr = float(ts[i_dec]) + LAT_MS
    j = int(np.searchsorted(ts, t_arr, side="left"))
    if j >= len(ts) or ts[j] >= w.close:
        return "market_closed", None, t_arr + ACK_MS
    ask, _, depth = book(w, side, j)
    if ask is None or ask > limit + 1e-9 or depth * (1.0 - CONTENTION) + 1e-9 < q:
        return "fok_unfillable", None, float(ts[j]) + ACK_MS
    k = int(np.searchsorted(ts, t_arr + SURVIVAL_MS, side="left"))
    if k >= len(ts) or ts[k] >= w.close:
        return "quote_pulled", None, t_arr + SURVIVAL_MS + ACK_MS     # the venue's book is gone at the close
    a2, _, d2 = book(w, side, k)
    if SCENARIO == "calibrated":
        # the later book must fill the size at or under the LIMIT; charged at the worse of the two books
        if a2 is None or a2 > limit + 1e-9 or d2 + 1e-9 < q:
            return "quote_pulled", None, t_arr + SURVIVAL_MS + ACK_MS
        return "filled", max(ask, a2), t_arr + SURVIVAL_MS + ACK_MS
    if a2 is None or a2 > ask + 1e-9 or d2 + 1e-9 < q:
        return "quote_pulled", None, t_arr + SURVIVAL_MS + ACK_MS
    return "filled", ask, t_arr + SURVIVAL_MS + ACK_MS


# ───────────────────────────────── strategies ─────────────────────────────────


class Pending:
    __slots__ = ("kind", "cyc", "side", "q", "limit", "ask_dec", "status", "px", "t_result", "t_dec", "hkind", "left", "info")


def run_window(w: Win, strat: str, prm: pl.PairLockParams, lim: pl.RiskLimits, model: pl.ProbModel,
               trig: np.ndarray, allow_entries: bool = True, base_cfg: dict | None = None):
    """One window, one strategy. Returns the market record."""
    tl = w.t.tolist()
    bc = {"pair": 0.96, "strict": False, "offset": 0.03, "shares": 5.0, "cap": 0.96, **(base_cfg or {})}
    cycles: list[pl.Cycle] = []
    rec = defaultdict(float)
    ev = {"entries": 0, "entry_fills": 0, "entry_kills": 0, "hedges": 0, "hedge_fills": 0, "hedge_kills": 0,
          "hedge_log": [], "slip": 0.0, "resting": [], "entry_log": []}
    pending: Pending | None = None
    base_rest = []                                     # baseline: (cycle, price, t_from)
    no_chase = strat == "BASE"

    def market_exposure():
        cap = sum(c.pos.cost for c in cycles) + (pending.q * (pending.limit + pl.fee_per_share(pending.limit))
                                                if pending is not None else 0.0)
        sh = sum(c.pos.qa + c.pos.qb for c in cycles)
        worst = sum(c.pos.worst_case for c in cycles if c.pos.legs)
        return pl.MarketExposure(capital=cap, shares=sh, worst_case=worst, cycles=len(cycles))

    def resolve(p):
        if p.kind == "entry":
            if p.status == "filled":
                leg = pl.Leg(p.side, p.q, p.px, p.q * pl.fee_per_share(p.px), p.t_result, "entry")
                pl.on_entry_result(p.cyc, leg)
                ev["entry_fills"] += 1
                ev["slip"] += (p.px - p.ask_dec) * p.q
                ev["entry_log"].append({"side": p.side, "ask": p.ask_dec, "px": p.px, "left": p.left, "q": p.q,
                                        "t_dec": p.t_dec, "t_known": p.t_result, "p": p.info.get("p"),
                                        "win": 1.0 if w.outcome == p.side else 0.0})
                if strat == "BASE" and bc["pair"] > 0.0:
                    # the resting pair: the highest tick with fill + pair <= target (< target when strict)
                    y = math.floor((bc["pair"] - p.px) * 100 + (-1e-6 if bc["strict"] else 1e-6)) / 100
                    if y >= 0.01:
                        base_rest.append([p.cyc, y, p.t_result, False])
            else:
                pl.on_entry_result(p.cyc, None)
                cycles.remove(p.cyc)
                ev["entry_kills"] += 1
        else:
            if p.status == "filled":
                leg = pl.Leg(p.side, p.q, p.px, p.q * pl.fee_per_share(p.px), p.t_result, "hedge")
                pl.on_hedge_result(p.cyc, leg, prm, p.hkind, p.t_result)
                ev["hedge_fills"] += 1
                ev["slip"] += (p.px - p.ask_dec) * p.q
                ev["hedge_log"].append({"kind": p.hkind, "left": p.left, "q": p.q, "px": p.px, "ask": p.ask_dec,
                                        "pair_cost": p.cyc.pos.pair_cost, "info": p.info})
            else:
                pl.on_hedge_result(p.cyc, None, prm, p.hkind, p.t_result)
                ev["hedge_kills"] += 1
                ev["hedge_log"].append({"kind": p.hkind, "left": p.left, "q": p.q, "px": None, "ask": p.ask_dec,
                                        "status": p.status, "info": p.info})

    for i in range(w.i0, w.i1):
        t = tl[i]
        if pending is not None and t >= pending.t_result:
            p = pending
            pending = None
            resolve(p)
            p = None
        # the baseline's resting pairs fill when the other side's ask trades strictly below them (maker, no fee)
        for r in base_rest:
            if r[3] or t <= r[2]:
                continue
            cyc, y = r[0], r[1]
            a, _, _ = book(w, cyc.pos.b_side, i)
            if a is not None and a < y - 1e-9:
                cyc.pos.legs.append(pl.Leg(cyc.pos.b_side, cyc.pos.qa, y, 0.0, t, "hedge"))
                cyc.kinds.append("resting")
                r[3] = True
        if pending is not None:
            continue
        cur = cycles[-1] if cycles else None
        hedging = strat in ("B", "C") and cur is not None and cur.state in (pl.State.MONITOR, pl.State.ADVERSE_MOVE,
                                                                              pl.State.PARTIAL_HEDGE)
        if not hedging and (not allow_entries or trig[i - w.i0] == 0):
            continue
        f = feat_at(w, i)
        if f is None:
            continue
        left = (w.close - t) / 1000.0
        if hedging:
            a_side, b_side = cur.pos.a_side, cur.pos.b_side
            _, bid_a, _ = book(w, a_side, i)
            ask_b, bid_b, depth_b = book(w, b_side, i)
            dec, _why = pl.hedge_decision(cur, f, bid_a, ask_b, bid_b, depth_b * (1.0 - CONTENTION), t, left, model,
                                          prm, lim, market_exposure())
            if dec is not None:
                pl.on_hedge_sent(cur, t)
                st, px, tr = fok(w, i, b_side, dec.shares, dec.limit)
                p = Pending()
                p.kind, p.cyc, p.side, p.q, p.limit, p.ask_dec = "hedge", cur, b_side, dec.shares, dec.limit, ask_b
                p.status, p.px, p.t_result, p.t_dec, p.hkind, p.left = st, px, tr, t, dec.kind, left
                p.info = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in dec.info.items()}
                pending = p
                ev["hedges"] += 1
            continue
        if not allow_entries:
            continue
        d = int(trig[i - w.i0])
        if d == 0:
            continue
        side = "UP" if d > 0 else "DOWN"
        if no_chase and d * f["tv1"] > 1.0:
            continue                                   # the live no-chase gate (token rose > 1c in 1 s)
        ask, bid, depth = book(w, side, i)
        if strat == "BASE":
            # the current bot: every triangle, stacked; the market cap is the only limit
            if ask is None or not (prm.band_lo - 1e-9 <= ask <= prm.band_hi + 1e-9) or left < 60.0:
                continue
            q = bc["shares"]
            limit = min(bc["cap"], round(ask + bc["offset"], 2))
            if market_exposure().capital + q * (limit + pl.fee_per_share(limit)) > 50.0:
                continue
            cyc = pl.Cycle(pos=pl.PairPosition(a_side=side), started_t=t)
        else:
            prev = cur
            if cur is not None and cur.state not in pl.FLAT_STATES:
                continue
            dec, _why = pl.entry_decision(f, side, ask, bid, depth * (1.0 - CONTENTION), model, prm, lim,
                                          market_exposure(), left, prev)
            if dec is None:
                continue
            q, limit = dec.shares, dec.limit
            cyc = pl.Cycle(pos=pl.PairPosition(a_side=side), started_t=t)
        cycles.append(cyc)
        st, px, tr = fok(w, i, side, q, limit)
        p = Pending()
        p.kind, p.cyc, p.side, p.q, p.limit, p.ask_dec = "entry", cyc, side, q, limit, ask
        p.status, p.px, p.t_result, p.t_dec, p.hkind, p.left = st, px, tr, t, "", left
        p.info = {"p": model.p_side(f, side)}
        pending = p
        ev["entries"] += 1
    if pending is not None:
        if pending.status == "filled":
            resolve(pending)          # matched before the close; its result lands after the last sample
        elif pending.kind == "entry" and pending.cyc in cycles and not pending.cyc.pos.legs:
            cycles.remove(pending.cyc)
    # settlement
    out = {"slug": w.slug, "day": w.day, "outcome": w.outcome, "cycles": len(cycles), **{k: v for k, v in ev.items() if k != "slip"},
           "slippage": ev["slip"]}
    tot = defaultdict(float)
    pcs, naked, locks, full, partial = [], 0, 0, 0, 0
    for c in cycles:
        if not c.pos.legs:
            continue
        a = c.pos.attribution(w.outcome)
        for k, v in a.items():
            tot[k] += v
        tot["fees"] += c.pos.fees
        tot["capital"] += c.pos.cost
        if c.pos.pair_cost is not None:
            pcs.append(c.pos.pair_cost)
        if c.pos.unmatched >= prm.min_size - 1e-9 or (strat == "BASE" and c.pos.qb == 0):
            naked += 1
        if c.state is pl.State.PAIR_LOCKED or (strat == "BASE" and c.pos.qb > 0):
            locks += 1
        elif c.state is pl.State.FULL_HEDGE:
            full += 1
        elif c.pos.qb > 0:
            partial += 1
        if c.pos.qb > 0 and c.pos.unmatched > 1e-9:
            tot["incomplete_hedge_pnl"] += a["directional"]
    tot["max_exposure"] = tot["capital"]
    out.update({k: round(v, 5) for k, v in tot.items()})
    out.update({"pair_costs": pcs, "naked": naked, "locks": locks, "full_hedges": full, "partial": partial,
                "hedge_ratio": [c.pos.hedge_ratio for c in cycles if c.pos.legs],
                "traded": int(any(c.pos.legs for c in cycles))})
    return out


def run_split(ws, strat, prm, lim, model, trig_k=2.5, daily_loss=None, base_cfg=None):
    recs = []
    day, day_pnl = None, 0.0
    for w in ws:
        if w.trig is None or w.trig[0] != trig_k:
            w.trig = (trig_k, triggers(w, trig_k))
        if w.day != day:
            day, day_pnl = w.day, 0.0
        allow = daily_loss is None or day_pnl > -daily_loss
        r = run_window(w, strat, prm, lim, model, w.trig[1], allow_entries=allow, base_cfg=base_cfg)
        day_pnl += r.get("total", 0.0)
        recs.append(r)
    return recs


def summarize(recs, label=""):
    traded = [r for r in recs if r["traded"]]
    pnl = [r.get("total", 0.0) for r in traded]
    n = len(traded)
    cap = sum(r.get("capital", 0.0) for r in traded)
    eq = peak = dd = 0.0
    for r in recs:
        eq += r.get("total", 0.0)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    pcs = [x for r in recs for x in r["pair_costs"]]
    hr = [x for r in recs for x in r["hedge_ratio"]]
    mean = statistics.mean(pnl) if pnl else 0.0
    sd = statistics.pstdev(pnl) if len(pnl) > 1 else 0.0
    s = {
        "label": label, "windows": len(recs), "markets_traded": n,
        "entries": sum(r["entries"] for r in recs), "entry_fills": sum(r["entry_fills"] for r in recs),
        "entry_fill_rate": (sum(r["entry_fills"] for r in recs) / max(1, sum(r["entries"] for r in recs))),
        "hedge_attempts": sum(r["hedges"] for r in recs), "hedge_fills": sum(r["hedge_fills"] for r in recs),
        "avg_hedge_ratio": statistics.mean(hr) if hr else 0.0,
        "pair_locks": sum(r["locks"] for r in recs), "full_hedges": sum(r["full_hedges"] for r in recs),
        "partial_pairs": sum(r["partial"] for r in recs), "naked": sum(r["naked"] for r in recs),
        "pair_cost_avg": statistics.mean(pcs) if pcs else None, "pair_cost_min": min(pcs) if pcs else None,
        "pair_cost_max": max(pcs) if pcs else None,
        "fees": sum(r.get("fees", 0.0) for r in recs), "slippage": sum(r.get("slippage", 0.0) for r in recs),
        "net": sum(pnl), "gross": sum(pnl) + sum(r.get("fees", 0.0) for r in recs),
        "per_market": mean, "sd_market": sd, "se_market": sd / math.sqrt(n) if n > 1 else 0.0,
        "sharpe_like": mean / sd if sd > 0 else 0.0,
        "per_100_capital": 100.0 * sum(pnl) / cap if cap > 0 else 0.0,
        "capital_per_market": cap / n if n else 0.0,
        "max_exposure": max((r.get("max_exposure", 0.0) for r in recs), default=0.0),
        "max_drawdown": dd, "win_rate": sum(1 for x in pnl if x > 0) / n if n else 0.0,
        "loss_rate": sum(1 for x in pnl if x < 0) / n if n else 0.0,
        "tail_p05": float(np.percentile(pnl, 5)) if pnl else 0.0, "worst": min(pnl) if pnl else 0.0,
        "pnl_prediction": sum(r.get("prediction", 0.0) for r in recs),
        "pnl_hedging": sum(r.get("hedging", 0.0) for r in recs),
        "pnl_pairs": sum(r.get("pairs", 0.0) for r in recs),
        "pnl_directional": sum(r.get("directional", 0.0) for r in recs),
        "pnl_incomplete_hedges": sum(r.get("incomplete_hedge_pnl", 0.0) for r in recs),
    }
    return s


# ───────────────────────────────── calibration of the execution constants (TRAIN) ─────────────────────────────────


def calibrate_exec(train, model, prm, lim, trig_k):
    """Entry fill rate, slippage and adverse selection from ungated entries; hedge fill rate and slippage from
    the unconditional strategy's hedges. TRAIN windows only."""
    m0 = pl.ProbModel.from_json(model.to_json())
    m0.exec = {"entry_fill_rate": 1.0, "entry_slip": 0.0, "entry_adv": 0.0, "hedge_fill_rate": 1.0, "hedge_slip": 0.0}
    prm0 = replace(prm, entry_edge=-1.0)
    fills, adv, slip, sent = 0, [], [], 0
    hfills, hslip, hsent = 0, [], 0
    for w in train:
        if w.trig is None or w.trig[0] != trig_k:
            w.trig = (trig_k, triggers(w, trig_k))
        trig = w.trig[1]
        for k, i in enumerate(range(w.i0, w.i1)):
            d = int(trig[k])
            if d == 0:
                continue
            f = feat_at(w, i)
            if f is None:
                continue
            side = "UP" if d > 0 else "DOWN"
            ask, bid, depth = book(w, side, i)
            left = (w.close - float(w.t[i])) / 1000.0
            dec, _ = pl.entry_decision(f, side, ask, bid, depth * (1 - CONTENTION), m0, prm0, lim, pl.MarketExposure(),
                                       left, None)
            if dec is None:
                continue
            sent += 1
            st, px, _ = fok(w, i, side, dec.shares, dec.limit)
            if st != "filled":
                continue
            fills += 1
            slip.append(px - ask)
            won = 1.0 if w.outcome == side else 0.0
            adv.append(dec.p - won)          # predicted P(win) minus the realised result, on fills
        rB = run_window(w, "B", replace(prm, unconditional=True, entry_edge=-1.0), lim, m0, trig)
        hsent += rB["hedges"]
        hfills += rB["hedge_fills"]
        hslip += [x["px"] - x["ask"] for x in rB["hedge_log"] if x.get("px") is not None]
    return {"entry_fill_rate": fills / max(1, sent), "entry_slip": statistics.mean(slip) if slip else 0.0,
            "entry_adv": statistics.mean(adv) if adv else 0.0, "hedge_fill_rate": hfills / max(1, hsent),
            "hedge_slip": statistics.mean(hslip) if hslip else 0.0,
            "n_entries": sent, "n_entry_fills": fills, "n_hedges": hsent, "n_hedge_fills": hfills}


def cmd_prepare():
    ws = load_cache() if os.path.exists(f"{OUT}/windows.pkl") and "--rebuild" not in sys.argv else prepare_cache()
    sp = split(ws)
    model, rep = fit_model(sp)
    prm, lim = pl.PairLockParams(), pl.RiskLimits()
    ex = calibrate_exec(sp["train"], model, prm, lim, 2.5)
    model.exec = {k: float(v) for k, v in ex.items() if not k.startswith("n_")}
    model.meta["exec_counts"] = {k: v for k, v in ex.items() if k.startswith("n_")}
    model.meta["fit_report"] = rep
    model.save(MODEL_PATH)
    json.dump({"fit": rep, "exec": ex, "splits": {k: [v[0].slug, v[-1].slug, len(v)] for k, v in sp.items()}},
              open(f"{OUT}/prepare.json", "w"), indent=1)
    print(json.dumps({"fit": rep, "exec": ex}, indent=1))


STRATS = {
    "BASE": ("BASE", {}),
    "A_nogate": ("A", {"entry_edge": -1.0}),
    "A": ("A", {}),
    "B": ("B", {"unconditional": True}),
    "C": ("C", {}),
}


def cmd_run(name, overrides=None, splits_=None, quiet=False):
    ws = load_cache()
    sp = split(ws)
    model = pl.ProbModel.load(MODEL_PATH)
    strat, base = STRATS[name] if name in STRATS else (name, {})
    kw = {**base, **(overrides or {})}
    trig_k = kw.pop("trig_k", 2.5)
    daily = kw.pop("daily_loss", 40.0)
    lim_kw = {k[4:]: kw.pop(k) for k in list(kw) if k.startswith("lim_")}
    prm = replace(pl.PairLockParams(), **kw)
    lim = replace(pl.RiskLimits(), **lim_kw)
    res = {}
    for s in (splits_ or [x[0] for x in SPLITS]):
        recs = run_split(sp[s], strat, prm, lim, model, trig_k, daily)
        res[s] = summarize(recs, f"{name} {s}")
        res[s]["_recs"] = recs
    if not quiet:
        for s, r in res.items():
            print(fmt(r))
    return res


def fmt(s):
    g = lambda v, f="{:+.2f}": "-" if v is None else f.format(v)   # noqa: E731
    return (f"{s['label']:28} mkts {s['markets_traded']:4} entries {s['entries']:4} fills {s['entry_fills']:4} "
            f"({s['entry_fill_rate']:.0%}) hedges {s['hedge_fills']}/{s['hedge_attempts']} locks {s['pair_locks']} "
            f"full {s['full_hedges']} partial {s['partial_pairs']} naked {s['naked']} pair {g(s['pair_cost_avg'], '{:.3f}')} "
            f"| net {s['net']:+.2f} ({s['per_market']:+.3f}/mkt se {s['se_market']:.3f}) sharpe {s['sharpe_like']:+.3f} "
            f"DD {s['max_drawdown']:+.2f} win {s['win_rate']:.0%} p5 {s['tail_p05']:+.2f} fees {s['fees']:.2f} "
            f"| pred {s['pnl_prediction']:+.2f} hedge {s['pnl_hedging']:+.2f} pairs {s['pnl_pairs']:+.2f} "
            f"dir {s['pnl_directional']:+.2f}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "prepare":
        cmd_prepare()
    elif cmd == "run":
        cmd_run(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else None)


# ───────────────────────────────── batch runs (one load, a process pool) ─────────────────────────────────

_WS = None
_MODEL = None


def _job(arg):
    name, overrides, splits_, keep = arg
    strat, base = STRATS[name] if name in STRATS else (name, {})
    kw = {**base, **(overrides or {})}
    trig_k = kw.pop("trig_k", 2.5)
    daily = kw.pop("daily_loss", 40.0)
    lim_kw = {k[4:]: kw.pop(k) for k in list(kw) if k.startswith("lim_")}
    base_cfg = {k[5:]: kw.pop(k) for k in list(kw) if k.startswith("base_")}
    prm = replace(pl.PairLockParams(), **kw)
    lim = replace(pl.RiskLimits(), **lim_kw)
    sp = split(_WS)
    out = {}
    for s in splits_:
        recs = run_split(sp[s], strat, prm, lim, _MODEL, trig_k, daily, base_cfg)
        out[s] = summarize(recs, f"{name} {s}")
        if keep:
            out[s]["_recs"] = recs
    return name, overrides, out


def batch(jobs, splits_=None, keep=False, procs=4):
    """jobs: [(name, overrides)]. Returns [(name, overrides, {split: summary})]."""
    global _WS, _MODEL
    import multiprocessing as mp
    if _WS is None:
        _WS = load_cache()
        _MODEL = pl.ProbModel.load(MODEL_PATH)
    splits_ = splits_ or [x[0] for x in SPLITS]
    args = [(n, o, splits_, keep) for n, o in jobs]
    if procs <= 1 or len(args) == 1:
        return [_job(a) for a in args]
    ctx = mp.get_context("fork")
    with ctx.Pool(procs) as pool:
        return pool.map(_job, args, chunksize=1)
