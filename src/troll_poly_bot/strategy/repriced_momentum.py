"""REPRICED_MOMENTUM: after a spot shock, once the token has ALREADY repriced part of
the way, buy only while the spot is still moving and the token still following, and
sell within seconds.

EXPERIMENTAL, and the research verdict is NEGATIVE (docs/repriced_momentum_2026-09-24.md):
on the 5-minute archive, with the same execution model as spot-lag, no configuration
made money on the three held-out periods (3,928 configurations, 0 % positive; the one
chosen on training lost 1.8c/share unseen, t -2.2). The momentum is real but smaller
than one round trip's spread and fees, and the orders that fill are the ones where it
stalled. The engine exists so the hypothesis can be watched in paper and the answer
checked on live books; it is behind ``TPB_REPRICED_MOM_ENABLED=1`` and refuses real
money unless ``TPB_REPRICED_MOM_ALLOW_REAL=1`` is set as well (__main__). It is a
separate strategy with its own id (``repriced_momentum``), its own env prefix
(``TPB_REPRICED_MOM_*``), its own budget and its own data files; spot-lag is untouched.

Per market, every evaluation (the live bot's 200 ms tick)::

    shock      the spot-lag rule: 1 s spot move >= k sigma, 3 s agrees, accelerating;
               it opens a ``shock_window_s`` window with the token mid and the
               market-implied fair value (MFV, spot_lag) as they were 1 s before it
    expected   = side MFV now - side MFV before          (where the token should go)
    actual     = side mid now - side mid before         (where it went)
    pct        = actual / expected                      (DOWN normalised: + is our way)
    fair       = side mid before + expected
    momentum   spot return over ``momentum_horizon_ms``, in sigma (sqrt-time scaled),
               must be > 0 and >= ``min_spot_z``; the token's own ``token_horizon_ms``
               move must be > 0 (direction agreement)
    overshoot  = ask - fair  <=  ``max_overshoot_cents``
    edge       = expected continuation (CONTINUATION_C, TRAINING only) - spread - both
                 taker fees - slippage  >=  ``min_edge_cents``
    order      FOK at the ask (+ ``limit_offset_cents``, 0 by default: never chases)

Exit: an optional TAKE-PROFIT (``take_profit_cents`` above the entry, sold at the bid) and
STOP-LOSS come first; then ``exit_mode``: ``timer`` sells ``hold_s`` after the fill (<= 0:
hold to the target, the stop or settlement); ``reversal`` sells when
the spot's 500 ms move or the token's turns against the position; ``fair`` when the bid
reaches the fair value; ``any`` on either -- the event modes at most ``max_hold_s``.
Every candidate evaluation is written, with every feature and the decision, to
``repriced_momentum_candidates[.TAG].jsonl``; the log carries [MOM] lines with explicit
REJECT_* reasons.
"""
from __future__ import annotations

import bisect
import json
import logging
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from ..features.engine import FeatureEngine
from ..pricing.twap import twap_fair_value
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason, SideView
from .momentum import MomentumConfig, calculate_net_edge, depth_under, exit_cost_estimate, get_executable_price
from .spot_lag import SpotLagConfig, SpotLagEngine, market_implied_fair

log = logging.getLogger("live")

STRATEGY_ID = "repriced_momentum"
ENABLE_ENV = "TPB_REPRICED_MOM_ENABLED"
ALLOW_REAL_ENV = "TPB_REPRICED_MOM_ALLOW_REAL"

#: forward horizons of the continuation table, seconds
CONT_HORIZONS_S: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0)
#: repricing buckets [lo, hi) and spot-momentum buckets (500 ms move, sigma units)
PCT_BUCKETS: tuple[tuple[float, float], ...] = ((0.25, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9),
                                                (0.9, 1.0), (1.0, 1.1), (1.1, 1.25), (1.25, 9.0))
