"""The 2026-09-24 latency work: every optimisation must give the SAME answers
as the code it replaced, and the new plumbing must do what it says.

* chart points leave the state file (history_store) -- the 5 MB rewrite that
  froze the event loop -- and the dashboard still gets the market it draws
* one-pass order-flow windows and the bisected TWAP integral are bit-identical
* per-exchange feed lag, a cached latency report, the event-loop lag monitor
* the order worker wakes on submit and keeps its venue connection warm
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
from collections import deque
from types import SimpleNamespace

import pytest

from troll_poly_bot import history_store
from troll_poly_bot.features.orderflow import OrderFlowState
from troll_poly_bot.feeds.spot import _iso_ms, parse_coinbase_trade
from troll_poly_bot.history_store import HistoryStore, read_points, safe_slug
from troll_poly_bot.live import EmpiricalLatency, LiveBot
from troll_poly_bot.pricing.twap import TwapState

SLUG = "btc-updown-5m-1790260200"


# ─────────────────────────────── history store ────────────────────────────


def test_history_round_trip_and_reset(tmp_path):
    state = tmp_path / "live_state.json"
    st = HistoryStore(state)
    for t in range(5):
        st.append(SLUG, {"t": t, "up": 0.5})
    st.flush()
    assert [p["t"] for p in read_points(state, SLUG)] == [0, 1, 2, 3, 4]
    assert read_points(state, SLUG, limit=2) == [{"t": 3, "up": 0.5}, {"t": 4, "up": 0.5}]
    st.close()
    HistoryStore(state)                      # a new run starts empty
    assert read_points(state, SLUG) is None


def test_history_layout_follows_the_state_file_and_its_tag(tmp_path):
    assert history_store.history_dir(tmp_path / "live_state.json") == tmp_path / "live_state.history"
    assert history_store.history_dir(tmp_path / "live_state.spot.json") == tmp_path / "live_state.spot.history"


def test_a_torn_last_line_is_skipped(tmp_path):
    state = tmp_path / "s.json"
    st = HistoryStore(state)
    st.append(SLUG, {"t": 1})
    st.flush()
    with (history_store.history_dir(state) / f"{SLUG}.jsonl").open("ab") as fh:
        fh.write(b'{"t": 2, "up"')           # the writer caught mid-append
    assert read_points(state, SLUG) == [{"t": 1}]


@pytest.mark.parametrize("slug", ["../etc/passwd", "a/b", "", None, ".hidden", "x" * 200, "a b"])
def test_unsafe_slugs_never_touch_the_filesystem(tmp_path, slug):
    assert not safe_slug(slug)
    st = HistoryStore(tmp_path / "s.json")
    st.append(slug, {"t": 1})
    st.flush()
    assert list(history_store.history_dir(tmp_path / "s.json").iterdir()) == []
    assert read_points(tmp_path / "s.json", slug) is None


def test_drop_removes_the_file(tmp_path):
    st = HistoryStore(tmp_path / "s.json")
    st.append(SLUG, {"t": 1})
    st.flush()
    st.drop(SLUG)
    assert read_points(tmp_path / "s.json", SLUG) is None


def _bot(tmp_path):
    return LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                   earnings_path=str(tmp_path / "e.jsonl"), kill_file=str(tmp_path / "KILL"))


def test_the_state_carries_counts_not_points(tmp_path):
    bot = _bot(tmp_path)
    bot.price_history[SLUG] = deque([{"t": 1}, {"t": 2}, {"t": 3}])
    bot.price_history["eth-updown-5m-1790260200"] = deque([{"t": 1}])       # < 2 points: not drawable
    snap = bot.snapshot()
    assert "price_history" not in snap
    assert snap["history_meta"] == {SLUG: 3}
    assert set(snap["loop_lag_ms"]) == {"p50", "p99", "max", "n"}
    # and the state stays small enough to write without stalling the loop
    assert len(json.dumps(snap, default=str)) < 200_000


def test_the_dashboard_reads_the_chart_it_draws_from_the_history_file(tmp_path, monkeypatch):
    from troll_poly_bot.web import server as srv
    state = tmp_path / "live_state.json"
    state.write_text(json.dumps({"history_meta": {SLUG: 3, "eth-updown-5m-1": 7}, "live_markets": []}))
    st = HistoryStore(state)
    for t in (1, 2, 3):
        st.append(SLUG, {"t": t})
    st.flush()
    monkeypatch.setattr(srv, "LIVE_STATE", state)
    out = srv.Handler._live_payload(SLUG, None)
    assert [p["t"] for p in out["price_history"][SLUG]] == [1, 2, 3]
    assert out["history_meta"] == {SLUG: 3, "eth-updown-5m-1": 7}          # the bot's counts, untouched
    delta = srv.Handler._live_payload(SLUG, 2)
    assert [p["t"] for p in delta["price_history"][SLUG]] == [3] and delta["history_partial"] is True
    assert srv.Handler._live_payload(None, None)["price_history"] == {}
    assert srv.Handler._live_payload("../../secret", None)["price_history"] == {}


# ─────────────────────────── identical answers ─────────────────────────────


def test_order_flow_memo_is_exact_and_invalidated_by_new_data():
    rng = random.Random(3)
    of = OrderFlowState()
    t = 1_000_000.0
    for _ in range(300):
        t += rng.uniform(0, 200)
        of.on_trade(rng.uniform(1, 1e4), rng.random() < 0.5, t)
    of.on_quote("binance", 5.0, 3.0, t)
    first = of.features(t + 100)
    assert of.features(t + 100) == first == of._features(t + 100)       # a hit is the same answer
    first["tfi_5s"] = 99.0                                               # callers get copies
    assert of.features(t + 100)["tfi_5s"] != 99.0
    of.on_trade(1e6, True, t + 50)                                       # new data: recomputed
    assert of.features(t + 100) == of._features(t + 100) != first
    of.on_quote("bybit", 1.0, 9.0, t + 60)
    assert of.features(t + 100)["obi"] == of._features(t + 100)["obi"]


def _integrate_reference(samples, a_ms, b_ms):
    """The pre-2026-09-24 TwapState._integrate, verbatim."""
    if b_ms <= a_ms or not samples:
        return 0.0, 0.0
    pts = list(samples)
    total = covered = 0.0
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        lo, hi = max(t0, a_ms), min(t1, b_ms)
        if hi <= lo:
            continue
        span = t1 - t0
        if span <= 0:
            continue
        f0 = v0 + (v1 - v0) * (lo - t0) / span
        f1 = v0 + (v1 - v0) * (hi - t0) / span
        total += 0.5 * (f0 + f1) * (hi - lo) / 1000.0
        covered += (hi - lo) / 1000.0
    t_last, v_last = pts[-1]
    if b_ms > max(t_last, a_ms):
        lo = max(t_last, a_ms)
        total += v_last * (b_ms - lo) / 1000.0
        covered += (b_ms - lo) / 1000.0
    return total, covered


@pytest.mark.parametrize("disorder", [False, True])
def test_bisected_twap_integral_matches_the_reference(disorder):
    """Ordered samples now use prefix sums (2026-09-29, tests/test_twap_prefix.py): the same
    integral to rounding, no longer bit for bit. Out-of-order samples still take the walk."""
    rng = random.Random(11)
    for trial in range(200):
        tw = TwapState()
        t = 1_790_000_000_000.0
        px = 100.0
        for _ in range(rng.randint(1, 600)):
            t += rng.choice((0.0, rng.uniform(1, 400)))            # duplicates included
            px *= math.exp(rng.gauss(0, 1e-4))
            tw.update(px, t - (rng.uniform(0, 5000) if disorder and rng.random() < 0.02 else 0.0))
        a = t - rng.uniform(-10_000, 150_000)
        b = a + rng.uniform(-1000, 90_000)
        got, want = tw._integrate(a, b), _integrate_reference(tw._samples, a, b)
        if disorder and not tw._ordered:
            assert got == want                                      # the walk, bit for bit
        assert got[0] == pytest.approx(want[0], rel=1e-9, abs=1e-9) and got[1] == pytest.approx(want[1], abs=1e-9)


# ─────────────────────────────── feeds and latency ─────────────────────────


def test_coinbase_trades_carry_the_exchange_time():
    msg = {"type": "ticker", "product_id": "BTC-USD", "price": "83000.1", "last_size": "0.01", "side": "buy",
           "time": "2026-09-24T14:40:48.123456Z"}
    t = parse_coinbase_trade(msg, {"BTC-USD": "BTC"}, now_ms=1.0)
    assert t.event_ts_ms == pytest.approx(_iso_ms("2026-09-24T14:40:48.123456Z"))
    assert _iso_ms("2026-09-24T14:40:48.123456Z") == pytest.approx(1790260848123.456)
    assert _iso_ms("garbage") is None and _iso_ms(None) is None


def test_per_exchange_lag_is_reported():
    lat = EmpiricalLatency(seed=1)
    for ms in (50, 51, 52):
        lat.record_spot_source("coinbase", ms)
    for ms in (120, 130, 140):
        lat.record_spot_source("binance", ms)
    rep = lat.report()
    assert rep["spot_p50_coinbase"] == 51 and rep["spot_p50_binance"] == 130


def test_report_is_cached_for_spot_but_never_stale_for_orders():
    lat = EmpiricalLatency(seed=1)
    lat.record_spot(100.0)
    first = lat.report()
    lat.record_spot(900.0)
    assert lat.report() is first                               # high-rate buckets: up to 1 s stale
    for _ in range(30):
        lat.record_order(500.0)
    assert lat.report()["order_rtt_p50"] == 500.0              # an order sample clears the cache


def test_loop_lag_monitor_measures_a_blocked_loop(tmp_path):
    bot = _bot(tmp_path)

    async def scenario():
        task = asyncio.create_task(bot.loop_lag_loop(every=0.01))
        await asyncio.sleep(0.05)
        time.sleep(0.12)                                        # block the loop like a big state write did
        await asyncio.sleep(0.05)
        bot._stop.set()
        await asyncio.wait_for(task, 1.0)

    asyncio.run(scenario())
    rep = bot.loop_lag_report()
    assert rep["max"] >= 100.0 and rep["n"] >= 3


# ───────────────────────────── the order worker ────────────────────────────


def _exchange(client):
    from troll_poly_bot.execution.paper import FeeModel
    from troll_poly_bot.execution.polymarket import LiveCaps, PolymarketExchange
    from troll_poly_bot.signals.costs import FeeSchedule
    return PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=20.0,
                              caps=LiveCaps(kill_file="KILL-nope"), armed=True, clock=lambda: 1_000.0,
                              wallet="0x" + "ab" * 20)


def test_keep_warm_pings_the_order_connection():
    calls = []
    transport = SimpleNamespace(get_json=lambda path, **kw: calls.append(path) or 1)
    ex = _exchange(SimpleNamespace(_ctx=SimpleNamespace(secure_clob=transport)))
    before = ex._last_clob_mono
    ex._keep_warm()
    assert calls == ["/time"] and ex._last_clob_mono >= before


def test_keep_warm_skips_a_client_without_the_transport():
    ex = _exchange(SimpleNamespace())
    ex._keep_warm()                                             # no attribute error, nothing sent


def test_the_worker_wakes_on_submit_not_on_its_idle_tick():
    ex = _exchange(SimpleNamespace())
    placed = []
    ex._place = lambda order: placed.append(time.perf_counter())
    ex._precheck = lambda order: None
    ex._last_clob_mono = time.monotonic()                       # no keep-warm in this test
    ex.last_reconcile_ms = time.monotonic() * 1000.0            # nor a reconcile

    async def scenario():
        stop = asyncio.Event()
        worker = asyncio.create_task(ex.run(stop))
        await asyncio.sleep(0.02)                               # worker is idle, waiting
        from troll_poly_bot.types import Order, Side, TimeInForce
        t0 = time.perf_counter()
        ex.submit(Order(token_id="T", side=Side.BUY, price=0.4, size=5.0, tif=TimeInForce.FOK), 1_000.0)
        for _ in range(100):
            if placed:
                break
            await asyncio.sleep(0.001)
        stop.set()
        await asyncio.wait_for(worker, 2.0)
        return placed[0] - t0

    assert asyncio.run(scenario()) < 0.02                      # was up to 50 ms


# ──────────────────────── a backlogged feed is not fresh ───────────────────


def _q(exchange, price, ts):
    from troll_poly_bot.feeds.spot import SpotQuote
    return SpotQuote(asset="BTC", exchange=exchange, bid=price - 0.5, ask=price + 0.5, price=price, ts_ms=ts)


def _calibrated_composite():
    from troll_poly_bot.feeds.spot import CompositeSpot
    c = CompositeSpot(warmup_ms=0.0)
    t = 1_000.0
    for _ in range(10):                       # co-fresh pairs so every exchange calibrates
        t += 100.0
        for e, p in (("binance", 100.0), ("bybit", 100.0), ("coinbase", 100.0)):
            c.update(_q(e, p, t))
    return c, t


def test_a_lagging_exchange_leaves_the_composite_and_the_basis():
    c, t = _calibrated_composite()
    assert c.view("BTC", t).n_sources == 3
    c.lagging.add("binance")
    basis_before = {e: b.value for e, b in c.basis["BTC"].items()}
    c.update(_q("binance", 90.0, t + 50))      # a 10 %-stale price arriving "now"
    c.update(_q("bybit", 100.0, t + 60))
    v = c.view("BTC", t + 70)
    assert set(v.sources) == {"bybit", "coinbase"} and v.price == pytest.approx(100.0)
    assert c.basis["BTC"]["binance"].value == basis_before["binance"]     # never compared while lagging
    c.lagging.discard("binance")
    c.update(_q("binance", 100.0, t + 80))
    assert c.view("BTC", t + 90).n_sources == 3


def test_backlog_watch_needs_the_lag_to_persist():
    from troll_poly_bot.feeds.spot import _BacklogWatch
    w = _BacklogWatch(threshold_ms=5000, hold_ms=3000)
    assert not w.behind(9000, 0.0)
    assert not w.behind(9000, 2000.0)
    assert not w.behind(100, 2500.0)            # caught up: the clock restarts
    assert not w.behind(9000, 3000.0)
    assert w.behind(9000, 6000.0)


def test_a_backlogged_connection_is_dropped():
    from troll_poly_bot.feeds.spot import FeedBacklog, TradeTick, _BacklogWatch, _lag
    seen = []
    w = _BacklogWatch(threshold_ms=5000, hold_ms=0)
    t = TradeTick("BTC", "binance", 1.0, 1.0, True, ts_ms=10_000.0, event_ts_ms=1_000.0)
    with pytest.raises(FeedBacklog):
        _lag(lambda s, ms: seen.append((s, ms)), "binance", 10_000.0, t, w)
    assert seen == [("binance", 9_000.0)]


def test_the_bot_excludes_and_readmits_a_lagging_feed(tmp_path):
    bot = _bot(tmp_path)
    now = [1_000_000.0]
    bot.clock.now = lambda: now[0]
    bot._on_source_lag("binance", 120.0)
    assert bot.composite.lagging == set()
    bot._on_source_lag("binance", 9_800.0)
    assert bot.composite.lagging == {"binance"}
    bot._on_source_lag("binance", 600.0)        # better, not yet recovered (hysteresis)
    assert bot.composite.lagging == {"binance"}
    bot._on_source_lag("binance", 150.0)
    assert bot.composite.lagging == set()
    bot._on_source_lag("bybit", 2_000.0)
    now[0] += 11_000.0                           # and then no trades at all
    bot._forget_stale_lag_flags(now[0])
    assert bot.composite.lagging == set()
    assert bot.latency.report()["spot_p50_binance"] > 0
