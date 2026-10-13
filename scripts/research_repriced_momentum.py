"""Research, stage 1: the repriced-token DATASET.

    python scripts/research_repriced_momentum.py cache                  # features from data/charts
    python scripts/research_repriced_momentum.py build [--shocks spotlag|broad|both]

Every archive point in the 5 s after a spot shock becomes one row (``Candidate``):
what was knowable at that instant (features), what happened after (forward moves,
excursions -- DIAGNOSTICS only, never inputs), and what one realistic order would
have done: one draw of the measured latency, the venue_lag fill rule, and the
outcome of each of 22 exits. The strategy work (stage 2, ``analyze``) filters and
combines these rows; nothing there re-simulates or looks ahead.

Shock sets
  spotlag  the live rule: 1 s spot move >= 3 sigma, 3 s agrees >= 0.5, accelerating
  broad    >= 2 sigma, no 3 s / acceleration filter (kept as features): for the
           shock-size analysis and a redefined shock
Execution (backtest/spot_shock_replay.Execution -- the one model spot-lag uses)
  FOK buy at a limit: fills at the proxied venue ask on arrival if <= limit with 5
  shares behind it. The row stores the arrival ask, so any limit is evaluated
  exactly (fill iff arrival ask <= limit) against the same exits.
Exits (22): timers 0.25 0.5 0.75 1 1.5 2 3 5 8 10 s; events (max 8 s) spot reversal,
  token reversal, momentum collapse, fair reached, fair exceeded, book reversal,
  spread expansion, cross-exchange confirmation lost; MAKER targets (resting sell at
  entry +1/+2/+3c or at fair, filled only when the venue bid trades THROUGH it, no
  fee; else taker at 5 s).
READ-ONLY on the archive; writes data/replay/rm_research/dataset_<set>.npz.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.backtest.spot_shock_replay import (  # noqa: E402
    SETTLE_GUARD_MS, SIZE, Execution, build_features, chronological_splits, detect_shocks, load_asset, windows,
)

CACHE = ROOT / "data/replay/rm_research_cache"
OUT = ROOT / "data/replay/rm_research"
ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
SPLITS = ("train", "validation", "unseen_1", "unseen_2", "unseen_3")
#: the last window open any earlier study had seen (2026-09-24 16:25 UTC): everything after
#: it is UNSEEN_3, data no study had examined in any form
FRESH_AFTER = 1790267100
SHOCK_SETS = {"spotlag": dict(k=3.0, k3=0.5, accel=True), "broad": dict(k=2.0, k3=-1e9, accel=False)}
WINDOW_MS = 5000.0
HORIZONS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
TIMERS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 10.0)
EVENTS = ("spot_reversal", "token_reversal", "momentum_collapse", "fair_reached", "fair_exceeded",
          "book_reversal", "spread_expansion", "xex_lost")
MAKERS = ("maker_1c", "maker_2c", "maker_3c", "maker_fair")
EXITS = tuple(f"t{h:g}" for h in TIMERS) + EVENTS + MAKERS
EVENT_MAX_S = 8.0
MAKER_MAX_S = 5.0
EXCHANGES = ("bn", "byb", "cb")
FEATURES = (
    # the shock
    "z1_shock", "z3_shock", "acc_shock", "peak_z1", "shock_dur", "n_prior", "persist",
    # cross-exchange, at the shock and now
    "zx_bn", "zx_byb", "zx_cb", "n_confirm2", "zx_min", "zx_max", "first_mover", "n_conf_now", "n_exch",
    # repricing
    "pct", "expected", "actual", "gap", "overshoot", "ask", "bid", "mid", "spread", "fair",
    # momentum now
    "z250", "z500", "z1000", "z2000", "t250", "t500", "t1000", "t_acc", "s_acc", "tok_vel", "tok_high_pb",
    "breakout",
    # the Polymarket book (the traded token)
    "ask_sz", "bid_sz", "imb", "ask_sz_chg", "spread_chg",
    # spot order flow, signed to the shock's direction
    "tfi", "obi", "orate",
    # regime and context
    "sigma_bps", "left", "tss", "is_up", "rate",
)
FWD_MID = tuple(f"mid_{h:g}" for h in HORIZONS)
FWD_BID = tuple(f"bid_{h:g}" for h in HORIZONS)
FWD_OTHER = ("fairdist_1", "fairdist_3", "fairdist_5", "mfe_bid", "mae_bid", "mfe_mid", "mae_mid", "t_mfe", "t_rev")
ENTRY = ("lat_ms", "known_ms", "arr_ask", "arr_ok", "won")


def split_fn(epochs) -> dict:
    """The earlier study's chronological split of everything up to FRESH_AFTER (train 50 %,
    validation 20 %, unseen 1 and 2 15 % each), plus UNSEEN_3 = every later window."""
    old = [e for e in set(epochs) if e <= FRESH_AFTER]
    base = chronological_splits(old, names=("train", "validation", "unseen_1", "unseen_2"))
    return {e: base.get(e, "unseen_3") for e in set(epochs)}


def _ret(t: np.ndarray, x: np.ndarray, lag_ms: float) -> np.ndarray:
    """Causal difference x[now] - x[now - lag] (NaN without a point that old, or a gap)."""
    j = np.searchsorted(t, t - lag_ms, side="right") - 1
    ok = (j >= 0) & (t - t[np.maximum(j, 0)] <= lag_ms + 1500.0)
    return np.where(ok, x - x[np.maximum(j, 0)], np.nan)


def build_asset(args) -> dict:
    asset, set_name = args
    sk = SHOCK_SETS[set_name]
    d, meta = load_asset(CACHE, asset)
    ex = Execution(seed=sum(map(ord, asset)) + (7 if set_name == "broad" else 0))
    rows_F, rows_M, rows_B, rows_O, rows_E, rows_X, keys = [], [], [], [], [], [], []
    base = []
    shock_no = 0
    for m, w in zip(meta, windows(d, meta)):
        if len(w) < 30:
            continue
        s0, e0 = int(m[6]), int(m[7])
        t = w.t
        sg = w.sigma
        lx = {x: np.log(np.where(d[x][s0:e0] > 0, d[x][s0:e0], np.nan)) for x in EXCHANGES}
        tfi, obi, orate = d["tfi"][s0:e0], d["obi"][s0:e0], d["orate"][s0:e0]
        r250, r500, r1, r2, r3 = (_ret(t, w.lp, h) for h in (250.0, 500.0, 1000.0, 2000.0, 3000.0))
        rx1 = {x: _ret(t, lx[x], 1000.0) for x in EXCHANGES}
        rx500 = {x: _ret(t, lx[x], 500.0) for x in EXCHANGES}
        k250, k500, k1000 = (w.lagged_index(h) for h in (250.0, 500.0, 1000.0))
        guard = w.last_at(w.close - SETTLE_GUARD_MS - 1.0)
        z1 = r1 / sg
        shocks = detect_shocks(w, k=sk["k"], k3=sk["k3"], accel=sk["accel"])
        for si, (i, dr) in enumerate(shocks):
            side = "UP" if dr > 0 else "DOWN"
            sv = w.side[side]
            mid, bid, ask, fvs, adep, bdep = sv["mid"], sv["bid"], sv["ask"], sv["mfv"], sv["adep"], sv["bdep"]
            i0 = k1000[i]
            if i0 < 0 or mid[i0] != mid[i0] or fvs[i0] != fvs[i0]:
                continue
            shock_no += 1
            mid_b, fv_b = float(mid[i0]), float(fvs[i0])
            won = np.nan if w.won_up is None else float((side == "UP") == w.won_up)
            # ---- the shock's own features (known at the shock) ----
            z1s = dr * z1[i]
            z3s = dr * r3[i] / (sg[i] * math.sqrt(3.0))
            accs = dr * (r1[i] - r3[i] / 3.0) / sg[i]
            k = i
            while k > 0 and dr * z1[k - 1] >= 1.0 and t[i] - t[k - 1] <= 10_000.0:
                k -= 1
            shock_dur = (t[i] - t[k]) / 1000.0
            n_prior = sum(1 for (i2, d2) in shocks[:si] if d2 == dr and t[i] - t[i2] <= 30_000.0)
            zx = {x: dr * rx1[x][i] / sg[i] for x in EXCHANGES}
            zvals = [v for v in zx.values() if v == v]
            n_conf2 = sum(1 for v in zvals if v >= 2.0)
            first, first_t = -1, np.inf
            kk = np.searchsorted(t, t[i] - 1000.0)
            for xi, x in enumerate(EXCHANGES):
                seg = dr * rx1[x][kk:i + 1] / sg[kk:i + 1]
                hit = np.where(seg >= 1.5)[0]
                if len(hit) and t[kk + hit[0]] < first_t:
                    first, first_t = xi, t[kk + hit[0]]
            # ---- per-window arrays the exits read ----
            z500a, tok500, fair_a, imb_a, spr_a, nconf_a, navail = _exit_arrays(sv, dr, sg, r500, k500, rx500,
                                                                                 mid_b, fv_b)
            won_f = None if won != won else won

            if set_name == "spotlag":
                base.append(_spot_lag_baseline(w, ex, sv, i, dr, r1, mid_b, fv_b, won_f, asset))

            j_last = min(w.last_at(t[i] + WINDOW_MS), len(t) - 1)
            hi_mid = -np.inf
            for j in range(i, j_last + 1):
                if w.left[j] < 60.0:
                    break
                a, b, mm = ask[j], bid[j], mid[j]
                prev_hi = hi_mid
                if mm == mm:
                    hi_mid = max(hi_mid, mm)
                if a != a or b != b or mm != mm or fvs[j] != fvs[j]:
                    continue
                expected = float(fvs[j] - fv_b)
                if expected < 0.01:
                    continue
                pct = (mm - mid_b) / expected
                if not (-0.25 <= pct <= 3.0) or not (0.03 <= a <= 0.97) or a - b > 0.05 + 1e-9 or (adep[j] or 0.0) < SIZE:
                    continue
                sgj = float(sg[j])
                fair = mid_b + expected
                j5 = k500[j]
                F = [z1s, z3s, accs, float(np.nanmax(dr * z1[i:j + 1])), shock_dur, n_prior,
                     dr * (w.lp[j] - w.lp[i]) / sgj,
                     zx["bn"], zx["byb"], zx["cb"], n_conf2, min(zvals) if zvals else np.nan,
                     max(zvals) if zvals else np.nan, first, nconf_a[j], navail[j],
                     pct, expected, mm - mid_b, fair - a, a - fair, a, b, mm, a - b, fair,
                     dr * r250[j] / (sgj * 0.5), z500a[j], dr * r1[j] / sgj, dr * r2[j] / (sgj * math.sqrt(2.0)),
                     mm - mid[k250[j]] if k250[j] >= 0 else np.nan, tok500[j],
                     mm - mid[k1000[j]] if k1000[j] >= 0 else np.nan,
                     (tok500[j] - (mm - mid[k1000[j]] - tok500[j])) if k1000[j] >= 0 else np.nan,
                     dr * (r500[j] - (r1[j] - r500[j])) / sgj,
                     (mm - mid[i]) / max((t[j] - t[i]) / 1000.0, 0.2),
                     (prev_hi - mm) if prev_hi > -np.inf else 0.0,
                     1.0 if (prev_hi > -np.inf and mm > prev_hi + 0.004) else 0.0,
                     adep[j], bdep[j], imb_a[j],
                     (adep[j] - adep[j5]) if j5 >= 0 else np.nan, (spr_a[j] - spr_a[j5]) if j5 >= 0 else np.nan,
                     dr * tfi[j], dr * obi[j], orate[j],
                     sgj * 1e4, w.left[j], (t[j] - t[i]) / 1000.0, 1.0 if dr > 0 else 0.0, w.rate]
                # ---- what happened next (diagnostics only) ----
                fm, fb = [], []
                for h in HORIZONS:
                    jh = w.last_at(t[j] + h * 1000.0)
                    ok = j < jh <= guard and mid[jh] == mid[jh]
                    fm.append(mid[jh] - mm if ok else np.nan)
                    fb.append(bid[jh] if ok else np.nan)
                fo = []
                for h in (1.0, 3.0, 5.0):
                    jh = w.last_at(t[j] + h * 1000.0)
                    fo.append(fair_a[jh] - mid[jh] if (j < jh <= guard and mid[jh] == mid[jh]) else np.nan)
                j5s = min(w.last_at(t[j] + 5000.0), guard)
                if j5s > j:
                    bseg, mseg = bid[j + 1:j5s + 1], mid[j + 1:j5s + 1]
                    if np.all(np.isnan(bseg)):
                        fo += [np.nan] * 6
                    else:
                        kb = int(np.nanargmax(bseg))
                        rev = np.where(mseg < mm - 0.005)[0]
                        fo += [np.nanmax(bseg) - a, np.nanmin(bseg) - a, np.nanmax(mseg) - mm, np.nanmin(mseg) - mm,
                               (t[j + 1 + kb] - t[j]) / 1000.0, (t[j + 1 + rev[0]] - t[j]) / 1000.0 if len(rev) else np.nan]
                else:
                    fo += [np.nan] * 6
                # ---- one realistic order ----
                fl = ex.buy(w, sv, float(t[j]), 1.0)             # limit 1.0: arrival ask and depth, any limit later
                arr_ask = fl["arrival_ask"] if fl["arrival_ask"] is not None else np.nan
                E = [fl["latency_ms"], fl["t_known"] - t[j], arr_ask, 1.0 if fl["filled"] else 0.0, won]
                X = np.full((len(EXITS), 3), np.nan, dtype=np.float32)
                if fl["filled"]:
                    _exits(w, ex, sv, X, float(fl["entry"]), fl["t_arr"], fl["t_known"], guard, won_f,
                           _triggers(z500a, tok500, bid, fair_a, imb_a, spr_a, a - b, navail, nconf_a), fair_a)
                rows_F.append(F)
                rows_M.append(fm)
                rows_B.append(fb)
                rows_O.append(fo)
                rows_E.append(E)
                rows_X.append(X)
                keys.append((SPLITS.index(w.split) if w.split in SPLITS else -1, w.epoch, shock_no, j - i, float(t[j])))
    k = np.array(keys, dtype=np.float64).reshape(-1, 5)
    return {"asset": asset, "F": np.array(rows_F, dtype=np.float32).reshape(-1, len(FEATURES)),
            "M": np.array(rows_M, dtype=np.float32).reshape(-1, len(HORIZONS)),
            "B": np.array(rows_B, dtype=np.float32).reshape(-1, len(HORIZONS)),
            "O": np.array(rows_O, dtype=np.float32).reshape(-1, len(FWD_OTHER)),
            "E": np.array(rows_E, dtype=np.float64).reshape(-1, len(ENTRY)),
            "X": np.array(rows_X, dtype=np.float32).reshape(-1, len(EXITS), 3),
            "split": k[:, 0].astype(np.int8), "epoch": k[:, 1].astype(np.int64), "shock": k[:, 2].astype(np.int64),
            "rank": k[:, 3].astype(np.int16), "t": k[:, 4], "base": base}


def _exit_arrays(sv, dr, sg, r500, k500, rx500, mid_b, fv_b):
    """What the event exits watch, over the whole window, for a shock in direction ``dr``."""
    mid, bid, ask, fvs, adep, bdep = sv["mid"], sv["bid"], sv["ask"], sv["mfv"], sv["adep"], sv["bdep"]
    z500a = dr * r500 / (sg * math.sqrt(0.5))
    tok500 = np.where(k500 >= 0, mid - mid[np.maximum(k500, 0)], np.nan)
    fair_a = mid_b + (fvs - fv_b)
    imb_a = bdep / (bdep + adep)
    spr_a = ask - bid
    nconf_a = np.zeros(len(mid))
    navail = np.zeros(len(mid))
    for x in EXCHANGES:
        v = rx500[x]
        navail += ~np.isnan(v)
        nconf_a += np.nan_to_num(dr * v) > 0
    return z500a, tok500, fair_a, imb_a, spr_a, nconf_a, navail


def _triggers(z500a, tok500, bid, fair_a, imb_a, spr_a, entry_spread, navail, nconf_a) -> dict:
    return {"spot_reversal": np.nan_to_num(z500a, nan=0.0) < 0.0,
            "token_reversal": np.nan_to_num(tok500, nan=0.0) < 0.0,
            "momentum_collapse": np.nan_to_num(z500a, nan=0.0) < 0.5,
            "fair_reached": bid >= fair_a - 1e-9,
            "fair_exceeded": bid >= fair_a + 0.01 - 1e-9,
            "book_reversal": np.nan_to_num(imb_a, nan=0.5) <= 0.3,
            "spread_expansion": np.nan_to_num(spr_a, nan=0.0) >= max(0.03, 2.0 * entry_spread) - 1e-9,
            "xex_lost": (navail >= 2) & (nconf_a <= navail / 2.0)}


class ScaledExecution(Execution):
    """The same execution model with every latency draw scaled (stress tests and the
    faster-execution counterfactual)."""

    def __init__(self, seed: int = 0, proxy_ms: float = 500.0, size: float = SIZE, scale: float = 1.0) -> None:
        super().__init__(seed=seed, proxy_ms=proxy_ms, size=size)
        self.scale = scale

    def latency(self) -> tuple[float, float]:
        pre, half = super().latency()
        return pre * self.scale, half * self.scale


def resimulate(args) -> dict:
    """Re-run the ORDER and the exits of chosen rows under different execution: latency
    scaled by ``scale``, the venue book read ``proxy_ms`` late. Same shock context, same
    exit code as the build. ``rows``: (epoch, t, rank, is_up, row id) for one asset.
    Returns {row id: (arr_ask, arr_ok, lat_ms, {exit: (px, hold_s, flag)})}."""
    asset, rows, scale, proxy_ms, seed, exits = args
    d, meta = load_asset(CACHE, asset)
    ex = ScaledExecution(seed=seed, proxy_ms=proxy_ms, scale=scale)
    by_epoch: dict[int, list] = {}
    for r in rows:
        by_epoch.setdefault(int(r[0]), []).append(r)
    out = {}
    for m, w in zip(meta, windows(d, meta)):
        todo = by_epoch.get(int(m[1]))
        if not todo or len(w) < 30:
            continue
        s0, e0 = int(m[6]), int(m[7])
        t, sg = w.t, w.sigma
        lx = {x: np.log(np.where(d[x][s0:e0] > 0, d[x][s0:e0], np.nan)) for x in EXCHANGES}
        r500 = _ret(t, w.lp, 500.0)
        rx500 = {x: _ret(t, lx[x], 500.0) for x in EXCHANGES}
        k500, k1000 = w.lagged_index(500.0), w.lagged_index(1000.0)
        guard = w.last_at(w.close - SETTLE_GUARD_MS - 1.0)
        for (_, tj, rank, is_up, rid) in todo:
            j = int(np.searchsorted(t, tj))
            i = j - int(rank)
            dr = 1 if is_up else -1
            sv = w.side["UP" if dr > 0 else "DOWN"]
            i0 = k1000[i]
            mid_b, fv_b = float(sv["mid"][i0]), float(sv["mfv"][i0])
            won = None if w.won_up is None else float(("UP" if dr > 0 else "DOWN") == ("UP" if w.won_up else "DOWN"))
            z500a, tok500, fair_a, imb_a, spr_a, nconf_a, navail = _exit_arrays(sv, dr, sg, r500, k500, rx500,
                                                                                 mid_b, fv_b)
            fl = ex.buy(w, sv, float(t[j]), 1.0)
            X = np.full((len(EXITS), 3), np.nan, dtype=np.float32)
            if fl["filled"]:
                _exits(w, ex, sv, X, float(fl["entry"]), fl["t_arr"], fl["t_known"], guard, won,
                       _triggers(z500a, tok500, sv["bid"], fair_a, imb_a, spr_a, float(sv["ask"][j] - sv["bid"][j]),
                                 navail, nconf_a), fair_a)
            out[int(rid)] = (fl["arrival_ask"] if fl["arrival_ask"] is not None else np.nan,
                             1.0 if fl["filled"] else 0.0, fl["latency_ms"],
                             {e: tuple(float(v) for v in X[EXITS.index(e)]) for e in exits})
    return out


class ExitLab:
    """Exits simulated on demand for FILLED orders, as the live bot runs them: every
    archive point (~200 ms tick) it reads its local bid; a take-profit is a FOK sell AT
    the bid, sent only when the bid is ``tp`` above the entry and the trade clears
    ``min_net`` after both fees (live._exit_candidate); a killed exit waits 2 s
    (EXIT_RETRY_MS); a stop-loss sells through the bid (bid - 2c, retried); no exit is
    attempted in the market's last 10 s -- the position settles at 1 or 0, no fee."""

    def __init__(self, seed: int = 23, proxy_ms: float = 500.0) -> None:
        self.w = {}
        for ai, a in enumerate(ASSETS):
            d, meta = load_asset(CACHE, a)
            for m, w in zip(meta, windows(d, meta)):
                self.w[(ai, int(m[1]))] = w
        self.ex = Execution(seed=seed, proxy_ms=proxy_ms)

    def run(self, asset_idx: int, epoch: int, is_up: bool, entry: float, t_arr: float, t_known: float,
            tp: float, sl: float, max_hold_s: float, min_net: float = 0.005, retry_ms: float = 2000.0):
        """(exit price, hold seconds, flag 0 = sold / 2 = settled, kind) or None if unresolvable."""
        w = self.w[(asset_idx, epoch)]
        sv = w.side["UP" if is_up else "DOWN"]
        won = None if w.won_up is None else float(bool(is_up) == bool(w.won_up))
        bid, bdep = sv["bid"], sv["bdep"]
        fee = w.fee
        ex = self.ex
        t_end = t_arr + max_hold_s * 1000.0 if max_hold_s > 0 else math.inf
        last_exit = -math.inf
        j = w.first_at(t_known)
        while j < len(w):
            tj = float(w.t[j])
            if tj >= w.close - SETTLE_GUARD_MS:
                return None if won is None else (won, (w.close - t_arr) / 1000.0, 2.0, "SETTLE")
            if tj >= t_end:
                break
            b = bid[j]
            if b == b and tj - last_exit >= retry_ms:
                if tp > 0 and b - entry >= tp - 1e-9 and b - entry - fee(entry) - fee(b) >= min_net - 1e-9:
                    last_exit = tj
                    pre, half = ex.latency()
                    t_s = tj + pre + half
                    js = w.first_at(t_s + ex.proxy_ms)
                    if js < len(w) and w.t[js] < w.close and bid[js] >= b - 1e-9 and (bdep[js] or 0.0) >= SIZE:
                        return float(bid[js]), (t_s - t_arr) / 1000.0, 0.0, "TAKE_PROFIT"
                elif sl > 0 and entry - b >= sl - 1e-9:
                    px, how, t_out, _ = ex.sell(w, sv, tj, float(b) - 0.02, won)
                    if px is None:
                        return None
                    return px, (t_out - t_arr) / 1000.0, 2.0 if how == "SETTLE" else 0.0, "STOP_LOSS"
            j += 1
        if t_end == math.inf:
            return None if won is None else (won, (w.close - t_arr) / 1000.0, 2.0, "SETTLE")
        px, how, t_out, _ = _sell(w, ex, sv, t_end, won)
        if px is None:
            return None
        return px, (t_out - t_arr) / 1000.0, 2.0 if how == "SETTLE" else 0.0, "TIME_STOP"


