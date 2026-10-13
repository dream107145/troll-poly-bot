"""More BTC price sources, and a validator for what they send (2026-09-30).

Why this module exists
----------------------
The bot's composite spot is fed by four exchanges (feeds/spot.py). The question
of docs/feed_benchmark_2026-09-30.md is whether any OTHER public real-time
source tells this machine about a BTC move earlier, and whether that is early
enough to matter at the venue. Answering it needs the candidates wired up the
same way as the existing feeds -- same reconnect loop, same quote and trade
structures, the same composite -- so that a recording and a paper run use the
code a live run would. Nothing here is on by default: a source runs only when
named in ``--exchanges``.

What every adapter here guarantees (Phase 14 of the brief)
----------------------------------------------------------
* connection timeout (``open_timeout``) and a receive timeout that turns a
  silent socket into a reconnect;
* reconnect with exponential backoff and health reporting (``spot._loop``);
* the venue's own keep-alive where it has one (OKX and Hyperliquid application
  pings, Deribit heartbeats, Bitstamp ``bts:request_reconnect``);
* the exchange's event time and sequence number preserved on every quote and
  trade that carries one, never replaced by our clock (``event_ts_ms`` /
  ``sequence``; the receipt time is ``ts_ms``);
* duplicate, out-of-order and stale messages detected per stream
  (``StreamValidator``); duplicates are dropped, the rest are counted and
  delivered, because the composite has its own freshness gate;
* a backlogged connection (seconds behind its own trade stamps) is dropped and
  reopened (``spot._BacklogWatch``);
* one adapter failing never touches another: each is its own task under
  ``run_all``.

Clocks
------
``event_ts_ms`` is on the EXCHANGE's clock. Exchanges are not synchronised with
each other or with us (ClockSync aligns us to Binance only), so a delivery
delay measured as ``ts_ms - event_ts_ms`` carries that exchange's clock offset.
Cross-source comparisons in the benchmark therefore use OUR receipt time only.

Sources (all public, no key, all reachable from the Dublin VPS on 2026-09-30):
    okx              OKX spot           bbo-tbt + trades (+ books5 depth)
    okx_swap         OKX USDT perpetual bbo-tbt + trades
    bitstamp         Bitstamp           order_book top + live_trades (+ depth)
    binance_futures  Binance USDS-M perpetual bookTicker + aggTrade
    bybit_linear     Bybit USDT perpetual orderbook.1 + publicTrade
    deribit          Deribit BTC/ETH perpetual quote + trades (100 ms)
    hyperliquid      Hyperliquid perpetual bbo + trades
    coinbase_adv     Coinbase Advanced Trade ticker + market_trades
    gemini           Gemini v1 market data, top of book + trades
    bitfinex         Bitfinex v2 ticker + trades (sequence + timestamp flags)
    pm_chainlink     Polymarket RTDS Chainlink price: the SETTLEMENT reference,
                     ~1 Hz and about a second behind spot. Reference only:
                     ``run_all`` refuses it as a composite source.
    binance_depth    Binance spot depth10@100ms, depth only (benchmark OBI)
Pyth Hermes requires an API key since 2026-08-26 (401 on every route from
here) and is not included.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import orjson

from .spot import (
    Clock, FeedStatus, OnQuote, OnSourceLatency, OnTrade, SpotQuote, TradeTick, _BacklogWatch, _iso_ms,
    _lag, _loop, _maybe, _microprice, _touch,
)

log = logging.getLogger(__name__)

OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"
BITSTAMP_WS = "wss://ws.bitstamp.net"
BINANCE_FUTURES_WS = "wss://fstream.binance.com/stream?streams="
BINANCE_SPOT_WS = "wss://stream.binance.com:9443/stream?streams="
BYBIT_LINEAR_WS = "wss://stream.bybit.com/v5/public/linear"
DERIBIT_WS = "wss://www.deribit.com/ws/api/v2"
HYPERLIQUID_WS = "wss://api.hyperliquid.xyz/ws"
COINBASE_ADV_WS = "wss://advanced-trade-ws.coinbase.com"
GEMINI_WS = "wss://api.gemini.com/v1/marketdata/"
BITFINEX_WS = "wss://api-pub.bitfinex.com/ws/2"
PM_RTDS_WS = "wss://ws-live-data.polymarket.com"

#: sources that describe the settlement reference, never a tradable quote:
#: a composite fed one would follow a price that is a second old by design
REFERENCE_ONLY = {"pm_chainlink"}

#: listed on the derivative venues (probed 2026-09-30); other assets skipped
DERIBIT_LISTED = {"BTC", "ETH"}
GEMINI_LISTED = {"BTC", "ETH", "SOL", "XRP", "DOGE"}
BITFINEX_LISTED = {"BTC", "ETH", "SOL", "XRP", "DOGE"}
BITSTAMP_LISTED = {"BTC", "ETH", "SOL", "XRP", "DOGE"}


@dataclass(slots=True)
class DepthSnapshot:
    """The top levels of one exchange's book, best first, for the imbalance study."""
    asset: str
    exchange: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    ts_ms: float
    event_ts_ms: float | None = None
    sequence: int | None = None


OnDepth = Callable[[DepthSnapshot], None]


# ---------------------------------------------------------------- validator

