"""FASTCLIP (strategy/fastclip.py): the feature flag and the absence of any real-money switch,
its own config prefix, the trigger on the composite and on a leading exchange, the value and
edge gate, clip sizing, the same-side gap, both sides in one market, the per-market budget,
hold-to-settlement, the live bot's wiring -- and that the opt-in hooks it needs (adding to an
open position, a shorter per-market retry throttle) change nothing for the other strategies.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from troll_poly_bot.__main__ import check_fastclip
from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.live import ENTRY_RETRY_MS, LiveBot, LiveMarket
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy import fastclip as fc
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.fastclip import (
    FastClipConfig, FastClipEngine, lead_adjusted_p, norm_pdf_of_quantile, settle_sigma,
)
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce

T0 = 1_800_000_000_000.0
SIGMA = 1e-4
FEE = FeeSchedule()


def _cfg(**kw) -> FastClipConfig:
    base = dict(enabled=True, source="comp", assets="BTC,ETH")
    base.update(kw)
    return FastClipConfig(**base)


def _book(token: str, bid: float, ask: float, size: float = 100.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)])


class _Vol:
    ready = True
    sigma_per_sec = SIGMA


# ─────────────────────────────── flags ───────────────────────────────


def test_off_by_default_and_the_engine_refuses_to_exist_without_the_flag():
    assert FastClipConfig().enabled is False
    with pytest.raises(ValueError, match="TPB_FASTCLIP_ENABLED"):
        FastClipEngine(EngineConfig(strategy="fastclip"), FastClipConfig(), RiskManager(cfg=RiskConfig(), starting_balance=50.0))


def test_paper_needs_the_flag_and_real_money_is_refused_whatever_is_set():
    with pytest.raises(SystemExit, match="TPB_FASTCLIP_ENABLED"):
        check_fastclip("fastclip", "paper", env={})
    check_fastclip("fastclip", "paper", env={"TPB_FASTCLIP_ENABLED": "1"})
    for mode in ("micro-live", "live", "shadow"):
        with pytest.raises(SystemExit, match="paper only"):
            check_fastclip("fastclip", mode, env={"TPB_FASTCLIP_ENABLED": "1", "TPB_FASTCLIP_ALLOW_REAL": "1",
                                                  "TPB_SPOT_LAG_ALLOW_REAL": "1"})
    check_fastclip("spot_lag", "live", env={})


def test_its_own_env_prefix_leaves_spot_lag_alone():
    env = {"TPB_FASTCLIP_ENABLED": "1", "TPB_FASTCLIP_SOURCE": "comp", "TPB_FASTCLIP_K_SIGMA": "1.5",
           "TPB_FASTCLIP_CLIP_SHARES": "12", "TPB_SPOT_LAG_K_SIGMA": "3.5"}
    c = FastClipConfig.from_env(env)
    assert c.enabled and c.source == "comp" and c.k_sigma == 1.5 and c.clip_shares == 12.0
    assert c.hold_to_settlement is True and c.order_type == "FAK" and c.assets == "BTC"
    s = SpotLagConfig.from_env(env)
    assert s.enabled is False and s.k_sigma == 3.5


def test_an_unknown_trigger_source_is_refused():
    with pytest.raises(ValueError, match="source"):
        FastClipEngine(EngineConfig(strategy="fastclip"), _cfg(source="fastest"), RiskManager(cfg=RiskConfig(), starting_balance=50.0))


# ─────────────────────────────── the pieces ───────────────────────────────


def test_the_lead_adjustment_is_the_model_slope_times_the_unseen_move():
    assert norm_pdf_of_quantile(0.5) == pytest.approx(1 / math.sqrt(2 * math.pi), rel=1e-6)
    assert norm_pdf_of_quantile(0.8413447) == pytest.approx(math.exp(-0.5) / math.sqrt(2 * math.pi), rel=1e-4)
    s = settle_sigma(SIGMA, 280.0)
    assert s == pytest.approx(SIGMA * math.sqrt(240.0))
    assert settle_sigma(SIGMA, 30.0) == pytest.approx(SIGMA * math.sqrt(30.0 ** 3 / 10800.0))
    assert lead_adjusted_p(0.5, 3e-4, SIGMA, 280.0) == pytest.approx(0.5 + 3e-4 / math.sqrt(2 * math.pi) / s)
    assert lead_adjusted_p(0.5, -3e-4, SIGMA, 280.0) < 0.5 < lead_adjusted_p(0.5, 3e-4, SIGMA, 280.0)
    assert lead_adjusted_p(0.999, 1.0, SIGMA, 280.0) == 0.999               # clamped


# ─────────────────────────────── the engine ───────────────────────────────


class _World:
    """One engine; the model P(up) and the market's curve pinned (the curve is the identity), so
    value = side mid 1 s ago + (p now - p 1 s ago) [+ the lead adjustment]."""

    def __init__(self, monkeypatch, **scfg) -> None:
        rc = RiskConfig(max_position_usdc=7.0, max_total_exposure_usdc=100.0, max_concurrent_positions=4,
                        max_asset_exposure_usdc=100.0, max_epoch_exposure_usdc=100.0, max_daily_loss_usdc=40.0)
        self.scfg = _cfg(**scfg)
        self.scfg.apply_budget(rc)
        self.risk = RiskManager(cfg=rc, starting_balance=200.0)
        self.e = FastClipEngine(EngineConfig(strategy="fastclip"), self.scfg, self.risk, FeatureEngine())
        self.p_now = 0.5
        monkeypatch.setattr(fc, "twap_fair_value", lambda **kw: SimpleNamespace(p_up=self.p_now))
        monkeypatch.setattr(fc, "market_implied_fair", lambda p, left: p)

    def meta(self, asset: str = "BTC", open_ts: float = T0) -> MarketMeta:
        return MarketMeta(market=Market(condition_id=f"c-{asset}-{open_ts:.0f}", asset=asset,
                                        yes_token_id=f"{asset}-{open_ts:.0f}-UP", no_token_id=f"{asset}-{open_ts:.0f}-DN",
                                        strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                                        slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}", min_size=5.0),
                          resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)

    def spot(self, asset: str, t: float, price: float) -> None:
        self.e.features.update_spot(asset, price, t)

    def evaluate(self, meta, now, up_mid, spread=0.02, sources=None, depth=100.0, balance=200.0):
        m = meta.market
        view = CompositeView(m.asset, 100.0, 100.0, 3, 1.0, 1.0, 50.0, sources or {"binance": 100.0})
        h = spread / 2.0
        up = _book(m.yes_token_id, round(up_mid - h, 4), round(up_mid + h, 4), depth)
        dn = _book(m.no_token_id, round(1 - up_mid - h, 4), round(1 - up_mid + h, 4), depth)
        return self.e.evaluate(meta, up, dn, view, 200.0, 100.0, now, _Vol(), SimpleNamespace(ready=True), COLOCATED,
                               balance, quote_ages=None)

    def shock(self, meta, now, *, bps=3.0, up_mid_before=0.50, up_mid_now=0.51, p_before=0.50, p_after=0.60,
              direction=1, **kw):
        """Flat spot, one evaluation 1 s before (the value's reference), then a ``bps`` move in the
        last 400 ms and the model moved ``p_before`` -> ``p_after``; evaluated at ``now``."""
        asset = meta.market.asset
        t = now - 10_000.0
        while t < now - 400.0:
            self.spot(asset, t, 100.0)
            t += 100.0
        self.p_now = p_before
        self.evaluate(meta, now - 1000.0, up_mid_before, **kw)
        self.spot(asset, now - 200.0, 100.0 * math.exp(direction * bps * 0.5e-4))
        self.spot(asset, now, 100.0 * math.exp(direction * bps * 1e-4))
        self.p_now = p_after
        return self.evaluate(meta, now, up_mid_now, **kw)


def test_a_composite_move_buys_the_lagging_side_in_a_fak_clip(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta()
    now = T0 + 20_000.0
    ev = w.shock(meta, now)                                    # +3 bps in 400 ms: z ~ 4.2 over 500 ms
    assert ev.reason is None, (ev.reason, ev.detail)
    assert ev.side == "UP" and ev.token_id == meta.market.yes_token_id
    ask = 0.52
    value = 0.50 + (0.60 - 0.50)                               # mid 1 s ago + the curve's move
    f = ev.features["fastclip"]
    assert f["value"] == pytest.approx(value) and f["source"] == "composite"
    assert ev.net_edge == pytest.approx(value - ask - FEE.taker_fee_per_share(ask))
    assert ev.limit_price == pytest.approx(ask)                # limit_offset 0: never chases
    assert ev.size == pytest.approx(min(16.0, math.floor(7.0 / ask * 100) / 100))
    order = w.e.to_order(ev, now)
    assert order.tif is TimeInForce.IOC and order.price == pytest.approx(ask) and "fastclip" in order.tag


def test_a_falling_spot_buys_down(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta()
    now = T0 + 20_000.0
    ev = w.shock(meta, now, direction=-1, up_mid_before=0.50, up_mid_now=0.49, p_before=0.50, p_after=0.40)
    assert ev.reason is None and ev.side == "DOWN" and ev.token_id == meta.market.no_token_id
    assert ev.features["fastclip"]["value"] == pytest.approx(0.60)


def test_a_small_move_or_a_repriced_token_is_not_bought(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta()
    now = T0 + 20_000.0
    assert w.shock(meta, now, bps=0.5).reason is Reason.WEAK_MOMENTUM
    # the token has already moved to the value: no edge left after the fee
    w2 = _World(monkeypatch)
    ev = w2.shock(meta, now, up_mid_now=0.60)
    assert ev.reason is Reason.EDGE_TOO_SMALL


def test_the_lead_source_fires_on_one_exchange_before_the_composite(monkeypatch):
    now = T0 + 20_000.0
    for source, want in (("comp", Reason.WEAK_MOMENTUM), ("lead", None)):
        w = _World(monkeypatch, source=source)
        meta = w.meta()
        for t in range(int(now - 10_000), int(now - 400), 100):
            w.spot("BTC", t, 100.0)
        w.p_now = 0.5
        w.evaluate(meta, now - 1000.0, 0.50, sources={"binance": 100.0, "bybit": 100.0})
        w.evaluate(meta, now - 500.0, 0.50, sources={"binance": 100.0, "bybit": 100.0})
        w.spot("BTC", now, 100.0)                             # the composite has not moved
        ev = w.evaluate(meta, now, 0.51, sources={"binance": 100.0 * math.exp(3e-4), "bybit": 100.0})
        assert ev.reason is want, (source, ev.reason, ev.detail)
        if want is None:
            f = ev.features["fastclip"]
            assert f["source"] == "binance" and f["extra_bps"] == pytest.approx(3.0, abs=0.01)
            # the unseen 3 bps enter the value through the model's slope
            expect = 0.50 + (lead_adjusted_p(0.5, 3e-4, SIGMA, meta.market.seconds_remaining(now)) - 0.5)
            assert f["value"] == pytest.approx(expect, abs=1e-4)


def test_clips_on_the_same_side_are_paced_and_the_other_side_is_allowed(monkeypatch):
    w = _World(monkeypatch, same_side_gap_ms=1000.0)
    meta = w.meta()
    now = T0 + 20_000.0
    assert w.shock(meta, now).reason is None
    ev = w.evaluate(meta, now + 300.0, 0.51)                  # same side, 300 ms later
    assert ev.reason is Reason.COOLDOWN
    # the spot turns: DOWN is bought at once, both sides now in one market
    w.spot("BTC", now + 900.0, 100.0 * math.exp(0.0))
    w.spot("BTC", now + 1100.0, 100.0 * math.exp(-3e-4))
    w.p_now = 0.40
    ev = w.evaluate(meta, now + 1100.0, 0.50)
    assert ev.reason is None and ev.side == "DOWN"


def test_the_market_budget_and_the_order_count_bound_the_clips(monkeypatch):
    w = _World(monkeypatch, max_market_usdc=10.0, max_orders_per_market=3, same_side_gap_ms=0.0)
    meta = w.meta()
    now = T0 + 20_000.0
    ev = w.shock(meta, now)
    assert ev.reason is None
    w.risk.on_fill(meta.market.slug, "BTC", int(T0 // 1000), "UP", ev.size * 0.52, ev.size)   # $7 in the market
    ev2 = w.evaluate(meta, now + 100.0, 0.51)
    room = 10.0 - ev.size * 0.52                                              # about $3 left
    assert ev2.reason is None and ev2.size * ev2.limit_price <= room + 1e-9 and ev2.size >= 5.0
    w.risk.on_fill(meta.market.slug, "BTC", int(T0 // 1000), "UP", 2.6, 5.0)                    # $9.60 in
    assert w.evaluate(meta, now + 200.0, 0.51).reason is Reason.EXPOSURE_CAP     # a 5-share clip no longer fits
    w2 = _World(monkeypatch, max_orders_per_market=2, same_side_gap_ms=0.0)
    assert w2.shock(meta, now).reason is None
    assert w2.evaluate(meta, now + 100.0, 0.51).reason is None
    assert w2.evaluate(meta, now + 200.0, 0.51).reason is Reason.MARKET_ATTEMPTS


def test_the_band_the_spread_and_the_time_window(monkeypatch):
    now = T0 + 20_000.0
    w = _World(monkeypatch)
    assert w.shock(w.meta(), now, up_mid_before=0.94, up_mid_now=0.95, p_before=0.9, p_after=0.99).reason is Reason.PRICE_BAND
    w = _World(monkeypatch)
    assert w.shock(w.meta(), now, spread=0.05).reason is Reason.LOW_LIQUIDITY
    w = _World(monkeypatch)
    late = T0 + 250_000.0                                      # 50 s left
    assert w.shock(w.meta(), late).reason is Reason.OUTSIDE_TIME_WINDOW
    w = _World(monkeypatch)
    assert w.shock(w.meta("SOL"), now).reason is Reason.ASSET_FILTERED


def test_positions_are_never_sold_before_settlement(monkeypatch):
    w = _World(monkeypatch)
    for bid in (0.01, 0.5, 0.99):
        assert w.e.exit_decision(asset="BTC", side="UP", token_id="t", entry=0.4, bid=bid, now_ms=T0 + 9e5) is None
    assert w.e.mcfg.max_hold_s >= 1e8


# ─────────────────────────── the opt-in hooks ───────────────────────────


def test_the_risk_check_adds_to_an_open_market_only_when_asked():
    r = RiskManager(cfg=RiskConfig(max_position_usdc=10.0, max_total_exposure_usdc=100.0, max_concurrent_positions=1,
                                   max_asset_exposure_usdc=100.0, max_epoch_exposure_usdc=100.0), starting_balance=100.0)
    r.on_fill("m1", "BTC", 1, "UP", 5.0, 10.0)
    assert r.check("m1", "BTC", 1, 5.0).reason == "ALREADY_POSITIONED"           # the default: one per market
    assert r.check("m1", "BTC", 1, 5.0, add_to_open=True).ok                     # at the position cap: still fine
    assert not r.check("m2", "BTC", 2, 5.0, add_to_open=True).ok                  # a NEW market still counts
    r.on_fill("m1", "BTC", 1, "DOWN", 4.0, 10.0)
    assert r.open["m1"].usdc == pytest.approx(9.0)


def test_the_live_bot_runs_it_with_its_own_budget_and_hooks(tmp_path):
    cfg = BotConfig()
    cfg.engine.strategy = "fastclip"
    cfg.execution_scenario = "venue_lag"
    cfg.deploy_mode = "paper"
    cfg.fastclip = _cfg(assets="BTC")
    bot = LiveBot(assets=("BTC",), balance=200.0, cfg=cfg, state_path=str(tmp_path / "live_state.fc.json"),
                  trade_log=str(tmp_path / "live_trades.fc.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    assert type(bot.engine) is FastClipEngine and bot.engine.scfg is cfg.fastclip
    rc = bot.risk.cfg
    assert rc.max_asset_exposure_usdc == 40.0 and rc.max_position_usdc <= 7.0 and rc.max_concurrent_positions == 2
    assert bot.engine.snapshot()["strategy"] == "fastclip"
    lm = LiveMarket(meta=_World.meta(SimpleNamespace(), "BTC"))
    lm.last_entry_ms = T0 - 500.0
    assert bot._entry_throttled(lm, T0) is False                                  # noqa: SLF001 -- 250 ms throttle
    assert bot.engine.stacks_positions is True


def test_spot_lag_keeps_its_throttle_and_one_position_per_market(tmp_path, monkeypatch):
    monkeypatch.setenv("TPB_SPOT_LAG_ENABLED", "1")
    cfg = BotConfig()
    cfg.engine.strategy = "spot_lag"
    cfg.deploy_mode = "paper"
    cfg.spot_lag = SpotLagConfig(enabled=True)
    bot = LiveBot(assets=("BTC",), balance=50.0, cfg=cfg, state_path=str(tmp_path / "s.json"),
                  trade_log=str(tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    lm = LiveMarket(meta=_World.meta(SimpleNamespace(), "BTC"))
    lm.last_entry_ms = T0 - 500.0
    assert bot._entry_throttled(lm, T0) is True and ENTRY_RETRY_MS == 2000.0     # noqa: SLF001
    assert getattr(bot.engine, "stacks_positions", False) is False