def spotlag_signals(args) -> dict:
    """SPOT-LAG signals for the settings study (scripts/analyze_repriced_momentum.py slsim).

    Every point where the live spot-lag rule fires (detect_shocks: 1 s move >= ``k``
    sigma, 3 s agrees, accelerating, one per direction per 2 s -- exactly the engine's
    signal) with what the engine prices it from: the side's ask/bid/depth, the expected
    price ev = mid 1 s ago + (MFV now - MFV then), the fee rate. One order per signal is
    simulated at an unlimited limit (the venue ask on arrival and whether 5 shares were
    there), so ANY price cap is evaluated exactly: filled iff arrival ask <= cap. Exits
    (independent of the entry price): the live rule (5 s, held to 10 s while the 1 s spot
    move is >= 0.5 sigma our way) and take-profit variants."""
    asset, k = args
    d, meta = load_asset(CACHE, asset)
    ex = Execution(seed=sum(map(ord, asset)) + int(k * 10))
    rows = []
    TPS = ((0.05, 10.0), (0.10, 20.0), (0.05, 20.0))
    for m, w in zip(meta, windows(d, meta)):
        if len(w) < 30:
            continue
        r1 = _ret(w.t, w.lp, 1000.0)
        k1000 = w.lagged_index(1000.0)
        for i, dr in detect_shocks(w, k=k):
            side = "UP" if dr > 0 else "DOWN"
            sv = w.side[side]
            i0 = k1000[i]
            if i0 < 0 or w.t[i] - w.t[i0] > 1600.0:
                continue
            mid, fvs, ask, bid = sv["mid"], sv["mfv"], sv["ask"], sv["bid"]
            if mid[i0] != mid[i0] or fvs[i0] != fvs[i0] or ask[i] != ask[i] or bid[i] != bid[i] or fvs[i] != fvs[i]:
                continue
            ev = min(max(float(mid[i0] + (fvs[i] - fvs[i0])), 0.0), 1.0)
            won = None if w.won_up is None else float(side == ("UP" if w.won_up else "DOWN"))
            fl = ex.buy(w, sv, float(w.t[i]), 1.0)
            X = [np.nan] * (3 * (1 + len(TPS)))
            if fl["filled"]:
                entry = float(fl["entry"])
                z1s = dr * r1 / w.sigma
                j = w.first_at(fl["t_known"])
                t_sell = None
                while j < len(w) and w.t[j] < w.close - SETTLE_GUARD_MS:
                    held = (w.t[j] - fl["t_arr"]) / 1000.0
                    if held >= 5.0 and not (held < 10.0 and z1s[j] == z1s[j] and z1s[j] >= 0.5):
                        t_sell = float(w.t[j])
                        break
                    j += 1
                if t_sell is None:
                    if won is not None:
                        X[0:3] = [won, (w.close - fl["t_arr"]) / 1000.0, 2.0]
                else:
                    jd = w.last_at(t_sell)
                    px, how, t_out, _ = ex.sell(w, sv, t_sell, float(bid[jd]) - 0.02, won)
                    if px is not None:
                        X[0:3] = [px, (t_out - fl["t_arr"]) / 1000.0, 2.0 if how == "SETTLE" else 0.0]
                for n, (tp, mh) in enumerate(TPS):
                    got = _tp_exit(w, sv, ex, entry, fl["t_arr"], fl["t_known"], tp, 0.0, mh, won)
                    if got is not None:
                        X[3 * (n + 1):3 * (n + 2)] = list(got[:3])
            rows.append([SPLITS.index(w.split) if w.split in SPLITS else -1, w.epoch, ASSETS.index(asset), float(w.t[i]),
                         dr, float(ask[i]), float(bid[i]), ev, w.rate, float(dr * r1[i] / w.sigma[i]), float(w.left[i]),
                         float(sv["adep"][i] or 0.0), fl["latency_ms"], fl["t_known"] - w.t[i],
                         fl["arrival_ask"] if fl["arrival_ask"] is not None else np.nan, 1.0 if fl["filled"] else 0.0] + X)
    return {"asset": asset, "rows": np.array(rows, dtype=np.float64)}


