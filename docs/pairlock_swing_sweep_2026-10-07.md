# Swing recovery parameter sweep — 2026-10-07

**Verdict: DO NOT TRADE.** No robust positive-EV parameter region exists under realistic execution:

* **Test period (05–07 Oct):** 0 of the 1,078 parameter sets made money.
* **All three periods:** none of them is positive in train, validation and test.
* **Where in-sample profit comes from:** the best sets made money in train and validation only by shrinking the recovery towards nothing. Their profit was the first buy itself: the token leading about 20 s after the open went on to win 62–64 % of markets in train and validation, against an entry price of about 0.59. That fell to 55 % in the test period.
* **The recovery logic itself:** it lost money in every period and every configuration with meaningful recovery sizes.

Files:
* `scripts/pairlock/replay_swing_sweep.py` — the sweep.
* `scripts/pairlock/swing_sweep_neighbourhood.py` — the stability check.
* `scripts/pairlock/swing_sweep_report.py` — the tables below.
* `results/pairlock_swing_sweep.json` — every parameter set, by period.
* `results/pairlock_swing_sweep.csv` — 1,318 rows: 1,078 parameter sets + 240 stress runs.
* `results/pairlock_swing_sweep_markets.json.gz` — per-market rows for the baselines and the top 20.
* `results/pairlock_swing_sweep_neighbourhood.json`.
* Tests: `tests/test_pairlock_swing_sweep.py` (20).

Nothing in the bot, its configuration or its defaults was changed by this work.

## Method

* **Data:** every recorded BTC 5-minute market, 2026-09-22 15:40 → 2026-10-07 19:05 UTC. That is 3,219 markets with best bid/ask and depth within 2 ¢ of the touch, about 5 samples a second, and the venue's outcome. The data is a frozen snapshot, so the sweep is reproducible.
* **Periods (chronological, never shuffled):**

  | Period | Dates | Markets |
  |---|---|--:|
  | Train | 22 Sep → 01 Oct | 2,022 |
  | Validation | 02 Oct → 04 Oct | 785 |
  | Test | 05 Oct → 07 Oct | 412 |

