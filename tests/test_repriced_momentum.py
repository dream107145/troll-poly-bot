"""REPRICED_MOMENTUM (strategy/repriced_momentum.py): the repricing arithmetic for both
tokens, the momentum features, fair value and overshoot, the expected-continuation
table and the edge gate, stale books, the limit order and what FOK means for it
(quote disappearance, no partial fills, cancellation before sending), the exits,
multi-market isolation, the budgets and global limits, the kill switch, strategy
identification and P&L accounting -- and that spot-lag is untouched.

Engine tests drive the real engine through spot paths fed into its FeatureEngine;
the model probability and the market's curve are pinned (``_World``) so the fair value
is known exactly. Live tests run the real LiveBot loop in paper mode.
"""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

import troll_poly_bot.strategy.repriced_momentum as rm
from troll_poly_bot.__main__ import check_repriced_momentum, check_spot_lag
from troll_poly_bot.backtest.spot_shock_replay import Execution, Window, trade_pnl
from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.live import LiveBot, LiveBook, LiveMarket
from troll_poly_bot.pricing.twap import TwapState
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.engine import EngineConfig, Evaluation, Reason, SideView
from troll_poly_bot.strategy.momentum import calculate_net_edge, exit_cost_estimate
from troll_poly_bot.strategy.repriced_momentum import (
    CONTINUATION_C, RepricedMomentumConfig, RepricedMomentumEngine, bucket, exit_reason, expected_continuation,
    reject_code, repricing, round_trip_cost, side_price,
)
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.types import BookLevel, Market, OrderBook, Side, TimeInForce

import numpy as np

T0 = 1_800_000_000_000.0             # a window open, ms
SIGMA = 1e-4                         # 1 bp per second
FEE = FeeSchedule()


class _Vol:
    ready = True
    sigma_per_sec = SIGMA


def _cfg(**kw) -> RepricedMomentumConfig:
    base = dict(enabled=True, edge_gate=False, max_overshoot_cents=5.0)
    base.update(kw)
    return RepricedMomentumConfig(**base)


def _book(token: str, bid: float, ask: float, size: float = 100.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)])


# ─────────────────────────────── the pure pieces ───────────────────────────────


def test_repricing_percentage_of_the_up_token():
    # the market's curve says the Up token should go 0.50 -> 0.60; it went to 0.57
    expected, actual, pct = repricing(0.50, 0.57, 0.50, 0.60, +1)
    assert expected == pytest.approx(0.10) and actual == pytest.approx(0.07) and pct == pytest.approx(0.70)


def test_the_down_token_is_normalised_so_positive_is_the_shocks_direction():
    # a DOWN shock: Up fair 0.50 -> 0.40, Up mid 0.50 -> 0.43; the DOWN token rose 0.50 -> 0.57
    expected, actual, pct = repricing(0.50, 0.43, 0.50, 0.40, -1)
    assert expected == pytest.approx(0.10) and actual == pytest.approx(0.07) and pct == pytest.approx(0.70)
    assert side_price(0.43, -1) == pytest.approx(0.57) and side_price(0.43, +1) == pytest.approx(0.43)
    # a token that moved AGAINST the shock is a negative repricing, not a positive one
    assert repricing(0.50, 0.52, 0.50, 0.40, -1)[2] == pytest.approx(-0.2)


@pytest.mark.parametrize("args", [
    (0.50, 0.57, 0.50, 0.505, +1),          # expected move under 1c: a near-zero denominator
    (0.50, 0.57, 0.50, 0.45, +1),           # the curve moved against the shock
    (None, 0.57, 0.50, 0.60, +1),           # a missing token price
    (0.50, float("nan"), 0.50, 0.60, +1),   # not a number
    (0.50, 0.57, 0.50, 1.20, +1),           # an impossible fair value
])
def test_invalid_repricing_cannot_be_calculated(args):
    assert repricing(*args)[2] is None


def test_expected_continuation_comes_from_the_training_table():
    # 70-80 % repriced, strong spot (>= 1 sigma), 8 s: the table's cell (3, 3), horizon index 6
    assert expected_continuation(0.75, 1.4, 8.0) == pytest.approx(CONTINUATION_C[(3, 3)][6] / 100.0)
    # the nearest horizon: 2.2 s -> 2 s
    assert expected_continuation(0.75, 1.4, 2.2) == pytest.approx(CONTINUATION_C[(3, 3)][3] / 100.0)
    # bucket edges are [lo, hi)
    assert bucket(0.50, rm.PCT_BUCKETS) == 1 and bucket(0.4999, rm.PCT_BUCKETS) == 0
    # outside the table (under 25 % repriced) there is no estimate
    assert expected_continuation(0.10, 1.4, 8.0) is None and expected_continuation(None, 1.0, 8.0) is None


def test_round_trip_cost_is_what_the_cost_model_charges():
    ask, bid, cont = 0.58, 0.56, 0.01
    mid = (ask + bid) / 2
    cb = calculate_net_edge(mid + cont, ask, bid, FEE, _cfg().momentum_mirror(), 0.0, 0.0,
                            exit_cost_estimate(mid + cont, ask - bid, FEE))
    assert cb.net_edge == pytest.approx(cont - round_trip_cost(ask, bid, cont, FEE, 0.005))
    # spread + two taker fees + slippage: about 4.4c at these prices, far above a 1c continuation
    assert round_trip_cost(ask, bid, cont, FEE, 0.005) > 0.04


def test_exit_rules():
    # timer
    assert exit_reason("timer", 1.9, 2.0, 8.0, -3.0, -0.05, 0.9, 0.5) is None
    assert exit_reason("timer", 2.0, 2.0, 8.0, None, None, None, None) == "TIME_STOP"
    # momentum reversal: the spot's or the token's own 500 ms move turns against us
    assert exit_reason("reversal", 1.0, 2.0, 8.0, -0.1, +0.01, 0.5, 0.6) == "SPOT_REVERSAL"
    assert exit_reason("reversal", 1.0, 2.0, 8.0, +0.5, -0.01, 0.5, 0.6) == "TOKEN_REVERSAL"
    assert exit_reason("reversal", 1.0, 2.0, 8.0, +0.5, +0.01, 0.7, 0.6) is None      # continuing: hold
    # the fair value reached
    assert exit_reason("fair", 1.0, 2.0, 8.0, -1.0, -1.0, 0.60, 0.60) == "FAIR_REACHED"
    assert exit_reason("any", 1.0, 2.0, 8.0, +1.0, +0.01, 0.61, 0.60) == "FAIR_REACHED"
    # the maximum hold always ends it; a minimum hold delays the events
    assert exit_reason("reversal", 8.0, 2.0, 8.0, +1.0, +0.01, 0.5, 0.6) == "TIME_STOP"
    assert exit_reason("reversal", 0.3, 2.0, 8.0, -1.0, -0.01, 0.5, 0.6, min_hold_s=0.5) is None


