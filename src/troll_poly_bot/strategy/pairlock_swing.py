"""LIMITED-DEPTH DIRECTIONAL RECOVERY WITH A TWO-SIDED HEDGE AT SWING 3+ (the user's spec, 2026-10-07) for the
pair-lock recovery paper bot. Pure: no I/O. strategy/pairlock_recovery_engine.py runs it when
TPB_PAIRLOCK_REC_SWING_MODE=1; the fills-based PairBook stays the inventory, this module is the decision logic and
the per-market swing accounting.

    SWING 0     ~INITIAL_ENTRY_DELAY_S (20) s after the open, buy INITIAL_SHARES (10) of the LEADING token when the
                executable VWAP of those shares (walked through the ask levels, never the mid) is inside
                [INITIAL_ENTRY_MIN, INITIAL_ENTRY_MAX] (0.55-0.60). The order's limit is INITIAL_ENTRY_MAX.
    A SWING     leadership changes: the token AGAINST the position's current direction leads by at least
                LEADER_HYSTERESIS (mid against mid) AND its best ask has reached SWING_RECOVERY_PRICE (0.70).
                A -> B = swing 1, B -> A = swing 2, A -> B = swing 3, B -> A = swing 4 ... A lead that never reaches
                the recovery price, or a lead inside the hysteresis, is not a swing.
    SWING 1     buy SWING1_SHARES (24) of the new leader         10 A @ .60 + 24 B @ .70 = 22.80; B wins: +1.20
    SWING 2     buy SWING2_SHARES (80) of the new leader         A 90 / B 24, cost 78.80;       A wins: +11.20
    SWING 3+    (any swing past MAX_RECOVERY_SWING = 2) NO directional buy: HEDGE MODE. Buy the side with FEWER
                shares until UP = DOWN (hedge = |A - B|; 66 B in the default path -> A 90 / B 90, cost 125.00,
                -35.00 whoever wins). Swing 4, 5, ... stay in hedge mode: with UP = DOWN nothing more is bought; an
                unfinished hedge (depth, capital) keeps evening out. Never back to directional recovery.

The sizes are fixed (never a multiplier). Every buy is sized and priced on the live ask levels and bounded by: the
target (the swing's shares, or the inventory gap for a hedge), the depth at or under the order's limit,
MAX_MARKET_CAPITAL (the order's WORST-CASE cost -- every share at its limit plus that price's fee -- must fit under the
cap with everything already spent in the market), MAX_TOTAL_POSITION and the free bankroll. When the capital binds,
a lower limit (down to the best ask) that lets more shares fit is used. What a fill-and-kill order leaves unfilled is
retried while its swing stands (directional) or while the inventory is uneven (hedge); a remainder under the venue
minimum counts as done.

The strategy's parameters (sizes, prices, depth, hysteresis, hedge switch) are captured per market at its entry
(SwingState.plan), so a market is managed by one rule set from start to end; the capital cap and the position cap
are read live (a lowered safety limit binds at once).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from .pairlock import MarketView
from .pairlock_book import PairBook, fee_per_share

OTHER = {"UP": "DOWN", "DOWN": "UP"}
SIDES = ("UP", "DOWN")


class Phase(str, Enum):
    INITIAL_ENTRY = "INITIAL_ENTRY"          # the first buy is in flight, nothing held yet
    SWING_0 = "SWING_0"                      # holding the initial directional position
    SWING_1_RECOVERY = "SWING_1_RECOVERY"
    SWING_2_RECOVERY = "SWING_2_RECOVERY"
    HEDGE_MODE = "HEDGE_MODE"                # swing 3+ (past the recovery depth): two-sided inventory balancing
    RESOLUTION = "RESOLUTION"                # the market closed: no more orders, settlement pending / done


RECOVERY_PHASES = (Phase.SWING_1_RECOVERY, Phase.SWING_2_RECOVERY)
#: the spec defines directional sizes for swings 1 and 2 only: the recovery depth is 0..2
MAX_DEPTH = len(RECOVERY_PHASES)

TRANSITIONS = {
    Phase.INITIAL_ENTRY: {Phase.SWING_0, Phase.RESOLUTION},
    Phase.SWING_0: {Phase.SWING_1_RECOVERY, Phase.HEDGE_MODE, Phase.RESOLUTION},
    Phase.SWING_1_RECOVERY: {Phase.SWING_2_RECOVERY, Phase.HEDGE_MODE, Phase.RESOLUTION},
    Phase.SWING_2_RECOVERY: {Phase.HEDGE_MODE, Phase.RESOLUTION},
    Phase.HEDGE_MODE: {Phase.RESOLUTION},    # every later swing stays here
    Phase.RESOLUTION: set(),
}


def phase_for(swing_number: int, max_recovery_swing: int) -> Phase:
    """The phase a market is in after its ``swing_number``-th swing."""
    if swing_number <= 0:
        return Phase.SWING_0
    if swing_number <= min(int(max_recovery_swing), MAX_DEPTH):
        return RECOVERY_PHASES[swing_number - 1]
    return Phase.HEDGE_MODE


def bucket(swing_number: int) -> str:
    """The report's bucket: the deepest swing a market reached."""
    return "3+" if swing_number >= 3 else str(max(0, int(swing_number)))


def plan_from(prm) -> dict:
    """The strategy parameters one market is managed with (captured at its entry)."""
    return {"initial_shares": float(prm.swing_initial_shares), "entry_min": float(prm.swing_entry_min),
            "entry_max": float(prm.swing_entry_max), "recovery_price": float(prm.swing_recovery_price),
            "sizing": str(getattr(prm, "swing_sizing", "fixed")), "target_profit": float(getattr(prm, "swing_target_profit", 3.0)),
            "swing_shares": [float(prm.swing1_shares), float(prm.swing2_shares)],
            "max_recovery_swing": max(0, min(int(prm.swing_max_recovery), MAX_DEPTH)),
            "two_sided_hedge": bool(prm.swing_two_sided_hedge), "hysteresis": float(prm.swing_leader_hysteresis)}


# ───────────────────────────────── the market's swing state ─────────────────────────────────


