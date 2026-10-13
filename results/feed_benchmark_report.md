# Multi-source BTC feed benchmark (BTC)

Recordings: feedbench_20260930_184451.jsonl.gz — 2026-09-30 18:44 UTC → 2026-09-30 18:54 UTC (10 min, 549,270 records, 14 quoting sources). DEV = first 50 %, VALIDATION = next 25 %, TEST = last 25 % (2026-09-30 18:52 UTC onward).

## 1. Delivery: what each source sends and how late it arrives

Delay is `receipt − exchange event time`, on the exchange's clock (its offset to ours is inside the number; Hyperliquid stamps block time). Binance spot's `bookTicker` carries no event time: its trades stand in. Stale = event time more than 2 s before receipt; dup / out-of-order per the validator (`feeds/sources.py`). Gaps in sequence numbers are informational -- most venues number every book change, not every message -- and are not shown.

| source | quotes/min | trades/min | delay p50 | p90 | p95 | p99 | max | stale | dup | out-of-order | silences > 5 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| deribit | 651 | 126 | 33 | 40 | 46 | 79 | 233 | 0% | 0% | 0% | 0 |
| kraken | 641 | 102 | 38 | 59 | 70 | 93 | 1478 | 0% | 0% | 0% | 0 |
| bitstamp | 554 | 24 | 46 | 65 | 69 | 76 | 99 | 0% | 0% | 0% | 0 |
| coinbase_adv | 286 | 348 | 68 | 69 | 70 | 76 | 183 | 0% | 0% | 24% | 0 |
| coinbase | 338 | 338 | 71 | 82 | 96 | 162 | 584 | 0% | 0% | 0% | 0 |
| gemini | 399 | 12 | 111 | 141 | 156 | 300 | 364 | 0% | 0% | 0% | 12 |
| bybit | 482 | 591 | 117 | 120 | 122 | 133 | 1401 | 0% | 0% | 0% | 0 |
| bybit_linear | 1186 | 1011 | 117 | 118 | 120 | 131 | 345 | 0% | 0% | 0% | 0 |
| okx_swap | 1506 | 579 | 148 | 152 | 154 | 169 | 259 | 0% | 0% | 0% | 0 |
| okx | 482 | 107 | 148 | 150 | 153 | 162 | 197 | 0% | 0% | 0% | 0 |
| binance | 6414 | 511 | 152 | 238 | 271 | 406 | 516 | 0% | 0% | 0% | 0 |
| binance_futures | 18392 | 0 | 178 | 318 | 390 | 736 | 902 | 0% | 0% | 0% | 0 |
| hyperliquid | 409 | 218 | 360 | 452 | 542 | 644 | 815 | 0% | 0% | 35% | 0 |
| pm_chainlink | 59 | 0 | 1466 | 1914 | 2023 | 2232 | 2536 | 7% | 0% | 0% | 0 |

Recorder loop lag (a 100 ms timer firing late, samples over 2 ms): 11 samples, p50 3.3 ms, p99 8.1 ms, max 32.8 ms over the recording; receipt times carry at most this much of our own delay.

## 2. Shock events: which source tells this machine first

Events are consensus 1 s returns beyond the threshold (1, 2, 5, 10 bp), 5 s apart. A source detects one when its own mid has moved half the event's size (receipt time). `first` = share of events it detected before every other source; `lag` = ms after the first source (0 when it is the first); `vs coinbase` = ms before (−) or after (+) the live bot's current source; `missed` = never crossed half the move within ±1.5 s.

### 1 bp: 25 events, 25 with two or more detections (2026-09-30 18:45 UTC → 2026-09-30 18:54 UTC)

