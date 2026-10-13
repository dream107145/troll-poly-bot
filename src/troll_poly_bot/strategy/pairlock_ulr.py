"""UNFILLED-LEG RECOVERY (ULR) for the pair-lock BTC paper bot -- the user's spec of 2026-10-05. Pure logic, no I/O.

The pair-lock strategy itself is untouched: the first leg (the conditions rule), its resting pair bid at
``pair_sum - fill`` and every other rule of strategy/pairlock_recovery.py stay as they are. This module adds ONE
separate state machine per unhedged first leg, driven by strategy/pairlock_recovery_engine.py:

    HEDGE_PENDING             the first leg filled, its pair bid rests (the hedge may still fill)
      -> HEDGED               the pair bid filled in full: the arbitrage completed, recovery never activates
      -> UNHEDGED_RECOVERY    the opposite token's ask is >= RECOVERY_MIN_MOVE above the bid's price: the hedge
                              opportunity is exhausted (RECOVERY_HEDGE_STILL_POSSIBLE until then)
    UNHEDGED_RECOVERY         eligible; a recovery entry needs ALL of: opposite ask > 0.50, the move above, opposite
                              momentum up, first-leg momentum down, the spot confirming (fresh), >= MIN_TIME_REMAINING
                              left, the expected move > spread + fees + slippage + margin, the caps
      -> RECOVERY_ENTRY_PENDING   a fill-and-kill buy of RECOVERY_INITIAL_RATIO x the unhedged shares is in flight
      -> UNHEDGED_RECOVERY        ... killed (an attempt, not an entry; at most MAX_ATTEMPTS)
    RECOVERY_OPEN             the recovery position (a TRADE, not a settlement hedge); watched every tick for
      -> RECOVERY_EXIT_PENDING    a sell: RECOVERY_TAKE_PROFIT (executable bid, fees in) | RECOVERY_STOP_LOSS |
                                  RECOVERY_SIGNAL_REVERSAL | RECOVERY_EMERGENCY_EXIT (before the close)
      -> RECOVERY_OPEN            ... the sell killed or partial: retried after the cooldown
      -> RECOVERY_CLOSED          sold out, or RECOVERY_MARKET_RESOLUTION (still held at the close: settles with it)
    one filled entry per position (MAX_RECOVERY_ENTRIES_PER_POSITION 1): no averaging down, no martingale

Accounting: the recovery's buys are PairBook fills of kind "ulr", its sells kind "ulr_exit" (action SELL), in the
same book as the first leg, so the market's P&L reconciles as before; ``settle`` splits it into the original
position's P&L (the counterfactual: what the market would have made WITHOUT the recovery) and the recovery's own
P&L; their sum is the combined P&L and the recovery's P&L IS the improvement over the counterfactual.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .pairlock_book import EPS, FillRecord, PairBook, fee_per_share, vwap_buy

OTHER = {"UP": "DOWN", "DOWN": "UP"}

HEDGE_PENDING = "HEDGE_PENDING"
HEDGED = "HEDGED"
UNHEDGED_RECOVERY = "UNHEDGED_RECOVERY"
RECOVERY_ENTRY_PENDING = "RECOVERY_ENTRY_PENDING"
RECOVERY_OPEN = "RECOVERY_OPEN"
RECOVERY_EXIT_PENDING = "RECOVERY_EXIT_PENDING"
RECOVERY_CLOSED = "RECOVERY_CLOSED"

TRANSITIONS = {
    HEDGE_PENDING: {HEDGED, UNHEDGED_RECOVERY, RECOVERY_CLOSED},        # ... CLOSED: the market resolved first
    UNHEDGED_RECOVERY: {HEDGED, RECOVERY_ENTRY_PENDING, RECOVERY_CLOSED},
    RECOVERY_ENTRY_PENDING: {UNHEDGED_RECOVERY, RECOVERY_OPEN},
    RECOVERY_OPEN: {RECOVERY_EXIT_PENDING, RECOVERY_CLOSED},
    RECOVERY_EXIT_PENDING: {RECOVERY_OPEN, RECOVERY_CLOSED},
    HEDGED: set(),
    RECOVERY_CLOSED: set(),
}

# ---- reason codes (every decision carries one; the dashboard and the logs count them)
R_HEDGE_STILL_POSSIBLE = "RECOVERY_HEDGE_STILL_POSSIBLE"
R_HEDGED = "RECOVERY_HEDGED"
R_OPPOSITE_BELOW_THRESHOLD = "RECOVERY_OPPOSITE_BELOW_THRESHOLD"
R_MOVE_TOO_SMALL = "RECOVERY_MOVE_TOO_SMALL"
R_MOMENTUM_NOT_CONFIRMED = "RECOVERY_MOMENTUM_NOT_CONFIRMED"
R_SPOT_NOT_CONFIRMED = "RECOVERY_SPOT_NOT_CONFIRMED"
R_SPOT_STALE = "RECOVERY_SPOT_STALE"
R_TIME_TOO_SHORT = "RECOVERY_TIME_TOO_SHORT"
R_EXPECTED_EDGE_TOO_SMALL = "RECOVERY_EXPECTED_EDGE_TOO_SMALL"
R_ALREADY_USED = "RECOVERY_ALREADY_USED"
R_NO_QUOTE = "RECOVERY_NO_QUOTE"
R_SIZE_TOO_SMALL = "RECOVERY_SIZE_TOO_SMALL"
R_INSUFFICIENT_DEPTH = "RECOVERY_INSUFFICIENT_DEPTH"
R_EXPOSURE_CAP = "RECOVERY_EXPOSURE_CAP"
R_BANKROLL = "RECOVERY_BANKROLL"
R_COOLDOWN = "RECOVERY_COOLDOWN"
R_ORDER_IN_FLIGHT = "RECOVERY_ORDER_IN_FLIGHT"
R_ENTRY = "RECOVERY_ENTRY"
R_ENTRY_KILLED = "RECOVERY_ENTRY_KILLED"
R_TAKE_PROFIT = "RECOVERY_TAKE_PROFIT"
R_STOP_LOSS = "RECOVERY_STOP_LOSS"
R_SIGNAL_REVERSAL = "RECOVERY_SIGNAL_REVERSAL"
R_EMERGENCY_EXIT = "RECOVERY_EMERGENCY_EXIT"
R_HOLD_TO_RESOLUTION = "RECOVERY_HOLD_TO_RESOLUTION"
R_MARKET_RESOLUTION = "RECOVERY_MARKET_RESOLUTION"
R_EXIT_KILLED = "RECOVERY_EXIT_KILLED"
R_HOLD = "RECOVERY_HOLD"
EXIT_KINDS = (R_TAKE_PROFIT, R_STOP_LOSS, R_SIGNAL_REVERSAL, R_EMERGENCY_EXIT)


@dataclass
class ULR:
    """One unhedged first leg's recovery state. ``position_id`` ties it to the pair-lock position it belongs to."""
    position_id: str
    original_leg: str
    recovery_leg: str
    state: str = HEDGE_PENDING
    leg_qty: float = 0.0                 # the first leg's shares this pair bid was for
    hedge_target: float | None = None    # the resting bid's price (the hedge level the move is measured from)
    hedge_size: float = 0.0
    hedge_filled: float = 0.0
    hedged_late: bool = False            # the pair bid filled while a recovery position was open
    attempts: int = 0                    # recovery buy orders sent
    entries: int = 0                     # ... that filled (at least partly)
    entry_oid: str = ""
    entry_qty: float = 0.0
    entry_gross: float = 0.0
    entry_fees: float = 0.0
    exit_oid: str = ""
    exit_kind: str = ""
    exit_qty: float = 0.0
    exit_proceeds: float = 0.0
    exit_fees: float = 0.0
    exit_sends: int = 0
    last_order_t: float = -1e18
    t_entry: float | None = None
    t_exit: float | None = None
    final: dict | None = None            # the settlement record (settle)
    reasons: list = field(default_factory=list)       # (t, code, detail): the last 60 decisions
    transitions: list = field(default_factory=list)

    def to(self, new: str, t: float, why: str = "") -> None:
        if new == self.state:
            return
        if new not in TRANSITIONS[self.state]:
            raise RuntimeError(f"ULR {self.state} -> {new} is not a valid transition")
        self.transitions.append((t, self.state, new, why))
        self.state = new

    def note(self, t: float, code: str, detail: str = "") -> None:
        self.reasons.append((t, code, detail))
        if len(self.reasons) > 60:
            del self.reasons[:-60]

    @property
    def entry_avg(self) -> float | None:
        return self.entry_gross / self.entry_qty if self.entry_qty > EPS else None

    @property
    def open_qty(self) -> float:
        return max(0.0, self.entry_qty - self.exit_qty)

    @property
    def unhedged_qty(self) -> float:
        return max(0.0, self.leg_qty - self.hedge_filled)

    @property
    def realized_pnl(self) -> float:
        """Sells against the entry's average cost, fees in (what is still held is not in it)."""
        if self.exit_qty <= EPS:
            return 0.0
        return self.exit_proceeds - self.exit_fees - self.exit_qty * (self.entry_gross + self.entry_fees) / self.entry_qty

    def to_json(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k not in ("reasons", "transitions")}
        d["reasons"] = list(self.reasons[-20:])
        d["transitions"] = list(self.transitions[-20:])
        return d

    @classmethod
    def from_json(cls, d: dict, restart: bool = False) -> "ULR":
        kw = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        kw["reasons"] = [tuple(x) for x in d.get("reasons") or []]
        kw["transitions"] = [tuple(x) for x in d.get("transitions") or []]
        u = cls(**kw)
        if restart:
            # an order in flight at the stop resolved while we were down: the book's fills say what we hold
            if u.state == RECOVERY_ENTRY_PENDING:
                u.state = RECOVERY_OPEN if u.open_qty > EPS else UNHEDGED_RECOVERY
            elif u.state == RECOVERY_EXIT_PENDING:
                u.state = RECOVERY_OPEN if u.open_qty > EPS else RECOVERY_CLOSED
        return u


