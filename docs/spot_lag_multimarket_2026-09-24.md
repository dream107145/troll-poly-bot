# SPOT_LAG across every 5-minute market (2026-09-24)

**Goal.** Evaluate every 5-minute crypto market independently. Give each market one entry when the strategy's
own conditions qualify it. Let positions in different markets coexist, with every risk limit enforced globally.

**Status.** Implemented and tested. It runs in **paper** as `tpb-spotlag-paper` with the multi-market limits.

**15:19 and 15:23 UTC update.** On your instruction ("restart all bots and web server to use new optimized
latency code"), `tpb-spotlag-live` was restarted with a plain `pm2 restart`. It now runs **the new code** (latency
work, per-market limits, exposure fixes, position restore) but keeps **the previous real-money ceilings**:
- 1 position at a time;
- 5 buys an hour;
- 10 USDC open exposure;
- 10 USDC daily loss;
- 5 USDC per order.

The multi-market ceilings (4 positions, 60 buys an hour) reach real money only when you restart that app from the
updated config (section 7).

The second restart carried one fix. Before it, the live bot's guard had latched `BALANCE_DRIFT` at 14:18:48, 3 s
after its first real fill: the venue had not yet moved that buy's 2.05 USDC. It then took no entries until 15:19.
The adapter now skips the cash comparison while a fill is under 60 s old (`CASH_SETTLE_GRACE_S`); real
unexplained moves are still caught at the next quiet check (test
`test_a_settling_fill_is_not_balance_drift_but_a_real_move_still_is`).

---

## 1. Files changed

| file | change |
|---|---|
| `src/troll_poly_bot/strategy/spot_lag.py` | portfolio budget in `SpotLagConfig` (`apply_budget`); per-market attempt and entry counters; global hourly buy budget; sizing inside the remaining global exposure (reduce-to-fit or refuse); restart hooks (`restore_entry`); `[SKIP]` reason texts; counters forgotten a minute after each market's close, never earlier |
| `src/troll_poly_bot/strategy/engine.py` | new refusal reasons `MARKET_ENTERED`, `MARKET_ATTEMPTS`, `HOURLY_BUY_LIMIT`, `EXPOSURE_CAP` |
| `src/troll_poly_bot/risk/limits.py` | working (in-flight) entries count toward exposure at their worst case (limit × size); `capacity_total()` |
| `src/troll_poly_bot/live.py` | applies the budget; passes each order's worst case to the risk book; per-market attempt hook; re-entry only when configured; saves open positions and today's P&L to `data/open_positions<tag>.json` and restores them on start (never across paper and live; in live mode a restored position the venue does not confirm is dropped); structured `[MARKET]`/`[SIGNAL]`/`[ORDER]`/`[POSITION]`/`[SKIP]` logs |
| `src/troll_poly_bot/execution/polymarket.py` | the real-money adapter re-checks open exposure immediately before posting each buy |
| `ecosystem.spotlag.config.js` | explicit budget for every app; `tpb-spotlag-paper` now writes tagged files; `tpb-spotlag-live` raises the micro-live position ceiling (1 → 4) and hourly buy ceiling (5 → 60) |
| `tests/test_spot_lag_multimarket.py` | NEW: 23 tests (section 5) |
| `tests/test_spot_lag.py` | unchanged behaviour; 17 tests still pass |
| `README.md`, `.env.example` | documented |

## 2. Configuration

All values are explicit environment variables. Nothing is hard-coded in the trading path.

| variable | default | before |
|---|---:|---|
| `TPB_SPOT_LAG_MAX_SIMULTANEOUS_POSITIONS` | 4 | 1 (micro-live ceiling) |
| `TPB_SPOT_LAG_MAX_BUYS_PER_HOUR` | 60 | 5 (micro-live ceiling, killed buys included) |
| `TPB_SPOT_LAG_MAX_ENTRIES_PER_MARKET` | 1 | none |
| `TPB_SPOT_LAG_MAX_ATTEMPTS_PER_MARKET` | 3 | none |
| `TPB_SPOT_LAG_ORDER_SIZE_USDC` | 0 (venue minimum: 5 shares) | 5 shares |
| `TPB_SPOT_LAG_MAX_ORDER_USDC` | 5 | 5 |
| `TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC` | 10 | 10 |
| `TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC` | 10 | 10 |
| `TPB_MICRO_MAX_CONCURRENT_POSITIONS` (`tpb-spotlag-live` only) | 4 | 1 |
| `TPB_MICRO_MAX_ORDERS_PER_HOUR` (`tpb-spotlag-live` only) | 60 | 5 |

