"""Pair-lock with a RECOVERY HEDGE and a PROFIT LOCK (2026-10-02 spec; v2 accounting): features, the probability
models and the decisions, shared by the live engine (strategy/pairlock_engine.py) and the replay
(scripts/pairlock/replay3.py). The accounting, the states and the candidate pricing live in strategy/pairlock_book.py.

The strategy (implemented as specified, not re-optimised):
  INITIAL ENTRY (unchanged v2 gate) -> directional position -> ADVERSE MOVE (materially losing: RECOVERY_MIN_LOSS and
  RECOVERY_MIN_ADVERSE_MOVE) -> REVERSAL CONFIRMED (>= RECOVERY_MIN_CONFIRMATIONS of 4 signals, held for
  RECOVERY_CONFIRMATION_WINDOW, fresh data, normal spreads) -> RECOVERY EVALUATION -> partial / full recovery hedge
  -> matched pairs held to settlement. Separately, while NOT materially losing, a PROFIT LOCK when the opposite
  token is cheap enough. After any inventory the market starts no other cycle (REENTRY_BLOCKED).

MODE B, recovery hedge (manage_position, from REVERSAL_CONFIRMED only). For each candidate q (fractions of
  min(unmatched, RECOVERY_MAX_HEDGE_FRACTION x first-leg qty - already hedged)):
    acceptable  <=> generic limits AND incr_pair_cost(q) <= MAX_ACCEPTABLE_PAIR_COST
                    AND downside_reduction(q) >= MIN_RECOVERY_DOWNSIDE_REDUCTION
    utility(q)   = expected_pnl_after(q) + RISK_AVERSION x downside_reduction(q)
    send the acceptable q with the largest utility iff utility(q) > expected_pnl_before (= utility(0)).
  No MIN_HEDGE_EV: a recovery hedge may lower the expected P&L if it buys enough downside reduction.
MODE A, profit lock (from DIRECTIONAL_POSITION / UNMATCHED_DIRECTIONAL only):
    qualifies   <=> generic limits AND incr_pair_cost(q) <= PROFIT_LOCK_MAX_COST AND incr_pair_edge(q) >=
                    PROFIT_LOCK_MIN_EDGE AND q x incr_pair_edge(q) >= MIN_LOCKED_PROFIT
    send the qualifying q with the largest expected_after + PROFIT_LOCK_RISK_AVERSION x downside_reduction iff it
    beats holding (expected_pnl_before).
  incr_pair_cost(q) = heavy avg cost + heavy fee per share + (opposite VWAP + SLIPPAGE_ALLOWANCE) + opposite fee;
  the FOK limit is capped so that no fill can pair above the cap.

A fact the numbers rest on: the two books mirror each other (DOWN ask = 1 - UP bid on every recorded sample), so
buying q of the opposite token after the original fell to bid b pairs at avg + (1 - b) + fees = 1 + (avg - b) + fees.
A materially losing position can therefore only be recovery-hedged at a pair cost ABOVE 1 by at least the loss per
share; with MAX_ACCEPTABLE_PAIR_COST <= 1 no recovery hedge can qualify. Payoff-wise a hedge of q equals selling q of
the original at its bid (same outcomes, same fee formula), with more capital tied up until settlement.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass, field

from .pairlock_book import (EPS, HELD_STATES, LEGACY_STATES, TRANSITIONS, WATCH_STATES, FillRecord,  # noqa: F401
                            HedgeCandidate, PairBook, PendingOrder, State, check_transition, evaluate_hedges,
                            fee_per_share, pair_lock_check, vwap_buy)

FEE_RATE = 0.07
FEATURES = ("logit_mid", "z_fv", "mom1", "mom3", "mom10", "tv1", "tv3", "tacc", "imb", "pmflow", "agree")
#: the reversal score's components, each signed in the UP direction (the score flips them toward the hedge side)
REVERSAL = ("tv1", "tv3", "tacc", "mom1", "mom3", "imb", "pmflow", "agree")
#: the short-horizon model's features, signed toward the traded side
SHORT = ("mom1", "mom3", "tv1", "tv3", "tacc", "imb", "pmflow", "agree", "logit_side", "left_frac")
DIRECTIONAL = ("z_fv", "mom1", "mom3", "mom10", "tv1", "tv3", "tacc", "imb", "pmflow", "agree")
EXCHANGES = ("binance", "bybit", "coinbase", "deribit", "kraken", "bitstamp")


def _logit(p: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1.0 - p))


def _clip(x: float, lim: float) -> float:
    return -lim if x < -lim else lim if x > lim else x


def mid_of(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return 0.5 * (bid + ask)
    if ask is not None:
        return ask - 0.005
    if bid is not None:
        return bid + 0.005
    return None


# ───────────────────────────────── history and features ─────────────────────────────────


class History:
    """A market's sampled state in the chart point schema (t, ub, ua, db, da, ubd, uad, spot, srcs). The replay
    builds it from data/charts, the live engine appends one point per evaluation; features read indices <= i."""

    __slots__ = ("t", "ub", "ua", "db", "da", "ubd", "uad", "spot", "srcs", "keep_ms")
    _SERIES = ("t", "ub", "ua", "db", "da", "ubd", "uad", "spot", "srcs")

    def __init__(self, keep_ms: float = 0.0) -> None:
        for n in self._SERIES:
            setattr(self, n, [])
        self.keep_ms = keep_ms

    def append(self, t: float, ub, ua, db, da, ubd, uad, spot, srcs: dict | None = None) -> None:
        if self.t and t <= self.t[-1]:
            return
        self.t.append(t)
        self.ub.append(ub)
        self.ua.append(ua)
        self.db.append(db)
        self.da.append(da)
        self.ubd.append(float(ubd or 0.0))
        self.uad.append(float(uad or 0.0))
        self.spot.append(spot)
        self.srcs.append(dict(srcs) if srcs else None)
        if self.keep_ms > 0.0 and len(self.t) > 64 and self.t[0] < t - self.keep_ms:
            cut = bisect_right(self.t, t - self.keep_ms)
            for name in self._SERIES:
                del getattr(self, name)[:cut]

    @classmethod
    def from_points(cls, points: list[dict]) -> "History":
        h = cls()
        for p in points:
            h.append(p["t"], p.get("ub"), p.get("ua"), p.get("db"), p.get("da"), p.get("ubd"), p.get("uad"),
                     p.get("spot"), p.get("srcs"))
        return h

    def __len__(self) -> int:
        return len(self.t)


def realized_sigma(h: History, t_end: float, lookback_s: float = 300.0) -> float | None:
    """Per-second log volatility of the spot over [t_end - lookback, t_end), from ~1 s steps. Needs >= 60 steps."""
    i0 = bisect_right(h.t, t_end - lookback_s * 1000.0)
    i1 = bisect_right(h.t, t_end) - 1
    acc, n, prev = 0.0, 0, None
    for i in range(i0, i1 + 1):
        s = h.spot[i]
        if s is None or s <= 0.0:
            continue
        if prev is None:
            prev = (h.t[i], s)
            continue
        dt = (h.t[i] - prev[0]) / 1000.0
        if dt >= 1.0:
            if dt <= 5.0:
                r = math.log(s / prev[1])
                acc += r * r / dt
                n += 1
            prev = (h.t[i], s)
    return math.sqrt(acc / n) if n >= 60 else None


def features(h: History, i: int, strike: float, sigma1: float, close_ts: float) -> dict | None:
    """The decision features at sample i, from samples <= i only (signed toward UP). None when the history
    cannot support them."""
    t = h.t[i]
    mu = mid_of(h.ub[i], h.ua[i])
    s = h.spot[i]
    if mu is None or s is None or s <= 0.0 or not sigma1 or sigma1 <= 0.0 or strike <= 0.0:
        return None

    def back(k_ms: float):
        j = bisect_right(h.t, t - k_ms) - 1
        if j < 0 or t - h.t[j] > k_ms + 1500.0:
            return None
        return j

    js = {k: back(k) for k in (1000.0, 2000.0, 3000.0, 10_000.0)}
    if any(v is None for v in js.values()):
        return None
    sp = {k: h.spot[j] for k, j in js.items()}
    if any(v is None or v <= 0.0 for v in sp.values()):
        return None
    mids = {k: mid_of(h.ub[j], h.ua[j]) for k, j in js.items()}
    if any(v is None for v in mids.values()):
        return None
    left = (close_ts - t) / 1000.0
    r1, r3, r10 = math.log(s / sp[1000.0]), math.log(s / sp[3000.0]), math.log(s / sp[10_000.0])
    tv1 = (mu - mids[1000.0]) * 100.0
    tv_prev = (mids[1000.0] - mids[2000.0]) * 100.0
    du, ua = h.ubd[i], h.uad[i]
    j1 = js[1000.0]
    # Polymarket depth flow over 1 s, toward UP: UP asks removed near the touch (consumed or pulled) minus UP bids
    # removed. (DOWN's book mirrors UP's: DOWN asks = UP bids.) 200 ms samples cannot split consumption from
    # cancellation; the tick study does (scripts/pairlock/orderflow_study.py).
    a0, b0 = h.uad[j1], h.ubd[j1]
    acons = max(0.0, a0 - ua) / max(a0, 1.0)
    bcons = max(0.0, b0 - du) / max(b0, 1.0)
    # cross-exchange agreement: the mean sign of each exchange's 1 s return (+1 all up), |r| >= 0.1 sigma counts
    agree, n_ex = 0.0, 0
    cur, old = h.srcs[i] or {}, h.srcs[j1] or {}
    for x in EXCHANGES:
        a, b = cur.get(x), old.get(x)
        if a and b and a > 0 and b > 0:
            r = math.log(a / b)
            agree += (1.0 if r > 0 else -1.0) if abs(r) >= 0.1 * sigma1 else 0.0
            n_ex += 1
    return {
        "logit_mid": _logit(mu),
        "z_fv": _clip(math.log(s / strike) / (sigma1 * math.sqrt(max(left, 1.0))), 8.0),
        "mom1": _clip(r1 / sigma1, 8.0),
        "mom3": _clip(r3 / (sigma1 * math.sqrt(3.0)), 8.0),
        "mom10": _clip(r10 / (sigma1 * math.sqrt(10.0)), 8.0),
        "tv1": _clip(tv1, 50.0),
        "tv3": _clip((mu - mids[3000.0]) * 100.0, 50.0),
        "tacc": _clip(tv1 - tv_prev, 50.0),
        "imb": (du - ua) / (du + ua) if du + ua > 0.0 else 0.0,
        "pmflow": acons - bcons,
        "agree": agree / n_ex if n_ex else 0.0,
        "mid_up": mu, "left": left,
    }


def side_features(feat: dict, side: str) -> dict:
    """The short-horizon model's inputs, signed toward ``side``."""
    d = 1.0 if side == "UP" else -1.0
    out = {k: d * feat[k] for k in DIRECTIONAL if k in feat}
    m = feat["mid_up"] if side == "UP" else 1.0 - feat["mid_up"]
    out["logit_side"] = _logit(m)
    out["left_frac"] = feat["left"] / 300.0
    return out


