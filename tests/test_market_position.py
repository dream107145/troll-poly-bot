"""The Market page's position panel (the user, 2026-10-09): live.py _market_position -- each token's shares, cost
(fees in, net of sells), value at the bid, and the market's P&L now and if UP / DOWN wins."""
from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace

import pytest

from troll_poly_bot.live import LiveBot
from troll_poly_bot.types import Fill, Position, Side


def _book(bid):
    return SimpleNamespace(to_order_book=lambda: SimpleNamespace(best_bid=bid, best_ask=None if bid is None else bid + 0.01))


def _bot(fills, bids):
    positions = defaultdict(lambda: Position(""))
    for tok, side, px, size, fee in fills:
        positions[tok].apply(Fill("o", tok, side, px, size, fee, 0.0, 0.0, 0.0, expected_price=px))
    market = SimpleNamespace(yes_token_id="UP", no_token_id="DN")
    lm = SimpleNamespace(meta=SimpleNamespace(market=market), books={t: _book(b) for t, b in bids.items()})
    bot = SimpleNamespace(exchange=SimpleNamespace(positions=positions), _last_mark={})
    bot._true_book = lambda tok: None
    bot._mark = lambda tok: LiveBot._mark(bot, tok)
    return bot, lm


def test_a_pair_shows_each_tokens_cost_and_the_pnl_of_each_outcome():
    bot, lm = _bot([("UP", Side.BUY, 0.55, 10.0, 0.12), ("DN", Side.BUY, 0.38, 10.0, 0.0)], {"UP": 0.61, "DN": 0.37})
    p = LiveBot._market_position(bot, lm)
    assert p["up"]["shares"] == 10.0 and p["up"]["cost"] == pytest.approx(5.62) and p["up"]["avg"] == pytest.approx(0.562)
    assert p["down"]["cost"] == pytest.approx(3.80) and p["down"]["value"] == pytest.approx(3.70)
    assert p["cost"] == pytest.approx(9.42)
    assert p["pnl_up_win"] == pytest.approx(10.0 - 9.42) and p["pnl_down_win"] == pytest.approx(10.0 - 9.42)
    assert p["pnl_now"] == pytest.approx(6.10 + 3.70 - 9.42)


def test_one_side_held_and_a_partial_sell_count_net():
    bot, lm = _bot([("UP", Side.BUY, 0.60, 10.0, 0.0), ("UP", Side.SELL, 0.70, 4.0, 0.0)], {"UP": 0.65, "DN": 0.34})
    p = LiveBot._market_position(bot, lm)
    assert p["up"]["shares"] == 6.0 and p["up"]["cost"] == pytest.approx(6.0 - 2.8)       # net of the sell
    assert p["down"]["shares"] == 0.0 and p["down"]["avg"] is None and p["down"]["value"] == 0.0
    assert p["pnl_up_win"] == pytest.approx(6.0 - 3.2) and p["pnl_down_win"] == pytest.approx(-3.2)
    assert p["pnl_now"] == pytest.approx(6.0 * 0.65 - 3.2)


def test_no_trade_is_none_and_a_held_token_without_a_bid_uses_equity_mark():
    bot, lm = _bot([], {"UP": 0.5, "DN": 0.5})
    assert LiveBot._market_position(bot, lm) is None
    bot, lm = _bot([("UP", Side.BUY, 0.60, 10.0, 0.0)], {"UP": None, "DN": 0.4})
    # No book bid: fall back to the same mark equity uses (here avg cost 0.60).
    p = LiveBot._market_position(bot, lm)
    assert p["up"]["bid"] is None and p["up"]["mark"] == pytest.approx(0.60)
    assert p["up"]["value"] == pytest.approx(6.0)
    assert p["pnl_now"] == pytest.approx(0.0) and p["pnl_up_win"] == pytest.approx(4.0)
