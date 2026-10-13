"""Strategy R on the live path (strategy/pairlock_recovery_engine.py): B's entry gate opens the position, the
recovery manager decides every further buy from the WHOLE position (both directions), a new entry trigger while
holding is REENTRY_BLOCKED, partial fills count what filled, settlement reconciles four ways, a restart rebuilds the
same inventory, and TPB_PAIRLOCK_STRATEGY=R selects the engine."""
from __future__ import annotations

import math
import time

import pytest
from test_flowsignal import _result
from test_pairlock_engine_v2 import TRI, VIEW, _ev, _fill, _model
from test_spot_lag import NOW, SLUG, _book, _meta
from test_triangle import _bot, _cfg

from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy import pairlock_v2 as plv2
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.pairlock_book import fee_per_share
from troll_poly_bot.strategy.pairlock_engine import pairlock_strategy
from troll_poly_bot.strategy.pairlock_engine_v2 import PairLockConfig
from troll_poly_bot.strategy.pairlock_recovery_engine import PairLockRecoveryEngine, recovery_params_from_env
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce

TOWARD_DOWN = dict(tv1=-5.0, tv3=-5.0, tacc=-3.0, mom1=-3.0, mom3=-3.0, imb=-0.5, pmflow=-0.5, agree=-1.0)
TOWARD_UP = {k: -v for k, v in TOWARD_DOWN.items()}


def _feat(p_up, **kw):
    f = {k: 0.0 for k in plv2.FEATURES}
    f.update({"logit_mid": math.log(p_up / (1 - p_up)), "mid_up": 0.5, "left": 150.0, **kw})
    return f


def _eng(rprm=None, model=None):
    scfg = _cfg(**{**TRI, "max_order_usdc": 30.0, "max_market_usdc": 40.0, "max_total_exposure_usdc": 80.0})
    rcfg = RiskConfig()
    scfg.apply_budget(rcfg)
    plc = PairLockConfig(model_path="/nonexistent.json", params=plv2.PairLockParams(entry_edge=0.0, band_lo=0.20, band_hi=0.96),
                         limits=plv2.RiskLimits())
    e = PairLockRecoveryEngine(EngineConfig(strategy="triangle"), scfg, RiskManager(cfg=rcfg, starting_balance=500.0),
                               FeatureEngine(), plcfg=plc, rprm=rprm or rc.RecoveryParams())
    e.plmodel = model or _model(1.0)
    for t in range(int(NOW - 12_000), int(NOW), 200):
        e._record(_meta("BTC"), _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50), VIEW, float(t))   # noqa: SLF001
    e.sigma[SLUG] = 1e-4
    return e


def _enter(e, now=NOW, price=0.52):
    ev = _ev(e, now, signal=_result(1, t=now))
    assert ev.tradeable, (ev.reason, ev.detail)
    o = e.to_order(ev, now)
    e.entry_result(o.order_id, True)
    _fill(e, o, price, ev.size, now + 300.0)
    return o


def _deep(token, bid, ask, size=1000.0):
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)])


def _eval_books(e, now, up, down):
    from test_pairlock_engine_v2 import _feed_to, _ticks
    from troll_poly_bot.execution.latency import COLOCATED
    from test_spot_lag import _Vol
    _ticks(e, now, (up[0], up[1]))
    tw = _feed_to(e, now)
    return e.evaluate(_meta("BTC"), _deep("UP", *up), _deep("DOWN", *down), VIEW, 200.0, 100.0, now, _Vol(), tw, COLOCATED, 500.0)


