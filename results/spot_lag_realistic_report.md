# Spot-lag realistic optimisation: tables

Candidates (HYPE excluded): 15504; latency draws 6; configurations in stage 1: 9450. Hours: train 33.6 (blocks 0-6), validation 17.3 (blocks 7-9), test 12.0 (blocks >= 10, after 00:45 UTC 09-25). Block = 6 h from 09-22 12:45 UTC.

## Latency stages actually drawn (ms, all candidates x draws)

| stage | p50 | p75 | p90 | p95 | p99 |
|---|---:|---:|---:|---:|---:|
| spot | 135 | 186 | 282 | 332 | 2216 |
| book | 90 | 168 | 207 | 226 | 255 |
| proc | 5 | 26 | 45 | 52 | 84 |
| pre | 49 | 62 | 78 | 90 | 176 |
| rtt | 245 | 364 | 648 | 888 | 1523 |
| shock on this machine -> order at the venue | 198 | 265 | 391 | 511 | 828 |
| quote age at the decision | 117 | 198 | 262 | 396 | 1068 |

## Stage 1: entry grid, selected on TRAIN, filtered on VALIDATION, locked, then the untouched TEST

Locked 2026-09-25 13:28:02 UTC: **3σ, edge 2c, +3c, spread 5c, ask≤0.60, 60–295s, quote≤250ms, spot≤1000ms** (smoothed train net +42.73, train +46.39 on 69 fills, validation +12.94 on 16).

| ranking | configuration | train net (fills) | validation net (fills) | **test net (fills)** | smoothed train |
|---|---|---:|---:|---:|---:|
| production (MAX_ASK 0.95) | 3σ, edge 2c, +2c, spread 3c, ask≤0.95, 60–295s, quote≤250ms, spot≤1000ms | +41.57 (109) | +5.04 (39) | **-4.31 (34)** | +37.65 |
| ma74 paper bot | 3σ, edge 2c, +2c, spread 3c, ask≤0.74, 60–295s, quote≤250ms, spot≤1000ms | +42.21 (85) | +12.28 (28) | **-3.24 (22)** | +39.50 |
| **selected (robust rule)** | 3σ, edge 2c, +3c, spread 5c, ask≤0.60, 60–295s, quote≤250ms, spot≤1000ms | +46.39 (69) | +12.94 (16) | **-0.48 (16)** | +42.73 |
| best raw training net | 3σ, edge 2c, +3c, spread 5c, ask≤0.60, 60–295s, quote≤250ms, spot≤1000ms | +46.39 (69) | +12.94 (16) | **-0.48 (16)** | +42.73 |
| best validation net | 4σ, edge 1c, +3c, spread 5c, ask≤0.60, 60–295s, quote≤250ms, spot≤1000ms | +38.82 (62) | +16.29 (19) | **+1.07 (11)** | +36.00 |
| best test net (NOT selectable: chosen on the test) | 2.5σ, edge 6c, +1c, spread 2c, ask≤0.60, 60–295s, quote≤250ms, spot≤1000ms | +32.80 (35) | +7.06 (6) | **+3.44 (5)** | +31.90 |

Of 9450 configurations, 3580 are eligible on training; 1249 of all have a positive test net (13%); among the eligible ones 1127 (31%). Rank correlation (Spearman) of train net with test net over eligible configs: +0.44; validation with test: +0.48.

Test net across ALL configurations: p10 -3.03, p25 -1.71, p50 -0.77, p75 +0.00, p90 +1.07

## Walk-forward (6 h blocks; train = blocks 0..i+3, validation = block i+4, test = block i+5; the same rule each fold)