@dataclass(slots=True)
class StreamStats:
    messages: int = 0
    dup: int = 0
    out_of_order: int = 0
    gaps: int = 0
    stale: int = 0
    last_seq: int | None = None
    last_event_ts: float | None = None
    max_delay_ms: float = 0.0


class StreamValidator:
    """Duplicate / out-of-order / stale detection per (exchange, asset, kind).

    A sequence number, where the venue sends one, is the authority: the same
    number again is a duplicate, a smaller one is out of order, a jump is a
    gap (informational: several streams number every book change, not every
    message). Without one the exchange event time decides ordering. A message
    whose event time is more than ``stale_ms`` before our receipt is stale --
    on that exchange's clock, so the threshold is generous.

    ``last`` is the verdict of the most recent ``check``; a recorder that
    receives the quote right after can read it without a second structure.
    """

    OK, DUP, OUT_OF_ORDER, GAP, STALE = "ok", "dup", "out_of_order", "gap", "stale"

    def __init__(self, stale_ms: float = 2_000.0) -> None:
        self.stale_ms = float(stale_ms)
        self.streams: dict[tuple[str, str, str], StreamStats] = {}
        self.last = self.OK

    def check(self, exchange: str, asset: str, kind: str, sequence: int | None, event_ts_ms: float | None,
              recv_ms: float) -> str:
        st = self.streams.get((exchange, asset, kind))
        if st is None:
            st = self.streams[(exchange, asset, kind)] = StreamStats()
        st.messages += 1
        verdict = self.OK
        if sequence is not None:
            if st.last_seq is not None:
                if sequence == st.last_seq:
                    verdict = self.DUP
                    st.dup += 1
                elif sequence < st.last_seq:
                    verdict = self.OUT_OF_ORDER
                    st.out_of_order += 1
                elif sequence > st.last_seq + 1:
                    st.gaps += 1
                    verdict = self.GAP
            if verdict != self.OUT_OF_ORDER:
                st.last_seq = max(st.last_seq or sequence, sequence)
        elif event_ts_ms is not None and st.last_event_ts is not None and event_ts_ms < st.last_event_ts:
            verdict = self.OUT_OF_ORDER
            st.out_of_order += 1
        if event_ts_ms is not None:
            if st.last_event_ts is None or event_ts_ms > st.last_event_ts:
                st.last_event_ts = event_ts_ms
            delay = recv_ms - event_ts_ms
            if delay > st.max_delay_ms:
                st.max_delay_ms = delay
            if verdict in (self.OK, self.GAP) and delay > self.stale_ms:
                verdict = self.STALE
                st.stale += 1
        self.last = verdict
        return verdict

    def snapshot(self) -> dict:
        return {f"{x}/{a}/{k}": {"messages": s.messages, "dup": s.dup, "out_of_order": s.out_of_order,
                                 "gaps": s.gaps, "stale": s.stale, "max_delay_ms": round(s.max_delay_ms, 1)}
                for (x, a, k), s in sorted(self.streams.items())}


def _levels(raw, n: int = 10) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for lv in raw or []:
        try:
            p, q = float(lv[0]), float(lv[1])
        except (IndexError, TypeError, ValueError):
            continue
        if p > 0 and q > 0:
            out.append((p, q))
        if len(out) >= n:
            break
    return out


# ------------------------------------------------------------------ parsers
# Pure functions, one per message type, unit-tested on captured messages.

