"""MOMENTUM_TAKER, 2026-09-23 rework: states, exhaustion, max entry price,
execution-adjusted edge, liquidity, uncertainty and the momentum-aware exits."""
from __future__ import annotations

import json
import math

import pytest

from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine, SpotHistory
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.pricing.twap import TwapState
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.momentum import (
    NEUTRAL, REVERSING, STRONG_DOWN, STRONG_UP, WEAK_UP, Calibration, MomentumConfig, MomentumEngine,
    MomentumSignal, calculate_net_edge, detect_momentum, estimate_uncertainty, exit_cost_estimate,
    get_executable_price, max_entry_price,
)
from troll_poly_bot.types import BookLevel, Market, OrderBook

T0 = 1_000_000_000.0
NOW = T0 + 120_000.0
SLUG = "btc-updown-5m-1"
SIGMA = 1e-4
FEE = FeeSchedule()


def _hist_from_seconds(cum_bps: list[float], flat_before_s: float = 30.0, step_ms: float = 250.0) -> SpotHistory:
    """A path given as the cumulative move (bps) at each whole second, ending
    NOW; linear inside each second, flat for ``flat_before_s`` before it."""
    h = SpotHistory()
    n = len(cum_bps)
    start = NOW - (n - 1) * 1000.0
    t = start - flat_before_s * 1000.0
    while t < start:
        h.update(100.0, t)
        t += step_ms
    for k in range(n - 1):
        a, b = cum_bps[k], cum_bps[k + 1]
        for j in range(int(1000 / step_ms)):
            frac = j * step_ms / 1000.0
            h.update(100.0 * math.exp((a + (b - a) * frac) * 1e-4), start + k * 1000.0 + j * step_ms)
    h.update(100.0 * math.exp(cum_bps[-1] * 1e-4), NOW)
    return h


def _ramp(total_bps: float, secs: int = 10) -> list[float]:
    """Accelerating: quadratic over ``secs`` seconds."""
    return [total_bps * (i / secs) ** 2 for i in range(secs + 1)]


# ─────────────────────────────── states ───────────────────────────────


def test_six_states():
    cfg = MomentumConfig()
    strong = detect_momentum(_hist_from_seconds(_ramp(20.0)), NOW, SIGMA, cfg)
    assert strong.label == STRONG_UP and strong.strong and strong.score > 0.5
    down = detect_momentum(_hist_from_seconds(_ramp(-20.0)), NOW, SIGMA, cfg)
    assert down.label == STRONG_DOWN and down.direction == -1
    weak = detect_momentum(_hist_from_seconds(_ramp(4.0)), NOW, SIGMA, cfg)      # z5 ~ 1.3
    assert weak.label == WEAK_UP and not weak.strong
    flat = detect_momentum(_hist_from_seconds([0.0] * 11), NOW, SIGMA, cfg)
    assert flat.label == NEUTRAL and flat.score == 0.0


def test_the_signal_is_multi_window_and_carries_its_evidence():
    sig = detect_momentum(_hist_from_seconds(_ramp(20.0)), NOW, SIGMA, MomentumConfig())
    assert {"1s", "3s", "main", "10s", "15s", "30s"} <= set(sig.z)
    assert sig.velocity["1s"] > sig.velocity["main"] > 0                  # accelerating
    assert sig.acceleration_bps > 0 and sig.persistence == 1.0 and sig.consistency >= 0.6
    d = sig.as_dict()
    assert d["label"] == STRONG_UP and "velocity_bps_s" in d and d["exhaustion"] == []


def test_one_tick_is_never_a_signal():
    h = SpotHistory()
    h.update(100.0, NOW - 30_000.0)
    h.update(100.0 * math.exp(30e-4), NOW)                                 # a single jump, no path
    e = _engine()
    e.features.spot["BTC"] = h
    e.features.update_market(SLUG, 0.50, NOW - 4000.0)
    ev = _run(e)
    assert not ev.tradeable


# ────────────────────────────── exhaustion ─────────────────────────────


