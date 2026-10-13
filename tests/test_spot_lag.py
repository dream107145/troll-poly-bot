"""SPOT_LAG (strategy/spot_lag.py): the feature flag, the spot-shock signal, the
lag score on the executable ask, the cost-capped FOK, the timed exit, the
real-money gate, and the paper engine's venue_lag fill model."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from troll_poly_bot.execution.latency import COLOCATED, INSTANT, LatencyModel
from troll_poly_bot.execution.paper import FeeModel, PaperExchange, RejectReason
from troll_poly_bot.execution.scenarios import VENUE_LAG, scenario_named
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.pricing.twap import TwapState
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.spot_lag import SpotLagConfig, SpotLagEngine, market_implied_fair
from troll_poly_bot.types import BookLevel, Market, Order, OrderBook, Side, TimeInForce

T0 = 1_000_000_000.0                  # window open
NOW = T0 + 120_000.0                  # 180 s left
SLUG = "btc-updown-5m-1"
SIGMA = 1e-4                          # 1 bp per second
FEE = FeeSchedule()


class _Vol:
    ready = True
    sigma_per_sec = SIGMA


class _Twap(TwapState):
    """A TwapState that remembers how far the test has fed it."""
    __slots__ = ("_fed_until",)


def _cfg(**kw) -> SpotLagConfig:
    base = dict(enabled=True)
    base.update(kw)
    return SpotLagConfig(**base)


def _engine(scfg: SpotLagConfig | None = None) -> SpotLagEngine:
    risk = RiskManager(cfg=RiskConfig(max_position_usdc=50.0, min_order_usdc=1.0, micro_fixed_size=True),
                       starting_balance=100.0)
    return SpotLagEngine(EngineConfig(strategy="spot_lag"), scfg or _cfg(), risk, FeatureEngine())


def _meta(asset: str = "BTC") -> MarketMeta:
    return MarketMeta(market=Market(condition_id="c", asset=asset, yes_token_id="UP", no_token_id="DOWN",
                                    strike=100.0, open_ts=T0, close_ts=T0 + 300_000.0, slug=SLUG, min_size=5.0),
                      resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)


def _feed(e: SpotLagEngine, twap: TwapState, jump_bps: float, asset: str = "BTC", until: float = NOW) -> None:
    """Flat at 100 for a minute, then a jump spread over the last 800 ms before NOW;
    fed in time order up to ``until`` (the TWAP state has no notion of the future)."""
    pts = [(t, 100.0) for t in range(int(NOW - 60_000.0), int(NOW - 800.0) + 1, 200)]
    pts += [(NOW - 800.0 + 200.0 * k, 100.0 * math.exp(jump_bps * 1e-4 * k / 4)) for k in range(1, 5)]
    done = getattr(twap, "_fed_until", -math.inf)
    for t, px in pts:
        if done < t <= until:
            e.features.update_spot(asset, px, t)
            twap.update(px, t)
    twap._fed_until = until                                           # noqa: SLF001 (test bookkeeping)


def _book(token: str, bid: float, ask: float, size: float = 100.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)])


def _eval(e, twap, now, up, down, asset="BTC", quote_ages=None):
    view = CompositeView(asset, 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
    return e.evaluate(_meta(asset), up, down, view, 200.0, 100.0, now, _Vol(), twap, COLOCATED, 100.0,
                      quote_ages=quote_ages)


def _shock(jump_bps=6.0, up_now=(0.50, 0.52), asset="BTC", scfg=None, quote_ages=None):
    """Evaluate 1 s before the shock (history), then at the shock with the given Up book."""
    e, twap = _engine(scfg), _Twap()
    _feed(e, twap, jump_bps, asset, until=NOW - 1000.0)
    flat = (_book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    _eval(e, twap, NOW - 1000.0, *flat, asset=asset)
    _feed(e, twap, jump_bps, asset, until=NOW)
    up = _book("UP", *up_now)
    down = _book("DOWN", round(1 - up_now[1], 2), round(1 - up_now[0], 2))
    return e, twap, _eval(e, twap, NOW, up, down, asset=asset, quote_ages=quote_ages)


# ─────────────────────────────── the flag ───────────────────────────────


def test_the_engine_is_behind_a_flag():
    with pytest.raises(ValueError, match="TPB_SPOT_LAG_ENABLED"):
        _engine(SpotLagConfig())
    assert SpotLagConfig.from_env({}).enabled is False
    cfg = SpotLagConfig.from_env({"TPB_SPOT_LAG_ENABLED": "1", "TPB_SPOT_LAG_K_SIGMA": "4",
                                  "TPB_SPOT_LAG_EXCLUDE_ASSETS": "", "TPB_SPOT_LAG_HOLD_S": "bad"})
    assert cfg.enabled and cfg.k_sigma == 4.0 and cfg.exclude_assets == "" and cfg.hold_s == 5.0


def test_real_money_needs_a_second_flag():
    from troll_poly_bot.__main__ import check_spot_lag
    check_spot_lag("momentum_taker", "micro-live", {})                       # other strategies untouched
    with pytest.raises(SystemExit, match="TPB_SPOT_LAG_ENABLED"):
        check_spot_lag("spot_lag", "paper", {})
    check_spot_lag("spot_lag", "paper", {"TPB_SPOT_LAG_ENABLED": "1"})
    check_spot_lag("spot_lag", "shadow", {"TPB_SPOT_LAG_ENABLED": "1"})
    for mode in ("micro-live", "live"):
        with pytest.raises(SystemExit, match="TPB_SPOT_LAG_ALLOW_REAL"):
            check_spot_lag("spot_lag", mode, {"TPB_SPOT_LAG_ENABLED": "1"})
    check_spot_lag("spot_lag", "micro-live", {"TPB_SPOT_LAG_ENABLED": "1", "TPB_SPOT_LAG_ALLOW_REAL": "1"})


def test_live_bot_builds_it_only_with_the_flag(tmp_path):
    from troll_poly_bot.config import BotConfig
    from troll_poly_bot.live import LiveBot
    from troll_poly_bot.strategy.momentum import MomentumEngine
    kw = dict(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
              earnings_path=str(tmp_path / "e.jsonl"))
    cfg = BotConfig()
    cfg.engine.strategy = "spot_lag"
    cfg.spot_lag = SpotLagConfig()
    with pytest.raises(ValueError, match="feature flag"):
        LiveBot(cfg=cfg, **kw)
    cfg.spot_lag = _cfg()
    bot = LiveBot(cfg=cfg, **kw)
    # a MomentumEngine subclass: the live bot's momentum exit and journal paths apply
    assert isinstance(bot.engine, SpotLagEngine) and isinstance(bot.engine, MomentumEngine)
    assert bot.engine.mcfg.max_quote_age_ms == 250.0 and bot.engine.mcfg.min_time_remaining_s == 60.0
    assert bot.engine.snapshot()["strategy"] == "spot_lag"


# ─────────────────────────────── entry ───────────────────────────────


def test_market_implied_fair_is_the_markets_own_curve():
    assert market_implied_fair(0.5, 150.0) == pytest.approx(0.5, abs=0.01)
    # the market is less extreme than the model early in the window (b < 1)
    assert 0.5 < market_implied_fair(0.8, 270.0) < market_implied_fair(0.8, 75.0) < 0.8
    assert market_implied_fair(0.2, 150.0) < 0.5
    assert market_implied_fair(0.5, 400.0) is None


def test_a_shock_with_a_lagging_token_is_bought_under_the_cap():
    e, _, ev = _shock(6.0)                                     # z1 = 6, the Up book has not moved
    assert ev.tradeable, (ev.reason, ev.detail)
    sl = ev.features["spot_lag"]
    assert ev.side == "UP" and ev.token_id == "UP" and sl["z1"] >= 3.0
    assert sl["lag"] > 0.0 and ev.net_edge >= 0.02
    assert ev.limit_price <= ev.max_entry_price + 1e-9 and ev.limit_price <= 0.52 + 0.02 + 1e-9
    assert ev.size == 5.0 and ev.expected_price == pytest.approx(0.52)
    order = e.to_order(ev)
    assert order.side is Side.BUY and order.tif is TimeInForce.FOK and order.price == ev.limit_price
    assert "spot_lag" in order.tag


def _expected(jump_bps: float) -> float:
    """The engine's own expected token value for this shock (the book does not enter it)."""
    return _shock(jump_bps)[2].features["spot_lag"]["ev"]


