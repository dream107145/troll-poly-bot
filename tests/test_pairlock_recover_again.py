"""Recover AGAIN on every swing (the user, 2026-10-05: "once it recovered a token, if the token falls again, it must be
recovered again too"). The live logs of both pair-lock bots showed why that failed: the model edge refused the
recovery at nearly every crossing, and once the requirement grew the whole plan had to fit the depth within
MAX_SLIPPAGE and every cap, or nothing at all was bought. EDGE_GATE off + STEP_FILL on fix both."""
from __future__ import annotations

import pytest

from test_pairlock_dynamic_recovery import _Model, feat, fill, held, mv

from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy.pairlock_book import fee_per_share
from troll_poly_bot.strategy.pairlock_recovery_engine import recovery_params_from_env

PRICE = dict(direction="price", depth_haircut=0.0, max_actions=30, max_total_position=300.0, max_capital=90.0,
             max_order_size=60.0)
NEW = rc.RecoveryParams(**PRICE, edge_gate=False, step_fill=True)
OLD = rc.RecoveryParams(**PRICE)


def _eval(c, view, prm, p_up=0.45, now=10_000.0, left=200.0, bankroll=100.0):
    return rc.evaluate_recovery(c, p_up, feat(), view, now, left, _Model(), prm, bankroll)


def _apply(c, dec, now, oid, price=None):
    rc.on_recovery_sent(c, dec, oid, now)
    rc.on_fill(c, fill(dec.side, dec.shares, price if price is not None else dec.vwap, kind="recovery", oid=oid), now + 300.0)
    rc.on_order_done(c, oid, True, now + 300.0)


def _branch(c, side):
    o = rc.outcomes(c.book)
    return o["pnl_up_win"] if side == "UP" else o["pnl_down_win"]


def test_the_crossing_is_recovered_without_the_model_edge():
    """UP 10 @ 0.60 fell; DOWN is now the higher price. The model says P(DOWN) 0.55, the ask is 0.56 + fee 0.017:
    the edge gate refuses (the logs: 'edge -0.02..-0.07/sh' at almost every crossing); without it the bot buys."""
    c = held(fill("UP", 10, 0.60))
    view = mv(0.43, 0.45, 0.55, 0.56)
    dec, rec = _eval(c, view, OLD)
    assert dec is None and rec["why"].startswith("edge "), rec["why"]
    dec, rec = _eval(c, view, NEW)
    assert dec is not None and dec.side == "DOWN", rec["why"]
    assert rec["edge_per_share"] < 0.0                       # recorded, not used


def test_every_swing_is_recovered_again():
    """UP falls -> buy DOWN; UP rises again -> buy UP; UP falls again -> buy DOWN; UP rises -> buy UP. Each time the
    side that is now higher gets its branch lifted back to the target, from the whole position."""
    c = held(fill("UP", 10, 0.60))
    swings = [("DOWN", mv(0.43, 0.45, 0.55, 0.56)),
              ("UP", mv(0.55, 0.56, 0.43, 0.45)),
              ("DOWN", mv(0.43, 0.45, 0.55, 0.56)),
              ("UP", mv(0.55, 0.56, 0.43, 0.45))]
    now = 10_000.0
    for k, (side, view) in enumerate(swings):
        assert _branch(c, side) < 0.50                       # the swing left this side's branch under the target
        dec, rec = _eval(c, view, NEW, now=now, bankroll=1_000.0)
        assert dec is not None and dec.side == side, (k, rec["why"])
        assert not rec["partial_step"], rec["step_bound"]
        _apply(c, dec, now, f"s{k}", price=view.ask(side))
        assert _branch(c, side) >= 0.50 - 1e-6, k          # recovered to the target again
        now += NEW.cooldown_ms + 1.0
    assert c.actions == 4 and [r["side"] for r in c.recoveries] == ["DOWN", "UP", "DOWN", "UP"]


