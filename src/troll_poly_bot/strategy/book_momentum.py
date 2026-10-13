"""BOOK_MOMENTUM (the user's plan, 2026-10-09 "Small-bankroll Polymarket BTC 5-minute trading plan"): a momentum
taker plus selective maker bids on the Up / Down tokens of BTC 5-minute markets, short holds, one shared risk budget.

EXPERIMENTAL, PAPER ONLY. ``TPB_BOOKMOM_ENABLED=1`` is the feature flag and there is NO real-money switch
(__main__.check_book_momentum refuses shadow, micro-live and live). Env prefix ``TPB_BOOKMOM_``; own data tag and
budget. Not backtested (the plan's phase 2 replay has not been run).

The signal (each token on its own, from the Polymarket book -- not from BTC spot):
    rise       ask now - ask ``lookback_ms`` (500 ms) ago >= ``min_rise`` (1.5c)
    confirm    the last ``confirm_updates`` changes of that ask were all increases
    fresh      the last change is at most ``max_signal_age_ms`` (250 ms) old, the token's quote too
    book       spread <= ``max_spread`` (2c); ``min_time_remaining_s`` (30 s) .. ``max_time_remaining_s`` left
The edge (never "a 1.5c rise is enough"):
    expected exit = bid now + ``continuation_flat`` + ``continuation`` x rise   (the further move the hold catches)
    MEASURED 2026-10-09 on this bot's own 3,899 signal episodes (scripts/bookmom_signal_study.py): the token mid moves
    +1.2c in the 10 s after a signal whatever the size of the rise (the slope on the rise is negative), so the
    defaults are flat 1.2c / ratio 0 -- the plan's ratio 1.0 imagined +5..16c of edge on the big jumps -- and a maker
    bid that fills finds the bid 5.7c lower 10 s on (adverse_selection 0.057, not 1c). With these inputs the gate
    refuses almost every signal: the round trip costs ~4c (spread + two fees) against ~1c of continuation.
    taker:  exit - ask - fee(ask) - fee(exit) - slippage - safety_margin >= 0   (both legs pay the taker fee)
    maker:  exit - adverse_selection - bid - fee(exit) - safety_margin >= 0      (a bid fill pays no fee)
    fee = 0.07 x p x (1 - p) per share (the market's own schedule at decision)
Taker module: a fill-and-kill buy at ask + ``max_slippage`` for ``taker_usdc`` (2-4 USDC; the venue minimum 5 shares).
Maker module: a post-only bid AT the best bid for ``maker_usdc``; cancelled when the token's momentum reverses, the
    bid moves away by ``maker_reprice`` (re-placed next tick if the signal still holds), after ``maker_max_rest_s``, or
    under ``min_time_remaining_s``. Never left resting just to earn a rebate.
Exits (every position, both modules; valued at the executable BID):
    TAKE_PROFIT        bid - entry - entry fee - exit fee >= ``take_profit_net`` (2c a share)
    STOP_LOSS          that net loss x shares <= -``max_loss_usdc`` (1 USDC)
    MOMENTUM_REVERSAL  the token's bid fell >= ``reversal_drop`` over the lookback
    TIME_STOP          held ``max_hold_s`` (10 s)
    (a stop cannot guarantee its price: live.py sells through the bid within exit_max_cross; no bid = no exit)
Shared budget (both modules): open positions + resting bids <= ``max_total_exposure_usdc`` (40), at most
    ``max_simultaneous_positions`` (2) positions, ``max_daily_loss_usdc`` (6) stops new entries (the risk manager),
    one position or bid per token, never a buy of the opposite token after a loss.
Markouts: every fill's token mid at 100 ms, 500 ms, 1 s and 3 s, per module ([BOOKMOM_MARKOUT] lines + snapshot).
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
from .engine import EngineConfig, Evaluation, Reason, SideView
from .momentum import MomentumConfig, MomentumSignal
from .spot_lag import SpotLagConfig, SpotLagEngine

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_BOOKMOM_ENABLED"
STRATEGY_ID = "book_momentum"
MARKOUT_S = (0.1, 0.5, 1.0, 3.0)


def taker_fee(p: float, rate: float = 0.07) -> float:
    return 0.0 if p <= 0.0 or p >= 1.0 else rate * p * (1.0 - p)


@dataclass(slots=True)
class BookMomentumConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_BOOKMOM_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    market_type: str = "5m"
    # --- the signal ---
    lookback_ms: float = 500.0
    min_rise: float = 0.015
    confirm_updates: int = 2
    max_signal_age_ms: float = 250.0
    max_spread: float = 0.02
    min_time_remaining_s: float = 30.0
    max_time_remaining_s: float = 295.0
    min_ask: float = 0.20                    # 10-09: a 0.13 token gapped to 0.035 through the 1 USDC stop (-1.49)
    max_ask: float = 0.80
    # --- the edge (measured 10-09, see the module doc) ---
    continuation: float = 0.0                # expected further rise of the bid = flat + this x the signal's rise
    continuation_flat: float = 0.012
    slippage: float = 0.005
    safety_margin: float = 0.005
    adverse_selection: float = 0.057         # maker: what a bid fill gives back (price falling into you), measured
    # --- the taker module ---
    taker_enabled: bool = True
    taker_usdc: float = 3.0
    taker_max_usdc: float = 4.0
    max_slippage: float = 0.01               # the FAK limit above the seen ask
    order_type: str = "FAK"
    # --- the maker module ---
    maker_enabled: bool = True
    maker_usdc: float = 3.0
    maker_max_usdc: float = 4.0
    maker_max_rest_s: float = 5.0
    maker_reprice: float = 0.01
    maker_cancel_drop: float = 0.01          # cancel when the token's ask fell this much over the lookback
    # --- exits ---
    take_profit_net: float = 0.02
    max_loss_usdc: float = 1.0
    reversal_drop: float = 0.015
    max_hold_s: float = 10.0
    reentry_cooldown_s: float = 2.0
    hold_to_settlement: bool = False
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 10.0
    extend_max_hold_s: float = 0.0
    # --- the shared budget ---
    assets: str = "BTC"
    exclude_assets: str = ""
    max_simultaneous_positions: int = 2
    max_total_exposure_usdc: float = 40.0
    max_daily_loss_usdc: float = 6.0
    max_order_usdc: float = 4.0
    max_buys_per_hour: int = 600
    max_entries_per_market: int = 1000
    max_attempts_per_market: int = 1000
    max_quote_age_ms: float = 250.0
    max_spot_age_ms: float = 5000.0
    fast_feed: bool = False

    def momentum_mirror(self) -> MomentumConfig:
        # explicit base call: a slots dataclass is rebuilt, which breaks zero-argument super()
        m = SpotLagConfig.momentum_mirror(self)
        m.max_hold_s = self.max_hold_s
        return m

    def apply_budget(self, risk_cfg, order_ceiling: float | None = None) -> list[str]:
        """The spot-lag budget plus the caps a small bankroll's 2-4 USDC orders need: the per-asset / per-window
        defaults scale with the balance and must not be tighter than the total cap (one asset, one window)."""
        changed = SpotLagConfig.apply_budget(self, risk_cfg, order_ceiling)
        total = float(self.max_total_exposure_usdc)
        if order_ceiling is None and risk_cfg.max_total_exposure_usdc != total:
            changed.append(f"max_total_exposure_usdc: {risk_cfg.max_total_exposure_usdc} -> {total}")
            risk_cfg.max_total_exposure_usdc = total
        for attr in ("max_asset_exposure_usdc", "max_epoch_exposure_usdc"):
            if getattr(risk_cfg, attr) < total:
                changed.append(f"{attr}: {getattr(risk_cfg, attr)} -> {total}")
                setattr(risk_cfg, attr, total)
        return changed


@dataclass
class Signal:
    side: str
    token: str
    ask: float
    bid: float
    rise: float
    age_ms: float
    confirmed: bool
    spread: float


class BookMomentumEngine(SpotLagEngine):
    """The spot-lag engine's surface (feeds, budget, order path, exits); the book-momentum decisions inside."""

    feature_key = STRATEGY_ID
    budget_is_ceiling = True
    allows_reentry = True                    # several short round trips per market
    pair_retry = False                       # a refused or crossing maker bid is simply dropped: never re-placed or taken
    wants_books = True                       # pair_requests gets the books and the cash

    def __init__(self, cfg: EngineConfig, scfg: BookMomentumConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the book_momentum strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, BookMomentumConfig):
            raise TypeError("BookMomentumEngine needs a BookMomentumConfig")
        super().__init__(cfg, scfg, risk, features)
        self.hist: dict[str, collections.deque] = {}          # token -> (t, bid, ask) at every change of the book top
        self.last_top: dict[str, tuple[float, float | None, float | None]] = {}
        self.positions: dict[str, dict] = {}                   # token -> {slug, side, module, shares, cost, fee, t}
        self.resting: dict[str, dict] = {}                     # order id -> {slug, side, token, price, size, t}
        self.cooldown: dict[str, float] = {}                   # token -> no new entry before this time
        self.markouts: list[dict] = []
        self.mk_stats = {m: {h: [0.0, 0] for h in MARKOUT_S} for m in ("taker", "maker")}
        self.bm_stats = collections.Counter()
        self.meta_by_slug: dict[str, object] = {}

    # ------------------------------------------------------------------ the book history and the signal

    def _record(self, token: str, now: float, bid: float | None, ask: float | None) -> None:
        last = self.last_top.get(token)
        if last is not None and last[1] == bid and last[2] == ask:
            return
        self.last_top[token] = (now, bid, ask)
        h = self.hist.setdefault(token, collections.deque())
        h.append((now, bid, ask))
        # keep the last change before the 10 s window: after a quiet spell the price "lookback ago" is that one
        # (10-10: a book flat for > 10 s left no history, and the first rise after it was not seen)
        while len(h) > 1 and h[1][0] < now - 10_000.0:
            h.popleft()

    def _price_at(self, token: str, t: float, idx: int) -> float | None:
        h = self.hist.get(token)
        if not h:
            return None
        val = None
        for row in h:
            if row[0] <= t:
                val = row[idx]
            else:
                break
        return val

    def signal(self, token: str, side: str, now: float) -> Signal | None:
        sc = self.scfg
        h = self.hist.get(token)
        if not h:
            return None
        t_last, bid, ask = h[-1]
        if ask is None or bid is None:
            return None
        then = self._price_at(token, now - sc.lookback_ms, 2)
        if then is None:
            return None
        asks = []                                   # the ask at each of ITS changes (a bid-only update is not one)
        for r in h:
            if r[2] is not None and (not asks or abs(r[2] - asks[-1]) > 1e-12):
                asks.append(r[2])
        n = int(sc.confirm_updates)
        confirmed = len(asks) > n and all(asks[-i] > asks[-i - 1] + 1e-12 for i in range(1, n + 1))
        return Signal(side=side, token=token, ask=ask, bid=bid, rise=ask - then, age_ms=now - t_last, confirmed=confirmed,
                      spread=ask - bid)

    def _drop(self, token: str, now: float) -> float:
        """How far the token's bid fell over the lookback (positive = fell)."""
        then = self._price_at(token, now - self.scfg.lookback_ms, 1)
        last = self.last_top.get(token)
        if then is None or last is None or last[1] is None:
            return 0.0
        return then - last[1]

    def exit_px(self, sig: Signal) -> float:
        """The bid the hold expects to sell at: now + the measured continuation."""
        sc = self.scfg
        return min(0.99, sig.bid + sc.continuation_flat + sc.continuation * max(sig.rise, 0.0))

    def edge(self, sig: Signal, module: str) -> float:
        sc = self.scfg
        exit_px = self.exit_px(sig)
        if module == "taker":
            return exit_px - sig.ask - taker_fee(sig.ask) - taker_fee(exit_px) - sc.slippage - sc.safety_margin
        return exit_px - sc.adverse_selection - sig.bid - taker_fee(exit_px) - sc.safety_margin

    # ------------------------------------------------------------------ the shared budget

    def exposure(self) -> tuple[float, int]:
        """(USDC in open positions + resting bids, positions held)."""
        pos = sum(p["cost"] + p["fee"] for p in self.positions.values())
        rest = sum(o["price"] * o["size"] for o in self.resting.values())
        return pos + rest, len(self.positions)

    def _budget_refusal(self, new_usdc: float, balance: float | None) -> str | None:
        sc = self.scfg
        exp, n = self.exposure()
        if n >= int(sc.max_simultaneous_positions):
            return f"{n} positions open (max {int(sc.max_simultaneous_positions)})"
        if exp + new_usdc > sc.max_total_exposure_usdc + 1e-9:
            return f"exposure {exp:.2f} + {new_usdc:.2f} > {sc.max_total_exposure_usdc:g} (positions + resting bids)"
        if balance is not None and new_usdc > float(balance) - sum(o["price"] * o["size"] for o in self.resting.values()) + 1e-9:
            return f"cash {float(balance):.2f} cannot fund {new_usdc:.2f} beside the resting bids"
        h = self.risk.halted()                          # a RiskVerdict (always truthy): read .ok
        if not h.ok:
            return f"risk halt: {h.detail}"
        # 10-09: the stop counted only realised P&L, so entries kept coming at -5 with positions open and the day ended at
        # -6.69 (stop 6): a new entry needs room for every open position, bid and itself to lose max_loss_usdc
        worst = (n + len(self.resting) + 1) * sc.max_loss_usdc
        if self.risk.realised_today - worst <= -sc.max_daily_loss_usdc + 1e-9:
            return (f"daily-loss headroom: realised {self.risk.realised_today:+.2f} - {worst:.2f} at risk "
                    f"<= -{sc.max_daily_loss_usdc:g}")
        return None

    def _token_busy(self, token: str, now: float) -> str | None:
        if token in self.positions:
            return "a position is open on this token"
        if any(o["token"] == token for o in self.resting.values()):
            return "a bid is resting on this token"
        if now < self.cooldown.get(token, -1e18):
            return "re-entry cooldown"
        return None

    def _shares(self, usdc: float, price: float, cap: float, venue_min: float) -> float | None:
        q = max(venue_min, math.floor(usdc / price * 100.0) / 100.0)
        return None if q * price > cap + 1e-9 else q

    # ------------------------------------------------------------------ the taker module (evaluate)

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        cfg, sc, m = self.cfg, self.scfg, meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        self.meta_by_slug[m.slug] = meta
        self._close_ts[m.slug] = m.close_ts
        for tok, b in ((m.yes_token_id, book_up), (m.no_token_id, book_down)):
            if b is not None:
                self._record(tok, now, b.best_bid, b.best_ask)
        self._markout_tick(now)
        self._forget(now)
        if not sc.asset_ok(m.asset) or f"-updown-{sc.market_type}-" not in m.slug:
            return self._reject(ev, Reason.ASSET_FILTERED, f"{m.slug}: not a {sc.assets} {sc.market_type} market")
        if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
        if book_up is None or book_down is None:
            return self._reject(ev, Reason.NO_OFFER, "no book")
        ev.p_market = book_up.mid
        ev.p_used = book_up.mid if book_up.mid is not None else 0.5
        if not sc.taker_enabled:
            return self._reject(ev, Reason.WEAK_MOMENTUM, "taker module off")
        best = None
        for side, tok in (("UP", m.yes_token_id), ("DOWN", m.no_token_id)):
            s = self.signal(tok, side, now)
            if s is not None and s.rise >= sc.min_rise - 1e-9 and s.confirmed and (best is None or s.rise > best.rise):
                best = s
        if best is None:
            return self._reject(ev, Reason.WEAK_MOMENTUM, f"no token rose {sc.min_rise * 100:.1f}c over {sc.lookback_ms:.0f} ms on rising updates")
        s = best
        book = book_up if s.side == "UP" else book_down
        info = {"side": s.side, "direction": s.side, "ask": s.ask, "bid": s.bid, "rise": round(s.rise, 4), "age_ms": round(s.age_ms),
                "spread": round(s.spread, 4), "module": "taker", "left_s": round(left, 1)}
        ev.features = {STRATEGY_ID: info,
                       "momentum": {"label": "BOOK_" + s.side, "z": {"main": round(s.rise * 100, 2)}, "score": round(s.rise * 100, 2)},
                       "execution": {"best_ask": s.ask, "best_bid": s.bid, "spread": round(s.spread, 4)}}
        self.last_signal[m.asset] = MomentumSignal(direction=1 if s.side == "UP" else -1, label="BOOK_" + s.side,
                                                   z={"main": s.rise * 100}, sigma_per_sec=0.0)
        sv = SideView(side=s.side, token_id=s.token, p_model=s.ask, p_market=s.ask, ask=s.ask, bid=s.bid, depth_at_limit=0.0,
                      costs=None, reason=None, entry_px=None)
        ev.sides = [sv]
        if s.age_ms > sc.max_signal_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"signal {s.age_ms:.0f} ms old > {sc.max_signal_age_ms:.0f}")
        qa = (quote_ages or {}).get(s.token) if quote_ages is not None else book_age_ms
        if qa is not None and qa > sc.max_quote_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"{s.side} quote {qa:.0f} ms old")
        if s.spread > sc.max_spread + 1e-9:
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"spread {s.spread:.3f} > {sc.max_spread:.2f}")
        if not (sc.min_ask <= s.ask <= sc.max_ask):
            return self._reject(ev, Reason.PRICE_BAND, f"ask {s.ask:.2f}")
        e = self.edge(s, "taker")
        info["edge"] = round(e, 4)
        if e < 0.0:
            return self._reject(ev, Reason.EDGE_TOO_SMALL,
                                f"exit {self.exit_px(s):.3f} - ask {s.ask:.3f} - fees "
                                f"{taker_fee(s.ask) + taker_fee(self.exit_px(s)):.4f} - slip/margin = {e:+.4f}")
        info["signal"] = True
        why = self._token_busy(s.token, now)
        if why:
            return self._reject(ev, Reason.MARKET_ENTERED, why)
        limit = min(0.99, round(s.ask + sc.max_slippage, 4))
        venue_min = float(m.min_size) if m.min_size else 5.0
        q = self._shares(sc.taker_usdc, limit, sc.taker_max_usdc, venue_min)
        if q is None:
            return self._reject(ev, Reason.SIZE_ZERO, f"{venue_min:g} sh at {limit:.2f} > {sc.taker_max_usdc:g} USDC")
        why = self._budget_refusal(q * limit, balance)
        if why:
            return self._reject(ev, Reason.EXPOSURE_CAP, why)
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), q * limit, add_to_open=True)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)
        info.update({"size": q, "limit": limit})
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = s.side, s.token, limit, s.ask
        ev.max_entry_price = limit
        ev.size, ev.net_edge, ev.confidence = q, e, 1.0
        ev.size_note = f"taker {q:g} sh <= {limit:.3f} (edge {e:+.4f})"
        self.bm_stats["taker_signals"] += 1
        self.last[m.slug] = ev
        return ev

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        bm = (ev.features or {}).get(STRATEGY_ID, {})
        self.bm_stats["taker_orders"] += 1
        self.cooldown[ev.token_id] = (now_ms or 0.0) + self.scfg.reentry_cooldown_s * 1000.0
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                     tif=TimeInForce.IOC if self._fak else TimeInForce.FOK, expected_price=ev.expected_price,
                     fair_at_decision=ev.p_used,
                     tag=f"{ev.side} book_momentum taker rise={bm.get('rise', 0) * 100:+.1f}c edge={ev.net_edge:+.4f}")

    def signal_text(self, ev: Evaluation) -> str:
        bm = (ev.features or {}).get(STRATEGY_ID, {})
        return f"BOOKMOM {bm.get('side')} ask {bm.get('ask')} rise {bm.get('rise')} edge {bm.get('edge')} ({bm.get('module')})"

    # ------------------------------------------------------------------ the maker module (resting bids)

    def pair_requests(self, now: float, markets: dict | None = None, books: dict | None = None,
                      balance: float | None = None) -> list[tuple[str, str, Order]]:
        sc = self.scfg
        out: list[tuple[str, str, Order]] = []
        if not sc.maker_enabled:
            return out
        for slug, m in (markets or {}).items():
            if not sc.asset_ok(m.asset) or f"-updown-{sc.market_type}-" not in slug:
                continue
            left = (m.close_ts - now) / 1000.0
            if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s) or now < m.open_ts:
                continue
            bk = (books or {}).get(slug) or (None, None)
            for side, tok, b in (("UP", m.yes_token_id, bk[0]), ("DOWN", m.no_token_id, bk[1])):
                if b is None:
                    continue
                self._record(tok, now, b.best_bid, b.best_ask)
                s = self.signal(tok, side, now)
                if s is None or s.rise < sc.min_rise - 1e-9 or not s.confirmed or s.age_ms > sc.max_signal_age_ms:
                    continue
                if s.spread > sc.max_spread + 1e-9 or not (sc.min_ask <= s.bid <= sc.max_ask) or s.spread < 0.01 - 1e-9:
                    continue
                if self._token_busy(tok, now) or self.edge(s, "maker") < 0.0:
                    if self.edge(s, "maker") < 0.0:
                        self.bm_stats["maker_edge_refused"] += 1
                    continue
                venue_min = float(getattr(m, "min_size", 0.0) or 5.0)
                q = self._shares(sc.maker_usdc, s.bid, sc.maker_max_usdc, venue_min)
                if q is None:
                    continue
                why = self._budget_refusal(q * s.bid, balance)
                if why:
                    self.bm_stats["maker_budget_refused"] += 1
                    continue
                o = Order(token_id=tok, side=Side.BUY, price=s.bid, size=q, tif=TimeInForce.GTC, expires_ts=m.close_ts,
                          expected_price=s.bid, post_only=True,
                          tag=f"{side} book_momentum maker bid {s.bid:.2f} x {q:g} (rise {s.rise * 100:+.1f}c, edge {self.edge(s, 'maker'):+.4f})")
                # held at once (before on_pair_submit) so the budget and the token lock see it this tick
                self.resting[o.order_id] = {"slug": slug, "side": side, "token": tok, "price": s.bid, "size": q, "t": now,
                                            "filled": 0.0, "state": "sent"}
                self.bm_stats["maker_orders"] += 1
                out.append((slug, side, o))
                log.info("[BOOKMOM_BID] %s %s %g @ %.2f | rise %+.1fc in %.0f ms, spread %.2f, edge %+.4f", slug, side, q, s.bid,
                         s.rise * 100, sc.lookback_ms, s.spread, self.edge(s, "maker"))
        return out

    def pair_cancels(self, now: float) -> list[str]:
        sc = self.scfg
        out = []
        self._markout_tick(now)
        for oid, o in list(self.resting.items()):
            if o.get("state") == "cancelling":
                continue
            close = self._close_ts.get(o["slug"])
            last = self.last_top.get(o["token"])
            why = None
            if now - o["t"] >= sc.maker_max_rest_s * 1000.0:
                why = "rested too long"
            elif close is not None and (close - now) / 1000.0 < sc.min_time_remaining_s:
                why = "too close to the close"
            elif last is not None and last[1] is not None and last[1] >= o["price"] + sc.maker_reprice - 1e-9:
                why = "the bid moved away (re-placed next tick if the signal holds)"
            elif self._drop(o["token"], now) >= sc.maker_cancel_drop - 1e-9 or \
                    (self._price_at(o["token"], now - sc.lookback_ms, 2) or 0.0) - ((last or (0, 0, 0))[2] or 0.0) >= sc.maker_cancel_drop - 1e-9:
                why = "momentum reversed"
            if why:
                o["state"] = "cancelling"
                out.append(oid)
                self.bm_stats["maker_cancels"] += 1
                log.info("[BOOKMOM_CANCEL] %s %s bid %.2f: %s", o["slug"], o["side"], o["price"], why)
        return out

    def on_pair_submit(self, order_id: str, slug: str, side: str, price: float, shares: float) -> None:
        o = self.resting.get(order_id)
        if o is not None and o.get("state") == "sent":
            o["state"] = "resting"

    def drop_pair(self, order_id: str) -> None:
        self.resting.pop(order_id, None)

    def on_pair_fill_record(self, slug: str, order, fill, now: float) -> None:
        o = self.resting.get(getattr(order, "order_id", ""))
        if o is None:
            return
        q, px, fee = float(fill.size), float(fill.price), float(getattr(fill, "fee", 0.0) or 0.0)
        o["filled"] += q
        self._add_position(o["token"], slug, o["side"], "maker", q, px, fee, now)
        self.bm_stats["maker_fills"] += 1
        if o["filled"] >= o["size"] - 1e-9:
            self.resting.pop(order.order_id, None)

    def on_pair_done(self, order_id: str, filled: bool, killed: bool = False) -> None:
        self.resting.pop(order_id, None)

    # ------------------------------------------------------------------ fills, exits, settlement

    def on_fill_detail(self, slug: str, side: str, price: float, size: float, fee: float, now: float) -> None:
        """live.py: every buy fill (taker entries and maker fills alike). A maker fill is booked by
        on_pair_fill_record instead; this one books the taker entries."""
        meta = self.meta_by_slug.get(slug)
        if meta is None:
            return
        m = meta.market
        tok = m.yes_token_id if side == "UP" else m.no_token_id
        if any(o["token"] == tok for o in self.resting.values()):
            return                                     # the maker's own fill
        # a bid that filled while its cancel was in flight is no longer in ``resting``: a fee-free fill is a maker fill
        # (a taker always pays 0.07*p*(1-p)), or the taker/maker markouts mix (paper 2026-10-09 01:35)
        module = "taker" if float(fee) > 0.0 else "maker"
        self._add_position(tok, slug, side, module, float(size), float(price), float(fee), now)
        self.bm_stats[f"{module}_fills"] += 1

    def _add_position(self, token, slug, side, module, q, px, fee, now) -> None:
        p = self.positions.setdefault(token, {"slug": slug, "side": side, "module": module, "shares": 0.0, "cost": 0.0,
                                              "fee": 0.0, "t": now})
        p["shares"] += q
        p["cost"] += q * px
        p["fee"] += fee
        self.entry_ms[token] = p["t"]
        for h in MARKOUT_S:
            self.markouts.append({"token": token, "slug": slug, "side": side, "module": module, "px": px, "t": now, "h": h})
        log.info("[BOOKMOM_FILL] %s %s %s %g @ %.3f fee %.4f | positions %d, exposure %.2f", module, slug, side, q, px, fee,
                 len(self.positions), self.exposure()[0])

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig: MomentumSignal | None = None, spread: float | None = None,
                      bid_depth: float | None = None, size: float | None = None,
                      data_age_ms: float | None = None) -> str | None:
        sc = self.scfg
        p = self.positions.get(token_id)
        q = float(size or (p["shares"] if p else 0.0))
        if q <= 0.0:
            return None
        if p and p["shares"] > 0:
            entry = p["cost"] / p["shares"]               # what this engine paid (the fee apart)
        entry_fee = (p["fee"] / p["shares"]) if p and p["shares"] > 0 else taker_fee(entry)
        net_ps = bid - entry - entry_fee - taker_fee(bid)
        if net_ps >= sc.take_profit_net - 1e-9:
            return "TAKE_PROFIT"
        if net_ps * q <= -sc.max_loss_usdc + 1e-9:
            return "STOP_LOSS"
        if self._drop(token_id, now_ms) >= sc.reversal_drop - 1e-9:
            return "MOMENTUM_REVERSAL"
        opened = self.entry_ms.get(token_id)
        if opened is not None and (now_ms - opened) / 1000.0 >= sc.max_hold_s:
            return "TIME_STOP"
        return None

    def on_exit(self, slug: str, token_id: str, now_ms: float) -> None:
        SpotLagEngine.on_exit(self, slug, token_id, now_ms)
        p = self.positions.pop(token_id, None)
        self.cooldown[token_id] = now_ms + self.scfg.reentry_cooldown_s * 1000.0
        if p is not None:
            self.bm_stats[f"{p['module']}_exits"] += 1

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        for tok in [t for t, p in self.positions.items() if p["slug"] == slug]:
            p = self.positions.pop(tok)
            self.bm_stats[f"{p['module']}_settled"] += 1

    def _forget(self, now: float) -> None:
        for slug, c in list(self._close_ts.items()):
            if now > c + 120_000.0:
                for tok in [t for t, p in self.positions.items() if p["slug"] == slug]:
                    self.positions.pop(tok, None)                 # settled or abandoned: no longer exposure
                for oid in [i for i, o in self.resting.items() if o["slug"] == slug]:
                    self.resting.pop(oid, None)
                self.meta_by_slug.pop(slug, None)
        self._forget_closed(now)

    # ------------------------------------------------------------------ markouts

    def _markout_tick(self, now: float) -> None:
        keep = []
        for mk in self.markouts:
            if now < mk["t"] + mk["h"] * 1000.0:
                keep.append(mk)
                continue
            last = self.last_top.get(mk["token"])
            if last is None or last[1] is None or last[2] is None:
                continue
            mid = (last[1] + last[2]) / 2.0
            move = mid - mk["px"]
            acc = self.mk_stats[mk["module"]][mk["h"]]
            acc[0] += move
            acc[1] += 1
            log.info("[BOOKMOM_MARKOUT] %s", json.dumps({"module": mk["module"], "slug": mk["slug"], "side": mk["side"],
                                                         "h_s": mk["h"], "delay_ms": round(now - mk["t"]), "fill": mk["px"],
                                                         "mid": round(mid, 4), "move_c": round(move * 100, 2)}))
        self.markouts = keep

    def snapshot(self) -> dict:
        snap = super().snapshot()
        exp, n = self.exposure()
        snap["strategy"] = STRATEGY_ID
        snap["book_momentum"] = {
            "config": self.scfg.as_dict(), "stats": dict(self.bm_stats), "positions": {k: dict(v) for k, v in self.positions.items()},
            "resting": {k: dict(v) for k, v in self.resting.items()}, "exposure_usdc": round(exp, 4), "positions_open": n,
            "markout_c": {mod: {f"{h:g}s": (round(a[0] / a[1] * 100, 3) if a[1] else None, a[1]) for h, a in hs.items()}
                          for mod, hs in self.mk_stats.items()}}
        return snap
