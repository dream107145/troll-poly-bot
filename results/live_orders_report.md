# Real spot-lag orders, followed through the archive

703 of 905 real entry orders matched to an archived window; 50 filled. Markouts are cents per share after taker fees on both legs, for a fill at the DECISION ask (mean ± 95 % CI). `real` = the actual round trips.

## Filled vs killed (adverse selection)

| orders | n | 1 s | 5 s | 20 s | 60 s | settle |
|---|---:|---:|---:|---:|---:|---:|
| filled | 50 | -3.5 ±1.1 | +1.6 ±3.8 | +5.6 ±4.6 | +8.2 ±8.8 | +6.0 ±13.3 |
| killed | 653 | +8.6 ±0.9 | +12.5 ±1.2 | +11.2 ±1.6 | +11.6 ±2.4 | +14.1 ±3.6 |
| all | 703 | +7.7 ±0.8 | +11.7 ±1.1 | +10.8 ±1.5 | +11.3 ±2.3 | +13.5 ±3.5 |

## DEV (384 orders, 25 filled)

### How far the ask had already moved our way in the last 1 s

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ask fell ≥ 1c (buying against the move) | 54 | 5 | 9% | +22.8 ±5.8 | +20.9 ±7.3 | +16.6 ±9.5 | +33.2 ±11.8 | -1.5 ±7.4 | -0.55 |
| flat (−1c..+1c) | 199 | 13 | 7% | +14.9 ±2.3 | +13.6 ±2.7 | +14.2 ±4.1 | +12.5 ±6.9 | +4.6 ±14.1 | +8.49 |
| already up 1–3c | 65 | 4 | 6% | +13.3 ±3.4 | +14.7 ±4.3 | +18.7 ±7.8 | +29.2 ±10.4 | +0.2 ±4.4 | +0.11 |
| already up 3–6c | 41 | 3 | 7% | +7.5 ±5.1 | +7.0 ±6.9 | +2.6 ±10.3 | +9.8 ±16.7 | -2.7 ±7.9 | -0.22 |
| already up > 6c | 25 | 0 | 0% | +14.1 ±5.5 | +14.4 ±9.0 | +12.8 ±15.0 | +14.1 ±19.2 | — | +0.00 |

### …over the last 3 s

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ≤ 0c | 161 | 9 | 6% | +17.2 ±2.6 | +16.5 ±3.1 | +13.9 ±4.7 | +19.2 ±7.5 | +6.9 ±20.1 | +9.07 |
| 0–3c | 107 | 4 | 4% | +14.6 ±2.9 | +13.8 ±4.0 | +19.5 ±6.0 | +25.6 ±9.0 | +3.3 ±6.0 | +1.35 |
| 3–6c | 54 | 7 | 13% | +15.2 ±4.9 | +13.8 ±6.0 | +13.1 ±8.6 | +16.8 ±12.3 | -3.4 ±4.6 | -1.91 |
| 6–10c | 40 | 2 | 5% | +10.1 ±4.5 | +7.6 ±6.8 | +6.1 ±10.6 | +9.9 ±15.7 | -1.8 ±7.3 | -0.09 |
| > 10c | 22 | 3 | 14% | +7.9 ±9.3 | +11.5 ±12.0 | +1.7 ±14.5 | -8.3 ±22.6 | -0.8 ±14.3 | -0.60 |

### Entry price (ask)

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| < 0.30 | 108 | 8 | 7% | +22.1 ±4.0 | +24.5 ±4.8 | +27.0 ±7.2 | +29.5 ±9.1 | +6.4 ±23.0 | +7.81 |
| 0.30–0.45 | 100 | 7 | 7% | +17.2 ±3.1 | +15.3 ±3.8 | +15.6 ±5.8 | +22.7 ±9.6 | -1.5 ±6.4 | -0.51 |
| 0.45–0.55 | 86 | 7 | 8% | +9.1 ±2.8 | +8.0 ±4.0 | +6.7 ±6.4 | +9.5 ±10.3 | +0.4 ±4.8 | +0.60 |
| 0.55–0.65 | 81 | 1 | 1% | +9.6 ±2.5 | +7.3 ±3.2 | +5.8 ±5.1 | +7.3 ±10.3 | -2.4 | -0.24 |
| ≥ 0.65 | 9 | 2 | 22% | +4.8 ±3.1 | +2.4 ±7.7 | +0.5 ±10.3 | +8.2 ±20.3 | +1.6 ±9.3 | +0.16 |

### Seconds left in the window

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 60–120 s | 59 | 5 | 8% | +20.7 ±6.5 | +20.5 ±8.5 | +24.4 ±14.0 | +24.9 ±12.9 | +13.4 ±37.0 | +9.53 |
| 120–180 s | 93 | 6 | 6% | +16.8 ±3.5 | +16.9 ±4.2 | +17.7 ±6.9 | +24.3 ±9.1 | -2.0 ±5.3 | -0.86 |
| 180–240 s | 109 | 7 | 6% | +14.9 ±3.0 | +13.3 ±3.8 | +15.9 ±5.4 | +16.6 ±9.5 | +0.6 ±5.0 | +0.34 |
| 240–300 s | 123 | 7 | 6% | +10.7 ±1.8 | +10.2 ±2.7 | +6.7 ±4.4 | +11.4 ±8.6 | -2.0 ±4.9 | -1.19 |

