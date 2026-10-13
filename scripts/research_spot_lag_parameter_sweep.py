"""Spot-lag parameter sweep: MAX_ASK x exit, with the realistic execution model.

    python scripts/research_spot_lag_parameter_sweep.py build [--latency-scale 1.0]
    python scripts/research_spot_lag_parameter_sweep.py sweep \\
        --max-asks 0.50 0.55 0.60 ... 0.98 --output results/spot_lag_parameter_sweep.csv

RESEARCH ONLY: reads the chart archive (through the research feature cache,
data/replay/rm_research_cache, built by scripts/research_repriced_momentum.py cache),
writes files under data/replay/sl_sweep/ and results/. It changes no strategy code,
no configuration and no running process.

build: every point where the live spot-lag rule fires (strategy/spot_lag.py:
1 s spot move >= 3 sigma, 3 s agrees >= 0.5, accelerating, one per market and direction
per 2 s, 60-295 s left) is a CANDIDATE, priced exactly as the engine prices it:
    ev        = the side's mid 1 s ago + (MFV now - MFV 1 s ago)
    exit cost = fee(ev) + spread / 2;   fixed = 0.5c slippage + 0.5c uncertainty + exit cost
    max entry = highest p on the cent grid with p + fee(p) <= ev - fixed - 2c
    cap       = min(ask + 2c, max entry)                          (the production limit)
A candidate is a SIGNAL when it passes every production entry gate except the price
ceiling (ask >= 0.05, spread <= 3c, lag = ev - ask > 0, max entry >= ask) with the research
ceiling of 0.98 -- MAX_ASK is then a pure filter applied in the sweep. Every signal gets ONE
realistic order (backtest/spot_shock_replay.Execution, the model spot-lag was validated
with): measured pre-send and RTT/2 latency, the venue's book read 500 ms after arrival, FOK
at the cap, 5 shares, no partial fills. A filled order gets EVERY exit below, all from the
same fill, plus its price path (maximum favourable excursion, targets reached, reversals).

Exits (sold as the live bot sells: FOK at the bid - 2c floor, retried every 2 s; no exit in
the market's last 10 s -- the position settles at 1 / 0, no fee):
    live            5 s after the fill, held to 10 s while the 1 s spot move is >= 0.5 sigma our
                    way (the production rule, strategy/spot_lag.py exit_decision)
    t<s>            a fixed hold: 0.5 1 2 3 5 7 10 15 20 30 s
    tp<c>_<max>     sell AT the bid once bid - entry >= c cents and the trade clears 0.5c after
                    both fees (the live bot's TAKE_PROFIT rule), else at <max> s (or settlement)
    <event>_<max>   spot_rev (spot 500 ms move against us), tok_rev (token 500 ms move against
                    us), gap_closed (mid >= ev now), fair_below_bid (ev now < bid), edge_gone
                    (ev now - bid <= 0.5c), else at <max> s
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from troll_poly_bot.backtest.spot_shock_replay import SETTLE_GUARD_MS, SIZE, detect_shocks, load_asset, windows  # noqa: E402
import research_repriced_momentum as R                                               # noqa: E402

CACHE = R.CACHE
OUT = ROOT / "data/replay/sl_sweep"
ASSETS = R.ASSETS
SPLITS = R.SPLITS
#: the last window open the earlier studies had read (09-24 19:10 UTC); later windows are
#: reported separately as UNSEEN 3b, data no study had examined
FRESH2_AFTER = 1790277000
# the production entry rule (strategy/spot_lag.py SpotLagConfig defaults / the live app)
EDGE, SLIP, UNC, MAX_SLIP, MAX_SPREAD, MIN_ASK, RESEARCH_MAX_ASK = 0.02, 0.005, 0.005, 0.02, 0.03, 0.05, 0.98
FIXED_S = (0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30)
TARGETS_C = (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20)
TARGET_MAX = (5, 10, 20, 30, 0)                  # 0 = no time limit: to the target or settlement
EVENTS = ("spot_rev", "tok_rev", "gap_closed", "fair_below_bid", "edge_gone")
EVENT_MAX = (10, 20, 30)
EXITS = (("live",) + tuple(f"t{s:g}" for s in FIXED_S)
         + tuple(f"tp{c}_{m if m else 'settle'}" for c in TARGETS_C for m in TARGET_MAX)
         + tuple(f"{e}_{m}" for e in EVENTS for m in EVENT_MAX))
XCOLS = ("px", "hold", "flag", "xbid", "xmid")   # exit price, seconds held, 0 sold / 2 settled, bid & mid at the exit decision
PATH_TARGETS_C = (1, 2, 3, 5, 10, 15, 20)
CAND = ("split", "epoch", "asset", "t", "dir", "ask", "bid", "ev", "rate", "left", "z1", "z3", "sigma_bps", "tok1",
        "adep", "signal", "cap", "max_entry", "lat_ms", "known_ms", "arr_ask", "filled", "entry", "fresh")
PATH = ("mfe30", "mae30", "t_mfe30", "mfe_all", "rev_after_mfe") + tuple(f"hit{c}" for c in PATH_TARGETS_C) + \
       tuple(f"t_hit{c}" for c in PATH_TARGETS_C)


def _max_entry(ev: float, rate: float, fixed: float, edge: float) -> float | None:
    budget = ev - fixed - edge
    p = math.floor((min(budget, 0.99) + 1e-9) * 100) / 100
    while p >= 0.01:
        if p + rate * p * (1 - p) <= budget + 1e-12:
            return round(p, 2)
        p = round(p - 0.01, 2)
    return None


def _sell_at(w, ex, sv, t_dec, won):
    """The live bot's time-stop sell decided at ``t_dec``: (px, how, t_out, bid, mid at decision)."""
    jd = w.last_at(t_dec)
    b = float(sv["bid"][jd]) if jd >= 0 and sv["bid"][jd] == sv["bid"][jd] else float("nan")
    mm = float(sv["mid"][jd]) if jd >= 0 and sv["mid"][jd] == sv["mid"][jd] else float("nan")
    px, how, t_out, _ = ex.sell(w, sv, t_dec, (b - 0.02) if b == b else 0.0, won)
    return px, how, t_out, b, mm