# ───────────────────────────────── the probability models ─────────────────────────────────


@dataclass
class ProbModel:
    """P(UP wins | features): logistic, fitted on TRAIN windows only (scripts/pairlock/replay.py prepare); the
    reversal score's scales; the execution constants measured on TRAIN; and the SHORT-horizon model
    P(the traded token's bid in N s >= the entry ask) on the side-signed features. ``market_only`` is the
    fallback: the market mid itself (no edge over the market by construction)."""
    features: tuple[str, ...] = FEATURES
    beta: list[float] = field(default_factory=lambda: [0.0, 1.0] + [0.0] * (len(FEATURES) - 1))
    rev_scales: dict[str, float] = field(default_factory=lambda: {k: 1.0 for k in REVERSAL})
    exec: dict[str, float] = field(default_factory=lambda: {"entry_fill_rate": 0.5, "entry_slip": 0.0,
                                                            "entry_adv": 0.0, "hedge_fill_rate": 0.8,
                                                            "hedge_slip": 0.0})
    short: dict = field(default_factory=dict)          # {"features", "beta", "horizon_s"}
    meta: dict = field(default_factory=dict)

    def p_up(self, feat: dict) -> float:
        x = self.beta[0] + sum(b * feat.get(f, 0.0) for b, f in zip(self.beta[1:], self.features))
        return 1.0 / (1.0 + math.exp(-_clip(x, 30.0)))

    def p_side(self, feat: dict, side: str) -> float:
        p = self.p_up(feat)
        return p if side == "UP" else 1.0 - p

    def p_favourable(self, feat: dict, side: str) -> float | None:
        if not self.short:
            return None
        sf = side_features(feat, side)
        b = self.short["beta"]
        x = b[0] + sum(c * sf.get(f, 0.0) for c, f in zip(b[1:], self.short["features"]))
        return 1.0 / (1.0 + math.exp(-_clip(x, 30.0)))

    @classmethod
    def market_only(cls) -> "ProbModel":
        return cls(meta={"kind": "market_only"})

    def to_json(self) -> dict:
        d = asdict(self)
        d["features"] = list(self.features)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "ProbModel":
        return cls(features=tuple(d["features"]), beta=list(d["beta"]), rev_scales=dict(d["rev_scales"]),
                   exec=dict(d["exec"]), short=dict(d.get("short") or {}), meta=dict(d.get("meta") or {}))

    @classmethod
    def load(cls, path: str) -> "ProbModel":
        with open(path, encoding="utf-8") as f:
            return cls.from_json(json.load(f))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, indent=1)


def reversal_score(feat: dict, a_side: str, scales: dict[str, float]) -> float:
    """Evidence that the market is moving AGAINST side A: the mean of the standardised components (token
    velocity / acceleration, crypto momentum, book imbalance, Polymarket depth flow, exchange agreement), each
    signed toward the other side. Equal weights on purpose: nothing in the score is fitted to outcomes."""
    d = 1.0 if a_side == "UP" else -1.0
    ks = [k for k in REVERSAL if k in feat]
    return sum(-d * feat[k] / (scales.get(k) or 1.0) for k in ks) / max(len(ks), 1)


# ───────────────────────────────── parameters and limits ─────────────────────────────────


@dataclass
class PairLockParams:
    """Every threshold, TPB_PAIRLOCK_<FIELD> (pairlock_engine.PairLockConfig.from_env). The recovery-hedge and
    profit-lock defaults were set a priori on 2026-10-02, NOT fitted (the spec: implement first, optimise later)."""
    # ---- entry (unchanged from v2)
    entry_edge: float = 0.02          # min settlement EV per share after fee, slippage and adverse selection
    entry_mode: str = "settlement"    # "settlement" | "both": also P(favourable within N s) >= short_min
    short_min: float = 0.5
    entry_shares: float = 10.0
    min_size: float = 5.0             # the venue minimum order
    band_lo: float = 0.20
    band_hi: float = 0.96
    entry_offset: float = 0.03        # FOK limit = ask + offset
    min_left_entry_s: float = 60.0
    max_cycles_per_market: int = 2    # entry ATTEMPTS; once a cycle holds inventory no other starts (REENTRY_BLOCKED)
    # ---- position management
    hedge_mode: str = "combined"      # "none" (A) | "recovery" (C) | "profit_lock" (D) | "combined" (E)
    fractions: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)   # candidate sizes, of the hedgeable quantity
    slippage_allowance: float = 0.005  # per share added to the opposite VWAP in every hedge decision
    hedge_offset: float = 0.03        # FOK limit = the VWAP's last level + offset (then capped by the pair-cost cap)
    hedge_cooldown_ms: float = 1000.0
    reeval_ms: float = 5000.0         # after a rejected evaluation, the next waits this long (unless newly confirmed)
    depth_haircut: float = 0.0        # share of each ask level assumed taken by faster traders
    # ---- MODE B: recovery hedge (the primary strategy)
    recovery_min_loss: float = 0.50                 # RECOVERY_MIN_LOSS: unrealised loss (USDC) at the executable bid
    recovery_min_adverse_move: float = 0.05         # RECOVERY_MIN_ADVERSE_MOVE: avg cost - bid, per share
    recovery_reversal_threshold: float = 1.5        # RECOVERY_REVERSAL_THRESHOLD: the composite reversal score
    recovery_confirmation_window_ms: float = 400.0  # RECOVERY_CONFIRMATION_WINDOW: the signals must hold this long
    recovery_min_confirmations: int = 3             # of the 4 reversal signals
    recovery_min_time_left_s: float = 10.0          # RECOVERY_MIN_TIME_LEFT
    recovery_max_hedge_fraction: float = 1.0        # RECOVERY_MAX_HEDGE_FRACTION: of the first leg's filled quantity
    max_acceptable_pair_cost: float = 1.00          # MAX_ACCEPTABLE_PAIR_COST: the paired portion, fees + slippage in
    min_recovery_downside_reduction: float = 0.50   # MIN_RECOVERY_DOWNSIDE_REDUCTION (USDC of worst case)
    risk_aversion: float = 0.25                     # RISK_AVERSION: utility = expected + RA x downside reduction
    # ---- MODE A: profit lock
    profit_lock_min_edge: float = 0.05              # PROFIT_LOCK_MIN_EDGE: net edge per paired share
    profit_lock_max_cost: float = 0.95              # PROFIT_LOCK_MAX_COST: pair cost per share, fees + slippage in
    profit_lock_risk_aversion: float = 0.25         # the profit lock's own risk-vs-return weight
    profit_lock_min_time_left_s: float = 10.0
    # ---- what counts as a PAIR_LOCKED (profitable, fully matched) position
    min_pair_edge: float = 0.05       # MIN_PAIR_EDGE: net pair edge per matched share
    min_locked_profit: float = 0.05   # MIN_LOCKED_PROFIT (USDC)

    def __post_init__(self) -> None:
        # the spec's invariants, not tunables: a recovery hedge needs a materially losing position AND a reversal
        # confirmed by at least two signals -- never the loss alone
        if not 2 <= int(self.recovery_min_confirmations) <= 4:
            raise ValueError(f"recovery_min_confirmations {self.recovery_min_confirmations}: must be 2..4")
        if self.recovery_min_adverse_move <= 0.0 or self.recovery_min_loss <= 0.0:
            raise ValueError("recovery_min_adverse_move and recovery_min_loss must be > 0 (materially losing)")
        if self.risk_aversion < 0.0 or self.profit_lock_risk_aversion < 0.0:
            raise ValueError("risk aversion must be >= 0")
        if self.hedge_mode not in ("none", "recovery", "profit_lock", "combined"):
            raise ValueError(f"hedge_mode {self.hedge_mode!r}: none | recovery | profit_lock | combined")


