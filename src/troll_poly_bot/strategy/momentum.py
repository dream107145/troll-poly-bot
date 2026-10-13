"""MOMENTUM_TAKER: buy the current executable ask when spot momentum is real,
still going, not yet priced into the token, and the price is still worth paying.

EXPERIMENTAL. Selected with ``EngineConfig.strategy = "momentum_taker"``
(``--strategy momentum_taker`` / ``TPB_STRATEGY``). The analytic strategy is
untouched; this engine subclasses it so every downstream consumer (risk,
decision journal, forensics, guard, paper and live exchanges, exits) is
reused unchanged.

The pipeline, every failure of which is a ``Reason`` in the journal:

    ASSET_ALLOWED -> MARKET_DATA_VALID (spot age, per-token QUOTE age,
    sources) -> ENOUGH_TIME_REMAINING -> MOMENTUM_STATE (STRONG_UP / WEAK_UP /
    NEUTRAL / WEAK_DOWN / STRONG_DOWN / REVERSING) -> NOT_EXHAUSTED (retrace,
    stall, book reversal) -> NO_VOLATILITY_SPIKE -> TOKEN_VALID (re-priced,
    not chased) -> ASK_AVAILABLE -> SPREAD -> FAIR_VALUE and MAX_ENTRY_PRICE
    -> ASK <= MAX_ENTRY -> LIQUIDITY (VWAP, depth consumed) -> NET_EDGE (after
    entry fee, exit fee and spread, slippage, latency and model uncertainty)
    -> cooldown / max trades -> RISK -> order: a marketable limit whose cap
    is the lower of ask + max_slippage and the max entry price. Never chased.

The entry formula, per share::

    fair        = mid(side) + continuation(z)             measured, capped
    uncertainty = base + frac * continuation * (2 - score) + vol * (vol_ratio - 1)+
    costs(p)    = fee(p) + slippage + latency + uncertainty + exit_cost
    exit_cost   = fee(fair) + spread / 2                   the exit is a taker too
    net(p)      = fair - p - costs(p)
    max_entry   = the highest tick p with net(p) >= min_net_edge
    trade iff   VWAP(size, cap) <= max_entry and net(VWAP) >= min_net_edge

What the archive says (scripts/research_momentum.py, 325 windows, 62k grid
seconds, docs/momentum_taker.md): after a 5 s spot move of z >= 3 sigma the
token on that side is worth +1.8 to +2.2 cents more than the ask 10 to 45 s
later, gross. After the venue fee (1.75c at the money), 0.5c slippage and
the ask that is actually reachable 2 s later, the net is -0.6 to -1.0c --
BEFORE paying to get out. With the exit fee and the spread charged, as they
now are, the strict profile will trade very rarely. That is the data's
verdict, and loosening ``z_threshold`` or ``min_net_edge`` to trade more is
exactly the mistake this configuration exists to prevent. The signal
outcome tracker (execution/outcomes.py) measures every skipped signal, so
evidence accumulates without spending money on it.
"""
from __future__ import annotations

import bisect
import json
import math
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..features.engine import FeatureEngine, SpotHistory
from ..risk.limits import RiskManager
from ..signals.costs import CostModel
from ..types import Order, OrderBook, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason, SideView, StrategyEngine

STRONG_UP, STRONG_DOWN = "STRONG_UP", "STRONG_DOWN"
WEAK_UP, WEAK_DOWN, NEUTRAL, REVERSING = "WEAK_UP", "WEAK_DOWN", "NEUTRAL", "REVERSING"
#: legacy name for "not strong" -- kept for callers that test ``label == WEAK``
WEAK = NEUTRAL
STATES = (STRONG_UP, WEAK_UP, NEUTRAL, WEAK_DOWN, STRONG_DOWN, REVERSING)

WINDOWS_S: tuple[tuple[str, float], ...] = (("1s", 1.0), ("2s", 2.0), ("3s", 3.0), ("main", 5.0),
                                            ("10s", 10.0), ("15s", 15.0), ("30s", 30.0))


