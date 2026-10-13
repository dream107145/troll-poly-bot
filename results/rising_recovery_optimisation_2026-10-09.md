# Rising-token recovery: replay optimisation (2026-10-09)

scripts/rising_recovery_replay.py drives the real engine (strategy/rising_recovery.py, momentum mode) pass by pass over 3,624 recorded BTC 5-minute markets (09-22 15:40 -> 10-09 13:40 UTC). Taker orders fill at the ask 250 ms later up to its depth (fee 0.07 p(1-p)); post-only bids fill only when the ask comes down to them; settlement on gamma outcomes. P&L per market (USDC, 3-USDC buys), chronological TRAIN 50 % / VALIDATION 25 % / TEST 25 %.

| round | config | train | val | test | all (t) | markets traded |
|---|---|---:|---:|---:|---:|---:|
| 1 | defaults | -0.451 | -0.384 | -0.557 | -0.461 (-8.8) | 3475 |
| 1 | momentum_only | -0.781 | -0.908 | -0.820 | -0.822 (-8.3) | 3528 |
| 1 | no_fix | -0.558 | -0.650 | -0.615 | -0.595 (-7.7) | 3528 |
| 1 | no_hedge | -0.098 | -0.225 | -0.329 | -0.188 (-2.7) | 3475 |
| 1 | no_positive_rule | -0.458 | -0.533 | -0.617 | -0.516 (-11.5) | 3475 |
| 1 | all_maker | -0.323 | -0.365 | -0.533 | -0.386 (-8.4) | 2947 |
| 1 | rise3c | -0.403 | -0.361 | -0.608 | -0.444 (-8.9) | 3416 |
| 1 | rise5c | -0.349 | -0.392 | -0.449 | -0.385 (-8.7) | 3182 |
| 1 | fav065 | -0.535 | -0.399 | -0.425 | -0.474 (-8.6) | 3481 |
| 1 | fav075 | -0.532 | -0.463 | -0.589 | -0.529 (-8.5) | 3492 |
| 1 | hedge010 | -0.268 | -0.363 | -0.418 | -0.329 (-5.4) | 3475 |
| 1 | hedge030 | -0.457 | -0.532 | -0.563 | -0.502 (-11.9) | 3475 |
| 1 | band30_70 | -0.448 | -0.364 | -0.439 | -0.425 (-8.2) | 3158 |
| 1 | late_entries | -0.197 | -0.186 | -0.145 | -0.181 (-5.2) | 1790 |
| 1 | early_entries | -0.450 | -0.407 | -0.547 | -0.464 (-8.9) | 3453 |
| 2 | defaults_attr | -0.451 | -0.384 | -0.557 | -0.461 (-8.8) | 3475 |
| 2 | nohedge_late150 | -0.028 | -0.088 | -0.051 | -0.049 (-1.0) | 1790 |
| 2 | nohedge_late100 | -0.029 | -0.070 | -0.118 | -0.062 (-1.8) | 1021 |
| 2 | nohedge_late150_rise5 | -0.006 | -0.030 | -0.101 | -0.036 (-0.9) | 1485 |
| 2 | nohedge_late150_maker | -0.076 | -0.188 | -0.161 | -0.125 (-3.7) | 1181 |
| 3 | A_late150_b55_80 | +0.033 | -0.155 | -0.026 | -0.029 (-0.6) | 1784 |
| 3 | B_late200_b55_80 | +0.023 | -0.120 | -0.260 | -0.084 (-1.4) | 2472 |
| 3 | C_all_b55_80 | +0.019 | -0.085 | -0.433 | -0.120 (-1.7) | 3473 |
| 3 | D_A_fav060 | -0.020 | -0.101 | -0.010 | -0.038 (-0.8) | 1784 |
| 3 | E_A_rise5 | +0.022 | -0.102 | -0.099 | -0.039 (-1.0) | 1470 |
| 3 | F_A_nofix | +0.033 | -0.020 | -0.113 | -0.017 (-0.3) | 1784 |
| 3 | G_late150_b60_85 | +0.076 | -0.064 | -0.040 | +0.012 (+0.2) | 2047 |
| 4 | FAV1 | +0.076 | -0.032 | -0.228 | -0.027 (-0.3) | 3079 |
| 4 | FAV2_left120 | +0.055 | -0.118 | -0.374 | -0.096 (-1.2) | 3175 |
| 4 | FAV3_to075 | +0.188 | -0.026 | -0.299 | +0.013 (+0.1) | 3487 |
| 4 | FAV4_fix | -0.060 | -0.116 | -0.343 | -0.145 (-2.1) | 3079 |
| 4 | FAV5_cool30 | +0.046 | +0.033 | -0.133 | -0.002 (-0.0) | 3029 |
| 4 | FAV6_3fills | +0.126 | +0.157 | -0.210 | +0.050 (+0.6) | 3079 |
| 5 | G | +0.076 | -0.064 | -0.040 | +0.012 (+0.2) | 2047 |
| 5 | G_maker | -0.029 | -0.082 | -0.118 | -0.064 (-1.9) | 1242 |
| 5 | G_nofix | +0.067 | +0.025 | -0.132 | +0.007 (+0.1) | 2047 |
| 5 | G_rise3 | +0.058 | -0.052 | -0.061 | +0.001 (+0.0) | 1942 |
| 5 | G_left120 | +0.044 | -0.063 | -0.023 | +0.000 (+0.0) | 1553 |
| 5 | G_b65_90 | +0.096 | -0.002 | -0.033 | +0.039 (+0.8) | 2356 |
| 5 | G_10fills | +0.076 | -0.064 | -0.040 | +0.012 (+0.2) | 2047 |
| 5 | G_maker_nofix | -0.005 | -0.018 | -0.183 | -0.053 (-1.6) | 1242 |
| 6 | F1_b55_65 | +0.012 | +0.085 | -0.082 | +0.007 (+0.2) | 2930 |
| 6 | F1_b55_75 | +0.060 | +0.094 | -0.148 | +0.016 (+0.4) | 3273 |
| 6 | G65_target05 | +0.093 | -0.004 | -0.041 | +0.035 (+0.8) | 2356 |
| 6 | G65_cost10 | +0.046 | +0.077 | -0.179 | -0.003 (-0.1) | 2356 |
| 6 | G70_95 | +0.126 | +0.001 | -0.100 | +0.038 (+0.8) | 2721 |
| 6 | G65_left180 | +0.088 | +0.020 | -0.216 | -0.005 (-0.1) | 2766 |

