"""Portfolio accounting for LEADER_PAIR (the spec's sections 2, 5 and 10): execution lots, FIFO pair matching,
fee-inclusive cost, settlement P&L under both outcomes and the conservative liquidation P&L.

Every number here comes from ACTUAL fills (price, size, fee) -- never from a last trade or an expected price.

The settlement identities (section 10), with C_total = cash paid for every share INCLUDING fees:

    PnL_if_Up_wins   = Q_up   * 1 + Q_down * 0 - C_total = Q_up   - C_total
    PnL_if_Down_wins = Q_up   * 0 + Q_down * 1 - C_total = Q_down - C_total

Proof: a winning share pays exactly 1 USDC and a losing one 0; nothing else moves cash after the last fill (no
exits by design); so the cash at resolution is the payout minus everything paid, which is C_total. QED. A pair
(one Up share and one Down share) pays 1 whatever happens, so its settlement margin is 1 - (its two lots' fee-
inclusive unit costs); summing the matched pairs and the unmatched shares gives the same two identities:

    PnL_if_S_wins = sum over pairs (1 - C_pair) + (unmatched shares of S) * 1 - (cost of all unmatched shares)

The matched shares are the same shares whichever side wins, which is why the pair margin is "locked" and the
unmatched inventory is the directional exposure.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SIDES = ("UP", "DOWN")


def other(side: str) -> str:
    return "DOWN" if side == "UP" else "UP"


@dataclass(slots=True)
class Lot:
    """One execution (or one fill of one order): the unit of pair matching."""
    lot_id: int
    side: str
    shares: float                   # filled
    price: float                    # the fill price
    fee: float                      # the fee charged on this fill, USDC
    ts: float                       # when the strategy learnt of the fill, epoch ms
    order_id: str = ""
    kind: str = ""                  # init | accum | falling | recovery
    remaining: float = 0.0          # shares not yet in a pair
    slippage_usdc: float = 0.0

    @property
    def unit_cost(self) -> float:
        """Fee-inclusive cost per share."""
        return self.price + (self.fee / self.shares if self.shares > 0 else 0.0)

    @property
    def cost(self) -> float:
        return self.price * self.shares + self.fee


@dataclass(slots=True)
class Pair:
    up_lot: int
    down_lot: int
    shares: float
    cost: float                     # fee-inclusive cost of ONE pair (one Up share + one Down share)
    qualifies: bool                 # cost <= the configured threshold at the time it was formed

    @property
    def margin(self) -> float:
        """Gross settlement margin per pair."""
        return 1.0 - self.cost


@dataclass(slots=True)
class Totals:
    q_up: float = 0.0
    q_down: float = 0.0
    c_up: float = 0.0               # fee-inclusive
    c_down: float = 0.0
    fees: float = 0.0
    cash_spent: float = 0.0         # price x size of every buy (fees apart)
    cash_received: float = 0.0      # of every sell (none by design; kept for completeness)
    matched_up: float = 0.0
    matched_down: float = 0.0
    purchases: int = 0
    slippage_usdc: float = 0.0

    @property
    def c_total(self) -> float:
        return self.c_up + self.c_down

    @property
    def unmatched_up(self) -> float:
        return self.q_up - self.matched_up

    @property
    def unmatched_down(self) -> float:
        return self.q_down - self.matched_down

    @property
    def pnl_if_up(self) -> float:
        return self.q_up - self.c_total

    @property
    def pnl_if_down(self) -> float:
        return self.q_down - self.c_total

    @property
    def worst(self) -> float:
        return min(self.pnl_if_up, self.pnl_if_down)

    @property
    def best(self) -> float:
        return max(self.pnl_if_up, self.pnl_if_down)


@dataclass
class Portfolio:
    """One market's inventory. ``pair_threshold`` qualifies a pair at formation; ``matching`` is fifo | lifo."""
    pair_threshold: float = 0.95
    matching: str = "fifo"
    lots: dict[str, list[Lot]] = field(default_factory=lambda: {"UP": [], "DOWN": []})
    pairs: list[Pair] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    realized_pnl: float = 0.0       # from sells only (none by design) and, at the end, the settlement
    settled: bool = False
    settlement_pnl: float | None = None
    _seq: int = 0
    _purchase_orders: set[str] = field(default_factory=set, repr=False)

    # ------------------------------------------------------------------ fills

    def add_fill(self, side: str, shares: float, price: float, fee: float, ts: float, order_id: str = "",
                 kind: str = "", slippage_usdc: float = 0.0) -> Lot:
        """Book one BUY fill as a lot, update the totals and match pairs. Returns the lot."""
        if side not in SIDES:
            raise ValueError(side)
        if shares <= 0.0:
            raise ValueError("a fill has a positive size")
        self._seq += 1
        lot = Lot(self._seq, side, shares, price, fee, ts, order_id, kind, remaining=shares, slippage_usdc=slippage_usdc)
        self.lots[side].append(lot)
        t = self.totals
        if side == "UP":
            t.q_up += shares
            t.c_up += lot.cost
        else:
            t.q_down += shares
            t.c_down += lot.cost
        t.fees += fee
        t.slippage_usdc += slippage_usdc
        t.cash_spent += price * shares
        if not order_id or order_id not in self._purchase_orders:
            t.purchases += 1
            if order_id:
                self._purchase_orders.add(order_id)
        self._match()
        return lot

    def _match(self) -> None:
        """Pair unmatched Up shares with unmatched Down shares, oldest first (FIFO) -- each share in one pair at most."""
        ups = [l for l in self.lots["UP"] if l.remaining > 1e-9]
        downs = [l for l in self.lots["DOWN"] if l.remaining > 1e-9]
        if self.matching == "lifo":
            ups, downs = ups[::-1], downs[::-1]
        i = j = 0
        while i < len(ups) and j < len(downs):
            u, d = ups[i], downs[j]
            q = min(u.remaining, d.remaining)
            cost = u.unit_cost + d.unit_cost
            self.pairs.append(Pair(u.lot_id, d.lot_id, q, cost, cost <= self.pair_threshold + 1e-12))
            u.remaining -= q
            d.remaining -= q
            self.totals.matched_up += q
            self.totals.matched_down += q
            if u.remaining <= 1e-9:
                i += 1
            if d.remaining <= 1e-9:
                j += 1

    # ------------------------------------------------------------------ figures

    def avg_cost(self, side: str) -> float | None:
        t = self.totals
        q, c = (t.q_up, t.c_up) if side == "UP" else (t.q_down, t.c_down)
        return c / q if q > 0 else None

    def qualifying_pairs(self) -> float:
        return sum(p.shares for p in self.pairs if p.qualifies)

    def formed_pairs(self) -> float:
        return sum(p.shares for p in self.pairs)

    def pair_margin(self, qualifying_only: bool = True) -> float:
        """Total gross settlement margin of the matched pairs (negative pairs count when qualifying_only is False)."""
        return sum(p.shares * p.margin for p in self.pairs if p.qualifies or not qualifying_only)

    def avg_pair_cost(self, qualifying_only: bool = False) -> float | None:
        ps = [p for p in self.pairs if p.qualifies or not qualifying_only]
        q = sum(p.shares for p in ps)
        return sum(p.shares * p.cost for p in ps) / q if q > 0 else None

    def settlement_pnl_after(self, side: str, shares: float, unit_cost: float) -> tuple[float, float]:
        """(PnL_if_Up, PnL_if_Down) if ``shares`` of ``side`` were bought at the fee-inclusive ``unit_cost``."""
        t = self.totals
        add_cost = shares * unit_cost
        up = t.q_up + (shares if side == "UP" else 0.0) - t.c_total - add_cost
        down = t.q_down + (shares if side == "DOWN" else 0.0) - t.c_total - add_cost
        return up, down

    def liquidation_pnl(self, bid_up: float | None, bid_down: float | None, fee_per_share, slippage: float = 0.0,
                        bid_levels_up=None, bid_levels_down=None) -> float | None:
        """The conservative mark-to-market (Rule 5):

            PnL_live = Q_up * P_up_bid + Q_down * P_down_bid - C_total - liquidation fees - slippage

        With bid DEPTH given ([(price, size)] best first) each side is walked down the book and shares the
        book cannot absorb count ZERO; without depth the whole position is valued at the best bid. A held side
        with no bid at all makes the figure None (unknown, never "zero")."""
        t = self.totals
        total = -t.c_total
        for q, bid, levels in ((t.q_up, bid_up, bid_levels_up), (t.q_down, bid_down, bid_levels_down)):
            if q <= 1e-9:
                continue
            if bid is None:
                return None
            if levels:
                left = q
                for price, size in sorted(levels, key=lambda l: -l[0]):
                    if left <= 1e-9:
                        break
                    take = min(left, size)
                    total += take * (price - fee_per_share(price) - slippage)
                    left -= take
            else:
                total += q * (bid - fee_per_share(bid) - slippage)
        return total

    def settle(self, up_won: bool) -> float:
        """Resolution: the realised settlement P&L (fees already inside C_total)."""
        pnl = self.totals.pnl_if_up if up_won else self.totals.pnl_if_down
        self.settled, self.settlement_pnl = True, pnl
        self.realized_pnl += pnl
        return pnl

    def contribution_by_kind(self, up_won: bool) -> dict[str, float]:
        """Settlement PnL by signal module; contributions sum to realized settlement PnL."""
        winning_side = "UP" if up_won else "DOWN"
        out: dict[str, float] = {}
        for side in SIDES:
            payout_per_share = 1.0 if side == winning_side else 0.0
            for lot in self.lots[side]:
                out[lot.kind or "unclassified"] = out.get(lot.kind or "unclassified", 0.0) + (
                    lot.shares * payout_per_share - lot.cost
                )
        return out

    def snapshot(self) -> dict:
        t = self.totals
        return {"q_up": round(t.q_up, 4), "q_down": round(t.q_down, 4), "c_up": round(t.c_up, 6), "c_down": round(t.c_down, 6),
                "c_total": round(t.c_total, 6), "fees": round(t.fees, 6), "cash_spent": round(t.cash_spent, 6),
                "slippage_usdc": round(t.slippage_usdc, 6), "purchases": t.purchases, "avg_up": self.avg_cost("UP"), "avg_down": self.avg_cost("DOWN"),
                "matched": round(min(t.matched_up, t.matched_down), 4), "qualifying_pairs": round(self.qualifying_pairs(), 4),
                "unmatched_up": round(t.unmatched_up, 4), "unmatched_down": round(t.unmatched_down, 4),
                "pair_margin": round(self.pair_margin(), 6), "avg_pair_cost": self.avg_pair_cost(),
                "pnl_if_up": round(t.pnl_if_up, 6), "pnl_if_down": round(t.pnl_if_down, 6), "worst": round(t.worst, 6),
                "settled": self.settled, "settlement_pnl": self.settlement_pnl,
                "lots": [{"id": l.lot_id, "side": l.side, "shares": l.shares, "price": l.price, "fee": round(l.fee, 6),
                          "kind": l.kind, "order_id": l.order_id, "unmatched": round(l.remaining, 4),
                          "slippage_usdc": round(l.slippage_usdc, 6)}
                         for s in SIDES for l in self.lots[s]],
                "pairs": [{"up_lot": p.up_lot, "down_lot": p.down_lot, "shares": p.shares, "cost": round(p.cost, 6),
                           "qualifies": p.qualifies} for p in self.pairs]}

    @classmethod
    def restore(cls, snap: dict, pair_threshold: float, matching: str = "fifo") -> "Portfolio":
        """Rebuild from ``snapshot()`` after a restart: lots replayed in order, pairs re-matched (identical by FIFO)."""
        p = cls(pair_threshold=pair_threshold, matching=matching)
        for l in sorted(snap.get("lots", []), key=lambda x: x["id"]):
            p.add_fill(l["side"], float(l["shares"]), float(l["price"]), float(l["fee"]), 0.0, l.get("order_id", ""),
                       l.get("kind", ""), float(l.get("slippage_usdc", 0.0)))
        if snap.get("settled") and snap.get("settlement_pnl") is not None:
            p.settled, p.settlement_pnl = True, float(snap["settlement_pnl"])
            p.realized_pnl = float(snap["settlement_pnl"])
        return p
