"""triangle: the price band (never buy under 0.05 or over 0.96) and the HIGH ZONE rule (a token above 0.85 that
is still pricing up buys 5 shares every 5 s), and the flow signal's own minimum time left."""
from __future__ import annotations

from collections import deque

import pytest

from test_spot_lag import NOW, SLUG, _book, _feed, _Twap
from test_triangle import _cfg, _engine, _eval, _shock

from troll_poly_bot.strategy.engine import Reason
from test_flowsignal import _result

BAND = dict(pair_cost=0.0, shares=5.0, max_order_usdc=5.0, limit_offset=0.03, wait_for_pair=False)


# ───────────────────────────── the price band ─────────────────────────────


@pytest.mark.parametrize("ask,ok", [(0.04, False), (0.19, False), (0.20, True), (0.96, True), (0.97, False)])
def test_no_buy_under_020_or_over_096(ask, ok):
    up_now = (round(max(ask - 0.02, 0.01), 2), ask)
    e, ev = _shock(6.0, up_now=up_now, scfg=_cfg(**BAND))
    assert ev.tradeable is ok, (ask, ev.reason, ev.detail)
    if not ok:
        assert ev.reason is Reason.PRICE_BAND and "outside the 0.20-0.96 band" in ev.detail
        assert e.shock_marks_for(SLUG)[-1]["ok"] is False                   # drawn hollow, with the reason
    else:
        assert ev.limit_price <= 0.96 + 1e-9                                 # the limit never exceeds the band


def test_the_limit_is_capped_at_the_band_and_a_fresh_ask_outside_it_is_not_sent():
    e, ev = _shock(6.0, up_now=(0.93, 0.95), scfg=_cfg(**BAND))
    assert ev.tradeable and ev.limit_price == 0.96                           # 0.95 + 3c, capped at 0.96
    assert e.revalidate(ev, _book("UP", 0.15, 0.19)) is Reason.PRICE_BAND    # the ask fell under 0.20 meanwhile
    assert e.revalidate(ev, _book("UP", 0.95, 0.97)) is Reason.PRICE_BAND
    assert e.revalidate(ev, _book("UP", 0.50, 0.52)) is None


# ───────────────────────────── the high zone ─────────────────────────────


def _walk(e, t0, t1, ask_of, side="UP", step=500.0):
    twap = _Twap()
    out, t = [], t0
    while t <= t1:
        _feed(e, twap, 0.0, until=t)
        a = ask_of(t)
        mine, other = _book(side, round(a - 0.02, 2), a), _book("DOWN" if side == "UP" else "UP", round(1 - a, 2), round(1 - a + 0.02, 2))
        up, down = (mine, other) if side == "UP" else (other, mine)
        out.append((t, _eval(e, twap, t, up, down)))
        t += step
    return out


def _hz_engine(**kw):
    return _engine(_cfg(signal_source="flow", hz_enabled=True, **{**BAND, **kw}))


def test_a_token_over_085_still_rising_buys_5_shares_every_5_seconds():
    e = _hz_engine()
    start = NOW - 6000.0
    rows = _walk(e, start, NOW + 4500.0, lambda t: round(0.86 + 0.01 * (t - start) / 1000.0, 2))
    bought = [(t, ev) for t, ev in rows if ev.tradeable]
    assert [round(t - NOW) for t, _ in bought] == [-1000, 4000]               # needs 5 s of history, then every 5 s
    t1, ev1 = bought[0]
    assert (ev1.side, ev1.token_id, ev1.size) == ("UP", "UP", 5.0) and ev1.expected_price == 0.91
    f = ev1.features["triangle"]
    assert f["trigger"] == "high_zone" and f["hz"]["ask_then"] == 0.86 and f["hz"]["lookback_s"] == 5.0
    assert "HIGH ZONE UP ask 0.91 still rising" in e.signal_text(ev1)
    marks = e.shock_marks_for(SLUG)
    assert [m["trigger"] for m in marks] == ["high_zone", "high_zone"] and all(m["ok"] for m in marks)
    assert bought[1][1].limit_price == 0.96 and e.snapshot()["triangle"]["hz_triggers"] == 2
    o = e.to_order(ev1, t1)
    assert o.size == 5.0 and o.token_id == "UP"


def test_the_down_token_counts_too():
    e = _hz_engine()
    start = NOW - 6000.0
    rows = _walk(e, start, NOW + 100.0, lambda t: round(0.86 + 0.01 * (t - start) / 1000.0, 2), side="DOWN")
    bought = [ev for _, ev in rows if ev.tradeable]
    assert len(bought) == 1 and (bought[0].side, bought[0].token_id) == ("DOWN", "DOWN")


def test_flat_falling_or_low_tokens_do_not_trigger():
    for ask_of in (lambda t: 0.90,                                            # high but not rising
                   lambda t: round(0.95 - 0.01 * (t - (NOW - 6000.0)) / 1000.0, 2),   # falling
                   lambda t: round(0.70 + 0.01 * (t - (NOW - 6000.0)) / 1000.0, 2)):  # rising but under 0.85
        e = _hz_engine()
        rows = _walk(e, NOW - 6000.0, NOW + 4500.0, ask_of)
        assert not any(ev.tradeable for _, ev in rows)
    e = _engine(_cfg(signal_source="flow", hz_enabled=False, **BAND))        # the rule off
    start = NOW - 6000.0
    assert not any(ev.tradeable for _, ev in _walk(e, start, NOW + 100.0,
                                                   lambda t: round(0.86 + 0.01 * (t - start) / 1000.0, 2)))