@dataclass
class RiskLimits:
    max_market_capital: float = 20.0      # MAX_MARKET_CAPITAL: USDC spent in one market, all cycles
    max_initial_position: float = 10.0    # MAX_INITIAL_POSITION: shares of a cycle's first leg
    max_total_position: float = 40.0      # MAX_TOTAL_POSITION: shares, both sides, one market
    max_hedge_position: float = 10.0      # MAX_HEDGE_POSITION: hedge shares in one cycle
    max_hedge_actions: int = 3            # MAX_HEDGE_ACTIONS: hedge orders sent in one cycle
    max_market_loss: float = 10.0         # MAX_MARKET_LOSS: worst-case settlement loss in one market
    max_daily_loss: float = 40.0          # MAX_DAILY_LOSS (checked by the caller: live RiskManager / replay per day)
    max_drawdown: float = 60.0            # MAX_DRAWDOWN (the caller)
    max_spread: float = 0.05              # MAX_SPREAD of the traded tokens
    max_quote_age_ms: float = 2_000.0     # MAX_QUOTE_AGE (live)
    max_spot_age_ms: float = 1_000.0      # MAX_SPOT_AGE (live)
    min_time_remaining_s: float = 10.0    # MIN_TIME_LEFT: nothing at all below this


#: v2 settings that no longer exist (the EV-maximising hedge rule): from_env warns when they are still set
OBSOLETE_PARAMS = ("ADVERSE_MIN", "REV_THR", "CONFIRM_MS", "MIN_HEDGE_EV", "LOCK_OPPORTUNITIES",
                   "UNCONDITIONAL_TRIGGER", "MIN_LEFT_HEDGE_S", "LIM_MAX_PAIR_COST")


@dataclass
class MarketExposure:
    """What one market already holds or has in flight, all cycles (the risk checks read it)."""
    capital: float = 0.0
    shares: float = 0.0
    worst_case: float = 0.0
    cycles: int = 0
    held: bool = False                # some cycle of this market holds inventory: no new cycle (REENTRY_BLOCKED)
    held_state: str = ""


@dataclass
class MarketView:
    """One sample of both tokens: best prices, the ask levels (and the bid levels where known), quote ages."""
    bid_up: float | None = None
    ask_up: float | None = None
    bid_down: float | None = None
    ask_down: float | None = None
    asks_up: list = field(default_factory=list)
    asks_down: list = field(default_factory=list)
    bids_up: list = field(default_factory=list)
    bids_down: list = field(default_factory=list)
    quote_age_up_ms: float | None = None
    quote_age_down_ms: float | None = None
    spot_age_ms: float | None = None

    def bid(self, side: str):
        return self.bid_up if side == "UP" else self.bid_down

    def ask(self, side: str):
        return self.ask_up if side == "UP" else self.ask_down

    def asks(self, side: str) -> list:
        return self.asks_up if side == "UP" else self.asks_down

    def bids(self, side: str) -> list:
        return self.bids_up if side == "UP" else self.bids_down

    def quote_age(self, side: str):
        return self.quote_age_up_ms if side == "UP" else self.quote_age_down_ms


# ───────────────────────────────── the cycle ─────────────────────────────────


@dataclass
class Transition:
    t: float
    frm: str
    to: str
    reason: str


@dataclass
class AuditEvent:
    t: float
    name: str
    state: str
    reason: str
    data: dict = field(default_factory=dict)


@dataclass
class Cycle:
    """One position in one market, from the first signal to settlement. The PairBook (fills) is authoritative;
    the state changes only through validated transitions (pairlock_book.TRANSITIONS); every decision leaves an
    AuditEvent with the inventory at that moment."""
    book: PairBook
    a_side: str
    state: State = State.WAITING
    started_t: float = 0.0
    entry_qty: float = 0.0            # the first leg's FILLED shares (the recovery fraction's base)
    entry_info: dict = field(default_factory=dict)
    hedge_actions: int = 0
    hedge_fills: int = 0
    hedge_kills: int = 0
    recovery_qty: float = 0.0
    profit_lock_qty: float = 0.0
    last_action_t: float = -1e18
    last_eval_t: float = -1e18
    last_eval_rejected: bool = False
    last_eval_episode: int = -1
    episode: int = 0                  # +1 every time a reversal is newly confirmed
    rev_since: float | None = None
    reversal_confirmed: bool = False
    adverse_now: bool = False
    adverse_seen: bool = False
    high_bid: float | None = None     # the heavy side's highest bid since the entry filled
    last_obs: dict = field(default_factory=dict)
    pending_hedge: dict | None = None
    recovery_downside: float = 0.0    # realised worst-case improvement of the recovery hedges (USDC)
    recovery_edges: list = field(default_factory=list)       # realised net edge per paired share, recovery fills
    profit_lock_locked: float = 0.0   # realised locked profit of the profit-lock fills (USDC)
    pair_formed_t: float | None = None
    locked_t: float | None = None
    settled_winner: str | None = None
    transitions: list[Transition] = field(default_factory=list)
    events: list[AuditEvent] = field(default_factory=list)

    @property
    def b_side(self) -> str:
        return "DOWN" if self.a_side == "UP" else "UP"

    def to(self, new: State, t: float, reason: str = "") -> None:
        check_transition(self.state, new)
        if new is not self.state:
            self.transitions.append(Transition(t, self.state.value, new.value, reason))
            self.state = new

    def note(self, name: str, t: float, reason: str = "", **data) -> None:
        self.events.append(AuditEvent(t, name, self.state.value, reason, data))

    @property
    def holds(self) -> bool:
        return self.book.up_qty + self.book.down_qty > EPS

    def attribution(self, winner: str) -> dict:
        """Settled P&L by leg: the first side alone (= the NO-HEDGE BASELINE of this cycle, nothing was ever sold) and
        each hedge kind's own leg; and by exposure (matched pairs vs the directional remainder). Both sum to the total."""
        b = self.book
        a, o = self.a_side, self.b_side
        leg_a = b.qty(a) * (1.0 if winner == a else 0.0) - b.total_cost_side(a)
        legs = {"recovery": 0.0, "profit_lock": 0.0}
        for f in b.fills:
            if f.side == o and f.action == "BUY":
                k = "profit_lock" if f.kind == "profit_lock" else "recovery"
                legs[k] += f.qty * (1.0 if winner == o else 0.0) - f.gross_cost - f.fee
        total = leg_a + legs["recovery"] + legs["profit_lock"] + b.realised_exit_pnl
        pairs = b.locked_pair_pnl
        return {"total": total, "prediction": leg_a, "hedging": total - leg_a, "recovery_leg": legs["recovery"],
                "profit_lock_leg": legs["profit_lock"], "pairs": pairs, "directional": total - pairs}

    def to_json(self) -> dict:
        return {"book": self.book.to_json(), "a_side": self.a_side, "state": self.state.value,
                "started_t": self.started_t, "entry_qty": self.entry_qty, "entry_info": self.entry_info,
                "hedge_actions": self.hedge_actions, "hedge_fills": self.hedge_fills, "hedge_kills": self.hedge_kills,
                "recovery_qty": self.recovery_qty, "profit_lock_qty": self.profit_lock_qty,
                "last_action_t": self.last_action_t, "adverse_seen": self.adverse_seen, "high_bid": self.high_bid,
                "episode": self.episode, "recovery_downside": self.recovery_downside,
                "recovery_edges": self.recovery_edges, "profit_lock_locked": self.profit_lock_locked,
                "pair_formed_t": self.pair_formed_t, "locked_t": self.locked_t,
                "transitions": [asdict(x) for x in self.transitions[-50:]],
                "events": [asdict(x) for x in self.events[-50:]]}

    @classmethod
    def from_json(cls, d: dict, prm: "PairLockParams | None" = None) -> "Cycle":
        raw = str(d.get("state", "DIRECTIONAL_POSITION"))
        st = State(LEGACY_STATES.get(raw, raw))
        c = cls(book=PairBook.from_json(d["book"]), a_side=d["a_side"], state=st,
                started_t=float(d.get("started_t") or 0.0), entry_qty=float(d.get("entry_qty") or 0.0),
                entry_info=dict(d.get("entry_info") or {}), hedge_actions=int(d.get("hedge_actions") or 0),
                hedge_fills=int(d.get("hedge_fills") or 0), hedge_kills=int(d.get("hedge_kills") or 0),
                recovery_qty=float(d.get("recovery_qty") or 0.0), profit_lock_qty=float(d.get("profit_lock_qty") or 0.0),
                last_action_t=float(d.get("last_action_t") or -1e18), adverse_seen=bool(d.get("adverse_seen")),
                high_bid=d.get("high_bid"), episode=int(d.get("episode") or 0),
                recovery_downside=float(d.get("recovery_downside") or 0.0),
                recovery_edges=list(d.get("recovery_edges") or []),
                profit_lock_locked=float(d.get("profit_lock_locked") or 0.0), pair_formed_t=d.get("pair_formed_t"),
                locked_t=d.get("locked_t"), transitions=[Transition(**x) for x in d.get("transitions") or []],
                events=[AuditEvent(**x) for x in d.get("events") or []])
        if not c.entry_qty:
            c.entry_qty = sum(f.qty for f in c.book.fills if f.kind == "entry" and f.action == "BUY")
        # an order in flight when the bot stopped resolved at the venue while it was down: a restart never resumes
        # a pending order (the paper venue forgot it); the inventory decides where the cycle stands
        if c.book.pending:
            c.book.pending.clear()
        if c.state in (State.INITIAL_ENTRY, State.RECOVERY_EVALUATION, State.RECOVERY_HEDGE_PENDING,
                       State.PROFIT_LOCK_EVALUATION, State.PROFIT_LOCK_PENDING, State.RECOVERY_HEDGED) \
                or raw in LEGACY_STATES:
            c.state = State.WAITING if not c.holds else watch_state(c, prm or PairLockParams())
        return c


