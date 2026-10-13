"""Research: the 60-SECOND SNIPE WINDOW -- the final minutes of a 5-minute market.

    python scripts/research_60s_snipe.py cache                     # per-point features from data/charts
    python scripts/research_60s_snipe.py sweep  [--stage coarse|fine|all]
    python scripts/research_60s_snipe.py select                    # TRAIN -> VALIDATION -> LOCK
    python scripts/research_60s_snipe.py evaluate                  # locked candidates + baselines, every period

RESEARCH ONLY. It reads the chart archive and writes under data/replay/snipe60/ and results/.
It changes no strategy code, no configuration and no running process, and places no orders.

Hypothesis
----------
In the last ~60 s the UP/DOWN token may stray from the probability implied by the spot, the
strike, the time left and short-term volatility. Buy the token whose EXECUTABLE edge
(model probability - ask - taker fee - slippage allowance) clears a threshold.

What settles the market (verified on the archive before anything else was built)
------------------------------------------------------------------------------------
The venue resolves Up iff the 60 s TWAP at the close >= the 60 s TWAP at the open
(pricing/twap.py). Against 3,254 venue outcomes the TWAP-vs-TWAP rule on our composite spot
agrees 98.3 % of the time (Binance alone 98.8 %); spot-vs-spot agrees 88.9 %. In the 330
windows where the two rules disagree, TWAP is right 318 times. So inside the last minute part
of the settling average is already LOCKED, and the residual ~1.5 % is the basis between our
composite and Chainlink's stream.

Features (causal; every value at a point uses only data stamped at or before it)
-----------------------------------------------------------------------------------
Per archive point (~216 ms apart) with <= 182 s left, both tokens' touch and depth, and:
    sigma      TwoScaleVol per-second sigma (VolConfig), stitched over each asset's windows
    mu         E[settling log-TWAP] - ln K, from the observed part of the averaging window and
               the latest spot, with the measured 0.18 s information lag (twap_fair_value)
    sd         sqrt(twap_variance(u, sigma)), u = seconds left + 0.18
    p_d        the PROJECT model: Student-t(4) digital on mu / sd (twap_fair_value, vectorised;
               tests/test_snipe60_research.py checks it against the live function)
    lzs        ln(spot / K): the naive distance
    r<L>       spot log return over L = 250 ms .. 10 s, NaN without a point that old
    rv30/rv60  realised per-second vol from 1 s returns over the last 30 / 60 s
    r60/r120   the trend: spot log return over the last 60 / 120 s
    du1/du3    the Up mid's change over the last 1 / 3 s;  dp1/dp3 the model's change
The strike K is the live bot's own (60 s TWAP at the open, recorded in the chart file).
The window's settlement proxy dclose = ln(our TWAP at the close / K) is stored for FITTING the
basis-noise model on TRAINING windows only; it is never a decision input.

Execution (backtest/spot_shock_replay.Execution, the model validated on venue books)
-------------------------------------------------------------------------------------
An order leaves presend ms after the decision and reaches the venue RTT/2 later, drawn from
the MEASURED priors; the venue's book at time T is read as the archived local book at
T + 500 ms; buys are FOK at a limit (filled at the arrival ask if it is <= the limit with 5
shares behind it; no partial fills); sells are FOK at a floor (bid - 2c, or the bid for a
take-profit), retried every 2 s; a position still open when an exit is due inside the last
10 s settles 1/0 with no exit fee. One change for this study: the existing model refuses any
BUY landing in the last 10 s (it was built for strategies that stop at 60 s). Here a buy fills
as long as its proxied venue book point is before the close -- the whole question is the
final seconds. The 10 s refusal is kept as a sensitivity (``buy_guard_ms``).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.special import ndtr, stdtr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from troll_poly_bot.backtest.momentum_replay import load_window  # noqa: E402
from troll_poly_bot.backtest.spot_shock_replay import INFO_LAG_S, _make_vol  # noqa: E402
from troll_poly_bot.execution.latency import PRESEND_PRIOR_MS, VENUE_RTT_PRIOR_MS  # noqa: E402
from troll_poly_bot.pricing.twap import TWAP_WINDOW_S  # noqa: E402
import research_repriced_momentum as R  # noqa: E402

OUT = ROOT / "data/replay/snipe60"
CACHE = OUT / "cache"
ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
#: the established research periods (docs/repriced_momentum_research_2026-09-24.md), reused exactly:
#: train / validation / unseen_1 / unseen_2 as split by the first spot-lag study, unseen_3 = every
#: window after FRESH_AFTER up to the last one in the shared cache (09-25 00:45 UTC). Windows
#: archived after that arrived while this study ran: FORWARD, never examined by any study.
SPLITS = ("train", "validation", "unseen_1", "unseen_2", "unseen_3", "forward")
UNSEEN3_LAST = 1790297100
KEEP_LEFT_S = 182.0
RET_LAGS_MS = (250, 500, 1000, 2000, 3000, 5000, 10000)
NU = 4.0
T_SCALE = math.sqrt(NU / (NU - 2.0))
COLS = ("t", "left", "spot", "sigma", "mu", "sd", "p_d", "lzs",
        "r250", "r500", "r1000", "r2000", "r3000", "r5000", "r10000", "rv30", "rv60", "r60", "r120",
        "du1", "du3", "dp1", "dp3",
        "ub", "ua", "ubd", "uad", "db", "da", "dbd", "dad", "tfi", "obi", "orate")


def split_fn(epochs) -> dict:
    base = R.split_fn([e for e in set(epochs) if e <= UNSEEN3_LAST])
    return {e: base.get(e, "forward") for e in set(epochs)}


def t4_cdf(z):
    """The project pricer: Student-t(4) standardised to unit variance (pricing/digital.py)."""
    return stdtr(NU, np.asarray(z, dtype=np.float64) * T_SCALE)


# ─────────────────────────────── TWAP state, vectorised ───────────────────────────────


def _cum_trapezoid(t: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Integral of the linear interpolant of x from t[0] to each t[k], in value * seconds."""
    return np.concatenate([[0.0], np.cumsum(0.5 * (x[1:] + x[:-1]) * np.diff(t) / 1000.0)])


def _integral_to(t, x, cum, b):
    """Integral from t[0] to each b, linear between samples; b must lie in [t[0], t[-1]]."""
    j = np.clip(np.searchsorted(t, b, side="right") - 1, 0, len(t) - 2)
    span = t[j + 1] - t[j]
    fb = x[j] + (x[j + 1] - x[j]) * np.where(span > 0, (b - t[j]) / np.where(span > 0, span, 1.0), 0.0)
    return cum[j] + 0.5 * (x[j] + fb) * (b - t[j]) / 1000.0


def twap_mean(t: np.ndarray, x: np.ndarray, close_ms: float, idx: np.ndarray,
              info_lag_s: float = INFO_LAG_S, window_s: float = TWAP_WINDOW_S):
    """(E[settling log-TWAP], u) at each point ``idx``, exactly as twap_fair_value computes it
    from a TwapState holding the samples up to that point: the observed integral runs from the
    averaging window's start to ``now - info_lag`` and the rest is weighted at the latest price."""
    cum = _cum_trapezoid(t, x)
    now = t[idx]
    u = np.maximum((close_ms - now) / 1000.0, 0.0) + info_lag_s
    mean = x[idx].copy()
    inside = u < window_s
    if inside.any():
        a = close_ms - window_s * 1000.0
        seen = now[inside] - info_lag_s * 1000.0
        integral = _integral_to(t, x, cum, seen) - _integral_to(t, x, cum, np.full(len(seen), a))
        mean[inside] = (integral + u[inside] * x[idx][inside]) / window_s
    return mean, u


def twap_var(u: np.ndarray, sigma: np.ndarray, window_s: float = TWAP_WINDOW_S) -> np.ndarray:
    s2 = sigma * sigma
    return np.where(u >= window_s, s2 * (u - 2.0 * window_s / 3.0), s2 * u ** 3 / (3.0 * window_s * window_s))


