"""The maker entry: rest a limit at the model's price instead of hitting the ask.

Every taker fill-or-kill the live bot sent on 2026-09-22 was killed -- the
model finds edge in a quote that is about to vanish, and a taker loses that
race at any latency. These tests pin the alternative end to end: the cost
model prices a resting order fee-free, the paper engine rests it and fills
it only when traded through, and the real adapter posts it post-only,
watches it, books maker fills at our price, and cancels it on time.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from troll_poly_bot.execution.latency import INSTANT, LatencyModel
from troll_poly_bot.execution.paper import FeeModel, PaperExchange, RejectReason
from troll_poly_bot.execution.polymarket import LiveCaps, PolymarketExchange
from troll_poly_bot.signals.costs import CostModel, FeeSchedule
from troll_poly_bot.types import BookLevel, Order, OrderBook, OrderState, Side, TimeInForce

TOKEN = "1234567890"
NOW = 1_000_000.0                      # ms


def _resting(price=0.40, size=10.0, ttl_ms=20_000.0, side=Side.BUY):
    return Order(token_id=TOKEN, side=side, price=price, size=size, tif=TimeInForce.GTC,
                 expected_price=price, tag="UP maker", expires_ts=NOW + ttl_ms)


def _book(bid, ask, size=200.0):
    return OrderBook(token_id=TOKEN, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size)], ts=NOW)


# ───────────────────────────────── cost model ─────────────────────────────────


def test_a_resting_order_is_priced_fee_free_and_without_arrival_slippage():
    cm = CostModel(fee=FeeSchedule(), expected_slippage=0.02, uncertainty_charge=0.0)
    taker = cm.evaluate(0.60, 0.50, 0.49)
    maker = cm.evaluate(0.60, 0.50, 0.49, maker=True)
    assert taker.fee > 0 and taker.slippage == pytest.approx(0.02)
    assert maker.fee == 0.0 and maker.slippage == 0.0
    assert maker.net_edge == pytest.approx(taker.net_edge + taker.fee + 0.02)


# ───────────────────────────── strategy engine ───────────────────────────────


def test_engine_rests_below_the_ask_and_builds_a_gtc_order():
    """Same mispriced offer the taker tests use: UP offered at 0.55 against a
    model well above it. The taker crosses to 0.56; the maker rests at 0.54,
    one tick below the ask, and the order it builds is GTC with a deadline."""
    from test_engine import _book as ebook, _engine, _eval, _meta, T0
    from troll_poly_bot.strategy.engine import Reason
    eng, _ = _engine(min_net_edge=0.05, uncertainty_charge=0.0, maker_entry=True, maker_ttl_s=20.0)
    ev = _eval(eng, _meta(strike=100.0), ebook("UP", 0.54, 0.55), ebook("DOWN", 0.44, 0.45), spot=100.4)
    assert ev.tradeable, (ev.reason, ev.detail, ev.p_used)
    assert ev.side == "UP"
    assert ev.limit_price == pytest.approx(0.54) and ev.expected_price == pytest.approx(0.54)
    assert ev.limit_price < 0.55                                          # never at or through the ask
    order = eng.to_order(ev, now_ms=T0 + 100_000.0)
    assert order.tif is TimeInForce.GTC and order.price == pytest.approx(0.54)
    assert order.expires_ts == pytest.approx(T0 + 120_000.0)
    assert "maker" in order.tag
    # revalidation: still below the ask -> post; ask fell onto us -> do not become a taker
    assert eng.revalidate(ev, ebook("UP", 0.54, 0.55)) is None
    assert eng.revalidate(ev, ebook("UP", 0.53, 0.54)) is Reason.HIGH_SLIPPAGE


def test_engine_taker_path_is_unchanged_when_maker_entry_is_off():
    from test_engine import _book as ebook, _engine, _eval, _meta
    eng, _ = _engine(min_net_edge=0.05, uncertainty_charge=0.0)
    ev = _eval(eng, _meta(strike=100.0), ebook("UP", 0.54, 0.55), ebook("DOWN", 0.44, 0.45), spot=100.4)
    assert ev.tradeable and ev.limit_price == pytest.approx(0.56)
    assert eng.to_order(ev).tif is TimeInForce.FOK


# ─────────────────────────────── paper engine ────────────────────────────────


def _paper():
    return PaperExchange(LatencyModel(INSTANT, seed=0), FeeModel.from_schedule(FeeSchedule()),
                         starting_balance=100.0)


def test_paper_rests_the_order_and_expires_it_untouched():
    ex = _paper()
    ex.submit(_resting(price=0.40, ttl_ms=5_000), NOW)
    rested = ex.step(NOW + 1, lambda t: _book(0.39, 0.45))
    assert [r.reject_reason for r in rested] == [RejectReason.NONE] and not rested[0].fills
    assert ex.step(NOW + 3_000, lambda t: _book(0.39, 0.45)) == []          # still waiting
    done = ex.step(NOW + 5_001, lambda t: _book(0.39, 0.45))
    assert [r.reject_reason for r in done] == [RejectReason.EXPIRED]
    assert done[0].order.state is OrderState.CANCELLED and not done[0].fills


def test_paper_fills_a_resting_order_only_when_traded_through_and_charges_no_fee():
    ex = _paper()
    order = _resting(price=0.40)
    ex.submit(order, NOW)
    ex.step(NOW + 1, lambda t: _book(0.39, 0.45))
    assert ex.step(NOW + 100, lambda t: _book(0.39, 0.40)) == []           # AT our price: no fill
    res = ex.step(NOW + 200, lambda t: _book(0.38, 0.39))                  # through it: filled
    assert len(res) == 1 and res[0].reject_reason is RejectReason.NONE
    (fill,) = res[0].fills
    assert fill.price == pytest.approx(0.40) and fill.size == pytest.approx(10.0)
    assert fill.fee == 0.0                                                # takers pay, makers do not
    assert order.state is OrderState.FILLED


def test_paper_cancels_a_resting_order_when_the_market_closes_under_it():
    ex = _paper()
    ex.submit(_resting(price=0.40, ttl_ms=60_000), NOW)
    ex.step(NOW + 1, lambda t: _book(0.39, 0.45))
    res = ex.step(NOW + 10_000, lambda t: _book(0.39, 0.45), close_ts_of=lambda t: NOW + 9_000)
    assert [r.reject_reason for r in res] == [RejectReason.MARKET_CLOSED]


# ────────────────────────────── the real adapter ─────────────────────────────


class RequestRejectedError(Exception):
    """Same name as the SDK's; the adapter matches by name."""
    code = None


