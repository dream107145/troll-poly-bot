"""The 2026-09-30 candidate sources (feeds/sources.py): parsers on messages captured from
each venue on that day, the stream validator, and the run_all dispatch."""
from __future__ import annotations

import asyncio

import pytest

from troll_poly_bot.feeds import sources as src
from troll_poly_bot.feeds.spot import (
    SpotQuote, TradeTick, parse_binance, parse_binance_trade, parse_bybit, parse_bybit_trades, parse_coinbase,
    parse_coinbase_trade, parse_kraken, run_all,
)


def test_okx_bbo_trades_and_depth():
    insts = {"BTC-USDT": "BTC"}
    q = src.parse_okx({"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT"}, "data": [
        {"asks": [["83995.8", "0.79905965", "0", "8"]], "bids": [["83995.7", "0.012087", "0", "11"]],
         "ts": "1790792434201", "seqId": 81827924720}]}, insts, 5.0)
    assert q.exchange == "okx" and q.bid == 83995.7 and q.ask == 83995.8 and q.event_ts_ms == 1790792434201.0
    assert q.sequence == 81827924720 and q.bid_size == pytest.approx(0.012087)
    ts = src.parse_okx_trades({"arg": {"channel": "trades", "instId": "BTC-USDT"}, "data": [
        {"tradeId": "1064973398", "px": "83895", "sz": "0.17", "side": "buy", "ts": "1790792863366"},
        {"tradeId": "1064973405", "px": "83895", "sz": "0.07", "side": "sell", "ts": "1790792863366"}]}, insts, 6.0)
    assert [t.aggressor_buy for t in ts] == [True, False] and ts[0].sequence == 1064973398
    d = src.parse_okx_depth({"arg": {"channel": "books5", "instId": "BTC-USDT"}, "data": [
        {"asks": [["83895", "2.4", "0", "12"], ["83896.4", "0.003", "0", "1"]],
         "bids": [["83894.9", "4.08", "0", "1"]], "ts": "1790792862428", "seqId": 5}]}, insts, 7.0)
    assert d.asks[0] == (83895.0, 2.4) and len(d.asks) == 2 and d.bids == [(83894.9, 4.08)] and d.sequence == 5
    assert src.parse_okx({"event": "subscribe", "arg": {"channel": "bbo-tbt"}}, insts, 1.0) is None
    # the perpetual is the same message shape under its own name
    q2 = src.parse_okx({"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"}, "data": [
        {"asks": [["83954", "732", "0", "32"]], "bids": [["83953.9", "312", "0", "57"]], "ts": "1", "seqId": 2}]},
        {"BTC-USDT-SWAP": "BTC"}, 1.0, "okx_swap")
    assert q2.exchange == "okx_swap"


def test_bitstamp_book_top_trade_and_depth():
    chans = {"order_book_btcusd": "BTC", "live_trades_btcusd": "BTC"}
    book = {"data": {"timestamp": "1790792433", "microtimestamp": "1790792433309182",
                     "bids": [["83966.09", "0.43"], ["83966.08", "0.06"]], "asks": [["83966.10", "0.5"]]},
            "channel": "order_book_btcusd", "event": "data"}
    q = src.parse_bitstamp(book, chans, 3.0)
    assert isinstance(q, SpotQuote) and q.bid == 83966.09 and q.ask == 83966.10 and q.event_ts_ms == pytest.approx(1790792433309.182)
    d = src.bitstamp_depth(book, chans, 3.0)
    assert d.bids == [(83966.09, 0.43), (83966.08, 0.06)] and d.exchange == "bitstamp"
    t = src.parse_bitstamp({"data": {"id": 646735431, "timestamp": "1790792861", "amount": 0.00006284, "price": 83870.16,
                                     "type": 1, "microtimestamp": "1790792861728000"},
                            "channel": "live_trades_btcusd", "event": "trade"}, chans, 4.0)
    assert isinstance(t, TradeTick) and t.aggressor_buy is False and t.sequence == 646735431
    assert t.event_ts_ms == pytest.approx(1790792861728.0)
    assert src.parse_bitstamp({"event": "bts:subscription_succeeded", "channel": "order_book_btcusd"}, chans, 1.0) is None


