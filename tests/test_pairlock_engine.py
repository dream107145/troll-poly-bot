"""strategy/pairlock_engine.py on the live path (2026-10-02 recovery / profit-lock spec): the entry candidate and order
hooks, inventory from FILLS only (order / fill ids, expected price), the recovery hedge (sent, killed = incomplete,
partially filled, filled), the profit lock after a favourable move, a hedge the bot did not send, REENTRY_BLOCKED,
SETTLEMENT_PENDING -> MARKET_RESOLVED -> SETTLED with the three-way reconciliation and the accounting halt, a restart
through the bot's positions file, the dashboard block."""
from __future__ import annotations

import math
import time
from collections import deque

from test_flowsignal import _result
from test_spot_lag import NOW, SLUG, _book, _meta, _Twap, _Vol
from test_triangle import _bot, _cfg

from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.strategy import pairlock as pl
from troll_poly_bot.strategy import pairlock_book as pb
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.pairlock_book import State
from troll_poly_bot.strategy.pairlock_engine import PairLockConfig, PairLockEngine
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
    e = PairLockEngine(EngineConfig(strategy="triangle"), scfg, RiskManager(cfg=rc, starting_balance=500.0),
                       FeatureEngine(), plcfg=plc)
    e.plmodel = model or _model()
    for t in range(int(NOW - 12_000), int(NOW), 200):                     # 12 s of history, sigma known
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


def test_the_entry_gate_and_the_candidate_states():
    e = _ple()                                                             # the market's own price: EV < 0
    ev = _ev(e, NOW, signal=_result(1))
    assert not ev.tradeable and "entry EV" in ev.detail and SLUG not in e.candidates
    e = _ple(model=_model(intercept=1.0))                                  # P(UP) ~0.74 at ask 0.52
    ev = _ev(e, NOW, signal=_result(1))
    assert ev.tradeable, (ev.reason, ev.detail)
    assert ev.size == 10.0 and ev.limit_price == 0.55
    assert e.candidates[SLUG].state is State.WAITING and e.candidates[SLUG].entry_info["model_p"] > 0.7
    o = e.to_order(ev, NOW)
    cyc = e.cycles[SLUG][-1]
    assert cyc.state is State.INITIAL_ENTRY and o.order_id in cyc.book.pending and cyc.book.up_qty == 0
    e.entry_result(o.order_id, False, "quote_pulled")                      # killed: nothing held, no cycle left
    assert not e.cycles.get(SLUG) and e.stats["entry_kills"] == 1


def test_inventory_comes_from_fills_with_their_ids():
    e = _ple(model=_model(1.0))
    ev, o = _enter(e)
    cyc = e.cycles[SLUG][-1]
    assert cyc.state is State.DIRECTIONAL_POSITION and not cyc.book.pending
    (f,) = cyc.book.fills
    assert (f.order_id, f.fill_id, f.qty, f.price, f.side, f.kind) == (o.order_id, f"{o.order_id}-1", 10.0, 0.52, "UP", "entry")
    assert f.expected_price == o.expected_price and cyc.book.up_avg_cost == 0.52
    assert e.persist_state()["pairlock_v2"][SLUG]["cycles"][0]["book"]["fills"][0]["fill_id"] == f.fill_id


def test_a_fill_without_its_order_halts_entries():
    e = _ple(model=_model(1.0))
    from troll_poly_bot.types import Order
    stray = Order(token_id="UP", side=Side.BUY, price=0.5, size=5.0, tif=TimeInForce.FOK)
    e.on_fill_record(SLUG, stray, Fill(stray.order_id, "UP", Side.BUY, 0.5, 5.0, 0.0, NOW, NOW, NOW), NOW)
    assert e.accounting_halt
    ev = _ev(e, NOW, signal=_result(1))
    assert not ev.tradeable and "accounting halt" in ev.detail


def test_no_new_entry_while_a_cycle_is_exposed():
    e = _ple(model=_model(1.0))
    _enter(e)
    ev = _ev(e, NOW + 3000.0, signal=_result(1, t=NOW + 3000.0, i=2))
    assert not ev.tradeable and ev.reason is Reason.ALREADY_POSITIONED and "REENTRY_BLOCKED" in ev.detail
    assert e.stats["reentry_blocked"] == 1
    assert e.cycles[SLUG][-1].events[-1].name == "REENTRY_BLOCKED"


#: every reversal signal toward DOWN (the engine's history in these tests is flat, so the features are given)
AGAINST_UP = dict(tv1=-5.0, tv3=-5.0, tacc=-3.0, mom1=-3.0, mom3=-3.0, imb=-0.5, pmflow=-0.5, agree=-1.0)


