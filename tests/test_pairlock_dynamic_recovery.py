"""The dynamic loss-recovery engine (strategy/pairlock_recovery.py): the spec's twelve deterministic cases. Every test
computes the EXPECTED figure from the spec's formula by hand (written out) and compares it with what the engine did;
run this file directly (python tests/test_pairlock_dynamic_recovery.py) to print the expected-vs-actual table."""
from __future__ import annotations

import math

import pytest

from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy.pairlock import MarketView
from troll_poly_bot.strategy.pairlock_book import FillRecord, PairBook, fee_per_share, vwap_buy

M = "btc-updown-5m-1"
#: every reversal signal toward DOWN for a UP position (the test's features; negate them for "toward UP")
TOWARD_DOWN = dict(tv1=-5.0, tv3=-5.0, tacc=-3.0, mom1=-3.0, mom3=-3.0, imb=-0.5, pmflow=-0.5, agree=-1.0)
TOWARD_UP = {k: -v for k, v in TOWARD_DOWN.items()}
TABLE: list[tuple[str, str, float, float]] = []


class _Model:
    rev_scales = {k: 1.0 for k in ("tv1", "tv3", "tacc", "mom1", "mom3", "imb", "pmflow", "agree")}


def feat(**kw):
    f = {k: 0.0 for k in ("logit_mid", "z_fv", "mom1", "mom3", "mom10", "tv1", "tv3", "tacc", "imb", "pmflow", "agree")}
    f.update(kw)
    return f


def mv(up_bid, up_ask, down_bid, down_ask, asks_up=None, asks_down=None, depth=1000.0):
    return MarketView(bid_up=up_bid, ask_up=up_ask, bid_down=down_bid, ask_down=down_ask,
                      asks_up=asks_up or [(up_ask, depth)], asks_down=asks_down or [(down_ask, depth)],
                      bids_up=[(up_bid, depth)], bids_down=[(down_bid, depth)])


def fill(side, qty, price, kind="entry", oid="o"):
    return FillRecord(M, 0.0, side.lower(), side, qty, price, fee_per_share(price) * qty, oid, f"{oid}-{side}-{qty}",
                      "BUY", price, kind)


def held(*fills_, target=0.50):
    c = rc.RecoveryCycle(book=PairBook(M), a_side=fills_[0].side, state=RState_HOLDING(), target=target)
    for f in fills_:
        c.book.add_fill(f)
        if f.kind == "entry":
            c.entry_qty += f.qty
    return c


def RState_HOLDING():
    return rc.RState.HOLDING


def show(case: str, what: str, expected: float, actual: float, tol: float = 1e-6):
    TABLE.append((case, what, expected, actual))
    assert actual == pytest.approx(expected, abs=tol), f"{case}: {what}: expected {expected}, got {actual}"


def expected_required(branch_pnl: float, target: float, v: float, slip: float = 0.005) -> float:
    """The spec: x = (R - P&L_losing_branch) / (1 - v - fee(v) - slippage), to the cent of a share."""
    return math.ceil((target - branch_pnl) / (1.0 - v - fee_per_share(v) - slip) * 100.0) / 100.0


def evaluate(c, p_up, f, view, prm=None, now=10_000.0, left=200.0, bankroll=100.0):
    prm = prm or rc.RecoveryParams()
    return rc.evaluate_recovery(c, p_up, f, view, now, left, _Model(), prm, bankroll)


# 1 ───────────────────────────────────────────────────────────────────────────────────────────────


def test_01_up_losing_buy_down():
    c = held(fill("UP", 10, 0.60))                           # cost 6 + 10 x fee(0.60) 0.0168 = 6.168
    C = 10 * 0.60 + 10 * fee_per_share(0.60)
    show("1", "UP cost incl. fees", C, c.book.total_cost)
    show("1", "P&L if DOWN wins, before", -C, rc.outcomes(c.book)["pnl_down_win"])
    dec, rec = evaluate(c, p_up=0.30, f=feat(**TOWARD_DOWN), view=mv(0.40, 0.41, 0.59, 0.60))
    assert dec is not None, rec["why"]
    x = expected_required(-C, 0.50, 0.60)                    # (0.50 + 6.168) / (1 - 0.60 - 0.0168 - 0.005) = 17.64
    show("1", "required DOWN shares", x, dec.plan_qty)
    assert dec.side == "DOWN" and dec.shares == x
    cost = x * (0.605 + fee_per_share(0.60))
    show("1", "P&L if DOWN wins, after", x - C - cost, rec["after_pnl_down_win"])
    show("1", "P&L if UP wins, after", 10 - C - cost, rec["after_pnl_up_win"])
    assert rec["after_pnl_down_win"] >= 0.50 - 1e-9 and rec["after_pnl_up_win"] < -C   # the other branch got worse
    show("1", "E[P&L] improvement = x (p_DOWN - v - fee - slip)", x * (0.70 - 0.60 - fee_per_share(0.60) - 0.005),
         rec["expected_improvement"])
    assert rec["improves_min"] is False and rec["decision"] == "BUY"