@dataclass(slots=True)
class MomentumConfig:
    #: spot log return over this many seconds, in units of sigma * sqrt(s)
    lookback_s: float = 5.0
    #: z5 >= 3: the smallest bucket with a positive GROSS continuation and
    #: more than 1,000 rows (z5 >= 2: +0.4c gross, negative net)
    z_threshold: float = 3.0
    #: below this |z| there is no signal at all (NEUTRAL); between it and the
    #: threshold the move is WEAK_UP / WEAK_DOWN and is recorded, not traded
    weak_z: float = 1.0
    #: fraction of the last 10 one-second steps in the move's direction.
    #: 0.6 is the median; 0.8 halves the rows without improving the net
    consistency_min: float = 0.6
    #: acceleration (ret_5 - previous ret_5) must not be negative; the
    #: decelerating bucket had 105 rows and -0.9c net
    require_acceleration: bool = True
    #: reversal: the last 3 s moved against the direction by this many sigma
    reversal_z: float = 0.5
    #: at least this many spot observations inside the lookback: never a
    #: signal from a single tick
    min_updates: int = 3
    # --- exhaustion: is the move already over? ---
    #: gave back at least this fraction of its 10 s extreme move (and that
    #: move was at least one sigma): the peak is behind us
    exhaustion_retrace: float = 0.4
    #: the last 2 s travelled less than this fraction of the 5 s velocity:
    #: continuation failed. 0 disables
    exhaustion_velocity_ratio: float = 0.25
    #: the token on our side fell at least this much over the last 1.5 s while
    #: spot says up: the book has turned against the move. 0 disables
    book_reversal_min: float = 0.01
    #: 10 s realised vol over the estimate: a jump, not a trend. 0 disables
    vol_spike_ratio: float = 3.0
    # --- data freshness (NOT auto-widened by measured latency) ---
    #: the traded token's quote age (live.LiveBot.quote_age_ms): the venue
    #: timestamp our view of it is valid as of. Measured book lag p50 36-48 ms,
    #: p95 99-124 ms on 2026-09-23; 500 ms is ~4x the healthy p95 and catches
    #: a backlogged reader, which is where the dead quotes came from
    max_quote_age_ms: float = 500.0
    #: spot age as the live bot measures it (since the composite last moved,
    #: plus the measured feed lag; p50 feed lag 150-180 ms)
    max_spot_age_ms: float = 1000.0
    #: 60-120 s left was the only positive time bucket, but its ask sits near
    #: 0.51 and its win rate near a coin flip; 90 s leaves room for the
    #: 45 s thesis and an exit
    min_time_remaining_s: float = 90.0
    max_time_remaining_s: float = 295.0
    #: the token must have STARTED re-pricing (a quote that has not moved at
    #: all after the spot moved is the dead quote the venue killed 27/29
    #: times) but not finished: >= 5c in 3 s had negative gross continuation
    min_reprice: float = 0.01
    max_reprice: float = 0.05
    #: expected token value = mid + continuation; continuation is
    #: per-sigma above the base, capped. Measured: +1c/sigma above 2 at
    #: 20 s (z5>=3 bucket +2.2c, z5>=4 bucket +7.5c but n=191)
    continuation_per_sigma: float = 0.01
    continuation_base_z: float = 2.0
    continuation_cap: float = 0.05
    # --- model uncertainty (a margin for being wrong about ``fair``) ---
    #: flat margin per share
    uncertainty_base: float = 0.003
    #: fraction of the estimated continuation charged again as uncertainty,
    #: scaled up by a weak momentum score: the z>=4 bucket's +2.3 c in-sample
    #: was -0.8 c out of sample, i.e. the estimate is about as uncertain as it is large
    uncertainty_frac: float = 0.5
    #: per unit of (10 s realised vol / estimated vol - 1)
    uncertainty_vol: float = 0.01
    #: per-asset extra margin, "BTC:0,XRP:0.01" (env TPB_MOMENTUM_ASSET_UNCERTAINTY)
    asset_uncertainty: str = ""
    #: expected net edge after every cost; the analytic engine's bar
    min_net_edge: float = 0.02
    #: charge the exit (taker fee + half spread) against the entry. The
    #: position is closed before the bell by construction (max_hold_s < the
    #: minimum time left), so the exit is a second taker trade
    include_exit_costs: bool = True
    max_spread: float = 0.03
    #: marketable limit cap above the seen ask; and never chase past it
    max_slippage: float = 0.02
    #: adverse move of the reachable ask between decision and send, per share
    expected_slippage: float = 0.005
    #: fraction of a tick per 100 ms of measured reaction lag, charged as cost
    latency_cost_per_s: float = 0.01
    # --- liquidity ---
    #: never take more than this fraction of the displayed depth under the cap
    max_depth_fraction: float = 0.5
    #: the intended size may not walk the book more than this above the touch
    max_vwap_slippage: float = 0.01
    #: below this confidence the order is the smallest valid one
    full_size_min_confidence: float = 0.5
    # --- exits ---
    take_profit: float = 0.04
    stop_loss: float = 0.06
    #: momentum did not continue: flat position after this long is closed
    max_hold_s: float = 45.0
    #: the horizon the continuation was measured over: past it, a position
    #: that is not in profit and whose momentum has faded below ``exit_weak_z``
    #: is closed (the thesis had its chance)
    expected_continuation_s: float = 20.0
    exit_weak_z: float = 1.0
    #: a reversal of at least this many sigma over the lookback closes it
    exit_reversal_z: float = 1.5
    #: MARKET_ABNORMALITY: spread this wide, or spot / book older than this
    abnormal_spread: float = 0.10
    abnormal_data_age_ms: float = 3000.0
    #: LIQUIDITY_FAILURE: bid depth near the touch under this multiple of the position
    liquidity_exit_mult: float = 1.5
    #: an urgent exit may sell at most this far below the bid to get the size done
    exit_max_cross: float = 0.05
    # --- overtrading ---
    cooldown_s: float = 60.0
    max_trades_per_market: int = 1
    #: never buy a token above this ask: above it the most the token can gain
    #: is a few cents against nearly the whole stake, and 4 of the 5 signals
    #: the strict profile found live were at 0.89-0.95
    max_ask: float = 0.85
    #: comma list of assets this strategy may trade; empty = every listed one
    assets: str = ""
    #: optional per-(asset, z-bucket) continuation table fitted on a TRAINING
    #: period by scripts/replay_momentum.py; empty = the constants above
    calibration_path: str = ""
    #: which profile the defaults came from (informational)
    profile: str = "strict"

    @classmethod
    def strict(cls) -> "MomentumConfig":
        """The archive-calibrated profile: trades only where the recorded data
        did not reject the edge. The default."""
        return cls(profile="strict")

    @classmethod
    def easy(cls) -> "MomentumConfig":
        """The operator's looser profile (2026-09-23): a 2-sigma move, no
        consistency or acceleration requirement, any re-pricing short of a
        chase, and a 0.5 c net-edge bar with the continuation measured at
        2 c per sigma above 2. The archive's answer for this bucket is
        -1.6 c per share net at the reachable ask, about -0.08 USDC per
        5-share micro order. DATA COLLECTION ONLY: it is not the default
        and the data does not say it pays. The freshness, exhaustion and
        exit-cost checks still apply."""
        return cls(profile="easy", z_threshold=2.0, consistency_min=0.0, require_acceleration=False,
                   min_reprice=0.005, max_reprice=0.08, continuation_per_sigma=0.02,
                   continuation_cap=0.08, min_net_edge=0.005, min_time_remaining_s=60.0, max_ask=0.85,
                   cooldown_s=30.0, max_trades_per_market=2)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "MomentumConfig":
        env = os.environ if env is None else env
        # strict by default: a threshold is not lowered without evidence
        prof = (env.get("TPB_MOMENTUM_PROFILE") or "strict").strip().lower()
        cfg = cls.easy() if prof == "easy" else cls.strict()
        for name in cfg.__dataclass_fields__:                      # type: ignore[attr-defined]
            if name == "profile":
                continue
            raw = (env.get("TPB_MOMENTUM_" + name.upper()) or "").strip()
            if not raw:
                continue
            cur = getattr(cfg, name)
            try:
                if isinstance(cur, bool):
                    setattr(cfg, name, raw.lower() not in ("0", "false", "no"))
                elif isinstance(cur, int):
                    setattr(cfg, name, int(raw))
                elif isinstance(cur, str):
                    setattr(cfg, name, raw)
                else:
                    setattr(cfg, name, float(raw))
            except ValueError:
                continue
        return cfg

    def allowed_assets(self) -> set[str]:
        return {a.strip().upper() for a in self.assets.split(",") if a.strip()}

    def extra_uncertainty(self, asset: str) -> float:
        for part in self.asset_uncertainty.split(","):
            if ":" in part:
                a, v = part.split(":", 1)
                if a.strip().upper() == asset.upper():
                    try:
                        return max(0.0, float(v))
                    except ValueError:
                        return 0.0
        return 0.0

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}   # type: ignore[attr-defined]


