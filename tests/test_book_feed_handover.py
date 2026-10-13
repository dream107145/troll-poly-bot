"""The book feed after 2026-09-28: bounded, cached books; a resubscribe that opens the
next connection BEFORE closing the current one; a subscription skip list."""
from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest

from troll_poly_bot import live as live_mod
from troll_poly_bot.live import LiveBook


def _snapshot(tid, n_levels=100, ts=1_000):
    return {"event_type": "book", "asset_id": tid, "timestamp": ts,
            "bids": [{"price": f"{0.50 - i * 0.001:.3f}", "size": "10"} for i in range(n_levels)],
            "asks": [{"price": f"{0.51 + i * 0.001:.3f}", "size": "10"} for i in range(n_levels)]}


def test_livebook_keeps_the_best_levels():
    lb = LiveBook("t")
    lb.apply_snapshot(_snapshot("t", 100), recv_ts=1_000, conn=1)
    assert len(lb.bids) == LiveBook.MAX_LEVELS and len(lb.asks) == LiveBook.MAX_LEVELS
    book = lb.to_order_book()
    assert book.asks[0].price == pytest.approx(0.51) and book.bids[0].price == pytest.approx(0.50)
    assert max(l.price for l in lb.to_order_book().bids) == pytest.approx(0.50)      # the best levels survived the trim
    # a delta shows in the next view
    lb.apply_change({"asset_id": "t", "price": "0.505", "size": "3", "side": "SELL"}, ts=1_001, recv_ts=1_001)
    assert lb.to_order_book().asks[0].price == pytest.approx(0.505)
    lb.apply_change({"asset_id": "t", "price": "0.505", "size": "0", "side": "SELL"}, ts=1_002, recv_ts=1_002)
    assert lb.to_order_book().asks[0].price == pytest.approx(0.51)


class _FakeWS:
    """A websocket that yields queued frames; closes when told to."""

    def __init__(self, frames: asyncio.Queue):
        self.frames = frames
        self.sent = []
        self.closed = False

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.frames.get()
        if item is None:
            raise StopAsyncIteration
        return item


class _Connect:
    """Stands in for websockets.connect: each call opens a new fake socket."""

    def __init__(self):
        self.sockets: list[_FakeWS] = []

    def __call__(self, *a, **k):
        ws = _FakeWS(asyncio.Queue())
        self.sockets.append(ws)

        @contextlib.asynccontextmanager
        async def cm():
            try:
                yield ws
            finally:
                ws.closed = True
        return cm()


def _bot_stub(tokens):
    """The slice of LiveBot the feed touches, on a bare object."""
    bot = SimpleNamespace()
    bot._stop = asyncio.Event()
    bot._resub = asyncio.Event()
    bot._book_conn = 0
    bot._book_conns_ok = set()
    bot._book_skip_assets = set()
    bot._book_last_venue_ts = 0.0
    bot.feed_health = {}
    bot.latency = SimpleNamespace(record_book=lambda ms: None)
    bot.clock = SimpleNamespace(now=lambda: 5_000.0)
    bot.token_index = {}
    for tid, asset in tokens:
        lm = SimpleNamespace(books={tid: LiveBook(tid)}, meta=SimpleNamespace(market=SimpleNamespace(asset=asset)))
        bot.token_index[tid] = (f"{asset.lower()}-slug", lm)
    bot._feed_state = lambda name, connected=None, error=None, message=False: live_mod.LiveBot._feed_state(bot, name, connected, error, message)
    bot._mark_due = lambda now: None
    bot._advance_book_clock = lambda ts, now: None
    for name in ("_book_tokens", "polymarket_feed", "_book_reader", "_handle_book_msg"):
        setattr(bot, name, getattr(live_mod.LiveBot, name).__get__(bot))
    bot._ping = live_mod.LiveBot._ping                      # a staticmethod: not rebound
    bot.RESUB_DEBOUNCE_S, bot.HANDOVER_TIMEOUT_S = 0.01, 1.0
    return bot


def test_book_tokens_skips_excluded_assets():
    bot = _bot_stub([("u1", "BTC"), ("d1", "BTC"), ("h1", "HYPE")])
    assert sorted(bot._book_tokens()) == ["d1", "h1", "u1"]
    bot._book_skip_assets = {"HYPE"}
    assert sorted(bot._book_tokens()) == ["d1", "u1"]


