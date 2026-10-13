# Spot-leading / token-lag momentum: validation study, 2026-09-23/24

**Question.** Does the crypto spot price lead the Polymarket 5-minute UP/DOWN token by enough, for long enough,
that this bot can buy the lagging token before it catches up? It has to work after both fees, the spread,
slippage and the bot's measured latency, on data the strategy was not tuned on.

## Verdict

**The broad hypothesis is false.** The lag is real (section B). Most of what the bot *sees* as lag is its own feed
delay (sections E-G). Buying lagging tokens in general loses 1-3.4c/share in every period, at every move size and
with every exit tested (section M).

**A narrow version is positive out of sample but not yet confirmed.** It enters only after a large spot shock
(≥ 3σ in one second; strongest at ≥ 4σ), and only when at least 2c of net edge remains after every cost. Results:
- **Out of sample: +3.5c/share, about 140 fills across four independent periods, t = +2.7** (clustered by 5-minute
  epoch).
- The result barely moves when the book-delay assumption behind the fill model is varied from 300 to 1,000 ms.
- It is positive, at +4.6c/share, on the real venue book during the tick recording. That is only about 5 fills.

It is not proven:
- The test period is flat (+0.2c).
- Most of the gain is BTC (+12.4c, t +4.4). The other six assets together make about +1.3c, which is not
  significant.
- Every period on its own has t < 2.
- The fill model is a validated proxy, not the real venue book.

**Implemented behind a flag; trading real money at the micro-live caps since 2026-09-24 14:00 UTC, on your
explicit instruction.** The candidate is `strategy/spot_lag.py`, and it is doubly gated:
- `TPB_SPOT_LAG_ENABLED=1` is required for the engine to exist at all;
- real orders additionally need `TPB_SPOT_LAG_ALLOW_REAL=1` and `--mode micro-live`.

It runs as `tpb-spotlag-live` with the unchanged `MicroCaps`:
- 5 USDC per order, 10 USDC open, 10 USDC daily loss;
- 5 buy orders an hour (sells are never capped);
- 1 position at a time.

It ran in paper (`tpb-spotlag-paper`) from 13:44 to 14:00 UTC; the real-money start skipped the multi-day paper
and shadow stages that section O recommends. `tpb-momentum-live` was stopped by an operator `pm2 stop` at
13:34 UTC and has not been restarted.

### Correction to the first draft

The first draft of this report said NO, based on an archive backtest at about −4c/share. That backtest counted a
fill only if the quote was **still on the book 2 s later**. That rule had reproduced 25 of 30 live FOK kills. The
tick recording shows it is biased against exactly this strategy:
- it keeps only 37 % of real fills;
- the fills it drops are the winners: +4.6c/share when sold 20 s later, against −1.9c for the fills it keeps.

All strategy results below use a fill model checked against the venue book instead (section G).

---

## A. Architecture map

```
SPOT     feeds/spot.py      Binance bookTicker + aggTrade, Bybit orderbook.1 + publicTrade, Coinbase ticker
                            SpotQuote.ts_ms = local receipt (clock-corrected)   .event_ts_ms = exchange stamp if any
                            CompositeSpot -> SpotHistory -> pricing/vol.py TwoScaleVol, pricing/twap.py TwapState
CLOCK    live.ClockSync     this machine vs Binance server time (NTP-style), re-synced
TOKEN    live.py            Polymarket market WS -> LiveBook per token (snapshot + price_change)
                            quote_age_ms(token) = now - newest VENUE timestamp delivered   (gate: 500 ms)
FAIR     pricing/twap.py    twap_fair_value: digital on the Chainlink 60 s TWAP vs the strike, Student-t(4)
SIGNAL   strategy/momentum.py  MomentumEngine.evaluate (every 200 ms)
                            detect_momentum -> reprice guard (STALE_QUOTE / TOKEN_ALREADY_MOVED, 3 s)
                            -> expected value = mid + continuation -> net edge after both fees, exit cost,
                               slippage, uncertainty -> max_entry_price -> cap = min(ask + slip, max entry)
EXECUTE  execution/polymarket.py   FOK marketable limit at the cap; _pre_post_refusal; timeline, RTT
         execution/latency.py      PRESEND_PRIOR_MS (15-250), VENUE_RTT_PRIOR_MS (218-661, p50 243)
RECORD   execution/decisions.py, outcomes.py, forensics.py   every decision, markouts, kills
RISK     risk/limits.py, risk/guard.py, graduation.py (MicroCaps), kill switch in live.py
SPOT_LAG strategy/spot_lag.py: SpotLagEngine(MomentumEngine), behind TPB_SPOT_LAG_ENABLED (NEW)
RESEARCH backtest/momentum_replay.py; scripts/record_ticks.py + scripts/spot_lag_ticks.py (NEW, read-only)
```