# ───────────────────────────── the hedge leg ─────────────────────────────


def start(position_id: str, original_leg: str, leg_qty: float) -> ULR:
    return ULR(position_id=position_id, original_leg=original_leg, recovery_leg=OTHER[original_leg], leg_qty=float(leg_qty))


def hedge_placed(u: ULR, price: float, size: float, t: float) -> None:
    u.hedge_target, u.hedge_size = float(price), float(size)
    u.note(t, R_HEDGE_STILL_POSSIBLE, f"pair bid {size:g} {u.recovery_leg} at {price:.2f} resting")


def hedge_skipped(u: ULR, why: str, t: float) -> None:
    """No pair bid could be placed (price under a cent / size): the hedge opportunity never existed."""
    u.hedge_size = u.leg_qty
    u.to(UNHEDGED_RECOVERY, t, f"no hedge: {why}")


def hedge_filled(u: ULR, qty: float, t: float) -> None:
    u.hedge_filled += float(qty)
    full = u.hedge_size > EPS and u.hedge_filled + 1e-6 >= u.hedge_size
    if u.state in (HEDGE_PENDING, UNHEDGED_RECOVERY):
        if full:
            u.to(HEDGED, t, "pair bid filled: the arbitrage completed")
            u.note(t, R_HEDGED, f"{u.hedge_filled:g} of {u.hedge_size:g} hedged")
    elif u.state in (RECOVERY_OPEN, RECOVERY_EXIT_PENDING, RECOVERY_ENTRY_PENDING):
        u.hedged_late = True
        u.note(t, R_HEDGED, f"pair bid filled {qty:g} while a recovery of {u.open_qty:g} is open")


