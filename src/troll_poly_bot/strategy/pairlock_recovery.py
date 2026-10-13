"""DYNAMIC LOSS-RECOVERY / REBALANCING for the pair-lock paper bot (the user's 2026-10-02 spec). Pure: no I/O.

The position is ONE portfolio per market (both sides, all fills), never a pair of independent legs. At every sample:

    outcomes                P&L_UP_WIN  = up_qty   - total cost (fees in)      P&L_DOWN_WIN = down_qty - total cost
                            MIN_SETTLEMENT_PNL = min of the two;  E[P&L] = p_up P&L_UP_WIN + (1 - p_up) P&L_DOWN_WIN
    the losing branch       the outcome with the lower P&L; the RECOVERY SIDE S is the token that pays in it
                            (UP-heavy and falling -> the DOWN-win branch is lower -> buy DOWN; after a DOWN hedge,
                            UP rising -> the UP-win branch is lower -> buy UP: the same rule both ways, any number of times)
    required quantity       x = (R - P&L_losing_branch) / (1 - v(x) - fee(v) - slippage), R = RECOVERY_TARGET_PROFIT,
                            v(x) = the executable VWAP of x shares over the live ask levels (iterated to convergence):
                            your Required_DOWN = (UP_cost + R) / (1 - P_DOWN) with the existing DOWN inventory and the
                            fees included (for a bare UP position P&L_DOWN_WIN = -UP_cost)
    what one buy does       S-wins branch: +x (1 - v - fee);  the other branch: -x (v + fee);  E[P&L]: +x (p_S - v - fee)
    acceptance              all of: inventory on the other side has moved against it by >= MIN_PRICE_MOVE and the
                            reversal signals point to S (>= MIN_CONFIRMATIONS of 4); the whole requirement fits the
                            depth within MAX_SLIPPAGE of the best ask; the plan (full requirement) fits MAX_TOTAL_POSITION,
                            MAX_RECOVERY_CAPITAL and the bankroll; per share p_S - v - fee - slippage >= MIN_RECOVERY_EDGE
                            (= the buy raises E[P&L] by at least MIN_RECOVERY_EDGE x shares); cooldown; MAX_ACTIONS;
                            time left. NOTHING here looks at UP ask + DOWN ask.
    the order               min(x, MAX_RECOVERY_ORDER_SIZE) shares, FOK at <= best ask + MAX_SLIPPAGE: a plan larger
                            than one order proceeds in steps, re-evaluated after every fill.
    STEP_FILL / EDGE_GATE   (2026-10-05) step_fill: the whole plan need not fit -- each step buys what the book within
                            MAX_SLIPPAGE and the caps allow now; edge_gate off: the price rule alone decides. Every
                            swing is recovered again until a cap (position, capital, bankroll, actions, time) stops it.

A structural fact the engine reports (``improves_min``) but does not impose: buying x of S closes the gap between the
two branches by exactly x shares, so MIN_SETTLEMENT_PNL rises only up to x* = (P&L_better - P&L_worse) shares and
falls beyond. Lifting the losing branch to R > min needs x > x*, so a recovery to target always LOWERS the other
branch below where it was (the position becomes a bet on S). ``require_min_improvement`` (default off) turns the
engine into a pure rebalancer that stops at x*.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from .pairlock import MarketView, PairLockParams, reversal_signals
from .pairlock_book import EPS, FillRecord, PairBook, PendingOrder, fee_per_share, vwap_buy
from .pairlock_swing import Phase as SwingPhase
from .pairlock_swing import SwingState

OTHER = {"UP": "DOWN", "DOWN": "UP"}
TARGET_MODES = ("target_profit", "break_even", "partial_recovery", "expected_value")


@dataclass
class RecoveryParams:
    """TPB_PAIRLOCK_REC_<FIELD>. Defaults are for the bot's 10-share first legs (your example's R = 5 on 100 shares
    is 8 % of the position's cost; 0.50 on a 10-share leg at ~0.6 is the same ratio). Nothing is fitted."""
    target_profit: float = 0.50          # RECOVERY_TARGET_PROFIT (USDC) the losing branch is lifted to, fees + slippage in
    max_order_size: float = 30.0         # MAX_RECOVERY_ORDER_SIZE (shares per order)
    max_total_position: float = 60.0     # MAX_TOTAL_POSITION (shares, both sides, one market)
    max_capital: float = 30.0            # MAX_RECOVERY_CAPITAL (USDC spent on recovery buys in one market)
    max_slippage: float = 0.03           # MAX_SLIPPAGE: the VWAP (and the FOK limit) at most this above the best ask
    slippage_allowance: float = 0.005    # estimated slippage per share, charged in every decision
    min_edge: float = 0.0                # MIN_RECOVERY_EDGE per share: p_S - VWAP - fee - slippage
    min_price_move: float = 0.05         # MIN_PRICE_MOVE: the other side's bid this far under its average cost
    cooldown_ms: float = 2000.0          # RECOVERY_COOLDOWN between recovery orders
    max_actions: int = 3                 # recovery orders per market
    min_confirmations: int = 3           # reversal signals toward S (of 4: original weakening, opposite flow, spot, score)
    reversal_threshold: float = 1.5      # the composite score's threshold (one of the 4 signals)
    min_time_left_s: float = 15.0
    min_size: float = 5.0                # the venue minimum
    depth_haircut: float = 0.0           # share of each ask level assumed taken by faster traders
    require_min_improvement: bool = False  # also require MIN_SETTLEMENT_PNL to rise (stops the plan at x*, see module doc)
    # ---- RECOVER AGAIN, EVERY SWING (the user, 2026-10-05: "once it recovered a token, if the token falls again, it
    #      must be recovered again too"). The live logs showed the recoveries failing at the swing itself: the model
    #      edge (P(S) ~ the mid, the ask + fee above it) refused nearly every crossing, and once the requirement grew
    #      the WHOLE plan had to fit the depth within MAX_SLIPPAGE and every cap, or nothing at all was bought.
    edge_gate: bool = True               # False: MIN_RECOVERY_EDGE is not applied (the price rule alone decides)
    step_fill: bool = False              # True: buy what fits NOW (the asks within MAX_SLIPPAGE, MAX_ORDER_SIZE, the room
                                         # left under MAX_TOTAL_POSITION / MAX_RECOVERY_CAPITAL / the bankroll) and keep
                                         # stepping, re-evaluated after every fill, until the branch reaches its target
    # ---- LOSS LIMITS (the user, 2026-10-05: "reduce the large losses"). Every big loss was a recovery chain that kept
    #      doubling until a swing could not be recovered. Where a recovery would be due but one of these holds, the bot
    #      LOCKS the market instead: it buys the side with fewer shares until UP = DOWN, which fixes the market's result
    #      whoever wins (later swings no longer matter), and recovers no more there. MAX_ACTIONS reached locks too.
    max_market_loss: float = 0.0         # USDC, 0 = off: a recovery whose FULL plan would leave the other branch under
                                         # -this LOCKS instead; a late buy is trimmed so no branch falls under -this
    recovery_max_price: float = 1.0      # the recovery token's ask above this -> LOCK instead (1 = off)
    lock_left_s: float = 0.0             # fewer seconds left than this -> LOCK instead of recovering (0 = off)
    # ---- RECOVER ONCE (the user, 2026-10-05: "recover the falling token only once, to reduce losses"): a market gets at
    #      most this many recoveries. One recovery = the buys toward one side while that side stays the higher price
    #      (a plan bought in several steps is one recovery); it ends when its target is reached or the other side
    #      turns higher. After the last one the market is HELD: no further recovery or lock buys. 0 = unlimited.
    max_recoveries: int = 0
    # ---- RECOVER ONLY ON A CLEAR SWING (the user, 2026-10-05, recovery bot: "every recovery must be tried when the
    #      price is over 0.70"): a recovery is bought only once the token it buys has an ask ABOVE this; below it the
    #      bot waits (a swing that turns back first needs no recovery). 0 = off (recover as soon as it turns higher).
    recovery_min_price: float = 0.0
    # ---- RECOVER ONLY WHILE THE RECOVERY TOKEN IS CHEAP ENOUGH (the user, 2026-10-05 16:05, recovery bot: "if the opposite
    #      token's price is under 0.7, then recover the losses"): a recovery is bought only while the token it buys has an
    #      ask UNDER this; at or above it the bot waits (no lock). 1 = off.
    recovery_wait_above: float = 1.0
    # ---- PROFIT EXIT (the user, 2026-10-05 23:20): after a recovery, once the WHOLE position's mark-to-market profit
    #      (every token at its bid, all costs and fees in) reaches this, sell every token held in the market at the bid
    #      (fill-and-kill) instead of waiting for settlement. 0 = off.
    profit_exit: float = 0.0
    # ---- SELL THE FALLING SIDE WHEN RECOVERING (the user, 2026-10-05 23:45: "sell falling tokens when recover"): once a
    #      recovery buy toward one side has filled, every token held of the OTHER (falling) side is sold at its bid
    #      (fill-and-kill, retried after the cooldown) while that bid is above the 0.01 floor. Not after a lock (a lock
    #      buys that side on purpose). The recovery itself is still sized without the sale's proceeds: they lift the
    #      recovered branch above the target and enter the next recovery's arithmetic through realised_exit_pnl.
    recovery_sell: bool = False
    # ---- EVEN OUT FROM THE N-th RECOVERY (the user, 2026-10-06 21:40: "when recovering, buy the same amount as the
    #      falling tokens -- to reduce large losses -- on the second, third recovery"). From recovery number
    #      even_out_from on, a recovery buys the rising token only until the market holds as many of it as of the
    #      falling token (UP = DOWN): both outcomes then pay the same, so the doubling chain stops there. The recoveries
    #      before it keep the target sizing. Sized like a lock (the gap, MAX_RECOVERY_ORDER_SIZE, the depth within
    #      MAX_SLIPPAGE, the bankroll; not MAX_RECOVERY_CAPITAL / MAX_TOTAL_POSITION: it removes risk) and counted as a
    #      recovery; the loss-limit lock triggers do not replace it. 0 = off.
    even_out_from: int = 0
    # ---- LATE LADDER (the user, 2026-10-06 21:40: "if < 90 s left, buy high price tokens in price 0.85-0.95 every
    #      20 s"): in a market the bot HOLDS, with ladder_min_left_s .. ladder_window_s seconds left, buy ladder_shares of
    #      the token with the higher ask while that ask is in [ladder_min_price, ladder_max_price], at most once every
    #      ladder_interval_s seconds; the limit is never above ladder_max_price. MAX_MARKET_LOSS trims a buy so the other
    #      outcome stays above -MAX_MARKET_LOSS. Its fills are late fills (kind "late"), with their own counters.
    ladder_window_s: float = 0.0         # 0 = off
    ladder_min_left_s: float = 10.0      # the bot refuses orders under TPB_TRIANGLE_MIN_TIME_REMAINING_S (10 s)
    ladder_min_price: float = 0.85
    ladder_max_price: float = 0.95
    ladder_interval_s: float = 20.0
    ladder_shares: float = 10.0
    ladder_max_orders: int = 0           # buys per market, 0 = no limit (the window and the interval bound it)
    pair_bid: bool = True                # open/conditions: place the other-side bid after the first buy (False: none)
    # ---- how a market is entered and when recovery starts (the user, 2026-10-02 20:00)
    entry_mode: str = "trigger"          # "trigger": B's spot shock + EV gate | "open": the OPEN rule below |
                                         # "conditions": the CONDITIONS rule (the user, 10-02 20:33) below |
                                         # "low": the LOW-PRICE first leg (the user's 2026-10-07 spec) below
    # ---- LOW-PRICE FIRST LEG (the user's spec, 2026-10-07): the CHEAPER token, not the dearer one, and never "because
    #      it is cheap": its ask must be in [low_price_min, low_price_max], the market must have low_min_left_s ..
    #      low_max_left_s seconds left, the spread <= low_max_spread, open_shares must be executable within
    #      low_max_slippage of the best ask, the pair-lock model's settlement EV per share (P(side) - executable VWAP
    #      - fee - the model's measured slippage and adverse-selection costs; plv2.entry_decision, the same gate as
    #      strategy B's entries) must be >= low_min_edge, and with low_require_flow_signal the flow signals toward that
    #      side (pairlock.reversal_signals, the four confirmations) must number >= low_min_signals. The notional of the
    #      leg (shares x limit) is capped by low_max_notional. The leg is then a strategy-R portfolio like any other:
    #      the recovery arithmetic below manages it (recovery_target_mode decides what it aims at).
    low_price_min: float = 0.05
    low_price_max: float = 0.45
    low_min_edge: float = 0.02           # like PairLockParams.entry_edge
    low_max_spread: float = 0.05
    low_max_slippage: float = 0.03       # the executable VWAP of open_shares at most this above the best ask
    low_min_left_s: float = 120.0        # no first leg with fewer seconds left ...
    low_max_left_s: float = 295.0        # ... nor more (the first seconds of a market: books still forming)
    low_require_flow_signal: bool = True
    low_min_signals: int = 2             # of the 4 reversal_signals toward the side
    low_max_notional: float = 10.0       # USDC per first leg (shares x limit)
    low_max_tries: int = 3               # buys sent per market (a kill leaves it flat)
    # ---- RECOVERY TARGET MODE (the user's spec, 2026-10-07, item 5). What a recovery lifts the recovery side's branch
    #      to (calculate_recovery_quantity):
    #        target_profit     +target_profit USDC (the strategy-R default, every rule above unchanged)
    #        break_even        0: the worse outcome ends >= 0 after every cost
    #        partial_recovery  -(1 - partial_target) x the first leg's cost: recover that share of the exposure
    #        expected_value    sized like break_even but each step must also raise E[P&L] (edge >= 0 at P(S) from the
    #                          model) -- never a hedge the model expects to lose on
    #      min_pnl_improvement: a step must raise the worse outcome (expected_value: the expected P&L) by at least this
    #      many USDC, else WAIT (0 = off). The spec's RECOVERY_OPPOSITE_MIN_PRICE is recovery_min_price above.
    recovery_target_mode: str = "target_profit"
    partial_target: float = 0.5          # the share of the first leg's cost a partial recovery brings back
    min_pnl_improvement: float = 0.0
    open_window_s: float = 10.0          # open: buy within this many seconds after the market opens ...
    open_delay_s: float = 0.0            # open: ... but not before this many seconds have passed (the user, 10-05: 5 s)
    open_min_price: float = 0.50         # ... the HIGHER-priced token, if its ask is in [min, max]
    open_max_price: float = 0.95
    open_shares: float = 10.0            # open: the first leg's size (fill-and-kill / fill-or-kill per the order type)
    open_limit_offset: float = 0.02      # open: limit = the ask + this
    open_max_tries: int = 3              # open: buys sent per market while the window lasts (a kill leaves it flat)
    pair_sum: float = 0.97               # open: right after the buy, a resting bid on the other side at pair_sum - fill
    pair_pull: bool = True               # pull that bid when the other side becomes the higher price (-> recovery)
    pair_rescue_price: float = 0.0       # the user, 2026-10-08: once the bid's token is priced (mid) ABOVE this while shares are
                                         # unpaired, pull the bid and buy that token for exactly the unpaired shares (UP = DOWN).
                                         # 0 = off
    pair_reprice_drop: float = 0.0       # the user, 2026-10-08: a TRAILING opposite bid -- once the bid's token has fallen (mid) this
                                         # much since the bid was placed (or last re-placed), cancel it and re-place it lower by
                                         # the same fall, for the shares still unpaired. 0 = off
    pair_reprice_max: int = 3            # ... at most this many re-placements per bid
    recovery_enabled: bool = True        # False: the loss-recovery arithmetic never buys (entries, pairs, late buys only)
    # ---- the CONDITIONS rule: buy open_shares of the higher-priced token (then the pair bid, as the open rule) when
    #      its ask is in [cond_min_price, cond_max_price], >= cond_min_left_s are left, cond_spot_lo < BTC - strike <
    #      cond_spot_hi (USD) and the market holds no tokens
    cond_min_price: float = 0.50
    cond_max_price: float = 0.60
    cond_min_left_s: float = 120.0
    cond_spot_lo: float = 0.0
    cond_spot_hi: float = 5.0
    cond_rising_window_s: float = 0.0    # the user, 2026-10-08: buy the higher token only while it is RISING -- its mid now
                                         # minus its mid this many seconds ago must exceed cond_rising_min (0 = off)
    cond_rising_min: float = 0.0         # USDC: 0 = any rise; a flat or falling token is not bought
    cond_rising_long_s: float = 0.0      # 2026-10-08 (the user: "it still buys downward tokens"): ALSO rising over this longer
                                         # window (mid now > mid then + cond_rising_min); 0 = off; <= 14 s (the history kept)
    cond_rising_last_s: float = 0.0      # ... and NOT falling over the last this-many seconds (mid now >= mid then); 0 = off
    cond_rising_spot: bool = False       # ... and BTC moving the token's way over the longer window (UP: BTC up, DOWN: down)
    # ---- the HIGH BAND (the user, 2026-10-08: "or the high price token is over 0.85 and under 0.92; repeat this once filled"):
    #      an alternative entry -- the higher-priced token with its ask strictly between cond_high_min and cond_high_max, the
    #      rising checks above and >= cond_min_left_s left (the BTC - strike band does NOT apply), bought again after each
    #      fill (no pair bid: the other token is at 0.08-0.15) up to cond_high_max_entries per market. 0 / 0 = off.
    cond_high_min: float = 0.0
    cond_high_max: float = 0.0
    cond_high_max_entries: int = 5      # the REPEAT COUNT: high-band buys per market
    cond_high_pair_bid: bool = False     # 2026-10-08 the user: after each high-band fill, the opposite bid at pair_sum - fill; the
                                         # next high-band buy then waits until that bid has filled (one bid works at a time)
    # ---- BUY THE LOWER TOKEN (the user, 2026-10-08: "when entry, buy the lower token instead of the high price token"): the
    #      conditions are judged on the higher-priced token as before (its ask in the band, BTC - strike, time left), then
    #      the OTHER, lower-priced token is bought; the rising checks apply to the token bought
    # ---- MARKET OPEN BUY (the user, 2026-10-08): within cond_open_window_s seconds after the market opens, while it holds
    #      nothing, buy the HIGHER-priced token if its ask is in [cond_open_min, cond_open_max] (open_shares, its opposite
    #      bid as usual). A separate entry rule: the BTC - strike band and the rising checks do not apply to it.
    cond_first_buy_delay_s: float = 0.0  # the user, 2026-10-08: a market's FIRST buy (any entry rule) not before this many
                                         # seconds after it opens (later buys are not delayed); 0 = at once
    cond_open_buy: bool = True
    # ---- the other two entry rules' switches (the user, 2026-10-08): the regular conditions entry ("after-open buy") and the
    #      high band ("high price buy", the band cond_high_min..cond_high_max). Off = that rule never buys.
    cond_after_buy: bool = True
    cond_after_delay_s: float = 0.0      # the after-open buy only this many seconds (or more) after the market opens (0 = at once)
    cond_high_buy: bool = True
    cond_open_window_s: float = 20.0
    cond_open_min: float = 0.50
    cond_open_max: float = 0.70
    cond_buy_lower: bool = False         # the normal conditions entry
    cond_high_buy_lower: bool = False    # the high band
    cond_max_entries: int = 1            # conditions buys per market: a buy after the first only once EVERY held token is
                                         # paired (UP = DOWN, the last pair bid filled) -- the user, 10-02 21:00
    direction: str = "signals"           # recovery side: "signals" (>= min_confirmations of 4) | "price" (the side
                                         # whose price is now higher than the held side's; the user's open rule)
    # ---- LATE BUY (the user, 2026-10-02: "at the last few seconds, buy winning tokens to increase profits")
    late_enabled: bool = True
    late_window_s: float = 20.0          # from this many seconds before the close ...
    late_min_left_s: float = 10.0        # ... down to this many (the bot refuses orders under TPB_TRIANGLE_MIN_TIME_REMAINING_S)
    late_min_p: float = 0.95             # the model's P(side wins) that makes a side "the winner"
    late_max_price: float = 0.97         # never pay more than this for it
    late_min_edge: float = 0.0           # per share: P(win) - VWAP - fee - slippage allowance
    late_shares: float = 10.0            # one clip
    late_max_orders: int = 2             # clips per market
    late_max_capital: float = 20.0       # USDC on late buys per market
    late_trigger: str = "model"          # "model": P(side) >= late_min_p and the edge | "price": the higher token's
                                         # mid > late_min_mid (the user, 10-02 20:33: "price higher than 0.93")
    late_min_mid: float = 0.93
    late_hedge_trigger: float = 0.0      # after a late buy: its token's mid falls under this -> buy the other side
    late_hedge_price: float = 0.20       # ... with a bid at this price, for the late shares (0 trigger = off)
    # ---- UNFILLED-LEG RECOVERY (the user's 2026-10-05 spec; the logic: strategy/pairlock_ulr.py). A first leg whose
    #      pair bid did not fill gets a PARTIAL opposite position as a TRADE with its own exits (take profit / stop /
    #      signal reversal / before the close), never an equal-cost pair held to settlement. While it is on, the
    #      strategy-R arithmetic above does not buy and the pair bid is never pulled (it rests until the close).
    ulr_enabled: bool = False
    ulr_min_move: float = 0.08           # RECOVERY_MIN_MOVE: the opposite ask this far above the hedge bid's price
    ulr_min_time_remaining_s: float = 60.0   # RECOVERY_MIN_TIME_REMAINING: no new recovery with fewer seconds left
    ulr_initial_ratio: float = 0.30      # RECOVERY_INITIAL_RATIO of the unhedged first-leg shares
    ulr_take_profit: float = 0.08        # RECOVERY_TAKE_PROFIT: the executable bid this far above the entry
    ulr_stop_loss: float = 0.04          # RECOVERY_STOP_LOSS: the executable bid this far under the entry
    ulr_max_entries: int = 1             # MAX_RECOVERY_ENTRIES_PER_POSITION (filled entries; no averaging down)
    ulr_max_attempts: int = 3            # orders sent per position (a killed order is an attempt, not an entry)
    ulr_require_spot: bool = True        # RECOVERY_REQUIRE_SPOT_CONFIRMATION
    ulr_signal_exit: bool = True         # RECOVERY_SIGNAL_EXIT
    ulr_opposite_min_price: float = 0.50 # the opposite token's ask must be above this
    ulr_momentum_window_s: float = 3.0   # the token mids / spot compared with this many seconds earlier
    ulr_momentum_min: float = 0.01       # ... and must have moved at least this (USDC) in the required direction
    ulr_expected_move: float = 0.08      # the subsequent move the trade bets on; must exceed spread + fees + slip + margin
    ulr_safety_margin: float = 0.01
    ulr_max_slippage: float = 0.02       # buy limit = ask + this, sell limit = bid - this
    ulr_exit_before_close_s: float = 20.0   # RECOVERY_MAX_TIME_TO_RESOLUTION: an open recovery is sold with fewer s left
    ulr_cooldown_ms: float = 2000.0      # between recovery orders (entry retries, sell retries)
    ulr_max_position_size: float = 30.0  # MAX_RECOVERY_POSITION_SIZE (shares)
    ulr_max_total_exposure: float = 30.0 # MAX_RECOVERY_TOTAL_EXPOSURE (USDC in open recovery positions, all markets)
    ulr_max_trades_per_market: int = 1   # MAX_RECOVERY_TRADES_PER_MARKET
    # ---- LIMITED-DEPTH DIRECTIONAL RECOVERY + TWO-SIDED HEDGE AT SWING 3+ (the user's spec, 2026-10-07; the logic:
    #      strategy/pairlock_swing.py). swing_mode replaces every rule above that buys or sells after the first leg (the
    #      open rule, the recovery arithmetic, locks, even-out, late buys, the ladder, profit exit, falling-side sale,
    #      pair bid): swing 0 = INITIAL_SHARES of the leader at an executable VWAP in [ENTRY_MIN, ENTRY_MAX] about
    #      ENTRY_DELAY_S after the open; swing 1 / 2 = SWING1_SHARES / SWING2_SHARES of the new leader once it leads by
    #      LEADER_HYSTERESIS with its ask at SWING_RECOVERY_PRICE; any swing past MAX_RECOVERY_SWING = buy the lighter
    #      side until UP = DOWN (ENABLE_TWO_SIDED_HEDGE); every order inside MAX_MARKET_CAPITAL. Env:
    #      TPB_PAIRLOCK_<SPEC NAME> (engine.SWING_SPEC_NAMES) or TPB_PAIRLOCK_REC_<FIELD>. Paper only (__main__ refuses it on real money).
    swing_mode: bool = False
    swing_initial_shares: float = 10.0       # INITIAL_SHARES
    swing_entry_min: float = 0.55            # INITIAL_ENTRY_MIN (the executable VWAP of the initial shares)
    swing_entry_max: float = 0.60            # INITIAL_ENTRY_MAX (also the entry order's limit)
    swing_entry_delay_s: float = 20.0        # INITIAL_ENTRY_DELAY_S: "approximately 20 s after the market starts" ...
    swing_entry_window_s: float = 30.0       # INITIAL_ENTRY_WINDOW_S: ... and no first buy later than this after the open
    swing_recovery_price: float = 0.70       # SWING_RECOVERY_PRICE: the new leader's best ask must reach this
    # ---- the recovery SIZE (the user, 2026-10-08: "the recovery quantity is an output of the current inventory + order book
    #      + fees + target profit, not a fixed parameter"): at swing 1 and swing 2 buy the smallest integer number of shares
    #      whose win outcome, from the whole inventory and cost so far and priced on the walked ask book with fees, exceeds
    #      TARGET_RECOVERY_PROFIT; refused (nothing bought) when the depth, MAX_RECOVERY_SHARES, MAX_RECOVERY_PRICE or the
    #      capital cap cannot take the whole size. "fixed" = the 2026-10-07 spec's SWING1_SHARES / SWING2_SHARES (legacy:
    #      the earlier backtest and sweep only).
    swing_sizing: str = "target"             # "target" | "fixed"
    swing_target_profit: float = 3.00        # TARGET_RECOVERY_PROFIT (USDC, fees in)
    swing_max_recovery_shares: float = 150.0 # MAX_RECOVERY_SHARES (one swing's recovery, all its orders)
    swing_max_recovery_price: float = 0.85   # MAX_RECOVERY_PRICE: no recovery size may need an ask level above this
    swing1_shares: float = 24.0              # SWING1_SHARES (swing_sizing "fixed" only)
    swing2_shares: float = 80.0              # SWING2_SHARES (swing_sizing "fixed" only)
    swing_max_recovery: int = 2              # MAX_RECOVERY_SWING: directional recoveries before hedge mode (0..2)
    swing_two_sided_hedge: bool = True       # ENABLE_TWO_SIDED_HEDGE
    swing_max_market_capital: float = 125.0  # MAX_MARKET_CAPITAL: USDC (fees in) one market may ever have spent
    swing_leader_hysteresis: float = 0.04    # LEADER_HYSTERESIS: the challenger's mid must exceed the leader's by this

    def __post_init__(self) -> None:
        if self.target_profit < 0.0:
            raise ValueError("target_profit must be >= 0")
        if not 1 <= int(self.min_confirmations) <= 4:
            raise ValueError("min_confirmations must be 1..4")
        if self.max_slippage < 0.0 or self.min_price_move <= 0.0:
            raise ValueError("max_slippage >= 0 and min_price_move > 0")
        if (self.entry_mode not in ("trigger", "open", "conditions", "low") or self.direction not in ("signals", "price")
                or self.late_trigger not in ("model", "price")):
            raise ValueError("entry_mode trigger|open|conditions|low, direction signals|price, late_trigger model|price")
        if self.recovery_target_mode not in TARGET_MODES:
            raise ValueError(f"recovery_target_mode one of {TARGET_MODES}")
        if not 0.0 < self.partial_target <= 1.0 or self.min_pnl_improvement < 0.0:
            raise ValueError("partial_target in (0, 1], min_pnl_improvement >= 0")
        if (not 0.0 < self.low_price_min <= self.low_price_max < 1.0 or self.low_max_spread < 0.0 or self.low_max_slippage < 0.0
                or self.low_min_left_s < 0.0 or self.low_max_left_s < self.low_min_left_s or self.low_min_signals < 0
                or self.low_max_notional <= 0.0 or self.low_max_tries < 1):
            raise ValueError("0 < low_price_min <= low_price_max < 1, low_max_spread/slippage >= 0, "
                             "0 <= low_min_left_s <= low_max_left_s, low_min_signals >= 0, low_max_notional > 0, low_max_tries >= 1")
        if not 0.5 <= self.pair_sum <= 1.0:
            raise ValueError("pair_sum must be in [0.50, 1.00]")
        if self.max_recoveries < 0:
            raise ValueError("max_recoveries >= 0 (0 = unlimited)")
        if not 0.0 <= self.recovery_min_price < 1.0:
            raise ValueError("recovery_min_price must be in [0, 1)")
        if not 0.0 < self.recovery_wait_above <= 1.0:
            raise ValueError("recovery_wait_above must be in (0, 1]")
        if self.max_market_loss < 0.0 or self.lock_left_s < 0.0 or not 0.0 < self.recovery_max_price <= 1.0:
            raise ValueError("max_market_loss >= 0, lock_left_s >= 0, 0 < recovery_max_price <= 1")
        if self.even_out_from < 0:
            raise ValueError("even_out_from >= 0 (0 = off)")
        if (self.ladder_window_s < 0.0 or self.ladder_interval_s < 0.0 or self.ladder_shares <= 0.0
                or self.ladder_max_orders < 0 or not 0.0 < self.ladder_min_price <= self.ladder_max_price < 1.0):
            raise ValueError("ladder_window_s >= 0, ladder_interval_s >= 0, ladder_shares > 0, ladder_max_orders >= 0, "
                             "0 < ladder_min_price <= ladder_max_price < 1")
        if not 0.0 < self.ulr_initial_ratio <= 1.0 or self.ulr_take_profit <= 0.0 or self.ulr_stop_loss <= 0.0:
            raise ValueError("ulr_initial_ratio in (0, 1], ulr_take_profit > 0, ulr_stop_loss > 0")
        if self.ulr_min_move < 0.0 or self.ulr_max_entries < 1 or self.ulr_max_attempts < 1:
            raise ValueError("ulr_min_move >= 0, ulr_max_entries >= 1, ulr_max_attempts >= 1")
        if (not 0.0 < self.swing_entry_min <= self.swing_entry_max < 1.0 or not 0.0 < self.swing_recovery_price < 1.0
                or self.swing_initial_shares <= 0.0 or self.swing1_shares < 0.0 or self.swing2_shares < 0.0
                or not 0 <= int(self.swing_max_recovery) <= 2 or self.swing_max_market_capital <= 0.0
                or self.swing_leader_hysteresis < 0.0 or not 0.0 <= self.swing_entry_delay_s <= self.swing_entry_window_s
                or self.swing_sizing not in ("target", "fixed") or self.swing_target_profit < 0.0
                or self.swing_max_recovery_shares <= 0.0 or not 0.0 < self.swing_max_recovery_price < 1.0):
            raise ValueError("0 < swing_entry_min <= swing_entry_max < 1, 0 < swing_recovery_price < 1, swing_initial_shares > 0, "
                             "swing1/2_shares >= 0, swing_max_recovery 0..2, swing_max_market_capital > 0, "
                             "swing_leader_hysteresis >= 0, 0 <= swing_entry_delay_s <= swing_entry_window_s, swing_sizing "
                             "target|fixed, swing_target_profit >= 0, swing_max_recovery_shares > 0, 0 < swing_max_recovery_price < 1")

    @property
    def lock_active(self) -> bool:
        """Any loss limit set: the bot locks a market instead of chasing a recovery it should not make."""
        return self.max_market_loss > 0.0 or self.recovery_max_price < 1.0 or self.lock_left_s > 0.0