The spot-lag engine subclasses `MomentumEngine`, so it reuses:
- the executable-ask pricing and the max-entry cap;
- FOK orders and revalidation;
- the entry confirmation checklist;
- the journals;
- the exit machinery;
- every risk control above.

### Timestamps: what each feed carries, and how late it reaches this machine (90-minute recording)

| feed | exchange's own stamp | median delay here | p95 | p99 |
|---|---|---:|---:|---:|
| Binance bookTicker | **none** (local receipt only) | — | — | — |
| Binance aggTrade | trade time `T` | 147 ms | 246 ms | 414 ms |
| Bybit orderbook.1 | book update `ts` | 116 ms | 144 ms | 214 ms |
| Coinbase ticker | `time` = last *trade* time, useless for quote timing | — | — | — |
| Polymarket book | venue event `timestamp` | 32 ms | 341 ms | 749 ms |
| Polymarket book, **in the 2 s after a ≥ 2σ spot move** | same | 40 ms | 657 ms | 2,582 ms |

This machine's clock runs 21 ms off Binance's, measured by 18 NTP-style syncs. Exchange time and local receipt time
are recorded separately (`et`/`vt` against `rt`) and are never mixed within one measurement.

---

## B. Lead/lag evidence

Token response to a ≥ 2σ one-second spot move: the Up-token mid change since 1 s before the move, signed in the
move's direction, in cents, all assets pooled. Three clocks:

| clock | events | −250 ms | 0 | +25 | +50 | +100 | +150 | +250 | +500 | +750 | +1 s | +2 s | +3 s | +5 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Exchange A: Bybit book stamp → Polymarket venue stamp | 2,367 | +1.16 | +2.06 | +2.24 | +2.53 | +3.48 | +4.21 | +5.09 | +5.79 | +5.99 | +6.17 | +6.45 | +6.54 | +6.75 |
| Exchange B: Binance trade stamp → Polymarket venue stamp | 1,372 | +1.28 | +2.28 | +2.45 | +2.61 | +3.28 | +4.21 | +5.43 | +6.54 | +6.61 | +6.76 | +6.87 | +7.00 | +7.33 |
| Local: spot received here → token book received here | 1,980 | +1.25 | +2.59 | +2.89 | +3.23 | +3.64 | +4.00 | +4.51 | +5.44 | +6.01 | +6.29 | +6.60 | +6.58 | +6.95 |

**Spot leads.** On the exchanges' own clocks, the token reaches half its 5 s response about 100-150 ms after the
spot stamp. A fitted impulse response on all data, not only events, agrees:

| exchange clock A | BTC | ETH | SOL | XRP | DOGE | BNB | HYPE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 50 % of response by | 300 ms | 200 ms | 150 ms | 150 ms | 200 ms | 200 ms | 100 ms |
| 90 % of response by | 1.25 s | 1.9 s | 3.9 s | 3.2 s | 4.8 s | 3.0 s | 1.2 s |

**But the bot sees it late.** Our spot data arrives 116-147 ms after the exchange stamp, so at the moment the bot
sees a move the token is already about half repriced: 52-62 % of its 5 s response at +100-150 ms on the exchange
clock. Our order then takes another presend + RTT/2 ≈ 130-450 ms to arrive, and by then 75-87 % of the reprice is
done. Only the slow tail is left: the last 15-25 %, spread over several seconds.

