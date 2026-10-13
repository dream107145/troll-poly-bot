"""strategy/flowsignal.py: the cross-exchange momentum + order-flow signal, the triangle engine's flow mode,
and its wiring on the live bot (every quote / trade scores a direction; a signal evaluates at once)."""
from __future__ import annotations

from collections import deque

import pytest

from test_spot_lag import NOW, SLUG, _book, _feed, _Twap
from test_triangle import _bot, _cfg, _engine, _eval

from troll_poly_bot.strategy.engine import Reason
from troll_poly_bot.strategy.flowsignal import FlowParams, FlowResult, FlowSignal
from troll_poly_bot.strategy.triangle import TriangleConfig, TriangleEngine

EX = ("coinbase", "bybit", "binance")
T0 = 1_000_000.0
BASE = 100_000.0


def _mid(x: str, t: float, *, scale=(1.0, 1.0, 0.8), start=(0.0, 10.0, 20.0), total_bp=2.5, sign=1, t_move=500.0,
         span=100.0, only=None) -> float:
    """Flat, then an ACCELERATING move of ``total_bp`` over ``span`` ms, the exchanges starting 10 ms apart."""
    i = EX.index(x)
    if only is not None and x != only:
        return BASE
    s = t - t_move - start[i]
    if s <= 0:
        return BASE
    frac = min(1.0, s / span) ** 2
    return BASE * (1.0 + sign * total_bp * scale[i] * frac * 1e-4)


def _run(fs: FlowSignal, *, agree=True, t_end=620.0, **kw):
    fired = []
    t = 0.0
    while t <= t_end:
        for x in EX:
            m = _mid(x, t, **kw)
            r = fs.on_quote("BTC", x, m - 0.5, m + 0.5, T0 + t, T0 + t)
            if r is not None:
                fired.append(r)
        if t >= 500.0:
            for x in EX:
                notional = 8_000.0
                sign = kw.get("sign", 1)
                r = fs.on_trade("BTC", x, notional, (sign > 0) == agree, T0 + t, T0 + t)
                if r is not None:
                    fired.append(r)
        t += 10.0
    return fired


def test_a_synchronised_accelerating_move_with_buying_is_an_up_signal():
    fs = FlowSignal(FlowParams())
    fired = _run(fs)
    assert fired, "no signal"
    r = fired[0]
    assert r.dir == 1 and r.score >= 0.70 and r.confirm >= 2 and r.fresh == 3
    assert r.cx > 0 and r.flow > 0 and r.acc >= 0 and r.first > 0
    assert r.first_mover == "coinbase"                       # it left the flat price first
    assert all(f.dir == 1 for f in fired) and len(fired) == 1  # one per direction per second (dedupe)


def test_the_same_move_down_is_a_down_signal():
    fired = _run(FlowSignal(FlowParams()), sign=-1)
    assert fired and fired[0].dir == -1 and fired[0].score <= -0.70


def test_selling_into_the_rise_is_no_signal():
    # price up on all three, but the aggressive flow is SELLING: order flow disagrees
    assert _run(FlowSignal(FlowParams()), agree=False, sign=1) == []


def test_one_exchange_alone_is_no_signal():
    assert _run(FlowSignal(FlowParams()), only="binance") == []


def test_quiet_ticks_cost_almost_nothing_and_fire_nothing():
    fs = FlowSignal(FlowParams())
    for k in range(200):
        for x in EX:
            fs.on_quote("BTC", x, BASE - 0.5, BASE + 0.5, T0 + 10.0 * k, T0 + 10.0 * k)
    assert fs.fired == 0 and fs.scored == 0 and fs.ticks == 600


def test_a_stale_exchange_does_not_count_and_two_fresh_are_needed():
    fs = FlowSignal(FlowParams(min_fresh=3))
    # binance never quotes: only two fresh exchanges but three are required
    t = 0.0
    fired = []
    while t <= 620.0:
        for x in ("coinbase", "bybit"):
            m = _mid(x, t)
            r = fs.on_quote("BTC", x, m - 0.5, m + 0.5, T0 + t, T0 + t)
            fired += [r] if r else []
        t += 10.0
    assert fired == []


def test_a_signal_is_taken_once_per_market_and_expires():
    fs = FlowSignal(FlowParams())
    (r,) = _run(fs)
    assert fs.take("BTC", "m1", r.t_local + 5.0) is r
    assert fs.take("BTC", "m1", r.t_local + 6.0) is None          # already taken by m1
    assert fs.take("BTC", "m2", r.t_local + 6.0) is r             # another market still can
    assert fs.take("BTC", "m3", r.t_local + 501.0) is None        # past valid_ms
    assert fs.take("ETH", "m4", r.t_local + 5.0) is None


# ───────────────────────── the triangle engine in flow mode ─────────────────────────


def _flow_engine(**kw) -> TriangleEngine:
    return _engine(_cfg(signal_source="flow", **kw))


def _result(d=1, t=None, i=1) -> FlowResult:
    return FlowResult(asset="BTC", dir=d, score=0.8 * d, cx=0.7 * d, flow=0.9 * d, acc=0.5 * d, first=0.6 * d,
                      first_mover="coinbase", confirm=3, fresh=3, t_info=NOW, t_local=NOW if t is None else t,
                      moves_bp={"coinbase": 2.0 * d, "bybit": 1.8 * d, "binance": 1.5 * d}, id=i)