class RState(str, Enum):
    WAITING = "WAITING"
    INITIAL_ENTRY = "INITIAL_ENTRY"
    HOLDING = "HOLDING"                      # inventory, no order in flight: evaluated every sample
    RECOVERY_PENDING = "RECOVERY_PENDING"    # a recovery order in flight (pending is NOT inventory)
    SETTLEMENT_PENDING = "SETTLEMENT_PENDING"
    SETTLED = "SETTLED"


TRANSITIONS = {
    RState.WAITING: {RState.INITIAL_ENTRY, RState.RECOVERY_PENDING},            # a late buy may open a market
    RState.INITIAL_ENTRY: {RState.HOLDING, RState.WAITING, RState.SETTLEMENT_PENDING},
    RState.HOLDING: {RState.RECOVERY_PENDING, RState.SETTLEMENT_PENDING,
                     RState.INITIAL_ENTRY},                                     # a conditions buy after a filled pair
    RState.RECOVERY_PENDING: {RState.HOLDING, RState.WAITING, RState.SETTLEMENT_PENDING},
    RState.SETTLEMENT_PENDING: {RState.SETTLED},
    RState.SETTLED: set(),
}


@dataclass
class RecoveryCycle:
    """One market's portfolio from the first signal to settlement; the PairBook (fills) is the inventory."""
    book: PairBook
    a_side: str
    state: RState = RState.WAITING
    started_t: float = 0.0
    target: float = 0.0                  # RECOVERY_TARGET_PROFIT this market is managed to (the engine sets it)
    entry_qty: float = 0.0
    actions: int = 0
    last_action_t: float = -1e18
    last_eval_t: float = -1e18
    recovery_cost: float = 0.0           # USDC spent on recovery buys (gross + fee)
    late_orders: int = 0
    late_cost: float = 0.0
    open_entry: bool = False             # entered by the open rule (its pair bid follows the fill)
    pair: dict | None = None             # the resting other-side bid: {oid, side, price, size, filled, state}
    late_hedge: dict | None = None       # after a late buy: {held, side, qty, trigger, price, oid, filled, state}
    pending: dict | None = None
    locked: bool = False                 # UP = DOWN after a LOCK: the result is fixed, no more recovery here
    lock_reason: str = ""
    lock_steps: int = 0                  # lock orders sent (not counted in actions)
    rec_episodes: int = 0                # recoveries started (counted at their first fill)
    rec_open: bool = False               # a recovery is in progress (its side is still the higher price)
    rec_side: str = ""                   # the side that recovery buys
    ladder_orders: int = 0               # late-ladder buys sent
    ladder_last_t: float = -1e18         # ... the last one's send time (ladder_interval_s apart)
    ladder_cost: float = 0.0             # USDC spent on late-ladder buys (gross + fee)
    ulr: dict | None = None             # the unfilled-leg recovery state (pairlock_ulr.ULR.to_json), ulr mode only
    swing: SwingState | None = None      # swing mode: the market's swing state machine and accounting (pairlock_swing)
    settled_winner: str | None = None
    recoveries: list = field(default_factory=list)      # one dict per recovery FILL (the sequence table)
    events: list = field(default_factory=list)          # every evaluation / order / fill record
    transitions: list = field(default_factory=list)

    def to(self, new: RState, t: float, reason: str = "") -> None:
        if new is self.state:
            return
        if new not in TRANSITIONS[self.state]:
            raise RuntimeError(f"{self.state.value} -> {new.value} is not a valid transition")
        self.transitions.append((t, self.state.value, new.value, reason))
        self.state = new

    @property
    def holds(self) -> bool:
        return self.book.up_qty + self.book.down_qty > EPS

    def to_json(self) -> dict:
        return {"book": self.book.to_json(), "a_side": self.a_side, "state": self.state.value, "started_t": self.started_t,
                "target": self.target, "entry_qty": self.entry_qty, "actions": self.actions, "last_action_t": self.last_action_t,
                "recovery_cost": self.recovery_cost, "late_orders": self.late_orders, "late_cost": self.late_cost,
                "open_entry": self.open_entry, "pair": self.pair, "late_hedge": self.late_hedge,
                "locked": self.locked, "lock_reason": self.lock_reason, "lock_steps": self.lock_steps,
                "rec_episodes": self.rec_episodes, "rec_open": self.rec_open, "rec_side": self.rec_side, "ulr": self.ulr,
                "ladder_orders": self.ladder_orders, "ladder_last_t": self.ladder_last_t, "ladder_cost": self.ladder_cost,
                "swing": None if self.swing is None else self.swing.to_json(), "recoveries": list(self.recoveries),
                "events": list(self.events[-40:]), "transitions": list(self.transitions[-40:])}

    @classmethod
    def from_json(cls, d: dict) -> "RecoveryCycle":
        c = cls(book=PairBook.from_json(d["book"]), a_side=d["a_side"], state=RState(d.get("state", "HOLDING")),
                started_t=float(d.get("started_t") or 0.0), target=float(d.get("target") or 0.0),
                entry_qty=float(d.get("entry_qty") or 0.0),
                actions=int(d.get("actions") or 0), last_action_t=float(d.get("last_action_t") or -1e18),
                recovery_cost=float(d.get("recovery_cost") or 0.0), late_orders=int(d.get("late_orders") or 0),
                late_cost=float(d.get("late_cost") or 0.0), open_entry=bool(d.get("open_entry")), pair=d.get("pair"),
                late_hedge=d.get("late_hedge"), locked=bool(d.get("locked")), lock_reason=str(d.get("lock_reason") or ""),
                lock_steps=int(d.get("lock_steps") or 0), rec_episodes=int(d.get("rec_episodes") or 0),
                rec_open=bool(d.get("rec_open")), rec_side=str(d.get("rec_side") or ""), ulr=d.get("ulr"),
                ladder_orders=int(d.get("ladder_orders") or 0), ladder_last_t=float(d.get("ladder_last_t") or -1e18),
                ladder_cost=float(d.get("ladder_cost") or 0.0),
                swing=SwingState.from_json(d["swing"]) if d.get("swing") else None,
                recoveries=list(d.get("recoveries") or []),
                events=list(d.get("events") or []), transitions=[tuple(x) for x in d.get("transitions") or []])
        c.book.pending.clear()                      # an order in flight at the stop resolved while we were down
        if c.state in (RState.INITIAL_ENTRY, RState.RECOVERY_PENDING):
            c.state = RState.HOLDING if c.holds else RState.WAITING
        if c.swing is not None and c.swing.phase is SwingPhase.INITIAL_ENTRY and c.holds:
            c.swing.phase = SwingPhase.SWING_0          # the first buy filled while the bot was down
        return c