| source | detected | first | lag p50 | p90 | p95 | p99 | max | vs coinbase p50 | vs binance p50 | missed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| deribit | 25 | 60% | 0 | 855 | 967 | 989 | 1222 | -32 | -176 | 0 |
| coinbase | 25 | 20% | 197 | 598 | 960 | 983 | 1022 | 0 | -125 | 0 |
| okx | 25 | 4% | 130 | 827 | 864 | 965 | 1242 | 2 | -125 | 0 |
| bybit_linear | 25 | 4% | 136 | 516 | 1114 | 1317 | 1497 | 47 | -50 | 0 |
| bitstamp | 22 | 4% | 254 | 1118 | 1214 | 1344 | 1648 | 92 | 10 | 3 |
| kraken | 25 | 4% | 254 | 621 | 1017 | 1070 | 1311 | 147 | 96 | 0 |
| hyperliquid | 22 | 4% | 899 | 1206 | 1512 | 1880 | 1899 | 736 | 700 | 3 |
| bybit | 25 | 0% | 157 | 479 | 964 | 1265 | 1265 | 16 | -117 | 0 |
| okx_swap | 23 | 0% | 177 | 967 | 972 | 1299 | 1366 | 45 | -26 | 2 |
| gemini | 23 | 0% | 201 | 835 | 898 | 1000 | 1684 | 82 | -20 | 2 |
| binance | 21 | 0% | 223 | 985 | 1246 | 1246 | 1398 | 125 | 0 | 4 |
| coinbase_adv | 25 | 0% | 285 | 632 | 664 | 1189 | 1358 | 164 | 66 | 0 |
| binance_futures | 25 | 0% | 297 | 525 | 1025 | 1235 | 1272 | 120 | 14 | 0 |

### 2 bp: 7 events, 7 with two or more detections (2026-09-30 18:45 UTC → 2026-09-30 18:54 UTC)

| source | detected | first | lag p50 | p90 | p95 | p99 | max | vs coinbase p50 | vs binance p50 | missed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| coinbase | 7 | 43% | 37 | 479 | 479 | 479 | 588 | 0 | -179 | 0 |
| deribit | 7 | 14% | 34 | 157 | 157 | 157 | 639 | 1 | -128 | 0 |
| gemini | 7 | 14% | 170 | 790 | 790 | 790 | 820 | 154 | -9 | 0 |
| kraken | 7 | 14% | 285 | 335 | 335 | 335 | 857 | 174 | -14 | 0 |
| hyperliquid | 5 | 14% | 846 | 913 | 913 | 913 | 1459 | 846 | 542 | 2 |
| okx | 7 | 0% | 116 | 269 | 269 | 269 | 711 | 58 | -67 | 0 |
| bybit_linear | 7 | 0% | 123 | 220 | 220 | 220 | 702 | 59 | -65 | 0 |
| binance | 6 | 0% | 125 | 463 | 463 | 463 | 767 | 125 | 0 | 1 |
| bybit | 7 | 0% | 195 | 210 | 210 | 210 | 693 | 48 | -117 | 0 |
| okx_swap | 7 | 0% | 205 | 779 | 779 | 779 | 1568 | 143 | -26 | 0 |
| coinbase_adv | 7 | 0% | 243 | 554 | 554 | 554 | 819 | 219 | 52 | 0 |
| bitstamp | 7 | 0% | 313 | 785 | 785 | 785 | 1594 | 196 | -52 | 0 |
| binance_futures | 7 | 0% | 341 | 719 | 719 | 719 | 880 | 261 | 15 | 0 |

### 5 bp: 0 events — too few to tabulate

### 10 bp: 0 events — too few to tabulate

## 3. Leader by direction, size and volatility regime

First-arrival share per source (2 bp events unless noted). Volatility terciles come from the rolling 5-minute standard deviation of consensus 1 s returns at the event; halves of the recording are shown so a leader that only led for an hour does not pass for a rule.

| regime | events | coinbase | deribit | gemini | hyperliquid | kraken | bitstamp | coinbase_adv | bybit |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all 2 bp | 7 | 43% / 37 | 14% / 34 | 14% / 170 | 14% / 846 | 14% / 285 | 0% / 313 | 0% / 243 | 0% / 195 |
| up 2 bp | 4 | 50% / 0 | 25% / 1 | 0% / 170 | 0% / 913 | 25% / 169 | 0% / 159 | 0% / 219 | 0% / 17 |
| down 2 bp | 3 | 33% / 162 | 0% / 157 | 33% / 154 | 33% / 497 | 0% / 335 | 0% / 381 | 0% / 243 | 0% / 210 |
| small (1–2 bp) | 24 | 17% / 197 | 62% / 0 | 0% / 201 | 4% / 899 | 4% / 240 | 4% / 317 | 0% / 285 | 0% / 157 |
| large (≥ 5 bp) | 0 | — | — | — | — | — | — | — | — |
| low vol | 2 | 50% / 0 | 0% / 1 | 50% / 0 | 0% / 497 | 0% / 285 | 0% / 50 | 0% / 161 | 0% / 8 |
| mid vol | 3 | 33% / 37 | 33% / 34 | 0% / 209 | 33% / 0 | 0% / 174 | 0% / 785 | 0% / 327 | 0% / 197 |
| high vol | 2 | 50% / 0 | 0% / 3 | 0% / 154 | 0% / 846 | 50% / 0 | 0% / 252 | 0% / 243 | 0% / 119 |
| first half | 2 | 50% / 0 | 0% / 1 | 50% / 0 | 0% / 497 | 0% / 285 | 0% / 50 | 0% / 161 | 0% / 8 |
| second half | 5 | 40% / 37 | 20% / 34 | 0% / 209 | 20% / 846 | 20% / 174 | 0% / 313 | 0% / 327 | 0% / 195 |
| DEV | 2 | 50% / 0 | 0% / 1 | 50% / 0 | 0% / 497 | 0% / 285 | 0% / 50 | 0% / 161 | 0% / 8 |
| TEST | 1 | 0% / 479 | 0% / 3 | 0% / 790 | 0% / 1459 | 100% / 0 | 0% / 313 | 0% / 554 | 0% / 195 |

