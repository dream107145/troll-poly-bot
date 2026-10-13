"""The decision journal keeps one mark per ORDER for the chart: sent, then filled or killed."""
from __future__ import annotations

from troll_poly_bot.execution.decisions import ORDER_MARKS_PER_MARKET, DecisionJournal


def _journal(tmp_path):
    return DecisionJournal(tmp_path / "decisions.jsonl", log_transitions=False)


def test_an_entry_order_is_marked_sent_then_killed(tmp_path):
    j = _journal(tmp_path)
    j.order_event("ORDER_CREATED", "o1", 1_000.0, market="btc-x", side="UP", price=0.41, size=10.0, tif="FOK")
    j.order_event("ORDER_SUBMITTED", "o1", 1_001.0, market="btc-x")
    [m] = j.order_marks_for("btc-x")
    assert m == {"id": "o1", "t": 1_000.0, "side": "UP", "kind": "entry", "limit": 0.41, "size": 10.0, "outcome": "sent"}
    j.order_event("ORDER_REJECTED", "o1", 1_900.0, reason="fok_unfillable", market="btc-x")
    [m] = j.order_marks_for("btc-x")
    assert (m["outcome"], m["reason"], m["t_done"]) == ("rejected", "fok_unfillable", 1_900.0)


def test_an_exit_order_keeps_its_kind_and_fill_price(tmp_path):
    j = _journal(tmp_path)
    j.order_event("ORDER_CREATED", "e1", 5_000.0, market="btc-x", side="UP", kind="TIME_STOP", price=0.55, size=10.0)
    j.order_event("ORDER_FILLED", "e1", 5_300.0, market="btc-x", kind="TIME_STOP", size=10.0, price=0.56)
    [m] = j.order_marks_for("btc-x")
    assert (m["kind"], m["outcome"], m["price"], m["filled"]) == ("TIME_STOP", "filled", 0.56, 10.0)
    # marks are copies: the caller cannot change the journal's record
    m["outcome"] = "x"
    assert j.order_marks_for("btc-x")[0]["outcome"] == "filled"


def test_marks_are_bounded_per_market_and_forgotten_with_it(tmp_path):
    j = _journal(tmp_path)
    for i in range(ORDER_MARKS_PER_MARKET + 5):
        j.order_event("ORDER_CREATED", f"o{i}", float(i), market="eth-x", side="DOWN", price=0.3, size=5.0)
    marks = j.order_marks_for("eth-x")
    assert len(marks) == ORDER_MARKS_PER_MARKET and marks[0]["id"] == "o5"
    j.order_event("ORDER_FILLED", "o0", 99.0, market="eth-x", side="DOWN", size=5.0, price=0.3)   # evicted: ignored
    assert all(m["outcome"] == "sent" for m in j.order_marks_for("eth-x"))
    j.forget("eth-x")
    assert j.order_marks_for("eth-x") == [] and not j._order_index
