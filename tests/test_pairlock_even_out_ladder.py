"""Two rules for the recovery paper bot (the user, 2026-10-06 21:40):

1. EVEN OUT from the 2nd recovery ("when recovering, buy the same amount as the falling tokens, to reduce large
   losses; on the second, third recovery"): from recovery number EVEN_OUT_FROM on, the recovery buys the rising token
   only until UP = DOWN, so both outcomes pay the same and the doubling chain stops.
2. LATE LADDER ("if < 90 s left, buy high price tokens in price 0.85-0.95 every 20 s"): in a held market, with
   LADDER_MIN_LEFT_S .. LADDER_WINDOW_S seconds left, buy LADDER_SHARES of the higher-ask token while that ask is in
   [LADDER_MIN_PRICE, LADDER_MAX_PRICE], once every LADDER_INTERVAL_S."""
from __future__ import annotations

from dataclasses import replace

import pytest

from test_pairlock_dynamic_recovery import M, fill, held, mv
from test_pairlock_recover_again import NEW, _apply, _branch, _eval

from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy.pairlock_book import PairBook
from troll_poly_bot.strategy.pairlock_recovery_engine import recovery_params_from_env

# the paper bot's rules on 2026-10-06 (Settings + env): recover above 0.70, up to 3 recoveries, loss limits 35 / 0.90 / 60 s
PAPER = replace(NEW, recovery_min_price=0.70, max_recoveries=3, max_market_loss=35.0, recovery_max_price=0.90,
                lock_left_s=60.0, even_out_from=2)
LAD = replace(PAPER, ladder_window_s=90.0, ladder_min_left_s=10.0, ladder_min_price=0.85, ladder_max_price=0.95,
              ladder_interval_s=20.0, ladder_shares=10.0)
DOWN_UP = mv(0.27, 0.28, 0.71, 0.72)      # DOWN is the higher token, its ask 0.72
UP_UP = mv(0.71, 0.72, 0.27, 0.28)        # UP is the higher token, its ask 0.72


def _first_recovery(prm=PAPER):
    """10 UP bought at 0.75, then DOWN turns higher at 0.72: recovery 1 (the target sizing) buys ~31 DOWN."""
    c = held(fill("UP", 10, 0.75))
    dec, rec = _eval(c, DOWN_UP, prm, now=10_000.0)
    assert dec is not None and dec.kind == "recovery" and dec.side == "DOWN", rec["why"]
    assert rec["recovery_number"] == 1 and not rec.get("even_out")
    _apply(c, dec, 10_000.0, "r1", price=0.72)
    assert _branch(c, "DOWN") >= 0.5 - 1e-6 and c.rec_episodes == 1
    return c


# ───────────────────────────── 1. even out ─────────────────────────────

def test_the_second_recovery_buys_only_until_up_equals_down():
    c = _first_recovery()
    gap = c.book.down_qty - c.book.up_qty
    dec, rec = _eval(c, UP_UP, PAPER, now=20_000.0)
    assert dec is not None and dec.kind == "recovery" and dec.side == "UP", rec["why"]
    assert rec["recovery_number"] == 2 and rec["even_out"] and "EVENS OUT" in dec.reason
    assert dec.shares == pytest.approx(gap, abs=0.011)
    _apply(c, dec, 20_000.0, "r2", price=0.72)
    o = rc.outcomes(c.book)
    assert c.book.up_qty == pytest.approx(c.book.down_qty, abs=0.011)
    assert o["pnl_up_win"] == pytest.approx(o["pnl_down_win"], abs=0.02)          # both outcomes pay the same
    assert c.rec_episodes == 2


def test_the_third_recovery_has_nothing_left_to_even():
    c = _first_recovery()
    dec, _ = _eval(c, UP_UP, PAPER, now=20_000.0)
    _apply(c, dec, 20_000.0, "r2", price=0.72)
    dec, rec = _eval(c, DOWN_UP, PAPER, now=30_000.0)
    assert dec is None and rec["decision"] == "NONE" and rec["recovery_number"] == 3
    assert "leaves nothing to even" in rec["why"], rec["why"]


def test_without_the_rule_the_second_recovery_doubles():
    """No loss limits: the old target sizing buys far more than the gap (the chain that makes the large losses)."""
    loose = replace(NEW, recovery_min_price=0.70, max_recoveries=3)
    c = _first_recovery(loose)
    gap = c.book.down_qty - c.book.up_qty
    old, _ = _eval(c, UP_UP, loose, now=20_000.0, bankroll=1_000.0)
    new, _ = _eval(c, UP_UP, replace(loose, even_out_from=2), now=20_000.0, bankroll=1_000.0)
    assert old is not None and new is not None
    assert old.plan_qty > 3 * gap and new.shares == pytest.approx(gap, abs=0.011)


