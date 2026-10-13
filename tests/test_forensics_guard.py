"""Trade forensics and the guard."""
from __future__ import annotations

import json

import pytest

from troll_poly_bot.execution.forensics import STAGES, TradeForensics, load_trades
from troll_poly_bot.risk.guard import Guard, GuardConfig


# ───────────────────────────────── forensics ──────────────────────────────────


def test_every_stage_is_timestamped_and_consolidated(tmp_path):
    f = TradeForensics(tmp_path / "f.jsonl", strategy_version="v1", mode="test")
    f.open("o1", slug="btc-updown-5m-1", side="UP", decision_price=0.50, signal_probability=0.6,
           market_probability=0.5, edge=0.05)
    f.stage("o1", "SIGNAL", 1000.0)
    f.stage("o1", "DECISION", 1010.0)
    f.stage("o1", "ORDER_SUBMITTED", 1020.0)
    f.stage("o1", "ORDER_ACKNOWLEDGED", 1320.0)
    f.stage("o1", "FULL_FILL", 1320.0, execution_price=0.51, fees=0.02, quantity=10.0)
    f.stage("o1", "POSITION_OPEN", 1320.0)
    f.bind_position("TOK", "o1")
    assert f.entry_for("TOK") == "o1"
    f.stage("o1", "SETTLED", 200_000.0)
    rec = f.close("o1", 200_000.0, reported_pnl=4.88, pnl=4.90, fees=0.02, outcome="UP")
    assert rec["latency_ms"] == 300.0 and rec["fill_latency_ms"] == 300.0
    assert rec["position_duration_s"] == pytest.approx(198.7, abs=0.06)
    assert rec["realized_slippage"] == pytest.approx(0.01)
    assert rec["pnl_after_fees"] == pytest.approx(4.88) and rec["won"] is True
    assert rec["pnl_mismatch"] is False and rec["strategy_version"] == "v1"
    assert f.entry_for("TOK") is None
    trades = load_trades(tmp_path / "f.jsonl")
    assert len(trades) == 1 and trades[0]["order_id"] == "o1"
    lines = [json.loads(l) for l in (tmp_path / "f.jsonl").read_text().splitlines()]
    assert sum(1 for l in lines if l["event"] == "stage") == 7


def test_a_loss_after_fees_is_never_reported_as_a_win(tmp_path):
    f = TradeForensics(tmp_path / "f.jsonl")
    f.open("o2")
    rec = f.close("o2", 1.0, reported_pnl=0.01, pnl=0.01, fees=0.05)
    assert rec["pnl_after_fees"] == pytest.approx(-0.04)
    assert rec["won"] is False and rec["pnl_mismatch"] is True
    assert f.pnl_mismatches == 1


def test_unknown_stage_is_refused_and_disk_errors_are_counted(tmp_path):
    f = TradeForensics(tmp_path / "missing" / "dir" / "f.jsonl")
    with pytest.raises(ValueError):
        f.stage("x", "NOT_A_STAGE", 1.0)
    f2 = TradeForensics(tmp_path)          # a directory: cannot be opened for append
    f2.stage("x", "SIGNAL", 1.0)
    assert f2.write_errors == 1
    assert set(STAGES) >= {"SIGNAL", "ORDER_SUBMITTED", "FULL_FILL", "SETTLED"}


def test_closing_twice_or_unknown_is_a_no_op(tmp_path):
    f = TradeForensics(tmp_path / "f.jsonl")
    assert f.close("nope", 1.0) is None
    f.open("o3")
    assert f.close("o3", 1.0) is not None
    assert f.close("o3", 2.0) is None


# ─────────────────────────────────── guard ────────────────────────────────────


def _feeds(ok=True, silence=0.5):
    return {"binance": {"connected": ok, "last_msg_age_s": silence, "last_error": "" if ok else "boom"}}


def test_guard_allows_entries_when_everything_is_healthy():
    g = Guard()
    assert g.update(100.0, feeds=_feeds(), data_age_ms=500.0, rtt_p95_ms=400.0) == {}
    assert g.entries_allowed


def test_feed_down_trips_and_clears_after_cooldown():
    g = Guard(GuardConfig(cooldown_s=30.0))
    assert "FEED_DOWN" in g.update(100.0, feeds=_feeds(ok=False))
    assert not g.entries_allowed and g.trips_total == 1
    assert "COOLDOWN" in g.update(110.0, feeds=_feeds())
    assert g.update(141.0, feeds=_feeds()) == {}
    assert g.entries_allowed


def test_silent_feed_stale_data_and_latency_trip():
    g = Guard(GuardConfig(max_feed_silence_s=5.0, max_data_age_ms=2000.0, max_order_rtt_p95_ms=1000.0, cooldown_s=0.0))
    assert "FEED_SILENT" in g.update(1.0, feeds=_feeds(silence=6.0))
    assert "STALE_DATA" in g.update(2.0, feeds=_feeds(), data_age_ms=2500.0)
    assert "LATENCY" in g.update(3.0, feeds=_feeds(), rtt_p95_ms=1500.0)


def test_execution_failure_rate_trips_on_a_run_of_kills():
    g = Guard(GuardConfig(kill_window=10, min_orders_for_kill_rate=8, max_kill_rate=0.8, cooldown_s=0.0))
    for _ in range(7):
        g.record_order_outcome(False)
    assert g.kill_rate() is None                       # not enough orders yet
    assert g.update(1.0) == {}
    g.record_order_outcome(False)
    assert g.kill_rate() == 1.0
    assert "EXECUTION_FAILURE_RATE" in g.update(2.0)
    for _ in range(10):
        g.record_order_outcome(True)
    assert g.update(3.0) == {}


def test_latching_conditions_survive_recovery():
    g = Guard(GuardConfig(max_balance_drift_usdc=1.0, cooldown_s=0.0))
    assert "BALANCE_DRIFT" in g.update(1.0, balance_drift_usdc=-2.5)
    assert "BALANCE_DRIFT" in g.update(2.0, balance_drift_usdc=0.0)    # still latched
    assert "UNKNOWN_POSITION" in g.update(3.0, unknown_positions=["tok"])
    assert "UNKNOWN_POSITION" not in g.update(3.5, unknown_positions=[])      # clears with the venue
    assert "PNL_MISMATCH" in g.update(4.0, pnl_mismatches=1)
    assert set(g.latched) == {"BALANCE_DRIFT", "PNL_MISMATCH"}
    snap = g.snapshot()
    assert snap["entries_allowed"] is False and snap["latched"]


def test_consecutive_losses_and_venue_halt():
    g = Guard(GuardConfig(max_consecutive_losses=3, cooldown_s=0.0))
    assert g.update(1.0, consecutive_losses=2) == {}
    assert "CONSECUTIVE_LOSSES" in g.update(2.0, consecutive_losses=3)
    assert "VENUE_HALTED" in g.update(3.0, venue_halted="daily loss")


def test_disabled_guard_never_trips():
    g = Guard(GuardConfig(enabled=False))
    assert g.update(1.0, feeds=_feeds(ok=False), unknown_positions=["x"]) == {}
    assert g.entries_allowed
