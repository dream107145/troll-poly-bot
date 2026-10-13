# Repriced-token momentum: implementation and research (2026-09-24)

> **Follow-up:** the idea was redesigned and re-tested from scratch in [repriced_momentum_research_2026-09-24.md](repriced_momentum_research_2026-09-24.md): new shock definitions, cross-exchange and order-book features, six entry styles, 22 exits and an expected-value model, over five chronological periods. Verdict **D: abandon**. No subset beats the costs out of sample.

**Verdict: the hypothesis is not supported.** On the archive we have, no configuration of repriced-token momentum made money after realistic execution costs on the held-out periods.

- The 50-90 % zone that the brief started from lost **3.8c per share** in every period (unseen t -18.7).
- With the training continuation table, its own edge rule never lets it trade.
- The best rule chosen on training lost **1.8c/share on the three held-out periods combined** (t -2.2). It was negative in each one.
- Of 3,928 configurations with enough fills, 5 (0.1 %) came out positive on unseen data, the best at +0.05c.

The strategy is implemented, tested and running in **paper only**. Real money for it stays **disabled**: it needs `TPB_REPRICED_MOM_ENABLED=1` *and* `TPB_REPRICED_MOM_ALLOW_REAL=1`, and nothing sets the second. The spot-lag strategy, its live bot and its statistics are unchanged.

Full generated report, with every table: `data/replay/repriced_momentum_research.md`. Regenerate it with `python scripts/replay_repriced_momentum.py`, which takes about 2 minutes and is deterministic.

---

## 1. Hypothesis

After a large spot shock, the token reprices toward where the market's own pricing curve says it should be. Suppose the token is already 50-90 % of the way there, the spot is **still** moving the same way, and the token is **still** following. The hypothesis is that some continuation is left, and that it is large enough to trade for a few seconds after costs.

This is a different idea from spot-lag. Spot-lag buys tokens that have *not yet* repriced. The earlier study found that the more a token has already moved, the worse spot-lag's trade gets (+5.9c when the token moved against us, -5.1c after more than 10c). This test asks whether strong continuing momentum picks out a profitable subset among the tokens that have already moved.

## 2. Methodology

`scripts/replay_repriced_momentum.py` works on the per-asset feature cache that `backtest/spot_shock_replay.py` builds from `data/charts`. It runs in five steps.

1. **Shocks.** The spot-lag rule, unchanged:
   - the 1 s spot log-return is at least 3 sigma;
   - the 3 s return agrees by at least 0.5 sigma;
   - the move is accelerating;
   - one shock per market and direction per 2 s;
   - 60-295 s left in the window.
2. **Candidates.** Every archive point in the 5 s after a shock where:
   - the book is valid;
   - the expected move is at least 1c;
   - the token has repriced 25-300 %;
   - the spread is at most 3c;
   - the ask is between 0.05 and 0.95;
   - there are at least 5 shares at the ask.

   That gives 167,457 candidate points over 10,304 shocks. A rule enters at the **first** candidate of a shock that qualifies, which is what the live engine sees in time order.
3. **Execution.** Every candidate is executed with the same model as spot-lag (section 4) and sold by each of 11 exits.
4. **Training only.** Four things are built on the training period and nothing else:
   - the repricing × momentum matrix;
   - the expected-continuation table (forward token moves);
   - a grid of 7,128 rules;
   - **one** rule, chosen by its training t-stat (with at least 30 training fills). It was **locked** and written to `data/replay/repriced_momentum_params.json` before any other period was scored.
5. **Scoring.** The locked rule, the brief's own first configuration and the baselines are scored once on validation and on two later unseen periods.

The t-statistics are clustered by 5-minute epoch, because trades in one window are not independent.

## 3. Data period

Chart archive: 2,903 five-minute windows over 7 assets (BNB, BTC, DOGE, ETH, HYPE, SOL, XRP). Points are about 220 ms apart. Split chronologically by epoch:

| period | epochs | windows opening (UTC) | spot shocks |
|---|---:|---|---:|
| TRAINING | 256 | 09-22 15:30 → 09-23 18:20 | 7,335 |
| VALIDATION | 103 | 09-23 18:25 → 09-24 02:55 | 3,183 |
| UNSEEN A | 77 | 09-24 03:00 → 09-24 09:20 | 2,769 |
| UNSEEN B | 77 | 09-24 09:25 → 09-24 16:25 | 2,572 |