def test_binance_futures_and_depth():
    syms = {"btcusdt": "BTC"}
    q = src.parse_binance_futures({"stream": "btcusdt@bookTicker", "data": {
        "e": "bookTicker", "u": 11701502776178, "s": "BTCUSDT", "b": "83968.40", "B": "6.364", "a": "83968.50",
        "A": "2.873", "T": 1790792433935, "E": 1790792433936}}, syms, 1.0)
    assert q.exchange == "binance_futures" and q.event_ts_ms == 1790792433936.0 and q.sequence == 11701502776178
    t = src.parse_binance_futures_trade({"stream": "btcusdt@aggTrade", "data": {"p": "1", "q": "2", "m": True, "T": 5, "a": 9}},
                                        syms, 1.0)
    assert t.aggressor_buy is False and t.sequence == 9 and t.event_ts_ms == 5.0
    d = src.parse_binance_depth({"stream": "btcusdt@depth10@100ms", "data": {
        "lastUpdateId": 100888513566, "bids": [["83902.0", "0.38"]], "asks": [["83902.5", "1.0"], ["83903", "2"]]}}, syms, 2.0)
    assert d.exchange == "binance" and d.sequence == 100888513566 and d.asks[1] == (83903.0, 2.0) and d.event_ts_ms is None


def test_deribit_quote_and_trades():
    insts = {"BTC-PERPETUAL": "BTC"}
    q = src.parse_deribit({"jsonrpc": "2.0", "method": "subscription", "params": {"channel": "quote.BTC-PERPETUAL", "data": {
        "timestamp": 1790792433245, "instrument_name": "BTC-PERPETUAL", "best_ask_price": 83983.5, "best_bid_price": 83983.0,
        "best_ask_amount": 33750.0, "best_bid_amount": 81610.0}}}, insts, 1.0)
    assert isinstance(q, SpotQuote) and q.bid == 83983.0 and q.event_ts_ms == 1790792433245.0
    assert q.bid_size == pytest.approx(81610.0 / 83983.0)              # USD notional -> BTC
    ts = src.parse_deribit({"method": "subscription", "params": {"channel": "trades.BTC-PERPETUAL.100ms", "data": [
        {"instrument_name": "BTC-PERPETUAL", "price": 84000.0, "amount": 8400.0, "direction": "sell", "timestamp": 7,
         "trade_seq": 11}]}}, insts, 1.0)
    assert ts[0].qty == pytest.approx(0.1) and ts[0].aggressor_buy is False and ts[0].sequence == 11
    assert src.parse_deribit({"jsonrpc": "2.0", "id": 1, "result": ["quote.BTC-PERPETUAL"]}, insts, 1.0) is None


def test_hyperliquid_bbo_and_trades():
    q = src.parse_hyperliquid({"channel": "bbo", "data": {"coin": "BTC", "time": 1790792434197, "bbo": [
        {"px": "83984.0", "sz": "8.38", "n": 38}, {"px": "83985.0", "sz": "2.67", "n": 16}]}}, {"BTC"}, 1.0)
    assert q.bid == 83984.0 and q.ask == 83985.0 and q.event_ts_ms == 1790792434197.0 and q.exchange == "hyperliquid"
    ts = src.parse_hyperliquid({"channel": "trades", "data": [
        {"coin": "BTC", "side": "B", "px": "83871.0", "sz": "0.0003", "time": 1790792838046, "tid": 1049990281501684},
        {"coin": "ETH", "side": "A", "px": "1", "sz": "1", "time": 1, "tid": 2}]}, {"BTC"}, 1.0)
    assert len(ts) == 1 and ts[0].aggressor_buy is True and ts[0].sequence == 1049990281501684
    assert src.parse_hyperliquid({"channel": "subscriptionResponse", "data": {}}, {"BTC"}, 1.0) is None


def test_coinbase_advanced_trade_ticker_and_trades():
    prods = {"BTC-USD": "BTC"}
    out = src.parse_coinbase_adv({"channel": "ticker", "timestamp": "2026-09-30T18:20:33.683936289Z", "sequence_num": 2,
                                  "events": [{"type": "update", "tickers": [{"type": "ticker", "product_id": "BTC-USD",
                                              "price": "83964.62", "best_bid": "83964.61", "best_ask": "83964.63",
                                              "best_bid_quantity": "0.5", "best_ask_quantity": "0.2"}]}]}, prods, 1.0)
    (q,) = out
    assert isinstance(q, SpotQuote) and q.bid == 83964.61 and q.sequence == 2 and q.bid_size == 0.5
    assert q.event_ts_ms == pytest.approx(1790792433683.936, abs=0.01)   # nanoseconds trimmed, not dropped
    (t,) = src.parse_coinbase_adv({"channel": "market_trades", "timestamp": "2026-09-30T18:20:33.7Z", "sequence_num": 3,
                                   "events": [{"type": "update", "trades": [{"trade_id": "77", "product_id": "BTC-USD",
                                               "price": "83964.62", "size": "0.01", "side": "SELL",
                                               "time": "2026-09-30T18:20:33.650000Z"}]}]}, prods, 1.0)
    assert isinstance(t, TradeTick) and t.aggressor_buy is True and t.sequence == 77      # SELL is the maker: taker bought
    assert t.event_ts_ms == pytest.approx(1790792433650.0)
    assert src.parse_coinbase_adv({"channel": "heartbeats", "events": []}, prods, 1.0) == []


