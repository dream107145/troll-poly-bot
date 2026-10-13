# Next-market TWAP prediction: research report (2026-09-25)

## Verdict: NO. There is no evidence of an executable edge. Do not paper-trade or shadow it.

**The information is real, but it is not tradable.** Information from about 3 s before a new market opens does predict its outcome. At T − 3 s the analytic TWAP model is right 58–59 % of the time out of sample (Brier 0.236–0.242 against 0.250 for a coin flip). But the prediction cannot be bought at a price that pays for itself:

- **Across the boundary the market makers pull their quotes.** The median spread is 30c from T − 0.5 s to T + 0.25 s (p90 78c). Buying "immediately at the open" means paying about 0.64–0.66 for the favoured token.
- **The token reprices to the model within 2–3 s, while the book is still too wide to trade.** Once the book is tight again (~2c), the market price already contains everything the model knows: the model's extra information is gone (z ≈ 0). No drift toward the model is left for a buyer.
- **A round trip costs about 3.3–3.5c of taker fees plus the spread,** and fills are adversely selected.

**Every version of the hypothesis loses out of sample:**
- **The literal strategy** (predict at T − 3 s, buy at the open, sell after 3 s) loses **−11.8c/share on TEST** (575 fills, t −23) and **−8.9c on FORWARD** (65 fills). That is only slightly better than buying a random side (−13.9c).
- **With an execution-aware 2c edge gate** it trades almost never: 15 fills in 12.75 h, −1.5c/share (t −1.7).
- **Among 83,732 short-hold configurations** with at least 30 TRAIN trades, 1.5 % were positive on TRAIN. The best of those (+5.3c) failed VALIDATION (−5.8c) and TEST (−3.8c). No holding period from 0.25 s to 30 s is reliably positive: the one positive TEST cell is +0.5c at 30 s on 15 trades (t 0.2), and the same cell lost −1.7c in development.
- **The only rules that survived selection are not the hypothesis.** They are settlement holds of BTC DOWN at asks ≤ 0.50. They are a bet on direction, not prediction (§18): +5.4c on 13 TEST fills (t 0.4), with no FORWARD trades.

Nothing was enabled. The strategy exists as a research module (`TPB_NEXT_MARKET_TWAP_ENABLED=false`, no real-money path) and a replay. No live process, configuration, risk limit or other strategy was touched.

### Answers to the ten questions

1. **Does predicting the next market ~3 s before it opens contain predictive information?** **Yes, but it is weak and mostly mechanical.**
   - Walk-forward accuracy rises from 55.6 % at T − 10 s to 58.7 % at the open. On TEST it is 58.9 % at T − 3 s.
   - Most of it is the gap between spot and the forming strike (a 60 s average). At T − 3 s, 95 % of the strike is already observed, and it is predicted to 0.04 bps.
   - The market's own pre-open quotes carry the same information: on TEST at T − 3 s they are 59.0 % accurate.
2. **Does it translate into executable UP/DOWN token edge?** **No.**
   - The model adds information to the market price only where the book is wide, i.e. where the "price" is the middle of an empty book.
   - On every tight book (spread ≤ 3c), before the open at every prediction time and after it, the model adds nothing to the mid (z −1.4 to +1.4; one cell of 16 reaches 2.4).
3. **Does the edge survive realistic latency?** **There is none to survive.**
   - At zero latency the edge-gated rule loses −2.9c (TEST) and −3.0c (development).
   - For the literal rule, lower latency is *worse* (−18c at 0 ms against −11.8c measured), because a faster order lands in the emptiest book.
4. **Does it survive spread and fees?** **No.**
   - Restricted to books ≤ 1c wide, the literal rule loses −4.5c/share on TEST. That is about the 3.3c of fees plus the 1c spread plus a little adverse selection.
   - Buying the favoured side 3 s after the open, once the book is tight, has a gross markout of about −2.7c and a net of −5.9c at every hold.
5. **Which holding period has the best out-of-sample net markout?** **None is positive.**
   - Losses shrink until the book has re-formed (2.5–5 s) and are flat after that.
   - Edge-gated rule on TEST: −3.7c at 1 s, −1.5c at 3 s, −0.8c at 4 s, +0.5c at 30 s (t 0.2, 15 trades).
   - Literal rule on TEST: −13.6c at 1 s, −11.8c at 3 s, −11.2c at 5 s, −11.0c at 30 s.
   - The 3–4 s hold of the hypothesis is where the loss stops shrinking, not where a profit appears.
6. **Which assets behave differently?** Liquidity at the open decides it:
   - **BTC** keeps a 1c book through the boundary and loses least (−3.6c TEST, literal rule).
   - **ETH** is close behind (−4.6c).
   - **BNB, DOGE and HYPE** pull hardest and lose −14 to −25c. HYPE's book is 60c+ wide before and after the open.
   - The model is most accurate on BTC (62 % on TEST) but worst calibrated there.
   - No asset is positive.
7. **Does UP behave differently from DOWN?** **Not robustly.**
   - The literal rule is symmetric: UP −11.0c, DOWN −12.7c on TEST; −12.9 / −12.8c in development.
   - The gated rule's DOWN trades were less bad in both periods, on 4–30 trades.
   - The settlement survivors are all DOWN because a recalibrated model absorbed TRAIN's down-drift (UP won 46 % of TRAIN).
8. **Does it work only in certain volatility regimes?** **It works in none.**
   - Every regime with more than a handful of trades is negative: high/low volatility, strong/weak trend, reversal/continuation, deep/thin book, narrow/wide spread.
   - High volatility loses a little less (−9.6c vs −13.4c on TEST, literal rule).
9. **What minimum edge should be required before entering?** **No threshold produces positive expectancy.**
   - Raising it from 0.5c to 5c only cuts volume: development −4.0c → −3.4c/share, 140 → 24 fills.
   - It never crosses zero on TRAIN, VALIDATION or TEST.
10. **Is there enough evidence to justify paper/shadow trading?** **NO.**
    - The hypothesis fails on every held-out period.
    - The only rules that survived selection hold to settlement, bet on one direction, trade one asset, and are too thin to judge (13 TEST trades, t 0.4).
    - Their neighbourhood is an isolated peak: max ask 0.50 but not 0.60, window 0 s but not 0.5 s.

### Required performance table (TEST, 09-25 00:40–13:25 UTC, 153 epochs, 957 markets; read once after the lock)

$5 orders, the measured-latency execution model (§7), fees and slippage in dollars. Slippage is a **cost**: negative means the fill was better than the decision ask. Around the open that happens because the ask falls while the order is in flight, i.e. the market moves against the side being bought.

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | Net c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **P1 literal hypothesis** (T − 3 s, buy at the open, no filter, sell after 3 s) | 575 | 71.3 | 8.5 | 0.643 | 0.557 | −$324.10 | $133.13 | −$290.04 | **−$457.24** | −$0.795 | **−11.80** | −23.2 | $457.24 |
| P2 + execution-aware edge ≥ 2c | 3 | 18.8 | 66.7 | 0.596 | 0.633 | +$0.85 | $0.78 | −$0.94 | +$0.08 | +$0.026 | +0.61 | 0.4 | $0.19 |
| **P3** + evaluate for 3 s after the open | 15 | 30.0 | 33.3 | 0.526 | 0.543 | +$2.39 | $4.69 | −$3.99 | **−$2.30** | −$0.153 | **−1.51** | −1.7 | $2.78 |
| B: P3 entry, 1 s exit | 15 | 30.0 | 6.7 | 0.526 | 0.522 | −$0.18 | $4.74 | −$4.16 | −$4.92 | −$0.328 | −3.66 | −4.6 | $4.92 |
| C: P3 entry, 2 s exit | 15 | 30.0 | 13.3 | 0.526 | 0.530 | +$0.43 | $4.69 | −$3.49 | −$4.26 | −$0.284 | −2.81 | −3.1 | $4.26 |
| D: P3 entry, 4 s exit | 15 | 30.0 | 33.3 | 0.526 | 0.550 | +$3.30 | $4.67 | −$3.15 | −$1.37 | −$0.091 | −0.76 | −1.4 | $2.03 |
| E: P3 entry, momentum exit | 15 | 30.0 | 13.3 | 0.526 | 0.524 | −$0.05 | $4.70 | −$2.07 | −$4.75 | −$0.317 | −3.46 | −1.9 | $4.75 |
| F: P3 entry, edge-gone exit | 15 | 30.0 | 20.0 | 0.526 | 0.519 | −$1.12 | $4.67 | −$2.39 | −$5.79 | −$0.386 | −3.90 | −1.8 | $5.79 |
| L6 best short-hold rule of the sweep (T − 2 s, 5 s window, spread ≤ 2c, ask ≤ 0.40, 30 s) | 10 | 50.0 | 30.0 | 0.377 | 0.371 | −$0.58 | $4.14 | −$1.80 | −$4.72 | −$0.472 | −3.80 | −1.2 | $4.72 |
| L1 selection survivor (recalibrated model, T − 1 s, at the open, spread ≤ 2c, ask ≤ 0.50, **settlement**) | 13 | 54.2 | 53.8 | 0.467 | 0.538 | +$10.51 | $2.38 | −$1.18 | +$8.13 | +$0.626 | +5.42 | 0.4 | $14.40 |
| L3 best on TRAIN (analytic model, T − 1 s pre-open, ask ≤ 0.50, settlement) | 12 | 48.0 | 41.7 | 0.488 | 0.417 | −$8.34 | $2.14 | −$0.21 | −$10.48 | −$0.874 | −8.92 | −0.6 | $10.54 |
| random side, at the open, 3 s | 614 | 76.5 | 5.7 | 0.577 | 0.471 | −$453.63 | $155.92 | −$398.28 | −$609.56 | −$0.993 | −13.86 | −33.4 | $609.56 |
| the market's pre-open side, at the open, 3 s | 589 | 73.0 | 7.6 | 0.648 | 0.548 | −$385.09 | $134.25 | −$291.63 | −$519.34 | −$0.882 | −13.23 | −23.4 | $519.34 |
| no trade | 0 | — | — | — | — | $0 | $0 | $0 | $0 | — | 0 | — | $0 |