@dataclass
class SwingState:
    initial_token: str
    plan: dict
    phase: Phase = Phase.INITIAL_ENTRY
    swing_number: int = 0
    direction: str = ""                  # the token the position bets on now: A, then each directional swing's token
    current_leader: str = ""             # the leader by mid, with the hysteresis
    started_t: float = 0.0
    last_swing_t: float | None = None
    last_swing_price: float | None = None   # the new leader's best ask when the swing was recognised
    last_order_t: float = -1e18
    entry: dict = field(default_factory=dict)   # target, filled, cost (fees in), fees, orders
    swings: list = field(default_factory=list)  # one row per recognised swing
    hedge: dict | None = None            # from the first hedge-mode swing: orders, filled, cost, fills, last plan
    resolution: dict | None = None
    transitions: list = field(default_factory=list)

    @classmethod
    def start(cls, side: str, now: float, prm) -> "SwingState":
        plan = plan_from(prm)
        return cls(initial_token=side, plan=plan, direction=side, current_leader=side, started_t=now,
                   entry={"target": plan["initial_shares"], "filled": 0.0, "cost": 0.0, "fees": 0.0, "orders": 0})

    def to(self, new: Phase, t: float, why: str = "") -> None:
        if new is self.phase:
            return
        if new not in TRANSITIONS[self.phase]:
            raise RuntimeError(f"swing state {self.phase.value} -> {new.value} is not a valid transition")
        self.transitions.append((t, self.phase.value, new.value, why))
        self.phase = new

    @property
    def hedge_mode(self) -> bool:
        return self.phase is Phase.HEDGE_MODE or (self.phase is Phase.RESOLUTION and self.hedge is not None)

    @property
    def recovery_mode(self) -> bool:
        return self.phase in RECOVERY_PHASES

    @property
    def current_swing(self) -> dict | None:
        return self.swings[-1] if self.swings else None

    def to_json(self) -> dict:
        return {"initial_token": self.initial_token, "plan": dict(self.plan), "phase": self.phase.value,
                "swing_number": self.swing_number, "direction": self.direction, "current_leader": self.current_leader,
                "started_t": self.started_t, "last_swing_t": self.last_swing_t, "last_swing_price": self.last_swing_price,
                "last_order_t": self.last_order_t, "entry": dict(self.entry), "swings": [dict(s) for s in self.swings],
                "hedge": None if self.hedge is None else dict(self.hedge), "resolution": self.resolution,
                "transitions": [list(x) for x in self.transitions[-40:]]}

    @classmethod
    def from_json(cls, d: dict) -> "SwingState":
        return cls(initial_token=d["initial_token"], plan=dict(d["plan"]), phase=Phase(d.get("phase", "SWING_0")),
                   swing_number=int(d.get("swing_number") or 0), direction=str(d.get("direction") or d["initial_token"]),
                   current_leader=str(d.get("current_leader") or ""), started_t=float(d.get("started_t") or 0.0),
                   last_swing_t=d.get("last_swing_t"), last_swing_price=d.get("last_swing_price"),
                   last_order_t=float(d.get("last_order_t") or -1e18), entry=dict(d.get("entry") or {}),
                   swings=[dict(s) for s in d.get("swings") or []], hedge=d.get("hedge"), resolution=d.get("resolution"),
                   transitions=[tuple(x) for x in d.get("transitions") or []])


# ───────────────────────────────── prices, leader, swings ─────────────────────────────────


def _mid(bid, ask):
    if bid is not None and ask is not None:
        return 0.5 * (bid + ask)
    return None


def mids(mv: MarketView) -> dict:
    return {"UP": _mid(mv.bid_up, mv.ask_up), "DOWN": _mid(mv.bid_down, mv.ask_down)}


def _leads(m: dict, a: str, b: str, h: float) -> bool:
    """Does ``a`` lead ``b`` by the hysteresis (by more than nothing when it is 0)?"""
    lead = m[a] - m[b]
    return lead >= h - 1e-12 if h > 0.0 else lead > 1e-12


def leader_with_hysteresis(current: str, m: dict, h: float) -> str:
    """The leader by mid: the current one stays until the other token leads it by at least ``h``. With no current
    leader, the token that leads by ``h`` (else '')."""
    if m.get("UP") is None or m.get("DOWN") is None:
        return current
    if current in SIDES:
        o = OTHER[current]
        return o if _leads(m, o, current, h) else current
    for s in SIDES:
        if _leads(m, s, OTHER[s], h):
            return s
    return ""


def observe(st: SwingState, mv: MarketView, now: float) -> dict | None:
    """Every sample of a held market (also while an order is in flight): the leader, and a SWING when the token
    against the position's direction leads by the hysteresis AND its best ask has reached the recovery price.
    Returns the new swing's row, else None."""
    m = mids(mv)
    if m["UP"] is None or m["DOWN"] is None:
        return None
    h = float(st.plan["hysteresis"])
    st.current_leader = leader_with_hysteresis(st.current_leader, m, h)
    if st.phase in (Phase.INITIAL_ENTRY, Phase.RESOLUTION) or st.direction not in SIDES:
        return None
    ch = OTHER[st.direction]
    ask = mv.ask(ch)
    if ask is None or not _leads(m, ch, st.direction, h) or ask < float(st.plan["recovery_price"]) - 1e-9:
        return None
    return register_swing(st, ch, now, ask, m)


def register_swing(st: SwingState, to: str, now: float, ask: float, m: dict) -> dict:
    n = st.swing_number + 1
    new = phase_for(n, st.plan["max_recovery_swing"])
    row = {"n": n, "t": now, "from": st.direction, "to": to, "ask": ask, "mid_to": m.get(to), "mid_from": m.get(st.direction),
           "phase": new.value}
    if new in RECOVERY_PHASES and st.plan.get("sizing", "fixed") == "target":
        row.update({"action": "recovery", "sizing": "target", "target_profit": float(st.plan["target_profit"]), "target": None,
                    "filled": 0.0, "cost": 0.0, "fees": 0.0, "orders": 0})
    elif new in RECOVERY_PHASES:
        row.update({"action": "recovery", "target": float(st.plan["swing_shares"][n - 1]), "filled": 0.0, "cost": 0.0,
                    "fees": 0.0, "orders": 0})
    elif st.phase is not Phase.HEDGE_MODE:
        row["action"] = "hedge" if st.plan["two_sided_hedge"] else "hold (two-sided hedge disabled)"
        st.hedge = {"started_swing": n, "started_t": now, "orders": 0, "filled": 0.0, "cost": 0.0, "fees": 0.0,
                    "fills": [], "plan": None}
    else:
        row["action"] = "none: hedge mode controls the position"
    st.swings.append(row)
    st.swing_number = n
    st.direction = to
    st.current_leader = to
    st.last_swing_t = now
    st.last_swing_price = ask
    st.to(new, now, f"swing {n}: {row['from']} -> {to} (ask {ask:.2f})")
    return row


# ───────────────────────────────── sizing ─────────────────────────────────


def _fee_over(used: list, fees) -> float:
    q = sum(float(n) for _, n in used)
    return sum(float(n) * fees(float(p)) for p, n in used) / q if q > 0.0 else 0.0


def walk(levels, qty: float, haircut: float = 0.0):
    """(VWAP or None, [(price, shares)]) of ``qty`` shares walked through ``levels`` (ascending asks)."""
    need, cost, used = qty, 0.0, []
    for p, s in sorted(((float(p), float(s) * (1.0 - haircut)) for p, s in levels), key=lambda x: x[0]):
        if s <= 1e-9:
            continue
        take = min(need, s)
        cost += take * p
        used.append((p, take))
        need -= take
        if need <= 1e-9:
            break
    if need > 1e-9 or qty <= 0.0:
        return None, used
    return cost / qty, used


