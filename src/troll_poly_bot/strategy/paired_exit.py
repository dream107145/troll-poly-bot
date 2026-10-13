"""PAIRED-EXIT MOMENTUM (2026-10-01): the triangle bot's redesign after the resting pair lost money.

The old rule bought a token on a signal, placed a resting BUY of the other token at 0.95 - fill, and held
the first token to settlement whether or not that order ever filled. The pair fills when the first leg is
WINNING (the other side falls through the price) and stays empty when it is losing, so a completed pair
earns ~+0.23 and a naked leg loses ~-3.6 (scripts/paired_exit/*, docs/paired_exit_2026-10-01.md).

Here the first buy stands on its own:

    SIGNAL      |1 s spot z| >= momentum_threshold on the 200 ms samples (the archive's own points), the side
                is the move's direction; cross-exchange agreement optional. signal_source "book" reads the
                direction from the Polymarket order book instead (BookModel), "both" / "either" combine them
    ENTRY       fair P(side) from a transparent logistic model (lock file per asset, fitted on the archive)
                EV = fair - ask - taker fee - (slippage + adverse selection + latency cost) >= min edge
    EXPOSURE    USDC per window, unpaired USDC, directional USDC, open legs, buys per window: checked before
                every order (a new leg is refused when it would add correlated risk)
    EXITS       every leg's OWN plan, armed the moment its fill is known: stop loss, thesis invalidated (fair
                value fell / spot reversed with cross-exchange agreement), max hold (UNPAIRED_TIMEOUT), the
                time-to-close exit, optional take-profit ladder (resting asks or taker sells)
    OPPOSITE    optional: a resting buy of the other token that LOCKS the remaining shares if it fills;
                if it never fills nothing changes -- the leg's own exits still run
    KILL        daily loss, unpaired exposure breach, loss streak, stale data, exchange disagreement, stale
                book, slow execution, abnormal spread, unexpected settlement: new entries stop

Pure logic (no I/O): the triangle engine (strategy/triangle.py) and the live bot call it; the replay
(scripts/paired_exit/pe_lib.py) computes the same features on the same samples (tests/test_paired_exit.py
checks the two agree).
"""
from __future__ import annotations

import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

EXCH = ("binance", "bybit", "coinbase", "deribit", "kraken", "bitstamp")
FEATS = ("lm", "dist", "z05", "z1", "z2", "z5", "accel", "persist", "agree", "disp", "tok1", "tok3", "tobi",
         "tfi", "ofi", "volr", "revs")
TICK = 0.01


def taker_fee(p: float, rate: float = 0.07) -> float:
    return rate * p * (1.0 - p)


def _nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _f(x) -> float:
    return float("nan") if x is None else float(x)


# ----------------------------------------------------------------------------------------- features


def _lag(T: list[float], i: int, h_ms: float, tol_ms: float = 450.0) -> int | None:
    """Index of the last sample at or before T[i] - h (None when there is none close enough): the replay's
    ``lag_index`` at one sample."""
    import bisect
    j = bisect.bisect_right(T, T[i] - h_ms) - 1
    if j < 0 or T[i] - T[j] > h_ms + tol_ms:
        return None
    return j


def _mid(ask, bid) -> float:
    a, b = _f(ask), _f(bid)
    if not math.isnan(a) and not math.isnan(b):
        return (a + b) / 2.0
    if not math.isnan(a):
        return max(a - 0.005, 0.0)
    if not math.isnan(b):
        return min(b + 0.005, 1.0)
    return float("nan")


def point_mid(p: dict) -> float:
    mu, md = _mid(p.get("ua"), p.get("ub")), _mid(p.get("da"), p.get("db"))
    if math.isnan(mu):
        return 1.0 - md
    if math.isnan(md):
        return mu
    return (mu + 1.0 - md) / 2.0