def test_a_token_that_already_repriced_is_not_chased():
    ev_px = _expected(6.0)
    bid, ask = round(ev_px - 0.01, 2), round(ev_px + 0.01, 2)   # the book caught up already
    e, _, ev = _shock(6.0, up_now=(bid, ask))
    assert ev.reason is Reason.TOKEN_ALREADY_MOVED and not ev.tradeable


def test_a_down_shock_buys_the_down_token():
    e, _, ev = _shock(-6.0)
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.side == "DOWN" and ev.token_id == "DOWN"


def test_a_small_move_is_not_a_signal():
    _, _, ev = _shock(2.0)                                     # z1 = 2 < 3
    assert ev.reason is Reason.WEAK_MOMENTUM


def test_a_small_lag_does_not_clear_the_costs():
    # 3c behind: after both fees (~1.4c each near 0.7), half the spread, slippage and
    # uncertainty that is not worth the 2c the strategy requires
    ev_px = _expected(6.0)
    ask = round(ev_px - 0.03, 2)
    _, _, ev = _shock(6.0, up_now=(round(ask - 0.01, 2), ask))
    assert not ev.tradeable and ev.features["spot_lag"]["lag"] > 0.0
    assert ev.reason in (Reason.EDGE_TOO_SMALL, Reason.ASK_ABOVE_MAX_ENTRY)


