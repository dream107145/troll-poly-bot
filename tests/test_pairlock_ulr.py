"""The UNFILLED-LEG RECOVERY of tpb-pairlock-paper (strategy/pairlock_ulr.py in strategy/pairlock_recovery_engine.py;
the user's 2026-10-05 spec, its 16 test cases). The pair-lock rule itself is unchanged: the conditions buy, the pair
bid at pair_sum - fill; the recovery is a separate state machine per unhedged first leg."""
from __future__ import annotations

import pytest
from test_pairlock_conditions import _buy, _ceng, _view
from test_pairlock_recovery_engine import _deep, _fill
from test_spot_lag import SLUG, T0, _meta, _Vol

from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy import pairlock_ulr as ulr
from troll_poly_bot.strategy.pairlock_book import fee_per_share
from troll_poly_bot.types import Fill, Side, TimeInForce

M = _meta("BTC").market
T = T0 + 20_000.0                     # the first leg's time (280 s left)


def _ueng(**kw):
    e = _ceng(**{"ulr_enabled": True, "pair_pull": False, "recovery_enabled": False, **kw})
    e.scfg.min_time_remaining_s = 10.0                   # the bot's order floor (TPB_TRIANGLE_MIN_TIME_REMAINING_S 10)
    # the base harness seeds the spot history 100 s in the test's future; the ULR's spot momentum needs it in order
    e.spot_hist.clear()
    for t in range(int(T0 - 2000), int(T0), 200):
        e._record(_meta("BTC"), _deep("UP", 0.50, 0.52), _deep("DOWN", 0.48, 0.50), _view(100.0), float(t))   # noqa: SLF001
    return e


def _ev(e, now, up, down, btc=102.0, spot_age=200.0):
    from test_pairlock_engine_v2 import _feed_to
    tw = _feed_to(e, now)
    return e.evaluate(_meta("BTC"), _deep("UP", *up), _deep("DOWN", *down), _view(btc), spot_age, 100.0, now, _Vol(), tw,
                      COLOCATED, 500.0)


def _mirror(up_bid, up_ask):
    return (round(1.0 - up_ask, 2), round(1.0 - up_bid, 2))


def _ramp(e, t0, t1, up0, up1, btc0, btc1, step=200.0):
    """Record 200 ms samples from t0 to t1: the UP mid moves up0 -> up1 (1c spread), DOWN mirrored, spot btc0 -> btc1."""
    n = max(1, int((t1 - t0) / step))
    for k in range(n + 1):
        f = k / n
        mid = up0 + (up1 - up0) * f
        ub, ua = round(mid - 0.005, 3), round(mid + 0.005, 3)
        db, da = _mirror(ub, ua)
        e._record(_meta("BTC"), _deep("UP", ub, ua), _deep("DOWN", db, da), _view(btc0 + (btc1 - btc0) * f), t0 + k * step)  # noqa: SLF001


def _leg(e, t=T, price=0.55, size=None):
    """The pair-lock first leg: 20 UP @ 0.55 and its DOWN pair bid at 0.42 (0.97 - 0.55)."""
    ev, o = _buy(e, t, price=price, size=size)
    q = size if size is not None else ev.size
    side, po = e.pair_order(M, "UP", q, price, t + 300.0)
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    return o, po


def _pair_fill(e, po, t, qty=None):
    q = qty if qty is not None else po.size
    f = Fill(po.order_id, po.token_id, Side.BUY, po.price, q, 0.0, t, t, t, expected_price=po.price)
    e.on_pair_fill_record(SLUG, po, f, t)
    if qty is None:
        e.on_pair_done(po.order_id, filled=True)


def _against(e, t_end, up_to=0.40, btc_to=99.5, secs=3.0):
    """The market moves against the UP leg over the last ``secs`` seconds: UP 0.55 -> up_to, DOWN mirrored, BTC falls."""
    _ramp(e, t_end - secs * 1000.0, t_end - 200.0, 0.55, up_to, 102.0, btc_to)
    up = (round(up_to - 0.005, 3), round(up_to + 0.005, 3))
    return _ev(e, t_end, up, _mirror(*up), btc=btc_to)


def _u(e):
    return ulr.ULR.from_json(e.cycles[SLUG].ulr)


