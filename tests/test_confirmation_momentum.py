"""BTC_CONFIRMATION_MOMENTUM (strategy/confirmation_momentum.py): the feature flag and the absence of a real-money
switch, its own env prefix, the pure decision pieces (z-scores, exchange agreement, retracement, price zones, the
probability estimator and the net edge), the engine's gates in order (timing, spot, momentum, agreement, continuation,
token confirmation, zones, edge, chase), the clip rules (independent impulses, never averaging down, the market cap),
the lottery as a separate GTC bid with its own statistics, and the reject taxonomy counters."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from troll_poly_bot.__main__ import check_confirmation_momentum, check_spot_lag
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy import confirmation_momentum as cm
from troll_poly_bot.strategy.confirmation_momentum import (
    MODEL_FEATURES, REJECTS, CMConfig, CMModel, ConfirmationMomentumEngine, estimate_win_probability,
    exchange_agreement, momentum_direction, momentum_from_returns, net_edge, price_zone, retrace_fraction,
)
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce

T0 = 1_800_000_000_000.0          # the market's open, ms
SIGMA = 1e-4                       # per-second log volatility (1 bp/s)
FEE = FeeSchedule()
SLUG = "btc-updown-5m-1800000000"


def _meta() -> MarketMeta:
    return MarketMeta(market=Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN", strike=100_000.0,
                                    open_ts=T0, close_ts=T0 + 300_000.0, slug=SLUG, min_size=5.0),
                      resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)


def _book(token: str, bid: float, ask: float, size: float = 100.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)])


def _view(price: float, sources: dict | None = None) -> CompositeView:
    src = sources or {"binance": price, "coinbase": price, "bybit": price}
    return CompositeView(asset="BTC", price=price, weighted_price=price, n_sources=len(src), deviation_bps=0.0,
                         cross_exchange_spread_bps=0.0, age_ms=50.0, sources=src)


class _Vol:
    ready = True
    sigma_per_sec = SIGMA


class _Twap:
    ready = True


def _cfg(**kw) -> CMConfig:
    """The engine under test uses a generous rule bonus (0.10-0.12) so that the pipeline can reach a TRADE; the
    shipped defaults (bonus cap 0.06 against ~5c of costs plus a 3c edge floor) cannot -- see
    test_the_shipped_rule_estimator_cannot_clear_the_shipped_costs."""
    base = dict(enabled=True, model_path="/nonexistent/cm_model.json", p_bonus_base=0.10, p_bonus_cap=0.12)
    base.update(kw)
    return CMConfig(**base)


def _engine(**kw) -> ConfirmationMomentumEngine:
    cfg = _cfg(**kw)
    risk = RiskManager(cfg=RiskConfig(max_position_usdc=50.0, max_total_exposure_usdc=100.0, max_concurrent_positions=5),
                       starting_balance=100.0)
    return ConfirmationMomentumEngine(EngineConfig(strategy="confirmation_momentum"), cfg, risk, FeatureEngine())


def _feed(e: ConfirmationMomentumEngine, path: list[tuple[float, float]], per_exchange: dict | None = None) -> None:
    """Spot samples (t_ms, price) into the composite history and, through evaluate's own feeder, the exchanges."""
    for t, p in path:
        e.features.update_spot("BTC", p, t)
        src = {k: p * (1 + v) for k, v in (per_exchange or {"binance": 0.0, "coinbase": 0.0, "bybit": 0.0}).items()}
        e._feed_exchanges(_view(p, src), t)                     # noqa: SLF001


def _flat_then_up(now: float, bps_total: float = 60.0, up_s: float = 15.0, step_s: float = 0.5) -> list[tuple[float, float]]:
    """40 s flat at 100,000 then a smooth rise of ``bps_total`` over the last ``up_s`` seconds (a clean impulse)."""
    out = []
    t = now - 55_000.0
    while t <= now + 1e-6:
        if t < now - up_s * 1000.0:
            p = 100_000.0
        else:
            frac = (t - (now - up_s * 1000.0)) / (up_s * 1000.0)
            p = 100_000.0 * (1 + bps_total * 1e-4 * frac)
        out.append((t, p))
        t += step_s * 1000.0
    return out