def test_the_brief_example_is_exhausted_not_bought():
    """+0.01 +0.04 +0.08 +0.12 +0.14 +0.13 +0.10 % : the move is large, and over."""
    path = [0.0] * 4 + [1.0, 4.0, 8.0, 12.0, 14.0, 13.0, 10.0]
    path = [x * 10.0 for x in path]                                         # percent -> bps (x100) / 10 for scale
    sig = detect_momentum(_hist_from_seconds(path), NOW, SIGMA, MomentumConfig())
    assert sig.label == REVERSING
    # one step earlier (+0.13 after +0.14) the continuation had already failed
    sig2 = detect_momentum(_hist_from_seconds(path[:-1]), NOW, SIGMA, MomentumConfig())
    assert sig2.label == REVERSING and sig2.exhaustion


def test_a_retrace_from_the_peak_is_exhaustion():
    path = [0.0, 0.0, 0.0, 0.0, 0.0, 8.0, 16.0, 24.0, 30.0, 25.0, 24.0]   # up 24 bps in 5 s, 6 below the peak
    sig = detect_momentum(_hist_from_seconds(path), NOW, SIGMA, MomentumConfig(exhaustion_retrace=0.15))
    assert sig.direction == 1 and not sig.reversing
    assert any("retrace" in x for x in sig.exhaustion) and sig.label == REVERSING


def test_a_stall_is_exhaustion_and_the_engine_skips_it():
    path = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 6.0, 12.0, 18.0, 18.2, 18.3]        # strong 5 s, nothing in the last 2
    cfg = MomentumConfig(require_acceleration=False, consistency_min=0.0, exhaustion_retrace=0.0)
    sig = detect_momentum(_hist_from_seconds(path), NOW, SIGMA, cfg)
    assert any("stall" in x for x in sig.exhaustion)
    e = _engine(_mc(require_acceleration=False, consistency_min=0.0, exhaustion_retrace=0.0))
    e.features.spot["BTC"] = _hist_from_seconds(path)
    e.features.update_market(SLUG, 0.50, NOW - 4000.0)
    assert _run(e).reason is Reason.MOMENTUM_EXHAUSTED


def test_a_book_turning_against_the_move_is_exhaustion():
    e = _engine(_strong())
    e.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e.features.update_market(SLUG, 0.49, NOW - 3500.0)                     # re-priced +3c over 3 s ...
    e.features.update_market(SLUG, 0.55, NOW - 1600.0)                     # ... but it was 0.55 1.5 s ago
    ev = _run(e)
    assert ev.reason is Reason.MOMENTUM_EXHAUSTED and "book reversal" in ev.detail


def test_a_jumpy_path_is_a_volatility_spike():
    path = [0.0, 0.0, 12.0, 0.0, 12.0, 0.0, 12.0, 0.0, 12.0, 14.0, 16.0]   # whipsaw, then up
    sig = detect_momentum(_hist_from_seconds(path), NOW, SIGMA, MomentumConfig(exhaustion_retrace=0.0,
                                                                              exhaustion_velocity_ratio=0.0))
    assert sig.vol_ratio >= 3.0
    clean = detect_momentum(_hist_from_seconds(_ramp(20.0)), NOW, SIGMA, MomentumConfig())
    assert clean.vol_ratio < 3.0                                             # a clean trend is not a spike


# ─────────────────────────── executable price ───────────────────────────


def test_multi_level_vwap_example_from_the_brief():
    book = OrderBook("UP", asks=[BookLevel(0.45, 2.0), BookLevel(0.46, 3.0), BookLevel(0.47, 5.0),
                                 BookLevel(0.50, 20.0)])
    px, avail = get_executable_price(book, 10.0)
    assert px == pytest.approx((2 * 0.45 + 3 * 0.46 + 5 * 0.47) / 10)      # 0.463, never 0.45
    assert px != pytest.approx(0.45)
    px, avail = get_executable_price(book, 10.0, cap=0.46)
    assert px is None and avail == 5.0                                      # only 5 under the cap