SL_COLS = ("split", "epoch", "asset", "t", "dir", "ask", "bid", "ev", "rate", "z1", "left", "adep", "lat_ms", "known_ms",
           "arr_ask", "arr_ok") + tuple(f"{e}_{c}" for e in ("live", "tp5_10", "tp10_20", "tp5_20") for c in ("px", "hold", "flag"))


def _tp_exit(w, sv, ex, entry, t_arr, t_known, tp, sl, max_hold_s, won, min_net=0.005, retry_ms=2000.0):
    """The live bot's take-profit / stop-loss / time-stop exit, tick by tick (ExitLab.run)."""
    bid, bdep = sv["bid"], sv["bdep"]
    fee = w.fee
    t_end = t_arr + max_hold_s * 1000.0 if max_hold_s > 0 else math.inf
    last_exit = -math.inf
    j = w.first_at(t_known)
    while j < len(w):
        tj = float(w.t[j])
        if tj >= w.close - SETTLE_GUARD_MS:
            return None if won is None else (won, (w.close - t_arr) / 1000.0, 2.0, "SETTLE")
        if tj >= t_end:
            break
        b = bid[j]
        if b == b and tj - last_exit >= retry_ms:
            if tp > 0 and b - entry >= tp - 1e-9 and b - entry - fee(entry) - fee(b) >= min_net - 1e-9:
                last_exit = tj
                pre, half = ex.latency()
                t_s = tj + pre + half
                js = w.first_at(t_s + ex.proxy_ms)
                if js < len(w) and w.t[js] < w.close and bid[js] >= b - 1e-9 and (bdep[js] or 0.0) >= SIZE:
                    return float(bid[js]), (t_s - t_arr) / 1000.0, 0.0, "TAKE_PROFIT"
            elif sl > 0 and entry - b >= sl - 1e-9:
                px, how, t_out, _ = ex.sell(w, sv, tj, float(b) - 0.02, won)
                if px is None:
                    return None
                return px, (t_out - t_arr) / 1000.0, 2.0 if how == "SETTLE" else 0.0, "STOP_LOSS"
        j += 1
    if t_end == math.inf:
        return None if won is None else (won, (w.close - t_arr) / 1000.0, 2.0, "SETTLE")
    px, how, t_out, _ = _sell(w, ex, sv, t_end, won)
    if px is None:
        return None
    return px, (t_out - t_arr) / 1000.0, 2.0 if how == "SETTLE" else 0.0, "TIME_STOP"


