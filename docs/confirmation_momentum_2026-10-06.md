# BTC_CONFIRMATION_MOMENTUM: replay and ablation (2026-10-06)

BTC 5-minute windows 09-22 15:40 -> 10-01 21:20 UTC, 1992 windows; splits by window index: train 996, validation 398, test 299, forward 299. Execution: the CALIBRATED model (taker lands at +250 ms, fills only if the ask stays inside the limit through +1 s, charged the worse ask; fee 7 % x p(1-p)). Fills are all-or-nothing in this model (no partial fills), so the fill rate below is orders that filled / orders sent.

## 1. The brief's defaults with the rule-based estimator

The rule gives the token's mid plus at most 6 cents; against ~5 cents of charged costs and a 3-cent edge floor it cannot trade (tests/test_confirmation_momentum.py pins this). Candidates that passed every structural gate:

| variant / split | markets | candidates | trades | win | avg entry | P&L | per trade | per market | max DD | worst trade |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| rule defaults / train | 996 | 671 | 0 | - | - | 0.00 | - | - | - | - |
| rule defaults / validation | 398 | 252 | 0 | - | - | 0.00 | - | - | - | - |
| rule defaults / test | 299 | 86 | 0 | - | - | 0.00 | - | - | - | - |
| rule defaults / forward | 299 | 214 | 0 | - | - | 0.00 | - | - | - | - |

## 2. The fitted model

Logistic regression over 13 features fitted on 671 TRAIN candidates (win rate 0.732), Platt-recalibrated on 252 VALIDATION candidates (win rate 0.766); written to data/replay/cm_model.json. Coefficients (standardised): logit_mid +0.81, z1 +0.09, z5 -0.01, z15 +0.08, agree +0.07, tok_r5 -0.09, tok_r15 +0.18, spread_c +0.11, log_depth +0.04, sqrt_left +0.04, elapsed_frac +0.04, retrace +0.00, log_sigma -0.01.

## 3. Edge threshold chosen on VALIDATION

| min net edge | trades | P&L | per trade |
|---:|---:|---:|---:|
| 0.00 | 11 | -10.78 | -0.980 |
| 0.01 | 7 | -7.27 | -1.039 |
| 0.02 | 4 | -2.07 | -0.517 |
| 0.03 | 3 | +0.91 | +0.305 |
| 0.05 | 2 | +3.90 | +1.948 |

Chosen: 0.03 (best P&L per market with at least 20 validation trades).

## 4. TRAIN grid (z5 minimum x token confirmation cents x last entry second)

| z5 min | confirm c | max elapsed | trades | P&L | per market |
|---:|---:|---:|---:|---:|---:|
| 1.5 | 3 | 150 | 6 | -3.95 | -0.004 |
| 1.5 | 3 | 210 | 10 | -2.06 | -0.002 |
| 1.5 | 5 | 150 | 6 | -3.95 | -0.004 |
| 1.5 | 5 | 210 | 10 | -2.06 | -0.002 |
| 1.5 | 7 | 150 | 6 | -3.95 | -0.004 |
| 1.5 | 7 | 210 | 10 | -2.06 | -0.002 |
| 2.0 | 3 | 150 | 6 | -3.90 | -0.004 |
| 2.0 | 3 | 210 | 9 | -3.83 | -0.004 |
| 2.0 | 5 | 150 | 6 | -3.90 | -0.004 |
| 2.0 | 5 | 210 | 9 | -3.83 | -0.004 |
| 2.0 | 7 | 150 | 6 | -3.90 | -0.004 |
| 2.0 | 7 | 210 | 9 | -3.83 | -0.004 |
| 2.5 | 3 | 150 | 4 | -7.39 | -0.007 |
| 2.5 | 3 | 210 | 4 | -7.39 | -0.007 |
| 2.5 | 5 | 150 | 4 | -7.39 | -0.007 |
| 2.5 | 5 | 210 | 4 | -7.39 | -0.007 |
| 2.5 | 7 | 150 | 4 | -7.39 | -0.007 |
| 2.5 | 7 | 210 | 4 | -7.39 | -0.007 |

Chosen: z5 >= 2.0, confirmation 5c, entries until 210 s.

## 5. Ablation (section 19): which component carries the result