def _put(X, n, got, t_arr):
    if got is None or got[0] is None:
        return
    px, how, t_out, b, mm = got
    X[n] = (px, (t_out - t_arr) / 1000.0, 2.0 if how == "SETTLE" else 0.0, b, mm)


def build_asset(args) -> dict:
    asset, scale = args
    d, meta = load_asset(CACHE, asset)
    ex = R.ScaledExecution(seed=sum(map(ord, asset)) * 7 + 3, scale=scale)
    C, P, XS = [], [], []
    for m, w in zip(meta, windows(d, meta)):
        if len(w) < 30:
            continue
        t, sg = w.t, w.sigma
        r1, r3, r500 = (R._ret(t, w.lp, h) for h in (1000.0, 3000.0, 500.0))
        k1000, k500 = w.lagged_index(1000.0), w.lagged_index(500.0)
        guard = w.last_at(w.close - SETTLE_GUARD_MS - 1.0)
        for i, dr in detect_shocks(w, k=3.0, k3=0.5, accel=True):
            sv = w.side["UP" if dr > 0 else "DOWN"]
            mid, fvs, ask, bid, adep = sv["mid"], sv["mfv"], sv["ask"], sv["bid"], sv["adep"]
            i0 = k1000[i]
            if i0 < 0 or t[i] - t[i0] > 1600.0 or mid[i0] != mid[i0] or fvs[i0] != fvs[i0] or ask[i] != ask[i] \
                    or bid[i] != bid[i] or fvs[i] != fvs[i]:
                continue
            a0, b0 = float(ask[i]), float(bid[i])
            mid_b, fv_b = float(mid[i0]), float(fvs[i0])
            ev = min(max(mid_b + float(fvs[i] - fv_b), 0.0), 1.0)
            rate = w.rate
            evc = min(max(ev, 0.01), 0.99)
            fixed = SLIP + UNC + rate * evc * (1 - evc) + (a0 - b0) / 2.0
            me = _max_entry(ev, rate, fixed, EDGE)
            signal = (MIN_ASK <= a0 <= RESEARCH_MAX_ASK and a0 - b0 <= MAX_SPREAD + 1e-9 and ev - a0 > 0
                      and me is not None and me >= a0 - 1e-9)
            cap = min(round(a0 + MAX_SLIP, 2), me) if signal else float("nan")
            won = None if w.won_up is None else float(("UP" if dr > 0 else "DOWN") == ("UP" if w.won_up else "DOWN"))
            row = [SPLITS.index(w.split) if w.split in SPLITS else -1, w.epoch, ASSETS.index(asset), float(t[i]), dr,
                   a0, b0, ev, rate, float(w.left[i]), float(dr * r1[i] / sg[i]), float(dr * r3[i] / (sg[i] * math.sqrt(3))),
                   float(sg[i] * 1e4), float(dr * (mid[i] - mid[i0]) if mid[i] == mid[i] else np.nan),
                   float(adep[i] or 0.0), 1.0 if signal else 0.0, cap, me if me is not None else np.nan]
            X = np.full((len(EXITS), len(XCOLS)), np.nan, dtype=np.float32)
            path = [np.nan] * len(PATH)
            if signal:
                fl = ex.buy(w, sv, float(t[i]), cap)
                row += [fl["latency_ms"], fl["t_known"] - t[i], fl["arrival_ask"] if fl["arrival_ask"] is not None else np.nan,
                        1.0 if fl["filled"] else 0.0, fl.get("entry", np.nan)]
                if fl["filled"]:
                    _exits(w, ex, sv, X, float(fl["entry"]), fl["t_arr"], fl["t_known"], dr, r1, r500, k500, mid_b, fv_b,
                           guard, won)
                    path = _path(w, sv, float(fl["entry"]), fl["t_known"], guard)
            else:
                row += [np.nan, np.nan, np.nan, 0.0, np.nan]
            row.append(1.0 if w.epoch > FRESH2_AFTER else 0.0)
            C.append(row)
            P.append(path)
            XS.append(X)
    return {"asset": asset, "C": np.array(C, dtype=np.float64).reshape(-1, len(CAND)),
            "P": np.array(P, dtype=np.float64).reshape(-1, len(PATH)),
            "X": np.array(XS, dtype=np.float32).reshape(-1, len(EXITS), len(XCOLS))}


