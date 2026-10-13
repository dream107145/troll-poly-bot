"""The repriced-momentum RESEARCH tooling (scripts/research_repriced_momentum.py,
scripts/analyze_repriced_momentum.py): what the conclusions in
docs/repriced_momentum_research_2026-09-24.md rest on.

* no feature can see the future (a synthetic window whose future is rewritten must
  produce identical features up to the rewrite);
* returns are causal;
* the period split keeps the never-examined data apart;
* a fill exactly at the limit counts (the float32 regression that understated fills);
* maker exits fill only when the bid trades THROUGH the resting price;
* the stress re-simulation scales every latency draw and nothing else;
* the dynamic limit and the one-entry-per-shock rule.
"""
from __future__ import annotations

import math
import pathlib
import sys

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import analyze_repriced_momentum as A          # noqa: E402
import research_repriced_momentum as R         # noqa: E402

from troll_poly_bot.backtest.spot_shock_replay import FEATURE_COLS, Execution, Window   # noqa: E402

T0 = 1_800_000_000_000.0
CLOSE = T0 + 300_000.0
TS = T0 + 100_000.0                     # the shock


def _synthetic(tmp_path, rewrite_after: float | None = None) -> None:
    """One 5-minute window, 200 ms points: a 6-sigma spot shock at TS that keeps going,
    the model probability and the Up token following it. ``rewrite_after``: from then on
    the spot collapses and the token and its book change -- the FUTURE is different."""
    t = np.arange(T0 + 1000.0, CLOSE - 1000.0, 200.0)
    bps = np.where(t < TS - 800, 0.0, np.where(t <= TS, 6.0 * (t - (TS - 800)) / 800.0, 6.0 + 2.0 * (t - TS) / 1000.0))
    p = np.where(t < TS - 800, 0.5, 0.5 + 0.1 * np.minimum(1.0, (t - (TS - 800)) / 1000.0))
    mid = np.where(t < TS, 0.5, 0.5 + 0.08 * np.minimum(1.0, (t - TS) / 1500.0))
    dep = np.full(len(t), 100.0)
    if rewrite_after is not None:
        f = t > rewrite_after
        bps = np.where(f, bps - 15.0, bps)
        p = np.where(f, 0.3, p)
        mid = np.where(f, 0.42, mid)
        dep = np.where(f, 7.0, dep)
    spot = 100.0 * np.exp(bps * 1e-4)
    cols = {"t": t, "left": (CLOSE - t) / 1000.0, "spot": spot, "sigma": np.full(len(t), 1e-4), "p": p,
            "ub": mid - 0.005, "ua": mid + 0.005, "ubd": dep, "uad": dep,
            "db": 1 - mid - 0.005, "da": 1 - mid + 0.005, "dbd": dep, "dad": dep,
            "tfi": np.full(len(t), 0.3), "obi": np.full(len(t), 0.1), "orate": np.full(len(t), 5.0),
            "bn": spot, "byb": spot * 1.0001, "cb": spot * 0.9999}
    assert set(cols) == set(FEATURE_COLS)
    meta = np.array([("tst-updown-5m-1800000000", 1800000000, "train", T0, CLOSE, 1, 0, len(t), 0.07)], dtype=object)
    np.savez_compressed(tmp_path / "TST.npz", **cols, meta=meta)


def test_no_feature_sees_the_future(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "CACHE", tmp_path)
    cut = TS + 2000.0
    _synthetic(tmp_path)
    a = R.build_asset(("TST", "spotlag"))
    _synthetic(tmp_path, rewrite_after=cut)
    b = R.build_asset(("TST", "spotlag"))
    ka, kb = a["t"] <= cut, b["t"] <= cut
    assert ka.sum() >= 5 and np.array_equal(a["t"][ka], b["t"][kb])
    np.testing.assert_array_equal(a["F"][ka], b["F"][kb])          # every decision-time feature, bit for bit
    # ... while what happened AFTER did change: the outcomes are not features
    assert not np.array_equal(np.nan_to_num(a["M"][ka]), np.nan_to_num(b["M"][kb]))


def test_returns_are_causal():
    t = np.arange(0.0, 10_000.0, 200.0)
    x = np.cumsum(np.random.default_rng(1).normal(size=len(t)))
    y = x.copy()
    y[30:] += 50.0                                   # rewrite the future from index 30
    for lag in (250.0, 500.0, 1000.0):
        np.testing.assert_array_equal(R._ret(t, x, lag)[:30], R._ret(t, y, lag)[:30])


def test_the_never_examined_period_is_kept_apart():
    old = list(range(1790091000, R.FRESH_AFTER + 1, 300))
    new = list(range(R.FRESH_AFTER + 300, R.FRESH_AFTER + 300 * 20, 300))
    sp = R.split_fn(old + new)
    assert all(sp[e] == "unseen_3" for e in new)
    assert {sp[e] for e in old} == {"train", "validation", "unseen_1", "unseen_2"}
    order = [sp[e] for e in sorted(old)]
    assert order == sorted(order, key=["train", "validation", "unseen_1", "unseen_2"].index)   # chronological
    # adding later data never moves an earlier epoch
    assert all(R.split_fn(old)[e] == sp[e] for e in old)