STRENGTH_BUCKETS: tuple[tuple[float, float], ...] = ((-99.0, 0.0), (0.0, 0.5), (0.5, 1.0), (1.0, 99.0))
#: EXPECTED CONTINUATION, cents: the mean change of the side's MID h seconds after a
#: decision point, per (repricing bucket, spot-strength bucket), token agreeing; first
#: qualifying moment per shock, >= 20 per cell. TRAINING period only (09-22 15:30 ->
#: 09-23 18:20 UTC, 256 five-minute epochs, scripts/replay_repriced_momentum.py) and
#: frozen: nothing after it was used. It is a gross move -- the costs come off it.
CONTINUATION_C: dict[tuple[int, int], tuple[float, ...]] = {
    (0, 0): (+0.27, +0.60, +1.20, +1.45, +1.71, +1.71, +1.61, +1.03),
    (0, 1): (+0.32, +0.40, +0.84, +1.04, +1.31, +1.88, +1.54, +1.90),
    (0, 2): (+0.64, +1.05, +1.21, +1.54, +1.03, +0.51, +0.05, +1.03),
    (0, 3): (+2.07, +2.65, +3.85, +4.09, +4.61, +5.08, +4.94, +5.01),
    (1, 0): (+0.14, +0.30, +0.58, +1.40, +1.05, +1.35, +0.94, +0.55),
    (1, 1): (+0.21, +0.28, +0.21, +0.57, +0.60, +0.91, +0.62, +1.36),
    (1, 2): (+0.79, +1.02, +1.51, +1.69, +1.51, +2.62, +2.04, +2.39),
    (1, 3): (+1.35, +1.87, +2.21, +2.23, +2.58, +2.76, +2.70, +2.70),
    (2, 0): (+0.03, +0.09, +0.37, +0.27, +0.21, +0.19, +0.56, +0.21),
    (2, 1): (+0.31, +0.40, +0.56, +0.63, +0.78, +1.06, +0.83, +0.97),
    (2, 2): (+0.08, +0.12, +0.09, +0.42, +0.33, +0.41, +0.02, +0.27),
    (2, 3): (+1.18, +1.45, +1.75, +1.81, +1.74, +1.90, +1.79, +1.75),
    (3, 0): (-0.04, +0.04, +0.15, +0.31, +0.37, +0.66, +0.69, +1.45),
    (3, 1): (+0.11, +0.21, +0.25, +0.40, +0.64, +0.44, +0.75, +0.39),
    (3, 2): (+0.13, +0.28, +0.42, +0.88, +0.57, +0.29, +0.53, +0.51),
    (3, 3): (+0.66, +0.79, +1.00, +1.00, +0.94, +0.94, +1.01, +1.09),
    (4, 0): (+0.00, -0.05, -0.25, -0.18, -0.26, -0.48, -0.36, +0.33),
    (4, 1): (+0.18, +0.25, +0.37, +0.28, +0.33, +0.49, +0.88, +0.94),
    (4, 2): (+0.11, +0.07, +0.34, +0.21, +0.57, +0.79, +0.52, -0.11),
    (4, 3): (+0.60, +0.75, +0.82, +0.88, +0.89, +1.03, +0.85, +0.81),
    (5, 0): (+0.02, +0.03, +0.05, +0.10, +0.09, +0.42, +0.58, +0.67),
    (5, 1): (+0.15, +0.22, +0.40, +0.19, +0.42, +0.91, +1.19, +1.27),
    (5, 2): (+0.16, +0.20, +0.18, +0.30, +0.22, +0.31, +0.71, +0.91),
    (5, 3): (+0.28, +0.26, +0.41, +0.39, +0.29, +0.35, +0.13, +0.14),
    (6, 0): (+0.03, -0.01, -0.27, -0.26, -0.19, -0.42, -0.48, -0.64),
    (6, 1): (-0.00, -0.01, -0.11, -0.28, -0.20, -0.14, +0.27, +0.25),
    (6, 2): (+0.10, +0.05, +0.12, -0.15, -0.39, +0.13, +0.02, +0.19),
    (6, 3): (+0.18, +0.16, +0.05, +0.07, +0.10, +0.24, +0.39, +0.42),
    (7, 0): (-0.18, -0.24, -0.30, -0.58, -0.77, -0.31, -0.15, +0.05),
    (7, 1): (+0.00, -0.06, -0.13, -0.23, -0.42, -0.48, -0.36, -0.43),
    (7, 2): (-0.06, -0.08, -0.10, -0.29, -0.38, -0.16, -0.69, -0.93),
    (7, 3): (+0.03, +0.05, -0.10, -0.31, -0.43, -0.07, -0.25, -0.18),
    (8, 0): (-0.14, -0.39, -0.80, -0.98, -1.11, -1.32, -1.20, -0.97),
    (8, 1): (-0.07, -0.09, -0.17, -0.32, -0.38, -0.58, -0.46, -0.17),
    (8, 2): (-0.20, -0.25, -0.33, -0.56, -0.90, -1.35, -0.96, -0.48),
    (8, 3): (-0.12, -0.24, -0.44, -0.77, -0.87, -0.97, -1.04, -0.84),
}
EXIT_MODES = ("timer", "reversal", "fair", "any")

#: the brief's REJECT_* names for the engine's reasons
REJECT_CODE = {
    Reason.STALE_DATA: "REJECT_STALE_BOOK",
    Reason.REPRICING_INVALID: "REJECT_REPRICING_INVALID",
    Reason.REPRICING_TOO_LOW: "REJECT_REPRICING_TOO_LOW",
    Reason.REPRICING_TOO_HIGH: "REJECT_REPRICING_TOO_HIGH",
    Reason.SPOT_MOMENTUM_WEAK: "REJECT_SPOT_MOMENTUM_WEAK",
    Reason.DIRECTION_MISMATCH: "REJECT_DIRECTION_MISMATCH",
    Reason.OVERSHOOT: "REJECT_OVERSHOOT",
    Reason.EDGE_TOO_SMALL: "REJECT_EDGE_TOO_LOW",
    Reason.RISK_LIMIT: "REJECT_RISK_LIMIT",
    Reason.EXPOSURE_CAP: "REJECT_RISK_LIMIT",
    Reason.MARKET_ENTERED: "REJECT_RISK_LIMIT",
    Reason.MARKET_ATTEMPTS: "REJECT_RISK_LIMIT",
    Reason.HOURLY_BUY_LIMIT: "REJECT_RISK_LIMIT",
    Reason.ALREADY_POSITIONED: "REJECT_RISK_LIMIT",
    Reason.SIZE_ZERO: "REJECT_RISK_LIMIT",
    Reason.NO_OFFER: "REJECT_NO_BOOK",
    Reason.PRICE_BAND: "REJECT_PRICE_BAND",
    Reason.LOW_LIQUIDITY: "REJECT_LIQUIDITY",
    Reason.MODEL_NOT_READY: "REJECT_MODEL_NOT_READY",
    Reason.OUTSIDE_TIME_WINDOW: "REJECT_TIME_WINDOW",
}


def reject_code(reason: Reason | None) -> str:
    if reason is None:
        return "ACCEPT"
    return REJECT_CODE.get(reason, "REJECT_" + reason.value)


# ───────────────────────── the pieces, pure (tested one by one) ─────────────────────────


