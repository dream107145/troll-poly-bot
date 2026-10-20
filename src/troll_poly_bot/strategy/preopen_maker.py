"""PREOPEN_MAKER: resting GTC bids on BOTH tokens of the NEXT BTC 5-minute market, placed about 45 s before it
opens, held to settlement -- the pattern of wallet PBot-6 (0x21d0a97a...), the user's brief of 2026-10-06.

EXPERIMENTAL, PAPER ONLY. ``TPB_POM_ENABLED=1`` is the feature flag and there is no real-money switch:
__main__.check_preopen_maker refuses shadow, micro-live and live unconditionally. Env prefix ``TPB_POM_``; own data
tag, budget and statistics. No other strategy is touched.

What it does, per market, through live.py's existing resting-order ("pair") machinery:

    T - placement_s   the market is known (discovered, books subscribed, not yet open): place a GTC bid for
                      ``quantity`` shares of UP at ``bid_price`` and the same for DOWN, if the inventory and cash
                      limits allow; otherwise record why not. Never a taker order, never a re-price, never a chase.
    T + cancel_after_s  pull whatever is still resting (default: at the open).
    fills             booked by live.py as pair fills (maker: no fee); the market is classified NO_FILLS / UP_ONLY /
                      DOWN_ONLY / BOTH / PARTIAL_UP / PARTIAL_DOWN / PARTIAL_BOTH; every fill carries the book at
                      placement and at fill, the BTC spot at both, the previous market's settlement value and this
                      market's strike (known only at the open), the resolution and the realised P&L.
    settlement        hold everything; a BOTH market pays 1.00 per paired share whatever happens.

Inventory limits (section "inventory management"): max total open exposure, max unmatched shares and USDC, max
markets with open positions. At a limit no new bids are placed and the reason is counted. There is no recovery.

The pure functions ``classify``, ``pair_economics`` and ``one_sided_ev`` are shared with the replay
(scripts/research_preopen_maker.py).
"""
from __future__ import annotations

import collections
import json
import logging
import math
from dataclasses import dataclass

from ..features.engine import FeatureEngine
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason
from .momentum import MomentumConfig
from .spot_lag import SpotLagConfig, SpotLagEngine

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_POM_ENABLED"
CLASSES = ("NO_FILLS", "UP_ONLY", "DOWN_ONLY", "BOTH", "PARTIAL_UP", "PARTIAL_DOWN", "PARTIAL_BOTH")
SKIPS = ("MAX_TOTAL_OPEN_EXPOSURE", "MAX_UNMATCHED_SHARES", "MAX_UNMATCHED_USDC", "MAX_MARKETS_WITH_OPEN_POSITIONS",
         "INSUFFICIENT_CASH", "NO_BOOK", "ALREADY_PLACED", "TOO_LATE", "MARKET_TYPE")


def classify(up_filled: float, down_filled: float, quantity: float, eps: float = 1e-6) -> str:
    """The brief's A-G classes from the filled quantities of each side."""
    u_full, d_full = up_filled >= quantity - eps, down_filled >= quantity - eps
    u_some, d_some = up_filled > eps, down_filled > eps
    if not u_some and not d_some:
        return "NO_FILLS"
    if u_some and d_some:
        return "BOTH" if u_full and d_full else "PARTIAL_BOTH"
    if u_some:
        return "UP_ONLY" if u_full else "PARTIAL_UP"
    return "DOWN_ONLY" if d_full else "PARTIAL_DOWN"


def pair_economics(up_qty: float, up_cost: float, down_qty: float, down_cost: float, fees: float = 0.0) -> dict:
    """The paired part of a market: min(UP, DOWN) shares pay 1.00 each whatever happens."""
    paired = min(up_qty, down_qty)
    if paired <= 1e-9:
        return {"paired": 0.0, "combined_cost_per_share": None, "guaranteed_gross": 0.0, "fees": fees, "net_guaranteed": -fees,
                "capital_locked": up_cost + down_cost}
    cps = up_cost / up_qty + down_cost / down_qty
    gross = paired * 1.0
    return {"paired": paired, "combined_cost_per_share": cps, "guaranteed_gross": gross, "fees": fees,
            "net_guaranteed": gross - paired * cps - fees, "capital_locked": up_cost + down_cost}


