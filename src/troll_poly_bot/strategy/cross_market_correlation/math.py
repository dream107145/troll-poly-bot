"""Payoff tables, EV equations, and guaranteed-arbitrage proof for pair trades.

For one matched share of candidate 1 (buy A UP + B DOWN):

| A \\ B | UP | DOWN |
|-------|----|------|
| UP    | $1 | $2   |
| DOWN  | $0 | $1   |

E[payout] = 1 + qUD - qDU
EV1 = 1 + qUD - qDU - C1

Candidate 2 (buy A DOWN + B UP) swaps qUD/qDU.

Guaranteed arbitrage requires min(P_s - C) > 0 over every valid settlement
scenario. High historical agreement is never treated as a guarantee.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

# Reuse CMA's executable-cost helpers (fee probes, ask-ladder walk).
from ..cross_market_arb.math import (  # noqa: F401
    FeeFn, LegQuote, PlannedLeg, PositionLeg, estimate_leg, planned_to_positions,
    round_shares, unit_all_in, walk_ask_cost,
)

Label = str  # "GUARANTEED_ARBITRAGE" | "STATISTICAL_CANDIDATE" | "REJECTED"
Direction = str  # "A_UP_B_DOWN" | "A_DOWN_B_UP"


@dataclass(frozen=True, slots=True)
class JointProbs:
    """P(A_outcome, B_outcome) for synchronized markets. Must sum to 1."""
    p_uu: float
    p_ud: float
    p_du: float
    p_dd: float
    n: int = 0
    uncertainty: float = 0.0  # e.g. half-width of CI on disagreement rates

    def normalized(self) -> "JointProbs":
        s = self.p_uu + self.p_ud + self.p_du + self.p_dd
        if s <= 0:
            return JointProbs(0.25, 0.25, 0.25, 0.25, self.n, 1.0)
        return JointProbs(
            self.p_uu / s, self.p_ud / s, self.p_du / s, self.p_dd / s,
            self.n, self.uncertainty,
        )

    @property
    def agreement_rate(self) -> float:
        return self.p_uu + self.p_dd

    @property
    def q_ud(self) -> float:
        return self.p_ud

    @property
    def q_du(self) -> float:
        return self.p_du


@dataclass(slots=True)
class PairPayoff:
    """Settlement payouts for each of the four binary outcomes (per matched share)."""
    uu: float
    ud: float
    du: float
    dd: float

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.uu, self.ud, self.du, self.dd)

    def min_payout(self) -> float:
        return min(self.as_tuple())

    def max_payout(self) -> float:
        return max(self.as_tuple())


def payoff_a_up_b_down(shares: float = 1.0) -> PairPayoff:
    """Candidate 1: long A UP + B DOWN."""
    q = float(shares)
    return PairPayoff(uu=q, ud=2.0 * q, du=0.0, dd=q)


def payoff_a_down_b_up(shares: float = 1.0) -> PairPayoff:
    """Candidate 2: long A DOWN + B UP."""
    q = float(shares)
    return PairPayoff(uu=q, ud=0.0, du=2.0 * q, dd=q)


def expected_payout(payoff: PairPayoff, probs: JointProbs) -> float:
    p = probs.normalized()
    return (payoff.uu * p.p_uu + payoff.ud * p.p_ud
            + payoff.du * p.p_du + payoff.dd * p.p_dd)


def expected_gross_payout_c1(q_ud: float, q_du: float) -> float:
    """E[payout] for candidate 1 (A UP + B DOWN) per matched share."""
    return 1.0 + float(q_ud) - float(q_du)


def expected_gross_payout_c2(q_ud: float, q_du: float) -> float:
    """E[payout] for candidate 2 (A DOWN + B UP) per matched share."""
    return 1.0 + float(q_du) - float(q_ud)


def ev_c1(q_ud: float, q_du: float, cost: float) -> float:
    return expected_gross_payout_c1(q_ud, q_du) - float(cost)


def ev_c2(q_ud: float, q_du: float, cost: float) -> float:
    return expected_gross_payout_c2(q_ud, q_du) - float(cost)


def scenario_pnls(payoff: PairPayoff, cost: float) -> list[float]:
    return [p - float(cost) for p in payoff.as_tuple()]


def worst_case_net(payoff: PairPayoff, cost: float) -> float:
    """min(P_s - C) over the four settlement scenarios."""
    return min(scenario_pnls(payoff, cost))


def is_guaranteed_arbitrage(payoff: PairPayoff, cost: float,
                            min_net: float = 0.0) -> bool:
    """Formal worst-case test. Historical correlation never qualifies alone."""
    return worst_case_net(payoff, cost) > float(min_net) + 1e-12


@dataclass(slots=True)
class PairCandidateMetrics:
    direction: Direction
    shares: float
    cost_per_pair: float  # all-in for one matched share
    total_cost: float
    payoff: PairPayoff
    probs: JointProbs
    expected_payout: float
    ev: float
    conservative_ev: float
    worst_case_net: float
    label: Label
    reject_reason: str = ""
    fill_prob_est: float = 1.0
    leg_a_unit: float = 0.0
    leg_b_unit: float = 0.0


def classify_candidate(
    direction: Direction,
    shares: float,
    cost_per_pair: float,
    probs: JointProbs,
    *,
    guaranteed_mode: bool,
    statistical_mode: bool,
    guaranteed_min_net: float,
    minimum_conservative_ev: float,
    uncertainty_z: float,
    max_uncertainty: float,
    min_sample_size: int,
    minimum_expected_net_profit: float,
    fill_prob_est: float = 1.0,
    leg_a_unit: float = 0.0,
    leg_b_unit: float = 0.0,
    allow_under_sampled_prior: bool = False,
    maximum_combined_cost: float = 0.98,
    minimum_combined_cost: float = 0.60,
) -> PairCandidateMetrics:
    """Score one direction; attach GUARANTEED / STATISTICAL / REJECTED label."""
    payoff = (payoff_a_up_b_down(shares) if direction == "A_UP_B_DOWN"
              else payoff_a_down_b_up(shares))
    total_cost = float(cost_per_pair) * float(shares)
    # Per-share EV uses unit cost; scale EV to position via shares for sizing gates.
    p = probs.normalized()
    if direction == "A_UP_B_DOWN":
        unit_ev = ev_c1(p.q_ud, p.q_du, cost_per_pair)
        unit_payout = expected_gross_payout_c1(p.q_ud, p.q_du)
    else:
        unit_ev = ev_c2(p.q_ud, p.q_du, cost_per_pair)
        unit_payout = expected_gross_payout_c2(p.q_ud, p.q_du)
    pos_ev = unit_ev * shares
    wc = worst_case_net(payoff, total_cost)
    under_sampled = p.n < int(min_sample_size)
    # When trading on the prior (paper bootstrap), do not let the empty-sample
    # uncertainty band wipe a clear cost edge: haircut uses a capped uncertainty.
    unc_for_cons = float(p.uncertainty)
    if allow_under_sampled_prior and under_sampled:
        unc_for_cons = min(unc_for_cons, 0.15)
    cons = pos_ev - float(uncertainty_z) * unc_for_cons * shares
    label: Label = "REJECTED"
    reason = ""

    if cost_per_pair < float(minimum_combined_cost) - 1e-12:
        reason = f"combined_cost:{cost_per_pair:.4f}<{minimum_combined_cost}"
    elif cost_per_pair > float(maximum_combined_cost) + 1e-12:
        reason = f"combined_cost:{cost_per_pair:.4f}>{maximum_combined_cost}"
    elif guaranteed_mode and is_guaranteed_arbitrage(payoff, total_cost, guaranteed_min_net):
        label = "GUARANTEED_ARBITRAGE"
    elif not statistical_mode:
        reason = "statistical_mode_disabled"
        if guaranteed_mode and not is_guaranteed_arbitrage(payoff, total_cost, guaranteed_min_net):
            reason = "not_guaranteed_worst_case"
    elif under_sampled and not allow_under_sampled_prior:
        reason = f"insufficient_samples:{p.n}<{min_sample_size}"
    elif (not (allow_under_sampled_prior and under_sampled)
          and p.uncertainty > float(max_uncertainty) + 1e-12):
        reason = f"uncertainty:{p.uncertainty:.3f}>{max_uncertainty}"
    elif cons < float(minimum_conservative_ev) - 1e-12:
        reason = f"conservative_ev:{cons:.4f}<{minimum_conservative_ev}"
    elif pos_ev * fill_prob_est < float(minimum_expected_net_profit) - 1e-12:
        reason = f"expected_profit:{pos_ev * fill_prob_est:.4f}"
    else:
        label = "STATISTICAL_CANDIDATE"

    # Never upgrade correlation to guaranteed without the formal test.
    if label == "GUARANTEED_ARBITRAGE" and not is_guaranteed_arbitrage(
            payoff, total_cost, guaranteed_min_net):
        label = "REJECTED"
        reason = "guarantee_recheck_failed"

    return PairCandidateMetrics(
        direction=direction, shares=shares, cost_per_pair=cost_per_pair,
        total_cost=total_cost, payoff=payoff, probs=p,
        expected_payout=unit_payout * shares, ev=pos_ev, conservative_ev=cons,
        worst_case_net=wc, label=label, reject_reason=reason,
        fill_prob_est=fill_prob_est, leg_a_unit=leg_a_unit, leg_b_unit=leg_b_unit,
    )


def normalize_probs(p_uu: float, p_ud: float, p_du: float, p_dd: float) -> tuple[float, float, float, float]:
    s = p_uu + p_ud + p_du + p_dd
    if s <= 0:
        return (0.25, 0.25, 0.25, 0.25)
    return (p_uu / s, p_ud / s, p_du / s, p_dd / s)


def wilson_halfwidth(k: int, n: int, z: float = 1.96) -> float:
    """Approximate CI half-width for a binomial rate (used as uncertainty)."""
    if n <= 0:
        return 1.0
    phat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (phat + z2 / (2 * n)) / denom
    rad = (z / denom) * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))
    lo = max(0.0, centre - rad)
    hi = min(1.0, centre + rad)
    return 0.5 * (hi - lo)