def test_reject_codes_are_the_briefs_names():
    assert reject_code(Reason.REPRICING_TOO_LOW) == "REJECT_REPRICING_TOO_LOW"
    assert reject_code(Reason.REPRICING_TOO_HIGH) == "REJECT_REPRICING_TOO_HIGH"
    assert reject_code(Reason.SPOT_MOMENTUM_WEAK) == "REJECT_SPOT_MOMENTUM_WEAK"
    assert reject_code(Reason.DIRECTION_MISMATCH) == "REJECT_DIRECTION_MISMATCH"
    assert reject_code(Reason.OVERSHOOT) == "REJECT_OVERSHOOT"
    assert reject_code(Reason.EDGE_TOO_SMALL) == "REJECT_EDGE_TOO_LOW"
    assert reject_code(Reason.STALE_DATA) == "REJECT_STALE_BOOK"
    assert reject_code(Reason.RISK_LIMIT) == reject_code(Reason.EXPOSURE_CAP) == "REJECT_RISK_LIMIT"
    assert reject_code(None) == "ACCEPT"


# ─────────────────────────────── flags and config ───────────────────────────────


def test_off_by_default_and_the_engine_refuses_to_exist_without_the_flag():
    assert RepricedMomentumConfig().enabled is False
    assert RepricedMomentumConfig.from_env({}).enabled is False
    rc = RiskConfig()
    with pytest.raises(ValueError, match="TPB_REPRICED_MOM_ENABLED"):
        RepricedMomentumEngine(EngineConfig(strategy="repriced_momentum"), RepricedMomentumConfig(), RiskManager(rc, 50.0))
    with pytest.raises(ValueError, match="exit_mode"):
        RepricedMomentumEngine(EngineConfig(), _cfg(exit_mode="sometimes"), RiskManager(rc, 50.0))


def test_real_money_needs_a_second_explicit_flag():
    with pytest.raises(SystemExit, match="TPB_REPRICED_MOM_ENABLED"):
        check_repriced_momentum("repriced_momentum", "paper", {})
    check_repriced_momentum("repriced_momentum", "paper", {"TPB_REPRICED_MOM_ENABLED": "1"})
    check_repriced_momentum("repriced_momentum", "shadow", {"TPB_REPRICED_MOM_ENABLED": "1"})
    for mode in ("micro-live", "live"):
        with pytest.raises(SystemExit, match="TPB_REPRICED_MOM_ALLOW_REAL"):
            check_repriced_momentum("repriced_momentum", mode, {"TPB_REPRICED_MOM_ENABLED": "1"})
    # spot-lag's own flags do not unlock it, and it does not unlock spot-lag
    with pytest.raises(SystemExit):
        check_repriced_momentum("repriced_momentum", "micro-live",
                                {"TPB_REPRICED_MOM_ENABLED": "1", "TPB_SPOT_LAG_ALLOW_REAL": "1"})
    with pytest.raises(SystemExit):
        check_spot_lag("spot_lag", "paper", {"TPB_REPRICED_MOM_ENABLED": "1"})


def test_the_two_strategies_read_separate_environments_and_budgets():
    env = {"TPB_REPRICED_MOM_ENABLED": "1", "TPB_REPRICED_MOM_MIN_PCT": "0.25", "TPB_REPRICED_MOM_MAX_ORDER_USDC": "2",
           "TPB_SPOT_LAG_HOLD_S": "9", "TPB_SPOT_LAG_ENABLED": "1"}
    mom, lag = RepricedMomentumConfig.from_env(env), SpotLagConfig.from_env(env)
    assert mom.enabled and mom.min_pct == 0.25 and mom.max_order_usdc == 2.0 and mom.hold_s == 2.0
    assert lag.enabled and lag.hold_s == 9.0 and not hasattr(lag, "min_pct")
    # the brief's first configuration and the momentum budget are the defaults
    d = RepricedMomentumConfig()
    assert (d.min_pct, d.max_pct, d.max_overshoot_cents, d.min_edge_cents) == (0.50, 0.90, 1.0, 1.0)
    assert (d.max_simultaneous_positions, d.max_total_exposure_usdc, d.max_order_usdc, d.max_daily_loss_usdc) == \
        (1, 5.0, 3.0, 5.0)
    assert (d.exit_mode, d.max_hold_s) == ("reversal", 8.0)


def test_spot_lag_is_unchanged():
    s = SpotLagConfig()
    assert (s.k_sigma, s.k3_sigma, s.min_net_edge, s.hold_s, s.extend_z1, s.extend_max_hold_s, s.max_order_usdc,
            s.max_total_exposure_usdc, s.max_simultaneous_positions, s.exclude_assets) == \
        (3.0, 0.5, 0.02, 5.0, 0.5, 10.0, 5.0, 10.0, 4, "HYPE")
    assert "min_pct" not in SpotLagConfig.__dataclass_fields__
    cfg = BotConfig()
    cfg.engine.strategy = "spot_lag"
    assert type(cfg.spot_lag) is SpotLagConfig and type(cfg.repriced_momentum) is RepricedMomentumConfig


# ─────────────────────────────── the engine ───────────────────────────────


