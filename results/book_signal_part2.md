# The triangle paired-exit strategy with a Polymarket-book trigger (BTC)

Execution: calibrated paper fills (lands 250 ms later, survives 1 s, worse of two books, fee 0.07 p(1-p)); exits as live (maker take-profit max(fill + 0.02, fair + 0.03), sell at 30 s left unless bid >= 0.95, else settle). Book model and fair model fitted on TRAIN only. forward2 = 10-01 21:25 UTC onward, never seen.

## A. Today's settings, only the trigger changed

### TRAIN

| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| today: crypto z>=2 (live settings) | 336 | 82% | 52% | +134.58 | +0.401 | +0.1353 | +4.02 | -4.29 |
| book >= 1 c (agreement off) | 504 | 75% | 44% | +73.16 | +0.145 | +0.0735 | +1.58 | -4.16 |
| book >= 1.5 c (agreement off) | 367 | 76% | 45% | +54.81 | +0.149 | +0.0551 | +1.43 | -4.29 |
| book >= 2 c (agreement off) | 140 | 81% | 51% | +49.40 | +0.353 | +0.0496 | +2.30 | -4.01 |
| book >= 3 c (agreement off) | 40 | 90% | 62% | +27.94 | +0.699 | +0.0281 | +2.98 | -3.45 |
| both: crypto z>=2 + book >= 0 c in the last 1 s | 335 | 83% | 52% | +136.20 | +0.407 | +0.1369 | +4.08 | -4.29 |
| both: crypto z>=2 + book >= 0.5 c in the last 1 s | 330 | 83% | 52% | +132.48 | +0.401 | +0.1331 | +4.03 | -4.29 |
| both: crypto z>=2 + book >= 1 c in the last 1 s | 322 | 82% | 51% | +119.64 | +0.372 | +0.1202 | +3.66 | -4.29 |
| either: crypto z>=2 or book >= 1.5 c | 364 | 78% | 49% | +80.56 | +0.221 | +0.0810 | +2.17 | -4.25 |
| either: crypto z>=2 or book >= 2 c | 342 | 82% | 52% | +129.44 | +0.378 | +0.1301 | +3.82 | -4.29 |
| either: crypto z>=2 or book >= 3 c | 334 | 82% | 52% | +132.76 | +0.397 | +0.1334 | +3.99 | -4.29 |

### VALIDATION

| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| today: crypto z>=2 (live settings) | 136 | 78% | 47% | +15.30 | +0.113 | +0.0385 | +0.65 | -4.29 |
| book >= 1 c (agreement off) | 223 | 75% | 44% | +28.52 | +0.128 | +0.0717 | +0.93 | -4.29 |
| book >= 1.5 c (agreement off) | 180 | 80% | 47% | +58.90 | +0.327 | +0.1480 | +2.28 | -4.20 |
| book >= 2 c (agreement off) | 64 | 78% | 45% | +8.40 | +0.131 | +0.0211 | +0.53 | -4.10 |
| book >= 3 c (agreement off) | 19 | 74% | 37% | +4.65 | +0.245 | +0.0117 | +0.47 | -3.82 |
| both: crypto z>=2 + book >= 0 c in the last 1 s | 135 | 78% | 47% | +14.75 | +0.109 | +0.0370 | +0.62 | -4.29 |
| both: crypto z>=2 + book >= 0.5 c in the last 1 s | 135 | 78% | 47% | +13.18 | +0.098 | +0.0331 | +0.56 | -4.29 |
| both: crypto z>=2 + book >= 1 c in the last 1 s | 133 | 77% | 46% | +9.54 | +0.072 | +0.0240 | +0.41 | -4.29 |
| either: crypto z>=2 or book >= 1.5 c | 157 | 83% | 54% | +59.95 | +0.382 | +0.1506 | +2.59 | -4.20 |
| either: crypto z>=2 or book >= 2 c | 138 | 78% | 50% | +16.68 | +0.121 | +0.0419 | +0.71 | -4.29 |
| either: crypto z>=2 or book >= 3 c | 137 | 79% | 49% | +22.11 | +0.161 | +0.0556 | +0.94 | -4.29 |

### TEST + FORWARD

| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| today: crypto z>=2 (live settings) | 181 | 80% | 52% | +43.41 | +0.240 | +0.0726 | +1.79 | -4.25 |
| book >= 1 c (agreement off) | 294 | 73% | 44% | +21.91 | +0.075 | +0.0366 | +0.64 | -4.20 |
| book >= 1.5 c (agreement off) | 248 | 75% | 45% | +28.93 | +0.117 | +0.0484 | +0.94 | -4.20 |
| book >= 2 c (agreement off) | 72 | 79% | 47% | +22.07 | +0.306 | +0.0369 | +1.40 | -4.25 |
| book >= 3 c (agreement off) | 19 | 79% | 42% | +3.19 | +0.168 | +0.0053 | +0.38 | -3.82 |
| both: crypto z>=2 + book >= 0 c in the last 1 s | 181 | 79% | 52% | +40.26 | +0.222 | +0.0673 | +1.65 | -4.25 |
| both: crypto z>=2 + book >= 0.5 c in the last 1 s | 181 | 79% | 52% | +40.63 | +0.224 | +0.0679 | +1.66 | -4.25 |
| both: crypto z>=2 + book >= 1 c in the last 1 s | 179 | 79% | 51% | +38.19 | +0.213 | +0.0639 | +1.57 | -4.25 |
| either: crypto z>=2 or book >= 1.5 c | 203 | 79% | 50% | +50.54 | +0.249 | +0.0845 | +1.92 | -4.20 |
| either: crypto z>=2 or book >= 2 c | 174 | 80% | 51% | +48.58 | +0.279 | +0.0812 | +2.06 | -4.25 |
| either: crypto z>=2 or book >= 3 c | 176 | 80% | 53% | +41.50 | +0.236 | +0.0694 | +1.74 | -4.25 |

### FORWARD2 (newest 4.6 h, untouched)

| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| today: crypto z>=2 (live settings) | 32 | 84% | 53% | +12.75 | +0.399 | +0.2319 | +1.27 | -3.82 |
| book >= 1 c (agreement off) | 39 | 77% | 54% | +8.79 | +0.225 | +0.1599 | +0.72 | -3.82 |
| book >= 1.5 c (agreement off) | 27 | 81% | 56% | +9.28 | +0.344 | +0.1688 | +0.95 | -3.82 |
| book >= 2 c (agreement off) | 11 | 73% | 55% | -1.34 | -0.122 | -0.0244 | -0.19 | -3.77 |
| book >= 3 c (agreement off) | 6 | 33% | 17% | -10.47 | -1.745 | -0.1904 | -1.47 | -3.77 |
| both: crypto z>=2 + book >= 0 c in the last 1 s | 32 | 84% | 53% | +12.75 | +0.399 | +0.2319 | +1.27 | -3.82 |
| both: crypto z>=2 + book >= 0.5 c in the last 1 s | 32 | 84% | 53% | +12.75 | +0.399 | +0.2319 | +1.27 | -3.82 |
| both: crypto z>=2 + book >= 1 c in the last 1 s | 31 | 84% | 52% | +12.19 | +0.393 | +0.2217 | +1.21 | -3.82 |
| either: crypto z>=2 or book >= 1.5 c | 31 | 84% | 58% | +10.05 | +0.324 | +0.1827 | +1.04 | -3.82 |
| either: crypto z>=2 or book >= 2 c | 31 | 84% | 55% | +11.03 | +0.356 | +0.2005 | +1.11 | -3.82 |
| either: crypto z>=2 or book >= 3 c | 32 | 84% | 53% | +12.75 | +0.399 | +0.2319 | +1.27 | -3.82 |

## B. Each mode with its own parameters (TRAIN grid -> VALIDATION pick -> untouched OOS)

- **crypto** (72 rules): chosen crypto z>=2, edge 0.02, agree 0.34, band 0.3-0.7, offset 0.02
- **book** (60 rules): chosen book>=0.75c, edge 0.02, agree -1, band 0.15-0.85, offset 0.03
- **both** (216 rules): chosen crypto z>=2 + book>=0c, edge 0.02, agree 0.34, band 0.15-0.85, offset 0.02
- **either** (72 rules): chosen crypto z>=2 or book>=3c, edge 0.02, agree -1, band 0.3-0.7, offset 0.02

### TRAIN (in-sample)

| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| crypto: crypto z>=2, edge 0.02, agree 0.34, band 0.3-0.7, offset 0.02 | 135 | 79% | 47% | +82.77 | +0.613 | +0.0832 | +3.44 | -3.67 |
| book: book>=0.75c, edge 0.02, agree -1, band 0.15-0.85, offset 0.03 | 526 | 76% | 45% | +99.15 | +0.188 | +0.0996 | +2.11 | -4.25 |
| both: crypto z>=2 + book>=0c, edge 0.02, agree 0.34, band 0.15-0.85, offset 0.02 | 273 | 83% | 52% | +118.64 | +0.435 | +0.1192 | +3.87 | -4.34 |
| either: crypto z>=2 or book>=3c, edge 0.02, agree -1, band 0.3-0.7, offset 0.02 | 159 | 77% | 45% | +83.69 | +0.526 | +0.0841 | +3.16 | -3.67 |

### VALIDATION (selection)

| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| crypto: crypto z>=2, edge 0.02, agree 0.34, band 0.3-0.7, offset 0.02 | 46 | 83% | 52% | +27.61 | +0.600 | +0.0694 | +2.08 | -3.52 |
| book: book>=0.75c, edge 0.02, agree -1, band 0.15-0.85, offset 0.03 | 232 | 77% | 45% | +50.42 | +0.217 | +0.1267 | +1.63 | -4.29 |
| both: crypto z>=2 + book>=0c, edge 0.02, agree 0.34, band 0.15-0.85, offset 0.02 | 100 | 80% | 49% | +21.02 | +0.210 | +0.0528 | +1.07 | -4.29 |
| either: crypto z>=2 or book>=3c, edge 0.02, agree -1, band 0.3-0.7, offset 0.02 | 53 | 81% | 53% | +29.70 | +0.560 | +0.0746 | +2.03 | -3.62 |

### TEST + FORWARD (untouched)

| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| crypto: crypto z>=2, edge 0.02, agree 0.34, band 0.3-0.7, offset 0.02 | 66 | 67% | 42% | -4.43 | -0.067 | -0.0074 | -0.25 | -3.62 |
| book: book>=0.75c, edge 0.02, agree -1, band 0.15-0.85, offset 0.03 | 299 | 74% | 44% | +37.67 | +0.126 | +0.0630 | +1.10 | -4.20 |
| both: crypto z>=2 + book>=0c, edge 0.02, agree 0.34, band 0.15-0.85, offset 0.02 | 146 | 77% | 52% | +23.56 | +0.161 | +0.0394 | +1.06 | -4.25 |
| either: crypto z>=2 or book>=3c, edge 0.02, agree -1, band 0.3-0.7, offset 0.02 | 87 | 66% | 40% | -3.07 | -0.035 | -0.0051 | -0.15 | -3.62 |

### FORWARD2 (untouched, newest)

| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| crypto: crypto z>=2, edge 0.02, agree 0.34, band 0.3-0.7, offset 0.02 | 6 | 67% | 17% | +0.67 | +0.112 | +0.0123 | +0.14 | -2.89 |
| book: book>=0.75c, edge 0.02, agree -1, band 0.15-0.85, offset 0.03 | 39 | 74% | 51% | +5.55 | +0.142 | +0.1010 | +0.43 | -3.96 |
| both: crypto z>=2 + book>=0c, edge 0.02, agree 0.34, band 0.15-0.85, offset 0.02 | 23 | 78% | 52% | +1.10 | +0.048 | +0.0201 | +0.12 | -3.82 |
| either: crypto z>=2 or book>=3c, edge 0.02, agree -1, band 0.3-0.7, offset 0.02 | 7 | 71% | 29% | +0.69 | +0.098 | +0.0125 | +0.14 | -2.89 |

### ALL OUT-OF-SAMPLE (test + forward + forward2)

| rule | fills | win rate | take-profit exits | net $ | $ per fill | $ per market | t | worst market $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| crypto: crypto z>=2, edge 0.02, agree 0.34, band 0.3-0.7, offset 0.02 | 72 | 67% | 40% | -3.75 | -0.052 | -0.0057 | -0.21 | -3.62 |
| book: book>=0.75c, edge 0.02, agree -1, band 0.15-0.85, offset 0.03 | 338 | 74% | 45% | +43.23 | +0.128 | +0.0662 | +1.18 | -4.20 |
| both: crypto z>=2 + book>=0c, edge 0.02, agree 0.34, band 0.15-0.85, offset 0.02 | 169 | 77% | 52% | +24.66 | +0.146 | +0.0378 | +1.02 | -4.25 |
| either: crypto z>=2 or book>=3c, edge 0.02, agree -1, band 0.3-0.7, offset 0.02 | 94 | 66% | 39% | -2.38 | -0.025 | -0.0036 | -0.12 | -3.62 |

(180 s)
