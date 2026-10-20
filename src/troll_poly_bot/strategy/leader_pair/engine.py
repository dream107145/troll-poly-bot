"""LEADER_PAIR's adapter to the live bot (live.py): the same hooks the other pair-order engines use.

    evaluate()            every pass, per market: records the books' ages; never a taker signal of its own
    pair_requests()       every pass: one ``MarketStrategy.on_tick`` per eligible market, its intent sent as a
                          fill-and-kill (IOC) BUY at the seen ask + max_slippage
    on_pair_submit / on_pair_done / on_pair_fill_record / on_market_settled   the order's life, the fills, the outcome
    guard_paused / risk_halted   set by live.py: the EMERGENCY STOP (a feed drop, the daily-loss / drawdown halt)

Paper only (``TPB_LP_ENABLED=1``); shadow, micro-live, and live are refused until positive cost-inclusive
out-of-sample results validate the combined strategy.
State (lots, leader, P_ref, cycle) is persisted to ``state_path`` after every fill and transition and restored at
start, so a crash or restart keeps the inventory and the cost basis.
"""
from __future__ import annotations

import collections
import json
import logging
import math
import os
from pathlib import Path

from ...features.engine import FeatureEngine
from ...risk.limits import RiskManager
from ...types import Order, Side, TimeInForce
from ..book_momentum import BookMomentumEngine, taker_fee
from ..engine import EngineConfig, Evaluation, Reason
from .config import ENABLE_ENV, STRATEGY_ID, LeaderPairConfig
from .core import Intent, MarketStrategy, Quote, State, View

log = logging.getLogger(__name__)


def _levels(ob, attr: str) -> list[tuple[float, float]]:
    return [(float(l.price), float(l.size)) for l in (getattr(ob, attr, None) or [])]