def test_excluded_asset_and_stale_quote_are_refused():
    _, _, ev = _shock(6.0, asset="HYPE")
    assert ev.reason is Reason.ASSET_FILTERED
    _, _, ev = _shock(6.0, quote_ages={"UP": 900.0, "DOWN": 20.0})
    assert ev.reason is Reason.STALE_DATA and "900" in ev.detail


def test_one_signal_per_direction_per_dedupe_window():
    e, twap, ev = _shock(6.0)
    assert ev.tradeable
    again = _eval(e, twap, NOW + 200.0, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    assert again.reason is Reason.COOLDOWN


def test_no_history_no_trade():
    e, twap = _engine(), _Twap()
    _feed(e, twap, 6.0)
    ev = _eval(e, twap, NOW, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))   # nothing 1 s ago
    assert ev.reason is Reason.MODEL_NOT_READY


# ─────────────────────────────── exit ───────────────────────────────


def test_the_exit_is_timed():
    e = _engine(_cfg(hold_s=5.0))
    kw = dict(asset="BTC", side="UP", token_id="UP", entry=0.52, bid=0.55)
    assert e.exit_decision(**kw, now_ms=NOW) == "TIME_STOP"                 # unknown entry time: close it
    e.on_entry_fill(SLUG, "UP", NOW)
    assert e.exit_decision(**kw, now_ms=NOW + 4_900.0) is None
    assert e.exit_decision(**kw, now_ms=NOW + 5_000.0) == "TIME_STOP"
    e.on_exit(SLUG, "UP", NOW + 5_000.0)
    assert "UP" not in e.entry_ms