Bankroll (`--balance 50`), order cap, open exposure and daily loss are **unchanged**.

**Order size.** Every order has been **5 shares**, the venue minimum: about 2.5-2.75 USDC at the usual 0.50-0.55,
never more than 4.75. The 5 USDC figure is the per-order **ceiling**, not the order size. That size is what the
study tested, so it stays the default. Set `TPB_SPOT_LAG_ORDER_SIZE_USDC=5` to buy about 5 USDC worth per order
instead. That roughly doubles the risk per trade and lowers the fill rate, because more depth is needed.

**Why these defaults.**
- **4 positions.** Positions last about 5 s, so they overlap only when several assets shock together, as in the
  14:06:32 market-wide move.
- **Exposure stays 10 USDC.** That fits 3 positions at typical prices, and the global exposure cap binds before
  the position cap does. A 4th fits only below ~0.50.
- **60 buys an hour.** The replay needs about 19 attempts an hour, because roughly 4 in 5 buys are killed, so
  60 never binds in normal conditions. It is a runaway guard. 60 is the bot's existing non-micro default
  (`--max-orders-per-hour`).
- **3 attempts per market.** A killed order does not end a market's chance. The limit stops one market from
  soaking up the hourly budget.

In micro-live the effective value is the tighter of these and the `MicroCaps` ceilings. A plain
`pm2 restart tpb-spotlag-live` keeps its old ceilings (1 position, 5 buys an hour); only a restart from the updated
config file picks up the new ones.

## 3. How markets are tracked

A market is one asset in one 5-minute window. Its id is the slug `<asset>-updown-5m-<window open epoch>`, e.g.
`btc-updown-5m-1790260500` for BTC 14:35-14:40. The bot never keys trading state by asset alone.

| state | keyed by | where |
|---|---|---|
| books, in-flight orders, last entry/exit time, exited flag | market (slug) | `LiveMarket` |
| open position, working entry and its worst case, exposure by market/asset/window | market (slug) | `RiskManager.open`, `.pending`, `.pending_usdc` |
| attempts and filled entries per market, signal de-duplication | market (slug), market + direction | `SpotLagEngine.attempts`, `.trades`, `._last_signal` |
| shares, cost basis, fees | token (one side of one market) | exchange `positions` |
| entry time for the timed exit | token | `SpotLagEngine.entry_ms` |
| hourly buy budget, exposure total, position count, daily P&L, guard, kill switch | global | `SpotLagEngine._buy_times`, `RiskManager`, `Guard` |

Market lifecycle:
1. Discovery registers the current and next window for every asset every 20 s (`[MARKET] ... registered`).
2. The strike is fixed from the 60 s TWAP at the open.
3. Every 200 ms, each live market is evaluated on its own. A market with an in-flight order or a recent attempt
   is skipped, but only that market.
4. On a valid signal (`[SIGNAL]`), a FOK buy goes out (`[ORDER] ... buy submitted`). The market's attempt count
   rises, and its worst case joins the global exposure.
5. A fill opens a position (`[POSITION] ... open`), and the market counts as entered. A kill (`[ORDER] ... not
   filled`) frees the budget, and the market may try again up to its attempt limit.
6. The position is managed on its own 5 s timer and sold (`[POSITION] ... closed`). Other markets keep trading
   meanwhile.
7. A market that has had its entry takes no more (`[SKIP] ... already entered this market`). The next window
   for the same asset is a new market.
8. Settlement books anything still held. The market's counters are forgotten a minute after it closes.