The whole archive covers about 49 hours. Three held-out periods (about 24 h together) is a real test, but it is a single market regime; section 18 covers what this does and does not rule out.

## 4. Execution assumptions

This is the same model as spot-lag. It was validated against venue-timestamped books: on the 2026-09-23 tick recording it reproduced the venue-truth P&L of 694 spot-shock orders. It was not written again for this study.

- **Latency.**
  - Decision → POST is drawn from the measured pre-send prior (15-250 ms).
  - POST → venue is half of a measured round trip (218-661 ms).
  - The mean decision → venue time here was 227-234 ms.
- **Venue book.** The book at arrival is read as the archived local book 500 ms later. The feed lags the venue.
- **Entry.**
  - A FOK BUY with **limit = the decision ask**, which never crosses further.
  - It fills at the arrival ask if that is at or under the limit with at least 5 shares behind it.
  - Otherwise it is killed. The quote has disappeared or moved, and nothing is assumed filled.
  - **There are no partial fills:** FOK is all-or-nothing, so thin depth means no fill.
- **Exit.**
  - A FOK SELL at the bid minus 2c, or at the bid for a take-profit.
  - It fills at the arrival bid and is retried every 2 s.
  - Inside the last 10 s of a window, the position settles at 1 or 0 with no exit fee.
- **Fees.** The taker fee `0.07 · p · (1 - p)` is charged on both legs.
- **Diagnostics only**, never selectable:
  - a limit at ask + 1c;
  - a passive bid at the decision bid, resting 2 s. It is treated as filled if the ask comes down to it, with no entry fee. That is optimistic about queue position.
- **Size.** 5 shares, the venue minimum. Results are in c/share. "USDC" figures are at 5 shares per trade.

## 5. Feature definitions

All features are causal: nothing at a point uses data stamped after it. Directions are normalised so that **positive always means the shock's direction**; for a DOWN shock the token is `1 - Up`.

| feature | definition |
|---|---|
| fair value (MFV) | the market's own curve `logit(Up mid) = a + b·logit(p_model)` (spot_lag.MFV_COEF, frozen on an earlier training slice), fed the TWAP model's p |
| expected_repricing | side MFV now − side MFV 1 s before the shock |
| actual_repricing | side mid now − side mid 1 s before the shock |
| repricing_pct | actual / expected. Undefined if expected < 1c, or if a price is missing, non-finite or outside [0, 1] |
| fair | side mid before + expected |
| overshoot | ask − fair |
| spot_return_h | log return over h = 250 ms, 500 ms, 1 s, 2 s; in sigma units as `d · r / (σ·√h)`. 100 ms is logged live, but the archive cannot resolve it (points ~220 ms apart) |
| token_return_h | side mid change over 250 ms, 500 ms, 1 s (and 100 ms live) |
| spot / token acceleration | the last 500 ms move minus the 500 ms before it |
| time_since_shock | seconds |
| direction agreement | spot move over the momentum horizon > 0 **and** token 500 ms move > 0 |
| momentum strength | spot 500 ms move in sigma: reversing < 0, weak 0-0.5, medium 0.5-1, strong ≥ 1 |
| expected continuation | the TRAINING mean change of the side's mid h seconds later, per (repricing bucket × strength), token agreeing (`CONTINUATION_C`) |
| expected edge | continuation − spread (half in, half out) − taker fee(ask) − taker fee(expected exit) − 0.5c slippage |

## 6. Training results

| rule | orders | fills | fill rate | win rate | net c/share | t | net USDC | max DD | profit factor |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A. spot-lag (its own entry and exit) | 522 | 132 | 25 % | 78 % | **+10.81** | +4.7 | +71.32 | | |
| B. naive: every token 50-90 % repriced | 3,480 | 1,802 | 52 % | 6 % | −3.89 | −18.5 | −350.06 | 350.06 | 0.06 |
| C-init. brief's first rule (50-90 %, continuing, agreeing, ≤1c overshoot, edge ≥1c) | **0** | 0 | | | | | | | |
| C-init without the edge gate | 2,534 | 1,202 | 47 % | 7 % | −3.84 | −16.6 | −230.77 | 230.77 | 0.07 |
| C0. locked zone, no momentum filter | 1,449 | 715 | 49 % | 16 % | −2.84 | −9.8 | −101.54 | 101.54 | 0.22 |
| **C. locked rule** | 97 | 47 | 48 % | 19 % | **+0.34** | +0.2 | +0.81 | 3.35 | 1.20 |

