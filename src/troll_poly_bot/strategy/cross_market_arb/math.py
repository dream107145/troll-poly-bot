"""Pure settlement / cost math for seven-market guaranteed-profit arbitrage.

Binary UP/DOWN markets settle at $1 per winning share. Guaranteed profit requires
that the minimum payout over all 2^N outcome combinations exceeds all-in
acquisition cost by more than the configured threshold. Correlations and EV are
never used as proof.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, Sequence


FeeFn = Callable[[float], float]


@dataclass(frozen=True, slots=True)
class LegQuote:
    """Executable ask snapshot for one token side of one market."""
    slug: str
    asset: str
    side: str  # "UP" | "DOWN"
    ask: float
    bid: float
    depth: float
    fee_fn: FeeFn
    tick: float = 0.01
    min_size: float = 5.0
    quote_age_ms: float = 0.0
    quote_ts_ms: float = 0.0
    levels: tuple[tuple[float, float], ...] = ()  # (price, size) ask ladder


@dataclass(frozen=True, slots=True)
class PlannedLeg:
    slug: str
    asset: str
    side: str
    shares: float
    limit: float
    unit_all_in: float
    total_cost: float
    ask: float


@dataclass(frozen=True, slots=True)
class PositionLeg:
    slug: str
    asset: str
    side: str
    shares: float
    cost: float  # inclusive of fees


@dataclass(slots=True)
class PortfolioMetrics:
    acquisition_cost: float
    min_payout: float
    max_payout: float
    min_net_profit: float
    n_scenarios: int
    scenario_pnls: list[float] = field(default_factory=list)


def round_shares(shares: float, step: float = 0.01) -> float:
    if shares <= 0.0:
        return 0.0
    return math.floor(shares / step + 1e-12) * step


def round_shares_up(shares: float, step: float = 0.01) -> float:
    if shares <= 0.0:
        return 0.0
    return math.ceil(shares / step - 1e-12) * step


def unit_all_in(ask: float, limit: float, fee_fn: FeeFn) -> float:
    """Worst all-in unit cost between visible ask and limit (price-dependent fees)."""
    probes = [ask, limit]
    if ask <= 0.5 <= limit:
        probes.append(0.5)
    return limit + max(float(fee_fn(px)) for px in probes)


def walk_ask_cost(levels: Sequence[tuple[float, float]] | None, shares: float,
                  cap: float) -> tuple[float | None, float]:
    """Return (VWAP to fill ``shares`` at prices <= cap, available depth under cap)."""
    if shares <= 0.0:
        return 0.0, 0.0
    if not levels:
        return None, 0.0
    remaining, cost, avail = shares, 0.0, 0.0
    for px, sz in levels:
        if px > cap + 1e-12:
            break
        avail += sz
        take = min(remaining, sz)
        if take > 0:
            cost += take * px
            remaining -= take
    if remaining > 1e-9:
        return None, avail
    return cost / shares, avail


def estimate_leg(quote: LegQuote, shares: float, slippage: float
                 ) -> PlannedLeg | None:
    """Plan a buy of ``shares`` using executable asks + fee at the limit."""
    if shares + 1e-12 < quote.min_size:
        return None
    shares = round_shares(shares)
    if shares + 1e-12 < quote.min_size:
        return None
    if quote.depth + 1e-9 < shares:
        return None
    tick = quote.tick or 0.01
    limit = min(1.0 - tick, round(quote.ask + slippage, 4))
    levels = quote.levels or ((quote.ask, quote.depth),)
    vwap, avail = walk_ask_cost(levels, shares, limit)
    if vwap is None or avail + 1e-9 < shares:
        return None
    unit = unit_all_in(vwap, limit, quote.fee_fn)
    return PlannedLeg(
        slug=quote.slug, asset=quote.asset, side=quote.side, shares=shares,
        limit=limit, unit_all_in=unit, total_cost=unit * shares, ask=quote.ask,
    )


def settlement_payout(legs: Sequence[PositionLeg], winners: dict[str, str]) -> float:
    payout = 0.0
    for leg in legs:
        if winners.get(leg.slug) == leg.side:
            payout += float(leg.shares)
    return payout


def settlement_pnl(legs: Sequence[PositionLeg], winners: dict[str, str]) -> float:
    return settlement_payout(legs, winners) - sum(float(l.cost) for l in legs)


def all_outcome_scenarios(legs: Sequence[PositionLeg],
                          market_slugs: Sequence[str]) -> list[dict]:
    """Enumerate all 2^N outcome combinations."""
    slugs = list(market_slugs)
    out = []
    for bits in itertools.product(("UP", "DOWN"), repeat=len(slugs)):
        winners = dict(zip(slugs, bits))
        out.append({
            "winners": winners,
            "payout": settlement_payout(legs, winners),
            "pnl": settlement_pnl(legs, winners),
        })
    return out


def closed_form_min_payout(legs: Sequence[PositionLeg]) -> float:
    """For independent binary markets, min payout = sum_m min(q_up, q_down)."""
    by_slug: dict[str, dict[str, float]] = {}
    for leg in legs:
        by_slug.setdefault(leg.slug, {"UP": 0.0, "DOWN": 0.0})
        by_slug[leg.slug][leg.side] = by_slug[leg.slug].get(leg.side, 0.0) + float(leg.shares)
    return sum(min(sides.get("UP", 0.0), sides.get("DOWN", 0.0)) for sides in by_slug.values())


def portfolio_metrics(legs: Sequence[PositionLeg],
                      market_slugs: Sequence[str] | None = None,
                      *, verify_enumeration: bool = True) -> PortfolioMetrics:
    """Compute acquisition cost and min payout; optionally verify via full 128-scan."""
    cost = sum(float(l.cost) for l in legs)
    slugs = list(market_slugs) if market_slugs is not None else sorted({l.slug for l in legs})
    cf_min = closed_form_min_payout(legs)
    if not slugs:
        return PortfolioMetrics(cost, 0.0, 0.0, -cost, 0, [])
    scenarios = all_outcome_scenarios(legs, slugs) if verify_enumeration else []
    if scenarios:
        pnls = [s["pnl"] for s in scenarios]
        payouts = [s["payout"] for s in scenarios]
        enum_min = min(payouts)
        # Closed form must match enumeration for independent binaries.
        if abs(enum_min - cf_min) > 1e-6:
            cf_min = enum_min
        return PortfolioMetrics(
            acquisition_cost=cost,
            min_payout=enum_min,
            max_payout=max(payouts),
            min_net_profit=enum_min - cost,
            n_scenarios=len(scenarios),
            scenario_pnls=pnls,
        )
    return PortfolioMetrics(
        acquisition_cost=cost,
        min_payout=cf_min,
        max_payout=sum(float(l.shares) for l in legs),
        min_net_profit=cf_min - cost,
        n_scenarios=2 ** len(slugs),
        scenario_pnls=[],
    )


def planned_to_positions(legs: Sequence[PlannedLeg]) -> list[PositionLeg]:
    return [PositionLeg(l.slug, l.asset, l.side, l.shares, l.total_cost) for l in legs]


def is_guaranteed_profit(legs: Sequence[PositionLeg], min_net_profit: float,
                         market_slugs: Sequence[str] | None = None) -> tuple[bool, PortfolioMetrics]:
    m = portfolio_metrics(legs, market_slugs, verify_enumeration=True)
    return m.min_net_profit > min_net_profit + 1e-12, m