**Audit of single-market assumptions** (what was checked, and what changed):
- **Global position variables, singleton state:** none. The risk book, positions and exits were already keyed per
  market or per token. The per-asset `last_signal` is display-only for this engine.
- **Buy cooldowns:** per market (`ENTRY_RETRY_MS`) and per market + direction (signal de-duplication).
- **Hourly counters:** the adapter's counter was global and set to 5 by micro-live; the ceiling is now 60 for this
  app. The engine's own hourly budget is global, so paper and live now behave the same.
- **Order locks:** per market (`LiveMarket.inflight`).
- **Shared entry flags:** only the guard and kill switch, which are global by design.
- **Exposure: gap found.** In-flight orders were not counted, so several markets signalling in one tick could
  overfill the cap. Fixed in the bot and re-checked in the adapter before each post.
- **Sell logic:** per market, and every exit is independent.
- **P&L accounting:** per market. The daily P&L is global.
- **Persistence: gap found.** Nothing survived a restart. In live mode, restarted positions became "unknown", which
  blocked every market until they settled; they were never sold, and the daily-loss count reset. Fixed as
  described in section 4.
- **PM2:** one process owns one set of files. Paper is now tagged, and the live app has no autorestart.
- **Dashboard:** it lists every position from the state file.
- **Files and concurrency:** one writer per file, and the positions file is replaced atomically. Orders are posted
  one at a time by the adapter's worker, so no asyncio locks are needed.

## 4. How bankroll and exposure are protected

- **Worst case, globally.** Before an order is sent, the risk book checks four things across **all** markets:
  - exposure = open positions + every working entry at its limit price × size, plus the new order's worst case,
    must stay ≤ `MAX_TOTAL_EXPOSURE_USDC`;
  - positions + working entries < `MAX_SIMULTANEOUS_POSITIONS`;
  - the per-asset and per-window caps;
  - the daily loss.
- **Reduce or refuse.** If the order does not fit the remaining exposure or the per-order cap, it shrinks to what
  fits when that is still a valid order (at least 5 shares and 1 USDC). Otherwise it is refused
  (`[SKIP] ...: insufficient bankroll: global exposure budget (x of 10.00 USDC exposure left ...)`). An order is
  never sent past the budget.
- **Re-checked at the venue.** The real-money adapter re-checks open exposure against `max_open_usdc` immediately
  before each post, in addition to its checks when an order is queued.
- **Released on close.** A sell, settlement or abandoned window removes the position from the risk book and frees
  its exposure (tested).
- **Survives a restart.** Positions and today's realised P&L are saved on every open or close and restored on
  start:
  - the exposure counts again, and the market counts as entered;
  - the timed exit runs from the original fill time;
  - the daily-loss budget is not reset by a restart;
  - the loss **streak** is reset: restarting after a streak halt is how you acknowledge it.
- **Unchanged:** the spread limit (3c), the slippage cap (ask + 2c and max entry), venue_lag fill validation, FOK
  only, the kill switch (`data/KILL` stops buys, sells still go out; `data/KILL_ALL` stops everything) and the
  consecutive-loss halt in live mode.

## 5. Tests

`tests/test_spot_lag_multimarket.py`, 23 tests, all passing. Brief item by item:

