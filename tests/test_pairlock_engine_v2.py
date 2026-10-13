"""Strategy B (strategy/pairlock_engine_v2.py on the frozen pairlock_v2 / pairlock_book_v2): the engine selection, and
the v2 engine's behaviour tests from before the 2026-10-02 rewrite, unchanged in substance -- the restored engine must
behave exactly as the one that ran."""
from __future__ import annotations

import time
from collections import deque

import pytest
from test_flowsignal import _result
from test_spot_lag import NOW, SLUG, _book, _meta, _Twap, _Vol
from test_triangle import _bot, _cfg

from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.strategy import pairlock_book_v2 as pb
from troll_poly_bot.strategy import pairlock_v2 as pl
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.pairlock_book_v2 import State
from troll_poly_bot.strategy.pairlock_engine import PairLockEngine, pairlock_strategy
from troll_poly_bot.strategy.pairlock_engine_v2 import PairLockConfig, PairLockEngineV2
from troll_poly_bot.types import Fill, Side, TimeInForce

VIEW = CompositeView("BTC", 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0, "coinbase": 100.0, "bybit": 100.0})
TRI = dict(signal_source="flow", hedge_mode="dynamic", shares=10.0, limit_offset=0.03, wait_for_pair=False,
           pair_cost=0.0, max_order_usdc=10.0)


def _model(intercept=0.0):
    m = pl.ProbModel(beta=[intercept, 1.0] + [0.0] * (len(pl.FEATURES) - 1))
    m.exec = {"entry_fill_rate": 1.0, "entry_slip": 0.0, "entry_adv": 0.0, "hedge_fill_rate": 1.0, "hedge_slip": 0.0}
    return m


def _ple(prm=None, lim=None, model=None):
    scfg = _cfg(**TRI)
    rc = RiskConfig()
    scfg.apply_budget(rc)
    plc = PairLockConfig(model_path="/nonexistent.json",
                         params=prm or pl.PairLockParams(entry_edge=0.0, band_lo=0.20, band_hi=0.96),
                         limits=lim or pl.RiskLimits())
    e = PairLockEngineV2(EngineConfig(strategy="triangle"), scfg, RiskManager(cfg=rc, starting_balance=500.0),
                         FeatureEngine(), plcfg=plc)
    e.plmodel = model or _model()
    for t in range(int(NOW - 12_000), int(NOW), 200):
        e._record(_meta("BTC"), _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50), VIEW, float(t))   # noqa: SLF001
    e.sigma[SLUG] = 1e-4
    return e


def _feed_to(e, now):
    tw = getattr(e, "_twap_test", None)
    start = NOW - 70_000.0 if tw is None else e._fed_until + 200.0
    if tw is None:
        tw = e._twap_test = _Twap()
    t = start
    while t <= now:
        e.features.update_spot("BTC", 100.0, t)
        tw.update(100.0, t)
        t += 200.0
    e._fed_until = t - 200.0
    return tw


def _ticks(e, now, up):
    h = e.hist[SLUG]
    t = h.t[-1] + 200.0
    down = (round(1 - up[1], 2), round(1 - up[0], 2))
    while t < now:
        e._record(_meta("BTC"), _book("UP", *up), _book("DOWN", *down), VIEW, t)   # noqa: SLF001
        t += 200.0


def _ev(e, now, up=(0.50, 0.52), signal=None):
    _ticks(e, now, up)
    if signal is not None:
        e.flow.recent.setdefault("BTC", deque(maxlen=64)).append(signal)
    tw = _feed_to(e, now)
    down = (round(1 - up[1], 2), round(1 - up[0], 2))
    return e.evaluate(_meta("BTC"), _book("UP", *up), _book("DOWN", *down), VIEW, 200.0, 100.0, now, _Vol(), tw,
                      COLOCATED, 500.0)


def _fill(e, o, price, size, now):
    f = Fill(o.order_id, o.token_id, Side.BUY, price, size, size * pl.fee_per_share(price), now, now, now,
             expected_price=o.expected_price)
    e.on_fill_record(SLUG, o, f, now)


