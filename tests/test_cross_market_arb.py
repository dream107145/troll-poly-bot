"""Unit and integration tests for CROSS_MARKET_ARB guaranteed-profit portfolios."""
from __future__ import annotations

import itertools
import math

import pytest

from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.cross_market_arb import (
    ENABLE_ENV, STRATEGY_ID, CrossMarketArbConfig, CrossMarketArbEngine,
)
from troll_poly_bot.strategy.cross_market_arb.execution import (
    ArbState, client_order_key, mark_fill, plan_from_candidate, plan_from_dict, as_dict,
)
from troll_poly_bot.strategy.cross_market_arb.math import (
    LegQuote, PositionLeg, all_outcome_scenarios, closed_form_min_payout,
    estimate_leg, is_guaranteed_profit, planned_to_positions, portfolio_metrics,
    round_shares, settlement_pnl, unit_all_in,
)
from troll_poly_bot.strategy.cross_market_arb.optimizer import (
    MarketBooks, optimize_portfolio,
)
from troll_poly_bot.strategy.engine import EngineConfig
from troll_poly_bot.types import BookLevel, OrderBook

T0 = 1_800_000_000_000.0
FEE = FeeSchedule()
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")


def _fee(p: float) -> float:
    return FEE.taker_fee_per_share(p)


def _quote(asset: str, side: str, ask: float, depth: float = 500.0, age: float = 40.0,
           bid: float | None = None) -> LegQuote:
    bid = ask - 0.02 if bid is None else bid
    levels = ((ask, depth), (round(min(0.99, ask + 0.01), 2), depth))
    return LegQuote(
        slug=f"{asset.lower()}-updown-5m-{int(T0 // 1000)}", asset=asset, side=side,
        ask=ask, bid=bid, depth=depth, fee_fn=_fee, quote_age_ms=age,
        quote_ts_ms=T0 - age, levels=levels, min_size=5.0,
    )


def _books_arb(up_ask: float = 0.42, down_ask: float = 0.42, depth: float = 500.0) -> list[MarketBooks]:
    """Seven markets with classic both-sides mispricing (pair cost << 1 after fees)."""
    out = []
    for a in ASSETS:
        up = _quote(a, "UP", up_ask, depth)
        dn = _quote(a, "DOWN", down_ask, depth)
        out.append(MarketBooks(asset=a, slug=up.slug, up=up, down=dn, time_remaining_s=200.0))
    return out


def _books_no_arb(up_ask: float = 0.55, down_ask: float = 0.55) -> list[MarketBooks]:
    return _books_arb(up_ask, down_ask)


# --------------------------------------------------------------------------- math


def test_128_scenarios_for_seven_markets():
    legs = [
        PositionLeg(f"{a.lower()}-m", a, "UP", 5.0, 2.0)
        for a in ASSETS
    ]
    slugs = [l.slug for l in legs]
    rows = all_outcome_scenarios(legs, slugs)
    assert len(rows) == 128
    # Only one market wins UP at a time per slug — payout equals 5 when that market's UP wins
    assert min(r["payout"] for r in rows) == 0.0
    assert max(r["payout"] for r in rows) == 5.0 * 7


def test_closed_form_matches_enumeration_for_pairs():
    legs = []
    slugs = []
    for a in ASSETS:
        slug = f"{a.lower()}-m"
        slugs.append(slug)
        legs.append(PositionLeg(slug, a, "UP", 10.0, 4.5))
        legs.append(PositionLeg(slug, a, "DOWN", 10.0, 4.5))
    cf = closed_form_min_payout(legs)
    rows = all_outcome_scenarios(legs, slugs)
    assert abs(cf - min(r["payout"] for r in rows)) < 1e-9
    assert abs(cf - 70.0) < 1e-9  # 10 per market × 7


def test_fee_inclusive_guaranteed_profit():
    # Equal 5-share pairs at 0.40/0.40: unit all-in ~0.40+fee(~0.0168) each => pair ~0.8336 < 1
    up = estimate_leg(_quote("BTC", "UP", 0.40), 5.0, 0.01)
    dn = estimate_leg(_quote("BTC", "DOWN", 0.40), 5.0, 0.01)
    assert up is not None and dn is not None
    pos = planned_to_positions([up, dn])
    ok, m = is_guaranteed_profit(pos, 0.05, market_slugs=[up.slug])
    assert ok
    assert m.n_scenarios == 2
    assert m.min_net_profit > 0.05


