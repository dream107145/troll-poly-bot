# LEADER_PAIR -- leader tracking, pair formation, recovery accumulation (2026-10-13)

The user's spec of 2026-10-13 ("Build a Polymarket Up/Down automated trading bot based on leader tracking, pair
formation, and recovery accumulation"), built as its own strategy inside this repo: `strategy/leader_pair/`,
pm2 app `tpb-leaderpair-paper` (`ecosystem.leaderpair.config.js`), data tag `leaderpair-paper`, env prefix `TPB_LP_`.
**Paper only.** The earlier implementation lost $1.23 per market in its 4,070-market BTC replay (22 Sep -> 13 Oct 2026).
The portfolio-aware decision and hard-limit changes described here have not been backtested; the old result is not
evidence for this revision, and nothing supports real money.

## 1. The strategy as a state machine

One instance per market (`core.MarketStrategy`). It sees only the quotes of the current tick and returns at most
one order intent per tick; fills and order ends come back through callbacks. The paper bot and the backtester run
the same object.

| From | Condition (checked every tick) | To | Side effects |
|---|---|---|---|
| INITIAL_ENTRY | `init_delay_s` after the open, both books fresh, leader by `leader_rule` (higher mid), its ask in `[init_min_ask, init_max_ask]`, risk checks pass | (order sent) | 10-share IOC buy of the leader at ask + `max_slippage` |
| INITIAL_ENTRY | the initial order filled (fully, or partially with `init_top_up` off) | MONITORING | P0 = average fill price, P_ref = 1 - P0, the 5-s schedule anchored at the fill |
| INITIAL_ENTRY | no fill after `init_max_tries` tries or `init_max_s` | NO_ENTRY (terminal) | nothing held |
| MONITORING / RECOVERY_STOPPED | P_ref < ask_opp <= `opp_ceiling` (0.50) | OPPOSITE_ACCUMULATION | a new accumulation sequence; its first decision at the next 5-s boundary |
| OPPOSITE_ACCUMULATION | each 5-s slot: ask_opp in range **and** ask_opp > previous fill of this sequence, risk checks pass | (order sent) | q = `accum_shares` IOC buy of the opposite |
| OPPOSITE_ACCUMULATION | ask_opp > `opp_ceiling` | MONITORING | the sequence ends ("stop this particular accumulation rule"); a later return into range starts sequence n+1 (`accum_resequence`, max `max_accum_sequences`) |
| MONITORING / OPPOSITE_ACCUMULATION / RECOVERY_STOPPED | leader signal price < `leader_reversal` (0.50) **and** <= price `signal_delta_s` ago - `fall_min_drop`, on `fall_confirm_ticks` consecutive evaluations | LEADER_FALLING | cycle += 1, the cycle's purchase counter reset; inventory and both settlement outcomes recorded (FALLING_LEADER event) |
| LEADER_FALLING | each slot (`falling_buy = improve_worst`): opposite ask <= `falling_opp_max_ask`, the buy raises min(PnL_if_Up, PnL_if_Down), risk checks pass | (order sent) | `falling_shares` IOC buy of the opposite |
| LEADER_FALLING | leader signal price >= price `signal_delta_s` ago + `recovery_min_rise`, on `recovery_confirm_ticks` consecutive evaluations | RECOVERY | the recovery schedule anchored here |
| RECOVERY | **every tick**: PnL_live > `recovery_stop_pnl` (0, strictly) | RECOVERY_STOPPED | RECOVERY_STOP event; no further recovery buys |
| RECOVERY | each slot, PnL_live <= 0 (re-checked before the order), leader ask <= `recovery_max_ask`, risk checks pass | (order sent) | `recovery_shares` IOC buy of the leader |
| RECOVERY / RECOVERY_STOPPED | the falling signal again | LEADER_FALLING | cycle += 1 (Rule 6: repeat) |
| any | a market-wide cap is used up (`max_market_capital_usdc`, `max_purchases_per_market`) | RISK_LIMIT_STOPPED (terminal) | holds to settlement |
| any | close_ts reached | MARKET_CLOSED (terminal) | settles on the venue's outcome |

Signal evaluation (`core._sample`): the leader's `signal_price` (mid) sampled every `signal_tick_ms` (250 ms) while the
book is fresh and the spread <= `max_signal_spread` (a spread blow-out is not a price move); "delta_t ago" is the
latest sample at least `signal_delta_s` (5 s) old. One tick never confirms anything: 3 consecutive evaluations do.