def causal_diff(t: np.ndarray, x: np.ndarray, lag_ms: float, idx: np.ndarray, slack_ms: float = 1500.0):
    """x[i] - x[the last point at or before t_i - lag], NaN without a point that old (or a gap)."""
    j = np.searchsorted(t, t[idx] - lag_ms, side="right") - 1
    ok = (j >= 0) & (t[idx] - t[np.maximum(j, 0)] <= lag_ms + slack_ms)
    return np.where(ok, x[idx] - x[np.maximum(j, 0)], np.nan)


def realised_vol(t: np.ndarray, x: np.ndarray, idx: np.ndarray, span_s: int) -> np.ndarray:
    """Per-second vol from 1 s returns over the last ``span_s`` seconds (last value at or before)."""
    grid = t[idx][:, None] - 1000.0 * np.arange(span_s + 1)[None, :]
    j = np.searchsorted(t, grid, side="right") - 1
    ok = j[:, -1] >= 0
    v = x[np.maximum(j, 0)]
    r = np.diff(v[:, ::-1], axis=1)
    return np.where(ok, np.sqrt(np.mean(r * r, axis=1)), np.nan)


# ─────────────────────────────── the cache ───────────────────────────────


def window_sigma(t_all: np.ndarray, spot_all: np.ndarray, vol, last_t: float):
    """Feed one window's points to the asset's stitched TwoScaleVol in time order. Returns
    (sigma after each point -- NaN where the point was not an update or the estimator is not
    warm --, the new last timestamp)."""
    good = (spot_all > 0) & ~np.isnan(spot_all)
    sig = np.full(len(t_all), np.nan)
    for i in np.where(good)[0]:
        ti = t_all[i]
        if ti > last_t:
            vol.update(float(spot_all[i]), ti)
            last_t = ti
            if vol.ready:
                sig[i] = vol.sigma_per_sec
    return sig, last_t


def window_features(w, sig_all: np.ndarray):
    """Causal features of one recorded window (``momentum_replay.Window``) at every point with
    <= KEEP_LEFT_S left: ({column: array}, dclose), or None. ``sig_all`` is the stitched sigma
    at each of the window's points (``window_sigma``). dclose (our TWAP over the last minute vs
    the strike) is the basis model's fitting target and is not a feature."""
    t_all = np.asarray(w.t, dtype=np.float64)
    spot_all = np.asarray(w.cols["spot"], dtype=np.float64)
    good = (spot_all > 0) & ~np.isnan(spot_all)
    if w.strike <= 0:
        return None
    # the window's own samples, strictly increasing in time (a TwapState skips repeats)
    g = np.where(good)[0]
    g = g[np.concatenate([[True], np.diff(t_all[g]) > 0])]
    t, x = t_all[g], np.log(spot_all[g])
    sig_g = sig_all[g]
    left_g = (w.close_ts - t) / 1000.0
    need = np.where((left_g > 0.0) & (left_g <= KEEP_LEFT_S + 3.5))[0]    # +3.5 s: the 3 s lags
    need = need[need >= 1]
    if len(need) < 30:
        return None
    lk = math.log(w.strike)
    mean, u = twap_mean(t, x, w.close_ts, need)
    sd = np.sqrt(np.maximum(twap_var(u, sig_g[need]), 1e-18))
    p = t4_cdf((mean - lk) / sd)
    # the token's Up mid on the same (deduplicated) points, NaN if one-sided
    ub_g, ua_g = w.cols["ub"][g], w.cols["ua"][g]
    umid = np.where(~np.isnan(ub_g) & ~np.isnan(ua_g), (ub_g + ua_g) / 2.0, np.nan)
    p_full = np.full(len(t), np.nan)
    p_full[need] = p
    store = need[(left_g[need] <= KEEP_LEFT_S) & ~np.isnan(sig_g[need])]
    if len(store) < 30:
        return None
    pos = np.searchsorted(need, store)
    cols = {
        "t": t[store], "left": left_g[store], "spot": np.exp(x[store]), "sigma": sig_g[store],
        "mu": mean[pos] - lk, "sd": sd[pos], "p_d": p[pos], "lzs": x[store] - lk,
        "rv30": realised_vol(t, x, store, 30), "rv60": realised_vol(t, x, store, 60),
        "r60": causal_diff(t, x, 60_000.0, store, 5000.0), "r120": causal_diff(t, x, 120_000.0, store, 5000.0),
        "du1": causal_diff(t, umid, 1000.0, store), "du3": causal_diff(t, umid, 3000.0, store),
        "dp1": causal_diff(t, p_full, 1000.0, store), "dp3": causal_diff(t, p_full, 3000.0, store),
    }
    for lag in RET_LAGS_MS:
        cols[f"r{lag}"] = causal_diff(t, x, float(lag), store)
    for k in ("ub", "ua", "ubd", "uad", "db", "da", "dbd", "dad", "tfi", "obi", "orate"):
        cols[k] = np.asarray(w.cols[k], dtype=np.float64)[g][store]
    a = w.close_ts - 60_000.0
    dclose = np.nan
    if ((t >= a) & (t <= w.close_ts)).sum() >= 20 and t[0] <= a:
        cum = _cum_trapezoid(t, x)
        b = min(w.close_ts, float(t[-1]))
        integ = _integral_to(t, x, cum, np.array([b])) - _integral_to(t, x, cum, np.array([a]))
        dclose = float(integ[0] / ((b - a) / 1000.0)) - lk
    return cols, dclose


def _build_asset(args) -> tuple[str, int, int]:
    asset, paths, split, out_dir = args
    ws = [w for w in (load_window(p) for p in paths) if w is not None]
    ws.sort(key=lambda w: w.open_ts)
    out = {k: [] for k in COLS}
    meta = []
    n_rows = 0
    vol, last_t = _make_vol(), -1e18
    for w in ws:
        if w.t[0] - last_t > 120_000:
            vol, last_t = _make_vol(), -1e18
        sig, last_t = window_sigma(np.asarray(w.t, dtype=np.float64), np.asarray(w.cols["spot"], dtype=np.float64),
                                   vol, last_t)
        got = window_features(w, sig)
        if got is None:
            continue
        cols, dclose = got
        for k in COLS:
            out[k].append(cols[k])
        n = len(cols["t"])
        meta.append((w.slug, int(w.epoch), split.get(int(w.epoch), "?"), float(w.open_ts), float(w.close_ts),
                     -1 if w.up_won is None else int(w.up_won), n_rows, n_rows + n, float(w.fee.rate),
                     float(w.strike), dclose))
        n_rows += n
    arrays = {k: np.concatenate(v) if v else np.zeros(0) for k, v in out.items()}
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_dir / f"{asset}.npz", **arrays, meta=np.array(meta, dtype=object))
    return asset, len(meta), n_rows


def build_cache(charts_dir: Path = ROOT / "data/charts", out_dir: Path = CACHE, procs: int = 3) -> dict:
    charts = sorted(Path(charts_dir).glob("*.json"))
    epochs = [int(p.stem.rsplit("-", 1)[-1]) for p in charts]
    split = split_fn(epochs)
    by: dict[str, list] = {}
    for p in charts:
        by.setdefault(p.stem.split("-")[0].upper(), []).append(p)
    res = {}
    with ProcessPoolExecutor(procs) as pool:
        for asset, nw, npts in pool.map(_build_asset, [(a, ps, split, Path(out_dir)) for a, ps in sorted(by.items())]):
            res[asset] = (nw, npts)
            print(f"  {asset}: {nw} windows, {npts} points", flush=True)
    eps = {}
    for e, s in split.items():
        eps.setdefault(s, []).append(e)
    (Path(out_dir) / "splits.json").write_text(json.dumps({s: [min(v), max(v), len(v)] for s, v in eps.items()}))
    return res