@dataclass(slots=True)
class MomentumSignal:
    direction: int                 # +1 up, -1 down, 0 none
    label: str
    z: dict = field(default_factory=dict)          # lookback -> signed z
    ret_bps: dict = field(default_factory=dict)
    consistency: float = math.nan
    acceleration_bps: float = math.nan
    reversing: bool = False
    n_updates: int = 0
    sigma_per_sec: float = 0.0
    #: bps per second over each window, in the move's direction
    velocity: dict = field(default_factory=dict)
    #: fraction of the 3/5/10/15 s windows moving the same way
    persistence: float = math.nan
    #: exhaustion evidence, e.g. ["retrace 45%", "stall 0.10"]; empty = none
    exhaustion: list = field(default_factory=list)
    #: 10 s realised vol over the estimate
    vol_ratio: float = math.nan
    #: 15 s taker trade-flow imbalance in the move's direction, [-1, 1]
    flow: float = math.nan
    score: float = 0.0

    @property
    def z_main(self) -> float:
        return self.z.get("main", 0.0)

    @property
    def strong(self) -> bool:
        return self.label in (STRONG_UP, STRONG_DOWN)

    def as_dict(self) -> dict:
        nan = lambda v, nd=2: None if (v is None or (isinstance(v, float) and math.isnan(v))) else round(v, nd)  # noqa: E731
        return {"direction": self.direction, "label": self.label,
                "z": {k: round(v, 3) for k, v in self.z.items()},
                "ret_bps": {k: round(v, 2) for k, v in self.ret_bps.items()},
                "velocity_bps_s": {k: round(v, 3) for k, v in self.velocity.items()},
                "consistency": nan(self.consistency), "acceleration_bps": nan(self.acceleration_bps),
                "persistence": nan(self.persistence), "reversing": self.reversing,
                "exhaustion": list(self.exhaustion), "vol_ratio": nan(self.vol_ratio),
                "flow": nan(self.flow), "score": round(self.score, 3), "n_updates": self.n_updates}


# ─────────────────────────────── the pieces ───────────────────────────────


def _extreme(hist: SpotHistory, t_from: float, t_to: float, direction: int) -> float | None:
    """The highest (direction +1) or lowest (-1) log price in [t_from, t_to]."""
    lo = bisect.bisect_left(hist._t, t_from)                    # noqa: SLF001
    hi = bisect.bisect_right(hist._t, t_to)                     # noqa: SLF001
    pts = hist._lp[lo:hi]                                       # noqa: SLF001
    if not pts:
        return None
    return max(pts) if direction > 0 else min(pts)


def detect_momentum(hist: SpotHistory | None, now_ms: float, sigma_per_sec: float,
                    cfg: MomentumConfig, orderflow: dict | None = None) -> MomentumSignal:
    """Momentum over 1..30 s windows, in units of the asset's own sigma.

    Never from one tick (``min_updates``), and never "spot moved up, buy UP":
    a move that is reversing, has retraced from its peak, has stalled, or is
    a volatility jump is labelled so and carries the evidence.
    """
    sig = MomentumSignal(direction=0, label=NEUTRAL, sigma_per_sec=sigma_per_sec)
    if hist is None or sigma_per_sec <= 0.0:
        return sig
    lb = cfg.lookback_s
    windows = [(n, lb if n == "main" else s) for n, s in WINDOWS_S]
    rets: dict[str, float] = {}
    for name, secs in windows:
        r = hist.logret(now_ms, secs)
        if r is not None:
            rets[name] = r
    if "main" not in rets or "3s" not in rets or "10s" not in rets:
        return sig
    for name, secs in windows:
        if name in rets:
            sig.z[name] = rets[name] / (sigma_per_sec * math.sqrt(secs))
            sig.ret_bps[name] = rets[name] * 1e4
    direction = 1 if rets["main"] > 0 else -1 if rets["main"] < 0 else 0
    sig.direction = direction
    # observations inside the lookback: the move must be built from several ticks
    t0 = now_ms - lb * 1000.0
    sig.n_updates = len(hist._t) - bisect.bisect_right(hist._t, t0)          # noqa: SLF001
    # consistency: one-second steps over the last 10 s in the direction
    steps = []
    prev = hist.at(now_ms - 10_000.0)
    for k in range(9, -1, -1):
        cur = hist.at(now_ms - k * 1000.0)
        if prev is not None and cur is not None:
            steps.append(cur - prev)
        prev = cur if cur is not None else prev
    if steps and direction:
        sig.consistency = sum(1 for s in steps if (s > 0) == (direction > 0) and s != 0) / len(steps)
    r5, r10 = rets["main"], rets["10s"]
    sig.acceleration_bps = (r5 - (r10 - r5)) * 1e4 * direction
    if direction:
        for name, secs in windows:
            if name in rets:
                sig.velocity[name] = rets[name] * 1e4 / secs * direction
        agree = [rets[n] * direction > 0 for n in ("3s", "main", "10s", "15s") if n in rets]
        sig.persistence = sum(agree) / len(agree) if agree else math.nan
    sig.reversing = direction != 0 and (rets["3s"] * direction) < 0 and abs(sig.z["3s"]) >= cfg.reversal_z

    # --- exhaustion: the move may be real and already over ---
    if direction:
        cur = hist.at(now_ms)
        start = hist.at(now_ms - 10_000.0)
        ext = _extreme(hist, now_ms - 10_000.0, now_ms, direction)
        if cur is not None and start is not None and ext is not None:
            move = (ext - start) * direction
            retr = (ext - cur) * direction
            if cfg.exhaustion_retrace > 0 and move >= sigma_per_sec * math.sqrt(10.0) and move > 0 \
                    and retr / move >= cfg.exhaustion_retrace:
                sig.exhaustion.append(f"retrace {retr / move:.0%} of the 10s move")
        v2, v5 = sig.velocity.get("2s"), sig.velocity.get("main")
        if cfg.exhaustion_velocity_ratio > 0 and v2 is not None and v5 is not None and v5 > 0 \
                and v2 < cfg.exhaustion_velocity_ratio * v5:
            sig.exhaustion.append(f"stall: last 2s at {v2 / v5:.2f}x the 5s velocity")
    # choppiness, not trend: the dispersion of the 1 s steps AROUND their mean.
    # A clean 3-sigma trend has small dispersion; a jump or a whipsaw has a
    # large one. (Raw realised vol would count the trend itself as a spike.)
    if len(steps) >= 5:
        mean = sum(steps) / len(steps)
        sd = math.sqrt(sum((s - mean) ** 2 for s in steps) / len(steps))
        sig.vol_ratio = sd / sigma_per_sec
    tfi = (orderflow or {}).get("tfi_15s")
    if tfi is not None and not (isinstance(tfi, float) and math.isnan(tfi)) and direction:
        sig.flow = float(tfi) * direction

    zd = sig.z_main * direction
    if direction == 0 or zd < cfg.weak_z:
        sig.label = NEUTRAL
    elif sig.reversing:
        sig.label = REVERSING
    elif zd < cfg.z_threshold:
        sig.label = WEAK_UP if direction > 0 else WEAK_DOWN
    elif sig.exhaustion:
        sig.label = REVERSING                      # strong, but the move is behind us
    else:
        sig.label = STRONG_UP if direction > 0 else STRONG_DOWN
    sig.score = calculate_momentum_score(sig, cfg)
    return sig


