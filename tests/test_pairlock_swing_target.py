"""TARGET-PROFIT RECOVERY SIZING (the user, 2026-10-08): swing 1 and swing 2 buy the SMALLEST INTEGER number of shares x
of the new leader with

    profit if it wins = held + x - (every cost so far + the walked-ask cost of x, fees in)  >  TARGET_RECOVERY_PROFIT

priced on the live ask book (quantity and VWAP found together), from the WHOLE current inventory (swing 2 from what
swing 1 left), refused -- nothing bought -- when the depth, MAX_RECOVERY_SHARES, MAX_RECOVERY_PRICE or the capital cap
cannot take the whole size. Every expected size below is found by an independent brute-force search."""
from __future__ import annotations

from dataclasses import replace

import pytest
from test_pairlock_swing import A_LEADS_ENTRY, Run, view

from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy import pairlock_swing as sw
from troll_poly_bot.strategy.pairlock_book import fee_per_share

TPRM = rc.RecoveryParams(swing_mode=True, swing_sizing="target", swing_target_profit=3.0, swing_max_recovery_shares=150.0,
                         swing_max_recovery_price=0.85, max_total_position=1000.0, max_slippage=0.03, min_time_left_s=15.0,
                         cooldown_ms=2000.0, depth_haircut=0.0, min_size=5.0, swing_max_market_capital=1000.0)
B_AT_074 = view(0.25, 0.26, 0.73, 0.74)
A_AT_074 = view(0.73, 0.74, 0.25, 0.26)


def walk_cost(levels, x):
    """The cost (fees in) of x shares walked through ``levels`` = [(price, size)], or None past the depth."""
    left, cost = x, 0.0
    for p, s in sorted(levels):
        take = min(left, s)
        cost += take * (p + fee_per_share(p))
        left -= take
        if left <= 1e-12:
            return cost
    return None


def brute(have, total_cost, levels, target, min_size=5, limit=2000):
    for x in range(1, limit):
        c = walk_cost(levels, x)
        if c is None:
            return None
        if x >= min_size and have + x - (total_cost + c) > target + 1e-9:
            return x
    return None


def test_the_users_example_10_a_for_6_and_b_at_074():
    r = sw.target_quantity(0.0, 6.0, 0.0, [(0.74, 1000.0)], 3.0, fee_per_share, 0.85, 150, 5)
    assert r["ok"] and r["x"] == 37 == brute(0.0, 6.0, [(0.74, 1000.0)], 3.0)
    assert r["profit_after"] > 3.0 and 36 - (6.0 + 36 * (0.74 + fee_per_share(0.74))) <= 3.0     # 36 is not enough
    assert r["vwap"] == pytest.approx(0.74) and r["fee_ps"] == pytest.approx(fee_per_share(0.74))
    assert sw.target_quantity(0.0, 6.0, 0.0, [(0.74, 1000.0)], 3.0, lambda p: 0.0, 0.85, 150, 5)["x"] == 35   # fees matter


@pytest.mark.parametrize("target", [1.0, 2.0, 3.0, 4.0, 5.0])
def test_every_target_gets_the_smallest_size_on_a_walked_book(target):
    book = [(0.72, 12.0), (0.73, 15.0), (0.75, 20.0), (0.78, 400.0)]
    r = sw.target_quantity(10.0, 30.5, 0.0, book, target, fee_per_share, 0.85, 1000, 5)
    want = brute(10.0, 30.5, book, target)
    assert r["ok"] and r["x"] == want
    assert 10 + want - (30.5 + walk_cost(book, want)) > target                     # reaches the target ...
    assert 10 + want - 1 - (30.5 + walk_cost(book, want - 1)) <= target           # ... and one share fewer does not
    assert r["cost"] == pytest.approx(walk_cost(book, want))                       # quantity and VWAP from the same walk
    left, deepest = want, None
    for p, sz in sorted(book):
        if left <= 1e-12:
            break
        deepest, left = p, left - min(left, sz)
    assert r["deepest"] == deepest                                                 # the deepest level the size reaches