# ───────────────────────────────── the arithmetic ─────────────────────────────────


def outcomes(book: PairBook, p_up: float | None = None) -> dict:
    """Both settlement branches of the whole portfolio (fees inside total cost), the minimum, and E[P&L]."""
    pu, pd = book.resolution_pnl("UP"), book.resolution_pnl("DOWN")
    out = {"pnl_up_win": pu, "pnl_down_win": pd, "min_settlement_pnl": min(pu, pd),
           "losing_branch": "UP" if pu < pd else "DOWN"}
    if p_up is not None:
        out["expected_pnl"] = p_up * pu + (1.0 - p_up) * pd
    return out


def required_quantity(branch_pnl: float, target: float, profit_per_share: float) -> float | None:
    """Shares of S that lift the S-wins branch from ``branch_pnl`` to ``target``: (R - P&L) / (1 - v - fee - slip).
    None when the profit per share is not positive (the price leaves nothing to recover with)."""
    if profit_per_share <= 1e-9:
        return None
    need = target - branch_pnl
    return 0.0 if need <= 0.0 else need / profit_per_share


def first_leg_cost(book: PairBook) -> float:
    """What the market's first leg(s) cost, fees in: the entry fills (an outside first fill counts too)."""
    c = sum(f.gross_cost + f.fee for f in book.fills if f.action == "BUY" and f.kind in ("entry", "external"))
    return c if c > 0.0 else book.total_cost


def recovery_target(book: PairBook, prm: RecoveryParams) -> float:
    """The USDC the recovery side's branch is lifted to (RECOVERY_TARGET_MODE)."""
    m = prm.recovery_target_mode
    if m == "target_profit":
        return prm.target_profit
    if m == "partial_recovery":
        return -(1.0 - prm.partial_target) * first_leg_cost(book)
    return 0.0                                          # break_even, expected_value


def _fee_over(used: list, fees) -> float:
    """The fee per share of a walk through ``used`` = [(price, shares)] under the caller's fee rule."""
    q = sum(float(n) for _, n in used)
    return sum(float(n) * fees(float(p)) for p, n in used) / q if q > 0.0 else 0.0


def calculate_recovery_quantity(first_leg_quantity: float, first_leg_cost: float, opposite_book: list,
                                target_worst_case_pnl: float, fees=fee_per_share, slippage: float = 0.0,
                                remaining_time: float | None = None, inventory: dict | None = None,
                                depth_haircut: float = 0.0, min_size: float = 0.0, iters: int = 8) -> dict:
    """The minimum OPPOSITE quantity that lifts the opposite-wins outcome to ``target_worst_case_pnl``, at the
    EXECUTABLE price of that very quantity (the VWAP walked through ``opposite_book`` = [(ask, shares)], not the best
    ask times the quantity), fees and ``slippage`` per share in. The spec's item 5 (2026-10-07).

    The arithmetic. With the whole position already held -- ``inventory`` = {"up", "down", "total_cost", "realised"}
    (shares of each side, every cost paid incl. fees, realised exit proceeds); without it, a first leg of
    ``first_leg_quantity`` shares that cost ``first_leg_cost`` and nothing else -- the opposite-wins outcome before the
    buy is  B = opposite shares - total cost + realised.  Each opposite share bought at VWAP v with fee f adds
    1 - v - f - slippage to that outcome, so  x = (target - B) / (1 - v - f - slippage).  NOTE the direction of this:
    the DEARER the opposite token, the MORE shares the recovery needs (20 UP @ 0.20 = 4 USDC: 10 DOWN at 0.60, 13.33 at
    0.70, 20 at 0.80, before fees); the cheap first leg only ever needs FEWER opposite shares than its own size while
    p1 + p2 < 1 (fees and slippage aside). v itself depends on x (deeper levels cost more), so the two are iterated.
    Shares of the opposite side ALREADY held count toward the requirement: additional = max(0, required - held).

    Returns every figure the decision and the RECOVERY_EVAL line need: required_total (opposite shares the position
    should hold), existing (held now), additional (to buy), vwap / fee_per_share / levels of the additional buy,
    cost (the buy's cost, fees and slippage in), worst / best / the opposite-wins and first-wins outcomes before and
    after, feasible (the visible depth fills it), and why when it cannot be sized."""
    inv = inventory or {"first": first_leg_quantity, "opp": 0.0, "total_cost": first_leg_cost, "realised": 0.0}
    held_first, held_opp = float(inv.get("first", 0.0)), float(inv.get("opp", 0.0))
    tc, realised = float(inv.get("total_cost", 0.0)), float(inv.get("realised", 0.0))
    opp_before = held_opp - tc + realised                   # the opposite side wins
    first_before = held_first - tc + realised               # the first leg's side wins
    out = {"first_leg_quantity": first_leg_quantity, "first_leg_cost": first_leg_cost, "existing": held_opp,
           "target": target_worst_case_pnl, "remaining_time": remaining_time,
           "opposite_win_before": opp_before, "first_win_before": first_before,
           "worst_before": min(opp_before, first_before), "best_before": max(opp_before, first_before),
           "required_total": None, "additional": 0.0, "vwap": None, "fee_per_share": 0.0, "cost": 0.0, "levels": [],
           "feasible": False, "why": ""}
    best = min((float(p) for p, q in opposite_book if float(q) > 0.0), default=None) if opposite_book else None
    out["best_ask"] = best
    if best is None:
        out["why"] = "no opposite ask"
        return out
    if opp_before >= target_worst_case_pnl - 1e-9:
        out.update(required_total=held_opp, feasible=True, vwap=best, fee_per_share=fees(best),
                   opposite_win_after=opp_before, first_win_after=first_before,
                   worst_after=out["worst_before"], best_after=out["best_before"], why="target already satisfied")
        return out
    v, fps = best, fees(best)
    x = None
    for _ in range(iters):
        pps = 1.0 - v - fps - slippage
        if pps <= 1e-9:
            out.update(vwap=v, fee_per_share=fps, why=f"the opposite side at {v:.3f} + fee + slippage leaves no profit per share")
            return out
        x_new = math.ceil((target_worst_case_pnl - opp_before) / pps * 100.0) / 100.0
        vw, _f, used = vwap_buy(opposite_book, max(x_new, min_size), depth_haircut)
        f2 = _fee_over(used, fees)
        if vw is None:
            out.update(required_total=held_opp + x_new, additional=x_new, vwap=None, fee_per_share=fps,
                       why=f"insufficient depth for {x_new:g} opposite shares")
            return out
        if x is not None and abs(vw - v) < 1e-9 and abs(x_new - x) < 1e-6:
            break
        v, fps, x = vw, f2, x_new
    x = max(x_new, min_size) if x_new > 0.0 else 0.0
    vw, _f, used = vwap_buy(opposite_book, x, depth_haircut) if x > 0.0 else (best, 0.0, [])
    fps = _fee_over(used, fees) if used else fees(best)
    cost = x * ((vw or 0.0) + fps + slippage)
    opp_after = opp_before + x * (1.0 - (vw or 0.0) - fps - slippage)
    first_after = first_before - cost
    out.update(required_total=held_opp + x, additional=x, vwap=vw, fee_per_share=fps, levels=used, cost=cost,
               price_impact=None if vw is None else vw - best, opposite_win_after=opp_after, first_win_after=first_after,
               worst_after=min(opp_after, first_after), best_after=max(opp_after, first_after), feasible=vw is not None,
               risk_reduction=min(opp_after, first_after) - min(opp_before, first_before))
    return out


