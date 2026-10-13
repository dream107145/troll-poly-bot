"""RISING-TOKEN RECOVERY (the user's spec, 2026-10-09): maker bids on the rising token; when the rise ends, the
opposite token is bought -- only as pairs that cost under $1 -- in a quantity worked out from the market's UP / DOWN
inventory and its total cost; buying stops at a guaranteed-profit target or a capital / loss limit.

EXPERIMENTAL, PAPER ONLY. ``TPB_RTR_ENABLED=1`` is the feature flag and there is NO real-money switch
(__main__.check_rising_recovery refuses shadow, micro-live and live). Env prefix ``TPB_RTR_``; own data tag and budget.
Not backtested (the spec was implemented as written, 2026-10-09).

Per market, the inventory: U / D shares, C = everything paid (fees included), the UNPAIRED lots of the side with more
shares (FIFO, each at its all-in cost). After every fill: P_UP = U - C, P_DOWN = D - C, worst = min(U, D) - C.

1. Rising token -> maker bid. A token whose ask rose >= ``min_rise`` over ``lookback_ms`` on rising updates (the
   book_momentum signal), ``entry_min_left_s`` .. ``entry_max_left_s`` left, spread <= ``max_spread``, price in
   ``min_ask`` .. ``max_ask``: a post-only bid AT the best bid (1c better on a 2c spread), ``entry_usdc`` (>= 5 shares).
   It must improve expected profit: q = the market mid (no model), q - price - ``entry_adverse_selection`` >=
   ``entry_min_edge``. Pulled after ``entry_max_rest_s``, when the bid moves 1c away, when the token turns down, or once
   the market is pairing. At most ``max_entry_fills`` entry fills and ``max_entry_cost_usdc`` per cycle.
2. The rise ends -> pairing. The lead token's mid fell ``reversal_drop`` below its peak since the cycle's first fill,
   or it made no new peak for ``stall_s``, or the OTHER token now shows the rise signal. Entry bids are pulled.
3. Pairs under $1 only. A pair = one unpaired lead share + one opposite share. The opposite buy is allowed only when
   avg cost of the unpaired lead lots + its own all-in price <= 1 - margin: TAKEN (fill-and-kill at the ask, the fee
   added) when that holds at the ask with ``pair_margin_taker`` (the locked profit must pay for the taker's EV cost),
   otherwise a post-only bid at min(best bid (+1c on a 2c spread), 1 - ``pair_margin`` - avg) resting until filled,
   re-priced when that price moves 1c, pulled ``pair_min_left_s`` before the close. Never bought "to recover a loss":
   a pair at or above $1 is refused however much the lead has fallen.
   Quantity: n = (T - worst) / (1 - all-in price), at least the venue's 5 shares, at most the unpaired lead (more only
   when the overshoot still raises the worst case), within the market's capital.
4. After every fill the market's P&L under both outcomes is logged ([RTR_FILL]).
5. Stop: worst = min(U, D) - C >= ``target_profit_usdc`` -> DONE (bids pulled, held to settlement). Entries also stop at
   ``max_market_cost_usdc`` (all buys), a worst case below -``max_market_loss_usdc``, ``max_total_cost_usdc`` open across
   markets, or the risk manager's daily stop (pairing buys, which only raise the worst case, continue).
   A fully paired market below the target starts a new cycle (entries again) while its capital lasts.
Everything is held to settlement: no sells.

HIGH : LOW RATIO (the user, 2026-10-09 15:5x: "buy high price token as 2 times of low token shares"). With
``high_ratio`` > 1 the pairing step no longer stops at equal shares: when the rise ends, the token with the higher mid
becomes the market's HIGH side, and the bot buys until high shares = ``high_ratio`` x low shares (the venue's 5-share
minimum respected; a shortfall under 2.5 shares counts as reached). Then the market is DONE (held to settlement).
  * shares that pair 1:1 (bought against the other side's unpaired lots) still need avg + price <= 1 - margin (taken
    only when the whole order pairs and the taker margin holds);
  * the extra high shares (the lean on the favourite) go in only as post-only bids at the bid;
  * every pairing buy must keep the worst case >= -``max_market_loss_usdc`` and the market within
    ``max_market_cost_usdc``; when no 5-share buy fits, the market is DONE at its limit;
  * under the risk manager's daily stop only buys that raise the worst case are sent.
``high_ratio`` 1 is the equal-pairs rule above (target / new cycles).

MOMENTUM MODE (``mode`` = "momentum", the user 2026-10-09 16:2x: "Don't think it should make exact pairs. Goal is make
positive P&L, maximize profit and minimize loss. So the bot should buy predicted rising tokens continuously"). No
pairing, no ratio, no reversal phase: every time a token shows the rise signal (``min_rise`` over ``lookback_ms`` on
rising updates, fresh, spread / price band as above, ``entry_min_left_s`` .. ``entry_max_left_s`` left) it is bought --
UP or DOWN, again and again through the market -- and the inventory leans however the momentum went.
  * buy: ``buy_mode`` "maker" = a post-only bid at the bid (1c better on a 2c spread), pulled after
    ``entry_max_rest_s`` / when the bid moves 1c away / when the token turns down; "taker" = fill-and-kill at the
    ask + ``max_slippage``; ``entry_usdc`` a buy (>= 5 shares), ``entry_cooldown_s`` per token, one order per token
  * loss control: the market's spend <= ``max_market_cost_usdc``, its worst outcome min(U, D) - C >= -
    ``max_market_loss_usdc`` after the buy (a buy that RAISES the worst case is always allowed within the spend),
    ``max_market_fills``, open cost across markets <= ``max_total_cost_usdc``; under the daily stop only buys that
    raise the worst case
  * profit lock: once min(U, D) - C >= ``target_profit_usdc`` (profit whichever side wins) the market stops buying
  * FAVOURITE FIX (the user 16:3x: "if expected P&L is negative, make positive to buy high price token"): the HIGH token
    is the one whose mid is >= ``high_min_mid``; when the market's P&L if it wins (its shares - C) is under
    ``high_win_target_usdc``, it is bought -- n = (target - that P&L) / (1 - its all-in price), >= 5 shares, cut to the
    spend / worst-case limits and the daily stop like every buy -- before any momentum buy, until ``pair_min_left_s``
    before the close. (The probability-weighted value at market prices, q U + (1 - q) D - C, cannot be raised by buying
    at the market's price; the favourite's outcome can.) A momentum buy of the UNDERDOG that would push the favourite's
    outcome under the target is refused (paper 16:35: such buys undid the fix within seconds, the fix bought again, and
    the market lost both ways after 20 s).
  * POSITIVE STATE (the user 16:4x: "if current or predicted P&L is positive, do not buy lower price token as current
    P&L will be negative. in this case, buy under 0.2 tokens to minimize max loss of worst case"): when the P&L if
    the higher-priced token wins, or the P&L at the current mids, is positive, the lower-priced token gets no momentum
    buys; it is bought only below ``hedge_max_price`` (0.20) as a HEDGE that lifts the worst outcome: n =
    min(P_fav - P_dog, (P_fav - ``high_win_target_usdc``) / price) -- the two outcomes evened out (the smallest
    max loss) without taking the favourite's outcome under the target -- >= 5 shares, limits as every buy.
  COVER (the user 10-10 02:3x: "When buy rising high price tokens, buy small amount of lower tokens after for a while,
  after it priced down. if opposite ask is not fill and opposite token is start to rising, buy it for reduce loss.
  Repeat it. When buy tokens, maximize profit and minimize loss for settlement"), ``cover_enabled``: every momentum
  fill starts a cover cycle on the other token --
    a) ``cover_delay_s`` after the fill, a small post-only bid (``cover_fraction`` x the unpaired lead shares, >= 5)
       ``cover_discount`` under the other token's bid and never above 1 - ``cover_margin`` - the lead's average cost
       (the covered shares pair under $1); it rests until filled, the close, or b);
    b) if that bid has not filled and the other token shows the rise signal, the bid is pulled and the token is TAKEN
       (``cover_take_fraction`` x the unpaired lead shares, ask <= ``cover_take_max_price``) to cut the loss;
    one bid and one take per momentum fill, so the cycle repeats with each new momentum buy.
  ``no_double_loss``: no buy may leave the market losing on BOTH outcomes when one of them was a profit before it.
  Order of a pass: the cover, the favourite fix, the hedge, a momentum buy.

OPT MODE (``mode`` = "opt", the user's consolidated spec 2026-10-13): one decision per pass at most, from these events --
the 6-s timer, the 3-s momentum timer (the lower-priced token rising through 0.30-0.40, until it reverses 2c from its
peak, reaches 0.50, leaves the zone or a limit stops it), a token's book rise signal, a confirmed flip of BTC's 2-s
direction. Initial entry: once, at >= 2 s after the open (and <= opt_init_max_s), 10 shares in the direction of BTC's
2-s move when |move| > 0.3 bp, subject only to the price filter and the limits. Every other buy comes from the
optimizer: for each side and each candidate quantity the fill is simulated against the ask depth (partial when
short) with the market's fee and a slippage allowance; the effective price must be in 0.10..0.90; the resulting
PnL_U = U - C and PnL_D = D - C give W = min, B = max; the candidate with the highest W wins (ties by B), and only
if W improves on no-trade by opt_min_improvement. ``opt_require_positive`` = "both" additionally demands PnL_U > 0
and PnL_D > 0 after the buy (else the trade is "infeasible" and skipped). Fees are the market's own schedule; orders
are fill-and-kill at the ask + max_slippage; one order per event, 1 s between orders, stale / invalid books rejected,
no re-sends (the next event decides afresh). Logged as [RTR_OPT] JSON.

SPOT MODE (``mode`` = "spot", the user 2026-10-12): BTC spot and the strike drive the buys, the inventory is tracked as
above (PnL_Up = U - C, PnL_Down = D - C, C = every fill's price x size + its fee). Per market, edge-triggered events
with unique ids (``MarketBook.fired``), one order per pass, ``spot_min_interval_s`` between orders:
    init     the first pass with 1 s of spot history: BTC rose over the previous second -> ``spot_init_shares`` UP,
             otherwise DOWN (trigger "init")
    rev      the 1-s direction (|move| >= ``spot_min_move_bps``, held ``spot_confirm_passes`` passes) flips ->
             ``spot_rev_shares`` of the new direction's outcome (trigger "rev:<n>")
    cross    spot crosses the strike: above -> ``spot_init_shares`` UP, below -> DOWN (trigger "cross:<n>"; the first
             observation only arms it)
    leader   the leading outcome's mid crosses above ``spot_leader_mid`` (edge): a candidate buy of
             ``spot_init_shares`` of it is sent only if PnL_Up AND PnL_Down after it are >= ``spot_leader_target_usdc``
    near     spot comes within ``spot_near_strike_pct`` of the strike (edge): candidate buys of either outcome, sent
             only if the worst-case settlement PnL improves
Each event becomes a WANT (trigger id -> side, shares) that is re-sent, one order per pass and ``spot_min_interval_s``
apart, until its shares are filled (a remainder under half the venue minimum counts as filled) or the market closes;
the leader / near-strike conditions are re-checked at every send. Nothing is sent before ``spot_open_delay_s`` after the
open. ``spot_pnl_rule``: while the market's mark-to-market PnL (U x mid_up + D x mid_down - C) is negative the
lower-priced token is not bought (that want is dropped).
CLIMB (``spot_climb_*``): while the leader (the higher-mid token) is still rising (mid above its mid
``spot_climb_lookback_s`` ago) and its ask is under ``spot_climb_max_ask``, it is bought every ``spot_climb_interval_s``
in ``spot_climb_shares`` lots, up to ``spot_climb_max_shares`` a market (trigger "climb:<n>"). With
``spot_climb_min_rise_1s`` > 0 the cadence is replaced by a JUMP trigger: a lot is bought when the leader's mid has risen
at least that much over the last second (edge-triggered: the rise must fall under the threshold before the next lot).
``spot_pair_fraction`` scales the pair buy (0.5 = half the full-cover amount).
PAIR (after the climb: its cap reached, or the leader no longer qualifies): if n shares of the cheap token at its
all-in price u make its outcome positive -- n = ceil((C - cheap_shares + target) / (1 - u)) -- while the leader's
outcome stays >= target (leader_shares - C - n u >= target), that n is bought (trigger "pair:<episode>", once per
climb episode; exempt from the PnL rule since its own condition is stronger).
Orders: fill-and-kill at the ask + ``max_slippage``, the size cut to the ask depth within that limit (the paper venue
walks the depth, fills partially, charges the market's own fee schedule and records slippage). Limits:
``spot_max_shares`` per outcome, ``spot_max_cost_usdc`` per market (fees and a slippage allowance included), no order
on a stale or invalid book (quote age > max_quote_age_ms, missing / crossed / out-of-range quotes) or stale spot
(> max_spot_age_ms), none inside ``pair_min_left_s`` of the close, none while the guard pauses entries, only
worst-case-raising buys under the risk manager's halts. Every decision is one [RTR_SPOT] JSON line.
  ORDER TYPES (the user 16:5x: "its fill rate is very poor. improve it"; 16:51-16:57 the maker momentum bids filled 4 of
  22 -- 15 pulled because the rising token's bid moved away -- and the fix bids 5 of 12): ``buy_mode`` (momentum) and
  ``fix_mode`` default to "taker" (fill-and-kill at the ask + ``max_slippage``, the fee in the sizing; a momentum buy
  puts its token on ``entry_cooldown_s`` so one move is not bought every pass); ``hedge_mode`` "auto" takes the cheap
  token when its ask + fee is under ``hedge_max_price`` and otherwise bids under it. (Our 10-09 study: on these signals
  a taker buy held to the close lost 1.6c a share, a filled maker bid 4.6c -- the bids fill when the price falls back.)
Held to settlement.
"""
from __future__ import annotations

import collections
import json
import logging
import math
from dataclasses import dataclass

from ..features.engine import FeatureEngine
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .book_momentum import BookMomentumConfig, BookMomentumEngine, Signal, taker_fee
from .engine import EngineConfig, Evaluation, Reason

log = logging.getLogger(__name__)

ENABLE_ENV = "TPB_RTR_ENABLED"
STRATEGY_ID = "rising_recovery"
VENUE_MIN = 5.0