The best of 3,928 eligible training rules made +0.34c/share with a t-stat of 0.2, which is noise. Only 36 of the 3,928 were positive even on the data they were chosen on.

**The locked rule:**
- 25-50 % repriced;
- spot at least 1 sigma over 250 ms, with the token following;
- ask not past fair (0c overshoot);
- expected continuation at least costs + 1c;
- sold 2 s after the fill.

## 7. Validation results

| rule | orders | fills | fill rate | win rate | net c/share | t | net USDC |
|---|---:|---:|---:|---:|---:|---:|---:|
| A. spot-lag | 258 | 65 | 25 % | 65 % | **+4.51** | +2.6 | +14.66 |
| B. naive 50-90 % | 1,523 | 810 | 53 % | 6 % | −3.95 | −14.5 | −160.17 |
| C-init (edge gate) | 0 | 0 | | | | | |
| C-init without the gate | 1,090 | 513 | 47 % | 7 % | −3.96 | −11.8 | −101.55 |
| C0. no momentum filter | 627 | 281 | 45 % | 17 % | −4.01 | −7.8 | −56.31 |
| **C. locked rule** | 35 | 14 | 40 % | 29 % | **−3.26** | −1.6 | −2.28 |

## 8. Unseen results

| rule | period | orders | fills | fill rate | net c/share | t | net USDC |
|---|---|---:|---:|---:|---:|---:|---:|
| A. spot-lag | unseen A | 105 | 34 | 32 % | +1.36 | +0.9 | +2.32 |
| | unseen B | 126 | 26 | 21 % | +4.01 | +0.8 | +5.21 |
| B. naive 50-90 % | unseen A | 1,208 | 699 | 58 % | −3.76 | −14.5 | −131.41 |
| | unseen B | 1,150 | 609 | 53 % | −3.96 | −12.8 | −120.70 |
| C-init without the gate | unseen A | 802 | 428 | 53 % | −3.77 | −12.1 | −80.68 |
| | unseen B | 816 | 383 | 47 % | −3.72 | −8.8 | −71.33 |
| **C. locked rule** | unseen A | 29 | 12 | 41 % | **−1.81** | −4.5 | −1.09 |
| | unseen B | 36 | 17 | 47 % | **−0.58** | −0.5 | −0.50 |

**The locked rule, all three held-out periods pooled:**
- 100 orders, 43 fills (43 %), 10 wins and 33 losses.
- Average entry 0.713, exit 0.707, hold 2.4 s.
- Gross −1.35 USDC, fees 2.51, spread cost 2.55, slippage −0.80.
- **Net −3.86 USDC = −1.80c/share (−9.0c per 5-share trade), t −2.2.**
- 95 % CI [−3.43, −0.17] c/share.
- Max drawdown 4.31 USDC; profit factor 0.30.
- Trades p25 / median / p75: −2.8 / −1.7 / −0.3 c/share.

**The brief's first rule without its edge gate, pooled unseen:**
- 2,708 orders, 1,324 fills (49 %), 7 % winners.
- Net **−253.56 USDC = −3.83c/share**, t −18.7, 95 % CI [−4.23, −3.43].
- Profit factor 0.06.

## 9. Repricing × momentum matrix

Each cell: shocks reaching the cell / fills / **net c per share** / net USDC. The entry is the first moment the token is in the cell with the token following. It is a FOK at the ask, and the exit is momentum reversal (spot or token) or 8 s, the brief's suggested exit.

**TRAINING**

