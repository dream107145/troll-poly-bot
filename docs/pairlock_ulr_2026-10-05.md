# Unfilled-Leg Recovery (ULR) for tpb-pairlock-paper — implementation report, 2026-10-05

Spec: the user's "Unfilled-Leg Recovery Strategy" of 2026-10-05 (paper only; do not restart the bot; report first).
Status: **implemented, tested (17/17), replayed on 2,831 recorded BTC windows. NOT running** — the bot was not
restarted, its process (14:04 UTC) carries none of the new settings. The replay says the recovery as specified
**loses money on every split and every day** (−0.51 per recovery, t −32); see §5 before enabling it.

## 1. Code changes

| file | change |
|---|---|
| `src/troll_poly_bot/strategy/pairlock_ulr.py` (new, 483 lines) | the pure logic: `ULR` state (per unhedged first leg), the state machine, reason codes, `momentum` / `spot_delta` from the engine's sampled histories, `recovery_size`, `evaluate_entry`, `evaluate_exit`, the lifecycle (`entry_sent/filled/done`, `exit_sent/filled/done`), `settle` (the counterfactual record) |
| `src/troll_poly_bot/strategy/pairlock_recovery.py` | `RecoveryParams.ulr_*` (22 fields, env `TPB_PAIRLOCK_REC_ULR_*`, validated); `RecoveryCycle.ulr` (persisted); fill kind `"ulr"` in `on_fill` (events only); `ulr_leg_pnl()` and the `ulr_leg_pnl` / `ulr_entries` fields of `on_settled` (the strategy-R `recovery_effect` now excludes the ULR leg); ULR orders do not count as strategy-R actions |
| `src/troll_poly_bot/strategy/pairlock_recovery_engine.py` | `manages_exits` property (True only in ULR mode); `_ulr_entry` (evaluated each tick for a holding market whose pair bid rests), `exit_actions` / `on_exit_order` / `on_exit_fill` / `on_exit_result` (the sells, through live.py's existing managed-exit path), the pair hooks start/advance the ULR (`pair_order` skip → unhedged at once, `on_pair_submit` → HEDGE_PENDING, `on_pair_fill_record` → hedged), `to_order` / `entry_result` / `_finalize` / `on_fill_record` handle kind `"ulr"`, settlement records the counterfactual, `_ulr_snapshot` for the dashboard, restore normalises an order in flight at the stop. **In ULR mode the strategy-R arithmetic never buys and the pair bid is never pulled** (runtime precedence, whatever the other flags say) |
| `src/troll_poly_bot/live.py` | one line: a managed `ExitAction` with `tif="IOC"` becomes a fill-and-kill sell (was GTC-or-FOK only) |
| `src/troll_poly_bot/control.py` | Settings group "Pair-lock BTC unfilled-leg recovery": `pairbtc.ulr_enabled, ulr_min_move, ulr_min_time_remaining_s, ulr_initial_ratio, ulr_take_profit, ulr_stop_loss, ulr_expected_move, ulr_exit_before_close_s, ulr_require_spot, ulr_signal_exit, ulr_max_position_size, ulr_max_total_exposure` (this bot only; the recovery bot's `pairlock.*` keys are separate) |
| `ecosystem.pairlock.config.js` | the ULR block (`TPB_PAIRLOCK_REC_ULR_*`, enabled) and, because of it, `PAIR_PULL 1 → 0`, `RECOVERY_ENABLED 1 → 0`. **Takes effect only at the next `pm2 start ecosystem.pairlock.config.js`**; the recovery bot's config builds on `baseB` and is unchanged |
| `tests/test_pairlock_ulr.py` (new) | the spec's 16 cases + the mode-exclusivity case |
| `scripts/pairlock/replay_ulr.py` (new) | the baseline-vs-ULR replay on `data/charts` |
| `results/pairlock_ulr_replay_summary.json`, `..._nosignalexit.json`, `..._window10.json` | the replay outputs |

Unchanged: the conditions entry, its price band, the pair bid at `pair_sum − fill`, the add-on rule, the late buy and
late hedge, P&L accounting (the recovery's fills live in the same `PairBook`, so settlement reconciles as before),
market resolution handling, market discovery, feeds, execution, dashboard plumbing.

## 2. State machine (one per unhedged first leg; `position_id = <slug>:<leg side>:<entry #>`)

```
first leg fills ──pair bid placed──▶ HEDGE_PENDING ──pair bid fills in full──▶ HEDGED (terminal: the arbitrage completed)
                 (no bid possible: price/size) ─┐        │ opposite ask ≥ bid price + RECOVERY_MIN_MOVE (0.08)
                                                ▼        ▼
                                          UNHEDGED_RECOVERY ──all entry conditions──▶ RECOVERY_ENTRY_PENDING
                                                ▲  │                                      │ filled (any qty)
                                                │  └── killed (an attempt; ≤ MAX_ATTEMPTS 3) ◀┘
                                                │                                         ▼
                                                │                                   RECOVERY_OPEN ──TP / SL / SIGNAL_REVERSAL / EMERGENCY──▶ RECOVERY_EXIT_PENDING
                                                │                                         ▲                                                   │ sold out
                                                │                                         └── killed or partial sell (retried after cooldown) ◀┘   ▼
                                                └────────── market resolves (any state) ─────────────────────────────────────────────▶ RECOVERY_CLOSED
                                                                                           (open at the close: RECOVERY_MARKET_RESOLUTION, settles with the market)
```

Entry needs ALL of: `RECOVERY_HEDGE_STILL_POSSIBLE` cleared (the move above), ≥ `RECOVERY_MIN_TIME_REMAINING` left,
opposite ask > 0.50, opposite mid up ≥ 0.01 and first-leg mid down ≥ 0.01 over the momentum window, fresh spot
moving against the first leg (UP leg: spot falling), size ≥ the venue minimum, the book holding the size within
ask + 0.02, `expected move − (spread + entry fee + exit fee + slippage + margin) ≥ 0`, exposure and bankroll caps,
no entry yet on this position. Every evaluation carries one reason code (`[RECOVERY_EVAL] {"kind": "ulr", "code": ...}`
in the log; counted on the dashboard):
`RECOVERY_HEDGE_STILL_POSSIBLE, RECOVERY_HEDGED, RECOVERY_OPPOSITE_BELOW_THRESHOLD, RECOVERY_MOVE_TOO_SMALL, RECOVERY_MOMENTUM_NOT_CONFIRMED,
RECOVERY_SPOT_NOT_CONFIRMED, RECOVERY_SPOT_STALE, RECOVERY_TIME_TOO_SHORT, RECOVERY_EXPECTED_EDGE_TOO_SMALL, RECOVERY_ALREADY_USED, RECOVERY_NO_QUOTE,
RECOVERY_SIZE_TOO_SMALL, RECOVERY_INSUFFICIENT_DEPTH, RECOVERY_EXPOSURE_CAP, RECOVERY_BANKROLL, RECOVERY_COOLDOWN, RECOVERY_ENTRY, RECOVERY_ENTRY_KILLED,
RECOVERY_TAKE_PROFIT, RECOVERY_STOP_LOSS, RECOVERY_SIGNAL_REVERSAL, RECOVERY_EMERGENCY_EXIT, RECOVERY_HOLD_TO_RESOLUTION, RECOVERY_MARKET_RESOLUTION, RECOVERY_EXIT_KILLED`.

Exits use the recovery token's **executable bid**: TP when `bid − entry ≥ RECOVERY_TAKE_PROFIT` and the sale nets a
profit after both fees; SL when `bid − entry ≤ −RECOVERY_STOP_LOSS`; signal reversal when the recovery token's
momentum ≤ −0.01 and the first leg's ≥ +0.01; emergency when fewer than `ulr_exit_before_close_s` are left (none under
the bot's 10 s order floor: the position settles with the market). Sells are fill-and-kill at `bid − 0.02`; a partial
or killed sell is retried after the cooldown. One filled entry per position; no averaging down.

## 3. Parameters (`TPB_PAIRLOCK_REC_ULR_*`; dashboard `pairbtc.ulr_*`)

| name | default | spec name |
|---|---|---|
| ulr_enabled | false (config block: 1) | RECOVERY_ENABLED |
| ulr_min_move | 0.08 | RECOVERY_MIN_MOVE |
| ulr_min_time_remaining_s | 60 | RECOVERY_MIN_TIME_REMAINING |
| ulr_initial_ratio | 0.30 | RECOVERY_INITIAL_RATIO |
| ulr_take_profit | 0.08 | RECOVERY_TAKE_PROFIT |
| ulr_stop_loss | 0.04 | RECOVERY_STOP_LOSS |
| ulr_max_entries | 1 | MAX_RECOVERY_ENTRIES_PER_POSITION |
| ulr_max_attempts | 3 | (orders sent per position; a kill is an attempt) |
| ulr_require_spot | true | RECOVERY_REQUIRE_SPOT_CONFIRMATION |
| ulr_signal_exit | true | RECOVERY_SIGNAL_EXIT |
| ulr_opposite_min_price | 0.50 | (condition A) |
| ulr_momentum_window_s | 3 | (conditions C/D/E window) |
| ulr_momentum_min | 0.01 | (minimum mid move) |
| ulr_expected_move | 0.08 | (the edge gate's expected move) |
| ulr_safety_margin | 0.01 | (edge gate) |
| ulr_max_slippage | 0.02 | (buy limit ask + this; sell limit bid − this) |
| ulr_exit_before_close_s | 20 | RECOVERY_MAX_TIME_TO_RESOLUTION |
| ulr_cooldown_ms | 2000 | |
| ulr_max_position_size | 30 | MAX_RECOVERY_POSITION_SIZE |
| ulr_max_total_exposure | 30 | MAX_RECOVERY_TOTAL_EXPOSURE |
| ulr_max_trades_per_market | 1 | MAX_RECOVERY_TRADES_PER_MARKET |
| (shared) slippage_allowance | 0.005 | the slippage term of the edge gate |

The edge gate's "expected move" is a parameter (the move the trade bets on, = the take profit), not a measurement:
there is no model of the recovery token's next move. The replay is what judges it.

## 4. Tests — `tests/test_pairlock_ulr.py`: **17 passed, 0 failed**

1 completed pair never recovers · 2 hedge fills later → never · 3 unfilled hedge → eligible · 4 opposite > 0.50 but no
momentum → none · 5 confirmed move → entry (6 of 20 shares, FOK at ask + 0.02, not a strategy-R action) · 6 size (ratio,
venue minimum, cap, partly hedged) · 7 TP at the executable bid (fees in) · 8 SL · 9 signal reversal (and off) · 10 never
twice (closed or open) · 11 no entry under 60 s, emergency sell under 20 s, no order under 10 s · 12 stale / contrary
spot blocks · 13 fees and the counterfactual split (settlement reconciles) · 14 partial entry and partial/killed sells
· 15 resolution with the recovery open · 16 restart persistence · + mode exclusivity.
Neighbouring suites after the change: 246 passed (conditions, recovery engine, recover-once, loss limits, dynamic
recovery, v2, controls, dashboard controls, triangle, late settle, paired exit).

## 5. Historical replay — `scripts/pairlock/replay_ulr.py`

2,831 recorded BTC 5-minute windows, 2026-09-22 → 2026-10-05 (every `data/charts/btc-updown-5m-*.json` with a strike
and an outcome). Both arms run the bot's configured conditions rule (20 shares of the higher token at 0.50–0.60,
≥ 120 s left, 0 < BTC − strike < 5; pair bid at 0.97 − fill, held to the close; add-on once all held tokens are
paired) with the paper engine's calibrated execution (arrival +143 ms, 20 % of the touch depth, the quote must
survive 1 s, worse of the two prices; a resting bid fills only when the other side's ask trades strictly through it;
taker fee 7 %·p(1−p), maker fee 0). The late buy / late hedge are left out of both arms; the dashboard overrides
(pair sum 0.95 etc.) are not applied. Splits are chronological (50 / 20 / 15 / 15 %).

| | all | train | validation | test | forward |
|---|---|---|---|---|---|
| windows (traded) | 2,831 (1,805) | 1,415 | 565 | 425 | 425 |
| first legs / pair bids / pair fills | 3,117 / 3,117 / 2,566 | | | | |
| unhedged positions at the close | 627 | 327 | 126 | 89 | 85 |
| **baseline P&L** | **−5,253.29** (unhedged legs −5,765.12) | −2,679 | −1,108 | −786 | −680 |
| **ULR arm P&L** | **−5,941.96** | −2,995 | −1,290 | −886 | −771 |
| arm difference | −688.67 | −315 | −182 | −100 | −91 |
| recovery entries sent / filled | 2,337 / 1,732 | | | | |
| recovered positions | 1,188 | 601 | 251 | 179 | 157 |
| **recovery P&L (= improvement)** | **−608.27** | −289 | −158 | −86 | −75 |
| avg per recovered position / t | −0.512 / −32.3 | | | | |
| wins / losses | 152 / 1,036 | | | | |
| TP / SL / signal exits / emergency / held to close | 170 / 759 / 803 / 0 / 1 | 67/235/299 | 27/104/119 | 28/68/83 | 18/60/79 |
| worst / best recovery | −4.00 / +1.20 | | | | |
| avg loss without → with recovery (recovered positions) | −4.58 → −5.10 | | | | |
| recovery fees | 235.50 | | | | |

The arm difference (−689) is larger than the recovery P&L (−608) because, while a recovery position is open, the market
is not "all paired", so the baseline's add-on buys come later or not at all in the ULR arm (−80).

Sensitivity (not an optimisation; the same conservative values otherwise):

| variant | recovery P&L | avg | t | W / L | TP / SL / signal | forward split |
|---|---|---|---|---|---|---|
| as specified | −608.27 | −0.512 | −32.3 | 152 / 1,036 | 170 / 759 / 803 | −75 |
| signal exit off | −606.11 | −0.510 | −24.4 | 304 / 884 | 435 / 1,294 / 0 | −64 |
| momentum window 10 s | −577.73 | −0.499 | −26.4 | 230 / 929 | 303 / 1,051 / 312 | −69 |

Per day: the ULR arm is below the baseline on all 14 days.

Why it loses, from the records: a recovery's round trip costs ≈ 0.05–0.06 per share (the 1 c spread, two fees of
≈ 1.7 c each, slippage) against a 0.08 target and a 0.04 stop, so it needs well over half of its entries to reach the
target; 13 % did. A 3 s (or 10 s) token-momentum signal in these books is mostly noise at the 1–3 c scale: 759 stops
and 803 signal exits against 170 take-profits. And the hedge is not actually exhausted when the recovery enters:
1,188 recoveries on 627 finally-unhedged legs means roughly half of the recovered pair bids filled later anyway (the
opposite came back through the bid), usually just after the recovery was stopped out — the recovery paid for the
whipsaw the resting bid then profited from.

Note on the baseline itself: the conditions rule as configured loses −1.69 per first leg in this replay; its hedged legs
made +512 and its 627 unhedged legs −5,765 (≈ −9.2 each). The loss problem the spec targets is real; this recovery
design does not reduce it.

## 6. Not done, by the spec

- `tpb-pairlock-paper` was not restarted; nothing was deployed; the recovery bot was not touched.
- Parameters were not optimised. If you want the recovery kept at all, the levers the records point at are a larger
  take-profit relative to the costs, a far stricter entry (fewer, larger moves), or a hedge-exhaustion test that waits
  for the pair bid to be truly out of reach; each of those is a new replay, not a restart.