# ───────────────────────────── signals ─────────────────────────────


def _mid(bid, ask):
    if bid is not None and ask is not None and not (math.isnan(bid) or math.isnan(ask)):
        return 0.5 * (bid + ask)
    return None


def momentum(hist, side: str, now: float, window_ms: float) -> float | None:
    """The token's mid now minus its mid ``window_ms`` earlier, from the engine's sampled History (t, ub, ua, db, da).
    None without a sample at least half the window old."""
    if hist is None or len(hist.t) < 2:
        return None
    bids, asks = (hist.ub, hist.ua) if side == "UP" else (hist.db, hist.da)
    now_mid = _mid(bids[-1], asks[-1])
    then = None
    for i in range(len(hist.t) - 1, -1, -1):
        if hist.t[i] <= now - window_ms:
            then = _mid(bids[i], asks[i])
            break
        if hist.t[i] <= now - 0.5 * window_ms:
            then = _mid(bids[i], asks[i])          # the oldest sample within reach, if it is old enough
    if now_mid is None or then is None:
        return None
    return now_mid - then


def spot_delta(spot_hist, now: float, window_ms: float) -> float | None:
    """The spot price now minus ``window_ms`` earlier (the engine's spot History); None if too little history."""
    if spot_hist is None or len(spot_hist.t) < 2:
        return None
    px = spot_hist.spot
    if px[-1] is None:
        return None
    then = None
    for i in range(len(spot_hist.t) - 1, -1, -1):
        if spot_hist.t[i] <= now - window_ms and px[i] is not None:
            then = px[i]
            break
        if spot_hist.t[i] <= now - 0.5 * window_ms and px[i] is not None:
            then = px[i]
    return None if then is None else float(px[-1]) - float(then)


