"""The real-money adapter, against a fake venue: every lock, every path."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from troll_poly_bot.execution.paper import FeeModel, RejectReason
from troll_poly_bot.execution.polymarket import (
    ACK_PHRASE, LiveCaps, LiveConfigError, LiveCredentials, PolymarketExchange, _parse_balance,
)
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.types import Order, Side, TimeInForce

TOKEN = "1234567890"
COND = "0x" + "c0" * 32


class InsufficientLiquidityError(Exception):
    """Same name as the SDK's; the adapter matches exceptions by name."""


def accepted(order_id, making, taking, status="matched"):
    return SimpleNamespace(ok=True, order_id=order_id, status=status, making_amount=Decimal(str(making)),
                           taking_amount=Decimal(str(taking)), trade_ids=(), transactions_hashes=())


def rejected(code, message=""):
    return SimpleNamespace(ok=False, code=code, message=message)


class FakeClient:
    """Just enough of polymarket.SecureClient to exercise the adapter."""

    wallet_type = "DEPOSIT_WALLET"
    wallet = "0x" + "ab" * 20

    def __init__(self, balance=50.0, allowance=1_000.0, responses=None, post_raises=None, sign_raises=None,
                 redeem_raises=None):
        self.balance, self.allowance = balance, allowance
        self.responses = list(responses or [])
        self.post_raises, self.sign_raises, self.redeem_raises = post_raises, sign_raises, redeem_raises
        self.signed: list = []
        self.posted: list = []
        self.orders: dict = {}
        self.redeemed: list = []
        self.closed = False

    def create_market_order(self, **kw):
        if self.sign_raises:
            raise self.sign_raises
        self.signed.append(kw)
        return {"signed": kw}

    def post_order(self, signed):
        self.posted.append(signed)
        if self.post_raises:
            raise self.post_raises
        return self.responses.pop(0) if self.responses else rejected("fok_not_filled", "killed")

    def get_order(self, *, order_id):
        if order_id not in self.orders:
            raise RuntimeError("404 order not found")
        return self.orders[order_id]

    def get_balance_allowance(self, *, asset_type):
        assert asset_type == "COLLATERAL"
        return SimpleNamespace(balance=int(self.balance * 1e6), allowances={"0xexchange": int(self.allowance * 1e6)})

    def redeem_positions(self, *, condition_id):
        if self.redeem_raises:
            raise self.redeem_raises
        self.redeemed.append(condition_id)
        return SimpleNamespace(wait=lambda: None)

    def close(self):
        self.closed = True


def _order(side=Side.BUY, price=0.40, size=10.0, tag="UP"):
    return Order(token_id=TOKEN, side=side, price=price, size=size, tif=TimeInForce.FOK,
                 expected_price=price, tag=tag)


def _ex(client, armed=True, bankroll=20.0, caps=None, tmp_path=None, can_redeem=False):
    caps = caps or LiveCaps(kill_file=str((tmp_path or "") and (tmp_path / "KILL")) if tmp_path else "KILL-nope")
    ex = PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=bankroll, caps=caps,
                            armed=armed, clock=lambda: 1_000.0, wallet=FakeClient.wallet, can_redeem=can_redeem)
    ex.authenticate()
    return ex


def _run(ex, order, now=1_000.0):
    ex.submit(order, now)
    if ex._queue:
        ex._place(ex._queue.popleft())
    return ex.step(now + 1)