def one_sided_ev(p_win: float, price: float, fee_per_share: float = 0.0, other_costs: float = 0.0) -> float:
    """EV per share of a one-sided fill at ``price`` with win probability ``p_win``."""
    return p_win * (1.0 - price) - (1.0 - p_win) * price - fee_per_share - other_costs


@dataclass(slots=True)
class POMConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_POM_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    # --- the bids ---
    bid_price: float = 0.47
    bid_price_up: float = 0.0            # 0 = bid_price (asymmetric bids are a later experiment)
    bid_price_down: float = 0.0
    quantity: float = 10.0
    placement_s: float = 45.0            # seconds before the open
    cancel_after_s: float = 0.0          # seconds after the open at which unfilled bids are pulled (0 = at the open)
    market_type: str = "5m"              # "5m" | "15m" (slug family)
    min_pair_edge: float = 0.0           # informational: a BOTH market's combined cost must be <= 1 - this to count as a pair
    # --- inventory ---
    max_total_open_exposure_usdc: float = 40.0
    max_unmatched_shares: float = 40.0
    max_unmatched_usdc: float = 20.0
    max_markets_with_open_positions: int = 4
    # --- the SpotLagEngine surface that is not used here ---
    size_shares: float = 10.0
    size_mode: str = "fixed"
    order_type: str = "GTC"
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0
    assets: str = "BTC"
    exclude_assets: str = ""
    max_simultaneous_positions: int = 8
    max_buys_per_hour: int = 400
    max_entries_per_market: int = 2
    max_attempts_per_market: int = 2
    max_order_usdc: float = 5.0
    max_total_exposure_usdc: float = 40.0
    max_daily_loss_usdc: float = 20.0
    fast_feed: bool = False

    def momentum_mirror(self) -> MomentumConfig:
        m = SpotLagConfig.momentum_mirror(self)
        m.max_hold_s = 1e9
        return m

    def price_for(self, side: str) -> float:
        p = self.bid_price_up if side == "UP" else self.bid_price_down
        return round(p if p > 0 else self.bid_price, 2)