def test_max_entry_price_formula():
    # fair 0.56, costs ~2c: the most it may pay leaves the required edge after its own fee
    p = max_entry_price(0.56, FEE, fixed_costs=0.005, min_net_edge=0.0)
    assert p is not None and p + FEE.taker_fee_per_share(p) <= 0.555 + 1e-9
    assert (p + 0.01) + FEE.taker_fee_per_share(p + 0.01) > 0.555           # the next tick would not
    assert max_entry_price(0.56, FEE, 0.005, 0.02) < p                      # a higher bar pays less
    assert max_entry_price(0.02, FEE, 0.05, 0.02) is None                   # nothing is worth it


def test_ask_above_max_entry_is_refused_and_the_cap_never_exceeds_it():
    e = _engine(_strong())
    e.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e.features.update_market(SLUG, 0.50, NOW - 4000.0)
    ev = _run(e)
    assert ev.tradeable and ev.limit_price <= ev.max_entry_price + 1e-9
    # a 2 c continuation on the same 0.52 mid, but the ask sits 3 c above it:
    # the price is more than the signal is worth after its costs -> no chase
    e2 = _engine(_strong(continuation_cap=0.02, max_spread=0.10))
    e2.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e2.features.update_market(SLUG, 0.50, NOW - 4000.0)
    rich = OrderBook("UP", bids=[BookLevel(0.49, 200.0)], asks=[BookLevel(0.55, 100.0)])
    ev2 = _run(e2, up=rich)
    assert ev2.reason is Reason.ASK_ABOVE_MAX_ENTRY
    assert ev2.max_entry_price is not None and ev2.max_entry_price < 0.55


def test_revalidation_refuses_a_price_above_max_entry():
    e = _engine(_strong())
    e.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e.features.update_market(SLUG, 0.50, NOW - 4000.0)
    ev = _run(e)
    assert ev.tradeable
    ev.max_entry_price = 0.53                                                # tighten after the fact
    assert e.revalidate(ev, OrderBook("UP", asks=[BookLevel(0.54, 100.0)])) in (Reason.ASK_ABOVE_MAX_ENTRY,
                                                                                Reason.HIGH_SLIPPAGE)
    assert e.revalidate(ev, OrderBook("UP", asks=[BookLevel(0.53, 100.0)])) is None


# ───────────────────────── execution-adjusted edge ─────────────────────────


def test_exit_costs_and_uncertainty_are_charged():
    cb = calculate_net_edge(0.56, 0.48, 0.46, FEE, _mc(expected_slippage=0.005, latency_cost_per_s=0.0),
                            reaction_lag_s=0.0, uncertainty=0.01, exit_cost=0.02)
    assert cb.net_edge == pytest.approx(0.08 - FEE.taker_fee_per_share(0.48) - 0.005 - 0.01 - 0.02)
    assert cb.uncertainty == 0.01 and cb.exit_cost == 0.02
    assert exit_cost_estimate(0.50, 0.02, FEE) == pytest.approx(FEE.taker_fee_per_share(0.50) + 0.01)


def test_uncertainty_grows_with_the_claim_a_weak_score_and_vol():
    cfg = MomentumConfig()
    strong = MomentumSignal(1, STRONG_UP, z={"main": 4.0}, score=1.0)
    weak = MomentumSignal(1, STRONG_UP, z={"main": 4.0}, score=0.2)
    assert estimate_uncertainty(0.03, weak, cfg) > estimate_uncertainty(0.03, strong, cfg)
    assert estimate_uncertainty(0.05, strong, cfg) > estimate_uncertainty(0.01, strong, cfg)
    jumpy = MomentumSignal(1, STRONG_UP, z={"main": 4.0}, score=1.0, vol_ratio=2.5)
    assert estimate_uncertainty(0.03, jumpy, cfg) > estimate_uncertainty(0.03, strong, cfg)
    per_asset = MomentumConfig(asset_uncertainty="XRP:0.02")
    assert estimate_uncertainty(0.03, strong, per_asset, "XRP") == pytest.approx(
        estimate_uncertainty(0.03, strong, per_asset, "BTC") + 0.02)


