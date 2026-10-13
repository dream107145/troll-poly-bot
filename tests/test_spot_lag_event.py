"""Spot-lag, event-driven: a spot shock is evaluated the moment its quote
arrives, not on the next 200 ms tick behind a 100 ms model throttle."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from troll_poly_bot.config import BotConfig
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.feeds.spot import SpotQuote
from troll_poly_bot.live import LiveBook, LiveBot, LiveMarket
from troll_poly_bot.strategy.engine import Evaluation, Reason
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.types import Market

NOW = 1_790_260_000_000.0
SIGMA = 1e-4                       # per second


class _Vol:
    ready = True
    sigma_per_sec = SIGMA

    def update(self, *_):
        pass


class _Composite:
    """Just the surface _on_quote and _maybe_shock_eval use."""

    def __init__(self, price):
        self.price = price
        self.lagging: set[str] = set()

    def update(self, q):
        pass

    def view(self, asset, now):
        return SimpleNamespace(asset=asset, price=self.price, n_sources=3, deviation_bps=0.0)


def _bot(tmp_path, event=True):
    cfg = BotConfig()
    cfg.engine.strategy = "spot_lag"
    cfg.deploy_mode = "paper"
    cfg.spot_lag = SpotLagConfig(enabled=True, event_driven=event)
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"),
                  trade_log=str(tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))
    clock = [NOW]
    bot.clock.now = lambda: clock[0]
    bot._ensure_asset("BTC")
    bot.vol["BTC"] = _Vol()
    bot.engine.features.update_spot("BTC", 100.0, NOW - 1000.0)     # the price 1 s ago
    bot._last_model_update["BTC"] = NOW - 10.0                       # inside the 100 ms throttle
    calls = []
    bot._evaluate_entries = lambda now, assets=None: calls.append((now, assets))
    return bot, clock, calls


def _quote(price):
    return SpotQuote(asset="BTC", exchange="binance", bid=price - 0.01, ask=price + 0.01, price=price, ts_ms=NOW)


def _shock(z):
    return 100.0 * (1.0 + z * SIGMA)


def test_a_shock_is_evaluated_at_once_on_a_fresh_model(tmp_path):
    bot, clock, calls = _bot(tmp_path)
    bot.composite = _Composite(_shock(3.5))
    bot._on_quote(_quote(_shock(3.5)))
    assert calls == [(NOW, {"BTC"})]
    # the throttle was bypassed: the engine reads THIS price, not one up to 100 ms old
    assert bot.spot["BTC"] == pytest.approx(_shock(3.5)) and bot.spot_ts["BTC"] == NOW
    assert bot.engine.features.spot["BTC"].logret(NOW, 1.0) == pytest.approx(math.log(_shock(3.5) / 100.0))
    assert bot.shock_evals == 1


def test_an_ordinary_quote_waits_for_the_tick(tmp_path):
    bot, clock, calls = _bot(tmp_path)
    bot.composite = _Composite(_shock(1.0))
    bot._on_quote(_quote(_shock(1.0)))
    assert calls == [] and "BTC" not in bot.spot                      # still throttled, as before


def test_the_pre_check_fires_a_little_below_k_and_the_engine_decides(tmp_path):
    bot, clock, calls = _bot(tmp_path)
    bot.composite = _Composite(_shock(2.8))                           # >= 0.9 x 3.0
    bot._on_quote(_quote(_shock(2.8)))
    assert len(calls) == 1


def test_event_evaluations_are_rate_limited_per_asset(tmp_path):
    bot, clock, calls = _bot(tmp_path)
    bot.composite = _Composite(_shock(4.0))
    bot._on_quote(_quote(_shock(4.0)))
    clock[0] = NOW + 20.0
    bot._on_quote(_quote(_shock(4.0)))
    assert len(calls) == 1
    clock[0] = NOW + 60.0
    bot._on_quote(_quote(_shock(4.0)))
    assert len(calls) == 2


def test_off_by_default(tmp_path):
    assert SpotLagConfig().event_driven is False
    bot, clock, calls = _bot(tmp_path, event=False)
    bot.composite = _Composite(_shock(5.0))
    bot._on_quote(_quote(_shock(5.0)))
    assert calls == []


def test_the_flag_comes_from_the_environment():
    on = SpotLagConfig.from_env({"TPB_SPOT_LAG_ENABLED": "1", "TPB_SPOT_LAG_EVENT_DRIVEN": "1"})
    assert on.event_driven is True
    assert SpotLagConfig.from_env({"TPB_SPOT_LAG_ENABLED": "1"}).event_driven is False


def test_an_event_evaluation_touches_only_the_shocked_assets_markets(tmp_path):
    bot, clock, _ = _bot(tmp_path)
    del bot._evaluate_entries                                         # the real one
    for asset in ("BTC", "ETH"):
        slug = f"{asset.lower()}-updown-5m-1790260000"
        m = Market(condition_id=slug, asset=asset, yes_token_id=f"{asset}Y", no_token_id=f"{asset}N", strike=100.0,
                   open_ts=NOW - 60_000.0, close_ts=NOW + 240_000.0, tick_size=0.01, min_size=5.0, slug=slug)
        lm = LiveMarket(meta=MarketMeta(market=m, resolution_source="", accepting_orders=True, duration_s=300))
        for tok in (m.yes_token_id, m.no_token_id):
            lm.books[tok] = LiveBook(tok)
        bot.markets[slug] = lm
        bot._ensure_asset(asset)
    seen = []

    def evaluate(meta, **kw):
        seen.append(meta.market.asset)
        return Evaluation(slug=meta.market.slug, asset=meta.market.asset, seconds_left=240.0,
                          reason=Reason.STALE_DATA)

    bot.engine.evaluate = evaluate
    bot._evaluate_entries(NOW, assets={"BTC"})
    assert seen == ["BTC"]
    seen.clear()
    bot._evaluate_entries(NOW)                                        # the tick: every market
    assert sorted(seen) == ["BTC", "ETH"]


def test_spot_history_answers_now_with_the_newest_price():
    from troll_poly_bot.features.engine import SpotHistory
    h = SpotHistory()
    h.update(100.0, 0.0)
    h.update(101.0, 150.0)                     # inside min_gap: not a stored sample...
    assert h._t == [0.0]
    assert h.at(150.0) == pytest.approx(math.log(101.0))          # ...but it IS the price now
    assert h.at(100.0) == pytest.approx(math.log(100.0))          # the past is unchanged
    h.update(102.0, 250.0)                     # a stored sample, newer than the tip
    assert h.at(260.0) == pytest.approx(math.log(102.0))
    h.update(99.0, 240.0)                      # late: never replaces the newer tip
    assert h.at(300.0) == pytest.approx(math.log(102.0))
    assert h.at(10_000.0) is None              # older than max_gap: no price
