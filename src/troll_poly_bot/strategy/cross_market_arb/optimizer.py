"""Deterministic portfolio optimizer for cross-market guaranteed-profit arb.

Method (bounded, auditable):
  1. For each market, score equal UP+DOWN sizes on a discrete share grid (unequal
     inventory cannot raise that market's contribution to min payout).
  2. Enumerate all 2^N subsets of markets that have a profitable pair size.
  3. Rank feasible portfolios by net profit, capital efficiency, execution risk.
  4. Verify the winner with a full 2^N outcome enumeration.

Single-sided holdings are evaluated only as audit rejects: they contribute 0 to
min payout for independent binaries and cannot create guaranteed profit alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .math import (
    LegQuote, PlannedLeg, PortfolioMetrics, estimate_leg, is_guaranteed_profit,
    planned_to_positions, portfolio_metrics, round_shares,
)


@dataclass(frozen=True, slots=True)
class MarketBooks:
    asset: str
    slug: str
    up: LegQuote
    down: LegQuote
    time_remaining_s: float = 300.0


@dataclass(slots=True)
class RejectRecord:
    reason: str
    detail: str = ""
    net_profit: float = 0.0


@dataclass(slots=True)
class PortfolioCandidate:
    legs: list[PlannedLeg]
    metrics: PortfolioMetrics
    capital_efficiency: float
    execution_risk: float
    rank_score: float
    reject_reason: str = ""


@dataclass(slots=True)
class OptimizeResult:
    best: PortfolioCandidate | None
    rejected: list[RejectRecord] = field(default_factory=list)
    evaluated: int = 0
    state: str = "NO_OPPORTUNITY"


def _share_grid(min_size: float, max_shares: float, step: float) -> list[float]:
    out: list[float] = []
    q = float(min_size)
    while q <= max_shares + 1e-9:
        out.append(round_shares(q, step))
        q += step
    seen: set[float] = set()
    clean = []
    for x in out:
        if x not in seen and x + 1e-12 >= min_size:
            seen.add(x)
            clean.append(x)
    return clean


def _best_equal_pair(m: MarketBooks, slippage: float, share_step: float,
                     max_shares_per_leg: float) -> tuple[list[PlannedLeg], PortfolioMetrics] | None:
    """Best equal-size both-sides package for one market, or None."""
    min_sz = max(m.up.min_size, m.down.min_size, 5.0)
    best: tuple[list[PlannedLeg], PortfolioMetrics] | None = None
    for q in _share_grid(min_sz, max_shares_per_leg, share_step):
        up = estimate_leg(m.up, q, slippage)
        dn = estimate_leg(m.down, q, slippage)
        if up is None or dn is None:
            continue
        legs = [up, dn]
        metrics = portfolio_metrics(planned_to_positions(legs), [m.slug], verify_enumeration=True)
        if best is None or metrics.min_net_profit > best[1].min_net_profit + 1e-12:
            best = (legs, metrics)
        elif (abs(metrics.min_net_profit - best[1].min_net_profit) <= 1e-12
              and metrics.acquisition_cost < best[1].acquisition_cost):
            best = (legs, metrics)
    return best


def _execution_risk(legs: Sequence[PlannedLeg], books: dict[str, MarketBooks]) -> float:
    if not legs:
        return 0.0
    risk = 0.0
    for leg in legs:
        m = books.get(leg.slug)
        if m is None:
            risk += 1.0
            continue
        q = m.up if leg.side == "UP" else m.down
        risk += leg.shares / max(q.depth, 1e-9)
    return risk + 0.05 * len(legs)


def _rank(metrics: PortfolioMetrics, risk: float) -> float:
    if metrics.acquisition_cost <= 0:
        return -1e18
    eff = metrics.min_net_profit / metrics.acquisition_cost
    return metrics.min_net_profit * 1000.0 + eff * 10.0 - risk


def optimize_portfolio(
    markets: Sequence[MarketBooks],
    *,
    minimum_net_profit: float,
    maximum_basket_notional: float,
    maximum_market_exposure: float,
    available_cash: float,
    minimum_cash_reserve: float,
    max_shares_per_leg: float = 50.0,
    share_step: float = 5.0,
    slippage: float = 0.01,
    max_combinations: int = 250_000,
) -> OptimizeResult:
    rejected: list[RejectRecord] = []
    if len(markets) < 1:
        return OptimizeResult(None, [RejectRecord("no_markets")], state="NO_OPPORTUNITY")

    books = {m.slug: m for m in markets}
    spendable = max(0.0, available_cash - minimum_cash_reserve)

    # Per-market best equal pair (may be negative — those markets are skippable).
    per: list[tuple[MarketBooks, list[PlannedLeg], PortfolioMetrics] | None] = []
    for m in markets:
        got = _best_equal_pair(m, slippage, share_step, max_shares_per_leg)
        if got is None:
            per.append(None)
            rejected.append(RejectRecord("no_depth", m.asset))
            continue
        legs, metrics = got
        # Audit: single-sided never clears guaranteed profit alone
        for side_q, label in ((m.up, "UP"), (m.down, "DOWN")):
            solo = estimate_leg(side_q, max(side_q.min_size, 5.0), slippage)
            if solo is not None:
                ok_s, ms = is_guaranteed_profit(planned_to_positions([solo]), minimum_net_profit, [m.slug])
                if not ok_s:
                    rejected.append(RejectRecord("single_sided", f"{m.asset}:{label}", ms.min_net_profit))
        if metrics.min_net_profit <= 0:
            rejected.append(RejectRecord("market_unprofitable", m.asset, metrics.min_net_profit))
            per.append(None)
        else:
            per.append((m, legs, metrics))

    profitable_idx = [i for i, row in enumerate(per) if row is not None]
    if not profitable_idx:
        return OptimizeResult(None, rejected[:200], 0, state="NO_OPPORTUNITY")

    best: PortfolioCandidate | None = None
    evaluated = 0
    n = len(profitable_idx)
    # 2^n subsets, n <= 7 => 128
    for mask in range(1, 1 << n):
        if evaluated >= max_combinations:
            break
        evaluated += 1
        legs: list[PlannedLeg] = []
        cost = 0.0
        ok_mask = True
        for bit, idx in enumerate(profitable_idx):
            if not (mask & (1 << bit)):
                continue
            row = per[idx]
            assert row is not None
            m, mlegs, mmetrics = row
            mcost = mmetrics.acquisition_cost
            if maximum_market_exposure > 0 and mcost > maximum_market_exposure + 1e-9:
                rejected.append(RejectRecord("market_exposure", m.asset, -mcost))
                ok_mask = False
                break
            legs.extend(mlegs)
            cost += mcost
        if not ok_mask or not legs:
            continue
        if maximum_basket_notional > 0 and cost > maximum_basket_notional + 1e-9:
            rejected.append(RejectRecord("basket_notional", f"{cost:.4f}", -cost))
            continue
        if cost > spendable + 1e-9:
            rejected.append(RejectRecord("cash", f"need {cost:.4f} have {spendable:.4f}", -cost))
            continue
        pos = planned_to_positions(legs)
        slugs = [m.slug for m in markets]
        ok, metrics = is_guaranteed_profit(pos, minimum_net_profit, slugs)
        if not ok:
            rejected.append(RejectRecord(
                "below_min_profit",
                f"min_net={metrics.min_net_profit:.4f} need>{minimum_net_profit:.4f}",
                metrics.min_net_profit,
            ))
            continue
        risk = _execution_risk(legs, books)
        eff = metrics.min_net_profit / metrics.acquisition_cost if metrics.acquisition_cost > 0 else 0.0
        cand = PortfolioCandidate(
            legs=list(legs), metrics=metrics, capital_efficiency=eff,
            execution_risk=risk, rank_score=_rank(metrics, risk),
        )
        if best is None or cand.rank_score > best.rank_score:
            best = cand

    if len(rejected) > 200:
        rejected = rejected[:200]
    if best is None:
        return OptimizeResult(None, rejected, evaluated, state="NO_OPPORTUNITY")
    return OptimizeResult(best, rejected, evaluated, state="OPPORTUNITY_FOUND")
