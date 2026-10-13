# Pair-lock: recovery hedge and profit lock, implemented to spec (2026-10-02)

Paper only. The bot is `tpb-pairlock-paper` (`ecosystem.pairlock.config.js`, data tag `pairlock-btc-paper`).
This is the strategy in the 2026-10-02 spec, implemented as written; nothing was optimised.

## The fact everything below rests on

The two token books mirror each other: on 82,805 of 82,805 recorded samples, **DOWN ask = 1 − UP bid**, exactly.

Take a position of `q` shares of side A at average cost `a`. If A's bid has fallen to `b`, buying the opposite token
pairs at

    pair cost = a + (1 − b) + fees = 1 + (a − b) + fees

So a **materially losing** position (`a − b ≥ RECOVERY_MIN_ADVERSE_MOVE`) can only be hedged at a pair cost **above
1**, by at least its loss per share. Two consequences follow:

- With `MAX_ACCEPTABLE_PAIR_COST = 1.00`, which sections 13 and 29 require, **no recovery hedge can ever
  qualify**. The replay confirms it: 3,225 recovery evaluations, 0 acceptable.
- In payoff terms, a recovery hedge of `q` shares is the same as selling `q` of the original at its bid. Both
  outcomes pay the same and the fee formula 7 % · p(1 − p) is symmetric. The hedge additionally ties up `q(1 − b)`
  of capital until settlement.

The cap is configurable. The diagnostic runs below set it to 1.05–1.20 only to show what the recovery logic does
when it is allowed to act. None of them is a recommendation.

## 1. Files changed

| file | change |
|---|---|
| `src/troll_poly_bot/strategy/pairlock_book.py` | New state set and transition table, plus `LEGACY_STATES` for restoring v2 state. Hedge candidates now carry before/after expected and worst case, downside reduction, the incremental (paired-portion) pair cost and edge, and a slippage allowance. The `best_hedge` / `MIN_HEDGE_EV` picker is removed. |
| `src/troll_poly_bot/strategy/pairlock.py` | New parameters (all spec names). `Cycle` gains audit events and a `watch_state` derived from inventory. `observe` handles the adverse move and the 4-signal reversal confirmation. `manage_position` holds MODE B (recovery) and MODE A (profit lock) as two separate decisions. FOK limits are capped by the pair-cost cap. `entry_decision` adds `REENTRY_BLOCKED`. The order lifecycle covers partial and unfilled hedges. Settlement goes MARKET_RESOLVED → SETTLED with an independent rebuild of P&L from the fills. |
| `src/troll_poly_bot/strategy/pairlock_engine.py` | Separate `recovery_*` / `profit_lock_*` counters. Every transition and audit event is logged with the inventory. Fills carry their hedge kind. The entry record includes spot age, quote age and decision-to-fill latency. Reconciliation is three-way per market, and a market the bot booked P&L on that the engine never held now fails reconciliation. |
| `src/troll_poly_bot/web/static/app.js` | Pair-lock panel v3: separate recovery and profit-lock KPIs, the no-hedge baseline, a "locked vs directional" column, and per-leg settled P&L. |
| `ecosystem.pairlock.config.js` | New environment block (`combined`, cap 1.00). The obsolete v2 settings are removed; the engine warns if one is still set. |
| `scripts/pairlock/v2_frozen/` | Frozen copy of the v2 code as deployed. `replay2.py` now imports it. |
| `scripts/pairlock/replay3.py` | Replay of A–E plus the diagnostics. |
| `tests/test_pairlock_recovery.py` (new) | The spec's 20 tests. `tests/test_pairlock*.py` updated to the new machine. |

Earlier the same day, and not part of this spec: the late-resolution fix in `live.py`, `_book_late` →
`engine.on_market_settled`, together with the startup handover. That is the "engine panel stuck" issue from section
24.

## 2. State machine

    WAITING -> INITIAL_ENTRY -> DIRECTIONAL_POSITION -> ADVERSE_MOVE -> REVERSAL_CONFIRMED -> RECOVERY_EVALUATION
      -> RECOVERY_HEDGE_PENDING -> RECOVERY_HEDGED -> PAIR_LOCKED | UNMATCHED_DIRECTIONAL
    DIRECTIONAL_POSITION | UNMATCHED_DIRECTIONAL -> PROFIT_LOCK_EVALUATION -> PROFIT_LOCK_PENDING
      -> PAIR_LOCKED | UNMATCHED_DIRECTIONAL | RECOVERY_HEDGED
    any state with inventory -> SETTLEMENT_PENDING -> MARKET_RESOLVED -> SETTLED