# ─────────────────────────────── loading ───────────────────────────────


class Data:
    """Every cached point of every window, concatenated (asset-major, then time).

    Prices are also kept in integer CENTS (``*_c``, -1 where the book had no quote): the
    repriced-momentum study lost its fill rates to float32 prices (0.88 read back as
    0.8799999952), so every price comparison here is on the integer grid."""

    def __init__(self, cache_dir: Path = CACHE, assets=ASSETS) -> None:
        parts = {k: [] for k in COLS}
        wm = []
        n = 0
        for ai, a in enumerate(assets):
            z = np.load(Path(cache_dir) / f"{a}.npz", allow_pickle=True)
            for k in COLS:
                parts[k].append(z[k])
            for slug, ep, sp, o, c, won, s, e, rate, strike, dclose in z["meta"]:
                wm.append((str(slug), ai, int(ep), SPLITS.index(sp) if sp in SPLITS else -1, float(o), float(c),
                           int(won), int(s) + n, int(e) + n, float(rate), float(strike), float(dclose)))
            n += len(z["t"])
        self.assets = tuple(assets)
        self.c = {k: np.concatenate(v) for k, v in parts.items()}
        self.n = n
        self.w_slug = [m[0] for m in wm]
        (self.w_asset, self.w_epoch, self.w_split, self.w_open, self.w_close, self.w_won, self.w_start, self.w_end,
         self.w_rate, self.w_strike, self.w_dclose) = (
            np.array([m[k] for m in wm], dtype=dt) for k, dt in
            ((1, np.int8), (2, np.int64), (3, np.int8), (4, np.float64), (5, np.float64), (6, np.int8),
             (7, np.int64), (8, np.int64), (9, np.float64), (10, np.float64), (11, np.float64)))
        self.W = len(wm)
        self.win = np.repeat(np.arange(self.W), self.w_end - self.w_start)
        c = self.c
        for k in ("ub", "ua", "db", "da"):
            c[k + "_c"] = np.where(np.isnan(c[k]), -1, np.rint(c[k] * 100.0)).astype(np.int16)
        for k in ("ubd", "uad", "dbd", "dad"):
            c[k] = np.nan_to_num(c[k], nan=0.0)
        # a globally sorted key: window-major, then time (t - close is within (-182 s, 0])
        self.key = self.win.astype(np.float64) * 1e9 + (c["t"] - self.w_close[self.win] + 5e5)
        self.split = self.w_split[self.win]
        self.asset = self.w_asset[self.win]
        self.epoch = self.w_epoch[self.win]
        self.won = self.w_won[self.win]
        self.rate = self.w_rate[self.win]

    def side(self, s: int) -> dict:
        """The arrays one token is traded on; ``s`` = +1 UP, -1 DOWN."""
        c = self.c
        if s > 0:
            return {"bid": c["ub_c"], "ask": c["ua_c"], "bdep": c["ubd"], "adep": c["uad"]}
        return {"bid": c["db_c"], "ask": c["da_c"], "bdep": c["dbd"], "adep": c["dad"]}

    def first_at_or_after(self, w: np.ndarray, t_ms: np.ndarray) -> np.ndarray:
        """Global index of the first point of window ``w`` at or after ``t_ms`` (= w_end if none)."""
        return np.searchsorted(self.key, w.astype(np.float64) * 1e9 + (t_ms - self.w_close[w] + 5e5))

    def last_at_or_before(self, w: np.ndarray, t_ms: np.ndarray) -> np.ndarray:
        """Global index of the last point of window ``w`` at or before ``t_ms`` (< w_start if none)."""
        return np.searchsorted(self.key, w.astype(np.float64) * 1e9 + (t_ms - self.w_close[w] + 5e5),
                               side="right") - 1


# ─────────────────────────────── fair-value models ───────────────────────────────
#
#   A  spot vs strike only             1 if spot >= K else 0 (a rule, not a probability)
#   B  naive spot digital               Phi(ln(S/K) / (sigma sqrt(left + lag))): settles on spot, no TWAP
#   D  the PROJECT model                twap_fair_value: Student-t(4) on the settling-TWAP distance
#   E  D + settlement basis             the Chainlink-vs-our-composite noise at the close, sd = k sigma,
#                                       k fitted on TRAINING windows (one number, all assets)
#   C  historical empirical             P(Up | seconds-left group, E's z bin): TRAINING frequencies,
#                                       shrunk toward E (the repo's CalibratedPricer idea)
#   F  logistic recalibration           per seconds-left group: logit(E), spot momentum at 1/5/10 s,
#                                       the Up token's 1 s move, the vol surprise, asset effects
# Fitted models are CROSS-FITTED inside TRAINING (each half predicted by a fit on the other half),
# so TRAINING results are out-of-fold too; every later period uses the fit on all of TRAINING.

MODELS = ("d", "e", "c", "f")
ZBINS = np.array([-np.inf, -5, -4, -3, -2.5, -2, -1.5, -1.25, -1, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75, 1,
                  1.25, 1.5, 2, 2.5, 3, 4, 5, np.inf])
LEFT_EDGES = np.array([10.0, 30.0, 60.0, 120.0])          # groups (0,10] (10,30] (30,60] (60,120] (120,180]
F_FEATURES = ("lp", "z1", "z5", "z10", "du1", "vs")


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def nll(p, y) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(np.where(y, np.log(p), np.log(1 - p))))


def left_group(left):
    return np.searchsorted(LEFT_EDGES, left, side="left")


def fit_basis(D: Data, wmask: np.ndarray) -> float:
    """k in basis sd = k * sigma: maximum likelihood of the venue outcome given our own TWAP
    distance at the close, dclose, under the project's t(4) link, on the windows in ``wmask``."""
    sig = D.c["sigma"][D.w_end - 1]
    m = wmask & (D.w_won >= 0) & ~np.isnan(D.w_dclose)
    d, y, s = D.w_dclose[m], D.w_won[m] == 1, sig[m]
    grid = np.arange(0.05, 6.0, 0.05)
    return float(grid[int(np.argmin([nll(t4_cdf(d / (k * s)), y) for k in grid]))])


def p_e(D: Data, k: float, idx=slice(None)):
    c = D.c
    return t4_cdf(c["mu"][idx] / np.sqrt(c["sd"][idx] ** 2 + (k * c["sigma"][idx]) ** 2))


def f_design(D: Data, pe: np.ndarray, idx) -> np.ndarray:
    c = D.c
    sg = c["sigma"][idx]
    z1 = np.nan_to_num(c["r1000"][idx] / sg)
    z5 = np.nan_to_num(c["r5000"][idx] / (sg * math.sqrt(5)))
    z10 = np.nan_to_num(c["r10000"][idx] / (sg * math.sqrt(10)))
    du1 = np.nan_to_num(c["du1"][idx])
    vs = np.nan_to_num(np.clip(c["rv60"][idx] / sg - 1.0, -1, 3))
    ast = np.eye(len(D.assets))[D.asset[idx]][:, 1:]
    return np.column_stack([np.ones(len(pe)), _logit(pe), np.clip(z1, -8, 8), np.clip(z5, -8, 8),
                            np.clip(z10, -8, 8), np.clip(du1, -0.3, 0.3), vs, ast])


def fit_logistic(X: np.ndarray, y: np.ndarray, lam: float = 1.0, iters: int = 30) -> np.ndarray:
    """Ridge-penalised logistic regression by IRLS (the intercept is not penalised)."""
    b = np.zeros(X.shape[1])
    pen = np.full(X.shape[1], lam)
    pen[0] = 0.0
    for _ in range(iters):
        eta = np.clip(X @ b, -30, 30)
        p = 1 / (1 + np.exp(-eta))
        wv = np.maximum(p * (1 - p), 1e-9)
        g = X.T @ (y - p) - pen * b
        H = (X * wv[:, None]).T @ X + np.diag(pen)
        step = np.linalg.solve(H, g)
        b += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return b


