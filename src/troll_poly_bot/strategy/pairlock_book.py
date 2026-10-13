"""Pair-lock accounting, state machine and hedge optimizer (2026-10-02 brief). Pure: no I/O, no clock.

ACCOUNTING (per market cycle; the authoritative inventory is the list of FILLS, never orders or prices)

    every fill:  market_id, ts, token, side, action, qty, price, gross_cost = qty * price, fee, slippage,
                 order_id, fill_id
    up_qty / down_qty                      shares held (buys - sells)
    up_gross_cost / down_gross_cost        qty * execution price of the shares held (average-cost basis)
    up_fees / down_fees                    taker fees of the shares held
    up_slippage / down_slippage            execution price - expected price, x qty: MEASURED and reported, but it is
                                           already inside gross_cost (the price paid). Adding it to the cash cost
                                           would count it twice and break the cash reconciliation, so the cash
                                           part of slippage (`*_slippage_cash`) is 0 on this venue.
    up_total_cost = up_gross_cost + up_fees + up_slippage_cash          (same for down)
    total_cost    = up_total_cost + down_total_cost
    up_avg_cost   = up_gross_cost / up_qty                              (fees NOT inside the average)

    matched_qty      = min(up_qty, down_qty)
    unmatched_up     = max(up_qty - down_qty, 0);  unmatched_down = max(down_qty - up_qty, 0)
    matched_pair_cost = (up_avg + down_avg) * matched + allocated fees (+ allocated slippage_cash)
    pair_edge        = 1 - up_avg - down_avg
    net_pair_edge    = pair_edge - allocated costs per matched share
    locked_pair_pnl  = matched - matched_pair_cost = matched * net_pair_edge
    UP-resolution P&L   = up_qty   - total_cost + realised_exit_pnl
    DOWN-resolution P&L = down_qty - total_cost + realised_exit_pnl
    worst-case P&L      = min of the two = matched_qty - total_cost + realised_exit_pnl   (tested identity)
    mark-to-market      = up_qty * up_bid + down_qty * down_bid - total_cost  (executable bids; NOT "locked")
    expected settlement = p_up * up_qty + (1 - p_up) * down_qty - total_cost

PENDING orders are kept apart and never count as inventory; a partial fill adds exactly what filled.

DECISION COSTS: every hedge candidate is priced at the opposite token's executable VWAP over the live ask levels
(depth haircut for faster traders) PLUS a per-share slippage allowance, with the taker fee of every level. The
fill price later recorded already contains the realised slippage, so the ACCOUNTING never adds slippage twice.

STATES (validated transitions only, see TRANSITIONS; 2026-10-02 recovery-hedge / profit-lock spec):

    WAITING -> INITIAL_ENTRY -> DIRECTIONAL_POSITION -> ADVERSE_MOVE -> REVERSAL_CONFIRMED -> RECOVERY_EVALUATION
      -> RECOVERY_HEDGE_PENDING -> RECOVERY_HEDGED -> PAIR_LOCKED | UNMATCHED_DIRECTIONAL
    DIRECTIONAL_POSITION | UNMATCHED_DIRECTIONAL -> PROFIT_LOCK_EVALUATION -> PROFIT_LOCK_PENDING
      -> PAIR_LOCKED | UNMATCHED_DIRECTIONAL
    any state holding inventory -> SETTLEMENT_PENDING -> MARKET_RESOLVED -> SETTLED

    PAIR_LOCKED            fully matched (unmatched under the venue minimum) and a PROFITABLE lock: net pair edge
                           >= MIN_PAIR_EDGE and locked P&L >= MIN_LOCKED_PROFIT. Held to settlement, nothing else.
    RECOVERY_HEDGED        hedged inventory that is not a profitable lock (the matched part's P&L is fixed, possibly
                           a loss) and has no hedgeable remainder. Held to settlement.
    UNMATCHED_DIRECTIONAL  matched pairs PLUS an unmatched remainder >= the venue minimum: only the matched part is
                           locked; the remainder stays directional and is still managed.
    No transition leads from a state holding inventory back to WAITING / INITIAL_ENTRY: a market that holds
    inventory never starts another cycle (REENTRY_BLOCKED, pairlock.entry_decision).
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum

FEE_RATE = 0.07
EPS = 1e-9


def fee_per_share(price: float, rate: float = FEE_RATE) -> float:
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return rate * price * (1.0 - price)


# ───────────────────────────────── fills and the book ─────────────────────────────────


@dataclass(frozen=True)
class FillRecord:
    market_id: str
    ts: float
    token: str
    side: str                 # "UP" | "DOWN"
    qty: float
    price: float
    fee: float
    order_id: str = ""
    fill_id: str = ""
    action: str = "BUY"       # "BUY" | "SELL"
    expected_price: float | None = None
    kind: str = "entry"       # entry | hedge | exit

    @property
    def gross_cost(self) -> float:
        return self.qty * self.price

    @property
    def slippage(self) -> float:
        """USDC lost to the price moving from the expected price (buy: paid more; sell: received less)."""
        if self.expected_price is None:
            return 0.0
        d = self.price - self.expected_price
        return self.qty * (d if self.action == "BUY" else -d)


@dataclass
class PendingOrder:
    order_id: str
    side: str
    qty: float
    limit: float
    kind: str
    ts: float = 0.0


@dataclass
class _SideAcc:
    qty: float = 0.0
    gross: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    slippage_cash: float = 0.0


class InvariantError(AssertionError):
    pass


class PairBook:
    """One cycle's inventory, built ONLY from fills. Recomputed after every fill."""

    def __init__(self, market_id: str = "") -> None:
        self.market_id = market_id
        self.fills: list[FillRecord] = []
        self.pending: dict[str, PendingOrder] = {}
        self._acc = {"UP": _SideAcc(), "DOWN": _SideAcc()}
        self.cash_spent = 0.0            # every buy's gross + fee
        self.exit_proceeds = 0.0         # every sell's gross - fee
        self.realised_exit_pnl = 0.0     # sells against the average cost basis

    # ---- inputs
    def add_pending(self, order: PendingOrder) -> None:
        self.pending[order.order_id] = order

    def resolve_pending(self, order_id: str) -> None:
        self.pending.pop(order_id, None)

    def add_fill(self, f: FillRecord) -> None:
        if f.qty <= 0.0:
            raise InvariantError(f"a fill of {f.qty} shares")
        if any(x.fill_id and x.fill_id == f.fill_id for x in self.fills):
            return                                            # the same fill delivered twice: once only
        a = self._acc[f.side]
        if f.action == "BUY":
            a.qty += f.qty
            a.gross += f.gross_cost
            a.fees += f.fee
            a.slippage += f.slippage
            self.cash_spent += f.gross_cost + f.fee
        else:
            if f.qty > a.qty + 1e-6:
                raise InvariantError(f"selling {f.qty} {f.side} with {a.qty} held")
            frac = f.qty / a.qty if a.qty > 0 else 1.0
            basis = frac * (a.gross + a.fees + a.slippage_cash)
            proceeds = f.gross_cost - f.fee
            self.realised_exit_pnl += proceeds - basis
            self.exit_proceeds += proceeds
            a.gross -= frac * a.gross
            a.fees -= frac * a.fees
            a.slippage -= frac * a.slippage
            a.slippage_cash -= frac * a.slippage_cash
            a.qty -= f.qty
            if a.qty < 1e-9:
                a.qty = a.gross = a.fees = a.slippage = a.slippage_cash = 0.0
        self.fills.append(f)
        self.check_invariants()

    # ---- per side
    def qty(self, side: str) -> float:
        return self._acc[side].qty

    @property
    def up_qty(self) -> float:
        return self._acc["UP"].qty

    @property
    def down_qty(self) -> float:
        return self._acc["DOWN"].qty

    @property
    def up_gross_cost(self) -> float:
        return self._acc["UP"].gross

    @property
    def down_gross_cost(self) -> float:
        return self._acc["DOWN"].gross

    @property
    def up_fees(self) -> float:
        return self._acc["UP"].fees

    @property
    def down_fees(self) -> float:
        return self._acc["DOWN"].fees

    @property
    def up_slippage(self) -> float:
        return self._acc["UP"].slippage

    @property
    def down_slippage(self) -> float:
        return self._acc["DOWN"].slippage

    def total_cost_side(self, side: str) -> float:
        a = self._acc[side]
        return a.gross + a.fees + a.slippage_cash

    @property
    def up_total_cost(self) -> float:
        return self.total_cost_side("UP")

    @property
    def down_total_cost(self) -> float:
        return self.total_cost_side("DOWN")

    @property
    def total_cost(self) -> float:
        return self.up_total_cost + self.down_total_cost

    @property
    def fees(self) -> float:
        return self.up_fees + self.down_fees

    @property
    def slippage(self) -> float:
        return self.up_slippage + self.down_slippage

    def avg_cost(self, side: str) -> float | None:
        a = self._acc[side]
        return a.gross / a.qty if a.qty > EPS else None

    @property
    def up_avg_cost(self) -> float | None:
        return self.avg_cost("UP")

    @property
    def down_avg_cost(self) -> float | None:
        return self.avg_cost("DOWN")

    # ---- pairs
    @property
    def matched_qty(self) -> float:
        return min(self.up_qty, self.down_qty)

    @property
    def unmatched_up(self) -> float:
        return max(self.up_qty - self.down_qty, 0.0)

    @property
    def unmatched_down(self) -> float:
        return max(self.down_qty - self.up_qty, 0.0)

    @property
    def unmatched(self) -> float:
        return abs(self.up_qty - self.down_qty)

    @property
    def heavy_side(self) -> str | None:
        """The side carrying the directional exposure (None when matched or flat)."""
        if self.up_qty > self.down_qty + EPS:
            return "UP"
        if self.down_qty > self.up_qty + EPS:
            return "DOWN"
        return None

    def _allocated_costs(self) -> float:
        """Fees (+ cash slippage) of the matched shares: each side's costs pro rata to matched / held."""
        m = self.matched_qty
        if m <= EPS:
            return 0.0
        out = 0.0
        for s in ("UP", "DOWN"):
            a = self._acc[s]
            out += (a.fees + a.slippage_cash) * (m / a.qty)
        return out

    @property
    def allocated_pair_fees(self) -> float:
        m = self.matched_qty
        return sum(self._acc[s].fees * (m / self._acc[s].qty) for s in ("UP", "DOWN")) if m > EPS else 0.0

    @property
    def matched_pair_cost(self) -> float:
        m = self.matched_qty
        if m <= EPS:
            return 0.0
        return (self.up_avg_cost + self.down_avg_cost) * m + self._allocated_costs()

    @property
    def pair_cost_per_share(self) -> float | None:
        """Effective cost of one matched pair, fees included."""
        m = self.matched_qty
        return self.matched_pair_cost / m if m > EPS else None

    @property
    def pair_edge(self) -> float | None:
        """1 - up_avg - down_avg: the gross edge per matched pair (before fees)."""
        if self.matched_qty <= EPS:
            return None
        return 1.0 - self.up_avg_cost - self.down_avg_cost

    @property
    def net_pair_edge(self) -> float | None:
        m = self.matched_qty
        if m <= EPS:
            return None
        return self.pair_edge - self._allocated_costs() / m

    @property
    def locked_pair_pnl(self) -> float:
        """Settlement value minus the cost of the MATCHED pairs only: what every outcome pays on them."""
        m = self.matched_qty
        return m - self.matched_pair_cost if m > EPS else 0.0

    # ---- outcomes
    def resolution_pnl(self, winner: str) -> float:
        return self.qty(winner) - self.total_cost + self.realised_exit_pnl

    @property
    def up_resolution_pnl(self) -> float:
        return self.resolution_pnl("UP")

    @property
    def down_resolution_pnl(self) -> float:
        return self.resolution_pnl("DOWN")

    @property
    def worst_case_pnl(self) -> float:
        return min(self.up_resolution_pnl, self.down_resolution_pnl)

    def liquidation_value(self, up_bid: float | None, down_bid: float | None) -> float:
        return self.up_qty * (up_bid or 0.0) + self.down_qty * (down_bid or 0.0)

    def mark_to_market_pnl(self, up_bid: float | None, down_bid: float | None) -> float:
        """Selling everything at the executable bids now (before the exit fee), minus the cost. NOT locked."""
        return self.liquidation_value(up_bid, down_bid) - self.total_cost + self.realised_exit_pnl

    def expected_settlement_pnl(self, p_up: float) -> float:
        return p_up * self.up_qty + (1.0 - p_up) * self.down_qty - self.total_cost + self.realised_exit_pnl

    def settlement_value(self, winner: str) -> float:
        return self.qty(winner)

    def final_pnl_from_cash(self, winner: str) -> float:
        """The reconciliation's independent figure: settlement proceeds - cash spent + exit proceeds."""
        return self.settlement_value(winner) - self.cash_spent + self.exit_proceeds

    # ---- integrity
    def check_invariants(self) -> None:
        up, dn = self.up_qty, self.down_qty
        if up < -1e-9 or dn < -1e-9:
            raise InvariantError(f"negative inventory up {up} down {dn}")
        if abs(self.matched_qty - min(up, dn)) > 1e-9:
            raise InvariantError("matched_qty != min(up_qty, down_qty)")
        if abs(self.unmatched_up - max(up - dn, 0.0)) > 1e-9 or abs(self.unmatched_down - max(dn - up, 0.0)) > 1e-9:
            raise InvariantError("unmatched quantities")
        if self.total_cost < -1e-9:
            raise InvariantError("negative total cost")
        if abs(self.worst_case_pnl - (self.matched_qty - self.total_cost + self.realised_exit_pnl)) > 1e-6:
            raise InvariantError("worst-case identity")
        filled_up = sum(f.qty * (1 if f.action == "BUY" else -1) for f in self.fills if f.side == "UP")
        filled_dn = sum(f.qty * (1 if f.action == "BUY" else -1) for f in self.fills if f.side == "DOWN")
        if abs(filled_up - up) > 1e-6 or abs(filled_dn - dn) > 1e-6:
            raise InvariantError("inventory differs from the fills")

    def summary(self, up_bid=None, down_bid=None, p_up=None) -> dict:
        r = lambda v: None if v is None else round(v, 6)   # noqa: E731
        d = {"up_qty": r(self.up_qty), "down_qty": r(self.down_qty), "matched_qty": r(self.matched_qty),
             "unmatched_up": r(self.unmatched_up), "unmatched_down": r(self.unmatched_down),
             "up_avg_cost": r(self.up_avg_cost), "down_avg_cost": r(self.down_avg_cost),
             "pair_cost": r(self.pair_cost_per_share), "pair_edge": r(self.pair_edge),
             "net_pair_edge": r(self.net_pair_edge), "locked_pair_pnl": r(self.locked_pair_pnl),
             "up_resolution_pnl": r(self.up_resolution_pnl), "down_resolution_pnl": r(self.down_resolution_pnl),
             "worst_case_pnl": r(self.worst_case_pnl), "total_cost": r(self.total_cost), "fees": r(self.fees),
             "slippage": r(self.slippage), "cash_spent": r(self.cash_spent),
             "pending": [asdict(p) for p in self.pending.values()]}
        if up_bid is not None or down_bid is not None:
            d["mark_to_market_pnl"] = r(self.mark_to_market_pnl(up_bid, down_bid))
        if p_up is not None:
            d["expected_pnl"] = r(self.expected_settlement_pnl(p_up))
        return d

    # ---- persistence: the fills ARE the state
    def to_json(self) -> dict:
        return {"market_id": self.market_id, "fills": [asdict(f) for f in self.fills],
                "pending": [asdict(p) for p in self.pending.values()]}

    @classmethod
    def from_json(cls, d: dict) -> "PairBook":
        b = cls(d.get("market_id", ""))
        for f in d.get("fills") or []:
            b.add_fill(FillRecord(**f))
        for p in d.get("pending") or []:
            b.add_pending(PendingOrder(**p))
        return b