Two states were added beyond the spec's list: `PROFIT_LOCK_EVALUATION` and `PROFIT_LOCK_PENDING`. Correctness needs
them. Without them a profit-lock order in flight would have no state of its own, and a recovery decision could start
while it was still pending.

| state | meaning |
|---|---|
| `PAIR_LOCKED` | Fully matched, net pair edge ≥ `MIN_PAIR_EDGE` and locked P&L ≥ `MIN_LOCKED_PROFIT`. |
| `RECOVERY_HEDGED` | Hedged and held to settlement, but not a profitable lock. The matched part's P&L is fixed, possibly at a loss. |
| `UNMATCHED_DIRECTIONAL` | Matched pairs plus an unmatched remainder. Only the matched part counts as locked. |

`RECOVERY_EVALUATION` is reachable only from `REVERSAL_CONFIRMED`, and `REVERSAL_CONFIRMED` only through
`ADVERSE_MOVE`. A profit-lock evaluation is reachable only from the two states that are not losing. No state that
holds inventory has any path back to `WAITING` or `INITIAL_ENTRY`; a unit test walks the whole graph to check this.

## 3. Recovery-hedge decision (MODE B)

**Adverse move.** Both conditions must hold:

    avg − bid ≥ RECOVERY_MIN_ADVERSE_MOVE   (0.05)
    unmatched × (exit VWAP on the bids − exit fee − avg − entry fee per share) ≤ −RECOVERY_MIN_LOSS   (0.50 USDC)

**Reversal confirmed.** The position must be adverse, and at least `RECOVERY_MIN_CONFIRMATIONS` (3) of the
following 4 signals must hold, all continuously for `RECOVERY_CONFIRMATION_WINDOW` (400 ms). Spot age must be ≤
`MAX_SPOT_AGE` and both quote ages ≤ `MAX_QUOTE_AGE`, with both spreads ≤ `MAX_SPREAD`. Stale data or a wide spread
restarts the clock.

| signal | condition |
|---|---|
| `original_weakening` | The original token's 1 s velocity ≤ 0 and its 3 s velocity < 0. |
| `opposite_flow` | Polymarket depth flow toward the opposite token > 0, and book imbalance toward it ≥ 0. |
| `spot_against` | The 1 s and 3 s spot momentum are both against the original, and the exchanges are not disagreeing. |
| `reversal_score` | The composite score ≥ `RECOVERY_REVERSAL_THRESHOLD` (1.5). |

`PairLockParams` refuses fewer than 2 required signals, and refuses a zero loss or zero move threshold.

**Candidates.** `q = f × min(unmatched, RECOVERY_MAX_HEDGE_FRACTION × first-leg qty − already hedged)`, for
`f ∈ {0.2, 0.4, 0.6, 0.8, 1.0}`. For a 10-share position that gives 2 / 4 / 6 / 8 / 10 shares. Each candidate gets
its new quantities, matched and unmatched, total cost, pair cost, pair edge, UP- and DOWN-resolution P&L, worst case,
expected P&L, downside reduction and capital.

**Acceptable** means all of the following:

    generic limits (venue minimum, depth, market capital, positions, market loss)
    incr_pair_cost(q) ≤ MAX_ACCEPTABLE_PAIR_COST
    downside_reduction(q) ≥ MIN_RECOVERY_DOWNSIDE_REDUCTION   (0.50)
    the FOK limit needed for q's levels ≤ the price the cap allows

where

    incr_pair_cost(q) = avg_heavy + heavy fee per share + (opposite VWAP + SLIPPAGE_ALLOWANCE) + opposite fee per share
    downside_reduction(q) = worst_case_after(q) − worst_case_before

**Decision.** Score each acceptable candidate with

    utility(q) = expected_after(q) + RISK_AVERSION × downside_reduction(q)   (RISK_AVERSION 0.25)

Send the acceptable `q` with the largest utility, if and only if `utility(q) > expected_before`. There is no
`MIN_HEDGE_EV`: a recovery hedge may lower the expected P&L if it buys enough downside reduction.

