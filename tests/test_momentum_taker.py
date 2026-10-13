"""MOMENTUM_TAKER: detection, executable price, edge, the entry pipeline, exits, isolation."""
from __future__ import annotations

import math

import pytest

from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine, SpotHistory
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.pricing.twap import TwapState
from troll_poly_bot.pricing.vol import EwmaVol
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.momentum import (
    REVERSING, STRONG_DOWN, STRONG_UP, WEAK, MomentumConfig, MomentumEngine, MomentumSignal,
    calculate_momentum_score, calculate_net_edge, detect_momentum, detect_momentum_reversal,
    estimate_expected_token_value, get_executable_price,
)
from troll_poly_bot.types import BookLevel, Market, OrderBook, Side, TimeInForce

#: the economics added on 2026-09-23 (exit costs, model uncertainty) have their
#: own tests in test_momentum_v2.py; these tests pin the other mechanics
NEUTRAL_COSTS = dict(include_exit_costs=False, uncertainty_base=0.0, uncertainty_frac=0.0, uncertainty_vol=0.0)


def _mc(**kw):
    base = dict(NEUTRAL_COSTS)
    base.update(kw)
    return MomentumConfig(**base)


T0 = 1_000_000_000.0
NOW = T0 + 120_000.0
SLUG = "btc-updown-5m-1"
SIGMA = 1e-4                           # per second


def _hist(path):
    """path: list of (seconds before NOW, log price offset). Sampled every 500 ms."""
    h = SpotHistory()
    for s_ago, lp in path:
        h.update(100.0 * math.exp(lp), NOW - s_ago * 1000.0)
    return h


def _ramp(total_bps: float, secs: float = 10.0, flat_before: float = 40.0, accelerating: bool = True):
    """Flat for ``flat_before`` s, then a move of ``total_bps`` over ``secs`` s, ending now.

    Quadratic by default: the last 5 s carry 75 % of the move, so the 5 s
    window is strong, every 1 s step is in the direction and it accelerates.
    """
    pts = [(t, 0.0) for t in [flat_before - i * 0.5 for i in range(int((flat_before - secs) * 2))]]
    n = int(secs * 2)
    for i in range(n + 1):
        frac = (i / n) ** (2 if accelerating else 0.5)
        pts.append((secs - i * 0.5, frac * total_bps * 1e-4))
    return pts


# ────────────────────────────── detection ─────────────────────────────────


def test_strong_up_momentum_is_detected_with_consistency_and_acceleration():
    sig = detect_momentum(_hist(_ramp(+10.0)), NOW, SIGMA, MomentumConfig())
    assert sig.direction == 1 and sig.label == STRONG_UP
    assert sig.z["main"] > 3.0 and sig.consistency >= 0.6 and sig.acceleration_bps > 0
    assert sig.n_updates >= 3 and not sig.reversing
    assert 0.0 < calculate_momentum_score(sig, MomentumConfig()) <= 1.0


def test_strong_down_momentum():
    sig = detect_momentum(_hist(_ramp(-10.0)), NOW, SIGMA, MomentumConfig())
    assert sig.direction == -1 and sig.label == STRONG_DOWN and sig.z["main"] < -3.0


def test_a_small_move_is_weak():
    sig = detect_momentum(_hist(_ramp(+1.0)), NOW, SIGMA, MomentumConfig())
    assert sig.label in (WEAK, "WEAK_UP") and not sig.strong and abs(sig.z["main"]) < 3.0
    assert calculate_momentum_score(sig, MomentumConfig()) < 0.6


def test_a_move_that_has_started_reversing_is_flagged():
    pts = _ramp(+20.0)
    # the last 3 s give back 4 bps of a 20 bps move: still > 3 sigma over 5 s, but turning
    pts = [(t, lp) for t, lp in pts if t > 3.0] + [(3.0, 20e-4), (1.5, 18e-4), (0.0, 16e-4)]
    sig = detect_momentum(_hist(pts), NOW, SIGMA, MomentumConfig())
    assert sig.reversing and sig.label == REVERSING


def test_no_history_or_no_sigma_is_weak():
    assert detect_momentum(None, NOW, SIGMA, MomentumConfig()).label == WEAK
    assert detect_momentum(_hist(_ramp(+10.0)), NOW, 0.0, MomentumConfig()).label == WEAK