def _recover(e, t_end=T + 40_000.0, fill_price=None, fill_size=None):
    """Drive a full recovery entry: the move against the leg, the ULR buy, its fill. Returns (ev, order)."""
    ev = _against(e, t_end)
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "ulr", (ev.reason, ev.detail)
    o = e.to_order(ev, t_end)
    e.entry_result(o.order_id, True)
    _fill(e, o, fill_price if fill_price is not None else ev.limit_price - 0.02, fill_size if fill_size is not None else ev.size,
          t_end + 300.0)
    e._finalize(SLUG, t_end + 400.0)                                                     # noqa: SLF001
    return ev, o


def _sell_books(dn_bid, dn_ask):
    ub, ua = _mirror(dn_bid, dn_ask)
    return _deep("UP", ub, ua), _deep("DOWN", dn_bid, dn_ask)


# 1. the normal pair completes -> recovery never activates
def test_01_a_completed_pair_never_recovers():
    e = _ueng()
    o, po = _leg(e)
    assert _u(e).state == ulr.HEDGE_PENDING and _u(e).hedge_target == pytest.approx(0.42)
    _pair_fill(e, po, T + 5000.0)
    assert _u(e).state == ulr.HEDGED
    ev = _against(e, T + 40_000.0)                       # the market then swings hard: nothing happens
    assert not ev.tradeable and e.stats["ulr_sent"] == 0 and _u(e).state == ulr.HEDGED
    assert e.exit_actions(M, *_sell_books(0.59, 0.60), T + 41_000.0, {"UP": 20.0, "DOWN": 20.0}, {}) == []


# 2. the hedge fills later -> never activates
def test_02_a_hedge_that_fills_later_never_recovers():
    e = _ueng()
    o, po = _leg(e)
    ev = _ev(e, T + 5000.0, (0.52, 0.53), (0.47, 0.48))                               # DOWN 0.48 < 0.50, move 0.06 < 0.08
    assert not ev.tradeable and _u(e).state == ulr.HEDGE_PENDING
    assert _u(e).reasons[-1][1] == ulr.R_HEDGE_STILL_POSSIBLE
    _pair_fill(e, po, T + 30_000.0)
    assert _u(e).state == ulr.HEDGED and e.stats["ulr_sent"] == 0


# 3. the hedge stays unfilled and the opposite moves past the bid -> eligible
def test_03_an_unfilled_hedge_becomes_eligible():
    e = _ueng()
    _leg(e)
    ev = _ev(e, T + 5000.0, (0.47, 0.48), (0.52, 0.53))                               # DOWN ask 0.53 = bid 0.42 + 0.11
    assert not ev.tradeable                                                           # eligible, but no momentum yet
    u = _u(e)
    assert u.state == ulr.UNHEDGED_RECOVERY and e.stats["ulr_candidates"] == 1 and e.stats["ulr_sent"] == 0


# 4. opposite > 0.50 but the momentum is not confirmed -> no recovery
def test_04_no_momentum_no_recovery():
    e = _ueng()
    _leg(e)
    _ramp(e, T + 30_000.0, T + 39_800.0, 0.45, 0.45, 99.0, 99.0)                      # flat at UP 0.45 / DOWN 0.55
    ev = _ev(e, T + 40_000.0, (0.445, 0.455), _mirror(0.445, 0.455), btc=99.0)
    assert not ev.tradeable and e.stats["ulr_sent"] == 0
    assert _u(e).reasons[-1][1] == ulr.R_MOMENTUM_NOT_CONFIRMED
    assert e.ulr_reasons[ulr.R_MOMENTUM_NOT_CONFIRMED] >= 1