# ───────────────────────────────── pair lock ─────────────────────────────────


def pair_lock_check(book: PairBook, min_pair_edge: float, min_locked_profit: float) -> tuple[bool, str]:
    """Every condition of a valid PAIR_LOCKED (brief item 21), with the first that fails."""
    m = book.matched_qty
    if m <= EPS:
        return False, "no matched quantity"
    if abs(book.up_qty - book.down_qty) > 1e-6:
        return False, f"not fully matched (UP {book.up_qty:g} / DOWN {book.down_qty:g})"
    if book.pending:
        return False, f"{len(book.pending)} order(s) pending"
    if any(f.action == "BUY" and f.fee < 0 for f in book.fills):
        return False, "a fill without its fee"
    ne = book.net_pair_edge
    if ne is None or ne < min_pair_edge - 1e-12:
        return False, f"net pair edge {ne:+.4f} < {min_pair_edge:+.4f}"
    if book.locked_pair_pnl < min_locked_profit - 1e-12:
        return False, f"locked P&L {book.locked_pair_pnl:+.4f} < {min_locked_profit:+.4f}"
    return True, ""


# ───────────────────────────────── states ─────────────────────────────────


class State(str, Enum):
    WAITING = "WAITING"                                   # no inventory, nothing in flight
    INITIAL_ENTRY = "INITIAL_ENTRY"                       # the first leg's order in flight
    DIRECTIONAL_POSITION = "DIRECTIONAL_POSITION"         # one side held, not materially losing
    ADVERSE_MOVE = "ADVERSE_MOVE"                         # materially losing (RECOVERY_MIN_LOSS + _MIN_ADVERSE_MOVE)
    REVERSAL_CONFIRMED = "REVERSAL_CONFIRMED"             # adverse AND a confirmed reversal (k of n signals, held)
    RECOVERY_EVALUATION = "RECOVERY_EVALUATION"           # the recovery-hedge candidates being priced
    RECOVERY_HEDGE_PENDING = "RECOVERY_HEDGE_PENDING"     # a recovery hedge order in flight
    RECOVERY_HEDGED = "RECOVERY_HEDGED"                   # hedged, not a profitable lock, nothing left to hedge
    PROFIT_LOCK_EVALUATION = "PROFIT_LOCK_EVALUATION"     # the profit-lock candidates being priced
    PROFIT_LOCK_PENDING = "PROFIT_LOCK_PENDING"           # a profit-lock order in flight
    PAIR_LOCKED = "PAIR_LOCKED"                           # fully matched, profitable lock
    UNMATCHED_DIRECTIONAL = "UNMATCHED_DIRECTIONAL"       # matched pairs + a directional remainder
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"             # the market closed; waiting for the venue outcome
    MARKET_RESOLVED = "MARKET_RESOLVED"                   # the outcome is known
    SETTLED = "SETTLED"                                   # P&L booked and reconciled