def test_reversal_for_an_open_position():
    cfg = MomentumConfig(exit_reversal_z=1.5)
    sig = MomentumSignal(direction=-1, label=STRONG_DOWN, z={"main": -2.0})
    assert detect_momentum_reversal(sig, "UP", cfg)
    assert not detect_momentum_reversal(sig, "DOWN", cfg)
    assert not detect_momentum_reversal(MomentumSignal(direction=0, label=WEAK), "UP", cfg)


# ─────────────────────────── price and edge ───────────────────────────────


def _book(token, bid, asks):
    return OrderBook(token, bids=[BookLevel(bid, 200.0)] if bid is not None else [],
                     asks=[BookLevel(p, s) for p, s in asks], ts=NOW)


def test_executable_price_walks_the_asks_not_the_mid():
    book = _book("UP", 0.44, [(0.46, 5.0), (0.47, 10.0), (0.50, 100.0)])
    px, avail = get_executable_price(book, 5.0)
    assert px == pytest.approx(0.46)
    px, avail = get_executable_price(book, 10.0)
    assert px == pytest.approx((5 * 0.46 + 5 * 0.47) / 10)
    px, avail = get_executable_price(book, 20.0, cap=0.47)
    assert px is None and avail == 15.0                     # not enough under the cap
    assert get_executable_price(None, 5.0) == (None, 0.0)
    assert get_executable_price(_book("UP", 0.44, []), 5.0) == (None, 0.0)


def test_expected_value_is_mid_plus_measured_continuation():
    cfg = MomentumConfig(continuation_per_sigma=0.01, continuation_base_z=2.0, continuation_cap=0.05)
    assert estimate_expected_token_value(0.50, MomentumSignal(1, STRONG_UP, z={"main": 3.0}), cfg) == pytest.approx(0.51)
    assert estimate_expected_token_value(0.50, MomentumSignal(1, STRONG_UP, z={"main": 9.0}), cfg) == pytest.approx(0.55)
    assert estimate_expected_token_value(0.50, MomentumSignal(1, STRONG_UP, z={"main": 1.0}), cfg) == pytest.approx(0.50)


def test_net_edge_charges_fee_slippage_and_latency():
    cfg = MomentumConfig(expected_slippage=0.005, latency_cost_per_s=0.01)
    cb = calculate_net_edge(0.56, 0.48, 0.46, FeeSchedule(), cfg, reaction_lag_s=0.5)
    fee = FeeSchedule().taker_fee_per_share(0.48)
    assert cb.gross_edge == pytest.approx(0.08)
    assert cb.net_edge == pytest.approx(0.08 - fee - 0.005 - 0.005)
    flat = calculate_net_edge(0.50, 0.48, 0.46, FeeSchedule(), cfg, 0.0)
    assert flat.net_edge < 0.0                              # 2c gross does not cover the fee


# ───────────────────────────── the pipeline ───────────────────────────────


def _meta():
    return MarketMeta(market=Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN",
                                    strike=100.0, open_ts=T0, close_ts=T0 + 300_000.0, slug=SLUG, min_size=5.0),
                      resolution_source="", accepting_orders=True, duration_s=300.0, fee=FeeSchedule())


def _view(price=100.0):
    return CompositeView("BTC", price, price, 3, 1.0, 1.0, 50.0, {"binance": price})


class _Vol:
    ready = True
    sigma_per_sec = SIGMA


def _engine(mcfg=None, risk_cfg=None, **eng):
    # the smallest valid order (as micro-live sizes it) keeps Kelly out of these tests
    risk = RiskManager(cfg=risk_cfg or RiskConfig(max_position_usdc=50.0, min_order_usdc=1.0, micro_fixed_size=True),
                       starting_balance=100.0)
    engine = MomentumEngine(EngineConfig(strategy="momentum_taker", **eng), mcfg or _mc(), risk, FeatureEngine())
    return engine


