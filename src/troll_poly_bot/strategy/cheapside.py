"""CHEAPSIDE: buy the cheaper token of a 5-minute market when a model says it is
underpriced, and hold it to settlement.

EXPERIMENTAL, PAPER ONLY. The research verdict is in docs/cheapside_2026-09-29.md
(scripts/research_cheapside.py). It is a separate strategy with its own id
(``cheapside``), env prefix (``TPB_CHEAPSIDE_*``), budget, model file and data files;
spot-lag, momentum and repriced-momentum are untouched. It is behind
``TPB_CHEAPSIDE_ENABLED=1`` and has NO real-money switch: __main__ refuses micro-live
and live for it unconditionally.

Per market, every evaluation (the 200 ms tick, or an event when the fast feed is on)::

    cheap      the side with the lower best ask; only 0.05 <= ask <= 0.35, spread <= 3c,
               and the venue minimum showing at the touch
    model      P(cheap side settles as the winner): a logistic regression fitted on
               TRAINING windows and recalibrated on validation (data/replay/cheapside_model.json),
               from the ask, time left, the analytic TWAP fair value (spot distance from the
               strike against the settling average's remaining variance), the spot's 250 ms,
               500 ms, 1 s and 3 s returns in sigma (signed to the cheap side), their sign
               consistency and acceleration, realised vol, spot order flow, spread and depth
    support    the 1 s spot return points the cheap side's way and 2 of the 3 short horizons agree
    edge       model_p - limit - taker fee(limit) - slippage >= ``min_net_edge`` (5c), the limit
               being ask + ``max_slippage`` (1c)
    order      FOK for ``size_shares`` (10; at most ``max_order_usdc`` = 5 USDC), one entry per
               market, ``max_attempts_per_market`` (3) orders >= 2 s apart
    exit       none: every position settles at 1 / 0. The only ways out are the operator's
               kill file and the risk halts (no new entries; open positions settle within
               5 minutes). Cheap does NOT mean value: the model is the only reason to buy.

The 50 / 100 ms spot returns are recorded as diagnostics from the fast feed when it is
on; the archive the model was fitted on cannot resolve them, so they are not inputs.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from ..features.engine import FeatureEngine
from ..pricing.twap import twap_fair_value
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason, SideView
from .momentum import MomentumConfig, MomentumSignal, get_executable_price
from .spot_lag import SpotLagConfig, SpotLagEngine

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_CHEAPSIDE_ENABLED"
DEFAULT_MODEL_PATH = "data/replay/cheapside_model.json"
#: the model's inputs, in the order the file's coefficients are stored
FEATURES = ("logit_ask", "logit_fair", "z250", "z500", "z1000", "z3000", "consist", "accel", "log_sigma",
            "sqrt_left", "dist", "tfi", "obi", "spread_c", "log_depth")


def _logit(p: float) -> float:
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-min(max(x, -30.0), 30.0)))


def cheap_side(ask_up: float | None, ask_down: float | None) -> str | None:
    """The side with the strictly lower best ask; None without both, or on a tie."""
    if ask_up is None or ask_down is None or ask_up == ask_down:
        return None
    return "UP" if ask_up < ask_down else "DOWN"


def features_for(*, ask: float, bid: float, depth: float, fair_side: float, left: float, sigma: float,
                 spot: float, strike: float, direction: int, r250: float, r500: float, r1000: float, r3000: float,
                 tfi: float | None, obi: float | None) -> dict[str, float]:
    """The model's inputs from live values (research_cheapside.candidates mirrors this)."""
    sig = max(sigma, 1e-7)
    z = {h: r / (sig * math.sqrt(h / 1000.0)) * direction for h, r in ((250, r250), (500, r500), (1000, r1000), (3000, r3000))}
    consist = float(sum(1 for h in (250, 500, 1000) if z[h] > 0))
    dist = math.log(spot / strike) / (sig * math.sqrt(max(left, 1.0))) * direction if strike > 0 and spot > 0 else 0.0
    return {
        "logit_ask": _logit(ask), "logit_fair": _logit(fair_side), "z250": z[250], "z500": z[500],
        "z1000": z[1000], "z3000": z[3000], "consist": consist, "accel": z[500] - z[1000] / math.sqrt(2.0),
        "log_sigma": math.log(sig * 1e4), "sqrt_left": math.sqrt(max(left, 0.0) / 300.0),
        "dist": min(6.0, max(-6.0, dist)), "tfi": (tfi or 0.0) * direction, "obi": (obi or 0.0) * direction,
        "spread_c": (ask - bid) * 100.0, "log_depth": math.log1p(max(depth, 0.0)),
    }


