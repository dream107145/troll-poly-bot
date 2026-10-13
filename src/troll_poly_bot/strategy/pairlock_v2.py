"""Conditional dynamic hedge / pair-lock (v2, 2026-10-02 brief): features, the probability models and the
decisions, shared by the live engine (strategy/pairlock_engine.py) and the replay (scripts/pairlock/replay.py).
The accounting, the states and the hedge optimizer live in strategy/pairlock_book.py.

Facts the design rests on (binary market, winner pays 1):
* Buying the opposite token B matches against A's bids, so B's ask ~ 1 - A's bid. After an ADVERSE move of A the
  pair cost is avg_A + ask_B ~ 1 + (avg_A - bid_A) > 1: a hedge after a loss can only lock that loss in. A pair cost
  under 1 exists only after a FAVOURABLE move of A (Edge B is only available when Edge A already paid).
* hedge_EV = E[P&L | hedge] - E[P&L | no hedge] = q * (P(B) - vwap_B - fee_B) * P(fill): B as a standalone bet. The
  loss carried on A is sunk and never enters the decision (no martingale, no "recover the loss").

Two edges, measured separately (scripts/pairlock/replay.py): Edge A = the first leg's own P&L; Edge B = what the
opposite leg added (hedging P&L) and what the matched pairs earned (pair P&L).
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass, field

from .pairlock_book_v2 import (CLOSED_STATES, HEDGE_WATCH_STATES, EPS, FillRecord, HedgeCandidate, PairBook,
                            PendingOrder, State, best_hedge, check_transition, evaluate_hedges, fee_per_share,
                            pair_lock_check)

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
    # ---- entry (Edge A)
    entry_edge: float = 0.02          # min settlement EV per share after fee, slippage and adverse selection
    entry_mode: str = "settlement"    # "settlement" | "both": also P(favourable within N s) >= short_min
    short_min: float = 0.5
    entry_shares: float = 10.0
    min_size: float = 5.0
    band_lo: float = 0.20
    band_hi: float = 0.96
    entry_offset: float = 0.03        # FOK limit = ask + offset
    min_left_entry_s: float = 60.0
    max_cycles_per_market: int = 2
    # ---- adverse move / reversal
    adverse_min: float = 0.03         # the heavy side's bid this far below its average cost = ADVERSE_MOVE
    rev_thr: float = 1.0              # reversal score threshold (standardised units)
    confirm_ms: float = 400.0         # the score must stay >= rev_thr this long
    # ---- hedging (Edge B)
    hedge_mode: str = "conditional"   # "none" (A) | "unconditional" (B) | "conditional" (C)
    unconditional_trigger: float = 0.05
    fractions: tuple[float, ...] = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
    min_hedge_ev: float = 0.0         # MIN_HEDGE_EV: hedge_EV (USDC) must exceed this
    lock_opportunities: bool = True   # C: evaluate when a qualifying lock exists, without a reversal
    min_pair_edge: float = 0.02       # MIN_PAIR_EDGE: net pair edge per matched share for PAIR_LOCKED
    min_locked_profit: float = 0.05   # MIN_LOCKED_PROFIT (USDC) for PAIR_LOCKED
    hedge_offset: float = 0.03        # hedge FOK limit = the VWAP's last level + offset
    hedge_cooldown_ms: float = 1000.0
    reeval_ms: float = 5000.0         # after a NO_HEDGE, the next evaluation waits this long (unless newly confirmed)
    min_left_hedge_s: float = 10.0
    depth_haircut: float = 0.0        # share of each ask level assumed taken by faster traders


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
    max_pair_cost: float = 1.15           # MAX_PAIR_COST: no hedge may leave a matched pair dearer than this
    max_spread: float = 0.05              # MAX_SPREAD of the traded token
    max_quote_age_ms: float = 2_000.0     # MAX_QUOTE_AGE (live)
    max_spot_age_ms: float = 1_000.0      # MAX_SPOT_AGE (live)
    min_time_remaining_s: float = 10.0    # MIN_TIME_REMAINING: nothing at all below this


@dataclass
class MarketExposure:
    """What one market already holds or has in flight, all cycles (the risk checks read it)."""
    capital: float = 0.0
    shares: float = 0.0
    worst_case: float = 0.0
    cycles: int = 0


# ───────────────────────────────── the cycle ─────────────────────────────────


@dataclass
class Transition:
    t: float
    frm: str
    to: str
    reason: str


@dataclass
class Cycle:
    """One position in one market, from the first signal to settlement. The PairBook (fills) is authoritative;
    the state changes only through validated transitions (pairlock_book.TRANSITIONS)."""
    book: PairBook
    a_side: str
    state: State = State.NO_POSITION
    started_t: float = 0.0
    hedge_actions: int = 0
    hedge_fills: int = 0
    hedge_kills: int = 0
    last_action_t: float = -1e18
    last_eval_t: float = -1e18
    last_eval_rejected: bool = False
    last_eval_confirmed: bool = False
    rev_since: float | None = None
    adverse_seen: bool = False
    adverse_now: bool = False
    locked_t: float | None = None
    settled_winner: str | None = None
    transitions: list[Transition] = field(default_factory=list)

    @property
    def b_side(self) -> str:
        return "DOWN" if self.a_side == "UP" else "UP"

    def to(self, new: State, t: float, reason: str = "") -> None:
        check_transition(self.state, new)
        if new is not self.state:
            self.transitions.append(Transition(t, self.state.value, new.value, reason))
            self.state = new

    def watch_state(self) -> State:
        """Where the cycle watches from after a NO_HEDGE decision or a killed hedge: the inventory decides."""
        if self.book.up_qty > EPS and self.book.down_qty > EPS:
            return State.PAIR_FORMED
        return State.ADVERSE_MOVE if self.adverse_now else State.MONITORING

    @property
    def closed(self) -> bool:
        """Directional exposure resolved: locked, settled, or a fully matched (flat) pair with nothing pending."""
        if self.state in CLOSED_STATES:
            return True
        b = self.book
        return (self.state is State.PAIR_FORMED and b.unmatched <= EPS and not b.pending and b.matched_qty > EPS)

    def attribution(self, winner: str) -> dict:
        """Settled P&L split two ways: by leg (Edge A = the first side, the prediction; hedging = the opposite side)
        and by exposure (matched pairs vs the directional remainder). Both sum to the total."""
        b = self.book
        a, o = self.a_side, self.b_side
        leg_a = b.qty(a) * (1.0 if winner == a else 0.0) - b.total_cost_side(a)
        leg_b = b.qty(o) * (1.0 if winner == o else 0.0) - b.total_cost_side(o)
        pairs = b.locked_pair_pnl
        total = leg_a + leg_b + b.realised_exit_pnl
        return {"total": total, "prediction": leg_a, "hedging": leg_b + b.realised_exit_pnl, "pairs": pairs,
                "directional": total - pairs}

    def to_json(self) -> dict:
        return {"book": self.book.to_json(), "a_side": self.a_side, "state": self.state.value,
                "started_t": self.started_t, "hedge_actions": self.hedge_actions, "hedge_fills": self.hedge_fills,
                "hedge_kills": self.hedge_kills, "last_action_t": self.last_action_t, "adverse_seen": self.adverse_seen,
                "locked_t": self.locked_t, "transitions": [asdict(x) for x in self.transitions[-50:]]}

    @classmethod
    def from_json(cls, d: dict) -> "Cycle":
        c = cls(book=PairBook.from_json(d["book"]), a_side=d["a_side"], state=State(d.get("state", "MONITORING")),
                started_t=float(d.get("started_t") or 0.0), hedge_actions=int(d.get("hedge_actions") or 0),
                hedge_fills=int(d.get("hedge_fills") or 0), hedge_kills=int(d.get("hedge_kills") or 0),
                last_action_t=float(d.get("last_action_t") or -1e18), adverse_seen=bool(d.get("adverse_seen")),
                locked_t=d.get("locked_t"),
                transitions=[Transition(**x) for x in d.get("transitions") or []])
        # an order in flight when the bot stopped resolved at the venue while it was down: a restart never
        # resumes a pending order (the paper venue forgot it); the inventory decides where to watch from
        if c.book.pending:
            c.book.pending.clear()
        if c.state in (State.INITIAL_ENTRY_CANDIDATE, State.INITIAL_ENTRY, State.HEDGE_EVALUATION, State.NO_HEDGE,
                       State.PARTIAL_HEDGE, State.FULL_HEDGE):
            c.state = State.NO_POSITION if c.book.up_qty + c.book.down_qty <= EPS else c.watch_state()
        return c


# ───────────────────────────────── decisions ─────────────────────────────────


@dataclass
class Decision:
    action: str                   # "entry" | "hedge"
    side: str
    shares: float
    limit: float
    ask: float
    reason: str = ""
    ev: float = 0.0               # entry: EV per share; hedge: hedge_EV (USDC)
    p: float = 0.5
    kind: str = ""                # hedge: PARTIAL_HEDGE | FULL_HEDGE (+ "lock" / "reversal" / "unconditional")
    candidate: HedgeCandidate | None = None
    info: dict = field(default_factory=dict)


def entry_decision(feat: dict, side: str, ask: float | None, bid: float | None, ask_levels: list, model: ProbModel,
                   prm: PairLockParams, lim: RiskLimits, mkt: MarketExposure, left: float,
                   prev: Cycle | None) -> tuple[Decision | None, str]:
    """Enter only if the expected NET value per share clears ``entry_edge``:
        EV = P(side wins) - expected fill - fee - expected slippage - adverse-selection cost (TRAIN),
    the expected fill being the executable VWAP of the order's size on the ask levels (not the mid, not the last
    trade, not a fair value). ``entry_mode="both"`` also requires P(favourable within N s) >= short_min."""
    if prev is not None and not prev.closed:
        return None, f"the previous cycle is still open ({prev.state.value})"
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
    from .pairlock_book_v2 import vwap_buy
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


def hedge_step(cyc: Cycle, feat: dict, bid_up: float | None, bid_down: float | None, ask_b: float | None,
               bid_b: float | None, ask_levels_b: list, now: float, left: float, model: ProbModel,
               prm: PairLockParams, lim: RiskLimits, mkt: MarketExposure) -> tuple[Decision | None, str, dict | None]:
    """Advance the cycle from this sample (MONITORING <-> ADVERSE_MOVE, the reversal clock) and, when the strategy
    calls for it, run a HEDGE_EVALUATION: (decision or None, why, the evaluation record or None)."""
    b = cyc.book
    if cyc.state not in HEDGE_WATCH_STATES:
        return None, f"state {cyc.state.value}", None
    heavy = b.heavy_side
    if heavy is None:
        return None, "fully matched", None
    light = "DOWN" if heavy == "UP" else "UP"
    avg = b.avg_cost(heavy)
    bid_h = bid_up if heavy == "UP" else bid_down
    adverse = bid_h is not None and avg is not None and bid_h <= avg - prm.adverse_min + 1e-9
    cyc.adverse_now = adverse
    if adverse:
        cyc.adverse_seen = True
        if cyc.state is State.MONITORING:
            cyc.to(State.ADVERSE_MOVE, now, f"{heavy} bid {bid_h:.2f} <= avg {avg:.3f} - {prm.adverse_min:.2f}")
    elif cyc.state is State.ADVERSE_MOVE:
        cyc.to(State.MONITORING, now, f"{heavy} bid {bid_h} recovered")
    score = reversal_score(feat, heavy, model.rev_scales)
    if score >= prm.rev_thr:
        cyc.rev_since = now if cyc.rev_since is None else cyc.rev_since
    else:
        cyc.rev_since = None
    if prm.hedge_mode == "none":
        return None, "strategy A: no hedging", None
    if cyc.hedge_actions >= lim.max_hedge_actions:
        return None, "hedge actions exhausted", None
    if now - cyc.last_action_t < prm.hedge_cooldown_ms or now - cyc.last_eval_t < prm.hedge_cooldown_ms:
        return None, "cooldown", None
    if left < max(prm.min_left_hedge_s, lim.min_time_remaining_s):
        return None, f"{left:.0f} s left", None
    if b.unmatched < prm.min_size - 1e-9:
        return None, "unmatched under the venue minimum", None
    if ask_b is None:
        return None, f"no {light} ask", None
    if bid_b is None or ask_b - bid_b > lim.max_spread + 1e-9:
        return None, f"{light} spread", None
    p_up = model.p_up(feat)
    hedge_used = sum(f.qty for f in b.fills if f.kind == "hedge" and f.action == "BUY")
    cands = evaluate_hedges(
        b, p_up, ask_levels_b, fractions=prm.fractions, min_size=prm.min_size, haircut=prm.depth_haircut,
        exec_prob=float(model.exec.get("hedge_fill_rate", 1.0)), max_market_capital=lim.max_market_capital,
        capital_used=mkt.capital, max_total_position=lim.max_total_position, shares_used=mkt.shares,
        max_hedge_position=lim.max_hedge_position, hedge_shares_used=hedge_used, max_market_loss=lim.max_market_loss,
        other_worst=mkt.worst_case - b.worst_case_pnl, max_pair_cost=lim.max_pair_cost)
    confirmed = adverse and cyc.rev_since is not None and now - cyc.rev_since >= prm.confirm_ms - 1e-9
    if (cyc.last_eval_rejected and now - cyc.last_eval_t < prm.reeval_ms
            and not (confirmed and not cyc.last_eval_confirmed)):
        return None, "re-evaluation interval after a NO_HEDGE", None
    lockable = [c for c in cands if c.feasible and c.q > EPS and c.new_unmatched_up + c.new_unmatched_down <= EPS
                and (c.new_net_pair_edge or -1.0) >= prm.min_pair_edge - 1e-12
                and c.new_locked_pair_pnl >= prm.min_locked_profit - 1e-12]
    record = {"t": now, "p_up": p_up, "score": score, "adverse": adverse, "confirmed": confirmed,
              "lockable": len(lockable),
              "candidates": [{"label": c.label, "q": c.q, "vwap": c.vwap, "hedge_ev": round(c.hedge_ev, 5),
                              "feasible": c.feasible, "why": c.why, "pair_cost": c.new_pair_cost,
                              "worst": round(c.new_worst_case_pnl, 4), "locked": round(c.new_locked_pair_pnl, 4)}
                             for c in cands]}
    if prm.hedge_mode == "unconditional":
        if bid_h is None or avg is None or bid_h > avg - prm.unconditional_trigger + 1e-9:
            return None, "no adverse move (unconditional trigger)", None
        feas = [c for c in cands if c.feasible and c.q > EPS]
        cyc.to(State.HEDGE_EVALUATION, now, "unconditional: adverse move")
        cyc.last_eval_t = now
        if not feas:
            cyc.to(State.NO_HEDGE, now, "no feasible size")
            cyc.to(cyc.watch_state(), now, "")
            cyc.last_eval_rejected, cyc.last_eval_confirmed = True, confirmed
            return None, "no feasible size", record
        best, kind = max(feas, key=lambda c: c.q), "unconditional"
    else:
        if not confirmed and not (prm.lock_opportunities and lockable):
            return None, "no confirmed reversal and no qualifying lock", None
        cyc.to(State.HEDGE_EVALUATION, now, "confirmed reversal" if confirmed else "lock opportunity")
        cyc.last_eval_t = now
        pool = cands if confirmed else lockable + [c for c in cands if c.q <= EPS]
        best, why = best_hedge(pool, prm.min_hedge_ev)
        if best is None:
            cyc.to(State.NO_HEDGE, now, why)
            cyc.to(cyc.watch_state(), now, "")
            cyc.last_eval_rejected, cyc.last_eval_confirmed = True, confirmed
            record["decision"] = "NO_HEDGE"
            record["why"] = why
            return None, why, record
        kind = "reversal" if confirmed else "lock"
    cyc.last_eval_rejected, cyc.last_eval_confirmed = False, confirmed
    label = best.label
    cyc.to(State[label], now, f"{kind}: {best.q:g} sh of {light} at VWAP {best.vwap:.3f}, hedge_EV {best.hedge_ev:+.4f}")
    limit = min(0.99, round(_last_level(ask_levels_b, best.q, prm.depth_haircut) + prm.hedge_offset, 2))
    record["decision"] = label
    info = {"p_b": 1.0 - p_up if light == "DOWN" else p_up, "score": score, "adverse": adverse, "confirmed": confirmed,
            "q": best.q, "vwap": best.vwap, "hedge_ev": best.hedge_ev, "pair_cost_after": best.new_pair_cost,
            "net_pair_edge_after": best.new_net_pair_edge, "locked_after": best.new_locked_pair_pnl,
            "worst_after": best.new_worst_case_pnl, "expected_after": best.new_expected_pnl,
            "expected_without": best.expected_pnl_without, "capital": best.incremental_capital,
            "exec_prob": best.execution_probability}
    return (Decision("hedge", light, best.q, limit, ask_b, ev=best.hedge_ev, p=info["p_b"], kind=f"{label}:{kind}",
                     candidate=best, info=info), "", record)


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


def on_hedge_sent(cyc: Cycle, order_id: str, side: str, qty: float, limit: float, now: float) -> None:
    cyc.book.add_pending(PendingOrder(order_id, side, qty, limit, "hedge", now))
    cyc.hedge_actions += 1
    cyc.last_action_t = now


def on_fill(cyc: Cycle, fill: FillRecord, prm: PairLockParams, now: float) -> None:
    """A fill of the cycle's order (entry or hedge). Inventory and every derived figure update at once."""
    cyc.book.add_fill(fill)
    if fill.kind == "entry":
        if cyc.state is State.INITIAL_ENTRY:
            cyc.to(State.MONITORING, now, f"entry filled {fill.qty:g} @ {fill.price:.3f}")
        return
    cyc.hedge_fills += 1
    if cyc.state in (State.PARTIAL_HEDGE, State.FULL_HEDGE) and cyc.book.up_qty > EPS and cyc.book.down_qty > EPS:
        cyc.to(State.PAIR_FORMED, now, f"hedge filled {fill.qty:g} {fill.side} @ {fill.price:.3f}")
    try_lock(cyc, prm, now)