def calculate_momentum_score(sig: MomentumSignal, cfg: MomentumConfig) -> float:
    """0..1: strength above the weak level, consistency, persistence across
    horizons, acceleration and (when known) trade flow. Heuristic weights,
    NOT a calibrated probability: it scales sizing and the uncertainty
    margin, never the fair value."""
    if sig.direction == 0:
        return 0.0
    z = abs(sig.z_main)
    span = max(2.0 * cfg.z_threshold - cfg.weak_z, 1e-9)
    strength = max(0.0, min(1.0, (z - cfg.weak_z) / span))
    cons = 0.5 if math.isnan(sig.consistency) else sig.consistency
    pers = 0.5 if math.isnan(sig.persistence) else sig.persistence
    acc = 0.5 if math.isnan(sig.acceleration_bps) else (1.0 if sig.acceleration_bps > 0 else 0.0)
    flow = 0.5 if math.isnan(sig.flow) else (1.0 if sig.flow > 0.2 else 0.0 if sig.flow < -0.2 else 0.5)
    score = 0.4 * strength + 0.2 * cons + 0.2 * pers + 0.1 * acc + 0.1 * flow
    if sig.label not in (STRONG_UP, STRONG_DOWN):
        score *= 0.5
    return max(0.0, min(1.0, score))


def detect_momentum_reversal(sig: MomentumSignal, side: str, cfg: MomentumConfig) -> bool:
    """For an open position on ``side``: has momentum turned against it?

    Either the lookback move now points against us by ``exit_reversal_z``
    sigma, or the move in our direction is actively REVERSING (the last 3 s
    against it). A stall or a partial retrace is momentum WEAKENING, not
    reversing: it does not close the position on its own (that would pay a
    round trip of fees on every pause); the time stop handles a thesis that
    faded without paying off.
    """
    want = 1 if side == "UP" else -1
    z = sig.z.get("main")
    if z is None:
        return False
    if (z * want) <= -cfg.exit_reversal_z:
        return True
    return sig.reversing and sig.direction == want


def get_executable_price(book: OrderBook | None, size: float, cap: float | None = None) -> tuple[float | None, float]:
    """(average ask price to fill ``size``, size available at or below ``cap``).

    The executable price is what the asks cost, level by level -- never the
    mid, the last trade, the bid or the price we would like. 2 shares at 0.45
    and 3 at 0.46 cost 0.456 each for 5, not 0.45.
    """
    if book is None or not book.asks:
        return None, 0.0
    remaining, cost, avail = size, 0.0, 0.0
    for lvl in book.asks:
        if cap is not None and lvl.price > cap + 1e-9:
            break
        avail += lvl.size
        take = min(remaining, lvl.size)
        cost += take * lvl.price
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        # count everything under the cap even though we stopped early
        if cap is not None:
            avail = sum(l.size for l in book.asks if l.price <= cap + 1e-9)
        return None, avail
    return cost / size, avail


def depth_under(book: OrderBook | None, cap: float) -> float:
    return 0.0 if book is None else sum(l.size for l in book.asks if l.price <= cap + 1e-9)


def estimate_expected_token_value(mid: float, sig: MomentumSignal, cfg: MomentumConfig,
                                  continuation: float | None = None) -> float:
    """Mid plus the measured continuation for this much momentum."""
    if continuation is None:
        continuation = estimate_continuation(sig, cfg)
    return max(0.0, min(1.0, mid + continuation))


def estimate_continuation(sig: MomentumSignal, cfg: MomentumConfig) -> float:
    z = abs(sig.z_main)
    return min(cfg.continuation_cap, max(0.0, z - cfg.continuation_base_z) * cfg.continuation_per_sigma)