def _exits(w, ex, sv, X, entry, t_arr, t_known, dr, r1, r500, k500, mid_b, fv_b, guard, won):
    bid, mid, fvs = sv["bid"], sv["mid"], sv["mfv"]
    t = w.t
    n = 0
    # the production rule: 5 s, extended to 10 s while the 1 s spot move is >= 0.5 sigma our way
    z1s = dr * r1 / w.sigma
    j = w.first_at(t_known)
    t_sell = None
    while j < len(w) and t[j] < w.close - SETTLE_GUARD_MS:
        held = (t[j] - t_arr) / 1000.0
        if held >= 5.0 and not (held < 10.0 and z1s[j] == z1s[j] and z1s[j] >= 0.5):
            t_sell = float(t[j])
            break
        j += 1
    _put(X, n, _sell_at(w, ex, sv, t_sell, won) if t_sell is not None else
         ((won, "SETTLE", w.close, float("nan"), float("nan")) if won is not None else None), t_arr)
    n += 1
    for s in FIXED_S:
        _put(X, n, _sell_at(w, ex, sv, max(t_known, t_arr + s * 1000.0), won), t_arr)
        n += 1
    for c in TARGETS_C:
        for mx in TARGET_MAX:
            got = R._tp_exit(w, sv, ex, entry, t_arr, t_known, c / 100.0, 0.0, float(mx), won)
            if got is not None:
                px, hold, flag, kind = got
                jd = w.last_at(t_arr + hold * 1000.0)
                X[n] = (px, hold, flag, bid[jd] if jd >= 0 else np.nan, mid[jd] if jd >= 0 else np.nan)
            n += 1
    ev_now = mid_b + (fvs - fv_b)
    z500 = dr * r500
    tok500 = np.where(k500 >= 0, mid - mid[np.maximum(k500, 0)], np.nan)
    trig = {"spot_rev": np.nan_to_num(z500, nan=0.0) < 0.0,
            "tok_rev": np.nan_to_num(tok500, nan=0.0) < 0.0,
            "gap_closed": mid >= ev_now - 1e-9,
            "fair_below_bid": ev_now < bid - 1e-9,
            "edge_gone": ev_now - bid <= 0.005 + 1e-9}
    j_from = w.first_at(t_known)
    for e in EVENTS:
        for mx in EVENT_MAX:
            j_to = min(w.last_at(t_arr + mx * 1000.0), guard)
            seg = trig[e][j_from:j_to + 1] & ~np.isnan(bid[j_from:j_to + 1])
            if len(seg) and seg.any():
                jj = j_from + int(np.argmax(seg))
                got = _sell_at(w, ex, sv, float(t[jj]), won)
            else:
                got = _sell_at(w, ex, sv, max(t_known, t_arr + mx * 1000.0), won)
            _put(X, n, got, t_arr)
            n += 1


