"""Execution scenarios: the paper engine must not fill quotes the venue had already pulled.

On 2026-09-22 the paper bot filled 23 of 23 orders sent into a quote that had
not re-priced a spot move; the live bot filled 1 of 29. These tests pin the
quote-survival rule that closes that gap, and the scenario knobs around it.
"""
from __future__ import annotations

import pytest

from troll_poly_bot.execution.latency import INSTANT, LatencyModel
from troll_poly_bot.execution.paper import FeeModel, PaperExchange, RejectReason
from troll_poly_bot.execution.scenarios import (
    CALIBRATED, CONSERVATIVE, OPTIMISTIC, REALISTIC, SCENARIOS, STRESS, ExecutionScenario, scenario_named,
)
from troll_poly_bot.types import BookLevel, Order, OrderBook, OrderState, Side, TimeInForce

T = "TOK"


def book(bids, asks, ts=0.0):
    return OrderBook(token_id=T, bids=[BookLevel(p, s) for p, s in bids],
                     asks=[BookLevel(p, s) for p, s in asks], ts=ts)


def buy(price=0.11, size=10.0, tif=TimeInForce.FOK):
    return Order(token_id=T, side=Side.BUY, price=price, size=size, tif=tif, expected_price=price - 0.01)


def sell(price=0.30, size=10.0):
    return Order(token_id=T, side=Side.SELL, price=price, size=size, tif=TimeInForce.FOK, expected_price=price)


def ex(scenario=REALISTIC, seed=0):
    return PaperExchange(LatencyModel(INSTANT, seed=1), FeeModel(rate=0.0), starting_balance=100.0,
                         scenario=scenario, seed=seed)


# ───────────────────────────────── presets ────────────────────────────────────


def test_presets_are_ordered_from_kind_to_hostile():
    assert OPTIMISTIC.quote_survival_ms == 0.0
    assert 0 < REALISTIC.quote_survival_ms < CONSERVATIVE.quote_survival_ms < STRESS.quote_survival_ms
    assert CONSERVATIVE.reject_prob < STRESS.reject_prob
    # venue_lag is a different fill model (book read late, no survival rule), not a harsher preset
    assert set(SCENARIOS) == {"optimistic", "realistic", "conservative", "stress", "venue_lag", "calibrated"}
    assert scenario_named("Realistic") is REALISTIC
    with pytest.raises(ValueError):
        scenario_named("hopeful")


def test_the_default_engine_is_the_old_engine():
    """Direct users of PaperExchange (sim, old tests) see no behaviour change."""
    assert ex(OPTIMISTIC).scenario is OPTIMISTIC
    e = PaperExchange(LatencyModel(INSTANT), FeeModel(rate=0.0))
    assert e.scenario is OPTIMISTIC


# ────────────────────────────── quote survival ────────────────────────────────


def test_a_quote_that_vanishes_inside_the_window_is_not_filled():
    """The exact 2026-09-22 shape: ask 0.10 on the local book, spot has moved,
    the venue has already pulled it; the local book re-prices 1 s later."""
    e = ex(REALISTIC)
    stale = book([(0.09, 100)], [(0.10, 100), (0.11, 100)])
    repriced = book([(0.60, 100)], [(0.61, 100), (0.62, 100)])
    e.submit(buy(0.11, 10), now=0.0)
    assert e.step(0.0, lambda _: stale) == []            # matched, awaiting survival
    assert e.step(1000.0, lambda _: repriced) == []      # still inside the window
    res = e.step(2000.0, lambda _: repriced)
    assert len(res) == 1 and res[0].reject_reason is RejectReason.QUOTE_PULLED
    assert res[0].order.state is OrderState.REJECTED
    assert e.fills == [] and e.balance == 100.0
    assert e.latency_report()["rejected_quote_pulled"] == 1.0


def test_a_quote_that_survives_the_window_fills_at_the_arrival_price():
    e = ex(REALISTIC)
    b = book([(0.09, 100)], [(0.10, 100), (0.11, 100)])
    e.submit(buy(0.11, 10), now=0.0)
    e.step(0.0, lambda _: b)
    res = e.step(2000.0, lambda _: b)
    assert len(res) == 1 and not res[0].is_rejected
    f = res[0].fills
    assert len(f) == 1 and f[0].price == 0.10 and f[0].size == 10.0
    assert f[0].exchange_ts == 0.0                        # the fill happened on arrival
    assert e.positions[T].shares == 10.0
    assert e.balance == pytest.approx(99.0)