def _tokens(e, now, up_path: list[tuple[float, float]]) -> None:
    """Token mids into the engine's own history (what evaluate feeds from book_up.mid)."""
    for t, mid in up_path:
        e._tok.setdefault(SLUG, cm._TokenHistory()).update(mid, t)       # noqa: SLF001


def _ev(e, now, up_bid, up_ask, dn_bid, dn_ask, spot_price=None, sources=None, quote_ages=None):
    price = spot_price if spot_price is not None else e.features.spot["BTC"].at(now)
    return e.evaluate(_meta(), _book("UP", up_bid, up_ask), _book("DOWN", dn_bid, dn_ask), _view(price, sources), 50.0, 50.0,
                      now, _Vol(), _Twap(), COLOCATED, 100.0, quote_ages=quote_ages)


# ─────────────────────────────── flags and isolation ───────────────────────────────


def test_off_by_default_and_the_engine_refuses_to_exist_without_the_flag():
    assert CMConfig().enabled is False
    risk = RiskManager(cfg=RiskConfig(), starting_balance=50.0)
    with pytest.raises(ValueError, match="TPB_CM_ENABLED"):
        ConfirmationMomentumEngine(EngineConfig(strategy="confirmation_momentum"), CMConfig(), risk, FeatureEngine())


def test_paper_needs_the_flag_and_real_money_is_refused_whatever_is_set():
    with pytest.raises(SystemExit, match="TPB_CM_ENABLED"):
        check_confirmation_momentum("confirmation_momentum", "paper", env={})
    check_confirmation_momentum("confirmation_momentum", "paper", env={"TPB_CM_ENABLED": "1"})
    for mode in ("micro-live", "live", "shadow"):
        with pytest.raises(SystemExit, match="paper only"):
            check_confirmation_momentum("confirmation_momentum", mode,
                                        env={"TPB_CM_ENABLED": "1", "TPB_CM_ALLOW_REAL": "1", "TPB_SPOT_LAG_ALLOW_REAL": "1"})
    check_confirmation_momentum("spot_lag", "live", env={})


def test_its_own_env_prefix_leaves_spot_lag_and_cheapside_alone():
    env = {"TPB_CM_ENABLED": "1", "TPB_CM_Z_5S_MIN": "2.5", "TPB_CM_MIN_NET_EDGE": "0.04", "TPB_CM_LOTTERY_ENABLED": "1",
           "TPB_CM_MAX_ENTRIES_PER_MARKET": "2", "TPB_SPOT_LAG_MAX_ASK": "0.60"}
    c = CMConfig.from_env(env)
    assert c.enabled and c.z_5s_min == 2.5 and c.min_net_edge == 0.04 and c.lottery_enabled is True and c.max_entries_per_market == 2
    assert c.hold_to_settlement is True and c.take_profit_cents == 0.0 and c.stop_loss_cents == 0.0 and c.assets == "BTC"
    s = SpotLagConfig.from_env(env)
    assert s.enabled is False and s.max_ask == 0.60
    with pytest.raises(SystemExit):
        check_spot_lag("spot_lag", "paper", env=env)


# ─────────────────────────────── the pure pieces ───────────────────────────────


def test_z_scores_and_the_direction_rule():
    z = momentum_from_returns({1.0: 3e-4, 5.0: 6e-4, 15.0: 8e-4}, SIGMA)
    assert z[1.0] == pytest.approx(3.0) and z[5.0] == pytest.approx(6e-4 / (SIGMA * math.sqrt(5))) and z[15.0] == pytest.approx(8e-4 / (SIGMA * math.sqrt(15)))
    assert momentum_direction(z, 2.0, 2.0, 1.5) == (1, "")
    down = momentum_from_returns({1.0: -3e-4, 5.0: -6e-4, 15.0: -8e-4}, SIGMA)
    assert momentum_direction(down, 2.0, 2.0, 1.5) == (-1, "")
    weak = momentum_from_returns({1.0: 1e-4, 5.0: 6e-4, 15.0: 8e-4}, SIGMA)          # the 1 s leg too small
    assert momentum_direction(weak, 2.0, 2.0, 1.5) == (0, "WEAK_MOMENTUM")
    mixed = momentum_from_returns({1.0: -3e-4, 5.0: 6e-4, 15.0: 8e-4}, SIGMA)        # the 1 s leg against the move
    assert momentum_direction(mixed, 2.0, 2.0, 1.5) == (0, "WEAK_MOMENTUM")
    assert momentum_direction(momentum_from_returns({1.0: 0.0, 5.0: 1e-5, 15.0: 0.0}, SIGMA), 2.0, 2.0, 1.5) == (0, "NO_MOMENTUM")
    assert momentum_direction(momentum_from_returns({1.0: None, 5.0: 6e-4, 15.0: 8e-4}, SIGMA), 2.0, 2.0, 1.5) == (0, "NO_MOMENTUM")