def _path(w, sv, entry, t_known, guard):
    """What the bid did after the fill was known: the best and worst it offered within 30 s and
    to the end of trading, when each target was first reached, and whether the price came back
    below the entry after its 30 s best."""
    bid = sv["bid"]
    t = w.t
    j0 = w.first_at(t_known)
    j30 = min(w.last_at(t_known + 30_000.0), guard)
    if j30 <= j0:
        return [np.nan] * len(PATH)
    seg = bid[j0:j30 + 1] - entry
    ok = ~np.isnan(seg)
    if not ok.any():
        return [np.nan] * len(PATH)
    k = int(np.nanargmax(seg))
    mfe30, mae30 = float(np.nanmax(seg)), float(np.nanmin(seg))
    after = seg[k:]
    rev = 1.0 if np.nanmin(after) < 0 else 0.0
    alls = bid[j0:guard + 1] - entry
    mfe_all = float(np.nanmax(alls)) if np.any(~np.isnan(alls)) else np.nan
    hits, times = [], []
    for c in PATH_TARGETS_C:
        idx = np.where(np.nan_to_num(seg, nan=-9) >= c / 100.0 - 1e-9)[0]
        hits.append(1.0 if len(idx) else 0.0)
        times.append((t[j0 + idx[0]] - t_known) / 1000.0 if len(idx) else np.nan)
    return [mfe30, mae30, (t[j0 + k] - t_known) / 1000.0, mfe_all, rev] + hits + times


def build(scale: float = 1.0, procs: int = 3) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(procs) as pool:
        parts = list(pool.map(build_asset, [(a, scale) for a in ASSETS]))
    C = np.concatenate([p["C"] for p in parts if len(p["C"])])
    P = np.concatenate([p["P"] for p in parts if len(p["P"])])
    X = np.concatenate([p["X"] for p in parts if len(p["X"])])
    o = np.argsort(C[:, CAND.index("t")], kind="stable")
    out = OUT / f"signals_lat{scale:g}.npz"
    np.savez_compressed(out, C=C[o], P=P[o], X=X[o], cand=np.array(CAND), path=np.array(PATH), exits=np.array(EXITS),
                        xcols=np.array(XCOLS))
    return out


# ─────────────────────────────── the sweep ───────────────────────────────


class Data:
    def __init__(self, scale: float = 1.0) -> None:
        z = np.load(OUT / f"signals_lat{scale:g}.npz")
        self.C, self.P, self.X = z["C"], z["P"], z["X"].astype(np.float64)
        self.c = {n: i for i, n in enumerate(CAND)}
        self.p = {n: i for i, n in enumerate(PATH)}
        self.e = {n: i for i, n in enumerate(EXITS)}
        for n in ("ask", "bid", "cap", "arr_ask", "entry", "max_entry"):
            self.C[:, self.c[n]] = np.round(self.C[:, self.c[n]], 6)
        self.X[:, :, 0] = np.round(self.X[:, :, 0], 6)

    def col(self, n):
        return self.C[:, self.c[n]]