def supported(f: dict[str, float]) -> bool:
    """The spot signal backs the cheap side: the last second went its way and at
    least two of the three short horizons agree."""
    return f["z1000"] > 0.0 and f["consist"] >= 2.0


class CheapSideModel:
    """The fitted logistic regression: standardised inputs, coefficients, Platt recalibration."""

    def __init__(self, doc: dict) -> None:
        feats = tuple(doc.get("features") or FEATURES)
        if feats != FEATURES:
            raise ValueError(f"cheapside model features {feats} differ from the engine's {FEATURES}")
        self.beta = [float(x) for x in doc["beta"]]
        self.mu = [float(x) for x in doc["mu"]]
        self.sd = [float(x) for x in doc["sd"]]
        if len(self.beta) != len(FEATURES) + 1 or len(self.mu) != len(FEATURES) or len(self.sd) != len(FEATURES):
            raise ValueError("cheapside model: coefficient count does not match the features")
        pl = doc.get("platt") or {"a": 0.0, "b": 1.0}
        self.a, self.b = float(pl["a"]), float(pl["b"])
        self.rules = dict(doc.get("rules") or {})

    @classmethod
    def load(cls, path: str | Path) -> "CheapSideModel":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def raw(self, f: dict[str, float]) -> float:
        x = self.beta[0]
        for k, name in enumerate(FEATURES):
            x += self.beta[k + 1] * ((float(f.get(name, 0.0)) - self.mu[k]) / self.sd[k])
        return x

    def predict(self, f: dict[str, float]) -> float:
        return _sigmoid(self.a + self.b * self.raw(f))


@dataclass(slots=True)
class CheapSideConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_CHEAPSIDE_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    #: the fitted model (scripts/research_cheapside.py writes it)
    model_path: str = DEFAULT_MODEL_PATH
    #: the spot must back the cheap side (features.supported)
    require_support: bool = True
    # --- the entry (the research rules) ---
    min_ask: float = 0.05
    max_ask: float = 0.35
    max_spread: float = 0.03
    min_net_edge: float = 0.05
    slippage: float = 0.005
    uncertainty: float = 0.0
    max_slippage: float = 0.01                # the limit sits 1c above the seen ask
    size_shares: float = 10.0
    size_mode: str = "fixed"
    order_type: str = "FOK"
    min_time_remaining_s: float = 15.0
    max_time_remaining_s: float = 295.0
    max_quote_age_ms: float = 1000.0
    max_spot_age_ms: float = 1000.0
    # --- exits: none ---
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0
    # --- budget ---
    assets: str = "BTC,ETH,SOL,XRP,DOGE,BNB,HYPE"
    exclude_assets: str = ""
    max_simultaneous_positions: int = 4
    max_buys_per_hour: int = 60
    max_entries_per_market: int = 1
    max_attempts_per_market: int = 3
    max_order_usdc: float = 5.0
    max_total_exposure_usdc: float = 20.0
    max_daily_loss_usdc: float = 10.0
    # --- the fast feed: 50 / 100 ms diagnostics ---
    fast_feed: bool = True
    fast_horizons_ms: str = "50,100,250,500"
    k_sigma: float = 3.0

    def momentum_mirror(self) -> MomentumConfig:
        # explicit base call: a slots dataclass is rebuilt, which breaks zero-argument super()
        m = SpotLagConfig.momentum_mirror(self)
        m.max_hold_s = 1e9                     # nothing here sells on a timer
        return m