def _against(p_up=0.10):
    f = {k: 0.0 for k in pl.FEATURES}
    f.update({"logit_mid": math.log(p_up / (1 - p_up)), "mid_up": 0.45, "left": 150.0, **AGAINST_UP})
    return f


def _hedging_engine(**kw):
    """An open UP cycle (10 @ 0.52), a reversal on every signal, a pair-cost cap above 1 (the only way a recovery
    hedge can qualify on mirrored books) and a model that prices DOWN well above its ask."""
    prm = pl.PairLockParams(entry_edge=0.0, band_lo=0.20, band_hi=0.96, max_acceptable_pair_cost=1.30,
                            recovery_confirmation_window_ms=0.0, **kw)
    e = _ple(prm=prm, model=_model(1.0))
    _enter(e)
    e._features = lambda meta, now: _against()                              # noqa: SLF001
    return e


def test_a_hedge_is_sent_killed_partially_filled_then_completed():
    e = _hedging_engine()
    t = NOW + 3000.0
    ev = _ev(e, t, up=(0.44, 0.46))                                        # UP's bid 8c under its 0.52 fill
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "hedge", (ev.reason, ev.detail)
    assert ev.side == "DOWN" and ev.size == 10.0 and ev.features["triangle"]["pairlock"]["kind"] == "recovery"
    cyc = e.cycles[SLUG][-1]
    assert cyc.state is State.RECOVERY_EVALUATION
    assert e.revalidate(ev, _book("DOWN", 0.97, 0.98)) is None             # a hedge ignores the entry band
    o = e.to_order(ev, t)
    assert o.tif is TimeInForce.FOK and o.order_id in cyc.book.pending and cyc.book.down_qty == 0
    assert cyc.state is State.RECOVERY_HEDGE_PENDING and e.stats["recovery_hedges_sent"] == 1
    e.entry_result(o.order_id, False, "fok_unfillable")                     # an incomplete hedge
    assert cyc.state in (State.REVERSAL_CONFIRMED, State.ADVERSE_MOVE) and cyc.book.down_qty == 0
    assert e.stats["recovery_hedge_kills"] == 1
    t2 = t + 6000.0                                                         # after the cooldown
    ev = _ev(e, t2, up=(0.44, 0.46))
    o = e.to_order(ev, t2)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.56, 4.0, t2 + 300.0)                                     # 4 of 10 so far
    assert (cyc.book.down_qty, cyc.book.matched_qty, cyc.book.unmatched_up) == (4.0, 4.0, 6.0)
    assert cyc.state is State.RECOVERY_HEDGE_PENDING and o.order_id in cyc.book.pending
    _fill(e, o, 0.57, 6.0, t2 + 300.0)
    assert cyc.book.down_qty == 10.0 and not cyc.book.pending
    assert cyc.book.down_avg_cost == (4 * 0.56 + 6 * 0.57) / 10
    assert cyc.state is State.RECOVERY_HEDGED and cyc.book.pair_cost_per_share > 1.0   # a locked LOSS: not PAIR_LOCKED
    assert cyc.hedge_actions == 2 and e.stats["recovery_hedges_filled"] == 1 and e.stats["recovery_qty"] == 10.0
    assert e.stats["recovery_downside_reduction"] > 0
    names = [ev_.name for ev_ in cyc.events]
    assert "RECOVERY_HEDGE_SENT" in names and "RECOVERY_HEDGE_FILLED" in names and "PAIR_FORMED" in names


def test_after_a_recovery_hedge_the_market_never_starts_another_cycle():
    e = _hedging_engine()
    t = NOW + 3000.0
    ev = _ev(e, t, up=(0.44, 0.46))
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.56, 10.0, t + 300.0)
    cyc = e.cycles[SLUG][-1]
    assert cyc.state is State.RECOVERY_HEDGED
    e._features = lambda meta, now: dict(_against(0.9), **{k: -v for k, v in AGAINST_UP.items()})   # noqa: SLF001
    ev2 = _ev(e, t + 15_000.0, up=(0.60, 0.62), signal=_result(1, t=t + 15_000.0, i=3))   # the 06:55 case: UP again
    assert not ev2.tradeable and "REENTRY_BLOCKED" in ev2.detail
    assert len(e.cycles[SLUG]) == 1 and e.stats["entries"] == 1
    assert any(x.name == "REENTRY_BLOCKED" for x in cyc.events)