def simulate(d: Data, max_ask: float, exit_name: str, *, hype: bool = False, positions: int = 4, exposure: float = 10.0,
             per_hour: int = 60, attempts_max: int = 3, retry_ms: float = 2000.0, fee_mult: float = 1.0,
             slip_leg: float = 0.0, spread_add: float = 0.0, fill_keep: np.ndarray | None = None) -> list[dict]:
    """The production budget in time order across every market (4 positions and $10 at the limit,
    60 buys / h, 3 attempts and 1 entry per market, 2 s retry). Returns one record per SIGNAL:
    what happened to it (rejected by MAX_ASK / skipped by the budget / killed / filled and sold)."""
    C, c, e = d.C, d.c, d.e[exit_name]
    out = []
    busy, buys = [], []
    att, entered, last = defaultdict(int), set(), {}
    hype_i = ASSETS.index("HYPE")
    for n in range(len(C)):
        row = C[n]
        if row[c["signal"]] != 1.0 or (not hype and int(row[c["asset"]]) == hype_i):
            continue
        rec = {"n": n, "split": int(row[c["split"]]), "fresh": row[c["fresh"]] == 1.0, "status": ""}
        out.append(rec)
        ask, cap = row[c["ask"]], row[c["cap"]]
        if ask > max_ask + 1e-9:
            rec["status"] = "max_ask"
            continue
        t0 = row[c["t"]]
        mk = (int(row[c["asset"]]), int(row[c["epoch"]]))
        if mk in entered or att[mk] >= attempts_max or t0 - last.get(mk, -1e18) < retry_ms:
            rec["status"] = "market"
            continue
        busy = [b for b in busy if b[0] > t0]
        if len(busy) >= positions or sum(b[1] for b in busy) + cap * SIZE > exposure + 1e-9:
            rec["status"] = "budget"
            continue
        buys = [b for b in buys if b > t0 - 3_600_000.0]
        if len(buys) >= per_hour:
            rec["status"] = "hourly"
            continue
        buys.append(t0)
        att[mk] += 1
        last[mk] = t0
        px, hold, flag, xbid, xmid = d.X[n, e]
        filled = row[c["filled"]] == 1.0 and px == px and (fill_keep is None or fill_keep[n])
        if not filled:
            rec["status"] = "killed"
            busy.append((t0 + row[c["known_ms"]], cap * SIZE))
            continue
        entered.add(mk)
        rate = row[c["rate"]] * fee_mult
        entry = row[c["entry"]] + slip_leg + spread_add / 2.0
        taker = flag == 0.0
        xp = px - slip_leg - spread_add / 2.0 if taker else px
        fees = rate * entry * (1 - entry) + (rate * xp * (1 - xp) if taker else 0.0)
        mid_dec = (ask + row[c["bid"]]) / 2.0
        rec.update(status="filled", net=xp - entry - fees, gross=xp - entry, fees=fees,
                   spread=(ask - mid_dec) + ((xmid - xbid) if taker and xbid == xbid and xmid == xmid else 0.0),
                   slip=(row[c["entry"]] - ask) + ((xbid - px) if taker and xbid == xbid else 0.0),
                   hold=hold, asset=int(row[c["asset"]]), epoch=int(row[c["epoch"]]), t=t0, ask=ask)
        busy.append((t0 + row[c["lat_ms"]] + hold * 1000.0, row[c["entry"]] * SIZE))
    return out


def metrics(recs: list[dict], hours: float) -> dict:
    sig = len(recs)
    rej = sum(r["status"] == "max_ask" for r in recs)
    orders = sum(r["status"] in ("killed", "filled") for r in recs)
    f = [r for r in recs if r["status"] == "filled"]
    net = np.array([r["net"] for r in f])
    out = {"signals": sig, "rejected_max_ask": rej, "rejected_max_ask_pct": rej / sig if sig else float("nan"),
           "orders": orders, "fills": len(f), "fill_rate": len(f) / orders if orders else float("nan"), "hours": hours,
           "orders_per_h": orders / hours if hours else float("nan"), "fills_per_h": len(f) / hours if hours else float("nan")}
    if len(f) == 0:
        return out
    by = defaultdict(float)
    for r in f:
        by[r["epoch"]] += r["net"]
    v = np.array(list(by.values()))
    se = v.std(ddof=1) / math.sqrt(len(v)) if len(v) > 2 else float("nan")
    order = np.argsort([r["t"] for r in f])
    cum = np.concatenate([[0.0], np.cumsum(net[order] * SIZE)])
    wins, losses = net[net > 0], net[net <= 0]
    markets = len({(r["asset"], r["epoch"]) for r in f})
    out.update(wins=int((net > 0).sum()), losses=int((net <= 0).sum()), win_rate=float((net > 0).mean()),
               gross_usdc=float(sum(r["gross"] for r in f) * SIZE), fees_usdc=float(sum(r["fees"] for r in f) * SIZE),
               spread_usdc=float(sum(r["spread"] for r in f) * SIZE), slippage_usdc=float(sum(r["slip"] for r in f) * SIZE),
               net_usdc=float(net.sum() * SIZE), net_c_share=float(net.mean() * 100), median_c_share=float(np.median(net) * 100),
               sd_c_share=float(net.std(ddof=1) * 100) if len(net) > 1 else float("nan"),
               t_stat=float(v.mean() / se) if se == se and se > 0 else float("nan"),
               ci95_lo=float((net.mean() - 1.96 * se * len(v) / len(net)) * 100) if se == se else float("nan"),
               ci95_hi=float((net.mean() + 1.96 * se * len(v) / len(net)) * 100) if se == se else float("nan"),
               max_dd_usdc=float(np.max(np.maximum.accumulate(cum) - cum)),
               profit_factor=float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
               avg_hold_s=float(np.mean([r["hold"] for r in f])), usdc_per_market=float(net.sum() * SIZE / markets),
               usdc_per_hour=float(net.sum() * SIZE / hours) if hours else float("nan"),
               usdc_per_fill=float(net.mean() * SIZE), usdc_per_order=float(net.sum() * SIZE / orders),
               epochs=len(v))
    return out


