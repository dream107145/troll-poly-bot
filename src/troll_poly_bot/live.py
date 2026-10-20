"""Live paper trading against real Polymarket data, across every listed asset.

Real books, real prices, real measured latency -- **simulated fills**. There is
no order-signing code anywhere in this repo, so nothing here can place a real
order even if it wanted to.

Pipeline
--------
    AssetRegistry (probe Gamma for every candidate asset)      market/discovery
      -> MarketMeta per window, with the venue fee schedule    feeds/markets
      -> CLOB websocket books                                  LiveBook (below)
      -> composite spot from Binance + Bybit + Coinbase        feeds/spot
      -> vol, TWAP state, strike at the window open            pricing/*
      -> StrategyEngine: features, probability, costs, gates   strategy/engine
      -> RiskManager: caps, sizing                             risk/limits
      -> PaperExchange with measured latency                   execution/paper
      -> settlement from the venue, evidence statistics        below

Every market that is *not* traded carries a reason code, counted and shown
on the dashboard. That table is the product: it says why the bot is sitting
out, which on these markets is most of the time.

The Binance/Chainlink caveat, stated plainly
---------------------------------------------
These markets settle on a Chainlink 60s TWAP stream, not on any exchange. The
composite spot is a *proxy*; settlement records what our own TWAP said next to
what the venue resolved, and ``basis_disagreements`` counts the times they
differed (~1% of windows on the archive).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import json
import logging
import math
import orjson
import random
import statistics
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

import websockets

from . import earnings as earnings_log
from .config import BotConfig
from .control import BUDGET_TO_RISK, ControlFile, apply_to, current_values
from . import ledger as ledger_file
from .execution.decisions import DecisionJournal
from .execution.forensics import TradeForensics
from .execution.outcomes import MarkoutTracker, SignalOutcomeTracker
from .execution.latency import (
    HOME_BROADBAND, PRESEND_PRIOR_MS, VENUE_RTT_PRIOR_MS, LatencyModel, LatencyProfile,
)
from .execution.paper import FeeModel, PaperExchange
from .execution.scenarios import scenario_named
from .history_store import HistoryStore
from .features.engine import FeatureEngine
from .features.fastmove import FastMove, FastMoveDetector
from .risk.guard import Guard
from .features.orderflow import OrderFlowCalibration, OrderFlowState
from .feeds.markets import MarketMeta, StrikeTracker, parse_market
from .feeds.polymarket import GAMMA_BASE, window_epoch
from .feeds.spot import (
    RECONNECT_MAX_S, RECONNECT_MIN_S, CompositeSpot, FeedStatus, SpotQuote, TradeTick, run_all,
)
from .market.discovery import AssetRegistry, FetchFailed
from .pricing.twap import TwapState
from .pricing.vol import EwmaVol, TwoScaleVol
from .risk.limits import OpenPosition, RiskManager
from .signals.costs import FeeSchedule
from .strategy.cheapside import CheapSideEngine
from .strategy.cross_market_arb import CrossMarketArbEngine
from .strategy.cross_market_correlation import CrossMarketCorrelationEngine
from .strategy.late_confidence import LateConfidenceEngine
from .strategy.book_momentum import BookMomentumEngine
from .strategy.rising_recovery import RisingRecoveryEngine
from .strategy.leader_pair import LeaderPairEngine
from .strategy.confirmation_momentum import ConfirmationMomentumEngine
from .strategy.preopen_maker import PreopenMakerEngine
from .strategy.copytrade import CopyTradeEngine
from .strategy.btc_hedge import BtcHedgeEngine
from .strategy.engine import Evaluation, Reason, StrategyEngine
from .strategy.fastclip import FastClipEngine
from .strategy.triangle import TriangleEngine
from .strategy.momentum import MomentumEngine
from .strategy.repriced_momentum import RepricedMomentumEngine
from .strategy.spot_lag import SpotLagEngine, skip_reason_text
from .types import BookLevel, Order, OrderBook, OrderState, Side, TimeInForce

log = logging.getLogger("live")

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

def _order_type_label(order) -> str:
    """How an order is sent, in the venue's words: an IOC entry goes out as FAK."""
    return "FAK" if order.tif is TimeInForce.IOC else order.tif.value


def _reject_label(order, reason: str) -> str:
    """The log's wording for why an order did not fill. The recorded reason code keeps its
    historical name (fok_unfillable) for every kill; the log says what actually happened."""
    if reason == "fok_unfillable":
        return ("FAK found nothing at the limit (fak_not_filled)" if order.tif is TimeInForce.IOC
                else "FOK could not fill in full (fok_unfillable)")
    return reason


#: Share counts below this are dust, not a position worth an order and a fee.
EXIT_EPS = 1e-9
#: Minimum gap between exit attempts on one market. A killed fill-or-kill
#: exit is retried while the condition holds; this keeps that to a rate the
#: hourly order cap can absorb instead of one order per tick.
EXIT_RETRY_MS = 2000.0
#: Same for entries. A killed fill-or-kill is re-sent the very next tick
#: while the edge holds -- SOL twice in one second, DOGE three times in two
#: -- which spends the live hourly order budget in about a minute.
ENTRY_RETRY_MS = 2000.0
#: Written into every forensics record so a change of rules is visible in the data.
STRATEGY_VERSION = "engine-2026-09-23-stale-quote-gate"
#: How often the guard re-evaluates its conditions.
GUARD_INTERVAL_MS = 1000.0
#: Rejections that mean the market did not honour the order (the kill-rate guard)
VENUE_KILLS = frozenset({"fok_unfillable", "no_liquidity", "quote_pulled", "venue_reject"})
#: The staleness gates follow the measured feed latency up to this multiple
#: of their configured value, never further.
MAX_GATE_WIDENING = 2.0
#: Feeds that poll rather than stream; silence from them is normal.
POLLED_FEEDS = frozenset({"gamma"})


class _Probe:
    """An empty stand-in for a forensics record that does not exist."""
    stages: dict = {}


TradeRecordProbe = _Probe()


# ─────────────────────────────── clock sync ────────────────────────────────