def test_a_profit_lock_after_a_favourable_move():
    prm = pl.PairLockParams(entry_edge=0.0, band_lo=0.20, band_hi=0.96, min_pair_edge=0.05, min_locked_profit=0.20)
    e = _ple(prm=prm, model=_model(1.0))
    _enter(e)
    f = {k: 0.0 for k in pl.FEATURES}
    f.update({"logit_mid": math.log(0.77 / 0.23), "mid_up": 0.77, "left": 150.0})
    e._features = lambda meta, now: f                                       # noqa: SLF001
    t = NOW + 3000.0
    ev = _ev(e, t, up=(0.76, 0.78))                                        # DOWN offered at 0.24
    assert ev.features["triangle"]["trigger"] == "hedge" and ev.features["triangle"]["pairlock"]["kind"] == "profit_lock"
    assert e.stats["profit_lock_opportunities"] == 1
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.24, 10.0, t + 300.0)
    cyc = e.cycles[SLUG][-1]
    assert cyc.state is State.PAIR_LOCKED and cyc.book.matched_qty == 10.0 and e.stats["pair_locked"] == 1
    assert e.stats["profit_lock_fills"] == 1 and e.stats["recovery_hedges_sent"] == 0
    snap = e.snapshot()["pairlock"]
    (row,) = snap["open"]
    assert row["matched_qty"] == 10.0 and row["unmatched_up"] == 0 and "10 matched pairs locked" in row["locked_label"]
    assert snap["profit_lock"]["filled"] == 1 and snap["recovery"]["filled"] == 0


def test_a_hedge_the_bot_did_not_send_restores_the_state():
    e = _hedging_engine()
    t = NOW + 3000.0
    ev = _ev(e, t, up=(0.44, 0.46))
    assert ev.features["triangle"]["trigger"] == "hedge"
    e.entry_refused(ev, "entries paused by the guard (FEED_DOWN)")
    cyc = e.cycles[SLUG][-1]
    assert cyc.state in pb.WATCH_STATES and cyc.hedge_actions == 0 and SLUG not in e.pending
    ev = _ev(e, t + 6000.0, up=(0.44, 0.46))
    o = e.to_order(ev, t + 6000.0)
    e.entry_refused(ev, "pre-send check failed")                            # to_order ran, the order never left
    assert cyc.hedge_actions == 0 and cyc.state in pb.WATCH_STATES and e.stats["recovery_hedges_sent"] == 0
    assert SLUG not in e.pending and not cyc.book.pending


def test_settlement_pending_then_settled_and_reconciled():
    e = _hedging_engine()
    cyc = e.cycles[SLUG][-1]
    m = _meta("BTC").market
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    assert cyc.state is State.SETTLEMENT_PENDING
    venue_pnl = cyc.book.resolution_pnl("DOWN")
    e.on_market_settled(SLUG, False, venue_pnl, m.close_ts + 40_000.0)
    assert cyc.state is State.SETTLED and SLUG not in e.cycles
    s = e.stats
    assert s["settled_cycles"] == 1 and abs(s["pnl"] - venue_pnl) < 1e-9 and s["recon_max_diff"] < 1e-9
    assert s["prediction"] == s["pnl"]                                       # unhedged: the baseline IS the result
    assert not e.accounting_halt and s["recon_failures"] == 0
    e2 = _hedging_engine()
    e2.on_market_settled(SLUG, False, e2.cycles[SLUG][-1].book.resolution_pnl("DOWN") + 0.50, m.close_ts + 40_000.0)
    assert e2.accounting_halt and "venue" in e2.accounting_halt                # a mismatch stops new entries


def test_a_market_cannot_disappear_from_the_engine():
    e = _ple()
    e.on_market_settled("btc-updown-5m-9", True, 0.0, NOW)                  # never traded: nothing to check
    assert not e.accounting_halt and e.stats["recon_checked"] == 0
    e.on_market_settled("btc-updown-5m-9", True, 2.75, NOW)                 # the bot booked P&L the engine never held
    assert e.accounting_halt and "holds no cycle" in e.accounting_halt and e.stats["recon_failures"] == 1


def test_the_entry_records_its_signal_ages_and_latency():
    e = _ple(model=_model(1.0))
    _enter(e)
    cyc = e.cycles[SLUG][-1]
    info = cyc.entry_info
    assert {"token", "qty", "executable_vwap", "model_p", "signal", "spot_age_ms", "quote_age_ms", "decision_t"} <= set(info)
    assert info["spot_age_ms"] == 200.0
    filled = next(x for x in cyc.events if x.name == "INITIAL_ENTRY_FILLED")
    assert filled.data["decision_to_fill_ms"] == 300.0 and filled.data["price"] == 0.52