def window_sigma(points: list[dict]) -> float | None:
    """Realised 1 s log-return sigma (bp / sqrt(s)) of a closed window, from every 5th sample."""
    import bisect
    T = [p["t"] for p in points]
    lp = [math.log(p["spot"]) * 1e4 for p in points]
    r = []
    for i in range(0, len(points), 5):
        j = bisect.bisect_right(T, T[i] - 1000.0) - 1
        if j >= 0 and T[i] - T[j] < 1300.0:
            dt = (T[i] - T[j]) / 1000.0
            r.append((lp[i] - lp[j]) / math.sqrt(max(dt, 0.2)))
    if len(r) < 30:
        return None
    m = sum(r) / len(r)
    s = math.sqrt(sum((x - m) ** 2 for x in r) / len(r))
    return s if s > 0 else None


def features_at(points: list[dict], strike: float, sigma: float, close_ts: float, i: int | None = None) -> dict:
    """The model's features at sample ``i`` (default the last) of one window's 200 ms points -- exactly
    pe_lib.compute_features at that index. Signed toward UP."""
    n = len(points)
    i = n - 1 if i is None else i
    T = [float(p["t"]) for p in points]
    lp = [math.log(p["spot"]) * 1e4 if p.get("spot") else float("nan") for p in points]
    nan = float("nan")
    F: dict[str, float] = {}
    r = {}
    for h in (0.2, 0.5, 1.0, 2.0, 5.0):
        j = _lag(T, i, h * 1000.0)
        r[h] = nan if j is None else lp[i] - lp[j]
    for h, name in ((0.5, "z05"), (1.0, "z1"), (2.0, "z2"), (5.0, "z5")):
        F[name] = r[h] / (sigma * math.sqrt(h))
    F["accel"] = (r[0.5] / 0.5 - r[2.0] / 2.0) / sigma
    # persistence over the last 10 sample steps (NaN for the first 10 samples, as the replay)
    s1 = math.copysign(1.0, r[1.0]) if not math.isnan(r[1.0]) and r[1.0] != 0 else (0.0 if r[1.0] == 0 else nan)
    if i < 10:
        F["persist"] = nan
    else:
        same = 0
        for k in range(i - 9, i + 1):
            d = lp[k] - lp[k - 1] if k >= 1 else 0.0
            if d != 0 and not math.isnan(d) and not math.isnan(s1) and math.copysign(1.0, d) == s1:
                same += 1
        F["persist"] = same / 10.0 * s1
    # cross-exchange agreement (mean sign of each exchange's 1 s return) and dispersion (sigma units)
    j1 = _lag(T, i, 1000.0)
    rets = []
    for e in EXCH:
        if j1 is None:
            break
        xi = (points[i].get("srcs") or {}).get(e)
        xj = (points[j1].get("srcs") or {}).get(e)
        if xi and xj:
            rets.append(math.log(xi) * 1e4 - math.log(xj) * 1e4)
    if rets:
        thr = 0.1 * sigma
        sg = [(math.copysign(1.0, x) if abs(x) >= thr else 0.0) for x in rets]
        F["agree"] = sum(sg) / len(rets)
        if len(rets) > 1:
            m = sum(rets) / len(rets)
            F["disp"] = math.sqrt(sum((x - m) ** 2 for x in rets) / len(rets)) / sigma
        else:
            F["disp"] = 0.0
    else:
        F["agree"], F["disp"] = nan, 0.0
    F["n_exch"] = float(len(rets))
    mid = point_mid(points[i])
    F["mid"] = mid
    pm = min(max(mid, 0.02), 0.98) if not math.isnan(mid) else nan
    F["lm"] = math.log(pm / (1.0 - pm)) if not math.isnan(pm) else nan
    left = max((close_ts - T[i]) / 1000.0, 0.0)
    if strike and strike > 0:
        F["dist"] = max(-6.0, min(6.0, (lp[i] - math.log(strike) * 1e4) / (sigma * math.sqrt(max(left, 1.0)))))
    else:
        F["dist"] = 0.0
    for h, name in ((1000.0, "tok1"), (3000.0, "tok3")):
        j = _lag(T, i, h)
        F[name] = nan if j is None else (mid - point_mid(points[j])) * 100.0
    p = points[i]

    def obi(b, a):
        b, a = _f(b), _f(a)
        if math.isnan(b) or math.isnan(a) or b + a == 0:
            return 0.0
        return (b - a) / (b + a)
    F["tobi"] = (obi(p.get("ubd"), p.get("uad")) - obi(p.get("dbd"), p.get("dad"))) / 2.0
    of = p.get("of") or {}
    F["tfi"] = 0.0 if _nan(of.get("tfi15")) else float(of["tfi15"])
    F["ofi"] = 0.0 if _nan(of.get("ofi")) else float(of["ofi"])
    j30 = _lag(T, i, 30000.0, tol_ms=2000.0)
    if j30 is None:
        F["volr"] = F["revs"] = nan
    else:
        sq = 0.0
        for k in range(j30 + 1, i + 1):
            d = lp[k] - lp[k - 1]
            sq += 0.0 if math.isnan(d) else d * d
        F["volr"] = math.sqrt(max(sq, 0.0) / 30.0) / sigma
        flips = 0
        for k in range(j30 + 1, i + 1):
            if k - 5 < 0:
                continue
            a = _sign_r1(T, lp, k)
            b = _sign_r1(T, lp, k - 5)
            if a * b < 0:
                flips += 1
        F["revs"] = flips / 5.0
    F["left"] = left
    return F


