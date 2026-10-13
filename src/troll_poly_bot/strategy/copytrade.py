"""COPYTRADE: follow one Polymarket wallet's fills as fast as the public data allows and buy the same token
(the user, 2026-10-07: "monitor PBot-6's orders, execute the same orders with him, very quickly").

EXPERIMENTAL, PAPER ONLY. ``TPB_COPY_ENABLED=1`` is the feature flag; there is no real-money switch
(__main__.check_copytrade refuses shadow, micro-live and live). Env prefix ``TPB_COPY_``; own data tag and statistics.

What the venue publishes about another wallet is its FILLS (data-api /activity?user=&type=TRADE), a few seconds after they happen;
its resting orders are invisible. So the bot:

    poll        a thread asks the data API (activity, type TRADE) for the wallet's latest fills every ``poll_s`` seconds and queues each new
                BUY fill (transaction hash + token + size as the key) with the time it was first seen;
    copy        on the next loop tick, for each queued fill in a market this bot knows (its 5-minute markets are
                discovered minutes ahead, so pre-open fills are copyable): a FOK buy of min(his size, ``size_cap``)
                shares of the same token at his price + ``max_premium`` -- filled only if the ask is still inside;
                otherwise the copy is recorded as MISSED_PRICE. Fills older than ``max_age_s`` when seen are MISSED_LATE;
    hold        to settlement, like him (he never sells);
    record      per copy: his price/size/time, when we saw it (lag), when we sent, our fill price (slippage vs his), result.

Limits: max open exposure, max markets with positions, daily loss (the shared risk manager's), one copy per fill.
"""
from __future__ import annotations

import collections
import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass

from ..features.engine import FeatureEngine
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason
from .momentum import MomentumConfig
from .spot_lag import SpotLagConfig, SpotLagEngine

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_COPY_ENABLED"
DATA_API = "https://data-api.polymarket.com"
MISSES = ("MISSED_LATE", "MISSED_PRICE", "MISSED_MARKET", "MISSED_TYPE", "MISSED_LIMIT", "MISSED_CASH", "MISSED_SELL")


@dataclass(slots=True)
class CopyConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_COPY_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    wallet: str = "0x21d0a97aac03917e752857a551bbe5103a00e8d7"      # PBot-6
    poll_s: float = 0.7
    size_cap: float = 10.0
    size_mode: str = "match"            # "match": min(his size, size_cap); "fixed": size_cap
    max_premium: float = 0.02           # our limit = his price + this
    max_age_s: float = 20.0             # a fill seen later than this after it happened is not copied
    market_types: str = "5m"            # "5m" | "5m,15m"
    assets: str = "BTC"
    exclude_assets: str = ""
    copy_sells: bool = False            # he never sells; a SELL of his is recorded, not copied
    max_total_open_exposure_usdc: float = 40.0
    max_markets_with_open_positions: int = 4
    # --- the SpotLagEngine surface that is not used here ---
    size_shares: float = 10.0
    order_type: str = "FOK"
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0
    max_simultaneous_positions: int = 8
    max_buys_per_hour: int = 600
    max_entries_per_market: int = 50
    max_attempts_per_market: int = 50
    max_order_usdc: float = 10.0
    max_total_exposure_usdc: float = 40.0
    max_daily_loss_usdc: float = 20.0
    fast_feed: bool = False

    def momentum_mirror(self) -> MomentumConfig:
        m = SpotLagConfig.momentum_mirror(self)
        m.max_hold_s = 1e9
        return m

    def types(self) -> set[str]:
        return {t.strip() for t in self.market_types.split(",") if t.strip()}