## C. Lag distribution by asset

Per event: the time until the token has made half of its 5 s response. Only events with a response of at least
1c are counted. Resolution is the measurement grid (0, 25, 50, 100, 150, 250, 500, 750 ms, 1, 2, 3, 5 s).

| asset | n (exch. A) | p25 | median | p75 | p90 | p95 | half done already at the stamp | median, LOCAL clock |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | 145 | 150 ms | 250 ms | 500 ms | 2 s | 3 s | 17 % | 500 ms |
| ETH | 158 | 25 ms | 150 ms | 500 ms | 2 s | 3 s | 23 % | 500 ms |
| SOL | 265 | 0 | 150 ms | 500 ms | 2 s | 3 s | 26 % | 150 ms |
| XRP | 217 | 0 | 100 ms | 500 ms | 2 s | 3 s | 34 % | 25 ms |
| DOGE | 226 | 0 | 100 ms | 500 ms | 2 s | 3 s | 31 % | 50 ms |
| BNB | 244 | 50 ms | 250 ms | 1 s | 3 s | 5 s | 20 % | 150 ms |
| HYPE | 239 | 0 | 100 ms | 750 ms | 3 s | 5 s | 29 % | 50 ms |
| **all** | **1,494** | **0** | **150 ms** | **500 ms** | **2 s** | **3 s** | **26 %** | **150 ms** |

Median lag by condition, exchange clock A (local clock in brackets):
- **Time left:** 150 ms in every bucket from 295 s down to 60 s, and 100 ms under 60 s. Local: 100 / 150 / 250 /
  150 / 50 / 0 ms.
- **Move strength:** 150 ms at 2-3σ, 3-4σ and ≥ 4σ. Local: 150 / 25 / 50 ms.
- **Direction:** 150 ms both up and down. Local: 100 up, 150 down.
- **Volatility:** 150 ms when calm or normal, 250 ms when hot (60 s realised vol > 1.25× typical).
- **Liquidity** (smaller of best bid/ask size): thin (< 50 shares) 150 ms (n 1,370); 50-200 shares 250 ms (n 87);
  deep (≥ 200 shares) 500 ms (n 37). Local: 100 / 500 / 750 ms.
- **Spread:** 150 ms at 1c and 2c, 100 ms above 2c.

BTC and BNB lag the longest. Deep books reprice more slowly, but they are rare.

## D. Token response: does the token price the spot distance, and how fast?

**The market prices the strike distance tightly, in volatility units.** On 1.15 M quiet two-sided archive points,
the R² of logit(Up mid) against each regressor is:

| seconds left | raw distance (bps) | vol-normalised z | analytic model logit(p) |
|---|---:|---:|---:|
| 240-300 | 0.80 | 0.88 | 0.89 |
| 180-240 | 0.79 | 0.95 | 0.96 |
| 120-180 | 0.79 | 0.95 | 0.97 |
| 90-120 | 0.78 | 0.93 | 0.97 |
| 60-90 | 0.76 | 0.90 | 0.97 |
| 30-60 | 0.61 | 0.82 | 0.94 |

Market-implied fair value (MFV) is logit(mid) = a + b·logit(p_model), fitted on the **training** third only:
- b rises from 0.74 (240-300 s) to 0.95 (30-60 s), and a ≈ 0.
- Out-of-sample error is 2.5-4.5c.
- The market is calibrated: out-of-sample Brier scores for the market mid and the model agree within 0.002 in
  every bucket from 180 s down to 60 s.

So there is no standing mispricing; any edge has to come from timing.

**Most of the reprice is done by the time the bot sees the move.** At archive resolution the token has already
made 94 % (k = 1.5σ), 89 % (2σ), 82 % (3σ) and 75 % (4σ) of the implied move within the same second. The
tick-level signals give 118 %: an implied +2.24c against an actual +2.64c. **Larger shocks leave more unpriced**,
which is where the narrow version lives.

## E. Edge before latency

The lag score is ev − ask, where ev = token mid 1 s earlier + the change in MFV over that second. It is measured
on the tick recording (694 signals, ≥ 2σ, 92 windows), at the signal instant, on the bot's local book and on the
real venue book:

