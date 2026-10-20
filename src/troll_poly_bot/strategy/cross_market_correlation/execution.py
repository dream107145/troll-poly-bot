"""Two-leg execution state machine for cross-market correlation pairs.

Does not assume simultaneous fills. After any fill, EV / worst-case is
recomputed from actual inventory before further orders. One-leg residual
exposure is handled by an explicit policy (hold / cancel_alert / hedge_if_edge).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .math import (
    JointProbs, PairPayoff, Direction, Label, expected_payout, is_guaranteed_arbitrage,
    payoff_a_down_b_up, payoff_a_up_b_down, worst_case_net,
)


class PairState(str, Enum):
    IDLE = "IDLE"
    SCANNING = "SCANNING"
    ARMED = "ARMED"
    LEG1_SUBMITTED = "LEG1_SUBMITTED"
    LEG1_FILLED = "LEG1_FILLED"
    LEG2_SUBMITTED = "LEG2_SUBMITTED"
    MATCHED = "MATCHED"
    UNHEDGED = "UNHEDGED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    RISK_HALT = "RISK_HALT"
    COMPLETE = "COMPLETE"


EVT_SCAN = "PAIR_SCAN"
EVT_ARM = "PAIR_ARM"
EVT_SUBMIT = "LEG_SUBMIT"
EVT_FILL = "LEG_FILL"
EVT_REPRICE = "REPRICE_AFTER_FILL"
EVT_UNHEDGED = "UNHEDGED"
EVT_CANCEL = "CANCEL"
EVT_COMPLETE = "MATCHED_COMPLETE"
EVT_RISK = "RISK_REJECT"
EVT_CIRCUIT = "DAILY_CIRCUIT"


@dataclass(slots=True)
class LegStatus:
    slug: str
    asset: str
    side: str  # UP | DOWN
    role: str  # A | B
    target_shares: float
    limit: float
    filled_shares: float = 0.0
    filled_cost: float = 0.0
    order_ids: list[str] = field(default_factory=list)
    last_submit_ms: float = 0.0
    retries: int = 0
    done: bool = False


@dataclass(slots=True)
class PairPlan:
    plan_id: str
    epoch: int
    asset_a: str
    asset_b: str
    slug_a: str
    slug_b: str
    direction: Direction
    label: Label
    state: str = PairState.IDLE.value
    legs: list[LegStatus] = field(default_factory=list)
    reserved_cash: float = 0.0
    modeled_cost: float = 0.0
    modeled_ev: float = 0.0
    modeled_worst_case: float = 0.0
    actual_cost: float = 0.0
    probs: dict = field(default_factory=dict)
    reject_reason: str = ""
    client_order_keys: set[str] = field(default_factory=set)
    events: list[dict] = field(default_factory=list)
    created_ms: float = 0.0
    updated_ms: float = 0.0
    matched: bool = False
    unhedged: bool = False
    unhedged_alerted: bool = False

    def leg_a(self) -> LegStatus:
        return next(l for l in self.legs if l.role == "A")

    def leg_b(self) -> LegStatus:
        return next(l for l in self.legs if l.role == "B")

    def pending_legs(self) -> list[LegStatus]:
        return [l for l in self.legs if not l.done and l.filled_shares + 1e-9 < l.target_shares]

    def filled_matched_shares(self) -> float:
        return min(self.leg_a().filled_shares, self.leg_b().filled_shares)

    def unhedged_shares(self) -> float:
        return abs(self.leg_a().filled_shares - self.leg_b().filled_shares)

    def unhedged_notional(self) -> float:
        a, b = self.leg_a(), self.leg_b()
        if a.filled_shares > b.filled_shares + 1e-9:
            avg = a.filled_cost / max(a.filled_shares, 1e-9)
            return (a.filled_shares - b.filled_shares) * avg
        if b.filled_shares > a.filled_shares + 1e-9:
            avg = b.filled_cost / max(b.filled_shares, 1e-9)
            return (b.filled_shares - a.filled_shares) * avg
        return 0.0

    def remaining_notional(self) -> float:
        total = 0.0
        for l in self.legs:
            need = max(0.0, l.target_shares - l.filled_shares)
            total += need * l.limit
        return total


def new_plan_id() -> str:
    return uuid.uuid4().hex[:12]


def client_order_key(plan_id: str, slug: str, side: str, seq: int) -> str:
    return f"cmc:{plan_id}:{slug}:{side}:{seq}"


def payoff_for(direction: Direction, shares: float) -> PairPayoff:
    return (payoff_a_up_b_down(shares) if direction == "A_UP_B_DOWN"
            else payoff_a_down_b_up(shares))


def plan_from_scan(
    *,
    asset_a: str, asset_b: str, slug_a: str, slug_b: str,
    direction: Direction, label: Label,
    shares: float, limit_a: float, limit_b: float,
    side_a: str, side_b: str,
    reserved: float, modeled_ev: float, modeled_worst: float,
    probs: JointProbs, epoch: int, now_ms: float,
) -> PairPlan:
    return PairPlan(
        plan_id=new_plan_id(),
        epoch=epoch,
        asset_a=asset_a, asset_b=asset_b,
        slug_a=slug_a, slug_b=slug_b,
        direction=direction, label=label,
        state=PairState.ARMED.value,
        legs=[
            LegStatus(slug=slug_a, asset=asset_a, side=side_a, role="A",
                      target_shares=shares, limit=limit_a),
            LegStatus(slug=slug_b, asset=asset_b, side=side_b, role="B",
                      target_shares=shares, limit=limit_b),
        ],
        reserved_cash=reserved,
        modeled_cost=reserved,
        modeled_ev=modeled_ev,
        modeled_worst_case=modeled_worst,
        probs={
            "p_uu": probs.p_uu, "p_ud": probs.p_ud, "p_du": probs.p_du, "p_dd": probs.p_dd,
            "n": probs.n, "uncertainty": probs.uncertainty,
            "agreement": probs.agreement_rate,
        },
        created_ms=now_ms, updated_ms=now_ms,
    )


def sides_for_direction(direction: Direction) -> tuple[str, str]:
    if direction == "A_UP_B_DOWN":
        return "UP", "DOWN"
    return "DOWN", "UP"


def record_event(plan: PairPlan, event: str, now_ms: float, **kw) -> dict:
    row = {"t": now_ms, "event": event, "plan_id": plan.plan_id, "state": plan.state, **kw}
    plan.events.append(row)
    plan.updated_ms = now_ms
    if len(plan.events) > 200:
        plan.events = plan.events[-200:]
    return row


def refresh_actuals(plan: PairPlan) -> dict[str, Any]:
    a, b = plan.leg_a(), plan.leg_b()
    matched = min(a.filled_shares, b.filled_shares)
    cost = a.filled_cost + b.filled_cost
    plan.actual_cost = cost
    base = {
        "matched": matched,
        "cost": cost,
        "unhedged": plan.unhedged_shares(),
        "unhedged_notional": plan.unhedged_notional(),
    }
    if matched <= 1e-9:
        return {**base, "worst_case": -cost, "guaranteed": False, "expected_payout": 0.0, "ev": -cost}
    # Scale payoff to matched shares; residual unhedged treated as potentially zero.
    payoff = payoff_for(plan.direction, matched)
    # Unhedged leftover: worst case assumes it pays 0.
    wc = worst_case_net(payoff, cost)
    guaranteed = is_guaranteed_arbitrage(payoff, cost, 0.0)
    probs = JointProbs(
        float(plan.probs.get("p_uu", 0.25)),
        float(plan.probs.get("p_ud", 0.25)),
        float(plan.probs.get("p_du", 0.25)),
        float(plan.probs.get("p_dd", 0.25)),
        n=int(plan.probs.get("n", 0)),
        uncertainty=float(plan.probs.get("uncertainty", 1.0)),
    )
    ep = expected_payout(payoff, probs)
    return {
        **base,
        "worst_case": wc,
        "expected_payout": ep,
        "ev": ep - cost,
        "guaranteed": guaranteed,
    }


def mark_fill(plan: PairPlan, slug: str, side: str, shares: float, price: float,
              fee: float, now_ms: float) -> dict | None:
    for leg in plan.legs:
        if leg.slug == slug and leg.side == side:
            leg.filled_shares += float(shares)
            leg.filled_cost += float(price) * float(shares) + float(fee)
            if leg.filled_shares + 1e-9 >= leg.target_shares:
                leg.done = True
            actual = refresh_actuals(plan)
            evt = record_event(plan, EVT_FILL, now_ms, slug=slug, side=side,
                               shares=shares, price=price, fee=fee, **actual)
            # State transitions
            pending = plan.pending_legs()
            if not pending and plan.unhedged_shares() < 1e-6:
                plan.state = PairState.MATCHED.value
                plan.matched = True
                record_event(plan, EVT_COMPLETE, now_ms, **actual)
            elif plan.unhedged_shares() > 1e-6 and pending:
                plan.state = PairState.LEG1_FILLED.value
                record_event(plan, EVT_REPRICE, now_ms, **actual)
            elif plan.unhedged_shares() > 1e-6 and not pending:
                plan.state = PairState.UNHEDGED.value
                plan.unhedged = True
                record_event(plan, EVT_UNHEDGED, now_ms, **actual)
            return evt
    return None


def as_dict(plan: PairPlan) -> dict:
    return {
        "plan_id": plan.plan_id,
        "epoch": plan.epoch,
        "state": plan.state,
        "asset_a": plan.asset_a, "asset_b": plan.asset_b,
        "slug_a": plan.slug_a, "slug_b": plan.slug_b,
        "direction": plan.direction, "label": plan.label,
        "reserved_cash": plan.reserved_cash,
        "modeled_cost": plan.modeled_cost,
        "modeled_ev": plan.modeled_ev,
        "modeled_worst_case": plan.modeled_worst_case,
        "actual_cost": plan.actual_cost,
        "probs": dict(plan.probs),
        "reject_reason": plan.reject_reason,
        "matched": plan.matched,
        "unhedged": plan.unhedged,
        "unhedged_shares": plan.unhedged_shares(),
        "unhedged_notional": plan.unhedged_notional(),
        "legs": [
            {
                "slug": l.slug, "asset": l.asset, "side": l.side, "role": l.role,
                "target": l.target_shares, "filled": l.filled_shares,
                "cost": l.filled_cost, "limit": l.limit, "done": l.done,
                "retries": l.retries, "orders": list(l.order_ids),
            }
            for l in plan.legs
        ],
        "events": list(plan.events[-40:]),
        "created_ms": plan.created_ms,
        "updated_ms": plan.updated_ms,
    }


def plan_from_dict(doc: dict) -> PairPlan:
    legs = [
        LegStatus(
            slug=str(x["slug"]), asset=str(x.get("asset", "")), side=str(x["side"]),
            role=str(x.get("role", "A")),
            target_shares=float(x.get("target", 0)), limit=float(x.get("limit", 0)),
            filled_shares=float(x.get("filled", 0)), filled_cost=float(x.get("cost", 0)),
            order_ids=list(x.get("orders") or []),
            retries=int(x.get("retries", 0)),
            done=bool(x.get("done")),
        )
        for x in (doc.get("legs") or [])
    ]
    return PairPlan(
        plan_id=str(doc.get("plan_id") or new_plan_id()),
        epoch=int(doc.get("epoch") or 0),
        asset_a=str(doc.get("asset_a", "")),
        asset_b=str(doc.get("asset_b", "")),
        slug_a=str(doc.get("slug_a", "")),
        slug_b=str(doc.get("slug_b", "")),
        direction=str(doc.get("direction") or "A_UP_B_DOWN"),
        label=str(doc.get("label") or "REJECTED"),
        state=str(doc.get("state") or PairState.IDLE.value),
        legs=legs,
        reserved_cash=float(doc.get("reserved_cash") or 0),
        modeled_cost=float(doc.get("modeled_cost") or 0),
        modeled_ev=float(doc.get("modeled_ev") or 0),
        modeled_worst_case=float(doc.get("modeled_worst_case") or 0),
        actual_cost=float(doc.get("actual_cost") or 0),
        probs=dict(doc.get("probs") or {}),
        reject_reason=str(doc.get("reject_reason") or ""),
        client_order_keys=set(doc.get("client_order_keys") or []),
        events=list(doc.get("events") or []),
        created_ms=float(doc.get("created_ms") or 0),
        updated_ms=float(doc.get("updated_ms") or 0),
        matched=bool(doc.get("matched")),
        unhedged=bool(doc.get("unhedged")),
    )