def _dataset(tmp_path, ask: float, arr: float, px: float, flag: float = 0.0) -> A.DS:
    n = 1
    F = np.zeros((n, len(R.FEATURES)), dtype=np.float32)
    F[0, R.FEATURES.index("ask")] = ask
    F[0, R.FEATURES.index("rate")] = 0.07
    X = np.full((n, len(R.EXITS), 3), np.nan, dtype=np.float32)
    X[0, R.EXITS.index("t2")] = (px, 2.0, flag)
    np.savez_compressed(tmp_path / "dataset_x.npz", F=F, M=np.zeros((n, len(R.HORIZONS)), np.float32),
                        B=np.zeros((n, len(R.HORIZONS)), np.float32), O=np.zeros((n, len(R.FWD_OTHER)), np.float32),
                        E=np.array([[200.0, 300.0, arr, 1.0, 1.0]]), X=X, split=np.zeros(n, np.int8),
                        epoch=np.array([1], np.int64), shock=np.array([1], np.int64), asset=np.zeros(n, np.int8),
                        rank=np.zeros(n, np.int16), t=np.array([T0]), features=np.array(R.FEATURES),
                        exits=np.array(R.EXITS), horizons=np.array(R.HORIZONS))
    return A.DS("x")


def test_a_fill_exactly_at_the_limit_counts(tmp_path, monkeypatch):
    """0.88 stored as float32 reads 0.8799999952: an arrival ask of exactly 0.88 must still
    count as filled at a 0.88 limit (this understated every fill rate once)."""
    monkeypatch.setattr(A, "DATA", tmp_path)
    ds = _dataset(tmp_path, ask=0.88, arr=0.88, px=0.90)
    r, net = ds.trade(np.array([0]), "t2")
    assert len(r) == 1
    assert net[0] == pytest.approx(0.90 - 0.88 - 0.07 * 0.88 * 0.12 - 0.07 * 0.90 * 0.10)
    ds = _dataset(tmp_path, ask=0.88, arr=0.89, px=0.90)             # the ask moved: FOK killed
    assert len(ds.trade(np.array([0]), "t2")[0]) == 0
    ds = _dataset(tmp_path, ask=0.88, arr=0.88, px=0.90, flag=1.0)   # a maker exit pays no exit fee
    assert ds.trade(np.array([0]), "t2")[1][0] == pytest.approx(0.02 - 0.07 * 0.88 * 0.12)


def test_stress_latency_scales_every_draw_and_nothing_else():
    base, scaled = Execution(seed=3), R.ScaledExecution(seed=3, scale=1.5)
    for _ in range(20):
        (p0, h0), (p1, h1) = base.latency(), scaled.latency()
        assert p1 == pytest.approx(1.5 * p0) and h1 == pytest.approx(1.5 * h0)
    assert scaled.proxy_ms == base.proxy_ms and scaled.size == base.size


def _flat_window(bid_path):
    n = len(bid_path)
    t = T0 + np.arange(n) * 100.0
    bid = np.array(bid_path, float)
    sv = {"bid": bid, "ask": bid + 0.01, "bdep": np.full(n, 100.0), "adep": np.full(n, 100.0), "mid": bid + 0.005,
          "mfv": np.full(n, 0.6)}
    return Window(slug="tst", epoch=1, split="train", close=T0 + 300_000.0, won_up=True, rate=0.07, t=t,
                  left=np.full(n, 200.0), lp=np.zeros(n), sigma=np.full(n, 1e-4), side={"UP": sv}), sv


def test_a_maker_exit_needs_the_bid_to_trade_through():
    trig = {e: np.zeros(3000, bool) for e in R.EVENTS}
    entry = 0.50
    # the bid only REACHES entry + 1c: our resting sell there is not proven filled -> taker at 5 s
    w, sv = _flat_window([entry + 0.01] * 3000)
    X = np.full((len(R.EXITS), 3), np.nan, dtype=np.float32)
    R._exits(w, Execution(seed=1), sv, X, entry, T0 + 200.0, T0 + 400.0, 2999, 1.0, trig, np.full(3000, 0.6))
    assert X[R.EXITS.index("maker_1c"), 2] == 0.0
    # the bid trades THROUGH (entry + 2c) after 2 s: filled as maker at entry + 1c, no fee
    w, sv = _flat_window([entry] * 20 + [entry + 0.02] * 2980)
    X = np.full((len(R.EXITS), 3), np.nan, dtype=np.float32)
    R._exits(w, Execution(seed=1), sv, X, entry, T0 + 200.0, T0 + 400.0, 2999, 1.0, trig, np.full(3000, 0.6))
    px, hold, flag = X[R.EXITS.index("maker_1c")]
    assert flag == 1.0 and px == pytest.approx(entry + 0.01) and 0.0 < hold < 5.0


def test_the_dynamic_limit_is_the_highest_price_that_leaves_the_margin(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "DATA", tmp_path)
    ds = _dataset(tmp_path, ask=0.50, arr=0.50, px=0.50)
    ds.F[0, ds.fi["fair"]] = 0.60
    lim = A.dynamic_limit(ds, np.array([0]), margin=0.01, slip=0.005)[0]
    fee = lambda p: 0.07 * p * (1 - p)                                  # noqa: E731
    assert 0.60 - lim - fee(lim) - fee(0.60) - 0.005 >= 0.01 - 1e-9
    nxt = round(lim + 0.01, 2)
    assert 0.60 - nxt - fee(nxt) - fee(0.60) - 0.005 < 0.01


def test_one_entry_per_shock_at_its_first_qualifying_row(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "DATA", tmp_path)
    ds = _dataset(tmp_path, ask=0.5, arr=0.5, px=0.5)
    ds.shock = np.array([7, 7, 7, 9, 9])
    ds.rank = np.array([0, 1, 2, 0, 1])
    mask = np.array([False, True, True, True, True])
    assert list(ds.first(mask)) == [1, 3]


def test_ridge_recovers_a_planted_signal():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(4000, 5))
    y = 2.0 * X[:, 0] - 1.0 * X[:, 3] + rng.normal(scale=0.5, size=4000)
    m = A.Ridge(1e-3).fit(X, y)
    pred = m.predict(X)
    assert np.corrcoef(pred, y)[0, 1] > 0.95
    assert math.isclose(m.b, y.mean())