| population | n | lag on LOCAL book | lag on VENUE book, same instant | venue at arrival, 50 ms | venue at arrival, measured latency |
|---|---:|---:|---:|---:|---:|
| all signals | 694 | −1.08c | −3.73c | −4.40c | −5.79c |
| local lag > 0 | 206 | +2.59c | −0.84c | −1.50c | −3.03c |
| local lag ≥ 2c | 88 | +5.00c | −0.21c | −0.96c | −3.28c |
| local lag > 5c | 26 | +9.20c | +0.78c | +0.20c | −1.26c |
| eligible (net edge ≥ 1c locally) | 23 | +9.59c | +1.76c | +1.11c | −0.50c |

- **44 %** of the positive local lags do not exist on the venue at that instant; the venue ask is already higher.
- **63 %** are gone by the time an order would arrive.
- Paired on the same signals, local lag − venue lag = +3.4c on average (median +1.0c). **That gap is the "visual
  lag"**: the bot's own token feed running behind during a burst.

## F. Edge after latency

Tick recording, venue-truth simulation. The bot decides on its local book. Buys and sells fill only against the
venue book when the order arrives, with 5 shares of depth. Measured latency applies to every leg, averaged over
20 draws. Net is c/share per fill after both fees; t is clustered by epoch.

| population (measured latency) | fill rate | sell @1 s | sell @5 s | sell @20 s | exits A-F, 10 s | settle |
|---|---:|---:|---:|---:|---:|---:|
| eligible (net ≥ 1c), strategy cap | 22 % | +2.73 | +12.56 | +15.44 | −1.35 | +6.41 |
| local lag > 0, cap = ask + 2c | 39 % | −0.09 | +1.04 (t +0.8) | +2.28 (t +1.4) | −0.53 | +2.07 |
| local lag ≥ 2c, cap = ask + 2c | 31 % | −0.39 | +1.06 | +1.70 | −2.64 | −2.47 |
| local lag ≤ 0 (control) | 48 % | −2.28 (t −2.9) | −1.90 | −2.04 | −1.97 | −2.63 |
| random entry times (placebo) | 97 % | −4.16 (t −16) | −4.12 | −3.55 | −3.85 | −0.68 |
| **candidate: ≥ 3σ, net ≥ 2c** | 21 % | +4.73 | **+4.61 (t +0.9)** | −2.97 | +5.04 | −8.43 |

The eligible row is 5 expected fills and the candidate row about 4.6: too few to test on their own.

The lag score carries real information. Signals with a positive local lag beat the control by about 3c and a
random entry by about 5c. After costs, though, the broad population nets out around zero (t ≤ 1.4). The two
halves of the recording also disagree: +2.1c and −0.2c at 5 s.

**What if the bot were co-located (50 ms each way)?** Local lag > 0 would make +1.8c at 5 s (t +1.8) and +2.9c
at 20 s (t +1.9). Even co-location does not make the broad version clearly profitable.

## G. Fill-rate simulation, and the fill model used for the archive

| population | measured latency | 50 ms |
|---|---:|---:|
| eligible (strategy cap) | 22 % | 33 % |
| local lag > 0 | 39 % | 50 % |
| local lag ≤ 0 | 48 % | 61 % |
| candidate (≥ 3σ, net ≥ 2c) | 21 % | — |

The archive (2,688 windows) has only the bot's local book, sampled every ~233 ms. Each fill rule was tested
against the venue truth on the same tick signals (lag > 0, measured latency, 10 draws each):

| fill rule | precision | recall | net P&L vs truth (all signals, sell @1 s / exits A-F) |
|---|---:|---:|---|
| local ask at arrival | 59 % | 96 % | −0.38 / +0.21 vs −1.61 / −1.58: far too optimistic |
| local ask 300 ms after arrival | 80 % | 85 % | −1.30 / −1.11: slightly optimistic |
| **local ask 500 ms after arrival** | **86 %** | **81 %** | **−1.77 / −1.64: matches** |
| old rule: local ask at arrival and still there 2 s later | 91 % | **37 %** | drops the winners (+4.6c) and keeps the losers (−1.9c) |

