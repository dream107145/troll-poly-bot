# Paper bot (risingrecovery-paper) vs dangerously-skip-permissions, 10-13 14:35 -> 10-13 15:10 UTC

Resolved BTC 5m markets: wallet 6 (of 8 traded), bot 5 (of 7 traded), both 5.

| | P&L per market | spend per market | return on spend |
|---|---|---:|---:|
| wallet (all its markets) | -56.64 / market (+-36.54, n 6, total -339.82) | 1547.6 | -3.66 % |
| bot (all its markets) | +0.25 / market (+-1.68, n 5, total +1.23) | 33.8 | +0.73 % |
| wallet, markets both traded | -51.71 / market (+-43.51, n 5, total -258.53) | 1526.1 | -3.39 % |
| bot, markets both traded | +0.25 / market (+-1.68, n 5, total +1.23) | 33.8 | +0.73 % |

Wallet rebates paid in the window: +0.00 USDC (not in the P&L above).

## How they trade

- wallet: 8 markets, 815 buys; seconds after the open p10/median/p90 14/104/191; price median 0.50; both sides in 8/8; lean |U-D|/max median 0.17
- bot: 7 markets, 46 buys; seconds after the open p10/median/p90 4/75/195; price median 0.51; both sides in 7/7; lean |U-D|/max median 0.25
- markets both traded: the bigger side agreed in 1/5

## Per market (both traded)

| market (UTC) | won | wallet U/D, cost, P&L | bot U/D, cost, P&L |
|---|---|---|---|
| 10-13 14:40 | UP | 794/954, 844, -50.02 | 10.0/10.0, 10.64, -0.64 |
| 10-13 14:45 | UP | 1862/1543, 1795, +66.82 | 30.0/40.0, 28.95, +1.05 |
| 10-13 14:50 | DOWN | 510/526, 564, -37.28 | 30.0/10.0, 16.39, -6.39 |
| 10-13 14:55 | DOWN | 2593/2546, 2555, -9.45 | 30.0/40.0, 37.56, +2.44 |
| 10-13 15:00 | UP | 1644/2025, 1873, -228.60 | 80.0/40.0, 75.23, +4.77 |