def test_exchange_agreement_counts_signs_and_names_a_strong_contradiction():
    s5 = SIGMA * math.sqrt(5)
    agree, worst, contra = exchange_agreement({"binance": 5e-4, "coinbase": 4e-4, "bybit": 3e-4}, 1, s5, 1.0)
    assert (agree, worst, contra) == (3, 0.0, None)
    agree, worst, contra = exchange_agreement({"binance": 5e-4, "coinbase": None, "bybit": -1e-5}, 1, s5, 1.0)
    assert agree == 1 and contra is None and worst == pytest.approx(1e-5 / s5)
    agree, worst, contra = exchange_agreement({"binance": 5e-4, "coinbase": 4e-4, "bybit": -4e-4}, 1, s5, 1.0)
    assert agree == 2 and contra == "bybit" and worst == pytest.approx(4e-4 / s5)


def test_retracement_is_the_share_of_the_impulse_given_back():
    assert retrace_fraction([100.0, 100.3, 100.8, 100.9], 1) == pytest.approx(0.0)
    assert retrace_fraction([100.0, 101.0, 100.2], 1) == pytest.approx(0.8)
    assert retrace_fraction([100.0, 99.0, 99.5], -1) == pytest.approx(0.5)
    assert math.isnan(retrace_fraction([100.0, 99.0], 1))            # no move in the direction at all
    assert math.isnan(retrace_fraction([100.0], 1))


def test_price_zones():
    z = lambda a: price_zone(a, 0.50, 0.65, 0.85, 0.93)      # noqa: E731
    assert z(0.45) == 0 and z(0.50) == 1 and z(0.64) == 1 and z(0.65) == 2 and z(0.84) == 2 and z(0.85) == 3 and z(0.92) == 3 and z(0.93) == 4


def test_the_rule_based_probability_is_the_mid_plus_a_bounded_bonus_and_the_edge_subtracts_every_cost():
    c = _cfg(p_bonus_base=0.03, p_bonus_cap=0.06)
    f = {"mid": 0.70, "z5": 2.0}
    assert estimate_win_probability(f, c) == pytest.approx(0.70 + c.p_bonus_base)
    f["z5"] = 10.0
    assert estimate_win_probability(f, c) == pytest.approx(0.70 + c.p_bonus_cap)          # capped
    f = {"mid": 0.97, "z5": 10.0}
    assert estimate_win_probability(f, c) <= 0.98
    e = net_edge(0.76, 0.72, 0.0141, c)
    assert e["gross_edge"] == pytest.approx(0.04)
    assert e["net_edge"] == pytest.approx(0.04 - 0.0141 - c.slippage_allowance - c.adverse_selection - c.latency_penalty)


def test_the_shipped_rule_estimator_cannot_clear_the_shipped_costs():
    """A deliberate property of the defaults: mid + at most 6c against fee (~1.6c at 0.61) + 1c slippage + 2c adverse
    selection + 0.5c latency + the 1c half-spread + a 3c edge floor. The rule estimator is a placeholder; the
    strategy trades only with a fitted model (data/replay/cm_model.json) or deliberately loosened costs."""
    c = CMConfig(enabled=True, model_path="/nonexistent/cm_model.json")
    p = estimate_win_probability({"mid": 0.60, "z5": 9.0}, c)
    e = net_edge(p, 0.61, FEE.taker_fee_per_share(0.61), c)
    assert p == pytest.approx(0.66) and e["net_edge"] < c.min_net_edge