# 2 ───────────────────────────────────────────────────────────────────────────────────────────────


def test_02_down_losing_buy_up():
    c = held(fill("DOWN", 10, 0.60))
    C = c.book.total_cost
    dec, rec = evaluate(c, p_up=0.70, f=feat(**TOWARD_UP), view=mv(0.59, 0.60, 0.40, 0.41))
    assert dec is not None and dec.side == "UP", rec["why"]
    x = expected_required(-C, 0.50, 0.60)
    show("2", "required UP shares", x, dec.plan_qty)
    cost = x * (0.605 + fee_per_share(0.60))
    show("2", "P&L if UP wins, after", x - C - cost, rec["after_pnl_up_win"])
    show("2", "P&L if DOWN wins, after", 10 - C - cost, rec["after_pnl_down_win"])
    assert rec["losing_side_exposure"] == pytest.approx(C)        # the DOWN cost is what is at risk


# 3 ───────────────────────────────────────────────────────────────────────────────────────────────


def _after_down_hedge():
    """Test 1's position after its hedge filled: 10 UP @ 0.60 + 17.64 DOWN @ 0.605."""
    x = expected_required(-(10 * 0.60 + 10 * fee_per_share(0.60)), 0.50, 0.60)
    return held(fill("UP", 10, 0.60), fill("DOWN", x, 0.605, kind="recovery", oid="h1"))


def test_03_up_recovers_after_the_down_hedge_so_the_bot_buys_up_again():
    prm = rc.RecoveryParams(max_capital=40.0)
    c = _after_down_hedge()
    o = rc.outcomes(c.book)
    assert o["pnl_down_win"] > 0.50 - 1e-6 > o["pnl_up_win"]       # DOWN-heavy now: the UP-win branch is the losing one
    # UP rallies: UP bid 0.70 / ask 0.71; DOWN bid 0.29 (its cost 0.605: 0.315 against it); signals toward UP
    dec, rec = evaluate(c, p_up=0.80, f=feat(**TOWARD_UP), view=mv(0.70, 0.71, 0.29, 0.30), prm=prm)
    assert dec is not None and dec.side == "UP", rec["why"]
    x = expected_required(o["pnl_up_win"], 0.50, 0.71)
    show("3", "required UP shares", x, dec.plan_qty)
    cost = x * (0.715 + fee_per_share(0.71))
    show("3", "P&L if UP wins, after", c.book.up_qty + x - c.book.total_cost - cost, rec["after_pnl_up_win"])
    show("3", "P&L if DOWN wins, after", c.book.down_qty - c.book.total_cost - cost, rec["after_pnl_down_win"])
    assert rec["after_pnl_up_win"] >= 0.50 - 1e-9                 # UP-heavy -> DOWN-heavy -> UP-heavy again
    # the same moment with the DOWN inventory NOT yet losing (bid 0.57, 3.5c under its 0.605 cost): no recovery
    dec2, rec2 = evaluate(c, p_up=0.80, f=feat(**TOWARD_UP), view=mv(0.42, 0.43, 0.57, 0.58), prm=prm)
    assert dec2 is None and "MIN_PRICE_MOVE" in rec2["why"]


# 4 ───────────────────────────────────────────────────────────────────────────────────────────────