def test_the_env_settings_and_no_automatic_pair(caplog):
    import logging
    e = _ple()
    assert e.pair_order(_meta("BTC").market, "UP", 10.0, 0.5, NOW, other_ask=0.4) is None
    with caplog.at_level(logging.WARNING, logger="troll_poly_bot.strategy.pairlock_engine"):
        c = PairLockConfig.from_env({"TPB_PAIRLOCK_ENTRY_EDGE": "0.02", "TPB_PAIRLOCK_HEDGE_MODE": "recovery",
                                     "TPB_PAIRLOCK_FRACTIONS": "0,0.5,1.0", "TPB_PAIRLOCK_MIN_HEDGE_EV": "-0.05",
                                     "TPB_PAIRLOCK_MIN_PAIR_EDGE": "0.04", "TPB_PAIRLOCK_MIN_LOCKED_PROFIT": "0.1",
                                     "TPB_PAIRLOCK_RECOVERY_MIN_LOSS": "0.8", "TPB_PAIRLOCK_RISK_AVERSION": "0.5",
                                     "TPB_PAIRLOCK_MAX_ACCEPTABLE_PAIR_COST": "0.99",
                                     "TPB_PAIRLOCK_PROFIT_LOCK_MAX_COST": "0.93",
                                     "TPB_PAIRLOCK_LIM_MAX_HEDGE_ACTIONS": "1", "TPB_PAIRLOCK_LIM_MAX_QUOTE_AGE_MS": "500",
                                     "TPB_PAIRLOCK_MODEL_PATH": "x.json", "TPB_PAIRLOCK_ENTRY_GATE": "0"})
    p = c.params
    assert (p.entry_edge, p.hedge_mode, p.fractions, p.min_pair_edge, p.min_locked_profit) == \
        (0.02, "recovery", (0.0, 0.5, 1.0), 0.04, 0.1)
    assert (p.recovery_min_loss, p.risk_aversion, p.max_acceptable_pair_cost, p.profit_lock_max_cost) == (0.8, 0.5, 0.99, 0.93)
    assert c.limits.max_hedge_actions == 1 and c.limits.max_quote_age_ms == 500.0
    assert c.model_path == "x.json" and c.entry_gate is False
    assert any("MIN_HEDGE_EV is OBSOLETE" in r.getMessage() for r in caplog.records)


def test_the_live_bot_runs_it_and_a_restart_rebuilds_the_same_position(tmp_path):
    bot = _bot(tmp_path, **TRI)
    assert type(bot.engine) is PairLockEngine
    now = time.time() * 1000.0
    slug = f"btc-updown-5m-{int(now // 300_000) * 300}"
    cyc = pl.Cycle(book=pb.PairBook(slug), a_side="UP")
    pl.on_entry_sent(cyc, "e1", 10.0, 0.55, now)
    pl.on_fill(cyc, pb.FillRecord(slug, now, "up", "UP", 10.0, 0.52, 0.1747, "e1", "e1-1", expected_price=0.52),
               pl.PairLockParams(), now)
    pl.on_order_done(cyc, "e1", True, now)
    cyc.to(State.ADVERSE_MOVE, now, "test")
    cyc.to(State.REVERSAL_CONFIRMED, now, "test")
    cyc.to(State.RECOVERY_EVALUATION, now, "test")
    pl.on_hedge_sent(cyc, "h1", "DOWN", 10.0, 0.58, now, "recovery")
    pl.on_fill(cyc, pb.FillRecord(slug, now, "down", "DOWN", 4.0, 0.55, 4 * pl.fee_per_share(0.55), "h1", "h1-1",
                                  kind="recovery"), pl.PairLockParams(), now)
    bot.engine.cycles[slug] = [cyc]
    bot.engine.close_ts[slug] = now + 120_000.0
    before = cyc.book.summary(0.45, 0.50, 0.40)
    bot._persist_positions()                                                 # noqa: SLF001
    again = _bot(tmp_path, **TRI)                                            # restores at construction
    (back,) = again.engine.cycles[slug]
    assert back.book.summary(0.45, 0.50, 0.40) == {**before, "pending": []}   # the in-flight order is gone, not inventory
    assert back.hedge_actions == 1 and back.book.matched_qty == 4.0 and back.book.unmatched_up == 6.0
    assert back.state is State.UNMATCHED_DIRECTIONAL and back.recovery_qty == 4.0
    assert _bot(tmp_path / "plain", signal_source="flow").engine.__class__.__name__ == "TriangleEngine"


