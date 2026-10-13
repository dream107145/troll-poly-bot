"""The guard as the live bot feeds it: what must NOT trip it."""
from __future__ import annotations

from troll_poly_bot.live import LiveBook, LiveBot, LiveMarket
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.types import Market

SLUG = "btc-updown-5m-1"


def _bot(tmp_path):
    return LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                   earnings_path=str(tmp_path / "e.jsonl"))


def _market(bot, now):
    market = Market(condition_id="c", asset="BTC", yes_token_id="Y", no_token_id="N", strike=100.0,
                    open_ts=now - 1000.0, close_ts=now + 299_000.0, tick_size=0.01, min_size=5.0, slug=SLUG)
    lm = LiveMarket(meta=MarketMeta(market=market, resolution_source="", accepting_orders=True, duration_s=300))
    for token in ("Y", "N"):
        lm.books[token] = LiveBook(token)            # ts 0: no snapshot yet
    bot.markets[SLUG] = lm
    return lm


def test_books_without_a_snapshot_are_not_stale_data(tmp_path):
    bot = _bot(tmp_path)
    now = bot.clock.now()
    _market(bot, now)
    bot.spot_ts["BTC"] = now - 100.0
    bot._update_guard(now)
    assert "STALE_DATA" not in bot.guard.tripped


def test_the_gamma_poller_cannot_be_silent(tmp_path):
    bot = _bot(tmp_path)
    now = bot.clock.now()
    bot._feed_state("gamma", connected=True)
    bot.feed_health["gamma"].last_msg_ms = now - 600_000.0   # ten minutes since the last probe
    bot._feed_state("binance", connected=True, message=True)
    bot._update_guard(now)
    assert "FEED_SILENT" not in bot.guard.tripped and "FEED_DOWN" not in bot.guard.tripped


def test_a_disconnected_streaming_feed_does_trip(tmp_path):
    bot = _bot(tmp_path)
    now = bot.clock.now()
    bot._feed_state("polymarket", connected=False, error="gone")
    bot._update_guard(now)
    assert "FEED_DOWN" in bot.guard.tripped
    assert not bot.guard.entries_allowed
    assert bot.snapshot()["guard"]["tripped"]
