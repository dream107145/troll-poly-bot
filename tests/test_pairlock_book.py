"""Pair-lock accounting (strategy/pairlock_book.py): the brief's ten deterministic tests, the invariants, the
validated state transitions (2026-10-02 recovery / profit-lock machine), executable VWAP and the hedge candidates'
no-martingale property."""
from __future__ import annotations

import itertools
import json
import random

import pytest

from troll_poly_bot.strategy import pairlock_book as pb
from troll_poly_bot.strategy.pairlock_book import FillRecord, PairBook, PendingOrder, State, fee_per_share

M = "btc-updown-5m-1"
_ids = itertools.count(1)


def buy(book, side, qty, price, fee=None, expected=None, order_id=None, kind="entry"):
    n = next(_ids)
    f = FillRecord(market_id=M, ts=float(n), token=side.lower(), side=side, qty=qty, price=price,
                   fee=fee_per_share(price) * qty if fee is None else fee, order_id=order_id or f"o{n}",
                   fill_id=f"f{n}", expected_price=expected, kind=kind)
    book.add_fill(f)
    return f


# ─────────────────────────── the brief's tests 1-10 ───────────────────────────


def test_1_profitable_matched_pair():
    b = PairBook(M)
    buy(b, "UP", 10, 0.40, fee=0.0)
    buy(b, "DOWN", 10, 0.50, fee=0.0)
    assert b.matched_qty == 10
    assert b.matched_pair_cost == pytest.approx(9.00)
    assert b.settlement_value("UP") == b.settlement_value("DOWN") == 10.0
    assert b.locked_pair_pnl == pytest.approx(1.00)
    assert b.pair_edge == pytest.approx(0.10)
    ok, why = pb.pair_lock_check(b, min_pair_edge=0.02, min_locked_profit=0.10)
    assert ok, why
    # with the real taker fees the lock is smaller but still positive
    b2 = PairBook(M)
    buy(b2, "UP", 10, 0.40)
    buy(b2, "DOWN", 10, 0.50)
    assert b2.matched_pair_cost == pytest.approx(9.00 + 10 * (fee_per_share(0.40) + fee_per_share(0.50)))
    assert b2.locked_pair_pnl == pytest.approx(1.00 - 10 * (0.0168 + 0.0175))


def test_2_negative_pair_is_never_locked():
    b = PairBook(M)
    buy(b, "UP", 10, 0.60, fee=0.0)
    buy(b, "DOWN", 10, 0.45, fee=0.0)
    assert b.matched_pair_cost == pytest.approx(10.50)
    assert b.locked_pair_pnl == pytest.approx(-0.50)
    ok, why = pb.pair_lock_check(b, 0.0, 0.0)
    assert not ok and "net pair edge" in why


def test_3_unequal_inventory():
    b = PairBook(M)
    buy(b, "UP", 10, 0.40)
    buy(b, "DOWN", 6, 0.50)
    assert (b.matched_qty, b.unmatched_up, b.unmatched_down) == (6, 4, 0)
    assert b.heavy_side == "UP"
    ok, why = pb.pair_lock_check(b, -1.0, -100.0)
    assert not ok and "not fully matched" in why                 # 10 UP / 6 DOWN is NOT a locked position
    # the locked P&L covers the 6 pairs only
    assert b.locked_pair_pnl == pytest.approx(6 * (1 - 0.40 - 0.50) - 6 * fee_per_share(0.40) - 6 * fee_per_share(0.50))


def test_4_partial_fill_adds_only_what_filled():
    b = PairBook(M)
    buy(b, "UP", 10, 0.40)
    b.add_pending(PendingOrder("h1", "DOWN", 10.0, 0.52, "hedge"))
    assert b.down_qty == 0 and b.matched_qty == 0                  # requested, not filled
    buy(b, "DOWN", 3, 0.50, order_id="h1", kind="hedge")
    assert b.down_qty == 3 and b.matched_qty == 3 and b.unmatched_up == 7
    assert not pb.pair_lock_check(b, -1.0, -100.0)[0]
    b.resolve_pending("h1")
    assert b.down_qty == 3


