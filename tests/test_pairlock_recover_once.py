"""Recover ONCE (the user, 2026-10-05: "recover the falling token only once, to reduce losses"). MAX_RECOVERIES caps how
many recoveries a market gets; one recovery is the buys toward one side while it stays the higher price (a plan bought
in steps is one recovery). After the last one the market is held to settlement: no further recovery or lock buys."""
from __future__ import annotations

from dataclasses import replace

import pytest

from test_pairlock_dynamic_recovery import fill, held, mv
from test_pairlock_loss_limit import LIMITS
from test_pairlock_recover_again import NEW, _apply, _branch, _eval

from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy.pairlock_recovery_engine import recovery_params_from_env

ONCE = replace(NEW, max_recoveries=1)
DOWN_UP = mv(0.43, 0.45, 0.55, 0.56)          # UP fell: DOWN is the higher token
UP_UP = mv(0.55, 0.56, 0.43, 0.45)            # UP back on top


def test_one_recovery_then_the_market_is_held():
    c = held(fill("UP", 10, 0.60))
    dec, rec = _eval(c, DOWN_UP, ONCE)
    assert dec is not None and dec.side == "DOWN", rec["why"]
    _apply(c, dec, 10_000.0, "r1", price=0.56)
    assert c.rec_episodes == 1 and _branch(c, "DOWN") >= 0.5 - 1e-6
    before = rc.outcomes(c.book)
    dec, rec = _eval(c, UP_UP, ONCE, now=20_000.0)                   # the price swings back: UP is higher again
    assert dec is None and rec["why"].startswith("recovered 1 time(s) already"), rec["why"]
    dec, rec = _eval(c, DOWN_UP, ONCE, now=30_000.0)                 # and back: DOWN's branch is still at its target
    assert dec is None and rec["decision"] == "NONE"
    assert rc.outcomes(c.book) == before and len(c.recoveries) == 1  # nothing more was bought


def test_the_lock_does_not_buy_after_the_recovery_either():
    prm = replace(LIMITS, max_recoveries=1)
    c = held(fill("UP", 10, 0.60))
    dec, _ = _eval(c, DOWN_UP, prm)
    assert dec is not None and dec.kind == "recovery"
    _apply(c, dec, 10_000.0, "r1", price=0.56)
    dec, rec = _eval(c, UP_UP, prm, now=20_000.0, left=40.0)          # late, inside the lock window: still held
    assert dec is None and "MAX_RECOVERIES" in rec["why"] and not c.locked


def test_a_recovery_bought_in_steps_is_one_recovery():
    c = held(fill("UP", 20, 0.60))
    thin = mv(0.43, 0.45, 0.55, 0.56, asks_down=[(0.56, 8.0), (0.58, 10.0), (0.70, 1000.0)])
    dec, rec = _eval(c, thin, ONCE)
    assert dec is not None and rec["partial_step"]
    _apply(c, dec, 10_000.0, "s1")
    dec2, rec2 = _eval(c, DOWN_UP, ONCE, now=13_000.0)               # the same recovery goes on
    assert dec2 is not None and dec2.side == "DOWN", rec2["why"]
    _apply(c, dec2, 13_000.0, "s2", price=0.56)
    assert c.rec_episodes == 1 and _branch(c, "DOWN") >= 0.5 - 1e-6


def test_an_order_that_never_filled_does_not_use_the_recovery_up():
    c = held(fill("UP", 10, 0.60))
    dec, _ = _eval(c, DOWN_UP, ONCE)
    rc.on_recovery_sent(c, dec, "k1", 10_000.0)
    rc.on_order_done(c, "k1", False, 10_300.0, "fok_unfillable")
    assert c.rec_episodes == 0 and c.state is rc.RState.HOLDING
    dec, rec = _eval(c, DOWN_UP, ONCE, now=13_000.0)
    assert dec is not None and dec.side == "DOWN", rec["why"]


def test_two_recoveries_allowed_then_held():
    prm = replace(NEW, max_recoveries=2)
    c = held(fill("UP", 10, 0.60))
    now = 10_000.0
    for k, (side, view) in enumerate((("DOWN", DOWN_UP), ("UP", UP_UP))):
        dec, rec = _eval(c, view, prm, now=now, bankroll=1_000.0)
        assert dec is not None and dec.side == side, rec["why"]
        _apply(c, dec, now, f"r{k}", price=view.ask(side))
        now += 5_000.0
    dec, rec = _eval(c, DOWN_UP, prm, now=now)
    assert dec is None and "MAX_RECOVERIES 2" in rec["why"] and c.rec_episodes == 2


def test_default_unlimited_env_and_restart():
    assert rc.RecoveryParams().max_recoveries == 0
    assert recovery_params_from_env({"TPB_PAIRLOCK_REC_MAX_RECOVERIES": "1"}).max_recoveries == 1
    with pytest.raises(ValueError):
        rc.RecoveryParams(max_recoveries=-1)
    c = held(fill("UP", 10, 0.60))
    c.rec_episodes, c.rec_open, c.rec_side = 1, True, "DOWN"
    c2 = rc.RecoveryCycle.from_json(c.to_json())
    assert (c2.rec_episodes, c2.rec_open, c2.rec_side) == (1, True, "DOWN")
