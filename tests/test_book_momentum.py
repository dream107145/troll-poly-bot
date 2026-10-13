"""BOOK_MOMENTUM (strategy/book_momentum.py, the user's plan 2026-10-09): the mechanics -- signal, fee-aware edge,
taker sizing, maker bids and their cancels, the shared budget, exits, markouts, paper only. Not a test of whether
the strategy makes money (it has not been backtested)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from troll_poly_bot.__main__ import check_book_momentum
from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.book_momentum import BookMomentumConfig, BookMomentumEngine, taker_fee
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce

T0 = 1_800_000_000_000.0


def _book(token, bid, ask, size=100.0):
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size)])


# the mechanics tests run on the plan's original edge inputs (continuation 1.0 x the rise, 1c adverse selection) so
# that trades happen; the measured defaults (10-09) refuse nearly everything -- see test_the_measured_defaults_*
PLAN = {"continuation": 1.0, "continuation_flat": 0.0, "adverse_selection": 0.01}


class _W:
    def __init__(self, **kw):
        self.sc = BookMomentumConfig(enabled=True, **{**PLAN, **kw})
        rc = RiskConfig(max_position_usdc=4.0, max_total_exposure_usdc=40.0, max_concurrent_positions=2,
                        max_asset_exposure_usdc=40.0, max_epoch_exposure_usdc=40.0, max_daily_loss_usdc=6.0)
        self.sc.apply_budget(rc)
        self.risk = RiskManager(cfg=rc, starting_balance=200.0)
        self.e = BookMomentumEngine(EngineConfig(strategy="book_momentum"), self.sc, self.risk, FeatureEngine())
        m = Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DN", strike=100.0, open_ts=T0,
                   close_ts=T0 + 300_000.0, slug=f"btc-updown-5m-{int(T0 // 1000)}", min_size=5.0)
        self.meta = MarketMeta(market=m, resolution_source="", accepting_orders=True, duration_s=300.0, fee=FeeSchedule())

    def ev(self, t_s, up, dn=None, balance=200.0):
        """Evaluate at t_s seconds after the open with UP (bid, ask); DOWN mirrors it unless given."""
        dn = dn or (round(1 - up[1], 3), round(1 - up[0], 3))
        now = T0 + t_s * 1000.0
        view = CompositeView("BTC", 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
        return self.e.evaluate(self.meta, _book("UP", *up), _book("DN", *dn), view, 100.0, 50.0, now,
                               SimpleNamespace(ready=True, sigma_per_sec=1e-4), SimpleNamespace(ready=True), COLOCATED, balance)

    def ramp(self, t0_s, asks, spread=0.01, step_ms=150):
        self.ev(t0_s - 1.0, (round(asks[0] - spread, 3), asks[0]))       # the book a second before: flat
        ev = None
        for i, a in enumerate(asks):
            ev = self.ev(t0_s + i * step_ms / 1000.0, (round(a - spread, 3), a))
        return ev


def test_flag_and_paper_only():
    assert BookMomentumConfig().enabled is False
    with pytest.raises(ValueError):
        BookMomentumEngine(EngineConfig(strategy="book_momentum"), BookMomentumConfig(), RiskManager(), FeatureEngine())
    with pytest.raises(SystemExit):
        check_book_momentum("book_momentum", "paper", {})
    check_book_momentum("book_momentum", "paper", {"TPB_BOOKMOM_ENABLED": "1"})
    for mode in ("shadow", "micro-live", "live"):
        with pytest.raises(SystemExit):
            check_book_momentum("book_momentum", mode, {"TPB_BOOKMOM_ENABLED": "1"})
    d = BookMomentumConfig()
    assert (d.lookback_ms, d.min_rise, d.max_spread, d.max_signal_age_ms, d.min_time_remaining_s, d.take_profit_net,
            d.max_hold_s, d.max_loss_usdc, d.max_simultaneous_positions, d.max_total_exposure_usdc, d.max_daily_loss_usdc,
            d.assets) == (500.0, 0.015, 0.02, 250.0, 30.0, 0.02, 10.0, 1.0, 2, 40.0, 6.0, "BTC")


def test_a_1_5c_rise_does_not_pay_the_two_taker_fees():
    """The plan's own warning: at ~0.5 each taker leg costs ~1.75c, so a 1.5c rise is refused on edge."""
    w = _W()
    ev = w.ramp(100.0, [0.50, 0.505, 0.51, 0.515])
    assert ev.reason is Reason.EDGE_TOO_SMALL, (ev.reason, ev.detail)
    assert taker_fee(0.5) == pytest.approx(0.0175)


