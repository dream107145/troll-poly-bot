"""LATE_CONFIDENCE (strategy/late_confidence.py, the user's spec 2026-10-08): the code runs as specified -- the >0.98
token in the last 30 s when the settling TWAP is >= 2 sd away and P(win) beats limit + fee + spread + margin; 20 shares
fill-and-kill, one per market, two at once, never more than the cash; held to settlement; paper only. These tests
check the mechanics, not whether the strategy makes money (it has not been backtested)."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from troll_poly_bot.__main__ import check_late_confidence
from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy import late_confidence as lc
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.late_confidence import LateConfidenceConfig, LateConfidenceEngine, winning_side
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce

T0 = 1_800_000_000_000.0
FEE = FeeSchedule()


def _book(token, bid, ask, size=100.0):
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size)])


class _Vol:
    ready = True
    sigma_per_sec = 1e-4


class _World:
    def __init__(self, monkeypatch, balance=40.0, **kw):
        self.scfg = LateConfidenceConfig(enabled=True, **kw)
        rc = RiskConfig(max_position_usdc=20.0, max_total_exposure_usdc=40.0, max_concurrent_positions=2,
                        max_asset_exposure_usdc=40.0, max_epoch_exposure_usdc=40.0, max_daily_loss_usdc=40.0,
                        micro_fixed_size=True)
        self.scfg.apply_budget(rc)
        self.risk = RiskManager(cfg=rc, starting_balance=balance)
        self.e = LateConfidenceEngine(EngineConfig(strategy="late_confidence"), self.scfg, self.risk, FeatureEngine())
        self.fv = SimpleNamespace(p_up=0.999, z=3.0)
        monkeypatch.setattr(lc, "twap_fair_value", lambda **k: self.fv)

    def meta(self, asset="BTC", open_ts=T0, tick=0.01):
        m = Market(condition_id=f"c-{asset}", asset=asset, yes_token_id=f"{asset}-UP", no_token_id=f"{asset}-DN",
                   strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                   slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}", min_size=5.0)
        m.tick_size = tick
        return MarketMeta(market=m, resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)

    def evaluate(self, meta, left_s, up_ask, dn_ask, spread=0.01, depth=100.0, balance=40.0):
        m = meta.market
        now = m.close_ts - left_s * 1000.0
        view = CompositeView(m.asset, 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
        up = _book(m.yes_token_id, round(up_ask - spread, 4), up_ask, depth)
        dn = _book(m.no_token_id, round(dn_ask - spread, 4), dn_ask, depth)
        return self.e.evaluate(meta, up, dn, view, 100.0, 100.0, now, _Vol(), SimpleNamespace(ready=True), COLOCATED, balance)


def test_flag_and_paper_only():
    assert LateConfidenceConfig().enabled is False
    with pytest.raises(ValueError):
        LateConfidenceEngine(EngineConfig(strategy="late_confidence"), LateConfidenceConfig(), RiskManager(), FeatureEngine())
    with pytest.raises(SystemExit):
        check_late_confidence("late_confidence", "paper", {})
    check_late_confidence("late_confidence", "paper", {"TPB_LATECONF_ENABLED": "1"})
    for mode in ("shadow", "micro-live", "live"):
        with pytest.raises(SystemExit):
            check_late_confidence("late_confidence", mode, {"TPB_LATECONF_ENABLED": "1"})
    check_late_confidence("cheapside", "live", {})                       # other strategies are not its business


def test_env_and_the_spec_defaults():
    c = LateConfidenceConfig.from_env({"TPB_LATECONF_ENABLED": "1", "TPB_LATECONF_MIN_Z": "2.5",
                                       "TPB_LATECONF_SIZE_SHARES": "20", "TPB_LATECONF_MAX_TIME_REMAINING_S": "30"})
    assert c.enabled and c.min_z == 2.5 and c.size_shares == 20.0 and c.max_time_remaining_s == 30.0
    d = LateConfidenceConfig()
    assert (d.min_ask, d.max_time_remaining_s, d.min_z, d.size_shares, d.max_simultaneous_positions, d.hold_to_settlement,
            d.stop_loss_cents, d.order_type) == (0.98, 30.0, 2.0, 20.0, 2, True, 0.0, "FAK")
    assert hasattr(BotConfig(), "late_confidence")


def test_winning_side_is_the_higher_ask():
    assert winning_side(0.99, 0.02) == "UP" and winning_side(0.02, 0.99) == "DOWN"
    assert winning_side(0.5, 0.5) is None and winning_side(None, 0.9) is None


def test_qualifying_entry(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta()
    ev = w.evaluate(meta, 20.0, up_ask=0.99, dn_ask=0.02)
    assert ev.reason is None, (ev.reason, ev.detail)
    assert ev.side == "UP" and ev.size == 20.0 and ev.limit_price == pytest.approx(0.992)  # ask + 0.2c
    f = ev.features["late_confidence"]
    assert f["signal"] and f["z"] == pytest.approx(3.0) and f["p_win"] == pytest.approx(0.999)
    assert f["edge"] == pytest.approx(0.999 - 0.992 - FEE.taker_fee_per_share(0.992) - 0.005, abs=1e-5)
    o = w.e.to_order(ev, 0.0)
    assert o.tif is TimeInForce.IOC and o.size == 20.0 and o.price == pytest.approx(0.992) and "late_confidence" in o.tag
    # DOWN is the winning side when its ask is the high one; the z must point DOWN too
    w.fv = SimpleNamespace(p_up=0.001, z=-3.0)
    ev = w.evaluate(w.meta("ETH"), 20.0, up_ask=0.02, dn_ask=0.99)
    assert ev.reason is None and ev.side == "DOWN" and ev.token_id == "ETH-DN"
    assert w.e.exit_decision(asset="BTC", side="UP", token_id="x", entry=0.99, bid=0.5, now_ms=0.0) is None   # no stop


def test_the_rules_refuse(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta()
    assert w.evaluate(meta, 45.0, 0.99, 0.02).reason is Reason.OUTSIDE_TIME_WINDOW        # > 30 s left
    assert w.evaluate(meta, 1.0, 0.99, 0.02).reason is Reason.OUTSIDE_TIME_WINDOW         # < 2 s left
    assert w.evaluate(meta, 20.0, 0.98, 0.03).reason is Reason.PRICE_BAND                 # not ABOVE 0.98
    w.fv = SimpleNamespace(p_up=0.98, z=1.9)
    ev = w.evaluate(meta, 20.0, 0.99, 0.02)
    assert ev.reason is Reason.WEAK_MOMENTUM and "z" in ev.detail                         # z under 2
    w.fv = SimpleNamespace(p_up=0.99, z=-2.5)                                             # the spot says DOWN
    assert w.evaluate(meta, 20.0, 0.99, 0.02).reason is Reason.WEAK_MOMENTUM
    w.fv = SimpleNamespace(p_up=0.993, z=2.6)                                             # z ok, but P < cost + margin
    ev = w.evaluate(meta, 20.0, 0.99, 0.02)
    assert ev.reason is Reason.EDGE_TOO_SMALL and ev.features["late_confidence"]["edge"] < 0
    w.fv = SimpleNamespace(p_up=0.9999, z=4.0)
    assert w.evaluate(meta, 20.0, 0.99, 0.02, spread=0.05).reason is Reason.LOW_LIQUIDITY  # spread 5c


def test_cash_and_one_position_per_market(monkeypatch):
    w = _World(monkeypatch)
    w.fv = SimpleNamespace(p_up=0.9999, z=4.0)
    meta = w.meta()
    ev = w.evaluate(meta, 20.0, 0.99, 0.02, balance=12.0)                                 # 20 x 0.99 > the cash
    assert ev.reason is None and ev.size == pytest.approx(math.floor(12.0 / 0.992 * 100) / 100)
    assert ev.features["late_confidence"]["size_reduced_from"] == 20.0
    assert w.evaluate(meta, 20.0, 0.99, 0.02, balance=4.0).reason is Reason.SIZE_ZERO      # under the venue minimum
    w.e.trades[meta.market.slug] = 1
    assert w.evaluate(meta, 20.0, 0.99, 0.02).reason is Reason.MARKET_ENTERED              # one position per market


def test_the_bot_builds_the_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("TPB_LATECONF_ENABLED", "1")
    from troll_poly_bot.live import LiveBot
    cfg = BotConfig()
    cfg.engine.strategy = "late_confidence"
    cfg.late_confidence = LateConfidenceConfig.from_env()
    cfg.deploy_mode = "paper"
    cfg.execution_scenario = "calibrated"
    bot = LiveBot(assets=("BTC", "ETH"), balance=40.0, cfg=cfg, state_path=str(tmp_path / "live_state.lc.json"),
                  trade_log=str(tmp_path / "live_trades.lc.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    assert type(bot.engine) is LateConfidenceEngine and bot.engine.scfg is cfg.late_confidence
    assert bot.risk.cfg.max_concurrent_positions == 2 and bot.risk.cfg.max_position_usdc <= 20.0
    assert bot.risk.cfg.max_total_exposure_usdc == pytest.approx(40.0)      # room for the two positions it allows
    # a 20-share order at 0.99 must fit the per-asset and per-window caps (the balance-scaled defaults refused it)
    assert bot.risk.cfg.max_asset_exposure_usdc >= 20 * 0.99 and bot.risk.cfg.max_epoch_exposure_usdc >= 2 * 20 * 0.99
    assert bot.risk.check("doge-updown-5m-1", "DOGE", 1, 19.8).ok
    assert bot.engine.snapshot()["strategy"] == "late_confidence"


def test_the_literal_spread_rule_can_never_pass_on_a_one_cent_book(monkeypatch):
    """ask 0.99 / bid 0.98 with the spread charged again: P would have to exceed 1.0 -- why it is off by default."""
    w = _World(monkeypatch, spread_in_edge=True)
    w.fv = SimpleNamespace(p_up=0.99999, z=6.0)
    assert w.evaluate(w.meta(), 20.0, 0.99, 0.02).reason is Reason.EDGE_TOO_SMALL


def test_the_limit_never_sits_under_a_fine_tick_ask(monkeypatch):
    """At 0.999 asks (the venue's 0.001 tick near the extremes) the limit is the ask, not 1 - the listed 0.01 tick;
    the 0.005 safety margin then refuses it on edge (at most 0.001 a share to earn), not on depth."""
    w = _World(monkeypatch)
    w.fv = SimpleNamespace(p_up=0.99999, z=6.0)
    ev = w.evaluate(w.meta(), 20.0, up_ask=0.999, dn_ask=0.002, spread=0.008)
    assert ev.reason is Reason.EDGE_TOO_SMALL and ev.features["late_confidence"]["limit"] == pytest.approx(0.999)
    w2 = _World(monkeypatch, safety_margin=0.0)
    w2.fv = SimpleNamespace(p_up=0.99999, z=6.0)
    ev = w2.evaluate(w2.meta(), 20.0, up_ask=0.999, dn_ask=0.002, spread=0.008)
    assert ev.reason is None and ev.limit_price == pytest.approx(0.999) and ev.size == 20.0
    ev = w2.evaluate(w2.meta("ETH"), 20.0, up_ask=0.993, dn_ask=0.008, spread=0.003)
    assert ev.limit_price == pytest.approx(0.995)                                   # ask + 0.2c


def test_the_models_cannot_price_answer_is_not_a_z_of_zero(monkeypatch):
    w = _World(monkeypatch)
    w.fv = SimpleNamespace(p_up=0.5, z=0.0, uncertainty=0.5)                 # twap_fair_value's refusal
    assert w.evaluate(w.meta(), 20.0, 0.99, 0.02).reason is Reason.MODEL_NOT_READY


def test_two_full_positions_fit_the_budget_at_a_40_usdc_bankroll():
    from troll_poly_bot.config import BotConfig
    cfg = BotConfig()
    cfg.scale_risk_to_balance(40.0)                          # the bot's own scaling (total 50 -> 20)
    sc = LateConfidenceConfig(enabled=True)
    sc.apply_budget(cfg.risk)
    rm = RiskManager(cfg=cfg.risk, starting_balance=40.0)
    assert cfg.risk.max_total_exposure_usdc == pytest.approx(40.0) and cfg.risk.max_concurrent_positions == 2
    assert rm.check("btc-updown-5m-1", "BTC", 1, 19.8).ok
    rm.on_submit("btc-updown-5m-1", usdc=19.8, asset="BTC", epoch=1)
    assert rm.check("eth-updown-5m-1", "ETH", 1, 19.8).ok     # the second position, same 5-minute window
