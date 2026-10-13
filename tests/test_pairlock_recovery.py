"""The 2026-10-02 recovery-hedge / profit-lock spec, item 22: the twenty required tests, numbered as in the spec.
Accounting is the fills-based PairBook (strategy/pairlock_book.py); decisions are pairlock.manage_position."""
from __future__ import annotations

import itertools
import json
import math
from dataclasses import replace

import pytest

from troll_poly_bot.strategy import pairlock as pl
from troll_poly_bot.strategy import pairlock_book as pb
from troll_poly_bot.strategy.pairlock_book import FillRecord, PairBook, PendingOrder, State, fee_per_share

M = "btc-updown-5m-1"
_ids = itertools.count(1)
AGAINST_UP = dict(tv1=-5.0, tv3=-5.0, tacc=-3.0, mom1=-3.0, mom3=-3.0, imb=-0.5, pmflow=-0.5, agree=-1.0)


def _model():
    m = pl.ProbModel(beta=[0.0, 1.0] + [0.0] * (len(pl.FEATURES) - 1))     # P(UP) = the logit_mid feature
    m.exec = {"entry_fill_rate": 1.0, "entry_slip": 0.0, "entry_adv": 0.0, "hedge_fill_rate": 1.0, "hedge_slip": 0.0}
    return m


def _feat(p_up=0.5, **kw):
    f = {k: 0.0 for k in pl.FEATURES}
    f.update({"logit_mid": math.log(p_up / (1 - p_up)), "mid_up": p_up, "left": 150.0})
    f.update(kw)
    return f


def fill(side, qty, price, kind="entry", order_id=None, expected=None, fee=None):
    n = next(_ids)
    return FillRecord(M, float(n), side.lower(), side, qty, price, fee_per_share(price) * qty if fee is None else fee,
                      order_id or f"o{n}", f"f{n}", "BUY", price if expected is None else expected, kind)


def book(*fills_):
    b = PairBook(M)
    for f in fills_:
        b.add_fill(f)
    return b


def cycle(side="UP", q=10.0, px=0.50, prm=None):
    prm = prm or pl.PairLockParams()
    c = pl.Cycle(book=PairBook(M), a_side=side)
    pl.on_entry_sent(c, "e1", q, px + 0.03, 0.0)
    pl.on_fill(c, fill(side, q, px, order_id="e1"), prm, 0.0)
    pl.on_order_done(c, "e1", True, 0.0, prm=prm)
    return c


def mv(bid_up, depth=1e6, down_levels=None):
    ask_up = round(bid_up + 0.01, 2)
    bid_dn, ask_dn = round(1 - ask_up, 2), round(1 - bid_up, 2)
    return pl.MarketView(bid_up=bid_up, ask_up=ask_up, bid_down=bid_dn, ask_down=ask_dn, asks_up=[(ask_up, depth)],
                         asks_down=down_levels or [(ask_dn, depth)], bids_up=[(bid_up, depth)], bids_down=[(bid_dn, depth)])


def manage(c, f, view, prm, now=1000.0, left=150.0):
    return pl.manage_position(c, f, view, now, left, _model(), prm, pl.RiskLimits(),
                              pl.MarketExposure(capital=c.book.cash_spent, shares=c.book.up_qty + c.book.down_qty,
                                                held=True))


RECOVER = pl.PairLockParams(max_acceptable_pair_cost=1.30, recovery_confirmation_window_ms=0.0)


def recovered(q_fill=10.0, q_req=10.0, prm=RECOVER):
    """10 UP @ 0.50, UP fell to 0.40 with every reversal signal: a recovery hedge decided, sent, filled q_fill."""
    c = cycle("UP", 10, 0.50, prm)
    dec, why, _ = manage(c, _feat(0.4, **AGAINST_UP), mv(0.40), prm)
    assert dec is not None and dec.kind == "recovery", why
    pl.on_hedge_sent(c, "h1", "DOWN", q_req, dec.limit, 1000.0, "recovery", dec.info)
    if q_fill > 0:
        pl.on_fill(c, fill("DOWN", q_fill, 0.60, kind="recovery", order_id="h1", expected=0.60), prm, 1300.0)
    pl.on_order_done(c, "h1", q_fill > 0, 1300.0, "fok_unfillable" if q_fill <= 0 else "", prm=prm)
    return c, dec


