"""`trade_detail()` feeds the dashboard's trade dialog: one window of one bot.

Clicking a ledger row draws that window's prices with the bot's orders and
fills on them, beside the order list. The orders come from two files that
describe the same thing at different times -- the live state's order marks
while the window runs, the forensics log once each order has finished -- so
the merge between them is what is covered most here, along with the delta
contract that keeps a once-a-second poll cheap.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from troll_poly_bot import archive, earnings
from troll_poly_bot.web import server

SLUG = "xrp-updown-5m-1790683200"
OPEN = 1790683200_000
CLOSE = OPEN + 300_000


def _jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """A main bot whose files live in tmp_path, with fresh caches."""
    state = tmp_path / "live_state.json"
    log = tmp_path / "live_trades.jsonl"
    monkeypatch.setattr(server, "LIVE_STATE", state)
    monkeypatch.setattr(server, "TRADE_LOG", log)
    monkeypatch.setattr(server, "ARCHIVE", archive.ArchiveIndex(tmp_path / "charts", tmp_path / "idx.json"))
    monkeypatch.setattr(server, "ARCHIVED", server._ArchivedWindows())
    monkeypatch.setattr(server, "FORENSICS", server._ForensicsIndex())
    _jsonl(log, [
        {"ts": OPEN + 162_000, "event": "fill", "action": "BUY", "slug": SLUG, "side": "UP",
         "price": 0.37, "size": 10.0, "cost": 3.7, "fee": 0.17, "mode": "paper"},
        {"ts": OPEN + 169_000, "event": "fill", "action": "SELL", "slug": SLUG, "side": "UP",
         "kind": "TAKE_PROFIT", "price": 0.51, "size": 10.0, "cost": -5.1, "fee": 0.18, "mode": "paper"},
        {"ts": OPEN + 169_000, "event": "settle", "slug": SLUG, "exited": True, "pnl": 1.05, "mode": "paper"},
        {"ts": OPEN + 10_000, "event": "fill", "action": "BUY", "slug": "btc-updown-5m-1790683200",
         "side": "DOWN", "price": 0.5, "size": 1.0, "cost": 0.5},
    ])
    return tmp_path


def _forensics(tmp_path, rows):
    _jsonl(tmp_path / "trade_forensics.jsonl", rows)


FILLED_ENTRY = {
    "event": "trade", "order_id": "ord-1", "slug": SLUG, "side": "UP", "kind": "entry",
    "outcome": "EXIT", "decision_price": 0.39, "limit_price": None, "quantity": 10.0,
    "execution_price": 0.37,
    "stages": {"DECISION": OPEN + 161_600, "ORDER_SUBMITTED": OPEN + 161_600, "FULL_FILL": OPEN + 162_000},
}
REJECTED_EXIT = {
    "event": "trade", "order_id": "ord-2", "slug": SLUG, "side": "UP", "kind": "take_profit",
    "outcome": "REJECTED:fok_unfillable", "decision_price": 0.49, "limit_price": 0.49, "quantity": 10.0,
    "execution_price": None,
    "stages": {"ORDER_SUBMITTED": OPEN + 164_100, "ORDER_REJECTED": OPEN + 164_500},
}


def test_forensics_file_is_named_the_way_the_bot_names_it(tmp_path):
    assert server.forensics_path(tmp_path / "live_trades.jsonl").name == "trade_forensics.jsonl"
    assert server.forensics_path(tmp_path / "live_trades.spot.jsonl").name == "trade_forensics.spot.jsonl"


def test_a_filled_entry_reads_as_an_order_mark():
    o = server.order_from_forensics(FILLED_ENTRY)
    assert o == {"id": "ord-1", "slug": SLUG, "t": OPEN + 161_600, "side": "UP", "kind": "entry", "limit": 0.39,
                 "size": 10.0, "outcome": "filled", "price": 0.37, "t_done": OPEN + 162_000, "reason": None,
                 "fee": pytest.approx(0.07 * 0.37 * 0.63 * 10.0, abs=1e-4),
                 "book_age_ms": None, "spot_age_ms": None, "spot_src": None, "t_event": None, "t_sent": None}


def test_a_rejected_exit_keeps_its_reason_and_no_fill_price():
    o = server.order_from_forensics(REJECTED_EXIT)
    assert (o["kind"], o["outcome"], o["reason"], o["price"], o["limit"]) == \
        ("take_profit", "rejected", "fok_unfillable", None, 0.49)
    assert o["t_done"] == OPEN + 164_500


def test_a_live_mark_updates_a_working_order_but_never_unfinishes_one():
    finished = [server.order_from_forensics(FILLED_ENTRY)]
    marks = [
        {"id": "ord-1", "t": OPEN + 161_600, "side": "UP", "kind": "entry", "outcome": "sent", "limit": 0.39},
        {"id": "ord-3", "t": OPEN + 170_000, "side": "UP", "kind": "TIME_STOP", "outcome": "sent", "limit": 0.3},
    ]
    out = server.merge_orders(finished, marks)
    assert [o["id"] for o in out] == ["ord-1", "ord-3"]            # oldest first
    assert out[0]["outcome"] == "filled"                          # not walked back to "sent"
    assert out[1]["kind"] == "time_stop"                          # live kinds are upper case
    # the venue answered: the mark now carries the fill, and it wins
    marks[1].update(outcome="filled", price=0.29, t_done=OPEN + 170_400)
    out = server.merge_orders(finished, marks)
    assert (out[1]["outcome"], out[1]["price"]) == ("filled", 0.29)


def test_an_archived_window_brings_its_path_fills_and_orders(bot):
    _forensics(bot, [FILLED_ENTRY, {"event": "stage", "order_id": "ord-1", "stage": "SIGNAL"}, REJECTED_EXIT])
    (bot / "charts").mkdir()
    pts = [{"t": OPEN - 30_000, "up": 0.5, "left": 330.0},        # before the open: not drawn
           {"t": OPEN + 1_000, "up": 0.5, "down": 0.5, "left": 299.0, "srcs": {"binance": 1.5}},
           {"t": OPEN + 200_000, "up": 0.6, "down": 0.4, "left": 100.0},
           {"t": CLOSE + 5_000, "up": 1.0, "left": 0.0}]           # after the close: not drawn
    (bot / "charts" / f"{SLUG}.json").write_text(json.dumps(
        {"slug": SLUG, "strike": 1.51, "outcome": "UP", "points": pts}), encoding="utf-8")

    d = server.trade_detail(SLUG)
    assert (d["open_ts"], d["close_ts"], d["asset"]) == (OPEN, CLOSE, "XRP")
    assert (d["source"], d["live"], d["outcome"], d["strike"]) == ("archive", False, "UP", 1.51)
    assert [p["t"] for p in d["points"]] == [OPEN + 1_000, OPEN + 200_000]
    assert "srcs" not in d["points"][0]                            # trimmed to what is drawn
    assert [f["action"] for f in d["fills"]] == ["BUY", "SELL"]    # this window only
    assert [o["id"] for o in d["orders"]] == ["ord-1", "ord-2"]
    assert d["trips"][0]["status"] == "sold" and d["trips"][0]["pnl"] == 1.05

    # an archive cannot change: a client that holds it is told to keep it
    again = server.trade_detail(SLUG, since=OPEN + 200_000, have="archive")
    assert again["points"] is None


def test_a_running_window_streams_its_points_and_live_orders(bot):
    _forensics(bot, [])
    state = {"live_markets": [{"slug": SLUG, "strike": 1.5, "secs_left": 90.0}],
             "order_marks": {SLUG: [{"id": "ord-9", "t": OPEN + 205_000, "side": "DOWN", "kind": "entry",
                                     "limit": 0.41, "size": 5.0, "outcome": "sent"}]}}
    server.LIVE_STATE.write_text(json.dumps(state), encoding="utf-8")
    hist = bot / "live_state.history"
    hist.mkdir()
    _jsonl(hist / f"{SLUG}.jsonl", [{"t": OPEN + i * 1_000, "up": 0.5, "left": 300 - i} for i in range(1, 6)])

    d = server.trade_detail(SLUG)
    assert (d["source"], d["live"], d["running"], d["secs_left"]) == ("live", True, True, 90.0)
    assert len(d["points"]) == 5 and d["partial"] is False
    assert d["orders"][0]["outcome"] == "sent"

    delta = server.trade_detail(SLUG, since=OPEN + 3_000, have="live")
    assert [p["t"] for p in delta["points"]] == [OPEN + 4_000, OPEN + 5_000]
    assert delta["partial"] is True
    # a client holding a different source is sent everything
    assert server.trade_detail(SLUG, since=OPEN + 3_000, have="archive")["partial"] is False


def test_a_stopped_bot_is_not_reported_live(bot, monkeypatch):
    _forensics(bot, [])
    server.LIVE_STATE.write_text(json.dumps({"live_markets": []}), encoding="utf-8")
    hist = bot / "live_state.history"
    hist.mkdir()
    _jsonl(hist / f"{SLUG}.jsonl", [{"t": OPEN + 1_000, "up": 0.5}])
    monkeypatch.setattr(server, "BOT_STALE_AFTER_S", -1.0)
    d = server.trade_detail(SLUG)
    assert (d["source"], d["running"], d["live"]) == ("live", False, False)


def test_the_live_push_carries_every_markets_orders_but_one_markets_signals():
    payload = {"order_marks": {SLUG: [{"id": "a"}], "btc-updown-5m-1790683200": [{"id": "b"}],
                               "eth-updown-5m-1790683200": []},
               "signal_marks": {SLUG: [{"t": 1}], "btc-updown-5m-1790683200": [{"t": 2}]}}
    out = server._filter_live(payload, SLUG)
    assert set(out["order_marks"]) == {SLUG, "btc-updown-5m-1790683200"}      # empties dropped
    assert set(out["signal_marks"]) == {SLUG}
    assert set(server._filter_live({"order_marks": {SLUG: [{"id": "a"}]}}, None)["order_marks"]) == {SLUG}


def test_recent_orders_span_every_market_newest_last(bot):
    other = {**FILLED_ENTRY, "order_id": "ord-0", "slug": "btc-updown-5m-1790682900",
             "stages": {"ORDER_SUBMITTED": OPEN - 100_000, "FULL_FILL": OPEN - 99_000}}
    _forensics(bot, [other, FILLED_ENTRY, REJECTED_EXIT])
    server.LIVE_STATE.write_text(json.dumps({"order_marks": {
        "eth-updown-5m-1790683500": [{"id": "ord-5", "t": OPEN + 310_000, "side": "UP", "kind": "entry",
                                      "limit": 0.5, "size": 5.0, "outcome": "sent"}]}}), encoding="utf-8")
    out = server.recent_orders()
    assert [(o["id"], o["slug"]) for o in out["orders"]] == [
        ("ord-0", "btc-updown-5m-1790682900"), ("ord-1", SLUG), ("ord-2", SLUG),
        ("ord-5", "eth-updown-5m-1790683500")]                   # a live mark learns its market
    assert [o["id"] for o in server.recent_orders(limit=2)["orders"]] == ["ord-2", "ord-5"]
    assert server.recent_orders("../x") is None


@pytest.mark.parametrize("slug,bot_id", [
    ("../../etc/passwd", "main"),
    ("xrp-updown-5m-1790683200/../x", "main"),
    ("", "main"),
    (SLUG, "../elsewhere"),
])
def test_anything_that_is_not_a_market_or_a_bot_is_refused(bot, slug, bot_id):
    assert server.trade_detail(slug, bot=bot_id) is None


def test_earnings_days_start_at_midnight_in_the_display_timezone():
    gmt9 = timezone(timedelta(hours=9))
    # 16:00 UTC on the 29th is 01:00 on the 30th in GMT+9
    ts = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc).timestamp() * 1000
    rows = [earnings.Settlement(ts=ts, pnl=2.0, mode="paper", live=False)]
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc).timestamp() * 1000
    out = earnings.aggregate(rows, period="day", span=2, now=now, tz=gmt9)
    assert [(b["key"], b["pnl"]) for b in out["buckets"]] == [("2026-09-29", 0.0), ("2026-09-30", 2.0)]
    assert out["buckets"][1]["start_ts"] == datetime(2026, 9, 30, tzinfo=gmt9).timestamp() * 1000
    utc = earnings.aggregate(rows, period="day", span=2, now=now, tz=timezone.utc)
    assert [(b["key"], b["pnl"]) for b in utc["buckets"]] == [("2026-09-29", 2.0), ("2026-09-30", 0.0)]


def test_an_order_carries_its_ages_and_timeline_for_the_order_list():
    rec = {**REJECTED_EXIT, "quote_age_ms": 237.9, "spot_age_ms": 169.4, "t0_spot_event": OPEN + 163_900,
           "t9_exit_sent": OPEN + 164_120}
    o = server.order_from_forensics(rec)
    assert (o["book_age_ms"], o["spot_age_ms"]) == (237.9, 169.4)
    assert (o["t_event"], o["t_sent"]) == (OPEN + 163_900, OPEN + 164_120)
    # the live marks have no ages; a merge keeps the forensics record's
    merged = server.merge_orders([o], [{"id": o["id"], "t": o["t"], "outcome": "rejected", "kind": "TAKE_PROFIT"}])
    assert merged[0]["book_age_ms"] == 237.9