| # | requirement | test |
|---|---|---|
| 1-3 | multiple markets open at once; BTC does not block ETH; ETH does not block SOL | `test_1_2_3_...`, `test_btc_position_does_not_block_eth_...`, `test_the_loop_enters_several_markets_in_one_tick_...` (3 buys in one tick of the real loop) |
| 4 | one market, one initial entry | `test_btc_position_...` (MARKET_ENTERED after the position closed), `test_one_entry_per_market_and_the_next_window_trades` |
| 5 | 12+ markets an hour | `test_5_twelve_plus_markets_an_hour_...` (14 windows, 14 entries), `test_the_hourly_buy_budget_is_global_and_explicit` |
| 6 | global bankroll limit never exceeded | `test_6_...counts_working_orders...`, `test_6_randomised_many_markets_never_exceed_the_budgets` (300 random steps), `test_the_concurrency_cap_is_global`, `test_order_size_usdc_and_the_per_order_cap` |
| 7 | daily loss | `test_7_the_daily_loss_limit_stops_every_market` |
| 8-9 | spread and slippage | `test_8_9_spread_and_slippage_protection_still_hold` |
| 10 | latency and fill validation | `test_10_11_a_killed_order_...` (the ask runs away while the order is in flight; venue_lag kills it, and no position appears) |
| 11 | a failed order in A does not block B | same test, plus `test_a_market_has_a_limited_number_of_attempts` |
| 12 | a sold position frees exposure | `test_12_a_sold_position_frees_exposure_for_another_market` |
| 13 | restart restores multiple positions | `test_13_a_restart_restores_every_open_position`, `test_restore_refuses_another_modes_file_and_closed_markets`, `test_live_restore_drops_what_the_venue_does_not_hold` |
| 14 | kill switch blocks buys, allows exits | `test_14_the_kill_switch_stops_buys_but_not_exits` |
| 15 | existing tests | full suite: **717 passed** |

Also covered: the budget only tightens (`test_budget_only_tightens_risk_limits`), comes from the environment
(`test_budget_values_come_from_the_environment`), the adapter's pre-post exposure re-check, and the structured log
lines.

## 6. Replay and paper results

### Replay

Archive portfolio replay: 2,688 windows, 6 assets (HYPE excluded). Fills and sells read the archived local book
500 ms late, the model validated against the venue book. One measured-latency draw per order; sell 5 s after the
fill. Every limit is applied in time order across all markets at once.

| period | limits | markets evaluated | buy attempts | fills | fills/h | peak positions | peak exposure | P&L (after fees) | per fill | fees | slippage vs signal ask | qualifying fills missed |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| out of sample (27.9 h) | **new** | 1,678 | 528 | 114 | **4.08** | 4 | 9.75 | +30.75 | +5.39c/sh (t +3.4) | 15.10 | −0.09c | 22 (−1.29): 16 already entered, 3 exposure, 2 attempts, 1 positions |
| out of sample | old | 1,678 | 127 | 30 | 1.07 | 1 | 4.75 | +7.89 | +5.26c/sh | 3.91 | −0.37c | 106 (+21.57): **105 hourly limit**, 1 position |
| holdout only (14.5 h) | **new** | 904 | 234 | 66 | **4.55** | 4 | 9.75 | +15.79 | +4.79c/sh (t +2.0) | 8.26 | −0.21c | 14 (−1.47) |
| holdout only | old | 904 | 65 | 23 | 1.59 | 1 | 4.85 | +6.03 | +5.24c/sh | 2.87 | −0.35c | 57 (+8.29): 55 hourly limit |

- Qualifying signals arrive at about 19-22 an hour, across roughly 72 markets an hour. About 4 in 5 buys are
  killed. So **about 7 % of markets end up with a fill**: most 5-minute markets never produce a qualifying signal,
  and that is the strategy refusing, not a limit.
- The new limits recover almost everything the old ones blocked.
- Exposure peaked at 9.75 of 10 USDC and positions at 4 of 4, so the global limits bound as designed.

**These P&L figures are not new evidence of profit.** They come from the same archive and fill model as the study,
which was optimistic for exactly these trades on the handful of fills that could be checked against the real venue
book. The infrastructure trading more markets does not make the strategy profitable.

### Paper

`tpb-spotlag-paper`, live data, venue_lag fills, the multi-market budget: 14:30-16:05 UTC (1.6 h), with a
1-minute restart at 15:19 for the latency code.

| metric | value |
|---|---|
| markets registered | 147 (126 tradable; HYPE excluded) |
| new spot-lag signals | 379 |
| buy attempts | 12 (7.6/h) |
| fills | **1** (8 % of attempts); 11 FOK kills (the price had moved past the cap by arrival) |
| simultaneous working orders | up to 3 in one tick (15:57:31: ETH, SOL, XRP); peak exposure 6.40 of 10 USDC |
| P&L after fees | −0.52 USDC (the one trade: SOL DOWN bought 0.48, sold 0.41 after 5 s) |
| fees | 0.17 USDC |
| fill price vs the ask at the signal | −1.0c (one fill) |
| signals not sent | 367: token already repriced 119, quote older than 250 ms 69, ask above max entry 65, price outside 0.05-0.95 46, spread 35, no 1 s history 33 |
| errors | 0 |