Timers: the accumulation slots are `floor((now - initial fill) / 5 s)`; one decision per slot (`accum_eval =
slot_start`: taken on the first tick of the slot; `any_tick`: at most one buy per slot, on the first tick inside it
that qualifies). The falling-buy and recovery slots are anchored at the entry into their state and start at once.
Duplicate data messages cannot buy twice: a slot already decided is skipped, and only one order is ever in flight
per market. Nothing resets inventory, cost or the market's purchase count.

### Ambiguities in the spec and the assumptions taken (all configurable)

| # | Ambiguity | Assumption (config) |
|---|---|---|
| A1 | "leader-selection rule" | the token with the higher **mid** at `init_delay_s` = 5 s after the open (`leader_rule`); an exact tie = no entry (`leader_tie`) |
| A2 | P0 with a partial / multi-level initial fill | P0 = quantity-weighted average **fill price** of what filled, fee excluded (`p_ref_includes_fee=0`); a partial is kept as it is (`init_top_up=0`), the schedule starts at the first fill |
| A3 | is the "leader" ever re-chosen? | no: the initial leader stays the leader for the market; the opposite is the other token |
| A4 | the fixed quantity per additional purchase | 5 shares (the venue minimum) for accumulation, falling and recovery buys (`accum_shares`, `falling_shares`, `recovery_shares`) |
| A5 | what "stop this accumulation rule" means for Rule 6 | the sequence ends; a new sequence (previous-fill reference reset) may start when the ask returns to (P_ref, 0.50] (`accum_resequence`, `max_accum_sequences` = 3) |
| A6 | Rule 4 step 5 "whether additional opposite-token purchases would improve the risk profile" | buy the opposite only if it raises the worst settlement outcome, at an ask <= 0.65 (`falling_buy`, `falling_opp_max_ask`); `none` turns it off |
| A7 | "cycle" for the per-cycle purchase cap | from the initial entry until the first LEADER_FALLING (cycle 0), then one cycle per LEADER_FALLING entry; the counter resets there |
| A8 | the recovery purchase price | no cap beyond the price band (`recovery_max_ask` = 0.95); the sensitivity set `rec055` caps it at 0.55 |
| A9 | PnL_live "including expected liquidation fees and slippage" | the venue's taker fee at the bid price plus `liq_slippage` = 1c a share; with bid depth known, shares the book cannot absorb count zero; a held side with no bid makes PnL_live unknown and no recovery order is sent |
| A10 | matched vs. unmatched when a pair does not qualify | pairs are formed FIFO regardless and carry their actual cost and a `qualifies` flag; "matched-pair count" in the report = qualifying pairs, "formed" = all |
| A11 | Rule 3 pairs can never qualify | by construction P0 + ask_opp > P0 + (1 - P0) = 1 > 0.95: Rule-3 lots only form qualifying pairs with LATER cheaper lots (recovery buys of the fallen leader). Stated, not changed. |
| A12 | where the strategy runs | paper only; shadow, micro-live, and live are blocked until the revised combined strategy has positive cost-inclusive out-of-sample evidence. |

### The favourite fix (the user, 2026-10-13 evening; both paper bots)

"The final goal is profit after settlement ... if the price is in 0.6-0.8 and the settlement PnL of the leader is
negative, buy leader tokens and make the settlement PnL +$5. Do not change the lower-price token purchase behaviour."
Built identically into this bot (`core._fix`, kind `fix`) and the rising-recovery bot's opt mode
(`rising_recovery._opt_fix`, kind `opt_fix`): every tick, before any other rule, the HIGHER-priced token (by mid) whose
ask lies in `[fix_min_ask, fix_max_ask]` (0.60..0.80) and whose settlement P&L should it win is negative is bought
with `n = ceil((target - PnL_if_it_wins) / (1 - ask - fee))` shares, target `fix_target_usdc` = 5 (rising recovery:
`high_win_target_usdc`). The hard limits still apply: the order is cut to what the capital / settlement-loss caps allow
and bought again (after the five-second minimum interval) while the outcome stays negative. This adjustment targets
only one settlement branch; it is not the portfolio +$5 profit lock, which requires both branches to be at least +$5.
The full target must be achievable within limits or the buy is refused. The fix counts toward the six-purchase cycle cap.
Dashboard rows: `lp.fix_*` and `rtr.fix_*` / `rtr.high_win_target_usdc` (the band is per bot). Not backtested:
the 6.1 figures predate it; the paper bots were restarted fresh 21:27 UTC with it.

