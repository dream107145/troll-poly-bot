"""FASTCLIP: the x-moneyforwhiskas pattern (docs/trader_x-moneyforwhiskas_2026-09-29.md) at our
speed -- many small taker clips on whichever side the spot is moving toward, both sides allowed,
every position held to settlement.

EXPERIMENTAL, PAPER ONLY. The research verdict is in docs/fastclip_2026-09-30.md
(scripts/research_fastclip.py). It is a separate strategy with its own id (``fastclip``), env
prefix (``TPB_FASTCLIP_*``), budget and data files; spot-lag, cheapside and the momentum strategies
are untouched. It is behind ``TPB_FASTCLIP_ENABLED=1`` and has NO real-money switch: __main__
refuses micro-live, live and shadow for it unconditionally.

What the trader does, and what this engine does about each part::

    market     BTC 5-minute only, every window           -> ``assets`` (default BTC)
    trigger    the first tick of a spot move on a fast   -> the spot's return over ``trigger_ms``,
               source, ~1 s before the token reprices       in sigma, >= ``k_sigma``, on the composite
                                                            or on whichever exchange moved most
                                                            (``source`` = comp | lead)
    value      (implicit: it buys only what reprices)    -> the side's mid 1 s ago plus the change in
                                                            the market's own fair-value curve since
                                                            then; ``lead`` adds the move the composite
                                                            has not shown yet through the model's slope
    entry      takes the ask in small clips               -> FAK at ask + ``limit_offset`` when
                                                            value - ask - taker fee >= ``min_edge``,
                                                            up to ``clip_shares`` / ``clip_usdc``
    clips      ~43 per market, both sides in 97 %         -> one order in flight, the same side again
                                                            only ``same_side_gap_ms`` later, at most
                                                            ``max_orders_per_market`` and
                                                            ``max_market_usdc`` per market; both sides
    timing     8 s after the open to ~60 s before close   -> ``min/max_time_remaining_s``
    exit       none; settles at 1 / 0                    -> none
    makers     resting bids, no edge, earn rebates       -> not copied: at our volume there is no
                                                            rebate tier, and the study found no edge
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

from ..features.engine import FeatureEngine, SpotHistory
from ..pricing.twap import twap_fair_value
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason, SideView
from .momentum import MomentumConfig, MomentumSignal
from .spot_lag import SpotLagConfig, SpotLagEngine, market_implied_fair

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_FASTCLIP_ENABLED"
EXCHANGES = ("binance", "bybit", "coinbase")


def norm_pdf_of_quantile(p: float) -> float:
    """phi(Phi^-1(p)): the normal density at the quantile of ``p`` (Newton on erf)."""
    p = min(max(p, 1e-4), 1.0 - 1e-4)
    z = 0.0
    for _ in range(40):
        cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        pdf = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
        step = (cdf - p) / max(pdf, 1e-12)
        z -= step
        if abs(step) < 1e-10:
            break
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def settle_sigma(sigma: float, seconds_left: float) -> float:
    """Standard deviation of the settling 60 s TWAP's log price (pricing/twap.py's variance)."""
    u = max(seconds_left, 0.0)
    var = u - 40.0 if u >= 60.0 else u ** 3 / 10800.0
    return max(sigma, 1e-7) * math.sqrt(max(var, 1.0))


def lead_adjusted_p(p_up: float, extra_logret: float, sigma: float, seconds_left: float) -> float:
    """The model P(up) moved by a log-price change the composite has not shown yet, through its
    local slope: dp = phi(Phi^-1(p)) * dlnS / sigma_settle."""
    dp = norm_pdf_of_quantile(p_up) * extra_logret / settle_sigma(sigma, seconds_left)
    return min(max(p_up + dp, 0.001), 0.999)


@dataclass(slots=True)
class FastClipConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_FASTCLIP_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    # --- the trigger ---
    #: "comp" = the composite spot; "lead" = whichever exchange moved most over ``trigger_ms``.
    #: The defaults are the research lock (data/replay/fastclip_locked.json: best train +
    #: validation net of 108 rules): composite, 500 ms, 1.5 sigma, no edge margin, no chase.
    source: str = "comp"
    trigger_ms: float = 500.0
    k_sigma: float = 1.5
    #: the value reference: the side's mid this long ago
    lookback_ms: float = 1000.0
    # --- the entry ---
    min_edge: float = 0.0
    limit_offset: float = 0.0
    min_ask: float = 0.10
    max_ask: float = 0.90
    max_spread: float = 0.03
    max_slippage: float = 0.01               # the engine-level bound on limit_offset
    clip_shares: float = 16.0
    clip_usdc: float = 7.0
    order_type: str = "FAK"
    min_time_remaining_s: float = 60.0
    max_time_remaining_s: float = 292.0
    max_quote_age_ms: float = 1000.0
    max_spot_age_ms: float = 1000.0
    # --- the clips ---
    same_side_gap_ms: float = 1000.0
    max_orders_per_market: int = 40
    max_market_usdc: float = 40.0
    # --- exits: none ---
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0
    # --- budget ---
    assets: str = "BTC"
    exclude_assets: str = ""
    max_simultaneous_positions: int = 2
    max_buys_per_hour: int = 600
    max_entries_per_market: int = 1_000
    max_attempts_per_market: int = 1_000
    max_order_usdc: float = 7.0
    max_total_exposure_usdc: float = 80.0
    max_daily_loss_usdc: float = 40.0

    def momentum_mirror(self) -> MomentumConfig:
        # explicit base call: a slots dataclass is rebuilt, which breaks zero-argument super()
        m = SpotLagConfig.momentum_mirror(self)
        m.max_hold_s = 1e9                     # nothing here sells on a timer
        return m

    def apply_budget(self, risk_cfg, order_ceiling: float | None = None) -> list[str]:
        """The spot-lag budget (positions, exposure, daily loss, order cap), then the per-asset and
        per-window caps SET to what stacking clips needs: a market may hold ``max_market_usdc``, a
        window ``max_market_usdc`` per asset. Paper only -- __main__ refuses real money for it."""
        changed = SpotLagConfig.apply_budget(self, risk_cfg, order_ceiling)
        n_assets = max(1, len([a for a in self.assets.split(",") if a.strip()]) or 7)
        for attr, new in (("max_asset_exposure_usdc", self.max_market_usdc),
                          ("max_epoch_exposure_usdc", self.max_market_usdc * n_assets),
                          ("max_total_exposure_usdc", self.max_total_exposure_usdc),
                          ("max_drawdown_usdc", max(risk_cfg.max_drawdown_usdc, 2.0 * self.max_daily_loss_usdc))):
            old = getattr(risk_cfg, attr)
            if old != new:
                setattr(risk_cfg, attr, new)
                changed.append(f"{attr}: {old} -> {new}")
        return changed


class FastClipEngine(SpotLagEngine):
    """Same surface as the spot-lag engine; the clip decision inside."""

    feature_key = "fastclip"
    #: the dashboard's shared control file may not lift this strategy's budget
    budget_is_ceiling = True
    #: the live bot may add to a market that already holds a position (live._confirm_entry)
    stacks_positions = True
    #: the live bot's per-market retry throttle, ms (live._entry_throttled): clips are repeated
    #: on purpose; the engine's own same-side gap and one-order-in-flight rule pace them
    entry_retry_ms = 250.0

    def __init__(self, cfg: EngineConfig, scfg: FastClipConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the fastclip strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, FastClipConfig):
            raise TypeError("FastClipEngine needs a FastClipConfig")
        if scfg.source not in ("comp", "lead"):
            raise ValueError(f"fastclip source {scfg.source!r}: comp | lead")
        super().__init__(cfg, scfg, risk, features)
        #: asset -> exchange -> sampled price history, for the "lead" trigger
        self._xhist: dict[str, dict[str, SpotHistory]] = {}
        #: (slug, side) -> when the last clip on that side was decided
        self._last_side: dict[tuple[str, str], float] = {}
        #: slug -> orders decided in that market
        self.orders_in: dict[str, int] = {}
        self.last_fastclip: dict[str, dict] = {}

    def _forget_closed(self, now_ms: float) -> None:
        gone = [s for s, c in self._close_ts.items() if now_ms > c + 60_000.0]
        SpotLagEngine._forget_closed(self, now_ms)
        for slug in gone:
            self.orders_in.pop(slug, None)
            self._last_side.pop((slug, "UP"), None)
            self._last_side.pop((slug, "DOWN"), None)

    def _trigger(self, asset: str, now: float, sigma: float, spot) -> tuple[float, float, str] | None:
        """(z of the trigger, the log move the composite has not shown, the source that fired)."""
        sc = self.scfg
        h = sc.trigger_ms / 1000.0
        hist = self.features.spot.get(asset)
        rc = hist.logret(now, h) if hist is not None else None
        if rc is None:
            return None
        scale = sigma * math.sqrt(h)
        best = (rc / scale, 0.0, "composite")
        if sc.source != "lead":
            return best
        xh = self._xhist.setdefault(asset, {})
        for name, px in (getattr(spot, "sources", None) or {}).items():
            if px and px > 0:
                xh.setdefault(name, SpotHistory(min_gap_ms=150.0)).update(float(px), now)
        for name, hx in xh.items():
            rx = hx.logret(now, h)
            if rx is None:
                continue
            zx = rx / scale
            if abs(zx) > abs(best[0]):
                best = (zx, rx - rc, name)
        return best

    # --------------------------------------------------------------- entry

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        cfg, sc, m = self.cfg, self.scfg, meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        if not sc.asset_ok(m.asset):
            return self._reject(ev, Reason.ASSET_FILTERED, f"{m.asset} excluded")
        if spot is None or spot_age_ms is None:
            return self._reject(ev, Reason.STALE_DATA, "no spot")
        if spot_age_ms > min(cfg.max_spot_age_ms, sc.max_spot_age_ms):
            return self._reject(ev, Reason.STALE_DATA, f"spot {spot_age_ms:.0f}ms")
        if book_age_ms is None or book_age_ms > cfg.max_book_age_ms:
            return self._reject(ev, Reason.STALE_DATA, "book" if book_age_ms is None else f"book {book_age_ms:.0f}ms")
        if m.strike <= 0.0:
            return self._reject(ev, Reason.NO_STRIKE)
        if vol is None or not getattr(vol, "ready", False) or twap is None or not twap.ready:
            return self._reject(ev, Reason.MODEL_NOT_READY)
        sigma = vol.sigma_per_sec
        self.last_sigma[m.asset] = sigma
        fv = twap_fair_value(state=twap, strike=m.strike, sigma_per_sec=sigma, now_ms=now, close_ts_ms=m.close_ts,
                             info_lag_s=sc.info_lag_s)
        ev.p_analytic = fv.p_up
        mid_up = book_up.mid if book_up is not None else None
        if mid_up is None and book_down is not None and book_down.mid is not None:
            mid_up = 1.0 - book_down.mid
        ev.p_market = mid_up
        mfv_now = market_implied_fair(fv.p_up, left)
        before = self._history_at(m.slug, now - sc.lookback_ms)
        if mid_up is not None and mfv_now is not None:
            h = self._hist.setdefault(m.slug, deque())
            if not h or now > h[-1][0]:
                h.append((now, mid_up, mfv_now))
            while h and h[0][0] < now - 10_000.0:
                h.popleft()
        self._close_ts[m.slug] = m.close_ts
        self._forget_closed(now)
        trig = self._trigger(m.asset, now, sigma, spot)
        if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
        if left * 1000.0 < latency.round_trip_ms() + cfg.latency_safety_margin_ms:
            return self._reject(ev, Reason.CANNOT_LAND_IN_TIME)
        if trig is None or sigma <= 0.0:
            return self._reject(ev, Reason.MODEL_NOT_READY, "spot history")
        z, extra, src = trig
        d = 1 if z > 0 else -1
        side = "UP" if d > 0 else "DOWN"
        info = {"z": round(z, 3), "source": src, "extra_bps": round(extra * 1e4, 2), "side": side, "direction": side,
                "sigma_bps_s": round(sigma * 1e4, 3)}
        ev.features = {"fastclip": info,
                       "momentum": {"label": ("CLIP_UP" if d > 0 else "CLIP_DOWN") if abs(z) >= sc.k_sigma else "NONE",
                                    "z": {"main": round(z, 3)}, "score": round(abs(z), 3)}}
        self.last_signal[m.asset] = MomentumSignal(direction=d if abs(z) >= sc.k_sigma else 0,
                                                   label=ev.features["momentum"]["label"], z={"main": z},
                                                   sigma_per_sec=sigma)
        self.last_fastclip[m.asset] = {"slug": m.slug, "t": now, **info}
        if abs(z) < sc.k_sigma:
            return self._reject(ev, Reason.WEAK_MOMENTUM, f"z {z:+.2f} < {sc.k_sigma} ({src})")
        token = m.yes_token_id if side == "UP" else m.no_token_id
        book = book_up if side == "UP" else book_down
        if book is None or book.best_ask is None or book.best_bid is None:
            return self._reject(ev, Reason.NO_OFFER, f"no {side} book")
        ask, bid = book.best_ask, book.best_bid
        if not (sc.min_ask <= ask <= sc.max_ask):
            return self._reject(ev, Reason.PRICE_BAND, f"{side} ask {ask:.2f}")
        if ask - bid > min(sc.max_spread, cfg.max_spread) + 1e-9:
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"spread {ask - bid:.3f}")
        qa = (quote_ages or {}).get(token) if quote_ages is not None else book_age_ms
        if quote_ages is not None and qa is None:
            return self._reject(ev, Reason.STALE_DATA, f"no valid {side} book on the current feed connection")
        if qa is not None and qa > sc.max_quote_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"{side} quote {qa:.0f}ms > {sc.max_quote_age_ms:.0f}")
        if mfv_now is None or before is None:
            return self._reject(ev, Reason.MODEL_NOT_READY, "no token / fair-value history")
        mid_b, mfv_b = before
        mfv_adj = mfv_now
        if extra:
            mfv_adj = market_implied_fair(lead_adjusted_p(fv.p_up, extra, sigma, left), left) or mfv_now
        d_mfv = mfv_adj - mfv_b
        value = (mid_b + d_mfv) if side == "UP" else ((1.0 - mid_b) - d_mfv)
        value = min(max(value, 0.0), 1.0)
        fee = meta.fee.taker_fee_per_share(ask)
        edge = value - ask - fee
        limit = min(1.0 - (m.tick_size or 0.01), round(ask + min(sc.limit_offset, sc.max_slippage, cfg.max_slippage), 4))
        ev.p_used = value if side == "UP" else 1.0 - value
        info.update({"ask": ask, "bid": bid, "value": round(value, 4), "edge": round(edge, 4), "limit": round(limit, 4),
                     "quote_age_ms": None if qa is None else round(qa), "spot_age_ms": round(spot_age_ms)})
        ev.features["execution"] = {"fair": round(value, 4), "cap": round(limit, 4), "best_ask": ask, "best_bid": bid,
                                    "spread": round(ask - bid, 4), "quote_age_ms": info["quote_age_ms"],
                                    "spot_age_ms": round(spot_age_ms)}
        sv = SideView(side=side, token_id=token, p_model=value, p_market=ask, ask=ask, bid=bid, depth_at_limit=0.0,
                      costs=None, reason=None, entry_px=None)
        ev.sides = [sv]
        if edge < sc.min_edge - 1e-12:
            sv.reason = Reason.EDGE_TOO_SMALL
            return self._reject(ev, Reason.EDGE_TOO_SMALL, f"value {value:.3f} - ask {ask:.2f} - fee {fee:.3f} = {edge:+.3f}")
        info["signal"] = True
        # the clips' own pacing and per-market budget
        last = self._last_side.get((m.slug, side))
        if last is not None and now - last < sc.same_side_gap_ms:
            return self._reject(ev, Reason.COOLDOWN, f"{side} clip {now - last:.0f}ms after the last")
        if self.orders_in.get(m.slug, 0) >= int(sc.max_orders_per_market):
            return self._reject(ev, Reason.MARKET_ATTEMPTS, f"{self.orders_in[m.slug]} orders already")
        if self.buys_last_hour(now) >= int(sc.max_buys_per_hour):
            return self._reject(ev, Reason.HOURLY_BUY_LIMIT, f"{len(self._buy_times)} buys in the last hour")
        held = self.risk.open.get(m.slug)
        committed = held.usdc if held is not None else 0.0
        room_mkt = sc.max_market_usdc - committed
        venue_min = float(m.min_size) if m.min_size else 5.0
        usdc = min(sc.clip_usdc, sc.max_order_usdc, room_mkt, self.risk.cfg.max_position_usdc, self.risk.capacity_total())
        size = min(sc.clip_shares, math.floor(usdc / limit * 100.0) / 100.0)
        if size + 1e-9 < venue_min:
            return self._reject(ev, Reason.EXPOSURE_CAP,
                                f"{committed:.2f} of {sc.max_market_usdc:.2f} USDC in this market; a clip needs "
                                f"{venue_min * limit:.2f}")
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit, add_to_open=True)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)
        if size * limit > balance + 1e-9:
            return self._reject(ev, Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * limit:.2f}")
        sv.entry_px, sv.depth_at_limit = ask, float(book.asks[0].size if book.asks else 0.0)
        self._last_side[(m.slug, side)] = now
        self.orders_in[m.slug] = self.orders_in.get(m.slug, 0) + 1
        info.update({"size": size, "worst_case_usdc": round(size * limit, 4), "market_usdc": round(committed, 4)})
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, limit, ask
        ev.max_entry_price = limit
        ev.size, ev.net_edge, ev.confidence = size, edge, 1.0
        ev.size_note = f"{size:g} shares clip ({size * limit:.2f} USDC; {committed:.2f} already in this market)"
        self.last[m.slug] = ev
        return ev

    def revalidate(self, ev: Evaluation, book, min_edge: float | None = None) -> Reason | None:
        """A FAK takes what is at the limit: the ask must still be at or under it."""
        if book is None or book.best_ask is None:
            return Reason.NO_OFFER
        if book.best_ask > ev.limit_price + 1e-9:
            return Reason.HIGH_SLIPPAGE
        return None

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        f = (ev.features or {}).get("fastclip", {})
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size, tif=TimeInForce.IOC,
                     expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                     tag=f"{ev.side} fastclip z={f.get('z', 0):+.2f} {f.get('source', '')} edge={ev.net_edge:+.3f} "
                         f"cap={ev.limit_price:.2f}")

    def signal_text(self, ev: Evaluation) -> str:
        f = (ev.features or {}).get("fastclip", {})
        return (f"CLIP {f.get('side')} z {f.get('z'):+} ({f.get('source')}) ask {f.get('ask')} value {f.get('value')} "
                f"edge {f.get('edge'):+}")

    # ---------------------------------------------------------------- exits

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig: MomentumSignal | None = None, spread: float | None = None,
                      bid_depth: float | None = None, size: float | None = None,
                      data_age_ms: float | None = None) -> str | None:
        """Never: every clip settles at 1 / 0."""
        return None

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = "fastclip"
        snap["fastclip_config"] = self.scfg.as_dict()
        snap["fastclip"] = dict(self.last_fastclip)
        return snap