def test_credentials_from_env_and_redaction():
    env = {"TPB_POLY_PRIVATE_KEY": "0x" + "ab" * 32, "TPB_POLY_WALLET": "0x" + "cd" * 20}
    c = LiveCredentials.from_env(env)
    assert c.wallet == env["TPB_POLY_WALLET"] and c.funder == c.wallet
    assert c.signature_type is None and c.api_key is None and not c.can_redeem
    red = c.redacted()
    assert "abab" not in red["private_key"][:-4] and red["api_creds"] == "derived" and red["relayer_key"] == "none"
    # the older variable names still work, and the signature type is only informational now
    old = LiveCredentials.from_env({"TPB_POLY_PRIVATE_KEY": "k", "TPB_POLY_FUNDER": env["TPB_POLY_WALLET"],
                                    "TPB_POLY_SIGNATURE_TYPE": "3"})
    assert old.wallet == env["TPB_POLY_WALLET"] and old.signature_type == 3
    # the wallet may be omitted: the SDK derives the signer's Deposit Wallet
    assert LiveCredentials.from_env({"TPB_POLY_PRIVATE_KEY": "k"}).wallet is None
    relay = LiveCredentials.from_env({"TPB_POLY_PRIVATE_KEY": "k", "TPB_POLY_RELAYER_API_KEY": "r",
                                      "TPB_POLY_RELAYER_API_KEY_ADDRESS": "0x" + "ef" * 20})
    assert relay.can_redeem and relay.redacted()["relayer_key"] == "given"
    for bad in ({}, {"TPB_POLY_PRIVATE_KEY": "k", "TPB_POLY_WALLET": "nope"},
                {"TPB_POLY_PRIVATE_KEY": "k", "TPB_POLY_SIGNATURE_TYPE": "7"},
                {"TPB_POLY_PRIVATE_KEY": "k", "TPB_POLY_RELAYER_API_KEY": "r"}):
        with pytest.raises(LiveConfigError):
            LiveCredentials.from_env(bad)
    assert ACK_PHRASE == "I_UNDERSTAND_REAL_MONEY"


def test_parse_balance_shapes():
    # the SDK model: integer base units
    assert _parse_balance(SimpleNamespace(balance=100_270_000, allowances={"a": 10 ** 30, "b": 5})) == \
        (100.27, 10 ** 30 / 1e6)
    assert _parse_balance(SimpleNamespace(balance=0, allowances={})) == (0.0, None)
    # the raw REST shapes
    assert _parse_balance({"balance": "12500000", "allowances": {"a": "1000000", "b": "5000000"}}) == (12.5, 5.0)
    assert _parse_balance({"balance": "1000000", "allowance": "2000000"}) == (1.0, 2.0)
    assert _parse_balance({"balance": "0"}) == (0.0, None)
    with pytest.raises(LiveConfigError):
        _parse_balance("garbage")


def test_authenticate_caps_bankroll_and_refuses_no_allowance():
    ex = _ex(FakeClient(balance=12.0), bankroll=100.0)
    assert ex.bankroll == 12.0 and ex.balance == 12.0 and ex.venue_balance == 12.0
    with pytest.raises(LiveConfigError):
        _ex(FakeClient(balance=12.0, allowance=0.0))
    with pytest.raises(LiveConfigError):
        _ex(FakeClient(balance=0.5))


def test_dry_run_signs_a_bounded_fok_market_order_but_never_posts():
    client = FakeClient()
    ex = _ex(client, armed=False)
    res = _run(ex, _order(price=0.40, size=10.0))
    assert len(client.signed) == 1 and client.posted == []
    kw = client.signed[0]
    assert kw["side"] == "BUY" and kw["order_type"] == "FOK"
    assert kw["amount"] == "4.00" and kw["max_price"] == "0.40" and kw["token_id"] == TOKEN
    assert res[0].reject_reason is RejectReason.DRY_RUN
    assert ex.balance == 20.0 and not ex.positions


def test_armed_buy_fill_uses_the_venue_matched_amounts():
    client = FakeClient(responses=[accepted("0xo1", making=3.9, taking=10)])
    ex = _ex(client)
    res = _run(ex, _order(price=0.40, size=10.0))
    assert client.posted and res[0].reject_reason is RejectReason.NONE
    f = res[0].fills[0]
    assert f.size == 10.0 and f.price == pytest.approx(0.39)      # better than the 0.40 bound
    assert f.fee == pytest.approx(0.07 * 0.39 * 0.61 * 10)
    assert ex.positions[TOKEN].shares == 10.0
    assert ex.balance == pytest.approx(20.0 - 3.9 - f.fee)
    assert ex.latency_report()["orders_filled"] == 1


