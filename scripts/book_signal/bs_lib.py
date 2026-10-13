"""Polymarket order-book direction signal (2026-10-02 study): causal book features on the 200 ms window archive,
a ridge model of the next-1 s token move fitted on TRAIN, and the loaders shared by the study scripts.

Every feature is computed from what the live bot samples every 200 ms (live._sample_prices): both tokens' best
bid / ask and the depth within 2 ticks of each touch (ubd / uad / dbd / dad). All are SIGNED TOWARD UP: + means
the book is pushing the UP token up (or DOWN down).

    vel{k}    change of the UP-equivalent mid over k samples, cents
    acons{k}  ask consumption: UP asks taken or pulled at the touch minus DOWN's (shares, signed log)
    bcons{k}  bid consumption: DOWN bids hit / pulled minus UP's (shares, signed log)
    newliq{k} new bid depth minus new ask depth, UP minus DOWN (shares, signed log)
    imb       near-touch depth imbalance, (UP bid - UP ask) - (DOWN bid - DOWN ask) over the total
    micro     microprice minus mid, UP minus DOWN (cents)
    spr       UP-minus-DOWN spread change over 1 s (a widening ask side is a reprice in progress)
"""
from __future__ import annotations

import json
import math
import sys

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L   # noqa: E402

ROOT = L.ROOT
OUT = f"{ROOT}/data/replay/book_signal"
LAGS = (1, 2, 5)                 # samples: 200 ms, 400 ms, 1 s
BOOK_FEATS = tuple(f"{n}{k}" for k in LAGS for n in ("vel", "acons", "bcons", "newliq")) + ("imb", "micro", "spr")
HORIZON = 5                      # the model's target: UP mid move over the next 5 samples (1 s), cents


def slog(x):
    return np.sign(x) * np.log1p(np.abs(x))


def _cons(p_now, p_prev, d_now, d_prev, ask):
    """Depth taken (or pulled) at one touch between two samples: the touch moved away -> all of the old touch
    depth; same price -> the depth decrease; moved toward -> nothing."""
    with np.errstate(invalid="ignore"):
        away = (p_now > p_prev + 1e-9) if ask else (p_now < p_prev - 1e-9)
        same = np.abs(p_now - p_prev) < 1e-9
    out = np.where(away, np.nan_to_num(d_prev), np.where(same, np.maximum(0.0, np.nan_to_num(d_prev) - np.nan_to_num(d_now)), 0.0))
    return np.where(np.isnan(p_now) | np.isnan(p_prev), 0.0, out)


