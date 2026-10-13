"""STALE_QUOTE: do not buy the side spot has just moved toward while the token has not re-priced.

27 of the 29 real orders sent in that state on 2026-09-22 were killed by the
venue; the paper engine filled every one. The gate reads two things the
engine now records on every evaluation: the spot move over the window and
the Up mid's change over the same window.
"""
from __future__ import annotations

import math

from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.pricing.twap import TwapState
from troll_poly_bot.pricing.vol import EwmaVol
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.engine import EngineConfig, Reason, StrategyEngine
from troll_poly_bot.types import BookLevel, Market, OrderBook

T0 = 1_000_000_000.0
NOW = T0 + 100_000.0
SLUG = "btc-updown-5m-1"


def _meta(strike=100.0):
    return MarketMeta(market=Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN",
                                    strike=strike, open_ts=T0, close_ts=T0 + 300_000.0, slug=SLUG),
                      resolution_source="", accepting_orders=True, duration_s=300.0, fee=FeeSchedule())


def _book(token, bid, ask, size=200.0):
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)], ts=T0)


def _view(price):
    return CompositeView("BTC", price, price, 3, 1.0, 1.0, 50.0, {"binance": price})


def _warm(price=100.0, sigma_per_sec=1e-4):
    vol = EwmaVol(grid_ms=1000.0, halflife_s=120.0)
    tw = TwapState()
    for s in range(400):
        px = price * (1 + sigma_per_sec * ((s % 2) * 2 - 1))
        t = T0 - 400_000.0 + s * 1000.0
        vol.update(px, t)
        tw.update(px, t)
    tw.update(price, T0)
    return vol, tw


def _engine(**cfg):
    risk = RiskManager(cfg=RiskConfig(), starting_balance=100.0)
    eng = StrategyEngine(EngineConfig(min_net_edge=0.0, confidence_gate_mult=0.0, **cfg), risk)
    return eng


def _run(eng, spot_then, spot_now, mid_then, mid_now, up_ask):
    """Spot moved spot_then -> spot_now over the last 3 s; the Up mid moved mid_then -> mid_now."""
    vol, tw = _warm()
    tw.update(spot_now, NOW - 100.0)
    eng.features.update_spot("BTC", spot_then, NOW - 4_000.0)
    eng.features.update_spot("BTC", spot_then, NOW - 3_500.0)
    eng.features.update_spot("BTC", spot_now, NOW - 200.0)
    eng.features.update_market(SLUG, mid_then, NOW - 4_000.0)
    up = _book("UP", round(up_ask - 0.01, 2), up_ask)
    down = _book("DOWN", round(1 - up_ask - 0.01, 2), round(1 - up_ask, 2))
    # mid_now is what the books say; make the Up book's mid equal it
    up = _book("UP", round(mid_now - 0.005, 3), round(mid_now + 0.005, 3))
    down = _book("DOWN", round(1 - mid_now - 0.005, 3), round(1 - mid_now + 0.005, 3))
    return eng.evaluate(_meta(), up, down, _view(spot_now), 300.0, 200.0, NOW, vol, tw, COLOCATED, 100.0)


def test_spot_up_market_flat_blocks_the_up_side():
    eng = _engine()
    ev = _run(eng, spot_then=100.0, spot_now=100.3, mid_then=0.50, mid_now=0.50, up_ask=0.505)
    assert ev.reason is Reason.STALE_QUOTE, ev.as_dict()
    assert ev.spot_move_bps > 25 and abs(ev.market_reprice) < 0.001
    assert "spot" in ev.detail
    assert [s.reason for s in ev.sides if s.side == "UP"] == [Reason.STALE_QUOTE]


def test_once_the_market_has_repriced_the_gate_lets_it_through():
    eng = _engine()
    ev = _run(eng, spot_then=100.0, spot_now=100.3, mid_then=0.50, mid_now=0.60, up_ask=0.605)
    assert ev.reason is not Reason.STALE_QUOTE
    assert ev.market_reprice > 0.05


def test_a_small_spot_move_inside_the_band_does_not_trip():
    eng = _engine(stale_quote_sigma_mult=100.0)
    ev = _run(eng, spot_then=100.0, spot_now=100.3, mid_then=0.50, mid_now=0.50, up_ask=0.505)
    assert ev.reason is not Reason.STALE_QUOTE


def test_spot_down_blocks_the_down_side_only():
    eng = _engine()
    ev = _run(eng, spot_then=100.3, spot_now=100.0, mid_then=0.50, mid_now=0.50, up_ask=0.505)
    reasons = {s.side: s.reason for s in ev.sides}
    assert reasons["DOWN"] is Reason.STALE_QUOTE
    assert reasons["UP"] is not Reason.STALE_QUOTE


def test_disabled_gate_records_the_numbers_but_does_not_block():
    eng = _engine(stale_quote_enabled=False)
    ev = _run(eng, spot_then=100.0, spot_now=100.3, mid_then=0.50, mid_now=0.50, up_ask=0.505)
    assert ev.reason is not Reason.STALE_QUOTE
    assert not math.isnan(ev.spot_move_bps) and not math.isnan(ev.market_reprice)
    assert ev.as_dict()["spot_move_bps"] is not None


def test_the_market_history_is_now_written_by_the_engine():
    """mkt_chg_* were NaN live because nothing called update_market; now the engine does."""
    eng = _engine(stale_quote_enabled=False)
    _run(eng, 100.0, 100.0, 0.50, 0.50, 0.505)
    assert SLUG in eng.features.market