# 5. the opposite moves strongly against the first leg -> recovery activates
def test_05_a_confirmed_move_against_the_leg_recovers():
    e = _ueng()
    _leg(e)
    ev = _against(e, T + 40_000.0)
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "ulr" and ev.side == "DOWN", (ev.reason, ev.detail)
    assert ev.size == 6.0 and ev.limit_price == pytest.approx(0.605 + 0.02, abs=0.0051)   # 0.30 x 20; ask + max slippage
    assert _u(e).reasons[-1][1] == ulr.R_ENTRY and e.stats["ulr_signals"] == 1
    o = e.to_order(ev, T + 40_000.0)
    assert o.tif is TimeInForce.FOK and o.side is Side.BUY and _u(e).state == ulr.RECOVERY_ENTRY_PENDING
    assert e.cycles[SLUG].actions == 0                                                # not a strategy-R action
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.605, 6.0, T + 40_300.0)
    u = _u(e)
    assert u.state == ulr.RECOVERY_OPEN and u.entry_qty == 6.0 and u.entries == 1 and e.cycles[SLUG].book.down_qty == 6.0
    assert e.stats["ulr_fills"] == 1 and "UNFILLED-LEG RECOVERY buy 6 DOWN" in e.signal_text(ev)


# 6. the recovery size
def test_06_recovery_size():
    prm = rc.RecoveryParams(ulr_enabled=True, pair_pull=False, recovery_enabled=False)
    u = ulr.start("p", "UP", 20.0)
    assert ulr.recovery_size(u, prm, 5.0) == (6.0, "")                               # 0.30 x 20
    assert ulr.recovery_size(u, rc.RecoveryParams(ulr_initial_ratio=0.5), 5.0)[0] == 10.0
    u10 = ulr.start("p", "UP", 10.0)
    assert ulr.recovery_size(u10, prm, 5.0)[0] == 5.0                                # 3 < the venue minimum -> 5 (fits in 10)
    u4 = ulr.start("p", "UP", 4.0)
    q, why = ulr.recovery_size(u4, prm, 5.0)
    assert q == 0.0 and "venue minimum" in why
    assert ulr.recovery_size(u, rc.RecoveryParams(ulr_initial_ratio=1.0, ulr_max_position_size=8.0), 5.0)[0] == 8.0
    u.hedge_size, u.hedge_filled = 20.0, 8.0                                          # a partly filled hedge: 12 unhedged
    assert u.unhedged_qty == 12.0 and ulr.recovery_size(u, prm, 5.0)[0] == 5.0        # 3.6 -> the minimum


# 7. take profit
def test_07_take_profit_sells_at_the_executable_bid():
    e = _ueng()
    _leg(e)
    ev, o = _recover(e, fill_price=0.60)
    t = T + 45_000.0
    assert e.exit_actions(M, *_sell_books(0.66, 0.67), t, {"UP": 20.0, "DOWN": 6.0}, {}) == []     # +0.06 < TP 0.08
    acts = e.exit_actions(M, *_sell_books(0.68, 0.69), t, {"UP": 20.0, "DOWN": 6.0}, {})
    assert len(acts) == 1 and acts[0].kind == ulr.R_TAKE_PROFIT and acts[0].side == "DOWN" and acts[0].shares == 6.0
    assert acts[0].price == pytest.approx(0.66) and acts[0].tif == "FOK" and acts[0].leg_id == _u(e).position_id
    e.on_exit_order(acts[0].leg_id, acts[0].kind, "sell-1")
    assert _u(e).state == ulr.RECOVERY_EXIT_PENDING and e.stats["ulr_exits_sent"] == 1
    fee = 6.0 * fee_per_share(0.68)
    kind = e.on_exit_fill("sell-1", 6.0, 0.68, fee, t + 300.0)
    u = _u(e)
    assert kind == ulr.R_TAKE_PROFIT and u.state == ulr.RECOVERY_CLOSED and u.open_qty == 0.0
    assert u.realized_pnl == pytest.approx(6.0 * (0.68 - 0.60) - fee - 6.0 * fee_per_share(0.60))
    assert e.cycles[SLUG].book.down_qty == 0.0 and e.stats["ulr_tp"] == 1
    assert e.exit_actions(M, *_sell_books(0.75, 0.76), t + 1000.0, {"UP": 20.0, "DOWN": 0.0}, {}) == []


