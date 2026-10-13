"""LIMITED-DEPTH DIRECTIONAL RECOVERY WITH A TWO-SIDED HEDGE AT SWING 3+ (the user's spec, 2026-10-07;
strategy/pairlock_swing.py, run by the recovery engine with TPB_PAIRLOCK_SWING_MODE=1).

The spec's arithmetic is checked on the pure state machine with NO fees (the spec's numbers are before fees): every buy
is sized and priced from the ask levels the test gives. Then the same path with the venue's fee, non-ideal hedge
prices, a book too thin for the hedge, the capital limit, the recovery depth switch and the engine's live path
(entry -> swing 1 -> swing 2 -> hedge -> swing 4 -> settlement, persisted and restored)."""
from __future__ import annotations

import random
from dataclasses import replace

import pytest

from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.strategy import pairlock_swing as sw
from troll_poly_bot.strategy.pairlock import MarketView
from troll_poly_bot.strategy.pairlock_book import FillRecord, PairBook, fee_per_share

M = "btc-updown-5m-1"
NO_FEE = lambda p: 0.0                                    # noqa: E731  (the spec's numbers are before fees)
PRM = rc.RecoveryParams(swing_mode=True, swing_sizing="fixed", max_total_position=300.0, max_slippage=0.03, min_time_left_s=15.0,
                        cooldown_ms=2000.0, depth_haircut=0.0, min_size=5.0)


def view(up_bid, up_ask, down_bid, down_ask, asks_up=None, asks_down=None, depth=1000.0) -> MarketView:
    return MarketView(bid_up=up_bid, ask_up=up_ask, bid_down=down_bid, ask_down=down_ask,
                      asks_up=asks_up or [(up_ask, depth)], asks_down=asks_down or [(down_ask, depth)],
                      bids_up=[(up_bid, depth)], bids_down=[(down_bid, depth)])


A_LEADS_ENTRY = view(0.59, 0.60, 0.40, 0.41)            # ~20 s after the open: A = UP leads, 10 UP executable at 0.60
B_AT_070 = view(0.30, 0.31, 0.69, 0.70)                 # DOWN (B) leads, its ask at the recovery price
A_AT_070 = view(0.69, 0.70, 0.30, 0.31)                 # UP (A) leads again at 0.70


class Run:
    """One market on the pure state machine: decisions from ``sw``, fills applied at the order's VWAP (or a given price)."""

    def __init__(self, prm=PRM, fees=NO_FEE):
        self.prm, self.fees = prm, fees
        self.book = PairBook(M)
        self.st: sw.SwingState | None = None
        self.t = 0.0
        self.orders: list[sw.SwingOrder] = []

    def enter(self, mv=A_LEADS_ENTRY, elapsed=21.0, bankroll=1000.0):
        dec, rec = sw.entry_decision(mv, elapsed, 279.0, self.prm, bankroll, now=self.t, fees=self.fees)
        assert dec is not None, rec["why"]
        self.st = sw.SwingState.start(dec.side, self.t, self.prm)
        self.fill(dec)
        return dec

    def step(self, mv, left=200.0, elapsed=100.0, bankroll=1000.0):
        self.t += 3000.0                                   # past the 2 s cooldown
        return sw.evaluate(self.st, self.book, mv, self.t, left, elapsed, self.prm, bankroll, self.fees)

    def fill(self, dec: sw.SwingOrder, qty=None, price=None):
        self.orders.append(dec)
        sw.on_sent(self.st, dec.kind, dec.swing, self.t)
        q = dec.shares if qty is None else qty
        px = dec.vwap if price is None else price
        fee = self.fees(px) * q
        n = len(self.orders)
        self.book.add_fill(FillRecord(M, self.t, dec.side.lower(), dec.side, q, px, fee, f"o{n}", f"o{n}-1", "BUY", px, dec.kind))
        sw.on_fill(self.st, dec.kind, dec.side, q, px, fee, self.t + 300.0, dec.swing)

    def pnl(self, winner):
        return self.book.resolution_pnl(winner)