### The fix's gates and the cheap-token hedge (the user, 2026-10-14; both paper bots)

The favourite fix now runs only with at most `fix_max_left_s` (210 s) left, only while the favourite is RISING (its
mid above its mid `fix_rise_window_s` = 5 s ago by `fix_min_rise` = 1c) and only inside the band 0.50..0.80 (so
never above 0.85); the target stays +5. New: the cheap-token hedge -- when the lower-priced token's ask is at most
`hedge_max_ask` (0.20) and the worst settlement outcome is negative, it is bought up to the size that keeps the best
outcome at `hedge_min_best_usdc` (+2) and never past the point where both outcomes are equal (`core._hedge`, kind
`hedge`; rising recovery `_opt_hedge`, kind `opt_hedge`, `opt_hedge_*` / `hedge_max_price`). Both are exempt from
the per-cycle purchase cap and cut by the hard limits like the fix. Rows: `lp.fix_max_left_s`, `lp.fix_require_rising`,
`lp.fix_rise_*`, `lp.hedge_*`; `rtr.fix_max_left_s`, `rtr.fix_require_rising`, `rtr.fix_rise_*`, `rtr.opt_hedge_*`,
`rtr.hedge_max_price`.


### The pair buy and the profit lock (the user, 2026-10-14; both paper bots)

Checked on every pass before every other rule. **Pair buy**: the token opposite the side holding unmatched shares
is bought when, lot by lot in FIFO order, the lot's fee-inclusive unit cost + this ask + its fee is at most the
pair threshold (0.95); the amount is the number of shares that completes those pairs (cut by the hard limits and the
depth, bought again next pass while lots remain). **Lock**: once the worst settlement outcome, min(PnL if UP, PnL if
DOWN), reaches `lock_target_usdc` (+5) the market is LOCKED (a terminal state: held to settlement, no buys of any
kind). Rising recovery: `_opt_pair` (kind `opt_pair`, `opt_pair_*`) and `target_profit_usdc` = 5 as the lock in opt
mode (`_done`). Rows: `lp.pair_buy_enabled`, `lp.pair_buy_cooldown_s`, `lp.lock_target_usdc`; `rtr.opt_pair_enabled`,
`rtr.opt_pair_threshold`, `rtr.target_profit_usdc`.

### Rising-recovery momentum phase, as of 2026-10-14 (its bot only)

Zone 0.15..0.80, the LOWER-priced token only: bought (5 sh) the moment it starts rising, then on every 3c rise of its
mid; 3 s after every buy (and again every 3 s while the check keeps passing) the mid must be at least 1c above the
last check, else the top-side token is bought in the same size as the last momentum buy (a pair) and the phase
ends; a 2c reversal from the peak ends it the same way. `rtr.opt_mom_*` on the Variables page.

### Execution (revised)

All leader-pair orders are taker-only fill-and-kill (FAK) orders, translated from the shared `IOC` order type, with a
strict maximum price. There are no maker or speed-based execution options in this strategy. Each order is rechecked
against current data and risk gates; three consecutive unfilled orders stop the market.

## 2. Accounting (`accounting.py`)

Every fill is a **lot** (price, size, fee, time, order id, kind). Totals per side: Q (shares), C (fee-inclusive
cost), plus cash spent, fees, purchases. Pairs are formed FIFO after every fill (`lifo` for the study only); each
share is in at most one pair (`Lot.remaining`); a pair's cost is the two lots' fee-inclusive unit costs, and it
**qualifies** iff cost <= `pair_threshold` (0.95). Gross settlement margin per pair = 1 - cost.

Settlement identities, with C_total the cash paid for every share including fees and no exits:

    PnL_if_Up_wins   = Q_up - C_total
    PnL_if_Down_wins = Q_down - C_total

