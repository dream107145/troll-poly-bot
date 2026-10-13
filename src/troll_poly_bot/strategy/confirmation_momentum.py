"""BTC_CONFIRMATION_MOMENTUM: wait for a confirmed BTC spot move, buy the token that has ALREADY followed it, in
small independent clips, held to settlement (the user's brief, 2026-10-06, after the analysis of wallet
0x75cc3b63a2: buys after a ~0.10 token move, 7-share clips, never sells, 61 % of markets positive).

EXPERIMENTAL, PAPER ONLY. ``TPB_CM_ENABLED=1`` is the feature flag and there is NO real-money switch:
__main__.check_confirmation_momentum refuses shadow, micro-live and live unconditionally. Env prefix ``TPB_CM_``;
its own data tag, budget and statistics; spot-lag, cheapside and the others are untouched.

The decision, per 5-minute BTC market, on every evaluation::

    timing      elapsed in [min_elapsed_s, max_elapsed_s] and remaining >= min_remaining_s (TOO_EARLY / TOO_LATE)
    spot        composite BTC spot <= max_spot_age_ms old, >= min_exchange_agreement sources fresh (STALE_SPOT / NO_SPOT_DATA)
    momentum    returns over 1/3/5/10/15 s; z_h = return_h / (sigma_per_sec * sqrt(h)); a direction needs
                z1 >= z_1s_min, z5 >= z_5s_min, z15 >= z_15s_min with the same sign (NO_MOMENTUM / WEAK_MOMENTUM)
    agreement   the 5 s move of each exchange (binance, coinbase, bybit) must agree in sign on >= min_exchange_agreement
                of them, and no exchange may contradict by more than contradiction_z (EXCHANGE_DISAGREEMENT)
    retrace     the impulse (the 15 s path's extreme in the direction) must not have given back more than
                max_retrace_fraction of itself (MOMENTUM_REVERSING)
    token       the candidate token (UP for an up move) must itself have risen >= token_confirm_cents over
                token_confirm_s (NO_TOKEN_CONFIRMATION); the requirement scales with the price zone
    zone        ask in [zone_min, zone1_max) normal, [zone1_max, zone2_max) stronger, [zone2_max, zone3_max)
                strongest confirmation; >= zone3_max rejected (TOKEN_TOO_EXPENSIVE)
    probability estimate_win_probability(features): the token mid plus a bounded momentum bonus (rule based,
                transparent), or a fitted logistic model when model_path points at one
    edge        net = p - executable price - taker fee - slippage_allowance - adverse_selection - latency_penalty
                >= min_net_edge (INSUFFICIENT_EDGE)
    chase       the ask may not have moved more than max_chase_cents since the impulse was first seen (CHASED_PRICE)
    entries     up to max_entries_per_market clips per market, each on a NEW impulse and only while the token is
                at least min_new_signal_cents above the last clip's price: never averaging down (MAX_ENTRIES)
    order       FOK / FAK taker for base_order_usdc (<= max_order_usdc, <= max_market_exposure_usdc per market)
    exit        none: every clip settles at 1 / 0

The LOTTERY (``lottery_enabled``, off by default, its P&L kept apart): in the last lottery_max_remaining_s, a
resting GTC bid for the LOSING token at lottery_min_price..lottery_max_price, lottery_max_risk_usdc of risk per
market, held to settlement. Paper fills follow the venue rule for resting orders -- the far side must trade
STRICTLY THROUGH the bid -- never a touch.

The pure functions (``momentum_from_returns``, ``exchange_agreement``, ``retrace_fraction``, ``price_zone``,
``estimate_win_probability``, ``net_edge``) take plain numbers, so the replay (scripts/research_confirmation_momentum.py)
and the live engine run the same arithmetic.
"""
from __future__ import annotations

import collections
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from ..features.engine import FeatureEngine, SpotHistory
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason, SideView
from .momentum import MomentumConfig, MomentumSignal, get_executable_price
from .spot_lag import SpotLagConfig, SpotLagEngine

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_CM_ENABLED"
HORIZONS_S = (1.0, 3.0, 5.0, 10.0, 15.0)
TOKEN_HORIZONS_S = (1.0, 3.0, 5.0, 10.0, 15.0)
#: the reject taxonomy of the brief (section 17); every evaluation that is not a TRADE increments exactly one
REJECTS = ("NO_SPOT_DATA", "STALE_SPOT", "EXCHANGE_DISAGREEMENT", "NO_MOMENTUM", "WEAK_MOMENTUM", "MOMENTUM_REVERSING",
           "NO_TOKEN_CONFIRMATION", "TOKEN_TOO_EXPENSIVE", "INSUFFICIENT_EDGE", "SPREAD_TOO_WIDE", "INSUFFICIENT_DEPTH",
           "TOO_EARLY", "TOO_LATE", "MAX_EXPOSURE", "MAX_ENTRIES", "ORDER_NOT_EXECUTABLE", "CHASED_PRICE", "OTHER")
#: the fitted model's inputs, in the order its coefficients are stored
MODEL_FEATURES = ("logit_mid", "z1", "z5", "z15", "agree", "tok_r5", "tok_r15", "spread_c", "log_depth", "sqrt_left",
                  "elapsed_frac", "retrace", "log_sigma")


def _logit(p: float) -> float:
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# ───────────────────────────── the pure decision pieces ─────────────────────────────


def momentum_from_returns(returns: dict[float, float | None], sigma_per_sec: float) -> dict:
    """Signed z-scores per horizon: z_h = r_h / (sigma * sqrt(h)). Missing returns give NaN."""
    out = {}
    for h, r in returns.items():
        out[h] = math.nan if r is None or sigma_per_sec <= 0.0 else r / (sigma_per_sec * math.sqrt(h))
    return out