# ───────────────────────────── the entry decision ─────────────────────────────


def recovery_size(u: ULR, prm, min_size: float) -> tuple[float, str]:
    """RECOVERY_INITIAL_RATIO x the unhedged shares, rounded down to a cent of a share, never under the venue's
    minimum (the minimum is used instead when it fits inside the unhedged shares) and never over the cap."""
    q = math.floor(u.unhedged_qty * prm.ulr_initial_ratio * 100.0 + 1e-9) / 100.0
    if q < min_size - 1e-9:
        if min_size <= u.unhedged_qty + 1e-9:
            q = float(min_size)
        else:
            return 0.0, f"{prm.ulr_initial_ratio:g} x {u.unhedged_qty:g} unhedged = {q:g} sh < the venue minimum {min_size:g}"
    q = min(q, float(prm.ulr_max_position_size))
    return q, ""


def evaluate_entry(u: ULR, book: PairBook, mv, now: float, left_s: float, prm, mom_opp: float | None,
                   mom_orig: float | None, spot_d: float | None, spot_age_ms: float | None, max_spot_age_ms: float,
                   bankroll_free: float, open_exposure: float, min_size: float = 5.0) -> tuple[dict | None, dict]:
    """One sample of an unhedged first leg: (the buy to send or None, the record with its reason code)."""
    S, O = u.recovery_leg, u.original_leg
    ask, bid = mv.ask(S), mv.bid(S)
    rec = {"t": now, "kind": "ulr", "position_id": u.position_id, "state": u.state, "original_leg": O, "recovery_leg": S,
           "time_left": left_s, "opp_ask": ask, "opp_bid": bid, "hedge_target": u.hedge_target, "mom_opp": mom_opp,
           "mom_orig": mom_orig, "spot_delta": spot_d, "spot_age_ms": spot_age_ms, "unhedged_qty": u.unhedged_qty,
           "decision": "REJECTED", "code": "", "why": ""}

    def reject(code: str, why: str) -> tuple[None, dict]:
        rec["code"], rec["why"] = code, why
        u.note(now, code, why)
        return None, rec

    if u.state == HEDGED:
        return reject(R_HEDGED, "the pair bid filled: nothing to recover")
    if u.state in (RECOVERY_ENTRY_PENDING, RECOVERY_EXIT_PENDING):
        return reject(R_ORDER_IN_FLIGHT, f"{u.state}")
    if u.state in (RECOVERY_OPEN, RECOVERY_CLOSED) or u.entries >= prm.ulr_max_entries:
        return reject(R_ALREADY_USED, f"{u.entries} recovery entr{'y' if u.entries == 1 else 'ies'} on this position "
                                      f"(MAX_RECOVERY_ENTRIES_PER_POSITION {prm.ulr_max_entries})")
    if u.attempts >= prm.ulr_max_attempts:
        return reject(R_ALREADY_USED, f"{u.attempts} recovery orders sent (MAX_ATTEMPTS {prm.ulr_max_attempts})")
    if u.entries >= prm.ulr_max_trades_per_market:
        return reject(R_ALREADY_USED, f"MAX_RECOVERY_TRADES_PER_MARKET {prm.ulr_max_trades_per_market}")
    if ask is None or bid is None:
        return reject(R_NO_QUOTE, f"no two-sided {S} quote")
    if u.hedge_target is None:
        return reject(R_HEDGE_STILL_POSSIBLE, "the pair bid is not placed yet")
    if now - u.last_order_t < prm.ulr_cooldown_ms:
        return reject(R_COOLDOWN, f"{now - u.last_order_t:.0f} ms since the last recovery order")
    # the hedge opportunity: exhausted once the opposite ask is RECOVERY_MIN_MOVE above the bid's price
    move = ask - u.hedge_target
    rec["move"] = move
    if move < prm.ulr_min_move - 1e-9:
        return reject(R_HEDGE_STILL_POSSIBLE, f"{S} ask {ask:.2f} is {move:+.3f} from the pair bid {u.hedge_target:.2f} "
                                              f"(RECOVERY_MIN_MOVE {prm.ulr_min_move:.2f}): the hedge may still fill")
    if u.state == HEDGE_PENDING:
        u.to(UNHEDGED_RECOVERY, now, f"{S} ask {ask:.2f} >= pair bid {u.hedge_target:.2f} + {prm.ulr_min_move:.2f}")
    if left_s < prm.ulr_min_time_remaining_s:
        return reject(R_TIME_TOO_SHORT, f"{left_s:.0f} s left < RECOVERY_MIN_TIME_REMAINING {prm.ulr_min_time_remaining_s:g}")
    # A. the opposite token above the threshold
    if ask <= prm.ulr_opposite_min_price + 1e-9:
        return reject(R_OPPOSITE_BELOW_THRESHOLD, f"{S} ask {ask:.2f} <= {prm.ulr_opposite_min_price:.2f}")
    # C./D. momentum: the opposite rising, the first leg weakening
    if mom_opp is None or mom_orig is None:
        return reject(R_MOMENTUM_NOT_CONFIRMED, f"no {prm.ulr_momentum_window_s:g} s price history yet")
    if mom_opp < prm.ulr_momentum_min - 1e-12 or mom_orig > -prm.ulr_momentum_min + 1e-12:
        return reject(R_MOMENTUM_NOT_CONFIRMED, f"{S} {mom_opp:+.3f} / {O} {mom_orig:+.3f} over {prm.ulr_momentum_window_s:g} s "
                                                f"(need >= +{prm.ulr_momentum_min:.2f} and <= -{prm.ulr_momentum_min:.2f})")
    # E. spot confirms the first leg weakening (UP leg: spot falling; DOWN leg: spot rising), fresh data only
    if prm.ulr_require_spot:
        if spot_age_ms is None or spot_age_ms > max_spot_age_ms or spot_d is None:
            return reject(R_SPOT_STALE, f"spot age {spot_age_ms} ms (max {max_spot_age_ms:g}) / no spot history")
        if (O == "UP" and spot_d >= 0.0) or (O == "DOWN" and spot_d <= 0.0):
            return reject(R_SPOT_NOT_CONFIRMED, f"spot {spot_d:+.2f} over {prm.ulr_momentum_window_s:g} s does not weaken {O}")
    # size: a partial opposite position
    q, why = recovery_size(u, prm, min_size)
    if q <= 0.0:
        return reject(R_SIZE_TOO_SMALL, why)
    # the executable entry and the edge gate: the move the trade bets on must pay the round trip
    limit = min(0.99, round(ask + prm.ulr_max_slippage, 2))
    vw, fee_in, used = vwap_buy(mv.asks(S), q, 0.0)
    if vw is None or (used and max(p for p, _ in used) > limit + 1e-9):
        return reject(R_INSUFFICIENT_DEPTH, f"the book does not hold {q:g} {S} within {limit:.2f}")
    spread_cost = max(0.0, vw - bid)                               # what selling straight back would lose
    fee_out = fee_per_share(min(0.99, vw + prm.ulr_expected_move))
    costs = spread_cost + fee_in + fee_out + prm.slippage_allowance + prm.ulr_safety_margin
    edge = prm.ulr_expected_move - costs
    cost = q * (vw + fee_in)
    rec.update({"q": q, "vwap": vw, "limit": limit, "spread_cost": spread_cost, "fee_in": fee_in, "fee_out": fee_out,
                "costs": costs, "expected_move": prm.ulr_expected_move, "edge": edge, "cost": cost})
    if edge < -1e-12:
        return reject(R_EXPECTED_EDGE_TOO_SMALL, f"expected move {prm.ulr_expected_move:.3f} - costs {costs:.4f} (spread "
                                                 f"{spread_cost:.3f} + fees {fee_in + fee_out:.4f} + slip {prm.slippage_allowance:.3f}"
                                                 f" + margin {prm.ulr_safety_margin:.2f}) = {edge:+.4f}/sh")
    if open_exposure + cost > prm.ulr_max_total_exposure + 1e-9:
        return reject(R_EXPOSURE_CAP, f"open recovery {open_exposure:.2f} + {cost:.2f} > MAX_RECOVERY_TOTAL_EXPOSURE "
                                      f"{prm.ulr_max_total_exposure:.2f}")
    if cost > bankroll_free + 1e-9:
        return reject(R_BANKROLL, f"cost {cost:.2f} > free bankroll {bankroll_free:.2f}")
    why = (f"unfilled-leg recovery: {O} leg {u.leg_qty:g} sh unhedged ({u.hedge_filled:g} hedged), {S} ask {ask:.2f} is "
           f"{move:+.2f} over the pair bid {u.hedge_target:.2f}, {S} {mom_opp:+.3f} / {O} {mom_orig:+.3f} in "
           f"{prm.ulr_momentum_window_s:g} s, spot {spot_d if spot_d is None else round(spot_d, 2)}; buy {q:g} sh "
           f"({prm.ulr_initial_ratio:g} x {u.unhedged_qty:g}) at VWAP {vw:.3f} <= {limit:.2f}; edge {edge:+.4f}/sh; "
           f"TP {prm.ulr_take_profit:.2f} SL {prm.ulr_stop_loss:.2f}")
    rec.update({"decision": "BUY", "code": R_ENTRY, "why": why})
    u.note(now, R_ENTRY, why)
    return {"side": S, "q": q, "limit": limit, "vwap": vw, "why": why}, rec


