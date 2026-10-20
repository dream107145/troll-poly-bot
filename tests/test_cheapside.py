"""CHEAPSIDE (strategy/cheapside.py): the feature flag and the absence of any real-money
switch, its own config prefix, the cheap-side selection and price band, the model file
and its prediction, the spot-support and edge gates, one position per market (never
both sides), hold-to-settlement, and that the live bot runs it under its own name and
budget without touching spot-lag.
"""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from troll_poly_bot.__main__ import check_cheapside, check_spot_lag
from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.live import LiveBot
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy import cheapside as cs
from troll_poly_bot.strategy.cheapside import (
    FEATURES, CheapSideConfig, CheapSideEngine, CheapSideModel, cheap_side, features_for, supported,
)
from troll_poly_bot.strategy.engine import EngineConfig, Evaluation, Reason
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce

T0 = 1_800_000_000_000.0
SIGMA = 1e-4
FEE = FeeSchedule()


def _model_doc(**kw) -> dict:
    """A model whose raw score is exactly the standardised logit_ask plus a constant, so
    the probability it returns is known in closed form."""
    n = len(FEATURES)
    beta = [kw.get("intercept", 0.0)] + [0.0] * n
    beta[1 + FEATURES.index("logit_ask")] = kw.get("w_ask", 1.0)
    return {"features": list(FEATURES), "beta": beta, "mu": [0.0] * n, "sd": [1.0] * n,
            "platt": {"a": kw.get("a", 0.0), "b": kw.get("b", 1.0)}, "rules": {"min_edge": 0.05}}


@pytest.fixture
def model_path(tmp_path):
    p = tmp_path / "cheapside_model.json"
    p.write_text(json.dumps(_model_doc(intercept=1.0)))        # p = sigmoid(1 + logit(ask)): 0.30 -> 0.538
    return str(p)


def _cfg(model_path, **kw) -> CheapSideConfig:
    base = dict(enabled=True, model_path=model_path)
    base.update(kw)
    return CheapSideConfig(**base)


def _book(token: str, bid: float, ask: float, size: float = 100.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)])


class _Vol:
    ready = True
    sigma_per_sec = SIGMA


# ─────────────────────────────── flags ───────────────────────────────


def test_off_by_default_and_the_engine_refuses_to_exist_without_the_flag(model_path):
    assert CheapSideConfig().enabled is False
    risk = RiskManager(cfg=RiskConfig(), starting_balance=50.0)
    with pytest.raises(ValueError, match="TPB_CHEAPSIDE_ENABLED"):
        CheapSideEngine(EngineConfig(strategy="cheapside"), CheapSideConfig(model_path=model_path), risk, FeatureEngine())


def test_paper_needs_the_flag_and_real_money_is_refused_whatever_is_set():
    with pytest.raises(SystemExit, match="TPB_CHEAPSIDE_ENABLED"):
        check_cheapside("cheapside", "paper", env={})
    check_cheapside("cheapside", "paper", env={"TPB_CHEAPSIDE_ENABLED": "1"})
    for mode in ("micro-live", "live", "shadow"):
        with pytest.raises(SystemExit, match="paper only"):
            check_cheapside("cheapside", mode, env={"TPB_CHEAPSIDE_ENABLED": "1", "TPB_CHEAPSIDE_ALLOW_REAL": "1",
                                                    "TPB_SPOT_LAG_ALLOW_REAL": "1"})
    check_cheapside("spot_lag", "live", env={})                     # not this strategy: not its business


def test_its_own_env_prefix_and_flag_leave_spot_lag_alone():
    env = {"TPB_CHEAPSIDE_ENABLED": "1", "TPB_CHEAPSIDE_MAX_ASK": "0.30", "TPB_CHEAPSIDE_MIN_NET_EDGE": "0.07",
           "TPB_CHEAPSIDE_REQUIRE_SUPPORT": "0", "TPB_SPOT_LAG_MAX_ASK": "0.60"}
    c = CheapSideConfig.from_env(env)
    assert c.enabled and c.max_ask == 0.30 and c.min_net_edge == 0.07 and c.require_support is False
    assert c.hold_to_settlement is True and c.take_profit_cents == 0.0 and c.stop_loss_cents == 0.0
    s = SpotLagConfig.from_env(env)
    assert s.enabled is False and s.max_ask == 0.60 and s.min_net_edge == 0.02
    with pytest.raises(SystemExit):
        check_spot_lag("spot_lag", "paper", env=env)