def test_even_out_is_not_turned_into_a_lock():
    """Under 60 s left the first recovery still locks (unchanged); the second evens out as a recovery (counted)."""
    c = held(fill("UP", 10, 0.75))
    dec, _ = _eval(c, DOWN_UP, PAPER, left=45.0)
    assert dec is not None and dec.kind == "lock"
    c = _first_recovery()
    dec, rec = _eval(c, UP_UP, PAPER, now=20_000.0, left=45.0)
    assert dec is not None and dec.kind == "recovery" and rec["even_out"], rec["why"]


def test_even_out_steps_by_the_order_size_and_ignores_the_recovery_capital():
    prm = replace(PAPER, max_order_size=15.0, max_capital=25.0)    # recovery 1 already spent ~23 of the 25
    c = _first_recovery(replace(PAPER, max_order_size=60.0))
    gap = c.book.down_qty - c.book.up_qty
    dec, rec = _eval(c, UP_UP, prm, now=20_000.0)
    assert dec is not None and dec.shares == 15.0 and rec["step_bound"] == "MAX_RECOVERY_ORDER_SIZE" and rec["partial_step"]
    _apply(c, dec, 20_000.0, "r2a", price=0.72)
    dec, rec = _eval(c, UP_UP, prm, now=25_000.0)
    assert dec is not None and rec["recovery_number"] == 2 and dec.shares == pytest.approx(gap - 15.0, abs=0.011)
    _apply(c, dec, 25_000.0, "r2b", price=0.72)
    assert c.rec_episodes == 2 and c.book.up_qty == pytest.approx(c.book.down_qty, abs=0.011)


# ───────────────────────────── 2. the late ladder ─────────────────────────────

def _ladder_buy(c, dec, now, oid, price):
    rc.on_recovery_sent(c, dec, oid, now)
    rc.on_fill(c, fill(dec.side, dec.shares, price, kind="late", oid=oid), now + 300.0)
    rc.on_order_done(c, oid, True, now + 300.0)


def test_the_ladder_buys_the_higher_token_under_90_s_inside_the_band():
    c = held(fill("UP", 10, 0.75))
    view = mv(0.89, 0.90, 0.09, 0.10)
    assert "outside the ladder window" in rc.evaluate_ladder(c, view, 10_000.0, 95.0, LAD, 100.0)[1]["why"]
    dec, rec = rc.evaluate_ladder(c, view, 10_000.0, 85.0, LAD, 100.0)
    assert dec is not None and dec.kind == "late" and dec.side == "UP" and dec.shares == 10.0, rec["why"]
    assert rec["ladder"] and dec.limit == pytest.approx(0.93)
    dec, _ = rc.evaluate_ladder(c, mv(0.93, 0.94, 0.05, 0.06), 10_000.0, 85.0, LAD, 100.0)
    assert dec is not None and dec.limit == pytest.approx(0.95)                    # never above the band's top
    dec, _ = rc.evaluate_ladder(c, mv(0.09, 0.10, 0.88, 0.89), 10_000.0, 85.0, LAD, 100.0)
    assert dec is not None and dec.side == "DOWN"
    for view in (mv(0.83, 0.84, 0.15, 0.16), mv(0.95, 0.96, 0.03, 0.04)):
        dec, rec = rc.evaluate_ladder(c, view, 10_000.0, 85.0, LAD, 100.0)
        assert dec is None and "outside the ladder band" in rec["why"] and not rec["in_band"]
    assert "asks equal" in rc.evaluate_ladder(c, mv(0.49, 0.50, 0.49, 0.50), 10_000.0, 85.0, LAD, 100.0)[1]["why"]
    assert "outside the ladder window" in rc.evaluate_ladder(c, view, 10_000.0, 9.0, LAD, 100.0)[1]["why"]