Proof: a winning share pays exactly 1 USDC and a losing one 0; no other cash moves after the last fill, so the
cash at resolution is the payout minus everything paid. Decomposed by lots: PnL_if_S_wins = sum over pairs
(1 - C_pair) + (unmatched shares of S) - (cost of all unmatched shares); `test_settlement_pnl_identities...`
checks the two forms agree. The pair margin is locked (the same shares pay whoever wins); the unmatched inventory
is the directional exposure. A positive mark-to-market is never a guarantee of settlement profit: the two numbers
are reported apart everywhere (snapshot, [LP] events, the backtest rows).

Conservative liquidation: `PnL_live = sum Q_side * (bid - fee(bid) - liq_slippage) - C_total`, walked down the bid
depth when known.

## 3. Risk controls (`risk.py`) -- before every order, in this order

MARKET_STATUS, STALE_DATA (`max_book_age_ms`), TIME_REMAINING, PENDING_ORDER (one order in flight per market),
END_GUARD (a token whose order just ended unfilled: its fill may still arrive), NO_BOOK / BAD_BOOK, SIZE (venue
minimum 5), PRICE_BAND, SPREAD, LIQUIDITY (depth at or under the limit >= `min_depth_shares`), SLIPPAGE (VWAP vs best
ask), PURCHASES_CYCLE, PURCHASES_MARKET, MARKET_CAPITAL, TOTAL_CAPITAL (every open market), SETTLEMENT_LOSS (after
the buy both outcomes >= -`max_settlement_loss_usdc`), NO_IMPROVEMENT (the objective fails after fees), TARGET_UNACHIEVABLE
(the favourite adjustment cannot reach its target), BALANCE. The first failure is logged with its code and
counted. Hard ceilings cannot be raised above $150 per market, $500 across open markets, $20 worst-settlement loss,
six additional purchases per cycle, 20 purchases per market, 2c slippage, or the final-20-second cutoff. Partial
fills of one order count as one purchase. Emergency stop (engine.py): while live.py's guard pauses entries (feed drop,
execution failures) or the risk manager is halted (daily loss / drawdown), no order leaves, logged once as `[LP_STOP]`.
Three consecutive unfilled FAK orders stop that market; initial-entry retries are also limited to three attempts and
the market is entered only while the live price signal and all gates still qualify. Position size never
grows to recover a loss: every quantity is a fixed configured number.

## 4. Implementation map