def _floor2(x: float) -> float:
    return math.floor(max(0.0, x) * 100.0 + 1e-6) / 100.0


def size_buy(side: str, want: float, mv: MarketView, book: PairBook, prm, bankroll_free: float, top_limit: float,
             fees=fee_per_share) -> dict:
    """The largest buy of ``side`` up to ``want`` shares that every bound allows, at the highest limit (<= top_limit,
    >= the best ask) that buys the most. Bounds: want; the depth at or under the limit; MAX_MARKET_CAPITAL (spent so
    far + shares x (limit + fee(limit)) <= the cap: the worst case of a fill-and-kill at that limit); MAX_TOTAL_POSITION;
    the free bankroll. Returns {"ok", "q", "limit", "vwap", "fee_ps", "cost", "levels", "bound", "room", "why"}."""
    asks = [(float(p), float(s)) for p, s in (mv.asks(side) or []) if float(s) > 0.0]
    best = min((p for p, _ in asks), default=None)
    out = {"ok": False, "side": side, "want": want, "best_ask": best, "q": 0.0, "limit": None, "vwap": None,
           "fee_ps": 0.0, "cost": 0.0, "levels": [], "bound": "", "room": {}, "why": ""}
    if best is None:
        out["why"] = f"no {side} ask"
        return out
    top = min(0.99, round(top_limit, 2))
    if best > top + 1e-9:
        out["why"] = f"{side} best ask {best:.2f} is above the limit {top:.2f}"
        return out
    cap = float(prm.swing_max_market_capital)
    spent = book.cash_spent
    haircut = float(prm.depth_haircut)
    best_pick = None
    lim = top
    while lim >= best - 1e-9:
        unit = lim + fees(lim)
        levels = [(p, s) for p, s in asks if p <= lim + 1e-9]
        room = {"target": want, "MAX_MARKET_CAPITAL": (cap - spent) / unit,
                "MAX_TOTAL_POSITION": float(prm.max_total_position) - (book.up_qty + book.down_qty),
                "depth at or under the limit": sum(s * (1.0 - haircut) for _, s in levels),
                "free bankroll": bankroll_free / unit}
        bound = min(room, key=room.get)
        q = _floor2(room[bound])
        if best_pick is None or q > best_pick[0] + 1e-9:
            best_pick = (q, lim, bound, room, levels)
        if bound not in ("MAX_MARKET_CAPITAL", "free bankroll"):
            break                                         # only the cost per share can improve at a lower limit
        lim = round(lim - 0.01, 2)
    q, lim, bound, room, levels = best_pick
    out.update({"q": q, "limit": lim, "bound": bound, "room": {k: round(v, 2) for k, v in room.items()}})
    if q < float(prm.min_size) - 1e-9:
        out["why"] = (f"{bound} leaves {max(0.0, room[bound]):.2f} sh < the {float(prm.min_size):g}-share minimum "
                      f"(wanted {want:g} {side})")
        return out
    vw, used = walk(levels, q, haircut)
    if vw is None:
        out["why"] = f"insufficient depth for {q:g} {side} at or under {lim:.2f}"
        return out
    fps = _fee_over(used, fees)
    out.update({"ok": True, "vwap": vw, "fee_ps": fps, "cost": q * (vw + fps), "levels": used,
                "worst_case_cost": q * (lim + fees(lim)), "partial": q < want - 1e-9})
    return out


def target_quantity(have: float, total_cost: float, realised: float, asks, target: float, fees=fee_per_share,
                    max_price: float = 0.99, max_shares: float = 1e9, min_size: float = 0.0) -> dict:
    """THE RECOVERY SIZE (the user, 2026-10-08): the SMALLEST INTEGER number of shares x of the recovery token such that

        profit if that token wins = (held + x) - (total cost so far + cost of x) + realised  >  target

    where cost of x walks the live ask levels (price ascending) share by share, each part at its level's price plus that
    price's fee -- the quantity and its VWAP are found together, so a size that reaches dearer levels is priced there.
    The asks already contain the spread (nothing is added for it). ``have`` / ``total_cost`` / ``realised`` are the
    market's whole inventory of that token and every cost paid so far (all fills, both sides, fees in).

    Refused (ok False, why) when the visible depth at or under ``max_price`` runs out first (``reason`` "depth", or
    "price" when the levels it would need are above ``max_price``), when x would exceed ``max_shares``, or when a share
    no longer earns anything (price + fee >= 1)."""
    base = have - total_cost + realised
    levels = sorted((float(p), float(sz)) for p, sz in (asks or []) if float(sz) > 0.0)
    allowed = [(p, sz) for p, sz in levels if p <= max_price + 1e-9]
    above = any(p > max_price + 1e-9 for p, _ in levels)
    out = {"ok": False, "profit_before": base, "target": target, "x": 0, "vwap": None, "fee_ps": 0.0, "cost": 0.0,
           "gross": 0.0, "fees": 0.0, "deepest": None, "levels": [], "profit_after": base, "reason": "", "why": "",
           "depth_under_max_price": sum(sz for _, sz in allowed), "best_ask": levels[0][0] if levels else None}
    if base > target + 1e-9:
        out.update(ok=True, why=f"the token's win already pays {base:+.2f} > target {target:.2f}")
        return out
    if not levels:
        out.update(reason="depth", why="no ask")
        return out
    x, cost, gross, fee, i = 0, 0.0, 0.0, 0.0, 0
    left = allowed[0][1] if allowed else 0.0
    used: dict[float, float] = {}
    while not (base + x - cost > target + 1e-9 and x >= min_size - 1e-9):
        if x + 1 > max_shares + 1e-9:
            est = _est_qty(base, target, allowed[min(i, len(allowed) - 1)][0] if allowed else levels[0][0], fees)
            out.update(reason="max_shares", x=x, why=f"needs more than MAX_RECOVERY_SHARES {max_shares:g} (about {est} at the "
                                                     f"current level)")
            return out
        need = 1.0
        while need > 1e-12:
            if i >= len(allowed):
                est = _est_qty(base, target, (allowed[-1][0] if allowed else levels[0][0]), fees)
                out.update(reason="price" if above else "depth", x=x,
                           why=(f"the {sum(sz for _, sz in allowed):g} sh offered at or under {max_price:.2f} are not enough "
                                f"(about {est} needed); the rest is above MAX_RECOVERY_PRICE {max_price:.2f}") if above else
                           f"insufficient depth: {sum(sz for _, sz in allowed):g} sh offered, about {est} needed")
                return out
            p = allowed[i][0]
            if p + fees(p) >= 1.0 - 1e-12:
                out.update(reason="price", x=x, why=f"at {p:.2f} + fee a share earns nothing: the target is unreachable")
                return out
            take = min(need, left)
            cost += take * (p + fees(p))
            gross += take * p
            fee += take * fees(p)
            used[p] = used.get(p, 0.0) + take
            left -= take
            need -= take
            if left <= 1e-12:
                i += 1
                left = allowed[i][1] if i < len(allowed) else 0.0
        x += 1
    out.update(ok=True, x=x, vwap=gross / x, fee_ps=fee / x, cost=cost, gross=gross, fees=fee, deepest=max(used),
               levels=sorted(used.items()), profit_after=base + x - cost,
               why=f"{x} sh at VWAP {gross / x:.4f} + fee {fee / x:.4f}: the win pays {base + x - cost:+.2f} > {target:.2f}")
    return out