# ----------------------------------------------------------------------------------------- book direction signal
# The Polymarket order book's own read of the token's next move (scripts/book_signal/, 2026-10-02): features of
# both tokens' touches and near-touch depth on the same 200 ms points, a ridge model of the UP-equivalent mid's
# move over the next second (cents, + = UP up). Exactly scripts/book_signal/bs_lib.book_features at one sample.

BOOK_LAGS = (1, 2, 5)
BOOK_FEATS = tuple(f"{n}{k}" for k in BOOK_LAGS for n in ("vel", "acons", "bcons", "newliq")) + ("imb", "micro", "spr")


def _nz(x) -> float:
    return 0.0 if _nan(x) else float(x)


def _slog(x: float) -> float:
    return 0.0 if x == 0 else math.copysign(math.log1p(abs(x)), x)


def _cons(p_now, p_prev, d_now, d_prev, ask: bool) -> float:
    """Depth taken (or pulled) at one touch between two samples: the touch moved away -> all of the old touch
    depth; same price -> the depth decrease; moved toward -> nothing."""
    if _nan(p_now) or _nan(p_prev):
        return 0.0
    if (p_now > p_prev + 1e-9) if ask else (p_now < p_prev - 1e-9):
        return _nz(d_prev)
    if abs(p_now - p_prev) < 1e-9:
        return max(0.0, _nz(d_prev) - _nz(d_now))
    return 0.0


def book_features_at(points: list[dict], i: int | None = None) -> dict:
    """The book model's features at sample ``i`` (default the last), signed toward UP."""
    n = len(points)
    i = n - 1 if i is None else i
    p = points[i]
    mid_i = point_mid(p)
    F: dict[str, float] = {}
    for k in BOOK_LAGS:
        j = i - k
        if j < 0 or float(p["t"]) - float(points[j]["t"]) > k * 200.0 + 450.0:
            for name in ("vel", "acons", "bcons", "newliq"):
                F[f"{name}{k}"] = float("nan")
            continue
        q = points[j]
        v = (mid_i - point_mid(q)) * 100.0
        F[f"vel{k}"] = 0.0 if math.isnan(v) else v
        F[f"acons{k}"] = _slog(_cons(p.get("ua"), q.get("ua"), p.get("uad"), q.get("uad"), True)
                               - _cons(p.get("da"), q.get("da"), p.get("dad"), q.get("dad"), True))
        F[f"bcons{k}"] = _slog(_cons(p.get("db"), q.get("db"), p.get("dbd"), q.get("dbd"), False)
                               - _cons(p.get("ub"), q.get("ub"), p.get("ubd"), q.get("ubd"), False))

        def dd(key):
            a, b = _f(p.get(key)), _f(q.get(key))
            return 0.0 if math.isnan(a) or math.isnan(b) else a - b
        F[f"newliq{k}"] = _slog(max(0.0, dd("ubd")) - max(0.0, dd("uad")) - (max(0.0, dd("dbd")) - max(0.0, dd("dad"))))
    ub_, ua_, db_, da_ = (_nz(p.get(x)) for x in ("ubd", "uad", "dbd", "dad"))
    F["imb"] = ((ub_ - ua_) - (db_ - da_)) / max(1.0, ub_ + ua_ + db_ + da_)

    def micro(bid, ask, bdep, adep):
        b, a = _f(bid), _f(ask)
        if bdep + adep <= 0 or math.isnan(b) or math.isnan(a):
            return float("nan")
        return (b * adep + a * bdep) / (bdep + adep) - (a + b) / 2.0
    m = micro(p.get("ub"), p.get("ua"), ub_, ua_) - micro(p.get("db"), p.get("da"), db_, da_)
    F["micro"] = 0.0 if math.isnan(m) else m * 100.0
    q5 = points[max(i - 5, 0)]

    def spread(pt, a, b):
        return _f(pt.get(a)) - _f(pt.get(b))
    s = ((spread(p, "da", "db") - spread(q5, "da", "db")) - (spread(p, "ua", "ub") - spread(q5, "ua", "ub"))) * 100.0
    F["spr"] = 0.0 if math.isnan(s) else s
    return F