# 8. stop loss
def test_08_stop_loss_sells():
    e = _ueng()
    _leg(e)
    _recover(e, fill_price=0.60)
    t = T + 45_000.0
    assert e.exit_actions(M, *_sell_books(0.57, 0.58), t, {"UP": 20.0, "DOWN": 6.0}, {}) == []     # -0.03 > -SL 0.04
    acts = e.exit_actions(M, *_sell_books(0.56, 0.57), t, {"UP": 20.0, "DOWN": 6.0}, {})
    assert len(acts) == 1 and acts[0].kind == ulr.R_STOP_LOSS and acts[0].price == pytest.approx(0.54)
    e.on_exit_order(acts[0].leg_id, acts[0].kind, "sell-2")
    e.on_exit_fill("sell-2", 6.0, 0.56, 6.0 * fee_per_share(0.56), t + 300.0)
    u = _u(e)
    assert u.state == ulr.RECOVERY_CLOSED and u.realized_pnl < 0 and e.stats["ulr_sl"] == 1


# 9. signal reversal
def test_09_signal_reversal_sells_inside_tp_and_sl():
    e = _ueng()
    _leg(e)
    _recover(e, fill_price=0.60)
    t = T + 50_000.0
    _ramp(e, t - 3000.0, t - 200.0, 0.38, 0.42, 99.5, 100.5)                        # UP rising, DOWN falling: the signal reversed
    acts = e.exit_actions(M, *_sell_books(0.575, 0.585), t, {"UP": 20.0, "DOWN": 6.0}, {})
    assert len(acts) == 1 and acts[0].kind == ulr.R_SIGNAL_REVERSAL
    # ... unless the signal exit is off
    e2 = _ueng(ulr_signal_exit=False)
    _leg(e2)
    _recover(e2, fill_price=0.60)
    _ramp(e2, t - 3000.0, t - 200.0, 0.38, 0.42, 99.5, 100.5)
    assert e2.exit_actions(M, *_sell_books(0.575, 0.585), t, {"UP": 20.0, "DOWN": 6.0}, {}) == []


# 10. never twice on the same failed leg
def test_10_recovery_cannot_enter_twice():
    e = _ueng()
    _leg(e)
    _recover(e, fill_price=0.60)
    t = T + 50_000.0
    acts = e.exit_actions(M, *_sell_books(0.56, 0.57), t, {"UP": 20.0, "DOWN": 6.0}, {})
    e.on_exit_order(acts[0].leg_id, acts[0].kind, "sell-3")
    e.on_exit_fill("sell-3", 6.0, 0.56, 6.0 * fee_per_share(0.56), t + 300.0)
    assert _u(e).state == ulr.RECOVERY_CLOSED
    ev = _against(e, t + 10_000.0, up_to=0.30, btc_to=98.0)                          # another strong move: no second entry
    assert not ev.tradeable and e.stats["ulr_sent"] == 1 and _u(e).reasons[-1][1] == ulr.R_ALREADY_USED
    # and while a recovery is open there is no averaging down either
    e2 = _ueng()
    _leg(e2)
    _recover(e2, fill_price=0.60)
    ev2 = _against(e2, T + 50_000.0, up_to=0.30, btc_to=98.0)
    assert not ev2.tradeable and e2.stats["ulr_sent"] == 1 and e2.cycles[SLUG].book.down_qty == 6.0


# 11. not near the resolution; an open recovery is sold before the close
def test_11_time_limits():
    e = _ueng()
    _leg(e)
    close = T0 + 300_000.0
    ev = _against(e, close - 50_000.0)                                               # 50 s left < 60
    assert not ev.tradeable and _u(e).reasons[-1][1] == ulr.R_TIME_TOO_SHORT
    e2 = _ueng()
    _leg(e2)
    _recover(e2, fill_price=0.60)
    acts = e2.exit_actions(M, *_sell_books(0.60, 0.61), close - 19_000.0, {"UP": 20.0, "DOWN": 6.0}, {})
    assert len(acts) == 1 and acts[0].kind == ulr.R_EMERGENCY_EXIT                   # 19 s left < 20: sold at the bid
    e3 = _ueng()
    _leg(e3)
    _recover(e3, fill_price=0.60)
    assert e3.exit_actions(M, *_sell_books(0.60, 0.61), close - 8_000.0, {"UP": 20.0, "DOWN": 6.0}, {}) == []   # < 10 s: no order
    assert _u(e3).reasons[-1][1] == ulr.R_HOLD_TO_RESOLUTION