class RestingClient:
    """Enough of polymarket.SecureClient for resting orders."""

    wallet_type = "DEPOSIT_WALLET"
    wallet = "0x" + "ab" * 20

    def __init__(self, post_raises=None):
        self.limit_kw: list = []
        self.posted: list = []
        self.cancelled: list = []
        self.orders: dict = {}
        self.post_raises = post_raises
        self._seq = 0

    def create_limit_order(self, **kw):
        self.limit_kw.append(kw)
        return {"signed": kw}

    def post_order(self, signed):
        self.posted.append(signed)
        if self.post_raises:
            raise self.post_raises
        self._seq += 1
        oid = f"venue-{self._seq}"
        self.orders[oid] = SimpleNamespace(size_matched="0", status="LIVE",
                                           original_size=signed["signed"]["size"])
        return SimpleNamespace(ok=True, order_id=oid, status="live", making_amount=None,
                               taking_amount=None, trade_ids=(), transactions_hashes=())

    def get_order(self, *, order_id):
        if order_id not in self.orders:
            raise RuntimeError("404 order not found")
        return self.orders[order_id]

    def cancel_order(self, *, order_id):
        self.cancelled.append(order_id)
        self.orders.pop(order_id, None)
        return SimpleNamespace(canceled=[order_id])

    def get_balance_allowance(self, *, asset_type):
        return SimpleNamespace(balance=int(50e6), allowances={"0xexchange": int(1000e6)})

    def close(self):
        pass


