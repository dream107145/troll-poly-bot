"""Order lanes (2026-09-29): fill-or-kill orders signed and POSTed from dedicated threads,
in parallel, without breaking the exposure cap, the reconcile guards or result delivery."""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from types import SimpleNamespace

from troll_poly_bot.execution.paper import FeeModel, RejectReason
from troll_poly_bot.execution.polymarket import LiveCaps, PolymarketExchange
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.types import Order, Side, TimeInForce


def accepted(order_id, making, taking):
    return SimpleNamespace(ok=True, order_id=order_id, status="matched", making_amount=Decimal(str(making)),
                           taking_amount=Decimal(str(taking)), trade_ids=(), transactions_hashes=())


class SlowVenue:
    """A thread-safe fake venue whose POST takes ``rtt`` seconds and fills every order."""

    wallet_type = "DEPOSIT_WALLET"
    wallet = "0x" + "ab" * 20

    def __init__(self, rtt=0.2, balance=100.0, fill=True, sign_raises_for=()):
        self.rtt, self.balance, self.fill = rtt, balance, fill
        self.sign_raises_for = set(sign_raises_for)
        self.lock = threading.Lock()
        self.post_starts: list[float] = []
        self.inflight = 0
        self.max_inflight = 0
        self.n = 0
        self.on_post = None

    def create_market_order(self, **kw):
        if kw["token_id"] in self.sign_raises_for:
            raise RuntimeError("boom")
        return {"signed": kw}

    def post_order(self, signed):
        with self.lock:
            self.post_starts.append(time.perf_counter())
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            self.n += 1
            oid = f"v{self.n}"
        if self.on_post:
            self.on_post()
        time.sleep(self.rtt)
        with self.lock:
            self.inflight -= 1
        kw = signed["signed"]
        if not self.fill:
            return SimpleNamespace(ok=False, code="fok_not_filled", message="killed")
        amount = float(kw["amount"])
        price = float(kw["max_price"])
        return accepted(oid, amount, round(amount / price, 2))           # USDC paid, shares got

    def get_balance_allowance(self, *, asset_type):
        return SimpleNamespace(balance=int(self.balance * 1e6), allowances={"0xexchange": int(1e9)})

    def close(self):
        pass


def _ex(venue, lanes=4, max_open=100.0):
    ex = PolymarketExchange(venue, FeeModel.from_schedule(FeeSchedule()), bankroll=100.0,
                            caps=LiveCaps(max_order_usdc=10.0, max_open_usdc=max_open, max_orders_per_hour=1000,
                                          kill_file="KILL-nope-lanes"),
                            armed=True, clock=lambda: time.time() * 1000.0, wallet=SlowVenue.wallet, order_lanes=lanes)
    ex.authenticate()
    return ex


def _order(token, price=0.50, size=10.0):
    return Order(token_id=token, side=Side.BUY, price=price, size=size, tif=TimeInForce.FOK,
                 expected_price=price, tag="UP")


def _collect(ex, n, timeout=5.0):
    out, t0 = [], time.monotonic()
    while len(out) < n and time.monotonic() - t0 < timeout:
        out.extend(ex.step(time.time() * 1000.0))
        time.sleep(0.005)
    return out


def test_simultaneous_orders_are_posted_in_parallel():
    venue = SlowVenue(rtt=0.2)
    ex = _ex(venue, lanes=4)
    t0 = time.perf_counter()
    for i in range(4):
        ex.submit(_order(f"tok{i}"), time.time() * 1000.0)
    res = _collect(ex, 4)
    elapsed = time.perf_counter() - t0
    assert len(res) == 4 and all(r.fills for r in res)
    assert venue.max_inflight == 4, "the four POSTs overlapped"
    assert max(venue.post_starts) - min(venue.post_starts) < 0.1
    assert elapsed < 0.5, f"one round trip for all four, not four in a row ({elapsed:.2f}s)"
    assert all(ex.positions[f"tok{i}"].shares > 0 for i in range(4))
    ex.stop_lanes()


def test_the_default_is_the_original_single_worker():
    venue = SlowVenue(rtt=0.0)
    ex = _ex(venue, lanes=0)
    ex.submit(_order("tok0"), time.time() * 1000.0)
    assert len(ex._queue) == 1 and not ex._lanes


def test_parallel_entries_cannot_all_pass_the_exposure_cap():
    venue = SlowVenue(rtt=0.2)
    ex = _ex(venue, lanes=4, max_open=10.0)                    # room for two 5-USDC entries
    for i in range(4):
        ex.submit(_order(f"tok{i}", price=0.50, size=10.0), time.time() * 1000.0)
    res = _collect(ex, 4)
    posted = [r for r in res if r.fills]
    refused = [r for r in res if r.reject_reason is RejectReason.LIVE_CAPS]
    assert len(posted) == 2 and len(refused) == 2, [r.reject_reason for r in res]
    assert venue.n == 2
    assert not ex._reservations, "every reservation is released"
    ex.stop_lanes()


def test_a_reconcile_during_a_post_does_not_judge_the_venue():
    venue = SlowVenue(rtt=0.3)
    ex = _ex(venue, lanes=2)
    ex._last_fill_mono = 0.0
    started = threading.Event()
    venue.on_post = started.set

    ex.submit(_order("tok0", price=0.50, size=10.0), time.time() * 1000.0)
    assert started.wait(2.0)
    venue.balance -= 5.0                                        # the venue has matched; our reply is not back yet
    ex.reconcile()
    assert ex.balance_drift == 0.0 and ex.reconcile_deferred >= 1
    res = _collect(ex, 1)
    assert res and res[0].fills
    # now quiet: the booked fill explains the cash (the fee is our estimate; allow it)
    ex._last_fill_mono = time.monotonic() - ex.CASH_SETTLE_GRACE_S - 1.0
    ex.reconcile()
    assert abs(ex.balance_drift) < 0.5
    ex.stop_lanes()


def test_no_result_is_lost_while_the_loop_collects_concurrently():
    venue = SlowVenue(rtt=0.01, fill=False)
    ex = _ex(venue, lanes=4)
    n = 200
    got = []
    stop = threading.Event()

    def collector():
        while not stop.is_set():
            got.extend(ex.step(time.time() * 1000.0))

    th = threading.Thread(target=collector)
    th.start()
    for i in range(n):
        ex.submit(_order(f"tok{i % 7}"), time.time() * 1000.0)
    t0 = time.monotonic()
    while len(got) < n and time.monotonic() - t0 < 10.0:
        time.sleep(0.01)
    stop.set()
    th.join()
    got.extend(ex.step(time.time() * 1000.0))
    assert len(got) == n
    ex.stop_lanes()


def test_a_failing_order_does_not_kill_its_lane():
    venue = SlowVenue(rtt=0.0, sign_raises_for={"bad"})
    ex = _ex(venue, lanes=1)
    ex.submit(_order("bad"), time.time() * 1000.0)
    ex.submit(_order("good"), time.time() * 1000.0)
    res = _collect(ex, 2)
    by_token = {r.order.token_id: r for r in res}
    assert by_token["bad"].reject_reason is RejectReason.API_ERROR
    assert by_token["good"].fills
    ex.stop_lanes()
