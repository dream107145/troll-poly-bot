"""Spot-shock replay on the 5-minute chart archive: shared by the spot-lag and
repriced-momentum research, so both are judged by ONE execution model.

Features (causal, per archive point, ~220 ms apart)
---------------------------------------------------
From ``data/charts/<slug>.json`` via ``momentum_replay.load_window``: the
composite spot, both tokens' touch and depth. Per asset, stitched over
windows: the TwoScaleVol sigma (``VolConfig``), the TWAP state, the analytic
fair value ``twap_fair_value`` (information lag 0.18 s, the measured spot
feed lag) and the market-implied fair value MFV (``strategy.spot_lag``'s
frozen curve). Nothing at a point uses data stamped after it. The archive
cannot resolve 100 ms (points are ~220 ms apart); the shortest meaningful
lookback is ~250 ms.

Execution (the model validated against venue-timestamped books)
----------------------------------------------------------------
* an order leaves ``presend`` ms after the decision and reaches the venue
  ``RTT/2`` later, both drawn from the MEASURED priors (execution/latency.py);
* the venue's book at time T is read as the archived LOCAL book at T + 500 ms
  (on the 2026-09-23 tick recording this reproduced the venue-truth P&L of
  694 spot-shock orders; the 2 s quote-survival rule dropped the winners);
* buys are FOK at a limit: filled at the (proxied) ask if it is at or under
  the limit with ``size`` shares behind it, else killed -- no partial fills,
  no fills at the decision price;
* sells are FOK at a floor (the bid for a take-profit, bid - 2c otherwise),
  retried every 2 s at a new floor; inside the market's last 10 s a position
  is held to settlement (payout 1/0, no exit fee);
* taker fee ``rate * p * (1 - p)`` per share on every taker fill, the window's
  own rate.

Splits are chronological by 5-minute epoch (``chronological_splits``).
"""
from __future__ import annotations

import json
import math
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import VolConfig
from ..execution.latency import PRESEND_PRIOR_MS, VENUE_RTT_PRIOR_MS
from ..pricing.twap import TwapState, twap_fair_value
from ..pricing.vol import EwmaVol, TwoScaleVol
from ..strategy.spot_lag import MFV_COEF
from .momentum_replay import load_window

INFO_LAG_S = 0.18
PROXY_MS = 500.0
SIZE = 5.0
SETTLE_GUARD_MS = 10_000.0
FEATURE_COLS = ("t", "left", "spot", "sigma", "p", "ub", "ua", "ubd", "uad", "db", "da", "dbd", "dad",
                "tfi", "obi", "orate", "bn", "byb", "cb")
#: copied from the chart point as recorded (NaN where it had nothing): top of both books,
#: the spot order flow (15 s trade-flow imbalance, book imbalance, trades/s) and each
#: exchange's own price
RAW_COLS = ("ub", "ua", "ubd", "uad", "db", "da", "dbd", "dad", "tfi", "obi", "orate", "bn", "byb", "cb")


# ─────────────────────────────── features ───────────────────────────────


def _make_vol() -> TwoScaleVol:
    v = VolConfig()
    return TwoScaleVol(fast=EwmaVol(grid_ms=v.grid_ms, halflife_s=v.halflife_s, floor_per_sec=v.floor_per_sec,
                                    ceil_per_sec=v.ceil_per_sec),
                       slow=EwmaVol(grid_ms=v.slow_grid_ms, halflife_s=v.slow_halflife_s,
                                    floor_per_sec=v.floor_per_sec, ceil_per_sec=v.ceil_per_sec),
                       default_ratio=v.default_ratio)


def chronological_splits(epochs, bounds=(0.5, 0.7, 0.85),
                         names=("train", "validation", "unseen_a", "unseen_b")) -> dict[int, str]:
    """Epoch -> split, in time order: the first 50 % train, then 20 % validation,
    then two unseen periods of 15 % each (by default)."""
    eps = sorted(set(epochs))
    n = len(eps)
    cuts = [int(n * b) for b in bounds]
    out = {}
    for k, e in enumerate(eps):
        idx = sum(1 for c in cuts if k >= c)
        out[e] = names[idx]
    return out