def test_5_fees_destroy_a_positive_gross_edge():
    b = PairBook(M)
    buy(b, "UP", 10, 0.49)
    buy(b, "DOWN", 10, 0.50)
    assert b.pair_edge == pytest.approx(0.01)                       # positive before costs
    assert b.net_pair_edge == pytest.approx(0.01 - fee_per_share(0.49) - fee_per_share(0.50))
    assert b.net_pair_edge < 0 and b.locked_pair_pnl < 0
    ok, why = pb.pair_lock_check(b, 0.0, 0.0)
    assert not ok


def test_6_multi_level_vwap_not_the_best_ask():
    levels = [(0.48, 3.0), (0.49, 3.0), (0.50, 4.0)]
    vw, fps, used = pb.vwap_buy(levels, 10.0)
    assert vw == pytest.approx((3 * 0.48 + 3 * 0.49 + 4 * 0.50) / 10)
    assert used == [(0.48, 3.0), (0.49, 3.0), (0.50, 4.0)]
    assert fps == pytest.approx((3 * fee_per_share(0.48) + 3 * fee_per_share(0.49) + 4 * fee_per_share(0.50)) / 10)
    assert pb.vwap_buy(levels, 11.0)[0] is None                   # more than the book shows: no fill assumed
    # half of each level taken by faster traders: 1.5 + 1.5 + 2 = 5 shares left
    assert pb.vwap_buy(levels, 5.0, haircut=0.5)[0] == pytest.approx((1.5 * 0.48 + 1.5 * 0.49 + 2 * 0.50) / 5)
    assert pb.vwap_buy(levels, 6.0, haircut=0.5)[0] is None


def test_7_a_pending_order_is_not_inventory():
    b = PairBook(M)
    b.add_pending(PendingOrder("e1", "UP", 10.0, 0.45, "entry"))
    assert b.up_qty == 0 and b.total_cost == 0 and b.matched_qty == 0 and b.cash_spent == 0
    assert b.summary()["pending"][0]["qty"] == 10.0


def test_8_weighted_average_over_fills():
    b = PairBook(M)
    buy(b, "UP", 5, 0.40, fee=0.0)
    buy(b, "UP", 5, 0.50, fee=0.0)
    buy(b, "UP", 10, 0.60, fee=0.0)
    assert b.up_qty == 20
    assert b.up_avg_cost == pytest.approx((2.0 + 2.5 + 6.0) / 20)
    assert b.up_gross_cost == pytest.approx(10.5)


def test_9_settlement_both_outcomes():
    b = PairBook(M)
    buy(b, "UP", 10, 0.40)
    buy(b, "DOWN", 6, 0.50)
    tc = b.total_cost
    assert b.up_resolution_pnl == pytest.approx(10 - tc)
    assert b.down_resolution_pnl == pytest.approx(6 - tc)
    assert b.worst_case_pnl == pytest.approx(min(10, 6) - tc)


def test_10_pnl_reconciles_with_cash():
    b = PairBook(M)
    buy(b, "UP", 10, 0.40)
    buy(b, "DOWN", 4, 0.55)
    # an exit: sell 2 UP at 0.70 (taker fee) -- the realised part must reconcile too
    b.add_fill(FillRecord(M, 99.0, "up", "UP", 2.0, 0.70, fee_per_share(0.70) * 2, "x1", "fx1", "SELL", 0.71, "exit"))
    for winner in ("UP", "DOWN"):
        assert b.resolution_pnl(winner) == pytest.approx(b.final_pnl_from_cash(winner), abs=1e-9)
    assert b.up_qty == 8 and b.down_qty == 4


# ─────────────────────────── identities and invariants ───────────────────────────