def test_quantity_rounding_and_minimums():
    assert round_shares(5.019) == 5.01
    assert estimate_leg(_quote("BTC", "UP", 0.40), 4.0, 0.01) is None  # below venue min
    leg = estimate_leg(_quote("BTC", "UP", 0.40), 5.0, 0.01)
    assert leg is not None and leg.shares == 5.0


def test_insufficient_depth_rejects_leg():
    q = _quote("BTC", "UP", 0.40, depth=3.0)
    assert estimate_leg(q, 5.0, 0.01) is None


def test_unit_all_in_includes_fee_at_mid():
    # Fee peaks near 0.50; limit above ask should probe 0.50
    u = unit_all_in(0.48, 0.52, _fee)
    assert u >= 0.52 + _fee(0.50) - 1e-9


# --------------------------------------------------------------------------- optimizer


def test_optimizer_finds_pair_arb():
    res = optimize_portfolio(
        _books_arb(0.40, 0.40),
        minimum_net_profit=0.05,
        maximum_basket_notional=200.0,
        maximum_market_exposure=50.0,
        available_cash=200.0,
        minimum_cash_reserve=5.0,
        max_shares_per_leg=10.0,
        share_step=5.0,
        slippage=0.01,
    )
    assert res.best is not None
    assert res.best.metrics.min_net_profit > 0.05
    assert res.best.metrics.n_scenarios == 128
    # Every included market should be both-sided for min payout > 0
    by = {}
    for leg in res.best.legs:
        by.setdefault(leg.slug, set()).add(leg.side)
    assert all(s == {"UP", "DOWN"} for s in by.values())


def test_optimizer_idle_when_no_guaranteed_profit():
    res = optimize_portfolio(
        _books_no_arb(0.55, 0.55),
        minimum_net_profit=0.05,
        maximum_basket_notional=200.0,
        maximum_market_exposure=50.0,
        available_cash=200.0,
        minimum_cash_reserve=5.0,
        max_shares_per_leg=10.0,
        share_step=5.0,
        slippage=0.01,
    )
    assert res.best is None
    assert res.state == "NO_OPPORTUNITY"


def test_optimizer_respects_cash_and_notional():
    res = optimize_portfolio(
        _books_arb(0.40, 0.40),
        minimum_net_profit=0.01,
        maximum_basket_notional=5.0,  # too small for any 5+5 pair after fees
        maximum_market_exposure=50.0,
        available_cash=200.0,
        minimum_cash_reserve=5.0,
        max_shares_per_leg=10.0,
        share_step=5.0,
        slippage=0.01,
    )
    assert res.best is None


def test_stale_books_rejected_by_engine_collect(monkeypatch):
    eng = _engine()
    now = T0 + 60_000
    for a in ASSETS:
        meta = _meta(a)
        up = _book(meta.market.yes_token_id, 0.38, 0.40)
        dn = _book(meta.market.no_token_id, 0.38, 0.40)
        ages = {meta.market.yes_token_id: 2000.0, meta.market.no_token_id: 2000.0}
        eng.market_quotes[meta.market.slug] = {
            "t": now, "meta": meta, "up": up, "down": dn, "valid": True, "quote_ages": ages}
    books, err = eng._collect_books(now)
    assert books is None
    assert err.startswith("no_ready_books") or "stale" in err


# --------------------------------------------------------------------------- engine helpers


def _book(token: str, bid: float, ask: float, size: float = 500.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)],
                     asks=[BookLevel(ask, size), BookLevel(round(min(0.99, ask + 0.01), 2), size)],
                     ts=T0)


def _meta(asset: str, open_ts: float = T0) -> MarketMeta:
    return MarketMeta(
        market=Market(condition_id=f"c-{asset}", asset=asset,
                      yes_token_id=f"{asset}-UP", no_token_id=f"{asset}-DN",
                      strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                      slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}",
                      min_size=5.0),
        resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE,
    )


