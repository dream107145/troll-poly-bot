"""Venue reconciliation of positions and open orders, the kill-file policy, balance drift."""
from __future__ import annotations

from types import SimpleNamespace

from troll_poly_bot.execution.paper import FeeModel, RejectReason
from troll_poly_bot.execution.polymarket import LiveCaps, PolymarketExchange
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.types import Order, Side, TimeInForce

from tests.test_polymarket_exchange import FakeClient, TOKEN, accepted


class ReconcilingClient(FakeClient):
    def __init__(self, positions=None, open_orders=None, **kw):
        super().__init__(**kw)
        self.positions = list(positions or [])
        self.open = list(open_orders or [])
        self.cancelled: list[str] = []

    def list_positions(self):
        return self.positions

    def list_open_orders(self):
        return self.open

    def cancel_order(self, *, order_id):
        self.cancelled.append(order_id)
        self.open = [o for o in self.open if o["id"] != order_id]


def _ex(client, tmp_path, armed=True):
    ex = PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=50.0,
                            caps=LiveCaps(kill_file=str(tmp_path / "KILL")), armed=armed, wallet="0x" + "ab" * 20)
    return ex


def test_startup_cancels_stray_orders_and_reports_unknown_positions(tmp_path):
    client = ReconcilingClient(positions=[{"asset": TOKEN, "size": "12.5"}],
                               open_orders=[{"id": "stray-1"}, SimpleNamespace(id="stray-2")])
    ex = _ex(client, tmp_path)
    ex.authenticate()
    assert client.cancelled == ["stray-1", "stray-2"]
    rep = ex.position_report
    # nothing is watched at start-up, so a venue position is legacy, not a missed fill
    assert rep["checked"] and rep["legacy_positions"] == [{"token_id": TOKEN, "venue": 12.5, "ours": 0.0}]
    assert rep["unknown_positions"] == []
    ex.watched_tokens = lambda: {TOKEN}
    assert ex.reconcile_positions()["unknown_positions"] == [{"token_id": TOKEN, "venue": 12.5, "ours": 0.0}]
    assert ex.snapshot()["positions"]["unknown_positions"]


def test_positions_we_hold_that_the_venue_does_not_are_missing(tmp_path):
    client = ReconcilingClient(positions=[])
    ex = _ex(client, tmp_path)
    ex.positions[TOKEN].token_id = TOKEN
    ex.positions[TOKEN].shares = 7.0
    ex.positions[TOKEN].cost_basis = 3.0
    rep = ex.reconcile_positions()
    assert rep["missing_positions"] == [{"token_id": TOKEN, "venue": 0.0, "ours": 7.0}]


def test_matching_positions_are_clean_within_rounding(tmp_path):
    client = ReconcilingClient(positions=[SimpleNamespace(asset_id=TOKEN, size=7.2)])
    ex = _ex(client, tmp_path)
    ex.positions[TOKEN].token_id = TOKEN
    ex.positions[TOKEN].shares = 7.0
    rep = ex.reconcile_positions()
    assert rep["checked"] and not rep["unknown_positions"] and not rep["missing_positions"]


def test_a_client_without_position_listing_is_tolerated(tmp_path):
    ex = _ex(FakeClient(), tmp_path)
    rep = ex.reconcile_positions()
    assert rep["checked"] is False and ex.consecutive_errors == 0


def test_our_own_resting_orders_are_not_stray(tmp_path):
    client = ReconcilingClient(open_orders=[{"id": "ours"}])
    ex = _ex(client, tmp_path)
    ex.open_orders["ours"] = {"order": Order(token_id=TOKEN, side=Side.BUY, price=0.4, size=5, tif=TimeInForce.GTC),
                              "venue_id": "ours", "matched": 0.0, "fills": [], "errors": 0}
    assert ex.reconcile_positions()["stray_orders"] == []
    assert ex.cancel_stray_orders() == 0 and client.cancelled == []


def test_balance_drift_is_what_neither_fills_nor_redemptions_explain(tmp_path):
    client = ReconcilingClient(balance=50.0)
    ex = _ex(client, tmp_path)
    ex.authenticate()
    ex.pending_redemption = 3.0
    client.balance = 53.0                      # exactly the redemption
    ex.reconcile()
    assert ex.pending_redemption == 0.0 and ex.balance_drift == 0.0
    client.balance = 50.5                      # cash vanished with no fill of ours
    ex.reconcile()
    assert ex.balance_drift == -2.5
    assert ex.snapshot()["balance_drift"] == -2.5


