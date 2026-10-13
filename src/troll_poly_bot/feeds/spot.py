"""Multi-exchange spot feed: basis-adjusted composite price plus order flow.

Why more than one exchange
--------------------------
The venue settles on a Chainlink TWAP, which aggregates several exchanges.
No single exchange is that feed. A composite across Binance, Bybit and
Coinbase is closer to it, survives one feed stalling, and covers assets
Binance does not list (HYPE). The cross-exchange deviation is also a
data-quality gate: if sources disagree the strategy says STALE_DATA rather
than trade.

Why the composite is basis-adjusted
-----------------------------------
Exchanges quote at persistently different LEVELS (USDT pairs vs USD pairs,
venue premia): measured live, Coinbase sat ~9 bps under Binance on BTC. A
raw median of such sources jumps by that basis whenever a slow source drops
in or out of the freshness window, and a vol estimator fed those jumps
reported 9 bps/s on BTC where 0.9 is right -- a 10x error that would have
priced every market at a coin flip. Each exchange therefore carries a slowly
adapting offset to the primary source's level, and only joins the composite
once that offset is calibrated.

Order flow
----------
Besides the price, every adapter now emits the top-of-book sizes and each
trade with its aggressor side. That is the raw material for the order-flow
imbalance signal (features/orderflow.py): the one short-horizon predictor
with a consistent literature behind it, and the one input the earlier bot
never looked at.

Reconnects
----------
A DNS outage once made every feed reconnect every two seconds for three
hours, ~5,000 warnings. Reconnects now back off exponentially (2 s -> 60 s),
warn once and then only every tenth attempt, and report their health so the
dashboard can say "feeds down since 00:14" instead of nothing.

Message parsing is kept in pure functions so it is unit-tested; the network
loops only reconnect and dispatch.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

import orjson

log = logging.getLogger(__name__)

BINANCE_WS = "wss://stream.binance.com:9443/stream?streams="
BYBIT_WS = "wss://stream.bybit.com/v5/public/spot"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
#: 17 ms from the Dublin VPS on 2026-09-29 (Bitstamp 32, Coinbase 53, Bybit 97, Binance 125)
KRAKEN_WS = "wss://ws.kraken.com/v2"
KRAKEN_UNLISTED = {"BNB"}

#: Assets Binance spot does not list (probed 2026-09-17). Others are assumed
#: listed as <asset>USDT; an unlisted symbol simply never ticks.
BINANCE_UNLISTED = {"HYPE"}

RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0


@dataclass(slots=True)
class SpotQuote:
    asset: str
    exchange: str
    bid: float
    ask: float
    price: float            # microprice when sizes are known, else mid
    ts_ms: float            # local receipt time (clock-corrected)
    event_ts_ms: float | None = None   # exchange event time, when supplied
    bid_size: float | None = None      # top-of-book size, base units
    ask_size: float | None = None
    sequence: int | None = None        # the exchange's update id / sequence, when it sends one

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.price * 1e4 if self.price > 0 else float("nan")


@dataclass(slots=True)
class TradeTick:
    asset: str
    exchange: str
    price: float
    qty: float              # base units
    aggressor_buy: bool     # True when the taker bought (lifted the ask)
    ts_ms: float
    event_ts_ms: float | None = None
    sequence: int | None = None        # trade id, when the exchange sends one

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class CompositeView:
    asset: str
    price: float                   # median of basis-adjusted fresh sources
    weighted_price: float          # inverse-spread weighted
    n_sources: int
    deviation_bps: float           # max |adjusted source - median| / median
    cross_exchange_spread_bps: float   # (max - min) of adjusted prices / median
    age_ms: float                  # age of the freshest source
    sources: dict[str, float]      # RAW prices per exchange
    basis_bps: dict[str, float] = field(default_factory=dict)   # level offset vs the primary


@dataclass
class FeedStatus:
    """Health of one network feed, for the dashboard and the status line."""
    name: str
    connected: bool = False
    since_ms: float = 0.0          # when the current state began
    last_msg_ms: float = 0.0
    messages: int = 0
    reconnects: int = 0
    last_error: str = ""
    backoff_s: float = RECONNECT_MIN_S

    def as_dict(self, now_ms: float) -> dict:
        return {
            "connected": self.connected,
            "state_age_s": round(max(0.0, now_ms - self.since_ms) / 1000.0, 1) if self.since_ms else None,
            "last_msg_age_s": round(max(0.0, now_ms - self.last_msg_ms) / 1000.0, 1) if self.last_msg_ms else None,
            "messages": self.messages,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


def _microprice(bid: float, ask: float, bq: float | None, aq: float | None) -> float:
    if bq and aq and bq + aq > 0:
        return (bid * aq + ask * bq) / (bq + aq)
    return (bid + ask) / 2.0


def _maybe(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ parsers

def parse_binance(msg: dict, by_symbol: dict[str, str], now_ms: float) -> SpotQuote | None:
    """Combined-stream ``bookTicker`` message -> quote."""
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
    return SpotQuote(asset, "binance", bid, ask, _microprice(bid, ask, bq, aq), now_ms,
                     bid_size=bq or None, ask_size=aq or None, sequence=_int(d.get("u")))


def parse_binance_trade(msg: dict, by_symbol: dict[str, str], now_ms: float) -> TradeTick | None:
    """Combined-stream ``aggTrade`` -> trade. ``m`` true means the BUYER was
    the maker, i.e. the aggressor sold."""
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
    return TradeTick(asset, "binance", price, qty, aggressor_buy=not bool(d.get("m")),
                     ts_ms=now_ms, event_ts_ms=_maybe(d.get("T") or d.get("E")), sequence=_int(d.get("a")))


def parse_bybit(msg: dict, by_symbol: dict[str, str], now_ms: float,
                state: dict[str, list[float | None]]) -> SpotQuote | None:
    """``orderbook.1.<SYMBOL>`` snapshot/delta -> quote.

    Level-1 deltas carry only the side that changed, so the last seen bid/ask
    per symbol is kept in ``state`` and the quote is emitted once both exist.
    """
    topic = msg.get("topic", "")
    if not topic.startswith("orderbook.1."):
        return None
    sym = topic.split(".")[-1]
    asset = by_symbol.get(sym)
    if asset is None:
        return None
    d = msg.get("data") or {}
    st = state.setdefault(sym, [None, None, None, None])     # bid, ask, bq, aq
    try:
        if d.get("b"):
            st[0], st[2] = float(d["b"][0][0]), float(d["b"][0][1])
        if d.get("a"):
            st[1], st[3] = float(d["a"][0][0]), float(d["a"][0][1])
    except (IndexError, TypeError, ValueError):
        return None
    if st[0] is None or st[1] is None or st[0] <= 0 or st[1] <= 0:
        return None
    ev = msg.get("ts")
    return SpotQuote(asset, "bybit", st[0], st[1], _microprice(st[0], st[1], st[2], st[3]), now_ms,
                     float(ev) if isinstance(ev, (int, float)) else None,
                     bid_size=st[2] or None, ask_size=st[3] or None, sequence=_int(d.get("seq")))


def parse_bybit_trades(msg: dict, by_symbol: dict[str, str], now_ms: float) -> list[TradeTick]:
    """``publicTrade.<SYMBOL>`` -> trades. ``S`` is the taker side."""
    topic = msg.get("topic", "")
    if not topic.startswith("publicTrade."):
        return []
    asset = by_symbol.get(topic.split(".")[-1])
    if asset is None:
        return []
    out: list[TradeTick] = []
    for d in msg.get("data") or []:
        try:
            price, qty = float(d["p"]), float(d["v"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0 or qty <= 0:
            continue
        out.append(TradeTick(asset, "bybit", price, qty, aggressor_buy=str(d.get("S", "")).lower() == "buy",
                             ts_ms=now_ms, event_ts_ms=_maybe(d.get("T")), sequence=_int(d.get("i"))))
    return out


def parse_coinbase(msg: dict, by_product: dict[str, str], now_ms: float) -> SpotQuote | None:
    """Exchange feed ``ticker`` message -> quote."""
    if msg.get("type") != "ticker":
        return None
    asset = by_product.get(msg.get("product_id", ""))
    if asset is None:
        return None
    try:
        bid, ask = float(msg["best_bid"]), float(msg["best_ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    bq = _maybe(msg.get("best_bid_size"))
    aq = _maybe(msg.get("best_ask_size"))
    return SpotQuote(asset, "coinbase", bid, ask, _microprice(bid, ask, bq, aq), now_ms,
                     event_ts_ms=_iso_ms(msg.get("time")), bid_size=bq, ask_size=aq,
                     sequence=_int(msg.get("sequence")))


def parse_coinbase_trade(msg: dict, by_product: dict[str, str], now_ms: float) -> TradeTick | None:
    """The same ``ticker`` message carries the last trade: ``price``,
    ``last_size`` and ``side`` (the taker side)."""
    if msg.get("type") != "ticker":
        return None
    asset = by_product.get(msg.get("product_id", ""))
    if asset is None:
        return None
    price, qty = _maybe(msg.get("price")), _maybe(msg.get("last_size"))
    side = str(msg.get("side", "")).lower()
    if not price or not qty or side not in ("buy", "sell"):
        return None
    return TradeTick(asset, "coinbase", price, qty, aggressor_buy=(side == "buy"), ts_ms=now_ms,
                     event_ts_ms=_iso_ms(msg.get("time")), sequence=_int(msg.get("trade_id")))


def parse_kraken(msg: dict, by_symbol: dict[str, str], now_ms: float) -> list[SpotQuote]:
    """v2 ``ticker`` (bbo-triggered) -> quotes. ``timestamp`` (RFC 3339) is the ticker's own time."""
    if msg.get("channel") != "ticker":
        return []
    out: list[SpotQuote] = []
    for d in msg.get("data") or []:
        asset = by_symbol.get(d.get("symbol", ""))
        if asset is None:
            continue
        try:
            bid, ask = float(d["bid"]), float(d["ask"])
        except (KeyError, TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0:
            continue
        bq, aq = _maybe(d.get("bid_qty")), _maybe(d.get("ask_qty"))
        out.append(SpotQuote(asset, "kraken", bid, ask, _microprice(bid, ask, bq, aq), now_ms,
                             event_ts_ms=_iso_ms(d.get("timestamp")), bid_size=bq, ask_size=aq))
    return out


def parse_kraken_trades(msg: dict, by_symbol: dict[str, str], now_ms: float) -> list[TradeTick]:
    """v2 ``trade`` -> trades. ``side`` is the taker side; ``timestamp`` is RFC 3339."""
    if msg.get("channel") != "trade":
        return []
    out: list[TradeTick] = []
    for d in msg.get("data") or []:
        asset = by_symbol.get(d.get("symbol", ""))
        if asset is None:
            continue
        price, qty = _maybe(d.get("price")), _maybe(d.get("qty"))
        if not price or not qty or price <= 0 or qty <= 0:
            continue
        out.append(TradeTick(asset, "kraken", price, qty, aggressor_buy=str(d.get("side", "")).lower() == "buy",
                             ts_ms=now_ms, event_ts_ms=_iso_ms(d.get("timestamp")), sequence=_int(d.get("trade_id"))))
    return out


def _iso_ms(v) -> float | None:
    """Coinbase's ``2026-09-24T14:40:48.123456Z`` as epoch ms."""
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return None


# ---------------------------------------------------------------- composite

@dataclass(slots=True)
class _Basis:
    """EWMA of log(price_exchange / price_primary), with a sample count."""
    value: float = 0.0
    n: int = 0
    last_ts: float = 0.0


@dataclass(slots=True)
class CompositeSpot:
    """Latest quote per (asset, exchange) and the composite view across them.

    The first exchange in ``reference_order`` that has ever quoted an asset is
    that asset's primary; every other exchange's level is expressed relative
    to it. When the primary is stale, the others still combine at the
    primary's level, so the composite series has no level jumps.
    """
    max_age_ms: float = 3_000.0
    reference_order: tuple[str, ...] = ("binance", "bybit", "coinbase", "kraken")
    #: LEAD MODE (0 = off, the plain median of every fresh source). Each source's
    #: quote describes the market as of ``ts_ms - lag[source]`` (its measured feed
    #: delay from this machine: Coinbase ~55 ms, Bybit ~100, Binance ~125 on
    #: 2026-09-29). With it on, the composite is the median of only the sources
    #: whose information is within this many ms of the newest -- so a move that
    #: the nearest exchange shows first moves the composite at once instead of
    #: when a second exchange confirms it, which on the recording was ~120 ms
    #: later at 3-sigma shocks. A lone source further than ``lead_max_dev_bps``
    #: from the all-source median is a glitch, not a lead, and the median stands.
    lead_window_ms: float = 0.0
    lead_max_dev_bps: float = 25.0
    #: measured feed delay per source, ms (LiveBot keeps it from trade timestamps)
    lag: dict[str, float] = field(default_factory=dict)
    basis_halflife_s: float = 300.0
    #: pairs of co-fresh quotes needed before an exchange joins the composite
    basis_min_samples: int = 5
    #: quotes further apart than this are not compared for the basis
    pair_window_ms: float = 1_000.0
    #: no view until this long after an asset's first quote, so every exchange
    #: has had a chance to quote and the primary is settled before anything
    #: downstream (the vol estimator in particular) sees a price
    warmup_ms: float = 3_000.0
    quotes: dict[str, dict[str, SpotQuote]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    primary: dict[str, str] = field(default_factory=dict)
    basis: dict[str, dict[str, _Basis]] = field(default_factory=dict)
    first_seen: dict[str, float] = field(default_factory=dict)
    #: exchanges whose feed is running behind (measured from their own trade
    #: timestamps, see LiveBot._on_source_lag). A backlogged connection
    #: delivers quotes seconds old that carry no timestamp of their own, so
    #: they would enter the median -- and the basis -- as if they were fresh.
    lagging: set[str] = field(default_factory=set)

    def _rank(self, exchange: str) -> int:
        return self.reference_order.index(exchange) if exchange in self.reference_order else 99

    def update(self, q: SpotQuote) -> None:
        qs = self.quotes.setdefault(q.asset, {})
        qs[q.exchange] = q
        self.counts[q.exchange] = self.counts.get(q.exchange, 0) + 1
        self.first_seen.setdefault(q.asset, q.ts_ms)
        if q.exchange in self.lagging:
            return                  # stored, but never compared: an old price would bend the basis
        prim = self.primary.get(q.asset)
        if prim is None or self._rank(q.exchange) < self._rank(prim):
            # a better-ranked exchange takes over as the level reference; the
            # old primary's zero basis is now wrong and must be re-measured
            # (seen live: Coinbase quoted first, kept basis 0, and sat 9.7 bps
            # off Binance's level while counted as calibrated)
            self.basis[q.asset] = {q.exchange: _Basis(0.0, self.basis_min_samples)}
            self.primary[q.asset] = prim = q.exchange
        if q.exchange == prim:
            return
        # compare against the freshest calibrated source, chained to the primary level
        ref = self._reference(q.asset, q.ts_ms, exclude=q.exchange)
        if ref is None or abs(ref.ts_ms - q.ts_ms) > self.pair_window_ms:
            return
        bmap = self.basis.setdefault(q.asset, {})
        ref_basis = bmap[ref.exchange].value
        d = math.log(q.price / ref.price) + ref_basis
        b = bmap.get(q.exchange)
        if b is None:
            bmap[q.exchange] = _Basis(d, 1, q.ts_ms)
            return
        dt_s = max((q.ts_ms - b.last_ts) / 1000.0, 0.01) if b.last_ts else 1.0
        alpha = 1.0 - 0.5 ** (dt_s / self.basis_halflife_s)
        # a young estimate learns fast, a mature one slowly
        alpha = max(alpha, 1.0 / (b.n + 1))
        b.value += alpha * (d - b.value)
        b.n += 1
        b.last_ts = q.ts_ms

    def _calibrated(self, asset: str, exchange: str) -> bool:
        b = self.basis.get(asset, {}).get(exchange)
        return b is not None and b.n >= self.basis_min_samples

    def _reference(self, asset: str, now_ms: float, exclude: str | None = None) -> SpotQuote | None:
        best: SpotQuote | None = None
        for e in sorted(self.quotes.get(asset, {}), key=self._rank):
            q = self.quotes[asset][e]
            if e == exclude or e in self.lagging or now_ms - q.ts_ms > self.max_age_ms \
                    or not self._calibrated(asset, e):
                continue
            if best is None:
                best = q
        return best

    def adjusted(self, q: SpotQuote) -> float:
        b = self.basis.get(q.asset, {}).get(q.exchange)
        return q.price * math.exp(-b.value) if b is not None else q.price

    def fresh_quotes(self, asset: str, now_ms: float) -> list[SpotQuote]:
        return [q for q in self.quotes.get(asset, {}).values()
                if now_ms - q.ts_ms <= self.max_age_ms and q.exchange not in self.lagging
                and self._calibrated(asset, q.exchange)]

    def _lead_price(self, qs: list[SpotQuote], prices: list[float], med: float) -> float:
        """The lead-mode price (see ``lead_window_ms``), or the median when off."""
        if self.lead_window_ms <= 0.0 or len(qs) < 2:
            return med
        lag = self.lag
        info = [q.ts_ms - lag.get(q.exchange, 0.0) for q in qs]
        newest = max(info)
        lead = [p for p, t in zip(prices, info) if t >= newest - self.lead_window_ms]
        if len(lead) == len(qs):
            return med
        lp = statistics.median(lead)
        if med > 0.0 and abs(lp - med) / med * 1e4 > self.lead_max_dev_bps:
            return med
        return lp

    def price(self, asset: str, now_ms: float) -> float | None:
        """The composite price alone (what ``view`` reports as ``price``), without
        building the view. The event-driven shock pre-check asks on every spot
        quote, several hundred times a second; the full view was a twentieth of
        the live bot's CPU on 2026-09-29."""
        if now_ms - self.first_seen.get(asset, now_ms) < self.warmup_ms:
            return None
        qs = self.fresh_quotes(asset, now_ms)
        if not qs:
            return None
        if len(qs) == 1:
            return self.adjusted(qs[0])
        prices = [self.adjusted(q) for q in qs]
        med = statistics.median(prices)
        return self._lead_price(qs, prices, med)

    def view(self, asset: str, now_ms: float) -> CompositeView | None:
        if now_ms - self.first_seen.get(asset, now_ms) < self.warmup_ms:
            return None
        qs = self.fresh_quotes(asset, now_ms)
        if not qs:
            return None
        prices = [self.adjusted(q) for q in qs]
        med = statistics.median(prices)
        weights = [1.0 / max(q.ask - q.bid, 1e-9) for q in qs]
        wp = sum(w * p for w, p in zip(weights, prices)) / sum(weights)
        dev = max(abs(p - med) for p in prices) / med * 1e4 if med > 0 else float("nan")
        xs = (max(prices) - min(prices)) / med * 1e4 if med > 0 else float("nan")
        return CompositeView(
            asset=asset, price=self._lead_price(qs, prices, med), weighted_price=wp, n_sources=len(qs),
            deviation_bps=dev,
            cross_exchange_spread_bps=xs, age_ms=min(now_ms - q.ts_ms for q in qs),
            sources={q.exchange: q.price for q in qs},
            basis_bps={e: round(b.value * 1e4, 2) for e, b in self.basis.get(asset, {}).items()},
        )


# ---------------------------------------------------------------- adapters

OnQuote = Callable[[SpotQuote], None]
OnTrade = Callable[[TradeTick], None]
Clock = Callable[[], float]


def _touch(health: dict[str, FeedStatus] | None, name: str, clock: Clock, connected: bool | None = None,
           error: str | None = None, message: bool = False) -> None:
    if health is None:
        return
    st = health.setdefault(name, FeedStatus(name))
    now = clock()
    if message:
        st.messages += 1
        st.last_msg_ms = now
    if connected is not None and connected != st.connected:
        st.connected, st.since_ms = connected, now
        if connected:
            st.backoff_s = RECONNECT_MIN_S
    if error is not None:
        st.last_error = error


async def _loop(name: str, connect: Callable[[], Awaitable[None]], stop: asyncio.Event,
                clock: Clock, health: dict[str, FeedStatus] | None) -> None:
    """Reconnect forever with exponential backoff; quiet after the first warning."""
    backoff = RECONNECT_MIN_S
    while not stop.is_set():
        started = time.monotonic()
        try:
            await connect()
        except Exception as exc:                          # noqa: BLE001
            if stop.is_set():
                return
            st = health.setdefault(name, FeedStatus(name)) if health is not None else None
            # a connection that lived a while earned a fresh, short backoff
            if time.monotonic() - started > 30.0:
                backoff = RECONNECT_MIN_S
            _touch(health, name, clock, connected=False, error=f"{type(exc).__name__}: {exc}"[:120])
            if st is not None:
                st.reconnects += 1
                st.backoff_s = backoff
            n = st.reconnects if st is not None else 1
            msg = "%s feed dropped (%s); retry in %.0fs (attempt %d)"
            (log.warning if n == 1 or n % 10 == 0 else log.debug)(msg, name, type(exc).__name__, backoff, n)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, RECONNECT_MAX_S)
        else:
            if stop.is_set():
                return
            _touch(health, name, clock, connected=False, error="closed")
            await asyncio.sleep(RECONNECT_MIN_S)


OnSourceLatency = Callable[[str, float], None]

#: a feed this far behind (its own trade timestamps) for this long is
#: reconnected: a new connection skips the backlog queued on the old one
BACKLOG_RECONNECT_MS = 5_000.0
BACKLOG_HOLD_MS = 3_000.0


class FeedBacklog(RuntimeError):
    """The connection is delivering data seconds late; drop and reconnect."""


@dataclass(slots=True)
class _BacklogWatch:
    threshold_ms: float = BACKLOG_RECONNECT_MS
    hold_ms: float = BACKLOG_HOLD_MS
    since: float | None = None

    def behind(self, lag_ms: float, now_ms: float) -> bool:
        """True once the lag has stayed above the threshold for ``hold_ms``."""
        if lag_ms < self.threshold_ms:
            self.since = None
            return False
        if self.since is None:
            self.since = now_ms
        return now_ms - self.since >= self.hold_ms


def _lag(on_source_latency: OnSourceLatency | None, source: str, now: float, t: TradeTick,
         watch: _BacklogWatch | None = None) -> None:
    """Exchange trade time -> our receipt: reported per exchange, and a
    connection that stays seconds behind is dropped (FeedBacklog)."""
    if not t.event_ts_ms:
        return
    lag = max(0.0, now - t.event_ts_ms)
    if on_source_latency is not None:
        on_source_latency(source, lag)
    if watch is not None and watch.behind(lag, now):
        raise FeedBacklog(f"{source} {lag:.0f} ms behind")


async def run_binance(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                      on_latency: Callable[[float], None] | None = None, on_trade: OnTrade | None = None,
                      health: dict[str, FeedStatus] | None = None,
                      on_source_latency: OnSourceLatency | None = None) -> None:
    import websockets
    syms = {f"{a.lower()}usdt": a for a in assets if a not in BINANCE_UNLISTED}
    if not syms:
        return
    streams = "/".join(f"{s}@bookTicker" for s in syms) + "/" + "/".join(f"{s}@aggTrade" for s in syms)

    async def connect() -> None:
        watch = _BacklogWatch()
        async with websockets.connect(BINANCE_WS + streams, open_timeout=20, ping_interval=15,
                                      close_timeout=3) as ws:
            log.info("binance feed connected (%s)", ", ".join(syms.values()))
            _touch(health, "binance", clock, connected=True)
            while not stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=45)
                msg = orjson.loads(raw)
                now = clock()
                _touch(health, "binance", clock, message=True)
                q = parse_binance(msg, syms, now)
                if q is not None:
                    on_quote(q)
                    continue
                t = parse_binance_trade(msg, syms, now)
                if t is not None:
                    if on_trade is not None:
                        on_trade(t)
                    if on_latency is not None and t.event_ts_ms:
                        on_latency(max(0.0, now - t.event_ts_ms))
                    _lag(on_source_latency, "binance", now, t, watch)
    await _loop("binance", connect, stop, clock, health)