def _build_asset(args) -> tuple[str, int, int]:
    asset, paths, split, strikes, out_dir = args
    ws = [w for w in (load_window(p) for p in paths) if w is not None]
    ws.sort(key=lambda w: w.open_ts)
    cols = {k: [] for k in FEATURE_COLS}
    meta = []
    vol, tw, last_t = _make_vol(), TwapState(), -1e18
    for w in ws:
        strike = float(strikes.get(w.slug) or 0.0)
        if w.t[0] - last_t > 120_000:
            vol, tw = _make_vol(), TwapState()
        start = len(cols["t"])
        c = w.cols
        for i in range(len(w)):
            t = float(w.t[i]); px = float(c["spot"][i])
            if px != px or px <= 0:
                continue
            if t > last_t:
                vol.update(px, t); tw.update(px, t); last_t = t
            left = (w.close_ts - t) / 1000.0
            if not (0.0 < left <= 300.0) or not vol.ready or not tw.ready or strike <= 0:
                continue
            fv = twap_fair_value(state=tw, strike=strike, sigma_per_sec=vol.sigma_per_sec, now_ms=t,
                                 close_ts_ms=w.close_ts, info_lag_s=INFO_LAG_S)
            cols["t"].append(t); cols["left"].append(left); cols["spot"].append(px)
            cols["sigma"].append(vol.sigma_per_sec); cols["p"].append(fv.p_up)
            for k in RAW_COLS:
                cols[k].append(float(c[k][i]))
        end = len(cols["t"])
        if end > start:
            meta.append((w.slug, int(w.epoch), split.get(int(w.epoch), "?"), float(w.open_ts), float(w.close_ts),
                         -1 if w.up_won is None else int(w.up_won), start, end, float(w.fee.rate)))
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_dir / f"{asset}.npz", **{k: np.asarray(v, dtype=np.float64) for k, v in cols.items()},
                        meta=np.array(meta, dtype=object))
    return asset, len(meta), len(cols["t"])


def build_features(charts_dir: Path, out_dir: Path, procs: int = 3, split_fn=chronological_splits) -> dict:
    """Causal per-point features for every archived window, one npz per asset."""
    charts = sorted(Path(charts_dir).glob("*.json"))
    idx_path = Path(charts_dir).parent / "charts_index.json"
    strikes = {}
    if idx_path.exists():
        strikes = {k: (v or {}).get("strike") for k, v in json.loads(idx_path.read_text())["rows"].items()}
    for p in charts:                                   # windows archived after the index was built
        if not strikes.get(p.stem):
            try:
                strikes[p.stem] = json.loads(p.read_text()).get("strike")
            except (OSError, ValueError):
                pass
    epochs = [int(p.stem.rsplit("-", 1)[-1]) for p in charts]
    split = split_fn(epochs)
    by: dict[str, list] = {}
    for p in charts:
        by.setdefault(p.stem.split("-")[0].upper(), []).append(p)
    out = {}
    with ProcessPoolExecutor(procs) as pool:
        for asset, nw, npts in pool.map(_build_asset, [(a, ps, split, strikes, Path(out_dir)) for a, ps in sorted(by.items())]):
            out[asset] = (nw, npts)
    (Path(out_dir) / "splits.json").write_text(json.dumps(
        {s: [min(e for e, x in split.items() if x == s), max(e for e, x in split.items() if x == s),
             sum(1 for x in split.values() if x == s)] for s in sorted(set(split.values()))}))
    return out


def mfv(p: np.ndarray, left: np.ndarray) -> np.ndarray:
    """The market-implied fair value of the Up token (spot_lag.MFV_COEF), vectorised."""
    out = np.full(len(p), np.nan)
    x = np.log(np.clip(p, 0.01, 0.99) / (1.0 - np.clip(p, 0.01, 0.99)))
    for lo, hi, a, b in MFV_COEF:
        m = (left > lo) & (left <= hi)
        out[m] = 1.0 / (1.0 + np.exp(-(a + b * x[m])))
    return out


def load_asset(cache_dir: Path, asset: str):
    z = np.load(Path(cache_dir) / f"{asset}.npz", allow_pickle=True)
    d = {k: z[k] for k in z.files if k != "meta"}
    d["mfv"] = mfv(d["p"], d["left"])
    return d, z["meta"]


# ─────────────────────────────── windows ───────────────────────────────


@dataclass
class Window:
    """One market's arrays, with the side views the strategies read."""
    slug: str
    epoch: int
    split: str
    close: float
    won_up: bool | None
    rate: float
    t: np.ndarray
    left: np.ndarray
    lp: np.ndarray
    sigma: np.ndarray
    side: dict

    def __len__(self) -> int:
        return len(self.t)

    def first_at(self, tq: float) -> int:
        return int(np.searchsorted(self.t, tq))

    def last_at(self, tq: float) -> int:
        return int(np.searchsorted(self.t, tq, side="right")) - 1

    def ret(self, lag_ms: float) -> np.ndarray:
        """Causal spot log return over ``lag_ms`` at every point (NaN if no point that old)."""
        j = np.searchsorted(self.t, self.t - lag_ms, side="right") - 1
        ok = (j >= 0) & (self.t - self.t[np.maximum(j, 0)] <= lag_ms + 1500.0)
        return np.where(ok, self.lp - self.lp[np.maximum(j, 0)], np.nan)

    def lagged_index(self, lag_ms: float) -> np.ndarray:
        return np.searchsorted(self.t, self.t - lag_ms, side="right") - 1

    def fee(self, p: float) -> float:
        return self.rate * p * (1.0 - p)


