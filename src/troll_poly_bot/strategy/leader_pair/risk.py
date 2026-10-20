"""Hard pre-order risk controls for LEADER_PAIR (the spec's section 11). Strategy logic cannot bypass them:
``check()`` runs on every candidate order, in this order, and the first failure is the refusal (logged by the
caller with its reason code). Nothing here places orders or holds state.

Checks (reason codes):
    MARKET_STATUS      the market is not accepting orders / already closed / its tokens unknown
    STALE_DATA         the book is older than max_book_age_ms (or no evaluation reached the engine recently)
    TIME_REMAINING     fewer than min_time_remaining_s left (or more than max_time_remaining_s)
    PENDING_ORDER      an order is already in flight in this market (never two at once: no duplicates)
    END_GUARD          an order on this token ended unfilled a moment ago; its fill may still arrive
    NO_BOOK / BAD_BOOK no executable ask, or an inverted / impossible book
    PRICE_BAND         the limit price is outside [min_trade_price, max_trade_price]
    SPREAD             the spread is wider than max_spread
    LIQUIDITY          ask depth at or under the limit is below min_depth_shares (or the requested size)
    SLIPPAGE           the VWAP of the requested size exceeds the best ask by more than max_slippage
    PURCHASES_CYCLE    the cycle's additional-purchase cap is used up (the initial buy is exempt)
    PURCHASES_MARKET   the market's purchase cap is used up
    MARKET_CAPITAL     cost (fee in) would take the market's capital past max_market_capital_usdc
    TOTAL_CAPITAL      ... or every open market's capital past max_total_capital_usdc
    SETTLEMENT_LOSS    after the buy one settlement outcome would lose more than max_settlement_loss_usdc
    BALANCE            the account cannot pay the cost plus the fee
"""
from __future__ import annotations

from dataclasses import dataclass

from .accounting import Portfolio
from .config import LeaderPairConfig, VENUE_MIN_SHARES

MAX_MARKET_CAPITAL_USDC = 150.0
MAX_ACCOUNT_CAPITAL_USDC = 500.0
MAX_SETTLEMENT_LOSS_USDC = 20.0
MAX_PURCHASES_PER_CYCLE = 6
MAX_PURCHASES_PER_MARKET = 20
MIN_TIME_REMAINING_S = 20.0
MAX_SLIPPAGE = 0.02
MAX_SPREAD = 0.03
MIN_DEPTH_SHARES = VENUE_MIN_SHARES
MAX_BOOK_AGE_MS = 2500.0
MAX_TRADE_PRICE = 0.95


@dataclass(slots=True)
class Candidate:
    side: str
    shares: float
    kind: str                        # init | accum | falling | recovery
    best_bid: float | None
    best_ask: float | None
    ask_levels: list[tuple[float, float]]      # (price, size), best first
    book_age_ms: float
    seconds_left: float
    accepting: bool
    balance: float | None
    purchases_this_cycle: int        # additional purchases already made in the cycle (the initial buy is not one)
    purchases_this_market: int
    pending_in_market: bool
    end_guarded: bool
    capital_other_markets: float     # fee-inclusive cost held in the OTHER open markets
    resting_cost: float = 0.0        # this market's unconfirmed orders: fee-inclusive committed capital
    allow_partial_fix: bool = False  # a risk-capped fix may improve the favourite without reaching its full target


@dataclass(slots=True)
class Approval:
    limit: float
    vwap: float                      # of the requested size at or under the limit
    fillable: float                  # shares available at or under the limit
    fee: float                       # estimated, on the VWAP
    cost: float                      # vwap x shares + fee
    unit_cost: float                 # fee-inclusive, per share


def walk_asks(levels: list[tuple[float, float]], shares: float, limit: float) -> tuple[float, float]:
    """(fillable shares at or under ``limit``, their VWAP) for a buy of ``shares``."""
    left, cost = shares, 0.0
    for price, size in sorted(levels, key=lambda l: l[0]):
        if price > limit + 1e-9 or left <= 1e-9:
            break
        take = min(left, size)
        cost += take * price
        left -= take
    got = shares - left
    return got, (cost / got if got > 0 else 0.0)