def _est_qty(base: float, target: float, p: float, fees) -> str:
    per = 1.0 - p - fees(p)
    return f"{math.ceil((target - base) / per)} sh" if per > 1e-9 else "no size"


def after_buy(book: PairBook, side: str, q: float, cost: float) -> dict:
    up = book.up_qty + (q if side == "UP" else 0.0)
    dn = book.down_qty + (q if side == "DOWN" else 0.0)
    tc = book.total_cost + cost
    r = book.realised_exit_pnl
    return {"up": up, "down": dn, "total_cost": tc, "pnl_up_win": up - tc + r, "pnl_down_win": dn - tc + r}


# ───────────────────────────────── decisions ─────────────────────────────────


@dataclass
class SwingOrder:
    kind: str                     # "entry" | "recovery" | "hedge"
    side: str
    shares: float
    limit: float
    vwap: float
    fee_ps: float
    plan_qty: float               # what this action wants in all (the swing's remaining shares, the hedge's gap)
    swing: int                    # the swing number the order belongs to (0 = the initial entry)
    reason: str
    record: dict


def _base(book: PairBook, mv: MarketView, now: float, left: float, st: SwingState | None) -> dict:
    up, dn, tc, r = book.up_qty, book.down_qty, book.total_cost, book.realised_exit_pnl
    rec = {"t": now, "event": "SWING_EVAL", "time_left": left, "up_bid": mv.bid_up, "up_ask": mv.ask_up,
           "down_bid": mv.bid_down, "down_ask": mv.ask_down, "up_qty": up, "down_qty": dn, "total_cost": tc,
           "capital_spent": book.cash_spent, "pnl_up_win": up - tc + r, "pnl_down_win": dn - tc + r,
           "decision": "NONE", "why": ""}
    if st is not None:
        rec.update({"phase": st.phase.value, "swing_number": st.swing_number, "initial_token": st.initial_token,
                    "direction": st.direction, "current_leader": st.current_leader})
    return rec


def entry_decision(mv: MarketView, elapsed_s: float, left_s: float, prm, bankroll_free: float, book: PairBook | None = None,
                   st: SwingState | None = None, now: float = 0.0, fees=fee_per_share) -> tuple[SwingOrder | None, dict]:
    """SWING 0: the leading token's INITIAL_SHARES when their executable VWAP is in [entry_min, entry_max], inside the
    entry window. ``st``/``book`` given = a top-up of an initial entry that filled only in part."""
    book = book or PairBook("")
    plan = st.plan if st is not None else plan_from(prm)
    rec = _base(book, mv, now, left_s, st)
    rec.update({"kind": "swing_entry", "elapsed_s": elapsed_s})

    def no(why, decision="REJECTED"):
        rec.update(decision=decision, why=why)
        return None, rec

    lo, hi = float(prm.swing_entry_delay_s), float(prm.swing_entry_window_s)
    if not lo - 1e-9 <= elapsed_s <= hi + 1e-9:
        return no(f"{elapsed_s:.1f} s after the open is outside the entry window {lo:g}-{hi:g} s", "NONE")
    m = mids(mv)
    if m["UP"] is None or m["DOWN"] is None:
        return no("no two-sided price")
    side = st.initial_token if st is not None else leader_with_hysteresis("", m, float(plan["hysteresis"]))
    if side not in SIDES:
        return no(f"no leader: UP mid {m['UP']:.3f} vs DOWN mid {m['DOWN']:.3f} within the hysteresis "
                  f"{float(plan['hysteresis']):.2f}", "NONE")
    if st is not None and not _leads(m, side, OTHER[side], float(plan["hysteresis"])):
        return no(f"top-up: {side} no longer leads", "NONE")
    have = 0.0 if st is None else float(st.entry.get("filled", 0.0))
    want = _floor2(float(plan["initial_shares"]) - have)
    rec.update({"side": side, "entry_target": plan["initial_shares"], "entry_have": have})
    if want < float(prm.min_size) - 1e-9:
        return no(f"initial position complete ({have:g} of {plan['initial_shares']:g})", "NONE")
    if left_s < float(prm.min_time_left_s):
        return no(f"{left_s:.0f} s left < MIN_TIME_LEFT {float(prm.min_time_left_s):g}")
    best = min((float(p) for p, s_ in (mv.asks(side) or []) if float(s_) > 0.0), default=None)
    if best is None:
        return no(f"no {side} ask")
    if best > float(plan["entry_max"]) + 1e-9:
        return no(f"{side} best ask {best:.2f} is above the entry band's top {float(plan['entry_max']):.2f}", "NONE")
    s = size_buy(side, want, mv, book, prm, bankroll_free, float(plan["entry_max"]), fees)
    rec.update({"sizing": {k: s[k] for k in ("q", "limit", "bound", "room", "best_ask")}})
    if not s["ok"]:
        return no(f"entry: {s['why']}")
    vw = s["vwap"]
    rec.update({"vwap": vw, "fee_per_share": s["fee_ps"], "order_qty": s["q"], "limit": s["limit"]})
    if not float(plan["entry_min"]) - 1e-9 <= vw <= float(plan["entry_max"]) + 1e-9:
        return no(f"{side} VWAP {vw:.3f} for {s['q']:g} sh is outside the entry band "
                  f"{float(plan['entry_min']):.2f}-{float(plan['entry_max']):.2f}", "NONE")
    aft = after_buy(book, side, s["q"], s["cost"])
    rec.update({"decision": "BUY", "after_pnl_up_win": aft["pnl_up_win"], "after_pnl_down_win": aft["pnl_down_win"],
                "order_cost": s["cost"], "improves_min": False, "edge_per_share": 0.0})
    reason = (f"SWING 0 {'top-up' if st is not None else 'entry'}: {side} leads (mid {m[side]:.3f} vs {m[OTHER[side]]:.3f}), "
              f"{s['q']:g} sh at VWAP {vw:.3f} (fee {s['fee_ps']:.4f}) in {float(plan['entry_min']):.2f}-"
              f"{float(plan['entry_max']):.2f}, limit {s['limit']:.2f}, {elapsed_s:.1f} s after the open"
              + (f"; limited by {s['bound']}" if s["partial"] else ""))
    rec["why"] = reason
    return SwingOrder("entry", side, s["q"], s["limit"], vw, s["fee_ps"], want, 0, reason, rec), rec