class WalletPoller(threading.Thread):
    """Polls the data API for a wallet's trades; new ones go into ``queue`` with the time they were first seen."""

    def __init__(self, wallet: str, poll_s: float, fetch=None) -> None:
        super().__init__(daemon=True, name="copy-poller")
        self.wallet, self.poll_s = wallet, poll_s
        self.queue: collections.deque = collections.deque()
        self.seen: dict[str, float] = {}
        self.polls = self.errors = 0
        self.last_rtt_ms = 0.0
        self.last_poll_mono = 0.0
        self.last_rows = 0
        self.last_newest_ts = 0.0
        self._stop = threading.Event()
        self._fetch = fetch or self._http

    def _http(self):
        # /activity?type=TRADE is the complete, newest-first fill feed; /trades?user= returns a sparse history (10-07: 50 of
        # the last hour's fills vs 1)
        req = urllib.request.Request(f"{DATA_API}/activity?user={self.wallet}&limit=40&type=TRADE", headers={"User-Agent": "tpb-copy"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.load(r)

    def ingest(self, rows, seen_at: float) -> int:
        """Rows from the API (newest first). Returns how many were new. The first poll only primes ``seen``."""
        new = 0
        prime = not self.seen
        for x in rows or ():
            key = f"{x.get('transactionHash') or ''}|{x.get('asset')}|{x.get('size')}|{x.get('timestamp')}"
            if key in self.seen:
                continue
            self.seen[key] = seen_at
            if prime:
                continue
            new += 1
            self.queue.append({"key": key, "seen_at": seen_at, "ts": float(x.get("timestamp") or 0.0), "side": str(x.get("side") or "").upper(),
                               "token": str(x.get("asset") or ""), "size": float(x.get("size") or 0.0), "price": float(x.get("price") or 0.0),
                               "outcome": str(x.get("outcome") or "").upper(), "slug": str(x.get("slug") or ""), "tx": x.get("transactionHash")})
        if len(self.seen) > 5000:
            for k in list(self.seen)[:2500]:
                self.seen.pop(k, None)
        return new

    def run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                rows = self._fetch()
                self.polls += 1
                self.last_rtt_ms = (time.monotonic() - t0) * 1000.0
                self.last_rows = len(rows) if isinstance(rows, list) else -1
                self.last_newest_ts = max((float(x.get("timestamp") or 0) for x in rows), default=0.0) if isinstance(rows, list) else 0.0
                n = self.ingest(rows, time.time())
                if self.polls % 60 == 1 or n:
                    log.info("[COPY_POLL] poll %d: %s rows, newest %s, %d new, seen %d, queue %d, rtt %.0f ms", self.polls,
                             self.last_rows, time.strftime("%H:%M:%S", time.gmtime(self.last_newest_ts)) if self.last_newest_ts else "-",
                             n, len(self.seen), len(self.queue), self.last_rtt_ms)
            except Exception as exc:                        # noqa: BLE001
                self.errors += 1
                if self.errors % 20 == 1:
                    log.warning("[COPY] poll failed: %s", str(exc)[:120])
            self.last_poll_mono = time.monotonic()
            self._stop.wait(max(0.1, self.poll_s - (time.monotonic() - t0)))

    def stop(self) -> None:
        self._stop.set()


class CopyTradeEngine(SpotLagEngine):
    """No signals of its own: the followed wallet's fills are the signal. Orders go through live.py's pair-order
    hooks (FOK buys), so they work before a market opens."""

    feature_key = "copy"
    budget_is_ceiling = True
    stacks_positions = True
    wants_books = True

    def __init__(self, cfg: EngineConfig, scfg: CopyConfig, risk: RiskManager, features: FeatureEngine | None = None,
                 poller: WalletPoller | None = None, start_poller: bool = True) -> None:
        if not scfg.enabled:
            raise ValueError(f"the copytrade strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, CopyConfig):
            raise TypeError("CopyTradeEngine needs a CopyConfig")
        super().__init__(cfg, scfg, risk, features)
        self.poller = poller or WalletPoller(scfg.wallet, scfg.poll_s)
        if start_poller and not self.poller.is_alive():
            self.poller.start()
        self.copies: dict[str, dict] = {}            # our order id -> copy record
        self.by_market: dict[str, dict] = {}         # slug -> {"filled": {...}, "cost": ..., "his": [...]}
        self.misses: collections.Counter = collections.Counter()
        self.stats = {"his_fills_seen": 0, "copies_sent": 0, "copies_filled": 0, "copies_killed": 0, "settled": 0, "pnl": 0.0,
                      "lag_s_mean": None, "slippage_mean": None, "his_sells_seen": 0}
        self._lags: collections.deque = collections.deque(maxlen=200)
        self._slips: collections.deque = collections.deque(maxlen=200)
        self.last_copies: collections.deque = collections.deque(maxlen=50)

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        ev = Evaluation(slug=meta.market.slug, asset=meta.market.asset, seconds_left=meta.market.seconds_remaining(now), reason=None)
        return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW, "copytrade: no signals of its own")

    def exit_decision(self, **kw) -> str | None:
        return None

    # ------------------------------------------------------------- the hook

    def _inventory(self) -> tuple[float, int]:
        cost = 0.0
        open_m = 0
        for rec in self.by_market.values():
            if rec.get("settled"):
                continue
            c = sum(rec["cost"].values())
            cost += c
            if c > 1e-9:
                open_m += 1
        return cost, open_m

    def pair_requests(self, now: float, markets: dict | None = None, books: dict | None = None,
                      balance: float | None = None) -> list[tuple[str, str, Order]]:
        sc = self.scfg
        out = []
        slug_by_token = {}
        for slug, m in (markets or {}).items():
            slug_by_token[m.yes_token_id] = (slug, "UP", m)
            slug_by_token[m.no_token_id] = (slug, "DOWN", m)
        while self.poller.queue:
            f = self.poller.queue.popleft()
            self.stats["his_fills_seen"] += 1
            lag = f["seen_at"] - f["ts"]
            self._lags.append(lag)
            self.stats["lag_s_mean"] = round(sum(self._lags) / len(self._lags), 2)
            rec = {"his": f, "lag_s": round(lag, 2), "t_seen": f["seen_at"], "copy": None}
            if f["side"] != "BUY":
                self.stats["his_sells_seen"] += 1
                rec["copy"] = "MISSED_SELL"
                self.misses["MISSED_SELL"] += 1
                self.last_copies.append(rec)
                continue
            hit = slug_by_token.get(f["token"])
            if hit is None:
                # a market we do not track: a 15m/hourly/other-asset fill is a TYPE miss (by design), a BTC 5m one a MARKET miss
                hs = f.get("slug") or ""
                htype = hs.split("-updown-")[1].split("-")[0] if "-updown-" in hs else ""
                hasset = hs.split("-updown-")[0].upper() if "-updown-" in hs else ""
                miss = "MISSED_MARKET" if (htype in sc.types() and sc.asset_ok(hasset)) else "MISSED_TYPE"
                rec["copy"] = miss
                self.misses[miss] += 1
                self.last_copies.append(rec)
                continue
            slug, side, m = hit
            mtype = slug.split("-updown-")[1].split("-")[0] if "-updown-" in slug else ""
            if mtype not in sc.types() or not sc.asset_ok(m.asset):
                rec["copy"] = "MISSED_TYPE"
                self.misses["MISSED_TYPE"] += 1
                self.last_copies.append(rec)
                continue
            if lag > sc.max_age_s or now / 1000.0 - f["ts"] > sc.max_age_s or now >= m.close_ts:
                rec["copy"] = "MISSED_LATE"
                self.misses["MISSED_LATE"] += 1
                self.last_copies.append(rec)
                continue
            size = min(f["size"], sc.size_cap) if sc.size_mode == "match" else sc.size_cap
            size = max(float(m.min_size or 5.0), round(size, 2))
            limit = round(min(0.99, f["price"] + sc.max_premium), 2)
            cost, open_m = self._inventory()
            if cost + size * limit > sc.max_total_open_exposure_usdc + 1e-9 or (slug not in self.by_market and open_m >= int(sc.max_markets_with_open_positions)):
                rec["copy"] = "MISSED_LIMIT"
                self.misses["MISSED_LIMIT"] += 1
                self.last_copies.append(rec)
                continue
            if balance is not None and size * limit > balance + 1e-9:
                rec["copy"] = "MISSED_CASH"
                self.misses["MISSED_CASH"] += 1
                self.last_copies.append(rec)
                continue
            bk = (books or {}).get(slug) or (None, None)
            book = bk[0] if side == "UP" else bk[1]
            ask_now = float(book.best_ask) if book is not None and book.best_ask is not None else None
            if ask_now is not None and ask_now > limit + 1e-9:
                rec["copy"] = "MISSED_PRICE"
                rec["ask_at_decision"] = ask_now
                self.misses["MISSED_PRICE"] += 1
                self.last_copies.append(rec)
                log.info("[COPY] %s %s: his %g @ %.2f, ask now %.2f > limit %.2f: not copied", slug, side, f["size"], f["price"], ask_now, limit)
                continue
            o = Order(token_id=f["token"], side=Side.BUY, price=limit, size=size, tif=TimeInForce.FOK, expected_price=ask_now if ask_now else limit,
                      tag=f"{side} copy of {sc.wallet[:8]} {f['size']:g} @ {f['price']:.2f} seen {lag:.1f} s late")
            rec.update({"copy": "SENT", "order_id": None, "slug": slug, "side": side, "size": size, "limit": limit, "ask_at_decision": ask_now, "t_sent": now})
            self.copies[o.order_id] = rec
            mk = self.by_market.setdefault(slug, {"slug": slug, "filled": {"UP": 0.0, "DOWN": 0.0}, "cost": {"UP": 0.0, "DOWN": 0.0}, "fees": 0.0, "his": [], "settled": False, "close_ts": m.close_ts})
            mk["his"].append({"side": side, "size": f["size"], "price": f["price"], "ts": f["ts"]})
            self.stats["copies_sent"] += 1
            self.last_copies.append(rec)
            log.info("[COPY] %s %s: his %g @ %.2f (seen %.1f s late) -> our FOK %g @ <= %.2f, ask %s", slug, side, f["size"], f["price"], lag, size, limit,
                     "n/a" if ask_now is None else f"{ask_now:.2f}")
            out.append((slug, side, o))
        return out

    def pair_cancels(self, now: float) -> list[str]:
        """Nothing to pull (copies are FOK), but live.py reaches pair_requests only through this hook."""
        return []

    def on_pair_submit(self, order_id: str, slug: str, side: str, price: float, shares: float) -> None:
        # live.py gives the order its id before this call: find our record by the pending slug/side/limit
        for oid, rec in list(self.copies.items()):
            if rec.get("order_id") is None and rec.get("slug") == slug and rec.get("side") == side and abs(rec.get("limit", 0) - price) < 1e-9:
                rec["order_id"] = order_id
                if oid != order_id:
                    self.copies[order_id] = rec
                    self.copies.pop(oid, None)
                break

    def drop_pair(self, order_id: str) -> None:
        self.copies.pop(order_id, None)

    def on_pair_fill_record(self, slug: str, order, fill, now: float) -> None:
        rec = self.copies.get(getattr(order, "order_id", ""))
        mk = self.by_market.get(slug)
        if rec is None or mk is None:
            return
        q, px, fee = float(fill.size), float(fill.price), float(getattr(fill, "fee", 0.0) or 0.0)
        side = rec["side"]
        mk["filled"][side] += q
        mk["cost"][side] += q * px
        mk["fees"] += fee
        rec.update({"copy": "FILLED", "fill_price": px, "fill_size": q, "fee": fee, "t_fill": now, "slippage_vs_his": round(px - rec["his"]["price"], 4)})
        self._slips.append(px - rec["his"]["price"])
        self.stats["slippage_mean"] = round(sum(self._slips) / len(self._slips), 4)
        self.stats["copies_filled"] += 1
        log.info("[COPY_FILL] %s %s %g @ %.2f (his %.2f, +%.2f) %.1f s after his fill", slug, side, q, px, rec["his"]["price"], px - rec["his"]["price"],
                 now / 1000.0 - rec["his"]["ts"])

    def on_pair_done(self, order_id: str, filled: bool, killed: bool = False) -> None:
        rec = self.copies.get(order_id)
        if rec is None or filled:
            return
        if rec.get("copy") == "SENT":
            rec["copy"] = "KILLED"
            self.stats["copies_killed"] += 1
            self.misses["MISSED_PRICE"] += 1

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        mk = self.by_market.get(slug)
        if mk is None:
            return
        mk["settled"] = True
        winner = "UP" if won_up else "DOWN"
        u, d = mk["filled"]["UP"], mk["filled"]["DOWN"]
        realised = (u if won_up else d) - mk["cost"]["UP"] - mk["cost"]["DOWN"] - mk["fees"]
        mk["pnl"] = realised
        mk["winner"] = winner
        if u > 1e-9 or d > 1e-9:
            self.stats["settled"] += 1
            self.stats["pnl"] += realised
            his_cost = sum(h["size"] * h["price"] for h in mk["his"])
            his_pay = sum(h["size"] for h in mk["his"] if h["side"] == winner)
            log.warning("[COPY_SETTLED] %s winner %s: ours UP %.1f DOWN %.1f -> %+.3f | his copied legs %+.2f on %.0f USDC", slug, winner, u, d, realised,
                        his_pay - his_cost, his_cost)
        for s in [s for s, r in self.by_market.items() if r.get("settled") and now - r["close_ts"] > 3_600_000.0]:
            self.by_market.pop(s, None)

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = "copytrade"
        snap["copy_config"] = self.scfg.as_dict()
        snap["copy"] = {"stats": dict(self.stats), "misses": {k: self.misses.get(k, 0) for k in MISSES},
                        "poller": {"polls": self.poller.polls, "errors": self.poller.errors, "rtt_ms": round(self.poller.last_rtt_ms, 1),
                                   "alive": self.poller.is_alive(), "last_rows": self.poller.last_rows, "queue": len(self.poller.queue),
                                   "seen": len(self.poller.seen), "newest_ts": self.poller.last_newest_ts},
                        "open_markets": [{"slug": r["slug"], "filled": r["filled"], "cost": r["cost"]} for r in self.by_market.values() if not r.get("settled")],
                        "last_copies": [{k: v for k, v in c.items() if k != "his"} | {"his": c["his"]} for c in list(self.last_copies)[-20:]]}
        return snap
