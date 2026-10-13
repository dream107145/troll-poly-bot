"""Paired-exit momentum study (2026-10-01): data, causal features, the fair-probability model and the
execution simulator shared by every strategy (A-F) so that differences come only from the trade structure.

Data: data/replay/paired_exit/<asset>.npz (scripts/paired_exit/cache.py) -- the dashboard's 200 ms window
archive: composite spot, per-exchange spot (binance / bybit / coinbase; BTC also deribit / kraken / bitstamp
from 10-01 19:49), both tokens' best bid / ask with depth near the touch, crypto order flow, the venue outcome.

Execution model (the paper bot's CALIBRATED scenario, fitted on 193 real orders):
  * a taker order decided at t lands at t + LAT (250 ms x latency multiplier);
  * BUY fills when the ask stays <= its limit from the last sample before landing through landing + 1 s,
    charged the WORSE of the ask at landing and at landing + 1 s; SELL mirrors it on the bid;
  * a killed exit is retried with the new bid once its result is known (landing + 1 s);
  * the fill is known at landing + 1 s (the paper venue holds the result for the survival check);
  * taker fee 0.07 x p x (1 - p) per share; maker fee 0 (no rebate counted);
  * a RESTING order (pair buy, ladder ask) fills only when the far touch trades STRICTLY through its price
    (paper venue rule; ``touch_fill`` relaxes it to "reaches") and only after it has been live LAT ms;
  * stress knobs: latency multiplier, fill-probability haircut (random kills of would-be fills), spread
    widening (ask + w/2, bid - w/2), extra slippage on every taker fill, stale book / stale spot.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

ROOT = "/root/troll-poly-bot"
CACHE = f"{ROOT}/data/replay/paired_exit"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
EXCH = ("binance", "bybit", "coinbase", "deribit", "kraken", "bitstamp")
SPLITS = (("train", 0.0, 0.5), ("validation", 0.5, 0.7), ("test", 0.7, 0.85), ("forward", 0.85, 1.0))
FEE_RATE = 0.07
TICK = 0.01
HORIZONS = (0.2, 0.5, 1.0, 2.0, 5.0)


def fee(p):
    return FEE_RATE * p * (1.0 - p)


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ------------------------------------------------------------------------------------------- data


class Win:
    """One 5-minute window: arrays over its 200 ms samples plus causal features."""

    __slots__ = ("asset", "slug", "outcome", "open", "close", "strike", "split", "day", "k", "sigma",
                 "T", "left", "spot", "ua", "ub", "da", "db", "uad", "ubd", "dad", "dbd", "tfi", "obi", "ofi",
                 "X", "F", "fair_up", "regime", "n")

    def side_arrays(self, side):
        return (self.ua, self.ub) if side == "UP" else (self.da, self.db)

    def ix(self, t):
        """Index of the last sample at or before t (-1 if none)."""
        return int(np.searchsorted(self.T, t, side="right")) - 1


def _logit(p):
    p = np.clip(p, 0.02, 0.98)
    return np.log(p / (1.0 - p))


def load_asset(asset: str):
    z = np.load(f"{CACHE}/{asset.lower()}.npz", allow_pickle=False)
    data, off, cols = z["data"], z["off"], [str(c) for c in z["cols"]]
    metas = json.loads(str(z["meta"]))
    ci = {c: i for i, c in enumerate(cols)}
    wins = []
    for k, m in enumerate(metas):
        a = data[off[k]:off[k + 1]]
        w = Win()
        w.asset, w.slug, w.outcome = m["asset"] or asset.upper(), m["slug"], m["outcome"]
        w.open, w.close, w.strike = m["open"], m["close"], m["strike"]
        w.T = a[:, ci["t"]]
        order = np.argsort(w.T, kind="stable")
        a = a[order]
        w.T = a[:, ci["t"]]
        w.left = (w.close - w.T) / 1000.0
        w.spot = a[:, ci["spot"]]
        for name in ("ua", "ub", "da", "db", "uad", "ubd", "dad", "dbd"):
            setattr(w, name, a[:, ci[name]])
        w.tfi, w.obi, w.ofi = a[:, ci["tfi15"]], a[:, ci["obi"]], a[:, ci["ofi"]]
        w.X = {e: a[:, ci[f"x_{e}"]] for e in EXCH}
        w.n = len(w.T)
        w.day = int(w.open // 86_400_000)
        wins.append(w)
    wins.sort(key=lambda w: w.open)
    n = len(wins)
    for k, w in enumerate(wins):
        w.k = k
        w.split = next(s for s, lo, hi in SPLITS if int(lo * n) <= k < int(hi * n) or (hi == 1.0 and k >= int(lo * n)))
    return wins


def window_sigma(w):
    """Realised 1 s log-return sigma (bp / sqrt(s)) of a window, from every 5th sample."""
    lp = np.log(w.spot) * 1e4
    idx = np.arange(0, w.n, 5)
    j = np.searchsorted(w.T, w.T[idx] - 1000.0, side="right") - 1
    ok = (j >= 0) & (w.T[idx] - w.T[np.maximum(j, 0)] < 1300.0)
    if ok.sum() < 30:
        return None
    dt = (w.T[idx][ok] - w.T[j[ok]]) / 1000.0
    r = (lp[idx][ok] - lp[j[ok]]) / np.sqrt(np.maximum(dt, 0.2))
    s = float(np.std(r))
    return s if s > 0 else None


def lag_index(T, h_ms, tol_ms=450.0):
    j = np.searchsorted(T, T - h_ms, side="right") - 1
    ok = (j >= 0) & ((T - T[np.maximum(j, 0)]) <= h_ms + tol_ms)
    return np.maximum(j, 0), ok


def mid_of(ask, bid):
    m = np.where(np.isnan(ask) | np.isnan(bid), np.nan, (ask + bid) / 2.0)
    m = np.where(np.isnan(m) & ~np.isnan(ask), np.maximum(ask - 0.005, 0.0), m)
    m = np.where(np.isnan(m) & ~np.isnan(bid), np.minimum(bid + 0.005, 1.0), m)
    return m


FEATS = ("lm", "dist", "z05", "z1", "z2", "z5", "accel", "persist", "agree", "disp", "tok1", "tok3", "tobi",
         "tfi", "ofi", "volr", "revs")


def compute_features(w, sigma, spot_shift_ms=0.0, drop_exchange=None):
    """Causal features at every sample, signed toward UP. ``spot_shift_ms`` > 0 reads the spot that much
    late (a delayed feed); ``drop_exchange`` removes one exchange from the cross-exchange terms."""
    T = w.T
    Ts = T - spot_shift_ms
    lp_all = np.log(w.spot) * 1e4
    if spot_shift_ms > 0:
        js = np.searchsorted(T, Ts, side="right") - 1
        lp = np.where(js >= 0, lp_all[np.maximum(js, 0)], np.nan)
    else:
        lp = lp_all
    F = {}
    r = {}
    for h in HORIZONS:
        j, ok = lag_index(T, h * 1000.0)
        rr = lp - lp[j]
        rr[~ok] = np.nan
        r[h] = rr
    for h, name in ((0.5, "z05"), (1.0, "z1"), (2.0, "z2"), (5.0, "z5")):
        F[name] = r[h] / (sigma * math.sqrt(h))
    F["accel"] = (r[0.5] / 0.5 - r[2.0] / 2.0) / sigma
    # persistence: share of the last 10 sample steps moving the way of the 1 s return
    d = np.diff(lp, prepend=lp[0])
    s1 = np.sign(r[1.0])
    same = np.zeros(w.n)
    for lag in range(10):
        same += (np.sign(np.roll(d, lag)) == s1) & (np.roll(d, lag) != 0)
    same[:10] = np.nan
    F["persist"] = (same / 10.0) * s1                       # signed: + = persistent up
    # cross-exchange: mean sign of each exchange's 1 s return (+1 all up), dispersion in sigma units
    j1, ok1 = lag_index(T, 1000.0)
    rets = []
    for e in EXCH:
        if e == drop_exchange:
            continue
        x = w.X[e]
        if np.all(np.isnan(x)):
            continue
        lx = np.log(x) * 1e4
        re_ = lx - lx[j1]
        re_[~ok1] = np.nan
        rets.append(re_)
    if rets:
        R = np.vstack(rets)
        thr = 0.1 * sigma
        sg = np.where(np.isnan(R), np.nan, np.where(np.abs(R) >= thr, np.sign(R), 0.0))
        cnt = np.sum(~np.isnan(R), axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            F["agree"] = np.where(cnt > 0, np.nansum(sg, axis=0) / np.maximum(cnt, 1), np.nan)
            F["disp"] = np.where(cnt > 1, np.nanstd(R, axis=0) / sigma, 0.0)
        F["n_exch"] = cnt.astype(float)
    else:
        F["agree"] = np.full(w.n, np.nan); F["disp"] = np.zeros(w.n); F["n_exch"] = np.zeros(w.n)
    mu = mid_of(w.ua, w.ub)
    md = mid_of(w.da, w.db)
    mid = np.where(np.isnan(mu), 1.0 - md, np.where(np.isnan(md), mu, (mu + 1.0 - md) / 2.0))
    F["mid"] = mid
    F["lm"] = _logit(mid)
    te = np.maximum(w.left, 1.0)
    F["dist"] = np.clip((lp - math.log(w.strike) * 1e4) / (sigma * np.sqrt(te)), -6, 6) if w.strike > 0 else np.zeros(w.n)
    for h, name in ((1000.0, "tok1"), (3000.0, "tok3")):
        j, ok = lag_index(T, h)
        v = (mid - mid[j]) * 100.0
        v[~ok] = np.nan
        F[name] = v
    with np.errstate(invalid="ignore", divide="ignore"):
        ou = (w.ubd - w.uad) / (w.ubd + w.uad)
        od = (w.dbd - w.dad) / (w.dbd + w.dad)
    F["tobi"] = np.nan_to_num((np.nan_to_num(ou) - np.nan_to_num(od)) / 2.0)
    F["tfi"], F["ofi"] = np.nan_to_num(w.tfi), np.nan_to_num(w.ofi)
    # realised vol over the last 30 s relative to sigma, and the number of 1 s direction flips in it
    j30, ok30 = lag_index(T, 30000.0, tol_ms=2000.0)
    sq = np.nancumsum(np.nan_to_num(d) ** 2)
    rv = np.sqrt(np.maximum(sq - sq[j30], 0.0) / 30.0)
    F["volr"] = np.where(ok30, rv / sigma, np.nan)
    flips = np.zeros(w.n)
    s_prev = np.roll(s1, 5)
    ch = (s1 * s_prev < 0).astype(float)
    cc = np.cumsum(ch)
    F["revs"] = np.where(ok30, (cc - cc[j30]) / 5.0, np.nan)   # sampled every ~1 s (5 samples)
    F["spread_up"] = w.ua - w.ub
    F["spread_dn"] = w.da - w.db
    return F


def design(F, idx=None):
    cols = [F[k] if idx is None else F[k][idx] for k in FEATS]
    return np.column_stack(cols)


def fit_logistic(X, y, l2=10.0, iters=60):
    mu = np.nanmean(X, 0)
    sd = np.nanstd(X, 0) + 1e-9
    Z = (np.where(np.isnan(X), mu, X) - mu) / sd
    Z = np.hstack([np.ones((len(Z), 1)), Z])
    wgt = np.zeros(Z.shape[1])
    reg = l2 * np.r_[0.0, np.ones(Z.shape[1] - 1)]
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Z @ wgt, -30, 30)))
        g = Z.T @ (p - y) + reg * wgt
        H = (Z * (p * (1 - p))[:, None]).T @ Z + np.diag(reg + 1e-9)
        step = np.linalg.solve(H, g)
        wgt -= step
        if np.abs(step).max() < 1e-7:
            break
    return {"features": list(FEATS), "mu": mu.tolist(), "sd": sd.tolist(), "w": wgt.tolist(), "l2": l2}


def predict(model, X):
    mu, sd, w = np.asarray(model["mu"]), np.asarray(model["sd"]), np.asarray(model["w"])
    Z = (np.where(np.isnan(X), mu, X) - mu) / sd
    return 1.0 / (1.0 + np.exp(-np.clip(w[0] + Z @ w[1:], -30, 30)))


def prepare(wins):
    """Causal sigma (previous window's) and features for every window after the first per asset."""
    out, prev = [], None
    for w in wins:
        s = window_sigma(w)
        if prev is not None:
            w.sigma = prev
            w.F = compute_features(w, prev)
            out.append(w)
        if s:
            prev = s
    return out


def classify_regime(w):
    """Hindsight labels for the regime analysis only (never used to trade)."""
    lp = np.log(w.spot) * 1e4
    move = abs(lp[-1] - lp[0]) / (w.sigma * math.sqrt(300.0))
    i10 = np.searchsorted(w.T, np.arange(w.T[0], w.T[-1], 10_000.0))
    seg = np.sign(np.diff(lp[np.minimum(i10, w.n - 1)]))
    seg = seg[seg != 0]
    flips = int(np.sum(seg[1:] != seg[:-1])) if len(seg) > 1 else 0
    trend = "strong trend" if move >= 1.0 else "weak trend" if move >= 0.5 else "sideways"
    return {"trend": trend, "move_sig": move, "flips": flips}


# ------------------------------------------------------------------------------------------- execution


@dataclass
class Exec:
    lat_ms: float = 250.0
    lat_mult: float = 1.0
    survive_ms: float = 1000.0
    fill_haircut: float = 0.0            # probability a would-be taker fill is killed anyway
    spread_add: float = 0.0              # ask + w/2, bid - w/2
    slip_add: float = 0.0                # every taker fill this much worse
    touch_fill: bool = False             # resting orders fill on touch instead of trade-through
    stale_book_ms: float = 0.0           # decisions read the book this much late
    seed: int = 7
    rng: np.random.Generator = field(default=None, repr=False)

    def __post_init__(self):
        self.rng = np.random.default_rng(self.seed)

    @property
    def lat(self):
        return self.lat_ms * self.lat_mult

    def ask_arr(self, w, side):
        a = w.ua if side == "UP" else w.da
        return a + self.spread_add / 2.0 if self.spread_add else a

    def bid_arr(self, w, side):
        b = w.ub if side == "UP" else w.db
        return b - self.spread_add / 2.0 if self.spread_add else b

    def seen(self, w, arr, t):
        """What the bot sees at t (a stale book reads stale_book_ms late)."""
        i = w.ix(t - self.stale_book_ms)
        return None if i < 0 or math.isnan(arr[i]) else float(arr[i])

    def taker_buy(self, w, side, t, limit):
        """(fill price, time the result is known) or (None, known time)."""
        arr = self.ask_arr(w, side)
        land = t + self.lat
        i0 = max(w.ix(land), 0)
        i1 = int(np.searchsorted(w.T, land + self.survive_ms, side="right"))
        known = land + self.survive_ms
        if land >= w.close - 200.0:
            return None, known
        seg = arr[i0:max(i1, i0 + 1)]
        if seg.size == 0 or np.isnan(seg).any() or seg.max() > limit + 1e-9:
            return None, known
        if self.fill_haircut and self.rng.random() < self.fill_haircut:
            return None, known
        a_land, a_end = float(arr[i0]), float(arr[min(max(i1 - 1, i0), w.n - 1)])
        px = min(max(a_land, a_end) + self.slip_add, limit)
        return round(px, 4), known

    def taker_sell(self, w, side, t, limit):
        arr = self.bid_arr(w, side)
        land = t + self.lat
        i0 = max(w.ix(land), 0)
        i1 = int(np.searchsorted(w.T, land + self.survive_ms, side="right"))
        known = land + self.survive_ms
        if land >= w.close - 200.0:
            return None, known
        seg = arr[i0:max(i1, i0 + 1)]
        if seg.size == 0 or np.isnan(seg).any() or seg.min() < limit - 1e-9:
            return None, known
        if self.fill_haircut and self.rng.random() < self.fill_haircut:
            return None, known
        b_land, b_end = float(arr[i0]), float(arr[min(max(i1 - 1, i0), w.n - 1)])
        px = max(min(b_land, b_end) - self.slip_add, limit)
        return round(px, 4), known

    def resting_buy_fill(self, w, side, t_live, price, t_end):
        """First time a resting BUY at ``price`` (live from t_live) fills before t_end, else None."""
        arr = self.ask_arr(w, side)
        i0 = int(np.searchsorted(w.T, t_live, side="left"))
        i1 = int(np.searchsorted(w.T, min(t_end, w.close), side="left"))
        if i1 <= i0:
            return None
        seg = arr[i0:i1]
        m = (seg <= price + 1e-9) if self.touch_fill else (seg < price - 1e-9)
        m &= ~np.isnan(seg)
        if not m.any():
            return None
        return float(w.T[i0 + int(np.argmax(m))])


def stop_sell(ex, w, side, t, cross=0.02, max_tries=40):
    """An urgent taker exit: FOK at bid - cross, retried at the new bid until filled or the window ends.
    Returns (price, time decided-to-filled known, tries) or (None, None, tries) -> settles."""
    tries = 0
    while t < w.close - 500.0 and tries < max_tries:
        b = ex.seen(w, ex.bid_arr(w, side), t)
        tries += 1
        if b is None or b <= 0.0:
            t += 1000.0
            continue
        lim = max(TICK, round(b - cross, 2))
        px, known = ex.taker_sell(w, side, t, lim)
        if px is not None:
            return px, known, tries
        t = known
    return None, None, tries