| repriced | reversing (< 0) | weak (0-0.5σ) | medium (0.5-1σ) | strong (≥ 1σ) |
|---|---|---|---|---|
| 25-50 % | 184 / 110 / **−3.11c** / −17.1 | 238 / 146 / **−3.98c** / −29.1 | 80 / 49 / **−4.85c** / −11.9 | 638 / 225 / **−2.74c** / −30.8 |
| 50-60 % | 162 / 94 / **−4.08c** / −19.2 | 209 / 135 / **−3.97c** / −26.8 | 81 / 40 / **−3.42c** / −6.8 | 490 / 189 / **−3.47c** / −32.8 |
| 60-70 % | 289 / 188 / **−4.31c** / −40.5 | 352 / 223 / **−4.10c** / −45.7 | 130 / 85 / **−3.89c** / −16.6 | 679 / 300 / **−3.64c** / −54.6 |
| 70-80 % | 343 / 240 / **−3.89c** / −46.7 | 467 / 307 / **−4.18c** / −64.2 | 192 / 119 / **−3.84c** / −22.8 | 863 / 422 / **−4.19c** / −88.4 |
| 80-90 % | 451 / 320 / **−4.42c** / −70.7 | 655 / 400 / **−4.20c** / −83.9 | 272 / 163 / **−3.98c** / −32.4 | 1005 / 540 / **−3.72c** / −100.6 |
| 90-100 % | 543 / 362 / **−4.13c** / −74.8 | 779 / 480 / **−4.02c** / −96.6 | 347 / 212 / **−4.16c** / −44.1 | 1076 / 626 / **−3.72c** / −116.3 |
| 100-110 % | 495 / 341 / **−3.94c** / −67.2 | 723 / 489 / **−4.24c** / −103.7 | 341 / 212 / **−4.05c** / −43.0 | 990 / 616 / **−3.84c** / −118.3 |
| 110-125 % | 564 / 417 / **−3.97c** / −82.7 | 840 / 570 / **−4.16c** / −118.5 | 393 / 250 / **−4.23c** / −52.9 | 1107 / 729 / **−3.84c** / −140.1 |
| 125+ % | 822 / 629 / **−4.25c** / −133.8 | 1021 / 711 / **−3.96c** / −140.8 | 494 / 352 / **−3.90c** / −68.7 | 1277 / 908 / **−3.88c** / −176.0 |

**UNSEEN (validation + A + B)**

| repriced | reversing (< 0) | weak (0-0.5σ) | medium (0.5-1σ) | strong (≥ 1σ) |
|---|---|---|---|---|
| 25-50 % | 186 / 111 / **−4.41c** / −24.5 | 245 / 156 / **−4.26c** / −33.3 | 76 / 46 / **−4.46c** / −10.3 | 642 / 214 / **−2.75c** / −29.4 |
| 50-60 % | 166 / 97 / **−3.90c** / −18.9 | 210 / 131 / **−4.34c** / −28.4 | 56 / 37 / **−3.88c** / −7.2 | 500 / 198 / **−4.13c** / −40.8 |
| 60-70 % | 238 / 152 / **−4.13c** / −31.4 | 362 / 230 / **−3.71c** / −42.6 | 117 / 75 / **−4.09c** / −15.3 | 668 / 278 / **−3.70c** / −51.4 |
| 70-80 % | 353 / 237 / **−3.86c** / −45.8 | 521 / 339 / **−4.14c** / −70.2 | 201 / 117 / **−4.17c** / −24.4 | 982 / 438 / **−3.76c** / −82.2 |
| 80-90 % | 445 / 304 / **−3.79c** / −57.6 | 718 / 447 / **−3.93c** / −87.8 | 321 / 208 / **−4.43c** / −46.1 | 1178 / 674 / **−3.83c** / −129.0 |
| 90-100 % | 559 / 383 / **−3.99c** / −76.4 | 813 / 519 / **−3.79c** / −98.3 | 386 / 232 / **−4.17c** / −48.4 | 1366 / 816 / **−3.81c** / −155.6 |
| 100-110 % | 598 / 406 / **−4.45c** / −90.4 | 883 / 603 / **−4.34c** / −130.7 | 392 / 254 / **−4.42c** / −56.2 | 1299 / 815 / **−3.99c** / −162.5 |
| 110-125 % | 648 / 450 / **−4.18c** / −94.0 | 1031 / 706 / **−4.45c** / −157.2 | 460 / 317 / **−4.39c** / −69.6 | 1402 / 905 / **−4.18c** / −188.9 |
| 125+ % | 874 / 652 / **−4.83c** / −157.5 | 1225 / 864 / **−4.36c** / −188.4 | 522 / 367 / **−4.39c** / −80.6 | 1457 / 1017 / **−4.30c** / −218.8 |