def _prime(engine, path=None, mid_prev=0.50, mid_prev_at=4.0):
    """Feed spot history and the token mid 4 s ago."""
    for s_ago, lp in (path or _ramp(+10.0)):
        engine.features.update_spot("BTC", 100.0 * math.exp(lp), NOW - s_ago * 1000.0)
    engine.features.update_market(SLUG, mid_prev, NOW - mid_prev_at * 1000.0)


def _run(engine, up=None, down=None, spot_age=300.0, book_age=200.0, now=NOW, view=None):
    if up is None:
        up = _book("UP", 0.51, [(0.53, 100.0), (0.54, 100.0)])       # mid 0.52: re-priced +2c vs 0.50
    if down is None:
        down = _book("DOWN", 0.46, [(0.49, 100.0)])
    return engine.evaluate(_meta(), up, down, view or _view(), spot_age, book_age, now, _Vol(), TwapState(),
                           COLOCATED, 100.0)


def test_stale_data_is_the_first_gate():
    e = _engine()
    _prime(e)
    assert _run(e, spot_age=5000.0).reason is Reason.STALE_DATA
    assert _run(e, book_age=None).reason is Reason.STALE_DATA
    assert _run(e, view=None, spot_age=None).reason is Reason.STALE_DATA


def test_time_remaining_gates():
    e = _engine(_mc(min_time_remaining_s=90.0))
    _prime(e)
    assert _run(e, now=T0 + 220_000.0).reason is Reason.OUTSIDE_TIME_WINDOW      # 80 s left
    assert _run(e, now=T0 + 2_000.0).reason is Reason.OUTSIDE_TIME_WINDOW        # 298 s left


def test_weak_momentum_is_rejected_with_the_measured_z():
    e = _engine()
    _prime(e, _ramp(+1.0))
    ev = _run(e)
    assert ev.reason is Reason.WEAK_MOMENTUM and "z5" in ev.detail


def test_reversing_momentum_is_rejected():
    e = _engine()
    pts = [(t, lp) for t, lp in _ramp(+20.0) if t > 3.0] + [(3.0, 20e-4), (1.5, 18e-4), (0.0, 16e-4)]
    _prime(e, pts)
    assert _run(e).reason is Reason.MOMENTUM_REVERSING


def test_a_token_that_has_not_repriced_is_a_stale_quote():
    e = _engine()
    _prime(e, mid_prev=0.52)                                  # mid 4 s ago == mid now
    ev = _run(e)
    assert ev.reason is Reason.STALE_QUOTE and ev.sides[0].reason is Reason.STALE_QUOTE


def test_a_token_that_already_moved_too_far_is_skipped():
    e = _engine()
    _prime(e, mid_prev=0.40)                                  # +12c in 3 s: chased
    assert _run(e).reason is Reason.TOKEN_ALREADY_MOVED


def test_missing_ask_wide_spread_and_thin_depth():
    e = _engine(_strong_cfg())
    _prime(e, _ramp(+20.0))
    assert _run(e, up=_book("UP", 0.51, [])).reason is Reason.NO_OFFER
    # a book that re-priced but is 6c wide
    wide = OrderBook("UP", bids=[BookLevel(0.49, 200.0)], asks=[BookLevel(0.55, 100.0)], ts=NOW)
    assert _run(e, up=wide).reason is Reason.LOW_LIQUIDITY
    thin = _book("UP", 0.51, [(0.53, 3.0), (0.60, 500.0)])    # 3 shares under the cap (0.55)
    ev = _run(e, up=thin)
    assert ev.reason is Reason.LOW_LIQUIDITY and "depth" in ev.detail


def test_low_edge_reports_the_arithmetic():
    e = _engine(_mc(min_net_edge=0.02, continuation_cap=0.02))
    _prime(e)
    ev = _run(e)
    # a 1.35c continuation cannot pay a 1.75c fee: the ask is above the most
    # this signal may pay, which is the low-edge case stated as a price
    assert ev.reason is Reason.ASK_ABOVE_MAX_ENTRY
    assert "fair" in ev.detail and "max entry" in ev.detail and ev.max_entry_price < 0.53
    cb = ev.sides[0].costs
    assert cb is not None and cb.ask == pytest.approx(0.53)   # the executable ask, not the mid


def _strong_cfg(**kw):
    base = dict(min_net_edge=0.01, continuation_cap=0.08, continuation_per_sigma=0.02, max_slippage=0.02)
    base.update(kw)
    return _mc(**base)


