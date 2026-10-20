"""Strike-relative predictor for opposite-outcome CMC pairs.

For direction ``A_UP_B_DOWN`` (buy A-UP and B-DOWN), settlement pays
``I(A finishes UP) + I(B finishes DOWN)`` ∈ {0, 1, 2}.

Each market finishes UP iff final oracle spot ≥ strike. Live, we approximate
that with signed distance to strike:

    bps = 10_000 * (spot - strike) / strike

Prediction:
    A_up   ≈ (bps_a ≥ 0)
    B_down ≈ (bps_b < 0)
    payout ≈ I(A_up) + I(B_down)

When both assets sit on the **same** side of strike, payout is usually 1
(small win if combined cost < 1). Opposite sides → 0 or 2 (wipe vs jackpot).

``min_abs_bps`` filters knife-edge prints near the strike (higher = fewer,
cleaner predictions).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Direction = Literal["A_UP_B_DOWN", "A_DOWN_B_UP"]


def signed_bps(spot: float, strike: float) -> float | None:
    """Basis points of spot above (+) / below (−) strike. None if unusable."""
    try:
        s = float(spot)
        k = float(strike)
    except (TypeError, ValueError):
        return None
    if not (k > 0) or s != s or k != k:  # NaN guard
        return None
    return 10_000.0 * (s - k) / k


@dataclass(frozen=True, slots=True)
class PairPrediction:
    direction: Direction
    """Predicted settlement payout in shares (0, 1, or 2)."""
    predicted_payout: int
    """True when both legs clear ``min_abs_bps`` away from strike."""
    confident: bool
    bps_a: float
    bps_b: float
    """Same side of strike ⇒ agreement regime (expect payout 1)."""
    same_side_of_strike: bool
    """Human-readable regime for gates / logs."""
    regime: Literal["agreement", "favor_direction", "against_direction", "near_strike"]

    @property
    def expect_small_win(self) -> bool:
        return self.confident and self.predicted_payout == 1

    @property
    def expect_wipe(self) -> bool:
        return self.confident and self.predicted_payout == 0

    @property
    def expect_jackpot(self) -> bool:
        return self.confident and self.predicted_payout == 2


def predict_opposite_pair(
    spot_a: float,
    strike_a: float,
    spot_b: float,
    strike_b: float,
    *,
    direction: Direction = "A_UP_B_DOWN",
    min_abs_bps: float = 0.0,
) -> PairPrediction | None:
    """Predict payout for an opposite-outcome pair from live spots and strikes.

    Returns ``None`` when either strike/spot is missing or invalid.
    """
    bps_a = signed_bps(spot_a, strike_a)
    bps_b = signed_bps(spot_b, strike_b)
    if bps_a is None or bps_b is None:
        return None

    a_up = bps_a >= 0.0
    b_up = bps_b >= 0.0
    if direction == "A_UP_B_DOWN":
        payout = (1 if a_up else 0) + (0 if b_up else 1)
    else:
        payout = (0 if a_up else 1) + (1 if b_up else 0)

    abs_a, abs_b = abs(bps_a), abs(bps_b)
    confident = abs_a >= float(min_abs_bps) and abs_b >= float(min_abs_bps)
    same_side = a_up == b_up

    if not confident:
        regime: Literal["agreement", "favor_direction", "against_direction", "near_strike"] = (
            "near_strike"
        )
    elif same_side:
        regime = "agreement"
    elif payout == 2:
        regime = "favor_direction"
    else:
        regime = "against_direction"

    return PairPrediction(
        direction=direction,
        predicted_payout=int(payout),
        confident=confident,
        bps_a=float(bps_a),
        bps_b=float(bps_b),
        same_side_of_strike=same_side,
        regime=regime,
    )


def actual_payout(a_up: bool, b_up: bool, direction: Direction = "A_UP_B_DOWN") -> int:
    """Settlement payout for the opposite pair given realized outcomes."""
    if direction == "A_UP_B_DOWN":
        return (1 if a_up else 0) + (0 if b_up else 1)
    return (0 if a_up else 1) + (1 if b_up else 0)


def trade_label(payout: int, combined_cost: float) -> Literal["win", "lose", "flat"]:
    """Mark-to-settlement label for a fully filled opposite pair (ignore fees)."""
    pnl = float(payout) - float(combined_cost)
    if abs(pnl) < 1e-12:
        return "flat"
    return "win" if pnl > 0 else "lose"


# ---------------------------------------------------------------------------
# Calibrated big-loss (wipe) risk — fit on charts_index 70/30 time split
# (train epochs < 2026-10 cut). Features: against-strike geometry, strength,
# alt/HYPE flags, UP-book wipe alignment, empirical pair wipe prior.
# ---------------------------------------------------------------------------

_ALT = frozenset({"HYPE", "BNB", "DOGE"})

#: Logistic weights for features
#: [against, against_str, log1p(against_str), n_alt, hype, book_wipe, pair_rate]
_WIPE_WEIGHTS: tuple[float, ...] = (
    2.189023, 0.056568, 0.786943, 0.121081, -0.069034, 2.350823, 4.120887,
)
_WIPE_BIAS: float = -4.128675

#: Laplace-smoothed wipe rate per unordered pair from training window.
_PAIR_WIPE_PRIOR: dict[str, float] = {
    "BNB|BTC": 0.08530, "BNB|DOGE": 0.09310, "BNB|ETH": 0.07621,
    "BNB|HYPE": 0.12477, "BNB|SOL": 0.09132, "BNB|XRP": 0.09989,
    "BTC|DOGE": 0.08809, "BTC|ETH": 0.05230, "BTC|HYPE": 0.11806,
    "BTC|SOL": 0.07433, "BTC|XRP": 0.08484, "DOGE|ETH": 0.07933,
    "DOGE|HYPE": 0.12465, "DOGE|SOL": 0.07715, "DOGE|XRP": 0.07472,
    "ETH|HYPE": 0.11454, "ETH|SOL": 0.06250, "ETH|XRP": 0.07701,
    "HYPE|SOL": 0.12160, "HYPE|XRP": 0.12546, "SOL|XRP": 0.07634,
}

#: Default skip threshold on p_wipe (held-out: ~67% prec, ~26% recall, F1~0.38
#: vs baseline against&str>=3 at F1~0.30). Use 0.45 with require_against for
#: higher recall (F1~0.46, prec~60%).
DEFAULT_WIPE_SKIP_THRESHOLD: float = 0.55


def _pair_key(asset_a: str, asset_b: str) -> str:
    a, b = sorted((asset_a.strip().upper(), asset_b.strip().upper()))
    return f"{a}|{b}"


def _sigmoid(z: float) -> float:
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


@dataclass(frozen=True, slots=True)
class WipeRisk:
    """Calibrated probability that the chosen opposite direction fully wipes."""

    direction: Direction
    asset_a: str
    asset_b: str
    p_wipe: float
    against_direction: bool
    against_strength_bps: float
    book_wipe_score: float
    pair_prior: float
    bps_a: float
    bps_b: float

    def should_skip(self, threshold: float = DEFAULT_WIPE_SKIP_THRESHOLD) -> bool:
        """True when wipe probability clears the skip threshold."""
        return self.p_wipe >= float(threshold)


def predict_wipe_risk(
    spot_a: float,
    strike_a: float,
    spot_b: float,
    strike_b: float,
    *,
    asset_a: str = "A",
    asset_b: str = "B",
    direction: Direction = "A_UP_B_DOWN",
    ask_up_a: float | None = None,
    ask_up_b: float | None = None,
) -> WipeRisk | None:
    """Estimate P(payout=0) for an opposite pair from spots, strikes, optional books.

    ``ask_up_*`` are optional UP-token asks (0–1). When present they form a
    book-alignment feature: high when the book already prices the wipe side
    (A looking DOWN and B looking UP for ``A_UP_B_DOWN``).
    """
    bps_a = signed_bps(spot_a, strike_a)
    bps_b = signed_bps(spot_b, strike_b)
    if bps_a is None or bps_b is None:
        return None

    a_name = asset_a.strip().upper()
    b_name = asset_b.strip().upper()

    if direction == "A_UP_B_DOWN":
        # Wipe = A DOWN + B UP ⇒ need A below strike, B above.
        against = bps_a < 0.0 and bps_b > 0.0
        against_str = min(-bps_a, bps_b) if against else 0.0
        if ask_up_a is not None and ask_up_b is not None:
            ua, ub = float(ask_up_a), float(ask_up_b)
            book = max(0.0, min(1.0, (1.0 - ua) * ub))
        else:
            book = 0.0
    else:
        # A_DOWN_B_UP wipe = A UP + B DOWN.
        against = bps_a >= 0.0 and bps_b < 0.0
        against_str = min(bps_a, -bps_b) if against else 0.0
        if ask_up_a is not None and ask_up_b is not None:
            ua, ub = float(ask_up_a), float(ask_up_b)
            book = max(0.0, min(1.0, ua * (1.0 - ub)))
        else:
            book = 0.0

    n_alt = (1 if a_name in _ALT else 0) + (1 if b_name in _ALT else 0)
    hype = 1.0 if (a_name == "HYPE" or b_name == "HYPE") else 0.0
    pair_prior = float(_PAIR_WIPE_PRIOR.get(_pair_key(a_name, b_name), 0.09))

    feats = (
        1.0 if against else 0.0,
        float(against_str),
        math.log1p(float(against_str)),
        float(n_alt),
        hype,
        float(book),
        pair_prior,
    )
    z = _WIPE_BIAS + sum(w * x for w, x in zip(_WIPE_WEIGHTS, feats))
    p = _sigmoid(z)

    return WipeRisk(
        direction=direction,
        asset_a=a_name,
        asset_b=b_name,
        p_wipe=float(p),
        against_direction=bool(against),
        against_strength_bps=float(against_str),
        book_wipe_score=float(book),
        pair_prior=pair_prior,
        bps_a=float(bps_a),
        bps_b=float(bps_b),
    )