Rows B–F are strategies B–F of item 29. Strategy A is the P3 row (the same entry with the 3 s exit). The same comparison with the unfiltered P1 entry is −13.6 / −12.2 / −11.8 / −11.4 / −13.0 / −11.5c for the 1 s, 2 s, 3 s, 4 s, momentum and edge exits (§10).

**FORWARD** (09-25 13:25–14:45 UTC, 16 epochs, 100 markets, archived while this study ran, first read after the lock):
- The literal rule: **−8.92c/share** on 65 fills (t −6.8).
- P2 and P3: one fill each, −13.4c.
- L6 (the best short-hold rule of the sweep): one fill, −7.1c.
- L1/L2 (the settlement survivors): no trades.

### Holding time (TEST)

| Holding Time | P3 Trades | P3 Avg Markout (c) | P3 Net Markout (c) | P3 Win % | P3 Net P&L | P1 Trades | P1 Avg Markout (c) | P1 Net Markout (c) | P1 Win % | P1 Net P&L |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.25 s | 15 | −1.14 | −4.41 | 6.7 | −$5.88 | 575 | −13.74 | −16.94 | 1.7 | −$648.85 |
| 0.5 s | 15 | −1.25 | −4.52 | 6.7 | −$6.02 | 575 | −12.61 | −15.82 | 2.1 | −$608.39 |
| 0.75 s | 15 | −0.92 | −4.19 | 6.7 | −$5.47 | 575 | −11.55 | −14.76 | 2.4 | −$568.94 |
| 1 s | 15 | −0.39 | −3.66 | 6.7 | −$4.92 | 575 | −10.33 | −13.55 | 2.6 | −$524.63 |
| 1.5 s | 15 | +0.28 | −2.95 | 13.3 | −$4.24 | 575 | −9.34 | −12.55 | 5.6 | −$488.21 |
| 2 s | 15 | +0.42 | −2.81 | 13.3 | −$4.26 | 575 | −9.04 | −12.24 | 5.9 | −$474.51 |
| 2.5 s | 15 | +1.55 | −1.67 | 26.7 | −$2.59 | 575 | −8.61 | −11.81 | 7.3 | −$457.25 |
| 3 s | 15 | +1.71 | −1.51 | 33.3 | −$2.30 | 575 | −8.61 | −11.80 | 8.5 | −$457.24 |
| 3.5 s | 15 | +2.38 | −0.84 | 26.7 | −$1.35 | 575 | −8.45 | −11.64 | 10.3 | −$451.05 |
| 4 s | 15 | +2.44 | −0.76 | 33.3 | −$1.37 | 575 | −8.21 | −11.40 | 11.8 | −$440.59 |
| 5 s | 15 | +0.50 | −2.67 | 33.3 | −$4.63 | 575 | −8.04 | −11.22 | 13.6 | −$433.47 |
| 7.5 s | 15 | +0.59 | −2.55 | 33.3 | −$4.54 | 575 | −8.20 | −11.37 | 15.5 | −$438.88 |
| 10 s | 15 | +0.35 | −2.82 | 33.3 | −$3.78 | 575 | −8.11 | −11.26 | 19.1 | −$432.36 |
| 15 s | 15 | +0.87 | −2.28 | 46.7 | −$3.51 | 575 | −8.38 | −11.52 | 18.4 | −$444.18 |
| 30 s | 15 | +3.75 | +0.55 | 46.7 | +$1.20 | 575 | −7.88 | −10.96 | 26.4 | −$420.82 |
| settlement | 15 | −5.92 | −7.54 | 46.7 | −$11.94 | 575 | −6.17 | −7.71 | 58.1 | −$289.11 |

Executable: an exit decided `hold` seconds after the fill is known, a FOK sell at the bid − 2c, fees on both sides. "Avg markout" = exit − entry before fees. The development-period tables (§13) have 72 and 2,059 trades and the same shape.

### Prediction time (TEST, 3 s exit)

| Prediction Time | Accuracy | Calibration (ECE) | Brier | Avg Edge p − ask (P3 signals) | P3 Fill % | P3 Net c/share | P3 Net P&L | P1 Fill % | P1 Net c/share | P1 Net P&L |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| T − 10 s | 56.3 % | 3.4c | 0.2419 | +15.1c | 40.0 | −3.26 | −$7.24 | 69.8 | −12.32 | −$465.29 |
| T − 5 s | 57.1 % | 2.6c | 0.2392 | +12.5c | 36.1 | −2.85 | −$6.91 | 71.1 | −12.43 | −$480.14 |
| **T − 3 s** | **58.9 %** | 3.0c | 0.2361 | +11.5c | 30.0 | −1.51 | −$2.30 | 71.3 | −11.80 | −$457.24 |
| T − 2 s | 58.8 % | 3.1c | 0.2353 | +11.6c | 32.0 | −2.67 | −$4.19 | 70.2 | −11.90 | −$451.65 |
| T − 1 s | 58.0 % | 2.4c | 0.2359 | +11.9c | 36.0 | −2.56 | −$4.07 | 71.2 | −11.71 | −$450.36 |
| T + 0 s | 58.5 % | 3.5c | 0.2345 | +12.2c | 34.0 | −2.51 | −$4.15 | 70.5 | −11.80 | −$448.66 |

Accuracy and calibration are the analytic model D, which has no fitted parameters. The P3 signals see 11–15c of edge against the ask, which is the model's settlement probability minus the price. None of it is realised.

---

## 1. Strategy hypothesis

About 3 s before a new 5-minute UP/DOWN market opens:
- predict where its settlement condition will land: the strike, the settling TWAP, the displacement between them, P(UP) and P(DOWN);
- when the market opens, buy the favoured token at the ask if the execution-aware edge is large enough;
- hold for a few seconds (initially 3–4 s) and sell into the bid.

The central question: **can information available about 3 s before a new market opens predict a short-lived, executable mispricing in its UP/DOWN tokens?**

Nothing in the live system was touched. No order was placed, and no process was restarted or reconfigured. The strategy exists only as a research module and a replay.

## 2. Market mechanics

**What settles the market.** It resolves UP iff the 60 s trailing TWAP at the close is at least the 60 s trailing TWAP at the open. The Chainlink stream is `btc-usd-twap-60s` (`pricing/twap.py`, verified in `docs/60s_snipe_research.md`). This study re-checked the **open** side, which is the part it depends on. The check ran against all 4,432 venue-resolved markets in the archive, holding the close side fixed at the trailing TWAP at the close:

| strike definition (our composite spot) | agrees with the venue | n |
|---|---:|---:|
| **trailing 60 s TWAP ending at the open** | **98.3 %** (Binance alone 98.8 %) | 3,742 |
| the live bot's own strike (the same quantity, computed live) | 98.4 % | 4,432 |
| spot at the open | 93.0 % | 4,388 |
| TWAP over the market's first 60 s | 88.0 % | 4,388 |

The residual ~1.7 % is the basis between our Binance/Bybit/Coinbase composite and Chainlink's stream; no Chainlink feed is recorded on this machine.

What follows for this strategy:

1. **The strike is the average over [open − 60 s, open].** At T − 3 s, 57 of its 60 seconds are already observed. At T − 3 s the strike is predicted to 0.04 bps median (0.13 bps p90) of the value the bot later strikes.
2. **The next market's strike IS the previous market's settlement value.** Both are the trailing TWAP at the same instant, so "previous market direction" is the projected result of the market still running.
3. **The next market does not open at 50/50.** If the price has risen over the last minute, spot sits above the trailing average, and P(UP) > 0.5 the moment the window opens. That is the information the hypothesis wants to trade.
4. **The UP/DOWN tokens trade before the open.** The venue lists the market and its book is live well before its window starts. The archive records it from 60 s before the open.

**What the book does around the open.** This is the single most important fact in this report. Market makers **pull their quotes across the boundary**:

Median spread of both tokens by offset from the open (TRAIN + VALIDATION, 3,424 markets):

