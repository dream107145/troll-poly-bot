# FASTCLIP backtest: the x-moneyforwhiskas pattern at our speed

Markets (BTC, with a venue outcome): train 193, validation 63, test 128, forward 1,081.

## 1. The grid (BTC, one latency draw): net $ per period

108 configurations. Chosen before test / forward were looked at: the highest train + validation net with >= 30 fills in each (`data/replay/fastclip_locked.json`).

| config (top 15 and bottom 5 by train+validation) | train | validation | test | forward |
|---|---:|---:|---:|---:|
| comp 500ms k1.5 e0.00 +0.00 **chosen** | +352 (349) | +39 (105) | -129 (212) | +204 (1581) |
| lead 1000ms k3 e0.00 +0.01  | +320 (487) | +35 (166) | -61 (299) | +63 (1904) |
| comp 250ms k2 e0.00 +0.00  | +313 (288) | +30 (85) | -21 (180) | +114 (1406) |
| lead 1000ms k3 e0.02 +0.01  | +291 (324) | +46 (120) | -24 (176) | +120 (1242) |
| comp 500ms k1.5 e0.01 +0.00  | +272 (235) | +59 (74) | -31 (117) | +283 (1051) |
| comp 500ms k1.5 e0.00 +0.01  | +289 (472) | +39 (146) | -89 (299) | -15 (2089) |
| comp 1000ms k3 e0.00 +0.01  | +260 (157) | +61 (54) | +17 (78) | +293 (507) |
| comp 1000ms k1.5 e0.00 +0.00  | +314 (409) | -0 (114) | -83 (235) | +134 (1526) |
| lead 500ms k3 e0.00 +0.00  | +310 (452) | +3 (149) | +54 (267) | -101 (1926) |
| lead 1000ms k3 e0.01 +0.01  | +285 (408) | +22 (142) | +17 (233) | +119 (1561) |
| comp 250ms k3 e0.00 +0.00  | +310 (179) | -4 (54) | -11 (86) | +162 (724) |
| comp 500ms k1.5 e0.01 +0.01  | +272 (313) | +33 (94) | -45 (159) | +285 (1374) |
| comp 250ms k2 e0.00 +0.01  | +302 (406) | -3 (120) | -56 (266) | -40 (1886) |
| comp 250ms k2 e0.01 +0.00  | +261 (191) | +35 (61) | +15 (103) | +131 (890) |
| comp 500ms k2 e0.00 +0.00  | +267 (242) | +23 (67) | -62 (132) | +287 (1059) |
| lead 250ms k1.5 e0.00 +0.01  | -2 (1168) | -105 (348) | +75 (803) | -886 (5445) |
| lead 500ms k1.5 e0.00 +0.01  | +7 (1119) | -118 (351) | -52 (771) | -755 (5200) |
| lead 250ms k2 e0.00 +0.01  | +5 (1077) | -124 (329) | +73 (747) | -1030 (4942) |
| lead 250ms k1.5 e0.01 +0.01  | +70 (924) | -197 (266) | -149 (596) | -1099 (4217) |
| lead 1000ms k1.5 e0.01 +0.01  | -71 (911) | -93 (282) | -45 (601) | -302 (4230) |

Configurations with a positive net: train 102/108, validation 50/108, test 46/108, forward 67/108.

Positive on BOTH test and forward: 35/108.

## 2. The chosen rule: {'source': 'comp', 'h': 500, 'k': 1.5, 'edge': 0.0, 'offset': 0.0}

| period | markets | traded | orders | fills | fill rate | orders/mkt | volume | fees | net $ | net / volume | markets won | both sides | Up+Down avg px | max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| train | 193 | 147 | 1,491 | 353 | 24% | 10.1 | $2,095 | $62 | **+344.50** | +16.44% | 58% | 45% | 1.010 | $38 |
| validation | 63 | 42 | 447 | 104 | 23% | 10.6 | $651 | $18 | **+40.42** | +6.21% | 53% | 49% | 1.132 | $55 |
| test | 128 | 93 | 963 | 211 | 22% | 10.4 | $1,272 | $35 | **-114.35** | -8.99% | 44% | 49% | 1.065 | $126 |
| forward | 1,081 | 696 | 6,465 | 1,579 | 24% | 9.3 | $9,380 | $263 | **+176.89** | +1.89% | 53% | 41% | 1.026 | $168 |

Averages over 3 latency draws. Up+Down avg px: the median, over markets where both sides were bought, of the average Up price plus the average Down price (the trader: 0.986).

**test: by entry price** (net $ per share, pooled draws)

| price | fills | win rate | avg price | net/share |
|---|---:|---:|---:|---:|
| 0.10–0.20 | 49 | 0.122 | 0.141 | -2.74c |
| 0.20–0.30 | 64 | 0.141 | 0.246 | -11.82c |
| 0.30–0.40 | 58 | 0.241 | 0.347 | -11.43c |
| 0.40–0.50 | 62 | 0.403 | 0.437 | -5.05c |
| 0.50–0.60 | 84 | 0.512 | 0.541 | -5.70c |
| 0.60–0.70 | 98 | 0.694 | 0.639 | +5.30c |
| 0.70–0.80 | 92 | 0.739 | 0.745 | -2.16c |
| 0.80–0.90 | 115 | 0.826 | 0.840 | -2.67c |