def test_unmatched_fok_and_missing_amounts_are_rejected_not_filled():
    client = FakeClient(responses=[rejected("fok_not_filled", "order couldn't be fully filled"),
                                   accepted("0xo2", making=0, taking=0)])
    ex = _ex(client)
    assert _run(ex, _order())[0].reject_reason is RejectReason.FOK_UNFILLABLE
    # amounts missing and the order record is gone -> no fill either
    assert _run(ex, _order())[0].reject_reason is RejectReason.FOK_UNFILLABLE
    assert not ex.fills and ex.consecutive_errors == 0 and not ex.halted


def test_missing_amounts_recovered_from_the_order_record():
    client = FakeClient(responses=[accepted("0xo3", making=0, taking=0)])
    client.orders["0xo3"] = SimpleNamespace(size_matched=Decimal("8"), price=Decimal("0.42"))
    ex = _ex(client)
    res = _run(ex, _order(price=0.45, size=8.0))
    assert res[0].fills[0].size == 8.0 and res[0].fills[0].price == pytest.approx(0.42)


def test_caps_are_checked_before_anything_is_signed(tmp_path):
    client = FakeClient()
    ex = _ex(client, caps=LiveCaps(max_order_usdc=2.0, kill_file=str(tmp_path / "KILL")))
    res = _run(ex, _order(price=0.40, size=10.0))            # 4.00 > 2.00
    assert res[0].reject_reason is RejectReason.LIVE_CAPS and client.signed == []
    (tmp_path / "KILL").write_text("stop")
    res = _run(ex, _order(price=0.10, size=10.0))
    assert res[0].reject_reason is RejectReason.KILL_SWITCH and client.signed == []


def test_open_notional_and_rate_caps():
    client = FakeClient(responses=[accepted("a", making=4, taking=10)])
    ex = _ex(client, caps=LiveCaps(max_order_usdc=5.0, max_open_usdc=6.0, max_orders_per_hour=1, kill_file="none"))
    assert _run(ex, _order(price=0.40, size=10.0))[0].reject_reason is RejectReason.NONE
    r = _run(ex, _order(price=0.40, size=10.0))              # open 4 + 4 > 6
    assert r[0].reject_reason is RejectReason.LIVE_CAPS
    ex.caps.max_open_usdc = 100.0
    r = _run(ex, _order(price=0.10, size=10.0))              # one order per hour already sent
    assert r[0].reject_reason is RejectReason.LIVE_CAPS


def test_api_errors_halt_after_five_in_a_row():
    client = FakeClient(post_raises=RuntimeError("503"))
    ex = _ex(client, caps=LiveCaps(max_orders_per_hour=100, kill_file="none"))
    for _ in range(5):
        assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.API_ERROR
    assert ex.halted and "5 consecutive" in ex.halted
    assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.HALTED


def test_venue_rejections_that_are_not_a_no_match_count_as_api_errors():
    msg = "the order signer address has to be the address of the API KEY"
    client = FakeClient(responses=[rejected("unknown", msg)] * 5)
    ex = _ex(client, caps=LiveCaps(max_orders_per_hour=100, kill_file="none"))
    for _ in range(5):
        assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.API_ERROR
    assert ex.halted and "API KEY" in ex.last_error


def test_no_liquidity_at_signing_is_unfillable_not_an_api_error():
    client = FakeClient(sign_raises=InsufficientLiquidityError("Insufficient liquidity for full fill."))
    ex = _ex(client)
    res = _run(ex, _order(price=0.10, size=10.0))
    assert res[0].reject_reason is RejectReason.FOK_UNFILLABLE
    assert client.posted == [] and ex.consecutive_errors == 0


class RequestRejectedError(Exception):
    """Same name and shape as the SDK's: it carries the venue's ``code``."""

    def __init__(self, message, *, status=400, code=None):
        super().__init__(message)
        self.status, self.code = status, code