# 12. stale spot blocks the entry when confirmation is required
def test_12_stale_spot_blocks_the_entry():
    e = _ueng()
    _leg(e)
    t = T + 40_000.0
    _ramp(e, t - 3000.0, t - 200.0, 0.55, 0.40, 102.0, 99.5)
    ev = _ev(e, t, (0.395, 0.405), _mirror(0.395, 0.405), btc=99.5, spot_age=5000.0)
    assert not ev.tradeable and _u(e).reasons[-1][1] == ulr.R_SPOT_STALE
    e2 = _ueng(ulr_require_spot=False)
    _leg(e2)
    _ramp(e2, t - 3000.0, t - 200.0, 0.55, 0.40, 102.0, 99.5)
    ev2 = _ev(e2, t, (0.395, 0.405), _mirror(0.395, 0.405), btc=99.5, spot_age=5000.0)
    assert ev2.tradeable and ev2.features["triangle"]["trigger"] == "ulr"
    # spot moving the wrong way does not confirm either
    e3 = _ueng()
    _leg(e3)
    _ramp(e3, t - 3000.0, t - 200.0, 0.55, 0.40, 102.0, 103.0)                      # BTC rising while UP falls
    ev3 = _ev(e3, t, (0.395, 0.405), _mirror(0.395, 0.405), btc=103.0)
    assert not ev3.tradeable and _u(e3).reasons[-1][1] == ulr.R_SPOT_NOT_CONFIRMED


# 13. fees are in every figure; the counterfactual splits the market's P&L
def test_13_fees_and_the_counterfactual():
    e = _ueng()
    _leg(e)
    _recover(e, fill_price=0.60)
    t = T + 45_000.0
    acts = e.exit_actions(M, *_sell_books(0.70, 0.71), t, {"UP": 20.0, "DOWN": 6.0}, {})
    e.on_exit_order(acts[0].leg_id, acts[0].kind, "sell-4")
    fee_out = 6.0 * fee_per_share(0.70)
    e.on_exit_fill("sell-4", 6.0, 0.70, fee_out, t + 300.0)
    u = _u(e)
    fee_in = 6.0 * fee_per_share(0.60)
    assert u.entry_fees == pytest.approx(fee_in) and u.exit_fees == pytest.approx(fee_out)
    assert u.realized_pnl == pytest.approx(6.0 * 0.10 - fee_in - fee_out)
    b = e.cycles[SLUG].book
    e.observe_point(SLUG, M, {"t": M.close_ts + 1000.0, "spot": 100.0}, M.close_ts + 1000.0)
    pnl = b.resolution_pnl("DOWN")                                                  # DOWN won: the UP leg lost
    e.on_market_settled(SLUG, False, pnl, M.close_ts + 40_000.0)
    assert not e.accounting_halt and e.stats["recon_max_diff"] < 1e-9
    fin = e.ulr_settled[-1]
    first_leg = -(20.0 * 0.55 + 20.0 * fee_per_share(0.55))
    assert fin["original_pnl"] == pytest.approx(first_leg, abs=1e-6)                 # without the recovery
    assert fin["recovery_pnl"] == pytest.approx(u.realized_pnl, abs=1e-6)
    assert fin["combined_pnl"] == pytest.approx(pnl, abs=1e-6) and fin["combined_pnl"] == pytest.approx(fin["original_pnl"] + fin["recovery_pnl"])
    assert fin["improvement"] == pytest.approx(fin["recovery_pnl"]) and e.stats["ulr_recovered"] == 1
    snap = e.snapshot()["pairlock"]["ulr"]
    assert snap["recovery_improvement"] == pytest.approx(round(u.realized_pnl, 4)) and snap["take_profits"] == 1
    assert snap["avg_loss_without_recovery"] == pytest.approx(round(first_leg, 4)) and snap["worst_recovery_loss"] == 0.0