def test_partial_survival_counts_by_cumulative_size():
    """We took 150 across 0.10 and 0.11; later only 120 is left at or below 0.11."""
    e = ex(REALISTIC)
    before = book([], [(0.10, 100), (0.11, 100)])
    after = book([], [(0.10, 20), (0.11, 100)])
    e.submit(buy(0.11, 150), now=0.0)
    e.step(0.0, lambda _: before)
    res = e.step(2000.0, lambda _: after)
    assert res[0].reject_reason is RejectReason.QUOTE_PULLED


def test_a_better_book_still_counts_as_survival():
    """More size at a better price is not a pulled quote."""
    e = ex(REALISTIC)
    before = book([], [(0.10, 100)])
    after = book([], [(0.09, 300)])
    e.submit(buy(0.11, 10), now=0.0)
    e.step(0.0, lambda _: before)
    res = e.step(2000.0, lambda _: after)
    assert not res[0].is_rejected and res[0].fills[0].price == 0.10


def test_sells_are_held_to_the_same_rule():
    """A stop-loss sent into a bid that was already gone does not fill."""
    e = ex(REALISTIC)
    e.positions[T].token_id = T
    e.positions[T].shares = 10.0
    e.positions[T].cost_basis = 4.5
    before = book([(0.30, 100)], [(0.31, 100)])
    after = book([(0.08, 100)], [(0.09, 100)])
    e.submit(sell(0.30, 10), now=0.0)
    e.step(0.0, lambda _: before)
    res = e.step(2000.0, lambda _: after)
    assert res[0].reject_reason is RejectReason.QUOTE_PULLED
    assert e.positions[T].shares == 10.0


def test_a_gtc_whose_marketable_part_vanished_rests_instead():
    e = ex(REALISTIC)
    before = book([], [(0.10, 100)])
    after = book([(0.05, 100)], [(0.60, 100)])
    e.submit(buy(0.11, 10, tif=TimeInForce.GTC), now=0.0)
    e.step(0.0, lambda _: before)
    res = e.step(2000.0, lambda _: after)
    assert not res[0].is_rejected and res[0].fills == []
    assert res[0].order.state is OrderState.RESTING


def test_optimistic_fills_on_arrival_as_before():
    e = ex(OPTIMISTIC)
    e.submit(buy(0.11, 10), now=0.0)
    res = e.step(0.0, lambda _: book([], [(0.10, 100)]))
    assert len(res) == 1 and res[0].fills[0].price == 0.10


# ─────────────────────────────── other knobs ──────────────────────────────────


def test_random_venue_rejects_are_seeded_and_counted():
    sc = ExecutionScenario("flaky", reject_prob=0.5)
    outcomes = []
    for seed in range(2):
        e = ex(sc, seed=seed)
        got = []
        for i in range(40):
            e.submit(buy(0.11, 1), now=float(i))
            got.extend(r.reject_reason for r in e.step(float(i), lambda _: book([], [(0.10, 1000)])))
        outcomes.append(got)
        assert 5 < got.count(RejectReason.VENUE_REJECT) < 35
        assert e.latency_report()["rejected_venue_reject"] == got.count(RejectReason.VENUE_REJECT)
    e1, e2 = ex(sc, seed=0), ex(sc, seed=0)
    for i in range(10):
        e1.submit(buy(0.11, 1), now=float(i)); e2.submit(buy(0.11, 1), now=float(i))
        a = [r.reject_reason for r in e1.step(float(i), lambda _: book([], [(0.10, 1000)]))]
        b = [r.reject_reason for r in e2.step(float(i), lambda _: book([], [(0.10, 1000)]))]
        assert a == b


