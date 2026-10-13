"""Spot-lag: realistic execution simulator + parameter optimisation (2026-09-25 brief).

    python scripts/research_spot_lag_realistic.py build  [--scale 1.0 --proxy 500 --draws 6 --tag base]
    python scripts/research_spot_lag_realistic.py sweep  [--tag base]      # stage 1: the entry grid x draws x 6 h blocks
    python scripts/research_spot_lag_realistic.py analyze                   # stages 2-4, walk-forward, MC, stress, report tables

RESEARCH ONLY. Reads the research feature cache (research_repriced_momentum.py cache), writes
data/replay/sl_real/ and results/spot_lag_realistic_*. Changes no strategy code or process.

The simulated sequence, per spot shock (archive point i, LOCAL receive time t_i):
    t_i          the shock is on this machine (the archive IS the bot's received feed, so the
                 spot feed latency is already inside it; the spot's age at the decision is
                 spot_feed_latency + decision latency)
    t_dec        = t_i + decision latency (measured signal_processing_ms)
    t_sent       = t_dec + presend (measured decision -> POST)
    t_arr        = t_sent + RTT / 2 (measured venue round trip)
    venue book   = the archived local book at t_arr + proxy_ms (500 ms: validated 2026-09-23
                 against venue-timestamped books; stress: 750 / 1000)
    fill         FOK: filled at the venue ask iff ask <= limit AND top-level depth >= size;
                 else NO FILL. No partial fills (the live order is FOK), no fill at the
                 decision price, never a mid price.
    t_known      = t_arr + RTT / 2 (the fill is known here; the exit clock starts at t_arr)
    exits        FOK sells at a floor (bid at a target, bid - 2c otherwise) with the same
                 latency draws, depth >= size, retried every 2 s; last 10 s = settlement.
    fees         rate * p * (1 - p) per share on every taker leg.
Every latency stage is drawn from the LIVE bot's measured samples
(data/replay/live_latency_samples.json, n = 41-52 each) x U(0.9, 1.1) x scale.
Quote age at the decision = (t_dec - last archived change of that side's book) + book latency
draw; spot age = spot feed latency draw + decision latency. Both are gated like the live bot.
The archive has top-of-book depth only: an order larger than the top level does not fill
(conservative; walking deeper levels cannot be simulated).
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from troll_poly_bot.backtest.spot_shock_replay import SETTLE_GUARD_MS, Execution, detect_shocks, load_asset, windows  # noqa: E402
import research_repriced_momentum as R                                               # noqa: E402

CACHE = R.CACHE
OUT = ROOT / "data/replay/sl_real"
RES = ROOT / "results"
ASSETS = R.ASSETS
SPLITS = R.SPLITS
LAT = json.loads((ROOT / "data/replay/live_latency_samples.json").read_text())
#: the untouched final test: windows opening after 00:45 UTC 2026-09-25 (no parameter was
#: chosen on them; the paper-history audit of 13:00 UTC looked at 6 fixed settings there)
TEST_AFTER = 1790297100
BLOCK_MS = 6 * 3600 * 1000.0
T0_BLOCKS = (TEST_AFTER - 10 * 21600) * 1000.0   # 2026-09-22 12:45 UTC: block 10 starts exactly at TEST_AFTER
SLIP_C, UNC_C, MIN_ASK = 0.005, 0.005, 0.05
K_SET = (2.5, 3.0, 3.5, 4.0, 5.0)
EDGES = (0.01, 0.02, 0.03, 0.04, 0.06)
SLIPS = (0.01, 0.02, 0.03)
SPREADS = (0.02, 0.03, 0.05)
MAX_ASKS = (0.50, 0.60, 0.70, 0.74, 0.80, 0.95)
WINDOWS = ((60, 295), (60, 240), (60, 180), (30, 180), (30, 120), (60, 120), (30, 90))
QAGES = (100, 200, 250, 300, 500, 750, 1000, 1500)
SAGES = (300, 500, 1000, 2000)
SIZES = (5, 10, 15, 25, 50)                        # share buckets the exits are simulated for
SIZE_USD = (0.0, 1.0, 2.0, 3.0, 5.0, 10.0)         # 0 = the venue minimum (5 shares)
EXITS5 = ("live", "t3", "t5", "t10", "t20", "tp3_10", "tp5_10", "tp5_20", "tp10_20", "tp10_settle",
          "sl3_10", "sl5_10", "tp5sl3_20", "tp10sl5_20", "tp5sl5_settle")
SLOTS = EXITS5 + tuple(f"live_s{s}" for s in SIZES[1:])
LOOSE_SPREAD, LOOSE_ASK = max(SPREADS), 0.98
PROD = dict(k=3.0, edge=0.02, slip=0.02, spread=0.03, max_ask=0.95, window=(60, 295), qage=250, sage=1000)
CAND = ("split", "epoch", "asset", "t", "dir", "ask", "bid", "ev", "rate", "left", "z1", "z3", "sigma_bps", "tok1",
        "adep", "bdep", "kmask", "qage_local", "mid5", "bid5", "bdep5", "test")
DRAW = ("spot", "book", "proc", "pre", "rtt", "t_arr", "arr_ask", "arr_adep", "t_known")
XC = ("px", "hold", "flag", "xbid", "xmid")


class RealExec(Execution):
    """Execution with every latency stage drawn from the live bot's measured samples."""

    def __init__(self, seed: int, scale: float = 1.0, proxy_ms: float = 500.0, size: float = 5.0) -> None:
        super().__init__(seed=seed, proxy_ms=proxy_ms, size=size)
        self.scale = scale
        self.last: dict = {}

    def _d(self, key: str) -> float:
        return self.rng.choice(LAT[key]) * self.rng.uniform(0.9, 1.1) * self.scale

    def draw(self) -> dict:
        d = {k: self._d(v) for k, v in (("spot", "spot_feed_latency_ms"), ("book", "book_latency_ms"),
                                         ("proc", "signal_processing_ms"), ("pre", "presend_ms"), ("rtt", "venue_rtt_ms"))}
        self.last = d
        return d

    def latency(self) -> tuple[float, float]:
        """(decision -> POST, POST -> venue) for the exit legs: fresh draws each time."""
        return self._d("signal_processing_ms") + self._d("presend_ms"), self._d("venue_rtt_ms") / 2.0