def default_path(r: Run, through: int):
    """The spec's default path up to swing ``through`` (0..3), every buy filled in full at its VWAP."""
    r.enter()
    seq = [B_AT_070, A_AT_070, B_AT_070]
    for k in range(through):
        dec, rec = r.step(seq[k])
        assert dec is not None, rec["why"]
        r.fill(dec)
    return r


# ───────────────────────────── the spec's arithmetic (item 17) ─────────────────────────────

def test_swing_0_ten_a_at_060():
    r = Run()
    dec = r.enter()
    assert (dec.kind, dec.side, dec.shares, dec.limit) == ("entry", "UP", 10.0, 0.60)
    assert dec.vwap == pytest.approx(0.60)
    assert r.st.phase is sw.Phase.SWING_0 and r.st.initial_token == "UP" and r.st.swing_number == 0
    assert r.book.total_cost == pytest.approx(6.00)                       # cost 6
    assert r.book.qty("UP") == 10.0                                      # payout 10 if A wins
    assert r.pnl("UP") == pytest.approx(4.00)                            # P&L +4


def test_swing_1_twenty_four_b_at_070():
    r = Run()
    r.enter()
    dec, rec = r.step(B_AT_070)
    assert dec is not None and (dec.kind, dec.side, dec.shares, dec.swing) == ("recovery", "DOWN", 24.0, 1), rec["why"]
    assert rec["new_swing"]["n"] == 1 and r.st.phase is sw.Phase.SWING_1_RECOVERY
    r.fill(dec)
    assert r.book.total_cost == pytest.approx(22.80)                     # 6.00 + 16.80
    assert r.book.qty("DOWN") == 24.0                                    # B payout 24
    assert r.pnl("DOWN") == pytest.approx(1.20)                          # +1.20
    sw_row = r.st.swings[0]
    assert sw_row["filled"] == 24.0 and sw_row["vwap"] == pytest.approx(0.70) and sw_row["cost"] == pytest.approx(16.80)


def test_swing_2_eighty_a_at_070():
    r = default_path(Run(), 2)
    assert r.st.phase is sw.Phase.SWING_2_RECOVERY and r.st.swing_number == 2
    assert r.orders[-1].shares == 80.0 and r.orders[-1].side == "UP"
    assert (r.book.qty("UP"), r.book.qty("DOWN")) == (90.0, 24.0)       # A 90 / B 24
    assert r.book.total_cost == pytest.approx(78.80)
    assert r.pnl("UP") == pytest.approx(11.20)                           # A payout 90 - 78.80
    assert r.pnl("DOWN") == pytest.approx(24.0 - 78.80)                  # swing 2 is recoverable, not a failure: it is a bet on A


def test_swing_3_hedges_sixty_six_b_to_ninety_ninety():
    r = default_path(Run(), 2)
    dec, rec = r.step(B_AT_070)
    assert rec["new_swing"]["n"] == 3 and r.st.phase is sw.Phase.HEDGE_MODE and r.st.hedge_mode
    assert dec is not None and dec.kind == "hedge" and dec.side == "DOWN" and dec.shares == 66.0, rec["why"]
    hp = rec["hedge"]                                                    # the item-7 report, before the order
    assert (hp["existing_a"], hp["existing_b"], hp["hedge_required"], hp["hedge_qty"]) == (90.0, 24.0, 66.0, 66.0)
    assert hp["hedge_vwap"] == pytest.approx(0.70) and hp["hedge_cost"] == pytest.approx(46.20)
    assert (hp["resulting_a"], hp["resulting_b"]) == (90.0, 90.0)
    assert hp["total_cost_after"] == pytest.approx(125.00) and hp["guaranteed_payout"] == pytest.approx(90.0)
    assert hp["locked_pnl"] == pytest.approx(-35.00)
    r.fill(dec)
    assert (r.book.qty("UP"), r.book.qty("DOWN")) == (90.0, 90.0)
    assert r.book.total_cost == pytest.approx(125.00)
    assert r.pnl("UP") == pytest.approx(-35.00) and r.pnl("DOWN") == pytest.approx(-35.00)   # payout 90 regardless
    acc = sw.accounting(r.st, r.book, PRM, fees=NO_FEE)
    assert acc["payout_if_up_wins"] == acc["payout_if_down_wins"] == 90.0
    assert acc["pnl_if_up_wins"] == pytest.approx(-35.0) and acc["pnl_if_down_wins"] == pytest.approx(-35.0)
    assert acc["maximum_additional_capital_required"] == pytest.approx(0.0) and acc["hedge_mode"] and not acc["recovery_mode"]