S = State
#: where a cycle watches its position from (the inventory and the signals decide which, see pairlock.watch_state)
WATCH_STATES = frozenset({S.DIRECTIONAL_POSITION, S.ADVERSE_MOVE, S.REVERSAL_CONFIRMED, S.UNMATCHED_DIRECTIONAL})
#: hedged and held to settlement: no further decision
HELD_STATES = frozenset({S.PAIR_LOCKED, S.RECOVERY_HEDGED})
_W = WATCH_STATES
TRANSITIONS: dict[State, frozenset[State]] = {
    S.WAITING: frozenset({S.INITIAL_ENTRY}),
    # filled -> DIRECTIONAL_POSITION; killed / never sent -> WAITING (no inventory: a new attempt may follow)
    S.INITIAL_ENTRY: frozenset({S.DIRECTIONAL_POSITION, S.WAITING, S.SETTLEMENT_PENDING}),
    S.DIRECTIONAL_POSITION: frozenset({S.ADVERSE_MOVE, S.PROFIT_LOCK_EVALUATION, S.SETTLEMENT_PENDING}),
    S.ADVERSE_MOVE: frozenset({S.DIRECTIONAL_POSITION, S.UNMATCHED_DIRECTIONAL, S.REVERSAL_CONFIRMED,
                               S.SETTLEMENT_PENDING}),
    S.REVERSAL_CONFIRMED: frozenset({S.RECOVERY_EVALUATION, S.ADVERSE_MOVE, S.DIRECTIONAL_POSITION,
                                     S.UNMATCHED_DIRECTIONAL, S.SETTLEMENT_PENDING}),
    # accepted -> the order; rejected -> back to where the inventory and the signals say
    S.RECOVERY_EVALUATION: frozenset({S.RECOVERY_HEDGE_PENDING, S.SETTLEMENT_PENDING} | _W),
    # filled (any quantity) -> RECOVERY_HEDGED; not filled -> back to watching
    S.RECOVERY_HEDGE_PENDING: frozenset({S.RECOVERY_HEDGED, S.SETTLEMENT_PENDING} | _W),
    S.RECOVERY_HEDGED: frozenset({S.PAIR_LOCKED, S.UNMATCHED_DIRECTIONAL, S.SETTLEMENT_PENDING}),
    S.PROFIT_LOCK_EVALUATION: frozenset({S.PROFIT_LOCK_PENDING, S.SETTLEMENT_PENDING} | _W),
    S.PROFIT_LOCK_PENDING: frozenset({S.PAIR_LOCKED, S.RECOVERY_HEDGED, S.SETTLEMENT_PENDING} | _W),
    S.UNMATCHED_DIRECTIONAL: frozenset({S.ADVERSE_MOVE, S.PROFIT_LOCK_EVALUATION, S.SETTLEMENT_PENDING}),
    S.PAIR_LOCKED: frozenset({S.SETTLEMENT_PENDING}),
    S.SETTLEMENT_PENDING: frozenset({S.MARKET_RESOLVED}),
    S.MARKET_RESOLVED: frozenset({S.SETTLED}),
    S.SETTLED: frozenset(),
}
#: v2 state names (saved before 2026-10-02 15:00) -> their place in this machine (restarts)
LEGACY_STATES = {"NO_POSITION": "WAITING", "INITIAL_ENTRY_CANDIDATE": "WAITING", "MONITORING": "DIRECTIONAL_POSITION",
                 "HEDGE_EVALUATION": "DIRECTIONAL_POSITION", "NO_HEDGE": "DIRECTIONAL_POSITION",
                 "PARTIAL_HEDGE": "DIRECTIONAL_POSITION", "FULL_HEDGE": "DIRECTIONAL_POSITION",
                 "PAIR_FORMED": "UNMATCHED_DIRECTIONAL"}