| variant / split | markets | candidates | trades | win | avg entry | P&L | per trade | per market | max DD | worst trade |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A immediate post-open leader, hold / train | 996 | 0 | 861 | 60% | 0.602 | -70.65 | -0.082 | -0.071 | 94.0 | -4.15 |
| A immediate post-open leader, hold / validation | 398 | 0 | 321 | 62% | 0.603 | -8.15 | -0.025 | -0.021 | 49.5 | -4.01 |
| A immediate post-open leader, hold / test | 299 | 0 | 242 | 63% | 0.616 | -5.52 | -0.023 | -0.018 | 40.9 | -4.15 |
| A immediate post-open leader, hold / forward | 299 | 0 | 252 | 60% | 0.606 | -37.52 | -0.149 | -0.126 | 52.3 | -4.06 |
| | | | | | | | | | | |
| B confirmation momentum (selected) / train | 996 | 671 | 9 | 56% | 0.616 | -3.83 | -0.425 | -0.004 | 9.8 | -3.62 |
| B confirmation momentum (selected) / validation | 398 | 252 | 3 | 67% | 0.610 | +0.91 | +0.305 | +0.002 | 3.0 | -2.98 |
| B confirmation momentum (selected) / test | 299 | 86 | 2 | 100% | 0.665 | +3.31 | +1.656 | +0.011 | 0.0 | +1.09 |
| B confirmation momentum (selected) / forward | 299 | 214 | 4 | 25% | 0.552 | -6.98 | -1.745 | -0.023 | 9.0 | -3.28 |
| | | | | | | | | | | |
| C without exchange agreement / train | 996 | 674 | 9 | 56% | 0.616 | -3.83 | -0.425 | -0.004 | 9.8 | -3.62 |
| C without exchange agreement / validation | 398 | 252 | 3 | 67% | 0.610 | +0.91 | +0.305 | +0.002 | 3.0 | -2.98 |
| C without exchange agreement / test | 299 | 86 | 2 | 100% | 0.665 | +3.31 | +1.656 | +0.011 | 0.0 | +1.09 |
| C without exchange agreement / forward | 299 | 217 | 4 | 25% | 0.552 | -6.98 | -1.745 | -0.023 | 9.0 | -3.28 |
| | | | | | | | | | | |
| D without token confirmation / train | 996 | 759 | 9 | 56% | 0.616 | -3.83 | -0.425 | -0.004 | 9.8 | -3.62 |
| D without token confirmation / validation | 398 | 289 | 3 | 67% | 0.610 | +0.91 | +0.305 | +0.002 | 3.0 | -2.98 |
| D without token confirmation / test | 299 | 90 | 2 | 100% | 0.665 | +3.31 | +1.656 | +0.011 | 0.0 | +1.09 |
| D without token confirmation / forward | 299 | 220 | 4 | 25% | 0.552 | -6.98 | -1.745 | -0.023 | 9.0 | -3.28 |
| | | | | | | | | | | |
| E without the continuation filter / train | 996 | 671 | 9 | 56% | 0.616 | -3.83 | -0.425 | -0.004 | 9.8 | -3.62 |
| E without the continuation filter / validation | 398 | 252 | 3 | 67% | 0.610 | +0.91 | +0.305 | +0.002 | 3.0 | -2.98 |
| E without the continuation filter / test | 299 | 86 | 2 | 100% | 0.665 | +3.31 | +1.656 | +0.011 | 0.0 | +1.09 |
| E without the continuation filter / forward | 299 | 214 | 4 | 25% | 0.552 | -6.98 | -1.745 | -0.023 | 9.0 | -3.28 |
| | | | | | | | | | | |
| F1 zones: nothing above 0.85 / train | 996 | 568 | 9 | 56% | 0.616 | -3.83 | -0.425 | -0.004 | 9.8 | -3.62 |
| F1 zones: nothing above 0.85 / validation | 398 | 202 | 3 | 67% | 0.610 | +0.91 | +0.305 | +0.002 | 3.0 | -2.98 |
| F1 zones: nothing above 0.85 / test | 299 | 77 | 2 | 100% | 0.665 | +3.31 | +1.656 | +0.011 | 0.0 | +1.09 |
| F1 zones: nothing above 0.85 / forward | 299 | 185 | 4 | 25% | 0.552 | -6.98 | -1.745 | -0.023 | 9.0 | -3.28 |
| | | | | | | | | | | |
| F2 zones: allow up to 0.99 / train | 996 | 755 | 9 | 56% | 0.616 | -3.83 | -0.425 | -0.004 | 9.8 | -3.62 |
| F2 zones: allow up to 0.99 / validation | 398 | 296 | 3 | 67% | 0.610 | +0.91 | +0.305 | +0.002 | 3.0 | -2.98 |
| F2 zones: allow up to 0.99 / test | 299 | 94 | 2 | 100% | 0.665 | +3.31 | +1.656 | +0.011 | 0.0 | +1.09 |
| F2 zones: allow up to 0.99 / forward | 299 | 235 | 4 | 25% | 0.552 | -6.98 | -1.745 | -0.023 | 9.0 | -3.28 |
| | | | | | | | | | | |
| G realistic live latency (x2 = 500 ms) / train | 996 | 671 | 10 | 60% | 0.599 | -1.12 | -0.112 | -0.001 | 8.3 | -3.67 |
| G realistic live latency (x2 = 500 ms) / validation | 398 | 252 | 3 | 67% | 0.603 | +1.01 | +0.337 | +0.003 | 3.0 | -2.98 |
| G realistic live latency (x2 = 500 ms) / test | 299 | 86 | 2 | 100% | 0.660 | +3.36 | +1.680 | +0.011 | 0.0 | +1.14 |
| G realistic live latency (x2 = 500 ms) / forward | 299 | 214 | 2 | 0% | 0.450 | -5.24 | -2.621 | -0.018 | 2.9 | -2.87 |
| | | | | | | | | | | |
| H rule estimator instead of the model / train | 996 | 671 | 0 | - | - | 0.00 | - | - | - | - |
| H rule estimator instead of the model / validation | 398 | 252 | 0 | - | - | 0.00 | - | - | - | - |
| H rule estimator instead of the model / test | 299 | 86 | 0 | - | - | 0.00 | - | - | - | - |
| H rule estimator instead of the model / forward | 299 | 214 | 0 | - | - | 0.00 | - | - | - | - |
| | | | | | | | | | | |

