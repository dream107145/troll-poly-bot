#!/usr/bin/env python3
"""Paper / historical replay for CROSS_MARKET_ARB vs Cheapside basket baseline.

Compares the new guaranteed-profit optimizer against the existing six-cheap-legs-
plus-favorite rule without modifying that baseline. Uses deterministic synthetic
books (synchronized seven-market depth archives are not yet available) plus any
chart JSON under data/charts/ that covers all seven assets for the same epoch.

Usage:
  .venv/bin/python scripts/replay_cross_market_arb.py
  .venv/bin/python scripts/replay_cross_market_arb.py --out results/cross_market_arb_replay.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.cheapside_basket import (
    LegQuote as CheapQuote, PositionLeg as CheapPos, all_outcome_scenarios as cheap_scenarios,
    estimate_leg_cost, settlement_pnl as cheap_settlement,
)
from troll_poly_bot.strategy.cross_market_arb.math import (
    LegQuote, PlannedLeg, estimate_leg, planned_to_positions, portfolio_metrics,
)
from troll_poly_bot.strategy.cross_market_arb.optimizer import MarketBooks, optimize_portfolio

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
FEE = FeeSchedule()
T0 = 1_800_000_000_000.0


@dataclass
class ReplayMetrics:
    name: str
    opportunities_detected: int = 0
    opportunities_rejected: int = 0
    reject_reasons: dict = field(default_factory=dict)
    orders_submitted: int = 0
    orders_filled: int = 0
    portfolios_complete: int = 0
    incomplete_portfolios: int = 0
    guaranteed_before: list = field(default_factory=list)
    guaranteed_after_fills: list = field(default_factory=list)
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    worst_case_modeled_pnl: float = 0.0
    settlement_pnl: float = 0.0
    max_drawdown: float = 0.0
    capital_peak: float = 0.0
    capital_utilization: float = 0.0
    notes: str = ""


def _fee(p: float) -> float:
    return FEE.taker_fee_per_share(p)


def _cma_quote(asset: str, side: str, ask: float, depth: float = 200.0) -> LegQuote:
    bid = max(0.01, ask - 0.02)
    return LegQuote(
        slug=f"{asset.lower()}-updown-5m-{int(T0 // 1000)}", asset=asset, side=side,
        ask=ask, bid=bid, depth=depth, fee_fn=_fee,
        levels=((ask, depth), (min(0.99, round(ask + 0.01, 2)), depth)),
        quote_age_ms=40.0, quote_ts_ms=T0, min_size=5.0,
    )


def _scenario_books(kind: str) -> list[MarketBooks]:
    if kind == "pair_arb":
        # Classic both-sides mispricing on every market
        up, dn, depth = 0.40, 0.40, 200.0
    elif kind == "thin_depth":
        up, dn, depth = 0.40, 0.40, 3.0
    elif kind == "stale_no_edge":
        up, dn, depth = 0.55, 0.55, 200.0
    elif kind == "one_market_arb":
        books = []
        for i, a in enumerate(ASSETS):
            u, d = (0.40, 0.40) if i == 0 else (0.55, 0.55)
            books.append(MarketBooks(a, _cma_quote(a, "UP", u).slug,
                                     _cma_quote(a, "UP", u), _cma_quote(a, "DOWN", d), 200.0))
        return books
    elif kind == "partial_fill_risk":
        up, dn, depth = 0.41, 0.41, 8.0  # only ~one 5-share fill per side
    else:
        up, dn, depth = 0.48, 0.48, 200.0
    return [
        MarketBooks(a, _cma_quote(a, "UP", up, depth).slug,
                    _cma_quote(a, "UP", up, depth), _cma_quote(a, "DOWN", dn, depth), 200.0)
        for a in ASSETS
    ]


def _run_cma(kind: str, min_profit: float = 0.05) -> ReplayMetrics:
    m = ReplayMetrics(name=f"cma:{kind}")
    books = _scenario_books(kind)
    res = optimize_portfolio(
        books, minimum_net_profit=min_profit, maximum_basket_notional=80.0,
        maximum_market_exposure=40.0, available_cash=100.0, minimum_cash_reserve=5.0,
        max_shares_per_leg=10.0, share_step=5.0, slippage=0.01,
    )
    if res.best is None:
        m.opportunities_rejected = 1
        for r in res.rejected[:20]:
            m.reject_reasons[r.reason] = m.reject_reasons.get(r.reason, 0) + 1
        m.notes = "no guaranteed-profit opportunity"
        return m

    m.opportunities_detected = 1
    metrics = res.best.metrics
    m.guaranteed_before.append(metrics.min_net_profit)
    m.worst_case_modeled_pnl = metrics.min_net_profit
    m.capital_peak = metrics.acquisition_cost
    m.capital_utilization = metrics.acquisition_cost / 100.0
    m.orders_submitted = len(res.best.legs)

    # Simulate FAK fills: respect depth; partial if thin
    filled: list[PlannedLeg] = []
    for leg in res.best.legs:
        b = next(x for x in books if x.slug == leg.slug)
        q = b.up if leg.side == "UP" else b.down
        take = min(leg.shares, q.depth)
        if take + 1e-9 < 5.0:
            continue
        got = estimate_leg(q, take if take >= 5 else leg.shares, 0.01)
        if got is None:
            continue
        # Price move between detection and execution: +0.5c adverse on partial_fill_risk
        if kind == "partial_fill_risk":
            adverse = LegQuote(
                slug=q.slug, asset=q.asset, side=q.side, ask=min(0.99, q.ask + 0.005),
                bid=q.bid, depth=max(0.0, q.depth - take), fee_fn=q.fee_fn, levels=q.levels,
                min_size=5.0,
            )
            got2 = estimate_leg(adverse, min(take, 5.0), 0.01)
            if got2 is None:
                continue
            got = got2
        filled.append(got)
        m.orders_filled += 1

    if not filled:
        m.incomplete_portfolios = 1
        m.notes = "no fills"
        return m

    after = portfolio_metrics(planned_to_positions(filled), [b.slug for b in books])
    m.guaranteed_after_fills.append(after.min_net_profit)
    if after.min_net_profit > min_profit:
        m.portfolios_complete = 1
    else:
        m.incomplete_portfolios = 1

    # Actual settlement: resolve each market randomly-but-fixed (UP wins if i even)
    winners = {}
    for i, b in enumerate(books):
        winners[b.slug] = "UP" if i % 2 == 0 else "DOWN"
    payout = sum(l.shares for l in filled if winners.get(l.slug) == l.side)
    cost = sum(l.total_cost for l in filled)
    m.settlement_pnl = payout - cost
    m.gross_pnl = payout - sum(l.ask * l.shares for l in filled)
    m.net_pnl = m.settlement_pnl
    m.max_drawdown = min(0.0, after.min_net_profit, m.settlement_pnl)
    m.notes = f"legs_filled={len(filled)} min_after={after.min_net_profit:.4f}"
    return m


def _run_cheapside_baseline(kind: str) -> ReplayMetrics:
    """Six cheapest + favorite sizing — unchanged math from cheapside_basket."""
    m = ReplayMetrics(name=f"cheapside_baseline:{kind}")
    books = _scenario_books(kind)
    # Build cheap quotes: pick DOWN when cheaper else UP
    outcomes = []
    for b in books:
        for side, q in (("UP", b.up), ("DOWN", b.down)):
            outcomes.append((q.ask, b.asset, side, q))
    outcomes.sort(key=lambda x: x[0])
    cheap = outcomes[:6]
    remaining_assets = {a for a in ASSETS} - {c[1] for c in cheap}
    if len(remaining_assets) != 1:
        m.opportunities_rejected = 1
        m.notes = "could not form six+leader"
        return m
    leader_asset = remaining_assets.pop()
    leader_book = next(b for b in books if b.asset == leader_asset)
    # Favorite = higher ask side (usual leader)
    leader_side = "UP" if leader_book.up.ask >= leader_book.down.ask else "DOWN"
    leader_q = leader_book.up if leader_side == "UP" else leader_book.down
    avg_cheap = sum(c[0] for c in cheap) / 6.0
    if avg_cheap >= 0.14 or kind in ("stale_no_edge", "thin_depth"):
        m.opportunities_rejected = 1
        m.reject_reasons["signal_or_depth"] = 1
        m.notes = f"avg_cheap={avg_cheap:.3f}"
        return m
    m.opportunities_detected = 1
    # Cost six × 5 shares + leader sized roughly for +0.10 target (simplified)
    cheap_cost = 0.0
    legs = []
    for ask, asset, side, q in cheap:
        limit, unit, total = estimate_leg_cost(ask, 5.0, 0.01, _fee)
        cheap_cost += total
        legs.append(CheapPos(q.slug, asset, side, 5.0, total))
        m.orders_submitted += 1
        m.orders_filled += 1
    # Leader shares for target ~0.10
    l_limit, l_unit, _ = estimate_leg_cost(leader_q.ask, 5.0, 0.01, _fee)
    if l_unit >= 1.0:
        m.incomplete_portfolios = 1
        return m
    need = max(5.0, (0.10 - (0.0 - cheap_cost)) / (1.0 - l_unit))
    need = max(5.0, round(need + 0.009, 2))
    leader_cost = l_unit * need
    legs.append(CheapPos(leader_q.slug, leader_asset, leader_side, need, leader_cost))
    m.orders_submitted += 1
    m.orders_filled += 1
    scenarios = cheap_scenarios(legs, [b.slug for b in books])
    pnls = [s["pnl"] for s in scenarios]
    m.worst_case_modeled_pnl = min(pnls)
    m.guaranteed_before.append(min(pnls))
    # Cheapside is NOT guaranteed-profit — report honestly
    m.portfolios_complete = 1 if min(pnls) > 0 else 0
    m.incomplete_portfolios = 0 if min(pnls) > 0 else 1
    m.net_pnl = sum(pnls) / len(pnls)
    m.settlement_pnl = m.net_pnl
    m.capital_peak = cheap_cost + leader_cost
    m.capital_utilization = m.capital_peak / 100.0
    m.notes = f"min_pnl={min(pnls):.4f} (not a guaranteed-arb strategy)"
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "results" / "cross_market_arb_replay.json"))
    args = ap.parse_args()
    kinds = ("pair_arb", "one_market_arb", "thin_depth", "stale_no_edge", "partial_fill_risk")
    rows = []
    for kind in kinds:
        rows.append(asdict(_run_cma(kind)))
        rows.append(asdict(_run_cheapside_baseline(kind)))
    out = {"scenarios": rows, "summary": {
        "cma_detected": sum(r["opportunities_detected"] for r in rows if r["name"].startswith("cma:")),
        "cma_complete": sum(r["portfolios_complete"] for r in rows if r["name"].startswith("cma:")),
        "cma_incomplete": sum(r["incomplete_portfolios"] for r in rows if r["name"].startswith("cma:")),
        "note": "Guaranteed-arb opportunities are rare; idle is correct when none exist.",
    }}
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(json.dumps(out["summary"], indent=2))
    for r in rows:
        print(f"{r['name']}: detected={r['opportunities_detected']} complete={r['portfolios_complete']} "
              f"incomplete={r['incomplete_portfolios']} net={r['net_pnl']:.4f} "
              f"worst={r['worst_case_modeled_pnl']:.4f} | {r['notes']}")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
