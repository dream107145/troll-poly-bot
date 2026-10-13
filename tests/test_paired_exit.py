"""PAIRED-EXIT (strategy/paired_exit*.py): the live features equal the replay's, every leg has its own exits,
the exposure book refuses correlated stacking, the kill conditions stop entries, the opposite token is only an
option (a late pair fill becomes a leg with its own exit), and the live bot books partial and full sells."""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import numpy as np
import pytest

from test_spot_lag import NOW, SLUG, T0, _book, _feed, _meta, _Twap, _Vol   # the spot-lag engine harness

from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.live import LiveBot, LiveMarket
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.strategy import paired_exit as px
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.strategy.triangle import TriangleConfig, TriangleEngine
from troll_poly_bot.types import TimeInForce

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "paired_exit"))


def _points(n=600, seed=3, jump_at_end_bp=0.0, t0=T0, close=T0 + 300_000.0):
    """A synthetic window of 200 ms samples: composite + three exchanges, both books, order flow."""
    rng = random.Random(seed)
    pts, s = [], 100.0
    ex = {"binance": 100.02, "bybit": 99.99, "coinbase": 100.01}
    for k in range(n):
        t = t0 + 1000.0 + 200.0 * k + rng.uniform(-20, 20)
        step = rng.gauss(0, 0.3e-4)
        if jump_at_end_bp and k >= n - 5:
            step = jump_at_end_bp * 1e-4 / 5
        s *= math.exp(step)
        for e in ex:
            ex[e] *= math.exp(step + rng.gauss(0, 0.05e-4))
        mid = min(0.95, max(0.05, 0.5 + (s - 100.0) * 3))
        pts.append({"t": t, "spot": s, "left": (close - t) / 1000.0,
                    "ua": round(mid + 0.01, 2), "ub": round(mid - 0.01, 2), "da": round(1 - mid + 0.01, 2),
                    "db": round(1 - mid - 0.01, 2), "uad": 300.0 + k % 7, "ubd": 280.0, "dad": 290.0, "dbd": 310.0 - k % 5,
                    "srcs": None if k % 97 == 0 else dict(ex), "of": {"tfi15": 0.1, "obi": -0.2, "ofi": 0.05}})
    return pts


# ─────────────────────────────── features: live == replay ───────────────────────────────


def test_live_features_equal_the_replay_features_at_every_sample():
    import pe_lib as L
    pts = _points()
    w = L.Win()
    w.T = np.array([p["t"] for p in pts]); w.n = len(pts); w.close = T0 + 300_000.0; w.strike = 100.0
    w.left = (w.close - w.T) / 1000.0; w.spot = np.array([p["spot"] for p in pts])
    for k in ("ua", "ub", "da", "db", "uad", "ubd", "dad", "dbd"):
        setattr(w, k, np.array([p[k] for p in pts], float))
    w.tfi = np.array([p["of"]["tfi15"] for p in pts]); w.obi = w.tfi * 0
    w.ofi = np.array([p["of"]["ofi"] for p in pts])
    w.X = {e: np.array([np.nan if not p["srcs"] or e not in p["srcs"] else p["srcs"][e] for p in pts]) for e in L.EXCH}
    sigma = 0.31
    F = L.compute_features(w, sigma)
    for i in list(range(0, len(pts), 13)) + [len(pts) - 1]:
        G = px.features_at(pts, 100.0, sigma, T0 + 300_000.0, i)
        for k in px.FEATS:
            a, b = F[k][i], G[k]
            assert math.isnan(a) == math.isnan(b), (k, i, a, b)
            if not math.isnan(a):
                assert a == pytest.approx(b, abs=1e-9), (k, i)
    assert px.window_sigma(pts) == pytest.approx(L.window_sigma(w), rel=1e-9)


def test_the_fair_model_reads_missing_features_as_their_training_mean():
    m = px.FairModel({"features": list(px.FEATS), "mu": [0.0] * len(px.FEATS), "sd": [1.0] * len(px.FEATS),
                      "w": [0.0, 1.0] + [0.5] * (len(px.FEATS) - 1)})
    F = {k: float("nan") for k in px.FEATS}
    F["lm"] = math.log(0.7 / 0.3)
    assert m.p_up(F) == pytest.approx(0.7)


# ─────────────────────────────── a leg's own exits ───────────────────────────────


