"""MICRO-LIVE: configuration from the environment, the smallest valid order, the decision journal."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.decisions import DecisionJournal, label_for
from troll_poly_bot.graduation import MicroCaps
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.strategy.engine import Evaluation, Reason, SideView
from troll_poly_bot.signals.costs import CostBreakdown


# ───────────────────────────────── caps ───────────────────────────────────────


def test_micro_caps_come_from_the_environment():
    m = MicroCaps.from_env({"TPB_MICRO_MAX_ORDER_USD": "2.5", "TPB_MICRO_MAX_ORDERS_PER_HOUR": "3",
                            "TPB_MICRO_MAKER_ENTRY": "0", "TPB_MICRO_MAX_SPREAD": "0.02"})
    assert m.max_order_usdc == 2.5 and m.max_orders_per_hour == 3 and m.maker_entry is False
    assert m.max_spread == 0.02 and m.max_open_usdc == MicroCaps().max_open_usdc
    junk = MicroCaps.from_env({"TPB_MICRO_MAX_ORDER_USD": "lots"})
    assert junk.max_order_usdc == MicroCaps().max_order_usdc


def test_micro_caps_apply_to_the_config_without_loosening_anything():
    cfg = BotConfig()
    cfg.engine.max_spread = 0.05
    cfg.risk.max_daily_loss_usdc = 20.0
    changed = MicroCaps().apply(cfg)
    assert cfg.engine.max_spread == 0.03 and cfg.engine.max_slippage == 0.02 and cfg.engine.maker_entry is True
    assert cfg.risk.max_concurrent_positions == 1 and cfg.risk.micro_fixed_size is True
    assert cfg.risk.max_daily_loss_usdc == 10.0 and cfg.risk.max_position_usdc == 5.0
    assert any("max_concurrent_positions" in c for c in changed)
    tight = BotConfig()
    tight.engine.max_spread = 0.01
    tight.risk.max_daily_loss_usdc = 2.0
    MicroCaps().apply(tight)
    assert tight.engine.max_spread == 0.01 and tight.risk.max_daily_loss_usdc == 2.0   # never loosened


# ─────────────────────────────── sizing ──────────────────────────────────────


def _risk(**kw):
    cfg = RiskConfig(micro_fixed_size=True, max_position_usdc=5.0, max_total_exposure_usdc=10.0, **kw)
    return RiskManager(cfg=cfg, starting_balance=50.0)


def test_smallest_valid_order_is_five_shares_or_one_dollar():
    r = _risk()
    shares, note = r.size(p=0.7, price=0.60, confidence=1.0, balance=50.0, asset="BTC", epoch=1, side="UP",
                          available_shares=200.0, min_size=5.0)
    assert shares == 5.0 and note == "micro minimum"
    shares, _ = r.size(p=0.3, price=0.10, confidence=1.0, balance=50.0, asset="BTC", epoch=1, side="UP",
                       available_shares=200.0, min_size=5.0)
    assert shares == 10.0                                  # 1 USDC of notional needs 10 shares at 0.10


def test_micro_size_never_grows_with_edge_or_balance():
    r = _risk()
    a, _ = r.size(p=0.99, price=0.50, confidence=1.0, balance=5000.0, asset="BTC", epoch=1, side="UP",
                  available_shares=1000.0, min_size=5.0)
    b, _ = r.size(p=0.55, price=0.50, confidence=0.1, balance=20.0, asset="BTC", epoch=1, side="UP",
                  available_shares=1000.0, min_size=5.0)
    assert a == b == 5.0


def test_micro_size_respects_caps_and_depth():
    r = _risk()
    shares, note = r.size(p=0.99, price=0.96, confidence=1.0, balance=50.0, asset="BTC", epoch=1, side="UP",
                          available_shares=1000.0, min_size=6.0)
    assert shares == 0.0 and "position cap" in note        # 6 x 0.96 = 5.76 > 5
    shares, note = r.size(p=0.7, price=0.5, confidence=1.0, balance=50.0, asset="BTC", epoch=1, side="UP",
                          available_shares=8.0, min_size=5.0)
    assert shares == 0.0 and "depth" in note


def test_no_kelly_edge_still_means_no_trade():
    r = _risk()
    shares, note = r.size(p=0.4, price=0.5, confidence=1.0, balance=50.0, asset="BTC", epoch=1, side="UP",
                          available_shares=100.0, min_size=5.0)
    assert shares == 0.0 and note == "no kelly edge"


# ───────────────────────────── decision journal ───────────────────────────────


def _ev(reason, slug="btc-updown-5m-1", net_edge=-0.01, tradeable=False):
    ev = Evaluation(slug=slug, asset="BTC", seconds_left=120.0, reason=reason)
    cb = CostBreakdown(model_p=0.55, market_p=0.5, ask=0.51, gross_edge=0.04, fee=0.0175, half_spread=0.005,
                       slippage=0.005, latency_cost=0.0, net_edge=net_edge)
    ev.sides = [SideView("UP", "Y", 0.55, 0.5, 0.51, 0.50, 120.0, cb, None if tradeable else reason),
                SideView("DOWN", "N", 0.45, 0.5, 0.50, 0.49, 90.0, None, Reason.EDGE_TOO_SMALL)]
    if tradeable:
        ev.side, ev.token_id, ev.limit_price, ev.size, ev.net_edge = "UP", "Y", 0.52, 5.0, net_edge
    return ev


def test_labels():
    assert label_for(None) == "SIGNAL_GENERATED"
    assert label_for("EDGE_TOO_SMALL") == "SIGNAL_REJECTED_LOW_EDGE"
    assert label_for("ALREADY_POSITIONED") == "SIGNAL_REJECTED_POSITION_LIMIT"
    assert label_for("SOMETHING_NEW") == "SIGNAL_REJECTED_OTHER"


def test_transitions_are_written_and_heartbeats_are_throttled(tmp_path):
    j = DecisionJournal(tmp_path / "d.jsonl", heartbeat_s=5.0, log_transitions=False)
    assert j.record(_ev(Reason.EDGE_TOO_SMALL), now_ms=0.0, mode="micro-live") is not None
    assert j.record(_ev(Reason.EDGE_TOO_SMALL), now_ms=1000.0, mode="micro-live") is None      # same reason, too soon
    assert j.record(_ev(Reason.EDGE_TOO_SMALL), now_ms=6000.0, mode="micro-live") is not None  # heartbeat
    assert j.record(_ev(Reason.LOW_LIQUIDITY), now_ms=6100.0, mode="micro-live") is not None   # transition
    row = j.record(_ev(None, net_edge=0.04, tradeable=True), now_ms=6200.0, mode="micro-live")
    assert row["decision"] == "SIGNAL_GENERATED" and row["side"] == "UP" and row["estimated_net_edge"] == 0.04
    assert row["market_price"] == 0.51 and row["spread"] == pytest.approx(0.01) and row["liquidity"] == 120.0
    s = j.summary()
    assert s["signals"] == 5 and s["rejected"] == {"LOW_EDGE": 3, "LIQUIDITY": 1}
    lines = [json.loads(l) for l in (tmp_path / "d.jsonl").read_text().splitlines()]
    assert len(lines) == 4


def test_quiet_reasons_are_counted_but_not_heartbeat_written(tmp_path):
    j = DecisionJournal(tmp_path / "d.jsonl", heartbeat_s=1.0, log_transitions=False)
    ev = Evaluation(slug="x", asset="BTC", seconds_left=299.0, reason=Reason.OUTSIDE_TIME_WINDOW)
    assert j.record(ev, now_ms=0.0, mode="paper") is not None       # first sight is a transition
    assert j.record(ev, now_ms=5000.0, mode="paper") is None         # no heartbeat for a non-signal
    assert j.summary()["signals"] == 0


def test_overrides_and_order_events(tmp_path):
    j = DecisionJournal(tmp_path / "d.jsonl", log_transitions=False)
    row = j.record(_ev(None, net_edge=0.04, tradeable=True), now_ms=0.0, mode="micro-live", override="GUARD",
                   detail="FEED_DOWN")
    assert row["decision"] == "SIGNAL_REJECTED_GUARD" and row["detail"] == "FEED_DOWN"
    j.order_event("ORDER_SUBMITTED", "o1", 1.0, market="m")
    j.order_event("ORDER_ACCEPTED", "o1", 2.0)
    j.order_event("ORDER_EXPIRED", "o1", 3.0)
    with pytest.raises(ValueError):
        j.order_event("ORDER_MAGIC", "o1", 4.0)
    assert j.summary()["orders"]["ORDER_EXPIRED"] == 1
    assert "submitted 1, accepted 1, filled 0" in j.report_text()


# ──────────────────────── working orders count as positions ───────────────────


def test_a_resting_entry_counts_toward_the_concurrent_cap():
    r = RiskManager(cfg=RiskConfig(max_concurrent_positions=1), starting_balance=50.0)
    assert r.check("btc-1", "BTC", 1, 2.0).ok
    r.on_submit("btc-1")
    v = r.check("eth-1", "ETH", 1, 2.0)
    assert not v.ok and "orders working" in v.detail
    assert r.check("btc-1", "BTC", 1, 2.0).ok              # the same market may re-check itself
    r.on_done("btc-1")
    assert r.check("eth-1", "ETH", 1, 2.0).ok
    r.on_submit("btc-1")
    r.on_fill("btc-1", "BTC", 1, "UP", 2.0, 5.0)
    assert r.pending == set() and not r.check("eth-1", "ETH", 1, 2.0).ok
    assert r.snapshot()["orders_working"] == 0


def test_late_paper_results_amend_a_closed_record(tmp_path):
    from troll_poly_bot.execution.forensics import TradeForensics, load_trades
    f = TradeForensics(tmp_path / "f.jsonl")
    f.open("e1", kind="entry", slug="s")
    f.stage("e1", "FULL_FILL", 1.0)
    f.stage("e1", "POSITION_CLOSED", 2.0)
    f.close("e1", 2.0, pnl_after_fees=0.1)
    f.amend("e1", 20.0, paper_outcome="REJECTED:expired")
    trades = load_trades(tmp_path / "f.jsonl")
    assert trades[0]["paper_outcome"] == "REJECTED:expired"
    assert f.closed[0]["paper_outcome"] == "REJECTED:expired"