| Spec module | Here |
|---|---|
| config.py | `strategy/leader_pair/config.py` (`LeaderPairConfig`, `TPB_LP_*`, `validate()`) |
| portfolio.py / pair_accounting.py | `strategy/leader_pair/accounting.py` |
| risk_manager.py | `strategy/leader_pair/risk.py` (+ the repo's `risk/limits.py` budget the engine registers through `apply_budget`) |
| strategy.py | `strategy/leader_pair/core.py` |
| market_data.py / execution.py / polymarket_client.py / paper_trading.py / main.py | the repo's `feeds/`, `execution/paper.py`, `execution/polymarket.py`, `live.py`, `__main__.py`; `strategy/leader_pair/engine.py` is the adapter |
| backtest.py / reporting.py | `strategy/leader_pair/backtest.py` + `scripts/leader_pair_backtest.py` |
| tests/ | `tests/test_leader_pair.py` (21 tests: the section-16 acceptance list, order failures, partial fills, duplicated events, restarts, the hooks, look-ahead, reproducibility) |

Persistence: `data/leader_pair_state.<tag>.json` after every fill and transition (lots, leader, P_ref, cycle,
counters); restored at start. live.py's own journal (`data/live_trades.<tag>.jsonl`, positions) is unchanged.
Forensic trail: `[LP]` JSON lines (every transition, order, fill, refusal with reason, recovery stop, settlement) and
`[LP_ORDER]` / `[LP_FILL]` / `[LP_SETTLED]` lines in the pm2 log; the dashboard's state carries
`leader_pair.markets.<slug>` with the state, P_ref, PnL_live, both outcomes, pairs and refusals.

## 5. Historical-data schema (what the backtester reads)

`data/replay/btc5m_books.pkl`: `{slug: {open, close, strike, t[], ua[], ub[], da[], db[], uad[], dad[], spot[], tfi[], obi[]}}`
-- epoch-ms timestamps about every 200 ms; best Up/Down ask and bid; **depth at the best ask only**; BTC spot;
optional `fee_rate`. `data/replay/btc5m_outcomes.json`: `{slug: 1 | 0}` (Up won). Built from `data/charts/<slug>.json`
by `scripts/momentum_inventory_study.py`. **Limits:** no bid depth (liquidation valued at the best bid), no deeper
ask levels (an order larger than the touch fills the touch only), no trade prints, one asset and one market type
(BTC 5-minute). Missing for a fuller test: ETH/SOL/XRP books, full depth, and more than three weeks of history.

## 6. Backtest (`scripts/leader_pair_backtest.py`)

Method: the engine driven point by point; an order reaches the venue 250 ms later and is matched against the book
recorded at the first point after that: IOC filled at the ask if ask <= limit, up to the displayed ask depth (partial
fills modelled), fee 0.07 p (1 - p) a share; otherwise unfilled and the engine told so; the order's end is reported
before its fills, as live.py does. Chronological TRAIN / VAL / TEST = 50 / 25 / 25 % of the markets (cuts
2026-10-02 03:05 UTC and 2026-10-07 01:15 UTC). No parameter was fitted: the defaults are the spec's numbers, the
sensitivity sets are compared on TRAIN+VAL and read as a map, not a selection. Reproducible (no randomness;
`test_backtest_is_reproducible_and_uses_no_future_data` also mutates every point after a cut and checks the earlier
decisions do not change).

### 6.1 The defaults (results/leader_pair_backtest_2026-10-13_1715_defaults2.jsonl)

| | TRAIN (2,035) | VAL (1,017) | TEST (1,018) | ALL (4,070) |
|---|---|---|---|---|
| net P&L after fees | -2,421.76 | -1,717.26 | -850.74 | -4,989.76 |
| per market (t-stat) | -1.190 (-5.1) | -1.689 (-5.0) | -0.836 (-2.4) | -1.226 (-7.3) |
| gross P&L (before fees) | -604.92 | -811.71 | +94.37 | -1,322.27 |
| fees | 1,816.84 | 905.55 | 945.10 | 3,667.50 |
| fill slippage vs the ask seen | -271 (fills better than the seen ask) | -122 | -120 | -513 |
| win rate (traded markets) | 52.0 % | 49.8 % | 52.0 % | 51.5 % |
| max drawdown (cumulative) | 2,433 | 1,827 | 971 | 5,037 |
| worst market / worst settlement outcome | -20.09 | -19.99 | -20.06 | -20.09 (the 20 USDC limit held) |
| best market | +58.05 | +67.14 | +70.88 | +70.88 |
| max / avg capital in a market | 59.93 / 27.92 | 59.98 / 28.00 | 59.85 / 29.35 | 59.98 / 28.30 |
| turnover (cash spent) | 54,973 | 27,377 | 28,611 | 110,960 |
| net / gross return on turnover | -4.26 % / -1.07 % | -6.07 % / -2.87 % | -2.88 % / +0.32 % | -4.35 % / -1.15 % |
| purchases (orders, unfilled, partial) | 21,180 (22,360 / 1,180 / 5) | 10,568 | 10,995 | 42,743 (44,993 / 2,250 / 6) |
| pairs formed / qualifying (<= 0.95) | 36,593 / 2,229 | 18,371 / 1,090 | 19,425 / 1,170 | 74,389 / 4,489 |
| average cost per formed pair | 1.085 | 1.081 | 1.083 | 1.083 |
| total margin of qualifying pairs | +308.7 | +151.7 | +167.2 | +627.6 |
| unmatched shares at expiry (markets) | 42,876 (1,906) | 21,144 (947) | 21,160 (933) | 85,181 (3,786) |
| recovery stops (markets with one) | 1,199 (728) | 534 (336) | 663 (394) | 2,396 (1,458) |
| falling-leader events = cycles | 7,878 | 3,750 | 3,966 | 15,594 |

Markets entered: 4,051 of 4,070 (19 NO_ENTRY: no fill inside 60 s). Final states at the close: OPPOSITE_ACCUMULATION
1,477, LEADER_FALLING 931, RISK_LIMIT_STOPPED 880, RECOVERY_STOPPED 643, RECOVERY 91, MONITORING 29.
Limit activations (refusals): recovery PURCHASES_CYCLE 3,511, recovery SETTLEMENT_LOSS 3,280, recovery
PURCHASES_MARKET 629, accumulation PURCHASES_CYCLE 357, falling PURCHASES_MARKET 219, MARKET_CAPITAL 28.

Baselines (per market, ALL): no trade 0; buy the initial leader and hold **-0.110**; buy the opposite and hold
-0.318; pair-only (leader entry, then the opposite only when the pair would cost <= 0.95) -0.396. The strategy's
-1.226 is worse than every baseline, including doing nothing.

Attribution by purchase kind (ALL; P&L of the fills themselves, cents a share):

| kind | fills | shares | P&L | c / share | TRAIN | VAL | TEST |
|---|---|---|---|---|---|---|---|
| init (the leader, 10 sh) | 4,051 | 40,504 | -430 | -1.06 | -0.09 | -2.61 | -1.47 |
| accum (opposite, rising, <= 0.50) | 5,844 | 29,216 | -844 | -2.89 | -3.92 | -1.11 | -2.51 |
| falling (opposite, improves the worst case) | 9,662 | 48,308 | -872 | -1.81 | -2.16 | +2.13 | -4.88 |
| recovery (the leader, until PnL_live > 0) | 23,186 | 115,930 | -2,844 | -2.45 | -2.29 | -5.66 | +0.35 |

By time left at the fill: > 240 s -2.15 c/sh (18,839 fills), 180-240 s -2.25, 120-180 s -1.58, 60-120 s -2.41,
< 60 s -4.52. By price: 0.0x -3.7, 0.1x -4.1, 0.2x -4.1, 0.3x -1.7, 0.4x -3.5, 0.5x -1.8, 0.6x -0.1, 0.7x -2.4,
0.8x +2.1 (218 fills), 0.9x -5.6. Market type: BTC 5-minute only (the only type recorded).

Reading: the fee alone (0.07 p (1 - p), ~1.7 c a share near 0.50) is 0.90 USDC a market at 10.5 purchases a market,
and the gross result before fees is itself negative on TRAIN and VAL and flat on TEST (+0.32 % of turnover). The
spec's own warning holds: Rule-3 pairs cost more than $1 (average formed pair 1.083), only 6 % of the pairs
qualify, and 85 k of the 185 k shares bought are unmatched at expiry -- the position is mostly directional, bought
at the ask, in both directions as the leader swings. The recovery leg (54 % of the shares) is the largest loser:
"buy the leader every 5 s while the liquidation value is under the cost" adds to the losing side of a swing. The
20 USDC settlement-loss cap and the capital cap are what bound the damage (880 markets hit a cap).

### 6.2 Sensitivity (TRAIN+VAL only; `results/leader_pair_backtest_*_sweep.jsonl`)

Every parameter set (TRAIN+VAL, 3,052 markets) and both execution sensitivities; TEST was read for the defaults only.

| set | change | TRAIN+VAL net | per market | fees | purchases / mkt | qualifying pairs | recovery stops | worst market | TEST per market (t) |
|---|---|---|---|---|---|---|---|---|---|
| defaults2 | the spec's defaults | -4139 | -1.356 | 2722 | 10.4 | 3319 | 1733 | -20.1 | -0.836 (-2.38) |
| thr090 | pair_threshold=0.90 | -4139 | -1.356 | 2722 | 10.4 | 2106 | 1733 | -20.1 | not read |
| thr100 | pair_threshold=1.00 | -4139 | -1.356 | 2722 | 10.4 | 5675 | 1733 | -20.1 | not read |
| int3 | accum_interval_s=3 recovery_interval_s=3 | -4668 | -1.529 | 2975 | 11.3 | 3783 | 1724 | -20.1 | not read |
| int10 | accum_interval_s=10 recovery_interval_s=10 | -3219 | -1.055 | 2309 | 8.8 | 2510 | 1716 | -20.0 | not read |
| q10 | accum_shares=10 falling_shares=10 recovery_shares=10 | -5548 | -1.818 | 3335 | 6.9 | 5244 | 2327 | -20.2 | not read |
| loss10 | max_settlement_loss_usdc=10 | -3746 | -1.227 | 2442 | 9.1 | 3879 | 1582 | -10.1 | not read |
| loss40 | max_settlement_loss_usdc=40 | -4252 | -1.393 | 2770 | 10.6 | 3174 | 1681 | -35.8 | not read |
| nofall | falling_buy=none | -3652 | -1.197 | 1889 | 7.1 | 2085 | 2859 | -20.1 | not read |
| anytick | accum_eval=any_tick | -4237 | -1.388 | 2768 | 10.6 | 3714 | 2153 | -20.1 | not read |
| rec055 | recovery_max_ask=0.55 | -4350 | -1.425 | 2529 | 9.6 | 3704 | 1631 | -20.0 | not read |
| fast | fall_confirm_ticks=1 recovery_confirm_ticks=1 | -4375 | -1.434 | 2911 | 11.1 | 3293 | 1887 | -20.1 | not read |
| slow | fall_confirm_ticks=6 recovery_confirm_ticks=6 | -3290 | -1.078 | 2517 | 9.6 | 3243 | 1595 | -20.1 | not read |
| cap6 | max_purchases_per_market=6 | -1675 | -0.549 | 1408 | 4.6 | 590 | 317 | -16.5 | not read |
| cycle2 | max_purchases_per_cycle=2 | -3706 | -1.214 | 2199 | 8.5 | 2863 | 1382 | -20.0 | not read |
| slip1c | slippage +0.01 | -5825 | -1.908 | 2732 | 10.4 | 2861 | 1441 | -20.1 | not read |
| haircut50 | depth x0.5 | -4159 | -1.363 | 2721 | 10.4 | 3305 | 1734 | -20.1 | not read |

Reading: no set is positive. The pair threshold changes nothing but the qualification count (no rule in the spec
buys or sells on it -- it is accounting). Fewer purchases lose less (cap6 -0.55, int10 -1.06, nofall -1.20,
cycle2 -1.21): the loss scales with the number of ask-crossing buys, which is the fee-and-spread signature, not a
signal that fewer trades would turn it positive (the initial-leader-and-hold baseline, one purchase, is itself
-0.11). Larger clips (q10) and faster confirmations (fast, int3) lose more; a wider loss limit (loss40) lets a
market lose 36. One extra cent of slippage a share costs another 0.55 a market (slip1c); halving the visible depth
changes almost nothing (the touch usually holds 5-10 shares).

### 6.3 Verdict

Negative expected net return after fees on every split, every parameter set tried, and both execution
sensitivities; worse than every baseline including no trading. The implementation is complete and tested, the
strategy is not a candidate for real money. The paper bot exists to measure live fill quality against the replay
(fill rate, slippage, the recovery-stop frequency), not because the backtest suggests an edge.

## 7. Paper trading

```
pm2 start ecosystem.leaderpair.config.js --only tpb-leaderpair-paper      # 300 USDC paper bankroll, BTC 5m
pm2 logs tpb-leaderpair-paper | grep -E "LP_ORDER|LP_FILL|LP_SETTLED|LP_STOP|\[LP\]"
```
Dashboard: the `leaderpair-paper` bot in the picker; `leader_pair.markets.<slug>` in the state. Every parameter is a
`TPB_LP_*` variable in the ecosystem file (restart to apply; `TPB_LP_ENABLED=1` is the paper-run flag). All orders
are taker-only fill-and-kill orders with a strict price limit. The risk gates include a $500 account budget, $150
per-market capital cap, $150 daily loss cap, and the $20 worst-settlement-loss limit. A proposed opposite-token buy
must improve worst-case settlement PnL after fees; an unaffordable favourite adjustment is refused rather than
partially filled below its target. The run is paper-only:
`__main__` refuses shadow / micro-live / live regardless of `TPB_LP_ALLOW_REAL`.
Restarts keep the inventory
(`data/leader_pair_state.leaderpair-paper.json`, `--keep-history`); pause entries first if a market is mid-cycle
(see the restart handover notes in memory: never delete the state file while a market is open).

## 8. Live deployment (blocked)

The combined strategy remains unproven: the prior implementation lost **$1.23 per market** in the recorded replay,
and the revised decision/risk rules have not yet been backtested. The prior result is not evidence for this revision.
Before any combined strategy is considered, replay the original strategy and each addition independently, then
demonstrate positive net PnL after fees and slippage on an untouched out-of-sample split. Until then, the
`__main__` mode gate rejects shadow, micro-live, and live even if `TPB_LP_ALLOW_REAL=1` is set.

## 9. Not done / open

* No bid depth or deeper ask levels in the recording: the liquidation estimate and large orders are approximations
  (conservative where it matters: unsold shares count zero only when depth is known; otherwise the best bid).
* Only BTC 5-minute markets were recorded; the "performance by market type" breakdown has one row.
* The dashboard shows the strategy's state through the generic bot view; no dedicated page was added.