def _enter(e, now=NOW, price=0.52):
    ev = _ev(e, now, signal=_result(1, t=now))
    assert ev.tradeable, (ev.reason, ev.detail)
    o = e.to_order(ev, now)
    e.entry_result(o.order_id, True)
    _fill(e, o, price, ev.size, now + 300.0)
    return ev, o


def _hedging_engine(**kw):
    prm = pl.PairLockParams(entry_edge=0.0, band_lo=0.20, band_hi=0.96, rev_thr=-99.0, confirm_ms=0.0,
                            min_hedge_ev=0.0, lock_opportunities=False, **kw)
    e = _ple(prm=prm, model=_model(1.0))
    _enter(e)
    e.plmodel = _model(-2.0)
    return e


# ─────────────────────────── selection ───────────────────────────


def test_strategy_b_selects_the_v2_engine_and_the_running_env_stays_b(tmp_path, monkeypatch):
    assert pairlock_strategy({"TPB_PAIRLOCK_STRATEGY": "B"}) == "B"
    assert pairlock_strategy({"TPB_PAIRLOCK_HEDGE_MODE": "conditional"}) == "B"     # the env a v2 run started with
    assert pairlock_strategy({}) == "E" and pairlock_strategy({"TPB_PAIRLOCK_STRATEGY": "c"}) == "C"
    with pytest.raises(ValueError):
        pairlock_strategy({"TPB_PAIRLOCK_STRATEGY": "C", "TPB_PAIRLOCK_HEDGE_MODE": "combined"})
    monkeypatch.setenv("TPB_PAIRLOCK_STRATEGY", "B")
    monkeypatch.setenv("TPB_PAIRLOCK_HEDGE_MODE", "conditional")
    monkeypatch.setenv("TPB_PAIRLOCK_MIN_HEDGE_EV", "0.05")
    bot = _bot(tmp_path, **TRI)
    assert type(bot.engine) is PairLockEngineV2 and bot.engine.prm.min_hedge_ev == 0.05
    assert bot.engine.prm.hedge_mode == "conditional" and bot.engine.isolated_controls
    monkeypatch.delenv("TPB_PAIRLOCK_STRATEGY")
    assert type(_bot(tmp_path / "again", **TRI).engine) is PairLockEngineV2                # no letter, v2 mode: B
    monkeypatch.setenv("TPB_PAIRLOCK_STRATEGY", "E")
    monkeypatch.delenv("TPB_PAIRLOCK_HEDGE_MODE")
    monkeypatch.delenv("TPB_PAIRLOCK_MIN_HEDGE_EV")
    e = _bot(tmp_path / "e", **TRI).engine
    assert type(e) is PairLockEngine and e.prm.hedge_mode == "combined"


def test_the_ecosystem_file_runs_b_with_the_running_settings():
    import json
    import subprocess
    # since 2026-10-02 20:40 the app runs the user's conditions rule (strategy R, entry mode "conditions"); B's settings
    # stay in baseB, which the recovery and book A/B configs build on
    mod = json.loads(subprocess.check_output(
        ["node", "-e", "const m=require('/root/troll-poly-bot/ecosystem.pairlock.config.js');"
                       "console.log(JSON.stringify({app: m.apps[0].env, b: m.baseB.env}))"]))
    app, env = mod["app"], mod["b"]
    assert pairlock_strategy(app) == "R" and app["TPB_PAIRLOCK_REC_ENTRY_MODE"] == "conditions"
    assert env["TPB_PAIRLOCK_STRATEGY"] == "B" and pairlock_strategy(env) == "B"
    c = PairLockConfig.from_env(env)
    p = c.params
    assert (p.hedge_mode, p.rev_thr, p.confirm_ms, p.min_hedge_ev, p.min_pair_edge, p.adverse_min) == \
        ("conditional", 1.5, 400.0, 0.05, 0.05, 0.03)
    assert p.fractions == tuple(k / 10 for k in range(11)) and c.limits.max_pair_cost == 1.15
    assert (p.entry_edge, p.entry_mode, p.short_min, p.depth_haircut) == (0.02, "both", 0.55, 0.8)


