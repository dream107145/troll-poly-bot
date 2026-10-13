# Polymarket book direction signal vs crypto spot (BTC 5-minute markets)

Windows per split: train 995, validation 398, test 299, forward 299, forward2 55. forward2 = the windows archived after the paired-exit study's cache (10-01 21:25 UTC onward), never used by any study.

## 1. The book model (ridge, fitted on TRAIN only): next-1 s UP mid move in cents

| feature | weight per sd |
|---|---:|
| vel1 | +0.127 |
| acons1 | +0.054 |
| bcons1 | +0.072 |
| newliq1 | +0.155 |
| vel2 | -0.019 |
| acons2 | +0.024 |
| bcons2 | +0.046 |
| newliq2 | +0.054 |
| vel5 | +0.010 |
| acons5 | +0.035 |
| bcons5 | +0.043 |
| newliq5 | +0.040 |
| imb | +0.003 |
| micro | +0.474 |
| spr | -0.000 |

| split | samples | corr(book, next 1 s) | corr(crypto z1, next 1 s) | book sign hit, strongest 10 % | crypto sign hit, strongest 10 % |
|---|---:|---:|---:|---:|---:|
| train | 1292761 | +0.282 | +0.101 | 85% (n 98446) | 65% (n 64327) |
| validation | 542175 | +0.284 | +0.124 | 81% (n 59936) | 63% (n 34421) |
| test | 410125 | +0.314 | +0.129 | 84% (n 46254) | 63% (n 32374) |
| forward | 422699 | +0.288 | +0.133 | 84% (n 46639) | 67% (n 27888) |
| forward2 | 78113 | +0.278 | +0.103 | 85% (n 6465) | 63% (n 3918) |

Strongest-10 % thresholds from TRAIN: |book| >= 1.109 c, |z1| >= 1.21.

## 2. Who fires first on a real token move

A move = the UP-equivalent mid changes >= 2c within the next 1 s from a sample (the first such sample starts it; moves >= 3 s apart). For each, the first trigger in the move's direction from 3 s before the start to 1 s after it. Crypto = |1 s spot z| >= 2 (the bot today). Book = |book model| >= the threshold.

**train**: 29553 moves

| trigger | found | median ms vs start | before the start | book earlier than crypto (both found) |
|---|---:|---:|---:|---:|
| crypto | 16% | -1280 | 63% |  |
| book 0.5 | 97% | -2343 | 87% | 72% earlier, 17% same sample, median lead +676 ms (n 4580) |
| book 0.75 | 93% | -1939 | 77% | 67% earlier, 19% same sample, median lead +445 ms (n 4478) |
| book 1.0 | 85% | -1331 | 65% | 60% earlier, 22% same sample, median lead +226 ms (n 4313) |

**validation**: 13287 moves

| trigger | found | median ms vs start | before the start | book earlier than crypto (both found) |
|---|---:|---:|---:|---:|
| crypto | 17% | -1316 | 66% |  |
| book 0.5 | 99% | -2527 | 89% | 72% earlier, 17% same sample, median lead +661 ms (n 2310) |
| book 0.75 | 97% | -2297 | 83% | 69% earlier, 17% same sample, median lead +439 ms (n 2279) |
| book 1.0 | 92% | -1902 | 75% | 64% earlier, 19% same sample, median lead +350 ms (n 2230) |

**test**: 9719 moves

| trigger | found | median ms vs start | before the start | book earlier than crypto (both found) |
|---|---:|---:|---:|---:|
| crypto | 25% | -1301 | 67% |  |
| book 0.5 | 99% | -2510 | 88% | 72% earlier, 13% same sample, median lead +830 ms (n 2424) |
| book 0.75 | 97% | -2188 | 81% | 67% earlier, 15% same sample, median lead +441 ms (n 2385) |
| book 1.0 | 94% | -1734 | 73% | 62% earlier, 16% same sample, median lead +414 ms (n 2329) |

**forward**: 9856 moves

| trigger | found | median ms vs start | before the start | book earlier than crypto (both found) |
|---|---:|---:|---:|---:|
| crypto | 19% | -1348 | 64% |  |
| book 0.5 | 99% | -2443 | 89% | 73% earlier, 15% same sample, median lead +810 ms (n 1890) |
| book 0.75 | 97% | -2229 | 83% | 68% earlier, 16% same sample, median lead +607 ms (n 1851) |
| book 1.0 | 93% | -1743 | 73% | 62% earlier, 18% same sample, median lead +215 ms (n 1812) |

**forward2**: 1630 moves