class BookModel:
    """Predicted UP-mid move over the next second (cents) = w . standardised book features; NaN takes the mean."""

    def __init__(self, doc: dict) -> None:
        self.mu = [float(x) for x in doc["mu"]]
        self.sd = [float(x) for x in doc["sd"]]
        self.w = [float(x) for x in doc["w"]]
        self.feats = tuple(doc.get("features") or BOOK_FEATS)
        self.meta = {k: doc.get(k) for k in ("asset", "train_windows", "fitted_to", "target")}

    @classmethod
    def load(cls, path: str) -> "BookModel":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def score(self, F: dict) -> float:
        z = self.w[0]
        for k, mu, sd, w in zip(self.feats, self.mu, self.sd, self.w[1:]):
            x = F.get(k)
            if x is None or (isinstance(x, float) and math.isnan(x)):
                x = mu
            z += w * (x - mu) / sd
        return z


SIGNAL_SOURCES = ("crypto", "book", "both", "either")


def pick_signal(source: str, z1: float | None, book: float | None, prm: "PxParams",
                book_recent: list[tuple[float, float]] | None = None, t: float = 0.0) -> tuple[int, str] | None:
    """Which way (and from what) a sample signals, or None -- the replay's ``sim.candidates`` at one sample.
    crypto  |1 s spot z| >= momentum_threshold
    book    |book model| >= book_threshold (cents), the side is its sign
    both    a crypto signal whose side the book showed (>= book_confirm) in the last book_confirm_ms
    either  crypto if it fires, else the book"""
    zc = None if z1 is None or math.isnan(z1) else z1
    bk = 0.0 if book is None or math.isnan(book) else book
    crypto = zc is not None and abs(zc) >= prm.momentum_threshold
    if source == "crypto":
        return (1 if zc > 0 else -1, "crypto") if crypto else None
    if source == "book":
        return (1 if bk > 0 else -1, "book") if abs(bk) >= prm.book_threshold else None
    if source == "both":
        if not crypto:
            return None
        d = 1 if zc > 0 else -1
        recent = [b * d for tb, b in (book_recent or []) if t - tb <= prm.book_confirm_ms]
        return (d, "both") if recent and max(recent) >= prm.book_confirm else None
    if source == "either":
        if crypto:
            return 1 if zc > 0 else -1, "crypto"
        return (1 if bk > 0 else -1, "book") if abs(bk) >= prm.book_threshold else None
    raise ValueError(f"signal source {source!r}: one of {SIGNAL_SOURCES}")


def _sign_r1(T, lp, k) -> float:
    j = _lag(T, k, 1000.0)
    if j is None:
        return float("nan")
    r = lp[k] - lp[j]
    if math.isnan(r):
        return float("nan")
    return 0.0 if r == 0 else math.copysign(1.0, r)


class FairModel:
    """P(UP wins) = logistic(w . standardised features); NaN features take the training mean."""

    def __init__(self, doc: dict) -> None:
        self.mu = [float(x) for x in doc["mu"]]
        self.sd = [float(x) for x in doc["sd"]]
        self.w = [float(x) for x in doc["w"]]
        self.feats = tuple(doc.get("features") or FEATS)
        self.meta = {k: doc.get(k) for k in ("asset", "train_windows", "train_to", "fitted_to")}

    @classmethod
    def load(cls, path: str) -> "FairModel":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def p_up(self, F: dict) -> float:
        z = self.w[0]
        for k, mu, sd, w in zip(self.feats, self.mu, self.sd, self.w[1:]):
            x = F.get(k)
            if x is None or (isinstance(x, float) and math.isnan(x)):
                x = mu
            z += w * (x - mu) / sd
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))


