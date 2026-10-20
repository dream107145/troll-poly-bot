"""The LEADER_PAIR state machine (the user's spec of 2026-10-13, sections 4-9), one instance per market.

Pure and deterministic: ``on_tick(view)`` sees ONLY the quotes of that moment and returns at most one order intent;
the fills come back through ``on_fill`` and the order's end through ``on_order_done``. No I/O, no clock of its own,
so the paper bot (engine.py) and the backtester (backtest.py) run the very same code.

States and transitions (docs/leader_pair_2026-10-13.md has the full table):

    INITIAL_ENTRY ---(initial order filled)-------------------------> MONITORING
    INITIAL_ENTRY ---(init_max_s passed without a fill)-------------> NO_ENTRY            (terminal)
    MONITORING / RECOVERY_STOPPED ---(P_ref < ask_opp <= ceiling)---> OPPOSITE_ACCUMULATION
    OPPOSITE_ACCUMULATION ---(ask_opp > ceiling)--------------------> MONITORING          (the sequence ended)
    MONITORING / OPPOSITE_ACCUMULATION / RECOVERY_STOPPED
                  ---(leader < reversal and falling, confirmed)------> LEADER_FALLING      (cycle += 1)
    LEADER_FALLING ---(leader rising, confirmed)--------------------> RECOVERY
    RECOVERY ---(PnL_live > recovery_stop_pnl)----------------------> RECOVERY_STOPPED
    RECOVERY / RECOVERY_STOPPED ---(leader falling again, confirmed)-> LEADER_FALLING      (cycle += 1)
    any ---(market capital or purchase cap used up)-----------------> RISK_LIMIT_STOPPED  (terminal: holds)
    any ---(close_ts reached)---------------------------------------> MARKET_CLOSED       (terminal; settles)

Timers: the accumulation slots are anchored at the initial fill; the falling-buy and recovery slots at the entry
into their state. The previous-fill reference resets only when a NEW accumulation sequence starts. Inventory,
cost and the purchase count of the market are never reset.
"""
from __future__ import annotations

import collections
import math
from dataclasses import dataclass, field
from enum import Enum

from .accounting import Portfolio, other
from .config import LeaderPairConfig
from .risk import Approval, Candidate, check

MAX_EVENTS = 400


class State(str, Enum):
    INITIAL_ENTRY = "INITIAL_ENTRY"
    MONITORING = "MONITORING"
    OPPOSITE_ACCUMULATION = "OPPOSITE_ACCUMULATION"
    LEADER_FALLING = "LEADER_FALLING"
    RECOVERY = "RECOVERY"
    RECOVERY_STOPPED = "RECOVERY_STOPPED"
    RISK_LIMIT_STOPPED = "RISK_LIMIT_STOPPED"
    MARKET_CLOSED = "MARKET_CLOSED"
    NO_ENTRY = "NO_ENTRY"
    LOCKED = "LOCKED"                    # the user 10-14: the locked profit reached lock_target_usdc -- held, no more buys


TERMINAL = (State.RISK_LIMIT_STOPPED, State.MARKET_CLOSED, State.NO_ENTRY, State.LOCKED)
HUB = (State.MONITORING, State.OPPOSITE_ACCUMULATION, State.RECOVERY_STOPPED)


@dataclass(slots=True)
class Quote:
    bid: float | None
    ask: float | None
    bid_levels: list[tuple[float, float]]     # (price, size) best first
    ask_levels: list[tuple[float, float]]
    age_ms: float = 0.0

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2.0 if self.bid is not None and self.ask is not None else None

    @property
    def spread(self) -> float | None:
        return self.ask - self.bid if self.bid is not None and self.ask is not None else None


@dataclass(slots=True)
class View:
    """What the strategy may know at ``now``: this moment's quotes and nothing later."""
    now: float
    open_ts: float
    close_ts: float
    up: Quote | None
    down: Quote | None
    fee_per_share: object                     # callable(price) -> taker fee per share
    balance: float | None = None
    accepting: bool = True
    capital_other_markets: float = 0.0
    resting_cost: float = 0.0                 # this market's unconfirmed orders, counted as committed capital
    rise: dict | None = None                  # side -> mid now - mid fix_rise_window_s ago (engine.py; None = unknown)
    peer_avg: dict | None = None               # side -> mean executable ask in the other configured crypto markets

    @property
    def seconds_left(self) -> float:
        return max(0.0, (self.close_ts - self.now) / 1000.0)

    def quote(self, side: str) -> Quote | None:
        return self.up if side == "UP" else self.down


@dataclass(slots=True)
class Intent:
    side: str
    shares: float
    kind: str                                 # init | accum | falling | recovery
    limit: float
    expected: float                           # the best ask seen
    approval: Approval
    note: str = ""


@dataclass
class Accumulation:
    seq: int = 0                              # sequences started (0 = none yet)
    active: bool = False
    prev_fill: float | None = None            # the last successful fill price of THIS sequence
    last_slot: int = -1
    fills: int = 0
    ended: str = ""


