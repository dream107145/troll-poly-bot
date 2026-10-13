# CHEAPSIDE backtest: buying the cheap token and holding to settlement

Archive windows with a venue outcome: train 1,408, validation 564, test 931, forward 8,102. Candidates are book updates with the cheaper ask in $0.05–0.35. Model fitted on train, Platt-recalibrated on validation; test and forward untouched.

## 1. Does the model know more than the price? (all candidates, every period)

Brier / log loss of the model's P(cheap side wins) against the market's own price (the ask is the market's probability). Lower is better; a model that cannot beat the ask has no edge to sell.

| period | candidates | actual win rate | mean ask | mean model p | Brier ask | Brier model | logloss ask | logloss model |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| train | 584,891 | 0.178 | 0.196 | 0.164 | 0.1374 | 0.1363 | 0.4359 | 0.4329 |
| validation | 202,795 | 0.176 | 0.201 | 0.176 | 0.1358 | 0.1341 | 0.4322 | 0.4262 |
| test | 381,134 | 0.156 | 0.197 | 0.167 | 0.1245 | 0.1229 | 0.4046 | 0.3981 |
| forward | 3,251,452 | 0.163 | 0.197 | 0.177 | 0.1298 | 0.1297 | 0.4183 | 0.4167 |

## 2. Calibration by price bucket (all candidates)

**train**

| ask bucket | n | mean ask | mean model p | actual win rate | model − actual |
|---|---:|---:|---:|---:|---:|
| $0.05–0.10 | 112694 | 0.069 | 0.040 | 0.048 | -0.8c |
| $0.10–0.15 | 91706 | 0.120 | 0.082 | 0.100 | -1.8c |
| $0.15–0.20 | 88232 | 0.170 | 0.129 | 0.143 | -1.4c |
| $0.20–0.25 | 85708 | 0.220 | 0.181 | 0.200 | -1.9c |
| $0.25–0.30 | 91115 | 0.270 | 0.238 | 0.256 | -1.7c |
| $0.30–0.35 | 95660 | 0.320 | 0.299 | 0.310 | -1.1c |

**validation**

| ask bucket | n | mean ask | mean model p | actual win rate | model − actual |
|---|---:|---:|---:|---:|---:|
| $0.05–0.10 | 37260 | 0.069 | 0.042 | 0.049 | -0.7c |
| $0.10–0.15 | 29537 | 0.119 | 0.085 | 0.082 | +0.4c |
| $0.15–0.20 | 29280 | 0.170 | 0.135 | 0.122 | +1.3c |
| $0.20–0.25 | 30452 | 0.220 | 0.191 | 0.190 | +0.1c |
| $0.25–0.30 | 32433 | 0.270 | 0.249 | 0.246 | +0.2c |
| $0.30–0.35 | 36266 | 0.320 | 0.309 | 0.321 | -1.1c |

**test**

| ask bucket | n | mean ask | mean model p | actual win rate | model − actual |
|---|---:|---:|---:|---:|---:|
| $0.05–0.10 | 69643 | 0.069 | 0.041 | 0.032 | +0.9c |
| $0.10–0.15 | 60058 | 0.120 | 0.084 | 0.078 | +0.6c |
| $0.15–0.20 | 59629 | 0.170 | 0.131 | 0.116 | +1.5c |
| $0.20–0.25 | 57842 | 0.220 | 0.184 | 0.173 | +1.1c |
| $0.25–0.30 | 58642 | 0.270 | 0.241 | 0.239 | +0.2c |
| $0.30–0.35 | 62819 | 0.320 | 0.302 | 0.277 | +2.5c |

**forward**

| ask bucket | n | mean ask | mean model p | actual win rate | model − actual |
|---|---:|---:|---:|---:|---:|
| $0.05–0.10 | 609975 | 0.069 | 0.045 | 0.043 | +0.2c |
| $0.10–0.15 | 508391 | 0.120 | 0.090 | 0.086 | +0.4c |
| $0.15–0.20 | 488996 | 0.170 | 0.141 | 0.135 | +0.6c |
| $0.20–0.25 | 495305 | 0.220 | 0.197 | 0.186 | +1.2c |
| $0.25–0.30 | 510878 | 0.270 | 0.256 | 0.233 | +2.3c |
| $0.30–0.35 | 528732 | 0.320 | 0.317 | 0.283 | +3.4c |

Model coefficients (standardised inputs, largest first): logit_ask +0.58, logit_fair +0.49, dist -0.27, z500 +0.12, log_sigma -0.11, z1000 -0.07, spread_c -0.07, accel -0.07, sqrt_left +0.04, tfi -0.03, log_depth -0.03, obi +0.01, z3000 +0.01, consist +0.01, z250 +0.00; Platt a -0.01 b 1.08.

## 3. The strategy, realistic execution (FOK 10 shares at ask + 1c, edge >= 0.05, hold to settlement)

| period | markets | candidates | +support | eligible | orders | fills | fill rate | gross $ | fees $ | net $ | net/trade $ | win rate | max DD $ | max exposure $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| train | 1,408 | 584,891 | 164,534 | 235 | 156 | 89 | 57% | +209.31 | 12.33 | **+196.98** | +2.222 | 52% | 41.58 | 15.10 |
| validation | 564 | 202,795 | 52,229 | 101 | 77 | 40 | 52% | +105.97 | 5.71 | **+100.25** | +2.506 | 56% | 11.36 | 7.90 |
| test | 931 | 381,134 | 106,786 | 153 | 109 | 58 | 54% | +163.17 | 7.80 | **+155.36** | +2.663 | 55% | 13.87 | 13.60 |
| forward | 8,102 | 3,251,452 | 811,214 | 2,356 | 1651 | 1069 | 65% | +660.97 | 155.61 | **+505.36** | +0.473 | 36% | 111.25 | 19.53 |

Per-draw averages over 3 latency draws. Candidates = cheap-side book updates in the band; +support = of those, the spot points the cheap side's way; eligible = also >= the edge bar; orders = sent under the 1-position / 3-attempt / 2 s budget.

### train: the fills, by regime (all 3 draws pooled; net $ per 10-share trade)

Entry price mean 0.286, model p mean 0.409, actual win rate 0.523, net/trade +2.222 (t = 7.3, n = 266).

**by entry price**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| $0.05–0.10 | 10 | 0.174 | 0.300 | +22.23 | +2.223 |
| $0.10–0.15 | 11 | 0.249 | 0.000 | -15.35 | -1.395 |
| $0.15–0.20 | 12 | 0.325 | 0.500 | +39.99 | +3.332 |
| $0.20–0.25 | 30 | 0.327 | 0.667 | +128.95 | +4.298 |
| $0.25–0.30 | 28 | 0.392 | 0.464 | +49.51 | +1.768 |
| $0.30–0.35 | 118 | 0.447 | 0.602 | +311.88 | +2.643 |

**by time left at entry**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| 0–60 s | 18 | 0.243 | 0.000 | -26.62 | -1.479 |
| 60–120 s | 6 | 0.293 | 1.000 | +52.37 | +8.728 |
| 120–180 s | 9 | 0.345 | 0.667 | +37.91 | +4.212 |
| 180–240 s | 6 | 0.438 | 0.500 | +8.96 | +1.494 |
| 240–300 s | 227 | 0.427 | 0.546 | +518.32 | +2.283 |

**by momentum (1 s spot return in sigma, signed to the side)**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| flat | 181 | 0.423 | 0.508 | +354.53 | +1.959 |
| with (0.5-2) | 57 | 0.399 | 0.509 | +116.17 | +2.038 |
| strong with (> 2) | 28 | 0.338 | 0.643 | +120.25 | +4.295 |

**by asset**

| asset | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| BTC | 56 | 0.412 | 0.446 | +81.52 | +1.456 |
| ETH | 54 | 0.424 | 0.556 | +136.67 | +2.531 |
| SOL | 41 | 0.394 | 0.659 | +151.73 | +3.701 |
| XRP | 37 | 0.406 | 0.324 | +4.77 | +0.129 |
| DOGE | 9 | 0.377 | 0.333 | +2.61 | +0.290 |
| BNB | 39 | 0.399 | 0.769 | +187.35 | +4.804 |
| HYPE | 30 | 0.423 | 0.400 | +26.30 | +0.877 |

Unfilled orders: QUOTE_GONE 201.

### validation: the fills, by regime (all 3 draws pooled; net $ per 10-share trade)

Entry price mean 0.293, model p mean 0.409, actual win rate 0.558, net/trade +2.506 (t = 5.5, n = 120).

**by entry price**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| $0.10–0.15 | 3 | 0.252 | 1.000 | +26.49 | +8.831 |
| $0.15–0.20 | 6 | 0.338 | 0.500 | +17.95 | +2.992 |
| $0.20–0.25 | 13 | 0.326 | 0.462 | +30.98 | +2.383 |
| $0.25–0.30 | 28 | 0.402 | 0.429 | +39.41 | +1.407 |
| $0.30–0.35 | 46 | 0.439 | 0.717 | +174.06 | +3.784 |

**by time left at entry**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| 0–60 s | 6 | 0.325 | 0.500 | +20.47 | +3.412 |
| 60–120 s | 3 | 0.440 | 0.000 | -11.08 | -3.693 |
| 120–180 s | 3 | 0.509 | 1.000 | +21.80 | +7.265 |
| 180–240 s | 3 | 0.460 | 0.000 | -10.57 | -3.523 |
| 240–300 s | 105 | 0.409 | 0.581 | +280.15 | +2.668 |

**by momentum (1 s spot return in sigma, signed to the side)**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| flat | 102 | 0.416 | 0.569 | +257.23 | +2.522 |
| with (0.5-2) | 9 | 0.365 | 0.333 | +4.37 | +0.486 |
| strong with (> 2) | 9 | 0.379 | 0.667 | +39.16 | +4.351 |

**by asset**

| asset | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| BTC | 24 | 0.415 | 0.625 | +75.81 | +3.159 |
| ETH | 20 | 0.395 | 0.300 | +3.20 | +0.160 |
| SOL | 18 | 0.421 | 0.500 | +35.13 | +1.952 |
| XRP | 12 | 0.457 | 1.000 | +80.39 | +6.699 |
| DOGE | 18 | 0.391 | 0.667 | +65.15 | +3.620 |
| BNB | 22 | 0.386 | 0.455 | +33.04 | +1.502 |
| HYPE | 6 | 0.443 | 0.500 | +8.04 | +1.341 |

Unfilled orders: QUOTE_GONE 111.

### test: the fills, by regime (all 3 draws pooled; net $ per 10-share trade)

Entry price mean 0.275, model p mean 0.422, actual win rate 0.554, net/trade +2.663 (t = 7.4, n = 175).

**by entry price**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| $0.05–0.10 | 12 | 0.168 | 0.000 | -7.67 | -0.639 |
| $0.10–0.15 | 3 | 0.279 | 0.000 | -3.19 | -1.063 |
| $0.15–0.20 | 3 | 0.245 | 0.000 | -5.08 | -1.694 |
| $0.20–0.25 | 14 | 0.407 | 0.643 | +56.79 | +4.057 |
| $0.25–0.30 | 35 | 0.470 | 0.571 | +99.95 | +2.856 |
| $0.30–0.35 | 83 | 0.458 | 0.590 | +207.67 | +2.502 |

**by time left at entry**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| 0–60 s | 30 | 0.228 | 0.100 | -4.38 | -0.146 |
| 120–180 s | 3 | 0.435 | 0.000 | -10.06 | -3.352 |
| 240–300 s | 142 | 0.463 | 0.662 | +480.53 | +3.384 |

**by momentum (1 s spot return in sigma, signed to the side)**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| flat | 115 | 0.427 | 0.583 | +337.26 | +2.933 |
| with (0.5-2) | 54 | 0.410 | 0.556 | +149.15 | +2.762 |
| strong with (> 2) | 6 | 0.437 | 0.000 | -20.32 | -3.386 |

**by asset**

| asset | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| BTC | 27 | 0.437 | 0.333 | +8.31 | +0.308 |
| ETH | 33 | 0.432 | 0.636 | +117.42 | +3.558 |
| SOL | 42 | 0.392 | 0.429 | +62.10 | +1.479 |
| XRP | 23 | 0.390 | 0.609 | +73.45 | +3.193 |
| DOGE | 12 | 0.436 | 0.750 | +62.56 | +5.214 |
| BNB | 26 | 0.449 | 0.538 | +61.94 | +2.382 |
| HYPE | 12 | 0.457 | 1.000 | +80.31 | +6.693 |

Unfilled orders: QUOTE_GONE 152.

### forward: the fills, by regime (all 3 draws pooled; net $ per 10-share trade)

Entry price mean 0.303, model p mean 0.425, actual win rate 0.365, net/trade +0.473 (t = 5.6, n = 3206).

**by entry price**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| $0.05–0.10 | 45 | 0.192 | 0.000 | -34.18 | -0.760 |
| $0.10–0.15 | 31 | 0.249 | 0.452 | +99.78 | +3.219 |
| $0.15–0.20 | 71 | 0.301 | 0.296 | +76.65 | +1.080 |
| $0.20–0.25 | 204 | 0.352 | 0.353 | +236.23 | +1.158 |
| $0.25–0.30 | 635 | 0.401 | 0.345 | +360.84 | +0.568 |
| $0.30–0.35 | 1588 | 0.445 | 0.389 | +771.19 | +0.486 |

**by time left at entry**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| 0–60 s | 152 | 0.306 | 0.151 | -29.83 | -0.196 |
| 60–120 s | 84 | 0.446 | 0.310 | -16.70 | -0.199 |
| 120–180 s | 169 | 0.417 | 0.343 | +34.97 | +0.207 |
| 180–240 s | 504 | 0.418 | 0.298 | -183.73 | -0.365 |
| 240–300 s | 2297 | 0.434 | 0.397 | +1711.39 | +0.745 |

**by momentum (1 s spot return in sigma, signed to the side)**

| bucket | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| flat | 2427 | 0.427 | 0.376 | +1304.85 | +0.538 |
| with (0.5-2) | 618 | 0.417 | 0.286 | -125.67 | -0.203 |
| strong with (> 2) | 161 | 0.411 | 0.503 | +336.92 | +2.093 |

**by asset**

| asset | fills | model p | actual | net $ | net/trade |
|---|---:|---:|---:|---:|---:|
| BTC | 898 | 0.422 | 0.342 | +172.48 | +0.192 |
| ETH | 682 | 0.426 | 0.336 | +93.94 | +0.138 |
| SOL | 342 | 0.441 | 0.447 | +448.79 | +1.312 |
| XRP | 289 | 0.431 | 0.464 | +443.23 | +1.534 |
| DOGE | 230 | 0.408 | 0.465 | +411.40 | +1.789 |
| BNB | 685 | 0.424 | 0.293 | -211.22 | -0.308 |
| HYPE | 80 | 0.406 | 0.487 | +157.49 | +1.969 |

Unfilled orders: QUOTE_GONE 1744, NO_DEPTH 3.