## 4. Profit-lock decision (MODE A)

It is evaluated only in `DIRECTIONAL_POSITION` or `UNMATCHED_DIRECTIONAL`, that is, never while materially losing.

**Qualifies** means all of the following:

    generic limits
    incr_pair_cost(q) ≤ PROFIT_LOCK_MAX_COST   (0.95)
    1 − incr_pair_cost(q) ≥ PROFIT_LOCK_MIN_EDGE   (0.05)
    q × incr_pair_edge(q) ≥ MIN_LOCKED_PROFIT   (0.05)
    the FOK limit fits under min(PROFIT_LOCK_MAX_COST, 1 − PROFIT_LOCK_MIN_EDGE)

**Decision.** Send the qualifying `q` with the largest `expected_after + PROFIT_LOCK_RISK_AVERSION ×
downside_reduction` (0.25), if and only if it beats `expected_before` (holding). The evaluation logs the locked
profit, expected P&L if holding, expected P&L after the lock, worst case before and after, capital and the remaining
unmatched quantity.

## 5. Pair accounting

All figures come from the fills; unchanged from v2.

    up_qty, down_qty = buys − sells per side;  gross = Σ qty × fill price;  fees per fill
    matched = min(up, down);  unmatched_up = max(up − down, 0);  unmatched_down = max(down − up, 0)
    matched_pair_cost = matched × (up_avg + down_avg) + fees allocated pro rata (matched / qty per side)
    locked pair P&L = matched − matched_pair_cost;  edge per share = 1 − matched_pair_cost / matched
    total cash cost = up gross + down gross + fees
    settlement P&L = qty(winner) − total cash cost + realised exit P&L
    portfolio worst case = min(up, down) − total cash cost   (a separate figure from the pair lock)

**One departure from section 9, and why.** The spec's total cash cost adds slippage on top of the fill prices. The
gross cost already uses the actual fill price, and slippage is `(fill − expected) × qty`, which is already inside
that price. Adding it again would count it twice and break the reconciliation against venue cash. So slippage is
measured and reported per fill, not added to cost. In decisions, before any fill exists, `SLIPPAGE_ALLOWANCE` is
added to the executable VWAP instead.

## 6. Executable VWAP

`vwap_buy` walks the opposite token's live ask levels in price order, assuming `DEPTH_HAIRCUT` (80 %) of each level
goes to faster traders. It charges a fee per level and returns None when the visible depth cannot fill the order.
The decision adds `SLIPPAGE_ALLOWANCE` (0.5 ¢).

The FOK limit is the last level used + 3 ¢, capped at the highest price `p` with
`avg + fee_heavy + p + fee(p) ≤ cap`. Since `p + fee(p)` increases in `p`, no fill under that limit can pair above
the cap. The replay uses the calibrated scenario: arrival after 143 ms, the 1 s survival check, and the worse VWAP
of the two books.

## 7. Partial fills

Each fill updates the book at once. The order stays `*_PENDING` until it finishes. Then:
- If less than requested filled, a `PARTIAL_HEDGE` event is logged and the state comes from what is held, for
  example 10 UP + 6 DOWN → `UNMATCHED_DIRECTIONAL` with 6 locked and 4 directional.
- If nothing filled, a `*_NOT_FILLED` event is logged and the cycle goes back to watching; the exposure is unchanged.

Pending orders are never inventory, and a restart drops them.

## 8. Re-entry

`entry_decision` refuses any market where a cycle holds inventory (`REENTRY_BLOCKED`, logged at most every 30 s per
market). The transition table has no road back from any state with inventory. In the replay, `markets_with_2_cycles`
is 0 in every new-code run, against 17 under B (v2).

## 9. Unit tests

`tests/test_pairlock_recovery.py` holds the spec's tests 1–20; 14 and 15 are one parametrised test.
`tests/test_pairlock.py` adds:
- a loss alone never reaches an evaluation;
- a reversal without a material loss is not adverse;
- the k-of-4 signals, the confirmation window and staleness;
- the utility trade-off (lower EV accepted for downside, rejected at RA 0);
- the strict cap blocking every recovery because the books mirror;
- the FOK limit never pairing above the cap;
- the max-hedge-fraction cap;
- re-evaluation, action limit and time left;
- never chasing the loss;
- each mode doing only its own decision.