def test_the_entry_opens_one_portfolio_and_a_second_trigger_is_reentry_blocked():
    e = _eng()
    _enter(e)
    cyc = e.cycles[SLUG]
    assert cyc.state is rc.RState.HOLDING and cyc.book.up_qty == 10.0 and cyc.entry_qty == 10.0 and cyc.target == 0.5
    assert cyc.events[0]["event"] == "INITIAL_ENTRY" and cyc.events[1]["event"] == "ENTRY_FILLED"
    ev = _ev(e, NOW + 3000.0, signal=_result(1, t=NOW + 3000.0, i=2))
    assert not ev.tradeable and ev.reason is Reason.ALREADY_POSITIONED and "REENTRY_BLOCKED" in ev.detail
    assert e.stats["reentry_blocked"] == 1 and len(e.cycles) == 1


def test_a_recovery_order_through_the_live_path_then_the_other_way():
    e = _eng()
    _enter(e)                                                       # 10 UP @ 0.52
    cyc = e.cycles[SLUG]
    C = cyc.book.total_cost
    e.plmodel = _model(0.0)                                         # from here P(UP) = the feature's logit_mid
    # UP falls to 0.40, the book favours DOWN (every signal), P(UP) 0.30: buy DOWN
    e._features = lambda meta, now: _feat(0.30, **TOWARD_DOWN)      # noqa: SLF001
    t = NOW + 3000.0
    ev = _eval_books(e, t, up=(0.40, 0.41), down=(0.59, 0.60))
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "recovery" and ev.side == "DOWN", (ev.reason, ev.detail)
    x = math.ceil((0.5 + C) / (1 - 0.60 - fee_per_share(0.60) - 0.005) * 100) / 100
    assert ev.size == pytest.approx(x) and ev.limit_price == pytest.approx(0.63)      # best ask + MAX_SLIPPAGE
    o = e.to_order(ev, t)
    assert o.tif is TimeInForce.FOK and cyc.state is rc.RState.RECOVERY_PENDING and cyc.book.down_qty == 0
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.60, 6.0, t + 300.0)                                # a partial fill: 6 of x
    assert cyc.book.down_qty == 6.0 and cyc.state is rc.RState.RECOVERY_PENDING
    _fill(e, o, 0.60, x - 6.0, t + 300.0)
    assert cyc.state is rc.RState.HOLDING and cyc.book.down_qty == pytest.approx(x) and cyc.actions == 1
    o1 = rc.outcomes(cyc.book)
    assert o1["pnl_down_win"] >= 0.5 - 1e-6 and o1["pnl_up_win"] < 10 - C      # the UP-win branch is worse than before
    assert e.stats["recovery_fills"] == 1 and e.stats["recovery_down"] == 1 and SLUG not in e.pending
    # UP rallies to 0.70, the book favours UP, P(UP) 0.80: the whole position says buy UP (DOWN is 0.3 under its cost)
    e._features = lambda meta, now: _feat(0.80, **TOWARD_UP)        # noqa: SLF001
    e.rprm = rc.RecoveryParams(max_capital=40.0)
    t2 = t + 5000.0
    ev2 = _eval_books(e, t2, up=(0.70, 0.71), down=(0.29, 0.30))
    assert ev2.tradeable and ev2.side == "UP" and ev2.features["triangle"]["trigger"] == "recovery", (ev2.reason, ev2.detail)
    x2 = math.ceil((0.5 - o1["pnl_up_win"]) / (1 - 0.71 - fee_per_share(0.71) - 0.005) * 100) / 100
    assert ev2.size == pytest.approx(min(x2, 30.0))                  # capped by MAX_RECOVERY_ORDER_SIZE
    o2 = e.to_order(ev2, t2)
    e.entry_result(o2.order_id, True)
    _fill(e, o2, 0.71, ev2.size, t2 + 300.0)
    assert e.stats["recovery_up"] == 1 and cyc.actions == 2 and len(cyc.recoveries) == 3    # 2 fills + 1 fill, one row each
    assert rc.outcomes(cyc.book)["pnl_up_win"] > o1["pnl_up_win"]


