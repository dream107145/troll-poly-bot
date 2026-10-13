"""The prefix-sum TWAP integrator (2026-09-29) answers exactly what the sample walk did."""
from __future__ import annotations

import math
import random

import pytest

from troll_poly_bot.pricing.twap import TwapState


def _walk(samples, a, b):
    """The original pair-by-pair integral, kept here as the oracle."""
    if b <= a or not samples:
        return 0.0, 0.0
    total = covered = 0.0
    for (t0, v0), (t1, v1) in zip(samples, samples[1:]):
        lo, hi = max(t0, a), min(t1, b)
        if hi <= lo:
            continue
        span = t1 - t0
        if span <= 0:
            continue
        f0 = v0 + (v1 - v0) * (lo - t0) / span
        f1 = v0 + (v1 - v0) * (hi - t0) / span
        total += 0.5 * (f0 + f1) * (hi - lo) / 1000.0
        covered += (hi - lo) / 1000.0
    t_last, v_last = samples[-1]
    if b > max(t_last, a):
        lo = max(t_last, a)
        total += v_last * (b - lo) / 1000.0
        covered += (b - lo) / 1000.0
    return total, covered


@pytest.mark.parametrize("seed", range(6))
def test_prefix_integral_matches_the_walk_on_every_kind_of_range(seed):
    rng = random.Random(seed)
    st = TwapState(window_s=60.0)
    t = 1_000_000.0
    px = 100.0
    for _ in range(3000):
        t += rng.choice([0.0, 5.0, 37.0, 120.0, 900.0])          # duplicates and gaps included
        px *= math.exp(rng.gauss(0.0, 1e-4))
        st.update(px, t)
    s = st._samples                                              # noqa: SLF001
    assert st._ordered and len(st._cum) == len(s)                # noqa: SLF001
    t0, t1 = s[0][0], s[-1][0]
    cases = [
        (t1 - 60_000.0, t1), (t1 - 60_000.0, t1 + 3_000.0),      # trailing window; held forward
        (t0 - 5_000.0, t0 + 10.0), (t0 - 5_000.0, t1 + 5_000.0),  # before the first sample
        (t1 + 100.0, t1 + 900.0),                                # entirely after the last sample
        (s[10][0], s[10][0] + 1.0), (s[10][0] - 1.0, s[10][0]),  # a and b inside one pair
        (s[20][0], s[25][0]), (s[20][0] + 0.5, s[25][0] - 0.5),  # on samples, and strictly between
        (t1, t1 + 1.0), (t1 - 1.0, t1),
    ]
    for _ in range(200):
        a = rng.uniform(t0 - 1000.0, t1 + 1000.0)
        cases.append((a, a + rng.uniform(0.0, 90_000.0)))
    for a, b in cases:
        want = _walk(s, a, b)
        got = st._integrate(a, b)                                # noqa: SLF001
        assert got[0] == pytest.approx(want[0], rel=1e-9, abs=1e-9), (a - t0, b - t0)
        assert got[1] == pytest.approx(want[1], rel=1e-9, abs=1e-9), (a - t0, b - t0)
        lo, hi = st.observed_range(a, b)
        vals = [v for tt, v in s if a <= tt <= b]
        assert (lo, hi) == ((min(vals), max(vals)) if vals else (None, None))


def test_samples_are_trimmed_to_twice_the_window_with_their_prefix_sums():
    st = TwapState(window_s=10.0)
    for i in range(500):
        st.update(100.0 + (i % 7), 1_000_000.0 + i * 100.0)
    assert len(st._samples) == len(st._cum)                      # noqa: SLF001
    assert st._samples[0][0] >= st._last_ts - 20_000.0           # noqa: SLF001
    assert st.trailing_twap(st._last_ts) == pytest.approx(103.0, abs=0.6)


def test_out_of_order_samples_fall_back_to_the_walk():
    st = TwapState(window_s=60.0)
    st.update(100.0, 2_000.0)
    st.update(101.0, 1_000.0)
    st.update(102.0, 3_000.0)
    assert not st._ordered                                       # noqa: SLF001
    assert st._integrate(0.0, 4_000.0) == _walk(st._samples, 0.0, 4_000.0)   # noqa: SLF001
