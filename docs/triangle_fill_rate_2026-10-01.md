# Triangle paper bot: why the fill rate was 16 %, and the fix (2026-10-01, ~20:00 UTC)

Scripts: `scripts/triangle_audit/fill_anatomy.py` (the paper bot's own orders against their 200 ms archives),
`scripts/triangle_audit/fill_calibration.py` (paper fill rules against REAL Polymarket orders). Paper only.

## 1. The run being diagnosed (17:37–19:31 UTC, scenario `realistic`, sources binance,bybit,coinbase)

104 entry orders, 9 fills. 56 orders sit in windows with an archive:

| | orders | share |
|---|---:|---:|
| filled | 9 | 16 % |
| killed `quote_pulled` (the ask we matched left the book within 2 s) | 29 | 52 % |
| killed `fok_unfillable` (no 5 shares inside the limit at arrival) | 18 | 32 % |

The killed orders were the winners: had they filled at their limit, 94 % would have won (+0.83 USDC per
order) against 78 % (+0.23 per fill) for the fills. No `quote_pulled` kill had the ask over the limit at
+250 ms; 12 of 29 never had it over the limit within 2 s. The rule killed them because the ask ticked up
inside our +3c limit after the fill, which is what a winning momentum buy looks like.

By the spot source that completed the move: Coinbase 14 % filled (spot age 85 ms), Bybit 4 % (108 ms),
Binance 0 % (167 ms). The composite is the median of the fresh sources; with three it moves on the second
quote, usually Bybit.

## 2. Which paper rule matches the venue (real spot-lag orders since the 09-29 order-path work)

Each real order replayed on its archive: arrival = decision + 250 ms; filled if the ask is inside the limit
at arrival and again S ms later.

| survival S | slow feeds (143 orders, real 10 %) predicted / agree | fast feeds (50 orders, real 50 %) predicted / agree |
|---:|---:|---:|
| 0 | 57 % / 53 % | 64 % / 86 % |
| 400 ms | 13 % / 92 % | 46 % / 92 % |
| **1 s** | **11 % / 97 %** | **44 % / 90 %** |
| 2 s | 11 % / 97 % | 40 % / 86 % |
| `realistic` as coded (2 s, every matched level) | 6 % / 96 % | 36 % / 82 % |

Real limit offsets 0–4c filled the same (17–28 % since 09-29 19:00): price is not the lever, speed is.
Real BTC fill rate by feed set: binance,bybit,coinbase 10 % (spot age 164 ms); coinbase,deribit,kraken,bitstamp
47 % (120 ms); + binance 75 % (37 ms, 4 orders). Those spot-lag fills still lost money under spot-lag's
10c / 5c / 20 s exits: speed buys fills, the entry rule decides whether they pay.

## 3. Changes

| change | evidence |
|---|---|
| scenario `calibrated` (`quote_survival_ms` 1000, `survive_to_limit`): the later book must still fill the size inside the LIMIT; the fill is charged at the worse of the two books | best single rule on both feed periods of real orders (above); on the triangle's 56 orders: 46 % filled, +0.71 per fill, 88 % win (realistic: 16 %, +0.23) |
| sources deribit,kraken,bitstamp,coinbase,binance (median of 5 moves on the 3rd quote, ~49 ms, not the 2nd of 3, ~106 ms) | feed benchmark latencies; real venue fill rates above. Binance kept for its trade flow (order-flow features) |
| `realistic` unchanged; other apps unaffected | |

What this is not: the scenario change makes the paper bot judge fills the way the venue did; it does not make
the strategy better. The feed change is the real speed gain. The entry model, limit (+3c), size and caps are
unchanged. Judge the new run by fills per order, fill price over the decision ask, and settled P&L per fill.

## 4. First 20 minutes of the new run (restarted 19:49:38 UTC, fresh ledger; old run archived)

| | |
|---|---|
| orders / fills | 22 / 12 (55 %; was 16 %) |
| kills | quote_pulled 5, fok_unfillable 5 |
| paid over the decision ask | +1.0c on average |
| spot age at decision, p50 | 60 ms (was ~115) |
| settled | 19:55 window: 30 DOWN shares bought 0.25-0.84, UP won, -21.6; 20:00 no trade; 20:05: -2.58 |

More fills means more shares per window, and every fill in a window shares one outcome: a window that flips
now costs up to the 50 USDC per-market cap instead of one or two legs. The archive replay (80 % fills, the same
caps) already contained this and was +2.3 per market out of sample; three windows say nothing either way.