class Models:
    """p(Up) at every cached point for each model, cross-fitted inside TRAINING."""

    def __init__(self, D: Data, fit_every: int = 4) -> None:
        self.D = D
        c = D.c
        tr_w = D.w_split == 0
        eps = np.sort(np.unique(D.w_epoch[tr_w]))
        half = eps[len(eps) // 2]
        # fold per window: 1 = first half of TRAINING, 2 = second half, 0 = any later period
        self.w_fold = np.where(tr_w, np.where(D.w_epoch < half, 1, 2), 0).astype(np.int8)
        fold = self.w_fold[D.win]
        fit_sets = {0: tr_w, 1: tr_w & (self.w_fold == 2), 2: tr_w & (self.w_fold == 1)}
        self.k = {f: fit_basis(D, m) for f, m in fit_sets.items()}
        p = {"d": c["p_d"].copy(), "a": (c["lzs"] >= 0).astype(np.float64),
             "b": ndtr(c["lzs"] / (c["sigma"] * np.sqrt(c["left"] + INFO_LAG_S)))}
        pe = np.empty(D.n)
        for f in (0, 1, 2):
            m = fold == f
            pe[m] = p_e(D, self.k[f], m)
        p["e"] = pe
        y = D.won == 1
        g = left_group(c["left"])
        zb = np.searchsorted(ZBINS, c["mu"] / np.sqrt(c["sd"] ** 2 + (self.k[0] * c["sigma"]) ** 2)) - 1
        pc = np.empty(D.n)
        pf = np.empty(D.n)
        self.coef = {}
        sub = (np.arange(D.n) % fit_every) == 0          # thin the correlated ~216 ms points for fitting
        for f in (0, 1, 2):
            fitm = fit_sets[f][D.win] & (D.won >= 0)
            tgt = fold == f
            # C: frequency table over (group, z bin), shrunk toward E with a 20-point prior
            key = g * len(ZBINS) + zb
            nb = len(ZBINS) * 6
            cnt = np.bincount(key[fitm], minlength=nb)
            hit = np.bincount(key[fitm], weights=y[fitm].astype(float), minlength=nb)
            pri = np.bincount(key[fitm], weights=pe[fitm], minlength=nb) / np.maximum(cnt, 1)
            table = (hit + 20.0 * pri) / (cnt + 20.0)
            pc[tgt] = np.where(cnt[key[tgt]] > 0, table[key[tgt]], pe[tgt])
            # F: logistic per seconds-left group
            for gi in range(5):
                mfit = fitm & sub & (g == gi)
                mt = tgt & (g == gi)
                if not mt.any():
                    continue
                pe_f = p_e(D, self.k[f], mfit)
                b = fit_logistic(f_design(D, pe_f, mfit), y[mfit].astype(float))
                self.coef[(f, gi)] = b
                pf[mt] = 1 / (1 + np.exp(-np.clip(f_design(D, pe[mt], mt) @ b, -30, 30)))
        p["c"], p["f"] = pc, pf
        self.p = p

    def side(self, model: str, s: int) -> np.ndarray:
        return self.p[model] if s > 0 else 1.0 - self.p[model]


# ─────────────────────────────── execution ───────────────────────────────

PRESEND = np.asarray(PRESEND_PRIOR_MS)
RTT = np.asarray(VENUE_RTT_PRIOR_MS)
PROXY_MS = 500.0
SIZE = 5.0
SLIP = 0.005                    # the slippage allowance charged in the edge (spot-lag, engine: 0.5c)
SELL_GUARD_MS = 10_000.0        # spot_shock_replay.SETTLE_GUARD_MS: no exit decided in the last 10 s
THROTTLE_MS = 2000.0            # the live entry retry throttle after a killed FOK
SELL_RETRY_MS = 2000.0


def fee(rate, p):
    return rate * p * (1.0 - p)


@dataclass
class Orders:
    """One FOK order per (point, side) decision instant, both sides sharing the latency draw.
    ``ja``: the point whose book the venue matches against (-1: it would land after the close);
    common random numbers across latency scales (same seed, scaled draws)."""
    scale: float
    proxy_ms: float
    ja: np.ndarray
    t_arr: np.ndarray
    t_known: np.ndarray


def make_orders(D: Data, scale: float = 1.0, seed: int = 11, proxy_ms: float = PROXY_MS,
                buy_guard_ms: float = 0.0, proxy_scales: bool = True) -> Orders:
    rng = np.random.default_rng(seed)
    n = D.n
    u1, f1 = rng.integers(0, len(PRESEND), n), rng.uniform(0.85, 1.15, n)
    u2, f2 = rng.integers(0, len(RTT), n), rng.uniform(0.85, 1.15, n)
    pre = PRESEND[u1] * f1 * scale
    half = RTT[u2] * f2 / 2.0 * scale
    t = D.c["t"]
    t_arr = t + pre + half
    px = proxy_ms * (scale if proxy_scales else 1.0)
    ja = D.first_at_or_after(D.win, t_arr + px)
    ok = ja < D.w_end[D.win]
    if buy_guard_ms > 0:
        ok &= D.c["t"][np.minimum(ja, n - 1)] < D.w_close[D.win] - buy_guard_ms
    return Orders(scale, px, np.where(ok, ja, -1), t_arr, t_arr + half)


def max_entry_c(p_side: np.ndarray, rate: np.ndarray, min_edge: float) -> np.ndarray:
    """Highest cent price q with p - q - fee(q) - SLIP >= min_edge (q + r q (1 - q) is increasing)."""
    B = p_side - SLIP - min_edge
    r = rate
    disc = np.maximum((1 + r) ** 2 - 4 * r * B, 0.0)
    q = ((1 + r) - np.sqrt(disc)) / (2 * r)
    return np.floor(q * 100.0 + 1e-9).astype(np.int64)


@dataclass(frozen=True)
class Cfg:
    model: str = "e"
    hi: float = 60.0             # entries while lo < seconds left <= hi
    lo: float = 0.0
    min_edge: float = 0.02       # executable edge p - ask - fee(ask) - 0.5c
    max_ask: float = 0.95
    min_ask: float = 0.05
    max_spread: float = 0.03     # 1.0 = no limit (a book with no bid counts as infinitely wide)
    min_depth: float = 5.0
    slip_cap: float = 0.02       # FOK limit = min(ask + slip_cap, max entry)
    sides: int = 0               # 0 both, +1 UP only, -1 DOWN only
    attempts: int = 3

    def tag(self) -> str:
        return (f"{self.model}|{self.hi:g}-{self.lo:g}s|e{self.min_edge * 100:g}c|ask{self.min_ask:.2f}-{self.max_ask:.2f}"
                f"|spr{self.max_spread * 100:g}c|cap+{self.slip_cap * 100:g}c|sides{self.sides:+d}")


class Candidates:
    """Every (point, side) with an ask, 5 shares at it, <= 180 s left and an executable edge of
    at least 0.4c under ``model`` -- the superset every configuration filters."""

    def __init__(self, D: Data, M: Models, model: str, floor: float = 0.004, max_left: float = 180.0) -> None:
        c = D.c
        parts = []
        for s in (1, -1):
            sv = D.side(s)
            ask = sv["ask"].astype(np.float64) / 100.0
            ps = M.side(model, s)
            edge = ps - ask - fee(D.rate, ask) - SLIP
            m = (sv["ask"] >= 1) & (sv["ask"] <= 99) & (sv["adep"] >= 5.0) & (c["left"] <= max_left) & (edge >= floor)
            idx = np.where(m)[0]
            spr = np.where(sv["bid"][idx] >= 0, sv["ask"][idx] - sv["bid"][idx], 999).astype(np.int64)
            parts.append((idx, np.full(len(idx), s, np.int8), edge[idx], sv["ask"][idx].astype(np.int64), spr,
                          sv["adep"][idx], ps[idx]))
        cat = [np.concatenate([p[k] for p in parts]) for k in range(7)]
        o = np.lexsort((-cat[2], cat[0]))                # by point, larger edge first
        self.idx, self.side, self.edge, self.ask, self.spread, self.adep, self.p = (x[o] for x in cat)
        self.left = c["left"][self.idx]
        self.split = D.split[self.idx]
        self.model = model


def select(D: Data, C: Candidates, cfg: Cfg, splits) -> tuple[np.ndarray, np.ndarray]:
    """The configuration's SIGNALS: positions into ``C`` (one side per point, the larger edge),
    and their FOK limits in cents."""
    m = np.isin(C.split, splits) & (C.left > cfg.lo) & (C.left <= cfg.hi) & (C.edge >= cfg.min_edge - 1e-12) \
        & (C.ask <= int(round(cfg.max_ask * 100))) & (C.ask >= int(round(cfg.min_ask * 100))) \
        & (C.adep >= cfg.min_depth)
    if cfg.max_spread < 1.0:
        m &= C.spread <= int(round(cfg.max_spread * 100))
    if cfg.sides:
        m &= C.side == cfg.sides
    pos = np.where(m)[0]
    if len(pos):
        keep = np.concatenate([[True], C.idx[pos][1:] != C.idx[pos][:-1]])
        pos = pos[keep]
    me = max_entry_c(C.p[pos], D.rate[C.idx[pos]], cfg.min_edge)
    cap = np.minimum(C.ask[pos] + int(round(cfg.slip_cap * 100)), me)
    return pos, cap


def walk(D: Data, C: Candidates, O: Orders, pos: np.ndarray, cap: np.ndarray, attempts: int = 3,
         throttle_ms: float = THROTTLE_MS) -> dict:
    """The per-market entry loop: the first signal sends a FOK; a killed order may be followed by
    the next signal at least ``throttle_ms`` after the kill is known, up to ``attempts`` orders;
    one position per market (a fill ends the market's entries). Returns the ORDERS."""
    idx = C.idx[pos]
    w = D.win[idx]
    out = {k: [] for k in ("pos", "attempt", "filled", "entry_c", "arr_ask")}
    if len(pos) == 0:
        return {k: np.zeros(0, dtype=np.int64) for k in out}
    _, first = np.unique(w, return_index=True)
    cur = first
    for a in range(attempts):
        if len(cur) == 0:
            break
        i = idx[cur]
        s = C.side[pos[cur]]
        ja = O.ja[i]
        ok = ja >= 0
        jj = np.where(ok, ja, 0)
        ask_up, ask_dn = D.c["ua_c"][jj], D.c["da_c"][jj]
        dep_up, dep_dn = D.c["uad"][jj], D.c["dad"][jj]
        arr = np.where(s > 0, ask_up, ask_dn).astype(np.int64)
        dep = np.where(s > 0, dep_up, dep_dn)
        filled = ok & (arr >= 1) & (arr <= cap[cur]) & (dep >= 5.0)
        out["pos"].append(pos[cur])
        out["attempt"].append(np.full(len(cur), a + 1))
        out["filled"].append(filled)
        out["entry_c"].append(np.where(filled, arr, -1))
        out["arr_ask"].append(np.where(ok, arr, -2))          # -2: lands after the close; -1: no ask
        # retries: the next signal of the same market at or after (kill known + throttle)
        k = cur[~filled]
        if len(k) == 0 or a + 1 >= attempts:
            break
        wk = w[k]
        pt = D.first_at_or_after(wk, O.t_known[idx[k]] + throttle_ms)
        nxt = np.searchsorted(idx, pt)
        good = (nxt < len(idx)) & (pt < D.w_end[wk])
        nxt = np.where(good, nxt, 0)
        good &= w[nxt] == wk
        cur = nxt[good]
    return {k: np.concatenate(v) for k, v in out.items()}


# ─────────────────────────────── exits ───────────────────────────────

TIME_EXITS = (1, 2, 3, 5, 10, 20, 30)
TARGETS_C = (1, 2, 3, 5, 10)
TARGET_MAX = (1, 2, 3, 5, 10, 20, 30, 0)           # 0 = no time limit: the target or settlement
STOPS = ("stop_cross", "stop_toward", "stop_fair_bid", "stop_edge_neg", "stop_tok_rev", "stop_spot_rev")
EXITS = (("settle",) + tuple(f"t{h}" for h in TIME_EXITS)
         + tuple(f"tp{c}_{m if m else 'settle'}" for c in TARGETS_C for m in TARGET_MAX) + STOPS)
EXIT_IX = {e: k for k, e in enumerate(EXITS)}


class Exits:
    """Every exit of a filled order, from the same fill, on the same archived book; memoised.

    Sells are FOK with a floor (bid - 2c; the take-profit sells at the bid it saw), sent with a
    fresh latency draw, matched against the book ``proxy`` ms after arrival, retried every 2 s;
    an exit due inside the last ``guard`` ms settles 1/0 instead (no fee)."""

    def __init__(self, D: Data, M: Models, scale: float = 1.0, proxy_ms: float = PROXY_MS,
                 guard_ms: float = SELL_GUARD_MS, proxy_scales: bool = True) -> None:
        self.D, self.M = D, M
        self.scale, self.guard = scale, guard_ms
        self.proxy = proxy_ms * (scale if proxy_scales else 1.0)
        self.memo: dict = {}

    def get(self, i: int, s: int, entry_c: int, t_arr: float, t_known: float, model: str) -> np.ndarray:
        key = (i, s, model)
        r = self.memo.get(key)
        if r is None:
            r = self._compute(i, s, entry_c, t_arr, t_known, model)
            self.memo[key] = r
        return r

    def _compute(self, i, s, entry_c, t_arr, t_known, model) -> np.ndarray:
        D = self.D
        c = D.c
        w = int(D.win[i])
        s0, e0 = int(D.w_start[w]), int(D.w_end[w])
        close = float(D.w_close[w])
        won = int(D.w_won[w])
        won_side = np.nan if won < 0 else float((won == 1) == (s > 0))
        T = c["t"][s0:e0]
        sv = D.side(s)
        bid = sv["bid"][s0:e0].astype(np.int64)
        bdep = sv["bdep"][s0:e0]
        # the sell latencies: a seeded batch per fill (scalar Generator calls cost ~6 us each)
        rng = np.random.default_rng((i * 2 + (s > 0)) * 7919 + int(self.scale * 1000))
        sc, proxy, guard = self.scale, self.proxy, self.guard
        NB = 256
        pres = (PRESEND[rng.integers(0, len(PRESEND), NB)] * rng.uniform(.85, 1.15, NB) * sc).tolist()
        halves = (RTT[rng.integers(0, len(RTT), NB)] * rng.uniform(.85, 1.15, NB) / 2.0 * sc).tolist()
        k_draw = [0]
        n = len(T)
        # price, hold s, flag (0 sold / 2 settled), the bid the (last) sell decision saw
        res = np.full((len(EXITS), 4), np.nan)

        def settle():
            return (won_side, (close - t_arr) / 1000.0, 2.0, np.nan)

        def draw():
            j = k_draw[0] % NB
            k_draw[0] += 1
            return pres[j], halves[j]

        def sell(t_dec, floor, dbid):
            while True:
                if t_dec >= close - guard:
                    return settle()
                pre, half = draw()
                t_s = t_dec + pre + half
                js = int(T.searchsorted(t_s + proxy))
                if js < n and bid[js] >= 1 and bid[js] >= floor and bdep[js] >= 5.0:
                    return (bid[js] / 100.0, (t_s - t_arr) / 1000.0, 0.0, dbid)
                t_dec = t_s + half + SELL_RETRY_MS
                jd = int(T.searchsorted(t_dec, side="right")) - 1
                if jd >= 0 and bid[jd] >= 1:            # else no bid to read: the same floor again
                    dbid = bid[jd] / 100.0
                    floor = bid[jd] - 2

        def sell_at(t_dec):
            jd = int(T.searchsorted(t_dec, side="right")) - 1
            b = bid[jd] if jd >= 0 else -1
            return sell(t_dec, b - 2 if b >= 1 else 0, b / 100.0 if b >= 1 else np.nan)

        j0 = int(T.searchsorted(t_known))
        jg = int(T.searchsorted(close - guard))
        res[EXIT_IX["settle"]] = settle()
        for h in TIME_EXITS:
            res[EXIT_IX[f"t{h}"]] = sell_at(max(t_known, t_arr + h * 1000.0))
        # take-profits: sell AT the bid once bid >= entry + c; a killed sell keeps watching. The
        # attempt sequence does not depend on the time limit, so it is simulated once per target;
        # a target not reached in time falls back to the SAME sale as the plain time exit t<max>
        for tc in TARGETS_C:
            hit_all = bid >= entry_c + tc
            j, first_ok = j0, None
            while j < jg:
                rel = np.flatnonzero(hit_all[j:jg])
                if len(rel) == 0:
                    break
                jh = j + int(rel[0])
                pre, half = draw()
                t_s = T[jh] + pre + half
                js = int(T.searchsorted(t_s + proxy))
                if js < n and bid[js] >= bid[jh] and bdep[js] >= 5.0:
                    first_ok = (T[jh], (bid[js] / 100.0, (t_s - t_arr) / 1000.0, 0.0, bid[jh] / 100.0))
                    break
                j = max(jh + 1, int(T.searchsorted(t_s + half)))    # zero latency: never re-read jh
            for mx in TARGET_MAX:
                t_end = t_arr + mx * 1000.0 if mx else close
                if first_ok is not None and first_ok[0] <= t_end:
                    done = first_ok[1]
                else:
                    done = res[EXIT_IX[f"t{mx}"]] if mx else res[EXIT_IX["settle"]]
                res[EXIT_IX[f"tp{tc}_{mx if mx else 'settle'}"]] = done
        # invalidation exits: the first point (after the fill is known) where the condition holds
        if j0 < jg:
            sl = slice(s0 + j0, s0 + jg)
            pu = self.M.p[model][sl]
            ps = pu if s > 0 else 1.0 - pu
            conds = {
                "stop_cross": s * c["lzs"][sl] < 0,
                "stop_toward": s * c["mu"][sl] < 0.5 * s * c["mu"][i],
                "stop_fair_bid": (ps * 100.0 < bid[j0:jg]) & (bid[j0:jg] >= 1),
                "stop_edge_neg": ps < entry_c / 100.0 + fee(D.w_rate[w], entry_c / 100.0),
                "stop_tok_rev": np.nan_to_num(s * c["du1"][sl]) <= -0.02,
                "stop_spot_rev": np.nan_to_num(s * c["r1000"][sl] / c["sigma"][sl]) <= -1.5,
            }
            for name, cond in conds.items():
                rel = np.flatnonzero(cond)
                res[EXIT_IX[name]] = sell_at(T[j0 + int(rel[0])]) if len(rel) else settle()
        else:
            for name in STOPS:
                res[EXIT_IX[name]] = settle()
        return res


def pnl_per_share(res: np.ndarray, entry: float, rate: float) -> np.ndarray:
    """Net per share after the entry fee and (when sold) the exit fee, for every exit of one fill."""
    px, flag = res[:, 0], res[:, 2]
    return px - entry - fee(rate, entry) - np.where(flag == 0, fee(rate, px), 0.0)


# ─────────────────────────────── statistics ───────────────────────────────


def clustered(x: np.ndarray, g: np.ndarray) -> tuple[float, float]:
    """(mean, CR1 cluster-robust standard error of the mean), clusters ``g`` (5-minute epochs:
    every asset in one window is one bet)."""
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else math.nan), math.nan
    mu = float(x.mean())
    _, inv = np.unique(g, return_inverse=True)
    G = inv.max() + 1
    if G < 2:
        return mu, math.nan
    s = np.bincount(inv, weights=x - mu)
    return mu, float(math.sqrt(np.sum(s * s) * G / (G - 1)) / n)