def watch_state(cyc: Cycle, prm: PairLockParams) -> State:
    """Where a cycle stands from its INVENTORY (and the adverse / reversal flags of the unmatched part):
    fully matched -> PAIR_LOCKED (a profitable lock) or RECOVERY_HEDGED; otherwise the directional states."""
    b = cyc.book
    if not cyc.holds:
        return State.WAITING
    if b.matched_qty > EPS and b.unmatched <= 1e-6:
        ok, _ = pair_lock_check(b, prm.min_pair_edge, prm.min_locked_profit)
        return State.PAIR_LOCKED if ok else State.RECOVERY_HEDGED
    if cyc.reversal_confirmed and cyc.adverse_now:
        return State.REVERSAL_CONFIRMED
    if cyc.adverse_now:
        return State.ADVERSE_MOVE
    return State.UNMATCHED_DIRECTIONAL if b.matched_qty > EPS else State.DIRECTIONAL_POSITION


def _move(cyc: Cycle, target: State, t: float, reason: str) -> None:
    """A watch-state change through the validated table (a directional position reaches REVERSAL_CONFIRMED
    only through ADVERSE_MOVE; a remainder with pairs comes back through UNMATCHED_DIRECTIONAL)."""
    if target is cyc.state:
        return
    if target not in TRANSITIONS[cyc.state]:
        if target is State.REVERSAL_CONFIRMED and State.ADVERSE_MOVE in TRANSITIONS[cyc.state]:
            cyc.to(State.ADVERSE_MOVE, t, reason)
        elif target in (State.ADVERSE_MOVE, State.REVERSAL_CONFIRMED) and State.UNMATCHED_DIRECTIONAL in TRANSITIONS[cyc.state]:
            cyc.to(State.UNMATCHED_DIRECTIONAL, t, reason)
            if target not in TRANSITIONS[cyc.state]:
                cyc.to(State.ADVERSE_MOVE, t, reason)
    cyc.to(target, t, reason)


# ───────────────────────────────── observation: adverse move and reversal ─────────────────────────────────


def vwap_sell(levels, qty: float) -> tuple[float | None, float]:
    """Walk the bid levels (price descending) for ``qty`` shares: (VWAP or None when the depth cannot take it,
    the taker fee per share)."""
    need, got, fee = qty, 0.0, 0.0
    for p, s in sorted(levels, key=lambda x: -x[0]):
        take = min(need, float(s))
        if take <= EPS:
            continue
        got += take * p
        fee += take * fee_per_share(p)
        need -= take
        if need <= 1e-9:
            break
    if need > 1e-9 or qty <= 0:
        return None, 0.0
    return got / qty, fee / qty


REVERSAL_SIGNALS = ("original_weakening", "opposite_flow", "spot_against", "reversal_score")


def reversal_signals(feat: dict, heavy: str, model: ProbModel, prm: PairLockParams) -> dict:
    """The four confirmation signals, each signed toward the OPPOSITE (light) side (brief item 6):
        original_weakening  the original token's 1 s velocity <= 0 and 3 s velocity < 0
        opposite_flow       Polymarket depth flow toward the opposite token > 0 and book imbalance toward it >= 0
        spot_against        the composite spot's 1 s and 3 s momentum against the original, exchanges not disagreeing
        reversal_score      the equal-weight composite score >= RECOVERY_REVERSAL_THRESHOLD
    (The two books mirror each other -- DOWN ask = 1 - UP bid on every recorded sample -- so the opposite token's
    price velocity is the original's with the sign flipped; the order FLOW is the independent opposite-side signal.)"""
    d = 1.0 if heavy == "UP" else -1.0
    score = reversal_score(feat, heavy, model.rev_scales)
    out = {"original_weakening": d * feat.get("tv1", 0.0) <= 0.0 and d * feat.get("tv3", 0.0) < 0.0,
           "opposite_flow": -d * feat.get("pmflow", 0.0) > 0.0 and -d * feat.get("imb", 0.0) >= 0.0,
           "spot_against": -d * feat.get("mom1", 0.0) > 0.0 and -d * feat.get("mom3", 0.0) > 0.0
                           and -d * feat.get("agree", 0.0) >= 0.0,
           "reversal_score": score >= prm.recovery_reversal_threshold}
    return {"signals": out, "count": sum(out.values()), "score": score,
            "momentum_1s": -d * feat.get("mom1", 0.0), "velocity_1s": d * feat.get("tv1", 0.0),
            "velocity_3s": d * feat.get("tv3", 0.0), "acceleration": d * feat.get("tacc", 0.0),
            "flow": -d * feat.get("pmflow", 0.0), "imbalance": -d * feat.get("imb", 0.0)}