def _ex(client, **caps):
    caps = LiveCaps(max_orders_per_hour=100, kill_file="none", **caps)
    return PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=20.0,
                              caps=caps, armed=True, clock=lambda: NOW)


def _send(ex, order):
    ex.submit(order, NOW)
    if ex._queue:
        ex._place(ex._queue.popleft())
    return ex.step(NOW + 1)


def test_adapter_posts_a_post_only_gtc_with_a_venue_backstop_and_tracks_it():
    client = RestingClient()
    ex = _ex(client)
    assert _send(ex, _resting(price=0.40, size=10.0)) == []              # nothing to report yet
    (kw,) = client.limit_kw
    assert kw["side"] == "BUY" and kw["price"] == "0.40" and kw["size"] == "10.00"
    assert kw["post_only"] is True
    assert kw["expiration"] >= int(__import__("time").time()) + 180       # SDK: >= 3 minutes out
    assert len(ex.open_orders) == 1
    assert ex._open_notional() == pytest.approx(4.0)                     # reserved while it rests


def test_a_maker_fill_is_booked_at_our_price_with_no_fee():
    client = RestingClient()
    ex = _ex(client)
    order = _resting(price=0.40, size=10.0)
    _send(ex, order)
    client.orders["venue-1"].size_matched = "10"
    client.orders["venue-1"].status = "MATCHED"
    ex._poll_open_orders()
    res = ex.step(NOW + 2)
    assert len(res) == 1 and res[0].reject_reason is RejectReason.NONE
    (fill,) = res[0].fills
    assert fill.price == pytest.approx(0.40) and fill.size == pytest.approx(10.0) and fill.fee == 0.0
    assert ex.positions[TOKEN].shares == pytest.approx(10.0)
    assert ex.balance == pytest.approx(20.0 - 4.0)
    assert not ex.open_orders and order.state is OrderState.FILLED


def test_an_unfilled_resting_order_is_cancelled_after_its_ttl():
    client = RestingClient()
    ex = _ex(client)
    order = _resting(price=0.40, size=10.0, ttl_ms=-1.0)                 # deadline already passed
    _send(ex, order)
    ex._poll_open_orders()
    assert client.cancelled == ["venue-1"]
    res = ex.step(NOW + 2)
    assert [r.reject_reason for r in res] == [RejectReason.EXPIRED] and not res[0].fills
    assert ex.consecutive_errors == 0 and not ex.halted and not ex.open_orders


def test_a_partial_fill_survives_the_cancel():
    client = RestingClient()
    ex = _ex(client)
    order = _resting(price=0.40, size=10.0, ttl_ms=-1.0)
    _send(ex, order)
    client.orders["venue-1"].size_matched = "4"
    ex._poll_open_orders()                                               # books 4, then cancels
    res = ex.step(NOW + 2)
    assert res[0].reject_reason is RejectReason.NONE
    assert sum(f.size for f in res[0].fills) == pytest.approx(4.0)
    assert order.state is OrderState.PARTIAL and ex.positions[TOKEN].shares == pytest.approx(4.0)


def test_a_vanished_order_is_retired_without_an_error():
    client = RestingClient()
    ex = _ex(client)
    _send(ex, _resting())
    client.orders.clear()                                                # the venue forgot it
    ex._poll_open_orders()
    assert [r.reject_reason for r in ex.step(NOW + 2)] == [RejectReason.EXPIRED]
    assert ex.consecutive_errors == 0


def test_shutdown_cancels_everything_resting():
    client = RestingClient()
    ex = _ex(client, max_open_usdc=100.0)
    _send(ex, _resting(price=0.40, size=10.0))
    _send(ex, _resting(price=0.30, size=10.0))
    assert len(ex.open_orders) == 2
    ex.cancel_open_orders()
    assert sorted(client.cancelled) == ["venue-1", "venue-2"] and not ex.open_orders