def test_swing_4_and_later_never_restart_the_directional_recovery():
    r = default_path(Run(), 3)
    for k, mv in enumerate((A_AT_070, B_AT_070, A_AT_070, B_AT_070), start=4):
        dec, rec = r.step(mv)
        assert rec["new_swing"]["n"] == k and rec["new_swing"]["action"].startswith("none")
        assert dec is None and r.st.phase is sw.Phase.HEDGE_MODE, rec["why"]
        assert "balanced" in rec["why"]
    assert (r.book.qty("UP"), r.book.qty("DOWN")) == (90.0, 90.0) and r.book.total_cost == pytest.approx(125.0)
    assert len(r.orders) == 4                                            # entry, swing 1, swing 2, the hedge: nothing more


def test_noise_is_not_a_swing():
    r = Run()
    r.enter()
    # B leads inside the hysteresis (mids 0.51 vs 0.49), then B leads clearly but its ask stays under 0.70
    for mv in (view(0.48, 0.50, 0.50, 0.52), view(0.40, 0.42, 0.58, 0.60), view(0.31, 0.33, 0.67, 0.69)):
        dec, rec = r.step(mv)
        assert dec is None and "new_swing" not in rec and r.st.swing_number == 0, rec["why"]
    assert r.st.current_leader == "DOWN"                                 # the leader moved (hysteresis passed) ...
    assert r.st.direction == "UP" and r.st.phase is sw.Phase.SWING_0     # ... but no swing: 0.70 never reached
    # back and forth around 0.50 within the hysteresis does not even move the leader
    assert sw.leader_with_hysteresis("UP", {"UP": 0.49, "DOWN": 0.51}, 0.04) == "UP"
    assert sw.leader_with_hysteresis("UP", {"UP": 0.47, "DOWN": 0.52}, 0.04) == "DOWN"


def test_a_partial_directional_fill_is_completed_while_the_swing_stands():
    r = Run()
    r.enter()
    dec, _ = r.step(B_AT_070)
    r.fill(dec, qty=15.0)                                                # fill-and-kill: 15 of 24
    dec, rec = r.step(view(0.28, 0.29, 0.71, 0.72))
    assert dec is not None and dec.kind == "recovery" and dec.shares == 9.0 and dec.swing == 1, rec["why"]
    r.fill(dec)
    dec, rec = r.step(B_AT_070)
    assert dec is None and "recovery done: 24 of 24" in rec["why"]


# ───────────────────────────── execution realism ─────────────────────────────

def test_non_ideal_hedge_prices_use_the_executable_vwap():
    """A ask 0.32, B ask 0.71: the hedge is priced at B's own levels (0.71, then 0.72), never at 1 - 0.32 = 0.68."""
    r = default_path(Run(replace(PRM, swing_max_market_capital=200.0)), 2)
    mv = view(0.31, 0.32, 0.70, 0.71, asks_down=[(0.71, 40.0), (0.72, 100.0)])
    dec, rec = r.step(mv)
    assert dec is not None and dec.kind == "hedge" and dec.shares == 66.0, rec["why"]
    hp = rec["hedge"]
    vwap = (40 * 0.71 + 26 * 0.72) / 66
    assert hp["hedge_vwap"] == pytest.approx(vwap) and hp["hedge_vwap"] != pytest.approx(1 - 0.32)
    assert hp["hedge_cost"] == pytest.approx(40 * 0.71 + 26 * 0.72)
    assert hp["locked_pnl"] == pytest.approx(90.0 - 78.80 - (40 * 0.71 + 26 * 0.72))   # worse than -35: the real asks
    assert mv.ask_up + mv.ask_down == pytest.approx(1.03)


