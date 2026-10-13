"""NEXT_MARKET_TWAP (research / paper only): predict the next 5-minute market
a few seconds before it opens, buy the favoured token at the open, sell into
the bid a few seconds later.

This module is the rule and nothing else: causal features, the prediction,
the execution-aware edge, the entry and exit decisions and the log records.
It is NOT wired into ``live.py`` or any real-money path. The only callers are
the research replay (``scripts/research_next_market_twap.py``) and the tests.
``TPB_NEXT_MARKET_TWAP_ENABLED`` defaults to false, and ``REAL_MONEY`` is a
constant False: enabling real money is a separate, deliberate change.

What settles the market
-----------------------
UP iff TWAP60(close) >= TWAP60(open), both trailing 60 s averages of the
Chainlink stream (``pricing/twap.py``; re-checked on 4,432 venue outcomes by
the research ``verify`` step: 98.3 % agreement on our composite spot, against
93.0 % for spot-at-open and 88.0 % for the market's first-minute average).

So the strike K is the average over [open - 60 s, open]. At a prediction time
``pred = open + offset`` (offset <= 0) the part up to ``pred - info_lag`` has
already been seen and only the last ``u_k = -offset + info_lag`` seconds are
unknown. With X a driftless Brownian log price (per-second vol sigma) and x the
latest log price:

    E[ln K]          = (integral of seen log price + u_k * x) / 60
    E[ln TWAP close] = x
    displacement D   = ln TWAP(close) - ln K
    E[D]             = x - E[ln K]
    Var[D]           = sigma^2 * ((u - 40) + u_k^3 / 10800 - u_k^2 / 120)

where u = seconds from ``pred - info_lag`` to the close. The last two terms are
the strike's own uncertainty and its covariance with the settling average
(both depend on the path between now and the open). P(UP) is the project's
Student-t(4) digital on E[D] / sd (``pricing/digital.StudentTPricer``).

No look-ahead
-------------
Every feature is computed from the prefix of the series with timestamps at or
before ``pred_ms`` (``_cut``). The archive's timestamps are the LOCAL receive
times, so "stamped at or before" means "had arrived". The tests rewrite
everything after ``pred_ms`` and require identical features.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import numpy as np

from ..pricing.digital import StudentTPricer
from ..signals.costs import FeeSchedule

ENABLE_ENV = "TPB_NEXT_MARKET_TWAP_ENABLED"
#: This strategy has no real-money path. Changing that is a code change, not a setting.
REAL_MONEY = False

TWAP_WINDOW_S = 60.0
INFO_LAG_S = 0.18                  # the measured spot information lag (backtest/spot_shock_replay)
GRID_MS = 250.0
RET_LAGS_MS = (250, 500, 1000, 2000, 3000, 5000, 10000, 30000, 60000)
RV_SPANS_S = (1, 5, 10, 30, 60, 300)
EXCHANGES = ("bn", "byb", "cb")
PRED_OFFSETS_S = (-10.0, -5.0, -3.0, -2.0, -1.0, 0.0)
PRICER = StudentTPricer(nu=4.0)
MIN_SHARES = 5.0                   # the venue's minimum order size on these markets

# rejection reasons
DISABLED = "DISABLED"
NO_PREDICTION = "NO_PREDICTION"
BEFORE_MARKET_START = "BEFORE_MARKET_START"
ENTRY_WINDOW_CLOSED = "ENTRY_WINDOW_CLOSED"
LOW_PROBABILITY = "LOW_PROBABILITY"
NO_ASK = "NO_ASK"
NO_BID = "NO_BID"
ASK_OUT_OF_RANGE = "ASK_OUT_OF_RANGE"
SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
THIN_BOOK = "THIN_BOOK"
BELOW_MIN_SIZE = "BELOW_MIN_SIZE"
QUOTE_MOVED = "QUOTE_MOVED"
EDGE_TOO_SMALL = "EDGE_TOO_SMALL"
ENTER = "ENTER"

# exit reasons
EXIT_TIME = "TIME"
EXIT_TAKE_PROFIT = "TAKE_PROFIT"
EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_MOMENTUM = "MOMENTUM_REVERSAL"
EXIT_EDGE_GONE = "EDGE_GONE"
EXIT_MAX_HOLD = "MAX_HOLD"


# ─────────────────────────────── configuration ───────────────────────────────


@dataclass(slots=True)
class NextMarketConfig:
    """Every threshold the rule reads. ``from_env`` fills it from ``TPB_NEXT_MARKET_TWAP_*``."""

    enabled: bool = False
    predict_offset_s: float = -3.0
    #: entries are evaluated from the open to this many seconds after it
    entry_window_s: float = 0.0
    min_probability: float = 0.5
    min_net_edge: float = 0.02
    max_spread: float = 0.03
    max_entry_price: float = 0.60
    min_entry_price: float = 0.05
    #: skip if the ask moved against us by more than this between prediction and entry
    max_quote_move: float = 1.0
    order_usdc: float = 5.0
    #: the order must be at most this fraction of the ask depth near the touch
    max_depth_fraction: float = 0.5
    hold_s: float = 3.0
    take_profit: float = 0.0          # 0 = off
    stop_loss: float = 0.0            # 0 = off
    max_hold_s: float = 30.0
    #: Platt recalibration of the analytic probability: logit(p) -> a + b * logit(p)
    calib_a: float = 0.0
    calib_b: float = 1.0
    #: fraction of the model-vs-market gap the token closes over the hold (research estimate)
    kappa: float = 0.0
    #: expected half spread of the book at the exit (it re-forms to ~2c within 2-3 s of the open)
    exit_half_spread: float = 0.01
    #: execution-model inputs (research estimates; see the report)
    expected_adverse: float = 0.005
    expected_fill_prob: float = 1.0

    ENV_PREFIX = "TPB_NEXT_MARKET_TWAP_"

    @classmethod
    def from_env(cls, env: dict | None = None) -> "NextMarketConfig":
        env = os.environ if env is None else env
        cfg = cls()
        cfg.enabled = (env.get(ENABLE_ENV) or "").strip().lower() in ("1", "true", "yes")
        for name in cfg.__dataclass_fields__:                      # type: ignore[attr-defined]
            if name == "enabled":
                continue
            raw = (env.get(cls.ENV_PREFIX + name.upper()) or "").strip()
            if raw:
                setattr(cfg, name, float(raw))
        return cfg


# ─────────────────────────────── features ───────────────────────────────


@dataclass(slots=True)
class SpotView:
    """One asset's spot history as this machine received it.

    ``t`` local receive time (ms, increasing); ``lx`` log composite price;
    ``ex`` each exchange's log price (NaN when absent); ``flow`` the spot
    order-flow columns (tfi, obi, ofi, rate). Arrays may extend past the
    prediction time: ``boundary_features`` only reads the prefix."""

    t: np.ndarray
    lx: np.ndarray
    ex: dict = field(default_factory=dict)
    flow: dict = field(default_factory=dict)


def _cut(v: SpotView, pred_ms: float) -> int:
    """Number of points stamped at or before ``pred_ms``: the only ones a decision may read."""
    return int(np.searchsorted(v.t, pred_ms, side="right"))


def _grid(t: np.ndarray, x: np.ndarray, end_ms: float, n: int, stale_ms: float = 5000.0) -> np.ndarray:
    """x on a 250 ms grid ending at ``end_ms`` (oldest first): the last value at or before each
    grid time, NaN where there is none within ``stale_ms``."""
    g = end_ms - GRID_MS * np.arange(n - 1, -1, -1)
    j = np.searchsorted(t, g, side="right") - 1
    ok = (j >= 0) & (g - t[np.maximum(j, 0)] <= stale_ms)
    return np.where(ok, x[np.maximum(j, 0)], np.nan)


def _interp(t: np.ndarray, x: np.ndarray, q: float) -> float:
    j = min(max(int(np.searchsorted(t, q, side="right")) - 1, 0), len(t) - 2)
    span = t[j + 1] - t[j]
    return float(x[j] + (x[j + 1] - x[j]) * ((q - t[j]) / span if span > 0 else 0.0))


def _integral(t: np.ndarray, x: np.ndarray, a_ms: float, b_ms: float) -> tuple[float, float]:
    """Trapezoid integral (value*s) of the linear interpolant over [a, b] and the seconds covered
    (only [first, last] of the series counts)."""
    if len(t) < 2:
        return 0.0, 0.0
    a, b = max(a_ms, float(t[0])), min(b_ms, float(t[-1]))
    if b <= a:
        return 0.0, 0.0
    i0 = int(np.searchsorted(t, a, side="right"))
    i1 = int(np.searchsorted(t, b, side="left"))
    tt = np.concatenate([[a], t[i0:i1], [b]])
    xx = np.concatenate([[_interp(t, x, a)], x[i0:i1], [_interp(t, x, b)]])
    return float(np.sum(0.5 * (xx[1:] + xx[:-1]) * np.diff(tt)) / 1000.0), (b - a) / 1000.0


def displacement_variance(u_s: float, u_k_s: float, sigma: float, window_s: float = TWAP_WINDOW_S) -> float:
    """Var[ln TWAP(close) - ln K] seen from ``u_s`` before the close with ``u_k_s`` of the strike's
    averaging window still unobserved (module docstring). Requires u_s - u_k_s >= window_s."""
    w = window_s
    return sigma * sigma * max((u_s - 2.0 * w / 3.0) + u_k_s ** 3 / (3.0 * w * w) - u_k_s ** 2 / (2.0 * w), 1e-12)


def boundary_features(v: SpotView, open_ms: float, pred_ms: float, sigma: float,
                      prev_strike: float | None = None, market_s: float = 300.0,
                      info_lag_s: float = INFO_LAG_S) -> dict | None:
    """Every feature of the market opening at ``open_ms``, as known at ``pred_ms``. None if the series
    is too thin to price (under 60 s of history, a stale latest price, a hole in the strike window)."""
    n = _cut(v, pred_ms)
    if n < 20 or sigma is None or not (sigma > 0):
        return None
    t, x = v.t[:n], v.lx[:n]
    if pred_ms - t[-1] > 5000.0 or pred_ms - t[0] < TWAP_WINDOW_S * 1000.0:
        return None
    f: dict[str, float] = {}
    xnow = float(x[-1])
    f["spot_age_s"] = (pred_ms - float(t[-1])) / 1000.0
    ng = int(300_000 / GRID_MS) + 1
    xg = _grid(t, x, pred_ms, ng)
    for lag in RET_LAGS_MS:
        k = int(lag / GRID_MS)
        f[f"r{lag}"] = float(xg[-1] - xg[-1 - k])
    d = np.diff(xg)
    for s in RV_SPANS_S:
        k = int(s * 1000 / GRID_MS)
        seg = d[-k:]
        seg = seg[~np.isnan(seg)]
        f[f"rv{s}"] = (float(math.sqrt(np.mean(seg * seg) / (GRID_MS / 1000.0)))
                       if len(seg) >= max(2, k // 2) else float("nan"))
    # EMAs of log price on the grid (half-lives 5 s and 30 s), seeded at the oldest valid value
    xs = xg[~np.isnan(xg)]
    emas = {}
    for hl in (5.0, 30.0):
        a = 1.0 - 0.5 ** (GRID_MS / 1000.0 / hl)
        e = xs[0]
        hist = np.empty(len(xs))
        for i, val in enumerate(xs):
            e = e + a * (val - e)
            hist[i] = e
        emas[hl] = hist
    f["ema_gap"] = float(emas[5.0][-1] - emas[30.0][-1])
    f["ema_fast_slope"] = float(emas[5.0][-1] - emas[5.0][-5]) if len(xs) > 4 else float("nan")
    f["velocity"] = f["r1000"]
    f["accel"] = float((xg[-1] - xg[-5]) - (xg[-5] - xg[-9]))
    f["accel5"] = float(f["r5000"] - (xg[-21] - xg[-41]))
    run = 0
    for dv in d[::-1]:
        if dv != dv or dv == 0.0:
            break
        if run == 0 or (run > 0) == (dv > 0):
            run += 1 if dv > 0 else -1
        else:
            break
    f["run_ticks"] = float(run)
    f["vol_regime"] = f["rv60"] / f["rv300"] if f["rv300"] and f["rv300"] > 0 else float("nan")
    # spot order flow (last value) and the trade rate against its last minute
    for k, arr in v.flow.items():
        f[k] = float(arr[n - 1])
    if "rate" in v.flow:
        seg = v.flow["rate"][:n][t >= pred_ms - 60_000.0]
        seg = seg[~np.isnan(seg)]
        m = float(np.mean(seg)) if len(seg) else float("nan")
        f["rate_rel"] = float(f["rate"] / m) if m and m > 0 else float("nan")
    # cross-exchange: deviation from the composite, dispersion, staleness, 1 s lead
    devs = []
    for k in EXCHANGES:
        arr = v.ex.get(k)
        if arr is None:
            continue
        a = arr[:n]
        last = float(a[-1])
        f[f"dev_{k}"] = (last - xnow) * 1e4 if last == last else float("nan")
        if last == last:
            devs.append(last)
        chg = np.where(np.diff(a) != 0)[0]
        f[f"stale_{k}"] = float((pred_ms - t[chg[-1] + 1]) / 1000.0) if len(chg) else float("nan")
        ag = _grid(t, a, pred_ms, 5)
        f[f"lead_{k}"] = float(((ag[-1] - ag[0]) - f["r1000"]) * 1e4)
    f["xdisp_bps"] = float((max(devs) - min(devs)) * 1e4) if len(devs) >= 2 else float("nan")
    f["n_sources"] = float(len(devs))
    # the strike: what is already locked in, and its expected value
    start = open_ms - TWAP_WINDOW_S * 1000.0
    seen_until = min(pred_ms - info_lag_s * 1000.0, open_ms)
    if t[0] > start + 1000.0 or np.max(np.diff(t[t >= start - 5000.0])) > 5000.0:
        return None                                              # a hole in the strike's window
    # Integrate the DEVIATION from the latest price: a log price is ~4-11, so integrating the level
    # turns a 0.05 s coverage sliver into tens of bps of strike error. The first point's value is
    # held back to the window start and the latest value forward to ``seen_until`` (TwapState's
    # step-hold at the edges).
    dev = x - xnow
    integ, _ = _integral(t, dev, start, seen_until)
    integ += max(float(t[0]) - start, 0.0) / 1000.0 * float(dev[0])
    u_k = max((open_ms - seen_until) / 1000.0, 0.0)
    lk_hat = xnow + integ / TWAP_WINDOW_S
    close_ms = open_ms + market_s * 1000.0
    u = (close_ms - seen_until) / 1000.0
    var = displacement_variance(u, u_k, sigma)
    disp = xnow - lk_hat
    f["sigma"] = float(sigma)
    f["strike_seen_frac"] = float(1.0 - u_k / TWAP_WINDOW_S)
    f["lk_hat"] = float(lk_hat)
    f["x_now"] = xnow
    f["disp"] = float(disp)
    f["disp_sd"] = float(math.sqrt(var))
    f["z_disp"] = float(disp / math.sqrt(var))
    f["p_twap"] = PRICER.cdf(f["z_disp"])
    # the previous market: its settling TWAP IS our strike, so this is its projected result
    if prev_strike and prev_strike > 0:
        f["prev_disp"] = float(lk_hat - math.log(prev_strike))
        f["prev_z"] = float(f["prev_disp"] / (sigma * math.sqrt(TWAP_WINDOW_S)))
    else:
        f["prev_disp"] = f["prev_z"] = float("nan")
    # normalised returns and EMA gap: what the fitted models read
    for lag in RET_LAGS_MS:
        f[f"z{lag}"] = f[f"r{lag}"] / (sigma * math.sqrt(lag / 1000.0))
    f["z_ema"] = f["ema_gap"] / (sigma * math.sqrt(30.0))
    return f


# ─────────────────────────────── prediction ───────────────────────────────


@dataclass(slots=True)
class Prediction:
    asset: str
    open_ms: float
    pred_ms: float
    reference_price: float          # the latest composite spot at pred_ms
    predicted_start: float          # E[strike] = E[TWAP60 at the open]
    predicted_twap: float           # E[TWAP60 at the close] (driftless)
    predicted_displacement: float   # predicted_twap - predicted_start, in price
    p_up: float
    confidence: float               # |2 p_up - 1|
    disp_sd_bps: float

    @property
    def p_down(self) -> float:
        return 1.0 - self.p_up

    @property
    def side(self) -> str:
        return "UP" if self.p_up >= 0.5 else "DOWN"


def recalibrate(p: float, a: float = 0.0, b: float = 1.0) -> float:
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    z = a + b * math.log(p / (1.0 - p))
    return 1.0 / (1.0 + math.exp(-z))


def predict(asset: str, f: dict, open_ms: float, pred_ms: float, cfg: NextMarketConfig | None = None,
            p_up: float | None = None) -> Prediction:
    """The analytic next-market prediction from ``boundary_features`` (optionally recalibrated, or
    replaced by ``p_up`` from a fitted model)."""
    cfg = cfg or NextMarketConfig()
    p = recalibrate(f["p_twap"], cfg.calib_a, cfg.calib_b) if p_up is None else float(p_up)
    spot = math.exp(f["x_now"])
    k = math.exp(f["lk_hat"])
    return Prediction(asset=asset, open_ms=open_ms, pred_ms=pred_ms, reference_price=spot, predicted_start=k,
                      predicted_twap=spot, predicted_displacement=spot - k, p_up=p,
                      confidence=abs(2.0 * p - 1.0), disp_sd_bps=f["disp_sd"] * 1e4)


# ─────────────────────────────── execution-aware edge ───────────────────────────────


@dataclass(slots=True)
class Quote:
    bid: float | None
    ask: float | None
    bid_depth: float = 0.0          # shares within 2 ticks of the bid
    ask_depth: float = 0.0          # shares within 2 ticks of the ask

    @property
    def spread(self) -> float | None:
        return None if self.bid is None or self.ask is None else self.ask - self.bid

    @property
    def mid(self) -> float | None:
        return None if self.bid is None or self.ask is None else (self.bid + self.ask) / 2.0


#: The archive records the depth within 2 ticks of each touch, not per level. On the 2026-09-23 tick
#: recording the touch held a median 24 % (mean 28 %) of it; the rest is split evenly one and two
#: ticks away.
TOUCH_FRAC = 0.25


def _levels(depth2: float, touch_frac: float) -> tuple[float, float, float]:
    rest = max(depth2, 0.0) * (1.0 - touch_frac) / 2.0
    return max(depth2, 0.0) * touch_frac, rest, rest


def fillable(best: float | None, depth2: float | None, limit: float, buy: bool = True,
             touch_frac: float = TOUCH_FRAC) -> float:
    """Shares available at or better than ``limit`` (a buy: asks <= limit; a sell: bids >= limit)."""
    if best is None or depth2 is None or best != best or depth2 != depth2:
        return 0.0
    q = 0.0
    for k, sz in enumerate(_levels(depth2, touch_frac)):
        px = best + 0.01 * k if buy else best - 0.01 * k
        if (px <= limit + 1e-9) if buy else (px >= limit - 1e-9):
            q += sz
    return q


def vwap(best: float | None, depth2: float | None, shares: float, buy: bool = True,
         touch_frac: float = TOUCH_FRAC) -> float | None:
    """Average price of ``shares`` taken from a book with ``depth2`` shares within 2 ticks of ``best``
    (asks for a buy, bids for a sell). None when the order is larger than that depth."""
    if best is None or depth2 is None or shares > depth2 + 1e-9 or shares <= 0:
        return None
    left, cost = shares, 0.0
    for k, sz in enumerate(_levels(depth2, touch_frac)):
        take = min(left, sz)
        cost += take * (best + 0.01 * k if buy else best - 0.01 * k)
        left -= take
        if left <= 1e-12:
            break
    return cost / shares


def vwap_buy(ask: float | None, depth2: float | None, shares: float, touch_frac: float = TOUCH_FRAC) -> float | None:
    return vwap(ask, depth2, shares, True, touch_frac)


def vwap_sell(bid: float | None, depth2: float | None, shares: float, touch_frac: float = TOUCH_FRAC) -> float | None:
    return vwap(bid, depth2, shares, False, touch_frac)


@dataclass(slots=True)
class EdgeBreakdown:
    side: str
    p_side: float
    ask: float
    bid: float
    spread: float
    entry_vwap: float
    expected_exit_bid: float
    edge_before_costs: float        # expected exit bid - ask
    settlement_edge: float          # p_side - ask (what a settlement hold earns before fees)
    fee_entry: float
    fee_exit: float
    slippage: float                 # entry VWAP - ask
    adverse: float                  # expected adverse move of the ask while the order is in flight
    net_if_filled: float
    expected_net_edge: float        # per order sent: fill probability x net if filled

    def as_dict(self) -> dict:
        return {k: (round(v, 5) if isinstance(v, float) else v)
                for k, v in ((k, getattr(self, k)) for k in self.__dataclass_fields__)}  # type: ignore[attr-defined]


def expected_net_edge(side: str, p_side: float, q: Quote, fee: FeeSchedule, shares: float, kappa: float,
                      adverse: float = 0.005, fill_prob: float = 1.0, touch_frac: float = TOUCH_FRAC,
                      exit_half_spread: float = 0.01, hold_to_settlement: bool = False) -> EdgeBreakdown | None:
    """Execution-aware expected value per share of buying ``side`` now and selling after the hold.

    ``kappa`` is the fraction of the model-vs-market gap (p_side - mid) the token's mid closes over
    the hold (estimated on TRAINING data; 0 = the market does not move toward the model). The exit
    is at the BID of the book as it will be then, not as it is now: around the open the book is
    ~30c wide and re-forms to ~2c within 2-3 s, so the expected exit bid is
    ``mid + kappa * (p_side - mid) - exit_half_spread``. Entry is the VWAP at the ask plus the
    expected adverse move in flight. Both taker fees are charged; a settlement hold pays one."""
    if q.ask is None or q.bid is None:
        return None
    entry = vwap_buy(q.ask, q.ask_depth, shares, touch_frac)
    if entry is None:
        return None
    mid = (q.ask + q.bid) / 2.0
    if hold_to_settlement:
        exit_bid, fee_out = p_side, 0.0
    else:
        exit_bid = min(max(mid + kappa * (p_side - mid) - exit_half_spread, 0.0), 1.0)
        fee_out = fee.taker_fee_per_share(exit_bid)
    paid = entry + adverse
    fee_in = fee.taker_fee_per_share(min(max(paid, 0.01), 0.99))
    net = exit_bid - paid - fee_in - fee_out
    return EdgeBreakdown(side=side, p_side=p_side, ask=q.ask, bid=q.bid, spread=q.ask - q.bid, entry_vwap=entry,
                         expected_exit_bid=exit_bid, edge_before_costs=exit_bid - q.ask,
                         settlement_edge=p_side - q.ask, fee_entry=fee_in, fee_exit=fee_out,
                         slippage=entry - q.ask, adverse=adverse, net_if_filled=net,
                         expected_net_edge=fill_prob * net)


@dataclass(slots=True)
class EntryDecision:
    action: str                     # ENTER or a rejection reason
    side: str | None = None
    shares: float = 0.0
    limit: float | None = None
    edge: EdgeBreakdown | None = None


def evaluate_entry(cfg: NextMarketConfig, pred: Prediction | None, now_ms: float, quotes: dict,
                   fee: FeeSchedule, quotes_at_pred: dict | None = None,
                   hold_to_settlement: bool = False) -> EntryDecision:
    """At ``now_ms`` (the open or later), decide whether to buy the predicted side."""
    if not cfg.enabled:
        return EntryDecision(DISABLED)
    if pred is None:
        return EntryDecision(NO_PREDICTION)
    since = (now_ms - pred.open_ms) / 1000.0
    if since < 0.0:
        return EntryDecision(BEFORE_MARKET_START)
    if since > cfg.entry_window_s + 1e-9:
        return EntryDecision(ENTRY_WINDOW_CLOSED)
    side = pred.side
    p_side = pred.p_up if side == "UP" else pred.p_down
    if p_side < cfg.min_probability:
        return EntryDecision(LOW_PROBABILITY, side)
    q: Quote = quotes[side]
    if q.ask is None:
        return EntryDecision(NO_ASK, side)
    if q.bid is None:
        return EntryDecision(NO_BID, side)
    if q.ask > cfg.max_entry_price + 1e-9 or q.ask < cfg.min_entry_price - 1e-9:
        return EntryDecision(ASK_OUT_OF_RANGE, side)
    if q.ask - q.bid > cfg.max_spread + 1e-9:
        return EntryDecision(SPREAD_TOO_WIDE, side)
    shares = math.floor(cfg.order_usdc / q.ask * 100.0) / 100.0
    if shares < MIN_SHARES:
        return EntryDecision(BELOW_MIN_SIZE, side, shares)
    if shares > cfg.max_depth_fraction * (q.ask_depth or 0.0):
        return EntryDecision(THIN_BOOK, side, shares)
    if quotes_at_pred is not None:
        q0 = quotes_at_pred.get(side)
        if q0 is not None and q0.ask is not None and q.ask - q0.ask > cfg.max_quote_move + 1e-9:
            return EntryDecision(QUOTE_MOVED, side, shares)
    e = expected_net_edge(side, p_side, q, fee, shares, cfg.kappa, cfg.expected_adverse, cfg.expected_fill_prob,
                          exit_half_spread=cfg.exit_half_spread, hold_to_settlement=hold_to_settlement)
    if e is None:
        return EntryDecision(THIN_BOOK, side, shares)
    if e.net_if_filled < cfg.min_net_edge - 1e-12:
        return EntryDecision(EDGE_TOO_SMALL, side, shares, edge=e)
    return EntryDecision(ENTER, side, shares, limit=q.ask, edge=e)


def exit_reason(cfg: NextMarketConfig, held_s: float, entry: float, bid: float | None,
                p_side_now: float | None = None, ret_1s_dir: float | None = None, sigma: float | None = None,
                fee: FeeSchedule | None = None) -> str | None:
    """Why to sell now, or None to keep holding. ``ret_1s_dir`` is the latest 1 s spot log return
    signed by the position (positive = in our favour)."""
    if held_s >= cfg.max_hold_s:
        return EXIT_MAX_HOLD
    if bid is not None:
        if cfg.take_profit > 0 and bid >= entry + cfg.take_profit - 1e-9:
            return EXIT_TAKE_PROFIT
        if cfg.stop_loss > 0 and bid <= entry - cfg.stop_loss + 1e-9:
            return EXIT_STOP_LOSS
        if p_side_now is not None and fee is not None and p_side_now <= bid + fee.taker_fee_per_share(bid):
            return EXIT_EDGE_GONE
    if ret_1s_dir is not None and sigma and ret_1s_dir < -0.5 * sigma:
        return EXIT_MOMENTUM
    if cfg.hold_s > 0 and held_s >= cfg.hold_s:
        return EXIT_TIME
    return None


# ─────────────────────────────── log records ───────────────────────────────


def _fmt_quote(q: Quote | None) -> str:
    if q is None:
        return "bid=None ask=None spread=None depth=None"
    sp = q.spread
    return f"bid={q.bid} ask={q.ask} spread={'None' if sp is None else f'{sp:.2f}'} depth={q.ask_depth:.0f}"


def format_prediction(pred: Prediction, quotes: dict, decision: EntryDecision, timeline: dict | None = None) -> str:
    """The NEXT_MARKET_PREDICTION record (one per market boundary)."""
    lines = [
        "NEXT_MARKET_PREDICTION",
        f"asset={pred.asset}",
        f"market_start={pred.open_ms:.0f}",
        f"prediction_time={pred.pred_ms:.0f} ({(pred.pred_ms - pred.open_ms) / 1000.0:+.1f}s)",
        f"reference_price={pred.reference_price:.6g}",
        f"predicted_start={pred.predicted_start:.6g}",
        f"predicted_twap={pred.predicted_twap:.6g}",
        f"predicted_displacement={pred.predicted_displacement:+.6g}",
        f"p_up={pred.p_up:.4f}",
        f"p_down={pred.p_down:.4f}",
        f"confidence={pred.confidence:.4f}",
        f"UP: {_fmt_quote(quotes.get('UP'))}",
        f"DOWN: {_fmt_quote(quotes.get('DOWN'))}",
        f"decision={decision.side if decision.action == ENTER else 'NONE'} ({decision.action})",
    ]
    if decision.edge is not None:
        e = decision.edge
        lines += [f"expected_edge_before_costs={e.edge_before_costs:+.4f}",
                  f"expected_edge_after_costs={e.net_if_filled:+.4f}"]
    for k, v in (timeline or {}).items():
        lines.append(f"{k}={v}")
    return "\n".join(lines)


def format_exit(rec: dict) -> str:
    keys = ("asset", "side", "hold_time", "entry_ask", "current_bid", "markout", "fees", "slippage", "net_pnl",
            "exit_reason")
    return "\n".join(["NEXT_MARKET_EXIT"] + [f"{k}={rec.get(k)}" for k in keys if k in rec])