def test_the_exit_waits_while_the_spot_is_still_moving_our_way():
    e = _engine(_cfg(hold_s=5.0, extend_z1=0.5, extend_max_hold_s=10.0))
    e.last_sigma["BTC"] = SIGMA
    t_fill = NOW
    e.on_entry_fill(SLUG, "UP", t_fill)

    from troll_poly_bot.features.engine import SpotHistory

    def spot(bps_last_second, at):
        # flat, then this move over the second before ``at``
        h = SpotHistory()
        h.update(100.0, at - 5_000.0)
        h.update(100.0, at - 1_000.0)
        h.update(100.0 * math.exp(bps_last_second * 1e-4), at)
        e.features.spot["BTC"] = h
        if "off" in locals_:
            locals_["off"].features.spot["BTC"] = h

    locals_: dict = {}

    kw = dict(asset="BTC", token_id="UP", entry=0.52, bid=0.55)
    spot(+1.0, t_fill + 5_000.0)                                          # +1 sigma our way at 5 s
    assert e.exit_decision(side="UP", now_ms=t_fill + 5_000.0, **kw) is None
    spot(+0.2, t_fill + 6_000.0)                                          # stalled: sell now
    assert e.exit_decision(side="UP", now_ms=t_fill + 6_000.0, **kw) == "TIME_STOP"
    spot(+3.0, t_fill + 10_000.0)                                         # never past the maximum
    assert e.exit_decision(side="UP", now_ms=t_fill + 10_000.0, **kw) == "TIME_STOP"
    # a DOWN position waits on a falling spot, not a rising one
    e.on_entry_fill(SLUG, "DOWN", t_fill)
    spot(-1.0, t_fill + 5_000.0)
    assert e.exit_decision(side="DOWN", now_ms=t_fill + 5_000.0, asset="BTC", token_id="DOWN",
                           entry=0.5, bid=0.5) is None
    spot(+1.0, t_fill + 5_000.0)
    assert e.exit_decision(side="DOWN", now_ms=t_fill + 5_000.0, asset="BTC", token_id="DOWN",
                           entry=0.5, bid=0.5) == "TIME_STOP"
    # extend_max_hold_s <= hold_s: the plain timer
    off = _engine(_cfg(hold_s=5.0, extend_max_hold_s=5.0))
    off.last_sigma["BTC"] = SIGMA
    locals_["off"] = off
    off.on_entry_fill(SLUG, "UP", t_fill)
    spot(+3.0, t_fill + 5_000.0)
    assert off.exit_decision(side="UP", now_ms=t_fill + 5_000.0, **kw) == "TIME_STOP"


# ─────────────────────────── venue_lag fills ───────────────────────────


def test_venue_lag_matches_the_book_as_seen_500ms_after_arrival():
    assert scenario_named("venue_lag") is VENUE_LAG and VENUE_LAG.quote_survival_ms == 0.0
    books = {"early": _book("T", 0.49, 0.50), "late": _book("T", 0.59, 0.60)}
    clock = SimpleNamespace(now=0.0)

    def book_of(_token):          # the local book: stale until 400 ms, then shows the venue's repricing
        return books["early" if clock.now < 400.0 else "late"]

    def run(scenario):
        ex = PaperExchange(LatencyModel(INSTANT), FeeModel(rate=0.0), starting_balance=100.0, scenario=scenario)
        ex.submit(Order(token_id="T", side=Side.BUY, price=0.52, size=5.0, tif=TimeInForce.FOK), 0.0)
        out = []
        for t in range(0, 1200, 100):
            clock.now = float(t)
            out += ex.step(float(t), book_of)
        return out

    optimistic = run(scenario_named("optimistic"))
    assert optimistic and optimistic[0].fills and optimistic[0].fills[0].price == pytest.approx(0.50)
    lagged = run(VENUE_LAG)                  # matched at 500 ms against the repriced book: killed
    assert lagged and not lagged[0].fills and lagged[0].reject_reason is RejectReason.FOK_UNFILLABLE


# ─────────────────────────── the guard ───────────────────────────


def _bot(tmp_path, mode="paper", strategy="spot_lag"):
    from troll_poly_bot.config import BotConfig
    from troll_poly_bot.live import LiveBot
    cfg = BotConfig()
    cfg.engine.strategy = strategy
    cfg.deploy_mode = mode
    cfg.spot_lag = _cfg()
    return LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"),
                   trade_log=str(tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))


