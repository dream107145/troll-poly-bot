# PREOPEN_MAKER (PBot-6 pattern): replay on the BTC 5-minute archive (2026-10-06)

1992 BTC windows, 09-22 15:40 -> 10-01 21:20 UTC, 10 UTC days; every window has the 60 s before its open. Bids of 10 shares on BOTH tokens, placed P s before the open, pulled at the open. Fill rules: see the script header; **strict** = the paper venue's rule (the ask trades strictly through the bid), **persist** = strict for >= 400 ms, **haircut** = strict with half the fills refused, **touch** = an optimistic upper bound. Maker fills pay no fee; a bid that crosses the ask at placement executes as a taker at the ask with the taker fee. Partial fills cannot be reconstructed (no trade sizes in the archive). No decision uses anything after the placement timestamp.

## 0. Executive summary

**The pure pre-open maker strategy loses on this data, at every bid price, every placement time and under every fill rule.**
At the reported parameters (bid 0.47, 10 shares a side, T-45 s, pulled at the open) it loses 1,077 USDC over 1,989 markets
(-0.27 per order, -5.6c per filled share), 9 of 10 days negative, max drawdown 1,108. The walk-forward (parameters chosen on
the previous five days) loses 505 over its 1,109 out-of-sample markets.

Where the money goes: **one-sided fills are adverse selection, not information.** A bid at 0.47 fills when the token is
already falling through it: the filled token's mid goes 0.489 (5 s before) -> 0.445 (at the fill) -> 0.41 (at the open) ->
0.40 (one minute later), and it wins 38-39 % of the time against the 48-50 % of a random purchase of the same token at
the same price. One-sided legs lose 1,213; **the pairs are the only profitable part**: 240 markets (12 %) filled both
sides at a combined 0.937 and earned +136 guaranteed. Pairs are rare because the two bids fill together only when both
tokens pass through 0.47, i.e. when the book is flat around 0.50 at the open.

Causal pre-open information exists but is weak: the strike is the trailing 60 s TWAP at the open (previous TWAP vs next
strike differ by 0.5 bps median), and "spot above its trailing TWAP at T-45" predicts the outcome 52.6% of the time
(n 1609); it does not rescue a strategy that is adversely selected on every fill.

## 11. Comparison with PBot-6

| reported characteristic | reproduced? |
|---|---|
| ~90 % of buys pre-open, GTC bids on both sides, ~10 shares, ~$14 per market | yes: the bot does exactly this; capital locked peaks at 28 USDC, 100 USDC is ample |
| one-sided fills at ~0.47 winning ~63 % | **no**: 38-39 % on 1,449 one-sided fills, below the 48-50 % of a random 0.47 purchase |
| pairs at ~0.97 | different: pairs cost 0.937 here (both legs at the bid) but occur in only 12 % of markets; his 0.97 suggests legs filled at different prices, i.e. bids re-placed or asymmetric |
| profitable days with large losing days | not reproduced: 9 of 10 days negative here |
| mostly maker fills | partly: 317 of the 0.47 bids would have crossed the ask at placement and executed as takers |
| ETH and 15-minute markets | not measured (BTC 5-minute archive only) |
| queue position, partial fills, his exact timing of re-placement | cannot be measured from a 200 ms best-bid/ask archive |

**Verdict:** the data does not support the claimed edge of the pure pre-open maker. Whatever PBot-6 does, it is not
"rest at 0.47 on both sides and hold": either his bids are placed and re-placed on information inside the last seconds,
or his profit came from a period or a venue state this archive does not cover. The strategy should NOT proceed to
micro-live. The paper bot (tpb-pom-paper) runs so that live pre-open books can be compared with the archive; it is not a
candidate for real money on this evidence.

## 1. Bid price (placement T-45 s, strict fills)