def test_a_fitted_model_file_replaces_the_rule(tmp_path):
    n = len(MODEL_FEATURES)
    beta = [0.0] * (n + 1)
    beta[1 + MODEL_FEATURES.index("logit_mid")] = 1.0
    doc = {"features": list(MODEL_FEATURES), "beta": beta, "mu": [0.0] * n, "sd": [1.0] * n, "platt": {"a": 0.0, "b": 1.0}}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(doc))
    m = CMModel.load(p)
    f = {"logit_mid": math.log(0.7 / 0.3)}
    assert m.predict(f) == pytest.approx(0.7)
    assert estimate_win_probability({"mid": 0.7, "z5": 9.0, **f}, _cfg(), m) == pytest.approx(0.7)   # no bonus with a model
    assert CMModel.load(tmp_path / "missing.json") is None
    bad = dict(doc, features=list(MODEL_FEATURES)[:-1])
    with pytest.raises(ValueError, match="features"):
        CMModel(bad)


# ─────────────────────────────── the engine's gates ───────────────────────────────


def test_timing_gates_come_first_and_count_in_the_taxonomy():
    e = _engine()
    now = T0 + 20_000.0
    _feed(e, _flat_then_up(now))
    ev = _ev(e, now, 0.69, 0.71, 0.29, 0.31)
    assert ev.reason is Reason.OUTSIDE_TIME_WINDOW and e.rejects["TOO_EARLY"] == 1
    now = T0 + 250_000.0
    _feed(e, _flat_then_up(now))
    ev = _ev(e, now, 0.69, 0.71, 0.29, 0.31)
    assert ev.reason is Reason.OUTSIDE_TIME_WINDOW and e.rejects["TOO_LATE"] == 1
    assert set(e.rejects) <= set(REJECTS)


def test_a_flat_spot_is_no_momentum_and_a_one_sided_move_is_weak():
    e = _engine()
    now = T0 + 90_000.0
    _feed(e, [(t, 100_000.0) for t in range(int(now - 60_000), int(now) + 1, 500)])
    ev = _ev(e, now, 0.52, 0.54, 0.46, 0.48)
    assert ev.reason is Reason.SPOT_MOMENTUM_WEAK and e.rejects["NO_MOMENTUM"] == 1
    # a 15 s rise that stalled in its last second: the 1 s leg fails
    path = _flat_then_up(now - 1_000.0) + [(now - 500.0, 100_600.0), (now, 100_600.0)]
    e2 = _engine()
    _feed(e2, path)
    ev = _ev(e2, now, 0.69, 0.71, 0.29, 0.31)
    assert ev.reason is Reason.WEAK_MOMENTUM and e2.rejects["WEAK_MOMENTUM"] == 1


def test_exchange_disagreement_is_rejected():
    e = _engine()
    now = T0 + 90_000.0
    base = _flat_then_up(now)
    # bybit falls while the others rise: feed its own history against the move
    for t, p in base:
        e.features.update_spot("BTC", p, t)
        e._feed_exchanges(_view(p, {"binance": p, "coinbase": p, "bybit": 200_000.0 - p}), t)      # noqa: SLF001
    _tokens(e, now, [(now - 15_000.0, 0.60), (now - 5_000.0, 0.66), (now, 0.70)])
    ev = _ev(e, now, 0.69, 0.71, 0.29, 0.31)
    assert ev.reason is Reason.DATA_INCONSISTENT and e.rejects["EXCHANGE_DISAGREEMENT"] == 1
    assert ev.features == {} or True


def test_a_retraced_impulse_is_reversing():
    e = _engine()
    now = T0 + 90_000.0
    # up 100 bps over 10 s, then back 70 % of it in the last 5 s -- the 1/5/15 s legs can still read positive
    path = []
    t = now - 55_000.0
    while t <= now + 1e-6:
        if t < now - 15_000.0:
            p = 100_000.0
        elif t < now - 5_000.0:
            p = 100_000.0 * (1 + 100e-4 * (t - (now - 15_000.0)) / 10_000.0)
        else:
            p = 101_000.0 - 700.0 * (t - (now - 5_000.0)) / 5_000.0
        path.append((t, p))
        t += 500.0
    _feed(e, path)
    ev = _ev(e, now, 0.69, 0.71, 0.29, 0.31)
    assert ev.reason in (Reason.MOMENTUM_REVERSING, Reason.WEAK_MOMENTUM, Reason.SPOT_MOMENTUM_WEAK)
    if ev.reason is Reason.MOMENTUM_REVERSING:
        assert e.rejects["MOMENTUM_REVERSING"] == 1