class PreopenMakerEngine(SpotLagEngine):
    """No taker entries at all (``evaluate`` only records quotes); the bids go through live.py's pair-order hooks."""

    feature_key = "pom"
    budget_is_ceiling = True
    stacks_positions = True
    #: The restart restore path deduplicates legacy duplicate GTC pairs for this engine.
    one_pair_per_market_side = True
    #: Penny orders should fill in paper when the ask reaches exactly $0.01.
    fill_at_touch = True
    #: live.py passes the markets' books to pair_requests (both tokens) when this is set
    wants_books = True

    def __init__(self, cfg: EngineConfig, scfg: POMConfig, risk: RiskManager, features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the preopen_maker strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, POMConfig):
            raise TypeError("PreopenMakerEngine needs a POMConfig")
        super().__init__(cfg, scfg, risk, features)
        #: slug -> market record (orders, fills, books, classification, result)
        self.markets: dict[str, dict] = {}
        self._by_order: dict[str, tuple[str, str]] = {}
        self.skips: collections.Counter = collections.Counter()
        self.classes: collections.Counter = collections.Counter()
        self.stats = {"markets_bid": 0, "orders": 0, "fills": 0, "partial_fills": 0, "cancelled_unfilled": 0,
                      "settled": 0, "pnl": 0.0, "pair_pnl": 0.0, "one_sided_pnl": 0.0, "pairs": 0, "one_sided": 0,
                      "one_sided_won": 0, "fees": 0.0, "capital_locked_max": 0.0}
        self.last_settled: collections.deque = collections.deque(maxlen=100)
        self._spot: tuple[float, float] | None = None          # (ts, price) of the last composite spot seen
        self._prev_settle: dict[int, float] = {}                 # market epoch -> the previous market's settlement value (its strike)

    def persist_state(self) -> dict:
        """Persist market/order records so a restart cannot quote a market twice."""
        return {
            "markets": self.markets,
            "prev_settle": self._prev_settle,
            "stats": dict(self.stats),
            "classes": dict(self.classes),
            "skips": dict(self.skips),
            "last_settled": list(self.last_settled),
        }

    def restore_state(self, doc: dict | None, now: float) -> None:
        doc = doc or {}
        self.markets = {str(slug): rec for slug, rec in (doc.get("markets") or {}).items()}
        self._prev_settle = {int(epoch): float(value) for epoch, value in (doc.get("prev_settle") or {}).items()}
        self.stats.update(doc.get("stats") or {})
        self.classes.update(doc.get("classes") or {})
        self.skips.update(doc.get("skips") or {})
        self.last_settled.extend(doc.get("last_settled") or [])
        self._by_order = {
            str(order["order_id"]): (slug, side)
            for slug, rec in self.markets.items()
            for side, order in (rec.get("orders") or {}).items()
            if order.get("order_id")
        }

        # Older versions saved held positions and GTC orders but no engine state. Seed
        # those markets as already quoted, using the restored position as the fill record.
        for slug, pos in self.risk.open.items():
            if slug in self.markets or pos.side not in ("UP", "DOWN"):
                continue
            self.markets[slug] = {
                "slug": slug, "open_ts": pos.epoch * 1000.0,
                "close_ts": (pos.epoch + (900 if self.scfg.market_type == "15m" else 300)) * 1000.0,
                "placed": True, "skip": None, "t_placed": now, "orders": {},
                "filled": {"UP": pos.shares if pos.side == "UP" else 0.0,
                           "DOWN": pos.shares if pos.side == "DOWN" else 0.0},
                "cost": {"UP": pos.usdc if pos.side == "UP" else 0.0,
                         "DOWN": pos.usdc if pos.side == "DOWN" else 0.0},
                "fees": 0.0, "fills": [], "strike": None, "prev_settlement": None,
                "settled": False, "path": [],
            }

    # ------------------------------------------------------------ the SpotLagEngine surface

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        """Markets that are open: nothing is bought here. The quotes are noted for the fill-to-outcome record."""
        m = meta.market
        if spot is not None:
            self._spot = (now, float(spot.price))
        rec = self.markets.get(m.slug)
        if rec is not None and book_up is not None and book_up.mid is not None:
            rec.setdefault("path", []).append((round((now - m.open_ts) / 1000.0, 1), round(float(book_up.mid), 3)))
            if m.strike > 0 and rec.get("strike") is None:
                rec["strike"] = m.strike
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=m.seconds_remaining(now), reason=None)
        return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW, "preopen maker: no taker entries")

    def exit_decision(self, **kw) -> str | None:
        return None

    # ------------------------------------------------------------ inventory / capital

    def _open_inventory(self) -> dict:
        up = dn = cost = 0.0
        open_markets = 0
        resting = 0.0
        for rec in self.markets.values():
            if rec.get("settled"):
                continue
            u, d = rec["filled"]["UP"], rec["filled"]["DOWN"]
            if u > 1e-9 or d > 1e-9:
                open_markets += 1
            up += u
            dn += d
            cost += rec["cost"]["UP"] + rec["cost"]["DOWN"]
            for side in ("UP", "DOWN"):
                o = rec["orders"].get(side)
                if o and o.get("state") == "resting":
                    resting += (o["size"] - rec["filled"][side]) * o["price"]
        unmatched = abs(up - dn)
        return {"up": up, "down": dn, "unmatched_shares": unmatched, "unmatched_usdc": unmatched * self.scfg.bid_price,
                "exposure_usdc": cost, "resting_usdc": resting, "open_markets": open_markets}

    def _limit_reason(self, inv: dict, new_cost: float, balance: float | None) -> str | None:
        sc = self.scfg
        if inv["exposure_usdc"] + inv["resting_usdc"] + new_cost > sc.max_total_open_exposure_usdc + 1e-9:
            return "MAX_TOTAL_OPEN_EXPOSURE"
        if inv["unmatched_shares"] > sc.max_unmatched_shares + 1e-9:
            return "MAX_UNMATCHED_SHARES"
        if inv["unmatched_usdc"] > sc.max_unmatched_usdc + 1e-9:
            return "MAX_UNMATCHED_USDC"
        if inv["open_markets"] >= int(sc.max_markets_with_open_positions):
            return "MAX_MARKETS_WITH_OPEN_POSITIONS"
        if balance is not None and new_cost > balance - inv["resting_usdc"] + 1e-9:
            return "INSUFFICIENT_CASH"
        return None

    # ------------------------------------------------------------ the live.py hooks

    def pair_requests(self, now: float, markets: dict | None = None, books: dict | None = None,
                      balance: float | None = None) -> list[tuple[str, str, Order]]:
        """Called every tick. For each market of the configured type that opens within placement_s and has no bids
        yet: two GTC bids, if the limits allow."""
        sc = self.scfg
        out: list[tuple[str, str, Order]] = []
        for slug, m in (markets or {}).items():
            if slug in self.markets or m.asset.upper() not in {a.strip().upper() for a in sc.assets.split(",") if a.strip()}:
                continue
            if f"-updown-{sc.market_type}-" not in slug:
                self.skips["MARKET_TYPE"] += 0        # other families are simply not ours
                continue
            lead = (m.open_ts - now) / 1000.0
            if lead > sc.placement_s:
                continue
            if lead < -sc.cancel_after_s - 0.5:
                # discovered too late for this market
                self.skips["TOO_LATE"] += 1
                self.markets[slug] = self._new_record(m, now, placed=False, why="TOO_LATE")
                continue
            bk = (books or {}).get(slug) or (None, None)
            up_b, dn_b = bk if isinstance(bk, tuple) else (None, None)
            if up_b is None or dn_b is None:
                self.skips["NO_BOOK"] += 1
                continue                                    # try again next tick while there is time
            inv = self._open_inventory()
            new_cost = sc.quantity * (sc.price_for("UP") + sc.price_for("DOWN"))
            why = self._limit_reason(inv, new_cost, balance)
            rec = self._new_record(m, now, placed=why is None, why=why)
            rec["book_at_placement"] = {"UP": (up_b.best_bid, up_b.best_ask), "DOWN": (dn_b.best_bid, dn_b.best_ask)}
            rec["spot_at_placement"] = self._spot[1] if self._spot else None
            rec["inventory_at_placement"] = inv
            self.markets[slug] = rec
            if why is not None:
                self.skips[why] += 1
                log.info("[POM] %s: no bids: %s (%s)", slug, why, json.dumps({k: round(v, 2) if isinstance(v, float) else v for k, v in inv.items()}))
                continue
            for side, tok in (("UP", m.yes_token_id), ("DOWN", m.no_token_id)):
                px = sc.price_for(side)
                o = Order(token_id=tok, side=Side.BUY, price=px, size=float(sc.quantity), tif=TimeInForce.GTC,
                          expires_ts=m.close_ts, expected_price=px,
                          fill_at_touch=self.fill_at_touch,
                          tag=f"{side} preopen bid {px:.2f} x {sc.quantity:g}, {lead:.0f} s before the open")
                rec["orders"][side] = {"order_id": None, "price": px, "size": float(sc.quantity), "state": "sent", "t": now}
                out.append((slug, side, o))
            self.stats["markets_bid"] += 1
            self.stats["orders"] += 2
            log.info("[POM] %s: bids %s x %g on both sides, %.0f s before the open | UP %.2f/%.2f DOWN %.2f/%.2f | inventory %s",
                     slug, sc.bid_price, sc.quantity, lead, up_b.best_bid or 0, up_b.best_ask or 0, dn_b.best_bid or 0, dn_b.best_ask or 0,
                     json.dumps({k: round(v, 2) if isinstance(v, float) else v for k, v in inv.items()}))
        return out

    def _new_record(self, m, now: float, placed: bool, why: str | None) -> dict:
        prev_epoch = int(m.open_ts // 1000) - (900 if self.scfg.market_type == "15m" else 300)
        return {"slug": m.slug, "open_ts": m.open_ts, "close_ts": m.close_ts, "placed": placed, "skip": why, "t_placed": now,
                "orders": {}, "filled": {"UP": 0.0, "DOWN": 0.0}, "cost": {"UP": 0.0, "DOWN": 0.0}, "fees": 0.0,
                "fills": [], "strike": m.strike if m.strike > 0 else None, "prev_settlement": self._prev_settle.get(prev_epoch),
                "settled": False, "path": []}

    def pair_cancels(self, now: float) -> list[str]:
        """Bids still resting cancel_after_s after their market's open."""
        out = []
        for rec in self.markets.values():
            if rec["settled"] or now < rec["open_ts"] + self.scfg.cancel_after_s * 1000.0:
                continue
            for side, o in rec["orders"].items():
                if o.get("state") == "resting" and o.get("order_id"):
                    out.append(o["order_id"])
                    o["state"] = "cancelling"
        return out

    def on_pair_submit(self, order_id: str, slug: str, side: str, price: float, shares: float) -> None:
        rec = self.markets.get(slug)
        if rec is None:
            # A legacy saved pair can be restored before this market is rediscovered.
            # Keep a record now so pair_requests will not issue a second pair meanwhile.
            epoch_text = slug.rsplit("-", 1)[-1]
            epoch = int(epoch_text) if epoch_text.isdigit() else 0
            span = 900 if self.scfg.market_type == "15m" else 300
            rec = self.markets.setdefault(slug, {
                "slug": slug, "open_ts": epoch * 1000.0, "close_ts": (epoch + span) * 1000.0,
                "placed": True, "skip": None, "t_placed": None, "orders": {},
                "filled": {"UP": 0.0, "DOWN": 0.0}, "cost": {"UP": 0.0, "DOWN": 0.0},
                "fees": 0.0, "fills": [], "strike": None, "prev_settlement": None,
                "settled": False, "path": [],
            })
        o = rec["orders"].setdefault(side, {"price": price, "size": shares, "t": None})
        o.update({"order_id": order_id, "state": "resting"})
        self._by_order[order_id] = (slug, side)

    def drop_pair(self, order_id: str) -> None:
        self._by_order.pop(order_id, None)

    def on_pair_fill_record(self, slug: str, order, fill, now: float) -> None:
        rec = self.markets.get(slug)
        where = self._by_order.get(getattr(order, "order_id", ""))
        if rec is None or where is None:
            return
        side = where[1]
        q, px, fee = float(fill.size), float(fill.price), float(getattr(fill, "fee", 0.0) or 0.0)
        rec["filled"][side] += q
        rec["cost"][side] += q * px
        rec["fees"] += fee
        o = rec["orders"].get(side) or {}
        full = rec["filled"][side] >= o.get("size", self.scfg.quantity) - 1e-6
        if full:
            o["state"] = "filled"
        rec["fills"].append({"side": side, "price": px, "qty": q, "fee": fee, "t": now, "t_placed": o.get("t"),
                             "s_before_open": round((rec["open_ts"] - now) / 1000.0, 1),
                             "book_at_placement": (rec.get("book_at_placement") or {}).get(side),
                             "spot_at_placement": rec.get("spot_at_placement"), "spot_at_fill": self._spot[1] if self._spot else None,
                             "prev_settlement": rec.get("prev_settlement"), "strike": rec.get("strike"), "partial": not full})
        self.stats["fills"] += 1
        self.stats["partial_fills"] += 0 if full else 1
        self.stats["fees"] += fee
        inv = self._open_inventory()
        self.stats["capital_locked_max"] = max(self.stats["capital_locked_max"], inv["exposure_usdc"] + inv["resting_usdc"])
        log.info("[POM_FILL] %s %s %g @ %.2f (%s) %.0f s before the open | UP %.1f DOWN %.1f | unmatched %.1f sh", slug, side, q, px,
                 "full" if full else "partial", (rec["open_ts"] - now) / 1000.0, rec["filled"]["UP"], rec["filled"]["DOWN"], inv["unmatched_shares"])

    def on_pair_done(self, order_id: str, filled: bool, killed: bool = False) -> None:
        where = self._by_order.get(order_id)
        if where is None:
            return
        slug, side = where
        rec = self.markets.get(slug)
        if rec is None:
            return
        o = rec["orders"].get(side)
        if o is None:
            return
        if filled:
            o["state"] = "filled"
        elif o.get("state") in ("resting", "cancelling", "sent"):
            o["state"] = "cancelled" if rec["filled"][side] <= 1e-9 else "partial_cancelled"
            self.stats["cancelled_unfilled"] += 1 if rec["filled"][side] <= 1e-9 else 0

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        rec = self.markets.get(slug)
        winner = "UP" if won_up else "DOWN"
        epoch = int(slug.rsplit("-", 1)[-1]) if slug.rsplit("-", 1)[-1].isdigit() else None
        if rec is None:
            return
        rec["settled"] = True
        rec["winner"] = winner
        if rec.get("strike") and epoch is not None:
            # the NEXT market's record gets this market's settlement value (= its own strike)
            self._prev_settle[epoch + (900 if self.scfg.market_type == "15m" else 300)] = rec["strike"]
        u, d = rec["filled"]["UP"], rec["filled"]["DOWN"]
        payout = u if won_up else d
        cost = rec["cost"]["UP"] + rec["cost"]["DOWN"]
        realised = payout - cost - rec["fees"]
        rec["pnl"] = realised
        cls = classify(u, d, self.scfg.quantity)
        rec["class"] = cls
        self.classes[cls] += 1
        pe = pair_economics(u, rec["cost"]["UP"], d, rec["cost"]["DOWN"], rec["fees"])
        rec["pair"] = pe
        if pe["paired"] > 0:
            self.stats["pairs"] += 1
            self.stats["pair_pnl"] += pe["net_guaranteed"]
        one_sided = realised - (pe["net_guaranteed"] if pe["paired"] > 0 else 0.0)
        if cls in ("UP_ONLY", "DOWN_ONLY", "PARTIAL_UP", "PARTIAL_DOWN") or (pe["paired"] > 0 and abs(u - d) > 1e-9):
            self.stats["one_sided"] += 1
            self.stats["one_sided_pnl"] += one_sided
            held = "UP" if u > d else "DOWN"
            self.stats["one_sided_won"] += int(held == winner)
        if u > 1e-9 or d > 1e-9:
            self.stats["settled"] += 1
            self.stats["pnl"] += realised
        self.last_settled.append({"slug": slug, "class": cls, "winner": winner, "up": round(u, 2), "down": round(d, 2),
                                  "cost": round(cost, 4), "pnl": round(realised, 4), "pair_cost_per_share": pe["combined_cost_per_share"],
                                  "strike": rec.get("strike"), "prev_settlement": rec.get("prev_settlement"), "fills": rec["fills"], "t": now})
        if u > 1e-9 or d > 1e-9:
            log.warning("[POM_SETTLED] %s %s: winner %s, UP %.1f DOWN %.1f, cost %.2f -> %+.3f%s", slug, cls, winner, u, d, cost, realised,
                        f" (pair {pe['combined_cost_per_share']:.3f}/sh, guaranteed {pe['net_guaranteed']:+.2f})" if pe["paired"] > 0 else "")
        # forget old records
        for s in [s for s, r in self.markets.items() if r["settled"] and now - r["close_ts"] > 3_600_000.0]:
            self.markets.pop(s, None)

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = "preopen_maker"
        snap["pom_config"] = self.scfg.as_dict()
        snap["pom"] = {"stats": dict(self.stats), "classes": {c: self.classes.get(c, 0) for c in CLASSES},
                       "skips": {s: self.skips.get(s, 0) for s in SKIPS}, "inventory": self._open_inventory(),
                       "open_markets": [{"slug": r["slug"], "orders": r["orders"], "filled": r["filled"], "cost": r["cost"]}
                                        for r in self.markets.values() if not r["settled"]],
                       "last_settled": list(self.last_settled)[-20:]}
        return snap