| offset from open | median spread c | p90 spread c | % <=1c | % <=2c | % <=3c | % <=5c | median ask depth (2 ticks) | % no ask |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| -10s | 5 | 31 | 24.62 | 32.16 | 41.44 | 60.34 | 65 | 5.9 |
| -5s | 5 | 35 | 21.2 | 28.65 | 38.29 | 57.54 | 68.8 | 5.9 |
| -3s | 5 | 47 | 19.95 | 28.21 | 37.62 | 57.45 | 90 | 5.9 |
| -2s | 12 | 59 | 17.87 | 25.82 | 34.46 | 39.19 | 200 | 5.9 |
| -1s | 17 | 78 | 17.52 | 24.53 | 33.79 | 38.32 | 117 | 5.9 |
| -0.5s | 30 | 78 | 10.22 | 15.83 | 20.44 | 27.92 | 63 | 5.9 |
| +0s | 30 | 78 | 9.696 | 15.25 | 19.74 | 27.45 | 63 | 5.9 |
| +0.25s | 25 | 78 | 11.3 | 15.33 | 18.63 | 24.97 | 50 | 5.87 |
| +0.5s | 11 | 58 | 16.33 | 20.47 | 24.47 | 30.67 | 50.05 | 5.754 |
| +1s | 11 | 53 | 18.98 | 23.36 | 27.57 | 33.03 | 50 | 5.754 |
| +1.5s | 8 | 35 | 25.23 | 31.48 | 35.46 | 40.8 | 60 | 5.695 |
| +2s | 2 | 14 | 36.92 | 47.81 | 53.94 | 63.61 | 75 | 5.666 |
| +3s | 2 | 7 | 45.33 | 60.54 | 70.53 | 81.22 | 75 | 5.549 |
| +5s | 1 | 6 | 48.51 | 62.44 | 72.46 | 84.55 | 87.55 | 5.111 |
| +10s | 1 | 4 | 58.62 | 78.3 | 86.21 | 92.03 | 104 | 3.125 |
| +30s | 1 | 4 | 60.08 | 81.31 | 88.2 | 95.3 | 104 | 0.02921 |

By asset (median spread, cents):

| asset | median spread c at -3s | 0s | +1s | +2s | +3s |
|---|---:|---:|---:|---:|---:|
| BNB | 7 | 78 | 16 | 8 | 3 |
| BTC | 1 | 1 | 1 | 1 | 1 |
| DOGE | 5 | 40 | 15 | 6 | 4 |
| ETH | 3 | 11 | 2 | 1 | 1 |
| HYPE | 61 | 64 | 71 | 10 | 5 |
| SOL | 2 | 4 | 8 | 2 | 1 |
| XRP | 5 | 39 | 11 | 2 | 1 |

On the venue-timestamped tick recording (2026-09-23, 126 markets, event resolution) the same thing happens.
The median spread is 4c at T − 3 s, 20c at T − 1 s, 21.5–30c from T − 0.5 s to T + 0.1 s (p90 78c), 11c at T + 0.25 s, and 2c by T + 2 s.
Book updates jump about 20-fold at the open, as makers re-post.

- From about T − 1 s to T + 0.25 s, the median spread of either token is **30c** (p90 78c).
- The two asks sum to about 1.30. The book re-forms by T + 2 s (median 2c).
- BTC is the exception: its book stays 1c wide throughout. HYPE is wide (60c+) before and after.

A taker who buys "immediately at the open" therefore buys into a book about 30c wide.

## 3. Data sources