`tests/test_pairlock_book.py` adds no path back from inventory, and the slippage allowance.
`tests/test_pairlock_engine.py` adds re-entry blocked after a hedge (the 06:55 case), profit lock via the engine,
"a market cannot disappear", the entry record, and obsolete-setting warnings.

The full suite passes: 1,253 tests.

## 10. Replay: the same 1,649 BTC windows (09-22 → 10-01) and the same 745 traded markets

The daily-loss stop is off, so A, C, D and E take exactly the same entries. Figures are net USDC, with per traded
market in brackets.

| | train | validation | test | forward | test + forward | all |
|---|---:|---:|---:|---:|---:|---:|
| A entry only | +40.25 (+0.108) | +22.29 (+0.134) | +30.72 (+0.317) | −1.40 (−0.013) | +29.32 (+0.142) | **+91.85 (+0.123), t 0.81** |
| B current v2 (EV hedge) | +52.08 | +54.41 | +27.88 | +11.76 | +39.65 (+0.192) | **+146.14 (+0.196), t 1.34** |
| C recovery (cap 1.00) | = A | = A | = A | = A | = A | **+91.85**: 0 hedges |
| D profit lock | −150.09 | −30.03 | −23.62 | −3.50 | −27.12 (−0.132) | **−207.24 (−0.278), t −2.81** |
| E combined (cap 1.00) | −148.08 | −30.03 | −23.41 | −3.50 | −26.91 (−0.131) | **−205.02 (−0.275), t −2.78** |
| *diag C @ 1.05* | = A | | | | | +91.85: 0 hedges |
| *diag C @ 1.10* | +24.82 | +21.53 | +21.27 | −26.39 | −5.13 | +41.22 |
| *diag C @ 1.15* | +36.64 | +1.58 | −4.24 | −47.52 | −51.76 | −13.54 |
| *diag C @ 1.20* | +7.43 | −34.92 | +21.46 | −59.51 | −38.05 | −65.53 |
| *diag E @ 1.10 / 1.15 / 1.20* | | | | | | −233.77 / −227.18 / −242.36 |

How B's +54 over A breaks down:
- +32.4 from 17 re-entered second cycles (their first legs), which the spec now forbids;
- +21.9 from the hedge leg (52 hedge fills).

Split by exposure instead, B's matched pairs lost −50.7: the average pair cost was 1.10, and 48 of its 52 pairs cost
more than 1. That is the same P&L divided a different way, not a third item.

## 11–19. The requested counts

| | C (cap 1.00) | diag C @ 1.10 | diag C @ 1.15 | diag C @ 1.20 | D profit lock |
|---|---:|---:|---:|---:|---:|
| Markets materially adverse | 559 | 559 | 559 | 559 | 464 |
| Recovery opportunities: markets with a confirmed reversal (episodes) | 488 (3,239) | 488 (2,715) | 488 (1,616) | 488 (1,082) | — |
| Recovery evaluations / with an acceptable size | 3,225 / **0** | 2,707 / 156 | 1,611 / 367 | 1,078 / 462 | — |
| Rejected: pair-cost cap / depth / utility | 3,095 / 130 / 0 | 2,441 / 110 / 1 | 1,178 / 66 / 1 | 572 / 44 / 1 | — |
| Hedges sent / filled (fill rate) | 0 / 0 | 155 / 105 (68 %) | 366 / 297 (81 %) | 461 / 381 (83 %) | 758 / 638 (84 %) |
| Average hedge quantity | — | 9.92 | 9.98 | 9.97 | 9.94 |
| Average pair cost | — | 1.086 | 1.112 | 1.130 | 0.924 |
| Average downside reduction per fill (USDC) | — | 6.72 | 6.23 | 6.00 | 8.12 |
| Settlement P&L of the hedged markets | — | −91.35 | −333.10 | −494.17 | +487.20 |
| ... the same markets with no hedge | — | −40.71 | −227.70 | −336.78 | +786.29 |
| The hedge leg's own P&L | — | **−50.64** | **−105.40** | **−157.39** | **−299.09** |
| Locked profit (profit lock) | — | — | — | — | +481.86 |

The no-hedge baseline (A) is **+91.85**: 745 markets, +0.123 per market, t 0.81, win rate 75 %.

## 20. Accounting and reconciliation