def plan_recovery(book: PairBook, side: str, asks: list, prm: RecoveryParams, p_side: float | None = None) -> dict:
    """The requirement for ``side`` at the EXECUTABLE price: x and v(x) iterated together (v depends on how deep x
    reaches). Returns every figure the decision and the log need; ``feasible_depth`` False when the ask levels cannot
    fill x within MAX_SLIPPAGE of the best ask."""
    o = outcomes(book)
    branch = o["pnl_up_win"] if side == "UP" else o["pnl_down_win"]
    target = recovery_target(book, prm)
    other = OTHER[side]
    c = calculate_recovery_quantity(book.qty(other), first_leg_cost(book), asks, target, fee_per_share,
                                    prm.slippage_allowance, None,
                                    {"first": book.qty(other), "opp": book.qty(side), "total_cost": book.total_cost,
                                     "realised": book.realised_exit_pnl},
                                    prm.depth_haircut, prm.min_size)
    best = c.get("best_ask")
    out = {"side": side, "branch_pnl_before": branch, "best_ask": best, "target": target, "sizing": c}
    if best is None:
        return {**out, "q_required": None, "vwap": None, "fee_ps": 0.0, "feasible_depth": False, "why": f"no {side} ask"}
    if c["why"] == "target already satisfied":
        return {**out, "q_required": 0.0, "vwap": best, "fee_ps": fee_per_share(best), "feasible_depth": True,
                "why": "target already satisfied"}
    if c["vwap"] is None:
        why = c["why"].replace("opposite", side).replace("the opposite side", side)
        return {**out, "q_required": c["required_total"] and c["additional"], "vwap": None, "fee_ps": c["fee_per_share"],
                "feasible_depth": False, "why": f"{side} at {c['vwap'] or best:.3f} + fee leaves no profit per share"
                if "no profit" in c["why"] else why}
    q, vw, fps, used, slip = c["additional"], c["vwap"], c["fee_per_share"], c["levels"], c["price_impact"]
    return {**out, "q_required": q, "vwap": vw, "fee_ps": fps, "levels": used,
            "price_impact": slip, "feasible_depth": vw is not None and slip <= prm.max_slippage + 1e-9,
            "profit_per_share": None if vw is None else 1.0 - vw - fps - prm.slippage_allowance,
            "edge_per_share": None if vw is None or p_side is None else p_side - vw - fps - prm.slippage_allowance,
            "why": "" if vw is not None and slip <= prm.max_slippage + 1e-9 else
            (f"insufficient depth for {q:g} {side}" if vw is None else
             f"VWAP {vw:.3f} is {slip:.3f} over the best ask {best:.2f} (MAX_SLIPPAGE {prm.max_slippage:.2f})")}


def simulate_buy(book: PairBook, side: str, q: float, vwap: float, fee_ps: float, slip: float, p_up: float | None) -> dict:
    """The whole portfolio after buying q of ``side`` at vwap + slip with fee_ps per share."""
    price = vwap + slip
    cost = q * (price + fee_ps)
    up = book.up_qty + (q if side == "UP" else 0.0)
    dn = book.down_qty + (q if side == "DOWN" else 0.0)
    tc = book.total_cost + cost
    pu, pd = up - tc + book.realised_exit_pnl, dn - tc + book.realised_exit_pnl
    out = {"q": q, "price": price, "cost": cost, "up_qty": up, "down_qty": dn, "total_cost": tc,
           "pnl_up_win": pu, "pnl_down_win": pd, "min_settlement_pnl": min(pu, pd)}
    if p_up is not None:
        out["expected_pnl"] = p_up * pu + (1.0 - p_up) * pd
    return out


# ───────────────────────────────── the decision ─────────────────────────────────


@dataclass
class RecoveryDecision:
    side: str
    shares: float
    limit: float
    vwap: float
    plan_qty: float
    reason: str
    record: dict
    kind: str = "recovery"               # "recovery" | "late" | "lock" | "ulr"


def evaluate_recovery(cyc: RecoveryCycle, p_up: float | None, feat: dict | None, mv: MarketView, now: float,
                      left: float, model, prm: RecoveryParams, bankroll_free: float,
                      quote_fresh: bool = True) -> tuple[RecoveryDecision | None, dict]:
    """One sample of a held portfolio. Returns (the order or None, the full evaluation record: spec item 13)."""
    b = cyc.book
    o = outcomes(b, p_up)
    rec = {"t": now, "state": cyc.state.value, "up_bid": mv.bid_up, "up_ask": mv.ask_up, "down_bid": mv.bid_down,
           "down_ask": mv.ask_down, "up_qty": b.up_qty, "down_qty": b.down_qty, "up_avg_cost": b.up_avg_cost,
           "down_avg_cost": b.down_avg_cost, "total_cost": b.total_cost, "fees_paid": b.fees,
           "slippage_paid": b.slippage, "p_up": p_up, "pnl_up_win": o["pnl_up_win"], "pnl_down_win": o["pnl_down_win"],
           "min_settlement_pnl": o["min_settlement_pnl"], "expected_pnl": o.get("expected_pnl"),
           "losing_branch": o["losing_branch"], "target": recovery_target(b, prm), "target_mode": prm.recovery_target_mode,
           "time_left": left, "first_side": cyc.a_side, "first_qty": b.qty(cyc.a_side), "first_vwap": b.avg_cost(cyc.a_side),
           "first_leg_cost": first_leg_cost(b), "decision": "REJECTED", "verdict": "REJECT", "why": ""}
    tgt = rec["target"]

    def reject(why: str) -> tuple[None, dict]:
        rec["why"] = why
        rec["verdict"] = _verdict(rec)
        cyc.events.append(dict(rec))
        return None, rec

    if cyc.state is not RState.HOLDING or not cyc.holds:
        return reject(f"state {cyc.state.value}")
    if cyc.locked:
        rec["decision"] = "NONE"
        return reject(f"market locked ({cyc.lock_reason}): UP {b.up_qty:g} / DOWN {b.down_qty:g}")
    if o["min_settlement_pnl"] >= tgt - 1e-9:
        rec["decision"] = "NONE"
        return reject("target already satisfied on both branches")
    if left < prm.min_time_left_s:
        return reject(f"{left:.0f} s left < MIN_TIME_LEFT {prm.min_time_left_s:.0f}")
    if cyc.actions >= prm.max_actions and not prm.lock_active:
        return reject(f"MAX_ACTIONS {prm.max_actions} reached")
    if now - cyc.last_action_t < prm.cooldown_ms:
        return reject(f"cooldown ({now - cyc.last_action_t:.0f} ms since the last order)")
    if not quote_fresh or feat is None:
        return reject("stale data")
    if cyc.lock_steps > 0 and not cyc.locked:
        # a lock has started: finish it (UP = DOWN) whatever the price does now -- it is never undone by a recovery
        return _lock_order(cyc, cyc.lock_reason or "lock in progress", o, mv, prm, bankroll_free, rec, reject, p_up)
    # 1. direction: the side the market now favours is the RECOVERY SIDE S (the confirmed reversal toward it, >=
    #    MIN_CONFIRMATIONS of 4 signals); the other side O must hold inventory that has moved against it by
    #    >= MIN_PRICE_MOVE. "UP is moving against the position and may lose -> buy DOWN", and the reverse.
    pp = PairLockParams(recovery_reversal_threshold=prm.reversal_threshold)
    sig = {s: reversal_signals(feat, OTHER[s], model, pp) for s in ("UP", "DOWN")}     # signals TOWARD s
    if prm.direction == "price":
        # the open rule: recovery starts when the OTHER side's price is higher than the held side's price
        mids = {s: _mid(mv.bid(s), mv.ask(s)) for s in ("UP", "DOWN")}
        if mids["UP"] is None or mids["DOWN"] is None:
            return reject("no two-sided price")
        if abs(mids["UP"] - mids["DOWN"]) < 1e-9:
            return reject(f"UP and DOWN at the same price ({mids['UP']:.3f})")
        S = "UP" if mids["UP"] > mids["DOWN"] else "DOWN"
        O = OTHER[S]
        rec.update({"recovery_side": S, "confirmations": sig[S]["count"], "signals": sig[S]["signals"],
                    "confirmations_toward_up": sig["UP"]["count"], "confirmations_toward_down": sig["DOWN"]["count"],
                    "losing_side_exposure": b.total_cost_side(O), "mid_up": mids["UP"], "mid_down": mids["DOWN"]})
    else:
        S = max(("UP", "DOWN"), key=lambda s: (sig[s]["count"], s == o["losing_branch"]))
        O = OTHER[S]
        rec.update({"recovery_side": S, "confirmations": sig[S]["count"], "signals": sig[S]["signals"],
                    "confirmations_toward_up": sig["UP"]["count"], "confirmations_toward_down": sig["DOWN"]["count"],
                    "losing_side_exposure": b.total_cost_side(O)})
        if sig[S]["count"] < prm.min_confirmations:
            return reject(f"no confirmed reversal (toward UP {sig['UP']['count']}/4, toward DOWN {sig['DOWN']['count']}/4; "
                          f"MIN_CONFIRMATIONS {prm.min_confirmations})")
    if cyc.rec_open and S != cyc.rec_side:
        cyc.rec_open = False                 # the other side turned higher: that recovery is over
    rec["recoveries_done"] = cyc.rec_episodes
    avg_o, bid_o = b.avg_cost(O), mv.bid(O)
    if b.qty(O) <= EPS:
        return reject(f"the market favours {S} but there is no {O} inventory to recover from")
    if bid_o is None or avg_o is None:
        return reject(f"no {O} bid")
    move = avg_o - bid_o
    rec["losing_side_move"] = move
    if move < prm.min_price_move - 1e-9:
        return reject(f"{O} bid {bid_o:.2f} is {move:+.3f} under its cost {avg_o:.3f}: MIN_PRICE_MOVE {prm.min_price_move:.2f} not met")
    branch_s = o["pnl_up_win"] if S == "UP" else o["pnl_down_win"]
    rec["branch_pnl_before"] = branch_s
    if branch_s >= tgt - 1e-9:
        if cyc.rec_open and S == cyc.rec_side:
            cyc.rec_open = False             # that recovery reached its target
        rec["decision"] = "NONE"
        return reject(f"target already satisfied on the {S}-win branch ({branch_s:+.3f} >= {tgt:.2f})")
    # 2./3./4. the requirement at the executable price, fees and slippage in
    p_side = None if p_up is None else (p_up if S == "UP" else 1.0 - p_up)
    plan = plan_recovery(b, S, mv.asks(S), prm, p_side)
    sz = plan.get("sizing") or {}
    rec.update({"executable_vwap": plan.get("vwap"), "fee_per_share": plan.get("fee_ps"),
                "recovery_qty_required": plan.get("q_required"), "price_impact": plan.get("price_impact"),
                "profit_per_share": plan.get("profit_per_share"), "edge_per_share": plan.get("edge_per_share"),
                # the spec's recovery-decision fields (2026-10-07, item 13)
                "opposite_side": S, "opposite_best_ask": plan.get("best_ask"), "opposite_vwap": plan.get("vwap"),
                "existing_opposite_qty": b.qty(S), "required_opposite_qty": sz.get("required_total"),
                "additional_qty": sz.get("additional"), "recovery_cost": sz.get("cost"),
                "worst_case_before": sz.get("worst_before"), "worst_case_after": sz.get("worst_after"),
                "best_case_before": sz.get("best_before"), "best_case_after": sz.get("best_after"),
                "risk_reduction": sz.get("risk_reduction"), "expected_before": o.get("expected_pnl"),
                "expected_after": None if p_up is None or sz.get("additional") is None or sz.get("vwap") is None else
                simulate_buy(b, S, sz["additional"], sz["vwap"], sz["fee_per_share"], prm.slippage_allowance, p_up)["expected_pnl"]})
    best_s = plan.get("best_ask")
    if prm.recovery_min_price > 0.0 and (best_s is None or best_s <= prm.recovery_min_price + 1e-9):
        rec["decision"] = "NONE"
        return reject(f"waiting: {S} ask {'none' if best_s is None else f'{best_s:.2f}'} is not above the recovery's "
                      f"lowest price {prm.recovery_min_price:.2f}")
    if prm.recovery_wait_above < 1.0 and (best_s is None or best_s >= prm.recovery_wait_above - 1e-9):
        rec["decision"] = "NONE"
        return reject(f"waiting: {S} ask {'none' if best_s is None else f'{best_s:.2f}'} is not under the recovery's "
                      f"highest price {prm.recovery_wait_above:.2f}")
    if prm.max_recoveries > 0 and not (cyc.rec_open and S == cyc.rec_side) and cyc.rec_episodes >= prm.max_recoveries:
        rec["decision"] = "NONE"
        return reject(f"recovered {cyc.rec_episodes} time(s) already (MAX_RECOVERIES {prm.max_recoveries}): "
                      f"the market is held, the {S} branch stays {branch_s:+.3f}")
    number = cyc.rec_episodes if (cyc.rec_open and S == cyc.rec_side) else cyc.rec_episodes + 1
    rec["recovery_number"] = number
    if prm.even_out_from > 0 and number >= prm.even_out_from:
        return _even_out_order(cyc, S, number, mv, prm, bankroll_free, rec, reject, p_up)
    if prm.lock_active:
        why_lock = _lock_trigger(cyc, S, branch_s, plan, o, left, prm, rec)
        if why_lock:
            return _lock_order(cyc, why_lock, o, mv, prm, bankroll_free, rec, reject, p_up)
    if prm.step_fill:
        return _step_order(cyc, S, branch_s, plan, p_side, p_up, o, mv, prm, bankroll_free, rec, reject)
    if plan.get("q_required") in (None, 0.0) or not plan["feasible_depth"]:
        return reject(plan["why"] or "nothing to buy")
    q_req, vw, fps = plan["q_required"], plan["vwap"], plan["fee_ps"]
    full = simulate_buy(b, S, q_req, vw, fps, prm.slippage_allowance, p_up)
    rec.update({"recovery_cost_required": full["cost"], "plan_pnl_up_win": full["pnl_up_win"],
                "plan_pnl_down_win": full["pnl_down_win"], "plan_min_settlement_pnl": full["min_settlement_pnl"]})
    # 5./6. the PLAN (the whole requirement) must fit the limits -- a hedge that cannot be completed is not started
    if b.up_qty + b.down_qty + q_req > prm.max_total_position + 1e-9:
        return reject(f"position after the plan {b.up_qty + b.down_qty + q_req:g} sh > MAX_TOTAL_POSITION {prm.max_total_position:g}")
    if cyc.recovery_cost + full["cost"] > prm.max_capital + 1e-9:
        return reject(f"recovery capital {cyc.recovery_cost + full['cost']:.2f} > MAX_RECOVERY_CAPITAL {prm.max_capital:.2f}")
    if full["cost"] > bankroll_free + 1e-9:
        return reject(f"cost {full['cost']:.2f} > free bankroll {bankroll_free:.2f}")
    # 9. the buy must improve the portfolio: E[P&L] by >= MIN_RECOVERY_EDGE per share (p_S above the executable cost)
    edge = plan["edge_per_share"]
    if edge is None and prm.edge_gate:
        return reject("no probability for the recovery side")
    if prm.edge_gate and edge < prm.min_edge - 1e-12:
        return reject(f"edge {edge:+.4f}/sh (P({S}) {p_side:.3f} - VWAP {vw:.3f} - fee {fps:.4f} - slip) < MIN_RECOVERY_EDGE {prm.min_edge:+.4f}")
    # the order: one step of the plan
    q = min(q_req, prm.max_order_size)
    if q < prm.min_size - 1e-9:
        q = prm.min_size
    vw_q, fps_q, used = vwap_buy(mv.asks(S), q, prm.depth_haircut)
    if vw_q is None:
        return reject(f"insufficient depth for the {q:g}-share step")
    step = simulate_buy(b, S, q, vw_q, fps_q, prm.slippage_allowance, p_up)
    improves_min = step["min_settlement_pnl"] > o["min_settlement_pnl"] + 1e-9
    rec.update({"order_qty": q, "order_vwap": vw_q, "order_cost": step["cost"], "after_pnl_up_win": step["pnl_up_win"],
                "after_pnl_down_win": step["pnl_down_win"], "after_min_settlement_pnl": step["min_settlement_pnl"],
                "after_expected_pnl": step.get("expected_pnl"), "improves_min": improves_min,
                "expected_improvement": None if p_up is None else step["expected_pnl"] - o["expected_pnl"],
                "partial_step": q < q_req - 1e-9})
    if prm.require_min_improvement and not improves_min:
        return reject(f"MIN_SETTLEMENT_PNL would not improve ({o['min_settlement_pnl']:+.3f} -> {step['min_settlement_pnl']:+.3f})")
    limit = min(0.99, round(plan["best_ask"] + prm.max_slippage, 2))
    last = max(p for p, _ in used)
    if last > limit + 1e-9:
        return reject(f"the step needs level {last:.2f} > limit {limit:.2f} (MAX_SLIPPAGE)")
    rec["decision"] = "BUY"
    rec["verdict"] = "BUY"
    rec["limit"] = limit
    reason = (f"recover via {S}: its branch {branch_s:+.3f} -> target {plan['target']:+.2f} "
              f"needs {q_req:g} sh at VWAP {vw:.3f} (fee {fps:.4f}); this order {q:g} sh; after it UP-win "
              f"{step['pnl_up_win']:+.3f} DOWN-win {step['pnl_down_win']:+.3f} min {step['min_settlement_pnl']:+.3f}; "
              f"E[P&L] {o.get('expected_pnl', 0):+.3f} -> {step.get('expected_pnl', 0):+.3f}; improves min: {improves_min}")
    rec["why"] = reason
    cyc.events.append(dict(rec))
    return RecoveryDecision(S, q, limit, vw_q, q_req, reason, rec), rec