def test_04_down_recovers_after_the_up_hedge():
    prm = rc.RecoveryParams(max_capital=40.0)
    x1 = expected_required(-(10 * 0.60 + 10 * fee_per_share(0.60)), 0.50, 0.60)
    c = held(fill("DOWN", 10, 0.60), fill("UP", x1, 0.605, kind="recovery", oid="h1"))
    o = rc.outcomes(c.book)
    dec, rec = evaluate(c, p_up=0.20, f=feat(**TOWARD_DOWN), view=mv(0.29, 0.30, 0.70, 0.71), prm=prm)
    assert dec is not None and dec.side == "DOWN", rec["why"]
    x = expected_required(o["pnl_down_win"], 0.50, 0.71)
    show("4", "required DOWN shares", x, dec.plan_qty)
    cost = x * (0.715 + fee_per_share(0.71))
    show("4", "P&L if DOWN wins, after", c.book.down_qty + x - c.book.total_cost - cost, rec["after_pnl_down_win"])
    assert rec["after_pnl_down_win"] >= 0.50 - 1e-9


# 5 ───────────────────────────────────────────────────────────────────────────────────────────────


def test_05_recovery_target_already_satisfied():
    c = held(fill("UP", 10, 0.40), fill("DOWN", 10, 0.45, kind="recovery", oid="h1"))   # pair cost 0.85 + fees
    o = rc.outcomes(c.book)
    both = 10 - c.book.total_cost
    show("5", "P&L either way", both, o["min_settlement_pnl"])
    assert both > 0.50
    dec, rec = evaluate(c, p_up=0.5, f=feat(**TOWARD_DOWN), view=mv(0.40, 0.41, 0.59, 0.60))
    assert dec is None and rec["decision"] == "NONE" and "already satisfied" in rec["why"]
    # one branch at target, the other not: plan_recovery for the satisfied branch reports 0 to buy
    c2 = held(fill("UP", 10, 0.60))
    plan = rc.plan_recovery(c2.book, "UP", [(0.41, 1000.0)], rc.RecoveryParams())
    assert plan["q_required"] == 0.0 and plan["why"] == "target already satisfied"


# 6 ───────────────────────────────────────────────────────────────────────────────────────────────


def test_06_insufficient_bankroll():
    c = held(fill("UP", 10, 0.60))
    x = expected_required(-c.book.total_cost, 0.50, 0.60)
    need = x * (0.605 + fee_per_share(0.60))
    show("6", "capital the plan needs", need, evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60),
                                                      bankroll=5.0)[1]["recovery_cost_required"])
    dec, rec = evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60), bankroll=5.0)
    assert dec is None and "free bankroll" in rec["why"]
    assert evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60), bankroll=need + 0.01)[0] is not None


# 7 ───────────────────────────────────────────────────────────────────────────────────────────────


def test_07_insufficient_order_book_depth():
    c = held(fill("UP", 10, 0.60))
    view = mv(0.40, 0.41, 0.59, 0.60, asks_down=[(0.60, 10.0)])    # only 10 DOWN offered; ~17.6 are needed
    dec, rec = evaluate(c, 0.30, feat(**TOWARD_DOWN), view)
    assert dec is None and "insufficient depth" in rec["why"]
    assert rec["executable_vwap"] is None                          # nothing is pretended executable
    show("7", "shares the plan needs (more than the 10 offered)", expected_required(-c.book.total_cost, 0.50, 0.60),
         rec["recovery_qty_required"])


# 8 ───────────────────────────────────────────────────────────────────────────────────────────────


def test_08_slippage_exceeds_the_limit():
    c = held(fill("UP", 10, 0.60))
    asks = [(0.60, 5.0), (0.62, 5.0), (0.66, 100.0)]                # the requirement reaches the 0.66 level
    dec, rec = evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60, asks_down=asks))
    assert dec is None and "MAX_SLIPPAGE" in rec["why"]
    vw, _, _ = vwap_buy(asks, rec["recovery_qty_required"])
    show("8", "price impact VWAP - best ask (> 0.03)", vw - 0.60, rec["price_impact"])
    assert rec["price_impact"] > 0.03
    dec2, rec2 = evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60, asks_down=asks),
                          prm=rc.RecoveryParams(max_slippage=0.08))
    assert dec2 is not None and dec2.limit == pytest.approx(0.68)   # the limit = best ask + MAX_SLIPPAGE


# 9 ───────────────────────────────────────────────────────────────────────────────────────────────