def test_tradeable_signal_is_a_marketable_limit_with_a_price_cap():
    e = _engine(_strong_cfg())
    _prime(e, _ramp(+20.0))
    ev = _run(e)
    assert ev.tradeable, ev.as_dict()
    assert ev.side == "UP" and ev.limit_price == pytest.approx(0.55)        # ask 0.53 + 2c cap
    assert ev.expected_price == pytest.approx(0.53) and ev.size >= 5.0
    order = e.to_order(ev, NOW)
    assert order.tif is TimeInForce.FOK and order.side is Side.BUY and order.price == pytest.approx(0.55)
    assert "momentum" in order.tag


def test_revalidation_never_chases_past_the_cap():
    e = _engine(_strong_cfg())
    _prime(e, _ramp(+20.0))
    ev = _run(e)
    assert ev.tradeable
    assert e.revalidate(ev, _book("UP", 0.51, [(0.53, 100.0)])) is None
    assert e.revalidate(ev, _book("UP", 0.55, [(0.58, 100.0)])) is Reason.HIGH_SLIPPAGE
    assert e.revalidate(ev, _book("UP", 0.51, [(0.55, 2.0)])) is Reason.LOW_LIQUIDITY
    assert e.revalidate(ev, None) is Reason.NO_OFFER


def test_duplicate_position_cooldown_and_max_trades():
    e = _engine(_strong_cfg(cooldown_s=60.0, max_trades_per_market=1))
    _prime(e, _ramp(+20.0))
    e.risk.on_fill(SLUG, "BTC", int(T0 // 1000), "UP", 2.6, 5.0)
    assert _run(e).reason is Reason.ALREADY_POSITIONED
    e.risk.on_settle(SLUG, 0.1)
    e.on_exit(SLUG, "UP", NOW - 10_000.0)
    assert _run(e).reason is Reason.COOLDOWN
    e.last_exit_ms.clear()
    e.on_entry_fill(SLUG, "UP", NOW - 100_000.0)
    e.on_exit(SLUG, "UP", NOW - 100_000.0)
    assert _run(e).reason is Reason.MAX_TRADES


def test_the_short_side_is_evaluated_on_the_down_token():
    e = _engine(_strong_cfg())
    _prime(e, _ramp(-20.0), mid_prev=0.50)
    down = _book("DOWN", 0.51, [(0.53, 100.0)])              # DOWN mid 0.52: re-priced +2c
    up = _book("UP", 0.46, [(0.49, 100.0)])
    ev = _run(e, up=up, down=down)
    assert ev.tradeable and ev.side == "DOWN" and ev.token_id == "DOWN"


# ────────────────────────────────── exits ─────────────────────────────────


def test_exit_kinds():
    e = _engine(_mc(take_profit=0.04, stop_loss=0.06, max_hold_s=45.0, exit_reversal_z=1.5))
    e.on_entry_fill(SLUG, "UP", NOW)
    assert e.exit_kind("BTC", "UP", "UP", 0.50, 0.54, NOW + 1000) == "TAKE_PROFIT"
    assert e.exit_kind("BTC", "UP", "UP", 0.50, 0.44, NOW + 1000) == "STOP_LOSS"
    assert e.exit_kind("BTC", "UP", "UP", 0.50, 0.51, NOW + 1000) is None
    e.last_signal["BTC"] = MomentumSignal(-1, STRONG_DOWN, z={"main": -2.0})
    assert e.exit_kind("BTC", "UP", "UP", 0.50, 0.51, NOW + 1000) == "MOMENTUM_REVERSAL"
    e.last_signal.clear()
    assert e.exit_kind("BTC", "UP", "UP", 0.50, 0.51, NOW + 46_000) == "TIME_STOP"
    e.on_exit(SLUG, "UP", NOW + 46_000)
    assert e.exit_kind("BTC", "UP", "UP", 0.50, 0.51, NOW + 90_000) is None    # no entry time: no time stop


def test_live_bot_uses_the_momentum_exits(tmp_path):
    from troll_poly_bot.live import LiveBook, LiveBot, LiveMarket
    from troll_poly_bot.types import Position
    cfg = BotConfig()
    cfg.engine.strategy = "momentum_taker"
    cfg.momentum = MomentumConfig(take_profit=0.04, stop_loss=0.06)
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"),
                  trade_log=str(tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))
    assert isinstance(bot.engine, MomentumEngine)
    market = Market(condition_id="c", asset="BTC", yes_token_id="Y", no_token_id="N", strike=100.0,
                    open_ts=0, close_ts=300_000.0, tick_size=0.01, min_size=5.0, slug=SLUG)
    lm = LiveMarket(meta=MarketMeta(market=market, resolution_source="", accepting_orders=True, duration_s=300))
    for token in ("Y", "N"):
        lm.books[token] = LiveBook(token)
    lm.books["Y"].bids = {0.53: 500.0}
    bot.markets[SLUG] = lm
    bot.exchange.positions["Y"] = Position(token_id="Y", shares=5.0, cost_basis=2.5, fees_paid=0.0)
    ex = bot._exit_candidate(lm, "Y", 1000.0)
    assert ex is None                                       # +3c: below the 4c momentum take-profit
    lm.books["Y"].bids = {0.55: 500.0}
    ex = bot._exit_candidate(lm, "Y", 1000.0)
    assert ex is not None and ex["kind"] == "TAKE_PROFIT"
    assert bot.snapshot()["rejections"] is not None


def test_unknown_strategy_is_refused(tmp_path):
    from troll_poly_bot.live import LiveBot
    cfg = BotConfig()
    cfg.engine.strategy = "magic"
    with pytest.raises(ValueError):
        LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"),
                trade_log=str(tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))