def test_insufficient_depth_hedges_only_what_the_book_holds():
    r = default_path(Run(replace(PRM, swing_max_market_capital=200.0)), 2)
    thin = view(0.29, 0.30, 0.69, 0.70, asks_down=[(0.70, 25.0), (0.72, 15.0), (0.80, 500.0)])   # 40 sh within 0.73
    dec, rec = r.step(thin)
    assert dec is not None and dec.kind == "hedge" and dec.shares == 40.0, rec["why"]
    assert rec["hedge"]["partial"] and rec["hedge"]["bound"] == "depth at or under the limit"
    assert rec["hedge"]["resulting_b"] == 64.0                           # not 90: the report does not pretend
    r.fill(dec)
    assert (r.book.qty("UP"), r.book.qty("DOWN")) == (90.0, 64.0)
    dec, rec = r.step(view(0.29, 0.30, 0.69, 0.70, asks_down=[(0.70, 10.0)]))
    assert dec is not None and dec.shares == 10.0                       # keeps evening out with what is there
    r.fill(dec)
    dec, rec = r.step(view(0.70, 0.71, 0.28, 0.29, asks_down=[(0.29, 500.0)]))   # swing 4: B is cheap now
    assert rec["new_swing"]["n"] == 4 and dec is not None and dec.kind == "hedge" and dec.side == "DOWN" and dec.shares == 16.0
    r.fill(dec)
    assert r.book.qty("UP") == r.book.qty("DOWN") == 90.0


def test_the_capital_limit_is_never_exceeded():
    # cap 120 < the default path's 125: the hedge is cut to what fits at its worst-case price
    r = default_path(Run(replace(PRM, swing_max_market_capital=120.0)), 2)
    dec, rec = r.step(B_AT_070)
    assert dec is not None and rec["hedge"]["bound"] == "MAX_MARKET_CAPITAL"
    assert r.book.cash_spent + dec.shares * dec.limit <= 120.0 + 1e-9    # worst case: every share at the limit
    r.fill(dec)
    assert r.book.cash_spent <= 120.0 + 1e-9
    dec, rec = r.step(B_AT_070)
    assert dec is None and "MAX_MARKET_CAPITAL" in rec["why"]            # what is left under the cap is under 5 shares
    # the default path at the spec's cap of 125.00 fits exactly (no fees): the hedge's limit drops to 0.70 to fit 66
    r = default_path(Run(), 2)
    dec, rec = r.step(B_AT_070)
    assert dec.shares == 66.0 and dec.limit == pytest.approx(0.70)
    r.fill(dec)
    assert r.book.cash_spent == pytest.approx(125.0)
    # with the venue's fee (0.07 p (1 - p)) the same path needs ~127.67: at 125 the hedge stops short, never over
    r = default_path(Run(fees=fee_per_share), 2)
    dec, rec = r.step(B_AT_070)
    assert dec.shares < 66.0 and rec["hedge"]["bound"] == "MAX_MARKET_CAPITAL"
    assert r.book.cash_spent + dec.shares * (dec.limit + fee_per_share(dec.limit)) <= 125.0 + 1e-9
    r.fill(dec)
    assert r.book.cash_spent <= 125.0 + 1e-9