def _last_change_idx(*arrs) -> np.ndarray:
    a = np.stack([np.nan_to_num(np.asarray(x, dtype=float), nan=-1.0) for x in arrs])
    ch = np.any(a[:, 1:] != a[:, :-1], axis=0)
    n = a.shape[1]
    return np.maximum.accumulate(np.where(np.r_[True, ch], np.arange(n), 0))


def _live_exit(w, ex, sv, t_arr, t_known, dr, r1, won):
    """The production rule: 5 s after the fill, extended to 10 s while the 1 s spot move is
    >= 0.5 sigma our way. Returns (px, hold, flag, xbid, xmid) or None."""
    t = w.t
    z1s = dr * r1 / w.sigma
    j = w.first_at(t_known)
    t_sell = None
    while j < len(w) and t[j] < w.close - SETTLE_GUARD_MS:
        held = (t[j] - t_arr) / 1000.0
        if held >= 5.0 and not (held < 10.0 and z1s[j] == z1s[j] and z1s[j] >= 0.5):
            t_sell = float(t[j])
            break
        j += 1
    if t_sell is None:
        return (won, (w.close - t_arr) / 1000.0, 2.0, np.nan, np.nan) if won is not None else None
    jd = w.last_at(t_sell)
    b = float(sv["bid"][jd]) if jd >= 0 and sv["bid"][jd] == sv["bid"][jd] else float("nan")
    mm = float(sv["mid"][jd]) if jd >= 0 and sv["mid"][jd] == sv["mid"][jd] else float("nan")
    px, how, t_out, _ = ex.sell(w, sv, t_sell, (b - 0.02) if b == b else 0.0, won)
    if px is None:
        return None
    return px, (t_out - t_arr) / 1000.0, 2.0 if how == "SETTLE" else 0.0, b, mm