def test_config_from_env_and_defaults_are_the_measured_ones():
    m = MomentumConfig.from_env({"TPB_MOMENTUM_Z_THRESHOLD": "4", "TPB_MOMENTUM_MAX_TRADES_PER_MARKET": "2",
                                 "TPB_MOMENTUM_REQUIRE_ACCELERATION": "0", "TPB_MOMENTUM_TAKE_PROFIT": "junk"})
    assert m.z_threshold == 4.0 and m.max_trades_per_market == 2 and m.require_acceleration is False
    assert m.take_profit == MomentumConfig().take_profit
    d = MomentumConfig()
    assert d.z_threshold == 3.0 and d.min_net_edge == 0.02 and d.max_spread == 0.03 and d.max_slippage == 0.02
    assert d.min_reprice == 0.01 and d.max_reprice == 0.05 and d.max_hold_s == 45.0


def test_a_down_signal_reaches_the_order_path_in_the_live_bot(tmp_path):
    """The momentum Evaluation carries ONE side; the live bot used to index sides[1] for DOWN
    and crash the tick (4 of 5 real signals lost on 2026-09-23)."""
    from troll_poly_bot.live import LiveBook, LiveBot, LiveMarket
    cfg = BotConfig()
    cfg.engine.strategy = "momentum_taker"
    cfg.momentum = _strong_cfg()
    cfg.risk.micro_fixed_size = True
    cfg.risk.max_position_usdc = 50.0
    cfg.risk.min_order_usdc = 1.0
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"),
                  trade_log=str(tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))
    now = bot.clock.now()
    market = Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN", strike=100.0,
                    open_ts=now - 30_000.0, close_ts=now + 270_000.0, tick_size=0.01, min_size=5.0, slug=SLUG)
    lm = LiveMarket(meta=MarketMeta(market=market, resolution_source="", accepting_orders=True, duration_s=300))
    for token in ("UP", "DOWN"):
        lm.books[token] = LiveBook(token)
        lm.books[token].ts = now
    lm.books["DOWN"].bids, lm.books["DOWN"].asks = {0.51: 200.0}, {0.53: 100.0}
    lm.books["UP"].bids, lm.books["UP"].asks = {0.46: 200.0}, {0.49: 100.0}
    bot.markets[SLUG] = lm
    bot.token_index["UP"] = bot.token_index["DOWN"] = (SLUG, lm)
    for s_ago, lp in _ramp(-20.0):
        t = now - s_ago * 1000.0
        px = 100.0 * math.exp(lp)
        bot.engine.features.update_spot("BTC", px, t)
        bot.twap.setdefault("BTC", TwapState()).update(px, t)
    bot.engine.features.update_market(SLUG, 0.50, now - 4000.0)
    bot.spot["BTC"], bot.spot_ts["BTC"] = 100.0, now - 200.0
    bot.vol["BTC"] = _Vol()

    bot.composite = type("C", (), {"view": staticmethod(lambda asset, t: _view(100.0)),
                                   "counts": {}})()
    bot._tick()
    assert bot.exec_rejections.get("NO_SIDE_VIEW", 0) == 0
    assert bot.decisions.summary()["orders"]["ORDER_SUBMITTED"] == 1
    assert lm.submitted == 1


