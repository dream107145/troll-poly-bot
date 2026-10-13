# Dynamic loss-recovery / rebalancing for the pair-lock paper bot (2026-10-02)

Built to the user's spec of 2026-10-02 ("DYNAMIC LOSS-RECOVERY / REBALANCING, not pair arbitrage"). Paper only.
The running bot (`tpb-pairlock-paper`, strategy B) was **not** restarted and its code is untouched. The new strategy is
`TPB_PAIRLOCK_STRATEGY=R`, with its own paper app `tpb-pairlockrecovery-paper` (`ecosystem.pairlockrecovery.config.js`),
defined and **not started**.

## 1. The mathematics (done first)

Hold `q_U` UP at average `a_U` and `q_D` DOWN at `a_D`; total acquisition cost `C` includes every fee. The two
settlement branches of the whole portfolio are

    P&L_UP_WIN  = q_U − C          P&L_DOWN_WIN = q_D − C          MIN = min of the two
    E[P&L]      = p_UP · P&L_UP_WIN + (1 − p_UP) · P&L_DOWN_WIN

Buying `x` shares of side S at executable VWAP `v` with fee `f` per share changes them by exactly

    S-wins branch:  + x · (1 − v − f)        other branch:  − x · (v + f)        E[P&L]:  + x · (p_S − v − f)

so the quantity that lifts the S-wins branch to the target R is

    x = (R − P&L_S_WIN) / (1 − v(x) − f(v) − slippage)

which is the spec's `Required_DOWN = (UP_cost + R) / (1 − P_DOWN)` with existing inventory on both sides, fees and
slippage included (for a bare UP position `P&L_DOWN_WIN = −UP_cost`). `v(x)` is the VWAP of `x` over the live ask
levels, so `x` and `v` are solved together (fixed-point iteration; unit test 12 checks the fixed point).

Two structural facts the engine reports and the backtest confirms:

- **Buying x closes the gap between the branches by exactly x shares.** MIN rises only up to `x* = P&L_better −
  P&L_worse` shares and falls beyond. Lifting the losing branch to R > MIN always needs `x > x*`, so a recovery to
  target always lowers the other branch below where it was: the position becomes a bet on S. The engine logs
  `improves_min` on every evaluation; `REQUIRE_MIN_IMPROVEMENT` (off by default) would stop it at `x*`.
- **E[P&L] improves only if p_S > v + f**: the model must price S above its executable cost. That, not `UP ask + DOWN
  ask`, is the economic test, and `MIN_RECOVERY_EDGE` is its threshold per share. Nothing in the engine looks at the
  sum of the two asks.

Worked example (the spec's, at the bot's fee 7 % · p(1 − p)): 100 UP @ 0.60 (cost 61.68), DOWN at 0.60, R = 5:
`x = 66.68 / (1 − 0.60 − 0.0168 − 0.005) = 176` DOWN. Afterwards P&L_DOWN_WIN = +5.0 and P&L_UP_WIN = −70.4
(before: −61.68 / +38.3); E[P&L] changes by `176 · (p_DOWN − 0.6218)`.

## 2. Files and functions

