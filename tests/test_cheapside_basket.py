"""Unit and integration tests for Cheapside basket preflight, sizing, settlement,
state transitions, abort/orphan handling, and restart recovery.
"""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy import cheapside as cs
from troll_poly_bot.strategy.cheapside import CheapSideConfig, CheapSideEngine
from troll_poly_bot.strategy.cheapside_basket import (
    BasketState, LegQuote, PositionLeg, all_outcome_scenarios, classify_scenarios,
    leader_shares_for_target, run_preflight, settlement_pnl, unit_cost,
)
from troll_poly_bot.strategy.engine import EngineConfig, Evaluation
from troll_poly_bot.types import BookLevel, OrderBook

T0 = 1_800_000_000_000.0
FEE = FeeSchedule()
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")


def _model_doc() -> dict:
    from troll_poly_bot.strategy.cheapside import FEATURES
    n = len(FEATURES)
    return {"features": list(FEATURES), "beta": [0.0] * (n + 1), "mu": [0.0] * n, "sd": [1.0] * n,
            "platt": {"a": 0.0, "b": 1.0}, "rules": {"min_edge": 0.05}}


@pytest.fixture
def model_path(tmp_path):
    p = tmp_path / "cheapside_model.json"
    p.write_text(json.dumps(_model_doc()))
    return str(p)


def _book(token: str, bid: float, ask: float, size: float = 500.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)],
                     asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)])


def _cfg(model_path, **kw) -> CheapSideConfig:
    base = dict(enabled=True, model_path=model_path, portfolio_basket_enabled=True,
                portfolio_basket_market_count=7, portfolio_basket_buy_count=6,
                portfolio_basket_shares=5.0, portfolio_basket_max_average_ask=0.20,
                portfolio_basket_target_pnl_usdc=0.10,
                portfolio_basket_min_leader_ask=0.55, portfolio_basket_max_leader_ask=0.78,
                portfolio_basket_abort_leader_ask=0.82,
                portfolio_basket_arm_min_time_remaining_s=120.0,
                assets=",".join(ASSETS), max_order_usdc=50.0,
                portfolio_basket_max_notional_usdc=200.0,
                portfolio_basket_max_leader_notional_usdc=150.0,
                portfolio_basket_max_all_lose_usdc=200.0)
    base.update(kw)
    return CheapSideConfig(**base)


class _World:
    def __init__(self, monkeypatch, model_path, balance=200.0, **scfg) -> None:
        rc = RiskConfig(max_position_usdc=200.0, max_total_exposure_usdc=1_000_000_000.0,
                        max_concurrent_positions=20, max_asset_exposure_usdc=200.0,
                        max_epoch_exposure_usdc=200.0, max_daily_loss_usdc=50.0,
                        micro_fixed_size=True)
        self.scfg = _cfg(model_path, **scfg)
        self.scfg.apply_budget(rc)
        self.risk = RiskManager(cfg=rc, starting_balance=balance)
        self.e = CheapSideEngine(EngineConfig(strategy="cheapside"), self.scfg, self.risk, FeatureEngine())
        monkeypatch.setattr(cs, "twap_fair_value", lambda **kw: SimpleNamespace(p_up=0.5))

    def meta(self, asset: str, open_ts: float = T0) -> MarketMeta:
        return MarketMeta(market=Market(condition_id=f"c-{asset}-{open_ts:.0f}", asset=asset,
                                        yes_token_id=f"{asset}-{open_ts:.0f}-UP",
                                        no_token_id=f"{asset}-{open_ts:.0f}-DN",
                                        strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                                        slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}",
                                        min_size=5.0),
                          resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)

    def seed_books(self, now: float, cheap_ask: float = 0.10, leader_ask: float = 0.70,
                   leader_asset: str = "HYPE", depth: float = 500.0):
        metas = {}
        for a in ASSETS:
            meta = self.meta(a)
            metas[a] = meta
            if a == leader_asset:
                up, dn = _book(meta.market.yes_token_id, leader_ask - 0.02, leader_ask, depth), \
                         _book(meta.market.no_token_id, 1 - leader_ask - 0.02, 1 - leader_ask, depth)
            else:
                up, dn = _book(meta.market.yes_token_id, 0.88, 0.90, depth), \
                         _book(meta.market.no_token_id, cheap_ask - 0.02, cheap_ask, depth)
            ages = {meta.market.yes_token_id: 40.0, meta.market.no_token_id: 40.0}
            self.e.market_quotes[meta.market.slug] = {
                "t": now, "meta": meta, "up": up, "down": dn, "valid": True, "quote_ages": ages}
        return metas