- **The multi-market machinery works.** Several markets entered in the same tick, working orders counted against
  the exposure budget, and a kill in one market blocked nothing.
- **Fill rate 8 % against about 22 % in the replay.** That is 1 of 12, too few to tell whether live conditions are
  harsher than the model or this was bad luck; it needs about a day of paper.
- **Two live-only filters remove signals the replay never saw.** The quote-age guard skipped 69 and the missing
  1-second history skipped 33. Neither was part of the backtested rule, so check both once there is more data.

**No profitability claim follows from this run.**

## 7. How to run the updated bot

Paper (running now; tagged files, no account, no money):

```
pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-paper
pm2 logs tpb-spotlag-paper | grep -E '\[(MARKET|SIGNAL|ORDER|POSITION|SKIP)\]'
```

Real money: only when you decide. It replaces the running old version and **must** be started from the file so the
new ceilings apply:

```
pm2 delete tpb-spotlag-live
pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-live
```

`pm2 restart tpb-spotlag-live` alone would run the new code with the old micro-live ceilings (1 position, 5 buys an
hour).

## 8. Real-money status

- **Real money is ON** in `tpb-spotlag-live`: new code, the **old ceilings** (1 position, 5 buys an hour, 10 USDC
  open, 10 USDC daily loss). Restarted on your instruction at 15:19 and 15:23 UTC.
- **The multi-market ceilings (4 positions, 60 buys an hour) are NOT live.** Section 7 has the command.
- To stop real money: `pm2 stop tpb-spotlag-live`, or `touch data/KILL` to stop buys while letting sells out.

## 9. Exit timing (added 2026-09-24 ~16:00 UTC)

**Question:** is selling 5 s after the fill the best time, or would another exit earn more?

**Method:**
- The same archive, entries (≥ 3σ jump, net edge ≥ 2c, HYPE excluded) and fills as section 6: about 247 expected
  fills, with every rule applied to the same fills.
- 38 exit rules in six families: fixed holds of 1-30 s; sell once the token has caught up to its expected price;
  catch-up or spot reversal; extend while the spot keeps moving; early exit on a spot reversal; trailing stops.
- The rule is chosen on the training period only; validation, test, the tick period and the holdout are scored
  once.

**Value of holding, by fixed sell time (out of sample, net per share after both fees):**

| sell after | 1 s | 2 s | 3 s | 4 s | 5 s | 6 s | 8 s | 10 s | 15 s | 20 s | 30 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| net | +1.9c | +3.6c | +4.0c | +3.9c | **+4.3c** | +4.4c | +4.0c | +3.9c | +3.4c | +2.1c | +2.5c |