def _flow_shock(e, jump_bps=6.0, flow=None, up_now=(0.50, 0.52)):
    twap = _Twap()
    _feed(e, twap, jump_bps, until=NOW - 1000.0)
    _eval(e, twap, NOW - 1000.0, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    _feed(e, twap, jump_bps, until=NOW)
    if flow is not None:
        e.flow.recent.setdefault("BTC", deque(maxlen=64)).append(flow)
    up = _book("UP", *up_now)
    down = _book("DOWN", round(1 - up_now[1], 2), round(1 - up_now[0], 2))
    return _eval(e, twap, NOW, up, down)


def test_flow_mode_trades_the_flow_signal_not_the_spot_shock():
    e = _flow_engine()
    assert e.flow is not None and _engine().flow is None
    ev = _flow_shock(e, 6.0)                                      # a 6 bp 1 s shock, but no flow signal
    assert not ev.tradeable and ev.reason is Reason.WEAK_MOMENTUM and "no flow signal" in ev.detail
    assert e.shock_marks_for(SLUG) == []                          # spot-lag's own detection is not drawn either


def test_a_flow_signal_buys_its_side_and_draws_a_filled_flow_triangle():
    e = _flow_engine()
    ev = _flow_shock(e, 0.0, flow=_result(-1), up_now=(0.60, 0.62))   # flow says DOWN while spot is flat
    assert ev.tradeable, (ev.reason, ev.detail)
    assert (ev.side, ev.token_id) == ("DOWN", "DOWN")
    (mk,) = e.shock_marks_for(SLUG)
    assert mk["dir"] == -1 and mk["ok"] is True and mk["trigger"] == "flow" and mk["flow"]["first_mover"] == "coinbase"
    f = ev.features["triangle"]
    assert f["flow"]["score"] == pytest.approx(-0.8) and f["trigger"] == "flow"
    assert "FLOW score -0.80" in e.signal_text(ev) and "first mover coinbase" in e.signal_text(ev)
    assert e.snapshot()["triangle"]["flow"] == {"ticks": 0, "scored": 0, "fired": 0}


def test_a_taken_flow_signal_is_not_traded_twice_in_the_same_market():
    e = _flow_engine()
    twap = _Twap()
    ev = _flow_shock(e, 0.0, flow=_result(1))
    assert ev.tradeable
    from test_triangle import _book as b2, _eval as ev2                    # noqa: F401
    again = _eval(e, twap, NOW + 5.0, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    assert not again.tradeable                                              # the market already took it


def test_the_settings_read_from_the_environment():
    c = TriangleConfig.from_env({"TPB_TRIANGLE_ENABLED": "1", "TPB_TRIANGLE_SIGNAL_SOURCE": "flow",
                                 "TPB_TRIANGLE_FLOW_REF_BP": "0.6", "TPB_TRIANGLE_FLOW_THRESHOLD": "0.65",
                                 "TPB_TRIANGLE_FLOW_WINDOWS_MS": "20, 50, 100"})
    p = c.flow_params()
    assert c.signal_source == "flow" and p.ref_bp == 0.6 and p.threshold == 0.65 and p.windows_ms == (20.0, 50.0, 100.0)
    assert p.exchanges == ("coinbase", "bybit", "binance") and (p.w_cx, p.w_flow, p.w_acc, p.w_first) == (0.35, 0.30, 0.20, 0.15)
    assert TriangleConfig().signal_source == "spot_lag"


# ───────────────────────────── on the live bot ─────────────────────────────


def test_the_live_bot_scores_every_quote_and_trade_and_evaluates_on_a_signal(tmp_path, monkeypatch):
    from troll_poly_bot.feeds.spot import SpotQuote, TradeTick
    bot = _bot(tmp_path, signal_source="flow")
    assert bot.engine.flow is not None and _bot(tmp_path / "plain").engine.flow is None
    calls = []
    monkeypatch.setattr(bot, "_maybe_shock_eval",
                        lambda asset, now, updated=False, force=False: calls.append((asset, force)))
    clock = bot.clock.now()
    for step in range(0, 63):
        ms = step * 10.0
        for x in EX:
            m = _mid(x, ms)
            bot._on_quote(SpotQuote("BTC", x, m - 0.5, m + 0.5, m, clock + ms, clock + ms))        # noqa: SLF001
        if ms >= 500.0:
            for x in EX:
                bot._on_trade(TradeTick("BTC", x, BASE, 0.08, True, clock + ms, clock + ms))      # noqa: SLF001
    assert bot.engine.flow.ticks >= 63 * 3
    assert bot.engine.flow.fired >= 1 and calls and calls[0] == ("BTC", True)
    assert bot.flow_signals >= 1


def test_the_calibrated_defaults_are_the_replayed_ones():
    p = TriangleConfig().flow_params()
    assert (p.ref_bp, p.accel_ref_bp, p.min_bp, p.gate_bp, p.threshold) == (0.6, 0.6, 0.2, 0.15, 0.70)