| fold | train blocks | val | test block (UTC start) | chosen configuration | test net (fills) | production in that block | ma74 in that block |
|---|---|---|---|---|---:|---:|---:|
| 0 | 0-3 | 4 | 5 (09-23 18:45) | 3σ, edge 3c, +3c, spread 5c, ask≤0.74, 60–295s, quote≤250ms, spot≤1000ms | +4.57 (19) | +3.02 | +3.20 |
| 1 | 0-4 | 5 | 6 (09-24 00:45) | 3σ, edge 2c, +3c, spread 5c, ask≤0.60, 60–295s, quote≤250ms, spot≤1000ms | +4.85 (8) | +4.33 | +3.74 |
| 2 | 0-5 | 6 | 7 (09-24 06:45) | 3σ, edge 2c, +3c, spread 5c, ask≤0.60, 60–295s, quote≤250ms, spot≤1000ms | +0.45 (3) | -3.39 | -0.04 |
| 3 | 0-6 | 7 | 8 (09-24 12:45) | 3σ, edge 2c, +3c, spread 5c, ask≤0.74, 60–295s, quote≤250ms, spot≤1000ms | +0.47 (9) | -0.86 | +0.46 |
| 4 | 0-7 | 8 | 9 (09-24 18:45) | 3σ, edge 2c, +3c, spread 5c, ask≤0.60, 60–295s, quote≤250ms, spot≤1000ms | +11.23 (8) | +9.29 | +11.86 |
| 5 | 0-8 | 9 | 10 (09-25 00:45) **(untouched test)** | 3σ, edge 2c, +3c, spread 5c, ask≤0.60, 60–295s, quote≤250ms, spot≤1000ms | -0.42 (9) | -1.07 | -0.91 |
| 6 | 0-9 | 10 | 11 (09-25 06:45) **(untouched test)** | 3σ, edge 4c, +3c, spread 5c, ask≤0.70, 60–295s, quote≤250ms, spot≤1000ms | +0.46 (4) | -3.24 | -2.34 |
| 7 | 0-10 | 11 | 12 (09-25 12:45) **(untouched test)** | 3σ, edge 3c, +3c, spread 5c, ask≤0.70, 60–295s, quote≤250ms, spot≤1000ms | +0.00 (0) | +0.00 | +0.00 |

Walk-forward total: **+21.60 USDC over 8 folds, 6 positive**; production over the same blocks +8.07; ma74 +15.98.

## Stage 2: quote-age and spot-age gates on the selected configuration (net USDC, mean over draws)

| max quote age (ms) | spot ≤ 300 ms: train / val / test | spot ≤ 500 ms: train / val / test | spot ≤ 1000 ms: train / val / test | spot ≤ 2000 ms: train / val / test |
|---:|---|---|---|---|
| 100 | +19.0 (33) / +6.1 / -0.6 | +21.7 (36) / +7.2 / -0.7 | +21.9 (36) / +7.3 / -0.7 | +21.9 (36) / +7.3 / -0.7 |
| 200 | +34.7 (56) / +9.2 / -0.4 | +39.0 (60) / +10.7 / -0.5 | +39.5 (61) / +10.7 / -0.7 | +39.5 (61) / +10.7 / -0.7 |
| 250 | +40.5 (63) / +10.8 / -0.2 | +45.8 (68) / +12.8 / -0.4 | +46.4 (69) / +12.9 / -0.5 | +46.4 (69) / +12.9 / -0.5 |
| 300 | +47.0 (75) / +13.0 / +0.6 | +52.4 (80) / +14.9 / +0.5 | +53.2 (82) / +15.1 / +0.4 | +53.2 (82) / +15.1 / +0.4 |
| 500 | +58.7 (95) / +17.0 / +0.8 | +64.4 (102) / +18.8 / +0.7 | +65.7 (104) / +19.0 / +0.6 | +65.7 (104) / +19.0 / +0.6 |
| 750 | +67.9 (110) / +19.6 / +0.7 | +74.2 (118) / +21.8 / +0.4 | +75.8 (121) / +21.8 / +0.3 | +75.8 (121) / +21.8 / +0.3 |
| 1000 | +70.6 (118) / +25.1 / +1.1 | +77.3 (126) / +27.8 / +1.0 | +78.7 (129) / +27.8 / +0.9 | +78.7 (129) / +27.8 / +0.9 |
| 1500 | +70.7 (121) / +25.5 / +2.8 | +77.2 (130) / +28.2 / +2.8 | +78.7 (132) / +28.1 / +2.6 | +78.7 (132) / +28.1 / +2.6 |

Best on train + validation: quote ≤ 1000 ms, spot ≤ 500 ms (dev +105.12, the least permissive gate within 2 % of the best +106.83; production 250 / 1000: +59.33). Kept only if the gain is > 5 %.