def test_depth_haircut_and_contention_override():
    sc = ExecutionScenario("thin", depth_haircut=0.5, contention=0.5)
    e = ex(sc)
    # 100 displayed -> 50 believed -> 25 after contention at the touch: a 30-share FOK fails
    e.submit(buy(0.11, 30), now=0.0)
    res = e.step(0.0, lambda _: book([], [(0.10, 100)]))
    assert res[0].reject_reason is RejectReason.FOK_UNFILLABLE
    e.submit(buy(0.11, 20), now=1.0)
    res = e.step(1.0, lambda _: book([], [(0.10, 100)]))
    assert not res[0].is_rejected and res[0].fills[0].size == 20.0


def test_latency_multiplier_scales_the_draws():
    from troll_poly_bot.execution.latency import VPS_NEARBY
    base = PaperExchange(LatencyModel(VPS_NEARBY, seed=3), FeeModel(rate=0.0), scenario=OPTIMISTIC)
    slow = PaperExchange(LatencyModel(VPS_NEARBY, seed=3), FeeModel(rate=0.0),
                         scenario=ExecutionScenario("slow", latency_multiplier=3.0))
    for i in range(50):
        base.submit(buy(0.11, 1), now=0.0)
        slow.submit(buy(0.11, 1), now=0.0)
    assert sum(slow._submit_latencies) == pytest.approx(3.0 * sum(base._submit_latencies))


def test_the_live_bot_defaults_to_the_realistic_scenario(tmp_path):
    from troll_poly_bot.live import LiveBot
    bot = LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"))
    assert bot.exchange.scenario is REALISTIC
    assert bot.snapshot()["execution_scenario"] == "realistic"
    assert bot.snapshot()["orders"]["scenario"] == "realistic"


# ───────────────── calibrated: survival judged against the LIMIT (2026-10-01) ─────────────────


def test_calibrated_fills_a_tick_up_inside_the_limit_at_the_worse_price():
    """Matched 0.70 with a 0.73 limit; 1 s later the ask is 0.71. The venue would have filled us inside
    the limit, so this is a fill -- charged at 0.71, the worse of the two books. REALISTIC kills it."""
    assert CALIBRATED.quote_survival_ms == 1000.0 and CALIBRATED.survive_to_limit
    arrive = book([(0.69, 100)], [(0.70, 100)])
    later = book([(0.70, 100)], [(0.71, 100)])
    e = ex(CALIBRATED)
    e.submit(buy(0.73, 5), now=0.0)
    e.step(0.0, lambda _: arrive)
    res = e.step(1000.0, lambda _: later)
    assert len(res) == 1 and not res[0].is_rejected
    assert res[0].fills[0].price == pytest.approx(0.71) and res[0].fills[0].exchange_ts == 0.0
    old = ex(REALISTIC)
    old.submit(buy(0.73, 5), now=0.0)
    old.step(0.0, lambda _: arrive)
    assert old.step(2000.0, lambda _: later)[0].reject_reason is RejectReason.QUOTE_PULLED


def test_calibrated_keeps_the_arrival_price_when_the_book_did_not_get_worse():
    arrive = book([], [(0.70, 100)])
    later = book([], [(0.69, 100)])
    e = ex(CALIBRATED)
    e.submit(buy(0.73, 5), now=0.0)
    e.step(0.0, lambda _: arrive)
    res = e.step(1000.0, lambda _: later)
    assert res[0].fills[0].price == pytest.approx(0.70)


def test_calibrated_kills_an_ask_that_left_the_limit():
    arrive = book([], [(0.70, 100)])
    later = book([], [(0.74, 100)])
    e = ex(CALIBRATED)
    e.submit(buy(0.73, 5), now=0.0)
    e.step(0.0, lambda _: arrive)
    res = e.step(1000.0, lambda _: later)
    assert res[0].reject_reason is RejectReason.QUOTE_PULLED and e.fills == []


def test_calibrated_needs_the_whole_size_inside_the_limit():
    arrive = book([], [(0.70, 100)])
    later = book([], [(0.72, 3), (0.80, 100)])
    e = ex(CALIBRATED)
    e.submit(buy(0.73, 5), now=0.0)
    e.step(0.0, lambda _: arrive)
    assert e.step(1000.0, lambda _: later)[0].reject_reason is RejectReason.QUOTE_PULLED