def test_no_recovery_without_a_confirmed_reversal_and_the_evaluation_is_logged(caplog):
    import logging
    e = _eng()
    _enter(e)
    e._features = lambda meta, now: _feat(0.30)                     # noqa: SLF001  (losing, no signal)
    with caplog.at_level(logging.INFO, logger="troll_poly_bot.strategy.pairlock_recovery_engine"):
        ev = _eval_books(e, NOW + 3000.0, up=(0.40, 0.41), down=(0.59, 0.60))
    assert not ev.tradeable and e.stats["recovery_sent"] == 0
    lines = [r.getMessage() for r in caplog.records if "[RECOVERY_EVAL]" in r.getMessage()]
    assert lines and '"decision": "REJECTED"' in lines[-1] and "no confirmed reversal" in lines[-1]
    assert all(k in lines[-1] for k in ('"up_bid"', '"down_ask"', '"pnl_up_win"', '"pnl_down_win"', '"min_settlement_pnl"',
                                        '"losing_side_exposure"', '"target"'))


def test_settlement_reconciles_four_ways_and_a_mismatch_halts():
    e = _eng()
    _enter(e)
    cyc = e.cycles[SLUG]
    m = _meta("BTC").market
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    assert cyc.state is rc.RState.SETTLEMENT_PENDING
    pnl = cyc.book.resolution_pnl("DOWN")
    e.on_market_settled(SLUG, False, pnl, m.close_ts + 40_000.0)
    assert SLUG not in e.cycles and e.stats["settled_cycles"] == 1 and e.stats["recon_max_diff"] < 1e-9
    assert e.stats["ended_negative"] == 1 and e.stats["recovered"] == 0 and not e.accounting_halt
    assert e.settled_cycles[-1]["prediction"] == pytest.approx(pnl, abs=1e-4)   # no recovery: the first leg IS the result
    e2 = _eng()
    _enter(e2)
    e2.on_market_settled(SLUG, False, e2.cycles[SLUG].book.resolution_pnl("DOWN") + 0.5, m.close_ts + 40_000.0)
    assert e2.accounting_halt and e2.stats["recon_failures"] == 1
    e3 = _eng()
    e3.on_market_settled("btc-updown-5m-9", True, 2.0, NOW)                  # P&L on a market never held
    assert e3.accounting_halt and "holds no cycle" in e3.accounting_halt


def test_the_dashboard_block_shows_both_branches_and_the_snapshot_is_v4():
    e = _eng()
    _enter(e)
    snap = e.snapshot()["pairlock"]
    assert snap["version"] == 4 and snap["strategy"] == "R"
    (row,) = snap["open"]
    assert row["up_qty"] == 10.0 and "UP-win" in row["locked_label"] and "DOWN-win" in row["locked_label"]
    assert snap["baseline"]["no_hedge_pnl"] == 0.0 and snap["recovery"]["sent"] == 0


def test_env_names_both_spellings_and_the_selector(tmp_path, monkeypatch):
    p = recovery_params_from_env({"TPB_PAIRLOCK_RECOVERY_TARGET_PROFIT": "1.5", "TPB_PAIRLOCK_MAX_RECOVERY_ORDER_SIZE": "12",
                                  "TPB_PAIRLOCK_REC_MIN_EDGE": "0.02", "TPB_PAIRLOCK_RECOVERY_COOLDOWN": "3000",
                                  "TPB_PAIRLOCK_REC_REQUIRE_MIN_IMPROVEMENT": "1"})
    assert (p.target_profit, p.max_order_size, p.min_edge, p.cooldown_ms, p.require_min_improvement) == (1.5, 12.0, 0.02, 3000.0, True)
    assert pairlock_strategy({"TPB_PAIRLOCK_STRATEGY": "R"}) == "R"
    monkeypatch.setenv("TPB_PAIRLOCK_STRATEGY", "R")
    monkeypatch.setenv("TPB_PAIRLOCK_HEDGE_MODE", "conditional")
    bot = _bot(tmp_path, **TRI)
    assert type(bot.engine) is PairLockRecoveryEngine and bot.engine.rprm.target_profit == 0.5