def _parse_exit(name: str):
    """-> (tp, sl, max_hold_s) for research_repriced_momentum._tp_exit; None for 'live'."""
    if name == "live":
        return None
    if name.startswith("t") and not name.startswith("tp"):
        return 0.0, 0.0, float(name[1:])
    body, mx = name.split("_")
    tp = sl = 0.0
    if "sl" in body:
        a, b = body.split("sl")
        tp = float(a[2:]) / 100 if a.startswith("tp") else 0.0
        sl = float(b) / 100
    elif body.startswith("tp"):
        tp = float(body[2:]) / 100
    return tp, sl, 0.0 if mx == "settle" else float(mx)


def build_asset(args) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    asset, scale, proxy, draws = args
    d, meta = load_asset(CACHE, asset)
    ex = RealExec(seed=sum(map(ord, asset)) * 13 + 7, scale=scale, proxy_ms=proxy)
    C, D, X = [], [], []
    for m, w in zip(meta, windows(d, meta)):
        if len(w) < 30:
            continue
        t = w.t
        r1 = R._ret(t, w.lp, 1000.0)
        k1000 = w.lagged_index(1000.0)
        marks: dict[tuple[int, int], int] = {}
        for b, k in enumerate(K_SET):
            for i, dr in detect_shocks(w, k=k, k3=0.5, accel=True, min_left=min(x[0] for x in WINDOWS),
                                       max_left=max(x[1] for x in WINDOWS)):
                marks[(i, dr)] = marks.get((i, dr), 0) | (1 << b)
        z1a = w.ret(1000.0) / w.sigma
        z3a = w.ret(3000.0) / (w.sigma * math.sqrt(3.0))
        lc = {s: _last_change_idx(w.side[s]["ask"], w.side[s]["bid"], w.side[s]["adep"], w.side[s]["bdep"]) for s in ("UP", "DOWN")}
        won_up = w.won_up
        for (i, dr), mask in sorted(marks.items()):
            side = "UP" if dr > 0 else "DOWN"
            sv = w.side[side]
            mid, fvs, ask, bid, adep, bdep = sv["mid"], sv["mfv"], sv["ask"], sv["bid"], sv["adep"], sv["bdep"]
            i0 = k1000[i]
            if i0 < 0 or t[i] - t[i0] > 1600.0 or mid[i0] != mid[i0] or fvs[i0] != fvs[i0] or ask[i] != ask[i] \
                    or bid[i] != bid[i] or fvs[i] != fvs[i]:
                continue
            a0, b0 = float(ask[i]), float(bid[i])
            ev = min(max(float(mid[i0]) + float(fvs[i] - fvs[i0]), 0.0), 1.0)
            if not (MIN_ASK <= a0 <= LOOSE_ASK and a0 - b0 <= LOOSE_SPREAD + 1e-9 and ev - a0 > 0):
                continue
            won = None if won_up is None else float((dr > 0) == bool(won_up))
            j5 = w.last_at(t[i] + 5000.0)
            row = [SPLITS.index(w.split) if w.split in SPLITS else -1, w.epoch, ASSETS.index(asset), float(t[i]), dr,
                   a0, b0, ev, w.rate, float(w.left[i]), float(dr * z1a[i]), float(dr * z3a[i]), float(w.sigma[i] * 1e4),
                   float(dr * (mid[i] - mid[i0])) if mid[i] == mid[i] else np.nan, float(adep[i] or 0.0), float(bdep[i] or 0.0),
                   float(mask), float(t[i] - t[lc[side][i]]),
                   float(mid[j5]) if j5 >= 0 and mid[j5] == mid[j5] else np.nan,
                   float(bid[j5]) if j5 >= 0 and bid[j5] == bid[j5] else np.nan,
                   float(bdep[j5] or 0.0) if j5 >= 0 else 0.0, 1.0 if w.epoch > TEST_AFTER else 0.0]
            Dn = np.full((draws, len(DRAW)), np.nan)
            Xn = np.full((draws, len(SLOTS), len(XC)), np.nan, dtype=np.float32)
            for q in range(draws):
                L = ex.draw()
                t_dec = t[i] + L["proc"]
                t_arr = t_dec + L["pre"] + L["rtt"] / 2.0
                t_known = t_arr + L["rtt"] / 2.0
                ja = w.first_at(t_arr + proxy)
                arr_ask = arr_dep = np.nan
                if ja < len(w) and t[ja] < w.close - SETTLE_GUARD_MS and ask[ja] == ask[ja]:
                    arr_ask, arr_dep = float(ask[ja]), float(adep[ja] or 0.0)
                Dn[q] = (L["spot"], L["book"], L["proc"], L["pre"], L["rtt"], t_arr, arr_ask, arr_dep, t_known)
                if arr_ask != arr_ask or arr_dep < SIZES[0]:
                    continue
                entry = arr_ask
                for s, name in enumerate(SLOTS):
                    size = SIZES[0] if s < len(EXITS5) else SIZES[1 + s - len(EXITS5)]
                    if size > arr_dep:
                        continue
                    ex.size = float(size)
                    if name.startswith("live"):
                        got = _live_exit(w, ex, sv, t_arr, t_known, dr, r1, won)
                        if got is not None:
                            Xn[q, s] = got
                    else:
                        tp, sl, mx = _parse_exit(name)
                        got = R._tp_exit(w, sv, ex, entry, t_arr, t_known, tp, sl, mx, won)
                        if got is not None:
                            px, hold, flag, _ = got
                            jd = w.last_at(t_arr + hold * 1000.0)
                            Xn[q, s] = (px, hold, flag, bid[jd] if jd >= 0 else np.nan, mid[jd] if jd >= 0 else np.nan)
                ex.size = float(SIZES[0])
            C.append(row)
            D.append(Dn)
            X.append(Xn)
    return (np.array(C, dtype=np.float64).reshape(-1, len(CAND)), np.array(D).reshape(-1, draws, len(DRAW)),
            np.array(X, dtype=np.float32).reshape(-1, draws, len(SLOTS), len(XC)))