def test_09_fees_turn_a_theoretical_recovery_into_a_loss():
    c = held(fill("UP", 10, 0.60))
    C = c.book.total_cost
    v = 0.95                                                        # DOWN offered at 0.95
    x_naive = (0.50 + C) / (1.0 - v)                                # the fee-free formula: 133.4 shares
    after_naive = rc.simulate_buy(c.book, "DOWN", x_naive, v, fee_per_share(v), 0.005, None)
    show("9", "DOWN-win P&L if the fee-free quantity were bought (a LOSS)",
         x_naive - C - x_naive * (v + 0.005 + fee_per_share(v)), after_naive["pnl_down_win"])
    assert after_naive["pnl_down_win"] < 0.0
    x_fee = expected_required(-C, 0.50, v)                          # fees in: 160 shares
    show("9", "fee-aware requirement", x_fee, rc.plan_recovery(c.book, "DOWN", [(v, 1e6)], rc.RecoveryParams())["q_required"])
    dec, rec = evaluate(c, 0.03, feat(**TOWARD_DOWN), mv(0.04, 0.05, 0.94, 0.95))
    assert dec is None and "MAX_TOTAL_POSITION" in rec["why"]      # the honest requirement is beyond the limits: no order


# 10 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_10_multiple_sequential_rebalances():
    prm = rc.RecoveryParams(max_order_size=8.0)
    c = held(fill("UP", 10, 0.60))
    view = mv(0.40, 0.41, 0.59, 0.60)
    f = feat(**TOWARD_DOWN)
    now = 10_000.0
    steps = []
    for k in range(3):
        dec, rec = evaluate(c, 0.30, f, view, prm=prm, now=now)
        assert dec is not None, rec["why"]
        rc.on_recovery_sent(c, dec, f"h{k}", now)
        rc.on_fill(c, fill("DOWN", dec.shares, 0.60, kind="recovery", oid=f"h{k}"), now + 300.0)
        rc.on_order_done(c, f"h{k}", True, now + 300.0)
        steps.append((dec.plan_qty, dec.shares, rc.outcomes(c.book)["pnl_down_win"]))
        now += prm.cooldown_ms + 1.0
    C = 10 * 0.60 + 10 * fee_per_share(0.60)
    x1 = expected_required(-C, 0.50, 0.60)
    show("10", "step 1 plan", x1, steps[0][0])
    show("10", "step 1 order (capped at 8)", 8.0, steps[0][1])
    d1 = 8 - C - 8 * (0.60 + fee_per_share(0.60))                  # DOWN-win after 8 DOWN
    show("10", "DOWN-win after step 1", d1, steps[0][2])
    x2 = expected_required(d1, 0.50, 0.60)
    show("10", "step 2 plan (recomputed on the WHOLE position)", x2, steps[1][0])
    show("10", "step 3 order (remainder 1.53 -> venue minimum 5)", 5.0, steps[2][1])
    assert steps[2][2] >= 0.50 and c.book.down_qty == 21.0 and c.actions == 3
    dec, rec = evaluate(c, 0.30, f, view, prm=prm, now=now)
    assert dec is None and "MAX_ACTIONS" in rec["why"]
    assert c.state is rc.RState.HOLDING and len(c.recoveries) == 3


# 11 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_11_existing_up_and_existing_down_inventory():
    c = held(fill("UP", 10, 0.60), fill("DOWN", 6, 0.50, kind="recovery", oid="h0"))
    o = rc.outcomes(c.book)
    C = c.book.total_cost
    show("11", "P&L if UP wins", 10 - C, o["pnl_up_win"])
    show("11", "P&L if DOWN wins", 6 - C, o["pnl_down_win"])
    dec, rec = evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60))
    assert dec is not None and dec.side == "DOWN"
    x = expected_required(6 - C, 0.50, 0.60)                        # (0.50 - (6 - C)) / 0.3782 = 9.98, not 17.64
    show("11", "required DOWN (existing 6 DOWN counted)", x, dec.plan_qty)
    assert x < expected_required(-(10 * 0.60 + 10 * fee_per_share(0.60)), 0.50, 0.60)


# 12 ──────────────────────────────────────────────────────────────────────────────────────────────


