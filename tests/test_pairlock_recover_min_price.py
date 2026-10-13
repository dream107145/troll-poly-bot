"""Recover only above a price (the user, 2026-10-05, recovery bot: "repeat the recovery, not once; but every recovery
must be tried when the price is over 0.70"). RECOVERY_MIN_PRICE: the bot waits until the token the recovery buys has
an ask above it; every swing past it is recovered again (MAX_RECOVERIES 0 = no limit)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from test_pairlock_dynamic_recovery import fill, held, mv
from test_pairlock_loss_limit import LIMITS
from test_pairlock_recover_again import NEW, _apply, _branch, _eval

from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy.pairlock_recovery_engine import recovery_params_from_env

P70 = replace(NEW, recovery_min_price=0.70, max_recoveries=0)


def test_it_waits_until_the_recovery_token_is_above_the_price():
    c = held(fill("UP", 10, 0.55))
    for view in (mv(0.43, 0.45, 0.55, 0.56), mv(0.30, 0.31, 0.69, 0.70)):    # DOWN higher, but 0.56 / 0.70
        dec, rec = _eval(c, view, P70)
        assert dec is None and rec["decision"] == "NONE" and rec["why"].startswith("waiting: DOWN ask"), rec["why"]
    dec, rec = _eval(c, mv(0.27, 0.28, 0.71, 0.72), P70)                       # 0.72: recover
    assert dec is not None and dec.side == "DOWN" and dec.kind == "recovery", rec["why"]


def test_every_swing_past_the_price_is_recovered_again():
    c = held(fill("UP", 10, 0.55))
    swings = [("DOWN", mv(0.27, 0.28, 0.71, 0.72)), ("UP", mv(0.71, 0.72, 0.27, 0.28)),
              ("DOWN", mv(0.27, 0.28, 0.71, 0.72))]
    roomy = replace(P70, max_capital=1_000.0, max_total_position=2_000.0, max_order_size=500.0)
    now = 10_000.0
    for k, (side, view) in enumerate(swings):
        dec, rec = _eval(c, view, roomy, now=now, bankroll=10_000.0)
        assert dec is not None and dec.side == side, (k, rec["why"])
        _apply(c, dec, now, f"r{k}", price=view.ask(side))
        assert _branch(c, side) >= 0.5 - 1e-6
        now += 5_000.0
    assert c.rec_episodes == 3 and [r["side"] for r in c.recoveries] == ["DOWN", "UP", "DOWN"]
    q = [r["qty"] for r in c.recoveries]
    assert q[1] > 1.8 * q[0] and q[2] > 1.8 * q[1]                 # at 0.72 every swing about doubles the buy


def test_the_recovery_capital_cap_still_ends_a_long_chain():
    c = held(fill("UP", 10, 0.55))
    now, views = 10_000.0, [("DOWN", mv(0.27, 0.28, 0.71, 0.72)), ("UP", mv(0.71, 0.72, 0.27, 0.28))] * 3
    for k, (side, view) in enumerate(views):
        dec, rec = _eval(c, view, P70, now=now, bankroll=10_000.0)
        if dec is None:
            assert "MAX_RECOVERY_CAPITAL" in rec["why"], rec["why"]
            break
        _apply(c, dec, now, f"r{k}", price=view.ask(side))
        now += 5_000.0
    assert c.recovery_cost <= P70.max_capital + 1e-6


def test_with_the_loss_limits_on_a_swing_past_the_price_locks_instead():
    """MAX_MARKET_LOSS 8 / RECOVERY_MAX_PRICE 0.75: recovering 10 UP bought at 0.55 at 0.72 needs ~24 DOWN, leaving the
    UP-win branch near -13 -- over the limit, so the market is locked instead (the rules conflict by design)."""
    c = held(fill("UP", 10, 0.55))
    dec, rec = _eval(c, mv(0.27, 0.28, 0.71, 0.72), replace(LIMITS, recovery_min_price=0.70, max_recoveries=0))
    assert dec is not None and dec.kind == "lock" and "MAX_MARKET_LOSS" in dec.reason


def test_env_and_validation():
    p = recovery_params_from_env({"TPB_PAIRLOCK_REC_RECOVERY_MIN_PRICE": "0.70", "TPB_PAIRLOCK_REC_MAX_RECOVERIES": "0"})
    assert p.recovery_min_price == 0.70 and p.max_recoveries == 0
    assert rc.RecoveryParams().recovery_min_price == 0.0
    with pytest.raises(ValueError):
        rc.RecoveryParams(recovery_min_price=1.0)