def build(scale: float, proxy: float, draws: int, tag: str, procs: int = 3) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(procs) as pool:
        parts = list(pool.map(build_asset, [(a, scale, proxy, draws) for a in ASSETS]))
    C = np.concatenate([p[0] for p in parts if len(p[0])])
    D = np.concatenate([p[1] for p in parts if len(p[1])])
    X = np.concatenate([p[2] for p in parts if len(p[2])])
    o = np.argsort(C[:, CAND.index("t")], kind="stable")
    out = OUT / f"cands_{tag}.npz"
    np.savez_compressed(out, C=C[o], D=D[o], X=X[o], scale=scale, proxy=proxy)
    return out


# ─────────────────────────────── simulation ───────────────────────────────


class Data:
    def __init__(self, tag: str = "base") -> None:
        z = np.load(OUT / f"cands_{tag}.npz")
        C, D, X = z["C"], z["D"], z["X"].astype(np.float64)
        keep = C[:, CAND.index("asset")] != ASSETS.index("HYPE")
        self.C, self.D, self.X = C[keep], D[keep], X[keep]
        for n in ("ask", "bid"):
            self.C[:, CAND.index(n)] = np.round(self.C[:, CAND.index(n)], 6)
        self.D[:, :, DRAW.index("arr_ask")] = np.round(self.D[:, :, DRAW.index("arr_ask")], 6)
        self.X[:, :, :, 0] = np.round(self.X[:, :, :, 0], 6)
        self.col = {n: self.C[:, i] for i, n in enumerate(CAND)}
        self.draws = self.D.shape[1]
        a, b, ev, rate = self.col["ask"], self.col["bid"], self.col["ev"], self.col["rate"]
        evc = np.clip(ev, 0.01, 0.99)
        fixed = SLIP_C + UNC_C + rate * evc * (1 - evc) + (a - b) / 2.0
        self.me = {e: self._max_entry(ev - fixed - e, rate) for e in EDGES}
        split = self.col["split"].astype(int)
        per = np.array([SPLITS[s] if s >= 0 else "none" for s in split], dtype=object)
        per[self.col["test"] == 1.0] = "test"
        self.period = per
        self.block = np.floor((self.col["t"] - T0_BLOCKS) / BLOCK_MS).astype(int)
        self.nblocks = int(self.block.max()) + 1
        ep = self.col["epoch"]
        self.hours = {p: len(np.unique(ep[per == p])) / 12.0 for p in set(per)}
        self.block_hours = np.array([len(np.unique(ep[self.block == b])) / 12.0 for b in range(self.nblocks)])
        # net per share for every (candidate, draw, slot): exit px - entry - both fees (settlement: no exit fee)
        arr = self.D[:, :, DRAW.index("arr_ask")]
        px, flag = self.X[:, :, :, 0], self.X[:, :, :, 2]
        fee = lambda p: rate[:, None, None] * p * (1 - p)                                   # noqa: E731
        self.net = px - arr[:, :, None] - fee(arr[:, :, None]) - np.where(flag == 0.0, fee(np.nan_to_num(px)), 0.0)
        self.gross = px - arr[:, :, None]

    @staticmethod
    def _max_entry(budget, rate):
        p0 = np.floor((np.minimum(budget, 0.99) + 1e-9) * 100) / 100
        out = np.full(budget.shape, np.nan)
        for s in (3, 2, 1, 0):
            p = np.round(p0 - s * 0.01, 2)
            ok = (p >= 0.01) & (p + rate * p * (1 - p) <= budget + 1e-12)
            out = np.where(ok, p, out)
        return out