@dataclass(slots=True)
class RisingRecoveryConfig(BookMomentumConfig):
    ENV_PREFIX = "TPB_RTR_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    # --- 1. the rising token (the book_momentum signal fields: lookback_ms, min_rise, confirm_updates, ...) ---
    lookback_ms: float = 1000.0
    min_rise: float = 0.02
    max_signal_age_ms: float = 500.0
    min_ask: float = 0.20
    max_ask: float = 0.80
    entry_min_left_s: float = 60.0
    entry_max_left_s: float = 285.0
    entry_usdc: float = 3.0
    entry_max_usdc: float = 5.0
    entry_max_rest_s: float = 5.0
    entry_adverse_selection: float = 0.0     # what a filled bid is expected to give back; the 10-09 study measured 5.7c
    entry_min_edge: float = 0.0
    max_entry_fills: int = 3                 # per cycle
    max_entry_cost_usdc: float = 10.0        # per cycle
    entry_cooldown_s: float = 3.0
    # --- 2. the rise ends ---
    reversal_drop: float = 0.02
    stall_s: float = 20.0
    # --- 3. pairs under $1 ---
    pair_margin: float = 0.01                # maker pair bid: avg lead + bid <= 1 - this
    pair_margin_taker: float = 0.02          # taken pair: avg lead + ask + fee <= 1 - this
    pair_taker: bool = True
    pair_min_left_s: float = 10.0
    high_ratio: float = 2.0                  # pairing ends with high-token shares = this x low-token shares (1 = pairs)
    # --- the mode ---
    mode: str = "momentum"                   # "momentum": buy every rising token continuously | "pairs": the pairing rules
    buy_mode: str = "taker"                  # momentum mode: "taker" FAK at the ask | "maker" post-only bid at the bid
    fix_mode: str = "taker"                  # the favourite fix: "taker" | "maker"
    hedge_mode: str = "auto"                 # the cheap hedge: "auto" (take when ask + fee < hedge_max_price) | "taker" | "maker"
    max_market_fills: int = 40               # momentum mode: buys a market may fill
    entry_signal: str = "rise"               # momentum mode: "rise" = the rise signal | "favourite" = the higher-mid token
                                             # whenever its ask is in min_ask..max_ask (no rise needed), entry_cooldown_s apart
    fix_high: bool = True                    # momentum mode: buy the favourite when its winning outcome is negative
    high_min_mid: float = 0.55               # the favourite: mid at least this
    high_win_target_usdc: float = 0.10       # ... and the P&L if it wins must reach this
    fix_max_price: float = 1.0               # no fix above this all-in price (10-12 02:04: fixes at 0.93-0.99 wanted
                                             # 59 -> 4,735 shares: n = need / (1 - price) explodes near 1)
    fix_max_per_market: int = 0              # fix fills a market may take (0 = no limit; 10-12 02:38 fixes flip-flopped
                                             # DOWN -> UP in one market as the favourite changed)
    fix_partial: bool = True                 # False: a fix the limits cut short is not sent (replay 18:3x: that LOST more,
                                             # -0.015 vs +0.039 a market, though one cut fix at 18:13 left -10 / -2.35)
    hedge_max_price: float = 0.20            # positive state: the lower-priced token only below this, as a hedge
    hedge_enabled: bool = True               # the cheap hedge on / off
    # --- spot mode (see the module doc) ---
    spot_init_shares: float = 10.0
    spot_rev_shares: float = 5.0             # 50 % of the initial size
    spot_min_move_bps: float = 1.0           # the 1-s direction needs at least this move
    spot_confirm_passes: int = 2             # ... held for this many passes to be "confirmed"
    spot_leader_mid: float = 0.75
    spot_leader_target_usdc: float = 5.0     # both settlement outcomes must reach this after the candidate buy
    spot_near_strike_pct: float = 0.05       # the near-strike zone: |spot - strike| / strike <= this %
    spot_max_shares: float = 100.0           # per outcome
    spot_max_cost_usdc: float = 100.0        # per market, fees and slippage included
    spot_min_interval_s: float = 1.0
    spot_slippage_allowance: float = 0.01    # per share, counted against spot_max_cost_usdc before the fill
    spot_open_delay_s: float = 5.0           # no buy before this many seconds after the open (the user, 10-12 14:5x)
    spot_retry_log_s: float = 5.0            # a pending want's transient refusals are logged at most this often
    spot_pnl_rule: bool = True               # the market's mark-to-market PnL must be >= 0 to buy the lower-priced token
    # the climb + pair rules (the user, 10-12 15:1x)
    spot_climb_enabled: bool = True
    spot_climb_shares: float = 10.0          # one climb buy
    spot_climb_max_shares: float = 50.0      # climb shares a market may buy
    spot_climb_interval_s: float = 5.0       # one climb buy per this many seconds
    spot_climb_max_ask: float = 0.90         # the leader is bought only under this ask
    spot_climb_lookback_s: float = 5.0       # "still rising": the leader's mid is above its mid this long ago
    spot_pair_target_usdc: float = 0.10      # the pair buy: both outcomes >= this after it
    spot_climb_min_rise_1s: float = 0.0      # > 0 (the user, 10-12 16:1x): the leader is bought only on a jump of at least
                                             # this over 1 s (edge-triggered), no interval cadence
    spot_pair_fraction: float = 1.0          # the pair buy = this x the full-cover amount (the user 16:1x: 0.5)
    spot_init_enabled: bool = True           # the rule switches (the user 16:4x: "only if the token rose 10c" -> all off
    spot_rev_enabled: bool = True            # but the jump trigger and the pair rule)
    spot_cross_enabled: bool = True
    spot_leader_enabled: bool = True
    spot_near_enabled: bool = True
    spot_climb_any_token: bool = False       # jump trigger on whichever token jumped (not only the leader)
    spot_cover_enabled: bool = False         # after the initial buy fills: a resting bid on the OTHER token (the user 18:3x)
    spot_cover_fraction: float = 0.5         # ... for this fraction of the initial shares (>= the venue minimum)
    spot_cover_pair_cost: float = 0.80       # ... at (this - the initial fill price): the pair would cost this
    spot_climb_reset_drop: float = 0.0       # > 0 (the user 19:1x): once the climbed token fell this much from its peak and
                                             # jumps again, the climb count restarts (a new cycle up to the cap)
    spot_low_ratio_max: float = 0.0          # > 0 (the user 20:5x): a pairing buy (cover / pair / guard) of the LOWER-priced
                                             # token may not take its shares above this x the higher-priced token's shares
    # --- OPT mode (the user's consolidated spec, 2026-10-13; "OPT MODE" in the module doc) ---
    opt_open_window_s: float = 2.0           # the initial signal needs 2 s of BTC after the open (never future prices)
    opt_init_bps: float = 0.3                # |BTC move over 2 s| > this -> the initial 10-share buy in that direction
    opt_init_shares: float = 10.0
    opt_init_max_s: float = 30.0             # no "initial" entry later than this after the open
    opt_price_min: float = 0.10              # the safe effective-price range (fees + slippage in)
    opt_price_max: float = 0.90
    opt_eval_interval_s: float = 6.0         # the normal decision timer
    opt_mom_interval_s: float = 3.0          # the momentum timer while the lower-priced token rises through the zone
    opt_mom_low: float = 0.30
    opt_mom_high: float = 0.40
    opt_mom_target: float = 0.50             # assumed target: logged as a projection and ends the momentum phase
    opt_quantities: str = "5,10,15,20"       # candidate quantities (no-trade is always a candidate)
    opt_min_improvement: float = 0.05        # a buy must raise the worst-case PnL by at least this to be worth its costs
    opt_tie_eps: float = 0.01                # worst cases within this are "effectively equal": the best case decides
    opt_require_positive: str = "none"       # "both": only buys that leave BOTH outcomes > 0 (else "infeasible", skipped)
    spot_lowrise_enabled: bool = False       # the user 21:2x: the LOWER-priced token starting to rise (the book signal:
    spot_lowrise_shares: float = 10.0        # ask +min_rise over lookback_ms on rising updates) is bought this many shares,
    spot_lowrise_max_per_market: int = 2     # once per rise, up to this many buys a market; the guard then recovers
    spot_guard_enabled: bool = False         # the PnL guard (the user 19:2x: "keep positive PnL by gathering rising tokens")
    spot_guard_min_rise_1s: float = 0.02     # a token rising this much over 1 s ...
    spot_guard_target_usdc: float = 0.10     # ... is bought until the market's PnL if it wins reaches this
    spot_guard_max_shares: float = 10.0      # per guard order
    spot_cover_mode: str = "bid"             # "bid": the resting bid above | "rise" (the user 18:5x): no resting order --
    spot_cover_min_rise_1s: float = 0.02     # the other token is TAKEN once its mid rises this much over 1 s
    spot_init_signal: str = "btc"            # the initial entry's side: "btc" = BTC's 1-s direction | "token" = the token
                                             # whose mid rose more over the last second (BTC decides when neither rose)
    cover_enabled: bool = False              # the cover cycle after each momentum fill (see the module doc)
    cover_delay_s: float = 10.0
    cover_fraction: float = 0.5
    cover_discount: float = 0.02
    cover_margin: float = 0.01
    cover_take_fraction: float = 1.0
    cover_take_max_price: float = 0.60
    cover_take_min_rise: float = 0.0         # the take's own rise threshold (0 = min_rise)
    cover_take_min_price: float = 0.0        # take only once the other token's ask is at least this (the turn is real)
    no_double_loss: bool = False
    positive_rule: bool = True               # positive state: no momentum buys of the lower-priced token
    end_guard_s: float = 3.0                 # no new order on a token this long after one ended unfilled (a late fill)
    # --- 5. target and limits ---
    target_profit_usdc: float = 0.20
    max_market_cost_usdc: float = 20.0
    max_market_loss_usdc: float = 10.0
    max_total_cost_usdc: float = 40.0
    # the shared budget the risk manager sees
    max_total_exposure_usdc: float = 40.0
    max_daily_loss_usdc: float = 20.0
    max_order_usdc: float = 10.0
    max_simultaneous_positions: int = 4


@dataclass
class Lot:
    shares: float
    unit: float                              # all-in cost per share (price + fee / share)


class MarketBook:
    """One market's inventory and cycle state."""

    def __init__(self, slug: str, close_ts: float, tokens: dict[str, str] | None = None) -> None:
        self.slug, self.close_ts = slug, close_ts
        self.tokens = tokens or {}                         # side -> token id
        self.U = self.D = self.C = 0.0
        self.CU = self.CD = 0.0              # per-side acquisition cost, fees included (Avg_U = CU / U)
        self.fees = 0.0
        self.lots: dict[str, list[Lot]] = {"UP": [], "DOWN": []}   # unpaired shares (only one side has any)
        self.phase = "flat"                  # flat -> long -> pairing -> (flat: next cycle) | done
        self.lead: str | None = None
        self.high: str | None = None         # ratio mode: the token with the higher mid when the rise ended
        self.peak = 0.0
        self.peak_t = 0.0
        self.cycle_fills = 0
        self.cycle_cost = 0.0
        self.cycles = 0
        self.locked_pairs = 0.0
        self.why_done = ""
        # the cover cycle: one per momentum fill
        self.lead_seq = 0                    # momentum fills so far
        self.lead_t = 0.0
        self.lead_side: str | None = None
        self.cover_seq = 0                   # the momentum fill whose cover bid was placed
        self.cover_filled_seq = 0            # ... whose cover bid filled
        self.take_seq = 0                    # ... whose reversal take was sent
        self.fix_orders: set[str] = set()    # favourite-fix orders that filled in this market (partial fills: one)
        # spot mode
        self.fired: set[str] = set()         # trigger ids already acted on (duplicate suppression)
        self.direction: int = 0              # the confirmed 1-s spot direction (+1 / -1), 0 = none yet
        self.cand_dir: int = 0               # the direction seen on the last pass ...
        self.cand_passes: int = 0            # ... and for how many passes in a row
        self.rev_n = self.cross_n = self.leader_n = self.near_n = 0
        self.above: bool | None = None       # spot above the strike at the last pass (None = not seen yet)
        self.leader_was_above: dict[str, bool] = {}
        self.in_near_zone: bool | None = None
        self.last_order_t: float = -1e18
        self.last_spot: dict = {}
        self.wants: dict[str, dict] = {}     # trigger id -> {side, shares, filled, kind, t, logged_t}: re-sent until filled
        self.climb_n = 0
        self.climb_bought = 0.0              # climb shares filled in this market
        self.climb_last_t: float = -1e18     # the last climb want
        self.climb_episode = 0               # bumped by each climb fill; the pair rule fires once per episode
        self.pair_episode_done = -1
        self.blocked_log_t: float = -1e18    # the last logged "blocked" reject
        self.climb_armed = True              # jump trigger: re-armed once the 1-s rise is under the threshold
        self.climb_side: str | None = None   # the side the last climb lot bought (the pair rule covers the OTHER side)
        self.guard_armed: dict[str, bool] = {"UP": True, "DOWN": True}   # the PnL guard: one order per rise
        self.lowrise_armed = True
        self.lowrise_n = 0
        # opt mode
        self.opt_last_eval_t: float = -1e18
        self.opt_mom_last_t: float = -1e18
        self.opt_in_mom = False
        self.opt_mom_peak: float | None = None
        self.opt_dir = 0
        self.opt_rise_armed: dict[str, bool] = {"UP": True, "DOWN": True}
        self.opt_n = 0
        self.opt_init_filled = 0.0
        self.opt_init_tries = 0
        self._tops = None                    # the latest books (for the liquidation / outcome reports)
        self.guard_n = 0
        self.climb_peak: float | None = None # the climbed token's highest mid since its last climb fill
        self.climb_dipped = False            # ... and whether it has fallen spot_climb_reset_drop from there
        self.init_px: float | None = None    # the initial buy's average fill price (the cover bid hangs off it)
        self.init_side: str | None = None
        self.init_shares_filled = 0.0

    @property
    def worst(self) -> float:
        return min(self.U, self.D) - self.C

    def excess(self) -> tuple[str | None, float, float]:
        """(the side holding unpaired shares, how many, their average all-in cost)."""
        for side in ("UP", "DOWN"):
            q = sum(lot.shares for lot in self.lots[side])
            if q > 1e-9:
                return side, q, sum(lot.shares * lot.unit for lot in self.lots[side]) / q
        return None, 0.0, 0.0

    def add(self, side: str, shares: float, price: float, fee: float) -> float:
        """Book a fill; returns the pairs it completed."""
        unit = price + fee / shares if shares > 0 else price
        if side == "UP":
            self.U += shares
        else:
            self.D += shares
        self.C += shares * price + fee
        if side == "UP":
            self.CU += shares * price + fee
        else:
            self.CD += shares * price + fee
        self.fees += fee
        other = "DOWN" if side == "UP" else "UP"
        left, paired = shares, 0.0
        while left > 1e-9 and self.lots[other]:
            lot = self.lots[other][0]
            take = min(left, lot.shares)
            lot.shares -= take
            left -= take
            paired += take
            if lot.shares <= 1e-9:
                self.lots[other].pop(0)
        if left > 1e-9:
            self.lots[side].append(Lot(left, unit))
        self.locked_pairs += paired
        return paired

    def shares(self, side: str) -> float:
        return self.U if side == "UP" else self.D

    def line(self) -> str:
        side, q, avg = self.excess()
        un = f"unpaired {side} {q:.2f} @ {avg:.3f}" if side else "fully paired"
        return (f"U {self.U:.2f} D {self.D:.2f} C {self.C:.2f} | if UP wins {self.U - self.C:+.2f}, if DOWN wins "
                f"{self.D - self.C:+.2f}, worst {self.worst:+.2f} | {un} | phase {self.phase}")