_WAIT_MARKS = ("waiting:", "cooldown", "stale data", "MIN_PRICE_MOVE", "s left <", "recovered ", "target already",
               "MIN_PNL_IMPROVEMENT", "market locked", "nothing to buy", "leaves nothing to even", "no two-sided",
               "the market favours", "same price")


def _verdict(rec: dict) -> str:
    """The spec's three-way reading of a record (2026-10-07): BUY, WAIT (nothing wrong, not yet / not now) or
    REJECT (the recovery as sized is refused)."""
    if rec.get("decision") in ("BUY", "LOCK"):
        return "BUY"
    why = str(rec.get("why") or "")
    if rec.get("decision") in ("NONE", "LOCKED") or any(m in why for m in _WAIT_MARKS):
        return "WAIT"
    return "REJECT"


def _plan_qty(branch_s: float, plan: dict, prm: RecoveryParams) -> tuple[float | None, float | None]:
    """(shares the full recovery needs, the price it is estimated at): the plan's VWAP when it could price the whole
    requirement, else the best ask. (None, None) when the price leaves no profit per share."""
    best = plan.get("best_ask")
    if best is None:
        return None, None
    q, v = plan.get("q_required"), plan.get("vwap")
    if q is None or v is None:
        v = best
        q = required_quantity(branch_s, plan.get("target", prm.target_profit), 1.0 - v - fee_per_share(v) - prm.slippage_allowance)
        if q is None:
            return None, v
        q = math.ceil(q * 100.0) / 100.0
    return q, v


def _lock_trigger(cyc: RecoveryCycle, S: str, branch_s: float, plan: dict, o: dict, left: float,
                  prm: RecoveryParams, rec: dict) -> str:
    """Why this recovery should LOCK the market instead ('' = recover as usual)."""
    if cyc.actions >= prm.max_actions:
        return f"MAX_ACTIONS {prm.max_actions} reached"
    if prm.lock_left_s > 0.0 and left < prm.lock_left_s:
        return f"{left:.0f} s left < {prm.lock_left_s:g}"
    best = plan.get("best_ask")
    if prm.recovery_max_price < 1.0 and best is not None and best > prm.recovery_max_price + 1e-9:
        return f"{S} ask {best:.2f} > the recovery's highest price {prm.recovery_max_price:.2f}"
    if prm.max_market_loss > 0.0:
        L = prm.max_market_loss
        if o["min_settlement_pnl"] < -L - 1e-9:
            return f"the {o['losing_branch']}-win branch {o['min_settlement_pnl']:+.2f} is already under -{L:g}"
        q, v = _plan_qty(branch_s, plan, prm)
        if q is None:
            return f"{S} at {v:.2f} leaves no profit per share" if v is not None else ""
        full = simulate_buy(cyc.book, S, q, v, fee_per_share(v), prm.slippage_allowance, None)
        rec["plan_min_settlement_pnl"] = full["min_settlement_pnl"]
        if full["min_settlement_pnl"] < -L - 1e-9:
            return (f"recovering {q:g} {S} would leave the {OTHER[S]}-win branch at {full['min_settlement_pnl']:+.2f} "
                    f"(MAX_MARKET_LOSS {L:g})")
    return ""


def _lock_order(cyc: RecoveryCycle, why: str, o: dict, mv: MarketView, prm: RecoveryParams, bankroll_free: float,
                rec: dict, reject, p_up: float | None) -> tuple[RecoveryDecision | None, dict]:
    """LOCK: buy the side with FEWER shares until UP = DOWN; both branches then pay the same, so the market's result
    is fixed. Not limited by MAX_TOTAL_POSITION / MAX_RECOVERY_CAPITAL / MAX_ACTIONS (it only removes risk); the book
    within MAX_SLIPPAGE, the order size and the bankroll still apply. A gap under the venue minimum counts as locked."""
    b = cyc.book
    gap = b.up_qty - b.down_qty
    side = "DOWN" if gap > 0 else "UP"
    need = math.floor(abs(gap) * 100.0 + 1e-6) / 100.0
    rec.update({"lock_reason": why, "lock_side": side, "lock_gap": need})
    if need < prm.min_size - 1e-9:
        cyc.locked, cyc.lock_reason = True, why
        rec["decision"] = "LOCKED"
        cyc.events.append({**rec, "event": "LOCKED"})
        return reject(f"LOCKED ({why}): UP {b.up_qty:g} / DOWN {b.down_qty:g}, result fixed at "
                      f"UP-win {o['pnl_up_win']:+.2f} / DOWN-win {o['pnl_down_win']:+.2f}")
    st = _evening_step(side, need, mv, prm, bankroll_free, rec)
    if isinstance(st, str):
        return reject(f"lock ({why}): {st}")
    q, vw, fps, limit = st
    step = simulate_buy(b, side, q, vw, fps, prm.slippage_allowance, p_up)
    rec.update({"decision": "LOCK", "verdict": "BUY", "limit": limit, "order_qty": q, "order_vwap": vw, "order_cost": step["cost"],
                "after_pnl_up_win": step["pnl_up_win"], "after_pnl_down_win": step["pnl_down_win"],
                "after_min_settlement_pnl": step["min_settlement_pnl"], "improves_min": True,
                "expected_improvement": None if p_up is None else step["expected_pnl"] - o["expected_pnl"],
                "partial_step": q < need - 1e-9})
    reason = (f"LOCK ({why}): buy {q:g} {side} at VWAP {vw:.3f} to even UP {b.up_qty:g} / DOWN {b.down_qty:g}; "
              f"after it UP-win {step['pnl_up_win']:+.3f} DOWN-win {step['pnl_down_win']:+.3f}")
    rec["why"] = reason
    cyc.events.append(dict(rec))
    return RecoveryDecision(side, q, limit, vw, need, reason, rec, kind="lock"), rec


def _evening_step(side: str, need: float, mv: MarketView, prm: RecoveryParams, bankroll_free: float,
                  rec: dict) -> tuple[float, float, float, float] | str:
    """One buy of ``side`` toward UP = DOWN (a lock or an even-out recovery): (shares, VWAP, fee/share, limit), or why
    not. Bounded by the gap, MAX_RECOVERY_ORDER_SIZE, the asks within MAX_SLIPPAGE of the best ask and the free
    bankroll -- not by MAX_TOTAL_POSITION / MAX_RECOVERY_CAPITAL (evening out only removes risk)."""
    asks = mv.asks(side)
    best = min((p for p, sz in asks if sz > 0), default=None) if asks else None
    if best is None:
        return f"no {side} ask"
    limit = min(0.99, round(best + prm.max_slippage, 2))
    levels = [(p, sz) for p, sz in asks if p <= limit + 1e-9 and sz > 0]
    unit = limit + fee_per_share(limit) + prm.slippage_allowance
    room = {"the gap": need, "MAX_RECOVERY_ORDER_SIZE": prm.max_order_size,
            "depth within MAX_SLIPPAGE": sum(sz * (1.0 - prm.depth_haircut) for _, sz in levels),
            "free bankroll": bankroll_free / unit}
    bound = min(room, key=room.get)
    q = math.floor(max(0.0, room[bound]) * 100.0) / 100.0
    rec.update({"step_room": {k: round(v, 2) for k, v in room.items()}, "step_bound": bound})
    if q < prm.min_size - 1e-9:
        return f"{bound} leaves {max(0.0, room[bound]):.2f} sh < the {prm.min_size:g}-share minimum"
    vw, fps, _used = vwap_buy(levels, q, prm.depth_haircut)
    if vw is None:
        return f"insufficient depth for {q:g} {side}"
    return q, vw, fps, limit


def _even_out_order(cyc: RecoveryCycle, S: str, number: int, mv: MarketView, prm: RecoveryParams, bankroll_free: float,
                    rec: dict, reject, p_up: float | None) -> tuple[RecoveryDecision | None, dict]:
    """EVEN OUT (recovery number >= even_out_from): buy the rising side S only until it matches the falling side's
    shares (UP = DOWN), so both outcomes pay the same. Counted as a recovery (kind "recovery")."""
    b = cyc.book
    O = OTHER[S]
    need = math.floor((b.qty(O) - b.qty(S)) * 100.0 + 1e-6) / 100.0
    rec.update({"even_out": True, "even_out_gap": need})
    if need < prm.min_size - 1e-9:
        rec["decision"] = "NONE"
        return reject(f"recovery {number} evens out: {S} {b.qty(S):g} vs {O} {b.qty(O):g} leaves nothing to even "
                      f"(gap {max(need, 0.0):g} sh < the {prm.min_size:g}-share minimum)")
    st = _evening_step(S, need, mv, prm, bankroll_free, rec)
    if isinstance(st, str):
        return reject(f"recovery {number} evens out: {st}")
    q, vw, fps, limit = st
    o = outcomes(b, p_up)
    step = simulate_buy(b, S, q, vw, fps, prm.slippage_allowance, p_up)
    rec.update({"decision": "BUY", "verdict": "BUY", "limit": limit, "order_qty": q, "order_vwap": vw, "order_cost": step["cost"],
                "after_pnl_up_win": step["pnl_up_win"], "after_pnl_down_win": step["pnl_down_win"],
                "after_min_settlement_pnl": step["min_settlement_pnl"],
                "improves_min": step["min_settlement_pnl"] > o["min_settlement_pnl"] + 1e-9,
                "expected_improvement": None if p_up is None else step["expected_pnl"] - o["expected_pnl"],
                "partial_step": q < need - 1e-9})
    reason = (f"recovery {number} EVENS OUT: buy {q:g} {S} at VWAP {vw:.3f} (fee {fps:.4f}) toward {S} = {O} "
              f"({b.qty(S):g} -> {b.qty(S) + q:g} vs {b.qty(O):g}); after it UP-win {step['pnl_up_win']:+.3f} "
              f"DOWN-win {step['pnl_down_win']:+.3f}")
    rec["why"] = reason
    cyc.events.append(dict(rec))
    return RecoveryDecision(S, q, limit, vw, need, reason, rec), rec