# ───────────────────────────── the exit decision ─────────────────────────────


def evaluate_exit(u: ULR, mv, now: float, left_s: float, prm, mom_opp: float | None, mom_orig: float | None,
                  min_left_s: float = 10.0) -> tuple[dict | None, dict]:
    """The open recovery position every tick: (the sell to send or None, the record). Prices are executable (the
    recovery token's best bid), P&L per share has both fees in."""
    S = u.recovery_leg
    bid = mv.bid(S)
    avg = u.entry_avg
    rec = {"t": now, "kind": "ulr_exit", "position_id": u.position_id, "state": u.state, "recovery_leg": S, "time_left": left_s,
           "bid": bid, "entry_avg": avg, "open_qty": u.open_qty, "mom_opp": mom_opp, "mom_orig": mom_orig,
           "decision": "HOLD", "code": R_HOLD, "why": ""}
    if u.state != RECOVERY_OPEN or u.open_qty <= EPS or avg is None:
        rec["why"] = f"state {u.state}"
        return None, rec
    if now - u.last_order_t < prm.ulr_cooldown_ms:
        rec["code"], rec["why"] = R_COOLDOWN, "a recovery order left under the cooldown ago"
        return None, rec
    if bid is None:
        rec["code"], rec["why"] = R_NO_QUOTE, f"no {S} bid"
        return None, rec
    gross = bid - avg
    fee_in_ps = u.entry_fees / u.entry_qty if u.entry_qty > EPS else 0.0
    net_ps = gross - fee_in_ps - fee_per_share(bid)
    rec.update({"gross_ps": gross, "net_ps": net_ps, "net_pnl_at_bid": net_ps * u.open_qty})
    kind, why = None, ""
    if left_s < prm.ulr_exit_before_close_s:
        if left_s < min_left_s:
            rec["code"], rec["why"] = R_HOLD_TO_RESOLUTION, f"{left_s:.0f} s left: no order under {min_left_s:g} s; settles with the market"
            u.note(now, R_HOLD_TO_RESOLUTION, rec["why"])
            return None, rec
        kind, why = R_EMERGENCY_EXIT, f"{left_s:.0f} s left < RECOVERY_MAX_TIME_TO_RESOLUTION {prm.ulr_exit_before_close_s:g}"
    elif gross >= prm.ulr_take_profit - 1e-9 and net_ps > 0.0:
        kind, why = R_TAKE_PROFIT, f"bid {bid:.2f} - entry {avg:.3f} = {gross:+.3f} >= TP {prm.ulr_take_profit:.2f}; net {net_ps:+.4f}/sh"
    elif gross <= -prm.ulr_stop_loss + 1e-9:
        kind, why = R_STOP_LOSS, f"bid {bid:.2f} - entry {avg:.3f} = {gross:+.3f} <= -SL {prm.ulr_stop_loss:.2f}"
    elif (prm.ulr_signal_exit and mom_opp is not None and mom_orig is not None
          and mom_opp <= -prm.ulr_momentum_min + 1e-12 and mom_orig >= prm.ulr_momentum_min - 1e-12):
        kind, why = R_SIGNAL_REVERSAL, (f"{S} {mom_opp:+.3f} / {u.original_leg} {mom_orig:+.3f} over "
                                        f"{prm.ulr_momentum_window_s:g} s: the recovery signal reversed")
    if kind is None:
        rec["why"] = f"bid {bid:.2f} vs entry {avg:.3f} ({gross:+.3f}): inside TP/SL, signal intact"
        return None, rec
    limit = max(0.01, round(bid - prm.ulr_max_slippage, 2))
    rec.update({"decision": "SELL", "code": kind, "why": why, "limit": limit, "q": u.open_qty})
    u.note(now, kind, why)
    return {"kind": kind, "side": S, "q": u.open_qty, "limit": limit, "why": why}, rec