def build_spotlag_signals(k: float, procs: int = 3) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(procs) as pool:
        parts = list(pool.map(spotlag_signals, [(a, k) for a in ASSETS]))
    rows = np.concatenate([p["rows"] for p in parts if len(p["rows"])])
    out = OUT / f"spotlag_signals_k{k:g}.npz"
    np.savez_compressed(out, rows=rows, cols=np.array(SL_COLS))
    return out


def _sell(w, ex, sv, t_dec, won):
    bid = sv["bid"]
    jd = w.last_at(t_dec)
    b = float(bid[jd]) if jd >= 0 and bid[jd] == bid[jd] else np.nan
    floor = (b - 0.02) if b == b else 0.0
    return ex.sell(w, sv, t_dec, floor, won)


def _put(X, n, px, how, t_out, t_arr):
    if px is None:
        return
    X[n] = (px, (t_out - t_arr) / 1000.0, {"SOLD": 0.0, "MAKER": 1.0, "SETTLE": 2.0}.get(how, 0.0))


def _exits(w, ex, sv, X, entry, t_arr, t_known, guard, won, trig, fair_a):
    """Fill the 22 exit outcomes of a position bought at ``entry`` arriving at ``t_arr``."""
    bid, bdep = sv["bid"], sv["bdep"]
    n = 0
    for h in TIMERS:
        px, how, t_out, _ = _sell(w, ex, sv, max(t_known, t_arr + h * 1000.0), won)
        _put(X, n, px, how, t_out, t_arr)
        n += 1
    j_from = w.first_at(t_known)
    j_to = min(w.last_at(t_arr + EVENT_MAX_S * 1000.0), guard)
    for name in EVENTS:
        seg = trig[name][j_from:j_to + 1] & ~np.isnan(bid[j_from:j_to + 1])
        if len(seg) and seg.any():
            jj = j_from + int(np.argmax(seg))
            take = name in ("fair_reached", "fair_exceeded")
            px, how, t_out, _ = ex.sell(w, sv, float(w.t[jj]), float(bid[jj]) - (0.0 if take else 0.02), won)
        else:
            px, how, t_out, _ = _sell(w, ex, sv, max(t_known, t_arr + EVENT_MAX_S * 1000.0), won)
        _put(X, n, px, how, t_out, t_arr)
        n += 1
    # MAKER targets: a resting sell placed once the fill is known; filled only when the
    # venue's bid trades THROUGH it (a buyer above our price must have crossed us); no fee
    pre, half = ex.latency()
    t_place = t_known + pre + half
    k0 = w.first_at(t_place + ex.proxy_ms)
    k1 = min(w.last_at(t_arr + MAKER_MAX_S * 1000.0 + ex.proxy_ms), guard)
    jf = w.last_at(t_known)
    fair_now = float(fair_a[jf]) if jf >= 0 and fair_a[jf] == fair_a[jf] else entry + 0.01
    for tgt in (entry + 0.01, entry + 0.02, entry + 0.03, max(entry + 0.01, math.ceil(fair_now * 100 - 1e-6) / 100)):
        tgt = round(tgt, 2)
        filled = False
        if k0 <= k1 and tgt < 1.0:
            hit = (bid[k0:k1 + 1] >= tgt + 0.01 - 1e-9) & (np.nan_to_num(bdep[k0:k1 + 1]) >= SIZE)
            if hit.any():
                kf = k0 + int(np.argmax(hit))
                X[n] = (tgt, (float(w.t[kf]) - ex.proxy_ms - t_arr) / 1000.0, 1.0)
                filled = True
        if not filled:
            px, how, t_out, _ = _sell(w, ex, sv, max(t_known, t_arr + MAKER_MAX_S * 1000.0), won)
            _put(X, n, px, how, t_out, t_arr)
        n += 1