def test_expected_fok_kills_do_not_feed_the_execution_failure_guard(tmp_path):
    from troll_poly_bot.execution.paper import OrderResult
    bot = _bot(tmp_path)

    def reject(reason):
        order = Order(token_id="UP", side=Side.BUY, price=0.52, size=5.0, tif=TimeInForce.FOK)
        bot._on_result(OrderResult(order, [], reason, 0.0), NOW)            # noqa: SLF001

    for _ in range(12):
        reject(RejectReason.FOK_UNFILLABLE)          # the lag was gone by arrival: the design, not a failure
        reject(RejectReason.QUOTE_PULLED)
    assert len(bot.guard.outcomes) == 0 and bot.guard.kill_rate() is None
    reject(RejectReason.VENUE_REJECT)                # the venue refusing us still counts
    assert list(bot.guard.outcomes) == [False]
    # every other engine keeps counting FOK kills
    other = _bot(tmp_path / "m", strategy="momentum_taker")
    order = Order(token_id="UP", side=Side.BUY, price=0.52, size=5.0, tif=TimeInForce.FOK)
    other._on_result(OrderResult(order, [], RejectReason.FOK_UNFILLABLE, 0.0), NOW)   # noqa: SLF001
    assert list(other.guard.outcomes) == [False]


def test_the_loss_streak_halt_is_off_only_for_paper(tmp_path):
    assert _bot(tmp_path / "p", "paper").guard.cfg.max_consecutive_losses == 0
    assert _bot(tmp_path / "s", "shadow").guard.cfg.max_consecutive_losses == 5
    assert _bot(tmp_path / "m", "paper", "momentum_taker").guard.cfg.max_consecutive_losses == 5