| bid | fill rate / order | markets with a fill | one-sided | pairs | one-sided win | implied at fill | avg pair cost | EV / share | EV / order | P&L | max DD | worst day | crossed |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.44 | 32.7% | 63% | 61% | 2% | 37% | 0.414 | 0.879 | -0.0623 | -0.204 | -810.1 | 904.6 | -185.0 | 61 |
| 0.45 | 37.4% | 71% | 67% | 4% | 38% | 0.424 | 0.899 | -0.0599 | -0.224 | -892.3 | 961.3 | -222.0 | 106 |
| 0.46 | 42.6% | 78% | 71% | 7% | 39% | 0.434 | 0.918 | -0.0561 | -0.239 | -950.4 | 992.5 | -215.1 | 176 |
| 0.47 | 48.5% | 85% | 73% | 12% | 39% | 0.445 | 0.937 | -0.0558 | -0.271 | -1076.7 | 1108.1 | -195.4 | 317 |
| 0.48 | 55.1% | 91% | 73% | 19% | 39% | 0.455 | 0.956 | -0.0510 | -0.281 | -1117.6 | 1170.5 | -197.0 | 558 |
| 0.49 | 64.2% | 97% | 65% | 32% | 40% | 0.468 | 0.974 | -0.0402 | -0.258 | -1026.9 | 1071.5 | -198.4 | 1028 |
| 0.50 | 76.0% | 100% | 48% | 52% | 41% | 0.480 | 0.991 | -0.0311 | -0.236 | -939.6 | 971.6 | -194.6 | 1972 |

## 2. Placement time (bid 0.47, strict fills)

| placement | fill rate / order | one-sided | pairs | one-sided win | implied | EV / order | P&L | max DD |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| T-60 | 48.7% | 73% | 12% | 39% | 0.446 | -0.266 | -1059.1 | 1098.4 |
| T-45 | 48.5% | 73% | 12% | 39% | 0.445 | -0.271 | -1076.7 | 1108.1 |
| T-30 | 47.7% | 74% | 11% | 38% | 0.442 | -0.279 | -1110.3 | 1166.0 |
| T-20 | 45.4% | 76% | 7% | 39% | 0.434 | -0.246 | -977.1 | 1024.4 |
| T-10 | 41.7% | 77% | 3% | 38% | 0.420 | -0.245 | -976.5 | 1047.8 |

## 3. Fill model sensitivity (bid 0.47, T-45)

| rule | fill rate / order | one-sided | pairs | one-sided win | P&L | pair net | one-sided P&L |
|---|---:|---:|---:|---:|---:|---:|---:|
| strict | 48.5% | 73% | 12% | 39% | -1076.7 | +136.5 | -1213.1 |
| persist | 48.0% | 73% | 12% | 39% | -1058.0 | +131.4 | -1189.4 |
| touch | 53.8% | 74% | 17% | 39% | -1008.4 | +194.8 | -1203.2 |
| haircut | 27.5% | 46% | 5% | 41% | -526.9 | +51.1 | -577.9 |

## 4. One-sided fills (bid 0.47, T-45, strict): does the fill carry information?

| side | n | won | price | token mid at fill | mid at open | mid +30 s | mid +60 s | P&L | EV / share at the fill price | EV if it were a random 0.47 purchase |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| UP only | 666 | 38% | 0.468 | 0.444 | 0.412 | 0.399 | 0.400 | -580.7 | -0.085 | +0.008 (win 48%, n 836) |
| DOWN only | 783 | 39% | 0.467 | 0.446 | 0.411 | 0.399 | 0.394 | -632.4 | -0.077 | +0.032 (win 50%, n 1078) |

Reading: the 'mid at fill' column is what the market thought the token was worth when our bid filled; if the win rate sits far above it the fill carries information, if it sits at it the fill is a random liquidity event, if below it is adverse selection.

## 5. Pairs (bid 0.47, T-45, strict)

pairs 240 of 1989 markets; combined cost per share mean 0.937, median 0.940, min 0.880, max 0.940; guaranteed gross 2400.0, fees 14.43, net guaranteed +136.47; capital locked per pair 9.37 USDC for about 420 s.