class CheapSideEngine(SpotLagEngine):
    """Same surface as the spot-lag engine; the cheap-side decision inside."""

    feature_key = "cheapside"
    #: the dashboard's shared control file may not lift this strategy's budget
    budget_is_ceiling = True

    def __init__(self, cfg: EngineConfig, scfg: CheapSideConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the cheapside strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, CheapSideConfig):
            raise TypeError("CheapSideEngine needs a CheapSideConfig")
        # the parent checks the flag on the same field; the config carries its own env name
        super().__init__(cfg, scfg, risk, features)
        self.model = CheapSideModel.load(scfg.model_path)
        self.last_cheapside: dict[str, dict] = {}

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
        self._close_ts[m.slug] = m.close_ts
        self._forget_closed(now)
        if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
        if left * 1000.0 < latency.round_trip_ms() + cfg.latency_safety_margin_ms:
            return self._reject(ev, Reason.CANNOT_LAND_IN_TIME)

        ask_up = book_up.best_ask if book_up is not None else None
        ask_dn = book_down.best_ask if book_down is not None else None
        side = cheap_side(ask_up, ask_dn)
        if side is None:
            return self._reject(ev, Reason.NO_OFFER, "no ask on both books, or equal asks")
        book = book_up if side == "UP" else book_down
        ask, bid = book.best_ask, book.best_bid
        d = 1 if side == "UP" else -1
        token = m.yes_token_id if side == "UP" else m.no_token_id
        ev.p_market = book_up.mid if book_up is not None and book_up.mid is not None else None
        if not (sc.min_ask <= ask <= sc.max_ask):
            return self._reject(ev, Reason.PRICE_BAND, f"{side} ask {ask:.2f} outside {sc.min_ask:.2f}-{sc.max_ask:.2f}")
        if bid is None:
            return self._reject(ev, Reason.NO_OFFER, f"no {side} bid")
        spread = ask - bid
        if spread > min(sc.max_spread, cfg.max_spread) + 1e-9:
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
        ev.p_analytic = fv.p_up
        hist = self.features.spot.get(m.asset)
        rets = {h: (hist.logret(now, h) if hist is not None else None) for h in (0.05, 0.1, 0.25, 0.5, 1.0, 3.0)}
        if any(rets[h] is None for h in (0.25, 0.5, 1.0, 3.0)) or sigma <= 0.0:
            return self._reject(ev, Reason.MODEL_NOT_READY, "spot history")
        of = orderflow or {}
        depth = book.asks[0].size if book.asks else 0.0
        f = features_for(ask=ask, bid=bid, depth=depth, fair_side=fv.p_up if side == "UP" else 1.0 - fv.p_up,
                         left=left, sigma=sigma, spot=spot.price, strike=m.strike, direction=d,
                         r250=rets[0.25], r500=rets[0.5], r1000=rets[1.0], r3000=rets[3.0],
                         tfi=of.get("tfi15", of.get("tfi")), obi=of.get("obi"))
        p = self.model.predict(f)
        limit = min(1.0 - (m.tick_size or 0.01), round(ask + min(sc.max_slippage, cfg.max_slippage), 4))
        fee = meta.fee.taker_fee_per_share(limit)
        edge = p - limit - fee - sc.slippage - sc.uncertainty
        info = {"side": side, "direction": side, "ask": ask, "bid": bid, "limit": round(limit, 4), "p_model": round(p, 4),
                "p_fair": round(f["logit_fair"], 3), "edge": round(edge, 4), "z250": round(f["z250"], 3),
                "z500": round(f["z500"], 3), "z1000": round(f["z1000"], 3), "consist": f["consist"],
                "accel": round(f["accel"], 3), "dist": round(f["dist"], 3), "sigma_bps_s": round(sigma * 1e4, 3),
                "quote_age_ms": None if qa is None else round(qa), "spot_age_ms": round(spot_age_ms),
                "support": supported(f)}
        for h in (0.05, 0.1):
            if rets[h] is not None:
                info[f"z{int(h * 1000)}"] = round(rets[h] / (sigma * math.sqrt(h)) * d, 3)
        ev.features = {"cheapside": info,
                       "momentum": {"label": "CHEAP_" + side, "z": {"main": round(f["z1000"], 3), "3s": round(f["z3000"], 3)},
                                    "score": round(abs(f["z1000"]), 3)},
                       "execution": {"fair": round(p, 4), "cap": round(limit, 4), "best_ask": ask, "best_bid": bid,
                                     "spread": round(spread, 4), "quote_age_ms": info["quote_age_ms"],
                                     "spot_age_ms": round(spot_age_ms)}}
        ev.spot_move_bps = rets[1.0] * 1e4
        ev.p_used = p if side == "UP" else 1.0 - p
        self.last_signal[m.asset] = MomentumSignal(direction=d, label=ev.features["momentum"]["label"],
                                                   z={"main": f["z1000"], "3s": f["z3000"]}, sigma_per_sec=sigma)
        sv = SideView(side=side, token_id=token, p_model=p, p_market=ask, ask=ask, bid=bid, depth_at_limit=0.0,
                      costs=None, reason=None, entry_px=None)
        ev.sides = [sv]
        self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, **info}
        if sc.require_support and not supported(f):
            sv.reason = Reason.WEAK_MOMENTUM
            return self._reject(ev, Reason.WEAK_MOMENTUM, f"spot does not back {side}: z1 {f['z1000']:+.2f}, {f['consist']:.0f}/3 agree")
        if edge < sc.min_net_edge:
            sv.reason = Reason.EDGE_TOO_SMALL
            return self._reject(ev, Reason.EDGE_TOO_SMALL, f"model {p:.3f} - limit {limit:.2f} - costs = {edge:+.3f} < {sc.min_net_edge}")
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
        per_order = min(sc.max_order_usdc, self.risk.cfg.max_position_usdc)
        room = self.risk.capacity_total()
        bound = min(per_order, room)
        if size * limit > bound + 1e-9:
            fit = math.floor(bound / limit * 100.0) / 100.0
            if fit + 1e-9 < venue_min:
                return self._reject(ev, Reason.EXPOSURE_CAP if room < per_order else Reason.RISK_LIMIT,
                                    f"{bound:.2f} USDC of room, the minimum order is {venue_min * limit:.2f}")
            info["size_reduced_from"] = size
            size = fit
        exec_price, avail = get_executable_price(book, size, limit)
        sv.depth_at_limit = avail
        if exec_price is None:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"{avail:.0f} shares under {limit:.2f}, need {size:.0f}")
        sv.entry_px = exec_price
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)
        if size * limit > balance + 1e-9:
            return self._reject(ev, Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * limit:.2f}")
        info.update({"size": size, "worst_case_usdc": round(size * limit, 4)})
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, limit, exec_price
        ev.max_entry_price = limit
        ev.size, ev.net_edge, ev.confidence = size, edge, 1.0
        ev.size_note = f"{size:g} shares ({size * limit:.2f} USDC worst case)"
        self.last[m.slug] = ev
        return ev

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        cs = (ev.features or {}).get("cheapside", {})
        # 2026-10-07: the order type was hard-coded FOK whatever TPB_CHEAPSIDE_ORDER_TYPE said; on the thin books of
        # cheap tokens (12-60 shares at the ask) 14 of 15 all-or-nothing orders were killed. FAK takes what is there.
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                     tif=TimeInForce.IOC if self._fak else TimeInForce.FOK,
                     expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                     tag=f"{ev.side} cheapside p={cs.get('p_model', 0):.3f} edge={ev.net_edge:+.3f} cap={ev.limit_price:.2f}")

    def signal_text(self, ev: Evaluation) -> str:
        cs = (ev.features or {}).get("cheapside", {})
        return (f"CHEAP {cs.get('side')} ask {cs.get('ask')} model {cs.get('p_model')} edge {cs.get('edge'):+} "
                f"z1 {cs.get('z1000'):+} agree {cs.get('consist')}/3")

    # ---------------------------------------------------------------- exits

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig: MomentumSignal | None = None, spread: float | None = None,
                      bid_depth: float | None = None, size: float | None = None,
                      data_age_ms: float | None = None) -> str | None:
        """Never: the position settles. (The kill file and the risk halts stop new
        entries; nothing here sells a position early.)"""
        return None

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = "cheapside"
        snap["cheapside_config"] = self.scfg.as_dict()
        snap["cheapside"] = dict(self.last_cheapside)
        snap["cheapside_model"] = {"path": self.scfg.model_path, "platt": [self.model.a, self.model.b],
                                   "rules": self.model.rules}
        return snap