def bootstrap_ci(x: np.ndarray, g: np.ndarray, reps: int = 2000, seed: int = 5) -> tuple[float, float]:
    """95 % percentile interval of the mean, resampling whole epochs."""
    if len(x) < 2:
        return math.nan, math.nan
    _, inv = np.unique(g, return_inverse=True)
    G = inv.max() + 1
    sums = np.bincount(inv, weights=x)
    cnts = np.bincount(inv)
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, G, size=(reps, G))
    means = sums[draws].sum(1) / np.maximum(cnts[draws].sum(1), 1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def conservative_c(x: np.ndarray, level: float = 0.95) -> float:
    """c/share with the LOSS RATE replaced by its one-sided Clopper-Pearson upper bound: a
    set of high-priced favourites with no loss yet has a near-zero variance and an absurd t, but
    one loss costs ~20 wins; this charges the loss rate the sample cannot rule out. A sample
    with no loss is charged the average entry cost as its loss size (the token paying 0)."""
    from scipy.stats import beta
    n = len(x)
    if n == 0:
        return math.nan
    lose = x <= 0
    k = int(lose.sum())
    q = float(beta.ppf(level, k + 1, n - k)) if k < n else 1.0
    win = float(x[~lose].mean()) if k < n else 0.0
    loss = float(x[lose].mean()) if k else -(1.0 - win)
    return ((1 - q) * win + q * loss) * 100.0


def trade_stats(x: np.ndarray, epoch: np.ndarray, order_t: np.ndarray | None = None, boot: bool = False) -> dict:
    """Per-share results of a set of trades (x in dollars per share, SIZE shares each)."""
    n = len(x)
    if n == 0:
        return {"fills": 0}
    mu, se = clustered(x, epoch)
    o = np.argsort(order_t if order_t is not None else epoch, kind="stable")
    eq = np.cumsum(x[o] * SIZE)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq)) if n else 0.0
    wins, losses = x[x > 0].sum(), -x[x <= 0].sum()
    out = {"fills": n, "win": float(np.mean(x > 0)), "net_usdc": float(x.sum() * SIZE), "c_share": mu * 100.0,
           "median_c": float(np.median(x) * 100.0), "sd_c": float(np.std(x, ddof=1) * 100.0) if n > 1 else math.nan,
           "t": mu / se if se and se > 0 else math.nan, "ci_lo": (mu - 1.96 * se) * 100.0 if se == se else math.nan,
           "ci_hi": (mu + 1.96 * se) * 100.0 if se == se else math.nan, "max_dd": max(dd, 0.0),
           "pf": float(wins / losses) if losses > 0 else math.inf, "epochs": int(len(np.unique(epoch))),
           "losses": int(np.sum(x <= 0)), "c_cons": conservative_c(x)}
    if boot:
        lo, hi = bootstrap_ci(x, epoch)
        out["boot_lo"], out["boot_hi"] = lo * 100.0, hi * 100.0
    return out