def test_12_recovery_quantity_requiring_multiple_ask_levels():
    c = held(fill("UP", 10, 0.60))
    asks = [(0.60, 6.0), (0.61, 6.0), (0.62, 100.0)]
    dec, rec = evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60, asks_down=asks))
    assert dec is not None, rec["why"]
    x = dec.plan_qty
    vw, fps, used = vwap_buy(asks, x)
    show("12", "VWAP over the levels used", vw, rec["executable_vwap"])
    assert len(used) == 3 and used[0] == (0.60, 6.0) and used[1] == (0.61, 6.0)
    # x is the fixed point of the spec's formula at ITS OWN VWAP: (0.5 + C) / (1 - v(x) - fee(v(x)) - slip)
    C = c.book.total_cost
    show("12", "x = (R + C) / (1 - VWAP(x) - fee - slip)", math.ceil((0.50 + C) / (1 - vw - fps - 0.005) * 100) / 100, x)
    show("12", "DOWN-win after, at the multi-level cost (>= 0.50)", x - C - x * (vw + 0.005 + fps), rec["after_pnl_down_win"])
    assert rec["after_pnl_down_win"] >= 0.50 - 1e-9


# ───────────────────────────────── lifecycle, states, restart ─────────────────────────────────


def test_states_fills_and_settlement_reconcile():
    c = rc.RecoveryCycle(book=PairBook(M), a_side="UP", target=0.5)
    rc.on_entry_sent(c, "e1", 10.0, 0.63, 0.0)
    assert c.state is rc.RState.INITIAL_ENTRY and not c.holds
    rc.on_fill(c, fill("UP", 10, 0.60), 1.0)
    rc.on_order_done(c, "e1", True, 1.0)
    assert c.state is rc.RState.HOLDING
    dec, rec = evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60))
    rc.on_recovery_sent(c, dec, "h1", 2.0)
    assert c.state is rc.RState.RECOVERY_PENDING and c.book.down_qty == 0          # pending is not inventory
    rc.on_fill(c, fill("DOWN", 6.0, 0.60, kind="recovery", oid="h1"), 3.0)        # a partial fill
    rc.on_order_done(c, "h1", True, 3.0)
    assert c.state is rc.RState.HOLDING and c.book.down_qty == 6.0 and c.events[-1]["partial"] is True
    back = rc.RecoveryCycle.from_json(c.to_json())
    assert back.book.summary() == c.book.summary() and back.actions == 1 and back.state is rc.RState.HOLDING
    assert rc.on_close(c, 300_000.0) and c.state is rc.RState.SETTLEMENT_PENDING
    r = rc.on_settled(c, "DOWN", 340_000.0)
    assert r["pnl"] == pytest.approx(r["pnl_from_cash"]) == pytest.approx(r["pnl_reconstructed"])
    assert r["pnl"] == pytest.approx(6.0 - c.book.total_cost) and r["recovered"] is True
    assert r["first_leg_pnl"] == pytest.approx(-(10 * 0.60 + 10 * fee_per_share(0.60)))
    with pytest.raises(RuntimeError):
        c.to(rc.RState.HOLDING, 1.0)                                                # SETTLED is final


def test_a_loss_alone_or_a_reversal_alone_is_not_enough():
    c = held(fill("UP", 10, 0.60))
    dec, rec = evaluate(c, 0.30, feat(), mv(0.40, 0.41, 0.59, 0.60))                 # losing, no reversal signal
    assert dec is None and "reversal" in rec["why"]
    dec, rec = evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.58, 0.59, 0.41, 0.42))    # signals, but 2c is not a move
    assert dec is None and "MIN_PRICE_MOVE" in rec["why"]
    dec, rec = evaluate(c, 0.55, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60))    # P(DOWN) 0.45 < cost 0.62: no edge
    assert dec is None and "MIN_RECOVERY_EDGE" in rec["why"]
    dec, rec = evaluate(c, 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60),
                        prm=rc.RecoveryParams(require_min_improvement=True))
    assert dec is None and "MIN_SETTLEMENT_PNL would not improve" in rec["why"]


def test_the_minimum_rises_only_up_to_the_balanced_quantity():
    """The structural fact: buying x closes the branch gap by x; the minimum peaks at x* = gap and falls beyond."""
    c = held(fill("UP", 10, 0.60))
    o = rc.outcomes(c.book)
    gap = o["pnl_up_win"] - o["pnl_down_win"]
    show("x*", "gap between the branches = x* (shares)", 10.0, gap)
    mins = [rc.simulate_buy(c.book, "DOWN", x, 0.60, fee_per_share(0.60), 0.0, None)["min_settlement_pnl"]
            for x in (0.0, 5.0, 10.0, 15.0, 20.0)]
    assert mins[2] == max(mins) and mins[3] < mins[2] and mins[4] < mins[3]