## Findings

- The 16:24-17:02 settings (momentum buys 30-285 s left at 0.20-0.80, favourite fix, positive-state hedge < 0.20) lose 0.46 a market, t -8.8, in every split.
- Attribution (defaults, c/share held): hedges -3.6 (bids) / -4.2 (taken) -- cheap tokens win less often than their price; favourite fixes -2.0 (-3.3 at 0.5, +2.8 at 0.7); momentum buys -0.6 (+1.1 / +0.6 at 0.6-0.8, -2.7 / -1.7 at 0.4-0.6); most buying happened with > 200 s left.
- Removing the hedge (-0.19), buying only in the last 150 s (-0.18) and only tokens at 0.60-0.90 bring it to ~0.
- A static check of the favourite at its ask: 0.55-0.65 with 200-290 s left was positive in every split (+6.1 / +3.3 / +2.3c a share, market-weighted), but through the engine (repeated or single buys, real timing) it is ~0 and negative on TEST.
- Every favourite-buying variant is positive on TRAIN (09-22 -> 10-01) and negative on TEST (10-04 -> 10-09): the periods differ more than the variants do. No configuration is positive in all three splits with t > 1.
- Deployed 18:11 UTC: G_b65_90 (momentum buys only at 0.65-0.90, last 150 s, hedge off, fix and positive rule on, taker): train +0.096, val -0.002, test -0.033, all +0.039 (t +0.8) -- about break-even, not a proven edge.

## Round 7 -- the fix when the limits cut it short (after the 18:13 paper market: -10.00 / -2.35)

| config | train | val | test | all (t) |
|---|---:|---:|---:|---:|
| G65_fixfull | +0.076 | -0.079 | -0.133 | -0.015 (-0.3) |
| G65_fixpartial | +0.096 | -0.002 | -0.033 | +0.039 (+0.8) |
| G65_nofix | +0.095 | -0.018 | -0.114 | +0.014 (+0.3) |
| G65_fixfull_fav060 | +0.043 | -0.035 | -0.119 | -0.017 (-0.3) |

All-or-nothing fixes lose more than partial ones in every split, so the partial fix stays (fix_partial = True).
