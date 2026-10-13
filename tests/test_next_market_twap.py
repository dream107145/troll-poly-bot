"""NEXT_MARKET_TWAP: disabled by default, no look-ahead in the boundary features, the strike estimate,
the ask-based execution-aware edge, entry timing and gates, exits, VWAP and the log records."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy import next_market_twap as nm
from troll_poly_bot.strategy.next_market_twap import (
    NextMarketConfig, Quote, SpotView, boundary_features, displacement_variance, evaluate_entry, exit_reason,
    expected_net_edge, format_exit, format_prediction, predict,
)

OPEN = 1_790_000_100_000.0
FEE = FeeSchedule()
ON = NextMarketConfig(enabled=True)


def _series(seed: int = 3, start_s: float = -400.0, end_s: float = 60.0, sigma: float = 1e-4):
    """A random-walk composite and three exchange prices on a ~224 ms local clock."""
    rng = np.random.default_rng(seed)
    t = OPEN + np.cumsum(rng.uniform(150.0, 300.0, 4000)) + start_s * 1000.0
    t = t[t <= OPEN + end_s * 1000.0]
    lx = math.log(100.0) + np.cumsum(rng.normal(0.0, sigma * math.sqrt(0.224), len(t)))
    ex = {k: lx + rng.normal(0, 1e-5, len(t)) for k in nm.EXCHANGES}
    flow = {k: rng.normal(0, 1, len(t)) for k in ("tfi", "obi", "ofi", "rate")}
    flow["rate"] = np.abs(flow["rate"]) + 1.0
    return SpotView(t=t, lx=lx, ex=ex, flow=flow)


# ─────────────────────────────── safety ───────────────────────────────


def test_disabled_by_default_and_never_real_money():
    assert NextMarketConfig().enabled is False
    assert NextMarketConfig.from_env({}).enabled is False
    assert NextMarketConfig.from_env({"TPB_NEXT_MARKET_TWAP_ENABLED": "false"}).enabled is False
    assert NextMarketConfig.from_env({"TPB_NEXT_MARKET_TWAP_ENABLED": "1"}).enabled is True
    assert nm.REAL_MONEY is False
    assert nm.ENABLE_ENV == "TPB_NEXT_MARKET_TWAP_ENABLED"


def test_env_overrides_thresholds():
    cfg = NextMarketConfig.from_env({"TPB_NEXT_MARKET_TWAP_MIN_NET_EDGE": "0.03", "TPB_NEXT_MARKET_TWAP_HOLD_S": "4"})
    assert cfg.min_net_edge == 0.03 and cfg.hold_s == 4.0


def test_disabled_config_never_enters():
    v = _series()
    f = boundary_features(v, OPEN, OPEN - 3000.0, 1e-4)
    pred = predict("BTC", f, OPEN, OPEN - 3000.0, p_up=0.9)
    d = evaluate_entry(NextMarketConfig(), pred, OPEN, _quotes(), FEE)
    assert d.action == nm.DISABLED


# ─────────────────────────────── no look-ahead ───────────────────────────────


@pytest.mark.parametrize("offset_s", nm.PRED_OFFSETS_S)
def test_features_ignore_everything_after_the_prediction_time(offset_s):
    v = _series()
    pred = OPEN + offset_s * 1000.0
    f0 = boundary_features(v, OPEN, pred, 1e-4, prev_strike=99.9)
    assert f0 is not None
    later = v.t > pred
    v2 = SpotView(t=v.t.copy(), lx=v.lx.copy(), ex={k: a.copy() for k, a in v.ex.items()},
                  flow={k: a.copy() for k, a in v.flow.items()})
    v2.lx[later] += 0.05                                   # a 5 % jump that "has not happened yet"
    for a in list(v2.ex.values()) + list(v2.flow.values()):
        a[later] = -a[later] * 3.0
    f1 = boundary_features(v2, OPEN, pred, 1e-4, prev_strike=99.9)
    assert f0.keys() == f1.keys()
    for k in f0:
        a, b = f0[k], f1[k]
        assert (a == b) or (a != a and b != b), k


def test_a_point_stamped_after_the_prediction_is_not_read():
    v = _series()
    pred = OPEN - 3000.0
    j = int(np.searchsorted(v.t, pred, side="right"))
    t = v.t.copy()
    t[j] = pred + 1.0                                    # arrives 1 ms too late
    v2 = SpotView(t=t, lx=v.lx.copy(), ex=v.ex, flow=v.flow)
    v2.lx[j] += 0.01
    assert boundary_features(v, OPEN, pred, 1e-4)["x_now"] == boundary_features(v2, OPEN, pred, 1e-4)["x_now"]


# ─────────────────────────────── the strike and the displacement ───────────────────────────────


def test_strike_estimate_matches_the_trailing_twap_at_the_open():
    v = _series(seed=7)
    f = boundary_features(v, OPEN, OPEN + 1000.0, 1e-4, info_lag_s=0.0)     # the whole window is seen
    m = (v.t >= OPEN - 60_000.0) & (v.t <= OPEN)
    tt, xx = v.t[m], v.lx[m]
    # reference: trapezoid over the window with the edges interpolated
    i0 = np.searchsorted(v.t, OPEN - 60_000.0) - 1
    i1 = np.searchsorted(v.t, OPEN)
    ts = np.concatenate([[OPEN - 60_000.0], tt, [OPEN]])
    x0 = v.lx[i0] + (v.lx[i0 + 1] - v.lx[i0]) * (OPEN - 60_000.0 - v.t[i0]) / (v.t[i0 + 1] - v.t[i0])
    x1 = v.lx[i1 - 1] + (v.lx[i1] - v.lx[i1 - 1]) * (OPEN - v.t[i1 - 1]) / (v.t[i1] - v.t[i1 - 1])
    xs = np.concatenate([[x0], xx, [x1]])
    ref = np.sum(0.5 * (xs[1:] + xs[:-1]) * np.diff(ts)) / 1000.0 / 60.0
    assert f["lk_hat"] == pytest.approx(ref, abs=2e-7)          # well under 0.01 bps
    assert f["strike_seen_frac"] == pytest.approx(1.0)


def test_before_the_open_the_unseen_part_is_weighted_at_the_latest_price():
    v = _series(seed=9)
    pred = OPEN - 3000.0
    f = boundary_features(v, OPEN, pred, 1e-4, info_lag_s=0.0)
    # a price that stays flat from now on gives the same strike as the estimate
    n = int(np.searchsorted(v.t, pred, side="right"))
    t = np.concatenate([v.t[:n], OPEN - 3000.0 + np.arange(1, 40) * 100.0])
    lx = np.concatenate([v.lx[:n], np.full(39, v.lx[n - 1])])
    flat = SpotView(t=t, lx=lx)
    g = boundary_features(flat, OPEN, OPEN + 100.0, 1e-4, info_lag_s=0.0)
    assert f["lk_hat"] == pytest.approx(g["lk_hat"], abs=1e-7)
    assert f["strike_seen_frac"] == pytest.approx(57.0 / 60.0)


def test_displacement_variance():
    s = 1e-4
    assert displacement_variance(300.0, 0.0, s) == pytest.approx(s * s * 260.0)
    u, uk = 303.18, 3.18
    ref = s * s * ((u - 40.0) + uk ** 3 / 10800.0 - uk ** 2 / 120.0)
    assert displacement_variance(u, uk, s) == pytest.approx(ref)
    assert displacement_variance(u, uk, s) < s * s * (u - 40.0)


def test_up_move_into_the_open_favours_up():
    v = _series(seed=11, sigma=2e-5)
    n = int(np.searchsorted(v.t, OPEN - 3000.0, side="right"))
    lx = v.lx.copy()
    lx[n - 100:] += np.linspace(0.0, 0.003, len(lx) - (n - 100))    # a 30 bps rally into the prediction
    f = boundary_features(SpotView(t=v.t, lx=lx), OPEN, OPEN - 3000.0, 1e-4)
    assert f["disp"] > 0 and f["p_twap"] > 0.5
    p = predict("BTC", f, OPEN, OPEN - 3000.0)
    assert p.side == "UP" and p.predicted_twap > p.predicted_start
    assert p.p_up + p.p_down == pytest.approx(1.0)


def test_a_hole_in_the_strike_window_refuses_to_predict():
    v = _series()
    keep = ~((v.t > OPEN - 40_000.0) & (v.t < OPEN - 30_000.0))
    assert boundary_features(SpotView(t=v.t[keep], lx=v.lx[keep]), OPEN, OPEN - 3000.0, 1e-4) is None
    assert boundary_features(v, OPEN, OPEN - 3000.0, 0.0) is None


# ─────────────────────────────── execution-aware edge and entry gates ───────────────────────────────


def _quotes(ub=0.49, ua=0.51, db=0.49, da=0.51, depth=200.0):
    return {"UP": Quote(bid=ub, ask=ua, bid_depth=depth, ask_depth=depth),
            "DOWN": Quote(bid=db, ask=da, bid_depth=depth, ask_depth=depth)}


def _pred(p_up=0.7):
    return nm.Prediction(asset="BTC", open_ms=OPEN, pred_ms=OPEN - 3000.0, reference_price=100.0,
                         predicted_start=99.9, predicted_twap=100.0, predicted_displacement=0.1, p_up=p_up,
                         confidence=abs(2 * p_up - 1), disp_sd_bps=10.0)


def test_the_entry_is_priced_at_the_ask_and_the_exit_at_the_bid():
    e = expected_net_edge("UP", 0.70, Quote(bid=0.49, ask=0.55, ask_depth=500), FEE, 10.0, kappa=1.0, adverse=0.0)
    assert e.entry_vwap == 0.55                              # not the 0.52 mid
    assert e.settlement_edge == pytest.approx(0.15)
    # full convergence: the exit is the model price less the exit half spread
    assert e.expected_exit_bid == pytest.approx(0.70 - 0.01)
    assert e.net_if_filled == pytest.approx(0.69 - 0.55 - FEE.taker_fee_per_share(0.55) - FEE.taker_fee_per_share(0.69))
    e0 = expected_net_edge("UP", 0.70, Quote(bid=0.49, ask=0.55, ask_depth=500), FEE, 10.0, kappa=0.0, adverse=0.0)
    assert e0.net_if_filled < 0                              # no convergence: pay the spread and two fees


def test_settlement_hold_pays_one_fee():
    e = expected_net_edge("UP", 0.70, Quote(bid=0.49, ask=0.55, ask_depth=500), FEE, 10.0, kappa=0.0, adverse=0.0,
                          hold_to_settlement=True)
    assert e.fee_exit == 0.0
    assert e.net_if_filled == pytest.approx(0.70 - 0.55 - FEE.taker_fee_per_share(0.55))


def test_vwap_walks_the_three_level_book():
    assert nm.vwap_buy(0.50, 100.0, 20.0) == 0.50           # inside the 25-share touch
    assert nm.vwap_buy(0.50, 100.0, 50.0) == pytest.approx((25 * 0.50 + 25 * 0.51) / 50)
    assert nm.vwap_buy(0.50, 100.0, 100.0) == pytest.approx((25 * 0.50 + 37.5 * 0.51 + 37.5 * 0.52) / 100)
    assert nm.vwap_buy(0.50, 100.0, 101.0) is None
    assert nm.vwap_sell(0.50, 100.0, 50.0) == pytest.approx((25 * 0.50 + 25 * 0.49) / 50)
    assert nm.fillable(0.50, 100.0, 0.50) == pytest.approx(25.0)
    assert nm.fillable(0.50, 100.0, 0.51) == pytest.approx(62.5)


@pytest.mark.parametrize("offset_s, action", [(-0.001, nm.BEFORE_MARKET_START), (0.0, nm.ENTER),
                                              (0.001, nm.ENTRY_WINDOW_CLOSED)])
def test_entry_only_at_the_open_by_default(offset_s, action):
    cfg = NextMarketConfig(enabled=True, min_net_edge=-1.0)
    d = evaluate_entry(cfg, _pred(), OPEN + offset_s * 1000.0, _quotes(), FEE)
    assert d.action == action


def test_entry_window_extends_after_the_open():
    cfg = NextMarketConfig(enabled=True, min_net_edge=-1.0, entry_window_s=2.0)
    assert evaluate_entry(cfg, _pred(), OPEN + 2000.0, _quotes(), FEE).action == nm.ENTER
    assert evaluate_entry(cfg, _pred(), OPEN + 2001.0, _quotes(), FEE).action == nm.ENTRY_WINDOW_CLOSED


def test_entry_gates():
    cfg = NextMarketConfig(enabled=True, min_net_edge=-1.0)
    assert evaluate_entry(cfg, None, OPEN, _quotes(), FEE).action == nm.NO_PREDICTION
    assert evaluate_entry(NextMarketConfig(enabled=True, min_probability=0.8), _pred(0.7), OPEN, _quotes(), FEE).action \
        == nm.LOW_PROBABILITY
    assert evaluate_entry(cfg, _pred(), OPEN, _quotes(ub=0.35, ua=0.65), FEE).action == nm.ASK_OUT_OF_RANGE
    assert evaluate_entry(cfg, _pred(), OPEN, _quotes(ub=0.40, ua=0.45), FEE).action == nm.SPREAD_TOO_WIDE
    assert evaluate_entry(cfg, _pred(), OPEN, _quotes(ua=None), FEE).action == nm.NO_ASK
    assert evaluate_entry(cfg, _pred(), OPEN, _quotes(depth=12.0), FEE).action == nm.THIN_BOOK
    small = NextMarketConfig(enabled=True, min_net_edge=-1.0, order_usdc=1.0)
    assert evaluate_entry(small, _pred(), OPEN, _quotes(), FEE).action == nm.BELOW_MIN_SIZE
    moved = NextMarketConfig(enabled=True, min_net_edge=-1.0, max_quote_move=0.01)
    before = _quotes(ub=0.46, ua=0.48)
    assert evaluate_entry(moved, _pred(), OPEN, _quotes(), FEE, quotes_at_pred=before).action == nm.QUOTE_MOVED
    d = evaluate_entry(cfg, _pred(0.3), OPEN, _quotes(), FEE)
    assert d.action == nm.ENTER and d.side == "DOWN" and d.limit == 0.51


def test_edge_gate_uses_costs():
    tight = NextMarketConfig(enabled=True, min_net_edge=0.02, kappa=0.8)
    assert evaluate_entry(tight, _pred(0.52), OPEN, _quotes(), FEE).action == nm.EDGE_TOO_SMALL
    d = evaluate_entry(tight, _pred(0.75), OPEN, _quotes(), FEE)
    assert d.action == nm.ENTER and d.edge.net_if_filled >= 0.02


# ─────────────────────────────── exits ───────────────────────────────


def test_exit_rules():
    cfg = NextMarketConfig(hold_s=3.0, take_profit=0.03, stop_loss=0.02, max_hold_s=30.0)
    assert exit_reason(cfg, 1.0, 0.50, 0.51) is None
    assert exit_reason(cfg, 1.0, 0.50, 0.53) == nm.EXIT_TAKE_PROFIT
    assert exit_reason(cfg, 1.0, 0.50, 0.48) == nm.EXIT_STOP_LOSS
    assert exit_reason(cfg, 3.0, 0.50, 0.51) == nm.EXIT_TIME
    assert exit_reason(cfg, 30.0, 0.50, 0.51) == nm.EXIT_MAX_HOLD
    assert exit_reason(NextMarketConfig(hold_s=0), 1.0, 0.50, 0.60, p_side_now=0.6, fee=FEE) == nm.EXIT_EDGE_GONE
    assert exit_reason(NextMarketConfig(hold_s=0), 1.0, 0.50, 0.51, ret_1s_dir=-2e-4, sigma=1e-4) == nm.EXIT_MOMENTUM


# ─────────────────────────────── log records ───────────────────────────────


def test_log_records_carry_the_required_fields():
    d = evaluate_entry(NextMarketConfig(enabled=True, min_net_edge=-1.0), _pred(), OPEN, _quotes(), FEE)
    txt = format_prediction(_pred(), _quotes(), d, {"order_submit": 1, "fill": 2, "entry_price": 0.51})
    for key in ("NEXT_MARKET_PREDICTION", "asset=BTC", "prediction_time=", "(-3.0s)", "reference_price=",
                "predicted_start=", "predicted_twap=", "predicted_displacement=", "p_up=", "p_down=", "confidence=",
                "UP: bid=", "DOWN: bid=", "decision=UP", "expected_edge_before_costs=", "expected_edge_after_costs=",
                "entry_price=0.51"):
        assert key in txt, key
    ex = format_exit({"hold_time": 3.0, "entry_ask": 0.51, "current_bid": 0.53, "markout": 0.02, "fees": 0.035,
                      "slippage": 0.0, "net_pnl": -0.015, "exit_reason": "TIME"})
    assert ex.startswith("NEXT_MARKET_EXIT") and "exit_reason=TIME" in ex


# ─────────────────────────────── the research simulator's fills ───────────────────────────────


def _analysis():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import analyze_next_market_twap as A
    return A


def test_research_fok_buy_respects_the_limit_and_depth():
    A = _analysis()
    ask = np.array([0.50, 0.50, 0.52, np.nan, 0.50])
    dep = np.array([100.0, 100.0, 100.0, 100.0, 10.0])
    q, vw = A._fill_buy(ask, dep, np.array([0.50, 0.51, 0.51, 0.60, 0.52]), np.array([20.0, 50.0, 5.0, 5.0, 20.0]),
                        partial=False)
    assert list(q) == [20.0, 50.0, 0.0, 0.0, 0.0]       # moved above the limit / no quote / too thin
    assert vw[1] == pytest.approx((25 * 0.50 + 25 * 0.51) / 50)
    q, _ = A._fill_buy(ask[:1], dep[:1], np.array([0.50]), np.array([40.0]), partial=True)
    assert q[0] == pytest.approx(25.0)                   # FAK: the touch only


def test_research_sell_floor():
    A = _analysis()
    ok, vw = A._fill_sell(np.array([0.50, 0.50]), np.array([100.0, 100.0]), np.array([0.48, 0.505]),
                          np.array([60.0, 10.0]))
    assert list(ok) == [True, False]
    assert vw[0] == pytest.approx((25 * 0.50 + 35 * 0.49) / 60)