def test_the_rebalancing_repeats_at_every_swing_until_the_bankroll_ends_it():
    """The user (2026-10-02): after a recovery, when the first token rises again, buy it again; repeat. Four swings:
    UP falls -> buy DOWN; UP rises -> buy UP; UP falls -> buy DOWN; UP rises -> buy UP. Each step lifts the favoured
    branch to the target from the WHOLE position, so the quantities roughly double; the bankroll, not an action cap,
    ends the sequence."""
    prm = rc.RecoveryParams(max_actions=10, max_total_position=300.0, max_capital=90.0, max_order_size=60.0)
    c = held(fill("UP", 10, 0.60), target=0.50)
    swings = [("DOWN", 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60)),     # UP down to 0.40: DOWN at 0.60
              ("UP", 0.80, feat(**TOWARD_UP), mv(0.70, 0.71, 0.29, 0.30)),         # UP back up to 0.70: UP at 0.71
              ("DOWN", 0.30, feat(**TOWARD_DOWN), mv(0.40, 0.41, 0.59, 0.60)),
              ("UP", 0.80, feat(**TOWARD_UP), mv(0.70, 0.71, 0.29, 0.30))]
    now, bankroll = 10_000.0, 100.0
    sizes = []
    for k, (side, p_up, f, view) in enumerate(swings):
        o = rc.outcomes(c.book)
        branch = o["pnl_up_win"] if side == "UP" else o["pnl_down_win"]
        ask = view.ask(side)
        x_expected = expected_required(branch, 0.50, ask)
        dec, rec = rc.evaluate_recovery(c, p_up, f, view, now, 200.0, _Model(), prm, bankroll - c.book.cash_spent)
        if dec is None:
            assert "free bankroll" in rec["why"] or "MAX_RECOVERY_CAPITAL" in rec["why"], rec["why"]
            TABLE.append((f"rep{k + 1}", f"swing {k + 1}: plan {x_expected:.2f} {side} rejected by the money limits", x_expected,
                          rec["recovery_qty_required"]))
            break
        assert dec.side == side
        show(f"rep{k + 1}", f"swing {k + 1}: buy {side} to lift its branch to 0.50", x_expected, dec.plan_qty)
        q = dec.shares
        rc.on_recovery_sent(c, dec, f"s{k}", now)
        rc.on_fill(c, fill(side, q, ask, kind="recovery", oid=f"s{k}"), now + 300.0)
        rc.on_order_done(c, f"s{k}", True, now + 300.0)
        sizes.append(q)
        after = rc.outcomes(c.book)
        tgt = after["pnl_up_win"] if side == "UP" else after["pnl_down_win"]
        assert tgt >= 0.50 - 1e-6 or q < dec.plan_qty                   # the favoured branch reaches the target
        now += prm.cooldown_ms + 1.0
    assert len(sizes) >= 3 and all(b > a for a, b in zip(sizes, sizes[1:]))   # repeated, and growing each swing
    assert c.actions == len(sizes) and c.state is rc.RState.HOLDING


# ───────────────────────────────── the late buy ─────────────────────────────────


def test_late_buy_of_the_winner_in_the_last_seconds():
    """The user (2026-10-02): "at the last few seconds, buy winning tokens to increase profits more"."""
    prm = rc.RecoveryParams()
    c = held(fill("UP", 10, 0.60))
    C = c.book.total_cost
    view = mv(0.94, 0.95, 0.05, 0.06)
    dec, rec = rc.evaluate_late(c, 0.97, view, 10_000.0, 15.0, prm, 100.0)      # 15 s left, P(UP) 0.97, UP at 0.95
    assert dec is not None and dec.kind == "late" and dec.side == "UP" and dec.shares == 10.0, rec["why"]
    show("L1", "edge/sh = P - ask - fee - slip", 0.97 - 0.95 - fee_per_share(0.95) - 0.005, rec["edge_per_share"])
    cost = 10 * (0.955 + fee_per_share(0.95))
    show("L1", "UP-win after the clip", 20 - C - cost, rec["after_pnl_up_win"])
    show("L1", "DOWN-win after the clip", -C - cost, rec["after_pnl_down_win"])
    assert dec.limit == pytest.approx(0.98)
    rc.on_recovery_sent(c, dec, "l1", 10_000.0)
    rc.on_fill(c, fill("UP", 10, 0.95, kind="late", oid="l1"), 10_300.0)
    rc.on_order_done(c, "l1", True, 10_300.0)
    assert c.late_orders == 1 and c.actions == 0 and c.book.up_qty == 20 and not c.recoveries
    rc.on_close(c, 300_000.0)
    r = rc.on_settled(c, "UP", 340_000.0)
    show("L1", "late leg P&L = 10 - 10 (0.95 + fee)", 10 - 10 * (0.95 + fee_per_share(0.95)), r["late_leg_pnl"])
    assert r["pnl"] == pytest.approx(r["pnl_reconstructed"]) and r["recovery_effect"] == pytest.approx(0.0)