| file | change |
|---|---|
| `src/troll_poly_bot/strategy/pairlock_recovery.py` (new) | Pure logic: `RecoveryParams`, `RecoveryCycle` (states WAITING → INITIAL_ENTRY → HOLDING ⇄ RECOVERY_PENDING → SETTLEMENT_PENDING → SETTLED), `outcomes`, `required_quantity`, `plan_recovery` (x and VWAP together), `simulate_buy`, `evaluate_recovery` (the decision and the item-13 record), the order lifecycle (`on_entry_sent`, `on_recovery_sent`, `on_fill`, `on_order_done`, `on_close`, `on_settled`), `reconstruct_pnl`. Inventory is the fills-based `PairBook` from `pairlock_book.py` (unchanged). |
| `src/troll_poly_bot/strategy/pairlock_recovery_engine.py` (new) | `PairLockRecoveryEngine(TriangleEngine)`: strategy B's entry gate (`pairlock_v2.entry_decision`) for the first leg, then `evaluate_recovery` on every evaluation; REENTRY_BLOCKED for any new entry trigger while the market holds inventory; one JSON line per evaluation; four-way settlement reconciliation; restart from fills; dashboard block (version 4, rendered by the existing panel). |
| `src/troll_poly_bot/live.py` | Three lines: `TPB_PAIRLOCK_STRATEGY=R` builds this engine. |
| `src/troll_poly_bot/strategy/pairlock_engine.py` | `pairlock_strategy()` accepts `R`. |
| `ecosystem.pairlockrecovery.config.js` (new) | The paper app: the B bot's env and args plus the recovery parameters and its own budget (`TPB_TRIANGLE_MAX_ORDER_USDC 30`, market 40, exposure 80: a recovery order can be 30 shares). Own data tag, `--keep-history`. Not started. |
| `scripts/pairlock/replay_recovery.py` (new) | The backtest: A (strategy B, frozen code) against R and three parameter variants on identical windows. |
| `tests/test_pairlock_dynamic_recovery.py` (new) | The spec's 12 cases with the expected figure written out, plus lifecycle/restart/structural tests (15). Runs as a script to print the table below. |
| `tests/test_pairlock_recovery_engine.py` (new) | The live path (7). |
| `results/pairlock_recovery_replay_summary.json`, `results/pairlock_recovery_sequences.json` | The backtest numbers and every one of the 271 recovery sequences. |

Not modified: market discovery, settlement detection, feeds, execution, authentication, the dashboard code, the
shared risk limits, strategy B and the triangle engine.

## 3. The decision, in order (`evaluate_recovery`)

1. State HOLDING with inventory; both branches below R → otherwise `NONE: target already satisfied`.
2. Time left ≥ `MIN_TIME_LEFT`; recovery actions < `MAX_RECOVERY_ACTIONS`; `RECOVERY_COOLDOWN` since the last order;
   fresh spot and quotes.
3. **Direction.** The recovery side S is the side the market now favours: ≥ `RECOVERY_MIN_CONFIRMATIONS` (3) of the
   4 reversal signals point to S (original token weakening, opposite order flow, spot against, composite score ≥
   `REVERSAL_THRESHOLD`). The other side O must hold inventory whose bid is ≥ `MIN_PRICE_MOVE` under its average cost.
   Both directions use the same rule, so UP-heavy → DOWN-heavy → UP-heavy follows the market (sequence table, rows 1–2).
4. The S-wins branch below R (else `NONE`).
5. **Requirement** `x` at the executable VWAP (fees and `SLIPPAGE_ALLOWANCE` in). Depth: the ask levels must fill `x`
   with VWAP ≤ best ask + `MAX_SLIPPAGE`, else rejected (`insufficient depth` / `MAX_SLIPPAGE`).
6. **The plan must fit**: position after `x` ≤ `MAX_TOTAL_POSITION`; recovery capital ≤ `MAX_RECOVERY_CAPITAL`; cost ≤
   free bankroll. A hedge that could not be completed is not started.
7. **Edge**: `p_S − VWAP − fee − slippage ≥ MIN_RECOVERY_EDGE` (E[P&L] rises by at least that per share). `p_S` is the
   bot's existing probability model (`pairlock2_model.json`).
8. The order is `min(x, MAX_RECOVERY_ORDER_SIZE)` (≥ the venue minimum), FOK at ≤ best ask + `MAX_SLIPPAGE`; a larger
   plan proceeds in steps, each recomputed on the whole position after every fill (test 10).

Every evaluation that reaches step 3 is logged as `[RECOVERY_EVAL] {json}` with: timestamp, UP/DOWN bid and ask,
shares and average cost per side, total cost, fees and slippage paid, both branch P&Ls, the minimum, E[P&L], the
losing side's exposure and move, the signals, the executable VWAP, the required quantity and its cost, both branches
after the order, the minimum after, `improves_min`, the expected improvement, the target, the decision and the reason.
A rejection repeating for the same reason is printed once per 5 s with a count.

## 4. Backtest: A (current pair-lock, strategy B) against R (dynamic recovery)