async def run_bybit(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                    on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                    on_source_latency: OnSourceLatency | None = None) -> None:
    import websockets
    syms = {f"{a}USDT": a for a in assets}

    async def connect() -> None:
        watch = _BacklogWatch()
        async with websockets.connect(BYBIT_WS, open_timeout=20, ping_interval=None, close_timeout=3) as ws:
            args = [f"orderbook.1.{s}" for s in syms] + [f"publicTrade.{s}" for s in syms]
            # Bybit caps the topics per subscribe request; one oversized
            # request was silently refused and the feed went quiet (3 messages
            # in 75 s). Ten per request is inside the limit.
            for i in range(0, len(args), 10):
                await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 10]}))
            log.info("bybit feed connected (%s)", ", ".join(syms.values()))
            _touch(health, "bybit", clock, connected=True)
            state: dict[str, list[float | None]] = {}
            last_ping = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - last_ping > 20:
                    await ws.send(json.dumps({"op": "ping"}))
                    last_ping = time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=25)
                except asyncio.TimeoutError:
                    continue
                msg = orjson.loads(raw)
                now = clock()
                _touch(health, "bybit", clock, message=True)
                q = parse_bybit(msg, syms, now, state)
                if q is not None:
                    on_quote(q)
                    continue
                if on_trade is not None or on_source_latency is not None:
                    for t in parse_bybit_trades(msg, syms, now):
                        if on_trade is not None:
                            on_trade(t)
                        _lag(on_source_latency, "bybit", now, t, watch)
    await _loop("bybit", connect, stop, clock, health)