Every settled market in every run reconciles three ways: the book's P&L, the cash view, and a rebuild from the raw
fills. The maximum difference is **0.000000**. In the new code no market has more than one cycle with inventory. In
the cap-1.00 runs (C, D, E) no pair costs more than 1. Every state change went through `check_transition`, which
raises on an invalid transition, and none raised.

One limit on what has been verified: the live engine's reconciliation (engine vs venue P&L, plus the new
"engine holds no cycle" check) is covered by tests only. The paper bot has not been restarted on this code.

## Final verification (section 29)

| # | question | answer | evidence |
|---|---|---|---|
| 1 | Does a recovery hedge require both an adverse move and a confirmed reversal? | **YES** | `RECOVERY_EVALUATION` is reachable only from `REVERSAL_CONFIRMED` (transition table). `watch_state` gives `REVERSAL_CONFIRMED` only when adverse and confirmed. Tests 20, `test_a_loss_alone_never_reaches_a_recovery_evaluation`, `test_a_reversal_without_a_material_loss_is_not_adverse`. |
| 2 | Can the bot hedge merely because the position is losing? | **NO** | At least 2 confirmation signals are enforced in `__post_init__`; the default is 3 of 4. |
| 3 | Can a hedge create a pair with pair cost > 1? | **NO at the configured cap 1.00** | Every decision is capped (`incr_pair_cost ≤ cap`), and so is the FOK limit, so no fill can exceed it. Replay C/D/E: 0 pairs above 1. It is possible only if `MAX_ACCEPTABLE_PAIR_COST` is set above 1, as in the diagnostics. |
| 4 | Are fees included? | **YES** | Per-level taker fee in every candidate and every fill. Test 10. |
| 5 | Is slippage included? | **YES** | Realised: inside the fill price, reported per fill. Decisions: `SLIPPAGE_ALLOWANCE`. Test 11. |
| 6 | Is executable depth used? | **YES** | `vwap_buy` over the live levels with the haircut. Tests 9 and 16. |
| 7 | Are actual fills used? | **YES** | `PairBook` is built only from `FillRecord`s. Tests 5, 11, 16. |
| 8 | Are partial fills handled correctly? | **YES** | Test 5 and the engine's 4-then-6 fill test. |
| 9 | Can pending orders increase inventory? | **NO** | Test 12, v2 test 7, restart test 17. |
| 10 | Are matched and unmatched quantities separate? | **YES** | Tests 3 and 4; the `UNMATCHED_DIRECTIONAL` label; the dashboard "locked vs directional" column. |
| 11 | Can a hedged market immediately start another cycle? | **NO** | Test 13, engine test `test_after_a_recovery_hedge_the_market_never_starts_another_cycle`; replay `markets_with_2_cycles` = 0. |
| 12 | Does settlement reconcile exactly? | **YES** | Replay max difference 0.000000 three ways; tests 14 and 15; engine reconciliation tests. |
| 13 | Can a restart reconstruct the same pair state? | **YES** | Test 17 (in-flight orders are dropped, not counted as stock), and the engine restart test through the bot's positions file. |
| 14 | Are profit-lock and recovery-hedge decisions separate? | **YES** | Separate functions, states, events and counters; `test_no_hedging_in_strategy_a_and_each_mode_only_its_own_decision`. |
| 15 | Does the old `expected_after > expected_before + 0.05` rule still control recovery? | **NO** | `best_hedge` is deleted (`test_the_old_ev_maximising_picker_is_gone`). The v2 setting names are obsolete and trigger a warning. |

## Optional findings (not implemented)

- **The profit lock, as specified, costs money.** It fires in 638 of 745 markets. It locks +0.76 per market but gives
  up about 0.47 of what holding would have made, for −0.40 per market against A, negative in every split. The cause
  is `PROFIT_LOCK_RISK_AVERSION` 0.25: the downside reduction (about 8 USDC per lock) outweighs the lost expected P&L
  in almost every case. The bot buys favourites at about 0.76 that win about 83 % of the time, so locking early gives
  away their upside. One consistent alternative would be a higher `PROFIT_LOCK_MIN_EDGE`, or a near-zero risk
  aversion, which would leave a lock only when the model disagrees with the market.
- **Recovery hedges lose 0.35–0.48 per fill against not hedging** at every cap that lets them fire (1.10–1.20):
  −50.64 / 105, −105.40 / 297, −157.39 / 381. That matches the v2 counterfactual (−0.28 to −0.46). After a confirmed reversal the opposite token is overpriced
  relative to how often it wins.