def _spot_lag_baseline(w, ex, sv, i, dr, r1, mid_b, fv_b, won, asset):
    """Baseline 4: the spot-lag live rule at the shock with its own exit (5 s, held on
    to 10 s while the spot is still moving >= 0.5 sigma our way). HYPE excluded, as live."""
    ask, bid, fvs = sv["ask"], sv["bid"], sv["mfv"]
    rec = {"split": w.split, "epoch": w.epoch, "asset": asset, "order": False}
    if asset == "HYPE" or ask[i] != ask[i] or bid[i] != bid[i] or not (0.05 <= ask[i] <= 0.95) \
            or ask[i] - bid[i] > 0.03 + 1e-9:
        return rec
    ev = mid_b + (fvs[i] - fv_b)
    a0 = float(ask[i])
    fixed = 0.01 + w.fee(min(max(ev, .01), .99)) + float(ask[i] - bid[i]) / 2.0
    if not (ev - a0 > 0 and ev - a0 - w.fee(a0) - fixed >= 0.02):
        return rec
    budget = ev - fixed - 0.02
    p = math.floor((min(budget, 0.99) + 1e-9) * 100) / 100
    me = None
    while p >= 0.01:
        if p + w.fee(p) <= budget + 1e-12:
            me = round(p, 2)
            break
        p = round(p - 0.01, 2)
    if me is None or me < a0 - 1e-9:
        return rec
    rec["order"] = True
    fl = ex.buy(w, sv, float(w.t[i]), min(a0 + 0.02, me))
    rec["filled"] = fl["filled"]
    if fl["filled"]:
        z1s = dr * r1 / w.sigma
        j = w.first_at(fl["t_known"])
        t_sell = None
        while j < len(w) and w.t[j] < w.close - SETTLE_GUARD_MS:
            held = (w.t[j] - fl["t_arr"]) / 1000.0
            if held >= 5.0 and not (held < 10.0 and z1s[j] == z1s[j] and z1s[j] >= 0.5):
                t_sell = float(w.t[j])
                break
            j += 1
        if t_sell is None:
            px, how = (won, "SETTLE") if won is not None else (None, "UNKNOWN")
        else:
            jd = w.last_at(t_sell)
            px, how, _, _ = ex.sell(w, sv, t_sell, float(bid[jd]) - 0.02, won)
        if px is not None:
            rec["net"] = px - fl["entry"] - w.fee(fl["entry"]) - (0.0 if how == "SETTLE" else w.fee(px))
    return rec