# 1 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_01_ten_up_only():
    b = book(fill("UP", 10, 0.80))
    cost = 10 * 0.80 + 10 * fee_per_share(0.80)
    assert (b.up_qty, b.down_qty, b.matched_qty, b.unmatched_up) == (10, 0, 0, 10)
    assert b.total_cost == pytest.approx(cost)
    assert b.locked_pair_pnl == 0.0 and b.pair_cost_per_share is None
    assert b.resolution_pnl("UP") == pytest.approx(10 - cost) and b.resolution_pnl("DOWN") == pytest.approx(-cost)
    assert b.worst_case_pnl == pytest.approx(-cost)
    assert not pb.pair_lock_check(b, -1.0, -100.0)[0]


# 2 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_02_ten_up_ten_down():
    b = book(fill("UP", 10, 0.40, fee=0.0), fill("DOWN", 10, 0.50, fee=0.0))
    assert b.matched_qty == 10 and b.unmatched == 0
    assert b.matched_pair_cost == pytest.approx(9.00) and b.locked_pair_pnl == pytest.approx(1.00)
    assert b.worst_case_pnl == pytest.approx(1.00) == pytest.approx(b.resolution_pnl("UP"))
    c = pl.Cycle(book=b, a_side="UP")
    assert pl.watch_state(c, pl.PairLockParams()) is State.PAIR_LOCKED


# 3 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_03_ten_up_six_down_is_six_locked_four_directional():
    b = book(fill("UP", 10, 0.40), fill("DOWN", 6, 0.50))
    assert (b.matched_qty, b.unmatched_up, b.unmatched_down) == (6, 4, 0)
    assert b.locked_pair_pnl == pytest.approx(6 * (1 - 0.40 - 0.50) - 6 * (fee_per_share(0.40) + fee_per_share(0.50)))
    assert b.worst_case_pnl == pytest.approx(6 - b.total_cost)          # the portfolio worst case, not the pair lock
    assert b.worst_case_pnl != pytest.approx(b.locked_pair_pnl)
    c = pl.Cycle(book=b, a_side="UP")
    assert pl.watch_state(c, pl.PairLockParams()) is State.UNMATCHED_DIRECTIONAL   # never "locked" as a whole
    assert not pb.pair_lock_check(b, -1.0, -100.0)[0]
    assert "4 unmatched UP DIRECTIONAL (not locked)" in pl._inv(c)               # noqa: SLF001


# 4 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_04_six_up_ten_down():
    b = book(fill("UP", 6, 0.55), fill("DOWN", 10, 0.35))
    assert (b.matched_qty, b.unmatched_up, b.unmatched_down, b.heavy_side) == (6, 0, 4, "DOWN")
    assert b.resolution_pnl("UP") == pytest.approx(6 - b.total_cost)
    assert b.resolution_pnl("DOWN") == pytest.approx(10 - b.total_cost)
    assert b.worst_case_pnl == pytest.approx(6 - b.total_cost)
    assert pl.watch_state(pl.Cycle(book=b, a_side="DOWN"), pl.PairLockParams()) is State.UNMATCHED_DIRECTIONAL


# 5 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_05_partial_hedge_counts_only_what_filled():
    c, dec = recovered(q_fill=6.0, q_req=10.0)
    b = c.book
    assert (b.up_qty, b.down_qty, b.matched_qty, b.unmatched_up) == (10, 6, 6, 4)
    assert c.state is State.UNMATCHED_DIRECTIONAL                            # 6 pairs + 4 UP still directional
    names = [e.name for e in c.events]
    assert "PARTIAL_HEDGE" in names and "PAIR_FORMED" in names and "RECOVERY_HEDGE_FILLED" in names
    # every figure recomputed from the 6 that filled, not the 10 requested
    assert b.worst_case_pnl == pytest.approx(6 - b.total_cost)
    assert b.expected_settlement_pnl(0.4) == pytest.approx(0.4 * 10 + 0.6 * 6 - b.total_cost)
    assert b.pair_cost_per_share == pytest.approx(0.50 + 0.60 + fee_per_share(0.50) + fee_per_share(0.60))
    assert c.recovery_qty == 6 and not b.pending