# ─────────────────────────────── the pieces ───────────────────────────────


def test_cheap_side_is_the_lower_ask_and_a_tie_is_no_side():
    assert cheap_side(0.30, 0.72) == "UP" and cheap_side(0.72, 0.30) == "DOWN"
    assert cheap_side(0.50, 0.50) is None and cheap_side(None, 0.3) is None and cheap_side(0.3, None) is None


def test_features_are_signed_to_the_cheap_side_and_causal_in_form():
    kw = dict(ask=0.30, bid=0.28, depth=50.0, fair_side=0.45, left=250.0, sigma=SIGMA, spot=100.5, strike=100.0,
              r250=2e-4, r500=3e-4, r1000=4e-4, r3000=5e-4, tfi=0.3, obi=-0.2)
    up = features_for(direction=1, **kw)
    dn = features_for(direction=-1, **kw)
    assert up["z1000"] == pytest.approx(4.0) and dn["z1000"] == pytest.approx(-4.0)
    assert up["consist"] == 3.0 and dn["consist"] == 0.0
    assert up["dist"] > 0 > dn["dist"] and up["tfi"] == -dn["tfi"]
    assert up["spread_c"] == pytest.approx(2.0) and up["log_depth"] == pytest.approx(math.log1p(50.0))
    assert supported(up) and not supported(dn)
    assert set(up) == set(FEATURES)


def test_the_model_file_is_checked_and_predicts_in_closed_form(tmp_path):
    m = CheapSideModel(_model_doc(intercept=1.0, a=0.1, b=2.0))
    f = {k: 0.0 for k in FEATURES}
    f["logit_ask"] = math.log(0.3 / 0.7)
    assert m.predict(f) == pytest.approx(1 / (1 + math.exp(-(0.1 + 2.0 * (1.0 + math.log(0.3 / 0.7))))))
    bad = _model_doc()
    bad["features"] = list(FEATURES)[:-1]
    with pytest.raises(ValueError, match="features"):
        CheapSideModel(bad)
    bad = _model_doc()
    bad["beta"] = bad["beta"][:-1]
    with pytest.raises(ValueError, match="coefficient"):
        CheapSideModel(bad)


# ─────────────────────────────── the engine ───────────────────────────────