## Stage 3: exits on the selected configuration (net USDC, mean over draws; hold s)

| exit | train net (fills) | validation | test | all c/share | hold | max DD (all) |
|---|---:|---:|---:|---:|---:|---:|
| live | +77.27 (126) | +27.84 | +0.99 | +12.1 | 6.0 | 2.87 |
| t3 | +73.74 (126) | +28.36 | +2.06 | +11.8 | 3.5 | 1.68 |
| t5 | +74.83 (126) | +27.64 | +0.49 | +11.7 | 5.8 | 2.78 |
| t10 | +78.97 (126) | +27.46 | +1.51 | +12.3 | 10.7 | 3.71 |
| t20 | +78.02 (126) | +24.76 | +4.12 | +12.1 | 20.7 | 5.11 |
| tp3_10 | +75.89 (126) | +27.06 | +2.83 | +12.0 | 4.2 | 2.12 |
| tp5_10 | +74.80 (126) | +27.64 | +3.42 | +12.0 | 4.5 | 2.07 |
| tp5_20 | +76.86 (126) | +28.64 | +4.20 | +12.5 | 5.9 | 2.38 |
| tp10_20 | +76.47 (126) | +27.07 | +3.25 | +12.1 | 8.2 | 3.92 |
| tp10_settle | +66.29 (126) | +31.16 | +0.55 | +11.1 | 30.6 | 8.33 |
| sl3_10 | +60.70 (126) | +19.04 | +0.31 | +9.1 | 7.7 | 3.14 |
| sl5_10 | +74.19 (126) | +24.66 | +1.21 | +11.4 | 9.4 | 3.50 |
| tp5sl3_20 | +57.84 (126) | +19.19 | +2.54 | +9.0 | 3.1 | 1.68 |
| tp10sl5_20 | +70.41 (126) | +25.09 | +3.21 | +11.2 | 5.8 | 2.36 |
| tp5sl5_settle | +69.75 (126) | +26.29 | +3.53 | +11.3 | 4.5 | 1.57 |

Best on train + validation: t10 (+106.44 vs live +105.12); selected: **live**.

## Stage 4: order size (existing caps: order ≤ 5 USDC, exposure ≤ 10; fills need the top level to hold the whole size)

| size | shares (typical) | orders | fills | fill rate | train net | validation | test | all net | max DD | note |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| venue min | 5 | 888 | 176 | 20% | +77.27 | +27.84 | +0.99 | +106.11 | 2.87 |  |
| $1 | 5 | 888 | 173 | 19% | +87.56 | +32.08 | -0.38 | +119.27 | 3.35 |  |
| $2 | 5 | 889 | 164 | 18% | +118.69 | +37.95 | -0.15 | +156.49 | 3.42 |  |
| $3 | 7 | 880 | 154 | 18% | +152.71 | +51.48 | -0.56 | +203.63 | 4.68 |  |
| $5 | 11 | 848 | 132 | 16% | +208.31 | +58.21 | -0.77 | +265.75 | 7.73 |  |
| $10 | 23 | 735 | 95 | 13% | +191.93 | +89.00 | -7.12 | +273.81 | 14.38 | needs the 5 USDC order cap raised |

## Finalists by period (trade level, mean over draws; net range across draws)

### train (33.6 h)

| candidate | orders | fills | fill rate | fills/h | net USDC [draw range] | c/share | win | PF | max DD | worst trade | losing streak | t |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| production | 588 | 109 | 19% | 3.2 | +41.57 [+40.3, +43.3] | +7.6 | 64% | 4.49 | 1.76 | -1.40 | 4 | +4.4 |
| ma74 | 511 | 85 | 17% | 2.5 | +42.21 [+40.5, +43.7] | +9.9 | 70% | 6.56 | 1.49 | -1.40 | 3 | +4.8 |
| selected | 408 | 69 | 17% | 2.1 | +46.39 [+42.9, +49.0] | +13.4 | 77% | 9.04 | 1.41 | -1.40 | 2 | +4.8 |
| best validation | 313 | 62 | 20% | 1.9 | +38.82 [+35.6, +41.1] | +12.5 | 70% | 5.65 | 1.67 | -1.08 | 4 | +3.0 |
| **recommended** (selected + gates + exit) | 494 | 126 | 26% | 3.8 | +77.27 [+73.5, +79.9] | +12.2 | 79% | 9.05 | 1.53 | -1.48 | 3 | +5.8 |