def estimate_uncertainty(continuation: float, sig: MomentumSignal, cfg: MomentumConfig, asset: str = "",
                         calibrated_se: float | None = None) -> float:
    """A margin for being wrong about the fair value, per share.

    Grows with the size of the claimed continuation (the estimate is about
    as uncertain as it is large out of sample), with a weak score, with a
    volatility regime above the estimate, and per asset. A calibration table
    adds its own standard error.
    """
    u = cfg.uncertainty_base + cfg.uncertainty_frac * continuation * (2.0 - sig.score)
    if not math.isnan(sig.vol_ratio) and sig.vol_ratio > 1.0:
        u += cfg.uncertainty_vol * (sig.vol_ratio - 1.0)
    u += cfg.extra_uncertainty(asset)
    if calibrated_se is not None:
        u += calibrated_se
    return u


def exit_cost_estimate(fair: float, spread: float | None, fee) -> float:
    """Selling before the bell: the exit taker fee at about the fair price and
    half a spread (the bid, not the mid)."""
    half = 0.0 if spread is None else max(0.0, spread) / 2.0
    return fee.taker_fee_per_share(min(max(fair, 0.01), 0.99)) + half


def calculate_net_edge(expected: float, exec_price: float, bid: float | None, fee, cfg: MomentumConfig,
                       reaction_lag_s: float = 0.0, uncertainty: float = 0.0, exit_cost: float = 0.0):
    """Expected value minus everything execution costs, per share."""
    cm = CostModel(fee=fee, expected_slippage=cfg.expected_slippage, uncertainty_charge=1.0)
    latency = cfg.latency_cost_per_s * max(0.0, reaction_lag_s)
    cb = cm.evaluate(expected, exec_price, bid, model_uncertainty=latency)
    if uncertainty or exit_cost:
        cb = replace(cb, uncertainty=uncertainty, exit_cost=exit_cost,
                     net_edge=cb.net_edge - uncertainty - exit_cost)
    return cb


def max_entry_price(fair: float, fee, fixed_costs: float, min_net_edge: float, tick: float = 0.01) -> float | None:
    """The highest price on the tick grid that still leaves ``min_net_edge``
    after the entry fee and every fixed cost. p + fee(p) is increasing in p
    (the venue's rate is far below 1), so a downward scan from ``fair`` finds it.
    """
    budget = fair - fixed_costs - min_net_edge
    p = math.floor((min(budget, 1.0 - tick) + 1e-9) / tick) * tick
    while p >= tick - 1e-9:
        if p + fee.taker_fee_per_share(p) <= budget + 1e-12:
            return round(p, 4)
        p = round(p - tick, 4)
    return None


class Calibration:
    """Per-(asset, z bucket) continuation measured on a TRAINING period.

    File: ``{"horizon_s": 20, "fitted_on": [...], "rows": {"BTC|3-4": {"n_windows": 41,
    "mean": 0.012, "se": 0.006}, ...}}`` written by scripts/replay_momentum.py.
    The measured mean is shrunk toward the constant prior by its sample size;
    its standard error is charged as uncertainty.
    """

    PRIOR_WINDOWS = 30.0

    def __init__(self, rows: dict | None = None, meta: dict | None = None) -> None:
        self.rows = rows or {}
        self.meta = meta or {}

    @classmethod
    def load(cls, path: str) -> "Calibration | None":
        if not path:
            return None
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return cls(rows=data.get("rows") or {}, meta={k: v for k, v in data.items() if k != "rows"})

    @staticmethod
    def bucket(z: float) -> str:
        z = abs(z)
        return "4+" if z >= 4 else "3-4" if z >= 3 else "2-3" if z >= 2 else "<2"

    def continuation(self, asset: str, z: float, prior: float) -> tuple[float, float | None]:
        row = self.rows.get(f"{asset.upper()}|{self.bucket(z)}") or self.rows.get(f"*|{self.bucket(z)}")
        if not row or not row.get("n_windows"):
            return prior, None
        n = float(row["n_windows"])
        w = n / (n + self.PRIOR_WINDOWS)
        return w * float(row["mean"]) + (1.0 - w) * prior, float(row.get("se") or 0.0)


# ──────────────────────────────── the engine ───────────────────────────────