The catch-up is worth most at about 5-6 s. After that the token drifts randomly, and holding longer only adds
risk. The training period alone suggested holding longer, which is why the rule chosen on training ("sell at fair
value, max 20 s") did **worse** out of sample: −1.26c/share against the 5 s timer. Take-profit targets, trailing
stops and early exits on a spot reversal were all flat or worse.

**Adopted:** sell at 5 s, **unless the last second's spot move is still ≥ 0.5σ in the position's direction; then
keep holding, at most to 10 s.** It was the only rule ahead of the timer in both training (+0.32c/share, t 1.8) and
out of sample (+0.24c, t 1.2; 3 of 4 periods). The reason: while the spot is still moving, the token has not
finished repricing. It is small and not statistically proven, and it barely changes risk (average hold 5.6 s →
5.9 s).

- Settings: `TPB_SPOT_LAG_EXTEND_Z1=0.5` and `TPB_SPOT_LAG_EXTEND_MAX_HOLD_S=10`. Set `EXTEND_MAX_HOLD_S=5` for the
  plain timer.
- Test: `tests/test_spot_lag.py::test_the_exit_waits_while_the_spot_is_still_moving_our_way`.
- It is the code default. The paper bot picks it up on its restart. **The real-money bot picks it up only when it is
  next restarted** (not done).

## Paper entry settings for more orders and fills (2026-09-24, live in paper from 21:04 UTC)

**Why fills were rare.** From 16:40 to 21:00 the paper bot sent 85 orders and 5 filled (6 %). All 5 were winners (+$4.33). The other 80 were killed because the ask had moved above the order's limit (ask + 2c) before it arrived. The replay shows the same collapse in the freshest period, 16:30–19:10 UTC: 8 % filled.

**The study.** `scripts/analyze_repriced_momentum.py slsim` replays this bot's gates, price cap, fill model, exit and budget on the archive, with signals from `scripts/research_repriced_momentum.py spotlag`. It covered 48 settings:
- price limit ask + 2c / ask + 4c / up to the max-entry price;
- minimum edge 2c / 1c;
- maximum spread 3c / 5c;
- HYPE excluded / included;
- shock threshold 3σ / 2.5σ.

Full table: `data/replay/rm_research/slsim.md`. All 48 were positive in training and validation, and 47 on the unseen periods. The **2.5σ threshold** and **HYPE** pushed the unseen results toward zero.

| all five periods | orders / h | fills / h | fill | profitable fills / h | net per share | USDC / day |
|---|---:|---:|---:|---:|---:|---:|
| before (ask + 2c, edge 2c, spread 3c) | 20.1 | 5.0 | 25 % | 3.5 | +8.1c | +48 |
| **now (ask + 4c, edge 1c, spread 5c)** | 26.3 | 6.9 | 26 % | 4.2 | +5.1c | +42 |
| more aggressive (max-entry cap, edge 1c, spread 5c) | 26.0 | 7.5 | 29 % | 4.5 | +4.4c | +40 |

- **Per period (now):** +6.6 / +3.4 / +1.1 / +4.2 / +13.9c per share, positive in every one.
- **The most recent period (16:30–19:10 UTC):** fills rise from 1.5 to 3.6 an hour (8 % → 14 %), and dollars per day from +28 to +61.
- **Why not the more aggressive setting:** it was about zero in two periods.
- **The trade-off:** more volume at a lower edge per share. On training data alone, the old rule earns more per day.

**Scope.** These settings apply to paper only, `tpb-spotlag-paper` in `ecosystem.spotlag.config.js`. `tpb-spotlag-live` and `tpb-spotlag-shadow` keep the tested rule, and the real-money bot was not restarted.

**Price ceiling 0.60 (paper only, live from 2026-09-25 00:46 UTC, the user's instruction).**

Replay on the original rule, all signals that passed the live gates:

| token price (ask) | training + validation | unseen |
|---|---:|---:|
| 0.05–0.40 | +15.2c | +11.2c |
| 0.40–0.60 | +8.7c | +11.7c |
| 0.60–0.80 | +3.2c | −0.3c |
| 0.80–0.95 | −1.1c | −10.0c |

(net per share)

**Waiting for a lower price does not help.** A resting buy below the ask, or buying on a dip, loses 4–7c per share: the token only comes back down when the move has failed. Details are in `data/replay/rm_research/spotlag_lowbuy.json`, produced by the scratchpad `lowbuy.py`.

With the paper bot's settings (ask + 4c, edge 1c, spread 5c), `slsim` gives:

| ceiling | orders / h | fills / h | net per share | USDC / day | per period |
|---|---:|---:|---:|---:|---|
| 0.95 | 26.3 | 6.9 | +5.1c | +42 | +6.6 / +3.3 / +1.1 / +4.2 / +13.9 |
| **0.60** | 17.3 | 4.3 | **+10.4c** | **+53** | +11.7 / +6.7 / +5.5 / +13.8 / +23.2 |

The ceiling was found after looking at every period. It holds in both halves, but the most expensive bucket is small.