def repricing(mid_up_before: float | None, mid_up_now: float | None, fair_up_before: float | None,
              fair_up_now: float | None, direction: int,
              min_move: float = 0.01) -> tuple[float | None, float | None, float | None]:
    """(expected, actual, pct) of the shock's token, from Up-token prices.

    The DOWN token is 1 - Up, so its moves are the Up token's with the sign flipped;
    every value comes back normalised so that positive is the shock's direction.
    ``pct`` is None when it cannot be calculated: a price missing, not finite, or an
    expected move under ``min_move`` (a near-zero denominator is not a repricing)."""
    vals = (mid_up_before, mid_up_now, fair_up_before, fair_up_now)
    if any(v is None or not math.isfinite(v) for v in vals):
        return None, None, None
    if any(not (0.0 <= v <= 1.0) for v in vals):                          # an impossible price
        return None, None, None
    s = 1.0 if direction > 0 else -1.0
    expected = s * (fair_up_now - fair_up_before)
    actual = s * (mid_up_now - mid_up_before)
    if expected < min_move:
        return expected, actual, None
    return expected, actual, actual / expected


def side_price(up_price: float, direction: int) -> float:
    """The shock token's price from the Up token's."""
    return up_price if direction > 0 else 1.0 - up_price


def bucket(x: float | None, buckets: tuple[tuple[float, float], ...]) -> int | None:
    if x is None or not math.isfinite(x):
        return None
    for k, (lo, hi) in enumerate(buckets):
        if lo <= x < hi:
            return k
    return None


def expected_continuation(pct: float | None, spot_z500: float | None, horizon_s: float,
                          table: dict = CONTINUATION_C) -> float | None:
    """The TRAINING mean forward move of the token's mid (price units) for this
    repricing and spot strength at the horizon nearest ``horizon_s``; None outside
    the table (too few training cases to say)."""
    k, g = bucket(pct, PCT_BUCKETS), bucket(spot_z500, STRENGTH_BUCKETS)
    if k is None or g is None or (k, g) not in table:
        return None
    h = min(range(len(CONT_HORIZONS_S)), key=lambda i: abs(CONT_HORIZONS_S[i] - horizon_s))
    return table[(k, g)][h] / 100.0


def round_trip_cost(ask: float, bid: float, continuation: float, fee, slippage: float) -> float:
    """What a round trip costs per share: the spread (half in, half out), the taker fee
    on the buy and on the sell (at the expected exit price), and the slippage allowance."""
    mid = (ask + bid) / 2.0
    exit_px = min(max(mid + continuation, 0.01), 0.99)
    return (ask - bid) + fee.taker_fee_per_share(ask) + fee.taker_fee_per_share(exit_px) + slippage


def exit_reason(mode: str, held_s: float, hold_s: float, max_hold_s: float, spot_z: float | None,
                token_move: float | None, bid: float | None, fair: float | None, min_hold_s: float = 0.0,
                entry: float | None = None, take_profit: float = 0.0, stop_loss: float = 0.0) -> str | None:
    """Why to sell now, or None to keep holding. ``spot_z`` and ``token_move`` are the
    position's-direction 500 ms moves (negative = against us).

    ``take_profit`` / ``stop_loss`` (price units, 0 = off) come first, in every mode:
    TAKE_PROFIT once the bid is that far above the entry (the live bot then sells AT the
    bid, and only if the trade clears its minimum net after both fees -- otherwise it
    keeps holding), STOP_LOSS once it is that far below (sold through the bid). In
    ``timer`` mode ``hold_s <= 0`` means no time stop: hold until one of them or the
    market's settlement."""
    # the time limit first: a take-profit the live bot refuses (fees would eat it) must
    # never keep a position open past its fallback
    if mode == "timer":
        if 0.0 < hold_s <= held_s:
            return "TIME_STOP"
    elif held_s >= max_hold_s:
        return "TIME_STOP"
    if entry is not None and bid is not None:
        if take_profit > 0.0 and bid - entry >= take_profit - 1e-9:
            return "TAKE_PROFIT"
        if stop_loss > 0.0 and entry - bid >= stop_loss - 1e-9:
            return "STOP_LOSS"
    if mode == "timer":
        return None
    if held_s < min_hold_s:
        return None
    if mode in ("reversal", "any"):
        if spot_z is not None and spot_z < 0.0:
            return "SPOT_REVERSAL"
        if token_move is not None and token_move < 0.0:
            return "TOKEN_REVERSAL"
    if mode in ("fair", "any") and bid is not None and fair is not None and bid >= fair - 1e-9:
        return "FAIR_REACHED"
    return None


# ─────────────────────────────── config ───────────────────────────────