def _step_order(cyc: RecoveryCycle, S: str, branch_s: float, plan: dict, p_side: float | None, p_up: float | None,
                o: dict, mv: MarketView, prm: RecoveryParams, bankroll_free: float, rec: dict,
                reject) -> tuple[RecoveryDecision | None, dict]:
    """STEP_FILL: one order of the recovery now, as large as everything allows -- the requirement (to the target),
    MAX_RECOVERY_ORDER_SIZE, the asks within MAX_SLIPPAGE of the best ask, the room left under MAX_TOTAL_POSITION,
    MAX_RECOVERY_CAPITAL and the free bankroll. The rest follows in the next steps (every fill re-evaluates), so a
    requirement that has outgrown the book is bought in pieces instead of not at all."""
    b = cyc.book
    best = plan.get("best_ask")
    if best is None:
        return reject(plan.get("why") or f"no {S} ask")
    q_req = plan.get("q_required")
    if q_req is None or (plan.get("vwap") is None and q_req > 0.0):
        # the plan could not be priced over the whole requirement: size it at the best ask (the step is capped below)
        q_req = required_quantity(branch_s, plan.get("target", prm.target_profit), 1.0 - best - fee_per_share(best) - prm.slippage_allowance)
        if q_req is None:
            return reject(f"{S} at {best:.2f} + fee leaves no profit per share")
        q_req = math.ceil(q_req * 100.0) / 100.0
    if q_req <= 0.0:
        rec["decision"] = "NONE"
        return reject("nothing to buy")
    q_want = max(q_req, prm.min_size)
    limit = min(0.99, round(best + prm.max_slippage, 2))
    levels = [(p, s) for p, s in mv.asks(S) if p <= limit + 1e-9 and s > 0]
    unit = limit + fee_per_share(limit) + prm.slippage_allowance        # the most one share can cost in this order
    room = {"requirement": q_want, "MAX_RECOVERY_ORDER_SIZE": prm.max_order_size,
            "depth within MAX_SLIPPAGE": sum(s * (1.0 - prm.depth_haircut) for _, s in levels),
            "MAX_TOTAL_POSITION": prm.max_total_position - (b.up_qty + b.down_qty),
            "MAX_RECOVERY_CAPITAL": (prm.max_capital - cyc.recovery_cost) / unit,
            "free bankroll": bankroll_free / unit}
    bound = min(room, key=room.get)
    q = math.floor(max(0.0, room[bound]) * 100.0) / 100.0
    rec.update({"recovery_qty_required": q_req, "step_room": {k: round(v, 2) for k, v in room.items()}, "step_bound": bound})
    if q < prm.min_size - 1e-9:
        return reject(f"{bound} leaves {max(0.0, room[bound]):.2f} sh < the {prm.min_size:g}-share minimum "
                      f"(needs {q_req:g} sh of {S})")
    vw_q, fps_q, used = vwap_buy(levels, q, prm.depth_haircut)
    if vw_q is None:
        return reject(f"insufficient depth for the {q:g}-share step")
    edge = None if p_side is None else p_side - vw_q - fps_q - prm.slippage_allowance
    rec["edge_per_share"] = edge
    if prm.edge_gate and (edge is None or edge < prm.min_edge - 1e-12):
        return reject("no probability for the recovery side" if edge is None else
                      f"edge {edge:+.4f}/sh (P({S}) {p_side:.3f} - VWAP {vw_q:.3f} - fee {fps_q:.4f} - slip) < "
                      f"MIN_RECOVERY_EDGE {prm.min_edge:+.4f}")
    step = simulate_buy(b, S, q, vw_q, fps_q, prm.slippage_allowance, p_up)
    improves_min = step["min_settlement_pnl"] > o["min_settlement_pnl"] + 1e-9
    partial = q < q_want - 1e-9
    rec.update({"order_qty": q, "order_vwap": vw_q, "order_cost": step["cost"], "after_pnl_up_win": step["pnl_up_win"],
                "after_pnl_down_win": step["pnl_down_win"], "after_min_settlement_pnl": step["min_settlement_pnl"],
                "after_expected_pnl": step.get("expected_pnl"), "improves_min": improves_min,
                "expected_improvement": None if p_up is None else step["expected_pnl"] - o["expected_pnl"],
                "partial_step": partial})
    if prm.require_min_improvement and not improves_min:
        return reject(f"MIN_SETTLEMENT_PNL would not improve ({o['min_settlement_pnl']:+.3f} -> {step['min_settlement_pnl']:+.3f})")
    # the spec's trigger (2026-10-07, item 7): the buy must IMPROVE the portfolio, not merely follow a price
    gain = step["min_settlement_pnl"] - o["min_settlement_pnl"]
    if prm.recovery_target_mode == "expected_value":
        if edge is None:
            return reject("expected_value mode: no probability for the recovery side")
        if edge < -1e-12:
            return reject(f"expected_value mode: edge {edge:+.4f}/sh < 0 (P({S}) {p_side:.3f} - VWAP {vw_q:.3f} - fee - slip): "
                          "the hedge would lower E[P&L]")
        gain = rec["expected_improvement"] if rec["expected_improvement"] is not None else gain
    if prm.min_pnl_improvement > 0.0 and gain < prm.min_pnl_improvement - 1e-9:
        return reject(f"improvement {gain:+.3f} < MIN_PNL_IMPROVEMENT {prm.min_pnl_improvement:.2f} "
                      f"({'E[P&L]' if prm.recovery_target_mode == 'expected_value' else 'worst outcome'})")
    rec["decision"] = "BUY"
    rec["verdict"] = "BUY"
    rec["limit"] = limit
    reason = (f"recover via {S}: its branch {branch_s:+.3f} -> target {plan['target']:+.2f} needs {q_req:g} sh; "
              f"this step {q:g} sh at VWAP {vw_q:.3f} (fee {fps_q:.4f})"
              + (f", limited by {bound}" if partial else "") +
              f"; after it UP-win {step['pnl_up_win']:+.3f} DOWN-win {step['pnl_down_win']:+.3f}")
    rec["why"] = reason
    cyc.events.append(dict(rec))
    return RecoveryDecision(S, q, limit, vw_q, q_req, reason, rec), rec


def _mid(bid, ask):
    if bid is not None and ask is not None:
        return 0.5 * (bid + ask)
    return ask if ask is not None else bid


def open_entry_side(mv: MarketView, prm: RecoveryParams) -> tuple[str | None, str]:
    """THE OPEN RULE's first leg: the token with the higher ask, if that ask is in [open_min_price, open_max_price]."""
    if mv.ask_up is None or mv.ask_down is None:
        return None, "no two-sided book"
    side = "UP" if mv.ask_up >= mv.ask_down else "DOWN"
    ask = mv.ask(side)
    if not (prm.open_min_price - 1e-9 <= ask <= prm.open_max_price + 1e-9):
        return None, f"higher token {side} ask {ask:.2f} outside {prm.open_min_price:.2f}-{prm.open_max_price:.2f}"
    return side, ""


def low_entry_side(mv: MarketView, prm: RecoveryParams, left: float, feat: dict | None, model,
                   entry_prm: PairLockParams | None = None) -> tuple[str | None, dict]:
    """THE LOW-PRICE FIRST LEG (the user's spec, 2026-10-07): the CHEAPER token, if it earns its place. Returns
    (side or None, the evaluation record: the ask, executable VWAP, fee, P(side), EV/share, the flow signals and why).
    The EV gate is strategy B's own (plv2.entry_decision with the low band and edge): P(side wins) from the pair-lock
    model minus the executable VWAP of open_shares, the fee, and the slippage and adverse-selection costs the model
    measured on real orders. No second probability model."""
    from . import pairlock_v2 as plv2
    rec = {"kind": "low", "decision": "REJECTED", "up_ask": mv.ask_up, "down_ask": mv.ask_down, "time_left": left}
    if mv.ask_up is None or mv.ask_down is None:
        return None, {**rec, "why": "no two-sided book"}
    if not prm.low_min_left_s <= left <= prm.low_max_left_s:
        return None, {**rec, "why": f"{left:.0f} s left is outside the low-price window {prm.low_max_left_s:g}..{prm.low_min_left_s:g} s"}
    if abs(mv.ask_up - mv.ask_down) < 1e-9:
        return None, {**rec, "why": f"UP and DOWN at the same ask ({mv.ask_up:.2f}): no cheaper token"}
    side = "UP" if mv.ask_up < mv.ask_down else "DOWN"
    ask, bid = mv.ask(side), mv.bid(side)
    rec.update({"side": side, "ask": ask, "bid": bid})
    if not (prm.low_price_min - 1e-9 <= ask <= prm.low_price_max + 1e-9):
        return None, {**rec, "why": f"cheaper token {side} ask {ask:.2f} outside {prm.low_price_min:.2f}-{prm.low_price_max:.2f}"}
    if feat is None:
        return None, {**rec, "why": "pairlock features not ready (history / sigma)"}
    if prm.low_require_flow_signal:
        sig = reversal_signals(feat, OTHER[side], model, PairLockParams(recovery_reversal_threshold=prm.reversal_threshold))
        rec.update({"signals": [k for k, v in sig["signals"].items() if v], "confirmations": sig["count"]})
        if sig["count"] < prm.low_min_signals:
            return None, {**rec, "why": f"flow toward {side}: {sig['count']}/4 signals < LOW_MIN_SIGNALS {prm.low_min_signals}"}
    # strategy B's entry gate, bounded to the low band: EV = P(side) - VWAP(open_shares) - fee - slip - adverse
    ep = entry_prm or PairLockParams()
    gate = plv2.PairLockParams(entry_edge=prm.low_min_edge, entry_shares=prm.open_shares, min_size=prm.min_size,
                               band_lo=prm.low_price_min, band_hi=prm.low_price_max, entry_offset=prm.open_limit_offset,
                               min_left_entry_s=prm.low_min_left_s, depth_haircut=prm.depth_haircut)
    lim = plv2.RiskLimits(max_spread=prm.low_max_spread, min_time_remaining_s=prm.low_min_left_s,
                          max_market_capital=1e9, max_total_position=1e9, max_market_loss=1e9)
    dec, why = plv2.entry_decision(feat, side, ask, bid, mv.asks(side), model, gate, lim, plv2.MarketExposure(), left, None)
    if dec is None:
        return None, {**rec, "why": why}
    info = dec.info
    rec.update({"p_win": info["p"], "ev_per_share": info["ev"], "vwap": info["vwap"], "fee_per_share": info["fee"],
                "p_favourable": info.get("p_fav"), "limit": dec.limit, "shares": dec.shares})
    if info["vwap"] - ask > prm.low_max_slippage + 1e-9:
        return None, {**rec, "why": f"VWAP {info['vwap']:.3f} is {info['vwap'] - ask:.3f} over the ask (LOW_MAX_SLIPPAGE {prm.low_max_slippage:.2f})"}
    if dec.shares * dec.limit > prm.low_max_notional + 1e-9:
        return None, {**rec, "why": f"notional {dec.shares * dec.limit:.2f} > LOW_MAX_NOTIONAL {prm.low_max_notional:.2f}"}
    rec["decision"] = "BUY"
    rec["why"] = (f"low-price first leg: {side} is the cheaper token (ask {ask:.2f} vs {mv.ask(OTHER[side]):.2f}), "
                  f"P({side}) {info['p']:.3f}, EV {info['ev']:+.3f}/sh at VWAP {info['vwap']:.3f} >= {prm.low_min_edge:+.2f}"
                  + (f", flow {rec['confirmations']}/4" if "confirmations" in rec else "") + f", {left:.0f} s left")
    return side, rec


def conditions_entry_side(mv: MarketView, prm: RecoveryParams, left: float, spot: float | None,
                          strike: float | None, held: float, rise: dict | None = None,
                          spot_move: float | None = None) -> tuple[str | None, str]:
    """THE CONDITIONS rule's first leg: (the higher-priced token, '') when every condition holds, else (None, why).
    ``rise``: {side: its mid now minus its mid cond_rising_window_s ago (None without history)} -- with the rising
    condition on, the token must be rising (more than cond_rising_min), never flat or falling."""
    if held > EPS:
        return None, f"the market holds {held:g} tokens"
    if left < prm.cond_min_left_s - 1e-9:
        return None, f"{left:.0f} s left < {prm.cond_min_left_s:g}"
    if not spot or not strike or strike <= 0.0:
        return None, "no BTC price or strike"
    d = spot - strike
    if not (prm.cond_spot_lo < d < prm.cond_spot_hi):
        return None, f"BTC - strike {d:+.2f} not in ({prm.cond_spot_lo:g}, {prm.cond_spot_hi:g})"
    if mv.ask_up is None or mv.ask_down is None:
        return None, "no two-sided book"
    if mv.ask_up == mv.ask_down:
        if mv.bid_up == mv.bid_down:
            return None, "UP and DOWN at the same price: no higher token"
        side = "UP" if (mv.bid_up or 0.0) > (mv.bid_down or 0.0) else "DOWN"
    else:
        side = "UP" if mv.ask_up > mv.ask_down else "DOWN"
    ask = mv.ask(side)
    if not (prm.cond_min_price - 1e-9 <= ask <= prm.cond_max_price + 1e-9):
        return None, f"higher token {side} ask {ask:.2f} outside {prm.cond_min_price:.2f}-{prm.cond_max_price:.2f}"
    buy = OTHER[side] if prm.cond_buy_lower else side
    bad, why_rise = rising_check(buy, rise, spot_move, prm)
    if bad:
        return None, bad
    lower = f"; buy the LOWER token {buy} at {mv.ask(buy):.2f}" if buy != side else ""
    return buy, f"BTC - strike {d:+.2f}, {side} ask {ask:.2f}{lower}{why_rise}, {left:.0f} s left"


def rising_check(side: str, rise: dict | None, spot_move: float | None, prm: RecoveryParams) -> tuple[str, str]:
    """The RISING condition (the user, 2026-10-08): ('', note) when ``side`` may be bought, else (why, ''). ``rise`` =
    {side: {"short": mid change over cond_rising_window_s, "long": over cond_rising_long_s, "last": over
    cond_rising_last_s}} (a bare number = "short"); ``spot_move`` = BTC now minus BTC the longer window ago."""
    if prm.cond_rising_window_s <= 0.0:
        return "", ""
    r = (rise or {}).get(side)
    if not isinstance(r, dict):
        r = {"short": r}
    notes = []
    for key, win, strict in (("short", prm.cond_rising_window_s, True), ("long", prm.cond_rising_long_s, True),
                             ("last", prm.cond_rising_last_s, False)):
        if win <= 0.0:
            continue
        v = r.get(key)
        if v is None:
            return f"{side}: no {win:g} s price history yet (rising condition)", ""
        if strict and v <= prm.cond_rising_min + 1e-9:
            return f"{side} is not rising: mid {v:+.3f} over {win:g} s (must be > {prm.cond_rising_min:+.3f})", ""
        if not strict and v < -1e-9:
            return f"{side} is falling: mid {v:+.3f} in the last {win:g} s", ""
        notes.append(f"{v:+.3f}/{win:g}s")
    if prm.cond_rising_spot:
        w = max(prm.cond_rising_long_s, prm.cond_rising_window_s)
        if spot_move is None:
            return f"no {w:g} s BTC history yet (rising condition)", ""
        if (spot_move <= 0.0) if side == "UP" else (spot_move >= 0.0):
            return f"BTC {spot_move:+.2f} over {w:g} s is not moving {side}'s way", ""
        notes.append(f"BTC {spot_move:+.2f}")
    return "", ", rising " + " ".join(notes)


