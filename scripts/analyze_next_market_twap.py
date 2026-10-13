"""Research, stage 2: NEXT-MARKET TWAP predictions, execution and verdict.

    python scripts/analyze_next_market_twap.py grid        # 50 ms as-of book/spot grid around every open
    python scripts/analyze_next_market_twap.py diag        # TRAIN-only diagnostics (information, markouts)
    python scripts/analyze_next_market_twap.py models      # baselines A-F, walk-forward, calibration
    python scripts/analyze_next_market_twap.py sim         # execution grid on TRAIN/VALIDATION
    python scripts/analyze_next_market_twap.py lock        # choose the research configuration (no TEST)
    python scripts/analyze_next_market_twap.py test        # the locked configuration on TEST (+ FORWARD)

RESEARCH ONLY; reads data/replay/next_market/ (built by research_next_market_twap.py) and writes
under it and results/. No strategy configuration or running process is touched.

The grid
--------
For every market, both tokens' touch and 2-tick depth and the composite spot, each "as of" (the last
archived point at or before) every 50 ms from 10 s before the open to 60 s after it. The archive is
sampled every ~224 ms, so the grid adds no information; it only makes every latency, hold and exit
a vectorised index. A lookup at time T reads what this machine had at T.

Execution (the project's venue_lag model, backtest/spot_shock_replay.Execution)
-------------------------------------------------------------------------------
decision at t_d on the as-of book -> the order reaches the venue L later (L drawn from the MEASURED
presend and RTT/2 samples, data/replay/live_latency_samples.json) -> it is matched against the
archived book at t_d + L + PROXY (500 ms: the offset that reproduced venue-truth P&L on 694
spot-shock orders). The explicit delay grid instead reads the book at t_d + D with no proxy.
Buys fill at the arrival VWAP within the limit (3-level depth model), sells FOK at bid - 2c retried
every 2 s. Taker fee rate * p * (1 - p) on every fill.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from scipy.special import stdtr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import research_next_market_twap as RB  # noqa: E402

OUT = RB.OUT
RES = ROOT / "results"
G0_S, G1_S, GSTEP_S = -10.0, 60.0, 0.05
GS = np.round(np.arange(G0_S, G1_S + 1e-9, GSTEP_S), 3)
NG = len(GS)
PX = ("ub", "ua", "db", "da")
SZ = ("ubd", "uad", "dbd", "dad")
NU = 4.0
T_SCALE = math.sqrt(NU / (NU - 2.0))


def gi(s):
    """Grid index of an offset (s after the open), clipped to the grid."""
    return np.clip(np.round((np.asarray(s, dtype=np.float64) - G0_S) / GSTEP_S).astype(np.int64), 0, NG - 1)


def t4(z):
    return stdtr(NU, np.asarray(z, dtype=np.float64) * T_SCALE)


# ─────────────────────────────── the grid ───────────────────────────────


def _grid_asset(asset: str):
    A = RB.Asset(asset)
    n = len(A.meta)
    px = {k: np.full((n, NG), -1, dtype=np.int16) for k in PX}
    sz = {k: np.full((n, NG), np.nan, dtype=np.float32) for k in SZ}
    age = np.full((n, NG), np.nan, dtype=np.float32)
    xs = np.full((n, NG), np.nan, dtype=np.float64)
    rows = []
    for k, m in enumerate(A.meta):
        o = float(m[2])
        b0, b1 = int(m[7]), int(m[8])
        tq = o + GS * 1000.0
        if b1 > b0:
            bt = A.bt[b0:b1]
            j = np.searchsorted(bt, tq, side="right") - 1
            ok = j >= 0
            jj = b0 + np.maximum(j, 0)
            for key in PX:
                v = np.round(A.px[key][jj] * 100.0)
                px[key][k] = np.where(ok & ~np.isnan(v), v, -1).astype(np.int16)
            for key in SZ:
                sz[key][k] = np.where(ok, A.sz[key][jj], np.nan)
            age[k] = np.where(ok, (tq - A.bt[jj]) / 1000.0, np.nan)
        j = np.searchsorted(A.st, tq, side="right") - 1
        ok = (j >= 0) & (tq - A.st[np.maximum(j, 0)] <= 5000.0)
        xs[k] = np.where(ok, A.lx[np.maximum(j, 0)], np.nan)
        rows.append((RB.ASSETS.index(asset), int(m[1]), k, o, int(m[4]), float(m[5]), float(m[6])))
    return asset, px, sz, age, xs, np.asarray(rows, dtype=np.float64)


def build_grid(procs: int = 3) -> None:
    parts = {}
    with ProcessPoolExecutor(procs) as pool:
        for asset, px, sz, age, xs, rows in pool.map(_grid_asset, RB.ASSETS):
            parts[asset] = (px, sz, age, xs, rows)
            print(f"  {asset}: {len(rows)} markets", flush=True)
    out = {}
    for k in PX:
        out[k] = np.concatenate([parts[a][0][k] for a in RB.ASSETS])
    for k in SZ:
        out[k] = np.concatenate([parts[a][1][k] for a in RB.ASSETS])
    out["age"] = np.concatenate([parts[a][2] for a in RB.ASSETS])
    out["x"] = np.concatenate([parts[a][3] for a in RB.ASSETS])
    out["mk"] = np.concatenate([parts[a][4] for a in RB.ASSETS])     # asset, epoch, k, open, won, strike, fee
    np.savez(OUT / "grid.npz", **out)


# ─────────────────────────────── loading ───────────────────────────────


class Data:
    """The boundary dataset (one row per market x prediction offset) joined to the grid (one row per
    market). ``g`` maps each dataset row to its grid row."""

    def __init__(self) -> None:
        z = np.load(OUT / "dataset.npz", allow_pickle=True)
        self.X = z["X"]
        self.M = z["M"]
        self.cols = [str(c) for c in z["cols"]]
        self.c = {k: i for i, k in enumerate(self.cols)}
        gz = np.load(OUT / "grid.npz")
        self.px = {k: gz[k] for k in PX}
        self.sz = {k: gz[k] for k in SZ}
        self.age = gz["age"]
        self.x = gz["x"]
        self.mk = gz["mk"]
        start, off = 0, {}
        for a in range(len(RB.ASSETS)):
            off[a] = start
            start += int((self.mk[:, 0] == a).sum())
        self.g = np.array([off[int(a)] + int(k) for a, k in zip(self.M[:, 0], self.M[:, 2])], dtype=np.int64)
        split = RB.split_of(self.mk[:, 1].astype(int))
        self.gsplit = np.array([RB.SPLIT_NAMES.index(split[int(e)]) for e in self.mk[:, 1]])
        self.asset = self.M[:, 0].astype(int)
        self.epoch = self.M[:, 1].astype(int)
        self.tau = self.M[:, 3]
        self.y = self.M[:, 4]
        self.split = self.M[:, 5].astype(int)
        self.rate = self.M[:, 6]

    def f(self, name: str) -> np.ndarray:
        return self.X[:, self.c[name]]

    def price(self, key: str, rows: np.ndarray, s) -> np.ndarray:
        """A token price (dollars, NaN if no quote) for dataset rows at offset(s) ``s`` after the open."""
        v = self.px[key][self.g[rows], gi(s)].astype(np.float64)
        return np.where(v < 0, np.nan, v / 100.0)

    def depth(self, key: str, rows: np.ndarray, s) -> np.ndarray:
        return self.sz[key][self.g[rows], gi(s)].astype(np.float64)

    def mkt_p(self, rows: np.ndarray, s) -> np.ndarray:
        """The market's P(UP) at offset s: the Up mid, else 1 - the Down mid."""
        ub, ua = self.price("ub", rows, s), self.price("ua", rows, s)
        db, da = self.price("db", rows, s), self.price("da", rows, s)
        up = (ub + ua) / 2.0
        dn = 1.0 - (db + da) / 2.0
        return np.where(~np.isnan(up), up, dn)


# ─────────────────────────────── statistics ───────────────────────────────