def test_the_ladder_buys_every_20_s_with_its_own_counters():
    c = held(fill("UP", 10, 0.75))
    view = mv(0.89, 0.90, 0.09, 0.10)
    dec, _ = rc.evaluate_ladder(c, view, 10_000.0, 85.0, LAD, 100.0)
    _ladder_buy(c, dec, 10_000.0, "l1", 0.90)
    assert c.ladder_orders == 1 and c.ladder_cost > 0 and c.late_orders == 0 and c.late_cost == 0.0 and c.book.up_qty == 20
    assert [x["event"] for x in c.events[-2:]] == ["LADDER_FILLED", "LADDER_DONE"] and not c.recoveries
    dec, rec = rc.evaluate_ladder(c, view, 20_000.0, 75.0, LAD, 100.0)
    assert dec is None and "next ladder buy" in rec["why"]
    dec, rec = rc.evaluate_ladder(c, view, 30_000.0, 65.0, LAD, 100.0)
    assert dec is not None, rec["why"]
    _ladder_buy(c, dec, 30_000.0, "l2", 0.90)
    assert c.ladder_orders == 2 and c.book.up_qty == 30
    # the model-gated late buy keeps its own allowance
    late = replace(LAD, late_trigger="price")
    dec, rec = rc.evaluate_late(c, None, mv(0.94, 0.95, 0.04, 0.05), 60_000.0, 15.0, late, 100.0)
    assert dec is not None and "LATE_MAX_ORDERS" not in rec["why"]
    # a ladder fill is a late fill at settlement
    rc.on_close(c, 300_000.0)
    r = rc.on_settled(c, "UP", 340_000.0)
    assert r["late_fills"] == 2 and r["late_leg_pnl"] == pytest.approx(20 * 0.10 - 20 * rc.fee_per_share(0.90), abs=1e-6)


def test_the_ladder_only_adds_to_a_held_market_and_respects_the_loss_limit():
    empty = rc.RecoveryCycle(book=PairBook(M), a_side="UP", target=0.5)
    dec, rec = rc.evaluate_ladder(empty, mv(0.89, 0.90, 0.09, 0.10), 10_000.0, 85.0, LAD, 100.0)
    assert dec is None and "holds" in rec["why"]
    c = held(fill("UP", 10, 0.75))                                   # DOWN-win -7.63
    dec, rec = rc.evaluate_ladder(c, mv(0.89, 0.90, 0.09, 0.10), 10_000.0, 85.0, replace(LAD, max_market_loss=15.0), 100.0)
    assert dec is not None and rec["trimmed_from"] == 10.0 and 5.0 <= dec.shares < 10.0
    assert rec["after_pnl_down_win"] >= -15.0 - 1e-6
    dec, rec = rc.evaluate_ladder(c, mv(0.89, 0.90, 0.09, 0.10), 10_000.0, 85.0, replace(LAD, max_market_loss=12.0), 100.0)
    assert dec is None and "under -12" in rec["why"]
    assert "off" in rc.evaluate_ladder(c, mv(0.89, 0.90, 0.09, 0.10), 10_000.0, 85.0, PAPER, 100.0)[1]["why"]
    one = replace(LAD, ladder_max_orders=1)
    dec, _ = rc.evaluate_ladder(c, mv(0.89, 0.90, 0.09, 0.10), 10_000.0, 85.0, one, 100.0)
    _ladder_buy(c, dec, 10_000.0, "l1", 0.90)
    assert "LADDER_MAX_ORDERS" in rc.evaluate_ladder(c, mv(0.89, 0.90, 0.09, 0.10), 40_000.0, 55.0, one, 100.0)[1]["why"]


def test_env_validation_and_restart():
    p = recovery_params_from_env({"TPB_PAIRLOCK_REC_EVEN_OUT_FROM": "2", "TPB_PAIRLOCK_REC_LADDER_WINDOW_S": "90",
                                  "TPB_PAIRLOCK_REC_LADDER_MIN_PRICE": "0.85", "TPB_PAIRLOCK_REC_LADDER_MAX_PRICE": "0.95",
                                  "TPB_PAIRLOCK_REC_LADDER_INTERVAL_S": "20", "TPB_PAIRLOCK_REC_LADDER_SHARES": "10"})
    assert (p.even_out_from, p.ladder_window_s, p.ladder_min_price, p.ladder_max_price, p.ladder_interval_s,
            p.ladder_shares) == (2, 90.0, 0.85, 0.95, 20.0, 10.0)
    d = rc.RecoveryParams()
    assert d.even_out_from == 0 and d.ladder_window_s == 0.0                       # both off unless configured
    for bad in (dict(even_out_from=-1), dict(ladder_min_price=0.96, ladder_max_price=0.95), dict(ladder_shares=0.0)):
        with pytest.raises(ValueError):
            rc.RecoveryParams(**bad)
    c = held(fill("UP", 10, 0.75))
    dec, _ = rc.evaluate_ladder(c, mv(0.89, 0.90, 0.09, 0.10), 10_000.0, 85.0, LAD, 100.0)
    _ladder_buy(c, dec, 10_000.0, "l1", 0.90)
    c2 = rc.RecoveryCycle.from_json(c.to_json())
    assert (c2.ladder_orders, c2.ladder_last_t, c2.ladder_cost) == (c.ladder_orders, c.ladder_last_t, c.ladder_cost)