def test_kill_file_blocks_buys_but_lets_a_sell_out(tmp_path):
    client = FakeClient(responses=[accepted("id-sell", 5.0, 1.0)])
    ex = _ex(client, tmp_path)
    (tmp_path / "KILL").write_text("stop")
    buy = Order(token_id=TOKEN, side=Side.BUY, price=0.50, size=5.0, tif=TimeInForce.FOK)
    ex.submit(buy, now=1.0)
    assert ex.step(1.0)[0].reject_reason is RejectReason.KILL_SWITCH
    sell = Order(token_id=TOKEN, side=Side.SELL, price=0.20, size=5.0, tif=TimeInForce.FOK)
    ex.submit(sell, now=2.0)
    ex._place(ex._queue.popleft())
    assert client.posted and not ex.step(2.0)[0].is_rejected


def test_kill_all_stops_sells_too(tmp_path):
    client = FakeClient()
    ex = _ex(client, tmp_path)
    (tmp_path / "KILL_ALL").write_text("stop everything")
    sell = Order(token_id=TOKEN, side=Side.SELL, price=0.20, size=5.0, tif=TimeInForce.FOK)
    ex.submit(sell, now=2.0)
    assert ex.step(2.0)[0].reject_reason is RejectReason.KILL_SWITCH and client.posted == []


def test_a_halt_blocks_buys_but_not_sells(tmp_path):
    client = FakeClient(responses=[accepted("id-sell", 5.0, 1.0)])
    ex = _ex(client, tmp_path)
    ex.halt("daily loss")
    buy = Order(token_id=TOKEN, side=Side.BUY, price=0.50, size=5.0, tif=TimeInForce.FOK)
    ex.submit(buy, now=1.0)
    assert ex.step(1.0)[0].reject_reason is RejectReason.HALTED
    sell = Order(token_id=TOKEN, side=Side.SELL, price=0.20, size=5.0, tif=TimeInForce.FOK)
    ex.submit(sell, now=2.0)
    assert len(ex._queue) == 1


class _Page:
    def __init__(self, items):
        self.items = tuple(items)


class _Paginator:
    """The SDK's shape: iterating yields pages; iter_items() yields rows."""

    def __init__(self, items):
        self._items = list(items)

    def first_page(self):
        return _Page(self._items)

    def iter_items(self):
        yield from self._items

    def __iter__(self):
        yield _Page(self._items)


class PagedClient(FakeClient):
    def __init__(self, positions=(), open_orders=(), trades=(), **kw):
        super().__init__(**kw)
        self._positions, self._open, self._trades = list(positions), list(open_orders), list(trades)

    def list_positions(self, **kw):
        return _Paginator(self._positions)

    def list_open_orders(self, **kw):
        return _Paginator(self._open)

    def list_trades(self, **kw):
        return _Paginator(self._trades)


def test_sdk_paginators_are_iterated_by_item(tmp_path):
    pos = SimpleNamespace(asset_id="legacy-token", current_size=20.83, avg_price=0.0)
    watched = SimpleNamespace(asset_id=TOKEN, current_size=5.0)
    client = PagedClient(positions=[pos, watched], open_orders=[SimpleNamespace(id="o-1")],
                         trades=[SimpleNamespace(asset_id=TOKEN, side="BUY", size=5, price=0.4, timestamp="t", slug="s")])
    ex = _ex(client, tmp_path)
    ex.watched_tokens = lambda: {TOKEN}
    rep = ex.reconcile_positions()
    assert rep["checked"]
    assert rep["unknown_positions"] == [{"token_id": TOKEN, "venue": 5.0, "ours": 0.0}]
    assert rep["legacy_positions"] == [{"token_id": "legacy-token", "venue": 20.83, "ours": 0.0}]
    assert rep["stray_orders"] == ["o-1"]
    assert ex.venue_trades()[0]["slug"] == "s"


def test_without_a_watch_list_every_venue_position_is_legacy(tmp_path):
    client = PagedClient(positions=[SimpleNamespace(asset_id="x", current_size=3.0)])
    ex = _ex(client, tmp_path)
    rep = ex.reconcile_positions()
    assert rep["unknown_positions"] == [] and rep["legacy_positions"][0]["token_id"] == "x"
