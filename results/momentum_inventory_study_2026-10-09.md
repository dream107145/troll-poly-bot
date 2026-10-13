# Momentum-following inventory strategy: test (2026-10-09)

3,624 recorded BTC 5-minute markets (09-22 15:40 -> 10-09 13:40 UTC), outcomes from gamma (closed, resolved 1/0). Edge per share = payout - price - taker fee; maker fills pay no fee and get NO rebate.

## Part A -- the wallet's own fills against the spec

92,591 wallet fills in 3,261 BTC markets we recorded (09-22 -> 10-08), matched to our book at block time - 2 s (block times are whole seconds: +-1 s).

| fills | shares | cost | edge per share | edge % of cost |
|---|---:|---:|---:|---:|
| taker | 2,386,180 | 1,198,878 | +0.46c | +0.91 % |
| taker trigger=yes | 1,500,014 | 778,636 | +0.45c | +0.86 % |
| taker trigger=no | 886,166 | 420,243 | +0.48c | +1.02 % |
| taker vs book: below | 1,077,661 | 541,763 | +1.28c | +2.54 % |
| taker vs book: at | 596,650 | 294,600 | -0.02c | -0.03 % |
| taker vs book: above | 711,869 | 362,515 | -0.38c | -0.75 % |
| maker | 452,504 | 223,427 | +2.14c | +4.32 % |
| maker trigger=yes | 274,401 | 139,626 | +2.72c | +5.35 % |
| maker trigger=no | 178,104 | 83,801 | +1.23c | +2.62 % |

The same trigger-accepted taker buys at OUR execution (the ask our book shows L ms after the wallet's match time, limit = that ask + 1c, fee added):

| latency | shares filled | missed buys | edge per share | edge % of cost |
|---|---:|---:|---:|---:|
| 250 ms | 1,115,700 | 11,194 | -1.59c | -3.03 % |
| 500 ms | 1,001,987 | 14,420 | -1.85c | -3.55 % |
| 1000 ms | 905,018 | 17,078 | -2.23c | -4.37 % |

## Part B -- the spec on its own (recorded books, chronological split)

TRAIN < 10-01 01:40 <= VALIDATION < 10-04 14:25 <= TEST (UTC). 192 configurations (trigger 1/2/3c over 1 s or 3 s, confirmation on/off, exhaustion cap off/5c, spread <= 1c/2c, margin 0.5/1/2/3c).

Every trigger, no model (bought at the ask 250 ms later, held): train -1.30c ±0.11 (n 116,035, t -12.0), validation -1.33c ±0.19 (n 53,639, t -6.9), test -1.55c ±0.17 (n 58,356, t -8.9) (trigger 1c / 1 s, spread <= 1c).

**Taker, best on TRAIN:** trigger 3c over 3 s, confirm True, cap None, spread <= 1c, margin 3c

| split | realised per share | model's expected edge |
|---|---|---:|
| train | +4.15c ±0.85 (n 7,010, t +4.9) | +4.35c |
| val | +0.09c ±1.36 (n 4,705, t +0.1) | +4.44c |
| test | -3.57c ±1.44 (n 4,069, t -2.5) | +4.19c |

TEST robustness: latency 500 ms -3.05c ±1.32 (n 4,419, t -2.3); 1 s -2.47c ±1.17 (n 4,982, t -2.1); +1c slippage -4.46c ±1.85 (n 1,712, t -2.4). Validation Brier: model 0.1784 vs market mid 0.1783.

**Taker, chosen on VALIDATION:** trigger 1c over 3 s, confirm False, cap None, spread <= 1c, margin 3c

| split | realised per share | model's expected edge |
|---|---|---:|
| train | +2.61c ±0.93 (n 12,688, t +2.8) | +4.16c |
| val | +1.39c ±1.35 (n 8,983, t +1.0) | +4.23c |
| test | -2.61c ±1.47 (n 7,366, t -1.8) | +4.14c |

TEST robustness: latency 500 ms -3.13c ±1.37 (n 7,957, t -2.3); 1 s -2.66c ±1.21 (n 8,938, t -2.2); +1c slippage -3.78c ±1.80 (n 2,997, t -2.1). Validation Brier: model 0.1622 vs market mid 0.1624.

Configurations positive on VALIDATION: 161 of 192; of those, positive on TEST too: 0.

**Maker bids, chosen on VALIDATION** (trigger 2c / 3 s, margin 3c): fill rate 55% of 17,005 bids; train +1.18c ±0.69 (n 17,355, t +1.7), validation +0.22c ±1.07 (n 8,309, t +0.2), test -3.09c ±1.07 (n 9,312, t -2.9).

**Section-7 portfolio on TEST** (bankroll 200, open cost <= 40, projected loss <= 10, daily stop 6, one market):

- $2 orders as written: 79 orders, 3970 signals impossible (5 shares cost more than $2 above ~0.40), P&L +73.36, max drawdown 19.99, 20/53 markets positive
- 5-share minimum orders: 174 orders, P&L -55.28, max drawdown 74.47, 34/57 markets positive


## Part C -- maker bids on a rising token (the wallet's maker side)

TRAIN < 10-01 01:40 <= VALIDATION < 10-04 14:25 <= TEST. 48 configurations (rise 1/2c over 1/3 s, rest 5/15/30 s, cancel on a 1c reversal or not, band 0.05-0.95 or 0.30-0.85). Per share filled = payout - bid price (no fee, no rebate). CROSS = filled only when the ask comes down to the bid (lower bound); QUEUE = filled once the bid level's depth fell by the queue ahead + 5 sh (cancels counted as trades: optimistic).

| | split | placed | CROSS fills: rate, edge | QUEUE fills: rate, edge |
|---|---|---:|---|---|
| chosen on VALIDATION (0.01, 1, 30, True, (0.3, 0.85)) | train | 55,209 | 1%, +1.76c ±2.01 (n 521, t +0.9) | 12%, -0.03c ±0.55 (n 6,638, t -0.0) |
|  | val | 26,095 | 1%, +0.23c ±2.91 (n 252, t +0.1) | 16%, +0.04c ±0.77 (n 4,100, t +0.1) |
|  | test | 29,489 | 1%, -5.63c ±2.85 (n 287, t -2.0) | 16%, -2.93c ±0.74 (n 4,595, t -3.9) |
| BASELINE every 10 s, no momentum | train | 42,384 | 1%, +1.37c ±2.19 (n 384, t +0.6) | 14%, -0.57c ±0.52 (n 6,003, t -1.1) |
|  | val | 21,354 | 1%, -5.22c ±3.32 (n 160, t -1.6) | 19%, -0.60c ±0.70 (n 3,977, t -0.9) |
|  | test | 22,484 | 1%, -0.77c ±2.85 (n 205, t -0.3) | 17%, -1.00c ±0.68 (n 3,872, t -1.5) |

QUEUE model: 1 of 48 configurations positive on VALIDATION, 0 of those also on TEST. (The CROSS fill rate is 1 % because the
1c-reversal cancel fires before the ask can reach the bid; without that cancel, Part B's maker module filled 55 % and lost
-3.09c on TEST.)

## Part A follow-up -- the wallet's maker fills by state (our book at block time - 2 s)

Token rising >= 1c in the prior 3 s +3.10c a share (245k sh), flat +2.07c, falling +0.67c; price 0.4-0.8 +1.9..+4.2c,
0.1-0.2 -0.0..-0.9c; >240 s left +3.03c, 30-240 s +1.7..1.8c. (Block times are +-1 s, so "where in the book" is not
reliable.)