All archive results below read the local book 500 ms late for every venue lookup: the buy at arrival and each
sell at arrival. The sensitivity runs use 300 ms and 1,000 ms.

## H. Entry algorithm (as tested; the candidate's values in bold)

1. **Spot shock:** z1 = 1 s spot log-return ÷ causal σ (TwoScaleVol) must be at least **k = 3**. Also:
   - the 3 s return agrees (z3 ≥ 0.5);
   - the move is accelerating (the 1 s return is more than a third of the 3 s return);
   - at most one signal per direction every 2 s;
   - 60-295 s left in the window.
2. **Expected token value:** ev = token mid 1 s earlier + Δ MFV over that second, using the frozen coefficients
   fitted on the training period.
3. **Lag score:** ev − executable ask. The mid is never used.
4. **Net edge:** lag − entry fee(ask) − exit cost (fee at ev + half spread) − 0.5c slippage − 0.5c uncertainty.
   Enter only if:
   - net edge is at least **2c**;
   - spread ≤ 3c;
   - ask between 0.05 and 0.95;
   - at least 5 shares at the ask.
5. **Order:** FOK marketable limit, 5 shares. Cap = min(ask + 2c, max entry), where max entry is the highest tick
   that still leaves a 2c net edge. One order per signal and no chasing: a killed order is not re-sent.
6. **Arrival:** signal + presend (15-250 ms) + RTT/2 (109-330 ms), drawn from the measured priors.

## I. Exit algorithm

The candidate uses **a sell 5 s after the fill**. That is a FOK marketable sell with its limit (floor) 2c under
the bid. A killed sell retries every second with a fresh floor. Inside the last 10 s the position is held to
settlement.

The active alternative starts once the fill confirmation reaches the bot and decides on the local book every
point. First match wins:
- A: bid ≥ ev_now − 0.5c (sold at the bid);
- F: stop, bid ≤ entry − 5c;
- B: reversal, z1 against the position ≤ −1.5σ;
- D: exhaustion, the 3 s return turns against the position after at least 1 s;
- E: max hold of 10 s.

For the candidate out of sample (proxy 500 ms), the exits compare as follows:

| exit | validation | test | tick period | holdout (15 h) | all out-of-sample |
|---|---:|---:|---:|---:|---:|
| **sell @5 s** | +6.37 (t +1.9) | +0.23 | +6.16 (t +1.7) | +2.93 (t +1.6) | **+3.55 (t +2.7)** |
| exits A-F, 10 s | +5.70 | −1.01 | +2.12 | +2.89 | +2.71 (t +2.1) |
| hold to settlement | +8.98 | −5.77 | −9.62 | +6.28 | +2.92 (t +0.8) |

The edge is a few seconds of catch-up after a shock. Holding to settlement adds noise and no edge.

## J. Risk controls

`SpotLagEngine` inherits the momentum stack's controls, all unchanged:
- executable-ask pricing and the max-entry cap;
- FOK only, plus revalidation on the freshest book;
- the entry confirmation checklist;
- the kill switch (`data/KILL`);
- per-asset and daily loss caps;
- `MicroCaps`;
- the graduation gate.

On top of that, as implemented:
- **Two flags.** `TPB_SPOT_LAG_ENABLED=1` is needed for the engine to exist, and `TPB_SPOT_LAG_ALLOW_REAL=1` as
  well for micro-live or live (`__main__.check_spot_lag`).
- **Venue-lag guard.** The traded token's venue-timestamped quote age must be ≤ 250 ms, against 500 ms for
  momentum. In the 2 s after a move the feed's p95 delay is 657 ms, and a stale local book is where the phantom
  lag comes from.
- **Kill tolerance.** About 80 % of orders are expected to be FOK-killed (price moved past the cap, or the quote
  was pulled). Those two outcomes are kept out of the guard's execution-failure rate for this engine only
  (`SpotLagEngine.expected_kills`). Otherwise the guard would trip on normal behaviour and, with no new orders,
  never clear. Venue rejects and missing liquidity still count.
