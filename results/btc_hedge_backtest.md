# BTC trend-reversal-hedge backtest

BTC 5-minute windows with a venue outcome: train 193, validation 63, test 128, forward 1,081. Observation 10 s, trend 1 σ over 5 s, reversal confirmed over 3 s, 5-share FOK at ask + 1c, initial ask ≤ 0.65, 8 attempts; 3 latency draws.

## 1. max_pair_cost (variant C), chosen on train + validation

| max_pair_cost | train | validation | test | forward |
|---|---:|---:|---:|---:|
| 0.94 | -48 (hedged 65% of reversals) | -10 (hedged 64% of reversals) | -37 (hedged 63% of reversals) | -343 (hedged 53% of reversals) |
| 0.95 | -51 (hedged 65% of reversals) | -8 (hedged 66% of reversals) | -37 (hedged 64% of reversals) | -325 (hedged 55% of reversals) |
| 0.96 | -51 (hedged 66% of reversals) | -9 (hedged 66% of reversals) | -36 (hedged 65% of reversals) | -324 (hedged 56% of reversals) |
| 0.97 | -46 (hedged 68% of reversals) | -11 (hedged 66% of reversals) | -33 (hedged 67% of reversals) | -321 (hedged 57% of reversals) |
| 0.98 | -46 (hedged 68% of reversals) | -9 (hedged 69% of reversals) | -35 (hedged 67% of reversals) | -318 (hedged 57% of reversals) |

Chosen: **0.98** (`data/replay/btc_hedge_locked.json`). Test and forward were not read for this choice.

## 2. The three variants

### A) trend-only

| metric | train | validation | test | forward |
|---|---:|---:|---:|---:|
| markets | 193 | 63 | 128 | 1081 |
| with initial trade | 189 | 61 | 125 | 1049 |
| initial trade rate | 97.9% | 96.8% | 97.7% | 97.0% |
| fills | 189 | 61 | 125 | 1049 |
| reversal rate | 96.8% | 96.7% | 96.8% | 91.0% |
| hedge rate (of reversals) | 0.0% | 0.0% | 0.0% | 0.0% |
| hedge completion | — | — | — | — |
| avg initial price | 0.434 | 0.436 | 0.431 | 0.410 |
| avg hedge price | — | — | — | — |
| avg pair cost (prices) | — | — | — | — |
| fees $ | 15.57 | 5.05 | 10.18 | 85.12 |
| slippage allowance $ | 4.73 | 1.53 | 3.12 | 26.23 |
| **net P&L $** | **-16.05** | **-18.02** | **-34.25** | **-278.10** |
| expectancy / market $ | -0.085 | -0.295 | -0.274 | -0.265 |
| win rate (markets) | 43.4% | 39.3% | 39.2% | 37.4% |
| max drawdown $ | 28.10 | 23.18 | 65.59 | 288.69 |
| avg unmatched exposure $ | 2.17 | 2.18 | 2.15 | 2.05 |
| longest losing streak | 7.0 | 6.0 | 10.0 | 18.0 |

| period | net: initial UP | net: initial DOWN | net: TREND_UP (n) | net: TREND_DOWN (n) | net: CHEAPEST fallback (n) | net: reversal markets (n) | net: no-reversal markets (n) |
|---|---:|---:|---:|---:|---:|---:|---:|
| train | -32.94 | +16.89 | +9.42 (17) | +6.17 (15) | -31.65 (157) | -22.95 (183) | +6.90 (6) |
| validation | -7.86 | -10.16 | +5.26 (3) | -3.45 (6) | -19.82 (52) | -23.75 (59) | +5.73 (2) |
| test | -42.38 | +8.13 | -8.11 (9) | -2.20 (14) | -23.94 (102) | -36.12 (121) | +1.87 (4) |
| forward | -133.20 | -144.90 | -0.53 (65) | +1.63 (53) | -279.21 (931) | -401.95 (954) | +123.85 (95) |

### B) trend + unconditional hedge