@dataclass
class MarketStrategy:
    cfg: LeaderPairConfig
    slug: str
    open_ts: float
    close_ts: float
    state: State = State.INITIAL_ENTRY
    leader: str | None = None
    p0: float | None = None
    p_ref: float | None = None
    portfolio: Portfolio = field(init=False)
    pending: dict[str, Intent] = field(default_factory=dict)      # order id -> intent (one at a time)
    sent: dict[str, tuple[str, str]] = field(default_factory=dict)  # order id -> (kind, side), kept past the order's end
    accum: Accumulation = field(default_factory=Accumulation)
    cycle: int = 0
    purchases_this_cycle: int = 0
    cycle_purchase_orders: set[str] = field(default_factory=set)
    anchor_ts: float | None = None            # the initial fill: the 5-s schedule starts here
    phase_anchor: float | None = None         # the current LEADER_FALLING / RECOVERY entry
    phase_slot: int = -1
    init_tries: int = 0
    init_last_try: float = -1e18
    consecutive_rejections: int = 0
    init_filled: float = 0.0
    init_notional: float = 0.0
    init_fee: float = 0.0
    samples: collections.deque = field(default_factory=collections.deque)
    last_sample_t: float = -1e18
    fall_n: int = 0
    rise_n: int = 0
    pnl_live: float | None = None
    recovery_stops: int = 0
    falling_events: int = 0
    recovery_events: int = 0
    end_guard: dict[str, float] = field(default_factory=dict)
    refusals: collections.Counter = field(default_factory=collections.Counter)
    events: list[dict] = field(default_factory=list)
    won_up: bool | None = None
    why_stopped: str = ""
    last_view_t: float = 0.0
    last_fix_t: float = -1e18
    fix_fills: int = 0
    leader_repair_started: bool = False       # persistent: finish the +$1 leader repair after a risk-capped partial
    cross_leader: str | None = None
    cross_leader_price: float | None = None    # actual fill price, not the displayed ask
    cross_pair_filled: bool = False
    cross_signal_filled: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.portfolio = Portfolio(pair_threshold=self.cfg.pair_threshold, matching=self.cfg.pair_matching)

    # ------------------------------------------------------------------ bookkeeping

    def _event(self, now: float, event: str, **kw) -> None:
        rec = {"t": now, "event": event, "state": self.state.value, **kw}
        self.events.append(rec)
        if len(self.events) > MAX_EVENTS:
            del self.events[: len(self.events) - MAX_EVENTS]

    def _go(self, now: float, new: State, why: str = "", **kw) -> None:
        if new is self.state:
            return
        old = self.state
        self.state = new
        self._event(now, "TRANSITION", frm=old.value, to=new.value, why=why, **kw)

    def _refuse(self, now: float, kind: str, side: str, reason: str, **kw) -> None:
        self.refusals[f"{kind}:{reason}"] += 1
        self._event(now, "REFUSED", kind=kind, side=side, reason=reason, **kw)
        if reason in ("MARKET_CAPITAL", "TOTAL_CAPITAL", "SETTLEMENT_LOSS", "BALANCE", "PURCHASES_MARKET") and self.state not in TERMINAL:
            self.why_stopped = reason
            self._go(now, State.RISK_LIMIT_STOPPED, reason)

    @property
    def opposite(self) -> str | None:
        return other(self.leader) if self.leader else None

    def _price(self, q: Quote | None) -> float | None:
        if q is None:
            return None
        return q.mid if self.cfg.signal_price == "mid" else q.bid if self.cfg.signal_price == "bid" else q.ask

    def _slot(self, anchor: float | None, interval_s: float, now: float) -> int:
        return -1 if anchor is None else int(math.floor((now - anchor) / (interval_s * 1000.0)))

    # ------------------------------------------------------------------ the signals (rules 4 and 5)

    def _sample(self, v: View) -> None:
        """Record the leader's signal price at most every signal_tick_ms and update the fall / rise confirmations."""
        q = v.quote(self.leader) if self.leader else None
        px = self._price(q)
        if q is None or px is None or v.now - self.last_sample_t < self.cfg.signal_tick_ms - 1e-9:
            return
        if q.age_ms > self.cfg.max_book_age_ms or (q.spread is not None and q.spread > self.cfg.max_signal_spread + 1e-9):
            return                                              # stale or a spread blow-out: not a price move
        self.last_sample_t = v.now
        self.samples.append((v.now, px))
        horizon = (self.cfg.signal_delta_s + 2.0) * 1000.0
        while len(self.samples) > 1 and self.samples[1][0] < v.now - horizon:
            self.samples.popleft()
        then = None
        for t, p in self.samples:
            if t <= v.now - self.cfg.signal_delta_s * 1000.0:
                then = p
            else:
                break
        if then is None:
            self.fall_n = self.rise_n = 0
            return
        falling = px < self.cfg.leader_reversal - 1e-12 and px <= then - self.cfg.fall_min_drop + 1e-12 and px < then - 1e-12
        rising = px >= then + self.cfg.recovery_min_rise - 1e-12 and px > then + 1e-12
        self.fall_n = self.fall_n + 1 if falling else 0
        self.rise_n = self.rise_n + 1 if rising else 0

    def falling_confirmed(self) -> bool:
        return self.fall_n >= max(3, self.cfg.fall_confirm_ticks)

    def rising_confirmed(self) -> bool:
        return self.rise_n >= max(3, self.cfg.recovery_confirm_ticks)

    # ------------------------------------------------------------------ figures

    def liquidation(self, v: View) -> float | None:
        up, dn = v.up, v.down
        return self.portfolio.liquidation_pnl(up.bid if up else None, dn.bid if dn else None, v.fee_per_share,
                                              self.cfg.liq_slippage, up.bid_levels if up else None, dn.bid_levels if dn else None)

    def outcomes(self) -> dict:
        t = self.portfolio.totals
        return {"q_up": round(t.q_up, 2), "q_down": round(t.q_down, 2), "c_total": round(t.c_total, 4),
                "matched": round(min(t.matched_up, t.matched_down), 2), "unmatched_up": round(t.unmatched_up, 2),
                "unmatched_down": round(t.unmatched_down, 2), "pnl_if_up": round(t.pnl_if_up, 4),
                "pnl_if_down": round(t.pnl_if_down, 4)}

    # ------------------------------------------------------------------ orders

    def _candidate(self, v: View, side: str, shares: float, kind: str) -> Intent | None:
        q = v.quote(side)
        why, ap = self._candidate_check(v, side, shares, kind)
        if why:
            self._refuse(v.now, kind, side, why, ask=q.ask if q else None, shares=shares)
            return None
        return Intent(side=side, shares=shares, kind=kind, limit=ap.limit, expected=q.ask, approval=ap)

    def on_order_sent(self, order_id: str, intent: Intent, now: float) -> None:
        self.pending[order_id] = intent
        self.sent[order_id] = (intent.kind, intent.side)
        if intent.kind == "init":
            self.init_tries += 1
            self.init_last_try = now
        self._event(now, "ORDER", id=order_id, kind=intent.kind, side=intent.side, shares=intent.shares,
                    limit=intent.limit, ask=intent.expected, est_cost=round(intent.approval.cost, 4))

    def on_order_done(self, order_id: str, filled: bool, now: float, guard: bool = True) -> None:
        it = self.pending.pop(order_id, None)
        if it is None:
            return
        if not filled:
            if guard:
                self.end_guard[it.side] = now + self.cfg.end_guard_s * 1000.0
            self._event(now, "UNFILLED", id=order_id, kind=it.kind, side=it.side)
            self.consecutive_rejections += 1
            if self.consecutive_rejections >= 3 and self.state not in TERMINAL:
                self.why_stopped = "three consecutive unfilled orders"
                self._go(now, State.RISK_LIMIT_STOPPED, self.why_stopped)

    def on_fill(self, order_id: str, side: str, shares: float, price: float, fee: float, now: float,
                kind: str | None = None, expected_price: float | None = None) -> None:
        """A confirmed fill (possibly one of several partial fills of one order)."""
        it = self.pending.get(order_id)
        kind = kind or (it.kind if it else self.sent.get(order_id, ("untracked", side))[0])
        self.consecutive_rejections = 0
        slippage = (price - expected_price) * shares if expected_price is not None else 0.0
        self.portfolio.add_fill(side, shares, price, fee, now, order_id, kind, slippage)
        if it is not None:
            it.shares -= shares
            if it.shares <= 1e-9:
                self.pending.pop(order_id, None)
        t = self.portfolio.totals
        self._event(now, "FILL", id=order_id, kind=kind, side=side, shares=shares, price=price, fee=round(fee, 6),
                    slippage=round(slippage, 6), **self.outcomes())
        if kind == "init":
            self.init_filled += shares
            self.init_notional += shares * price
            self.init_fee += fee
            p0 = self.init_notional / self.init_filled
            if self.cfg.p_ref_includes_fee:
                p0 += self.init_fee / self.init_filled
            self.p0, self.p_ref = p0, 1.0 - p0
            if self.leader is None:
                self.leader = side
            if self.anchor_ts is None:
                self.anchor_ts = now
            if self.state is State.INITIAL_ENTRY and (not self.cfg.init_top_up or self.init_filled >= self.cfg.init_shares - 1e-9):
                self._go(now, State.MONITORING, "initial leader filled", leader=side, p0=round(p0, 4), p_ref=round(1.0 - p0, 4))
            return
        if kind == "leader":
            self.cross_leader, self.cross_leader_price = side, price
        elif kind == "cross_signal":
            self.cross_signal_filled.add(side)
        elif kind == "cross_pair":
            self.cross_pair_filled = True
        if not order_id or order_id not in self.cycle_purchase_orders:
            self.purchases_this_cycle += 1
            if order_id:
                self.cycle_purchase_orders.add(order_id)
        if kind in ("fix", "hedge", "pair"):
            self.fix_fills += 1
        if kind == "accum":
            self.accum.prev_fill = price if self.accum.prev_fill is None else max(self.accum.prev_fill, price)
            self.accum.fills += 1
        if self.state is State.RECOVERY:
            self._event(now, "RECOVERY_FILL", worst=round(t.worst, 4))

    def on_settled(self, up_won: bool, now: float) -> float:
        self.won_up = up_won
        pnl = self.portfolio.settle(up_won)
        self._go(now, State.MARKET_CLOSED, "settled")
        t = self.portfolio.totals
        self._event(now, "SETTLED", up_won=up_won, pnl=round(pnl, 4), fees=round(t.fees, 6),
                    slippage=round(t.slippage_usdc, 6),
                    module_pnl={k: round(v, 4) for k, v in self.portfolio.contribution_by_kind(up_won).items()},
                    **self.outcomes())
        return pnl

    # ------------------------------------------------------------------ the decision

    def on_tick(self, v: View) -> Intent | None:
        self.last_view_t = v.now
        if self.cfg.cross_market_enabled:
            return self._cross_market(v)
        if self.state in TERMINAL:
            if v.now >= self.close_ts and self.state is not State.MARKET_CLOSED and self.state is not State.NO_ENTRY:
                self._go(v.now, State.MARKET_CLOSED, "close")
            return None
        if v.now >= self.close_ts:
            self._go(v.now, State.MARKET_CLOSED, "close")
            return None
        self._sample(v)
        if self.state is State.INITIAL_ENTRY:
            return self._initial_entry(v)
        if self.pending:
            return None                                          # one order at a time: no duplicates, no conflicts
        t = self.portfolio.totals
        if self.cfg.lock_target_usdc > 0 and t.purchases > 0 and t.worst >= self.cfg.lock_target_usdc - 1e-9:
            self.why_stopped = "locked"
            self._go(v.now, State.LOCKED, f"locked profit {t.worst:+.2f} >= {self.cfg.lock_target_usdc:g}: no more buys", **self.outcomes())
            return None
        # This leader repair takes priority over pairing and other buy rules:
        # keep working toward +$1 on the leader's winning outcome while its ask
        # is above 0.70 and that outcome is still negative.
        if self._leader_repair_active(v):
            return self._fix(v, leader_repair=True)
        it = self._pair_buy(v)                                   # pairs under the threshold come first ...
        if it is not None:
            return it
        if self.state in TERMINAL:
            return None
        it = self._fix(v)                                        # ... then the favourite fix, before every other rule
        if it is not None:
            return it
        if self.state in TERMINAL:
            return None
        it = self._hedge(v)                                      # ... then the cheap-token hedge of the worst case
        if it is not None:
            return it
        if self.state in TERMINAL:
            return None
        if self.state in HUB:
            return self._hub(v)
        if self.state is State.LEADER_FALLING:
            return self._falling(v)
        if self.state is State.RECOVERY:
            return self._recovery(v)
        return None

    def _cross_market(self, v: View) -> Intent | None:
        """The 2026-10-19 cross-crypto rule, with one 5-share order per rule.

        Peer averages are passed in by the engine after it has observed all six
        same-window books.  Only executable asks are used, so the signal and the
        price used for a buy are consistent.

        Time window (cross_start_s..cross_end_s, default 0..240): only new leader
        buys are gated. The hold signal and the opposite-token pair after a +0.30
        rise may still fire in the last minute.
        """
        if v.now >= self.close_ts:
            self._go(v.now, State.MARKET_CLOSED, "close")
            return None
        if self.pending:
            return None
        elapsed = (v.now - self.open_ts) / 1000.0
        in_leader_window = self.cfg.cross_start_s <= elapsed <= self.cfg.cross_end_s
        peers = v.peer_avg or {}

        # The >$0.90 peer signal has priority over the broader leader condition.
        # It is a direct, once-per-token settlement hold; it does not start a pair.
        for side in ("UP", "DOWN"):
            q = v.quote(side)
            avg = peers.get(side)
            if (side not in self.cross_signal_filled and q is not None and q.ask is not None
                    and avg is not None and avg > self.cfg.cross_peer_signal_avg
                    and self.cfg.cross_signal_min_ask < q.ask < self.cfg.cross_signal_max_ask):
                # Do not double-buy a leader already held on this outcome.
                if self.cross_leader == side:
                    continue
                it = self._candidate(v, side, self.cfg.cross_signal_shares, "cross_signal")
                if it is not None:
                    it.note = f"other {self.cfg.cross_peer_count} {side} asks average {avg:.3f} > {self.cfg.cross_peer_signal_avg:.2f}"
                return it

        if self.cross_leader is None:
            if not in_leader_window:
                return None
            # "Leader" is the outcome led by the other five markets, not the
            # locally dearer outcome.  That distinction is what permits a
            # qualifying local leader at $0.30-$0.50.
            candidates = []
            for side in ("UP", "DOWN"):
                q, avg = v.quote(side), peers.get(side)
                if (q is not None and q.ask is not None and avg is not None
                        and self.cfg.cross_leader_min_ask <= q.ask <= self.cfg.cross_leader_max_ask
                        and avg > self.cfg.cross_peer_leader_avg):
                    candidates.append((avg, side, q))
            if not candidates:
                return None
            avg, side, q = max(candidates, key=lambda row: (row[0], row[1]))
            it = self._candidate(v, side, self.cfg.cross_leader_shares, "leader")
            if it is not None:
                it.note = (f"{side} leader ask {q.ask:.2f} in {self.cfg.cross_leader_min_ask:.2f}-{self.cfg.cross_leader_max_ask:.2f}; "
                           f"other {self.cfg.cross_peer_count} average {avg:.3f} > {self.cfg.cross_peer_leader_avg:.2f}")
            return it

        if self.cross_pair_filled or self.cross_leader_price is None:
            return None
        q = v.quote(self.cross_leader)
        if q is None or q.ask is None or q.ask < self.cross_leader_price + self.cfg.cross_pair_rise - 1e-9:
            return None
        side = other(self.cross_leader)
        it = self._candidate(v, side, self.cfg.cross_pair_shares, "cross_pair")
        if it is not None:
            it.note = (f"{self.cross_leader} rose {q.ask - self.cross_leader_price:.2f} from filled {self.cross_leader_price:.2f}; "
                       f"buy opposite {side}")
        return it

    # The pair buy (the user, 2026-10-14): the token opposite the side holding unmatched shares is bought when, lot by lot
    # (oldest first, the FIFO matching order), the lot's fee-inclusive unit cost plus this ask plus its fee is at most
    # pair_threshold -- in the number of shares that completes those pairs. Cut by the hard limits like the fix.
    def _pair_buy(self, v: View) -> Intent | None:
        cfg = self.cfg
        if not cfg.pair_buy_enabled or self.portfolio.totals.purchases == 0:
            return None
        if v.now - self.last_fix_t < max(5.0, cfg.pair_buy_cooldown_s) * 1000.0:
            return None
        for side in ("UP", "DOWN"):
            lots = [l for l in self.portfolio.lots[other(side)] if l.remaining > 1e-9]
            if not lots:
                continue
            q = v.quote(side)
            if q is None or q.ask is None:
                continue
            unit = q.ask + v.fee_per_share(q.ask)
            amount = 0.0
            for l in lots:
                if l.unit_cost + unit <= cfg.pair_threshold + 1e-9:
                    amount += l.remaining
                else:
                    break
            amount = math.floor(amount * 100.0) / 100.0
            if amount < 5.0 - 1e-9:
                continue
            n, why, ap = amount, None, None
            while n >= 5.0 - 1e-9:
                why, ap = self._candidate_check(v, side, n, "pair")
                if why is None or why not in ("MARKET_CAPITAL", "TOTAL_CAPITAL", "SETTLEMENT_LOSS", "BALANCE", "LIQUIDITY"):
                    break
                n = math.floor(n / 2.0 * 100.0) / 100.0
            if why is not None:
                self._refuse(v.now, "pair", side, why, ask=q.ask, shares=n, amount=amount)
                if self.state in TERMINAL:
                    return None
                continue
            self.last_fix_t = v.now
            cost = lots[0].unit_cost + ap.unit_cost
            self._event(v.now, "PAIR_BUY", side=side, ask=q.ask, shares=n, amount=amount, pair_cost=round(cost, 4), **self.outcomes())
            return Intent(side=side, shares=n, kind="pair", limit=ap.limit, expected=q.ask, approval=ap,
                          note=f"pairs {other(side)} lots (from {lots[0].unit_cost:.3f}) with {side} at {q.ask:.2f}+fee: {cost:.3f} <= {cfg.pair_threshold:.2f}, {amount:g} to complete")
        return None

    # The favourite fix (the user, 2026-10-13): the higher-priced token's ask inside [fix_min_ask, fix_max_ask] and the
    # settlement P&L if it wins negative -> buy the number of its shares that lifts that outcome to fix_target_usdc.
    # Sized on the ask plus the fee; refuse if the hard limits cannot fund the full target.
    def _leader_repair_active(self, v: View) -> bool:
        if self.leader not in ("UP", "DOWN") or self.portfolio.totals.purchases == 0:
            return False
        q = v.quote(self.leader)
        t = self.portfolio.totals
        leader_pnl = (t.q_up if self.leader == "UP" else t.q_down) - t.c_total
        if self.leader_repair_started:
            if leader_pnl >= 1.0 - 1e-9:
                self.leader_repair_started = False
                self._event(v.now, "LEADER_REPAIR_COMPLETE", pnl_hi=round(leader_pnl, 4), target=1.0)
                return False
            return True
        if q is not None and q.ask is not None and q.ask > 0.70 + 1e-9 and leader_pnl < -1e-9:
            self.leader_repair_started = True
            self._event(v.now, "LEADER_REPAIR_START", side=self.leader, ask=q.ask,
                        pnl_hi=round(leader_pnl, 4), target=1.0)
            return True
        return False

    def _fix(self, v: View, *, leader_repair: bool = False) -> Intent | None:
        cfg = self.cfg
        if not cfg.fix_enabled or self.portfolio.totals.purchases == 0:
            return None
        if v.now - self.last_fix_t < max(5.0, cfg.fix_cooldown_s) * 1000.0:
            return None
        up, dn = v.up, v.down
        if up is None or dn is None or up.mid is None or dn.mid is None:
            return None
        hi = self.leader if leader_repair else ("UP" if up.mid >= dn.mid else "DOWN")
        q = v.quote(hi)
        if q is None or q.ask is None:
            return None
        if leader_repair:
            if q.ask <= 0.70 + 1e-9:
                return None
        elif not (cfg.fix_min_ask - 1e-9 <= q.ask <= cfg.fix_max_ask + 1e-9):
            return None
        t = self.portfolio.totals
        pnl_hi = (t.q_up if hi == "UP" else t.q_down) - t.c_total
        if pnl_hi >= 0.0:
            return None
        target = 1.0 if leader_repair else cfg.fix_target_usdc
        if not leader_repair and v.seconds_left > cfg.fix_max_left_s + 1e-9:
            self.refusals["fix:TOO_EARLY"] += 1                 # the user 10-14: not before 210 s left
            return None
        if not leader_repair and cfg.fix_require_rising:
            r = (v.rise or {}).get(hi)
            if r is None or r < cfg.fix_min_rise - 1e-12:
                self.refusals["fix:NOT_RISING"] += 1            # the user 10-14: only while the token is rising
                return None
        unit = q.ask + v.fee_per_share(q.ask)
        if unit >= 1.0 - 1e-9:
            return None
        need = math.ceil((target - pnl_hi) / (1.0 - unit) * 100.0 - 1e-6) / 100.0
        requested = max(need, 5.0)
        n = requested
        why, ap = self._candidate_check(v, hi, n, "fix", allow_partial_fix=leader_repair)
        requested_refusal = why
        size_limit_reasons = {"SETTLEMENT_LOSS", "MARKET_CAPITAL", "TOTAL_CAPITAL", "BALANCE", "LIQUIDITY"}
        cut_reason = None
        if why in size_limit_reasons or why == "TARGET_UNACHIEVABLE":
            # A full $5 target can require an order larger than the remaining
            # settlement-loss budget. Find the largest venue-sized increment
            # that is still safe, rather than letting one oversized proposal
            # terminally stop every strategy path for this market.
            lo, hi_cents = 500, int(math.floor(requested * 100.0 + 1e-7))
            best: tuple[float, Approval] | None = None
            min_why, min_ap = self._candidate_check(v, hi, 5.0, "fix", allow_partial_fix=True)
            if min_why is None and min_ap is not None:
                best = (5.0, min_ap)
                lo = 501
                while lo <= hi_cents:
                    mid = (lo + hi_cents) // 2
                    candidate_n = mid / 100.0
                    candidate_why, candidate_ap = self._candidate_check(
                        v, hi, candidate_n, "fix", allow_partial_fix=True)
                    if candidate_why is None and candidate_ap is not None:
                        best = (candidate_n, candidate_ap)
                        lo = mid + 1
                    elif candidate_why in size_limit_reasons:
                        hi_cents = mid - 1
                    else:
                        # This failure is not improved by shrinking the size.
                        break
            if best is not None:
                n, ap = best
                why = None
                if n + 1e-9 < requested:
                    cut_reason = requested_refusal or "RISK_BUDGET"
                    self._event(v.now, "FIX_SIZE_CUT", requested=requested, shares=n,
                                reason=cut_reason, pnl_hi=round(pnl_hi, 4))
            elif min_why is not None:
                why = min_why
                n = 5.0
                ap = None
        if why is not None or ap is None:
            self._refuse(v.now, "fix", hi, why or "RISK_LIMIT", ask=q.ask, shares=n, needed=need,
                         pnl_hi=round(pnl_hi, 4))
            self.last_fix_t = v.now
            return None
        self.last_fix_t = v.now
        after = pnl_hi + n * (1.0 - ap.unit_cost)
        target_reached = after >= target - 1e-9
        self._event(v.now, "FIX", side=hi, ask=q.ask, needed=need, shares=n, pnl_hi=round(pnl_hi, 4), pnl_hi_after=round(after, 4),
                    target=target, target_reached=target_reached, size_cut_reason=cut_reason)
        note = (f"{'leader repair' if leader_repair else 'favourite ' + hi} ask {q.ask:.2f}: if it wins "
                f"{pnl_hi:+.2f} -> {after:+.2f} (target {target:g}"
                + (f", risk-capped from {need:g} to {n:g} sh" if n + 1e-9 < requested else "") + ")")
        return Intent(side=hi, shares=n, kind="fix", limit=ap.limit, expected=q.ask, approval=ap,
                      note=note)

    # The cheap-token hedge (the user, 2026-10-14): the lower-priced token's ask at most hedge_max_ask and the worst
    # outcome negative -> buy it up to the size that keeps the best outcome at hedge_min_best_usdc and never past the
    # point where both outcomes are equal; cut by the hard limits like the fix.
    def _hedge(self, v: View) -> Intent | None:
        cfg = self.cfg
        if not cfg.hedge_enabled or self.portfolio.totals.purchases == 0:
            return None
        if v.now - self.last_fix_t < max(5.0, cfg.fix_cooldown_s) * 1000.0:
            return None
        up, dn = v.up, v.down
        if up is None or dn is None or up.mid is None or dn.mid is None:
            return None
        lo = "DOWN" if up.mid >= dn.mid else "UP"
        q = v.quote(lo)
        if q.ask is None or q.ask > cfg.hedge_max_ask + 1e-9:
            return None
        t = self.portfolio.totals
        pnl_lo = (t.q_up if lo == "UP" else t.q_down) - t.c_total
        pnl_hi = (t.q_down if lo == "UP" else t.q_up) - t.c_total
        worst, best = min(pnl_lo, pnl_hi), max(pnl_lo, pnl_hi)
        if worst >= 0.0 or pnl_lo >= pnl_hi or best <= cfg.hedge_min_best_usdc + 1e-9:
            return None
        unit = q.ask + v.fee_per_share(q.ask)
        if unit >= 1.0 - 1e-9:
            return None
        n = min((best - cfg.hedge_min_best_usdc) / unit, (pnl_hi - pnl_lo) / 1.0)   # best stays >= +2; never past equal
        n = math.floor(n * 100.0) / 100.0
        if n < 5.0 - 1e-9:
            self.refusals["hedge:TOO_SMALL"] += 1
            return None
        why, ap = None, None
        while n >= 5.0 - 1e-9:
            why, ap = self._candidate_check(v, lo, n, "hedge")
            if why is None or why not in ("MARKET_CAPITAL", "TOTAL_CAPITAL", "SETTLEMENT_LOSS", "BALANCE", "LIQUIDITY"):
                break
            n = math.floor(n / 2.0 * 100.0) / 100.0
        if why is not None:
            self._refuse(v.now, "hedge", lo, why, ask=q.ask, shares=n, worst=round(worst, 4), best=round(best, 4))
            return None
        self.last_fix_t = v.now
        w_after, b_after = worst + n * (1.0 - ap.unit_cost), best - n * ap.unit_cost
        self._event(v.now, "HEDGE", side=lo, ask=q.ask, shares=n, worst=round(worst, 4), worst_after=round(w_after, 4),
                    best=round(best, 4), best_after=round(b_after, 4))
        return Intent(side=lo, shares=n, kind="hedge", limit=ap.limit, expected=q.ask, approval=ap,
                      note=f"cheap {lo} ask {q.ask:.2f} <= {cfg.hedge_max_ask:.2f}: worst {worst:+.2f} -> {w_after:+.2f}, best {best:+.2f} -> {b_after:+.2f}")

    def _candidate_check(self, v: View, side: str, shares: float, kind: str, *, allow_partial_fix: bool = False):
        q = v.quote(side)
        # Once the initial entry is filled, don't chase an already-rising token
        # through the 0.50-0.70 ask band. `rise` is the configured lookback
        # change in the token's midpoint; unknown/flat prices aren't classified
        # as rising. Initial-entry top-ups retain their initial-entry behavior.
        if (kind != "init" and self.portfolio.totals.purchases > 0 and q is not None and q.ask is not None
                and 0.50 - 1e-9 <= q.ask <= 0.70 + 1e-9
                and (v.rise or {}).get(side) is not None and (v.rise or {}).get(side) > 1e-12):
            return "RISING_TOKEN_PRICE_BAND", None
        c = Candidate(side=side, shares=shares, kind=kind, best_bid=q.bid if q else None, best_ask=q.ask if q else None,
                      ask_levels=q.ask_levels if q else [], book_age_ms=q.age_ms if q else 1e18,
                      seconds_left=v.seconds_left, accepting=v.accepting, balance=v.balance,
                      purchases_this_cycle=self.purchases_this_cycle, purchases_this_market=self.portfolio.totals.purchases,
                      pending_in_market=bool(self.pending), end_guarded=v.now < self.end_guard.get(side, -1e18),
                      capital_other_markets=v.capital_other_markets, resting_cost=v.resting_cost,
                      allow_partial_fix=allow_partial_fix)
        return check(c, self.cfg, self.portfolio, v.fee_per_share)

    # Rule 1
    def _initial_entry(self, v: View) -> Intent | None:
        cfg = self.cfg
        since_open = (v.now - self.open_ts) / 1000.0
        if since_open < cfg.init_delay_s:
            return None
        if self.pending:
            return None
        if since_open > cfg.init_max_s or self.init_tries >= cfg.init_max_tries:
            if self.init_filled > 0.0:
                # a partial initial fill that could not be topped up: reconciled -- the strategy runs on what filled
                self._go(v.now, State.MONITORING, f"initial entry partially filled ({self.init_filled:g} sh): proceeding")
                return None
            self.why_stopped = "no initial fill"
            self._go(v.now, State.NO_ENTRY, f"no initial fill after {self.init_tries} tries / {since_open:.0f} s")
            return None
        if v.now - self.init_last_try < cfg.init_retry_s * 1000.0:
            return None
        up, dn = v.up, v.down
        if up is None or dn is None:
            return None
        key = {"mid": lambda q: q.mid, "ask": lambda q: q.ask, "bid": lambda q: q.bid}[cfg.leader_rule]
        pu, pd = key(up), key(dn)
        if pu is None or pd is None:
            return None
        if abs(pu - pd) < 1e-12:
            if cfg.leader_tie == "skip":
                self.refusals["init:TIE"] += 1
                return None
            side = "UP" if cfg.leader_tie == "up" else "DOWN"
        else:
            side = "UP" if pu > pd else "DOWN"
        q = v.quote(side)
        if q.ask is None or not (cfg.init_min_ask - 1e-9 <= q.ask <= cfg.init_max_ask + 1e-9):
            self._refuse(v.now, "init", side, "INIT_PRICE", ask=q.ask)
            return None
        shares = cfg.init_shares - self.init_filled if cfg.init_top_up else cfg.init_shares
        if self.init_filled > 0.0 and not cfg.init_top_up:
            return None
        if shares < 5.0 - 1e-9:
            return None
        it = self._candidate(v, side, shares, "init")
        if it is not None:
            it.note = f"leader by {cfg.leader_rule} {pu:.3f} vs {pd:.3f}"
        return it

    # Rules 3, 4 (detection) and 6
    def _hub(self, v: View) -> Intent | None:
        cfg = self.cfg
        if self.falling_confirmed():
            return self._enter_falling(v)
        opp = self.opposite
        q = v.quote(opp)
        if q is None or q.ask is None or self.p_ref is None:
            return None
        in_range = self.p_ref < q.ask - 1e-12 and q.ask <= cfg.opp_ceiling + 1e-12
        if self.accum.active and q.ask > cfg.opp_ceiling + 1e-12:
            self.accum.active, self.accum.ended = False, "ask above the ceiling"
            self._event(v.now, "ACCUMULATION_END", seq=self.accum.seq, ask=q.ask, fills=self.accum.fills)
            self._go(v.now, State.MONITORING, "the opposite token rose above the ceiling")
            return None
        if not self.accum.active and in_range and self.state is not State.LEADER_FALLING:
            if self.accum.seq == 0 or (cfg.accum_resequence and self.accum.seq < cfg.max_accum_sequences):
                # the sequence's first decision is at the NEXT 5-s boundary (the schedule runs from the initial fill;
                # the slot the sequence starts in is never a decision point)
                self.accum = Accumulation(seq=self.accum.seq + 1, active=True,
                                          last_slot=self._slot(self.anchor_ts, cfg.accum_interval_s, v.now))
                self._event(v.now, "ACCUMULATION_START", seq=self.accum.seq, ask=q.ask, p_ref=round(self.p_ref, 4))
                self._go(v.now, State.OPPOSITE_ACCUMULATION, "the opposite ask is above P_ref and at most the ceiling")
            else:
                return None
        if not self.accum.active:
            return None
        slot = self._slot(self.anchor_ts, cfg.accum_interval_s, v.now)
        if slot <= self.accum.last_slot:
            return None
        if cfg.accum_eval == "slot_start":
            self.accum.last_slot = slot                          # one decision per slot, taken now
        ok = in_range and (self.accum.prev_fill is None or q.ask > self.accum.prev_fill + 1e-12)
        if not ok:
            self.refusals["accum:CONDITIONS"] += 1
            return None
        it = self._candidate(v, opp, cfg.accum_shares, "accum")
        if it is not None:
            self.accum.last_slot = slot
            it.note = f"seq {self.accum.seq} slot {slot}: P_ref {self.p_ref:.3f} < ask {q.ask:.2f} <= {cfg.opp_ceiling:.2f}" + \
                      (f", prev fill {self.accum.prev_fill:.3f}" if self.accum.prev_fill is not None else "")
        return it

    def _enter_falling(self, v: View) -> None:
        self.cycle += 1
        self.purchases_this_cycle = 0
        self.cycle_purchase_orders.clear()
        self.falling_events += 1
        self.phase_anchor, self.phase_slot = v.now, -1
        self.rise_n = 0
        if self.accum.active:
            self.accum.active, self.accum.ended = False, "leader falling"
        q = v.quote(self.leader)
        self._go(v.now, State.LEADER_FALLING, "leader under the reversal threshold and falling (confirmed)",
                 cycle=self.cycle, leader_px=self._price(q), **self.outcomes())
        self._event(v.now, "FALLING_LEADER", cycle=self.cycle, **self.outcomes())
        return None

    # Rule 4 (purchases)
    def _falling(self, v: View) -> Intent | None:
        cfg = self.cfg
        if self.rising_confirmed():
            self.recovery_events += 1
            self.phase_anchor, self.phase_slot = v.now, -1
            self.fall_n = 0
            self._go(v.now, State.RECOVERY, "leader rising (confirmed)", cycle=self.cycle, **self.outcomes())
            return None
        if cfg.falling_buy == "none":
            return None
        slot = self._slot(self.phase_anchor, cfg.accum_interval_s, v.now)
        if slot <= self.phase_slot:
            return None
        self.phase_slot = slot
        opp = self.opposite
        q = v.quote(opp)
        if q is None or q.ask is None or q.ask > cfg.falling_opp_max_ask + 1e-12:
            self.refusals["falling:PRICE"] += 1
            return None
        it = self._candidate(v, opp, cfg.falling_shares, "falling")
        if it is None:
            return None
        t = self.portfolio.totals
        up, dn = self.portfolio.settlement_pnl_after(opp, it.shares, it.approval.unit_cost)
        if min(up, dn) <= t.worst + 1e-9:
            self.refusals["falling:NO_IMPROVEMENT"] += 1
            self._event(v.now, "REFUSED", kind="falling", side=opp, reason="NO_IMPROVEMENT", worst=round(t.worst, 4),
                        worst_after=round(min(up, dn), 4))
            return None
        it.note = f"worst {t.worst:+.3f} -> {min(up, dn):+.3f}"
        return it

    # Rule 5
    def _recovery(self, v: View) -> Intent | None:
        cfg = self.cfg
        if self.falling_confirmed():
            return self._enter_falling(v)
        self.pnl_live = self.liquidation(v)
        if self.pnl_live is not None and self.pnl_live > cfg.recovery_stop_pnl + 1e-12:
            self.recovery_stops += 1
            self._go(v.now, State.RECOVERY_STOPPED, f"PnL_live {self.pnl_live:+.4f} > {cfg.recovery_stop_pnl:g}",
                     pnl_live=round(self.pnl_live, 4), **self.outcomes())
            self._event(v.now, "RECOVERY_STOP", pnl_live=round(self.pnl_live, 4), **self.outcomes())
            return None
        slot = self._slot(self.phase_anchor, cfg.recovery_interval_s, v.now)
        if slot <= self.phase_slot:
            return None
        self.phase_slot = slot
        if self.pnl_live is None:
            self.refusals["recovery:NO_MARK"] += 1               # a held side has no bid: the stop cannot be judged
            return None
        q = v.quote(self.leader)
        if q is None or q.ask is None or q.ask > cfg.recovery_max_ask + 1e-12:
            self.refusals["recovery:PRICE"] += 1
            return None
        it = self._candidate(v, self.leader, cfg.recovery_shares, "recovery")
        if it is not None:
            it.note = f"PnL_live {self.pnl_live:+.3f} <= {cfg.recovery_stop_pnl:g}, cycle {self.cycle}"
        return it

    # ------------------------------------------------------------------ snapshot / restore

    def summary(self) -> dict:
        return {"state": self.state.value, "leader": self.leader, "p0": self.p0, "p_ref": self.p_ref, "cycle": self.cycle,
                "purchases_this_cycle": self.purchases_this_cycle, "accum": {"seq": self.accum.seq, "active": self.accum.active,
                "prev_fill": self.accum.prev_fill, "fills": self.accum.fills, "ended": self.accum.ended},
                "pnl_live": self.pnl_live, "fall_n": self.fall_n, "rise_n": self.rise_n, "recovery_stops": self.recovery_stops,
                "fix_fills": self.fix_fills,
                "leader_repair_started": self.leader_repair_started,
                "cross_market": {"leader": self.cross_leader, "leader_price": self.cross_leader_price,
                                 "pair_filled": self.cross_pair_filled, "signal_filled": sorted(self.cross_signal_filled)},
                "falling_events": self.falling_events, "recovery_events": self.recovery_events,
                "pending": {k: {"kind": i.kind, "side": i.side, "shares": i.shares, "limit": i.limit} for k, i in self.pending.items()},
                "refusals": dict(self.refusals), "why_stopped": self.why_stopped, "portfolio": self.portfolio.snapshot()}

    def persist(self) -> dict:
        """What a restart needs: the lots (the fills), the leader and the state. Pending orders are NOT carried
        over (the venue's answer to them is re-read by live.py's own restore)."""
        return {"slug": self.slug, "open_ts": self.open_ts, "close_ts": self.close_ts, "state": self.state.value,
                "leader": self.leader, "p0": self.p0, "p_ref": self.p_ref, "cycle": self.cycle,
                "purchases_this_cycle": self.purchases_this_cycle, "anchor_ts": self.anchor_ts, "phase_anchor": self.phase_anchor,
                "phase_slot": self.phase_slot, "cycle_purchase_orders": sorted(self.cycle_purchase_orders),
                "accum": self.accum.__dict__.copy(), "init_tries": self.init_tries,
                "consecutive_rejections": self.consecutive_rejections,
                "init_filled": self.init_filled, "init_notional": self.init_notional, "init_fee": self.init_fee,
                "recovery_stops": self.recovery_stops, "falling_events": self.falling_events,
                "recovery_events": self.recovery_events, "why_stopped": self.why_stopped,
                "leader_repair_started": self.leader_repair_started,
                "cross_leader": self.cross_leader, "cross_leader_price": self.cross_leader_price,
                "cross_pair_filled": self.cross_pair_filled, "cross_signal_filled": sorted(self.cross_signal_filled),
                "portfolio": self.portfolio.snapshot()}

    @classmethod
    def restore(cls, cfg: LeaderPairConfig, rec: dict) -> "MarketStrategy":
        m = cls(cfg, rec["slug"], float(rec["open_ts"]), float(rec["close_ts"]))
        m.state = State(rec.get("state", "INITIAL_ENTRY"))
        m.leader, m.p0, m.p_ref = rec.get("leader"), rec.get("p0"), rec.get("p_ref")
        m.cycle = int(rec.get("cycle", 0))
        m.purchases_this_cycle = int(rec.get("purchases_this_cycle", 0))
        m.cycle_purchase_orders = set(rec.get("cycle_purchase_orders", []))
        m.anchor_ts, m.phase_anchor = rec.get("anchor_ts"), rec.get("phase_anchor")
        m.phase_slot = int(rec.get("phase_slot", -1))
        m.accum = Accumulation(**{k: v for k, v in (rec.get("accum") or {}).items() if k in Accumulation.__dataclass_fields__})
        m.init_tries = int(rec.get("init_tries", 0))
        m.consecutive_rejections = int(rec.get("consecutive_rejections", 0))
        m.init_filled, m.init_notional, m.init_fee = float(rec.get("init_filled", 0)), float(rec.get("init_notional", 0)), float(rec.get("init_fee", 0))
        m.recovery_stops, m.falling_events = int(rec.get("recovery_stops", 0)), int(rec.get("falling_events", 0))
        m.recovery_events, m.why_stopped = int(rec.get("recovery_events", 0)), rec.get("why_stopped", "")
        m.leader_repair_started = bool(rec.get("leader_repair_started", False))
        m.cross_leader, m.cross_leader_price = rec.get("cross_leader"), rec.get("cross_leader_price")
        m.cross_pair_filled = bool(rec.get("cross_pair_filled", False))
        m.cross_signal_filled = set(rec.get("cross_signal_filled", []))
        m.portfolio = Portfolio.restore(rec.get("portfolio") or {}, cfg.pair_threshold, cfg.pair_matching)
        m.fall_n = m.rise_n = 0                                   # the signal history is not persisted: re-confirm
        return m