# ----------------------------------------------------------------------------------------- parameters


@dataclass(slots=True)
class PxParams:
    """The paired-exit knobs (TriangleConfig ``px_*`` fields, env TPB_TRIANGLE_PX_*)."""
    momentum_threshold: float = 2.0       # |z1| that makes a signal
    signal_source: str = "crypto"         # "crypto" | "book" | "both" | "either" (pick_signal)
    book_threshold: float = 1.5           # |book model| (predicted next-1 s UP mid move, cents) that signals
    book_confirm: float = 0.0             # "both": the book toward the side >= this ...
    book_confirm_ms: float = 1000.0       # ... somewhere in this many ms before the crypto signal
    min_agree: float = -1.0               # cross-exchange agreement toward the side (-1 = off)
    entry_min_edge: float = 0.02          # EV after every cost must be >= this (safety margin)
    entry_min_confidence: float = 0.0     # fair P(side) >= this
    cost_slippage: float = 0.003          # expected fill - decision ask
    cost_adverse: float = 0.02            # adverse selection of filled orders (TRAIN)
    cost_latency: float = 0.0             # extra expected move against us while the order travels
    lo: float = 0.15                      # ask band
    hi: float = 0.85
    limit_offset: float = 0.02
    shares: float = 5.0
    min_left_s: float = 60.0
    max_left_s: float = 290.0
    late_left_s: float = 180.0            # under this many seconds left the edge must be ...
    late_edge_add: float = 0.0            # ... this much larger
    max_window_usdc: float = 10.0
    max_position_usdc: float = 5.0
    max_unpaired_usdc: float = 10.0
    max_directional_usdc: float = 10.0
    max_open_positions: int = 1
    max_buys_per_window: int = 3
    # exits (0 / "" = off)
    stop_loss: float = 0.0
    stop_cross: float = 0.02
    max_hold_s: float = 0.0
    thesis_mode: str = ""                 # "fair" | "mom" | "both"
    thesis_drop: float = 0.10
    reversal_threshold: float = 2.0
    time_exit_s: float = 0.0
    settle_keep: float = 0.95
    ladder_levels: tuple[float, ...] = ()
    ladder_fracs: tuple[float, ...] = ()
    ladder_mode: str = "maker"
    ladder_fair_offset: float = -1.0      # < 0: static levels; else level >= fair + this
    ladder_reprice_s: float = 1.0
    pair_enabled: bool = False
    pair_target: float = 0.95             # resting buy of the other side at < target - fill
    # kill conditions
    max_daily_loss: float = 20.0
    max_consecutive_losses: int = 6
    loss_pause_s: float = 900.0
    max_data_age_ms: float = 1500.0
    max_signal_age_ms: float = 600.0
    max_execution_latency_ms: float = 1500.0
    max_spread: float = 0.05
    max_disagreement: float = 3.0         # cross-exchange dispersion of 1 s returns, sigma units
    max_settle_mismatches: int = 3

    @property
    def entry_cost(self) -> float:
        return self.cost_slippage + self.cost_adverse + self.cost_latency


# ----------------------------------------------------------------------------------------- legs


@dataclass(slots=True)
class Leg:
    leg_id: str
    slug: str
    asset: str
    side: str
    token_id: str
    shares: float
    fill_px: float
    t_fill: float                    # when the fill became known (the exit plan starts here)
    close_ts: float
    fair_entry: float = float("nan")
    edge_entry: float = float("nan")
    remaining: float = 0.0
    state: str = "OPEN"              # OPEN | EXITING | PAIRED | CLOSED
    exit_kind: str = ""
    exit_order: str = ""             # the working urgent exit order id
    exit_tries: int = 0
    next_try_ms: float = 0.0
    realised: float = 0.0            # proceeds - exit fees of shares already sold
    ladder: list = field(default_factory=list)   # [{"price", "shares", "order_id", "filled"}]
    pair_order: str = ""
    pair_price: float = 0.0

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict) -> "Leg":
        leg = cls(**{k: d[k] for k in ("leg_id", "slug", "asset", "side", "token_id", "shares", "fill_px",
                                        "t_fill", "close_ts")})
        for k in cls.__slots__:
            if k in d and k not in ("leg_id",):
                setattr(leg, k, d[k])
        return leg