def logit(p):
    p = np.clip(np.asarray(p, dtype=np.float64), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def sigm(z):
    return 1.0 / (1.0 + np.exp(-np.asarray(z, dtype=np.float64)))


def fit_logistic(X: np.ndarray, y: np.ndarray, lam: float = 1.0, iters: int = 50, w: np.ndarray | None = None):
    """L2-penalised logistic regression by Newton steps (intercept unpenalised). Returns (beta, cov)."""
    n, k = X.shape
    Xa = np.column_stack([np.ones(n), X])
    b = np.zeros(k + 1)
    P = lam * np.eye(k + 1)
    P[0, 0] = 0.0
    w = np.ones(n) if w is None else w
    for _ in range(iters):
        p = sigm(Xa @ b)
        W = w * p * (1 - p)
        H = Xa.T @ (Xa * W[:, None]) + P
        gr = Xa.T @ (w * (y - p)) - P @ b
        step = np.linalg.solve(H, gr)
        b += step
        if np.max(np.abs(step)) < 1e-8:
            break
    p = sigm(Xa @ b)
    H = Xa.T @ (Xa * (w * p * (1 - p))[:, None]) + P
    return b, np.linalg.inv(H)


def pred_logistic(b, X):
    return sigm(np.column_stack([np.ones(len(X)), X]) @ b)


def brier(p, y):
    return float(np.mean((p - y) ** 2))


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def ece(p, y, bins: int = 10) -> float:
    """Expected calibration error over equal-count bins."""
    if len(p) < bins:
        return float("nan")
    o = np.argsort(p)
    tot = 0.0
    for part in np.array_split(o, bins):
        tot += len(part) * abs(p[part].mean() - y[part].mean())
    return float(tot / len(p))


def acc(p, y):
    return float(np.mean((p >= 0.5) == (y == 1)))


def clustered(x: np.ndarray, g: np.ndarray) -> tuple[float, float]:
    """Mean and its t-statistic with errors clustered by ``g`` (the 5-minute epoch: every asset of one
    boundary is one bet)."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < 2:
        return (float(x.mean()) if len(x) else float("nan")), float("nan")
    m = x.mean()
    ug, inv = np.unique(g, return_inverse=True)
    s = np.bincount(inv, weights=x - m)
    se = math.sqrt(np.sum(s * s)) / len(x) * math.sqrt(len(ug) / max(len(ug) - 1, 1))
    return float(m), float(m / se) if se > 0 else float("nan")


def boot_ci(x: np.ndarray, g: np.ndarray, reps: int = 2000, seed: int = 5) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    ug, inv = np.unique(g, return_inverse=True)
    sums = np.bincount(inv, weights=x)
    cnts = np.bincount(inv)
    idx = rng.integers(0, len(ug), size=(reps, len(ug)))
    means = sums[idx].sum(1) / np.maximum(cnts[idx].sum(1), 1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def fee(rate, p):
    p = np.asarray(p, dtype=np.float64)
    return rate * p * (1.0 - p)


def md_table(header: list[str], rows: list[list]) -> str:
    def fmt(v):
        if isinstance(v, float):
            if v != v:
                return "—"
            return f"{v:.4g}" if abs(v) < 1e4 else f"{v:,.0f}"
        return str(v)
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---:" if i else "---" for i in range(len(header))) + "|"]
    out += ["| " + " | ".join(fmt(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


# ─────────────────────────────── TRAIN-only diagnostics ───────────────────────────────

AFTER_S = (0.0, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0, 60.0)


def diag(D: Data, splits=(0,)) -> str:
    """Does the pre-open prediction carry information the market price does not, and does the token
    move toward the model after the open? TRAIN only unless told otherwise."""
    lines = [f"# Diagnostics on {'+'.join(RB.SPLIT_NAMES[s] for s in splits)}\n"]
    base = np.isin(D.split, splits) & (D.y >= 0)
    pD = D.f("p_twap")
    # 1. accuracy / calibration by prediction time, model vs the market's own price then
    rows = []
    for tau in (-10.0, -5.0, -3.0, -2.0, -1.0, 0.0):
        m = base & (D.tau == tau) & ~np.isnan(D.f("mkt_p"))
        y, p, q = D.y[m], pD[m], D.f("mkt_p")[m]
        rows.append([f"{tau:+.0f}s", int(m.sum()), acc(p, y), brier(p, y), logloss(p, y), ece(p, y),
                     acc(q, y), brier(q, y), float(np.corrcoef(p, q)[0, 1])])
    lines.append("## Model D (analytic TWAP) vs the market's pre-open mid, by prediction time\n")
    lines.append(md_table(["pred time", "n", "D acc", "D Brier", "D logloss", "D ECE", "mkt acc", "mkt Brier",
                           "corr(D, mkt)"], rows))
    # 2. the market after the open, from the T-3 rows
    m3 = base & (D.tau == -3.0)
    r3 = np.where(m3)[0]
    rows = []
    for h in AFTER_S:
        q = D.mkt_p(r3, h)
        ok = ~np.isnan(q)
        y, p = D.y[r3][ok], pD[r3][ok]
        X = np.column_stack([logit(q[ok]), logit(p)])
        b, cov = fit_logistic(X, y, lam=1e-6)
        rows.append([f"+{h:g}s", int(ok.sum()), acc(q[ok], y), brier(q[ok], y), float(np.corrcoef(p, q[ok])[0, 1]),
                     b[1], b[2], b[2] / math.sqrt(cov[2, 2])])
    lines.append("\n## The market after the open vs the T-3 s model\n")
    lines.append("logistic y ~ logit(market) + logit(model D): a model coefficient > 0 means the prediction "
                 "carries information the market price at that moment does not.\n")
    lines.append(md_table(["market at", "n", "mkt acc", "mkt Brier", "corr(D, mkt)", "coef mkt", "coef D", "z(D)"], rows))
    # 3. convergence: how much of the model-vs-market gap closes after the open
    rows = []
    q0 = D.mkt_p(r3, 0.0)
    gap = pD[r3] - q0
    for h in AFTER_S[1:]:
        dq = D.mkt_p(r3, h) - q0
        ok = ~np.isnan(dq) & ~np.isnan(gap)
        k = float(np.sum(dq[ok] * gap[ok]) / np.sum(gap[ok] ** 2))
        big = ok & (np.abs(gap) >= 0.05)
        rows.append([f"+{h:g}s", int(ok.sum()), k, float(np.corrcoef(dq[ok], gap[ok])[0, 1]),
                     float(np.mean(dq[big] * np.sign(gap[big]))) * 100.0, int(big.sum())])
    lines.append("\n## Convergence after the open (T-3 s model, market mid at the open as the base)\n")
    lines.append("kappa = OLS slope of the mid change on the gap (model D - mid at open). Last columns: mean mid "
                 "move toward the model, in cents, where |gap| >= 5c.\n")
    lines.append(md_table(["horizon", "n", "kappa", "corr", "move toward model (c), |gap|>=5c", "n"], rows))
    # 4. the raw markout curve: buy the model's side at the open ask, mark to the bid later
    rows = []
    up = pD[r3] >= 0.5
    ask0 = np.where(up, D.price("ua", r3, 0.0), D.price("da", r3, 0.0))
    for h in AFTER_S[1:]:
        bid = np.where(up, D.price("ub", r3, h), D.price("db", r3, h))
        mo = bid - ask0
        ok = ~np.isnan(mo)
        net = mo - fee(D.rate[r3], ask0) - fee(D.rate[r3], bid)
        mean, t = clustered(mo[ok], D.epoch[r3][ok])
        nmean, nt = clustered(net[ok], D.epoch[r3][ok])
        rows.append([f"+{h:g}s", int(ok.sum()), mean * 100, t, nmean * 100, nt, float(np.mean(mo[ok] > 0))])
    lines.append("\n## Markout of buying the model's side at the open ask (T-3 s model, every market, no filter)\n")
    lines.append(md_table(["hold", "n", "markout c", "t", "net c", "t", "P(markout>0)"], rows))
    return "\n".join(lines)


# ─────────────────────────────── prediction models (baselines A-F) ───────────────────────────────

MOM_LAGS = (1000, 3000, 5000, 10000, 30000, 60000)
RET_FEATS = ("z250", "z500", "z1000", "z2000", "z3000", "z5000", "z10000", "z30000", "z60000")
FULL_FEATS = RET_FEATS + ("z_disp", "z_ema", "accel_n", "run_ticks", "vol_regime", "rv1_n", "rv5_n", "rv10_n",
                          "rv30_n", "tfi", "obi", "ofi", "rate_rel", "lead_bn", "lead_byb", "lead_cb", "dev_bn",
                          "dev_byb", "dev_cb", "xdisp_bps", "prev_z", "prev2_up", "log_sigma")
MODEL_NAMES = ("A_momentum", "B_returns", "C_ema", "D_twap", "D_recal", "E_logistic", "F_gbm", "M_market",
               "EM_logistic_mkt")


def feature_matrix(D: Data, names) -> np.ndarray:
    s = D.f("sigma")
    derived = {
        "accel_n": D.f("accel") / s, "log_sigma": np.log(s),
        "rv1_n": D.f("rv1") / s, "rv5_n": D.f("rv5") / s, "rv10_n": D.f("rv10") / s, "rv30_n": D.f("rv30") / s,
        "prev2_up": np.where(D.f("prev2_up") < 0, 0.0, D.f("prev2_up") - 0.5),
        "logit_mkt": logit(D.f("mkt_p")),
    }
    return np.column_stack([derived[n] if n in derived else D.f(n) for n in names])


class Prep:
    """Median fill, 1/99 % clip and standardisation, fitted on the training rows only."""

    def __init__(self, X: np.ndarray) -> None:
        self.med = np.nanmedian(X, axis=0)
        Xf = np.where(np.isnan(X), self.med, X)
        self.lo, self.hi = np.percentile(Xf, 1, axis=0), np.percentile(Xf, 99, axis=0)
        Xc = np.clip(Xf, self.lo, self.hi)
        self.mu, self.sd = Xc.mean(0), Xc.std(0) + 1e-12

    def __call__(self, X: np.ndarray) -> np.ndarray:
        Xf = np.where(np.isnan(X), self.med, X)
        return (np.clip(Xf, self.lo, self.hi) - self.mu) / self.sd


class GBM:
    """Small histogram gradient-boosted trees for the logistic loss (no sklearn on this machine):
    depth-limited trees on quantile bins, shrinkage, row subsampling, an L2 leaf penalty."""

    def __init__(self, rounds: int = 150, lr: float = 0.05, depth: int = 3, min_leaf: int = 60,
                 lam: float = 5.0, bins: int = 32, subsample: float = 0.8, seed: int = 3) -> None:
        self.rounds, self.lr, self.depth, self.min_leaf = rounds, lr, depth, min_leaf
        self.lam, self.bins, self.subsample, self.seed = lam, bins, subsample, seed

    def _bin(self, X):
        return np.stack([np.searchsorted(self.edges[j], X[:, j], side="right") for j in range(X.shape[1])], 1)

    def fit(self, X, y):
        Xf = np.where(np.isnan(X), np.nanmedian(X, 0), X)
        self.fill = np.nanmedian(X, 0)
        self.edges = [np.unique(np.quantile(Xf[:, j], np.linspace(0, 1, self.bins + 1)[1:-1])) for j in range(X.shape[1])]
        B = self._bin(Xf)
        rng = np.random.default_rng(self.seed)
        self.base = float(logit(np.clip(y.mean(), 1e-3, 1 - 1e-3)))
        F = np.full(len(y), self.base)
        self.trees = []
        for _ in range(self.rounds):
            p = sigm(F)
            g, h = p - y, p * (1 - p)
            use = rng.random(len(y)) < self.subsample
            tree = self._grow(B, g, h, np.where(use)[0], 0)
            self.trees.append(tree)
            F += self.lr * self._apply(tree, B)
        return self

    def _grow(self, B, g, h, idx, d):
        G, H = g[idx].sum(), h[idx].sum()
        leaf = ("leaf", -G / (H + self.lam))
        if d >= self.depth or len(idx) < 2 * self.min_leaf:
            return leaf
        best = (0.0, None)
        base = G * G / (H + self.lam)
        for j in range(B.shape[1]):
            nb = len(self.edges[j]) + 1
            gs = np.bincount(B[idx, j], weights=g[idx], minlength=nb)
            hs = np.bincount(B[idx, j], weights=h[idx], minlength=nb)
            cs = np.bincount(B[idx, j], minlength=nb)
            GL, HL, CL = np.cumsum(gs)[:-1], np.cumsum(hs)[:-1], np.cumsum(cs)[:-1]
            ok = (CL >= self.min_leaf) & (len(idx) - CL >= self.min_leaf)
            if not ok.any():
                continue
            gain = GL ** 2 / (HL + self.lam) + (G - GL) ** 2 / (H - HL + self.lam) - base
            gain = np.where(ok, gain, -np.inf)
            k = int(np.argmax(gain))
            if gain[k] > best[0]:
                best = (float(gain[k]), (j, k))
        if best[1] is None:
            return leaf
        j, k = best[1]
        left = idx[B[idx, j] <= k]
        right = idx[B[idx, j] > k]
        return ("split", j, k, self._grow(B, g, h, left, d + 1), self._grow(B, g, h, right, d + 1))

    def _apply(self, tree, B):
        out = np.empty(len(B))
        stack = [(tree, np.arange(len(B)))]
        while stack:
            node, idx = stack.pop()
            if node[0] == "leaf":
                out[idx] = node[1]
                continue
            _, j, k, l, r = node
            m = B[idx, j] <= k
            stack.append((l, idx[m]))
            stack.append((r, idx[~m]))
        return out

    def predict(self, X):
        Xf = np.where(np.isnan(X), self.fill, X)
        B = self._bin(Xf)
        F = np.full(len(X), self.base)
        for t in self.trees:
            F += self.lr * self._apply(t, B)
        return sigm(F)


def _fit_predict(name: str, D: Data, tr: np.ndarray, te: np.ndarray, cache: dict) -> np.ndarray:
    """Fit model ``name`` on rows ``tr`` (outcomes known) and predict rows ``te``."""
    y = D.y[tr]
    if name == "D_twap":
        return D.f("p_twap")[te]
    if name == "M_market":
        return D.f("mkt_p")[te]
    if name == "D_recal":
        b, _ = fit_logistic(logit(D.f("p_twap")[tr])[:, None], y, lam=1e-3)
        return pred_logistic(b, logit(D.f("p_twap")[te])[:, None])
    if name == "A_momentum":
        # the lookback is chosen inside the training rows (the lowest in-sample log loss)
        best = None
        for lag in MOM_LAGS:
            x = np.nan_to_num(D.f(f"z{lag}")[tr])[:, None]
            b, _ = fit_logistic(x, y, lam=1e-3)
            ll = logloss(pred_logistic(b, x), y)
            if best is None or ll < best[0]:
                best = (ll, lag, b)
        cache.setdefault("A_lag", []).append(best[1])
        return pred_logistic(best[2], np.nan_to_num(D.f(f"z{best[1]}")[te])[:, None])
    if name == "C_ema":
        x = np.nan_to_num(D.f("z_ema"))[:, None]
        b, _ = fit_logistic(x[tr], y, lam=1e-3)
        return pred_logistic(b, x[te])
    feats = {"B_returns": RET_FEATS, "E_logistic": FULL_FEATS, "F_gbm": FULL_FEATS,
             "EM_logistic_mkt": FULL_FEATS + ("logit_mkt",)}[name]
    X = feature_matrix(D, feats)
    if name == "F_gbm":
        return GBM().fit(X[tr], y).predict(X[te])
    pr = Prep(X[tr])
    b, _ = fit_logistic(pr(X[tr]), y, lam=float(len(tr)) * 0.01)
    return pred_logistic(b, pr(X[te]))


def model_predictions(D: Data, final: bool = False) -> dict[str, np.ndarray]:
    """Out-of-sample probabilities for every dataset row, per model, fitted per prediction time:
    TRAIN rows cross-fitted (epoch halves), VALIDATION rows from a fit on all of TRAIN. With
    ``final`` the TEST and FORWARD rows get a fit on TRAIN + VALIDATION (read once, after the lock)."""
    out = {n: np.full(len(D.y), np.nan) for n in MODEL_NAMES}
    cache: dict = {}
    for tau in np.unique(D.tau):
        at = (D.tau == tau)
        tr = np.where(at & (D.split == 0) & (D.y >= 0))[0]
        half = np.median(D.epoch[tr])
        a, b = tr[D.epoch[tr] <= half], tr[D.epoch[tr] > half]
        va = np.where(at & (D.split == 1))[0]
        te = np.where(at & (D.split >= 2))[0]            # TEST and FORWARD
        trva = np.where(at & (D.split <= 1) & (D.y >= 0))[0]
        for n in MODEL_NAMES:
            out[n][b] = _fit_predict(n, D, a, b, cache)
            out[n][a] = _fit_predict(n, D, b, a, cache)
            out[n][va] = _fit_predict(n, D, tr, va, cache)
            if final:
                out[n][te] = _fit_predict(n, D, trva, te, cache)
    return out


def walk_forward(D: Data, names=MODEL_NAMES, n_folds: int = 5, first: float = 0.4) -> dict:
    """Expanding-window walk-forward over TRAIN + VALIDATION epochs: each fold is predicted by a fit on
    every earlier epoch. Returns {(name, tau): (y, p)} over all folds."""
    eps = np.unique(D.epoch[(D.split <= 1)])
    cut = [int(len(eps) * (first + (1 - first) * k / n_folds)) for k in range(n_folds + 1)]
    res = {}
    for tau in np.unique(D.tau):
        for n in names:
            ys, ps = [], []
            for k in range(n_folds):
                lo, hi = eps[cut[k]], eps[min(cut[k + 1], len(eps) - 1)]
                tr = np.where((D.tau == tau) & (D.split <= 1) & (D.y >= 0) & (D.epoch < lo))[0]
                te = np.where((D.tau == tau) & (D.split <= 1) & (D.y >= 0) & (D.epoch >= lo) &
                              ((D.epoch < hi) if k < n_folds - 1 else (D.epoch <= hi)))[0]
                p = _fit_predict(n, D, tr, te, {})
                ok = ~np.isnan(p)
                ys.append(D.y[te][ok])
                ps.append(p[ok])
            res[(n, float(tau))] = (np.concatenate(ys), np.concatenate(ps))
    return res


def models_report(D: Data) -> str:
    preds = model_predictions(D)
    np.savez(OUT / "preds.npz", **preds)
    wf = walk_forward(D)
    lines = ["# Prediction models (TEST untouched)\n",
             "TRAIN = cross-fitted out-of-fold; VALIDATION = fit on TRAIN; WALK-FORWARD = 5 expanding folds over "
             "the last 60 % of TRAIN + VALIDATION epochs. Coin flip: Brier 0.25, log loss 0.693.\n"]
    for tau in sorted(np.unique(D.tau)):
        rows = []
        for n in MODEL_NAMES:
            r = []
            for sp in (0, 1):
                m = (D.tau == tau) & (D.split == sp) & (D.y >= 0) & ~np.isnan(preds[n])
                y, p = D.y[m], preds[n][m]
                r += [acc(p, y), brier(p, y), logloss(p, y), ece(p, y)]
            y, p = wf[(n, float(tau))]
            r += [acc(p, y), brier(p, y), logloss(p, y), ece(p, y), len(y)]
            rows.append([n] + r)
        lines.append(f"\n## Prediction time {tau:+.0f} s\n")
        lines.append(md_table(["model", "TR acc", "TR Brier", "TR logloss", "TR ECE", "VA acc", "VA Brier",
                               "VA logloss", "VA ECE", "WF acc", "WF Brier", "WF logloss", "WF ECE", "WF n"], rows))
    # per asset at T-3 s, walk-forward rows
    lines.append("\n## Per asset at T-3 s (TRAIN out-of-fold + VALIDATION)\n")
    rows = []
    for a, name in enumerate(RB.ASSETS):
        m = (D.tau == -3.0) & (D.split <= 1) & (D.y >= 0) & (D.asset == a)
        r = [name, int(m.sum())]
        for n in ("D_twap", "D_recal", "E_logistic", "F_gbm", "M_market"):
            ok = m & ~np.isnan(preds[n])
            r += [acc(preds[n][ok], D.y[ok]), brier(preds[n][ok], D.y[ok])]
        rows.append(r)
    lines.append(md_table(["asset", "n", "D acc", "D Brier", "Drecal acc", "Drecal Brier", "E acc", "E Brier",
                           "F acc", "F Brier", "mkt acc", "mkt Brier"], rows))
    return "\n".join(lines)


# ─────────────────────────────── execution simulation ───────────────────────────────

LAT_SAMPLES = json.loads((ROOT / "data/replay/live_latency_samples.json").read_text())
PROXY_MS = 500.0
TOUCH_FRAC = 0.25
LEVEL_FRAC = (TOUCH_FRAC, (1 - TOUCH_FRAC) / 2, (1 - TOUCH_FRAC) / 2)
MIN_SHARES = 5.0
N_EXIT_TRIES = 12
HOLDS_S = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 7.5, 10.0, 15.0, 30.0)


@dataclass
class Cfg:
    model: str = "D_twap"
    tau: float = -3.0
    #: scan window after the open in seconds (0 = only at the open); -1 = buy at the prediction time
    win: float = 0.0
    min_edge: float = -9.0             # -9 = no edge gate (direction only)
    min_prob: float = 0.5
    max_spread: float = 9.0
    max_ask: float = 0.99
    min_ask: float = 0.02
    usd: float = 5.0
    depth_frac: float = 1.0            # the order must be <= this fraction of the 2-tick ask depth
    entry: str = "fok0"                # fok0 | fak0 | fok1 | fok2 | fokm (limit at the edge's max price)
    qmove: float = 9.0                 # skip if the ask rose more than this since the prediction time
    lat: str = "real"                  # real | d<ms>
    lat_scale: float = 1.0
    lat_extra: float = 0.0             # extra one-way delay per order, uniform on [0, lat_extra] ms
    edge_hold: float = 3.0             # the hold the edge formula prices (s); 0 = settlement
    adverse: float = 0.005
    sides: str = "both"                # both | UP | DOWN

    def key(self) -> tuple:
        return tuple(getattr(self, f) for f in CFG_FIELDS)


CFG_FIELDS = ("model", "tau", "win", "min_edge", "min_prob", "max_spread", "max_ask", "min_ask", "usd", "depth_frac",
              "entry", "qmove", "lat", "lat_scale", "lat_extra", "edge_hold", "adverse", "sides")


class Sim:
    """Everything that does not depend on the configuration: the rows, their sides' prices on the grid,
    the latency draws (common random numbers across configurations) and the TRAIN estimates the edge
    formula uses (kappa by decision offset and hold, the book's half spread by offset)."""

    def __init__(self, D: Data, preds: dict, seed: int = 12345) -> None:
        self.D, self.P = D, preds
        n = len(D.y)
        rng = np.random.default_rng(seed)
        pre, rtt = np.asarray(LAT_SAMPLES["presend_ms"]), np.asarray(LAT_SAMPLES["venue_rtt_ms"])
        self.pre = rng.choice(pre, size=(n, 1 + N_EXIT_TRIES)) * rng.uniform(.85, 1.15, (n, 1 + N_EXIT_TRIES))
        self.rtt = rng.choice(rtt, size=(n, 1 + N_EXIT_TRIES)) * rng.uniform(.85, 1.15, (n, 1 + N_EXIT_TRIES))
        self.u_noise = rng.random(n)
        self._estimate(D)

    def _estimate(self, D: Data) -> None:
        tr = np.where((D.split == 0) & (D.tau == -3.0) & (D.y >= 0))[0]
        pD = D.f("p_twap")[tr]
        self.k_offsets = np.array([0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0])
        self.k_holds = np.array((0.0,) + HOLDS_S)
        kap = np.zeros((len(self.k_offsets), len(self.k_holds)))
        for i, e in enumerate(self.k_offsets):
            q0 = D.mkt_p(tr, e)
            for j, h in enumerate(self.k_holds):
                dq = D.mkt_p(tr, e + h) - q0
                gap = pD - q0
                ok = ~np.isnan(dq) & ~np.isnan(gap)
                kap[i, j] = max(0.0, float(np.sum(dq[ok] * gap[ok]) / max(np.sum(gap[ok] ** 2), 1e-12))) if h > 0 else 0.0
        self.kappa = kap
        offs = np.round(np.arange(-10.0, 60.01, 0.25), 2)
        hs = []
        for o in offs:
            sp = np.concatenate([D.price("ua", tr, o) - D.price("ub", tr, o), D.price("da", tr, o) - D.price("db", tr, o)])
            hs.append(float(np.nanmedian(sp)) / 2.0)
        self.hs_off, self.hs = offs, np.asarray(hs)

    def kappa_at(self, e: np.ndarray, h: float) -> np.ndarray:
        i = np.clip(np.searchsorted(self.k_offsets, e, side="right") - 1, 0, len(self.k_offsets) - 1)
        j = int(np.argmin(np.abs(self.k_holds - h)))
        return self.kappa[i, j]

    def half_spread_at(self, s: np.ndarray) -> np.ndarray:
        return np.interp(s, self.hs_off, self.hs)

    # ── helpers ──

    def side_px(self, rows, up, s, what: str) -> np.ndarray:
        """ask / bid (dollars) or adep / bdep of the chosen side at offsets ``s`` (per row, or rows x K)."""
        D = self.D
        ku, kd = {"ask": ("ua", "da"), "bid": ("ub", "db"), "adep": ("uad", "dad"), "bdep": ("ubd", "dbd")}[what]
        g = D.g[rows]
        idx = gi(s)
        if idx.ndim == 2:
            g = g[:, None]
            upb = up[:, None]
        else:
            upb = up
        if what in ("ask", "bid"):
            a = D.px[ku][g, idx].astype(np.float64)
            b = D.px[kd][g, idx].astype(np.float64)
            v = np.where(upb, a, b)
            return np.where(v < 0, np.nan, v / 100.0)
        return np.where(upb, D.sz[ku][g, idx], D.sz[kd][g, idx]).astype(np.float64)

    def latency(self, rows, cfg: Cfg, k: int = 0):
        """(one-way ms, ack ms, proxy ms) of order attempt k (0 = the entry)."""
        if cfg.lat == "real":
            ow = self.pre[rows, k] + self.rtt[rows, k] / 2.0
            ack = self.rtt[rows, k] / 2.0
            px = PROXY_MS
        else:
            ms = float(cfg.lat[1:])
            ow = np.full(len(rows), ms)
            ack = np.full(len(rows), ms)
            px = 0.0
        if cfg.lat_extra > 0:
            ow = ow + cfg.lat_extra * ((self.u_noise[rows] * 7.31 + k * 0.618) % 1.0)
        return ow * cfg.lat_scale, ack * cfg.lat_scale, px * cfg.lat_scale


def _fill_buy(ask, dep, limit, shares, partial: bool):
    """(filled shares, VWAP) against a 3-level book: asks at ask, +1c, +2c within the limit."""
    qty = np.zeros(len(ask))
    cost = np.zeros(len(ask))
    ok = ~np.isnan(ask) & ~np.isnan(dep)
    for k, fr in enumerate(LEVEL_FRAC):
        px = ask + 0.01 * k
        avail = np.where(ok & (px <= limit + 1e-9), fr * np.nan_to_num(dep), 0.0)
        take = np.minimum(avail, shares - qty)
        qty += take
        cost += take * np.nan_to_num(px)
    full = qty >= shares - 1e-9
    q = np.where(full | (partial & (qty > 0)), qty, 0.0)
    vw = np.where(q > 0, cost / np.maximum(qty, 1e-12), np.nan)
    return q, vw


def _fill_sell(bid, dep, floor, qty):
    got = np.zeros(len(bid))
    cost = np.zeros(len(bid))
    ok = ~np.isnan(bid) & ~np.isnan(dep)
    for k, fr in enumerate(LEVEL_FRAC):
        px = bid - 0.01 * k
        avail = np.where(ok & (px >= floor - 1e-9), fr * np.nan_to_num(dep), 0.0)
        take = np.minimum(avail, qty - got)
        got += take
        cost += take * np.nan_to_num(px)
    full = got >= qty - 1e-9
    return full, np.where(full, cost / np.maximum(got, 1e-12), np.nan)


def edge_matrix(S: Sim, cfg: Cfg, e, ask, bid, dep, p_side, rate, shares):
    """The module's expected_net_edge, vectorised: net per share if filled."""
    mid = (ask + bid) / 2.0
    # 3-level VWAP of the order at the decision book
    q, vw = _fill_buy(ask.ravel(), dep.ravel(), (ask + 0.02).ravel(), shares.ravel() if np.ndim(shares) else shares,
                      partial=False)
    vw = vw.reshape(ask.shape)
    paid = vw + cfg.adverse
    fin = fee(rate, np.clip(paid, 0.01, 0.99))
    if cfg.edge_hold <= 0:
        return p_side - paid - fin
    kap = S.kappa_at(e, cfg.edge_hold)
    exb = np.clip(mid + kap * (p_side - mid) - S.half_spread_at(e + cfg.edge_hold), 0.0, 1.0)
    return exb - paid - fin - fee(rate, exb)


GRID_END_S = G1_S - 1.0


def candidate_rows(S: Sim, cfg: Cfg, splits) -> np.ndarray:
    D = S.D
    p = S.P[cfg.model]
    return np.where(np.isin(D.split, splits) & (D.tau == cfg.tau) & (D.y >= 0) & ~np.isnan(p))[0]


def enter(S: Sim, cfg: Cfg, rows: np.ndarray) -> dict:
    """Decide (scan the window) and fill. Returns per-order arrays for the orders sent."""
    D = S.D
    p_up = S.P[cfg.model][rows]
    up = p_up >= 0.5
    if cfg.sides != "both":
        keep = up if cfg.sides == "UP" else ~up
        rows, p_up, up = rows[keep], p_up[keep], up[keep]
    p_side = np.where(up, p_up, 1.0 - p_up)
    E = np.array([cfg.tau]) if cfg.win < 0 else np.round(np.arange(0.0, cfg.win + 1e-9, GSTEP_S), 3)
    Em = np.broadcast_to(E, (len(rows), len(E)))
    ask = S.side_px(rows, up, Em, "ask")
    bid = S.side_px(rows, up, Em, "bid")
    dep = S.side_px(rows, up, Em, "adep")
    ask_tau = S.side_px(rows, up, np.full(len(rows), cfg.tau), "ask")
    with np.errstate(invalid="ignore", divide="ignore"):
        shares = np.floor(cfg.usd / ask * 100.0) / 100.0
        valid = (~np.isnan(ask) & ~np.isnan(bid) & (ask <= cfg.max_ask + 1e-9) & (ask >= cfg.min_ask - 1e-9)
                 & (ask - bid <= cfg.max_spread + 1e-9) & (shares >= MIN_SHARES) & (shares <= cfg.depth_frac * dep + 1e-9)
                 & (p_side >= cfg.min_prob)[:, None])
        if cfg.qmove < 9:
            valid &= ~((ask - ask_tau[:, None]) > cfg.qmove + 1e-9)
        edge = None
        if cfg.min_edge > -9 or cfg.entry == "fokm":
            edge = edge_matrix(S, cfg, Em, ask, bid, dep, p_side[:, None], D.rate[rows][:, None], shares)
            if cfg.min_edge > -9:
                valid &= edge >= cfg.min_edge - 1e-12
    has = valid.any(1)
    first = np.argmax(valid, 1)
    sel = np.where(has)[0]
    k = first[sel]
    o = {"rows": rows[sel], "up": up[sel], "p_side": p_side[sel], "e": E[k], "ask_d": ask[sel, k],
         "bid_d": bid[sel, k], "dep_d": dep[sel, k], "shares": shares[sel, k],
         "edge_d": edge[sel, k] if edge is not None else np.full(len(sel), np.nan),
         "n_candidates": len(rows)}
    r = o["rows"]
    ow, ack, px = S.latency(r, cfg, 0)
    t_a = o["e"] + ow / 1000.0
    ask_a = S.side_px(r, o["up"], t_a + px / 1000.0, "ask")
    dep_a = S.side_px(r, o["up"], t_a + px / 1000.0, "adep")
    if cfg.entry in ("fok0", "fak0"):
        limit = o["ask_d"]
    elif cfg.entry == "fok1":
        limit = o["ask_d"] + 0.01
    elif cfg.entry == "fok2":
        limit = o["ask_d"] + 0.02
    else:                                   # fokm: the highest price at which the edge still clears the bar
        room = np.floor(np.maximum(o["edge_d"] - max(cfg.min_edge, 0.0), 0.0) * 100.0 + 1e-9) / 100.0
        limit = o["ask_d"] + np.minimum(np.nan_to_num(room), 0.05)
    q, vw = _fill_buy(ask_a, dep_a, limit, o["shares"], partial=cfg.entry == "fak0")
    o.update(t_arr=t_a, ask_a=ask_a, qty=q, vw_in=vw, t_known=t_a + ack / 1000.0, lat_ms=ow + px)
    return o


def sell(S: Sim, cfg: Cfg, rows, up, t_x, qty, y_side):
    """FOK sells from decision time t_x (retried every 2 s at bid - 2c); settles past the grid."""
    n = len(rows)
    px_out = np.full(n, np.nan)
    t_out = np.full(n, np.nan)
    settled = np.zeros(n, bool)
    dec_bid = S.side_px(rows, up, np.minimum(t_x, GRID_END_S), "bid")
    floor = np.where(np.isnan(dec_bid), 0.0, dec_bid - 0.02)
    first_bid = dec_bid.copy()
    pend = np.arange(n)
    tx = t_x.astype(np.float64).copy()
    for k in range(1, N_EXIT_TRIES + 1):
        if not len(pend):
            break
        late = tx[pend] >= GRID_END_S
        if late.any():
            lp = pend[late]
            px_out[lp], t_out[lp], settled[lp] = y_side[lp], 300.0, True
            pend = pend[~late]
            if not len(pend):
                break
        ow, ack, pxm = S.latency(rows[pend], cfg, k)
        ts = tx[pend] + ow / 1000.0
        b = S.side_px(rows[pend], up[pend], ts + pxm / 1000.0, "bid")
        bd = S.side_px(rows[pend], up[pend], ts + pxm / 1000.0, "bdep")
        full, vw = _fill_sell(b, bd, floor[pend], qty[pend])
        done = pend[full]
        px_out[done], t_out[done] = vw[full], ts[full]
        rest = pend[~full]
        tx[rest] = ts[~full] + ack[~full] / 1000.0 + 2.0
        nb = S.side_px(rows[rest], up[rest], np.minimum(tx[rest], GRID_END_S), "bid")
        floor[rest] = np.where(np.isnan(nb), floor[rest] - 0.02, nb - 0.02)
        pend = rest
    if len(pend):
        px_out[pend], t_out[pend], settled[pend] = y_side[pend], 300.0, True
    return px_out, t_out, settled, first_bid


def exit_times(S: Sim, cfg: Cfg, o: dict, f: np.ndarray, ex: tuple) -> tuple[np.ndarray, np.ndarray]:
    """Exit DECISION time (s after the open) for filled orders ``f`` and the reason code."""
    D = S.D
    r, up = o["rows"][f], o["up"][f]
    t0 = o["t_known"][f]
    kind = ex[0]
    if kind == "t":
        return t0 + ex[1], np.zeros(len(f), int)
    maxh = ex[-1]
    L = int(round(maxh / GSTEP_S)) + 1
    T = t0[:, None] + GSTEP_S * np.arange(L)[None, :]
    Tc = np.minimum(T, GRID_END_S)
    bid = S.side_px(r, up, Tc, "bid")
    entry = o["vw_in"][f][:, None]
    if kind == "tp":
        trig = bid >= entry + ex[1] - 1e-9
    elif kind == "sl":
        trig = bid <= entry - ex[1] + 1e-9
    elif kind == "tpsl":
        trig = (bid >= entry + ex[1] - 1e-9) | (bid <= entry - ex[2] + 1e-9)
    elif kind == "mom":
        g = D.g[r][:, None]
        x_now = D.x[g, gi(Tc)]
        x_1 = D.x[g, gi(Tc - 1.0)]
        sig = D.f("sigma")[r][:, None]
        dr = np.where(up[:, None], x_now - x_1, x_1 - x_now)
        trig = dr < -0.5 * sig
    elif kind == "edge":
        g = D.g[r][:, None]
        x_now = D.x[g, gi(Tc)]
        lk = D.f("lk_bot")[r][:, None]
        sig = D.f("sigma")[r][:, None]
        u = np.maximum(300.0 - Tc + 0.18 - 40.0, 1.0)
        pu = t4((x_now - lk) / (sig * np.sqrt(u)))
        ps = np.where(up[:, None], pu, 1.0 - pu)
        trig = ps <= bid + fee(D.rate[r][:, None], bid)
    else:
        raise ValueError(kind)
    trig = np.where(np.isnan(bid) & (kind != "mom"), False, trig)
    trig[:, 0] &= kind in ("mom", "edge")     # an event must be seen after the fill is known
    hit = trig.any(1)
    j = np.where(hit, np.argmax(trig, 1), L - 1)
    return t0 + GSTEP_S * j, np.where(hit, 1, 0)


EXITS = ([("t", h) for h in HOLDS_S] + [("settle",)] +
         [("tp", c / 100.0, 30.0) for c in (1, 2, 3, 5, 7, 10)] +
         [("sl", c / 100.0, 3.0) for c in (1, 2, 3, 5, 7, 10)] +
         [("sl", c / 100.0, 10.0) for c in (1, 2, 3, 5, 7, 10)] +
         [("tpsl", 0.03, 0.03, 10.0), ("tpsl", 0.05, 0.05, 30.0), ("mom", 10.0), ("mom", 30.0),
          ("edge", 10.0), ("edge", 30.0)])


def exit_name(ex: tuple) -> str:
    if ex[0] == "t":
        return f"time {ex[1]:g}s"
    if ex[0] == "settle":
        return "settlement"
    if ex[0] == "tp":
        return f"target +{ex[1] * 100:.0f}c (max {ex[2]:g}s)"
    if ex[0] == "sl":
        return f"stop -{ex[1] * 100:.0f}c (else {ex[2]:g}s)"
    if ex[0] == "tpsl":
        return f"target +{ex[1] * 100:.0f}c / stop -{ex[2] * 100:.0f}c (max {ex[3]:g}s)"
    if ex[0] == "mom":
        return f"momentum reversal (max {ex[1]:g}s)"
    return f"edge gone (max {ex[1]:g}s)"


def simulate(S: Sim, cfg: Cfg, splits=(0,), exits=EXITS, orders: dict | None = None) -> dict:
    """One entry configuration, every exit. Returns {"orders": o, exit_name: trade arrays}."""
    D = S.D
    o = orders if orders is not None else enter(S, cfg, candidate_rows(S, cfg, splits))
    f = np.where(o["qty"] > 0)[0]
    r, up = o["rows"][f], o["up"][f]
    y_side = np.where(up, D.y[r], 1.0 - D.y[r])
    out = {"orders": o, "filled": f}
    for ex in exits:
        if ex[0] == "settle":
            px, tt, st, fb = y_side.astype(float), np.full(len(f), 300.0), np.ones(len(f), bool), np.full(len(f), np.nan)
            reason = np.zeros(len(f), int)
        else:
            tx, reason = exit_times(S, cfg, o, f, ex)
            px, tt, st, fb = sell(S, cfg, r, up, tx, o["qty"][f], y_side)
        out[exit_name(ex)] = {"px": px, "t": tt, "settled": st, "first_bid": fb, "reason": reason}
    return out


def trade_table(S: Sim, res: dict, name: str) -> dict:
    """Per-trade dollars and per-share cents for one exit."""
    D = S.D
    o, f = res["orders"], res["filled"]
    x = res[name]
    q = o["qty"][f]
    r = o["rows"][f]
    ein, eout = o["vw_in"][f], x["px"]
    fin = fee(D.rate[r], ein)
    fout = np.where(x["settled"], 0.0, fee(D.rate[r], eout))
    gross = q * (eout - ein)
    fees_ = q * (fin + fout)
    slip = q * (ein - o["ask_d"][f]) + np.where(x["settled"], 0.0, q * np.nan_to_num(x["first_bid"] - eout))
    return {"rows": r, "epoch": D.epoch[r], "asset": D.asset[r], "up": o["up"][f], "qty": q, "entry": ein,
            "exit": eout, "gross": gross, "fees": fees_, "slip": slip, "net": gross - fees_,
            "net_c": (eout - ein - fin - fout) * 100.0, "hold": x["t"] - o["t_arr"][f], "settled": x["settled"],
            "e": o["e"][f], "ask_d": o["ask_d"][f], "t_arr": o["t_arr"][f]}


def summarize(tt: dict, n_orders: int, hours: float | None = None) -> dict:
    n = len(tt["net"])
    if n == 0:
        return {"orders": n_orders, "trades": 0, "fill": 0.0 if n_orders else float("nan")}
    order = np.lexsort((tt["asset"], tt["epoch"]))
    cum = np.cumsum(tt["net"][order])
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], cum])) - np.concatenate([[0.0], cum])))
    mc, t = clustered(tt["net_c"], tt["epoch"])
    ep_pnl = np.bincount(np.unique(tt["epoch"], return_inverse=True)[1], weights=tt["net"])
    sharpe = float(ep_pnl.mean() / ep_pnl.std() * math.sqrt(288)) if len(ep_pnl) > 2 and ep_pnl.std() > 0 else float("nan")
    return {"orders": n_orders, "trades": n, "fill": n / max(n_orders, 1), "win": float(np.mean(tt["net"] > 0)),
            "avg_entry": float(np.mean(tt["entry"])), "avg_exit": float(np.mean(tt["exit"])),
            "hold_s": float(np.median(tt["hold"])), "gross": float(tt["gross"].sum()), "fees": float(tt["fees"].sum()),
            "slip": float(tt["slip"].sum()), "net": float(tt["net"].sum()), "net_trade": float(tt["net"].mean()),
            "c_share": mc, "t": t, "dd": dd, "sharpe_day": sharpe, "shares": float(tt["qty"].sum())}


