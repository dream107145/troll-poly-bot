"""Execution state machine and portfolio plan tracking for CROSS_MARKET_ARB."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .math import PositionLeg, portfolio_metrics
from .optimizer import PortfolioCandidate


class ArbState(str, Enum):
    IDLE = "IDLE"
    SCANNING = "SCANNING"
    OPPORTUNITY_FOUND = "OPPORTUNITY_FOUND"
    PREFLIGHT = "PREFLIGHT"
    EXECUTING = "EXECUTING"
    RECONCILING = "RECONCILING"
    COMPLETE = "COMPLETE"
    PREFLIGHT_REJECT = "PREFLIGHT_REJECT"
    CANCELLED = "CANCELLED"
    INCOMPLETE = "INCOMPLETE"
    RISK_HALT = "RISK_HALT"


# Structured log / dashboard event names
EVT_OPPORTUNITY = "OPPORTUNITY_DISCOVERY"
EVT_PREFLIGHT_PASS = "PREFLIGHT_PASS"
EVT_PREFLIGHT_REJECT = "PREFLIGHT_REJECT"
EVT_CAPITAL_RESERVE = "CAPITAL_RESERVATION"
EVT_ORDER_SUBMIT = "ORDER_SUBMISSION"
EVT_ORDER_FILL = "ORDER_FILL"
EVT_ORDER_CANCEL = "ORDER_CANCELLATION"
EVT_RECONCILE = "PORTFOLIO_RECONCILIATION"
EVT_COMPLETE = "ARBITRAGE_COMPLETION"
EVT_INCOMPLETE = "INCOMPLETE_PORTFOLIO"
EVT_RISK_REJECT = "RISK_LIMIT_REJECTION"
EVT_CIRCUIT = "DAILY_CIRCUIT_BREAKER"


@dataclass(slots=True)
class LegStatus:
    slug: str
    asset: str
    side: str
    target_shares: float
    limit: float
    filled_shares: float = 0.0
    filled_cost: float = 0.0
    order_ids: list[str] = field(default_factory=list)
    last_submit_ms: float = 0.0
    done: bool = False


@dataclass(slots=True)
class PortfolioPlan:
    plan_id: str
    epoch: int
    state: str = ArbState.IDLE.value
    legs: list[LegStatus] = field(default_factory=list)
    reserved_cash: float = 0.0
    modeled_min_profit: float = 0.0
    modeled_cost: float = 0.0
    actual_min_profit: float | None = None
    actual_cost: float = 0.0
    reject_reason: str = ""
    client_order_keys: set[str] = field(default_factory=set)
    events: list[dict] = field(default_factory=list)
    created_ms: float = 0.0
    updated_ms: float = 0.0
    complete: bool = False
    incomplete: bool = False

    def position_legs(self) -> list[PositionLeg]:
        return [
            PositionLeg(l.slug, l.asset, l.side, l.filled_shares, l.filled_cost)
            for l in self.legs if l.filled_shares > 1e-9
        ]

    def pending_legs(self) -> list[LegStatus]:
        return [l for l in self.legs if not l.done and l.filled_shares + 1e-9 < l.target_shares]

    def remaining_notional(self) -> float:
        total = 0.0
        for l in self.legs:
            need = max(0.0, l.target_shares - l.filled_shares)
            total += need * l.limit
        return total


def new_plan_id() -> str:
    return uuid.uuid4().hex[:12]


def plan_from_candidate(cand: PortfolioCandidate, epoch: int, now_ms: float) -> PortfolioPlan:
    legs = [
        LegStatus(
            slug=leg.slug, asset=leg.asset, side=leg.side,
            target_shares=leg.shares, limit=leg.limit,
        )
        for leg in cand.legs
    ]
    return PortfolioPlan(
        plan_id=new_plan_id(),
        epoch=epoch,
        state=ArbState.OPPORTUNITY_FOUND.value,
        legs=legs,
        reserved_cash=cand.metrics.acquisition_cost,
        modeled_min_profit=cand.metrics.min_net_profit,
        modeled_cost=cand.metrics.acquisition_cost,
        created_ms=now_ms,
        updated_ms=now_ms,
    )


def refresh_actuals(plan: PortfolioPlan, market_slugs: list[str]) -> dict[str, Any]:
    pos = plan.position_legs()
    m = portfolio_metrics(pos, market_slugs, verify_enumeration=True)
    plan.actual_cost = m.acquisition_cost
    plan.actual_min_profit = m.min_net_profit
    return {
        "acquisition_cost": m.acquisition_cost,
        "min_payout": m.min_payout,
        "min_net_profit": m.min_net_profit,
        "n_scenarios": m.n_scenarios,
        "guaranteed": m.min_net_profit > 0,
    }


def record_event(plan: PortfolioPlan, event: str, now_ms: float, **kw) -> dict:
    row = {"t": now_ms, "event": event, "plan_id": plan.plan_id, "state": plan.state, **kw}
    plan.events.append(row)
    plan.updated_ms = now_ms
    if len(plan.events) > 200:
        plan.events = plan.events[-200:]
    return row


def client_order_key(plan_id: str, slug: str, side: str, seq: int) -> str:
    """Idempotent key to prevent duplicate orders after restart/timeout."""
    return f"cma:{plan_id}:{slug}:{side}:{seq}"


def mark_fill(plan: PortfolioPlan, slug: str, side: str, shares: float, price: float,
              fee: float, now_ms: float, market_slugs: list[str]) -> dict | None:
    for leg in plan.legs:
        if leg.slug == slug and leg.side == side:
            leg.filled_shares += float(shares)
            leg.filled_cost += float(price) * float(shares) + float(fee)
            if leg.filled_shares + 1e-9 >= leg.target_shares:
                leg.done = True
            actual = refresh_actuals(plan, market_slugs)
            return record_event(plan, EVT_ORDER_FILL, now_ms, slug=slug, side=side,
                                shares=shares, price=price, fee=fee, **actual)
    return None


def as_dict(plan: PortfolioPlan) -> dict:
    return {
        "plan_id": plan.plan_id,
        "epoch": plan.epoch,
        "state": plan.state,
        "reserved_cash": plan.reserved_cash,
        "modeled_min_profit": plan.modeled_min_profit,
        "modeled_cost": plan.modeled_cost,
        "actual_min_profit": plan.actual_min_profit,
        "actual_cost": plan.actual_cost,
        "reject_reason": plan.reject_reason,
        "complete": plan.complete,
        "incomplete": plan.incomplete,
        "legs": [
            {
                "slug": l.slug, "asset": l.asset, "side": l.side,
                "target": l.target_shares, "filled": l.filled_shares,
                "cost": l.filled_cost, "limit": l.limit, "done": l.done,
                "orders": list(l.order_ids),
            }
            for l in plan.legs
        ],
        "events": list(plan.events[-40:]),
        "created_ms": plan.created_ms,
        "updated_ms": plan.updated_ms,
    }


def plan_from_dict(doc: dict) -> PortfolioPlan:
    legs = [
        LegStatus(
            slug=str(x["slug"]), asset=str(x.get("asset", "")), side=str(x["side"]),
            target_shares=float(x.get("target", 0)), limit=float(x.get("limit", 0)),
            filled_shares=float(x.get("filled", 0)), filled_cost=float(x.get("cost", 0)),
            order_ids=list(x.get("orders") or []),
            done=bool(x.get("done")),
        )
        for x in (doc.get("legs") or [])
    ]
    return PortfolioPlan(
        plan_id=str(doc.get("plan_id") or new_plan_id()),
        epoch=int(doc.get("epoch") or 0),
        state=str(doc.get("state") or ArbState.IDLE.value),
        legs=legs,
        reserved_cash=float(doc.get("reserved_cash") or 0),
        modeled_min_profit=float(doc.get("modeled_min_profit") or 0),
        modeled_cost=float(doc.get("modeled_cost") or 0),
        actual_min_profit=doc.get("actual_min_profit"),
        actual_cost=float(doc.get("actual_cost") or 0),
        reject_reason=str(doc.get("reject_reason") or ""),
        client_order_keys=set(doc.get("client_order_keys") or []),
        events=list(doc.get("events") or []),
        created_ms=float(doc.get("created_ms") or 0),
        updated_ms=float(doc.get("updated_ms") or time.time() * 1000),
        complete=bool(doc.get("complete")),
        incomplete=bool(doc.get("incomplete")),
    )