def hours_by_period(d: Data) -> dict:
    out = {}
    for i, s in enumerate(SPLITS):
        out[s] = len(np.unique(d.col("epoch")[d.col("split") == i])) / 12.0
    fresh = d.col("fresh") == 1.0
    out["unseen_3b"] = len(np.unique(d.col("epoch")[fresh])) / 12.0
    return out


PERIODS = SPLITS + ("unseen_3b",)


def split_recs(recs, period):
    if period == "unseen_3b":
        return [r for r in recs if r["fresh"]]
    return [r for r in recs if SPLITS[r["split"]] == period]


def sweep(max_asks, exits, output: Path, scale: float = 1.0) -> None:
    d = Data(scale)
    hours = hours_by_period(d)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["max_ask", "exit", "period"] + list(metrics([], 1.0).keys()) + [
        "wins", "losses", "win_rate", "gross_usdc", "fees_usdc", "spread_usdc", "slippage_usdc", "net_usdc", "net_c_share",
        "median_c_share", "sd_c_share", "t_stat", "ci95_lo", "ci95_hi", "max_dd_usdc", "profit_factor", "avg_hold_s",
        "usdc_per_market", "usdc_per_hour", "usdc_per_fill", "usdc_per_order", "epochs"]
    n = 0
    with open(output, "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=fields)
        wr.writeheader()
        for ma in max_asks:
            for ex in exits:
                recs = simulate(d, ma, ex)
                for p in PERIODS:
                    m = metrics(split_recs(recs, p), hours[p])
                    wr.writerow({"max_ask": ma, "exit": ex, "period": p, **m})
                m = metrics(recs, sum(hours[s] for s in SPLITS))
                wr.writerow({"max_ask": ma, "exit": ex, "period": "all", **m})
                n += 1
    print(f"wrote {output}: {n} combinations x {len(PERIODS) + 1} periods")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--latency-scale", type=float, default=1.0)
    s = sub.add_parser("sweep")
    s.add_argument("--max-asks", type=float, nargs="+",
                   default=[0.50, 0.55] + [round(0.60 + 0.01 * k, 2) for k in range(36)] + [0.98])
    s.add_argument("--exits", nargs="+", default=list(EXITS), help=f"any of: {' '.join(EXITS)}")
    s.add_argument("--exit-times", type=float, nargs="*", default=None,
                   help="shorthand: only these fixed holds (seconds) plus the live exit")
    s.add_argument("--latency-scale", type=float, default=1.0)
    s.add_argument("--output", default=str(ROOT / "results/spot_lag_parameter_sweep.csv"))
    args = ap.parse_args()
    if args.cmd == "build":
        print("built", build(args.latency_scale))
    else:
        exits = args.exits if args.exit_times is None else ["live"] + [f"t{x:g}" for x in args.exit_times]
        sweep(sorted(set(args.max_asks)), exits, Path(args.output), args.latency_scale)


if __name__ == "__main__":
    main()