# 6 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_06_hedge_completely_unfilled_leaves_the_exposure():
    c, _ = recovered(q_fill=0.0)
    assert c.book.down_qty == 0 and c.book.matched_qty == 0 and c.book.up_qty == 10 and not c.book.pending
    assert c.hedge_kills == 1 and c.state in (State.REVERSAL_CONFIRMED, State.ADVERSE_MOVE)
    assert c.events[-1].name == "RECOVERY_HEDGE_NOT_FILLED"


# 7 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_07_expensive_opposite_ask_is_rejected():
    prm = replace(RECOVER, max_acceptable_pair_cost=1.05)
    c = cycle("UP", 10, 0.50, prm)
    dec, why, rec = manage(c, _feat(0.4, **AGAINST_UP), mv(0.40), prm)    # DOWN at 0.60: pair ~1.14
    assert dec is None and "MAX_ACCEPTABLE_PAIR_COST" in why
    assert all(x["rejected"] for x in rec["candidates"] if x["q"] > 0)
    assert c.events[-1].name == "RECOVERY_HEDGE_REJECTED"


# 8 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_08_cheap_opposite_ask_is_a_profit_lock():
    prm = pl.PairLockParams()                                               # default: PROFIT_LOCK_MAX_COST 0.95
    c = cycle("UP", 10, 0.80, prm)
    dec, why, rec = manage(c, _feat(0.9), mv(0.88), prm)                   # UP ran to 0.88: DOWN at 0.12
    assert dec is not None and dec.kind == "profit_lock" and dec.side == "DOWN", why
    assert dec.info["pair_cost"] == pytest.approx(0.80 + fee_per_share(0.80) + 0.125 + fee_per_share(0.12))
    assert dec.info["pair_cost"] <= prm.profit_lock_max_cost
    assert dec.info["locked_profit"] == pytest.approx(dec.shares * (1 - dec.info["pair_cost"]))


# 9 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_09_insufficient_depth():
    c = cycle("UP", 10, 0.50, RECOVER)
    dec, why, rec = manage(c, _feat(0.4, **AGAINST_UP), mv(0.40, down_levels=[(0.60, 3.0)]), RECOVER)
    assert dec is None and "not enough executable depth" in why
    assert {x["why"] for x in rec["candidates"] if x["q"] >= 5} == {"not enough executable depth"}


# 10 ─────────────────────────────────────────────────────────────────────────────────────────────


def test_10_fees_eliminate_the_pair_edge():
    b = book(fill("UP", 10, 0.50))
    (c,) = pb.evaluate_hedges(b, 0.5, [(0.45, 100.0)], fractions=(1.0,))
    assert 1 - 0.50 - 0.45 == pytest.approx(0.05)                           # 5c gross: would pass the 5c minimum ...
    assert c.incr_pair_edge == pytest.approx(0.05 - fee_per_share(0.50) - fee_per_share(0.45))
    assert "PROFIT_LOCK_MIN_EDGE" in pl.profit_lock_constraints(c, pl.PairLockParams(profit_lock_max_cost=1.0))
    cy = cycle("UP", 10, 0.50)
    assert manage(cy, _feat(0.5), mv(0.55), pl.PairLockParams())[1] == "no profit-lock opportunity"


# 11 ─────────────────────────────────────────────────────────────────────────────────────────────