# ─────────────────────────────── the sweep (TRAIN + VALIDATION only) ───────────────────────────────

SWEEP_EXITS = ([("t", h) for h in (1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 30.0)] + [("settle",), ("tp", 0.03, 30.0),
               ("sl", 0.03, 10.0), ("mom", 30.0), ("edge", 30.0)])
SUM_FIELDS = ("orders", "trades", "fill", "win_rate", "avg_entry", "avg_exit", "hold_s", "gross", "fees", "slip", "net",
              "net_trade", "c_share", "t", "dd", "sharpe_day", "shares")
HOURS = {0: 458 / 12.0, 1: 153 / 12.0, 2: 153 / 12.0}


def sweep_configs() -> list[Cfg]:
    out = []
    for model in ("D_twap", "D_recal"):
        for tau in (-10.0, -5.0, -3.0, -2.0, -1.0, 0.0):
            for win in (-1.0, 0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0):
                for me in (-9.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05):
                    for ms in (0.01, 0.02, 0.03, 0.05, 9.0):
                        for ma in (0.3, 0.4, 0.5, 0.6, 0.7, 0.99):
                            out.append(Cfg(model=model, tau=tau, win=win, min_edge=me, max_spread=ms, max_ask=ma))
    return out


_S: Sim | None = None


def _init_worker() -> None:
    global _S
    D = Data()
    _S = Sim(D, dict(np.load(OUT / "preds.npz")))