@pytest.mark.asyncio
async def test_resubscribe_opens_the_next_connection_before_closing_the_current(monkeypatch):
    connect = _Connect()
    monkeypatch.setattr(live_mod.websockets, "connect", connect)
    bot = _bot_stub([("u1", "BTC")])
    feed = asyncio.create_task(bot.polymarket_feed())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if connect.sockets:
            break
    ws1 = connect.sockets[0]
    assert json.loads(ws1.sent[0])["assets_ids"] == ["u1"]
    await ws1.frames.put(json.dumps(_snapshot("u1", 5, ts=4_000)))
    await asyncio.sleep(0.05)
    assert bot._book_conn == 1 and bot.token_index["u1"][1].books["u1"].conn == 1
    assert bot._book_conns_ok == {1}

    # a new market appears: the feed must open connection 2 while connection 1 still runs
    lm2 = SimpleNamespace(books={"u2": LiveBook("u2")}, meta=SimpleNamespace(market=SimpleNamespace(asset="ETH")))
    bot.token_index["u2"] = ("eth-slug", lm2)
    bot._resub.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(connect.sockets) == 2:
            break
    ws2 = connect.sockets[1]
    assert sorted(json.loads(ws2.sent[0])["assets_ids"]) == ["u1", "u2"]
    assert not ws1.closed, "the old connection was closed before the new one delivered its snapshots"
    assert bot._book_conns_ok == {1, 2}, "both connections count as current during the handover"
    # frames from the OLD connection still update the books meanwhile
    await ws1.frames.put(json.dumps({"event_type": "price_change", "timestamp": 4_100,
                                     "price_changes": [{"asset_id": "u1", "price": "0.52", "size": "7", "side": "SELL"}]}))
    await asyncio.sleep(0.05)
    assert bot.token_index["u1"][1].books["u1"].asks.get(0.52) == 7.0
    # the new connection delivers its snapshots: the handover completes and connection 1 is retired
    await ws2.frames.put(json.dumps([_snapshot("u1", 5, ts=4_200), _snapshot("u2", 5, ts=4_200)]))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if ws1.closed:
            break
    assert ws1.closed and not ws2.closed
    assert bot._book_conn == 2 and bot._book_conns_ok == {2}
    assert bot.token_index["u1"][1].books["u1"].conn == 2 and bot.token_index["u2"][1].books["u2"].conn == 2
    assert bot.feed_health["polymarket"].reconnects == 0, "a planned handover is not a drop"

    bot._stop.set()
    await ws2.frames.put(None)
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(feed, 2)


@pytest.mark.asyncio
async def test_a_dropped_connection_is_replaced_and_counted(monkeypatch):
    connect = _Connect()
    monkeypatch.setattr(live_mod.websockets, "connect", connect)
    monkeypatch.setattr(live_mod, "RECONNECT_MIN_S", 0.01)
    bot = _bot_stub([("u1", "BTC")])
    feed = asyncio.create_task(bot.polymarket_feed())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if connect.sockets:
            break
    ws1 = connect.sockets[0]
    await ws1.frames.put(json.dumps(_snapshot("u1", 5, ts=4_000)))
    await asyncio.sleep(0.05)

    class Boom(Exception):
        pass

    async def failing_next(self):
        raise Boom("1013 slow consumer")
    monkeypatch.setattr(_FakeWS, "__anext__", failing_next)     # every socket now dies on its next frame
    await ws1.frames.put("{}")                                  # wake the reader so it asks for that next frame
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(connect.sockets) >= 2:
            break
    assert len(connect.sockets) >= 2
    assert bot.feed_health["polymarket"].reconnects >= 1
    assert bot._book_conn >= 2
    bot._stop.set()
    with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
        await asyncio.wait_for(feed, 2)


async def test_a_drop_during_a_handover_is_not_a_feed_outage(monkeypatch):
    """The venue cuts the OLD connection as a slow consumer while the new one's snapshots
    land (every resubscribe on 2026-09-29). With a successor already delivering, the feed
    is not down: marking it so tripped the guard and its 30 s cooldown for nothing."""
    connect = _Connect()
    monkeypatch.setattr(live_mod.websockets, "connect", connect)
    bot = _bot_stub([("u1", "BTC")])
    bot._book_conn = 1
    bot._book_conns_ok = {1, 2}                                  # a handover is in progress
    bot._feed_state("polymarket", connected=True)

    class Boom(Exception):
        pass

    async def failing_next(self):
        raise Boom("1013 slow consumer")
    monkeypatch.setattr(_FakeWS, "__anext__", failing_next)
    await bot._book_reader(1, ["u1"], asyncio.Event())
    st = bot.feed_health["polymarket"]
    assert st.reconnects == 1 and st.connected is True, "a successor is live: not an outage"
    # the same drop with no successor is an outage
    bot._book_conns_ok = {1}
    await bot._book_reader(1, ["u1"], asyncio.Event())
    assert bot.feed_health["polymarket"].connected is False