def momentum_direction(z: dict[float, float], z1_min: float, z5_min: float, z15_min: float) -> tuple[int, str]:
    """(+1 / -1 / 0, why). A direction needs the 1 s, 5 s and 15 s z-scores on the same side of zero and each past
    its threshold; the 5 s z names the direction."""
    z1, z5, z15 = z.get(1.0, math.nan), z.get(5.0, math.nan), z.get(15.0, math.nan)
    if any(math.isnan(v) for v in (z1, z5, z15)):
        return 0, "NO_MOMENTUM"
    if abs(z5) < 0.5:
        return 0, "NO_MOMENTUM"
    d = 1 if z5 > 0 else -1
    if z1 * d < z1_min or z5 * d < z5_min or z15 * d < z15_min:
        return 0, "WEAK_MOMENTUM"
    return d, ""


def exchange_agreement(moves: dict[str, float | None], direction: int, sigma_sqrt_h: float,
                       contradiction_z: float) -> tuple[int, float, str | None]:
    """(exchanges agreeing in sign with ``direction``, the strongest contradicting z, the contradicting exchange).
    ``moves`` are the per-exchange log returns over the agreement horizon; None = no fresh data."""
    agree, worst, who = 0, 0.0, None
    for name, r in moves.items():
        if r is None or math.isnan(r):
            continue
        z = r / sigma_sqrt_h if sigma_sqrt_h > 0 else 0.0
        if z * direction > 0:
            agree += 1
        elif -z * direction > worst:
            worst, who = -z * direction, name
    return agree, worst, (who if worst >= contradiction_z else None)


def retrace_fraction(path: list[float], direction: int) -> float:
    """How much of the impulse the spot has given back: (extreme - now) / (extreme - start) in the direction of the
    move, over the path's samples (oldest first). 0 = at the extreme, 1 = all the way back; NaN when the path has
    no move in the direction."""
    if len(path) < 2:
        return math.nan
    start, now = path[0], path[-1]
    ext = max(path) if direction > 0 else min(path)
    impulse = (ext - start) * direction
    if impulse <= 0.0:
        return math.nan
    return max(0.0, (ext - now) * direction) / impulse


def price_zone(ask: float, zone_min: float, zone1_max: float, zone2_max: float, zone3_max: float) -> int:
    """0 = below the band (not a leader we buy), 1 / 2 / 3 = the brief's zones, 4 = too expensive."""
    if ask < zone_min:
        return 0
    if ask < zone1_max:
        return 1
    if ask < zone2_max:
        return 2
    if ask < zone3_max:
        return 3
    return 4


class CMModel:
    """A fitted logistic model over MODEL_FEATURES (standardised, with a Platt recalibration), written by the replay.
    Same file schema as the cheapside model so the tooling is shared."""

    def __init__(self, doc: dict) -> None:
        feats = tuple(doc.get("features") or ())
        if feats != MODEL_FEATURES:
            raise ValueError(f"model features {feats} != {MODEL_FEATURES}")
        beta = [float(x) for x in doc.get("beta") or []]
        if len(beta) != len(MODEL_FEATURES) + 1:
            raise ValueError("model coefficient count")
        self.beta, self.mu, self.sd = beta, [float(x) for x in doc["mu"]], [float(x) for x in doc["sd"]]
        pl = doc.get("platt") or {}
        self.a, self.b = float(pl.get("a", 0.0)), float(pl.get("b", 1.0))

    @classmethod
    def load(cls, path: str | Path) -> "CMModel | None":
        p = Path(path)
        if not p.exists():
            return None
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def predict(self, f: dict[str, float]) -> float:
        x = self.beta[0]
        for k, name in enumerate(MODEL_FEATURES):
            sd = self.sd[k] or 1.0
            x += self.beta[k + 1] * ((float(f.get(name, 0.0)) - self.mu[k]) / sd)
        return _sigmoid(self.a + self.b * x)


def estimate_win_probability(f: dict[str, float], cfg: "CMConfig", model: CMModel | None = None) -> float:
    """P(the candidate token settles at 1). With a fitted model, the model. Without one, the transparent rule:
    the token's own mid (the market's estimate) plus a bounded bonus for confirmed momentum --
    p_bonus_base once every confirmation passed, plus p_bonus_per_z for each unit of the 5 s z-score above its
    threshold, capped at p_bonus_cap. Nothing here claims more than a few cents over the market."""
    if model is not None:
        return min(max(model.predict(f), 0.02), 0.98)
    mid = float(f.get("mid", 0.5))
    z5 = abs(float(f.get("z5", 0.0)))
    bonus = cfg.p_bonus_base + cfg.p_bonus_per_z * max(0.0, z5 - cfg.z_5s_min)
    return min(max(mid + min(bonus, cfg.p_bonus_cap), 0.02), 0.98)


def net_edge(p: float, exec_price: float, fee_per_share: float, cfg: "CMConfig") -> dict:
    gross = p - exec_price
    net = gross - fee_per_share - cfg.slippage_allowance - cfg.adverse_selection - cfg.latency_penalty
    return {"gross_edge": gross, "fee": fee_per_share, "slippage": cfg.slippage_allowance,
            "adverse_selection": cfg.adverse_selection, "latency_penalty": cfg.latency_penalty, "net_edge": net}


# ───────────────────────────────────── config ─────────────────────────────────────