def hedge_plan(st: SwingState, book: PairBook, mv: MarketView, prm, bankroll_free: float, fees=fee_per_share) -> dict:
    """The spec's item-7 figures for evening the inventory out: the existing UP / DOWN (A / B) inventory, the gap, the
    hedge quantity the bounds allow, its executable VWAP and cost, the inventory, cost, guaranteed payout and locked
    P&L after it. Never assumes UP ask + DOWN ask = 1: the light side's own ask levels price it."""
    up, dn = book.up_qty, book.down_qty
    light = "DOWN" if up > dn else "UP"
    gap = _floor2(abs(up - dn))
    A, B = st.initial_token, OTHER[st.initial_token]
    out = {"initial_token": A, "existing_up": up, "existing_down": dn, "existing_a": book.qty(A), "existing_b": book.qty(B),
           "light_side": light, "hedge_required": gap, "total_cost_before": book.total_cost, "balanced": False, "ok": False}
    if gap < float(prm.min_size) - 1e-9:
        out.update(balanced=True, why=f"UP {up:g} / DOWN {dn:g}: the gap {gap:g} is under the {float(prm.min_size):g}-share "
                                      "minimum: balanced")
        return out
    best = min((float(p) for p, s in (mv.asks(light) or []) if float(s) > 0.0), default=None)
    s = size_buy(light, gap, mv, book, prm, bankroll_free, (best or 0.0) + float(prm.max_slippage), fees)
    out.update({"sizing": {k: s[k] for k in ("q", "limit", "bound", "room", "best_ask")}, "light_best_ask": best})
    if not s["ok"]:
        out["why"] = f"hedge: {s['why']}"
        return out
    aft = after_buy(book, light, s["q"], s["cost"])
    payout = min(aft["up"], aft["down"])
    out.update({"ok": True, "hedge_qty": s["q"], "hedge_vwap": s["vwap"], "hedge_fee_per_share": s["fee_ps"],
                "hedge_cost": s["cost"], "limit": s["limit"], "partial": s["partial"], "bound": s["bound"],
                "resulting_up": aft["up"], "resulting_down": aft["down"], "resulting_a": aft["up"] if A == "UP" else aft["down"],
                "resulting_b": aft["down"] if A == "UP" else aft["up"], "total_cost_after": aft["total_cost"],
                "guaranteed_payout": payout, "locked_pnl": payout - aft["total_cost"] + book.realised_exit_pnl,
                "pnl_up_win_after": aft["pnl_up_win"], "pnl_down_win_after": aft["pnl_down_win"]})
    return out


def evaluate(st: SwingState, book: PairBook, mv: MarketView, now: float, left_s: float, elapsed_s: float, prm,
             bankroll_free: float, fees=fee_per_share) -> tuple[SwingOrder | None, dict]:
    """One sample of a held market with no order in flight: the leader and swing recognition (observe), then the
    phase's action -- the initial position's top-up, the current directional swing's buy, or the hedge."""
    new = observe(st, mv, now)
    rec = _base(book, mv, now, left_s, st)
    if new is not None:
        rec["new_swing"] = dict(new)

    def no(why, decision="NONE"):
        rec.update(decision=decision, why=why)
        return None, rec

    if st.phase is Phase.RESOLUTION:
        return no("market closed: resolution pending")
    if mids(mv)["UP"] is None or mids(mv)["DOWN"] is None:
        return no("no two-sided price", "REJECTED")
    # the initial position's top-up (a partial first fill), only while nothing has swung and the window lasts
    if st.phase is Phase.SWING_0 and st.swing_number == 0 and float(st.entry.get("filled", 0.0)) < \
            float(st.plan["initial_shares"]) - float(prm.min_size) + 1e-9 and elapsed_s <= float(prm.swing_entry_window_s):
        if now - st.last_order_t < float(prm.cooldown_ms):
            return no(f"cooldown ({now - st.last_order_t:.0f} ms since the last order)")
        dec, erec = entry_decision(mv, elapsed_s, left_s, prm, bankroll_free, book, st, now, fees)
        erec.update({k: rec[k] for k in ("phase", "swing_number", "direction", "current_leader")})
        return dec, erec
    if st.phase is Phase.SWING_0:
        return no(f"holding swing 0: {st.direction} leads or no swing yet (leader {st.current_leader or '-'})")
    if st.phase in RECOVERY_PHASES and st.current_swing.get("sizing") == "target":
        return _target_recovery(st, book, mv, now, left_s, prm, bankroll_free, fees, rec, no)
    if st.phase in RECOVERY_PHASES:
        cur = st.current_swing
        side = cur["to"]
        remaining = _floor2(float(cur["target"]) - float(cur["filled"]))
        rec.update({"swing_side": side, "swing_target": cur["target"], "swing_filled": cur["filled"],
                    "swing_remaining": remaining})
        if remaining < float(prm.min_size) - 1e-9:
            return no(f"swing {cur['n']} recovery done: {cur['filled']:g} of {cur['target']:g} {side} bought"
                      + (f" (the {remaining:g} left is under the {float(prm.min_size):g}-share minimum)" if remaining > 0 else ""))
        gate = _gates(st, now, left_s, prm)
        if gate:
            return no(gate, "WAIT")
        best = min((float(p) for p, s in (mv.asks(side) or []) if float(s) > 0.0), default=None)
        s = size_buy(side, remaining, mv, book, prm, bankroll_free, (best or 0.0) + float(prm.max_slippage), fees)
        rec["sizing"] = {k: s[k] for k in ("q", "limit", "bound", "room", "best_ask")}
        if not s["ok"]:
            return no(f"swing {cur['n']} recovery: {s['why']}", "REJECTED")
        aft = after_buy(book, side, s["q"], s["cost"])
        rec.update({"decision": "BUY", "order_qty": s["q"], "vwap": s["vwap"], "fee_per_share": s["fee_ps"], "limit": s["limit"],
                    "order_cost": s["cost"], "worst_case_cost": s["worst_case_cost"], "after_up_qty": aft["up"],
                    "after_down_qty": aft["down"], "after_total_cost": aft["total_cost"], "after_pnl_up_win": aft["pnl_up_win"],
                    "after_pnl_down_win": aft["pnl_down_win"], "improves_min": False, "edge_per_share": 0.0})
        reason = (f"SWING {cur['n']} RECOVERY ({cur['from']} -> {side} at ask {cur['ask']:.2f}): buy {s['q']:g} of the "
                  f"{remaining:g} {side} still due (target {cur['target']:g}) at VWAP {s['vwap']:.3f} (fee {s['fee_ps']:.4f}), "
                  f"limit {s['limit']:.2f}" + (f", limited by {s['bound']}" if s["partial"] else "")
                  + f"; after it UP {aft['up']:g} / DOWN {aft['down']:g}, cost {aft['total_cost']:.2f}: "
                    f"UP-win {aft['pnl_up_win']:+.2f} DOWN-win {aft['pnl_down_win']:+.2f}")
        rec["why"] = reason
        return SwingOrder("recovery", side, s["q"], s["limit"], s["vwap"], s["fee_ps"], remaining, cur["n"], reason, rec), rec
    # HEDGE MODE (swing past the recovery depth): even the inventory out, never a directional buy
    if not st.plan["two_sided_hedge"]:
        return no(f"swing {st.swing_number}: hedge mode with the two-sided hedge disabled: holding to resolution")
    hp = hedge_plan(st, book, mv, prm, bankroll_free, fees)
    rec["hedge"] = {k: v for k, v in hp.items() if k != "sizing"}
    rec["sizing"] = hp.get("sizing")
    if hp["balanced"]:
        return no(f"hedge mode (swing {st.swing_number}): {hp['why']}")
    gate = _gates(st, now, left_s, prm)
    if gate:
        return no(gate, "WAIT")
    if not hp["ok"]:
        return no(hp["why"], "REJECTED")
    if st.hedge is not None:
        st.hedge["plan"] = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in hp.items() if k != "sizing"}
    side, q = hp["light_side"], hp["hedge_qty"]
    rec.update({"decision": "BUY", "order_qty": q, "vwap": hp["hedge_vwap"], "fee_per_share": hp["hedge_fee_per_share"],
                "limit": hp["limit"], "order_cost": hp["hedge_cost"], "after_up_qty": hp["resulting_up"],
                "after_down_qty": hp["resulting_down"], "after_total_cost": hp["total_cost_after"],
                "after_pnl_up_win": hp["pnl_up_win_after"], "after_pnl_down_win": hp["pnl_down_win_after"],
                "improves_min": True, "edge_per_share": 0.0})
    reason = (f"HEDGE (swing {st.swing_number}, two-sided): A={st.initial_token} {hp['existing_a']:g} / B "
              f"{hp['existing_b']:g} -> buy {q:g} {side} of the {hp['hedge_required']:g} gap at VWAP {hp['hedge_vwap']:.3f} "
              f"(fee {hp['hedge_fee_per_share']:.4f}, limit {hp['limit']:.2f}) = {hp['hedge_cost']:.2f}"
              + (f", limited by {hp['bound']}" if hp["partial"] else "")
              + f"; after it UP {hp['resulting_up']:g} / DOWN {hp['resulting_down']:g}, total cost {hp['total_cost_after']:.2f}, "
                f"guaranteed payout {hp['guaranteed_payout']:.2f}, locked P&L {hp['locked_pnl']:+.2f}")
    rec["why"] = reason
    return SwingOrder("hedge", side, q, hp["limit"], hp["hedge_vwap"], hp["hedge_fee_per_share"], hp["hedge_required"],
                      st.swing_number, reason, rec), rec


