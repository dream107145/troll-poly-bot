"""EARLY_WINDOW_LOW_ASK (research only): buy a cheap token in the first minute.

The hypothesis under test: a token whose executable ask is below a low-price
threshold during the first 60 seconds of a 5-minute window subsequently
re-prices far enough to pay for

    entry ASK + spread + taker fee + slippage + latency + exit at the BID

This module is the decision rule and nothing else. It is NOT wired into
``live.py``, the paper bot or any real-money path; the only caller is the
replay (``backtest/early_window_replay.py``). Enabling it anywhere with money
behind it is a separate, deliberate decision that the replay has to earn.

Timing
------
``seconds_since_market_start = (now - open_ts) / 1000``. A new entry is allowed
only while

    entry_window_start_s <= seconds_since_market_start <= entry_window_end_s

which is ``0 <= s <= 60`` by default: nothing before the window opens (the
venue lists the market and accepts orders earlier; we do not), nothing after
the first minute. Both bounds are inclusive, so 12:00:00 and 12:01:00 are
allowed and 11:59:59 and 12:01:01 are not.

Entry conditions (every one must hold; each failure is a named reason)
----------------------------------------------------------------------
    inside the entry window                  BEFORE_MARKET_START / ENTRY_WINDOW_CLOSED
    order book fresh and not crossed         STALE_BOOK / BOOK_INVALID
    crypto spot fresh and consistent         STALE_SPOT / DATA_INCONSISTENT
    model probability available              MODEL_NOT_READY
    per side:
      an ask exists                          NO_ASK
      ask < low_price_threshold              ASK_NOT_LOW   (and >= min_ask: ASK_TOO_LOW)
      spread <= max_spread                   SPREAD_TOO_WIDE
      spread / ask <= max_spread_pct         SPREAD_PCT_TOO_WIDE
      ask depth >= min_ask_depth             THIN_ASK
      order size <= depth * max_depth_frac   EXECUTION_NOT_REALISTIC  (and time to land)
      p_side - ask >= min_model_edge         EDGE_TOO_SMALL
      expected net after costs > min_net     NET_NOT_POSITIVE

The entry price is the executable ASK. The mid appears only in the blended
probability (the live engine's winner's-curse correction), never as a price.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..signals.costs import FeeSchedule

TICK = 0.01

BEFORE_MARKET_START = "BEFORE_MARKET_START"
ENTRY_WINDOW_CLOSED = "ENTRY_WINDOW_CLOSED"
STALE_BOOK = "STALE_BOOK"
BOOK_INVALID = "BOOK_INVALID"
STALE_SPOT = "STALE_SPOT"
DATA_INCONSISTENT = "DATA_INCONSISTENT"
MODEL_NOT_READY = "MODEL_NOT_READY"
NO_ASK = "NO_ASK"
ASK_NOT_LOW = "ASK_NOT_LOW"
ASK_TOO_LOW = "ASK_TOO_LOW"
SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
SPREAD_PCT_TOO_WIDE = "SPREAD_PCT_TOO_WIDE"
THIN_ASK = "THIN_ASK"
EXECUTION_NOT_REALISTIC = "EXECUTION_NOT_REALISTIC"
EDGE_TOO_SMALL = "EDGE_TOO_SMALL"
NET_NOT_POSITIVE = "NET_NOT_POSITIVE"

#: most informative first: a side that reached the edge test says more than one with no offer
_SIDE_REASON_ORDER = (NET_NOT_POSITIVE, EDGE_TOO_SMALL, EXECUTION_NOT_REALISTIC, THIN_ASK, SPREAD_PCT_TOO_WIDE,
                      SPREAD_TOO_WIDE, ASK_TOO_LOW, ASK_NOT_LOW, NO_ASK)

TARGET_OR_SETTLE = "target_or_settle"
TARGET_OR_TIME = "target_or_time"


@dataclass(frozen=True, slots=True)
class EarlyWindowConfig:
    """Defaults were fixed BEFORE the replay was run and are not tuned to it."""
    # --- timing: seconds since the market start, both bounds inclusive ------
    entry_window_start_s: float = 0.0
    entry_window_end_s: float = 60.0

    # --- price and book -------------------------------------------------------
    #: the ask must be strictly below this
    low_price_threshold: float = 0.40
    #: the analytic engine's price band floor: 1-4 c tokens are lottery tickets
    min_ask: float = 0.05
    max_spread: float = 0.03
    #: spread / ask; at a 0.30 ask a 3 c spread is 10 %
    max_spread_pct: float = 0.10
    #: shares within two ticks of the ask (what the recorder keeps)
    min_ask_depth: float = 10.0
    #: our order may take at most this share of the visible depth (the rest is
    #: assumed to go to faster takers); the momentum engine's rule
    max_depth_frac: float = 0.5
    #: shares per order: the venue minimum, as micro-live sends
    order_size: float = 5.0

    # --- data freshness -------------------------------------------------------
    max_book_age_ms: float = 1000.0
    max_spot_age_ms: float = 1500.0
    min_spot_sources: int = 1
    max_source_deviation_bps: float = 15.0

    # --- model and costs ------------------------------------------------------
    #: weight on the market mid in the probability (the live engine's 0.5)
    market_blend: float = 0.5
    #: p_side - ask must be at least this
    min_model_edge: float = 0.03
    #: expected net per share after every cost must be strictly above this
    min_expected_net: float = 0.0
    expected_slippage: float = 0.005
    #: edge lost per second between the data we decided on and the order
    #: landing (the momentum engine's measured-reaction-lag charge)
    latency_cost_per_s: float = 0.01
    #: fraction of the pricer's sigma-error band charged as uncertainty
    uncertainty_charge: float = 0.5
    #: the order may pay up to this much above the seen ask (never above the
    #: price at which the expected net stops being positive)
    max_slippage: float = 0.01
    #: an order must be able to land with this much of the window left
    min_secs_left_to_land: float = 5.0

    # --- exits ----------------------------------------------------------------
    exit_mode: str = TARGET_OR_SETTLE
    #: sell into the BID once it is this far above the entry price
    take_profit: float = 0.05
    #: and the round trip clears this much per share after both fees
    take_profit_min_net: float = 0.005
    #: TARGET_OR_TIME: sell at the bid after holding this long
    max_hold_s: float = 60.0
    #: time exits cross the bid by this much (urgent, as the live stop does)
    urgent_cross: float = 0.02
    #: no exit attempts inside the last seconds: the position settles 1/0
    exit_min_secs_left: float = 10.0
    #: charge the exit fee at entry+target in the expected net (conservative
    #: when the position is instead held to a fee-free settlement)
    charge_exit_fee: bool = True

    @property
    def uses_target(self) -> bool:
        return self.take_profit > 0.0


@dataclass(frozen=True, slots=True)
class Quote:
    bid: float | None
    bid_depth: float
    ask: float | None
    ask_depth: float


@dataclass(slots=True)
class SideCheck:
    side: str
    ask: float | None = None
    bid: float | None = None
    spread: float | None = None
    spread_pct: float | None = None
    ask_depth: float = 0.0
    p_side: float | None = None
    model_edge: float | None = None
    expected_net: float | None = None
    costs: dict = field(default_factory=dict)
    limit_price: float | None = None
    reason: str | None = None


@dataclass(slots=True)
class EntryDecision:
    seconds_since_start: float
    reason: str | None = None
    side: str | None = None
    ask: float | None = None
    bid: float | None = None
    spread: float | None = None
    spread_pct: float | None = None
    ask_depth: float = 0.0
    p_up_model: float | None = None
    p_side: float | None = None
    model_edge: float | None = None
    expected_net: float | None = None
    limit_price: float | None = None
    size: float = 0.0
    costs: dict = field(default_factory=dict)
    sides: list[SideCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.reason is None and self.side is not None


def seconds_since_market_start(open_ts_ms: float, now_ms: float) -> float:
    return (now_ms - open_ts_ms) / 1000.0


def entry_window_reason(s: float, cfg: EarlyWindowConfig) -> str | None:
    """None when a new entry is allowed at ``s`` seconds since the market start."""
    if s < cfg.entry_window_start_s:
        return BEFORE_MARKET_START
    if s > cfg.entry_window_end_s:
        return ENTRY_WINDOW_CLOSED
    return None


def expected_costs(cfg: EarlyWindowConfig, fee: FeeSchedule, price: float, latency_s: float,
                   model_uncertainty: float) -> dict:
    """Every per-share cost of buying at ``price`` and getting out again."""
    entry_fee = fee.taker_fee_per_share(price)
    exit_fee = fee.taker_fee_per_share(min(price + cfg.take_profit, 1.0 - TICK)) \
        if (cfg.charge_exit_fee and cfg.uses_target) else 0.0
    latency = cfg.latency_cost_per_s * max(0.0, latency_s)
    unc = cfg.uncertainty_charge * max(0.0, model_uncertainty)
    return {"entry_fee": entry_fee, "exit_fee": exit_fee, "slippage": cfg.expected_slippage,
            "latency": latency, "uncertainty": unc,
            "total": entry_fee + exit_fee + cfg.expected_slippage + latency + unc}


def evaluate_entry(
    cfg: EarlyWindowConfig,
    *,
    now_ms: float,
    open_ts_ms: float,
    close_ts_ms: float,
    quotes: dict[str, Quote],
    book_age_ms: float | None,
    spot_age_ms: float | None,
    n_sources: int,
    source_deviation_bps: float,
    p_up_model: float | None,
    model_uncertainty: float,
    fee: FeeSchedule,
    one_way_ms: float,
) -> EntryDecision:
    """Is a new entry allowed right now, on which side, at what price cap?"""
    s = seconds_since_market_start(open_ts_ms, now_ms)
    d = EntryDecision(seconds_since_start=s, p_up_model=p_up_model)

    r = entry_window_reason(s, cfg)
    if r is not None:
        d.reason = r
        return d
    if book_age_ms is None or book_age_ms > cfg.max_book_age_ms:
        d.reason = STALE_BOOK
        return d
    for q in quotes.values():
        if q.bid is not None and q.ask is not None and q.bid >= q.ask:
            d.reason = BOOK_INVALID
            return d
    if spot_age_ms is None or spot_age_ms > cfg.max_spot_age_ms:
        d.reason = STALE_SPOT
        return d
    if n_sources < cfg.min_spot_sources or (n_sources >= 2 and source_deviation_bps > cfg.max_source_deviation_bps):
        d.reason = DATA_INCONSISTENT
        return d
    if p_up_model is None or math.isnan(p_up_model):
        d.reason = MODEL_NOT_READY
        return d

    up = quotes.get("UP")
    mid_up = None
    if up is not None and up.bid is not None and up.ask is not None:
        mid_up = (up.bid + up.ask) / 2.0
    p_used = p_up_model if mid_up is None else (1.0 - cfg.market_blend) * p_up_model + cfg.market_blend * mid_up
    left_s = (close_ts_ms - now_ms) / 1000.0
    latency_s = ((spot_age_ms or 0.0) + one_way_ms) / 1000.0

    best: SideCheck | None = None
    for side in ("UP", "DOWN"):
        q = quotes.get(side)
        sc = SideCheck(side=side, p_side=p_used if side == "UP" else 1.0 - p_used)
        d.sides.append(sc)
        if q is None or q.ask is None:
            sc.reason = NO_ASK
            continue
        sc.ask, sc.bid, sc.ask_depth = q.ask, q.bid, q.ask_depth
        if q.bid is not None:
            sc.spread = round(q.ask - q.bid, 6)
            sc.spread_pct = sc.spread / q.ask if q.ask > 0 else math.inf
        if not q.ask < cfg.low_price_threshold:
            sc.reason = ASK_NOT_LOW
            continue
        if q.ask < cfg.min_ask:
            sc.reason = ASK_TOO_LOW
            continue
        # no bid = no measurable spread = no exit: treat as infinitely wide
        if sc.spread is None or sc.spread > cfg.max_spread + 1e-9:
            sc.reason = SPREAD_TOO_WIDE
            continue
        if sc.spread_pct > cfg.max_spread_pct + 1e-9:
            sc.reason = SPREAD_PCT_TOO_WIDE
            continue
        if q.ask_depth < cfg.min_ask_depth:
            sc.reason = THIN_ASK
            continue
        if cfg.order_size > cfg.max_depth_frac * q.ask_depth or left_s < cfg.min_secs_left_to_land:
            sc.reason = EXECUTION_NOT_REALISTIC
            continue
        sc.model_edge = sc.p_side - q.ask
        costs = expected_costs(cfg, fee, q.ask, latency_s, model_uncertainty)
        sc.costs = {k: round(v, 5) for k, v in costs.items()}
        sc.expected_net = sc.model_edge - costs["total"]
        if sc.model_edge < cfg.min_model_edge:
            sc.reason = EDGE_TOO_SMALL
            continue
        if not sc.expected_net > cfg.min_expected_net:
            sc.reason = NET_NOT_POSITIVE
            continue
        # the cap: at most max_slippage through the seen ask, and never a
        # price at which the expected net stops being positive
        cap = q.ask
        p = round(q.ask + TICK, 4)
        while p <= q.ask + cfg.max_slippage + 1e-9:
            c = expected_costs(cfg, fee, p, latency_s, model_uncertainty)
            if sc.p_side - p - c["total"] <= cfg.min_expected_net:
                break
            cap = p
            p = round(p + TICK, 4)
        sc.limit_price = cap
        if best is None or sc.expected_net > best.expected_net:
            best = sc

    if best is None:
        reasons = {sc.reason for sc in d.sides}
        d.reason = next((r for r in _SIDE_REASON_ORDER if r in reasons), NO_ASK)
        return d
    d.side, d.ask, d.bid, d.spread, d.spread_pct = best.side, best.ask, best.bid, best.spread, best.spread_pct
    d.ask_depth, d.p_side, d.model_edge, d.expected_net = best.ask_depth, best.p_side, best.model_edge, best.expected_net
    d.limit_price, d.costs, d.size = best.limit_price, best.costs, cfg.order_size
    return d


def exit_reason(cfg: EarlyWindowConfig, fee: FeeSchedule, *, entry: float, bid: float | None, held_s: float,
                secs_left: float) -> str | None:
    """TAKE_PROFIT / TIME_EXIT, or None to keep holding (to settlement)."""
    if bid is None or secs_left <= cfg.exit_min_secs_left:
        return None
    if cfg.uses_target and bid >= entry + cfg.take_profit - 1e-9:
        net = bid - entry - fee.taker_fee_per_share(entry) - fee.taker_fee_per_share(bid)
        if net >= cfg.take_profit_min_net:
            return "TAKE_PROFIT"
    if cfg.exit_mode == TARGET_OR_TIME and held_s >= cfg.max_hold_s:
        return "TIME_EXIT"
    return None