# ───────────────────────────── through the engine ─────────────────────────────

def test_the_ladder_through_the_live_path_and_the_settings_rows():
    from test_pairlock_engine_v2 import _fill
    from test_pairlock_recovery_engine import _eng, _enter, _eval_books
    from test_spot_lag import SLUG, T0

    from troll_poly_bot.control import TUNABLES, apply_to, current_values
    e = _eng(rprm=LAD)
    _enter(e)                                                       # 10 UP @ 0.52
    t = T0 + 300_000.0 - 85_000.0                                   # 85 s before the close
    ev = _eval_books(e, t, up=(0.89, 0.90), down=(0.09, 0.10))
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "late" and ev.side == "UP" and ev.size == 10.0, (ev.reason, ev.detail)
    assert ev.p_used == pytest.approx(0.895) and 1 - ev.p_used == pytest.approx(0.105)   # live.py logs p (DOWN: 1 - p)
    o = e.to_order(ev, t)
    assert e.pending[SLUG]["ladder"]
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.90, 10.0, t + 300.0)
    cyc = e.cycles[SLUG]
    assert cyc.book.up_qty == 20.0 and cyc.ladder_orders == 1 and cyc.late_orders == 0
    assert (e.stats["ladder_sent"], e.stats["ladder_fills"], e.stats["ladder_qty"]) == (1, 1, 10.0)
    ev = _eval_books(e, t + 10_000.0, up=(0.89, 0.90), down=(0.09, 0.10))      # 10 s later: not yet
    assert not (ev.tradeable and ev.features["triangle"].get("trigger") == "late")
    ev = _eval_books(e, t + 20_500.0, up=(0.89, 0.90), down=(0.09, 0.10))      # 20 s later: the next one
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "late", (ev.reason, ev.detail)
    # the Settings rows reach this bot's parameters
    cfg = type("C", (), {"engine": None, "risk": None})()
    keys = ("pairlock.even_out_from", "pairlock.ladder_window_s", "pairlock.ladder_min_price", "pairlock.ladder_max_price",
            "pairlock.ladder_interval_s", "pairlock.ladder_shares")
    vals = current_values(cfg, None, pairlock=e.rprm)
    assert all(k in TUNABLES and k in vals for k in keys)
    clean = {k: TUNABLES[k].coerce(v)[0] for k, v in zip(keys, (3, 60.0, 0.88, 0.93, 15.0, 5.0))}
    assert apply_to(clean, cfg, None, pairlock=e.rprm)
    assert (e.rprm.even_out_from, e.rprm.ladder_window_s, e.rprm.ladder_min_price, e.rprm.ladder_max_price,
            e.rprm.ladder_interval_s, e.rprm.ladder_shares) == (3, 60.0, 0.88, 0.93, 15.0, 5.0)


def test_from_the_first_recovery_every_recovery_only_matches_the_opposite_amount():
    """The user, 21:50: "first recovery should be same with second recovery" (EVEN_OUT_FROM 1): 10 UP held, DOWN turns
    higher -> buy exactly 10 DOWN; both outcomes then pay the same and later swings have nothing to even."""
    prm = replace(PAPER, even_out_from=1)
    c = held(fill("UP", 10, 0.75))
    dec, rec = _eval(c, DOWN_UP, prm, now=10_000.0)
    assert dec is not None and dec.kind == "recovery" and dec.side == "DOWN" and dec.shares == 10.0, rec["why"]
    assert rec["recovery_number"] == 1 and rec["even_out"]
    _apply(c, dec, 10_000.0, "r1", price=0.72)
    o = rc.outcomes(c.book)
    assert c.rec_episodes == 1 and o["pnl_up_win"] == pytest.approx(o["pnl_down_win"])
    dec, rec = _eval(c, UP_UP, prm, now=20_000.0)
    assert dec is None and "leaves nothing to even" in rec["why"]