@dataclass(slots=True)
class ExitAction:
    """What the live bot should do for one leg now."""
    kind: str                        # STOP_LOSS | THESIS_INVALIDATED | UNPAIRED_TIMEOUT | TIME_EXIT |
    #                                  TAKE_PROFIT | LADDER_PLACE | LADDER_CANCEL | PAIR_CANCEL
    leg_id: str
    token_id: str
    side: str
    shares: float = 0.0
    price: float = 0.0
    tif: str = "FOK"
    order_id: str = ""               # for cancels
    detail: str = ""


def up_tick(x: float) -> float:
    return round(math.ceil(x / TICK - 1e-6) * TICK, 2)


def ladder_allocation(shares: float, fracs: tuple[float, ...], min_size: float = 5.0) -> list[float]:
    """Shares per ladder level: whole multiples of nothing smaller than the venue minimum; levels that would
    be under ``min_size`` are merged into the previous one (a 5-share leg gets ONE level)."""
    if not fracs:
        return []
    out: list[float] = []
    for j, f in enumerate(fracs):
        sh = shares - sum(out) if j == len(fracs) - 1 else math.floor(shares * f + 1e-9)
        out.append(float(sh))
    merged: list[float] = []
    for sh in out:
        if merged and (sh < min_size or merged[-1] < min_size):
            merged[-1] += sh
        else:
            merged.append(sh)
    if merged and merged[0] < min_size and len(merged) > 1:
        merged[1] += merged.pop(0)
    return [s for s in merged if s > 0]


def plan_exit(leg: Leg, prm: PxParams, now: float, bid: float | None, fair_side: float | None,
              z1: float | None, agree: float | None, left_s: float) -> tuple[str, str] | None:
    """The full-exit trigger for one leg at ``now`` (first that holds, in priority order), or None:
    (kind, human detail). The replay's ``leg_life`` uses the same conditions on the same samples."""
    if leg.state not in ("OPEN",):
        return None
    d = 1 if leg.side == "UP" else -1
    if prm.stop_loss > 0 and bid is not None and bid <= leg.fill_px - prm.stop_loss + 1e-9:
        return "STOP_LOSS", f"bid {bid:.2f} <= fill {leg.fill_px:.2f} - {prm.stop_loss:.2f}"
    if prm.thesis_mode in ("fair", "both") and fair_side is not None and fair_side < leg.fill_px - prm.thesis_drop:
        return "THESIS_INVALIDATED", f"fair {fair_side:.3f} < fill {leg.fill_px:.2f} - {prm.thesis_drop:.2f}"
    if (prm.thesis_mode in ("mom", "both") and z1 is not None and not math.isnan(z1) and z1 * d <= -prm.reversal_threshold
            and agree is not None and not math.isnan(agree) and agree * d <= -0.5):
        return "THESIS_INVALIDATED", f"spot reversed: z1 {z1:+.2f}, exchanges agree {agree:+.2f}"
    if prm.max_hold_s > 0 and now - leg.t_fill >= prm.max_hold_s * 1000.0 and left_s > 1.0:
        return "UNPAIRED_TIMEOUT", f"held {(now - leg.t_fill) / 1000:.0f} s >= {prm.max_hold_s:.0f} s"
    if prm.time_exit_s > 0 and left_s <= prm.time_exit_s and bid is not None and bid < prm.settle_keep:
        return "TIME_EXIT", f"{left_s:.0f} s left, bid {bid:.2f} < keep {prm.settle_keep:.2f}"
    return None