def test_a_requirement_larger_than_the_book_is_bought_in_steps_not_skipped():
    """Only 18 DOWN within MAX_SLIPPAGE (0.56 x 8, 0.58 x 10; the next level 0.70) but ~30 are needed: the old rule
    bought nothing ('VWAP ... over the best ask' / 'insufficient depth'); now 18 are bought and the rest follows."""
    c = held(fill("UP", 20, 0.60))
    asks = [(0.56, 8.0), (0.58, 10.0), (0.70, 1000.0)]
    view = mv(0.43, 0.45, 0.55, 0.56, asks_down=asks)
    dec, rec = _eval(c, view, rc.RecoveryParams(**PRICE, edge_gate=False))
    assert dec is None and ("over the best ask" in rec["why"] or "depth" in rec["why"]), rec["why"]
    dec, rec = _eval(c, view, NEW)
    assert dec is not None and dec.shares == pytest.approx(18.0), rec["why"]
    assert rec["partial_step"] and rec["step_bound"] == "depth within MAX_SLIPPAGE"
    assert dec.limit == pytest.approx(0.59) and dec.plan_qty > 18.0
    _apply(c, dec, 10_000.0, "s1")
    # the book refills: the next step buys the rest of the requirement
    dec2, rec2 = _eval(c, mv(0.43, 0.45, 0.55, 0.56), NEW, now=13_000.0)
    assert dec2 is not None and dec2.side == "DOWN", rec2["why"]
    _apply(c, dec2, 13_000.0, "s2", price=0.56)
    assert _branch(c, "DOWN") >= 0.50 - 1e-6


def test_the_caps_shrink_the_step_and_only_stop_it_under_the_venue_minimum():
    c = held(fill("UP", 20, 0.60))
    view = mv(0.43, 0.45, 0.55, 0.56)
    unit = 0.59 + fee_per_share(0.59) + NEW.slippage_allowance           # limit 0.56 + 0.03
    prm = rc.RecoveryParams(**{**PRICE, "max_capital": 10.0}, edge_gate=False, step_fill=True)
    dec, rec = _eval(c, view, prm)
    assert dec is not None and rec["step_bound"] == "MAX_RECOVERY_CAPITAL"
    assert dec.shares == pytest.approx(int(10.0 / unit * 100) / 100.0)
    c.recovery_cost = 8.0                                                # 2 USDC left: under 5 shares
    dec, rec = _eval(c, view, prm)
    assert dec is None and rec["why"].startswith("MAX_RECOVERY_CAPITAL leaves"), rec["why"]
    dec, rec = _eval(held(fill("UP", 20, 0.60)), view, NEW, bankroll=6.0)
    assert dec is not None and rec["step_bound"] == "free bankroll" and dec.shares * unit <= 6.0 + 1e-9
    dec, rec = _eval(held(fill("UP", 20, 0.60)), view, rc.RecoveryParams(**{**PRICE, "max_total_position": 32.0},
                                                                         edge_gate=False, step_fill=True))
    assert dec is not None and dec.shares == pytest.approx(12.0) and rec["step_bound"] == "MAX_TOTAL_POSITION"


def test_nothing_is_bought_while_the_held_side_is_the_higher_one_or_has_not_fallen():
    c = held(fill("UP", 10, 0.60))
    dec, rec = _eval(c, mv(0.58, 0.60, 0.40, 0.42), NEW)                 # UP still the higher token
    assert dec is None and "no DOWN inventory" in rec["why"]
    c2 = held(fill("UP", 10, 0.50))
    dec, rec = _eval(c2, mv(0.48, 0.49, 0.51, 0.52), NEW)                # DOWN higher, but UP only 0.02 under its cost
    assert dec is None and "MIN_PRICE_MOVE" in rec["why"]


def test_defaults_keep_the_old_rule_and_the_env_switches_both():
    d = rc.RecoveryParams()
    assert d.edge_gate is True and d.step_fill is False
    p = recovery_params_from_env({"TPB_PAIRLOCK_REC_EDGE_GATE": "0", "TPB_PAIRLOCK_REC_STEP_FILL": "1"})
    assert p.edge_gate is False and p.step_fill is True