def test_with_real_costs_a_measured_continuation_does_not_trade_at_the_money():
    """The archive's measured continuation (1 c/sigma above 2) cannot pay two
    taker fees and a spread at 0.50: the strict default must say so."""
    e = _engine(MomentumConfig())                                            # every cost on
    e.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e.features.update_market(SLUG, 0.50, NOW - 4000.0)
    ev = _run(e)
    assert not ev.tradeable and ev.reason in (Reason.ASK_ABOVE_MAX_ENTRY, Reason.EDGE_TOO_SMALL)
    exe = ev.features["execution"]
    assert exe["exit_cost"] > 0.015 and exe["uncertainty"] > 0.0


def test_calibration_shrinks_toward_the_prior_and_charges_its_error(tmp_path):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"horizon_s": 20, "rows": {"BTC|3-4": {"n_windows": 30, "mean": 0.03, "se": 0.01}}}))
    cal = Calibration.load(str(p))
    cont, se = cal.continuation("BTC", 3.5, prior=0.01)
    assert cont == pytest.approx(0.5 * 0.03 + 0.5 * 0.01) and se == 0.01
    assert cal.continuation("ETH", 3.5, prior=0.01) == (0.01, None)
    assert Calibration.load(str(tmp_path / "missing.json")) is None


# ─────────────────────────────── liquidity ───────────────────────────────


def test_the_order_may_not_take_most_of_the_book():
    e = _engine(_strong(max_depth_fraction=0.5), risk_cfg=RiskConfig(max_position_usdc=50.0, min_order_usdc=1.0,
                                                                     micro_fixed_size=True, micro_min_shares=8.0))
    e.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e.features.update_market(SLUG, 0.50, NOW - 4000.0)
    thin = OrderBook("UP", bids=[BookLevel(0.51, 200.0)], asks=[BookLevel(0.53, 12.0)])   # 8 of 12 = 67 %
    ev = _run(e, up=thin)
    assert not ev.tradeable and ev.reason in (Reason.LOW_LIQUIDITY, Reason.SIZE_ZERO)


def test_walking_the_book_is_bounded():
    e = _engine(_strong(max_vwap_slippage=0.003), risk_cfg=RiskConfig(max_position_usdc=50.0, min_order_usdc=1.0,
                                                                      micro_fixed_size=True, micro_min_shares=10.0,
                                                                      max_depth_fraction=1.0))
    e.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e.features.update_market(SLUG, 0.50, NOW - 4000.0)
    book = OrderBook("UP", bids=[BookLevel(0.51, 200.0)], asks=[BookLevel(0.53, 11.0), BookLevel(0.54, 40.0)])
    ev = _run(e, up=book)
    # 10 shares fit at 0.53 -> fine
    assert ev.tradeable, ev.detail
    # 20 shares: 11 at 0.53 and 9 at 0.54 -> VWAP 0.5345, 0.45 c past the touch
    e.risk.cfg.micro_min_shares = 20.0
    ev2 = _run(e, up=book, now=NOW)
    assert ev2.reason is Reason.LOW_LIQUIDITY and "VWAP" in ev2.detail


# ─────────────────────────── freshness gates ───────────────────────────


def test_a_stale_quote_is_never_traded_and_a_fresh_one_is():
    e = _engine(_strong(max_quote_age_ms=500.0))
    e.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e.features.update_market(SLUG, 0.50, NOW - 4000.0)
    ev = _run(e, quote_ages={"UP": 800.0, "DOWN": 40.0})
    assert ev.reason is Reason.STALE_DATA and "quote" in ev.detail
    ev = _run(e, quote_ages={"UP": None, "DOWN": 40.0})
    assert ev.reason is Reason.STALE_DATA
    e2 = _engine(_strong(max_quote_age_ms=500.0))
    e2.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e2.features.update_market(SLUG, 0.50, NOW - 4000.0)
    assert _run(e2, quote_ages={"UP": 120.0, "DOWN": 40.0}).tradeable