def test_gemini_top_of_book_and_trades():
    top = [None, None, None, None]
    first = src.parse_gemini({"eventId": 1, "events": [
        {"delta": "0.01", "price": "83974.24", "reason": "initial", "remaining": "0.01", "side": "ask", "type": "change"},
        {"delta": "0.0058", "price": "83974.23", "reason": "initial", "remaining": "0.0058", "side": "bid", "type": "change"}],
        "socket_sequence": 0, "type": "update"}, "BTC", 1.0, top)
    (q,) = first
    assert q.bid == 83974.23 and q.ask == 83974.24 and q.sequence == 0 and q.event_ts_ms is None
    second = src.parse_gemini({"eventId": 2, "events": [
        {"amount": "0.00008218", "makerSide": "ask", "price": "83974.24", "tid": 2840141030784244, "type": "trade"},
        {"price": "83974.24", "reason": "top-of-book", "remaining": "0.011", "side": "ask", "type": "change"}],
        "socket_sequence": 1, "timestamp": 1790792433, "timestampms": 1790792433650, "type": "update"}, "BTC", 2.0, top)
    t, q2 = second
    assert isinstance(t, TradeTick) and t.aggressor_buy is True and t.event_ts_ms == 1790792433650.0
    assert isinstance(q2, SpotQuote) and q2.ask_size == 0.011 and q2.bid == 83974.23       # the bid carried over


def test_bitfinex_ticker_and_trades_with_seq_and_timestamp():
    chans = {206752: ("ticker", "BTC"), 178383: ("trades", "BTC")}
    (q,) = src.parse_bitfinex([206752, [83947, 1.63983995, 83955, 1.00833916, 212, 0.0025, 83962, 1156.9, 85586, 83017],
                               17, 1790792433000], chans, 1.0)
    assert q.bid == 83947 and q.ask == 83955 and q.sequence == 17 and q.event_ts_ms == 1790792433000.0
    (t,) = src.parse_bitfinex([178383, "te", [1985508285, 1790792863202, -0.00022, 83854], 2, 1790792863203], chans, 1.0)
    assert t.aggressor_buy is False and t.qty == pytest.approx(0.00022) and t.sequence == 1985508285
    assert src.parse_bitfinex([178383, "tu", [1985508285, 1790792863202, -0.00022, 83854], 3, 1], chans, 1.0) == []
    assert src.parse_bitfinex([178383, "hb"], chans, 1.0) == []
    assert src.parse_bitfinex([178383, [[1, 2, 3, 4]]], chans, 1.0) == []                  # the trades snapshot


def test_polymarket_chainlink_reference():
    q = src.parse_pm_chainlink({"connection_id": "x", "payload": {"full_accuracy_value": "1", "symbol": "btc/usd",
                                "timestamp": 1790792860000, "value": 83900.5}, "timestamp": 1790792861043,
                                "topic": "crypto_prices_chainlink", "type": "update"}, {"BTC"}, 1790792861204.0)
    assert q.exchange == "pm_chainlink" and q.bid == q.ask == 83900.5 and q.event_ts_ms == 1790792860000.0
    assert src.parse_pm_chainlink({"topic": "crypto_prices_chainlink", "payload": {"symbol": "sol/usd", "value": 1}},
                                  {"BTC"}, 1.0) is None