- **Re-entry was part of B's apparent edge** (+32 of its +54 over A). It came from 17 second cycles that the spec
  now forbids.
- Because the books mirror, a recovery hedge is a partial stop-loss that ties up extra capital. If the intent is
  downside reduction, selling `q` at the bid achieves the same payoff with less capital at risk. It is not part of
  the spec, so it is not implemented.

## Not done: deployment

`ecosystem.pairlock.config.js` holds the new settings (`combined`, cap 1.00). The running bot still runs the v2 code
until it is restarted. That restart empties its trade log (the app has no `--keep-history`) and needs the user's
choice of cap.

## Deployment: strategy B (the user's choice, 2026-10-02 ~16:00 UTC)

The user chose strategy B. The paper bot (`tpb-pairlock-paper`) has run B since 14:19 UTC: the v2 code was what it
loaded at that restart. To make B survive a restart:

- **Frozen code:** `strategy/pairlock_v2.py` and `strategy/pairlock_book_v2.py`, plus the v2 engine restored as
  `strategy/pairlock_engine_v2.py`. Only the class name, a start-up log line and a `strategy: "B"` dashboard field
  differ from the running engine; the decision logic is identical.
- **Selection:** `live.py` picks the engine with `TPB_PAIRLOCK_STRATEGY` (A–E). With no letter set, a v2 hedge mode
  (`conditional` / `unconditional`) means B, so the env pm2 holds now also restarts as B.
- **Config:** `ecosystem.pairlock.config.js` has a `STRATEGY = 'B'` constant. Its B block is exactly the running env
  plus `TPB_PAIRLOCK_STRATEGY=B`. The A/C/D/E block is kept for a switch.
- **Check:** replay B on the package copy reproduces +146.1437, 762 cycles, 80 / 52 hedges, 17 re-entries, recon 0.
  `tests/test_pairlock_engine_v2.py` holds the v2 behaviour tests, the selection, and the config file. The full
  suite passes: 1,263 tests.

B keeps its own rule that a market may start a second cycle after a fully matched pair. That is the behaviour the
spec calls a bug for A/C/D/E, and it is about +32 of B's replay result. The bot was not restarted, so its current
trade log (since 14:19) is kept.

## Book trigger: strategy B with the Polymarket order book instead of the crypto spot shock (2026-10-02 ~16:30 UTC)

The user asked for a "book spot mode instead of crypto spot" for the pair-lock bot. The triangle bot already had one
(`TPB_TRIANGLE_PX_SIGNAL_SOURCE`, docs/book_signal_2026-10-02.md); the pair-lock bot did not. The replay here is
strategy B unchanged -- entry gate, EV hedge, locks, re-entry -- with only the entry trigger swapped
(`scripts/pairlock/replay_book.py`, results in `results/pairlock_book_replay_summary.json`).

The book model is the triangle study's ridge model of the next-1 s UP mid move (cents), on the bot's own 200 ms
samples: mid velocity, ask and bid consumption, new liquidity over 200/400/1000 ms, plus depth imbalance, microprice
and spread change. The replay uses the fit on that study's TRAIN windows (09-22 -> 09-27 13:20), which lie inside this
cache's TRAIN: VALIDATION, TEST and FORWARD are out of sample for the model. On this cache the model's score
correlates +0.36 with the next second's move. The archive keeps each token's own ask levels, and the DOWN book mirrors
the UP book exactly (prices and depths, 75,219 of 75,219 samples), so the cache has both books.

Triggers, each deduplicated to one per direction per second as the crypto proxy is:

- **crypto** -- the spot shock (1 s move >= 2.5 sigma, 3 s agreeing): B as replayed before.
- **book X** -- |book score| >= X cents; the side is its sign.
- **either X** -- crypto when it fires in that second, else book >= X.
- **both C** -- a crypto shock the book showed on the same side (>= C cents) within the last second.

Net USDC (per traded market, t) -- the same 1,649 windows; choose on TRAIN+VALIDATION, judge on TEST+FORWARD:

| trigger | train+validation | test+forward | all | markets | hedges filled | 2nd cycles |
|---|---:|---:|---:|---:|---:|---:|
| crypto (B) | +106.50 (+0.198, t 1.13) | +39.65 (+0.192, t 0.72) | +146.14 (+0.196, t 1.34) | 745 | 52 | 17 |
| book 0.75 | +132.73 (+0.195, t 1.21) | +26.62 (+0.091, t 0.38) | +159.35 (+0.164, t 1.22) | 974 | 43 | 14 |
| book 1.0 | +86.59 (+0.131, t 0.80) | +30.65 (+0.103, t 0.42) | +117.24 (+0.122, t 0.90) | 960 | 39 | 11 |
| book 1.5 | +74.98 (+0.143, t 0.79) | +50.02 (+0.194, t 0.76) | +125.00 (+0.160, t 1.08) | 781 | 35 | 13 |
| book 2.0 | −12.85 (−0.068) | +34.17 (+0.427, t 1.00) | +21.33 (+0.079, t 0.32) | 270 | 14 | 1 |
| either 1.0 | +122.45 (+0.181, t 1.11) | +58.81 (+0.198, t 0.82) | +181.26 (+0.186, t 1.38) | 974 | 43 | 13 |
| either 1.5 | +111.35 (+0.182, t 1.08) | **+83.32 (+0.292, t 1.23)** | +194.67 (+0.217, t 1.58) | 896 | 47 | 16 |
| either 2.0 | +105.78 (+0.204, t 1.14) | +73.38 (+0.356, t 1.34) | +179.15 (+0.247, t 1.67) | 725 | 48 | 14 |
| both 0 | +90.40 (+0.169, t 0.96) | +36.64 (+0.180, t 0.66) | +127.04 (+0.172, t 1.17) | 740 | 51 | 17 |
| both 0.5 | +73.99 (+0.139, t 0.79) | +32.01 (+0.158, t 0.58) | +106.00 (+0.144, t 0.98) | 734 | 50 | 17 |

Per split, book 1.5 is TEST −24.01 and FORWARD +74.03; book 2.0 is VALIDATION −64.38 on 62 markets. Reconciliation
is 0 in every run.

Reading:

- **The book alone does not beat the crypto shock out of sample.** At 0.75 and 1.0 ¢ it trades 30 % more markets for
  half the profit per market (+0.09/+0.10 vs +0.19). At 1.5 ¢ it ties per market but is unstable across the two
  unseen splits. The triangle study found the same: the book predicts the next second better, but it also fires on
  moves the market makers have already priced.
- **Adding the book to crypto ("either") is the only variant that is better on every unseen split**: +0.29–0.36 per
  market on TEST+FORWARD against +0.19, with more markets traded (896 and 725 vs 745; at 2 ¢ the one-per-second
  rule lets a book trigger pre-empt a crypto one in the same second, so the count can fall). Its t on TEST+FORWARD is
  1.2–1.3: not significant. The whole table has no t above 1.7.
- **"both" (the book as a confirmation filter) only removes trades** and lands slightly under B.

Live mode (`strategy/pairlock_booksignal.py`, wired into `strategy/pairlock_engine_v2.py`, default `crypto` = B
exactly as it runs): `TPB_PAIRLOCK_SIGNAL_SOURCE` crypto | book | either | both, `TPB_PAIRLOCK_BOOK_THRESHOLD`
(cents), `_BOOK_CONFIRM`, `_BOOK_CONFIRM_MS`, `_BOOK_MAX_AGE_MS` (a book signal older than 500 ms at the
evaluation is not traded), `_BOOK_MODEL_PATH` (the triangle bot's live model, fitted on all 2,046 windows -- the
replay used the TRAIN-only fit). The trigger scores every 200 ms sample in `observe_point`; the same tick's
`_evaluate_entries` consumes it, so the latency is one tick at most, as in the triangle bot's book mode. A book
trigger builds an entry evaluation that goes through the same pair-lock gate as a crypto one (band, spread,
executable VWAP, EV, P(favourable)); a refusal before the signal stage (stale spot, no strike, time window) stands.
In `book` mode a crypto shock is refused and counted (`crypto_ignored`); in `either` the crypto shock takes its
second and spends any pending book signal, as in the replay. App: `ecosystem.pairlockbook.config.js` ->
`tpb-pairlockbook-paper`, reading B's env and args from `ecosystem.pairlock.config.js`, own tag
`pairlock-book-paper`, `--keep-history`. Not started.