def parse_okx(msg: dict, by_inst: dict[str, str], now_ms: float, exchange: str = "okx") -> SpotQuote | None:
    """``bbo-tbt``: ``asks``/``bids`` as ``[px, sz, liquidated, orders]``, ``ts``, ``seqId``."""
    arg = msg.get("arg") or {}
    if arg.get("channel") != "bbo-tbt":
        return None
    asset = by_inst.get(arg.get("instId", ""))
    if asset is None:
        return None
    for d in msg.get("data") or []:
        try:
            bid, bq = float(d["bids"][0][0]), float(d["bids"][0][1])
            ask, aq = float(d["asks"][0][0]), float(d["asks"][0][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0:
            continue
        return SpotQuote(asset, exchange, bid, ask, _microprice(bid, ask, bq, aq), now_ms, _maybe(d.get("ts")),
                         bid_size=bq or None, ask_size=aq or None, sequence=_int(d.get("seqId")))
    return None


def parse_okx_trades(msg: dict, by_inst: dict[str, str], now_ms: float, exchange: str = "okx") -> list[TradeTick]:
    arg = msg.get("arg") or {}
    if arg.get("channel") != "trades":
        return []
    asset = by_inst.get(arg.get("instId", ""))
    if asset is None:
        return []
    out: list[TradeTick] = []
    for d in msg.get("data") or []:
        price, qty = _maybe(d.get("px")), _maybe(d.get("sz"))
        if not price or not qty or price <= 0 or qty <= 0:
            continue
        out.append(TradeTick(asset, exchange, price, qty, aggressor_buy=str(d.get("side", "")).lower() == "buy",
                             ts_ms=now_ms, event_ts_ms=_maybe(d.get("ts")), sequence=_int(d.get("tradeId"))))
    return out


def parse_okx_depth(msg: dict, by_inst: dict[str, str], now_ms: float, exchange: str = "okx") -> DepthSnapshot | None:
    arg = msg.get("arg") or {}
    if arg.get("channel") != "books5":
        return None
    asset = by_inst.get(arg.get("instId", ""))
    if asset is None:
        return None
    for d in msg.get("data") or []:
        bids, asks = _levels(d.get("bids")), _levels(d.get("asks"))
        if bids and asks:
            return DepthSnapshot(asset, exchange, bids, asks, now_ms, _maybe(d.get("ts")), _int(d.get("seqId")))
    return None


def parse_bitstamp(msg: dict, by_channel: dict[str, str], now_ms: float) -> SpotQuote | DepthSnapshot | TradeTick | None:
    """``order_book_<pair>`` -> quote (top) ; ``live_trades_<pair>`` -> trade.
    ``microtimestamp`` is microseconds; ``type`` 0 = buy, 1 = sell (the taker)."""
    ch = msg.get("channel", "")
    asset = by_channel.get(ch)
    if asset is None or msg.get("event") not in ("data", "trade"):
        return None
    d = msg.get("data") or {}
    et = _maybe(d.get("microtimestamp"))
    et = et / 1000.0 if et else None
    if ch.startswith("order_book_"):
        bids, asks = _levels(d.get("bids")), _levels(d.get("asks"))
        if not bids or not asks:
            return None
        (bid, bq), (ask, aq) = bids[0], asks[0]
        return SpotQuote(asset, "bitstamp", bid, ask, _microprice(bid, ask, bq, aq), now_ms, et,
                         bid_size=bq, ask_size=aq)
    if ch.startswith("live_trades_"):
        price, qty = _maybe(d.get("price")), _maybe(d.get("amount"))
        if not price or not qty or price <= 0 or qty <= 0:
            return None
        return TradeTick(asset, "bitstamp", price, qty, aggressor_buy=(_int(d.get("type")) == 0), ts_ms=now_ms,
                         event_ts_ms=et, sequence=_int(d.get("id")))
    return None


def bitstamp_depth(msg: dict, by_channel: dict[str, str], now_ms: float) -> DepthSnapshot | None:
    ch = msg.get("channel", "")
    asset = by_channel.get(ch)
    if asset is None or not ch.startswith("order_book_") or msg.get("event") != "data":
        return None
    d = msg.get("data") or {}
    bids, asks = _levels(d.get("bids")), _levels(d.get("asks"))
    if not bids or not asks:
        return None
    et = _maybe(d.get("microtimestamp"))
    return DepthSnapshot(asset, "bitstamp", bids, asks, now_ms, et / 1000.0 if et else None)


def parse_binance_futures(msg: dict, by_symbol: dict[str, str], now_ms: float) -> SpotQuote | None:
    """Combined-stream futures ``bookTicker``: ``u`` update id, ``T`` transaction time."""
    stream, d = msg.get("stream", ""), msg.get("data") or {}
    if not stream.endswith("@bookTicker"):
        return None
    asset = by_symbol.get(stream.split("@")[0])
    if asset is None:
        return None
    try:
        bid, ask = float(d["b"]), float(d["a"])
        bq, aq = float(d.get("B") or 0), float(d.get("A") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    # E is when the venue emitted the update; T, the matching-engine transaction time, can be older
    return SpotQuote(asset, "binance_futures", bid, ask, _microprice(bid, ask, bq, aq), now_ms,
                     _maybe(d.get("E") or d.get("T")), bid_size=bq or None, ask_size=aq or None,
                     sequence=_int(d.get("u")))


def parse_binance_futures_trade(msg: dict, by_symbol: dict[str, str], now_ms: float) -> TradeTick | None:
    stream, d = msg.get("stream", ""), msg.get("data") or {}
    if not stream.endswith("@aggTrade"):
        return None
    asset = by_symbol.get(stream.split("@")[0])
    if asset is None:
        return None
    try:
        price, qty = float(d["p"]), float(d["q"])
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0 or qty <= 0:
        return None
    return TradeTick(asset, "binance_futures", price, qty, aggressor_buy=not bool(d.get("m")), ts_ms=now_ms,
                     event_ts_ms=_maybe(d.get("T") or d.get("E")), sequence=_int(d.get("a")))


def parse_binance_depth(msg: dict, by_symbol: dict[str, str], now_ms: float) -> DepthSnapshot | None:
    """Spot partial book ``<sym>@depth10@100ms`` (combined stream): ``lastUpdateId``, no event time."""
    stream, d = msg.get("stream", ""), msg.get("data") or {}
    if "@depth" not in stream:
        return None
    asset = by_symbol.get(stream.split("@")[0])
    if asset is None:
        return None
    bids, asks = _levels(d.get("bids")), _levels(d.get("asks"))
    if not bids or not asks:
        return None
    return DepthSnapshot(asset, "binance", bids, asks, now_ms, None, _int(d.get("lastUpdateId")))


def parse_deribit(msg: dict, by_inst: dict[str, str], now_ms: float) -> SpotQuote | TradeTick | list[TradeTick] | None:
    """``quote.<inst>`` -> quote (amounts are USD notional); ``trades.<inst>.100ms`` -> trades."""
    if msg.get("method") != "subscription":
        return None
    params = msg.get("params") or {}
    ch = str(params.get("channel", ""))
    data = params.get("data")
    if ch.startswith("quote."):
        d = data or {}
        asset = by_inst.get(str(d.get("instrument_name", "")))
        if asset is None:
            return None
        bid, ask = _maybe(d.get("best_bid_price")), _maybe(d.get("best_ask_price"))
        if not bid or not ask or bid <= 0 or ask <= 0:
            return None
        # amounts are in USD for the inverse perpetual: convert to base units for the microprice
        bq, aq = _maybe(d.get("best_bid_amount")), _maybe(d.get("best_ask_amount"))
        bq = bq / bid if bq else None
        aq = aq / ask if aq else None
        return SpotQuote(asset, "deribit", bid, ask, _microprice(bid, ask, bq, aq), now_ms, _maybe(d.get("timestamp")),
                         bid_size=bq, ask_size=aq)
    if ch.startswith("trades."):
        out: list[TradeTick] = []
        for d in data or []:
            asset = by_inst.get(str(d.get("instrument_name", "")))
            price, amt = _maybe(d.get("price")), _maybe(d.get("amount"))
            if asset is None or not price or not amt or price <= 0 or amt <= 0:
                continue
            out.append(TradeTick(asset, "deribit", price, amt / price, aggressor_buy=str(d.get("direction")) == "buy",
                                 ts_ms=now_ms, event_ts_ms=_maybe(d.get("timestamp")), sequence=_int(d.get("trade_seq"))))
        return out
    return None


def parse_hyperliquid(msg: dict, assets: set[str], now_ms: float) -> SpotQuote | list[TradeTick] | None:
    """``bbo``: ``[bid, ask]`` levels with ``px``/``sz``, block ``time``; ``trades``: ``side`` B/A."""
    ch, d = msg.get("channel"), msg.get("data")
    if ch == "bbo" and isinstance(d, dict):
        asset = str(d.get("coin", ""))
        if asset not in assets:
            return None
        try:
            b, a = d["bbo"][0], d["bbo"][1]
            bid, ask = float(b["px"]), float(a["px"])
            bq, aq = float(b.get("sz") or 0), float(a.get("sz") or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        if bid <= 0 or ask <= 0:
            return None
        return SpotQuote(asset, "hyperliquid", bid, ask, _microprice(bid, ask, bq, aq), now_ms, _maybe(d.get("time")),
                         bid_size=bq or None, ask_size=aq or None)
    if ch == "trades" and isinstance(d, list):
        out: list[TradeTick] = []
        for t in d:
            asset = str(t.get("coin", ""))
            price, qty = _maybe(t.get("px")), _maybe(t.get("sz"))
            if asset not in assets or not price or not qty or price <= 0 or qty <= 0:
                continue
            out.append(TradeTick(asset, "hyperliquid", price, qty, aggressor_buy=str(t.get("side")) == "B", ts_ms=now_ms,
                                 event_ts_ms=_maybe(t.get("time")), sequence=_int(t.get("tid"))))
        return out
    return None


def parse_coinbase_adv(msg: dict, by_product: dict[str, str], now_ms: float) -> list[SpotQuote | TradeTick]:
    """Advanced Trade ``ticker`` (every match; ``best_bid``/``best_ask`` with quantities) and
    ``market_trades`` (whose ``side`` is the maker's). The message ``timestamp`` is RFC 3339 with
    nanoseconds; ``sequence_num`` is per connection, all channels."""
    ch = msg.get("channel")
    out: list[SpotQuote | TradeTick] = []
    et = _iso_ms(_trim_ns(msg.get("timestamp")))
    seq = _int(msg.get("sequence_num"))
    if ch == "ticker":
        for ev in msg.get("events") or []:
            for t in ev.get("tickers") or []:
                asset = by_product.get(str(t.get("product_id", "")))
                bid, ask = _maybe(t.get("best_bid")), _maybe(t.get("best_ask"))
                if asset is None or not bid or not ask or bid <= 0 or ask <= 0:
                    continue
                bq, aq = _maybe(t.get("best_bid_quantity")), _maybe(t.get("best_ask_quantity"))
                out.append(SpotQuote(asset, "coinbase_adv", bid, ask, _microprice(bid, ask, bq, aq), now_ms, et,
                                     bid_size=bq, ask_size=aq, sequence=seq))
    elif ch == "market_trades":
        for ev in msg.get("events") or []:
            for t in ev.get("trades") or []:
                asset = by_product.get(str(t.get("product_id", "")))
                price, qty = _maybe(t.get("price")), _maybe(t.get("size"))
                if asset is None or not price or not qty or price <= 0 or qty <= 0:
                    continue
                # ``side`` is the MAKER's side here (1,545 of 1,545 trades joined on trade_id to the
                # Exchange ticker, whose side is the taker's, disagreed on 2026-09-30): SELL maker = taker bought
                out.append(TradeTick(asset, "coinbase_adv", price, qty, aggressor_buy=str(t.get("side")) == "SELL",
                                     ts_ms=now_ms, event_ts_ms=_iso_ms(_trim_ns(t.get("time"))) or et,
                                     sequence=_int(t.get("trade_id"))))
    return out


def parse_gemini(msg: dict, asset: str, now_ms: float, top: list) -> list[SpotQuote | TradeTick]:
    """v1 ``update``: ``change`` events with ``side``/``price``/``remaining`` (top of book when
    subscribed with ``top_of_book=true``), ``trade`` events with ``makerSide``. ``top`` keeps the
    last best bid/ask and sizes across messages: ``[bid, bq, ask, aq]``."""
    if msg.get("type") != "update":
        return []
    et = _maybe(msg.get("timestampms"))
    seq = _int(msg.get("socket_sequence"))
    out: list[SpotQuote | TradeTick] = []
    changed = False
    for ev in msg.get("events") or []:
        kind = ev.get("type")
        if kind == "change":
            price, rem = _maybe(ev.get("price")), _maybe(ev.get("remaining"))
            if not price or rem is None:
                continue
            if ev.get("side") == "bid":
                top[0], top[1] = price, rem
            elif ev.get("side") == "ask":
                top[2], top[3] = price, rem
            else:
                continue
            changed = True
        elif kind == "trade":
            price, qty = _maybe(ev.get("price")), _maybe(ev.get("amount"))
            if not price or not qty or price <= 0 or qty <= 0:
                continue
            # the maker was on the bid side -> the taker sold
            out.append(TradeTick(asset, "gemini", price, qty, aggressor_buy=str(ev.get("makerSide")) == "ask",
                                 ts_ms=now_ms, event_ts_ms=et, sequence=_int(ev.get("tid"))))
    if changed and top[0] and top[2] and top[0] > 0 and top[2] > 0:
        out.append(SpotQuote(asset, "gemini", top[0], top[2], _microprice(top[0], top[2], top[1], top[3]), now_ms, et,
                             bid_size=top[1] or None, ask_size=top[3] or None, sequence=seq))
    return out


def parse_bitfinex(msg, chans: dict[int, tuple[str, str]], now_ms: float) -> list[SpotQuote | TradeTick]:
    """v2 array frames after ``conf`` flags 98304 (SEQ_ALL + TIMESTAMP): ticker
    ``[chanId, [BID, BID_SIZE, ASK, ASK_SIZE, ...], seq, ts]``; trades ``[chanId, "te", [ID, MTS,
    AMOUNT, PRICE], seq, ts]`` (``"tu"`` repeats the same trade with its id: skipped). Amount's
    sign is the taker side. ``chans`` maps channel id -> (channel, asset)."""
    if not isinstance(msg, list) or len(msg) < 2 or not isinstance(msg[0], int):
        return []
    info = chans.get(msg[0])
    if info is None or msg[1] == "hb":
        return []
    channel, asset = info
    out: list[SpotQuote | TradeTick] = []
    if channel == "ticker" and isinstance(msg[1], list) and len(msg[1]) >= 4:
        v = msg[1]
        seq = _int(msg[2]) if len(msg) > 2 else None
        ts = _maybe(msg[3]) if len(msg) > 3 else None
        bid, bq, ask, aq = _maybe(v[0]), _maybe(v[1]), _maybe(v[2]), _maybe(v[3])
        if bid and ask and bid > 0 and ask > 0:
            out.append(SpotQuote(asset, "bitfinex", bid, ask, _microprice(bid, ask, bq, aq), now_ms, ts,
                                 bid_size=bq, ask_size=aq, sequence=seq))
    elif channel == "trades" and msg[1] == "te" and len(msg) >= 3 and isinstance(msg[2], list) and len(msg[2]) >= 4:
        tid, mts, amt, price = msg[2][:4]
        amt, price = _maybe(amt), _maybe(price)
        if amt and price and price > 0:
            out.append(TradeTick(asset, "bitfinex", price, abs(amt), aggressor_buy=amt > 0, ts_ms=now_ms,
                                 event_ts_ms=_maybe(mts), sequence=_int(tid)))
    return out


def parse_pm_chainlink(msg: dict, assets: set[str], now_ms: float) -> SpotQuote | None:
    """RTDS ``crypto_prices_chainlink`` update: ``payload.symbol`` ``btc/usd``, ``value``,
    ``timestamp`` (the Chainlink round, whole seconds). Bid = ask = value."""
    if msg.get("topic") != "crypto_prices_chainlink":
        return None
    p = msg.get("payload") or {}
    asset = str(p.get("symbol", "")).split("/")[0].upper()
    price = _maybe(p.get("value"))
    if asset not in assets or not price or price <= 0:
        return None
    return SpotQuote(asset, "pm_chainlink", price, price, price, now_ms, _maybe(p.get("timestamp")))


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _trim_ns(v) -> str | None:
    """``2026-09-30T18:20:33.683936289Z`` -> microseconds, which fromisoformat accepts."""
    if not isinstance(v, str):
        return None
    if "." in v and v.endswith("Z"):
        head, frac = v[:-1].split(".", 1)
        return f"{head}.{frac[:6]}Z"
    return v


# ---------------------------------------------------------------- adapters

@dataclass
class _Sink:
    """One place the adapters hand their messages to, with the validator in front."""
    name: str
    on_quote: OnQuote
    clock: Clock
    on_trade: OnTrade | None = None
    on_depth: OnDepth | None = None
    health: dict[str, FeedStatus] | None = None
    on_source_latency: OnSourceLatency | None = None
    validator: StreamValidator = field(default_factory=StreamValidator)
    watch: _BacklogWatch = field(default_factory=_BacklogWatch)

    def quote(self, q: SpotQuote) -> None:
        if self.validator.check(q.exchange, q.asset, "q", q.sequence, q.event_ts_ms, q.ts_ms) == StreamValidator.DUP:
            return
        self.on_quote(q)

    def trade(self, t: TradeTick) -> None:
        if self.validator.check(t.exchange, t.asset, "t", t.sequence, t.event_ts_ms, t.ts_ms) == StreamValidator.DUP:
            return
        if self.on_trade is not None:
            self.on_trade(t)
        _lag(self.on_source_latency, self.name, t.ts_ms, t, self.watch)

    def depth(self, d: DepthSnapshot) -> None:
        if self.on_depth is not None:
            self.on_depth(d)

    def touch(self, connected: bool | None = None, message: bool = False) -> None:
        _touch(self.health, self.name, self.clock, connected=connected, message=message)


async def _recv(ws, timeout: float):
    return await asyncio.wait_for(ws.recv(), timeout=timeout)


async def run_okx(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                  on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                  on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                  validator: StreamValidator | None = None, swap: bool = False) -> None:
    """OKX ``bbo-tbt`` + ``trades`` (+ ``books5`` when ``on_depth``). Application ping: the docs
    require a ``ping`` string within 30 s of silence, ``pong`` back."""
    import websockets
    name = "okx_swap" if swap else "okx"
    insts = {f"{a}-USDT-SWAP" if swap else f"{a}-USDT": a for a in assets}
    sink = _Sink(name, on_quote, clock, on_trade, on_depth, health, on_source_latency, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(OKX_WS, open_timeout=20, ping_interval=None, close_timeout=3) as ws:
            args = [{"channel": "bbo-tbt", "instId": i} for i in insts] + [{"channel": "trades", "instId": i} for i in insts]
            if on_depth is not None:
                args += [{"channel": "books5", "instId": i} for i in insts]
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            log.info("%s feed connected (%s)", name, ", ".join(insts.values()))
            sink.touch(connected=True)
            while not stop.is_set():
                try:
                    raw = await _recv(ws, 20)
                except asyncio.TimeoutError:
                    await ws.send("ping")
                    raw = await _recv(ws, 10)          # a silent socket after the ping is dead
                if raw == "pong":
                    continue
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                if msg.get("event") == "error":
                    log.warning("%s refused: %s", name, str(msg)[:160])
                    continue
                q = parse_okx(msg, insts, now, name)
                if q is not None:
                    sink.quote(q)
                    continue
                for t in parse_okx_trades(msg, insts, now, name):
                    sink.trade(t)
                if on_depth is not None:
                    d = parse_okx_depth(msg, insts, now, name)
                    if d is not None:
                        sink.depth(d)
    await _loop(name, connect, stop, clock, health)


async def run_okx_swap(assets, on_quote, clock, stop, on_trade=None, health=None, on_source_latency=None,
                       on_depth=None, validator=None) -> None:
    await run_okx(assets, on_quote, clock, stop, on_trade, health, on_source_latency, None, validator, swap=True)


class _ReconnectRequested(RuntimeError):
    """The venue asked us to move to another server (Bitstamp ``bts:request_reconnect``)."""


async def run_bitstamp(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                       on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                       on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                       validator: StreamValidator | None = None) -> None:
    import websockets
    chans = {}
    for a in assets:
        if a in BITSTAMP_LISTED:
            chans[f"order_book_{a.lower()}usd"] = a
            chans[f"live_trades_{a.lower()}usd"] = a
    if not chans:
        return
    sink = _Sink("bitstamp", on_quote, clock, on_trade, on_depth, health, on_source_latency, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(BITSTAMP_WS, open_timeout=20, ping_interval=15, close_timeout=3) as ws:
            for c in chans:
                await ws.send(json.dumps({"event": "bts:subscribe", "data": {"channel": c}}))
            log.info("bitstamp feed connected (%s)", ", ".join(sorted(set(chans.values()))))
            sink.touch(connected=True)
            while not stop.is_set():
                raw = await _recv(ws, 45)
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                if msg.get("event") == "bts:request_reconnect":
                    raise _ReconnectRequested("bitstamp asked for a reconnect")
                r = parse_bitstamp(msg, chans, now)
                if isinstance(r, SpotQuote):
                    sink.quote(r)
                    if on_depth is not None:
                        d = bitstamp_depth(msg, chans, now)
                        if d is not None:
                            sink.depth(d)
                elif isinstance(r, TradeTick):
                    sink.trade(r)
    await _loop("bitstamp", connect, stop, clock, health)


async def run_binance_futures(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                              on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                              on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                              validator: StreamValidator | None = None) -> None:
    import websockets
    syms = {f"{a.lower()}usdt": a for a in assets}
    streams = "/".join(f"{s}@bookTicker" for s in syms) + "/" + "/".join(f"{s}@aggTrade" for s in syms)
    sink = _Sink("binance_futures", on_quote, clock, on_trade, None, health, on_source_latency, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(BINANCE_FUTURES_WS + streams, open_timeout=20, ping_interval=15,
                                      close_timeout=3) as ws:
            log.info("binance_futures feed connected (%s)", ", ".join(syms.values()))
            sink.touch(connected=True)
            while not stop.is_set():
                raw = await _recv(ws, 45)
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                q = parse_binance_futures(msg, syms, now)
                if q is not None:
                    sink.quote(q)
                    continue
                t = parse_binance_futures_trade(msg, syms, now)
                if t is not None:
                    sink.trade(t)
    await _loop("binance_futures", connect, stop, clock, health)


async def run_binance_depth(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                            on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                            on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                            validator: StreamValidator | None = None) -> None:
    """Binance spot ``depth10@100ms`` partial books, depth only. Emits nothing to the composite."""
    import websockets
    from .spot import BINANCE_UNLISTED
    if on_depth is None:
        return
    syms = {f"{a.lower()}usdt": a for a in assets if a not in BINANCE_UNLISTED}
    if not syms:
        return
    streams = "/".join(f"{s}@depth10@100ms" for s in syms)
    sink = _Sink("binance_depth", on_quote, clock, None, on_depth, health, None, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(BINANCE_SPOT_WS + streams, open_timeout=20, ping_interval=15, close_timeout=3) as ws:
            log.info("binance_depth feed connected (%s)", ", ".join(syms.values()))
            sink.touch(connected=True)
            while not stop.is_set():
                raw = await _recv(ws, 45)
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                d = parse_binance_depth(msg, syms, now)
                if d is not None:
                    sink.depth(d)
    await _loop("binance_depth", connect, stop, clock, health)


async def run_bybit_linear(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                           on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                           on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                           validator: StreamValidator | None = None) -> None:
    """Bybit USDT perpetual: the spot parsers apply (same topics), relabelled."""
    import websockets
    from .spot import parse_bybit, parse_bybit_trades
    syms = {f"{a}USDT": a for a in assets}
    sink = _Sink("bybit_linear", on_quote, clock, on_trade, None, health, on_source_latency, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(BYBIT_LINEAR_WS, open_timeout=20, ping_interval=None, close_timeout=3) as ws:
            args = [f"orderbook.1.{s}" for s in syms] + [f"publicTrade.{s}" for s in syms]
            for i in range(0, len(args), 10):
                await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 10]}))
            log.info("bybit_linear feed connected (%s)", ", ".join(syms.values()))
            sink.touch(connected=True)
            state: dict[str, list[float | None]] = {}
            last_ping = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - last_ping > 20:
                    await ws.send(json.dumps({"op": "ping"}))
                    last_ping = time.monotonic()
                try:
                    raw = await _recv(ws, 25)
                except asyncio.TimeoutError:
                    continue
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                q = parse_bybit(msg, syms, now, state)
                if q is not None:
                    q.exchange = "bybit_linear"
                    sink.quote(q)
                    continue
                for t in parse_bybit_trades(msg, syms, now):
                    t.exchange = "bybit_linear"
                    sink.trade(t)
    await _loop("bybit_linear", connect, stop, clock, health)


async def run_deribit(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                      on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                      on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                      validator: StreamValidator | None = None) -> None:
    """Deribit ``quote.<inst>`` + ``trades.<inst>.100ms`` (``raw`` needs an account). Heartbeat: we ask
    for one every 30 s and answer each ``test_request`` with ``public/test``."""
    import websockets
    insts = {f"{a}-PERPETUAL": a for a in assets if a in DERIBIT_LISTED}
    if not insts:
        return
    sink = _Sink("deribit", on_quote, clock, on_trade, None, health, on_source_latency, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(DERIBIT_WS, open_timeout=20, ping_interval=None, close_timeout=3) as ws:
            chans = [f"quote.{i}" for i in insts] + [f"trades.{i}.100ms" for i in insts]
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "public/subscribe", "params": {"channels": chans}}))
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "public/set_heartbeat", "params": {"interval": 30}}))
            log.info("deribit feed connected (%s)", ", ".join(insts.values()))
            sink.touch(connected=True)
            while not stop.is_set():
                raw = await _recv(ws, 45)
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                if msg.get("method") == "heartbeat":
                    if (msg.get("params") or {}).get("type") == "test_request":
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "public/test", "params": {}}))
                    continue
                if "error" in msg:
                    log.warning("deribit error: %s", str(msg.get("error"))[:160])
                    continue
                r = parse_deribit(msg, insts, now)
                if isinstance(r, SpotQuote):
                    sink.quote(r)
                elif isinstance(r, list):
                    for t in r:
                        sink.trade(t)
    await _loop("deribit", connect, stop, clock, health)