# ─────────────────────────────── running configurations ───────────────────────────────

WINDOWS_S = (180, 150, 120, 90, 75, 60, 45, 30, 20, 15, 10, 5)
EDGES_C = (0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 15)
MAX_ASKS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.98)
COARSE_MAX_ASKS = (0.60, 0.80, 0.90, 0.95, 0.98)
SLICES = ((120, 90), (90, 60), (60, 45), (45, 30), (30, 20), (20, 10), (10, 5), (5, 0))
SWEEP_EXITS = ("settle", "t2", "t5", "t10", "t30", "tp1_5", "tp2_10", "tp3_30", "tp5_settle", "tp10_settle",
               "stop_fair_bid", "stop_toward", "stop_edge_neg")
#: the hypothesis exactly as stated, fixed before any result was seen: the project model, the last
#: 60 s, 2c executable edge, MAX_ASK 0.95, spread <= 3c, the spot-lag FOK cap, held to settlement
CANONICAL = (dict(model="d", hi=60.0, lo=0.0, min_edge=0.02, max_ask=0.95, max_spread=0.03), "settle")


class Engine:
    """One execution setting (latency scale, guards): orders, exits and candidate sets."""

    def __init__(self, D: Data, M: Models, scale: float = 1.0, sell_guard_ms: float = SELL_GUARD_MS,
                 buy_guard_ms: float = 0.0, proxy_scales: bool = True, seed: int = 11) -> None:
        self.D, self.M = D, M
        self.O = make_orders(D, scale, seed, buy_guard_ms=buy_guard_ms, proxy_scales=proxy_scales)
        self.X = Exits(D, M, scale, guard_ms=sell_guard_ms, proxy_scales=proxy_scales)
        self._C: dict[str, Candidates] = {}

    def cands(self, model: str) -> Candidates:
        if model not in self._C:
            self._C[model] = Candidates(self.D, self.M, model)
        return self._C[model]

    def run(self, cfg: Cfg, splits, raw: bool = False) -> dict:
        """Signals -> orders -> fills -> every exit, for the periods ``splits``. ``raw`` also
        returns each exit's (price, hold s, flag, decision bid) for the stress tests."""
        C = self.cands(cfg.model)
        pos, cap = select(self.D, C, cfg, splits)
        return self.run_on(C, pos, cap, cfg.model, cfg.attempts, raw, cfg=cfg)

    def run_on(self, C, pos: np.ndarray, cap: np.ndarray, model: str, attempts: int = 3, raw: bool = False,
               cfg: Cfg | None = None) -> dict:
        """The same pipeline on any signal list: ``C`` has idx / side / ask / edge / p arrays
        (sorted by point, one side per point at ``pos``), ``cap`` the FOK limits in cents."""
        D, O = self.D, self.O
        od = walk(D, C, O, pos, cap, attempts)
        f = od["filled"].astype(bool)
        fp = od["pos"][f]
        idx, side, ent = C.idx[fp], C.side[fp], od["entry_c"][f]
        R = np.full((len(fp), len(EXITS)), np.nan)
        RAW = np.full((len(fp), len(EXITS), 4), np.nan) if raw else None
        for k, (i, s, e) in enumerate(zip(idx, side, ent)):
            res = self.X.get(int(i), int(s), int(e), float(O.t_arr[i]), float(O.t_known[i]), model)
            R[k] = pnl_per_share(res, e / 100.0, float(D.rate[i]))
            if raw:
                RAW[k] = res
        ja = O.ja[idx]
        arr_bid = np.where(side > 0, D.c["ub_c"][np.maximum(ja, 0)], D.c["db_c"][np.maximum(ja, 0)])
        return {"cfg": cfg, "sig_idx": C.idx[pos], "sig_edge": C.edge[pos], "sig_side": C.side[pos],
                "ord_idx": C.idx[od["pos"]], "ord_filled": f, "ord_arr": od["arr_ask"], "ord_dec": C.ask[od["pos"]],
                "ord_cap": cap[np.searchsorted(pos, od["pos"])], "ord_side": C.side[od["pos"]],
                "ord_attempt": od["attempt"],
                "idx": idx, "side": side, "entry_c": ent, "dec_ask": C.ask[fp], "edge": C.edge[fp],
                "cap": cap[np.searchsorted(pos, fp)], "p": C.p[fp], "arr_bid_c": arr_bid, "R": R, "RAW": RAW}


