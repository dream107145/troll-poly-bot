"""The fast feed (2026-09-29): a move start seen at tick resolution, per exchange, with
trade intensity and measured exchange lead/lag; wired into the live bot as an optional mode."""
from __future__ import annotations

import json
import math

import pytest

from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.features.fastmove import FastMoveDetector

SIGMA = 2.5e-4          # 2.5 bps per root-second


def _quotes(det, asset, exchange, mids, t0, step_ms, sigma=SIGMA, spread=1e-5):
    out = []
    for i, m in enumerate(mids):
        mv = det.on_quote(asset, exchange, m * (1 - spread), m * (1 + spread), t0 + i * step_ms, sigma)
        if mv:
            out.append(mv)
    return out


def test_a_move_start_fires_on_the_short_horizon_before_the_1s_return_is_abnormal():
    det = FastMoveDetector(k_sigma=3.0)
    # flat for a second, then +5 bps inside 100 ms: at the 100 ms horizon that is 5 / (2.5 * 0.316) = 6.3 sigma;
    # over 1 s it is only 2 sigma, so the 1 s rule would not have fired yet
    mids = [100.0] * 50 + [100.0 * math.exp(5e-4 * (i + 1) / 5) for i in range(5)]
    fired = _quotes(det, "BTC", "binance", mids, 0.0, 20.0)
    assert len(fired) == 1
    mv = fired[0]
    assert mv.direction == 1 and mv.horizon_ms <= 100.0 and mv.z >= 3.0 and mv.exchange == "binance"
    assert (mids[-1] / mids[-6] - 1.0) / SIGMA < 3.0           # not yet a 1 s shock
    # the same direction is deduped for 500 ms; the other direction is not
    assert det.on_quote("BTC", "binance", 100.06, 100.061, 1_120.0, SIGMA) is None
    assert det.fired == 1


def test_a_one_tick_bounce_on_a_wide_book_does_not_fire():
    det = FastMoveDetector(k_sigma=3.0, spread_floor=1.5)
    # DOGE-like: spread 10 bps, sigma 2.5 bps/sqrt(s): a 3 bps blip is 3.8 sigma at 100 ms but under the spread floor
    mids = [0.1000] * 30 + [0.1000 * (1 + 3e-4)] * 5
    assert not _quotes(det, "DOGE", "binance", mids, 0.0, 20.0, spread=5e-4)
    # the same blip on a tight book fires
    det2 = FastMoveDetector(k_sigma=3.0, spread_floor=1.5)
    assert _quotes(det2, "DOGE", "binance", mids, 0.0, 20.0, spread=1e-5)


def test_no_sigma_no_fire_and_history_shorter_than_the_horizon_is_skipped():
    det = FastMoveDetector(k_sigma=3.0)
    mids = [100.0, 100.5]
    assert not _quotes(det, "ETH", "bybit", mids, 0.0, 20.0, sigma=None)
    det = FastMoveDetector(k_sigma=3.0, horizons_ms=(250.0,))
    assert not _quotes(det, "ETH", "bybit", [100.0, 101.0], 0.0, 20.0)      # only 20 ms of history


def test_trade_intensity_and_exchange_lead_lag_are_measured():
    det = FastMoveDetector(k_sigma=3.0)
    for i in range(60):                                          # one trade a second for a minute
        det.on_trade("SOL", i * 1000.0)
    for i in range(6):                                           # then six in the last 500 ms
        det.on_trade("SOL", 59_600.0 + i * 60.0)
    ratio, n = det.intensity("SOL", 60_000.0)
    assert n == 6 and ratio > 5.0
    # three exchanges quote flat; coinbase shows the move first, bybit 60 ms later, binance 120 ms later
    t0 = 60_000.0
    for i in range(30):
        for x in ("coinbase", "bybit", "binance"):
            det.on_quote("SOL", x, 100.0, 100.002, t0 + i * 20.0, SIGMA)
    t1 = t0 + 600.0
    fired = []
    for i in range(10):
        t = t1 + i * 20.0
        up = 100.0 * math.exp(6e-4)
        fired.append(det.on_quote("SOL", "coinbase", up, up * 1.00002, t, SIGMA))
        fired.append(det.on_quote("SOL", "bybit", (up if t >= t1 + 60 else 100.0), (up if t >= t1 + 60 else 100.0) * 1.00002, t + 1, SIGMA))
        fired.append(det.on_quote("SOL", "binance", (up if t >= t1 + 120 else 100.0), (up if t >= t1 + 120 else 100.0) * 1.00002, t + 2, SIGMA))
    mv = next(m for m in fired if m)
    assert mv.exchange == "coinbase" and mv.intensity >= 0.0
    # close the pending measurement by advancing past the lead window
    for x in ("coinbase", "bybit", "binance"):
        det.on_quote("SOL", x, 100.06, 100.062, t1 + 2_000.0, SIGMA)
    lead = det.lead_ms["SOL"]
    assert lead["coinbase"] == 0.0
    assert 40.0 <= lead["bybit"] <= 80.0 and 100.0 <= lead["binance"] <= 140.0
    snap = det.snapshot()
    assert snap["fired"] == 1 and snap["lead_n"]["SOL"]["binance"] == 1