def test_the_shared_dashboard_triangle_controls_do_not_steer_it(tmp_path):
    from troll_poly_bot.control import apply_to, current_values
    bot = _bot(tmp_path, **TRI)
    t = bot._control_targets()                                               # noqa: SLF001
    assert t["triangle"] is None
    assert "triangle.max_price" not in current_values(bot.cfg, bot.exchange, **t)
    before = bot.cfg.triangle.max_price
    apply_to({"triangle.max_price": 0.5, "triangle.limit_offset": 0.09}, bot.cfg, bot.exchange, **t)
    assert bot.cfg.triangle.max_price == before and bot.cfg.triangle.limit_offset == 0.03


def test_refusals_carry_their_own_reason_code():
    R = PairLockEngine._reason_for
    assert R("entry EV -0.030 < +0.020") is Reason.EDGE_TOO_SMALL and R("P(favourable) 0.4 < 0.55") is Reason.EDGE_TOO_SMALL
    assert R("spread") is Reason.LOW_LIQUIDITY and R("depth") is Reason.LOW_LIQUIDITY and R("no ask") is Reason.NO_OFFER
    assert R("ask 0.98 outside 0.20-0.96") is Reason.PRICE_BAND and R("45 s left") is Reason.OUTSIDE_TIME_WINDOW
    assert R("market capital") is Reason.EXPOSURE_CAP and R("market loss limit") is Reason.EXPOSURE_CAP
    assert R("REENTRY_BLOCKED: the market already holds inventory (RECOVERY_HEDGED)") is Reason.ALREADY_POSITIONED


def test_repeated_rejected_evaluations_are_logged_once_per_interval(caplog):
    import logging
    prm = pl.PairLockParams(entry_edge=0.0, band_lo=0.20, band_hi=0.96, profit_lock_risk_aversion=0.0,
                            min_pair_edge=0.05, min_locked_profit=0.20, reeval_ms=1000.0)
    e = _ple(prm=prm, model=_model(1.0))
    _enter(e)
    f = {k: 0.0 for k in pl.FEATURES}
    f.update({"logit_mid": math.log(0.9 / 0.1), "mid_up": 0.77, "left": 150.0})
    e._features = lambda meta, now: f                                       # noqa: SLF001
    cyc = e.cycles[SLUG][-1]
    with caplog.at_level(logging.INFO, logger="troll_poly_bot.strategy.pairlock_engine"):
        for k in range(8):                                                 # a lock on offer, rejected every 1.5 s
            _ev(e, NOW + 3000.0 + 1500.0 * k, up=(0.76, 0.78))
    rejected = [r for r in caplog.records if "PROFIT_LOCK_REJECTED" in r.getMessage()]
    evaluated = sum(1 for x in cyc.events if x.name == "PROFIT_LOCK_REJECTED")
    assert evaluated >= 6 and len(rejected) == 1                           # every one recorded, one printed
    assert e.stats["profit_lock_rejected"] == evaluated


def test_a_late_resolved_window_settles_after_a_restart_long_past_its_close():
    # 2026-10-02: five windows resolved late (LATE_SETTLE) stayed SETTLEMENT_PENDING in the engine forever;
    # a restart now keeps a pending cycle (up to 24 h) so the ledger's late result can settle it
    e = _hedging_engine()
    cyc = e.cycles[SLUG][-1]
    m = _meta("BTC").market
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    assert cyc.state is State.SETTLEMENT_PENDING
    doc = e.persist_state()
    venue_pnl = cyc.book.resolution_pnl("UP")
    e2 = _ple()
    e2.restore_state(doc, m.close_ts + 3 * 3600_000.0)
    assert e2.holds_window(SLUG)
    e2.on_market_settled(SLUG, True, venue_pnl, m.close_ts + 3 * 3600_000.0)
    assert not e2.holds_window(SLUG)
    assert e2.stats["settled_cycles"] == 1 and e2.stats["recon_max_diff"] < 1e-9 and not e2.accounting_halt
    e3 = _ple()
    e3.restore_state(doc, m.close_ts + 25 * 3600_000.0)                    # a day later it is dropped
    assert not e3.holds_window(SLUG)


def test_a_fresh_run_drops_restored_windows_past_the_close_that_nothing_will_settle():
    e = _hedging_engine()
    m = _meta("BTC").market
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    doc = e.persist_state()
    e2 = _ple()
    e2.restore_state(doc, m.close_ts + 3600_000.0)
    e2.forget_closed_windows(m.close_ts + 3600_000.0, keep={SLUG})         # a late resolution is queued: kept
    assert e2.holds_window(SLUG)
    e2.forget_closed_windows(m.close_ts + 3600_000.0)                      # nothing will settle it: dropped
    assert not e2.holds_window(SLUG)