def period_hours(D: Data, sp: int) -> float:
    return len(np.unique(D.w_epoch[D.w_split == sp])) * 5.0 / 60.0


def summarize(D: Data, r: dict, sp: int | None, exits=SWEEP_EXITS, boot: bool = False) -> list[dict]:
    """One row per exit: the funnel (signals, orders, fills) and the trade statistics for the
    period ``sp`` (None = every period in ``r``)."""
    def inp(a):
        return np.ones(len(a), bool) if sp is None else D.split[a] == sp
    ms, mo, mt = inp(r["sig_idx"]), inp(r["ord_idx"]), inp(r["idx"])
    if sp is None:
        sps = np.unique(D.split[r["sig_idx"]]) if len(r["sig_idx"]) else []
        markets = int(sum(np.sum(D.w_split == s) for s in sps))
        hours = sum(period_hours(D, s) for s in sps)
    else:
        markets = int(np.sum(D.w_split == sp))
        hours = period_hours(D, sp)
    n_ord, n_fill = int(mo.sum()), int(mt.sum())
    base = {"markets": markets, "hours": round(hours, 2), "signals": int(ms.sum()),
            "opportunities": int(len(np.unique(D.win[r["sig_idx"][ms]]))), "orders": n_ord, "fills": n_fill,
            "fill_rate": n_fill / n_ord if n_ord else math.nan,
            "avg_edge_c": float(np.mean(r["edge"][mt]) * 100) if n_fill else math.nan,
            "avg_ask": float(np.mean(r["dec_ask"][mt]) / 100) if n_fill else math.nan,
            "avg_entry": float(np.mean(r["entry_c"][mt]) / 100) if n_fill else math.nan}
    out = []
    ep = D.epoch[r["idx"][mt]]
    tt = D.c["t"][r["idx"][mt]]
    for ex in exits:
        x = r["R"][mt, EXIT_IX[ex]]
        ok = ~np.isnan(x)
        st = trade_stats(x[ok], ep[ok], tt[ok], boot=boot)
        net = st.get("net_usdc", 0.0)
        row = dict(base, exit=ex, **st)
        row["fills"] = int(ok.sum())
        row["unresolved"] = int((~ok).sum())
        row.update({"usdc_per_fill": net / ok.sum() if ok.sum() else math.nan,
                    "usdc_per_order": net / n_ord if n_ord else math.nan,
                    "usdc_per_market": net / markets if markets else math.nan,
                    "usdc_per_hour": net / hours if hours else math.nan})
        out.append(row)
    return out


CFG_FIELDS = ("model", "hi", "lo", "min_edge", "max_ask", "min_ask", "max_spread", "slip_cap", "sides", "attempts")
ROW_FIELDS = ("stage", "exit", "period") + CFG_FIELDS + (
    "markets", "hours", "signals", "opportunities", "orders", "fills", "fill_rate", "avg_edge_c", "avg_ask",
    "avg_entry", "win", "net_usdc", "c_share", "median_c", "sd_c", "t", "ci_lo", "ci_hi", "max_dd", "pf",
    "epochs", "unresolved", "usdc_per_fill", "usdc_per_order", "usdc_per_market", "usdc_per_hour")
BAND_FIELDS = ROW_FIELDS + ("losses", "c_cons")

# shared with forked workers (copy-on-write): set by _init_shared before the pool is created
_SH: dict = {}


def _init_shared(scale: float = 1.0, **kw) -> None:
    if "D" not in _SH:
        _SH["D"] = Data()
        _SH["M"] = Models(_SH["D"])
    _SH["E"] = Engine(_SH["D"], _SH["M"], scale=scale, **kw)


def _run_chunk(args) -> list[dict]:
    cfgs, splits, stage, exits = args
    D, E = _SH["D"], _SH["E"]
    rows = []
    for cfg in cfgs:
        r = E.run(cfg, splits)
        for sp in splits:
            for row in summarize(D, r, sp, exits):
                row.update({k: getattr(cfg, k) for k in CFG_FIELDS})
                row.update(stage=stage, period=SPLITS[sp])
                rows.append(row)
    return rows


def run_grid(cfgs: list[Cfg], splits=(0, 1), stage: str = "coarse", exits=SWEEP_EXITS, procs: int = 3) -> list[dict]:
    chunks = [cfgs[k::procs * 8] for k in range(procs * 8)]
    rows: list[dict] = []
    with ProcessPoolExecutor(procs) as pool:
        for got in pool.map(_run_chunk, [(ch, splits, stage, exits) for ch in chunks if ch]):
            rows.extend(got)
    return rows