async def run_coinbase(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                       on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                       on_source_latency: OnSourceLatency | None = None) -> None:
    import websockets
    prods = {f"{a}-USD": a for a in assets}

    async def connect() -> None:
        watch = _BacklogWatch()
        # the first ticker per product after subscribing is a snapshot of the
        # LAST trade, often seconds old: not a lagging feed, so not measured
        snapshot_seen: set[str] = set()
        async with websockets.connect(COINBASE_WS, open_timeout=20, ping_interval=15, close_timeout=3) as ws:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": list(prods),
                                      "channels": ["ticker", "heartbeat"]}))
            log.info("coinbase feed connected (%s)", ", ".join(prods.values()))
            _touch(health, "coinbase", clock, connected=True)
            while not stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=45)
                msg = orjson.loads(raw)
                now = clock()
                _touch(health, "coinbase", clock, message=True)
                q = parse_coinbase(msg, prods, now)
                if q is not None:
                    on_quote(q)
                    if on_trade is not None or on_source_latency is not None:
                        t = parse_coinbase_trade(msg, prods, now)
                        if t is not None:
                            if on_trade is not None:
                                on_trade(t)
                            if t.asset in snapshot_seen:
                                _lag(on_source_latency, "coinbase", now, t, watch)
                            else:
                                snapshot_seen.add(t.asset)
    await _loop("coinbase", connect, stop, clock, health)


