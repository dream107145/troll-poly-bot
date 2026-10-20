"""CHEAPSIDE: optionally buy the cheapest outcome in each of seven markets, held to settlement.

EXPERIMENTAL, PAPER ONLY. The research verdict is in docs/cheapside_2026-09-29.md
(scripts/research_cheapside.py). It is a separate strategy with its own id
(``cheapside``), env prefix (``TPB_CHEAPSIDE_*``), budget, model file and data files;
spot-lag, momentum and repriced-momentum are untouched. It is behind
``TPB_CHEAPSIDE_ENABLED=1`` and has NO real-money switch: __main__ refuses micro-live
and live for it unconditionally.

Per market, every evaluation (the 200 ms tick, or an event when the fast feed is on)::

    cheap      the side with the lower best ask; only 0.05 <= ask <= 0.35, spread <= 3c,
               and the venue minimum showing at the touch
    exception  buy the opposite, leading side only at >= 85% model probability, with spot
               confirmation, ask 0.50-0.70, and the same net-edge and book checks
    model      P(cheap side settles as the winner): a logistic regression fitted on
               TRAINING windows and recalibrated on validation (data/replay/cheapside_model.json),
               from the ask, time left, the analytic TWAP fair value (spot distance from the
               strike against the settling average's remaining variance), the spot's 250 ms,
               500 ms, 1 s and 3 s returns in sigma (signed to the cheap side), their sign
               consistency and acceleration, realised vol, spot order flow, spread and depth
    support    the 1 s spot return points the cheap side's way and 2 of the 3 short horizons agree
    edge       model_p - limit - taker fee(limit) - slippage >= ``min_net_edge`` (5c), the limit
               being ask + ``max_slippage`` (1c)
    order      FOK for ``size_shares`` (10; at most ``max_order_usdc`` = 5 USDC), one model entry per
               market, ``max_attempts_per_market`` (3) orders >= 2 s apart
    portfolio after every currently open position holds its cheaper side, compute the sum of settlement PnL
               when each market's current leader wins; if below +$2, buy the cheapest current leader, with
               additional clips recalculated from actual fills and bounded by the shared risk controls
    exit       none: every position settles at 1 / 0. The only ways out are the operator's
               kill file and the risk halts (no new entries; open positions settle within
               5 minutes). The model gates first entries; the separate portfolio rule buys a leader only to repair
               the defined all-current-leaders-win settlement scenario.

Configured basket mode monitors all seven current crypto 5m markets. When the six cheapest outcomes
average below the configured threshold, strict preflight gates (favourite ask/unit-cost band, cash and
hedge feasibility with reserved capital, arm time cutoff, book quality/skew) must all pass before the
plan is frozen. The bot then buys those six outcomes and, on the same tick they finish, the favourite
in the remaining market (live routes that order to its book). Mid-execution abort stops new cheap legs
if the favourite blows off, the hedge becomes unaffordable, depth/time/risk caps fail, or orphan limits
are breached; incomplete baskets are tracked explicitly (no silent reshuffle). The favourite protects
only the six-cheap-lose / favourite-win settlement scenario — not an all-seven-lose hedge.

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
from .cheapside_basket import (
    EVT_BASKET_ABORT, EVT_BASKET_ARMED, EVT_BASKET_COMPLETE,
    EVT_CHEAP_LEG_FAILURE, EVT_CHEAP_LEG_FILL, EVT_CHEAP_LEG_PARTIAL,
    EVT_HEDGE_ABORT, EVT_HEDGE_REPRICE, EVT_LEADER_FILL, EVT_LEADER_ORDER,
    EVT_ORPHAN_EXPOSURE, EVT_PREFLIGHT_PASS, EVT_PREFLIGHT_REJECT, EVT_RISK_LIMIT_REJECT,
    BasketState, LegQuote, PositionLeg, all_outcome_scenarios, classify_scenarios,
    estimate_leg_cost, leader_shares_for_target, run_preflight, unit_cost,
)
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
    #: paper experiment: if ordinary model/support gates fail, submit a bounded
    #: fallback for the cheaper side in each market that passes price, book and risk checks.
    force_one_per_market: bool = False
    #: Buy the current favourite only when the model, spot direction, and net edge
    #: all strongly support it. The favourite ask has its own conservative cap.
    leader_buy_enabled: bool = True
    leader_min_win_probability: float = 0.85
    leader_min_ask: float = 0.50
    leader_max_ask: float = 0.70
    #: Once every open position is on its market's cheaper side, repair the combined
    #: all-current-leaders-win settlement scenario to this amount.
    portfolio_repair_target_usdc: float = 2.0
    portfolio_repair_cooldown_s: float = 2.0
    #: Keep portfolio repair active beyond the narrower entry window.
    portfolio_min_time_remaining_s: float = 60.0
    #: Coordinated basket: cheapest outcome in every market.
    portfolio_basket_enabled: bool = False
    portfolio_basket_market_count: int = 7
    portfolio_basket_buy_count: int = 6
    portfolio_basket_max_average_ask: float = 0.14
    portfolio_basket_shares: float = 5.0
    #: Target settlement PnL when all six cheap legs lose and the favourite wins.
    portfolio_basket_target_pnl_usdc: float = 0.10
    #: Favourite ask band for arming / continuing cheap legs (executable ask).
    portfolio_basket_min_leader_ask: float = 0.55
    portfolio_basket_max_leader_ask: float = 0.78
    #: Abort NEW cheap-leg orders when the favourite ask reaches this level.
    portfolio_basket_abort_leader_ask: float = 0.82
    #: Reject if favourite all-in unit cost (limit + worst fee) exceeds this.
    #: 0.81 covers ask 0.78 + 1c slippage + crypto taker fee (~0.802); the old 0.80
    #: cap rejected every arm at the top of the favourite ask band.
    portfolio_basket_max_leader_unit_cost: float = 0.81
    #: Minimum seconds remaining to arm a new basket or start a new cheap-leg buy.
    portfolio_basket_arm_min_time_remaining_s: float = 120.0
    #: Max age skew across the seven synchronized books (ms).
    portfolio_basket_max_quote_skew_ms: float = 1000.0
    #: Cash reserved for the planned hedge may not be spent on unrelated orders.
    portfolio_basket_cash_buffer_usdc: float = 1.0
    portfolio_basket_max_cash_utilization: float = 0.95
    #: Risk caps (0 = disabled). Enforced on the order-submission path.
    portfolio_basket_max_notional_usdc: float = 80.0
    portfolio_basket_max_leader_notional_usdc: float = 60.0
    portfolio_basket_max_leader_multiple: float = 15.0
    portfolio_basket_max_all_lose_usdc: float = 80.0
    portfolio_basket_max_orphan_usdc: float = 40.0
    portfolio_basket_max_concurrent_exposure_usdc: float = 120.0
    #: Remaining leader shares below this after fills may mark COMPLETE if PnL ok.
    portfolio_basket_dust_shares: float = 0.05
    #: Orphan policy: hold (default) | alert_only. Never auto-dump at any price.
    portfolio_basket_orphan_policy: str = "hold"
    #: Cheap-leg FAK retry interval (ms); leader uses entry_retry_ms.
    portfolio_basket_cheap_retry_ms: float = 250.0
    #: When false, keep preflight gates but do not abort mid-execution on hedge blow-off
    #: (used by replay variant B). Default true = full monitoring.
    portfolio_basket_execution_abort: bool = True
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
    max_simultaneous_positions: int = 8
    max_entries_per_market: int = 1
    max_attempts_per_market: int = 3
    max_order_usdc: float = 5.0
    # Effectively disabled for this paper-only strategy; available balance is
    # the aggregate limit, with per-leg quote and execution checks still active.
    max_total_exposure_usdc: float = 1_000_000_000.0
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

    def apply_budget(self, risk_cfg, order_ceiling: float | None = None) -> list[str]:
        # Keep the per-order ceiling for cheap legs and reserve concurrent slots
        # for a second seven-market epoch if the first is still settling.
        changed = SpotLagConfig.apply_budget(self, risk_cfg, order_ceiling)
        # The paper balance is the aggregate spend limit. Do not let generic
        # total exposure settings strand a basket part way through.
        if risk_cfg.max_total_exposure_usdc < self.max_total_exposure_usdc:
            old = risk_cfg.max_total_exposure_usdc
            risk_cfg.max_total_exposure_usdc = self.max_total_exposure_usdc
            changed.append(f"max_total_exposure_usdc: {old} -> {self.max_total_exposure_usdc} (Cheapside total cap disabled)")
        if self.portfolio_basket_enabled:
            # The recovery leader can require more than a normal $5 entry to
            # offset the six cheap legs. It remains bounded by paper cash and
            # the executable quote/depth checks in _portfolio_order.
            for attr in ("max_position_usdc", "max_asset_exposure_usdc", "max_epoch_exposure_usdc"):
                old = getattr(risk_cfg, attr)
                if old < self.max_total_exposure_usdc:
                    setattr(risk_cfg, attr, self.max_total_exposure_usdc)
                    changed.append(f"{attr}: {old} -> {self.max_total_exposure_usdc} (basket recovery sizing)")
            required = 2 * int(self.portfolio_basket_market_count)
            if risk_cfg.max_concurrent_positions < required:
                old = risk_cfg.max_concurrent_positions
                risk_cfg.max_concurrent_positions = required
                changed.append(f"max_concurrent_positions: {old} -> {required} (two basket epochs)")
        return changed


class CheapSideEngine(SpotLagEngine):
    """Same surface as the spot-lag engine; the cheap-side decision inside."""

    feature_key = "cheapside"
    stacks_positions = True
    #: Basket leader asks move quickly after the six cheap fills; retry far
    #: sooner than the default 2 s entry throttle so recovery does not chase
    #: a rising favourite.
    entry_retry_ms = 200.0
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
        # The basket rule prices contracts directly and does not depend on a BTC strike/TWAP model.
        self.requires_strike = not scfg.portfolio_basket_enabled
        self.requires_market_open = scfg.portfolio_basket_enabled
        # Settlement can leave the previous seven-market basket open while the next
        # basket enters. Give this paper-only strategy room for two baskets; the
        # per-order, per-asset, and per-epoch limits still bind.
        if scfg.portfolio_basket_enabled:
            risk.cfg.max_concurrent_positions = max(
                risk.cfg.max_concurrent_positions, 2 * int(scfg.portfolio_basket_market_count))
        self.model = CheapSideModel.load(scfg.model_path)
        self.last_cheapside: dict[str, dict] = {}
        self.inventory: dict[str, dict[str, dict[str, float]]] = {}
        self.market_quotes: dict[str, dict] = {}
        self.repair_last_try: dict[str, float] = {}
        self.basket_plans: dict[str, dict] = {}
        self.basket_events: list[dict] = []

    def revalidate(self, ev: Evaluation, book, min_edge: float | None = None) -> Reason | None:
        """Forced paper samples keep the hard price and depth checks, but not the edge gate."""
        info = (ev.features or {}).get("cheapside", {})
        if not (info.get("forced_one_per_market") or info.get("portfolio_recovery")
                or info.get("portfolio_lower_buy") or info.get("basket_low_buy") or info.get("basket_leader_buy")):
            return super().revalidate(ev, book, min_edge)
        if book is None or book.best_ask is None:
            return Reason.NO_OFFER
        ask = float(book.best_ask)
        if info.get("basket_leader_buy"):
            lo = self.scfg.portfolio_basket_min_leader_ask
            hi = self.scfg.portfolio_basket_abort_leader_ask
            if ask < lo - 1e-9 or ask > hi + 1e-9:
                return Reason.PRICE_BAND
        exec_price, _ = get_executable_price(book, ev.size, ev.limit_price)
        if exec_price is None:
            return Reason.HIGH_SLIPPAGE if book.best_ask > ev.limit_price + 1e-9 else Reason.LOW_LIQUIDITY
        if ev.max_entry_price is not None and exec_price > ev.max_entry_price + 1e-9:
            return Reason.ASK_ABOVE_MAX_ENTRY
        return None

    def on_fill_detail(self, slug: str, side: str, price: float, size: float, fee: float, now: float) -> None:
        sides = self.inventory.setdefault(slug, {})
        pos = sides.setdefault(side, {"shares": 0.0, "cost": 0.0})
        pos["shares"] += float(size)
        pos["cost"] += float(price) * float(size) + float(fee)
        for plan in self.basket_plans.values():
            if plan.get("state") in (BasketState.COMPLETE.value, BasketState.ABORTED.value):
                continue
            leader = plan.get("leader") or {}
            if slug == leader.get("slug") and side == leader.get("side"):
                self._basket_event(plan, EVT_LEADER_FILL, now, slug=slug, side=side,
                                   size=size, price=price, fee=fee)
                self._refresh_basket_reservation(plan, now)
            elif any(leg.get("slug") == slug and leg.get("side") == side for leg in plan.get("cheap", [])):
                held = float(pos["shares"])
                target = float(self.scfg.portfolio_basket_shares)
                evt = EVT_CHEAP_LEG_PARTIAL if held + 1e-9 < target else EVT_CHEAP_LEG_FILL
                self._basket_event(plan, evt, now, slug=slug, side=side, size=size,
                                   price=price, fee=fee, held=held)
                self._refresh_basket_reservation(plan, now)

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        self.inventory.pop(slug, None)
        self.market_quotes.pop(slug, None)
        self.repair_last_try.pop(slug, None)
        for key, plan in list(self.basket_plans.items()):
            if any(leg.get("slug") == slug for leg in [*plan.get("cheap", []), plan.get("leader") or {}]):
                self.basket_plans.pop(key, None)

    def persist_state(self) -> dict:
        return {"cheapside_inventory": self.inventory, "cheapside_baskets": self.basket_plans}

    def restore_state(self, doc: dict | None, now: float) -> None:
        saved = (doc or {}).get("cheapside_inventory", {})
        self.inventory = {
            str(slug): {str(side): {"shares": float(p.get("shares", 0.0)), "cost": float(p.get("cost", 0.0))}
                        for side, p in sides.items() if side in ("UP", "DOWN")}
            for slug, sides in saved.items()
        }
        self.basket_plans = {str(k): v for k, v in (doc or {}).get("cheapside_baskets", {}).items()}
        # Idempotent restart: reconcile reservations; do not re-arm completed plans.
        for plan in self.basket_plans.values():
            if plan.get("complete"):
                plan["state"] = BasketState.COMPLETE.value
            elif plan.get("aborted"):
                plan["state"] = plan.get("state") or BasketState.ABORTED.value
            else:
                plan.setdefault("state", BasketState.ARMED.value)
                self._refresh_basket_reservation(plan, now)

    def _portfolio_order(self, meta, book_up, book_down, side: str, now: float, balance: float,
                         quote_ages, needed: float, scenario_pnl: float, kind: str) -> Evaluation | None:
        m = meta.market
        def blocked(reason: str) -> None:
            self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, "mode": "basket_blocked",
                                             "order_kind": kind, "side": side, "shares_needed": round(needed, 2),
                                             "why": reason}
            return None
        book = book_up if side == "UP" else book_down
        token = m.yes_token_id if side == "UP" else m.no_token_id
        ask, bid = (book.best_ask, book.best_bid) if book is not None else (None, None)
        qa = (quote_ages or {}).get(token)
        left = m.seconds_remaining(now)
        if ask is None or bid is None or qa is None or qa > self.scfg.max_quote_age_ms:
            return blocked("missing or stale outcome quote")
        if ask <= bid or ask - bid > min(self.scfg.max_spread, self.cfg.max_spread) + 1e-9:
            return blocked("outcome spread is not executable")
        if kind == "basket_low_buy":
            # New cheap-leg purchases share the arm entry cutoff.
            min_left = self.scfg.portfolio_basket_arm_min_time_remaining_s
            in_window = min_left <= left <= self.scfg.max_time_remaining_s
        elif kind == "basket_leader_buy":
            # Leader recovery may continue inside a looser settlement window.
            min_left = min(self.scfg.portfolio_min_time_remaining_s,
                           self.scfg.portfolio_basket_arm_min_time_remaining_s)
            in_window = min_left <= left <= self.scfg.max_time_remaining_s
        elif kind.startswith("basket_"):
            in_window = self.scfg.portfolio_min_time_remaining_s <= left <= self.scfg.max_time_remaining_s
        else:
            in_window = (max(self.scfg.portfolio_min_time_remaining_s, self.cfg.trade_window_end_s)
                         <= left <= min(self.scfg.max_time_remaining_s, self.cfg.trade_window_start_s))
        if not in_window:
            return blocked(f"outside basket order window ({left:.1f}s remaining)")
        limit = min(1.0 - (m.tick_size or 0.01), round(ask + min(self.scfg.max_slippage, self.cfg.max_slippage), 4))
        if limit <= 0.0:
            return None
        epoch = int(m.open_ts // 1000)
        leader_recovery = kind == "basket_leader_buy"
        # Hedge capital reserved on an armed basket must not be spent on cheap legs
        # or unrelated portfolio orders.
        reserved = 0.0 if leader_recovery else self._basket_reserved_cash(exclude_leader_spend=True)
        spendable = max(0.0, balance - reserved)
        order_cap = spendable if leader_recovery else min(self.scfg.max_order_usdc, spendable)
        room = min(order_cap, self.risk.cfg.max_position_usdc,
                   self.risk.capacity_total(), max(0.0, self.risk.cfg.max_asset_exposure_usdc - self.risk.exposure_asset(m.asset)),
                   max(0.0, self.risk.cfg.max_epoch_exposure_usdc - self.risk.exposure_epoch(epoch)), spendable)
        if self.scfg.portfolio_basket_max_concurrent_exposure_usdc > 0:
            room = min(room, max(0.0, self.scfg.portfolio_basket_max_concurrent_exposure_usdc
                                 - self.risk.exposure_total()))
        venue_min = float(m.min_size) if m.min_size else 5.0
        # A partially filled FAK can leave less than the venue's minimum
        # outstanding (observed: 4/5 shares filled). The paper engine can top
        # up that remainder; applying the full minimum here stranded baskets.
        min_order_size = min(venue_min, needed) if kind in ("basket_low_buy", "basket_leader_buy") else venue_min
        size = math.floor(min(needed, room / limit) * 100.0) / 100.0
        if size < min_order_size - 1e-9:
            return blocked(f"risk/cash room permits {size:.2f} shares; minimum for this order is {min_order_size:.2f}")
        exec_price, available = get_executable_price(book, size, limit)
        if exec_price is None and available >= min_order_size:
            size = math.floor(available * 100.0) / 100.0
            exec_price, _ = get_executable_price(book, size, limit)
        if exec_price is None or size < min_order_size - 1e-9:
            return blocked(f"book depth permits {available:.2f} shares at limit {limit:.3f}; need {size:.2f}")
        verdict = self.risk.check(m.slug, m.asset, epoch, size * limit, add_to_open=True)
        if not verdict.ok:
            return blocked(f"risk rejected order: {verdict.reason} {verdict.detail}".strip())
        up_mid = book_up.mid if book_up is not None else None
        down_mid = book_down.mid if book_down is not None else None
        if up_mid is None or down_mid is None:
            return blocked("missing both market mid prices")
        p_side = up_mid if side == "UP" else 1.0 - up_mid
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None,
                        p_used=p_side, p_market=up_mid, side=side, token_id=token,
                        limit_price=limit, expected_price=exec_price, size=size,
                        net_edge=0.0, confidence=1.0, size_note=kind)
        _, depth = get_executable_price(book, size, limit)
        ev.sides = [SideView(side=side, token_id=token, p_model=p_side,
                             p_market=up_mid if side == "UP" else down_mid,
                             ask=ask, bid=bid, depth_at_limit=depth, costs=None, reason=None,
                             entry_px=exec_price)]
        ev.max_entry_price = limit
        ev.features = {"cheapside": {kind: True, "basket_entry": kind.startswith("basket_"),
                                      "side": side, "ask": ask, "limit": limit,
                                      "size": size, "shares_requested": round(needed, 4),
                                      "shares_ordered": round(size, 4),
                                      "order_clipped_by_risk_or_cash": size + 1e-9 < needed,
                                      "leader_win_pnl": round(scenario_pnl, 4)}}
        if kind == "portfolio_recovery":
            ev.features["cheapside"].update({"target_pnl": self.scfg.portfolio_repair_target_usdc,
                                             "target_market": m.slug})
        self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, "mode": kind, "side": side,
                                        "ask": ask, "leader_win_pnl": round(scenario_pnl, 4), "size": size}
        self.repair_last_try[m.slug] = now
        return ev

    def _portfolio_recovery(self, meta, book_up, book_down, now: float, balance: float,
                            quote_ages=None, book_age_ms: float | None = None) -> Evaluation | None:
        """Repair aggregate settlement P&L when every current position's cheaper side is held."""
        m = meta.market
        up = book_up.mid if book_up is not None else None
        down = book_down.mid if book_down is not None else None
        yes, no = m.yes_token_id, m.no_token_id
        ages = quote_ages or {}
        self.market_quotes[m.slug] = {"t": now, "meta": meta, "up": book_up, "down": book_down,
                                      "valid": (book_age_ms is not None and book_age_ms <= self.cfg.max_book_age_ms
                                                and ages.get(yes) is not None and ages.get(yes) <= self.scfg.max_quote_age_ms
                                                and ages.get(no) is not None and ages.get(no) <= self.scfg.max_quote_age_ms),
                                      "quote_ages": dict(ages)}

        # Seed old single-side positions (e.g. before this engine version tracked fills).
        for slug, rp in self.risk.open.items():
            if slug not in self.inventory and rp.side in ("UP", "DOWN"):
                self.inventory[slug] = {rp.side: {"shares": float(rp.shares), "cost": float(rp.usdc)}}
        held = []
        for slug in self.risk.open:
            snapshot = self.market_quotes.get(slug)
            # Expired positions await settlement and are outside the set of
            # currently open markets this rule coordinates.
            if snapshot is not None and snapshot["meta"].market.close_ts <= now:
                continue
            held.append((slug, self.risk.open.get(slug), self.inventory.get(slug), snapshot))
        if not held:
            return None

        leader_candidates, lower_needs = [], []
        leader_win_pnl = 0.0
        for slug, risk_pos, inv, snapshot in held:
            if (risk_pos is None or inv is None or snapshot is None or not snapshot.get("valid")
                    or now - snapshot["t"] > min(1000.0, self.scfg.max_quote_age_ms)):
                return None
            sm = snapshot["meta"].market
            if sm.close_ts <= now:
                return None
            su, sd = snapshot["up"], snapshot["down"]
            mu, md = (su.mid if su is not None else None), (sd.mid if sd is not None else None)
            if mu is None or md is None:
                return None
            leader = "UP" if mu >= md else "DOWN"
            lower = "DOWN" if leader == "UP" else "UP"
            lower_shares = inv.get(lower, {}).get("shares", 0.0)
            lower_min = max(5.0, float(sm.min_size) if sm.min_size else 5.0)
            if lower_shares < lower_min - 1e-9:
                lower_book = su if lower == "UP" else sd
                lower_ask = lower_book.best_ask if lower_book is not None else None
                lower_token = sm.yes_token_id if lower == "UP" else sm.no_token_id
                if lower_ask is None:
                    return None
                lower_needs.append((lower_ask, slug, lower, snapshot, lower_token,
                                    max(lower_min, lower_min - lower_shares)))
            leader_win_pnl += inv.get(leader, {}).get("shares", 0.0) - sum(x["cost"] for x in inv.values())
            leader_book = su if leader == "UP" else sd
            ask = leader_book.best_ask if leader_book is not None else None
            if ask is None:
                return None
            token = sm.yes_token_id if leader == "UP" else sm.no_token_id
            qa = (quote_ages or {}).get(token) if slug == m.slug and quote_ages is not None else None
            if slug == m.slug and (qa is None or qa > self.scfg.max_quote_age_ms):
                return None
            leader_candidates.append((ask, slug, leader, snapshot, leader_book, token, mu, md))

        # Complete the lower-side inventory in each held market before paying
        # for an all-leaders-win recovery. Only one additional order is sent at a time.
        if lower_needs:
            _, target_slug, side, snapshot, token, needed = min(lower_needs, key=lambda x: (x[0], x[1]))
            if target_slug != m.slug:
                return None
            if now - self.repair_last_try.get(target_slug, -1e18) < self.scfg.portfolio_repair_cooldown_s * 1000.0:
                return None
            return self._portfolio_order(snapshot["meta"], snapshot["up"], snapshot["down"], side, now,
                                         balance, snapshot["quote_ages"], needed, leader_win_pnl,
                                         "portfolio_lower_buy")

        if leader_win_pnl >= self.scfg.portfolio_repair_target_usdc - 1e-9:
            return None
        _, target_slug, side, snapshot, book, token, mu, md = min(leader_candidates, key=lambda x: (x[0], x[1]))
        if target_slug != m.slug:
            return None
        if now - self.repair_last_try.get(target_slug, -1e18) < self.scfg.portfolio_repair_cooldown_s * 1000.0:
            return None
        ask = book.best_ask
        limit = min(1.0 - (m.tick_size or 0.01), round(ask + min(self.scfg.max_slippage, self.cfg.max_slippage), 4))
        fee_fn = snapshot["meta"].fee.taker_fee_per_share
        unit = limit + fee_fn(limit)
        edge = 1.0 - unit
        if edge <= 1e-9:
            return None
        needed = math.ceil((self.scfg.portfolio_repair_target_usdc - leader_win_pnl) / edge * 100.0 - 1e-7) / 100.0
        return self._portfolio_order(snapshot["meta"], snapshot["up"], snapshot["down"], side, now,
                                     balance, snapshot["quote_ages"], needed, leader_win_pnl,
                                     "portfolio_recovery")

    def _portfolio_basket(self, ev: Evaluation, meta, book_up, book_down, now: float, balance: float,
                          book_age_ms: float | None, quote_ages=None) -> Evaluation | None:
        """Buy six cheap legs, then immediately recover with the frozen favourite market."""
        m = meta.market
        yes, no = m.yes_token_id, m.no_token_id
        ages = quote_ages or {}
        valid = (book_age_ms is not None and book_age_ms <= self.cfg.max_book_age_ms
                 and ages.get(yes) is not None and ages.get(yes) <= self.scfg.max_quote_age_ms
                 and ages.get(no) is not None and ages.get(no) <= self.scfg.max_quote_age_ms)
        self.market_quotes[m.slug] = {"t": now, "meta": meta, "up": book_up, "down": book_down,
                                      "valid": valid, "quote_ages": dict(ages)}
        for slug, rp in self.risk.open.items():
            if slug not in self.inventory and rp.side in ("UP", "DOWN"):
                self.inventory[slug] = {rp.side: {"shares": float(rp.shares), "cost": float(rp.usdc)}}

        assets = sorted({a.strip().upper() for a in self.scfg.assets.split(",") if a.strip()})
        count, buy_count = int(self.scfg.portfolio_basket_market_count), int(self.scfg.portfolio_basket_buy_count)
        if count < 2 or buy_count != count - 1 or len(assets) != count:
            return None
        group_key = str(int(m.open_ts // 1000))
        for old_key, old_plan in list(self.basket_plans.items()):
            old_epoch = int(old_plan.get("epoch", old_key))
            if old_epoch + 300 <= int(now // 1000):
                self.basket_plans.pop(old_key, None)

        # One armed/executing basket at a time unless a prior plan is complete/aborted.
        active = [p for p in self.basket_plans.values()
                  if not p.get("complete") and not p.get("aborted")
                  and p.get("state") not in (BasketState.COMPLETE.value, BasketState.PREFLIGHT_REJECT.value)]
        plan = self.basket_plans.get(group_key)
        if plan is None and any(str(p.get("epoch")) != group_key for p in active):
            self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, "mode": "basket_blocked",
                                            "why": "another basket is already armed",
                                            "event": EVT_RISK_LIMIT_REJECT}
            return None

        current = self._basket_sync_markets(group_key, assets, now)
        if len(current) != count or {x["asset"] for x in current} != set(assets):
            self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, "mode": "basket_wait",
                                            "state": BasketState.IDLE.value,
                                            "why": "waiting for seven fresh, synchronized market books"}
            return None
        current.sort(key=lambda x: (x["cheap_ask"], x["asset"], x["slug"]))
        lows = current[:buy_count]
        avg = sum(x["cheap_ask"] for x in lows) / buy_count
        # Inclusive: dashboard "average 0.14" with trigger 0.14 must arm.
        triggered = avg <= self.scfg.portfolio_basket_max_average_ask + 1e-9
        left = m.seconds_remaining(now)
        self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, "mode": "basket_monitor",
                                        "state": (plan or {}).get("state", BasketState.IDLE.value),
                                        "average_cheap_ask": round(avg, 4),
                                        "trigger": self.scfg.portfolio_basket_max_average_ask,
                                        "triggered": triggered, "time_remaining_s": round(left, 1)}

        if plan is not None and (plan.get("complete") or plan.get("aborted")):
            return None

        if plan is None:
            if not triggered:
                return None
            return self._basket_try_arm(group_key, current, lows, avg, now, balance, left, meta)

        # Frozen plan: never reshuffle cheap legs or favourite.
        plan["average_cheap_ask"] = avg
        current_by_slug = {x["slug"]: x for x in current}
        if any(leg["slug"] not in current_by_slug for leg in plan["cheap"]):
            return self._basket_abort(plan, now, m.asset, "selected market quotes not fresh",
                                      orphan=True)
        leader = plan["leader"]
        if leader["slug"] not in current_by_slug:
            return self._basket_abort(plan, now, m.asset, "favourite quote not fresh", orphan=True)

        if self.scfg.portfolio_basket_execution_abort:
            abort = self._basket_should_abort_cheap(plan, now, balance, left)
            if abort:
                return self._basket_abort(plan, now, m.asset, abort, orphan=True)

        all_cheap_filled, cheap_out = self._basket_execute_cheap_legs(plan, meta, now, balance)
        if cheap_out is not None:
            return cheap_out
        if not all_cheap_filled:
            pending = [{"asset": leg["asset"], "shares": round(max(
                0.0, self.scfg.portfolio_basket_shares
                - self.inventory.get(leg["slug"], {}).get(leg["side"], {}).get("shares", 0.0)), 2)}
                       for leg in plan["cheap"]
                       if self.scfg.portfolio_basket_shares
                       - self.inventory.get(leg["slug"], {}).get(leg["side"], {}).get("shares", 0.0) > 0.01]
            self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, "mode": "basket_progress",
                                            "state": BasketState.EXECUTING_CHEAP_LEGS.value,
                                            "basket_id": plan.get("basket_id"),
                                            "pending_cheap": pending, "reserved_cash": plan.get("reserved_cash"),
                                            "why": "waiting for remaining cheap-leg fills"}
            return None

        plan["state"] = BasketState.EXECUTING_LEADER.value
        return self._basket_execute_leader(plan, m, now, balance)

    def _basket_sync_markets(self, group_key: str, assets: set[str], now: float) -> list[dict]:
        current = []
        for slug, snap in self.market_quotes.items():
            sm = snap["meta"].market
            if (int(sm.open_ts // 1000) != int(group_key) or sm.close_ts <= now
                    or sm.asset.upper() not in assets or not snap.get("valid")
                    or now - snap["t"] > min(1000.0, self.scfg.max_quote_age_ms)):
                continue
            up, down = snap["up"], snap["down"]
            if up is None or down is None:
                continue
            up_mid, down_mid = up.mid, down.mid
            up_ask, down_ask = up.best_ask, down.best_ask
            up_bid, down_bid = up.best_bid, down.best_bid
            if None in (up_mid, down_mid, up_ask, down_ask, up_bid, down_bid) or up_ask == down_ask:
                continue
            if up_ask <= up_bid or down_ask <= down_bid:
                continue
            ages = snap.get("quote_ages") or {}
            cheap = "UP" if up_ask < down_ask else "DOWN"
            leader_side = "UP" if up_mid >= down_mid else "DOWN"
            current.append({
                "slug": slug, "asset": sm.asset.upper(), "cheap": cheap, "leader": leader_side,
                "cheap_ask": min(up_ask, down_ask), "up_ask": up_ask, "down_ask": down_ask,
                "up_bid": up_bid, "down_bid": down_bid, "open_ts": sm.open_ts,
                "up_age": ages.get(sm.yes_token_id, now - snap["t"]),
                "down_age": ages.get(sm.no_token_id, now - snap["t"]),
                "snap_t": snap["t"],
            })
        return current

    def _basket_try_arm(self, group_key: str, current: list[dict], lows: list[dict], avg: float,
                        now: float, balance: float, left: float, meta) -> Evaluation | None:
        sc = self.scfg
        asset = meta.market.asset
        leader_market = current[len(lows)]
        slip = min(sc.max_slippage, self.cfg.max_slippage)
        cheap_quotes, fee_fns = [], []
        for x in lows:
            snap = self.market_quotes[x["slug"]]
            sm = snap["meta"].market
            book = snap["up"] if x["cheap"] == "UP" else snap["down"]
            ask, bid = book.best_ask, book.best_bid
            _, depth = get_executable_price(book, sc.portfolio_basket_shares,
                                            min(1.0 - (sm.tick_size or 0.01), round(ask + slip, 4)))
            cheap_quotes.append(LegQuote(
                slug=x["slug"], asset=x["asset"], side=x["cheap"], ask=ask, bid=bid,
                depth=float(depth or 0.0), fee_per_share_at_limit=0.0,
                tick=float(sm.tick_size or 0.01), min_size=float(sm.min_size or 5.0),
                quote_age_ms=float(max(x["up_age"], x["down_age"])),
                spread=float(ask - bid)))
            fee_fns.append(snap["meta"].fee.taker_fee_per_share)
        lsnap = self.market_quotes[leader_market["slug"]]
        lm = lsnap["meta"].market
        lbook = lsnap["up"] if leader_market["leader"] == "UP" else lsnap["down"]
        la, lb = lbook.best_ask, lbook.best_bid
        l_limit = min(1.0 - (lm.tick_size or 0.01), round(la + slip, 4))
        _, ldepth = get_executable_price(lbook, 50.0, l_limit)
        lq = LegQuote(slug=leader_market["slug"], asset=leader_market["asset"],
                      side=leader_market["leader"], ask=la, bid=lb,
                      depth=float(ldepth or 0.0), fee_per_share_at_limit=0.0,
                      tick=float(lm.tick_size or 0.01), min_size=float(lm.min_size or 5.0),
                      quote_age_ms=float(max(leader_market["up_age"], leader_market["down_age"])),
                      spread=float(la - lb))
        ages = [q.quote_age_ms for q in (*cheap_quotes, lq)]
        # Also enforce wall-clock snap skew across markets.
        snap_ts = [self.market_quotes[x["slug"]]["t"] for x in lows] + [lsnap["t"]]
        skew = max(snap_ts) - min(snap_ts)
        pf = run_preflight(
            cheap_quotes=cheap_quotes, leader_quote=lq,
            shares_per_cheap=sc.portfolio_basket_shares,
            target_pnl=sc.portfolio_basket_target_pnl_usdc, slippage=slip,
            fee_fn_leader=lsnap["meta"].fee.taker_fee_per_share, fee_fns_cheap=fee_fns,
            available_cash=balance, cash_buffer_usdc=sc.portfolio_basket_cash_buffer_usdc,
            cash_util_max=sc.portfolio_basket_max_cash_utilization,
            min_leader_ask=sc.portfolio_basket_min_leader_ask,
            max_leader_ask=sc.portfolio_basket_max_leader_ask,
            max_leader_unit=sc.portfolio_basket_max_leader_unit_cost,
            max_basket_notional=sc.portfolio_basket_max_notional_usdc,
            max_leader_notional=sc.portfolio_basket_max_leader_notional_usdc,
            max_leader_multiple=sc.portfolio_basket_max_leader_multiple,
            max_all_lose=sc.portfolio_basket_max_all_lose_usdc,
            max_spread=min(sc.max_spread, self.cfg.max_spread),
            max_quote_age_ms=sc.max_quote_age_ms,
            max_quote_skew_ms=sc.portfolio_basket_max_quote_skew_ms,
            min_time_remaining_s=sc.portfolio_basket_arm_min_time_remaining_s,
            time_remaining_s=left, avg_cheap_ask=avg,
            max_average_ask=sc.portfolio_basket_max_average_ask,
        )
        if skew > sc.portfolio_basket_max_quote_skew_ms:
            pf = type(pf)(False, f"book quality: snap skew {skew:.0f}ms", EVT_PREFLIGHT_REJECT,
                          details={"skew_ms": skew})
        if not pf.ok:
            self._basket_event(None, pf.event, now, reason=pf.reason, **pf.details)
            self.last_cheapside[asset] = {"slug": "", "t": now, "mode": "basket_preflight",
                                          "state": BasketState.PREFLIGHT_REJECT.value,
                                          "event": pf.event, "why": pf.reason,
                                          "average_cheap_ask": round(avg, 4)}
            return None

        basket_id = f"{group_key}-{int(now)}"
        plan = {
            "basket_id": basket_id, "epoch": int(group_key), "created": now,
            "state": BasketState.ARMED.value, "complete": False, "aborted": False,
            "average_cheap_ask": avg, "armed_average_cheap_ask": avg,
            "initial_prices": {x["slug"]: {"cheap_ask": x["cheap_ask"], "side": x["cheap"]}
                               for x in lows} | {
                leader_market["slug"]: {"leader_ask": la, "side": leader_market["leader"]}},
            "expected_cheap_cost": pf.cheap_cost_est,
            "expected_leader_shares": pf.leader_shares,
            "expected_leader_unit": pf.leader_unit,
            "reserved_cash": pf.total_reserved,
            "reserved_hedge": pf.leader_notional,
            "projected_protected_pnl": pf.projected_protected_pnl,
            "all_lose_pnl": pf.all_lose_pnl,
            "cheap": [{"slug": x["slug"], "side": x["cheap"], "cheap": x["cheap"],
                        "leader": x["leader"], "asset": x["asset"],
                        "armed_ask": x["cheap_ask"]} for x in lows],
            "leader": {"slug": leader_market["slug"], "side": leader_market["leader"],
                        "cheap": leader_market["cheap"], "leader": leader_market["leader"],
                        "asset": leader_market["asset"], "armed_ask": la},
        }
        self.basket_plans[group_key] = plan
        self._basket_event(plan, EVT_PREFLIGHT_PASS, now, **pf.details)
        self._basket_event(plan, EVT_BASKET_ARMED, now, reserved=pf.total_reserved,
                           leader_shares=pf.leader_shares)
        plan["state"] = BasketState.EXECUTING_CHEAP_LEGS.value
        self.last_cheapside[asset] = {"slug": plan["cheap"][0]["slug"], "t": now,
                                      "mode": "basket_armed", "state": plan["state"],
                                      "event": EVT_BASKET_ARMED, "basket_id": basket_id,
                                      "reserved_cash": pf.total_reserved,
                                      "average_cheap_ask": round(avg, 4)}
        # Begin cheap-leg execution on this same evaluation when possible.
        return self._basket_execute_cheap_legs(plan, meta, now, balance)[1]

    def _basket_execute_cheap_legs(self, plan: dict, meta, now: float,
                                   balance: float) -> tuple[bool, Evaluation | None]:
        """Return (all_filled, order_or_None). Emits at most one order."""
        m = meta.market
        sc = self.scfg
        left = m.seconds_remaining(now)
        all_filled = True
        for leg in plan["cheap"]:
            snap = self.market_quotes.get(leg["slug"])
            if snap is None or not snap.get("valid") or snap["meta"].market.close_ts <= now:
                all_filled = False
                continue
            held = self.inventory.get(leg["slug"], {}).get(leg["side"], {}).get("shares", 0.0)
            need = max(0.0, sc.portfolio_basket_shares - held)
            if need <= sc.portfolio_basket_dust_shares:
                continue
            all_filled = False
            if leg["slug"] in self.risk.pending:
                continue
            # Entry cutoff: never start NEW cheap-leg purchases past the arm window.
            if left < sc.portfolio_basket_arm_min_time_remaining_s:
                self._basket_event(plan, EVT_CHEAP_LEG_FAILURE, now, reason="entry cutoff",
                                   asset=leg["asset"], left=left)
                continue
            retry_ms = sc.portfolio_basket_cheap_retry_ms
            retry_in = retry_ms - (now - self.repair_last_try.get(leg["slug"], 0.0))
            if leg["slug"] != m.slug:
                continue
            if retry_in > 0.0:
                self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, "mode": "basket_progress",
                                                "state": BasketState.EXECUTING_CHEAP_LEGS.value,
                                                "basket_id": plan.get("basket_id"),
                                                "pending_cheap_asset": leg["asset"],
                                                "shares_needed": round(need, 2),
                                                "retry_in_s": round(retry_in / 1000.0, 3)}
                return False, None
            out = self._portfolio_order(snap["meta"], snap["up"], snap["down"], leg["side"], now,
                                        balance, snap["quote_ages"], need, 0.0, "basket_low_buy")
            if out is None:
                self._basket_event(plan, EVT_CHEAP_LEG_FAILURE, now, asset=leg["asset"],
                                   reason=(self.last_cheapside.get(m.asset) or {}).get("why"))
                return False, None
            out.features["cheapside"].update({
                "average_cheap_ask": round(plan["average_cheap_ask"], 4),
                "basket_epoch": plan["epoch"], "basket_id": plan.get("basket_id"),
                "basket_state": plan.get("state"),
            })
            return False, out
        return all_filled, None

    def _basket_execute_leader(self, plan: dict, m, now: float, balance: float) -> Evaluation | None:
        leader = plan["leader"]
        leader_snap = self.market_quotes.get(leader["slug"])
        if (leader_snap is None or not leader_snap.get("valid")
                or now - leader_snap["t"] > min(1000.0, self.scfg.max_quote_age_ms)
                or leader_snap["meta"].market.close_ts <= now):
            self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, "mode": "basket_leader_size",
                                            "state": BasketState.EXECUTING_LEADER.value,
                                            "basket_id": plan.get("basket_id"),
                                            "leader_need": 0.0, "why": "waiting for a fresh leader quote"}
            return None
        if leader["slug"] in self.risk.pending:
            return None
        sizing = self._basket_leader_need(plan, now)
        if sizing is None:
            return self._basket_abort(plan, now, m.asset,
                                      "leader quote unaffordable or outside abort band", orphan=True)
        need, pnl_before, pnl_after, why = sizing
        dust = self.scfg.portfolio_basket_dust_shares
        if need <= dust:
            if pnl_before + 1e-9 >= self.scfg.portfolio_basket_target_pnl_usdc or need <= 0.0:
                return self._basket_mark_complete(plan, now, m.asset, pnl_before)
            # Dust remainder but target not met: treat as orphan exposure, do not chase.
            return self._basket_abort(plan, now, m.asset,
                                      f"leader dust {need:.4f} left; protected pnl {pnl_before:.4f}",
                                      orphan=True)
        # Risk caps on live leader size (never silently shrink while claiming the target).
        unit = self._leader_unit_cost(plan, now)
        leader_notional = need * unit
        sc = self.scfg
        if sc.portfolio_basket_max_leader_notional_usdc > 0 and leader_notional > sc.portfolio_basket_max_leader_notional_usdc:
            return self._basket_abort(plan, now, m.asset,
                                      f"leader notional {leader_notional:.2f} exceeds cap", orphan=True)
        prior = self.repair_last_try.get(leader["slug"], 0.0)
        leader_cooldown_ms = 0.0 if prior <= 0.0 else min(250.0, self.entry_retry_ms)
        retry_in = leader_cooldown_ms - (now - prior)
        if retry_in > 0.0:
            self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, "mode": "basket_leader_size",
                                            "state": BasketState.EXECUTING_LEADER.value,
                                            "basket_id": plan.get("basket_id"),
                                            "leader_need": round(need, 2),
                                            "retry_in_s": round(retry_in / 1000.0, 3),
                                            "why": "waiting for leader recovery retry"}
            return None
        prev_unit = float(plan.get("expected_leader_unit") or unit)
        if abs(unit - prev_unit) > 0.005:
            self._basket_event(plan, EVT_HEDGE_REPRICE, now, old_unit=prev_unit, new_unit=unit,
                               leader_need=need)
            plan["expected_leader_unit"] = unit
        self._basket_event(plan, EVT_LEADER_ORDER, now, shares=need, unit=unit,
                           projected_pnl=pnl_after)
        out = self._portfolio_order(leader_snap["meta"], leader_snap["up"], leader_snap["down"],
                                    leader["side"], now, balance, leader_snap["quote_ages"],
                                    need, pnl_before, "basket_leader_buy")
        if out is not None:
            out.features["cheapside"].update({
                "average_cheap_ask": round(plan["average_cheap_ask"], 4),
                "basket_epoch": plan["epoch"], "basket_id": plan.get("basket_id"),
                "basket_state": plan.get("state"),
                "leader_shares_needed": round(need, 2),
                "projected_protected_pnl": round(pnl_after, 4),
            })
        return out

    def _basket_should_abort_cheap(self, plan: dict, now: float, balance: float,
                                   left: float) -> str | None:
        """Abort NEW cheap-leg orders when hedge / risk / time deteriorate."""
        sc = self.scfg
        leader = plan["leader"]
        snap = self.market_quotes.get(leader["slug"])
        if snap is None or not snap.get("valid"):
            return "favourite book unavailable"
        book = snap["up"] if leader["side"] == "UP" else snap["down"]
        ask = book.best_ask if book is not None else None
        if ask is None:
            return "favourite ask missing"
        if ask > sc.portfolio_basket_abort_leader_ask + 1e-9:
            self._basket_event(plan, EVT_HEDGE_ABORT, now, reason="favorite above abort", ask=ask)
            return f"favorite ask {ask:.3f} > abort {sc.portfolio_basket_abort_leader_ask:.2f}"
        unit = self._leader_unit_cost(plan, now)
        if unit > sc.portfolio_basket_max_leader_unit_cost + 1e-9:
            self._basket_event(plan, EVT_HEDGE_ABORT, now, reason="unit unaffordable", unit=unit)
            return f"favorite all-in unit {unit:.4f} unaffordable"
        # Remaining hedge affordability against cash after filled costs.
        cheap_filled = sum(sum(float(p.get("cost", 0.0)) for p in self.inventory.get(leg["slug"], {}).values())
                           for leg in plan["cheap"])
        remaining_cheap = 0.0
        for leg in plan["cheap"]:
            held = self.inventory.get(leg["slug"], {}).get(leg["side"], {}).get("shares", 0.0)
            need = max(0.0, sc.portfolio_basket_shares - held)
            if need <= sc.portfolio_basket_dust_shares:
                continue
            csnap = self.market_quotes.get(leg["slug"])
            if csnap is None:
                continue
            cbook = csnap["up"] if leg["side"] == "UP" else csnap["down"]
            cask = cbook.best_ask if cbook is not None else None
            if cask is None:
                continue
            slip = min(sc.max_slippage, self.cfg.max_slippage)
            _, _, tot = estimate_leg_cost(cask, need, slip, csnap["meta"].fee.taker_fee_per_share,
                                          float(csnap["meta"].market.tick_size or 0.01))
            remaining_cheap += tot
        target = sc.portfolio_basket_target_pnl_usdc
        leader_inv = self.inventory.get(leader["slug"], {})
        already_sh = float(leader_inv.get(leader["side"], {}).get("shares", 0.0))
        already_cost = sum(float(p.get("cost", 0.0)) for p in leader_inv.values())
        projected_cheap = cheap_filled + remaining_cheap
        need_l, _ = leader_shares_for_target(projected_cheap, unit, target, already_sh, already_cost)
        hedge_cost = need_l * unit
        if cheap_filled + remaining_cheap + hedge_cost > balance - sc.portfolio_basket_cash_buffer_usdc + 1e-9:
            return "hedge unaffordable given remaining cash"
        if sc.portfolio_basket_max_notional_usdc > 0 and projected_cheap + hedge_cost > sc.portfolio_basket_max_notional_usdc:
            return "basket notional would exceed cap"
        orphan = cheap_filled  # if we stop now
        if sc.portfolio_basket_max_orphan_usdc > 0 and orphan > sc.portfolio_basket_max_orphan_usdc:
            # Still allow abort path to record orphan; do not add more legs.
            pass
        if left < sc.portfolio_basket_arm_min_time_remaining_s:
            unfilled = any(
                sc.portfolio_basket_shares
                - self.inventory.get(leg["slug"], {}).get(leg["side"], {}).get("shares", 0.0)
                > sc.portfolio_basket_dust_shares
                for leg in plan["cheap"])
            if unfilled:
                return "entry cutoff; stopping new cheap-leg purchases"
        daily = float(getattr(self.risk, "realised_today", 0.0) or 0.0)
        if sc.max_daily_loss_usdc > 0 and daily <= -sc.max_daily_loss_usdc:
            return f"daily loss limit ({daily:.2f})"
        return None

    def _basket_abort(self, plan: dict, now: float, asset: str, reason: str,
                      orphan: bool = False) -> None:
        plan["aborted"] = True
        plan["abort_reason"] = reason
        plan["state"] = BasketState.ORPHAN.value if orphan and self._basket_orphan_exposure(plan) > 0 else BasketState.ABORTED.value
        filled_cost = self._basket_orphan_exposure(plan)
        # Release reservation no longer needed; keep orphan cost recorded.
        plan["reserved_cash"] = 0.0
        plan["reserved_hedge"] = 0.0
        self._basket_event(plan, EVT_BASKET_ABORT, now, reason=reason, orphan_exposure=filled_cost)
        if filled_cost > 0:
            self._basket_event(plan, EVT_ORPHAN_EXPOSURE, now, exposure=filled_cost,
                               policy=self.scfg.portfolio_basket_orphan_policy)
            log.warning("cheapside basket %s ORPHAN_EXPOSURE=%.4f policy=%s reason=%s",
                        plan.get("basket_id"), filled_cost, self.scfg.portfolio_basket_orphan_policy, reason)
        self.last_cheapside[asset] = {"slug": "", "t": now, "mode": "basket_abort",
                                      "state": plan["state"], "event": EVT_BASKET_ABORT,
                                      "basket_id": plan.get("basket_id"), "why": reason,
                                      "orphan_exposure": filled_cost}
        return None

    def _basket_mark_complete(self, plan: dict, now: float, asset: str, protected_pnl: float) -> None:
        plan["complete"] = True
        plan["state"] = BasketState.COMPLETE.value
        plan["reserved_cash"] = 0.0
        plan["reserved_hedge"] = 0.0
        plan["actual_protected_pnl"] = protected_pnl
        summary = self.basket_settlement_summary(plan)
        plan["settlement_summary"] = summary
        self._basket_event(plan, EVT_BASKET_COMPLETE, now, protected_pnl=protected_pnl,
                           all_seven_lose=summary.get("all_seven_lose_pnl"),
                           min_pnl=summary.get("min_pnl"))
        self.last_cheapside[asset] = {"slug": "", "t": now, "mode": "basket_complete",
                                      "state": BasketState.COMPLETE.value,
                                      "event": EVT_BASKET_COMPLETE,
                                      "basket_id": plan.get("basket_id"),
                                      "protected_pnl": protected_pnl}
        return None

    def _basket_orphan_exposure(self, plan: dict) -> float:
        total = 0.0
        for leg in [*plan.get("cheap", []), plan.get("leader") or {}]:
            if not leg:
                continue
            for pos in self.inventory.get(leg.get("slug", ""), {}).values():
                total += float(pos.get("cost", 0.0))
        return total

    def _basket_reserved_cash(self, exclude_leader_spend: bool = False) -> float:
        total = 0.0
        for plan in self.basket_plans.values():
            if plan.get("complete") or plan.get("aborted"):
                continue
            if exclude_leader_spend:
                # Cheap-leg path: keep hedge notional reserved.
                total += float(plan.get("reserved_hedge") or 0.0)
            else:
                total += float(plan.get("reserved_cash") or 0.0)
        return total

    def _refresh_basket_reservation(self, plan: dict, now: float) -> None:
        """Shrink reservation to remaining cheap cost + required hedge after fills."""
        if plan.get("complete") or plan.get("aborted"):
            plan["reserved_cash"] = 0.0
            plan["reserved_hedge"] = 0.0
            return
        sc = self.scfg
        slip = min(sc.max_slippage, self.cfg.max_slippage)
        remaining_cheap = 0.0
        for leg in plan.get("cheap", []):
            held = self.inventory.get(leg["slug"], {}).get(leg["side"], {}).get("shares", 0.0)
            need = max(0.0, sc.portfolio_basket_shares - held)
            if need <= sc.portfolio_basket_dust_shares:
                continue
            snap = self.market_quotes.get(leg["slug"])
            if snap is None:
                remaining_cheap += need * 0.25  # conservative placeholder until quote returns
                continue
            book = snap["up"] if leg["side"] == "UP" else snap["down"]
            ask = book.best_ask if book is not None else None
            if ask is None:
                continue
            _, _, tot = estimate_leg_cost(ask, need, slip, snap["meta"].fee.taker_fee_per_share,
                                          float(snap["meta"].market.tick_size or 0.01))
            remaining_cheap += tot
        sizing = self._basket_leader_need(plan, now)
        hedge = 0.0
        if sizing is not None:
            need, _, _, _ = sizing
            hedge = need * self._leader_unit_cost(plan, now)
        plan["reserved_hedge"] = hedge
        plan["reserved_cash"] = remaining_cheap + hedge

    def _basket_event(self, plan: dict | None, event: str, now: float, **fields) -> None:
        row = {"event": event, "t": now, "basket_id": (plan or {}).get("basket_id"),
               "state": (plan or {}).get("state"), **fields}
        self.basket_events.append(row)
        if len(self.basket_events) > 500:
            self.basket_events = self.basket_events[-500:]
        # Preflight rejects fire on every market tick; rate-limit the log line.
        if event == EVT_PREFLIGHT_REJECT:
            reason = str(fields.get("reason") or "")
            key = f"preflight:{reason.split(':')[0][:48]}"
            last = getattr(self, "_basket_log_last", {}).get(key, 0.0)
            if now - last < 2000.0:
                return
            if not hasattr(self, "_basket_log_last"):
                self._basket_log_last = {}
            self._basket_log_last[key] = now
        log.info("cheapside %s basket_id=%s %s", event, row.get("basket_id"),
                 " ".join(f"{k}={v}" for k, v in fields.items() if k != "event"))

    def basket_settlement_summary(self, plan: dict) -> dict:
        """All 128 outcome PnLs for the current inventory of this basket."""
        legs: list[PositionLeg] = []
        cheap_sides = {}
        for leg in plan.get("cheap", []):
            inv = self.inventory.get(leg["slug"], {}).get(leg["side"], {})
            legs.append(PositionLeg(leg["slug"], leg["asset"], leg["side"],
                                    float(inv.get("shares", 0.0)), float(inv.get("cost", 0.0))))
            cheap_sides[leg["slug"]] = leg["side"]
        leader = plan.get("leader") or {}
        if leader:
            inv = self.inventory.get(leader["slug"], {}).get(leader["side"], {})
            legs.append(PositionLeg(leader["slug"], leader.get("asset", ""), leader["side"],
                                    float(inv.get("shares", 0.0)), float(inv.get("cost", 0.0))))
        slugs = [leg.slug for leg in legs]
        scenarios = all_outcome_scenarios(legs, slugs)
        return classify_scenarios(scenarios, list(cheap_sides), leader.get("slug", ""),
                                  leader.get("side", ""), cheap_sides)

    def _basket_wait_detail(self, asset: str) -> str:
        """Explain why basket mode did not submit an order on this evaluation."""
        info = self.last_cheapside.get(asset, {})
        mode = info.get("mode")
        if mode == "basket_monitor":
            average = float(info.get("average_cheap_ask", 0.0))
            trigger = float(info.get("trigger", self.scfg.portfolio_basket_max_average_ask))
            return f"six-token average ask {average:.3f}; basket buys when at or below {trigger:.3f}"
        if mode == "basket_wait":
            return str(info.get("why") or "basket is not currently executable")
        if mode == "basket_blocked":
            return str(info.get("why") or "basket order is blocked")
        if mode in ("basket_preflight", "basket_abort", "basket_armed", "basket_complete"):
            return f"{info.get('event') or mode}: {info.get('why') or info.get('state')}"
        if mode == "basket_progress":
            pending = info.get("pending_cheap") or []
            assets = ", ".join(f"{x['asset']} ({x['shares']:.2f} sh)" for x in pending)
            if info.get("pending_cheap_asset"):
                assets = f"{info['pending_cheap_asset']} ({info.get('shares_needed', 0):.2f} sh)"
            why = info.get("why") or (f"retry in {info['retry_in_s']:.2f}s"
                                        if info.get("retry_in_s") else "retrying")
            return f"basket in progress; cheap legs still needed: {assets or 'none'}; {why}"
        if mode == "basket_leader_size":
            return (f"six cheap legs filled; leader leg needs {float(info.get('leader_need', 0.0)):.2f} shares; "
                    f"{info.get('why') or 'waiting for its order window'}")
        return "waiting for seven fresh, synchronized market books"

    def _leader_unit_cost(self, plan: dict, now: float) -> float:
        leader = plan["leader"]
        snap = self.market_quotes.get(leader["slug"])
        if snap is None:
            return 1.0
        sm = snap["meta"].market
        book = snap["up"] if leader["side"] == "UP" else snap["down"]
        ask = book.best_ask if book is not None else None
        if ask is None:
            return 1.0
        limit = min(1.0 - (sm.tick_size or 0.01), round(ask + min(self.scfg.max_slippage, self.cfg.max_slippage), 4))
        return self._basket_unit_cost(snap["meta"].fee, ask, limit)

    @staticmethod
    def _basket_unit_cost(fee, ask: float, limit: float) -> float:
        """Worst fee per share between the visible ask and our limit, plus the limit price."""
        return unit_cost(ask, limit, fee.taker_fee_per_share)

    def _basket_leader_need(self, plan: dict, now: float) -> tuple[float, float, float, str] | None:
        """Size the frozen favourite to recover the six cheap legs' worst-case cost."""
        target = self.scfg.portfolio_basket_target_pnl_usdc
        leader = plan["leader"]
        snap = self.market_quotes.get(leader["slug"])
        if snap is None or not snap.get("valid"):
            return None
        sm = snap["meta"].market
        book = snap["up"] if leader["side"] == "UP" else snap["down"]
        ask = book.best_ask if book is not None else None
        if ask is None:
            return None
        # Soft band for leader *orders*: allow up to abort price while executing.
        if ask > self.scfg.portfolio_basket_abort_leader_ask + 1e-9:
            return None
        if ask < self.scfg.portfolio_basket_min_leader_ask - 1e-9:
            return None
        limit = min(1.0 - (sm.tick_size or 0.01), round(ask + min(self.scfg.max_slippage, self.cfg.max_slippage), 4))
        unit = self._basket_unit_cost(snap["meta"].fee, ask, limit)
        if unit >= 1.0 or unit > self.scfg.portfolio_basket_max_leader_unit_cost + 1e-9:
            return None
        cheap_cost = sum(sum(float(p.get("cost", 0.0)) for p in self.inventory.get(leg["slug"], {}).values())
                         for leg in plan["cheap"])
        # Include projected cost of any unfilled cheap dust only via actual fills;
        # sizing uses actual acquisition costs as required.
        leader_inv = self.inventory.get(leader["slug"], {})
        leader_shares = float(leader_inv.get(leader["side"], {}).get("shares", 0.0))
        leader_cost = sum(float(p.get("cost", 0.0)) for p in leader_inv.values())
        venue_min = max(5.0, float(sm.min_size) if sm.min_size else 5.0)
        need, pnl_after = leader_shares_for_target(
            cheap_cost, unit, target, leader_shares, leader_cost,
            venue_min=venue_min, dust=self.scfg.portfolio_basket_dust_shares)
        pnl_before = leader_shares - cheap_cost - leader_cost
        return need, pnl_before, pnl_after, "recover six-cheap worst-case loss"

    def _forced_book_entry(self, ev: Evaluation, meta, book_up, book_down, now, balance,
                           quote_ages=None) -> Evaluation:
        """Bounded paper sample while spot/model history warms up; choose the cheaper ask."""
        sc, m = self.scfg, meta.market
        left = m.seconds_remaining(now)
        if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
        ask_up = book_up.best_ask if book_up is not None else None
        ask_dn = book_down.best_ask if book_down is not None else None
        side = cheap_side(ask_up, ask_dn)
        if side is None:
            return self._reject(ev, Reason.NO_OFFER, "no ask on both books, or equal asks")
        book = book_up if side == "UP" else book_down
        ask, bid = book.best_ask, book.best_bid
        token = m.yes_token_id if side == "UP" else m.no_token_id
        if not (sc.min_ask <= ask <= sc.max_ask):
            return self._reject(ev, Reason.PRICE_BAND, f"{side} ask {ask:.2f} outside {sc.min_ask:.2f}-{sc.max_ask:.2f}")
        if bid is None:
            return self._reject(ev, Reason.NO_OFFER, f"no {side} bid")
        spread = ask - bid
        if spread > min(sc.max_spread, self.cfg.max_spread) + 1e-9:
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"spread {spread:.3f}")
        qa = (quote_ages or {}).get(token) if quote_ages is not None else None
        if quote_ages is not None and qa is None:
            return self._reject(ev, Reason.STALE_DATA, "no valid book on the current feed connection")
        if qa is not None and qa > sc.max_quote_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"{side} quote {qa:.0f}ms > {sc.max_quote_age_ms:.0f}")
        self._close_ts[m.slug] = m.close_ts
        self._forget_closed(now)
        if self.trades.get(m.slug, 0) >= int(sc.max_entries_per_market):
            return self._reject(ev, Reason.MARKET_ENTERED, "this market already has its position")
        if self.attempts.get(m.slug, 0) >= int(sc.max_attempts_per_market):
            return self._reject(ev, Reason.MARKET_ATTEMPTS, f"{self.attempts[m.slug]} attempts already")
        limit = min(1.0 - (m.tick_size or 0.01), round(ask + min(sc.max_slippage, self.cfg.max_slippage), 4))
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
            size = fit
        exec_price, avail = get_executable_price(book, size, limit)
        if exec_price is None:
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"{avail:.0f} shares under {limit:.2f}, need {size:.0f}")
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)
        if size * limit > balance + 1e-9:
            return self._reject(ev, Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * limit:.2f}")
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, limit, exec_price
        ev.max_entry_price, ev.size, ev.net_edge, ev.confidence = limit, size, 0.0, 1.0
        ev.size_note = f"{size:g} shares ({size * limit:.2f} USDC worst case); forced paper sample"
        ev.features = {"cheapside": {"forced_one_per_market": True, "model_warmup_fallback": True,
                                      "side": side, "ask": ask, "bid": bid, "limit": limit,
                                      "spread": spread, "depth": avail, "quote_age_ms": qa}}
        ev.p_used = 0.5
        ev.p_market = ask
        ev.spot_move_bps = None
        self.last[m.slug] = ev
        return ev

    # --------------------------------------------------------------- entry

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        cfg, sc, m = self.cfg, self.scfg, meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        if not sc.asset_ok(m.asset):
            return self._reject(ev, Reason.ASSET_FILTERED, f"{m.asset} excluded")
        if sc.portfolio_basket_enabled:
            basket = self._portfolio_basket(ev, meta, book_up, book_down, now, balance, book_age_ms, quote_ages)
            if basket is not None:
                return basket
            return self._reject(ev, Reason.MODEL_NOT_READY, self._basket_wait_detail(m.asset))
        recovery = self._portfolio_recovery(meta, book_up, book_down, now, balance, quote_ages, book_age_ms)
        if recovery is not None:
            return recovery
        if book_age_ms is None or book_age_ms > cfg.max_book_age_ms:
            return self._reject(ev, Reason.STALE_DATA, "book" if book_age_ms is None else f"book {book_age_ms:.0f}ms")
        if spot is None or spot_age_ms is None or spot_age_ms > min(cfg.max_spot_age_ms, sc.max_spot_age_ms):
            if sc.force_one_per_market:
                return self._forced_book_entry(ev, meta, book_up, book_down, now, balance, quote_ages)
            detail = "no spot" if spot is None or spot_age_ms is None else f"spot {spot_age_ms:.0f}ms"
            return self._reject(ev, Reason.STALE_DATA, detail)
        if m.strike <= 0.0 and not sc.force_one_per_market:
            return self._reject(ev, Reason.NO_STRIKE)
        if vol is None or not getattr(vol, "ready", False) or twap is None or not twap.ready:
            if sc.force_one_per_market:
                return self._forced_book_entry(ev, meta, book_up, book_down, now, balance, quote_ages)
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
            if sc.force_one_per_market:
                return self._forced_book_entry(ev, meta, book_up, book_down, now, balance, quote_ages)
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
        selected_side, selected_book, selected_token = side, book, token
        selected_ask, selected_bid, selected_spread = ask, bid, spread
        selected_p, selected_limit, selected_edge = p, limit, edge
        selected_qa, selected_support = qa, supported(f)
        selection_mode = "cheap"
        leader_side = "DOWN" if side == "UP" else "UP"
        leader_p = 1.0 - p
        leader_supported = f["z1000"] < 0.0 and (3.0 - f["consist"]) >= 2.0
        if sc.leader_buy_enabled and leader_p >= sc.leader_min_win_probability and leader_supported:
            leader_book = book_down if leader_side == "DOWN" else book_up
            leader_ask = leader_book.best_ask if leader_book is not None else None
            leader_bid = leader_book.best_bid if leader_book is not None else None
            leader_spread = leader_ask - leader_bid if leader_ask is not None and leader_bid is not None else None
            leader_token = m.no_token_id if leader_side == "DOWN" else m.yes_token_id
            leader_qa = (quote_ages or {}).get(leader_token) if quote_ages is not None else book_age_ms
            if (leader_ask is not None and leader_bid is not None
                    and sc.leader_min_ask - 1e-9 <= leader_ask <= sc.leader_max_ask + 1e-9
                    and leader_spread is not None and leader_spread <= min(sc.max_spread, cfg.max_spread) + 1e-9
                    and (quote_ages is None or (leader_qa is not None and leader_qa <= sc.max_quote_age_ms))):
                leader_limit = min(1.0 - (m.tick_size or 0.01),
                                   round(leader_ask + min(sc.max_slippage, cfg.max_slippage), 4))
                leader_fee = meta.fee.taker_fee_per_share(leader_limit)
                leader_edge = leader_p - leader_limit - leader_fee - sc.slippage - sc.uncertainty
                if leader_edge >= sc.min_net_edge:
                    selected_side, selected_book, selected_token = leader_side, leader_book, leader_token
                    selected_ask, selected_bid, selected_spread = leader_ask, leader_bid, leader_spread
                    selected_p, selected_limit, selected_edge = leader_p, leader_limit, leader_edge
                    selected_qa, selected_support = leader_qa, True
                    selection_mode = "strong_leader"
                    info.update({"cheap_side": side, "cheap_model_p": round(p, 4),
                                 "leader_side": leader_side, "leader_model_p": round(leader_p, 4),
                                 "leader_edge": round(leader_edge, 4),
                                 "z250": round(f["z250"] * -1, 3), "z500": round(f["z500"] * -1, 3),
                                 "z1000": round(f["z1000"] * -1, 3), "z3000": round(f["z3000"] * -1, 3),
                                 "consist": 3.0 - f["consist"], "p_fair": round(_sigmoid(-f["logit_fair"]), 3),
                                 "accel": round(-f["accel"], 3)})
        selected_d = 1 if selected_side == "UP" else -1
        info.update({"side": selected_side, "direction": selected_side, "selection_mode": selection_mode,
                     "ask": selected_ask, "bid": selected_bid, "limit": round(selected_limit, 4),
                     "p_model": round(selected_p, 4), "edge": round(selected_edge, 4),
                     "quote_age_ms": None if selected_qa is None else round(selected_qa),
                     "support": selected_support})
        for h in (0.05, 0.1):
            if rets[h] is not None:
                info[f"z{int(h * 1000)}"] = round(rets[h] / (sigma * math.sqrt(h)) * d, 3)
        ev.features = {"cheapside": info,
                       "momentum": {"label": ("LEADER_" if selection_mode == "strong_leader" else "CHEAP_") + selected_side,
                                    "z": {"main": round(f["z1000"] * selected_d * d, 3),
                                          "3s": round(f["z3000"] * selected_d * d, 3)},
                                    "score": round(abs(f["z1000"]), 3)},
                       "execution": {"fair": round(selected_p, 4), "cap": round(selected_limit, 4),
                                     "best_ask": selected_ask, "best_bid": selected_bid,
                                     "spread": round(selected_spread, 4), "quote_age_ms": info["quote_age_ms"],
                                     "spot_age_ms": round(spot_age_ms)}}
        ev.spot_move_bps = rets[1.0] * 1e4
        ev.p_used = selected_p if selected_side == "UP" else 1.0 - selected_p
        selected_z1000, selected_z3000 = f["z1000"] * selected_d * d, f["z3000"] * selected_d * d
        self.last_signal[m.asset] = MomentumSignal(direction=selected_d, label=ev.features["momentum"]["label"],
                                                   z={"main": selected_z1000, "3s": selected_z3000}, sigma_per_sec=sigma)
        sv = SideView(side=selected_side, token_id=selected_token, p_model=selected_p, p_market=selected_ask,
                      ask=selected_ask, bid=selected_bid, depth_at_limit=0.0,
                      costs=None, reason=None, entry_px=None)
        ev.sides = [sv]
        self.last_cheapside[m.asset] = {"slug": m.slug, "t": now, **info}
        support_ok = selected_support
        edge_ok = selected_edge >= sc.min_net_edge
        forced = sc.force_one_per_market and not (support_ok and edge_ok)
        if sc.require_support and not support_ok and not forced:
            sv.reason = Reason.WEAK_MOMENTUM
            return self._reject(ev, Reason.WEAK_MOMENTUM,
                                f"spot does not back {selected_side}: z1 {selected_z1000:+.2f}, {f['consist']:.0f}/3 agree")
        if not edge_ok and not forced:
            sv.reason = Reason.EDGE_TOO_SMALL
            return self._reject(ev, Reason.EDGE_TOO_SMALL,
                                f"model {selected_p:.3f} - limit {selected_limit:.2f} - costs = {selected_edge:+.3f} < {sc.min_net_edge}")
        if forced:
            # Keep the fallback visible in the decision log and order tag. This
            # is a paper-only sampling mode, not a model-qualified signal.
            info["forced_one_per_market"] = True
        # an OPPORTUNITY: every refusal from here is logged as a skip
        info["signal"] = True
        if self.trades.get(m.slug, 0) >= int(sc.max_entries_per_market):
            return self._reject(ev, Reason.MARKET_ENTERED, "this market already has its position")
        if self.attempts.get(m.slug, 0) >= int(sc.max_attempts_per_market):
            return self._reject(ev, Reason.MARKET_ATTEMPTS, f"{self.attempts[m.slug]} attempts already")
        venue_min = float(m.min_size) if m.min_size else 5.0
        size = max(venue_min, float(sc.size_shares))
        per_order = min(sc.max_order_usdc, self.risk.cfg.max_position_usdc)
        room = self.risk.capacity_total()
        bound = min(per_order, room)
        if size * selected_limit > bound + 1e-9:
            fit = math.floor(bound / selected_limit * 100.0) / 100.0
            if fit + 1e-9 < venue_min:
                return self._reject(ev, Reason.EXPOSURE_CAP if room < per_order else Reason.RISK_LIMIT,
                                    f"{bound:.2f} USDC of room, the minimum order is {venue_min * selected_limit:.2f}")
            info["size_reduced_from"] = size
            size = fit
        exec_price, avail = get_executable_price(selected_book, size, selected_limit)
        sv.depth_at_limit = avail
        if exec_price is None:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"{avail:.0f} shares under {selected_limit:.2f}, need {size:.0f}")
        sv.entry_px = exec_price
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * selected_limit)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)
        if size * selected_limit > balance + 1e-9:
            return self._reject(ev, Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * selected_limit:.2f}")
        info.update({"size": size, "worst_case_usdc": round(size * selected_limit, 4)})
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = selected_side, selected_token, selected_limit, exec_price
        ev.max_entry_price = selected_limit
        ev.size, ev.net_edge, ev.confidence = size, selected_edge, 1.0
        ev.size_note = (f"{size:g} shares ({size * selected_limit:.2f} USDC worst case)"
                        + ("; forced paper sample" if forced else ""))
        self.last[m.slug] = ev
        return ev

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        cs = (ev.features or {}).get("cheapside", {})
        # 2026-10-07: the order type was hard-coded FOK whatever TPB_CHEAPSIDE_ORDER_TYPE said; on the thin books of
        # cheap tokens (12-60 shares at the ask) 14 of 15 all-or-nothing orders were killed. FAK takes what is there.
        forced = bool(cs.get("forced_one_per_market"))
        if cs.get("basket_low_buy"):
            tag = f"{ev.side} cheapside basket_low avg={cs.get('average_cheap_ask', 0):.3f} x {ev.size:g}"
        elif cs.get("basket_leader_buy"):
            tag = (f"{ev.side} cheapside basket_leader avg={cs.get('average_cheap_ask', 0):.3f} "
                   f"need={cs.get('leader_shares_needed', ev.size):.2f} x {ev.size:g}")
        elif cs.get("portfolio_recovery"):
            tag = (f"{ev.side} cheapside portfolio_recovery leader_win_pnl={cs.get('leader_win_pnl', 0):+.2f} "
                   f"target=+{cs.get('target_pnl', self.scfg.portfolio_repair_target_usdc):.2f}")
        elif cs.get("portfolio_lower_buy"):
            tag = f"{ev.side} cheapside portfolio_lower_buy ask={cs.get('ask', 0):.2f}"
        else:
            tag = (f"{ev.side} cheapside{' FORCED_PAPER' if forced else ''} "
                   f"p={cs.get('p_model', 0):.3f} edge={ev.net_edge:+.3f} cap={ev.limit_price:.2f}")
        # Cheapside entries must always take displayed asks and must never
        # rest on the book. FAK/IOC is taker-only; partial matches are kept.
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                     tif=TimeInForce.IOC,
                     expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                     tag=tag)

    def signal_text(self, ev: Evaluation) -> str:
        cs = (ev.features or {}).get("cheapside", {})
        if cs.get("basket_low_buy"):
            return (f"BASKET CHEAP {ev.side} ask {cs.get('ask'):.3f}; six-token mean "
                    f"{cs.get('average_cheap_ask'):.3f}")
        if cs.get("basket_leader_buy"):
            return (f"BASKET LEADER {ev.side} ask {cs.get('ask'):.3f}; recovering six-token worst case "
                    f"from {cs.get('average_cheap_ask'):.3f} mean")
        if cs.get("portfolio_recovery"):
            return (f"PORTFOLIO RECOVERY {ev.side} ask {cs.get('ask'):.3f}; all-leaders PnL "
                    f"{cs.get('leader_win_pnl'):+.2f} -> target +{cs.get('target_pnl'):.2f}")
        if cs.get("portfolio_lower_buy"):
            return f"PORTFOLIO LOWER BUY {ev.side} ask {cs.get('ask'):.3f}"
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
        snap["cheapside_baskets"] = dict(self.basket_plans)
        snap["cheapside_basket_events"] = list(self.basket_events[-50:])
        snap["cheapside_projection"] = self._basket_projection()
        snap["cheapside_model"] = {"path": self.scfg.model_path, "platt": [self.model.a, self.model.b],
                                   "rules": self.model.rules}
        return snap

    def _basket_projection(self) -> dict | None:
        """Current marked value and protected / all-lose settlement scenarios for the newest basket."""
        candidates = sorted(self.basket_plans.values(), key=lambda p: int(p.get("epoch", 0)), reverse=True)
        quote_now = max((float(s.get("t", 0.0)) for s in self.market_quotes.values()), default=0.0)
        buy_count = int(self.scfg.portfolio_basket_buy_count)
        for plan in candidates:
            legs = list(plan.get("cheap", []))
            leader = plan.get("leader")
            if len(legs) != buy_count or not leader:
                continue
            all_legs = [*legs, leader]
            rows = []
            totals = {"now_value": 0.0, "cost": 0.0, "protected_value": 0.0,
                      "all_lose_value": 0.0, "shares": 0.0}
            for leg in all_legs:
                snap = self.market_quotes.get(leg.get("slug"))
                if (snap is None or not snap.get("valid")
                        or quote_now - float(snap.get("t", 0.0))
                        > min(1000.0, self.scfg.max_quote_age_ms)):
                    continue
                sm = snap["meta"].market
                inv = self.inventory.get(leg["slug"], {})
                up_mid = snap["up"].mid if snap.get("up") is not None else None
                down_mid = snap["down"].mid if snap.get("down") is not None else None
                if up_mid is None or down_mid is None:
                    continue
                up = inv.get("UP", {})
                down = inv.get("DOWN", {})
                cost = sum(float(x.get("cost", 0.0)) for x in inv.values())
                now_value = float(up.get("shares", 0.0)) * up_mid + float(down.get("shares", 0.0)) * down_mid
                held = float(inv.get(leg["side"], {}).get("shares", 0.0))
                is_leader = leg.get("slug") == leader.get("slug")
                # Protected: cheap legs expire worthless; favourite pays $1.
                protected_add = held if is_leader else 0.0
                totals["now_value"] += now_value
                totals["cost"] += cost
                totals["protected_value"] += protected_add
                totals["all_lose_value"] += 0.0
                totals["shares"] += float(up.get("shares", 0.0)) + float(down.get("shares", 0.0))
                chosen_book = snap["up"] if leg["side"] == "UP" else snap["down"]
                rows.append({"asset": sm.asset.upper(), "side": leg["side"],
                             "role": "leader" if is_leader else "cheap",
                             "now_price": chosen_book.mid if chosen_book is not None else None,
                             "shares": held, "now_value": round(now_value, 4), "cost": round(cost, 4)})
            if rows:
                summary = self.basket_settlement_summary(plan) if any(
                    self.inventory.get(x.get("slug", "")) for x in all_legs) else {}
                return {
                    "epoch": plan.get("epoch"), "basket_id": plan.get("basket_id"),
                    "state": plan.get("state"), "quote_count": len(rows),
                    "market_count": int(self.scfg.portfolio_basket_market_count),
                    "reserved_cash": plan.get("reserved_cash"),
                    "updated": max(self.market_quotes[x["slug"]]["t"]
                                   for x in all_legs if x.get("slug") in self.market_quotes),
                    "rows": rows,
                    "settlement_summary": summary,
                    "totals": {**{k: round(v, 4) for k, v in totals.items()},
                               "now_pnl": round(totals["now_value"] - totals["cost"], 4),
                               "protected_pnl": round(totals["protected_value"] - totals["cost"], 4),
                               "all_seven_lose_pnl": round(totals["all_lose_value"] - totals["cost"], 4)},
                }
        return None