def _target_recovery(st: SwingState, book: PairBook, mv: MarketView, now: float, left_s: float, prm, bankroll_free: float,
                     fees, rec: dict, no) -> tuple[SwingOrder | None, dict]:
    """A swing-1 / swing-2 recovery under the TARGET sizing: buy the smallest integer size that lifts the new leader's
    win outcome above TARGET_RECOVERY_PROFIT, from the whole inventory and cost so far (after swing 1, swing 2 starts
    from what swing 1 left), priced on the live asks with fees. All or nothing: the size is REFUSED -- nothing is bought,
    the next sample tries again while the swing stands -- when the depth, MAX_RECOVERY_SHARES, MAX_RECOVERY_PRICE, the
    capital cap, the position cap or the bankroll cannot take the WHOLE size. A fill-and-kill that fills in part is
    followed by a new size from the new inventory."""
    cur = st.current_swing
    side = cur["to"]
    target = float(cur.get("target_profit", st.plan.get("target_profit", 3.0)))
    profit_now = book.qty(side) - book.total_cost + book.realised_exit_pnl
    bid, ask = mv.bid(side), mv.ask(side)
    rec.update({"swing_side": side, "sizing": "target", "target_profit": target, "profit_if_side_wins": profit_now,
                "swing_filled": cur["filled"], "spread": None if bid is None or ask is None else ask - bid,
                "side_bid": bid, "side_ask": ask})
    if profit_now > target + 1e-9:
        return no(f"swing {cur['n']} recovery done: the {side} win pays {profit_now:+.2f} > target {target:.2f} "
                  f"({cur['filled']:g} {side} bought)")
    gate = _gates(st, now, left_s, prm)
    if gate:
        return no(gate, "WAIT")
    max_price = float(getattr(prm, "swing_max_recovery_price", 0.99))
    max_sh = float(getattr(prm, "swing_max_recovery_shares", 1e9)) - float(cur["filled"])
    q = target_quantity(book.qty(side), book.total_cost, book.realised_exit_pnl, mv.asks(side), target, fees, max_price,
                        max_sh, float(prm.min_size))
    rec["target_sizing"] = {k: q[k] for k in ("x", "vwap", "fee_ps", "cost", "deepest", "profit_before", "profit_after", "reason",
                                              "depth_under_max_price", "best_ask")}
    if not q["ok"]:
        return no(f"swing {cur['n']} recovery refused: {q['why']}", "REJECTED")
    x = q["x"]
    if x <= 0:
        return no(f"swing {cur['n']} recovery done: {q['why']}")
    # the order's limit: the deepest level the size needs, plus MAX_SLIPPAGE of room while the capital cap allows it
    best = q["best_ask"]
    top = min(max_price, max(q["deepest"], round(best + float(prm.max_slippage), 2)))
    cap, spent = float(prm.swing_max_market_capital), book.cash_spent
    lim = None
    k = round(top, 2)
    while k >= q["deepest"] - 1e-9:
        if spent + x * (k + fees(k)) <= cap + 1e-9:
            lim = k
            break
        k = round(k - 0.01, 2)
    rec["capital"] = {"spent": spent, "cap": cap, "worst_case_at_deepest": spent + x * (q["deepest"] + fees(q["deepest"]))}
    if lim is None:
        return no(f"swing {cur['n']} recovery refused: {x} {side} would take the market to "
                  f"{spent + x * (q['deepest'] + fees(q['deepest'])):.2f} > MAX_MARKET_CAPITAL {cap:.2f}", "REJECTED")
    if book.up_qty + book.down_qty + x > float(prm.max_total_position) + 1e-9:
        return no(f"swing {cur['n']} recovery refused: {x} sh would exceed MAX_TOTAL_POSITION {float(prm.max_total_position):g}",
                  "REJECTED")
    if x * (lim + fees(lim)) > bankroll_free + 1e-9:
        return no(f"swing {cur['n']} recovery refused: {x * (lim + fees(lim)):.2f} > free bankroll {bankroll_free:.2f}", "REJECTED")
    aft = after_buy(book, side, x, q["cost"])
    mid = None if bid is None or ask is None else 0.5 * (bid + ask)
    rec.update({"decision": "BUY", "order_qty": x, "vwap": q["vwap"], "fee_per_share": q["fee_ps"], "limit": lim,
                "order_cost": q["cost"], "worst_case_cost": x * (lim + fees(lim)), "after_up_qty": aft["up"],
                "after_down_qty": aft["down"], "after_total_cost": aft["total_cost"], "after_pnl_up_win": aft["pnl_up_win"],
                "after_pnl_down_win": aft["pnl_down_win"], "spread_cost_vs_mid": None if mid is None else x * (q["vwap"] - mid),
                "improves_min": False, "edge_per_share": 0.0})
    reason = (f"SWING {cur['n']} RECOVERY ({cur['from']} -> {side} at ask {cur['ask']:.2f}), target sizing: the {side} win pays "
              f"{profit_now:+.2f} now -> buy {x} {side} at VWAP {q['vwap']:.4f} + fee {q['fee_ps']:.4f} (levels to {q['deepest']:.2f}, "
              f"limit {lim:.2f}, spread {rec['spread'] if rec['spread'] is None else round(rec['spread'], 3)}) = {q['cost']:.2f}; "
              f"after it UP {aft['up']:g} / DOWN {aft['down']:g}, cost {aft['total_cost']:.2f}: {side}-win {q['profit_after']:+.2f} "
              f"> {target:.2f}, other side {(aft['pnl_down_win'] if side == 'UP' else aft['pnl_up_win']):+.2f}")
    rec["why"] = reason
    return SwingOrder("recovery", side, float(x), lim, q["vwap"], q["fee_ps"], float(x), cur["n"], reason, rec), rec