- **Loss streak (paper only).** The 5-losses-in-a-row halt is off in paper for this engine. It clears only on a
  win, which cannot happen while entries are blocked, so it would end a forward test silently. Shadow and the
  real-money modes keep it.
- **Asset scope.** HYPE is excluded: it lost in training (−4.1c, t −3.0) and out of sample (−4.4c).
- **One position per market**, via the live bot's existing no-duplicate-position and exited-market rules. No
  averaging down.
- **Before micro-live:** the `MicroCaps` limit of 5 orders an hour would allow only about 1 fill an hour at a
  ~21 % fill rate. Raising it is a limit change, so it is your decision.

## K. Parameter recommendations

- **Existing momentum engine: unchanged.** Its strict profile placed zero orders on the 1,136-window replay, and
  nothing here argues for loosening it.
- **Broad spot-lag (k ≤ 2σ, or no net-edge filter): do not build.** It loses in every period.
- **Candidate: implemented with these defaults and running in paper.** Use real money only if the forward test
  (section O) confirms it.

  | parameter | value |
  |---|---|
  | k | 3σ |
  | min net edge | 2c after both fees, exit cost, 0.5c slippage and 0.5c uncertainty |
  | cap | min(ask + 2c, max entry) |
  | exit | sell 5 s after the fill, floor = bid − 2c |
  | size | 5 shares |
  | window | 60-295 s left |
  | spread | ≤ 3c |
  | assets | exclude HYPE |
  | venue-lag guard (quote age) | 250 ms: between the normal-time p90 (120 ms) and the burst p75 (192 ms) above it; not yet tested on its own |

## L. Profiles (defined on training data; results out of sample, sell @5 s, proxy 500 ms)

| profile | rule | train | out of sample (fills, t) |
|---|---|---:|---:|
| Conservative | ≥ 4σ shock, net edge ≥ 2c | +11.77 | **+4.66** (70, t +2.2) |
| **Balanced (the candidate)** | ≥ 3σ, net edge ≥ 2c | +8.16 | **+3.55** (140, t +2.7) |
| Aggressive | ≥ 3σ, net edge ≥ 1c | +5.42 | +1.95 (189, t +1.7) |
| for contrast | ≥ 2σ, net edge ≥ 1c | +3.29 | −0.56 (304, t −0.7) |
| for contrast | ≥ 2σ, any positive lag, cap = ask + 2c | −2.07 | −1.4 to −3.4 in every period |

The Conservative profile is a subset of the Balanced one. None of them is enabled, and no flag or env variable
exists for them.

## M. Backtest (archive, 2,688 windows, 7 assets, 09-22 15:30 → 09-24 13:05 UTC)

The grid covers k ∈ {1.5, 2, 3}σ, five entry rules (buy any positive lag, lag ≥ 2c, net edge ≥ 0 / 1c / 2c) and
six exits. The full tables are in the research output.
- **Buying any positive lag loses in all 5 periods at every k**: −1.1 to −3.4c at 5 s, t down to −17.
- **The net-edge filter is what matters**, and it helps more the larger the shock. At 2σ it is flat out of
  sample; at 3σ it is positive.
- **Breakdown of the candidate out of sample:**
  - By asset: BTC +12.4c (t +4.4), XRP +3.8, DOGE +3.7, BNB +2.1, ETH +0.7, SOL −1.0, HYPE −4.4.
  - By time left: 180-120 s is the strongest, at +7.0c (t +3.4); other buckets range −0.5 to +2.3c.
  - By strength: extreme ≥ 4σ +4.7c (t +2.2); strong 3-4σ +2.5c (t +1.6).
  - Across all shock sizes (k = 1.5, net edge ≥ 2c) only extreme moves work: weak −1.4c, moderate −3.0c.
- **Fills per day:** in the 15 h holdout the candidate produced about 20 signals and 5.5 expected fills an hour.
  At 5 shares and +3.5c that is about $20-25 a day before any live slippage beyond the model.

**Shadow comparison with the current momentum engine** (same archive): the strict profile places 0 orders. The
candidate would have added about 5 fills an hour at the out-of-sample expectancy above.

