"""TRIANGLE (strategy/triangle.py): one share on every FILLED sudden-move triangle (a spot-lag
signal), the triangle's side, held to settlement -- the flag and the absence of any real-money
switch, its own config prefix and budget, the trigger being exactly the filled mark, spot-lag's
post-signal gates not applied, stacking in one market, no exits, and the live bot's wiring
(including the pre-send risk check that refused a second buy in a market it already held)."""
from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from test_spot_lag import NOW, SLUG, T0, _book, _feed, _meta, _Twap, _Vol   # the spot-lag engine harness

from troll_poly_bot.__main__ import check_triangle
from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.live import LiveBot, LiveMarket
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.strategy.triangle import TriangleConfig, TriangleEngine
from troll_poly_bot.types import TimeInForce


def _cfg(**kw) -> TriangleConfig:
    base = dict(enabled=True)
    base.update(kw)
    return TriangleConfig(**base)


def _engine(scfg: TriangleConfig | None = None) -> TriangleEngine:
    scfg = scfg or _cfg()
    rc = RiskConfig()
    scfg.apply_budget(rc)
    return TriangleEngine(EngineConfig(strategy="triangle"), scfg, RiskManager(cfg=rc, starting_balance=500.0),
                          FeatureEngine())


def _eval(e, twap, now, up, down, balance=500.0):
    view = CompositeView("BTC", 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
    return e.evaluate(_meta("BTC"), up, down, view, 200.0, 100.0, now, _Vol(), twap, COLOCATED, balance)


def _shock(jump_bps=6.0, up_now=(0.50, 0.52), scfg=None, e=None):
    """The spot-lag harness: flat, one evaluation 1 s before, then a ``jump_bps`` move evaluated at NOW."""
    e, twap = e or _engine(scfg), _Twap()
    _feed(e, twap, jump_bps, until=NOW - 1000.0)
    _eval(e, twap, NOW - 1000.0, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    _feed(e, twap, jump_bps, until=NOW)
    up = _book("UP", *up_now)
    down = _book("DOWN", round(1 - up_now[1], 2), round(1 - up_now[0], 2))
    return e, _eval(e, twap, NOW, up, down)


# ─────────────────────────────── flags ───────────────────────────────


def test_off_by_default_and_the_engine_refuses_to_exist_without_the_flag():
    assert TriangleConfig().enabled is False
    with pytest.raises(ValueError, match="TPB_TRIANGLE_ENABLED"):
        TriangleEngine(EngineConfig(strategy="triangle"), TriangleConfig(), RiskManager(cfg=RiskConfig()))


def test_paper_needs_the_flag_and_real_money_is_refused_whatever_is_set():
    with pytest.raises(SystemExit, match="TPB_TRIANGLE_ENABLED"):
        check_triangle("triangle", "paper", env={})
    check_triangle("triangle", "paper", env={"TPB_TRIANGLE_ENABLED": "1"})
    for mode in ("micro-live", "live", "shadow"):
        with pytest.raises(SystemExit, match="paper only"):
            check_triangle("triangle", mode, env={"TPB_TRIANGLE_ENABLED": "1", "TPB_SPOT_LAG_ALLOW_REAL": "1"})
    check_triangle("spot_lag", "live", env={})


def test_its_own_env_prefix_and_the_mirrors_signal_defaults():
    env = {"TPB_TRIANGLE_ENABLED": "1", "TPB_TRIANGLE_SHARES": "2", "TPB_TRIANGLE_TREND_WINDOW_S": "60",
           "TPB_SPOT_LAG_K_SIGMA": "3.5"}
    c = TriangleConfig.from_env(env)
    assert c.enabled and c.shares == 2.0 and c.trend_window_s == 60.0 and c.k_sigma == 3.0
    d = TriangleConfig()
    # the paper mirror's signal: 3 sigma, 30 s trend, no fair-value trigger, spot <= 500 ms, HYPE out
    assert (d.k_sigma, d.trend_window_s, d.trend_min_bps, d.fv_trigger_cents, d.max_spot_age_ms) == (3.0, 30.0, 0.0, 0.0, 500.0)
    assert d.exclude_assets == "HYPE" and d.event_driven and d.hold_to_settlement and d.shares == 1.0
    assert SpotLagConfig.from_env(env).k_sigma == 3.5 and SpotLagConfig.from_env(env).enabled is False


def test_its_budget_is_set_wide_not_only_tightened():
    rc = RiskConfig()
    assert rc.max_concurrent_positions == 10
    _cfg().apply_budget(rc)
    assert rc.max_concurrent_positions == 50 and rc.max_total_exposure_usdc == 400.0
    assert rc.max_daily_loss_usdc == 250.0 and rc.max_position_usdc == 1.0 and rc.max_asset_exposure_usdc == 100.0


# ─────────────────────────────── the trigger ───────────────────────────────


def test_a_filled_up_triangle_buys_one_up_share_fok_at_ask_plus_two_cents():
    e, ev = _shock(6.0)
    assert ev.tradeable, (ev.reason, ev.detail)
    assert (ev.side, ev.token_id, ev.size, ev.limit_price, ev.expected_price) == ("UP", "UP", 1.0, 0.54, 0.52)
    (mk,) = e.shock_marks_for(SLUG)
    assert mk["ok"] is True and mk["dir"] == 1
    tri = ev.features["triangle"]
    assert tri["signal"] is True and tri["direction"] == "UP" and tri["t"] == mk["t"]
    o = e.to_order(ev, NOW)
    assert o.tif is TimeInForce.FOK and o.size == 1.0 and o.price == 0.54 and "triangle" in o.tag
    assert e.triangles == 1 and e.orders_decided == 1


def test_a_filled_down_triangle_buys_one_down_share():
    e, ev = _shock(-6.0, up_now=(0.48, 0.50))
    assert ev.tradeable, (ev.reason, ev.detail)
    assert (ev.side, ev.token_id, ev.size) == ("DOWN", "DOWN", 1.0)
    assert ev.expected_price == 0.52 and ev.limit_price == 0.54
    assert e.shock_marks_for(SLUG)[-1]["dir"] == -1


def test_spot_lags_post_signal_gates_are_not_applied():
    # the Up token already repriced past the expected value and above spot-lag's 0.95 band: spot-lag
    # refuses, the triangle still buys -- and records what spot-lag would have done
    e, ev = _shock(6.0, up_now=(0.94, 0.96))
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.limit_price == 0.96 and ev.expected_price == 0.96             # capped at the price band's top
    assert ev.features["triangle"]["spot_lag_verdict"] != "ORDER"


def test_no_shock_and_a_hollow_triangle_are_no_order():
    e, ev = _shock(2.0)                                                   # under 3 sigma: no mark
    assert ev.reason is Reason.WEAK_MOMENTUM and e.shock_marks_for(SLUG) == [] and e.triangles == 0
    e, ev = _shock(6.0, scfg=_cfg(trend_window_s=30.0, trend_min_bps=1.0))  # detected, skipped by the trend
    (mk,) = e.shock_marks_for(SLUG)
    assert mk["ok"] is False and not ev.tradeable and e.triangles == 0 and "triangle" not in ev.features


def test_a_signal_in_a_second_that_first_drew_a_hollow_mark_fills_it():
    e = _engine()
    e.shock_marks[SLUG] = deque([{"t": NOW - 300.0, "dir": 1, "ok": False, "why": "z3 does not agree"}])
    mk = e._filled_mark(SLUG, 1, NOW)                                   # noqa: SLF001
    assert mk is not None and mk["ok"] is True and mk["why"] == ""
    assert e._filled_mark(SLUG, -1, NOW) is None                        # noqa: SLF001 -- the other direction


def test_every_triangle_stacks_in_the_same_market_up_to_its_market_budget():
    e = _engine()
    e.risk.on_fill(SLUG, "BTC", int(T0 // 1000), "DOWN", 0.48, 1.0)       # already holding this market
    e, ev = _shock(6.0, e=e)
    assert ev.tradeable, (ev.reason, ev.detail)
    e2 = _engine(_cfg(max_market_usdc=10.0))
    e2.risk.on_fill(SLUG, "BTC", int(T0 // 1000), "UP", 9.8, 18.0)
    e2, ev2 = _shock(6.0, e=e2)
    assert ev2.reason is Reason.EXPOSURE_CAP and e2.refused == {"EXPOSURE_CAP": 1}


def test_no_exit_ever_and_revalidation_only_needs_an_ask():
    e, ev = _shock(6.0)
    assert e.exit_decision(asset="BTC", side="UP", token_id="UP", entry=0.52, bid=0.99, now_ms=NOW) is None
    assert e.exit_decision(asset="BTC", side="UP", token_id="UP", entry=0.52, bid=0.01, now_ms=NOW) is None
    assert e.revalidate(ev, _book("UP", 0.80, 0.90)) is None             # moved past the limit: the FOK decides
    assert e.revalidate(ev, SimpleNamespace(best_ask=None)) is Reason.NO_OFFER
    snap = e.snapshot()
    assert snap["strategy"] == "triangle" and snap["triangle"]["orders_decided"] == 1


# ─────────────────────────────── the live bot ───────────────────────────────


def _bot(tmp_path, strategy="triangle", **tri):
    cfg = BotConfig()
    cfg.engine.strategy = strategy
    cfg.execution_scenario = "venue_lag"
    cfg.deploy_mode = "paper"
    cfg.triangle = _cfg(**tri)
    cfg.spot_lag = SpotLagConfig(enabled=True)
    return LiveBot(assets=("BTC",), balance=500.0, cfg=cfg, state_path=str(tmp_path / "live_state.tri.json"),
                   trade_log=str(tmp_path / "live_trades.tri.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                   kill_file=str(tmp_path / "KILL"))


def test_the_live_bot_runs_it_with_its_own_budget_and_hooks(tmp_path):
    bot = _bot(tmp_path)
    assert type(bot.engine) is TriangleEngine and bot.engine.scfg is bot.cfg.triangle
    assert bot.risk.cfg.max_concurrent_positions == 50 and bot.risk.cfg.max_position_usdc == 1.0
    assert bot._control_targets()["strategy"] is None                   # noqa: SLF001 -- the dashboard cannot steer it
    lm = LiveMarket(meta=_meta("BTC"))
    lm.last_entry_ms = NOW - 1.0
    assert bot._entry_throttled(lm, NOW) is False                       # noqa: SLF001


def test_the_pre_send_check_lets_a_stacking_engine_add_to_its_market(tmp_path):
    bot = _bot(tmp_path)
    e, ev = _shock(6.0, e=bot.engine)
    assert ev.tradeable, (ev.reason, ev.detail)
    lm = LiveMarket(meta=_meta("BTC"))
    bot.risk.on_fill(SLUG, "BTC", lm.epoch, "UP", 0.52, 1.0)              # a first triangle already filled
    failed, checklist = bot._confirm_entry(ev, lm, NOW, 100.0, 50.0)     # noqa: SLF001
    assert checklist["RISK_LIMITS"] == "ok" and checklist["NO_POSITION"] is not None
    assert failed is None, (failed, checklist)


def test_five_shares_fak_at_the_venue_maximum():
    # the app's settings since 2026-10-01 02:04 UTC: 5 shares (the venue minimum), FAK, limit 0.99
    e, ev = _shock(6.0, scfg=_cfg(shares=5.0, order_type="FAK", limit_offset=0.99, max_order_usdc=5.0))
    assert ev.tradeable, (ev.reason, ev.detail)
    assert (ev.size, ev.limit_price, ev.expected_price) == (5.0, 0.96, 0.52)     # 0.99 capped at the 0.96 band
    o = e.to_order(ev, NOW)
    assert o.tif is TimeInForce.IOC and o.size == 5.0 and o.price == 0.96


# ─────────────────────────────── the pair order ───────────────────────────────


def test_the_pair_order_is_the_other_side_at_pair_cost_minus_the_fill():
    e = _engine(_cfg(pair_cost=0.95))
    m = _meta("BTC").market
    side, o = e.pair_order(m, "UP", 5.0, 0.75, NOW)
    assert side == "DOWN" and o.token_id == "DOWN" and o.price == 0.20 and o.size == 5.0
    assert o.tif is TimeInForce.GTC and o.expires_ts == m.close_ts and o.side.value == "BUY"
    side, o = e.pair_order(m, "DOWN", 5.0, 0.333, NOW)                    # 0.617 rounds DOWN to the tick
    assert side == "UP" and o.token_id == "UP" and o.price == 0.61
    assert e.pair_order(m, "UP", 5.0, 0.98, NOW) is None                  # 0.95 - 0.98 < one tick
    assert e.pair_order(m, "UP", 3.0, 0.50, NOW) is None                  # a partial fill under the 5-share minimum
    assert e.pairs_placed == 2 and e.pairs_skipped == {"price": 1, "size": 1}
    assert _engine(_cfg()).pair_order(m, "UP", 5.0, 0.50, NOW) is None   # off by default


def test_a_fill_places_a_resting_pair_that_does_not_hold_the_market(tmp_path):
    from troll_poly_bot.execution.paper import OrderResult, RejectReason
    from troll_poly_bot.types import Fill, Order, OrderState, Side
    bot = _bot(tmp_path)
    bot.cfg.triangle.pair_cost = 0.95
    lm = LiveMarket(meta=_meta("BTC"))
    bot.markets[SLUG] = lm

    def result(token, side, px, tif=TimeInForce.IOC, pair=False):
        o = Order(token_id=token, side=Side.BUY, price=0.99 if not pair else px, size=5.0, tif=tif)
        o.filled_size, o.state = 5.0, OrderState.FILLED
        bot.fair_by_order[o.order_id] = {"slug": SLUG, "asset": "BTC", "epoch": lm.epoch, "token_id": token,
                                         "side": side, **({"pair": True} if pair else {})}
        return OrderResult(o, [Fill(o.order_id, token, Side.BUY, px, 5.0, 0.0, NOW, NOW, NOW)], RejectReason.NONE, NOW)

    lm.inflight = 1
    bot.risk.on_submit(SLUG, usdc=4.95, asset="BTC", epoch=lm.epoch)
    bot._on_result(result("UP", "UP", 0.75), NOW)                          # noqa: SLF001
    pairs = [(k, v) for k, v in bot.fair_by_order.items() if v.get("pair")]
    assert len(pairs) == 1 and pairs[0][1]["side"] == "DOWN" and pairs[0][1]["expected"] == 0.20
    sent = [s.payload for s in bot.exchange._inflight]                     # noqa: SLF001
    assert len(sent) == 1 and sent[0].tif is TimeInForce.GTC and sent[0].token_id == "DOWN" and sent[0].price == 0.20
    assert lm.inflight == 0 and SLUG not in bot.risk.pending              # the pair holds nothing
    # a second triangle is in flight when the pair fills: its slot is untouched, no pair of the pair
    lm.inflight = 1
    bot.risk.on_submit(SLUG, usdc=4.95, asset="BTC", epoch=lm.epoch)
    bot._on_result(result("DOWN", "DOWN", 0.20, tif=TimeInForce.GTC, pair=True), NOW + 1000.0)   # noqa: SLF001
    assert lm.inflight == 1 and SLUG in bot.risk.pending
    assert bot.risk.open[SLUG].shares == 10.0 and bot.risk.open[SLUG].usdc == pytest.approx(4.75)
    assert len([v for v in bot.fair_by_order.values() if v.get("pair")]) == 2   # the filled pair + none new


def test_no_new_buy_while_a_pair_order_is_working():
    e = _engine(_cfg(pair_cost=0.95))
    e.on_pair_submit("ord-p1", "btc-updown-5m-0", "DOWN", 0.20, 5.0)
    e, ev = _shock(6.0, e=e)
    assert not ev.tradeable and ev.reason is Reason.ALREADY_POSITIONED and "waiting for the pair" in ev.detail
    (mk,) = e.shock_marks_for(SLUG)
    assert mk["ok"] is False and "waiting for the pair" in mk["why"]          # drawn hollow, with the reason
    assert ev.features["triangle"]["waiting_for_pair"] is True and e.orders_decided == 0
    e.on_pair_done("ord-p1", filled=False)                                     # expired at its close: resolved
    assert e.pairs_working == {} and e.pairs_expired == 1
    e2 = _engine(_cfg(pair_cost=0.95))
    e2.on_pair_submit("ord-p2", SLUG, "DOWN", 0.20, 5.0)
    e2.on_pair_done("ord-p2", filled=True)
    e2, ev2 = _shock(6.0, e=e2)
    assert ev2.tradeable and e2.pairs_filled == 1
    e3 = _engine(_cfg(pair_cost=0.95, wait_for_pair=False))                    # the switch off: buys anyway
    e3.on_pair_submit("ord-p3", SLUG, "DOWN", 0.20, 5.0)
    e3, ev3 = _shock(6.0, e=e3)
    assert ev3.tradeable


def test_the_live_bot_tells_the_engine_when_a_pair_starts_and_resolves(tmp_path):
    from troll_poly_bot.execution.paper import OrderResult, RejectReason
    from troll_poly_bot.types import Fill, Order, OrderState, Side
    bot = _bot(tmp_path)
    bot.cfg.triangle.pair_cost = 0.95
    lm = LiveMarket(meta=_meta("BTC"))
    bot.markets[SLUG] = lm
    o = Order(token_id="UP", side=Side.BUY, price=0.99, size=5.0, tif=TimeInForce.IOC)
    o.filled_size, o.state = 5.0, OrderState.FILLED
    bot.fair_by_order[o.order_id] = {"slug": SLUG, "asset": "BTC", "epoch": lm.epoch, "token_id": "UP", "side": "UP"}
    lm.inflight = 1
    bot._on_result(OrderResult(o, [Fill(o.order_id, "UP", Side.BUY, 0.70, 5.0, 0.0, NOW, NOW, NOW)],   # noqa: SLF001
                               RejectReason.NONE, NOW), NOW)
    (pid,) = list(bot.engine.pairs_working)
    assert bot.engine.pairs_working[pid] == (SLUG, "DOWN", 0.25, 5.0)
    p = next(s.payload for s in bot.exchange._inflight if s.payload.order_id == pid)            # noqa: SLF001
    p.state = OrderState.CANCELLED
    bot._on_result(OrderResult(p, [], RejectReason.MARKET_CLOSED, NOW + 300_000.0), NOW + 300_000.0)   # noqa: SLF001
    assert bot.engine.pairs_working == {} and bot.engine.pairs_expired == 1 and lm.inflight == 0


def test_a_two_sided_market_survives_a_restart(tmp_path):
    import time as _time
    from troll_poly_bot.feeds.markets import MarketMeta
    from troll_poly_bot.signals.costs import FeeSchedule
    from troll_poly_bot.types import Market
    open_ts = (_time.time() // 300) * 300_000.0
    slug = f"btc-updown-5m-{int(open_ts // 1000)}"
    meta = MarketMeta(market=Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN", strike=100.0,
                                    open_ts=open_ts, close_ts=open_ts + 300_000.0, slug=slug, min_size=5.0),
                      resolution_source="", accepting_orders=True, duration_s=300.0, fee=FeeSchedule())
    bot = _bot(tmp_path)
    lm = LiveMarket(meta=meta)
    bot.markets[slug] = lm
    for tok, side, sh, cost in (("DOWN", "DOWN", 20.0, 16.30), ("UP", "UP", 10.0, 2.80)):
        pos = bot.exchange.positions[tok]
        pos.token_id, pos.shares, pos.cost_basis = tok, sh, cost
        bot.risk.on_fill(slug, "BTC", lm.epoch, side, cost, sh)
    bot._persist_positions()                                              # noqa: SLF001
    again = _bot(tmp_path)                                                # restores at construction
    assert again.exchange.positions["DOWN"].shares == 20.0 and again.exchange.positions["UP"].shares == 10.0
    assert again.risk.open[slug].shares == 30.0 and again.risk.open[slug].usdc == pytest.approx(19.10)
    assert again.exchange.balance == pytest.approx(500.0 - 19.10)


# ─────────────── 2026-10-01 12:50: a pair after every buy; filled triangle = bought ───────────────


def _now_meta():
    """A market in the current real 5-minute window (restores compare with the wall clock)."""
    import time as _time
    from troll_poly_bot.feeds.markets import MarketMeta
    from troll_poly_bot.signals.costs import FeeSchedule
    from troll_poly_bot.types import Market
    open_ts = (_time.time() // 300) * 300_000.0
    slug = f"btc-updown-5m-{int(open_ts // 1000)}"
    return MarketMeta(market=Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN", strike=100.0,
                                    open_ts=open_ts, close_ts=open_ts + 300_000.0, slug=slug, min_size=5.0),
                      resolution_source="", accepting_orders=True, duration_s=300.0, fee=FeeSchedule())


def test_no_buy_where_no_pair_can_follow_and_the_limit_stops_there():
    e, ev = _shock(6.0, up_now=(0.94, 0.96), scfg=_cfg(pair_cost=0.95, shares=5.0, limit_offset=0.99, max_order_usdc=5.0))
    assert ev.reason is Reason.PRICE_BAND and "no room for a pair" in ev.detail
    (mk,) = e.shock_marks_for(SLUG)
    assert mk["ok"] is False and "no room for a pair" in mk["why"]           # drawn hollow, with the reason
    e, ev = _shock(6.0, up_now=(0.88, 0.90), scfg=_cfg(pair_cost=0.95, shares=5.0, limit_offset=0.99, max_order_usdc=5.0))
    assert ev.tradeable and ev.limit_price == 0.94                           # any fill leaves a >= 0.01 pair
    side, o = e.pair_order(_meta("BTC").market, "UP", 5.0, 0.94, NOW)
    assert side == "DOWN" and o.price == 0.01
    e, ev = _shock(6.0, up_now=(0.94, 0.96), scfg=_cfg(pair_cost=0.95, pair_room_required=False))
    assert ev.tradeable                                                      # the switch off: as before


def test_a_triangle_with_no_buy_is_drawn_hollow_with_the_reason():
    from troll_poly_bot.types import OrderBook
    e, twap = _engine(), _Twap()
    _feed(e, twap, 6.0, until=NOW - 1000.0)
    _eval(e, twap, NOW - 1000.0, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    _feed(e, twap, 6.0, until=NOW)
    ev = _eval(e, twap, NOW, OrderBook("UP", bids=_book("UP", 0.99, 0.99).bids, asks=[]), _book("DOWN", 0.01, 0.02))
    assert ev.reason is Reason.NO_OFFER
    (mk,) = e.shock_marks_for(SLUG)
    assert mk["ok"] is False and "no UP ask" in mk["why"] and "0.99" in mk["why"]
    # sent, then not filled -> hollow; filled -> stays filled
    e, ev = _shock(6.0)
    o = e.to_order(ev, NOW)
    e.entry_result(o.order_id, False, "fok_unfillable")
    assert e.shock_marks_for(SLUG)[-1]["ok"] is False and "fok_unfillable" in e.shock_marks_for(SLUG)[-1]["why"]
    e, ev = _shock(6.0)
    e.entry_result(e.to_order(ev, NOW).order_id, True)
    assert e.shock_marks_for(SLUG)[-1]["ok"] is True
    # refused by the live bot before sending (guard, pre-send check)
    e, ev = _shock(6.0)
    e.entry_refused(ev, "entries paused by the guard (COOLDOWN)")
    assert e.shock_marks_for(SLUG)[-1]["ok"] is False and "guard" in e.shock_marks_for(SLUG)[-1]["why"]


def test_a_working_pair_survives_a_restart_and_holds_buys_until_re_placed(tmp_path):
    meta = _now_meta()
    m = meta.market
    bot = _bot(tmp_path)
    bot.cfg.triangle.pair_cost = 0.95
    lm = LiveMarket(meta=meta)
    bot.markets[m.slug] = lm
    pos = bot.exchange.positions["UP"]
    pos.token_id, pos.shares, pos.cost_basis = "UP", 5.0, 3.50
    bot.risk.on_fill(m.slug, "BTC", lm.epoch, "UP", 3.50, 5.0)
    side, order = bot.engine.pair_order(m, "UP", 5.0, 0.70, m.open_ts)
    assert bot._place_pair(lm, side, order, "ord-entry", m.open_ts + 10_000.0)   # noqa: SLF001 -- persists
    again = _bot(tmp_path)                                                    # restores at construction
    assert len(again._pairs_to_place) == 1                                     # noqa: SLF001
    assert len(again.engine.pairs_working) == 1                                # holds new buys already
    lm2 = LiveMarket(meta=meta)
    again.markets[m.slug] = lm2
    import time as _time
    now = _time.time() * 1000.0
    again._place_restored_pairs(now)                                           # noqa: SLF001 -- no book yet
    assert len(again._pairs_to_place) == 1                                     # noqa: SLF001
    from troll_poly_bot.live import LiveBook
    lm2.books["DOWN"] = LiveBook("DOWN")
    lm2.books["DOWN"].apply_snapshot({"bids": [{"price": "0.28", "size": "50"}], "asks": [{"price": "0.30", "size": "50"}],
                                      "timestamp": str(int(now))})
    again._place_restored_pairs(now)                                           # noqa: SLF001
    assert again._pairs_to_place == []                                         # noqa: SLF001
    (pid, rec), = again._pairs_open.items()                                    # noqa: SLF001
    assert rec["side"] == "DOWN" and rec["price"] == 0.25 and rec["size"] == 5.0
    assert list(again.engine.pairs_working) == [pid]                           # the placeholder replaced
    sent = [s.payload for s in again.exchange._inflight]                       # noqa: SLF001
    assert len(sent) == 1 and sent[0].tif is TimeInForce.GTC and sent[0].token_id == "DOWN" and sent[0].price == 0.25


def test_a_pair_refused_while_its_market_is_open_is_placed_again(tmp_path):
    from troll_poly_bot.execution.paper import OrderResult, RejectReason
    from troll_poly_bot.types import OrderState
    meta = _now_meta()
    m = meta.market
    bot = _bot(tmp_path)
    bot.cfg.triangle.pair_cost = 0.95
    lm = LiveMarket(meta=meta)
    bot.markets[m.slug] = lm
    side, order = bot.engine.pair_order(m, "UP", 5.0, 0.70, m.open_ts)
    bot._place_pair(lm, side, order, "ord-entry", m.open_ts + 10_000.0)      # noqa: SLF001
    order.state = OrderState.REJECTED
    bot._on_result(OrderResult(order, [], RejectReason.NO_LIQUIDITY, m.open_ts + 11_000.0), m.open_ts + 11_000.0)  # noqa: SLF001
    assert len(bot._pairs_to_place) == 1 and len(bot.engine.pairs_working) == 1   # noqa: SLF001 -- still holding
    order2 = bot.engine.pair_order(m, "UP", 5.0, 0.70, m.open_ts)[1]
    order2.state = OrderState.CANCELLED
    bot._place_pair(lm, "DOWN", order2, "ord-entry2", m.open_ts + 12_000.0)    # noqa: SLF001
    bot._on_result(OrderResult(order2, [], RejectReason.MARKET_CLOSED, m.close_ts), m.close_ts)  # noqa: SLF001
    assert order2.order_id not in bot._pairs_open and bot.engine.pairs_expired == 1   # noqa: SLF001 -- resolved


def test_the_guard_cooldown_can_be_shortened_on_paper_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TPB_GUARD_COOLDOWN_S", "5")
    assert _bot(tmp_path).guard.cfg.cooldown_s == 5.0


def test_the_dashboard_sets_the_pair_total(tmp_path):
    from troll_poly_bot.control import apply_to, current_values, validate
    bot = _bot(tmp_path)
    bot.cfg.triangle.pair_cost = 0.95
    targets = bot._control_targets()                                          # noqa: SLF001
    assert current_values(bot.cfg, bot.exchange, **targets)["triangle.pair_cost"] == 0.95
    clean, errors = validate({"triangle.pair_cost": 0.96})
    assert errors == [] and clean == {"triangle.pair_cost": 0.96}
    assert apply_to(clean, bot.cfg, bot.exchange, **targets) == ["Pair total (Up + Down): 0.95 -> 0.96"]
    side, o = bot.engine.pair_order(_meta("BTC").market, "UP", 5.0, 0.70, NOW)
    assert o.price == 0.26                                                    # 0.96 - 0.70
    e, ev = _shock(6.0, up_now=(0.93, 0.95), scfg=_cfg(pair_cost=0.96, shares=5.0, limit_offset=0.99, max_order_usdc=5.0))
    assert ev.tradeable and ev.limit_price == 0.95                            # buys now stop at 0.96 - 0.01
    spot = _bot(tmp_path, strategy="spot_lag")
    assert "triangle.pair_cost" not in current_values(spot.cfg, spot.exchange, **spot._control_targets())  # noqa: SLF001


# ───────── 2026-10-01 14:10: every sudden-move triangle is an order; the price is the ask + a small limit ─────────


ALL = dict(trend_window_s=0.0, k3_sigma=-99.0, require_acceleration=False, dedupe_s=1.0, wait_for_pair=False,
           limit_offset=0.03, order_type="FOK", shares=5.0, max_order_usdc=5.0, pair_cost=0.95)


def test_the_app_settings_read_from_the_environment():
    env = {"TPB_TRIANGLE_ENABLED": "1", "TPB_TRIANGLE_LIMIT_OFFSET": "0.03", "TPB_TRIANGLE_WAIT_FOR_PAIR": "0",
           "TPB_TRIANGLE_TREND_WINDOW_S": "0", "TPB_TRIANGLE_K3_SIGMA": "-99",
           "TPB_TRIANGLE_REQUIRE_ACCELERATION": "0", "TPB_TRIANGLE_DEDUPE_S": "1"}
    c = TriangleConfig.from_env(env)
    assert (c.limit_offset, c.wait_for_pair, c.trend_window_s, c.k3_sigma, c.require_acceleration, c.dedupe_s) == (
        0.03, False, 0.0, -99.0, False, 1.0)


def test_the_limit_is_the_ask_seen_at_the_triangle_plus_three_cents():
    e, ev = _shock(6.0, scfg=_cfg(**ALL))
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.expected_price == 0.52 and ev.limit_price == 0.55
    o = e.to_order(ev, NOW)
    assert o.price == 0.55 and o.tif is TimeInForce.FOK and o.size == 5.0


def test_a_sudden_move_the_old_rules_skipped_is_now_an_order():
    # the 30 s trend filter against it: hollow before, an order now
    _, ev = _shock(6.0, scfg=_cfg(trend_window_s=30.0, trend_min_bps=1.0))
    assert not ev.tradeable
    e, ev = _shock(6.0, scfg=_cfg(**ALL))
    assert ev.tradeable and e.shock_marks_for(SLUG)[-1]["ok"] is True
    # a pair resting no longer holds the buy
    e = _engine(_cfg(**ALL))
    e.on_pair_submit("ord-p1", "btc-updown-5m-0", "DOWN", 0.20, 5.0)
    e, ev = _shock(6.0, e=e)
    assert ev.tradeable and e.refused.get("PAIR_WORKING") is None


def test_the_dashboard_sets_the_price_limit_and_the_pair_wait(tmp_path):
    from troll_poly_bot.control import apply_to, current_values, validate
    bot = _bot(tmp_path)
    t = bot._control_targets()                                                # noqa: SLF001
    cur = current_values(bot.cfg, bot.exchange, **t)
    assert "triangle.limit_offset" in cur and "triangle.wait_for_pair" in cur
    clean, errors = validate({"triangle.limit_offset": 0.05, "triangle.wait_for_pair": False})
    assert errors == [] and clean == {"triangle.limit_offset": 0.05, "triangle.wait_for_pair": False}
    apply_to(clean, bot.cfg, bot.exchange, **t)
    assert bot.cfg.triangle.limit_offset == 0.05 and bot.cfg.triangle.wait_for_pair is False


def test_the_other_side_ask_exists_only_after_the_buy_filled(tmp_path):
    from troll_poly_bot.execution.paper import OrderResult, RejectReason
    from troll_poly_bot.types import Fill, Order, OrderState, Side
    bot = _bot(tmp_path, pair_cost=0.96)
    lm = LiveMarket(meta=_meta("BTC"))
    bot.markets[SLUG] = lm

    def buy(token, side):
        o = Order(token_id=token, side=Side.BUY, price=0.55, size=5.0, tif=TimeInForce.FOK)
        bot.fair_by_order[o.order_id] = {"slug": SLUG, "asset": "BTC", "epoch": lm.epoch, "token_id": token, "side": side}
        lm.inflight += 1
        return o

    killed = buy("UP", "UP")                                    # the price ran away: the FOK was killed
    killed.state = OrderState.REJECTED
    bot._on_result(OrderResult(killed, [], RejectReason.FOK_UNFILLABLE, NOW), NOW)        # noqa: SLF001
    assert bot.engine.pairs_working == {} and bot.engine.pairs_placed == 0
    assert bot._pairs_open == {} and list(bot.exchange._inflight) == []                  # noqa: SLF001

    filled = buy("UP", "UP")                                    # bought: now, and only now, the other side's ask
    filled.filled_size, filled.state = 5.0, OrderState.FILLED
    bot._on_result(OrderResult(filled, [Fill(filled.order_id, "UP", Side.BUY, 0.50, 5.0, 0.0, NOW, NOW, NOW)],   # noqa: SLF001
                               RejectReason.NONE, NOW), NOW)
    (pid, rec), = bot._pairs_open.items()                                                # noqa: SLF001
    assert rec["side"] == "DOWN" and rec["price"] == 0.46 and rec["pair_of"] == filled.order_id
    assert [s.payload.order_id for s in bot.exchange._inflight] == [pid]                 # noqa: SLF001


def test_the_log_says_which_buy_an_ask_belongs_to_and_a_partial_fill_gets_none(tmp_path, caplog):
    import logging
    from troll_poly_bot.execution.paper import OrderResult, RejectReason
    from troll_poly_bot.types import Fill, Order, OrderState, Side
    bot = _bot(tmp_path, pair_cost=0.96)
    lm = LiveMarket(meta=_meta("BTC"))
    bot.markets[SLUG] = lm

    def buy(size=5.0):
        o = Order(token_id="UP", side=Side.BUY, price=0.55, size=size, tif=TimeInForce.FOK)
        bot.fair_by_order[o.order_id] = {"slug": SLUG, "asset": "BTC", "epoch": lm.epoch, "token_id": "UP", "side": "UP"}
        lm.inflight += 1
        return o

    caplog.set_level(logging.INFO, logger="live")
    killed = buy()
    killed.state = OrderState.REJECTED
    bot._on_result(OrderResult(killed, [], RejectReason.FOK_UNFILLABLE, NOW), NOW)        # noqa: SLF001
    assert any("NO other-side ask" in r.getMessage() for r in caplog.records)
    caplog.clear()
    ok = buy()
    ok.filled_size, ok.state = 5.0, OrderState.FILLED
    bot._on_result(OrderResult(ok, [Fill(ok.order_id, "UP", Side.BUY, 0.50, 5.0, 0.0, NOW, NOW, NOW)],   # noqa: SLF001
                               RejectReason.NONE, NOW), NOW)
    line = next(r.getMessage() for r in caplog.records if "pair buy resting" in r.getMessage())
    assert "for the UP buy of 5.00 sh @ 0.500 that FILLED" in line and ok.order_id in line
    caplog.clear()
    part = buy(size=5.0)                                        # a fill smaller than the order: no ask at all
    part.filled_size, part.state = 3.0, OrderState.FILLED
    before = bot.engine.pairs_placed
    bot._on_result(OrderResult(part, [Fill(part.order_id, "UP", Side.BUY, 0.50, 3.0, 0.0, NOW, NOW, NOW)],   # noqa: SLF001
                               RejectReason.NONE, NOW), NOW)
    assert bot.engine.pairs_placed == before and any("NO other-side ask" in r.getMessage() for r in caplog.records)


def test_strict_pair_keeps_the_sum_of_both_prices_under_pair_cost():
    """2026-10-01 ~20:20, the user: "when you buy one side, set an order on the opposite side; the sum of the
    two must be under 0.95"."""
    e = _engine(_cfg(pair_cost=0.95, pair_cost_strict=True))
    m = _meta("BTC").market
    for fill, want in ((0.60, 0.34), (0.75, 0.19), (0.605, 0.34), (0.333, 0.61), (0.93, 0.01)):
        side, o = e.pair_order(m, "UP", 5.0, fill, NOW)
        assert side == "DOWN" and o.price == pytest.approx(want) and o.tif is TimeInForce.GTC and o.size == 5.0
        assert fill + o.price < 0.95 - 1e-9
    assert e.pair_order(m, "UP", 5.0, 0.94, NOW) is None                  # 0.94 + 0.01 is not under 0.95
    # with room required, buys stop two ticks under: every fill gets a pair
    e, ev = _shock(6.0, up_now=(0.93, 0.94), scfg=_cfg(pair_cost=0.95, pair_cost_strict=True, shares=5.0,
                                                        limit_offset=0.99, max_order_usdc=5.0))
    assert ev.reason is Reason.PRICE_BAND and "under 0.95" in ev.detail
    e, ev = _shock(6.0, up_now=(0.88, 0.90), scfg=_cfg(pair_cost=0.95, pair_cost_strict=True, shares=5.0,
                                                        limit_offset=0.99, max_order_usdc=5.0))
    assert ev.tradeable and ev.limit_price == 0.93