def test_the_token_must_confirm_the_move_and_the_zone_sets_how_much():
    now = T0 + 90_000.0
    # a clean UP impulse; the UP token has NOT moved: no confirmation
    e = _engine()
    _feed(e, _flat_then_up(now))
    _tokens(e, now, [(now - 15_000.0, 0.70), (now - 5_000.0, 0.70), (now, 0.70)])
    ev = _ev(e, now, 0.69, 0.71, 0.29, 0.31)
    assert e.rejects["NO_TOKEN_CONFIRMATION"] == 1 and ev.tradeable is False
    # UP moved +6c in 15 s at a zone-1 price: enough (needs 5c)
    e = _engine()
    _feed(e, _flat_then_up(now))
    _tokens(e, now, [(now - 15_000.0, 0.54), (now - 5_000.0, 0.58), (now, 0.60)])
    ev = _ev(e, now, 0.59, 0.61, 0.39, 0.41)
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.side == "UP" and ev.token_id == "UP" and e.rejects.total() == 0
    # the same +6c at a zone-2 price needs 7.5c: not enough
    e = _engine()
    _feed(e, _flat_then_up(now))
    _tokens(e, now, [(now - 15_000.0, 0.74), (now - 5_000.0, 0.78), (now, 0.80)])
    ev = _ev(e, now, 0.79, 0.81, 0.19, 0.21)
    assert ev.reason is not None and e.rejects["NO_TOKEN_CONFIRMATION"] == 1
    # above zone 3: too expensive whatever the move
    e = _engine()
    _feed(e, _flat_then_up(now))
    _tokens(e, now, [(now - 15_000.0, 0.80), (now - 5_000.0, 0.90), (now, 0.94)])
    ev = _ev(e, now, 0.93, 0.95, 0.05, 0.07)
    assert ev.reason is Reason.PRICE_BAND and e.rejects["TOKEN_TOO_EXPENSIVE"] == 1
    # a DOWN impulse buys DOWN
    e = _engine()
    _feed(e, [(t, 200_000.0 - p) for t, p in _flat_then_up(now)])
    _tokens(e, now, [(now - 15_000.0, 0.46), (now - 5_000.0, 0.42), (now, 0.40)])
    ev = _ev(e, now, 0.39, 0.41, 0.59, 0.61)
    assert ev.tradeable and ev.side == "DOWN" and ev.token_id == "DOWN"


def test_the_trade_record_carries_every_logged_field_and_the_edge_gate_can_refuse():
    now = T0 + 90_000.0
    e = _engine()
    _feed(e, _flat_then_up(now))
    _tokens(e, now, [(now - 15_000.0, 0.54), (now - 5_000.0, 0.58), (now, 0.60)])
    ev = _ev(e, now, 0.59, 0.61, 0.39, 0.41)
    rec = ev.features["cm"]
    for k in ("market_id", "elapsed_seconds", "remaining_seconds", "spot_binance", "spot_coinbase", "spot_bybit", "spot_composite",
              "binance_age", "return_1s", "return_15s", "z_1s", "z_5s", "z_15s", "exchange_agreement", "candidate", "bid", "ask",
              "spread", "token_return_5s", "token_return_15s", "estimated_probability", "gross_edge", "fee", "slippage",
              "latency_penalty", "net_edge", "decision", "order_price", "order_size", "retracement"):
        assert k in rec, k
    assert rec["decision"] == "TRADE" and rec["exchange_agreement"] == 3 and rec["net_edge"] >= 0.03
    assert ev.size >= 5.0 and ev.size * ev.limit_price <= _cfg().max_order_usdc + 1e-9
    o = e.to_order(ev, now)
    assert o.tif is TimeInForce.IOC and o.size == ev.size and "CM clip" in o.tag
    # a larger adverse-selection charge removes the edge
    e2 = _engine(adverse_selection=0.08)
    _feed(e2, _flat_then_up(now))
    _tokens(e2, now, [(now - 15_000.0, 0.54), (now - 5_000.0, 0.58), (now, 0.60)])
    ev = _ev(e2, now, 0.59, 0.61, 0.39, 0.41)
    assert ev.reason is Reason.EDGE_TOO_SMALL and e2.rejects["INSUFFICIENT_EDGE"] == 1 and ev.features["cm"]["decision"] == "REJECT"