### validation (17.3 h)

| candidate | orders | fills | fill rate | fills/h | net USDC [draw range] | c/share | win | PF | max DD | worst trade | losing streak | t |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| production | 297 | 39 | 13% | 2.3 | +5.04 [+3.7, +7.3] | +2.6 | 50% | 1.38 | 4.52 | -2.31 | 5 | +0.5 |
| ma74 | 242 | 28 | 11% | 1.6 | +12.28 [+10.0, +14.7] | +8.9 | 66% | 4.25 | 1.85 | -1.12 | 3 | +1.4 |
| selected | 190 | 16 | 9% | 1.0 | +12.94 [+11.8, +14.5] | +15.8 | 75% | 16.03 | 0.53 | -0.50 | 2 | +1.6 |
| best validation | 140 | 19 | 13% | 1.1 | +16.29 [+15.9, +17.1] | +17.4 | 80% | 21.74 | 0.44 | -0.38 | 2 | +2.0 |
| **recommended** (selected + gates + exit) | 216 | 28 | 13% | 1.6 | +27.84 [+24.9, +31.1] | +20.3 | 78% | 22.80 | 0.53 | -0.50 | 2 | +2.9 |

### test (12.0 h)

| candidate | orders | fills | fill rate | fills/h | net USDC [draw range] | c/share | win | PF | max DD | worst trade | losing streak | t |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| production | 218 | 34 | 16% | 2.9 | -4.31 [-5.1, -3.0] | -2.5 | 35% | 0.55 | 6.33 | -1.26 | 8 | -1.4 |
| ma74 | 183 | 22 | 12% | 1.8 | -3.24 [-5.2, -1.2] | -3.0 | 29% | 0.52 | 4.93 | -1.23 | 7 | -1.2 |
| selected | 157 | 16 | 10% | 1.3 | -0.48 [-2.2, +0.2] | -0.6 | 34% | 0.97 | 2.53 | -0.79 | 6 | -0.2 |
| best validation | 126 | 11 | 8% | 0.9 | +1.07 [+0.2, +3.2] | +2.2 | 43% | 1.28 | 1.10 | -0.89 | 4 | +0.5 |
| **recommended** (selected + gates + exit) | 178 | 22 | 12% | 1.8 | +0.99 [-1.9, +3.4] | +0.9 | 40% | 1.27 | 2.87 | -0.79 | 6 | +0.3 |

### all (62.9 h)

| candidate | orders | fills | fill rate | fills/h | net USDC [draw range] | c/share | win | PF | max DD | worst trade | losing streak | t |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| production | 1103 | 182 | 17% | 2.9 | +42.29 [+38.9, +45.8] | +4.6 | 56% | 2.27 | 6.33 | -2.31 | 8 | +3.0 |
| ma74 | 936 | 135 | 14% | 2.1 | +51.25 [+45.4, +54.6] | +7.6 | 62% | 3.89 | 4.96 | -1.52 | 7 | +4.0 |
| selected | 756 | 102 | 13% | 1.6 | +58.84 [+55.8, +62.4] | +11.6 | 70% | 6.77 | 2.53 | -1.40 | 6 | +4.5 |
| best validation | 580 | 92 | 16% | 1.5 | +56.18 [+52.0, +58.2] | +12.3 | 69% | 5.89 | 1.67 | -1.14 | 4 | +3.6 |
| **recommended** (selected + gates + exit) | 888 | 176 | 20% | 2.8 | +106.11 [+99.1, +110.4] | +12.1 | 74% | 8.11 | 2.87 | -1.48 | 6 | +6.2 |

## P&L by 6 h block (mean over draws): recommended vs production

