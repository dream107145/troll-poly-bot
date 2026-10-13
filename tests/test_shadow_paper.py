"""In live mode every real order is mirrored into a shadow paper engine, and the journal is written."""
from __future__ import annotations

from troll_poly_bot.execution.paper import FeeModel
from troll_poly_bot.execution.polymarket import LiveCaps, PolymarketExchange
from troll_poly_bot.execution.scenarios import OPTIMISTIC
from troll_poly_bot.live import LiveBook, LiveBot, LiveMarket
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.types import Market, Order, Side, TimeInForce

from tests.test_polymarket_exchange import FakeClient, accepted

SLUG = "btc-updown-5m-1"
Y, N = "Y", "N"


def _live_bot(tmp_path, client):
    ex = PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=50.0,
                            caps=LiveCaps(kill_file=str(tmp_path / "KILL")), armed=True, wallet="0x" + "ab" * 20)
    bot = LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"), exchange=ex)
    bot.shadow.scenario = OPTIMISTIC                       # fill on arrival: the mechanics, not the survival rule
    now = bot.clock.now()
    market = Market(condition_id="c", asset="BTC", yes_token_id=Y, no_token_id=N, strike=100.0,
                    open_ts=now - 1000.0, close_ts=now + 299_000.0, tick_size=0.01, min_size=5.0, slug=SLUG)
    lm = LiveMarket(meta=MarketMeta(market=market, resolution_source="", accepting_orders=True, duration_s=300))
    for token in (Y, N):
        lm.books[token] = LiveBook(token)
        lm.books[token].ts = now
    lm.books[Y].asks = {0.50: 100.0, 0.51: 100.0}
    lm.books[Y].bids = {0.49: 100.0}
    bot.markets[SLUG] = lm
    bot.token_index[Y] = (SLUG, lm)
    bot.token_index[N] = (SLUG, lm)
    return bot, lm, now


def test_a_live_order_is_mirrored_and_the_paper_outcome_lands_in_forensics(tmp_path):
    client = FakeClient(responses=[accepted("id-1", 2.55, 5.0)])       # venue fills 5 shares for 2.55 USDC
    bot, lm, now = _live_bot(tmp_path, client)
    order = Order(token_id=Y, side=Side.BUY, price=0.51, size=5.0, tif=TimeInForce.FOK, expected_price=0.50)
    bot.fair_by_order[order.order_id] = {"slug": SLUG, "asset": "BTC", "epoch": 1, "token_id": Y, "side": "UP"}
    bot.forensics.open(order.order_id, kind="entry", slug=SLUG)
    bot.exchange.submit(order, now)
    bot._shadow_submit(order, now)
    assert len(bot.shadow_of) == 1
    bot.exchange._place(bot.exchange._queue.popleft())
    for res in bot.exchange.step(now):
        bot._on_result(res, now)
    for res in bot.shadow.step(now + 500.0, bot._true_book, bot._close_ts):
        bot._on_shadow_result(res, now + 500.0)
    rec = bot.forensics.records[order.order_id]
    assert rec.fields["paper_outcome"] == "FILLED" and rec.fields["paper_fill_price"] == 0.50
    assert rec.fields["execution_price"] == 0.51
    assert bot.shadow_of == {}
    assert bot.decisions.summary()["orders"]["ORDER_FILLED"] == 1
    assert bot.snapshot()["shadow"]["orders_filled"] == 1.0


def test_a_paper_kill_is_recorded_against_the_live_order(tmp_path):
    client = FakeClient()
    bot, lm, now = _live_bot(tmp_path, client)
    lm.books[Y].asks = {}                                   # nothing to buy in the local book
    order = Order(token_id=Y, side=Side.BUY, price=0.51, size=5.0, tif=TimeInForce.FOK, expected_price=0.50)
    bot.forensics.open(order.order_id, kind="entry", slug=SLUG)
    bot._shadow_submit(order, now)
    for res in bot.shadow.step(now + 500.0, bot._true_book, bot._close_ts):
        bot._on_shadow_result(res, now + 500.0)
    assert bot.forensics.records[order.order_id].fields["paper_outcome"] == "REJECTED:fok_unfillable"


def test_an_exit_without_a_paper_position_is_marked(tmp_path):
    bot, lm, now = _live_bot(tmp_path, FakeClient())
    order = Order(token_id=Y, side=Side.SELL, price=0.49, size=5.0, tif=TimeInForce.FOK, expected_price=0.49)
    bot.forensics.open(order.order_id, kind="take_profit")
    bot._shadow_submit(order, now)
    assert bot.forensics.records[order.order_id].fields["paper_outcome"] == "NO_PAPER_POSITION"
    assert bot.shadow_of == {}


def test_paper_bot_has_no_shadow(tmp_path):
    bot = LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"))
    assert bot.shadow is None and bot.snapshot()["shadow"] is None


def test_the_trade_journal_gets_one_line_per_completed_position(tmp_path):
    from troll_poly_bot.execution.forensics import TradeForensics
    f = TradeForensics(tmp_path / "f.jsonl")
    f.open("e1", kind="entry", slug=SLUG, side="UP", entry_price=0.5, quantity=5.0, paper_outcome="FILLED")
    f.stage("e1", "FULL_FILL", 1.0)
    f.stage("e1", "SETTLED", 2.0)
    f.close("e1", 2.0, pnl_after_fees=2.4, exit_price=1.0, reason_for_exit="SETTLEMENT", paper_pnl=2.4)
    f.open("k1", kind="entry", slug=SLUG)
    f.close("k1", 3.0, outcome="REJECTED:fok_unfillable")   # never filled: not a trade
    lines = (tmp_path / "trade_journal.jsonl").read_text().splitlines()
    assert len(lines) == 1 and '"result": "WIN"' in lines[0] and '"paper_pnl": 2.4' in lines[0]


def test_a_tagged_bot_keeps_its_own_side_files(tmp_path):
    bot = LiveBot(assets=("BTC",), state_path=str(tmp_path / "live_state.momentum.json"),
                  trade_log=str(tmp_path / "live_trades.momentum.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))
    assert bot.forensics.path.name == "trade_forensics.momentum.jsonl"
    assert bot.forensics.journal_path.name == "trade_journal.momentum.jsonl"
    assert bot.decisions.path.name == "decisions.momentum.jsonl"
    plain = LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                    earnings_path=str(tmp_path / "e.jsonl"))
    assert plain.forensics.path.name == "trade_forensics.jsonl"