class _World:
    """One engine; spot paths fed into its FeatureEngine; the model probability (``p``)
    and the market's curve pinned so fair = side mid before + (p now - p before)."""

    def __init__(self, monkeypatch, scfg: RepricedMomentumConfig | None = None,
                 risk_cfg: RiskConfig | None = None) -> None:
        rc = risk_cfg or RiskConfig(max_position_usdc=5.0, max_total_exposure_usdc=50.0, max_concurrent_positions=10,
                                    max_asset_exposure_usdc=50.0, max_epoch_exposure_usdc=50.0,
                                    max_daily_loss_usdc=10.0, micro_fixed_size=True)
        self.scfg = scfg or _cfg()
        self.scfg.apply_budget(rc)
        self.risk = RiskManager(cfg=rc, starting_balance=50.0)
        self.e = RepricedMomentumEngine(EngineConfig(strategy="repriced_momentum"), self.scfg, self.risk,
                                        FeatureEngine())
        self.p: dict[str, float] = {}
        monkeypatch.setattr(rm, "twap_fair_value", lambda **kw: SimpleNamespace(p_up=self.p_now))
        monkeypatch.setattr(rm, "market_implied_fair", lambda p, left: p)
        self.p_now = 0.5
        self.last_t: dict[str, float] = {}

    def meta(self, asset: str, open_ts: float = T0) -> MarketMeta:
        return MarketMeta(market=Market(condition_id=f"c-{asset}-{open_ts:.0f}", asset=asset,
                                        yes_token_id=f"{asset}-{open_ts:.0f}-UP", no_token_id=f"{asset}-{open_ts:.0f}-DN",
                                        strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                                        slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}", min_size=5.0),
                          resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)

    def feed(self, asset: str, until: float, bps_at) -> None:
        t = self.last_t.get(asset, until - 70_000.0)
        while t < until:
            t += 100.0
            self.e.features.update_spot(asset, 100.0 * math.exp(bps_at(t) * 1e-4), t)
        self.last_t[asset] = t

    def evaluate(self, meta: MarketMeta, now: float, up_mid: float, spread: float = 0.02, quote_ages=None,
                 depth: float = 100.0, balance: float = 50.0) -> Evaluation:
        m = meta.market
        view = CompositeView(m.asset, 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
        h = spread / 2.0
        up = _book(m.yes_token_id, round(up_mid - h, 4), round(up_mid + h, 4), depth)
        dn = _book(m.no_token_id, round(1 - up_mid - h, 4), round(1 - up_mid + h, 4), depth)
        return self.e.evaluate(meta, up, dn, view, 200.0, 100.0, now, _Vol(), SimpleNamespace(ready=True), COLOCATED,
                               balance, quote_ages=quote_ages)

    def run(self, meta: MarketMeta, t_shock: float, t_end: float, *, direction: int = 1, jump_bps: float = 6.0,
            after_bps_s: float = 3.0, p_after: float = 0.60, token=None, **kw) -> list[tuple[float, Evaluation]]:
        """Flat until ``t_shock`` - 800 ms, then a ``jump_bps`` spot move by ``t_shock`` that
        keeps going at ``after_bps_s``; the model moves 0.50 -> ``p_after`` (Up) with it;
        the Up token's mid follows ``token(t)``. Evaluates every 200 ms from 3 s before."""
        d = direction
        start = t_shock - 800.0

        def bps(t):
            if t < start:
                return 0.0
            if t <= t_shock:
                return d * jump_bps * (t - start) / 800.0
            return d * (jump_bps + after_bps_s * (t - t_shock) / 1000.0)
        token = token or (lambda t: 0.50)
        out = []
        t = t_shock - 3000.0
        while t <= t_end:
            self.feed(meta.market.asset, t, bps)
            self.p_now = 0.50 if t < start else p_after
            out.append((t, self.evaluate(meta, t, token(t), **kw)))
            t += 200.0
        return out


def _ramp(t_shock, frm=0.50, to=0.57, over_ms=600.0, then_per_s=0.01):
    """The Up token's mid: flat, then repricing ``frm`` -> ``to`` over ``over_ms`` after the
    shock, then drifting on at ``then_per_s``."""
    def f(t):
        if t <= t_shock:
            return frm
        if t <= t_shock + over_ms:
            return frm + (to - frm) * (t - t_shock) / over_ms
        return to + then_per_s * (t - t_shock - over_ms) / 1000.0
    return f


def _cands(evs):
    return [(t, ev) for t, ev in evs if (ev.features or {}).get("repriced_momentum")]


def _first_trade(evs):
    return next(((t, ev) for t, ev in evs if ev.tradeable), (None, None))


def test_a_repriced_token_with_continuing_momentum_is_bought_at_the_ask(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta("BTC")
    ts = T0 + 100_000.0
    evs = w.run(meta, ts, ts + 2_000.0, token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02))
    t, ev = _first_trade(evs)
    assert ev is not None
    c = ev.features["repriced_momentum"]
    assert c["direction"] == "UP" and ev.side == "UP" and ev.token_id == meta.market.yes_token_id
    assert 0.50 <= c["repricing_pct"] <= 0.90
    assert c["fair_value"] == pytest.approx(0.60)               # side mid before + expected move
    assert c["expected_repricing"] == pytest.approx(0.10)
    assert c["direction_agreement"] and c["momentum_score"] > 0 and c["token_return_500ms"] > 0
    for k in ("spot_return_100ms", "spot_return_250ms", "spot_return_500ms", "spot_return_1s", "spot_return_2s",
              "token_return_100ms", "token_return_250ms", "token_return_1s", "spot_acceleration",
              "token_acceleration", "time_since_shock_s", "spread", "expected_slippage"):
        assert k in c
    # the limit IS the decision ask: never a market order, never chasing
    order = w.e.to_order(ev, t)
    assert order.side is Side.BUY and order.tif is TimeInForce.FOK
    assert order.price == pytest.approx(c["ask"]) and ev.limit_price == pytest.approx(c["ask"])
    assert "repriced_momentum" in order.tag


def test_momentum_calculation_is_sigma_scaled_and_signed_by_the_shock(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta("BTC")
    ts = T0 + 100_000.0
    evs = w.run(meta, ts, ts + 1_000.0, after_bps_s=3.0, token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02))
    t, ev = next((t, ev) for t, ev in _cands(evs) if t >= ts + 800.0)
    c = ev.features["repriced_momentum"]
    # the spot moves 3 bp/s after the shock: 1.5 bp in 500 ms; sigma 1 bp/s -> 1.5 / sqrt(0.5)
    assert c["spot_z_500ms"] == pytest.approx(1.5 / math.sqrt(0.5), rel=0.05)
    assert c["momentum_score"] == pytest.approx(c["spot_z_500ms"])
    assert c["spot_return_500ms"] == pytest.approx(1.5, rel=0.05)           # in bps
    # the same shock DOWN: the spot falls, and the normalised scores are still positive
    w2 = _World(monkeypatch)
    evs2 = w2.run(w2.meta("ETH"), ts, ts + 1_000.0, direction=-1, after_bps_s=3.0, p_after=0.40,
                  token=_ramp(ts, frm=0.50, to=0.43, over_ms=800.0, then_per_s=-0.02))
    c2 = next(ev for t, ev in _cands(evs2) if t >= ts + 800.0).features["repriced_momentum"]
    assert c2["direction"] == "DOWN" and c2["spot_z_500ms"] == pytest.approx(c["spot_z_500ms"], rel=0.05)
    assert c2["spot_return_500ms"] < 0 < c2["token_return_500ms"]


def test_a_down_shock_buys_the_down_token(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta("SOL")
    ts = T0 + 100_000.0
    up_mid = _ramp(ts, frm=0.50, to=0.43, over_ms=800.0, then_per_s=-0.02)
    evs = w.run(meta, ts, ts + 2_000.0, direction=-1, p_after=0.40, token=up_mid)
    t, ev = _first_trade(evs)
    assert ev is not None and ev.side == "DOWN" and ev.token_id == meta.market.no_token_id
    c = ev.features["repriced_momentum"]
    # the DOWN token: fair 0.50 + 0.10, priced at 1 - the Up mid, bought at the DOWN book's ask
    assert c["fair_value"] == pytest.approx(0.60) and 0.5 <= c["repricing_pct"] <= 0.9
    assert c["token_price"] == pytest.approx(1 - up_mid(t)) and c["token_price"] > 0.5
    assert ev.limit_price == pytest.approx(round(1 - up_mid(t) + 0.01, 4))


def _reasons(evs):
    return [ev.features["repriced_momentum"]["reason"] for _, ev in _cands(evs)]


def test_repricing_outside_the_zone_is_rejected_with_its_reason(monkeypatch):
    ts = T0 + 100_000.0
    # the token barely moves: 20 % repriced
    w = _World(monkeypatch)
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, token=_ramp(ts, to=0.52, then_per_s=0.0))
    assert "REJECT_REPRICING_TOO_LOW" in _reasons(evs) and not any(ev.tradeable for _, ev in evs)
    # the token is already past fair: 110 %
    w = _World(monkeypatch)
    evs = w.run(w.meta("ETH"), ts, ts + 2_000.0, token=_ramp(ts, to=0.61, over_ms=200.0, then_per_s=0.0))
    assert "REJECT_REPRICING_TOO_HIGH" in _reasons(evs) and not any(ev.tradeable for _, ev in evs)


def test_a_spot_that_turned_or_a_token_that_stopped_following_is_a_direction_mismatch(monkeypatch):
    ts = T0 + 100_000.0
    w = _World(monkeypatch)                         # the spot reverses right after the shock
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, after_bps_s=-4.0,
                token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02))
    late = [ev.features["repriced_momentum"] for t, ev in _cands(evs) if t >= ts + 600.0]
    assert late and all(c["reason"] != "ACCEPT" for c in late)
    assert any(c["reason"] == "REJECT_DIRECTION_MISMATCH" and "spot" in c["detail"] for c in late)
    w = _World(monkeypatch)                         # the token stalls while the spot runs on
    evs = w.run(w.meta("ETH"), ts, ts + 2_500.0, token=_ramp(ts, to=0.57, over_ms=400.0, then_per_s=0.0))
    late = [ev.features["repriced_momentum"] for t, ev in _cands(evs) if t >= ts + 1_000.0]
    assert late and all(c["reason"] == "REJECT_DIRECTION_MISMATCH" and "token" in c["detail"] for c in late)