def test_larger_targets_need_more_shares():
    book = [(0.70, 30.0), (0.71, 30.0), (0.73, 300.0)]
    xs = [sw.target_quantity(0.0, 6.0, 0.0, book, t, fee_per_share, 0.85, 1000, 5)["x"] for t in (1, 2, 3, 4, 5)]
    assert xs == sorted(xs) and len(set(xs)) == 5


def test_the_spread_is_not_counted_twice():
    """The asks already contain the spread: a wider bid-ask spread (same asks) changes nothing in the size or its cost;
    the spread is reported on its own."""
    tight, wide = view(0.25, 0.26, 0.73, 0.74), view(0.25, 0.26, 0.60, 0.74)
    out = []
    for mv in (tight, wide):
        r = Run(TPRM, fees=fee_per_share)
        r.enter()
        dec, rec = r.step(mv)
        out.append((dec.shares, dec.vwap, rec["order_cost"], rec["spread"]))
    assert out[0][:3] == out[1][:3] and out[0][3] == pytest.approx(0.01) and out[1][3] == pytest.approx(0.14)


# ───────────────────────────── through the swing state machine ─────────────────────────────

def test_swing_1_then_swing_2_from_the_actual_inventory():
    r = Run(TPRM, fees=fee_per_share)
    r.enter()                                                                       # 10 A (UP) at 0.60, fee in
    c0 = r.book.total_cost
    dec, rec = r.step(B_AT_074)
    x1 = brute(0.0, c0, [(0.74, 1000.0)], 3.0)
    assert dec.kind == "recovery" and dec.side == "DOWN" and dec.shares == x1, rec["why"]
    assert x1 == 38                     # the entry's own fee makes the cost 6.17, not 6.00: 37 would pay only +2.97
    assert rec["profit_if_side_wins"] == pytest.approx(-c0) and "target sizing" in rec["why"]
    r.fill(dec)
    assert r.book.resolution_pnl("DOWN") > 3.0
    dec, rec = r.step(B_AT_074)
    assert dec is None and "recovery done" in rec["why"]                            # the target is met: nothing more
    c1 = r.book.total_cost
    dec, rec = r.step(A_AT_074)                                                    # swing 2: from 10 A / 37 B and cost c1
    x2 = brute(10.0, c1, [(0.74, 1000.0)], 3.0)
    assert dec.side == "UP" and dec.shares == x2 and x2 != 80, rec["why"]          # 10 + x - (34.80 + 0.7535 x) > 3
    r.fill(dec)
    assert r.book.resolution_pnl("UP") > 3.0 and r.book.qty("UP") == 10 + x2 and r.book.qty("DOWN") == x1


def test_a_partial_fill_is_followed_by_the_rest_of_the_target():
    r = Run(TPRM, fees=fee_per_share)
    r.enter()
    dec, _ = r.step(B_AT_074)
    r.fill(dec, qty=20.0)                                                           # fill-and-kill: 20 of 37
    dec, rec = r.step(B_AT_074)
    assert dec is not None and dec.shares == brute(20.0, r.book.total_cost, [(0.74, 1000.0)], 3.0)
    r.fill(dec)
    assert r.book.resolution_pnl("DOWN") > 3.0


@pytest.mark.parametrize("why,prm,mv,needle", [
    ("depth", TPRM, view(0.25, 0.26, 0.73, 0.74, asks_down=[(0.74, 20.0)]), "insufficient depth"),
    ("max shares", replace(TPRM, swing_max_recovery_shares=30.0), B_AT_074, "MAX_RECOVERY_SHARES"),
    ("max price", replace(TPRM, swing_max_recovery_price=0.74), view(0.25, 0.26, 0.73, 0.74, asks_down=[(0.74, 20.0), (0.80, 500.0)]),
     "MAX_RECOVERY_PRICE"),
    ("capital", replace(TPRM, swing_max_market_capital=30.0), B_AT_074, "MAX_MARKET_CAPITAL"),
    ("position", replace(TPRM, max_total_position=40.0), B_AT_074, "MAX_TOTAL_POSITION"),
])
def test_a_size_that_does_not_fit_whole_is_refused(why, prm, mv, needle):
    r = Run(prm, fees=fee_per_share)
    r.enter()
    dec, rec = r.step(mv)
    assert dec is None and rec["decision"] == "REJECTED" and needle in rec["why"], (why, rec["why"])
    assert r.book.qty("DOWN") == 0.0                                                # nothing bought, not even part


