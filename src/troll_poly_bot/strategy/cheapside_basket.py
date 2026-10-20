"""Pure helpers for the Cheapside seven-market basket: sizing, settlement scenarios,
preflight math, and state labels. Kept separate from CheapSideEngine so unit tests can
exercise the mathematics without a live book feed.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence


class BasketState(str, Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    ARMED = "ARMED"
    EXECUTING_CHEAP_LEGS = "EXECUTING_CHEAP_LEGS"
    EXECUTING_LEADER = "EXECUTING_LEADER"
    COMPLETE = "COMPLETE"
    PREFLIGHT_REJECT = "PREFLIGHT_REJECT"
    ABORTED = "ABORTED"
    ORPHAN = "ORPHAN"


# Structured log event names (also surfaced on last_cheapside / dashboard).
EVT_PREFLIGHT_PASS = "PREFLIGHT_PASS"
EVT_PREFLIGHT_REJECT = "PREFLIGHT_REJECT"
EVT_BASKET_ARMED = "BASKET_ARMED"
EVT_CHEAP_LEG_FILL = "CHEAP_LEG_FILL"
EVT_CHEAP_LEG_PARTIAL = "CHEAP_LEG_PARTIAL"
EVT_CHEAP_LEG_FAILURE = "CHEAP_LEG_FAILURE"
EVT_HEDGE_REPRICE = "HEDGE_REPRICE"
EVT_HEDGE_ABORT = "HEDGE_ABORT"
EVT_LEADER_ORDER = "LEADER_ORDER"
EVT_LEADER_FILL = "LEADER_FILL"
EVT_BASKET_COMPLETE = "BASKET_COMPLETE"
EVT_BASKET_ABORT = "BASKET_ABORT"
EVT_ORPHAN_EXPOSURE = "ORPHAN_EXPOSURE"
EVT_RISK_LIMIT_REJECT = "RISK_LIMIT_REJECT"


@dataclass(frozen=True, slots=True)
class LegQuote:
    """Executable quote snapshot used for preflight / sizing."""
    slug: str
    asset: str
    side: str
    ask: float
    bid: float
    depth: float
    fee_per_share_at_limit: float
    tick: float = 0.01
    min_size: float = 5.0
    quote_age_ms: float = 0.0
    spread: float = 0.0


@dataclass(frozen=True, slots=True)
class PositionLeg:
    slug: str
    asset: str
    side: str
    shares: float
    cost: float  # inclusive of fees


@dataclass(slots=True)
class PreflightResult:
    ok: bool
    reason: str = ""
    event: str = EVT_PREFLIGHT_PASS
    cheap_cost_est: float = 0.0
    leader_unit: float = 0.0
    leader_shares: float = 0.0
    leader_notional: float = 0.0
    total_reserved: float = 0.0
    projected_protected_pnl: float = 0.0
    all_lose_pnl: float = 0.0
    details: dict = field(default_factory=dict)


def round_shares_up(shares: float, step: float = 0.01) -> float:
    if shares <= 0.0:
        return 0.0
    return math.ceil(shares / step - 1e-12) * step


def unit_cost(ask: float, limit: float, fee_fn) -> float:
    """Worst all-in unit cost between visible ask and limit (price-dependent fees)."""
    probes = [ask, limit]
    if ask <= 0.5 <= limit:
        probes.append(0.5)
    return limit + max(float(fee_fn(px)) for px in probes)


def estimate_leg_cost(ask: float, shares: float, slippage: float, fee_fn,
                      tick: float = 0.01) -> tuple[float, float, float]:
    """Return (limit, unit_all_in, total_cost) for a planned buy."""
    limit = min(1.0 - tick, round(ask + slippage, 4))
    unit = unit_cost(ask, limit, fee_fn)
    return limit, unit, unit * shares


def leader_shares_for_target(cheap_cost: float, leader_unit: float, target_pnl: float,
                             already_shares: float = 0.0, already_cost: float = 0.0,
                             venue_min: float = 5.0, dust: float = 0.01) -> tuple[float, float]:
    """Size leader so protected scenario ≈ target. Returns (need, projected_pnl_after)."""
    if leader_unit >= 1.0 - 1e-12:
        return 0.0, already_shares - cheap_cost - already_cost
    pnl_before = already_shares - cheap_cost - already_cost
    raw = max(0.0, target_pnl - pnl_before) / (1.0 - leader_unit)
    need = round_shares_up(raw)
    if dust < need < venue_min:
        need = venue_min
    pnl_after = pnl_before + need * (1.0 - leader_unit)
    return need, pnl_after


def settlement_pnl(legs: Sequence[PositionLeg], winners: dict[str, str]) -> float:
    """Net settlement PnL given a map slug -> winning side ('UP'/'DOWN')."""
    payout = 0.0
    cost = 0.0
    for leg in legs:
        cost += float(leg.cost)
        if winners.get(leg.slug) == leg.side:
            payout += float(leg.shares)
    return payout - cost


def all_outcome_scenarios(legs: Sequence[PositionLeg],
                          market_slugs: Sequence[str]) -> list[dict]:
    """Enumerate all 2^N outcome combinations for the given markets.

    Each row: winners map, pnl, protected (favorite-wins / all-cheap-lose flag is
    computed by the caller via tags).
    """
    slugs = list(market_slugs)
    out = []
    for bits in itertools.product(("UP", "DOWN"), repeat=len(slugs)):
        winners = dict(zip(slugs, bits))
        out.append({"winners": winners, "pnl": settlement_pnl(legs, winners)})
    return out


def classify_scenarios(scenarios: Iterable[dict], cheap_slugs: Sequence[str],
                       leader_slug: str, leader_side: str,
                       cheap_sides: dict[str, str]) -> dict:
    """Summarise scenario PnLs; distinguish protected vs all-seven-lose."""
    rows = list(scenarios)
    pnls = [r["pnl"] for r in rows]
    protected = None
    all_lose = None
    for r in rows:
        w = r["winners"]
        cheap_all_lose = all(w.get(s) != cheap_sides[s] for s in cheap_slugs)
        leader_wins = w.get(leader_slug) == leader_side
        seven_lose = (all(w.get(s) != cheap_sides[s] for s in cheap_slugs)
                      and w.get(leader_slug) != leader_side)
        if cheap_all_lose and leader_wins:
            protected = r["pnl"]
        if seven_lose:
            all_lose = r["pnl"]
    return {
        "n_scenarios": len(rows),
        "min_pnl": min(pnls) if pnls else 0.0,
        "max_pnl": max(pnls) if pnls else 0.0,
        "protected_pnl": protected,
        "all_seven_lose_pnl": all_lose,
        "mean_pnl": sum(pnls) / len(pnls) if pnls else 0.0,
    }


def run_preflight(
    *,
    cheap_quotes: Sequence[LegQuote],
    leader_quote: LegQuote,
    shares_per_cheap: float,
    target_pnl: float,
    slippage: float,
    fee_fn_leader,
    fee_fns_cheap: Sequence,
    available_cash: float,
    cash_buffer_usdc: float,
    cash_util_max: float,
    min_leader_ask: float,
    max_leader_ask: float,
    max_leader_unit: float,
    max_basket_notional: float,
    max_leader_notional: float,
    max_leader_multiple: float,
    max_all_lose: float,
    max_spread: float,
    max_quote_age_ms: float,
    max_quote_skew_ms: float,
    min_time_remaining_s: float,
    time_remaining_s: float,
    avg_cheap_ask: float,
    max_average_ask: float,
) -> PreflightResult:
    """Strict preflight: favourite band, cash/hedge, time, book quality, signal."""
    details: dict = {}
    if time_remaining_s < min_time_remaining_s:
        return PreflightResult(False, f"time gate: {time_remaining_s:.1f}s < {min_time_remaining_s:.1f}s",
                               EVT_PREFLIGHT_REJECT, details={"time_remaining_s": time_remaining_s})
    if avg_cheap_ask > max_average_ask + 1e-9:
        return PreflightResult(False, f"signal: avg cheap ask {avg_cheap_ask:.4f} > {max_average_ask:.4f}",
                               EVT_PREFLIGHT_REJECT)

    ages = [q.quote_age_ms for q in (*cheap_quotes, leader_quote)]
    if any(a > max_quote_age_ms for a in ages):
        return PreflightResult(False, "book quality: stale quote", EVT_PREFLIGHT_REJECT,
                               details={"ages_ms": ages})
    if max(ages) - min(ages) > max_quote_skew_ms:
        return PreflightResult(False, "book quality: cross-market quote skew", EVT_PREFLIGHT_REJECT,
                               details={"skew_ms": max(ages) - min(ages)})

    for q in (*cheap_quotes, leader_quote):
        if q.ask is None or q.bid is None or q.ask <= q.bid:
            return PreflightResult(False, f"book quality: missing/crossed book on {q.asset}",
                                   EVT_PREFLIGHT_REJECT)
        if q.spread > max_spread + 1e-9 or (q.ask - q.bid) > max_spread + 1e-9:
            return PreflightResult(False, f"book quality: spread on {q.asset}", EVT_PREFLIGHT_REJECT)
        if q.depth + 1e-9 < shares_per_cheap and q is not leader_quote:
            return PreflightResult(False, f"book quality: insufficient depth on {q.asset}",
                                   EVT_PREFLIGHT_REJECT)

    la = leader_quote.ask
    if not (min_leader_ask - 1e-9 <= la <= max_leader_ask + 1e-9):
        return PreflightResult(False, f"favorite ask {la:.3f} outside {min_leader_ask:.2f}-{max_leader_ask:.2f}",
                               EVT_PREFLIGHT_REJECT, details={"leader_ask": la})

    l_limit = min(1.0 - leader_quote.tick, round(la + slippage, 4))
    l_unit = unit_cost(la, l_limit, fee_fn_leader)
    if l_unit > max_leader_unit + 1e-9:
        return PreflightResult(False, f"favorite all-in unit {l_unit:.4f} > {max_leader_unit:.2f}",
                               EVT_PREFLIGHT_REJECT, details={"leader_unit": l_unit})

    cheap_cost = 0.0
    for q, fee_fn in zip(cheap_quotes, fee_fns_cheap):
        _, _, total = estimate_leg_cost(q.ask, shares_per_cheap, slippage, fee_fn, q.tick)
        if q.depth + 1e-9 < shares_per_cheap:
            return PreflightResult(False, f"depth: {q.asset} cannot fill {shares_per_cheap}",
                                   EVT_PREFLIGHT_REJECT)
        cheap_cost += total

    need, proj = leader_shares_for_target(cheap_cost, l_unit, target_pnl,
                                          venue_min=leader_quote.min_size)
    if need > 0 and leader_quote.depth + 1e-9 < need:
        return PreflightResult(False, f"depth: leader book {leader_quote.depth:.2f} < need {need:.2f}",
                               EVT_PREFLIGHT_REJECT)

    leader_notional = need * l_unit
    total = cheap_cost + leader_notional
    all_lose = -(cheap_cost + leader_notional)

    if max_basket_notional > 0 and total > max_basket_notional + 1e-9:
        return PreflightResult(False, f"risk: basket notional {total:.2f} > {max_basket_notional:.2f}",
                               EVT_RISK_LIMIT_REJECT, cheap_cost_est=cheap_cost, leader_unit=l_unit,
                               leader_shares=need, leader_notional=leader_notional, total_reserved=total,
                               projected_protected_pnl=proj, all_lose_pnl=all_lose)
    if max_leader_notional > 0 and leader_notional > max_leader_notional + 1e-9:
        return PreflightResult(False, f"risk: leader notional {leader_notional:.2f} > {max_leader_notional:.2f}",
                               EVT_RISK_LIMIT_REJECT, cheap_cost_est=cheap_cost, leader_unit=l_unit,
                               leader_shares=need, leader_notional=leader_notional, total_reserved=total,
                               projected_protected_pnl=proj, all_lose_pnl=all_lose)
    if max_leader_multiple > 0 and cheap_cost > 0 and leader_notional > max_leader_multiple * cheap_cost + 1e-9:
        return PreflightResult(False,
                               f"risk: leader/cheap multiple {leader_notional / cheap_cost:.2f} > {max_leader_multiple:.2f}",
                               EVT_RISK_LIMIT_REJECT, cheap_cost_est=cheap_cost, leader_unit=l_unit,
                               leader_shares=need, leader_notional=leader_notional, total_reserved=total,
                               projected_protected_pnl=proj, all_lose_pnl=all_lose)
    if max_all_lose > 0 and abs(all_lose) > max_all_lose + 1e-9:
        return PreflightResult(False, f"risk: all-lose {all_lose:.2f} breaches -{max_all_lose:.2f}",
                               EVT_RISK_LIMIT_REJECT, cheap_cost_est=cheap_cost, leader_unit=l_unit,
                               leader_shares=need, leader_notional=leader_notional, total_reserved=total,
                               projected_protected_pnl=proj, all_lose_pnl=all_lose)

    usable = available_cash * cash_util_max - cash_buffer_usdc
    if total > usable + 1e-9:
        return PreflightResult(False,
                               f"cash: need {total:.2f} (buffer {cash_buffer_usdc:.2f}) but usable {usable:.2f}",
                               EVT_PREFLIGHT_REJECT, cheap_cost_est=cheap_cost, leader_unit=l_unit,
                               leader_shares=need, leader_notional=leader_notional, total_reserved=total,
                               projected_protected_pnl=proj, all_lose_pnl=all_lose,
                               details={"available_cash": available_cash})

    details.update({
        "leader_ask": la, "leader_unit": l_unit, "leader_shares": need,
        "cheap_cost_est": cheap_cost, "total_reserved": total,
        "projected_protected_pnl": proj, "all_lose_pnl": all_lose,
    })
    return PreflightResult(
        True, "ok", EVT_PREFLIGHT_PASS,
        cheap_cost_est=cheap_cost, leader_unit=l_unit, leader_shares=need,
        leader_notional=leader_notional, total_reserved=total,
        projected_protected_pnl=proj, all_lose_pnl=all_lose, details=details,
    )