def book_features(w, mid=None):
    """{name: array over w's samples}, causal (sample i uses samples <= i)."""
    n = w.n
    T = w.T
    if mid is None:
        mu = L.mid_of(w.ua, w.ub)
        md = L.mid_of(w.da, w.db)
        mid = np.where(np.isnan(mu), 1.0 - md, np.where(np.isnan(md), mu, (mu + 1.0 - md) / 2.0))
    F = {}
    for k in LAGS:
        j = np.arange(n) - k
        ok = (j >= 0)
        jj = np.maximum(j, 0)
        ok &= (T - T[jj]) <= k * 200.0 + 450.0
        v = (mid - mid[jj]) * 100.0
        ac = _cons(w.ua, w.ua[jj], w.uad, w.uad[jj], True) - _cons(w.da, w.da[jj], w.dad, w.dad[jj], True)
        bc = _cons(w.db, w.db[jj], w.dbd, w.dbd[jj], False) - _cons(w.ub, w.ub[jj], w.ubd, w.ubd[jj], False)
        dBu = np.nan_to_num(w.ubd - w.ubd[jj]); dAu = np.nan_to_num(w.uad - w.uad[jj])
        dBd = np.nan_to_num(w.dbd - w.dbd[jj]); dAd = np.nan_to_num(w.dad - w.dad[jj])
        nl = np.maximum(0, dBu) - np.maximum(0, dAu) - (np.maximum(0, dBd) - np.maximum(0, dAd))
        for name, arr in ((f"vel{k}", v), (f"acons{k}", slog(ac)), (f"bcons{k}", slog(bc)), (f"newliq{k}", slog(nl))):
            a = np.where(ok, np.nan_to_num(arr), np.nan)
            F[name] = a
    ub_, ua_, db_, da_ = (np.nan_to_num(x) for x in (w.ubd, w.uad, w.dbd, w.dad))
    tot = np.maximum(1.0, ub_ + ua_ + db_ + da_)
    F["imb"] = ((ub_ - ua_) - (db_ - da_)) / tot
    with np.errstate(invalid="ignore", divide="ignore"):
        mpu = np.where(ub_ + ua_ > 0, (w.ub * ua_ + w.ua * ub_) / (ub_ + ua_), np.nan) - (w.ua + w.ub) / 2.0
        mpd = np.where(db_ + da_ > 0, (w.db * da_ + w.da * db_) / (db_ + da_), np.nan) - (w.da + w.db) / 2.0
    F["micro"] = np.nan_to_num(mpu - mpd) * 100.0
    j5 = np.maximum(np.arange(n) - 5, 0)
    su, sd = w.ua - w.ub, w.da - w.db
    F["spr"] = np.nan_to_num(((sd - sd[j5]) - (su - su[j5])) * 100.0)
    F["mid"] = mid
    return F


def future_move(w, mid, h=HORIZON):
    """UP mid change over the next h samples (cents), NaN when the gap is too long or past the window."""
    n = w.n
    j = np.minimum(np.arange(n) + h, n - 1)
    ok = (np.arange(n) + h < n) & ((w.T[j] - w.T) <= h * 200.0 + 450.0)
    y = (mid[j] - mid) * 100.0
    return np.where(ok, y, np.nan)


def design(B, idx=None):
    return np.column_stack([B[k] if idx is None else B[k][idx] for k in BOOK_FEATS])


def fit_ridge(X, y, l2=50.0):
    mu = np.nanmean(X, 0)
    sd = np.nanstd(X, 0) + 1e-9
    Z = (np.where(np.isnan(X), mu, X) - mu) / sd
    Z = np.hstack([np.ones((len(Z), 1)), Z])
    reg = l2 * np.r_[0.0, np.ones(Z.shape[1] - 1)]
    wgt = np.linalg.solve(Z.T @ Z + np.diag(reg), Z.T @ y)
    return {"features": list(BOOK_FEATS), "mu": mu.tolist(), "sd": sd.tolist(), "w": wgt.tolist(), "l2": l2,
            "target": f"UP mid move over the next {HORIZON} samples (cents)"}


def predict(model, X):
    mu, sd, w = np.asarray(model["mu"]), np.asarray(model["sd"]), np.asarray(model["w"])
    Z = (np.where(np.isnan(X), mu, X) - mu) / sd
    return w[0] + Z @ w[1:]


def load_btc():
    """BTC windows from the new cache with causal sigma + the paired-exit features. Splits: windows that were in
    the paired-exit study's cache keep that study's split; the newer ones are 'forward2' (never seen by anything)."""
    old = json.loads(str(np.load(f"{L.CACHE}/btc.npz", allow_pickle=False)["meta"]))
    n_old = len(old)
    split_of = {}
    for k, m in enumerate(old):
        split_of[m["slug"]] = next(s for s, lo, hi in L.SPLITS if int(lo * n_old) <= k < int(hi * n_old)
                                   or (hi == 1.0 and k >= int(lo * n_old)))
    keep = L.CACHE
    L.CACHE = OUT
    try:
        wins = L.load_asset("BTC")
    finally:
        L.CACHE = keep
    for w in wins:
        w.split = split_of.get(w.slug, "forward2")
    return L.prepare(wins)