@dataclass(slots=True)
class CMConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_CM_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    #: a fitted model (scripts/research_confirmation_momentum.py writes it); absent = the rule-based estimator
    model_path: str = "data/replay/cm_model.json"
    # --- timing (section 1) ---
    min_elapsed_s: float = 45.0
    max_elapsed_s: float = 210.0
    min_remaining_s: float = 30.0
    # --- spot (section 2) ---
    max_spot_age_ms: float = 500.0
    exchanges: str = "binance,coinbase,bybit"
    agreement_horizon_s: float = 5.0
    # --- momentum (section 3) ---
    z_1s_min: float = 2.0
    z_5s_min: float = 2.0
    z_15s_min: float = 1.5
    # --- exchange agreement (section 4) ---
    min_exchange_agreement: int = 2
    contradiction_z: float = 1.0
    # --- continuation (section 5) ---
    retrace_window_s: float = 15.0
    max_retrace_fraction: float = 0.50
    # --- token confirmation (section 6) ---
    token_confirm_cents: float = 5.0
    token_confirm_s: float = 15.0
    # --- price zones (section 7): the confirmation multiplier per zone ---
    zone_min: float = 0.50
    zone1_max: float = 0.65
    zone2_max: float = 0.85
    zone3_max: float = 0.93
    zone2_confirm_mult: float = 1.5
    zone3_confirm_mult: float = 2.0
    zone2_z_mult: float = 1.25
    zone3_z_mult: float = 1.5
    # --- probability / edge (section 8) ---
    p_bonus_base: float = 0.03
    p_bonus_per_z: float = 0.01
    p_bonus_cap: float = 0.06
    min_net_edge: float = 0.03
    slippage_allowance: float = 0.01
    adverse_selection: float = 0.02
    latency_penalty: float = 0.005
    # --- execution (section 10) ---
    max_slippage: float = 0.02                 # the limit sits this far above the seen ask
    max_chase_cents: float = 3.0
    max_spread: float = 0.03
    min_depth_shares: float = 10.0
    order_type: str = "FAK"
    max_quote_age_ms: float = 1000.0
    # --- sizing (sections 11-12) ---
    base_order_usdc: float = 3.0
    max_order_usdc: float = 5.0
    max_market_exposure_usdc: float = 10.0
    max_entries_per_market: int = 3
    max_attempts_per_market: int = 6
    min_new_signal_cents: float = 4.0
    size_shares: float = 5.0
    size_mode: str = "fixed"
    # --- exits: none (section 13) ---
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0
    # --- risk (section 14) ---
    assets: str = "BTC"
    exclude_assets: str = ""
    max_simultaneous_positions: int = 3
    max_buys_per_hour: int = 60
    max_total_exposure_usdc: float = 20.0
    max_daily_loss_usdc: float = 10.0
    min_time_remaining_s: float = 30.0
    max_time_remaining_s: float = 300.0
    # --- the separate lottery (section 15) ---
    lottery_enabled: bool = False
    lottery_max_risk_usdc: float = 2.0
    lottery_min_price: float = 0.01
    lottery_max_price: float = 0.03
    lottery_max_remaining_s: float = 30.0
    lottery_min_remaining_s: float = 5.0
    lottery_max_loser_price: float = 0.10     # the losing token must already be this cheap
    # --- the fast feed stays off: the brief measures 1 s and longer ---
    fast_feed: bool = False

    def momentum_mirror(self) -> MomentumConfig:
        m = SpotLagConfig.momentum_mirror(self)
        m.max_hold_s = 1e9
        return m

    def exchange_list(self) -> tuple[str, ...]:
        return tuple(x.strip().lower() for x in self.exchanges.split(",") if x.strip())

    def confirm_cents_for_zone(self, zone: int) -> float:
        return self.token_confirm_cents * (self.zone2_confirm_mult if zone == 2 else self.zone3_confirm_mult if zone == 3 else 1.0)

    def z_mult_for_zone(self, zone: int) -> float:
        return self.zone2_z_mult if zone == 2 else self.zone3_z_mult if zone == 3 else 1.0


# ───────────────────────────────────── engine ─────────────────────────────────────


class _TokenHistory:
    """(ts, up mid) samples of one market, for the token's 1-15 s returns."""
    __slots__ = ("t", "p")

    def __init__(self) -> None:
        self.t: list[float] = []
        self.p: list[float] = []

    def update(self, up_mid: float, ts_ms: float) -> None:
        if self.t and ts_ms <= self.t[-1]:
            return
        self.t.append(ts_ms)
        self.p.append(up_mid)
        if len(self.t) > 3000:
            del self.t[:500]
            del self.p[:500]

    def at(self, ts_ms: float, max_gap_ms: float = 3000.0) -> float | None:
        import bisect
        k = bisect.bisect_right(self.t, ts_ms) - 1
        if k < 0 or ts_ms - self.t[k] > max_gap_ms:
            return None
        return self.p[k]


