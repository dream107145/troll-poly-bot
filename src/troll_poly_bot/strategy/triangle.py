"""TRIANGLE: buy one share on every FILLED sudden-move triangle the dashboard draws, hold to settlement.

The user's rule (2026-10-01): "When each triangle displays, create an order buying 1 corresponding
token -- ▲ buy 1 Up, ▼ buy 1 Down -- and hold all bought tokens to settlement." The triangles are
the spot-lag engine's SHOCK MARKS (strategy/spot_lag.py ``shock_marks``; app.js ``drawShocks``);
the user chose the FILLED ones only, i.e. the moments a sudden spot move passed the spot-lag
SIGNAL gates (k_sigma 1 s shock, the 3 s return agreeing, accelerating, the trend filter, the
2 s per-direction de-duplication). Everything spot-lag checks AFTER the signal -- one entry per
market, the lag / expected value, price band, spread, max entry, net edge, size, budget -- is
not applied: every filled triangle is one order.

EXPERIMENTAL, PAPER ONLY, untested: no study stands behind it. Its own id (``triangle``), env
prefix (``TPB_TRIANGLE_*``), budget and data files; spot-lag and the other strategies are
untouched. Behind ``TPB_TRIANGLE_ENABLED=1``; __main__ refuses micro-live, live and shadow for it
unconditionally (and 1 share is below the venue's 5-share minimum anyway).

    trigger   the spot-lag signal (a filled ▲ / ▼), with the paper mirror's signal settings
    order     ``shares`` (1) of the triangle's side, FOK at ask + ``limit_offset``
    stacking  every triangle is a new order: several per market, both sides allowed
    pair      (``pair_cost`` > 0) after each buy fills, a resting BUY of the OTHER side at
              ``pair_cost`` - the fill's average price, the same shares, until the market closes;
              filled, the pair cost ``pair_cost`` and pays 1 at settlement. While any pair order is
              working (``wait_for_pair``), no new buy: the triangle is drawn hollow, "waiting for the pair"
    exit      none; every share settles at 1 / 0
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

from ..features.engine import FeatureEngine
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason, SideView
from .momentum import MomentumConfig, MomentumSignal
from .flowsignal import FlowParams, FlowResult, FlowSignal
from .spot_lag import SpotLagConfig, SpotLagEngine
from .paired_exit_engine import PairedExitMixin

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_TRIANGLE_ENABLED"
STRATEGY_ID = "triangle"


@dataclass(slots=True)
class TriangleConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_TRIANGLE_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    # --- the signal: the spot-lag fields, defaulted to the paper mirror's (tpb-spotlag-paper-mirror,
    # 2026-10-01) so this bot's filled triangles are the ones the mirror's chart shows
    max_spot_age_ms: float = 500.0          # the mirror's "optimized" profile
    trend_window_s: float = 30.0
    trend_min_bps: float = 0.0
    fv_trigger_cents: float = 0.0
    exclude_assets: str = "HYPE"
    event_driven: bool = True
    # --- the signal source (2026-10-01, the user's cross-exchange momentum + order-flow strategy) ---
    #: "spot_lag": the 1-second spot shock (above); "flow": every Coinbase / Bybit / Binance tick and trade
    #: scores a direction at once (strategy/flowsignal.py). In flow mode spot-lag's own detections are not
    #: traded or drawn: the triangles are the flow signals. Defaults calibrated on recorded ticks (09-23 1.5 h,
    #: 09-29 0.75 h): 130-145 signals / hour on BTC, 93-96 % of them followed by a move the signal's way
    #: within 0.5-1 s (mean +0.6 to +0.75 bp). The spec's own 2 bp scale gave 7-19 signals / hour.
    signal_source: str = "spot_lag"
    flow_exchanges: str = "coinbase,bybit,binance"
    flow_windows_ms: str = "20,50,100"
    flow_primary_ms: float = 50.0
    flow_ref_bp: float = 0.6
    flow_min_bp: float = 0.2
    flow_min_confirm: int = 2
    flow_min_fresh: int = 2
    flow_gate_bp: float = 0.15
    flow_trade_window_ms: float = 250.0
    flow_trade_full_usd: float = 20_000.0
    flow_accel_ref_bp: float = 0.6
    flow_first_lookback_ms: float = 200.0
    flow_first_cross_bp: float = 0.5
    flow_w_cx: float = 0.35
    flow_w_flow: float = 0.30
    flow_w_acc: float = 0.20
    flow_w_first: float = 0.15
    flow_threshold: float = 0.70
    flow_dedupe_ms: float = 1000.0
    flow_valid_ms: float = 500.0
    #: NO CHASING (2026-10-01 ~16:55, the user: "the bot buys when the price is about to go down, after the rise
    #: ended -- the signal is too late"): a signal is skipped when the side's ask has ALREADY risen by more than
    #: this (USDC) over the last ``flow_token_lookback_ms`` -- the reprice has happened and a buy now is the top.
    #: 0 = any rise skips; negative = off. The triangle is drawn hollow: "token already repriced +Xc".
    flow_max_token_move: float = -1.0
    flow_token_lookback_ms: float = 1000.0
    #: a flow signal is only traded with at least this many seconds left (the spot-lag engine's own window
    #: starts at ``min_time_remaining_s``; the high-zone rule below needs the last minute too)
    flow_min_left_s: float = 60.0
    # --- the price band (2026-10-01, the user: no buys over 0.96; floor 0.05, raised to 0.20 at ~15:35 UTC) ---
    #: the token's ASK must lie in [min_price, max_price]; the order's limit is capped at max_price. The band
    #: governs the BUY a triangle (or the high zone) makes; the other-side ask that follows is priced
    #: pair_cost - fill, often under the floor, and is exempt
    min_price: float = 0.20
    max_price: float = 0.96
    # --- the HIGH ZONE rule (2026-10-01, the user: "a token still pricing up over 0.85: buy 5 shares every 5 s
    # and create the other side's ask") ---
    #: a token whose ask is above ``hz_min_price`` and HIGHER than ``hz_lookback_s`` ago (and not falling now)
    #: buys ``shares`` every ``hz_every_s`` seconds per market, whatever the flow signal says; every buy gets
    #: its pair order like any other
    hz_enabled: bool = False
    hz_min_price: float = 0.85
    hz_every_s: float = 5.0
    hz_lookback_s: float = 5.0
    # --- the order ---
    #: shares bought per triangle
    shares: float = 1.0
    #: the FOK limit: the side's ask plus this (never above 1 - tick)
    limit_offset: float = 0.02
    order_type: str = "FOK"
    #: the pre-send book-age check (live._confirm_entry); generous, since every triangle should
    #: become an order and a stale ask only means the FOK is killed on arrival
    max_quote_age_ms: float = 10_000.0
    #: PAIR ORDER (2026-10-01, the user's rule "when the bot buys a token at a price, put an order for the
    #: other token at 0.95 - that price"): after each buy fills, a resting (GTC) BUY of the other side at
    #: this minus the fill's average price, rounded DOWN to the tick, the same shares, until the market
    #: closes. Skipped when that price is under one tick or the shares are under the venue minimum.
    #: 0 = off.
    pair_cost: float = 0.0
    #: (2026-10-01, the user: "if at least one pair is not filled, do not buy more tokens until the pair
    #: resolves") while any pair order is working -- placed, neither filled nor expired at its market's
    #: close -- every new triangle is refused, in every market
    wait_for_pair: bool = True
    #: (2026-10-01 12:50, the user: "it doesn't always make the other side's order -- very risky") with
    #: ``pair_cost`` on, never buy where no pair can follow: the ask must be at most ``pair_cost`` - one tick
    #: and the order's limit is capped there, so every fill leaves a pair price of at least one tick. Before
    #: this, 38 of 200 fills (09:40 h) were at 0.95-0.99 and got no pair at all.
    pair_room_required: bool = True
    #: (2026-10-01 ~20:20, the user: "sum of two tokens must be under 0.95") the pair price is the highest tick
    #: with fill + pair STRICTLY below ``pair_cost`` (0.60 -> 0.34, sum 0.94); False keeps fill + pair <= it.
    #: With ``pair_room_required`` the buys stop two ticks under ``pair_cost``, so every fill gets a pair.
    pair_cost_strict: bool = False
    #: HEDGE MODE (2026-10-01 audit): "resting" = the rule above (a GTC buy at pair_cost - fill, which may never
    #: fill: 23 % of the flow era's first legs stayed naked and lost 1.03 USDC each). "executable" = buy only
    #: when the pair can be completed NOW: the side's ask + the other side's ask + both taker fees +
    #: ``hedge_slippage`` must be <= ``max_pair_cost``; after the first leg fills, the other side is bought
    #: at once with a FOK at its ask + ``hedge_slippage`` (killed -> the position is NAKED, logged, never
    #: re-placed as a resting order). Nothing assumes a resting hedge fills.
    hedge_mode: str = "resting"
    max_pair_cost: float = 0.96
    hedge_slippage: float = 0.03
    #: "ev" (2026-10-01 entry-model brief): the second leg is bought (FOK at the other ask + hedge_slippage)
    #: only when the CERTAIN pair beats the model's expectation of the naked leg:
    #:     1 - other_ask - fee(other_ask) >= P(win) + hedge_ev_margin
    #: otherwise the first leg is held to settlement unhedged (counted in pairs_skipped["ev"]).
    hedge_ev_margin: float = 0.02
    # --- ENTRY MODEL (2026-10-01 brief, scripts/triangle_audit/entry_model.py) ---
    #: score every candidate with the locked logistic model (features: crypto momentum / acceleration /
    #: order flow, token velocity, depth, spread, time left, price, fair-value gap, UP+DOWN pressure) and
    #: buy only when EV = P(win) - expected fill - fee >= entry_min_ev. Off = today's rule (every triangle).
    entry_model: bool = False
    entry_model_path: str = "data/replay/triangle_entry_locked.json"
    #: the thresholds chosen on VALIDATION (results/triangle_entry_model.md): explicit here, so a run's
    #: parameters are in its config, not hidden in the lock file (which supplies the coefficients only)
    entry_min_ev: float = 0.02
    entry_max_price: float = 0.96
    entry_min_left_s: float = 60.0
    # --- exits: none ---
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0
    # --- PAIRED-EXIT MOMENTUM (2026-10-01 redesign, strategy/paired_exit*.py, docs/paired_exit_2026-10-01.md) ---
    #: on: the triangle trigger, entry model and resting pair are REPLACED by the paired-exit strategy: a fair-value
    #: EV gate after costs, an exposure book, every leg's own exits, the opposite token only as an option
    px_enabled: bool = False
    px_model_dir: str = "data/replay/paired_exit/live"
    px_chart_dir: str = "data/charts"             # the previous window's archived points seed sigma after a start
    px_momentum_threshold: float = 2.0          # |1 s spot z| (previous window's sigma) that makes a signal
    px_signal_source: str = "crypto"            # crypto (spot z) | book (Polymarket book model) | both | either
    px_book_threshold: float = 1.5              # |book model| (predicted next-1 s UP mid move, cents) that signals
    px_book_confirm: float = 0.0                # "both": the book toward the side >= this ...
    px_book_confirm_ms: float = 1000.0          # ... within this many ms before the crypto signal
    px_min_agree: float = -1.0                  # cross-exchange agreement toward the side; -1 = off
    px_entry_min_edge: float = 0.02             # ENTRY_MIN_EDGE: EV after every cost must reach this
    px_entry_min_confidence: float = 0.0        # ENTRY_MIN_CONFIDENCE: fair P(side) floor
    px_cost_slippage: float = 0.003             # expected fill - decision ask (TRAIN)
    px_cost_adverse: float = 0.033              # adverse selection of filled orders (TRAIN, BTC)
    px_cost_latency: float = 0.0                # extra cost per share for the order's travel time
    px_min_price: float = 0.15
    px_max_price: float = 0.85
    px_limit_offset: float = 0.02
    px_shares: float = 5.0
    px_min_left_s: float = 60.0
    px_max_left_s: float = 290.0
    px_late_left_s: float = 180.0
    px_late_edge_add: float = 0.0
    px_max_window_usdc: float = 10.0            # MAX_USDC_PER_WINDOW
    px_max_position_usdc: float = 5.0           # MAX_POSITION_USDC
    px_max_unpaired_usdc: float = 10.0          # MAX_UNPAIRED_USDC
    px_max_directional_usdc: float = 10.0       # TOTAL_DIRECTIONAL_EXPOSURE cap
    px_max_open_positions: int = 1              # MAX_OPEN_POSITIONS (legs not yet exited)
    px_max_buys_per_window: int = 3             # MAX_BUYS_PER_WINDOW (orders sent)
    px_stop_loss: float = 0.0                   # STOP_LOSS: bid <= fill - this; 0 = off
    px_stop_cross: float = 0.02                 # an urgent sell's limit: bid - this
    px_max_hold_seconds: float = 0.0            # MAX_HOLD_SECONDS (UNPAIRED_TIMEOUT); 0 = off
    px_thesis_mode: str = ""                    # THESIS_INVALIDATED: "fair" | "mom" | "both" | "" (off)
    px_thesis_drop: float = 0.10                # fair value fell this far under the fill
    px_reversal_threshold: float = 2.0          # REVERSAL_THRESHOLD: |z1| against the leg, exchanges agreeing
    px_time_exit_seconds: float = 0.0           # TIME_EXIT_SECONDS: sell at this many seconds left; 0 = off
    px_settle_keep: float = 0.95                # ... unless the bid is at least this
    px_ladder_levels: str = ""                  # LADDER_LEVELS: offsets above the fill, e.g. "0.05,0.10"
    px_ladder_size_percentages: str = ""        # LADDER_SIZE_PERCENTAGES, e.g. "50,50"
    px_ladder_mode: str = "maker"               # resting asks ("maker") or taker sells at the bid ("taker")
    px_ladder_fair_offset: float = -1.0         # >= 0: a level is never under fair + this (re-priced)
    px_ladder_reprice_s: float = 1.0
    px_pair_enabled: bool = False               # the OPTIONAL opposite-token order
    px_pair_target: float = 0.95                # PAIR_ORDER_DISTANCE: the pair's total stays under this
    px_max_daily_loss: float = 20.0             # MAX_DAILY_LOSS (this strategy's own kill)
    px_max_consecutive_losses: int = 6          # MAX_CONSECUTIVE_LOSSES -> pause
    px_loss_pause_s: float = 900.0
    px_max_data_age_ms: float = 1500.0          # MAX_DATA_AGE_MS (spot and book)
    px_max_signal_age_ms: float = 600.0         # MAX_SIGNAL_AGE_MS
    px_max_execution_latency_ms: float = 1500.0  # MAX_EXECUTION_LATENCY_MS (measured order round trip)
    px_max_spread: float = 0.05                 # MAX_SPREAD
    px_max_disagreement: float = 3.0            # cross-exchange dispersion of 1 s returns (sigma units)
    px_max_settle_mismatches: int = 3
    # --- budget: wide on purpose (the rule is "every triangle"); paper only ---
    max_simultaneous_positions: int = 50
    max_buys_per_hour: int = 100_000
    max_entries_per_market: int = 100_000
    max_attempts_per_market: int = 100_000
    max_order_usdc: float = 1.0
    #: USDC one market may hold (all its triangles together)
    max_market_usdc: float = 50.0
    max_total_exposure_usdc: float = 400.0
    max_daily_loss_usdc: float = 250.0

    def flow_params(self) -> FlowParams:
        def nums(text: str, default: tuple[float, ...]) -> tuple[float, ...]:
            try:
                out = tuple(float(x) for x in str(text).split(",") if x.strip())
            except ValueError:
                out = ()
            return out or default
        ex = tuple(x.strip().lower() for x in str(self.flow_exchanges).split(",") if x.strip())
        return FlowParams(
            exchanges=ex or ("coinbase", "bybit", "binance"),
            windows_ms=nums(self.flow_windows_ms, (20.0, 50.0, 100.0)), primary_ms=self.flow_primary_ms,
            ref_bp=self.flow_ref_bp, min_bp=self.flow_min_bp, min_confirm=int(self.flow_min_confirm),
            min_fresh=int(self.flow_min_fresh), gate_bp=self.flow_gate_bp,
            trade_window_ms=self.flow_trade_window_ms, trade_full_usd=self.flow_trade_full_usd,
            accel_ref_bp=self.flow_accel_ref_bp, first_lookback_ms=self.flow_first_lookback_ms,
            first_cross_bp=self.flow_first_cross_bp, w_cx=self.flow_w_cx, w_flow=self.flow_w_flow,
            w_acc=self.flow_w_acc, w_first=self.flow_w_first, threshold=self.flow_threshold,
            dedupe_ms=self.flow_dedupe_ms, valid_ms=self.flow_valid_ms)

    def momentum_mirror(self) -> MomentumConfig:
        # explicit base call: a slots dataclass is rebuilt, which breaks zero-argument super()
        m = SpotLagConfig.momentum_mirror(self)
        m.max_hold_s = 1e9                     # nothing here sells on a timer
        return m

    def apply_budget(self, risk_cfg, order_ceiling: float | None = None) -> list[str]:
        """SET (not only tighten) every cap the rule touches to this config's values: the global
        defaults (10 positions, 50 USDC exposure, scaled to the bankroll) would stop it after a
        few windows. Paper only -- __main__ refuses real money for this strategy."""
        changed = SpotLagConfig.apply_budget(self, risk_cfg, order_ceiling)
        if self.px_enabled and risk_cfg.max_position_usdc < self.px_max_position_usdc and order_ceiling is None:
            # paired-exit: one leg may cost up to MAX_POSITION_USDC (the exposure book enforces the rest)
            changed.append(f"max_position_usdc: {risk_cfg.max_position_usdc} -> {self.px_max_position_usdc}")
            risk_cfg.max_position_usdc = self.px_max_position_usdc
        n_assets = 7
        for attr, new in (("max_concurrent_positions", int(self.max_simultaneous_positions)),
                          ("max_asset_exposure_usdc", 2.0 * self.max_market_usdc),
                          ("max_epoch_exposure_usdc", self.max_market_usdc * n_assets),
                          ("max_total_exposure_usdc", self.max_total_exposure_usdc),
                          ("max_daily_loss_usdc", self.max_daily_loss_usdc),
                          ("max_drawdown_usdc", 2.0 * self.max_daily_loss_usdc)):
            old = getattr(risk_cfg, attr)
            if old != new:
                setattr(risk_cfg, attr, new)
                changed.append(f"{attr}: {old} -> {new}")
        return changed


class TriangleEngine(PairedExitMixin, SpotLagEngine):
    """The spot-lag engine's signal; one share per filled triangle; no exits."""

    feature_key = STRATEGY_ID
    #: the dashboard's shared control file may not lift (or steer) this strategy's config
    budget_is_ceiling = True
    #: several orders per market, both sides (live._confirm_entry, RiskManager.check add_to_open)
    stacks_positions = True
    #: no per-market retry pause: a new order needs a new triangle (at most one per direction
    #: per ``dedupe_s``), so nothing can be re-sent, and a pause would skip evaluations -- and
    #: with them triangles
    entry_retry_ms = 0.0
    #: a 1-share FOK that misses is the rule working, not the venue failing: kept out of the
    #: guard's execution-failure rate, which would otherwise pause entries
    expected_kills: frozenset[str] = frozenset({"fok_unfillable", "quote_pulled", "no_liquidity"})

    def __init__(self, cfg: EngineConfig, scfg: TriangleConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the triangle strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, TriangleConfig):
            raise TypeError("TriangleEngine needs a TriangleConfig")
        super().__init__(cfg, scfg, risk, features)
        #: counters for the dashboard: triangles seen, orders decided, triangles refused (by reason)
        self.triangles = 0
        self.orders_decided = 0
        self.refused: dict[str, int] = {}
        self.last_triangle: dict[str, dict] = {}
        #: pair orders placed, and skipped by reason ("price" under a tick, "size" under the venue minimum)
        self.pairs_placed = 0
        self.pairs_skipped: dict[str, int] = {}
        #: pair orders working at the venue: order id -> (slug, side, price, shares)
        self.pairs_working: dict[str, tuple[str, str, float, float]] = {}
        self.pairs_filled = 0
        self.pairs_expired = 0
        self.pairs_naked = 0                  # executable hedges killed: first legs left without a hedge
        #: the flow signal (None unless signal_source == "flow"); the live bot feeds it every tick and trade
        self.flow: FlowSignal | None = FlowSignal(scfg.flow_params()) if scfg.signal_source == "flow" else None
        #: entry order id -> (slug, side, the triangle's mark time), to redraw it if the order does not fill
        self._order_marks: dict[str, tuple[str, str, float]] = {}
        #: the high-zone rule's state: each token's ask history, and when each market last bought
        self._ask_hist: dict[tuple[str, str], deque] = {}
        self._hz_last: dict[str, float] = {}
        self.hz_triggers = 0
        #: the entry model (None when off or the lock file is missing: then every triangle trades, logged once)
        self.model: dict | None = None
        self.model_feats: tuple[str, ...] = ()
        if scfg.entry_model:
            self._load_entry_model(scfg.entry_model_path)
        #: P(win) the model gave the last decision per (slug, side): the "ev" hedge mode reads it after the fill
        self._pwin: dict[tuple[str, str], float] = {}
        self.entry_scored = 0
        self.entry_refused_ev = 0
        self._px_init(scfg)
        # the active configuration, printed once at start so a run's parameters are in its log
        active = {k: v for k, v in scfg.as_dict().items() if not str(k).startswith("flow_") or k in ("flow_threshold", "flow_ref_bp")}
        log.info("TRIANGLE_CONFIG %s", " ".join(f"{k}={v}" for k, v in sorted(active.items())))

    # the entry thresholds are read from the config at every use: the dashboard's "Entry min EV" control changes
    # scfg, and a copy taken at start never saw it (2026-10-01: the control logged "0.02 -> 0.5", buys went on)
    @property
    def entry_min_ev(self) -> float:
        return self.scfg.entry_min_ev

    @property
    def entry_max_price(self) -> float:
        return self.scfg.entry_max_price

    @property
    def entry_min_left_s(self) -> float:
        return self.scfg.entry_min_left_s

    def _load_entry_model(self, path: str) -> None:
        import json
        try:
            with open(path, encoding="utf-8") as fh:
                lock = json.load(fh)
            self.model = lock["model"]
            self.model_feats = tuple(lock["features"])
            locked = (lock.get("ev_min"), lock.get("max_price"), lock.get("min_left_s"))
            log.info("ENTRY_MODEL loaded %s: %d features; active min EV %+.3f, max price %.2f, min left %.0f s "
                     "(the study locked %s)", path, len(self.model_feats), self.entry_min_ev, self.entry_max_price,
                     self.entry_min_left_s, locked)
        except (OSError, KeyError, ValueError, TypeError) as exc:
            self.model = None
            log.error("ENTRY_MODEL not loaded from %s (%s): every triangle trades, as without the model", path, exc)

    def entry_features(self, meta, side: str, book, now: float, base: Evaluation, orderflow, hz: bool) -> dict | None:
        """The entry model's features, signed by the side (+ = evidence for the side), as the study computes
        them from the same quantities (scripts/triangle_audit/entry_model.py)."""
        m = meta.market
        d = 1 if side == "UP" else -1
        hist = self.features.spot.get(m.asset)
        sigma = self.last_sigma.get(m.asset)
        if hist is None or not sigma or sigma <= 0.0:
            return None
        r1, r3 = hist.logret(now, 1.0), hist.logret(now, 3.0)
        if r1 is None or r3 is None:
            return None
        r1, r3 = r1 * 1e4, r3 * 1e4
        sig_bp = sigma * 1e4
        of = orderflow or {}
        ask, bid = book.best_ask, book.best_bid
        dq = self._ask_hist.get((m.slug, side))
        a1, a3 = self._ask_at(dq, now - 1000.0), self._ask_at(dq, now - 3000.0)
        depth = sum(lvl.size for lvl in book.asks if lvl.price <= ask + 0.02 + 1e-9)
        left = m.seconds_remaining(now)
        p_up = base.p_analytic if base.p_analytic is not None else base.p_market
        fv = None if p_up is None else (p_up if side == "UP" else 1.0 - p_up)
        ua = self._ask_at(self._ask_hist.get((m.slug, "UP")), now)
        da = self._ask_at(self._ask_hist.get((m.slug, "DOWN")), now)
        return {"z1": r1 * d / sig_bp, "z3": r3 * d / (sig_bp * math.sqrt(3.0)), "accel": (r1 - r3 / 3.0) * d / sig_bp,
                "tfi15": (of.get("tfi_15s") or 0.0) * d, "obi": (of.get("obi") or 0.0) * d, "ofi": (of.get("ofi_score") or 0.0) * d,
                "tok_vel1": 0.0 if a1 is None else (ask - a1) * 100.0, "tok_vel3": 0.0 if a3 is None else (ask - a3) * 100.0,
                "depth_ask": math.log1p(depth), "spread": 0.0 if bid is None else (ask - bid) * 100.0,
                "left_frac": left / 300.0, "ask": ask, "fv_gap": 0.0 if fv is None else fv - ask,
                "pressure": 0.0 if ua is None or da is None else (ua + da - 1.0) * 100.0, "hz": 1.0 if hz else 0.0}

    def entry_score(self, feats: dict) -> tuple[float, float]:
        """(P(win), EV per share) from the locked logistic model; EV uses the paper run's measured markup
        (ask + 1c) as the expected fill and the taker fee on it."""
        mdl = self.model
        z = mdl["w"][0]
        for k, mu, sd, w in zip(self.model_feats, mdl["mu"], mdl["sd"], mdl["w"][1:]):
            x = feats.get(k)
            if x is None or x != x:
                x = mu
            z += w * (x - mu) / sd
        z = max(-30.0, min(30.0, z))
        p = 1.0 / (1.0 + math.exp(-z))
        ask = feats["ask"]
        exp_fill = min(ask + 0.01, self.scfg.max_price)
        return p, p - exp_fill - 0.07 * exp_fill * (1.0 - exp_fill)

    def _filled_mark(self, slug: str, d: int, now: float) -> dict | None:
        """This second's mark in direction ``d``, made FILLED: the spot-lag engine leaves it hollow
        when the same second first failed a gate and then passed (one mark per second)."""
        for mk in reversed(self.shock_marks.get(slug, ())):
            if now - mk["t"] >= 1_000.0:
                break
            if mk["dir"] == d:
                if not mk.get("ok"):
                    mk["ok"], mk["why"] = True, ""
                return mk
        return None

    def _track_asks(self, slug: str, now: float, book_up, book_down) -> None:
        keep = (self.scfg.hz_lookback_s + 3.0) * 1000.0
        for side, book in (("UP", book_up), ("DOWN", book_down)):
            ask = None if book is None else book.best_ask
            if ask is None:
                continue
            dq = self._ask_hist.setdefault((slug, side), deque())
            if not dq or ask != dq[-1][1] or now - dq[-1][0] >= 500.0:
                dq.append((now, ask))                     # (time, ask); the model reads bid / depth off the book itself
            while len(dq) > 2 and dq[1][0] <= now - keep:
                dq.popleft()
        if len(self._ask_hist) > 200:                     # markets long closed
            for k in [k for k, v in self._ask_hist.items() if v and now - v[-1][0] > 600_000.0]:
                self._ask_hist.pop(k, None)
                self._hz_last.pop(k[0], None)

    @staticmethod
    def _ask_at(dq: deque | None, t: float) -> float | None:
        if not dq or dq[0][0] > t:
            return None                                   # not observed that far back
        for i in range(len(dq) - 1, -1, -1):
            if dq[i][0] <= t:
                return dq[i][1]
        return None

    def _high_zone(self, meta, book_up, book_down, now: float) -> dict | None:
        """A token above ``hz_min_price`` whose ask is still rising: due to buy every ``hz_every_s``."""
        sc, slug = self.scfg, meta.market.slug
        if not sc.hz_enabled:
            return None
        last = self._hz_last.get(slug)
        if last is not None and now - last < sc.hz_every_s * 1000.0:
            return None
        for side, book in (("UP", book_up), ("DOWN", book_down)):
            ask = None if book is None else book.best_ask
            if ask is None or ask <= sc.hz_min_price + 1e-9:
                continue
            hist = self._ask_hist.get((slug, side))
            then = self._ask_at(hist, now - sc.hz_lookback_s * 1000.0)
            if then is None:
                continue
            one = self._ask_at(hist, now - 1000.0)
            if ask > then + 1e-9 and (one is None or ask >= one - 1e-9):
                self._hz_last[slug] = now
                self.hz_triggers += 1
                return {"side": side, "ask": ask, "ask_then": then, "rise": round(ask - then, 4),
                        "lookback_s": sc.hz_lookback_s}
        return None

    def _hz_mark(self, slug: str, now: float, d: int, hz: dict) -> dict:
        dq = self.shock_marks.setdefault(slug, deque(maxlen=self.SHOCK_MARKS_PER_MARKET))
        mk = {"t": round(now), "dir": d, "z1": None, "z3": None, "r1_bps": None, "trigger": "high_zone",
              "ok": True, "why": "", "hz": dict(hz)}
        dq.append(mk)
        return mk

    def _note_shock(self, slug: str, now: float, d: int, info: dict) -> dict | None:
        if self.flow is not None or self.px is not None:
            return None                         # flow / paired-exit mode: spot-lag's detections are not drawn or traded
        return SpotLagEngine._note_shock(self, slug, now, d, info)

    def _flow_mark(self, slug: str, now: float, res: FlowResult) -> dict:
        """The triangle of a flow signal: one per market and direction per second, filled (it is an order)."""
        dq = self.shock_marks.setdefault(slug, deque(maxlen=self.SHOCK_MARKS_PER_MARKET))
        for mk in reversed(dq):
            if mk["dir"] == res.dir and now - mk["t"] < 1_000.0:
                return mk
            if now - mk["t"] >= 1_000.0:
                break
        mv = list(res.moves_bp.values())
        mk = {"t": round(now), "dir": res.dir, "z1": None, "z3": None,
              "r1_bps": round(sum(mv) / len(mv), 2) if mv else None, "trigger": "flow", "ok": True, "why": "",
              "flow": res.as_dict()}
        dq.append(mk)
        return mk

    def _refuse(self, ev: Evaluation, reason: Reason, detail: str, mark: dict | None = None) -> Evaluation:
        self.refused[reason.value] = self.refused.get(reason.value, 0) + 1
        self._hollow(mark, f"not bought: {detail}")
        return self._reject(ev, reason, detail)

    @staticmethod
    def _hollow(mark: dict | None, why: str) -> None:
        """A triangle that did not end in a buy is drawn hollow with the reason: filled = bought."""
        if mark is not None:
            mark["ok"], mark["why"] = False, why

    def _mark_of(self, slug: str, side: str, t) -> dict | None:
        d = 1 if side == "UP" else -1
        for mk in reversed(self.shock_marks.get(slug, ())):
            if mk["dir"] == d and t is not None and abs(mk["t"] - t) < 1.0:
                return mk
        return None

    def entry_refused(self, ev: Evaluation, why: str) -> None:
        """The live bot did not send this triangle's order (guard, kill switch, the last pre-send check)."""
        f = (ev.features or {}).get(STRATEGY_ID) or {}
        self.refused["NOT_SENT"] = self.refused.get("NOT_SENT", 0) + 1
        self._hollow(self._mark_of(ev.slug, f.get("side", ""), f.get("t")), f"not bought: {why}")

    def entry_result(self, order_id: str, filled: bool, why: str = "") -> None:
        """A triangle's order came back: unfilled, its triangle is drawn hollow with the reason."""
        key = self._order_marks.pop(order_id, None)
        if key is not None and not filled:
            self.refused["NOT_FILLED"] = self.refused.get("NOT_FILLED", 0) + 1
            self._hollow(self._mark_of(*key), f"order not filled: {why}")

    # --------------------------------------------------------------- entry

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        self._track_asks(meta.market.slug, now, book_up, book_down)
        base = SpotLagEngine.evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol,
                                      twap, latency, balance, orderflow, quote_ages)
        fres: FlowResult | None = None
        hz: dict | None = None
        if self.px is not None:
            # paired-exit mode: a refusal BEFORE spot-lag's signal stage (stale spot, no strike, outside the time
            # window) stands; otherwise the paired-exit entry decides
            if "spot_lag" not in (base.features or {}):
                return base
            return self.px_evaluate(meta, book_up, book_down, base, now, spot_age_ms, book_age_ms, latency, balance,
                                    quote_ages)
        if self.flow is not None:
            # flow mode: the trigger is a cross-exchange flow signal; spot-lag's own shock is neither traded
            # nor drawn. A refusal BEFORE the signal stage (stale spot, no strike, outside the time window)
            # has no spot-lag block in its features and stands.
            if "spot_lag" not in (base.features or {}):
                return base
            fres = self.flow.take(meta.market.asset, meta.market.slug, now)
            if fres is not None and base.seconds_left < self.scfg.flow_min_left_s:
                fres = None                     # too late in the window for a flow signal (not drawn, not bought)
            if fres is None:
                hz = self._high_zone(meta, book_up, book_down, now)
                if hz is None:
                    # spot-lag's own verdict on its own shock is not this strategy's business: say what is true
                    return self._reject(base, Reason.WEAK_MOMENTUM, "no flow signal")
            sl = {"trigger": "flow" if fres is not None else "high_zone", "signal": True}
        else:
            sl = (base.features or {}).get("spot_lag") or {}
            if not sl.get("signal"):
                hz = self._high_zone(meta, book_up, book_down, now) if "spot_lag" in (base.features or {}) else None
                if hz is None:
                    return base                 # no filled triangle: spot-lag's own reason stands
                sl = {"trigger": "high_zone", "signal": True}
        sc, m = self.scfg, meta.market
        if fres is not None:
            d = fres.dir
            mark = self._flow_mark(m.slug, now, fres)
        elif hz is not None:
            d = 1 if hz["side"] == "UP" else -1
            mark = self._hz_mark(m.slug, now, d, hz)
        else:
            d = 1 if (base.features.get("momentum") or {}).get("label") == "SHOCK_UP" else -1
            mark = self._filled_mark(m.slug, d, now)
        side = "UP" if d > 0 else "DOWN"
        if sc.wait_for_pair and self.pairs_working:
            slug_p, side_p, px_p, sh_p = next(iter(self.pairs_working.values()))
            why = (f"waiting for the pair: {side_p} {sh_p:g} sh at {px_p:.2f} in {slug_p} not filled"
                   + (f" (+{len(self.pairs_working) - 1} more)" if len(self.pairs_working) > 1 else ""))
            if mark is not None:
                mark["ok"], mark["why"] = False, why        # drawn hollow, with the reason
            self.refused["PAIR_WORKING"] = self.refused.get("PAIR_WORKING", 0) + 1
            # a triangle the rule saw and skipped: the live bot logs it as [SKIP] with the reason
            base.features = {**base.features, STRATEGY_ID: {"signal": True, "direction": side, "side": side,
                                                            "waiting_for_pair": True}}
            return self._reject(base, Reason.ALREADY_POSITIONED, why)
        self.triangles += 1
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=base.seconds_left, reason=None)
        ev.p_analytic, ev.p_market = base.p_analytic, base.p_market
        ev.spot_move_bps, ev.market_reprice = base.spot_move_bps, base.market_reprice
        info = {"signal": True, "direction": side, "side": side, "t": mark["t"] if mark else round(now),
                "z1": sl.get("z1"), "z3": sl.get("z3"), "r1_bps": sl.get("r1_bps"), "trend_bps": sl.get("trend_bps"),
                "trigger": sl.get("trigger", "sigma"),
                # what spot-lag itself would have done with this signal (it is not applied here)
                "spot_lag_verdict": base.reason.value if base.reason is not None else "ORDER",
                "spot_lag_detail": base.detail}
        if fres is not None:
            info.update({"flow": fres.as_dict(), "score": round(fres.score, 3), "first_mover": fres.first_mover,
                         "flow_age_ms": round(now - fres.t_local)})
        if hz is not None:
            info.update({"hz": dict(hz), "trigger": "high_zone"})
        ev.features = {**base.features, STRATEGY_ID: info}
        self.last_triangle[m.asset] = {"slug": m.slug, **info}
        token = m.yes_token_id if side == "UP" else m.no_token_id
        book = book_up if side == "UP" else book_down
        tick = m.tick_size or 0.01
        if book is None or book.best_ask is None:
            bid_now = None if book is None else book.best_bid
            return self._refuse(ev, Reason.NO_OFFER, f"no {side} ask" + (f" (bid {bid_now:.2f}: nothing offered below "
                                                                       f"{1.0:.2f})" if bid_now else ""), mark)
        ask, bid = book.best_ask, book.best_bid
        if fres is not None and sc.flow_max_token_move >= 0.0:
            then = self._ask_at(self._ask_hist.get((m.slug, side)), now - sc.flow_token_lookback_ms)
            if then is not None and ask - then > sc.flow_max_token_move + 1e-9:
                return self._refuse(ev, Reason.TOKEN_ALREADY_MOVED,
                                    f"{side} token already repriced +{(ask - then) * 100:.0f}c in the last "
                                    f"{sc.flow_token_lookback_ms / 1000:g} s ({then:.2f} -> {ask:.2f}): not chasing", mark)
        if ask < sc.min_price - 1e-9 or ask > sc.max_price + 1e-9:
            return self._refuse(ev, Reason.PRICE_BAND,
                                f"{side} ask {ask:.2f} is outside the {sc.min_price:.2f}-{sc.max_price:.2f} band", mark)
        limit = min(1.0 - tick, sc.max_price, round(ask + max(sc.limit_offset, 0.0), 4))
        if sc.pair_cost > 0.0 and sc.hedge_mode == "executable":
            other_book = book_down if side == "UP" else book_up
            o0 = None if other_book is None else other_book.best_ask
            if o0 is None:
                return self._refuse(ev, Reason.NO_OFFER, f"no {('DOWN' if side == 'UP' else 'UP')} ask: the pair cannot "
                                                         f"be priced", mark)
            est = ask + o0 + meta.fee.taker_fee_per_share(ask) + meta.fee.taker_fee_per_share(o0) + sc.hedge_slippage
            if est > sc.max_pair_cost + 1e-9:
                return self._refuse(ev, Reason.PRICE_BAND,
                                    f"executable pair cost {est:.3f} ({side} {ask:.2f} + other {o0:.2f} + fees + "
                                    f"{sc.hedge_slippage:.2f} slippage) > max {sc.max_pair_cost:.2f}", mark)
            info_pair = {"pair_est": round(est, 4), "other_ask": o0}
        else:
            info_pair = {}
        if sc.pair_cost > 0.0 and sc.pair_room_required and sc.hedge_mode not in ("executable", "ev"):
            # the highest fill that still leaves a 1-tick pair (strict: the sum must stay under pair_cost)
            room = round(sc.pair_cost - (2 if sc.pair_cost_strict else 1) * tick, 4)
            if ask > room + 1e-9:
                return self._refuse(ev, Reason.PRICE_BAND,
                                    f"{side} ask {ask:.2f} leaves no room for a pair (sum "
                                    f"{'under' if sc.pair_cost_strict else 'at most'} {sc.pair_cost:.2f}); "
                                    f"buys stop at {room:.2f}", mark)
            limit = min(limit, room)
        if ask > limit + 1e-9:
            return self._refuse(ev, Reason.PRICE_BAND, f"{side} ask {ask:.2f} above the venue's {1.0 - tick:.2f}", mark)
        p_win = ev_share = None
        if self.model is not None:
            feats = self.entry_features(meta, side, book, now, base, orderflow, hz is not None)
            if feats is None:
                return self._refuse(ev, Reason.MODEL_NOT_READY, "entry model: spot history / sigma not ready", mark)
            p_win, ev_share = self.entry_score(feats)
            self.entry_scored += 1
            info.update({"p_win": round(p_win, 4), "entry_ev": round(ev_share, 4),
                         "entry_feats": {k: round(v, 4) for k, v in feats.items()}})
            log.info("ENTRY_SCORE market=%s side=%s ask=%.2f p_win=%.3f ev=%+.4f min_ev=%+.3f left=%.0fs z1=%+.2f tok_vel1=%+.1fc "
                     "ofi=%+.2f fv_gap=%+.3f trigger=%s", m.slug, side, ask, p_win, ev_share, self.entry_min_ev, base.seconds_left,
                     feats["z1"], feats["tok_vel1"], feats["ofi"], feats["fv_gap"], info.get("trigger"))
            if base.seconds_left < self.entry_min_left_s:
                self.entry_refused_ev += 1
                return self._refuse(ev, Reason.OUTSIDE_TIME_WINDOW,
                                    f"entry model: {base.seconds_left:.0f} s left < {self.entry_min_left_s:.0f}", mark)
            if ask > self.entry_max_price + 1e-9:
                self.entry_refused_ev += 1
                return self._refuse(ev, Reason.PRICE_BAND, f"entry model: ask {ask:.2f} > {self.entry_max_price:.2f}", mark)
            if ev_share < self.entry_min_ev:
                self.entry_refused_ev += 1
                return self._refuse(ev, Reason.EDGE_TOO_SMALL,
                                    f"entry EV {ev_share:+.3f}/share < {self.entry_min_ev:+.3f} (P(win) {p_win:.2f} at {ask:.2f})", mark)
            self._pwin[(m.slug, side)] = p_win
        size = float(sc.shares)
        held = self.risk.open.get(m.slug)
        committed = held.usdc if held is not None else 0.0
        if committed + size * limit > sc.max_market_usdc + 1e-9:
            return self._refuse(ev, Reason.EXPOSURE_CAP,
                                f"{committed:.2f} of {sc.max_market_usdc:.2f} USDC already in this market", mark)
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit, add_to_open=True)
        if not verdict.ok:
            return self._refuse(ev, Reason(verdict.reason), verdict.detail, mark)
        if size * limit > balance + 1e-9:
            return self._refuse(ev, Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * limit:.2f}", mark)
        p_up = base.p_analytic if base.p_analytic is not None else base.p_market
        p_side = None if p_up is None else (p_up if side == "UP" else 1.0 - p_up)
        fee = meta.fee.taker_fee_per_share(ask)
        edge = (p_side - ask - fee) if p_side is not None else 0.0
        info.update({"ask": ask, "bid": bid, "limit": limit, "size": size, "fee": round(fee, 4),
                     "model_edge": round(edge, 4), "market_usdc": round(committed, 4), **info_pair})
        ev.features["execution"] = {"fair": None if p_side is None else round(p_side, 4), "cap": round(limit, 4),
                                    "best_ask": ask, "best_bid": bid,
                                    "spread": None if bid is None else round(ask - bid, 4),
                                    "quote_age_ms": None if not quote_ages or quote_ages.get(token) is None
                                    else round(quote_ages[token]),
                                    "spot_age_ms": None if spot_age_ms is None else round(spot_age_ms)}
        depth = sum(lvl.size for lvl in book.asks if lvl.price <= limit + 1e-9)
        ev.sides = [SideView(side=side, token_id=token, p_model=p_side if p_side is not None else ask, p_market=ask,
                             ask=ask, bid=bid, depth_at_limit=depth, costs=None, reason=None, entry_px=ask)]
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, limit, ask
        ev.max_entry_price = limit
        ev.p_used = p_up
        ev.size, ev.net_edge, ev.confidence = size, edge, 1.0
        ev.size_note = f"{size:g} share per triangle ({size * limit:.2f} USDC worst case; {committed:.2f} already here)"
        self.orders_decided += 1
        self.last[m.slug] = ev
        return ev

    def pair_order(self, market, side: str, shares: float, avg_px: float, now_ms: float,
                   other_ask: float | None = None) -> tuple[str, Order] | None:
        """The hedge of the OTHER side after a ``side`` buy of ``shares`` at ``avg_px``: (its side, the
        order), or None when off or not placeable (counted in ``pairs_skipped``). Resting mode: a GTC buy at
        pair_cost - fill. Executable mode: a FOK at the other side's ask now + hedge_slippage."""
        if self.px is not None:
            return self.px_pair_order(market, side, shares, avg_px)
        sc = self.scfg
        if sc.pair_cost <= 0.0 or shares <= 0.0:
            return None
        tick = market.tick_size or 0.01
        other = "DOWN" if side == "UP" else "UP"
        token = market.no_token_id if side == "UP" else market.yes_token_id
        if sc.hedge_mode == "ev":
            # the certain pair (1 - other ask - its fee) must beat holding the leg naked (P(win)) by the margin
            p_win = self._pwin.get((market.slug, side))
            if other_ask is None or p_win is None:
                self.pairs_skipped["no_ask" if other_ask is None else "no_pwin"] = \
                    self.pairs_skipped.get("no_ask" if other_ask is None else "no_pwin", 0) + 1
                self.pairs_naked += 1
                return None
            fee_o = 0.07 * other_ask * (1.0 - other_ask)
            certain = 1.0 - other_ask - fee_o
            if certain < p_win + sc.hedge_ev_margin or (market.min_size and shares + 1e-9 < float(market.min_size)):
                self.pairs_skipped["ev"] = self.pairs_skipped.get("ev", 0) + 1
                self.pairs_naked += 1
                log.info("[PAIR] %s %s: NO hedge by EV: certain pair %.3f (other ask %.2f) < P(win) %.2f + margin %.2f: "
                         "holding %.2f sh @ %.3f to settlement", market.slug, side, certain, other_ask, p_win,
                         sc.hedge_ev_margin, shares, avg_px)
                return None
            px = min(1.0 - tick, round(other_ask + sc.hedge_slippage, 4))
            self.pairs_placed += 1
            log.info("[PAIR] %s %s: HEDGE by EV: certain pair %.3f (other ask %.2f) >= P(win) %.2f + %.2f", market.slug,
                     side, certain, other_ask, p_win, sc.hedge_ev_margin)
            return other, Order(token_id=token, side=Side.BUY, price=px, size=round(shares, 2), tif=TimeInForce.FOK,
                                expected_price=other_ask,
                                tag=f"{other} triangle hedge by EV: FOK ask {other_ask:.2f}+{sc.hedge_slippage:.2f} "
                                    f"(pair {avg_px + other_ask:.3f}, P(win) {p_win:.2f})")
        if sc.hedge_mode == "executable":
            if market.min_size and shares + 1e-9 < float(market.min_size):
                self.pairs_skipped["size"] = self.pairs_skipped.get("size", 0) + 1
                return None
            if other_ask is None:
                self.pairs_skipped["no_ask"] = self.pairs_skipped.get("no_ask", 0) + 1
                log.warning("[PAIR] %s %s: no %s ask to hedge against: the %.2f sh bought at %.3f are NAKED",
                            market.slug, side, other, shares, avg_px)
                self.pairs_naked += 1
                return None
            px = min(1.0 - tick, round(other_ask + sc.hedge_slippage, 4))
            self.pairs_placed += 1
            return other, Order(token_id=token, side=Side.BUY, price=px, size=round(shares, 2), tif=TimeInForce.FOK,
                                expected_price=other_ask,
                                tag=f"{other} triangle hedge FOK ask {other_ask:.2f}+{sc.hedge_slippage:.2f} (pair "
                                    f"{avg_px + other_ask:.3f}, max {sc.max_pair_cost:.2f})")
        if sc.pair_cost_strict:
            # the highest tick with avg + px strictly under pair_cost
            px = round(math.floor((sc.pair_cost - avg_px - 1e-9) / tick) * tick, 4)
        else:
            px = round(math.floor((sc.pair_cost - avg_px) / tick + 1e-6) * tick, 4)
        why = ("price" if px < tick - 1e-9 else
               "size" if market.min_size and shares + 1e-9 < float(market.min_size) else None)
        if why is not None:
            self.pairs_skipped[why] = self.pairs_skipped.get(why, 0) + 1
            log.info("[PAIR] %s %s: no pair order (%s): %.2f sh bought at %.3f, %.2f - %.3f = %.2f",
                     market.slug, side, why, shares, avg_px, sc.pair_cost, avg_px, sc.pair_cost - avg_px)
            return None
        self.pairs_placed += 1
        return other, Order(token_id=token, side=Side.BUY, price=px, size=round(shares, 2), tif=TimeInForce.GTC,
                            expires_ts=market.close_ts, expected_price=px,
                            tag=f"{other} triangle pair {avg_px:.3f} + {px:.2f} = {avg_px + px:.3f} "
                                f"({'<' if sc.pair_cost_strict else '<='} {sc.pair_cost:.2f})")

    def on_pair_submit(self, order_id: str, slug: str, side: str, price: float, shares: float) -> None:
        self.pairs_working[order_id] = (slug, side, price, shares)
        if self.px is not None:
            self.px_on_pair_submit(order_id)

    def drop_pair(self, order_id: str) -> None:
        """Forget a pair without counting it (a restart's placeholder, replaced by the re-placed order)."""
        self.pairs_working.pop(order_id, None)

    def on_pair_done(self, order_id: str, filled: bool, killed: bool = False) -> None:
        """The pair order filled, or ended unfilled (expired at its market's close; ``killed``: an executable
        hedge FOK the venue could not fill -> the first leg is naked): it resolved."""
        if self.pairs_working.pop(order_id, None) is not None:
            if filled:
                self.pairs_filled += 1
            elif killed:
                self.pairs_naked += 1
            else:
                self.pairs_expired += 1

    def revalidate(self, ev: Evaluation, book, min_edge: float | None = None) -> Reason | None:
        """Every triangle is an order: only a side with no ask at all is not sent (the FOK limit decides)."""
        if book is None or book.best_ask is None:
            return Reason.NO_OFFER
        if self.px is not None:
            # paired-exit: the freshest ask must still be inside the band and under the order's limit
            if not (self.px.lo - 1e-9 <= book.best_ask <= self.px.hi + 1e-9) or book.best_ask > (ev.limit_price or 0) + 1e-9:
                return Reason.PRICE_BAND
            return None
        if book.best_ask < self.scfg.min_price - 1e-9 or book.best_ask > self.scfg.max_price + 1e-9:
            return Reason.PRICE_BAND            # the freshest ask left the band between decision and send
        return None

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        f = (ev.features or {}).get(STRATEGY_ID, {})
        o = Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                  tif=TimeInForce.IOC if self._fak else TimeInForce.FOK,
                  expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                  tag=f"{ev.side} triangle z1={f.get('z1') or 0:+.2f} ask={f.get('ask') or 0:.2f} "
                      f"cap={ev.limit_price:.2f}")
        self._order_marks[o.order_id] = (ev.slug, ev.side, f.get("t"))
        while len(self._order_marks) > 500:
            self._order_marks.pop(next(iter(self._order_marks)))
        return o

    def signal_text(self, ev: Evaluation) -> str:
        f = (ev.features or {}).get(STRATEGY_ID, {})
        arrow = "▲" if f.get("side") == "UP" else "▼"
        hzi = f.get("hz")
        if hzi:
            return (f"TRIANGLE {arrow} HIGH ZONE {hzi['side']} ask {hzi['ask']:.2f} still rising (from "
                    f"{hzi['ask_then']:.2f} {hzi['lookback_s']:g} s ago, over {self.scfg.hz_min_price:.2f}): buy "
                    f"{f.get('size'):g} at ask {f.get('ask')} (limit {f.get('limit')})")
        fl = f.get("flow")
        if fl:
            return (f"TRIANGLE {arrow} FLOW score {fl['score']:+.2f} (momentum {fl['cx']:+.2f}, trade flow "
                    f"{fl['flow']:+.2f}, accel {fl['acc']:+.2f}, first mover {fl['first_mover'] or '-'} "
                    f"{fl['first']:+.2f}; {fl['confirm']}/{fl['fresh']} exchanges confirm): buy "
                    f"{f.get('size'):g} at ask {f.get('ask')} (limit {f.get('limit')})")
        return (f"TRIANGLE {arrow} z1 {f.get('z1') or 0:+.2f} trend {f.get('trend_bps') or 0:+.1f} bps: buy "
                f"{f.get('size'):g} at ask {f.get('ask')} (limit {f.get('limit')}); spot-lag alone: "
                f"{f.get('spot_lag_verdict')}")

    # ---------------------------------------------------------------- exits

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig: MomentumSignal | None = None, spread: float | None = None,
                      bid_depth: float | None = None, size: float | None = None,
                      data_age_ms: float | None = None) -> str | None:
        """Never: every share settles at 1 / 0."""
        return None

    def persist_state(self) -> dict | None:
        return self.px_persist()

    def restore_state(self, doc: dict | None, now: float) -> None:
        self.px_restore(doc, now)

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = STRATEGY_ID
        snap["triangle_config"] = self.scfg.as_dict()
        snap["paired_exit"] = self.px_snapshot()
        snap["triangle"] = {"triangles": self.triangles, "orders_decided": self.orders_decided,
                            "refused": dict(self.refused), "last": dict(self.last_triangle),
                            "pairs_placed": self.pairs_placed, "pairs_skipped": dict(self.pairs_skipped),
                            "pairs_working": len(self.pairs_working), "pairs_filled": self.pairs_filled,
                            "pairs_expired": self.pairs_expired, "pairs_naked": self.pairs_naked,
                            "hedge_mode": self.scfg.hedge_mode, "hz_triggers": self.hz_triggers,
                            "entry_model": self.model is not None, "entry_scored": self.entry_scored,
                            "entry_refused_ev": self.entry_refused_ev,
                            "entry_min_ev": None if self.model is None else round(self.entry_min_ev, 4),
                            "flow": None if self.flow is None else {"ticks": self.flow.ticks, "scored": self.flow.scored,
                                                                    "fired": self.flow.fired}}
        return snap