def test_weak_spot_momentum_is_rejected(monkeypatch):
    w = _World(monkeypatch, _cfg(min_spot_z=5.0))
    ts = T0 + 100_000.0
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02))
    assert "REJECT_SPOT_MOMENTUM_WEAK" in _reasons(evs) and not any(ev.tradeable for _, ev in evs)


def test_overshoot_past_fair_is_rejected(monkeypatch):
    # 95 % repriced at the mid; a 3c spread puts the ask 1c past the 0.60 fair
    w = _World(monkeypatch, _cfg(max_overshoot_cents=0.5, max_pct=1.0))
    ts = T0 + 100_000.0
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, token=_ramp(ts, to=0.595, over_ms=800.0, then_per_s=0.001),
                spread=0.03)
    cs = [ev.features["repriced_momentum"] for _, ev in _cands(evs)]
    over = [c for c in cs if c["reason"] == "REJECT_OVERSHOOT"]
    assert over and all(c["overshoot"] > 0.005 for c in over)
    # on the way up the ask was still under fair + 0.5c and could trade; past it, never
    assert all(ev.features["repriced_momentum"]["overshoot"] <= 0.005 + 1e-9 for _, ev in evs if ev.tradeable)
    # the same book with 2c allowed trades
    w = _World(monkeypatch, _cfg(max_overshoot_cents=2.0, max_pct=1.0))
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, token=_ramp(ts, to=0.595, over_ms=800.0, then_per_s=0.001),
                spread=0.03)
    assert any(ev.tradeable for _, ev in evs)


def test_the_edge_gate_wants_the_continuation_to_beat_the_round_trip(monkeypatch):
    ts = T0 + 100_000.0
    tok = _ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02)
    # the default gate: the training continuation (~1c) never covers ~4-5c of spread and fees
    w = _World(monkeypatch, _cfg(edge_gate=True, min_edge_cents=1.0))
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, token=tok)
    cs = [ev.features["repriced_momentum"] for _, ev in _cands(evs)]
    gated = [c for c in cs if c["reason"] == "REJECT_EDGE_TOO_LOW"]
    assert gated and not any(ev.tradeable for _, ev in evs)
    c = gated[0]
    assert c["expected_edge"] == pytest.approx(c["expected_continuation"] - c["round_trip_cost"], abs=1e-3)
    # a gate below the edge lets the same candidate through, and the edge rides on the evaluation
    w = _World(monkeypatch, _cfg(edge_gate=True, min_edge_cents=-10.0))
    t, ev = _first_trade(w.run(w.meta("BTC"), ts, ts + 2_000.0, token=tok))
    assert ev is not None and ev.net_edge == pytest.approx(ev.features["repriced_momentum"]["expected_edge"], abs=1e-4)


def test_a_stale_book_is_rejected(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta("BTC")
    ts = T0 + 100_000.0
    old = {meta.market.yes_token_id: 900.0, meta.market.no_token_id: 900.0}
    evs = w.run(meta, ts, ts + 2_000.0, token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02), quote_ages=old)
    assert set(_reasons(evs)) == {"REJECT_STALE_BOOK"} and not any(ev.tradeable for _, ev in evs)