def conditions_open_side(mv: MarketView, prm: RecoveryParams, elapsed_s: float, rise: dict | None = None,
                         spot_move: float | None = None) -> tuple[str | None, str]:
    """THE MARKET OPEN BUY: (the higher-priced token, why) within cond_open_window_s of the open with its ask in
    [cond_open_min, cond_open_max], else (None, why). With cond_buy_lower (the user, 2026-10-08: "buy the low price token
    which is rising up now at first") it is the LOWER token instead. Either way the token bought must pass the rising
    checks (2026-10-09), never flat or falling."""
    if not prm.cond_open_buy:
        return None, "market open buy off"
    if not 0.0 <= elapsed_s <= prm.cond_open_window_s + 1e-9:
        return None, f"{elapsed_s:.1f} s after the open is outside the open-buy window (first {prm.cond_open_window_s:g} s)"
    if mv.ask_up is None or mv.ask_down is None:
        return None, "no two-sided book"
    if abs(mv.ask_up - mv.ask_down) < 1e-9:
        if (mv.bid_up or 0.0) == (mv.bid_down or 0.0):
            return None, "UP and DOWN at the same price: no higher token yet"
        side = "UP" if (mv.bid_up or 0.0) > (mv.bid_down or 0.0) else "DOWN"
    else:
        side = "UP" if mv.ask_up > mv.ask_down else "DOWN"
    ask = mv.ask(side)
    if not (prm.cond_open_min - 1e-9 <= ask <= prm.cond_open_max + 1e-9):
        return None, f"open buy: higher token {side} ask {ask:.2f} outside {prm.cond_open_min:.2f}-{prm.cond_open_max:.2f}"
    band = f"{side} is the higher token, ask {ask:.2f} in {prm.cond_open_min:.2f}-{prm.cond_open_max:.2f}"
    # the user, 2026-10-09: "buy price rising tokens only at first" -- the token bought (the higher one, or the lower
    # one with cond_buy_lower) must pass the rising checks, as every other entry does
    buy = OTHER[side] if prm.cond_buy_lower else side
    bad, why_rise = rising_check(buy, rise, spot_move, prm)
    if bad:
        return None, f"open buy: {bad}"
    lower = f"; buy the LOWER token {buy} at {mv.ask(buy):.2f}" if buy != side else ""
    return buy, f"market open buy: {band}{lower}{why_rise}, {elapsed_s:.1f} s after the open"


def conditions_high_side(mv: MarketView, prm: RecoveryParams, left: float, rise: dict | None = None,
                         spot_move: float | None = None) -> tuple[str | None, str]:
    """THE HIGH BAND (the user, 2026-10-08): the higher-priced token when its ask is strictly between cond_high_min and
    cond_high_max, it passes the rising checks and >= cond_min_left_s are left. (side, why) or (None, why)."""
    if prm.cond_high_max <= 0.0:
        return None, "high band off"
    if mv.ask_up is None or mv.ask_down is None:
        return None, "no two-sided book"
    if left < prm.cond_min_left_s - 1e-9:
        return None, f"{left:.0f} s left < {prm.cond_min_left_s:g}"
    side = "UP" if mv.ask_up > mv.ask_down else "DOWN"
    ask = mv.ask(side)
    if not (prm.cond_high_min + 1e-9 < ask < prm.cond_high_max - 1e-9):
        return None, f"higher token {side} ask {ask:.2f} not in the high band ({prm.cond_high_min:.2f}, {prm.cond_high_max:.2f})"
    buy = OTHER[side] if prm.cond_high_buy_lower else side
    bad, why_rise = rising_check(buy, rise, spot_move, prm)
    if bad:
        return None, bad
    lower = f"; buy the LOWER token {buy} at {mv.ask(buy):.2f}" if buy != side else ""
    return buy, (f"high band: {side} ask {ask:.2f} in ({prm.cond_high_min:.2f}, {prm.cond_high_max:.2f}){lower}{why_rise}, "
                 f"{left:.0f} s left")


def fully_paired(cyc: RecoveryCycle) -> bool:
    """Every held token is in a pair: UP = DOWN (> 0) and the last pair bid filled in full."""
    b = cyc.book
    return (b.up_qty > EPS and abs(b.up_qty - b.down_qty) <= 1e-6 and cyc.pair is not None
            and cyc.pair.get("state") == "filled")


def entry_count(cyc: RecoveryCycle) -> int:
    return len({f.order_id for f in cyc.book.fills if f.kind == "entry"})


def pair_bid_price(prm: RecoveryParams, fill_price: float) -> float | None:
    """The other side's resting bid: pair_sum - the first leg's average fill price, rounded DOWN to the cent (the
    pair can only cost less than pair_sum). None when that leaves under one cent."""
    px = math.floor((prm.pair_sum - fill_price) * 100.0 + 1e-9) / 100.0
    return px if px >= 0.01 else None


def evaluate_late(cyc: RecoveryCycle, p_up: float | None, mv: MarketView, now: float, left: float,
                  prm: RecoveryParams, bankroll_free: float, quote_fresh: bool = True) -> tuple[RecoveryDecision | None, dict]:
    """THE LATE BUY: in the last ``late_window_s`` .. ``late_min_left_s`` seconds, buy one clip of the side the model
    gives >= ``late_min_p``, if its ask is <= ``late_max_price`` and P(win) - VWAP - fee - slippage >= ``late_min_edge``.
    Works on a market with or without a position; the clip joins the same portfolio accounting."""
    b = cyc.book
    o = outcomes(b, p_up)
    rec = {"t": now, "event": "LATE_EVAL", "state": cyc.state.value, "time_left": left, "p_up": p_up, "up_ask": mv.ask_up,
           "down_ask": mv.ask_down, "up_qty": b.up_qty, "down_qty": b.down_qty, "pnl_up_win": o["pnl_up_win"],
           "pnl_down_win": o["pnl_down_win"], "decision": "REJECTED", "why": ""}

    def reject(why):
        rec["why"] = why
        cyc.events.append(dict(rec))
        return None, rec

    if not prm.late_enabled:
        return reject("late buy off")
    if not (prm.late_min_left_s <= left <= prm.late_window_s):
        return reject(f"{left:.1f} s left is outside the late window {prm.late_window_s:g}..{prm.late_min_left_s:g} s")
    if cyc.state not in (RState.HOLDING, RState.WAITING) or b.pending:
        return reject(f"state {cyc.state.value} / order in flight")
    price_rule = prm.late_trigger == "price"
    if not quote_fresh or (p_up is None and not price_rule):
        return reject("stale data or no probability")
    if cyc.late_orders >= prm.late_max_orders:
        return reject(f"LATE_MAX_ORDERS {prm.late_max_orders} reached")
    if now - cyc.last_action_t < prm.cooldown_ms:
        return reject("cooldown")
    if price_rule:
        mids = {s: _mid(mv.bid(s), mv.ask(s)) for s in ("UP", "DOWN")}
        if mids["UP"] is None or mids["DOWN"] is None:
            return reject("no two-sided price")
        W = "UP" if mids["UP"] >= mids["DOWN"] else "DOWN"
        p_w = mids[W]
        rec.update({"winner": W, "p_win": p_w, "mid_win": p_w})
        if p_w <= prm.late_min_mid + 1e-9:
            return reject(f"{W} mid {p_w:.3f} not above LATE_MIN_MID {prm.late_min_mid:.2f}")
    else:
        W = "UP" if p_up >= 0.5 else "DOWN"
        p_w = p_up if W == "UP" else 1.0 - p_up
        rec.update({"winner": W, "p_win": p_w})
        if p_w < prm.late_min_p - 1e-12:
            return reject(f"P({W}) {p_w:.3f} < LATE_MIN_P {prm.late_min_p:.2f}")
    ask = mv.ask(W)
    if ask is None:
        return reject(f"no {W} ask")
    if ask > prm.late_max_price + 1e-9:
        return reject(f"{W} ask {ask:.2f} > LATE_MAX_PRICE {prm.late_max_price:.2f}")
    q = max(prm.late_shares, prm.min_size)
    vw, fps, used = vwap_buy(mv.asks(W), q, prm.depth_haircut)
    if vw is None:
        return reject(f"insufficient depth for {q:g} {W}")
    if vw - ask > prm.max_slippage + 1e-9:
        return reject(f"VWAP {vw:.3f} is {vw - ask:.3f} over the ask (MAX_SLIPPAGE {prm.max_slippage:.2f})")
    edge = p_w - vw - fps - prm.slippage_allowance
    cost = q * (vw + prm.slippage_allowance + fps)
    after = simulate_buy(b, W, q, vw, fps, prm.slippage_allowance, p_up)
    rec.update({"q": q, "vwap": vw, "fee_per_share": fps, "edge_per_share": edge, "cost": cost,
                "after_pnl_up_win": after["pnl_up_win"], "after_pnl_down_win": after["pnl_down_win"],
                "expected_improvement": None if p_up is None else after["expected_pnl"] - o["expected_pnl"]})
    if edge < prm.late_min_edge - 1e-12 and not price_rule:
        return reject(f"edge {edge:+.4f}/sh < LATE_MIN_EDGE {prm.late_min_edge:+.4f}")
    if prm.max_market_loss > 0.0:
        # the late buy may not leave the OTHER branch under -MAX_MARKET_LOSS: trim it to what fits, or skip it
        other_now = o["pnl_down_win"] if W == "UP" else o["pnl_up_win"]
        per = vw + fps + prm.slippage_allowance
        fit = math.floor(max(0.0, (other_now + prm.max_market_loss) / per) * 100.0) / 100.0
        if fit < q - 1e-9:
            if fit < prm.min_size - 1e-9:
                return reject(f"a late buy would leave the {OTHER[W]}-win branch under -{prm.max_market_loss:g} "
                              f"(now {other_now:+.2f}; room {fit:g} sh < {prm.min_size:g})")
            rec["trimmed_from"] = q
            q = fit
            vw, fps, used = vwap_buy(mv.asks(W), q, prm.depth_haircut)
            edge = p_w - vw - fps - prm.slippage_allowance
            cost = q * (vw + prm.slippage_allowance + fps)
            after = simulate_buy(b, W, q, vw, fps, prm.slippage_allowance, p_up)
            rec.update({"q": q, "vwap": vw, "fee_per_share": fps, "edge_per_share": edge, "cost": cost,
                        "after_pnl_up_win": after["pnl_up_win"], "after_pnl_down_win": after["pnl_down_win"],
                        "expected_improvement": None if p_up is None else after["expected_pnl"] - o["expected_pnl"]})
    if cyc.late_cost + cost > prm.late_max_capital + 1e-9:
        return reject(f"late capital {cyc.late_cost + cost:.2f} > LATE_MAX_CAPITAL {prm.late_max_capital:.2f}")
    if b.up_qty + b.down_qty + q > prm.max_total_position + 1e-9:
        return reject("MAX_TOTAL_POSITION")
    if cost > bankroll_free + 1e-9:
        return reject(f"cost {cost:.2f} > free bankroll {bankroll_free:.2f}")
    limit = min(0.99, round(ask + prm.max_slippage, 2), prm.late_max_price + prm.max_slippage)
    if max(p for p, _ in used) > limit + 1e-9:
        return reject("the clip needs a level above the limit")
    reason = (f"late buy: {'mid' if price_rule else 'P'}({W}) {p_w:.3f}, {q:g} sh"
              + (f" (trimmed from {rec['trimmed_from']:g} to MAX_MARKET_LOSS {prm.max_market_loss:g})" if "trimmed_from" in rec else "")
              + f" at VWAP {vw:.3f} (fee {fps:.4f}), edge {edge:+.4f}/sh; "
              f"UP-win {o['pnl_up_win']:+.3f} -> {after['pnl_up_win']:+.3f}, DOWN-win {o['pnl_down_win']:+.3f} -> "
              f"{after['pnl_down_win']:+.3f}, {left:.1f} s left")
    rec.update({"decision": "BUY", "limit": limit, "why": reason})
    cyc.events.append(dict(rec))
    return RecoveryDecision(W, q, limit, vw, q, reason, rec, kind="late"), rec