| source | what | used for |
|---|---|---|
| `data/charts/*.json` (the live bots' archive) | one point every ~224 ms, from 60 s before each open to ~30 s after the close. Each point holds the composite spot, the Binance/Bybit/Coinbase prices, spot order flow (15 s trade-flow imbalance, book imbalance, OFI, trades/s), both tokens' touch, and the depth within 2 ticks of each touch | everything |
| venue outcomes (in the same files, `outcome_source = venue`) | the resolved side of all 4,432 markets | labels |
| `data/replay/live_latency_samples.json` | measured presend (p50 48 ms), venue RTT (p50 242 ms), spot feed (p50 141 ms), book (p50 93 ms) | latency model |
| `data/ticks/ticks_20260923_204148.jsonl.gz` (90 min, event level) | touch size vs depth within 2 ticks | the depth split (the touch holds a median 24 %, mean 28 %) |

**Coverage.** 764 five-minute epochs (4,432 markets, 7 assets), 2026-09-22 15:30 to 2026-09-25 13:20 UTC. The 51 boundaries with a hole in the spot series are dropped at every prediction time, leaving 4,381 rows per prediction time.

**Periods** (chronological by epoch; every asset of one epoch is one bet and lands in the same period):

| period | from (UTC) | to | epochs | markets at T − 3 s | UP rate | used for |
|---|---|---|---:|---:|---:|---|
| TRAIN | 09-22 15:30 | 09-24 11:10 | 458 | 2,548 | 46.1 % | fitting, choosing |
| VALIDATION | 09-24 11:15 | 09-25 00:35 | 153 | 876 | 53.9 % | filtering |
| TEST | 09-25 00:40 | 09-25 13:20 | 153 | 957 | 51.5 % | read once, after the lock |
| FORWARD | 09-25 13:25 | 09-25 14:45 | 16 | 100 | 49.0 % | archived while this study ran; read after the lock |

TRAIN has a down-drift (UP won 46 %), as the 60 s study found. That matters for fitted models (§6).

**What the archive cannot resolve:**
- anything faster than ~224 ms; the 250 ms horizons are one archive step;
- Polymarket trades;
- depth beyond 2 ticks of the touch;
- the Chainlink stream itself.

Each point is what the bot held **locally** at that instant: spot about 140–300 ms behind the exchanges, the book about 90 ms behind the venue. The archive is therefore itself a causal information set.

## 4. Prediction methodology

**Target.** The venue's resolved side, which is the TWAP-vs-TWAP condition above, not "BTC goes up".

**The analytic model (D).** Let `pred = open + offset` (offset ≤ 0), x the latest log price, and u_k = −offset + 0.18 s (the measured information lag) the part of the strike's window not yet seen. With a driftless Brownian log price, per-second vol σ (the project's `TwoScaleVol`, stitched over each asset's history):

    E[ln K]          = (integral of the seen log price over [open − 60 s, pred − 0.18 s] + u_k · x) / 60
    E[ln TWAP close] = x
    E[D]             = x − E[ln K]                                   (the predicted displacement)
    Var[D]           = σ² · ((u − 40) + u_k³/10800 − u_k²/120)        (u = seconds to the close)
    P(UP)            = Student-t(4) digital on E[D] / sd              (the project pricer)

The last two variance terms are the strike's own uncertainty and its covariance with the settling average. Both depend on the path between now and the open.

Each prediction reports:
- `predicted_start` = E[K];
- `predicted_twap` = E[TWAP close] (driftless: the current price);
- `predicted_displacement`;
- `p_up` and `p_down`;
- `confidence` = |2·p_up − 1|.

Fitted models output a probability only; they reuse D's start and TWAP.

**No look-ahead.**
- `boundary_features` reads only the prefix of the series stamped at or before `pred` (`_cut`).
- The book used at a decision is the last archived point at or before it.
- An order is matched against the book at its **arrival**, never the decision's.
- An exit decision reads only the bid known at that moment.
- `tests/test_next_market_twap.py` rewrites everything after the prediction time (a 5 % jump, inverted flows) at all six offsets and requires every feature to be bit-identical. It also moves a single point 1 ms past the cut and checks it is ignored.

**Chronological validation.** Three layers, none of which shuffles:
- **TRAIN cross-fit:** fitted models are cross-fitted on TRAIN epoch halves, so TRAIN numbers are out-of-fold.
- **VALIDATION:** predicted by a fit on all of TRAIN.
- **Walk-forward:** 5 expanding folds over the last 60 % of TRAIN + VALIDATION epochs.

TEST was predicted once, by a fit on TRAIN + VALIDATION, after the lock (§18).

## 5. Features

Everything at the prediction time, from `strategy/next_market_twap.boundary_features`. The research and any future paper runner call the same function.

| group | features |
|---|---|
| returns | 250 ms, 500 ms, 1, 2, 3, 5, 10, 30, 60 s log returns (a 250 ms as-of grid), also σ-normalised |
| momentum | EMA(5 s) − EMA(30 s), the fast EMA's 1 s slope, velocity (1 s return), acceleration (1 s and 5 s second differences), consecutive same-sign 250 ms ticks |
| volatility | realised per-second vol over 1, 5, 10, 30, 60, 300 s; regime = rv60 / rv300; the TwoScaleVol σ |
| order flow | 15 s trade-flow imbalance, book imbalance, OFI, trades/s and trades/s against its last minute. Individual large trades and CVD are not in the archive |
| cross-exchange | each exchange's deviation from the composite (bps), max − min dispersion, seconds since each exchange's price last changed (staleness), each exchange's 1 s move minus the composite's (lead) |
| transition | predicted strike and displacement (log and σ-normalised), share of the strike's window already seen, previous market's projected result (E[ln K] − the previous strike, i.e. its settlement shaping up), the market two back's venue outcome, the latest spot age |
| the market itself | the new market's own pre-open Up mid, its 3 s and 10 s change, both tokens' touch and depth, book age |

Time between markets is always 0 (the windows are contiguous) and is not a feature.

## 6. Baselines

Every model predicts the same target from the same causal features at each prediction time. The fitted ones are fitted per prediction time and pooled across assets (per-asset fits would overfit ~450 TRAIN rows).

| model | what | fitted |
|---|---|---|
| A momentum | logistic on one σ-normalised return; the lookback (1–60 s) chosen inside the training rows | yes |
| B weighted recent returns | logistic on all nine normalised returns | yes |
| C EMA momentum | logistic on (EMA 5 s − EMA 30 s) / σ√30 | yes |
| **D reference-price displacement** | the analytic TWAP model of §4 | **no** |
| D recal | D with Platt recalibration | yes |
| E logistic regression | 32 features (§5), L2, clipped and standardised | yes |
| F gradient-boosted trees | histogram GBM (depth 3, 150 rounds, lr 0.05, min leaf 60); no sklearn on this machine | yes |
| M market | the new market's own pre-open Up mid at the prediction time | — |
| E + M | E plus logit(market mid) | yes |

**At T − 3 s** (coin flip: accuracy 50 %, Brier 0.250, log loss 0.693):

| model | TRAIN acc | TRAIN Brier | VAL acc | VAL Brier | walk-forward acc | walk-forward Brier | walk-forward log loss | walk-forward ECE | TEST acc | TEST Brier |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A momentum | 55.8 % | 0.2459 | 52.7 % | 0.2491 | 55.4 % | 0.2468 | 0.6870 | 5.0c | — | — |
| B returns | 55.0 % | 0.2466 | 53.3 % | 0.2510 | 55.8 % | 0.2470 | 0.6876 | 4.5c | — | — |
| C EMA | 56.4 % | 0.2445 | 51.7 % | 0.2536 | 56.0 % | 0.2472 | 0.6875 | 4.1c | — | — |
| **D analytic** | 55.9 % | 0.2451 | **58.3 %** | **0.2408** | **58.1 %** | **0.2418** | **0.6770** | **3.1c** | **58.9 %** | **0.2361** |
| D recal | 57.3 % | 0.2424 | 55.1 % | 0.2468 | 57.9 % | 0.2432 | 0.6796 | 4.8c | 59.0 % | 0.2379 |
| E logistic | 55.9 % | 0.2498 | 54.8 % | 0.2481 | 55.1 % | 0.2477 | 0.6896 | 4.4c | 57.3 % | 0.2418 |
| F boosted trees | 54.7 % | 0.2572 | 50.7 % | 0.2579 | 52.5 % | 0.2574 | 0.7105 | 7.5c | 54.7 % | 0.2457 |
| M market pre-open mid | 54.2 % | 0.2460 | 57.6 % | 0.2408 | 56.5 % | 0.2432 | 0.6794 | 3.8c | 59.0 % | 0.2393 |
| E + M | 55.9 % | 0.2499 | 54.8 % | 0.2478 | 55.0 % | 0.2476 | 0.6895 | 3.8c | — | — |

(TRAIN = cross-fitted out-of-fold. Full tables for all six prediction times: `data/replay/next_market/models.md`.)

- **The complex models are not better.** The analytic model D, with no fitted parameter, is best or tied-best at every prediction time in walk-forward and on TEST.
  - The logistic (E) and the trees (F) are worse than a coin flip in log loss out of sample.
  - The fitted models absorb TRAIN's down-drift (UP won 46 %) and pay for it on VALIDATION (UP won 54 %). Recalibration helps TRAIN and hurts VALIDATION.
- **The market's own pre-open mid is about as good as D.** On TEST at T − 3 s it has the same accuracy (59.0 % vs 58.9 %) and a slightly worse Brier (0.2393 vs 0.2361).
- **Does D know something the price does not?** Logistic of the outcome on logit(market mid) and logit(D), TRAIN + VALIDATION:

| comparison | books | n | z of the model coefficient |
|---|---|---:|---:|
| pre-open, T − 10 … T + 0 s | all | 3,222 each | +4.4 … +8.6 |
| pre-open, T − 10 … T + 0 s | Up spread ≤ 3c | 676–1,419 | −1.4 … +1.4 |
| after the open, T − 3 s model vs mid at +0 / +0.5 / +1 s | all | ~3,225 | +7.6 / +5.3 / +4.3 |
| same | Up spread ≤ 3c | 676 / 838 / 944 | +0.8 / +1.0 / +0.2 (+2.4 at +0.25 s, n 638) |
| after the open, mid at +2 / +3 / +5 / +10 s | all or tight | 1,847–3,317 | −1.5 … +0.8 |

The model's information shows up **only against wide books**, whose mid is not a price anyone can trade at. **Wherever the book is tight, the price already contains it.** The +2.4 at +0.25 s is one cell of 16, and the rules that traded there lost (§9). Full table: `data/replay/next_market/dev_information.md`.

**Convergence (TRAIN).** κ is the fraction of the model-vs-mid gap that the mid closes after the open, measured from the (wide) mid at the open: +0.25 s 0.06, +0.5 s 0.34, +1 s 0.42, +2 s 0.67, +3 s 0.77, +5 s 0.79, +30 s 0.77. Measured from a decision 3 s after the open, κ is 0.00–0.07 at every hold: nothing is left to converge.

## 7. Execution model

The project's `venue_lag` model (`backtest/spot_shock_replay.Execution`), re-implemented vectorised on a 50 ms as-of grid (`analyze_next_market_twap.py`, class `Sim`):

1. **Decision** at time t_d, on the last archived book at or before t_d. Entry gates, all at the ask: probability, spread, price range, the 5-share venue minimum, the order ≤ a fraction of the 2-tick ask depth, quote movement since the prediction, and the execution-aware edge.
2. **Order** leaves after `presend` and reaches the venue RTT/2 later. Each is drawn per order from the 41 measured samples × U(0.85, 1.15): one-way p50 181 ms, p90 379 ms.
3. **Venue book** at arrival T is read as the archived local book at T + 500 ms. On the 2026-09-23 tick recording this proxy reproduced the venue-truth P&L of 694 spot-shock orders (effective delay p50 681 ms).
4. **Fill.** The book within 2 ticks is split into levels: 25 % at the touch (the tick recording's median share) and 37.5 % at each of +1c and +2c.
   - FOK fills the whole order at the arrival VWAP within the limit, else nothing. A fill may be **below** the decision ask, if the ask has fallen.
   - FAK takes what is available within the limit.
5. **Exit** decided at fill + ack + hold (or at an event), as a FOK sell at the decision bid − 2c. It fills at the arrival bid VWAP, is retried every 2 s, and falls back to settlement past the grid (never needed).
6. **Fees:** rate · p · (1 − p) per share on every taker fill, at the window's own rate (0.07 on every market). A settlement hold pays one fee.

**The execution-aware edge** (`expected_net_edge`, the module's function; the simulator's vectorised copy is checked against it trade by trade):

    expected exit bid = mid + κ(e, h) · (p_side − mid) − half spread expected at e + h
    net if filled     = expected exit bid − (ask VWAP + 0.5c adverse) − fee(entry) − fee(exit)
    expected per order = P(fill) × net if filled

- κ(e, h) is the fraction of the model-vs-mid gap the mid closes h seconds after a decision at e s past the open.
- The expected half spread is by offset from the open.
- Both are estimated on TRAIN only. A settlement version uses p_side − VWAP − adverse − one fee.
- The edge is never `P(UP) − ask`: it is anchored on the book's *future* bid, because the book at the open is 30c wide and re-forms within 2–3 s.

## 8. Latency model

| component | value | source |
|---|---|---|
| spot feed (exchange → bot) | p50 141 ms, p90 275 ms | measured; already inside the archive (points are receive-time) |
| book (venue → bot) | p50 93 ms, p90 204 ms | measured; already inside the archive |
| decision / processing | p50 5 ms, p90 45 ms | measured |
| presend (decision → POST) | p50 48 ms, p90 75 ms | measured, drawn per order |
| venue RTT | p50 242 ms, p90 654 ms | measured, drawn per order (one way = RTT/2) |
| venue matching vs our view | +500 ms proxy | validated on venue-timestamped books |

The explicit delay grid (§9) instead reads the book at t_d + D exactly, with no proxy, for D = 0 … 1000 ms. Robustness scales every delay by 1.25, 1.5 and 2, and adds a random 0–300 ms or 0–1000 ms per order (§19).

The simulated timeline records the market start, the entry decision, the order submit, the order arrival, the fill (with the book read at arrival + proxy), the exit decision, the exit arrival and the exit fill. §20 has example records.

## 9. Entry analysis

Development tables are on TRAIN + VALIDATION (51.0 h, 611 epochs); TEST is read once, after the lock. All rows use $5 orders, the measured latency and c/share net of both fees.

**Experiments 1–5** (T − 3 s prediction, entry at the open, sell after 3 s):

| experiment | dev trades | dev c/share | dev t | TEST trades | TEST c/share |
|---|---:|---:|---:|---:|---:|
| 1 direction only | 2,059 | −12.87 | −50.3 | 575 | −11.80 |
| 2 + P(side) ≥ 0.60 | 656 | −11.58 | −25.0 | 214 | −8.96 |
| 3 + spread ≤ 3c | 113 | −3.71 | −6.7 | 36 | −4.05 |
| 4 + depth (order ≤ 50 % of the 2-tick depth, VWAP) | 110 | −3.82 | −6.7 | 36 | −4.05 |
| 5 + complete execution model (net edge ≥ 2c) | 22 | −2.50 | −2.2 | 1 | +0.91 |
| 5b same, evaluated for 3 s after the open | 51 | −2.89 | −3.6 | 7 | −1.43 |
| random side (baseline) | 2,187 | −15.32 | −55.8 | 614 | −13.86 |
| the market's pre-open side (baseline) | 2,173 | −15.74 | −55.6 | 589 | −13.23 |

- Direction is worth about 2.5c/share over a random side, which is the model's accuracy.
- The **spread filter is the only step that matters**: it removes the 30c-wide books and takes the loss from about −12c to about −4c.
- Probability, depth and the edge gate then shave volume, not losses.

**Entry order types** (P3: T − 3 s, 3 s window, edge ≥ 2c, sell after 3 s):

| type | dev fill % | dev trades | dev c/share | TEST fill % | TEST trades | TEST c/share |
|---|---:|---:|---:|---:|---:|---:|
| A immediate FOK at the ask | 45.3 | 72 | −3.33 | 30.0 | 15 | −1.51 |
| B FAK at the ask (partial fills) | 55.3 | 88 | −3.26 | 46.0 | 23 | −0.83 |
| C marketable limit, ask + 1c | 55.3 | 88 | −3.34 | 48.0 | 24 | −2.01 |
| C marketable limit, ask + 2c | 65.4 | 104 | −3.70 | 60.0 | 30 | −2.25 |
| C limit at the edge's maximum price (≤ +5c) | 56.0 | 89 | −3.93 | 40.0 | 20 | −1.48 |
| D skip if the ask rose > 0c since the prediction | 46.3 | 69 | −3.33 | 30.4 | 14 | −1.33 |
| D skip if the ask rose > 1c | 45.8 | 71 | −3.21 | — | — | — |

Crossing wider fills more and loses more, as `docs/replay.md` found for the fair-value engine. The extra fills are the ones the market was moving away from.

**When after the open** (direction only, 10 s window, spread ≤ 3c, the first quote that qualifies):

| decision after the open | dev trades | dev c/share | TEST trades | TEST c/share |
|---|---:|---:|---:|---:|
| 0–250 ms | 370 | −4.64 | 112 | −3.87 |
| 250–500 ms | 117 | −3.80 | 38 | −5.64 |
| 500 ms–1 s | 105 | −5.07 | 40 | −4.55 |
| 1–2 s | 462 | −4.94 | 98 | −5.11 |
| 2–3 s | 382 | −5.60 | 63 | −6.76 |
| 3–5 s | 129 | −7.55 | 46 | −5.43 |
| 5–10 s | 144 | −5.06 | 58 | −6.37 |

- The first quarter-second is the least bad. That is mostly BTC and ETH, whose books stay tight through the boundary. It is still −4c.
- With the edge gate (development), trades happen only in the first 2 s: −2.2c (0–250 ms, 36 trades), −5.2c (250–500 ms, 23), −2.6c (1–2 s, 10). After 2 s the gate never fires, because the market has converged.
- **Entering before the open** at the prediction time, into the pre-open book, loses too: −6.6c (T − 10 s, 16 trades), −1.4c (T − 1 s, 34), +1.6c (T + 0 s, 19). At T − 3 s the gate refuses every trade, because its exit would fall inside the empty-book window.

**Execution delay** (the book that fills the order is read at decision + D exactly, no venue proxy):

| delay | dev P3 trades | dev P3 c/share | TEST P3 trades | TEST P3 c/share | dev P1 c/share | TEST P1 c/share |
|---|---:|---:|---:|---:|---:|---:|
| 0 ms | 120 | −2.99 | 33 | −2.85 | −19.18 | −18.01 |
| 25 ms | 120 | −2.96 | 33 | −2.73 | — | — |
| 50 ms | 120 | −2.75 | 33 | −2.70 | — | — |
| 100 ms | 118 | −2.67 | 32 | −2.49 | −19.29 | −18.01 |
| 150 ms | 112 | −2.67 | 31 | −2.45 | — | — |
| 200 ms | 104 | −2.54 | 26 | −3.07 | — | — |
| 300 ms | 82 | −3.24 | 20 | −3.37 | −18.49 | −14.97 |
| 500 ms | 73 | −2.91 | 16 | −2.74 | −14.11 | −12.57 |
| 750 ms | 66 | −2.42 | 16 | −1.10 | — | — |
| 1000 ms | 67 | −2.81 | 15 | −1.60 | −12.63 | −10.98 |
| measured + 500 ms proxy | 72 | −3.33 | 15 | −1.51 | −12.87 | −11.80 |

- **Speed does not create the edge.** With the gate, every delay loses 1–3c.
- **Without the gate, faster is worse.** A 0 ms order lands in the emptiest book and pays about 0.71 on average, against 0.64 at the measured latency.

**Minimum execution-aware edge** (P3, development): ≥ 0.5c −3.98 (140 trades), 1c −4.14 (107), 1.5c −3.65 (84), 2c −3.33 (72), 3c −3.23 (46), 5c −3.35 (24).
- Priced and held as a settlement bet (edge ≥ 2c on p − ask − one fee): −0.88c on 510 trades (t −0.3).
- The threshold never crosses zero.

**Entry price bands.** Nothing trades under 0.30. Around the open the favoured side's ask is 0.40 or more: the model's side is the favourite, and the pulled book lifts both asks.
- Cumulative, literal rule (development): ask < 0.50 −4.38 (44 trades), < 0.60 −5.39 (407).
- Edge-gated: < 0.40 −6.79 (7), < 0.50 −5.43 (28), < 0.60 −5.10 (46).
- Cheap tokens are not better.

## 10. Exit analysis

P3 entry (development | TEST), c/share:

| exit | dev | TEST |
|---|---:|---:|
| A fixed 1 s | −3.43 | −3.66 |
| A fixed 2 s | −3.06 | −2.81 |
| A fixed 3 s | −3.33 | −1.51 |
| A fixed 4 s | −2.73 | −0.76 |
| A fixed 5 s | −2.71 | −2.67 |
| B momentum reversal (1 s spot move against us > 0.5σ; max 10 s / 30 s) | −3.53 / −3.72 | — / −3.46 |
| C target +1c / +2c / +3c / +5c / +7c / +10c (else 30 s) | −2.83 / −2.44 / −1.83 / −1.78 / −2.38 / −1.93 | — |
| D edge gone (model p_side ≤ bid + fee; max 10 s / 30 s) | −2.09 / −1.98 | — / −3.90 |
| E max hold 15 s / 30 s | −2.25 / −1.72 | −2.28 / +0.55 |
| settlement | −5.66 | −7.54 |

Literal P1 entry on TEST: 1 s −13.55, 2 s −12.24, 3 s −11.80, 4 s −11.40, momentum −13.02, edge gone −11.45, settlement −7.71.

**Stop loss** (P3, development; else sell at 3 s | 10 s):
- no stop: −3.33 | −3.53
- −1c: −5.00 | −5.00
- −2c: −4.42 | −3.66
- −3c: −4.19 | −3.62
- −5c: −3.72 | −3.21
- −7c: −3.64 | −3.08
- −10c: −3.79 | −3.81

Tight stops sell into the spread and pay the second fee sooner. Wide stops are no stop at all. **No exit, stop or target turns the entry positive**; exits only change how much of the entry's loss is realised.

## 11. Asset analysis

TEST, literal rule (P1) with the 3 s exit, each asset on its own:

| asset | opportunities | trades | fill % | win % | avg entry | avg exit | gross | fees | slippage | net | net c/share | max DD | Sharpe-like (per day) | model acc | model ECE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BNB | 147 | 142 | 96.6 | 2.1 | 0.671 | 0.544 | −$111.81 | $28.65 | −$108.99 | −$140.45 | −15.83 | $140.45 | −29.4 | 56.8 % | 6.3c |
| BTC | 127 | 86 | 67.7 | 20.9 | 0.571 | 0.569 | −$1.59 | $24.88 | −$14.24 | −$26.46 | −3.56 | $26.46 | −12.9 | 62.3 % | 14.3c |
| DOGE | 144 | 110 | 76.4 | 1.8 | 0.647 | 0.539 | −$83.93 | $25.26 | −$48.52 | −$109.19 | −14.04 | $109.19 | −28.0 | 60.3 % | 9.6c |
| ETH | 90 | 81 | 90.0 | 14.8 | 0.599 | 0.586 | −$7.42 | $20.00 | −$55.27 | −$27.42 | −4.58 | $27.69 | −14.8 | 58.2 % | 9.0c |
| HYPE | 120 | 63 | 52.5 | 3.2 | 0.755 | 0.533 | −$89.11 | $12.29 | −$4.66 | −$101.40 | −25.06 | $101.40 | −36.8 | 57.3 % | 7.7c |
| SOL | 102 | 25 | 24.5 | 12.0 | 0.538 | 0.506 | −$7.10 | $7.86 | −$5.14 | −$14.95 | −6.60 | $15.15 | −19.6 | 57.1 % | 10.7c |
| XRP | 76 | 68 | 89.5 | 13.2 | 0.653 | 0.600 | −$23.16 | $14.20 | −$53.22 | −$37.36 | −8.46 | $37.53 | −18.3 | 60.7 % | 9.6c |

Median holding time is 3.3–3.4 s for every asset. Development gives the same ranking: HYPE −31.5, BNB −13.8, DOGE −12.5, XRP −10.1, SOL −6.0, BTC −4.6, ETH −4.3c.

**What separates the assets is the book at the boundary, not the prediction** (median spread at the open, development: BTC 1c, SOL 4c, ETH 11c, XRP 39c, DOGE 40c, HYPE 64c, BNB 78c). The model is about as accurate on every asset (55–62 %). Where makers stay (BTC, ETH), the loss is the fees. Where they leave, the loss is the spread. Under the edge gate almost every trade is BTC or ETH (TEST: BTC 9 trades −0.2c, ETH 2, SOL 2, XRP 1, BNB 1).

## 12. UP vs DOWN

| | dev UP | dev DOWN | TEST UP | TEST DOWN |
|---|---:|---:|---:|---:|
| P1 literal (trades, c/share) | 1,072, −12.90 | 987, −12.83 | 309, −11.01 | 266, −12.73 |
| P3 edge-gated | 42, −4.62 | 30, −1.52 | 11, −3.48 | 4, +3.93 |

- The literal rule is symmetric.
- The gated rule's DOWN side was less bad in both periods, on 30 and 4 trades.
- The one DOWN-only rule that survived selection (L1, §18) shows what a DOWN tilt earns in this sample: regime, not prediction. UP won 46 % of TRAIN, 54 % of VALIDATION and 52 % of TEST.

## 13. Holding-time analysis

The executable table for TEST is at the top of this report. Development:

- **P1 (2,059 trades):**
  - 0.25 s −20.9, 0.5 s −19.2, 1 s −15.6, 2 s −13.4, 3 s −12.9, 5 s −12.9, 10 s −12.5, 30 s −12.1c/share;
  - settlement −10.5c (t −7.3).
- **P3 (72 trades):**
  - 0.25 s −4.1, 0.5 s −3.7, 1 s −3.4, 1.5 s −3.1, 2 s −3.1, 3 s −3.3, 4 s −2.7, 5 s −2.7, 7.5 s −2.9, 10 s −3.5, 15 s −2.3, 30 s −1.7c/share;
  - settlement −5.7c.

**The markout of every signal** (item 39; the model's side, the ask at the decision, the bid later; whether or not an order would have filled; `data/replay/next_market/signal_markouts_*.jsonl` has one row per signal with the bid path):

| hold | dev, decide at the open: gross / net (c) | TEST, at the open: gross / net | dev, decide 3 s after the open: gross / net | TEST, 3 s after: gross / net |
|---|---:|---:|---:|---:|
| +0.25 s | −31.4 / −34.2 | −26.2 / −29.2 | −2.9 / −6.2 | −3.8 / −7.1 |
| +0.5 s | −24.2 / −27.1 | −19.8 / −22.8 | −2.7 / −6.0 | −3.7 / −7.0 |
| +1 s | −22.5 / −25.4 | −19.3 / −22.3 | −2.6 / −5.9 | −3.6 / −6.9 |
| +2 s | −16.2 / −19.2 | −14.4 / −17.5 | −2.5 / −5.8 | −2.9 / −6.2 |
| +3 s | −13.9 / −16.9 | −12.8 / −15.8 | −2.5 / −5.8 | −2.7 / −5.9 |
| +5 s | −13.4 / −16.4 | −11.8 / −14.9 | −2.3 / −5.6 | −2.9 / −6.2 |
| +10 s | −12.9 / −15.9 | −11.5 / −14.5 | −2.2 / −5.4 | −3.1 / −6.3 |
| +30 s | −12.7 / −15.6 | −12.0 / −14.9 | −2.0 / −5.2 | −2.8 / −6.0 |

(3,222 development and 870 TEST signals; markout = future bid − entry ask; net subtracts both taker fees.)

- The curve rises until about 3 s, which is the book re-forming, then goes flat. **It never crosses zero**, not even gross of fees.
- Decided after the book has re-formed, the gross markout is about −2.5c at every horizon. That is roughly the spread: no directional drift is left.

## 14. Spread analysis

Maximum spread at the decision, on the side being bought (c/share, trades):

| max spread | dev P1 | TEST P1 | dev P3 |
|---|---:|---:|---:|
| 0.5c | no trades (the tick is 1c) | — | no trades |
| 1c | −4.74 (180) | −4.47 (48) | −2.99 (57) |
| 2c | −4.75 (299) | −4.18 (81) | −3.05 (62) |
| 3c | −4.67 (356) | −3.94 (102) | −3.36 (65) |
| 5c | −4.86 (414) | −4.15 (120) | −3.07 (71) |
| any | −12.87 (2,059) | −11.80 (575) | −3.33 (72) |

- The spread filter removes the pulled books and cuts the loss by two-thirds, but **even 1c books lose about 4.5c**: fees of about 3.3c, 1c of spread and some adverse selection.
- Around the open only 10–20 % of books are that tight (§2).
- A cheap-looking token is not an opportunity when the spread eats the edge. At the open the "cheap" side is the far side of a 30c book.

## 15. Depth analysis

The median depth within 2 ticks of the ask is 50–63 shares at the open, against 75–105 once the book has re-formed. The simulator walks three levels (25 % at the touch, 37.5 % at +1c and +2c). A FOK is killed when the order does not fit inside its limit.

| order | dev P1 fill % | dev P1 trades | dev P1 c/share | TEST P1 fill % | TEST P1 trades | TEST P1 c/share | dev P3 trades | dev P3 c/share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| $1 | — | 0 | — (below the 5-share minimum at any ask ≥ 0.20) | — | 0 | — | 0 | — |
| $2 | — | 0 | — (the same, asks ≥ 0.40) | — | 0 | — | 10 | −5.90 |
| $5 | 72.9 | 2,059 | −12.87 | 71.3 | 575 | −11.80 | 72 | −3.33 |
| $10 | 63.7 | 1,654 | −14.28 | 57.8 | 433 | −12.79 | 61 | −3.36 |
| $20 | 53.5 | 1,099 | −16.36 | 48.0 | 283 | −13.53 | 46 | −2.52 |
| $50 | 28.3 | 405 | −14.23 | 26.6 | 84 | −7.47 | 23 | −3.28 |

- Larger orders fill less and pay more per share (the VWAP walks up the book). A $50 order fills about 27 % of the time at the open.
- The $50 TEST row is less bad only because it fills in the few deep books.
- Requiring the order to be ≤ 50 % of the 2-tick depth (experiment 4) changed nothing: at $5 the depth is rarely the binding limit.

## 16. Slippage analysis

- **Entry.** With a FOK limit at the decision ask, the fill price is at or below that ask by construction. Measured slippage on fills is therefore **negative**: −$1,108 over 2,059 development fills of P1, about −7c/share including the exit leg.
- **Why that is bad news.** The ask falls while the order is in flight exactly when the market is moving against the side being bought. That is adverse selection:
  - 77–80 % of P1 fills see the bid more than 2c under the entry price 1 s after the fill;
  - 17–19 % of P1 orders and 45–54 % of P3 orders are killed because the ask rose, i.e. the market moved the model's way before the order arrived.
- **Wider limits** (ask + 1c / + 2c) turn some of those kills into fills at worse prices, and lose more (§9).
- **Exit.** 26–31 % of exits sell below the exit-decision bid (FOK floor bid − 2c).
- **Cost of worse execution.** Adding 1c to every entry or exit costs exactly 1c/share (§19).

## 17. Walk-forward results

Chronological folds of TRAIN + VALIDATION (about 10 h each), then TEST, net c/share (trades):

| rule | fold 1 | fold 2 | fold 3 | fold 4 | fold 5 | TEST |
|---|---:|---:|---:|---:|---:|---:|
| P1 literal | −11.98 (398) | −11.91 (378) | −15.30 (377) | −11.12 (418) | −13.91 (465) | −11.80 (575) |
| P3 edge-gated | +0.22 (24) | −6.54 (13) | −6.45 (9) | −1.57 (11) | −4.70 (9) | −1.51 (15) |
| L1 settlement survivor | −3.10 (11) | +28.72 (13) | +6.25 (18) | +28.21 (17) | +7.59 (16) | +5.42 (13) |

The prediction models themselves were also walked forward (§6): the analytic model is the most stable, and every fitted model loses ground out of fold.

## 18. Out-of-sample results

**Selection protocol** (TRAIN → VALIDATION → lock), run on the 241,920 configuration × exit rows of the sweep:
- the grid: 2 models × 6 prediction times × 8 entry windows × 7 edge thresholds × 5 spread caps × 6 max asks, × 12 exits;
- TRAIN: at least 30 trades, c/share > 0, t ≥ 1.5, and the median of its one-step neighbours positive;
- VALIDATION: at least 10 trades and c/share > 0.

| | count |
|---|---:|
| configuration × exit rows | 241,920 |
| with ≥ 30 TRAIN trades | 91,344 |
| positive on TRAIN | 2,960 (3.2 %) |
| positive on VALIDATION | 1,191 |
| positive on both | 228 (0.25 %) |
| median TRAIN / VALIDATION c/share | −3.93 / −5.88 |
| correlation of TRAIN and VALIDATION c/share across configurations | 0.13 |
| pass the TRAIN filter (t ≥ 1.5, stable neighbourhood) | 31 |
| … and positive on VALIDATION | 15 (48 %, what chance gives rules with no edge) |
| short-hold (non-settlement) rows among the 15 | **0** |

- **Short holds (the hypothesis).** 83,732 short-hold rows had ≥ 30 TRAIN trades. 1,231 were positive on TRAIN, none passed, and the median was −3.95c.
  - The best of them was L6: T − 2 s, 5 s window, spread ≤ 2c, ask ≤ 0.40, sell after 30 s. It made +5.3c on TRAIN (39 trades, t 1.05), then −5.8c on VALIDATION, −3.8c on TEST and −7.1c on its one FORWARD trade.
- **The 15 survivors are one rule family:** buy the model's side at the open only when the market still offers it at ≤ 0.50 with a spread of 2–3c, and hold to settlement.
  - The top one (L1) uses the recalibrated model at T − 1 s: TRAIN +15.2c (58 trades, t 2.4), VALIDATION +12.6c (18, t 1.0).
  - **55 of its 57 TRAIN trades, and all 31 VALIDATION and TEST trades, are DOWN.** 51 of 57 on TRAIN are BTC, and all 13 on TEST.
  - The recalibration learned TRAIN's down-drift, and BTC is the only book tight enough at the open. The rule therefore bets that BTC finishes DOWN whenever the market prices DOWN at ≤ 0.50.
  - TEST: +5.4c on 13 trades (t 0.4). FORWARD: no trades.
  - The analytic-model version (L3/L4: T − 1 s, entering before the open) was the best on TRAIN (+18.1c, 44 trades) but lost on TEST (−8.9c and −5.0c).
- **Locking.** Locked at 14:43:52 UTC and amended before TEST was read (an identical rule dropped, L6 added). An earlier lock at 14:42:09 was void: the sweep file had written the win *rate* into the scan-window column, because of a name collision (`win`). The file was repaired from its deterministic row order, and all 483,840 rows were verified. `results/next_market_twap_locked.json` records all of this.

## 19. Robustness tests

TEST, net c/share (trades). The fill-rate rows drop fills at random; "best 10 %" removes the best fills.

| degradation | P1 literal | P3 edge-gated | L1 survivor |
|---|---:|---:|---:|
| base | −11.80 (575) | −1.51 (15) | +5.42 (13) |
| latency × 1.25 | −11.45 | −1.33 | +5.57 |
| latency × 1.5 | −11.09 | −1.19 | +10.53 |
| latency × 2 | −9.94 | −1.67 | +4.88 |
| random extra 0–300 ms per order | −11.72 | −0.81 | +5.57 |
| random extra 0–1000 ms per order | −10.36 | −0.76 | **−2.34** |
| fill rate −25 % | −11.77 | −1.40 | **−2.74** |
| fill rate −50 % | −11.99 | −0.87 | **−14.40** |
| best 10 % of fills never happen | −13.42 | −2.75 | +1.35 |
| spread +1c | −12.80 | −2.51 | +4.92 |
| entry slippage +1c | −12.78 | −2.50 | +4.41 |
| exit 1c worse | −12.81 | −2.51 | +5.42 (settles) |
| all at once (× 1.5, +1c / +1c, −25 % fills) | −13.07 | −3.11 | +1.18 |
| prediction noise, logit sd 0.25 / 0.5 / 1.0 | −11.65 / −12.26 / −12.30 | −4.22 / −6.25 / −7.99 | +18.77 / **−5.36** / +3.29 |

- The hypothesis rules are negative under every degradation.
- **More latency makes the literal rule less bad**, because the book keeps re-forming.
- The survivor's sign depends on which 13 trades happen to fill: it flips negative when a quarter of them are dropped at random.

## 20. Failure cases

Every order and exit in the replay runs through the failure paths below. TEST counts:

| event | P1 (806 orders) | P3 (50 orders) |
|---|---:|---:|
| filled | 575 (71.3 %) | 15 (30.0 %) |
| no fill: the ask moved above the limit in flight (adverse move) | 136 (16.9 %) | 27 (54.0 %) |
| no fill: not enough depth within the limit | 95 (11.8 %) | 8 (16.0 %) |
| no fill: no ask on arrival (quote disappeared) | 0 | 0 |
| the spread was wider on arrival than at the decision | 106 (13.2 %) | 20 (40.0 %) |
| decision on a book point > 1 s old (stale quote) | 0 | 0 |
| exit sold below the exit-decision bid | 162 (28.2 % of fills) | 1 (6.7 %) |
| exit with no bid available | 0 | 0 |
| exit never sold, fell back to settlement | 0 | 0 |
| bid > 2c under the entry 1 s after the fill (moved against us at once) | 443 (77.0 % of fills) | 4 (26.7 %) |
| losing trades | 526 (91.5 %) | 10 (66.7 %) |

Also modelled:
- partial fills (the FAK entry, §9);
- a delayed spot feed (every feature is computed on the spot as received, 140–300 ms late);
- delayed book, order acknowledgement and fill (the measured latency draws and the 500 ms venue proxy);
- rejected orders below the 5-share venue minimum ($1 and $2 orders, §15);
- market-data gaps: 51 boundaries with a hole in the strike window are refused (`boundary_features` returns None);
- missing reference data: no Chainlink feed is recorded; the composite's basis to it is the ~1.7 % settlement disagreement of §2;
- extreme volatility and sudden reversals (§8 regimes, and the robustness rows).

**Example records.** These come from the replay, built by the strategy module's own `evaluate_entry`, `format_prediction` and `format_exit`. The module's scalar edge is asserted equal to the simulator's vectorised edge on every example. The timeline's fill is matched against the venue book read at arrival + 500 ms.

```
NEXT_MARKET_PREDICTION
asset=BTC
market_start=1790301900000
prediction_time=1790301897000 (-3.0s)
reference_price=84852.9
predicted_start=84794.5
predicted_twap=84852.9
predicted_displacement=+58.4331
p_up=0.8138
p_down=0.1862
confidence=0.6276
UP: bid=0.73 ask=0.74 spread=0.01 depth=62
DOWN: bid=0.26 ask=0.27 spread=0.01 depth=248
decision=UP (ENTER)
expected_edge_before_costs=+0.0504
expected_edge_after_costs=+0.0205
market_start=1790301900000
entry_decision=1790301900250 (+0.25s)
order_submit=1790301900301
order_arrival=1790301900476 (+0.48s)
fill=1790301900476 (6.75 shares)
entry_price=0.7200

NEXT_MARKET_EXIT
asset=BTC
side=UP
hold_time=3.35s
entry_ask=0.74
current_bid=0.72
markout=+0.0000
fees=0.0282/share
slippage=-0.0200 entry, +0.0000 exit
net_pnl=-0.1905 USDC
exit_reason=time 3s

what the bid did (item 39):
  +0.25s bid=0.71   +0.5s bid=0.71   +1s bid=0.71   +2s bid=0.69
  +3s bid=0.72      +5s bid=0.73     +10s bid=0.72  +30s bid=0.72
```

The trade shows the whole problem in miniature:
- the model was right (UP, p 0.81) and the book was tight;
- the ask even fell 2c before the order landed;
- the bid then went nowhere, and two fees made a flat markout into −2.8c/share.

## 21. Parameter sensitivity

One parameter at a time around P3 (TRAIN | VALIDATION c/share, from the sweep):

| parameter | values → TRAIN c/share | VALIDATION c/share |
|---|---|---|
| prediction time | −10 s −5.8, −5 s −5.7, **−3 s −3.1**, −2 s −2.2, −1 s −1.7, 0 s +0.7 | −7.0, −8.7, **−4.8**, −5.8, −3.6, −5.7 |
| window after the open | 0 s −2.7, 0.5 s −3.2, 1 s −3.3, **2–10 s −3.1** | −2.4, −3.2, −4.4, −4.8 |
| min edge | none −12.0, 0.5c −3.4, 1c −3.5, 1.5c −3.1, **2c −3.1**, 3c −3.3, 5c −3.8 | −12.8, −7.1, −8.5, −6.8, −4.8, −3.0, −1.0 |
| max spread | 1c −2.9, 2c −2.9, 3c −3.3, 5c −2.8, **any −3.1** | −3.3, −3.9, −3.8, −4.8, −4.8 |
| max ask | 0.40 −5.1, 0.50 −4.6, 0.60 −3.6, 0.70 −3.4, **0.99 −3.1** | −9.5, −7.7, −7.7, −5.2, −4.8 |
| exit | 1 s −3.1, 2 s −2.9, **3 s −3.1**, 4 s −2.5, 5 s −2.4, 10 s −3.1, 30 s −2.3, settle −8.9, +3c target −1.7, −3c stop −3.3, momentum −3.8, edge gone −2.2 | −5.3, −4.2, −4.8, −4.5, −4.4, −6.4, +1.8, +14.3, −2.6, −5.3, −3.4, −0.7 |

- **A broad, flat, negative region.** No neighbour of the hypothesis is positive on both TRAIN and VALIDATION.
- The holding-time row is the item 33 table: 1 s −3.1, 2 s −2.9, 3 s −3.1, 4 s −2.5, 5 s −2.4 on TRAIN, with no peak.
- **Around the survivor L1 the picture is the opposite: isolated peaks, the overfitting signature.**
  - max ask 0.50 → +15.2 / +12.6, but 0.60 → +1.5 / +3.0;
  - window 0 s → +15.2 / +12.6, but 0.5 s → +8.9 / −2.2 and 1 s → +8.1 / −8.5;
  - settlement → +15.2 / +12.6, while the short exits of the same entry → −3.0 to −5.3 on TRAIN and −5.5 to +1.1 on VALIDATION.
  - Full tables: `data/replay/next_market/sensitivity_p3.md` and `final_report.md`.

## 22. Recommended research configuration

**None for paper or shadow trading.** The evidence does not justify running this strategy anywhere, even on paper: every version of the hypothesis loses on held-out data.

The module's defaults keep the pre-registered research configuration for replay only:
- predict at T − 3 s, enter only at the open;
- net edge ≥ 2c, spread ≤ 3c, ask ≤ 0.60;
- $5 orders, sell after 3 s.

`TPB_NEXT_MARKET_TWAP_ENABLED` stays false, and there is no real-money path.

**What would change the verdict** (re-run the deterministic pipeline if any of these happens):
- **A large cut in the taker fee.** The fees alone (~3.3c/share per round trip) are larger than every gross markout measured here.
- **Market makers keeping quotes through the boundary** on more assets than BTC. Then the pre-open information would be buyable at a tight spread. But on tight books today the price already contains it (§6).
- **More data on the settlement pocket (L1).** It trades about once per hour, on BTC DOWN only. Weeks of archive would be needed to tell a +5c edge from zero and from the market's direction. It can be re-tested on future archive data with `analyze_next_market_twap.py final`, with no process running.

## 23. Why the strategy has no evidence of an edge

The chain the hypothesis needs is **prediction → a mispriced token you can buy → a repricing larger than the costs, within seconds**. Every link except the first breaks.

1. **The prediction is real, but mostly mechanical, and the market knows the mechanism.** At T − 3 s the strike is 95 % observed. P(UP) is largely the gap between spot and a 60 s average. The market maker's pre-open quotes price the same thing: at T − 10 s and T − 5 s the pre-open mid is as good as the model (walk-forward Brier 0.2451 vs 0.2441, 0.2437 vs 0.2424). On pre-open books with spread ≤ 3c, the model adds nothing to the mid at any prediction time (z −1.4 to +1.1).
2. **The model's extra information exists only while the book is too wide to trade.** From T − 1 s to T + 0.25 s, market makers pull their quotes. The median spread is 30c in the archive, and 20–30c on the venue-timestamped tick recording. The "market price" there is the middle of an empty book, and the model beats it easily (z 7.6). The ask the strategy would have to pay averages 0.65 on the favoured side.
3. **The repricing is fast and ends before the book is tradable.** The mid closes 34 % of the model-vs-mid gap by +0.5 s, 67 % by +2 s and 77 % by +3 s. By +2 s the model adds nothing to the mid, and the spread is back to 2c. After that, κ ≈ 0: no further drift toward the model for a buyer to collect.
4. **What is left is smaller than the costs.** A round trip pays two taker fees, about 3.3–3.5c/share at these prices, plus the spread. Buying the favoured side after the book re-forms has a gross markout of −2.5c at every hold (roughly the spread) and a net markout of −5.8c.
5. **The fills that happen are the adverse ones.** Under the edge gate, 45–55 % of FOK orders are killed because the ask moved up in flight. The orders that fill are the ones the market moved against: 21–29 % of fills see the bid 2c under entry within a second. Removing latency does not rescue it: at 0 ms the gated rule still loses about 3c/share.
6. **Where the model sees the most edge, it is the model that is wrong.** Among P3's signals the model sees ~13c of settlement edge (p_side − ask), yet held to settlement they return about −1c (t −0.3). This is the pattern the 60 s study found: the model disagrees with a better-informed market.

The only way to own the information in (2) would be to already hold the position when the book empties. That means a pre-open fill, and pre-open books price the same signal (1). The sweep's pre-open entries (`win = −1`) lose like everything else.

## Limitations

- **Resolution.** The archive samples every ~224 ms. The 0.25 s horizons are one archive step, and the 0–250 ms decision bucket is one or two points. The venue-timestamped tick recording confirms the boundary picture at event resolution, but it covers only 90 minutes (126 markets).
- **Depth.** Only the depth within 2 ticks is recorded. The per-level split (25 / 37.5 / 37.5 %) is the tick recording's median, and orders above the 2-tick depth are treated as unfillable. $20–$50 orders are therefore estimated conservatively, and $5 orders mostly fill at the touch.
- **Chainlink.** No Chainlink feed is recorded here. Our composite agrees with the venue on 98.3 % of settlements. The remaining basis (about 1–3 bps) adds noise to every probability, but it cannot explain the result: the model's problem is not calibration against settlement, it is that the market prices the same information.
- **One execution model.** The 500 ms venue proxy was validated on spot-shock orders, not on market opens. The explicit delay grid (0–1000 ms, no proxy) brackets it, and no delay is profitable.
- **Sample.** 2.8 days, 764 epochs, 4,432 markets. That is enough to reject the literal strategy (t −50) and the broad rules. It cannot resolve an edge of ±1c/share on the few dozen trades a strict gate allows. But no strict gate is positive in both TRAIN and VALIDATION with a stable neighbourhood either (§21).
- **Taker only, as specified.** Resting (maker) orders around the boundary were not tested. Market makers widen to 20–30c exactly because the informed flow arrives then, and a resting order would be filled by that flow.

## Reproduce

    python scripts/research_next_market_twap.py cache --last-open 1790342400   # 2 min; compact arrays from data/charts
    python scripts/research_next_market_twap.py verify                         # the settlement check (§2)
    python scripts/research_next_market_twap.py build                          # 5 min; features at 6 prediction times
    python scripts/analyze_next_market_twap.py grid                            # 50 ms as-of book/spot grid
    python scripts/analyze_next_market_twap.py diag                            # TRAIN-only diagnostics
    python scripts/analyze_next_market_twap.py models                          # baselines A-F, walk-forward
    python scripts/analyze_next_market_twap.py report                          # development tables (TRAIN + VALIDATION)
    python scripts/analyze_next_market_twap.py sweep                           # 20,160 configurations x 12 exits (~1.5 h)
    python scripts/analyze_next_market_twap.py lock                            # selection protocol -> results/next_market_twap_locked.json
    python scripts/analyze_next_market_twap.py final                           # TEST (+ FORWARD), read once

Every step is deterministic (seeded latency draws, common random numbers across configurations). Run research jobs under `nice -n 19`; never renice a bot (memory: bot CPU priority).

## Files

| file | what |
|---|---|
| `src/troll_poly_bot/strategy/next_market_twap.py` | the rule: config (`TPB_NEXT_MARKET_TWAP_*`, disabled by default, `REAL_MONEY = False`), causal boundary features, prediction, VWAP, execution-aware edge, entry gates, exits, `NEXT_MARKET_PREDICTION` / `NEXT_MARKET_EXIT` records. Not wired into `live.py` |
| `scripts/research_next_market_twap.py` | cache, settlement check, boundary dataset |
| `scripts/analyze_next_market_twap.py` | grid, diagnostics, models A–F, simulator, sweep, lock, final |
| `tests/test_next_market_twap.py` | 28 tests: disabled by default, no look-ahead (future rewritten at every offset), strike estimate, displacement variance, ask-based edge, entry timing and gates, VWAP, exits, log records, research fills |
| `.env.example` | the `TPB_NEXT_MARKET_TWAP_*` block, documented as research only |
| `data/replay/next_market/` | cache, dataset, grid, predictions, `diag_train.md`, `models.md`, `dev_report.md`, `dev_information.md`, `dev_prediction_time.md`, `final_report.md`, signal markouts (`signal_markouts_*.jsonl`, item 39) |
| `results/next_market_sweep.csv`, `results/next_market_twap_locked.json` | the sweep and the lock |