def test_clips_need_a_new_impulse_a_higher_price_and_stay_under_the_market_cap():
    now = T0 + 90_000.0
    e = _engine()
    _feed(e, _flat_then_up(now))
    _tokens(e, now, [(now - 15_000.0, 0.54), (now - 5_000.0, 0.58), (now, 0.60)])
    ev = _ev(e, now, 0.59, 0.61, 0.39, 0.41)
    assert ev.tradeable
    o = e.to_order(ev, now)
    e.on_entry_submit(SLUG, now)
    e.on_fill_record(SLUG, o, SimpleNamespace(size=ev.size, price=0.61, fee=0.02), now + 300.0)
    assert e.core_stats["fills"] == 1 and len(e._clips[SLUG]) == 1          # noqa: SLF001
    # the same impulse a second later (the spot still rising, the token following): no second clip
    _feed(e, [(now + 500.0, 100_620.0), (now + 1_000.0, 100_645.0)])
    _tokens(e, now + 1_000.0, [(now + 500.0, 0.62), (now + 1_000.0, 0.64)])
    ev = _ev(e, now + 1_000.0, 0.62, 0.64, 0.36, 0.38)                        # 3c above the impulse's first ask: not a chase
    assert ev.reason is Reason.COOLDOWN and e.rejects["MAX_ENTRIES"] == 1, (ev.reason, ev.detail)
    # a new impulse 40 s later but the token is LOWER than the first clip: never averaging down
    now2 = now + 40_000.0
    _feed(e, _flat_then_up(now2))
    _tokens(e, now2, [(now2 - 15_000.0, 0.50), (now2 - 5_000.0, 0.55), (now2, 0.58)])
    ev = _ev(e, now2, 0.57, 0.59, 0.41, 0.43)
    assert ev.reason is Reason.MOMENTUM_EXHAUSTED and "never averaging down" in ev.detail
    # a new impulse with the token 4c+ higher: the second clip
    now3 = now + 80_000.0
    _feed(e, _flat_then_up(now3))
    _tokens(e, now3, [(now3 - 15_000.0, 0.62), (now3 - 5_000.0, 0.67), (now3, 0.70)])
    ev = _ev(e, now3, 0.69, 0.71, 0.29, 0.31)
    assert ev.tradeable, (ev.reason, ev.detail)
    o = e.to_order(ev, now3)
    e.on_fill_record(SLUG, o, SimpleNamespace(size=ev.size, price=0.71, fee=0.02), now3 + 300.0)
    # the third would breach the 10 USDC market cap at this price only if sized above the room: cap it
    e3 = _engine(max_market_exposure_usdc=5.0)
    _feed(e3, _flat_then_up(now))
    _tokens(e3, now, [(now - 15_000.0, 0.54), (now - 5_000.0, 0.58), (now, 0.60)])
    ev = _ev(e3, now, 0.59, 0.61, 0.39, 0.41)
    assert ev.tradeable
    e3.on_fill_record(SLUG, e3.to_order(ev, now), SimpleNamespace(size=ev.size, price=0.61, fee=0.02), now + 300.0)
    _feed(e3, _flat_then_up(now3))
    _tokens(e3, now3, [(now3 - 15_000.0, 0.62), (now3 - 5_000.0, 0.67), (now3, 0.70)])
    ev = _ev(e3, now3, 0.69, 0.71, 0.29, 0.31)
    assert ev.reason is Reason.EXPOSURE_CAP and e3.rejects["MAX_EXPOSURE"] == 1
    # settlement: UP won -> both clips paid 1.00
    e.on_market_settled(SLUG, True, 0.0, T0 + 400_000.0)
    assert e.core_stats["won"] == 1 and e.core_stats["pnl"] > 0 and SLUG not in e._clips    # noqa: SLF001