def test_11_slippage_eliminates_the_pair_edge():
    b = book(fill("UP", 10, 0.80))
    edge0 = 1 - (0.80 + fee_per_share(0.80) + 0.12 + fee_per_share(0.12))
    assert edge0 > 0.05                                                     # qualifies with no slippage ...
    ok = pb.evaluate_hedges(b, 0.9, [(0.12, 100.0)], fractions=(1.0,), slippage_allowance=0.0)[0]
    bad = pb.evaluate_hedges(b, 0.9, [(0.12, 100.0)], fractions=(1.0,), slippage_allowance=0.02)[0]
    prm = pl.PairLockParams()
    assert pl.profit_lock_constraints(ok, prm) == "" and pl.profit_lock_constraints(bad, prm) != ""
    # realised: a fill 2c worse than expected leaves a pair under MIN_PAIR_EDGE -- not a PAIR_LOCKED position
    prm2 = pl.PairLockParams(min_pair_edge=0.05)
    c = cycle("UP", 10, 0.80, prm2)
    c.to(State.PROFIT_LOCK_EVALUATION, 1.0)
    pl.on_hedge_sent(c, "h1", "DOWN", 10.0, 0.14, 1.0, "profit_lock")
    pl.on_fill(c, fill("DOWN", 10, 0.14, kind="profit_lock", order_id="h1", expected=0.12), prm2, 2.0)
    pl.on_order_done(c, "h1", True, 2.0, prm=prm2)
    assert c.book.slippage == pytest.approx(10 * 0.02)
    assert c.book.net_pair_edge < 0.05 and c.state is State.RECOVERY_HEDGED and c.state is not State.PAIR_LOCKED


# 12 ─────────────────────────────────────────────────────────────────────────────────────────────


def test_12_a_pending_order_is_not_inventory():
    c = cycle("UP", 10, 0.50, RECOVER)
    dec, _, _ = manage(c, _feat(0.4, **AGAINST_UP), mv(0.40), RECOVER)
    pl.on_hedge_sent(c, "h1", "DOWN", dec.shares, dec.limit, 1000.0, "recovery")
    b = c.book
    assert c.state is State.RECOVERY_HEDGE_PENDING and "h1" in b.pending
    assert (b.down_qty, b.matched_qty, b.unmatched_up) == (0, 0, 10)
    assert b.worst_case_pnl == pytest.approx(-b.total_cost)                 # nothing hedged yet
    assert c.recovery_qty == 0 and b.summary()["pending"][0]["qty"] == dec.shares


# 13 ─────────────────────────────────────────────────────────────────────────────────────────────


def test_13_a_recovery_hedge_is_never_followed_by_a_new_entry():
    c, _ = recovered(q_fill=10.0)
    assert c.state in (State.RECOVERY_HEDGED, State.PAIR_LOCKED) and c.book.unmatched == 0
    m = _model()
    f = _feat(0.7)
    mkt = pl.MarketExposure(capital=c.book.cash_spent, shares=20.0, cycles=1, held=True, held_state=c.state.value)
    dec, why = pl.entry_decision(f, "UP", 0.50, 0.49, [(0.50, 1e6)], m, pl.PairLockParams(entry_edge=-1.0),
                                 pl.RiskLimits(max_market_capital=100.0, max_market_loss=100.0), mkt, 200.0, c)
    assert dec is None and why.startswith("REENTRY_BLOCKED")
    # and the state machine itself has no road back: RECOVERY_HEDGED / PAIR_LOCKED -> ... -> SETTLED only
    for st in (State.RECOVERY_HEDGED, State.PAIR_LOCKED):
        assert State.INITIAL_ENTRY not in pb.TRANSITIONS[st] and State.WAITING not in pb.TRANSITIONS[st]