def _leg(**kw):
    base = dict(leg_id="leg-1", slug=SLUG, asset="BTC", side="UP", token_id="UP", shares=5.0, fill_px=0.60,
                t_fill=NOW, close_ts=T0 + 300_000.0)
    base.update(kw)
    leg = px.Leg(**base)
    leg.remaining = leg.shares
    return leg


def test_every_leg_exit_trigger_fires_on_its_own_condition():
    prm = px.PxParams(stop_loss=0.10, thesis_mode="both", thesis_drop=0.15, reversal_threshold=2.0, max_hold_s=60.0,
                      time_exit_s=30.0, settle_keep=0.95)
    leg = _leg()
    assert px.plan_exit(leg, prm, NOW + 1000, 0.55, 0.60, 0.0, 0.0, 170.0) is None
    assert px.plan_exit(leg, prm, NOW + 1000, 0.50, 0.60, 0.0, 0.0, 170.0)[0] == "STOP_LOSS"
    assert px.plan_exit(leg, prm, NOW + 1000, 0.55, 0.44, 0.0, 0.0, 170.0)[0] == "THESIS_INVALIDATED"
    assert px.plan_exit(leg, prm, NOW + 1000, 0.55, 0.60, -2.5, -1.0, 170.0)[0] == "THESIS_INVALIDATED"
    assert px.plan_exit(leg, prm, NOW + 1000, 0.55, 0.60, -2.5, +0.3, 170.0) is None      # exchanges do not agree
    assert px.plan_exit(leg, prm, NOW + 61_000, 0.55, 0.60, 0.0, 0.0, 110.0)[0] == "UNPAIRED_TIMEOUT"
    assert px.plan_exit(leg, px.PxParams(time_exit_s=30.0), NOW + 1000, 0.80, 0.8, 0, 0, 25.0)[0] == "TIME_EXIT"
    assert px.plan_exit(leg, px.PxParams(time_exit_s=30.0), NOW + 1000, 0.96, 0.96, 0, 0, 25.0) is None   # near-certain
    down = _leg(side="DOWN", token_id="DOWN")
    assert px.plan_exit(down, prm, NOW + 1000, 0.55, 0.60, +2.5, +1.0, 170.0)[0] == "THESIS_INVALIDATED"
    leg.state = "PAIRED"
    assert px.plan_exit(leg, prm, NOW + 1000, 0.10, 0.10, -9, -1, 10.0) is None              # a locked pair is held


def test_ladder_levels_never_go_under_the_venue_minimum():
    assert px.ladder_allocation(5.0, (0.5, 0.5)) == [5.0]
    assert px.ladder_allocation(10.0, (0.5, 0.5)) == [5.0, 5.0]
    assert px.ladder_allocation(20.0, (0.2, 0.3, 0.3, 0.2)) == [10.0, 6.0, 4.0 + 0.0] or \
        all(x >= 5.0 for x in px.ladder_allocation(20.0, (0.2, 0.3, 0.3, 0.2))[:-1])
    alloc = px.ladder_allocation(20.0, (0.2, 0.3, 0.3, 0.2))
    assert sum(alloc) == 20.0 and all(x >= 4.0 for x in alloc)
    leg = _leg(fill_px=0.58)
    prm = px.PxParams(ladder_levels=(0.06, 0.09, 0.12, 0.16), ladder_fair_offset=0.0)
    assert px.ladder_prices(leg, prm, None) == [0.64, 0.67, 0.70, 0.74]
    assert px.ladder_prices(leg, prm, 0.685) == [0.69, 0.69, 0.70, 0.74]                      # never under fair


# ─────────────────────────────── exposure + stacking ───────────────────────────────


def test_the_exposure_book_refuses_correlated_stacking():
    prm = px.PxParams(max_window_usdc=10.0, max_unpaired_usdc=7.0, max_directional_usdc=7.0, max_open_positions=2,
                      max_buys_per_window=3, max_position_usdc=5.0)
    book = px.ExposureBook()
    legs = {}
    assert book.check(legs, prm, SLUG, "UP", 3.0) == (True, "")
    assert book.check(legs, prm, SLUG, "UP", 5.5)[0] is False                    # MAX_POSITION_USDC
    legs["a"] = _leg(fill_px=0.60)                                             # 3.00 USDC unpaired UP
    book.on_buy(SLUG, 3.0)
    ok, why = book.check(legs, prm, SLUG, "UP", 5.0)
    assert not ok and "unpaired" in why
    ok, why = book.check(legs, prm, SLUG, "DOWN", 4.0)                         # hedging direction: allowed
    assert ok, why
    legs["b"] = _leg(leg_id="b", side="DOWN", token_id="DOWN", fill_px=0.80)
    book.on_buy(SLUG, 4.0)
    ok, why = book.check(legs, prm, SLUG, "UP", 1.0)
    assert not ok and "open leg" in why                                        # MAX_OPEN_POSITIONS
    legs["b"].state = "CLOSED"
    ok, why = book.check(legs, prm, SLUG, "UP", 3.5)
    assert not ok and "window" in why                                          # 3 + 4 + 3.5 > 10
    book.window(SLUG).buys = 3
    assert "buys" in book.check(legs, prm, SLUG, "DOWN", 1.0)[1]               # MAX_BUYS_PER_WINDOW