def test_micro_live_limits_bound_the_cap():
    # MicroCaps writes its slippage / spread ceilings into EngineConfig; the tighter one wins
    risk = RiskManager(cfg=RiskConfig(max_position_usdc=50.0, min_order_usdc=1.0, micro_fixed_size=True),
                       starting_balance=100.0)
    e = SpotLagEngine(EngineConfig(strategy="spot_lag", max_slippage=0.01), _cfg(), risk, FeatureEngine())
    twap = _Twap()
    _feed(e, twap, 6.0, until=NOW - 1000.0)
    _eval(e, twap, NOW - 1000.0, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    _feed(e, twap, 6.0, until=NOW)
    ev = _eval(e, twap, NOW, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    assert ev.tradeable and ev.limit_price <= 0.52 + 0.01 + 1e-9
    e2 = SpotLagEngine(EngineConfig(strategy="spot_lag", max_spread=0.01), _cfg(), risk, FeatureEngine())
    twap = _Twap()
    _feed(e2, twap, 6.0, until=NOW - 1000.0)
    _eval(e2, twap, NOW - 1000.0, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    _feed(e2, twap, 6.0, until=NOW)
    ev2 = _eval(e2, twap, NOW, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    assert ev2.reason is Reason.LOW_LIQUIDITY                       # a 2c spread over a 1c ceiling


def test_profile_optimized_applies_then_env_overrides():
    import pytest as _pytest
    from troll_poly_bot.strategy.spot_lag import SpotLagConfig as _C
    base = _C.from_env({"TPB_SPOT_LAG_ENABLED": "1"})
    assert (base.max_ask, base.max_quote_age_ms, base.max_spot_age_ms) == (0.95, 250.0, 1000.0)
    opt = _C.from_env({"TPB_SPOT_LAG_ENABLED": "1", "TPB_SPOT_LAG_PROFILE": "optimized"})
    assert (opt.max_ask, opt.max_quote_age_ms, opt.max_spot_age_ms, opt.max_slippage, opt.max_spread) == (0.60, 1000.0, 500.0, 0.03, 0.05)
    assert opt.min_net_edge == base.min_net_edge and opt.k_sigma == base.k_sigma     # the profile changes nothing else
    over = _C.from_env({"TPB_SPOT_LAG_ENABLED": "1", "TPB_SPOT_LAG_PROFILE": "optimized", "TPB_SPOT_LAG_MAX_ASK": "0.70"})
    assert over.max_ask == 0.70 and over.max_quote_age_ms == 1000.0                  # explicit env still wins
    with _pytest.raises(ValueError):
        _C.from_env({"TPB_SPOT_LAG_PROFILE": "nope"})


def test_price_exits_fire_before_the_timer_when_configured():
    e = _engine(_cfg(hold_s=20.0, extend_max_hold_s=0.0, take_profit_cents=10.0, stop_loss_cents=5.0))
    e.entry_ms["UP"] = NOW
    kw = dict(asset="BTC", side="UP", token_id="UP", entry=0.50)
    assert e.exit_decision(**kw, bid=0.55, now_ms=NOW + 1_000.0) is None            # +5c: neither
    assert e.exit_decision(**kw, bid=0.60, now_ms=NOW + 1_000.0) == "TAKE_PROFIT"   # +10c, at once
    assert e.exit_decision(**kw, bid=0.45, now_ms=NOW + 1_000.0) == "STOP_LOSS"     # -5c, at once
    assert e.exit_decision(**kw, bid=0.46, now_ms=NOW + 19_000.0) is None           # -4c: still held
    assert e.exit_decision(**kw, bid=0.55, now_ms=NOW + 20_000.0) == "TIME_STOP"    # the cap
    # DOWN positions read the same way: the Down token's bid against its entry
    e.entry_ms["DOWN"] = NOW
    assert e.exit_decision(asset="BTC", side="DOWN", token_id="DOWN", entry=0.30, bid=0.41, now_ms=NOW + 500.0) == "TAKE_PROFIT"
    # off by default: the timer alone
    e2 = _engine(_cfg(hold_s=5.0))
    e2.entry_ms["UP"] = NOW
    assert e2.exit_decision(**kw, bid=0.90, now_ms=NOW + 1_000.0) is None
    assert e2.exit_decision(**kw, bid=0.10, now_ms=NOW + 1_000.0) is None



def test_a_shorter_shock_window_fires_on_the_same_move_earlier():
    # the standard 6 bps shock is spread over 800 ms; over the last 500 ms it is ~3-4.5 bps,
    # which is >= 3 sigma once sqrt(0.5)-scaled, so the fast rule fires as the 1 s rule does
    e, _twap, ev = _shock(jump_bps=6.0, scfg=_cfg(shock_window_s=0.5))
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.side == "UP" and ev.features["spot_lag"]["z1"] >= 3.0
    # a 2.9 bps move: 2.9 sigma over 1 s (no signal), but its last 600 ms (the archive's
    # 200 ms cadence makes a 500 ms lookback span three steps) carry 2.2 bps = 3.1 sigma
    # once sqrt(0.5)-scaled: only the fast window sees it
    e2, _t2, ev2 = _shock(jump_bps=2.9, scfg=_cfg(shock_window_s=0.5, k_sigma=3.0))
    e3, _t3, ev3 = _shock(jump_bps=2.9)
    assert ev2.tradeable, (ev2.reason, ev2.detail)
    assert not ev3.tradeable and ev3.reason is Reason.WEAK_MOMENTUM


def test_touch_sizing_takes_what_the_book_holds():
    def thin(size):        # ONE ask level under the limit, nothing behind it
        return OrderBook("UP", bids=[BookLevel(0.50, 100.0)], asks=[BookLevel(0.52, size), BookLevel(0.60, 100.0)])
    up_thin = thin(7.0)
    down = _book("DOWN", 0.48, 0.50)
    # fixed: 10 shares or nothing
    e, twap = _engine(_cfg(size_shares=10.0)), _Twap()
    _feed(e, twap, 6.0, until=NOW - 1000.0)
    _eval(e, twap, NOW - 1000.0, _book("UP", 0.50, 0.51), _book("DOWN", 0.49, 0.50))
    _feed(e, twap, 6.0, until=NOW)
    ev = _eval(e, twap, NOW, up_thin, down)
    assert not ev.tradeable and ev.reason is Reason.LOW_LIQUIDITY
    # touch: 7 shares, the whole top level
    e, twap = _engine(_cfg(size_shares=10.0, size_mode="touch")), _Twap()
    _feed(e, twap, 6.0, until=NOW - 1000.0)
    _eval(e, twap, NOW - 1000.0, _book("UP", 0.50, 0.51), _book("DOWN", 0.49, 0.50))
    _feed(e, twap, 6.0, until=NOW)
    ev = _eval(e, twap, NOW, up_thin, down)
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.size == pytest.approx(7.0) and ev.expected_price == pytest.approx(0.52)
    # but never below the venue minimum
    e, twap = _engine(_cfg(size_shares=10.0, size_mode="touch")), _Twap()
    _feed(e, twap, 6.0, until=NOW - 1000.0)
    _eval(e, twap, NOW - 1000.0, _book("UP", 0.50, 0.51), _book("DOWN", 0.49, 0.50))
    _feed(e, twap, 6.0, until=NOW)
    ev = _eval(e, twap, NOW, thin(4.0), down)
    assert not ev.tradeable and ev.reason is Reason.LOW_LIQUIDITY


def test_hold_to_settlement_never_times_out_but_price_exits_still_apply():
    e = _engine(_cfg(hold_s=5.0, hold_to_settlement=True))
    kw = dict(asset="BTC", side="UP", token_id="UP", entry=0.50)
    assert e.exit_decision(**kw, bid=0.55, now_ms=NOW) is None                      # no entry time: still held
    e.entry_ms["UP"] = NOW
    assert e.exit_decision(**kw, bid=0.30, now_ms=NOW + 250_000.0) is None          # never a time stop
    e2 = _engine(_cfg(hold_to_settlement=True, take_profit_cents=10.0, stop_loss_cents=5.0))
    e2.entry_ms["UP"] = NOW
    assert e2.exit_decision(**kw, bid=0.60, now_ms=NOW + 1_000.0) == "TAKE_PROFIT"
    assert e2.exit_decision(**kw, bid=0.45, now_ms=NOW + 1_000.0) == "STOP_LOSS"
    assert e2.exit_decision(**kw, bid=0.52, now_ms=NOW + 200_000.0) is None



def test_fak_entries_send_the_full_size_when_the_venue_minimum_shows():
    def thin(size):        # ONE ask level under the limit, nothing behind it
        return OrderBook("UP", bids=[BookLevel(0.50, 100.0)], asks=[BookLevel(0.52, size), BookLevel(0.60, 100.0)])
    down = _book("DOWN", 0.48, 0.50)

    def run(scfg, up_now):
        e, twap = _engine(scfg), _Twap()
        _feed(e, twap, 6.0, until=NOW - 1000.0)
        _eval(e, twap, NOW - 1000.0, _book("UP", 0.50, 0.51), _book("DOWN", 0.49, 0.50))
        _feed(e, twap, 6.0, until=NOW)
        return e, _eval(e, twap, NOW, up_now, down)

    # 7 shares under the cap: FOK refuses a 10-share order, FAK sends it and takes what is there
    e, ev = run(_cfg(size_shares=10.0), thin(7.0))
    assert not ev.tradeable and ev.reason is Reason.LOW_LIQUIDITY
    _e, full = run(_cfg(size_shares=10.0), _book("UP", 0.50, 0.52))   # the size a full book gets (the order cap trims it)
    e, ev = run(_cfg(size_shares=10.0, order_type="FAK"), thin(7.0))
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.size == pytest.approx(full.size) and ev.size > 7.0      # the whole order, not the 7 visible
    assert ev.features["spot_lag"]["fak_visible_shares"] == pytest.approx(7.0)
    order = e.to_order(ev, NOW)
    assert order.tif is TimeInForce.IOC and order.size == ev.size and order.price == ev.limit_price
    # below the venue minimum even a FAK is not sent
    e, ev = run(_cfg(size_shares=10.0, order_type="FAK"), thin(4.0))
    assert not ev.tradeable and ev.reason is Reason.LOW_LIQUIDITY
    # a full book: FAK is an IOC of the full size; the default stays FOK
    e, ev = run(_cfg(size_shares=10.0, order_type="FAK"), _book("UP", 0.50, 0.52))
    assert ev.tradeable and e.to_order(ev, NOW).tif is TimeInForce.IOC
    e, ev = run(_cfg(size_shares=10.0), _book("UP", 0.50, 0.52))
    assert ev.tradeable and e.to_order(ev, NOW).tif is TimeInForce.FOK
