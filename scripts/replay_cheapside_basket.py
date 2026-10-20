#!/usr/bin/env python3
"""Scenario replay comparing Cheapside basket variants under realistic fees/FAK fills.

The archive under data/replay/ currently has single-asset BTC book dumps, not a
synchronized seven-market depth tape. Until a multi-asset book archive exists,
this script runs deterministic synthetic scenarios (stale books, favourite blow-off,
partial FAK fills, cash shortfalls, quote skew) for three policy variants:

  A) baseline — signal + average trigger only (gates loosened)
  B) gates — favourite/cash/time/book preflight
  C) full — gates + abort monitoring + incomplete-basket handling (current defaults)

Usage:
  .venv/bin/python scripts/replay_cheapside_basket.py
  .venv/bin/python scripts/replay_cheapside_basket.py --out data/replay/cheapside_basket_replay.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy import cheapside as cs
from troll_poly_bot.strategy.cheapside import CheapSideConfig, CheapSideEngine
from troll_poly_bot.strategy.cheapside_basket import BasketState, leader_shares_for_target
from troll_poly_bot.strategy.engine import EngineConfig, Evaluation
from troll_poly_bot.types import BookLevel, OrderBook

T0 = 1_800_000_000_000.0
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
FEE = FeeSchedule()
MODEL = ROOT / "data/replay/cheapside_model.json"


@dataclass
class VariantResult:
    name: str
    baskets_armed: int = 0
    baskets_complete: int = 0
    baskets_aborted: int = 0
    orphan_events: int = 0
    orders: int = 0
    fills: int = 0
    net_pnl: float = 0.0
    worst_basket_loss: float = 0.0
    max_drawdown: float = 0.0
    capital_peak: float = 0.0
    notes: str = ""


def _book(token: str, bid: float, ask: float, size: float = 200.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)],
                     asks=[BookLevel(ask, size), BookLevel(round(min(0.99, ask + 0.01), 2), size)])


def _meta(asset: str, open_ts: float = T0) -> MarketMeta:
    return MarketMeta(market=Market(condition_id=f"c-{asset}", asset=asset,
                                    yes_token_id=f"{asset}-UP", no_token_id=f"{asset}-DN",
                                    strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                                    slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}",
                                    min_size=5.0),
                      resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)


def _cfg(variant: str) -> CheapSideConfig:
    common = dict(
        enabled=True, model_path=str(MODEL), portfolio_basket_enabled=True,
        portfolio_basket_market_count=7, portfolio_basket_buy_count=6,
        portfolio_basket_shares=5.0, portfolio_basket_max_average_ask=0.20,
        portfolio_basket_target_pnl_usdc=0.10, assets=",".join(ASSETS),
        max_order_usdc=50.0, max_spread=0.03, max_slippage=0.01, max_quote_age_ms=1000.0,
        portfolio_min_time_remaining_s=30.0,
    )
    if variant == "A":
        # Loosen gates to approximate the prior arm-on-average behaviour.
        common.update(
            portfolio_basket_min_leader_ask=0.01, portfolio_basket_max_leader_ask=0.99,
            portfolio_basket_abort_leader_ask=0.99, portfolio_basket_max_leader_unit_cost=0.99,
            portfolio_basket_arm_min_time_remaining_s=15.0,
            portfolio_basket_max_notional_usdc=0.0, portfolio_basket_max_leader_notional_usdc=0.0,
            portfolio_basket_max_all_lose_usdc=0.0, portfolio_basket_cash_buffer_usdc=0.0,
            portfolio_basket_max_cash_utilization=1.0,
        )
    elif variant == "B":
        common.update(
            portfolio_basket_min_leader_ask=0.55, portfolio_basket_max_leader_ask=0.78,
            portfolio_basket_abort_leader_ask=0.82, portfolio_basket_max_leader_unit_cost=0.80,
            portfolio_basket_arm_min_time_remaining_s=120.0,
            portfolio_basket_execution_abort=False,  # preflight only
        )
    else:  # C full
        common.update(
            portfolio_basket_min_leader_ask=0.55, portfolio_basket_max_leader_ask=0.78,
            portfolio_basket_abort_leader_ask=0.82, portfolio_basket_max_leader_unit_cost=0.80,
            portfolio_basket_arm_min_time_remaining_s=120.0,
            portfolio_basket_execution_abort=True,
        )
    return CheapSideConfig(**common)


def _engine(variant: str, balance: float = 200.0) -> CheapSideEngine:
    rc = RiskConfig(max_position_usdc=200.0, max_total_exposure_usdc=1e9,
                    max_concurrent_positions=20, max_asset_exposure_usdc=200.0,
                    max_epoch_exposure_usdc=200.0, max_daily_loss_usdc=0.0)
    scfg = _cfg(variant)
    scfg.apply_budget(rc)
    return CheapSideEngine(EngineConfig(strategy="cheapside"), scfg, RiskManager(cfg=rc, starting_balance=balance),
                           FeatureEngine())


def _seed(e: CheapSideEngine, now: float, cheap_ask: float, leader_ask: float,
          depth: float = 200.0, age_ms: float = 40.0, skew_ms: float = 0.0):
    for i, a in enumerate(ASSETS):
        meta = _meta(a)
        t = now + (skew_ms if i == len(ASSETS) - 1 else 0.0)
        if a == "HYPE":
            up, dn = _book(meta.market.yes_token_id, leader_ask - 0.02, leader_ask, depth), \
                     _book(meta.market.no_token_id, max(0.01, 1 - leader_ask - 0.02),
                           max(0.02, 1 - leader_ask), depth)
        else:
            up, dn = _book(meta.market.yes_token_id, 0.88, 0.90, depth), \
                     _book(meta.market.no_token_id, max(0.01, cheap_ask - 0.02), cheap_ask, depth)
        ages = {meta.market.yes_token_id: age_ms, meta.market.no_token_id: age_ms}
        e.market_quotes[meta.market.slug] = {
            "t": t, "meta": meta, "up": up, "down": dn, "valid": age_ms <= 1000.0,
            "quote_ages": ages}


def _fak_fill(e: CheapSideEngine, ev: Evaluation, fill_frac: float = 1.0) -> float:
    """Apply a partial/full paper fill at expected price + fee."""
    if ev is None or ev.size is None:
        return 0.0
    size = float(ev.size) * fill_frac
    if size <= 0:
        return 0.0
    px = float(ev.expected_price or ev.limit_price)
    fee = FEE.taker_fee_per_share(px) * size
    e.on_fill_detail(ev.slug, ev.side, px, size, fee, T0)
    e.risk.open[ev.slug] = SimpleNamespace(side=ev.side, shares=size, usdc=px * size + fee,
                                           asset=ev.asset, epoch=int(T0 // 1000))
    return px * size + fee


def _settle_basket(e: CheapSideEngine, plan: dict, cheap_win: bool, leader_win: bool) -> float:
    """cheap_win True => each cheap side wins (worst for hedge thesis)."""
    pnl = 0.0
    for leg in plan.get("cheap", []):
        inv = e.inventory.get(leg["slug"], {}).get(leg["side"], {})
        sh, cost = float(inv.get("shares", 0.0)), float(inv.get("cost", 0.0))
        pnl += (sh if cheap_win else 0.0) - cost
    leader = plan.get("leader") or {}
    inv = e.inventory.get(leader.get("slug", ""), {}).get(leader.get("side", ""), {})
    sh, cost = float(inv.get("shares", 0.0)), float(inv.get("cost", 0.0))
    pnl += (sh if leader_win else 0.0) - cost
    return pnl


def run_scenario(variant: str, name: str, *, cheap_ask=0.10, leader_ask=0.70,
                 seconds_into=100.0, fill_frac=1.0, blowoff_ask=None,
                 balance=200.0, age_ms=40.0, skew_ms=0.0, depth=200.0,
                 outcome="protected") -> dict:
    e = _engine(variant, balance=balance)
    cs.twap_fair_value = lambda **kw: SimpleNamespace(p_up=0.5)
    now = T0 + seconds_into * 1000.0
    _seed(e, now, cheap_ask, leader_ask, depth=depth, age_ms=age_ms, skew_ms=skew_ms)
    spent = 0.0
    orders = fills = 0
    # One market evaluation per tick (matches live scan cadence more closely).
    for tick in range(80):
        t = now + tick * 200.0
        # Blow the favourite after a couple of cheap fills so full policy can abort.
        if blowoff_ask is not None and tick == 3:
            _seed(e, t, cheap_ask, blowoff_ask, depth=depth, age_ms=age_ms)
        a = ASSETS[tick % len(ASSETS)]
        meta = _meta(a)
        snap = e.market_quotes[meta.market.slug]
        left = meta.market.seconds_remaining(t)
        ev0 = Evaluation(slug=meta.market.slug, asset=a, seconds_left=left, reason=None)
        out = e._portfolio_basket(ev0, meta, snap["up"], snap["down"], t,
                                  balance=balance - spent, book_age_ms=age_ms,
                                  quote_ages=snap["quote_ages"])
        if out is None:
            continue
        orders += 1
        cost = _fak_fill(e, out, fill_frac=fill_frac)
        if cost > 0:
            fills += 1
            spent += cost
        e.risk.pending.clear()

    plans = list(e.basket_plans.values())
    armed = len(plans)
    complete = sum(1 for p in plans if p.get("complete"))
    aborted = sum(1 for p in plans if p.get("aborted"))
    orphans = sum(1 for ev in e.basket_events if ev.get("event") == "ORPHAN_EXPOSURE")
    pnl = 0.0
    for p in plans:
        if outcome == "protected":
            pnl += _settle_basket(e, p, cheap_win=False, leader_win=True)
        elif outcome == "all_lose":
            pnl += _settle_basket(e, p, cheap_win=False, leader_win=False)
        else:
            pnl += _settle_basket(e, p, cheap_win=True, leader_win=False)
    return {
        "scenario": name, "variant": variant, "armed": armed, "complete": complete,
        "aborted": aborted, "orphan_events": orphans, "orders": orders, "fills": fills,
        "spent": round(spent, 4), "settlement_pnl": round(pnl, 4),
        "events": [ev.get("event") for ev in e.basket_events],
        "states": [p.get("state") for p in plans],
    }


SCENARIOS = [
    dict(name="healthy_protected", cheap_ask=0.10, leader_ask=0.70, seconds_into=100.0,
         outcome="protected"),
    dict(name="late_window_reject", cheap_ask=0.10, leader_ask=0.70, seconds_into=200.0,
         outcome="protected"),
    dict(name="favorite_too_rich_arm", cheap_ask=0.10, leader_ask=0.85, seconds_into=100.0,
         outcome="protected"),
    dict(name="blowoff_mid_execution", cheap_ask=0.10, leader_ask=0.70, seconds_into=100.0,
         blowoff_ask=0.86, fill_frac=1.0, outcome="all_lose"),
    dict(name="partial_fak", cheap_ask=0.10, leader_ask=0.70, seconds_into=100.0,
         fill_frac=0.6, outcome="protected"),
    dict(name="thin_depth", cheap_ask=0.10, leader_ask=0.70, seconds_into=100.0,
         depth=3.0, outcome="protected"),
    dict(name="cash_short", cheap_ask=0.12, leader_ask=0.72, seconds_into=100.0,
         balance=8.0, outcome="protected"),
    dict(name="stale_quotes", cheap_ask=0.10, leader_ask=0.70, seconds_into=100.0,
         age_ms=2000.0, outcome="protected"),
    dict(name="quote_skew", cheap_ask=0.10, leader_ask=0.70, seconds_into=100.0,
         skew_ms=1500.0, outcome="protected"),
    dict(name="all_seven_lose", cheap_ask=0.10, leader_ask=0.70, seconds_into=100.0,
         outcome="all_lose"),
]


def summarize(rows: list[dict], variant: str) -> VariantResult:
    subset = [r for r in rows if r["variant"] == variant]
    vr = VariantResult(name=variant)
    vr.baskets_armed = sum(r["armed"] for r in subset)
    vr.baskets_complete = sum(r["complete"] for r in subset)
    vr.baskets_aborted = sum(r["aborted"] for r in subset)
    vr.orphan_events = sum(r["orphan_events"] for r in subset)
    vr.orders = sum(r["orders"] for r in subset)
    vr.fills = sum(r["fills"] for r in subset)
    pnls = [r["settlement_pnl"] for r in subset]
    vr.net_pnl = round(sum(pnls), 4)
    vr.worst_basket_loss = round(min(pnls), 4) if pnls else 0.0
    # Simple path drawdown on cumulative scenario PnL.
    eq = peak = dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    vr.max_drawdown = round(dd, 4)
    vr.capital_peak = round(max((r["spent"] for r in subset), default=0.0), 4)
    return vr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data/replay/cheapside_basket_replay.json"))
    args = ap.parse_args()
    if not MODEL.exists():
        print(f"missing model {MODEL}", file=sys.stderr)
        return 2
    rows = []
    for spec in SCENARIOS:
        for variant in ("A", "B", "C"):
            rows.append(run_scenario(variant, **spec))
    summary = {v: asdict(summarize(rows, v)) for v in ("A", "B", "C")}
    # Completion / abort rates
    for v, s in summary.items():
        armed = max(s["baskets_armed"], 1)
        s["completion_rate"] = round(s["baskets_complete"] / armed, 4)
        s["abort_rate"] = round(s["baskets_aborted"] / armed, 4)
    doc = {
        "limitation": (
            "No synchronized seven-market book-depth archive is available in data/replay "
            "(only btc5m_books.pkl). Results are synthetic scenario replays with venue fee "
            "schedule, FAK partial fills, and executable asks — not claimed live profitability."
        ),
        "summary": summary,
        "rows": rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"wrote {out}")
    print("LIMITATION:", doc["limitation"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
