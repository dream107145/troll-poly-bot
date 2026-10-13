"""Fast-move detection fixes (2026-09-30): the shock is judged against 1-second volatility, not the
two-scale sigma that prices the 5-minute outcome; lead mode dates stamped sources by their quote delay."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from troll_poly_bot.feeds.spot import SpotQuote
from troll_poly_bot.live import LiveBot
from troll_poly_bot.pricing.vol import EwmaVol, TwoScaleVol, shock_sigma
from troll_poly_bot.strategy.spot_lag import SpotLagConfig

T = 1_790_800_000_000.0


def _vol(fast_sigma: float, ratio: float) -> TwoScaleVol:
    v = TwoScaleVol(fast=EwmaVol(grid_ms=1000.0, halflife_s=120.0), slow=EwmaVol(grid_ms=30_000.0, halflife_s=3600.0))
    v.fast._n, v.fast._var = 10_000, fast_sigma ** 2        # warmed up
    v._ratio = ratio
    return v


def test_shock_sigma_is_the_one_second_scale_not_the_two_scale_product():
    v = _vol(0.5e-4, 2.0)
    assert v.sigma_per_sec == pytest.approx(1.0e-4)          # what prices the 5-minute outcome
    assert shock_sigma(v) == pytest.approx(0.5e-4)            # what a 1-second move is judged against
    # a plain estimator, or a two-scale one still warming up, falls back to its own sigma
    plain = SimpleNamespace(sigma_per_sec=0.7e-4, ready=True)
    assert shock_sigma(plain) == pytest.approx(0.7e-4)
    cold = TwoScaleVol()
    assert shock_sigma(cold) == pytest.approx(cold.sigma_per_sec)


def test_a_3_sigma_move_under_a_high_ratio_is_now_a_shock(tmp_path):
    cfg_env = {"TPB_SPOT_LAG_ENABLED": "1"}
    sc = SpotLagConfig.from_env(cfg_env)
    assert sc.shock_sigma_1s is False                        # the tested rule by default (2026-10-01 audit)
    sc.shock_sigma_1s = True
    from troll_poly_bot.config import BotConfig
    cfg = BotConfig()
    cfg.engine.strategy = "spot_lag"
    cfg.spot_lag = sc
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"))
    v = _vol(0.5e-4, 2.0)
    # a 1.6 bp move in a second: 3.2 one-second sigmas, 1.6 two-scale sigmas
    assert bot._shock_sigma(v) == pytest.approx(0.5e-4)
    assert 1.6e-4 / bot._shock_sigma(v) >= sc.k_sigma
    sc.shock_sigma_1s = False                                  # the dashboard switch restores the old rule
    assert bot._shock_sigma(v) == pytest.approx(1.0e-4)
    assert 1.6e-4 / bot._shock_sigma(v) < sc.k_sigma


def test_lead_mode_dates_a_stamped_source_by_its_quote_delay(tmp_path):
    bot = LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"))
    bot.clock.now = lambda: T
    bot.composite.warmup_ms = 0.0
    q = SpotQuote("BTC", "deribit", 83999.5, 84000.5, 84000.0, T, T - 30.0)
    bot._on_quote(q)
    assert bot.composite.lag["deribit"] == pytest.approx(30.0)
    # its batched trade stream reads 200 ms: no longer allowed to overwrite the quote delay
    bot._on_source_lag("deribit", 200.0)
    assert bot.composite.lag["deribit"] == pytest.approx(30.0)
    # a source whose quotes carry no time (Binance bookTicker) is still dated by its trades
    bot._on_source_lag("binance", 150.0)
    assert bot.composite.lag["binance"] == pytest.approx(150.0)


# ───────────────────────── the fair-value trigger ─────────────────────────
# the engine harness of test_spot_lag: a market AT the strike, 180 s left, sigma 1 bp/s

from test_spot_lag import _cfg, _shock                    # noqa: E402

from troll_poly_bot.strategy.engine import Reason         # noqa: E402


def test_a_2_sigma_move_at_the_strike_is_a_signal_only_with_the_fair_value_trigger():
    _, _, off = _shock(2.0)                                           # z1 = 2: no sigma shock
    assert off.reason is Reason.WEAK_MOMENTUM
    _, _, on = _shock(2.0, scfg=_cfg(fv_trigger_cents=3.0))
    sl = on.features["spot_lag"]
    assert sl["trigger"] == "fv" and sl["fv_move_c"] >= 3.0 and sl["z1"] < 3.0
    assert on.reason is not Reason.WEAK_MOMENTUM, (on.reason, on.detail)
    assert on.features["momentum"]["label"] == "SHOCK_UP" and on.side in (None, "", "UP")
    # the later gates still decide: a token that already repriced is not chased on this path either
    _, _, down = _shock(-2.0, scfg=_cfg(fv_trigger_cents=3.0))
    assert down.features["spot_lag"]["trigger"] == "fv" and down.features["momentum"]["label"] == "SHOCK_DOWN"


def test_a_fair_value_move_under_the_threshold_is_not_a_signal():
    _, _, ev = _shock(2.0, scfg=_cfg(fv_trigger_cents=40.0))
    assert ev.reason is Reason.WEAK_MOMENTUM and ev.features["spot_lag"]["trigger"] == "sigma"


def test_a_sigma_shock_still_follows_the_sigma_rule_with_the_trigger_on():
    _, _, ev = _shock(6.0, scfg=_cfg(fv_trigger_cents=3.0))
    assert ev.features["spot_lag"]["trigger"] == "sigma" and ev.tradeable, (ev.reason, ev.detail)


def test_the_quote_level_precheck_fires_on_a_small_move_near_the_strike(tmp_path):
    from troll_poly_bot.config import BotConfig
    from troll_poly_bot.features.engine import SpotHistory
    from troll_poly_bot.strategy.spot_lag import SpotLagConfig
    cfg = BotConfig()
    cfg.engine.strategy = "spot_lag"
    cfg.spot_lag = SpotLagConfig(enabled=True, event_driven=True, fv_trigger_cents=10.0)
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"))
    now = T
    strike = 84_000.0
    m = SimpleNamespace(asset="BTC", strike=strike, close_ts=now + 180_000.0)
    bot.markets = {"btc-updown-5m-x": SimpleNamespace(meta=SimpleNamespace(market=m), settled=False)}
    hist = SpotHistory()
    hist.update(strike, now - 1000.0)
    vol = SimpleNamespace(sigma_per_sec=1e-4, ready=True)
    # sigma*sqrt(140 s) = 11.8 bp; at the strike a 3 bp move is ~10c of fair value
    assert bot._fv_precheck("BTC", strike * (1 + 3e-4), hist, vol, now)
    assert not bot._fv_precheck("BTC", strike * (1 + 0.5e-4), hist, vol, now)          # ~1.7c
    # far from the strike the same move barely changes the fair value
    m.strike = strike * (1 - 40e-4)
    assert not bot._fv_precheck("BTC", strike * (1 + 3e-4), hist, vol, now)
    cfg.spot_lag.fv_trigger_cents = 0.0                                                   # off
    m.strike = strike
    assert not bot._fv_precheck("BTC", strike * (1 + 3e-4), hist, vol, now)


# ───────────────────────────── the trend filter ─────────────────────────────

def test_the_trend_filter_refuses_a_shock_against_the_prior_trend():
    # the harness feeds a flat minute then the shock: the 30 s trend before the shock is 0 bps
    _, _, ev = _shock(6.0, scfg=_cfg(trend_window_s=30.0, trend_min_bps=1.0))
    assert ev.reason is Reason.WEAK_MOMENTUM and "trend" in (ev.detail or "")
    assert ev.features["spot_lag"]["trend_bps"] == pytest.approx(0.0, abs=0.01)
    _, _, ok = _shock(6.0, scfg=_cfg(trend_window_s=30.0, trend_min_bps=0.0))      # 0 bps >= 0: allowed
    assert ok.tradeable, (ok.reason, ok.detail)
    _, _, off = _shock(6.0, scfg=_cfg(trend_window_s=0.0, trend_min_bps=5.0))     # window 0 = off
    assert off.tradeable and "trend_bps" not in off.features["spot_lag"]


# ───────────────────────────── shock marks ─────────────────────────────

def test_every_detection_is_marked_with_whether_it_became_a_signal():
    e, _, ev = _shock(6.0)                                                   # a clean signal
    (mk,) = e.shock_marks_for("btc-updown-5m-1")
    assert mk["dir"] == 1 and mk["ok"] is True and mk["why"] == "" and mk["z1"] >= 3.0 and mk["t"] == int(NOW_T())
    e2, _, ev2 = _shock(6.0, scfg=_cfg(trend_window_s=30.0, trend_min_bps=1.0))   # detected, then skipped by the trend
    (mk2,) = e2.shock_marks_for("btc-updown-5m-1")
    assert mk2["ok"] is False and "trend" in mk2["why"] and mk2["trend_bps"] == pytest.approx(0.0, abs=0.01)
    e3, _, ev3 = _shock(2.0)                                                   # no detection: no mark
    assert e3.shock_marks_for("btc-updown-5m-1") == []


def NOW_T():
    from test_spot_lag import NOW
    return NOW