| block | UTC start | period | recommended net (fills) | production net (fills) |
|---:|---|---|---:|---:|
| 0 | 09-22 12:45 | train | +19.65 (12) | +9.54 (9) |
| 1 | 09-22 18:45 | train | +16.29 (20) | +12.41 (18) |
| 2 | 09-23 00:45 | train | +11.17 (22) | +7.46 (17) |
| 3 | 09-23 06:45 | train | +9.86 (20) | +4.05 (16) |
| 4 | 09-23 12:45 | train | +1.89 (4) | +0.76 (6) |
| 5 | 09-23 18:45 | train | +8.93 (26) | +3.02 (25) |
| 6 | 09-24 00:45 | train | +9.49 (22) | +4.33 (19) |
| 7 | 09-24 06:45 | validation | +6.74 (6) | -3.39 (10) |
| 8 | 09-24 12:45 | validation | +7.93 (10) | -0.86 (10) |
| 9 | 09-24 18:45 | validation | +13.17 (11) | +9.29 (19) |
| 10 | 09-25 00:45 | TEST | -0.62 (13) | -1.07 (18) |
| 11 | 09-25 06:45 | TEST | +1.39 (9) | -3.24 (16) |
| 12 | 09-25 12:45 | TEST | +0.22 (0) | +0.00 (0) |

## Counterfactual: where the paper edge goes (per share, mean over draws; ALL periods and TEST)

Each row adds one realism step to the one above. 'signal-time' = the book the bot saw when it decided.

| step | fills (all) | c/share (all) | net USDC (all) | fills (test) | c/share (test) | net USDC (test) |
|---|---:|---:|---:|---:|---:|---:|
| **production: 3σ, edge 2c, +2c, spread 3c, ask≤0.95, 60–295s, quote≤250ms, spot≤1000ms** | | | | | | |
| 1. fill at the signal-time ask, exit at MID 5 s later, no fees, no latency, no budget | 1507 | +11.4 | +858.2 | 291 | +10.9 | +158.1 |
| 2. + exit at the BID (spread paid) | 1514 | +10.7 | +806.3 | 293 | +10.0 | +146.4 |
| 3. + both taker fees | 1514 | +7.9 | +597.3 | 293 | +7.2 | +106.0 |
| 4. + latency: fill only if the ask at ARRIVAL (+500 ms venue proxy) is within the limit; exit at the bid seen 5 s after the fill | 344 | +5.9 | +101.6 | 60 | -1.3 | -3.9 |
| 5. + exit execution: FOK sell at bid − 2c with depth, 2 s retries, settlement in the last 10 s | 344 | +5.9 | +101.2 | 60 | -1.4 | -4.1 |
| 6. + quote/spot-age gates, the portfolio budget (4 positions, 10 USDC, 60/h, 1 entry per market), all draws | 182 | +4.6 | +42.3 | 34 | -2.5 | -4.3 |
| **recommended: 3σ, edge 2c, +3c, spread 5c, ask≤0.60, 60–295s, quote≤1000ms, spot≤500ms** | | | | | | |
| 1. fill at the signal-time ask, exit at MID 5 s later, no fees, no latency, no budget | 1015 | +14.4 | +731.2 | 203 | +14.4 | +146.1 |
| 2. + exit at the BID (spread paid) | 1016 | +13.7 | +695.6 | 203 | +13.5 | +137.1 |
| 3. + both taker fees | 1016 | +10.7 | +543.4 | 203 | +10.5 | +106.9 |
| 4. + latency: fill only if the ask at ARRIVAL (+500 ms venue proxy) is within the limit; exit at the bid seen 5 s after the fill | 222 | +10.8 | +119.5 | 30 | +2.8 | +4.2 |
| 5. + exit execution: FOK sell at bid − 2c with depth, 2 s retries, settlement in the last 10 s | 222 | +10.5 | +116.1 | 30 | +2.1 | +3.2 |
| 6. + quote/spot-age gates, the portfolio budget (4 positions, 10 USDC, 60/h, 1 entry per market), all draws | 176 | +12.1 | +106.1 | 22 | +0.9 | +1.0 |

The live paper bots' own fills, audited against the recorded book (docs/spot_lag_paper_history_2026-09-25.md): 10 of tpb-spotlag-paper's 37 fills and 13 of ma74's 20 fills could not have filled; those 13 carried +8.78 of ma74's +12.63 USDC.