**test: by time left**

| time left | fills | net/share |
|---|---:|---:|
| 240–292 s | 132 | -0.71c |
| 180–240 s | 255 | -4.86c |
| 120–180 s | 158 | -6.70c |
| 60–120 s | 88 | -5.11c |

**forward: by entry price** (net $ per share, pooled draws)

| price | fills | win rate | avg price | net/share |
|---|---:|---:|---:|---:|
| 0.10–0.20 | 472 | 0.136 | 0.142 | -1.41c |
| 0.20–0.30 | 409 | 0.242 | 0.249 | -2.05c |
| 0.30–0.40 | 519 | 0.355 | 0.345 | -1.00c |
| 0.40–0.50 | 489 | 0.513 | 0.447 | +5.23c |
| 0.50–0.60 | 602 | 0.598 | 0.544 | +3.35c |
| 0.60–0.70 | 624 | 0.657 | 0.644 | -0.37c |
| 0.70–0.80 | 654 | 0.768 | 0.746 | +0.78c |
| 0.80–0.90 | 801 | 0.895 | 0.844 | +3.95c |

**forward: by time left**

| time left | fills | net/share |
|---|---:|---:|
| 240–292 s | 1166 | +1.22c |
| 180–240 s | 1757 | +2.73c |
| 120–180 s | 1204 | -1.40c |
| 60–120 s | 611 | -0.17c |

## 3. The same rule with zero latency (a trader ~1 s ahead of our feed)

| period | markets | traded | orders | fills | fill rate | orders/mkt | volume | fees | net $ | net / volume | markets won | both sides | Up+Down avg px | max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| train | 193 | 189 | 1,067 | 1,066 | 100% | 5.6 | $6,229 | $190 | **+903.11** | +14.50% | 60% | 89% | 0.989 | $81 |
| validation | 63 | 60 | 307 | 307 | 100% | 5.1 | $1,824 | $55 | **+107.87** | +5.91% | 58% | 83% | 0.998 | $97 |
| test | 128 | 127 | 670 | 670 | 100% | 5.3 | $3,941 | $114 | **+258.77** | +6.57% | 58% | 80% | 1.006 | $96 |
| forward | 1,081 | 1,024 | 4,866 | 4,865 | 100% | 4.8 | $28,205 | $812 | **+2,893.51** | +10.26% | 60% | 72% | 0.990 | $113 |

## 4. No trigger: random clips at the same size and budget (no value gate)

| period | markets | traded | orders | fills | fill rate | orders/mkt | volume | fees | net $ | net / volume | markets won | both sides | Up+Down avg px | max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| train | 193 | 192 | 1,570 | 1,218 | 78% | 8.2 | $6,942 | $228 | **-362.14** | -5.22% | 39% | 94% | 0.986 | $453 |
| validation | 63 | 63 | 501 | 389 | 78% | 8.0 | $2,213 | $73 | **-226.17** | -10.22% | 32% | 95% | 0.991 | $268 |
| test | 128 | 128 | 1,099 | 818 | 74% | 8.6 | $4,524 | $147 | **-189.03** | -4.18% | 39% | 93% | 0.978 | $281 |
| forward | 1,081 | 1,078 | 9,005 | 6,935 | 77% | 8.4 | $38,654 | $1,265 | **-3,411.35** | -8.83% | 35% | 95% | 0.990 | $3,434 |

## 5. The chosen rule on every asset (the trader trades BTC only)

| period | markets | traded | orders | fills | fill rate | orders/mkt | volume | fees | net $ | net / volume | markets won | both sides | Up+Down avg px | max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| train | 1,408 | 1,144 | 8,891 | 3,600 | 40% | 7.8 | $20,308 | $594 | **+190.76** | +0.94% | 48% | 58% | 1.010 | $387 |
| validation | 564 | 442 | 3,261 | 1,406 | 43% | 7.4 | $8,207 | $233 | **-422.90** | -5.15% | 48% | 58% | 1.020 | $529 |
| test | 931 | 765 | 5,333 | 2,291 | 43% | 7.0 | $12,828 | $359 | **-65.83** | -0.51% | 47% | 55% | 1.018 | $350 |
| forward | 8,102 | 6,354 | 45,462 | 20,228 | 44% | 7.2 | $113,721 | $3,190 | **-1,868.21** | -1.64% | 47% | 58% | 1.022 | $2,048 |

| asset | test net (of volume) | forward net (of volume) |
|---|---:|---:|
| BTC | -107 (-8.6%) | +200 (+2.1%) |
| ETH | -78 (-3.4%) | -580 (-2.7%) |
| SOL | +77 (+3.4%) | -456 (-2.7%) |
| XRP | +68 (+2.9%) | -316 (-1.5%) |
| DOGE | -15 (-0.9%) | -48 (-0.3%) |
| BNB | +27 (+1.5%) | -684 (-3.7%) |
| HYPE | -39 (-3.4%) | +15 (+0.2%) |