### Shock size |z|

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 3.0–3.5σ | 98 | 11 | 11% | +9.5 ±3.2 | +7.0 ±3.7 | +6.9 ±6.0 | +11.3 ±9.8 | +8.2 ±15.9 | +12.69 |
| 3.5–4σ | 73 | 7 | 10% | +10.4 ±3.3 | +9.1 ±4.7 | +14.0 ±7.7 | +15.3 ±11.0 | -5.8 ±5.0 | -4.34 |
| 4–5σ | 80 | 5 | 6% | +9.5 ±2.7 | +10.3 ±4.3 | +6.5 ±6.3 | +17.4 ±10.2 | -1.5 ±7.3 | -0.69 |
| ≥ 5σ | 133 | 2 | 2% | +24.5 ±2.9 | +24.4 ±3.6 | +23.8 ±5.4 | +25.0 ±8.4 | +1.6 ±9.3 | +0.16 |

## TEST (319 orders, 25 filled)

### How far the ask had already moved our way in the last 1 s

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ask fell ≥ 1c (buying against the move) | 63 | 6 | 10% | +9.2 ±2.6 | +9.7 ±3.7 | +9.0 ±6.0 | +16.2 ±10.8 | -2.3 ±7.9 | -0.62 |
| flat (−1c..+1c) | 165 | 15 | 9% | +7.5 ±1.8 | +6.2 ±2.5 | +7.2 ±4.6 | +5.6 ±6.9 | -2.0 ±5.4 | -1.13 |
| already up 1–3c | 42 | 2 | 5% | +9.0 ±3.5 | +5.3 ±5.8 | -2.2 ±8.7 | -0.0 ±12.8 | +8.3 ±4.8 | +1.69 |
| already up 3–6c | 37 | 1 | 3% | +3.1 ±3.5 | +4.0 ±7.1 | +18.9 ±12.0 | +11.3 ±15.4 | -8.6 | -0.51 |
| already up > 6c | 12 | 1 | 8% | +19.2 ±8.5 | +11.8 ±15.2 | +21.2 ±30.8 | +16.7 ±27.0 | +10.6 | +1.12 |

### …over the last 3 s

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ≤ 0c | 151 | 15 | 10% | +8.6 ±1.8 | +7.4 ±2.6 | +7.8 ±4.8 | +9.1 ±7.1 | -5.5 ±4.8 | -4.20 |
| 0–3c | 78 | 4 | 5% | +8.0 ±2.5 | +5.9 ±3.7 | +5.8 ±6.3 | +4.0 ±9.8 | +8.5 ±2.2 | +2.63 |
| 3–6c | 48 | 4 | 8% | +7.7 ±3.5 | +5.5 ±5.6 | +6.4 ±8.5 | +2.4 ±12.7 | +5.7 ±6.7 | +2.09 |
| 6–10c | 20 | 0 | 0% | +3.7 ±5.4 | +6.0 ±8.8 | +4.8 ±16.7 | +5.9 ±21.9 | — | +0.00 |
| > 10c | 22 | 2 | 9% | +7.5 ±6.4 | +8.6 ±9.4 | +25.9 ±14.8 | +29.0 ±18.2 | +1.0 ±18.8 | +0.04 |

### Entry price (ask)

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| < 0.30 | 122 | 9 | 7% | +8.0 ±2.1 | +6.0 ±3.0 | +9.0 ±5.6 | +2.9 ±7.0 | -5.3 ±4.8 | -2.73 |
| 0.30–0.45 | 95 | 9 | 9% | +9.2 ±2.6 | +8.9 ±3.9 | +10.6 ±5.9 | +9.0 ±10.1 | +0.5 ±7.4 | +1.14 |
| 0.45–0.55 | 46 | 1 | 2% | +5.5 ±3.6 | +6.0 ±5.4 | +8.7 ±8.9 | +16.7 ±13.8 | +11.7 | +0.59 |
| 0.55–0.65 | 40 | 4 | 10% | +7.7 ±2.7 | +3.6 ±4.9 | -2.5 ±10.0 | +7.2 ±14.6 | +3.7 ±8.9 | +1.97 |
| ≥ 0.65 | 16 | 2 | 12% | +7.8 ±4.1 | +10.4 ±5.1 | +9.8 ±11.4 | +18.6 ±16.7 | -3.9 ±24.4 | -0.40 |