def ladder_prices(leg: Leg, prm: PxParams, fair_side: float | None) -> list[float]:
    out = []
    for off in prm.ladder_levels:
        lvl = up_tick(leg.fill_px + off)
        if prm.ladder_fair_offset >= 0 and fair_side is not None and not math.isnan(fair_side):
            lvl = max(lvl, up_tick(fair_side + prm.ladder_fair_offset))
        out.append(min(lvl, 0.99))
    return out


# ----------------------------------------------------------------------------------------- exposure


@dataclass(slots=True)
class WindowBook:
    spent: float = 0.0
    buys: int = 0


class ExposureBook:
    """Window / unpaired / directional exposure of the legs, checked before every order."""

    def __init__(self) -> None:
        self.windows: dict[str, WindowBook] = {}

    def window(self, slug: str) -> WindowBook:
        return self.windows.setdefault(slug, WindowBook())

    @staticmethod
    def open_legs(legs: dict[str, Leg], slug: str | None = None) -> list[Leg]:
        return [lg for lg in legs.values() if lg.state in ("OPEN", "EXITING") and (slug is None or lg.slug == slug)]

    def check(self, legs: dict[str, Leg], prm: PxParams, slug: str, side: str, stake: float) -> tuple[bool, str]:
        wb = self.window(slug)
        if wb.buys >= prm.max_buys_per_window:
            return False, f"{wb.buys} buys in this window (max {prm.max_buys_per_window})"
        here = self.open_legs(legs, slug)
        if len(here) >= prm.max_open_positions:
            return False, f"{len(here)} open leg(s) in this window (max {prm.max_open_positions})"
        if stake > prm.max_position_usdc + 1e-9:
            return False, f"position {stake:.2f} > max {prm.max_position_usdc:.2f} USDC"
        if wb.spent + stake > prm.max_window_usdc + 1e-9:
            return False, f"window {wb.spent:.2f} + {stake:.2f} > max {prm.max_window_usdc:.2f} USDC"
        allopen = self.open_legs(legs)
        unpaired = sum(lg.remaining * lg.fill_px for lg in allopen if lg.state != "PAIRED")
        if unpaired + stake > prm.max_unpaired_usdc + 1e-9:
            return False, f"unpaired {unpaired:.2f} + {stake:.2f} > max {prm.max_unpaired_usdc:.2f} USDC"
        direc = sum((1 if lg.side == "UP" else -1) * lg.remaining * lg.fill_px for lg in allopen if lg.state != "PAIRED")
        new = direc + (stake if side == "UP" else -stake)
        if abs(new) > prm.max_directional_usdc + 1e-9 and abs(new) > abs(direc):
            return False, f"directional {direc:+.2f} -> {new:+.2f} > max {prm.max_directional_usdc:.2f} USDC"
        return True, ""

    def on_buy(self, slug: str, usdc: float) -> None:
        wb = self.window(slug)
        wb.spent += usdc
        wb.buys += 1

    def unpaired_usdc(self, legs: dict[str, Leg]) -> float:
        return sum(lg.remaining * lg.fill_px for lg in self.open_legs(legs) if lg.state != "PAIRED")

    def forget(self, before_ms: float, close_of) -> None:
        for slug in [s for s in self.windows if close_of(s) and close_of(s) < before_ms]:
            self.windows.pop(slug, None)


# ----------------------------------------------------------------------------------------- kill monitor