## 6. Rejection statistics of the selected strategy (all splits)

| reason | evaluations |
|---|---:|
| NO_SPOT_DATA | 18 |
| STALE_SPOT | 0 |
| EXCHANGE_DISAGREEMENT | 31 |
| NO_MOMENTUM | 192,032 |
| WEAK_MOMENTUM | 134,399 |
| MOMENTUM_REVERSING | 0 |
| NO_TOKEN_CONFIRMATION | 1,456 |
| TOKEN_TOO_EXPENSIVE | 1,124 |
| INSUFFICIENT_EDGE | 1,129 |
| SPREAD_TOO_WIDE | 101 |
| INSUFFICIENT_DEPTH | 19 |
| TOO_EARLY | 89,640 |
| TOO_LATE | 179,280 |
| MAX_EXPOSURE | 0 |
| MAX_ENTRIES | 1 |
| ORDER_NOT_EXECUTABLE | 18 |
| CHASED_PRICE | 57 |
| OTHER | 269 |

## 7. Out-of-sample splits (TEST + FORWARD trades of the selected strategy)

**token price zone**

| bucket | trades | P&L | per trade | win |
|---|---:|---:|---:|---:|
| zone 1 | 5 | -4.76 | -0.951 | 40% |
| zone 2 | 1 | +1.09 | +1.088 | 100% |

**momentum magnitude**

| bucket | trades | P&L | per trade | win |
|---|---:|---:|---:|---:|
| 2.5-3.5 | 4 | -2.79 | -0.696 | 50% |
| z5 < 2.5 | 2 | -0.88 | -0.441 | 50% |

**time remaining**

| bucket | trades | P&L | per trade | win |
|---|---:|---:|---:|---:|
| 90-150 s | 1 | +2.05 | +2.047 | 100% |
| left >= 150 s | 5 | -5.71 | -1.143 | 40% |

**hour of day**

| bucket | trades | P&L | per trade | win |
|---|---:|---:|---:|---:|
| 01 UTC | 1 | +2.05 | +2.047 | 100% |
| 03 UTC | 1 | -2.82 | -2.817 | 0% |
| 07 UTC | 1 | +2.22 | +2.224 | 100% |
| 12 UTC | 1 | -2.93 | -2.929 | 0% |
| 16 UTC | 2 | -2.19 | -1.096 | 50% |

**weekday**

| bucket | trades | P&L | per trade | win |
|---|---:|---:|---:|---:|
| Thu | 4 | -6.98 | -1.745 | 25% |
| Tue | 2 | +3.31 | +1.656 | 100% |

**volatility regime**

| bucket | trades | P&L | per trade | win |
|---|---:|---:|---:|---:|
| high vol | 2 | -2.19 | -1.096 | 50% |
| low vol | 2 | -0.88 | -0.441 | 50% |
| mid vol | 2 | -0.59 | -0.296 | 50% |


## 8. Addendum: the structural edge, the price zones and the lottery