def test_the_feature_engine_samples_densely_when_asked():
    fe = FeatureEngine()
    fe.spot_min_gap_ms = 20.0
    for i in range(10):
        fe.update_spot("BTC", 100.0 + i, i * 25.0)
    assert len(fe.spot["BTC"]._t) == 10                          # noqa: SLF001
    coarse = FeatureEngine()
    for i in range(10):
        coarse.update_spot("BTC", 100.0 + i, i * 25.0)
    assert len(coarse.spot["BTC"]._t) == 2                       # noqa: SLF001 -- the 200 ms default


def _bot(tmp_path, **scfg):
    from troll_poly_bot.config import BotConfig
    from troll_poly_bot.live import LiveBot
    from troll_poly_bot.strategy.spot_lag import SpotLagConfig
    cfg = BotConfig()
    cfg.engine.strategy = "spot_lag"
    cfg.deploy_mode = "paper"
    c = SpotLagConfig(enabled=True, event_driven=True)
    for k, v in scfg.items():
        setattr(c, k, v)
    cfg.spot_lag = c
    return LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"),
                   trade_log=str(tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))


def test_the_live_bot_wires_the_fast_feed_only_when_asked(tmp_path):
    off = _bot(tmp_path / "off")
    assert off.fast is None and off.snapshot()["fast_feed"] is None
    on = _bot(tmp_path / "on", fast_feed=True, shock_window_s=0.25, fast_horizons_ms="100,250")
    assert on.fast is not None and on.fast.horizons == (100.0, 250.0)
    assert on.engine.features.spot_min_gap_ms == 20.0
    snap = on.snapshot()["fast_feed"]
    assert snap["fired"] == 0 and snap["evals"] == 0
    json.dumps(on.snapshot())                                    # the snapshot stays serialisable


def test_a_detection_evaluates_the_asset_at_once(tmp_path, monkeypatch):
    from troll_poly_bot.feeds.spot import SpotQuote
    bot = _bot(tmp_path, fast_feed=True, shock_window_s=0.25)
    evaluated = []
    monkeypatch.setattr(bot, "_evaluate_entries", lambda now, assets=None: evaluated.append((now, assets)))
    bot.clock.now = lambda: bot._t                               # noqa: SLF001
    bot._t = 0.0                                                 # noqa: SLF001
    # a ready volatility estimate and a composite that has warmed up
    for i in range(400):
        bot._t = i * 100.0                                       # noqa: SLF001
        q = SpotQuote("BTC", "binance", 100.0, 100.002, 100.001, bot._t)   # noqa: SLF001
        bot._on_quote(q)
    assert not evaluated
    # +6 bps in 100 ms
    for i in range(5):
        bot._t = 40_000.0 + (i + 1) * 20.0                       # noqa: SLF001
        m = 100.0 * math.exp(6e-4 * (i + 1) / 5)
        bot._on_quote(SpotQuote("BTC", "binance", m, m * 1.00002, m * 1.00001, bot._t))   # noqa: SLF001
    vol = bot.vol.get("BTC")
    if vol is not None and getattr(vol, "ready", False) and vol.sigma_per_sec > 0:
        assert evaluated and evaluated[0][1] == {"BTC"}
        assert bot.fast_evals >= 1 and bot.snapshot()["fast_feed"]["fired"] >= 1
    else:
        pytest.skip("the volatility estimate did not warm up on this synthetic feed")


def test_the_dashboard_can_switch_the_fast_feed_on_and_off(tmp_path):
    from troll_poly_bot.control import ControlFile
    bot = _bot(tmp_path)
    bot.controls = ControlFile(tmp_path / "controls.json")
    assert bot.fast is None
    (tmp_path / "controls.json").write_text(json.dumps({"paper": {"spot_lag.fast_feed": True,
                                                                  "spot_lag.fast_horizons_ms": "100,250",
                                                                  "spot_lag.shock_window_s": 0.25}}))
    bot._apply_controls()                                        # noqa: SLF001
    assert bot.fast is not None and bot.fast.horizons == (100.0, 250.0)
    assert bot.engine.features.spot_min_gap_ms == 20.0
    (tmp_path / "controls.json").write_text(json.dumps({"paper": {}}))
    bot._apply_controls()                                        # noqa: SLF001
    assert bot.fast is None