def test_a_post_only_order_that_would_cross_is_not_an_api_error():
    client = RestingClient(post_raises=RequestRejectedError("post only order would cross the book"))
    ex = _ex(client)
    res = _send(ex, _resting())
    assert res[0].reject_reason is RejectReason.NO_LIQUIDITY
    assert ex.consecutive_errors == 0 and not ex.halted and not ex.open_orders


def test_reserved_notional_counts_toward_the_open_cap():
    client = RestingClient()
    ex = _ex(client, max_open_usdc=6.0)
    assert _send(ex, _resting(price=0.40, size=10.0)) == []              # 4.00 reserved
    res = _send(ex, _resting(price=0.30, size=10.0))                     # + 3.00 > 6.00
    assert res[0].reject_reason is RejectReason.LIVE_CAPS


def test_dry_run_signs_a_resting_order_but_never_posts_it():
    client = RestingClient()
    ex = PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=20.0,
                            caps=LiveCaps(max_orders_per_hour=100, kill_file="none"),
                            armed=False, clock=lambda: NOW)
    res = _send(ex, _resting())
    assert res[0].reject_reason is RejectReason.DRY_RUN
    assert client.limit_kw and not client.posted and not ex.open_orders


# ───────────────────────── 2026-10-08: the strategy's own cancel (a pair bid re-placed lower) ─────────────────────────

class KeepingClient(RestingClient):
    """The real venue: a cancelled order stays readable, with what matched before the cancel."""

    def cancel_order(self, *, order_id):
        self.cancelled.append(order_id)
        if order_id in self.orders:
            self.orders[order_id].status = "CANCELED"
        return SimpleNamespace(canceled=[order_id])


def test_cancel_pulls_the_resting_order_and_ends_it_with_one_result():
    client = KeepingClient()
    ex = _ex(client)
    order = _resting(price=0.40, size=10.0)
    _send(ex, order)
    assert ex.cancel_reports_result
    ex.cancel(order.order_id, NOW)
    assert client.cancelled == []                                        # non-blocking: the worker sends it
    ex._process_cancels()
    assert client.cancelled == ["venue-1"] and not ex.open_orders and not ex._cancel_wanted
    res = ex.step(NOW + 2)
    assert [r.reject_reason for r in res] == [RejectReason.EXPIRED] and not res[0].fills
    assert res[0].order.order_id == order.order_id and order.state is OrderState.REJECTED   # = ended unfilled


def test_a_fill_that_beat_the_cancel_is_booked_in_its_result():
    client = KeepingClient()
    ex = _ex(client)
    order = _resting(price=0.40, size=10.0)
    _send(ex, order)
    client.orders["venue-1"].size_matched = "6"                          # matched after the last poll
    ex.cancel(order.order_id, NOW)
    ex._process_cancels()
    (res,) = ex.step(NOW + 2)
    assert res.reject_reason is RejectReason.NONE and sum(f.size for f in res.fills) == pytest.approx(6.0)
    assert ex.positions[TOKEN].shares == pytest.approx(6.0) and order.state is OrderState.PARTIAL


def test_a_cancel_of_a_queued_order_drops_it_and_an_unknown_id_is_given_up():
    client = KeepingClient()
    ex = _ex(client)
    order = _resting(price=0.40, size=10.0)
    ex.submit(order, NOW)                                                # queued, not posted yet
    ex.cancel(order.order_id, NOW)
    ex._drop_queued_cancels()
    assert not ex._queue and not ex._cancel_wanted and client.posted == []
    assert [r.reject_reason for r in ex.step(NOW + 2)] == [RejectReason.EXPIRED]
    ex.cancel("ord-nope", NOW)
    ex._process_cancels()
    assert "ord-nope" in ex._cancel_wanted                               # it may still be posting: wait
    ex._cancel_wanted["ord-nope"] -= 31.0
    ex._process_cancels()
    assert not ex._cancel_wanted and client.cancelled == []