## Monte Carlo (recommended configuration): 2,000 runs, each a random latency draw, a random fill-drop rate U(0, 30 %), extra slippage U(0, 1c) per leg, a delayed exit (t10 instead of the production exit) with probability 30 %, and a shuffled trade order

**all** (62.9 h): cumulative net P10 +68.42, P25 +75.30, P50 +83.09, P75 +91.10, P90 +97.36; P(net > 0) 100%; max drawdown P50 1.92, P90 2.78

**test** (12.0 h): cumulative net P10 -2.49, P25 -1.32, P50 -0.00, P75 +1.31, P90 +2.19; P(net > 0) 50%; max drawdown P50 2.29, P90 3.68

**dev** (50.9 h): cumulative net P10 +68.41, P25 +75.68, P50 +83.77, P75 +90.97, P90 +96.82; P(net > 0) 100%; max drawdown P50 1.70, P90 2.55

## Stress tests (net USDC, mean over draws): recommended | production

| scenario | dev (train + validation) | test | all | all c/share (rec) |
|---|---:|---:|---:|---:|
| normal (measured latency, venue proxy 500 ms) | +105.12 (154) \| +46.61 (148) | +0.99 (22) \| -4.31 (34) | +106.11 (176) \| +42.29 (182) | +12.1 |
| slow network: every latency stage x1.5 | +96.02 (139) \| +27.05 (112) | -0.35 (22) \| -4.22 (26) | +95.67 (161) \| +22.84 (138) | +11.9 |
| stale book: venue proxy 750 ms | +94.65 (141) \| +35.65 (147) | -0.16 (26) \| -4.05 (37) | +94.49 (167) \| +31.61 (184) | +11.3 |
| low liquidity: top-level depth halved | +92.89 (140) \| +37.44 (135) | -0.54 (19) \| -5.59 (32) | +92.35 (160) \| +31.84 (167) | +11.6 |
| bad fills: +1c slippage on each leg | +89.57 (154) \| +31.71 (148) | -1.23 (22) \| -7.74 (34) | +88.33 (176) \| +23.96 (182) | +10.0 |
| fees x1.5 | +94.18 (154) \| +36.71 (148) | -0.73 (22) \| -6.64 (34) | +93.45 (176) \| +30.08 (182) | +10.6 |
| adverse selection: the best 25 % of fills never happen | -11.12 (38) \| -23.94 (63) | -4.12 (12) \| -9.43 (23) | -15.23 (50) \| -33.36 (86) | -6.1 |
| combined worst reasonable: latency x2, proxy 1000 ms, +1c slippage, depth halved | +51.90 (106) \| +5.71 (87) | -3.71 (19) \| -7.04 (22) | +48.19 (125) \| -1.32 (110) | +7.7 |

## Sensitivity: one parameter at a time from the recommended configuration (dev = train + validation; net USDC, fills, max DD; then test net)

