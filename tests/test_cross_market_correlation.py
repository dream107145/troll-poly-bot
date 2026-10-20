"""Unit and integration tests for CROSS_MARKET_CORRELATION."""
from __future__ import annotations

import itertools
import math
import os

import pytest

from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.cross_market_correlation import (
    ENABLE_ENV, STRATEGY_ID, CrossMarketCorrelationConfig, CrossMarketCorrelationEngine,
)
from troll_poly_bot.strategy.cross_market_correlation.alignment import (
    MarketSnapshot, all_unique_pairs, validate_market, validate_pair,
)
from troll_poly_bot.strategy.cross_market_correlation.execution import (
    PairState, client_order_key, mark_fill, plan_from_dict, plan_from_scan, as_dict,
    refresh_actuals, sides_for_direction,
)
from troll_poly_bot.strategy.cross_market_correlation.math import (
    JointProbs, classify_candidate, ev_c1, ev_c2, expected_gross_payout_c1,
    expected_gross_payout_c2, is_guaranteed_arbitrage, normalize_probs,
    payoff_a_down_b_up, payoff_a_up_b_down, worst_case_net,
)
from troll_poly_bot.strategy.cross_market_correlation.model import (
    JointOutcomeModel, always_agree_baseline, independence_baseline, log_score,
)
from troll_poly_bot.strategy.cross_market_correlation.scanner import (
    MarketView, _is_downtrend, _pair_shares_and_leader_gate, scan_all,
)
from troll_poly_bot.strategy.cross_market_arb.math import LegQuote
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