def test_existing_parsers_now_carry_sequence_and_event_time():
    q = parse_binance({"stream": "btcusdt@bookTicker", "data": {"u": 400, "b": "1", "B": "1", "a": "2", "A": "1"}},
                      {"btcusdt": "BTC"}, 1.0)
    assert q.sequence == 400
    t = parse_binance_trade({"stream": "btcusdt@aggTrade", "data": {"p": "1", "q": "1", "m": False, "T": 3, "a": 12}},
                            {"btcusdt": "BTC"}, 1.0)
    assert t.sequence == 12
    q2 = parse_bybit({"topic": "orderbook.1.BTCUSDT", "ts": 5, "data": {"b": [["1", "1"]], "a": [["2", "1"]], "seq": 818}},
                     {"BTCUSDT": "BTC"}, 1.0, {})
    assert q2.sequence == 818 and q2.event_ts_ms == 5.0
    (t2,) = parse_bybit_trades({"topic": "publicTrade.BTCUSDT", "data": [{"p": "1", "v": "1", "S": "Buy", "T": 1, "i": "abc"}]},
                               {"BTCUSDT": "BTC"}, 1.0)
    assert t2.sequence is None                                        # bybit trade ids are not integers: kept None
    q3 = parse_coinbase({"type": "ticker", "product_id": "BTC-USD", "best_bid": "1", "best_ask": "2", "price": "1.5",
                         "sequence": 99, "time": "2026-09-30T18:20:33.5Z", "trade_id": 7, "last_size": "1", "side": "buy"},
                        {"BTC-USD": "BTC"}, 1.0)
    assert q3.sequence == 99 and q3.event_ts_ms == pytest.approx(1790792433500.0)
    t3 = parse_coinbase_trade({"type": "ticker", "product_id": "BTC-USD", "price": "1", "last_size": "1", "side": "buy",
                               "trade_id": 7}, {"BTC-USD": "BTC"}, 1.0)
    assert t3.sequence == 7
    (q4,) = parse_kraken({"channel": "ticker", "data": [{"symbol": "BTC/USD", "bid": 1.0, "ask": 2.0,
                                                          "timestamp": "2026-09-30T18:20:32.5Z"}]}, {"BTC/USD": "BTC"}, 1.0)
    assert q4.event_ts_ms == pytest.approx(1790792432500.0)


def test_validator_flags_duplicates_order_gaps_and_staleness():
    v = src.StreamValidator(stale_ms=1000.0)
    assert v.check("okx", "BTC", "q", 10, 500.0, 600.0) == "ok"
    assert v.check("okx", "BTC", "q", 11, 510.0, 610.0) == "ok"
    assert v.check("okx", "BTC", "q", 11, 510.0, 611.0) == "dup"
    assert v.check("okx", "BTC", "q", 9, 400.0, 612.0) == "out_of_order"
    assert v.check("okx", "BTC", "q", 20, 520.0, 620.0) == "gap"
    assert v.check("okx", "BTC", "q", 21, 530.0, 2_000.0) == "stale"
    # without a sequence the event time orders the stream; without either, everything is ok
    assert v.check("kraken", "BTC", "t", None, 100.0, 150.0) == "ok"
    assert v.check("kraken", "BTC", "t", None, 90.0, 151.0) == "out_of_order"
    assert v.check("binance", "BTC", "q", None, None, 152.0) == "ok"
    s = v.snapshot()["okx/BTC/q"]
    assert (s["messages"], s["dup"], s["out_of_order"], s["gaps"], s["stale"]) == (6, 1, 1, 1, 1)
    assert s["max_delay_ms"] == 1470.0 and v.last == "ok"


def test_sink_drops_duplicates_and_reports_trade_lag():
    seen, lags = [], []
    sink = src._Sink("okx", seen.append, lambda: 0.0, on_trade=seen.append, on_source_latency=lambda s, ms: lags.append((s, ms)))
    q = SpotQuote("BTC", "okx", 1.0, 2.0, 1.5, 100.0, 90.0, sequence=5)
    sink.quote(q)
    sink.quote(q)
    assert seen == [q]
    sink.trade(TradeTick("BTC", "okx", 1.0, 1.0, True, 200.0, 150.0, sequence=1))
    assert lags == [("okx", 50.0)] and len(seen) == 2


def test_run_all_refuses_the_reference_and_ignores_unknown_names(monkeypatch, caplog):
    async def go(exchanges):
        stop = asyncio.Event()
        stop.set()
        await run_all(("BTC",), lambda q: None, lambda: 0.0, stop, exchanges=exchanges)
    with pytest.raises(ValueError):
        asyncio.run(go(("coinbase", "pm_chainlink")))
    started = []
    monkeypatch.setitem(src.ADAPTERS, "okx", lambda *a, **k: started.append("okx") or asyncio.sleep(0))
    asyncio.run(go(("okx", "nosuch")))
    assert started == ["okx"] and "unknown spot source 'nosuch'" in caplog.text
    assert set(src.ADAPTERS) >= {"okx", "okx_swap", "bitstamp", "binance_futures", "bybit_linear", "deribit",
                                 "hyperliquid", "coinbase_adv", "gemini", "bitfinex"}