def check(c: Candidate, cfg: LeaderPairConfig, pf: Portfolio, fee_per_share) -> tuple[str | None, Approval | None]:
    """(refusal reason or None, the approval with the executable figures when approved)."""
    if not c.accepting:
        return "MARKET_STATUS", None
    if c.book_age_ms is None or c.book_age_ms > min(MAX_BOOK_AGE_MS, cfg.max_book_age_ms):
        return "STALE_DATA", None
    if c.seconds_left < max(MIN_TIME_REMAINING_S, cfg.min_time_remaining_s) or c.seconds_left > cfg.max_time_remaining_s:
        return "TIME_REMAINING", None
    if c.pending_in_market:
        return "PENDING_ORDER", None
    if c.end_guarded:
        return "END_GUARD", None
    if c.best_ask is None:
        return "NO_BOOK", None
    if c.best_bid is None:
        return "NO_BOOK", None
    if not (0.0 < c.best_bid < c.best_ask < 1.0):
        return "BAD_BOOK", None
    if c.shares < VENUE_MIN_SHARES - 1e-9:
        return "SIZE", None
    max_slippage = min(MAX_SLIPPAGE, cfg.max_slippage)
    max_trade_price = min(MAX_TRADE_PRICE, cfg.max_trade_price)
    limit = round(min(max_trade_price, c.best_ask + max_slippage), 2)
    if c.best_ask > max_trade_price + 1e-9 or c.best_ask < cfg.min_trade_price - 1e-9:
        return "PRICE_BAND", None
    if c.best_ask - c.best_bid > min(MAX_SPREAD, cfg.max_spread) + 1e-9:
        return "SPREAD", None
    fillable, vwap = walk_asks(c.ask_levels, c.shares, limit)
    if fillable < max(cfg.min_depth_shares, MIN_DEPTH_SHARES) - 1e-9:
        return "LIQUIDITY", None
    if fillable + 1e-9 < c.shares and fillable < c.shares * 0.999:
        # the book cannot absorb the whole order: a partial fill is NOT refused (the accounting reconciles it) when at
        # least min_depth_shares is there, but the strategy is told so through fillable < shares
        pass
    if vwap - c.best_ask > max_slippage + 1e-9:
        return "SLIPPAGE", None
    if c.kind != "init" and c.purchases_this_cycle >= min(MAX_PURCHASES_PER_CYCLE, cfg.max_purchases_per_cycle):
        return "PURCHASES_CYCLE", None
    if c.purchases_this_market >= min(MAX_PURCHASES_PER_MARKET, cfg.max_purchases_per_market):
        return "PURCHASES_MARKET", None
    fee = fee_per_share(vwap) * c.shares
    cost = vwap * c.shares + fee
    unit = cost / c.shares
    if pf.totals.c_total + c.resting_cost + cost > min(MAX_MARKET_CAPITAL_USDC, cfg.max_market_capital_usdc) + 1e-9:
        return "MARKET_CAPITAL", None
    if c.capital_other_markets + pf.totals.c_total + c.resting_cost + cost > min(
        MAX_ACCOUNT_CAPITAL_USDC, cfg.max_total_capital_usdc
    ) + 1e-9:
        return "TOTAL_CAPITAL", None
    up, down = pf.settlement_pnl_after(c.side, c.shares, unit)
    if min(up, down) < -min(MAX_SETTLEMENT_LOSS_USDC, cfg.max_settlement_loss_usdc) - 1e-9:
        return "SETTLEMENT_LOSS", None
    if c.kind in ("accum", "falling", "pair", "hedge", "cross_pair") and min(up, down) <= pf.totals.worst + 1e-9:
        return "NO_IMPROVEMENT", None
    if c.kind == "hedge" and max(up, down) < cfg.hedge_min_best_usdc - 1e-9:
        return "BEST_CASE_FLOOR", None
    if c.kind == "fix" and not c.allow_partial_fix:
        leader_pnl = up if c.side == "UP" else down
        if leader_pnl < cfg.fix_target_usdc - 1e-9:
            return "TARGET_UNACHIEVABLE", None
    if c.balance is not None and cost > float(c.balance) + 1e-9:
        return "BALANCE", None
    return None, Approval(limit=limit, vwap=vwap, fillable=fillable, fee=fee, cost=cost, unit_cost=unit)