class LeaderPairEngine(BookMomentumEngine):
    feature_key = STRATEGY_ID
    budget_is_ceiling = True
    allows_reentry = True
    pair_retry = False                 # an IOC's unfilled remainder never rests; the strategy decides again next slot
    wants_books = True
    guard_paused = False
    risk_halted = False

    def __init__(self, cfg: EngineConfig, scfg: LeaderPairConfig, risk: RiskManager,
                 features: FeatureEngine | None = None, state_path: str | os.PathLike | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the leader_pair strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, LeaderPairConfig):
            raise TypeError("LeaderPairEngine needs a LeaderPairConfig")
        bad = scfg.validate()
        if bad:
            raise ValueError("leader_pair config: " + "; ".join(bad))
        super().__init__(cfg, scfg, risk, features)
        self.scfg: LeaderPairConfig = scfg
        self.risk.cfg.max_daily_loss_usdc = min(self.risk.cfg.max_daily_loss_usdc, scfg.max_daily_loss_usdc, 150.0)
        self.markets: dict[str, MarketStrategy] = {}
        self.orders: dict[str, dict] = {}            # order id -> {slug, side, kind}; kept past the order's end
        self.market_data: dict[str, dict] = {}       # slug -> {t, book_age_ms}
        self.lp_stats = collections.Counter()
        self._now = 0.0
        self._stopped_logged: str | None = None
        self.state_path = Path(state_path) if state_path else None
        self._restored = False

    # ------------------------------------------------------------------ data ages

    def set_book_age(self, slug: str, now: float, age_ms: float) -> None:
        self.market_data[slug] = {"t": now, "book_age_ms": float(age_ms)}

    def _age(self, slug: str, now: float, books=None) -> float:
        d = self.market_data.get(slug)
        if d is not None and now - d["t"] <= 3000.0:
            return float(d["book_age_ms"]) + max(0.0, now - d["t"])
        # nobody told us the feed's age lately (live.py does so each pass; evaluate() only once the strike is known):
        # fall back to the books' own venue timestamps of their last change, else stale
        ts = [float(getattr(ob, "ts", 0.0) or 0.0) for ob in (books or ()) if ob is not None]
        if ts and all(t > 0.0 for t in ts):
            return max(0.0, now - min(ts))
        return 1e18

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        m = meta.market
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=m.seconds_remaining(now), reason=None)
        self.meta_by_slug[m.slug] = meta
        self._close_ts[m.slug] = m.close_ts
        for tok, bk in ((m.yes_token_id, book_up), (m.no_token_id, book_down)):
            if bk is not None:
                self._record(tok, now, bk.best_bid, bk.best_ask)
        ages = [a for a in (quote_ages or {}).values() if a is not None]
        self.set_book_age(m.slug, now, max(ages) if ages else (book_age_ms if book_age_ms is not None else 1e18))
        ev.p_market = book_up.mid if book_up is not None else None
        ev.p_used = ev.p_market if ev.p_market is not None else 0.5
        st = self.markets[m.slug].state.value if m.slug in self.markets else "new"
        return self._reject(ev, Reason.WEAK_MOMENTUM, f"leader_pair orders go through pair_requests (market {st})")

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:      # pragma: no cover
        raise RuntimeError("leader_pair places every order through pair_requests")

    # ------------------------------------------------------------------ the pass

    def _fee_fn(self, slug: str):
        meta = self.meta_by_slug.get(slug)
        fee = getattr(meta, "fee", None)
        return fee.taker_fee_per_share if fee is not None else taker_fee

    def _market(self, m) -> MarketStrategy:
        ms = self.markets.get(m.slug)
        if ms is None:
            ms = self.markets[m.slug] = MarketStrategy(self.scfg, m.slug, float(m.open_ts), float(m.close_ts))
        return ms

    def open_capital(self, except_slug: str | None = None) -> float:
        return sum(ms.portfolio.totals.c_total for s, ms in self.markets.items()
                   if s != except_slug and not ms.portfolio.settled)

    def pending_cost(self, except_slug: str | None = None) -> float:
        return sum(
            max(0.0, o["size"] - o["filled"])
            * (o["price"] + self._fee_fn(o["slug"])(o["price"]))
            for o in self.resting.values()
            if o["slug"] != except_slug and o.get("state") != "cancelling"
        )

    def resting_cost(self, slug: str) -> float:
        """Fee-inclusive capital reserved by this market's unconfirmed orders."""
        return sum(
            max(0.0, o["size"] - o["filled"])
            * (o["price"] + self._fee_fn(slug)(o["price"]))
            for o in self.resting.values()
            if o["slug"] == slug and o.get("state") != "cancelling"
        )

    def pair_cancels(self, now: float) -> list[str]:
        """Advance state and cleanup; taker-only FAK orders end at the venue and are never rested."""
        self._now = max(self._now, now)
        if not self._restored:
            self._restore(now)
        self._cleanup(now)
        return []

    def pair_requests(self, now: float, markets: dict | None = None, books: dict | None = None,
                      balance: float | None = None) -> list[tuple[str, str, Order]]:
        sc = self.scfg
        self._now = max(self._now, now)
        if not self._restored:
            self._restore(now)
        out: list[tuple[str, str, Order]] = []
        self.risk.cfg.max_daily_loss_usdc = min(self.risk.cfg.max_daily_loss_usdc, sc.max_daily_loss_usdc, 150.0)
        stop = "guard paused (feed / execution guard)" if self.guard_paused else "risk halted" if self.risk_halted else None
        if stop != self._stopped_logged:
            if stop:
                log.warning("[LP_STOP] emergency stop: %s -- no orders until it clears", stop)
            else:
                log.info("[LP_STOP] cleared")
            self._stopped_logged = stop
        # Snapshot every eligible book before deciding any market.  The cross
        # strategy must never use a peer price from a later engine pass.
        eligible: dict[str, tuple[object, tuple]] = {}
        for slug, m in (markets or {}).items():
            if not sc.asset_ok(m.asset) or f"-updown-{sc.market_type}-" not in slug:
                continue
            if now < float(m.open_ts) or now >= float(m.close_ts) + 1000.0:
                continue
            bk = (books or {}).get(slug) or (None, None)
            eligible[slug] = (m, bk)
            for tok_, ob_ in ((m.yes_token_id, bk[0]), (m.no_token_id, bk[1])):
                if ob_ is not None:
                    self._record(tok_, now, ob_.best_bid, ob_.best_ask)
        peer_averages: dict[str, dict[str, float]] = {}
        if sc.cross_market_enabled:
            for slug, (m, _) in eligible.items():
                avgs: dict[str, float] = {}
                for side, index in (("UP", 0), ("DOWN", 1)):
                    peers = []
                    for peer_slug, (peer_m, peer_books) in eligible.items():
                        if peer_slug == slug or peer_m.asset.upper() == m.asset.upper():
                            continue
                        # Averages must be from markets resolving together.
                        if float(peer_m.open_ts) != float(m.open_ts) or float(peer_m.close_ts) != float(m.close_ts):
                            continue
                        peer_book = peer_books[index]
                        ask = getattr(peer_book, "best_ask", None) if peer_book is not None else None
                        if ask is not None and 0.0 < ask < 1.0:
                            peers.append(float(ask))
                    if len(peers) == sc.cross_peer_count:
                        avgs[side] = sum(peers) / len(peers)
                peer_averages[slug] = avgs
        for slug, (m, bk) in eligible.items():
            ms = self._market(m)
            age = self._age(slug, now, bk)
            quotes = []
            for ob in bk:
                if ob is None:
                    quotes.append(None)
                else:
                    quotes.append(Quote(ob.best_bid, ob.best_ask, _levels(ob, "bids"), _levels(ob, "asks"), age))
            meta = self.meta_by_slug.get(slug)
            v = View(now=now, open_ts=float(m.open_ts), close_ts=float(m.close_ts), up=quotes[0], down=quotes[1],
                     fee_per_share=self._fee_fn(slug), balance=balance,
                     accepting=bool(getattr(meta, "accepting_orders", True)) if meta is not None else True,
                     capital_other_markets=self.open_capital(except_slug=slug) + self.pending_cost(except_slug=slug),
                     resting_cost=self.resting_cost(slug),
                     rise={"UP": self.rise(m.yes_token_id, now), "DOWN": self.rise(m.no_token_id, now)},
                     peer_avg=peer_averages.get(slug))
            before = ms.state
            it = ms.on_tick(v)
            self._drain(ms)
            if before is not ms.state:
                self.lp_stats[f"to_{ms.state.value}"] += 1
                if ms.state is not State.MARKET_CLOSED:
                    self._persist()
            if it is None:
                continue
            if stop:
                ms.refusals[f"{it.kind}:EMERGENCY_STOP"] += 1
                self.lp_stats["emergency_stop_refusals"] += 1
                continue
            tok = m.yes_token_id if it.side == "UP" else m.no_token_id
            # Taker-only FAK execution at the approved maximum price.
            q_t = round(it.shares, 2)
            it.shares = q_t
            o = Order(token_id=tok, side=Side.BUY, price=it.limit, size=q_t, tif=TimeInForce.IOC,
                      expected_price=it.expected, decision_ts=now,
                      tag=f"{it.side} leader_pair {it.kind} {q_t:g} <= {it.limit:.2f} ({it.note})")
            ms.on_order_sent(o.order_id, it, now)
            self.orders[o.order_id] = {"slug": slug, "side": it.side, "kind": it.kind, "expected": it.expected}
            self.resting[o.order_id] = {"slug": slug, "side": it.side, "token": tok, "price": it.limit, "size": q_t,
                                        "t": now, "filled": 0.0, "state": "sent", "kind": it.kind}
            self.lp_stats[f"{it.kind}_orders"] += 1
            self.lp_stats["taker_orders"] += 1
            log.info("[LP_ORDER] %s %s %s %g <= %.2f (ask %.2f, est cost %.2f) | %s | %s", slug, it.kind, it.side, q_t,
                     it.limit, it.expected, it.approval.cost, it.note, self._line(ms))
            out.append((slug, it.side, o))
        return out

    def rise(self, token: str, now: float) -> float | None:
        """mid now - mid fix_rise_window_s ago (signed), None without that much history."""
        last = self.last_top.get(token)
        if last is None or last[1] is None or last[2] is None:
            return None
        w = self.scfg.fix_rise_window_s * 1000.0
        then_b, then_a = self._price_at(token, now - w, 1), self._price_at(token, now - w, 2)
        if then_b is None or then_a is None:
            return None
        return (last[1] + last[2]) / 2.0 - (then_b + then_a) / 2.0

    def _line(self, ms: MarketStrategy) -> str:
        t = ms.portfolio.totals
        return (f"{ms.state.value} cycle {ms.cycle} | U {t.q_up:.2f} D {t.q_down:.2f} C {t.c_total:.2f} | if UP {t.pnl_if_up:+.2f} "
                f"if DOWN {t.pnl_if_down:+.2f} | pairs {ms.portfolio.qualifying_pairs():.0f}/{ms.portfolio.formed_pairs():.0f}"
                f" | live {ms.pnl_live if ms.pnl_live is None else round(ms.pnl_live, 3)}")

    def _drain(self, ms: MarketStrategy) -> None:
        """Log the market's new events as [LP] JSON lines (the forensic trail)."""
        n = getattr(ms, "_logged", 0)
        new = ms.events[n:] if n <= len(ms.events) else ms.events
        for rec in new:
            if rec["event"] == "REFUSED" and rec.get("reason") in ("PENDING_ORDER",):
                continue
            log.info("[LP] %s", json.dumps({"market": ms.slug, **rec}, default=str))
            self.lp_stats[f"ev_{rec['event']}"] += 1
        ms._logged = len(ms.events)                           # noqa: SLF001

    # ------------------------------------------------------------------ the order's life

    def on_pair_submit(self, order_id: str, slug: str, side: str, price: float, shares: float) -> None:
        o = self.resting.get(order_id)
        if o is not None and o.get("state") == "sent":
            o["state"] = "resting"

    def drop_pair(self, order_id: str) -> None:
        self.resting.pop(order_id, None)
        info = self.orders.get(order_id)
        ms = self.markets.get(info["slug"]) if info else None
        if ms is not None:
            ms.on_order_done(order_id, filled=False, now=self._now)

    def on_pair_done(self, order_id: str, filled: bool, killed: bool = False) -> None:
        self.resting.pop(order_id, None)
        info = self.orders.get(order_id)
        ms = self.markets.get(info["slug"]) if info else None
        if ms is not None:
            ms.on_order_done(order_id, filled, now=self._now)
            self._drain(ms)
            if not filled:
                self.lp_stats[f"{info['kind']}_unfilled"] += 1

    def on_fill_detail(self, slug: str, side: str, price: float, size: float, fee: float, now: float) -> None:
        """Every order of this engine is a pair order: booked in on_pair_fill_record."""

    def on_pair_fill_record(self, slug: str, order, fill, now: float) -> None:
        oid = getattr(order, "order_id", "")
        info = self.orders.get(oid)
        ms = self.markets.get(slug)
        if ms is None:
            meta = self.meta_by_slug.get(slug)
            if meta is None:
                log.warning("[LP_FILL] %s: a fill for a market this engine never saw -- not booked", slug)
                return
            ms = self._market(meta.market)
        side = info["side"] if info else None
        if side is None:
            meta = self.meta_by_slug.get(slug)
            tok = getattr(fill, "token_id", None)
            if meta is not None and tok == meta.market.yes_token_id:
                side = "UP"
            elif meta is not None and tok == meta.market.no_token_id:
                side = "DOWN"
        if side is None:
            log.warning("[LP_FILL] %s: a fill of an unknown token -- not booked", slug)
            return
        q, px, fee = float(fill.size), float(fill.price), float(getattr(fill, "fee", 0.0) or 0.0)
        kind = info["kind"] if info else "untracked"
        o = self.resting.get(oid)
        if o is not None:
            o["filled"] += q
            if o["filled"] >= o["size"] - 1e-9:
                self.resting.pop(oid, None)
        expected = info.get("expected") if info else None
        ms.on_fill(oid, side, q, px, fee, now, kind=kind, expected_price=expected)
        self.lp_stats[f"{kind}_fills"] += 1
        self.lp_stats["taker_fills"] += 1
        self._drain(ms)
        self._persist()
        log.info("[LP_FILL] %s %s %s %g @ %.3f fee %.4f | %s", slug, kind, side, q, px, fee, self._line(ms))

    def on_external_fill(self, slug: str, side: str, size: float, price: float, fee: float, action: str, now: float,
                         tag: str = "") -> None:
        """The dashboard's manual BUY: reconciled into the market's inventory (a sell is logged, not modelled)."""
        ms = self.markets.get(slug)
        if ms is None or action != "BUY":
            log.warning("[LP_EXT] %s %s %s %g @ %.3f not booked (%s)", slug, action, side, size, price, tag)
            return
        ms.on_fill(f"ext-{tag}-{int(now)}", side, float(size), float(price), float(fee), now, kind="manual")
        self._drain(ms)
        self._persist()

    # ------------------------------------------------------------------ held to settlement

    def exit_decision(self, **kw) -> str | None:
        return None

    def on_exit(self, slug: str, token_id: str, now_ms: float) -> None:
        pass

    def holds_window(self, slug: str) -> bool:
        ms = self.markets.get(slug)
        return ms is not None and not ms.portfolio.settled and ms.portfolio.totals.purchases > 0

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        ms = self.markets.get(slug)
        if ms is None or ms.portfolio.settled:
            return
        got = ms.on_settled(won_up, now)
        self._drain(ms)
        self.lp_stats["markets_settled"] += 1
        self.lp_stats["markets_won"] += int(got > 0)
        t = ms.portfolio.totals
        log.info("[LP_SETTLED] %s %s won: P&L %+.4f (if UP %+.2f / if DOWN %+.2f; pairs %.0f qualifying of %.0f, margin %+.3f; "
                 "unmatched UP %.2f DOWN %.2f; %d cycles, %d recovery stops)", slug, "UP" if won_up else "DOWN", got,
                 t.pnl_if_up, t.pnl_if_down, ms.portfolio.qualifying_pairs(), ms.portfolio.formed_pairs(), ms.portfolio.pair_margin(),
                 t.unmatched_up, t.unmatched_down, ms.cycle, ms.recovery_stops)
        self._persist()

    def _cleanup(self, now: float) -> None:
        for slug, ms in list(self.markets.items()):
            if now > ms.close_ts + 900_000.0 and (ms.portfolio.settled or ms.portfolio.totals.purchases == 0):
                self.markets.pop(slug, None)
                self.meta_by_slug.pop(slug, None)
                self.market_data.pop(slug, None)
                for oid in [i for i, o in self.orders.items() if o["slug"] == slug]:
                    self.orders.pop(oid, None)
        for oid in [i for i, o in self.resting.items() if now > self._close_ts.get(o["slug"], now) + 60_000.0]:
            self.resting.pop(oid, None)
        self._forget_closed(now)

    # ------------------------------------------------------------------ persistence

    def _persist(self) -> None:
        if self.state_path is None:
            return
        try:
            rec = {"strategy": STRATEGY_ID, "t": self._now,
                   "markets": [ms.persist() for ms in self.markets.values() if ms.portfolio.totals.purchases > 0 or ms.state is not State.INITIAL_ENTRY]}
            tmp = self.state_path.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(rec, default=str))
            os.replace(tmp, self.state_path)
        except OSError:
            log.exception("[LP] could not persist the state")

    def _restore(self, now: float) -> None:
        self._restored = True
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            rec = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            log.exception("[LP] could not read the state file")
            return
        n = 0
        for r in rec.get("markets", []):
            try:
                ms = MarketStrategy.restore(self.scfg, r)
            except Exception:                                  # noqa: BLE001 -- one bad record never stops the bot
                log.exception("[LP] could not restore %s", r.get("slug"))
                continue
            if now > ms.close_ts + 900_000.0 and ms.portfolio.settled:
                continue
            self.markets[ms.slug] = ms
            n += 1
        if n:
            log.warning("[LP] restored %d market(s) from %s: %s", n, self.state_path,
                        ", ".join(f"{s} {m.state.value} U {m.portfolio.totals.q_up:.0f} D {m.portfolio.totals.q_down:.0f}"
                                  for s, m in self.markets.items()))

    # ------------------------------------------------------------------ dashboard

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = STRATEGY_ID
        snap.pop("book_momentum", None)
        snap["leader_pair"] = {
            "config": self.scfg.as_dict(), "stats": dict(self.lp_stats), "open_capital_usdc": round(self.open_capital(), 4),
            "emergency_stop": self._stopped_logged,
            "pending": {k: dict(v) for k, v in self.resting.items()},
            "markets": {s: {k: v for k, v in ms.summary().items() if k != "portfolio"} | {"portfolio": {k: v for k, v in ms.portfolio.snapshot().items() if k not in ("lots", "pairs")}}
                        for s, ms in self.markets.items() if ms.state is not State.INITIAL_ENTRY or ms.portfolio.totals.purchases}}
        return snap
