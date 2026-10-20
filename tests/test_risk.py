"""Risk limits: caps bind, correlated bets shrink, losses halt, never size up after a loss."""
from __future__ import annotations

import pytest

from troll_poly_bot.risk.limits import RiskConfig, RiskManager


def _rm(**kw):
    return RiskManager(cfg=RiskConfig(**kw), starting_balance=100.0)


def test_no_edge_no_size():
    rm = _rm()
    shares, why = rm.size(p=0.5, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                          available_shares=1000)
    assert shares == 0.0 and "kelly" in why


def test_kelly_and_position_cap():
    rm = _rm(max_position_usdc=10.0)
    # f* = (0.9-0.5)/(0.5) = 0.8 -> stake 100*0.25*0.8 = 20 -> capped at 10 -> 20 shares
    shares, why = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                          available_shares=1000)
    assert shares == pytest.approx(20.0) and why == "position cap"


def test_confidence_scales_and_depth_caps():
    rm = _rm(max_position_usdc=100.0, max_total_exposure_usdc=100.0, max_asset_exposure_usdc=100.0,
             max_epoch_exposure_usdc=100.0, max_shares_per_order=1000)
    full, _ = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                      available_shares=1000)
    half, _ = rm.size(p=0.9, price=0.5, confidence=0.5, balance=100, asset="BTC", epoch=1, side="UP",
                      available_shares=1000)
    assert half == pytest.approx(full / 2)
    thin, _ = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                      available_shares=20)
    assert thin == pytest.approx(10.0)          # half the resting depth


def test_same_direction_in_epoch_is_scaled_by_correlation():
    rm = _rm(max_position_usdc=100.0, max_epoch_exposure_usdc=100.0, max_asset_exposure_usdc=100.0,
             max_total_exposure_usdc=100.0, epoch_correlation=0.5)
    alone, _ = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="ETH", epoch=7, side="UP",
                       available_shares=1000)
    rm.on_fill("btc-7", "BTC", 7, "UP", usdc=5.0, shares=10.0)
    with_peer, _ = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="ETH", epoch=7, side="UP",
                           available_shares=1000)
    assert with_peer == pytest.approx(alone / 2)


def test_exposure_caps_in_check():
    rm = _rm(max_epoch_exposure_usdc=8.0, max_position_usdc=10.0)
    rm.on_fill("btc-1", "BTC", 1, "UP", usdc=5.0, shares=10.0)
    assert rm.check("btc-1", "BTC", 1, 1.0).reason == "ALREADY_POSITIONED"
    v = rm.check("eth-1", "ETH", 1, 4.0)
    assert not v.ok and "epoch" in v.detail
    assert rm.check("eth-2", "ETH", 2, 4.0).ok


def test_daily_loss_and_drawdown_halt():
    rm = _rm(max_daily_loss_usdc=5.0, max_drawdown_usdc=100.0)
    rm.on_fill("a", "BTC", 1, "UP", 5.0, 10.0)
    rm.on_settle("a", -6.0)
    assert not rm.halted().ok and "daily" in rm.halted().detail
    # Drawdown vs 24h-ago balance (falls back to starting_balance), not peak.
    rm2 = _rm(max_daily_loss_usdc=100.0, max_drawdown_usdc=5.0)
    rm2.on_fill("a", "BTC", 1, "UP", 5.0, 10.0)
    rm2.on_settle("a", +10.0)
    rm2.on_fill("b", "BTC", 2, "UP", 5.0, 10.0)
    rm2.on_settle("b", -6.0)
    assert rm2.halted().ok
    assert rm2.drawdown == pytest.approx(-4.0)  # initial 100 → equity 104
    rm3 = _rm(max_daily_loss_usdc=100.0, max_drawdown_usdc=5.0)
    rm3.on_fill("a", "BTC", 1, "UP", 5.0, 10.0)
    rm3.on_settle("a", -6.0)  # equity 94 → drawdown 6
    assert not rm3.halted().ok and "drawdown" in rm3.halted().detail
    assert rm3.drawdown == pytest.approx(6.0)


def test_drawdown_uses_balance_24h_ago():
    import time as _time
    from troll_poly_bot.risk.limits import DRAWDOWN_LOOKBACK_S

    now = _time.time()
    rm = _rm(max_daily_loss_usdc=1000.0, max_drawdown_usdc=5.0)
    # Replace seed so the only sample 24h ago is 150; current marked is 140.
    rm._balance_history = [(now - DRAWDOWN_LOOKBACK_S, 150.0)]
    rm._last_balance_sample_ts = now - DRAWDOWN_LOOKBACK_S
    rm.note_balance(140.0, now_s=now)
    assert rm.balance_24h_ago(now_s=now) == pytest.approx(150.0)
    assert rm.drawdown == pytest.approx(10.0)  # 150 - 140
    assert not rm.halted(now_s=now).ok
    assert "drawdown" in rm.halted(now_s=now).detail
    # Before 24h of history, reference stays at initial bankroll.
    rm2 = _rm(max_daily_loss_usdc=1000.0, max_drawdown_usdc=50.0)
    rm2.note_balance(120.0, now_s=now)
    assert rm2.balance_24h_ago(now_s=now + 3600.0) == pytest.approx(100.0)
    assert rm2.current_balance == pytest.approx(120.0)
    assert rm2.balance_24h_ago(now_s=now) - rm2.current_balance == pytest.approx(-20.0)


def test_size_never_grows_after_a_loss():
    rm = _rm()
    before, _ = rm.size(p=0.8, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                        available_shares=1000)
    rm.on_fill("a", "BTC", 1, "UP", 5.0, 10.0)
    rm.on_settle("a", -5.0)
    after, _ = rm.size(p=0.8, price=0.5, confidence=1.0, balance=95, asset="BTC", epoch=2, side="UP",
                       available_shares=1000)
    assert after <= before