class InvalidTransition(RuntimeError):
    pass


def check_transition(old: State, new: State) -> None:
    if new is old:
        return
    if new not in TRANSITIONS[old]:
        raise InvalidTransition(f"{old.value} -> {new.value} is not a valid transition")


# ───────────────────────────────── executable prices ─────────────────────────────────


def vwap_buy(levels, qty: float, haircut: float = 0.0) -> tuple[float | None, float, list[tuple[float, float]]]:
    """Walk the ask levels (price ascending) for ``qty`` shares. ``haircut``: share of each level assumed taken by
    faster traders. Returns (VWAP or None when the visible depth cannot fill it, fee per share at that fill,
    [(price, shares)] used). The fee is the per-level fee averaged over the shares."""
    need, cost, fee, used = qty, 0.0, 0.0, []
    for lv in sorted(levels, key=lambda x: x[0]):
        p, s = float(lv[0]), float(lv[1]) * (1.0 - haircut)
        if s <= EPS:
            continue
        take = min(need, s)
        cost += take * p
        fee += take * fee_per_share(p)
        used.append((p, take))
        need -= take
        if need <= 1e-9:
            break
    if need > 1e-9 or qty <= 0:
        return None, 0.0, used
    return cost / qty, fee / qty, used


# ───────────────────────────────── hedge candidates ─────────────────────────────────