Cells: first-arrival share / median lag behind the first source, ms.

### Rolling 30-minute windows (2 bp events): first-arrival share

| window (UTC) | events | coinbase | deribit | gemini | hyperliquid | kraken | bitstamp |
|---|---:|---:|---:|---:|---:|---:|---:|
| 18:44 | 7 | 43% | 14% | 14% | 14% | 14% | 0% |

## 4. Predictive value: a source's own shocks, and the microstructure features

**4a. A source's own 1 s shock as a signal.** For each source, the moments its OWN 1 s return (receipt time, 100 ms grid) first crosses the threshold, 5 s apart. `same sign` = the consensus moved the same way over the next 1 s; `confirmed` = the consensus 1 s return reached the threshold within 1.5 s (the shock was real); `lead` = ms before (−) the consensus crossing. DEV / TEST.

| source | thr | DEV n | same sign | confirmed | lead p50 | TEST n | same sign | confirmed | lead p50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| binance | 1 | 7 | 71% | 100% | 100 | 3 | 100% | 67% | -200 |
| binance | 2 | 2 | 100% | 50% | 250 | 1 | 100% | 100% | -100 |
| binance | 5 | 0 | — | — | — | 0 | — | — | — |
| binance_futures | 1 | 8 | 62% | 100% | 0 | 5 | 80% | 80% | -50 |
| binance_futures | 2 | 2 | 100% | 100% | 200 | 2 | 100% | 0% | — |
| binance_futures | 5 | 0 | — | — | — | 0 | — | — | — |
| bitstamp | 1 | 8 | 88% | 100% | -50 | 8 | 50% | 50% | 50 |
| bitstamp | 2 | 2 | 100% | 100% | 0 | 1 | 100% | 100% | 100 |
| bitstamp | 5 | 0 | — | — | — | 0 | — | — | — |
| bybit | 1 | 6 | 100% | 100% | -50 | 5 | 100% | 80% | -350 |
| bybit | 2 | 2 | 100% | 100% | -150 | 1 | 100% | 0% | — |
| bybit | 5 | 0 | — | — | — | 0 | — | — | — |
| bybit_linear | 1 | 15 | 93% | 60% | -150 | 6 | 83% | 67% | -250 |
| bybit_linear | 2 | 2 | 100% | 100% | -150 | 2 | 100% | 50% | 150 |
| bybit_linear | 5 | 0 | — | — | — | 0 | — | — | — |
| coinbase | 1 | 17 | 82% | 59% | -100 | 8 | 75% | 50% | 0 |
| coinbase | 2 | 2 | 100% | 100% | -200 | 2 | 100% | 50% | 100 |
| coinbase | 5 | 0 | — | — | — | 0 | — | — | — |
| coinbase_adv | 1 | 17 | 65% | 59% | 0 | 8 | 88% | 50% | -100 |
| coinbase_adv | 2 | 2 | 100% | 100% | 0 | 2 | 100% | 50% | 200 |
| coinbase_adv | 5 | 0 | — | — | — | 0 | — | — | — |
| deribit | 1 | 18 | 89% | 61% | -200 | 10 | 50% | 30% | -400 |
| deribit | 2 | 4 | 100% | 50% | -200 | 3 | 100% | 33% | -300 |
| deribit | 5 | 0 | — | — | — | 0 | — | — | — |
| gemini | 1 | 12 | 92% | 67% | -150 | 4 | 100% | 75% | 50 |
| gemini | 2 | 3 | 67% | 33% | 0 | 1 | 100% | 0% | — |
| gemini | 5 | 0 | — | — | — | 0 | — | — | — |
| hyperliquid | 1 | 8 | 50% | 75% | 450 | 6 | 67% | 67% | 850 |
| hyperliquid | 2 | 4 | 75% | 25% | 100 | 2 | 50% | 50% | 850 |
| hyperliquid | 5 | 0 | — | — | — | 0 | — | — | — |
| kraken | 1 | 11 | 91% | 82% | 0 | 3 | 100% | 67% | -300 |
| kraken | 2 | 2 | 100% | 100% | 50 | 1 | 100% | 100% | -250 |
| kraken | 5 | 0 | — | — | — | 0 | — | — | — |
| okx | 1 | 14 | 86% | 64% | -100 | 6 | 100% | 67% | -250 |
| okx | 2 | 2 | 100% | 50% | -200 | 2 | 100% | 50% | 0 |
| okx | 5 | 0 | — | — | — | 0 | — | — | — |
| okx_swap | 1 | 12 | 92% | 58% | -200 | 7 | 71% | 57% | -150 |
| okx_swap | 2 | 3 | 67% | 67% | 0 | 1 | 100% | 0% | — |
| okx_swap | 5 | 0 | — | — | — | 0 | — | — | — |