def test_a_restart_rebuilds_the_same_position(tmp_path, monkeypatch):
    monkeypatch.setenv("TPB_PAIRLOCK_STRATEGY", "R")
    monkeypatch.setenv("TPB_PAIRLOCK_HEDGE_MODE", "conditional")
    bot = _bot(tmp_path, **TRI)
    now = time.time() * 1000.0
    slug = f"btc-updown-5m-{int(now // 300_000) * 300}"
    cyc = rc.RecoveryCycle(book=__import__("troll_poly_bot.strategy.pairlock_book", fromlist=["PairBook"]).PairBook(slug),
                           a_side="UP", target=0.5)
    rc.on_entry_sent(cyc, "e1", 10.0, 0.55, now)
    rc.on_fill(cyc, rc.FillRecord(slug, now, "up", "UP", 10.0, 0.52, 0.1747, "e1", "e1-1", "BUY", 0.52, "entry"), now)
    rc.on_order_done(cyc, "e1", True, now)
    dec = rc.RecoveryDecision("DOWN", 8.0, 0.63, 0.60, 17.64, "test", {})
    rc.on_recovery_sent(cyc, dec, "r1", now)
    rc.on_fill(cyc, rc.FillRecord(slug, now, "down", "DOWN", 8.0, 0.60, 8 * fee_per_share(0.60), "r1", "r1-1", "BUY", 0.60, "recovery"), now)
    rc.on_order_done(cyc, "r1", True, now)
    bot.engine.cycles[slug] = cyc
    bot.engine.close_ts[slug] = now + 120_000.0
    before = cyc.book.summary(0.45, 0.50, 0.40)
    bot._persist_positions()                                                 # noqa: SLF001
    again = _bot(tmp_path, **TRI)
    assert type(again.engine) is PairLockRecoveryEngine
    back = again.engine.cycles[slug]
    assert back.book.summary(0.45, 0.50, 0.40) == before and back.actions == 1 and len(back.recoveries) == 1
    assert back.state is rc.RState.HOLDING and back.book.down_qty == 8.0


def test_the_late_buy_through_the_live_path():
    from test_spot_lag import T0
    e = _eng()
    _enter(e)                                                       # 10 UP @ 0.52
    e.plmodel = _model(0.0)
    e._features = lambda meta, now: _feat(0.97)                     # noqa: SLF001  P(UP) 0.97
    t = T0 + 300_000.0 - 15_000.0                                   # 15 s before the close
    ev = _eval_books(e, t, up=(0.94, 0.95), down=(0.05, 0.06))
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "late" and ev.side == "UP" and ev.size == 10.0, (ev.reason, ev.detail)
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.95, 10.0, t + 300.0)
    cyc = e.cycles[SLUG]
    assert cyc.book.up_qty == 20.0 and cyc.late_orders == 1 and e.stats["late_fills"] == 1 and e.stats["recovery_sent"] == 0
    # a market with no position: the late buy opens one
    e2 = _eng()
    e2.plmodel = _model(0.0)
    e2._features = lambda meta, now: _feat(0.03)                    # noqa: SLF001  P(DOWN) 0.97
    ev2 = _eval_books(e2, t, up=(0.04, 0.05), down=(0.94, 0.95))
    assert ev2.tradeable and ev2.side == "DOWN" and ev2.features["triangle"]["trigger"] == "late", (ev2.reason, ev2.detail)
    o2 = e2.to_order(ev2, t)
    e2.entry_result(o2.order_id, True)
    _fill(e2, o2, 0.95, 10.0, t + 300.0)
    assert e2.cycles[SLUG].book.down_qty == 10.0 and e2.cycles[SLUG].state is rc.RState.HOLDING
    m = _meta("BTC").market
    e2.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    pnl = e2.cycles[SLUG].book.resolution_pnl("DOWN")
    e2.on_market_settled(SLUG, False, pnl, m.close_ts + 40_000.0)
    assert e2.stats["late_leg"] == pytest.approx(pnl) and not e2.accounting_halt