def test_a_strong_confirmed_rise_becomes_a_small_fak_buy():
    w = _W()
    ev = w.ramp(100.0, [0.50, 0.53, 0.56, 0.58])                 # +8c in 450 ms, every update up
    assert ev.reason is None, (ev.reason, ev.detail)
    f = ev.features["book_momentum"]
    exit_px = 0.57 + 1.0 * f["rise"]
    assert f["edge"] == pytest.approx(exit_px - 0.58 - taker_fee(0.58) - taker_fee(exit_px) - 0.005 - 0.005, abs=1e-4)
    assert ev.side == "UP" and ev.limit_price == pytest.approx(0.59) and 5.0 <= ev.size and ev.size * 0.59 <= 4.0
    o = w.e.to_order(ev, T0 + 100_500.0)
    assert o.tif is TimeInForce.IOC and "taker" in o.tag


def test_the_signal_needs_consecutive_rises_freshness_and_time():
    w = _W()
    assert w.ramp(100.0, [0.50, 0.56, 0.54, 0.58]).reason is Reason.WEAK_MOMENTUM          # a dip in between
    w2 = _W()
    assert w2.ramp(275.0, [0.50, 0.53, 0.56, 0.58]).reason is Reason.OUTSIDE_TIME_WINDOW   # inside the last 30 s
    w3 = _W()
    w3.ramp(100.0, [0.50, 0.53, 0.56, 0.58])
    ev = w3.ev(100.45 + 0.3, (0.57, 0.58))                                                  # no new update for 300 ms
    assert ev.reason is Reason.STALE_DATA
    w4 = _W()
    assert w4.ramp(100.0, [0.50, 0.53, 0.56, 0.58], spread=0.03).reason is Reason.LOW_LIQUIDITY


def test_maker_bid_at_the_bid_and_its_cancels():
    w = _W(continuation=1.0, adverse_selection=0.0)
    w.ramp(100.0, [0.50, 0.53, 0.56, 0.58], spread=0.02)
    now = T0 + 100_450.0
    books = {w.meta.market.slug: (_book("UP", 0.56, 0.58), _book("DN", 0.42, 0.44))}
    reqs = w.e.pair_requests(now, {w.meta.market.slug: w.meta.market}, books, balance=200.0)
    assert len(reqs) == 1
    slug, side, o = reqs[0]
    assert side == "UP" and o.tif is TimeInForce.GTC and o.post_only and o.price == pytest.approx(0.56) and o.size * 0.56 <= 4.0
    w.e.on_pair_submit(o.order_id, slug, side, o.price, o.size)
    assert w.e.pair_requests(now + 100, {slug: w.meta.market}, books, balance=200.0) == []   # one bid per token
    assert w.e.pair_cancels(now + 1_000) == []
    assert w.e.pair_cancels(now + 5_100) == [o.order_id]                                     # rested 5 s
    # the bid moving away cancels too
    w2 = _W(continuation=1.0, adverse_selection=0.0)
    w2.ramp(100.0, [0.50, 0.53, 0.56, 0.58], spread=0.02)
    r = w2.e.pair_requests(now, {slug: w2.meta.market}, books, balance=200.0)[0][2]
    w2.ev(100.6, (0.58, 0.60))
    assert w2.e.pair_cancels(T0 + 100_600.0) == [r.order_id]


def test_the_budget_is_shared_by_both_modules():
    w = _W()
    w.e.positions["X1"] = {"slug": "a", "side": "UP", "module": "taker", "shares": 5, "cost": 3.0, "fee": 0.1, "t": 0}
    w.e.positions["X2"] = {"slug": "b", "side": "UP", "module": "maker", "shares": 5, "cost": 3.0, "fee": 0.0, "t": 0}
    ev = w.ramp(100.0, [0.50, 0.53, 0.56, 0.58])
    assert ev.reason is Reason.EXPOSURE_CAP and "positions open" in ev.detail                 # 2 positions
    w2 = _W()
    w2.e.resting["o1"] = {"slug": "x", "side": "UP", "token": "Z", "price": 0.5, "size": 78.0, "t": 0, "filled": 0, "state": "resting"}
    ev = w2.ramp(100.0, [0.50, 0.53, 0.56, 0.58])
    assert ev.reason is Reason.EXPOSURE_CAP and "resting bids" in ev.detail                   # 39 USDC already resting