def test_random_paths_never_spend_above_the_cap():
    rng = random.Random(7)
    for cap in (40.0, 90.0, 125.0, 150.0):
        prm = replace(PRM, swing_max_market_capital=cap)
        for _ in range(60):
            r = Run(prm, fees=fee_per_share)
            dec, _ = sw.entry_decision(A_LEADS_ENTRY, 21.0, 279.0, prm, 1000.0, fees=fee_per_share)
            if dec is None:
                continue
            r.st = sw.SwingState.start(dec.side, 0.0, prm)
            r.fill(dec)
            for _ in range(12):
                b_up = rng.random() < 0.5
                ask = round(rng.uniform(0.60, 0.95), 2)
                asks = [(ask, rng.uniform(5, 120)), (round(min(0.99, ask + 0.01), 2), rng.uniform(5, 120))]
                mv = (view(round(1 - ask - 0.01, 2), round(1 - ask + 0.01, 2), round(ask - 0.01, 2), ask, asks_down=asks)
                      if b_up else view(round(ask - 0.01, 2), ask, round(1 - ask - 0.01, 2), round(1 - ask + 0.01, 2), asks_up=asks))
                dec, _ = r.step(mv)
                if dec is not None:
                    worst = r.book.cash_spent + dec.shares * (dec.limit + fee_per_share(dec.limit))
                    assert worst <= cap + 1e-9
                    r.fill(dec, price=max(p for p, n in mv.asks(dec.side) if p <= dec.limit + 1e-9))   # the worst fill
                assert r.book.cash_spent <= cap + 1e-9


def test_the_entry_uses_the_executable_vwap_not_the_mid():
    # mids say UP ~0.585, but only 4 shares are offered at 0.60 and the rest at 0.64: VWAP(10) is outside the band
    mv = view(0.57, 0.60, 0.40, 0.43, asks_up=[(0.60, 4.0), (0.64, 100.0)])
    dec, rec = sw.entry_decision(mv, 21.0, 279.0, PRM, 1000.0)
    assert dec is None or dec.shares <= 4.0                              # never 10 shares above 0.60
    assert dec is None and "minimum" in rec["why"]                       # 4 sh under the limit < the 5-share minimum
    # depth spread over levels inside the band: the VWAP decides
    mv = view(0.53, 0.54, 0.45, 0.47, asks_up=[(0.54, 3.0), (0.56, 7.0)])
    dec, rec = sw.entry_decision(mv, 21.0, 279.0, PRM, 1000.0)
    assert dec is not None and dec.vwap == pytest.approx((3 * 0.54 + 7 * 0.56) / 10) and dec.limit == 0.60
    # outside the window, or no leader, or the leader too dear: nothing
    assert sw.entry_decision(A_LEADS_ENTRY, 12.0, 288.0, PRM, 1000.0)[0] is None
    assert sw.entry_decision(A_LEADS_ENTRY, 31.0, 269.0, PRM, 1000.0)[0] is None
    assert sw.entry_decision(view(0.49, 0.51, 0.48, 0.50), 21.0, 279.0, PRM, 1000.0)[0] is None
    assert sw.entry_decision(view(0.64, 0.65, 0.35, 0.36), 21.0, 279.0, PRM, 1000.0)[0] is None


# ───────────────────────────── the depth switch, the hedge switch ─────────────────────────────

def test_recovery_depth_one_hedges_at_swing_2():
    r = default_path(Run(replace(PRM, swing_max_recovery=1)), 1)
    dec, rec = r.step(A_AT_070)
    assert r.st.phase is sw.Phase.HEDGE_MODE and dec.kind == "hedge" and dec.side == "UP" and dec.shares == 14.0
    r.fill(dec)
    assert r.book.qty("UP") == r.book.qty("DOWN") == 24.0
    assert r.pnl("UP") == pytest.approx(24.0 - 22.80 - 14 * 0.70)


def test_recovery_depth_zero_hedges_at_swing_1():
    r = default_path(Run(replace(PRM, swing_max_recovery=0)), 0)
    dec, rec = r.step(B_AT_070)
    assert r.st.phase is sw.Phase.HEDGE_MODE and dec.kind == "hedge" and dec.side == "DOWN" and dec.shares == 10.0


def test_hedge_disabled_holds_at_swing_3():
    r = default_path(Run(replace(PRM, swing_two_sided_hedge=False)), 2)
    dec, rec = r.step(B_AT_070)
    assert dec is None and r.st.phase is sw.Phase.HEDGE_MODE and "disabled" in rec["why"]
    assert (r.book.qty("UP"), r.book.qty("DOWN")) == (90.0, 24.0)


