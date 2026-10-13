"""triangle hedge modes (2026-10-01 audit): "resting" (the rule so far) and "executable" -- buy only when the
pair can be completed now (both asks + fees + slippage <= max_pair_cost), then hedge at once with a FOK; a killed
hedge leaves the first leg naked, said loudly, never re-placed as a resting order."""
from __future__ import annotations

import logging

import pytest

from test_spot_lag import NOW, SLUG, _book, _meta
from test_triangle import _bot, _cfg, _shock

from troll_poly_bot.execution.paper import OrderResult, RejectReason
from troll_poly_bot.live import LiveBook, LiveMarket
from troll_poly_bot.strategy.engine import Reason
from troll_poly_bot.types import Fill, Order, OrderState, Side, TimeInForce

EXE = dict(pair_cost=0.96, hedge_mode="executable", max_pair_cost=0.96, hedge_slippage=0.03, shares=5.0,
           max_order_usdc=5.0, limit_offset=0.03, wait_for_pair=False)


def test_the_gate_prices_the_pair_at_both_asks_plus_fees_and_slippage():
    # UP ask 0.52 + DOWN ask 0.50 + fees (0.07 x p(1-p) each ~0.0175) + 0.03 = 1.085 > 0.96: refused
    e, ev = _shock(6.0, up_now=(0.50, 0.52), scfg=_cfg(**EXE))
    assert ev.reason is Reason.PRICE_BAND and "executable pair cost 1.085" in ev.detail and "> max 0.96" in ev.detail
    assert e.shock_marks_for(SLUG)[-1]["ok"] is False
    # UP ask 0.52 + DOWN ask 0.38 + fees 0.0175 + 0.0165 + 0.03 = 0.964 > 0.96 still; at max 0.97 it passes
    e, ev = _shock(6.0, up_now=(0.50, 0.52), scfg=_cfg(**{**EXE, "max_pair_cost": 0.97}))
    # the harness mirrors the DOWN book from the UP book (DOWN ask = 1 - UP bid = 0.50): build one by hand
    assert ev.reason is Reason.PRICE_BAND
    e = __import__("test_triangle")._engine(_cfg(**{**EXE, "max_pair_cost": 0.97}))
    from test_spot_lag import _feed, _Twap
    from test_triangle import _eval
    twap = _Twap()
    _feed(e, twap, 6.0, until=NOW - 1000.0)
    _eval(e, twap, NOW - 1000.0, _book("UP", 0.50, 0.52), _book("DOWN", 0.36, 0.38))
    _feed(e, twap, 6.0, until=NOW)
    ev = _eval(e, twap, NOW, _book("UP", 0.50, 0.52), _book("DOWN", 0.36, 0.38))
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.features["triangle"]["pair_est"] == pytest.approx(0.52 + 0.38 + 0.07 * 0.52 * 0.48 + 0.07 * 0.38 * 0.62 + 0.03, abs=1e-4)
    assert ev.limit_price == 0.55                                             # no "pair room" cap in this mode


def test_the_executable_hedge_is_a_fok_at_the_other_ask_plus_slippage():
    e = __import__("test_triangle")._engine(_cfg(**EXE))
    side, o = e.pair_order(_meta("BTC").market, "UP", 5.0, 0.52, NOW, other_ask=0.38)
    assert side == "DOWN" and o.tif is TimeInForce.FOK and o.price == 0.41 and o.size == 5.0
    assert e.pair_order(_meta("BTC").market, "UP", 5.0, 0.52, NOW, other_ask=None) is None
    assert e.pairs_naked == 1 and e.pairs_skipped == {"no_ask": 1}
    r = __import__("test_triangle")._engine(_cfg(pair_cost=0.96))
    side, o = r.pair_order(_meta("BTC").market, "UP", 5.0, 0.52, NOW, other_ask=0.38)
    assert o.tif is TimeInForce.GTC and o.price == 0.44                       # resting mode unchanged


def test_on_the_live_bot_a_fill_hedges_at_once_and_a_killed_hedge_is_naked(tmp_path, caplog):
    bot = _bot(tmp_path, **EXE)
    lm = LiveMarket(meta=_meta("BTC"))
    bot.markets[SLUG] = lm
    lm.books["DOWN"] = LiveBook("DOWN")
    lm.books["DOWN"].apply_snapshot({"bids": [{"price": "0.36", "size": "50"}], "asks": [{"price": "0.38", "size": "50"}],
                                     "timestamp": str(int(NOW))})
    caplog.set_level(logging.INFO, logger="live")
    o = Order(token_id="UP", side=Side.BUY, price=0.55, size=5.0, tif=TimeInForce.FOK)
    bot.fair_by_order[o.order_id] = {"slug": SLUG, "asset": "BTC", "epoch": lm.epoch, "token_id": "UP", "side": "UP"}
    lm.inflight = 1
    o.filled_size, o.state = 5.0, OrderState.FILLED
    bot._on_result(OrderResult(o, [Fill(o.order_id, "UP", Side.BUY, 0.52, 5.0, 0.0, NOW, NOW, NOW)],   # noqa: SLF001
                               RejectReason.NONE, NOW), NOW)
    (pid, rec), = bot._pairs_open.items()                                     # noqa: SLF001
    assert rec["tif"] == "FOK" and rec["price"] == 0.41 and rec["side"] == "DOWN"
    sent = [s.payload for s in bot.exchange._inflight]                        # noqa: SLF001
    assert len(sent) == 1 and sent[0].tif is TimeInForce.FOK and sent[0].price == 0.41
    assert any("hedge buy sent" in r.getMessage() for r in caplog.records)
    assert list(bot.engine.pairs_working) == [pid]
    # the venue kills it: naked, counted, NOT re-placed
    hedge = sent[0]
    hedge.state = OrderState.REJECTED
    bot._on_result(OrderResult(hedge, [], RejectReason.FOK_UNFILLABLE, NOW + 800.0), NOW + 800.0)   # noqa: SLF001
    assert bot.engine.pairs_naked == 1 and bot.engine.pairs_working == {} and bot._pairs_to_place == []   # noqa: SLF001
    assert any("is NAKED" in r.getMessage() for r in caplog.records)
    assert bot.engine.snapshot()["triangle"]["hedge_mode"] == "executable"


def test_the_dashboard_switches_the_mode_and_the_max_pair_cost(tmp_path):
    from troll_poly_bot.control import apply_to, current_values, validate
    bot = _bot(tmp_path, pair_cost=0.96)
    t = bot._control_targets()                                                # noqa: SLF001
    cur = current_values(bot.cfg, bot.exchange, **t)
    assert cur["triangle.hedge_mode"] == "resting" and cur["triangle.max_pair_cost"] == 0.96
    clean, errors = validate({"triangle.hedge_mode": "executable", "triangle.max_pair_cost": 0.94})
    assert errors == [] and clean == {"triangle.hedge_mode": "executable", "triangle.max_pair_cost": 0.94}
    apply_to(clean, bot.cfg, bot.exchange, **t)
    assert bot.cfg.triangle.hedge_mode == "executable" and bot.cfg.triangle.max_pair_cost == 0.94
    assert validate({"triangle.hedge_mode": "sometimes"})[1]