def eval_cfg(S: Sim, cfg: Cfg, splits=(0, 1), exits=SWEEP_EXITS) -> list[dict]:
    res = simulate(S, cfg, splits, exits)
    o = res["orders"]
    rows = []
    osplit = S.D.split[o["rows"]]
    for ex in exits:
        name = exit_name(ex)
        tt = trade_table(S, res, name)
        tsplit = S.D.split[tt["rows"]]
        for sp in splits:
            m = tsplit == sp
            sub = {k: v[m] for k, v in tt.items()}
            sm = summarize(sub, int(np.sum(osplit == sp)))
            sm["win_rate"] = sm.pop("win", float("nan"))       # "win" is also the config's scan window
            rows.append({**{f: getattr(cfg, f) for f in CFG_FIELDS}, "exit": name, "split": RB.SPLIT_NAMES[sp], **sm})
    return rows


def _run_chunk(cfgs: list[Cfg]) -> list[dict]:
    out = []
    for c in cfgs:
        out += eval_cfg(_S, c)
    return out


def write_csv(rows: list[dict], path: Path) -> None:
    import csv
    fields = list(CFG_FIELDS) + ["exit", "split"] + list(SUM_FIELDS)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def read_csv(path: Path) -> list[dict]:
    import csv
    out = []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            for k, v in r.items():
                if k in ("model", "entry", "lat", "sides", "exit", "split"):
                    continue
                r[k] = float(v) if v not in ("", None) else float("nan")
            out.append(r)
    return out


