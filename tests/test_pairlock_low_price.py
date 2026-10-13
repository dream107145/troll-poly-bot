"""LOW-PRICE FIRST LEG + DYNAMIC PARTIAL RECOVERY (the user's spec, 2026-10-07) on the pair-lock recovery engine:
the cheaper token as the first leg, through strategy B's EV gate; the recovery sized by calculate_recovery_quantity
at the executable VWAP toward the configured target (break even / target profit / partial / expected value), from
the WHOLE inventory, in (partial) steps that must improve the portfolio. The spec's tests 1-9 (10 = the existing
pair-lock suites)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from test_pairlock_dynamic_recovery import M, _Model, feat, fill, held, mv
from test_pairlock_engine_v2 import _model
from test_pairlock_recover_again import NEW, _apply, _branch, _eval

from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy import pairlock_v2 as plv2
from troll_poly_bot.strategy.pairlock_book import PairBook, fee_per_share
from troll_poly_bot.strategy.pairlock_recovery_engine import recovery_params_from_env

NOFEE = lambda p: 0.0                                     # noqa: E731  the spec's "before fees" figures
# the low-price app's recovery rules (ecosystem.pairlocklow.config.js)
LOW = replace(NEW, entry_mode="low", recovery_target_mode="break_even", recovery_min_price=0.55, min_price_move=0.01,
              min_pnl_improvement=0.05, pair_bid=False, late_enabled=False)
CHEAP = held(fill("UP", 20, 0.20))                        # 20 UP @ 0.20: cost 4 + fee


# ───────────────────────── the mathematics (spec tests 1-3, 6): the dearer the opposite, the MORE shares ─────────────────────────

@pytest.mark.parametrize("p2,expect", [(0.60, 10.0), (0.70, 13.33), (0.80, 20.0)])
def test_1_to_3_cheap_first_leg_needs_more_opposite_shares_as_the_opposite_gets_dearer(p2, expect):
    c = rc.calculate_recovery_quantity(20.0, 4.0, [(p2, 1000.0)], 0.0, fees=NOFEE)
    assert c["feasible"] and c["additional"] == pytest.approx(expect, abs=0.011) and c["existing"] == 0.0
    assert c["worst_before"] == pytest.approx(-4.0) and c["worst_after"] == pytest.approx(0.0, abs=0.02)
    assert c["first_win_after"] == pytest.approx(16.0 - expect * p2, abs=0.02)         # the first leg's win pays less
    # the useful property: a cheap first leg needs FEWER opposite shares than its own size while p1 + p2 < 1
    assert (c["additional"] < 20.0) == (0.20 + p2 < 1.0 - 1e-9)


def test_the_requirement_is_not_monotone_decreasing_in_the_opposite_price():
    q = [rc.calculate_recovery_quantity(20.0, 4.0, [(p, 1000.0)], 0.0, fees=NOFEE)["additional"] for p in (0.5, 0.6, 0.7, 0.8, 0.9)]
    assert q == sorted(q) and q[0] < q[-1]                                              # 8, 10, 13.33, 20, 40


def test_4_existing_opposite_inventory_counts_toward_the_requirement():
    """20 UP @ 0.20 (4) + 5 DOWN @ 0.60 (3) held: at a 1.60 target the position needs 14 DOWN -> 9 more."""
    inv = {"first": 20.0, "opp": 5.0, "total_cost": 7.0, "realised": 0.0}
    c = rc.calculate_recovery_quantity(20.0, 4.0, [(0.60, 1000.0)], 1.60, fees=NOFEE, inventory=inv)
    assert c["required_total"] == pytest.approx(14.0, abs=0.011) and c["existing"] == 5.0
    assert c["additional"] == pytest.approx(9.0, abs=0.011)
    # and through the engine's own sizing: the held DOWN enters the branch arithmetic, the plan buys the difference
    b = held(fill("UP", 20, 0.20), fill("DOWN", 5, 0.60, kind="recovery", oid="r0")).book
    plan = rc.plan_recovery(b, "DOWN", [(0.60, 1000.0)], replace(LOW, recovery_target_mode="target_profit", target_profit=1.60))
    need_nofee = 9.0
    assert need_nofee < plan["q_required"] < need_nofee + 2.0                            # fees + the slippage allowance add some
    assert plan["sizing"]["existing"] == 5.0 and plan["sizing"]["required_total"] == pytest.approx(5.0 + plan["q_required"])


def test_6_insufficient_depth_rejects_or_reduces_the_order():
    c = rc.calculate_recovery_quantity(20.0, 4.0, [(0.60, 3.0)], 0.0, fees=NOFEE)
    assert not c["feasible"] and "insufficient depth" in c["why"] and c["vwap"] is None
    # the engine: with only 3 shares inside MAX_SLIPPAGE the step is under the venue minimum -> nothing sent
    c2 = held(fill("UP", 20, 0.20))
    view = mv(0.14, 0.15, 0.59, 0.60, asks_down=[(0.60, 3.0), (0.70, 1000.0)])
    dec, rec = _eval(c2, view, LOW)
    assert dec is None and rec["verdict"] == "REJECT" and ("depth" in rec["why"] or "minimum" in rec["why"]), rec["why"]
    # with 6 shares inside MAX_SLIPPAGE the step is REDUCED to them, the rest follows after the fill
    view = mv(0.14, 0.15, 0.59, 0.60, asks_down=[(0.60, 6.0), (0.70, 1000.0)])
    dec, rec = _eval(c2, view, LOW)
    assert dec is not None and dec.shares == 6.0 and rec["step_bound"] == "depth within MAX_SLIPPAGE" and rec["partial_step"]


# ───────────────────────── through the recovery engine (spec tests 5, 7, 8, 9) ─────────────────────────

def test_5_a_partial_fill_recalculates_the_requirement_from_the_inventory():
    c = held(fill("UP", 20, 0.20))
    view = mv(0.14, 0.15, 0.59, 0.60)
    dec, rec = _eval(c, view, LOW, now=10_000.0)
    assert dec is not None and dec.side == "DOWN" and rec["verdict"] == "BUY", rec["why"]
    full = rec["required_opposite_qty"]
    assert 10.0 < full < 12.0 and rec["existing_opposite_qty"] == 0.0 and rec["additional_qty"] == pytest.approx(full)
    assert rec["worst_case_before"] == pytest.approx(_branch(c, "DOWN")) and rec["worst_case_after"] >= -1e-6
    rc.on_recovery_sent(c, dec, "r1", 10_000.0)
    rc.on_fill(c, fill("DOWN", 4, 0.60, kind="recovery", oid="r1"), 10_300.0)        # 4 of the ~10.4 filled
    rc.on_order_done(c, "r1", True, 10_300.0)
    assert c.book.down_qty == 4.0 and c.state is rc.RState.HOLDING
    dec2, rec2 = _eval(c, view, LOW, now=13_000.0)
    assert dec2 is not None, rec2["why"]
    assert rec2["existing_opposite_qty"] == 4.0
    assert rec2["additional_qty"] == pytest.approx(rec2["required_opposite_qty"] - 4.0, abs=0.011)
    assert full - 4.0 - 0.3 < dec2.shares < full - 4.0 + 0.3                           # the 4 held count (fees shift it a little)
    assert rec2["worst_case_before"] > rec["worst_case_before"]                        # the 4 shares already helped
    assert rec2["worst_case_after"] >= -1e-6
    # nothing filled at all: the requirement is unchanged
    c3 = held(fill("UP", 20, 0.20))
    d3, r3 = _eval(c3, view, LOW, now=10_000.0)
    rc.on_recovery_sent(c3, d3, "k1", 10_000.0)
    rc.on_order_done(c3, "k1", False, 10_300.0, "killed")
    d4, r4 = _eval(c3, view, LOW, now=13_000.0)
    assert d4 is not None and d4.shares == pytest.approx(d3.shares)


def test_7_a_recovery_that_does_not_improve_the_portfolio_is_a_wait_or_reject():
    c = held(fill("UP", 20, 0.20))
    view = mv(0.14, 0.15, 0.59, 0.60)
    # expected_value mode: the model gives DOWN 0.50 at an ask of 0.60 -> the hedge lowers E[P&L] -> REJECT
    dec, rec = _eval(c, view, replace(LOW, recovery_target_mode="expected_value"), p_up=0.50)
    assert dec is None and rec["verdict"] == "REJECT" and "lower E[P&L]" in rec["why"], rec["why"]
    assert rec["expected_after"] < rec["expected_before"]
    # the same at P(DOWN) 0.80: E[P&L] rises -> BUY
    dec, rec = _eval(c, view, replace(LOW, recovery_target_mode="expected_value"), p_up=0.20)
    assert dec is not None and rec["verdict"] == "BUY" and rec["expected_after"] > rec["expected_before"]
    # a step too small to improve the worse outcome by MIN_PNL_IMPROVEMENT -> WAIT
    dec, rec = _eval(c, mv(0.14, 0.15, 0.59, 0.60, asks_down=[(0.60, 5.0), (0.70, 1000.0)]), replace(LOW, min_pnl_improvement=3.0))
    assert dec is None and rec["verdict"] == "WAIT" and "MIN_PNL_IMPROVEMENT" in rec["why"], rec["why"]
    # the opposite token under RECOVERY_MIN_PRICE -> WAIT; the first leg not yet under its cost -> WAIT
    dec, rec = _eval(c, mv(0.14, 0.15, 0.54, 0.55), LOW)
    assert dec is None and rec["verdict"] == "WAIT" and rec["why"].startswith("waiting"), rec["why"]
    dec, rec = _eval(c, mv(0.20, 0.21, 0.78, 0.79), LOW)
    assert dec is None and rec["verdict"] == "WAIT" and "MIN_PRICE_MOVE" in rec["why"]


def test_8_fees_and_slippage_are_in_the_break_even():
    nofee = rc.calculate_recovery_quantity(20.0, 4.0, [(0.60, 1000.0)], 0.0, fees=NOFEE)
    real = rc.calculate_recovery_quantity(20.0, 4.0 + 20 * fee_per_share(0.20), [(0.60, 1000.0)], 0.0, slippage=0.005)
    assert real["additional"] > nofee["additional"] + 0.3                                 # fees + slippage need more shares
    assert real["worst_after"] >= -1e-6
    # 10 DOWN @ 0.60 (the no-fee answer) does NOT break even once the real fees are paid
    b = held(fill("UP", 20, 0.20), fill("DOWN", 10, 0.60, kind="recovery", oid="r")).book
    assert b.resolution_pnl("DOWN") < 0.0 and b.resolution_pnl("UP") > 0.0
    # and the engine's own break-even order leaves the worse outcome >= 0 after fees and the slippage allowance
    c = held(fill("UP", 20, 0.20))
    dec, rec = _eval(c, mv(0.14, 0.15, 0.59, 0.60), replace(LOW, min_pnl_improvement=0.0))
    assert dec is not None and rec["after_min_settlement_pnl"] >= -1e-6
    assert "GUARANTEED" not in rec["why"].upper()


def test_9_both_settlement_outcomes_realise_the_right_pnl():
    c = held(fill("UP", 20, 0.20))
    dec, _ = _eval(c, mv(0.14, 0.15, 0.69, 0.70), replace(LOW, min_pnl_improvement=0.0), now=10_000.0)
    _apply(c, dec, 10_000.0, "r1", price=0.70)
    b = c.book
    x = b.down_qty
    cost = 20 * 0.20 + 20 * fee_per_share(0.20) + x * 0.70 + x * fee_per_share(0.70)
    assert b.total_cost == pytest.approx(cost, abs=1e-6)
    assert b.resolution_pnl("UP") == pytest.approx(20.0 - cost, abs=1e-6)
    assert b.resolution_pnl("DOWN") == pytest.approx(x - cost, abs=1e-6)
    rc.on_close(c, 300_000.0)
    r = rc.on_settled(c, "DOWN", 340_000.0)
    assert r["pnl"] == pytest.approx(x - cost, abs=1e-6) and r["pnl_reconstructed"] == pytest.approx(r["pnl"], abs=1e-6)
    c2 = held(fill("UP", 20, 0.20))
    dec, _ = _eval(c2, mv(0.14, 0.15, 0.69, 0.70), replace(LOW, min_pnl_improvement=0.0), now=10_000.0)
    _apply(c2, dec, 10_000.0, "r1", price=0.70)
    rc.on_close(c2, 300_000.0)
    r2 = rc.on_settled(c2, "UP", 340_000.0)
    assert r2["pnl"] == pytest.approx(20.0 - c2.book.total_cost, abs=1e-6)


# ───────────────────────── the target modes and the low-price first leg ─────────────────────────

def test_the_target_modes():
    b = held(fill("UP", 20, 0.20)).book
    C = rc.first_leg_cost(b)
    assert C == pytest.approx(4.0 + 20 * fee_per_share(0.20))
    assert rc.recovery_target(b, replace(LOW, recovery_target_mode="target_profit", target_profit=0.5)) == 0.5
    assert rc.recovery_target(b, replace(LOW, recovery_target_mode="break_even")) == 0.0
    assert rc.recovery_target(b, replace(LOW, recovery_target_mode="partial_recovery", partial_target=0.5)) == pytest.approx(-0.5 * C)
    assert rc.recovery_target(b, replace(LOW, recovery_target_mode="expected_value")) == 0.0
    # partial recovery buys fewer shares than break-even, and the worse outcome ends at half the first leg's cost
    c = held(fill("UP", 20, 0.20))
    view = mv(0.14, 0.15, 0.59, 0.60)
    full, rf = _eval(c, view, replace(LOW, min_pnl_improvement=0.0))
    part, rp = _eval(c, view, replace(LOW, recovery_target_mode="partial_recovery", partial_target=0.5, min_pnl_improvement=0.0))
    assert part.shares < full.shares and rp["target"] == pytest.approx(-0.5 * C)
    assert rp["after_min_settlement_pnl"] == pytest.approx(-0.5 * C, abs=0.02)
    with pytest.raises(ValueError):
        rc.RecoveryParams(recovery_target_mode="guaranteed")
    with pytest.raises(ValueError):
        rc.RecoveryParams(low_price_min=0.5, low_price_max=0.4)


def _ask_model(p_up):
    import math
    return _model(math.log(p_up / (1 - p_up)))             # beta[1] = 1 on logit_mid = 0 -> P(UP) = p_up


def test_the_low_price_first_leg_is_the_cheaper_token_through_the_ev_gate():
    f = feat(tv1=1.0, tv3=1.0, pmflow=1.0, imb=1.0, mom1=1.0, mom3=1.0, agree=1.0)   # every flow signal toward UP
    view = mv(0.21, 0.22, 0.77, 0.78)
    side, rec = rc.low_entry_side(view, LOW, 200.0, f, _ask_model(0.30))
    assert side == "UP" and rec["decision"] == "BUY", rec["why"]
    assert rec["p_win"] == pytest.approx(0.30) and rec["vwap"] == pytest.approx(0.22) and rec["ev_per_share"] > 0.02
    assert rec["confirmations"] >= 3 and rec["shares"] == 10.0 and rec["limit"] == pytest.approx(0.24)
    # cheap is not enough: P(UP) 0.22 at an ask of 0.22 has no edge
    side, rec = rc.low_entry_side(view, LOW, 200.0, f, _ask_model(0.22))
    assert side is None and "entry EV" in rec["why"], rec["why"]
    # no flow toward the cheap side
    side, rec = rc.low_entry_side(view, LOW, 200.0, feat(), _ask_model(0.30))
    assert side is None and "LOW_MIN_SIGNALS" in rec["why"]
    side, rec = rc.low_entry_side(view, replace(LOW, low_require_flow_signal=False), 200.0, feat(), _ask_model(0.30))
    assert side == "UP"
    # the band, the window, the spread, the notional and the depth
    assert rc.low_entry_side(mv(0.47, 0.48, 0.51, 0.52), LOW, 200.0, f, _ask_model(0.6))[1]["why"].startswith("cheaper token UP ask 0.48 outside")
    assert "window" in rc.low_entry_side(view, LOW, 100.0, f, _ask_model(0.30))[1]["why"]
    assert "window" in rc.low_entry_side(view, LOW, 298.0, f, _ask_model(0.30))[1]["why"]
    assert rc.low_entry_side(mv(0.10, 0.22, 0.77, 0.78), LOW, 200.0, f, _ask_model(0.30))[1]["why"] == "spread"
    assert "LOW_MAX_NOTIONAL" in rc.low_entry_side(view, replace(LOW, low_max_notional=2.0), 200.0, f, _ask_model(0.30))[1]["why"]
    thin = mv(0.21, 0.22, 0.77, 0.78, asks_up=[(0.22, 3.0), (0.30, 1000.0)])
    assert "LOW_MAX_SLIPPAGE" in rc.low_entry_side(thin, LOW, 200.0, f, _ask_model(0.40))[1]["why"]   # EV passes, the walk does not
    # the dearer token is never the first leg here, however good the model thinks it is
    f_down = feat(tv1=-1.0, tv3=-1.0, pmflow=-1.0, imb=-1.0, mom1=-1.0, mom3=-1.0, agree=-1.0)
    side, rec = rc.low_entry_side(mv(0.77, 0.78, 0.21, 0.22), LOW, 200.0, f_down, _ask_model(0.70))
    assert side == "DOWN"


def test_the_low_price_app_config_and_the_old_defaults():
    p = recovery_params_from_env({"TPB_PAIRLOCK_REC_ENTRY_MODE": "low", "TPB_PAIRLOCK_REC_LOW_PRICE_MIN": "0.05",
                                  "TPB_PAIRLOCK_REC_LOW_PRICE_MAX": "0.45", "TPB_PAIRLOCK_REC_LOW_MIN_EDGE": "0.02",
                                  "TPB_PAIRLOCK_REC_RECOVERY_TARGET_MODE": "break_even", "TPB_PAIRLOCK_REC_PARTIAL_TARGET": "0.5",
                                  "TPB_PAIRLOCK_REC_MIN_PNL_IMPROVEMENT": "0.05", "TPB_PAIRLOCK_REC_LOW_REQUIRE_FLOW_SIGNAL": "1",
                                  "TPB_PAIRLOCK_REC_LOW_MIN_SIGNALS": "2"})
    assert (p.entry_mode, p.low_price_min, p.low_price_max, p.low_min_edge, p.recovery_target_mode, p.partial_target,
            p.min_pnl_improvement, p.low_require_flow_signal, p.low_min_signals) == ("low", 0.05, 0.45, 0.02, "break_even", 0.5, 0.05, True, 2)
    d = rc.RecoveryParams()                                       # every existing bot: nothing changed
    assert d.entry_mode == "trigger" and d.recovery_target_mode == "target_profit" and d.min_pnl_improvement == 0.0
    assert rc.recovery_target(PairBook(M), d) == d.target_profit == 0.5


def test_the_low_mode_through_the_engine_buys_the_cheaper_token_then_recovers_to_break_even():
    from test_pairlock_engine_v2 import _feed_to, _fill
    from test_pairlock_recovery_engine import _deep, _eng, _eval_books
    from test_spot_lag import SLUG, T0, _Vol, _book
    from troll_poly_bot.execution.latency import COLOCATED
    from troll_poly_bot.strategy.pairlock_recovery_engine import PairLockRecoveryEngine   # noqa: F401
    prm = replace(LOW, max_order_size=60.0, max_capital=90.0, max_total_position=300.0)
    e = _eng(rprm=prm, model=_ask_model(0.35))
    e._features = lambda meta, now: feat(tv1=1.0, tv3=1.0, pmflow=1.0, imb=1.0, mom1=1.0, mom3=1.0, agree=1.0)   # noqa: SLF001
    t = T0 + 60_000.0                                                # 240 s left: inside the low window
    ev = _eval_books(e, t, up=(0.21, 0.22), down=(0.77, 0.78))
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "open" and ev.side == "UP" and ev.size == 10.0, (ev.reason, ev.detail)
    assert e.stats["low_candidates"] == 1
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.22, 10.0, t + 300.0)
    cyc = e.cycles[SLUG]
    assert cyc.book.up_qty == 10.0 and cyc.pair is None            # no pair bid in this mode
    # the dearer token would never be bought: the higher-ask side is not a low-price candidate
    e2 = _eng(rprm=prm, model=_ask_model(0.80))
    e2._features = e._features                                       # noqa: SLF001
    ev2 = _eval_books(e2, t, up=(0.77, 0.78), down=(0.21, 0.22))
    assert not ev2.tradeable and SLUG not in e2.cycles
    # UP falls, DOWN is the higher price at 0.60: one break-even recovery sized from the whole inventory
    e._features = lambda meta, now: feat()                           # noqa: SLF001
    ev3 = _eval_books(e, t + 30_000.0, up=(0.14, 0.15), down=(0.59, 0.60))
    assert ev3.tradeable and ev3.features["triangle"]["trigger"] == "recovery" and ev3.side == "DOWN", (ev3.reason, ev3.detail)
    pk = ev3.features["triangle"]["pairlock"]
    assert pk["after_pnl_up_win"] > -1e-6 or pk["after_pnl_down_win"] > -1e-6
    assert 5.0 < ev3.size < 6.5                                      # 10 UP @ 0.22 (2.33 with fees) at 0.60: ~5.9 DOWN


def test_a_bot_can_leave_the_shared_pairlock_settings_rows_alone(monkeypatch):
    """TPB_CONTROLS_IGNORE pairlock.*: the dashboard's shared pair-lock rows do not override this app's config."""
    from troll_poly_bot.control import TUNABLES, apply_to
    prm = replace(LOW)
    cfg = type("C", (), {"engine": None, "risk": None})()
    vals = {"pairlock.even_out_from": TUNABLES["pairlock.even_out_from"].coerce(1)[0],
            "pairlock.ladder_window_s": TUNABLES["pairlock.ladder_window_s"].coerce(120.0)[0]}
    monkeypatch.setenv("TPB_CONTROLS_IGNORE", "pairlock.*")
    assert apply_to(vals, cfg, None, pairlock=prm) == [] and prm.even_out_from == 0 and prm.ladder_window_s == 0.0
    monkeypatch.setenv("TPB_CONTROLS_IGNORE", "pairlock.ladder_window_s")
    assert apply_to(vals, cfg, None, pairlock=prm) and prm.even_out_from == 1 and prm.ladder_window_s == 0.0
    monkeypatch.delenv("TPB_CONTROLS_IGNORE")
    assert apply_to(vals, cfg, None, pairlock=prm) and prm.ladder_window_s == 120.0