def write_rows(rows: list[dict], path: Path, append: bool = False, fields=ROW_FIELDS) -> None:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not (append and path.exists())
    with open(path, "a" if append else "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            wr.writeheader()
        for r in rows:
            wr.writerow({k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()})


def coarse_grid() -> list[Cfg]:
    return [Cfg(model=m, hi=float(h), lo=0.0, min_edge=e / 100.0, max_ask=a, max_spread=spr)
            for m in MODELS for h in WINDOWS_S for e in EDGES_C for a in COARSE_MAX_ASKS for spr in (0.03, 1.0)]


def slice_grid() -> list[Cfg]:
    return [Cfg(model=m, hi=float(h), lo=float(lo), min_edge=e / 100.0, max_ask=a, max_spread=spr)
            for m in MODELS for (h, lo) in SLICES for e in (0.5, 1, 2, 3, 5, 8) for a in (0.80, 0.95, 0.98)
            for spr in (0.03, 1.0)]


#: the PRICE-BAND stage (added after the diagnostics showed every entry below ~0.70 losing and the
#: only positive TRAINING pocket at high-priced, far-from-strike favourites): MIN_ASK and MAX_ASK
#: together. Ranked separately (analyze_60s_snipe: 'band' criteria, by conservative c/share).
BAND_MIN = (0.50, 0.70, 0.80, 0.85, 0.90, 0.93, 0.95)
BAND_MAX = (0.90, 0.93, 0.95, 0.97, 0.98, 0.99)


def band_grid() -> list[Cfg]:
    return [Cfg(model=m, hi=float(h), lo=0.0, min_edge=e / 100.0, min_ask=lo, max_ask=hi, max_spread=spr)
            for m in MODELS for h in (180, 120, 90, 60, 45, 30, 20, 10) for e in (0.5, 1, 2, 3, 5)
            for lo in BAND_MIN for hi in BAND_MAX if lo < hi for spr in (0.03, 1.0)]


def fine_grid(seeds: list[Cfg]) -> list[Cfg]:
    out = set()
    for s in seeds:
        for a in MAX_ASKS:
            for lo in (0.0, 2.0, 5.0, 10.0):
                if lo >= s.hi:
                    continue
                for spr in (0.01, 0.02, 0.03, 0.05, 1.0):
                    for cap in (0.0, 0.01, 0.02):
                        out.add(Cfg(model=s.model, hi=s.hi, lo=lo, min_edge=s.min_edge, max_ask=a, max_spread=spr,
                                    slip_cap=cap))
    return sorted(out, key=lambda c: c.tag())


SWEEP_CSV = ROOT / "results/60s_snipe_sweep.csv"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cache")
    c.add_argument("--procs", type=int, default=3)
    sw = sub.add_parser("sweep", help="TRAINING and VALIDATION only; unseen periods are never read here")
    sw.add_argument("--stage", choices=("coarse", "slices", "fine", "band"), required=True)
    sw.add_argument("--procs", type=int, default=3)
    sw.add_argument("--output", type=Path, default=OUT / "sweep_trainval.csv")
    su = sub.add_parser("sweep-unseen", help="AFTER the lock: every sweep configuration on the unseen and "
                                             "forward periods, merged into results/60s_snipe_sweep.csv")
    su.add_argument("--procs", type=int, default=3)
    a = ap.parse_args(argv)
    if a.cmd == "cache":
        print(build_cache(procs=a.procs))
    elif a.cmd == "sweep":
        _init_shared()
        if a.stage == "coarse":
            cfgs = coarse_grid()
        elif a.stage == "slices":
            cfgs = slice_grid()
        elif a.stage == "band":
            cfgs = band_grid()
        else:
            import analyze_60s_snipe as A
            cfgs = fine_grid(A.fine_seeds(a.output))
        print(f"{a.stage}: {len(cfgs)} configurations x {len(SWEEP_EXITS)} exits, TRAINING + VALIDATION", flush=True)
        rows = run_grid(cfgs, splits=(0, 1), stage=a.stage, procs=a.procs)
        if a.stage == "band":
            path = a.output.with_name("sweep_band_trainval.csv")
            write_rows(rows, path, fields=BAND_FIELDS)
        else:
            path = a.output
            write_rows(rows, path, append=a.stage != "coarse")
        print(f"wrote {len(rows)} rows to {path}")
    elif a.cmd == "sweep-unseen":
        lock = OUT / "locked.json"
        if not lock.exists():
            raise SystemExit("no lock: the unseen periods are read only after analyze_60s_snipe.py select")
        import analyze_60s_snipe as A
        _init_shared()
        stages = {"coarse": coarse_grid(), "slices": slice_grid(), "fine": fine_grid(A.fine_seeds(OUT / "sweep_trainval.csv")),
                  "band": band_grid()}
        tv = pd_read(OUT / "sweep_trainval.csv"), pd_read(OUT / "sweep_band_trainval.csv")
        rows = []
        for stage, cfgs in stages.items():
            print(f"{stage}: {len(cfgs)} configurations on unseen_1..3 + forward", flush=True)
            rows += run_grid(cfgs, splits=(2, 3, 4, 5), stage=stage, procs=a.procs)
        write_rows(rows, OUT / "sweep_unseen.csv", fields=BAND_FIELDS)
        import pandas as pd
        un = pd.read_csv(OUT / "sweep_unseen.csv")
        full = pd.concat([tv[0], tv[1], un], ignore_index=True)
        full["post_lock"] = full["period"].isin(["unseen_1", "unseen_2", "unseen_3", "forward"])
        full.to_csv(OUT / "sweep_long_all_periods.csv.gz", index=False, compression="gzip")
        (OUT / "sweep_unseen.csv").unlink()
        wide = compact_sweep(full)
        wide.to_csv(SWEEP_CSV, index=False)
        print(f"wrote {len(full)} long rows to {OUT / 'sweep_long_all_periods.csv.gz'}, {len(wide)} to {SWEEP_CSV}")


def pd_read(path: Path):
    import pandas as pd
    return pd.read_csv(path)


def compact_sweep(full):
    """results/60s_snipe_sweep.csv: one row per (stage, configuration, exit) of the coarse, time-slice
    and price-band grids (the fine stage is in the long file), the TRAINING funnel and fills / c-share /
    t / net per period, plus the unseen periods pooled. unseen_* and forward_* were computed after the lock."""
    import pandas as pd
    key = ["stage", "model", "hi", "lo", "min_edge", "min_ask", "max_ask", "max_spread", "slip_cap", "sides",
           "attempts", "exit"]
    tr = full[full.period == "train"]
    parts = [tr[key + ["signals", "orders", "fill_rate", "avg_edge_c", "avg_ask", "win", "max_dd", "usdc_per_fill",
                       "usdc_per_order", "usdc_per_market", "usdc_per_hour"]].set_index(key).add_prefix("train_")]
    for per in ("train", "validation", "unseen_1", "unseen_2", "unseen_3", "forward"):
        d = full[full.period == per].set_index(key)[["fills", "c_share", "t", "net_usdc"]]
        parts.append(d.add_prefix(f"{per}_"))
    w = pd.concat(parts, axis=1).reset_index()
    un = ("unseen_1", "unseen_2", "unseen_3")
    f = sum(w[f"{p}_fills"].fillna(0) for p in un)
    w["unseen_fills"] = f
    w["unseen_c_share"] = sum(w[f"{p}_c_share"].fillna(0) * w[f"{p}_fills"].fillna(0) for p in un) / f.replace(0, np.nan)
    w = w[w.stage != "fine"]
    return w.round({c: 4 for c in w.columns if w[c].dtype == float})


if __name__ == "__main__":
    main()
