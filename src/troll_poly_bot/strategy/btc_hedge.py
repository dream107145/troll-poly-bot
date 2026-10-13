"""BTC_TREND_REVERSAL_HEDGE: one initial buy in every BTC 5-minute market on the short-term BTC
direction, a price-gated hedge of the other side after a confirmed reversal, everything held to
settlement.

EXPERIMENTAL, PAPER ONLY (docs/btc_hedge_2026-09-30.md; scripts/research_btc_hedge.py). Its own
id (``btc_trend_reversal_hedge``), env prefix (``TPB_BTC_HEDGE_*``), budget and data files;
behind ``TPB_BTC_HEDGE_ENABLED=1``; __main__ refuses micro-live, live and shadow for it
unconditionally. The other strategies are untouched.

Per market::

    MARKET_START   the first evaluation after the open: spot and both asks
    observe        ``initial_observation_seconds`` after the open
    INITIAL_ENTRY  the ONE mandatory trade: the side of the 5 s BTC return when it is at least
                   ``trend_z`` sigma and the 1 s return agrees (TREND_UP / TREND_DOWN), else the
                   cheaper executable ask (CHEAPEST_SIDE_FALLBACK); ``initial_entry_size`` shares,
                   FOK at ask + max_slippage, ask <= ``max_initial_entry_price``; a kill is retried
                   on the next tick after the bot's retry gap, ``initial_attempts`` times, and after
                   ``initial_fak_after`` kills as a fill-and-kill (the existing fallback). Never on
                   stale data, never past a risk limit: the requirement is to try, not to override
    add            while the trend continues (same-sign 5 s return >= trend_z) up to
                   ``add_while_trending`` extra clips, if configured (default 0)
    REVERSAL       the 5 s return points against the held side by >= trend_z for
                   ``reversal_confirmation_seconds`` of consecutive observations
    HEDGE_CHECK    pair_cost = average first-leg price + opposite ask + fee(ask) + slippage +
                   latency allowance; HEDGE when <= ``max_pair_cost`` (``hedge_mode`` gated), always
                   (unconditional) or never (none); size = the unmatched first-leg quantity, FOK;
                   a WAIT is re-checked on every later tick while unmatched
    MARKET_END     at settlement: both legs, matched / unmatched, payout, fees, net
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from ..features.engine import FeatureEngine
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason, SideView
from .momentum import MomentumConfig, MomentumSignal, get_executable_price
from .spot_lag import SpotLagConfig, SpotLagEngine

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_BTC_HEDGE_ENABLED"
HEDGE_MODES = ("gated", "unconditional", "none")


@dataclass(slots=True)
class BtcHedgeConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_BTC_HEDGE_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    # --- the brief's switches ---
    btc_only: bool = True
    require_initial_trade: bool = True
    initial_entry_size: float = 5.0
    initial_observation_seconds: float = 10.0
    max_pair_cost: float = 0.97
    max_initial_entry_price: float = 0.65
    reversal_confirmation_seconds: float = 3.0
    #: 0 = the unmatched quantity of the first leg (never more)
    max_hedge_size: float = 0.0
    hold_matched_to_settlement: bool = True
    # --- the rest ---
    hedge_mode: str = "gated"                    # gated | unconditional | none
    # --- PAIRS (2026-10-01 brief): repeat first-leg + hedge pairs while the market is open ---
    #: pairs per market (1 = the original one-and-done flow)
    max_pairs_per_market: int = 1
    #: a hedge waits this long after the first leg filled; a new first leg this long after a pair closed
    min_seconds_between_legs: float = 0.0
    #: the pair must pay at least this much: pair_cost <= min(max_pair_cost, 1 - min_pair_edge)
    min_pair_edge: float = 0.0
    trend_z: float = 1.0
    initial_attempts: int = 8
    #: after this many killed FOKs the initial entry goes out as a fill-and-kill
    initial_fak_after: int = 3
    add_while_trending: int = 0
    latency_allowance: float = 0.005
    slippage: float = 0.005
    max_slippage: float = 0.01
    min_ask: float = 0.02
    max_ask: float = 0.98
    max_spread: float = 0.05
    min_time_remaining_s: float = 15.0
    max_time_remaining_s: float = 295.0
    max_quote_age_ms: float = 1000.0
    max_spot_age_ms: float = 1000.0
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0
    assets: str = "BTC"
    exclude_assets: str = ""
    max_simultaneous_positions: int = 2
    max_buys_per_hour: int = 120
    max_entries_per_market: int = 1_000
    max_attempts_per_market: int = 1_000
    max_order_usdc: float = 10.0
    max_total_exposure_usdc: float = 40.0
    max_daily_loss_usdc: float = 20.0

    def momentum_mirror(self) -> MomentumConfig:
        m = SpotLagConfig.momentum_mirror(self)
        m.max_hold_s = 1e9
        return m

    def apply_budget(self, risk_cfg, order_ceiling: float | None = None) -> list[str]:
        """The spot-lag budget, then the per-asset / per-window caps set so a first leg and
        its hedge fit in one market (paper only -- __main__ refuses real money for it)."""
        changed = SpotLagConfig.apply_budget(self, risk_cfg, order_ceiling)
        per_market = 2.0 * self.max_order_usdc
        for attr, new in (("max_asset_exposure_usdc", max(risk_cfg.max_asset_exposure_usdc, per_market * self.max_simultaneous_positions)),
                          ("max_epoch_exposure_usdc", max(risk_cfg.max_epoch_exposure_usdc, per_market)),
                          ("max_drawdown_usdc", max(risk_cfg.max_drawdown_usdc, 2.0 * self.max_daily_loss_usdc))):
            old = getattr(risk_cfg, attr)
            if old != new:
                setattr(risk_cfg, attr, new)
                changed.append(f"{attr}: {old} -> {new}")
        return changed


@dataclass(slots=True)
class MarketState:
    slug: str
    started: bool = False
    side: str | None = None
    reason: str | None = None
    z1: float | None = None
    z5: float | None = None
    attempts: int = 0
    first_qty: float = 0.0
    first_cost: float = 0.0
    hedge_qty: float = 0.0
    hedge_cost: float = 0.0
    fees: float = 0.0
    adds: int = 0
    against_since: float | None = None
    reversal: bool = False
    hedge_attempts: int = 0
    last_check_ms: float = -math.inf
    best_pair: float | None = None
    pending: str | None = None            # "initial" | "add" | "hedge": what the working order is
    done: bool = False
    # per token side, across every pair of the market (the settlement is computed from these)
    up_qty: float = 0.0
    up_cost: float = 0.0
    dn_qty: float = 0.0
    dn_cost: float = 0.0
    pairs: int = 0                        # completed pairs (hedge fully matched)
    first_fill_ms: float | None = None
    pair_done_ms: float | None = None
    pair_log: list = field(default_factory=list)

    @property
    def avg_first(self) -> float | None:
        return self.first_cost / self.first_qty if self.first_qty > 1e-9 else None

    @property
    def unmatched(self) -> float:
        return max(0.0, self.first_qty - self.hedge_qty)


class BtcHedgeEngine(SpotLagEngine):
    """Same surface as the spot-lag engine; the mandatory-entry-then-hedge decision inside."""

    feature_key = "btc_hedge"
    budget_is_ceiling = True
    stacks_positions = True
    entry_retry_ms = 2000.0

    def __init__(self, cfg: EngineConfig, scfg: BtcHedgeConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the btc_trend_reversal_hedge strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, BtcHedgeConfig):
            raise TypeError("BtcHedgeEngine needs a BtcHedgeConfig")
        if scfg.hedge_mode not in HEDGE_MODES:
            raise ValueError(f"btc_hedge hedge_mode {scfg.hedge_mode!r}: {' | '.join(HEDGE_MODES)}")
        super().__init__(cfg, scfg, risk, features)
        self.markets: dict[str, MarketState] = {}
        self.ended: list[dict] = []                    # the last MARKET_END records (bounded)

    def _forget_closed(self, now_ms: float) -> None:
        gone = [s for s, c in self._close_ts.items() if now_ms > c + 60_000.0]
        SpotLagEngine._forget_closed(self, now_ms)
        for slug in gone:
            self.markets.pop(slug, None)

    def _state(self, slug: str) -> MarketState:
        st = self.markets.get(slug)
        if st is None:
            st = self.markets[slug] = MarketState(slug=slug)
        return st

    # ------------------------------------------------------------ the decision

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        cfg, sc, m = self.cfg, self.scfg, meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        if (sc.btc_only and m.asset.upper() != "BTC") or not sc.asset_ok(m.asset):
            return self._reject(ev, Reason.ASSET_FILTERED, f"{m.asset}: BTC only")
        st = self._state(m.slug)
        self._close_ts[m.slug] = m.close_ts
        self._forget_closed(now)
        a_up = book_up.best_ask if book_up is not None else None
        a_dn = book_down.best_ask if book_down is not None else None
        if not st.started:
            st.started = True
            log.info("MARKET_START market=%s btc_spot=%s up_ask=%s down_ask=%s", m.slug,
                     None if spot is None else round(spot.price, 2), a_up, a_dn)
        if spot is None or spot_age_ms is None:
            return self._reject(ev, Reason.STALE_DATA, "no spot")
        if spot_age_ms > min(cfg.max_spot_age_ms, sc.max_spot_age_ms):
            return self._reject(ev, Reason.STALE_DATA, f"spot {spot_age_ms:.0f}ms")
        if book_age_ms is None or book_age_ms > cfg.max_book_age_ms:
            return self._reject(ev, Reason.STALE_DATA, "book" if book_age_ms is None else f"book {book_age_ms:.0f}ms")
        if vol is None or not getattr(vol, "ready", False):
            return self._reject(ev, Reason.MODEL_NOT_READY)
        sigma = vol.sigma_per_sec
        self.last_sigma[m.asset] = sigma
        hist = self.features.spot.get(m.asset)
        r1 = hist.logret(now, 1.0) if hist is not None else None
        r5 = hist.logret(now, 5.0) if hist is not None else None
        if r1 is None or r5 is None or sigma <= 0.0:
            return self._reject(ev, Reason.MODEL_NOT_READY, "spot history")
        z1, z5 = r1 / sigma, r5 / (sigma * math.sqrt(5.0))
        d5 = 1 if z5 > 0 else -1
        ev.p_market = book_up.mid if book_up is not None else None
        ev.features = {"btc_hedge": {"z1": round(z1, 3), "z5": round(z5, 3), "side": st.side, "phase": "initial" if st.first_qty <= 0 else "holding"},
                       "momentum": {"label": "NONE", "z": {"main": round(z5, 3)}, "score": round(abs(z5), 3)}}
        self.last_signal[m.asset] = MomentumSignal(direction=d5 if abs(z5) >= sc.trend_z else 0, label="NONE",
                                                   z={"main": z5}, sigma_per_sec=sigma)
        ev.spot_move_bps = r1 * 1e4
        if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
        if left * 1000.0 < latency.round_trip_ms() + cfg.latency_safety_margin_ms:
            return self._reject(ev, Reason.CANNOT_LAND_IN_TIME)
        # the reversal clock runs whatever else happens this tick
        if st.first_qty > 0 and st.side is not None:
            want = -1 if st.side == "UP" else 1
            if z5 * want >= sc.trend_z:
                st.against_since = now if st.against_since is None else st.against_since
                if not st.reversal and now - st.against_since >= sc.reversal_confirmation_seconds * 1000.0:
                    st.reversal = True
                    log.info("REVERSAL market=%s original_side=%s new_direction=%s confirmation=%.1fs z5=%+.2f",
                             m.slug, st.side, "DOWN" if st.side == "UP" else "UP", (now - st.against_since) / 1000.0, z5)
            else:
                st.against_since = None
        if st.pending is not None:
            return self._reject(ev, Reason.COOLDOWN, "order working")
        # a completed pair makes room for the next one (max_pairs_per_market), after the leg gap
        if st.first_qty > 0.0 and st.unmatched <= 1e-9 and st.hedge_qty > 0.0:
            if st.pairs < int(sc.max_pairs_per_market):
                if st.pair_done_ms is not None and now - st.pair_done_ms >= sc.min_seconds_between_legs * 1000.0:
                    self._next_pair(st, now)
            else:
                return self._reject(ev, Reason.ALREADY_POSITIONED, f"{st.pairs} pair(s) done, holding to settlement")

        # ── the initial entry (mandatory, or on a trend only)
        if st.first_qty <= 0.0:
            if (m.close_ts - left * 1000.0) + sc.initial_observation_seconds * 1000.0 > now + 1e-9 and \
                    now < m.open_ts + sc.initial_observation_seconds * 1000.0:
                return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW, f"observing for {sc.initial_observation_seconds:g}s")
            if st.attempts >= int(sc.initial_attempts):
                return self._reject(ev, Reason.MARKET_ATTEMPTS, f"{st.attempts} initial attempts")
            if st.side is None:
                if abs(z5) >= sc.trend_z and z1 * z5 > 0:
                    st.side, st.reason = ("UP" if z5 > 0 else "DOWN"), ("TREND_UP" if z5 > 0 else "TREND_DOWN")
                elif not sc.require_initial_trade:
                    # no mandatory trade: only a trend earns an entry, the fallback never fires
                    return self._reject(ev, Reason.WEAK_MOMENTUM, f"no trend (z5 {z5:+.2f}); initial trade not required")
                elif a_up is not None and a_dn is not None:
                    st.side, st.reason = ("UP" if a_up <= a_dn else "DOWN"), "CHEAPEST_SIDE_FALLBACK"
                else:
                    return self._reject(ev, Reason.NO_OFFER, "no asks to choose from")
                st.z1, st.z5 = z1, z5
            side = st.side
            fak = st.attempts >= int(sc.initial_fak_after)
            return self._entry(ev, meta, book_up if side == "UP" else book_down, side, sc.initial_entry_size,
                               "initial", now, quote_ages, balance, max_price=sc.max_initial_entry_price, fak=fak)

        # ── after the entry: add while trending, or hedge
        side = st.side
        other = "DOWN" if side == "UP" else "UP"
        ev.features["btc_hedge"].update({"unmatched": round(st.unmatched, 2), "reversal": st.reversal})
        if st.reversal and sc.hedge_mode != "none" and st.unmatched > 1e-9:
            if st.first_fill_ms is not None and now - st.first_fill_ms < sc.min_seconds_between_legs * 1000.0:
                return self._reject(ev, Reason.COOLDOWN, f"hedge waits {sc.min_seconds_between_legs:g}s after the first leg")
            obook = book_down if side == "UP" else book_up
            ask = obook.best_ask if obook is not None else None
            if ask is None:
                return self._reject(ev, Reason.NO_OFFER, f"no {other} ask")
            fee = meta.fee.taker_fee_per_share(ask)
            pair = st.avg_first + ask + fee + sc.slippage + sc.latency_allowance
            st.best_pair = pair if st.best_pair is None else min(st.best_pair, pair)
            cap = min(sc.max_pair_cost, 1.0 - sc.min_pair_edge)
            go = sc.hedge_mode == "unconditional" or pair <= cap + 1e-9
            if now - st.last_check_ms >= 2000.0 or go:
                st.last_check_ms = now
                log.info("HEDGE_CHECK market=%s original_avg=%.4f opposite_ask=%.2f pair_cost=%.4f fees=%.4f slippage=%.3f "
                         "effective_cost=%.4f max_pair_cost=%.2f decision=%s", m.slug, st.avg_first, ask, st.avg_first + ask,
                         fee, sc.slippage + sc.latency_allowance, pair, sc.max_pair_cost, "HEDGE" if go else "WAIT")
            ev.features["btc_hedge"].update({"pair_cost": round(pair, 4), "hedge_decision": "HEDGE" if go else "WAIT"})
            if not go:
                return self._reject(ev, Reason.EDGE_TOO_SMALL, f"pair {pair:.4f} > {cap:.2f}: WAIT")
            size = st.unmatched if sc.max_hedge_size <= 0 else min(st.unmatched, sc.max_hedge_size)
            venue_min = float(m.min_size) if m.min_size else 5.0
            if size + 1e-9 < venue_min:
                return self._reject(ev, Reason.SIZE_ZERO, f"unmatched {size:.2f} below the venue minimum")
            return self._entry(ev, meta, obook, other, size, "hedge", now, quote_ages, balance, max_price=0.99, fak=False)
        if sc.add_while_trending > 0 and st.adds < int(sc.add_while_trending) and not st.reversal:
            want = 1 if side == "UP" else -1
            if z5 * want >= sc.trend_z and z1 * want > 0:
                return self._entry(ev, meta, book_up if side == "UP" else book_down, side, sc.initial_entry_size,
                                   "add", now, quote_ages, balance, max_price=sc.max_initial_entry_price, fak=False)
        return self._reject(ev, Reason.ALREADY_POSITIONED, "holding to settlement" + (" (reversal, hedge waits)" if st.reversal else ""))

    def _entry(self, ev, meta, book, side, size, kind, now, quote_ages, balance, *, max_price, fak) -> Evaluation:
        cfg, sc, m = self.cfg, self.scfg, meta.market
        st = self._state(m.slug)
        token = m.yes_token_id if side == "UP" else m.no_token_id
        if book is None or book.best_ask is None or book.best_bid is None:
            return self._reject(ev, Reason.NO_OFFER, f"no {side} book")
        ask, bid = book.best_ask, book.best_bid
        qa = (quote_ages or {}).get(token) if quote_ages is not None else None
        if quote_ages is not None and qa is None:
            return self._reject(ev, Reason.STALE_DATA, f"no valid {side} book on the current feed connection")
        if qa is not None and qa > sc.max_quote_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"{side} quote {qa:.0f}ms > {sc.max_quote_age_ms:.0f}")
        if not (sc.min_ask <= ask <= min(sc.max_ask, max_price)):
            return self._reject(ev, Reason.PRICE_BAND, f"{side} ask {ask:.2f} outside {sc.min_ask:.2f}-{min(sc.max_ask, max_price):.2f}")
        if ask - bid > min(sc.max_spread, cfg.max_spread) + 1e-9:
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"spread {ask - bid:.3f}")
        limit = min(1.0 - (m.tick_size or 0.01), round(ask + min(sc.max_slippage, cfg.max_slippage), 4))
        venue_min = float(m.min_size) if m.min_size else 5.0
        size = max(venue_min, math.floor(size * 100.0) / 100.0)
        per_order = min(sc.max_order_usdc, self.risk.cfg.max_position_usdc)
        if size * limit > per_order + 1e-9:
            return self._reject(ev, Reason.RISK_LIMIT, f"{size:g} sh at {limit:.2f} exceeds the order cap {per_order:.2f}")
        exec_price, avail = get_executable_price(book, size, limit)
        if exec_price is None and not (fak and avail + 1e-9 >= venue_min):
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"{avail:.0f} shares under {limit:.2f}, need {size:g}")
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit, add_to_open=True)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)
        if size * limit > balance + 1e-9:
            return self._reject(ev, Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * limit:.2f}")
        if self.buys_last_hour(now) >= int(sc.max_buys_per_hour):
            return self._reject(ev, Reason.HOURLY_BUY_LIMIT, f"{len(self._buy_times)} buys in the last hour")
        f = ev.features["btc_hedge"]
        f.update({"signal": True, "direction": side, "kind": kind, "ask": ask, "limit": round(limit, 4), "size": size,
                  "fak": fak, "reason": st.reason})
        ev.features["execution"] = {"fair": None, "cap": round(limit, 4), "best_ask": ask, "best_bid": bid,
                                    "spread": round(ask - bid, 4), "quote_age_ms": None if qa is None else round(qa)}
        sv = SideView(side=side, token_id=token, p_model=ask, p_market=ask, ask=ask, bid=bid, depth_at_limit=avail,
                      costs=None, reason=None, entry_px=exec_price if exec_price is not None else ask)
        ev.sides = [sv]
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, limit, sv.entry_px
        ev.max_entry_price = limit
        ev.size, ev.net_edge, ev.confidence, ev.p_used = size, 0.0, 1.0, ask if side == "UP" else 1.0 - ask
        ev.size_note = f"{kind}: {size:g} shares ({size * limit:.2f} USDC)"
        self.last[m.slug] = ev
        return ev

    # ------------------------------------------------------------ the hooks

    def _next_pair(self, st: MarketState, now: float) -> None:
        """Close the books on a completed pair and start the next first leg from scratch."""
        st.pair_log.append({"side": st.side, "reason": st.reason, "first_qty": round(st.first_qty, 2),
                            "first_px": round(st.avg_first, 4) if st.avg_first else None,
                            "hedge_qty": round(st.hedge_qty, 2), "hedge_px": round(st.hedge_cost / st.hedge_qty, 4) if st.hedge_qty else None,
                            "pair_cost": st.best_pair})
        log.info("PAIR_DONE market=%s pair=%d side=%s first_px=%.4f hedge_px=%.4f", st.slug, st.pairs, st.side,
                 st.avg_first or 0.0, (st.hedge_cost / st.hedge_qty) if st.hedge_qty else 0.0)
        st.side = st.reason = None
        st.z1 = st.z5 = None
        st.attempts = st.hedge_attempts = st.adds = 0
        st.first_qty = st.first_cost = st.hedge_qty = st.hedge_cost = 0.0
        st.against_since = None
        st.reversal = False
        st.best_pair = None
        st.first_fill_ms = st.pair_done_ms = None
        st.last_check_ms = -math.inf

    def on_entry_submit(self, slug: str, now_ms: float) -> None:
        SpotLagEngine.on_entry_submit(self, slug, now_ms)
        st = self._state(slug)
        ev = self.last.get(slug)
        kind = (ev.features or {}).get("btc_hedge", {}).get("kind") if ev is not None else None
        st.pending = kind or "initial"
        if st.pending == "initial":
            st.attempts += 1
        elif st.pending == "hedge":
            st.hedge_attempts += 1
        elif st.pending == "add":
            st.adds += 1

    def on_entry_done(self, slug: str, now_ms: float) -> None:
        """The working order finished (filled, killed, cancelled)."""
        st = self.markets.get(slug)
        if st is not None:
            st.pending = None

    def on_fill_detail(self, slug: str, side: str, price: float, size: float, fee: float, now_ms: float) -> None:
        st = self._state(slug)
        if st.side is None:
            st.side = side
        if side == "UP":
            st.up_qty += size; st.up_cost += price * size
        else:
            st.dn_qty += size; st.dn_cost += price * size
        if side == st.side:
            first = st.first_qty <= 0.0
            st.first_qty += size
            st.first_cost += price * size
            if first:
                st.first_fill_ms = now_ms
            if first:
                log.info("INITIAL_ENTRY market=%s side=%s reason=%s price=%.4f quantity=%.2f btc_return_1s=%s btc_return_5s=%s",
                         slug, side, st.reason, price, size,
                         None if st.z1 is None else f"{st.z1:+.2f}s", None if st.z5 is None else f"{st.z5:+.2f}s")
        else:
            st.hedge_qty += size
            st.hedge_cost += price * size
            if st.hedge_qty >= st.first_qty - 1e-9:
                st.pairs += 1
                st.pair_done_ms = now_ms
        st.fees += fee
        st.pending = None

    def on_market_settled(self, slug: str, won_up: bool | None, pnl: float, now_ms: float) -> None:
        st = self.markets.get(slug)
        if st is None or st.done:
            return
        st.done = True
        won = None if won_up is None or st.side is None else (won_up == (st.side == "UP"))
        matched = min(st.up_qty, st.dn_qty)
        unmatched = (st.up_qty - st.dn_qty) if won_up is None else ((st.up_qty - st.dn_qty) if st.side == "UP" else (st.dn_qty - st.up_qty))
        # settlement from the per-side totals: every UP share pays 1 if UP won, every DOWN share if not
        payout = None if won_up is None else (st.up_qty if won_up else st.dn_qty)
        total_cost = st.up_cost + st.dn_cost
        net = None if payout is None else payout - total_cost - st.fees
        rec = {"slug": slug, "initial_side": st.side, "reason": st.reason, "initial_filled": round(st.first_qty, 2),
               "first_leg_cost": round(st.first_cost, 4), "hedge_filled": round(st.hedge_qty, 2),
               "second_leg_cost": round(st.hedge_cost, 4), "matched_qty": round(matched, 2), "unmatched_qty": round(unmatched, 2),
               "total_cost": round(total_cost, 4), "payout": None if payout is None else round(payout, 4),
               "fees": round(st.fees, 4), "settlement_pnl": None if payout is None else round(payout - total_cost, 4),
               "net_pnl": None if net is None else round(net, 4), "reversal": st.reversal, "best_pair": st.best_pair,
               "pairs": st.pairs, "pair_log": list(st.pair_log),
               "initial_attempts": st.attempts, "hedge_attempts": st.hedge_attempts, "ledger_pnl": round(pnl, 4)}
        self.ended.append(rec)
        if len(self.ended) > 200:
            del self.ended[:50]
        log.info("MARKET_END market=%s initial_side=%s initial_filled=%.2f hedge_filled=%.2f matched_qty=%.2f unmatched_qty=%.2f "
                 "first_leg_cost=%.4f second_leg_cost=%.4f payout=%s fees=%.4f settlement_pnl=%s net_pnl=%s reversal=%s",
                 slug, st.side, st.first_qty, st.hedge_qty, matched, unmatched, st.first_cost, st.hedge_cost,
                 rec["payout"], st.fees, rec["settlement_pnl"], rec["net_pnl"], st.reversal)

    # ------------------------------------------------------------ orders, exits

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        f = (ev.features or {}).get("btc_hedge", {})
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                     tif=TimeInForce.IOC if f.get("fak") else TimeInForce.FOK,
                     expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                     tag=f"{ev.side} btc_hedge {f.get('kind')} {f.get('reason') or ''} cap={ev.limit_price:.2f}")

    def revalidate(self, ev: Evaluation, book, min_edge: float | None = None) -> Reason | None:
        if book is None or book.best_ask is None:
            return Reason.NO_OFFER
        if book.best_ask > ev.limit_price + 1e-9:
            return Reason.HIGH_SLIPPAGE
        return None

    def signal_text(self, ev: Evaluation) -> str:
        f = (ev.features or {}).get("btc_hedge", {})
        return f"{str(f.get('kind', '')).upper()} {f.get('direction')} {f.get('reason') or ''} ask {f.get('ask')} z1 {f.get('z1')} z5 {f.get('z5')}"

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig: MomentumSignal | None = None, spread: float | None = None,
                      bid_depth: float | None = None, size: float | None = None,
                      data_age_ms: float | None = None) -> str | None:
        return None

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = "btc_trend_reversal_hedge"
        snap["btc_hedge_config"] = self.scfg.as_dict()
        snap["btc_hedge"] = {s: {"side": st.side, "reason": st.reason, "first_qty": st.first_qty, "hedge_qty": st.hedge_qty,
                                 "reversal": st.reversal, "attempts": st.attempts, "best_pair": st.best_pair}
                             for s, st in self.markets.items()}
        snap["btc_hedge_ended"] = self.ended[-20:]
        return snap