# 14 / 15 ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("winner", ["UP", "DOWN"])
def test_14_15_settlement_with_either_winner(winner):
    c, _ = recovered(q_fill=6.0, q_req=10.0)
    assert pl.on_close(c, 300_000.0) and c.state is State.SETTLEMENT_PENDING
    r = pl.on_settled(c, winner, 340_000.0)
    b = c.book
    settlement_value = b.up_qty if winner == "UP" else b.down_qty
    assert r["pnl"] == pytest.approx(settlement_value - b.total_cost)
    assert r["pnl"] == pytest.approx(r["pnl_from_cash"]) == pytest.approx(r["pnl_reconstructed"])
    assert r["prediction"] + r["recovery_leg"] + r["profit_lock_leg"] == pytest.approx(r["total"])
    assert r["pairs"] + r["directional"] == pytest.approx(r["total"])
    assert r["pairs"] == pytest.approx(b.matched_qty - b.matched_pair_cost)   # the pair lock, separately
    assert [t.to for t in c.transitions][-3:] == ["SETTLEMENT_PENDING", "MARKET_RESOLVED", "SETTLED"]
    assert c.events[-1].name == "PAIR_SETTLED"


# 16 ─────────────────────────────────────────────────────────────────────────────────────────────


def test_16_multiple_fills_at_different_prices():
    prm = RECOVER
    c = cycle("UP", 10, 0.50, prm)
    dec, _, _ = manage(c, _feat(0.4, **AGAINST_UP), mv(0.40, down_levels=[(0.60, 4.0), (0.61, 100.0)]), prm)
    assert dec is not None and dec.info["vwap"] == pytest.approx((4 * 0.60 + 6 * 0.61) / 10)
    pl.on_hedge_sent(c, "h1", "DOWN", 10.0, dec.limit, 1000.0, "recovery")
    pl.on_fill(c, fill("DOWN", 4, 0.60, kind="recovery", order_id="h1"), prm, 1100.0)
    assert c.state is State.RECOVERY_HEDGE_PENDING and c.book.matched_qty == 4          # still in flight
    pl.on_fill(c, fill("DOWN", 6, 0.61, kind="recovery", order_id="h1"), prm, 1101.0)
    pl.on_order_done(c, "h1", True, 1101.0, prm=prm)
    b = c.book
    assert b.down_avg_cost == pytest.approx((4 * 0.60 + 6 * 0.61) / 10)
    assert b.down_fees == pytest.approx(4 * fee_per_share(0.60) + 6 * fee_per_share(0.61))
    assert b.matched_qty == 10 and c.state is State.RECOVERY_HEDGED       # pair cost > 1: hedged, not a profitable lock


# 17 ─────────────────────────────────────────────────────────────────────────────────────────────


def test_17_restart_reconstructs_the_identical_inventory():
    c, _ = recovered(q_fill=6.0, q_req=10.0)
    c.book.add_pending(PendingOrder("h2", "DOWN", 4.0, 0.62, "recovery", 2000.0))    # in flight at the stop
    before = c.book.summary(0.40, 0.59, 0.4)
    back = pl.Cycle.from_json(json.loads(json.dumps(c.to_json())), RECOVER)
    assert back.book.summary(0.40, 0.59, 0.4) == {**before, "pending": []}  # same fills; the in-flight order is NOT stock
    assert back.state is c.state is State.UNMATCHED_DIRECTIONAL
    assert (back.entry_qty, back.recovery_qty, back.hedge_actions) == (c.entry_qty, c.recovery_qty, c.hedge_actions)
    assert back.book.matched_qty == 6 and back.book.unmatched_up == 4
    # a v2 cycle saved under the old state names comes back in the new machine, by its inventory
    old = c.to_json() | {"state": "PAIR_FORMED"}
    assert pl.Cycle.from_json(old, RECOVER).state is State.UNMATCHED_DIRECTIONAL


# 18 ─────────────────────────────────────────────────────────────────────────────────────────────


