"""Lead mode of the composite spot (2026-09-29): the nearest exchange's quote moves the
composite at once; a lone glitch does not. Plus the Kraken adapter's parsers."""
from __future__ import annotations

import pytest

from troll_poly_bot.feeds.spot import CompositeSpot, SpotQuote, parse_kraken, parse_kraken_trades


def _q(exchange, price, ts, asset="BTC"):
    return SpotQuote(asset, exchange, price - 0.5, price + 0.5, price, ts)


def _warm(comp, t0=0.0):
    """Three calibrated sources at the same level."""
    for i in range(8):
        t = t0 + i * 100.0
        for x in ("binance", "bybit", "coinbase"):
            comp.update(_q(x, 100_000.0, t))
    return t0 + 700.0


def test_lead_mode_follows_the_freshest_information_and_the_median_waits():
    plain = CompositeSpot(warmup_ms=0.0)
    lead = CompositeSpot(warmup_ms=0.0, lead_window_ms=30.0)
    lead.lag.update({"binance": 125.0, "bybit": 100.0, "coinbase": 55.0})
    for comp in (plain, lead):
        t = _warm(comp)
        # coinbase shows a 10 bps move; binance and bybit quotes are newer by receipt but
        # describe the market 50 and 35 ms earlier than coinbase's (their feeds are slower)
        comp.update(_q("coinbase", 100_100.0, t + 100.0))
        comp.update(_q("binance", 100_000.0, t + 120.0))
        comp.update(_q("bybit", 100_000.0, t + 110.0))
    now = 900.0
    assert plain.price("BTC", now) == pytest.approx(100_000.0)          # two of three have not moved
    # within 2 bps: the basis learner absorbs a sliver of the move, as it does in the median path
    assert lead.price("BTC", now) == pytest.approx(100_100.0, rel=2e-4)  # the lead is the newest information
    assert lead.view("BTC", now).price == pytest.approx(100_100.0, rel=2e-4)
    assert lead.view("BTC", now).n_sources == 3                         # the rest of the view still sees all
    # once the others confirm, both agree
    lead.update(_q("binance", 100_100.0, now + 80.0))
    lead.update(_q("bybit", 100_100.0, now + 80.0))
    assert lead.price("BTC", now + 100.0) == pytest.approx(100_100.0, rel=2e-4)


def test_a_lone_source_far_from_the_median_is_a_glitch_not_a_lead():
    lead = CompositeSpot(warmup_ms=0.0, lead_window_ms=30.0, lead_max_dev_bps=25.0)
    lead.lag.update({"binance": 125.0, "bybit": 100.0, "coinbase": 55.0})
    t = _warm(lead)
    lead.update(_q("coinbase", 101_000.0, t + 100.0))                    # +100 bps on one exchange
    lead.update(_q("binance", 100_000.0, t + 120.0))
    lead.update(_q("bybit", 100_000.0, t + 120.0))
    assert lead.price("BTC", 900.0) == pytest.approx(100_000.0)
    # without lag figures the receipt time is the information time: the newest quotes lead
    # (a fresh composite: the glitch above also bent coinbase's basis, as it would the median's)
    lead = CompositeSpot(warmup_ms=0.0, lead_window_ms=30.0)
    t = _warm(lead)
    lead.update(_q("coinbase", 100_100.0, t + 300.0))
    assert lead.price("BTC", t + 310.0) == pytest.approx(100_100.0, rel=2e-4)   # binance/bybit quotes are 300 ms older
    lead.update(_q("binance", 100_000.0, t + 305.0))
    assert lead.price("BTC", t + 310.0) == pytest.approx(100_050.0, rel=2e-4)   # coinbase + binance within 30 ms


def test_lead_mode_off_is_the_plain_median_and_one_source_is_itself():
    comp = CompositeSpot(warmup_ms=0.0)
    t = _warm(comp)
    comp.update(_q("coinbase", 100_300.0, t + 100.0))
    assert comp.price("BTC", t + 110.0) == pytest.approx(100_000.0)
    single = CompositeSpot(warmup_ms=0.0, lead_window_ms=30.0)
    single.update(_q("binance", 50.0, 0.0))
    assert single.price("BTC", 10.0) == pytest.approx(50.0)


def test_kraken_parsers():
    syms = {"BTC/USD": "BTC", "ETH/USD": "ETH"}
    msg = {"channel": "ticker", "type": "update",
           "data": [{"symbol": "BTC/USD", "bid": 84000.1, "bid_qty": 2.0, "ask": 84000.9, "ask_qty": 1.0,
                     "last": 84000.5}]}
    (q,) = parse_kraken(msg, syms, 1_000.0)
    assert q.exchange == "kraken" and q.asset == "BTC" and q.bid == 84000.1 and q.ask == 84000.9
    assert 84000.1 < q.price < 84000.9 and q.event_ts_ms is None
    assert parse_kraken({"channel": "heartbeat"}, syms, 1_000.0) == []
    assert parse_kraken({"channel": "ticker", "data": [{"symbol": "SOL/USD", "bid": 1, "ask": 2}]}, syms, 0.0) == []
    trades = parse_kraken_trades({"channel": "trade", "type": "update",
                                  "data": [{"symbol": "ETH/USD", "side": "sell", "price": 2690.5, "qty": 0.1,
                                            "timestamp": "2026-09-29T16:40:48.123456Z"}]}, syms, 2_000.0)
    assert len(trades) == 1 and trades[0].asset == "ETH" and not trades[0].aggressor_buy
    assert trades[0].event_ts_ms == pytest.approx(1790700048123.456, abs=1.0)