class MomentumEngine(StrategyEngine):
    """Same surface as StrategyEngine; a different decision inside."""

    def __init__(self, cfg: EngineConfig, mcfg: MomentumConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        super().__init__(cfg, risk, features)
        self.mcfg = mcfg
        self.calibration = Calibration.load(mcfg.calibration_path)
        self.last_signal: dict[str, MomentumSignal] = {}
        self.last_sigma: dict[str, float] = {}          # asset -> sigma per second last seen
        self.last_exit_ms: dict[str, float] = {}        # slug -> when we last exited
        self.trades: dict[str, int] = {}                # slug -> entries filled
        self.entry_ms: dict[str, float] = {}            # token -> fill time

    # --------------------------------------------------------------- entry

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        cfg, mc, m = self.cfg, self.mcfg, meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        allowed = mc.allowed_assets()
        if allowed and m.asset.upper() not in allowed:
            return self._reject(ev, Reason.ASSET_FILTERED, f"{m.asset} not in {sorted(allowed)}")

        # MARKET_DATA_VALID / MARKET_NOT_STALE
        if spot is None or spot_age_ms is None:
            return self._reject(ev, Reason.STALE_DATA, "no spot")
        max_spot = min(cfg.max_spot_age_ms, mc.max_spot_age_ms)
        if spot_age_ms > max_spot:
            return self._reject(ev, Reason.STALE_DATA, f"spot {spot_age_ms:.0f}ms > {max_spot:.0f}")
        if book_age_ms is None or book_age_ms > cfg.max_book_age_ms:
            return self._reject(ev, Reason.STALE_DATA, "book" if book_age_ms is None else f"book {book_age_ms:.0f}ms")
        if spot.n_sources >= 2 and spot.deviation_bps > cfg.max_source_deviation_bps:
            return self._reject(ev, Reason.DATA_INCONSISTENT, f"sources differ {spot.deviation_bps:.1f} bps")
        # ENOUGH_TIME_REMAINING
        if not (mc.min_time_remaining_s <= left <= mc.max_time_remaining_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
        need_ms = latency.round_trip_ms() + cfg.latency_safety_margin_ms
        if left * 1000.0 < need_ms:
            return self._reject(ev, Reason.CANNOT_LAND_IN_TIME)
        if vol is None or not getattr(vol, "ready", False):
            return self._reject(ev, Reason.MODEL_NOT_READY)
        self.last_sigma[m.asset] = vol.sigma_per_sec
        # the market's own probability, for the record and the re-pricing test
        mid_up = book_up.mid if book_up is not None else None
        if mid_up is None and book_down is not None and book_down.mid is not None:
            mid_up = 1.0 - book_down.mid
        ev.p_market = mid_up
        mh = self.features.market.get(m.slug)
        prev_mid = mh.at(now - 3000.0) if mh is not None else None
        prev_mid_short = mh.at(now - 1500.0) if mh is not None else None
        if mid_up is not None:
            self.features.update_market(m.slug, mid_up, now)
        # MOMENTUM_STATE
        sig = detect_momentum(self.features.spot.get(m.asset), now, vol.sigma_per_sec, mc, orderflow)
        self.last_signal[m.asset] = sig
        ev.features = {"momentum": sig.as_dict()}
        ev.spot_move_bps = sig.ret_bps.get("main", math.nan)
        if prev_mid is not None and mid_up is not None:
            ev.market_reprice = mid_up - prev_mid
        if sig.label == REVERSING and sig.reversing:
            return self._reject(ev, Reason.MOMENTUM_REVERSING, f"z3 {sig.z.get('3s', 0):+.2f} against z{mc.lookback_s:.0f} {sig.z_main:+.2f}")
        if sig.label == REVERSING:
            return self._reject(ev, Reason.MOMENTUM_EXHAUSTED, "; ".join(sig.exhaustion))
        if not sig.strong:
            return self._reject(ev, Reason.WEAK_MOMENTUM,
                                f"{sig.label} z{mc.lookback_s:.0f} {sig.z_main:+.2f} < {mc.z_threshold}")
        if sig.n_updates < mc.min_updates:
            return self._reject(ev, Reason.WEAK_MOMENTUM, f"{sig.n_updates} spot updates in {mc.lookback_s:.0f}s")
        if not math.isnan(sig.consistency) and sig.consistency < mc.consistency_min:
            return self._reject(ev, Reason.WEAK_MOMENTUM, f"consistency {sig.consistency:.2f}")
        if mc.require_acceleration and not math.isnan(sig.acceleration_bps) and sig.acceleration_bps < 0:
            return self._reject(ev, Reason.MOMENTUM_EXHAUSTED, f"decelerating {sig.acceleration_bps:+.1f} bps")
        if mc.vol_spike_ratio > 0 and not math.isnan(sig.vol_ratio) and sig.vol_ratio >= mc.vol_spike_ratio:
            return self._reject(ev, Reason.VOLATILITY_SPIKE, f"10s vol {sig.vol_ratio:.1f}x the estimate")
        side = "UP" if sig.direction > 0 else "DOWN"
        token = m.yes_token_id if side == "UP" else m.no_token_id
        book = book_up if side == "UP" else book_down
        # the traded token's own quote age: stale -> no trade, whatever else says go
        qa = (quote_ages or {}).get(token) if quote_ages is not None else book_age_ms
        if quote_ages is not None and qa is None:
            return self._reject(ev, Reason.STALE_DATA, f"no valid {side} book on the current feed connection")
        if qa is not None and qa > mc.max_quote_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"{side} quote {qa:.0f}ms > {mc.max_quote_age_ms:.0f}")
        # POLYMARKET_TOKEN_VALID: mid, and it must have started re-pricing
        if mid_up is None:
            return self._reject(ev, Reason.NO_OFFER, "no token mid")
        mid = mid_up if side == "UP" else 1.0 - mid_up
        reprice = (ev.market_reprice if side == "UP" else -ev.market_reprice) if not math.isnan(ev.market_reprice) else None
        # a book that has turned against the move over the last 1.5 s
        if mc.book_reversal_min > 0 and prev_mid_short is not None:
            short = (mid_up - prev_mid_short) * (1 if side == "UP" else -1)
            if short <= -mc.book_reversal_min:
                return self._reject(ev, Reason.MOMENTUM_EXHAUSTED,
                                    f"book reversal: {side} mid {short:+.3f} in 1.5s against spot")
        continuation = estimate_continuation(sig, mc)
        cal_se = None
        if self.calibration is not None:
            continuation, cal_se = self.calibration.continuation(m.asset, sig.z_main, continuation)
            continuation = max(0.0, min(mc.continuation_cap, continuation))
        p_used = estimate_expected_token_value(mid, sig, mc, continuation)
        ev.p_analytic, ev.p_used = p_used if side == "UP" else 1.0 - p_used, p_used if side == "UP" else 1.0 - p_used
        sv = SideView(side=side, token_id=token, p_model=p_used, p_market=mid, ask=None, bid=None,
                      depth_at_limit=0.0, costs=None, reason=None)
        ev.sides = [sv]
        if reprice is None or reprice < mc.min_reprice:
            sv.reason = Reason.STALE_QUOTE
            return self._reject(ev, Reason.STALE_QUOTE, f"token re-priced {0.0 if reprice is None else reprice:+.3f} in 3s")
        if reprice > mc.max_reprice:
            sv.reason = Reason.TOKEN_ALREADY_MOVED
            return self._reject(ev, Reason.TOKEN_ALREADY_MOVED, f"token re-priced {reprice:+.3f} in 3s")
        # ASK_AVAILABLE / SPREAD_ACCEPTABLE
        if book is None or book.best_ask is None:
            sv.reason = Reason.NO_OFFER
            return self._reject(ev, Reason.NO_OFFER)
        ask, bid = book.best_ask, book.best_bid
        sv.ask, sv.bid = ask, bid
        if not (cfg.min_trade_price <= ask <= min(cfg.max_trade_price, mc.max_ask)):
            sv.reason = Reason.PRICE_BAND
            return self._reject(ev, Reason.PRICE_BAND, f"ask {ask:.2f} outside {cfg.min_trade_price:.2f}-{min(cfg.max_trade_price, mc.max_ask):.2f}")
        spread = None if bid is None else ask - bid
        if bid is None or spread > mc.max_spread + 1e-9:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"spread {0 if bid is None else spread:.3f}")
        # FAIR VALUE -> costs -> the most this signal may pay
        lag_s = (latency.reaction_lag_ms() + (spot_age_ms or 0.0)) / 1000.0
        latency_cost = mc.latency_cost_per_s * max(0.0, lag_s)
        uncertainty = estimate_uncertainty(continuation, sig, mc, m.asset, cal_se)
        exit_cost = exit_cost_estimate(p_used, spread, meta.fee) if mc.include_exit_costs else 0.0
        fixed = mc.expected_slippage + latency_cost + uncertainty + exit_cost
        max_entry = max_entry_price(p_used, meta.fee, fixed, mc.min_net_edge, m.tick_size or 0.01)
        ev.max_entry_price = max_entry
        cap = min(1.0 - m.tick_size, round(ask + mc.max_slippage, 4))
        if max_entry is not None:
            cap = min(cap, max_entry)
        exec_info = {"fair": round(p_used, 4), "continuation": round(continuation, 4),
                     "uncertainty": round(uncertainty, 4), "exit_cost": round(exit_cost, 4),
                     "latency_cost": round(latency_cost, 4), "latency_estimate_ms": round(lag_s * 1000.0),
                     "quote_age_ms": None if qa is None else round(qa), "spot_age_ms": round(spot_age_ms),
                     "best_ask": ask, "best_bid": bid, "spread": round(spread, 4),
                     "max_entry": max_entry, "cap": round(cap, 4), "calibrated": cal_se is not None}
        ev.features["execution"] = exec_info
        # EXPECTED_EDGE_CALCULATED on the EXECUTABLE price for the minimum size
        probe = max(m.min_size, cfg.min_depth_shares)
        exec_probe, _ = get_executable_price(book, probe, ask + mc.max_slippage)
        if exec_probe is not None:
            sv.costs = calculate_net_edge(p_used, exec_probe, bid, meta.fee, mc, lag_s, uncertainty, exit_cost)
            sv.entry_px = exec_probe
        if max_entry is None or ask > max_entry + 1e-9:
            # the chase stops here: the price the book asks is more than the
            # signal is worth after costs, however strong the momentum is
            sv.reason = Reason.ASK_ABOVE_MAX_ENTRY
            return self._reject(ev, Reason.ASK_ABOVE_MAX_ENTRY,
                                f"ask {ask:.2f} > max entry {'none' if max_entry is None else f'{max_entry:.2f}'} "
                                f"(fair {p_used:.3f} - costs {fixed:.3f} - edge {mc.min_net_edge})")
        # LIQUIDITY_SUFFICIENT under the cap
        depth = depth_under(book, cap)
        sv.depth_at_limit = depth
        exec_info["depth_under_cap"] = round(depth, 2)
        if depth < cfg.min_depth_shares:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"depth {depth:.0f} at {cap:.2f}")
        exec_price, avail = get_executable_price(book, probe, cap)
        if exec_price is None:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"{avail:.0f} shares under {cap:.2f}, need {probe:.0f}")
        cb = calculate_net_edge(p_used, exec_price, bid, meta.fee, mc, lag_s, uncertainty, exit_cost)
        sv.costs = cb
        sv.entry_px = exec_price
        # NET_EDGE_ABOVE_THRESHOLD
        if cb.net_edge < mc.min_net_edge:
            sv.reason = Reason.EDGE_TOO_SMALL
            return self._reject(ev, Reason.EDGE_TOO_SMALL,
                                f"expected {p_used:.3f} exec {exec_price:.3f} net {cb.net_edge:+.4f} < {mc.min_net_edge}")
        # overtrading: cooldown and per-market cap
        last = self.last_exit_ms.get(m.slug)
        if last is not None and now - last < mc.cooldown_s * 1000.0:
            return self._reject(ev, Reason.COOLDOWN, f"{(mc.cooldown_s * 1000.0 - (now - last)) / 1000.0:.0f}s left")
        if self.trades.get(m.slug, 0) >= mc.max_trades_per_market:
            return self._reject(ev, Reason.MAX_TRADES, f"{self.trades[m.slug]} in this market")
        # RISK_CHECK / NO_DUPLICATE_POSITION, sized by confidence and liquidity
        confidence = sig.score * (cb.net_edge / (cb.net_edge + uncertainty)) if cb.net_edge + uncertainty > 0 else 0.0
        epoch = int(m.open_ts // 1000)
        avail_for_size = depth * mc.max_depth_fraction / max(self.risk.cfg.max_depth_fraction, 1e-9)
        shares, note = self.risk.size(p=p_used, price=exec_price, confidence=max(confidence, 0.1), balance=balance,
                                      asset=m.asset, epoch=epoch, side=side, available_shares=avail_for_size,
                                      min_size=m.min_size)
        if shares > 0.0 and confidence < mc.full_size_min_confidence and not self.risk.cfg.micro_fixed_size:
            small, small_note = self.risk._smallest_valid(exec_price, balance, m.asset, epoch,   # noqa: SLF001
                                                          avail_for_size, m.min_size)
            if 0.0 < small < shares:
                shares, note = small, f"low confidence {confidence:.2f}: smallest valid size"
        if shares <= 0.0:
            return self._reject(ev, Reason.SIZE_ZERO, note)
        exec_for_size, _ = get_executable_price(book, shares, cap)
        if exec_for_size is None:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"cannot fill {shares:.2f} under {cap:.2f}")
        consumed = shares / depth if depth > 0 else 1.0
        exec_info.update({"size": shares, "vwap": round(exec_for_size, 4), "depth_consumed": round(consumed, 3),
                          "vwap_slippage": round(exec_for_size - ask, 4)})
        if consumed > mc.max_depth_fraction + 1e-9:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"{shares:.1f} sh would take {consumed:.0%} of the depth under {cap:.2f}")
        if exec_for_size - ask > mc.max_vwap_slippage + 1e-9:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"VWAP {exec_for_size:.3f} walks {exec_for_size - ask:+.3f} past the ask")
        cb_size = calculate_net_edge(p_used, exec_for_size, bid, meta.fee, mc, lag_s, uncertainty, exit_cost)
        if cb_size.net_edge < mc.min_net_edge:
            sv.costs = cb_size
            sv.reason = Reason.EDGE_TOO_SMALL
            return self._reject(ev, Reason.EDGE_TOO_SMALL,
                                f"at size {shares:.1f}: VWAP {exec_for_size:.3f} net {cb_size.net_edge:+.4f} < {mc.min_net_edge}")
        verdict = self.risk.check(m.slug, m.asset, epoch, shares * exec_for_size)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)
        sv.costs = cb_size
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, cap, exec_for_size
        ev.size, ev.net_edge, ev.confidence, ev.size_note = shares, cb_size.net_edge, confidence, note
        self.last[m.slug] = ev
        return ev

    # ------------------------------------------------------------ revalidate

    def revalidate(self, ev: Evaluation, book: OrderBook | None, min_edge: float | None = None) -> Reason | None:
        """The ask must still be inside the cap and the edge must survive it. Never chase."""
        if book is None or book.best_ask is None:
            return Reason.NO_OFFER
        exec_price, _ = get_executable_price(book, ev.size, ev.limit_price)
        if exec_price is None:
            return Reason.HIGH_SLIPPAGE if book.best_ask > ev.limit_price + 1e-9 else Reason.LOW_LIQUIDITY
        if ev.max_entry_price is not None and exec_price > ev.max_entry_price + 1e-9:
            return Reason.ASK_ABOVE_MAX_ENTRY
        moved = exec_price - ev.expected_price
        if ev.net_edge - max(0.0, moved) < (self.mcfg.min_net_edge if min_edge is None else min_edge):
            return Reason.EDGE_TOO_SMALL
        return None

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        """A marketable limit: fill-or-kill at up to the cap, never a market order."""
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                     tif=TimeInForce.FOK, expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                     tag=f"{ev.side} momentum z={ev.features.get('momentum', {}).get('z', {}).get('main', 0):+.2f} "
                         f"edge={ev.net_edge:+.3f} cap={ev.limit_price:.2f}")

    # ---------------------------------------------------------------- exits

    def on_entry_fill(self, slug: str, token_id: str, now_ms: float) -> None:
        self.trades[slug] = self.trades.get(slug, 0) + 1
        self.entry_ms[token_id] = now_ms

    def on_exit(self, slug: str, token_id: str, now_ms: float) -> None:
        self.last_exit_ms[slug] = now_ms
        self.entry_ms.pop(token_id, None)

    def momentum_now(self, asset: str, now_ms: float, orderflow: dict | None = None) -> MomentumSignal | None:
        """A fresh read of the asset's momentum for position management (the
        entry scan may not have evaluated this asset this tick)."""
        sigma = self.last_sigma.get(asset)
        hist = self.features.spot.get(asset)
        if sigma is None or hist is None:
            return self.last_signal.get(asset)
        sig = detect_momentum(hist, now_ms, sigma, self.mcfg, orderflow)
        self.last_signal[asset] = sig
        return sig

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig: MomentumSignal | None = None, spread: float | None = None,
                      bid_depth: float | None = None, size: float | None = None,
                      data_age_ms: float | None = None) -> str | None:
        """manage_momentum_position: which exit fires, if any, and why.

        TAKE_PROFIT         the bid reached entry + take_profit
        STOP_LOSS           the bid fell to entry - stop_loss
        LIQUIDITY_FAILURE   the bid side near the touch is thinning below
                            ``liquidity_exit_mult`` x our size: leave while a bid exists
        MARKET_ABNORMALITY  spread blow-out, or spot / book data too old to
                            manage the position on its thesis
        MOMENTUM_REVERSAL   momentum turned against the position
        TIME_STOP           past the continuation horizon, not in profit and
                            momentum faded -- or the hard maximum hold

        Momentum continuing in our favour holds the position (no time stop
        before the hard maximum while it is still strong); momentum weakening
        with no profit after the expected horizon closes it.
        """
        mc = self.mcfg
        want = 1 if side == "UP" else -1
        rise = bid - entry
        if rise >= mc.take_profit:
            return "TAKE_PROFIT"
        if rise <= -mc.stop_loss:
            return "STOP_LOSS"
        if bid_depth is not None and size is not None and size > 0 and bid_depth < mc.liquidity_exit_mult * size:
            return "LIQUIDITY_FAILURE"
        if (spread is not None and spread >= mc.abnormal_spread) or \
                (data_age_ms is not None and data_age_ms >= mc.abnormal_data_age_ms):
            return "MARKET_ABNORMALITY"
        if sig is None:
            sig = self.last_signal.get(asset)
        if sig is not None and detect_momentum_reversal(sig, side, mc):
            return "MOMENTUM_REVERSAL"
        opened = self.entry_ms.get(token_id)
        if opened is not None:
            held_s = (now_ms - opened) / 1000.0
            if held_s >= mc.max_hold_s:
                return "TIME_STOP"
            still = sig is not None and (sig.z_main * want) >= mc.exit_weak_z
            if held_s >= mc.expected_continuation_s and rise <= 0.0 and not still:
                return "TIME_STOP"
        return None

    def exit_kind(self, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float) -> str | None:
        """The price/momentum/time exits on the last signal seen (compatibility)."""
        return self.exit_decision(asset=asset, side=side, token_id=token_id, entry=entry, bid=bid, now_ms=now_ms)

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = "momentum_taker"
        snap["momentum"] = {a: s.as_dict() for a, s in self.last_signal.items()}
        snap["momentum_config"] = self.mcfg.as_dict()
        snap["calibration"] = None if self.calibration is None else self.calibration.meta
        return snap