def test_without_a_shock_there_is_no_candidate_and_the_window_expires(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta("BTC")
    ts = T0 + 100_000.0
    evs = w.run(meta, ts, ts + 8_000.0, jump_bps=1.0, after_bps_s=0.0, token=_ramp(ts))   # 1 sigma is no shock
    assert not _cands(evs) and all(ev.reason is Reason.WEAK_MOMENTUM for _, ev in evs[-5:])
    w = _World(monkeypatch)
    evs = w.run(meta, ts, ts + 8_000.0, after_bps_s=1.0, token=_ramp(ts, to=0.52, then_per_s=0.0))
    last_cand = max(t for t, _ in _cands(evs))
    assert last_cand <= ts + 5_200.0                                        # 5 s after the shock, no more
    assert evs[-1][1].detail == "no active shock"


def test_the_limit_never_chases_unless_configured(monkeypatch):
    ts = T0 + 100_000.0
    tok = _ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02)
    w = _World(monkeypatch, _cfg(limit_offset_cents=1.0))
    t, ev = _first_trade(w.run(w.meta("BTC"), ts, ts + 2_000.0, token=tok))
    assert ev.limit_price == pytest.approx(ev.features["repriced_momentum"]["ask"] + 0.01)


def test_quote_disappearance_before_sending_cancels_the_entry(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta("BTC")
    ts = T0 + 100_000.0
    t, ev = _first_trade(w.run(meta, ts, ts + 2_000.0, token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02)))
    tok = ev.token_id
    ask = ev.limit_price
    assert w.e.revalidate(ev, _book(tok, ask - 0.02, ask)) is None
    # the ask moved up before the order left: not chased, not sent
    assert w.e.revalidate(ev, _book(tok, ask, ask + 0.01)) is Reason.HIGH_SLIPPAGE
    # the quote is gone
    assert w.e.revalidate(ev, OrderBook(tok, bids=[BookLevel(ask - 0.02, 100)], asks=[])) is Reason.NO_OFFER
    # FOK is all-or-nothing: 3 shares at the limit cannot fill 5
    thin = OrderBook(tok, bids=[BookLevel(ask - 0.02, 100)], asks=[BookLevel(ask, 3.0), BookLevel(ask + 0.05, 100)])
    assert w.e.revalidate(ev, thin) is Reason.LOW_LIQUIDITY


def test_thin_depth_at_the_limit_is_refused_at_the_decision(monkeypatch):
    w = _World(monkeypatch)
    ts = T0 + 100_000.0
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02), depth=3.0)
    assert "REJECT_LIQUIDITY" in _reasons(evs) and not any(ev.tradeable for _, ev in evs)


def _window(ask_path, bid_path, depth=100.0, won_up=True):
    """A replay window: points every 100 ms, the UP side's book."""
    n = len(ask_path)
    t = T0 + np.arange(n) * 100.0
    ask, bid = np.array(ask_path, float), np.array(bid_path, float)
    sv = {"ask": ask, "bid": bid, "adep": np.full(n, depth), "bdep": np.full(n, depth),
          "mid": (ask + bid) / 2, "mfv": np.full(n, 0.6)}
    return Window(slug="btc-updown-5m-1", epoch=1, split="train", close=T0 + 300_000.0, won_up=won_up, rate=0.07,
                  t=t, left=np.full(n, 200.0), lp=np.zeros(n), sigma=np.full(n, SIGMA), side={"UP": sv}), sv


def test_the_shared_replay_fill_model_kills_a_moved_quote_and_never_fills_partially():
    ex = Execution(seed=1)
    # the ask runs away from 0.57 to 0.60 within 100 ms: by arrival it is above the limit
    w, sv = _window([0.57] + [0.60] * 99, [0.55] + [0.58] * 99)
    r = ex.buy(w, sv, float(w.t[0]), 0.57)
    assert not r["filled"] and r["quote_gone"] and r["reason"] == "QUOTE_GONE" and r["arrival_ask"] == 0.60
    # the ask stays, but only 3 shares: FOK fills nothing
    w, sv = _window([0.57] * 100, [0.55] * 100, depth=3.0)
    r = ex.buy(w, sv, float(w.t[0]), 0.57)
    assert not r["filled"] and r["reason"] == "NO_DEPTH"
    # the ask stays with depth: filled at the ask on arrival, with its latency
    w, sv = _window([0.57] * 100, [0.55] * 100)
    r = ex.buy(w, sv, float(w.t[0]), 0.57)
    assert r["filled"] and r["entry"] == 0.57 and 0 < r["latency_ms"] < 1_000


def test_pnl_accounting_charges_both_fees_and_none_at_settlement():
    w, _ = _window([0.57] * 10, [0.55] * 10)
    net, fees = trade_pnl(w, 0.57, 0.60, "SOLD")
    assert fees == pytest.approx(0.07 * 0.57 * 0.43 + 0.07 * 0.60 * 0.40)
    assert net == pytest.approx(0.60 - 0.57 - fees)
    net, fees = trade_pnl(w, 0.57, 1.0, "SETTLE")
    assert fees == pytest.approx(0.07 * 0.57 * 0.43) and net == pytest.approx(1.0 - 0.57 - fees)