| pair cost threshold | pairs at or under | net guaranteed |
|---:|---:|---:|
| 0.94 | 240 | +136.47 |
| 0.95 | 240 | +136.47 |
| 0.96 | 240 | +136.47 |
| 0.97 | 240 | +136.47 |
| 0.98 | 240 | +136.47 |
| 0.99 | 240 | +136.47 |
| 1.00 | 240 | +136.47 |

83 of the 240 pairs include a leg that crossed the ask at placement (a taker fill at the ask, fee charged).

## 6. Fill selection (bid 0.47, T-45, strict): filled vs not filled

| group | n | spot 5 s before fill (bps) | spot 5 s after (bps) | token mid before -> at -> after | ask depth | spread | spot - strike at fill (bps) | prev market same direction | s before open |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|
| filled, won | 800 | +0.08 | +0.01 | 0.489 -> 0.446 -> 0.441 | 672 | 0.014 | -0.9 | 51% | 25 |
| filled, lost | 1129 | -0.04 | +0.07 | 0.488 -> 0.445 -> 0.430 | 693 | 0.013 | -0.8 | 50% | 24 |

'spot - strike at fill' is signed toward the filled side (positive = the spot already favoured the side we bought); the strike itself is known only at the open, so this row is diagnostic, not a usable signal.

## 7. Previous market, strike and a causal pre-open lean

Consecutive markets 1612: |previous close - next strike| median 2.08 bps, |previous 60 s TWAP - next strike| median 0.53 bps (the strike IS the trailing 60 s TWAP at the open, so the previous market's closing TWAP equals the next strike up to the last 60 s of drift). A causal lean at T-45 (spot above its trailing 60 s TWAP -> UP) predicts the outcome 52.6% of the time (n 1609).

## 8. Walk-forward (strict fills): parameters chosen on the previous 5 days, applied to the next day

| day | chosen bid | chosen placement | train P&L / market | OOS markets | OOS P&L |
|---|---:|---:|---:|---:|---:|
| 09-27 | 0.44 | T-10 | -0.1827 | 243 | -60.25 |
| 09-28 | 0.44 | T-10 | -0.2221 | 221 | -44.24 |
| 09-29 | 0.44 | T-10 | -0.2909 | 224 | -179.74 |
| 09-30 | 0.44 | T-10 | -0.4365 | 225 | -124.98 |
| 10-01 | 0.44 | T-10 | -0.4987 | 196 | -95.76 |

**Walk-forward out-of-sample total: -504.97 USDC over 1109 markets.**

## 9. Capital (bid 0.47, T-45, strict)

Max capital locked at once (resting bids + open positions until settlement + 2 min): 28.2 USDC; max unmatched one-sided shares outstanding: 20. Daily turnover at 10 shares: about 1872 USDC of bids a day, 907 USDC filled a day. A 100 USDC bankroll covers the strategy at this size; the bottleneck is the number of markets open at once, not the cash.

## 10. Drawdown and concentration (bid 0.47, T-45, strict)

Max drawdown 1108.06; worst day -195.37, best day +20.88, days positive 10%; longest losing streak 9 markets; worst market -4.87, best market +6.04; share of the total made by the best 2 markets: -.

## 12. Paper run, 10-06 22:14 -> 10-07 11:10 UTC (tpb-pom-paper, 100 USDC)

151 markets bid, 146 fills (2 partial), 126 settled: UP-only 44, DOWN-only 62, BOTH 20. One-sided fills won 38 of 104
(36.5 %); the 20 pairs cost ~0.94 and paid +0.6 each (+12). Total -86 to -97 (ledger vs engine, the gap being the two
markets still unsettled); balance 8.28, the last 20 markets skipped for INSUFFICIENT_CASH. The replay predicted
39 % one-sided wins, 12 % pairs and -0.54 per market: the live paper books reproduce the archive. Flat-book gates
(both asks >= 0.50 at placement), lower bids and later placement cut the loss per market to -0.34..-0.47 but never
to zero on the archive. Bot stopped 10-07 12:37 UTC: out of paper cash and nothing left to learn from it.