Same 1,649 BTC windows (09-22 → 10-01), same trigger and entry gate, same execution model. R parameters: target 0.50
on the bot's 10-share legs (8 % of the position, the spec's 5-on-100 ratio), order ≤ 30 shares, position ≤ 60,
recovery capital ≤ 30, slippage ≤ 3 ¢, edge ≥ 0, move ≥ 5 ¢, 3 of 4 signals, cooldown 2 s, 3 actions. Nothing tuned.

| | train | validation | test | forward | **test + forward** | all |
|---|---:|---:|---:|---:|---:|---:|
| A: strategy B | +52.1 | +54.4 | +27.9 | +11.8 | **+39.6 (+0.192/pos, t 0.7)** | +146.1 (745 pos) |
| R: dynamic recovery | +44.7 | +9.4 | −48.9 | −38.6 | **−87.5 (−0.478/pos, t −1.2)** | −33.4 (680 pos) |
| R, MIN_RECOVERY_EDGE 0.02 | +56.8 | −54.1 | −36.3 | −25.0 | −61.3 | −58.6 |
| R, target 1.00 | +21.8 | −12.3 | −23.1 | −6.4 | −29.5 | −20.0 |
| R, MAX_SLIPPAGE 0.05 | +108.9 | −35.3 | −72.3 | −35.0 | −107.3 | −33.7 |

The requested counts (R, all windows; A where it applies):

| | A | R |
|---|---:|---:|
| initial positions | 745 markets (762 fills; B re-enters in 17) | 680 |
| recovery opportunities (adverse move + confirmed reversal, per sample) | — | 14,346 |
| recovery orders sent / filled (fill rate) | 80 / 52 hedges | 756 / 484 (64 %) |
| UP recovery orders / fills | — | 455 / 287 |
| DOWN recovery orders / fills | — | 301 / 197 |
| average recovery size | — | 15.1 shares |
| average recovery price | — | 0.610 |
| total fees / slippage | — | 205.2 / 136.7 |
| total P&L | +146.1 | −33.4 |
| P&L per position | +0.196 | −0.049 |
| worst position | −8.87 | −25.34 |
| maximum capital exposure (one market) | 19.9 | 38.3 |
| maximum inventory (shares, one market) | 20 | 59.3 |
| positions recovered (≥ 1 recovery fill) | — | 271 = 39.9 % |
| reaching the target (final P&L ≥ 0.50) | — | 74.6 % of all; 48.7 % of recovered |
| ending negative | 26.7 % | 18.4 % |
| average MIN_SETTLEMENT_PNL at settlement | — | −8.80 |
| recovery effect (P&L − the first leg alone, summed) | — | **−89.7** |
| reconciliation (book = cash = rebuilt from fills) | 0.000000 | 0.000000 |

Why the recoveries sum to a loss. In the 271 recovered markets the first legs alone made −628.2; with the recoveries
−717.9. When the last recovery side was the winner (183 markets, 68 %) the result was +163.9 in total (about +0.9
each: the target plus a little); when it was not (88 markets) −881.9 (about −10 each). The recovery turns a 10-share
loss into a 15–30-share bet on the reversal; a right call earns R, a wrong one loses the whole enlarged cost. At the
model's probabilities the edge test passed, so the model's P(S) after a confirmed reversal was too high for what then
happened — the same pattern the earlier hedge studies found. Rejections, all windows: no confirmed reversal 538,930
samples; nothing to recover from (the favoured side is the one held) 26,289; move under MIN_PRICE_MOVE 15,724; edge
5,967; target satisfied 4,575; depth 2,175; position limit 1,027; capital limit 132.

**Sequence table** (all 271 are in `results/pairlock_recovery_sequences.json`; fills are `[t, side, qty, price,
UP-win after, DOWN-win after]`):