@dataclass(slots=True)
class RepricedMomentumConfig(SpotLagConfig):
    """Everything spot-lag's config has (the shock rule, data freshness, the portfolio
    gates) under ``TPB_REPRICED_MOM_*``, with the momentum strategy's own budget and
    rules. The defaults are the brief's FIRST configuration (50-90 %, 1c overshoot, 1c
    edge), not a proven one: the research rejected it (docs/repriced_momentum_*.md)."""
    ENV_PREFIX = "TPB_REPRICED_MOM_"
    PROFILES = {"baseline": {}}                 # the spot-lag profiles are not this strategy's
    ENABLE_ENV_NAME = ENABLE_ENV

    # --- the entry zone: how far the token has already repriced ---
    min_pct: float = 0.50
    max_pct: float = 0.90
    #: an expected move smaller than this is no repricing to measure (price units)
    min_expected_move: float = 0.01
    #: a shock's candidates: this long after it
    shock_window_s: float = 5.0
    # --- continuation confirmation ---
    #: the spot's move over this lookback (250 / 500 / 1000 ms), sigma-scaled
    momentum_horizon_ms: float = 500.0
    #: ... must be > 0 and at least this (0: merely still moving our way)
    min_spot_z: float = 0.0
    #: the token's own move over this lookback must be > 0 (direction agreement)
    require_token_agreement: bool = True
    token_horizon_ms: float = 500.0
    # --- price protection ---
    #: the ask may sit at most this far above the fair value, cents
    max_overshoot_cents: float = 1.0
    #: expected continuation - round-trip costs must reach this, cents
    min_edge_cents: float = 1.0
    #: False: log the edge, do not require it (research only)
    edge_gate: bool = True
    #: the FOK limit sits this far above the decision ask, cents; 0 never chases
    limit_offset_cents: float = 0.0
    # --- exit ---
    exit_mode: str = "reversal"
    #: the timer mode's holding time (``hold_s``, inherited, is spot-lag's 5 s); with a
    #: take-profit it is the fallback when the target is never reached; <= 0: no time
    #: stop, the position is held to the target, the stop-loss or settlement
    hold_s: float = 2.0
    #: sell once the bid is this many cents above the entry (0 = off). The live bot sells
    #: AT the bid, and only when the trade also clears its minimum net after both fees
    #: (EngineConfig.take_profit_min_net, 0.5c): a +2c target at mid prices waits for ~+4c
    take_profit_cents: float = 0.0
    #: sell through the bid once it is this many cents below the entry (0 = off)
    stop_loss_cents: float = 0.0
    max_hold_s: float = 8.0
    min_hold_s: float = 0.0
    #: spot-lag's hold extension does not apply here
    extend_max_hold_s: float = 0.0
    # --- the strategy's OWN budget (the global limits still apply on top) ---
    max_simultaneous_positions: int = 1
    max_total_exposure_usdc: float = 5.0
    max_order_usdc: float = 3.0
    max_daily_loss_usdc: float = 5.0
    max_attempts_per_market: int = 3
    #: HYPE was spot-lag's loser; the momentum research included every asset
    exclude_assets: str = ""

    def continuation_horizon_s(self) -> float:
        return self.hold_s if self.exit_mode == "timer" and self.hold_s > 0 else self.max_hold_s

    def momentum_mirror(self) -> MomentumConfig:
        return MomentumConfig(
            profile=STRATEGY_ID, max_quote_age_ms=self.max_quote_age_ms, max_spot_age_ms=self.max_spot_age_ms,
            min_time_remaining_s=self.min_time_remaining_s, max_time_remaining_s=self.max_time_remaining_s,
            min_net_edge=self.min_edge_cents / 100.0, expected_slippage=self.slippage, latency_cost_per_s=0.0,
            max_slippage=self.limit_offset_cents / 100.0, max_spread=self.max_spread, max_ask=self.max_ask,
            max_hold_s=self.hold_s if self.exit_mode == "timer" else self.max_hold_s,
            exit_max_cross=self.exit_max_cross, cooldown_s=0.0, max_trades_per_market=1_000_000)


# ─────────────────────────────── engine ───────────────────────────────