def windows(d: dict, meta):
    for slug, ep, sp, o, c, uw, s, e, rate in meta:
        side = {}
        for name, (b, a, bd, ad) in (("UP", ("ub", "ua", "ubd", "uad")), ("DOWN", ("db", "da", "dbd", "dad"))):
            bid, ask = d[b][s:e], d[a][s:e]
            fv = d["mfv"][s:e] if name == "UP" else 1.0 - d["mfv"][s:e]
            mid = np.where(~np.isnan(bid) & ~np.isnan(ask), (bid + ask) / 2.0, np.nan)
            side[name] = {"bid": bid, "ask": ask, "bdep": d[bd][s:e], "adep": d[ad][s:e], "mfv": fv, "mid": mid}
        yield Window(slug=str(slug), epoch=int(ep), split=str(sp), close=float(c),
                     won_up=None if int(uw) < 0 else bool(uw), rate=float(rate), t=d["t"][s:e],
                     left=d["left"][s:e], lp=np.log(d["spot"][s:e]), sigma=d["sigma"][s:e], side=side)


def detect_shocks(w: Window, k: float = 3.0, k3: float = 0.5, accel: bool = True, dedupe_ms: float = 2000.0,
                  min_left: float = 60.0, max_left: float = 295.0) -> list[tuple[int, int]]:
    """The spot-lag shock rule: |1 s return| >= k sigma, the 3 s return agrees,
    accelerating, one per direction per ``dedupe_ms``. Returns (index, direction)."""
    r1, r3 = w.ret(1000.0), w.ret(3000.0)
    z1 = r1 / w.sigma
    z3 = r3 / (w.sigma * math.sqrt(3.0))
    last = {1: -1e18, -1: -1e18}
    out = []
    for i in np.where(np.abs(z1) >= k)[0]:
        if not (min_left <= w.left[i] <= max_left):
            continue
        d = 1 if z1[i] > 0 else -1
        if w.t[i] - last[d] < dedupe_ms or not (z3[i] * d >= k3):
            continue
        if accel and not (r1[i] * d > r3[i] * d / 3.0):
            continue
        last[d] = w.t[i]
        out.append((int(i), d))
    return out


# ─────────────────────────────── execution ───────────────────────────────


class Execution:
    """The venue_lag execution model. One instance per worker; its random
    stream is seeded so a replay is repeatable."""

    def __init__(self, seed: int = 0, proxy_ms: float = PROXY_MS, size: float = SIZE) -> None:
        self.rng = random.Random(seed)
        self.proxy_ms = proxy_ms
        self.size = size

    def latency(self) -> tuple[float, float]:
        """(presend, one-way) ms: decision -> POST, POST -> venue."""
        r = self.rng
        return (r.choice(PRESEND_PRIOR_MS) * r.uniform(.85, 1.15),
                r.choice(VENUE_RTT_PRIOR_MS) * r.uniform(.85, 1.15) / 2.0)

    def buy(self, w: Window, sv: dict, t_dec: float, limit: float) -> dict:
        """FOK buy decided at ``t_dec`` with a price limit. Fills at the venue ask on
        arrival if it is at or under the limit with ``size`` behind it."""
        pre, half = self.latency()
        t_arr = t_dec + pre + half
        ja = w.first_at(t_arr + self.proxy_ms)
        out = {"t_arr": t_arr, "t_known": t_arr + half, "latency_ms": pre + half, "filled": False,
               "arrival_ask": None, "quote_gone": True}
        if ja >= len(w) or w.t[ja] >= w.close - SETTLE_GUARD_MS:
            out["reason"] = "MARKET_CLOSING"
            return out
        a = sv["ask"][ja]
        out["arrival_ask"] = None if a != a else float(a)
        if a != a or a > limit + 1e-9 or (sv["adep"][ja] or 0.0) < self.size:
            out["reason"] = "QUOTE_GONE" if a == a and a > limit + 1e-9 else "NO_DEPTH"
            return out
        out.update(filled=True, entry=float(a), quote_gone=False, reason="FILLED")
        return out

    def sell(self, w: Window, sv: dict, t_dec: float, floor: float, won: float | None) -> tuple:
        """FOK sell decided at ``t_dec``: (price, how, time, decision bid). Retried every
        2 s with a floor of bid - 2c; the last 10 s of the market settle it."""
        bid, bdep = sv["bid"], sv["bdep"]
        jd0 = w.last_at(t_dec)
        dec_bid = float(bid[jd0]) if jd0 >= 0 and bid[jd0] == bid[jd0] else None
        while True:
            if t_dec >= w.close - SETTLE_GUARD_MS:
                return (won, "SETTLE", w.close, dec_bid) if won is not None else (None, "UNKNOWN", w.close, dec_bid)
            pre, half = self.latency()
            t_s = t_dec + pre + half
            js = w.first_at(t_s + self.proxy_ms)
            if js < len(w) and w.t[js] < w.close and bid[js] == bid[js] and bid[js] >= floor - 1e-9 \
                    and (bdep[js] or 0.0) >= self.size:
                return float(bid[js]), "SOLD", t_s, dec_bid
            t_dec = t_s + half + 2000.0
            jd = w.last_at(t_dec)
            floor = (float(bid[jd]) if jd >= 0 and bid[jd] == bid[jd] else floor + 0.02) - 0.02


def trade_pnl(w: Window, entry: float, exit_px: float, how: str) -> tuple[float, float]:
    """(net per share after both fees, fees per share)."""
    fees = w.fee(entry) + (0.0 if how == "SETTLE" else w.fee(exit_px))
    return exit_px - entry - fees, fees