async def run_kraken(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                     on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None,
                     on_source_latency: OnSourceLatency | None = None) -> None:
    import websockets
    syms = {f"{a}/USD": a for a in assets if a not in KRAKEN_UNLISTED}
    if not syms:
        return

    async def connect() -> None:
        watch = _BacklogWatch()
        async with websockets.connect(KRAKEN_WS, open_timeout=20, ping_interval=15, close_timeout=3) as ws:
            # one request per symbol: an unlisted pair fails alone instead of the batch
            for sym in syms:
                await ws.send(json.dumps({"method": "subscribe", "params": {
                    "channel": "ticker", "symbol": [sym], "event_trigger": "bbo", "snapshot": True}}))
                await ws.send(json.dumps({"method": "subscribe", "params": {
                    "channel": "trade", "symbol": [sym], "snapshot": False}}))
            log.info("kraken feed connected (%s)", ", ".join(syms.values()))
            _touch(health, "kraken", clock, connected=True)
            while not stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=45)
                msg = orjson.loads(raw)
                now = clock()
                _touch(health, "kraken", clock, message=True)
                quotes = parse_kraken(msg, syms, now)
                if quotes:
                    for q in quotes:
                        on_quote(q)
                    continue
                if msg.get("method") == "subscribe" and msg.get("success") is False:
                    log.warning("kraken refused a subscription: %s", str(msg.get("error"))[:120])
                    continue
                if on_trade is not None or on_source_latency is not None:
                    for t in parse_kraken_trades(msg, syms, now):
                        if on_trade is not None:
                            on_trade(t)
                        _lag(on_source_latency, "kraken", now, t, watch)
    await _loop("kraken", connect, stop, clock, health)