# 14. a partial fill
def test_14_partial_fill():
    e = _ueng()
    _leg(e)
    ev, o = _recover(e, fill_price=0.60, fill_size=4.0)                              # 4 of the 6 were there
    u = _u(e)
    assert u.state == ulr.RECOVERY_OPEN and u.entry_qty == 4.0 and u.open_qty == 4.0 and SLUG not in e.pending
    t = T + 45_000.0
    acts = e.exit_actions(M, *_sell_books(0.69, 0.70), t, {"UP": 20.0, "DOWN": 4.0}, {})
    assert len(acts) == 1 and acts[0].shares == 4.0
    e.on_exit_order(acts[0].leg_id, acts[0].kind, "sell-5")
    e.on_exit_fill("sell-5", 2.5, 0.69, 2.5 * fee_per_share(0.69), t + 300.0)       # the sell itself fills partly
    u = _u(e)
    assert u.state == ulr.RECOVERY_OPEN and u.open_qty == pytest.approx(1.5)
    acts2 = e.exit_actions(M, *_sell_books(0.69, 0.70), t + 2500.0, {"UP": 20.0, "DOWN": 1.5}, {})
    assert len(acts2) == 1 and acts2[0].shares == 1.5                               # the rest, after the cooldown
    e.on_exit_order(acts2[0].leg_id, acts2[0].kind, "sell-6")
    e.on_exit_result("sell-6", "fok_unfillable", t + 2800.0)                       # killed: still open, retried later
    assert _u(e).state == ulr.RECOVERY_OPEN and e.stats["ulr_exit_kills"] == 1


# 15. the market resolves while the recovery is open
def test_15_market_resolves_with_the_recovery_open():
    e = _ueng()
    _leg(e)
    _recover(e, fill_price=0.60)
    e.observe_point(SLUG, M, {"t": M.close_ts + 1000.0, "spot": 100.0}, M.close_ts + 1000.0)
    b = e.cycles[SLUG].book
    pnl = b.resolution_pnl("UP")                                                     # UP won after all
    e.on_market_settled(SLUG, True, pnl, M.close_ts + 40_000.0)
    assert not e.accounting_halt
    fin = e.ulr_settled[-1]
    assert fin["exit_kind"] == ulr.R_MARKET_RESOLUTION and fin["recovery_held_at_close"] == 6.0
    assert fin["recovery_pnl"] == pytest.approx(-(6.0 * 0.60 + 6.0 * fee_per_share(0.60)))   # the 6 DOWN paid 0
    assert fin["original_pnl"] == pytest.approx(20.0 - 20.0 * 0.55 - 20.0 * fee_per_share(0.55))
    assert e.stats["ulr_resolution"] == 1 and e.stats["ulr_worst"] == pytest.approx(fin["recovery_pnl"], abs=1e-6)


# 16. restart: the recovery state survives with the cycle
def test_16_state_persists_across_a_restart():
    e = _ueng()
    _leg(e)
    _recover(e, fill_price=0.60)
    t = T + 45_000.0
    acts = e.exit_actions(M, *_sell_books(0.69, 0.70), t, {"UP": 20.0, "DOWN": 6.0}, {})
    e.on_exit_order(acts[0].leg_id, acts[0].kind, "sell-7")                        # a sell in flight at the stop
    doc = e.persist_state()
    e2 = _ueng()
    e2.restore_state(doc, t + 60_000.0)
    u = _u(e2)
    assert u.state == ulr.RECOVERY_OPEN and u.open_qty == 6.0 and u.entry_avg == pytest.approx(0.60)   # not EXIT_PENDING
    assert e2.cycles[SLUG].book.down_qty == 6.0 and u.position_id == _u(e).position_id
    acts2 = e2.exit_actions(M, *_sell_books(0.69, 0.70), t + 63_000.0, {"UP": 20.0, "DOWN": 6.0}, {})
    assert len(acts2) == 1 and acts2[0].kind == ulr.R_TAKE_PROFIT


# the modes are exclusive: with ULR on, the strategy-R arithmetic never buys and the pair bid is never pulled
def test_ulr_mode_turns_off_the_r_recovery_and_the_pull():
    e = _ueng(pair_pull=True, recovery_enabled=True, direction="price")
    _leg(e)
    ev = _against(e, T + 40_000.0)
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "ulr"             # the ULR buy, not a strategy-R one
    assert e.pair_cancels(T + 40_000.0) == [] and e.stats["recovery_sent"] == 0
    assert e.manages_exits is True and _ceng().manages_exits is False
    assert e._recovery_gate(e.cycles[SLUG]).startswith("unfilled-leg recovery mode")   # noqa: SLF001