* **Decisions:** the bot's own code (`pairlock_swing.entry_decision` / `evaluate` / `observe`) runs unchanged on every sample, with each candidate's parameters. The state machine and the definition of a swing are untouched.
* **Fills:** the existing realistic model (`replay_swing.execute`, the paper bot's "calibrated" scenario):
  * the order arrives 143 ms after the decision and walks the recorded asks then;
  * 80 % of the best-ask level has already been taken by faster traders;
  * fill-and-kill up to the limit price;
  * the book 1 s later must still fill the order at or under the limit, otherwise it is killed;
  * the fill is charged at the worse of the two VWAPs;
  * the venue fee is 0.07·p·(1−p) per share.

  No fixed-price fills, no assumption that UP + DOWN = 1, no infinite depth.
* **Policies the bot does not have** (tested only here, as filters on the bot's decisions):
  * recovery price limit on the decision-time executable VWAP: `reduce` buys the largest size whose VWAP ≤ the limit; `reject` waits;
  * break-even limit = the price at which, if the recovery side wins, the market ends at 0 after costs (computed from inventory, no model);
  * hedge ratio of the inventory gap: `once` at the start of hedging, or `each` at every later swing;
  * hedge price limit.
* **Fast path:** a held market that cannot act jumps to the next sample where the swing condition holds. Tests check it is decision-identical to sampling every point: it reproduces the existing replay market by market. The current baseline gives −1,398.7 over the 3,219 markets, versus −1,399.2 over 3,216 in today's earlier backtest.
* **Search, in stages:**
  1. baselines;
  2. one dimension at a time — entry (72), trigger × hysteresis (70), sizes with hedge (104), sizes without hedge (64), hedge ratio / limit / mode (50), recovery price (28), capital (7);
  3. a factorial of each dimension's best values (324);
  4. a local search around the top 10, repeated 4 times (364);
  5. 12 stress tests on the top 20.
* **Selection never reads the test period.** Ranking score, from train and validation only:

  `score = min(P&L/market train, P&L/market validation) − 0.001·|max drawdown| − 0.002·|1st-percentile market| − 0.0005·95th-percentile capital − 0.20·share of losing days`

  The test results are reported for the ranked candidates without re-ranking.

## A. Current strategy (baseline A: 20–30 s, 0.55–0.60, 10 / 24 / 80, trigger 0.70, hysteresis 0.04, hedge 100 %, cap 125)

| | Train | Validation | Test | All |
|---|--:|--:|--:|--:|
| P&L per market offered | −0.361 | −0.396 | −0.870 | **−0.435** |
| Total | −729.5 | −310.8 | −358.4 | **−1,398.7** |
| First leg alone (held to settlement) | +7.0 | +78.0 | −71.8 | +13.2 |
| Effect of the recovery logic | −736.4 | −388.8 | −286.6 | −1,411.9 |

Win rate 73.5 %, max drawdown −1,434, worst market −78.0, 95th-percentile capital 124. By bucket:

| Bucket | Markets | P&L | Per market |
|---|--:|--:|--:|
| Swing 0 | 641 | +2,599 | +4.05 |
| Swing 1 | 527 | −181 | −0.34 |
| Swing 2 | 190 | +287 | +1.51 |
| Swing 3+ | 93 | −4,103 | −44.11 |

The other baselines are all negative in every period:

| Baseline | Per market (train / val / test / all) |
|---|---|
| B: no hedge | −0.26 / −0.02 / −0.98 / −0.30 |
| C: 10 / 24 / 40 | −0.23 / −0.24 / −0.71 / −0.29 |
| D: 10 / 15 / 30 | −0.16 / −0.17 / −0.52 / −0.21 |
| E: 10 / 15 / 20 | −0.13 / −0.10 / −0.47 / −0.16 |

Every reduction in recovery size helps. The recovery is not what pays.

## B–C. Top 20 and the best robust candidate

The top-20 table is in the appendix. The leaders form one family:
* entry 15–30 s at 0.55–0.62;
* tiny recoveries (swing 1 = 8–15 shares, swing 2 = 1.5× that);
* the trigger at the top of the grid (0.78, so a swing is recognised late);
* hysteresis 0.08–0.10;
* no hedge (or 25 %);
* recovery VWAP limit 0.77;
* cap 100 (never binding).

The search pushed every recovery-related setting towards its least-recovery extreme.

**Best candidate (rank 1):** entry 15–25 s, band 0.55–0.62, 10 / 8 / 12 shares, trigger 0.78, hysteresis 0.10, no hedge, recovery VWAP ≤ 0.77 (reduce), cap 100. It was selected because it has the highest train + validation score. All 11 of its one-step neighbours are also positive in train and validation (a broad region in-sample).

| | Train | Validation | Test | All |
|---|--:|--:|--:|--:|
| P&L per market offered | +0.073 | +0.145 | **−0.252** | +0.049 (t +0.9) |
| Total | +147.8 | +113.8 | −103.8 | +157.9 |
| First leg alone | +191.0 | +145.7 | −117.2 | +219.5 |
| Effect of the recovery logic | −43.2 | −31.8 | +13.4 | −61.6 |
| Leader at entry won | 62.5 % | 64.0 % | 55.0 % | |

* **Profit source:** its in-sample profit is the entry, and the recovery subtracts from it. Its swing-1 bucket loses −2,969 (616 markets): in those markets the other side took over and the 8-share recovery only softens the loss on the first 10 shares.
* **Neighbourhood:** median all-period P&L per market +0.04; **0 of 11 neighbours positive in the test period.**

## D. Out of sample

Of 1,078 parameter sets:

| Positive in | Parameter sets |
|---|--:|
| Train | 341 |
| Validation | 665 |
| Test | **0** |
| All three | **0** |

Over the whole period only 23 sets are positive in total. The best of them is +0.049 per market (t +0.9: not distinguishable from zero after 1,078 tries).

Top-5 neighbourhoods (every one-step neighbour run):

| Rank | Neighbours | Positive train & val | Positive test | Median score | Median all-period P&L/market |
|--:|--:|--:|--:|--:|--:|
| 1 | 11 | 100 % | 0 % | −0.162 | +0.042 |
| 2 | 11 | 100 % | 0 % | −0.162 | +0.042 |
| 3 | 14 | 64 % | 0 % | −0.233 | −0.032 |
| 4 | 16 | 69 % | 0 % | −0.229 | −0.033 |
| 5 | 12 | 83 % | 0 % | −0.189 | +0.004 |

## E. P&L by swing

See A (current) and C (rank 1); every parameter set's buckets are in the CSV (`swing0_pnl` … `swing3plus_pnl`, `swing*_avg`).
* **Current strategy:** a swing-0 market makes about +4, and swing 3+ loses about −44.
* **Rank 1:** it avoids swing-3 losses (32 markets, −6.7 each) only because its recoveries are too small to build a large position.

## F. Execution economics: trigger versus fill

| Trigger (best ask that recognises the swing) | 0.60 | 0.64 | 0.68 | 0.70 | 0.72 | 0.74 | 0.78 |
|---|--:|--:|--:|--:|--:|--:|--:|
| Swing-1 fill VWAP | 0.648 | 0.688 | 0.723 | 0.741 | 0.758 | 0.774 | 0.810 |
| Swing-2 fill VWAP | 0.661 | 0.702 | 0.745 | 0.767 | 0.782 | 0.794 | 0.838 |
| P&L per market (all) | −0.859 | −0.555 | −0.411 | −0.435 | −0.331 | −0.294 | −0.196 |

* **Fill versus trigger:** fills land about 4 ¢ (swing 1) to 6 ¢ (swing 2) above the trigger, at every trigger. A swing is recognised while the new leader is still moving, and the order meets the book 143 ms later.
* **Current strategy's averages:** the swing-1 trigger ask is 0.720 and its fill 0.741. For swing 2 they are 0.732 and 0.767; for the hedge, 0.753 and 0.767.
* **Fill rates:** entry 86 %, swing 1 80 %, swing 2 70 %, hedge 48 %.
* **Recovery price limits:** a VWAP limit of 0.65–0.70 does buy cheaper (swing 1 at 0.62–0.69) but is no better (−0.42 to −0.46 per market). The cheap fills are the swings that reverse before they would have paid.
* **Break-even limit:** −0.41 per market.

## G. Sensitivity (each row varies one setting of the current strategy; full tables in the appendix)

* **Swing 1 size:** 8 → 30 shares: −0.38 → −0.45 per market. Smaller is better.
* **Swing 2 size:** 20 → 100 shares: −0.20 → −0.41. Smaller is better.
* **Swing 2 as a multiple of swing 1:** 1.5× → 3.5×: −0.27 → −0.41.
* **Trigger:** 0.60 → 0.78: −0.86 → −0.20. Later is better, because it recovers less often.
* **Hysteresis:** 0.02 → 0.06 gives identical results (the 0.70 trigger already implies a large lead); 0.10 gives −0.43.
* **Hedge ratio:** 0 % → 100 %: −0.30 → −0.44. Train and validation are better without a hedge; test is slightly better with one (−0.98 vs −0.87).
* **Hedge price limit:** −0.42 to −0.45 at any limit.
* **Capital cap:** 100 gives −0.35, 125 gives −0.44, 150+ gives −0.46. A lower cap is better because it cuts the hedge.
* **Entry start:** 15–40 s gives −0.43 to −0.52; 20 s is best for the current sizes.
* **Entry band:** 0.55–0.58 / 0.55–0.60 give −0.43; 0.55–0.65 gives −0.52; 0.50–0.65 gives −0.59. Wider bands are worse.
  * The band now set on the Variables page (0.50–0.80) is outside the sweep. The trend says it is worse still.

## H. Stress tests (top 20, P&L per market over all periods)

**Rank 1** (+0.049 unstressed):

| Stress | Fill price +1 ¢ | +2 ¢ | +3 ¢ | Slippage +0.5 ¢ | +1 ¢ | +2 ¢ | Fill rate −10 % | −20 % | −30 % | Fees +10 % | +25 % | +50 % |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| P&L / market | −0.020 | −0.088 | −0.157 | +0.029 | −0.020 | −0.075 | +0.051 | +0.049 | +0.031 | +0.038 | +0.022 | −0.006 |

A one-cent increase in execution cost turns the best in-sample result negative. Every other top-20 set is negative or within ±0.015 per market before stress.

## I. Recommendation

**DO NOT TRADE.**
* No parameter region of the swing recovery is positive out of sample.
* The only in-sample profit is the leader-at-entry bet, which reversed in the test period.
* The recovery and hedge logic loses money in every period at every size that matters.
* The best in-sample result does not survive one cent of extra execution cost.

**Exact parameter changes:** none recommended for trading.

If the paper bot keeps running only to collect data, the least-damaging settings in this sweep are rank 1's:

| Setting | Value |
|---|---|
| Swing 0: not before / not after | 15 / 25 s |
| Entry VWAP | 0.55–0.62 |
| Swing 1 / swing 2 shares | 8 / 12 |
| Swing recovery price (trigger) | 0.78 |
| Leader hysteresis | 0.10 |
| Two-sided hedge | off |
| Max capital per market | 100 |

The recovery VWAP limit of 0.77 has no setting in the bot (it was tested only here). In the test period these settings still lost −0.25 per market.

## Limits of this study

* **Book depth:** depth beyond 2 ¢ of the best ask is not recorded and is taken as zero. The 2-¢ depth is split evenly over three price levels, an assumption.
* **Fill model:** it was calibrated on spot-lag orders. Today's two live paper markets matched its behaviour (swing 1 killed at 0.73, refilled at 0.75), but it is not validated for 80-share orders.
* **Sample size:** the test period is 412 markets over 2.8 days, and the whole history covers one 16-day regime. 1,078 configurations were tried, so in-sample maxima are biased upward.
* **Local search:** it reached its cap of 4 rounds. The leaders it found are near-duplicates of one family.
* **Policies:** the hedge ratio, price limits and break-even rule are filters applied in the sweep. The bot does not have them.

## Appendix — generated tables (scripts/pairlock/swing_sweep_report.py)

Data: 3219 markets 2026-09-22 15:40 .. 2026-10-07 19:05 UTC (last btc-updown-5m-1791399900); periods {'train': 2022, 'validation': 785, 'test': 412}; 1078 parameter sets + 240 stress runs in 79 min.

## Baselines

| Baseline | Score | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Total | Traded | Win % | Max DD | Worst | P95 cap | S0 | S1 | S2 | S3+ (markets: P&L) |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|---|---|---|
| A current (10/24/80, hedge 100%, cap 125) | -1.831 | -0.361 | -0.396 | -0.870 | -0.435 | -1398.7 | 1451 | 73.5% | -1434 | -78.0 | 124 | 641: +2599 | 527: -181 | 190: +287 | 93: -4103 |
| B no hedge | -1.307 | -0.264 | -0.016 | -0.983 | -0.296 | -952.1 | 1451 | 75.1% | -986 | -78.4 | 89 | 641: +2599 | 527: -181 | 190: +287 | 93: -3656 |
| C 10/24/40 | -1.160 | -0.227 | -0.241 | -0.709 | -0.292 | -940.1 | 1451 | 65.1% | -948 | -38.7 | 71 | 641: +2599 | 527: -181 | 190: -1111 | 93: -2246 |
| D 10/15/30 | -0.899 | -0.163 | -0.170 | -0.522 | -0.210 | -677.6 | 1451 | 50.7% | -684 | -31.2 | 56 | 641: +2599 | 527: -1257 | 190: -228 | 93: -1791 |
| E 10/15/20 | -0.676 | -0.128 | -0.099 | -0.465 | -0.164 | -527.9 | 1451 | 44.3% | -532 | -21.6 | 42 | 641: +2599 | 527: -1257 | 190: -579 | 93: -1291 |

## Top 20

| Rank | Entry | S1 | S2 | Trigger | Hyst | Hedge | Recovery limit | Cap | Score | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Test P&L | Max DD | P95 capital | Neighbours >0 |
|---:|---|--:|--:|--:|--:|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 1 | 15–25s 0.55–0.62 | 8 | 1.5x | 0.78 | 0.10 | 0% | ≤0.77 reduce | 100 | -0.145 | +0.073 | +0.145 | -0.252 | +0.049 | -103.8 | -169 | 17 | 100% of 2 |
| 2 | 15–25s 0.55–0.62 | 8 | 1.5x | 0.78 | 0.10 | 0% | ≤0.77 reject | 100 | -0.145 | +0.073 | +0.146 | -0.252 | +0.049 | -103.8 | -168 | 17 | 100% of 2 |
| 3 | 20–30s 0.55–0.62 | 15 | 1.5x | 0.78 | 0.08 | 0% | ≤0.77 reduce | 100 | -0.166 | +0.023 | +0.051 | -0.440 | -0.029 | -181.3 | -256 | 30 | 64% of 14 |
| 4 | 20–30s 0.55–0.62 | 15 | 1.5x | 0.78 | 0.08 | 25% once | ≤0.77 reduce | 100 | -0.167 | +0.023 | +0.044 | -0.437 | -0.031 | -180.2 | -261 | 31 | 100% of 2 |
| 5 | 20–30s 0.55–0.62 | 8 | 1.5x | 0.78 | 0.10 | 0% | ≤0.77 reduce | 100 | -0.179 | +0.032 | +0.076 | -0.232 | +0.009 | -95.6 | -149 | 19 | 83% of 12 |
| 6 | 20–30s 0.55–0.62 | 8 | 1.5x | 0.78 | 0.10 | 0% | ≤0.77 reject | 100 | -0.179 | +0.032 | +0.077 | -0.232 | +0.009 | -95.6 | -148 | 19 | 83% of 12 |
| 7 | 20–30s 0.55–0.62 | 10 | 1.5x | 0.78 | 0.08 | 0% | ≤0.77 reduce | 100 | -0.180 | +0.022 | +0.067 | -0.383 | -0.019 | -157.8 | -225 | 23 | 71% of 14 |
| 8 | 20–30s 0.55–0.62 | 10 | 1.5x | 0.78 | 0.08 | 0% | ≤0.77 reject | 100 | -0.180 | +0.022 | +0.069 | -0.382 | -0.018 | -157.5 | -224 | 23 | 71% of 14 |
| 9 | 20–30s 0.55–0.62 | 10 | 1.5x | 0.78 | 0.08 | 25% once | ≤0.77 reduce | 100 | -0.180 | +0.022 | +0.062 | -0.383 | -0.020 | -157.8 | -230 | 23 | 75% of 16 |
| 10 | 20–30s 0.55–0.62 | 10 | 1.5x | 0.78 | 0.08 | 25% once ≤0.78 | ≤0.77 reduce | 100 | -0.180 | +0.022 | +0.063 | -0.383 | -0.020 | -157.8 | -229 | 23 | 76% of 17 |
| 11 | 20–30s 0.55–0.62 | 10 | 1.5x | 0.78 | 0.08 | 25% once | ≤0.77 reject | 100 | -0.180 | +0.022 | +0.064 | -0.382 | -0.019 | -157.5 | -228 | 23 | 75% of 16 |
| 12 | 20–30s 0.55–0.62 | 10 | 1.5x | 0.78 | 0.08 | 25% once ≤0.70 | ≤0.77 reduce | 100 | -0.180 | +0.022 | +0.063 | -0.383 | -0.020 | -157.8 | -229 | 23 | 76% of 17 |
| 13 | 20–30s 0.55–0.62 | 10 | 1.5x | 0.78 | 0.08 | 25% once ≤0.78 | ≤0.77 reject | 100 | -0.180 | +0.022 | +0.064 | -0.382 | -0.019 | -157.5 | -228 | 23 | 76% of 17 |
| 14 | 20–30s 0.55–0.62 | 10 | 1.5x | 0.78 | 0.08 | 25% once ≤0.70 | ≤0.77 reject | 100 | -0.180 | +0.022 | +0.064 | -0.382 | -0.019 | -157.5 | -228 | 23 | 100% of 1 |
| 15 | 20–30s 0.55–0.62 | 8 | 1.5x | 0.78 | 0.08 | 0% | ≤0.77 reduce | 100 | -0.181 | +0.021 | +0.082 | -0.355 | -0.013 | -146.1 | -213 | 19 | 77% of 13 |
| 16 | 20–30s 0.55–0.62 | 8 | 1.5x | 0.78 | 0.08 | 0% | ≤0.77 reject | 100 | -0.181 | +0.021 | +0.083 | -0.354 | -0.012 | -145.8 | -212 | 19 | 77% of 13 |
| 17 | 20–30s 0.55–0.62 | 15 | 1.5x | 0.78 | 0.08 | 0% | ≤0.77 reject | 100 | -0.182 | +0.022 | +0.054 | -0.439 | -0.029 | -181.0 | -253 | 30 | 100% of 1 |
| 18 | 20–30s 0.55–0.62 | 12 | 1.5x | 0.78 | 0.08 | 0% | ≤0.77 reduce | 100 | -0.182 | +0.023 | +0.059 | -0.412 | -0.024 | -169.6 | -241 | 27 | 71% of 14 |
| 19 | 20–30s 0.55–0.62 | 12 | 1.5x | 0.78 | 0.08 | 0% | ≤0.77 reject | 100 | -0.183 | +0.021 | +0.061 | -0.411 | -0.024 | -169.3 | -239 | 26 | 71% of 14 |
| 20 | 20–30s 0.55–0.62 | 8 | 2x | 0.78 | 0.10 | 0% | ≤0.77 reduce | 100 | -0.183 | +0.035 | +0.067 | -0.251 | +0.006 | -103.5 | -154 | 22 | 100% of 2 |

**Baseline A**: `current strategy`

| Period | Markets | Traded | P&L | /market | /traded | Median | Win % | Max DD | Worst | P1 | P5 | Losing streak | Avg cap | P95 cap |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| train | 2022 | 908 | -729.5 | -0.361 | -0.803 | +3.83 | 74.8% | -828 | -78.0 | -58.4 | -35.0 | 5 | 29.9 | 124 |
| validation | 785 | 357 | -310.8 | -0.396 | -0.871 | +3.83 | 72.3% | -315 | -68.3 | -56.9 | -34.9 | 6 | 28.4 | 124 |
| test | 412 | 186 | -358.4 | -0.870 | -1.927 | +1.17 | 69.9% | -371 | -60.7 | -55.4 | -35.0 | 5 | 36.3 | 124 |
| all | 3219 | 1451 | -1398.7 | -0.435 | -0.964 | +3.83 | 73.5% | -1434 | -78.0 | -57.2 | -35.0 | 6 | 30.3 | 124 |

| Bucket | Markets | Share | P&L | Per market |
|---|--:|--:|--:|--:|
| Swing 0 | 641 | 44.2% | +2598.5 | +4.05 |
| Swing 1 | 527 | 36.3% | -181.3 | -0.34 |
| Swing 2 | 190 | 13.1% | +286.7 | +1.51 |
| Swing 3+ | 93 | 6.4% | -4102.7 | -44.11 |

| Order class | Orders | Fill rate | Partial | Shares | Avg VWAP | Avg trigger ask |
|---|--:|--:|--:|--:|--:|--:|
| entry | 1703 | 0.86 | 9 | 14484 | 0.5752 | — |
| swing 1 | 1054 | 0.80 | 25 | 19365 | 0.7406 | 0.7195 |
| swing 2 | 475 | 0.70 | 33 | 22223 | 0.7667 | 0.7324 |
| hedge | 259 | 0.48 | 27 | 4559 | 0.7671 | 0.7530 |

Entry: 1589 markets offered an entry, 1451 filled, 138 missed (killed / not filled); capital-bound markets 88; P&L per USDC of capital -0.0318; max inventory 180 sh; first leg alone +13.2.

**Rank 1**: `e15-25s 0.55-0.62 | 10/8/1.5x | trig 0.78 hyst 0.10 | h0% rec<=0.77/reduce | cap 100`

| Period | Markets | Traded | P&L | /market | /traded | Median | Win % | Max DD | Worst | P1 | P5 | Losing streak | Avg cap | P95 cap |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| train | 2022 | 1040 | +147.8 | +0.073 | +0.142 | +3.64 | 57.6% | -131 | -13.8 | -8.5 | -6.2 | 6 | 8.5 | 13 |
| validation | 785 | 420 | +113.8 | +0.145 | +0.271 | +3.64 | 59.8% | -63 | -13.4 | -9.6 | -6.2 | 5 | 8.3 | 18 |
| test | 412 | 209 | -103.8 | -0.252 | -0.497 | +0.50 | 51.7% | -138 | -14.2 | -12.7 | -6.3 | 8 | 8.9 | 19 |
| all | 3219 | 1669 | +157.9 | +0.049 | +0.095 | +3.64 | 57.4% | -169 | -14.2 | -10.6 | -6.3 | 8 | 8.5 | 17 |

| Bucket | Markets | Share | P&L | Per market |
|---|--:|--:|--:|--:|
| Swing 0 | 884 | 53.0% | +3456.6 | +3.91 |
| Swing 1 | 616 | 36.9% | -2968.5 | -4.82 |
| Swing 2 | 137 | 8.2% | -115.6 | -0.84 |
| Swing 3+ | 32 | 1.9% | -214.7 | -6.71 |

| Order class | Orders | Fill rate | Partial | Shares | Avg VWAP | Avg trigger ask |
|---|--:|--:|--:|--:|--:|--:|
| entry | 1889 | 0.89 | 10 | 16661 | 0.5895 | — |
| swing 1 | 637 | 0.87 | 2 | 4427 | 0.7471 | 0.7941 |
| swing 2 | 131 | 0.66 | 2 | 1014 | 0.7336 | 0.8117 |
| hedge | 0 | — | 0 | 0 | — | 0.8250 |

Entry: 1798 markets offered an entry, 1669 filled, 129 missed (killed / not filled); capital-bound markets 0; P&L per USDC of capital +0.0111; max inventory 30 sh; first leg alone +219.5.

## Sensitivity


**Swing 1 size (swing 2 = 80, hedge 100 %)**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 8 | -0.306 | -0.410 | -0.699 | -0.382 | 1451 | 0.741 | 0.767 | 0.772 | -1310 | -1.758 |
| 10 | -0.314 | -0.406 | -0.721 | -0.389 | 1451 | 0.740 | 0.767 | 0.772 | -1325 | -1.760 |
| 12 | -0.322 | -0.404 | -0.694 | -0.390 | 1451 | 0.740 | 0.767 | 0.769 | -1322 | -1.767 |
| 15 | -0.332 | -0.402 | -0.734 | -0.401 | 1451 | 0.741 | 0.767 | 0.768 | -1345 | -1.787 |
| 18 | -0.341 | -0.402 | -0.786 | -0.413 | 1451 | 0.741 | 0.767 | 0.769 | -1374 | -1.810 |
| 20 | -0.349 | -0.399 | -0.815 | -0.421 | 1451 | 0.741 | 0.767 | 0.768 | -1395 | -1.817 |
| 24 | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 30 | -0.377 | -0.398 | -0.935 | -0.453 | 1451 | 0.741 | 0.767 | 0.761 | -1489 | -1.861 |

**Swing 2 size (swing 1 = 24)**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 20 | -0.158 | -0.094 | -0.586 | -0.197 | 1451 | 0.741 | 0.764 | 0.788 | -642 | -0.769 |
| 30 | -0.193 | -0.167 | -0.652 | -0.245 | 1451 | 0.741 | 0.765 | 0.788 | -797 | -0.948 |
| 40 | -0.227 | -0.241 | -0.709 | -0.292 | 1451 | 0.741 | 0.766 | 0.788 | -948 | -1.160 |
| 50 | -0.260 | -0.311 | -0.783 | -0.339 | 1451 | 0.741 | 0.767 | 0.789 | -1105 | -1.392 |
| 60 | -0.296 | -0.382 | -0.840 | -0.387 | 1451 | 0.741 | 0.767 | 0.788 | -1265 | -1.620 |
| 70 | -0.324 | -0.445 | -0.858 | -0.422 | 1451 | 0.741 | 0.767 | 0.779 | -1386 | -1.818 |
| 80 | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 100 | -0.363 | -0.229 | -1.016 | -0.414 | 1451 | 0.741 | 0.767 | 0.763 | -1386 | -1.734 |

**Swing 2 = multiple of swing 1 = 24**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 1.5x | -0.212 | -0.211 | -0.690 | -0.273 | 1451 | 0.741 | 0.766 | 0.788 | -886 | -1.069 |
| 2x | -0.253 | -0.293 | -0.766 | -0.328 | 1451 | 0.741 | 0.767 | 0.789 | -1067 | -1.341 |
| 2.5x | -0.296 | -0.382 | -0.840 | -0.387 | 1451 | 0.741 | 0.767 | 0.788 | -1265 | -1.620 |
| 3x | -0.332 | -0.440 | -0.867 | -0.427 | 1451 | 0.741 | 0.767 | 0.778 | -1403 | -1.829 |
| 3.5x | -0.344 | -0.364 | -0.834 | -0.411 | 1451 | 0.741 | 0.767 | 0.758 | -1364 | -1.748 |

**Swing trigger (hysteresis 0.04)**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0.60 | -0.662 | -1.317 | -0.957 | -0.859 | 1451 | 0.648 | 0.661 | 0.675 | -2910 | -4.173 |
| 0.62 | -0.577 | -0.977 | -0.625 | -0.681 | 1451 | 0.668 | 0.683 | 0.699 | -2278 | -3.313 |
| 0.64 | -0.452 | -0.946 | -0.313 | -0.555 | 1451 | 0.688 | 0.702 | 0.707 | -1901 | -3.020 |
| 0.66 | -0.420 | -0.548 | -0.495 | -0.461 | 1451 | 0.707 | 0.726 | 0.736 | -1545 | -2.211 |
| 0.68 | -0.343 | -0.430 | -0.707 | -0.411 | 1451 | 0.723 | 0.745 | 0.750 | -1376 | -1.857 |
| 0.70 | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 0.72 | -0.286 | -0.083 | -1.025 | -0.331 | 1451 | 0.758 | 0.782 | 0.796 | -1076 | -1.262 |
| 0.74 | -0.257 | -0.026 | -0.988 | -0.294 | 1451 | 0.774 | 0.794 | 0.793 | -956 | -1.169 |
| 0.76 | -0.279 | -0.004 | -0.801 | -0.279 | 1451 | 0.792 | 0.819 | 0.784 | -905 | -1.266 |
| 0.78 | -0.107 | -0.010 | -0.991 | -0.196 | 1451 | 0.810 | 0.838 | 0.794 | -638 | -0.747 |

**Hysteresis (trigger 0.70)**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0.02 | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 0.03 | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 0.04 | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 0.05 | -0.361 | -0.396 | -0.870 | -0.434 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 0.06 | -0.361 | -0.396 | -0.870 | -0.434 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 0.08 | -0.362 | -0.399 | -0.864 | -0.435 | 1448 | 0.740 | 0.767 | 0.767 | -1436 | -1.839 |
| 0.10 | -0.374 | -0.460 | -0.611 | -0.425 | 1339 | 0.740 | 0.769 | 0.766 | -1384 | -1.953 |

**Hedge ratio (no hedge price limit; 'once')**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0% | -0.264 | -0.016 | -0.983 | -0.296 | 1451 | 0.741 | 0.767 | — | -986 | -1.307 |
| 25% | -0.287 | -0.142 | -0.975 | -0.340 | 1451 | 0.741 | 0.767 | 0.789 | -1127 | -1.396 |
| 50% | -0.309 | -0.267 | -0.965 | -0.383 | 1451 | 0.741 | 0.767 | 0.788 | -1267 | -1.513 |
| 75% | -0.333 | -0.371 | -0.927 | -0.419 | 1451 | 0.741 | 0.767 | 0.779 | -1383 | -1.728 |
| 100% | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |

**Hedge price limit (ratio 100 %)**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| none | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 0.70 | -0.391 | -0.367 | -0.827 | -0.441 | 1451 | 0.741 | 0.767 | 0.591 | -1453 | -1.855 |
| 0.72 | -0.386 | -0.390 | -0.846 | -0.446 | 1451 | 0.741 | 0.767 | 0.607 | -1469 | -1.862 |
| 0.74 | -0.366 | -0.369 | -0.794 | -0.421 | 1451 | 0.741 | 0.767 | 0.626 | -1390 | -1.785 |
| 0.76 | -0.355 | -0.379 | -0.916 | -0.433 | 1451 | 0.741 | 0.767 | 0.662 | -1427 | -1.783 |
| 0.78 | -0.351 | -0.381 | -0.921 | -0.431 | 1451 | 0.741 | 0.767 | 0.671 | -1422 | -1.777 |
| 0.80 | -0.347 | -0.352 | -0.905 | -0.420 | 1451 | 0.741 | 0.767 | 0.682 | -1384 | -1.716 |

**Recovery price limit (VWAP, reduce)**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0.65 | -0.457 | -0.417 | -0.575 | -0.462 | 1451 | 0.620 | 0.605 | 0.763 | -1493 | -2.022 |
| 0.67 | -0.416 | -0.406 | -0.690 | -0.449 | 1451 | 0.646 | 0.628 | 0.770 | -1456 | -1.926 |
| 0.69 | -0.408 | -0.359 | -0.666 | -0.429 | 1451 | 0.669 | 0.655 | 0.768 | -1390 | -1.852 |
| 0.70 | -0.409 | -0.353 | -0.633 | -0.424 | 1451 | 0.688 | 0.679 | 0.768 | -1375 | -1.853 |
| 0.71 | -0.397 | -0.320 | -0.656 | -0.411 | 1451 | 0.699 | 0.692 | 0.769 | -1360 | -1.821 |
| 0.72 | -0.397 | -0.325 | -0.690 | -0.417 | 1451 | 0.706 | 0.699 | 0.769 | -1400 | -1.866 |
| 0.73 | -0.386 | -0.286 | -0.553 | -0.383 | 1451 | 0.710 | 0.705 | 0.768 | -1318 | -1.826 |
| 0.74 | -0.391 | -0.290 | -0.651 | -0.400 | 1451 | 0.713 | 0.710 | 0.767 | -1349 | -1.811 |
| 0.75 | -0.334 | -0.262 | -0.662 | -0.358 | 1451 | 0.718 | 0.714 | 0.768 | -1211 | -1.613 |
| 0.77 | -0.313 | -0.312 | -0.958 | -0.395 | 1451 | 0.724 | 0.720 | 0.765 | -1316 | -1.576 |
| break-even only | -0.322 | -0.377 | -0.876 | -0.406 | 1451 | 0.712 | 0.735 | 0.762 | -1358 | -1.733 |
| min(0.68, break-even) | -0.382 | -0.448 | -0.568 | -0.422 | 1451 | 0.658 | 0.641 | 0.769 | -1366 | -1.903 |
| min(0.70, break-even) | -0.409 | -0.353 | -0.633 | -0.424 | 1451 | 0.688 | 0.679 | 0.768 | -1375 | -1.853 |
| min(0.72, break-even) | -0.397 | -0.328 | -0.690 | -0.418 | 1451 | 0.706 | 0.699 | 0.769 | -1402 | -1.868 |

**Recovery price limit (VWAP, reject)**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0.65 | -0.450 | -0.410 | -0.561 | -0.454 | 1451 | 0.620 | 0.603 | 0.762 | -1467 | -1.995 |
| 0.67 | -0.424 | -0.390 | -0.688 | -0.449 | 1451 | 0.646 | 0.627 | 0.770 | -1458 | -1.937 |
| 0.69 | -0.410 | -0.393 | -0.666 | -0.438 | 1451 | 0.669 | 0.655 | 0.768 | -1420 | -1.884 |
| 0.70 | -0.408 | -0.390 | -0.721 | -0.444 | 1451 | 0.688 | 0.678 | 0.766 | -1438 | -1.878 |
| 0.71 | -0.388 | -0.305 | -0.652 | -0.402 | 1451 | 0.699 | 0.691 | 0.770 | -1329 | -1.787 |
| 0.72 | -0.401 | -0.328 | -0.635 | -0.413 | 1451 | 0.706 | 0.700 | 0.769 | -1387 | -1.882 |
| 0.73 | -0.385 | -0.302 | -0.559 | -0.387 | 1451 | 0.710 | 0.706 | 0.768 | -1329 | -1.832 |
| 0.74 | -0.384 | -0.296 | -0.651 | -0.397 | 1451 | 0.713 | 0.710 | 0.767 | -1356 | -1.810 |
| 0.75 | -0.332 | -0.272 | -0.729 | -0.368 | 1451 | 0.717 | 0.714 | 0.768 | -1244 | -1.602 |
| 0.77 | -0.312 | -0.299 | -0.995 | -0.396 | 1451 | 0.724 | 0.720 | 0.765 | -1321 | -1.565 |

**Capital cap**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 100 | -0.303 | -0.147 | -0.949 | -0.348 | 1451 | 0.741 | 0.767 | 0.754 | -1153 | -1.458 |
| 110 | -0.319 | -0.250 | -0.941 | -0.382 | 1451 | 0.741 | 0.767 | 0.759 | -1263 | -1.538 |
| 125 | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 140 | -0.353 | -0.501 | -0.909 | -0.460 | 1451 | 0.741 | 0.767 | 0.780 | -1517 | -2.002 |
| 150 | -0.354 | -0.517 | -0.905 | -0.464 | 1451 | 0.741 | 0.767 | 0.784 | -1531 | -2.036 |
| 175 | -0.354 | -0.517 | -0.905 | -0.464 | 1451 | 0.741 | 0.767 | 0.786 | -1531 | -2.035 |
| 200 | -0.354 | -0.517 | -0.905 | -0.464 | 1451 | 0.741 | 0.767 | 0.786 | -1531 | -2.035 |

**Entry start (window 10 s, band 0.55–0.60)**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 15s | -0.413 | -0.461 | -0.598 | -0.448 | 1545 | 0.740 | 0.772 | 0.765 | -1514 | -2.072 |
| 20s | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 25s | -0.474 | -0.568 | -0.643 | -0.519 | 1390 | 0.741 | 0.763 | 0.776 | -1700 | -2.384 |
| 30s | -0.493 | -0.497 | -0.607 | -0.509 | 1323 | 0.742 | 0.762 | 0.779 | -1664 | -2.281 |
| 35s | -0.493 | -0.615 | -0.465 | -0.519 | 1260 | 0.743 | 0.771 | 0.777 | -1740 | -2.532 |
| 40s | -0.471 | -0.568 | -0.120 | -0.450 | 1191 | 0.743 | 0.772 | 0.786 | -1504 | -2.374 |

**Entry band (20–30 s)**

| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0.50–0.58 | -0.492 | -0.462 | -0.764 | -0.520 | 1374 | 0.741 | 0.772 | 0.756 | -1690 | -2.233 |
| 0.50–0.60 | -0.491 | -0.392 | -0.766 | -0.502 | 1602 | 0.741 | 0.771 | 0.758 | -1641 | -2.181 |
| 0.50–0.62 | -0.498 | -0.414 | -0.667 | -0.499 | 1822 | 0.740 | 0.769 | 0.763 | -1627 | -2.214 |
| 0.50–0.65 | -0.601 | -0.461 | -0.744 | -0.585 | 2111 | 0.742 | 0.772 | 0.764 | -1924 | -2.579 |
| 0.52–0.58 | -0.492 | -0.462 | -0.764 | -0.520 | 1374 | 0.741 | 0.772 | 0.756 | -1690 | -2.233 |
| 0.52–0.60 | -0.491 | -0.392 | -0.766 | -0.502 | 1602 | 0.741 | 0.771 | 0.758 | -1641 | -2.181 |
| 0.52–0.62 | -0.498 | -0.414 | -0.667 | -0.499 | 1822 | 0.740 | 0.769 | 0.763 | -1627 | -2.214 |
| 0.52–0.65 | -0.601 | -0.461 | -0.744 | -0.585 | 2111 | 0.742 | 0.772 | 0.764 | -1924 | -2.579 |
| 0.55–0.58 | -0.362 | -0.421 | -0.774 | -0.429 | 1184 | 0.740 | 0.767 | 0.764 | -1390 | -1.868 |
| 0.55–0.60 | -0.361 | -0.396 | -0.870 | -0.435 | 1451 | 0.741 | 0.767 | 0.767 | -1434 | -1.831 |
| 0.55–0.62 | -0.377 | -0.416 | -0.866 | -0.449 | 1686 | 0.740 | 0.765 | 0.773 | -1468 | -1.857 |
| 0.55–0.65 | -0.473 | -0.441 | -0.902 | -0.520 | 1988 | 0.741 | 0.768 | 0.765 | -1707 | -2.155 |

## Stress (P&L per market, all periods)

| Rank | None | fill price +0.01 | fill price +0.02 | fill price +0.03 | fill rate -10% | fill rate -20% | fill rate -30% | slippage +0.005 | slippage +0.010 | slippage +0.020 | fees +10% | fees +25% | fees +50% |
|---:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 1 | +0.049 | -0.020 | -0.088 | -0.157 | +0.051 | +0.049 | +0.031 | +0.029 | -0.020 | -0.075 | +0.038 | +0.022 | -0.006 |
| 2 | +0.049 | -0.019 | -0.088 | -0.157 | +0.051 | +0.049 | +0.031 | +0.029 | -0.019 | -0.075 | +0.038 | +0.022 | -0.005 |
| 3 | -0.029 | -0.115 | -0.201 | -0.287 | -0.030 | -0.012 | -0.005 | -0.066 | -0.103 | -0.182 | -0.042 | -0.062 | -0.095 |
| 4 | -0.031 | -0.117 | -0.203 | -0.289 | -0.031 | -0.013 | -0.007 | -0.067 | -0.105 | -0.184 | -0.044 | -0.064 | -0.097 |
| 5 | +0.009 | -0.057 | -0.123 | -0.189 | +0.006 | +0.014 | +0.011 | -0.012 | -0.047 | -0.106 | -0.002 | -0.017 | -0.044 |
| 6 | +0.009 | -0.057 | -0.123 | -0.189 | +0.006 | +0.014 | +0.012 | -0.012 | -0.047 | -0.106 | -0.001 | -0.017 | -0.043 |
| 7 | -0.019 | -0.094 | -0.169 | -0.243 | -0.017 | -0.002 | +0.000 | -0.047 | -0.080 | -0.146 | -0.030 | -0.048 | -0.078 |
| 8 | -0.018 | -0.093 | -0.168 | -0.243 | -0.016 | -0.002 | +0.001 | -0.046 | -0.079 | -0.145 | -0.030 | -0.048 | -0.077 |
| 9 | -0.020 | -0.095 | -0.170 | -0.245 | -0.018 | -0.004 | -0.001 | -0.048 | -0.081 | -0.147 | -0.032 | -0.049 | -0.079 |
| 10 | -0.020 | -0.095 | -0.170 | -0.245 | -0.018 | -0.004 | -0.001 | -0.048 | -0.081 | -0.146 | -0.032 | -0.049 | -0.079 |