@pytest.mark.parametrize("exc", [
    RequestRejectedError("order couldn't be fully filled. FOK orders are fully filled or killed."),
    RequestRejectedError("killed", code="fok_not_filled"),
    RequestRejectedError("no match", code="unmatched"),
    InsufficientLiquidityError("Insufficient liquidity for full fill."),
])
def test_a_killed_fok_raised_at_post_time_is_not_an_api_error(exc):
    """The SDK raises a killed FOK instead of returning an unfilled response.

    Five of those in a row halted the live bot on 2026-09-22 as '5
    consecutive API errors'. A kill is the order's normal other outcome; six
    in a row must leave the error counter at zero and the bot trading.
    """
    client = FakeClient(post_raises=exc)
    ex = _ex(client, caps=LiveCaps(max_orders_per_hour=100, kill_file="none"))
    for _ in range(6):
        assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.FOK_UNFILLABLE
    assert ex.consecutive_errors == 0 and not ex.halted
    assert len(client.posted) == 6                    # they did reach the venue
    assert ex.n_rejected[RejectReason.FOK_UNFILLABLE] == 6


def test_the_venue_size_floor_is_our_mistake_not_an_api_error():
    """Seen live: 10.53 sh @ 0.07 = $0.73 -> 'invalid amount for a marketable
    BUY order'. It was counted toward the consecutive-error halt; it must not be."""
    exc = RequestRejectedError("invalid amount for a marketable BUY order ($0.73), minimum is $1")
    client = FakeClient(post_raises=exc)
    ex = _ex(client, caps=LiveCaps(max_orders_per_hour=100, kill_file="none"))
    for _ in range(6):
        assert _run(ex, _order(price=0.07, size=10.53))[0].reject_reason is RejectReason.MIN_SIZE
    assert ex.consecutive_errors == 0 and not ex.halted


def test_the_hourly_cap_never_blocks_a_sell():
    """Entries may exhaust the budget; a position must still be exitable."""
    client = FakeClient(post_raises=RequestRejectedError("killed", code="fok_not_filled"))
    ex = _ex(client, caps=LiveCaps(max_orders_per_hour=1, kill_file="none"))
    assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.FOK_UNFILLABLE
    assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.LIVE_CAPS
    res = _run(ex, _order(side=Side.SELL, price=0.60, size=10.0))
    assert res[0].reject_reason is RejectReason.FOK_UNFILLABLE     # sent, not capped
    assert len(client.posted) == 2


def test_a_killed_fok_still_counts_toward_the_hourly_cap():
    """It was posted, so it spends an order slot -- same as a killed response."""
    exc = RequestRejectedError("killed", code="fok_not_filled")
    client = FakeClient(post_raises=exc)
    ex = _ex(client, caps=LiveCaps(max_orders_per_hour=2, kill_file="none"))
    assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.FOK_UNFILLABLE
    assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.FOK_UNFILLABLE
    assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.LIVE_CAPS


def test_other_rejections_at_post_time_still_count_as_api_errors():
    """The fix must not swallow real faults: a signer mismatch is still an error."""
    exc = RequestRejectedError("the order signer address has to be the address of the API KEY", code="invalid_signature")
    client = FakeClient(post_raises=exc)
    ex = _ex(client, caps=LiveCaps(max_orders_per_hour=100, kill_file="none"))
    for _ in range(5):
        assert _run(ex, _order(price=0.10, size=10.0))[0].reject_reason is RejectReason.API_ERROR
    assert ex.halted and "5 consecutive" in ex.halted


def test_settlement_carries_redemption_and_reconcile_retires_it():
    client = FakeClient(balance=50.0, responses=[accepted("a", making=4, taking=10)])
    ex = _ex(client, bankroll=20.0, caps=LiveCaps(kill_file="none"))
    _run(ex, _order(price=0.40, size=10.0))
    fee = ex.fills[0].fee
    pnl = ex.settle_market(TOKEN, won=True)
    assert pnl == pytest.approx(10.0 - 4.0 - fee)
    assert ex.pending_redemption == 10.0 and ex.positions[TOKEN].shares == 0.0
    assert ex.equity() == pytest.approx(20.0 - 4.0 - fee + 10.0)
    assert not ex._redeem_pending                            # no relayer key: nothing queued
    # the venue then shows the 4 USDC spent AND the 10 USDC redeemed -- once the
    # fill has settled there (a check inside the grace period compares nothing)
    client.balance = 50.0 - 4.0 + 10.0
    ex._last_fill_mono -= ex.CASH_SETTLE_GRACE_S + 1.0
    ex.reconcile()
    assert ex.balance == pytest.approx(20.0 + 6.0)
    assert ex.pending_redemption == pytest.approx(0.0)
    assert ex.equity() == pytest.approx(26.0)


