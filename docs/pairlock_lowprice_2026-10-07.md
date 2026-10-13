# Low-price first leg + dynamic partial recovery (2026-10-07)

The user's spec: a new mode of the pair-lock RECOVERY engine (strategy R) that buys the CHEAPER token as the first leg
-- only when it earns its place -- and recovers it with the minimum opposite quantity that reaches a configured
objective, sized at the executable VWAP from the whole inventory. Built as a separate paper app
(`tpb-pairlocklow-paper`, data tag `pairlock-low-paper`, `ecosystem.pairlocklow.config.js`); the recovery paper bot,
the live bot and the conditions bot keep their rules.

## 1. The existing architecture (read before editing)

| | |
|---|---|
| Entry flow | `PairLockRecoveryEngine.evaluate` -> `TriangleEngine` (spot-shock trigger) -> entry mode `open` / `conditions`: `_open()` buys the HIGHER-ask token in the open window within `[open_min_price, open_max_price]`, fill-and-kill at ask + offset; `trigger`: `_gate_entry` -> `plv2.entry_decision` (model EV >= `entry_edge`, band, spread, executable VWAP, caps) |
| State machine | `RState` WAITING -> INITIAL_ENTRY -> HOLDING <-> RECOVERY_PENDING -> SETTLEMENT_PENDING -> SETTLED; cycle flags `rec_episodes` / `rec_open`, `locked`, `pair`, ladder counters |
| Recovery flow | `evaluate_recovery` on every sample while HOLDING: S = the higher-mid side, the held side must sit >= `min_price_move` under its cost, the S branch < target, `recovery_min_price`, `max_recoveries`, even-out, lock triggers (`max_market_loss` / `recovery_max_price` / `lock_left_s`); `plan_recovery` sizes the buy at the executable VWAP; `step_fill` buys what fits (order size, depth within `max_slippage`, position, capital, bankroll) and re-evaluates after every (partial) fill |
| Position | `PairBook`: fills -> UP/DOWN shares, total cost incl. fees, realised exits, `resolution_pnl(winner)`, pending orders |
| Fill model | paper: `PaperExchange` "calibrated" -- the order lands after the measured latency, matches the book THEN, the quote must survive 1 s at or under the limit, the worse VWAP, IOC partial fills; replay: `replay2.fok` (+143 ms, 80 % depth haircut, 1 s survival, all-or-nothing) |
| Configuration | `TPB_PAIRLOCK_REC_<FIELD>` env -> `RecoveryParams` (`recovery_params_from_env`); dashboard Settings `pairlock.*` applied at runtime; one `ecosystem.*.config.js` per app; `TPB_PAIRLOCK_STRATEGY=R` selects the engine |
| Tests | 14 `tests/test_pairlock_*.py` files (216 passing; 3 pre-existing failures in `test_pairlock_booksignal.py`, another session's unstarted bot) |

## 2. What was added (new code), what was reused, what is untouched

New, all in existing files unless noted:
- `strategy/pairlock_recovery.py`: `RecoveryParams.entry_mode="low"` + `low_*` fields; `recovery_target_mode`,
  `partial_target`, `min_pnl_improvement`; `first_leg_cost()`, `recovery_target()`,
  `calculate_recovery_quantity()` (the dedicated sizing function; `plan_recovery` now delegates to it);
  `low_entry_side()`; the spec's named fields + a `verdict` (BUY / WAIT / REJECT) on every `RECOVERY_EVAL` record;
  the expected-value gate and `MIN_PNL_IMPROVEMENT` in `_step_order`.
- `strategy/pairlock_recovery_engine.py`: `_open` handles `low` (the cheaper token through the EV gate); two counters.
- `control.py`: `TPB_CONTROLS_IGNORE` (an app may leave shared dashboard rows alone).
- `ecosystem.pairlocklow.config.js` (new app), `tests/test_pairlock_low_price.py` (15 tests),
  `scripts/pairlock/replay_lowprice.py` (old vs new on the same windows).

Reused: `plv2.entry_decision` + `ProbModel` (the first leg's EV; no second model), `reversal_signals` (the flow
signal), `vwap_buy`, `PairBook`, the step / partial-fill / lock / cooldown machinery, `PaperExchange`, `RiskManager`,
live.py's order path, settlement and reconciliation.

Untouched: market discovery, token/book feeds, settlement handling, risk guards, every other strategy, every
existing default (`RecoveryParams()` is unchanged: `entry_mode="trigger"`, `recovery_target_mode="target_profit"`).

## 3. The state machine (unchanged names)

```
WAITING ──(low_entry_side: cheaper token passes the gate; order sent)──▶ INITIAL_ENTRY
INITIAL_ENTRY ──fill──▶ HOLDING                        ──kill──▶ WAITING (up to low_max_tries)
HOLDING ──(evaluate_recovery: verdict BUY)──▶ RECOVERY_PENDING ──fill / kill──▶ HOLDING (re-evaluated from the inventory)
HOLDING ──(verdict WAIT / REJECT)──▶ HOLDING
HOLDING ──close──▶ SETTLEMENT_PENDING ──outcome──▶ SETTLED
```
A lock (`max_market_loss` / `lock_left_s`) is a RECOVERY_PENDING order of kind `lock`, as before.

## 4. The recovery mathematics (`calculate_recovery_quantity`)

With the whole position held -- first-side shares n, opposite shares h, every cost paid C (fees in), realised exits E:

```
opposite-wins outcome before the buy   B = h - C + E
first-wins outcome before the buy      F = n - C + E
each opposite share bought at VWAP v with fee f (and the slippage allowance s) adds  1 - v - f - s  to B
required additional quantity           x = (target - B) / (1 - v - f - s)      (v itself walked for x: iterated)
required total                         h + x ;   additional = max(0, required_total - h)
after the buy                          B' = B + x (1 - v - f - s),   F' = F - x (v + f + s)
```
The dearer the opposite token, the MORE shares the recovery needs: 20 UP @ 0.20 (4 USDC) needs 10 DOWN at 0.60,
13.33 at 0.70, 20 at 0.80 (before fees). A cheap first leg only ever needs fewer opposite shares than its own size
while p1 + p2 < 1. `target` by `recovery_target_mode`: `target_profit` +0.50 (the old rule), `break_even` 0,
`partial_recovery` -(1 - partial_target) x first-leg cost, `expected_value` 0 and every step must have
P(S) - v - f - s >= 0. A step must also raise the worse outcome (expected_value: E[P&L]) by >= `min_pnl_improvement`.
The step itself is then the old `step_fill` (order size, the asks within `max_slippage`, position, capital,
bankroll), re-evaluated after every partial fill from the inventory, never from the order's size. Nothing is ever
labelled guaranteed: the record carries `worst_case_after` at the executable cost, fees and slippage in.

## 5. Configuration (all `TPB_PAIRLOCK_REC_<NAME>`; existing names kept)

| new | default | the low app |
|---|---|---|
| ENTRY_MODE | trigger | low |
| LOW_PRICE_MIN / LOW_PRICE_MAX | 0.05 / 0.45 | 0.05 / 0.45 |
| LOW_MIN_EDGE | 0.02 | 0.02 |
| LOW_MAX_SPREAD / LOW_MAX_SLIPPAGE | 0.05 / 0.03 | 0.05 / 0.03 |
| LOW_MIN_LEFT_S / LOW_MAX_LEFT_S | 120 / 295 | 120 / 295 |
| LOW_REQUIRE_FLOW_SIGNAL / LOW_MIN_SIGNALS | 1 / 2 | 1 / 2 |
| LOW_MAX_NOTIONAL / LOW_MAX_TRIES | 10 / 3 | 10 / 3 |
| RECOVERY_TARGET_MODE | target_profit | break_even |
| PARTIAL_TARGET | 0.5 | 0.5 |
| MIN_PNL_IMPROVEMENT | 0 | 0.05 |

Reused as the spec's names: `RECOVERY_OPPOSITE_MIN_PRICE` = `RECOVERY_MIN_PRICE` (0.55 in the app),
`RECOVERY_MAX_QTY` = `MAX_RECOVERY_ORDER_SIZE` / `MAX_TOTAL_POSITION`, `RECOVERY_MAX_NOTIONAL` =
`MAX_RECOVERY_CAPITAL`, `RECOVERY_MAX_SLIPPAGE` = `MAX_SLIPPAGE`, `RECOVERY_MIN_TIME_REMAINING` = `MIN_TIME_LEFT_S`,
`RECOVERY_ENABLED` = `RECOVERY_ENABLED`, `MAX_FIRST_LEG_NOTIONAL` = `LOW_MAX_NOTIONAL`, `MAX_OPEN_MARKETS` =
`TPB_TRIANGLE_MAX_SIMULTANEOUS_POSITIONS`, the daily-loss / drawdown / kill controls as before. The app also sets
`TPB_CONTROLS_IGNORE=pairlock.*`.

## 6. Tests

See the final report section below.

## 7. Historical replay: OLD vs NEW on the same 2,749 BTC markets (09-22 .. 10-07)

`scripts/pairlock/replay_lowprice.py` -> `results/pairlock_lowprice_replay_2026-10-07.json` (+ `_positions_`, `_raw_`).
Same windows, features, probability model (`pairlock2_model.json`, fitted on the older train set), execution model
(arrival +143 ms, 80 % depth haircut, 1 s quote survival, worse VWAP, all-or-nothing) and time splits
(train 1,374 / validation 550 / test 412 / forward 413 markets). No lookahead. Caveats: the replay cannot show partial
fills (the paper bot can); the archive's book is the touch plus two ticks; the low mode found only 96 positions.

| all 2,749 markets | A (old strategy B) | R-open (recovery bot's rule) | LOW-hold (cheap leg, no hedge) | **LOW (spec: break-even)** | LOW-p50 | LOW-ev |
|---|---:|---:|---:|---:|---:|---:|
| first-leg opportunities / fills | - / 1,240 | 3,394 / 2,706 | 483 / 96 | 483 / 96 | 483 / 96 | 483 / 96 |
| recovery evaluations (BUY / WAIT / REJECT) | - | 322,204 (3,031 / 319,036 / 137) | 0 | 25,466 (110 / 25,200 / 156) | 3,138 (135 / 2,989 / 14) | 4,680 (236 / 1,061 / 3,383) |
| recovery orders / fills (+ lock fills) | 133 / 87 | 2,177 / 1,774 (+591) | 0 | 110 / 96 | 135 / 118 | 208 / 154 (+16) |
| recovered / unrecovered positions | - | 1,372 / 1,334 | 0 / 96 | 96 / 0 | 96 / 0 | 83 / 13 |
| pair completions (worse outcome >= 0) | - | 1 | 0 | 0 | 0 | 1 |
| one-sided wins / losses | - | 1,326 / 8 | 35 / 61 | 0 / 0 | 0 / 0 | 13 / 0 |
| settlement wins / losses | 905 / 310 | 2,113 / 593 | 35 / 61 | 42 / 54 | 29 / 67 | 79 / 17 |
| avg first-leg price / avg recovery price | - | 0.62 / 0.76 | 0.41 / - | 0.41 / 0.65 | 0.41 / 0.66 | 0.41 / 0.69 |
| recovery qty / first-leg qty | - | 2.61 | - | 1.18 | 0.72 | 2.31 |
| worst case before -> after the first recovery | - | -6.07 -> -15.38 | - | -4.13 -> -1.71 | -4.13 -> -2.07 | -4.15 -> -2.77 |
| expected P&L before -> after (model) | - | -3.68 -> -3.37 | - | -0.21 -> -0.66 | -0.20 -> -0.44 | -1.10 -> -0.80 |
| first-leg P&L / recovery effect | +393 / -19 | -162 / -704 | -46 / 0 | -46 / -24 | -46 / -4 | -46 / +48 |
| fees / slippage | - | 1,015 / 174 | 16 / 3 | 34 / 7 | 27 / 6 | 46 / 10 |
| gross / **net P&L** | - / **+375** | +148 / **-866** | -30 / **-46** | -36 / **-70** | -23 / **-50** | +48 / **+1** |
| per position (t) | +0.31 (2.7) | -0.32 (-2.7) | -0.48 (-1.0) | **-0.73 (-6.4)** | -0.52 (-3.0) | +0.01 (0.03) |
| worst position / max drawdown | -9 / -103 | -33 / -880 | -5 / -95 | -5 / -70 | -2 / -54 | -20 / -52 |
| max / avg capital per market | 20 / - | 72 / 19 | 5 / 4 | 15 / 12 | 14 / 9 | 82 / 19 |
| avg holding (s) | - | 296 | 220 | 220 | 220 | 220 |

Per split, net P&L (per position, t):

| | train | validation | test | forward |
|---|---:|---:|---:|---:|
| A | +123 (+0.20, 1.2) | +89 (+0.37, 1.5) | +129 (+0.85, 3.2) | +33 (+0.17, 0.6) |
| R-open | -415 (-0.31, -1.8) | -317 (-0.59, -2.1) | +68 (+0.17, 0.6) | -203 (-0.50, -1.6) |
| LOW-hold | +1 (+0.01, 0.0) | -45 (-3.48, -4.7) | -21 (-1.05, -1.0) | +19 (+1.60, 1.2) |
| **LOW** | -34 (-0.68, -5.4) | -6 (-0.42, -1.6) | -12 (-0.61, -2.6) | -18 (-1.51, -3.0) |
| LOW-p50 | -16 (-0.31, -1.2) | -18 (-1.35, -4.5) | -11 (-0.56, -1.7) | -6 (-0.47, -0.9) |
| LOW-ev | +41 (+0.81, 1.9) | -9 (-0.72, -0.7) | +4 (+0.20, 0.3) | -35 (-2.92, -1.4) |

### The experimental split (item 21)
- **A. First-leg directional edge:** none. The cheap leg alone (LOW-hold) is -0.48 a position (t -1.0), positive in
  train and forward, negative in validation and test; 20 % of the 483 opportunities filled, the fills skew to the
  ones that then lost (adverse selection: first-leg P&L -46 on 96 fills).
- **B. Recovery edge:** negative. Once the cheap leg sits under its cost the opposite token costs ~0.65, so
  p1 + p2 ~ 1.06 before fees: the break-even target is unreachable and every "recovery" turns the position into a
  certain small loss. The model agrees at the time of the buy (expected P&L -0.21 -> -0.66). Realised recovery
  effect -24 on 96 positions. Only the expected-value variant, which refuses the hedge whenever the model's P(S)
  is under the price (3,383 REJECTs), avoids that -- and ends at +1 overall, a coin flip across splits.
- **C. Pair completion:** 0 of 96 (1 of 96 in LOW-ev). A worse outcome >= 0 needs p1 + p2 + fees < 1 at the
  executable sizes; it never happened after the first leg had moved against the position.
- **D. Risk reduction:** real but bought with certain losses. The worse outcome rises from -4.13 to -1.71 on average,
  the worst position is -4.86 vs -33 (R-open), the drawdown -70 vs -880 -- because the hedge fixes the loss, not
  because it ever creates a profit.

### The spec's questions
1. **Net P&L improved?** No. LOW: -70 (-0.73 a position, t -6.4, negative in all four splits). R-open: -866 but
   -0.32 a position; A: +375. The total is less negative only because the low mode trades 28x fewer markets.
2. **Fewer unrecovered first legs?** Yes, by construction (0 of 96 unhedged) -- and every hedged one lost.
3. **Less downside?** Yes: worst position -4.86, drawdown -70. The price of it is a deterministic ~-1.9 a position.
4. **More adverse selection?** Yes: 20 % fill rate on the cheap leg and the filled legs lose where the opportunities
   on average did not.
5. **More capital?** 12 USDC a market vs 4 for the leg alone (less than R-open's 19).
6. **Longer holding?** No: 220 s vs 296 s (the leg enters with >= 120 s left).
7. **More pair completions?** No: 0.
8. **Statistically meaningful?** The LOSS of the break-even mode is (t -6.4, all splits negative). The first-leg
   edge and the expected-value variant are not distinguishable from zero on 96 positions.

**Verdict: NO.** The mode is built, tested and safe to run on paper, but the replay gives no reason to run it: a cheap
first leg has no measured edge, and hedging it after the move is a guaranteed small loss. If anything is worth a
paper run it is `LOW-ev` (recovery only when the model's P(S) covers the price), as an experiment, not a candidate.
The app is defined (`ecosystem.pairlocklow.config.js`) and NOT started.