def build(set_name: str, procs: int = 3) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(procs) as pool:
        parts = list(pool.map(build_asset, [(a, set_name) for a in ASSETS]))
    keep = [p for p in parts if len(p["F"])]
    asset = np.concatenate([np.full(len(p["F"]), ASSETS.index(p["asset"]), dtype=np.int8) for p in keep])
    shock = np.concatenate([p["shock"] + 10_000_000 * ASSETS.index(p["asset"]) for p in keep])
    out = OUT / f"dataset_{set_name}.npz"
    np.savez_compressed(out, F=np.concatenate([p["F"] for p in keep]), M=np.concatenate([p["M"] for p in keep]),
                        B=np.concatenate([p["B"] for p in keep]), O=np.concatenate([p["O"] for p in keep]),
                        E=np.concatenate([p["E"] for p in keep]), X=np.concatenate([p["X"] for p in keep]),
                        split=np.concatenate([p["split"] for p in keep]),
                        epoch=np.concatenate([p["epoch"] for p in keep]), shock=shock, asset=asset,
                        rank=np.concatenate([p["rank"] for p in keep]), t=np.concatenate([p["t"] for p in keep]),
                        features=np.array(FEATURES), exits=np.array(EXITS), horizons=np.array(HORIZONS))
    if set_name == "spotlag":
        (OUT / "baseline_spotlag.json").write_text(json.dumps([b for p in parts for b in p["base"]]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cache")
    sl = sub.add_parser("spotlag")
    sl.add_argument("--k", type=float, nargs="+", default=[3.0, 2.5])
    b = sub.add_parser("build")
    b.add_argument("--shocks", choices=("spotlag", "broad", "both"), default="both")
    b.add_argument("--procs", type=int, default=3)
    args = ap.parse_args()
    if args.cmd == "cache":
        print(build_features(ROOT / "data/charts", CACHE, procs=3, split_fn=split_fn))
        print((CACHE / "splits.json").read_text())
    elif args.cmd == "spotlag":
        for k in args.k:
            print("built", build_spotlag_signals(k), flush=True)
    elif args.cmd == "build":
        for s in (("spotlag", "broad") if args.shocks == "both" else (args.shocks,)):
            print("built", build(s, args.procs), flush=True)


if __name__ == "__main__":
    main()