class ConfirmationMomentumEngine(SpotLagEngine):
    """Same surface as the spot-lag engine (live.py drives it unchanged); the confirmation-momentum decision inside."""

    feature_key = "cm"
    budget_is_ceiling = True
    stacks_positions = True        # several independent clips per market (live.py: risk.check add_to_open)

    def __init__(self, cfg: EngineConfig, scfg: CMConfig, risk: RiskManager, features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the confirmation-momentum strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, CMConfig):
            raise TypeError("ConfirmationMomentumEngine needs a CMConfig")
        super().__init__(cfg, scfg, risk, features)
        self.model = CMModel.load(scfg.model_path)
        self.rejects: collections.Counter = collections.Counter()
        self.trade_count = 0
        self.lottery_stats = {"orders": 0, "filled": 0, "partial": 0, "cost": 0.0, "won": 0, "lost": 0,
                              "unfilled": 0, "pnl": 0.0, "fill_probability": None}
        self.core_stats = {"fills": 0, "cost": 0.0, "won": 0, "lost": 0, "pnl": 0.0}
        self._ex_hist: dict[str, SpotHistory] = {}
        self._tok: dict[str, _TokenHistory] = {}
        #: slug -> {"dir", "t0", "ask0"}: the impulse currently being chased (CHASED_PRICE, one clip per impulse)
        self._impulse: dict[str, dict] = {}
        #: slug -> list of clips filled: [{"price", "t", "impulse_t0", "size"}]
        self._clips: dict[str, list[dict]] = {}
        self._spent: dict[str, float] = {}
        #: slug -> lottery order id -> record
        self._lottery: dict[str, dict] = {}
        self._kind_by_order: dict[str, str] = {}
        self.last_cm: dict[str, dict] = {}
        self.candidates: collections.deque = collections.deque(maxlen=200)

    # ----------------------------------------------------------------- helpers

    def _rej(self, ev: Evaluation, taxonomy: str, reason: Reason, detail: str = "", rec: dict | None = None) -> Evaluation:
        self.rejects[taxonomy if taxonomy in REJECTS else "OTHER"] += 1
        if rec is not None:
            rec["decision"], rec["reject_reason"] = "REJECT", taxonomy
            self._record(rec)
        return self._reject(ev, reason, detail)

    def _record(self, rec: dict) -> None:
        self.last_cm[rec.get("market_id", "")] = rec
        self.candidates.append(rec)
        if rec.get("decision") == "TRADE" or rec.get("stage", 0) >= 3:
            log.info("[CM] %s", json.dumps(rec, separators=(",", ":"), default=str))

    def _feed_exchanges(self, spot, now: float) -> None:
        src = getattr(spot, "sources", None) or {}
        for name, px in src.items():
            h = self._ex_hist.get(name)
            if h is None:
                h = self._ex_hist[name] = SpotHistory(min_gap_ms=100.0)
            if px:
                h.update(float(px), now)

    def _spot_path(self, hist: SpotHistory, now: float, window_s: float, step_s: float = 1.0) -> list[float]:
        out = []
        t = now - window_s * 1000.0
        while t <= now + 1e-6:
            v = hist.at(t)
            if v is not None:
                out.append(v)
            t += step_s * 1000.0
        return out

    IMPULSE_GAP_MS = 10_000.0      # momentum absent this long = the next qualifying tick is a NEW impulse

    def _impulse_for(self, slug: str, direction: int, ask: float, now: float) -> dict:
        imp = self._impulse.get(slug)
        if imp is None or imp["dir"] != direction or now - imp["last_qualify"] > self.IMPULSE_GAP_MS:
            imp = self._impulse[slug] = {"dir": direction, "t0": now, "ask0": ask, "last_qualify": now}
        imp["last_qualify"] = now
        return imp

    # ------------------------------------------------------------------- entry

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        cfg, sc, m = self.cfg, self.scfg, meta.market
        left = m.seconds_remaining(now)
        elapsed = (now - m.open_ts) / 1000.0
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        if not sc.asset_ok(m.asset):
            return self._reject(ev, Reason.ASSET_FILTERED, f"{m.asset} excluded")
        self._close_ts[m.slug] = m.close_ts
        self._forget_closed(now)
        # the token history is fed on every evaluation, whatever the gates below say
        if book_up is not None and book_up.mid is not None:
            self._tok.setdefault(m.slug, _TokenHistory()).update(float(book_up.mid), now)
        if spot is not None:
            self._feed_exchanges(spot, now)
        rec = {"market_id": m.slug, "asset": m.asset, "market_start": m.open_ts, "market_end": m.close_ts,
               "elapsed_seconds": round(elapsed, 1), "remaining_seconds": round(left, 1), "t": now, "stage": 0}
        # the lottery is its own experiment in the last seconds (section 15)
        if sc.lottery_enabled and left <= sc.lottery_max_remaining_s:
            return self._lottery_evaluate(ev, meta, book_up, book_down, now, left, balance, rec)
        if elapsed < sc.min_elapsed_s:
            return self._rej(ev, "TOO_EARLY", Reason.OUTSIDE_TIME_WINDOW, f"{elapsed:.0f} s after the open < {sc.min_elapsed_s:.0f}")
        if elapsed > sc.max_elapsed_s or left < sc.min_remaining_s:
            return self._rej(ev, "TOO_LATE", Reason.OUTSIDE_TIME_WINDOW, f"{elapsed:.0f} s elapsed / {left:.0f} s left")
        if left * 1000.0 < latency.round_trip_ms() + cfg.latency_safety_margin_ms:
            return self._rej(ev, "TOO_LATE", Reason.CANNOT_LAND_IN_TIME)
        if spot is None or spot_age_ms is None:
            return self._rej(ev, "NO_SPOT_DATA", Reason.STALE_DATA, "no spot")
        if spot_age_ms > min(cfg.max_spot_age_ms, sc.max_spot_age_ms):
            return self._rej(ev, "STALE_SPOT", Reason.STALE_DATA, f"spot {spot_age_ms:.0f} ms")
        if book_age_ms is None or book_age_ms > cfg.max_book_age_ms:
            return self._rej(ev, "OTHER", Reason.STALE_DATA, "book" if book_age_ms is None else f"book {book_age_ms:.0f} ms")
        if m.strike <= 0.0:
            return self._rej(ev, "OTHER", Reason.NO_STRIKE)
        if vol is None or not getattr(vol, "ready", False):
            return self._rej(ev, "OTHER", Reason.MODEL_NOT_READY, "volatility")
        hist = self.features.spot.get(m.asset)
        if hist is None:
            return self._rej(ev, "NO_SPOT_DATA", Reason.MODEL_NOT_READY, "spot history")
        sigma = float(vol.sigma_per_sec)
        self.last_sigma[m.asset] = sigma
        # --- spot prices and ages per exchange
        src = dict(getattr(spot, "sources", None) or {})
        wanted = sc.exchange_list()
        ages = {e: (round(now - self._ex_hist[e]._tip_t) if e in self._ex_hist and self._ex_hist[e]._tip_t > -math.inf else None)
                for e in wanted}
        rec.update({"spot_composite": round(spot.price, 2), "spot_age_ms": round(spot_age_ms),
                    **{f"spot_{e}": src.get(e) for e in wanted}, **{f"{e}_age": ages[e] for e in wanted}, "stage": 1})
        # --- momentum (section 3)
        rets = {h: hist.logret(now, h) for h in HORIZONS_S}
        z = momentum_from_returns(rets, sigma)
        rec.update({f"return_{int(h)}s": (None if rets[h] is None else round(rets[h] * 1e4, 2)) for h in HORIZONS_S})
        rec.update({f"z_{int(h)}s": (None if math.isnan(z[h]) else round(z[h], 2)) for h in (1.0, 5.0, 15.0)})
        rec["sigma_bps_s"] = round(sigma * 1e4, 3)
        d, why = momentum_direction(z, sc.z_1s_min, sc.z_5s_min, sc.z_15s_min)
        if d == 0:
            return self._rej(ev, why, Reason.WEAK_MOMENTUM if why == "WEAK_MOMENTUM" else Reason.SPOT_MOMENTUM_WEAK,
                             f"z1 {z[1.0]:+.2f} z5 {z[5.0]:+.2f} z15 {z[15.0]:+.2f}" if not math.isnan(z[5.0]) else "no spot history", rec)
        rec["direction"] = "UP" if d > 0 else "DOWN"
        _ask_now = (book_up if d > 0 else book_down)
        self._impulse_for(m.slug, d, float(_ask_now.best_ask) if _ask_now is not None and _ask_now.best_ask is not None else math.nan, now)
        # --- exchange agreement (section 4)
        moves = {}
        for e in wanted:
            h_e = self._ex_hist.get(e)
            moves[e] = h_e.logret(now, sc.agreement_horizon_s) if h_e is not None and (ages[e] is not None and ages[e] <= sc.max_spot_age_ms) else None
        agree, worst, contra = exchange_agreement(moves, d, sigma * math.sqrt(sc.agreement_horizon_s), sc.contradiction_z)
        rec["exchange_agreement"] = agree
        rec["exchange_moves_bps"] = {e: (None if v is None else round(v * 1e4, 2)) for e, v in moves.items()}
        fresh = sum(1 for v in moves.values() if v is not None)
        if fresh < sc.min_exchange_agreement:
            return self._rej(ev, "NO_SPOT_DATA", Reason.STALE_DATA, f"{fresh} fresh exchange(s) < {sc.min_exchange_agreement}", rec)
        if agree < sc.min_exchange_agreement or contra is not None:
            return self._rej(ev, "EXCHANGE_DISAGREEMENT", Reason.DATA_INCONSISTENT,
                             f"{agree}/{fresh} agree" + (f", {contra} contradicts by {worst:.1f} sigma" if contra else ""), rec)
        # --- continuation (section 5)
        path = self._spot_path(hist, now, sc.retrace_window_s)
        retr = retrace_fraction(path, d)
        rec["retracement"] = None if math.isnan(retr) else round(retr, 3)
        rec["stage"] = 2
        if math.isnan(retr) or retr > sc.max_retrace_fraction:
            return self._rej(ev, "MOMENTUM_REVERSING", Reason.MOMENTUM_REVERSING,
                             "no impulse in the window" if math.isnan(retr) else f"retraced {retr:.0%} of the impulse", rec)
        # --- the candidate token (section 6)
        side = "UP" if d > 0 else "DOWN"
        book = book_up if side == "UP" else book_down
        if book is None or book.best_ask is None or book.best_bid is None:
            return self._rej(ev, "OTHER", Reason.NO_OFFER, f"no two-sided {side} book", rec)
        ask, bid = float(book.best_ask), float(book.best_bid)
        token = m.yes_token_id if side == "UP" else m.no_token_id
        qa = (quote_ages or {}).get(token) if quote_ages is not None else book_age_ms
        if quote_ages is not None and qa is None:
            return self._rej(ev, "OTHER", Reason.STALE_DATA, f"no valid {side} book on this feed connection", rec)
        if qa is not None and qa > sc.max_quote_age_ms:
            return self._rej(ev, "OTHER", Reason.STALE_DATA, f"{side} quote {qa:.0f} ms", rec)
        spread = ask - bid
        depth = float(book.asks[0].size) if book.asks else 0.0
        th = self._tok.get(m.slug)
        up_now = float(book_up.mid) if book_up is not None and book_up.mid is not None else None
        tok_r = {}
        for h in TOKEN_HORIZONS_S:
            past = th.at(now - h * 1000.0) if th is not None else None
            if past is None or up_now is None:
                tok_r[h] = None
            else:
                tok_r[h] = (up_now - past) * (1 if side == "UP" else -1)      # the candidate's own move
        rec.update({"candidate": side, "bid": bid, "ask": ask, "spread": round(spread, 4), "depth": depth,
                    **{f"token_return_{int(h)}s": (None if tok_r[h] is None else round(tok_r[h] * 100.0, 2)) for h in TOKEN_HORIZONS_S}})
        ev.p_market = up_now
        zone = price_zone(ask, sc.zone_min, sc.zone1_max, sc.zone2_max, sc.zone3_max)
        rec["zone"] = zone
        if zone == 4:
            return self._rej(ev, "TOKEN_TOO_EXPENSIVE", Reason.PRICE_BAND, f"{side} ask {ask:.2f} >= {sc.zone3_max:.2f}", rec)
        if zone == 0:
            return self._rej(ev, "NO_TOKEN_CONFIRMATION", Reason.PRICE_BAND, f"{side} ask {ask:.2f} under {sc.zone_min:.2f}: not the leader yet", rec)
        conf_h = sc.token_confirm_s
        conf = tok_r.get(conf_h) if conf_h in tok_r else None
        if conf is None:
            conf = max((v for v in (tok_r.get(10.0), tok_r.get(5.0)) if v is not None), default=None)
        need = sc.confirm_cents_for_zone(zone) / 100.0
        if conf is None or conf < need - 1e-9:
            return self._rej(ev, "NO_TOKEN_CONFIRMATION", Reason.TOKEN_ALREADY_MOVED if conf is not None and conf < 0 else Reason.WEAK_MOMENTUM,
                             f"{side} moved {0 if conf is None else conf * 100:+.1f}c in {conf_h:.0f} s, needs {need * 100:.1f}c (zone {zone})", rec)
        zm = sc.z_mult_for_zone(zone)
        if zm > 1.0 and (z[5.0] * d < sc.z_5s_min * zm or z[15.0] * d < sc.z_15s_min * zm):
            return self._rej(ev, "WEAK_MOMENTUM", Reason.WEAK_MOMENTUM, f"zone {zone} needs z5 >= {sc.z_5s_min * zm:.2f}", rec)
        if spread > min(sc.max_spread, cfg.max_spread) + 1e-9:
            return self._rej(ev, "SPREAD_TOO_WIDE", Reason.LOW_LIQUIDITY, f"spread {spread:.3f}", rec)
        if depth < sc.min_depth_shares:
            return self._rej(ev, "INSUFFICIENT_DEPTH", Reason.LOW_LIQUIDITY, f"{depth:.0f} shares at the touch", rec)
        rec["stage"] = 3
        # --- probability and edge (section 8)
        mid = (ask + bid) / 2.0
        f = {"mid": mid, "logit_mid": _logit(mid), "z1": z[1.0] * d, "z5": z[5.0] * d, "z15": z[15.0] * d, "agree": agree,
             "tok_r5": (tok_r.get(5.0) or 0.0), "tok_r15": (tok_r.get(15.0) or 0.0), "spread_c": spread * 100.0,
             "log_depth": math.log1p(depth), "sqrt_left": math.sqrt(max(left, 0.0)), "elapsed_frac": elapsed / 300.0,
             "retrace": retr, "log_sigma": math.log(max(sigma, 1e-9))}
        p = estimate_win_probability(f, sc, self.model)
        limit = min(1.0 - (m.tick_size or 0.01), round(ask + min(sc.max_slippage, cfg.max_slippage), 4))
        venue_min = float(m.min_size) if m.min_size else 5.0
        size = max(venue_min, math.floor(sc.base_order_usdc / limit * 100.0) / 100.0)
        if size * limit > sc.max_order_usdc + 1e-9:
            size = math.floor(sc.max_order_usdc / limit * 100.0) / 100.0
        exec_price, avail = get_executable_price(book, size, limit)
        if exec_price is None:
            rec.update({"estimated_probability": round(p, 4)})
            return self._rej(ev, "ORDER_NOT_EXECUTABLE", Reason.LOW_LIQUIDITY, f"{avail:.0f} shares under {limit:.2f}, need {size:g}", rec)
        fee = meta.fee.taker_fee_per_share(exec_price)
        e = net_edge(p, exec_price, fee, sc)
        rec.update({"estimated_probability": round(p, 4), "effective_entry_price": round(exec_price, 4), "order_price": round(limit, 4),
                    **{k: round(v, 4) for k, v in e.items()}})
        ev.p_analytic = p if side == "UP" else 1.0 - p
        ev.p_used = ev.p_analytic
        ev.spot_move_bps = (rets[1.0] or 0.0) * 1e4
        sv = SideView(side=side, token_id=token, p_model=p, p_market=ask, ask=ask, bid=bid, depth_at_limit=avail,
                      costs=None, reason=None, entry_px=exec_price)
        ev.sides = [sv]
        self.last_signal[m.asset] = MomentumSignal(direction=d, label="CM_" + side, z={"main": z[1.0] * d, "5s": z[5.0] * d, "15s": z[15.0] * d},
                                                   sigma_per_sec=sigma)
        ev.features = {"cm": rec, "momentum": {"label": "CM_" + side, "z": {"main": round(z[1.0] * d, 3), "5s": round(z[5.0] * d, 3)},
                                               "score": round(abs(z[5.0]), 3)},
                       "execution": {"fair": round(p, 4), "cap": round(limit, 4), "best_ask": ask, "best_bid": bid,
                                     "spread": round(spread, 4), "quote_age_ms": None if qa is None else round(qa),
                                     "spot_age_ms": round(spot_age_ms)}}
        if e["net_edge"] < sc.min_net_edge:
            sv.reason = Reason.EDGE_TOO_SMALL
            return self._rej(ev, "INSUFFICIENT_EDGE", Reason.EDGE_TOO_SMALL,
                             f"p {p:.3f} - {exec_price:.3f} - costs = {e['net_edge']:+.3f} < {sc.min_net_edge:.2f}", rec)
        # --- chase and the clip rules (sections 10 and 12)
        imp = self._impulse_for(m.slug, d, ask, now)
        if math.isnan(imp["ask0"]):
            imp["ask0"] = ask
        if (ask - imp["ask0"]) * 100.0 > sc.max_chase_cents + 1e-9:
            return self._rej(ev, "CHASED_PRICE", Reason.ASK_ABOVE_MAX_ENTRY,
                             f"ask {ask:.2f} is {(ask - imp['ask0']) * 100:.0f}c above the impulse's first ask {imp['ask0']:.2f}", rec)
        clips = self._clips.get(m.slug, [])
        if len(clips) >= int(sc.max_entries_per_market):
            return self._rej(ev, "MAX_ENTRIES", Reason.MARKET_ENTERED, f"{len(clips)} clips already", rec)
        if clips:
            last = clips[-1]
            if last.get("side") != side:
                return self._rej(ev, "MAX_ENTRIES", Reason.DIRECTION_MISMATCH, f"the market holds {last.get('side')}; no second side", rec)
            if imp["t0"] <= last["impulse_t0"]:
                return self._rej(ev, "MAX_ENTRIES", Reason.COOLDOWN, "same impulse as the last clip", rec)
            if mid < last["price"] + sc.min_new_signal_cents / 100.0 - 1e-9:
                return self._rej(ev, "MAX_ENTRIES", Reason.MOMENTUM_EXHAUSTED,
                                 f"{side} mid {mid:.2f} is not {sc.min_new_signal_cents:.0f}c above the last clip at {last['price']:.2f}: never averaging down", rec)
        if self.attempts.get(m.slug, 0) >= int(sc.max_attempts_per_market):
            return self._rej(ev, "MAX_ENTRIES", Reason.MARKET_ATTEMPTS, f"{self.attempts[m.slug]} attempts already", rec)
        if self.buys_last_hour(now) >= int(sc.max_buys_per_hour):
            return self._rej(ev, "MAX_EXPOSURE", Reason.HOURLY_BUY_LIMIT, f"{len(self._buy_times)} buys in the last hour", rec)
        spent = self._spent.get(m.slug, 0.0)
        if spent + size * limit > sc.max_market_exposure_usdc + 1e-9:
            fit = math.floor((sc.max_market_exposure_usdc - spent) / limit * 100.0) / 100.0
            if fit + 1e-9 < venue_min:
                return self._rej(ev, "MAX_EXPOSURE", Reason.EXPOSURE_CAP, f"{spent:.2f} of {sc.max_market_exposure_usdc:.2f} USDC used in this market", rec)
            size = fit
        room = self.risk.capacity_total()
        if size * limit > room + 1e-9:
            return self._rej(ev, "MAX_EXPOSURE", Reason.EXPOSURE_CAP, f"{room:.2f} USDC of total room", rec)
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit, add_to_open=True)
        if not verdict.ok:
            return self._rej(ev, "MAX_EXPOSURE", Reason(verdict.reason), verdict.detail, rec)
        if size * limit > balance + 1e-9:
            return self._rej(ev, "MAX_EXPOSURE", Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * limit:.2f}", rec)
        rec.update({"decision": "TRADE", "order_size": size, "signal_generation_timestamp": now, "impulse_t0": imp["t0"], "kind": "CORE"})
        self._record(rec)
        rec["signal"] = True
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, limit, exec_price
        ev.max_entry_price = limit
        ev.size, ev.net_edge, ev.confidence = size, e["net_edge"], 1.0
        ev.size_note = f"{size:g} shares ({size * limit:.2f} USDC)"
        self.last[m.slug] = ev
        return ev

    # ------------------------------------------------------------------ lottery

    def _lottery_evaluate(self, ev, meta, book_up, book_down, now, left, balance, rec) -> Evaluation:
        sc, m = self.scfg, meta.market
        rec["kind"] = "LOTTERY"
        if left < sc.lottery_min_remaining_s:
            return self._rej(ev, "TOO_LATE", Reason.OUTSIDE_TIME_WINDOW, f"{left:.0f} s left", rec)
        if m.slug in self._lottery:
            return self._rej(ev, "MAX_ENTRIES", Reason.MARKET_ENTERED, "lottery bid already resting", rec)
        if book_up is None or book_down is None or book_up.best_ask is None or book_down.best_ask is None:
            return self._rej(ev, "OTHER", Reason.NO_OFFER, "no two-sided books", rec)
        loser = "UP" if book_up.best_ask < book_down.best_ask else "DOWN"
        book = book_up if loser == "UP" else book_down
        ask = float(book.best_ask)
        if ask > sc.lottery_max_loser_price + 1e-9:
            return self._rej(ev, "TOKEN_TOO_EXPENSIVE", Reason.PRICE_BAND, f"losing token {loser} still asks {ask:.2f}", rec)
        bid_now = float(book.best_bid) if book.best_bid is not None else 0.0
        price = min(sc.lottery_max_price, max(sc.lottery_min_price, bid_now))
        price = round(price, 2)
        size = math.floor(sc.lottery_max_risk_usdc / price)
        venue_min = float(m.min_size) if m.min_size else 5.0
        if size < venue_min:
            return self._rej(ev, "OTHER", Reason.SIZE_ZERO, f"{sc.lottery_max_risk_usdc:.2f} USDC buys {size} shares at {price:.2f}", rec)
        if size * price > balance + 1e-9:
            return self._rej(ev, "MAX_EXPOSURE", Reason.SIZE_ZERO, "balance", rec)
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * price, add_to_open=True)
        if not verdict.ok:
            return self._rej(ev, "MAX_EXPOSURE", Reason(verdict.reason), verdict.detail, rec)
        token = m.yes_token_id if loser == "UP" else m.no_token_id
        rec.update({"decision": "TRADE", "candidate": loser, "bid": bid_now, "ask": ask, "order_price": price, "order_size": size,
                    "signal_generation_timestamp": now})
        self._record(rec)
        ev.features = {"cm": rec, "momentum": {"label": "LOTTERY_" + loser, "z": {}, "score": 0.0}}
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = loser, token, price, price
        ev.size, ev.net_edge, ev.confidence = float(size), 0.0, 0.0
        ev.p_used = 0.0
        ev.size_note = f"lottery {size} sh at {price:.2f} ({size * price:.2f} USDC at risk)"
        self.last[m.slug] = ev
        return ev

    # ------------------------------------------------------------------ orders

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        rec = (ev.features or {}).get("cm", {})
        if rec.get("kind") == "LOTTERY":
            o = Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size, tif=TimeInForce.GTC,
                      expected_price=ev.limit_price, fair_at_decision=None, expires_ts=float(rec.get("market_end") or 0.0),
                      tag=f"{ev.side} LOTTERY bid {ev.limit_price:.2f} x {ev.size:g}")
            self._lottery[rec["market_id"]] = {"order_id": None, "price": ev.limit_price, "size": ev.size, "filled": 0.0, "cost": 0.0,
                                               "side": ev.side, "t": rec.get("t")}
            self.lottery_stats["orders"] += 1
            return o
        tif = TimeInForce.IOC if self.scfg.order_type.upper() in ("FAK", "IOC") else TimeInForce.FOK
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size, tif=tif,
                     expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                     tag=f"{ev.side} CM clip p={rec.get('estimated_probability', 0):.3f} edge={ev.net_edge:+.3f} cap={ev.limit_price:.2f}")

    def on_entry_submit(self, slug: str, now_ms: float) -> None:
        super().on_entry_submit(slug, now_ms)

    def on_fill_record(self, slug: str, order, fill, now_ms: float) -> None:
        """live.py: a BUY filled (whole or part). Clips and lottery fills are kept apart."""
        tag = str(getattr(order, "tag", "") or "")
        size = float(getattr(fill, "size", 0.0) or 0.0)
        price = float(getattr(fill, "price", 0.0) or 0.0)
        fee = float(getattr(fill, "fee", 0.0) or 0.0)
        if size <= 0.0:
            return
        if "LOTTERY" in tag:
            lot = self._lottery.get(slug)
            if lot is not None:
                lot["order_id"] = getattr(order, "order_id", None)
                lot["filled"] += size
                lot["cost"] += size * price + fee
                self.lottery_stats["filled"] += 1 if lot["filled"] <= size + 1e-9 else 0
                if 0 < lot["filled"] < lot["size"] - 1e-6:
                    self.lottery_stats["partial"] += 1
                self.lottery_stats["cost"] += size * price + fee
            return
        ev = self.last.get(slug)
        side = ev.side if ev is not None and ev.token_id == getattr(order, "token_id", None) else \
            (self._clips[slug][-1].get("side") if self._clips.get(slug) else None)
        imp = self._impulse.get(slug) or {"t0": now_ms}
        self._clips.setdefault(slug, []).append({"price": price, "t": now_ms, "impulse_t0": imp["t0"], "size": size, "side": side})
        self._spent[slug] = self._spent.get(slug, 0.0) + size * price + fee
        self.core_stats["fills"] += 1
        self.core_stats["cost"] += size * price + fee
        self.trades[slug] = len(self._clips[slug])

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now_ms: float) -> None:
        """live.py: the market resolved. The lottery's result is booked to its own statistics, the clips' to the core's."""
        winner = "UP" if won_up else "DOWN"
        lot = self._lottery.pop(slug, None)
        if lot is not None:
            if lot["filled"] > 1e-9:
                payout = lot["filled"] if lot["side"] == winner else 0.0
                self.lottery_stats["pnl"] += payout - lot["cost"]
                self.lottery_stats["won" if payout > 0 else "lost"] += 1
            else:
                self.lottery_stats["unfilled"] += 1
            n = self.lottery_stats["orders"]
            self.lottery_stats["fill_probability"] = round(self.lottery_stats["filled"] / n, 4) if n else None
        clips = self._clips.pop(slug, [])
        self._spent.pop(slug, None)
        self._impulse.pop(slug, None)
        self._tok.pop(slug, None)
        if clips:
            cost = sum(c["size"] * c["price"] for c in clips)
            payout = sum(c["size"] for c in clips if c.get("side") == winner)
            self.core_stats["pnl"] += payout - cost
            self.core_stats["won" if payout > cost else "lost"] += 1

    def signal_text(self, ev: Evaluation) -> str:
        r = (ev.features or {}).get("cm", {})
        if r.get("kind") == "LOTTERY":
            return f"LOTTERY {r.get('candidate')} bid {r.get('order_price')} x {r.get('order_size')}"
        return (f"CM {r.get('candidate')} ask {r.get('ask')} p {r.get('estimated_probability')} net {r.get('net_edge'):+} "
                f"z5 {r.get('z_5s'):+} agree {r.get('exchange_agreement')} tok15 {r.get('token_return_15s')}c")

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig: MomentumSignal | None = None, spread: float | None = None,
                      bid_depth: float | None = None, size: float | None = None,
                      data_age_ms: float | None = None) -> str | None:
        """Never: every clip settles (section 13)."""
        return None

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = "confirmation_momentum"
        snap["cm_config"] = self.scfg.as_dict()
        snap["cm"] = dict(self.last_cm)
        snap["cm_rejects"] = {k: self.rejects.get(k, 0) for k in REJECTS}
        snap["cm_core"] = dict(self.core_stats)
        snap["cm_lottery"] = dict(self.lottery_stats)
        snap["cm_model"] = {"path": self.scfg.model_path, "fitted": self.model is not None}
        return snap