def test_worst_case_identity_on_random_books():
    rng = random.Random(5)
    for _ in range(300):
        b = PairBook(M)
        for _k in range(rng.randint(1, 6)):
            buy(b, rng.choice(("UP", "DOWN")), rng.choice((5.0, 7.5, 10.0)), round(rng.uniform(0.02, 0.98), 2))
        assert b.worst_case_pnl == pytest.approx(min(b.up_qty, b.down_qty) - b.total_cost, abs=1e-9)
        assert b.worst_case_pnl == pytest.approx(min(b.up_resolution_pnl, b.down_resolution_pnl), abs=1e-9)
        for w in ("UP", "DOWN"):
            assert b.resolution_pnl(w) == pytest.approx(b.final_pnl_from_cash(w), abs=1e-9)
        if b.matched_qty > 0:
            assert b.locked_pair_pnl == pytest.approx(b.matched_qty * b.net_pair_edge, abs=1e-9)
        b.check_invariants()


def test_mark_to_market_is_separate_from_settlement():
    b = PairBook(M)
    buy(b, "UP", 10, 0.80)
    buy(b, "DOWN", 10, 0.30)                        # pair cost 1.10 + fees: settlement locks a LOSS
    assert b.locked_pair_pnl < 0 and b.worst_case_pnl < 0
    mtm = b.mark_to_market_pnl(up_bid=0.95, down_bid=0.30)          # an executable-bid view can be positive
    assert mtm == pytest.approx(10 * 0.95 + 10 * 0.30 - b.total_cost)
    assert mtm > 0 > b.worst_case_pnl


def test_a_duplicate_fill_is_counted_once_and_bad_fills_are_refused():
    b = PairBook(M)
    f = buy(b, "UP", 10, 0.40)
    b.add_fill(f)
    assert b.up_qty == 10
    with pytest.raises(pb.InvariantError):
        b.add_fill(FillRecord(M, 1.0, "down", "DOWN", 5.0, 0.4, 0.0, "s", "s1", "SELL"))   # selling what is not held
    with pytest.raises(pb.InvariantError):
        b.add_fill(FillRecord(M, 1.0, "up", "UP", 0.0, 0.4, 0.0, "z", "z1"))


def test_slippage_is_reported_but_not_charged_twice():
    b = PairBook(M)
    buy(b, "UP", 10, 0.42, expected=0.40)
    assert b.up_slippage == pytest.approx(0.20)                      # 2c worse than expected, 10 shares
    assert b.total_cost == pytest.approx(10 * 0.42 + 10 * fee_per_share(0.42))   # the price paid already holds it
    assert b.final_pnl_from_cash("UP") == pytest.approx(b.up_resolution_pnl)


def test_restart_rebuilds_the_identical_book():
    b = PairBook(M)
    buy(b, "UP", 10, 0.40, expected=0.39)
    buy(b, "DOWN", 3, 0.55, kind="hedge")
    b.add_pending(PendingOrder("h9", "DOWN", 7.0, 0.58, "hedge", 5.0))
    b2 = PairBook.from_json(json.loads(json.dumps(b.to_json())))
    assert b2.summary(0.45, 0.50, 0.6) == b.summary(0.45, 0.50, 0.6)
    assert b2.pending.keys() == b.pending.keys()


# ─────────────────────────── states ───────────────────────────


def test_only_listed_transitions_are_allowed():
    pb.check_transition(State.WAITING, State.INITIAL_ENTRY)
    pb.check_transition(State.RECOVERY_HEDGED, State.PAIR_LOCKED)
    pb.check_transition(State.PAIR_LOCKED, State.SETTLEMENT_PENDING)
    for bad in ((State.WAITING, State.PAIR_LOCKED), (State.DIRECTIONAL_POSITION, State.PAIR_LOCKED),
                (State.PAIR_LOCKED, State.DIRECTIONAL_POSITION), (State.SETTLED, State.DIRECTIONAL_POSITION),
                (State.ADVERSE_MOVE, State.RECOVERY_EVALUATION),            # a recovery needs REVERSAL_CONFIRMED
                (State.DIRECTIONAL_POSITION, State.RECOVERY_EVALUATION),
                (State.ADVERSE_MOVE, State.PROFIT_LOCK_EVALUATION),         # no profit lock while losing
                (State.SETTLEMENT_PENDING, State.PAIR_LOCKED), (State.SETTLEMENT_PENDING, State.SETTLED)):
        with pytest.raises(pb.InvalidTransition):
            pb.check_transition(*bad)
    # every state can reach SETTLED (no dead end) and nothing leaves SETTLED
    for st in State:
        seen, todo = set(), [st]
        while todo:
            x = todo.pop()
            seen.add(x)
            todo.extend(y for y in pb.TRANSITIONS[x] if y not in seen)
        assert State.SETTLED in seen
    assert pb.TRANSITIONS[State.SETTLED] == frozenset()