**Every cell loses, in both periods.** Strong momentum is the least-bad column in only one row (25-50 %, −2.7c). Among highly repriced tokens, strong momentum is no better than weak momentum, so the profitable subset the brief hoped for does not exist in this data. No exit rescues any bucket either. The generated report tabulates each bucket against 11 exits. On training, the best exit for any bucket is −1.9c (25-50 %, hold 3 s), and every other bucket loses at least 3.2c under every exit.

**The continuation itself, with no execution** (mid move h seconds after the decision, spot continuing and token agreeing, first moment per shock):

| repriced | period | +1 s | +3 s | +8 s | zero-latency round trip after fees, +3 s |
|---|---|---:|---:|---:|---:|
| 25-50 % | train / unseen | +3.41 / +3.37 | +4.07 / +3.93 | +4.27 / +4.26 | −0.03 / −0.18 |
| 50-60 % | train / unseen | +1.81 / +1.95 | +2.10 / +2.22 | +2.19 / +2.61 | −2.04 / −1.87 |
| 70-80 % | train / unseen | +0.81 / +1.29 | +0.83 / +1.53 | +0.95 / +1.43 | −3.30 / −2.58 |
| 80-90 % | train / unseen | +0.67 / +0.59 | +0.71 / +0.81 | +0.79 / +0.55 | −3.38 / −3.27 |
| 90-100 % | train / unseen | +0.36 / +0.32 | +0.39 / +0.18 | +0.51 / +0.10 | −3.64 / −3.89 |

The momentum **is real and stable** between training and unseen data. It is also **small**. It shrinks as repricing rises, from about 4c at 25-50 % to 0.5-1c at 80-90 %. A round trip costs about 4-5c: the spread (1-2c) plus two taker fees (about 1.7c each near 0.5) plus slippage. Even a trader with **zero latency**, buying the ask and selling the bid, only breaks even at 25-50 % and loses 2-4c everywhere above that.

**Continuation past fair (section 18 of the brief):** there is none.
- 100-110 %: mid +3 s −0.07c (train) / −0.21c (unseen).
- 110-125 %: −0.44 / −0.53c.
- 125 %+: −0.80 / −0.90c.

Tokens past fair **mean-revert**, and the zero-latency round trip loses 4-5c. Overshoot is not useful; it is a warning sign.

## 10. Asset breakdown (unseen, pooled)

| asset | brief's first rule, no gate: fills | net c/share | locked rule: fills | net c/share |
|---|---:|---:|---:|---:|
| BNB | 205 | −4.55 | 8 | −4.47 |
| BTC | 162 | −3.22 | 2 | +4.63 |
| DOGE | 184 | −4.29 | 9 | −1.73 |
| ETH | 222 | −3.59 | 1 | −4.09 |
| HYPE | 101 | −4.17 | 4 | +0.02 |
| SOL | 207 | −3.87 | 9 | −1.58 |
| XRP | 243 | −3.33 | 10 | −1.70 |

No asset works. BTC's +4.6c comes from 2 trades.

## 11. Shock-size breakdown (unseen, brief's first rule without the gate)

| shock | fills | net c/share |
|---|---:|---:|
| 3-4 σ | 1,014 | −3.82 |
| 4-6 σ | 269 | −3.96 |
| ≥ 6 σ | 41 | −3.19 |

The locked rule's split is 3-4 σ −1.68c (28 fills), 4-6 σ −3.62c (12) and ≥ 6 σ +4.40c (3); the last is 3 trades.

## 12. Hold-time breakdown (unseen, brief's first rule without the gate; exit: reversal or 8 s)

| hold | fills | net c/share |
|---|---:|---:|
| < 2 s | 1,075 | −3.42 |
| 2-5 s | 218 | −4.76 |
| 5-10 s | 29 | −9.66 |
| ≥ 10 s (sell retries) | 2 | −38.69 |