def observe(cyc: Cycle, feat: dict | None, mv: MarketView, now: float, left: float, model: ProbModel,
            prm: PairLockParams, lim: RiskLimits) -> dict:
    """Update the position's adverse / reversal view from this sample (brief items 5 and 6) and move the cycle
    between the watch states. Returns what was measured (the audit record's position block)."""
    b = cyc.book
    heavy = b.heavy_side
    if heavy is None or cyc.state not in WATCH_STATES:
        return {}
    light = "DOWN" if heavy == "UP" else "UP"
    rem = b.unmatched
    avg = b.avg_cost(heavy)
    h_fee_ps = (b.up_fees if heavy == "UP" else b.down_fees) / max(b.qty(heavy), EPS)
    bid_h, ask_h, ask_l = mv.bid(heavy), mv.ask(heavy), mv.ask(light)
    if bid_h is not None:
        cyc.high_bid = bid_h if cyc.high_bid is None else max(cyc.high_bid, bid_h)
    ex_px, ex_fee = vwap_sell(mv.bids(heavy), rem) if mv.bids(heavy) else (bid_h, fee_per_share(bid_h or 0.0))
    if ex_px is None:
        ex_px, ex_fee = bid_h, fee_per_share(bid_h or 0.0)
    unreal = None if ex_px is None or avg is None else rem * (ex_px - ex_fee - avg - h_fee_ps)
    move = None if bid_h is None or avg is None else bid_h - avg
    spread_h = None if bid_h is None or ask_h is None else ask_h - bid_h
    spread_l = None if mv.bid(light) is None or ask_l is None else ask_l - mv.bid(light)
    qa = [x for x in (mv.quote_age(heavy), mv.quote_age(light)) if x is not None]
    fresh = ((mv.spot_age_ms is None or mv.spot_age_ms <= lim.max_spot_age_ms)
             and all(x <= lim.max_quote_age_ms for x in qa) and feat is not None)
    spread_ok = (spread_h is not None and spread_h <= lim.max_spread + 1e-9
                 and spread_l is not None and spread_l <= lim.max_spread + 1e-9)
    adverse = (move is not None and unreal is not None and -move >= prm.recovery_min_adverse_move - 1e-9
               and unreal <= -prm.recovery_min_loss + 1e-9)
    rs = reversal_signals(feat, heavy, model, prm) if feat is not None else {"signals": {}, "count": 0, "score": None}
    cond = adverse and fresh and spread_ok and rs["count"] >= prm.recovery_min_confirmations
    if cond:
        cyc.rev_since = now if cyc.rev_since is None else cyc.rev_since
    else:
        cyc.rev_since = None
    confirmed = cond and now - cyc.rev_since >= prm.recovery_confirmation_window_ms - 1e-9
    obs = {"original": heavy, "opposite": light, "entry_vwap": avg, "bid": bid_h, "ask": ask_h,
           "exit_value": None if ex_px is None else rem * (ex_px - ex_fee), "unrealised_pnl": unreal,
           "price_change": move, "drawdown_from_high": None if cyc.high_bid is None or bid_h is None else bid_h - cyc.high_bid,
           "opposite_ask": ask_l, "time_left": left, "fresh": fresh, "spread_ok": spread_ok,
           "confirmations": rs["count"], "signals": rs["signals"], "score": rs.get("score"),
           "momentum_1s": rs.get("momentum_1s"), "velocity_1s": rs.get("velocity_1s"),
           "acceleration": rs.get("acceleration"), "flow": rs.get("flow"), "unmatched": rem}
    cyc.last_obs = obs
    was_adv, was_conf = cyc.adverse_now, cyc.reversal_confirmed
    cyc.adverse_now, cyc.reversal_confirmed = adverse, confirmed
    if adverse:
        cyc.adverse_seen = True
    target = watch_state(cyc, prm)
    if target is not cyc.state and target in WATCH_STATES:
        if target is State.REVERSAL_CONFIRMED and not was_conf:
            cyc.episode += 1
        reason = (f"{heavy} bid {_f(bid_h)} vs entry {_f(avg)} ({_sg(move)}), unrealised {_sg(unreal)}, "
                  f"high {_f(cyc.high_bid)}, {rs['count']}/{len(REVERSAL_SIGNALS)} reversal signals")
        if adverse and not was_adv:
            cyc.note("ADVERSE_MOVE_DETECTED", now, reason, **_pos(obs))
        _move(cyc, target, now, reason)
        if target is State.REVERSAL_CONFIRMED:
            on = [k for k, v in rs["signals"].items() if v]
            cyc.note("REVERSAL_CONFIRMED", now,
                     f"original={heavy} entry={_f(avg)} current={_f(bid_h)} drawdown={_sg(move)} "
                     f"momentum={_sg(rs.get('momentum_1s'))} opposite={light} opposite_ask={_f(ask_l)} "
                     f"confirmation={rs['count']}/{len(REVERSAL_SIGNALS)} ({', '.join(on)}) "
                     f"held {prm.recovery_confirmation_window_ms:.0f} ms time_left={left:.0f}s", **_pos(obs))
    return obs


def _pos(obs: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in obs.items() if k != "signals"} | \
        {"signals": dict(obs.get("signals") or {})}