def test_no_state_holding_inventory_can_lead_back_to_a_new_entry():
    holding = set(State) - {State.WAITING, State.INITIAL_ENTRY}
    for st in holding:
        seen, todo = set(), [st]
        while todo:
            x = todo.pop()
            seen.add(x)
            todo.extend(y for y in pb.TRANSITIONS[x] if y not in seen and y is not State.WAITING)
        assert State.INITIAL_ENTRY not in seen, st
        assert State.WAITING not in pb.TRANSITIONS[st] or st is State.INITIAL_ENTRY


# ─────────────────────────── hedge candidates ───────────────────────────


def _book_10_up(price=0.60):
    b = PairBook(M)
    buy(b, "UP", 10, price)
    return b


def test_candidate_sizes_and_figures_never_depend_on_the_loss():
    levels = [(0.48, 3.0), (0.49, 3.0), (0.50, 40.0)]
    small, large = _book_10_up(0.55), _book_10_up(0.90)            # the same 10 UP, a small vs a large loss
    for p_up in (0.3, 0.5, 0.7):
        a = pb.evaluate_hedges(small, p_up, levels)
        b = pb.evaluate_hedges(large, p_up, levels)
        assert [c.q for c in a] == [c.q for c in b] == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
        for ca, cb in zip(a, b):
            # the change each hedge makes is the same; the sunk cost of the first leg only shifts both levels
            assert ca.new_expected_pnl - ca.expected_pnl_before == pytest.approx(cb.new_expected_pnl - cb.expected_pnl_before)
            assert ca.downside_reduction == pytest.approx(cb.downside_reduction)
        for c in a:
            assert c.new_worst_case_pnl == pytest.approx(min(c.new_up_qty, c.new_down_qty) - c.new_total_cost)
            if c.q > 0:
                assert c.new_expected_pnl - c.expected_pnl_before == pytest.approx(
                    c.q * ((1 - p_up) - c.vwap - c.fee_per_share))
                assert c.downside_reduction == pytest.approx(c.q * (1.0 - c.vwap - c.fee_per_share))   # q <= unmatched


def test_the_old_ev_maximising_picker_is_gone():
    assert not hasattr(pb, "best_hedge")                           # MIN_HEDGE_EV no longer decides anything


def test_candidates_respect_depth_capital_and_the_venue_minimum():
    b = _book_10_up(0.60)
    cands = pb.evaluate_hedges(b, 0.3, [(0.40, 6.0)], fractions=(0.0, 0.1, 0.5, 1.0),
                               max_market_capital=b.total_cost + 2.0, capital_used=b.total_cost)
    by_q = {c.q: c for c in cands}
    assert not by_q[1.0].feasible and by_q[1.0].why == "under the venue minimum"
    assert not by_q[10.0].feasible and by_q[10.0].why == "not enough executable depth"
    assert not by_q[5.0].feasible and by_q[5.0].why == "market capital"     # 5 x 0.40 + fee > 2.00
    assert by_q[0.0].label == "NO_HEDGE" and by_q[0.0].downside_reduction == 0.0


def test_the_slippage_allowance_is_priced_into_every_candidate():
    b = _book_10_up(0.80)
    c0 = pb.evaluate_hedges(b, 0.2, [(0.13, 50.0)], fractions=(1.0,))[0]
    c1 = pb.evaluate_hedges(b, 0.2, [(0.13, 50.0)], fractions=(1.0,), slippage_allowance=0.02)[0]
    assert c1.price == pytest.approx(0.15) and c0.price == pytest.approx(0.13)
    assert c1.incr_pair_cost == pytest.approx(c0.incr_pair_cost + 0.02)
    assert c1.new_total_cost == pytest.approx(c0.new_total_cost + 10 * 0.02)
