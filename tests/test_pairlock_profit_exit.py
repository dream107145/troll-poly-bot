"""2026-10-05, the user: "After recover losses, if total profit is over $0.5, sell all tokens." The recovery engine's
PROFIT EXIT: once a cycle has a recovery fill and the whole position, sold at the bids net of exit fees, is over
profit_exit ahead of everything it cost, every token worth anything is sold; the market's result is realised when
live.py reports the market flat (on_market_exited), or at settlement when a worthless loser was left to resolve."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from test_pairlock_conditions import _view
from test_pairlock_engine_v2 import _feed_to, _fill, _model
from test_pairlock_recovery_engine import _deep, _eng, _feat
from test_spot_lag import SLUG, T0, _Vol, _book, _meta

from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.strategy import pairlock_book as pb
from troll_poly_bot.strategy import pairlock_recovery as rc

logging.disable(logging.CRITICAL)
MARKET = _meta("BTC").market


def _recovered(profit_exit: float):
    """10 UP @ 0.56 at the open, then a recovery of 24.97 DOWN @ 0.73 (target +0.50 if DOWN wins)."""
    prm = rc.RecoveryParams(entry_mode="open", direction="price", pair_bid=False, recovery_min_price=0.70, open_delay_s=0.0,
                            max_order_size=60, max_capital=90, edge_gate=False, step_fill=True, max_recoveries=0,
                            profit_exit=profit_exit)
    e = _eng(rprm=prm)
    e.hist.clear()
    for t in range(int(T0 - 2000), int(T0), 200):
        e._record(_meta("BTC"), _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50), _view(100.0), float(t))   # noqa: SLF001

    def ev(now, up, dn):
        tw = _feed_to(e, now)
        return e.evaluate(_meta("BTC"), _deep("UP", *up), _deep("DOWN", *dn), _view(100.0), 200.0, 100.0, now, _Vol(), tw, COLOCATED, 500.0)

    t = T0 + 3000.0
    x = ev(t, (0.55, 0.56), (0.44, 0.45))
    assert x.tradeable, (x.reason, x.detail)
    o = e.to_order(x, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.56, 10.0, t + 300.0)
    assert e.exit_actions(MARKET, _deep("UP", 0.90, 0.91), _deep("DOWN", 0.09, 0.10), t + 1000.0, {}, {}) == []   # no recovery yet
    e.plmodel = _model(0.0)
    e._features = lambda meta, now: _feat(0.30)                                                                # noqa: SLF001
    x2 = ev(t + 20000.0, (0.27, 0.28), (0.72, 0.73))
    assert x2.tradeable and x2.side == "DOWN", (x2.reason, x2.detail)
    o2 = e.to_order(x2, t + 20000.0)
    e.entry_result(o2.order_id, True)
    _fill(e, o2, 0.73, x2.size, t + 20300.0)
    e._finalize(SLUG, t + 20400.0)                                                                             # noqa: SLF001
    cyc = e.cycles[SLUG]
    assert cyc.state is rc.RState.HOLDING and len(cyc.recoveries) == 1
    assert cyc.book.resolution_pnl("DOWN") >= 0.5
    return e, cyc, t + 30000.0


def _held(b):
    return {"UP": b.up_qty, "DOWN": b.down_qty}


def test_the_whole_position_is_sold_once_its_value_at_the_bids_is_over_the_threshold():
    e, cyc, now = _recovered(0.30)
    b = cyc.book
    # DOWN bid 0.95 / UP bid 0.04: 24.12 - fees - 24.35 is still under water -> nothing
    assert e.exit_actions(MARKET, _deep("UP", 0.04, 0.05), _deep("DOWN", 0.95, 0.96), now, _held(b), {}) == []
    # DOWN bid 0.99, UP bid 0.02: value 24.72 + 0.20 - fees - cost = over +0.30 -> both sides sold at bid - slippage, IOC
    acts = e.exit_actions(MARKET, _deep("UP", 0.02, 0.03), _deep("DOWN", 0.99, 1.00), now, _held(b), {})
    assert [(a.kind, a.side, a.shares) for a in acts] == [("PROFIT_EXIT", "UP", 10.0), ("PROFIT_EXIT", "DOWN", round(b.down_qty, 2))]
    assert all(a.tif in ("IOC", "FOK") and a.leg_id == f"pexit:{SLUG}:{a.side}" for a in acts)   # IOC on the FAK bots
    assert acts[1].price == pytest.approx(0.99 - e.rprm.max_slippage)
    assert e.stats["profit_exits"] == 1 and cyc.events[-1]["event"] == "PROFIT_EXIT_SENT"
    # the same tick again: in flight / cooldown -> not sent twice
    for a in acts:
        e.on_exit_order(a.leg_id, a.kind, f"sell-{a.side}")
    assert e.exit_actions(MARKET, _deep("UP", 0.02, 0.03), _deep("DOWN", 0.99, 1.00), now + 1.0, _held(b), {}) == []
    cost_before, proceeds = b.total_cost, 0.0
    for a in acts:
        px = 0.02 if a.side == "UP" else 0.99
        fee = a.shares * pb.fee_per_share(px)
        e.on_exit_fill(f"sell-{a.side}", a.shares, px, fee, now + 300.0)
        e.on_exit_result(f"sell-{a.side}", True, now + 300.0)
        proceeds += a.shares * px - fee
    assert b.up_qty == pytest.approx(0.0) and b.down_qty == pytest.approx(0.0) and b.total_cost == pytest.approx(0.0)
    pnl = proceeds - cost_before                                   # sold out: the realised figure is the market's result
    assert b.realised_exit_pnl == pytest.approx(pnl) and b.fills[-1].kind == "profit_exit"
    assert e.stats["profit_exit_fills"] == 2
    assert pnl > 0.30
    # live.py reports the market flat with the venue's figure: the cycle settles on its sales, no halt
    e.on_market_exited(SLUG, pnl, now + 400.0)
    assert SLUG not in e.cycles and not e.accounting_halt
    assert e.stats["profit_exit_pnl"] == pytest.approx(pnl) and e.stats["settled_cycles"] == 1
    assert e.settled_cycles[-1]["winner"] == "SOLD" and e.settled_cycles[-1]["pnl"] == pytest.approx(pnl, abs=1e-4)
    # the market's resolution later finds no cycle and books nothing: no halt either
    e.on_market_settled(SLUG, False, 0.0, now + 200_000.0)
    assert not e.accounting_halt


def test_a_loser_at_the_floor_is_neither_valued_nor_sold_and_the_market_settles_the_rest():
    e, cyc, now = _recovered(0.30)
    b = cyc.book
    # UP has no bid at all: the exit values it at nothing, sells DOWN only
    acts = e.exit_actions(MARKET, _deep("UP", 0.01, 0.02), _deep("DOWN", 0.99, 1.00), now, _held(b), {})
    assert [(a.side, a.shares) for a in acts] == [("DOWN", round(b.down_qty, 2))]
    e.on_exit_order(acts[0].leg_id, acts[0].kind, "sell-DOWN")
    e.on_exit_fill("sell-DOWN", acts[0].shares, 0.99, acts[0].shares * pb.fee_per_share(0.99), now + 300.0)
    e.on_exit_result("sell-DOWN", True, now + 300.0)
    assert b.down_qty == pytest.approx(0.0) and b.up_qty == 10.0
    # nothing left worth selling: no further action, and no second "profit exit" counted
    assert e.exit_actions(MARKET, _deep("UP", 0.01, 0.02), None, now + 20_000.0, _held(b), {}) == []
    assert e.exit_actions(MARKET, _deep("UP", 0.01, 0.02), _deep("DOWN", 0.99, 1.00), now + 20_000.0, _held(b), {}) == []
    assert e.stats["profit_exits"] == 1
    # the market resolves DOWN: the 10 UP are worth 0, the cycle's result is its sale proceeds minus everything paid
    m = SimpleNamespace(close_ts=T0 + 300_000.0)
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 99.0}, m.close_ts + 1000.0)
    pnl = b.resolution_pnl("DOWN")
    assert pnl == pytest.approx(b.realised_exit_pnl - b.total_cost) and pnl > 0.30
    e.on_market_settled(SLUG, False, pnl, m.close_ts + 40_000.0)
    assert not e.accounting_halt and e.stats["settled_cycles"] == 1 and e.stats["pnl"] == pytest.approx(pnl)


def test_the_threshold_is_the_default_half_dollar_and_switches_the_engine_to_managing_exits():
    e, cyc, now = _recovered(0.50)
    assert e.manages_exits
    b = cyc.book
    # a recovery sized to +0.50 at settlement is under +0.50 at any bid below 1.00: the exit waits for resolution
    assert e.exit_actions(MARKET, _deep("UP", 0.01, 0.02), _deep("DOWN", 0.99, 1.00), now, _held(b), {}) == []
    e.rprm = rc.RecoveryParams(**{**e.rprm.__dict__, "profit_exit": 0.0})
    assert not e.manages_exits


# ---- "Sell falling tokens when recover" (the user, 2026-10-05 23:45)

def test_after_a_recovery_the_falling_side_is_sold_at_its_bid_and_the_sale_enters_the_books():
    e, cyc, now = _recovered(0.0)                                 # the profit exit off: the sale alone
    e.rprm = rc.RecoveryParams(**{**e.rprm.__dict__, "recovery_sell": True})
    assert e.manages_exits
    b = cyc.book
    cost_before, up_basis = b.total_cost, b.total_cost_side("UP")
    # the recovery went toward DOWN: the 10 UP (falling, bid 0.27) are sold, DOWN is kept
    acts = e.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now, _held(b), {})
    assert [(a.kind, a.side, a.shares, a.tif in ("IOC", "FOK")) for a in acts] == [("RECOVERY_SELL", "UP", 10.0, True)]
    assert acts[0].price == pytest.approx(0.27 - e.rprm.max_slippage) and acts[0].leg_id == f"rsell:{SLUG}:UP"
    assert e.stats["recovery_sells"] == 1 and cyc.events[-1]["event"] == "RECOVERY_SELL_SENT"
    e.on_exit_order(acts[0].leg_id, acts[0].kind, "rs-1")
    assert e.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now + 1.0, _held(b), {}) == []   # in flight
    # killed with no fill: retried after the cooldown
    e.on_exit_result("rs-1", "killed", now + 200.0)
    assert e.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now + 200.0, _held(b), {}) == []
    acts = e.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now + e.rprm.cooldown_ms + 1.0, _held(b), {})
    assert len(acts) == 1
    e.on_exit_order(acts[0].leg_id, acts[0].kind, "rs-2")
    fee = 10.0 * pb.fee_per_share(0.27)
    assert e.on_exit_fill("rs-2", 10.0, 0.27, fee, now + e.rprm.cooldown_ms + 300.0) == "RECOVERY_SELL"
    assert b.up_qty == pytest.approx(0.0) and b.down_qty == pytest.approx(24.97) and b.fills[-1].kind == "recovery_sell"
    proceeds = 10.0 * 0.27 - fee
    assert b.realised_exit_pnl == pytest.approx(proceeds - up_basis) and b.total_cost == pytest.approx(cost_before - up_basis)
    assert e.stats["recovery_sell_fills"] == 1 and e.stats["recovery_sell_proceeds"] == pytest.approx(proceeds)
    # the branches: DOWN winning is lifted by the proceeds; UP winning now loses everything paid less the proceeds
    o = rc.outcomes(b)
    assert o["pnl_down_win"] == pytest.approx(24.97 - cost_before + proceeds) and o["pnl_down_win"] > 0.5
    assert o["pnl_up_win"] == pytest.approx(proceeds - cost_before)
    # nothing left of the falling side: no second sale
    t2 = now + 2 * e.rprm.cooldown_ms + 1000.0
    assert e.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), t2, _held(b), {}) == []
    assert e.stats["recovery_sells"] == 2
    # settlement reconciles with the sale in the figure
    m = SimpleNamespace(close_ts=T0 + 300_000.0)
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 99.0}, m.close_ts + 1000.0)
    pnl = b.resolution_pnl("DOWN")
    e.on_market_settled(SLUG, False, pnl, m.close_ts + 40_000.0)
    assert not e.accounting_halt and e.stats["pnl"] == pytest.approx(pnl)


def test_the_falling_side_is_not_sold_at_the_floor_nor_after_a_lock_nor_when_off():
    e, cyc, now = _recovered(0.0)
    b = cyc.book
    # off (the default): nothing
    assert not e.manages_exits
    assert e.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now, _held(b), {}) == []
    e.rprm = rc.RecoveryParams(**{**e.rprm.__dict__, "recovery_sell": True})
    # a bid at the floor: worth nothing, left to settle
    assert e.exit_actions(MARKET, _deep("UP", 0.01, 0.02), _deep("DOWN", 0.98, 0.99), now, _held(b), {}) == []
    # after a lock the fewer side was bought on purpose: never sold back
    cyc.lock_steps = 1
    assert e.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now, _held(b), {}) == []
    cyc.lock_steps = 0
    assert len(e.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now, _held(b), {})) == 1
    assert e.stats["recovery_sells"] == 1


# ---- 2026-10-06 00:32 live: "not enough balance" (26.4789 held, 26.48 asked) and "tick_size" (a 0.003 remnant) every 2 s

def test_sells_ask_for_the_holding_rounded_down_skip_remnants_under_the_minimum_and_back_off_after_a_refusal():
    from troll_poly_bot.strategy.pairlock_recovery_engine import EXIT_REJECT_BACKOFF_MS, sell_qty
    assert sell_qty(26.4789, 5.0) == 26.47 and sell_qty(0.003332, 5.0) == 0.0 and sell_qty(4.99, 5.0) == 0.0 and sell_qty(5.0, 5.0) == 5.0
    e, cyc, now = _recovered(0.30)
    b = cyc.book
    # the venue holds a hair less than the books: the order asks for what the venue holds, rounded down
    acts = e.exit_actions(MARKET, _deep("UP", 0.02, 0.03), _deep("DOWN", 0.99, 1.00), now, {"UP": 9.996, "DOWN": 24.9789}, {})
    assert [(a.side, a.shares) for a in acts] == [("UP", 9.99), ("DOWN", 24.97)]
    # a remnant under the venue minimum is valued but never sent (it would be refused for its tick size)
    e2, cyc2, now2 = _recovered(0.30)
    acts = e2.exit_actions(MARKET, _deep("UP", 0.02, 0.03), _deep("DOWN", 0.99, 1.00), now2, {"UP": 0.003332, "DOWN": 24.97}, {})
    assert [(a.side, a.shares) for a in acts] == [("DOWN", 24.97)]
    # the venue refuses the sell: not asked again for the cooldown plus the back-off
    e2.on_exit_order(acts[0].leg_id, acts[0].kind, "px-1")
    e2.on_exit_result("px-1", "api_error", now2 + 300.0)
    assert e2.exit_actions(MARKET, _deep("UP", 0.02, 0.03), _deep("DOWN", 0.99, 1.00), now2 + 300.0 + e2.rprm.cooldown_ms + 1.0,
                           {"UP": 0.003332, "DOWN": 24.97}, {}) == []
    later = now2 + 300.0 + EXIT_REJECT_BACKOFF_MS + e2.rprm.cooldown_ms + 1.0
    assert len(e2.exit_actions(MARKET, _deep("UP", 0.02, 0.03), _deep("DOWN", 0.99, 1.00), later, {"UP": 0.003332, "DOWN": 24.97}, {})) == 1
    # the recovery sell: same rounding, same remnant rule
    e3, cyc3, now3 = _recovered(0.0)
    e3.rprm = rc.RecoveryParams(**{**e3.rprm.__dict__, "recovery_sell": True})
    acts = e3.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now3, {"UP": 9.996, "DOWN": 24.97}, {})
    assert [(a.side, a.shares) for a in acts] == [("UP", 9.99)]
    e3.on_exit_order(acts[0].leg_id, acts[0].kind, "rs-1")
    e3.on_exit_result("rs-1", "tick_size", now3 + 300.0)
    assert e3.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now3 + 300.0 + e3.rprm.cooldown_ms + 1.0, {"UP": 9.996, "DOWN": 24.97}, {}) == []
    assert e3.exit_actions(MARKET, _deep("UP", 0.27, 0.28), _deep("DOWN", 0.72, 0.73), now3, {"UP": 0.4, "DOWN": 24.97}, {}) == []


def test_a_market_reported_flat_before_its_last_sell_is_booked_waits_for_it():
    """2026-10-06 21:44 (paper): both profit-exit sells filled in one tick; live.py booked the DOWN sell, found the
    venue flat and reported the WHOLE P&L (+1.21) while the engine had only the DOWN sale (+8.27) -> a false
    RECONCILIATION_FAILURE halted every new entry. The engine now waits for the UP sell (reported flat again, P&L 0)."""
    e, cyc, now = _recovered(0.30)
    b = cyc.book
    acts = e.exit_actions(MARKET, _deep("UP", 0.02, 0.03), _deep("DOWN", 0.99, 1.00), now, _held(b), {})
    assert {a.side for a in acts} == {"UP", "DOWN"}
    for a in acts:
        e.on_exit_order(a.leg_id, a.kind, f"sell-{a.side}")
    cost_before = b.total_cost
    px = {"UP": 0.02, "DOWN": 0.99}
    q = {a.side: a.shares for a in acts}
    fee = {s: q[s] * pb.fee_per_share(px[s]) for s in q}
    whole = sum(q[s] * px[s] - fee[s] for s in q) - cost_before          # what the venue's positions say once both filled
    e.on_exit_fill("sell-DOWN", q["DOWN"], px["DOWN"], fee["DOWN"], now + 300.0)
    e.on_market_exited(SLUG, whole, now + 300.0)                          # flat at the venue, the UP sale not booked yet
    assert SLUG in e.cycles and not e.accounting_halt and e.stats["settled_cycles"] == 0
    e.on_exit_fill("sell-UP", q["UP"], px["UP"], fee["UP"], now + 300.0)
    e.on_market_exited(SLUG, 0.0, now + 300.0)                            # the second booking: positions already cleared
    assert SLUG not in e.cycles and not e.accounting_halt, e.accounting_halt
    assert e.stats["settled_cycles"] == 1 and e.stats["profit_exit_pnl"] == pytest.approx(whole)
    assert e.settled_cycles[-1]["pnl"] == pytest.approx(whole, abs=1e-4)
    e.on_market_exited(SLUG, 0.0, now + 400.0)                            # nothing left: a no-op
    assert e.stats["settled_cycles"] == 1 and not e.accounting_halt


def test_the_dashboards_sell_all_waits_for_both_sides_before_the_market_is_reconciled():
    """The Market page's "Sell all tokens" (2026-10-07) sells UP and DOWN: if both fill before live.py books either,
    the market is reported flat (whole P&L) while the engine has one sale -- it now waits for the other dashboard
    sell (on_manual_sell_order .. on_external_fill), as it does for its own profit-exit sells."""
    e, cyc, now = _recovered(0.0)
    b = cyc.book
    up, dn = b.up_qty, b.down_qty
    cost_before = b.total_cost
    e.on_manual_sell_order(SLUG, "UP", "m-up", up, now)
    e.on_manual_sell_order(SLUG, "DOWN", "m-dn", dn, now + 3500.0)
    fee = {s: q * pb.fee_per_share(px) for s, q, px in (("UP", up, 0.30), ("DOWN", dn, 0.69))}
    whole = up * 0.30 + dn * 0.69 - fee["UP"] - fee["DOWN"] - cost_before
    e.on_external_fill(SLUG, "UP", up, 0.30, fee["UP"], "SELL", now + 3800.0, "MANUAL_SELL")
    e.on_market_exited(SLUG, whole, now + 3800.0)                 # flat at the venue; the DOWN sale not booked yet
    assert SLUG in e.cycles and not e.accounting_halt and e.stats["settled_cycles"] == 0
    e.on_external_fill(SLUG, "DOWN", dn, 0.69, fee["DOWN"], "SELL", now + 3800.0, "MANUAL_SELL")
    e.on_market_exited(SLUG, 0.0, now + 3800.0)
    assert SLUG not in e.cycles and not e.accounting_halt, e.accounting_halt
    assert e.settled_cycles[-1]["pnl"] == pytest.approx(whole, abs=1e-4) and not e._manual_sells   # noqa: SLF001
    # a dashboard sell that ends unfilled is forgotten (it cannot hold a later exit back)
    e.on_manual_sell_order(SLUG, "UP", "m-killed", 5.0, now + 9000.0)
    e.on_exit_result("m-killed", "killed", now + 9300.0)
    assert not e._manual_sells                                                                         # noqa: SLF001
