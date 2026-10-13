"""The pair-lock engine's Polymarket-book entry trigger (strategy/pairlock_booksignal.py, 2026-10-02): the four
signal sources against the paired-exit study's pick_signal semantics, and the strategy-B engine entering from the
book with no crypto shock, preferring crypto in "either", confirming in "both", ignoring crypto in "book"."""
from __future__ import annotations

import math

import pytest
from test_flowsignal import _result
from test_pairlock_engine_v2 import TRI, _ev, _fill, _model
from test_spot_lag import NOW, SLUG, T0, _meta
from test_triangle import _cfg

from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.strategy import paired_exit as px
from troll_poly_bot.strategy import pairlock_v2 as pl
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.pairlock_book_v2 import State
from troll_poly_bot.strategy.pairlock_booksignal import BookTrigger, BookTriggerParams
from troll_poly_bot.strategy.pairlock_engine_v2 import PairLockConfig, PairLockEngineV2

LIVE_MODEL = "/root/troll-poly-bot/data/replay/paired_exit/live/book_btc.json"
VIEW_SPOT = 100.0


class _Stub:
    """A book model whose score is whatever the test says (set ``value`` between samples)."""
    meta = {"stub": True}

    def __init__(self, value=0.0):
        self.value = value

    def score(self, F):
        return self.value


def _point(t, up=(0.50, 0.52)):
    return {"t": t, "spot": VIEW_SPOT, "ub": up[0], "ua": up[1], "db": round(1 - up[1], 2), "da": round(1 - up[0], 2),
            "ubd": 100.0, "uad": 100.0, "dbd": 100.0, "dad": 100.0}


def _trigger(source, value=0.0, **kw):
    return BookTrigger(BookTriggerParams(signal_source=source, **kw), model=_Stub(value))


def _engine(source, threshold=1.5, value=0.0, model=None, **kw):
    scfg = _cfg(**TRI)
    rc = RiskConfig()
    scfg.apply_budget(rc)
    plc = PairLockConfig(model_path="/nonexistent.json",
                         params=pl.PairLockParams(entry_edge=0.0, band_lo=0.20, band_hi=0.96),
                         book=BookTriggerParams(signal_source=source, book_threshold=threshold,
                                                book_model_path=LIVE_MODEL, **kw))
    e = PairLockEngineV2(EngineConfig(strategy="triangle"), scfg, RiskManager(cfg=rc, starting_balance=500.0),
                         FeatureEngine(), plcfg=plc)
    e.plmodel = model or _model(1.0)                      # P(UP) ~0.74 at mid 0.51: the entry gate passes
    from test_pairlock_engine_v2 import VIEW, _book
    for t in range(int(NOW - 12_000), int(NOW), 200):
        e._record(_meta("BTC"), _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50), VIEW, float(t))   # noqa: SLF001
    e.sigma[SLUG] = 1e-4
    if e.booksig is not None:
        e.booksig.model = _Stub(value)
    return e


def _observe(e, now, value=None, n=3):
    """A few 200 ms samples up to ``now`` (the live bot's _sample_prices -> observe_point)."""
    if value is not None and e.booksig is not None:
        e.booksig.model.value = value
    for k in range(n, 0, -1):
        e.observe_point(SLUG, _meta("BTC").market, _point(now - 200.0 * k), now - 200.0 * k)


# ─────────────────────────── the trigger alone ───────────────────────────


def test_the_sources_follow_the_study_pick_signal_semantics():
    m = _meta("BTC").market
    prm = px.PxParams(momentum_threshold=2.0, book_threshold=1.5, book_confirm=0.0, book_confirm_ms=1000.0)
    for source in ("book", "either", "both"):
        for z1, score in [(0.0, 0.3), (0.0, 1.5), (0.0, -2.2), (2.5, 0.0), (2.5, -3.0), (-2.5, -0.5), (2.5, 1.0),
                          (-2.5, 2.0), (1.0, 1.6)]:
            tr = _trigger(source, score)
            t = T0 + 5000.0
            tr.observe(SLUG, m, _point(t), t)
            crypto_dir = (1 if z1 > 0 else -1) if abs(z1) >= 2.0 else None
            got = tr.take(SLUG, t, crypto_dir)
            want = px.pick_signal(source, z1, score, prm, [(t, score)], t)
            assert (None if got is None else got[:2]) == want, (source, z1, score, got, want)


def test_one_book_signal_per_direction_per_second_and_stale_ones_are_dropped():
    m = _meta("BTC").market
    tr = _trigger("book", 3.0, book_max_age_ms=500.0)
    t = T0 + 5000.0
    for k in range(5):                                        # 5 samples in one second: one signal
        tr.observe(SLUG, m, _point(t + 200.0 * k), t + 200.0 * k)
    assert tr.n_signals == 1 and tr.n_scored == 5
    assert tr.take(SLUG, t + 1300.0, None) is None and tr.n_stale == 1    # 1.3 s old: not traded
    tr.observe(SLUG, m, _point(t + 1400.0), t + 1400.0)                   # a new second: a new signal
    assert tr.take(SLUG, t + 1500.0, None) == (1, "book", 3.0)
    assert tr.take(SLUG, t + 1500.0, None) is None                        # consumed


def test_params_are_validated_and_a_missing_model_raises():
    with pytest.raises(ValueError):
        BookTriggerParams(signal_source="spot")
    with pytest.raises(ValueError):
        BookTriggerParams(book_threshold=0.0)
    with pytest.raises(OSError):
        BookTrigger(BookTriggerParams(signal_source="book", book_model_path="/nonexistent/book.json"))
    c = PairLockConfig.from_env({"TPB_PAIRLOCK_SIGNAL_SOURCE": "either", "TPB_PAIRLOCK_BOOK_THRESHOLD": "2",
                                 "TPB_PAIRLOCK_BOOK_CONFIRM_MS": "800", "TPB_PAIRLOCK_HEDGE_MODE": "conditional"})
    assert (c.book.signal_source, c.book.book_threshold, c.book.book_confirm_ms) == ("either", 2.0, 800.0)
    assert PairLockConfig.from_env({}).book.signal_source == "crypto"