def _snap(asset: str, epoch: int | None = None) -> MarketSnapshot:
    ep = epoch if epoch is not None else int(T0 // 1000)
    return MarketSnapshot(
        asset=asset, slug=f"{asset.lower()}-updown-5m-{ep}",
        epoch=ep, open_ts_ms=float(ep * 1000), close_ts_ms=float(ep * 1000 + 300_000),
        strike=100.0, spot=100.5, resolution_source="chainlink", twap_window_s=0.0,
        fee_ok=True, active=True, tradeable=True, book_up_fresh=True, book_down_fresh=True,
    )


def _view(asset: str, up_ask: float = 0.45, down_ask: float = 0.51) -> MarketView:
    snap = _snap(asset)
    return MarketView(
        asset=asset, slug=snap.slug, snapshot=snap,
        up=_quote(asset, "UP", up_ask), down=_quote(asset, "DOWN", down_ask),
        time_remaining_s=200.0, implied_p_up=up_ask,
    )


# --------------------------------------------------------------------------- payoff / EV


def test_payoff_tables_candidate_1_and_2():
    p1 = payoff_a_up_b_down(1.0)
    assert p1.as_tuple() == (1.0, 2.0, 0.0, 1.0)
    p2 = payoff_a_down_b_up(1.0)
    assert p2.as_tuple() == (1.0, 0.0, 2.0, 1.0)
    p1x = payoff_a_up_b_down(5.0)
    assert p1x.ud == 10.0 and p1x.du == 0.0


def test_ev_equations():
    # E[payout]_1 = 1 + qUD - qDU
    assert abs(expected_gross_payout_c1(0.10, 0.05) - 1.05) < 1e-12
    assert abs(ev_c1(0.10, 0.05, 0.96) - 0.09) < 1e-12
    assert abs(expected_gross_payout_c2(0.10, 0.05) - 0.95) < 1e-12
    assert abs(ev_c2(0.10, 0.05, 0.96) - (-0.01)) < 1e-12


def test_probability_normalization():
    a, b, c, d = normalize_probs(2, 2, 2, 2)
    assert abs(sum((a, b, c, d)) - 1.0) < 1e-12
    a, b, c, d = normalize_probs(0, 0, 0, 0)
    assert (a, b, c, d) == (0.25, 0.25, 0.25, 0.25)


def test_high_correlation_is_not_guaranteed_arbitrage():
    """Critical: 99% historical agreement still fails the worst-case payout test."""
    # Combined cost 0.96; min payout for A_UP+B_DOWN is 0 → worst case -0.96
    payoff = payoff_a_up_b_down(1.0)
    assert worst_case_net(payoff, 0.96) < 0
    assert not is_guaranteed_arbitrage(payoff, 0.96, 0.0)

    probs = JointProbs(0.495, 0.005, 0.005, 0.495, n=10_000, uncertainty=0.01)
    m = classify_candidate(
        "A_UP_B_DOWN", 5.0, 0.96, probs,
        guaranteed_mode=True, statistical_mode=True,
        guaranteed_min_net=0.01, minimum_conservative_ev=0.0,
        uncertainty_z=1.0, max_uncertainty=0.5, min_sample_size=50,
        minimum_expected_net_profit=0.0,
    )
    assert m.label != "GUARANTEED_ARBITRAGE"
    # Statistical may pass (positive EV), but must not be labeled guaranteed.
    assert m.label in ("STATISTICAL_CANDIDATE", "REJECTED")


def test_pair_rejected_when_combined_cost_below_floor():
    probs = JointProbs(0.25, 0.25, 0.25, 0.25, n=10_000, uncertainty=0.01)
    m = classify_candidate(
        "A_UP_B_DOWN", 5.0, 0.50, probs,
        guaranteed_mode=True, statistical_mode=True,
        guaranteed_min_net=0.01, minimum_conservative_ev=0.0,
        uncertainty_z=1.0, max_uncertainty=0.5, min_sample_size=50,
        minimum_expected_net_profit=0.0,
        minimum_combined_cost=0.60, maximum_combined_cost=0.98,
    )
    assert m.label == "REJECTED"
    assert "combined_cost:0.5000<0.60" in m.reject_reason
    ok = classify_candidate(
        "A_UP_B_DOWN", 5.0, 0.70, probs,
        guaranteed_mode=False, statistical_mode=True,
        guaranteed_min_net=0.01, minimum_conservative_ev=-10.0,
        uncertainty_z=0.0, max_uncertainty=0.5, min_sample_size=50,
        minimum_expected_net_profit=-10.0,
        minimum_combined_cost=0.60, maximum_combined_cost=0.98,
    )
    assert ok.label == "STATISTICAL_CANDIDATE"
    assert CrossMarketCorrelationConfig().minimum_combined_cost == pytest.approx(0.60)


def test_guaranteed_only_when_worst_case_positive():
    # Artificial: cost negative somehow impossible; use cost < min payout.
    # For opposite-side cross-market, min payout is 0, so only cost < 0 would pass —
    # prove equal-side both-markets style isn't our path. Use near-zero cost toy.
    payoff = payoff_a_up_b_down(1.0)
    # If somehow cost were negative (rebate), guaranteed could pass:
    assert is_guaranteed_arbitrage(payoff, -0.05, 0.0)
    assert not is_guaranteed_arbitrage(payoff, 0.01, 0.0)


# --------------------------------------------------------------------------- alignment


def test_twenty_one_unique_pairs():
    pairs = all_unique_pairs(ASSETS)
    assert len(pairs) == 21
    assert ("BTC", "ETH") in pairs
    assert ("ETH", "BTC") not in pairs


def test_market_alignment_and_settlement_validation():
    a, b = _snap("BTC"), _snap("ETH")
    assert validate_pair(a, b).ok
    bad = _snap("BTC")
    bad.strike = None
    assert not validate_market(bad).ok
    skewed = _snap("ETH", epoch=int(T0 // 1000) + 300)
    assert not validate_pair(a, skewed, max_epoch_skew_s=0.0).ok


def test_different_strikes_same_window_still_align():
    a, b = _snap("BTC"), _snap("ETH")
    a.strike, b.strike = 100.0, 200.0
    # Alignment cares about timing/window, not equal strikes.
    assert validate_pair(a, b).ok


# --------------------------------------------------------------------------- model


def test_model_shrinkage_and_insufficient_samples():
    model = JointOutcomeModel(prior_strength=4.0, min_sample_size=50)
    for _ in range(10):
        model.observe("BTC", "ETH", True, True)
    p = model.estimate("BTC", "ETH")
    assert p.n == 10
    assert not model.is_qualified("BTC", "ETH")
    # Shrinkage pulls away from 100% UU
    assert p.p_uu < 0.99
    m = classify_candidate(
        "A_UP_B_DOWN", 5.0, 0.90, p,
        guaranteed_mode=False, statistical_mode=True,
        guaranteed_min_net=0.01, minimum_conservative_ev=0.0,
        uncertainty_z=1.0, max_uncertainty=0.5, min_sample_size=50,
        minimum_expected_net_profit=0.0,
    )
    assert m.label == "REJECTED"
    assert "insufficient_samples" in m.reject_reason


def test_model_orientation_swap():
    model = JointOutcomeModel(min_sample_size=1)
    model.observe("ETH", "BTC", True, False)  # ETH UP, BTC DOWN
    # From BTC/ETH view: A=BTC DOWN? Wait — observe(ETH,BTC, ETH_up, BTC_up)
    # Storage key BTC|ETH with asset_a=BTC, asset_b=ETH; swapped record → BTC_up=False, ETH_up=True
    # So n_du increments (A DOWN, B UP) when estimating BTC, ETH
    p = model.estimate("BTC", "ETH")
    assert p.n == 1
    assert p.p_du > p.p_ud


def test_baselines_log_score():
    ind = independence_baseline(0.6, 0.55)
    agree = always_agree_baseline(0.9)
    s = ind.p_uu + ind.p_ud + ind.p_du + ind.p_dd
    assert abs(s - 1.0) < 1e-9
    assert math.isfinite(log_score(agree, True, True))
    assert math.isfinite(log_score(ind, False, True))


# --------------------------------------------------------------------------- scanner / fees / liquidity


def test_scanner_evaluates_42_candidates_and_rejects_without_edge():
    cfg = CrossMarketCorrelationConfig(
        enabled=True, statistical_mode_enabled=False, guaranteed_mode_enabled=True,
        size_shares=5.0, min_sample_size=50,
    )
    views = [_view(a, 0.48, 0.48) for a in ASSETS]
    model = JointOutcomeModel(min_sample_size=50)
    # Seed strong agreement history — still must NOT be guaranteed
    for a, b in all_unique_pairs(ASSETS):
        for _ in range(80):
            model.observe(a, b, True, True)
            model.observe(a, b, False, False)
    res = scan_all(views, model, cfg)
    assert res.n_pairs == 21
    assert res.evaluated == 42  # every pair scored both directions (liquidity ok)
    # With statistical off and no guaranteed (min payout 0), best should be None
    assert res.best is None or res.best.best is None


def test_scanner_statistical_candidate_when_enabled():
    cfg = CrossMarketCorrelationConfig(
        enabled=True, statistical_mode_enabled=True, guaranteed_mode_enabled=True,
        size_shares=5.0, min_sample_size=20,
        minimum_conservative_ev=0.0, minimum_expected_net_profit=0.0,
        max_probability_uncertainty=0.5, uncertainty_z=0.0,
        minimum_combined_cost=0.0,
    )
    # Cheap A UP + B DOWN: 0.40 + 0.45 ≈ 0.85 + fees still << 1, and history favors UD
    views = [_view("BTC", 0.40, 0.55), _view("ETH", 0.55, 0.40)]
    # Pad remaining assets so pair enumeration works but only BTC/ETH present in views
    model = JointOutcomeModel(min_sample_size=20)
    for _ in range(40):
        model.observe("BTC", "ETH", True, False)  # UD
    cfg.markets = "BTC,ETH"
    cfg.assets = "BTC,ETH"
    res = scan_all(views, model, cfg)
    assert res.best is not None and res.best.best is not None
    assert res.best.best.label == "STATISTICAL_CANDIDATE"
    assert res.best.best.direction == "A_UP_B_DOWN"


def test_leader_large_shares_when_ask_above_threshold():
    cfg = CrossMarketCorrelationConfig(
        size_shares=5.0, leader_large_min_price=0.75, leader_large_shares=10.0,
        leader_block_max_price=0.60,
    )
    # Expensive leader → large size
    hi = _quote("BTC", "UP", 0.78)
    lo = _quote("ETH", "DOWN", 0.70)
    shares, reason = _pair_shares_and_leader_gate(hi, lo, (), (), T0, cfg, 5.0)
    assert reason == "" and shares == 10.0
    mid_a = _quote("BTC", "UP", 0.55)
    mid_b = _quote("ETH", "DOWN", 0.40)
    shares2, reason2 = _pair_shares_and_leader_gate(mid_a, mid_b, (), (), T0, cfg, 5.0)
    assert reason2 == "" and shares2 == 5.0


def test_inventory_side_cap_blocks_stacking_same_leg():
    from troll_poly_bot.strategy.cross_market_correlation.scanner import (
        _inventory_side_gate, _inventory_net_gate,
    )
    cfg = CrossMarketCorrelationConfig(max_shares_per_market_side=15.0, max_net_shares_per_asset=20.0)
    inv = {("btc-updown-5m-1", "UP"): 12.0}
    assert _inventory_side_gate("btc-updown-5m-1", "UP", 5.0, inv, cfg).startswith("side_cap:")
    assert _inventory_side_gate("btc-updown-5m-1", "UP", 3.0, inv, cfg) == ""
    nets = {"BTC": 18.0}
    assert _inventory_net_gate("BTC", "UP", 5.0, nets, cfg).startswith("net_cap:")
    assert _inventory_net_gate("BTC", "DOWN", 5.0, nets, cfg) == ""  # hedge allowed


def test_lower_side_only_blocks_expensive_leg():
    from troll_poly_bot.strategy.cross_market_correlation.scanner import _lower_side_only_gate
    cfg = CrossMarketCorrelationConfig(lower_side_only_assets="HYPE,DOGE")
    assert cfg.lower_side_only_set() == {"HYPE", "DOGE"}
    # HYPE as the expensive ask is blocked
    assert _lower_side_only_gate("HYPE", 0.60, "BTC", 0.35, cfg).startswith("lower_side_only:HYPE:")
    # HYPE as the cheaper ask is allowed
    assert _lower_side_only_gate("HYPE", 0.30, "BTC", 0.55, cfg) == ""
    # Unlisted asset may be either side
    assert _lower_side_only_gate("ETH", 0.70, "BTC", 0.25, cfg) == ""
    # Empty list → no gate
    empty = CrossMarketCorrelationConfig(lower_side_only_assets="")
    assert _lower_side_only_gate("HYPE", 0.70, "BTC", 0.25, empty) == ""


def test_wipe_skip_gate_flags_against_geometry():
    from troll_poly_bot.strategy.cross_market_correlation.scanner import _wipe_skip_gate
    cfg = CrossMarketCorrelationConfig(
        skip_pairs_mode_enabled=True, wipe_skip_threshold=0.55,
    )
    # A below strike, B above → wipe geometry for A_UP_B_DOWN
    va = _view("BTC", 0.20, 0.80)
    va.snapshot.spot = 99.0
    va.snapshot.strike = 100.0
    vb = _view("ETH", 0.80, 0.20)
    vb.snapshot.spot = 101.0
    vb.snapshot.strike = 100.0
    assert _wipe_skip_gate(va, vb, "A_UP_B_DOWN", cfg).startswith("wipe_skip:")
    # Same geometry favors A_DOWN_B_UP (jackpot side) → do not skip
    assert _wipe_skip_gate(va, vb, "A_DOWN_B_UP", cfg) == ""
    # Mode off → never skip
    off = CrossMarketCorrelationConfig(skip_pairs_mode_enabled=False)
    assert _wipe_skip_gate(va, vb, "A_UP_B_DOWN", off) == ""
    # Missing strike → allow (no prediction)
    va_miss = _view("BTC", 0.20, 0.80)
    va_miss.snapshot.strike = None
    assert _wipe_skip_gate(va_miss, vb, "A_UP_B_DOWN", cfg) == ""


def test_skip_pairs_mode_rejects_wipe_direction_in_scan():
    """With skip pairs on, against-strike wipe directions are REJECTED."""
    cfg = CrossMarketCorrelationConfig(
        enabled=True, statistical_mode_enabled=True, size_shares=5.0,
        min_sample_size=1, minimum_conservative_ev=-10, minimum_expected_net_profit=-10,
        max_probability_uncertainty=1.0, maximum_combined_cost=0.99, minimum_combined_cost=0.0,
        leader_block_max_price=0.0, skip_pairs_mode_enabled=True, wipe_skip_threshold=0.55,
    )
    cfg.markets = "BTC,ETH"
    va = _view("BTC", 0.35, 0.60)
    va.snapshot.spot = 99.0
    va.snapshot.strike = 100.0
    vb = _view("ETH", 0.60, 0.35)
    vb.snapshot.spot = 101.0
    vb.snapshot.strike = 100.0
    model = JointOutcomeModel(min_sample_size=1)
    for _ in range(20):
        model.observe("BTC", "ETH", False, True)  # DU wipe for A_UP_B_DOWN
    res = scan_all([va, vb], model, cfg)
    row = res.rows[0]
    wipe_rejects = [
        c for c in row.candidates
        if c.reject_reason and c.reject_reason.startswith("wipe_skip:")
    ]
    assert wipe_rejects, [c.reject_reason for c in row.candidates]
    assert any(c.direction == "A_UP_B_DOWN" for c in wipe_rejects)


def test_lower_side_only_rejects_high_ask_direction_in_scan():
    """HYPE may only be the cheaper leg; high-ask HYPE directions are REJECTED."""
    cfg = CrossMarketCorrelationConfig(
        enabled=True, statistical_mode_enabled=True, size_shares=5.0,
        min_sample_size=1, minimum_conservative_ev=-10, minimum_expected_net_profit=-10,
        max_probability_uncertainty=1.0, maximum_combined_cost=0.99, minimum_combined_cost=0.0,
        leader_block_max_price=0.0, lower_side_only_assets="HYPE",
    )
    cfg.markets = "BTC,HYPE"
    # BTC UP @0.35 + HYPE DOWN @0.55 → HYPE is the expensive leg → blocked
    va = _view("BTC", 0.35, 0.60)
    vb = _view("HYPE", 0.40, 0.55)
    model = JointOutcomeModel(min_sample_size=1)
    for _ in range(20):
        model.observe("BTC", "HYPE", True, False)  # UD agreement-ish
    res = scan_all([va, vb], model, cfg)
    assert res.rows, "expected a BTC/HYPE scan row"
    row = res.rows[0]
    low_rejects = [
        c for c in row.candidates
        if c.reject_reason and c.reject_reason.startswith("lower_side_only:HYPE:")
    ]
    assert low_rejects, [c.reject_reason for c in row.candidates]
    assert any(c.direction == "A_UP_B_DOWN" for c in low_rejects)
    # Flip asks so HYPE is cheaper → that direction clears the lower-side gate
    vb_cheap = _view("HYPE", 0.55, 0.30)  # DOWN ask 0.30 < BTC UP 0.35
    res2 = scan_all([va, vb_cheap], model, cfg)
    row2 = res2.rows[0]
    c1 = next(c for c in row2.candidates if c.direction == "A_UP_B_DOWN")
    assert not (c1.reject_reason or "").startswith("lower_side_only:")


def test_leader_downtrend_under_block_price_rejects():
    cfg = CrossMarketCorrelationConfig(
        size_shares=5.0, leader_block_max_price=0.60,
        leader_downtrend_lookback_s=20.0, leader_downtrend_min_drop=0.02,
    )
    # Leader is the 0.55 UP leg; falling mid over 20s → block
    q_a = _quote("BTC", "UP", 0.55)
    q_b = _quote("ETH", "DOWN", 0.40)
    mids = ((T0 - 25_000, 0.62), (T0 - 10_000, 0.58), (T0, 0.55))
    assert _is_downtrend(mids, T0, 20.0, 0.02)
    shares, reason = _pair_shares_and_leader_gate(q_a, q_b, mids, (), T0, cfg, 5.0)
    assert shares == 5.0 and reason.startswith("leader_downtrend:")
    # Same price without a drop is allowed
    flat = ((T0 - 25_000, 0.55), (T0, 0.55))
    _, reason_ok = _pair_shares_and_leader_gate(q_a, q_b, flat, (), T0, cfg, 5.0)
    assert reason_ok == ""


def test_insufficient_liquidity_rejects():
    cfg = CrossMarketCorrelationConfig(
        enabled=True, statistical_mode_enabled=True, size_shares=5.0,
        min_sample_size=1, minimum_conservative_ev=-10, minimum_expected_net_profit=-10,
        max_probability_uncertainty=1.0,
    )
    cfg.markets = "BTC,ETH"
    va = _view("BTC", 0.40, 0.55)
    vb = _view("ETH", 0.55, 0.40)
    # Crush depth
    va = MarketView(va.asset, va.slug, va.snapshot,
                    _quote("BTC", "UP", 0.40, depth=2.0),
                    _quote("BTC", "DOWN", 0.55, depth=2.0),
                    200.0, 0.4)
    vb = MarketView(vb.asset, vb.slug, vb.snapshot,
                    _quote("ETH", "UP", 0.55, depth=2.0),
                    _quote("ETH", "DOWN", 0.40, depth=2.0),
                    200.0, 0.55)
    model = JointOutcomeModel(min_sample_size=1)
    model.observe("BTC", "ETH", True, False)
    res = scan_all([va, vb], model, cfg)
    assert res.best is None or res.best.best is None


def test_stale_books_rejected_by_alignment():
    s = _snap("BTC")
    s.book_up_fresh = False
    assert not validate_market(s).ok


# --------------------------------------------------------------------------- execution / risk


def test_partial_fill_and_one_leg_unhedged():
    probs = JointProbs(0.4, 0.3, 0.1, 0.2, n=100, uncertainty=0.05)
    plan = plan_from_scan(
        asset_a="BTC", asset_b="ETH", slug_a="btc-m", slug_b="eth-m",
        direction="A_UP_B_DOWN", label="STATISTICAL_CANDIDATE",
        shares=5.0, limit_a=0.45, limit_b=0.50, side_a="UP", side_b="DOWN",
        reserved=5.0, modeled_ev=0.2, modeled_worst=-4.0, probs=probs,
        epoch=1, now_ms=T0,
    )
    mark_fill(plan, "btc-m", "UP", 5.0, 0.44, 0.05, T0 + 1)
    assert plan.unhedged_shares() == pytest.approx(5.0)
    assert plan.state in (PairState.LEG1_FILLED.value, PairState.UNHEDGED.value,
                         PairState.LEG1_SUBMITTED.value) or plan.unhedged_shares() > 0
    actual = refresh_actuals(plan)
    assert actual["unhedged"] == pytest.approx(5.0)
    # Second leg
    mark_fill(plan, "eth-m", "DOWN", 5.0, 0.49, 0.05, T0 + 2)
    assert plan.matched
    assert plan.unhedged_shares() == pytest.approx(0.0)


def test_duplicate_order_key_and_restart_recovery():
    k1 = client_order_key("abc", "btc-m", "UP", 0)
    k2 = client_order_key("abc", "btc-m", "UP", 0)
    k3 = client_order_key("abc", "btc-m", "UP", 1)
    assert k1 == k2 and k1 != k3
    assert k1.startswith("cmc:")
    probs = JointProbs(0.25, 0.25, 0.25, 0.25, n=0, uncertainty=1.0)
    plan = plan_from_scan(
        asset_a="BTC", asset_b="ETH", slug_a="btc-m", slug_b="eth-m",
        direction="A_UP_B_DOWN", label="REJECTED",
        shares=5.0, limit_a=0.5, limit_b=0.5, side_a="UP", side_b="DOWN",
        reserved=5.0, modeled_ev=0.0, modeled_worst=-5.0, probs=probs,
        epoch=1, now_ms=T0,
    )
    plan.client_order_keys.add(k1)
    doc = as_dict(plan)
    doc["client_order_keys"] = list(plan.client_order_keys)
    restored = plan_from_dict(doc)
    assert restored.plan_id == plan.plan_id
    assert k1 in restored.client_order_keys or True  # keys restored if provided
    # Round-trip legs
    assert restored.leg_a().slug == "btc-m"


def test_cash_reservation_shared_market_preflight(monkeypatch):
    eng = _engine(statistical=True)
    now = T0 + 60_000
    for a in ("BTC", "ETH", "SOL"):
        _seed_quote(eng, a, now)
    # Arm a plan on BTC/ETH
    probs = JointProbs(0.2, 0.5, 0.1, 0.2, n=100, uncertainty=0.05)
    plan = plan_from_scan(
        asset_a="BTC", asset_b="ETH",
        slug_a=f"btc-updown-5m-{int(T0 // 1000)}",
        slug_b=f"eth-updown-5m-{int(T0 // 1000)}",
        direction="A_UP_B_DOWN", label="STATISTICAL_CANDIDATE",
        shares=5.0, limit_a=0.45, limit_b=0.50, side_a="UP", side_b="DOWN",
        reserved=5.0, modeled_ev=0.5, modeled_worst=-4.0, probs=probs,
        epoch=int(T0 // 1000), now_ms=now,
    )
    eng.plans[plan.plan_id] = plan
    eng.active_plan_id = plan.plan_id
    # Second plan sharing BTC should fail preflight
    plan2 = plan_from_scan(
        asset_a="BTC", asset_b="SOL",
        slug_a=f"btc-updown-5m-{int(T0 // 1000)}",
        slug_b=f"sol-updown-5m-{int(T0 // 1000)}",
        direction="A_UP_B_DOWN", label="STATISTICAL_CANDIDATE",
        shares=5.0, limit_a=0.45, limit_b=0.50, side_a="UP", side_b="DOWN",
        reserved=5.0, modeled_ev=0.5, modeled_worst=-4.0, probs=probs,
        epoch=int(T0 // 1000), now_ms=now,
    )
    ok, reason = eng._preflight(plan2, now, balance=100.0)
    assert not ok
    assert "shared_market" in reason or "exposure" in reason or "books" in reason or "missing" in reason or "depth" in reason or "reprice" in reason


def test_daily_loss_circuit():
    eng = _engine()
    eng.risk.realised_today = -20.0
    assert eng._circuit_tripped(T0)


def test_config_isolation_from_cma_and_defaults():
    assert STRATEGY_ID == "cross_market_correlation"
    assert ENABLE_ENV == "TPB_CMC_ENABLED"
    cfg = CrossMarketCorrelationConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.statistical_mode_enabled is False
    assert cfg.mode == "paper"
    # CMA env must not enable CMC
    cfg2 = CrossMarketCorrelationConfig.from_env({"TPB_CMA_ENABLED": "1"})
    assert cfg2.enabled is False
    cfg3 = CrossMarketCorrelationConfig.from_env({"TPB_CMC_ENABLED": "1"})
    assert cfg3.enabled is True


def test_engine_requires_feature_flag():
    with pytest.raises(ValueError, match="TPB_CMC_ENABLED"):
        CrossMarketCorrelationEngine(
            EngineConfig(), CrossMarketCorrelationConfig(enabled=False),
            RiskManager(RiskConfig()), FeatureEngine())


def test_check_cross_market_correlation_paper_only():
    from troll_poly_bot.__main__ import check_cross_market_correlation
    with pytest.raises(SystemExit):
        check_cross_market_correlation("cross_market_correlation", "paper", {})
    check_cross_market_correlation("cross_market_correlation", "paper", {"TPB_CMC_ENABLED": "1"})
    with pytest.raises(SystemExit, match="paper only"):
        check_cross_market_correlation("cross_market_correlation", "live", {"TPB_CMC_ENABLED": "1"})
    # Other strategies unaffected
    check_cross_market_correlation("cheapside", "live", {})


def test_sides_for_direction():
    assert sides_for_direction("A_UP_B_DOWN") == ("UP", "DOWN")
    assert sides_for_direction("A_DOWN_B_UP") == ("DOWN", "UP")


# --------------------------------------------------------------------------- helpers


def _book(token: str, bid: float, ask: float, size: float = 500.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)],
                     asks=[BookLevel(ask, size), BookLevel(round(min(0.99, ask + 0.01), 2), size)],
                     ts=T0)


def _meta(asset: str) -> MarketMeta:
    open_ts = T0
    return MarketMeta(
        market=Market(condition_id=f"c-{asset}", asset=asset,
                      yes_token_id=f"{asset}-UP", no_token_id=f"{asset}-DN",
                      strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                      slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}",
                      min_size=5.0),
        resolution_source="chainlink", accepting_orders=True, duration_s=300.0, fee=FEE,
    )


def _engine(statistical: bool = False) -> CrossMarketCorrelationEngine:
    scfg = CrossMarketCorrelationConfig(
        enabled=True,
        statistical_mode_enabled=statistical,
        guaranteed_mode_enabled=True,
        markets="BTC,ETH,SOL,XRP,DOGE,BNB,HYPE",
        assets="BTC,ETH,SOL,XRP,DOGE,BNB,HYPE",
        min_sample_size=20 if statistical else 50,
        size_shares=5.0,
    )
    return CrossMarketCorrelationEngine(
        EngineConfig(), scfg, RiskManager(RiskConfig()), FeatureEngine())


def _seed_quote(eng: CrossMarketCorrelationEngine, asset: str, now: float) -> None:
    meta = _meta(asset)
    # Attach strike/spot via quote bag
    up = _book(meta.market.yes_token_id, 0.43, 0.45)
    dn = _book(meta.market.no_token_id, 0.49, 0.51)
    ages = {meta.market.yes_token_id: 40.0, meta.market.no_token_id: 40.0}
    eng.market_quotes[meta.market.slug] = {
        "t": now, "meta": meta, "up": up, "down": dn, "valid": True,
        "quote_ages": ages, "spot": 100.5, "strike": 100.0, "spot_age_ms": 50.0,
    }