| trigger | found | median ms vs start | before the start | book earlier than crypto (both found) |
|---|---:|---:|---:|---:|
| crypto | 18% | -1623 | 64% |  |
| book 0.5 | 98% | -2308 | 87% | 74% earlier, 15% same sample, median lead +614 ms (n 291) |
| book 0.75 | 95% | -2026 | 79% | 69% earlier, 17% same sample, median lead +408 ms (n 281) |
| book 1.0 | 89% | -1426 | 68% | 64% earlier, 19% same sample, median lead +214 ms (n 273) |

## 3. Every trigger as a taker trade

Bought at the calibrated paper fill (lands 250 ms later, must survive 1 s, worse of two books, limit ask + 0.03, band 0.15-0.85, 60-290 s left), fee 0.07 p(1-p). Markout = the token's mid h later minus the fill price minus the fee (cents per share). Rate = triggers per hour of markets. The fair-value gate is NOT applied here (that is part 2).

**train** (995 markets)

| trigger | per hour | fill rate | +1 s | +5 s | +30 s | settlement | settlement win rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| crypto 2 | 106 | 59% | -2.78 | -2.20 | -2.07 | -1.41 | 56% |
| crypto 3 | 56 | 51% | -2.76 | -1.71 | -2.22 | -1.70 | 56% |
| book 0.5 | 1495 | 86% | -2.42 | -2.16 | -2.06 | -2.27 | 51% |
| book 0.75 | 1150 | 83% | -2.43 | -2.13 | -2.02 | -2.21 | 52% |
| book 1 | 808 | 78% | -2.47 | -2.16 | -2.00 | -2.03 | 52% |
| book 1.5 | 272 | 60% | -2.77 | -2.59 | -2.68 | -1.70 | 52% |

**validation** (398 markets)

| trigger | per hour | fill rate | +1 s | +5 s | +30 s | settlement | settlement win rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| crypto 2 | 121 | 57% | -3.06 | -2.98 | -2.30 | -1.96 | 56% |
| crypto 3 | 57 | 49% | -3.11 | -3.09 | -2.41 | -2.91 | 55% |
| book 0.5 | 1630 | 82% | -2.62 | -2.51 | -2.68 | -2.52 | 51% |
| book 0.75 | 1378 | 80% | -2.64 | -2.51 | -2.64 | -2.42 | 51% |
| book 1 | 1089 | 76% | -2.68 | -2.54 | -2.74 | -2.55 | 51% |
| book 1.5 | 419 | 62% | -3.00 | -2.97 | -3.32 | -2.82 | 50% |

**test** (299 markets)

| trigger | per hour | fill rate | +1 s | +5 s | +30 s | settlement | settlement win rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| crypto 2 | 202 | 68% | -2.86 | -2.98 | -2.74 | -2.67 | 52% |
| crypto 3 | 66 | 54% | -2.87 | -3.04 | -2.76 | -4.09 | 52% |
| book 0.5 | 1604 | 82% | -2.54 | -2.43 | -2.28 | -2.10 | 52% |
| book 0.75 | 1353 | 80% | -2.55 | -2.42 | -2.28 | -2.11 | 53% |
| book 1 | 1075 | 76% | -2.57 | -2.47 | -2.52 | -2.64 | 52% |
| book 1.5 | 423 | 62% | -2.82 | -2.85 | -2.79 | -2.53 | 51% |

**forward** (299 markets)

| trigger | per hour | fill rate | +1 s | +5 s | +30 s | settlement | settlement win rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| crypto 2 | 152 | 62% | -2.83 | -2.80 | -2.77 | -1.84 | 54% |
| crypto 3 | 65 | 53% | -2.94 | -2.76 | -2.45 | -1.62 | 55% |
| book 0.5 | 1774 | 83% | -2.60 | -2.46 | -2.42 | -2.07 | 52% |
| book 0.75 | 1459 | 80% | -2.61 | -2.43 | -2.28 | -1.68 | 53% |
| book 1 | 1135 | 77% | -2.63 | -2.45 | -2.27 | -1.97 | 52% |
| book 1.5 | 445 | 64% | -2.86 | -2.69 | -2.58 | -2.32 | 51% |

**forward2** (55 markets)

| trigger | per hour | fill rate | +1 s | +5 s | +30 s | settlement | settlement win rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| crypto 2 | 118 | 57% | -2.88 | -2.69 | -1.15 | -0.99 | 59% |
| crypto 3 | 64 | 53% | -2.93 | -3.24 | -0.65 | +0.97 | 62% |
| book 0.5 | 1604 | 83% | -2.54 | -2.25 | -1.97 | -1.94 | 52% |
| book 0.75 | 1258 | 80% | -2.55 | -2.24 | -2.02 | -2.32 | 52% |
| book 1 | 904 | 75% | -2.62 | -2.39 | -1.87 | -1.86 | 52% |
| book 1.5 | 307 | 57% | -3.10 | -2.66 | -1.26 | -2.92 | 51% |