def test_every_market_is_independent(monkeypatch):
    w = _World(monkeypatch, _cfg(max_simultaneous_positions=4, max_total_exposure_usdc=20.0))
    ts = T0 + 100_000.0
    btc, eth = w.meta("BTC"), w.meta("ETH")
    tok = _ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02)
    t, ev = _first_trade(w.run(btc, ts, ts + 2_000.0, token=tok))
    assert ev is not None
    # the BTC entry is sent and filled
    w.risk.on_submit(ev.slug, usdc=ev.limit_price * ev.size, asset="BTC", epoch=int(T0 // 1000))
    w.e.on_entry_submit(ev.slug, t)
    w.risk.on_fill(ev.slug, "BTC", int(T0 // 1000), "UP", ev.expected_price * ev.size, ev.size)
    w.e.on_entry_fill(ev.slug, ev.token_id, t + 300.0)
    # ETH has no shock of its own: nothing, whatever BTC does
    assert "eth-updown-5m-1800000000" not in w.e._shock                                   # noqa: SLF001
    # ETH's own shock trades, BTC's position notwithstanding
    t2, ev2 = _first_trade(w.run(eth, ts + 20_000.0, ts + 22_000.0, token=_ramp(ts + 20_000.0, to=0.57,
                                                                                    over_ms=800.0, then_per_s=0.02)))
    assert ev2 is not None and ev2.asset == "ETH"
    # BTC's market had its entry: a second shock there is refused as a risk limit
    evs = w.run(btc, ts + 40_000.0, ts + 42_000.0, token=_ramp(ts + 40_000.0, frm=0.57, to=0.64, over_ms=800.0,
                                                                 then_per_s=0.02))
    assert not any(e.tradeable for _, e in evs) and "REJECT_RISK_LIMIT" in _reasons(evs)
    # the NEXT BTC window is a different market
    nxt = w.meta("BTC", T0 + 300_000.0)
    t3, ev3 = _first_trade(w.run(nxt, T0 + 400_000.0, T0 + 402_000.0,
                                 token=_ramp(T0 + 400_000.0, to=0.57, over_ms=800.0, then_per_s=0.02)))
    assert ev3 is not None and ev3.slug != ev.slug


def test_the_momentum_budget_and_the_global_limits_apply(monkeypatch):
    ts = T0 + 100_000.0
    tok = _ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02)
    # the momentum budget tightens the global limits and never loosens them
    rc = RiskConfig(max_position_usdc=5.0, max_total_exposure_usdc=50.0, max_concurrent_positions=10,
                    max_daily_loss_usdc=2.0)
    lines = _cfg().apply_budget(rc)
    assert rc.max_concurrent_positions == 1 and rc.max_total_exposure_usdc == 5.0 and rc.max_position_usdc == 3.0
    assert rc.max_daily_loss_usdc == 2.0 and lines                                        # the tighter global stays
    # $3 per order: 5 shares at 0.62 is 3.10 -> refused, never resized below the venue minimum
    w = _World(monkeypatch)
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, token=_ramp(ts, frm=0.55, to=0.62, over_ms=800.0, then_per_s=0.02),
                p_after=0.65)
    assert not any(ev.tradeable for _, ev in evs)
    assert any("order cap" in ev.features["repriced_momentum"]["detail"] for _, ev in _cands(evs))
    # one position at a time, all markets together
    w = _World(monkeypatch)
    w.risk.on_fill("xrp-updown-5m-1", "XRP", 1, "UP", 2.5, 5.0)
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, token=tok)
    assert not any(ev.tradeable for _, ev in evs) and "REJECT_RISK_LIMIT" in _reasons(evs)
    # the daily loss limit halts every entry
    w = _World(monkeypatch)
    w.risk.on_settle("xrp-updown-5m-1", -5.0, now_s=ts / 1000.0)
    w.risk._roll_day = lambda now_s: None                                                 # noqa: SLF001
    evs = w.run(w.meta("BTC"), ts, ts + 2_000.0, token=tok)
    cs = [ev.features["repriced_momentum"] for _, ev in _cands(evs) if ev.features["repriced_momentum"].get("signal")]
    assert cs and all(c["reason"] == "REJECT_RISK_LIMIT" and "daily loss" in c["detail"] for c in cs)


def test_every_candidate_is_written_with_its_features_and_decision(monkeypatch, tmp_path):
    w = _World(monkeypatch)
    w.e.candidate_log_path = tmp_path / "repriced_momentum_candidates.jsonl"
    ts = T0 + 100_000.0
    w.run(w.meta("BTC"), ts, ts + 2_000.0, token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02))
    rows = [json.loads(x) for x in w.e.candidate_log_path.read_text().splitlines()]
    assert rows and all(r["strategy"] == "repriced_momentum" and r["decision"] in ("BUY", "SKIP") for r in rows)
    assert {r["decision"] for r in rows} == {"BUY", "SKIP"}
    for k in ("ts", "market", "asset", "direction", "spot_price", "token_price", "fair_value", "expected_repricing",
              "actual_repricing", "repricing_pct", "spot_return_100ms", "spot_return_250ms", "spot_return_500ms",
              "spot_return_1s", "token_return_100ms", "token_return_250ms", "token_return_500ms", "token_return_1s",
              "momentum_score", "spread", "expected_slippage", "reason"):
        assert k in rows[-1]
    assert all(r["reason"].startswith("REJECT_") for r in rows if r["decision"] == "SKIP")
    assert w.e.reject_counts.get("ACCEPT", 0) >= 1


def test_the_exit_follows_the_momentum(monkeypatch):
    w = _World(monkeypatch)
    meta = w.meta("BTC")
    ts = T0 + 100_000.0
    t, ev = _first_trade(w.run(meta, ts, ts + 2_000.0, token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02)))
    tok = ev.token_id
    w.e.on_entry_fill(ev.slug, tok, t + 300.0)
    kw = dict(asset="BTC", side="UP", token_id=tok, entry=ev.limit_price, spread=0.02)
    # still running our way: hold
    w.feed("BTC", t + 1_000.0, lambda x: 6.0 + 3.0 * (x - ts) / 1000.0)
    assert w.e.exit_decision(bid=0.57, now_ms=t + 800.0, **kw) is None
    assert w.e.exit_decision(bid=0.575, now_ms=t + 1_000.0, **kw) is None
    # the spot turns: out
    w.feed("BTC", t + 1_600.0, lambda x: 6.0 + 3.0 * (t + 1_000.0 - ts) / 1000.0 - 4.0 * (x - t - 1_000.0) / 1000.0)
    assert w.e.exit_decision(bid=0.576, now_ms=t + 1_600.0, **kw) == "SPOT_REVERSAL"
    # the maximum hold ends any position; a position of unknown age is closed at once
    assert w.e.exit_decision(bid=0.60, now_ms=t + 300.0 + 8_000.0, **kw) == "TIME_STOP"
    assert w.e.exit_decision(bid=0.60, now_ms=t, **{**kw, "token_id": "unknown"}) == "TIME_STOP"
    # the timer mode sells after hold_s whatever the momentum
    w2 = _World(monkeypatch, _cfg(exit_mode="timer", hold_s=2.0))
    t2, ev2 = _first_trade(w2.run(w2.meta("ETH"), ts, ts + 2_000.0,
                                  token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02)))
    w2.e.on_entry_fill(ev2.slug, ev2.token_id, t2)
    kw2 = {**kw, "asset": "ETH", "token_id": ev2.token_id}
    assert w2.e.exit_decision(bid=0.5, now_ms=t2 + 1_900.0, **kw2) is None
    assert w2.e.exit_decision(bid=0.5, now_ms=t2 + 2_000.0, **kw2) == "TIME_STOP"