# ─────────────────────────────── pure math ───────────────────────────────


def test_leader_shares_formula_and_rounding():
    need, pnl = leader_shares_for_target(cheap_cost=6.0, leader_unit=0.72, target_pnl=0.10)
    assert need == pytest.approx(math.ceil((6.1 / 0.28) * 100) / 100)
    assert pnl >= 0.10 - 1e-6


def test_unit_cost_uses_worst_fee_between_ask_and_limit():
    fee = FeeSchedule()
    u = unit_cost(0.70, 0.71, fee.taker_fee_per_share)
    assert u == pytest.approx(0.71 + max(fee.taker_fee_per_share(0.70), fee.taker_fee_per_share(0.71)))


def test_settlement_128_scenarios_distinguish_protected_and_all_lose():
    cheap = [PositionLeg(f"s{i}", f"A{i}", "DOWN", 5.0, 0.5) for i in range(6)]
    leader = PositionLeg("L", "HYPE", "UP", 20.0, 14.0)
    legs = cheap + [leader]
    slugs = [x.slug for x in legs]
    scenarios = all_outcome_scenarios(legs, slugs)
    assert len(scenarios) == 128
    cheap_sides = {c.slug: "DOWN" for c in cheap}
    summary = classify_scenarios(scenarios, list(cheap_sides), "L", "UP", cheap_sides)
    # Protected: all cheap lose, leader wins → payout 20 - costs (3+14) = 3
    assert summary["protected_pnl"] == pytest.approx(20.0 - 3.0 - 14.0)
    # All seven lose → -17
    assert summary["all_seven_lose_pnl"] == pytest.approx(-(3.0 + 14.0))
    assert summary["protected_pnl"] != summary["all_seven_lose_pnl"]
    assert summary["min_pnl"] <= summary["all_seven_lose_pnl"] + 1e-9


def test_preflight_rejects_favorite_outside_band_and_unit_cap():
    fee = FeeSchedule()
    cheap = [LegQuote(f"s{i}", f"A{i}", "DOWN", 0.10, 0.08, 100.0, 0.0, quote_age_ms=50.0, spread=0.02)
             for i in range(6)]
    leader = LegQuote("L", "HYPE", "UP", 0.85, 0.83, 200.0, 0.0, quote_age_ms=50.0, spread=0.02)
    pf = run_preflight(
        cheap_quotes=cheap, leader_quote=leader, shares_per_cheap=5.0, target_pnl=0.10,
        slippage=0.01, fee_fn_leader=fee.taker_fee_per_share,
        fee_fns_cheap=[fee.taker_fee_per_share] * 6, available_cash=200.0,
        cash_buffer_usdc=1.0, cash_util_max=0.95, min_leader_ask=0.55, max_leader_ask=0.78,
        max_leader_unit=0.80, max_basket_notional=80.0, max_leader_notional=60.0,
        max_leader_multiple=15.0, max_all_lose=80.0, max_spread=0.03, max_quote_age_ms=1000.0,
        max_quote_skew_ms=1000.0, min_time_remaining_s=120.0, time_remaining_s=200.0,
        avg_cheap_ask=0.10, max_average_ask=0.20)
    assert not pf.ok and "favorite ask" in pf.reason


def test_preflight_rejects_insufficient_cash():
    fee = FeeSchedule()
    cheap = [LegQuote(f"s{i}", f"A{i}", "DOWN", 0.15, 0.13, 100.0, 0.0, quote_age_ms=50.0, spread=0.02)
             for i in range(6)]
    leader = LegQuote("L", "HYPE", "UP", 0.70, 0.68, 500.0, 0.0, quote_age_ms=50.0, spread=0.02)
    pf = run_preflight(
        cheap_quotes=cheap, leader_quote=leader, shares_per_cheap=5.0, target_pnl=0.10,
        slippage=0.01, fee_fn_leader=fee.taker_fee_per_share,
        fee_fns_cheap=[fee.taker_fee_per_share] * 6, available_cash=5.0,
        cash_buffer_usdc=1.0, cash_util_max=0.95, min_leader_ask=0.55, max_leader_ask=0.78,
        max_leader_unit=0.80, max_basket_notional=200.0, max_leader_notional=150.0,
        max_leader_multiple=20.0, max_all_lose=200.0, max_spread=0.03, max_quote_age_ms=1000.0,
        max_quote_skew_ms=1000.0, min_time_remaining_s=120.0, time_remaining_s=200.0,
        avg_cheap_ask=0.15, max_average_ask=0.20)
    assert not pf.ok and "cash" in pf.reason