def try_lock(cyc: Cycle, prm: PairLockParams, now: float) -> tuple[bool, str]:
    if cyc.state is not State.PAIR_FORMED:
        return False, f"state {cyc.state.value}"
    ok, why = pair_lock_check(cyc.book, prm.min_pair_edge, prm.min_locked_profit)
    if ok:
        cyc.to(State.PAIR_LOCKED, now, f"net pair edge {cyc.book.net_pair_edge:+.4f}, locked {cyc.book.locked_pair_pnl:+.4f}")
        cyc.locked_t = now
    return ok, why


def on_order_done(cyc: Cycle, order_id: str, filled_any: bool, now: float, why: str = "",
                  prm: PairLockParams | None = None) -> None:
    """The order's final result. Unfilled: the entry cycle goes back to NO_POSITION; a hedge leaves the
    exposure as it was (an incomplete hedge) and the cycle watches again. Filled: with nothing pending any more,
    the pair-lock rule is checked again (``prm`` given) -- a lock is never decided while an order is in flight."""
    p = cyc.book.pending.pop(order_id, None)
    if p is None:
        return
    if filled_any and prm is not None and cyc.state is State.PAIR_FORMED:
        try_lock(cyc, prm, now)
        return
    if p.kind == "entry":
        if not filled_any and cyc.state is State.INITIAL_ENTRY:
            cyc.to(State.NO_POSITION, now, f"entry not filled ({why})")
        return
    if not filled_any:
        cyc.hedge_kills += 1
        if cyc.state in (State.PARTIAL_HEDGE, State.FULL_HEDGE):
            cyc.to(cyc.watch_state(), now, f"hedge not filled ({why})")
    elif cyc.state in (State.PARTIAL_HEDGE, State.FULL_HEDGE):
        cyc.to(cyc.watch_state(), now, "hedge done")


def on_close(cyc: Cycle, now: float) -> bool:
    """The market closed. A cycle holding shares waits for the venue's outcome; one that holds nothing is
    dropped (returns False)."""
    b = cyc.book
    if b.up_qty + b.down_qty <= EPS:
        return False
    if cyc.state is not State.SETTLEMENT_PENDING:
        if cyc.state in (State.HEDGE_EVALUATION,):
            cyc.to(State.NO_HEDGE, now, "market closed")
        if cyc.state is State.NO_HEDGE:
            cyc.to(cyc.watch_state(), now, "")
        cyc.to(State.SETTLEMENT_PENDING, now, "market closed")
    return True


def on_settled(cyc: Cycle, winner: str, now: float) -> dict:
    """The venue's outcome is booked: SETTLED, with the reconciliation figures."""
    cyc.settled_winner = winner
    if cyc.state is not State.SETTLEMENT_PENDING:
        on_close(cyc, now)
    cyc.to(State.SETTLED, now, f"{winner} won")
    b = cyc.book
    return {"winner": winner, "pnl": b.resolution_pnl(winner), "pnl_from_cash": b.final_pnl_from_cash(winner),
            **cyc.attribution(winner)}