@dataclass
class HedgeCandidate:
    """Buying ``q`` shares of the light (opposite) side, simulated on the fills-based book (brief item 7, 1-13).
    Every figure is at ``price`` = executable VWAP + the slippage allowance, plus the per-level taker fee."""
    frac: float
    q: float
    vwap: float | None
    fee_per_share: float
    price: float                      # vwap + slippage allowance: what the decision assumes is paid per share
    feasible: bool                    # the generic limits (venue minimum, depth, capital, positions, market loss)
    why: str
    new_up_qty: float
    new_down_qty: float
    new_matched_qty: float
    new_unmatched_up: float
    new_unmatched_down: float
    new_total_cost: float
    new_pair_cost: float | None       # all matched pairs after the hedge, per share, fees included
    new_net_pair_edge: float | None
    new_locked_pair_pnl: float
    incr_pair_cost: float | None      # the PORTION BEING PAIRED: heavy avg cost + its fee per share + price + fee
    incr_pair_edge: float | None      # 1 - incr_pair_cost
    new_up_resolution_pnl: float
    new_down_resolution_pnl: float
    new_worst_case_pnl: float
    new_expected_pnl: float
    expected_pnl_before: float
    worst_case_pnl_before: float
    downside_reduction: float         # new worst case - worst case before
    incremental_capital: float
    unmatched_after: float

    @property
    def label(self) -> str:
        return "NO_HEDGE" if self.q <= EPS else ("FULL_HEDGE" if self.frac >= 1.0 - 1e-9 else "PARTIAL_HEDGE")

    def utility(self, risk_aversion: float) -> float:
        """expected P&L + RISK_AVERSION x downside reduction (the q = 0 candidate scores expected_pnl_before)."""
        return self.new_expected_pnl + risk_aversion * self.downside_reduction

    def brief(self) -> dict:
        r = lambda v: None if v is None else round(v, 4)   # noqa: E731
        return {"label": self.label, "q": self.q, "vwap": r(self.vwap), "price": r(self.price), "feasible": self.feasible,
                "why": self.why, "matched": r(self.new_matched_qty), "unmatched": r(self.unmatched_after),
                "total_cost": r(self.new_total_cost), "pair_cost": r(self.new_pair_cost),
                "incr_pair_cost": r(self.incr_pair_cost), "incr_pair_edge": r(self.incr_pair_edge),
                "up_res": r(self.new_up_resolution_pnl), "down_res": r(self.new_down_resolution_pnl),
                "worst": r(self.new_worst_case_pnl), "expected": r(self.new_expected_pnl),
                "downside_reduction": r(self.downside_reduction), "capital": r(self.incremental_capital)}