def cfg_tuple(**kw):
    c = dict(PROD)
    c.update(kw)
    return (c["k"], c["edge"], c["slip"], c["spread"], c["max_ask"], tuple(c["window"]), c["qage"], c["sage"])


def simulate(d: Data, cfg, draw: int, *, exit_name: str = "live", size_usd: float = 0.0, positions: int = 4,
             exposure: float = 10.0, per_hour: int = 60, attempts_max: int = 3, retry_ms: float = 2000.0,
             max_order: float = 5.0, depth_mult: float = 1.0, slip_extra: float = 0.0, fee_mult: float = 1.0,
             drop: np.ndarray | None = None):
    """One configuration through the production budget in time order. Returns
    (order idx, filled flags, shares, net USDC per order (0 if not filled), net per share)."""
    k, edge, slip, spread, max_ask, (lo, hi), qmax, smax = cfg
    col, Dq = d.col, d.D[:, draw]
    me = d.me[edge]
    bit = 1 << K_SET.index(k)
    ask = col["ask"]
    qage = col["qage_local"] + Dq[:, DRAW.index("proc")] + Dq[:, DRAW.index("book")]
    sage = Dq[:, DRAW.index("spot")] + Dq[:, DRAW.index("proc")]
    sig = (((col["kmask"].astype(int) & bit) > 0) & (col["left"] >= lo) & (col["left"] <= hi)
           & (ask >= MIN_ASK) & (ask <= max_ask + 1e-9) & (ask - col["bid"] <= spread + 1e-9)
           & (col["ev"] - ask > 0) & (me >= ask - 1e-9) & (qage <= qmax) & (sage <= smax))
    idx = np.nonzero(sig)[0]
    cap = np.minimum(np.round(ask[idx] + slip, 2), me[idx])
    shares = np.full(len(idx), float(SIZES[0])) if size_usd <= 0 else np.maximum(SIZES[0], np.floor(size_usd / cap))
    bucket = np.searchsorted(np.array(SIZES), shares)                     # smallest simulated size >= shares
    bucket = np.minimum(bucket, len(SIZES) - 1)
    bsize = np.array(SIZES)[bucket]
    slot = np.where(bucket == 0, SLOTS.index(exit_name), len(EXITS5) + bucket - 1)
    arr_ask, arr_dep = Dq[idx, DRAW.index("arr_ask")], Dq[idx, DRAW.index("arr_adep")]
    netps = d.net[idx, draw, slot]
    fill = (arr_ask == arr_ask) & (arr_ask <= cap + 1e-9) & (arr_dep * depth_mult >= bsize) & ~np.isnan(netps)
    if drop is not None:
        fill &= ~drop[idx]
    if slip_extra or fee_mult != 1.0:
        rate = col["rate"][idx] * fee_mult
        px = d.X[idx, draw, slot, 0]
        flag = d.X[idx, draw, slot, 2]
        e2 = arr_ask + slip_extra
        x2 = np.where(flag == 0.0, px - slip_extra, px)
        netps = x2 - e2 - rate * e2 * (1 - e2) - np.where(flag == 0.0, rate * x2 * (1 - x2), 0.0)
    t, asset, epoch = col["t"][idx], col["asset"][idx], col["epoch"][idx]
    known, lat, hold = Dq[idx, DRAW.index("t_known")], Dq[idx, DRAW.index("t_arr")] - t, d.X[idx, draw, slot, 1]
    busy: list = []
    buys: deque = deque()
    att: dict = defaultdict(int)
    entered, last = set(), {}
    o_idx, o_fill, o_sh, o_net, o_nps = [], [], [], [], []
    for j in range(len(idx)):
        if cap[j] * shares[j] > max_order + 1e-9:
            continue
        t0 = t[j]
        mk = (asset[j], epoch[j])
        if mk in entered or att[mk] >= attempts_max or t0 - last.get(mk, -1e18) < retry_ms:
            continue
        if busy:
            busy = [b for b in busy if b[0] > t0]
        if len(busy) >= positions or sum(b[1] for b in busy) + cap[j] * shares[j] > exposure + 1e-9:
            continue
        while buys and buys[0] <= t0 - 3_600_000.0:
            buys.popleft()
        if len(buys) >= per_hour:
            continue
        buys.append(t0)
        att[mk] += 1
        last[mk] = t0
        o_idx.append(idx[j])
        o_fill.append(bool(fill[j]))
        o_sh.append(shares[j])
        if fill[j]:
            entered.add(mk)
            o_net.append(netps[j] * shares[j])
            o_nps.append(netps[j])
            busy.append((t0 + lat[j] + hold[j] * 1000.0, arr_ask[j] * shares[j]))
        else:
            o_net.append(0.0)
            o_nps.append(0.0)
            busy.append((known[j], cap[j] * shares[j]))
    return (np.array(o_idx, dtype=int), np.array(o_fill, dtype=bool), np.array(o_sh), np.array(o_net), np.array(o_nps))