# ───────────────────────────── profiles and marks ─────────────────────────────


def test_strict_profile_is_the_default_from_env_and_easy_is_opt_in():
    """2026-09-23: a threshold is not lowered without evidence. The easy
    profile's measured net was -1.6 c/share; it stays selectable, not default."""
    strict = MomentumConfig.from_env({})
    assert strict.profile == "strict" and strict.z_threshold == 3.0 and strict.min_net_edge == 0.02
    easy = MomentumConfig.from_env({"TPB_MOMENTUM_PROFILE": "easy"})
    assert easy.profile == "easy" and easy.z_threshold == 2.0 and easy.min_net_edge == 0.005
    assert easy.require_acceleration is False and easy.max_ask == 0.85
    assert easy.include_exit_costs and easy.max_quote_age_ms == strict.max_quote_age_ms   # the safety checks stay
    tuned = MomentumConfig.from_env({"TPB_MOMENTUM_PROFILE": "easy", "TPB_MOMENTUM_MAX_ASK": "0.7"})
    assert tuned.max_ask == 0.7 and tuned.z_threshold == 2.0


def test_an_ask_above_the_momentum_ceiling_is_price_banded():
    e = _engine(_strong_cfg(max_ask=0.85))
    _prime(e, _ramp(+20.0), mid_prev=0.88)
    up = _book("UP", 0.89, [(0.91, 300.0)])                 # mid 0.90: re-priced +2c, but above the ceiling
    down = _book("DOWN", 0.08, [(0.11, 300.0)])
    ev = _run(e, up=up, down=down)
    assert ev.reason is Reason.PRICE_BAND and "0.85" in ev.detail


def test_decision_marks_are_kept_per_market_and_forgotten(tmp_path):
    from troll_poly_bot.execution.decisions import DecisionJournal
    from troll_poly_bot.strategy.engine import Evaluation, SideView
    from troll_poly_bot.signals.costs import CostBreakdown
    j = DecisionJournal(tmp_path / "d.jsonl", log_transitions=False)
    cb = CostBreakdown(model_p=0.55, market_p=0.5, ask=0.51, gross_edge=0.04, fee=0.0175, half_spread=0.005,
                       slippage=0.005, latency_cost=0.0, net_edge=-0.01)
    ev = Evaluation(slug=SLUG, asset="BTC", seconds_left=120.0, reason=Reason.EDGE_TOO_SMALL)
    ev.sides = [SideView("UP", "Y", 0.55, 0.5, 0.51, 0.50, 120.0, cb, Reason.EDGE_TOO_SMALL)]
    j.record(ev, now_ms=1000.0, mode="paper")
    marks = j.marks_for(SLUG)
    assert len(marks) == 1 and marks[0]["ask"] == 0.51 and marks[0]["decision"] == "SIGNAL_REJECTED_LOW_EDGE"
    assert marks[0]["side"] == "UP" and marks[0]["net"] == -0.01
    j.forget(SLUG)
    assert j.marks_for(SLUG) == []


def test_the_server_trims_marks_to_the_selected_chart():
    from troll_poly_bot.web.server import _filter_live as _trim_history
    payload = {"price_history": {"a": [{"t": 1}], "b": [{"t": 2}]},
               "signal_marks": {"a": [{"t": 1, "ask": 0.5}], "b": [{"t": 2, "ask": 0.6}]}}
    out = _trim_history(dict(payload), "a", None)
    assert out["signal_marks"] == {"a": [{"t": 1, "ask": 0.5}]}
    assert _trim_history(dict(payload), None, None)["signal_marks"] == {}