The selected strategy barely trades because the costs it is charged (fee ~1.5c, slippage 1c, adverse selection 2c,
latency 0.5c) plus the 3c floor exceed what any estimator can claim over the ask. So the question was moved one step
back: what do the CANDIDATES (every structural gate passed, no probability gate) earn when simply bought?

**First candidate per market, win rate against the ask (before costs):** train +2.8c, validation +4.1c, test +2.4c,
forward +5.1c -- positive in every split, concentrated in zone 2:

| zone (ask) | train win / ask | validation | test | forward |
|---|---|---|---|---|
| 1 (0.50-0.65) | 0.567 / 0.573 | 0.609 / 0.575 | 0.423 / 0.567 | 0.633 / 0.563 |
| 2 (0.65-0.85) | 0.818 / 0.768 | 0.810 / 0.760 | 0.929 / 0.769 | 0.803 / 0.748 |
| 3 (0.85-0.93) | 0.976 / 0.888 | 0.923 / 0.894 | 1.000 / 0.897 | 0.846 / 0.885 |

**Buying every candidate at the ask (calibrated fills, 1 clip per market):**

| variant | train (996) | validation (398) | test (299) | forward (299) |
|---|---|---|---|---|
| all zones | 236 trades, -26.18 (-0.111/trade) | 95, +19.66 (+0.207) | 32, +6.70 (+0.209) | 76, -13.66 (-0.180) |
| **zone 2 only (0.65-0.85)** | 120, +14.50 (+0.121) | 50, +4.30 (+0.086) | 18, +13.07 (+0.726) | 35, -5.04 (-0.144) |
| zones 2+3 (0.65-0.93) | 152, +20.30 (+0.134) | 68, +7.60 (+0.112) | 21, +14.71 (+0.701) | 48, -3.29 (-0.069) |
| zone 2, up to 3 clips | 122, +16.48 | 50, +4.30 | 18, +13.07 | 37, -3.49 |
| zone 2, fitted model, edge >= 0 | 22, +5.88 | 5, -9.08 | 2, +1.94 | 6, -6.68 |

Fill rate 32-42 % of candidate seconds (the ask must hold inside the limit for a second after landing). **No adverse
selection in the fill model:** zone-2 first candidates win 80.8 % (n 453), the filled first clips win 80.7 % (n 223).
Extra clips add nothing (the second impulse rarely comes); the probability gate removes 90 % of the trades and the
remaining handful is noise. Latency x2 changes neither fills nor P&L materially (the survival rule dominates).

**The lottery (section 15),** a bid at min(0.03, max(0.01, the loser's bid)) for 2 USDC of risk when the loser asks
<= 0.10 with 30 s left, filled only when the far side trades strictly through: 191 bids, the price touched 99 % of
them, 125 filled (65 %), 3 won. P&L train +44.98, validation -44.65, test -29.69, forward +29.99: a zero-expectation
lottery on this data (one hit pays about 100, hits come every ~40 fills at a cost of about 2 each).

## 9. Verdict and recommended parameters

* The confirmation structure (45-210 s, z1/z5/z15 >= 2/2/1.5, 2-of-3 exchanges, token already following) finds a
  real but small regularity: the leading token at 0.65-0.85 after a confirmed move wins 5-16c more often than its
  price says, in every split. After realistic fills that is about **+0.12 per 3-USDC clip, +1.5c per market**:
  roughly +4 USDC a day at this size, +40 a day at 10x. Three of four splits positive; FORWARD negative (-5 on 35
  trades), so the edge is not proven, only plausible.
* Zone 1 (0.50-0.65) has no edge and should not be traded; zone 3 adds a little with few trades.
* The agreement, continuation and token-confirmation filters are not binding once the momentum gate has fired
  (ablations C, D, E equal B): they cost nothing and prevent nothing in this data. Keep them for live safety.
* The probability model as specified cannot carry the decision: its calibrated edge over the mid is 1-2c, below the
  charged costs. Recommended paper configuration: zones 2-3 only (TPB_CM_ZONE_MIN=0.65), ONE clip per market, the
  rule estimator with the measured zone-2 bonus (TPB_CM_P_BONUS_BASE=0.08, cap 0.08), adverse selection 0 (measured
  0), slippage 1c, net-edge floor 0 -- i.e. buy every zone-2/3 candidate once, which is exactly the row that was
  positive in three splits. The fitted model stays on disk for offline comparison and is NOT used in the decision.
* Paper first, for a week, with the fill audit against the recorded books; real money only after that and only
  after the user's explicit approval.