class ClockSync:
    """Offset between this machine's clock and the exchange's.

    The machine this was first run on was ~970ms FAST, and a wrong clock
    corrupts staleness gates, the strike instant and the "can this order land"
    check. Measured NTP-style, keeping the sample with the smallest round trip.
    """

    TIME_URL = "https://api.binance.com/api/v3/time"

    def __init__(self) -> None:
        self.offset_ms = 0.0          # local clock minus true time
        self.rtt_ms = 0.0
        self.synced = False

    def now(self) -> float:
        """Exchange-aligned epoch milliseconds."""
        return time.time() * 1000.0 - self.offset_ms

    async def resync(self, samples: int = 5) -> None:
        best: tuple[float, float] | None = None
        for _ in range(samples):
            try:
                t0 = time.time() * 1000.0
                body, _ = await asyncio.to_thread(self._fetch)
                t1 = time.time() * 1000.0
            except Exception:
                continue
            if not body:
                continue
            srv = _maybe_float(body.get("serverTime"))
            if srv is None:
                continue
            rtt = t1 - t0
            offset = (t0 + t1) / 2.0 - srv
            if best is None or rtt < best[0]:
                best = (rtt, offset)
            await asyncio.sleep(0.2)
        if best is not None:
            self.rtt_ms, self.offset_ms = best
            self.synced = True

    @staticmethod
    def _fetch():
        req = urllib.request.Request(ClockSync.TIME_URL,
                                     headers={"User-Agent": "troll-poly-bot/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r), None


# ────────────────────────────────── books ──────────────────────────────────


class LiveBook:
    """A book maintained from a snapshot plus ``price_change`` deltas.

    Two clocks per token: ``ts`` is the VENUE's timestamp on the last message
    that changed it, ``recv_ts`` the local (clock-corrected) time that message
    was processed. ``conn`` is the feed connection its snapshot arrived on: a
    book whose snapshot came over a connection that has since dropped is not
    a book at all until the resubscribe snapshot lands.
    """

    __slots__ = ("token_id", "bids", "asks", "ts", "recv_ts", "conn", "_best_bid", "_best_ask")

    #: levels kept per side. The strategy reads the touch, the depth within two ticks
    #: of it and a walk for one order's size; a venue snapshot can carry hundreds of
    #: levels, and parsing, holding and re-sorting them on every tick for every market
    #: was measurable. (No sorted-view cache on purpose: tests and tools assign the
    #: level dicts directly.)
    MAX_LEVELS = 40

    def __init__(self, token_id: str) -> None:
        self.token_id = token_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.ts: float = 0.0
        self.recv_ts: float = 0.0
        self.conn: int = 0
        self._best_bid: float | None = None
        self._best_ask: float | None = None

    def apply_snapshot(self, msg: dict, recv_ts: float | None = None, conn: int = 0) -> None:
        bids, asks = self._levels(msg.get("bids")), self._levels(msg.get("asks"))
        if len(bids) > self.MAX_LEVELS:
            bids = dict(sorted(bids.items(), key=lambda kv: -kv[0])[:self.MAX_LEVELS])
        if len(asks) > self.MAX_LEVELS:
            asks = dict(sorted(asks.items(), key=lambda kv: kv[0])[:self.MAX_LEVELS])
        self.bids, self.asks = bids, asks
        self.ts = _ts(msg.get("timestamp"))
        self.recv_ts = self.ts if recv_ts is None else recv_ts
        self.conn = conn
        self._best_bid = self._best_ask = None

    @staticmethod
    def _levels(raw) -> dict[float, float]:
        out: dict[float, float] = {}
        for lvl in raw or []:
            try:
                price, size = float(lvl["price"]), float(lvl["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0:
                out[price] = size
        return out

    def apply_change(self, change: dict, ts: float, recv_ts: float | None = None) -> None:
        try:
            price, size = float(change["price"]), float(change["size"])
        except (KeyError, TypeError, ValueError):
            return
        side = change.get("side")
        book = self.bids if side == "BUY" else self.asks if side == "SELL" else None
        if book is None:
            return
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
            if len(book) > 2 * self.MAX_LEVELS:
                # deltas add levels a snapshot would have trimmed; keep the sort in
                # to_order_book (every market, every tick) bounded
                keep = sorted(book.items(), key=lambda kv: -kv[0] if side == "BUY" else kv[0])[:self.MAX_LEVELS]
                book.clear()
                book.update(keep)
        self.ts = ts
        self.recv_ts = ts if recv_ts is None else recv_ts
        self._best_bid = _maybe_float(change.get("best_bid"))
        self._best_ask = _maybe_float(change.get("best_ask"))

    def to_order_book(self) -> OrderBook:
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:self.MAX_LEVELS]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:self.MAX_LEVELS]
        book = OrderBook(
            token_id=self.token_id,
            bids=[BookLevel(p, s) for p, s in bids],
            asks=[BookLevel(p, s) for p, s in asks],
            ts=self.ts,
            recv_ts=self.recv_ts,
        )
        if self._best_ask is not None and book.asks and book.asks[0].price < self._best_ask - 1e-9:
            book.asks = [l for l in book.asks if l.price >= self._best_ask - 1e-9]
        if self._best_bid is not None and book.bids and book.bids[0].price > self._best_bid + 1e-9:
            book.bids = [l for l in book.bids if l.price <= self._best_bid + 1e-9]
        return book


def outcome_of_row(row: dict) -> bool | None:
    """The OFFICIAL outcome of a Gamma market row: True = UP won, False = DOWN won, None = not resolved yet.

    ``outcomePrices`` are the market's trading prices until the market is resolved: a window that flips in its last
    seconds shows the loser at 0.9+ for up to a minute after the close (2026-10-02 19:20 window: the bot read DOWN
    at 19:25:43, Gamma resolved UP at 19:26:27, booked -49.13 instead of +0.87). So the prices count only once the
    row says the market is closed AND resolved, and only at 1 / 0 (>= 0.99); anything else is "ask again later"
    (the settle loop retries, then settles on its own TWAP, then the late resolution corrects it)."""
    if row.get("closed") is not True:
        return None
    status = str(row.get("umaResolutionStatus") or "").strip().lower()
    if status and status != "resolved":
        return None
    try:
        outcomes = json.loads(row.get("outcomes") or "[]")
        prices = [float(p) for p in json.loads(row.get("outcomePrices") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if len(outcomes) != len(prices) or not prices:
        return None
    lowered = [str(o).strip().lower() for o in outcomes]
    if "up" not in lowered:
        return None
    p_up = prices[lowered.index("up")]
    if p_up >= 0.99:
        return True
    if p_up <= 0.01:
        return False
    return None


def _ts(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return time.time() * 1000.0


def _maybe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sibling(trade_log: str | Path, base: str) -> Path:
    """``data/live_trades.TAG.jsonl`` -> ``data/<base>.TAG.jsonl``: a tagged bot
    keeps its own forensics, decisions and journal next to its own ledger."""
    p = Path(trade_log)
    name = p.name
    if name.startswith("live_trades"):
        return p.with_name(base + name[len("live_trades"):])
    return p.with_name(base + ".jsonl")


def _r4(v) -> float | None:
    """Round for a record; None for missing or NaN."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 4)


def _depth_near_touch(levels: list[BookLevel], best: float | None, ticks: float = 0.02, ask: bool = True) -> float:
    if best is None:
        return 0.0
    if ask:
        return sum(l.size for l in levels if l.price <= best + ticks + 1e-9)
    return sum(l.size for l in levels if l.price >= best - ticks - 1e-9)


# ───────────────────────────────── latency ─────────────────────────────────


class EmpiricalLatency(LatencyModel):
    """Bootstrap sampler over measured latencies.

    ``order`` holds REAL venue order round trips only (the POST, measured by
    the live adapter, or loaded from earlier live runs' forensics). Until
    there are enough of them the draw comes from ``VENUE_RTT_PRIOR_MS``, the
    round trips actually measured on this account -- never from the HTTP
    time of a Gamma GET, which is what this bucket used to hold (35 ms
    against a real 218-661 ms, so the paper engine landed its orders about
    fifteen times too early). ``presend`` is decision -> POST: queue wait and
    signing, measured live.
    """

    MIN_SAMPLES = 25
    CAP = 3000

    def __init__(self, seed_profile: LatencyProfile = HOME_BROADBAND, seed: int = 0,
                 rtt_prior: tuple[float, ...] = VENUE_RTT_PRIOR_MS,
                 presend_prior: tuple[float, ...] = PRESEND_PRIOR_MS) -> None:
        super().__init__(seed_profile, seed)
        self.spot: list[float] = []
        self.book: list[float] = []
        self.order: list[float] = []
        self.presend: list[float] = []
        self.gamma: list[float] = []
        #: per-exchange feed lag (exchange trade time -> our receipt), ms
        self.spot_by_source: dict[str, list[float]] = {}
        self.rtt_prior = tuple(rtt_prior)
        self.presend_prior = tuple(presend_prior)
        self._rand = random.Random(seed)
        self._rep: dict[str, float] | None = None
        self._rep_mono = 0.0

    #: the report sorts every bucket (up to 3,000 samples each); it was 5 % of
    #: the bot's CPU when rebuilt on every snapshot. Spot and book percentiles
    #: may be this stale; a new order, presend or Gamma sample clears it.
    #: sorting ~8 lists of 3000 samples per report was 4 % of CPU at 1 s (2026-09-29)
    REPORT_TTL_S = 5.0

    def _record(self, bucket: list[float], ms: float) -> None:
        if not math.isfinite(ms) or ms < 0 or ms > 60_000:
            return
        bucket.append(ms)
        if len(bucket) > self.CAP:
            del bucket[: len(bucket) - self.CAP]

    def record_spot(self, ms: float) -> None: self._record(self.spot, ms)
    def record_book(self, ms: float) -> None: self._record(self.book, ms)

    def record_order(self, ms: float) -> None:
        self._record(self.order, ms)
        self._rep = None

    def record_presend(self, ms: float) -> None:
        self._record(self.presend, ms)
        self._rep = None

    def record_gamma(self, ms: float) -> None:
        self._record(self.gamma, ms)
        self._rep = None

    def record_spot_source(self, source: str, ms: float) -> None:
        self._record(self.spot_by_source.setdefault(source, []), ms)

    def _draw_from(self, bucket: list[float], fallback) -> float:
        if len(bucket) < self.MIN_SAMPLES:
            return fallback()
        return self._rand.choice(bucket)

    def _prior(self, values: tuple[float, ...]) -> float:
        # a smoothed bootstrap: a handful of measured values, +-15 % jitter,
        # so the draw is not eleven spikes
        if not values:
            return 0.0
        return self._rand.choice(values) * self._rand.uniform(0.85, 1.15)

    def order_rtt(self) -> float:
        return self._draw_from(self.order, lambda: self._prior(self.rtt_prior))

    def presend_delay(self) -> float:
        return self._draw_from(self.presend, lambda: self._prior(self.presend_prior))

    @property
    def order_source(self) -> str:
        return "measured" if len(self.order) >= self.MIN_SAMPLES else "prior"

    def md_spot(self) -> float:
        return self._draw_from(self.spot, super().md_spot)

    def md_book(self) -> float:
        return self._draw_from(self.book, super().md_book)

    def submit(self) -> float:
        """Decision -> the order reaches the matching engine: queue and
        signing, then half the POST round trip."""
        return self.presend_delay() + self.order_rtt() / 2.0

    def ack(self) -> float:
        return self.order_rtt() / 2.0

    def cancel(self) -> float:
        return self.order_rtt() / 2.0

    @staticmethod
    def _pct(vals, q: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        return s[min(len(s) - 1, max(0, int(q * len(s))))]

    def report(self) -> dict[str, float]:
        now = time.monotonic()
        if self._rep is not None and now - self._rep_mono < self.REPORT_TTL_S:
            return self._rep
        order = self.order if len(self.order) >= self.MIN_SAMPLES else self.rtt_prior
        presend = self.presend if len(self.presend) >= self.MIN_SAMPLES else self.presend_prior
        rep = {
            "spot_p50": self._pct(self.spot, 0.5), "spot_p95": self._pct(self.spot, 0.95),
            "book_p50": self._pct(self.book, 0.5), "book_p95": self._pct(self.book, 0.95),
            "order_rtt_p50": self._pct(order, 0.5), "order_rtt_p95": self._pct(order, 0.95),
            "presend_p50": self._pct(presend, 0.5), "presend_p95": self._pct(presend, 0.95),
            "gamma_rtt_p50": self._pct(self.gamma, 0.5),
            "n_spot": len(self.spot), "n_book": len(self.book), "n_order": len(self.order),
            "n_presend": len(self.presend),
        }
        for src, vals in sorted(self.spot_by_source.items()):
            rep[f"spot_p50_{src}"] = self._pct(vals, 0.5)
            rep[f"spot_p95_{src}"] = self._pct(vals, 0.95)
        self._rep, self._rep_mono = rep, now
        return rep

    @property
    def reaction_lag_ms(self) -> float:
        """Spot move -> our order at the venue: feed lag, then presend and half a POST."""
        rep = self.report()
        return rep["spot_p50"] + rep["presend_p50"] + rep["order_rtt_p50"] / 2.0


# ────────────────────────────────── state ──────────────────────────────────


@dataclass
class LiveMarket:
    meta: MarketMeta
    books: dict[str, LiveBook] = field(default_factory=dict)
    settled: bool = False
    submitted: int = 0
    inflight: int = 0
    #: when the last take-profit / stop order went out, for the retry throttle;
    #: -inf means never, so the first attempt is never held back
    last_exit_ms: float = float("-inf")
    #: when the last entry order went out; same throttle, same reason
    last_entry_ms: float = float("-inf")
    outcome: str | None = None
    pnl: float = 0.0
    #: closed early by the take-profit rule. Keeps us from re-entering the same
    #: window we just de-risked, and stops settlement from reporting it twice.
    exited: bool = False
    settle_attempts: int = 0
    next_settle_ms: float = 0.0

    @property
    def epoch(self) -> int:
        return int(self.meta.market.open_ts // 1000)


@dataclass
class Agreement:
    """Running check of the analytic fair value against the market mid, by
    time-to-expiry bucket, for the dashboard."""
    n: int = 0
    sum_diff: float = 0.0
    sum_abs: float = 0.0
    sum_sq: float = 0.0
    buckets: dict = field(default_factory=dict)
    EDGES = (300.0, 150.0, 90.0, 60.0, 40.0, 25.0, 15.0, 8.0, 0.0)

    @classmethod
    def bucket_of(cls, secs_left: float) -> str:
        for i in range(len(cls.EDGES) - 1):
            hi, lo = cls.EDGES[i], cls.EDGES[i + 1]
            if lo < secs_left <= hi:
                return f"{lo:.0f}-{hi:.0f}s"
        return "0s"

    def add(self, fair_up: float, mid_up: float, secs_left: float | None = None) -> None:
        d = fair_up - mid_up
        self.n += 1
        self.sum_diff += d
        self.sum_abs += abs(d)
        self.sum_sq += d * d
        if secs_left is not None:
            b = self.buckets.setdefault(self.bucket_of(secs_left), [0, 0.0, 0.0])
            b[0] += 1
            b[1] += d
            b[2] += abs(d)

    def report(self) -> dict:
        if not self.n:
            return {}
        return {
            "n": self.n,
            "mean_bias": round(self.sum_diff / self.n, 4),
            "mean_abs": round(self.sum_abs / self.n, 4),
            "rms": round((self.sum_sq / self.n) ** 0.5, 4),
            "by_secs_left": {
                k: {"n": v[0], "bias": round(v[1] / v[0], 4), "abs": round(v[2] / v[0], 4)}
                for k, v in sorted(self.buckets.items(),
                                   key=lambda kv: -float(kv[0].split("-")[0].rstrip("s") or 0))
                if v[0] >= 20
            },
        }


@dataclass
class LiveStats:
    started: float = field(default_factory=time.time)
    windows_seen: int = 0
    windows_traded: int = 0
    settled: int = 0
    wins: int = 0
    losses: int = 0
    basis_disagreements: int = 0
    unresolvable: int = 0
    realised_pnl: float = 0.0


class LiveBot:
    def __init__(
        self,
        assets: tuple[str, ...] = (),
        balance: float = 100.0,
        cfg: BotConfig | None = None,
        state_path: str = "data/live_state.json",
        trade_log: str = "data/live_trades.jsonl",
        exchanges: tuple[str, ...] | None = None,
        reset_history: bool = True,
        exchange=None,
        earnings_path: str = "data/earnings.jsonl",
        kill_file: str | None = None,
        dry_run_twin=None,
    ) -> None:
        self.cfg = cfg or BotConfig()
        #: the append-only, never-reset record the Earnings page reads. It
        #: follows state_path/trade_log so a test bot cannot write into the
        #: real one -- 265 fixture settlements did, and the page showed them.
        self.earnings_path = earnings_path
        self.cfg.starting_balance = balance
        self.exchanges = tuple(exchanges or self.cfg.feeds.exchanges)
        #: empty = trade every asset the venue lists; otherwise a restriction
        self.registry = AssetRegistry(candidates=self.cfg.feeds.candidate_assets,
                                      reprobe_s=self.cfg.feeds.reprobe_s,
                                      pinned=tuple(a.upper() for a in assets))

        self.latency = EmpiricalLatency(HOME_BROADBAND, seed=self.cfg.latency_seed)
        self._seed_measured_latency(Path(trade_log).parent)
        if exchange is not None:
            # a real venue adapter (execution/polymarket.py); its fee model is
            # the one the per-market schedules are written into
            self.exchange = exchange
            self.fees = exchange.fees
        else:
            self.fees = FeeModel.from_schedule(FeeSchedule())
            self.exchange = PaperExchange(self.latency, self.fees, starting_balance=balance,
                                          scenario=scenario_named(self.cfg.execution_scenario),
                                          seed=self.cfg.latency_seed)
        self.is_live = bool(getattr(self.exchange, "is_live", False))
        #: SHADOW: the real-money adapter in DRY RUN beside a paper main
        #: exchange. Positions, exits and PnL are the realistic paper engine's;
        #: every order is ALSO taken through the real path -- account
        #: reconciliation, signing, the pre-post checks -- and never posted.
        self.dry_twin = dry_run_twin
        self.dry_of: dict[str, str] = {}               # twin order id -> paper order id
        #: the emergency stop, honoured in EVERY mode -- the paper engine
        #: used to ignore it. ``<kill>`` stops new entries, ``<kill>_ALL``
        #: stops exits too. Checked immediately before an order is handed
        #: to the exchange, and again by the venue adapter right before it posts.
        self.kill_path = Path(kill_file or getattr(getattr(self.exchange, "caps", None), "kill_file", None)
                              or self.cfg.live.kill_file)
        #: MICRO-LIVE: ceilings the dashboard's control file may never exceed
        self.micro_caps = None
        if self.cfg.deploy_mode == "micro-live":
            from .graduation import MicroCaps
            self.micro_caps = MicroCaps.from_env()
        self.risk = RiskManager(cfg=self.cfg.risk, starting_balance=balance)
        if self.cfg.engine.strategy == "momentum_taker":
            self.engine = MomentumEngine(self.cfg.engine, self.cfg.momentum, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "spot_lag":
            # behind TPB_SPOT_LAG_ENABLED=1: the engine refuses to exist without it.
            # A MomentumEngine subclass, so the momentum exit and journal paths below apply.
            self.engine = SpotLagEngine(self.cfg.engine, self.cfg.spot_lag, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "repriced_momentum":
            # behind TPB_REPRICED_MOM_ENABLED=1; a SpotLagEngine subclass with its OWN config
            # (TPB_REPRICED_MOM_*) and budget -- spot-lag's config is not read
            self.engine = RepricedMomentumEngine(self.cfg.engine, self.cfg.repriced_momentum, self.risk,
                                                 FeatureEngine())
            self.engine.candidate_log_path = _sibling(trade_log, "repriced_momentum_candidates")
        elif self.cfg.engine.strategy == "cheapside":
            # behind TPB_CHEAPSIDE_ENABLED=1, PAPER ONLY (__main__.check_cheapside); a SpotLagEngine
            # subclass with its OWN config (TPB_CHEAPSIDE_*), budget and model file
            self.engine = CheapSideEngine(self.cfg.engine, self.cfg.cheapside, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "cross_market_arb":
            # behind TPB_CMA_ENABLED=1, PAPER ONLY (__main__.check_cross_market_arb); seven-market
            # guaranteed-profit portfolios with OWN config (TPB_CMA_*), reservations and risk caps
            self.engine = CrossMarketArbEngine(self.cfg.engine, self.cfg.cross_market_arb, self.risk,
                                               FeatureEngine())
        elif self.cfg.engine.strategy == "cross_market_correlation":
            # behind TPB_CMC_ENABLED=1, PAPER ONLY (__main__.check_cross_market_correlation); pair-wise
            # opposite-outcome correlation trades with OWN config (TPB_CMC_*), model, and risk caps
            self.engine = CrossMarketCorrelationEngine(
                self.cfg.engine, self.cfg.cross_market_correlation, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "late_confidence":
            # behind TPB_LATECONF_ENABLED=1, PAPER ONLY (__main__.check_late_confidence); a SpotLagEngine subclass
            # with its OWN config (TPB_LATECONF_*) and budget: the >0.98 token in the last 30 s when z >= 2
            self.engine = LateConfidenceEngine(self.cfg.engine, self.cfg.late_confidence, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "book_momentum":
            # behind TPB_BOOKMOM_ENABLED=1, PAPER ONLY (__main__.check_book_momentum): book-momentum taker + selective
            # maker bids on BTC 5m, short holds, one shared budget (TPB_BOOKMOM_*)
            self.engine = BookMomentumEngine(self.cfg.engine, self.cfg.book_momentum, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "rising_recovery":
            # behind TPB_RTR_ENABLED=1, PAPER ONLY (__main__.check_rising_recovery): maker bids on the rising token, the
            # opposite token bought only as pairs under $1 after the rise ends, held to settlement (TPB_RTR_*)
            self.engine = RisingRecoveryEngine(self.cfg.engine, self.cfg.rising_recovery, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "leader_pair":
            # behind TPB_LP_ENABLED=1, PAPER ONLY unless TPB_LP_ALLOW_REAL=1 (__main__.check_leader_pair): the user's
            # 2026-10-13 spec -- initial leader, 5-s opposite accumulation, FIFO pairs, falling-leader / recovery
            # state machine, held to settlement; IOC buys through the pair-order hooks; state persisted per data tag
            self.engine = LeaderPairEngine(self.cfg.engine, self.cfg.leader_pair, self.risk, FeatureEngine(),
                                           state_path=_sibling(trade_log, "leader_pair_state").with_suffix(".json"))
        elif self.cfg.engine.strategy == "copytrade":
            # behind TPB_COPY_ENABLED=1, PAPER ONLY (__main__.check_copytrade): follows one wallet's published fills
            # with FOK buys through the pair-order hooks
            self.engine = CopyTradeEngine(self.cfg.engine, self.cfg.copytrade, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "preopen_maker":
            # behind TPB_POM_ENABLED=1, PAPER ONLY (__main__.check_preopen_maker): resting GTC bids on both tokens of
            # the next market before its open, through the pair-order hooks; no taker entries
            self.engine = PreopenMakerEngine(self.cfg.engine, self.cfg.preopen_maker, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "confirmation_momentum":
            # behind TPB_CM_ENABLED=1, PAPER ONLY (__main__.check_confirmation_momentum); a SpotLagEngine
            # subclass: BTC spot shock confirmed across exchanges, the token already following, small clips held
            self.engine = ConfirmationMomentumEngine(self.cfg.engine, self.cfg.confirmation_momentum, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "btc_trend_reversal_hedge":
            # behind TPB_BTC_HEDGE_ENABLED=1, PAPER ONLY (__main__.check_btc_hedge)
            self.engine = BtcHedgeEngine(self.cfg.engine, self.cfg.btc_hedge, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "fastclip":
            # behind TPB_FASTCLIP_ENABLED=1, PAPER ONLY (__main__.check_fastclip); small clips on both
            # sides, held to settlement, with its OWN config (TPB_FASTCLIP_*) and budget
            self.engine = FastClipEngine(self.cfg.engine, self.cfg.fastclip, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "triangle":
            # behind TPB_TRIANGLE_ENABLED=1, PAPER ONLY (__main__.check_triangle): one share on every
            # filled sudden-move triangle (a spot-lag signal), held to settlement; OWN config (TPB_TRIANGLE_*)
            if self.cfg.triangle.hedge_mode == "dynamic":
                # the pair-lock layer. TPB_PAIRLOCK_STRATEGY picks the replay's strategy (2026-10-02): B = the v2
                # EV-based conditional hedge (strategy/pairlock_engine_v2.py, frozen); A / C / D / E = the
                # recovery-hedge / profit-lock engine (strategy/pairlock_engine.py)
                from .strategy.pairlock_engine import pairlock_strategy
                st = pairlock_strategy()
                if st == "B":
                    from .strategy.pairlock_engine_v2 import PairLockEngineV2
                    self.engine = PairLockEngineV2(self.cfg.engine, self.cfg.triangle, self.risk, FeatureEngine())
                elif st == "R":
                    # the DYNAMIC LOSS-RECOVERY engine (strategy/pairlock_recovery_engine.py, 2026-10-02 spec)
                    from .strategy.pairlock_recovery_engine import PairLockRecoveryEngine
                    self.engine = PairLockRecoveryEngine(self.cfg.engine, self.cfg.triangle, self.risk, FeatureEngine())
                else:
                    from .strategy.pairlock_engine import PairLockEngine
                    self.engine = PairLockEngine(self.cfg.engine, self.cfg.triangle, self.risk, FeatureEngine())
            else:
                self.engine = TriangleEngine(self.cfg.engine, self.cfg.triangle, self.risk, FeatureEngine())
        elif self.cfg.engine.strategy == "analytic":
            self.engine = StrategyEngine(self.cfg.engine, self.risk, FeatureEngine())
        else:
            raise ValueError(f"unknown strategy {self.cfg.engine.strategy!r}: analytic | momentum_taker | spot_lag"
                             " | repriced_momentum | cheapside | cross_market_arb | cross_market_correlation"
                             " | late_confidence | book_momentum | rising_recovery | leader_pair"
                             " | confirmation_momentum | preopen_maker | copytrade | fastclip"
                             " | btc_trend_reversal_hedge | triangle")
        # spot-lag shocks are evaluated the moment they arrive when event_driven is on
        # (TPB_SPOT_LAG_EVENT_DRIVEN=1, or the dashboard): see the _shock_driven property
        if isinstance(self.engine, SpotLagEngine):
            # the portfolio budget (TPB_SPOT_LAG_MAX_*, or the subclass's own) tightens the
            # global risk limits, never loosens them: in micro-live the MicroCaps ceilings
            # stay in force. engine.scfg IS cfg.spot_lag for spot_lag.
            sc = self.engine.scfg
            name = self.cfg.engine.strategy.upper()
            ceiling = self.micro_caps.max_order_usdc if self.micro_caps is not None else None
            for line in sc.apply_budget(self.risk.cfg, order_ceiling=ceiling):
                log.warning("%s budget  %s", name, line)
            if self.micro_caps is not None and self.micro_caps.max_orders_per_hour < sc.max_buys_per_hour:
                # the venue adapter refuses buys past the micro-live hourly ceiling;
                # the engine's own budget follows it, so the refusal is a clear [SKIP]
                # instead of an order sent to be refused (and a market attempt spent)
                log.warning("%s budget  max_buys_per_hour: %s -> %s (micro-live ceiling)", name,
                            sc.max_buys_per_hour, self.micro_caps.max_orders_per_hour)
                sc.max_buys_per_hour = int(self.micro_caps.max_orders_per_hour)
        #: the engine's structured [MARKET]/[SIGNAL]/[ORDER]/[POSITION]/[SKIP] lines
        self._slog = bool(getattr(self.engine, "structured_logs", False))
        #: may an exited market be entered again (the engine's max entries per market > 1)
        self._reentry = bool(getattr(self.engine, "allows_reentry", False))
        self.strategy_latency: LatencyProfile = HOME_BROADBAND
        #: the automatic kill switch for new entries (risk/guard.py) and the
        #: per-order lifecycle record (execution/forensics.py)
        self.guard = Guard()
        if isinstance(self.engine, SpotLagEngine) and self.cfg.deploy_mode == "paper":
            # PAPER ONLY: the loss-streak trip clears on a win, which cannot come while
            # entries are blocked -- it would end a forward test silently. Nothing is at
            # stake in paper; shadow and the real-money modes keep it.
            self.guard.cfg.max_consecutive_losses = 0
            # PAPER ONLY (2026-10-01, triangle): how long entries stay paused after a feed trip clears
            # (default 30 s). The book feed's reconnect brings a full snapshot; the freshness gates still apply.
            cd = (os.getenv("TPB_GUARD_COOLDOWN_S") or "").strip()
            if cd:
                self.guard.cfg.cooldown_s = float(cd)
        self.forensics = TradeForensics(_sibling(trade_log, "trade_forensics"),
                                        journal_path=_sibling(trade_log, "trade_journal"),
                                        strategy_version=STRATEGY_VERSION)
        self._last_guard_ms = 0.0
        #: why did we, or did we not, trade (execution/decisions.py)
        self.decisions = DecisionJournal(_sibling(trade_log, "decisions"))
        #: what happened AFTER: post-fill markouts (adverse selection) and the
        #: fate of every priced signal, traded or not (execution/outcomes.py)
        self.markouts = MarkoutTracker(_sibling(trade_log, "markouts"))
        self.outcomes = SignalOutcomeTracker(
            _sibling(trade_log, "signal_outcomes"),
            primary_horizon_s=min(20.0, self.cfg.momentum.max_hold_s),
            fee_per_share=FeeSchedule().taker_fee_per_share)
        self._loop: asyncio.AbstractEventLoop | None = None
        #: LIVE ONLY: a paper engine fed the same books and the same orders,
        #: so every live order carries what the simulator would have done
        self.shadow: PaperExchange | None = None
        self.shadow_of: dict[str, str] = {}             # shadow order id -> live order id
        if self.is_live:
            self.shadow = PaperExchange(self.latency, FeeModel.from_schedule(FeeSchedule()),
                                        starting_balance=balance,
                                        scenario=scenario_named(self.cfg.execution_scenario),
                                        seed=self.cfg.latency_seed)

        self.clock = ClockSync()
        if self.is_live:
            self.exchange.clock = self.clock.now
            # venue positions outside the markets we trade are legacy, not missed fills
            self.exchange.watched_tokens = lambda: set(self.token_index)
            # the real POST round trips feed the latency model paper and the
            # strategy both read; the adapter asks our freshest book for a
            # last look right before it posts
            if hasattr(self.exchange, "on_rtt"):
                self.exchange.on_rtt = self.latency.record_order
            if hasattr(self.exchange, "pre_post_check"):
                self.exchange.pre_post_check = self._pre_post_check
            if hasattr(self.exchange, "on_fill_event"):
                self.exchange.on_fill_event = self._on_venue_fill_threadsafe
                # closed markets still awaiting their result: the venue may redeem them before we settle
                self.exchange.settling_tokens = self._settling_tokens
                # SYNC WITH MANUAL TRADING (the user, 2026-10-05): trades made on polymarket.com in our markets are
                # adopted as ours; TPB_LIVE_SYNC_MANUAL=0 turns it off (they then latch the guard as before)
                if (os.environ.get("TPB_LIVE_SYNC_MANUAL") or "1").strip().lower() not in ("0", "false", "no", "off"):
                    self.exchange.adopt_external = True
                    self.exchange.on_external_fill = self._on_external_fill_threadsafe
                    self.exchange.on_external_position = self._external_mark
        elif hasattr(self.exchange, "on_arrival"):
            self.exchange.on_arrival = self._on_paper_arrival
        #: SHADOW: the real account's readiness for micro-live, reported, not enforced
        self.account_check = Guard()
        if self.dry_twin is not None:
            self.dry_twin.clock = self.clock.now
            self.dry_twin.watched_tokens = lambda: set(self.token_index)
            self.dry_twin.pre_post_check = self._pre_post_check
        self._base_spot_age = self.cfg.engine.max_spot_age_ms
        self._base_book_age = self.cfg.engine.max_book_age_ms
        #: Tunables the dashboard may change while we run. ``control_scope``
        #: keeps a paper bot and a real-money bot in one directory steerable
        #: apart: each reads the shared "all" section plus its own.
        self.controls = ControlFile()
        self.control_scope = "live" if self.is_live else "paper"
        self.control_applied: list[str] = []
        self.control_errors: list[str] = []
        self.control_at: float | None = None
        self.tracker = StrikeTracker(tolerance_ms=3000.0)
        self.markets: dict[str, LiveMarket] = {}
        self.token_index: dict[str, tuple[str, LiveMarket]] = {}

        self.composite = CompositeSpot(max_age_ms=self.cfg.feeds.max_quote_age_ms)
        # lead mode (feeds/spot.py CompositeSpot.lead_window_ms): the composite follows the
        # nearest exchange's quote instead of waiting for a second one to confirm a move
        try:
            self.composite.lead_window_ms = max(0.0, float(os.getenv("TPB_SPOT_LEAD_WINDOW_MS", "0") or 0.0))
            self.composite.lead_max_dev_bps = max(1.0, float(os.getenv("TPB_SPOT_LEAD_MAX_DEV_BPS", "25") or 25.0))
        except ValueError:
            self.composite.lead_window_ms = 0.0
        self.vol: dict[str, object] = {}
        self.twap: dict[str, TwapState] = {}
        self.spot: dict[str, float] = {}
        self.spot_ts: dict[str, float] = {}
        self._last_model_update: dict[str, float] = {}
        self.orderflow: dict[str, OrderFlowState] = {}
        self._last_calib_ms: dict[str, float] = {}
        self.feed_health: dict[str, FeedStatus] = {}
        self.discovery_errors = 0
        self.last_discovery_error = ""

        self.stats = LiveStats()
        self.agreement: dict[str, Agreement] = {}
        self.pnl_by_asset: dict[str, float] = {}
        self.epoch_pnl: dict[int, float] = {}
        self.exec_rejections: dict[str, int] = {}
        self._last_mark: dict[str, float] = {}
        self.fair_by_order: dict[str, dict] = {}
        self.price_history: dict[str, deque] = {}
        self._last_hist_ms = 0.0
        #: spot-lag event-driven evaluation (_maybe_shock_eval)
        self._last_shock_eval: dict[str, float] = {}
        self.last_fast_move: dict[str, FastMove] = {}
        self.shock_evals = 0
        #: the fast feed (strategy/spot_lag.py fast_feed): raw quotes at tick resolution
        self.fast: FastMoveDetector | None = None
        self.fast_evals = 0
        self._fast_key: tuple | None = None
        self._sync_fast_feed()
        #: newest exchange trade -> the engine's first look at a shock, ms
        self.shock_decision_age: deque[float] = deque(maxlen=500)
        self._last_shock_seen: dict[str, float] = {}
        #: (shock id, reason) pairs already written as [SKIP] (repriced_momentum)
        self._skip_seen: dict[tuple[str, str], None] = {}
        #: when each exchange last reported a trade-timestamp lag (_on_source_lag)
        self._source_lag_seen: dict[str, float] = {}
        #: last trailing 60 s TWAP per asset, from _tick (display only)
        self._twap60: dict[str, float | None] = {}
        #: how late the event loop wakes a 50 ms sleep, ms (loop_lag_loop)
        self.loop_lag: deque[float] = deque(maxlen=1200)
        self.ledger: list[dict] = []

        self.state_path = Path(state_path)
        # strikes survive a restart: a market already open is traded at once with the strike taken at its open
        self.tracker.attach_store(self.state_path.with_name(self.state_path.name.replace("live_state", "strikes", 1)
                                                            if "live_state" in self.state_path.name else
                                                            f"strikes.{self.state_path.name}"))
        self.trade_log = Path(trade_log)
        self.chart_dir = Path(self.cfg.chart_dir)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_log.parent.mkdir(parents=True, exist_ok=True)
        # chart points go to per-market append-only files, not the state file
        # (history_store.py: a 5 MB state rewrite froze the event loop)
        self.history = HistoryStore(self.state_path)
        if reset_history:
            self._reset_trade_log()

        self._subscribed: set[str] = set()
        #: the venue book feed: which connection we are on and how far our view
        #: of the venue has got (the venue timestamp of the newest message
        #: processed, and when we processed it). A token's quote age is
        #: measured against this, so a backlogged or silent feed ages every book.
        self._book_conn = 0
        #: the connections whose snapshots are current: one, or two during a handover
        self._book_conns_ok: set[int] = set()
        #: assets whose tokens are tracked but NOT subscribed on the book feed
        #: (TPB_BOOK_SKIP_ASSETS, e.g. an asset the strategy excludes): fewer frames
        self._book_skip_assets = {a.strip().upper() for a in os.getenv("TPB_BOOK_SKIP_ASSETS", "").split(",") if a.strip()}
        self._book_last_venue_ts = 0.0
        self._book_last_recv_ts = 0.0
        #: newest exchange-side event time of a spot trade per asset (the fallback T0
        #: before any stamped quote has arrived)
        self.spot_event_ts: dict[str, float] = {}
        #: SPOT AGE, measured (2026-09-30). Per asset, the newest price information that
        #: has reached us: (exchange time of it, our receipt time, source). The exchange
        #: time is the venue's own stamp on the quote where it sends one (Coinbase,
        #: Kraken, Bitstamp, Deribit, OKX, Bybit ...), else receipt minus that source's
        #: measured delay (Binance's bookTicker carries no time). ``_spot_used`` is what
        #: the model last consumed; a decision's spot age is now minus its exchange time.
        #: Before this, the age was (now - model update) + the Binance-only trade lag,
        #: and with Binance off that lag was a hard-coded 120 ms.
        self._spot_newest: dict[str, tuple[float, float, str]] = {}
        self._spot_used: dict[str, tuple[float, float, str]] = {}
        #: sources whose quotes carry the venue's own time: their delay is measured on
        #: every quote, and their trade-based lag no longer feeds the per-source report
        self._quote_stamped: set[str] = set()
        self._resub = asyncio.Event()
        self._stop = asyncio.Event()
        self._spot_task: asyncio.Task | None = None
        self._spot_stop: asyncio.Event | None = None
        #: positions carried over a restart: slug -> the saved record, until they close
        self._restored_meta: dict[str, dict] = {}
        #: working PAIR orders (triangle): order id -> what is needed to re-place it after a restart,
        #: saved with the positions; and the saved ones still to re-place once their market's book is live
        self._pairs_open: dict[str, dict] = {}
        self._pairs_to_place: list[dict] = []
        #: PAIRED-EXIT (engines with ``manages_exits``): every exit / ladder order the engine asked for, by id
        self._managed_orders: dict[str, Order] = {}
        #: LATE RESOLUTION (2026-10-01): windows abandoned for "no venue outcome", still asked for their
        #: result until LATE_RESOLVE_MS after the close; slug -> what booking it needs (see _book_late). Defined
        #: before the restore: a real account's positions in closed markets are restored INTO this queue
        self._late: dict[str, dict] = {}
        if getattr(self.engine, "restores_positions", False):
            self._restore_positions()
        if not self.is_live:
            self._queue_ledger_late()

    # ───────────────────── open positions across restarts ─────────────────────
    #
    # Engines that set ``restores_positions`` (spot_lag) write every open position,
    # one per market, plus today's realised PnL, to data/open_positions<tag>.json
    # whenever one opens or closes, and pick them up on start: the exposure counts
    # against the global budget again, the market counts as entered, and the timed
    # exit runs from the original fill. A file written by another mode (paper vs
    # real money) is never restored, and in live mode a restored position the venue
    # does not confirm is dropped at the first reconciliation.

    def _positions_file(self) -> Path:
        name = self.state_path.name
        name = name.replace("live_state", "open_positions") if "live_state" in name else f"{name}.positions"
        return self.state_path.with_name(name)

    def _persist_positions(self) -> None:
        if not getattr(self.engine, "restores_positions", False):
            return
        rows = []
        entry_ms = getattr(self.engine, "entry_ms", {})
        for slug, op in self.risk.open.items():
            lm = self.markets.get(slug)
            rec = self._restored_meta.get(slug, {})
            if lm is not None:
                # EVERY side held, one row each: a two-sided engine (triangle's pair orders, btc_hedge,
                # fastclip) holds Up and Down in one market; saving only the first fill's side dropped the
                # other at a restart (2026-10-01: 10 Up shares lost)
                m = lm.meta.market
                held = [(side, tok) for side, tok in (("UP", m.yes_token_id), ("DOWN", m.no_token_id))
                        if (p := self.exchange.positions.get(tok)) is not None and p.shares > EXIT_EPS]
                close_ts = m.close_ts
            else:
                held = [(op.side, rec.get("token_id"))] if rec.get("token_id") else []
                close_ts = rec.get("close_ts", 0.0)
            for side, token in held:
                pos = self.exchange.positions.get(token) if token else None
                if pos is None or pos.shares <= EXIT_EPS:
                    continue
                # one side: the market's committed USDC, as before; two: each side's own cost
                usdc = op.usdc if len(held) == 1 else pos.cost_basis + pos.fees_paid
                rows.append({"slug": slug, "asset": op.asset, "epoch": op.epoch, "side": side, "token_id": token,
                             "shares": round(pos.shares, 6), "cost_basis": round(pos.cost_basis, 6),
                             "fees_paid": round(pos.fees_paid, 6), "usdc": round(usdc, 6),
                             "entry_ms": entry_ms.get(token), "close_ts": close_ts})
        persist = getattr(self.engine, "persist_state", None)
        hist_export = getattr(self.risk, "balance_history_export", None)
        doc = {"version": 1, "saved_ms": self.clock.now(), "mode": self.mode_label, "day": self.risk.day,
               "realised_today": round(self.risk.realised_today, 6),
               "balance_history": hist_export() if callable(hist_export) else [],
               "positions": rows,
               # an engine's own per-market state (pairlock cycles), restored with the positions
               "engine_state": persist() if callable(persist) else None,
               # the working pair orders (triangle): the paper venue forgets resting orders at a restart
               "pairs": list(self._pairs_open.values()) + [{k: v for k, v in p.items() if k != "placeholder"}
                                                           for p in self._pairs_to_place]}
        path = self._positions_file()
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(doc), encoding="utf-8")
            tmp.replace(path)                       # atomic: a crash leaves the old file or the new one
        except OSError:
            log.warning("could not write %s", path)

    def _restore_positions(self) -> None:
        path = self._positions_file()
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if doc.get("mode") != self.mode_label:
            log.warning("[POSITION] %s was written by a %s run: NOT restored into this %s run",
                        path, doc.get("mode"), self.mode_label)
            return
        if doc.get("day") == self.risk.day:
            # the daily loss budget survives a restart; the loss STREAK does not
            # (restarting after a streak halt is the operator's acknowledgement)
            self.risk.realised_today = float(doc.get("realised_today") or 0.0)
        restore_hist = getattr(self.risk, "balance_history_restore", None)
        if callable(restore_hist):
            restore_hist(doc.get("balance_history"))
        now = self.clock.now()
        for r in doc.get("positions") or []:
            slug, token = r.get("slug"), r.get("token_id")
            if not slug or not token or float(r.get("shares") or 0.0) <= EXIT_EPS:
                continue
            if float(r.get("close_ts") or 0.0) <= now:
                if not self.is_live:
                    log.warning("[POSITION] %s %s: its market closed while the bot was down; left to the venue's "
                                "settlement / redemption, not restored", slug, r.get("side"))
                    continue
                # a REAL account: the venue will pay (or has paid) this position. Restored, and queued for its result,
                # so the payout is a settlement the books expect -- not unexplained cash that latches the guard
                # (2026-10-05 19:52: +7.53 USDC from two such positions stopped the bot)
                pos = self.exchange.positions[token]
                pos.token_id = token
                pos.shares = float(r["shares"])
                pos.cost_basis = float(r.get("cost_basis") or 0.0)
                pos.fees_paid = float(r.get("fees_paid") or 0.0)
                it = self._late.setdefault(slug, {"asset": r.get("asset", ""), "epoch": int(r.get("epoch") or 0), "marked": 0.0,
                                                  "marked_ts": now, "credit": False, "stats": True, "next_ms": now,
                                                  "until": float(r.get("close_ts") or now) + self.LATE_RESOLVE_MS, "tokens": {}})
                it.setdefault("tokens", {})[token] = r.get("side", "")
                log.warning("[POSITION] %s %s restored into a closed market: %.2f sh, cost %.2f USDC; its result is looked "
                            "up and the venue's payout is expected", slug, r.get("side"), pos.shares, pos.cost_basis)
                continue
            pos = self.exchange.positions[token]
            pos.token_id = token
            pos.shares = float(r["shares"])
            pos.cost_basis = float(r.get("cost_basis") or 0.0)
            pos.fees_paid = float(r.get("fees_paid") or 0.0)
            if not self.is_live:
                # paper: the fresh balance pays for what the previous run already held
                self.exchange.balance -= pos.cost_basis + pos.fees_paid
            held = self.risk.open.get(slug)
            if held is not None:
                # the market's other side (a two-sided engine): one market position, both sides' shares
                held.usdc += float(r.get("usdc") or pos.cost_basis)
                held.shares += pos.shares
            else:
                self.risk.open[slug] = OpenPosition(slug, r.get("asset", ""), int(r.get("epoch") or 0),
                                                    r.get("side", ""), float(r.get("usdc") or pos.cost_basis),
                                                    pos.shares)
            restore = getattr(self.engine, "restore_entry", None)
            if callable(restore):
                restore(slug, token, float(r.get("entry_ms") or 0.0), float(r.get("close_ts") or 0.0))
            self._restored_meta.setdefault(slug, dict(r))
            log.warning("[POSITION] %s %s restored: %.2f sh, cost %.2f USDC (entry %s)", slug, r.get("side"),
                        pos.shares, pos.cost_basis,
                        time.strftime("%H:%M:%S", time.gmtime(float(r.get("entry_ms") or 0.0) / 1000.0)))
        if self._restored_meta:
            log.warning("[POSITION] %d position(s) restored; exposure %.2f of %.2f USDC",
                        len(self._restored_meta), self.risk.exposure_total(), self.risk.cfg.max_total_exposure_usdc)
        # Paper keep-history previously only subtracted open costs from --balance, so
        # realized PnL vanished on every restart (cash jumped back toward the start).
        # Re-anchor cash to the last marked equity in balance_history.
        if not self.is_live:
            hist = doc.get("balance_history") or []
            if hist:
                try:
                    last_eq = float(hist[-1][1])
                except (TypeError, ValueError, IndexError):
                    last_eq = None
                if last_eq is not None:
                    inv_cost = 0.0
                    for pos in self.exchange.positions.values():
                        if abs(pos.shares) > EXIT_EPS:
                            inv_cost += float(pos.cost_basis + pos.fees_paid)
                    # Marks are not ready yet; cost is the restore-time proxy for inventory.
                    self.exchange.balance = last_eq - inv_cost
                    log.warning("[POSITION] paper cash re-anchored to %.2f (last equity %.2f − open cost %.2f)",
                                self.exchange.balance, last_eq, inv_cost)
        restore_state = getattr(self.engine, "restore_state", None)
        if callable(restore_state):
            restore_state(doc.get("engine_state"), now)
        hold = getattr(self.engine, "on_pair_submit", None)
        restored_pairs = list(doc.get("pairs") or [])
        if getattr(self.engine, "one_pair_per_market_side", False):
            unique, seen = [], set()
            for pr in restored_pairs:
                key = (pr.get("slug"), pr.get("side"))
                if key in seen:
                    log.warning("[PAIR] %s %s duplicate saved order skipped during restore", *key)
                    continue
                seen.add(key)
                unique.append(pr)
            restored_pairs = unique
        for i, pr in enumerate(restored_pairs):
            if float(pr.get("close_ts") or 0.0) <= now or not pr.get("token_id") or pr.get("tif") == "FOK":
                continue                                  # a FOK hedge in flight at the stop resolved at the venue
            if pr.get("cancelling"):
                continue                                  # being pulled at the stop: the engine re-places its bid
            pid = f"restored-{i}-{pr.get('pair_of', '')}"
            if callable(hold):
                # it holds new buys (wait_for_pair) from now, not only once it is re-placed
                hold(pid, pr["slug"], pr["side"], float(pr["price"]), float(pr["size"]))
            self._pairs_to_place.append({**pr, "placeholder": pid})
            log.warning("[PAIR] %s %s pair restored: %.2f sh at <= %.2f, re-placed once its book is live",
                        pr["slug"], pr["side"], float(pr["size"]), float(pr["price"]))

    def _drop_unconfirmed_restores(self, report: dict) -> set[str]:
        """Live: a restored position the venue does not hold (sold or settled while
        we were down) is dropped, not kept as a phantom. Returns the dropped tokens."""
        if not self._restored_meta or not report.get("checked") or report.get("watched_n", 1) <= 0:
            return set()
        missing = {u["token_id"] for u in report.get("missing_positions", [])}
        dropped = set()
        for slug, rec in list(self._restored_meta.items()):
            token = rec.get("token_id")
            if token in missing:
                pos = self.exchange.positions.get(token)
                if pos is not None:
                    pos.shares = pos.cost_basis = pos.fees_paid = 0.0
                self.risk.open.pop(slug, None)
                getattr(self.engine, "entry_ms", {}).pop(token, None)
                self._restored_meta.pop(slug, None)
                dropped.add(token)
                log.warning("[POSITION] %s restored position is not on the venue: dropped", slug)
            elif slug not in self.risk.open:
                self._restored_meta.pop(slug, None)          # closed normally since
        if dropped:
            self._persist_positions()
        return dropped

    # ───────────────────────────── structured log ─────────────────────────────

    @staticmethod
    def _mkt_label(m) -> str:
        """"BTC 14:05-14:10": one asset in one 5-minute window."""
        fmt = lambda ms: time.strftime("%H:%M", time.gmtime(ms / 1000.0))  # noqa: E731
        return f"{m.asset} {fmt(m.open_ts)}-{fmt(m.close_ts)}"

    def _portfolio_line(self) -> str:
        r = self.risk
        return (f"{len(r.open)} open / {r.cfg.max_concurrent_positions} max, exposure "
                f"{r.exposure_total():.2f} of {r.cfg.max_total_exposure_usdc:.2f} USDC")

    def _skip_log(self, ev: Evaluation, lm: LiveMarket, reason: Reason | None, detail: str,
                  text: str | None = None) -> None:
        """[SKIP] for a NEW spot-lag signal that did not become an order, with the reason
        (repriced_momentum: a candidate its own gates passed; once per shock and reason)."""
        fk = getattr(self.engine, "feature_key", "spot_lag")
        if not self._slog or not (ev.features or {}).get(fk, {}).get("signal"):
            return
        sl = ev.features[fk]
        side = sl.get("direction") or ("UP" if sl.get("z1", 0) > 0 else "DOWN")
        msg = text if text is not None else skip_reason_text(reason, detail)
        if sl.get("shock_id"):
            # the momentum candidate recurs every tick of its window: say it once
            key = (sl["shock_id"], msg.split(" (")[0])
            if key in self._skip_seen:
                return
            self._skip_seen[key] = None
            while len(self._skip_seen) > 500:
                self._skip_seen.pop(next(iter(self._skip_seen)))
        log.info("[SKIP] %s %s: %s", self._mkt_label(lm.meta.market), side, msg)

    def _seed_measured_latency(self, data_dir: Path, limit: int = 3000) -> None:
        """Start from the venue round trips earlier REAL runs measured.

        Every live POST writes ``venue_rtt_ms`` (and the decision -> POST
        ``presend_ms``) into its forensics trade line. A paper bot started
        next to them draws its order latency from those instead of the
        11-sample prior, so paper learns what the venue actually did.
        """
        rtts: list[float] = []
        pres: list[float] = []
        try:
            files = sorted(data_dir.glob("trade_forensics*.jsonl"))
        except OSError:
            return
        for fpath in files:
            try:
                lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()[-20_000:]
            except OSError:
                continue
            for line in lines:
                if '"venue_rtt_ms"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") != "trade" or row.get("mode") != "LIVE-ARMED":
                    continue
                if isinstance(row.get("venue_rtt_ms"), (int, float)):
                    rtts.append(float(row["venue_rtt_ms"]))
                if isinstance(row.get("presend_ms"), (int, float)):
                    pres.append(float(row["presend_ms"]))
        for v in rtts[-limit:]:
            self.latency.record_order(v)
        for v in pres[-limit:]:
            self.latency.record_presend(v)
        if rtts:
            log.info("order latency seeded from %d measured venue round trips (p50 %.0f ms)",
                     len(rtts), sorted(rtts)[len(rtts) // 2])

    # ───────────────────────────── per-asset state ─────────────────────────

    def _make_vol(self):
        v = self.cfg.vol
        fast = EwmaVol(grid_ms=v.grid_ms, halflife_s=v.halflife_s,
                       floor_per_sec=v.floor_per_sec, ceil_per_sec=v.ceil_per_sec)
        if not v.two_scale:
            return fast
        return TwoScaleVol(
            fast=fast,
            slow=EwmaVol(grid_ms=v.slow_grid_ms, halflife_s=v.slow_halflife_s,
                         floor_per_sec=v.floor_per_sec, ceil_per_sec=v.ceil_per_sec),
            default_ratio=v.default_ratio,
        )

    def _ensure_asset(self, asset: str) -> None:
        if asset not in self.vol:
            self.vol[asset] = self._make_vol()
            self.twap[asset] = TwapState()
            self._last_model_update[asset] = 0.0
        self.orderflow.setdefault(asset, OrderFlowState())
        self.engine.ofi_calibration.setdefault(asset, OrderFlowCalibration())

    def _feed_state(self, name: str, connected: bool | None = None, error: str | None = None,
                    message: bool = False) -> FeedStatus:
        st = self.feed_health.setdefault(name, FeedStatus(name))
        now = self.clock.now()
        if message:
            st.messages += 1
            st.last_msg_ms = now
        if connected is not None and connected != st.connected:
            st.connected, st.since_ms = connected, now
            if connected:
                st.backoff_s = RECONNECT_MIN_S
        if error is not None:
            st.last_error = error
        return st

    @property
    def assets(self) -> tuple[str, ...]:
        return self.registry.assets

    @property
    def _shock_driven(self) -> bool:
        """Evaluate spot-lag shocks the moment a quote shows one. Read on every quote,
        so the dashboard can switch it without a restart."""
        eng = getattr(self, "engine", None)
        return isinstance(eng, SpotLagEngine) and bool(eng.scfg.event_driven)

    def _sync_fast_feed(self) -> None:
        """Build, rebuild or drop the fast-feed detector to match the strategy config
        (at start, and after every dashboard control change)."""
        eng = getattr(self, "engine", None)
        sc = getattr(eng, "scfg", None) if isinstance(eng, SpotLagEngine) else None
        if sc is None or not bool(getattr(sc, "fast_feed", False)):
            if self.fast is not None:
                log.warning("FAST FEED  off")
            self.fast, self._fast_key = None, None
            return
        key = (str(sc.fast_horizons_ms), float(sc.k_sigma), float(sc.event_trigger_frac),
               float(sc.fast_spread_floor), float(sc.fast_dedupe_ms), float(sc.fast_history_gap_ms))
        if self.fast is not None and key == self._fast_key:
            return
        try:
            horizons = tuple(float(x) for x in str(sc.fast_horizons_ms).split(",") if x.strip()) or (50.0, 100.0, 250.0, 500.0)
        except ValueError:
            horizons = (50.0, 100.0, 250.0, 500.0)
        self.fast = FastMoveDetector(horizons_ms=horizons, k_sigma=sc.k_sigma, trigger_frac=sc.event_trigger_frac,
                                     spread_floor=sc.fast_spread_floor, dedupe_ms=sc.fast_dedupe_ms)
        self._fast_key = key
        gap = max(5.0, float(sc.fast_history_gap_ms))
        self.engine.features.spot_min_gap_ms = gap
        for h in self.engine.features.spot.values():
            h.min_gap_ms = gap
        log.warning("FAST FEED  horizons %s ms, k %.2f x %.2f, spread floor %.1fx, history every %.0f ms",
                    ",".join(f"{h:g}" for h in horizons), sc.k_sigma, sc.event_trigger_frac,
                    sc.fast_spread_floor, gap)

    def _control_targets(self) -> dict:
        """The strategy config and micro-live envelope the dashboard may steer.

        Only the spot-lag strategy's config is steerable: repriced_momentum's budget is
        a ceiling the shared control file must not lift (its engine says so). The
        envelope exists only in micro-live, so a paper bot neither shows nor applies it.
        """
        eng = getattr(self, "engine", None)
        steerable = isinstance(eng, SpotLagEngine) and not getattr(eng, "budget_is_ceiling", False)
        rp = getattr(eng, "rprm", None)
        return {"strategy": eng.scfg if steerable else None, "micro": getattr(self, "micro_caps", None),
                # the triangle's own settings (triangle.*): only a triangle bot has them; its budget stays fixed.
                # An engine with ``isolated_controls`` (pairlock) is a separate bot: the shared control file's
                # triangle.* values are meant for the triangle bot, not for it
                "triangle": eng.scfg if isinstance(eng, TriangleEngine) and not getattr(eng, "isolated_controls", False)
                else None,
                # pair-lock R's own settings: pairlock.* for the recovery bot, pairbtc.* for the conditions-rule bot
                # (tpb-pairlock-paper), so a value set for one never reaches the other through the shared file
                "pairlock": rp if rp is not None and rp.entry_mode != "conditions" else None,
                "pairbtc": rp if rp is not None and rp.entry_mode == "conditions" else None,
                # LEADER_PAIR's lp.* and RISING-RECOVERY's rtr.* (the user, 2026-10-13: both paper bots' parameters on
                # the Variables page); each config object is also what the engine's markets read, so a change applies
                # to the next decision
                "lp": eng.scfg if isinstance(eng, LeaderPairEngine) else None,
                "rtr": eng.scfg if isinstance(eng, RisingRecoveryEngine) else None,
                # Cheapside's own basket and individual-entry parameters are separate from the inherited
                # spot_lag.* controls, but point to the same live config object.
                "cheapside": eng.scfg if isinstance(eng, CheapSideEngine) else None,
                # CROSS_MARKET_ARB's cma.* knobs (paper-only guaranteed-profit portfolios)
                "cma": eng.scfg if isinstance(eng, CrossMarketArbEngine) else None,
                # CROSS_MARKET_CORRELATION's cmc.* knobs (paper-only; statistical mode off by default)
                "cmc": eng.scfg if isinstance(eng, CrossMarketCorrelationEngine) else None}

    # ───────────────────────────── discovery ─────────────────────────────

    async def discover(self) -> None:
        """Probe which assets are listed, then construct current + next slugs."""
        while not self._stop.is_set():
            try:
                now_s = self.clock.now() / 1000.0
                if self.registry.due(now_s):
                    found = await self.registry.refresh(self._fetch_market, now_s)
                    for a in found:
                        self._ensure_asset(a)
                epoch_now = window_epoch(now_s)
                added = 0
                for offset in (0, 1):
                    ep = window_epoch(now_s, offset)
                    for asset in self.registry.assets:
                        slug = f"{asset.lower()}-updown-5m-{ep}"
                        if slug in self.markets:
                            continue
                        row = self.registry.active.get(asset) if ep == epoch_now else None
                        if not row or row.get("slug") != slug:
                            try:
                                row = await self._fetch_market(slug)
                            except FetchFailed as exc:
                                self.discovery_errors += 1
                                self.last_discovery_error = str(exc)
                                continue
                        if not row:
                            continue
                        meta = parse_market(row)
                        if meta is None:
                            continue
                        self._ensure_asset(asset)
                        lm = LiveMarket(meta=meta)
                        for tid in (meta.market.yes_token_id, meta.market.no_token_id):
                            lm.books[tid] = LiveBook(tid)
                            self.token_index[tid] = (slug, lm)
                            self.fees.set_token_schedule(tid, meta.fee)
                            for venue in (self.exchange if self.is_live else None, self.dry_twin):
                                prewarm = getattr(venue, "prewarm", None)
                                if callable(prewarm):
                                    prewarm(tid)      # metadata fetched now, not between decision and POST
                        self.markets[slug] = lm
                        self.tracker.register(meta)
                        self.stats.windows_seen += 1
                        log.info("discovered %-26s closes %s  fee %.0f%%  liq %.0f",
                                 slug, time.strftime("%H:%M:%S", time.gmtime(meta.market.close_ts / 1000)),
                                 meta.fee.rate * 100, meta.liquidity)
                        if self._slog:
                            log.info("[MARKET] %s registered (%s)", self._mkt_label(meta.market), slug)
                        added += 1
                if added:
                    # ONE resubscribe per discovery pass. Setting it per market reconnected
                    # the feed up to seven times a window (1,477 connections in 46 h live),
                    # each a full-snapshot burst and a gap in the books.
                    self._resub.set()
                self._reap(self.clock.now())
            except Exception:
                log.exception("discovery failed")
            await asyncio.sleep(20)

    def _holds_window(self, slug: str, m) -> bool:
        """True while the window is on the risk book or either token still has shares."""
        if slug in self.risk.open:
            return True
        return any(abs(self.exchange.positions[t].shares) > 1e-9
                   for t in (m.yes_token_id, m.no_token_id) if t in self.exchange.positions)

    def _reap(self, now_ms: float) -> None:
        # the short grace is for windows we only watched; one still holding a position keeps the
        # full one, or it is reaped before its settlement can run
        for slug in [s for s, m in self.markets.items()
                     if now_ms > m.meta.market.close_ts + (30_000 if (m.meta.market.strike <= 0
                                                                      and not self._holds_window(s, m.meta.market))
                                                           else 240_000)]:
            lm = self.markets.pop(slug)
            # Belt and braces. Whatever the reason a window reaches its reaping
            # still holding risk -- settlement never ran, an exception ate it,
            # a path added later -- letting it go silently costs a position
            # slot permanently. Nothing may leave here still on the risk book.
            if slug in self.risk.open:
                self._release(slug, lm.meta.market, lm, "reaped unsettled")
            for tid in lm.books:
                self.token_index.pop(tid, None)
                self.fees.per_token.pop(tid, None)
            self._subscribed.discard(slug)
            self.engine.features.forget_market(slug)
            self.decisions.forget(slug)
            self.engine.last.pop(slug, None)
        # price_history was never pruned here. Each market's deque is capped,
        # but the number of MARKETS was not: 244 of them and 154k points had
        # piled up into a 41 MB state file. Writing that takes about a second,
        # which silently capped the dashboard at 1 Hz no matter what
        # state_loop's interval said. The closed window is not lost -- it is
        # already archived to data/charts/ at settlement.
        for slug in [s for s in self.price_history if s not in self.markets]:
            self.price_history.pop(slug, None)
            self.history.drop(slug)

    async def _fetch_market(self, slug: str) -> dict | None:
        """The market row, None if the venue says it does not exist, or
        ``FetchFailed`` if the venue could not be asked. The three answers are
        different: a DNS outage once delisted five assets because the second
        and third were conflated."""
        url = f"{GAMMA_BASE}/markets?slug={urllib.parse.quote(slug)}"
        rows, rtt = await self._get_json(url)
        if rtt is not None:
            # a Gamma GET, not an order: kept apart from the venue order latency
            self.latency.record_gamma(rtt)
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def _fetch_closed_market(self, slug: str) -> dict | None:
        """The market row of a CLOSED market (``closed=true``), or None."""
        rows, _ = await self._get_json(f"{GAMMA_BASE}/markets?slug={urllib.parse.quote(slug)}&closed=true")
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def _get_json(self, url: str) -> tuple[object, float | None]:
        def _do():
            req = urllib.request.Request(url, headers={"User-Agent": "troll-poly-bot/0.1"})
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=12) as r:
                body = json.load(r)
            return body, (time.perf_counter() - t0) * 1000.0
        try:
            body, rtt = await asyncio.to_thread(_do)
        except Exception as exc:
            log.debug("GET %s failed: %s", url, exc)
            self._feed_state("gamma", connected=False, error=f"{type(exc).__name__}: {exc}"[:120])
            raise FetchFailed(f"{type(exc).__name__}: {exc}") from exc
        self._feed_state("gamma", connected=True, message=True)
        return body, rtt

    # ─────────────────────────────── feeds ───────────────────────────────

    def _on_quote(self, q: SpotQuote) -> None:
        self.composite.update(q)
        self._note_spot_info(q)
        of = self.orderflow.get(q.asset)
        if of is not None:
            of.on_quote(q.exchange, q.bid_size, q.ask_size, q.ts_ms)
        now = self.clock.now()
        flow = getattr(self.engine, "flow", None)
        if flow is not None:
            # the cross-exchange flow signal (triangle signal_source=flow): scored on EVERY quote, and a
            # signal evaluates this asset's markets at once
            res = flow.on_quote(q.asset, q.exchange, q.bid, q.ask,
                                self._flow_info_time(q.exchange, q.ts_ms, q.event_ts_ms), now)
            if res is not None:
                self._flow_fired(q.asset, now)
                return
        if self.fast is not None:
            vol = self.vol.get(q.asset)
            sigma = self._shock_sigma(vol) if vol is not None and getattr(vol, "ready", False) else None
            mv = self.fast.on_quote(q.asset, q.exchange, q.bid, q.ask, now, sigma)
            if mv is not None:
                self.last_fast_move[q.asset] = mv
                self._maybe_shock_eval(q.asset, now, force=True)
                return
        if now - self._last_model_update.get(q.asset, 0.0) < self.cfg.feeds.model_update_interval_ms:
            if getattr(self, "_shock_driven", False):
                self._maybe_shock_eval(q.asset, now)
            return
        view = self.composite.view(q.asset, now)
        if view is None:
            return
        self._update_model(q.asset, view, now)
        if getattr(self, "_shock_driven", False):
            self._maybe_shock_eval(q.asset, now, updated=True)

    #: a venue stamp this far from our receipt is a clock or parse fault, not a delay
    SPOT_STAMP_MAX_SKEW_MS = 30_000.0

    def _note_spot_info(self, q: SpotQuote) -> None:
        """Measure this quote's delivery delay and keep the newest spot information per asset.

        The delay is our receipt minus the venue's own stamp (clamped at 0: venue clocks are
        not ours, a few ms either way). Without a stamp the source's measured trade lag
        dates the quote. A source excluded as lagging never counts as the newest."""
        if q.exchange in getattr(self.composite, "lagging", ()):
            return
        et = q.event_ts_ms
        if et is not None and abs(q.ts_ms - et) <= self.SPOT_STAMP_MAX_SKEW_MS:
            delay = max(0.0, q.ts_ms - et)
            info = q.ts_ms - delay
            self._quote_stamped.add(q.exchange)
            self.latency.record_spot_source(q.exchange, delay)
            # lead mode dates each source by its delay: for a stamped source, its QUOTE delay.
            # The trade-based figure was wrong for batched trade streams (Deribit's 100 ms
            # batches read ~200 ms against ~30 ms quotes, so the nearest source never led).
            lag = getattr(self.composite, "lag", None)
            if lag is not None:
                prev = lag.get(q.exchange)
                sample = min(delay, 2_000.0)
                lag[q.exchange] = sample if prev is None else prev + 0.02 * (sample - prev)
        else:
            info = q.ts_ms - getattr(self.composite, "lag", {}).get(q.exchange, 0.0)
        cur = self._spot_newest.get(q.asset)
        if cur is None or info >= cur[0]:
            self._spot_newest[q.asset] = cur = (info, q.ts_ms, q.exchange)
        # the age of the newest information, sampled as each quote lands (when an
        # event-driven decision would run): the dashboard's "spot age"
        self.latency.record_spot(max(0.0, q.ts_ms - cur[0]))

    def spot_age_ms(self, asset: str, now: float) -> float | None:
        """How old the spot price the model last used was at ``now``: now minus the exchange
        time of the newest information in it. With no measured quote behind the model (a price
        set directly), its receipt age -- never an assumed feed delay. None before any price."""
        used = self._spot_used.get(asset)
        if used is not None:
            return max(0.0, now - used[0])
        sts = self.spot_ts.get(asset)
        return None if sts is None else max(0.0, now - sts)

    def _update_model(self, asset: str, view, now: float) -> None:
        """Feed one composite price to the spot model: history, vol, TWAP, order-flow calibration."""
        self._ensure_asset(asset)
        self._last_model_update[asset] = now
        newest = self._spot_newest.get(asset)
        if newest is not None:
            self._spot_used[asset] = newest
        self.spot[asset] = view.price
        self.spot_ts[asset] = now
        self.vol[asset].update(view.price, now)
        self.twap[asset].update(view.price, now)
        self.engine.features.update_spot(asset, view.price, now)
        # order-flow calibration: score now, realised return 10 s / 30 s later
        cal = self.engine.ofi_calibration.get(asset)
        if cal is not None:
            lp = math.log(view.price)
            cal.resolve(now, lp)
            if now - self._last_calib_ms.get(asset, 0.0) >= 1000.0:
                self._last_calib_ms[asset] = now
                cal.record(self.orderflow[asset].features(now).get("ofi_score", math.nan), now, lp)

    def _note_shock_age(self, ev: Evaluation, asset: str, now: float) -> None:
        """How old the newest exchange trade was when the engine FIRST saw a
        shock (its SHOCK_UP/SHOCK_DOWN label), one per asset per 2 s: the
        decision's own reaction time, tick-driven or event-driven."""
        label = ((ev.features or {}).get("momentum") or {}).get("label", "")
        if not str(label).startswith("SHOCK"):
            return
        if now - self._last_shock_seen.get(asset, -math.inf) < 2_000.0:
            self._last_shock_seen[asset] = now
            return
        self._last_shock_seen[asset] = now
        used = self._spot_used.get(asset)
        t0 = used[0] if used is not None else self.spot_event_ts.get(asset)
        if t0:
            self.shock_decision_age.append(max(0.0, now - t0))

    def shock_age_report(self) -> dict:
        v = sorted(self.shock_decision_age)
        if not v:
            return {"n": 0}
        pick = lambda q: round(v[min(len(v) - 1, int(q * len(v)))], 1)  # noqa: E731
        return {"n": len(v), "p25": pick(0.25), "p50": pick(0.5), "p75": pick(0.75), "p90": pick(0.9)}

    def _shock_sigma(self, vol) -> float:
        """The sigma a 1-second spot move is judged against: the engine's own choice
        (1-second scale by default, strategy.spot_lag.SpotLagEngine.shock_sigma)."""
        fn = getattr(self.engine, "shock_sigma", None)
        return fn(vol) if callable(fn) else vol.sigma_per_sec

    def _fv_precheck(self, asset: str, price: float, hist, vol, now: float) -> bool:
        """The fair-value trigger's quote-level pre-check (``fv_trigger_cents``): would the last
        second's spot move have repriced one of this asset's tradeable markets by about that much?
        A normal approximation of the engine's pricer (the settlement average counts as 40 s less
        time); the engine's own fair value then decides."""
        sc = self.engine.scfg
        x_c = float(getattr(sc, "fv_trigger_cents", 0.0) or 0.0)
        if x_c <= 0.0:
            return False
        lp1 = hist.at(now - 1000.0)
        sp = vol.sigma_per_sec
        if lp1 is None or sp <= 0.0 or price <= 0.0:
            return False
        lp = math.log(price)
        r1 = lp - lp1
        need = sc.event_trigger_frac * x_c / 100.0
        for lm in self.markets.values():
            m = lm.meta.market
            if m.asset != asset or m.strike <= 0.0 or lm.settled:
                continue
            left = (m.close_ts - now) / 1000.0
            if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s):
                continue
            s = sp * math.sqrt(max(left - 40.0, 1.0))
            x = (lp - math.log(m.strike)) / s
            if abs(math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi) * r1 / s) >= need:
                return True
        return False

    def _maybe_shock_eval(self, asset: str, now: float, updated: bool = False, force: bool = False) -> None:
        """Spot-lag, event-driven: on a quote that shows a 1 s spot move near
        the shock threshold, bring the model up to this instant and evaluate
        this asset's markets now. Everything the 200 ms tick would check is
        checked; only the wait is gone. ``force`` (the fast feed's detection)
        skips the pre-check and goes straight to the engine's gates."""
        sc = self.engine.scfg
        if now - self._last_shock_eval.get(asset, 0.0) < sc.event_min_interval_ms:
            return
        hist = self.engine.features.spot.get(asset)
        vol = self.vol.get(asset)
        ssig = self._shock_sigma(vol) if vol is not None and getattr(vol, "ready", False) else 0.0
        if hist is None or ssig <= 0.0:
            return
        if force:
            self._last_shock_eval[asset] = now
            view = self.composite.view(asset, now)
            if view is None:
                return
            self._update_model(asset, view, now)
            self.shock_evals += 1
            self.fast_evals += 1
            try:
                self._evaluate_entries(now, assets={asset})
            except Exception:                             # noqa: BLE001
                log.exception("fast-feed evaluation failed")
            return
        # the composite price alone (this runs on every quote); a stand-in composite
        # that only has view() still works
        fast = getattr(self.composite, "price", None)
        if callable(fast):
            price = fast(asset, now)
        else:
            view = self.composite.view(asset, now)
            price = None if view is None else view.price
        if price is None or price <= 0.0:
            return
        win_s = min(1.0, max(0.05, float(getattr(sc, "shock_window_s", 1.0) or 1.0)))
        lp1 = hist.at(now - win_s * 1000.0)               # a LOG price, over the strategy's shock window
        if lp1 is None:
            return
        z1 = (math.log(price) - lp1) / (ssig * math.sqrt(win_s))
        if abs(z1) < sc.event_trigger_frac * sc.k_sigma and not self._fv_precheck(asset, price, hist, vol, now):
            return
        self._last_shock_eval[asset] = now
        if not updated:
            view = self.composite.view(asset, now)
            if view is None:
                return
            self._update_model(asset, view, now)        # the engine reads THIS price, not one 100 ms old
        self.shock_evals += 1
        try:
            self._evaluate_entries(now, assets={asset})
        except Exception:                                 # noqa: BLE001
            log.exception("shock evaluation failed")

    #: an exchange whose own trade timestamps show it this far behind leaves
    #: the composite until it is back under the recovery level (hysteresis)
    SOURCE_LAG_EXCLUDE_MS = 750.0
    SOURCE_LAG_RECOVER_MS = 400.0
    #: no trade from a flagged exchange for this long: nothing says it is
    #: still behind, so it may rejoin (its quotes still age out at 3 s)
    SOURCE_LAG_FORGET_MS = 10_000.0

    def _on_source_lag(self, source: str, ms: float) -> None:
        """Per-exchange feed lag from trade timestamps. On a backlogged
        connection (seen 2026-09-24: Binance 9.8 s behind, Bybit and Coinbase
        0.1 s) the quotes are just as old but carry no timestamp, so they
        would enter the composite as fresh; this keeps them out."""
        if source not in self._quote_stamped:
            self.latency.record_spot_source(source, ms)      # stamped sources are measured per quote
        self._source_lag_seen[source] = self.clock.now()
        # the composite's lead mode needs each source's typical delay: a slow EWMA of the
        # trade-timestamp lag, samples capped so one backlogged burst cannot swing it
        lag = self.composite.lag
        if source not in self._quote_stamped:                # stamped sources: _note_spot_info
            prev = lag.get(source)
            sample = min(ms, 2_000.0)
            lag[source] = sample if prev is None else prev + 0.02 * (sample - prev)
        lagging = getattr(self.composite, "lagging", None)
        if lagging is None:
            return
        if ms > self.SOURCE_LAG_EXCLUDE_MS and source not in lagging:
            lagging.add(source)
            log.warning("FEED %s is %.0f ms behind: excluded from the composite spot", source, ms)
        elif ms < self.SOURCE_LAG_RECOVER_MS and source in lagging:
            lagging.discard(source)
            log.info("FEED %s caught up (%.0f ms): back in the composite spot", source, ms)

    def _forget_stale_lag_flags(self, now: float) -> None:
        lagging = getattr(self.composite, "lagging", None)
        for source in list(lagging or ()):
            if now - self._source_lag_seen.get(source, 0.0) > self.SOURCE_LAG_FORGET_MS:
                lagging.discard(source)

    def _flow_info_time(self, exchange: str, ts_ms: float, event_ts_ms: float | None) -> float:
        """When a tick's information existed: the exchange's own stamp, else receipt minus the source's delay."""
        if event_ts_ms and abs(ts_ms - event_ts_ms) <= self.SPOT_STAMP_MAX_SKEW_MS:
            return event_ts_ms
        return ts_ms - getattr(self.composite, "lag", {}).get(exchange, 0.0)

    def _flow_fired(self, asset: str, now: float) -> None:
        self.flow_signals = getattr(self, "flow_signals", 0) + 1
        self._maybe_shock_eval(asset, now, force=True)

    def _on_trade(self, t: TradeTick) -> None:
        flow = getattr(self.engine, "flow", None)
        if flow is not None:
            now = self.clock.now()
            res = flow.on_trade(t.asset, t.exchange, t.notional, t.aggressor_buy,
                                self._flow_info_time(t.exchange, t.ts_ms, t.event_ts_ms), now)
            if res is not None:
                self._flow_fired(t.asset, now)
        of = self.orderflow.get(t.asset)
        if of is not None:
            of.on_trade(t.notional, t.aggressor_buy, t.ts_ms)
        if self.fast is not None:
            self.fast.on_trade(t.asset, t.ts_ms)
        # T0: the newest exchange-side event time of a spot trade (exchange clock,
        # which ClockSync aligns ours to); quotes carry no event time
        if t.event_ts_ms and t.event_ts_ms > self.spot_event_ts.get(t.asset, 0.0):
            self.spot_event_ts[t.asset] = t.event_ts_ms

    def _of_point(self, asset: str, now: float) -> dict | None:
        of = self.orderflow.get(asset)
        if of is None:
            return None
        f = of.features(now)
        r3 = lambda v: None if v != v else round(v, 3)  # noqa: E731
        return {"tfi15": r3(f["tfi_15s"]), "obi": r3(f["obi"]), "ofi": r3(f["ofi_score"]),
                "rate": round(f["trade_rate_30s"], 2)}

    async def spot_feed(self) -> None:
        """Run the exchange feeds for the listed assets; restart when the set changes."""
        while not self._stop.is_set():
            assets = self.registry.assets
            if not assets:
                await asyncio.sleep(1)
                continue
            local_stop = asyncio.Event()
            task = asyncio.create_task(run_all(
                assets, self._on_quote, self.clock.now, local_stop, self.exchanges,
                on_latency=None, on_trade=self._on_trade,          # spot age: _note_spot_info
                health=self.feed_health, on_source_latency=self._on_source_lag))
            self._spot_task, self._spot_stop = task, local_stop
            try:
                while not self._stop.is_set() and self.registry.assets == assets:
                    await asyncio.sleep(1)
            finally:
                # reached on a normal stop AND on cancellation: the adapters must
                # never be orphaned, or the process cannot exit
                local_stop.set()
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(task), timeout=20.0)

    #: a resubscribe waits this long first, so one discovery pass becomes one connection
    RESUB_DEBOUNCE_S = 0.5
    #: how long the new connection may take to deliver its snapshots before the old
    #: one is retired anyway
    HANDOVER_TIMEOUT_S = 6.0

    def _book_tokens(self) -> list[str]:
        skip = self._book_skip_assets
        if not skip:
            return list(self.token_index)
        return [t for t, (_slug, lm) in self.token_index.items() if lm.meta.market.asset.upper() not in skip]

    async def polymarket_feed(self) -> None:
        """Supervises the book connection(s).

        A resubscribe (new markets discovered) opens the NEXT connection first and
        keeps reading the current one until the new one has delivered its snapshots
        (or HANDOVER_TIMEOUT_S), then retires the old one. The strategy never sees a
        gap, and the snapshot burst lands on a connection nobody depends on yet. A
        dropped connection is replaced the same way, with the usual backoff.
        """
        backoff = RECONNECT_MIN_S
        current: tuple[int, asyncio.Task] | None = None
        while not self._stop.is_set():
            alive = current is not None and not current[1].done()
            if alive and not self._resub.is_set():
                await asyncio.sleep(0.25)
                continue
            if self._resub.is_set():
                await asyncio.sleep(self.RESUB_DEBOUNCE_S)
                self._resub.clear()
            tokens = self._book_tokens()
            if not tokens:
                await asyncio.sleep(1)
                continue
            conn_id = self._book_conn + 1
            ready = asyncio.Event()
            task = asyncio.create_task(self._book_reader(conn_id, tokens, ready))
            if alive:
                self._book_conns_ok.add(conn_id)          # both current during the handover
            else:
                self._book_conn = conn_id
                self._book_conns_ok = {conn_id}
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ready.wait(), timeout=self.HANDOVER_TIMEOUT_S)
            if task.done():                               # it never connected (or died at once)
                if alive:
                    self._book_conns_ok.discard(conn_id)
                    self._resub.set()                     # try the handover again after the backoff
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, RECONNECT_MAX_S)
                continue
            backoff = RECONNECT_MIN_S
            if alive:
                old_id, old_task = current
                self._book_conn = conn_id
                self._book_conns_ok = {conn_id}
                old_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await old_task
                log.info("book feed handed over: conn %d -> %d (%d tokens)", old_id, conn_id, len(tokens))
            current = (conn_id, task)
        if current is not None and not current[1].done():
            current[1].cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await current[1]

    async def _book_reader(self, conn_id: int, tokens: list[str], ready: asyncio.Event) -> None:
        """One websocket: subscribe, then feed every frame to the handler. ``ready`` is
        set once a snapshot has arrived for every subscribed token (or on exit)."""
        want, got = set(tokens), set()
        started = time.monotonic()
        try:
            # max_queue: frames the library holds before it stops reading the socket. Past
            # that the venue's own send buffer fills and it closes us as a "slow consumer"
            # (1013) -- seen at every resubscribe, when 36 snapshots land at once and the
            # OLD connection, still the one the guard watches, is the one cut. 4096 frames
            # is a few MB and a few seconds of the busiest burst on our side instead.
            async with websockets.connect(CLOB_WS, open_timeout=20, ping_interval=10,
                                          max_size=8 * 1024 * 1024, max_queue=4096) as ws:
                await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                log.info("polymarket book feed connected (%d tokens, conn %d)", len(tokens), conn_id)
                self._feed_state("polymarket", connected=True)
                keepalive = asyncio.create_task(self._ping(ws))
                try:
                    # `async for` pulls frames straight off the library's queue (a task per
                    # frame was itself the slow consumer, see git history)
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        self._handle_book_msg(raw, conn_id, got)
                        if not ready.is_set() and want <= got:
                            ready.set()
                finally:
                    keepalive.cancel()
        except asyncio.CancelledError:
            raise                                          # retired by the supervisor: not a drop
        except Exception as exc:
            if self._stop.is_set():
                return
            st = self._feed_state("polymarket", error=f"{type(exc).__name__}: {exc}"[:120])
            st.reconnects += 1
            if conn_id == self._book_conn and not (self._book_conns_ok - {conn_id}):
                # the feed is down only when no successor is already delivering: a
                # drop during a handover (the venue cutting the old connection while
                # the new one's snapshots land) used to trip the guard's FEED_DOWN and
                # its 30 s cooldown for nothing, about 35 s of no entries per window
                self._feed_state("polymarket", connected=False)
            (log.warning if st.reconnects == 1 or st.reconnects % 10 == 0 else log.debug)(
                "book feed dropped (%s) after %.0fs (conn %d, drop %d)", type(exc).__name__,
                time.monotonic() - started, conn_id, st.reconnects)
        finally:
            ready.set()

    @staticmethod
    async def _ping(ws) -> None:
        with contextlib.suppress(Exception):
            while True:
                await asyncio.sleep(8)
                await ws.send("PING")

    def _handle_book_msg(self, raw: str | bytes, conn: int | None = None, got: set | None = None) -> None:
        if not raw or raw[0] not in ("[", "{", 91, 123):
            return
        try:
            msgs = orjson.loads(raw)                      # ~8x json.loads on this payload
        except orjson.JSONDecodeError:
            return
        if isinstance(msgs, dict):
            msgs = [msgs]
        now_ms = self.clock.now()
        self._feed_state("polymarket", message=True)
        if conn is None:
            conn = self._book_conn
        for m in msgs:
            ev = m.get("event_type")
            if ev == "book":
                ts = _ts(m.get("timestamp"))
                tid = str(m.get("asset_id"))
                entry = self.token_index.get(tid)
                if entry:
                    entry[1].books[tid].apply_snapshot(m, recv_ts=now_ms, conn=conn)
                    self.latency.record_book(max(0.0, now_ms - ts))
                if got is not None:
                    got.add(tid)
                self._advance_book_clock(ts, now_ms)
            elif ev == "price_change":
                ts = _ts(m.get("timestamp"))
                self.latency.record_book(max(0.0, now_ms - ts))
                for ch in m.get("price_changes") or []:
                    entry = self.token_index.get(str(ch.get("asset_id")))
                    if entry:
                        entry[1].books[str(ch["asset_id"])].apply_change(ch, ts, recv_ts=now_ms)
                self._advance_book_clock(ts, now_ms)
            elif m.get("timestamp") is not None:
                # tick_size_change, last_trade_price ...: still proof of how far the feed has got
                self._advance_book_clock(_ts(m.get("timestamp")), now_ms)
        self._mark_due(now_ms)

    def _advance_book_clock(self, venue_ts: float, recv_ts: float) -> None:
        # the venue's clock only moves forward on a healthy feed; a timestamp
        # in the future (bad parse) must not make every book look fresh
        if venue_ts > self._book_last_venue_ts and venue_ts <= recv_ts + 5_000.0:
            self._book_last_venue_ts = venue_ts
            self._book_last_recv_ts = recv_ts

    def quote_age_ms(self, token_id: str, now_ms: float) -> float | None:
        """How old our view of this token's book is, ms, or None if we have no
        valid book for it (no snapshot on the current feed connection).

        The venue pushes every change on one ordered stream, so a book that
        has not changed is still current as of the newest message the feed
        has delivered; its age is ``now - that message's venue timestamp``.
        That is the network lag plus any backlog in our reader -- exactly the
        staleness a 1013 "slow consumer" episode produces -- and it is NOT
        auto-widened by measured latency, unlike the engine's book-age gate.
        """
        entry = self.token_index.get(token_id)
        lb = entry[1].books.get(token_id) if entry else None
        if lb is None or lb.ts <= 0.0:
            return None
        ok = self._book_conns_ok or {self._book_conn}
        if lb.conn and lb.conn not in ok:
            return None                    # snapshot from a dead connection
        as_of = max(lb.ts, self._book_last_venue_ts if lb.conn in ok else 0.0)
        return max(0.0, now_ms - as_of)

    # ───────────────────────────── strategy ──────────────────────────────

    async def trade_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("trade tick failed")
            await asyncio.sleep(0.2)

    def _count_exec(self, code: str) -> None:
        self.exec_rejections[code] = self.exec_rejections.get(code, 0) + 1

    def _tick(self) -> None:
        now = self.clock.now()
        self._mark_due(now)
        self._forget_stale_lag_flags(now)
        # Drawdown = balance_24h_ago − current marked equity/cash.
        try:
            marked = float(self.exchange.equity(self._mark))
        except Exception:
            marked = float(getattr(self.exchange, "balance", 0.0) or 0.0)
        now_s = now / 1000.0 if now > 1e12 else float(now)
        self.risk.note_balance(marked, now_s=now_s)
        self.risk.halted(now_s=now_s)

        # The venue's strike is the trailing 60s TWAP at the window open. Only
        # offer one once the full window is backed by observations.
        for asset in list(self.twap):
            st = self.twap[asset]
            if not st.fully_covered(now):
                continue
            tw = st.trailing_twap(now)
            self._twap60[asset] = tw
            if tw is not None:
                self.tracker.on_price(asset, tw, now)
        for meta in self.tracker.poll(now):
            log.info("%s struck at %.6g (60s TWAP at open)", meta.market.slug, meta.market.strike)

        self._sync_latency_profile()

        for res in self.exchange.step(now, self._true_book, self._close_ts):
            self._on_result(res, now)
        if self.shadow is not None:
            for res in self.shadow.step(now, self._true_book, self._close_ts):
                self._on_shadow_result(res, now)
        if self.dry_twin is not None:
            for res in self.dry_twin.step(now):
                self._on_dry_result(res, now)

        self._sample_prices(now)
        self._take_profit(now)
        if self._pairs_to_place:
            self._place_restored_pairs(now)

        self._evaluate_entries(now)

    def _evaluate_entries(self, now: float, assets: set[str] | None = None) -> None:
        """Entry decisions for every live market (the 200 ms tick), or only the
        markets of ``assets`` (a spot shock, evaluated the moment it arrives:
        _maybe_shock_eval). The same gates, revalidation, kill switch and
        pre-send checklist either way."""
        # the guard decides whether ANY new entry may go out this tick;
        # exits above are unaffected by it
        if now - self._last_guard_ms >= GUARD_INTERVAL_MS:
            self._last_guard_ms = now
            self._update_guard(now)
        entries_allowed = self.guard.entries_allowed
        halted = self.risk.halted()
        risk_status = (f"halted: {halted.detail}" if not halted.ok else
                       f"guard: {','.join(self.guard.tripped)}" if self.guard.tripped else "ok")
        mode = self.cfg.deploy_mode

        for slug, lm in list(self.markets.items()):
            meta = lm.meta
            m = meta.market
            if assets is not None and m.asset not in assets:
                continue
            # `exited` markets are done for the window: re-entering the book we
            # just de-risked into would pay the spread twice to undo the exit
            if ((m.strike <= 0 and getattr(self.engine, "requires_strike", True))
                    or (now < m.open_ts and getattr(self.engine, "requires_market_open", False)) or lm.settled
                    or (lm.exited and not self._reentry) or now >= m.close_ts):
                continue
            if lm.inflight > 0:
                self._count_exec("ORDER_IN_FLIGHT")
                continue
            if self._entry_throttled(lm, now):
                continue
            asset = m.asset
            sts = self.spot_ts.get(asset)
            view = self.composite.view(asset, now)
            spot_age = self.spot_age_ms(asset, now)

            up = lm.books[m.yes_token_id].to_order_book()
            down = lm.books[m.no_token_id].to_order_book()
            book_age = max(0.0, now - max(up.ts, down.ts)) if (up.ts or down.ts) else None
            quote_ages = {m.yes_token_id: self.quote_age_ms(m.yes_token_id, now),
                          m.no_token_id: self.quote_age_ms(m.no_token_id, now)}

            ev = self.engine.evaluate(
                meta=meta, book_up=up, book_down=down, spot=view, spot_age_ms=spot_age,
                book_age_ms=book_age, now=now, vol=self.vol.get(asset), twap=self.twap[asset],
                latency=self.strategy_latency, balance=max(self.exchange.balance, 0.0),
                orderflow=self.orderflow[asset].features(now) if asset in self.orderflow else None,
                quote_ages=quote_ages,
            )
            if ev.p_analytic is not None and ev.p_market is not None:
                self.agreement.setdefault(asset, Agreement()).add(ev.p_analytic, ev.p_market, ev.seconds_left)
            self._note_shock_age(ev, asset, now)
            ctx = {"spot_age": spot_age, "quote_ages": quote_ages}
            if not ev.tradeable:
                self.decisions.record(ev, now_ms=now, mode=mode, risk_status=risk_status)
                self._note_signal(ev, lm, now, "SKIP", ev.reason.value if ev.reason else "", ctx)
                self._skip_log(ev, lm, ev.reason, ev.detail)
                continue
            if not entries_allowed:
                for code in self.guard.tripped:
                    self._count_exec(f"GUARD_{code}")
                self.decisions.record(ev, now_ms=now, mode=mode, risk_status=risk_status, override="GUARD",
                                      detail=",".join(self.guard.tripped))
                self._note_signal(ev, lm, now, "SKIP", "GUARD", ctx)
                self._skip_log(ev, lm, None, "", f"entries paused by the guard ({','.join(self.guard.tripped)})")
                self._engine_refused(ev, f"entries paused by the guard ({','.join(self.guard.tripped)})")
                continue

            # Basket / multi-market engines may return an order for a different
            # slug than the market currently being scanned. Bind books, risk and
            # forensics to the Evaluation's market so we never KeyError looking
            # up another market's token in this one's book (and so the leader
            # recovery can fire on the same tick the six cheap legs finish).
            order_lm = lm
            if ev.slug and ev.slug != slug:
                other = self.markets.get(ev.slug)
                if other is None or ev.token_id not in other.books:
                    self._count_exec("CROSS_MARKET_ORDER")
                    self._engine_refused(ev, f"evaluation for {ev.slug} while scanning {slug}")
                    continue
                if other.inflight > 0:
                    self._count_exec("ORDER_IN_FLIGHT")
                    continue
                if self._entry_throttled(other, now):
                    continue
                order_lm = other
                meta = order_lm.meta
                m = meta.market
                slug = m.slug
                asset = m.asset
                quote_ages = {m.yes_token_id: self.quote_age_ms(m.yes_token_id, now),
                              m.no_token_id: self.quote_age_ms(m.no_token_id, now)}
                sts = self.spot_ts.get(asset)
                spot_age = self.spot_age_ms(asset, now)
                ctx = {"spot_age": spot_age, "quote_ages": quote_ages}
            # Revalidate against the freshest book right before sending; the
            # book can move between evaluation and submission. Never chase.
            if ev.token_id not in order_lm.books:
                self._count_exec("NO_TOKEN_BOOK")
                self._engine_refused(ev, f"no book for {ev.token_id} on {slug}")
                continue
            fresh = order_lm.books[ev.token_id].to_order_book()
            why = self.engine.revalidate(ev, fresh)
            if why is not None:
                self._count_exec(f"REVALIDATE_{why.value}")
                self.decisions.record(ev, now_ms=now, mode=mode, risk_status=risk_status,
                                      override="REVALIDATION", detail=why.value)
                self._note_signal(ev, order_lm, now, "SKIP", f"REVALIDATION_{why.value}", ctx)
                self._skip_log(ev, order_lm, None, "", f"the freshest book no longer qualifies ({why.value})")
                self._engine_refused(ev, f"the freshest book no longer qualifies ({why.value})")
                continue
            # the emergency stop, in every mode, immediately before the order exists
            kill = self._kill_state()
            if kill is not None:
                self._count_exec("KILL_SWITCH")
                self.decisions.record(ev, now_ms=now, mode=mode, risk_status=risk_status,
                                      override="KILL_SWITCH", detail=f"{self.kill_path} ({kill})")
                self._note_signal(ev, order_lm, now, "SKIP", "KILL_SWITCH", ctx)
                self._skip_log(ev, order_lm, None, "", f"kill switch {self.kill_path} ({kill})")
                self._engine_refused(ev, f"kill switch {self.kill_path} ({kill})")
                continue
            # the last entry confirmation: every critical condition, re-read now
            failed, checklist = self._confirm_entry(ev, order_lm, now, spot_age, quote_ages.get(ev.token_id))
            if failed is not None:
                self._count_exec(f"CONFIRM_{failed}")
                self.decisions.record(ev, now_ms=now, mode=mode, risk_status=risk_status,
                                      override="CONFIRMATION", detail=f"{failed}: {checklist.get(failed)}")
                self._note_signal(ev, order_lm, now, "SKIP", f"CONFIRMATION_{failed}", ctx)
                self._skip_log(ev, order_lm, None, "", f"pre-send check {failed} failed ({checklist.get(failed)})")
                self._engine_refused(ev, f"pre-send check {failed} failed ({checklist.get(failed)})")
                continue
            self.decisions.record(ev, now_ms=now, mode=mode, risk_status=risk_status)

            order = self.engine.to_order(ev, now)
            # the analytic engine lists both sides; the momentum engine only the one
            # it wants, so pick by name (indexing [1] crashed every DOWN signal)
            side_view = next((s for s in ev.sides if s.side == ev.side), None)
            if side_view is None:
                self._count_exec("NO_SIDE_VIEW")
                self._engine_refused(ev, "no side view")
                continue
            order.signal_id = self._note_signal(ev, order_lm, now, "BUY", "", ctx) or ""
            self.fair_by_order[order.order_id] = {
                "slug": slug, "asset": asset, "epoch": order_lm.epoch, "token_id": ev.token_id,
                "side": ev.side, "p_used": ev.p_used, "p_analytic": ev.p_analytic,
                "p_market": side_view.p_market,
                "net_edge": ev.net_edge, "confidence": ev.confidence,
                "regime": ev.regime.label if ev.regime else "",
                "secs_left": ev.seconds_left, "expected": ev.expected_price,
                "fee_rate": meta.fee.rate,
                "ofi": None if math.isnan(ev.ofi_score) else round(ev.ofi_score, 3),
                "ofi_drift_bps": round(ev.ofi_drift_bps, 3),
                "signal_id": order.signal_id,
            }
            # forensics: the decision, the risk check it passed and the send
            spread = (side_view.ask - side_view.bid) if (side_view.ask is not None and side_view.bid is not None) else None
            book = fresh
            mom = (ev.features or {}).get("momentum") or {}
            self.forensics.open(
                order.order_id, kind="entry", slug=slug, asset=asset, epoch=order_lm.epoch, token_id=ev.token_id,
                market_id=m.slug, condition_id=m.condition_id, side=ev.side, mode=self.mode_label,
                signal_probability=round(side_view.p_model, 4), market_probability=_r4(side_view.p_market),
                edge=round(ev.net_edge, 4), spread=_r4(spread),
                estimated_slippage=side_view.costs.slippage if side_view.costs else None,
                decision_price=ev.expected_price, quantity=round(ev.size, 4), seconds_left=round(ev.seconds_left, 1),
                spot_move_bps=_r4(ev.spot_move_bps), market_reprice=_r4(ev.market_reprice),
                reason_for_entry=f"net edge {ev.net_edge:+.3f} conf {ev.confidence:.2f} {ev.regime.label if ev.regime else ''}",
                strategy=self.cfg.engine.strategy, strategy_version=STRATEGY_VERSION, signal_id=order.signal_id, limit_price=order.price,
                best_ask=_r4(side_view.ask), best_bid=_r4(side_view.bid), max_entry_price=_r4(ev.max_entry_price),
                quote_age_ms=_r4(quote_ages.get(ev.token_id)), spot_age_ms=_r4(spot_age),
                momentum_label=mom.get("label"), momentum_z=(mom.get("z") or {}).get("main"),
                momentum_score=mom.get("score"), preflight=checklist,
                # the timeline, T0..T4 (T5.. come back with the result)
                # T0 = the exchange's time on the newest price the model used, T1 = when it reached us
                t0_spot_event=(self._spot_used.get(asset) or (self.spot_event_ts.get(asset),))[0],
                t1_spot_recv=(self._spot_used.get(asset) or (None, sts))[1],
                spot_source=(self._spot_used.get(asset) or (None, None, None))[2], t2_signal=now,
                t3_book_venue=book.ts or None, t3_book_recv=book.recv_ts or None, t4_decision=now)
            self.forensics.stage(order.order_id, "MARKET_DATA", now - (spot_age or 0.0))
            self.forensics.stage(order.order_id, "SIGNAL", now)
            self.forensics.stage(order.order_id, "DECISION", now)
            self.forensics.stage(order.order_id, "RISK_CHECK", now, size_note=ev.size_note)
            self.decisions.order_event("ORDER_CREATED", order.order_id, now, market=slug, side=ev.side,
                                       price=order.price, size=round(order.size, 2), tif=order.tif.value,
                                       signal_id=order.signal_id)
            signal_text = getattr(self.engine, "signal_text", None)
            if self._slog and callable(signal_text):
                log.info("[SIGNAL] %s %s %s", self._mkt_label(m), ev.side, signal_text(ev))
            elif self._slog:
                sl = (ev.features or {}).get("spot_lag", {})
                log.info("[SIGNAL] %s %s valid spot-lag opportunity: z1 %+.2f, expected %.3f vs ask %.2f "
                         "(lag %+.3f), net edge %+.3f", self._mkt_label(m), ev.side, sl.get("z1", 0.0),
                         sl.get("ev", 0.0), sl.get("ask", 0.0), sl.get("lag", 0.0), ev.net_edge)
            self.exchange.submit(order, now)
            # the order's WORST case (limit x size) counts against the global exposure
            # budget until it resolves, so markets signalling together cannot overfill it
            self.risk.on_submit(slug, usdc=order.price * order.size, asset=asset, epoch=order_lm.epoch)
            on_submit = getattr(self.engine, "on_entry_submit", None)
            if callable(on_submit):
                on_submit(slug, now)
            self._shadow_submit(order, now)
            self._dry_submit(order, now)
            if self._slog:
                log.info("[ORDER] %s %s buy submitted: %.2f sh %s at <= %.2f (<= %.2f USDC) | %s",
                         self._mkt_label(m), ev.side, order.size, _order_type_label(order), order.price,
                         order.price * order.size, self._portfolio_line())
            self.forensics.stage(order.order_id, "ORDER_SUBMITTED", now, limit_price=order.price)
            self.decisions.order_event("ORDER_SUBMITTED", order.order_id, now, market=slug)
            order_lm.submitted += 1
            order_lm.inflight += 1
            order_lm.last_entry_ms = now
            log.info("SEND  %-26s %-4s %6.2f sh @ %.2f  p %.3f (mkt %.3f)  net edge %+.3f  conf %.2f  "
                     "%s  ofi %+.2f (%+.1f bps)  %5.1fs left",
                     slug, ev.side, ev.size, ev.limit_price, ev.p_used if ev.side == "UP" else 1 - ev.p_used,
                     self.fair_by_order[order.order_id]["p_market"], ev.net_edge, ev.confidence,
                     ev.regime.label if ev.regime else "",
                     0.0 if math.isnan(ev.ofi_score) else ev.ofi_score, ev.ofi_drift_bps, ev.seconds_left)

    def _confirm_entry(self, ev: Evaluation, lm: LiveMarket, now: float, spot_age: float | None,
                       quote_age: float | None) -> tuple[str | None, dict]:
        """Phase 14: immediately before an entry exists, every critical
        condition is re-read and written down. Returns (first failure, checklist).

        Most of these were true a few lines up; they are re-checked because
        the order goes out on the strength of THIS list, and the list is what
        the decision record keeps. ``book``/``ask``/``depth``/``net edge`` were
        re-checked by ``revalidate`` on the freshest book just before this.
        """
        m = lm.meta.market
        eng = self.cfg.engine
        mom = getattr(self.engine, "mcfg", None)
        cheapside_cfg = getattr(self.engine, "scfg", None)
        cheapside_info = (ev.features or {}).get("cheapside", {})
        cmc_info = (ev.features or {}).get("cross_market_correlation", {})
        basket_entry = bool(cheapside_info.get("basket_entry")
                            or cheapside_info.get("basket_low_buy") or cheapside_info.get("basket_leader_buy"))
        # Cross-market correlation legs are gated on books + pair EV, not spot shocks.
        book_priced_entry = basket_entry or bool(cmc_info.get("cmc_leg"))
        max_spot = min(eng.max_spot_age_ms, getattr(mom, "max_spot_age_ms", eng.max_spot_age_ms))
        max_quote = getattr(mom, "max_quote_age_ms", None) or eng.max_book_age_ms
        notional = ev.size * ev.limit_price
        halted = self.risk.halted()
        # an engine that buys a market in clips (fastclip, btc_hedge, triangle) adds to its own position
        # there: without add_to_open the second buy in a market failed RISK_LIMITS (ALREADY_POSITIONED)
        verdict = self.risk.check(m.slug, m.asset, lm.epoch, ev.size * ev.expected_price,
                                  add_to_open=bool(getattr(self.engine, "stacks_positions", False)))
        min_time_remaining = (cheapside_cfg.portfolio_min_time_remaining_s
                              if basket_entry and isinstance(self.engine, CheapSideEngine)
                              else (getattr(mom, "min_time_remaining_s", 0.0) if mom
                                    else eng.trade_window_end_s))
        checks = {
            # The seven-market basket is priced and gated entirely from the two
            # outcome books. Requiring a crypto spot tick here silently vetoed
            # otherwise valid cheap and leader orders.
            "SPOT_FRESH": (book_priced_entry or (spot_age is not None and spot_age <= max_spot),
                           None if spot_age is None else round(spot_age)),
            "BOOK_FRESH": (quote_age is not None and quote_age <= max_quote,
                           None if quote_age is None else round(quote_age)),
            "TIME_REMAINING": ((m.close_ts - now) / 1000.0 >= max(
                min_time_remaining,
                (self.strategy_latency.round_trip_ms() + eng.latency_safety_margin_ms) / 1000.0),
                round((m.close_ts - now) / 1000.0, 1)),
            "BALANCE": (self.exchange.balance >= notional, round(self.exchange.balance, 2)),
            "NO_OPEN_ORDER": (lm.inflight == 0 and m.slug not in self.risk.pending, lm.inflight),
            # an engine that buys a market in clips (fastclip) adds to its own position there
            "NO_POSITION": (m.slug not in self.risk.open or bool(getattr(self.engine, "stacks_positions", False)),
                            len(self.risk.open)),
            "RISK_LIMITS": (halted.ok and verdict.ok, "ok" if halted.ok and verdict.ok else (halted.detail or verdict.detail)),
            "GUARD": (self.guard.entries_allowed, ",".join(self.guard.tripped) or "ok"),
            "KILL_SWITCH": (self._kill_state() is None, str(self.kill_path)),
            "DIRECTION": (ev.side in ("UP", "DOWN") and ev.token_id in (m.yes_token_id, m.no_token_id), ev.side),
        }
        checklist = {k: v[1] for k, v in checks.items()}
        for name, (ok, _) in checks.items():
            if not ok:
                return name, checklist
        return None, checklist

    def _note_signal(self, ev: Evaluation, lm: LiveMarket, now: float, decision: str, reason: str,
                     ctx: dict) -> str | None:
        """Follow a priced signal to its fate (execution/outcomes.py).

        Only evaluations that priced a side are signals; "no momentum" is
        not an opportunity. Returns the signal id when one was recorded.
        """
        if not ev.sides:
            return None
        sv = next((x for x in ev.sides if x.side == ev.side), None) if ev.side else None
        if sv is None:
            priced = [x for x in ev.sides if x.costs is not None]
            sv = max(priced, key=lambda x: x.costs.net_edge) if priced else ev.sides[0]
        if sv.ask is None:
            return None
        traded = decision == "BUY"
        if not self.outcomes.should_record(ev.slug, reason, now, traded):
            return None
        sid = f"{ev.slug}:{sv.side}:{int(now)}"
        mom = (ev.features or {}).get("momentum") or {}
        m = lm.meta.market
        self.outcomes.on_signal(
            signal_id=sid, slug=ev.slug, token_id=sv.token_id, side=sv.side, decision=decision, reason=reason,
            now_ms=now, ask=sv.ask, cap=ev.limit_price if traded else (ev.max_entry_price or sv.ask),
            size=ev.size if traded else max(m.min_size, self.cfg.risk.micro_min_shares),
            arrival_ms=self.strategy_latency.submit_base,
            asset=ev.asset, bid=sv.bid, vwap=_r4(sv.entry_px), fair=_r4(sv.p_model), mid=_r4(sv.p_market),
            net_edge=_r4(sv.costs.net_edge) if sv.costs else None, depth=_r4(sv.depth_at_limit),
            max_entry_price=_r4(ev.max_entry_price), secs_left=round(ev.seconds_left, 1),
            quote_age_ms=_r4((ctx.get("quote_ages") or {}).get(sv.token_id)), spot_age_ms=_r4(ctx.get("spot_age")),
            momentum_label=mom.get("label"), momentum_z=(mom.get("z") or {}).get("main"),
            momentum_score=mom.get("score"), spot_move_bps=_r4(ev.spot_move_bps),
            market_reprice=_r4(ev.market_reprice), mode=self.mode_label, strategy=self.cfg.engine.strategy)
        return sid

    # ───────────────────────── shadow paper engine (live) ─────────────────────

    def _shadow_submit(self, order: Order, now: float) -> None:
        """Send a copy of a live order to the shadow paper engine."""
        if self.shadow is None:
            return
        if order.side is Side.SELL:
            pos = self.shadow.positions.get(order.token_id)
            if pos is None or pos.shares <= EXIT_EPS:
                self.forensics.open(order.order_id, paper_outcome="NO_PAPER_POSITION")
                return
        twin = Order(token_id=order.token_id, side=order.side, price=order.price, size=order.size,
                     tif=order.tif, expected_price=order.expected_price, fair_at_decision=order.fair_at_decision,
                     expires_ts=order.expires_ts, tag=order.tag)
        self.shadow_of[twin.order_id] = order.order_id
        self.shadow.submit(twin, now)

    def _dry_submit(self, order: Order, now: float) -> None:
        """SHADOW: take a copy of the order through the real-money path, unposted."""
        if self.dry_twin is None:
            return
        twin = Order(token_id=order.token_id, side=order.side, price=order.price, size=order.size,
                     tif=order.tif, expected_price=order.expected_price, fair_at_decision=order.fair_at_decision,
                     expires_ts=order.expires_ts, tag=order.tag, signal_id=order.signal_id)
        self.dry_of[twin.order_id] = order.order_id
        self.dry_twin.submit(twin, now)

    def _on_dry_result(self, res, now: float) -> None:
        """What the real path would have done with a SHADOW order: DRY_RUN means
        it would have been POSTED; anything else (stale decision, price moved,
        caps, kill switch, a signing error) is why it would not have been."""
        paper_id = self.dry_of.pop(res.order.order_id, None)
        if paper_id is None:
            return
        o = res.order
        fields = {"live_path": res.reject_reason.value or "none",
                  "live_path_would_post": res.reject_reason.value == "dry_run",
                  "live_path_presend_ms": round(o.sent_ts - o.decision_ts, 1) if (o.sent_ts and o.decision_ts) else
                  (round(self.clock.now() - o.decision_ts, 1) if o.decision_ts else None),
                  "live_path_sign_ms": round(o.sign_ms, 1) if o.sign_ms else None,
                  "live_path_sent_ask": o.sent_ask or None}
        self._count_exec(f"SHADOW_LIVE_PATH_{(res.reject_reason.value or 'none').upper()}")
        if paper_id in self.forensics.records:
            self.forensics.open(paper_id, **fields)
        else:
            self.forensics.amend(paper_id, now, **fields)
        if o.signal_id:
            self.outcomes.attach(o.signal_id, live_path=fields["live_path"])

    def _on_shadow_result(self, res, now: float) -> None:
        live_id = self.shadow_of.get(res.order.order_id)
        if live_id is None:
            return
        if res.order.state is OrderState.RESTING and not res.fills and not res.is_rejected:
            return                                          # still working in the shadow
        self.shadow_of.pop(res.order.order_id, None)
        if live_id not in self.forensics.records:
            # the live record already closed (a 3 s take-profit beats a 20 s
            # paper expiry): amend it in the file rather than lose the answer
            qty = sum(f.size for f in res.fills)
            self.forensics.amend(live_id, now, paper_outcome=(
                f"REJECTED:{res.reject_reason.value}" if res.is_rejected else "FILLED"),
                paper_fill_quantity=round(qty, 4) if qty else None,
                paper_fill_price=round(sum(f.price * f.size for f in res.fills) / qty, 5) if qty else None)
            log.info("PAPER-SHADOW %-12s (late) would have %s", live_id[:12],
                     f"been {res.reject_reason.value}" if res.is_rejected else "filled")
            return
        if res.is_rejected:
            self.forensics.open(live_id, paper_outcome=f"REJECTED:{res.reject_reason.value}")
            log.info("PAPER-SHADOW %-12s would have been %s", live_id[:12], res.reject_reason.value)
            return
        qty = sum(f.size for f in res.fills)
        px = sum(f.price * f.size for f in res.fills) / qty if qty else None
        fields = {"paper_outcome": "FILLED", "paper_fill_quantity": round(qty, 4),
                  "paper_fill_price": None if px is None else round(px, 5)}
        if res.order.side is Side.SELL:
            fields["paper_exit_price"] = fields["paper_fill_price"]
            entry_id = self.forensics.entry_for(res.order.token_id)
            if entry_id is not None:
                self.forensics.open(entry_id, paper_exit_price=fields["paper_fill_price"])
        self.forensics.open(live_id, **fields)
        log.info("PAPER-SHADOW %-12s would have filled %.2f @ %.4f", live_id[:12], qty, px or 0.0)

    def _update_guard(self, now: float) -> None:
        """Feed the guard every measurement it trips on."""
        ages = []
        for asset in self.assets:
            sts = self.spot_ts.get(asset)
            if sts is not None:
                ages.append(max(0.0, now - sts))
        # a book with ts 0 has had no snapshot yet: unknown, not infinitely old
        books = [b.ts for lm in self.markets.values() if not lm.settled and now < lm.meta.market.close_ts
                 for b in lm.books.values() if b.ts > 0]
        if books:
            ages.append(max(0.0, now - max(books)))
        data_age = min(ages) if ages else None       # the FRESHEST thing we have
        # only streaming feeds can be "silent"; gamma is a REST poll that
        # legitimately says nothing for minutes between probes
        feeds = {n: st.as_dict(now) for n, st in self.feed_health.items() if n not in POLLED_FEEDS}
        ex = self.exchange
        account = self.is_live
        live = ex.snapshot() if account else {}
        rtt = live.get("rtt_p95_ms") if self.is_live else None
        if self.dry_twin is not None:
            self._update_account_check(now)
        pos = live.get("positions") or {}
        dropped = self._drop_unconfirmed_restores(pos) if account else set()
        unknown = [u["token_id"] for u in pos.get("unknown_positions", [])] + \
                  [u["token_id"] for u in pos.get("missing_positions", []) if u["token_id"] not in dropped]
        self.guard.update(
            now / 1000.0, feeds=feeds, data_age_ms=data_age, rtt_p95_ms=rtt,
            balance_drift_usdc=live.get("balance_drift") if account else None,
            unknown_positions=unknown, consecutive_losses=self.risk.consecutive_losses,
            pnl_mismatches=self.forensics.pnl_mismatches,
            venue_halted=getattr(ex, "halted", None) if self.is_live else None,
            foreign_trades=live.get("foreign_trades") if account else None,
            # live and shadow: entries wait for a position check that could see the markets we trade
            reconciled=(bool(pos.get("checked")) and pos.get("watched_n", 1) > 0) if account else None)
        if self.guard.tripped and self.guard.since and abs(self.guard.since - now / 1000.0) < 1.5:
            log.error("GUARD tripped: %s", "; ".join(f"{k}: {v}" for k, v in self.guard.tripped.items()))

    # ───────────────────────── markouts and signal fates ──────────────────────

    def _sample_quote(self, token_id: str, cap: float | None = None):
        """(best bid, best ask, ask depth at or under ``cap``) on our book now."""
        entry = self.token_index.get(token_id)
        if entry is None:
            return None
        book = entry[1].books[token_id].to_order_book()
        depth = None if cap is None else sum(l.size for l in book.asks if l.price <= cap + 1e-9)
        return book.best_bid, book.best_ask, depth

    def _mark_due(self, now_ms: float) -> None:
        """Take whatever markout / signal-horizon samples are due. Called after
        every book message (one float comparison when nothing is due), so a
        100 ms markout is sampled within a message of 100 ms, not a tick."""
        if now_ms >= self.markouts.next_due:
            self.markouts.due(now_ms, self._sample_quote)
        if now_ms >= self.outcomes.next_due:
            self.outcomes.due(now_ms, self._sample_quote)

    def _start_markout(self, order: Order, fill_ts: float, price: float, shares: float) -> None:
        info = self.fair_by_order.get(order.order_id, {})
        q = self._sample_quote(order.token_id)
        bid, ask = (q[0], q[1]) if q else (None, None)
        spot = self.spot.get(info.get("asset", ""))
        self.markouts.on_fill(
            order_id=order.order_id, token_id=order.token_id, side=order.side.value, price=price, size=shares,
            now_ms=fill_ts, bid=bid, ask=ask, asset=info.get("asset"), slug=info.get("slug"),
            kind=info.get("kind", "entry") if info.get("exit") else "entry", mode=self.mode_label,
            signal_id=order.signal_id or None, decision_price=order.expected_price, limit=order.price,
            decision_ts=order.decision_ts, sent_ts=order.sent_ts or None, spot_at_fill=spot,
            signal_mid=info.get("p_market"), secs_left=info.get("secs_left"))

    def _on_paper_arrival(self, order: Order, arrival_ts: float, vwap: float, shares: float) -> None:
        """Paper: the order matched at its simulated arrival (provisionally --
        the quote-survival verdict comes later and is attached then)."""
        self._start_markout(order, arrival_ts, vwap, shares)

    def _settling_tokens(self) -> dict[str, float]:
        """token -> shares we hold in markets past their close that we have not settled (worker thread: reads only)."""
        out: dict[str, float] = {}
        now = self.clock.now()
        for lm in list(self.markets.values()):
            m = lm.meta.market
            if lm.settled or now < m.close_ts:
                continue
            for t in (m.yes_token_id, m.no_token_id):
                pos = self.exchange.positions.get(t)
                if pos is not None and pos.shares > 1e-9:
                    out[t] = pos.shares
        for it in list(self._late.values()):
            for t in (it.get("tokens") or {}):
                pos = self.exchange.positions.get(t)
                if pos is not None and pos.shares > 1e-9:
                    out[t] = pos.shares
        return out

    def _on_external_fill_threadsafe(self, order: Order, fill, trade: dict) -> None:
        """The venue worker found a trade made outside this bot (polymarket.com) and booked it; the loop records it."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._on_external_fill, order, fill, trade)

    def _external_mark(self, token_id: str, shares: float) -> float | None:
        """The price a venue position we did not buy is adopted at: the token's mid from our book (worker thread: reads only)."""
        hit = self.token_index.get(token_id)
        if hit is None:
            return None
        _slug, lm = hit
        live = lm.books.get(token_id)
        ob = live.to_order_book() if live is not None else None
        if ob is None:
            return None
        # a buy outside the bot is booked at the ask, a sale at the bid (what the trade most likely paid / got)
        if shares < 0:
            return ob.best_bid if ob.best_bid is not None else ob.best_ask
        return ob.best_ask if ob.best_ask is not None else ob.best_bid

    def _on_external_fill(self, order: Order, fill, trade: dict) -> None:
        """A fill made outside this bot (polymarket.com), already in the venue adapter's position and cash: the
        ledger, the order list (journal), the risk book and the engine's books follow, so the dashboard and the
        strategy see the same account the venue does."""
        now = self.clock.now()
        hit = self.token_index.get(order.token_id)
        if hit is None:
            log.warning("[EXTERNAL] a trade in a market this bot does not know (%s): in the account only", order.token_id[:10])
            return
        slug, lm = hit
        m = lm.meta.market
        side = "UP" if order.token_id == m.yes_token_id else "DOWN"
        sell = order.side is Side.SELL
        kind = "WEB_SELL" if sell else "WEB_BUY"
        oid = order.order_id
        self.decisions.order_event("ORDER_CREATED", oid, now, market=slug, side=side, kind=kind, price=fill.price,
                                   size=round(fill.size, 2), tif="FOK")
        self.decisions.order_event("ORDER_FILLED", oid, now, market=slug, side=side, size=round(fill.size, 2),
                                   price=round(fill.price, 4))
        self._ledger_append({"ts": now, "event": "fill", "action": "SELL" if sell else "BUY", "slug": slug, "asset": m.asset,
                             "epoch": lm.epoch, "token_id": order.token_id, "side": side, "kind": kind, "source": "polymarket.com",
                             "trade_id": trade.get("trade_id"), "price": round(fill.price, 4), "size": round(fill.size, 4),
                             "fee": round(fill.fee, 5), "cost": round((-1.0 if sell else 1.0) * fill.price * fill.size, 4),
                             "mode": self.mode_label, "live": self.is_live, "strategy": self.cfg.engine.strategy})
        self._apply_external_to_books(slug, lm, m, side, sell, fill.size, fill.price, fill.fee, now, kind)
        log.warning("[EXTERNAL] %s %s %.2f %s @ %.4f on polymarket.com adopted", kind, slug, fill.size, side, fill.price)

    def _apply_external_to_books(self, slug, lm, m, side: str, sell: bool, size: float, price: float, fee: float, now: float,
                                 kind: str) -> None:
        """The risk book and the engine, for a fill that did not come from one of the bot's own orders."""
        tokens = (m.yes_token_id, m.no_token_id)
        if sell:
            op = self.risk.open.get(slug)
            if op is not None:
                held = [self.exchange.positions[t] for t in tokens if t in self.exchange.positions]
                op.usdc = max(0.0, sum(p.cost_basis + p.fees_paid for p in held if p.shares > 1e-6))
                op.shares = sum(p.shares for p in held if p.shares > 1e-6)
        else:
            self.risk.on_fill(slug, m.asset, lm.epoch, side, price * size, size, ends_entry=False)
        on_ext = getattr(self.engine, "on_external_fill", None)
        if callable(on_ext):
            on_ext(slug, side, float(size), float(price), float(fee), "SELL" if sell else "BUY", now, kind)
        self._persist_positions()

    def _on_venue_fill_threadsafe(self, order: Order, price: float, shares: float, ts: float) -> None:
        """Live: called from the venue worker thread the moment a POST reply
        reports a match; the markout clock starts on the loop right away."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._start_markout, order, ts, price, shares)

    def _pre_post_check(self, order: Order) -> str | None:
        """The venue worker's last look at our freshest book, right before a POST.

        Runs in the adapter's WORKER THREAD, so it only reads: one snapshot
        of the token's ask levels (a C-level copy of a dict; a concurrent
        change can at worst make the copy fail, which is not a veto -- the
        order's price cap still binds at the venue). An entry is dropped when
        the ask has already left the cap or the depth under the cap no longer
        covers the size: the venue would kill it anyway, and a killed order
        spends the hourly budget and trips the kill-rate guard.
        """
        if order.side is not Side.BUY:
            return None
        if order.tif is TimeInForce.GTC:
            # a RESTING (maker) bid is meant to sit UNDER the ask until the market comes to it: "the ask has left the
            # cap" is its normal state, not a reason to drop it (2026-10-08: every opposite-side bid of the live pair-lock
            # bot was refused here as price_moved and re-sent under a new order id until the ask fell to it)
            return None
        entry = self.token_index.get(order.token_id)
        if entry is None:
            return None
        lb = entry[1].books.get(order.token_id)
        if lb is None:
            return None
        try:
            asks = sorted(list(lb.asks.items()))
        except RuntimeError:
            return None
        best = lb._best_ask                                  # noqa: SLF001 -- the venue's own touch, as to_order_book uses it
        if best is not None:
            asks = [a for a in asks if a[0] >= best - 1e-9]
        if not asks:
            return "no ask on the book"
        order.sent_ask = asks[0][0]                          # the price at submission (T5)
        if asks[0][0] > order.price + 1e-9:
            return f"ask {asks[0][0]:.2f} above the cap {order.price:.2f}"
        depth = sum(sz for px, sz in asks if px <= order.price + 1e-9)
        # a fill-or-kill needs its whole size under the cap; a fill-and-kill (IOC) takes
        # what is there, so the venue minimum is enough
        need = order.size
        if order.tif is TimeInForce.IOC:
            need = min(order.size, float(entry[1].meta.market.min_size or 5.0))
        if depth + 1e-9 < need:
            return f"{depth:.1f} shares under the cap {order.price:.2f}, need {need:.1f}"
        return None

    def _kill_state(self) -> str | None:
        """``"all"`` while ``<kill>_ALL`` exists, ``"entries"`` while ``<kill>``
        does, else None. A stat per ORDER, not per tick: it is only asked
        right before something would be sent."""
        try:
            if Path(str(self.kill_path) + "_ALL").exists():
                return "all"
            if self.kill_path.exists():
                return "entries"
        except OSError:
            return "entries"                  # cannot tell: fail closed for new risk
        return None

    def _update_account_check(self, now: float) -> None:
        """SHADOW: would the REAL account let micro-live trade right now?

        Balance drift, unknown positions, foreign trades and a missing
        reconciliation are evaluated on the real account (read-only) and
        REPORTED -- they do not stop shadow's paper decisions, or a shared or
        dirty account would leave shadow with nothing to measure. The answer
        is in the state as ``account_check`` and in the log.
        """
        snap = self.dry_twin.snapshot()
        pos = snap.get("positions") or {}
        unknown = [u["token_id"] for u in pos.get("unknown_positions", [])] + \
                  [u["token_id"] for u in pos.get("missing_positions", [])]
        before = set(self.account_check.tripped)
        self.account_check.update(
            now / 1000.0, balance_drift_usdc=snap.get("balance_drift"), unknown_positions=unknown,
            foreign_trades=snap.get("foreign_trades"),
            reconciled=bool(pos.get("checked")) and pos.get("watched_n", 1) > 0)
        new = set(self.account_check.tripped) - before - {"COOLDOWN"}
        if new:
            log.warning("SHADOW: micro-live would be BLOCKED on this account: %s",
                        "; ".join(f"{k}: {self.account_check.tripped[k]}" for k in sorted(new)))

    def _entry_throttled(self, lm: LiveMarket, now: float) -> bool:
        """True while this market's last entry order is too recent to retry.

        The in-flight guard only covers the ~40 ms an order is on the wire; a
        kill comes back inside that and the next tick re-sends the same order.
        """
        if now - lm.last_entry_ms < float(getattr(self.engine, "entry_retry_ms", ENTRY_RETRY_MS)):
            self._count_exec("ENTRY_THROTTLED")
            return True
        return False

    def _exit_candidate(self, lm: LiveMarket, token_id: str, now: float) -> dict | None:
        """Should we sell this token now, and at what price?

        Returns the exit's economics, or None with the reason counted. The
        price we test is the best BID -- the only price a taker can actually
        sell into -- and the profit is computed after BOTH fees, entry and
        exit, so a "profitable" exit is one the ledger will agree with.
        """
        cfg = self.cfg.engine
        pos = self.exchange.positions.get(token_id)
        if pos is None or pos.shares <= EXIT_EPS:
            return None
        size = pos.shares
        min_size = float(lm.meta.market.min_size or 0.0)
        if min_size and size + EXIT_EPS < min_size:
            # below the venue's minimum order (a partial fill-and-kill, or rounding dust):
            # no sell can be placed, so it is held to settlement instead of retried
            self._count_exec("EXIT_BELOW_MIN_SIZE")
            return None
        book = lm.books[token_id].to_order_book() if token_id in lm.books else None
        bid = book.best_bid if book is not None else None
        if bid is None:
            self._count_exec("EXIT_NO_BID")
            return None
        entry = pos.cost_basis / size                 # average price paid
        rise = bid - entry
        tick = lm.meta.market.tick_size or 0.01
        max_cross = None
        if isinstance(self.engine, MomentumEngine):
            # manage_momentum_position: price, momentum, time, liquidity and
            # market-state exits, on a FRESH momentum read for this asset
            m = lm.meta.market
            side = "UP" if token_id == m.yes_token_id else "DOWN"
            asset = m.asset
            of = self.orderflow[asset].features(now) if asset in self.orderflow else None
            sig = self.engine.momentum_now(asset, now, of)
            ask = book.best_ask
            spread = (ask - bid) if ask is not None else None
            near_bid = sum(l.size for l in book.bids if l.price >= bid - 2 * tick - EXIT_EPS)
            sts = self.spot_ts.get(asset)
            qa = self.quote_age_ms(token_id, now)
            ages = [a for a in ((now - sts) if sts is not None else None, qa) if a is not None]
            kind = self.engine.exit_decision(asset=asset, side=side, token_id=token_id, entry=entry, bid=bid,
                                             now_ms=now, sig=sig, spread=spread, bid_depth=near_bid, size=size,
                                             data_age_ms=max(ages) if ages else None)
            if kind is None:
                return None
            max_cross = self.engine.mcfg.exit_max_cross
        elif cfg.take_profit_enabled and rise >= cfg.take_profit_delta:
            kind = "TAKE_PROFIT"
        elif cfg.stop_loss_enabled and rise <= -cfg.stop_loss_delta:
            kind = "STOP_LOSS"
        else:
            return None
        # A stop is urgent: it sells through the bid so a downtick in flight
        # does not kill it. A take-profit is optional and waits at the touch.
        px = bid if kind == "TAKE_PROFIT" else max(tick, round(bid - cfg.stop_cross_ticks * tick, 4))
        if kind != "TAKE_PROFIT" and max_cross is not None:
            # an urgent exit walks the bids to the level that covers the whole
            # position (FOK is all-or-nothing), never further than max_cross
            # below the touch: a stop that is never sent is the worse failure
            cum, cover = 0.0, None
            for lvl in book.bids:
                cum += lvl.size
                if cum + EXIT_EPS >= size:
                    cover = lvl.price
                    break
            floor = max(tick, round(bid - max_cross, 4))
            if cover is not None and cover < px:
                px = max(cover, floor)
        # FOK is all-or-nothing: without the depth to clear the whole position
        # at or above our price the order is rejected, so do not send it.
        depth = sum(lvl.size for lvl in book.bids if lvl.price >= px - EXIT_EPS)
        if depth + EXIT_EPS < size:
            self._count_exec("EXIT_THIN_BID")
            return None
        fee = self.exchange.fees.charge(px, size, token_id)
        # full exit, so the realised PnL is everything the position ever cost
        pnl = px * size - fee - pos.cost_basis - pos.fees_paid
        # A take-profit that does not clear both fees is not a profit. A stop
        # is allowed to realise a loss -- that is the entire point of it.
        if kind == "TAKE_PROFIT" and pnl / size < cfg.take_profit_min_net:
            self._count_exec("EXIT_FEE_EATS_IT")
            return None
        return {"size": size, "bid": bid, "px": px, "entry": entry, "pnl": pnl,
                "fee": fee, "kind": kind}

    def _take_profit(self, now: float) -> None:
        """Sell anything that has risen far enough to be worth banking.

        Runs before the entry scan so a position is never added to in the same
        tick it is being closed in.
        """
        cfg = self.cfg.engine
        self._engine_pair_cancels(now)
        if getattr(self.engine, "manages_exits", False):
            self._managed_exits(now)
            return
        if not (cfg.take_profit_enabled or cfg.stop_loss_enabled or isinstance(self.engine, MomentumEngine)):
            return
        for slug, lm in list(self.markets.items()):
            m = lm.meta.market
            if lm.settled or (lm.exited and not self._reentry) or lm.inflight > 0:
                continue
            if (m.close_ts - now) / 1000.0 <= cfg.take_profit_min_secs_left:
                continue
            # A killed exit is retried while its condition holds. Unthrottled,
            # that is one real FOK per tick: 60 kills spent the live hourly
            # budget in about a minute and left the position with no way out.
            if now - lm.last_exit_ms < EXIT_RETRY_MS:
                continue
            for token_id, side in ((m.yes_token_id, "UP"), (m.no_token_id, "DOWN")):
                ex = self._exit_candidate(lm, token_id, now)
                if ex is None:
                    continue
                if self._kill_state() == "all":
                    # KILL stops entries only; KILL_ALL stops exits as well
                    self._count_exec("EXIT_KILL_SWITCH")
                    continue
                order = Order(
                    token_id=token_id, side=Side.SELL, price=ex["px"], size=ex["size"],
                    tif=TimeInForce.FOK, expected_price=ex["bid"],
                    tag=f"{ex['kind']} {side} entry={ex['entry']:.3f} bid={ex['bid']:.3f}",
                )
                self.fair_by_order[order.order_id] = {
                    "slug": slug, "asset": m.asset, "epoch": lm.epoch,
                    "token_id": token_id, "side": side, "exit": True,
                    "kind": ex["kind"], "entry": round(ex["entry"], 4),
                    "secs_left": round((m.close_ts - now) / 1000.0, 1),
                }
                entry_id = self.forensics.entry_for(token_id)
                if entry_id is not None:
                    self.forensics.stage(entry_id, "POSITION_MANAGED", now, exit_order_id=order.order_id,
                                         reason_for_exit=f"{ex['kind']} bid {ex['bid']:.3f} vs entry {ex['entry']:.3f}")
                self.forensics.open(order.order_id, kind=ex["kind"].lower(), slug=slug, asset=m.asset,
                                    epoch=lm.epoch, token_id=token_id, side=side, mode=self.mode_label,
                                    decision_price=ex["bid"], quantity=round(ex["size"], 4),
                                    entry_price=round(ex["entry"], 4), entry_order_id=entry_id,
                                    reason_for_exit=ex["kind"], strategy=self.cfg.engine.strategy,
                                    strategy_version=STRATEGY_VERSION)
                self.forensics.stage(order.order_id, "DECISION", now)
                self.decisions.order_event("ORDER_CREATED", order.order_id, now, market=slug, side=side,
                                           kind=ex["kind"], price=order.price, size=round(order.size, 2))
                self.exchange.submit(order, now)
                self._shadow_submit(order, now)
                self._dry_submit(order, now)
                self.forensics.stage(order.order_id, "ORDER_SUBMITTED", now, limit_price=order.price)
                self.decisions.order_event("ORDER_SUBMITTED", order.order_id, now, market=slug, kind=ex["kind"])
                lm.inflight += 1
                lm.last_exit_ms = now
                log.info("%-11s %-26s %-4s %6.2f sh  entry %.3f -> bid %.3f  "
                         "expect %+.2f  %5.1fs left",
                         ex["kind"], slug, side, ex["size"], ex["entry"], ex["bid"],
                         ex["pnl"], (m.close_ts - now) / 1000.0)
                break                       # one exit order per market per tick

    def _managed_exits(self, now: float) -> None:
        """PAIRED-EXIT: the engine owns every exit. Each tick, each market with legs gets the orders its legs'
        plans need: urgent FOK sells (stop / thesis / timeout / time exit), take-profit asks and their
        cancels, the optional pair's cancel. KILL stops entries only; KILL_ALL stops these too."""
        for slug, lm in list(self.markets.items()):
            m = lm.meta.market
            if lm.settled or now >= m.close_ts:
                continue
            up = lm.books[m.yes_token_id].to_order_book() if m.yes_token_id in lm.books else None
            dn = lm.books[m.no_token_id].to_order_book() if m.no_token_id in lm.books else None
            held = {t: (self.exchange.positions[t].shares if t in self.exchange.positions else 0.0)
                    for t in (m.yes_token_id, m.no_token_id)}
            states = {oid: o.state.name for oid, o in self._managed_orders.items()}
            try:
                actions = self.engine.exit_actions(m, up, dn, now, held, states)
            except Exception:                       # an exit planner bug must not stop the loop
                log.exception("[PX] exit planner failed for %s", slug)
                continue
            for a in actions:
                if a.kind in ("LADDER_CANCEL", "PAIR_CANCEL"):
                    if a.order_id and a.order_id != "pending":
                        self.exchange.cancel(a.order_id, now)
                    if a.kind == "PAIR_CANCEL":
                        pr = self._pairs_open.pop(a.order_id, None)
                        self.decisions.order_event("ORDER_CANCELLED", a.order_id, now, reason="pulled by the engine", market=slug)
                        done = getattr(self.engine, "on_pair_done", None)
                        if pr is not None and callable(done):
                            done(a.order_id, filled=False)
                        self._persist_positions()
                    # a cancelled ask stays in _managed_orders until the venue's state says it is gone: the engine
                    # re-places a level only after that, so two asks never rest for the same shares
                    continue
                if self._kill_state() == "all":
                    self._count_exec("EXIT_KILL_SWITCH")
                    continue
                gtc = a.tif == "GTC"
                order = Order(token_id=a.token_id, side=Side.SELL, price=a.price, size=a.shares,
                              # "IOC": fill-and-kill (pair-lock ULR sells what the bids hold, the rest is retried)
                              tif=TimeInForce.GTC if gtc else TimeInForce.IOC if a.tif == "IOC" else TimeInForce.FOK,
                              expires_ts=m.close_ts if gtc else None, expected_price=a.price,
                              tag=f"{a.kind} {a.side} {a.leg_id} {a.detail}")
                self.fair_by_order[order.order_id] = {
                    "slug": slug, "asset": m.asset, "epoch": lm.epoch, "token_id": a.token_id, "side": a.side,
                    "exit": True, "managed": True, "kind": a.kind, "leg_id": a.leg_id, "no_inflight": gtc,
                    "secs_left": round((m.close_ts - now) / 1000.0, 1)}
                self.forensics.open(order.order_id, kind=a.kind.lower(), slug=slug, asset=m.asset, epoch=lm.epoch,
                                    token_id=a.token_id, side=a.side, mode=self.mode_label, decision_price=a.price,
                                    quantity=round(a.shares, 4), reason_for_exit=a.kind,
                                    strategy=self.cfg.engine.strategy, strategy_version=STRATEGY_VERSION)
                self.forensics.stage(order.order_id, "DECISION", now)
                self.decisions.order_event("ORDER_CREATED", order.order_id, now, market=slug, side=a.side,
                                           kind=a.kind, price=order.price, size=round(order.size, 2))
                self.exchange.submit(order, now)
                self._shadow_submit(order, now)
                self._dry_submit(order, now)
                self._managed_orders[order.order_id] = order
                on_order = getattr(self.engine, "on_exit_order", None)
                if callable(on_order):
                    on_order(a.leg_id, a.kind, order.order_id)
                self.forensics.stage(order.order_id, "ORDER_SUBMITTED", now, limit_price=order.price)
                if not gtc:
                    lm.inflight += 1
                lm.last_exit_ms = now
                log.info("%-11s %-26s %-4s %6.2f sh %s at %s %.2f  (%s)  %5.1fs left", a.kind, slug, a.side, a.shares,
                         "GTC ask" if gtc else f"{order.tif.value} sell", ">=", a.price, a.detail, (m.close_ts - now) / 1000.0)
        for oid in [k for k, o in self._managed_orders.items()
                    if o.state.name in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")]:
            self._managed_orders.pop(oid, None)

    def _book_managed_exit(self, info: dict, fills: list, now: float) -> None:
        """A paired-exit sell filled, possibly for part of the market's holding. The venue position already
        carries it (Position.apply: shares down, cost basis down by the proceeds). While the market still holds
        shares nothing is realised yet -- its result is booked once, when it goes flat (here) or at settlement
        (payout - remaining cost basis, which already contains these proceeds)."""
        slug = info.get("slug", "")
        lm = self.markets.get(slug)
        token_id = info.get("token_id", "")
        oid = fills[0].order_id if fills else ""
        size = sum(f.size for f in fills)
        proceeds = sum(f.price * f.size for f in fills)
        fee = sum(f.fee for f in fills)
        avg = proceeds / size if size else 0.0
        self._managed_orders.pop(oid, None)
        if info.get("kind") == "MANUAL_SELL":
            # the dashboard's Sell: into the engine's books as an external fill, not one of its own exits
            on_ext = getattr(self.engine, "on_external_fill", None)
            if callable(on_ext):
                on_ext(slug, info.get("side", ""), float(size), float(avg), float(fee), "SELL", now, "MANUAL_SELL")
            kind = "MANUAL_SELL"
        else:
            on_fill = getattr(self.engine, "on_exit_fill", None)
            kind = on_fill(oid, size, avg, fee, now) if callable(on_fill) else info.get("kind", "EXIT")
        info = {**info, "kind": kind}
        m = lm.meta.market if lm is not None else None
        tokens = (m.yes_token_id, m.no_token_id) if m is not None else (token_id,)
        flat = all(self.exchange.positions[t].shares <= 1e-6 for t in tokens if t in self.exchange.positions)
        self._ledger_append({"ts": now, "event": "fill", "action": "SELL",
                             **{k: v for k, v in info.items() if k not in ("exit", "no_inflight")},
                             "price": round(avg, 4), "size": round(size, 4), "fee": round(fee, 4),
                             "cost": round(-proceeds, 4)})
        self.forensics.stage(oid, "POSITION_CLOSED" if flat else "PARTIAL_FILL", now)
        self.forensics.close(oid, now, execution_price=round(avg, 4), fees=round(fee, 4), exit_price=round(avg, 4))
        if not flat:
            op = self.risk.open.get(slug)
            if op is not None:
                held = [self.exchange.positions[t] for t in tokens if t in self.exchange.positions]
                op.usdc = max(0.0, sum(p.cost_basis + p.fees_paid for p in held if p.shares > 1e-6))
                op.shares = sum(p.shares for p in held if p.shares > 1e-6)
            log.info("SOLD  %-26s %-4s %6.2f sh @ %.3f  %s (part: the market still holds shares)  balance %.2f",
                     slug, info.get("side", ""), size, avg, kind, self.exchange.balance)
            self._persist_positions()
            return
        # flat: the whole market's round trip is realised now -- everything both tokens ever cost and returned
        pnl = 0.0
        for t in tokens:
            pos = self.exchange.positions.get(t)
            if pos is None:
                continue
            pnl -= pos.cost_basis + pos.fees_paid
            pos.cost_basis = pos.fees_paid = pos.shares = 0.0
        asset, epoch = info.get("asset", ""), int(info.get("epoch", 0))
        self.risk.on_settle(slug, pnl, self.clock.now() / 1000.0)
        self.stats.settled += 1
        self.stats.realised_pnl += pnl
        self.pnl_by_asset[asset] = self.pnl_by_asset.get(asset, 0.0) + pnl
        self.epoch_pnl[epoch] = self.epoch_pnl.get(epoch, 0.0) + pnl
        if pnl > 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        if lm is not None:
            lm.exited = True
            lm.pnl += pnl
        if isinstance(self.engine, MomentumEngine):
            self.engine.on_exit(slug, token_id, now)
        on_exited = getattr(self.engine, "on_market_exited", None)
        if callable(on_exited):
            on_exited(slug, pnl, now)                     # the engine realises the market now, before its resolution
        log.info("SOLD  %-26s %-4s %6.2f sh @ %.3f  %s  market flat: pnl %+.2f  balance %.2f",
                 slug, info.get("side", ""), size, avg, kind, pnl, self.exchange.balance)
        self._restored_meta.pop(slug, None)
        if self._slog and lm is not None:
            log.info("[POSITION] %s %s closed: %s, sold %.2f sh @ %.3f, pnl %+.2f | %s",
                     self._mkt_label(lm.meta.market), info.get("side", ""), kind, size, avg, pnl,
                     self._portfolio_line())
        self._persist_positions()
        self._ledger_append({"ts": now, "event": "settle", "slug": slug, "asset": asset, "epoch": epoch,
                             "exited": True, "pnl": round(pnl, 4), "balance": round(self.exchange.balance, 4),
                             "kind": kind})

    def _book_exit(self, info: dict, fills: list, now: float) -> None:
        """Realise an early exit: PnL, risk release, stats and the ledger.

        Deliberately NOT risk.on_fill: this reduces exposure. on_settle is what
        pops the open position and books the result, which is the same thing
        settlement does -- an exit is simply a settlement we chose the price of.
        """
        slug = info.get("slug", "")
        lm = self.markets.get(slug)
        token_id = info.get("token_id", "")
        pos = self.exchange.positions.get(token_id)
        size = sum(f.size for f in fills)
        proceeds = sum(f.price * f.size for f in fills)
        # everything the position ever cost is still on it; after a full exit
        # what remains is exactly the realised result
        pnl = -(pos.cost_basis + pos.fees_paid) if pos is not None else 0.0
        if pos is not None:
            pos.cost_basis = 0.0
            pos.fees_paid = 0.0
            pos.shares = 0.0
        asset = info.get("asset", "")
        epoch = int(info.get("epoch", 0))
        self.risk.on_settle(slug, pnl, self.clock.now() / 1000.0)
        self.stats.settled += 1
        self.stats.realised_pnl += pnl
        self.pnl_by_asset[asset] = self.pnl_by_asset.get(asset, 0.0) + pnl
        self.epoch_pnl[epoch] = self.epoch_pnl.get(epoch, 0.0) + pnl
        if pnl > 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        if lm is not None:
            lm.exited = True
            lm.pnl += pnl
        if isinstance(self.engine, MomentumEngine):
            self.engine.on_exit(slug, token_id, now)
        avg = proceeds / size if size else 0.0
        log.info("SOLD  %-26s %-4s %6.2f sh @ %.3f  pnl %+.2f  balance %.2f",
                 slug, info.get("side", ""), size, avg, pnl, self.exchange.balance)
        self._restored_meta.pop(slug, None)
        if self._slog and lm is not None:
            log.info("[POSITION] %s %s closed: %s, sold %.2f sh @ %.3f, pnl %+.2f | %s",
                     self._mkt_label(lm.meta.market), info.get("side", ""), info.get("kind", "exit"), size, avg,
                     pnl, self._portfolio_line())
        self._persist_positions()
        exit_fees = sum(f.fee for f in fills)
        exit_id = fills[0].order_id if fills else ""
        self.forensics.stage(exit_id, "POSITION_CLOSED", now)
        self.forensics.close(exit_id, now, execution_price=round(avg, 4), fees=round(exit_fees, 4),
                             exit_price=round(avg, 4), pnl=round(pnl, 4), pnl_after_fees=round(pnl, 4))
        entry_id = self.forensics.entry_for(token_id)
        if entry_id is not None:
            self.forensics.stage(entry_id, "POSITION_CLOSED", now)
            self.forensics.close(entry_id, now, reported_pnl=pnl, exit_price=round(avg, 4),
                                 pnl=round(pnl, 4), pnl_after_fees=round(pnl, 4),
                                 fees=round(exit_fees + (info.get("entry_fees") or 0.0), 4),
                                 reason_for_exit=info.get("kind"), outcome="EXIT")
        self._ledger_append({
            "ts": now, "event": "fill", "action": "SELL", **{
                k: v for k, v in info.items() if k != "exit"},
            "price": round(avg, 4), "size": round(size, 4),
            "fee": round(sum(f.fee for f in fills), 4),
            "cost": round(-proceeds, 4),      # a credit, not a cost
        })
        self._ledger_append({
            "ts": now, "event": "settle", "slug": slug, "asset": asset,
            "epoch": epoch, "exited": True, "pnl": round(pnl, 4),
            "balance": round(self.exchange.balance, 4),
        })

    # 200ms sampling, bounded to the window plus a 60s lead-in.
    HISTORY_INTERVAL_MS = 200.0
    HISTORY_POINTS = 2000

    @staticmethod
    def _token_price(book) -> float | None:
        """A usable price for one token even when its book is one-sided.

        Once a window is decided the venue books go one-sided in a
        complementary way (winner: bids only, loser: asks only). The side that
        IS quoted is the marketable price and the two sum to 1.00.
        """
        if book is None:
            return None
        if book.best_bid is not None and book.best_ask is not None:
            return book.mid
        if book.best_ask is not None:
            return book.best_ask
        if book.best_bid is not None:
            return book.best_bid
        return None

    @staticmethod
    def _recover_mids(up_mid, down_mid):
        """Recover a missing token price from the other token (they sum to 1)."""
        if up_mid is None and down_mid is not None:
            return 1.0 - down_mid, down_mid
        if down_mid is None and up_mid is not None:
            return up_mid, 1.0 - up_mid
        return up_mid, down_mid

    def _sample_prices(self, now: float) -> None:
        """Snapshot every live market's token prices, touch, depth, the
        composite spot and each exchange's price -- the data collection system.
        One time axis for all of it, so nothing can drift apart."""
        if now - self._last_hist_ms < self.HISTORY_INTERVAL_MS:
            return
        self._last_hist_ms = now
        r4 = lambda v: round(v, 4) if v is not None else None  # noqa: E731
        for slug, lm in self.markets.items():
            m = lm.meta.market
            if now < m.open_ts - 60_000 or now > m.close_ts + 30_000:
                continue
            up = lm.books[m.yes_token_id].to_order_book()
            dn = lm.books[m.no_token_id].to_order_book()
            um, dm = self._recover_mids(self._token_price(up), self._token_price(dn))
            if um is None or dm is None:
                continue
            view = self.composite.view(m.asset, now)
            spot = self.spot.get(m.asset)
            point = {
                "t": round(now), "up": round(um, 4), "down": round(dm, 4),
                "spot": round(spot, 6) if spot is not None else None,
                "left": round(m.seconds_remaining(now), 1),
                "ub": r4(up.best_bid), "ua": r4(up.best_ask), "db": r4(dn.best_bid), "da": r4(dn.best_ask),
                # depth within 2 ticks of each touch, in shares
                "ubd": round(_depth_near_touch(up.bids, up.best_bid, ask=False), 1),
                "uad": round(_depth_near_touch(up.asks, up.best_ask, ask=True), 1),
                "dbd": round(_depth_near_touch(dn.bids, dn.best_bid, ask=False), 1),
                "dad": round(_depth_near_touch(dn.asks, dn.best_ask, ask=True), 1),
                "srcs": {k: round(v, 6) for k, v in view.sources.items()} if view else None,
                "of": self._of_point(m.asset, now),
            }
            self.price_history.setdefault(slug, deque(maxlen=self.HISTORY_POINTS)).append(point)
            self.history.append(slug, point)
            observe = getattr(self.engine, "observe_point", None)
            if callable(observe):
                # paired-exit: its features are computed on exactly these samples (the archive's points)
                observe(slug, m, point, now)
        self.history.flush()

    def _sync_latency_profile(self) -> None:
        rep = self.latency.report()
        # the order round trip is the measured venue POST, or its measured
        # prior until enough live samples exist -- never the Gamma GET
        rtt = rep["order_rtt_p50"]
        presend = rep["presend_p50"]
        if abs(self.strategy_latency.round_trip_ms() - (rtt + presend)) >= 25.0 \
                or self.strategy_latency.name != "measured":
            self.strategy_latency = replace(
                HOME_BROADBAND, name="measured",
                submit_base=presend + rtt / 2.0, submit_jitter=0.0, ack_base=rtt / 2.0, ack_jitter=0.0,
                md_spot_base=rep["spot_p50"], md_spot_jitter=0.0,
                md_book_base=rep["book_p50"], md_book_jitter=0.0,
            )
        # Staleness gates catch a STALLED feed, not a slow one: scale to the
        # measured feed with a floor so a dead feed is still caught -- and a
        # CEILING. Uncapped, a starved process (spot lag p95 29 s on
        # 2026-09-23) widened the spot gate to 72 s, i.e. "trade on prices
        # from a minute ago". A slow feed is a reason not to trade.
        if rep["n_spot"] >= EmpiricalLatency.MIN_SAMPLES:
            self.cfg.engine.max_spot_age_ms = min(max(self._base_spot_age, rep["spot_p95"] * 2.5),
                                                  self._base_spot_age * MAX_GATE_WIDENING)
        if rep["n_book"] >= EmpiricalLatency.MIN_SAMPLES:
            self.cfg.engine.max_book_age_ms = min(max(self._base_book_age, rep["book_p95"] * 2.5),
                                                  self._base_book_age * MAX_GATE_WIDENING)

    def _true_book(self, token_id: str) -> OrderBook | None:
        entry = self.token_index.get(token_id)
        return entry[1].books[token_id].to_order_book() if entry else None

    def _close_ts(self, token_id: str) -> float:
        entry = self.token_index.get(token_id)
        return entry[1].meta.market.close_ts if entry else 0.0

    def _order_timeline(self, order: Order, prefix: str = "") -> dict:
        """T5..T7 (entries) or T9..T10 (exits) as forensics fields."""
        if prefix == "exit_":
            return {"t8_exit_decision": order.decision_ts or None, "t9_exit_sent": order.sent_ts or None,
                    "t10_exit_fill": order.venue_fill_ts or None}
        return {"t5_sent": order.sent_ts or None, "t6_venue_ack": order.venue_ack_ts or None,
                "t7_venue_fill": order.venue_fill_ts or None, "venue_order_id": order.venue_order_id or None,
                "sign_ms": round(order.sign_ms, 1) if order.sign_ms else None,
                "sent_ask": order.sent_ask or None,
                "venue_rtt_ms": round(order.venue_ack_ts - order.sent_ts, 1)
                if (order.venue_ack_ts and order.sent_ts and self.is_live) else None}

    def _on_result(self, res, now: float) -> None:
        info = self.fair_by_order.get(res.order.order_id, {})
        lm = self.markets.get(info.get("slug", ""))
        oid = res.order.order_id
        o = res.order
        done = not (o.state is OrderState.RESTING and not res.fills and not res.is_rejected)
        if done:
            if self.is_live and o.sent_ts and o.decision_ts:
                self.latency.record_presend(o.sent_ts - o.decision_ts)      # queue + signing, measured
            qty = sum(f.size for f in res.fills)
            px = (sum(f.price * f.size for f in res.fills) / qty) if qty else None
            fate = {"filled": bool(res.fills), "fill_price": _r4(px),
                    "reject_reason": res.reject_reason.value if res.is_rejected else None}
            self.markouts.attach(oid, **fate)
            if o.signal_id:
                self.outcomes.attach(o.signal_id, **fate, order_id=oid)
            if oid in self.forensics.records:
                self.forensics.open(oid, **self._order_timeline(o, "exit_" if info.get("exit") else ""))
                if info.get("exit"):
                    entry_id = self.forensics.entry_for(o.token_id)
                    if entry_id is not None:
                        self.forensics.open(entry_id, **self._order_timeline(o, "exit_"))
        if (res.order.state is OrderState.RESTING and not res.fills and not res.is_rejected):
            # the paper engine acks that a GTC order RESTED. It is still
            # working: keep the market held, or the next tick stacks another
            # order on top of it (four of them in six seconds, first run).
            self.forensics.stage(oid, "ORDER_ACKNOWLEDGED", now, resting=True)
            self.decisions.order_event("ORDER_ACCEPTED", oid, now, resting=True)
            return
        # a PAIR order (triangle: the resting buy of the other side after a fill) never held the market --
        # no in-flight count, no working entry -- so its end must not release one that another order holds
        pair = bool(info.get("pair"))
        if pair:
            pr = self._pairs_open.pop(oid, None)
            if pr is not None and pr.get("tif") == "FOK" and res.is_rejected:
                # an executable hedge the venue could not fill: the first leg is NAKED. Never re-placed as a
                # resting order (that would be the rule this mode replaces); said loudly, counted
                pair_done = getattr(self.engine, "on_pair_done", None)
                if callable(pair_done):
                    pair_done(oid, filled=False, killed=True)
                log.warning("[PAIR] %s %s hedge NOT filled (%s): the first leg (%s) is NAKED", pr["slug"], pr["side"],
                            res.reject_reason.value, pr.get("pair_of", "?"))
                self._persist_positions()
            elif (pr is not None and res.is_rejected and res.reject_reason.value != "market_closed"
                    and not pr.get("cancelling") and now < float(pr["close_ts"]) and getattr(self.engine, "pair_retry", True)):
                # refused on arrival with its market still open: the hedge must not just vanish -- place it
                # again (and keep holding new buys meanwhile)
                drop, hold = getattr(self.engine, "drop_pair", None), getattr(self.engine, "on_pair_submit", None)
                if callable(drop):
                    drop(oid)
                pid = f"retry-{oid}"
                if callable(hold):
                    hold(pid, pr["slug"], pr["side"], float(pr["price"]), float(pr.get("full_size", pr["size"])))
                # 2026-10-08: never re-send at once -- a tight retry loop spent the 240-orders/hour cap in a second and
                # then every order (entries too) was refused for an hour. A cap refusal waits 60 s, anything else 3 s;
                # a post-only bid refused because it would CROSS the book (the ask fell to the price) is bought instead
                why = res.reject_reason.value
                wait = 60_000.0 if why in ("live_caps", "halted", "kill_switch") else 3_000.0
                crossed = why in ("no_liquidity", "post_only_cross") and pr.get("tif") == "GTC"
                self._pairs_to_place.append({**pr, "placeholder": pid, "not_before": now + wait, "take": crossed})
                log.warning("[PAIR] %s %s pair refused (%s): placing it again in %.0f s%s", pr["slug"], pr["side"],
                            why, wait / 1000.0, " as a fill-and-kill BUY (the ask is at or under its price)" if crossed else "")
            elif (pr is not None and pr.get("tif") == "IOC" and not pr.get("cancelling") and now < float(pr["close_ts"])
                    and sum(f.size for f in res.fills) + 1e-9 < float(pr["size"]) and getattr(self.engine, "pair_retry", True)):
                # a pair TAKE that filled only part (or nothing on a thin book): the rest rests as a maker bid
                got = sum(f.size for f in res.fills)
                drop, hold = getattr(self.engine, "drop_pair", None), getattr(self.engine, "on_pair_submit", None)
                if callable(drop):
                    drop(oid)
                pid = f"retry-{oid}"
                full = float(pr.get("full_size", pr["size"]))
                if callable(hold):
                    hold(pid, pr["slug"], pr["side"], float(pr["price"]), full)
                rest = round(float(pr["size"]) - got, 2)
                self._pairs_to_place.append({**pr, "size": rest, "full_size": full, "placeholder": pid,
                                             "not_before": now + 1_000.0, "take": False, "tif": "GTC"})
                log.warning("[PAIR] %s %s pair take filled %.2f of %.2f: the rest (%.2f) rests as a maker bid at %.2f",
                            pr["slug"], pr["side"], got, float(pr["size"]), rest, float(pr["price"]))
            else:
                # resolved (filled, or expired at the close): the engine may buy again (triangle wait_for_pair)
                pair_done = getattr(self.engine, "on_pair_done", None)
                if callable(pair_done):
                    pair_done(oid, filled=bool(res.fills))
                px_done = getattr(self.engine, "px_on_pair_done", None)
                if callable(px_done) and getattr(self.engine, "manages_exits", False):
                    q = sum(f.size for f in res.fills)
                    px_done(oid, bool(res.fills), (sum(f.price * f.size for f in res.fills) / q) if q else None,
                            q or None, res.order.token_id, now)
                if pr is not None and not res.fills:
                    self._persist_positions()             # a fill persists below; an expiry here
        elif not info.get("exit") and not info.get("manual"):
            # the entry's fate, for engines that draw it (triangle: unfilled -> its triangle hollow)
            entry_result = getattr(self.engine, "entry_result", None)
            if callable(entry_result):
                entry_result(oid, bool(res.fills), res.reject_reason.value if res.is_rejected else "")
        if lm is not None and not pair and not info.get("no_inflight"):
            lm.inflight = max(0, lm.inflight - 1)
        if not info.get("exit") and not pair:
            self.risk.on_done(info.get("slug", ""))
            on_done = getattr(self.engine, "on_entry_done", None)
            if callable(on_done):
                on_done(info.get("slug", ""), now)
        if res.is_rejected and info.get("managed"):
            self._managed_orders.pop(oid, None)
            on_xr = getattr(self.engine, "on_exit_result", None)
            if callable(on_xr):
                on_xr(oid, res.reject_reason.value, now)
        if res.is_rejected:
            prefix = "EXIT_" if info.get("exit") else ""
            self._count_exec(f"{prefix}{res.reject_reason.value.upper()}")
            log.info("REJECT %-25s %s%s", info.get("slug", "?"), prefix,
                     res.reject_reason.value)
            # the kill-rate guard measures the VENUE refusing our orders; our own
            # refusals (kill switch, stale decision, dry run, caps) are not that,
            # nor are kills the engine expects as part of its design
            if self._slog and lm is not None:
                no_ask = ("" if pair or info.get("exit")
                          or not float(getattr(getattr(self.engine, "scfg", None), "pair_cost", 0.0) or 0.0) > 0.0
                          else " -> nothing bought, so NO other-side ask")
                log.info("[ORDER] %s %s %s not filled: %s%s | %s", self._mkt_label(lm.meta.market),
                         info.get("side", ""), "sell" if info.get("exit") else "pair buy" if pair else "buy",
                         _reject_label(res.order, res.reject_reason.value), no_ask, self._portfolio_line())
            if (res.reject_reason.value in VENUE_KILLS and not pair
                    and res.reject_reason.value not in getattr(self.engine, "expected_kills", ())):
                self.guard.record_order_outcome(False)
            self.forensics.stage(oid, "ORDER_REJECTED", now, reject_reason=res.reject_reason.value)
            self.forensics.close(oid, now, outcome=f"REJECTED:{res.reject_reason.value}")
            event = ("ORDER_EXPIRED" if res.reject_reason.value == "expired" else
                     "ORDER_CANCELLED" if res.order.state is OrderState.CANCELLED else "ORDER_REJECTED")
            self.decisions.order_event(event, oid, now, reason=res.reject_reason.value, market=info.get("slug"))
            return
        if not pair:
            self.guard.record_order_outcome(bool(res.fills))
        self.forensics.stage(oid, "ORDER_ACKNOWLEDGED", now)
        if "ORDER_ACCEPTED" not in self.forensics.records.get(oid, TradeRecordProbe).stages:
            self.decisions.order_event("ORDER_ACCEPTED", oid, now, market=info.get("slug"))
        if info.get("exit") and res.fills:
            # one round trip closed: booked whole, not fill by fill
            self.forensics.stage(oid, "FULL_FILL", now)
            xq = sum(f.size for f in res.fills)
            self.decisions.order_event("ORDER_FILLED", oid, now, market=info.get("slug"), kind=info.get("kind"),
                                       size=round(xq, 2), price=round(sum(f.price * f.size for f in res.fills) / xq, 4) if xq else None)
            if info.get("managed"):
                self._book_managed_exit(info, list(res.fills), now)
            else:
                self._book_exit(info, list(res.fills), now)
            return
        if res.fills:
            filled = sum(f.size for f in res.fills)
            notional = sum(f.price * f.size for f in res.fills)
            full = res.order.remaining <= 1e-9
            self.forensics.stage(oid, "FULL_FILL" if full else "PARTIAL_FILL", now,
                                 execution_price=round(notional / filled, 5), quantity=round(filled, 4),
                                 entry_price=round(notional / filled, 5),
                                 fees=round(sum(f.fee for f in res.fills), 5))
            self.decisions.order_event("ORDER_FILLED" if full else "ORDER_PARTIALLY_FILLED", oid, now,
                                       market=info.get("slug"), side=info.get("side"), size=round(filled, 2),
                                       price=round(notional / filled, 4))
            self.forensics.stage(oid, "POSITION_OPEN", now)
            self.forensics.bind_position(res.order.token_id, oid)
            info["entry_fees"] = round(sum(f.fee for f in res.fills), 5)
            if isinstance(self.engine, MomentumEngine):
                self.engine.on_entry_fill(info.get("slug", ""), res.order.token_id, now)
        on_detail = getattr(self.engine, "on_fill_detail", None)
        on_record = getattr(self.engine, "on_fill_record", None)
        on_pair_record = getattr(self.engine, "on_pair_fill_record", None)
        for f in res.fills:
            self.risk.on_fill(info.get("slug", ""), info.get("asset", ""), int(info.get("epoch", 0)),
                              info.get("side", ""), f.price * f.size, f.size, ends_entry=not info.get("pair"))
            if callable(on_detail) and not info.get("exit"):
                # an engine that tracks its legs per market (btc_hedge): price, size and fee of every buy
                on_detail(info.get("slug", ""), info.get("side", ""), f.price, f.size, f.fee, now)
            if callable(on_record) and not info.get("exit") and not info.get("pair") and not info.get("manual"):
                # the pair-lock engine's authoritative inventory: every fill with its order (id, expected price)
                on_record(info.get("slug", ""), res.order, f, now)
            elif info.get("manual") and not info.get("exit"):
                # the dashboard's Buy: into the engine's books as an external fill (not one of its own orders)
                on_ext = getattr(self.engine, "on_external_fill", None)
                if callable(on_ext):
                    on_ext(info.get("slug", ""), info.get("side", ""), float(f.size), float(f.price), float(f.fee), "BUY", now,
                           "MANUAL_BUY")
            elif callable(on_pair_record) and info.get("pair"):
                # pair-lock R: the pair bid's fill goes into the market's book too
                on_pair_record(info.get("slug", ""), res.order, f, now)
            log.info("FILL  %-26s %-4s %6.2f sh @ %.3f  fee %.4f  slip %+.3f  rtt %.0fms",
                     info.get("slug", "?"), info.get("side", ""), f.size, f.price, f.fee,
                     f.slippage, f.round_trip_ms)
            row = {
                "ts": now, "event": "fill", "action": "BUY", **info,
                "price": f.price, "size": f.size, "fee": f.fee,
                "slippage": f.slippage, "round_trip_ms": f.round_trip_ms,
                "cost": round(f.price * f.size, 4),
            }
            self._ledger_append(row)
        if res.fills and not info.get("exit") and not pair and lm is not None:
            on_leg = getattr(self.engine, "on_leg_fill", None)
            if callable(on_leg):
                q = sum(f.size for f in res.fills)
                on_leg(oid, lm.meta.market, info.get("side", ""), res.order.token_id, q,
                       sum(f.price * f.size for f in res.fills) / q, sum(f.fee for f in res.fills), now)
        if res.fills and not info.get("exit"):
            if self._slog and lm is not None:
                qty = sum(f.size for f in res.fills)
                px = sum(f.price * f.size for f in res.fills) / qty
                log.info("[POSITION] %s %s %s: %.2f sh @ %.3f (%.2f USDC) | %s", self._mkt_label(lm.meta.market),
                         info.get("side", ""), "pair filled" if pair else "open", qty, px, px * qty,
                         self._portfolio_line())
            self._persist_positions()
            if not pair and lm is not None:
                self._submit_pair(oid, info, list(res.fills), lm, now, res.order)

    def _engine_pair_cancels(self, now: float) -> None:
        """Pair orders the engine wants pulled (pair-lock R's open rule: the other side became the higher price
        before its bid filled). The venue's fill can still win the race; it is then booked like any pair fill."""
        cancels = getattr(self.engine, "pair_cancels", None)
        if not callable(cancels):
            return
        try:
            ids = cancels(now) or ()
        except Exception:                                   # a planner bug must not stop the loop
            log.exception("[PAIR] pair_cancels failed")
            return
        done = getattr(self.engine, "on_pair_done", None)
        reports = bool(ids) and bool(getattr(self.exchange, "cancel_reports_result", False))
        for oid in ids:
            try:
                self.exchange.cancel(oid, now)
            except Exception:                               # a venue fault must not stop the loop
                log.exception("[PAIR] cancel of %s failed", oid)
                continue
            if reports and oid in self._pairs_open:
                # the real venue: the order ends with its OWN result, carrying any fill that beat the cancel; the
                # engine hears of the end (and re-places a bid) only then -- never two bids working at once
                pr = self._pairs_open[oid]
                pr["cancelling"] = True
                log.info("[PAIR] %s %s pair bid %.2f sh at %.2f: cancel sent, waiting for the venue's answer", pr["slug"],
                         pr["side"], float(pr["size"]), float(pr["price"]))
                continue
            pr = self._pairs_open.pop(oid, None)
            # the paper venue drops a cancelled order without a result: the journal records the end here
            self.decisions.order_event("ORDER_CANCELLED", oid, now, reason="pulled by the engine",
                                       market=pr.get("slug") if pr else None)
            if callable(done):
                done(oid, filled=False)
            if pr is not None:
                log.info("[PAIR] %s %s pair bid %.2f sh at %.2f cancelled by the engine", pr["slug"], pr["side"],
                         float(pr["size"]), float(pr["price"]))
            self._persist_positions()
        # orders the engine asks for outside an entry fill (pair-lock R's late hedge), placed as pair orders
        requests = getattr(self.engine, "pair_requests", None)
        if not callable(requests):
            return
        if hasattr(self.engine, "guard_paused"):
            # an engine that opts in (rising_recovery) places nothing while the guard pauses entries -- its pair orders
            # used to bypass the guard: 10-09 19:23 a fix was bought 6 s after a book-feed drop, inside the cooldown
            self.engine.guard_paused = not self.guard.entries_allowed
        if hasattr(self.engine, "risk_halted"):
            # 10-13 16:16-16:33: the daily-loss halt (-23.55) stopped the signal path only; the engine's pair orders kept
            # buying (33 fills, -33 more). An engine that opts in places nothing while risk is halted.
            self.engine.risk_halted = not self.risk.halted().ok
        try:
            mkts = {slug: lm.meta.market for slug, lm in self.markets.items()}
            if getattr(self.engine, "wants_books", False):
                # an engine that quotes BEFORE a market opens (preopen_maker) needs the books and the cash as well
                books = {}
                for slug, lm in self.markets.items():
                    m = lm.meta.market
                    up = lm.books[m.yes_token_id].to_order_book() if m.yes_token_id in lm.books else None
                    dn = lm.books[m.no_token_id].to_order_book() if m.no_token_id in lm.books else None
                    books[slug] = (up if up is not None and (up.best_bid is not None or up.best_ask is not None) else None,
                                   dn if dn is not None and (dn.best_bid is not None or dn.best_ask is not None) else None)
                    if hasattr(self.engine, "set_book_age"):
                        # an engine with its own freshness gate (leader_pair) is told the feed's view of the two books'
                        # age (the stream's newest message, as quote_age_ms measures it); None = no valid book
                        ages = [self.quote_age_ms(m.yes_token_id, now), self.quote_age_ms(m.no_token_id, now)]
                        self.engine.set_book_age(slug, now, max(a for a in ages if a is not None) if all(a is not None for a in ages) else 1e18)
                asks = requests(now, mkts, books, balance=self.exchange.balance) or ()
            else:
                asks = requests(now, mkts) or ()
        except Exception:                                   # a planner bug must not stop the loop
            log.exception("[PAIR] pair_requests failed")
            return
        for slug, side, order in asks:
            lm = self.markets.get(slug)
            if lm is not None:
                self._place_pair(lm, side, order, f"engine-{slug}", now, parent=order.tag)

    def _submit_pair(self, entry_id: str, info: dict, fills: list, lm: LiveMarket, now: float,
                     entry: Order | None = None) -> None:
        """The engine's PAIR ORDER after an entry fill (triangle ``pair_cost``): a resting buy of the
        other side, working until the market closes. It does not hold the market -- no in-flight count,
        no working-entry slot -- so later signals in that market still trade while it rests."""
        make = getattr(self.engine, "pair_order", None)
        qty = sum(f.size for f in fills)
        if not callable(make) or qty <= 0.0:
            return
        if entry is not None and qty + 1e-9 < entry.size and not getattr(self.engine, "pair_after_partial", False):
            # the invariant: an other-side ask only ever follows a buy that FILLED IN FULL (an engine with
            # ``pair_after_partial`` sizes its pair to what filled)
            log.warning("[PAIR] %s %s: the buy filled only %.2f of %.2f sh, so NO other-side ask is placed",
                        lm.meta.market.slug, info.get("side", ""), qty, entry.size)
            return
        avg = sum(f.price * f.size for f in fills) / qty
        other_tok = lm.meta.market.no_token_id if info.get("side") == "UP" else lm.meta.market.yes_token_id
        live = lm.books.get(other_tok)
        ob = live.to_order_book() if live is not None else None
        made = make(lm.meta.market, info.get("side", ""), qty, avg, now, other_ask=None if ob is None else ob.best_ask)
        if made is None:
            return
        side, order = made
        self._place_pair(lm, side, order, entry_id, now,
                         parent=f"for the {info.get('side', '')} buy of {qty:.2f} sh @ {avg:.3f} that FILLED at "
                                f"{time.strftime('%H:%M:%S', time.gmtime(now / 1000.0))} ({entry_id})")

    def _engine_refused(self, ev: Evaluation, why: str) -> None:
        """Tell an engine that draws its signals (triangle) that this one was not sent, and why."""
        refused = getattr(self.engine, "entry_refused", None)
        if callable(refused):
            refused(ev, why)

    def _place_restored_pairs(self, now: float) -> None:
        """Re-place the pair orders a restart took from the paper venue, once each market's book is live."""
        drop = getattr(self.engine, "drop_pair", None)
        keep = []
        for pr in self._pairs_to_place:
            if now >= float(pr["close_ts"]):
                if callable(drop):
                    drop(pr["placeholder"])
                log.warning("[PAIR] %s %s restored pair: its market closed before it could be re-placed",
                            pr["slug"], pr["side"])
                continue
            if now < float(pr.get("not_before", 0.0)):
                keep.append(pr)                                   # backing off after a refusal
                continue
            lm = self.markets.get(pr["slug"])
            live = lm.books.get(pr["token_id"]) if lm is not None else None
            ob = live.to_order_book() if live is not None else None
            if ob is None or (ob.best_ask is None and ob.best_bid is None):
                keep.append(pr)
                continue
            if callable(drop):
                drop(pr["placeholder"])
            price = float(pr["price"])
            take = bool(pr.get("take")) or (ob.best_ask is not None and ob.best_ask <= price + 1e-9)
            if take:                                              # the ask is at or under the price: buy it now, AT the ask
                order = Order(token_id=pr["token_id"], side=Side.BUY,
                              price=round(min(price, ob.best_ask), 2) if ob.best_ask is not None else price, size=float(pr["size"]),
                              tif=TimeInForce.IOC, expected_price=ob.best_ask or price,
                              fill_at_touch=bool(getattr(self.engine, "fill_at_touch", False)),
                              tag=f"{pr['side']} triangle pair TAKEN at <= {price:.2f} (ask {ob.best_ask})")
            else:                                                 # a maker bid: post-only, resting until the close
                order = Order(token_id=pr["token_id"], side=Side.BUY, price=price, size=float(pr["size"]),
                              tif=TimeInForce.GTC, expires_ts=float(pr["close_ts"]), expected_price=price, post_only=True,
                              fill_at_touch=bool(getattr(self.engine, "fill_at_touch", False)),
                              tag=f"{pr['side']} triangle pair, re-placed")
            if self._place_pair(lm, pr["side"], order, pr.get("pair_of", ""), now) and "full_size" in pr:
                self._pairs_open[order.order_id]["full_size"] = pr["full_size"]
        self._pairs_to_place = keep
        # _place_pair persists while the restore queue still contains its placeholder;
        # write again after draining it so the saved file has each restored order once.
        if not keep:
            self._persist_positions()

    def _place_pair(self, lm: LiveMarket, side: str, order: Order, entry_id: str, now: float,
                    parent: str = "") -> bool:
        """Send a pair order (a hedge: only ``<kill>_ALL`` stops it) and keep it for restarts."""
        m = lm.meta.market
        if now >= m.close_ts:
            return False
        if self._kill_state() == "all":
            log.warning("[PAIR] %s %s pair NOT placed: kill switch %s_ALL", m.slug, side, self.kill_path)
            return False
        self.fair_by_order[order.order_id] = {
            "slug": m.slug, "asset": m.asset, "epoch": lm.epoch, "token_id": order.token_id, "side": side,
            "pair": True, "pair_of": entry_id, "expected": order.price, "secs_left": round((m.close_ts - now) / 1000.0, 1),
            "fee_rate": lm.meta.fee.rate}
        self.forensics.open(order.order_id, kind="pair", slug=m.slug, asset=m.asset, epoch=lm.epoch,
                            token_id=order.token_id, market_id=m.slug, condition_id=m.condition_id, side=side,
                            mode=self.mode_label, decision_price=order.price, limit_price=order.price,
                            quantity=round(order.size, 4), seconds_left=round((m.close_ts - now) / 1000.0, 1),
                            reason_for_entry=order.tag, strategy=self.cfg.engine.strategy,
                            strategy_version=STRATEGY_VERSION)
        self.forensics.stage(order.order_id, "DECISION", now)
        self.decisions.order_event("ORDER_CREATED", order.order_id, now, market=m.slug, side=side, price=order.price,
                                   size=round(order.size, 2), tif=order.tif.value, pair_of=entry_id)
        self.exchange.submit(order, now)
        pair_submit = getattr(self.engine, "on_pair_submit", None)
        if callable(pair_submit):
            pair_submit(order.order_id, m.slug, side, order.price, order.size)
        self._shadow_submit(order, now)
        self._dry_submit(order, now)
        self.forensics.stage(order.order_id, "ORDER_SUBMITTED", now, limit_price=order.price)
        self.decisions.order_event("ORDER_SUBMITTED", order.order_id, now, market=m.slug)
        self._pairs_open[order.order_id] = {"slug": m.slug, "side": side, "token_id": order.token_id,
                                            "price": order.price, "size": order.size, "close_ts": m.close_ts,
                                            "pair_of": entry_id, "tif": order.tif.value}
        self._persist_positions()
        if self._slog:
            if order.tif is TimeInForce.GTC:
                log.info("[ORDER] %s %s pair buy resting: %.2f sh GTC at <= %.2f until the close, %s",
                         self._mkt_label(m), side, order.size, order.price, parent or order.tag)
            else:
                log.info("[ORDER] %s %s hedge buy sent: %.2f sh FOK at <= %.2f now, %s",
                         self._mkt_label(m), side, order.size, order.price, parent or order.tag)
        return True

    # ──────────────────────────── settlement ─────────────────────────────

    #: Resolved rows vanish from Gamma within ~10 minutes, and the outcome
    #: is not always published at close+40s. Retry a few times before
    #: falling back to our own proxy, and only then give up.
    SETTLE_DELAY_MS = 40_000.0
    SETTLE_RETRY_MS = 30_000.0
    SETTLE_MAX_ATTEMPTS = 5
    #: an abandoned window is still asked for its outcome this long, this often
    LATE_RESOLVE_MS = 2 * 3600_000.0
    LATE_RETRY_MS = 60_000.0
    #: at start, abandoned windows this recent are looked up again (a closed market stays queryable)
    LATE_LOOKBACK_MS = 24 * 3600_000.0

    def _queue_ledger_late(self) -> None:
        """At start: every window the trade log shows abandoned at a mark in the last
        LATE_RESOLVE_MS and never corrected is asked for its outcome again (paper only). Its mark
        was paid into a previous run's cash, so only the realised figures and the ledger move."""
        try:
            rows = ledger_file.load(self.trade_log)
        except Exception:                                  # noqa: BLE001 -- a bad log never stops the bot
            return
        now = self.clock.now()
        done = {r.get("slug") for r in rows if r.get("event") == "settle" and r.get("resolved_late")}
        # a window resolved late by a run that did not tell the engine (before 2026-10-02): its
        # restored cycles still wait for the outcome, so hand it over from the ledger row
        holds = getattr(self.engine, "holds_window", None)
        on_settled = getattr(self.engine, "on_market_settled", None)
        if callable(holds) and callable(on_settled):
            for r in rows:
                if (r.get("event") == "settle" and r.get("resolved_late") and r.get("window_pnl") is not None
                        and holds(str(r.get("slug")))):
                    on_settled(str(r["slug"]), bool(r.get("venue_up")), float(r["window_pnl"]), now)
        for r in rows:
            if r.get("event") != "settle" or not r.get("unresolved") or r.get("slug") in done:
                continue
            ts = float(r.get("ts") or 0.0)
            if now - ts > self.LATE_LOOKBACK_MS:
                continue
            # a few tries over the next ten minutes: by now the venue has either resolved it or will not
            self._late[str(r["slug"])] = {"asset": r.get("asset", ""), "epoch": int(r.get("epoch") or 0),
                                         "marked": float(r.get("pnl") or 0.0), "marked_ts": ts, "credit": False,
                                         "stats": False, "next_ms": now, "until": now + 600_000.0}
        if self._late:
            log.info("LATE_SETTLE queued %d abandoned window(s) from the trade log: %s", len(self._late),
                     ", ".join(sorted(self._late)))
        forget = getattr(self.engine, "forget_closed_windows", None)
        if callable(forget):
            forget(now, keep=set(self._late))

    def _window_pnl_from_ledger(self, slug: str, up_won: bool) -> float | None:
        """A window's real P&L from its own fills in the trade log: the shares held at the close pay
        1 / 0, less every buy's cost and every fee, plus any sale's proceeds. None without fills."""
        try:
            fills = [r for r in ledger_file.load(self.trade_log) if r.get("slug") == slug and r.get("event") == "fill"]
        except Exception:                                  # noqa: BLE001
            return None
        if not fills:
            return None
        held = {"UP": 0.0, "DOWN": 0.0}
        cash = 0.0
        for r in fills:
            size = float(r.get("size") or 0.0)
            side = str(r.get("side") or "")
            cost = r.get("cost")
            cost = float(cost) if cost is not None else float(r.get("price") or 0.0) * size
            if side in held:
                held[side] += -size if ledger_file.is_sell(r) else size
            cash -= cost                                   # a sale is recorded with a negative cost
            cash -= float(r.get("fee") or 0.0)
        return held["UP"] * (1.0 if up_won else 0.0) + held["DOWN"] * (0.0 if up_won else 1.0) + cash

    async def _late_settle_tick(self, now: float) -> None:
        for slug, it in list(self._late.items()):
            if now < it["next_ms"]:
                continue
            if now > it["until"]:
                self._late.pop(slug, None)
                log.warning("LATE_SETTLE %s: still no venue outcome %.0f min after it was abandoned; its mark %+.2f stands",
                            slug, (now - it["marked_ts"]) / 60_000.0, it["marked"])
                continue
            it["next_ms"] = now + self.LATE_RETRY_MS
            try:
                up = await self._venue_outcome(slug)
            except FetchFailed:
                continue
            if up is None:
                continue
            self._late.pop(slug, None)
            self._book_late(slug, it, up)

    def _book_late(self, slug: str, it: dict, up_won: bool) -> None:
        """Replace an abandoned window's mark with its real outcome: the correction (real - mark) is
        booked like a settlement's P&L, and a settle row with ``resolved_late`` records it. Its ``pnl``
        is the CORRECTION, so every sum over the trade log stays right; ``window_pnl`` is the whole."""
        real = self._window_pnl_from_ledger(slug, up_won)
        held = [(t, s) for t, s in (it.get("tokens") or {}).items()
                if (p := self.exchange.positions.get(t)) is not None and abs(p.shares) > 1e-9]
        if real is None and not held:
            log.warning("LATE_SETTLE %s: resolved %s but no fills in the trade log; nothing to correct", slug,
                        "UP" if up_won else "DOWN")
            return
        now = self.clock.now()
        settled = 0.0
        for token, side in held:
            # a real account: the tokens still on our books pay out here (the venue's own redemption of them is
            # then the pending redemption this books, not drift)
            settled += self.exchange.settle_market(token, won=(str(side).upper() == ("UP" if up_won else "DOWN")))
        if real is None:
            # a position restored from before this run's trade log (a restart on fresh data, 2026-10-06 00:06: the
            # 29.46 DOWN winner stayed on the books and the guard read it as a missing position for good): the
            # window's result is what its tokens paid less what they cost
            real = settled
            log.warning("LATE_SETTLE %s: resolved %s; no fills in this run's trade log, the restored tokens settled: %+.2f",
                        slug, "UP" if up_won else "DOWN", real)
        delta = real - it["marked"]
        if it["credit"]:
            # the mark was paid into this run's cash at the abandon: the rest of the payout follows it
            self.exchange.balance += delta
        same_day = int(it["marked_ts"] // 86_400_000) == int(now // 86_400_000)
        self.risk.on_correction(delta, now / 1000.0, same_day=same_day)
        asset = it.get("asset") or ""
        if it.get("stats", True):
            self.stats.realised_pnl += delta
            self.pnl_by_asset[asset] = self.pnl_by_asset.get(asset, 0.0) + delta
            self.epoch_pnl[it["epoch"]] = self.epoch_pnl.get(it["epoch"], 0.0) + real
            self.stats.settled += 1
            if real > 0:
                self.stats.wins += 1
            else:
                self.stats.losses += 1
        lm = self.markets.get(slug)
        if lm is not None:
            lm.outcome = "UP" if up_won else "DOWN"
            lm.pnl += delta
        self.outcomes.on_settle(slug, up_won, now)
        # the abandon never told the engine the window ended: its cycles settle (and reconcile) now
        on_settled = getattr(self.engine, "on_market_settled", None)
        if callable(on_settled):
            on_settled(slug, up_won, real, now)
        doc = it.get("chart")
        if doc is not None:
            doc.update({"outcome": "UP" if up_won else "DOWN", "outcome_source": "venue (late)", "pnl": round(real, 4)})
            self._write_chart_doc(slug, doc)
        self._persist_positions()
        self._ledger_append({
            "ts": now, "event": "settle", "slug": slug, "asset": asset, "epoch": it["epoch"],
            "venue_up": bool(up_won), "resolved_late": True, "marked_pnl": round(it["marked"], 4),
            "window_pnl": round(real, 4), "pnl": round(delta, 4), "balance": round(self.exchange.balance, 4),
        })
        log.info("LATE_SETTLE %-25s %-4s  window pnl %+.2f (marked %+.2f, correction %+.2f)  balance %.2f",
                 slug, "UP" if up_won else "DOWN", real, it["marked"], delta, self.exchange.balance)

    async def settle_loop(self) -> None:
        while not self._stop.is_set():
            now = self.clock.now()
            for slug, lm in list(self.markets.items()):
                m = lm.meta.market
                if lm.settled or now < m.close_ts + self.SETTLE_DELAY_MS or now < lm.next_settle_ms:
                    continue
                if m.strike <= 0 and not self._holds_window(slug, m):
                    lm.settled = True                     # never priced: nothing to settle or archive
                    continue
                # a window found mid-way after a restart has no strike, but a position restored
                # into it still needs the venue's outcome (2026-10-01: one was reaped unsettled)
                try:
                    await self._settle(slug, lm)
                except Exception:
                    log.exception("settlement failed for %s", slug)
                    lm.settled = True
            if self._late:
                try:
                    await self._late_settle_tick(self.clock.now())
                except Exception:                         # noqa: BLE001
                    log.exception("late settlement failed")
            await asyncio.sleep(5)

    async def _settle(self, slug: str, lm: LiveMarket) -> None:
        m = lm.meta.market
        lm.settle_attempts += 1
        try:
            venue_up = await self._venue_outcome(slug)
        except FetchFailed as exc:
            venue_up = None
            self.last_discovery_error = str(exc)
        ours_up = None
        tw = self.twap[m.asset].trailing_twap(m.close_ts) if m.asset in self.twap else None
        if tw is not None and m.strike > 0:
            ours_up = tw >= m.strike                      # ties resolve Up
        # read before settle_market() moves anything, so both the resolved and
        # the unresolvable path below can tell whether we were actually in it
        had_position = any(
            abs(self.exchange.positions[t].shares) > 1e-9
            for t in (m.yes_token_id, m.no_token_id) if t in self.exchange.positions)
        if venue_up is None:
            # a REAL account keeps asking for as long as the late path would (the venue publishes the result
            # 1.5-3+ min after the close some days, 2026-10-05): abandoning left the tokens in limbo, the venue
            # then redeemed them and the guard tripped on the cash and the vanished position every market
            max_attempts = (int(self.LATE_RESOLVE_MS // self.SETTLE_RETRY_MS) if (self.is_live and had_position)
                            else self.SETTLE_MAX_ATTEMPTS)
            if lm.settle_attempts < max_attempts:
                lm.next_settle_ms = self.clock.now() + self.SETTLE_RETRY_MS
                if lm.settle_attempts <= self.SETTLE_MAX_ATTEMPTS or lm.settle_attempts % 10 == 0:
                    log.info("%s: venue outcome not available yet (attempt %d); retrying",
                             slug, lm.settle_attempts)
                return
            if ours_up is None:
                lm.settled = True
                self.stats.unresolvable += 1
                log.warning("%s: no outcome after %d attempts; positions left unsettled",
                            slug, lm.settle_attempts)
                # The window is over and we will never price it. Flatten it at
                # its last mark and hand the risk budget back -- leaving the
                # position open is what wedged the bot for twenty hours -- and
                # write the ledger row, without which the dashboard has no way
                # to learn the market ended and every fill on it reads "open".
                if had_position:
                    self._release(slug, m, lm, "no venue outcome")
                return
        lm.settled = True
        if venue_up is not None and ours_up is not None and venue_up != ours_up:
            self.stats.basis_disagreements += 1
            log.warning("%s: BASIS DISAGREEMENT venue=%s ours=%s", slug,
                        "UP" if venue_up else "DOWN", "UP" if ours_up else "DOWN")
        won_up = venue_up if venue_up is not None else ours_up

        condition_of = getattr(self.exchange, "condition_of", None)
        if condition_of is not None:                       # live: lets the venue adapter redeem
            condition_of[m.yes_token_id] = m.condition_id
            condition_of[m.no_token_id] = m.condition_id
        pnl = self.exchange.settle_market(m.yes_token_id, won=bool(won_up))
        pnl += self.exchange.settle_market(m.no_token_id, won=not won_up)
        paper_pnl = None
        if self.shadow is not None:
            paper_pnl = self.shadow.settle_market(m.yes_token_id, won=bool(won_up))
            paper_pnl += self.shadow.settle_market(m.no_token_id, won=not won_up)
        for token in (m.yes_token_id, m.no_token_id):
            entry_id = self.forensics.entry_for(token)
            if entry_id is not None and had_position:
                ts = self.clock.now()
                self.forensics.stage(entry_id, "SETTLED", ts)
                self.forensics.close(entry_id, ts, reported_pnl=pnl, pnl=round(pnl, 4),
                                     pnl_after_fees=round(pnl, 4), outcome="UP" if won_up else "DOWN",
                                     exit_price=1.0 if ((token == m.yes_token_id) == bool(won_up)) else 0.0,
                                     reason_for_exit="SETTLEMENT",
                                     paper_pnl=None if paper_pnl is None else round(paper_pnl, 4))

        doc = self._chart_doc(slug, lm, won_up, pnl, venue_up)
        if doc is not None:
            # ~500 KB of JSON per market, seven markets at once at every close: off the
            # event loop, or the book feed stalls exactly when the next window opens
            await asyncio.to_thread(self._write_chart_doc, slug, doc)
        self.outcomes.on_settle(slug, won_up, self.clock.now())
        lm.outcome = "UP" if won_up else "DOWN"
        # += so an early exit's realised PnL survives; for a market held to
        # settlement lm.pnl is still 0.0 here, so this is the old assignment
        lm.pnl += pnl
        settle_check = getattr(self.engine, "on_settle_check", None)
        if callable(settle_check):
            settle_check(slug, venue_up, ours_up, self.clock.now())
        on_settled = getattr(self.engine, "on_market_settled", None)
        if callable(on_settled):
            on_settled(slug, won_up, pnl, self.clock.now())
        if lm.submitted:
            self.stats.windows_traded += 1
        if had_position:
            self.risk.on_settle(slug, pnl, self.clock.now() / 1000.0)
            self.stats.settled += 1
            self.stats.realised_pnl += pnl
            self.pnl_by_asset[m.asset] = self.pnl_by_asset.get(m.asset, 0.0) + pnl
            self.epoch_pnl[lm.epoch] = self.epoch_pnl.get(lm.epoch, 0.0) + pnl
            if pnl > 0:
                self.stats.wins += 1
            else:
                self.stats.losses += 1
            log.info("SETTLE %-25s %-4s  pnl %+.2f  balance %.2f", slug, lm.outcome, pnl, self.exchange.balance)
            self._restored_meta.pop(slug, None)
            if self._slog:
                log.info("[POSITION] %s closed at settlement (%s): pnl %+.2f | %s", self._mkt_label(m),
                         lm.outcome, pnl, self._portfolio_line())
            self._persist_positions()
            self._ledger_append({
                "ts": self.clock.now(), "event": "settle", "slug": slug, "asset": m.asset,
                "epoch": lm.epoch, "strike": m.strike, "venue_up": venue_up, "ours_up": ours_up,
                "pnl": round(pnl, 4), "balance": round(self.exchange.balance, 4),
            })

    def _chart_doc(self, slug: str, lm: LiveMarket, won_up: bool | None, pnl: float,
                   venue_up: bool | None) -> dict | None:
        """The closed window's record for data/charts/<slug>.json, snapshotted on the loop."""
        pts = self.price_history.get(slug)
        if not pts:
            return None
        m = lm.meta.market
        fills = [r for r in self.ledger if r.get("event") == "fill" and r.get("slug") == slug]
        return {
            "slug": slug, "asset": m.asset, "strike": m.strike,
            "open_ts": m.open_ts, "close_ts": m.close_ts,
            "outcome": "UP" if won_up else "DOWN",
            "outcome_source": "venue" if venue_up is not None else "proxy",
            "pnl": round(pnl, 4), "fills": fills,
            "question": lm.meta.question, "fee": {"rate": lm.meta.fee.rate, "exponent": lm.meta.fee.exponent},
            "exchanges": list(self.exchanges),
            "points": list(pts),
            # the engine's own detections (▲ / ▼ on the spot chart), kept with the window
            "shocks": self.engine.shock_marks_for(slug) if hasattr(self.engine, "shock_marks_for") else [],
        }

    def _write_chart_doc(self, slug: str, doc: dict) -> None:
        """Serialise (orjson, ~10x json) and write; safe to run in a worker thread."""
        try:
            self.chart_dir.mkdir(parents=True, exist_ok=True)
            try:
                blob = orjson.dumps(doc, option=orjson.OPT_NON_STR_KEYS)
            except TypeError:
                blob = json.dumps(doc, separators=(",", ":")).encode()
            (self.chart_dir / f"{slug}.json").write_bytes(blob)
        except OSError:
            log.debug("could not archive chart for %s", slug)

    def _archive_chart(self, slug: str, lm: LiveMarket, won_up: bool | None, pnl: float,
                       venue_up: bool | None) -> None:
        """Write a closed window's path to data/charts/<slug>.json (the dataset), synchronously."""
        doc = self._chart_doc(slug, lm, won_up, pnl, venue_up)
        if doc is not None:
            self._write_chart_doc(slug, doc)

    async def _venue_outcome(self, slug: str) -> bool | None:
        row = await self._fetch_market(slug)
        if not row:
            # Gamma's markets?slug= stops listing a market once it is CLOSED; the same query with
            # closed=true returns it, outcome included. A window whose result was published after it
            # closed was therefore invisible here: 5 of 12 windows in a row on 2026-10-01 were
            # abandoned at their last mark although the venue had resolved them.
            row = await self._fetch_closed_market(slug)
        if not row:
            return None
        return outcome_of_row(row)

    # ───────────────────────────── reporting ─────────────────────────────

    def _append_log(self, row: dict) -> None:
        try:
            with self.trade_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            log.debug("could not append to trade log")

    def _abandon_position(self, m) -> float:
        """Flatten an unresolvable window at its last mark. Returns the PnL.

        There is no outcome to settle against, so the position is valued at the
        last price the book showed. That is the least invented number available:
        assuming a win overstates equity and assuming a total loss books a loss
        that probably did not happen. It is still an estimate, which is why the
        caller keeps it out of the evidence statistic.
        """
        total = 0.0
        if self.is_live:
            # nothing is sold here; the tokens stay in the account and settle
            # when the venue resolves. Only the risk budget is released.
            log.warning("%s: unresolved on a REAL account; tokens left in place, no PnL booked", m.slug)
            return 0.0
        for tid in (m.yes_token_id, m.no_token_id):
            pos = self.exchange.positions.get(tid)
            if pos is None or abs(pos.shares) <= EXIT_EPS:
                continue
            mark = self._mark(tid)
            mark = 0.0 if mark is None else max(0.0, min(1.0, mark))
            proceeds = pos.shares * mark
            total += proceeds - pos.cost_basis - pos.fees_paid
            self.exchange.balance += proceeds
            pos.shares = 0.0
            pos.cost_basis = 0.0
            pos.fees_paid = 0.0
        return total

    def _release(self, slug: str, m, lm: LiveMarket | None, why: str) -> None:
        """Give up on a window: flatten it, free its risk budget, say so.

        The risk budget is the part that matters. A position that is never
        popped from the risk manager consumes one of `max_concurrent_positions`
        FOREVER -- ten unresolvable windows in one ten-minute venue outage
        silently took the bot to its position cap and it bought nothing for the
        next twenty hours while looking perfectly healthy.
        """
        late = why in ("no venue outcome", "reaped unsettled")
        chart = self._chart_doc(slug, lm, None, 0.0, None) if (lm is not None and late and not self.is_live) else None
        pnl = self._abandon_position(m)
        if late:
            now = self.clock.now()
            self._late[slug] = {"asset": m.asset, "epoch": lm.epoch if lm is not None else 0, "marked": pnl,
                                "marked_ts": now, "credit": not self.is_live, "stats": True, "next_ms": now + self.LATE_RETRY_MS,
                                "until": m.close_ts + self.LATE_RESOLVE_MS, "chart": chart,
                                # a real account keeps its tokens: the late booking settles them
                                "tokens": {m.yes_token_id: "UP", m.no_token_id: "DOWN"} if self.is_live else {}}
        self.risk.on_settle(slug, pnl, self.clock.now() / 1000.0)
        self.stats.realised_pnl += pnl
        self.pnl_by_asset[m.asset] = self.pnl_by_asset.get(m.asset, 0.0) + pnl
        if lm is not None:
            lm.pnl += pnl
            lm.settled = True
        # Deliberately NOT counted in wins/losses, stats.settled or epoch_pnl:
        # a marked-to-market guess is not evidence of edge either way.
        log.warning("ABANDON %-24s %s  marked %+.2f  balance %.2f",
                    slug, why, pnl, self.exchange.balance)
        self._restored_meta.pop(slug, None)
        self._persist_positions()
        self._ledger_append({
            "ts": self.clock.now(), "event": "settle", "slug": slug,
            "asset": m.asset, "epoch": lm.epoch if lm is not None else 0,
            "unresolved": True, "reason": why, "marked": True,
            "pnl": round(pnl, 4), "balance": round(self.exchange.balance, 4),
        })

    def _ledger_append(self, row: dict) -> None:
        """Record one event to disk and to the in-memory tail the dashboard reads."""
        # Stamp who wrote it. A paper bot and a real-money bot run out of one
        # directory here and share this file, and without the tag their trades
        # read as one interleaved history.
        row.setdefault("mode", self.mode_label)
        row.setdefault("live", self.is_live)
        # ... and which strategy: spot_lag and repriced_momentum must never be read as one
        row.setdefault("strategy", getattr(getattr(getattr(self, "cfg", None), "engine", None), "strategy", ""))
        self._append_log(row)
        self.ledger.append(row)
        del self.ledger[:-200]
        if row.get("event") == "settle":
            # the run ledger is cleared on restart; the earnings record is not,
            # and it is tagged so paper money never lands in a real-money total
            earnings_log.append({
                "ts": row.get("ts"), "pnl": row.get("pnl"), "slug": row.get("slug", ""),
                "asset": row.get("asset", ""), "exited": bool(row.get("exited")),
                "mode": self.mode_label, "live": self.is_live,
                # which decision engine earned it: graduation is per strategy
                "strategy": getattr(getattr(getattr(self, "cfg", None), "engine", None), "strategy", ""),
                "deploy_mode": getattr(getattr(self, "cfg", None), "deploy_mode", ""),
                # a window flattened at an estimated mark is not evidence
                "marked": bool(row.get("marked")),
            }, path=getattr(self, "earnings_path", None))    # None -> the module default

    def _reset_trade_log(self) -> None:
        """Start each run with an empty trade log.

        A restart resets the balance to ``starting_balance``, so carrying the
        previous run's fills forward makes the dashboard's history describe PnL
        that the current run's equity does not account for. The per-window
        recordings in ``data/charts/`` are the durable research record and are
        left alone. Pass ``reset_history=False`` (``--keep-history``) to append
        across restarts instead.
        """
        try:
            self.trade_log.write_text("", encoding="utf-8")
        except OSError:
            log.debug("could not reset trade log")

    def evidence(self) -> dict:
        """Is there edge yet? Per-EPOCH realised PnL, because every asset in a
        window is the same bet. Nothing below |t| ~ 2 should be believed."""
        vals = list(self.epoch_pnl.values())
        n = len(vals)
        if n == 0:
            return {"epochs": 0}
        mean = statistics.fmean(vals)
        se = statistics.stdev(vals) / math.sqrt(n) if n > 1 else float("nan")
        return {
            "epochs": n, "mean_per_epoch": round(mean, 4),
            "se": None if n < 2 else round(se, 4),
            "t": None if n < 2 or se == 0 else round(mean / se, 2),
            "positive_epochs": sum(1 for v in vals if v > 0),
            "total": round(sum(vals), 4),
        }

    @property
    def mode_label(self) -> str:
        if not self.is_live:
            # shadow's fills are simulated too, but it is not the same run as
            # plain paper: its earnings and journals must never be summed with it
            return "shadow-paper" if getattr(self, "dry_twin", None) is not None else "live-paper"
        return "LIVE-ARMED" if getattr(self.exchange, "armed", False) else "live-dry-run"

    def snapshot(self) -> dict:
        lr = self.exchange.latency_report()
        equity = self.exchange.equity(self._mark)
        now = self.clock.now()
        eng = self.engine.snapshot()
        skips = dict(eng["rejections"])
        for k, v in self.exec_rejections.items():
            skips[k] = skips.get(k, 0) + v
        return {
            "mode": self.mode_label,
            "live": self.exchange.snapshot() if self.is_live else None,
            # the ACCOUNT as the venue reports it, at our marks: what the Equity card shows for a real account
            "account_sync": self._account_sync() if self.is_live else None,
            "shadow_account": self.dry_twin.snapshot() if self.dry_twin is not None else None,
            "account_check": self.account_check.snapshot() if self.dry_twin is not None else None,
            "started": self.stats.started,
            "uptime_s": round(time.time() - self.stats.started, 1),
            "assets": list(self.assets),
            "exchanges": list(self.exchanges),
            "balance": round(self.exchange.balance, 4),
            "equity": round(equity, 4),
            "starting_balance": self.cfg.starting_balance,
            "pnl": round(equity - self.cfg.starting_balance, 4),
            "realised_pnl": round(self.stats.realised_pnl, 4),
            "unrealised_pnl": round(equity - self.exchange.balance, 4),
            "pnl_by_asset": {a: round(v, 4) for a, v in sorted(self.pnl_by_asset.items())},
            "open_positions": {
                t: round(p.shares, 2)
                for t, p in self.exchange.positions.items() if abs(p.shares) > 1e-9
            },
            "live_markets": [
                {
                    "slug": s, "asset": lm.meta.market.asset, "strike": lm.meta.market.strike,
                    "secs_left": round(lm.meta.market.seconds_remaining(now), 1),
                    "submitted": lm.submitted, "fee_rate": lm.meta.fee.rate,
                    "status": self._market_status(lm, now),
                    "liquidity": round(lm.meta.liquidity, 0),
                    "up_shares": round(self.exchange.positions[lm.meta.market.yes_token_id].shares, 2)
                    if lm.meta.market.yes_token_id in self.exchange.positions else 0.0,
                    "down_shares": round(self.exchange.positions[lm.meta.market.no_token_id].shares, 2)
                    if lm.meta.market.no_token_id in self.exchange.positions else 0.0,
                    "decision": (eng["last"].get(s) or {}),
                }
                for s, lm in sorted(self.markets.items()) if not lm.settled
            ],
            # the Market page's position panel: shares, cost and P&L per outcome of every live market the bot holds
            # (its own key: the markets table re-renders only when live_markets changes, not at every bid tick)
            "market_positions": {s: pos for s, lm in sorted(self.markets.items())
                                 if not lm.settled and (pos := self._market_position(lm)) is not None},
            "closed_markets": [
                {
                    "slug": s, "asset": lm.meta.market.asset, "strike": lm.meta.market.strike,
                    "outcome": lm.outcome, "pnl": round(lm.pnl, 4), "traded": lm.submitted,
                }
                for s, lm in sorted(self.markets.items()) if lm.outcome is not None
            ],
            # the points themselves are in history_store files; only the counts ride here
            "history_meta": {slug: len(pts) for slug, pts in self.price_history.items() if len(pts) >= 2},
            "ledger": self.ledger[-60:],
            "stats": {
                "windows_seen": self.stats.windows_seen, "windows_traded": self.stats.windows_traded,
                "settled": self.stats.settled, "wins": self.stats.wins, "losses": self.stats.losses,
                "basis_disagreements": self.stats.basis_disagreements,
                "unresolvable": self.stats.unresolvable, "realised_pnl": round(self.stats.realised_pnl, 4),
            },
            "evidence": self.evidence(),
            "feeds": {n: st.as_dict(now) for n, st in sorted(self.feed_health.items())},
            "discovery": {
                "errors": self.discovery_errors, "last_error": self.last_discovery_error,
                "unresolved": sorted(self.registry.unresolved),
                "probe_failures": self.registry.probe_failures,
            },
            "orderflow": eng.get("orderflow", {}),
            # the pair-lock layer's cycles, counters and P&L attribution (strategy/pairlock_engine.py; else None)
            "pairlock": eng.get("pairlock"),
            # the paper-only experiments' own blocks (preopen_maker, confirmation_momentum): None for other engines
            "pom": eng.get("pom"),
            "copy": eng.get("copy"),
            "cm": {k: eng.get(k) for k in ("cm", "cm_rejects", "cm_core", "cm_lottery", "cm_model")} if "cm_rejects" in eng else None,
            # Cheapside's multi-market basket plan and trigger diagnostics.
            "cheapside": eng.get("cheapside"),
            "cheapside_config": eng.get("cheapside_config"),
            "cheapside_baskets": eng.get("cheapside_baskets"),
            "cheapside_projection": eng.get("cheapside_projection"),
            # CROSS_MARKET_ARB status (None for other engines)
            "cross_market_arb": eng.get("cross_market_arb"),
            "cross_market_arb_config": eng.get("cross_market_arb_config"),
            "cross_market_arb_plans": eng.get("cross_market_arb_plans"),
            # CROSS_MARKET_CORRELATION status (None for other engines)
            "cross_market_correlation": eng.get("cross_market_correlation"),
            "cross_market_correlation_config": eng.get("cross_market_correlation_config"),
            "cross_market_correlation_plans": eng.get("cross_market_correlation_plans"),
            "orderflow_now": {a: self._of_point(a, now) for a in self.assets},
            "risk": self.risk.snapshot(),
            "orders": {k: (round(v, 4) if isinstance(v, (int, float)) else v) for k, v in lr.items()},
            "guard": self.guard.snapshot(),
            "forensics": self.forensics.snapshot(),
            "markouts": self.markouts.summary(),
            "signal_outcomes": self.outcomes.summary(),
            "kill_switch": self._kill_state(),
            "latency_source": {"order_rtt": self.latency.order_source},
            "decisions": self.decisions.summary(),
            "order_marks": {slug: self.decisions.order_marks_for(slug) for slug in self.markets},
            "shock_marks": {slug: self.engine.shock_marks_for(slug) for slug in self.markets
                            if getattr(self.engine, "shock_marks", None) and self.engine.shock_marks.get(slug)},
            "signal_marks": {slug: self.decisions.marks_for(slug) for slug, lm in self.markets.items()
                             if not lm.settled and self.decisions.marks.get(slug)},
            "shadow": None if self.shadow is None else
            {k: (round(v, 4) if isinstance(v, (int, float)) else v) for k, v in self.shadow.latency_report().items()},
            "execution_scenario": self.cfg.execution_scenario if not self.is_live else None,
            "deploy_mode": self.cfg.deploy_mode,
            "measured_latency_ms": {k: round(v, 1) for k, v in self.latency.report().items()},
            "reaction_lag_ms": round(self.latency.reaction_lag_ms, 1),
            "loop_lag_ms": self.loop_lag_report(),
            "lagging_feeds": sorted(getattr(self.composite, "lagging", ())),
            # which engine this process runs, for the dashboard's bot picker
            "strategy": self.cfg.engine.strategy,
            "event_driven": bool(getattr(self, "_shock_driven", False)),
            "spot_lead": {"window_ms": self.composite.lead_window_ms,
                          "lag_ms": {k: round(v, 1) for k, v in self.composite.lag.items()}},
            "shock_decision_age_ms": self.shock_age_report(),
            "spot_age": {a: {"source": u[2], "age_ms": round(max(0.0, now - u[0]), 1),
                             "delay_ms": round(u[1] - u[0], 1)} for a, u in self._spot_used.items()},
            "spot_delay_from": {x: ("quote" if x in self._quote_stamped else "trade")
                                for x in self.latency.spot_by_source},
            "shock_evals": getattr(self, "shock_evals", 0),
            "fast_feed": None if self.fast is None else {
                **self.fast.snapshot(), "evals": self.fast_evals,
                "last": {a: {"dir": m.direction, "h_ms": m.horizon_ms, "z": round(m.z, 2), "bps": round(m.ret_bps, 2),
                             "x": m.exchange, "intensity": round(m.intensity, 2), "t": m.t_ms}
                         for a, m in self.last_fast_move.items()}},
            "clock_offset_ms": round(self.clock.offset_ms, 1),
            "model_vs_market": {a: g.report() for a, g in self.agreement.items()},
            "regime": eng["regime"],
            "rejections": eng["rejections"],
            "exec_rejections": dict(self.exec_rejections),
            "sigma_bps_per_sec": {a: round(v.sigma_per_sec * 1e4, 3) for a, v in self.vol.items()},
            "variance_ratio": {a: round(getattr(v, "variance_ratio", 1.0), 2) for a, v in self.vol.items()},
            "vol_annualised_pct": {a: round(v.annualised() * 100, 1) for a, v in self.vol.items()},
            "skips": skips,
            "gates": {
                "max_spot_age_ms": round(self.cfg.engine.max_spot_age_ms, 1),
                "max_book_age_ms": round(self.cfg.engine.max_book_age_ms, 1),
                "min_net_edge": self.cfg.engine.min_net_edge,
                "market_blend": self.cfg.engine.market_blend,
                "take_profit": (self.cfg.engine.take_profit_delta
                                if self.cfg.engine.take_profit_enabled else 0.0),
                "stop_loss": (self.cfg.engine.stop_loss_delta
                              if self.cfg.engine.stop_loss_enabled else 0.0),
                "trade_window_s": [self.cfg.engine.trade_window_start_s, self.cfg.engine.trade_window_end_s],
            },
            "controls": {
                "scope": self.control_scope,
                "values": current_values(self.cfg, self.exchange, **self._control_targets()),
                "restart_only": self._restart_only(),
                "applied": self.control_applied[-8:],
                "applied_at": self.control_at,
                "errors": self.control_errors[:8],
            },
            "spot": {a: round(p, 6) for a, p in self.spot.items()},
            "spot_sources": {
                a: (lambda v: None if v is None else {
                    "n": v.n_sources, "deviation_bps": round(v.deviation_bps, 2),
                    "sources": {k: round(p, 6) for k, p in v.sources.items()}})(self.composite.view(a, now))
                for a in self.assets
            },
            "exchange_counts": dict(self.composite.counts),
            "twap_coverage_s": {a: round(st.coverage_s(now), 1) for a, st in self.twap.items()},
            # the value _tick computed for the strike tracker (<= one tick old):
            # recomputing a 60 s integral per asset per snapshot was ~8 % of CPU
            "twap60": {a: (lambda v: round(v, 6) if v else None)(self._twap60.get(a)) for a in self.twap},
        }

    def _market_position(self, lm: LiveMarket) -> dict | None:
        """The Market page's position panel (the user, 2026-10-09): each token's shares, cost (fees in, net of any
        sells) and average price, its value at the best bid, and the market's P&L now (everything sold at the bids,
        before a sell's fee) and if UP / if DOWN wins. None when the bot never traded the market.

        When the book has no bid, fall back to the same mark equity uses (last mid / cost), so open P&L
        stays defined and matches portfolio = cash + marked value.
        """
        m = lm.meta.market
        out, cost, value, held = {}, 0.0, 0.0, False
        for side, tok in (("up", m.yes_token_id), ("down", m.no_token_id)):
            p = self.exchange.positions.get(tok)
            shares = float(p.shares) if p is not None else 0.0
            spent = float(p.cost_basis + p.fees_paid) if p is not None else 0.0
            held = held or abs(shares) > 1e-9 or abs(spent) > 1e-9
            live = lm.books.get(tok)
            ob = live.to_order_book() if live is not None else None
            bid = ob.best_bid if ob is not None else None
            mark = bid if bid is not None else (self._mark(tok) if abs(shares) > 1e-9 else None)
            worth = 0.0 if abs(shares) <= 1e-9 else (None if mark is None else shares * mark)
            out[side] = {"shares": round(shares, 4), "cost": round(spent, 4), "fees": round(float(p.fees_paid), 4) if p else 0.0,
                         "avg": round(spent / shares, 4) if shares > 1e-9 else None, "bid": bid,
                         "mark": None if mark is None else round(float(mark), 4),
                         "value": None if worth is None else round(worth, 4)}
            cost += spent
            value = None if value is None or worth is None else value + worth
        if not held:
            return None
        up, dn = out["up"]["shares"], out["down"]["shares"]
        return {**out, "cost": round(cost, 4), "pnl_now": None if value is None else round(value - cost, 4),
                "value": None if value is None else round(value, 4),
                "pnl_up_win": round(up - cost, 4), "pnl_down_win": round(dn - cost, 4)}

    @staticmethod
    def _market_status(lm: LiveMarket, now: float) -> str:
        m = lm.meta.market
        if m.strike > 0:
            return "closing" if m.seconds_remaining(now) < 60 else "live"
        if now < m.open_ts:
            return "pending"
        return "missed open"

    def _account_sync(self) -> dict | None:
        """The venue's cash and positions (its last read), the positions valued at our marks."""
        ex = self.exchange
        venue_pos = getattr(ex, "venue_positions_last", None) or {}
        cash = getattr(ex, "venue_balance", None)
        if cash is None:
            return None
        value, rows = 0.0, []
        for tok, size in venue_pos.items():
            mark = self._mark(tok)
            hit = self.token_index.get(tok)
            v = size * (mark if mark is not None else 0.0)
            value += v
            rows.append({"token_id": tok, "slug": hit[0] if hit else None,
                         "side": None if not hit else ("UP" if tok == hit[1].meta.market.yes_token_id else "DOWN"),
                         "shares": round(size, 4), "mark": None if mark is None else round(mark, 4), "value": round(v, 4)})
        return {"cash": round(cash, 4), "positions_value": round(value, 4), "portfolio": round(cash + value, 4),
                "positions": rows, "checked": bool((getattr(ex, "position_report", None) or {}).get("checked")),
                "reconcile_s": getattr(ex, "RECONCILE_S", None)}

    def _mark(self, token_id: str) -> float | None:
        book = self._true_book(token_id)
        if book is not None and book.mid is not None:
            self._last_mark[token_id] = book.mid
            return book.mid
        if token_id in self._last_mark:
            return self._last_mark[token_id]
        pos = self.exchange.positions.get(token_id)
        if pos is not None and abs(pos.shares) > 1e-9:
            return max(0.0, min(1.0, pos.cost_basis / pos.shares))
        return None

    async def loop_lag_loop(self, every: float = 0.05) -> None:
        """How late the event loop wakes a timer. Every spot and book message
        waits at least this long before anything reads it, so this is the
        bot's own share of the reaction lag, measured directly."""
        while not self._stop.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(every)
            self.loop_lag.append(max(0.0, (time.perf_counter() - t0 - every) * 1000.0))

    def loop_lag_report(self) -> dict[str, float]:
        v = sorted(self.loop_lag)
        if not v:
            return {"p50": 0.0, "p99": 0.0, "max": 0.0, "n": 0}
        return {"p50": round(v[len(v) // 2], 1), "p99": round(v[min(len(v) - 1, int(0.99 * len(v)))], 1),
                "max": round(v[-1], 1), "n": len(v)}

    async def clock_loop(self, every: float = 300.0) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(every)
            await self.clock.resync(samples=3)

    def _write_state(self, tmp, snap) -> None:
        """Serialise and swap in. Atomic, so a reader never sees half a file.

        orjson because this runs ten times a second and is ~8x faster than the
        stdlib on this payload; OPT_NON_STR_KEYS because epoch_pnl is keyed by
        int epoch, which json coerces to a string and orjson otherwise refuses.
        Falls back rather than losing a write if anything unexpected appears.
        """
        try:
            blob = orjson.dumps(snap, option=orjson.OPT_NON_STR_KEYS)
        except TypeError:
            blob = json.dumps(snap, separators=(",", ":")).encode()
        tmp.write_bytes(blob)
        tmp.replace(self.state_path)

    async def control_loop(self, every: float = 2.0) -> None:
        """Re-read the dashboard's tunables whenever the file changes.

        Polling a small file beats a socket here: the dashboard is not
        necessarily our parent, either process may restart independently, and
        a file that is a second stale costs nothing at a 5-minute cadence.
        """
        while not self._stop.is_set():
            try:
                if self.controls.changed():
                    self._apply_controls()
            except Exception as exc:                      # noqa: BLE001
                log.warning("control file could not be applied: %s", exc)
            try:
                self._poll_commands()
            except Exception:                             # noqa: BLE001 -- a bad command must not stop the loop
                log.exception("command file could not be applied")
            await asyncio.sleep(every)

    # ───────────────────────────── dashboard commands (the Sell button) ─────────────────────────────

    @property
    def commands_path(self) -> Path:
        """``live_trades.X.jsonl`` -> ``commands.X.jsonl`` (web/server.py request_sell writes it)."""
        name = self.trade_log.name
        if name.startswith("live_trades"):
            return self.trade_log.with_name("commands" + name[len("live_trades"):])
        return self.trade_log.with_name("commands.jsonl")

    def _poll_commands(self) -> None:
        """New lines of the command file since the last look: each is one request from the dashboard."""
        path = self.commands_path
        pos = getattr(self, "_commands_pos", None)
        try:
            size = path.stat().st_size
        except OSError:
            if pos is None:
                self._commands_pos = 0                    # no file yet: whatever appears later is new
            return
        if pos is None:
            # first look at a file that existed before this run: its lines predate the run and are NOT executed
            self._commands_pos = size
            return
        if size <= pos:
            if size < pos:
                self._commands_pos = size                 # truncated / replaced: start over
            return
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(pos)
            chunk = fh.read()
        self._commands_pos = pos + len(chunk.encode("utf-8"))
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            if not isinstance(cmd, dict):
                continue
            age_s = (self.clock.now() - float(cmd.get("ts") or 0.0)) / 1000.0
            if age_s > 60.0:
                # a request that waited (the bot was down, or the file was replayed) is not acted on a minute later
                log.warning("[COMMAND] %s %s %s ignored: %.0f s old", cmd.get("action"), cmd.get("slug"), cmd.get("side"), age_s)
                continue
            if cmd.get("action") == "sell":
                self._manual_sell(cmd, self.clock.now())
            elif cmd.get("action") == "buy":
                self._manual_buy(cmd, self.clock.now())
            else:
                log.warning("[COMMAND] unknown action %r ignored", cmd.get("action"))

    def _manual_buy(self, cmd: dict, now: float) -> None:
        """The dashboard's Buy: a fill-and-kill buy of ``shares`` of one side of one market at the best ask plus the
        slippage allowance. Booked like an entry fill (position, risk, ledger, forensics); the engine's own books are
        NOT told (its hooks expect its own orders), so an engine that reconciles per market halts new entries at that
        market's settlement -- the price of a manual trade until the engines learn to book them."""
        slug, side = str(cmd.get("slug") or ""), str(cmd.get("side") or "").upper()
        want = float(cmd.get("shares") or 0.0)
        lm = self.markets.get(slug)
        why = None
        if lm is None:
            why = "the market is not open on this bot"
        elif lm.settled or now >= lm.meta.market.close_ts:
            why = "the market has closed"
        elif side not in ("UP", "DOWN"):
            why = f"side {side!r}"
        elif self._kill_state() is not None:
            why = f"kill switch {self.kill_path}"
        if why is None:
            m = lm.meta.market
            token = m.yes_token_id if side == "UP" else m.no_token_id
            book = lm.books[token].to_order_book() if token in lm.books else None
            ask = book.best_ask if book is not None else None
            qty = math.floor(want * 100.0) / 100.0
            if ask is None:
                why = f"no {side} ask"
            elif qty < float(m.min_size or 5.0):
                why = f"{qty:g} sh is under the venue minimum {m.min_size or 5}"
            else:
                slip = float(getattr(getattr(self.engine, "scfg", None), "limit_offset", 0.02) or 0.02)
                price = min(0.99, round(ask + slip, 2))
                verdict = self.risk.check(slug, m.asset, lm.epoch, qty * price, add_to_open=True)
                if not verdict.ok:
                    why = f"risk: {verdict.detail}"
                elif qty * price > self.exchange.balance + 1e-9:
                    why = f"cost {qty * price:.2f} > cash {self.exchange.balance:.2f}"
        if why is not None:
            log.warning("[COMMAND] buy %s %s %g refused: %s", slug, side, want, why)
            self._ledger_append({"ts": now, "event": "command", "action": "buy", "slug": slug, "side": side,
                                 "shares": want, "refused": why, "by": cmd.get("by"), "id": cmd.get("id")})
            return
        fak = str(getattr(getattr(self.engine, "scfg", None), "order_type", "FOK") or "FOK").upper() == "FAK"
        order = Order(token_id=token, side=Side.BUY, price=price, size=qty, tif=TimeInForce.IOC if fak else TimeInForce.FOK,
                      expected_price=ask, tag=f"MANUAL_BUY {side} {qty:g} sh at <= {price:.2f} (dashboard, {cmd.get('by', '')})")
        self.fair_by_order[order.order_id] = {
            "slug": slug, "asset": m.asset, "epoch": lm.epoch, "token_id": token, "side": side, "manual": True,
            "kind": "MANUAL_BUY", "p_used": None, "p_analytic": None, "p_market": ask, "net_edge": 0.0, "confidence": 1.0,
            "regime": "", "secs_left": round((m.close_ts - now) / 1000.0, 1), "expected": ask, "fee_rate": lm.meta.fee.rate}
        self.forensics.open(order.order_id, kind="manual_buy", slug=slug, asset=m.asset, epoch=lm.epoch, token_id=token,
                            market_id=slug, condition_id=m.condition_id, side=side, mode=self.mode_label, decision_price=ask,
                            limit_price=price, quantity=round(qty, 4), seconds_left=round((m.close_ts - now) / 1000.0, 1),
                            reason_for_entry="MANUAL_BUY", strategy=self.cfg.engine.strategy, strategy_version=STRATEGY_VERSION)
        self.forensics.stage(order.order_id, "DECISION", now)
        self.decisions.order_event("ORDER_CREATED", order.order_id, now, market=slug, side=side, kind="MANUAL_BUY",
                                   price=order.price, size=round(order.size, 2), tif=order.tif.value)
        self.exchange.submit(order, now)
        self.risk.on_submit(slug, usdc=order.price * order.size, asset=m.asset, epoch=lm.epoch)
        self._shadow_submit(order, now)
        self._dry_submit(order, now)
        self.forensics.stage(order.order_id, "ORDER_SUBMITTED", now, limit_price=order.price)
        self.decisions.order_event("ORDER_SUBMITTED", order.order_id, now, market=slug)
        lm.submitted += 1
        lm.inflight += 1
        lm.last_entry_ms = now
        self._ledger_append({"ts": now, "event": "command", "action": "buy", "slug": slug, "side": side, "shares": qty,
                             "order_id": order.order_id, "limit": price, "by": cmd.get("by"), "id": cmd.get("id")})
        log.warning("[COMMAND] MANUAL BUY %s %s %.2f sh %s at <= %.2f (ask %.2f) from the dashboard (%s)",
                    slug, side, qty, order.tif.value, price, ask, cmd.get("by", ""))

    def _manual_sell(self, cmd: dict, now: float) -> None:
        """The dashboard's Sell: a fill-and-kill sell of ``shares`` of one side of one market at the best bid (less
        the triangle slippage allowance), for at most what is held. Booked like any exit (``_book_managed_exit``);
        the engine is told (``on_manual_sell_order`` / ``on_exit_fill``) so its own books stay reconciled."""
        slug, side = str(cmd.get("slug") or ""), str(cmd.get("side") or "").upper()
        want = float(cmd.get("shares") or 0.0)
        lm = self.markets.get(slug)
        why = None
        if lm is None:
            why = "the market is not open on this bot"
        elif lm.settled or now >= lm.meta.market.close_ts:
            why = "the market has closed"
        elif side not in ("UP", "DOWN"):
            why = f"side {side!r}"
        if why is None:
            m = lm.meta.market
            token = m.yes_token_id if side == "UP" else m.no_token_id
            held = self.exchange.positions[token].shares if token in self.exchange.positions else 0.0
            working = sum(o.size for o in self._managed_orders.values() if o.token_id == token
                          and o.state.name in ("IN_FLIGHT", "RESTING", "PARTIAL"))
            qty = math.floor(min(want, max(0.0, held - working)) * 100.0) / 100.0
            book = lm.books[token].to_order_book() if token in lm.books else None
            bid = book.best_bid if book is not None else None
            if qty <= 0.0:
                why = f"nothing to sell: holds {held:g} {side}, {working:g} already being sold"
            elif bid is None:
                why = f"no {side} bid"
            elif self._kill_state() == "all":
                why = f"kill switch {self.kill_path}_ALL"
        if why is not None:
            log.warning("[COMMAND] sell %s %s %g refused: %s", slug, side, want, why)
            self._ledger_append({"ts": now, "event": "command", "action": "sell", "slug": slug, "side": side,
                                 "shares": want, "refused": why, "by": cmd.get("by"), "id": cmd.get("id")})
            return
        slip = float(getattr(getattr(self.engine, "scfg", None), "max_slippage", 0.02) or 0.02)
        price = max(0.01, round(bid - slip, 2))
        fak = str(getattr(getattr(self.engine, "scfg", None), "order_type", "FOK") or "FOK").upper() == "FAK"
        order = Order(token_id=token, side=Side.SELL, price=price, size=qty, tif=TimeInForce.IOC if fak else TimeInForce.FOK,
                      expected_price=bid, tag=f"MANUAL_SELL {side} {qty:g} sh at >= {price:.2f} (dashboard, {cmd.get('by', '')})")
        self.fair_by_order[order.order_id] = {
            "slug": slug, "asset": m.asset, "epoch": lm.epoch, "token_id": token, "side": side, "exit": True, "managed": True,
            "kind": "MANUAL_SELL", "leg_id": cmd.get("id") or order.order_id, "no_inflight": False,
            "secs_left": round((m.close_ts - now) / 1000.0, 1)}
        self.forensics.open(order.order_id, kind="manual_sell", slug=slug, asset=m.asset, epoch=lm.epoch, token_id=token,
                            market_id=slug, condition_id=m.condition_id, side=side, mode=self.mode_label, decision_price=bid,
                            limit_price=price, quantity=round(qty, 4), seconds_left=round((m.close_ts - now) / 1000.0, 1),
                            reason_for_exit="MANUAL_SELL", strategy=self.cfg.engine.strategy, strategy_version=STRATEGY_VERSION)
        self.forensics.stage(order.order_id, "DECISION", now)
        self.decisions.order_event("ORDER_CREATED", order.order_id, now, market=slug, side=side, kind="MANUAL_SELL",
                                   price=order.price, size=round(order.size, 2), tif=order.tif.value)
        on_manual = getattr(self.engine, "on_manual_sell_order", None)
        if callable(on_manual):
            on_manual(slug, side, order.order_id, qty, now)
        self.exchange.submit(order, now)
        self._shadow_submit(order, now)
        self._dry_submit(order, now)
        self._managed_orders[order.order_id] = order
        self.forensics.stage(order.order_id, "ORDER_SUBMITTED", now, limit_price=order.price)
        self.decisions.order_event("ORDER_SUBMITTED", order.order_id, now, market=slug)
        lm.inflight += 1
        lm.last_exit_ms = now
        self._ledger_append({"ts": now, "event": "command", "action": "sell", "slug": slug, "side": side, "shares": qty,
                             "order_id": order.order_id, "limit": price, "by": cmd.get("by"), "id": cmd.get("id")})
        log.warning("[COMMAND] MANUAL SELL %s %s %.2f sh %s at >= %.2f (bid %.2f, held %.2f) from the dashboard (%s)",
                    slug, side, qty, order.tif.value, price, bid, held, cmd.get("by", ""))

    def _apply_controls(self) -> None:
        values, errors = self.controls.merged(self.control_scope)
        targets = self._control_targets()
        # an override taken away returns its value to what the bot started with,
        # instead of leaving the last applied value in place until a restart
        if getattr(self, "_control_base", None) is None:
            self._control_base = current_values(self.cfg, self.exchange, **targets)
            self._control_base["engine.max_spot_age_ms"] = self._base_spot_age
            self._control_base["engine.max_book_age_ms"] = self._base_book_age
        base = self._control_base
        dropped = getattr(self, "_control_keys", set()) - set(values)
        effective = dict(values)
        for key in dropped:
            if key in base:
                effective[key] = base[key]
        if targets["strategy"] is not None:
            # a spot-lag budget value is written straight into the risk limit it maps to
            # (the tighter of the two when both are set), so raising the budget raises
            # the limit; the real-money ceilings below still clamp it
            for skey, rkey in BUDGET_TO_RISK.items():
                if skey in values:
                    effective[rkey] = min(values[skey], values[rkey]) if rkey in values else values[skey]
                elif skey in dropped and rkey not in values and rkey in base:
                    effective[rkey] = base[rkey]
        self._control_keys = set(values)
        changed = apply_to(effective, self.cfg, self.exchange, **targets)
        if isinstance(getattr(self, "engine", None), CheapSideEngine):
            basket_on = bool(self.engine.scfg.portfolio_basket_enabled)
            self.engine.requires_market_open = basket_on
            self.engine.requires_strike = not basket_on
        if self.micro_caps is not None:
            # the dashboard steers inside the micro envelope, never out of it
            for line in self.micro_caps.enforce(self.cfg, getattr(self.exchange, "caps", None)):
                log.warning("CONTROL refused: %s", line)
                errors = [*errors, line]
        sc = targets["strategy"]
        if sc is not None and self.micro_caps is not None \
                and sc.max_buys_per_hour > self.micro_caps.max_orders_per_hour:
            line = (f"SpotLagConfig.max_buys_per_hour: {sc.max_buys_per_hour} -> "
                    f"{self.micro_caps.max_orders_per_hour} (micro-live ceiling)")
            sc.max_buys_per_hour = int(self.micro_caps.max_orders_per_hour)
            log.warning("CONTROL refused: %s", line)
            errors = [*errors, line]
        if sc is not None:
            # the shared momentum plumbing reads a mirror of the spot-lag config (entry
            # freshness, revalidation's edge bar, the exit's cross limit): rebuild it
            self.engine.mcfg = sc.momentum_mirror()
            self._reentry = bool(getattr(self.engine, "allows_reentry", False))
            self._sync_fast_feed()
        if getattr(self.engine, "budget_is_ceiling", False):
            # repriced_momentum: its own budget is a hard ceiling the shared dashboard
            # file cannot lift (that file's paper scope raises the daily loss to 40)
            ceiling = self.micro_caps.max_order_usdc if self.micro_caps is not None else None
            for line in self.engine.scfg.apply_budget(self.risk.cfg, order_ceiling=ceiling):
                log.warning("CONTROL refused: %s (%s budget)", line, self.cfg.engine.strategy)
                errors = [*errors, line]
        # the staleness gates are auto-widened from measured feed latency with
        # a floor; an operator setting them means setting that floor, or the
        # next latency report would immediately overwrite the new value
        if "engine.max_spot_age_ms" in effective:
            self._base_spot_age = effective["engine.max_spot_age_ms"]
        if "engine.max_book_age_ms" in effective:
            self._base_book_age = effective["engine.max_book_age_ms"]
        self.control_errors = errors
        if changed:
            self.control_applied = changed
            self.control_at = time.time()
            for line in changed:
                log.warning("CONTROL  %s", line)
        for problem in errors:
            log.warning("control file: %s", problem)

    def _restart_only(self) -> dict:
        """Settings fixed at start, shown read-only so the page can say a restart is due."""
        caps = getattr(self.exchange, "caps", None)
        out = {
            "mode": self.mode_label,
            "deploy_mode": getattr(self.cfg, "deploy_mode", ""),
            "strategy": self.cfg.engine.strategy,
            "assets": ",".join(self.assets) if getattr(self, "registry", None) is not None else "",
            "order_lanes": int(getattr(self.exchange, "order_lanes", 0) or 0),
            "book_skip_assets": ",".join(sorted(getattr(self, "_book_skip_assets", ()) or ())),
            "kill_file": str(self.kill_path),
        }
        if caps is not None:
            out["armed"] = bool(getattr(self.exchange, "armed", False))
        return out

    async def state_loop(self, every: float = 0.1) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        while not self._stop.is_set():
            await asyncio.sleep(every)
            snap = self.snapshot()
            try:
                await asyncio.to_thread(self._write_state, tmp, snap)
            except OSError:
                pass

    async def report_loop(self, every: float = 30.0) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(every)
            snap = self.snapshot()
            lat = snap["measured_latency_ms"]
            ev = snap["evidence"]
            log.info(
                "STATUS equity %.2f (%+.2f) | assets %s | live %d | settled %d (%dW/%dL) | "
                "orders %d filled %d | spot lag %.0fms book %.0fms rtt %.0fms | loop lag p50 %.0fms p99 %.0fms | "
                "book conn %d drops %d | evidence n=%s t=%s",
                snap["equity"], snap["pnl"], ",".join(snap["assets"]), len(snap["live_markets"]),
                snap["stats"]["settled"], snap["stats"]["wins"], snap["stats"]["losses"],
                int(snap["orders"].get("orders_submitted", 0)), int(snap["orders"].get("orders_filled", 0)),
                lat["spot_p50"], lat["book_p50"], lat["order_rtt_p50"],
                snap["loop_lag_ms"]["p50"], snap["loop_lag_ms"]["p99"],
                self._book_conn, self.feed_health["polymarket"].reconnects if "polymarket" in self.feed_health else 0,
                ev.get("epochs"), ev.get("t"),
            )
            top = sorted(snap["skips"].items(), key=lambda kv: -kv[1])[:6]
            log.info("  why not trading: %s", ", ".join(f"{k} {v}" for k, v in top) or "no evaluations yet")
            log.info("  DECISIONS %s", self.decisions.report_text())
            if snap["guard"]["tripped"]:
                log.warning("  GUARD: entries stopped: %s", snap["guard"]["tripped"])
            down = [f"{n} ({v['last_error'] or 'no messages'})" for n, v in snap["feeds"].items() if not v["connected"]]
            if down:
                log.warning("  FEEDS DOWN: %s", "; ".join(down))
            if snap["discovery"]["unresolved"]:
                log.warning("  assets unanswered by the venue: %s", ", ".join(snap["discovery"]["unresolved"]))
            for asset, rep in snap["model_vs_market"].items():
                if rep:
                    log.info("  MODEL vs MARKET %-5s bias %+.3f  mean|diff| %.3f  n=%d  sigma %.2f bps/s  regime %s",
                             asset, rep["mean_bias"], rep["mean_abs"], rep["n"],
                             snap["sigma_bps_per_sec"].get(asset, 0.0), snap["regime"].get(asset, "?"))

    # ─────────────────────────────── driver ──────────────────────────────

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.is_live:
            log.warning("%s | assets %s | bankroll $%.2f | %s",
                        self.mode_label, ", ".join(self.registry.pinned) or "ALL LISTED",
                        self.cfg.starting_balance,
                        "REAL ORDERS WILL BE POSTED" if self.mode_label == "LIVE-ARMED"
                        else "dry run: orders signed, never posted")
        else:
            log.info("live paper trading | assets %s | exchanges %s | balance $%.2f | PAPER FILLS ONLY",
                     ", ".join(self.registry.pinned) or "ALL LISTED", ", ".join(self.exchanges),
                     self.cfg.starting_balance)
        log.info("resolution is a Chainlink 60s TWAP; the composite spot is a PROXY -- watch basis_disagreements")
        await self.clock.resync()
        log.info("clock offset %+.0fms vs exchange (rtt %.0fms)%s",
                 self.clock.offset_ms, self.clock.rtt_ms,
                 "  <-- your system clock is off; consider w32tm /resync"
                 if abs(self.clock.offset_ms) > 250 else "")
        tasks = [
            asyncio.create_task(self.clock_loop()),
            asyncio.create_task(self.loop_lag_loop()),
            asyncio.create_task(self.discover()),
            asyncio.create_task(self.spot_feed()),
            asyncio.create_task(self.polymarket_feed()),
            asyncio.create_task(self.trade_loop()),
            asyncio.create_task(self.settle_loop()),
            asyncio.create_task(self.state_loop(self.cfg.state_interval_s)),
            asyncio.create_task(self.report_loop()),
            asyncio.create_task(self.control_loop()),
        ]
        if hasattr(self.exchange, "run"):
            tasks.append(asyncio.create_task(self.exchange.run(self._stop)))
        if self.dry_twin is not None:
            tasks.append(asyncio.create_task(self.dry_twin.run(self._stop)))
        try:
            await self._stop.wait()
        finally:
            if self._spot_stop is not None:
                self._spot_stop.set()
            if self._spot_task is not None:
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(self._spot_task, timeout=20.0)
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(t, timeout=15.0)
            self.history.close()

    def stop(self) -> None:
        self._stop.set()