| metric | train | validation | test | forward |
|---|---:|---:|---:|---:|
| markets | 193 | 63 | 128 | 1081 |
| with initial trade | 189 | 61 | 125 | 1048 |
| initial trade rate | 97.9% | 96.8% | 97.7% | 96.9% |
| fills | 372 | 120 | 245 | 1991 |
| reversal rate | 96.8% | 96.7% | 96.8% | 91.0% |
| hedge rate (of reversals) | 100.0% | 100.0% | 99.2% | 99.0% |
| hedge completion | 100.0% | 100.0% | 100.0% | 100.0% |
| avg initial price | 0.435 | 0.436 | 0.430 | 0.410 |
| avg hedge price | 0.596 | 0.590 | 0.559 | 0.634 |
| avg pair cost (prices) | 1.031 | 1.027 | 0.991 | 1.045 |
| fees $ | 28.31 | 8.95 | 18.66 | 143.66 |
| slippage allowance $ | 9.30 | 3.00 | 6.12 | 49.78 |
| **net P&L $** | **-49.06** | **-11.05** | **-13.13** | **-245.18** |
| expectancy / market $ | -0.260 | -0.181 | -0.105 | -0.234 |
| win rate (markets) | 25.6% | 24.6% | 27.2% | 25.7% |
| max drawdown $ | 49.06 | 11.29 | 20.56 | 247.28 |
| avg unmatched exposure $ | 0.07 | 0.07 | 0.08 | 0.20 |
| longest losing streak | 12.3 | 6.0 | 12.0 | 16.0 |

| period | net: initial UP | net: initial DOWN | net: TREND_UP (n) | net: TREND_DOWN (n) | net: CHEAPEST fallback (n) | net: reversal markets (n) | net: no-reversal markets (n) |
|---|---:|---:|---:|---:|---:|---:|---:|
| train | -13.01 | -36.04 | -0.23 (17) | -3.04 (15) | -45.78 (157) | -55.97 (183) | +6.91 (6) |
| validation | -1.63 | -9.42 | +1.13 (3) | -1.84 (6) | -10.33 (52) | -16.78 (59) | +5.73 (2) |
| test | -8.42 | -4.70 | -3.11 (9) | -0.70 (14) | -9.31 (102) | -14.97 (121) | +1.84 (4) |
| forward | -121.35 | -123.83 | -8.95 (64) | -9.87 (53) | -226.35 (931) | -367.76 (953) | +122.58 (94) |

### C) trend + hedge if pair ≤ 0.98

| metric | train | validation | test | forward |
|---|---:|---:|---:|---:|
| markets | 193 | 63 | 128 | 1081 |
| with initial trade | 189 | 61 | 125 | 1049 |
| initial trade rate | 97.9% | 96.8% | 97.7% | 97.0% |
| fills | 314 | 102 | 206 | 1598 |
| reversal rate | 96.8% | 96.7% | 96.8% | 91.0% |
| hedge rate (of reversals) | 68.3% | 69.5% | 66.9% | 57.5% |
| hedge completion | 100.0% | 100.0% | 100.0% | 100.0% |
| avg initial price | 0.435 | 0.436 | 0.430 | 0.411 |
| avg hedge price | 0.409 | 0.407 | 0.406 | 0.403 |
| avg pair cost (prices) | 0.856 | 0.854 | 0.847 | 0.830 |
| fees $ | 25.18 | 8.07 | 16.26 | 125.40 |
| slippage allowance $ | 7.85 | 2.55 | 5.15 | 39.94 |
| **net P&L $** | **-46.35** | **-9.24** | **-34.73** | **-317.71** |
| expectancy / market $ | -0.245 | -0.151 | -0.278 | -0.303 |
| win rate (markets) | 68.3% | 70.5% | 66.4% | 58.5% |
| max drawdown $ | 46.35 | 15.30 | 40.40 | 321.08 |
| avg unmatched exposure $ | 0.70 | 0.67 | 0.72 | 0.93 |
| longest losing streak | 3.0 | 2.0 | 4.0 | 13.0 |

| period | net: initial UP | net: initial DOWN | net: TREND_UP (n) | net: TREND_DOWN (n) | net: CHEAPEST fallback (n) | net: reversal markets (n) | net: no-reversal markets (n) |
|---|---:|---:|---:|---:|---:|---:|---:|
| train | -23.25 | -23.10 | -2.72 (17) | +1.57 (15) | -45.20 (157) | -53.26 (183) | +6.91 (6) |
| validation | +0.95 | -10.18 | +1.88 (3) | -0.33 (6) | -10.78 (52) | -14.97 (59) | +5.73 (2) |
| test | -26.22 | -8.51 | -11.14 (9) | -4.58 (14) | -19.01 (102) | -36.60 (121) | +1.87 (4) |
| forward | -151.66 | -166.05 | -13.24 (65) | -2.54 (53) | -301.93 (931) | -441.04 (954) | +123.33 (95) |

## 3. Does the mandatory initial trade have negative expectancy?

Variant A is exactly that trade alone (held to settlement, one fee): expectancy per market train -0.085 $, validation -0.295 $, test -0.274 $, forward -0.265 $. By how the side was chosen, see the TREND / CHEAPEST columns above.

