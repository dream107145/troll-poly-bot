"""The paired-exit engine's Polymarket-book signal (TPB_TRIANGLE_PX_SIGNAL_SOURCE, 2026-10-02): the live book
features equal the study's (scripts/book_signal/bs_lib.py), the live trigger equals the replay's candidates, and the
engine signals from the book only when asked to."""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import numpy as np
import pytest

from test_paired_exit import _model_dir, _peval, _pengine, _points
from test_spot_lag import NOW, SLUG, T0, _meta

from troll_poly_bot.strategy import paired_exit as px

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "paired_exit"))
sys.path.insert(0, str(ROOT / "scripts" / "book_signal"))


def _book_points(n=400, seed=11):
    """Both books moving in ticks with random near-touch depth, missing quotes and feed gaps."""
    rng = random.Random(seed)
    pts, t, mid = [], T0 + 1000.0, 0.50
    for k in range(n):
        t += 200.0 + rng.uniform(-20, 20) + (900.0 if k % 61 == 30 else 0.0)        # an occasional gap
        mid = min(0.9, max(0.1, mid + rng.choice((-0.01, 0.0, 0.0, 0.0, 0.01))))
        su, sd = rng.choice((0.01, 0.01, 0.02)), rng.choice((0.01, 0.02))
        p = {"t": t, "spot": 100.0, "ua": round(mid + su, 2), "ub": round(mid, 2),
             "da": round(1 - mid + sd, 2), "db": round(1 - mid - 0.01, 2),
             "uad": rng.choice((0.0, 5.0, 40.0, 300.0)), "ubd": rng.choice((10.0, 80.0, 250.0)),
             "dad": rng.choice((5.0, 60.0, 400.0)), "dbd": rng.choice((0.0, 20.0, 200.0))}
        if k % 47 == 5:
            p["ua"] = None
        if k % 53 == 9:
            p["db"] = None
        if k % 59 == 13:
            p["dbd"] = None
        pts.append(p)
    return pts


def _win(pts):
    import pe_lib as L
    w = L.Win()
    w.T = np.array([p["t"] for p in pts], float)
    w.n = len(pts)
    for k in ("ua", "ub", "da", "db", "uad", "ubd", "dad", "dbd"):
        setattr(w, k, np.array([np.nan if p.get(k) is None else p[k] for p in pts], float))
    return w


def test_live_book_features_equal_the_study_features_at_every_sample():
    import bs_lib as B
    pts = _book_points()
    F = B.book_features(_win(pts))
    assert tuple(B.BOOK_FEATS) == px.BOOK_FEATS
    for i in range(len(pts)):
        G = px.book_features_at(pts, i)
        for k in px.BOOK_FEATS:
            a, b = F[k][i], G[k]
            assert math.isnan(a) == math.isnan(b), (k, i, a, b)
            if not math.isnan(a):
                assert a == pytest.approx(b, abs=1e-9), (k, i)


def test_the_live_book_model_scores_like_the_study():
    import bs_lib as B
    pts = _book_points(seed=5)
    rng = np.random.default_rng(1)
    doc = {"features": list(B.BOOK_FEATS), "mu": rng.normal(0, 0.2, len(B.BOOK_FEATS)).tolist(),
           "sd": rng.uniform(0.5, 2.0, len(B.BOOK_FEATS)).tolist(), "w": rng.normal(0, 0.5, len(B.BOOK_FEATS) + 1).tolist()}
    want = B.predict(doc, B.design(B.book_features(_win(pts))))
    m = px.BookModel(doc)
    for i in range(0, len(pts), 7):
        assert m.score(px.book_features_at(pts, i)) == pytest.approx(want[i], abs=1e-9)


@pytest.mark.parametrize("source", ["crypto", "book", "both", "either"])
def test_the_live_trigger_fires_on_the_replays_candidates(source):
    import pe_lib as L
    import sim as S
    rng = np.random.default_rng(3)
    n = 1500
    w = L.Win()
    w.T = T0 + 200.0 * np.arange(n) + rng.uniform(-20, 20, n)
    w.n, w.close = n, T0 + 300_000.0
    w.left = (w.close - w.T) / 1000.0
    z1 = rng.normal(0, 1.2, n)
    z1[rng.random(n) < 0.03] = np.nan
    book = rng.normal(0, 0.9, n)
    book[rng.random(n) < 0.02] = np.nan
    w.F = {"z1": z1, "book": book}
    prm = px.PxParams(momentum_threshold=2.0, book_threshold=1.5, book_confirm=0.5, book_confirm_ms=1000.0)
    want = S.candidates(w, 2.0, 60.0, 290.0, source, 1.5, 0.5, 1000.0)
    got, sig, recent = [], px.Signals(), []
    for i in range(n):
        recent.append((w.T[i], book[i] if not math.isnan(book[i]) else 0.0))
        pick = px.pick_signal(source, z1[i], book[i], prm, [r for r in recent if w.T[i] - r[0] <= 1000.0], w.T[i])
        if pick is None or not (60.0 <= w.left[i] <= 290.0):
            continue
        if sig.take("s", pick[0], w.T[i]):
            got.append((i, pick[0]))
    assert got == want and len(want) > 10