## N. Out-of-sample

| period | what it is | candidate, sell @5 s |
|---|---|---:|
| train | 09-22 15:30 → 09-23 08:35 (141 epochs) | +8.16 (137 fills, t +3.7), used for selection |
| validation | → 14:40 (71 epochs) | +6.37 (22, t +1.9) |
| test | → 20:40 (71 epochs) | +0.23 (19, t +0.1) |
| tick period | 20:40 → 22:15 (19 epochs), archive proxy / **venue truth** | +6.16 (19, t +1.7) / **+4.61 (≈5, t +0.9)** |
| holdout | 09-23 22:15 → 09-24 13:05 (178 epochs, never examined before this run) | +2.93 (80, t +1.6) |
| **all out-of-sample** | | **+3.55 (140, t +2.7)** |

Sensitivity of the pooled out-of-sample result to the book-delay proxy:

| proxy | pooled OOS |
|---|---:|
| 300 ms | +3.79 (t +2.9) |
| 500 ms | +3.55 (t +2.7) |
| 1,000 ms | +3.48 (t +2.7) |

**Selection caveat, stated plainly.** Across the grid, 15 entry configurations × 6 exits were examined:
- The originally stated rule, highest training mean with ≥ 30 fills, picks the candidate with **hold to
  settlement**. That scores +2.9c out of sample, t +0.8: not significant.
- Picking by training t-statistic gives the sell-at-5-s exit shown above.
- The fill model was changed after the tick data exposed the survival-rule bias. It was calibrated to match
  venue-truth P&L on all signals, not to maximise the candidate.

Neighbouring configurations point the same way: ≥ 3σ with net edge ≥ 1c makes +2.0c. That argues the effect is
real, but the evidence is not strong enough to trade on.

**The brief's criterion, net expectancy positive out of sample, is met in aggregate for the candidate only, and
not in every period.**

## O. Deployment sequence

**Status 2026-09-24 14:00 UTC:** you chose to go straight to micro-live (`tpb-spotlag-live`) after 16 minutes of
paper. Steps 1-3 below are still the way to judge it: compare its real fills with the in-process `venue_lag`
shadow and with a parallel tick recording. Nothing larger than micro-live happens without your decision.

1. **Paper (13:44-14:00 UTC, stopped for live):** `tpb-spotlag-paper` uses live books and spot, the venue's
   measured order latency and `--scenario venue_lag` fills. The existing paper exchange's quote-survival rule is
   biased against this strategy, so it is not used here. Run it for at least 2 days. It writes the dashboard's
   default files, so it must not run beside `tpb-momentum-live`.
2. **Forward test against the real venue book (recommended alongside paper).** Run the read-only recorder in
   hourly files and score the same pre-registered rules with the tick simulator. Passing means **all** of:
   - at least 200 fills;
   - net > 0 with t > 2, clustered by epoch;
   - ex-BTC not negative.
3. **Shadow** (`tpb-spotlag-shadow`) for at least 3 days: live decisions, the real account's path in dry run,
   nothing posted. Gate: net per fill > 0 and fill rate in line with the forward test.
4. **Micro-live** only on your explicit decision, under the existing `MicroCaps` with no limit raised, and with
   `TPB_SPOT_LAG_ALLOW_REAL=1`. Also give the engine its own FOK-kill budget first (section J).
5. Live only on your explicit decision, after graduation.

Nothing moves between stages on its own.

## P. Exact files changed (this task)