The reversal exit fires quickly (1.2 s on average), because the token's own 500 ms move flickers negative. Every fixed hold from 1 s to 10 s was tried as well (report: "Does any exit rescue a repricing bucket?"), and none rescues anything.

## 13. Baseline comparison

| | training | validation | unseen A | unseen B |
|---|---:|---:|---:|---:|
| A. spot-lag | +10.81 | +4.51 | +1.36 | +4.01 |
| B. naive 50-90 % | −3.89 | −3.95 | −3.76 | −3.96 |
| C0. locked zone, no momentum filter | −2.84 | −4.01 | −3.17 | −2.68 |
| C. repriced + momentum (locked) | +0.34 | −3.26 | −1.81 | −0.58 |
| D. no trade | 0 | 0 | 0 | 0 |

(c/share.) The momentum filter does add something: C loses less than C0 and B. But it loses, and **D (no trade) beats every repriced-momentum rule in every held-out period.** Spot-lag stays positive in all four periods on this same data and execution model (its entry here is the at-shock replay approximation).

## 14. Drawdown and risk

Under the strategy's own budget (1 position, $3 per order, $5 exposure, $5 daily loss), replayed in time order across all assets on the unseen periods:

| rule | orders | fills | net USDC | max drawdown | skipped | by UTC day |
|---|---:|---:|---:|---:|---|---|
| C (locked) | 27 | 10 | +0.12 | 0.71 | 72 by the $3 order cap (asks > 0.60) | −0.24, +0.36 |
| brief's first rule, no gate | 110 | 52 | −10.44 | 10.44 | daily loss stop 1,195 | −5.22, −5.21 |
| B naive | 103 | 51 | −10.19 | 10.19 | daily loss stop 1,857 | −5.05, −5.14 |

The $3 cap excludes most of the locked rule's trades: its entries average 0.71. The 10 that remain came to +0.12 USDC, which is noise. The broader rules hit the $5 daily stop on both days. The budget limits the damage; it does not create an edge.

## 15. Fill-rate analysis: does the momentum survive the latency?

| rule (all periods) | orders | filled | killed (ask moved above the limit) | ask move decision → arrival | latency | mid +1 s: filled / killed | mid +3 s: filled / killed |
|---|---:|---:|---:|---:|---:|---:|---:|
| C (locked) | 197 | 46 % | 54 % | +1.46c | 234 ms | −0.36c / **+2.74c** | +1.14c / +2.50c |
| brief's first rule, no gate | 5,242 | 48 % | 52 % | +1.43c | 227 ms | −1.18c / **+3.69c** | −1.04c / **+3.97c** |
| B naive | 7,361 | 53 % | 47 % | +1.09c | 227 ms | −1.29c / +3.53c | −1.14c / +3.84c |

**This is the mechanism.** On average the ask moves up 1.1-1.5c in the ~230 ms before the order arrives.
- The orders where the momentum **continued** are exactly the ones that were killed: the ask ran past the limit. Their token then rose another 3.7-4.0c.
- The orders that **filled** are the ones where the move stalled or turned. Their token *fell* by about 1c.

This is adverse selection by construction, and a better limit does not fix it:
- A limit 1c higher fills more (59-64 %) and loses about the same (−2.1 to −2.7c unseen).
- A passive bid at the bid fills only 10-19 %, and those fills are worse still (−2.2 to −8.0c).

## 16. Failure cases

