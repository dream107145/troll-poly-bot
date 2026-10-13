# Rising-recovery paper bot — `opt` mode report (2026-10-13)

Strictly paper trading. No live order path was added or touched.

## 1. Architecture (what existed)

- `src/troll_poly_bot/live.py` — engine loop, paper venue (ask-depth walk, haircut/contention, partial fills,
  FeeModel from the market's `FeeSchedule`, slippage on Fill), feed guard (`guard_paused`), settlement.
- `src/troll_poly_bot/strategy/book_momentum.py` — book recorder (`_record`, 10-s window) + base engine hooks
  (`pair_requests` / `on_pair_submit` / `on_pair_done` / `on_pair_fill_record` / `drop_pair`, `evaluate`).
- `src/troll_poly_bot/strategy/rising_recovery.py` — `RisingRecoveryEngine` (modes `pairs`, `momentum`,
  `spot`, `opt`), `MarketBook` per market (shares U/D, cost C, per-side cost CU/CD, fees, phase, timers).
- `ecosystem.risingrecovery.config.js` — pm2 app `tpb-risingrecovery-paper` (TPB_RTR_* env, 200 USDC,
  own loss limits: daily 20, drawdown 60, per-market 50 sh / 50 USDC).
- `scripts/rising_recovery_replay.py` — chronological replay over recorded books (`data/replay/btc5m_books.pkl`,
  4,142 markets, outcomes from `btc5m_outcomes.json`), IOC at ask + 250 ms, train/val/test 50/25/25.
- `tests/test_rising_recovery.py`, `tests/test_book_momentum.py`.

## 2. Files changed for the spec

| file | change |
|---|---|
| `strategy/rising_recovery.py` | `opt` mode: 2-s BTC momentum entry (±0.3 bp, 10 sh, once, ≤ 30 s), real-time accounting (U, D, CU, CD, fees, PnL_Up / PnL_Down, liquidation PnL from bid depth minus selling fee), dynamic buying on a 6-s timer / 2-s reversal / book rise signal, candidate quantities incl. zero, optimizer = max worst-case then best-case with a minimum improvement, 0.10–0.90 effective-price filter, 0.30–0.40 momentum rule every 3 s with 0.50 target, duplicate suppression, one order per event, stale-book / stale-spot rejection, depth walk with fee + slippage, partial-fill credit, init re-arm when the first order filled nothing, `[RTR_OPT]` JSON log with both-outcome and predicted-winner reports |
| `ecosystem.risingrecovery.config.js` | `TPB_RTR_MODE=opt` + `TPB_RTR_OPT_*` settings (all limits configurable) |
| `tests/test_rising_recovery.py` | 98 tests (momentum thresholds, opening timestamp, timers, reversals, price limits, dynamic sizing, fee + slippage, partial fills, PnL/liquidation/outcome report, duplicate triggers, infeasible targets) |
| `scripts/rising_recovery_replay.py` | spot/opt feed (`_capture_spot`), per-market `FeeSchedule`, §8 metrics (fees, slippage, trades, max drawdown, worst/best-case means, both-positive rate) |

Tests: `98 passed` (rising_recovery + book_momentum).

## 3. Assumptions and unknowns (all configurable)

- Fee: live bot uses the venue's `meta.fee` schedule. The replay cache has no per-market fee → `0.07·p·(1−p)` assumed.
- Selling fee for the liquidation estimate = the same taker schedule (unknown whether the venue charges less on sells).
- Entry cutoff 30 s after the open; minimum improvement 0.05 USDC for a dynamic buy; slippage allowance 1c beyond the ask.
- Trend = 2-s BTC return sign; "rising" = book-signal edge on a token; 0.50 is a projection target, not a limit order.
- Strict "both outcomes positive" is off by default (`TPB_RTR_OPT_REQUIRE_POSITIVE=both` turns it on).
- No re-sends in opt mode except the initial order; venue min order 5 sh.
- Replay fills are IOC at the ask 250 ms later with no queue; real fills are worse (adverse selection seen in every earlier study).

## 4. Backtest (4,070 markets with outcomes, chronological, no look-ahead)

| variant | train /mkt | val /mkt | test /mkt | all /mkt | total | fees | slippage | trades | max DD | worst-case mean | best-case mean | both-positive |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| OPT_deployed | −0.242 | −0.128 | −0.164 | −0.194 | −791 | 711 | −128 | 4,409 | 791 | −0.36 | −0.35 | 16.6 % |
| OPT_strict_both | −0.183 | −0.122 | −0.085 | −0.143 | −583 | 646 | −206 | 4,112 | 597 | −0.27 | +1.07 | 86.7 % |
| OPT_improve_0.5 | −0.241 | −0.128 | −0.165 | −0.194 | −789 | 711 | −128 | 4,405 | 789 | −0.36 | −0.35 | 16.6 % |
| OPT_init_only | +0.131 | −0.015 | −0.116 | +0.033 | +132 | 358 | −18 | 2,200 | 208 | −5.69 | +4.30 | 0 % |

Traded 2,200 of 4,070 markets (the rest had no ±0.3 bp 2-s move or no depth in the first 30 s).

Reading: the optimizer hedges the initial 10 shares flat (U10/D10 at ~1.06 per pair), so every market pays
fees + spread and both outcomes land at about −0.35 to −0.63. Requiring both outcomes positive halves the loss but
is still negative on every split. Initial entry alone is a coin flip (val/test negative). No variant is positive
out of sample; the deployed configuration is the worst of the four.

## 5. Live paper run (opt v1.1, since 14:35:44 UTC)

- Bot fills: 13 (init + hedges), settled 4 markets: −0.64, +1.05, −6.39, +2.29 (the +/− larger ones include the
  user's manual dashboard buys, 28 manual fills since the restart).
- Equity 202.11 at 15:06 UTC, of which the bot's own contribution is ≈ −1.3.
- Feed guard tripped 15:06:41 (Polymarket websocket slow consumer), 30-s cooldown, no orders during it.

## 6. Unresolved

- Strategy is negative in replay and live; the hedge-to-flat behaviour is what the spec's worst-case optimizer
  produces when no token is trending. Not a real-money candidate.
- The liquidation estimate is logged, not yet charted on the dashboard.
- The bot has not been restarted on the final reporting-only code (liquidation/outcome lines) because the user is
  trading manually on it; nothing in the pending diff changes order behaviour.
- `git push` still needs GitHub credentials (gh auth login or an SSH remote).