| parameter | value | dev net | fills | fill rate | max DD | test net |
|---|---|---:|---:|---:|---:|---:|
| k | 2.5 | +96.71 | 189 | 23% | 3.47 | +2.04 |
| k | 3.0 **(rec)** | +105.12 | 154 | 22% | 1.53 | +0.99 |
| k | 3.5 | +101.49 | 137 | 23% | 1.48 | +1.22 |
| k | 4.0 | +93.57 | 119 | 25% | 1.01 | +0.99 |
| k | 5.0 | +72.46 | 88 | 27% | 2.17 | +1.81 |
| edge | 0.01 | +102.58 | 185 | 21% | 1.70 | -2.04 |
| edge | 0.02 **(rec)** | +105.12 | 154 | 22% | 1.53 | +0.99 |
| edge | 0.03 | +99.60 | 136 | 22% | 1.53 | +2.24 |
| edge | 0.04 | +97.52 | 119 | 23% | 1.51 | +2.47 |
| edge | 0.06 | +89.38 | 94 | 27% | 1.48 | +5.08 |
| slip | 0.01 | +99.60 | 138 | 19% | 1.53 | +1.11 |
| slip | 0.02 | +102.90 | 148 | 21% | 1.53 | +0.87 |
| slip | 0.03 **(rec)** | +105.12 | 154 | 22% | 1.53 | +0.99 |
| spread | 0.02 | +92.61 | 131 | 21% | 1.48 | +1.56 |
| spread | 0.03 | +101.88 | 144 | 21% | 1.51 | +1.39 |
| spread | 0.05 **(rec)** | +105.12 | 154 | 22% | 1.53 | +0.99 |
| max_ask | 0.5 | +87.75 | 115 | 21% | 1.52 | +1.53 |
| max_ask | 0.6 **(rec)** | +105.12 | 154 | 22% | 1.53 | +0.99 |
| max_ask | 0.7 | +102.95 | 187 | 21% | 1.97 | +0.29 |
| max_ask | 0.74 | +105.15 | 198 | 22% | 1.94 | -1.66 |
| max_ask | 0.8 | +98.78 | 219 | 22% | 2.77 | -1.11 |
| max_ask | 0.95 | +92.63 | 243 | 23% | 3.41 | -1.78 |
| window | (60, 295) **(rec)** | +105.12 | 154 | 22% | 1.53 | +0.99 |
| window | (60, 240) | +88.90 | 128 | 22% | 1.73 | +1.38 |
| window | (60, 180) | +75.21 | 92 | 25% | 1.69 | -0.48 |
| window | (30, 180) | +88.93 | 102 | 26% | 1.69 | +0.51 |
| window | (30, 120) | +50.42 | 48 | 25% | 1.54 | +0.57 |
| window | (60, 120) | +36.41 | 38 | 24% | 1.58 | -0.42 |
| window | (30, 90) | +40.53 | 31 | 32% | 1.54 | +1.01 |
| qage | 100 | +28.88 | 44 | 15% | 1.21 | -0.71 |
| qage | 200 | +49.72 | 74 | 15% | 1.40 | -0.54 |
| qage | 250 | +58.62 | 84 | 14% | 1.41 | -0.36 |
| qage | 300 | +67.37 | 99 | 16% | 1.46 | +0.49 |
| qage | 500 | +83.20 | 122 | 18% | 1.54 | +0.68 |
| qage | 750 | +96.07 | 141 | 20% | 1.54 | +0.39 |
| qage | 1000 **(rec)** | +105.12 | 154 | 22% | 1.53 | +0.99 |
| qage | 1500 | +105.41 | 158 | 22% | 1.57 | +2.76 |
| sage | 300 | +95.75 | 143 | 22% | 1.59 | +1.05 |
| sage | 500 **(rec)** | +105.12 | 154 | 22% | 1.53 | +0.99 |
| sage | 1000 | +106.53 | 156 | 22% | 1.53 | +0.86 |
| sage | 2000 | +106.53 | 156 | 22% | 1.53 | +0.86 |

| parameter | dev net range (USDC) | fills range | max DD range | test net min..max | class |
|---|---:|---:|---:|---|---|
| k | 32.7 | 101 | 2.46 | +1.0..+2.0 | high impact |
| edge | 15.7 | 92 | 0.22 | -2.0..+5.1 | high impact |
| slip | 5.5 | 16 | 0.00 | +0.9..+1.1 | medium |
| spread | 12.5 | 23 | 0.04 | +1.0..+1.6 | high impact |
| max_ask | 17.4 | 128 | 1.89 | -1.8..+1.5 | high impact |
| window | 68.7 | 123 | 0.20 | -0.5..+1.4 | high impact |
| qage | 76.5 | 114 | 0.36 | -0.7..+2.8 | high impact |
| sage | 10.8 | 14 | 0.06 | +0.9..+1.1 | high impact |
| exit rule | 29.4 | – | – | +0.3..+4.2 | high impact |

## Where the profit comes from (recommended configuration, draw 0): c/share (fills) by bucket, ALL periods | TEST

**asset**: BNB: +11.4 (29) | +1.6 (1); BTC: +13.1 (51) | +2.5 (5); DOGE: +13.7 (16) | +7.3 (2); ETH: +8.9 (33) | -5.2 (8); SOL: +11.6 (26) | -6.6 (4); XRP: +9.4 (18) | +nan (0)

