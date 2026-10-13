# Multi-source BTC feed: which source reaches this VPS first, and does it help at the venue? (2026-09-30)

**Answer, on the evidence so far.** Eleven more public sources were wired in beside the four the bot had,
recorded together for 10 minutes (at the user's request; the 5-hour run was cut short) and measured on one
clock. The Deribit perpetual and Kraken deliver 33–38 ms after their own stamps against Coinbase's 71 and
Binance's 152; at 1 bp consensus moves Deribit was first to show the move in 60 % of 25 events, Coinbase in
20 %; at 2 bp (7 events) Coinbase was first in 43 %. The venue token repriced a median 169 ms after the
first source, and an order needs ~135 ms to land — so the earliest possible detection leaves ~10–150 ms of
usable window, and "Coinbase alone" (live now) a median 27 ms. **There were no 5 bp or 10 bp events in the
recording, which are the moves the strategy actually trades on, so nothing here is evidence for real
money. Verdict: NOT READY for live; PAPER READY** (a paper app is defined, not started).
The full tables are in `results/feed_benchmark_report.md`; numbers in `results/feed_benchmark_summary.json`.

## 1. Architecture

Nothing in the live path changed. The additions sit beside it:

- **`feeds/sources.py`** (new): adapters for OKX spot and USDT-swap (`bbo-tbt`, `trades`, `books5`), Bitstamp
  (`order_book`, `live_trades`), Binance USDS-M futures (`bookTicker`, `aggTrade`), Bybit linear
  (`orderbook.1`, `publicTrade`), Deribit (`quote`, `trades.100ms`), Hyperliquid (`bbo`, `trades`), Coinbase
  Advanced Trade (`ticker`, `market_trades`), Gemini v1 (top of book + trades), Bitfinex v2 (ticker + trades,
  sequence/timestamp flags), Polymarket RTDS `crypto_prices_chainlink` (settlement reference, refused as a
  composite source), and a Binance spot `depth10@100ms` depth-only stream. Each emits the project's own
  `SpotQuote` / `TradeTick` with the venue's event time and sequence preserved, through a `_Sink` that runs a
  `StreamValidator` (duplicate / out-of-order / stale / gap per stream; duplicates dropped, the rest counted
  and delivered) and the shared reconnect loop (`spot._loop`: open timeout, receive timeout, exponential
  backoff 2 → 60 s, health, backlog drop). Venue keep-alives: OKX `ping`, Hyperliquid `{"method":"ping"}`,
  Deribit `set_heartbeat` + `public/test`, Bitstamp `bts:request_reconnect` → reconnect. Every adapter is its
  own task; one failing never stops the composite.
- **`feeds/spot.py`**: `SpotQuote.sequence` / `TradeTick.sequence` (Binance `u`/`a`, Bybit `seq`, Coinbase
  `sequence`/`trade_id`, Kraken `trade_id`); Kraken's ticker `timestamp` now kept as `event_ts_ms`; `run_all`
  dispatches any further `--exchanges` name to `sources.ADAPTERS`. Behaviour with the old names is unchanged.
- **`scripts/record_feed_benchmark.py`** (DATA_ONLY): all sources + the Polymarket BTC books in one process,
  four clocks kept apart (`et` exchange, `rt` receipt clock-corrected, `mt` monotonic, `vt` venue), the
  validator verdict per message, loop-lag and health records.
- **`scripts/research_feed_benchmark.py`**: the report below (delivery, arrival order, regimes, predictive
  features, fair-price variants, venue windows), DEV / VALIDATION / TEST by time.
- Modes: DATA_ONLY = the recorder; PAPER = `tpb-spotlag-paper-multisrc` in `ecosystem.spotlag.config.js`
  (defined, **not started**); LIVE = `tpb-spotlag-live`, untouched (Coinbase alone, BTC only).

## 2. Files

| file | change |
|---|---|
| `src/troll_poly_bot/feeds/sources.py` | new: 12 adapters, parsers, `StreamValidator`, `DepthSnapshot` |
| `src/troll_poly_bot/feeds/spot.py` | `sequence` on quotes/trades, Kraken event time, `run_all` dispatch |
| `src/troll_poly_bot/__main__.py` | `--exchanges` help lists the new names |
| `scripts/record_feed_benchmark.py` | new: DATA_ONLY recorder |
| `scripts/research_feed_benchmark.py` | new: the analysis |
| `tests/test_feed_sources.py` | new: 13 tests on captured messages, validator, dispatch (suite: 1,035 pass) |
| `ecosystem.spotlag.config.js` | `tpb-spotlag-paper-multisrc`, paper only, not started |
| `data/ticks/feedbench_20260930_184451.jsonl.gz` | the 10-minute recording (549 k records); `_183955` a 4.5-minute one |
| `results/feed_benchmark_report.md`, `results/feed_benchmark_summary.json` | the report and its numbers |

## 3. Sources and why

All reachable from the Dublin VPS without a key on 2026-09-30. Included: the four existing (Binance,
Bybit, Coinbase Exchange, Kraken), OKX spot and perpetual, Bitstamp, Binance and Bybit USDT perpetuals,
Deribit's inverse perpetual, Hyperliquid, Coinbase Advanced Trade (the newer Coinbase socket), Gemini,
Bitfinex, and Polymarket's Chainlink stream (what the market settles on). Findings that matter for the
adapters: Coinbase Advanced Trade's `market_trades.side` is the **maker's** (1,545 of 1,545 trades joined on
`trade_id` disagree with the Exchange ticker, whose side is the taker's) — flipped; Bitfinex throttles its
ticker to ~4 updates a minute (its trades are real-time); Hyperliquid stamps block time (~360 ms behind);
Deribit's raw trade stream needs an account (the 100 ms one is used). **Pyth Hermes** returns 401 on every
route — it requires an API key since 2026-08-26 — and is excluded rather than faked.

## 4. Benchmark (10 min, 18:44–18:54 UTC, BTC)

Delivery delay = receipt − the venue's own stamp (its clock offset is inside the number):

| source | quotes/min | p50 | p90 | p99 | max | stale | out-of-order |
|---|---:|---:|---:|---:|---:|---:|---:|
| deribit | 651 | 33 | 40 | 79 | 233 | 0 % | 0 % |
| kraken | 641 | 38 | 59 | 93 | 1478 | 0 % | 0 % |
| bitstamp | 554 | 46 | 65 | 76 | 99 | 0 % | 0 % |
| coinbase_adv | 286 | 68 | 69 | 76 | 183 | 0 % | 24 % (trade batches) |
| coinbase (live) | 338 | 71 | 82 | 162 | 584 | 0 % | 0 % |
| gemini | 399 | 111 | 141 | 300 | 364 | 0 % | 0 % |
| bybit / bybit_linear | 482 / 1186 | 117 | 120 | 133 | 1401 | 0 % | 0 % |
| okx / okx_swap | 482 / 1506 | 148 | 152 | 169 | 259 | 0 % | 0 % |
| binance (trades) | 6414 | 152 | 238 | 406 | 516 | 0 % | 0 % |
| binance_futures | 18392 | 178 | 318 | 736 | 902 | 0 % | 0 % |
| hyperliquid | 409 | 360 | 452 | 644 | 815 | 0 % | 35 % |
| pm_chainlink | 59 | 1466 | 1914 | 2232 | 2536 | 7 % | 0 % |

Recorder loop lag p50 3 ms, max 33 ms: receipt times carry at most that much of our own delay. No
duplicates anywhere; no silence over 5 s except Gemini (12).

## 5. Leader analysis (receipt time; a source "detects" when its own mid has moved half the event)

| event | n | first-arrival share (top) | median lag behind the first |
|---|---:|---|---|
| 1 bp | 25 | deribit 60 %, coinbase 20 %, okx / bybit_linear / bitstamp / kraken / hyperliquid 4 % | okx 130, bybit_linear 136, bybit 157, coinbase 197, binance 223, coinbase_adv 285, binance_futures 297 ms |
| 2 bp | 7 | coinbase 43 %, deribit / gemini / kraken / hyperliquid 14 % | deribit 34, coinbase 37, okx 116, bybit_linear 123, binance 125 ms |
| 5 / 10 bp | 0 | — | — |

Against the live source (Coinbase): Deribit −32 ms p50 at 1 bp, +1 ms at 2 bp; every USDT venue is
behind Coinbase. Against Binance: Coinbase −125 ms at 1 bp, −179 at 2 bp, Deribit −176 / −128. The leader
changes with the bucket (Deribit small moves, Coinbase 2 bp; up-moves Coinbase 50 %, down-moves split three
ways), the volatility terciles hold 2–3 events each: **a 10-minute sample cannot name a leader**. The
09-29 45-minute trade recording (docs/latency_2026-09-29b.md) had Coinbase −23 ms before Binance at 3σ and
Kraken +25; today's Kraken is +147 ms behind Coinbase at 1 bp — consistent with "Kraken is near but quotes
the move late", not with the delivery table.

Predictive value of a source's own 1 s shock (section 4a of the report): at 1 bp most sources' shocks are
followed by a same-sign consensus move 80–100 % of the time on DEV, 50–100 % on TEST (n = 3–10), and are
confirmed by the consensus 50–100 %; Hyperliquid's are late (+450 to +850 ms). Features (4b): velocity ρ
+0.2 to +0.3 with the next 1 s return everywhere (it is the move itself), OBI on Binance / OKX / Bitstamp ρ
+0.2 to +0.35 on both halves, trade imbalance +0.1 to +0.3, cross-exchange divergence unstable in sign
between DEV and TEST — none of it is a tradable signal at this sample size; it is what the 5-hour run is for.

## 6. Fair price

| composite | reaction after the first source, 2 bp p50 / p90 | own 2 bp shocks/h | noise (100 ms σ, bp) |
|---|---:|---:|---:|
| coinbase alone (live now) | 84 / 513 ms | 60 | 0.12 |
| median of 3 (previous live) | 220 / 513 | 36 | 0.10 |
| lead mode, 3 sources (live 09-29) | 84 / 198 | 78 | 0.36 |
| median of all spot / spot + perps / microprice | 220 / 313–413 | 42 | 0.08 |
| liquidity-weighted, all | 213 / 828 | 60 | 0.12 |
| **lead mode, all sources** | **34 / 221** | 120 | 0.47 |
| trade VWAP 1 s, all | 278 / 1034 | 48 | 0.16 |
| deribit alone | 70 / 171 | 90 | 0.15 |

Lead mode over every source reacts 50 ms sooner than Coinbase alone and 190 ms sooner than any median,
at 4x the noise and twice the shock rate of Coinbase alone; medians are the most robust and the slowest.
No composite produced a 2 bp shock that fewer than three sources confirmed (the confirmation test is lenient).

## 7. Polymarket

1 bp events with an in-band token (8 of 21; 81 % of tokens repriced within 5 s, a median 169 ms after the
first source): the first-source detection leaves a 147 ms median window before the ask moves (100 % positive),
Coinbase alone 27 ms (62 % positive), the median-of-3 11 ms. With the 135 ms pipeline the modelled fill
probability is 88 % (first source) against 62 % (Coinbase); the 20 s mark-out of those fills is −1.6c/share
mean for the first-source orders and +1.1c for Coinbase's (n = 7 and 5). At 2 bp: 3 in-band events. These
are not results; they are the shape of the measurement.

## 8. Replay old vs new

Not run: the two older tick recordings (09-23, 09-29) hold only Binance / Bybit / Coinbase, and today's
recording holds no 5 bp event, so the spot-lag rule (≥ 3σ ≈ 3 bp in 1 s) fires nowhere in it. The comparison
in section 7 is the replay at the thresholds the recording supports.

## 9. Recommendation and live-readiness

**NOT READY** for real money; **PAPER READY**. What the 10 minutes do support: the lowest-delay sources
(Deribit, Kraken, Bitstamp) are ~35 ms nearer than Coinbase; an all-source lead composite sees a 2 bp move
~50 ms before Coinbase alone; the venue reprices ~170 ms after the first source, so the whole gain is of
the order of the pipeline delay — the same wall every earlier study hit (docs/fast_feed_2026-09-29.md).
Whether that turns into fills and profit needs (1) a recording long enough to hold dozens of ≥ 3 bp events
(`python scripts/record_feed_benchmark.py --minutes 300`, DATA_ONLY, then the report script), and (2) the
paper A/B `tpb-spotlag-paper-multisrc` against the live bot over the same hours, judged by the recorded-book
audit. Do not add sources to `tpb-spotlag-live` on this evidence.