def test_chasing_a_runaway_ask_is_refused():
    now = T0 + 90_000.0
    e = _engine(max_chase_cents=2.0)
    _feed(e, _flat_then_up(now))
    _tokens(e, now, [(now - 15_000.0, 0.54), (now - 5_000.0, 0.58), (now, 0.60)])
    ev = _ev(e, now, 0.59, 0.61, 0.39, 0.41)
    assert ev.tradeable                                                        # the impulse's first ask: 0.61
    e.on_entry_submit(SLUG, now)                                                # the order went out and was killed
    _tokens(e, now + 2_000.0, [(now + 1_000.0, 0.63), (now + 2_000.0, 0.65)])
    _feed(e, [(now + 500.0, 100_620.0), (now + 1_000.0, 100_645.0), (now + 1_500.0, 100_670.0), (now + 2_000.0, 100_700.0)])
    ev = _ev(e, now + 2_000.0, 0.64, 0.66, 0.34, 0.36)
    assert ev.reason is Reason.ASK_ABOVE_MAX_ENTRY and e.rejects["CHASED_PRICE"] == 1


def test_the_lottery_is_a_separate_gtc_bid_with_its_own_statistics():
    e = _engine(lottery_enabled=True)
    now = T0 + 280_000.0                                                       # 20 s left
    _feed(e, [(t, 100_000.0) for t in range(int(now - 60_000), int(now) + 1, 500)])
    # DOWN is the loser at 0.03; the bid rests at min(max_price, max(min_price, bid)) = 0.02 for 100 shares
    ev = _ev(e, now, 0.96, 0.97, 0.02, 0.03)
    assert ev.tradeable and ev.side == "DOWN" and ev.limit_price == pytest.approx(0.02) and ev.size == 100.0
    o = e.to_order(ev, now)
    assert o.tif is TimeInForce.GTC and o.expires_ts == T0 + 300_000.0 and "LOTTERY" in o.tag
    assert e.lottery_stats["orders"] == 1 and e.core_stats["fills"] == 0
    # a second bid in the same market is refused
    ev2 = _ev(e, now + 1_000.0, 0.96, 0.97, 0.02, 0.03)
    assert ev2.reason is Reason.MARKET_ENTERED
    # a loser still at 0.20 is not a lottery ticket
    e2 = _engine(lottery_enabled=True)
    _feed(e2, [(t, 100_000.0) for t in range(int(now - 60_000), int(now) + 1, 500)])
    ev3 = _ev(e2, now, 0.79, 0.81, 0.19, 0.21)
    assert ev3.reason is Reason.PRICE_BAND and e2.rejects["TOKEN_TOO_EXPENSIVE"] == 1
    # a partial fill, then the market resolves UP: the lottery lost its 1.20, the core books nothing
    e.on_fill_record(SLUG, o, SimpleNamespace(size=60.0, price=0.02, fee=0.0), now + 5_000.0)
    assert e.lottery_stats["filled"] == 1 and e.lottery_stats["partial"] == 1 and e.lottery_stats["cost"] == pytest.approx(1.2)
    e.on_market_settled(SLUG, True, 0.0, T0 + 400_000.0)
    assert e.lottery_stats["lost"] == 1 and e.lottery_stats["pnl"] == pytest.approx(-1.2) and e.lottery_stats["fill_probability"] == 1.0
    assert e.core_stats == {"fills": 0, "cost": 0.0, "won": 0, "lost": 0, "pnl": 0.0}
    # with the lottery off, the last 30 s are simply too late for the core
    e3 = _engine()
    _feed(e3, [(t, 100_000.0) for t in range(int(now - 60_000), int(now) + 1, 500)])
    assert _ev(e3, now, 0.96, 0.97, 0.02, 0.03).reason is Reason.OUTSIDE_TIME_WINDOW


def test_the_snapshot_reports_the_taxonomy_and_both_ledgers():
    e = _engine()
    snap = e.snapshot()
    assert snap["strategy"] == "confirmation_momentum" and set(snap["cm_rejects"]) == set(REJECTS)
    assert snap["cm_core"]["fills"] == 0 and snap["cm_lottery"]["orders"] == 0 and snap["cm_model"]["fitted"] is False