async def run_hyperliquid(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                          on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                          on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                          validator: StreamValidator | None = None) -> None:
    """Hyperliquid ``bbo`` + ``trades``. Application ping ``{"method": "ping"}`` every 30 s."""
    import websockets
    coins = set(assets)
    sink = _Sink("hyperliquid", on_quote, clock, on_trade, None, health, on_source_latency, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(HYPERLIQUID_WS, open_timeout=20, ping_interval=None, close_timeout=3) as ws:
            for c in coins:
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "bbo", "coin": c}}))
                await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": c}}))
            log.info("hyperliquid feed connected (%s)", ", ".join(sorted(coins)))
            sink.touch(connected=True)
            last_ping = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - last_ping > 30:
                    await ws.send(json.dumps({"method": "ping"}))
                    last_ping = time.monotonic()
                try:
                    raw = await _recv(ws, 35)
                except asyncio.TimeoutError:
                    continue
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                r = parse_hyperliquid(msg, coins, now)
                if isinstance(r, SpotQuote):
                    sink.quote(r)
                elif isinstance(r, list):
                    for t in r:
                        sink.trade(t)
    await _loop("hyperliquid", connect, stop, clock, health)


async def run_coinbase_adv(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                           on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                           on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                           validator: StreamValidator | None = None) -> None:
    """Coinbase Advanced Trade ``ticker`` + ``market_trades`` + ``heartbeats`` (no JWT for market data)."""
    import websockets
    prods = {f"{a}-USD": a for a in assets}
    sink = _Sink("coinbase_adv", on_quote, clock, on_trade, None, health, on_source_latency, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(COINBASE_ADV_WS, open_timeout=20, ping_interval=15, close_timeout=3) as ws:
            for ch in ("ticker", "market_trades", "heartbeats"):
                await ws.send(json.dumps({"type": "subscribe", "product_ids": list(prods), "channel": ch}))
            log.info("coinbase_adv feed connected (%s)", ", ".join(prods.values()))
            sink.touch(connected=True)
            while not stop.is_set():
                raw = await _recv(ws, 45)
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                for r in parse_coinbase_adv(msg, prods, now):
                    if isinstance(r, SpotQuote):
                        sink.quote(r)
                    else:
                        sink.trade(r)
    await _loop("coinbase_adv", connect, stop, clock, health)


async def run_gemini(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                     on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                     on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                     validator: StreamValidator | None = None) -> None:
    """Gemini v1 market data: one socket per symbol (the v1 API is per symbol), top of book + trades."""
    import websockets
    wanted = [a for a in assets if a in GEMINI_LISTED]
    if not wanted:
        return
    sink = _Sink("gemini", on_quote, clock, on_trade, None, health, on_source_latency, validator or StreamValidator())

    async def one(asset: str) -> None:
        async def connect() -> None:
            url = f"{GEMINI_WS}{asset}USD?top_of_book=true&trades=true&heartbeat=true"
            async with websockets.connect(url, open_timeout=20, ping_interval=15, close_timeout=3) as ws:
                log.info("gemini feed connected (%s)", asset)
                sink.touch(connected=True)
                top: list = [None, None, None, None]
                while not stop.is_set():
                    raw = await _recv(ws, 45)
                    msg = orjson.loads(raw)
                    now = clock()
                    sink.touch(message=True)
                    for r in parse_gemini(msg, asset, now, top):
                        if isinstance(r, SpotQuote):
                            sink.quote(r)
                        else:
                            sink.trade(r)
        await _loop("gemini", connect, stop, clock, health)

    await asyncio.gather(*(one(a) for a in wanted))


async def run_bitfinex(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                       on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                       on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                       validator: StreamValidator | None = None) -> None:
    """Bitfinex v2 ``ticker`` + ``trades`` with ``conf`` flags 98304 (sequence numbers + timestamps)."""
    import websockets
    syms = {f"t{a}USD": a for a in assets if a in BITFINEX_LISTED}
    if not syms:
        return
    sink = _Sink("bitfinex", on_quote, clock, on_trade, None, health, on_source_latency, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(BITFINEX_WS, open_timeout=20, ping_interval=15, close_timeout=3) as ws:
            await ws.send(json.dumps({"event": "conf", "flags": 98304}))
            for s in syms:
                await ws.send(json.dumps({"event": "subscribe", "channel": "ticker", "symbol": s}))
                await ws.send(json.dumps({"event": "subscribe", "channel": "trades", "symbol": s}))
            log.info("bitfinex feed connected (%s)", ", ".join(syms.values()))
            sink.touch(connected=True)
            chans: dict[int, tuple[str, str]] = {}
            while not stop.is_set():
                raw = await _recv(ws, 45)
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                if isinstance(msg, dict):
                    if msg.get("event") == "subscribed" and msg.get("symbol") in syms:
                        chans[int(msg["chanId"])] = (str(msg.get("channel")), syms[msg["symbol"]])
                    elif msg.get("event") == "error":
                        log.warning("bitfinex error: %s", str(msg)[:160])
                    continue
                for r in parse_bitfinex(msg, chans, now):
                    if isinstance(r, SpotQuote):
                        sink.quote(r)
                    else:
                        sink.trade(r)
    await _loop("bitfinex", connect, stop, clock, health)


async def run_pm_chainlink(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                           on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                           on_source_latency: OnSourceLatency | None = None, on_depth: OnDepth | None = None,
                           validator: StreamValidator | None = None) -> None:
    """Polymarket RTDS, topic ``crypto_prices_chainlink``: the price the markets settle on. Reference only."""
    import websockets
    wanted = set(assets)
    sink = _Sink("pm_chainlink", on_quote, clock, None, None, health, None, validator or StreamValidator())

    async def connect() -> None:
        async with websockets.connect(PM_RTDS_WS, open_timeout=20, ping_interval=10, close_timeout=3) as ws:
            await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}))
            log.info("pm_chainlink feed connected")
            sink.touch(connected=True)
            while not stop.is_set():
                raw = await _recv(ws, 30)
                if not raw or raw[0] not in ("{", 123):
                    continue
                msg = orjson.loads(raw)
                now = clock()
                sink.touch(message=True)
                q = parse_pm_chainlink(msg, wanted, now)
                if q is not None:
                    sink.quote(q)
    await _loop("pm_chainlink", connect, stop, clock, health)


#: name -> adapter, the names ``--exchanges`` / ``run_all`` understand beyond the four in spot.py
ADAPTERS: dict[str, Callable[..., Awaitable[None]]] = {
    "okx": run_okx,
    "okx_swap": run_okx_swap,
    "bitstamp": run_bitstamp,
    "binance_futures": run_binance_futures,
    "binance_depth": run_binance_depth,
    "bybit_linear": run_bybit_linear,
    "deribit": run_deribit,
    "hyperliquid": run_hyperliquid,
    "coinbase_adv": run_coinbase_adv,
    "gemini": run_gemini,
    "bitfinex": run_bitfinex,
    "pm_chainlink": run_pm_chainlink,
}