| file | change | effect on trading |
|---|---|---|
| `src/troll_poly_bot/strategy/spot_lag.py` | NEW: `SpotLagConfig`, `SpotLagEngine` (a `MomentumEngine` subclass) | only when selected and flagged |
| `src/troll_poly_bot/live.py` | selects `SpotLagEngine`; skips engine-declared expected kills in the kill-rate guard; paper-only loss-streak exemption for this engine | none for other engines |
| `src/troll_poly_bot/config.py` | `BotConfig.spot_lag` (from `TPB_SPOT_LAG_*`) | none |
| `src/troll_poly_bot/__main__.py` | `--strategy spot_lag`, `--scenario venue_lag`, `check_spot_lag` (both flags) | none otherwise |
| `src/troll_poly_bot/execution/scenarios.py` | `book_delay_ms` field; new `venue_lag` preset | existing presets unchanged (delay 0) |
| `src/troll_poly_bot/execution/paper.py` | matches `book_delay_ms` after arrival, reporting the true arrival time | none when the delay is 0 |
| `tests/test_spot_lag.py` | NEW: 16 tests (flags, signal, lag, cap, dedupe, exit, venue_lag, guard) | — |
| `tests/test_execution_scenarios.py` | the preset list includes `venue_lag` | — |
| `ecosystem.spotlag.config.js` | NEW: `tpb-spotlag-paper`, `tpb-spotlag-shadow`; no real-money app | — |
| `.env.example`, `README.md` | documented | — |
| `scripts/record_ticks.py` | NEW: read-only tick recorder (public WS only, no keys, no orders) | none |
| `scripts/spot_lag_ticks.py` | NEW: read-only tick analysis (sections B, C and E) | none |
| `docs/spot_lag_study_2026-09-23.md` | NEW: this report | none |
| `.gitignore` | adds `data/ticks/` (recordings, ~84 MB per 90 min) | none |

The full suite passes (693 tests). Other uncommitted changes in the working tree come from the earlier
execution-realism work (`docs/momentum_execution_2026-09-23.md`). The archive feature extraction, MFV fit,
archive backtests and the tick venue-truth strategy simulator were one-off research scripts outside the
repository.

## Q. Environment variables added

All are off unless set:
- `TPB_SPOT_LAG_ENABLED` (default `0`): the feature flag. The engine refuses to start without it.
- `TPB_SPOT_LAG_ALLOW_REAL` (default `0`): additionally required for `--mode micro-live` / `live`.
- `TPB_SPOT_LAG_<FIELD>`: any `SpotLagConfig` field. The defaults are section K's values:

  | field | default |
  |---|---|
  | `K_SIGMA` | 3 |
  | `MIN_NET_EDGE` | 0.02 |
  | `MAX_SLIPPAGE` | 0.02 |
  | `HOLD_S` | 5 |
  | `MAX_QUOTE_AGE_MS` | 250 |
  | `SIZE_SHARES` | 5 |
  | `EXCLUDE_ASSETS` | HYPE |
  | `ASSETS` | (empty) |
- `TPB_EXECUTION_SCENARIO=venue_lag` (or `--scenario venue_lag`): the new paper fill model.

## R. Rollback

Stop the paper bot. Nothing else runs the new code:

```
pm2 delete tpb-spotlag-paper
```

With `TPB_SPOT_LAG_ENABLED` unset and the default scenario, the bot behaves exactly as before, so removing the
code is optional. To remove it anyway, delete the new files and revert the small hunks in `live.py`,
`config.py`, `__main__.py`, `execution/scenarios.py`, `execution/paper.py` and
`tests/test_execution_scenarios.py` (section P):

```
rm src/troll_poly_bot/strategy/spot_lag.py tests/test_spot_lag.py ecosystem.spotlag.config.js
rm scripts/record_ticks.py scripts/spot_lag_ticks.py docs/spot_lag_study_2026-09-23.md
rm -rf data/ticks/                       # recordings; and drop the `data/ticks/` line from .gitignore
```

---

## Lookahead controls

- **Signals use only data stamped at or before the signal:**
  - past-only 1 s and 3 s returns;
  - causal σ and TWAP state;
  - the strike, which is fixed at the window open;
  - the local book as of the decision time.
- **MFV coefficients** were fitted on the training third only and frozen for everything after.
- **Splits are chronological by 5-minute epoch**, and t-statistics are clustered by epoch because the seven
  assets move together. The holdout (09-23 22:15 onward) was not looked at before the final run.
- **Future book states** only decide whether an order *placed at the signal* fills, and what a later sell
  receives. That simulates the order's own future; it is not information available to the decision.
- **Settlement outcomes** are used only for scoring.