def run_sweep(procs: int = 3, chunk: int = 60) -> None:
    cfgs = sweep_configs()
    parts = [cfgs[i:i + chunk] for i in range(0, len(cfgs), chunk)]
    rows = []
    RES.mkdir(exist_ok=True)
    with ProcessPoolExecutor(procs, initializer=_init_worker) as pool:
        for k, r in enumerate(pool.map(_run_chunk, parts)):
            rows += r
            if k % 20 == 0:
                print(f"  {min((k + 1) * chunk, len(cfgs))}/{len(cfgs)} configs", flush=True)
    write_csv(rows, RES / "next_market_sweep.csv")


# ─────────────────────────────── report tables ───────────────────────────────

PERF_HDR = ["Strategy", "Trades", "Fill %", "Win %", "Avg Entry", "Avg Exit", "Gross P&L", "Fees", "Slippage",
            "Net P&L", "Net/Trade", "c/share", "t", "Max DD"]


def perf_row(label: str, sm: dict) -> list:
    if not sm.get("trades"):
        return [label, 0, sm.get("fill", float("nan")) * 100 if sm.get("orders") else float("nan")] + [float("nan")] * 11
    return [label, sm["trades"], sm["fill"] * 100, sm["win"] * 100, sm["avg_entry"], sm["avg_exit"],
            f"${sm['gross']:+.2f}", f"${sm['fees']:.2f}", f"${sm['slip']:.2f}", f"${sm['net']:+.2f}",
            f"${sm['net_trade']:+.3f}", sm["c_share"], sm["t"], f"${sm['dd']:.2f}"]


def run(S: Sim, cfg: Cfg, splits, exit_: tuple, orders=None):
    res = simulate(S, cfg, splits, [exit_], orders=orders)
    tt = trade_table(S, res, exit_name(exit_))
    return res, tt, summarize(tt, len(res["orders"]["rows"]))


def sub(tt: dict, m: np.ndarray) -> dict:
    return {k: v[m] for k, v in tt.items()}


def liquidity_profile(D: Data, splits=(0, 1)) -> str:
    r = np.where(np.isin(D.split, splits) & (D.tau == -3.0))[0]
    rows = []
    for off in (-10, -5, -3, -2, -1, -0.5, 0, 0.25, 0.5, 1, 1.5, 2, 3, 5, 10, 30):
        sp = np.concatenate([D.price("ua", r, off) - D.price("ub", r, off), D.price("da", r, off) - D.price("db", r, off)])
        dep = np.concatenate([D.depth("uad", r, off), D.depth("dad", r, off)])
        noask = np.mean(np.isnan(np.concatenate([D.price("ua", r, off), D.price("da", r, off)])))
        rows.append([f"{off:+g}s", float(np.nanmedian(sp)) * 100, float(np.nanpercentile(sp, 90)) * 100]
                    + [float(np.nanmean(sp <= x + 1e-9)) * 100 for x in (0.01, 0.02, 0.03, 0.05)]
                    + [float(np.nanmedian(dep)), noask * 100])
    t1 = md_table(["offset from open", "median spread c", "p90 spread c", "% <=1c", "% <=2c", "% <=3c", "% <=5c",
                   "median ask depth (2 ticks)", "% no ask"], rows)
    rows = []
    for a, name in enumerate(RB.ASSETS):
        ra = r[D.asset[r] == a]
        row = [name]
        for off in (-3, 0, 1, 2, 3):
            sp = np.concatenate([D.price("ua", ra, off) - D.price("ub", ra, off), D.price("da", ra, off) - D.price("db", ra, off)])
            row.append(float(np.nanmedian(sp)) * 100)
        rows.append(row)
    t2 = md_table(["asset", "median spread c at -3s", "0s", "+1s", "+2s", "+3s"], rows)
    return t1 + "\n\n" + t2


def signal_markouts(S: Sim, splits, model: str = "D_twap", tau: float = -3.0, entry_at: float = 0.0) -> tuple[str, list]:
    """Every signal (no filter): the model's side, its ask at the decision, its bid later; what would have
    happened whether or not we traded (item 39)."""
    D = S.D
    r = np.where(np.isin(D.split, splits) & (D.tau == tau) & (D.y >= 0) & ~np.isnan(S.P[model]))[0]
    p = S.P[model][r]
    up = p >= 0.5
    ask0 = S.side_px(r, up, np.full(len(r), entry_at), "ask")
    rate = D.rate[r]
    ok0 = ~np.isnan(ask0)
    rows, recs = [], []
    for h in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 10.0, 30.0):
        b = S.side_px(r, up, np.full(len(r), entry_at + h), "bid")
        mo = b - ask0
        net = mo - fee(rate, ask0) - fee(rate, b)
        ok = ok0 & ~np.isnan(mo)
        m, t = clustered(mo[ok] * 100, D.epoch[r][ok])
        nmn, nt = clustered(net[ok] * 100, D.epoch[r][ok])
        # $5 of shares at the ask
        dollars = np.floor(5.0 / ask0[ok] * 100) / 100 * net[ok]
        rows.append([f"{h:g}s", int(ok.sum()), m, t, nmn, nt, float(np.mean(net[ok] > 0)) * 100, f"${dollars.sum():+.2f}"])
    for i in np.where(ok0)[0]:
        rec = {"asset": RB.ASSETS[D.asset[r[i]]], "epoch": int(D.epoch[r[i]]), "p_up": round(float(p[i]), 4),
               "side": "UP" if up[i] else "DOWN", "ask_at_entry": float(ask0[i]),
               "outcome": "UP" if D.y[r[i]] == 1 else "DOWN"}
        for h in (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0):
            b = S.side_px(r[i:i + 1], up[i:i + 1], np.array([entry_at + h]), "bid")[0]
            rec[f"bid_+{h:g}s"] = None if b != b else float(b)
        recs.append(rec)
    return md_table(["Holding Time", "Trades", "Avg Markout (c)", "t", "Net Markout (c)", "t", "Win %",
                     "Net P&L ($5/signal)"], rows), recs


def holding_table(S: Sim, cfg: Cfg, splits) -> str:
    rows = []
    o = enter(S, cfg, candidate_rows(S, cfg, splits))
    for h in HOLDS_S:
        _, tt, sm = run(S, cfg, splits, ("t", h), orders=o)
        if not sm.get("trades"):
            rows.append([f"{h:g}s", 0] + [float("nan")] * 5)
            continue
        mo = (tt["exit"] - tt["entry"]) * 100
        rows.append([f"{h:g}s", sm["trades"], float(mo.mean()), sm["c_share"], sm["t"], sm["win"] * 100,
                     f"${sm['net']:+.2f}"])
    _, tt, sm = run(S, cfg, splits, ("settle",), orders=o)
    if sm.get("trades"):
        rows.append(["settlement", sm["trades"], float(((tt["exit"] - tt["entry"]) * 100).mean()), sm["c_share"], sm["t"],
                     sm["win"] * 100, f"${sm['net']:+.2f}"])
    return md_table(["Holding Time", "Trades", "Avg Markout (c)", "Net Markout (c)", "t", "Win %", "Net P&L"], rows)


def prediction_time_table(S: Sim, wf: dict, cfg: Cfg, splits, ex=("t", 3.0)) -> str:
    rows = []
    for tau in (-10.0, -5.0, -3.0, -2.0, -1.0, 0.0):
        y, p = wf[(cfg.model, tau)]
        c = replace(cfg, tau=tau)
        res, tt, sm = run(S, c, splits, ex)
        o = res["orders"]
        edge = (o["p_side"] - o["ask_d"]) * 100
        rows.append([f"T{tau:+g}s", acc(p, y) * 100, ece(p, y) * 100, brier(p, y), float(np.nanmean(edge)) if len(edge) else float("nan"),
                     len(o["rows"]), sm.get("fill", float("nan")) * 100, sm.get("trades", 0),
                     sm.get("c_share", float("nan")), f"${sm.get('net', 0.0):+.2f}"])
    return md_table(["Prediction Time", "Accuracy %", "Calibration (ECE, c)", "Brier", "Avg Edge p-ask (c)", "Orders",
                     "Fill %", "Trades", "Net c/share", "Net P&L"], rows)


def breakdowns(S: Sim, tt: dict, orders_n: int, D: Data) -> dict[str, str]:
    """Asset, UP/DOWN, decision-time bucket and regime tables for one set of trades."""
    out = {}
    rows = []
    for a, name in enumerate(RB.ASSETS):
        m = tt["asset"] == a
        sm = summarize(sub(tt, m), orders_n)
        rows.append(perf_row(name, sm)[:1] + perf_row(name, sm)[1:2] + perf_row(name, sm)[3:])
    out["asset"] = md_table([h for i, h in enumerate(PERF_HDR) if i != 2], rows)
    rows = []
    for lab, m in (("UP", tt["up"]), ("DOWN", ~tt["up"].astype(bool))):
        sm = summarize(sub(tt, m.astype(bool)), orders_n)
        rows.append([x for i, x in enumerate(perf_row(lab, sm)) if i != 2])
    out["side"] = md_table([h for i, h in enumerate(PERF_HDR) if i != 2], rows)
    rows = []
    for lo, hi in ((0, 0.25), (0.25, 0.5), (0.5, 1), (1, 2), (2, 3), (3, 5), (5, 10.01)):
        m = (tt["e"] >= lo - 1e-9) & (tt["e"] < hi - 1e-9)
        sm = summarize(sub(tt, m), orders_n)
        rows.append([x for i, x in enumerate(perf_row(f"{lo:g}-{hi:g}s", sm)) if i != 2])
    out["bucket"] = md_table(["Decision after open"] + [h for i, h in enumerate(PERF_HDR) if i not in (0, 2)], rows)
    return out


def regime_masks(D: Data, rows: np.ndarray) -> dict[str, np.ndarray]:
    """Regimes from what was known at the prediction; cut points from TRAIN rows at the same offset."""
    tr = np.where((D.split == 0) & (D.tau == D.tau[rows[0]]))[0] if len(rows) else np.zeros(0, int)
    rel = D.f("rv300") / D.f("sigma")
    vol = D.f("rv300")
    med = {a: np.nanmedian(vol[tr][D.asset[tr] == a]) for a in range(len(RB.ASSETS))}
    vr = vol / np.array([med[a] for a in D.asset])
    trend = np.abs(D.f("z60000"))
    dep = np.where(D.f("p_twap") >= 0.5, D.f("u_adep"), D.f("d_adep"))
    q = lambda x, lo, hi: (np.nanpercentile(x[tr], lo), np.nanpercentile(x[tr], hi))
    v1, v2 = q(vr, 33.3, 66.7)
    t1, t2 = q(trend, 33.3, 66.7)
    d1, d2 = q(dep, 33.3, 66.7)
    rev = np.sign(D.f("r10000")) != np.sign(D.f("r60000"))
    spike = (D.f("vol_regime") > 2.0) | (trend > 3.0)
    del rel
    return {"high volatility": vr >= v2, "low volatility": vr <= v1, "strong trend (|60s move|)": trend >= t2,
            "weak trend": trend <= t1, "reversal (10s against 60s)": rev, "continuation": ~rev,
            "vol spike / event proxy": spike, "high liquidity (depth)": dep >= d2, "low liquidity": dep <= d1}