def test_orders_are_fill_and_kill_when_the_order_type_is_fak():
    from troll_poly_bot.types import TimeInForce as TIF
    e = _eng()
    e.scfg.order_type = "FAK"
    ev = _ev(e, NOW, signal=_result(1, t=NOW))
    o = e.to_order(ev, NOW)
    assert o.tif is TIF.IOC                                                # the entry
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.52, 6.0, NOW + 300.0)                                    # only 6 of 10 were there
    e.evaluate  # noqa: B018
    cyc = e.cycles[SLUG]
    assert cyc.book.up_qty == 6.0
    e._finalize(SLUG, NOW + 400.0)                                         # noqa: SLF001
    assert cyc.state is rc.RState.HOLDING and SLUG not in e.pending        # a partial entry is a position, not a kill
    e.plmodel = _model(0.0)
    e._features = lambda meta, now: _feat(0.30, **TOWARD_DOWN)             # noqa: SLF001
    ev2 = _eval_books(e, NOW + 3000.0, up=(0.40, 0.41), down=(0.59, 0.60))
    assert ev2.tradeable, (ev2.reason, ev2.detail)
    assert e.to_order(ev2, NOW + 3000.0).tif is TIF.IOC                    # the recovery order too


# ───────────────────── THE OPEN RULE (the user, 2026-10-02 20:00) ─────────────────────

def _open_eng(**kw):
    from test_spot_lag import T0
    e = _eng(rprm=rc.RecoveryParams(entry_mode="open", direction="price", **kw))
    e.hist.clear()
    for t in range(int(T0 - 2000), int(T0), 200):                      # the samples before the open
        e._record(_meta("BTC"), _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50), VIEW, float(t))   # noqa: SLF001
    return e


def _open_buy(e, t, up=(0.55, 0.56), down=(0.44, 0.45), price=None, size=None):
    ev = _eval_books(e, t, up=up, down=down)
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "open", (ev.reason, ev.detail)
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, price if price is not None else ev.limit_price - 0.02, size if size is not None else ev.size, t + 300.0)
    return ev, o


def test_the_open_rule_buys_the_higher_token_only_in_the_first_seconds():
    from test_spot_lag import T0
    e = _open_eng()
    ev0 = _eval_books(_open_eng(), T0 + 3000.0, up=(0.55, 0.56), down=(0.44, 0.45))
    assert "OPEN buy 10 UP" in _open_eng().signal_text(ev0)                   # live.py logs it before the send
    assert ev0.p_used is not None and 0.0 < ev0.p_used < 1.0                     # ... and p_used after it
    ev, o = _open_buy(e, T0 + 3000.0)
    assert ev.side == "UP" and ev.size == 10.0 and ev.limit_price == pytest.approx(0.58) and o.tif is TimeInForce.FOK
    cyc = e.cycles[SLUG]
    assert cyc.open_entry and cyc.book.up_qty == 10.0 and cyc.state is rc.RState.HOLDING and e.stats["open_sent"] == 1
    # DOWN the higher token: DOWN is bought
    e2 = _open_eng()
    ev2, _ = _open_buy(e2, T0 + 1000.0, up=(0.37, 0.38), down=(0.62, 0.63))
    assert ev2.side == "DOWN"
    # after the window, and a spot-shock trigger, buy nothing
    e3 = _open_eng()
    late = _eval_books(e3, T0 + 11_000.0, up=(0.55, 0.56), down=(0.44, 0.45))
    assert not late.tradeable and SLUG not in e3.cycles
    trig = _ev(e3, NOW, signal=_result(1, t=NOW))
    assert not trig.tradeable and "open mode" in trig.detail
    # outside the price band (the higher ask over OPEN_MAX_PRICE): nothing
    e4 = _open_eng()
    assert not _eval_books(e4, T0 + 2000.0, up=(0.96, 0.97), down=(0.03, 0.04)).tradeable