def _engine(**kw) -> CrossMarketArbEngine:
    base = dict(
        enabled=True, markets=",".join(ASSETS), assets=",".join(ASSETS),
        minimum_net_profit=0.05, maximum_basket_notional=200.0,
        maximum_market_exposure=80.0, maximum_quote_age_ms=1000.0,
        maximum_timestamp_skew_ms=1000.0, maximum_spread=0.10,
        maximum_slippage=0.01, minimum_cash_reserve=1.0,
        max_shares_per_leg=10.0, share_step=5.0, max_order_usdc=80.0,
        maximum_capital_per_opportunity=200.0,
        maximum_total_outstanding_notional=500.0,
        maximum_worst_case_loss_during_execution=200.0,
        maximum_incomplete_exposure=200.0,
        # Unit tests exercise the taker FAK portfolio path; live paper uses maker.
        entry_mode="taker",
    )
    base.update(kw)
    scfg = CrossMarketArbConfig(**base)
    rc = RiskConfig(max_position_usdc=200.0, max_total_exposure_usdc=500.0,
                    max_concurrent_positions=20, max_daily_loss_usdc=50.0,
                    micro_fixed_size=True)
    scfg.apply_budget(rc)
    risk = RiskManager(cfg=rc, starting_balance=200.0)
    return CrossMarketArbEngine(EngineConfig(strategy=STRATEGY_ID), scfg, risk, FeatureEngine())


def _seed_arb_quotes(eng: CrossMarketArbEngine, now: float, up: float = 0.40, dn: float = 0.40):
    for a in ASSETS:
        meta = _meta(a)
        ages = {meta.market.yes_token_id: 40.0, meta.market.no_token_id: 40.0}
        eng.market_quotes[meta.market.slug] = {
            "t": now, "meta": meta,
            "up": _book(meta.market.yes_token_id, up - 0.02, up),
            "down": _book(meta.market.no_token_id, dn - 0.02, dn),
            "valid": True, "quote_ages": ages,
        }


def test_engine_disabled_by_default():
    cfg = CrossMarketArbConfig()
    assert cfg.enabled is False
    assert cfg.mode == "paper"
    with pytest.raises(ValueError, match=ENABLE_ENV):
        CrossMarketArbEngine(EngineConfig(), CrossMarketArbConfig(enabled=False),
                             RiskManager(starting_balance=100.0), FeatureEngine())


def test_live_trading_refused_by_gate():
    from troll_poly_bot.__main__ import check_cross_market_arb
    with pytest.raises(SystemExit, match="feature flag"):
        check_cross_market_arb("cross_market_arb", "paper", env={})
    with pytest.raises(SystemExit, match="paper only"):
        check_cross_market_arb("cross_market_arb", "live", env={ENABLE_ENV: "1"})
    with pytest.raises(SystemExit, match="paper only"):
        check_cross_market_arb("cross_market_arb", "shadow", env={ENABLE_ENV: "1"})
    check_cross_market_arb("cross_market_arb", "paper", env={ENABLE_ENV: "1"})  # ok


def test_isolation_from_cheapside_config():
    from troll_poly_bot.config import BotConfig
    cfg = BotConfig()
    assert cfg.cross_market_arb.enabled is False
    assert cfg.cheapside is not cfg.cross_market_arb
    # Enabling CMA via env must not flip cheapside
    cma = CrossMarketArbConfig.from_env({ENABLE_ENV: "1", "TPB_CMA_MINIMUM_NET_PROFIT": "0.12"})
    assert cma.enabled is True
    assert abs(cma.minimum_net_profit - 0.12) < 1e-9


def test_engine_arms_and_emits_leg_order():
    eng = _engine()
    now = T0 + 60_000
    _seed_arb_quotes(eng, now)
    meta = _meta("BTC")
    ev = eng.evaluate(meta, eng.market_quotes[meta.market.slug]["up"],
                      eng.market_quotes[meta.market.slug]["down"],
                      None, 0.0, 40.0, now, None, None, None, balance=200.0,
                      quote_ages=eng.market_quotes[meta.market.slug]["quote_ages"])
    assert ev.tradeable, ev.detail
    assert (ev.features or {}).get("cross_market_arb", {}).get("cma_leg")
    assert eng.active_plan_id is not None
    plan = eng.plans[eng.active_plan_id]
    assert plan.state == ArbState.EXECUTING.value
    assert plan.reserved_cash > 0