**direction**: DOWN: +10.8 (92) | -2.1 (12); UP: +12.2 (81) | -1.8 (8)

**time left (s)**: (0, 90]: +22.8 (22) | -21.1 (1); (150, 210]: +6.9 (47) | -0.3 (7); (210, 300]: +8.0 (58) | -0.2 (7); (90, 150]: +15.1 (46) | -2.9 (5)

**lag = fair − ask (c)**: (12, 100]: +18.9 (85) | +10.2 (4); (4, 6]: +16.5 (5) | -0.9 (1); (6, 8]: +1.8 (20) | -6.4 (3); (8, 12]: +4.1 (63) | -4.9 (12)

**spot move z1 (σ)**: (0.0, 3.5]: +6.0 (45) | -3.5 (7); (3.5, 4.5]: +8.2 (53) | +0.9 (11); (4.5, 6.0]: +13.6 (45) | -21.1 (1); (6.0, 100.0]: +22.3 (30) | -3.4 (1)

**spread (c)**: (0.0, 1.01]: +11.7 (128) | -1.4 (16); (1.01, 2.01]: +13.2 (19) | -0.9 (2); (2.01, 3.01]: +14.0 (15) | -7.2 (1); (3.01, 5.01]: +2.7 (11) | -8.1 (1)

**volatility σ (bps/s)**: (0.0, 0.6]: +9.7 (83) | -3.5 (12); (0.6, 1.0]: +14.3 (50) | -1.2 (6); (1.0, 1.5]: +10.8 (30) | +15.6 (1); (1.5, 100.0]: +13.9 (10) | -5.4 (1)

**entry price**: (0.0, 0.2]: +15.9 (34) | -1.1 (2); (0.2, 0.4]: +14.0 (63) | -0.5 (8); (0.4, 0.6]: +7.4 (76) | -3.3 (10)

**hold (s)**: (0.0, 5.5]: +11.4 (96) | +2.0 (6); (10.0, 60.0]: +1.8 (2) | -8.1 (1); (5.5, 7.0]: +14.8 (50) | -3.0 (8); (7.0, 10.0]: +5.9 (25) | -3.8 (5)

**exit kind**: sold: +11.5 (173) | -1.9 (20)

**hour (UTC)**: 00-06: +6.0 (54) | -4.5 (9); 06-12: +10.1 (37) | +0.7 (9); 12-18: +16.7 (28) | -2.2 (2); 18-24: +15.1 (54) | +nan (0)

Concentration: the top 5 fills are +16.63 of +99.11 USDC; net without them +82.48. Top 5 fills: 09-22 17:48:52 SOL UP @0.17 +3.99; 09-22 17:48:53 BNB UP @0.07 +3.83; 09-24 23:12:40 ETH DOWN @0.32 +3.06; 09-24 09:28:40 BTC DOWN @0.21 +3.04; 09-24 23:12:40 BNB DOWN @0.39 +2.70

## Price path after the recommended configuration's fills (draw 0): best and worst bid vs entry within 30 s

fills 173: MFE mean +27.9c (median +23.0), time to MFE median 14.2 s; MAE mean -7.3c (median -3.0), time to MAE median 0.2 s; fell back below the entry after the best: 39%; reached +5c 93%, +10c 84%; went 5c under 35%, 10c under 24%

## Success criteria (recommended configuration)

| criterion | met |
|---|---|
| positive validation expectancy | YES |
| positive untouched-test expectancy | YES |
| positive walk-forward expectancy | YES |
| reasonable drawdown (all-period max DD <= 5 USDC and <= 50 % of net) | YES |
| robust across execution stress (test net > 0 under every single stress except adverse selection, and >= 0 under the combined worst case) | **NO** |
| test edge is more than noise (Monte Carlo P(test net > 0) >= 90 %) | **NO** |
| not dependent on unrealistic fills (every fill is at the arrival book; Monte Carlo P(all-period net > 0) >= 90 %) | YES |
| stable across nearby parameter values (no sign change in the dev net across any parameter's grid) | YES |

**Verdict: NO ROBUST EDGE FOUND** (6 of 8 criteria met)