def stats(d: Data, o_idx, o_fill, o_sh, o_net, o_nps, mask=None, hours: float | None = None) -> dict:
    if mask is not None:
        keep = mask[o_idx]
        o_idx, o_fill, o_net, o_nps = o_idx[keep], o_fill[keep], o_net[keep], o_nps[keep]
    f = o_fill
    n = int(f.sum())
    out = {"orders": len(o_idx), "fills": n, "fill_rate": n / len(o_idx) if len(o_idx) else np.nan,
           "fills_per_h": n / hours if hours else np.nan}
    if n == 0:
        out.update(net_usdc=0.0, c_share=np.nan, win=np.nan, t=np.nan, max_dd=0.0, pf=np.nan, worst=0.0, streak=0,
                   sharpe=np.nan, avg_win=np.nan, avg_loss=np.nan)
        return out
    net, nps = o_net[f], o_nps[f]
    ep = d.col["epoch"][o_idx[f]]
    u, inv = np.unique(ep, return_inverse=True)
    v = np.bincount(inv, weights=net)
    se = v.std(ddof=1) / math.sqrt(len(v)) if len(v) > 2 else np.nan
    cum = np.concatenate([[0.0], np.cumsum(net)])
    streak = best = 0
    for x in net:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    wins, losses = net[net > 0], net[net <= 0]
    out.update(net_usdc=float(net.sum()), c_share=float(nps.mean() * 100), win=float((net > 0).mean()),
               t=float(v.mean() / se) if se == se and se > 0 else np.nan,
               max_dd=float(np.max(np.maximum.accumulate(cum) - cum)),
               pf=float(wins.sum() / -losses.sum()) if losses.sum() < 0 else np.inf, worst=float(net.min()),
               streak=best, sharpe=float(v.mean() / v.std(ddof=1) * math.sqrt(len(v))) if len(v) > 2 and v.std(ddof=1) > 0 else np.nan,
               avg_win=float(wins.mean()) if len(wins) else 0.0, avg_loss=float(losses.mean()) if len(losses) else 0.0,
               epochs=len(v))
    return out