def test_stale_spot_is_refused_at_the_momentum_limit():
    e = _engine(_strong(max_spot_age_ms=1000.0))
    e.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    e.features.update_market(SLUG, 0.50, NOW - 4000.0)
    assert _run(e, spot_age=1200.0).reason is Reason.STALE_DATA


def test_asset_filter():
    e = _engine(_strong(assets="ETH,SOL"))
    e.features.spot["BTC"] = _hist_from_seconds(_ramp(20.0))
    assert _run(e).reason is Reason.ASSET_FILTERED


# ───────────────────────────────── exits ─────────────────────────────────


def test_exit_decisions_are_momentum_aware():
    e = _engine(_mc(take_profit=0.04, stop_loss=0.06, max_hold_s=45.0, expected_continuation_s=20.0,
                    exit_weak_z=1.0))
    e.on_entry_fill(SLUG, "UP", NOW)
    going = MomentumSignal(1, STRONG_UP, z={"main": 3.0})
    faded = MomentumSignal(1, NEUTRAL, z={"main": 0.2})
    kw = dict(asset="BTC", side="UP", token_id="UP", entry=0.50)
    assert e.exit_decision(**kw, bid=0.54, now_ms=NOW + 1000, sig=going) == "TAKE_PROFIT"
    assert e.exit_decision(**kw, bid=0.44, now_ms=NOW + 1000, sig=going) == "STOP_LOSS"
    # momentum continues: hold, even past the continuation horizon
    assert e.exit_decision(**kw, bid=0.50, now_ms=NOW + 25_000, sig=going) is None
    # momentum faded, no profit, past the horizon: the thesis had its chance
    assert e.exit_decision(**kw, bid=0.50, now_ms=NOW + 25_000, sig=faded) == "TIME_STOP"
    # faded but still in profit before the take-profit: hold until the hard maximum
    assert e.exit_decision(**kw, bid=0.52, now_ms=NOW + 25_000, sig=faded) is None
    assert e.exit_decision(**kw, bid=0.52, now_ms=NOW + 46_000, sig=going) == "TIME_STOP"
    # reversal: the move in our direction is over
    rev = MomentumSignal(1, REVERSING, z={"main": 2.0}, reversing=True)
    assert e.exit_decision(**kw, bid=0.51, now_ms=NOW + 2000, sig=rev) == "MOMENTUM_REVERSAL"
    against = MomentumSignal(-1, STRONG_DOWN, z={"main": -2.0})
    assert e.exit_decision(**kw, bid=0.51, now_ms=NOW + 2000, sig=against) == "MOMENTUM_REVERSAL"


def test_market_abnormality_and_liquidity_failure_exits():
    e = _engine(_mc(abnormal_spread=0.10, abnormal_data_age_ms=3000.0, liquidity_exit_mult=1.5))
    e.on_entry_fill(SLUG, "UP", NOW)
    ok = MomentumSignal(1, STRONG_UP, z={"main": 3.0})
    kw = dict(asset="BTC", side="UP", token_id="UP", entry=0.50, bid=0.50, now_ms=NOW + 1000, sig=ok, size=5.0)
    assert e.exit_decision(**kw, spread=0.02, bid_depth=100.0, data_age_ms=200.0) is None
    assert e.exit_decision(**kw, spread=0.12, bid_depth=100.0, data_age_ms=200.0) == "MARKET_ABNORMALITY"
    assert e.exit_decision(**kw, spread=0.02, bid_depth=100.0, data_age_ms=4000.0) == "MARKET_ABNORMALITY"
    assert e.exit_decision(**kw, spread=0.02, bid_depth=6.0, data_age_ms=200.0) == "LIQUIDITY_FAILURE"