def test_a_high_zone_buy_above_the_pair_room_is_refused_but_still_counts_the_5_seconds():
    e = _hz_engine(pair_cost=0.96)                                            # pair room: buys stop at 0.95
    start = NOW - 6000.0
    rows = _walk(e, start, NOW + 4500.0, lambda t: round(0.86 + 0.01 * (t - start) / 1000.0, 2))
    bought = [round(t - NOW) for t, ev in rows if ev.tradeable]
    assert bought == [-1000]                                                  # the 0.96 buy at +4000 leaves no room for a pair
    assert any("no room for a pair" in (ev.detail or "") for _, ev in rows)


# ───────────────────── a flow signal needs 60 s left ─────────────────────


def test_a_flow_signal_late_in_the_window_is_ignored():
    e = _engine(_cfg(signal_source="flow", flow_min_left_s=200.0, **BAND))    # 180 s left < 200
    twap = _Twap()
    _feed(e, twap, 0.0, until=NOW)
    e.flow.recent.setdefault("BTC", deque(maxlen=64)).append(_result(1))
    ev = _eval(e, twap, NOW, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50))
    assert not ev.tradeable and "no flow signal" in ev.detail and e.shock_marks_for(SLUG) == []
    e2 = _engine(_cfg(signal_source="flow", flow_min_left_s=60.0, **BAND))
    twap2 = _Twap()
    _feed(e2, twap2, 0.0, until=NOW)
    e2.flow.recent.setdefault("BTC", deque(maxlen=64)).append(_result(1))
    assert _eval(e2, twap2, NOW, _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50)).tradeable


def test_the_dashboard_sets_the_band_and_the_high_zone(tmp_path):
    from test_triangle import _bot
    from troll_poly_bot.control import apply_to, current_values, validate
    bot = _bot(tmp_path)
    t = bot._control_targets()                                                # noqa: SLF001
    cur = current_values(bot.cfg, bot.exchange, **t)
    assert cur["triangle.min_price"] == 0.20 and cur["triangle.max_price"] == 0.96
    clean, errors = validate({"triangle.hz_enabled": True, "triangle.hz_min_price": 0.9, "triangle.hz_every_s": 3,
                              "triangle.max_price": 0.95})
    assert errors == []
    apply_to(clean, bot.cfg, bot.exchange, **t)
    c = bot.cfg.triangle
    assert (c.hz_enabled, c.hz_min_price, c.hz_every_s, c.max_price) == (True, 0.9, 3.0, 0.95)


# ───────────────────── no chasing: a token that already repriced is not bought ─────────────────────


def test_a_flow_signal_on_a_token_that_already_rose_is_skipped_as_the_top():
    from test_flowsignal import _result
    NC = dict(signal_source="flow", flow_max_token_move=0.01, flow_token_lookback_ms=1000.0, **BAND)
    e = _engine(_cfg(**NC))
    twap = _Twap()
    _feed(e, twap, 0.0, until=NOW - 1200.0)
    _eval(e, twap, NOW - 1200.0, _book("UP", 0.48, 0.50), _book("DOWN", 0.48, 0.50))     # UP ask 0.50 a second ago
    _feed(e, twap, 0.0, until=NOW)
    e.flow.recent.setdefault("BTC", deque(maxlen=64)).append(_result(1))
    ev = _eval(e, twap, NOW, _book("UP", 0.52, 0.54), _book("DOWN", 0.44, 0.46))        # now 0.54: +4c already
    assert ev.reason is Reason.TOKEN_ALREADY_MOVED and "already repriced +4c" in ev.detail
    assert e.shock_marks_for(SLUG)[-1]["ok"] is False and "not chasing" in e.shock_marks_for(SLUG)[-1]["why"]
    # the same signal on a token that has not moved yet is bought
    e2 = _engine(_cfg(**NC))
    twap2 = _Twap()
    _feed(e2, twap2, 0.0, until=NOW - 1200.0)
    _eval(e2, twap2, NOW - 1200.0, _book("UP", 0.48, 0.50), _book("DOWN", 0.48, 0.50))
    _feed(e2, twap2, 0.0, until=NOW)
    e2.flow.recent.setdefault("BTC", deque(maxlen=64)).append(_result(1))
    ev2 = _eval(e2, twap2, NOW, _book("UP", 0.49, 0.51), _book("DOWN", 0.47, 0.49))       # +1c: within the allowance
    assert ev2.tradeable, (ev2.reason, ev2.detail)
    # off by default
    e3 = _engine(_cfg(signal_source="flow", **BAND))
    assert e3.scfg.flow_max_token_move < 0


def test_the_dashboard_sets_the_no_chase_allowance(tmp_path):
    from test_triangle import _bot
    from troll_poly_bot.control import apply_to, validate
    bot = _bot(tmp_path)
    clean, errors = validate({"triangle.flow_max_token_move": 0.0})
    assert errors == [] and clean == {"triangle.flow_max_token_move": 0.0}
    apply_to(clean, bot.cfg, bot.exchange, **bot._control_targets())              # noqa: SLF001
    assert bot.cfg.triangle.flow_max_token_move == 0.0