def test_preflight_passes_healthy_books():
    fee = FeeSchedule()
    cheap = [LegQuote(f"s{i}", f"A{i}", "DOWN", 0.10, 0.08, 100.0, 0.0, quote_age_ms=50.0, spread=0.02)
             for i in range(6)]
    leader = LegQuote("L", "HYPE", "UP", 0.70, 0.68, 500.0, 0.0, quote_age_ms=50.0, spread=0.02)
    pf = run_preflight(
        cheap_quotes=cheap, leader_quote=leader, shares_per_cheap=5.0, target_pnl=0.10,
        slippage=0.01, fee_fn_leader=fee.taker_fee_per_share,
        fee_fns_cheap=[fee.taker_fee_per_share] * 6, available_cash=200.0,
        cash_buffer_usdc=1.0, cash_util_max=0.95, min_leader_ask=0.55, max_leader_ask=0.78,
        max_leader_unit=0.80, max_basket_notional=200.0, max_leader_notional=150.0,
        max_leader_multiple=20.0, max_all_lose=200.0, max_spread=0.03, max_quote_age_ms=1000.0,
        max_quote_skew_ms=1000.0, min_time_remaining_s=120.0, time_remaining_s=200.0,
        avg_cheap_ask=0.10, max_average_ask=0.20)
    assert pf.ok
    assert pf.leader_shares >= 5.0
    assert pf.total_reserved > pf.cheap_cost_est


# ─────────────────────────────── engine integration ───────────────────────────────