**4b. Microstructure features vs the next consensus move** (100 ms grid; Spearman rank correlation with the forward 1 s and 5 s consensus return, and the directional accuracy of the feature's sign in its top decile of |value|). Velocity = 200 ms return, acceleration = its change, trade imbalance = buy share of 1 s traded volume, OBI = (bid − ask size) / (bid + ask size) over the top 1 / 5 / 10 levels (Binance spot, OKX, Bitstamp depth streams), divergence = source mid − Binance mid, basis-adjusted, bp. DEV / TEST.

| feature | DEV n | ρ 1 s | ρ 5 s | top-decile accuracy 1 s | TEST n | ρ 1 s | ρ 5 s | top-decile accuracy 1 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| velocity binance | 2991 | +0.187 | +0.093 | 57% | 1450 | +0.257 | +0.236 | 51% |
| acceleration binance | 2989 | +0.075 | +0.038 | 56% | 1450 | +0.065 | +0.027 | 44% |
| velocity binance_futures | 2989 | +0.210 | +0.114 | 57% | 1450 | +0.313 | +0.210 | 53% |
| acceleration binance_futures | 2987 | +0.077 | +0.032 | 55% | 1450 | +0.065 | +0.025 | 48% |
| divergence binance_futures − binance | 2991 | -0.024 | -0.052 | 34% | 1450 | +0.185 | +0.233 | 35% |
| velocity bitstamp | 2997 | +0.169 | +0.106 | 56% | 1450 | +0.179 | +0.103 | 51% |
| acceleration bitstamp | 2995 | +0.029 | +0.008 | 55% | 1450 | +0.033 | +0.033 | 50% |
| divergence bitstamp − binance | 2993 | -0.021 | -0.034 | 8% | 1450 | +0.072 | +0.174 | 39% |
| velocity bybit | 2991 | +0.255 | +0.150 | 57% | 1450 | +0.312 | +0.207 | 53% |
| acceleration bybit | 2989 | +0.086 | +0.057 | 56% | 1450 | +0.041 | +0.020 | 46% |
| divergence bybit − binance | 2993 | +0.074 | +0.015 | 27% | 1450 | +0.292 | +0.407 | 35% |
| velocity bybit_linear | 2989 | +0.270 | +0.142 | 58% | 1450 | +0.273 | +0.157 | 53% |
| acceleration bybit_linear | 2987 | +0.069 | +0.034 | 42% | 1450 | +0.041 | +0.023 | 47% |
| divergence bybit_linear − binance | 2991 | +0.065 | +0.010 | 37% | 1450 | +0.237 | +0.348 | 35% |
| velocity coinbase | 2994 | +0.214 | +0.122 | 56% | 1450 | +0.246 | +0.142 | 68% |
| acceleration coinbase | 2992 | +0.065 | +0.035 | 38% | 1450 | +0.023 | +0.025 | 43% |
| divergence coinbase − binance | 2993 | +0.022 | -0.130 | 32% | 1450 | +0.269 | +0.390 | 41% |
| velocity coinbase_adv | 2993 | +0.157 | +0.082 | 46% | 1450 | +0.278 | +0.132 | 73% |
| acceleration coinbase_adv | 2991 | +0.040 | +0.011 | 36% | 1450 | +0.027 | -0.011 | 48% |
| divergence coinbase_adv − binance | 2993 | -0.004 | -0.138 | 32% | 1450 | +0.258 | +0.392 | 38% |
| velocity deribit | 2998 | +0.224 | +0.151 | 57% | 1450 | +0.272 | +0.165 | 70% |
| acceleration deribit | 2996 | +0.049 | +0.037 | 40% | 1450 | +0.022 | +0.034 | 39% |
| divergence deribit − binance | 2993 | +0.209 | +0.078 | 38% | 1450 | +0.310 | +0.339 | 50% |
| velocity gemini | 2995 | +0.139 | +0.051 | 56% | 1450 | +0.159 | +0.122 | 50% |
| acceleration gemini | 2993 | +0.063 | +0.024 | 54% | 1450 | +0.053 | +0.026 | 49% |
| divergence gemini − binance | 2993 | +0.018 | -0.012 | 32% | 1450 | +0.104 | +0.254 | 54% |
| velocity hyperliquid | 2990 | +0.100 | +0.054 | 54% | 1450 | +0.210 | +0.100 | 50% |
| acceleration hyperliquid | 2988 | -0.005 | +0.004 | 52% | 1450 | +0.003 | +0.012 | 49% |
| divergence hyperliquid − binance | 2992 | -0.187 | -0.228 | 11% | 1450 | -0.004 | +0.051 | 28% |
| velocity kraken | 2987 | +0.198 | +0.121 | 56% | 1450 | +0.256 | +0.169 | 51% |
| acceleration kraken | 2985 | +0.055 | +0.022 | 34% | 1450 | +0.030 | +0.021 | 49% |
| divergence kraken − binance | 2989 | +0.044 | +0.070 | 27% | 1450 | +0.237 | +0.384 | 50% |
| velocity okx | 2987 | +0.230 | +0.147 | 57% | 1450 | +0.258 | +0.170 | 51% |
| acceleration okx | 2985 | +0.067 | +0.040 | 55% | 1450 | -0.001 | +0.014 | 50% |
| divergence okx − binance | 2989 | +0.080 | -0.008 | 33% | 1450 | +0.210 | +0.323 | 32% |
| velocity okx_swap | 2987 | +0.179 | +0.123 | 56% | 1450 | +0.225 | +0.132 | 50% |
| acceleration okx_swap | 2985 | +0.074 | +0.037 | 55% | 1450 | +0.029 | +0.024 | 49% |
| divergence okx_swap − binance | 2989 | -0.139 | -0.227 | 25% | 1450 | +0.204 | +0.408 | 30% |
| trade imbalance coinbase | 2981 | +0.065 | +0.092 | 28% | 1444 | +0.231 | +0.153 | 37% |
| trade imbalance coinbase_adv | 2981 | -0.026 | -0.072 | 18% | 1444 | -0.211 | -0.139 | 13% |
| trade imbalance hyperliquid | 2254 | -0.010 | +0.000 | 20% | 1110 | +0.193 | +0.202 | 34% |
| trade imbalance binance | 2981 | +0.049 | +0.005 | 28% | 1431 | +0.115 | +0.095 | 31% |
| trade imbalance bybit | 1268 | +0.160 | +0.174 | 11% | 658 | +0.204 | +0.225 | 36% |
| trade imbalance okx_swap | 2494 | +0.165 | +0.094 | 23% | 1279 | +0.044 | +0.095 | 8% |
| trade imbalance bybit_linear | 2415 | +0.127 | +0.197 | 27% | 1025 | +0.238 | +0.260 | 28% |
| trade imbalance gemini | 431 | +0.070 | +0.182 | 15% | 200 | +0.156 | +0.109 | 47% |
| trade imbalance bitfinex | 667 | +0.317 | +0.395 | 43% | 312 | +0.506 | +0.415 | 78% |
| trade imbalance okx | 1528 | +0.137 | +0.122 | 13% | 673 | +0.227 | +0.237 | 9% |
| trade imbalance deribit | 938 | +0.277 | +0.317 | 37% | 557 | +0.326 | +0.330 | 40% |
| trade imbalance kraken | 1439 | +0.134 | +0.114 | 26% | 700 | +0.195 | +0.324 | 14% |
| trade imbalance bitstamp | 686 | +0.083 | +0.051 | 33% | 235 | +0.407 | +0.198 | 59% |
| OBI top1 bitstamp | 2999 | +0.225 | +0.200 | 34% | 1450 | +0.342 | +0.319 | 58% |
| OBI top5 bitstamp | 2999 | +0.245 | +0.216 | 29% | 1450 | +0.291 | +0.216 | 43% |
| OBI top10 bitstamp | 2999 | +0.190 | +0.169 | 34% | 1450 | +0.186 | +0.082 | 31% |
| OBI top1 binance | 2993 | +0.197 | +0.067 | 45% | 1450 | +0.343 | +0.369 | 48% |
| OBI top5 binance | 2993 | +0.191 | +0.059 | 48% | 1450 | +0.345 | +0.374 | 45% |
| OBI top10 binance | 2993 | +0.182 | +0.054 | 43% | 1450 | +0.343 | +0.363 | 46% |
| OBI top1 okx | 2989 | +0.308 | +0.368 | 46% | 1450 | +0.217 | +0.064 | 39% |
| OBI top5 okx | 2989 | +0.237 | +0.227 | 34% | 1450 | +0.246 | +0.111 | 40% |
| OBI top10 okx | 2989 | +0.237 | +0.227 | 34% | 1450 | +0.246 | +0.111 | 40% |

## 5. Fair-price representations: reaction speed against robustness

Each composite is built in receipt time from a subset of sources. `reaction` = ms after the FIRST individual source at consensus events (2 bp / 5 bp; the composite's own half-crossing); `false moves` = the composite's own 2 bp 1 s shocks that fewer than three individual sources confirmed within 1.5 s, per hour and as a share of its shocks; `noise` = standard deviation of its 100 ms returns, bp. DEV / TEST for the false-move share.

| composite | reaction 2 bp p50 | p90 | missed | reaction 5 bp p50 | p90 | own 2 bp shocks/h | false/h | false share DEV | TEST | noise |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| coinbase alone (live now) | 84 | 513 | 0 | — | — | 60.0 | 0.0 | 0% | 0% | 0.12 |
| median of 3 (previous live) | 220 | 513 | 0 | — | — | 36.0 | 0.0 | 0% | 0% | 0.10 |
| lead mode, 3 sources (live 09-29) | 84 | 198 | 0 | — | — | 78.0 | 0.0 | 0% | 0% | 0.36 |
| median of all spot | 220 | 413 | 0 | — | — | 42.0 | 0.0 | 0% | 0% | 0.08 |
| median of all (spot + perps) | 220 | 371 | 0 | — | — | 42.0 | 0.0 | 0% | 0% | 0.08 |
| microprice median, all | 220 | 313 | 0 | — | — | 42.0 | 0.0 | 0% | 0% | 0.08 |
| liquidity-weighted, all | 213 | 828 | 0 | — | — | 60.0 | 0.0 | 0% | 0% | 0.12 |
| lead mode, all sources | 34 | 221 | 0 | — | — | 120.0 | 0.0 | 0% | 0% | 0.47 |
| trade VWAP 1 s, all | 278 | 1034 | 1 | — | — | 48.0 | 6.0 | 0% | 100% | 0.16 |
| deribit alone (lowest delay) | 70 | 171 | 0 | — | — | 90.0 | 0.0 | 0% | 0% | 0.15 |

## 6. The venue: does an earlier BTC signal buy usable time on Polymarket?

For every consensus event the BTC 5-minute token on the event's side (UP for an up-move). `pre ask` = its best ask when the first source detected the move; `reprice` = the ask rising a tick or more within 5 s, timed from that first detection (receipt time of the venue message). For each feed variant an order decided at its detection lands 135 ms later and is judged against the book we had received by then + 31 ms: `window` = ms between the variant's detection and the reprice (negative: the token had already moved); `fillable` = the ask still within 4c of the pre ask with ≥ 5 shares when the order lands; `paid` = that ask; `markout` = the bid 20 s after landing minus paid minus the taker fee, cents per share over fillable orders. Tokens outside 0.05–0.70 (the live price band) are reported separately as `out of band`.

### 1 bp: 21 events with a venue book (8 in band); token repriced within 5 s in 17 (81%); response after the first source: p25 132, p50 169, p75 225, p90 308 ms

| feed variant | split | events | detect vs first p50 | window p50 | window p90 | window > 0 | fillable | paid − pre p50 | markout p50 | markout mean | n markout |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| first source (any) | DEV | 5 | 0 | 147 | 225 | 100% | 100% | 1.0c | -5.2c | -1.5c | 5 |
| first source (any) | ALL | 8 | 0 | 147 | 308 | 100% | 88% | 0.0c | -5.2c | -1.6c | 7 |
| coinbase alone (live now) | DEV | 5 | 261 | 46 | 61 | 60% | 60% | 2.0c | 9.4c | 3.1c | 3 |
| coinbase alone (live now) | ALL | 8 | 70 | 27 | 155 | 62% | 62% | 1.0c | 1.5c | 1.1c | 5 |
| median of 3 (previous live) | DEV | 5 | 120 | 11 | 105 | 60% | 80% | 2.0c | -7.3c | 0.5c | 4 |
| median of 3 (previous live) | ALL | 8 | 120 | 11 | 196 | 75% | 75% | 0.0c | -5.4c | -0.1c | 6 |
| lead mode, 3 sources (live 09-29) | DEV | 5 | -14 | 155 | 346 | 100% | 80% | 0.0c | -9.4c | -0.3c | 4 |
| lead mode, 3 sources (live 09-29) | ALL | 8 | 24 | 146 | 346 | 100% | 75% | 0.0c | -5.4c | -0.7c | 6 |
| lead mode, all sources | DEV | 5 | 20 | 205 | 296 | 80% | 80% | 0.0c | -5.2c | 2.5c | 4 |
| lead mode, all sources | ALL | 8 | 20 | 205 | 696 | 88% | 75% | 0.0c | -5.2c | 1.2c | 6 |
| median of all (spot + perps) | DEV | 5 | 170 | 46 | 55 | 60% | 80% | 3.0c | -8.3c | 0.3c | 4 |
| median of all (spot + perps) | ALL | 8 | 150 | 9 | 196 | 62% | 75% | 0.0c | -5.4c | -0.3c | 6 |

### 2 bp: 6 events with a venue book (3 in band); token repriced within 5 s in 4 (67%); response after the first source: p25 10, p50 141, p75 445, p90 445 ms

| feed variant | split | events | detect vs first p50 | window p50 | window p90 | window > 0 | fillable | paid − pre p50 | markout p50 | markout mean | n markout |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| first source (any) | DEV | 1 | 0 | 10 | 10 | 100% | 100% | 2.0c | 8.4c | 8.4c | 1 |
| first source (any) | ALL | 3 | 0 | 445 | 445 | 100% | 100% | 0.0c | 2.5c | 1.9c | 3 |
| coinbase alone (live now) | DEV | 1 | 171 | -161 | -161 | 0% | 100% | 2.0c | 8.4c | 8.4c | 1 |
| coinbase alone (live now) | ALL | 3 | 171 | 129 | 129 | 67% | 100% | 1.0c | 1.5c | 1.5c | 3 |
| median of 3 (previous live) | DEV | 1 | 221 | -211 | -211 | 0% | 0% | —c | —c | —c | 0 |
| median of 3 (previous live) | ALL | 3 | 221 | 29 | 29 | 67% | 67% | 0.0c | -5.4c | -2.0c | 2 |
| lead mode, 3 sources (live 09-29) | DEV | 1 | 171 | -161 | -161 | 0% | 100% | 2.0c | 8.4c | 8.4c | 1 |
| lead mode, 3 sources (live 09-29) | ALL | 3 | 198 | 129 | 129 | 67% | 100% | 1.0c | 1.5c | 1.5c | 3 |
| lead mode, all sources | DEV | 1 | 221 | -211 | -211 | 0% | 0% | —c | —c | —c | 0 |
| lead mode, all sources | ALL | 3 | 221 | 29 | 29 | 67% | 67% | 0.0c | -5.4c | -2.0c | 2 |
| median of all (spot + perps) | DEV | 1 | 371 | -361 | -361 | 0% | 0% | —c | —c | —c | 0 |
| median of all (spot + perps) | ALL | 3 | 371 | -71 | -71 | 33% | 67% | 0.0c | -5.4c | -3.5c | 2 |

### 5 bp: 0 events with a venue book — too few

### 10 bp: 0 events with a venue book — too few