class RisingRecoveryEngine(BookMomentumEngine):
    """The book_momentum engine's book history and signal; every order goes through the pair-order hooks."""

    feature_key = STRATEGY_ID
    budget_is_ceiling = True
    allows_reentry = True
    pair_retry = False
    wants_books = True
    #: set by live.py each pass: the guard pauses entries (a feed drop, its cooldown, ...) -> books are still recorded,
    #: nothing is placed (paper 10-09 19:23: a favourite fix went out 6 s after a book-feed drop and lost the market)
    guard_paused = False

    def __init__(self, cfg: EngineConfig, scfg: RisingRecoveryConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the rising_recovery strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, RisingRecoveryConfig):
            raise TypeError("RisingRecoveryEngine needs a RisingRecoveryConfig")
        super().__init__(cfg, scfg, risk, features)
        self.books: dict[str, MarketBook] = {}
        #: every order sent, kept past its end: live.py reports a fill AFTER on_pair_done has ended the order
        #: (paper 10-09 15:42: two fills were lost that way), and a cancel can lose the race to a fill
        self.orders: dict[str, dict] = {}
        #: token -> no new order before this time: live.py also ends a pair order on the paper venue's intermediate
        #: "accepted" answer (and a cancel can lose to a fill), and the fill may still come (paper 16:46: a second hedge
        #: was placed beside the first, both filled, the locked market lost on one side)
        self.end_guard: dict[str, float] = {}
        self._now = 0.0
        #: spot mode: BTC spot history per asset [(ms, price)] and the latest book / spot ages per market, written by
        #: evaluate() (live.py hands them there each tick) and read by pair_requests()
        self.spot_hist: dict[str, collections.deque] = {}
        self.market_data: dict[str, dict] = {}
        self.rr_stats = collections.Counter()

    # ------------------------------------------------------------------ helpers

    def _book(self, m) -> MarketBook:
        b = self.books.get(m.slug)
        if b is None:
            b = self.books[m.slug] = MarketBook(m.slug, m.close_ts, {"UP": m.yes_token_id, "DOWN": m.no_token_id})
        return b

    def _mid(self, token: str) -> float | None:
        last = self.last_top.get(token)
        if last is None or last[1] is None or last[2] is None:
            return None
        return (last[1] + last[2]) / 2.0

    def _resting_on(self, token: str) -> dict | None:
        for oid, o in self.resting.items():
            if o["token"] == token:
                return {"id": oid, **o}
        if self._now < self.end_guard.get(token, -1e18):
            return {"id": None, "token": token, "state": "ended, a fill may still come"}
        return None

    def open_cost(self) -> float:
        """USDC in markets still open + every resting bid."""
        held = sum(b.C for b in self.books.values() if b.phase != "settled")
        return held + sum(o["price"] * o["size"] for o in self.resting.values())

    def _halted(self) -> str | None:
        h = self.risk.halted()                          # a RiskVerdict (always truthy): read .ok
        return None if h.ok else h.detail

    def _done(self, b: MarketBook, why: str) -> None:
        if b.phase != "done":
            b.phase, b.why_done = "done", why
            self.rr_stats["markets_done"] += 1
            log.info("[RTR_DONE] %s %s | %s", b.slug, why, b.line())

    # ------------------------------------------------------------------ the taker path is unused

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        m = meta.market
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=m.seconds_remaining(now), reason=None)
        self.meta_by_slug[m.slug] = meta
        self._close_ts[m.slug] = m.close_ts
        for tok, bk in ((m.yes_token_id, book_up), (m.no_token_id, book_down)):
            if bk is not None:
                self._record(tok, now, bk.best_bid, bk.best_ask)
        ev.p_market = book_up.mid if book_up is not None else None
        ev.p_used = ev.p_market if ev.p_market is not None else 0.5
        if self.scfg.mode in ("spot", "opt"):
            self._capture_spot(m, spot, spot_age_ms, book_age_ms, quote_ages, now)
        self._cleanup(now)
        b = self.books.get(m.slug)
        state = b.phase if b else "flat"
        return self._reject(ev, Reason.WEAK_MOMENTUM, f"maker / pair orders only (market {state})")

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:      # pragma: no cover - never traded
        raise RuntimeError("rising_recovery places every order through pair_requests")

    # ------------------------------------------------------------------ orders

    def pair_requests(self, now: float, markets: dict | None = None, books: dict | None = None,
                      balance: float | None = None) -> list[tuple[str, str, Order]]:
        sc = self.scfg
        self._now = max(self._now, now)
        out: list[tuple[str, str, Order]] = []
        for slug, m in (markets or {}).items():
            if not sc.asset_ok(m.asset) or f"-updown-{sc.market_type}-" not in slug or now < m.open_ts:
                continue
            left = (m.close_ts - now) / 1000.0
            if left <= 0:
                continue
            bk = (books or {}).get(slug) or (None, None)
            tops = {"UP": (m.yes_token_id, bk[0]), "DOWN": (m.no_token_id, bk[1])}
            for side, (tok, ob) in tops.items():
                if ob is not None:
                    self._record(tok, now, ob.best_bid, ob.best_ask)
            self._close_ts[slug] = m.close_ts
            b = self._book(m)
            if self.guard_paused:
                self.rr_stats["guard_paused_passes"] += 1
                continue
            if b.phase == "done":
                continue
            if sc.mode == "spot":
                o = self._spot_pass(b, m, tops, now, left, balance)
                if o is not None:
                    out.append(o)
                continue
            if sc.mode == "opt":
                o = self._opt_pass(b, m, tops, now, left, balance)
                if o is not None:
                    out.append(o)
                continue
            if b.worst >= sc.target_profit_usdc - 1e-9 and (b.U > 0 or b.D > 0):
                self._done(b, f"target reached: worst {b.worst:+.2f} >= {sc.target_profit_usdc:g}")
                continue
            if sc.mode == "momentum":
                o = self._cover_order(b, m, tops, now, left, balance)
                if o is None:
                    o = self._fix_order(b, m, tops, now, left, balance)
                if o is None:
                    o = self._hedge_order(b, m, tops, now, left, balance)
                if o is None:
                    o = self._momentum_order(b, m, tops, now, left, balance)
                if o is not None:
                    out.append(o)
                continue
            self._track_lead(b, tops, now)
            o = self._pair_order(b, m, tops, now, left, balance)
            if o is not None:
                out.append(o)
            o = self._entry_order(b, m, tops, now, left, balance)
            if o is not None:
                out.append(o)
        return out

    def _track_lead(self, b: MarketBook, tops: dict, now: float) -> None:
        """The lead's peak since the cycle began; the end of its rise moves the market to pairing."""
        sc = self.scfg
        side, q, _ = b.excess()
        if side is None:
            if b.phase in ("long", "pairing") and sc.high_ratio <= 1.0 + 1e-9:
                b.phase, b.lead = "flat", None                  # fully paired: the next cycle may start
                b.cycle_fills, b.cycle_cost = 0, 0.0
            return
        if b.phase == "flat":
            b.phase, b.lead, b.cycles = "long", side, b.cycles + 1
            b.peak, b.peak_t = self._mid(tops[side][0]) or 0.0, now
        if b.phase != "long":
            return
        mid = self._mid(tops[b.lead][0])
        if mid is None:
            return
        if mid > b.peak + 1e-9:
            b.peak, b.peak_t = mid, now
        other = "DOWN" if b.lead == "UP" else "UP"
        s = self.signal(tops[other][0], other, now)
        why = None
        if mid <= b.peak - sc.reversal_drop + 1e-9:
            why = f"{b.lead} mid {mid:.3f} fell {100 * (b.peak - mid):.1f}c from its peak {b.peak:.3f}"
        elif sc.stall_s > 0 and now - b.peak_t >= sc.stall_s * 1000.0:
            why = f"{b.lead} made no new high for {sc.stall_s:g} s (peak {b.peak:.3f})"
        elif s is not None and s.confirmed and s.rise >= sc.min_rise - 1e-9:
            why = f"{other} is now rising ({100 * s.rise:+.1f}c in {sc.lookback_ms:.0f} ms)"
        if why:
            b.phase = "pairing"
            mids = {s: self._mid(tops[s][0]) for s in ("UP", "DOWN")}
            b.high = max(("UP", "DOWN"), key=lambda s: -1.0 if mids[s] is None else mids[s])
            self.rr_stats["reversals"] += 1
            ratio = f", high {b.high} (mid {mids[b.high]}) to {sc.high_ratio:g}x" if sc.high_ratio > 1.0 + 1e-9 else ""
            log.info("[RTR_REVERSAL] %s %s -> pairing%s | %s", b.slug, why, ratio, b.line())

    def quantity(self, b: MarketBook, all_in: float) -> float:
        """Shares of the opposite token: enough to lift the worst case to the target, at most the unpaired lead
        (rounded up to the venue minimum); 0 when no size helps."""
        sc = self.scfg
        side, excess, _ = b.excess()
        if all_in >= 1.0 or excess <= 0:
            return 0.0
        need = (sc.target_profit_usdc - b.worst) / (1.0 - all_in)
        n = min(excess, math.ceil(max(need, 0.0) * 100.0) / 100.0)
        n = max(n, VENUE_MIN)
        if n > excess + 1e-9:
            # an overshoot makes the other side short: allowed only if the worst case still rises
            U, D = (b.U, b.D + n) if side == "UP" else (b.U + n, b.D)
            if min(U, D) - (b.C + n * all_in) <= b.worst + 1e-9:
                return 0.0
        room = sc.max_market_cost_usdc - b.C
        if n * all_in > room + 1e-9:
            n = math.floor(room / all_in * 100.0) / 100.0
            if n < VENUE_MIN:
                return 0.0
        return round(n, 2)

    def ratio_need(self, b: MarketBook) -> tuple[str | None, float]:
        """(the side to buy, the shares it lacks) for high = high_ratio x low; (None, 0) once reached."""
        r = self.scfg.high_ratio
        hi = b.high or b.lead or "UP"
        lo = "DOWN" if hi == "UP" else "UP"
        H, L = b.shares(hi), b.shares(lo)
        if L < 1e-9 and H > 1e-9:
            return lo, H / r
        if H < 1e-9 and L > 1e-9:
            return hi, r * L
        need_hi, need_lo = r * L - H, H / r - L
        if need_hi >= VENUE_MIN / 2.0 - 1e-9:
            return hi, need_hi
        if need_lo >= VENUE_MIN / 2.0 - 1e-9:
            return lo, need_lo
        return None, 0.0

    def _worst_after(self, b: MarketBook, side: str, n: float, unit: float) -> float:
        U, D = (b.U + n, b.D) if side == "UP" else (b.U, b.D + n)
        return min(U, D) - (b.C + n * unit)

    def fit(self, b: MarketBook, side: str, n: float, unit: float) -> float:
        """n cut to the market's capital and worst-case limits (and, under the daily stop, to buys that raise the
        worst case); 0 when not even the venue minimum fits."""
        sc = self.scfg
        n = math.floor(min(n, (sc.max_market_cost_usdc - b.C) / unit) * 100.0 + 1e-6) / 100.0
        while n >= VENUE_MIN and self._worst_after(b, side, n, unit) < -sc.max_market_loss_usdc - 1e-9:
            n = round(n - 0.25, 2)
        if n < VENUE_MIN - 1e-9:
            return 0.0
        if self._halted() and self._worst_after(b, side, n, unit) < b.worst - 1e-9:
            return 0.0
        if self.double_loss(b, side, n, unit):
            self.rr_stats["double_loss_refused"] += 1
            return 0.0
        return n

    def double_loss(self, b: MarketBook, side: str, n: float, unit: float) -> bool:
        """no_double_loss: this buy would leave both outcomes negative where one was a profit before it."""
        if not self.scfg.no_double_loss:
            return False
        before = max(b.U, b.D) - b.C
        U, D = (b.U + n, b.D) if side == "UP" else (b.U, b.D + n)
        after = max(U, D) - (b.C + n * unit)
        return before >= -1e-9 and after < -1e-9

    def _ratio_order(self, b, m, tops, now, balance):
        sc = self.scfg
        side, need = self.ratio_need(b)
        if side is None:
            self._done(b, f"{sc.high_ratio:g}:1 reached (high {b.high})")
            return None
        tok, ob = tops[side]
        if ob is None or any(o["slug"] == b.slug and o["kind"] != "entry" for o in self.resting.values()):
            return None
        n = max(VENUE_MIN, math.ceil(need * 100.0 - 1e-6) / 100.0)
        ex_side, ex_q, ex_avg = b.excess()
        matched = min(n, ex_q) if ex_side is not None and ex_side != side else 0.0
        ask, bid = ob.best_ask, ob.best_bid
        role = "high" if side == b.high else "low"
        if (sc.pair_taker and matched >= n - 1e-9 and ask is not None
                and ex_avg + ask + taker_fee(ask) <= 1.0 - sc.pair_margin_taker + 1e-9):
            unit = ask + taker_fee(ask)
            q = self.fit(b, side, n, unit)
            if q <= 0:
                return self._limit_done(b, side, n, unit)
            if balance is None or q * unit <= float(balance) + 1e-9:
                o = Order(token_id=tok, side=Side.BUY, price=ask, size=q, tif=TimeInForce.IOC, expected_price=ask,
                          tag=f"{side} rising_recovery {role} TAKE {q:g} @ {ask:.2f} (pair {ex_avg + unit:.3f})")
                return self._hold(o, b, side, tok, ask, q, now, "pair_take", ex_avg)
        cap = math.floor((1.0 - sc.pair_margin - ex_avg) * 100.0 + 1e-9) / 100.0 if matched > 0 else None
        px = bid if bid is not None else cap
        if px is None:
            return None
        if bid is not None and ask is not None and ask - bid >= 0.02 - 1e-9:
            px = bid + 0.01
        if cap is not None:
            px = min(px, cap)
        if ask is not None and px >= ask - 1e-9:
            px = ask - 0.01                                   # post-only: never at or through the ask
        px = round(px, 2)
        if px < 0.01:
            self.rr_stats["pair_refused_cost"] += 1
            return None
        q = self.fit(b, side, n, px)
        if q <= 0:
            return self._limit_done(b, side, n, px)
        if balance is not None and q * px > float(balance) + 1e-9:
            return None
        o = Order(token_id=tok, side=Side.BUY, price=px, size=q, tif=TimeInForce.GTC, expires_ts=m.close_ts,
                  expected_price=px, post_only=True,
                  tag=f"{side} rising_recovery {role} bid {q:g} @ {px:.2f} (matched {matched:g}, cap {cap})")
        return self._hold(o, b, side, tok, px, q, now, "pair_bid", ex_avg if matched > 0 else None, cap=cap)

    def _limit_done(self, b, side, n, unit):
        if self._halted():
            return None                                        # the daily stop: wait, the next buy may raise the worst
        self._done(b, f"limit: {n:g} {side} at {unit:.3f} would break the market's {self.scfg.max_market_cost_usdc:g} USDC "
                      f"/ -{self.scfg.max_market_loss_usdc:g} worst case (high {b.high})")
        return None

    def _pair_order(self, b, m, tops, now, left, balance):
        sc = self.scfg
        if b.phase != "pairing" or left < sc.pair_min_left_s:
            return None
        if sc.high_ratio > 1.0 + 1e-9:
            return self._ratio_order(b, m, tops, now, balance)
        side, excess, avg = b.excess()
        if side is None:
            return None
        other = "DOWN" if side == "UP" else "UP"
        tok, ob = tops[other]
        if ob is None or self._resting_on(tok) is not None:
            return None
        ask, bid = ob.best_ask, ob.best_bid
        # (a) taken now, when the pair locks pair_margin_taker at the executable ask (the fee added)
        if sc.pair_taker and ask is not None and avg + ask + taker_fee(ask) <= 1.0 - sc.pair_margin_taker + 1e-9:
            all_in = ask + taker_fee(ask)
            n = self.quantity(b, all_in)
            if n > 0 and (balance is None or n * all_in <= float(balance) + 1e-9):
                o = Order(token_id=tok, side=Side.BUY, price=ask, size=n, tif=TimeInForce.IOC, expected_price=ask,
                          tag=f"{other} rising_recovery pair TAKE {n:g} @ {ask:.2f} (pair {avg + all_in:.3f})")
                return self._hold(o, b, other, tok, ask, n, now, "pair_take", avg)
        # (b) otherwise a resting bid that locks pair_margin if it fills
        cap = math.floor((1.0 - sc.pair_margin - avg) * 100.0 + 1e-9) / 100.0
        if bid is None:
            px = cap
        else:
            px = min(cap, bid + (0.01 if ask is not None and ask - bid >= 0.02 - 1e-9 else 0.0))
        if ask is not None and px >= ask - 1e-9:
            px = round(ask - 0.01, 2)                         # post-only: never at or through the ask
        if px < 0.01:
            self.rr_stats["pair_refused_cost"] += 1
            return None
        n = self.quantity(b, px)
        if n <= 0 or (balance is not None and n * px > float(balance) + 1e-9):
            return None
        o = Order(token_id=tok, side=Side.BUY, price=px, size=n, tif=TimeInForce.GTC, expires_ts=m.close_ts,
                  expected_price=px, post_only=True,
                  tag=f"{other} rising_recovery pair bid {n:g} @ {px:.2f} (pair {avg + px:.3f}, cap {cap:.2f})")
        return self._hold(o, b, other, tok, px, n, now, "pair_bid", avg, cap=cap)

    def _entry_order(self, b, m, tops, now, left, balance):
        sc = self.scfg
        if b.phase not in ("flat", "long") or not (sc.entry_min_left_s <= left <= sc.entry_max_left_s):
            return None
        if b.cycle_fills >= int(sc.max_entry_fills):
            return None
        halt = self._halted()
        if halt:
            self.rr_stats["entry_halted"] += 1
            return None
        best = None
        for side, (tok, ob) in tops.items():
            if ob is None or (b.phase == "long" and side != b.lead):
                continue
            s = self.signal(tok, side, now)
            if s is None or not s.confirmed or s.rise < sc.min_rise - 1e-9 or s.age_ms > sc.max_signal_age_ms:
                continue
            if best is None or s.rise > best[0].rise:
                best = (s, ob)
        if best is None:
            return None
        s, ob = best
        tok = s.token
        if self._resting_on(tok) is not None or now < self.cooldown.get(tok, -1e18):
            return None
        if s.spread > sc.max_spread + 1e-9 or s.spread < 0.01 - 1e-9 or not (sc.min_ask <= s.ask <= sc.max_ask):
            self.rr_stats["entry_band_or_spread"] += 1
            return None
        px = round(s.bid + (0.01 if s.spread >= 0.02 - 1e-9 else 0.0), 2)
        q = (s.bid + s.ask) / 2.0
        if q - px - sc.entry_adverse_selection < sc.entry_min_edge - 1e-9:
            self.rr_stats["entry_edge_refused"] += 1
            return None
        n = max(VENUE_MIN, math.floor(sc.entry_usdc / px * 100.0) / 100.0)
        cost = n * px
        why = None
        if cost > sc.entry_max_usdc + 1e-9:
            why = f"{n:g} sh at {px:.2f} > {sc.entry_max_usdc:g} USDC"
        elif b.cycle_cost + cost > sc.max_entry_cost_usdc + 1e-9:
            why = f"cycle entries {b.cycle_cost:.2f} + {cost:.2f} > {sc.max_entry_cost_usdc:g}"
        elif b.C + cost > sc.max_market_cost_usdc + 1e-9:
            why = f"market cost {b.C:.2f} + {cost:.2f} > {sc.max_market_cost_usdc:g}"
        elif self.open_cost() + cost > sc.max_total_cost_usdc + 1e-9:
            why = f"open cost {self.open_cost():.2f} + {cost:.2f} > {sc.max_total_cost_usdc:g}"
        elif balance is not None and cost > float(balance) - sum(o["price"] * o["size"] for o in self.resting.values()) + 1e-9:
            why = f"cash {float(balance):.2f} cannot fund {cost:.2f}"
        else:
            U, D = (b.U + n, b.D) if s.side == "UP" else (b.U, b.D + n)
            worst = min(U, D) - (b.C + cost)
            if worst < -sc.max_market_loss_usdc - 1e-9:
                why = f"worst case {worst:+.2f} < -{sc.max_market_loss_usdc:g}"
        if why:
            self.rr_stats["entry_limit_refused"] += 1
            log.debug("[RTR_ENTRY_REFUSED] %s %s: %s", b.slug, s.side, why)
            return None
        o = Order(token_id=tok, side=Side.BUY, price=px, size=n, tif=TimeInForce.GTC, expires_ts=m.close_ts,
                  expected_price=px, post_only=True,
                  tag=f"{s.side} rising_recovery entry bid {n:g} @ {px:.2f} (rise {100 * s.rise:+.1f}c)")
        log.info("[RTR_ENTRY_BID] %s %s %g @ %.2f | rise %+.1fc in %.0f ms, spread %.2f, mid %.3f | %s", b.slug, s.side, n,
                 px, 100 * s.rise, sc.lookback_ms, s.spread, q, b.line())
        return self._hold(o, b, s.side, tok, px, n, now, "entry")

    def favourite(self, tops: dict) -> tuple[str | None, float | None]:
        """(the token clearly priced to win, its mid) or (None, None) when neither mid reaches high_min_mid."""
        mids = {s: self._mid(tops[s][0]) for s in ("UP", "DOWN")}
        hi = max(("UP", "DOWN"), key=lambda s: -1.0 if mids[s] is None else mids[s])
        if mids[hi] is None or mids[hi] < self.scfg.high_min_mid - 1e-9:
            return None, None
        return hi, mids[hi]

    def state(self, b: MarketBook, tops: dict) -> tuple[str | None, str | None, bool]:
        """(the higher-priced token, the lower-priced one, positive state): positive when the P&L if the higher-priced
        token wins, or the P&L at the current mids, is above 0 (and the market holds something)."""
        mids = {s: self._mid(tops[s][0]) for s in ("UP", "DOWN")}
        if mids["UP"] is None or mids["DOWN"] is None:
            return None, None, False
        fav = "UP" if mids["UP"] >= mids["DOWN"] else "DOWN"
        dog = "DOWN" if fav == "UP" else "UP"
        if b.U <= 0 and b.D <= 0:
            return fav, dog, False
        mtm = b.U * mids["UP"] + b.D * mids["DOWN"] - b.C
        return fav, dog, (b.shares(fav) - b.C > 1e-9) or mtm > 1e-9

    def _hedge_order(self, b, m, tops, now, left, balance):
        """Positive state: the lower-priced token below hedge_max_price, sized to even out the two outcomes."""
        sc = self.scfg
        if not sc.hedge_enabled or left < sc.pair_min_left_s:
            return None
        fav, dog, positive = self.state(b, tops)
        if not positive:
            return None
        p_fav, p_dog = b.shares(fav) - b.C, b.shares(dog) - b.C
        if p_dog >= -1e-9 or p_fav <= sc.high_win_target_usdc + 1e-9:
            return None                                       # no loss to cover, or no room under the favourite's target
        tok, ob = tops[dog]
        if ob is None or ob.best_bid is None or ob.best_ask is None or self._resting_on(tok) is not None:
            return None
        bid, ask = ob.best_bid, ob.best_ask
        taker = sc.hedge_mode == "taker" or (sc.hedge_mode == "auto" and ask + taker_fee(ask) < sc.hedge_max_price - 1e-9)
        if taker:
            px = round(max(ask, min(ask + sc.max_slippage, sc.hedge_max_price - 0.01)), 2)
            unit = ask + taker_fee(ask)
        else:
            px = round(bid + (0.01 if ask - bid >= 0.02 - 1e-9 else 0.0), 2)
            if px >= ask - 1e-9:
                px = round(ask - 0.01, 2)
            if bid < sc.hedge_max_price - 1e-9:
                px = max(bid, min(px, round(sc.hedge_max_price - 0.01, 2)))   # improved, but kept under the hedge price
            unit = px
        if px >= sc.hedge_max_price - 1e-9 or unit <= 0.0:
            return None
        n = min(p_fav - p_dog, (p_fav - sc.high_win_target_usdc) / unit)
        n = math.floor(n * 100.0 + 1e-6) / 100.0
        if n < VENUE_MIN - 1e-9:
            return None
        q = self.fit(b, dog, n, unit)
        if q <= 0 or (balance is not None and q * unit > float(balance) + 1e-9):
            self.rr_stats["hedge_refused_limit"] += 1
            return None
        if taker:
            o = Order(token_id=tok, side=Side.BUY, price=px, size=q, tif=TimeInForce.IOC, expected_price=ask,
                      tag=f"{dog} rising_recovery hedge TAKE {q:g} <= {px:.2f}")
            kind = "hedge_take"
        else:
            o = Order(token_id=tok, side=Side.BUY, price=px, size=q, tif=TimeInForce.GTC, expires_ts=m.close_ts,
                      expected_price=px, post_only=True, tag=f"{dog} rising_recovery hedge bid {q:g} @ {px:.2f}")
            kind = "hedge"
        log.info("[RTR_HEDGE] %s positive (if %s wins %+.2f): %s at %.2f < %.2f -> %s %g | after: if %s wins %+.2f, if %s "
                 "wins %+.2f | %s", b.slug, fav, p_fav, dog, px, sc.hedge_max_price, "TAKE" if taker else "bid", q, fav,
                 p_fav - q * unit, dog, p_dog + q * (1.0 - unit), b.line())
        return self._hold(o, b, dog, tok, px, q, now, kind)

    def fix_need(self, b: MarketBook, hi: str) -> float:
        """How far the P&L if the favourite wins is under the target (0 = fine)."""
        return max(0.0, self.scfg.high_win_target_usdc - (b.shares(hi) - b.C))

    def _fix_order(self, b, m, tops, now, left, balance):
        """MOMENTUM MODE, the favourite fix: the market's P&L if the high-priced token wins must be positive."""
        sc = self.scfg
        if not sc.fix_high or (b.U <= 0 and b.D <= 0) or left < sc.pair_min_left_s:
            return None
        hi, mid = self.favourite(tops)
        if hi is None:
            return None
        short = self.fix_need(b, hi)
        if short <= 1e-9:
            return None
        tok, ob = tops[hi]
        if ob is None or ob.best_bid is None or ob.best_ask is None or self._resting_on(tok) is not None:
            return None
        bid, ask = ob.best_bid, ob.best_ask
        taker = sc.fix_mode == "taker"
        if taker:
            px = round(min(0.99, ask + sc.max_slippage), 2)
            unit = ask + taker_fee(ask)
        else:
            px = round(bid + (0.01 if ask - bid >= 0.02 - 1e-9 else 0.0), 2)
            if px >= ask - 1e-9:
                px = round(ask - 0.01, 2)
            unit = px
        if unit >= 1.0 - 1e-9:
            return None
        if unit > sc.fix_max_price + 1e-9:
            self.rr_stats["fix_refused_price"] += 1
            return None
        if sc.fix_max_per_market and len(b.fix_orders) >= int(sc.fix_max_per_market):
            self.rr_stats["fix_refused_count"] += 1
            return None
        n = max(VENUE_MIN, math.ceil(short / (1.0 - unit) * 100.0 - 1e-6) / 100.0)
        q = self.fit(b, hi, n, unit)
        if q <= 0 or (balance is not None and q * unit > float(balance) + 1e-9):
            self.rr_stats["fix_refused_limit"] += 1
            return None
        if q < n - 1e-9 and not sc.fix_partial:
            # cut short by the limits it cannot make the favourite's outcome positive and can leave the market losing both
            # ways (paper 18:13: UP 10 for 8.59, then DOWN 17.65 for 11.41 -> -10.00 / -2.35); on average it still helps
            self.rr_stats["fix_refused_partial"] += 1
            return None
        after_hi = b.shares(hi) + q - (b.C + q * unit)
        after_lo = b.shares("DOWN" if hi == "UP" else "UP") - (b.C + q * unit)
        if taker:
            o = Order(token_id=tok, side=Side.BUY, price=px, size=q, tif=TimeInForce.IOC, expected_price=ask,
                      tag=f"{hi} rising_recovery favourite fix TAKE {q:g} <= {px:.2f}")
            kind = "fix_take"
        else:
            o = Order(token_id=tok, side=Side.BUY, price=px, size=q, tif=TimeInForce.GTC, expires_ts=m.close_ts,
                      expected_price=px, post_only=True, tag=f"{hi} rising_recovery favourite fix bid {q:g} @ {px:.2f}")
            kind = "fix"
        log.info("[RTR_FIX] %s %s is the favourite (mid %.3f): if it wins %+.2f < %+.2f -> %s %g @ %.2f%s | after: if %s "
                 "wins %+.2f, otherwise %+.2f | %s", b.slug, hi, mid, b.shares(hi) - b.C, sc.high_win_target_usdc,
                 "TAKE" if taker else "bid", q, px, f" (cut from {n:g} by the limits)" if q < n - 1e-9 else "", hi, after_hi,
                 after_lo, b.line())
        return self._hold(o, b, hi, tok, px, q, now, kind)

    def _cover_order(self, b, m, tops, now, left, balance):
        """The cover cycle of the latest momentum fill: a cheap bid on the other token, or a take if it turns up."""
        sc = self.scfg
        if not sc.cover_enabled or b.lead_seq == 0 or left < sc.pair_min_left_s:
            return None
        side, excess, avg = b.excess()
        if side is None or side != b.lead_side or excess < VENUE_MIN - 1e-9:
            return None
        opp = "DOWN" if side == "UP" else "UP"
        tok, ob = tops[opp]
        if ob is None or ob.best_bid is None or ob.best_ask is None:
            return None
        s = self.signal(tok, opp, now)
        take_rise = sc.cover_take_min_rise or sc.min_rise
        rising = (s is not None and s.confirmed and s.rise >= take_rise - 1e-9 and s.age_ms <= sc.max_signal_age_ms
                  and s.ask >= sc.cover_take_min_price - 1e-9)
        if self._resting_on(tok) is not None:
            return None                                   # a resting cover bid: pair_cancels pulls it when rising
        bid, ask = ob.best_bid, ob.best_ask
        if rising:
            if b.take_seq == b.lead_seq or b.cover_filled_seq == b.lead_seq or ask > sc.cover_take_max_price + 1e-9:
                return None
            unit = ask + taker_fee(ask)
            n = max(VENUE_MIN, math.ceil(sc.cover_take_fraction * excess * 100.0 - 1e-6) / 100.0)
            q = self.fit(b, opp, n, unit)
            if q <= 0 or (balance is not None and q * unit > float(balance) + 1e-9):
                self.rr_stats["cover_take_refused"] += 1
                return None
            px = round(min(0.99, ask + sc.max_slippage), 2)
            o = Order(token_id=tok, side=Side.BUY, price=px, size=q, tif=TimeInForce.IOC, expected_price=ask,
                      tag=f"{opp} rising_recovery cover TAKE {q:g} <= {px:.2f} (the other token turned up)")
            b.take_seq = b.lead_seq
            log.info("[RTR_COVER_TAKE] %s %s rising (%+.1fc in %.0f ms) before the cover filled -> TAKE %g @ %.2f | after: if %s "
                     "wins %+.2f, if %s wins %+.2f | %s", b.slug, opp, 100 * s.rise, sc.lookback_ms, q, ask, side,
                     b.shares(side) - (b.C + q * unit), opp, b.shares(opp) + q - (b.C + q * unit), b.line())
            return self._hold(o, b, opp, tok, px, q, now, "cover_take")
        if b.cover_seq == b.lead_seq or now - b.lead_t < sc.cover_delay_s * 1000.0:
            return None
        cap = math.floor((1.0 - sc.cover_margin - avg) * 100.0 + 1e-9) / 100.0
        px = round(min(bid - sc.cover_discount, cap), 2)
        if px >= ask - 1e-9:
            px = round(ask - 0.01, 2)
        if px < 0.01:
            b.cover_seq = b.lead_seq                      # nothing cheap enough: this fill gets no cover bid
            self.rr_stats["cover_refused_cost"] += 1
            return None
        n = max(VENUE_MIN, math.ceil(sc.cover_fraction * excess * 100.0 - 1e-6) / 100.0)
        q = self.fit(b, opp, n, px)
        if q <= 0 or (balance is not None and q * px > float(balance) + 1e-9):
            self.rr_stats["cover_refused_limit"] += 1
            return None
        o = Order(token_id=tok, side=Side.BUY, price=px, size=q, tif=TimeInForce.GTC, expires_ts=m.close_ts,
                  expected_price=px, post_only=True, tag=f"{opp} rising_recovery cover bid {q:g} @ {px:.2f}")
        b.cover_seq = b.lead_seq
        log.info("[RTR_COVER_BID] %s %s %g @ %.2f (bid %.2f - %.2f, cap %.2f) %.0f s after the %s buy | if filled: if %s wins %+.2f, "
                 "if %s wins %+.2f | %s", b.slug, opp, q, px, bid, sc.cover_discount, cap, (now - b.lead_t) / 1000.0, side,
                 side, b.shares(side) - (b.C + q * px), opp, b.shares(opp) + q - (b.C + q * px), b.line())
        return self._hold(o, b, opp, tok, px, q, now, "cover_bid")

    def _momentum_order(self, b, m, tops, now, left, balance):
        """MOMENTUM MODE: buy the token the book says is rising, whichever it is, as often as it rises."""
        sc = self.scfg
        if not (sc.entry_min_left_s <= left <= sc.entry_max_left_s) or b.cycle_fills >= int(sc.max_market_fills):
            return None
        best = None
        if sc.entry_signal == "favourite":
            fav = self.state(b, tops)[0]
            tok, ob = tops.get(fav, (None, None)) if fav else (None, None)
            if (ob is None or ob.best_bid is None or ob.best_ask is None or self._resting_on(tok) is not None
                    or now < self.cooldown.get(tok, -1e18)):
                return None
            mids = {sd: self._mid(tops[sd][0]) for sd in ("UP", "DOWN")}
            if mids["UP"] is None or mids["DOWN"] is None or abs(mids["UP"] - mids["DOWN"]) < 1e-9:
                return None
            best = Signal(side=fav, token=tok, ask=ob.best_ask, bid=ob.best_bid, rise=0.0, age_ms=0.0, confirmed=True,
                          spread=ob.best_ask - ob.best_bid)
        for side, (tok, ob) in tops.items():
            if sc.entry_signal == "favourite":
                break
            if ob is None or self._resting_on(tok) is not None or now < self.cooldown.get(tok, -1e18):
                continue
            s = self.signal(tok, side, now)
            if s is None or not s.confirmed or s.rise < sc.min_rise - 1e-9 or s.age_ms > sc.max_signal_age_ms:
                continue
            if best is None or s.rise > best.rise:
                best = s
        if best is None:
            return None
        s = best
        if s.spread > sc.max_spread + 1e-9 or s.spread < 0.01 - 1e-9 or not (sc.min_ask <= s.ask <= sc.max_ask):
            self.rr_stats["buy_band_or_spread"] += 1
            return None
        q = (s.bid + s.ask) / 2.0
        taker = sc.buy_mode == "taker"
        if taker:
            px = round(min(0.99, s.ask + sc.max_slippage), 2)
            unit = s.ask + taker_fee(s.ask)
        else:
            px = round(s.bid + (0.01 if s.spread >= 0.02 - 1e-9 else 0.0), 2)
            unit = px
            if q - px - sc.entry_adverse_selection < sc.entry_min_edge - 1e-9:
                self.rr_stats["buy_edge_refused"] += 1
                return None
        n = max(VENUE_MIN, math.floor(sc.entry_usdc / unit * 100.0) / 100.0)
        cost = n * unit
        worst_after = self._worst_after(b, s.side, n, unit)
        raises = worst_after >= b.worst - 1e-9
        why = None
        if cost > sc.entry_max_usdc + 1e-9:
            why = f"{n:g} sh at {unit:.3f} > {sc.entry_max_usdc:g} USDC"
        elif b.C + cost > sc.max_market_cost_usdc + 1e-9:
            why = f"market spend {b.C:.2f} + {cost:.2f} > {sc.max_market_cost_usdc:g}"
        elif self.open_cost() + cost > sc.max_total_cost_usdc + 1e-9:
            why = f"open cost {self.open_cost():.2f} + {cost:.2f} > {sc.max_total_cost_usdc:g}"
        elif balance is not None and cost > float(balance) - sum(o["price"] * o["size"] for o in self.resting.values()) + 1e-9:
            why = f"cash {float(balance):.2f} cannot fund {cost:.2f}"
        elif worst_after < -sc.max_market_loss_usdc - 1e-9 and not raises:
            why = f"worst case {worst_after:+.2f} < -{sc.max_market_loss_usdc:g}"
        elif self._halted() and not raises:
            why = f"daily stop ({self._halted()}): only buys that raise the worst case"
        elif self.double_loss(b, s.side, n, unit):
            why = "it would leave the market losing on both outcomes"
            self.rr_stats["double_loss_refused"] += 1
        elif sc.positive_rule and self.state(b, tops)[2] and s.side == self.state(b, tops)[1]:
            why = (f"positive P&L: the lower-priced {s.side} only below {sc.hedge_max_price:.2f}, as a hedge")
            self.rr_stats["buy_underdog_refused"] += 1
        elif sc.fix_high:
            fav = self.favourite(tops)[0]
            if fav is not None and fav != s.side and b.shares(fav) - (b.C + cost) < sc.high_win_target_usdc - 1e-9:
                why = (f"buying the underdog {s.side} would leave the favourite {fav}'s outcome at "
                       f"{b.shares(fav) - (b.C + cost):+.2f} < {sc.high_win_target_usdc:+.2f}")
                self.rr_stats["buy_underdog_refused"] += 1
        if why:
            self.rr_stats["buy_limit_refused"] += 1
            log.debug("[RTR_BUY_REFUSED] %s %s: %s", b.slug, s.side, why)
            return None
        if taker:
            o = Order(token_id=s.token, side=Side.BUY, price=px, size=n, tif=TimeInForce.IOC, expected_price=s.ask,
                      tag=f"{s.side} rising_recovery momentum TAKE {n:g} <= {px:.2f} (rise {100 * s.rise:+.1f}c)")
            kind = "buy_take"
            self.cooldown[s.token] = now + sc.entry_cooldown_s * 1000.0    # one buy per move, not one per pass
        else:
            o = Order(token_id=s.token, side=Side.BUY, price=px, size=n, tif=TimeInForce.GTC, expires_ts=m.close_ts,
                      expected_price=px, post_only=True,
                      tag=f"{s.side} rising_recovery momentum bid {n:g} @ {px:.2f} (rise {100 * s.rise:+.1f}c)")
            kind = "entry"
        log.info("[RTR_BUY] %s %s %s %g @ %.2f | rise %+.1fc in %.0f ms, mid %.3f | after: if UP wins %+.2f, if DOWN wins "
                 "%+.2f | %s", b.slug, "TAKE" if taker else "bid", s.side, n, px, 100 * s.rise, sc.lookback_ms, q,
                 (b.U + (n if s.side == "UP" else 0.0)) - (b.C + cost), (b.D + (n if s.side == "DOWN" else 0.0)) - (b.C + cost),
                 b.line())
        return self._hold(o, b, s.side, s.token, px, n, now, kind)

    # ------------------------------------------------------------------ spot mode

    def _capture_spot(self, m, spot, spot_age_ms, book_age_ms, quote_ages, now) -> None:
        price = getattr(spot, "price", None)
        if price and spot_age_ms is not None and spot_age_ms <= self.scfg.max_spot_age_ms:
            h = self.spot_hist.setdefault(m.asset, collections.deque())
            if not h or h[-1][1] != price or now - h[-1][0] >= 100.0:
                h.append((now, float(price)))
            while len(h) > 1 and h[1][0] < now - 10_000.0:
                h.popleft()
        self.market_data[m.slug] = {"t": now, "spot": float(price) if price else None, "spot_age_ms": spot_age_ms,
                                    "book_age_ms": book_age_ms, "quote_ages": dict(quote_ages or {}), "strike": float(m.strike or 0.0)}

    def _spot_1s(self, asset: str, now: float) -> tuple[float, float] | None:
        """(price now, price a second ago) from the captured history, or None without a second of it."""
        h = self.spot_hist.get(asset)
        if not h or now - h[0][0] < 1000.0:
            return None
        then = None
        for t, p in h:
            if t <= now - 1000.0:
                then = p
            else:
                break
        return (h[-1][1], then) if then is not None else None

    def _spot_log(self, b, trigger, event, side=None, qty=None, price=None, fill_price=None, fill_qty=None, fee=None,
                  slippage=None, reason=None, now=None) -> None:
        d = b.last_spot or {}
        spot, strike = d.get("spot"), d.get("strike")
        rec = {"ts": now, "market": b.slug, "trigger": trigger, "event": event, "side": side, "qty": qty, "price": price,
               "btc": spot, "strike": strike,
               "strike_distance_bps": (round((spot - strike) / strike * 1e4, 2) if spot and strike else None),
               "fill_price": fill_price, "fill_qty": fill_qty, "fee": fee, "slippage": slippage,
               "up_shares": round(b.U, 2), "down_shares": round(b.D, 2), "cost": round(b.C, 4), "fees": round(b.fees, 4),
               "pnl_up": round(b.U - b.C, 4), "pnl_down": round(b.D - b.C, 4), "reason": reason}
        self.rr_stats[f"spot_{event}"] += 1
        log.info("[RTR_SPOT] %s", json.dumps(rec))

    def _spot_candidate(self, b, m, tops, side, shares, now, balance):
        """(order, unit cost, reason-if-refused) for a taker buy of ``shares`` of ``side`` on the current book: the
        size is cut to the ask depth within the limit and to the per-outcome / per-market caps."""
        sc = self.scfg
        tok, ob = tops[side]
        if ob is None or ob.best_ask is None or ob.best_bid is None:
            return None, 0.0, "no book"
        bid, ask = ob.best_bid, ob.best_ask
        if not (0.0 < bid < ask < 1.0):
            return None, 0.0, f"invalid book bid {bid} ask {ask}"
        if self._resting_on(tok) is not None:
            return None, 0.0, "an order on this token is still ending"
        limit = round(min(0.99, ask + sc.max_slippage), 2)
        depth = sum(lv.size for lv in (getattr(ob, "asks", None) or []) if lv.price <= limit + 1e-9)
        meta = self.meta_by_slug.get(b.slug)
        fee_ps = meta.fee.taker_fee_per_share(ask) if meta is not None and getattr(meta, "fee", None) else taker_fee(ask)
        unit = ask + fee_ps + sc.spot_slippage_allowance
        n = min(float(shares), math.floor(depth * 100.0) / 100.0, sc.spot_max_shares - b.shares(side),
                math.floor((sc.spot_max_cost_usdc - b.C) / unit * 100.0) / 100.0)
        if balance is not None:
            n = min(n, math.floor(float(balance) / unit * 100.0) / 100.0)
        if n < VENUE_MIN - 1e-9:
            why = ("ask depth" if depth < VENUE_MIN else f"{sc.spot_max_shares:g} shares per outcome" if sc.spot_max_shares - b.shares(side) < VENUE_MIN
                   else f"{sc.spot_max_cost_usdc:g} USDC market cost" if (sc.spot_max_cost_usdc - b.C) / unit < VENUE_MIN else "cash")
            return None, unit, f"below the 5-share minimum: {why}"
        o = Order(token_id=tok, side=Side.BUY, price=limit, size=round(n, 2), tif=TimeInForce.IOC, expected_price=ask,
                  tag=f"{side} rising_recovery spot {n:g} <= {limit:.2f}")
        return o, ask + fee_ps, None

    def _spot_pass(self, b, m, tops, now, left, balance):
        """One pass of the spot rules: the events of this pass become wants; at most one order goes out."""
        sc = self.scfg
        d = self.market_data.get(b.slug)
        if d is None or d.get("spot") is None or d.get("strike", 0.0) <= 0.0:
            return None                                   # no spot / strike yet: nothing to decide
        b.last_spot = d
        spot, strike = d["spot"], d["strike"]
        hist = self._spot_1s(m.asset, now)
        opened_s = (now - m.open_ts) / 1000.0
        blocked = None
        if opened_s < sc.spot_open_delay_s:
            blocked = f"{sc.spot_open_delay_s:g} s after the open not yet reached"
        elif left < sc.pair_min_left_s:
            blocked = "too close to the close"
        elif self.guard_paused:
            blocked = "guard paused"
        elif d.get("spot_age_ms") is not None and d["spot_age_ms"] > sc.max_spot_age_ms:
            blocked = f"stale spot ({d['spot_age_ms']:.0f} ms)"
        elif any(a is not None and a > sc.max_quote_age_ms for a in d.get("quote_ages", {}).values()) or \
                (d.get("book_age_ms") is not None and d["book_age_ms"] > sc.max_quote_age_ms):
            blocked = "stale book"
        elif now - b.last_order_t < sc.spot_min_interval_s * 1000.0:
            blocked = "min order interval"
        # --- the events of this pass (edge-triggered; the state advances whether or not an order can go out)
        events: list[tuple[str, str | None, float, str]] = []     # (trigger id, side, shares, kind)
        if hist is not None:
            p_now, p_then = hist
            move_bps = (p_now / p_then - 1.0) * 1e4 if p_then else 0.0
            cand = 1 if move_bps >= sc.spot_min_move_bps else -1 if move_bps <= -sc.spot_min_move_bps else 0
            if cand and cand == b.cand_dir:
                b.cand_passes += 1
            elif cand:
                b.cand_dir, b.cand_passes = cand, 1
            if cand and b.cand_passes >= int(sc.spot_confirm_passes) and cand != b.direction:
                if b.direction != 0 and sc.spot_rev_enabled:
                    b.rev_n += 1
                    events.append((f"rev:{b.rev_n}", "UP" if cand > 0 else "DOWN", sc.spot_rev_shares, "spot_rev"))
                b.direction = cand
        if sc.spot_init_enabled and "init" not in b.fired and opened_s >= sc.spot_open_delay_s:
            side = self._init_side(tops, hist, now)
            if side is not None:
                events.append(("init", side, sc.spot_init_shares, "spot_init"))
        if sc.spot_cover_enabled and b.init_px is not None and "cover0" not in b.fired:
            opp = "DOWN" if b.init_side == "UP" else "UP"
            n = max(VENUE_MIN, math.ceil(sc.spot_cover_fraction * b.init_shares_filled * 100.0) / 100.0)
            if sc.spot_cover_mode == "rise":
                t1 = self._price_at(tops[opp][0], now - 1000.0, 1), self._price_at(tops[opp][0], now - 1000.0, 2)
                mid = self._mid(tops[opp][0])
                rise = (mid - (t1[0] + t1[1]) / 2.0) if mid is not None and t1[0] is not None and t1[1] is not None else 0.0
                if rise >= sc.spot_cover_min_rise_1s - 1e-9:
                    events.append(("cover0", opp, n, "spot_cover_take"))
            else:
                events.append(("cover0", opp, n, "spot_cover"))
        above = spot > strike if spot != strike else b.above
        if sc.spot_cross_enabled and b.above is not None and above is not None and above != b.above:
            b.cross_n += 1
            events.append((f"cross:{b.cross_n}", "UP" if above else "DOWN", sc.spot_init_shares, "spot_cross"))
        b.above = above
        mids = {s_: self._mid(tops[s_][0]) for s_ in ("UP", "DOWN")}
        for s_, mid in mids.items():
            if mid is None:
                continue
            was = b.leader_was_above.get(s_, mid > sc.spot_leader_mid)
            if sc.spot_leader_enabled and mid > sc.spot_leader_mid and not was:
                b.leader_n += 1
                events.append((f"leader:{s_}:{b.leader_n}", s_, sc.spot_init_shares, "spot_leader"))
            b.leader_was_above[s_] = mid > sc.spot_leader_mid
        near = abs(spot - strike) / strike * 100.0 <= sc.spot_near_strike_pct
        if sc.spot_near_enabled and near and b.in_near_zone is False:
            b.near_n += 1
            events.append((f"near:{b.near_n}", None, sc.spot_init_shares, "spot_near"))
        b.in_near_zone = near
        if sc.spot_climb_enabled and mids["UP"] is not None and mids["DOWN"] is not None and mids["UP"] != mids["DOWN"]:
            lead = "UP" if mids["UP"] > mids["DOWN"] else "DOWN"
            ltok, lob = tops[lead]
            then = self._price_at(ltok, now - sc.spot_climb_lookback_s * 1000.0, 1), self._price_at(ltok, now - sc.spot_climb_lookback_s * 1000.0, 2)
            mid_then = (then[0] + then[1]) / 2.0 if then[0] is not None and then[1] is not None else None
            rising = mid_then is not None and mids[lead] > mid_then + 1e-9
            ask_ok = lob is not None and lob.best_ask is not None and lob.best_ask < sc.spot_climb_max_ask - 1e-9
            climb_pending = any(w["kind"] == "spot_climb" for w in b.wants.values())
            if sc.spot_climb_reset_drop > 0 and b.climb_side and mids.get(b.climb_side) is not None:
                cm = mids[b.climb_side]
                b.climb_peak = cm if b.climb_peak is None else max(b.climb_peak, cm)
                if cm <= b.climb_peak - sc.spot_climb_reset_drop + 1e-9:
                    b.climb_dipped = True                 # down from the peak: the next jump restarts the climb
            room = sc.spot_climb_max_shares - b.climb_bought - sum(w["shares"] - w["filled"] for w in b.wants.values() if w["kind"] == "spot_climb")
            if sc.spot_climb_min_rise_1s > 0:
                # the jump trigger: a mid up >= the threshold over 1 s, once per jump (the leader's, or with
                # spot_climb_any_token whichever token jumped more)
                rises = {}
                for s_ in ("UP", "DOWN"):
                    t1 = self._price_at(tops[s_][0], now - 1000.0, 1), self._price_at(tops[s_][0], now - 1000.0, 2)
                    rises[s_] = (mids[s_] - (t1[0] + t1[1]) / 2.0) if t1[0] is not None and t1[1] is not None else 0.0
                if sc.spot_climb_any_token:
                    lead = max(("UP", "DOWN"), key=lambda s_: rises[s_])
                    ltok, lob = tops[lead]
                    ask_ok = lob is not None and lob.best_ask is not None and lob.best_ask < sc.spot_climb_max_ask - 1e-9
                rise_1s = rises[lead]
                jumped = rise_1s >= sc.spot_climb_min_rise_1s - 1e-9
                due = jumped and b.climb_armed
                b.climb_armed = not jumped
                rising = rising or jumped
            else:
                due = now - b.climb_last_t >= sc.spot_climb_interval_s * 1000.0
            if sc.spot_climb_reset_drop > 0 and b.climb_dipped and due and rising and ask_ok and not climb_pending:
                self._spot_log(b, f"climb-reset:{b.climb_n + 1}", "reset", side=lead,
                               reason=f"{b.climb_side} fell {sc.spot_climb_reset_drop:.2f} from its peak {b.climb_peak:.3f} and jumps again: "
                                      f"the climb restarts ({b.climb_bought:g} bought so far)", now=now)
                b.climb_bought, b.climb_dipped, b.climb_peak = 0.0, False, None
                room = sc.spot_climb_max_shares
            if (rising and ask_ok and room >= VENUE_MIN / 2.0 and not climb_pending and due
                    and opened_s >= sc.spot_open_delay_s):
                b.climb_n += 1
                b.climb_last_t = now
                events.append((f"climb:{b.climb_n}", lead, min(sc.spot_climb_shares, room), "spot_climb"))
            elif (b.climb_bought >= VENUE_MIN - 1e-9 and not climb_pending and b.pair_episode_done < b.climb_episode
                    and (room < VENUE_MIN / 2.0 or not rising or not ask_ok)):
                # the climb is over for now: the pair rule, once per climb episode; a still-pending earlier pair want is
                # superseded (15:5x: seven pending pair wants piled up in one market and each would have bought)
                b.pair_episode_done = b.climb_episode
                for old in [k for k, w in b.wants.items() if w["kind"] == "spot_pair"]:
                    b.wants.pop(old, None)
                    self._spot_log(b, old, "drop", reason=f"superseded by pair:{b.climb_episode}", now=now)
                held = b.climb_side or lead                   # 17:00: after a DOWN jump the "leader" by rise was UP again
                events.append((f"pair:{b.climb_episode}", "DOWN" if held == "UP" else "UP", 0.0, "spot_pair"))
        if (sc.spot_lowrise_enabled and mids["UP"] is not None and mids["DOWN"] is not None and mids["UP"] != mids["DOWN"]
                and opened_s >= sc.spot_open_delay_s):
            low_side = "UP" if mids["UP"] < mids["DOWN"] else "DOWN"
            sig = self.signal(tops[low_side][0], low_side, now)
            rising = sig is not None and sig.confirmed and sig.rise >= sc.min_rise - 1e-9 and sig.age_ms <= sc.max_signal_age_ms
            pending = any(w["kind"] == "spot_lowrise" for w in b.wants.values())
            if rising and b.lowrise_armed and not pending and b.lowrise_n < int(sc.spot_lowrise_max_per_market):
                b.lowrise_n += 1
                events.append((f"lowrise:{b.lowrise_n}", low_side, sc.spot_lowrise_shares, "spot_lowrise"))
            b.lowrise_armed = not rising
        if sc.spot_guard_enabled and (b.U > 0 or b.D > 0) and mids["UP"] is not None and mids["DOWN"] is not None:
            for s_ in ("UP", "DOWN"):
                t1 = self._price_at(tops[s_][0], now - 1000.0, 1), self._price_at(tops[s_][0], now - 1000.0, 2)
                rise = (mids[s_] - (t1[0] + t1[1]) / 2.0) if t1[0] is not None and t1[1] is not None else 0.0
                rising = rise >= sc.spot_guard_min_rise_1s - 1e-9
                pending = any(w["kind"] == "spot_guard" and w["side"] == s_ for w in b.wants.values())
                if rising and b.guard_armed[s_] and not pending and b.shares(s_) - b.C < sc.spot_guard_target_usdc - 1e-9:
                    b.guard_n += 1
                    events.append((f"guard:{b.guard_n}", s_, 0.0, "spot_guard"))
                b.guard_armed[s_] = not rising
        for trig, side, shares, kind in events:
            if trig in b.fired:
                self._spot_log(b, trig, "duplicate", side=side, now=now)
                continue
            b.fired.add(trig)
            b.wants[trig] = {"side": side, "shares": shares, "filled": 0.0, "kind": kind, "t": now, "logged_t": -1e18}
            self._spot_log(b, trig, "want", side=side, qty=shares, now=now)
        if blocked:
            if b.wants and blocked != "min order interval" and now - b.blocked_log_t >= sc.spot_retry_log_s * 1000.0:
                b.blocked_log_t = now
                self._spot_log(b, next(iter(b.wants)), "reject", reason=blocked + " (kept)", now=now)
            return None
        # --- service the wants, oldest first: the first one that produces an order ends the pass
        for trig, w in list(b.wants.items()):
            if any(self.orders.get(oid, {}).get("trigger") == trig for oid in self.resting):
                continue                                  # an order of this want is still in flight (19:45: a re-sided
                                                          # init went out on the other token before the first was answered)
            remaining = w["shares"] - w["filled"]
            if remaining < VENUE_MIN / 2.0 and (w["kind"] not in ("spot_pair", "spot_guard") or w["shares"] > 0):
                b.wants.pop(trig, None)                   # filled (a pair want is sized at send time: shares 0 = not yet)
                continue
            o = self._spot_try(b, m, tops, mids, trig, w, max(remaining, VENUE_MIN), now, balance)
            if o is not None:
                return o
        return None

    def _init_side(self, tops, hist, now) -> str | None:
        """The token rising NOW: the one whose mid rose more over the last second ("token" signal; BTC's 1-s direction
        when neither rose), or None when nothing is rising. Re-evaluated at every (re)send of the initial buy
        (the user 18:4x: a re-order after a failed one must not keep the stale side)."""
        sc = self.scfg
        if sc.spot_init_signal == "token":
            rises = {}
            for s_ in ("UP", "DOWN"):
                t1 = self._price_at(tops[s_][0], now - 1000.0, 1), self._price_at(tops[s_][0], now - 1000.0, 2)
                mid = self._mid(tops[s_][0])
                rises[s_] = (mid - (t1[0] + t1[1]) / 2.0) if mid is not None and t1[0] is not None and t1[1] is not None else 0.0
            best_side = max(("UP", "DOWN"), key=lambda s_: rises[s_])
            if rises[best_side] > 1e-9:
                return best_side
        if hist is not None and hist[0] != hist[1]:
            return "UP" if hist[0] > hist[1] else "DOWN"
        return None

    def _spot_try(self, b, m, tops, mids, trig, w, shares, now, balance):
        """One attempt at a pending want: the kind's own condition, the PnL rule, the candidate, the halts."""
        sc = self.scfg
        kind = w["kind"]
        mtm = None
        if mids["UP"] is not None and mids["DOWN"] is not None:
            mtm = b.U * mids["UP"] + b.D * mids["DOWN"] - b.C
            low = "UP" if mids["UP"] < mids["DOWN"] else "DOWN"
        else:
            low = None

        def allowed(side):
            if kind in ("spot_pair", "spot_cover", "spot_cover_take", "spot_guard", "spot_lowrise"):
                return None                                # the pair's own condition is stronger; the others are explicit
            if sc.spot_pnl_rule and mtm is not None and mtm < -1e-9 and side == low:
                return f"current PnL {mtm:+.2f} < 0: the lower-priced {side} is not bought"
            return None

        if kind == "spot_near":
            best, notes = None, []
            for side in ("UP", "DOWN"):
                why = allowed(side)
                if why:
                    notes.append(why)
                    continue
                o, unit, why = self._spot_candidate(b, m, tops, side, shares, now, balance)
                if o is None:
                    notes.append(f"{side}: {why}")
                    continue
                gain = self._worst_after(b, side, o.size, unit) - b.worst
                notes.append(f"{side} {o.size:g} @ {o.price:.2f}: worst {b.worst:+.2f} -> {b.worst + gain:+.2f}")
                if gain > 1e-9 and (best is None or gain > best[3]):
                    best = (o, side, unit, gain)
            if best is None:
                transient = any(x in n for n in notes for x in ("still ending", "no book", "ask depth"))
                self._spot_drop_or_wait(b, trig, w, "no candidate improves the worst case: " + "; ".join(notes), transient, now)
                return None
            o, side, unit, _ = best
        else:
            side = w["side"]
            if kind == "spot_init" and w["filled"] <= 1e-9:
                # the initial buy goes to whichever token is rising at THIS send (a failed order must not keep a stale side)
                side = self._init_side(tops, self._spot_1s(m.asset, now), now)
                if side is None:
                    self._spot_drop_or_wait(b, trig, w, "neither token is rising now", True, now)
                    return None
                if side != w["side"]:
                    self._spot_log(b, trig, "reside", side=side, reason=f"the rising token is now {side}, was {w['side']}", now=now)
                    w["side"] = side
            why = allowed(side)
            if why:
                self._spot_drop_or_wait(b, trig, w, why, False, now)
                return None
            if kind == "spot_cover":
                return self._spot_cover_order(b, m, tops, trig, w, shares, now, balance)
            if kind == "spot_guard":
                # the amount that lifts the market's PnL if this (rising) token wins to the target
                tok, ob = tops[side]
                if ob is None or ob.best_ask is None:
                    self._spot_drop_or_wait(b, trig, w, "no book", True, now)
                    return None
                meta = self.meta_by_slug.get(b.slug)
                u = ob.best_ask + (meta.fee.taker_fee_per_share(ob.best_ask) if meta is not None and getattr(meta, "fee", None) else taker_fee(ob.best_ask))
                short = sc.spot_guard_target_usdc - (b.shares(side) - b.C)
                if short <= 1e-9 or u >= 1.0 - 1e-9:
                    self._spot_drop_or_wait(b, trig, w, f"PnL if {side} wins is {b.shares(side) - b.C:+.2f}: nothing to guard", False, now)
                    return None
                n = min(sc.spot_guard_max_shares, max(VENUE_MIN, math.ceil(short / (1.0 - u) * 100.0) / 100.0))
                shares = n
                w["shares"] = w["filled"] + n
            if kind == "spot_pair":
                # the amount that makes the cheap outcome positive while the leader's outcome stays positive
                lead = "DOWN" if side == "UP" else "UP"
                tok, ob = tops[side]
                if ob is None or ob.best_ask is None:
                    self._spot_drop_or_wait(b, trig, w, "no book", True, now)
                    return None
                meta = self.meta_by_slug.get(b.slug)
                u = ob.best_ask + (meta.fee.taker_fee_per_share(ob.best_ask) if meta is not None and getattr(meta, "fee", None) else taker_fee(ob.best_ask))
                need = (b.C - b.shares(side) + sc.spot_pair_target_usdc) / (1.0 - u)
                n = max(VENUE_MIN, math.ceil(max(need, 0.0) * sc.spot_pair_fraction * 100.0) / 100.0)
                lead_after = b.shares(lead) - b.C - n * u
                if lead_after < sc.spot_pair_target_usdc - 1e-9:
                    # no pair profit at this price: kept, the chance may come as the leader rises (the cheap token falls)
                    self._spot_drop_or_wait(b, trig, w, f"no pair profit yet: {n:g} {side} at {u:.3f} would leave the leader's "
                                            f"outcome {lead_after:+.2f} < +{sc.spot_pair_target_usdc:g}", True, now)
                    return None
                shares = n
                w["shares"] = w["filled"] + n
            if sc.spot_low_ratio_max > 0 and kind in ("spot_pair", "spot_cover", "spot_cover_take", "spot_guard") and side == low:
                hi_side = "DOWN" if side == "UP" else "UP"
                room_ratio = sc.spot_low_ratio_max * b.shares(hi_side) - b.shares(side)
                if room_ratio < VENUE_MIN - 1e-9:
                    self._spot_drop_or_wait(b, trig, w, f"the lower-priced {side} is at {b.shares(side):g} vs {b.shares(hi_side):g} "
                                            f"{hi_side}: the {sc.spot_low_ratio_max:g} ratio cap leaves no room", kind == "spot_guard" or kind == "spot_pair", now)
                    return None
                if shares > room_ratio:
                    shares = math.floor(room_ratio * 100.0) / 100.0
                    w["shares"] = w["filled"] + shares
            o, unit, why = self._spot_candidate(b, m, tops, side, shares, now, balance)
            if o is None:
                transient = any(x in (why or "") for x in ("still ending", "no book", "ask depth", "invalid book", "cash"))
                self._spot_drop_or_wait(b, trig, w, why, transient, now)
                return None
            if kind == "spot_leader":
                pu = (b.U + (o.size if side == "UP" else 0.0)) - (b.C + o.size * unit)
                pd = (b.D + (o.size if side == "DOWN" else 0.0)) - (b.C + o.size * unit)
                if min(pu, pd) < sc.spot_leader_target_usdc - 1e-9:
                    self._spot_drop_or_wait(b, trig, w, f"after the buy PnL_Up {pu:+.2f} / PnL_Down {pd:+.2f} < "
                                            f"+{sc.spot_leader_target_usdc:g} both", False, now)
                    return None
        if self._halted() and self._worst_after(b, side, o.size, unit) < b.worst - 1e-9:
            self._spot_drop_or_wait(b, trig, w, f"risk halt: {self._halted()}", True, now)
            return None
        return self._spot_send(b, o, side, trig, kind, now)

    def _spot_cover_order(self, b, m, tops, trig, w, shares, now, balance):
        """The resting cover bid: the other token, at spot_cover_pair_cost - the initial fill price, until filled or
        the close; taken at once if its ask is already at or under that price."""
        sc = self.scfg
        side = w["side"]
        tok, ob = tops[side]
        px = round(sc.spot_cover_pair_cost - b.init_px, 2)
        if px < 0.01:
            self._spot_drop_or_wait(b, trig, w, f"cover price {px:.2f} under 0.01 (initial fill {b.init_px:.3f})", False, now)
            return None
        if self._resting_on(tok) is not None:
            return None                                   # the bid is resting (or an order on the token is ending)
        if ob is None or ob.best_ask is None:
            self._spot_drop_or_wait(b, trig, w, "no book", True, now)
            return None
        n = min(float(shares), sc.spot_max_shares - b.shares(side), math.floor((sc.spot_max_cost_usdc - b.C) / px * 100.0) / 100.0)
        if balance is not None:
            n = min(n, math.floor(float(balance) / px * 100.0) / 100.0)
        if n < VENUE_MIN - 1e-9:
            self._spot_drop_or_wait(b, trig, w, "the cover does not fit the per-outcome / market-cost caps", False, now)
            return None
        n = round(n, 2)
        if ob.best_ask <= px + 1e-9:
            # already cheaper than the wanted price: taken now at the ask
            limit = round(min(0.99, ob.best_ask + sc.max_slippage), 2)
            o = Order(token_id=tok, side=Side.BUY, price=limit, size=n, tif=TimeInForce.IOC, expected_price=ob.best_ask,
                      tag=f"{side} rising_recovery cover TAKE {n:g} <= {limit:.2f} (wanted {px:.2f})")
            return self._spot_send(b, o, side, trig, kind="spot_cover", now=now)
        o = Order(token_id=tok, side=Side.BUY, price=px, size=n, tif=TimeInForce.GTC, expires_ts=m.close_ts, expected_price=px,
                  post_only=True, tag=f"{side} rising_recovery cover bid {n:g} @ {px:.2f} (= {sc.spot_cover_pair_cost:.2f} - {b.init_px:.3f})")
        return self._spot_send(b, o, side, trig, kind="spot_cover", now=now)

    def _spot_drop_or_wait(self, b, trig, w, why, transient, now):
        """A refused attempt: a transient reason keeps the want (orders again until it fills), logged at most every
        spot_retry_log_s; anything else drops it."""
        if transient:
            if now - w["logged_t"] >= self.scfg.spot_retry_log_s * 1000.0:
                w["logged_t"] = now
                self._spot_log(b, trig, "reject", side=w["side"], qty=w["shares"] - w["filled"], reason=why + " (kept)", now=now)
            return
        b.wants.pop(trig, None)
        self._spot_log(b, trig, "drop", side=w["side"], qty=w["shares"] - w["filled"], reason=why, now=now)

    def _spot_send(self, b, o, side, trig, kind, now):
        b.last_order_t = now
        self._spot_log(b, trig, "order", side=side, qty=o.size, price=o.price, now=now)
        self.cooldown[o.token_id] = now + self.scfg.entry_cooldown_s * 1000.0
        res = self._hold(o, b, side, o.token_id, o.price, o.size, now, kind)
        self.orders[o.order_id]["trigger"] = trig
        return res

    # ------------------------------------------------------------------ opt mode

    def _spot_price_at(self, asset: str, when: float) -> float | None:
        h = self.spot_hist.get(asset)
        if not h or h[0][0] > when:
            return None
        val = None
        for t, p in h:
            if t <= when:
                val = p
            else:
                break
        return val

    def _opt_liquidation(self, b, tops) -> dict:
        """What selling everything now would realise: each side walked down the executable bid depth (the levels
        below the best bid, as far as the book shows), the venue's taker fee at each level; unsold shares count 0."""
        out = {"proceeds": 0.0, "unsold_up": 0.0, "unsold_down": 0.0}
        meta = self.meta_by_slug.get(b.slug)
        for side, held in (("UP", b.U), ("DOWN", b.D)):
            tok, ob = tops[side]
            left = held
            for lv in sorted(getattr(ob, "bids", None) or [], key=lambda l: -l.price) if ob is not None else []:
                if left <= 1e-9:
                    break
                take = min(left, float(lv.size))
                fee = meta.fee.taker_fee_per_share(lv.price) if meta is not None and getattr(meta, "fee", None) else taker_fee(lv.price)
                out["proceeds"] += take * (lv.price - fee)
                left -= take
            out["unsold_" + side.lower()] = round(left, 2)
        out["liquidation_pnl"] = round(out["proceeds"] - b.C, 4)
        out["proceeds"] = round(out["proceeds"], 4)
        return out

    def _opt_outcomes(self, b, tops) -> dict:
        """Both-outcome profitability vs the predicted winner's (the higher-mid token): reported apart, never
        as a guarantee."""
        mids = {}
        for s_ in ("UP", "DOWN"):
            tok, ob = tops[s_]
            mids[s_] = ((ob.best_bid + ob.best_ask) / 2.0 if ob is not None and ob.best_bid is not None and ob.best_ask is not None
                        else self._mid(tok))
        pu, pd = b.U - b.C, b.D - b.C
        pred = None
        if mids["UP"] is not None and mids["DOWN"] is not None and mids["UP"] != mids["DOWN"]:
            pred = "UP" if mids["UP"] > mids["DOWN"] else "DOWN"
        return {"both_positive": bool(pu > 0 and pd > 0), "predicted_winner": pred,
                "predicted_winner_pnl": (round(pu if pred == "UP" else pd, 4) if pred else None),
                "predicted_winner_positive": (bool((pu if pred == "UP" else pd) > 0) if pred else None)}

    def _opt_log(self, b, event, decision, now, **extra) -> None:
        d = b.last_spot or {}
        rec = {"ts": now, "market": b.slug, "event": event, "decision": decision, "btc": d.get("spot"),
               "up_shares": round(b.U, 2), "down_shares": round(b.D, 2), "cost": round(b.C, 4), "fees": round(b.fees, 4),
               "avg_up": (round(b.CU / b.U, 4) if b.U > 0 else None), "avg_down": (round(b.CD / b.D, 4) if b.D > 0 else None),
               "pnl_up": round(b.U - b.C, 4), "pnl_down": round(b.D - b.C, 4),
               "worst": round(min(b.U, b.D) - b.C, 4), "best": round(max(b.U, b.D) - b.C, 4)}
        tops = getattr(b, "_tops", None)
        if tops:
            rec.update(self._opt_outcomes(b, tops))
            rec["liquidation"] = self._opt_liquidation(b, tops)
        rec.update(extra)
        self.rr_stats[f"opt_{decision}"] += 1
        log.info("[RTR_OPT] %s", json.dumps(rec))

    def _opt_simulate(self, b, m, tops, side, qty):
        """Simulate a taker buy of ``qty`` of ``side`` against the ask depth: (filled qty, vwap, effective price per
        share incl. fee + slippage allowance, reason-if-infeasible)."""
        sc = self.scfg
        tok, ob = tops[side]
        if ob is None or ob.best_ask is None or ob.best_bid is None:
            return 0.0, 0.0, 0.0, "no book"
        if not (0.0 < ob.best_bid < ob.best_ask < 1.0):
            return 0.0, 0.0, 0.0, "invalid book"
        limit = round(min(0.99, ob.best_ask + sc.max_slippage), 2)
        left, cost = float(qty), 0.0
        for lv in sorted(getattr(ob, "asks", None) or [], key=lambda l: l.price):
            if lv.price > limit + 1e-9 or left <= 1e-9:
                break
            take = min(left, float(lv.size))
            cost += take * lv.price
            left -= take
        filled = math.floor((qty - left) * 100.0) / 100.0
        if filled < VENUE_MIN - 1e-9:
            return 0.0, 0.0, 0.0, "ask depth under the 5-share minimum"
        vwap = cost / (qty - left)
        meta = self.meta_by_slug.get(b.slug)
        fee_ps = meta.fee.taker_fee_per_share(vwap) if meta is not None and getattr(meta, "fee", None) else taker_fee(vwap)
        eff = vwap + fee_ps + sc.spot_slippage_allowance
        if not (sc.opt_price_min - 1e-9 <= eff <= sc.opt_price_max + 1e-9):
            return 0.0, vwap, eff, f"effective price {eff:.3f} outside {sc.opt_price_min:.2f}..{sc.opt_price_max:.2f}"
        if b.shares(side) + filled > sc.spot_max_shares + 1e-9:
            return 0.0, vwap, eff, f"{sc.spot_max_shares:g} shares per outcome"
        if b.C + filled * eff > sc.spot_max_cost_usdc + 1e-9:
            return 0.0, vwap, eff, f"{sc.spot_max_cost_usdc:g} USDC market cost"
        return filled, vwap, eff, None

    def _opt_pass(self, b, m, tops, now, left, balance):
        sc = self.scfg
        d = self.market_data.get(b.slug)
        if d is None or d.get("spot") is None:
            return None
        b.last_spot = d
        b._tops = tops
        opened_s = (now - m.open_ts) / 1000.0
        mids = {s_: self._mid(tops[s_][0]) for s_ in ("UP", "DOWN")}
        blocked = None
        if opened_s < sc.opt_open_window_s:
            blocked = "opening window"
        elif left < sc.pair_min_left_s:
            blocked = "too close to the close"
        elif self.guard_paused:
            blocked = "guard paused"
        elif d.get("spot_age_ms") is not None and d["spot_age_ms"] > sc.max_spot_age_ms:
            blocked = "stale spot"
        elif any(a is not None and a > sc.max_quote_age_ms for a in d.get("quote_ages", {}).values()) or \
                (d.get("book_age_ms") is not None and d["book_age_ms"] > sc.max_quote_age_ms):
            blocked = "stale book"
        elif now - b.last_order_t < sc.spot_min_interval_s * 1000.0:
            blocked = "min order interval"
        # --- events (state advances whether or not an order can go out)
        events = []
        p_now, p_then = d["spot"], self._spot_price_at(m.asset, now - 2000.0)
        r2 = (p_now / p_then - 1.0) * 1e4 if p_then else None
        init_side = None
        if "init" not in b.fired and r2 is not None and opened_s >= sc.opt_open_window_s and opened_s <= sc.opt_init_max_s:
            if r2 > sc.opt_init_bps:
                init_side = "UP"
            elif r2 < -sc.opt_init_bps:
                init_side = "DOWN"
            if init_side:
                events.append("init")
        if r2 is not None:
            cand = 1 if r2 > sc.opt_init_bps else -1 if r2 < -sc.opt_init_bps else 0
            if cand and cand != b.opt_dir:
                if b.opt_dir != 0:
                    events.append("reversal")
                b.opt_dir = cand
        if now - b.opt_last_eval_t >= sc.opt_eval_interval_s * 1000.0:
            events.append("timer")
        for s_ in ("UP", "DOWN"):
            sig = self.signal(tops[s_][0], s_, now)
            rising = sig is not None and sig.confirmed and sig.rise >= sc.min_rise - 1e-9 and sig.age_ms <= sc.max_signal_age_ms
            if rising and b.opt_rise_armed[s_]:
                events.append(f"rise:{s_}")
            b.opt_rise_armed[s_] = not rising
        if mids["UP"] is not None and mids["DOWN"] is not None and mids["UP"] != mids["DOWN"]:
            low_side = "UP" if mids["UP"] < mids["DOWN"] else "DOWN"
            lm = mids[low_side]
            t3 = self._price_at(tops[low_side][0], now - 3000.0, 1), self._price_at(tops[low_side][0], now - 3000.0, 2)
            m3 = (t3[0] + t3[1]) / 2.0 if t3[0] is not None and t3[1] is not None else None
            sig = self.signal(tops[low_side][0], low_side, now)
            trending = m3 is not None and lm > m3 + 1e-9 and sig is not None and sig.confirmed
            if not b.opt_in_mom and sc.opt_mom_low - 1e-9 <= lm <= sc.opt_mom_high + 1e-9 and trending:
                b.opt_in_mom, b.opt_mom_peak = True, lm
                self._opt_log(b, "momentum-start", "monitor", now, side=low_side, mid=round(lm, 4))
            elif b.opt_in_mom:
                b.opt_mom_peak = max(b.opt_mom_peak or lm, lm)
                why = None
                if lm >= sc.opt_mom_target - 1e-9:
                    why = f"target {sc.opt_mom_target:.2f} reached"
                elif lm <= (b.opt_mom_peak or lm) - 0.02 + 1e-9:
                    why = "the trend reversed"
                elif lm < sc.opt_mom_low - 1e-9:
                    why = "left the zone"
                if why:
                    b.opt_in_mom, b.opt_mom_peak = False, None
                    self._opt_log(b, "momentum-end", "monitor", now, side=low_side, mid=round(lm, 4), reason=why)
                elif now - b.opt_mom_last_t >= sc.opt_mom_interval_s * 1000.0:
                    events.append("momentum")
        if not events:
            return None
        b.opt_n += 1
        tag = "+".join(events)
        if "timer" in events:
            b.opt_last_eval_t = now
        if "momentum" in events:
            b.opt_mom_last_t = now
        if blocked:
            self._opt_log(b, tag, "blocked", now, reason=blocked)
            return None
        # --- the initial entry: the fixed 10 shares, the price filter and the limits only
        if init_side:
            b.fired.add("init")
            filled, vwap, eff, why = self._opt_simulate(b, m, tops, init_side, sc.opt_init_shares)
            if why:
                self._opt_log(b, tag, "skip", now, reason=f"initial {init_side}: {why}", r_2s_bps=round(r2, 3))
            elif self._resting_on(tops[init_side][0]) is not None:
                self._opt_log(b, tag, "skip", now, reason="an order on this token is still ending")
            else:
                return self._opt_send(b, m, tops, init_side, filled, eff, tag, "opt_init", now, r_2s_bps=round(r2, 3))
        # --- the optimizer: every side x quantity, plus no-trade
        w0, b0 = min(b.U, b.D) - b.C, max(b.U, b.D) - b.C
        best, notes = None, []
        for side in ("UP", "DOWN"):
            if self._resting_on(tops[side][0]) is not None:
                notes.append(f"{side}: an order is still ending")
                continue
            for q in (float(x) for x in str(sc.opt_quantities).split(",") if x.strip()):
                filled, vwap, eff, why = self._opt_simulate(b, m, tops, side, q)
                if why:
                    notes.append(f"{side} {q:g}: {why}")
                    continue
                U, D = (b.U + filled, b.D) if side == "UP" else (b.U, b.D + filled)
                C = b.C + filled * eff
                w, bb = min(U, D) - C, max(U, D) - C
                if sc.opt_require_positive == "both" and not (U - C > 0 and D - C > 0):
                    notes.append(f"{side} {filled:g}: infeasible (PnL_U {U - C:+.2f}, PnL_D {D - C:+.2f})")
                    continue
                if best is None or w > best["w"] + sc.opt_tie_eps or (abs(w - best["w"]) <= sc.opt_tie_eps and bb > best["b"]):
                    best = {"side": side, "qty": filled, "eff": eff, "vwap": vwap, "w": w, "b": bb}
        if best is None or best["w"] < w0 + sc.opt_min_improvement - 1e-9:
            self._opt_log(b, tag, "skip", now, reason="no_feasible_improvement", worst_now=round(w0, 4),
                          best_candidate=(None if best is None else {k: (round(v, 4) if isinstance(v, float) else v) for k, v in best.items()}),
                          notes=notes[:6], mom_target_projection=(round(self._opt_projection(b, mids), 4) if b.opt_in_mom else None))
            return None
        return self._opt_send(b, m, tops, best["side"], best["qty"], best["eff"], tag, "opt_buy", now,
                              worst_now=round(w0, 4), best_now=round(b0, 4), worst_after=round(best["w"], 4), best_after=round(best["b"], 4))

    def _opt_projection(self, b, mids) -> float:
        """The mark-to-market PnL if the lower-priced token were at the assumed momentum target."""
        low = "UP" if mids["UP"] < mids["DOWN"] else "DOWN"
        pu = self.scfg.opt_mom_target if low == "UP" else 1.0 - self.scfg.opt_mom_target
        return b.U * pu + b.D * (1.0 - pu) - b.C

    def _opt_send(self, b, m, tops, side, qty, eff, tag, kind, now, **extra):
        tok, ob = tops[side]
        limit = round(min(0.99, ob.best_ask + self.scfg.max_slippage), 2)
        o = Order(token_id=tok, side=Side.BUY, price=limit, size=round(qty, 2), tif=TimeInForce.IOC, expected_price=ob.best_ask,
                  tag=f"{side} rising_recovery opt {kind} {qty:g} <= {limit:.2f} ({tag})")
        b.last_order_t = now
        self._opt_log(b, tag, "order", now, side=side, qty=round(qty, 2), limit=limit, effective_price=round(eff, 4), **extra)
        res = self._hold(o, b, side, tok, limit, qty, now, kind)
        self.orders[o.order_id]["trigger"] = tag
        return res

    def _hold(self, o, b, side, tok, px, n, now, kind, avg=None, cap=None):
        # held at once (before on_pair_submit) so the budget and the one-order-per-token rule see it this tick
        self.resting[o.order_id] = {"slug": b.slug, "side": side, "token": tok, "price": px, "size": n, "t": now,
                                    "filled": 0.0, "state": "sent", "kind": kind, "cap": cap}
        self.orders[o.order_id] = {"slug": b.slug, "side": side, "kind": kind, "seq": b.lead_seq}
        self.rr_stats[f"{kind}_orders"] += 1
        if kind not in ("entry", "buy_take", "fix", "fix_take", "hedge", "hedge_take", "cover_bid", "cover_take") and not kind.startswith(("spot_", "opt_")):
            log.info("[RTR_PAIR] %s %s %s %g @ %.2f | unpaired avg %.3f -> pair %.3f | %s", b.slug, kind, side, n, px,
                     avg or 0.0, (avg or 0.0) + px + (taker_fee(px) if kind == "pair_take" else 0.0), b.line())
        return b.slug, side, o

    def pair_cancels(self, now: float) -> list[str]:
        sc = self.scfg
        self._now = max(self._now, now)
        out = []
        for oid, o in list(self.resting.items()):
            if o.get("state") == "cancelling" or str(o.get("kind", "")).endswith("take"):
                continue
            if str(o.get("kind", "")).startswith(("spot_", "opt_")):
                if o["kind"] == "spot_cover":
                    b_ = self.books.get(o["slug"])
                    close_ = self._close_ts.get(o["slug"], b_.close_ts if b_ else None)
                    if close_ is not None and (close_ - now) / 1000.0 < sc.pair_min_left_s:
                        o["state"] = "cancelling"
                        out.append(oid)
                        self.rr_stats["cancel_spot_cover"] += 1
                        log.info("[RTR_CANCEL] %s spot_cover %s bid %.2f x %g: too close to the close", o["slug"], o["side"], o["price"], o["size"])
                continue                                  # takes and spot IOCs end on their own: never "cancelled" here
            b = self.books.get(o["slug"])
            close = self._close_ts.get(o["slug"], b.close_ts if b else None)
            left = (close - now) / 1000.0 if close is not None else 1e9
            last = self.last_top.get(o["token"])
            why = None
            if b is not None and b.phase == "done":
                why = "market done"
            elif o["kind"] == "cover_bid":
                side_ex = b.excess()[0] if b is not None else None
                s = self.signal(o["token"], o["side"], now)
                take_rise = sc.cover_take_min_rise or sc.min_rise
                if left < sc.pair_min_left_s:
                    why = "too close to the close"
                elif side_ex is None or side_ex == o["side"]:
                    why = "nothing left to cover"
                elif (s is not None and s.confirmed and s.rise >= take_rise - 1e-9 and s.age_ms <= sc.max_signal_age_ms
                      and s.ask >= sc.cover_take_min_price - 1e-9):
                    why = "the other token turned up: take it instead"
                    o["no_guard"] = True                      # the take goes out next pass, no 3 s hold
            elif o["kind"] == "hedge":
                tops = {s: (t, None) for s, t in b.tokens.items()} if b is not None and b.tokens else None
                fav, dog, positive = self.state(b, tops) if tops else (None, None, False)
                if left < sc.pair_min_left_s:
                    why = "too close to the close"
                elif not positive or dog != o["side"]:
                    why = "no longer a positive market's lower-priced token"
                elif b.shares(o["side"]) - b.C >= -1e-9:
                    why = "the worst outcome is covered"
                elif last is not None and last[1] is not None and o["price"] + 0.01 - 1e-9 <= last[1] < sc.hedge_max_price - 1e-9:
                    why = f"re-price to {last[1]:.2f}"
            elif o["kind"] == "fix":
                tops = {s: (t, None) for s, t in b.tokens.items()} if b is not None and b.tokens else None
                hi = self.favourite(tops)[0] if tops else None
                if left < sc.pair_min_left_s:
                    why = "too close to the close"
                elif hi != o["side"]:
                    why = "no longer the favourite"
                elif b is not None and self.fix_need(b, o["side"]) <= 1e-9:
                    why = "the favourite's outcome is positive now"
                elif last is not None and last[1] is not None and last[1] >= o["price"] + 0.01 - 1e-9:
                    why = f"re-price to {last[1]:.2f}"
            elif o["kind"] == "entry":
                if b is not None and b.phase == "pairing":
                    why = "the rise ended: pairing"
                elif now - o["t"] >= sc.entry_max_rest_s * 1000.0:
                    why = "rested too long"
                elif left < sc.entry_min_left_s:
                    why = "too close to the close for an entry"
                elif last is not None and last[1] is not None and last[1] >= o["price"] + 0.01 - 1e-9:
                    why = "the bid moved away"
                elif self._drop(o["token"], now) >= 0.01 - 1e-9:
                    why = "the token turned down"
            else:
                if left < sc.pair_min_left_s:
                    why = "too close to the close"
                elif b is not None and sc.high_ratio > 1.0 + 1e-9:
                    side, need = self.ratio_need(b)
                    cap = o.get("cap")
                    if cap is not None:
                        ex_side, _, ex_avg = b.excess()
                        cap = (math.floor((1.0 - sc.pair_margin - ex_avg) * 100.0 + 1e-9) / 100.0
                               if ex_side is not None and ex_side != o["side"] else None)
                    best = last[1] if last and last[1] is not None else None
                    if side != o["side"]:
                        why = f"{sc.high_ratio:g}:1 reached" if side is None else f"now {side} is short"
                    elif o["size"] - o["filled"] > max(need, VENUE_MIN) + 0.01:
                        why = f"re-size to {max(need, VENUE_MIN):.2f}"
                    elif cap is not None and o["price"] > cap + 1e-9:
                        why = f"the pair cap fell to {cap:.2f}"
                    elif best is not None and min(best, cap if cap is not None else best) >= o["price"] + 0.01 - 1e-9:
                        why = f"re-price to {min(best, cap if cap is not None else best):.2f}"
                elif b is not None:
                    side, _, avg = b.excess()
                    if side is None or side == o["side"]:
                        why = "nothing left to pair"
                    else:
                        cap = math.floor((1.0 - sc.pair_margin - avg) * 100.0 + 1e-9) / 100.0
                        best = min(cap, (last[1] if last and last[1] is not None else cap))
                        if o["price"] > cap + 1e-9:
                            why = f"the pair cap fell to {cap:.2f}"
                        elif best >= o["price"] + 0.01 - 1e-9:
                            why = f"re-price to {best:.2f}"
            if why:
                o["state"] = "cancelling"
                out.append(oid)
                if o["kind"] == "entry":
                    self.cooldown[o["token"]] = now + sc.entry_cooldown_s * 1000.0
                self.rr_stats[f"cancel_{o['kind']}"] += 1
                log.info("[RTR_CANCEL] %s %s %s bid %.2f x %g: %s", o["slug"], o["kind"], o["side"], o["price"], o["size"], why)
        return out

    def on_pair_submit(self, order_id: str, slug: str, side: str, price: float, shares: float) -> None:
        o = self.resting.get(order_id)
        if o is not None and o.get("state") == "sent":
            o["state"] = "resting"

    def drop_pair(self, order_id: str) -> None:
        self.resting.pop(order_id, None)

    def on_pair_done(self, order_id: str, filled: bool, killed: bool = False) -> None:
        o = self.resting.pop(order_id, None)
        if o is not None and not filled and not o.get("no_guard"):
            self.end_guard[o["token"]] = self._now + self.scfg.end_guard_s * 1000.0
        if o is not None and not filled and o.get("kind") == "opt_init":
            b = self.books.get(o["slug"])
            if b is not None and b.opt_init_filled <= 1e-9:
                b.fired.discard("init")                   # nothing bought: the initial entry may be proposed again
                b.opt_init_tries += 1
                self.rr_stats["opt_init_retry"] += 1

    # ------------------------------------------------------------------ fills (rule 4)

    def on_fill_detail(self, slug: str, side: str, price: float, size: float, fee: float, now: float) -> None:
        """Booked in on_pair_fill_record (every order of this engine is a pair order): nothing here."""

    def on_pair_fill_record(self, slug: str, order, fill, now: float) -> None:
        oid = getattr(order, "order_id", "")
        o = self.resting.get(oid)                        # None once live.py has ended the order (it does so first)
        sent = self.orders.get(oid) or {}
        b = self.books.get(slug)
        if b is None:
            meta = self.meta_by_slug.get(slug)
            if meta is None:
                log.warning("[RTR_FILL] %s: a fill for a market this engine never quoted -- not booked", slug)
                return
            b = self._book(meta.market)
        side = sent.get("side") or next((s for s, t in b.tokens.items() if t == getattr(fill, "token_id", None)), None)
        if side is None:
            log.warning("[RTR_FILL] %s: a fill of an unknown token %s -- not booked", slug, getattr(fill, "token_id", "?"))
            return
        q, px, fee = float(fill.size), float(fill.price), float(getattr(fill, "fee", 0.0) or 0.0)
        kind = sent.get("kind", "untracked")
        paired = b.add(side, q, px, fee)
        if kind in ("entry", "buy_take", "fix", "fix_take", "hedge", "hedge_take", "cover_bid", "cover_take") or kind.startswith(("spot_", "opt_")):
            b.cycle_fills += 1
            b.cycle_cost += q * px + fee
        if kind == "opt_init":
            b.opt_init_filled += q
        if kind.startswith("opt_"):
            exp = getattr(fill, "expected_price", None) or getattr(order, "expected_price", None)
            self._opt_log(b, sent.get("trigger", kind), "fill", now, side=side, fill_qty=q, fill_price=px, fee=round(fee, 5),
                          slippage=(round((px - exp) * q, 5) if exp else None))
        if kind in ("entry", "buy_take"):
            b.lead_seq += 1                               # a momentum fill: a new cover cycle
            b.lead_t, b.lead_side = now, side
        elif kind == "cover_bid":
            b.cover_filled_seq = max(b.cover_filled_seq, int(sent.get("seq", 0)))
        if kind in ("fix", "fix_take"):
            b.fix_orders.add(oid)
        if o is not None:
            o["filled"] += q
            if o["filled"] >= o["size"] - 1e-9:
                self.resting.pop(order.order_id, None)
        self.rr_stats[f"{kind}_fills"] += 1
        log.info("[RTR_FILL] %s %s %s %g @ %.3f fee %.4f (paired %.2f) | %s", slug, kind, side, q, px, fee, paired, b.line())
        if kind == "spot_climb":
            b.climb_bought += q
            b.climb_episode += 1
            if side != b.climb_side:
                b.climb_peak, b.climb_dipped = None, False   # a new token: its own peak
            b.climb_side = side
        elif kind == "spot_init":
            tot = b.init_shares_filled + q
            b.init_px = ((b.init_px or 0.0) * b.init_shares_filled + px * q) / tot
            b.init_shares_filled, b.init_side = tot, side
        if kind.startswith("spot_"):
            w = b.wants.get(sent.get("trigger", ""))
            if w is not None:
                w["filled"] += q
                if w["shares"] - w["filled"] < VENUE_MIN / 2.0:
                    b.wants.pop(sent["trigger"], None)
            exp = getattr(fill, "expected_price", None) or getattr(order, "expected_price", None)
            self._spot_log(b, trigger=sent.get("trigger", kind), event="fill", side=side, fill_price=px, fill_qty=q,
                           fee=round(fee, 5), slippage=(round((px - exp) * q, 5) if exp else None), now=now)
        if self.scfg.mode in ("spot", "opt"):
            pass                                           # spot / opt modes have no profit lock: their rules decide
        elif b.worst >= self.scfg.target_profit_usdc - 1e-9:
            self._done(b, f"target reached: worst {b.worst:+.2f} >= {self.scfg.target_profit_usdc:g}")
        elif b.phase == "done" and self.scfg.mode == "momentum" and b.why_done.startswith("target"):
            # a late fill broke the lock: the market is open again for the favourite fix / the hedge to repair it
            b.phase, b.why_done = "flat", ""
            self.rr_stats["lock_reopened"] += 1
            log.warning("[RTR_REOPEN] %s a late %s fill broke the profit lock: open again | %s", slug, kind, b.line())

    # ------------------------------------------------------------------ held to settlement

    def exit_decision(self, **kw) -> str | None:
        return None

    def on_exit(self, slug: str, token_id: str, now_ms: float) -> None:
        pass

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        b = self.books.get(slug)
        if b is None:
            return
        got = (b.U if won_up else b.D) - b.C
        self.rr_stats["markets_settled"] += 1
        self.rr_stats["target_markets"] += int(b.why_done.startswith("target"))
        log.info("[RTR_SETTLED] %s %s won: P&L %+.2f (worst was %+.2f, %d cycles) | %s", slug, "UP" if won_up else "DOWN",
                 got, b.worst, b.cycles, b.line())
        b.phase = "settled"

    def _cleanup(self, now: float) -> None:
        for slug, b in list(self.books.items()):
            if now > b.close_ts + 600_000.0:
                self.books.pop(slug, None)
                self.meta_by_slug.pop(slug, None)
                for oid in [i for i, o in self.orders.items() if o["slug"] == slug]:
                    self.orders.pop(oid, None)
        for oid in [i for i, o in self.resting.items() if now > (self.books[o["slug"]].close_ts if o["slug"] in self.books else 0) + 60_000.0]:
            self.resting.pop(oid, None)
        self._forget_closed(now)

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = STRATEGY_ID
        snap.pop("book_momentum", None)
        snap["rising_recovery"] = {
            "config": self.scfg.as_dict(), "stats": dict(self.rr_stats), "open_cost_usdc": round(self.open_cost(), 4),
            "resting": {k: dict(v) for k, v in self.resting.items()},
            "markets": {s: {"phase": b.phase, "high": b.high, "U": round(b.U, 2), "D": round(b.D, 2), "C": round(b.C, 4),
                            **(self._opt_outcomes(b, b._tops) if getattr(b, "_tops", None) else {}),
                            **({"liquidation": self._opt_liquidation(b, b._tops)} if getattr(b, "_tops", None) else {}),
                            "mtm": (round(b.U * self._mid(b.tokens["UP"]) + b.D * self._mid(b.tokens["DOWN"]) - b.C, 4)
                                    if b.tokens and self._mid(b.tokens.get("UP", "")) is not None and self._mid(b.tokens.get("DOWN", "")) is not None else None),
                            "if_up": round(b.U - b.C, 4), "if_down": round(b.D - b.C, 4), "worst": round(b.worst, 4),
                            "cycles": b.cycles, "done": b.why_done}
                        for s, b in self.books.items() if b.U or b.D}}
        return snap