def test_the_plan_is_fixed_per_market_and_the_states_never_go_back():
    prm = replace(PRM)
    r = default_path(Run(prm), 1)
    prm.swing2_shares = 5.0                                              # a dashboard change mid-market ...
    dec, _ = r.step(A_AT_070)
    assert dec.shares == 80.0                                            # ... applies to the next market, not this one
    with pytest.raises(RuntimeError):
        r.st.to(sw.Phase.SWING_1_RECOVERY, 0.0)                          # SWING_2 -> SWING_1 is not a transition
    st = sw.SwingState.start("UP", 0.0, PRM)
    st.phase = sw.Phase.HEDGE_MODE
    with pytest.raises(RuntimeError):
        st.to(sw.Phase.SWING_2_RECOVERY, 0.0)


def test_accounting_fields_and_projection():
    r = Run()
    r.enter()
    acc = sw.accounting(r.st, r.book, PRM, A_LEADS_ENTRY, fees=NO_FEE)
    for k in ("swing_number", "initial_token", "current_leader", "up_shares", "down_shares", "up_cost", "down_cost", "total_cost",
              "average_up_cost", "average_down_cost", "last_swing_timestamp", "last_swing_price", "hedge_mode", "recovery_mode",
              "realized_pnl", "theoretical_resolution_pnl", "maximum_additional_capital_required", "pnl_if_up_wins",
              "pnl_if_down_wins", "mark_to_market_pnl"):
        assert k in acc
    # from swing 0 the worst path still to come is 24 + 80 + the 66-share hedge at 0.70 = 119 (125 in all)
    assert acc["maximum_additional_capital_required"] == pytest.approx(119.0)
    assert acc["theoretical_resolution_pnl"] == {"UP": pytest.approx(4.0), "DOWN": pytest.approx(-6.0)}
    done = default_path(Run(), 3)
    fin = sw.on_settled(done.st, done.book, "UP", 1.0)
    assert fin["bucket"] == "3+" and fin["pnl"] == pytest.approx(-35.0) and fin["balanced"] and fin["hedged"]
    assert done.st.phase is sw.Phase.RESOLUTION and sw.accounting(done.st, done.book, PRM)["realized_pnl"] == pytest.approx(-35.0)


def test_state_round_trips_through_the_cycle_json():
    r = default_path(Run(), 2)
    c = rc.RecoveryCycle(book=r.book, a_side="UP", state=rc.RState.HOLDING, swing=r.st)
    c2 = rc.RecoveryCycle.from_json(c.to_json())
    assert c2.swing.to_json() == r.st.to_json() and c2.swing.phase is sw.Phase.SWING_2_RECOVERY
    assert c2.book.qty("UP") == 90.0 and c2.book.qty("DOWN") == 24.0


# ───────────────────────────── the engine's live path ─────────────────────────────

def _swing_engine(**kw):
    from test_pairlock_recovery_engine import _eng
    from test_spot_lag import T0, _Twap
    e = _eng(rprm=rc.RecoveryParams(swing_mode=True, max_total_position=300.0, **{"swing_sizing": "fixed", **kw}))
    tw = _Twap()
    for t in range(int(T0 - 60_000), int(T0 + 300_000), 200):
        tw.update(100.0, t)
    return e, tw


def _at(e, tw, t, up, down):
    from test_pairlock_engine_v2 import VIEW
    from test_pairlock_recovery_engine import _deep
    from test_spot_lag import _meta, _Vol
    from troll_poly_bot.execution.latency import COLOCATED
    return e.evaluate(_meta("BTC"), _deep("UP", *up), _deep("DOWN", *down), VIEW, 200.0, 100.0, t, _Vol(), tw, COLOCATED, 500.0)


def _buy(e, ev, t, px):
    from test_pairlock_engine_v2 import _fill
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, px, ev.size, t + 300.0)
    return o