class RepricedMomentumEngine(SpotLagEngine):
    """The spot-lag engine's plumbing (histories, portfolio gates, persistence); the
    repriced-momentum decision and exits."""

    #: the key of this engine's block in ``Evaluation.features`` (the live bot's [SKIP])
    feature_key = STRATEGY_ID
    strategy_id = STRATEGY_ID
    #: the budget (TPB_REPRICED_MOM_MAX_*) is a ceiling: the live bot re-applies it after
    #: every dashboard control change, so no shared setting can loosen it
    budget_is_ceiling = True

    def __init__(self, cfg: EngineConfig, scfg: RepricedMomentumConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the repriced_momentum strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if scfg.exit_mode not in EXIT_MODES:
            raise ValueError(f"exit_mode {scfg.exit_mode!r}: one of {', '.join(EXIT_MODES)}")
        super().__init__(cfg, scfg, risk, features)
        self.scfg: RepricedMomentumConfig = scfg
        #: slug -> the market's active shock (a newer one replaces it)
        self._shock: dict[str, dict] = {}
        self._n_shocks = 0
        #: token -> what the exit needs (the shock's baseline), from the order to the exit
        self._ctx: dict[str, dict] = {}
        #: token -> (t, side mid) seen by the exit, for the token's own 500 ms move
        self._exit_mid: dict[str, deque] = {}
        #: shock id -> the last reject code logged for it (one [MOM] line per change)
        self._logged: dict[str, str] = {}
        self.reject_counts: dict[str, int] = {}
        self.last_mom: dict[str, dict] = {}
        #: every candidate evaluation, one JSON line (set by the live bot, tagged)
        self.candidate_log_path: Path | None = None
        self._cand_fh = None

    # ------------------------------------------------------------ helpers

    def _forget_closed(self, now_ms: float) -> None:
        super()._forget_closed(now_ms)
        for slug in [s for s in self._shock if s not in self._close_ts]:
            sid = self._shock.pop(slug)["id"]
            self._logged.pop(sid, None)

    def _side_mid_at(self, slug: str, t_ms: float, d: int, max_gap_ms: float = 600.0) -> float | None:
        h = self._history_at(slug, t_ms, max_gap_ms)
        return None if h is None else side_price(h[0], d)

    def _spot_z(self, asset: str, now_ms: float, horizon_ms: float, d: int, sigma: float | None) -> float | None:
        hist = self.features.spot.get(asset)
        r = hist.logret(now_ms, horizon_ms / 1000.0) if hist is not None else None
        if r is None or not sigma:
            return None
        return d * r / (sigma * math.sqrt(horizon_ms / 1000.0))

    def _write_candidate(self, rec: dict) -> None:
        if self.candidate_log_path is None:
            return
        try:
            if self._cand_fh is None:
                self.candidate_log_path.parent.mkdir(parents=True, exist_ok=True)
                self._cand_fh = open(self.candidate_log_path, "a", encoding="utf-8")
            self._cand_fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            self._cand_fh.flush()
        except OSError:
            self._cand_fh = None

    def _decide(self, ev: Evaluation, reason: Reason | None, detail: str, cand: dict | None) -> Evaluation:
        """Every candidate ends here: counted, written to the candidate log and, once per
        change of reason within a shock, logged as a [MOM] line."""
        code = reject_code(reason)
        if cand is not None:
            cand.update(decision="BUY" if reason is None else "SKIP", reason=code, detail=detail)
            self.reject_counts[code] = self.reject_counts.get(code, 0) + 1
            self._write_candidate(cand)
            sid = cand["shock_id"]
            if self._logged.get(sid) != code:
                self._logged[sid] = code
                pct = cand.get("repricing_pct")
                log.info("[MOM] %s %s %s: %s%s | repriced %s, spot %s sigma, token %s, ask %s, fair %s, edge %s",
                         cand["market"], cand["direction"], cand["shock_id"].rsplit("#", 1)[-1], code,
                         f" ({detail})" if detail else "", "n/a" if pct is None else f"{pct:.0%}",
                         _f(cand.get("momentum_score"), "+.2f"), _f(cand.get("token_return_500ms"), "+.3f"),
                         _f(cand.get("ask"), ".2f"), _f(cand.get("fair_value"), ".3f"),
                         _f(cand.get("expected_edge"), "+.3f"))
            self.last_mom[ev.asset] = cand
        if reason is None:
            return ev
        return self._reject(ev, reason, detail)

    # --------------------------------------------------------------- entry

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        cfg, sc, m = self.cfg, self.scfg, meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        if not sc.asset_ok(m.asset):
            return self._reject(ev, Reason.ASSET_FILTERED, f"{m.asset} excluded")
        if spot is None or spot_age_ms is None:
            return self._reject(ev, Reason.STALE_DATA, "no spot")
        max_spot = min(cfg.max_spot_age_ms, sc.max_spot_age_ms)
        if spot_age_ms > max_spot:
            return self._reject(ev, Reason.STALE_DATA, f"spot {spot_age_ms:.0f}ms > {max_spot:.0f}")
        if book_age_ms is None or book_age_ms > cfg.max_book_age_ms:
            return self._reject(ev, Reason.STALE_DATA, "book" if book_age_ms is None else f"book {book_age_ms:.0f}ms")
        if spot.n_sources >= 2 and spot.deviation_bps > cfg.max_source_deviation_bps:
            return self._reject(ev, Reason.DATA_INCONSISTENT, f"sources differ {spot.deviation_bps:.1f} bps")
        if m.strike <= 0.0:
            return self._reject(ev, Reason.NO_STRIKE)
        if vol is None or not getattr(vol, "ready", False) or twap is None or not twap.ready:
            return self._reject(ev, Reason.MODEL_NOT_READY)
        sigma = vol.sigma_per_sec
        self.last_sigma[m.asset] = sigma

        # the token mid and the market-implied fair value, recorded every tick
        mid_up = book_up.mid if book_up is not None else None
        if mid_up is None and book_down is not None and book_down.mid is not None:
            mid_up = 1.0 - book_down.mid
        ev.p_market = mid_up
        fv = twap_fair_value(state=twap, strike=m.strike, sigma_per_sec=sigma, now_ms=now,
                             close_ts_ms=m.close_ts, info_lag_s=sc.info_lag_s)
        ev.p_analytic = fv.p_up
        mfv_now = market_implied_fair(fv.p_up, left)
        if mid_up is not None and mfv_now is not None:
            h = self._hist.setdefault(m.slug, deque())
            if not h or now > h[-1][0]:
                h.append((now, mid_up, mfv_now))
            while h and h[0][0] < now - 10_000.0:
                h.popleft()
        self._close_ts[m.slug] = m.close_ts
        self._forget_closed(now)

        if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
        need_ms = latency.round_trip_ms() + cfg.latency_safety_margin_ms
        if left * 1000.0 < need_ms:
            return self._reject(ev, Reason.CANNOT_LAND_IN_TIME)

        # A SHOCK (the spot-lag rule) opens the market's window
        hist = self.features.spot.get(m.asset)
        r1 = hist.logret(now, 1.0) if hist is not None else None
        r3 = hist.logret(now, 3.0) if hist is not None else None
        if r1 is None or r3 is None or sigma <= 0.0:
            return self._reject(ev, Reason.MODEL_NOT_READY, "spot history")
        z1 = r1 / sigma
        z3 = r3 / (sigma * math.sqrt(3.0))
        d_now = 1 if z1 > 0 else -1
        if abs(z1) >= sc.k_sigma and z3 * d_now >= sc.k3_sigma and \
                (not sc.require_acceleration or r1 * d_now > r3 * d_now / 3.0):
            key = (m.slug, d_now)
            last = self._last_signal.get(key)
            if last is None or now - last >= sc.dedupe_s * 1000.0:
                self._last_signal[key] = now
                before = self._history_at(m.slug, now - 1000.0)
                if before is not None:
                    self._n_shocks += 1
                    self._shock[m.slug] = {"id": f"{m.slug}#{self._n_shocks}", "t": now, "d": d_now,
                                           "mid_up_b": before[0], "mfv_up_b": before[1], "z1": z1}
        shock = self._shock.get(m.slug)
        if shock is None or now - shock["t"] > sc.shock_window_s * 1000.0:
            return self._reject(ev, Reason.WEAK_MOMENTUM, "no active shock")

        # A CANDIDATE: every refusal from here on is logged with its reason
        d = shock["d"]
        side = "UP" if d > 0 else "DOWN"
        token = m.yes_token_id if side == "UP" else m.no_token_id
        book = book_up if side == "UP" else book_down
        expected, actual, pct = repricing(shock["mid_up_b"], mid_up, shock["mfv_up_b"], mfv_now, d,
                                          sc.min_expected_move)
        mid_b = side_price(shock["mid_up_b"], d)
        fair = None if expected is None else mid_b + expected
        mid_side = None if mid_up is None else side_price(mid_up, d)
        spot_r = {h: (hist.logret(now, h / 1000.0) if hist is not None else None) for h in (100, 250, 500, 1000, 2000)}
        spot_z = {h: (None if r is None else d * r / (sigma * math.sqrt(h / 1000.0))) for h, r in spot_r.items()}
        tok = {h: (None if mid_side is None or (x := self._side_mid_at(m.slug, now - h, d)) is None else mid_side - x)
               for h in (100, 250, 500, 1000)}
        s_acc = None if spot_r[500] is None or spot_r[1000] is None else \
            d * (spot_r[500] - (spot_r[1000] - spot_r[500])) / sigma
        t_acc = None if tok[500] is None or tok[1000] is None else tok[500] - (tok[1000] - tok[500])
        mom_h = int(sc.momentum_horizon_ms)
        mom_score = spot_z.get(mom_h) if mom_h in spot_z else self._spot_z(m.asset, now, sc.momentum_horizon_ms, d, sigma)
        tok_h = int(sc.token_horizon_ms)
        tok_move = tok.get(tok_h) if tok_h in tok else (
            None if mid_side is None or (x := self._side_mid_at(m.slug, now - tok_h, d)) is None else mid_side - x)
        ask = book.best_ask if book is not None else None
        bid = book.best_bid if book is not None else None
        qa = (quote_ages or {}).get(token) if quote_ages is not None else book_age_ms
        cand = {
            "ts": round(now), "strategy": STRATEGY_ID, "market": m.slug, "asset": m.asset, "direction": side,
            "shock_id": shock["id"], "shock_z1": round(shock["z1"], 3),
            "time_since_shock_s": round((now - shock["t"]) / 1000.0, 3), "seconds_left": round(left, 1),
            "spot_price": getattr(spot, "price", None), "token_price": _r(mid_side), "fair_value": _r(fair),
            "token_price_before": _r(mid_b), "expected_repricing": _r(expected), "actual_repricing": _r(actual),
            "repricing_pct": _r(pct),
            **{f"spot_return_{_hl(h)}": (None if r is None else round(r * 1e4, 3)) for h, r in spot_r.items()},
            **{f"spot_z_{_hl(h)}": _r(z, 3) for h, z in spot_z.items()},
            **{f"token_return_{_hl(h)}": _r(x) for h, x in tok.items()},
            "spot_acceleration": _r(s_acc, 3), "token_acceleration": _r(t_acc),
            "direction_agreement": bool(mom_score is not None and mom_score > 0 and tok_move is not None and tok_move > 0),
            "momentum_score": _r(mom_score, 3), "ask": ask, "bid": bid,
            "spread": None if ask is None or bid is None else round(ask - bid, 4),
            "quote_age_ms": None if qa is None else round(qa), "expected_slippage": sc.slippage,
        }
        ev.features = {STRATEGY_ID: cand,
                       "momentum": {"label": "SHOCK_UP" if d > 0 else "SHOCK_DOWN",
                                    "z": {"main": round(shock["z1"], 3)},
                                    "score": None if mom_score is None else round(mom_score, 3)}}
        ev.spot_move_bps = r1 * 1e4
        ev.market_reprice = None if actual is None else actual * d

        if quote_ages is not None and qa is None:
            return self._decide(ev, Reason.STALE_DATA, f"no valid {side} book on the current feed", cand)
        if qa is not None and qa > sc.max_quote_age_ms:
            return self._decide(ev, Reason.STALE_DATA, f"{side} quote {qa:.0f}ms > {sc.max_quote_age_ms:.0f}", cand)
        if pct is None:
            why = ("missing token price or fair value" if expected is None else
                   f"expected move {expected:+.3f} < {sc.min_expected_move}")
            return self._decide(ev, Reason.REPRICING_INVALID, why, cand)
        if pct < sc.min_pct:
            return self._decide(ev, Reason.REPRICING_TOO_LOW, f"{pct:.0%} < {sc.min_pct:.0%}", cand)
        if pct > sc.max_pct:
            return self._decide(ev, Reason.REPRICING_TOO_HIGH, f"{pct:.0%} > {sc.max_pct:.0%}", cand)
        if mom_score is None or (sc.require_token_agreement and tok_move is None):
            return self._decide(ev, Reason.MODEL_NOT_READY, "no spot or token history for the momentum", cand)
        if mom_score <= 0.0:
            return self._decide(ev, Reason.DIRECTION_MISMATCH, f"spot {mom_score:+.2f} sigma is not moving our way", cand)
        if sc.require_token_agreement and tok_move <= 0.0:
            return self._decide(ev, Reason.DIRECTION_MISMATCH, f"token {tok_move:+.3f} is not following", cand)
        if mom_score < sc.min_spot_z:
            return self._decide(ev, Reason.SPOT_MOMENTUM_WEAK, f"spot {mom_score:+.2f} < {sc.min_spot_z} sigma", cand)

        sv = SideView(side=side, token_id=token, p_model=fair, p_market=mid_side, ask=ask, bid=bid,
                      depth_at_limit=0.0, costs=None, reason=None)
        ev.sides = [sv]
        if ask is None or bid is None:
            sv.reason = Reason.NO_OFFER
            return self._decide(ev, Reason.NO_OFFER, "", cand)
        if not (max(cfg.min_trade_price, sc.min_ask) <= ask <= min(cfg.max_trade_price, sc.max_ask)):
            sv.reason = Reason.PRICE_BAND
            return self._decide(ev, Reason.PRICE_BAND, f"ask {ask:.2f}", cand)
        spread = ask - bid
        if spread > min(sc.max_spread, cfg.max_spread) + 1e-9:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._decide(ev, Reason.LOW_LIQUIDITY, f"spread {spread:.3f}", cand)
        overshoot = ask - fair
        cand["overshoot"] = round(overshoot, 4)
        if overshoot > sc.max_overshoot_cents / 100.0 + 1e-9:
            return self._decide(ev, Reason.OVERSHOOT, f"ask {ask:.2f} is {overshoot * 100:+.1f}c past fair {fair:.3f}",
                                cand)

        # EXPECTED CONTINUATION (training table) against what a round trip costs
        cont = expected_continuation(pct, spot_z.get(500), sc.continuation_horizon_s())
        cand["expected_continuation"] = _r(cont)
        tick = m.tick_size or 0.01
        limit = min(round(ask + sc.limit_offset_cents / 100.0, 4), 1.0 - tick)
        if cont is not None:
            exit_cost = exit_cost_estimate(mid_side + cont, spread, meta.fee)
            cb = calculate_net_edge(mid_side + cont, ask, bid, meta.fee, self.mcfg, 0.0, 0.0, exit_cost)
            sv.costs, sv.entry_px, edge = cb, ask, cb.net_edge
            cand["round_trip_cost"] = round(round_trip_cost(ask, bid, cont, meta.fee, sc.slippage), 4)
            cand["expected_edge"] = round(edge, 4)
        else:
            edge = None
        if sc.edge_gate:
            if edge is None:
                return self._decide(ev, Reason.EDGE_TOO_SMALL, "no continuation estimate for this cell", cand)
            if edge < sc.min_edge_cents / 100.0 - 1e-9:
                return self._decide(ev, Reason.EDGE_TOO_SMALL,
                                    f"continuation {cont * 100:+.2f}c - costs {cand['round_trip_cost'] * 100:.2f}c = "
                                    f"{edge * 100:+.2f}c < {sc.min_edge_cents:g}c", cand)
        # the strategy's gates passed: from here a refusal is a skipped opportunity
        cand["signal"] = True

        # ONE MARKET = ONE OPPORTUNITY; the budgets below are global (spot-lag's gates)
        if self.trades.get(m.slug, 0) >= int(sc.max_entries_per_market):
            return self._decide(ev, Reason.MARKET_ENTERED, "already entered this market", cand)
        if self.attempts.get(m.slug, 0) >= int(sc.max_attempts_per_market):
            return self._decide(ev, Reason.MARKET_ATTEMPTS, f"{self.attempts[m.slug]} buy attempts in this market",
                                cand)
        if self.buys_last_hour(now) >= int(sc.max_buys_per_hour):
            return self._decide(ev, Reason.HOURLY_BUY_LIMIT, f"{len(self._buy_times)} buys in the last hour", cand)
        venue_min = float(m.min_size) if m.min_size else float(sc.size_shares)
        min_notional = getattr(self.risk.cfg, "micro_min_notional_usdc", 1.0) or 0.0
        venue_min = max(venue_min, math.ceil(min_notional / max(limit, 0.01) * 100.0) / 100.0)
        size = max(venue_min, float(sc.size_shares)) if sc.order_size_usdc <= 0.0 else \
            max(venue_min, math.floor(sc.order_size_usdc / max(limit, 0.01) * 100.0) / 100.0)
        per_order = min(sc.max_order_usdc, self.risk.cfg.max_position_usdc)
        room = self.risk.capacity_total()
        bound = min(per_order, room)
        if size * limit > bound + 1e-9:
            fit = math.floor(bound / limit * 100.0) / 100.0
            if fit + 1e-9 < venue_min:
                if room < per_order:
                    return self._decide(ev, Reason.EXPOSURE_CAP, f"{room:.2f} USDC of the exposure budget left", cand)
                return self._decide(ev, Reason.RISK_LIMIT, f"the minimum order {venue_min * limit:.2f} USDC exceeds "
                                                           f"the order cap {per_order:.2f}", cand)
            size = fit
        depth = depth_under(book, limit)
        sv.depth_at_limit = depth
        exec_price, avail = get_executable_price(book, size, limit)
        if exec_price is None:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._decide(ev, Reason.LOW_LIQUIDITY, f"{avail:.0f} shares under {limit:.2f}, need {size:.0f}", cand)
        epoch = int(m.open_ts // 1000)
        verdict = self.risk.check(m.slug, m.asset, epoch, size * limit)
        if not verdict.ok:
            return self._decide(ev, Reason(verdict.reason), verdict.detail, cand)
        if size * limit > balance + 1e-9:
            return self._decide(ev, Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * limit:.2f}", cand)
        cand.update(limit_price=limit, size=size)
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, limit, exec_price
        ev.max_entry_price, ev.p_used = limit, fair
        ev.size, ev.net_edge, ev.confidence = size, (edge if edge is not None else 0.0), 1.0
        ev.size_note = f"{size:g} shares ({size * limit:.2f} USDC worst case, {room:.2f} of budget left)"
        self.last[m.slug] = ev
        self._ctx[token] = {"slug": m.slug, "d": d, "mid_b": mid_b, "mfv_up_b": shock["mfv_up_b"],
                            "shock_id": shock["id"], "fair": fair, "decision_ask": ask, "decision_bid": bid,
                            "limit": limit, "t_decision": now}
        return self._decide(ev, None, "", cand)

    def revalidate(self, ev: Evaluation, book, min_edge: float | None = None) -> Reason | None:
        """The ask must still be inside the limit (never chase); the edge must survive
        it only when the edge gate is on."""
        sc = self.scfg
        return super().revalidate(ev, book, min_edge=(sc.min_edge_cents / 100.0) if sc.edge_gate else -math.inf)

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        """A limit at the decision ask, fill-or-kill: never a market order."""
        c = (ev.features or {}).get(STRATEGY_ID, {})
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                     tif=TimeInForce.FOK, expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                     tag=f"{ev.side} {STRATEGY_ID} repriced={c.get('repricing_pct', 0) or 0:.2f} "
                         f"spot_z={c.get('momentum_score', 0) or 0:+.2f} edge={ev.net_edge:+.3f} "
                         f"limit={ev.limit_price:.2f}")

    def signal_text(self, ev: Evaluation) -> str:
        """The live bot's [SIGNAL] line for this strategy."""
        c = (ev.features or {}).get(STRATEGY_ID, {})
        return (f"repriced-momentum candidate: {c.get('repricing_pct') or 0:.0%} repriced, spot "
                f"{c.get('momentum_score') or 0:+.2f} sigma, token {c.get('token_return_500ms') or 0:+.3f}, ask "
                f"{c.get('ask') or 0:.2f} vs fair {c.get('fair_value') or 0:.3f}, expected edge "
                f"{c.get('expected_edge') if c.get('expected_edge') is not None else float('nan'):+.3f}")

    # ---------------------------------------------------------------- exits

    def on_entry_fill(self, slug: str, token_id: str, now_ms: float) -> None:
        super().on_entry_fill(slug, token_id, now_ms)
        ctx = self._ctx.get(token_id)
        if ctx is not None:
            ctx["t_fill"] = now_ms
            log.info("[MOM] %s %s filled %.0f ms after the decision (limit %.2f)", slug,
                     "UP" if ctx["d"] > 0 else "DOWN", now_ms - ctx["t_decision"], ctx["limit"])

    def on_exit(self, slug: str, token_id: str, now_ms: float) -> None:
        opened = self.entry_ms.get(token_id)
        super().on_exit(slug, token_id, now_ms)
        ctx = self._ctx.pop(token_id, None)
        self._exit_mid.pop(token_id, None)
        if opened is not None:
            log.info("[MOM] %s exit sent after %.1f s held (%s)", slug, (now_ms - opened) / 1000.0,
                     (ctx or {}).get("exit_kind", "?"))

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig=None, spread: float | None = None, bid_depth: float | None = None,
                      size: float | None = None, data_age_ms: float | None = None) -> str | None:
        """``exit_mode``: timer / reversal / fair / any (module docstring). A position
        with no recorded entry time (e.g. inherited across a restart) is past its
        few-second thesis by definition, so it is closed at once."""
        sc = self.scfg
        opened = self.entry_ms.get(token_id)
        if opened is None:
            return "TIME_STOP"
        held = (now_ms - opened) / 1000.0
        want = 1 if side == "UP" else -1
        ctx = self._ctx.get(token_id)
        mid = None if bid is None else bid + max(0.0, spread or 0.0) / 2.0
        h = self._exit_mid.setdefault(token_id, deque())
        if mid is not None and (not h or now_ms > h[-1][0]):
            h.append((now_ms, mid))
        while h and h[0][0] < now_ms - 5_000.0:
            h.popleft()
        token_move = None
        if mid is not None:
            ts = [x[0] for x in h]
            k = bisect.bisect_right(ts, now_ms - 500.0) - 1
            if k >= 0 and now_ms - 500.0 - ts[k] <= 600.0:
                token_move = mid - h[k][1]
            elif ctx is not None:
                past = self._side_mid_at(ctx["slug"], now_ms - 500.0, want)
                token_move = None if past is None else mid - past
        fair = None
        if ctx is not None:
            hh = self._hist.get(ctx["slug"])
            if hh and now_ms - hh[-1][0] <= 1000.0:
                fair = ctx["mid_b"] + (side_price(hh[-1][2], want) - side_price(ctx["mfv_up_b"], want))
        kind = exit_reason(sc.exit_mode, held, sc.hold_s, sc.max_hold_s,
                           self._spot_z(asset, now_ms, 500.0, want, self.last_sigma.get(asset)),
                           token_move, bid, fair, sc.min_hold_s, entry=entry,
                           take_profit=sc.take_profit_cents / 100.0, stop_loss=sc.stop_loss_cents / 100.0)
        if kind is not None and ctx is not None:
            ctx["exit_kind"] = kind
        return kind

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = STRATEGY_ID
        snap.pop("spot_lag_config", None)
        snap.pop("spot_lag", None)
        snap["repriced_momentum_config"] = self.scfg.as_dict()
        snap["repriced_momentum"] = dict(self.last_mom)
        snap["repriced_momentum_rejects"] = dict(self.reject_counts)
        snap["active_shocks"] = {s: {"id": v["id"], "direction": "UP" if v["d"] > 0 else "DOWN"}
                                 for s, v in self._shock.items()}
        return snap


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


def _f(x, spec: str) -> str:
    return "n/a" if x is None else format(x, spec)


def _hl(ms: int) -> str:
    return f"{ms}ms" if ms < 1000 else f"{ms // 1000}s"
