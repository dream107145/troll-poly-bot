"""LATE HIGH-CONFIDENCE (the user's spec, 2026-10-08): in the last seconds of a 5-minute market, buy the token
already priced above 0.98 -- only when the crypto price is far enough from the strike, volatility-adjusted, that the
outcome is all but settled -- and hold it to resolution.

EXPERIMENTAL, PAPER ONLY. A separate strategy with its own id (``late_confidence``), env prefix
(``TPB_LATECONF_*``), budget and data files; behind ``TPB_LATECONF_ENABLED=1``, with NO real-money switch (__main__
refuses shadow, micro-live and live unconditionally). Not backtested: the spec's price x time-left x distance
optimisation has not been run.

Per market, on every evaluation:

    window     min_time_remaining_s <= time left <= max_time_remaining_s (30 s), and the order can land in time
    side       the token with the higher ask; its ask must be > min_ask (0.98) and <= max_ask
    distance   the settling TWAP's z-score toward that side (pricing.twap.twap_fair_value: the observed part of the
               60 s average plus the spot, against the remaining variance from the realised volatility) >= min_z (2)
    edge       P(side wins) - limit - taker fee(limit) - safety_margin >= 0, P from the same TWAP model, limit = ask +
               max_slippage (0.2c), capped at max_ask but never under the ask. The spread is a separate filter (<= max_spread): buying at the ask
               already pays it. The spec's literal "price + fees + spread + margin" (spread_in_edge=True) can never
               pass on a 1-cent book -- ask 0.99 / bid 0.98 needs P > 1.006 -- so it is off by default.
    order      fill-and-kill for size_shares (20); one position per market, max_attempts_per_market orders; at most
               max_simultaneous_positions (2) markets at once; never more than the cash available (the size is cut to
               what the balance pays for, never raised -- no size increase after a loss)
    exit       none: every position settles at 1 or 0. No stop-loss.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from ..features.engine import FeatureEngine
from ..pricing.twap import twap_fair_value
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason, SideView
from .momentum import MomentumConfig, MomentumSignal, get_executable_price
from .spot_lag import SpotLagConfig, SpotLagEngine

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_LATECONF_ENABLED"
STRATEGY_ID = "late_confidence"


@dataclass(slots=True)
class LateConfidenceConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_LATECONF_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    # --- the entry (the user's spec) ---
    min_ask: float = 0.98                     # the winning token's ask must be ABOVE this
    max_ask: float = 0.999
    min_z: float = 2.0                        # the settling TWAP's distance from the strike, in standard deviations
    safety_margin: float = 0.005              # P(win) must beat limit + fee by this much
    spread_in_edge: bool = False              # also charge the spread in the edge (the spec's literal rule; see above)
    max_spread: float = 0.03
    max_slippage: float = 0.002               # the limit sits this far above the seen ask (the edge is charged at it)
    slippage: float = 0.0
    uncertainty: float = 0.0
    min_net_edge: float = 0.0
    size_shares: float = 20.0
    size_mode: str = "fixed"
    order_type: str = "FAK"                   # fill-and-kill: what the book holds at the limit
    min_time_remaining_s: float = 2.0
    max_time_remaining_s: float = 30.0
    max_quote_age_ms: float = 1000.0
    max_spot_age_ms: float = 1000.0
    # --- exits: none (hold to resolution, no stop-loss) ---
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0
    # --- budget: a 40 USDC bankroll, 20 shares a position, two at once ---
    assets: str = "BTC,ETH,SOL,XRP,DOGE,BNB,HYPE"
    exclude_assets: str = ""
    max_simultaneous_positions: int = 2
    max_buys_per_hour: int = 60
    max_entries_per_market: int = 1
    max_attempts_per_market: int = 3
    max_order_usdc: float = 20.0
    max_total_exposure_usdc: float = 40.0
    max_daily_loss_usdc: float = 40.0
    fast_feed: bool = False
    k_sigma: float = 3.0

    def momentum_mirror(self) -> MomentumConfig:
        # explicit base call: a slots dataclass is rebuilt, which breaks zero-argument super()
        m = SpotLagConfig.momentum_mirror(self)
        m.max_hold_s = 1e9                     # nothing here sells on a timer
        return m

    def apply_budget(self, risk_cfg, order_ceiling: float | None = None) -> list[str]:
        """The spot-lag budget, plus the per-asset and per-window caps a 20-share order needs: the defaults are
        scaled to the balance (15 / 30 USDC per 100 -> 6 / 12 at 40), below one ~19.8 USDC position, which refused
        every order ("DOGE exposure cap", 2026-10-08 13:24). One position per asset; both positions may share a
        5-minute window."""
        changed = SpotLagConfig.apply_budget(self, risk_cfg, order_ceiling)
        # the total cap too: the base keeps the TIGHTER of this strategy's 40 and the default already scaled to the
        # balance (50 -> 20 at 40 USDC), which left room for ONE 20-share position although two are allowed
        # (2026-10-08 14:34: "exposure 19.44 of 20.00 USDC")
        total = float(self.max_total_exposure_usdc)
        if order_ceiling is None and risk_cfg.max_total_exposure_usdc != total:
            changed.append(f"max_total_exposure_usdc: {risk_cfg.max_total_exposure_usdc} -> {total}")
            risk_cfg.max_total_exposure_usdc = total
        for attr, new in (("max_asset_exposure_usdc", risk_cfg.max_position_usdc),
                          ("max_epoch_exposure_usdc", risk_cfg.max_total_exposure_usdc)):
            old = getattr(risk_cfg, attr)
            if new != old:
                setattr(risk_cfg, attr, new)
                changed.append(f"{attr}: {old} -> {new}")
        return changed


def winning_side(ask_up: float | None, ask_down: float | None) -> str | None:
    """The token the market already favours: the higher ask (None when a book is empty or the asks are equal)."""
    if ask_up is None or ask_down is None or abs(ask_up - ask_down) < 1e-12:
        return None
    return "UP" if ask_up > ask_down else "DOWN"


class LateConfidenceEngine(SpotLagEngine):
    """Same surface as the spot-lag engine (feeds, budget, order path); the late high-confidence decision inside."""

    feature_key = STRATEGY_ID
    #: the dashboard's shared control file may not lift this strategy's budget
    budget_is_ceiling = True

    def __init__(self, cfg: EngineConfig, scfg: LateConfidenceConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the late_confidence strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, LateConfidenceConfig):
            raise TypeError("LateConfidenceEngine needs a LateConfidenceConfig")
        super().__init__(cfg, scfg, risk, features)
        self.last_late: dict[str, dict] = {}

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        cfg, sc, m = self.cfg, self.scfg, meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        if not sc.asset_ok(m.asset):
            return self._reject(ev, Reason.ASSET_FILTERED, f"{m.asset} excluded")
        if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
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
        self._close_ts[m.slug] = m.close_ts
        self._forget_closed(now)
        if left * 1000.0 < latency.round_trip_ms() + cfg.latency_safety_margin_ms:
            return self._reject(ev, Reason.CANNOT_LAND_IN_TIME)

        side = winning_side(book_up.best_ask if book_up is not None else None,
                            book_down.best_ask if book_down is not None else None)
        if side is None:
            return self._reject(ev, Reason.NO_OFFER, "no ask on both books, or equal asks")
        book = book_up if side == "UP" else book_down
        ask, bid = book.best_ask, book.best_bid
        d = 1 if side == "UP" else -1
        token = m.yes_token_id if side == "UP" else m.no_token_id
        ev.p_market = book_up.mid if book_up is not None and book_up.mid is not None else None
        if not (ask > sc.min_ask + 1e-9 and ask <= sc.max_ask + 1e-9):
            return self._reject(ev, Reason.PRICE_BAND, f"{side} ask {ask:.3f} not in ({sc.min_ask:.2f}, {sc.max_ask:.3f}]")
        if bid is None:
            return self._reject(ev, Reason.NO_OFFER, f"no {side} bid")
        spread = ask - bid
        if spread > sc.max_spread + 1e-9:
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"spread {spread:.3f}")
        qa = (quote_ages or {}).get(token) if quote_ages is not None else book_age_ms
        if quote_ages is not None and qa is None:
            return self._reject(ev, Reason.STALE_DATA, f"no valid {side} book on the current feed connection")
        if qa is not None and qa > sc.max_quote_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"{side} quote {qa:.0f}ms > {sc.max_quote_age_ms:.0f}")

        sigma = vol.sigma_per_sec
        self.last_sigma[m.asset] = sigma
        fv = twap_fair_value(state=twap, strike=m.strike, sigma_per_sec=sigma, now_ms=now, close_ts_ms=m.close_ts,
                             info_lag_s=sc.info_lag_s)
        if abs(fv.p_up - 0.5) < 1e-12 and fv.z == 0.0 and getattr(fv, "uncertainty", 0.0) == 0.5:
            # the TWAP model's "cannot price" answer (no spot, state not ready, or the settling-average invariant
            # failed): not a reading of z = 0 (2026-10-08 14:50 ETH logged "P 0.5 z -0.00" as an evaluation)
            return self._reject(ev, Reason.MODEL_NOT_READY, "the TWAP model could not price this market")
        ev.p_analytic = fv.p_up
        p_side = fv.p_up if side == "UP" else 1.0 - fv.p_up
        z_side = d * float(fv.z)
        # the limit: ask + slippage, capped at max_ask -- NOT at 1 - the market's listed tick: near the extremes the
        # venue's tick drops to 0.001 (asks of 0.991..0.999) while the listing still says 0.01, and a 0.99 cap under a
        # 0.999 ask could never fill (2026-10-08 13:39 / 13:44: "0 shares at or under 0.990")
        limit = max(ask, min(sc.max_ask, round(ask + sc.max_slippage, 4)))
        fee = meta.fee.taker_fee_per_share(limit)
        edge = p_side - limit - fee - sc.safety_margin - (spread if sc.spread_in_edge else 0.0)
        info = {"side": side, "direction": side, "ask": ask, "bid": bid, "limit": round(limit, 4), "spread": round(spread, 4),
                "z": round(z_side, 3), "p_win": round(p_side, 5), "fee": round(fee, 5), "edge": round(edge, 5),
                "left_s": round(left, 2), "spot": spot.price, "strike": m.strike, "sigma_bps_s": round(sigma * 1e4, 3),
                "quote_age_ms": None if qa is None else round(qa), "spot_age_ms": round(spot_age_ms)}
        ev.features = {STRATEGY_ID: info,
                       "momentum": {"label": "LATE_" + side, "z": {"main": round(z_side, 3)}, "score": round(abs(z_side), 3)},
                       "execution": {"fair": round(p_side, 4), "cap": round(limit, 4), "best_ask": ask, "best_bid": bid,
                                     "spread": round(spread, 4), "quote_age_ms": info["quote_age_ms"],
                                     "spot_age_ms": round(spot_age_ms)}}
        ev.p_used = fv.p_up
        self.last_signal[m.asset] = MomentumSignal(direction=d, label="LATE_" + side, z={"main": z_side}, sigma_per_sec=sigma)
        sv = SideView(side=side, token_id=token, p_model=p_side, p_market=ask, ask=ask, bid=bid, depth_at_limit=0.0,
                      costs=None, reason=None, entry_px=None)
        ev.sides = [sv]
        self.last_late[m.asset] = {"slug": m.slug, "t": now, **info}
        if z_side < sc.min_z:
            sv.reason = Reason.WEAK_MOMENTUM
            return self._reject(ev, Reason.WEAK_MOMENTUM, f"{side} distance z {z_side:+.2f} < {sc.min_z:g}")
        if edge < sc.min_net_edge:
            sv.reason = Reason.EDGE_TOO_SMALL
            return self._reject(ev, Reason.EDGE_TOO_SMALL,
                                f"P {p_side:.4f} - limit {limit:.3f} - fee {fee:.4f} - margin {sc.safety_margin:g}"
                                + (f" - spread {spread:.3f}" if sc.spread_in_edge else "") + f" = {edge:+.4f}")
        # an OPPORTUNITY: every refusal from here is logged as a skip
        info["signal"] = True
        if self.trades.get(m.slug, 0) >= int(sc.max_entries_per_market):
            return self._reject(ev, Reason.MARKET_ENTERED, "this market already has its position")
        if self.attempts.get(m.slug, 0) >= int(sc.max_attempts_per_market):
            return self._reject(ev, Reason.MARKET_ATTEMPTS, f"{self.attempts[m.slug]} attempts already")
        if self.buys_last_hour(now) >= int(sc.max_buys_per_hour):
            return self._reject(ev, Reason.HOURLY_BUY_LIMIT, f"{len(self._buy_times)} buys in the last hour")
        venue_min = float(m.min_size) if m.min_size else 5.0
        size = max(venue_min, float(sc.size_shares))
        # never more than the cash available and the budget allows; the size is only ever cut, never raised
        bound = min(sc.max_order_usdc, self.risk.cfg.max_position_usdc, self.risk.capacity_total(), float(balance or 0.0))
        if size * limit > bound + 1e-9:
            fit = math.floor(bound / limit * 100.0) / 100.0
            if fit + 1e-9 < venue_min:
                return self._reject(ev, Reason.SIZE_ZERO, f"{bound:.2f} USDC available, the minimum order is {venue_min * limit:.2f}")
            info["size_reduced_from"] = size
            size = fit
        exec_price, avail = get_executable_price(book, size, limit)
        sv.depth_at_limit = avail
        if avail + 1e-9 < venue_min:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"{avail:.0f} shares at or under {limit:.3f}")
        sv.entry_px = exec_price if exec_price is not None else ask
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)
        info.update({"size": size, "worst_case_usdc": round(size * limit, 4), "depth_at_limit": round(avail, 2)})
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, limit, sv.entry_px
        ev.max_entry_price = limit
        ev.size, ev.net_edge, ev.confidence = size, edge, 1.0
        ev.size_note = f"{size:g} shares ({size * limit:.2f} USDC worst case)"
        self.last[m.slug] = ev
        return ev

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        lc = (ev.features or {}).get(STRATEGY_ID, {})
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                     tif=TimeInForce.IOC if self._fak else TimeInForce.FOK,
                     expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                     tag=f"{ev.side} late_confidence z={lc.get('z', 0):+.2f} p={lc.get('p_win', 0):.4f} cap={ev.limit_price:.3f}")

    def signal_text(self, ev: Evaluation) -> str:
        lc = (ev.features or {}).get(STRATEGY_ID, {})
        return (f"LATE {lc.get('side')} ask {lc.get('ask')} z {lc.get('z'):+} P {lc.get('p_win')} edge {lc.get('edge'):+} "
                f"{lc.get('left_s')} s left")

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig: MomentumSignal | None = None, spread: float | None = None,
                      bid_depth: float | None = None, size: float | None = None,
                      data_age_ms: float | None = None) -> str | None:
        """Never: the position settles at 1 or 0 (no stop-loss, no take-profit)."""
        return None

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = STRATEGY_ID
        snap["late_confidence_config"] = self.scfg.as_dict()
        snap["late_confidence_last"] = dict(self.last_late)
        return snap