| entry | recovery fills | final UP / DOWN | total cost | UP-win / DOWN-win | winner | P&L | first leg alone |
|---|---|---|---:|---:|---|---:|---:|
| 10 DOWN @ 0.59 | UP 15.15 @ 0.544 (+0.58 / −4.57) → DOWN 10.86 @ 0.5315 (−5.39 / +0.33) → DOWN 5 @ 0.53 (−8.12 / +2.59) | 15.15 / 25.86 | 23.27 | −8.12 / +2.59 | UP | −8.12 | −14.77 |
| 10 UP @ 0.82 | DOWN 12.96 @ 0.32 (−2.65 / +0.31) → DOWN 5 @ 0.43 (−4.88 / +3.08) → UP 20.66 @ 0.73 (+0.41 / −12.29) | 30.66 / 17.96 | 30.25 | +0.41 / −12.29 | DOWN | −12.29 | −23.67 |
| 10 DOWN @ 0.45 | UP 19.12 @ 0.71 (+0.60 / −8.52) | 19.12 / 10 | 18.52 | +0.60 / −8.52 | UP | +0.60 | −4.67 |
| 10 DOWN @ 0.56 | UP 20.82 @ 0.68 (+0.57 / −10.25) | 20.82 / 10 | 20.25 | +0.57 / −10.25 | UP | +0.57 | −5.77 |

Check of row 3: UP-win = 19.12 − 18.524 = +0.596 ✓ (target 0.50 reached, 19.12 chosen so); DOWN-win = 10 − 18.524 =
−8.524 ✓; cost = 10·0.45 + 10·fee(0.45) + 19.12·(0.71 + fee(0.71)) = 4.50 + 0.173 + 13.575 + 0.276 = 18.524 ✓.

Limits of the backtest: the archive holds each token's touch and the depth within two ticks, modelled as three levels
with 80 % taken by faster traders; a requirement beyond that is "insufficient depth" here even if the live book is
deeper (2,175 of 14,346 opportunities). R allows two entry attempts per market where B keeps retrying, so R has 680
first legs to B's 762; the recovery effect is measured within R's own positions and does not depend on that.

## 5. Unit tests: expected (by the formula) against the engine

`PYTHONPATH=src:tests .venv/bin/python tests/test_pairlock_dynamic_recovery.py` prints this table (15 passed):

| case | quantity | expected | actual |
|---|---|---:|---:|
| 1 UP losing, buy DOWN | UP cost incl. fees | 6.1680 | 6.1680 |
| | P&L if DOWN wins, before | −6.1680 | −6.1680 |
| | required DOWN = (0.50 + 6.168) / (1 − 0.60 − 0.0168 − 0.005) | 17.6400 | 17.6400 |
| | P&L if DOWN wins / UP wins, after | +0.5034 / −7.1366 | +0.5034 / −7.1366 |
| | E[P&L] improvement = x (p_DOWN − v − fee − slip) | 1.3794 | 1.3794 |
| 2 DOWN losing, buy UP | required UP; UP-win / DOWN-win after | 17.64; +0.5034 / −7.1366 | same |
| 3 UP recovers after the DOWN hedge | required UP; UP-win / DOWN-win after | 28.22; +0.5007 / −20.0793 | same |
| 4 DOWN recovers after the UP hedge | required DOWN; DOWN-win after | 28.22; +0.5007 | same |
| 5 target already satisfied | P&L either way (10 UP @ 0.40 + 10 DOWN @ 0.45) | 1.1587 → no order | 1.1587, `NONE` |
| 6 insufficient bankroll | capital the plan needs (> 5.00 free) | 10.9686 → rejected | 10.9686, rejected |
| 7 insufficient depth | shares needed (10 offered) | 17.64 → rejected | 17.64, rejected |
| 8 slippage exceeds the limit | VWAP − best ask | 0.0342 > 0.03 → rejected | 0.0342, rejected; 0.08 → sent |
| 9 fees turn a theoretical recovery into a loss | DOWN-win if the fee-free 133.4 were bought at 0.95 | −0.6102 | −0.6102 |
| | fee-aware requirement | 160.00 → over the position limit | 160.00, rejected |
| 10 multiple sequential rebalances (orders ≤ 8) | plan 17.64 → 8; DOWN-win −3.1024 → plan 9.53 → 8 → 5 | as listed | as listed; 21 DOWN, target reached, 3 actions |
| 11 existing UP + existing DOWN | UP-win / DOWN-win; required DOWN with the 6 DOWN counted | +0.727 / −3.273; 9.98 (not 17.64) | same |
| 12 several ask levels | VWAP over 3 levels; x = fixed point of the formula; DOWN-win after | 0.6101; 18.11; +0.5018 | same |