def test_the_pair_bid_rests_at_the_sum_minus_the_fill_and_locks_the_market():
    from test_spot_lag import T0
    e = _open_eng()
    t = T0 + 2000.0
    _open_buy(e, t, price=0.56)
    m = _meta("BTC").market
    side, po = e.pair_order(m, "UP", 10.0, 0.56, t + 300.0, other_ask=0.45)
    assert side == "DOWN" and po.price == pytest.approx(0.41) and po.size == 10.0           # 0.97 - 0.56
    assert po.tif is TimeInForce.GTC and po.expires_ts == m.close_ts and po.token_id == "DOWN"
    assert e.pair_order(m, "UP", 10.0, 0.56, t + 400.0) is None                             # one pair per open buy
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    cyc = e.cycles[SLUG]
    assert cyc.pair["state"] == "working" and e.stats["pairs_placed"] == 1
    # while the bid works and DOWN is NOT above UP: no cancel, no recovery
    e.plmodel = _model(0.0)
    e._features = lambda meta, now: _feat(0.40, **TOWARD_DOWN)      # noqa: SLF001
    ev = _eval_books(e, t + 5000.0, up=(0.50, 0.51), down=(0.48, 0.49))
    assert not ev.tradeable and e.pair_cancels(t + 5000.0) == []
    # the bid fills: the pair is locked, the recovery never buys
    from troll_poly_bot.types import Fill, Side
    f = Fill(po.order_id, "DOWN", Side.BUY, 0.41, 10.0, 0.0, t + 9000.0, t + 9000.0, t + 9000.0, expected_price=0.41)
    e.on_pair_fill_record(SLUG, po, f, t + 9000.0)
    e.on_pair_done(po.order_id, filled=True)
    assert cyc.book.up_qty == 10.0 and cyc.book.down_qty == 10.0 and cyc.pair["state"] == "filled" and e.stats["pairs_filled"] == 1
    o = rc.outcomes(cyc.book)
    assert o["pnl_up_win"] == pytest.approx(o["pnl_down_win"]) and o["pnl_up_win"] > 0                # 10 - 9.70 - the entry fee
    ev2 = _eval_books(e, t + 12_000.0, up=(0.30, 0.31), down=(0.69, 0.70))
    assert not ev2.tradeable and e.pair_cancels(t + 12_000.0) == []
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    pnl = cyc.book.resolution_pnl("DOWN")
    e.on_market_settled(SLUG, False, pnl, m.close_ts + 40_000.0)
    assert not e.accounting_halt and e.stats["pair_leg"] == pytest.approx(10 * (1 - 0.41))
    assert e.settled_cycles[-1]["pnl"] == pytest.approx(pnl, abs=1e-4)


def test_the_other_side_turning_higher_pulls_the_bid_and_hands_over_to_the_recovery():
    from test_spot_lag import T0
    e = _open_eng()
    t = T0 + 2000.0
    _open_buy(e, t, price=0.56)
    m = _meta("BTC").market
    side, po = e.pair_order(m, "UP", 10.0, 0.56, t + 300.0)
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    e.plmodel = _model(0.0)
    e._features = lambda meta, now: _feat(0.30)                     # noqa: SLF001  no reversal signals at all
    t1 = t + 20_000.0
    ev = _eval_books(e, t1, up=(0.40, 0.41), down=(0.59, 0.60))      # DOWN is now the higher price
    assert not ev.tradeable                                          # the bid still works on this tick
    assert e.pair_cancels(t1) == [po.order_id]
    cyc = e.cycles[SLUG]
    assert cyc.pair["state"] == "cancelled" and e.stats["pairs_cancelled"] == 1
    e.on_pair_done(po.order_id, filled=False)                        # live.py reports the pull
    assert cyc.pair["state"] == "cancelled" and e.stats["pairs_expired"] == 0
    ev2 = _eval_books(e, t1 + 300.0, up=(0.40, 0.41), down=(0.59, 0.60))
    assert ev2.tradeable and ev2.features["triangle"]["trigger"] == "recovery" and ev2.side == "DOWN", (ev2.reason, ev2.detail)
    C = cyc.book.total_cost
    x = math.ceil((0.5 + C) / (1 - 0.60 - fee_per_share(0.60) - 0.005) * 100) / 100
    assert ev2.size == pytest.approx(x)
    # the signals direction would have refused (no confirmed reversal): price mode needs none
    assert rc.RecoveryParams().direction == "signals"