# ─────────────────────────── the v2 behaviour ───────────────────────────


def test_the_entry_gate_and_the_candidate_states():
    e = _ple()
    ev = _ev(e, NOW, signal=_result(1))
    assert not ev.tradeable and "entry EV" in ev.detail and SLUG not in e.candidates
    e = _ple(model=_model(intercept=1.0))
    ev = _ev(e, NOW, signal=_result(1))
    assert ev.tradeable and ev.size == 10.0 and ev.limit_price == 0.55
    assert e.candidates[SLUG].state is State.INITIAL_ENTRY_CANDIDATE
    o = e.to_order(ev, NOW)
    cyc = e.cycles[SLUG][-1]
    assert cyc.state is State.INITIAL_ENTRY and o.order_id in cyc.book.pending and cyc.book.up_qty == 0
    e.entry_result(o.order_id, False, "quote_pulled")
    assert not e.cycles.get(SLUG) and e.stats["entry_kills"] == 1


def test_a_hedge_is_sent_killed_partially_filled_then_completed():
    e = _hedging_engine()
    t = NOW + 3000.0
    ev = _ev(e, t, up=(0.44, 0.46))
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "hedge", (ev.reason, ev.detail)
    assert ev.side == "DOWN" and ev.size == 10.0
    cyc = e.cycles[SLUG][-1]
    assert cyc.state is State.FULL_HEDGE
    o = e.to_order(ev, t)
    assert o.tif is TimeInForce.FOK and o.order_id in cyc.book.pending and cyc.book.down_qty == 0
    e.entry_result(o.order_id, False, "fok_unfillable")
    assert cyc.state in (State.MONITORING, State.ADVERSE_MOVE) and e.stats["hedge_kills"] == 1
    t2 = t + 6000.0
    ev = _ev(e, t2, up=(0.44, 0.46))
    o = e.to_order(ev, t2)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.56, 4.0, t2 + 300.0)
    assert (cyc.book.down_qty, cyc.book.matched_qty, cyc.book.unmatched_up) == (4.0, 4.0, 6.0)
    assert cyc.state is State.PAIR_FORMED and o.order_id in cyc.book.pending
    _fill(e, o, 0.57, 6.0, t2 + 300.0)
    assert cyc.book.down_qty == 10.0 and not cyc.book.pending
    assert cyc.state is State.PAIR_FORMED and cyc.book.pair_cost_per_share > 1.0
    assert cyc.closed and cyc.hedge_actions == 2


def test_b_allows_a_new_cycle_after_a_fully_matched_pair():
    """Strategy B's own rule (kept: it is what B is): a fully matched pair is a CLOSED cycle, so a later trigger may
    start the market's second cycle (max 2). The 2026-10-02 spec forbids this for A / C / D / E only."""
    e = _hedging_engine()
    t = NOW + 3000.0
    ev = _ev(e, t, up=(0.44, 0.46))
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.56, 10.0, t + 300.0)
    assert e.cycles[SLUG][-1].closed
    e.plmodel = _model(1.0)
    ev2 = _ev(e, t + 15_000.0, signal=_result(1, t=t + 15_000.0, i=3))
    assert ev2.tradeable and ev2.side == "UP", (ev2.reason, ev2.detail)


def test_a_lock_after_a_favourable_move():
    prm = pl.PairLockParams(entry_edge=0.0, band_lo=0.20, band_hi=0.96, rev_thr=99.0, min_hedge_ev=-1.0,
                            min_pair_edge=0.05, min_locked_profit=0.20)
    e = _ple(prm=prm, model=_model(1.0))
    _enter(e)
    e.plmodel = _model(0.0)
    t = NOW + 3000.0
    ev = _ev(e, t, up=(0.76, 0.78))
    assert ev.features["triangle"]["pairlock"]["kind"] == "FULL_HEDGE:lock"
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.24, 10.0, t + 300.0)
    cyc = e.cycles[SLUG][-1]
    assert cyc.state is State.PAIR_LOCKED and cyc.book.matched_qty == 10.0
    snap = e.snapshot()["pairlock"]
    assert snap["version"] == 2 and snap["strategy"] == "B" and "10 matched pairs locked" in snap["open"][0]["locked_label"]