Where the money goes (brief's first rule without the gate, unseen, per share, filled orders):
- The token's mid 1 s / 3 s after the decision: **−1.10c / −0.94c**. The fills are the stalls.
- Entry versus the decision mid: −0.37c. Fills come at or under the ask, often after it dipped.
- Price change from entry to exit fill: **−1.45c**.
- Fees, both legs: **2.38c**.
- **Net: −3.83c.**

Two things lose the money: the fees (a structural 2.4c) and the adverse selection (1.5c). The spread is mostly paid inside the price change.

The locked rule's worst unseen trades:
- A BNB token bought at 0.78 and sold at 0.52 (−28.9c). The move reversed hard, the first 2 s sell attempt did not fill, and the retry went out at 4.6 s.
- An XRP token bought at 0.88 and sold at 0.79 (−10.9c).
- The rest are 2-7c losses at extreme prices (0.08-0.11 or 0.85-0.93), where the fee is small but a 2-3c tick against us is the whole margin.

## 17. Overfitting checks

- **Grid:** 7,128 configurations:
  - 6 repricing zones (25-50, 40-70, 50-80, 50-90, 60-90, 70-100 %);
  - spot-momentum horizon 250 / 500 / 1000 ms;
  - threshold 0 / 0.5 / 1 σ;
  - overshoot 0 / 0.5 / 1 / 2c;
  - 11 exits (hold 1, 2, 3, 5, 8, 10 s; spot reversal, token reversal, agreement lost, fair reached, any event, the event exits capped at 8 s);
  - edge gate off / ≥ 0 / ≥ 1c.

  3,928 had at least 30 training fills. **36 (0.9 %) were positive on training** (the rest lost even on the data they were chosen on). That is the first sign there was nothing to find.
- **Selection:** by training t-stat only, locked before any held-out period was scored. The locked rule's training t was +0.2, so it was chosen from noise, and it went on to lose in all three held-out periods.
- **Out of sample, every configuration:** 5 of 3,928 (0.1 %) were positive on the pooled unseen data. The median was −3.25c, the best +0.05c, the worst −4.11c. Even choosing the best configuration **on the unseen data itself** (hindsight, not allowed) gives +0.05c/share, which is zero.
- **Stability:** the rank correlation of training vs unseen net per share across configurations is +0.83. The losses are structural and repeat period after period; they are not noise that a better selection could avoid.
- **Thresholds were not tuned toward profit.** The brief's initial values were tested as given, the grid is the brief's list, and nothing was widened after seeing results.

## 18. Final conclusion

**Does repriced-token momentum produce positive net P&L after realistic execution costs on multiple unseen chronological periods? No.**

- The continuation exists. It is about 1-4c over 1-8 s, and it is stable across periods.
- It is smaller than a round trip's spread plus two taker fees, and it shrinks as the token reprices.
- Latency then removes what is left. About 230 ms after the decision, the orders that can still fill at the decision ask are mostly the ones where the move has stalled.
- Strong momentum does not pick out a profitable subset among highly repriced tokens.
- Tokens past fair mean-revert.

Limits of this conclusion:
- The data is about 49 hours of one market regime.
- The fill model is a validated proxy, not real fills.
- The fee is the 7 % crypto schedule. If a fee cut halved the taker fee, the zero-latency round trip at 25-50 % would turn slightly positive; the 50-90 % zone would still lose.
- None of this is a reason to trade it now. It would be a reason to rerun this replay if fees or latency change materially.

**Recommendation:**
- Do not trade it with real money.
- Leave the paper bot running for a few days only to confirm the fill-rate and adverse-selection picture on live books, then stop it.
- Keep spot-lag as it is.

---

## Implementation

| file | what |
|---|---|
| `src/troll_poly_bot/strategy/repriced_momentum.py` | NEW. `RepricedMomentumConfig` / `RepricedMomentumEngine` (subclass of spot-lag's engine for its plumbing), the frozen `CONTINUATION_C` training table, pure functions `repricing`, `expected_continuation`, `round_trip_cost`, `exit_reason`, `reject_code` |
| `src/troll_poly_bot/backtest/spot_shock_replay.py` | NEW. The shared feature cache, chronological splits, shock rule and the venue_lag execution model (one simulator for both strategies) |
| `scripts/replay_repriced_momentum.py` | NEW. This study; writes the report and the locked parameters |
| `src/troll_poly_bot/strategy/engine.py` | 6 new `Reason` members (`REPRICING_INVALID`, `REPRICING_TOO_LOW`, `REPRICING_TOO_HIGH`, `SPOT_MOMENTUM_WEAK`, `DIRECTION_MISMATCH`, `OVERSHOOT`) |
| `src/troll_poly_bot/strategy/spot_lag.py` | `SpotLagConfig` reads its env prefix from a class attribute, so the subclass reads `TPB_REPRICED_MOM_*`. Spot-lag's keys, defaults and behaviour are unchanged |
| `src/troll_poly_bot/live.py` | selects the engine; applies the strategy's own budget (as a ceiling the dashboard cannot lift); writes the candidate log; strategy-aware `[SIGNAL]` / `[SKIP]`; **every ledger row and forensics trade now carries `strategy`** |
| `src/troll_poly_bot/execution/forensics.py` | `strategy` added to the trade record |
| `src/troll_poly_bot/__main__.py`, `config.py` | `--strategy repriced_momentum`; `check_repriced_momentum` (ENABLED for any mode, ALLOW_REAL for micro-live / live); `BotConfig.repriced_momentum` |
| `ecosystem.repricedmom.config.js` | NEW. `tpb-repricedmom-paper` only |
| `tests/test_repriced_momentum.py` | NEW. 39 tests |

**Entry, per market, every 200 ms.** A spot-lag-rule shock opens a 5 s window for that market; each market has its own shock, entry, position and exit state. The gates run in order, each rejection logged with its `REJECT_*` code:
1. stale book (`REJECT_STALE_BOOK`);
2. repricing undefined (`REJECT_REPRICING_INVALID`), below the zone (`…_TOO_LOW`) or above it (`…_TOO_HIGH`);
3. spot or token not moving our way (`REJECT_DIRECTION_MISMATCH`);
4. spot too slow (`REJECT_SPOT_MOMENTUM_WEAK`);
5. book, price band and spread;
6. overshoot (`REJECT_OVERSHOOT`);
7. expected edge (`REJECT_EDGE_TOO_LOW`);
8. the per-market, hourly, exposure and global risk limits (`REJECT_RISK_LIMIT`).

A candidate that passes is sent as a FOK BUY at the ask; `LIMIT_OFFSET_CENTS` defaults to 0, so it never chases. Just before sending, the order is revalidated against the freshest book: if the ask moved above the limit, the quote is gone or the depth is short, it is cancelled rather than sent.

**Exit.** `EXIT_MODE` sets how a position is closed:
- `timer`: after `HOLD_S`;
- `reversal`: when the spot's 500 ms move or the token's own 500 ms move turns against us;
- `fair`: when the bid reaches fair;
- `any`: the first of `reversal` and `fair`.

The event modes are capped at `MAX_HOLD_S` (8 s). A position of unknown age (restored after a restart) is closed at once.

**Logging.**
- Every candidate goes to `data/repriced_momentum_candidates.<tag>.jsonl` with every section-21 field, the decision and the reason.
- `[MOM]` log lines appear once per shock per change of reason, plus one on fill and one on exit.
- Trades appear in the tagged ledger (`live_trades.<tag>.jsonl`) and forensics (entry, exit, hold, fill latency, P&L after fees), all stamped `strategy: repriced_momentum`.

**Configuration** (see `.env.example`). The code defaults are the brief's first configuration: 50-90 %, 500 ms spot > 0, token agreement, 1c overshoot, 1c edge, reversal exit, 8 s maximum. The budget defaults are 1 position, $5 exposure, $3 per order and $5 daily loss. The paper app runs the **locked** research rule instead (section 6).

**Tests.** `tests/test_repriced_momentum.py` covers:
- the repricing %, for UP and DOWN tokens;
- momentum and direction normalisation;
- fair value and overshoot;
- the continuation table and the edge gate;
- stale books;
- quote disappearance and cancellation before sending;
- the FOK limit, and why no partial fill is possible;
- reversal and maximum-hold exits;
- multi-market isolation;
- the budget and global limits (order cap, one position, daily loss);
- the dashboard not being able to lift the budget;
- the kill switch;
- strategy identification in the ledger and forensics;
- P&L accounting with both fees;
- the feature flags;
- spot-lag's config and defaults being unchanged.

Full suite: 810 passed. One existing ledger test was updated to expect the new `strategy` field; its exact-equality check is kept.

**Running.** `pm2 start ecosystem.repricedmom.config.js --only tpb-repricedmom-paper`, which uses paper money only and tagged files. Stop it with `pm2 stop tpb-repricedmom-paper`. `touch data/KILL` stops its entries, as it does for every bot.

**Real money: DISABLED.** No real-money app exists for this strategy, and `TPB_REPRICED_MOM_ALLOW_REAL` is set nowhere. `tpb-spotlag-live` was not changed or restarted.
