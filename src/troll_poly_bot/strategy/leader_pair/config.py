"""LEADER_PAIR configuration: every strategy parameter of the user's 2026-10-13 spec ("Leader Tracking, Pair
Formation, and Recovery Accumulation"), configurable through ``TPB_LP_<FIELD>`` (see ``SpotLagConfig.from_env``).

The spec's parameters (section 3) and where they live:

    Initial purchase quantity (10)                      init_shares
    Pair acquisition-cost threshold ($0.95)             pair_threshold
    Accumulation interval (5 s)                         accum_interval_s
    Fixed quantity per additional purchase              accum_shares / falling_shares / recovery_shares
    Opposite-token reference price (1 - P0)             derived at the initial fill (p_ref_includes_fee)
    Opposite-token accumulation ceiling ($0.50)         opp_ceiling
    Leader reversal threshold ($0.50)                   leader_reversal
    Maximum total capital deployed                      max_market_capital_usdc / max_total_capital_usdc
    Maximum additional purchases per cycle              max_purchases_per_cycle (+ max_purchases_per_market)
    Maximum acceptable settlement loss                  max_settlement_loss_usdc
    Recovery stop condition (PnL_live > 0)              recovery_stop_pnl (strictly greater)
    Maximum spread / slippage / minimum liquidity       max_spread / max_slippage / min_depth_shares
    Minimum time remaining before expiry                min_time_remaining_s

Assumptions the spec leaves open are marked ASSUMPTION; docs/leader_pair_2026-10-13.md lists them all.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..book_momentum import BookMomentumConfig

ENABLE_ENV = "TPB_LP_ENABLED"
STRATEGY_ID = "leader_pair"
VENUE_MIN_SHARES = 5.0


@dataclass(slots=True)
class LeaderPairConfig(BookMomentumConfig):
    ENV_PREFIX = "TPB_LP_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}
    market_type: str = "5m"
    assets: str = "BTC"
    exclude_assets: str = ""
    hold_to_settlement: bool = True

    # ---- Cross-market leader / pair mode (user, 2026-10-19) -----------------------------------------------
    # The matching outcome's executable ask in each of the other five configured
    # crypto markets is the peer price.  This mode is deliberately separate from
    # the original per-market state machine below.
    cross_market_enabled: bool = False
    cross_peer_count: int = 5
    cross_start_s: float = 0.0
    cross_end_s: float = 240.0              # no new buys in the final minute
    cross_leader_shares: float = 5.0
    cross_leader_min_ask: float = 0.30
    cross_leader_max_ask: float = 0.60
    cross_peer_leader_avg: float = 0.88
    cross_pair_rise: float = 0.30           # measured from the filled leader price
    cross_pair_shares: float = 5.0
    cross_peer_signal_avg: float = 0.90
    cross_signal_min_ask: float = 0.50
    cross_signal_max_ask: float = 0.60
    cross_signal_shares: float = 5.0

    # ---- Rule 1: the initial leader --------------------------------------------------------------------------
    init_shares: float = 10.0
    #: ASSUMPTION: the leader is the token with the higher MID at the entry moment ("mid" | "ask" | "bid")
    leader_rule: str = "mid"
    #: ASSUMPTION: an exact tie -> no entry ("skip") | "up" | "down"
    leader_tie: str = "skip"
    #: the entry is taken this long after the open (the open's first prints are noise) and no later than init_max_s
    init_delay_s: float = 5.0
    init_max_s: float = 60.0
    #: a rejected / unfilled initial order is re-sent this many times (each after init_retry_s)
    init_max_tries: int = 3
    init_retry_s: float = 2.0
    #: ASSUMPTION: a PARTIAL initial fill is kept as it is (P0 from what filled); True = top up to init_shares
    init_top_up: bool = False
    #: the leader must be bought inside this executable-ask band
    init_min_ask: float = 0.50
    init_max_ask: float = 0.90
    #: ASSUMPTION: P_ref = 1 - P0 where P0 is the average FILL PRICE; True adds the fee per share to P0
    p_ref_includes_fee: bool = False

    # ---- Rule 2: pairs ---------------------------------------------------------------------------------------
    pair_threshold: float = 0.95
    #: FIFO lot matching (the documented method); "lifo" is offered for the sensitivity study only
    pair_matching: str = "fifo"

    # ---- Rule 3: opposite-token accumulation every 5 s ------------------------------------------------------
    accum_interval_s: float = 5.0
    accum_shares: float = 5.0            # the venue's 5-share minimum; the spec leaves q configurable
    opp_ceiling: float = 0.50
    #: ASSUMPTION: one decision per 5-s slot, taken at the FIRST tick of the slot ("slot_start"); "any_tick" buys
    #: at most once per slot but on whichever tick inside it first satisfies the conditions
    accum_eval: str = "slot_start"
    #: ASSUMPTION: after the opposite rose above the ceiling (the rule stopped) and came back under it, a NEW
    #: accumulation sequence starts with the previous-fill reference reset (Rule 6 "repeat"); False = one sequence
    accum_resequence: bool = True
    max_accum_sequences: int = 3

    # ---- Rule 4: the leader falls under the reversal threshold ----------------------------------------------
    leader_reversal: float = 0.50
    #: the observation interval delta_t and the noise filter (a fall must be at least this many USDC)
    signal_delta_s: float = 5.0
    fall_min_drop: float = 0.01
    #: ... confirmed on this many consecutive signal evaluations (spaced signal_tick_ms apart)
    fall_confirm_ticks: int = 3
    signal_tick_ms: float = 250.0
    #: the signal price: "mid" | "bid" | "ask"
    signal_price: str = "mid"
    #: no signal evaluation while the leader's spread is wider than this (a spread change is not a trend)
    max_signal_spread: float = 0.04
    #: ASSUMPTION: in LEADER_FALLING the opposite token is bought (fixed size, the accumulation cadence) when the buy
    #: raises the WORST settlement outcome and its ask is at most falling_opp_max_ask; "none" buys nothing
    falling_buy: str = "improve_worst"
    falling_shares: float = 5.0
    falling_opp_max_ask: float = 0.65

    # ---- Rule 5: recovery ------------------------------------------------------------------------------------
    recovery_min_rise: float = 0.01
    recovery_confirm_ticks: int = 3
    recovery_interval_s: float = 5.0
    recovery_shares: float = 5.0
    #: ASSUMPTION: recovery buys the LEADER token at an ask up to this (the spec sets no cap; the price band still holds)
    recovery_max_ask: float = 0.95
    recovery_stop_pnl: float = 0.0       # stop once PnL_live > this (strictly)
    #: the conservative liquidation estimate: the venue's taker fee at the bid plus this slippage per share
    liq_slippage: float = 0.01

    # ---- the favourite fix (the user, 2026-10-13: "if the price is in 0.6-0.8 and the settlement PnL of the leader is
    #      negative, buy leader tokens and make the settlement PnL +$5"; the lower-priced token's rules untouched) ----
    fix_enabled: bool = True
    fix_min_ask: float = 0.50            # the higher-priced token's ask must be inside [fix_min_ask, fix_max_ask]
    fix_max_ask: float = 0.80
    fix_target_usdc: float = 5.0         # ... then it is bought until the P&L if it wins reaches this
    fix_cooldown_s: float = 2.0          # between fix orders (a cut fix is bought again while the outcome stays negative)
    #: the user 10-14 ("do not buy high price tokens ... before 210 s left or above 0.85; buy only when the token rises
    #: from 0.5 to 0.8"): the fix runs only with at most fix_max_left_s left and only while the favourite is RISING
    #: (its mid above its mid fix_rise_window_s ago by fix_min_rise); the band is fix_min_ask..fix_max_ask (0.50..0.80)
    fix_max_left_s: float = 210.0
    fix_require_rising: bool = True
    fix_rise_window_s: float = 5.0
    fix_min_rise: float = 0.01
    #: the user 10-14 ("if the lower token's price is low, reduce the worst-case loss by buying lower tokens, but do not
    #: make the best-case PnL under +2"): the lower-priced token with its ask at most hedge_max_ask is bought, when the
    #: worst outcome is negative, up to the size that keeps the best outcome at hedge_min_best_usdc (never past the
    #: point where both outcomes are equal)
    hedge_enabled: bool = True
    hedge_max_ask: float = 0.20
    hedge_min_best_usdc: float = 2.0


    # ---- the pair buy and the profit lock (the user, 2026-10-14: "if buying a certain token can make pairs under 0.95
    #      in total, calculate the amount and buy it; if the locked PnL is over $5, lock buying") ---------------------
    pair_buy_enabled: bool = True        # the side with unmatched shares is completed into pairs costing <= pair_threshold
    pair_buy_cooldown_s: float = 2.0
    lock_target_usdc: float = 5.0        # once min(PnL_if_Up, PnL_if_Down) >= this the market is LOCKED: no more buys (0 = off)

    # ---- execution --------------------------------------------------------------------------------------------
    exec_mode: str = "taker"             # execution is taker-only; every order is fill-and-kill (FAK)

    # ---- hard risk controls (section 11) --------------------------------------------------------------------
    max_market_capital_usdc: float = 150.0
    max_total_capital_usdc: float = 500.0
    max_purchases_per_cycle: int = 6
    max_purchases_per_market: int = 20
    max_settlement_loss_usdc: float = 20.0
    max_spread: float = 0.03
    max_slippage: float = 0.02           # the IOC limit above the seen ask, and the VWAP tolerance
    min_depth_shares: float = 5.0        # at or under the limit
    min_time_remaining_s: float = 20.0
    max_time_remaining_s: float = 300.0
    max_book_age_ms: float = 2500.0
    min_trade_price: float = 0.05
    max_trade_price: float = 0.95
    #: no new order on a token this long after one ended unfilled (its fill may still arrive)
    end_guard_s: float = 2.0

    # ---- the shared budget the risk manager sees (SpotLagConfig.apply_budget) ------------------------------
    max_total_exposure_usdc: float = 180.0
    max_daily_loss_usdc: float = 150.0
    max_order_usdc: float = 150.0
    max_simultaneous_positions: int = 4
    max_buys_per_hour: int = 2000
    max_entries_per_market: int = 10000
    max_attempts_per_market: int = 10000

    def validate(self) -> list[str]:
        """Configuration errors (an empty list = valid). The engine refuses to start on any."""
        bad = []
        for name in ("init_shares", "accum_shares", "falling_shares", "recovery_shares",
                     "cross_leader_shares", "cross_pair_shares", "cross_signal_shares"):
            if getattr(self, name) < VENUE_MIN_SHARES - 1e-9:
                bad.append(f"{name} {getattr(self, name):g} is under the venue's {VENUE_MIN_SHARES:g}-share minimum")
        if not 0.0 < self.pair_threshold <= 1.0:
            bad.append("pair_threshold must be in (0, 1]")
        if self.accum_interval_s <= 0 or self.recovery_interval_s <= 0:
            bad.append("accumulation / recovery intervals must be positive")
        if self.leader_rule not in ("mid", "ask", "bid"):
            bad.append(f"leader_rule {self.leader_rule!r}")
        if self.leader_tie not in ("skip", "up", "down"):
            bad.append(f"leader_tie {self.leader_tie!r}")
        if self.accum_eval not in ("slot_start", "any_tick"):
            bad.append(f"accum_eval {self.accum_eval!r}")
        if self.falling_buy not in ("improve_worst", "none"):
            bad.append(f"falling_buy {self.falling_buy!r}")
        if self.pair_matching not in ("fifo", "lifo"):
            bad.append(f"pair_matching {self.pair_matching!r}")
        if self.signal_price not in ("mid", "bid", "ask"):
            bad.append(f"signal_price {self.signal_price!r}")
        if self.max_settlement_loss_usdc <= 0 or self.max_market_capital_usdc <= 0 or self.max_total_capital_usdc <= 0:
            bad.append("loss / capital limits must be positive")
        if self.exec_mode != "taker":
            bad.append("exec_mode must be 'taker'; leader_pair uses taker-only fill-and-kill orders")
        if not 0.0 < self.fix_min_ask <= self.fix_max_ask < 1.0:
            bad.append("fix band")
        if self.max_purchases_per_cycle < 0 or self.max_purchases_per_market < 1:
            bad.append("purchase caps")
        if self.cross_peer_count < 1:
            bad.append("cross_peer_count must be positive")
        if not (0.0 <= self.cross_start_s < self.cross_end_s <= 300.0):
            bad.append("cross-market trading window")
        if not (0.0 < self.cross_leader_min_ask <= self.cross_leader_max_ask < 1.0):
            bad.append("cross-market leader price band")
        if not (0.0 < self.cross_signal_min_ask <= self.cross_signal_max_ask < 1.0):
            bad.append("cross-market signal price band")
        if self.cross_pair_rise <= 0.0:
            bad.append("cross_pair_rise must be positive")
        return bad