def regime_table(S: Sim, tt: dict, orders_n: int) -> str:
    D = S.D
    if not len(tt["rows"]):
        return "(no trades)"
    masks = regime_masks(D, tt["rows"])
    rows = []
    for lab, m in masks.items():
        sm = summarize(sub(tt, m[tt["rows"]]), orders_n)
        rows.append([lab, sm.get("trades", 0), sm.get("win", float("nan")) * 100 if sm.get("trades") else float("nan"),
                     sm.get("c_share", float("nan")), sm.get("t", float("nan")), f"${sm.get('net', 0.0):+.2f}"])
    for lab, lo, hi in (("narrow spread (<=1c at decision)", 0, 0.0101), ("wide spread (>1c)", 0.0101, 9)):
        sp = tt["ask_d"] - S.side_px(tt["rows"], tt["up"].astype(bool), tt["e"], "bid")
        m = (sp >= lo) & (sp < hi)
        sm = summarize(sub(tt, m), orders_n)
        rows.append([lab, sm.get("trades", 0), sm.get("win", float("nan")) * 100 if sm.get("trades") else float("nan"),
                     sm.get("c_share", float("nan")), sm.get("t", float("nan")), f"${sm.get('net', 0.0):+.2f}"])
    return md_table(["regime", "trades", "win %", "net c/share", "t", "net P&L"], rows)


def failure_table(S: Sim, res: dict, exit_label: str) -> str:
    """How orders and exits fail, from the simulated timelines."""
    D = S.D
    o = res["orders"]
    n = len(o["rows"])
    if n == 0:
        return "(no orders)"
    filled = o["qty"] > 0
    ask_a = o["ask_a"]
    gone = np.isnan(ask_a)
    moved = ~gone & (ask_a > o["ask_d"] + 1e-9)
    thin = ~gone & ~moved & ~filled
    bid_a = S.side_px(o["rows"], o["up"], o["t_arr"] + PROXY_MS / 1000.0, "bid")
    widened = (ask_a - bid_a) > (o["ask_d"] - o["bid_d"]) + 1e-9
    stale = S.D.age[S.D.g[o["rows"]], gi(o["e"])] > 1.0
    partial = filled & (o["qty"] < o["shares"] - 1e-9)
    x = res[exit_label]
    tt = trade_table(S, res, exit_label)
    exit_retry = ~x["settled"] & (x["t"] - (tt["t_arr"] + tt["hold"] * 0) > 0) & (tt["hold"] > 0)
    first = x["first_bid"]
    worse = ~x["settled"] & ~np.isnan(first) & (x["px"] < first - 1e-9)
    no_bid = np.isnan(first) & ~x["settled"]
    fell = tt["net"] < 0
    adverse_now = S.side_px(tt["rows"], tt["up"].astype(bool), tt["t_arr"] + 1.0, "bid") < tt["entry"] - 0.02
    rows = [["orders sent", n, 100.0],
            ["filled", int(filled.sum()), filled.mean() * 100],
            ["no fill: quote gone (no ask on arrival)", int(gone.sum()), gone.mean() * 100],
            ["no fill: ask moved above the limit (adverse move in flight)", int((moved & ~filled).sum()), (moved & ~filled).mean() * 100],
            ["no fill: insufficient depth at the limit", int(thin.sum()), thin.mean() * 100],
            ["partial fill (FAK only)", int(partial.sum()), partial.mean() * 100],
            ["spread wider on arrival than at the decision", int(widened.sum()), widened.mean() * 100],
            ["decision on a book point > 1 s old (stale quote)", int(stale.sum()), stale.mean() * 100],
            ["exit sold below the exit-decision bid (exit slippage)", int(worse.sum()), worse.mean() * 100 if len(worse) else float("nan")],
            ["exit decided with no bid on the book", int(no_bid.sum()), no_bid.mean() * 100 if len(no_bid) else float("nan")],
            ["exit fell back to settlement (never sold)", int(x["settled"].sum()), x["settled"].mean() * 100 if len(x["settled"]) else float("nan")],
            ["bid >2c under entry 1 s after the fill (moved against us)", int(np.nansum(adverse_now)), float(np.nanmean(adverse_now)) * 100 if len(adverse_now) else float("nan")],
            ["losing trades", int(fell.sum()), fell.mean() * 100 if len(fell) else float("nan")]]
    del exit_retry, D
    return md_table(["failure / event", "count", "% of orders (fills for exit rows)"], rows)


def perturbed(tt: dict, D: Data, entry_add=0.0, exit_add=0.0, drop=0.0, seed=0, drop_best=False) -> dict:
    """Degrade executed trades after the fact: worse entry/exit prices, fewer fills."""
    t = {k: v.copy() for k, v in tt.items()}
    ent = np.clip(t["entry"] + entry_add, 0.01, 0.99)
    ext = np.where(t["settled"], t["exit"], np.clip(t["exit"] - exit_add, 0.0, 1.0))
    rate = D.rate[t["rows"]]
    fin, fout = fee(rate, ent), np.where(t["settled"], 0.0, fee(rate, ext))
    t["entry"], t["exit"] = ent, ext
    t["gross"] = t["qty"] * (ext - ent)
    t["fees"] = t["qty"] * (fin + fout)
    t["net"] = t["gross"] - t["fees"]
    t["net_c"] = (ext - ent - fin - fout) * 100
    if drop > 0:
        rng = np.random.default_rng(seed)
        if drop_best:
            keep = t["net_c"] <= np.percentile(t["net_c"], 100 * (1 - drop))
        else:
            keep = rng.random(len(ent)) >= drop
        t = sub(t, keep)
    return t


# ─────────────────────────────── selection and lock (no TEST) ───────────────────────────────

#: fixed before the sweep was read (2026-09-25 ~14:40 UTC)
PREREGISTERED = {
    "P1 literal hypothesis (T-3s, buy at the open, no filter, sell after 3 s)":
        (Cfg(model="D_twap", tau=-3.0, win=0.0), ("t", 3.0)),
    "P2 + execution-aware edge >= 2c":
        (Cfg(model="D_twap", tau=-3.0, win=0.0, min_edge=0.02), ("t", 3.0)),
    "P3 + evaluate for 3 s after the open":
        (Cfg(model="D_twap", tau=-3.0, win=3.0, min_edge=0.02), ("t", 3.0)),
}
NEIGHBOUR_AXES = {"tau": (-10.0, -5.0, -3.0, -2.0, -1.0, 0.0), "win": (-1.0, 0.0, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0),
                  "min_edge": (-9.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05), "max_spread": (0.01, 0.02, 0.03, 0.05, 9.0),
                  "max_ask": (0.3, 0.4, 0.5, 0.6, 0.7, 0.99)}
KEY = ("model", "tau", "win", "min_edge", "max_spread", "max_ask", "exit")


def select(rows: list[dict], min_train: int = 30, min_val: int = 10) -> dict:
    """TRAIN filter -> neighbourhood stability on TRAIN -> VALIDATION filter -> lock."""
    tr = {tuple(r[k] for k in KEY): r for r in rows if r["split"] == "train"}
    va = {tuple(r[k] for k in KEY): r for r in rows if r["split"] == "validation"}
    keys = list(tr)
    tc = np.array([tr[k]["c_share"] if tr[k]["trades"] >= min_train else np.nan for k in keys])
    vc = np.array([va[k]["c_share"] if k in va and va[k]["trades"] >= min_val else np.nan for k in keys])
    both = ~np.isnan(tc) & ~np.isnan(vc)
    stats = {"configs": len(keys), "with_train_volume": int(np.sum(~np.isnan(tc))),
             "train_positive": int(np.nansum(tc > 0)), "val_positive": int(np.nansum(vc > 0)),
             "both_positive": int(np.sum(both & (tc > 0) & (vc > 0))),
             "corr_train_val": float(np.corrcoef(tc[both], vc[both])[0, 1]) if both.sum() > 3 else float("nan"),
             "train_median_c": float(np.nanmedian(tc)), "val_median_c": float(np.nanmedian(vc))}

    def neighbours(k):
        d = dict(zip(KEY, k))
        out = []
        for ax, grid in NEIGHBOUR_AXES.items():
            i = grid.index(d[ax]) if d[ax] in grid else None
            if i is None:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(grid):
                    nk = tuple(grid[j] if kk == ax else d[kk] for kk in KEY)
                    if nk in tr and tr[nk]["trades"] >= min_train:
                        out.append(tr[nk]["c_share"])
        return out

    surv = []
    for i, k in enumerate(keys):
        r = tr[k]
        if np.isnan(tc[i]) or tc[i] <= 0 or r["t"] < 1.5:
            continue
        nb = neighbours(k)
        if len(nb) < 3 or np.median(nb) <= 0:
            continue
        surv.append((k, r, float(np.median(nb))))
    stats["train_pass"] = len(surv)
    final = []
    for k, r, nb in surv:
        v = va.get(k)
        if v is None or v["trades"] < min_val or v["c_share"] <= 0:
            continue
        final.append((min(r["c_share"], v["c_share"]), k, r, v, nb))
    final.sort(key=lambda x: -x[0])
    stats["validation_pass"] = len(final)
    return {"stats": stats, "survivors": [(dict(zip(KEY, k)), r, v, nb) for _, k, r, v, nb in final[:20]],
            "train_top": sorted(((tr[k]["c_share"], k) for i, k in enumerate(keys) if not np.isnan(tc[i])),
                                key=lambda x: -x[0])[:15]}


def cfg_from_key(d: dict) -> tuple[Cfg, tuple]:
    ex = next(e for e in EXITS + SWEEP_EXITS if exit_name(e) == d["exit"])
    return Cfg(model=d["model"], tau=d["tau"], win=d["win"], min_edge=d["min_edge"], max_spread=d["max_spread"],
               max_ask=d["max_ask"]), ex


# ─────────────────────────────── the development report (TRAIN + VALIDATION) ───────────────────────────────


def load_sim(final: bool = False) -> Sim:
    D = Data()
    P = dict(np.load(OUT / ("preds_final.npz" if final else "preds.npz")))
    rng = np.random.default_rng(99)
    P["R_random"] = np.where(rng.random(len(D.y)) < 0.5, 0.6, 0.4)
    return Sim(D, P)


def perf_block(S: Sim, items: list[tuple[str, Cfg, tuple]], splits) -> str:
    rows = []
    for lab, c, ex in items:
        _, _, sm = run(S, c, splits, ex)
        rows.append(perf_row(lab, sm))
    return md_table(PERF_HDR, rows)


