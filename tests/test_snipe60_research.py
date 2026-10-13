"""The 60-second snipe research pipeline (scripts/research_60s_snipe.py): the vectorised fair value
equals the live one, no feature looks ahead, the FOK limit and the entry walk behave as specified."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import research_60s_snipe as S  # noqa: E402
from troll_poly_bot.backtest.momentum_replay import COLS, EXTRA_COLS, Window  # noqa: E402
from troll_poly_bot.backtest.spot_shock_replay import _make_vol  # noqa: E402
from troll_poly_bot.pricing.twap import TwapState, twap_fair_value  # noqa: E402
from troll_poly_bot.signals.costs import FeeSchedule  # noqa: E402

OPEN = 1_790_000_000_000.0
CLOSE = OPEN + 300_000.0


def synthetic_window(seed: int = 1, n_pre_s: float = 60.0) -> Window:
    rng = np.random.default_rng(seed)
    t = np.arange(OPEN - n_pre_s * 1000.0, CLOSE + 20_000.0, 213.0) + rng.uniform(0, 40, 1)[0]
    spot = 100.0 * np.exp(np.cumsum(rng.normal(0, 1.2e-4 * math.sqrt(0.213), len(t))))
    cols = {k: np.full(len(t), np.nan) for k in COLS + EXTRA_COLS}
    cols["spot"] = spot
    up = np.clip(0.5 + np.cumsum(rng.normal(0, 0.01, len(t))), 0.02, 0.98)
    cols["ub"], cols["ua"] = np.round(up - 0.005, 2), np.round(up + 0.005, 2)
    cols["db"], cols["da"] = np.round(1 - cols["ua"], 2), np.round(1 - cols["ub"], 2)
    for k in ("ubd", "uad", "dbd", "dad"):
        cols[k] = rng.uniform(5, 300, len(t))
    for k in ("tfi", "obi", "orate"):
        cols[k] = rng.normal(0, 1, len(t))
    strike = float(np.mean(spot[(t >= OPEN - 60_000) & (t <= OPEN)]))
    return Window(slug="btc-updown-5m-1790000000", asset="BTC", open_ts=OPEN, close_ts=CLOSE, up_won=True,
                  fee=FeeSchedule(), t=[float(x) for x in t], cols=cols, strike=strike)


def features(w: Window):
    sig, _ = S.window_sigma(np.asarray(w.t), np.asarray(w.cols["spot"]), _make_vol(), -1e18)
    return S.window_features(w, sig)


def test_vectorised_fair_value_equals_the_live_twap_fair_value():
    w = synthetic_window()
    t, x = np.asarray(w.t), np.log(np.asarray(w.cols["spot"]))
    idx = np.where(((CLOSE - t) / 1000.0 > 0) & ((CLOSE - t) / 1000.0 <= 182.0))[0]
    mean, u = S.twap_mean(t, x, CLOSE, idx)
    sig = 1.1e-4
    p_vec = S.t4_cdf((mean - math.log(w.strike)) / np.sqrt(S.twap_var(u, np.full(len(idx), sig))))
    st, k = TwapState(), 0
    for m, i in enumerate(idx):
        while k <= i:
            st.update(float(np.exp(x[k])), float(t[k]))
            k += 1
        live = twap_fair_value(st, w.strike, sig, float(t[i]), CLOSE, info_lag_s=S.INFO_LAG_S).p_up
        assert live == pytest.approx(p_vec[m], abs=1e-9)


def test_no_feature_looks_ahead():
    """Rewrite everything after a cut (spot and both books); every feature at or before the cut
    must come out bit-for-bit the same."""
    w = synthetic_window(seed=3)
    base, _ = features(w)
    t = np.asarray(w.t)
    for cut_left in (150.0, 60.0, 20.0, 3.0):
        cut = CLOSE - cut_left * 1000.0
        w2 = synthetic_window(seed=3)
        rng = np.random.default_rng(99)
        after = t > cut
        w2.cols["spot"][after] *= np.exp(rng.normal(0, 3e-3, after.sum()))
        for k in ("ub", "ua", "db", "da", "ubd", "uad", "dbd", "dad", "tfi", "obi", "orate"):
            w2.cols[k][after] = rng.uniform(0.01, 0.99, after.sum())
        got, _ = features(w2)
        keep = base["t"] <= cut
        assert keep.sum() > 10
        for k in S.COLS:
            np.testing.assert_array_equal(base[k][keep], got[k][:keep.sum()], err_msg=f"{k} changed before the cut")


def test_the_settlement_proxy_is_not_a_feature():
    """dclose reads the whole last minute: it is the basis model's fitting target, stored per
    window, never per point."""
    assert "dclose" not in S.COLS


def test_max_entry_is_the_highest_cent_that_keeps_the_edge():
    rng = np.random.default_rng(0)
    for _ in range(500):
        p, e, r = rng.uniform(0.02, 1.0), rng.choice([0.005, 0.01, 0.02, 0.05]), 0.07
        got = int(S.max_entry_c(np.array([p]), np.array([r]), e)[0])
        ok = [q for q in range(1, 100) if p - q / 100 - r * q / 100 * (1 - q / 100) - S.SLIP >= e - 1e-12]
        assert got == (max(ok) if ok else got)
        if ok:
            assert got == max(ok)


def fake_data(n_per: int = 20, windows: int = 2) -> S.Data:
    """A two-window Data without the cache: 20 points 250 ms apart per window."""
    D = object.__new__(S.Data)
    t = np.concatenate([CLOSE + k * 300_000.0 - 180_000.0 + 250.0 * np.arange(n_per) for k in range(windows)])
    D.W = windows
    D.w_close = CLOSE + 300_000.0 * np.arange(windows)
    D.w_start = np.arange(windows) * n_per
    D.w_end = D.w_start + n_per
    D.win = np.repeat(np.arange(windows), n_per)
    D.n = len(t)
    D.c = {"t": t, "ua_c": np.full(len(t), 50, np.int16), "da_c": np.full(len(t), 51, np.int16),
           "uad": np.full(len(t), 100.0), "dad": np.full(len(t), 100.0)}
    D.key = D.win * 1e9 + (t - D.w_close[D.win] + 5e5)
    return D


def test_walk_one_position_per_market_and_the_retry_throttle():
    D = fake_data()
    D.c["ua_c"][3:7] = 60                      # window 0: the ask jumps while the first order is in flight
    n = D.n
    ja = np.minimum(np.arange(n) + 2, n - 1)   # every order lands two points (500 ms) later
    O = S.Orders(1.0, 0.0, ja, D.c["t"] + 300.0, D.c["t"] + 400.0)
    from types import SimpleNamespace
    sig_points = np.array([1, 2, 3, 12, 13, 25, 27])
    C = SimpleNamespace(idx=sig_points, side=np.ones(len(sig_points), np.int8),
                        ask=np.full(len(sig_points), 50), edge=np.full(len(sig_points), 0.03),
                        p=np.full(len(sig_points), 0.6))
    pos = np.arange(len(sig_points))
    cap = np.full(len(pos), 52)
    od = S.walk(D, C, O, pos, cap, attempts=3, throttle_ms=2000.0)
    got = sorted(zip(C.idx[od["pos"]].tolist(), od["attempt"].tolist(), od["filled"].tolist()))
    # window 0: point 1 killed (ask 60 on arrival); points 2, 3 are inside the throttle
    # (kill known at t1 + 400 ms, next order >= 2 s later: point 11+); point 12 fills; 13 never sent.
    # window 1 (points 20-39): point 25 fills; 27 is not sent (one position per market)
    assert got == [(1, 1, False), (12, 2, True), (25, 1, True)]
    assert od["entry_c"][od["filled"]].tolist() == [50, 50]


def test_walk_kills_an_order_that_lands_after_the_close():
    D = fake_data()
    ja = np.full(D.n, -1)
    O = S.Orders(1.0, 0.0, ja, D.c["t"] + 300.0, D.c["t"] + 400.0)
    from types import SimpleNamespace
    C = SimpleNamespace(idx=np.array([5]), side=np.ones(1, np.int8), ask=np.array([50]),
                        edge=np.array([0.03]), p=np.array([0.6]))
    od = S.walk(D, C, O, np.array([0]), np.array([99]), attempts=1)
    assert od["filled"].tolist() == [False] and od["arr_ask"].tolist() == [-2]


def test_settlement_pnl_charges_only_the_entry_fee():
    res = np.full((len(S.EXITS), 4), np.nan)
    res[S.EXIT_IX["settle"]] = (1.0, 50.0, 2.0, np.nan)
    res[S.EXIT_IX["t5"]] = (0.97, 5.0, 0.0, 0.98)
    pnl = S.pnl_per_share(res, 0.95, 0.07)
    assert pnl[S.EXIT_IX["settle"]] == pytest.approx(1.0 - 0.95 - 0.07 * 0.95 * 0.05)
    assert pnl[S.EXIT_IX["t5"]] == pytest.approx(0.97 - 0.95 - 0.07 * 0.95 * 0.05 - 0.07 * 0.97 * 0.03)


def test_conservative_c_charges_the_loss_rate_the_sample_cannot_rule_out():
    wins = np.full(40, 0.04)
    assert S.conservative_c(wins) < 0 < S.conservative_c(np.full(400, 0.04))
    assert S.conservative_c(np.append(np.full(399, 0.04), -0.96)) < S.conservative_c(np.full(400, 0.04))


def test_clustered_standard_error_matches_the_formula():
    x = np.array([1.0, 2.0, 3.0, 4.0, 10.0, 0.0])
    g = np.array([1, 1, 2, 2, 3, 3])
    mu, se = S.clustered(x, g)
    s = np.array([(1 - mu) + (2 - mu), (3 - mu) + (4 - mu), (10 - mu) + (0 - mu)])
    assert mu == pytest.approx(x.mean())
    assert se == pytest.approx(math.sqrt(np.sum(s ** 2) * 3 / 2) / 6)


def test_forward_windows_never_join_an_established_period():
    eps = [1790091000 + 300 * k for k in range(0, 700)]
    sp = S.split_fn(eps)
    assert all(sp[e] == "forward" for e in eps if e > S.UNSEEN3_LAST)
    assert all(sp[e] != "forward" for e in eps if e <= S.UNSEEN3_LAST)
    assert {sp[e] for e in eps if e <= S.R.FRESH_AFTER} <= {"train", "validation", "unseen_1", "unseen_2"}


def test_zero_latency_take_profit_terminates_when_the_bid_is_too_thin():
    """Regression: at zero latency a killed take-profit sell re-read the same point forever."""
    D = fake_data(n_per=40, windows=1)
    D.w_split = np.zeros(1, np.int8)
    D.w_won = np.ones(1, np.int8)
    D.w_rate = np.full(1, 0.07)
    D.c.update({"ub_c": np.full(D.n, 60, np.int16), "db_c": np.full(D.n, 39, np.int16),
                "ubd": np.full(D.n, 1.0), "dbd": np.full(D.n, 100.0),          # target reachable, never 5 shares
                "lzs": np.full(D.n, 1e-3), "mu": np.full(D.n, 1e-3), "du1": np.zeros(D.n),
                "r1000": np.zeros(D.n), "sigma": np.full(D.n, 1e-4), "left": np.linspace(180, 170, D.n)})
    from types import SimpleNamespace
    M = SimpleNamespace(p={"e": np.full(D.n, 0.7)})
    X = S.Exits(D, M, scale=0.0, guard_ms=0.0)
    res = X.get(2, 1, 50, float(D.c["t"][2]), float(D.c["t"][2]), "e")
    assert res[S.EXIT_IX["tp1_settle"], 2] == 2.0          # never sold: settles