# ─────────────────────────── the engine ───────────────────────────


def test_default_crypto_mode_builds_no_trigger_and_is_b_unchanged():
    e = _engine("crypto")
    assert e.booksig is None
    ev = _ev(e, NOW)                                          # no shock, no book: nothing
    assert not ev.tradeable
    assert e.snapshot()["pairlock"]["signal_source"] == "crypto" and e.snapshot()["pairlock"]["book_trigger"] is None


def test_book_mode_enters_from_the_book_with_no_crypto_shock():
    e = _engine("book", threshold=1.5)
    _observe(e, NOW, value=+3.0)
    assert e.booksig.n_signals == 1
    ev = _ev(e, NOW)                                          # no flow signal at all
    assert ev.tradeable, (ev.reason, ev.detail)
    tri = ev.features["triangle"]
    assert tri["trigger"] == "book" and tri["source"] == "book" and tri["book"] == 3.0 and ev.side == "UP"
    assert ev.size == 10.0 and ev.limit_price == pytest.approx(0.55) and tri["pairlock"]["kind"] == "entry"
    assert ev.net_edge == pytest.approx(tri["pairlock"]["ev"]) and ev.net_edge > 0
    assert e.shock_marks[SLUG][-1]["trigger"] == "book" and e.shock_marks[SLUG][-1]["ok"]
    o = e.to_order(ev, NOW)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.52, 10.0, NOW + 300.0)
    cyc = e.cycles[SLUG][-1]
    assert cyc.state is State.MONITORING and cyc.book.up_qty == 10.0
    assert e.stats["trigger_book"] == 1 and e.stats["entries"] == 1
    snap = e.snapshot()["pairlock"]
    assert snap["signal_source"] == "book" and snap["book_trigger"]["signals"] == 1
    # a DOWN reading enters DOWN
    e2 = _engine("book", threshold=1.5)
    _observe(e2, NOW, value=-2.0)
    ev2 = _ev(e2, NOW)
    assert ev2.tradeable and ev2.side == "DOWN" and ev2.features["triangle"]["book"] == -2.0


def test_book_mode_ignores_a_crypto_shock():
    e = _engine("book")
    _observe(e, NOW, value=0.0)
    ev = _ev(e, NOW, signal=_result(1, t=NOW))                # a crypto (flow) signal, the book quiet
    assert not ev.tradeable and ev.reason is Reason.WEAK_MOMENTUM and "signal source book" in ev.detail
    assert e.stats["crypto_ignored"] == 1 and e.stats["entries"] == 0


def test_either_prefers_the_crypto_shock_and_falls_back_to_the_book():
    e = _engine("either")
    _observe(e, NOW, value=+3.0)                              # a book signal is pending ...
    ev = _ev(e, NOW, signal=_result(1, t=NOW))                # ... and crypto fires this second
    assert ev.tradeable and ev.features["triangle"]["source"] == "crypto"
    assert SLUG not in e.booksig.pending                      # the book signal of this second is spent
    assert e.stats["trigger_crypto"] == 1
    e2 = _engine("either")
    _observe(e2, NOW, value=+3.0)
    ev2 = _ev(e2, NOW)                                        # crypto quiet: the book
    assert ev2.tradeable and ev2.features["triangle"]["source"] == "book" and e2.stats["trigger_book"] == 1


def test_both_needs_the_book_to_confirm_the_crypto_side():
    e = _engine("both", book_confirm=0.5)
    _observe(e, NOW, value=-3.0)                              # the book says DOWN
    ev = _ev(e, NOW, signal=_result(1, t=NOW))                # crypto says UP
    assert not ev.tradeable and "did not confirm" in ev.detail and e.stats["crypto_ignored"] == 1
    e2 = _engine("both", book_confirm=0.5)
    _observe(e2, NOW, value=+1.0)
    ev2 = _ev(e2, NOW, signal=_result(1, t=NOW))
    assert ev2.tradeable and ev2.features["triangle"]["source"] == "both" and ev2.features["triangle"]["book"] == 1.0
    e3 = _engine("both")
    _observe(e3, NOW, value=+3.0)
    assert not _ev(e3, NOW).tradeable                         # the book alone is never a signal in "both"


def test_a_book_signal_still_passes_the_pair_lock_entry_gate():
    e = _engine("book", model=_model(0.0))                   # the market's own price: EV < 0 at the ask
    _observe(e, NOW, value=+3.0)
    ev = _ev(e, NOW)
    assert not ev.tradeable and "entry EV" in ev.detail and e.stats["trigger_book"] == 0
    assert not e.shock_marks[SLUG][-1]["ok"]                  # drawn hollow with the reason


def test_a_refusal_before_the_signal_stage_stands():
    e = _engine("book")
    _observe(e, NOW, value=+3.0)
    ev = _ev(e, NOW + 170_000.0)                              # 10 s left: outside the time window
    assert not ev.tradeable and e.stats["trigger_book"] == 0


def test_settlement_forgets_the_market():
    e = _engine("book")
    _observe(e, NOW, value=+3.0)
    assert SLUG in e.booksig.last
    e.on_market_settled(SLUG, True, 0.0, NOW)
    assert SLUG not in e.booksig.last and SLUG not in e.booksig.pending
    assert math.isfinite(e.booksig.n_scored)