def test_live_bot_walks_the_bids_for_an_urgent_exit(tmp_path):
    from troll_poly_bot.config import BotConfig
    from troll_poly_bot.live import LiveBook, LiveBot, LiveMarket
    from troll_poly_bot.types import Position
    cfg = BotConfig()
    cfg.engine.strategy = "momentum_taker"
    cfg.momentum = _mc(stop_loss=0.06, exit_max_cross=0.05, liquidity_exit_mult=0.0)
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"))
    market = Market(condition_id="c", asset="BTC", yes_token_id="Y", no_token_id="N", strike=100.0, open_ts=0,
                    close_ts=300_000.0, tick_size=0.01, min_size=5.0, slug=SLUG)
    lm = LiveMarket(meta=MarketMeta(market=market, resolution_source="", accepting_orders=True, duration_s=300))
    for token in ("Y", "N"):
        lm.books[token] = LiveBook(token)
    # a 6c loss; 2 shares at 0.44, the rest at 0.41: a 2-tick cross would be killed
    lm.books["Y"].bids = {0.44: 2.0, 0.41: 50.0}
    lm.books["Y"].asks = {0.46: 50.0}
    bot.markets[SLUG] = lm
    bot.token_index["Y"] = bot.token_index["N"] = (SLUG, lm)
    bot.exchange.positions["Y"] = Position(token_id="Y", shares=5.0, cost_basis=2.5)
    ex = bot._exit_candidate(lm, "Y", 1000.0)
    assert ex is not None and ex["kind"] == "STOP_LOSS" and ex["px"] == pytest.approx(0.41)


# ─────────────────────────────── helpers ───────────────────────────────


NEUTRAL_COSTS = dict(include_exit_costs=False, uncertainty_base=0.0, uncertainty_frac=0.0, uncertainty_vol=0.0)


def _mc(**kw):
    base = dict(NEUTRAL_COSTS)
    base.update(kw)
    return MomentumConfig(**base)


def _strong(**kw):
    base = dict(min_net_edge=0.01, continuation_cap=0.08, continuation_per_sigma=0.02, max_slippage=0.02)
    base.update(kw)
    return _mc(**base)


def _engine(mcfg=None, risk_cfg=None):
    risk = RiskManager(cfg=risk_cfg or RiskConfig(max_position_usdc=50.0, min_order_usdc=1.0, micro_fixed_size=True),
                       starting_balance=100.0)
    return MomentumEngine(EngineConfig(strategy="momentum_taker"), mcfg or _mc(), risk, FeatureEngine())


def _meta():
    return MarketMeta(market=Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN",
                                    strike=100.0, open_ts=T0, close_ts=T0 + 300_000.0, slug=SLUG, min_size=5.0),
                      resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)


class _Vol:
    ready = True
    sigma_per_sec = SIGMA


def _run(e, up=None, down=None, spot_age=300.0, book_age=200.0, now=NOW, quote_ages=None):
    up = up or OrderBook("UP", bids=[BookLevel(0.51, 200.0)], asks=[BookLevel(0.53, 100.0), BookLevel(0.54, 100.0)])
    down = down or OrderBook("DOWN", bids=[BookLevel(0.46, 200.0)], asks=[BookLevel(0.49, 100.0)])
    view = CompositeView("BTC", 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
    return e.evaluate(_meta(), up, down, view, spot_age, book_age, now, _Vol(), TwapState(), COLOCATED, 100.0,
                      quote_ages=quote_ages)


def test_a_stall_after_entry_is_weakening_not_a_reversal_exit():
    e = _engine(_mc(expected_continuation_s=20.0))
    e.on_entry_fill(SLUG, "UP", NOW)
    stalled = MomentumSignal(1, REVERSING, z={"main": 2.5}, reversing=False, exhaustion=["stall"])
    kw = dict(asset="BTC", side="UP", token_id="UP", entry=0.50, bid=0.50)
    assert e.exit_decision(**kw, now_ms=NOW + 2000, sig=stalled) is None          # prepare, do not pay to leave
    faded = MomentumSignal(1, NEUTRAL, z={"main": 0.3})
    assert e.exit_decision(**kw, now_ms=NOW + 21_000, sig=faded) == "TIME_STOP"   # the thesis had its chance