def test_late_buy_gates_window_probability_price_edge_caps_and_no_position():
    prm = rc.RecoveryParams()
    view = mv(0.94, 0.95, 0.05, 0.06)
    c = held(fill("UP", 10, 0.60))
    assert "late window" in rc.evaluate_late(c, 0.97, view, 1.0, 25.0, prm, 100.0)[1]["why"]       # too early
    assert "late window" in rc.evaluate_late(c, 0.97, view, 1.0, 8.0, prm, 100.0)[1]["why"]        # too late to send
    assert "LATE_MIN_P" in rc.evaluate_late(c, 0.90, view, 1.0, 15.0, prm, 100.0)[1]["why"]        # not a clear winner
    assert "LATE_MAX_PRICE" in rc.evaluate_late(c, 0.99, mv(0.97, 0.98, 0.02, 0.03), 1.0, 15.0, prm, 100.0)[1]["why"]
    assert "LATE_MIN_EDGE" in rc.evaluate_late(c, 0.95, mv(0.95, 0.96, 0.04, 0.05), 1.0, 15.0, prm, 100.0)[1]["why"]  # 0.95 - 0.96 < 0
    # DOWN as the winner, on a market with no position at all
    c2 = rc.RecoveryCycle(book=PairBook(M), a_side="UP", target=0.5)
    dec, rec = rc.evaluate_late(c2, 0.03, mv(0.04, 0.05, 0.94, 0.95), 1.0, 12.0, prm, 100.0)
    assert dec is not None and dec.side == "DOWN" and c2.state is rc.RState.WAITING
    rc.on_recovery_sent(c2, dec, "l1", 1.0)
    rc.on_fill(c2, fill("DOWN", 10, 0.95, kind="late", oid="l1"), 2.0)
    rc.on_order_done(c2, "l1", True, 2.0)
    assert c2.state is rc.RState.HOLDING and c2.book.down_qty == 10
    # caps: 2 clips per market, 20 USDC
    dec2, rec2 = rc.evaluate_late(c2, 0.03, mv(0.04, 0.05, 0.94, 0.95), 1.0 + prm.cooldown_ms + 1, 11.0, prm, 100.0)
    assert dec2 is not None
    rc.on_recovery_sent(c2, dec2, "l2", 1.0 + prm.cooldown_ms + 1)
    rc.on_fill(c2, fill("DOWN", 10, 0.95, kind="late", oid="l2"), 2.0 + prm.cooldown_ms)
    rc.on_order_done(c2, "l2", True, 2.0 + prm.cooldown_ms)
    assert "LATE_MAX_ORDERS" in rc.evaluate_late(c2, 0.03, mv(0.04, 0.05, 0.94, 0.95), 2 * prm.cooldown_ms + 10, 10.5, prm, 100.0)[1]["why"]
    c3 = held(fill("UP", 10, 0.60))
    assert "LATE_MAX_CAPITAL" in rc.evaluate_late(c3, 0.97, view, 1.0, 15.0, rc.RecoveryParams(late_max_capital=5.0), 100.0)[1]["why"]
    assert "late buy off" in rc.evaluate_late(c3, 0.97, view, 1.0, 15.0, rc.RecoveryParams(late_enabled=False), 100.0)[1]["why"]


if __name__ == "__main__":
    import inspect
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and inspect.isfunction(v)]
    for t in tests:
        t()
    print(f"{len(tests)} tests passed\n")
    print(f"{'case':5s} {'quantity':62s} {'expected':>12s} {'actual':>12s}")
    for case, what, e, a in TABLE:
        print(f"{case:5s} {what:62s} {e:>12.4f} {a:>12.4f}")
    sys.exit(0)