def test_take_profit_and_stop_loss_come_first():
    kw = dict(mode="timer", held_s=0.5, hold_s=10.0, max_hold_s=8.0, spot_z=None, token_move=None, fair=None)
    # +3c target: not yet at +2c, sold at +3c
    assert exit_reason(bid=0.62, entry=0.60, take_profit=0.03, **kw) is None
    assert exit_reason(bid=0.63, entry=0.60, take_profit=0.03, **kw) == "TAKE_PROFIT"
    # a stop below the entry
    assert exit_reason(bid=0.55, entry=0.60, take_profit=0.03, stop_loss=0.05, **kw) == "STOP_LOSS"
    assert exit_reason(bid=0.56, entry=0.60, take_profit=0.03, stop_loss=0.05, **kw) is None
    # the fallback timer still ends it
    assert exit_reason(bid=0.60, entry=0.60, take_profit=0.03, **{**kw, "held_s": 10.0}) == "TIME_STOP"
    # hold_s <= 0: no time stop -- to the target, the stop or settlement
    assert exit_reason(bid=0.60, entry=0.60, take_profit=0.03, **{**kw, "held_s": 200.0, "hold_s": 0.0}) is None
    # the take-profit works in the event modes too, ahead of their own rules
    assert exit_reason("reversal", 1.0, 2.0, 8.0, -1.0, -0.01, 0.64, 0.7, entry=0.60, take_profit=0.03) == "TAKE_PROFIT"
    # off by default
    assert exit_reason(bid=0.99, entry=0.60, **kw) is None


def test_the_engine_takes_profit_at_its_target(monkeypatch):
    w = _World(monkeypatch, _cfg(exit_mode="timer", hold_s=10.0, take_profit_cents=3.0, stop_loss_cents=5.0))
    ts = T0 + 100_000.0
    t, ev = _first_trade(w.run(w.meta("BTC"), ts, ts + 2_000.0, token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02)))
    w.e.on_entry_fill(ev.slug, ev.token_id, t)
    kw = dict(asset="BTC", side="UP", token_id=ev.token_id, entry=0.58, spread=0.02)
    assert w.e.exit_decision(bid=0.60, now_ms=t + 1_000.0, **kw) is None
    assert w.e.exit_decision(bid=0.61, now_ms=t + 1_200.0, **kw) == "TAKE_PROFIT"
    assert w.e.exit_decision(bid=0.53, now_ms=t + 1_400.0, **kw) == "STOP_LOSS"
    assert w.e.exit_decision(bid=0.58, now_ms=t + 10_000.0, **kw) == "TIME_STOP"


def test_the_token_reversal_exit(monkeypatch):
    w = _World(monkeypatch)
    ts = T0 + 100_000.0
    t, ev = _first_trade(w.run(w.meta("BTC"), ts, ts + 2_000.0,
                               token=_ramp(ts, to=0.57, over_ms=800.0, then_per_s=0.02)))
    w.e.on_entry_fill(ev.slug, ev.token_id, t)
    kw = dict(asset="BTC", side="UP", token_id=ev.token_id, entry=ev.limit_price, spread=0.02)
    w.feed("BTC", t + 2_000.0, lambda x: 6.0 + 3.0 * (x - ts) / 1000.0)     # the spot keeps going
    assert w.e.exit_decision(bid=0.58, now_ms=t + 200.0, **kw) is None
    assert w.e.exit_decision(bid=0.58, now_ms=t + 600.0, **kw) is None
    assert w.e.exit_decision(bid=0.56, now_ms=t + 1_100.0, **kw) == "TOKEN_REVERSAL"


# ─────────────────────────────── the live bot, paper ───────────────────────────────


ASSETS = ("BTC", "ETH")