# ─────────────────────────────── stage 1 sweep ───────────────────────────────


def configs():
    return [cfg_tuple(k=k, edge=e, slip=s, spread=sp, max_ask=m, window=w)
            for k, e, s, sp, m, w in itertools.product(K_SET, EDGES, SLIPS, SPREADS, MAX_ASKS, WINDOWS)]


def _sweep_chunk(args):
    tag, cfgs = args
    d = Data(tag)
    nb = d.nblocks
    out = np.zeros((len(cfgs), d.draws, nb, 5), dtype=np.float32)   # orders, fills, net, sumsq, wins per block
    dd = np.zeros((len(cfgs), d.draws), dtype=np.float32)
    for ci, cfg in enumerate(cfgs):
        for q in range(d.draws):
            o_idx, o_fill, _, o_net, _ = simulate(d, cfg, q)
            if len(o_idx) == 0:
                continue
            b = d.block[o_idx]
            out[ci, q, :, 0] = np.bincount(b, minlength=nb)
            out[ci, q, :, 1] = np.bincount(b, weights=o_fill, minlength=nb)
            out[ci, q, :, 2] = np.bincount(b, weights=o_net, minlength=nb)
            out[ci, q, :, 3] = np.bincount(b, weights=o_net ** 2, minlength=nb)
            out[ci, q, :, 4] = np.bincount(b, weights=(o_net > 0), minlength=nb)
            cum = np.concatenate([[0.0], np.cumsum(o_net[o_fill])])
            dd[ci, q] = np.max(np.maximum.accumulate(cum) - cum)
    return out, dd


def sweep(tag: str, procs: int = 3) -> Path:
    cf = configs()
    nch = procs * 4
    chunks = [cf[i::nch] for i in range(nch)]
    with ProcessPoolExecutor(procs) as pool:
        parts = list(pool.map(_sweep_chunk, [(tag, c) for c in chunks]))
    d = Data(tag)
    S = np.zeros((len(cf), d.draws, d.nblocks, 5), dtype=np.float32)
    DD = np.zeros((len(cf), d.draws), dtype=np.float32)
    for i, (o, dd) in enumerate(parts):
        S[i::nch] = o
        DD[i::nch] = dd
    out = RES / f"spot_lag_realistic_sweep_{tag}.npz"
    np.savez_compressed(out, S=S, DD=DD, cfgs=np.array([json.dumps(c) for c in cf]), block_hours=d.block_hours)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--scale", type=float, default=1.0)
    b.add_argument("--proxy", type=float, default=500.0)
    b.add_argument("--draws", type=int, default=6)
    b.add_argument("--tag", default="base")
    b.add_argument("--procs", type=int, default=3)
    s = sub.add_parser("sweep")
    s.add_argument("--tag", default="base")
    s.add_argument("--procs", type=int, default=3)
    args = ap.parse_args()
    if args.cmd == "build":
        print("built", build(args.scale, args.proxy, args.draws, args.tag, args.procs))
    elif args.cmd == "sweep":
        print("wrote", sweep(args.tag, args.procs))


if __name__ == "__main__":
    main()