def evaluate_ladder(cyc: RecoveryCycle, mv: MarketView, now: float, left: float, prm: RecoveryParams,
                    bankroll_free: float, quote_fresh: bool = True) -> tuple[RecoveryDecision | None, dict]:
    """THE LATE LADDER: in a held market with ladder_min_left_s .. ladder_window_s seconds left, buy ladder_shares of the
    token with the higher ask while that ask is in [ladder_min_price, ladder_max_price], once every ladder_interval_s.
    A late order (kind "late") whose record says ``ladder``; rejections are not kept in the cycle's events (one per tick).
    ``rec["in_band"]`` tells the engine whether the rejection is worth a log line."""
    b = cyc.book
    o = outcomes(b)
    rec = {"t": now, "event": "LADDER_EVAL", "ladder": True, "state": cyc.state.value, "time_left": left,
           "up_ask": mv.ask_up, "down_ask": mv.ask_down, "up_qty": b.up_qty, "down_qty": b.down_qty,
           "pnl_up_win": o["pnl_up_win"], "pnl_down_win": o["pnl_down_win"], "ladder_orders": cyc.ladder_orders,
           "in_band": False, "decision": "REJECTED", "why": ""}

    def reject(why):
        rec["why"] = why
        return None, rec

    if prm.ladder_window_s <= 0.0:
        return reject("late ladder off")
    if not (prm.ladder_min_left_s <= left <= prm.ladder_window_s):
        return reject(f"{left:.1f} s left is outside the ladder window {prm.ladder_window_s:g}..{prm.ladder_min_left_s:g} s")
    if not cyc.holds:
        return reject("the ladder only adds to a market the bot holds")
    if cyc.state is not RState.HOLDING or b.pending:
        return reject(f"state {cyc.state.value} / order in flight")
    if not quote_fresh:
        return reject("stale data")
    if prm.ladder_max_orders > 0 and cyc.ladder_orders >= prm.ladder_max_orders:
        return reject(f"LADDER_MAX_ORDERS {prm.ladder_max_orders} reached")
    if now - cyc.ladder_last_t < prm.ladder_interval_s * 1000.0:
        return reject(f"next ladder buy {prm.ladder_interval_s:g} s after the last")
    if mv.ask_up is None or mv.ask_down is None:
        return reject("no two-sided book")
    if abs(mv.ask_up - mv.ask_down) < 1e-9:
        return reject(f"UP and DOWN asks equal ({mv.ask_up:.2f}): no higher token")
    W = "UP" if mv.ask_up > mv.ask_down else "DOWN"
    ask = mv.ask(W)
    rec.update({"winner": W, "ask": ask})
    if not (prm.ladder_min_price - 1e-9 <= ask <= prm.ladder_max_price + 1e-9):
        return reject(f"{W} ask {ask:.2f} is outside the ladder band {prm.ladder_min_price:.2f}-{prm.ladder_max_price:.2f}")
    rec["in_band"] = True
    limit = min(0.99, round(ask + prm.max_slippage, 2), round(prm.ladder_max_price, 2))
    levels = [(p, sz) for p, sz in mv.asks(W) if p <= limit + 1e-9 and sz > 0]
    q = max(prm.ladder_shares, prm.min_size)
    if prm.max_market_loss > 0.0:
        # the ladder may not leave the OTHER outcome under -MAX_MARKET_LOSS: trim the buy to what fits, or skip it
        other_now = o["pnl_down_win"] if W == "UP" else o["pnl_up_win"]
        per = limit + fee_per_share(limit) + prm.slippage_allowance
        fit = math.floor(max(0.0, (other_now + prm.max_market_loss) / per) * 100.0) / 100.0
        if fit < q - 1e-9:
            if fit < prm.min_size - 1e-9:
                return reject(f"a ladder buy would leave the {OTHER[W]}-win outcome under -{prm.max_market_loss:g} "
                              f"(now {other_now:+.2f}; room {fit:g} sh < {prm.min_size:g})")
            rec["trimmed_from"] = q
            q = fit
    if b.up_qty + b.down_qty + q > prm.max_total_position + 1e-9:
        return reject(f"MAX_TOTAL_POSITION {prm.max_total_position:g}")
    vw, fps, _used = vwap_buy(levels, q, prm.depth_haircut)
    if vw is None:
        return reject(f"insufficient depth for {q:g} {W} at or under {limit:.2f}")
    cost = q * (vw + prm.slippage_allowance + fps)
    if cost > bankroll_free + 1e-9:
        return reject(f"cost {cost:.2f} > free bankroll {bankroll_free:.2f}")
    after = simulate_buy(b, W, q, vw, fps, prm.slippage_allowance, None)
    rec.update({"q": q, "vwap": vw, "fee_per_share": fps, "cost": cost, "limit": limit, "decision": "BUY",
                "after_pnl_up_win": after["pnl_up_win"], "after_pnl_down_win": after["pnl_down_win"]})
    reason = (f"late ladder buy {cyc.ladder_orders + 1}: {W} ask {ask:.2f} in {prm.ladder_min_price:.2f}-"
              f"{prm.ladder_max_price:.2f}, {q:g} sh"
              + (f" (trimmed from {rec['trimmed_from']:g} to MAX_MARKET_LOSS {prm.max_market_loss:g})" if "trimmed_from" in rec else "")
              + f" at VWAP {vw:.3f} <= {limit:.2f}, {left:.0f} s left; UP-win {o['pnl_up_win']:+.3f} -> "
              f"{after['pnl_up_win']:+.3f}, DOWN-win {o['pnl_down_win']:+.3f} -> {after['pnl_down_win']:+.3f}")
    rec["why"] = reason
    cyc.events.append(dict(rec))
    return RecoveryDecision(W, q, limit, vw, q, reason, rec, kind="late"), rec


# ───────────────────────────────── order lifecycle ─────────────────────────────────


def on_entry_sent(cyc: RecoveryCycle, order_id: str, qty: float, limit: float, now: float) -> None:
    cyc.book.add_pending(PendingOrder(order_id, cyc.a_side, qty, limit, "entry", now))
    cyc.to(RState.INITIAL_ENTRY, now, f"FOK {qty:g} {cyc.a_side} <= {limit:.2f}")
    cyc.events.append({"t": now, "event": "INITIAL_ENTRY", "side": cyc.a_side, "qty": qty, "limit": limit})


def on_recovery_sent(cyc: RecoveryCycle, dec: RecoveryDecision, order_id: str, now: float) -> None:
    """A recovery OR late order leaves (dec.kind); pending is NOT inventory."""
    cyc.book.add_pending(PendingOrder(order_id, dec.side, dec.shares, dec.limit, dec.kind, now))
    ladder = dec.kind == "late" and bool(dec.record.get("ladder"))
    if ladder:
        cyc.ladder_orders += 1
        cyc.ladder_last_t = now
    elif dec.kind == "late":
        cyc.late_orders += 1
    elif dec.kind == "lock":
        cyc.lock_steps += 1
        cyc.lock_reason = cyc.lock_reason or str(dec.record.get("lock_reason") or "")
    elif dec.kind != "ulr":                      # the unfilled-leg recovery keeps its own counters (pairlock_ulr)
        cyc.actions += 1
    cyc.last_action_t = now
    cyc.pending = {"oid": order_id, "side": dec.side, "q": dec.shares, "plan_qty": dec.plan_qty, "vwap": dec.vwap,
                   "filled": 0.0, "before": outcomes(cyc.book), "kind": dec.kind, "ladder": ladder}
    cyc.to(RState.RECOVERY_PENDING, now, f"FOK {dec.shares:g} {dec.side} <= {dec.limit:.2f}")
    cyc.events.append({"t": now, "event": "LADDER_SENT" if ladder else
                       {"late": "LATE_SENT", "lock": "LOCK_SENT", "ulr": "ULR_SENT", "hedge": "HEDGE_SENT"}.get(dec.kind, "RECOVERY_SENT"), "order_id": order_id,
                       "side": dec.side, "qty": dec.shares, "plan_qty": dec.plan_qty, "limit": dec.limit, "vwap": dec.vwap})


def on_fill(cyc: RecoveryCycle, fill: FillRecord, now: float) -> None:
    """Every venue fill. Inventory and both branches update at once; a partial fill counts what filled."""
    before = outcomes(cyc.book)
    cyc.book.add_fill(fill)
    after = outcomes(cyc.book)
    if fill.kind == "entry":
        cyc.entry_qty += fill.qty
        if cyc.state is RState.INITIAL_ENTRY:
            cyc.to(RState.HOLDING, now, f"entry filled {fill.qty:g} @ {fill.price:.3f}")
        cyc.events.append({"t": now, "event": "ENTRY_FILLED", "side": fill.side, "qty": fill.qty, "price": fill.price,
                           "fee": fill.fee, **{k: after[k] for k in ("pnl_up_win", "pnl_down_win", "min_settlement_pnl")}})
        return
    if fill.kind == "ulr":
        if cyc.pending is not None:
            cyc.pending["filled"] = cyc.pending.get("filled", 0.0) + fill.qty
        cyc.events.append({"t": now, "event": "ULR_FILLED", "side": fill.side, "qty": fill.qty, "price": fill.price,
                           "fee": fill.fee, **{k: after[k] for k in ("pnl_up_win", "pnl_down_win", "min_settlement_pnl")}})
        return
    if fill.kind == "late_hedge":
        if cyc.late_hedge is not None:
            cyc.late_hedge["filled"] = float(cyc.late_hedge.get("filled") or 0.0) + fill.qty
        cyc.late_cost += fill.gross_cost + fill.fee
        cyc.events.append({"t": now, "event": "LATE_HEDGE_FILLED", "side": fill.side, "qty": fill.qty, "price": fill.price,
                           "fee": fill.fee, **{k: after[k] for k in ("pnl_up_win", "pnl_down_win", "min_settlement_pnl")}})
        return
    if fill.kind == "pair":
        if cyc.pair is not None:
            cyc.pair["filled"] = float(cyc.pair.get("filled") or 0.0) + fill.qty
        cyc.events.append({"t": now, "event": "PAIR_FILLED", "side": fill.side, "qty": fill.qty, "price": fill.price,
                           "fee": fill.fee, **{k: after[k] for k in ("pnl_up_win", "pnl_down_win", "min_settlement_pnl")}})
        if cyc.state is RState.WAITING:
            cyc.to(RState.HOLDING, now, "pair filled")
        return
    ladder = fill.kind == "late" and bool((cyc.pending or {}).get("ladder"))
    if ladder:
        cyc.ladder_cost += fill.gross_cost + fill.fee
    elif fill.kind == "late":
        cyc.late_cost += fill.gross_cost + fill.fee
    else:
        cyc.recovery_cost += fill.gross_cost + fill.fee
    if fill.kind == "recovery" and not (cyc.rec_open and fill.side == cyc.rec_side):
        cyc.rec_episodes += 1                # a new recovery: counted when something actually filled
        cyc.rec_open, cyc.rec_side = True, fill.side
    if cyc.pending is not None:
        cyc.pending["filled"] = cyc.pending.get("filled", 0.0) + fill.qty
    if cyc.state is RState.WAITING:
        cyc.to(RState.HOLDING, now, f"late buy filled {fill.qty:g} {fill.side} @ {fill.price:.3f}")
    row = {"t": now, "event": "LADDER_FILLED" if ladder else
           {"late": "LATE_FILLED", "lock": "LOCK_FILLED", "hedge": "HEDGE_FILLED"}.get(fill.kind, "RECOVERY_FILLED"), "side": fill.side, "qty": fill.qty, "price": fill.price, "fee": fill.fee,
           "slippage": fill.slippage, "up_qty": cyc.book.up_qty, "down_qty": cyc.book.down_qty,
           "up_avg": cyc.book.up_avg_cost, "down_avg": cyc.book.down_avg_cost, "total_cost": cyc.book.total_cost,
           "pnl_up_win_before": before["pnl_up_win"], "pnl_down_win_before": before["pnl_down_win"],
           "pnl_up_win": after["pnl_up_win"], "pnl_down_win": after["pnl_down_win"],
           "min_before": before["min_settlement_pnl"], "min_after": after["min_settlement_pnl"]}
    if fill.kind == "late":
        cyc.events.append(dict(row))
        return
    cyc.recoveries.append(row)
    cyc.events.append(dict(row))


def on_order_done(cyc: RecoveryCycle, order_id: str, filled_any: bool, now: float, why: str = "") -> None:
    p = cyc.book.pending.pop(order_id, None)
    if p is None:
        return
    if p.kind == "entry":
        if not filled_any and cyc.state is RState.INITIAL_ENTRY:
            cyc.to(RState.HOLDING if cyc.holds else RState.WAITING, now, f"entry not filled ({why})")
            cyc.events.append({"t": now, "event": "ENTRY_NOT_FILLED", "why": why})
        return
    ph = cyc.pending or {}
    cyc.pending = None
    filled = float(ph.get("filled", 0.0))
    cyc.events.append({"t": now, "event": "LADDER_DONE" if ph.get("ladder") else
                       {"late": "LATE_DONE", "lock": "LOCK_DONE", "hedge": "HEDGE_DONE"}.get(ph.get("kind"), "RECOVERY_DONE"),
                       "order_id": order_id, "requested": ph.get("q"),
                       "filled": filled, "why": why if not filled_any else "",
                       "partial": filled_any and filled < float(ph.get("q", 0.0)) - 1e-9})
    if cyc.state is RState.RECOVERY_PENDING:
        cyc.to(RState.HOLDING if cyc.holds else RState.WAITING, now, "order done" if filled_any else f"not filled ({why})")


def on_close(cyc: RecoveryCycle, now: float) -> bool:
    if not cyc.holds:
        return False
    if cyc.state not in (RState.SETTLEMENT_PENDING, RState.SETTLED):
        cyc.to(RState.SETTLEMENT_PENDING, now, "market closed")
    return True


def reconstruct_pnl(fills, winner: str) -> float:
    cash = sum(-(f.qty * f.price + f.fee) if f.action == "BUY" else (f.qty * f.price - f.fee) for f in fills)
    held = sum(f.qty * (1 if f.action == "BUY" else -1) for f in fills if f.side == winner)
    return held + cash


def ulr_leg_pnl(fills, winner: str) -> float:
    """The unfilled-leg recovery's own result: its sells' proceeds, what it still held paid at settlement, minus what
    its buys cost (fees in). Zero for a market without one."""
    bought = [f for f in fills if f.kind == "ulr"]
    sold = [f for f in fills if f.kind == "ulr_exit"]
    if not bought:
        return 0.0
    remaining = {s: sum(f.qty for f in bought if f.side == s) - sum(f.qty for f in sold if f.side == s) for s in ("UP", "DOWN")}
    return (sum(f.gross_cost - f.fee for f in sold) + max(0.0, remaining.get(winner, 0.0))
            - sum(f.gross_cost + f.fee for f in bought))


def on_settled(cyc: RecoveryCycle, winner: str, now: float) -> dict:
    cyc.settled_winner = winner
    if cyc.state is not RState.SETTLEMENT_PENDING:
        on_close(cyc, now)
    cyc.to(RState.SETTLED, now, f"{winner} won")
    b = cyc.book
    o = outcomes(b)
    a = cyc.a_side
    late = [f for f in b.fills if f.kind in ("late", "late_hedge")]
    late_leg = sum(f.qty * (1.0 if f.side == winner else 0.0) - f.gross_cost - f.fee for f in late)
    pair_leg = sum(f.qty * (1.0 if f.side == winner else 0.0) - f.gross_cost - f.fee for f in b.fills if f.kind == "pair")
    first_leg = (sum(f.qty * (1.0 if f.side == winner else 0.0) - f.gross_cost - f.fee for f in b.fills if f.kind == "entry"))
    ulr_leg = ulr_leg_pnl(b.fills, winner)
    r = {"winner": winner, "pnl": b.resolution_pnl(winner), "pnl_from_cash": b.final_pnl_from_cash(winner),
         "ulr_leg_pnl": ulr_leg, "ulr_entries": len({f.order_id for f in b.fills if f.kind == "ulr"}),
         "late_leg_pnl": late_leg, "late_fills": len(late), "pair_leg_pnl": pair_leg,
         "pair_filled": sum(f.qty for f in b.fills if f.kind == "pair"),
         "pnl_reconstructed": reconstruct_pnl(b.fills, winner), "first_leg_pnl": first_leg,
         "recovery_effect": b.resolution_pnl(winner) - first_leg - late_leg - pair_leg - ulr_leg, "recovered": bool(cyc.recoveries),
         "reached_target": b.resolution_pnl(winner) >= cyc.target - 1e-9, **o}
    cyc.events.append({"t": now, "event": "SETTLED", **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()}})
    return r


def summary_row(cyc: RecoveryCycle) -> dict:
    """The sequence table's row for a settled market: entry, every recovery fill, both branches, the result."""
    b = cyc.book
    return {"side": cyc.a_side, "entry_qty": cyc.entry_qty, "up_qty": b.up_qty, "down_qty": b.down_qty,
            "up_avg": b.up_avg_cost, "down_avg": b.down_avg_cost, "total_cost": b.total_cost, "fees": b.fees,
            "recoveries": [(r["side"], r["qty"], r["price"]) for r in cyc.recoveries],
            "pnl_up_win": b.resolution_pnl("UP"), "pnl_down_win": b.resolution_pnl("DOWN"),
            "winner": cyc.settled_winner, "pnl": None if cyc.settled_winner is None else b.resolution_pnl(cyc.settled_winner)}