def test_exits():
    w = _W()
    tok = "UP"
    w.e._add_position(tok, w.meta.market.slug, "UP", "taker", 5.0, 0.50, 5 * taker_fee(0.50), T0)   # noqa: SLF001
    kw = dict(asset="BTC", side="UP", token_id=tok, entry=0.50, size=5.0)
    assert w.e.exit_decision(bid=0.51, now_ms=T0 + 1000, **kw) is None
    assert w.e.exit_decision(bid=0.56, now_ms=T0 + 1000, **kw) == "TAKE_PROFIT"   # 6c - 1.75c - 1.73c = 2.5c net
    assert w.e.exit_decision(bid=0.31, now_ms=T0 + 1000, **kw) == "STOP_LOSS"     # 5 x (-19c - fees) < -1 USDC
    assert w.e.exit_decision(bid=0.51, now_ms=T0 + 10_100, **kw) == "TIME_STOP"
    w.e._record(tok, T0 + 1000, 0.52, 0.53)                                        # noqa: SLF001
    w.e._record(tok, T0 + 2400, 0.50, 0.51)                                        # noqa: SLF001
    assert w.e.exit_decision(bid=0.50, now_ms=T0 + 2400, **kw) == "MOMENTUM_REVERSAL"
    w.e.on_exit(w.meta.market.slug, tok, T0 + 2500)
    assert tok not in w.e.positions and w.e.cooldown[tok] > T0 + 2500


def test_markouts_at_100ms_500ms_1s_3s():
    w = _W()
    w.e._add_position("UP", w.meta.market.slug, "UP", "maker", 5.0, 0.50, 0.0, T0 + 100_000)      # noqa: SLF001
    for dt, (b, a) in ((50, (0.50, 0.51)), (150, (0.51, 0.52)), (600, (0.52, 0.53)), (1100, (0.53, 0.54)), (3200, (0.50, 0.51))):
        w.ev(100.0 + dt / 1000.0, (b, a))
    st = w.e.snapshot()["book_momentum"]["markout_c"]["maker"]
    assert [st[k][1] for k in ("0.1s", "0.5s", "1s", "3s")] == [1, 1, 1, 1]
    assert st["0.1s"][0] == pytest.approx(1.5) and st["3s"][0] == pytest.approx(0.5)