def _book_dir(tmp_path):
    """The fair model of test_paired_exit + a book model whose score is vel1 (the UP mid's last-sample move, c)."""
    d = Path(_model_dir(tmp_path))
    w = [0.0] * (len(px.BOOK_FEATS) + 1)
    w[1 + px.BOOK_FEATS.index("vel1")] = 1.0
    (d / "book_btc.json").write_text(json.dumps({"features": list(px.BOOK_FEATS), "mu": [0.0] * len(px.BOOK_FEATS),
                                                 "sd": [1.0] * len(px.BOOK_FEATS), "w": w}))
    return str(d)


def _feed(e, book_jump=0.0, spot_jump_bp=0.0):
    """One window ending at NOW; spot quiet (sigma 5 bp) unless spot_jump_bp; the last sample moves both books."""
    m = _meta("BTC").market
    e.px_sigma["BTC"] = 5.0
    pts = _points(n=595, jump_at_end_bp=spot_jump_bp, t0=T0)
    shift = NOW - pts[-1]["t"]
    for p in pts:
        p["t"] += shift
        p["ua"], p["ub"], p["da"], p["db"] = 0.46, 0.44, 0.56, 0.54
    last = pts[-1]
    last["ua"], last["ub"] = round(0.46 + book_jump, 2), round(0.44 + book_jump, 2)
    last["da"], last["db"] = round(0.56 - book_jump, 2), round(0.54 - book_jump, 2)
    for p in pts:
        e.observe_point(SLUG, m, p, p["t"])


def test_crypto_is_the_default_and_ignores_the_book(tmp_path):
    e = _pengine(tmp_path, px_model_dir=_book_dir(tmp_path))
    assert e.px.signal_source == "crypto"
    _feed(e, book_jump=0.03)
    assert SLUG not in e.px_pending and e.px_last[SLUG]["book"] is None


def test_book_mode_signals_from_the_book_alone_and_buys_on_the_ev_gate(tmp_path):
    e = _pengine(tmp_path, px_model_dir=_book_dir(tmp_path), px_signal_source="book", px_book_threshold=1.5)
    _feed(e, book_jump=0.03)
    sig = e.px_pending[SLUG]
    assert sig["dir"] == 1 and sig["trigger"] == "book" and sig["book"] == pytest.approx(3.0)
    assert abs(sig["z1"]) < 2.0                                    # spot said nothing
    ev = _peval(e, up=(0.39, 0.40))                                # fair ~0.45: 5c under it
    assert ev.tradeable, (ev.reason, ev.detail)
    info = ev.features["triangle"]
    assert ev.side == "UP" and info["source"] == "book" and info["book"] == pytest.approx(3.0)
    e2 = _pengine(tmp_path, px_model_dir=_book_dir(tmp_path), px_signal_source="book", px_book_threshold=1.5)
    _feed(e2, book_jump=-0.03)
    assert e2.px_pending[SLUG]["dir"] == -1
    e3 = _pengine(tmp_path, px_model_dir=_book_dir(tmp_path), px_signal_source="book", px_book_threshold=1.5)
    _feed(e3, book_jump=0.01)                                      # 1c: under the threshold
    assert SLUG not in e3.px_pending


def test_both_needs_the_book_to_confirm_the_crypto_move(tmp_path):
    e = _pengine(tmp_path, px_model_dir=_book_dir(tmp_path), px_signal_source="both", px_book_confirm=1.0)
    _feed(e, spot_jump_bp=25.0)                                    # crypto fires, the book is flat
    assert SLUG not in e.px_pending
    e2 = _pengine(tmp_path, px_model_dir=_book_dir(tmp_path), px_signal_source="both", px_book_confirm=1.0)
    _feed(e2, spot_jump_bp=25.0, book_jump=0.02)
    assert e2.px_pending[SLUG]["dir"] == 1 and e2.px_pending[SLUG]["trigger"] == "both"


def test_either_takes_crypto_first_then_the_book(tmp_path):
    e = _pengine(tmp_path, px_model_dir=_book_dir(tmp_path), px_signal_source="either", px_book_threshold=1.5)
    _feed(e, book_jump=0.03)
    assert e.px_pending[SLUG]["trigger"] == "book"
    e2 = _pengine(tmp_path, px_model_dir=_book_dir(tmp_path), px_signal_source="either", px_book_threshold=1.5)
    _feed(e2, spot_jump_bp=25.0, book_jump=-0.03)                  # both fire, opposite ways: crypto decides
    assert e2.px_pending[SLUG]["trigger"] == "crypto" and e2.px_pending[SLUG]["dir"] == 1


def test_a_missing_book_model_means_no_book_signals_and_a_bad_source_refuses_to_start(tmp_path):
    e = _pengine(tmp_path, px_signal_source="book")                # the model dir has no book_btc.json
    _feed(e, book_jump=0.03)
    assert SLUG not in e.px_pending and e.px_book_models["BTC"] is None
    with pytest.raises(ValueError):
        _pengine(tmp_path, px_signal_source="orderbook")


def test_the_live_book_model_file_is_the_studys_format():
    path = ROOT / "data" / "replay" / "paired_exit" / "live" / "book_btc.json"
    if not path.exists():
        pytest.skip("no live book model in this checkout")
    m = px.BookModel.load(str(path))
    assert m.feats == px.BOOK_FEATS and len(m.w) == len(px.BOOK_FEATS) + 1