def test_settlement_pending_then_settled_and_reconciled():
    e = _hedging_engine()
    cyc = e.cycles[SLUG][-1]
    m = _meta("BTC").market
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    assert cyc.state is State.SETTLEMENT_PENDING
    venue_pnl = cyc.book.resolution_pnl("DOWN")
    e.on_market_settled(SLUG, False, venue_pnl, m.close_ts + 40_000.0)
    assert cyc.state is State.SETTLED and SLUG not in e.cycles
    assert e.stats["settled_cycles"] == 1 and e.stats["recon_max_diff"] < 1e-9 and not e.accounting_halt
    e2 = _hedging_engine()
    e2.on_market_settled(SLUG, False, e2.cycles[SLUG][-1].book.resolution_pnl("DOWN") + 0.50, m.close_ts + 40_000.0)
    assert e2.accounting_halt and "venue" in e2.accounting_halt


def test_a_late_resolved_window_settles_after_a_restart_long_past_its_close():
    e = _hedging_engine()
    cyc = e.cycles[SLUG][-1]
    m = _meta("BTC").market
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    doc = e.persist_state()
    venue_pnl = cyc.book.resolution_pnl("UP")
    e2 = _ple()
    e2.restore_state(doc, m.close_ts + 3 * 3600_000.0)
    assert e2.holds_window(SLUG)
    e2.on_market_settled(SLUG, True, venue_pnl, m.close_ts + 3 * 3600_000.0)
    assert not e2.holds_window(SLUG) and e2.stats["settled_cycles"] == 1 and not e2.accounting_halt
    e3 = _ple()
    e3.restore_state(doc, m.close_ts + 3600_000.0)
    e3.forget_closed_windows(m.close_ts + 3600_000.0)
    assert not e3.holds_window(SLUG)


def test_the_live_bot_restores_the_same_b_position(tmp_path, monkeypatch):
    monkeypatch.setenv("TPB_PAIRLOCK_STRATEGY", "B")
    bot = _bot(tmp_path, **TRI)
    now = time.time() * 1000.0
    slug = f"btc-updown-5m-{int(now // 300_000) * 300}"
    cyc = pl.Cycle(book=pb.PairBook(slug), a_side="UP", state=State.NO_POSITION)
    cyc.to(State.INITIAL_ENTRY_CANDIDATE, now, "")
    pl.on_entry_sent(cyc, "e1", 10.0, 0.55, now)
    pl.on_fill(cyc, pb.FillRecord(slug, now, "up", "UP", 10.0, 0.52, 0.1747, "e1", "e1-1", expected_price=0.52),
               pl.PairLockParams(), now)
    pl.on_order_done(cyc, "e1", True, now)
    cyc.to(State.ADVERSE_MOVE, now, "test")
    pl.on_hedge_sent(cyc, "h1", "DOWN", 10.0, 0.58, now)
    pl.on_fill(cyc, pb.FillRecord(slug, now, "down", "DOWN", 4.0, 0.55, 4 * pl.fee_per_share(0.55), "h1", "h1-1",
                                  kind="hedge"), pl.PairLockParams(), now)
    bot.engine.cycles[slug] = [cyc]
    bot.engine.close_ts[slug] = now + 120_000.0
    before = cyc.book.summary(0.45, 0.50, 0.40)
    bot._persist_positions()                                                 # noqa: SLF001
    again = _bot(tmp_path, **TRI)
    assert type(again.engine) is PairLockEngineV2
    (back,) = again.engine.cycles[slug]
    assert back.book.summary(0.45, 0.50, 0.40) == {**before, "pending": []}
    assert back.hedge_actions == 1 and back.book.matched_qty == 4.0 and back.book.unmatched_up == 6.0


def test_refusal_codes():
    R = PairLockEngineV2._reason_for
    assert R("entry EV -0.030 < +0.020") is Reason.EDGE_TOO_SMALL and R("spread") is Reason.LOW_LIQUIDITY
    assert R("the previous cycle is still open (MONITORING)") is Reason.ALREADY_POSITIONED