def _gates(st: SwingState, now: float, left_s: float, prm) -> str:
    if left_s < float(prm.min_time_left_s):
        return f"{left_s:.0f} s left < MIN_TIME_LEFT {float(prm.min_time_left_s):g}: no more orders"
    if now - st.last_order_t < float(prm.cooldown_ms):
        return f"cooldown ({now - st.last_order_t:.0f} ms since the last order)"
    return ""


# ───────────────────────────────── order lifecycle ─────────────────────────────────


def on_sent(st: SwingState, kind: str, swing: int, now: float) -> None:
    st.last_order_t = now
    if kind == "entry":
        st.entry["orders"] = int(st.entry.get("orders", 0)) + 1
    elif kind == "recovery":
        row = next((r for r in st.swings if r["n"] == swing), None)
        if row is not None:
            row["orders"] = int(row.get("orders", 0)) + 1
    elif kind == "hedge" and st.hedge is not None:
        st.hedge["orders"] = int(st.hedge.get("orders", 0)) + 1


def on_not_sent(st: SwingState, kind: str, swing: int) -> None:
    """The order never left (refused before the venue): undo on_sent's count, no cooldown."""
    st.last_order_t = -1e18
    if kind == "entry":
        st.entry["orders"] = max(0, int(st.entry.get("orders", 0)) - 1)
    elif kind == "recovery":
        row = next((r for r in st.swings if r["n"] == swing), None)
        if row is not None:
            row["orders"] = max(0, int(row.get("orders", 0)) - 1)
    elif kind == "hedge" and st.hedge is not None:
        st.hedge["orders"] = max(0, int(st.hedge.get("orders", 0)) - 1)


def on_fill(st: SwingState, kind: str, side: str, qty: float, price: float, fee: float, now: float, swing: int) -> None:
    """A venue fill of one of the strategy's buys (the PairBook takes the same fill)."""
    cost = qty * price + fee
    if kind == "entry":
        e = st.entry
        e["filled"] = float(e.get("filled", 0.0)) + qty
        e["cost"] = float(e.get("cost", 0.0)) + cost
        e["fees"] = float(e.get("fees", 0.0)) + fee
        e["vwap"] = (e["cost"] - e["fees"]) / e["filled"]
        if st.phase is Phase.INITIAL_ENTRY:
            st.to(Phase.SWING_0, now, f"initial {qty:g} {side} @ {price:.3f}")
        return
    if kind == "recovery":
        row = next((r for r in st.swings if r["n"] == swing), None)
        if row is None:
            return
    elif kind == "hedge" and st.hedge is not None:
        row = st.hedge
        row["fills"] = list(row.get("fills") or []) + [[now, side, qty, price, fee]]
    else:
        return
    row["filled"] = float(row.get("filled", 0.0)) + qty
    row["cost"] = float(row.get("cost", 0.0)) + cost
    row["fees"] = float(row.get("fees", 0.0)) + fee
    row["vwap"] = (row["cost"] - row["fees"]) / row["filled"]


def on_close(st: SwingState, now: float) -> None:
    if st.phase is not Phase.RESOLUTION:
        st.to(Phase.RESOLUTION, now, "market closed")


def on_settled(st: SwingState, book: PairBook, winner: str, now: float) -> dict:
    on_close(st, now)
    tc = book.total_cost
    r = book.realised_exit_pnl
    res = {"winner": winner, "bucket": bucket(st.swing_number), "swing_number": st.swing_number,
           "initial_token": st.initial_token, "up_qty": book.up_qty, "down_qty": book.down_qty, "total_cost": tc,
           "fees": book.fees, "payout": book.qty(winner), "pnl": book.qty(winner) - tc + r,
           "pnl_if_up_wins": book.up_qty - tc + r, "pnl_if_down_wins": book.down_qty - tc + r,
           "hedged": st.hedge is not None and float(st.hedge.get("filled", 0.0)) > 0.0,
           "balanced": abs(book.up_qty - book.down_qty) < 1e-6 and book.up_qty > 0.0,
           "path": [f"{s['from']}->{s['to']}" for s in st.swings], "t": now}
    st.resolution = res
    return res


# ───────────────────────────────── accounting (spec items 10-11) ─────────────────────────────────