def test_18_profit_lock_decision():
    prm = pl.PairLockParams()
    c = cycle("UP", 10, 0.80, prm)
    dec, why, rec = manage(c, _feat(0.9), mv(0.88), prm)
    assert dec is not None and dec.kind == "profit_lock"
    i = dec.info
    assert i["expected_after"] < i["expected_before"]                      # locking gives up expected P&L ...
    assert i["downside_reduction"] > 0 and i["utility"] > i["expected_before"]     # ... for a better worst case
    assert [e.name for e in c.events][-2:] == ["PROFIT_LOCK_EVALUATION", "PROFIT_LOCK_ACCEPTED"]
    # "do not accept it solely because it creates a positive pair edge": no risk weight -> holding wins
    c2 = cycle("UP", 10, 0.80, prm)
    dec2, why2, rec2 = manage(c2, _feat(0.9), mv(0.88), replace(prm, profit_lock_risk_aversion=0.0))
    assert dec2 is None and rec2["decision"] == "REJECTED" and c2.events[-1].name == "PROFIT_LOCK_REJECTED"
    # filled: a fully matched, profitable pair -> PAIR_LOCKED, held to settlement
    pl.on_hedge_sent(c, "h1", "DOWN", dec.shares, dec.limit, 1000.0, "profit_lock", i)
    pl.on_fill(c, fill("DOWN", dec.shares, 0.12, kind="profit_lock", order_id="h1", expected=0.12), prm, 1200.0)
    pl.on_order_done(c, "h1", True, 1200.0, prm=prm)
    assert c.state is State.PAIR_LOCKED and c.book.locked_pair_pnl > 0 and c.profit_lock_locked > 0
    # a profit lock is never evaluated while the position is materially losing
    c3 = cycle("UP", 10, 0.80, prm)
    assert manage(c3, _feat(0.5), mv(0.70), prm)[2] is None and c3.state is State.ADVERSE_MOVE


# 19 ─────────────────────────────────────────────────────────────────────────────────────────────


def test_19_recovery_hedge_decision():
    c = cycle("UP", 10, 0.50, RECOVER)
    dec, why, rec = manage(c, _feat(0.4, **AGAINST_UP), mv(0.40), RECOVER)
    assert dec is not None and dec.kind == "recovery"
    names = [e.name for e in c.events]
    assert names.index("ADVERSE_MOVE_DETECTED") < names.index("REVERSAL_CONFIRMED") < \
        names.index("RECOVERY_HEDGE_EVALUATION") < names.index("RECOVERY_HEDGE_ACCEPTED")
    assert [t.to for t in c.transitions][-3:] == ["ADVERSE_MOVE", "REVERSAL_CONFIRMED", "RECOVERY_EVALUATION"]
    # every candidate carries the 13 figures of brief item 7
    want = {"q", "matched", "unmatched", "total_cost", "pair_cost", "incr_pair_cost", "incr_pair_edge", "up_res",
            "down_res", "worst", "expected", "downside_reduction", "capital", "utility"}
    assert all(want <= set(x) for x in rec["candidates"])
    # the chosen size maximises the utility among the acceptable candidates
    acc = [x for x in rec["candidates"] if x["q"] > 0 and not x["rejected"]]
    assert dec.shares == max(acc, key=lambda x: x["utility"])["q"]
    pl.on_hedge_sent(c, "h1", "DOWN", dec.shares, dec.limit, 1000.0, "recovery", dec.info)
    pl.on_fill(c, fill("DOWN", dec.shares, 0.60, kind="recovery", order_id="h1"), RECOVER, 1200.0)
    pl.on_order_done(c, "h1", True, 1200.0, prm=RECOVER)
    assert c.recovery_downside == pytest.approx(c.book.worst_case_pnl - (-(5.0 + 10 * fee_per_share(0.50))))
    assert [t.to for t in c.transitions][-2:] == ["RECOVERY_HEDGE_PENDING", "RECOVERY_HEDGED"]


# 20 ─────────────────────────────────────────────────────────────────────────────────────────────


def test_20_recovery_rejected_without_a_confirmed_reversal():
    c = cycle("UP", 10, 0.50, RECOVER)
    for k in range(20):                                                     # 4 s under water, no reversal
        dec, why, rec = manage(c, _feat(0.4), mv(0.40), RECOVER, now=1000.0 + 200 * k)
        assert dec is None and rec is None
    assert c.state is State.ADVERSE_MOVE
    assert not any(e.name.startswith("RECOVERY_HEDGE") for e in c.events)
    assert pl.watch_state(c, RECOVER) is State.ADVERSE_MOVE
