"""Spot age measured from the exchanges' own quote timestamps (2026-09-30). Before, it was
(now - model update) + a Binance-only trade lag, which with Binance off was a constant 120 ms."""
from __future__ import annotations

import pytest

from troll_poly_bot.feeds.spot import SpotQuote
from troll_poly_bot.live import LiveBot

T = 1_790_800_000_000.0


def _bot(tmp_path):
    bot = LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"))
    clock = [T]
    bot.clock.now = lambda: clock[0]
    bot.composite.warmup_ms = 0.0
    bot._ensure_asset("BTC")
    return bot, clock


def _q(x, recv, event=None, price=84_000.0):
    return SpotQuote("BTC", x, price - 0.5, price + 0.5, price, recv, event)


def test_age_comes_from_the_newest_stamped_quote_not_a_constant(tmp_path):
    bot, clock = _bot(tmp_path)
    # coinbase's quote is stamped 70 ms before it reached us, kraken's 35 ms
    bot._on_quote(_q("coinbase", T, T - 70.0))
    bot._on_quote(_q("kraken", T + 5.0, T + 5.0 - 35.0))
    assert bot._spot_newest["BTC"] == (T - 30.0, T + 5.0, "kraken")
    clock[0] = T + 10.0
    bot._update_model("BTC", bot.composite.view("BTC", clock[0]), clock[0])
    # 40 ms: kraken's 35 ms of delivery + 5 ms since it landed. Never 120.
    assert bot.spot_age_ms("BTC", T + 10.0) == pytest.approx(40.0)
    assert bot.spot_age_ms("BTC", T + 110.0) == pytest.approx(140.0)     # the price the model used, ageing
    rep = bot.latency.report()
    assert rep["spot_p50_coinbase"] == pytest.approx(70.0) and rep["spot_p50_kraken"] == pytest.approx(35.0)
    assert rep["n_spot"] == 2 and bot._quote_stamped == {"coinbase", "kraken"}


def test_an_older_stamp_arriving_later_does_not_replace_newer_information(tmp_path):
    bot, _ = _bot(tmp_path)
    bot._on_quote(_q("kraken", T, T - 35.0))
    bot._on_quote(_q("deribit", T + 20.0, T - 200.0))           # arrives later, describes an older market
    assert bot._spot_newest["BTC"][2] == "kraken"
    # its age sample is the age of the newest information when it landed: 20 + 35
    assert sorted(bot.latency.spot) == [pytest.approx(35.0), pytest.approx(55.0)]


def test_an_unstamped_source_is_dated_by_its_measured_lag(tmp_path):
    bot, _ = _bot(tmp_path)
    bot.composite.lag["binance"] = 150.0                          # bookTicker carries no event time
    bot._on_quote(_q("binance", T))
    assert bot._spot_newest["BTC"] == (T - 150.0, T, "binance")
    # its trades still feed the per-source report; a stamped source's trades no longer do
    bot._on_source_lag("binance", 152.0)
    bot._on_quote(_q("coinbase", T + 1.0, T - 69.0))
    bot._on_source_lag("coinbase", 400.0)                         # a trade-batch lag, not the quote delay
    rep = bot.latency.report()
    assert rep["spot_p50_binance"] == pytest.approx(152.0)
    assert rep["spot_p50_coinbase"] == pytest.approx(70.0)
    assert bot.snapshot()["spot_delay_from"] == {"binance": "trade", "coinbase": "quote"}


def test_a_lagging_source_never_counts_as_the_newest_and_bad_stamps_are_ignored(tmp_path):
    bot, _ = _bot(tmp_path)
    bot.composite.lagging.add("bybit")
    bot._on_quote(_q("bybit", T, T - 5.0))
    assert "BTC" not in bot._spot_newest
    bot._on_quote(_q("kraken", T, T - 3_600_000.0))                # an hour off: a clock or parse fault
    assert "kraken" not in bot._quote_stamped
    assert bot.latency.report().get("spot_p50_kraken") is None


def test_snapshot_reports_the_age_and_source(tmp_path):
    bot, clock = _bot(tmp_path)
    bot._on_quote(_q("bitstamp", T, T - 46.0))
    clock[0] = T + 4.0
    bot._update_model("BTC", bot.composite.view("BTC", clock[0]), clock[0])
    snap = bot.snapshot()
    assert snap["spot_age"]["BTC"] == {"source": "bitstamp", "age_ms": 50.0, "delay_ms": 46.0}
    assert snap["measured_latency_ms"]["n_spot"] == 1 and snap["measured_latency_ms"]["spot_p50"] == 46.0