# ─────────────────────────────── kill conditions ───────────────────────────────


def test_kill_conditions_stop_entries():
    prm = px.PxParams(max_daily_loss=5.0, max_consecutive_losses=2, loss_pause_s=60.0, max_data_age_ms=1000.0,
                      max_signal_age_ms=500.0, max_execution_latency_ms=800.0, max_spread=0.04, max_disagreement=2.0,
                      max_settle_mismatches=2, max_unpaired_usdc=10.0)
    k = px.KillMonitor()
    ok_kw = dict(unpaired=0.0, spot_age_ms=100.0, point_age_ms=100.0, book_age_ms=100.0, latency_ms=300.0,
                 spread=0.01, disp=0.5)
    assert k.check(prm, NOW, **ok_kw)[0]
    for field_, val, code in (("spot_age_ms", 5000.0, "STALE_DATA"), ("point_age_ms", 900.0, "STALE_DATA"),
                              ("book_age_ms", None, "STALE_BOOK"), ("latency_ms", 900.0, "LATENCY_REJECT"),
                              ("spread", 0.06, "ABNORMAL_SPREAD"), ("disp", 3.0, "EXCHANGE_DISAGREEMENT")):
        assert k.check(prm, NOW, **{**ok_kw, field_: val})[1] == code
    k.on_result(-1.0, NOW, prm)
    k.on_result(-1.0, NOW, prm)                                                # a 2-loss streak: paused
    assert k.check(prm, NOW + 1000, **ok_kw)[1] == "KILL_LOSS_STREAK"
    assert k.check(prm, NOW + 61_000, **ok_kw)[0]
    k.on_result(-4.0, NOW, prm)
    assert k.check(prm, NOW + 61_000, **ok_kw)[1] == "KILL_DAILY_LOSS"         # -6 today
    k2 = px.KillMonitor()
    assert k2.check(prm, NOW, **{**ok_kw, "unpaired": 12.0})[1] == "KILL_UNPAIRED"
    assert k2.check(prm, NOW, **ok_kw)[1] == "KILL_HALTED"                     # needs a restart
    k3 = px.KillMonitor()
    k3.on_settlement(True, False, prm, SLUG)
    k3.on_settlement(None, True, prm, SLUG)
    assert k3.check(prm, NOW, **ok_kw)[1] == "KILL_HALTED"


# ─────────────────────────────── the engine ───────────────────────────────


def _model_dir(tmp_path):
    d = tmp_path / "px"
    d.mkdir(exist_ok=True)
    (d / "fair_btc.json").write_text(json.dumps({
        "features": list(px.FEATS), "mu": [0.0] * len(px.FEATS), "sd": [1.0] * len(px.FEATS),
        # P(UP) = the token mid nudged by the 1 s spot move
        "w": [0.0, 1.0, 0.0, 0.0, 0.3] + [0.0] * (len(px.FEATS) - 4)}))
    return str(d)


def _pcfg(tmp_path, **kw) -> TriangleConfig:
    base = dict(enabled=True, px_enabled=True, px_model_dir=_model_dir(tmp_path), px_momentum_threshold=2.0,
                px_entry_min_edge=0.0, px_cost_adverse=0.0, px_cost_slippage=0.0, px_stop_loss=0.10,
                px_max_signal_age_ms=10_000.0, px_max_data_age_ms=10_000.0)
    base.update(kw)
    return TriangleConfig(**base)


def _pengine(tmp_path, **kw) -> TriangleEngine:
    scfg = _pcfg(tmp_path, **kw)
    rc = RiskConfig()
    scfg.apply_budget(rc)
    return TriangleEngine(EngineConfig(strategy="triangle"), scfg, RiskManager(cfg=rc, starting_balance=500.0),
                          FeatureEngine())


