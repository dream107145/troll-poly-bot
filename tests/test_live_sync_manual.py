"""2026-10-05: the live account kept tripping its guard for two reasons the user hit on the first evening of real
money -- the venue redeeming a resolved market before the bot's result lookup caught up (cash "unexplained",
position "missing"), and the user's own trades on polymarket.com (a "foreign trade"). Now the adapter expects the
first and adopts the second, and the recovery engine books fills that are not its own orders."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from test_polymarket_exchange import TOKEN, FakeClient, _ex, _order, _run, accepted

from troll_poly_bot.execution.polymarket import PolymarketExchange
from troll_poly_bot.types import Side

OTHER = "7" * 40


def _bare():
    """An adapter without a client: reconcile() with the venue reads stubbed."""
    ex = PolymarketExchange.__new__(PolymarketExchange)
    ex.venue_balance_at_start = None
    ex.pending_redemption = 0.0
    ex._local_cash_delta = 0.0
    ex._last_fill_mono = 0.0
    ex.balance_drift = 0.0
    ex.bankroll = ex.balance = 50.0
    ex.consecutive_errors = 0
    ex.redemptions_ahead = 0
    ex.redeemed_ahead = 0.0
    ex.settling_tokens = None
    ex.reconcile_positions = lambda **kw: {}
    ex.check_foreign_trades = lambda **kw: []
    ex._lock, ex._inflight, ex._post_epoch, ex.reconcile_deferred, ex.last_reconcile_ms = threading.RLock(), 0, 0, 0, 0.0
    return ex


def test_cash_from_an_unsettled_winner_is_a_redemption_ahead_of_us_not_drift():
    ex = _bare()
    venue = {"cash": 73.57}
    ex._fetch_balance = lambda: (venue["cash"], None)
    ex.reconcile()                                                         # baseline
    # the market closed, we hold 5.09 of the winner, our outcome lookup has not caught up; the venue pays
    ex.settling_tokens = lambda: {TOKEN: 5.09}
    venue["cash"] = 73.57 + 5.09
    ex.reconcile()
    assert ex.balance_drift == 0.0 and ex.redemptions_ahead == 1
    assert ex.balance == pytest.approx(55.09)                              # the cash is ours to trade with at once
    assert ex.redeemed_ahead == pytest.approx(5.09)                       # ... and remembered for the settlement
    # our settlement then books the payout: retired against the cash already received, nothing left pending
    from troll_poly_bot.types import Position
    ex.positions = {TOKEN: Position(token_id=TOKEN, shares=5.09, cost_basis=3.0, fees_paid=0.0)}
    ex.condition_of, ex.can_redeem, ex._redeem_pending, ex.halted, ex.realised = {}, False, {}, None, 0.0
    ex.caps = SimpleNamespace(max_daily_loss_usdc=1e9)
    assert ex.settle_market(TOKEN, won=True) == pytest.approx(5.09 - 3.0)
    assert ex.pending_redemption == pytest.approx(0.0) and ex.redeemed_ahead == pytest.approx(0.0)
    ex.settling_tokens = lambda: {}
    ex.reconcile()
    assert ex.balance_drift == 0.0 and ex.balance == pytest.approx(55.09)
    # cash with NO unsettled winner behind it is still drift
    venue["cash"] += 3.0
    ex.reconcile()
    assert ex.balance_drift == pytest.approx(3.0)


def test_a_vanished_position_of_an_unsettled_market_is_not_missing():
    client = FakeClient(balance=50.0, responses=[accepted("a", making=4, taking=10)])
    client.list_positions = lambda: []                                     # the venue already redeemed it
    client.list_open_orders = lambda: []
    ex = _ex(client, bankroll=20.0)
    ex.watched_tokens = lambda: {TOKEN}
    _run(ex, _order(price=0.40, size=10.0))
    rep = ex.reconcile_positions()
    assert rep["missing_positions"] and rep["missing_positions"][0]["ours"] == 10.0
    ex.settling_tokens = lambda: {TOKEN: 10.0}
    rep = ex.reconcile_positions()
    assert rep["missing_positions"] == [] and rep["redeemed_pending"][0]["ours"] == 10.0


def test_a_trade_made_on_the_website_is_adopted_not_foreign():
    client = FakeClient(balance=50.0)
    rows = [{"id": "t1", "asset_id": TOKEN, "side": "BUY", "size": 7.0, "price": 0.55, "trader_side": "TAKER",
             "taker_order_id": "web-order", "matched_at": time.time() + 5.0},
            {"id": "t2", "asset_id": OTHER, "side": "BUY", "size": 3.0, "price": 0.30, "trader_side": "TAKER",
             "taker_order_id": "web-order-2", "matched_at": time.time() + 5.0}]
    client.list_account_trades = lambda after=None: rows
    client.list_positions = lambda: []
    client.list_open_orders = lambda: []
    ex = _ex(client, bankroll=20.0)
    ex.watched_tokens = lambda: {TOKEN}                                  # OTHER is not a market this bot trades
    seen = []
    # without adoption: both are foreign
    assert len(ex.check_foreign_trades()) == 2 and ex.positions.get(TOKEN) is None
    ex._seen_trade_ids.clear()
    ex.foreign_trades.clear()
    ex.adopt_external = True
    ex.on_external_fill = lambda order, fill, trade: seen.append((order.side, fill.size, fill.price, trade["trade_id"]))
    new = ex.check_foreign_trades()
    assert new == []                                                       # nothing latches on a synchronised account
    assert any(t["trade_id"] == "t2" and t["role"].startswith("OTHER_MARKET") for t in ex.external_trades)   # listed, not booked
    assert ex.positions.get(OTHER) is None
    assert ex.positions[TOKEN].shares == pytest.approx(7.0) and ex.positions[TOKEN].cost_basis == pytest.approx(3.85)
    assert ex.balance == pytest.approx(20.0 - 3.85 - ex.fills[-1].fee)
    assert seen == [(Side.BUY, 7.0, 0.55, "t1")] and any(t["trade_id"] == "t1" for t in ex.external_trades)
    # a sale on the website of what we hold
    rows.append({"id": "t3", "asset_id": TOKEN, "side": "SELL", "size": 7.0, "price": 0.60, "trader_side": "TAKER",
                 "taker_order_id": "web-order-3", "matched_at": time.time() + 6.0})
    ex.check_foreign_trades()
    assert ex.positions[TOKEN].shares == pytest.approx(0.0) and seen[-1][0] is Side.SELL


def test_a_venue_position_we_never_bought_is_adopted_at_the_mark():
    client = FakeClient(balance=50.0)
    client.list_positions = lambda: [{"asset_id": TOKEN, "size": 12.0}]
    client.list_open_orders = lambda: ["someone-elses-order"]
    client.list_account_trades = lambda after=None: []
    ex = _ex(client, bankroll=20.0)                                        # armed: strays WOULD be cancelled ...
    ex.watched_tokens = lambda: {TOKEN}
    rep = ex.reconcile_positions()
    assert rep["unknown_positions"] and rep["unknown_positions"][0]["venue"] == 12.0
    ex.adopt_external = True
    ex.on_external_position = lambda tok, diff: 0.47
    # first sight of the difference: it waits out the confirmation window, reported as pending -- not a trip
    rep = ex.reconcile_positions()
    assert rep["unknown_positions"] == [] and rep["pending_adoption"][0]["venue"] == 12.0 and "adopted_positions" not in rep
    ex.ADOPT_CONFIRM_S = 0.0                                               # ... the window over, with no fill of ours
    ex._token_fill_mono.clear()
    rep = ex.reconcile_positions()
    assert rep["unknown_positions"] == [] and rep["adopted_positions"][0]["venue"] == 12.0
    assert ex.positions[TOKEN].shares == pytest.approx(12.0) and ex.positions[TOKEN].cost_basis == pytest.approx(12 * 0.47)
    # ... but not when the account's manual trading is synchronised: they are left alone at start
    cancelled = []
    client.cancel_order = lambda order_id: cancelled.append(order_id)
    ex2 = PolymarketExchange(client, ex.fees, bankroll=20.0, caps=ex.caps, armed=True, clock=lambda: 1_000.0,
                             wallet=FakeClient.wallet)
    ex2.adopt_external = True
    ex2.authenticate()
    assert cancelled == []


def test_the_recovery_engine_books_external_fills():
    from test_pairlock_conditions import _ceng
    from test_spot_lag import SLUG, T0
    from troll_poly_bot.strategy import pairlock_recovery as rc
    e = _ceng()
    e.on_external_fill(SLUG, "UP", 20.0, 0.55, 0.3465, "BUY", T0 + 5000.0, "WEB_BUY")
    cyc = e.cycles[SLUG]
    assert cyc.state is rc.RState.HOLDING and cyc.book.up_qty == 20.0 and cyc.book.fills[0].kind == "external"
    e.on_external_fill(SLUG, "UP", 8.0, 0.70, 0.1176, "SELL", T0 + 9000.0, "MANUAL_SELL")
    assert cyc.book.up_qty == pytest.approx(12.0) and cyc.book.realised_exit_pnl > 0
    m = SimpleNamespace(close_ts=T0 + 300_000.0)
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    pnl = cyc.book.resolution_pnl("UP")
    e.on_market_settled(SLUG, True, pnl, m.close_ts + 40_000.0)
    assert not e.accounting_halt and e.stats["external_fills"] == 2


def test_a_sale_outside_the_bot_is_adopted_from_the_positions_and_the_trade_row_is_not_booked_twice():
    client = FakeClient(balance=50.0, responses=[accepted("a", making=4, taking=10)])
    venue_pos = {TOKEN: 10.0}
    client.list_positions = lambda: [{"asset_id": t, "size": q} for t, q in venue_pos.items()]
    client.list_open_orders = lambda: []
    trades = []
    client.list_account_trades = lambda after=None: trades
    ex = _ex(client, bankroll=20.0)
    ex.watched_tokens = lambda: {TOKEN}
    _run(ex, _order(price=0.40, size=10.0))                                # we hold 10
    seen = []
    ex.adopt_external = True
    ex.on_external_position = lambda tok, diff: 0.62 if diff < 0 else 0.64   # the bid for a sale, the ask for a buy
    ex.on_external_fill = lambda order, fill, trade: seen.append((order.side, fill.size, fill.price, trade["role"]))
    cash_before = ex.balance
    venue_pos[TOKEN] = 4.0                                                  # the user sold 6 on polymarket.com
    rep = ex.reconcile_positions()                                          # first sight: pending, never "missing"
    assert rep["missing_positions"] == [] and rep["pending_adoption"][0]["ours"] == 10.0 and ex.positions[TOKEN].shares == 10.0
    ex.ADOPT_CONFIRM_S = 0.0
    ex._token_fill_mono.clear()                                             # the confirmation window over
    rep = ex.reconcile_positions()
    assert rep["missing_positions"] == [] and rep["adopted_positions"][0]["ours"] == 10.0
    assert ex.positions[TOKEN].shares == pytest.approx(4.0)
    assert ex.balance == pytest.approx(cash_before + 6 * 0.62 - ex.fills[-1].fee)
    assert seen == [(Side.SELL, 6.0, 0.62, "POSITION")]
    # the venue's trade list delivers the same sale a little later: not booked again
    trades.append({"id": "t9", "asset_id": TOKEN, "side": "SELL", "size": 6.0, "price": 0.63, "trader_side": "TAKER",
                   "taker_order_id": "web", "matched_at": time.time() + 5.0})
    assert ex.check_foreign_trades() == [] and ex.positions[TOKEN].shares == pytest.approx(4.0) and len(seen) == 1
    # a buy outside the bot, likewise from the positions
    venue_pos[TOKEN] = 9.0
    ex.reconcile_positions()                                                # first sight: pending
    ex._token_fill_mono.clear()                                             # (the adopted sale counts as our fill)
    ex.reconcile_positions()
    assert ex.positions[TOKEN].shares == pytest.approx(9.0) and seen[-1] == (Side.BUY, 5.0, 0.64, "POSITION")
    assert ex.snapshot()["venue_positions"] == {TOKEN: 9.0} and len(ex.snapshot()["external_trades"]) == 2
