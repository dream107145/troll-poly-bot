# Fast feed vs the 1 s rule, 2 tick recording(s)


### ticks_20260923_204148.jsonl.gz: 90 min, 140 windows, 100 with a strike

Feed delay used (trade stamp -> receipt, median ms; cb scaled from the live measurement): cb 62, by 121, bn 147

- BTC: 449,903 quotes, detector fired 686 times (50 ms: 539, 100 ms: 29, 250 ms: 38, 500 ms: 80), 455 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: by +128 (n 425), cb +181 (n 429), bn +183 (n 362)
- ETH: 272,502 quotes, detector fired 1028 times (50 ms: 874, 100 ms: 39, 250 ms: 30, 500 ms: 85), 682 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: by +100 (n 632), bn +141 (n 572), cb +354 (n 568)
- SOL: 132,873 quotes, detector fired 584 times (50 ms: 442, 100 ms: 38, 250 ms: 50, 500 ms: 54), 441 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: by +77 (n 408), bn +85 (n 405), cb +355 (n 353)
- XRP: 243,884 quotes, detector fired 1372 times (50 ms: 1185, 100 ms: 71, 250 ms: 33, 500 ms: 83), 1106 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: by +66 (n 1048), bn +97 (n 993), cb +364 (n 947)
- DOGE: 155,612 quotes, detector fired 698 times (50 ms: 433, 100 ms: 78, 250 ms: 95, 500 ms: 92), 559 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: bn +66 (n 553), by +135 (n 541), cb +451 (n 287)
- BNB: 88,407 quotes, detector fired 540 times (50 ms: 478, 100 ms: 12, 250 ms: 16, 500 ms: 34), 304 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: bn +38 (n 302), by +186 (n 287), cb +554 (n 81)
- HYPE: 19,668 quotes, detector fired 264 times (50 ms: 166, 100 ms: 39, 250 ms: 51, 500 ms: 8), 52 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: by +24 (n 52), cb +302 (n 52)

### ticks_20260929_183946.jsonl.gz: 45 min, 77 windows, 59 with a strike

Feed delay used (trade stamp -> receipt, median ms; cb scaled from the live measurement): cb 65, by 121, bn 154

- BTC: 263,681 quotes, detector fired 480 times (50 ms: 393, 100 ms: 24, 250 ms: 24, 500 ms: 39), 344 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: by +121 (n 323), cb +182 (n 311), bn +202 (n 260)
- ETH: 176,743 quotes, detector fired 678 times (50 ms: 598, 100 ms: 19, 250 ms: 19, 500 ms: 42), 457 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: by +113 (n 421), bn +186 (n 379), cb +466 (n 376)
- SOL: 80,031 quotes, detector fired 381 times (50 ms: 245, 100 ms: 30, 250 ms: 64, 500 ms: 42), 298 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: bn +63 (n 264), by +90 (n 270), cb +421 (n 253)
- XRP: 101,045 quotes, detector fired 632 times (50 ms: 471, 100 ms: 55, 250 ms: 67, 500 ms: 39), 504 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: bn +85 (n 469), by +106 (n 456), cb +464 (n 427)
- DOGE: 62,675 quotes, detector fired 295 times (50 ms: 141, 100 ms: 26, 250 ms: 72, 500 ms: 56), 213 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: by +57 (n 210), bn +159 (n 206), cb +382 (n 49)
- BNB: 45,346 quotes, detector fired 233 times (50 ms: 212, 100 ms: 5, 250 ms: 7, 500 ms: 9), 111 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: bn +0 (n 109), by +297 (n 102), cb +860 (n 18)
- HYPE: 12,759 quotes, detector fired 0 times (50 ms: 0, 100 ms: 0, 250 ms: 0, 500 ms: 0), 0 confirmed by a second exchange within 1.5 s; lag behind the first exchange to show a confirmed move, ms: 

Total covered: 135 min


## Signals, orders, fills and P&L per rule

| rule | signals | orders | fills | fill rate | shares | P&L USDC | P&L / fill |
|---|---:|---:|---:|---:|---:|---:|---:|
| A current | 890 | 52 | 10 | 19% | 100 | +20.11 | +2.011 |
| B lead | 2351 | 59 | 25 | 42% | 244 | -0.85 | -0.034 |
| C fast-250 | 1576 | 23 | 9 | 39% | 85 | +23.64 | +2.627 |
| D fast-500 | 1085 | 44 | 15 | 34% | 146 | +32.92 | +2.195 |
| E fast-1s | 661 | 52 | 18 | 35% | 176 | +36.54 | +2.030 |

## Detection lead over rule A, per asset (same market and direction, ms; negative = earlier)

| asset | B lead | C fast-250 | D fast-500 | E fast-1s |
|---|---:|---:|---:|---:|
| BTC | -97 (n 124) | -197 (n 58) | +149 (n 76) | +259 (n 78) |
| ETH | -59 (n 137) | -158 (n 65) | +20 (n 76) | +194 (n 75) |
| SOL | -28 (n 120) | -119 (n 68) | -20 (n 66) | +111 (n 59) |
| XRP | -30 (n 142) | -117 (n 73) | +0 (n 73) | +200 (n 74) |
| DOGE | -35 (n 160) | -180 (n 119) | -41 (n 96) | +81 (n 66) |
| BNB | -6 (n 138) | -6 (n 57) | +0 (n 52) | +143 (n 52) |
| HYPE | +0 (n 45) | -39 (n 40) | +0 (n 26) | +0 (n 21) |
| ALL | -33 (n 866) | -119 (n 480) | -1 (n 465) | +150 (n 425) |

## Fills and P&L per asset (rule A -> rule C -> rule D)

| asset | A fills / orders, P&L | C fills / orders, P&L | D fills / orders, P&L |
|---|---:|---:|---:|
| BTC | 4 / 16, +4.87 | 1 / 5, +3.41 | 2 / 10, +2.41 |
| ETH | 2 / 13, +6.00 | 4 / 8, +6.60 | 4 / 12, +7.14 |
| SOL | 0 / 3, +0.00 | 2 / 3, +6.49 | 2 / 5, +3.52 |
| XRP | 0 / 9, +0.00 | 0 / 2, +0.00 | 2 / 7, +6.72 |
| DOGE | 1 / 6, -1.05 | 0 / 1, +0.00 | 3 / 5, +4.87 |
| BNB | 3 / 5, +10.28 | 1 / 3, +7.67 | 2 / 5, +8.25 |
| HYPE | 0 / 0, +0.00 | 1 / 1, -0.52 | 0 / 0, +0.00 |

## Why signals did not become fills (rule A vs C)

- A current: edge 341, price band 325, budget 91, spread 81, killed 42, filled 10
- C fast-250: edge 759, price band 603, spread 110, budget 81, killed 14, filled 9
exit 0