class _World:
    def __init__(self, monkeypatch, model_path, **scfg) -> None:
        rc = RiskConfig(max_position_usdc=5.0, max_total_exposure_usdc=50.0, max_concurrent_positions=10,
                        max_asset_exposure_usdc=50.0, max_epoch_exposure_usdc=50.0, max_daily_loss_usdc=10.0,
                        micro_fixed_size=True)
        self.scfg = _cfg(model_path, **scfg)
        self.scfg.apply_budget(rc)
        self.risk = RiskManager(cfg=rc, starting_balance=50.0)
        self.e = CheapSideEngine(EngineConfig(strategy="cheapside"), self.scfg, self.risk, FeatureEngine())
        self.p_up = 0.5
        monkeypatch.setattr(cs, "twap_fair_value", lambda **kw: SimpleNamespace(p_up=self.p_up))
        self.last_t: dict[str, float] = {}

    def meta(self, asset: str, open_ts: float = T0) -> MarketMeta:
        return MarketMeta(market=Market(condition_id=f"c-{asset}-{open_ts:.0f}", asset=asset,
                                        yes_token_id=f"{asset}-{open_ts:.0f}-UP", no_token_id=f"{asset}-{open_ts:.0f}-DN",
                                        strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                                        slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}", min_size=5.0),
                          resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)

    def feed(self, asset: str, until: float, bps_per_s: float = 0.0) -> None:
        """A spot path ending at ``until``: flat, or drifting ``bps_per_s`` in the last 3 s."""
        t = until - 10_000.0
        while t < until:
            t += 100.0
            drift = bps_per_s * max(0.0, (t - (until - 3_000.0)) / 1000.0)
            self.e.features.update_spot(asset, 100.0 * math.exp(drift * 1e-4), t)

    def evaluate(self, meta: MarketMeta, now: float, up_ask: float, dn_ask: float, spread: float = 0.02,
                 depth: float = 100.0, balance: float = 50.0, quote_ages=None):
        m = meta.market
        view = CompositeView(m.asset, 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
        up = _book(m.yes_token_id, round(up_ask - spread, 4), up_ask, depth)
        dn = _book(m.no_token_id, round(dn_ask - spread, 4), dn_ask, depth)
        return self.e.evaluate(meta, up, dn, view, 200.0, 100.0, now, _Vol(), SimpleNamespace(ready=True), COLOCATED,
                               balance, orderflow={"tfi15": 0.1, "obi": 0.0}, quote_ages=quote_ages)


def test_a_supported_underpriced_cheap_side_becomes_a_fok_at_ask_plus_1c(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    meta = w.meta("BTC")
    now = T0 + 20_000.0
    w.feed("BTC", now, bps_per_s=+2.0)                  # the spot is rising: UP is supported
    ev = w.evaluate(meta, now, up_ask=0.30, dn_ask=0.72)
    assert ev.reason is None, (ev.reason, ev.detail)
    assert ev.side == "UP" and ev.token_id == meta.market.yes_token_id
    assert ev.limit_price == pytest.approx(0.31) and ev.size == 10.0
    f = ev.features["cheapside"]
    assert f["side"] == "UP" and f["support"] is True and f["signal"] is True
    # p = sigmoid(1 + logit(0.30)) = 0.538; edge = p - 0.31 - fee(0.31) - 0.005
    p = 1 / (1 + math.exp(-(1.0 + math.log(0.3 / 0.7))))
    assert f["p_model"] == pytest.approx(round(p, 4))
    assert ev.net_edge == pytest.approx(p - 0.31 - FEE.taker_fee_per_share(0.31) - 0.005, abs=1e-6)
    order = w.e.to_order(ev, now)
    assert order.tif is TimeInForce.IOC and order.price == pytest.approx(0.31) and order.size == 10.0
    assert "cheapside" in order.tag


def test_the_cheap_side_is_down_when_down_is_cheaper_and_needs_a_falling_spot(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    meta = w.meta("ETH")
    now = T0 + 20_000.0
    w.feed("ETH", now, bps_per_s=+2.0)                  # rising: DOWN is not supported
    ev = w.evaluate(meta, now, up_ask=0.72, dn_ask=0.30)
    assert ev.reason is Reason.WEAK_MOMENTUM and ev.features["cheapside"]["side"] == "DOWN"
    w2 = _World(monkeypatch, model_path)
    w2.feed("ETH", now, bps_per_s=-2.0)                 # falling: DOWN is supported
    ev = w2.evaluate(meta, now, up_ask=0.72, dn_ask=0.30)
    assert ev.reason is None and ev.side == "DOWN" and ev.token_id == meta.market.no_token_id


def test_support_can_be_switched_off_but_the_edge_bar_cannot(monkeypatch, model_path):
    w = _World(monkeypatch, model_path, require_support=False)
    meta = w.meta("SOL")
    now = T0 + 20_000.0
    w.feed("SOL", now, bps_per_s=-2.0)                  # against UP, but support is off
    ev = w.evaluate(meta, now, up_ask=0.30, dn_ask=0.72)
    assert ev.reason is None
    # the model says 0.538 for an ask of 0.30 but only 0.60 for 0.35: edge 0.60 - 0.36 - fee - 0.005 < 0.05? no,
    # so push the bar instead
    w = _World(monkeypatch, model_path, require_support=False, min_net_edge=0.30)
    w.feed("SOL", now)
    ev = w.evaluate(meta, now, up_ask=0.30, dn_ask=0.72)
    assert ev.reason is Reason.EDGE_TOO_SMALL


def test_the_band_the_spread_and_the_depth_gate(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    meta = w.meta("XRP")
    now = T0 + 20_000.0
    w.feed("XRP", now, bps_per_s=+2.0)
    assert w.evaluate(meta, now, up_ask=0.40, dn_ask=0.62).reason is Reason.PRICE_BAND          # both above 0.35
    assert w.evaluate(meta, now, up_ask=0.04, dn_ask=0.97).reason is Reason.PRICE_BAND          # below 0.05
    assert w.evaluate(meta, now, up_ask=0.30, dn_ask=0.72, spread=0.05).reason is Reason.LOW_LIQUIDITY
    assert w.evaluate(meta, now, up_ask=0.30, dn_ask=0.72, depth=4.0).reason is Reason.LOW_LIQUIDITY
    assert w.evaluate(meta, now, up_ask=0.50, dn_ask=0.50).reason is Reason.NO_OFFER            # a tie
    assert w.evaluate(meta, now, up_ask=0.30, dn_ask=0.72,
                      quote_ages={meta.market.yes_token_id: 1500.0}).reason is Reason.STALE_DATA


def test_one_position_per_market_and_never_both_sides(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    meta = w.meta("BNB")
    now = T0 + 20_000.0
    w.feed("BNB", now, bps_per_s=+2.0)
    ev = w.evaluate(meta, now, up_ask=0.30, dn_ask=0.72)
    assert ev.reason is None
    w.e.on_entry_submit(meta.market.slug, now)
    w.e.on_entry_fill(meta.market.slug, meta.market.yes_token_id, now + 400.0)
    # later the other side is the cheap one and the spot supports it: still refused
    later = now + 30_000.0
    w.feed("BNB", later, bps_per_s=-2.0)
    ev2 = w.evaluate(meta, later, up_ask=0.72, dn_ask=0.30)
    assert ev2.reason is Reason.MARKET_ENTERED
    # another market of the same asset is its own opportunity
    meta2 = w.meta("BNB", open_ts=T0 + 300_000.0)
    ev3 = w.evaluate(meta2, later + 300_000.0, up_ask=0.72, dn_ask=0.30)
    assert ev3.reason is None or ev3.reason not in (Reason.MARKET_ENTERED, Reason.MARKET_ATTEMPTS)


def test_attempts_are_bounded_per_market(monkeypatch, model_path):
    w = _World(monkeypatch, model_path, max_attempts_per_market=2)
    meta = w.meta("DOGE")
    now = T0 + 20_000.0
    w.feed("DOGE", now, bps_per_s=+2.0)
    for _ in range(2):
        assert w.evaluate(meta, now, up_ask=0.30, dn_ask=0.72).reason is None
        w.e.on_entry_submit(meta.market.slug, now)
    assert w.evaluate(meta, now, up_ask=0.30, dn_ask=0.72).reason is Reason.MARKET_ATTEMPTS


def test_positions_are_never_sold_before_settlement(monkeypatch, model_path):
    w = _World(monkeypatch, model_path)
    for bid in (0.01, 0.30, 0.99):
        assert w.e.exit_decision(asset="BTC", side="UP", token_id="t", entry=0.30, bid=bid, now_ms=T0 + 9e5) is None
    assert w.e.mcfg.max_hold_s >= 1e8


def test_the_model_file_must_exist(tmp_path):
    risk = RiskManager(cfg=RiskConfig(), starting_balance=50.0)
    with pytest.raises(FileNotFoundError):
        CheapSideEngine(EngineConfig(strategy="cheapside"), _cfg(str(tmp_path / "missing.json")), risk, FeatureEngine())


# ─────────────────────────────── the live bot ───────────────────────────────


def test_the_live_bot_runs_it_under_its_own_name_and_budget(tmp_path, model_path):
    cfg = BotConfig()
    cfg.engine.strategy = "cheapside"
    cfg.execution_scenario = "venue_lag"
    cfg.deploy_mode = "paper"
    cfg.cheapside = _cfg(model_path)
    bot = LiveBot(assets=("BTC", "ETH"), balance=50.0, cfg=cfg, state_path=str(tmp_path / "live_state.cs.json"),
                  trade_log=str(tmp_path / "live_trades.cs.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    assert type(bot.engine) is CheapSideEngine and bot.engine.scfg is cfg.cheapside
    assert bot.risk.cfg.max_concurrent_positions == cfg.cheapside.max_simultaneous_positions
    assert bot.risk.cfg.max_position_usdc <= 5.0
    # Basket-capable Cheapside disables the generic total-exposure ceiling; paper cash binds.
    assert bot.risk.cfg.max_total_exposure_usdc >= cfg.cheapside.max_total_exposure_usdc
    snap = bot.engine.snapshot()
    assert snap["strategy"] == "cheapside" and snap["cheapside_model"]["path"] == model_path
    assert cfg.spot_lag.enabled is False                          # spot-lag's own flag and config untouched


def test_the_dashboard_cannot_lift_its_budget(tmp_path, model_path):
    from troll_poly_bot.control import ControlFile
    cfg = BotConfig()
    cfg.engine.strategy = "cheapside"
    cfg.execution_scenario = "venue_lag"
    cfg.deploy_mode = "paper"
    cfg.cheapside = _cfg(model_path)
    bot = LiveBot(assets=("BTC",), balance=50.0, cfg=cfg, state_path=str(tmp_path / "live_state.cs.json"),
                  trade_log=str(tmp_path / "live_trades.cs.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    (tmp_path / "controls.json").write_text(json.dumps({"paper": {"risk.max_daily_loss_usdc": 40.0}}))
    bot.controls = ControlFile(tmp_path / "controls.json")
    bot._apply_controls()                                                                 # noqa: SLF001
    assert bot.risk.cfg.max_daily_loss_usdc == 10.0


def test_the_order_type_is_honoured_fak_is_fill_and_kill(monkeypatch):
    """Cheapside always submits FAK (IOC) so thin cheap books can partial-fill."""
    import inspect
    src = inspect.getsource(CheapSideEngine.to_order)
    assert "TimeInForce.IOC" in src and "FOK" not in src.split("return Order")[-1]


def test_basket_pre_send_does_not_depend_on_spot_tick(tmp_path, model_path):
    cfg = BotConfig()
    cfg.engine.strategy = "cheapside"
    cfg.execution_scenario = "venue_lag"
    cfg.deploy_mode = "paper"
    cfg.cheapside = _cfg(model_path, portfolio_basket_enabled=True)
    bot = LiveBot(assets=("BTC",), balance=50.0, cfg=cfg,
                  state_path=str(tmp_path / "state.json"), trade_log=str(tmp_path / "trades.jsonl"),
                  earnings_path=str(tmp_path / "earnings.jsonl"), kill_file=str(tmp_path / "KILL"))
    meta = _World.__new__(_World).meta("BTC")
    from troll_poly_bot.live import LiveMarket
    from troll_poly_bot.strategy.engine import Evaluation
    ev = Evaluation(slug=meta.market.slug, asset="BTC", seconds_left=100.0, reason=None,
                    side="UP", token_id=meta.market.yes_token_id, limit_price=0.21,
                    expected_price=0.20, size=5.0,
                    features={"cheapside": {"basket_entry": True, "basket_low_buy": True}})
    failed, checks = bot._confirm_entry(ev, LiveMarket(meta=meta), T0 + 200_000.0, None, 20.0)
    assert failed is None, checks
    assert checks["SPOT_FRESH"] is None


def test_basket_leader_need_recovers_all_six_cheap_worst_case_costs(monkeypatch, model_path):
    w = _World(monkeypatch, model_path, portfolio_basket_enabled=True,
               portfolio_basket_min_leader_ask=0.55, portfolio_basket_max_leader_ask=0.78,
               portfolio_basket_abort_leader_ask=0.82,
               portfolio_basket_target_pnl_usdc=0.10)
    leader_meta = w.meta("XRP")
    leader_book = _book(leader_meta.market.yes_token_id, 0.68, 0.70)
    w.e.market_quotes[leader_meta.market.slug] = {
        "t": T0 + 100_000, "meta": leader_meta, "up": leader_book,
        "down": _book(leader_meta.market.no_token_id, 0.28, 0.30), "valid": True,
    }
    cheap_legs = []
    for asset in ("BTC", "ETH", "SOL", "DOGE", "BNB", "HYPE"):
        meta = w.meta(asset)
        cheap_legs.append({"slug": meta.market.slug, "leader": "UP", "side": "DOWN", "asset": asset})
        w.e.inventory[meta.market.slug] = {
            "DOWN": {"shares": 5.0, "cost": 1.0}, "UP": {"shares": 0.0, "cost": 0.0}}
    plan = {"cheap": cheap_legs,
            "leader": {"slug": leader_meta.market.slug, "side": "UP", "leader": "UP"}}
    need, leaders_win, pnl_after, why = w.e._basket_leader_need(plan, T0 + 100_000)
    assert need > 5.0
    assert pnl_after >= 0.10 - 1e-6
    assert "worst-case" in why
    assert leaders_win < 0.0  # before sizing, six cheap costs dominate


def test_basket_leader_revalidate_enforces_abort_band(monkeypatch, model_path):
    w = _World(monkeypatch, model_path, portfolio_basket_enabled=True,
               portfolio_basket_min_leader_ask=0.55, portfolio_basket_abort_leader_ask=0.82)
    ev = SimpleNamespace(features={"cheapside": {"basket_leader_buy": True}}, size=5.0,
                         limit_price=0.66, max_entry_price=0.66)
    assert w.e.revalidate(ev, _book("token", 0.64, 0.65)) is None
    assert w.e.revalidate(ev, _book("token", 0.82, 0.83)) is Reason.PRICE_BAND


def test_basket_leader_buy_is_ready_as_soon_as_six_cheap_legs_are_filled(monkeypatch, model_path):
    """After the six cheap fills, the next evaluation of ANY market must emit the
    leader recovery (live.py routes it to the favourite's book). A long cooldown
    or waiting for the favourite's own scan lets the ask run away."""
    assets = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
    w = _World(monkeypatch, model_path, portfolio_basket_enabled=True,
               portfolio_basket_market_count=7, portfolio_basket_buy_count=6,
               portfolio_basket_shares=5.0, portfolio_basket_max_average_ask=0.20,
               portfolio_basket_target_pnl_usdc=0.10, portfolio_repair_cooldown_s=2.0,
               portfolio_basket_min_leader_ask=0.55, portfolio_basket_max_leader_ask=0.78,
               portfolio_basket_abort_leader_ask=0.82,
               assets=",".join(assets), max_order_usdc=50.0)
    assert w.e.entry_retry_ms <= 250.0
    now = T0 + 100_000.0
    metas = {a: w.meta(a) for a in assets}
    # Six cheap DOWNs at ~0.10 and HYPE favourite UP at 0.70.
    for a in assets:
        meta = metas[a]
        if a == "HYPE":
            up, dn = _book(meta.market.yes_token_id, 0.68, 0.70), _book(meta.market.no_token_id, 0.28, 0.30)
        else:
            up, dn = _book(meta.market.yes_token_id, 0.88, 0.90), _book(meta.market.no_token_id, 0.08, 0.10)
        ages = {meta.market.yes_token_id: 50.0, meta.market.no_token_id: 50.0}
        w.e.market_quotes[meta.market.slug] = {
            "t": now, "meta": meta, "up": up, "down": dn, "valid": True, "quote_ages": ages}
        if a != "HYPE":
            w.e.inventory[meta.market.slug] = {"DOWN": {"shares": 5.0, "cost": 0.5}}
    plan = {
        "epoch": int(T0 // 1000), "created": now, "average_cheap_ask": 0.10,
        "armed_average_cheap_ask": 0.10, "complete": False,
        "cheap": [{"slug": metas[a].market.slug, "side": "DOWN", "cheap": "DOWN",
                   "leader": "UP", "asset": a} for a in assets if a != "HYPE"],
        "leader": {"slug": metas["HYPE"].market.slug, "side": "UP", "cheap": "DOWN",
                   "leader": "UP", "asset": "HYPE"},
    }
    w.e.basket_plans[str(int(T0 // 1000))] = plan
    # Scan BTC (a cheap market), not HYPE: the leader order must still come out
    # immediately, with no 2 s repair cooldown on the first attempt.
    btc = metas["BTC"]
    ev = w.e._portfolio_basket(
        Evaluation(slug=btc.market.slug, asset="BTC", seconds_left=200.0, reason=None),
        btc, w.e.market_quotes[btc.market.slug]["up"], w.e.market_quotes[btc.market.slug]["down"],
        now, balance=100.0, book_age_ms=50.0,
        quote_ages=w.e.market_quotes[btc.market.slug]["quote_ages"])
    assert ev is not None, w.e.last_cheapside
    assert ev.slug == metas["HYPE"].market.slug and ev.side == "UP"
    assert ev.features["cheapside"].get("basket_leader_buy")
    assert ev.size >= 5.0