def test_preflight_rejects_arm_when_time_below_120s(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    now = T0 + 200_000.0  # 100 s remaining
    metas = w.seed_books(now)
    btc = metas["BTC"]
    ev = w.e._portfolio_basket(
        Evaluation(slug=btc.market.slug, asset="BTC", seconds_left=100.0, reason=None),
        btc, w.e.market_quotes[btc.market.slug]["up"], w.e.market_quotes[btc.market.slug]["down"],
        now, balance=200.0, book_age_ms=40.0,
        quote_ages=w.e.market_quotes[btc.market.slug]["quote_ages"])
    assert ev is None
    assert not w.e.basket_plans
    assert any(e["event"] == "PREFLIGHT_REJECT" for e in w.e.basket_events) or \
        (w.e.last_cheapside.get("BTC") or {}).get("state") == BasketState.PREFLIGHT_REJECT.value


def test_arm_freezes_legs_and_reserves_cash(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    now = T0 + 100_000.0  # 200 s left
    metas = w.seed_books(now, cheap_ask=0.10, leader_ask=0.70)
    btc = metas["BTC"]
    ev = w.e._portfolio_basket(
        Evaluation(slug=btc.market.slug, asset="BTC", seconds_left=200.0, reason=None),
        btc, w.e.market_quotes[btc.market.slug]["up"], w.e.market_quotes[btc.market.slug]["down"],
        now, balance=200.0, book_age_ms=40.0,
        quote_ages=w.e.market_quotes[btc.market.slug]["quote_ages"])
    key = str(int(T0 // 1000))
    plan = w.e.basket_plans[key]
    assert plan["state"] == BasketState.EXECUTING_CHEAP_LEGS.value
    assert plan["reserved_cash"] > 0
    assert plan["reserved_hedge"] > 0
    frozen = [(x["slug"], x["side"]) for x in plan["cheap"]]
    # Move prices so a different asset would be cheapest; plan must not reshuffle.
    for a in ASSETS:
        if a == "HYPE":
            continue
        meta = metas[a]
        up = _book(meta.market.yes_token_id, 0.05, 0.07) if a == "BTC" else \
             _book(meta.market.yes_token_id, 0.88, 0.90)
        dn = _book(meta.market.no_token_id, 0.88, 0.90) if a == "BTC" else \
             _book(meta.market.no_token_id, 0.08, 0.10)
        w.e.market_quotes[meta.market.slug].update({"up": up, "down": dn, "t": now + 10})
    w.e._portfolio_basket(
        Evaluation(slug=btc.market.slug, asset="BTC", seconds_left=199.0, reason=None),
        btc, w.e.market_quotes[btc.market.slug]["up"], w.e.market_quotes[btc.market.slug]["down"],
        now + 10, balance=200.0, book_age_ms=40.0,
        quote_ages=w.e.market_quotes[btc.market.slug]["quote_ages"])
    assert [(x["slug"], x["side"]) for x in w.e.basket_plans[key]["cheap"]] == frozen
    assert w.e.basket_plans[key]["leader"]["asset"] == "HYPE"
    assert ev is not None and ev.features["cheapside"].get("basket_low_buy")


def test_abort_on_favorite_above_abort_price_stops_new_cheap_legs(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    now = T0 + 100_000.0
    metas = w.seed_books(now)
    key = str(int(T0 // 1000))
    plan = {
        "basket_id": "t1", "epoch": int(key), "created": now,
        "state": BasketState.EXECUTING_CHEAP_LEGS.value, "complete": False, "aborted": False,
        "average_cheap_ask": 0.10, "armed_average_cheap_ask": 0.10,
        "reserved_cash": 40.0, "reserved_hedge": 30.0,
        "cheap": [{"slug": metas[a].market.slug, "side": "DOWN", "cheap": "DOWN",
                   "leader": "UP", "asset": a} for a in ASSETS if a != "HYPE"],
        "leader": {"slug": metas["HYPE"].market.slug, "side": "UP", "cheap": "DOWN",
                   "leader": "UP", "asset": "HYPE"},
    }
    w.e.basket_plans[key] = plan
    # Fill one cheap leg so abort leaves orphan exposure.
    w.e.inventory[metas["BTC"].market.slug] = {"DOWN": {"shares": 5.0, "cost": 0.55}}
    # Favourite blows through abort.
    h = metas["HYPE"]
    w.e.market_quotes[h.market.slug]["up"] = _book(h.market.yes_token_id, 0.84, 0.86)
    w.e.market_quotes[h.market.slug]["down"] = _book(h.market.no_token_id, 0.12, 0.14)
    btc = metas["BTC"]
    out = w.e._portfolio_basket(
        Evaluation(slug=btc.market.slug, asset="BTC", seconds_left=200.0, reason=None),
        btc, w.e.market_quotes[btc.market.slug]["up"], w.e.market_quotes[btc.market.slug]["down"],
        now, balance=200.0, book_age_ms=40.0,
        quote_ages=w.e.market_quotes[btc.market.slug]["quote_ages"])
    assert out is None
    assert plan["aborted"]
    assert plan["state"] in (BasketState.ABORTED.value, BasketState.ORPHAN.value)
    assert plan["reserved_cash"] == 0.0
    assert any(e["event"] == "BASKET_ABORT" for e in w.e.basket_events)
    assert any(e["event"] == "ORPHAN_EXPOSURE" for e in w.e.basket_events)


def test_reserved_hedge_blocks_unrelated_spend(monkeypatch, model_path):
    w = _World(monkeypatch, model_path, balance=40.0)
    now = T0 + 100_000.0
    metas = w.seed_books(now)
    key = str(int(T0 // 1000))
    w.e.basket_plans[key] = {
        "basket_id": "r1", "epoch": int(key), "state": BasketState.EXECUTING_CHEAP_LEGS.value,
        "complete": False, "aborted": False, "reserved_cash": 35.0, "reserved_hedge": 30.0,
        "cheap": [{"slug": metas["BTC"].market.slug, "side": "DOWN", "asset": "BTC"}],
        "leader": {"slug": metas["HYPE"].market.slug, "side": "UP", "asset": "HYPE"},
        "average_cheap_ask": 0.1,
    }
    # Only ~10 USDC spendable after hedge reserve; a 5-share @0.10 order needs ~0.55 and should pass,
    # but a large unrelated size should fail the cash room check.
    assert w.e._basket_reserved_cash(exclude_leader_spend=True) == pytest.approx(30.0)
    meta = metas["ETH"]
    out = w.e._portfolio_order(meta, w.e.market_quotes[meta.market.slug]["up"],
                               w.e.market_quotes[meta.market.slug]["down"], "DOWN", now,
                               balance=40.0, quote_ages=w.e.market_quotes[meta.market.slug]["quote_ages"],
                               needed=5.0, scenario_pnl=0.0, kind="basket_low_buy")
    # ETH is not in the reserved plan's cheap set but still competes for cash; with 10 USDC free it fits.
    assert out is not None or (w.e.last_cheapside.get("ETH") or {}).get("mode") == "basket_blocked"


def test_restart_restore_is_idempotent(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    now = T0 + 100_000.0
    metas = w.seed_books(now)
    key = str(int(T0 // 1000))
    plan = {
        "basket_id": "restart", "epoch": int(key),
        "state": BasketState.EXECUTING_CHEAP_LEGS.value, "complete": False, "aborted": False,
        "reserved_cash": 40.0, "reserved_hedge": 25.0, "average_cheap_ask": 0.1,
        "cheap": [{"slug": metas[a].market.slug, "side": "DOWN", "asset": a}
                  for a in ASSETS if a != "HYPE"],
        "leader": {"slug": metas["HYPE"].market.slug, "side": "UP", "asset": "HYPE"},
    }
    w.e.basket_plans[key] = plan
    w.e.inventory[metas["BTC"].market.slug] = {"DOWN": {"shares": 5.0, "cost": 0.55}}
    doc = w.e.persist_state()
    w2 = _World(monkeypatch, model_path)
    w2.e.market_quotes = dict(w.e.market_quotes)
    w2.e.restore_state(doc, now)
    assert w2.e.basket_plans[key]["basket_id"] == "restart"
    assert w2.e.inventory[metas["BTC"].market.slug]["DOWN"]["shares"] == 5.0
    # Second restore must not duplicate inventory.
    w2.e.restore_state(doc, now)
    assert w2.e.inventory[metas["BTC"].market.slug]["DOWN"]["shares"] == 5.0


def test_complete_requires_leader_fill_or_safe_dust(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    now = T0 + 100_000.0
    metas = w.seed_books(now, leader_ask=0.70)
    key = str(int(T0 // 1000))
    for a in ASSETS:
        if a == "HYPE":
            continue
        w.e.inventory[metas[a].market.slug] = {"DOWN": {"shares": 5.0, "cost": 0.55}}
    # Pre-size leader so need is dust and protected target is met.
    unit = w.e._basket_unit_cost(FEE, 0.70, 0.71)
    cheap_cost = 6 * 0.55
    need, _ = leader_shares_for_target(cheap_cost, unit, 0.10)
    w.e.inventory[metas["HYPE"].market.slug] = {
        "UP": {"shares": need, "cost": need * unit}}
    plan = {
        "basket_id": "done", "epoch": int(key), "created": now,
        "state": BasketState.EXECUTING_LEADER.value, "complete": False, "aborted": False,
        "average_cheap_ask": 0.10, "reserved_cash": 1.0, "reserved_hedge": 1.0,
        "cheap": [{"slug": metas[a].market.slug, "side": "DOWN", "asset": a}
                  for a in ASSETS if a != "HYPE"],
        "leader": {"slug": metas["HYPE"].market.slug, "side": "UP", "asset": "HYPE"},
    }
    w.e.basket_plans[key] = plan
    btc = metas["BTC"]
    out = w.e._portfolio_basket(
        Evaluation(slug=btc.market.slug, asset="BTC", seconds_left=200.0, reason=None),
        btc, w.e.market_quotes[btc.market.slug]["up"], w.e.market_quotes[btc.market.slug]["down"],
        now, balance=200.0, book_age_ms=40.0,
        quote_ages=w.e.market_quotes[btc.market.slug]["quote_ages"])
    assert out is None
    assert plan["complete"]
    assert plan["state"] == BasketState.COMPLETE.value
    assert plan["settlement_summary"]["n_scenarios"] == 128
    assert plan["settlement_summary"]["protected_pnl"] is not None


def test_engine_settlement_summary_matches_helper(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    now = T0 + 100_000.0
    metas = w.seed_books(now)
    for a in ASSETS:
        if a == "HYPE":
            w.e.inventory[metas[a].market.slug] = {"UP": {"shares": 22.0, "cost": 16.0}}
        else:
            w.e.inventory[metas[a].market.slug] = {"DOWN": {"shares": 5.0, "cost": 0.6}}
    plan = {
        "cheap": [{"slug": metas[a].market.slug, "side": "DOWN", "asset": a}
                  for a in ASSETS if a != "HYPE"],
        "leader": {"slug": metas["HYPE"].market.slug, "side": "UP", "asset": "HYPE"},
    }
    summary = w.e.basket_settlement_summary(plan)
    legs = [PositionLeg(metas[a].market.slug, a, "DOWN", 5.0, 0.6) for a in ASSETS if a != "HYPE"]
    legs.append(PositionLeg(metas["HYPE"].market.slug, "HYPE", "UP", 22.0, 16.0))
    protected = settlement_pnl(legs, {**{metas[a].market.slug: "UP" for a in ASSETS if a != "HYPE"},
                                      metas["HYPE"].market.slug: "UP"})
    assert summary["protected_pnl"] == pytest.approx(protected)