def dev_report(S: Sim, splits=(0, 1)) -> str:
    D = S.D
    P1, e3 = PREREGISTERED["P1 literal hypothesis (T-3s, buy at the open, no filter, sell after 3 s)"]
    P2, _ = PREREGISTERED["P2 + execution-aware edge >= 2c"]
    P3, _ = PREREGISTERED["P3 + evaluate for 3 s after the open"]
    L = [f"# Development tables ({'+'.join(RB.SPLIT_NAMES[x] for x in splits)}), generated by analyze_next_market_twap.py report\n"]
    L.append("## Liquidity around the open\n")
    L.append(liquidity_profile(D, splits))
    L.append("\n## Pre-registered configurations\n")
    L.append(perf_block(S, [(k, c, ex) for k, (c, ex) in PREREGISTERED.items()], splits))
    for sp in splits:
        L.append(f"\n{RB.SPLIT_NAMES[sp]} only:\n")
        L.append(perf_block(S, [(k, c, ex) for k, (c, ex) in PREREGISTERED.items()], (sp,)))
    L.append("\n## Markout of every signal (item 39): T-3 s model side, ask at the open, bid later\n")
    t, recs = signal_markouts(S, splits)
    L.append(t)
    with open(OUT / "signal_markouts_dev.jsonl", "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    L.append("\nSame, deciding 3 s after the open (the book has re-formed):\n")
    L.append(signal_markouts(S, splits, entry_at=3.0)[0])
    L.append("\n## Executable holding-time table\n")
    for lab, c in (("P1 (literal)", P1), ("P3 (edge >= 2c, 3 s window)", P3)):
        L.append(f"\n{lab}:\n")
        L.append(holding_table(S, c, splits))
    L.append("\n## Experiments 1-5 (T-3 s, at the open, sell after 3 s)\n")
    L.append(perf_block(S, [
        ("1 direction only", P1, e3),
        ("2 + probability >= 0.60", replace(P1, min_prob=0.60), e3),
        ("3 + spread <= 3c", replace(P1, min_prob=0.60, max_spread=0.03), e3),
        ("4 + depth (order <= 50 % of 2-tick depth)", replace(P1, min_prob=0.60, max_spread=0.03, depth_frac=0.5), e3),
        ("5 + execution-aware net edge >= 2c", replace(P1, min_prob=0.60, max_spread=0.03, depth_frac=0.5, min_edge=0.02), e3),
        ("5b same, 3 s window", replace(P1, win=3.0, min_prob=0.60, max_spread=0.03, depth_frac=0.5, min_edge=0.02), e3),
        ("random side (baseline)", replace(P1, model="R_random"), e3),
        ("market's pre-open side (baseline)", replace(P1, model="M_market"), e3)], splits))
    L.append("\n## Strategies A-F (same data, same execution)\n")
    for lab, base in (("entry P1 (no filter)", P1), ("entry P2 (edge >= 2c)", P2), ("entry P3 (edge >= 2c, 3 s window)", P3)):
        L.append(f"\n{lab}:\n")
        L.append(perf_block(S, [("A: 3 s exit", base, ("t", 3.0)), ("B: 1 s exit", base, ("t", 1.0)),
                                ("C: 2 s exit", base, ("t", 2.0)), ("D: 4 s exit", base, ("t", 4.0)),
                                ("E: momentum exit (max 30 s)", base, ("mom", 30.0)),
                                ("F: edge-gone exit (max 30 s)", base, ("edge", 30.0)),
                                ("settlement hold (reference)", base, ("settle",))], splits))
    L.append("\n## Entry types (T-3 s, 3 s window, edge >= 2c, sell after 3 s)\n")
    L.append(perf_block(S, [("A immediate FOK at the ask", P3, e3), ("B FAK at the ask (partial ok)", replace(P3, entry="fak0"), e3),
                            ("C marketable limit ask + 1c", replace(P3, entry="fok1"), e3),
                            ("C marketable limit ask + 2c", replace(P3, entry="fok2"), e3),
                            ("C limit at the edge's max price (<= +5c)", replace(P3, entry="fokm"), e3),
                            ("D no trade if the ask rose > 0c since the prediction", replace(P3, qmove=0.0), e3),
                            ("D no trade if the ask rose > 1c since the prediction", replace(P3, qmove=0.01), e3)], splits))
    L.append("\n## Latency: effective delay from decision to the book that fills (no proxy)\n")
    items = [(f"{ms} ms", replace(P3, lat=f"d{ms}"), e3) for ms in (0, 25, 50, 100, 150, 200, 300, 500, 750, 1000)]
    items.append(("measured draw + 500 ms venue proxy (the model used everywhere)", P3, e3))
    L.append(perf_block(S, items, splits))
    L.append("\nSame for P1 (no filter):\n")
    L.append(perf_block(S, [(f"{ms} ms", replace(P1, lat=f"d{ms}"), e3) for ms in (0, 100, 300, 500, 1000)]
                        + [("measured + proxy", P1, e3)], splits))
    L.append("\n## Spread filter\n")
    for lab, base in (("P1 base", P1), ("P3 base", P3)):
        L.append(f"\n{lab}:\n")
        L.append(perf_block(S, [(f"max spread {x * 100:g}c", replace(base, max_spread=x), e3)
                                for x in (0.005, 0.01, 0.02, 0.03, 0.05, 9.0)], splits))
    L.append("\n## Order size (depth, VWAP, slippage)\n")
    for lab, base in (("P1 base", P1), ("P3 base", P3)):
        L.append(f"\n{lab}:\n")
        L.append(perf_block(S, [(f"${u:g}", replace(base, usd=u), e3) for u in (1, 2, 5, 10, 20, 50)], splits))
    L.append("\n## Entry price thresholds (cumulative: ask < X)\n")
    for lab, base in (("P1 base", P1), ("P3 base", P3)):
        L.append(f"\n{lab}:\n")
        L.append(perf_block(S, [(f"ask < {x:.2f}", replace(base, max_ask=x - 0.001), e3)
                                for x in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60)], splits))
    L.append("\n## Minimum execution-aware edge (T-3 s, 3 s window, sell after 3 s)\n")
    L.append(perf_block(S, [(f"edge >= {x * 100:g}c", replace(P3, min_edge=x), e3) for x in (0.005, 0.01, 0.015, 0.02, 0.03, 0.05)]
                        + [("edge >= 2c priced as a settlement hold, held to settlement", replace(P3, edge_hold=0.0), ("settle",))],
                        splits))
    L.append("\n## Exits (P3 entry)\n")
    ex_items = ([(f"A fixed {h:g}s", P3, ("t", h)) for h in (1.0, 2.0, 3.0, 4.0, 5.0)] +
                [("B momentum reversal (max 10 s)", P3, ("mom", 10.0)), ("B momentum reversal (max 30 s)", P3, ("mom", 30.0))] +
                [(f"C target +{c}c (max 30 s)", P3, ("tp", c / 100.0, 30.0)) for c in (1, 2, 3, 5, 7, 10)] +
                [("D edge gone (max 10 s)", P3, ("edge", 10.0)), ("D edge gone (max 30 s)", P3, ("edge", 30.0)),
                 ("E max hold 15 s", P3, ("t", 15.0)), ("E max hold 30 s", P3, ("t", 30.0)), ("settlement", P3, ("settle",))])
    L.append(perf_block(S, ex_items, splits))
    L.append("\n## Stop loss (P3 entry; else sell at 3 s / 10 s)\n")
    L.append(perf_block(S, [("no stop, 3 s", P3, ("t", 3.0))] + [(f"stop -{c}c, else 3 s", P3, ("sl", c / 100.0, 3.0)) for c in (1, 2, 3, 5, 7, 10)]
                        + [("no stop, 10 s", P3, ("t", 10.0))] + [(f"stop -{c}c, else 10 s", P3, ("sl", c / 100.0, 10.0)) for c in (1, 2, 3, 5, 7, 10)],
                        splits))
    for lab, base in (("P1", P1), ("P3", P3)):
        res, tt, sm = run(S, base, splits, e3)
        b = breakdowns(S, tt, len(res["orders"]["rows"]), D)
        L.append(f"\n## By asset ({lab}, sell after 3 s)\n")
        L.append(b["asset"])
        L.append(f"\n## UP vs DOWN ({lab})\n")
        L.append(b["side"])
        L.append(f"\n## Regimes ({lab})\n")
        L.append(regime_table(S, tt, len(res["orders"]["rows"])))
        L.append(f"\n## Failure cases ({lab})\n")
        L.append(failure_table(S, res, exit_name(e3)))
    L.append("\n## Decision time after the open (10 s window, edge >= 2c, sell after 3 s)\n")
    res, tt, sm = run(S, replace(P3, win=10.0), splits, e3)
    L.append(breakdowns(S, tt, len(res["orders"]["rows"]), D)["bucket"])
    L.append("\nDirection only (no edge gate), 10 s window: every market enters at the first quote that passes:\n")
    res, tt, sm = run(S, replace(P1, win=10.0, max_spread=0.03), splits, e3)
    L.append(breakdowns(S, tt, len(res["orders"]["rows"]), D)["bucket"])
    return "\n".join(L)


# ─────────────────────────────── robustness, walk-forward, sensitivity, final ───────────────────────────────


def robustness(S: Sim, cfg: Cfg, ex: tuple, splits, seed: int = 7) -> str:
    """Degrade the configuration: slower, fewer fills, worse prices, a worse model."""
    D = S.D
    res, tt, sm = run(S, cfg, splits, ex)
    n = len(res["orders"]["rows"])
    rows = [perf_row("base", sm)]
    for k in (1.25, 1.5, 2.0):
        rows.append(perf_row(f"latency x{k:g}", run(S, replace(cfg, lat_scale=k), splits, ex)[2]))
    rows.append(perf_row("random extra delay 0-300 ms per order", run(S, replace(cfg, lat_extra=300.0), splits, ex)[2]))
    rows.append(perf_row("random extra delay 0-1000 ms per order", run(S, replace(cfg, lat_extra=1000.0), splits, ex)[2]))
    for d in (0.25, 0.5):
        rows.append(perf_row(f"fill rate -{d * 100:.0f} % (random fills dropped)", summarize(perturbed(tt, D, drop=d, seed=seed), n)))
    rows.append(perf_row("best 10 % of fills never happen", summarize(perturbed(tt, D, drop=0.10, drop_best=True), n)))
    rows.append(perf_row("spread +1c (entry +0.5c, exit -0.5c)", summarize(perturbed(tt, D, 0.005, 0.005), n)))
    rows.append(perf_row("slippage +1c on entry", summarize(perturbed(tt, D, 0.01, 0.0), n)))
    rows.append(perf_row("exit 1c worse", summarize(perturbed(tt, D, 0.0, 0.01), n)))
    rows.append(perf_row("everything above at once (x1.5, +1c/+1c, -25 % fills)",
                         summarize(perturbed(run(S, replace(cfg, lat_scale=1.5), splits, ex)[1], D, 0.01, 0.01, drop=0.25, seed=seed), n)))
    rng = np.random.default_rng(seed)
    for sd in (0.25, 0.5, 1.0):
        P2 = dict(S.P)
        P2[cfg.model] = sigm(logit(S.P[cfg.model]) + rng.normal(0.0, sd, len(S.P[cfg.model])))
        S2 = Sim.__new__(Sim)
        S2.__dict__.update(S.__dict__)
        S2.P = P2
        rows.append(perf_row(f"prediction noise (logit sd {sd:g})", run(S2, cfg, splits, ex)[2]))
    return md_table(PERF_HDR, rows)


def walk_forward_pnl(S: Sim, cfg: Cfg, ex: tuple, folds: int = 5, include_test: bool = False) -> str:
    D = S.D
    res, tt, _ = run(S, cfg, (0, 1, 2) if include_test else (0, 1), ex)
    o = res["orders"]
    eps = np.unique(D.epoch[D.split <= 1])
    cuts = np.array_split(eps, folds)
    rows = []
    for k, c in enumerate(cuts):
        m = (tt["epoch"] >= c[0]) & (tt["epoch"] <= c[-1])
        om = (D.epoch[o["rows"]] >= c[0]) & (D.epoch[o["rows"]] <= c[-1])
        sm = summarize(sub(tt, m), int(om.sum()))
        rows.append([x for i, x in enumerate(perf_row(f"fold {k + 1} (epochs {c[0]}-{c[-1]})", sm))])
    if include_test:
        m = D.split[tt["rows"]] == 2
        om = D.split[o["rows"]] == 2
        rows.append(perf_row("TEST", summarize(sub(tt, m), int(om.sum()))))
    return md_table(PERF_HDR, rows)


def sensitivity(rows: list[dict], base: dict) -> str:
    """One parameter at a time around ``base`` (a sweep key), TRAIN and VALIDATION c/share."""
    idx = {tuple(r[k] for k in KEY) + (r["split"],): r for r in rows}
    out = []
    for ax, grid in list(NEIGHBOUR_AXES.items()) + [("exit", tuple(exit_name(e) for e in SWEEP_EXITS))]:
        for v in grid:
            k = tuple(v if kk == ax else base[kk] for kk in KEY)
            tr, va = idx.get(k + ("train",)), idx.get(k + ("validation",))
            if tr is None:
                continue
            mark = " (base)" if v == base[ax] else ""
            out.append([f"{ax} = {v}{mark}", int(tr["trades"]), tr.get("c_share", float("nan")), tr.get("t", float("nan")),
                        int(va["trades"]) if va else 0, va.get("c_share", float("nan")) if va else float("nan"),
                        va.get("t", float("nan")) if va else float("nan")])
    return md_table(["parameter", "TRAIN trades", "TRAIN c/share", "TRAIN t", "VAL trades", "VAL c/share", "VAL t"], out)