def test_the_pair_follows_a_partial_fill_and_a_kill_retries_the_open_buy():
    from test_spot_lag import T0
    e = _open_eng(open_max_tries=2)
    e.scfg.order_type = "FAK"
    t = T0 + 1000.0
    ev, o = _open_buy(e, t, price=0.56, size=6.0)
    assert o.tif is TimeInForce.IOC and e.pair_after_partial
    side, po = e.pair_order(_meta("BTC").market, "UP", 6.0, 0.56, t + 300.0)
    assert po.size == 6.0 and po.price == pytest.approx(0.41)
    # a killed open buy: the market stays flat and the rule tries again inside the window, at most open_max_tries
    e2 = _open_eng(open_max_tries=2)
    for k in range(2):
        tk = t + 1000.0 * k
        evk = _eval_books(e2, tk, up=(0.55, 0.56), down=(0.44, 0.45))
        assert evk.tradeable, (k, evk.reason, evk.detail)
        ok = e2.to_order(evk, tk)
        e2.entry_result(ok.order_id, False, "killed")
        assert SLUG not in e2.cycles and SLUG not in e2.pending
    assert not _eval_books(e2, t + 3000.0, up=(0.55, 0.56), down=(0.44, 0.45)).tradeable
    assert e2.stats["open_kills"] == 2


def test_the_pair_sum_is_a_dashboard_setting_of_this_bot_only():
    from troll_poly_bot.control import TUNABLES, apply_to, current_values, spec
    e = _open_eng()
    assert "pairlock.pair_sum" in TUNABLES and any(r["key"] == "pairlock.pair_sum" for r in spec())
    cfg = type("C", (), {"engine": None, "risk": None})()
    assert current_values(cfg, None, pairlock=e.rprm)["pairlock.pair_sum"] == pytest.approx(0.97)
    assert "pairlock.pair_sum" not in current_values(cfg, None)                       # other bots do not have it
    clean, err = TUNABLES["pairlock.pair_sum"].coerce(0.95)
    assert err is None
    assert apply_to({"pairlock.pair_sum": clean}, cfg, None, pairlock=e.rprm)
    assert e.rprm.pair_sum == pytest.approx(0.95) and rc.pair_bid_price(e.rprm, 0.60) == pytest.approx(0.35)


def test_live_pulls_the_pairs_the_engine_cancels():
    from types import SimpleNamespace
    from troll_poly_bot.live import LiveBot
    pulled, done = [], []
    eng = SimpleNamespace(pair_cancels=lambda now: ["p1"], on_pair_done=lambda oid, filled: done.append((oid, filled)))
    bot = SimpleNamespace(engine=eng, exchange=SimpleNamespace(cancel=lambda oid, now: pulled.append(oid)),
                          _pairs_open={"p1": {"slug": SLUG, "side": "DOWN", "size": 10.0, "price": 0.41}},
                          _persist_positions=lambda: None, decisions=SimpleNamespace(order_event=lambda *a, **k: None))
    LiveBot._engine_pair_cancels(bot, NOW)                           # noqa: SLF001
    assert pulled == ["p1"] and done == [("p1", False)] and bot._pairs_open == {}