def evaluate_hedges(book: PairBook, p_up: float, ask_levels: list[tuple[float, float]], *,
                    fractions=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0), base_qty: float | None = None, min_size: float = 5.0,
                    haircut: float = 0.0, slippage_allowance: float = 0.0, max_market_capital: float = 1e18,
                    capital_used: float = 0.0, max_total_position: float = 1e18, shares_used: float = 0.0,
                    max_hedge_position: float = 1e18, hedge_shares_used: float = 0.0,
                    max_market_loss: float = 1e18, other_worst: float = 0.0) -> list[HedgeCandidate]:
    """Every candidate q = fraction x ``base_qty`` (default: the whole UNMATCHED quantity; never a function of the
    loss) of the light side, walked through the opposite token's executable ask levels. Only the generic limits
    are applied here; the profit-lock and recovery decisions (pairlock.py) add their own economic constraints.
    ``other_worst``: the worst case of the market's other cycles (their risk adds)."""
    heavy = book.heavy_side
    out: list[HedgeCandidate] = []
    if heavy is None:
        return out
    light = "DOWN" if heavy == "UP" else "UP"
    base = book.unmatched if base_qty is None else max(0.0, min(base_qty, book.unmatched))
    e0, w0 = book.expected_settlement_pnl(p_up), book.worst_case_pnl
    h_avg = book.avg_cost(heavy)
    h_fee_ps = (book.up_fees if heavy == "UP" else book.down_fees) / max(book.qty(heavy), EPS)
    for frac in fractions:
        q = math.floor(frac * base * 100.0 + 1e-6) / 100.0
        why = ""
        if q > EPS and q < min_size - 1e-9:
            why = "under the venue minimum"
        vw, fps, _used = (None, 0.0, []) if q <= EPS else vwap_buy(ask_levels, q, haircut)
        if q > EPS and vw is None and not why:
            why = "not enough executable depth"
        price = 0.0 if vw is None else vw + slippage_allowance
        add_cost = q * (price + fps)
        up_new = book.up_qty + (q if light == "UP" else 0.0)
        dn_new = book.down_qty + (q if light == "DOWN" else 0.0)
        tc = book.total_cost + add_cost
        m_new = min(up_new, dn_new)
        up_res = up_new - tc + book.realised_exit_pnl
        dn_res = dn_new - tc + book.realised_exit_pnl
        if m_new > EPS and q > EPS and vw is not None:
            g = {"UP": book.up_gross_cost, "DOWN": book.down_gross_cost}
            fz = {"UP": book.up_fees, "DOWN": book.down_fees}
            qq = {"UP": up_new, "DOWN": dn_new}
            g[light] += q * price
            fz[light] += q * fps
            avg = {s: g[s] / qq[s] for s in ("UP", "DOWN")}
            alloc = sum(fz[s] * m_new / qq[s] for s in ("UP", "DOWN"))
            pc = avg["UP"] + avg["DOWN"] + alloc / m_new
            npe = 1.0 - pc
            locked = m_new * npe
            ipc = h_avg + h_fee_ps + price + fps
            ipe = 1.0 - ipc
        else:
            pc, npe, locked, ipc, ipe = book.pair_cost_per_share, book.net_pair_edge, book.locked_pair_pnl, None, None
        e1 = p_up * up_new + (1.0 - p_up) * dn_new - tc + book.realised_exit_pnl
        w1 = min(up_res, dn_res)
        if not why and q > EPS:
            if capital_used + add_cost > max_market_capital + 1e-9:
                why = "market capital"
            elif shares_used + q > max_total_position + 1e-9:
                why = "total position"
            elif hedge_shares_used + q > max_hedge_position + 1e-9:
                why = "hedge position"
            elif other_worst + w1 < -max_market_loss - 1e-9 and w1 < w0 - 1e-9:
                why = "market loss limit"
        out.append(HedgeCandidate(
            frac=frac, q=q, vwap=vw, fee_per_share=fps, price=price, feasible=not why, why=why, new_up_qty=up_new,
            new_down_qty=dn_new, new_matched_qty=m_new, new_unmatched_up=max(up_new - dn_new, 0.0),
            new_unmatched_down=max(dn_new - up_new, 0.0), new_total_cost=tc, new_pair_cost=pc,
            new_net_pair_edge=npe, new_locked_pair_pnl=locked, incr_pair_cost=ipc, incr_pair_edge=ipe,
            new_up_resolution_pnl=up_res, new_down_resolution_pnl=dn_res, new_worst_case_pnl=w1, new_expected_pnl=e1,
            expected_pnl_before=e0, worst_case_pnl_before=w0, downside_reduction=w1 - w0, incremental_capital=add_cost,
            unmatched_after=abs(up_new - dn_new)))
    return out
