"""Loss limits for the pair-lock recovery (the user, 2026-10-05: "reduce the large losses"). Every big loss of the R bots
was a recovery chain that doubled until one swing could not be recovered. With MAX_MARKET_LOSS / RECOVERY_MAX_PRICE /
LOCK_LEFT_S (and MAX_ACTIONS) the bot LOCKS the market instead -- buys the side with fewer shares until UP = DOWN --
and the late buy is trimmed so it cannot break the limit."""
from __future__ import annotations

from dataclasses import replace

import pytest

from test_pairlock_dynamic_recovery import fill, held, mv
from test_pairlock_recover_again import NEW, _apply, _branch, _eval

from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy.pairlock_book import PairBook, fee_per_share
from troll_poly_bot.strategy.pairlock_recovery_engine import recovery_params_from_env

LIMITS = replace(NEW, max_market_loss=8.0, recovery_max_price=0.75, lock_left_s=60.0)
M = "btc-updown-5m-1"


def _lock_and_finish(c, view, prm, now):
    """Send the lock, fill it at the ask, then the next evaluation marks the market LOCKED."""
    dec, rec = _eval(c, view, prm, now=now)
    assert dec is not None and dec.kind == "lock", rec["why"]
    _apply(c, dec, now, f"lock{now:.0f}", price=view.ask(dec.side))
    _, rec2 = _eval(c, view, prm, now=now + prm.cooldown_ms + 1.0)
    assert rec2["decision"] == "LOCKED" and c.locked, rec2["why"]
    return dec, rec


def test_swings_are_recovered_until_the_next_one_would_breach_the_limit_then_the_market_locks():
    c = held(fill("UP", 10, 0.60))
    swings = [("DOWN", mv(0.43, 0.45, 0.55, 0.56)), ("UP", mv(0.55, 0.56, 0.43, 0.45))] * 3
    now, kinds = 10_000.0, []
    for k, (side, view) in enumerate(swings):
        dec, rec = _eval(c, view, LIMITS, now=now, bankroll=1_000.0)
        assert dec is not None, (k, rec["why"])
        kinds.append(dec.kind)
        if dec.kind == "lock":
            assert "MAX_MARKET_LOSS" in dec.reason and rec["plan_min_settlement_pnl"] < -8.0
            _apply(c, dec, now, f"s{k}", price=view.ask(dec.side))
            break
        _apply(c, dec, now, f"s{k}", price=view.ask(side))
        assert min(_branch(c, "UP"), _branch(c, "DOWN")) >= -8.0          # every recovery kept the worst branch inside
        now += LIMITS.cooldown_ms + 1.0
    assert kinds[-1] == "lock" and kinds.count("recovery") >= 2           # it still recovered the first swings
    o = rc.outcomes(c.book)
    assert abs(c.book.up_qty - c.book.down_qty) < 5.0
    assert o["pnl_up_win"] > -8.0 and o["pnl_down_win"] > -8.0             # the result is fixed inside the limit
    _, rec = _eval(c, swings[0][1], LIMITS, now=now + 5_000.0)
    assert c.locked and rec["decision"] == "LOCKED"
    _, rec = _eval(c, swings[1][1], LIMITS, now=now + 10_000.0)            # later swings change nothing
    assert rec["decision"] == "NONE" and "market locked" in rec["why"]


def test_a_high_price_locks_instead_of_a_recovery_that_needs_many_shares():
    """UP 10 @ 0.60, DOWN now at 0.80: a recovery to +0.50 needs ~36 DOWN (the UP-win branch would fall to ~-35);
    the lock buys 10 DOWN and fixes the market at ~-2.3 either way."""
    c = held(fill("UP", 10, 0.60))
    view = mv(0.18, 0.20, 0.79, 0.80)
    dec, rec = _eval(c, view, NEW)
    assert dec is not None and dec.kind == "recovery" and dec.plan_qty > 30
    dec, rec = _lock_and_finish(c, view, LIMITS, 10_000.0)
    assert dec.side == "DOWN" and dec.shares == pytest.approx(10.0) and "highest price" in dec.reason
    o = rc.outcomes(c.book)
    assert o["pnl_up_win"] == pytest.approx(o["pnl_down_win"])
    expect = 10 - (6.0 + 10 * fee_per_share(0.60)) - (8.0 + 10 * fee_per_share(0.80))
    assert o["pnl_up_win"] == pytest.approx(expect)


def test_late_in_the_market_it_locks_instead_of_recovering():
    c = held(fill("UP", 10, 0.60))
    view = mv(0.43, 0.45, 0.55, 0.56)
    dec, rec = _eval(c, view, LIMITS, left=50.0)
    assert dec is not None and dec.kind == "lock" and "50 s left" in dec.reason
    dec, rec = _eval(held(fill("UP", 10, 0.60)), view, LIMITS, left=200.0)
    assert dec is not None and dec.kind == "recovery"


def test_max_actions_locks_and_the_lock_ignores_the_recovery_caps_but_not_the_bankroll():
    c = held(fill("UP", 10, 0.60))
    c.actions, c.recovery_cost = LIMITS.max_actions, 1_000.0                 # out of actions and of recovery capital
    dec, rec = _eval(c, mv(0.43, 0.45, 0.55, 0.56), LIMITS)
    assert dec is not None and dec.kind == "lock" and "MAX_ACTIONS" in dec.reason and dec.shares == pytest.approx(10.0)
    dec, rec = _eval(held(fill("UP", 10, 0.60)), mv(0.18, 0.20, 0.79, 0.80), LIMITS, bankroll=3.0)
    assert dec is None and rec["why"].startswith("lock (") and "free bankroll" in rec["why"]
    old = rc.RecoveryParams(**{**vars(NEW)})                                  # limits off: MAX_ACTIONS just refuses
    c2 = held(fill("UP", 10, 0.60))
    c2.actions = old.max_actions
    dec, rec = _eval(c2, mv(0.43, 0.45, 0.55, 0.56), old)
    assert dec is None and "MAX_ACTIONS" in rec["why"]