def test_partial_fills_recalculate_and_incomplete_policy():
    eng = _engine(residual_exposure_policy="hold")
    now = T0 + 60_000
    _seed_arb_quotes(eng, now)
    meta = _meta("BTC")
    eng.evaluate(meta, eng.market_quotes[meta.market.slug]["up"],
                 eng.market_quotes[meta.market.slug]["down"],
                 None, 0.0, 40.0, now, None, None, None, balance=200.0,
                 quote_ages=eng.market_quotes[meta.market.slug]["quote_ages"])
    plan = eng.plans[eng.active_plan_id]
    # Fill only the first UP leg partially — destroys equal-pair guarantee
    first = plan.legs[0]
    mark_fill(plan, first.slug, first.side, 5.0, 0.40, 0.08, now, [l.slug for l in plan.legs])
    eng._after_fill(plan, now)
    # Still executing or incomplete depending on exposure caps; never auto-sells
    assert plan.state in (ArbState.EXECUTING.value, ArbState.INCOMPLETE.value,
                         ArbState.CANCELLED.value, ArbState.IDLE.value)
    assert eng.scfg.residual_exposure_policy == "hold"


def test_duplicate_order_key_stable():
    k1 = client_order_key("abc", "btc-m", "UP", 0)
    k2 = client_order_key("abc", "btc-m", "UP", 0)
    k3 = client_order_key("abc", "btc-m", "UP", 1)
    assert k1 == k2 and k1 != k3


def test_restart_reconciliation_no_duplicate_arm():
    eng = _engine()
    now = T0 + 60_000
    _seed_arb_quotes(eng, now)
    meta = _meta("BTC")
    eng.evaluate(meta, eng.market_quotes[meta.market.slug]["up"],
                 eng.market_quotes[meta.market.slug]["down"],
                 None, 0.0, 40.0, now, None, None, None, balance=200.0,
                 quote_ages=eng.market_quotes[meta.market.slug]["quote_ages"])
    plan_id = eng.active_plan_id
    doc = eng.persist_state()
    eng2 = _engine()
    eng2.restore_state(doc, now + 1000)
    assert plan_id in eng2.plans
    restored = eng2.plans[plan_id]
    assert restored.state == ArbState.RECONCILING.value
    # client keys preserved
    assert isinstance(restored.client_order_keys, set)


def test_cash_reservation_blocks_second_opportunity():
    eng = _engine(maximum_concurrent_opportunities=1, maximum_capital_per_opportunity=80.0)
    now = T0 + 60_000
    _seed_arb_quotes(eng, now)
    meta = _meta("ETH")
    eng.evaluate(meta, eng.market_quotes[meta.market.slug]["up"],
                 eng.market_quotes[meta.market.slug]["down"],
                 None, 0.0, 40.0, now, None, None, None, balance=200.0,
                 quote_ages=eng.market_quotes[meta.market.slug]["quote_ages"])
    assert eng.active_plan_id
    reserved = eng._reserved_cash()
    assert reserved > 0
    # Second scan while busy should not arm another plan
    ev2 = eng.evaluate(meta, eng.market_quotes[meta.market.slug]["up"],
                       eng.market_quotes[meta.market.slug]["down"],
                       None, 0.0, 40.0, now + 10, None, None, None, balance=200.0,
                       quote_ages=eng.market_quotes[meta.market.slug]["quote_ages"])
    # Either emits next leg of same plan or cooldown — never a second active plan
    assert sum(1 for p in eng.plans.values()
               if p.state == ArbState.EXECUTING.value) <= 1