def _f(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def _sg(x) -> str:
    return "-" if x is None else f"{x:+.3f}"


# ───────────────────────────────── decisions ─────────────────────────────────


@dataclass
class Decision:
    action: str                   # "entry" | "hedge"
    side: str
    shares: float
    limit: float
    ask: float
    reason: str = ""
    ev: float = 0.0               # entry: EV per share; hedge: utility gain (USDC)
    p: float = 0.5
    kind: str = ""                # hedge: "recovery" | "profit_lock"
    candidate: HedgeCandidate | None = None
    info: dict = field(default_factory=dict)


def entry_decision(feat: dict, side: str, ask: float | None, bid: float | None, ask_levels: list, model: ProbModel,
                   prm: PairLockParams, lim: RiskLimits, mkt: MarketExposure, left: float,
                   prev: Cycle | None = None) -> tuple[Decision | None, str]:
    """Enter only if the expected NET value per share clears ``entry_edge``:
        EV = P(side wins) - expected fill - fee - expected slippage - adverse-selection cost (TRAIN),
    the expected fill being the executable VWAP of the order's size on the ask levels (not the mid, not the last
    trade, not a fair value). ``entry_mode="both"`` also requires P(favourable within N s) >= short_min.
    A market whose cycle holds inventory (directional, hedged or locked) never starts another: REENTRY_BLOCKED."""
    if mkt.held or (prev is not None and prev.holds):
        held = mkt.held_state or (prev.state.value if prev is not None else "a cycle")
        return None, f"REENTRY_BLOCKED: the market already holds inventory ({held})"
    if prev is not None and prev.state is State.INITIAL_ENTRY:
        return None, "the previous entry is still in flight"
    if mkt.cycles >= prm.max_cycles_per_market:
        return None, f"{mkt.cycles} cycles already in this market"
    if left < max(prm.min_left_entry_s, lim.min_time_remaining_s):
        return None, f"{left:.0f} s left"
    if ask is None:
        return None, "no ask"
    if not (prm.band_lo - 1e-9 <= ask <= prm.band_hi + 1e-9):
        return None, f"ask {ask:.2f} outside {prm.band_lo:.2f}-{prm.band_hi:.2f}"
    if bid is None or ask - bid > lim.max_spread + 1e-9:
        return None, "spread"
    q = min(prm.entry_shares, lim.max_initial_position)
    if q < prm.min_size - 1e-9:
        return None, "size under the venue minimum"
    vw, fps, used = vwap_buy(ask_levels, q, prm.depth_haircut)
    if vw is None:
        return None, "depth"
    limit = min(0.99, round(max(p for p, _ in used) + prm.entry_offset, 2))
    worst_cost = q * (limit + fee_per_share(limit))
    if mkt.capital + worst_cost > lim.max_market_capital + 1e-9:
        return None, "market capital"
    if mkt.shares + q > lim.max_total_position + 1e-9:
        return None, "market position"
    if mkt.worst_case - worst_cost < -lim.max_market_loss - 1e-9:
        return None, "market loss limit"
    p = model.p_side(feat, side)
    ev = p - vw - fps - float(model.exec.get("entry_slip", 0.0)) - float(model.exec.get("entry_adv", 0.0))
    p_fav = model.p_favourable(feat, side)
    info = {"p": p, "ev": ev, "vwap": vw, "fee": fps, "p_fav": p_fav}
    if ev < prm.entry_edge - 1e-12:
        return None, f"entry EV {ev:+.3f} < {prm.entry_edge:+.3f}"
    if prm.entry_mode == "both" and (p_fav is None or p_fav < prm.short_min - 1e-12):
        return None, f"P(favourable) {p_fav} < {prm.short_min:.2f}"
    return Decision("entry", side, q, limit, ask, ev=ev, p=p, info=info), ""


def max_price_for_pair_cost(cap: float, heavy_avg: float, heavy_fee_ps: float) -> float:
    """The highest per-share price p of the opposite token with heavy_avg + heavy_fee + p + fee(p) <= cap: the FOK
    limit never allows a fill that would pair above the cap (fee(p) = 7 % p(1-p) rises on [0, 0.5])."""
    lo, hi = 0.0, 0.99
    if heavy_avg + heavy_fee_ps + lo > cap:
        return 0.0
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if heavy_avg + heavy_fee_ps + mid + fee_per_share(mid) <= cap + 1e-12:
            lo = mid
        else:
            hi = mid
    return lo


def _limit_for(c: HedgeCandidate, levels, haircut: float, offset: float, cap_price: float) -> tuple[float | None, str]:
    """FOK limit = the VWAP's last level + offset, capped so that no fill can pair above the cap (cents, down)."""
    last = _last_level(levels, c.q, haircut)
    lim = min(0.99, round(last + offset, 2), math.floor(cap_price * 100.0 + 1e-9) / 100.0)
    if lim + 1e-9 < last:
        return None, f"the pair-cost cap allows <= {cap_price:.3f}, the size needs {last:.2f}"
    return lim, ""


def _gates(cyc: Cycle, now: float, left: float, min_left: float, lim: RiskLimits, prm: PairLockParams,
           mv: MarketView, light: str, fresh: bool, spread_ok: bool) -> str:
    b = cyc.book
    if cyc.hedge_actions >= lim.max_hedge_actions:
        return "hedge actions exhausted"
    if now - cyc.last_action_t < prm.hedge_cooldown_ms or now - cyc.last_eval_t < prm.hedge_cooldown_ms:
        return "cooldown"
    if left < max(min_left, lim.min_time_remaining_s):
        return f"{left:.0f} s left"
    if b.unmatched < prm.min_size - 1e-9:
        return "unmatched under the venue minimum"
    if mv.ask(light) is None:
        return f"no {light} ask"
    if not fresh:
        return "stale spot or quote"
    if not spread_ok:
        return "spread"
    return ""


def _cands(cyc: Cycle, p_up: float, mv: MarketView, light: str, prm: PairLockParams, lim: RiskLimits,
           mkt: MarketExposure, base_qty: float | None) -> list[HedgeCandidate]:
    b = cyc.book
    hedge_used = sum(f.qty for f in b.fills if f.kind != "entry" and f.action == "BUY")
    return evaluate_hedges(b, p_up, mv.asks(light), fractions=prm.fractions, base_qty=base_qty, min_size=prm.min_size,
                           haircut=prm.depth_haircut, slippage_allowance=prm.slippage_allowance,
                           max_market_capital=lim.max_market_capital, capital_used=mkt.capital,
                           max_total_position=lim.max_total_position, shares_used=mkt.shares,
                           max_hedge_position=lim.max_hedge_position, hedge_shares_used=hedge_used,
                           max_market_loss=lim.max_market_loss, other_worst=mkt.worst_case - b.worst_case_pnl)


def recovery_constraints(c: HedgeCandidate, prm: PairLockParams) -> str:
    """MODE B's economic safety constraints on one candidate ('' = acceptable)."""
    if not c.feasible:
        return c.why
    if c.incr_pair_cost is None:
        return "no pair"
    if c.incr_pair_cost > prm.max_acceptable_pair_cost + 1e-9:
        return f"pair cost {c.incr_pair_cost:.3f} > MAX_ACCEPTABLE_PAIR_COST {prm.max_acceptable_pair_cost:.3f}"
    if c.downside_reduction < prm.min_recovery_downside_reduction - 1e-9:
        return (f"downside reduction {c.downside_reduction:+.3f} < MIN_RECOVERY_DOWNSIDE_REDUCTION "
                f"{prm.min_recovery_downside_reduction:.3f}")
    return ""


def profit_lock_constraints(c: HedgeCandidate, prm: PairLockParams) -> str:
    """MODE A's economic constraints on one candidate ('' = a qualifying profit lock)."""
    if not c.feasible:
        return c.why
    if c.incr_pair_cost is None:
        return "no pair"
    if c.incr_pair_cost > prm.profit_lock_max_cost + 1e-9:
        return f"pair cost {c.incr_pair_cost:.3f} > PROFIT_LOCK_MAX_COST {prm.profit_lock_max_cost:.3f}"
    if c.incr_pair_edge < prm.profit_lock_min_edge - 1e-9:
        return f"pair edge {c.incr_pair_edge:+.3f} < PROFIT_LOCK_MIN_EDGE {prm.profit_lock_min_edge:.3f}"
    if c.q * c.incr_pair_edge < prm.min_locked_profit - 1e-9:
        return f"locked profit {c.q * c.incr_pair_edge:+.3f} < MIN_LOCKED_PROFIT {prm.min_locked_profit:.3f}"
    return ""


def _record(kind: str, cyc: Cycle, p_up: float, cands: list[HedgeCandidate], whys: dict, ra: float, obs: dict,
            now: float) -> dict:
    b = cyc.book
    return {"t": now, "kind": kind, "p_up": p_up, "state": cyc.state.value,
            "before": {"expected": round(b.expected_settlement_pnl(p_up), 4), "worst": round(b.worst_case_pnl, 4),
                       "up_qty": b.up_qty, "down_qty": b.down_qty, "matched": b.matched_qty, "unmatched": b.unmatched,
                       "total_cost": round(b.total_cost, 4)},
            "risk_aversion": ra, "position": _pos(obs) if obs else {},
            "candidates": [{**c.brief(), "utility": round(c.utility(ra), 4), "rejected": whys.get(id(c), "")}
                           for c in cands]}


def manage_position(cyc: Cycle, feat: dict | None, mv: MarketView, now: float, left: float, model: ProbModel,
                    prm: PairLockParams, lim: RiskLimits, mkt: MarketExposure) -> tuple[Decision | None, str, dict | None]:
    """One sample of an open position: observe (adverse move, reversal), then -- per ``hedge_mode`` -- MODE B
    (recovery hedge, only from REVERSAL_CONFIRMED) or MODE A (profit lock, only while NOT materially losing).
    Returns (decision or None, why not, the evaluation record or None). Never hedges because of a loss alone."""
    obs = observe(cyc, feat, mv, now, left, model, prm, lim)
    if not obs or cyc.state not in WATCH_STATES:
        return None, f"state {cyc.state.value}", None
    if prm.hedge_mode == "none" or feat is None:
        return None, "no hedging (strategy A)" if prm.hedge_mode == "none" else "features not ready", None
    heavy, light = obs["original"], obs["opposite"]
    p_up = model.p_up(feat)
    if cyc.state is State.REVERSAL_CONFIRMED:
        if prm.hedge_mode not in ("recovery", "combined"):
            return None, "recovery hedging off", None
        why = _gates(cyc, now, left, prm.recovery_min_time_left_s, lim, prm, mv, light, obs["fresh"], obs["spread_ok"])
        if why:
            return None, why, None
        if cyc.last_eval_rejected and cyc.last_eval_episode == cyc.episode and now - cyc.last_eval_t < prm.reeval_ms:
            return None, "re-evaluation interval after a rejected recovery", None
        cap_left = prm.recovery_max_hedge_fraction * max(cyc.entry_qty, EPS) - cyc.recovery_qty
        base = min(cyc.book.unmatched, max(0.0, cap_left))
        if base < prm.min_size - 1e-9:
            return None, f"RECOVERY_MAX_HEDGE_FRACTION reached ({cyc.recovery_qty:g} of {cyc.entry_qty:g} hedged)", None
        cands = _cands(cyc, p_up, mv, light, prm, lim, mkt, base)
        b = cyc.book
        h_fee_ps = (b.up_fees if heavy == "UP" else b.down_fees) / max(b.qty(heavy), EPS)
        cap_price = max_price_for_pair_cost(prm.max_acceptable_pair_cost, b.avg_cost(heavy), h_fee_ps)
        whys, ok = {}, []
        for c in cands:
            if c.q <= EPS:
                continue
            w = recovery_constraints(c, prm)
            lim_px = None
            if not w:
                lim_px, w = _limit_for(c, mv.asks(light), prm.depth_haircut, prm.hedge_offset, cap_price)
            whys[id(c)] = w
            if not w:
                ok.append((c, lim_px))
        cyc.to(State.RECOVERY_EVALUATION, now, f"episode {cyc.episode}: {len(ok)} of {len(whys)} candidates acceptable")
        cyc.last_eval_t, cyc.last_eval_episode = now, cyc.episode
        rec = _record("recovery", cyc, p_up, cands, whys, prm.risk_aversion, obs, now)
        rec["acceptable"] = len(ok)
        u0 = b.expected_settlement_pnl(p_up)
        best = max(ok, key=lambda x: x[0].utility(prm.risk_aversion), default=None)
        cyc.note("RECOVERY_HEDGE_EVALUATION", now, f"{len(ok)} acceptable of {len(whys)}; utility before {u0:+.4f}",
                 candidates=rec["candidates"], before=rec["before"])
        if best is None or best[0].utility(prm.risk_aversion) <= u0 + 1e-9:
            why = ("no acceptable candidate: " + "; ".join(sorted({w for w in whys.values() if w}))) if best is None else \
                (f"best utility {best[0].utility(prm.risk_aversion):+.4f} ({best[0].q:g} sh) <= utility without a hedge "
                 f"{u0:+.4f}")
            cyc.last_eval_rejected = True
            cyc.note("RECOVERY_HEDGE_REJECTED", now, why)
            _move(cyc, watch_state(cyc, prm), now, "recovery rejected")
            rec["decision"], rec["why"] = "REJECTED", why
            return None, why, rec
        c, lim_px = best
        cyc.last_eval_rejected = False
        info = _hedge_info(c, prm.risk_aversion, u0, obs, cap_price)
        rec["decision"] = c.label
        reason = (f"recovery: {c.q:g} sh of {light} at VWAP {c.vwap:.3f} (+{prm.slippage_allowance:.3f} slip), pair cost "
                  f"{c.incr_pair_cost:.3f}, worst {c.worst_case_pnl_before:+.3f} -> {c.new_worst_case_pnl:+.3f}, expected "
                  f"{u0:+.3f} -> {c.new_expected_pnl:+.3f}, utility {c.utility(prm.risk_aversion):+.3f}")
        cyc.note("RECOVERY_HEDGE_ACCEPTED", now, reason, **info)
        return (Decision("hedge", light, c.q, lim_px, mv.ask(light), reason=reason,
                         ev=c.utility(prm.risk_aversion) - u0, p=(1.0 - p_up) if light == "DOWN" else p_up,
                         kind="recovery", candidate=c, info=info), "", rec)
    if cyc.state in (State.DIRECTIONAL_POSITION, State.UNMATCHED_DIRECTIONAL):
        if prm.hedge_mode not in ("profit_lock", "combined"):
            return None, "profit locking off", None
        why = _gates(cyc, now, left, prm.profit_lock_min_time_left_s, lim, prm, mv, light, obs["fresh"], obs["spread_ok"])
        if why:
            return None, why, None
        if cyc.last_eval_rejected and now - cyc.last_eval_t < prm.reeval_ms:
            return None, "re-evaluation interval after a rejected profit lock", None
        cands = _cands(cyc, p_up, mv, light, prm, lim, mkt, None)
        b = cyc.book
        h_fee_ps = (b.up_fees if heavy == "UP" else b.down_fees) / max(b.qty(heavy), EPS)
        cap = min(prm.profit_lock_max_cost, 1.0 - prm.profit_lock_min_edge)
        cap_price = max_price_for_pair_cost(cap, b.avg_cost(heavy), h_fee_ps)
        whys, ok = {}, []
        for c in cands:
            if c.q <= EPS:
                continue
            w = profit_lock_constraints(c, prm)
            lim_px = None
            if not w:
                lim_px, w = _limit_for(c, mv.asks(light), prm.depth_haircut, prm.hedge_offset, cap_price)
            whys[id(c)] = w
            if not w:
                ok.append((c, lim_px))
        if not ok:
            return None, "no profit-lock opportunity", None          # not an opportunity: no evaluation, no state change
        cyc.to(State.PROFIT_LOCK_EVALUATION, now, f"{len(ok)} of {len(whys)} candidates qualify")
        cyc.last_eval_t = now
        rec = _record("profit_lock", cyc, p_up, cands, whys, prm.profit_lock_risk_aversion, obs, now)
        rec["acceptable"] = len(ok)
        u0 = b.expected_settlement_pnl(p_up)
        best = max(ok, key=lambda x: x[0].utility(prm.profit_lock_risk_aversion))
        c, lim_px = best
        cyc.note("PROFIT_LOCK_EVALUATION", now, f"{len(ok)} qualify; locked profit {c.q * c.incr_pair_edge:+.3f} on "
                 f"{c.q:g} pairs; expected hold {u0:+.3f} vs lock {c.new_expected_pnl:+.3f}; worst "
                 f"{c.worst_case_pnl_before:+.3f} -> {c.new_worst_case_pnl:+.3f}; capital {c.incremental_capital:.2f}; "
                 f"unmatched after {c.unmatched_after:g}", candidates=rec["candidates"], before=rec["before"])
        if c.utility(prm.profit_lock_risk_aversion) <= u0 + 1e-9:
            why = (f"best utility {c.utility(prm.profit_lock_risk_aversion):+.4f} ({c.q:g} sh) <= holding {u0:+.4f}")
            cyc.last_eval_rejected = True
            cyc.note("PROFIT_LOCK_REJECTED", now, why)
            _move(cyc, watch_state(cyc, prm), now, "profit lock rejected")
            rec["decision"], rec["why"] = "REJECTED", why
            return None, why, rec
        cyc.last_eval_rejected = False
        info = _hedge_info(c, prm.profit_lock_risk_aversion, u0, obs, cap_price)
        info["locked_profit"] = c.q * c.incr_pair_edge
        rec["decision"] = c.label
        reason = (f"profit lock: {c.q:g} sh of {light} at VWAP {c.vwap:.3f}, pair cost {c.incr_pair_cost:.3f}, locks "
                  f"{c.q * c.incr_pair_edge:+.3f} on {c.q:g} pairs, unmatched after {c.unmatched_after:g}")
        cyc.note("PROFIT_LOCK_ACCEPTED", now, reason, **info)
        return (Decision("hedge", light, c.q, lim_px, mv.ask(light), reason=reason,
                         ev=c.utility(prm.profit_lock_risk_aversion) - u0, p=(1.0 - p_up) if light == "DOWN" else p_up,
                         kind="profit_lock", candidate=c, info=info), "", rec)
    return None, f"watching ({cyc.state.value})", None


def _hedge_info(c: HedgeCandidate, ra: float, u0: float, obs: dict, cap_price: float) -> dict:
    return {"q": c.q, "vwap": c.vwap, "price": c.price, "pair_cost": c.incr_pair_cost, "pair_edge": c.incr_pair_edge,
            "pair_cost_after": c.new_pair_cost, "expected_before": u0, "expected_after": c.new_expected_pnl,
            "worst_before": c.worst_case_pnl_before, "worst_after": c.new_worst_case_pnl,
            "downside_reduction": c.downside_reduction, "utility": c.utility(ra), "risk_aversion": ra,
            "capital": c.incremental_capital, "unmatched_after": c.unmatched_after, "max_price": cap_price,
            "score": obs.get("score"), "confirmations": obs.get("confirmations")}


def _last_level(levels, q: float, haircut: float) -> float:
    need = q
    last = None
    for p, s in sorted(levels, key=lambda x: x[0]):
        avail = s * (1.0 - haircut)
        if avail <= EPS:
            continue
        last = p
        need -= avail
        if need <= 1e-9:
            break
    return last if last is not None else 0.99


# ───────────────────────────────── order lifecycle ─────────────────────────────────


def on_entry_sent(cyc: Cycle, order_id: str, qty: float, limit: float, now: float) -> None:
    cyc.book.add_pending(PendingOrder(order_id, cyc.a_side, qty, limit, "entry", now))
    cyc.to(State.INITIAL_ENTRY, now, f"FOK {qty:g} {cyc.a_side} <= {limit:.2f}")
    cyc.note("INITIAL_ENTRY", now, f"FOK {qty:g} {cyc.a_side} <= {limit:.2f}", **cyc.entry_info)


def on_hedge_sent(cyc: Cycle, order_id: str, side: str, qty: float, limit: float, now: float, kind: str,
                  info: dict | None = None) -> None:
    """The decided hedge order leaves: RECOVERY_HEDGE_PENDING / PROFIT_LOCK_PENDING. Pending is NOT inventory."""
    cyc.book.add_pending(PendingOrder(order_id, side, qty, limit, kind, now))
    cyc.hedge_actions += 1
    cyc.last_action_t = now
    b = cyc.book
    cyc.pending_hedge = {"oid": order_id, "kind": kind, "q": qty, "limit": limit, "t": now, "filled": 0.0,
                         "worst_before": b.worst_case_pnl, "heavy": b.heavy_side, "info": dict(info or {})}
    if kind == "recovery":
        cyc.to(State.RECOVERY_HEDGE_PENDING, now, f"FOK {qty:g} {side} <= {limit:.2f}")
        cyc.note("RECOVERY_HEDGE_SENT", now, f"FOK {qty:g} {side} <= {limit:.2f}")
    else:
        cyc.to(State.PROFIT_LOCK_PENDING, now, f"FOK {qty:g} {side} <= {limit:.2f}")
        cyc.note("PROFIT_LOCK_SENT", now, f"FOK {qty:g} {side} <= {limit:.2f}")


def on_fill(cyc: Cycle, fill: FillRecord, prm: PairLockParams, now: float) -> None:
    """A fill of the cycle's order. Inventory and every derived figure update at once; a hedge partially filled
    counts exactly what filled."""
    b = cyc.book
    had_both = b.up_qty > EPS and b.down_qty > EPS
    if fill.kind != "entry":
        heavy = b.heavy_side
        h_avg = b.avg_cost(heavy) if heavy else None
        h_fee_ps = ((b.up_fees if heavy == "UP" else b.down_fees) / max(b.qty(heavy), EPS)) if heavy else 0.0
    b.add_fill(fill)
    if fill.kind == "entry":
        cyc.entry_qty += fill.qty
        if cyc.state is State.INITIAL_ENTRY:
            cyc.to(State.DIRECTIONAL_POSITION, now, f"entry filled {fill.qty:g} @ {fill.price:.3f}")
        dec_t = cyc.entry_info.get("decision_t")
        cyc.note("INITIAL_ENTRY_FILLED", now, f"{fill.qty:g} {fill.side} @ {fill.price:.4f}", token=fill.token,
                 qty=fill.qty, price=fill.price, expected=fill.expected_price, fee=fill.fee, slippage=fill.slippage,
                 order_id=fill.order_id, decision_to_fill_ms=None if dec_t is None else round(now - dec_t, 1))
        return
    cyc.hedge_fills += 1
    edge = None if h_avg is None else 1.0 - (h_avg + h_fee_ps + fill.price + fill.fee / fill.qty)
    if fill.kind == "recovery":
        cyc.recovery_qty += fill.qty
        if edge is not None:
            cyc.recovery_edges.append([fill.qty, edge])
    else:
        cyc.profit_lock_qty += fill.qty
        if edge is not None:
            cyc.profit_lock_locked += fill.qty * edge
    if cyc.pending_hedge is not None:
        cyc.pending_hedge["filled"] = cyc.pending_hedge.get("filled", 0.0) + fill.qty
    name = "RECOVERY_HEDGE_FILLED" if fill.kind == "recovery" else "PROFIT_LOCK_FILLED"
    cyc.note(name, now, f"{fill.qty:g} {fill.side} @ {fill.price:.4f} (expected {fill.expected_price}), paired edge "
             f"{_sg(edge)}", qty=fill.qty, price=fill.price, fee=fill.fee, slippage=fill.slippage, order_id=fill.order_id,
             pair_edge=edge)
    if not had_both and b.up_qty > EPS and b.down_qty > EPS:
        cyc.pair_formed_t = now
        cyc.note("PAIR_FORMED", now, f"{b.matched_qty:g} matched pairs, {b.unmatched:g} unmatched "
                 f"{b.heavy_side or ''}".strip())


def on_order_done(cyc: Cycle, order_id: str, filled_any: bool, now: float, why: str = "",
                  prm: PairLockParams | None = None) -> None:
    """The order's final result. Entry: unfilled -> WAITING. Hedge: the inventory decides (a partial fill counts
    exactly what filled; nothing filled = an incomplete hedge, the exposure is as it was)."""
    prm = prm or PairLockParams()
    p = cyc.book.pending.pop(order_id, None)
    if p is None:
        return
    if p.kind == "entry":
        if not filled_any and cyc.state is State.INITIAL_ENTRY:
            cyc.to(State.WAITING, now, f"entry not filled ({why})")
            cyc.note("INITIAL_ENTRY_NOT_FILLED", now, why)
        return
    ph = cyc.pending_hedge or {}
    cyc.pending_hedge = None
    kind = p.kind
    pending_state = State.RECOVERY_HEDGE_PENDING if kind == "recovery" else State.PROFIT_LOCK_PENDING
    filled = float(ph.get("filled", 0.0))
    if not filled_any or filled <= EPS:
        cyc.hedge_kills += 1
        cyc.note("RECOVERY_HEDGE_NOT_FILLED" if kind == "recovery" else "PROFIT_LOCK_NOT_FILLED", now,
                 f"{why}: nothing filled, {cyc.book.unmatched:g} still unmatched")
        if cyc.state is pending_state:
            _move(cyc, watch_state(cyc, prm), now, f"hedge not filled ({why})")
        return
    if filled < float(ph.get("q", 0.0)) - 1e-9:
        cyc.note("PARTIAL_HEDGE", now, f"{filled:g} of {ph.get('q'):g} filled: inventory +{filled:g}, matched "
                 f"{cyc.book.matched_qty:g}, unmatched {cyc.book.unmatched:g}")
    if kind == "recovery":
        cyc.recovery_downside += cyc.book.worst_case_pnl - float(ph.get("worst_before", cyc.book.worst_case_pnl))
    if cyc.state is not pending_state:
        return                                                    # e.g. the market closed while it was in flight
    tgt = watch_state(cyc, prm)
    if kind == "recovery":
        cyc.to(State.RECOVERY_HEDGED, now, f"{filled:g} filled; worst case {ph.get('worst_before', 0.0):+.3f} -> "
               f"{cyc.book.worst_case_pnl:+.3f}")
        if tgt is not State.RECOVERY_HEDGED:
            cyc.to(State.UNMATCHED_DIRECTIONAL if tgt in WATCH_STATES else tgt, now, _inv(cyc))
    else:
        cyc.to(State.UNMATCHED_DIRECTIONAL if tgt in WATCH_STATES else tgt, now, _inv(cyc))
    if cyc.state is State.PAIR_LOCKED:
        cyc.locked_t = now
        cyc.note("PAIR_LOCKED", now, _inv(cyc))


def _inv(cyc: Cycle) -> str:
    b = cyc.book
    rem = f", {b.unmatched:g} unmatched {b.heavy_side} DIRECTIONAL (not locked)" if b.unmatched > 1e-6 else ""
    return (f"{b.matched_qty:g} matched pairs, pair cost {_f(b.pair_cost_per_share)}, locked "
            f"{b.locked_pair_pnl:+.3f}{rem}")


def on_close(cyc: Cycle, now: float, prm: PairLockParams | None = None) -> bool:
    """The market closed. A cycle holding shares waits for the venue's outcome (SETTLEMENT_PENDING); one that
    holds nothing is dropped (returns False)."""
    if not cyc.holds:
        return False
    if cyc.state not in (State.SETTLEMENT_PENDING, State.MARKET_RESOLVED, State.SETTLED):
        if cyc.state in (State.RECOVERY_EVALUATION, State.PROFIT_LOCK_EVALUATION):
            _move(cyc, watch_state(cyc, prm or PairLockParams()), now, "market closed")
        cyc.to(State.SETTLEMENT_PENDING, now, "market closed: " + _inv(cyc))
    return True


def on_settled(cyc: Cycle, winner: str, now: float) -> dict:
    """The venue's outcome is booked: MARKET_RESOLVED, then SETTLED with the reconciliation figures (the book's
    P&L, the cash view, and an independent reconstruction from the raw fills)."""
    cyc.settled_winner = winner
    if cyc.state is not State.SETTLEMENT_PENDING:
        on_close(cyc, now)
    cyc.to(State.MARKET_RESOLVED, now, f"{winner} won")
    cyc.note("MARKET_RESOLVED", now, f"{winner} won")
    b = cyc.book
    r = {"winner": winner, "pnl": b.resolution_pnl(winner), "pnl_from_cash": b.final_pnl_from_cash(winner),
         "pnl_reconstructed": reconstruct_pnl(b.fills, winner), **cyc.attribution(winner)}
    cyc.to(State.SETTLED, now, f"P&L {r['pnl']:+.4f}")
    cyc.note("PAIR_SETTLED" if b.matched_qty > EPS else "SETTLED", now,
             f"{winner} won: P&L {r['pnl']:+.4f} (cash {r['pnl_from_cash']:+.4f}, rebuilt {r['pnl_reconstructed']:+.4f}); "
             f"no-hedge baseline {r['prediction']:+.4f}, recovery leg {r['recovery_leg']:+.4f}, profit-lock leg "
             f"{r['profit_lock_leg']:+.4f}; pairs {r['pairs']:+.4f}, directional {r['directional']:+.4f}",
             **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()})
    return r


def reconstruct_pnl(fills, winner: str) -> float:
    """Final P&L rebuilt from the raw fills alone (no PairBook): every share of the winner pays 1, every buy costs
    qty x price + fee, every sell returns qty x price - fee."""
    cash = sum(-(f.qty * f.price + f.fee) if f.action == "BUY" else (f.qty * f.price - f.fee) for f in fills)
    held = sum(f.qty * (1 if f.action == "BUY" else -1) for f in fills if f.side == winner)
    return held + cash