def test_engine_runs_the_whole_path_and_settles_by_swing_bucket():
    from test_spot_lag import SLUG, T0
    e, tw = _swing_engine(swing_max_market_capital=150.0, late_enabled=True, ladder_window_s=90.0, profit_exit=0.5,
                          recovery_sell=True, pair_bid=True)
    assert e.strategy_label == "R-swing" and e.manages_exits
    ev = _at(e, tw, T0 + 10_000, (0.59, 0.60), (0.40, 0.41))
    assert not ev.tradeable                                              # before the entry window
    ev = _at(e, tw, T0 + 21_000, (0.59, 0.60), (0.40, 0.41))
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "open" and (ev.side, ev.size, ev.limit_price) == ("UP", 10.0, 0.60)
    _buy(e, ev, T0 + 21_000, 0.60)
    cyc = e.cycles[SLUG]
    assert cyc.swing.phase is sw.Phase.SWING_0 and cyc.book.up_qty == 10.0
    assert e.pair_order(_market(), "UP", 10.0, 0.60, T0 + 22_000) is None   # no other-side bid in swing mode
    for t, up, down, side, q in ((60_000, (0.30, 0.31), (0.69, 0.70), "DOWN", 24.0),
                                 (120_000, (0.69, 0.70), (0.30, 0.31), "UP", 80.0),
                                 (180_000, (0.30, 0.31), (0.69, 0.70), "DOWN", 66.0)):
        ev = _at(e, tw, T0 + t, up, down)
        assert ev.tradeable and (ev.side, ev.size) == (side, q), (ev.reason, ev.detail)
        _buy(e, ev, T0 + t, 0.70)
    assert cyc.swing.phase is sw.Phase.HEDGE_MODE and cyc.book.up_qty == cyc.book.down_qty == 90.0
    assert e.stats["hedge_sent"] == 1 and e.stats["hedge_fills"] == 1 and e.stats["recovery_sent"] == 2
    # swing 4 and the last 90 s: no directional buy, no ladder, no late buy, no sells
    for t, up, down in ((240_000, (0.69, 0.70), (0.30, 0.31)), (285_000, (0.89, 0.90), (0.09, 0.10))):
        ev = _at(e, tw, T0 + t, up, down)
        assert not ev.tradeable
    assert cyc.swing.swing_number == 4 and len(cyc.book.fills) == 4
    from test_spot_lag import _book
    assert e.exit_actions(_market(), _book("UP", 0.99, 1.0), _book("DOWN", 0.98, 0.99), T0 + 286_000, {}, {}) == []
    snap = e.snapshot()["pairlock"]
    assert snap["open"][0]["state"] == "HEDGE_MODE" and "swing 4" in snap["open"][0]["locked_label"]
    o = snap["swing"]["open"][0]
    assert o["pnl_if_up_wins"] == pytest.approx(o["pnl_if_down_wins"]) and o["up_shares"] == o["down_shares"] == 90.0
    # persisted and restored: the same swing state
    from test_pairlock_recovery_engine import _eng
    e2 = _eng(rprm=e.rprm)
    e2.restore_state(e.persist_state(), T0 + 290_000)
    assert e2.cycles[SLUG].swing.to_json() == cyc.swing.to_json()
    # settlement: reconciled, counted in the 3+ bucket
    pnl = cyc.book.resolution_pnl("UP")
    assert pnl == pytest.approx(90.0 - (6.0 + 0.6 * 0.4 * 0.07 * 10 + (24 + 80 + 66) * (0.70 + fee_per_share(0.70))))
    e.on_market_settled(SLUG, True, pnl, T0 + 400_000)
    assert not e.accounting_halt and e.stats["swing3+_markets"] == 1 and e.stats["swing3+_pnl"] == pytest.approx(pnl)
    assert e.snapshot()["pairlock"]["swing"]["by_swing"]["3+"]["markets"] == 1


def _market():
    from test_spot_lag import _meta
    return _meta("BTC").market