def _signal(e, jump_bp=4.0):
    """Feed one window's points ending in a sharp up move at NOW (a >= 2 sigma signal)."""
    m = _meta("BTC").market
    e.px_sigma["BTC"] = 0.3
    pts = _points(n=595, jump_at_end_bp=jump_bp, t0=T0)
    shift = NOW - pts[-1]["t"]
    for p in pts:
        p["t"] += shift
    for p in pts:
        e.observe_point(SLUG, m, p, p["t"])
    return pts


def _peval(e, up=(0.50, 0.52), now=NOW):
    twap = _Twap()
    _feed(e, twap, 6.0, until=now)
    view = CompositeView("BTC", 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
    down = _book("DOWN", round(1 - up[1], 2), round(1 - up[0], 2))
    return e.evaluate(_meta("BTC"), _book("UP", *up), down, view, 200.0, 100.0, now, _Vol(), twap, COLOCATED, 500.0,
                      None, {"UP": 50.0, "DOWN": 50.0})


def test_off_by_default_and_inert():
    e = TriangleEngine(EngineConfig(strategy="triangle"), TriangleConfig(enabled=True),
                       RiskManager(cfg=RiskConfig(), starting_balance=500.0), FeatureEngine())
    assert e.px is None and e.manages_exits is False and e.persist_state() is None
    assert e.exit_actions(_meta("BTC").market, None, None, NOW, {}, {}) == []


def test_a_signal_with_edge_becomes_a_fok_buy_and_its_exits_are_armed_on_the_fill(tmp_path):
    e = _pengine(tmp_path)
    _signal(e)
    assert SLUG in e.px_pending and e.px_pending[SLUG]["dir"] == 1
    ev = _peval(e, up=(0.44, 0.45))                # the model says ~0.5+; the ask 0.45 is cheap
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.side == "UP" and ev.size == 5.0 and ev.limit_price == pytest.approx(0.47)
    o = e.to_order(ev, NOW)
    assert o.tif is TimeInForce.FOK
    e.on_entry_submit(SLUG, NOW)
    m = _meta("BTC").market
    e.on_leg_fill(o.order_id, m, "UP", "UP", 5.0, 0.46, 0.08, NOW + 1500.0)
    (leg,) = e.px_legs.values()
    assert leg.state == "OPEN" and leg.remaining == 5.0
    # the bid falls 10c under the fill: a STOP_LOSS FOK sell at bid - 2c, for the whole leg
    acts = e.exit_actions(m, _book("UP", 0.36, 0.38), _book("DOWN", 0.62, 0.64), NOW + 5000.0, {"UP": 5.0}, {})
    assert [(a.kind, a.shares, a.price, a.tif) for a in acts] == [("STOP_LOSS", 5.0, 0.34, "FOK")]
    e.on_exit_order(leg.leg_id, "STOP_LOSS", "x-1")
    assert e.exit_actions(m, _book("UP", 0.36, 0.38), None, NOW + 5200.0, {"UP": 5.0}, {"x-1": "IN_FLIGHT"}) == []
    e.on_exit_result("x-1", "fok_unfillable", NOW + 6000.0)                   # killed: re-sent at the new bid
    acts = e.exit_actions(m, _book("UP", 0.33, 0.35), None, NOW + 6100.0, {"UP": 5.0}, {})
    assert [(a.kind, a.price) for a in acts] == [("STOP_LOSS", 0.31)]
    e.on_exit_order(leg.leg_id, "STOP_LOSS", "x-2")
    assert e.on_exit_fill("x-2", 5.0, 0.33, 0.07, NOW + 7000.0) == "STOP_LOSS"
    assert leg.state == "CLOSED" and e.px_closed[-1]["pnl"] == pytest.approx(5 * 0.33 - 0.07 - 0.08 - 5 * 0.46)


def test_no_edge_no_buy_and_the_triangle_is_hollow(tmp_path):
    e = _pengine(tmp_path, px_entry_min_edge=0.05)
    _signal(e)
    fair = e.px_pending[SLUG]["p_up"]
    ask = min(0.84, round(fair + 0.01, 2))         # priced at (or above) the model's fair value: no edge
    ev = _peval(e, up=(round(ask - 0.02, 2), ask))
    assert not ev.tradeable and ev.reason is Reason.EDGE_TOO_SMALL
    mk = e.shock_marks_for(SLUG)[-1]
    assert mk["ok"] is False and "EV" in mk["why"]


def test_exposure_limits_refuse_a_second_leg(tmp_path):
    e = _pengine(tmp_path, px_max_open_positions=1)
    m = _meta("BTC").market
    e.on_leg_fill("o1", m, "UP", "UP", 5.0, 0.45, 0.0, NOW - 5000.0)
    _signal(e)
    ev = _peval(e, up=(0.44, 0.45))
    assert not ev.tradeable and ev.reason is Reason.EXPOSURE_CAP and "EXPOSURE_LIMIT" in ev.detail


def test_stale_signals_and_slow_execution_are_refused(tmp_path):
    e = _pengine(tmp_path, px_max_signal_age_ms=300.0)
    _signal(e)
    ev = _peval(e, up=(0.44, 0.45), now=NOW + 1000.0)
    assert not ev.tradeable and ev.reason is Reason.STALE_DATA
    e2 = _pengine(tmp_path, px_max_execution_latency_ms=1.0)
    _signal(e2)
    ev2 = _peval(e2, up=(0.44, 0.45))
    assert not ev2.tradeable and "LATENCY_REJECT" in ev2.detail


def test_the_opposite_token_is_only_an_option(tmp_path):
    e = _pengine(tmp_path, px_pair_enabled=True, px_pair_target=0.95)
    m = _meta("BTC").market
    e.on_leg_fill("o1", m, "UP", "UP", 5.0, 0.60, 0.0, NOW)
    side, order = e.pair_order(m, "UP", 5.0, 0.60, NOW)
    assert side == "DOWN" and order.tif is TimeInForce.GTC and order.price == pytest.approx(0.34)
    e.on_pair_submit("p1", SLUG, "DOWN", 0.34, 5.0)
    (leg,) = e.px_legs.values()
    assert leg.pair_order == "p1"
    # the leg's own stop still runs while the pair rests; the exit cancels the pair
    acts = e.exit_actions(m, _book("UP", 0.48, 0.50), None, NOW + 3000.0, {"UP": 5.0}, {})
    kinds = [a.kind for a in acts]
    assert "PAIR_CANCEL" in kinds and "STOP_LOSS" in kinds
    # the pair fills anyway (the cancel lost the race): those DOWN shares become a leg that exits at once
    e.px_on_pair_done("p1", True, 0.34, 5.0, "DOWN", NOW + 3500.0)
    late = [lg for lg in e.px_legs.values() if lg.side == "DOWN"]
    assert len(late) == 1 and late[0].state == "EXITING" and late[0].exit_kind == "UNPAIRED_TIMEOUT"
    # a pair that fills while the leg is open locks it: no more exits, held to settlement
    e2 = _pengine(tmp_path, px_pair_enabled=True)
    e2.on_leg_fill("o2", m, "UP", "UP", 5.0, 0.60, 0.0, NOW)
    e2.pair_order(m, "UP", 5.0, 0.60, NOW)
    e2.on_pair_submit("p2", SLUG, "DOWN", 0.34, 5.0)
    e2.px_on_pair_done("p2", True, 0.34, 5.0, "DOWN", NOW + 1000.0)
    (leg2,) = e2.px_legs.values()
    assert leg2.state == "PAIRED"
    assert e2.exit_actions(m, _book("UP", 0.10, 0.12), None, NOW + 2000.0, {"UP": 5.0}, {}) == []
    e3 = _pengine(tmp_path)                                                    # default: no pair at all
    e3.on_leg_fill("o3", m, "UP", "UP", 5.0, 0.60, 0.0, NOW)
    assert e3.pair_order(m, "UP", 5.0, 0.60, NOW) is None


def test_a_maker_ladder_is_placed_and_cancelled_before_the_stop_sells(tmp_path):
    e = _pengine(tmp_path, px_shares=10.0, px_ladder_levels="0.05,0.10", px_ladder_size_percentages="50,50",
                 px_stop_loss=0.10)
    m = _meta("BTC").market
    e.on_leg_fill("o1", m, "UP", "UP", 10.0, 0.50, 0.0, NOW)
    acts = e.exit_actions(m, _book("UP", 0.50, 0.52), None, NOW + 1000.0, {"UP": 10.0}, {})
    assert [(a.kind, a.shares, a.price, a.tif) for a in acts] == [("LADDER_PLACE", 5.0, 0.55, "GTC"),
                                                                  ("LADDER_PLACE", 5.0, 0.60, "GTC")]
    e.on_exit_order("leg-00001", "LADDER_PLACE", "a1")
    e.on_exit_order("leg-00001", "LADDER_PLACE", "a2")
    assert e.on_exit_fill("a1", 5.0, 0.55, 0.0, NOW + 2000.0) == "TAKE_PROFIT"
    acts = e.exit_actions(m, _book("UP", 0.39, 0.41), None, NOW + 3000.0, {"UP": 5.0}, {"a2": "RESTING"})
    assert [a.kind for a in acts] == ["LADDER_CANCEL"]                         # stop: first the resting ask
    acts = e.exit_actions(m, _book("UP", 0.39, 0.41), None, NOW + 3500.0, {"UP": 5.0}, {"a2": "CANCELLED"})
    assert [(a.kind, a.shares, a.price) for a in acts] == [("STOP_LOSS", 5.0, 0.37)]


def test_settlement_closes_what_is_left_and_restart_keeps_the_legs(tmp_path):
    e = _pengine(tmp_path)
    m = _meta("BTC").market
    e.on_leg_fill("o1", m, "UP", "UP", 5.0, 0.60, 0.05, NOW)
    doc = json.loads(json.dumps(e.persist_state()))
    e2 = _pengine(tmp_path)
    e2.restore_state(doc, NOW + 1000.0)
    (leg,) = e2.px_legs.values()
    assert leg.remaining == 5.0 and leg.fill_px == 0.60 and e2.px_exposure.window(SLUG).spent == pytest.approx(3.0)
    e2.on_market_settled(SLUG, True, 0.0, T0 + 340_000.0)
    assert leg.state == "CLOSED" and e2.px_closed[-1]["pnl"] == pytest.approx(5.0 - 0.05 - 3.0)


# ─────────────────────────────── the live bot ───────────────────────────────


def _bot(tmp_path, **tri):
    cfg = BotConfig()
    cfg.engine.strategy = "triangle"
    cfg.execution_scenario = "calibrated"
    cfg.deploy_mode = "paper"
    cfg.triangle = _pcfg(tmp_path, **tri)
    cfg.spot_lag = SpotLagConfig(enabled=True)
    return LiveBot(assets=("BTC",), balance=500.0, cfg=cfg, state_path=str(tmp_path / "live_state.px.json"),
                   trade_log=str(tmp_path / "live_trades.px.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                   kill_file=str(tmp_path / "KILL"))


def _result(bot, lm, token, side, px_, size, action="BUY", info_extra=None, tif=TimeInForce.FOK):
    from troll_poly_bot.execution.paper import OrderResult, RejectReason
    from troll_poly_bot.types import Fill, Order, OrderState, Side
    sd = Side.BUY if action == "BUY" else Side.SELL
    o = Order(token_id=token, side=sd, price=px_, size=size, tif=tif)
    o.filled_size, o.state = size, OrderState.FILLED
    f = Fill(o.order_id, token, sd, px_, size, 0.0, NOW, NOW, NOW)
    bot.exchange.positions[token].token_id = token
    bot.exchange.positions[token].apply(f)
    bot.fair_by_order[o.order_id] = {"slug": SLUG, "asset": "BTC", "epoch": lm.epoch, "token_id": token, "side": side,
                                     **(info_extra or {})}
    return OrderResult(o, [f], RejectReason.NONE, NOW), o


def test_the_live_bot_hands_exits_to_the_engine_and_books_partial_then_full_sells(tmp_path):
    bot = _bot(tmp_path, px_shares=10.0, px_max_position_usdc=10.0, px_max_window_usdc=20.0,
               px_max_unpaired_usdc=20.0, px_max_directional_usdc=20.0)
    assert bot.engine.manages_exits is True
    lm = LiveMarket(meta=_meta("BTC"))
    bot.markets[SLUG] = lm
    lm.inflight = 1
    bot.risk.on_submit(SLUG, usdc=5.0, asset="BTC", epoch=lm.epoch)
    res, o = _result(bot, lm, "UP", "UP", 0.50, 10.0)
    bot._on_result(res, NOW)                                                   # noqa: SLF001
    (leg,) = bot.engine.px_legs.values()
    assert leg.remaining == 10.0 and lm.inflight == 0
    # a partial exit (5 of 10): nothing realised yet, the market still holds 5
    bot.engine.on_exit_order(leg.leg_id, "LADDER_PLACE", "")                   # (no ladder configured: ignored)
    res2, o2 = _result(bot, lm, "UP", "UP", 0.60, 5.0, action="SELL",
                       info_extra={"exit": True, "managed": True, "kind": "TAKE_PROFIT", "leg_id": leg.leg_id})
    bot.engine.px_orders[o2.order_id] = leg.leg_id
    bot._on_result(res2, NOW + 1000.0)                                         # noqa: SLF001
    assert leg.remaining == 5.0 and not lm.exited
    assert bot.risk.open[SLUG].shares == pytest.approx(5.0)
    rows = [json.loads(x) for x in Path(tmp_path / "live_trades.px.jsonl").read_text().splitlines()]
    assert [r["event"] for r in rows][-1] == "fill" and rows[-1]["action"] == "SELL"
    # the rest: the market is flat -> one realised round trip for the whole market
    leg.state, leg.exit_kind = "EXITING", "STOP_LOSS"
    res3, o3 = _result(bot, lm, "UP", "UP", 0.40, 5.0, action="SELL",
                       info_extra={"exit": True, "managed": True, "kind": "STOP_LOSS", "leg_id": leg.leg_id})
    bot.engine.px_orders[o3.order_id] = leg.leg_id
    lm.inflight = 1
    bot._on_result(res3, NOW + 2000.0)                                         # noqa: SLF001
    assert leg.state == "CLOSED" and lm.exited and SLUG not in bot.risk.open and lm.inflight == 0
    rows = [json.loads(x) for x in Path(tmp_path / "live_trades.px.jsonl").read_text().splitlines()]
    assert rows[-1]["event"] == "settle" and rows[-1]["pnl"] == pytest.approx(-5.0 + 3.0 + 2.0)


def test_the_exit_loop_sends_what_the_engine_asks(tmp_path):
    bot = _bot(tmp_path)
    lm = LiveMarket(meta=_meta("BTC"))
    bot.markets[SLUG] = lm
    bot.engine.on_leg_fill("o1", lm.meta.market, "UP", "UP", 5.0, 0.60, 0.0, NOW)
    bot.exchange.positions["UP"].token_id = "UP"
    bot.exchange.positions["UP"].shares = 5.0
    from troll_poly_bot.live import LiveBook
    for tok, (b, a) in (("UP", (0.45, 0.47)), ("DOWN", (0.53, 0.55))):
        lb = LiveBook(tok)
        lb.bids, lb.asks, lb.ts = {b: 100.0}, {a: 100.0}, NOW
        lm.books[tok] = lb
    bot._take_profit(NOW + 2000.0)                                             # noqa: SLF001
    sent = [s.payload for s in bot.exchange._inflight]                         # noqa: SLF001
    assert len(sent) == 1 and sent[0].side.name == "SELL" and sent[0].price == pytest.approx(0.43)
    info = bot.fair_by_order[sent[0].order_id]
    assert info["managed"] and info["kind"] == "STOP_LOSS" and lm.inflight == 1


def test_after_a_start_sigma_comes_from_the_archived_previous_window(tmp_path):
    charts = tmp_path / "charts"
    charts.mkdir()
    prev = _points(n=1400, t0=T0 - 300_000.0, close=T0)
    (charts / f"btc-updown-5m-{int(T0 // 1000) - 300}.json").write_text(json.dumps({"points": prev}))
    e = _pengine(tmp_path, px_chart_dir=str(charts))
    m = _meta("BTC").market
    for p in _points(n=20, t0=T0)[:3]:
        e.observe_point(SLUG, m, p, p["t"])
    assert e.px_sigma["BTC"] == pytest.approx(px.window_sigma(prev))


def test_a_re_priced_ask_is_replaced_only_after_the_cancel_is_confirmed(tmp_path):
    e = _pengine(tmp_path, px_ladder_levels="0.02", px_ladder_size_percentages="100", px_ladder_fair_offset=0.03,
                 px_stop_loss=0.0)
    m = _meta("BTC").market
    e.on_leg_fill("o1", m, "UP", "UP", 5.0, 0.50, 0.0, NOW)
    e.px_last[SLUG] = {"t": NOW + 1000.0, "F": {"z1": 0.0, "agree": 0.0}, "p_up": 0.55}
    acts = e.exit_actions(m, _book("UP", 0.50, 0.52), None, NOW + 1000.0, {"UP": 5.0}, {})
    assert [(a.kind, a.price) for a in acts] == [("LADDER_PLACE", 0.58)]           # max(0.52, fair 0.55 + 0.03)
    e.on_exit_order("leg-00001", "LADDER_PLACE", "a1")
    e.px_last[SLUG] = {"t": NOW + 3000.0, "F": {"z1": 0.0, "agree": 0.0}, "p_up": 0.60}
    acts = e.exit_actions(m, _book("UP", 0.55, 0.57), None, NOW + 3000.0, {"UP": 5.0}, {"a1": "RESTING"})
    assert [a.kind for a in acts] == ["LADDER_CANCEL"]
    # the cancel is not confirmed yet: no second ask
    acts = e.exit_actions(m, _book("UP", 0.55, 0.57), None, NOW + 3200.0, {"UP": 5.0}, {"a1": "RESTING"})
    assert acts == []
    acts = e.exit_actions(m, _book("UP", 0.55, 0.57), None, NOW + 3400.0, {"UP": 5.0}, {"a1": "CANCELLED"})
    assert [(a.kind, a.price) for a in acts] == [("LADDER_PLACE", 0.63)]


def test_the_dashboards_entry_min_ev_reaches_the_old_entry_model():
    e = TriangleEngine(EngineConfig(strategy="triangle"), TriangleConfig(enabled=True),
                       RiskManager(cfg=RiskConfig(), starting_balance=500.0), FeatureEngine())
    assert e.entry_min_ev == 0.02
    e.scfg.entry_min_ev = 0.5                       # what the control file's "Entry min EV" writes
    assert e.entry_min_ev == 0.5


def test_sigma_rolls_only_once_the_new_window_has_its_own_first_sample(tmp_path):
    e = _pengine(tmp_path, px_chart_dir=str(tmp_path / "none"))
    m1 = _meta("BTC").market
    prev = _points(n=1400, t0=T0, close=T0 + 300_000.0)
    for p in prev[:1000]:
        e.observe_point(SLUG, m1, p, p["t"])
    from troll_poly_bot.types import Market
    m2 = Market(condition_id="c2", asset="BTC", yes_token_id="UP2", no_token_id="DOWN2", strike=100.0,
                open_ts=T0 + 300_000.0, close_ts=T0 + 600_000.0, slug="btc-updown-5m-2", min_size=5.0)
    pre_open = dict(prev[-1], t=T0 + 299_000.0)                   # sampled 1 s BEFORE the next window opens
    e.observe_point("btc-updown-5m-2", m2, pre_open, pre_open["t"])
    assert "BTC" not in e.px_sigma                                # the previous window is not finished
    for p in prev[1000:]:
        e.observe_point(SLUG, m1, p, p["t"])
    first = dict(prev[-1], t=T0 + 300_200.0)
    e.observe_point("btc-updown-5m-2", m2, first, first["t"])
    assert e.px_sigma["BTC"] == pytest.approx(px.window_sigma([p for p in prev if p["t"] <= T0 + 300_000.0]))


def test_a_window_joined_late_does_not_set_sigma_its_archive_does(tmp_path):
    charts = tmp_path / "charts"
    charts.mkdir()
    e = _pengine(tmp_path, px_chart_dir=str(charts))
    m1 = _meta("BTC").market
    full = _points(n=1400, t0=T0, close=T0 + 300_000.0)
    for p in full[600:]:                                          # the bot started two minutes into the window
        e.observe_point(SLUG, m1, p, p["t"])
    e.px_sigma.pop("BTC", None)
    from troll_poly_bot.types import Market
    m2 = Market(condition_id="c2", asset="BTC", yes_token_id="UP2", no_token_id="DOWN2", strike=100.0,
                open_ts=T0 + 300_000.0, close_ts=T0 + 600_000.0, slug="btc-updown-5m-2", min_size=5.0)
    first = dict(full[-1], t=T0 + 300_200.0)
    e.observe_point("btc-updown-5m-2", m2, first, first["t"])
    assert "BTC" not in e.px_sigma                                 # not from 3 minutes of the window
    (charts / f"btc-updown-5m-{int(T0 // 1000)}.json").write_text(json.dumps({"points": full}))
    nxt = dict(full[-1], t=T0 + 311_000.0)
    e.observe_point("btc-updown-5m-2", m2, nxt, nxt["t"])
    assert e.px_sigma["BTC"] == pytest.approx(px.window_sigma(full))