def test_the_order_never_risks_more_than_the_capital_cap():
    for cap in (40.0, 60.0, 125.0):
        r = Run(replace(TPRM, swing_max_market_capital=cap), fees=fee_per_share)
        r.enter()
        dec, rec = r.step(view(0.25, 0.26, 0.73, 0.74, asks_down=[(0.74, 30.0), (0.75, 30.0), (0.77, 500.0)]))
        if dec is not None:
            assert r.book.cash_spent + dec.shares * (dec.limit + fee_per_share(dec.limit)) <= cap + 1e-9
            assert dec.limit >= rec["target_sizing"]["deepest"]                     # the limit reaches the levels it needs


def test_the_hedge_and_later_swings_are_unchanged():
    r = Run(TPRM, fees=fee_per_share)
    r.enter()
    for mv in (B_AT_074, A_AT_074):
        dec, _ = r.step(mv)
        r.fill(dec)
    up, dn = r.book.qty("UP"), r.book.qty("DOWN")
    dec, rec = r.step(B_AT_074)                                                    # swing 3: hedge to UP = DOWN, as before
    assert r.st.phase is sw.Phase.HEDGE_MODE and dec.kind == "hedge" and dec.shares == pytest.approx(up - dn)


def test_a_market_started_under_the_fixed_sizes_keeps_them():
    """A state saved before 2026-10-08 (no sizing in its plan) is restored with the fixed sizes it was started with."""
    st = sw.SwingState.start("UP", 0.0, replace(TPRM, swing_sizing="fixed"))
    d = st.to_json()
    d["plan"].pop("sizing")
    old = sw.SwingState.from_json(d)
    old.phase = sw.Phase.SWING_0
    row = sw.register_swing(old, "DOWN", 1.0, 0.70, {"UP": 0.3, "DOWN": 0.7})
    assert row.get("sizing") is None and row["target"] == 24.0


def test_the_projection_and_the_worst_path_use_the_target_sizing():
    path = sw.plan_path(TPRM)
    assert [r["step"] for r in path] == ["swing 0", "swing 1", "swing 2", "hedge"]
    s1, s2 = path[1]["shares"], path[2]["shares"]
    c0 = 10 * (0.60 + fee_per_share(0.60))
    assert s1 == brute(0.0, c0, [(0.70, 1e6)], 3.0)                                # at the reference price 0.70
    assert s2 == brute(10.0, c0 + s1 * (0.70 + fee_per_share(0.70)), [(0.70, 1e6)], 3.0)
    r = Run(TPRM, fees=fee_per_share)
    r.enter()
    acc = sw.accounting(r.st, r.book, TPRM)
    assert acc["maximum_additional_capital_required"] == pytest.approx(path[-1]["cost"] - path[0]["cost"], rel=1e-6)


def test_the_engine_sends_the_target_size(monkeypatch):
    from test_pairlock_engine_v2 import _fill
    from test_pairlock_swing import _at, _swing_engine
    from test_spot_lag import SLUG, T0
    e, tw = _swing_engine(swing_sizing="target", swing_target_profit=3.0, swing_max_market_capital=200.0)
    assert e.rprm.swing_sizing == "target"
    ev = _at(e, tw, T0 + 21_000, (0.59, 0.60), (0.40, 0.41))
    o = e.to_order(ev, T0 + 21_000)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.60, 10.0, T0 + 21_300)
    cost = e.cycles[SLUG].book.total_cost
    ev = _at(e, tw, T0 + 60_000, (0.25, 0.26), (0.73, 0.74))
    want = brute(0.0, cost, [(0.74, 1000.0), (0.75, 1000.0)], 3.0)
    assert ev.tradeable and ev.side == "DOWN" and ev.size == want, (ev.reason, ev.detail)