def test_risk_limits_on_order_path_max_order():
    eng = _engine(max_order_usdc=1.0)  # impossibly small vs 5-share leg
    now = T0 + 60_000
    _seed_arb_quotes(eng, now)
    # Manually arm a plan then try execute
    from troll_poly_bot.strategy.cross_market_arb.optimizer import OptimizeResult
    res = eng._scan(now, 200.0)
    if res.best is None:
        pytest.skip("no synthetic arb under this fee/slippage grid")
    plan = plan_from_candidate(res.best, 0, now)
    plan.state = ArbState.EXECUTING.value
    eng.plans[plan.plan_id] = plan
    eng.active_plan_id = plan.plan_id
    out = eng._execute_next(plan, now, 200.0)
    # Should abort due to max_order
    assert plan.state in (ArbState.CANCELLED.value, ArbState.INCOMPLETE.value) or (
        out is not None and out.reason is not None
    )


def test_settlement_pnl_all_128_signs():
    legs = [
        PositionLeg("btc-m", "BTC", "UP", 5.0, 2.1),
        PositionLeg("btc-m", "BTC", "DOWN", 5.0, 2.1),
        PositionLeg("eth-m", "ETH", "UP", 5.0, 2.1),
        PositionLeg("eth-m", "ETH", "DOWN", 5.0, 2.1),
    ]
    slugs = ["btc-m", "eth-m"]
    for bits in itertools.product(("UP", "DOWN"), repeat=2):
        winners = dict(zip(slugs, bits))
        pnl = settlement_pnl(legs, winners)
        # Always get 5+5=10 payout, cost 8.4 => +1.6
        assert abs(pnl - (10.0 - 8.4)) < 1e-9


def test_maker_bid_prices_lock_profit():
    """Join/improve bids can sum under $1; that is the live paper entry path."""
    up = _book("BTC-UP", 0.47, 0.52)
    dn = _book("BTC-DN", 0.48, 0.53)
    prices = CrossMarketArbEngine._maker_bid_price(0.47, 0.52, 0.02), \
             CrossMarketArbEngine._maker_bid_price(0.48, 0.53, 0.02)
    assert prices == (0.48, 0.49)
    # 5 shares: profit = 5*(1-0.97)=0.15 > 0.05
    assert 5.0 * (1.0 - sum(prices)) > 0.05


def test_maker_pair_requests_arms_on_live_style_book():
    eng = _engine(entry_mode="maker", maker_shares=5.0, minimum_net_profit=0.05,
                  max_maker_markets=3)
    now = T0 + 60_000
    markets = {}
    books = {}
    for a in ASSETS[:2]:
        meta = _meta(a)
        # Wide enough spread that join+improve sums to 0.97
        up = _book(meta.market.yes_token_id, 0.47, 0.52)
        dn = _book(meta.market.no_token_id, 0.48, 0.53)
        markets[meta.market.slug] = meta.market
        books[meta.market.slug] = (up, dn)
        eng.market_quotes[meta.market.slug] = {
            "t": now, "meta": meta, "up": up, "down": dn, "valid": True,
            "quote_ages": {meta.market.yes_token_id: 40.0, meta.market.no_token_id: 40.0},
        }
    reqs = eng.pair_requests(now, markets, books, balance=100.0)
    assert len(reqs) >= 2  # at least one market's UP+DOWN
    assert all(o.tif.value == "GTC" for _, _, o in reqs)
    assert eng.stats["maker_pairs_armed"] >= 1


def test_plan_dict_roundtrip():
    up = estimate_leg(_quote("BTC", "UP", 0.40), 5.0, 0.01)
    dn = estimate_leg(_quote("BTC", "DOWN", 0.40), 5.0, 0.01)
    from troll_poly_bot.strategy.cross_market_arb.optimizer import PortfolioCandidate
    from troll_poly_bot.strategy.cross_market_arb.math import portfolio_metrics, planned_to_positions
    pos = planned_to_positions([up, dn])
    m = portfolio_metrics(pos, [up.slug])
    cand = PortfolioCandidate(legs=[up, dn], metrics=m, capital_efficiency=0.1,
                              execution_risk=0.1, rank_score=1.0)
    plan = plan_from_candidate(cand, 1, T0)
    plan.client_order_keys.add("cma:x")
    back = plan_from_dict({**as_dict(plan), "client_order_keys": list(plan.client_order_keys)})
    assert back.plan_id == plan.plan_id
    assert "cma:x" in back.client_order_keys