### Seconds left in the window

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 60–120 s | 57 | 4 | 7% | +10.6 ±3.6 | +7.7 ±5.9 | +19.2 ±14.1 | +12.2 ±10.2 | -0.8 ±6.8 | -0.12 |
| 120–180 s | 98 | 2 | 2% | +10.3 ±2.5 | +7.9 ±3.4 | +8.6 ±6.3 | +9.8 ±9.0 | +11.2 ±1.1 | +1.70 |
| 180–240 s | 81 | 11 | 14% | +3.8 ±2.1 | +3.8 ±3.6 | +2.9 ±6.2 | +1.4 ±10.2 | -1.0 ±7.2 | +0.31 |
| 240–300 s | 83 | 8 | 10% | +7.3 ±2.0 | +7.6 ±3.3 | +8.6 ±4.9 | +9.5 ±9.9 | -4.0 ±5.7 | -1.34 |

### Shock size |z|

| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 3.0–3.5σ | 160 | 13 | 8% | +5.3 ±1.7 | +4.4 ±2.7 | +4.6 ±4.6 | +8.3 ±7.0 | -3.1 ±4.9 | -1.53 |
| 3.5–4σ | 67 | 9 | 13% | +7.8 ±2.4 | +8.1 ±4.0 | +11.3 ±8.0 | +11.0 ±10.9 | -0.3 ±7.7 | +0.60 |
| 4–5σ | 59 | 2 | 3% | +12.2 ±3.2 | +8.7 ±4.6 | +8.8 ±7.6 | +7.1 ±11.6 | +3.7 ±15.7 | +0.37 |
| ≥ 5σ | 33 | 1 | 3% | +13.6 ±4.7 | +12.3 ±5.9 | +15.1 ±9.7 | +2.5 ±14.7 | +10.6 | +1.12 |

## Exits, replayed on the real fills

31 real fills with an archived path. Each exit rule is replayed from the real fill price: sell at the bid 300 ms after the first archive point that hits the take-profit / stop-loss / hold time (a stop that gaps is paid in full), taker fees on both legs; `settle` holds to the 1 / 0 outcome.

Calibration: the live rule (TP 10 / SL 5 / 20 s) replayed gives +1.96¢/share on these fills; their real result was +1.66¢/share (median |replay − real| 1.9¢).

Top 15 by DEV (chosen on DEV only), with TEST alongside; the live rule last.

| take-profit | stop-loss | max hold | DEV ¢/share | n | TEST ¢/share | n |
|---|---|---|---:|---:|---:|---:|
| off | off | settle | +43.3 ±32.9 | 7 | -0.9 ±18.7 | 24 |
| off | 30c | settle | +43.3 ±32.9 | 7 | +3.4 ±16.9 | 24 |
| off | 5c | settle | +37.0 ±33.6 | 7 | -9.9 ±5.4 | 24 |
| off | off | 120 s | +35.6 ±33.0 | 7 | +0.6 ±14.7 | 24 |
| off | 30c | 120 s | +35.6 ±33.0 | 7 | +0.5 ±14.3 | 24 |
| off | 8c | settle | +35.5 ±35.0 | 7 | -9.0 ±8.4 | 24 |
| off | 10c | settle | +35.0 ±35.4 | 7 | -9.5 ±8.4 | 24 |
| off | 5c | 120 s | +34.6 ±33.2 | 7 | -8.6 ±5.6 | 24 |
| off | 15c | settle | +33.6 ±36.6 | 7 | -0.6 ±13.8 | 24 |
| off | 8c | 120 s | +33.1 ±34.5 | 7 | -7.5 ±8.5 | 24 |
| off | 10c | 120 s | +32.6 ±34.9 | 7 | -8.2 ±8.6 | 24 |
| off | 20c | settle | +32.5 ±37.6 | 7 | +1.7 ±14.8 | 24 |
| off | 15c | 120 s | +31.2 ±36.1 | 7 | +0.7 ±13.4 | 24 |
| off | 20c | 120 s | +30.6 ±36.6 | 7 | +0.3 ±13.7 | 24 |
| off | 5c | 60 s | +29.7 ±33.5 | 7 | -2.9 ±7.3 | 24 |
| 10c | 5c | 20 s | +13.6 ±20.4 | 7 | -1.4 ±3.6 | 24 |

## The limit allowance: could a higher limit have caught the good orders?

For every killed order, the venue's ask when the order arrived (archive at sent + half the measured round trip) above the decision ask, and the result of a fill THERE: the bid 5 / 20 s later minus that ask minus fees. A higher limit only helps if these are positive.

| arrival ask above decision ask | killed orders | at arrival: 5 s | 20 s | settle |
|---|---:|---:|---:|---:|
| ≤ 4c (inside the limit: killed for depth or latency) | 441 | +11.6 ±1.4 | +10.1 ±1.9 | +15.9 ±4.4 |
| 4–6c | 14 | +0.2 ±5.6 | -1.9 ±9.5 | +4.1 ±21.0 |
| 6–10c | 17 | +9.7 ±10.0 | +11.7 ±13.4 | +8.9 ±25.6 |
| 10–15c | 8 | -1.3 ±14.4 | +2.0 ±17.0 | +6.1 ±32.0 |
| > 15c | 11 | -6.4 ±4.9 | -12.4 ±11.8 | -15.0 ±34.7 |