# ───────────────────────────── the lifecycle ─────────────────────────────


def entry_sent(u: ULR, order_id: str, q: float, t: float) -> None:
    u.attempts += 1
    u.entry_oid = order_id
    u.last_order_t = t
    u.to(RECOVERY_ENTRY_PENDING, t, f"buy {q:g} {u.recovery_leg} sent")


def entry_filled(u: ULR, qty: float, price: float, fee: float, t: float) -> None:
    first = u.entry_qty <= EPS
    u.entry_qty += float(qty)
    u.entry_gross += float(qty) * float(price)
    u.entry_fees += float(fee)
    if first:
        u.entries += 1
        u.t_entry = t
    if u.state == RECOVERY_ENTRY_PENDING:
        u.to(RECOVERY_OPEN, t, f"filled {qty:g} @ {price:.3f}")


def entry_done(u: ULR, t: float, why: str = "") -> None:
    """The buy's result is in: nothing filled -> an attempt, back to eligible (or used up)."""
    if u.state == RECOVERY_ENTRY_PENDING:
        u.to(UNHEDGED_RECOVERY, t, f"not filled ({why})")
        u.note(t, R_ENTRY_KILLED, why)


def exit_sent(u: ULR, order_id: str, kind: str, q: float, t: float) -> None:
    u.exit_oid, u.exit_kind = order_id, kind
    u.exit_sends += 1
    u.last_order_t = t
    u.to(RECOVERY_EXIT_PENDING, t, f"{kind}: sell {q:g} {u.recovery_leg}")