def test_the_bot_builds_it_with_the_plan_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("TPB_BOOKMOM_ENABLED", "1")
    from troll_poly_bot.live import LiveBot
    cfg = BotConfig()
    cfg.engine.strategy = "book_momentum"
    cfg.deploy_mode, cfg.execution_scenario = "paper", "calibrated"
    cfg.book_momentum = BookMomentumConfig.from_env()
    bot = LiveBot(assets=("BTC",), balance=200.0, cfg=cfg, state_path=str(tmp_path / "live_state.bm.json"),
                  trade_log=str(tmp_path / "live_trades.bm.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    assert type(bot.engine) is BookMomentumEngine and bot._reentry                     # noqa: SLF001
    r = bot.risk.cfg
    assert r.max_concurrent_positions == 2 and r.max_total_exposure_usdc == pytest.approx(40.0)
    assert r.max_daily_loss_usdc <= 6.0 and r.max_position_usdc <= 4.0
    assert bot.engine.snapshot()["strategy"] == "book_momentum"


def test_the_daily_loss_stop_halts_new_entries_and_bids():
    w = _W()
    w.risk.realised_today = -6.0
    ev = w.ramp(100.0, [0.50, 0.53, 0.56, 0.58])
    assert ev.reason in (Reason.EXPOSURE_CAP, Reason.RISK_LIMIT) and "daily loss" in ev.detail


def test_a_refused_or_part_filled_maker_bid_is_dropped_never_re_placed_or_taken(monkeypatch, tmp_path):
    # paper 2026-10-09 01:37: a post-only bid refused for crossing came back as a TAKER buy (live.py's triangle
    # hedge retry), its unfilled rest then rested again -- a maker bid this plan meant to simply cancel
    monkeypatch.setenv("TPB_BOOKMOM_ENABLED", "1")
    from troll_poly_bot.execution.paper import OrderResult, RejectReason
    from troll_poly_bot.live import LiveBot, LiveMarket
    from troll_poly_bot.types import Order, OrderState, Side
    cfg = BotConfig()
    cfg.engine.strategy = "book_momentum"
    cfg.deploy_mode, cfg.execution_scenario = "paper", "calibrated"
    cfg.book_momentum = BookMomentumConfig.from_env()
    bot = LiveBot(assets=("BTC",), balance=200.0, cfg=cfg, state_path=str(tmp_path / "live_state.bm.json"),
                  trade_log=str(tmp_path / "live_trades.bm.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    meta = _W().meta
    lm = LiveMarket(meta=meta)
    bot.markets[meta.market.slug] = lm
    for why in (RejectReason.POST_ONLY_CROSS, RejectReason.QUOTE_PULLED):
        o = Order(token_id="UP", side=Side.BUY, price=0.50, size=6.0, tif=TimeInForce.GTC, expires_ts=meta.market.close_ts,
                  post_only=True)
        bot.engine.resting[o.order_id] = {"slug": meta.market.slug, "side": "UP", "token": "UP", "price": 0.50, "size": 6.0,
                                          "t": T0 + 10_000.0, "filled": 0.0, "state": "sent"}
        bot._place_pair(lm, "UP", o, "engine", T0 + 10_000.0)                  # noqa: SLF001
        o.state = OrderState.REJECTED
        bot._on_result(OrderResult(o, [], why, T0 + 11_000.0), T0 + 11_000.0)   # noqa: SLF001
        assert bot._pairs_to_place == [] and o.order_id not in bot._pairs_open   # noqa: SLF001
        assert o.order_id not in bot.engine.resting


def test_a_bid_filling_during_its_cancel_is_booked_as_a_maker_fill():
    w = _W()
    w.e.meta_by_slug[w.meta.market.slug] = w.meta
    w.e.on_fill_detail(w.meta.market.slug, "DOWN", 0.47, 6.38, 0.0, T0 + 10_000)       # fee-free: the cancelled bid
    w.e.on_fill_detail(w.meta.market.slug, "UP", 0.13, 4.8, 4.8 * taker_fee(0.13), T0 + 11_000)
    assert w.e.positions["DN"]["module"] == "maker" and w.e.positions["UP"]["module"] == "taker"
    assert w.e.bm_stats["maker_fills"] == 1 and w.e.bm_stats["taker_fills"] == 1


def test_the_measured_defaults_refuse_a_strong_rise_and_its_maker_bid():
    """10-09, 3,899 signal episodes of this bot: +1.2c of mid in 10 s whatever the rise, maker fills 5.7c adverse.
    An +8c jump no longer promises +8c more: the taker round trip and the bid are both refused on edge."""
    d = BookMomentumConfig()
    assert (d.continuation, d.continuation_flat, d.adverse_selection, d.min_ask, d.max_ask) == (0.0, 0.012, 0.057, 0.20, 0.80)
    w = _W(continuation=d.continuation, continuation_flat=d.continuation_flat, adverse_selection=d.adverse_selection)
    ev = w.ramp(100.0, [0.50, 0.53, 0.56, 0.58])
    assert ev.reason is Reason.EDGE_TOO_SMALL, (ev.reason, ev.detail)
    exit_px = 0.57 + 0.012
    assert ev.features["book_momentum"]["edge"] == pytest.approx(
        exit_px - 0.58 - taker_fee(0.58) - taker_fee(exit_px) - 0.005 - 0.005, abs=1e-4)
    books = {w.meta.market.slug: (_book("UP", 0.57, 0.58), _book("DN", 0.42, 0.43))}
    assert w.e.pair_requests(T0 + 100_450.0, {w.meta.market.slug: w.meta.market}, books, balance=200.0) == []
    assert w.e.bm_stats["maker_edge_refused"] >= 1


def test_a_new_entry_needs_daily_loss_headroom_for_every_open_position():
    # paper 10-09 01:39: entries kept coming at -5 realised with positions open; the day ended at -6.69 (stop 6)
    w = _W()
    w.risk.realised_today = -4.5
    w.e.positions["X1"] = {"slug": "a", "side": "UP", "module": "taker", "shares": 5, "cost": 3.0, "fee": 0.1, "t": 0}
    ev = w.ramp(100.0, [0.50, 0.53, 0.56, 0.58])
    assert ev.reason is Reason.EXPOSURE_CAP and "headroom" in ev.detail           # -4.5 - 2 x 1 <= -6
    w2 = _W()
    w2.risk.realised_today = -3.5
    assert w2.ramp(100.0, [0.50, 0.53, 0.56, 0.58]).reason is None              # -3.5 - 1 > -6: room left
