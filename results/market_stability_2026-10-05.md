# Which 5-minute market suits the pair-lock recovery bot?

Common period of all 7 markets: 09-22 16:35 - 10-01 00:40 UTC. Open rule as the bot (higher token 1 s after the open, ask 0.50-0.95, bid at 0.95 - ask). A swing = the UP price crossing from >= 0.55 to <= 0.45 or back.

| market | windows | open leader wins | pair bid fills first | overtaken first (recovery) | swings / window | 2+ swings | 3+ swings | a swing in the last 60 s | spread c | near-touch ask depth | median 1 s move c | 95th pct 1 s move c |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | 1797 | 61% | 58% | 42% | 1.73 | 40% | 26% | 7% | 1.0 | 1454 | 0.00 | 3.42 |
| HYPE | 1189 | 48% | 18% | 82% | 1.73 | 41% | 26% | 9% | 3.0 | 22 | 0.00 | 3.00 |
| ETH | 1861 | 60% | 51% | 49% | 2.01 | 46% | 32% | 9% | 1.0 | 299 | 0.00 | 4.00 |
| BNB | 1975 | 59% | 38% | 62% | 2.64 | 51% | 37% | 10% | 2.0 | 49 | 0.00 | 4.50 |
| SOL | 2009 | 58% | 46% | 54% | 2.55 | 52% | 38% | 10% | 1.0 | 117 | 0.00 | 4.50 |
| XRP | 2020 | 58% | 43% | 57% | 2.86 | 54% | 42% | 11% | 1.0 | 94 | 0.00 | 5.00 |
| DOGE | 2043 | 58% | 36% | 64% | 2.94 | 56% | 44% | 12% | 2.0 | 93 | 0.00 | 5.50 |

Sorted by the share of windows with 3+ swings (the chains behind the big losses), then by pair fills.