def final_report(locked: list[tuple[str, Cfg, tuple]], sweep_rows: list[dict] | None, base_key: dict | None) -> str:
    """Everything that reads TEST (and FORWARD): run once, after the lock."""
    D = Data()
    preds = model_predictions(D, final=True)
    np.savez(OUT / "preds_final.npz", **preds)
    S = load_sim(final=True)
    wf = walk_forward(D, names=("D_twap",))
    has_fwd = bool(np.any(D.split == 3))
    L = ["# Final evaluation: TEST (and FORWARD), locked configurations\n"]
    items = locked + [(k, c, e) for k, (c, e) in PREREGISTERED.items()] + [
        ("random side, at the open, 3 s", Cfg(model="R_random", tau=-3.0), ("t", 3.0)),
        ("market's pre-open side, at the open, 3 s", Cfg(model="M_market", tau=-3.0), ("t", 3.0))]
    for sp, lab in ((2, "TEST"), (3, "FORWARD")):
        if sp == 3 and not has_fwd:
            continue
        L.append(f"\n## {lab}: required performance table\n")
        L.append(perf_block(S, items, (sp,)))
    L.append("\n## TEST: model accuracy by prediction time (fit on TRAIN + VALIDATION; D needs no fit)\n")
    rows = []
    for tau in (-10.0, -5.0, -3.0, -2.0, -1.0, 0.0):
        m = (D.split == 2) & (D.tau == tau) & (D.y >= 0)
        r = [f"T{tau:+g}s", int(m.sum())]
        for n in ("D_twap", "D_recal", "E_logistic", "F_gbm", "M_market"):
            ok = m & ~np.isnan(preds[n])
            r += [acc(preds[n][ok], D.y[ok]) * 100, brier(preds[n][ok], D.y[ok])]
        rows.append(r)
    L.append(md_table(["pred time", "n", "D acc", "D Brier", "Drecal acc", "Drecal Brier", "E acc", "E Brier", "F acc",
                       "F Brier", "mkt acc", "mkt Brier"], rows))
    for lab, c, e in locked[:2]:
        L.append(f"\n## TEST: prediction time ({lab})\n")
        L.append(prediction_time_table(S, {k: v for k, v in _test_wf(D, preds).items()}, c, (2,), e))
        L.append(f"\n## TEST: holding time ({lab})\n")
        L.append(holding_table(S, c, (2,)))
        res, tt, sm = run(S, c, (2,), e)
        b = breakdowns(S, tt, len(res["orders"]["rows"]), D)
        L.append(f"\n## TEST: by asset ({lab})\n")
        L.append(asset_table(S, c, e, (2,)))
        L.append(f"\n## TEST: UP vs DOWN ({lab})\n")
        L.append(b["side"])
        L.append(f"\n## TEST: regimes ({lab})\n")
        L.append(regime_table(S, tt, len(res["orders"]["rows"])))
        L.append(f"\n## TEST: failure cases ({lab})\n")
        L.append(failure_table(S, res, exit_name(e)))
        L.append(f"\n## Walk-forward P&L by fold ({lab})\n")
        L.append(walk_forward_pnl(S, c, e, include_test=True))
        L.append(f"\n## Robustness on TRAIN + VALIDATION ({lab})\n")
        L.append(robustness(S, c, e, (0, 1)))
        L.append(f"\n## Robustness on TEST ({lab})\n")
        L.append(robustness(S, c, e, (2,)))
    L.append("\n## TEST: signal markouts (every T-3 s signal, ask at the open)\n")
    t, recs = signal_markouts(S, (2,))
    L.append(t)
    L.append("\nDeciding 3 s after the open:\n")
    L.append(signal_markouts(S, (2,), entry_at=3.0)[0])
    with open(OUT / "signal_markouts_test.jsonl", "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    if sweep_rows is not None and base_key is not None:
        L.append("\n## Parameter sensitivity around the base configuration (sweep; TRAIN and VALIDATION only)\n")
        L.append(sensitivity(sweep_rows, base_key))
    del wf
    return "\n".join(L)


def _test_wf(D: Data, preds: dict) -> dict:
    """(y, p) of TEST rows per (model, tau), in the shape prediction_time_table reads."""
    out = {}
    for tau in (-10.0, -5.0, -3.0, -2.0, -1.0, 0.0):
        for n in MODEL_NAMES:
            m = (D.split == 2) & (D.tau == tau) & (D.y >= 0) & ~np.isnan(preds[n])
            out[(n, tau)] = (D.y[m], preds[n][m])
    return out



def example_logs(S: Sim, cfg: Cfg, ex: tuple, splits, n: int = 3) -> str:
    """NEXT_MARKET_PREDICTION / NEXT_MARKET_EXIT records for a few simulated trades, built with the strategy
    module itself; the module's scalar edge must equal the simulator's vectorised edge."""
    from troll_poly_bot.signals.costs import FeeSchedule
    from troll_poly_bot.strategy import next_market_twap as nm
    D = S.D
    res = simulate(S, cfg, splits, [ex])
    o, f = res["orders"], res["filled"]
    x = res[exit_name(ex)]
    out = []
    for j, k in enumerate(f[:n]):
        r = int(o["rows"][k])
        feats = {c: float(D.X[r, i]) for c, i in D.c.items()}
        open_ms = float(D.mk[D.g[r], 3])
        pred = nm.predict(RB.ASSETS[D.asset[r]], feats, open_ms, open_ms + cfg.tau * 1000.0,
                          p_up=float(S.P[cfg.model][r]))
        e = float(o["e"][k])
        q = {}
        for side, (b, a, bd, ad) in (("UP", ("ub", "ua", "ubd", "uad")), ("DOWN", ("db", "da", "dbd", "dad"))):
            q[side] = nm.Quote(bid=float(D.price(b, np.array([r]), e)[0]), ask=float(D.price(a, np.array([r]), e)[0]),
                               bid_depth=float(D.depth(bd, np.array([r]), e)[0]), ask_depth=float(D.depth(ad, np.array([r]), e)[0]))
        mcfg = nm.NextMarketConfig(enabled=True, predict_offset_s=cfg.tau, entry_window_s=max(cfg.win, 0.0),
                                   min_net_edge=cfg.min_edge if cfg.min_edge > -9 else -1.0, max_spread=cfg.max_spread,
                                   max_entry_price=cfg.max_ask, min_entry_price=cfg.min_ask, order_usdc=cfg.usd,
                                   max_depth_fraction=cfg.depth_frac,
                                   kappa=float(S.kappa_at(np.array([e]), cfg.edge_hold)[0]),
                                   exit_half_spread=float(S.half_spread_at(np.array([e + cfg.edge_hold]))[0]),
                                   expected_adverse=cfg.adverse)
        fs = FeeSchedule(rate=float(D.rate[r]))
        dec = nm.evaluate_entry(mcfg, pred, open_ms + e * 1000.0, q, fs)
        if cfg.min_edge > -9 and dec.edge is not None:
            assert abs(dec.edge.net_if_filled - float(o["edge_d"][k])) < 1e-9, (dec.edge.net_if_filled, o["edge_d"][k])
        t_arr = float(o["t_arr"][k])
        timeline = {"market_start": f"{open_ms:.0f}", "entry_decision": f"{open_ms + e * 1000:.0f} (+{e:.2f}s)",
                    "order_submit": f"{open_ms + e * 1000 + S.pre[r, 0] * cfg.lat_scale:.0f}",
                    "order_arrival": f"{open_ms + t_arr * 1000:.0f} (+{t_arr:.2f}s)",
                    "fill": f"{open_ms + t_arr * 1000:.0f} ({o['qty'][k]:.2f} shares)",
                    "entry_price": f"{o['vw_in'][k]:.4f}"}
        out.append(nm.format_prediction(pred, q, dec, timeline))
        rate = float(D.rate[r])
        fin = rate * o["vw_in"][k] * (1 - o["vw_in"][k])
        px = float(x["px"][j])
        fout = 0.0 if x["settled"][j] else rate * px * (1 - px)
        out.append(nm.format_exit({
            "asset": pred.asset, "side": "UP" if o["up"][k] else "DOWN",
            "hold_time": f"{x['t'][j] - t_arr:.2f}s", "entry_ask": f"{o['ask_d'][k]:.2f}",
            "current_bid": f"{x['first_bid'][j]:.2f}", "markout": f"{px - o['vw_in'][k]:+.4f}",
            "fees": f"{fin + fout:.4f}/share", "slippage": f"{o['vw_in'][k] - o['ask_d'][k]:+.4f} entry, "
            f"{(x['first_bid'][j] - px) if x['first_bid'][j] == x['first_bid'][j] else 0:+.4f} exit",
            "net_pnl": f"{o['qty'][k] * (px - o['vw_in'][k] - fin - fout):+.4f} USDC", "exit_reason": exit_name(ex)}))
        path = [f"  +{h:g}s bid={S.side_px(np.array([r]), o['up'][k:k + 1], np.array([e + h]), 'bid')[0]}"
                for h in (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0)]
        out.append("what the bid did (item 39):\n" + "\n".join(path))
    return "\n\n".join(out)


LOCK_PATH = RES / "next_market_twap_locked.json"


def do_lock() -> dict:
    """Read the TRAIN/VALIDATION sweep, apply the selection protocol and write the lock. Never reads TEST."""
    import datetime
    rows = read_csv(RES / "next_market_sweep.csv")
    sel = select(rows)
    locked = []
    for d, tr, va, nb in sel["survivors"][:3]:
        locked.append({"label": "survivor", "key": d, "train": tr, "validation": va, "neighbour_median_c": nb})
    # whatever survives, the least-bad TRAIN-ranked configurations with enough volume are carried to TEST too,
    # so the report can show what the best-looking in-sample rules do out of sample
    tr = [r for r in rows if r["split"] == "train" and r["trades"] >= 30]
    va = {tuple(r[k] for k in KEY): r for r in rows if r["split"] == "validation"}
    tr.sort(key=lambda r: -r["c_share"])
    for r in tr[:3]:
        k = {kk: r[kk] for kk in KEY}
        locked.append({"label": "best TRAIN", "key": k, "train": r, "validation": va.get(tuple(k[x] for x in KEY))})
    out = {"locked_at_utc": datetime.datetime.utcnow().isoformat(timespec="seconds"), "stats": sel["stats"],
           "locked": locked}
    LOCK_PATH.write_text(json.dumps(out, indent=1, default=float))
    return out



def asset_table(S: Sim, cfg: Cfg, ex: tuple, splits) -> str:
    """Item 22: every asset on its own, trading and prediction quality side by side."""
    D = S.D
    res, tt, _ = run(S, cfg, splits, ex)
    o = res["orders"]
    oa = D.asset[o["rows"]]
    rows = []
    for a, name in enumerate(RB.ASSETS):
        sm = summarize(sub(tt, tt["asset"] == a), int(np.sum(oa == a)))
        m = np.isin(D.split, splits) & (D.tau == cfg.tau) & (D.y >= 0) & (D.asset == a) & ~np.isnan(S.P[cfg.model])
        p, y = S.P[cfg.model][m], D.y[m]
        if not sm.get("trades"):
            rows.append([name, sm["orders"], 0] + [float("nan")] * 12 + [acc(p, y) * 100, ece(p, y) * 100])
            continue
        rows.append([name, sm["orders"], sm["trades"], sm["fill"] * 100, sm["win"] * 100, sm["avg_entry"], sm["avg_exit"],
                     sm["hold_s"], f"${sm['gross']:+.2f}", f"${sm['fees']:.2f}", f"${sm['slip']:.2f}", f"${sm['net']:+.2f}",
                     sm["c_share"], f"${sm['dd']:.2f}", sm["sharpe_day"], acc(p, y) * 100, ece(p, y) * 100])
    return md_table(["asset", "opportunities (orders)", "trades", "fill %", "win %", "avg entry", "avg exit", "median hold s",
                     "gross", "fees", "slippage", "net", "net c/share", "max DD", "Sharpe-like (daily)",
                     "model acc %", "model ECE (c)"], rows)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grid")
    g.add_argument("--procs", type=int, default=3)
    sub.add_parser("diag")
    sub.add_parser("models")
    sub.add_parser("report")
    sub.add_parser("lock")
    sub.add_parser("final")
    sw = sub.add_parser("sweep")
    sw.add_argument("--procs", type=int, default=3)
    args = ap.parse_args(argv)
    if args.cmd == "grid":
        build_grid(args.procs)
    elif args.cmd == "diag":
        txt = diag(Data())
        (OUT / "diag_train.md").write_text(txt)
        print(txt)
    elif args.cmd == "sweep":
        run_sweep(args.procs)
    elif args.cmd == "lock":
        print(json.dumps(do_lock(), indent=1, default=float))
    elif args.cmd == "final":
        lk = json.loads(LOCK_PATH.read_text())
        locked = []
        for i, it in enumerate(lk["locked"]):
            c, e = cfg_from_key(it["key"])
            locked.append((f"L{i + 1} {it['label']}: {it['key']['model']} T{c.tau:+g}s win {c.win:g}s edge "
                           f"{'none' if c.min_edge <= -9 else f'{c.min_edge * 100:g}c'} spread<={c.max_spread * 100:g}c "
                           f"ask<={c.max_ask:g}, {exit_name(e)}", c, e))
        base = lk["locked"][0]["key"]
        txt = final_report(locked, read_csv(RES / "next_market_sweep.csv"), base)
        (OUT / "final_report.md").write_text(txt)
        print(txt)
    elif args.cmd == "report":
        txt = dev_report(load_sim())
        (OUT / "dev_report.md").write_text(txt)
        print(txt)
    elif args.cmd == "models":
        txt = models_report(Data())
        (OUT / "models.md").write_text(txt)
        print(txt)


if __name__ == "__main__":
    main()