async def run_all(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                  exchanges: tuple[str, ...] = ("binance", "bybit", "coinbase"),
                  on_latency: Callable[[float], None] | None = None,
                  on_trade: OnTrade | None = None,
                  health: dict[str, FeedStatus] | None = None,
                  on_source_latency: OnSourceLatency | None = None) -> None:
    """Run the selected adapters until ``stop`` is set, then close them (bounded)."""
    tasks = []
    if "binance" in exchanges:
        tasks.append(asyncio.create_task(run_binance(assets, on_quote, clock, stop, on_latency, on_trade, health,
                                                     on_source_latency)))
    if "bybit" in exchanges:
        tasks.append(asyncio.create_task(run_bybit(assets, on_quote, clock, stop, on_trade, health,
                                                   on_source_latency)))
    if "coinbase" in exchanges:
        tasks.append(asyncio.create_task(run_coinbase(assets, on_quote, clock, stop, on_trade, health,
                                                      on_source_latency)))
    if "kraken" in exchanges:
        tasks.append(asyncio.create_task(run_kraken(assets, on_quote, clock, stop, on_trade, health,
                                                    on_source_latency)))
    extra = [x for x in exchanges if x not in ("binance", "bybit", "coinbase", "kraken")]
    if extra:
        from .sources import ADAPTERS, REFERENCE_ONLY     # the 2026-09-30 candidates, off unless named
        for x in extra:
            if x in REFERENCE_ONLY:
                raise ValueError(f"{x} is a settlement reference, not a composite source")
            run = ADAPTERS.get(x)
            if run is None:
                log.warning("unknown spot source %r ignored (known: %s)", x, ", ".join(sorted(ADAPTERS)))
                continue
            tasks.append(asyncio.create_task(run(assets, on_quote, clock, stop, on_trade, health, on_source_latency)))
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(t, timeout=5.0)