def test_a_gap_under_the_venue_minimum_counts_as_locked():
    c = held(fill("UP", 10, 0.60), fill("DOWN", 7, 0.40, kind="pair", oid="p"))
    dec, rec = _eval(c, mv(0.18, 0.20, 0.79, 0.80), LIMITS)
    assert dec is None and rec["decision"] == "LOCKED" and c.locked                # 3 shares apart: nothing to buy


def _late_prm(**kw):
    return replace(LIMITS, late_trigger="price", late_min_mid=0.93, late_shares=20.0, late_max_price=0.99,
                   late_window_s=40.0, late_min_left_s=10.0, late_max_capital=100.0, late_max_orders=1, **kw)


def test_the_late_buy_is_trimmed_to_the_loss_limit_or_skipped():
    view = mv(0.95, 0.96, 0.04, 0.05)
    flat = rc.RecoveryCycle(book=PairBook(M), a_side="UP")
    dec, rec = rc.evaluate_late(flat, 0.96, view, 10_000.0, 30.0, _late_prm(), 100.0)
    assert dec is not None and rec["trimmed_from"] == 20.0
    per = 0.96 + fee_per_share(0.96) + LIMITS.slippage_allowance
    assert dec.shares == pytest.approx(int(8.0 / per * 100) / 100.0)
    assert rec["after_pnl_down_win"] >= -8.0 - 1e-9
    dec, rec = rc.evaluate_late(flat, 0.96, view, 10_000.0, 30.0, _late_prm(max_market_loss=0.0), 100.0)
    assert dec is not None and dec.shares == 20.0                              # limit off: the user's full size
    deep = held(fill("UP", 10, 0.60), fill("DOWN", 3, 0.40, kind="pair", oid="p"))   # DOWN-win already -3.4
    dec, rec = rc.evaluate_late(deep, 0.96, view, 10_000.0, 30.0, _late_prm(max_market_loss=4.0), 100.0)
    assert dec is None and "under -4" in rec["why"]


def test_defaults_are_off_and_the_env_and_restart_carry_the_limits():
    d = rc.RecoveryParams()
    assert not d.lock_active and d.max_market_loss == 0.0 and d.recovery_max_price == 1.0 and d.lock_left_s == 0.0
    p = recovery_params_from_env({"TPB_PAIRLOCK_REC_MAX_MARKET_LOSS": "8", "TPB_PAIRLOCK_REC_RECOVERY_MAX_PRICE": "0.75",
                                  "TPB_PAIRLOCK_REC_LOCK_LEFT_S": "60"})
    assert p.lock_active and (p.max_market_loss, p.recovery_max_price, p.lock_left_s) == (8.0, 0.75, 60.0)
    with pytest.raises(ValueError):
        rc.RecoveryParams(recovery_max_price=0.0)
    c = held(fill("UP", 10, 0.60))
    c.locked, c.lock_reason, c.lock_steps = True, "test", 2
    c2 = rc.RecoveryCycle.from_json(c.to_json())
    assert c2.locked and c2.lock_reason == "test" and c2.lock_steps == 2


def test_a_lock_through_the_live_path():
    from test_pairlock_recovery_engine import _eval_books, _feat, _open_buy, _open_eng
    from test_pairlock_engine_v2 import _fill, _model
    from test_spot_lag import SLUG, T0, _meta
    lim = {k: getattr(LIMITS, k) for k in ("edge_gate", "step_fill", "max_market_loss", "recovery_max_price", "lock_left_s")}
    e = _open_eng(depth_haircut=0.0, **lim)
    t = T0 + 2000.0
    _open_buy(e, t, price=0.56)                                               # 10 UP @ 0.56
    m = _meta("BTC").market
    side, po = e.pair_order(m, "UP", 10.0, 0.56, t + 300.0)
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    e.plmodel = _model(0.0)
    e._features = lambda meta, now: _feat(0.20)                               # noqa: SLF001
    t1 = t + 20_000.0
    _eval_books(e, t1, up=(0.19, 0.20), down=(0.79, 0.80))                    # DOWN jumped to 0.80
    assert e.pair_cancels(t1) == [po.order_id]
    e.on_pair_done(po.order_id, filled=False)
    ev = _eval_books(e, t1 + 300.0, up=(0.19, 0.20), down=(0.79, 0.80))
    assert ev.tradeable and ev.side == "DOWN" and ev.features["triangle"]["pairlock"]["kind"] == "lock", (ev.reason, ev.detail)
    assert ev.size == pytest.approx(10.0)
    o = e.to_order(ev, t1 + 300.0)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.80, 10.0, t1 + 600.0)
    cyc = e.cycles[SLUG]
    assert cyc.lock_steps == 1 and cyc.actions == 0 and e.stats["lock_sent"] == 1 and e.stats["lock_fills"] == 1
    ev2 = _eval_books(e, t1 + 3_000.0, up=(0.60, 0.61), down=(0.39, 0.40))     # UP back on top: nothing more to do
    assert not ev2.tradeable and cyc.locked and e.stats["locked_markets"] == 1
    o = rc.outcomes(cyc.book)
    assert o["pnl_up_win"] == pytest.approx(o["pnl_down_win"])