def projected_additional_capital(st: SwingState, book: PairBook, prm, fees=fee_per_share) -> float:
    """MAXIMUM ADDITIONAL CAPITAL REQUIRED: what the rest of the plan can still cost on its worst path (every swing
    still possible happens), each future buy at the recovery reference price plus its fee (the entry's remainder at
    the entry band's top), from the inventory held now. Under the target sizing each future swing's size is the one
    that would lift its side's win above the target at that price, from the inventory and cost it would start from."""
    plan = st.plan
    if st.phase is Phase.RESOLUTION:
        return 0.0
    p = float(plan["recovery_price"])
    unit = p + fees(p)
    q = {"UP": book.up_qty, "DOWN": book.down_qty}
    cost = book.total_cost - book.realised_exit_pnl
    extra = 0.0
    target_sizing = plan.get("sizing", "fixed") == "target"
    target = float(plan.get("target_profit", 3.0))

    def to_target(side: str) -> float:
        need = target - (q[side] - cost)
        return 0.0 if need <= 0.0 else float(math.floor(need / (1.0 - unit)) + 1) if unit < 1.0 else 0.0

    if st.phase in (Phase.INITIAL_ENTRY, Phase.SWING_0) and st.swing_number == 0:
        left = max(0.0, float(plan["initial_shares"]) - float(st.entry.get("filled", 0.0)))
        if left >= float(prm.min_size) - 1e-9:
            pe = float(plan["entry_max"])
            extra += left * (pe + fees(pe))
            cost += left * (pe + fees(pe))
            q[st.initial_token] += left
    if st.phase in RECOVERY_PHASES and st.current_swing is not None:
        cur = st.current_swing
        left = to_target(cur["to"]) if cur.get("sizing") == "target" else max(0.0, float(cur["target"]) - float(cur["filled"]))
        if left >= float(prm.min_size) - 1e-9:
            extra += left * unit
            cost += left * unit
            q[cur["to"]] += left
    side = st.direction
    n = st.swing_number
    depth = int(plan["max_recovery_swing"])
    while st.phase is not Phase.HEDGE_MODE and n < depth:
        n += 1
        side = OTHER[side]
        sh = to_target(side) if target_sizing else float(plan["swing_shares"][n - 1])
        extra += sh * unit
        cost += sh * unit
        q[side] += sh
    if plan["two_sided_hedge"]:
        gap = abs(q["UP"] - q["DOWN"])
        if gap >= float(prm.min_size) - 1e-9:
            extra += gap * unit
    return extra


def plan_path(prm, fees=fee_per_share) -> list[dict]:
    """The worst path at the reference prices (entry at the band's top, every swing and the hedge at the recovery
    price): what each step buys and the inventory / cost after it. For the start-up check and the documentation."""
    pe, p = float(prm.swing_entry_max), float(prm.swing_recovery_price)
    n0 = float(prm.swing_initial_shares)
    q = {"A": n0, "B": 0.0}
    cost = n0 * (pe + fees(pe))
    rows = [{"step": "swing 0", "side": "A", "shares": n0, "up_to": dict(q), "cost": cost}]
    side = "A"
    for n in range(1, max(0, min(int(prm.swing_max_recovery), MAX_DEPTH)) + 1):
        side = "B" if side == "A" else "A"
        if getattr(prm, "swing_sizing", "fixed") == "target":
            need = float(prm.swing_target_profit) - (q[side] - cost)
            per = 1.0 - p - fees(p)
            sh = float(math.floor(need / per) + 1) if need > 0 and per > 0 else 0.0
        else:
            sh = float(prm.swing1_shares if n == 1 else prm.swing2_shares)
        q[side] += sh
        cost += sh * (p + fees(p))
        rows.append({"step": f"swing {n}", "side": side, "shares": sh, "up_to": dict(q), "cost": cost})
    if prm.swing_two_sided_hedge:
        light = "B" if q["A"] > q["B"] else "A"
        gap = abs(q["A"] - q["B"])
        q[light] += gap
        cost += gap * (p + fees(p))
        rows.append({"step": "hedge", "side": light, "shares": gap, "up_to": dict(q), "cost": cost})
    return rows


def accounting(st: SwingState, book: PairBook, prm, mv: MarketView | None = None, fees=fee_per_share) -> dict:
    """Every per-market figure the spec lists (item 10), both resolution branches (item 11) and the mark-to-market at
    the executable bids (net of the sell fee), tracked directly from the fills -- never rebuilt from logs."""
    up, dn = book.up_qty, book.down_qty
    upc, dnc, tc, r = book.up_total_cost, book.down_total_cost, book.total_cost, book.realised_exit_pnl
    out = {"swing_number": st.swing_number, "bucket": bucket(st.swing_number), "phase": st.phase.value,
           "initial_token": st.initial_token, "direction": st.direction, "current_leader": st.current_leader,
           "up_shares": up, "down_shares": dn, "up_cost": upc, "down_cost": dnc, "total_cost": tc,
           "average_up_cost": upc / up if up > 1e-9 else None, "average_down_cost": dnc / dn if dn > 1e-9 else None,
           "last_swing_timestamp": st.last_swing_t, "last_swing_price": st.last_swing_price,
           "hedge_mode": st.hedge_mode, "recovery_mode": st.recovery_mode,
           "realized_pnl": (st.resolution or {}).get("pnl", r) if st.resolution else r,
           "payout_if_up_wins": up, "payout_if_down_wins": dn,
           "pnl_if_up_wins": up - tc + r, "pnl_if_down_wins": dn - tc + r,
           "theoretical_resolution_pnl": {"UP": up - tc + r, "DOWN": dn - tc + r},
           "maximum_additional_capital_required": projected_additional_capital(st, book, prm, fees),
           "capital_spent": book.cash_spent, "max_market_capital": float(prm.swing_max_market_capital),
           "capital_room": float(prm.swing_max_market_capital) - book.cash_spent,
           "swings": [{k: s.get(k) for k in ("n", "from", "to", "ask", "action", "target", "filled", "vwap", "cost")}
                      for s in st.swings],
           "hedge": None if st.hedge is None else {k: st.hedge.get(k) for k in ("started_swing", "orders", "filled", "vwap", "cost")}}
    if mv is not None:
        bu, bd = mv.bid_up, mv.bid_down
        if bu is not None and bd is not None:
            val = up * bu + dn * bd - up * fees(bu) - dn * fees(bd)
            out.update({"mark_to_market_pnl": val - tc + r, "mtm_up_bid": bu, "mtm_down_bid": bd})
        mu, md = _mid(mv.bid_up, mv.ask_up), _mid(mv.bid_down, mv.ask_down)
        if mu is not None and md is not None:
            out["mid_value_pnl"] = up * mu + dn * md - tc + r
    return out


def label(st: SwingState, book: PairBook, prm) -> str:
    """One line for the dashboard's open-position table: the swing, the inventory and both outcomes."""
    tc = book.total_cost
    return (f"swing {st.swing_number} ({st.phase.value}); A={st.initial_token}, betting {st.direction}, leader "
            f"{st.current_leader or '-'}; UP {book.up_qty:g} / DOWN {book.down_qty:g}, cost {tc:.2f} of "
            f"{float(prm.swing_max_market_capital):.0f}; UP-win {book.up_qty - tc:+.2f} / DOWN-win {book.down_qty - tc:+.2f}; "
            f"max more {projected_additional_capital(st, book, prm):.2f}")