class KillMonitor:
    """Conditions that stop NEW entries (exits keep running). ``check`` -> (ok, code, detail)."""

    def __init__(self) -> None:
        self.realised_day: dict[int, float] = {}
        self.streak = 0
        self.paused_until = 0.0
        self.settle_mismatches = 0
        self.halted: str = ""            # a hard halt (unpaired breach, settlement anomalies): needs a restart
        self.trips: dict[str, int] = {}

    def trip(self, code: str) -> None:
        self.trips[code] = self.trips.get(code, 0) + 1

    def on_result(self, pnl: float, now_ms: float, prm: PxParams) -> None:
        day = int(now_ms // 86_400_000)
        self.realised_day[day] = self.realised_day.get(day, 0.0) + pnl
        self.streak = self.streak + 1 if pnl < 0 else 0
        if prm.max_consecutive_losses > 0 and self.streak >= prm.max_consecutive_losses:
            self.paused_until = now_ms + prm.loss_pause_s * 1000.0
            log.warning("[PX] KILL loss streak %d >= %d: new entries paused %.0f s", self.streak,
                        prm.max_consecutive_losses, prm.loss_pause_s)
            self.streak = 0

    def check(self, prm: PxParams, now_ms: float, *, unpaired: float, spot_age_ms: float | None,
              point_age_ms: float | None, book_age_ms: float | None, latency_ms: float | None,
              spread: float | None, disp: float | None) -> tuple[bool, str, str]:
        if self.halted:
            return False, "KILL_HALTED", self.halted
        day = int(now_ms // 86_400_000)
        if self.realised_day.get(day, 0.0) <= -prm.max_daily_loss:
            return False, "KILL_DAILY_LOSS", f"today {self.realised_day[day]:+.2f} <= -{prm.max_daily_loss:.2f}"
        if unpaired > prm.max_unpaired_usdc + 1e-6:
            self.halted = f"unpaired exposure {unpaired:.2f} > {prm.max_unpaired_usdc:.2f} USDC"
            return False, "KILL_UNPAIRED", self.halted
        if now_ms < self.paused_until:
            return False, "KILL_LOSS_STREAK", f"paused {(self.paused_until - now_ms) / 1000:.0f} s more"
        if spot_age_ms is None or spot_age_ms > prm.max_data_age_ms:
            return False, "STALE_DATA", f"spot age {spot_age_ms} ms > {prm.max_data_age_ms:.0f}"
        if point_age_ms is None or point_age_ms > prm.max_signal_age_ms:
            return False, "STALE_DATA", f"signal sample age {point_age_ms} ms > {prm.max_signal_age_ms:.0f}"
        if book_age_ms is None or book_age_ms > prm.max_data_age_ms:
            return False, "STALE_BOOK", f"book age {book_age_ms} ms > {prm.max_data_age_ms:.0f}"
        if latency_ms is not None and latency_ms > prm.max_execution_latency_ms:
            return False, "LATENCY_REJECT", f"order round trip {latency_ms:.0f} ms > {prm.max_execution_latency_ms:.0f}"
        if spread is not None and spread > prm.max_spread + 1e-9:
            return False, "ABNORMAL_SPREAD", f"spread {spread:.2f} > {prm.max_spread:.2f}"
        if disp is not None and not math.isnan(disp) and disp > prm.max_disagreement:
            return False, "EXCHANGE_DISAGREEMENT", f"1 s returns disagree by {disp:.1f} sigma > {prm.max_disagreement:.1f}"
        return True, "", ""

    def on_settlement(self, venue_up: bool | None, proxy_up: bool | None, prm: PxParams, slug: str) -> None:
        """A venue outcome that contradicts the spot clearly (or is missing) counts; enough of them halt."""
        if venue_up is None or (proxy_up is not None and venue_up != proxy_up):
            self.settle_mismatches += 1
            log.warning("[PX] unexpected settlement %s: venue %s, spot proxy %s (%d of %d)", slug, venue_up, proxy_up,
                        self.settle_mismatches, prm.max_settle_mismatches)
            if self.settle_mismatches >= prm.max_settle_mismatches:
                self.halted = f"{self.settle_mismatches} unexpected settlements"


class PointBuffer:
    """One window's 200 ms points (the archive's samples), as the live bot records them."""

    def __init__(self, open_ts: float, close_ts: float) -> None:
        self.open_ts, self.close_ts = open_ts, close_ts
        self.points: list[dict] = []

    def add(self, p: dict) -> bool:
        if p.get("spot") is None or p.get("t") is None:
            return False
        if not (self.open_ts <= p["t"] <= self.close_ts):
            return False
        if self.points and p["t"] <= self.points[-1]["t"]:
            return False
        self.points.append(p)
        return True


class Signals:
    """De-duplication of signals: one per direction per second (the replay's ``candidates``)."""

    def __init__(self) -> None:
        self.last: dict[tuple[str, int], float] = {}

    def take(self, slug: str, d: int, t: float) -> bool:
        k = (slug, d)
        if t - self.last.get(k, -1e18) >= 1000.0:
            self.last[k] = t
            return True
        return False


def deque_last(dq: deque, n: int) -> list:
    return list(dq)[-n:]