def test_winners_are_redeemed_through_the_relayer_when_configured():
    client = FakeClient(responses=[accepted("a", making=4, taking=10)])
    ex = _ex(client, caps=LiveCaps(kill_file="none"), can_redeem=True)
    ex.condition_of[TOKEN] = COND
    _run(ex, _order(price=0.40, size=10.0))
    ex.settle_market(TOKEN, won=True)
    assert COND in ex._redeem_pending and ex._redeem_due(0.0) is None      # not due yet
    ex._redeem_pending[COND] = (0, 0.0)
    assert ex._redeem_due(1.0) == COND
    ex._redeem(COND)
    assert client.redeemed == [COND] and ex.redeemed == [COND] and not ex._redeem_pending
    # a failed attempt (market not resolved on-chain yet) is retried later, not dropped
    client.redeem_raises = RuntimeError("condition not resolved")
    ex._redeem_pending[COND] = (0, 0.0)
    ex._redeem(COND)
    assert ex._redeem_pending[COND][0] == 1 and ex._redeem_pending[COND][1] > 0.0


def test_daily_loss_halts():
    client = FakeClient(responses=[accepted("a", making=8, taking=10)])
    ex = _ex(client, caps=LiveCaps(max_order_usdc=10.0, max_daily_loss_usdc=5.0, kill_file="none"))
    _run(ex, _order(price=0.80, size=10.0))
    ex.settle_market(TOKEN, won=False)
    assert ex.halted and "daily loss" in ex.halted.lower() or "max_daily_loss" in ex.halted


def test_sell_fill_reduces_position_and_adds_cash():
    client = FakeClient(responses=[accepted("a", making=4, taking=10), accepted("b", making=10, taking=6)])
    ex = _ex(client, caps=LiveCaps(kill_file="none", max_orders_per_hour=10))
    _run(ex, _order(price=0.40, size=10.0))
    before = ex.balance
    res = _run(ex, _order(side=Side.SELL, price=0.60, size=10.0))
    kw = client.signed[1]
    assert kw["side"] == "SELL" and kw["shares"] == "10.00" and kw["min_price"] == "0.60" and kw["order_type"] == "FOK"
    f = res[0].fills[0]
    assert f.side is Side.SELL and f.price == pytest.approx(0.60)
    assert ex.positions[TOKEN].shares == pytest.approx(0.0)
    assert ex.balance == pytest.approx(before + 6.0 - f.fee)


def test_worker_drains_the_queue_reconciles_and_closes_the_client():
    client = FakeClient(responses=[accepted("a", making=4, taking=10)])
    ex = _ex(client, caps=LiveCaps(kill_file="none"))
    ex.submit(_order(price=0.40, size=10.0), 1_000.0)
    stop = asyncio.Event()

    async def drive():
        task = asyncio.create_task(ex.run(stop))
        for _ in range(50):
            if ex._done:
                break
            await asyncio.sleep(0.02)
        stop.set()
        await task

    asyncio.run(drive())
    assert ex.step(2_000.0)[0].reject_reason is RejectReason.NONE
    snap = ex.snapshot()
    assert snap["armed"] and snap["venue_balance"] == 50.0 and snap["wallet_type"] == "DEPOSIT_WALLET"
    assert client.closed