class _Bot:
    """A paper LiveBot running repriced_momentum; the engine's decision is stubbed
    (a qualifying candidate on demand) so the test drives the bot's own plumbing."""

    def __init__(self, tmp_path, **scfg) -> None:
        cfg = BotConfig()
        cfg.engine.strategy = "repriced_momentum"
        cfg.execution_scenario = "venue_lag"
        cfg.deploy_mode = "paper"
        cfg.repriced_momentum = _cfg(**{"exit_mode": "timer", "hold_s": 2.0, **scfg})
        self.clock_t = T0 + 100_000.0
        self.bot = LiveBot(assets=ASSETS, balance=50.0, cfg=cfg, state_path=str(tmp_path / "live_state.rm.json"),
                           trade_log=str(tmp_path / "live_trades.rm.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                           kill_file=str(tmp_path / "KILL"))
        self.tmp = tmp_path
        self.bot.clock.now = lambda: self.clock_t
        self.want: set[str] = set()
        eng = self.bot.engine

        def evaluate(meta, book_up, book_down, *a, **kw):
            m = meta.market
            now = kw["now"] if "now" in kw else a[4]
            ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=m.seconds_remaining(now), reason=None)
            if m.slug not in self.want:
                return eng._reject(ev, Reason.WEAK_MOMENTUM, "no active shock")                # noqa: SLF001
            self.want.discard(m.slug)
            ev.features = {"repriced_momentum": {"signal": True, "direction": "UP", "shock_id": f"{m.slug}#1",
                                                 "repricing_pct": 0.7, "momentum_score": 1.2, "ask": 0.52,
                                                 "fair_value": 0.53, "expected_edge": 0.01}}
            sv = SideView(side="UP", token_id=m.yes_token_id, p_model=0.53, p_market=0.51, ask=0.52, bid=0.50,
                          depth_at_limit=100.0, costs=None, reason=None, entry_px=0.52)
            ev.sides = [sv]
            ev.side, ev.token_id, ev.limit_price, ev.expected_price = "UP", m.yes_token_id, 0.52, 0.52
            ev.size, ev.net_edge, ev.confidence, ev.p_used, ev.max_entry_price = 5.0, 0.01, 1.0, 0.53, 0.52
            return ev

        eng.evaluate = evaluate

    def add_market(self, asset: str, open_ts: float = T0, bid=0.50, ask=0.52) -> LiveMarket:
        slug = f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}"
        m = Market(condition_id=f"c-{slug}", asset=asset, yes_token_id=f"{slug}-UP", no_token_id=f"{slug}-DN",
                   strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0, tick_size=0.01, min_size=5.0, slug=slug)
        lm = LiveMarket(meta=MarketMeta(market=m, resolution_source="", accepting_orders=True, duration_s=300.0,
                                        fee=FEE))
        for tok, (b, a) in ((m.yes_token_id, (bid, ask)), (m.no_token_id, (round(1 - ask, 2), round(1 - bid, 2)))):
            lb = LiveBook(tok)
            lb.apply_snapshot({"bids": [{"price": str(b), "size": "100"}], "asks": [{"price": str(a), "size": "100"}],
                               "timestamp": str(int(self.clock_t))})
            lm.books[tok] = lb
            self.bot.token_index[tok] = (slug, lm)
        self.bot.markets[slug] = lm
        self.bot.twap.setdefault(asset, TwapState())
        return lm

    def tick(self, dt_ms: float = 200.0) -> None:
        self.clock_t += dt_ms
        for lm in self.bot.markets.values():
            for lb in lm.books.values():
                lb.ts = lb.recv_ts = self.clock_t
        for a in ASSETS:
            self.bot.spot_ts[a] = self.clock_t
        self.bot._tick()                                   # noqa: SLF001

    def run(self, ms: float) -> None:
        for _ in range(int(ms // 200)):
            self.tick()

    def ledger(self) -> list[dict]:
        p = self.tmp / "live_trades.rm.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_the_live_bot_runs_it_under_its_own_name_and_budget(tmp_path):
    b = _Bot(tmp_path)
    assert type(b.bot.engine) is RepricedMomentumEngine and b.bot.engine.scfg is b.bot.cfg.repriced_momentum
    # the momentum budget, not spot-lag's, went into the risk limits
    assert b.bot.risk.cfg.max_concurrent_positions == 1 and b.bot.risk.cfg.max_position_usdc <= 3.0
    assert b.bot.engine.candidate_log_path.name == "repriced_momentum_candidates.rm.jsonl"


def test_the_dashboard_cannot_lift_the_momentum_budget(tmp_path):
    from troll_poly_bot.control import ControlFile
    b = _Bot(tmp_path)
    (tmp_path / "controls.json").write_text(json.dumps({"paper": {"risk.max_daily_loss_usdc": 40.0}}))
    b.bot.controls = ControlFile(tmp_path / "controls.json")
    b.bot._apply_controls()                                                               # noqa: SLF001
    assert b.bot.risk.cfg.max_daily_loss_usdc == 5.0
    assert b.bot.risk.cfg.max_concurrent_positions == 1 and b.bot.risk.cfg.max_position_usdc <= 3.0


def test_a_paper_round_trip_is_labelled_and_accounted(tmp_path):
    b = _Bot(tmp_path)
    lm = b.add_market("BTC")
    b.want.add(lm.meta.market.slug)
    b.tick()
    assert b.bot.exchange.n_submitted == 1
    b.run(1_500)                                           # latency + venue_lag: filled
    tok = lm.meta.market.yes_token_id
    assert b.bot.exchange.positions[tok].shares == 5.0
    b.run(3_000)                                           # the 2 s timer: sold at the bid
    assert lm.exited and not b.bot.risk.open
    rows = b.ledger()
    assert rows and all(r.get("strategy") == "repriced_momentum" for r in rows)
    buy = next(r for r in rows if r.get("event") in ("fill", "buy") or r.get("side") == "BUY")
    sell = next(r for r in rows if r.get("kind") == "TIME_STOP" or r.get("event") in ("sell", "exit"))
    assert buy and sell
    # P&L: what the sale brought, less what the position cost, less both taker fees
    fee_in, fee_out = FEE.taker_fee_per_share(0.52) * 5, FEE.taker_fee_per_share(0.50) * 5
    assert b.bot.risk.realised_total == pytest.approx((0.50 - 0.52) * 5 - fee_in - fee_out, abs=0.02)
    b.bot.forensics.flush(b.clock_t + 60_000.0) if hasattr(b.bot.forensics, "flush") else None
    fx = tmp_path / "trade_forensics.rm.jsonl"
    recs = [json.loads(x) for x in fx.read_text().splitlines()] if fx.exists() else \
        [r.fields for r in b.bot.forensics.records.values()]
    trades = [r for r in recs if r.get("event") == "trade"]
    assert trades and all(r["strategy"] == "repriced_momentum" for r in trades)
    assert {r["kind"] for r in trades} >= {"entry"} and any(r.get("fill_latency_ms") is not None for r in trades)


def _move_book(b, lm, bid, ask):
    for tok, (bb, aa) in ((lm.meta.market.yes_token_id, (bid, ask)), (lm.meta.market.no_token_id, (round(1 - ask, 2), round(1 - bid, 2)))):
        lm.books[tok].apply_snapshot({"bids": [{"price": str(bb), "size": "100"}], "asks": [{"price": str(aa), "size": "100"}],
                                      "timestamp": str(int(b.clock_t))})


def test_the_live_bot_sells_at_the_take_profit(tmp_path):
    b = _Bot(tmp_path, hold_s=30.0, take_profit_cents=3.0)
    lm = b.add_market("BTC")
    b.want.add(lm.meta.market.slug)
    b.tick()
    b.run(1_500)
    tok = lm.meta.market.yes_token_id
    assert b.bot.exchange.positions[tok].shares == 5.0          # bought at 0.52
    _move_book(b, lm, 0.58, 0.59)                              # +6c: clears both fees and the 0.5c minimum
    b.run(2_000)
    assert lm.exited
    sell = next(r for r in b.ledger() if r.get("action") == "SELL")
    assert sell["kind"] == "TAKE_PROFIT" and sell["price"] == pytest.approx(0.58)
    assert b.bot.risk.realised_total > 0


def test_a_target_that_fees_would_eat_waits_for_the_fallback(tmp_path):
    b = _Bot(tmp_path, hold_s=4.0, take_profit_cents=2.0)
    lm = b.add_market("BTC")
    b.want.add(lm.meta.market.slug)
    b.tick()
    b.run(1_500)
    _move_book(b, lm, 0.54, 0.55)                              # +2c: less than the two fees, not a profit
    b.run(1_500)
    assert not lm.exited and b.bot.exec_rejections.get("EXIT_FEE_EATS_IT", 0) > 0
    b.run(4_000)                                               # the 4 s fallback sells it
    assert lm.exited
    assert next(r for r in b.ledger() if r.get("action") == "SELL")["kind"] == "TIME_STOP"


def test_the_kill_switch_stops_new_entries(tmp_path):
    b = _Bot(tmp_path)
    lm = b.add_market("BTC")
    (tmp_path / "KILL").write_text("stop")
    b.want.add(lm.meta.market.slug)
    b.tick()
    b.run(1_000)
    assert b.bot.exchange.n_submitted == 0 and not b.bot.risk.open
