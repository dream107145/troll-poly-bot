"""The execution model: real latency, quote age, what a paper fill has to
survive, the last look before a POST, the timeline and what happens after."""
from __future__ import annotations

from pathlib import Path

import pytest

from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.forensics import TradeForensics, latency_segments
from troll_poly_bot.execution.latency import VENUE_RTT_PRIOR_MS
from troll_poly_bot.execution.outcomes import MarkoutTracker, SignalOutcomeTracker
from troll_poly_bot.execution.paper import FeeModel, PaperExchange, RejectReason
from troll_poly_bot.execution.polymarket import LiveCaps, PolymarketExchange
from troll_poly_bot.execution.scenarios import OPTIMISTIC, REALISTIC
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.live import EmpiricalLatency, LiveBook, LiveBot, LiveMarket
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.types import BookLevel, Market, Order, OrderBook, OrderState, Side, TimeInForce

from test_polymarket_exchange import FakeClient, accepted, rejected

SLUG = "btc-updown-5m-1"


# ───────────────────────────────── latency ─────────────────────────────────


def test_order_latency_is_the_venue_not_a_gamma_get():
    lat = EmpiricalLatency(seed=1)
    for _ in range(100):
        lat.record_gamma(30.0)                                   # 30 ms Gamma GETs must not become order latency
    rep = lat.report()
    assert rep["n_order"] == 0 and lat.order_source == "prior"
    assert rep["order_rtt_p50"] == pytest.approx(sorted(VENUE_RTT_PRIOR_MS)[len(VENUE_RTT_PRIOR_MS) // 2])
    draws = [lat.submit() for _ in range(400)]
    assert min(draws) > 80.0                                     # presend + half a 218+ ms round trip
    assert 100.0 < sorted(draws)[len(draws) // 2] < 400.0
    for _ in range(30):
        lat.record_order(500.0)
    assert lat.order_source == "measured" and lat.report()["order_rtt_p50"] == 500.0
    assert lat.reaction_lag_ms > 250.0


def test_the_bot_keeps_gamma_out_of_the_order_bucket(tmp_path):
    bot = _bot(tmp_path)
    bot.latency.record_gamma(25.0)
    bot._sync_latency_profile()
    # the strategy's own view of a round trip is the measured venue prior, ~0.25 s, not 25 ms
    assert bot.strategy_latency.round_trip_ms() > 200.0


# ─────────────────────────── the paper venue ───────────────────────────


def _paper(scenario=REALISTIC, seed=3):
    return PaperExchange(EmpiricalLatency(seed=seed), FeeModel.from_schedule(FeeSchedule()), starting_balance=100.0,
                         scenario=scenario, seed=seed)


def _book(asks, bids=((0.48, 100.0),), ts=0.0):
    return OrderBook("T", bids=[BookLevel(p, s) for p, s in bids], asks=[BookLevel(p, s) for p, s in asks], ts=ts)


def _buy(price=0.51, size=5.0, tif=TimeInForce.FOK):
    return Order(token_id="T", side=Side.BUY, price=price, size=size, tif=tif, expected_price=0.50)


def test_a_paper_order_arrives_after_real_latency_and_matches_the_book_then():
    ex = _paper(OPTIMISTIC)
    state = {"book": _book([(0.50, 100.0)])}
    ex.submit(_buy(), 0.0)
    assert ex.step(50.0, lambda t: state["book"]) == []           # nothing can have happened in 50 ms
    state["book"] = _book([(0.55, 100.0)])                        # the ask moved while we were on the wire
    out = []
    for now in range(100, 3000, 100):
        out += ex.step(float(now), lambda t: state["book"])
    assert len(out) == 1 and out[0].reject_reason is RejectReason.FOK_UNFILLABLE     # changing ask: no fill


def test_a_disappearing_ask_is_no_fill():
    ex = _paper(OPTIMISTIC)
    state = {"book": _book([(0.50, 100.0)])}
    ex.submit(_buy(), 0.0)
    state["book"] = _book([])                                     # gone before we land
    out = []
    for now in range(100, 3000, 100):
        out += ex.step(float(now), lambda t: state["book"])
    assert out and out[0].is_rejected and not ex.fills


def test_quote_survival_refuses_a_fill_the_venue_had_already_pulled():
    ex = _paper(REALISTIC)
    arrivals = []
    ex.on_arrival = lambda o, ts, vwap, sh: arrivals.append((ts, vwap, sh))
    state = {"book": _book([(0.50, 100.0)])}
    ex.submit(_buy(), 0.0)
    out = []
    for now in range(100, 1000, 100):
        out += ex.step(float(now), lambda t: state["book"])
    assert arrivals and arrivals[0][1] == pytest.approx(0.50)   # matched at arrival, provisionally
    state["book"] = _book([(0.56, 100.0)])                        # ... and the quote is gone 1 s later
    for now in range(1000, 5000, 100):
        out += ex.step(float(now), lambda t: state["book"])
    assert len(out) == 1 and out[0].reject_reason is RejectReason.QUOTE_PULLED and not ex.fills


def test_insufficient_depth_is_a_whole_kill_never_a_partial_fok():
    ex = _paper(OPTIMISTIC)
    book = _book([(0.50, 3.0), (0.60, 100.0)])
    ex.submit(_buy(price=0.51, size=5.0), 0.0)
    out = []
    for now in range(100, 3000, 100):
        out += ex.step(float(now), lambda t: book)
    assert out[0].reject_reason is RejectReason.FOK_UNFILLABLE and out[0].order.filled_size == 0.0


def test_a_resting_order_can_partially_fill_and_be_cancelled():
    ex = PaperExchange(EmpiricalLatency(seed=2), FeeModel.from_schedule(FeeSchedule()), starting_balance=100.0,
                       scenario=OPTIMISTIC)
    book = _book([(0.50, 2.0)])
    o = _buy(price=0.50, size=5.0, tif=TimeInForce.GTC)
    ex.submit(o, 0.0)
    for now in range(100, 3000, 100):
        ex.step(float(now), lambda t: book)
    assert o.filled_size == pytest.approx(2.0 * (1 - ex.latency.profile.contention)) or o.filled_size > 0
    assert o.state in (OrderState.PARTIAL, OrderState.RESTING) and o.order_id in ex._resting
    ex.cancel(o.order_id, 3000.0)
    for now in range(3100, 6000, 100):
        ex.step(float(now), lambda t: _book([]))
    assert o.order_id not in ex._resting and o.state is OrderState.CANCELLED


def test_paper_stamps_the_order_timeline():
    ex = _paper(OPTIMISTIC)
    book = _book([(0.50, 100.0)])
    o = _buy()
    ex.submit(o, 1000.0)
    for now in range(1100, 4000, 100):
        ex.step(float(now), lambda t: book)
    assert o.sent_ts == 1000.0 and o.venue_fill_ts > o.sent_ts and o.venue_ack_ts >= o.venue_fill_ts


# ─────────────────────────────── quote age ───────────────────────────────


def test_quote_age_follows_the_feed_and_dies_with_the_connection(tmp_path):
    bot = _bot(tmp_path)
    lm = _market(bot, now=1_000_000.0)
    bot._book_conn = 1
    lm.books["Y"].apply_snapshot({"bids": [{"price": "0.48", "size": "10"}], "asks": [], "timestamp": "999900"},
                                 recv_ts=999_950.0, conn=1)
    bot._advance_book_clock(999_900.0, 999_950.0)
    assert bot.quote_age_ms("Y", 1_000_000.0) == pytest.approx(100.0)
    # a quiet token is current as of the newest message on the feed
    bot._advance_book_clock(999_990.0, 999_995.0)
    assert bot.quote_age_ms("Y", 1_000_000.0) == pytest.approx(10.0)
    # a backlogged / silent feed ages every book
    assert bot.quote_age_ms("Y", 1_003_000.0) == pytest.approx(3010.0)
    # a new connection: no book until its own snapshot arrives
    bot._book_conn = 2
    assert bot.quote_age_ms("Y", 1_000_000.0) is None
    assert bot.quote_age_ms("N", 1_000_000.0) is None               # never had a snapshot


def test_a_future_venue_timestamp_cannot_freshen_every_book(tmp_path):
    bot = _bot(tmp_path)
    bot._advance_book_clock(2_000_000.0, 1_000_000.0)                # 1000 s in the future: ignored
    assert bot._book_last_venue_ts == 0.0


# ───────────────────────────── kill switch ─────────────────────────────


def test_paper_honours_the_kill_file(tmp_path):
    kill = tmp_path / "KILL"
    bot = _bot(tmp_path, kill_file=str(kill))
    assert bot._kill_state() is None
    kill.write_text("x")
    assert bot._kill_state() == "entries"
    Path(str(kill) + "_ALL").write_text("x")
    assert bot._kill_state() == "all"


def test_a_kill_file_created_after_queueing_stops_the_post(tmp_path):
    client = FakeClient(responses=[accepted("0xa", 2.0, 5.0)])
    ex = _venue(client, tmp_path)
    ex.submit(_live_order(), 1000.0)
    (tmp_path / "KILL").write_text("stop")                          # between the queue and the POST
    ex._place(ex._queue.popleft())
    res = ex.step(1001.0)
    assert res[0].reject_reason is RejectReason.KILL_SWITCH and client.posted == []


def test_a_stale_decision_is_not_posted(tmp_path):
    client = FakeClient(responses=[accepted("0xa", 2.0, 5.0)])
    clock = {"now": 1000.0}
    ex = _venue(client, tmp_path, clock=lambda: clock["now"])
    ex.submit(_live_order(), 1000.0)
    clock["now"] = 1000.0 + ex.max_entry_decision_age_ms + 500.0      # the worker was busy for 1.5 s
    ex._place(ex._queue.popleft())
    assert ex.step(clock["now"])[0].reject_reason is RejectReason.STALE_DECISION and client.posted == []


def test_the_bot_can_veto_a_post_on_its_freshest_book(tmp_path):
    client = FakeClient(responses=[accepted("0xa", 2.0, 5.0)])
    ex = _venue(client, tmp_path)
    ex.pre_post_check = lambda o: "ask 0.60 above the cap 0.52"
    ex.submit(_live_order(), 1000.0)
    ex._place(ex._queue.popleft())
    assert ex.step(1001.0)[0].reject_reason is RejectReason.PRICE_MOVED and client.posted == []


def test_the_pre_post_check_reads_the_live_book(tmp_path):
    bot = _bot(tmp_path)
    lm = _market(bot, now=1_000_000.0)
    lm.books["Y"].asks = {0.52: 3.0, 0.53: 50.0}
    o = Order(token_id="Y", side=Side.BUY, price=0.52, size=5.0)
    assert "under the cap" in bot._pre_post_check(o)                 # 3 shares under the cap, 5 needed
    assert o.sent_ask == 0.52
    lm.books["Y"].asks = {0.55: 50.0}
    assert "above the cap" in bot._pre_post_check(o)
    lm.books["Y"].asks = {0.51: 50.0}
    assert bot._pre_post_check(o) is None
    assert bot._pre_post_check(Order(token_id="Y", side=Side.SELL, price=0.4, size=5.0)) is None
    # a RESTING maker bid sits under the ask by design: never vetoed (2026-10-08, the live pair bids)
    from troll_poly_bot.types import TimeInForce
    lm.books["Y"].asks = {0.55: 50.0}
    assert bot._pre_post_check(Order(token_id="Y", side=Side.BUY, price=0.40, size=5.0, tif=TimeInForce.GTC, post_only=True)) is None


def test_a_real_post_records_the_timeline_and_feeds_the_latency_model(tmp_path):
    client = FakeClient(responses=[accepted("0xvenue1", 2.0, 5.0)])
    ex = _venue(client, tmp_path)
    rtts, fills = [], []
    ex.on_rtt = rtts.append
    ex.on_fill_event = lambda o, px, sh, ts: fills.append((px, sh))
    o = _live_order()
    ex.submit(o, 1000.0)
    ex._place(ex._queue.popleft())
    assert o.sent_ts and o.venue_ack_ts and o.venue_fill_ts and o.venue_order_id == "0xvenue1"
    assert "0xvenue1" in ex.own_venue_ids and len(rtts) == 1 and fills == [(0.4, 5.0)]


def test_a_killed_post_still_measures_the_round_trip(tmp_path):
    client = FakeClient(responses=[rejected("fok_not_filled", "killed")])
    ex = _venue(client, tmp_path)
    rtts = []
    ex.on_rtt = rtts.append
    ex.submit(_live_order(), 1000.0)
    ex._place(ex._queue.popleft())
    assert ex.step(1001.0)[0].reject_reason is RejectReason.FOK_UNFILLABLE and len(rtts) == 1


# ─────────────────────────────── timeline ───────────────────────────────


def test_latency_segments_of_one_trade(tmp_path):
    seg = latency_segments({"t0_spot_event": 1000.0, "t1_spot_recv": 1160.0, "t3_book_venue": 1100.0,
                            "t3_book_recv": 1150.0, "t4_decision": 1300.0, "t5_sent": 1340.0,
                            "t6_venue_ack": 1600.0, "t7_venue_fill": 1600.0,
                            "t8_exit_decision": 9000.0, "t10_exit_fill": 9300.0})
    assert seg == {"spot_feed_latency_ms": 160.0, "book_latency_ms": 50.0, "presend_ms": 40.0,
                   "venue_ack_ms": 260.0, "venue_fill_latency_ms": 260.0, "decision_to_fill_ms": 300.0,
                   "data_to_fill_ms": 600.0, "exit_decision_to_fill_ms": 300.0, "signal_processing_ms": 140.0}
    f = TradeForensics(tmp_path / "f.jsonl", journal_path=tmp_path / "j.jsonl")
    f.open("o1", kind="entry", slug="btc-updown-5m-1790000000", t4_decision=1300.0, t5_sent=1340.0,
           t6_venue_ack=1600.0, t7_venue_fill=1600.0)
    out = f.close("o1", 2000.0)
    assert out["presend_ms"] == 40.0 and out["decision_to_fill_ms"] == 300.0 and out["t0_spot_event"] is None


# ────────────────────────── after the fill / signal ──────────────────────────


def test_markouts_measure_adverse_selection(tmp_path):
    mk = MarkoutTracker(tmp_path / "m.jsonl", offsets_ms=(100.0, 1000.0))
    mk.on_fill(order_id="o1", token_id="T", side="BUY", price=0.52, size=5.0, now_ms=0.0, bid=0.51, ask=0.53)
    quotes = {100.0: (0.49, 0.51, None), 1000.0: (0.47, 0.49, None)}    # the market ran away from us
    for t in (50.0, 100.0, 500.0, 1000.0):
        mk.due(t, lambda tok, cap, t=t: quotes.get(t, (0.51, 0.53, None)))
    row = mk.completed[-1]
    assert row["mid_move"]["100"] == pytest.approx(-0.02) and row["mid_move"]["1000"] == pytest.approx(-0.04)
    assert row["adverse_1s"] == pytest.approx(0.04) and row["markout"]["1000"] == pytest.approx(0.48 - 0.52)
    mk.attach("o1", filled=True)                                        # after completion: an amend line
    assert "markout_amend" in (tmp_path / "m.jsonl").read_text()


def test_signal_fates_separate_good_misses_from_bad_ones(tmp_path):
    so = SignalOutcomeTracker(tmp_path / "s.jsonl", horizons_s=(20.0,), primary_horizon_s=20.0, min_gap_ms=0.0)
    books = {}

    def sampler(tok, cap):
        b, a = books[tok]
        return b, a, 100.0 if cap is not None else None
    # a skip whose price was there and ran up: a BAD miss
    so.on_signal(signal_id="s1", slug="x-1", token_id="A", side="UP", decision="SKIP", reason="EDGE_TOO_SMALL",
                 now_ms=0.0, ask=0.50, cap=0.50, size=5.0, arrival_ms=300.0)
    # a skip whose price was gone at arrival: skipping it cost nothing
    so.on_signal(signal_id="s2", slug="x-2", token_id="B", side="UP", decision="SKIP", reason="STALE_QUOTE",
                 now_ms=0.0, ask=0.50, cap=0.50, size=5.0, arrival_ms=300.0)
    # a trade that was killed and would have lost
    so.on_signal(signal_id="s3", slug="x-3", token_id="C", side="UP", decision="BUY", reason="",
                 now_ms=0.0, ask=0.50, cap=0.51, size=5.0, arrival_ms=300.0)
    so.attach("s3", filled=False, reject_reason="fok_unfillable")
    books.update({"A": (0.49, 0.50), "B": (0.55, 0.57), "C": (0.49, 0.50)})
    so.due(300.0, sampler)
    books.update({"A": (0.60, 0.61), "B": (0.60, 0.61), "C": (0.45, 0.46)})
    so.due(20_000.0, sampler)
    klass = {r["signal_id"]: r["classification"] for r in so.completed}
    assert klass == {"s1": "BAD_MISS", "s2": "UNFILLABLE_SKIP", "s3": "KILLED_GOOD_MISS"}
    bad = next(r for r in so.completed if r["signal_id"] == "s1")
    fee = FeeSchedule().taker_fee_per_share
    assert bad["hypothetical_net"]["20"] == pytest.approx(0.60 - 0.50 - fee(0.50) - fee(0.60))


def test_a_rejected_signal_is_recorded_once_per_reason_not_every_tick(tmp_path):
    so = SignalOutcomeTracker(tmp_path / "s.jsonl", min_gap_ms=2000.0)
    assert so.should_record("m", "EDGE_TOO_SMALL", 0.0, traded=False)
    so._last["m"] = (0.0, "EDGE_TOO_SMALL")
    assert not so.should_record("m", "EDGE_TOO_SMALL", 200.0, traded=False)
    assert so.should_record("m", "STALE_QUOTE", 200.0, traded=False)
    assert so.should_record("m", "EDGE_TOO_SMALL", 2500.0, traded=False)
    assert so.should_record("m", "", 200.0, traded=True)


# ─────────────────────────── the bot, end to end ───────────────────────────


def test_an_order_in_flight_is_never_doubled(tmp_path):
    bot = _bot(tmp_path)
    lm = _market(bot, now=bot.clock.now())
    lm.inflight = 1
    bot._tick()
    assert bot.exec_rejections.get("ORDER_IN_FLIGHT", 0) >= 1 and lm.submitted == 0
    lm.inflight = 0
    lm.last_entry_ms = bot.clock.now()
    bot._tick()
    assert bot.exec_rejections.get("ENTRY_THROTTLED", 0) >= 1 and lm.submitted == 0


def test_shadow_takes_every_order_through_the_real_path_without_posting(tmp_path):
    client = FakeClient()
    dry = PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=20.0,
                             caps=LiveCaps(kill_file=str(tmp_path / "KILL")), armed=False,
                             clock=lambda: 1000.0, wallet=FakeClient.wallet)
    dry.authenticate()
    cfg = BotConfig()
    cfg.deploy_mode = "shadow"
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"), dry_run_twin=dry)
    assert not bot.is_live and isinstance(bot.exchange, PaperExchange) and bot.mode_label == "shadow-paper"
    o = Order(token_id="Y", side=Side.BUY, price=0.40, size=5.0)
    bot.forensics.open(o.order_id, kind="entry", slug=SLUG)
    now = bot.clock.now()                                           # the twin now runs on the bot's clock
    bot._dry_submit(o, now)
    dry._place(dry._queue.popleft())
    for res in dry.step(now + 1):
        bot._on_dry_result(res, now + 1)
    rec = bot.forensics.records[o.order_id].fields
    assert rec["live_path"] == "dry_run" and rec["live_path_would_post"] is True
    assert client.signed and client.posted == []                     # signed, never posted


# ───────────────────────────────── helpers ─────────────────────────────────


def _bot(tmp_path, kill_file=None):
    return LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                   earnings_path=str(tmp_path / "e.jsonl"), kill_file=kill_file or str(tmp_path / "KILL"))


def _market(bot, now):
    market = Market(condition_id="c", asset="BTC", yes_token_id="Y", no_token_id="N", strike=100.0,
                    open_ts=now - 60_000.0, close_ts=now + 240_000.0, tick_size=0.01, min_size=5.0, slug=SLUG)
    lm = LiveMarket(meta=MarketMeta(market=market, resolution_source="", accepting_orders=True, duration_s=300))
    for token in ("Y", "N"):
        lm.books[token] = LiveBook(token)
    bot.markets[SLUG] = lm
    bot.token_index["Y"] = bot.token_index["N"] = (SLUG, lm)
    return lm


def _venue(client, tmp_path, clock=None):
    ex = PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=20.0,
                            caps=LiveCaps(kill_file=str(tmp_path / "KILL")), armed=True,
                            clock=clock or (lambda: 1000.0), wallet=FakeClient.wallet)
    ex.authenticate()
    return ex