Also tested: states and transitions, pending ≠ inventory, partial fills, settlement reconciliation (book = cash =
rebuilt), restart from JSON, a loss alone or a reversal alone is rejected, the minimum peaks at `x*`. The live path
(`tests/test_pairlock_recovery_engine.py`, 7): entry → HOLDING, REENTRY_BLOCKED, a recovery order both ways through the
engine with a partial fill, the evaluation log line, four-way reconciliation and the halt, the dashboard block, env
names in both spellings, selection and restart.

## 6. Verification against the spec

- Both directions, any number of times: rows 1–2 of the sequence table; tests 3, 4, 10. ✓
- The complete position in every calculation: `outcomes`/`plan_recovery` read the `PairBook`; test 11. ✓
- Executable VWAP over the ask levels, never the mid or best ask alone: `vwap_buy`; tests 7, 8, 12. ✓
- Fees and slippage inside the target: the `(1 − v − f − slip)` denominator; test 9. ✓
- Partial fills recomputed: `on_fill` → `outcomes`; the live test's 6-then-rest fill. ✓
- No independent re-entry while holding; re-entry into the original side only through the recovery arithmetic. ✓
- No "pair cost ≤ 1" condition anywhere. ✓
- Both branch P&Ls and the minimum on every record, with `improves_min`. ✓
- Not restarted, not enabled; a separate app, off. ✓

## 7. Deployment

`tpb-pairlockrecovery-paper` has been running since 18:10 UTC (paper, 100 USDC, own data tag, `--keep-history`). At
18:48 UTC, at the user's instruction that the rebalancing must repeat at every swing ("after the bot recovers, when
the first token rises again, buy it again"), the caps that limited the repetition were raised and the app restarted:
`MAX_RECOVERY_ACTIONS` 3 → 10, `MAX_TOTAL_POSITION` 60 → 300, `MAX_RECOVERY_CAPITAL` 30 → 90, `MAX_RECOVERY_ORDER_SIZE`
30 → 60, and the app's own budget (order 60, market 100, exposure 100 USDC). The engine's rule was already symmetric;
the test `test_the_rebalancing_repeats_at_every_swing_until_the_bankroll_ends_it` shows four swings of a 10-share
position: 17.64 DOWN → 27.90 UP → 53.20 DOWN → a 120.75 UP plan refused by the bankroll. Each swing roughly doubles the
position and the losing branch (−6 → −7 → −20 → −33 USDC), so with a 100 USDC bankroll the sequence ends at the third
or fourth swing. Every evaluation is in the pm2 error log as a `[RECOVERY_EVAL]` JSON line.

## 8. Late buy (added 19:10 UTC)

At the user's instruction ("at the last few seconds, buy winning tokens to increase profits more"): in the last
`LATE_WINDOW_S` 20 s down to `LATE_MIN_LEFT_S` 10 s (the bot refuses orders under 10 s), if the model gives one side
≥ `LATE_MIN_P` 0.95 and its ask is ≤ `LATE_MAX_PRICE` 0.97, buy one clip of `LATE_SHARES` 10 when
`P(win) − VWAP − fee − slippage ≥ LATE_MIN_EDGE` 0, FOK at ask + `MAX_SLIPPAGE`; at most `LATE_MAX_ORDERS` 2 clips and
`LATE_MAX_CAPITAL` 20 USDC per market; a market with no position is opened by it. The clip joins the same portfolio
accounting; settlement reports its own P&L (`late buys … on n fills`). Tests: `test_late_buy_of_the_winner_in_the_last_seconds`,
`test_late_buy_gates_window_probability_price_edge_caps_and_no_position`, `test_the_late_buy_through_the_live_path`.
The 09-25 final-minute study (`docs/snipe60_*`) found no model edge late in the window, so this leg's P&L should be
read from the `[RECOVERY_SETTLED]` lines before trusting it.