def exit_filled(u: ULR, qty: float, price: float, fee: float, t: float) -> None:
    u.exit_qty += float(qty)
    u.exit_proceeds += float(qty) * float(price)
    u.exit_fees += float(fee)
    u.t_exit = t


def exit_done(u: ULR, t: float, filled_any: bool, why: str = "") -> None:
    if u.state != RECOVERY_EXIT_PENDING:
        return
    if u.open_qty <= 1e-6:
        u.to(RECOVERY_CLOSED, t, f"{u.exit_kind}: sold out")
    else:
        u.to(RECOVERY_OPEN, t, f"{u.exit_kind}: {'partly filled' if filled_any else 'not filled'} ({why}); retried")
        if not filled_any:
            u.note(t, R_EXIT_KILLED, why)


def exit_fill_record(u: ULR, slug: str, token: str, order_id: str, n: int, qty: float, price: float, fee: float,
                     t: float) -> FillRecord:
    return FillRecord(market_id=slug, ts=t, token=token, side=u.recovery_leg, qty=float(qty), price=float(price), fee=float(fee),
                      order_id=order_id, fill_id=f"{order_id}-{n}", action="SELL", expected_price=None, kind="ulr_exit")


def settle(u: ULR, book: PairBook, winner: str, t: float) -> dict:
    """The counterfactual record: the market's P&L split into the original position (what it would have made WITHOUT
    the recovery) and the recovery's own result; combined = the market's P&L; improvement = the recovery's P&L."""
    from .pairlock_recovery import ulr_leg_pnl
    total = book.resolution_pnl(winner)
    rec_pnl = ulr_leg_pnl(book.fills, winner)
    held = u.open_qty
    if u.state in (RECOVERY_OPEN, RECOVERY_EXIT_PENDING):
        u.exit_kind = u.exit_kind if u.exit_kind and u.exit_qty > EPS else R_MARKET_RESOLUTION
        u.note(t, R_MARKET_RESOLUTION, f"{held:g} {u.recovery_leg} held at the close: paid {'1' if winner == u.recovery_leg else '0'}")
        u.to(RECOVERY_CLOSED, t, "market resolved")
    elif u.state in (HEDGE_PENDING, UNHEDGED_RECOVERY):
        u.to(RECOVERY_CLOSED, t, "market resolved without a recovery")
    u.final = {"position_id": u.position_id, "winner": winner, "original_leg": u.original_leg, "recovery_leg": u.recovery_leg,
               "hedged": u.hedge_filled, "hedged_late": u.hedged_late, "recovered": u.entries > 0,
               "original_pnl": total - rec_pnl, "recovery_pnl": rec_pnl, "combined_pnl": total, "improvement": rec_pnl,
               "recovery_entry_cost": u.entry_gross, "recovery_fees": u.entry_fees + u.exit_fees,
               "recovery_exit_value": u.exit_proceeds, "recovery_realized_pnl": u.realized_pnl,
               "recovery_held_at_close": held, "exit_kind": u.exit_kind, "attempts": u.attempts, "entries": u.entries}
    return u.final