def test_a_settling_fill_is_not_balance_drift_but_a_real_move_still_is(monkeypatch):
    """2026-09-24 14:18: a reconciliation 3 s after a 2.05 USDC buy saw the venue's
    cash not yet moved, latched BALANCE_DRIFT +2.05 and stopped entries for an hour."""
    import time as _time
    from troll_poly_bot.execution.polymarket import PolymarketExchange
    ex = PolymarketExchange.__new__(PolymarketExchange)
    ex.venue_balance_at_start = None
    ex.pending_redemption = 0.0
    ex._local_cash_delta = 0.0
    ex._last_fill_mono = 0.0
    ex.balance_drift = 0.0
    ex.bankroll = ex.balance = 50.0
    ex.consecutive_errors = 0
    ex.reconcile_positions = lambda **kw: {}
    ex.check_foreign_trades = lambda **kw: []
    import threading as _threading
    ex._lock, ex._inflight, ex._post_epoch, ex.reconcile_deferred, ex.last_reconcile_ms = _threading.RLock(), 0, 0, 0, 0.0
    venue = {"cash": 73.57}
    ex._fetch_balance = lambda: (venue["cash"], None)
    ex.reconcile()                                   # baseline
    # the buy is booked locally; the venue has not settled it yet
    ex._local_cash_delta -= 2.05
    ex._last_fill_mono = _time.monotonic()
    ex.reconcile()
    assert ex.balance_drift == 0.0
    # it settles; the next quiet check agrees
    venue["cash"] = 71.52
    ex._last_fill_mono = _time.monotonic() - ex.CASH_SETTLE_GRACE_S - 1.0
    ex.reconcile()
    assert abs(ex.balance_drift) < 1e-9
    # cash that no fill explains (a deposit, a trade we never saw) is still drift
    venue["cash"] = 76.52
    ex.reconcile()
    assert ex.balance_drift == pytest.approx(5.0)


def _ioc(price=0.40, size=10.0):
    return Order(token_id=TOKEN, side=Side.BUY, price=price, size=size, tif=TimeInForce.IOC,
                 expected_price=price, tag="UP")


def test_an_ioc_entry_is_signed_as_fak_and_a_partial_fill_is_booked():
    from troll_poly_bot.types import OrderState
    # 10 shares wanted at <= 0.40; the venue had 6: 2.40 USDC paid for 6 shares
    client = FakeClient(responses=[accepted("v1", 2.40, 6.0)])
    ex = _ex(client)
    res = _run(ex, _ioc())
    assert client.signed[0]["order_type"] == "FAK"
    assert res[0].reject_reason is RejectReason.NONE and res[0].fills
    assert res[0].fills[0].size == pytest.approx(6.0)
    assert res[0].order.state is OrderState.PARTIAL and res[0].order.remaining == pytest.approx(4.0)
    assert ex.positions[TOKEN].shares == pytest.approx(6.0)
    # a plain FOK is still signed as FOK
    client2 = FakeClient()
    _run(_ex(client2), _order())
    assert client2.signed[0]["order_type"] == "FOK"


@pytest.mark.parametrize("message", ["no orders found to match with FAK order",
                                     "order couldn't be fully filled. FAK orders are partially filled or killed"])
def test_a_fak_that_found_nothing_is_not_an_api_error(message):
    client = FakeClient(post_raises=RequestRejectedError(message))
    ex = _ex(client)
    for _ in range(6):                                  # more than the halt threshold of 5
        res = _run(ex, _ioc())
        assert res[0].reject_reason is RejectReason.FOK_UNFILLABLE
    assert ex.consecutive_errors == 0 and not ex.halted


def test_a_buy_spends_the_expected_price_so_it_returns_the_shares_asked():
    """2026-10-08: the venue spends a market BUY's whole amount, so size x LIMIT bought more shares than asked whenever
    the price was better (10 sh <= 0.56 at 0.54 = 10.37). The amount is size x the expected price; the limit still caps."""
    client = FakeClient()
    ex = _ex(client, armed=False)
    o = _order(price=0.40, size=10.0)
    o.expected_price = 0.38
    _run(ex, o)
    kw = client.signed[-1]
    assert kw["amount"] == "3.80" and kw["max_price"] == "0.40"                 # 10 shares at 0.38, never above 0.40
    o2 = _order(price=0.30, size=10.37)
    o2.expected_price = 0.30                                                   # a pair take: limit AT the ask
    _run(ex, o2)
    assert client.signed[-1]["amount"] == "3.11"                               # 10.37 x 0.30 = 3.111 -> 3.11: 10.37 sh
    o3 = _order(price=0.40, size=10.0)                                        # an expected price above the limit: the limit
    o3.expected_price = 0.45
    _run(ex, o3)
    assert client.signed[-1]["amount"] == "4.00"