def _live_order():
    return Order(token_id="1234567890", side=Side.BUY, price=0.40, size=5.0, tif=TimeInForce.FOK, expected_price=0.40)


def test_paper_learns_order_latency_from_earlier_live_runs(tmp_path):
    rows = [{"event": "trade", "mode": "LIVE-ARMED", "venue_rtt_ms": 400.0 + i, "presend_ms": 30.0}
            for i in range(30)]
    rows.append({"event": "trade", "mode": "live-paper", "venue_rtt_ms": 5.0})     # paper is not evidence
    (tmp_path / "trade_forensics.momentum.jsonl").write_text("\n".join(__import__("json").dumps(r) for r in rows))
    bot = _bot(tmp_path)
    rep = bot.latency.report()
    assert rep["n_order"] == 30 and rep["order_rtt_p50"] >= 400.0 and bot.latency.order_source == "measured"


def test_a_slow_feed_cannot_widen_the_staleness_gates_without_limit(tmp_path):
    bot = _bot(tmp_path)
    base_spot, base_book = bot.cfg.engine.max_spot_age_ms, bot.cfg.engine.max_book_age_ms
    for _ in range(100):
        bot.latency.record_spot(29_000.0)                    # a starved process: spot 29 s late
        bot.latency.record_book(20_000.0)
    bot._sync_latency_profile()
    assert bot.cfg.engine.max_spot_age_ms == pytest.approx(base_spot * 2.0)
    assert bot.cfg.engine.max_book_age_ms == pytest.approx(base_book * 2.0)


def test_only_venue_kills_feed_the_kill_rate_guard(tmp_path):
    from troll_poly_bot.execution.paper import OrderResult
    bot = _bot(tmp_path)
    for reason in (RejectReason.KILL_SWITCH, RejectReason.STALE_DECISION, RejectReason.PRICE_MOVED,
                   RejectReason.DRY_RUN, RejectReason.LIVE_CAPS):
        o = Order(token_id="Y", side=Side.BUY, price=0.5, size=5.0)
        bot._on_result(OrderResult(o, [], reason, 0.0), 1000.0)
    assert len(bot.guard.outcomes) == 0                        # our own refusals are not the venue's
    o = Order(token_id="Y", side=Side.BUY, price=0.5, size=5.0)
    bot._on_result(OrderResult(o, [], RejectReason.FOK_UNFILLABLE, 0.0), 1000.0)
    assert list(bot.guard.outcomes) == [False]