def test_engine_swing_0_win_and_killed_entry_retry():
    from test_spot_lag import SLUG, T0
    e, tw = _swing_engine()
    ev = _at(e, tw, T0 + 21_000, (0.57, 0.58), (0.42, 0.43))
    o = e.to_order(ev, T0 + 21_000)
    e.entry_result(o.order_id, False, "fok_unfillable")                 # killed: the market is flat again
    _at(e, tw, T0 + 21_500, (0.57, 0.58), (0.42, 0.43))
    assert SLUG not in e.cycles
    ev = _at(e, tw, T0 + 22_000, (0.57, 0.58), (0.42, 0.43))
    assert ev.tradeable and ev.size == 10.0                             # tried again inside the window
    _buy(e, ev, T0 + 22_000, 0.58)
    cyc = e.cycles[SLUG]
    e.on_market_settled(SLUG, True, cyc.book.resolution_pnl("UP"), T0 + 400_000)
    assert e.stats["swing0_markets"] == 1 and e.stats["swing0_wins"] == 1
    assert e.stats["swing0_pnl"] == pytest.approx(10 - 10 * (0.58 + fee_per_share(0.58)))


# ───────────────────────────── configuration, controls, real-money guard ─────────────────────────────

def test_env_names_and_defaults():
    from troll_poly_bot.strategy.pairlock_recovery_engine import recovery_params_from_env
    d = rc.RecoveryParams()
    assert (d.swing_mode, d.swing_initial_shares, d.swing_entry_min, d.swing_entry_max, d.swing_recovery_price, d.swing_sizing,
            d.swing_target_profit, d.swing_max_recovery, d.swing_two_sided_hedge, d.swing_max_market_capital) == \
        (False, 10.0, 0.55, 0.60, 0.70, "target", 3.0, 2, True, 125.0)
    p = recovery_params_from_env({"TPB_PAIRLOCK_SWING_MODE": "1", "TPB_PAIRLOCK_SWING1_SHARES": "30",
                                  "TPB_PAIRLOCK_SWING_MAX_RECOVERY_SWING": "1", "TPB_PAIRLOCK_SWING_MAX_MARKET_CAPITAL": "150",
                                  "TPB_PAIRLOCK_SWING_ENABLE_TWO_SIDED_HEDGE": "0", "TPB_PAIRLOCK_SWING_LEADER_HYSTERESIS": "0.06",
                                  "TPB_PAIRLOCK_REC_SWING2_SHARES": "70"})
    assert (p.swing_mode, p.swing1_shares, p.swing2_shares, p.swing_max_recovery, p.swing_max_market_capital,
            p.swing_two_sided_hedge, p.swing_leader_hysteresis) == (True, 30.0, 70.0, 1, 150.0, False, 0.06)
    with pytest.raises(ValueError):
        rc.RecoveryParams(swing_max_recovery=3)                          # sizes exist for swings 1 and 2 only


def test_the_variables_page_steers_the_recovery_depth():
    from troll_poly_bot import control
    prm = rc.RecoveryParams(swing_mode=True)
    assert control.TUNABLES["pairlock.swing_max_recovery"].hi == 2
    changed = control.apply_to({"pairlock.swing_max_recovery": 1, "pairlock.swing_max_market_capital": 150.0}, cfg=None,
                               pairlock=prm)
    assert prm.swing_max_recovery == 1 and prm.swing_max_market_capital == 150.0 and len(changed) == 2
    vals = control.current_values(None, pairlock=prm)
    assert vals["pairlock.swing_max_recovery"] == 1 and vals["pairlock.swing_target_profit"] == 3.0
    assert "pairlock.swing1_shares" not in control.TUNABLES and "pairlock.swing2_shares" not in control.TUNABLES


def test_swing_mode_is_refused_on_real_money():
    from troll_poly_bot.__main__ import check_triangle
    env = {"TPB_TRIANGLE_ENABLED": "1", "TPB_PAIRLOCK_STRATEGY": "R", "TPB_PAIRLOCK_ALLOW_REAL": "1", "TPB_PAIRLOCK_SWING_MODE": "1"}
    for mode in ("micro-live", "shadow", "live"):
        with pytest.raises(SystemExit):
            check_triangle("triangle", mode, env)
    check_triangle("triangle", "paper", env)                             # paper: fine
    check_triangle("triangle", "micro-live", {**env, "TPB_PAIRLOCK_SWING_MODE": "0"})   # the live bot as it was
