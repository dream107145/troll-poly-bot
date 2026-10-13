# Spot-lag parameter optimisation under realistic execution (2026-09-25)

RESEARCH ONLY. No live process, live configuration or `.env` value was changed. The one code change is a
parameter profile switch (`TPB_SPOT_LAG_PROFILE`, default `baseline`, section 14), which nothing runs.

**Verdict: NO ROBUST EDGE FOUND.**

- **Over the 51 hours used for selection (09-22 12:45 → 09-25 00:45 UTC) the strategy is clearly profitable under
  realistic execution:** the locked configuration makes +$105 (176 fills, +12c/share, t 6.2) with a $2.87 maximum
  drawdown, and every Monte Carlo run is positive.
- **Over the 12 untouched hours after that it makes +$0.99**, with a Monte Carlo P(profit) of 50%, and it loses under
  the combined-worst execution case. Six of the eight success criteria pass; the two that fail are both about the
  untouched test.
- **The parameter that matters most is one the brief did not name: the quote-age gate.** The live bot refuses any
  signal whose Polymarket quote is older than 250 ms. Raising that to 1,000 ms nearly doubles the selection-period
  profit (+$59 → +$105) and is the only change that made the test period non-negative. The live bot's own 440
  stale-quote rejections would have been worth about +$18 in the simulator.
- **Nothing here justifies real money.** The recommendation is a paper test of the `optimized` profile, and above all
  more data: the strategy's best hours (18–24 UTC) have not yet occurred in the untouched period.

## 1. Architecture summary (what was inspected)

| stage | where | what it does |
|---|---|---|
| spot feeds | `feeds/spot.py`, `live.py` | Binance, Bybit, Coinbase trades → a composite spot; a per-exchange lag monitor excludes a feed more than 750 ms behind |
| spot timestamps | `live.py` (`spot_feed_latency_ms`) | exchange trade time vs local receive time, recorded per order in `data/trade_forensics*.jsonl` |
| Polymarket book | `feeds/polymarket` WS → `history_store.py` | top of book per token, venue timestamp vs receive time (`book_latency_ms`, `quote_age_ms`) |
| fair value | `pricing/twap.py`, `strategy/spot_lag.py` (`MFV_COEF`) | the analytic fair value from spot vs strike and σ, mapped through the market's own response curve to an expected token price `ev` |
| signal | `strategy/spot_lag.py:evaluate` | 1 s spot return ≥ 3σ, the 3 s return agrees, accelerating; one signal per market and direction per 2 s |
| entry pricing | same | `max_entry` = highest price leaving `min_net_edge` after both fees, half the spread, 0.5c slippage and 0.5c uncertainty; the FOK limit = min(ask + `max_slippage`, `max_entry`) |
| gates | same | 60–295 s left, ask in [0.05, min(0.95, `max_ask`)], spread ≤ 3c, quote age ≤ 250 ms, spot age ≤ 1,000 ms, HYPE excluded |
| order | `execution/polymarket.py` (live) / `execution/paper.py` (paper) | FOK, 5 shares (the venue minimum), signed and POSTed; paper matches against the local book 500 ms after simulated arrival |
| exit | `strategy/spot_lag.py:exit_decision` | sell 5 s after the fill, held to 10 s while the spot keeps moving; FOK sell at bid − 2c, retried every 2 s; the last 10 s settle at 1/0 |
| take-profit / stop-loss | `live.py:_take_profit` | present in the engine (TP at the bid when the net clears 0.5c; SL at bid − 2c), off in the spot-lag profile |
| fees | `signals/costs.py` | taker 0.07 × p × (1 − p) per share per leg; settlement free |
| risk | `risk/limits.py`, `graduation.py`, `SpotLagConfig` budget | 4 positions, $10 exposure, $5 per order, 60 buys/h, 1 entry and 3 attempts per market, $10 daily loss; micro-live caps on top |
| archive | `live.py:_archive_chart` → `data/charts/<slug>.json` | every bot writes each closed window's point path (~220 ms apart): spot, both books' top of book with depth; no venue timestamps |

## 2. The hypothesis, stated testably

Spot moves → the token reprices with a lag → buy the lagging token below its expected price → sell 5–10 s later.
Tradable edge exists only if the ask that is still below `ev` when the bot decides is still there when the order
lands, with enough size, and the token then moves enough to pay two fees, the spread and the 2c exit concession.
Observed lag is not edge: sections 8 and 12 quantify how much of it survives execution.

## 3. Dataset

| period | UTC | hours with data | role |
|---|---|---:|---|
| train | 09-22 12:45 → 09-24 06:45 (blocks 0–6) | 33.6 | selection |
| validation | 09-24 06:45 → 09-25 00:45 (blocks 7–9) | 17.3 | elimination |
| **test** | **09-25 00:45 → 13:10 (blocks 10–12)** | **12.0** | untouched by selection |

- 15,504 shock candidates (every ≥ 2.5σ shock inside the loosest gates, HYPE excluded), 6 latency draws each.
- Blocks are 6 h from 09-22 12:45 UTC so that block 10 starts exactly at the test boundary.
- Disclosure: no parameter was chosen on the test period, but the paper-history audit of 13:00 UTC had reported six
  fixed settings' results on it (docs/spot_lag_paper_history_2026-09-25.md). The test period contains no 18–24 UTC
  data, the strategy's strongest hours in every earlier period.

## 4. Execution model

The simulated sequence per shock, with every latency stage drawn from the live bot's measured samples
(`data/replay/live_latency_samples.json`, n = 41–52 per stage, from `data/trade_forensics.jsonl`):

| stage | source | p50 | p90 | p99 |
|---|---|---:|---:|---:|
| spot feed latency (exchange → this machine) | measured | 135 ms | 282 | 2,216 |
| book latency (venue → this machine) | measured | 90 | 207 | 255 |
| decision latency (signal processing) | measured | 5 | 45 | 84 |
| presend (decision → POST) | measured | 49 | 78 | 176 |
| venue round trip | measured | 245 | 648 | 1,523 |
| **shock on this machine → order at the venue** | derived | **198** | **391** | **828** |
| quote age at the decision | derived | 117 | 262 | 1,068 |

- **Book at execution:** the venue's book at arrival is read as the archived local book 500 ms later (the proxy
  validated on 09-23 against venue-timestamped books; stress: 750 and 1,000 ms).
- **Buy:** FOK. Fills at the arrival ask only if it is within the limit AND the top level holds the whole size.
  Otherwise NO FILL. No partial fills (the live order is FOK), never a fill at the decision price, never a mid.
- **Sell:** FOK at the bid − 2c (at the bid for a target) with the same latency draws and a depth check, retried every
  2 s; inside the last 10 s the position settles at 1 or 0 with no fee.
- **Gates:** quote age = time since the last archived change of that side's book + a book-latency draw (the live
  bot's `quote_age_ms` is the venue's last-update age, the same thing); spot age = spot-feed draw + decision draw.
- **Fees:** 0.07 × p × (1 − p) per share on each taker leg. Sizes above 5 shares must be covered by the top level.
- **What the archive cannot support:** depth beyond the top level (so no VWAP walking; larger orders are refused, which
  is conservative), venue timestamps (quote age is a proxy), and feed stalls (a frozen local book would look like a
  quiet book). The archive writer is whichever bot closed the window last; the 7 recent windows that could be checked
  match the event-driven paper bot's own history exactly, the process whose fills agreed with the recorded book.

## 5. Baseline: the paper edge and where it goes (section 31)

Per share, all periods | untouched test. Each row adds one realism step.

| step | fills | c/share | net USDC | test fills | test c/share | test USDC |
|---|---:|---:|---:|---:|---:|---:|
| **production rule (MAX_ASK 0.95)** | | | | | | |
| 1. fill at the signal-time ask, exit at mid after 5 s, no fees, no latency, no budget | 1,507 | +11.4 | +858 | 291 | +10.9 | +158 |
| 2. + exit at the bid (spread) | 1,514 | +10.7 | +806 | 293 | +10.0 | +146 |
| 3. + both taker fees | 1,514 | +7.9 | +597 | 293 | +7.2 | +106 |
| 4. + latency: fill only if the ARRIVAL ask is within the limit (missed fills, adverse selection) | 344 | +5.9 | +102 | 60 | −1.3 | −4 |
| 5. + exit execution (FOK at bid − 2c, depth, retries, settlement) | 344 | +5.9 | +101 | 60 | −1.4 | −4 |
| 6. + quote/spot-age gates and the portfolio budget | 182 | +4.6 | +42 | 34 | −2.5 | −4 |
| **recommended rule** | | | | | | |
| 1. signal-time fill, mid exit, no fees | 1,015 | +14.4 | +731 | 203 | +14.4 | +146 |
| 3. + spread and fees | 1,016 | +10.7 | +543 | 203 | +10.5 | +107 |
| 4. + latency and the arrival book | 222 | +10.8 | +120 | 30 | +2.8 | +4 |
| 6. + gates and budget (all draws) | 176 | +12.1 | +106 | 22 | +0.9 | +1 |

- **Latency and missed fills remove 80–85% of the signal-time profit.** Of every 100 signals that look profitable when
  the bot decides, 77 are gone when the order arrives; under the production rule the survivors keep +5.9c of the
  +7.9c after fees.
- **Spread and fees remove a third before that** (11.4 → 7.9c).
- **Exit execution costs almost nothing** (0.4 USDC): the 5-share sells find their bid.
- **The stale-quote gate then removes half of what is left** (102 → 42 under the production rule), which is the
  finding of section 8.
- **The original paper simulator** (`execution/paper.py` matching the process's own local book): the audit in
  docs/spot_lag_paper_history_2026-09-25.md found 13 of the tick-driven ma74 bot's 20 fills impossible against the
  recorded book; those 13 carried +$8.78 of its +$12.63. Its realistic replay was −$1.31. That simulator's fills were
  not used anywhere in this study.

## 6. Training results

Stage 1 grid: shock threshold × minimum edge × price limit × spread × MAX_ASK × time window = 9,450 configurations ×
6 latency draws, production exit and gates, through the production budget in time order; 3,580 configurations are
eligible (≥ 30 training fills, positive training net, block drawdown ≤ $5).

Selection rule (the same in every fold): rank eligible configurations by the neighbour-averaged training net (a
plateau, not a spike); the first with a positive validation net on ≥ 5 fills wins. Locked 2026-09-25 13:28:02 UTC.

| ranking | configuration | train net (fills) | validation | **test** |
|---|---|---:|---:|---:|
| production | 3σ, 2c edge, +2c, spread 3c, ask ≤ 0.95, 60–295 s | +41.57 (109) | +5.04 (39) | **−4.31 (34)** |
| ma74 paper bot | same, ask ≤ 0.74 | +42.21 (85) | +12.28 (28) | **−3.24 (22)** |
| **selected (robust rule) = best raw training net** | 3σ, 2c edge, +3c, spread 5c, ask ≤ 0.60, 60–295 s | +46.39 (69) | +12.94 (16) | **−0.48 (16)** |
| best validation net | 4σ, 1c edge, +3c, spread 5c, ask ≤ 0.60, 60–295 s | +38.82 (62) | +16.29 (19) | +1.07 (11) |
| best test net (not selectable) | 2.5σ, 6c edge, +1c, spread 2c, ask ≤ 0.60 | +32.80 (35) | +7.06 (6) | +3.44 (5) |

Across all 9,450 configurations the test net is p10 −$3.0, median −$0.8, p90 +$1.1; 13% are positive. Training rank
correlates with test rank at +0.44 (Spearman), validation at +0.48.

## 7. Validation results and the stages after selection

| stage | chosen on train + validation | kept? | effect (dev = train + validation) |
|---|---|---|---|
| gates (8 quote ages × 4 spot ages) | quote ≤ 1,000 ms, spot ≤ 500 ms (the least permissive within 2% of the best) | yes | dev +$59 → +$105 |
| exit (15 rules) | t10 (+1.3% over the production exit) | no (< 5%) | production exit kept |
| size ($1–$10) | see section 13 | no | venue minimum kept |

**Recommended configuration:** 3σ shock, 2c minimum edge, limit = ask + 3c, spread ≤ 5c, ask ≤ 0.60, 60–295 s
left, quote age ≤ 1,000 ms, spot age ≤ 500 ms, production exit, 5 shares.

| period | orders | fills | fill rate | fills/h | net USDC [draw range] | c/share | win | PF | max DD | worst | streak | t |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| train (33.6 h) | 494 | 126 | 26% | 3.8 | +77.27 [+73.5, +79.9] | +12.2 | 79% | 9.1 | 1.53 | −1.48 | 3 | +5.8 |
| validation (17.3 h) | 216 | 28 | 13% | 1.6 | +27.84 [+24.9, +31.1] | +20.3 | 78% | 22.8 | 0.53 | −0.50 | 2 | +2.9 |
| **test (12.0 h)** | 178 | 22 | 12% | 1.8 | **+0.99 [−1.9, +3.4]** | +0.9 | 40% | 1.3 | 2.87 | −0.79 | 6 | +0.3 |
| all (62.9 h) | 888 | 176 | 20% | 2.8 | +106.11 [+99.1, +110.4] | +12.1 | 74% | 8.1 | 2.87 | −1.48 | 6 | +6.2 |
| production, all | 1,103 | 182 | 17% | 2.9 | +42.29 | +4.6 | 56% | 2.3 | 6.33 | −2.31 | 8 | +3.0 |

## 8. Out-of-sample test results

- **The recommended configuration made +$0.99 on 22 fills in the untouched 12 hours** (40% win rate, range across
  latency draws −$1.9 to +$3.4). Production made −$4.31, the ma74 bot −$3.24.
- **By 6-hour block:** train blocks +$1.9 to +$19.7 each; validation +$6.7, +$7.9, +$13.2; test −$0.6, +$1.4, +$0.2.
- **The best configuration of the entire grid made +$3.44 on the test**, on 5 fills, and would have been chosen by
  nothing but the test itself.

## 9. Walk-forward results

Rolling folds: train = blocks 0..i+3, validation = block i+4, test = block i+5, the same selection rule each time.

| fold | test block (UTC start) | chosen | test net (fills) | production | ma74 |
|---|---|---|---:|---:|---:|
| 0 | 09-23 18:45 | 3σ, 3c, +3c, 5c, ≤ 0.74 | +4.57 (19) | +3.02 | +3.20 |
| 1 | 09-24 00:45 | 3σ, 2c, +3c, 5c, ≤ 0.60 | +4.85 (8) | +4.33 | +3.74 |
| 2 | 09-24 06:45 | same | +0.45 (3) | −3.39 | −0.04 |
| 3 | 09-24 12:45 | 3σ, 2c, +3c, 5c, ≤ 0.74 | +0.47 (9) | −0.86 | +0.46 |
| 4 | 09-24 18:45 | 3σ, 2c, +3c, 5c, ≤ 0.60 | +11.23 (8) | +9.29 | +11.86 |
| 5 | 09-25 00:45 (untouched) | same | −0.42 (9) | −1.07 | −0.91 |
| 6 | 09-25 06:45 (untouched) | 3σ, 4c, +3c, 5c, ≤ 0.70 | +0.46 (4) | −3.24 | −2.34 |
| 7 | 09-25 12:45 (25 min) | 3σ, 3c, +3c, 5c, ≤ 0.70 | 0.00 (0) | 0.00 | 0.00 |

Total +$21.60 over 8 folds, 6 positive (production +$8.07, ma74 +$15.98). The chosen configuration is stable
(3σ, +3c, 5c spread, ceiling 0.60–0.74 in every fold), but the two untouched folds sum to +$0.04.

## 10. Stress-test results

Net USDC, mean over draws; recommended | production.

| scenario | dev | test | all |
|---|---:|---:|---:|
| normal (measured latency, proxy 500 ms) | +105.1 \| +46.6 | +1.0 \| −4.3 | +106.1 \| +42.3 |
| slow network: every stage × 1.5 | +96.0 \| +27.1 | −0.4 \| −4.2 | +95.7 \| +22.8 |
| stale book: proxy 750 ms | +94.7 \| +35.7 | −0.2 \| −4.1 | +94.5 \| +31.6 |
| low liquidity: top-level depth halved | +92.9 \| +37.4 | −0.5 \| −5.6 | +92.4 \| +31.8 |
| bad fills: +1c slippage per leg | +89.6 \| +31.7 | −1.2 \| −7.7 | +88.3 \| +24.0 |
| fees × 1.5 | +94.2 \| +36.7 | −0.7 \| −6.6 | +93.5 \| +30.1 |
| adverse selection: the best 25% of fills never happen | −11.1 \| −23.9 | −4.1 \| −9.4 | −15.2 \| −33.4 |
| **combined worst reasonable** (latency × 2, proxy 1,000 ms, +1c slippage, depth halved) | +51.9 \| +5.7 | **−3.7** \| −7.0 | +48.2 \| −1.3 |

- On the selection period the recommended rule survives every single stress with 85–91% of its profit, and half of it
  under the combined case. Production keeps 50–80% and loses under the combined case.
- Removing the best quarter of fills kills any positively skewed strategy; it is reported because the brief asked,
  and it says the profit lives in the right tail (section 12).
- On the untouched test every stress turns the +$0.99 into a small loss.

## 11. Monte Carlo results

2,000 runs of the recommended configuration, each with a random latency draw, a random fill-drop rate U(0, 30%),
extra slippage U(0, 1c) per leg, a delayed exit (10 s instead of the production exit) with probability 30%, and a
shuffled trade order.

| period | P10 | P25 | P50 | P75 | P90 | P(net > 0) | max DD P50 / P90 |
|---|---:|---:|---:|---:|---:|---:|---|
| all (62.9 h) | +68.4 | +75.3 | +83.1 | +91.1 | +97.4 | 100% | 1.92 / 2.78 |
| dev (50.9 h) | +68.4 | +75.7 | +83.8 | +91.0 | +96.8 | 100% | 1.70 / 2.55 |
| **test (12.0 h)** | −2.5 | −1.3 | **0.0** | +1.3 | +2.2 | **50%** | 2.29 / 3.68 |

## 12. Best robust parameter region, sensitivity and the source of profit

**Parameter table (section 33).** "Test" is the untouched test net when only that parameter is varied.

| parameter | current (live) | search range | optimized | validation | test |
|---|---|---|---|---:|---:|
| lag threshold (fair value − ask must exceed) | = min edge + costs | via min edge | same | | |
| lag window (spot move measured over) | 1 s | not searched (structural) | 1 s | | |
| spot move window / token response window | 1 s and 3 s / 1 s | not searched (structural) | same | | |
| z threshold (spot shock, σ) | 3.0 | 2.5, 3, 3.5, 4, 5 | **3.0** | +27.8 | +1.0 (3σ) … +2.0 (2.5σ) |
| minimum edge (after both fees, half spread, 1c) | 2c | 1, 2, 3, 4, 6c | **2c** | +27.8 | −2.0 (1c) … +5.1 (6c) |
| price limit above the ask | 2c | 1, 2, 3c | **3c** | +27.8 | +0.9 … +1.1 |
| maximum spread | 3c | 2, 3, 5c | **5c** (engine cap 3c still applies) | +27.8 | +1.0 … +1.6 |
| minimum depth | top level ≥ order size | not searched (the venue minimum) | same | | |
| **maximum quote age** | **250 ms** | 100 … 1,500 ms | **1,000 ms** | +27.8 | −0.7 (100) … +2.8 (1,500) |
| maximum spot age | 1,000 ms | 300, 500, 1,000, 2,000 ms | **500 ms** | +27.8 | +0.9 … +1.1 |
| maximum entry price (MAX_ASK) | 0.95 | 0.50 … 0.95 | **0.60** | +27.8 | −1.8 (0.74) … +1.5 |
| time window (seconds left) | 60–295 | 7 windows, 30–295 | **60–295** | +27.8 | −0.5 … +1.4 |
| take profit | off | 3, 5, 10c with 10–20 s limits or to settlement | **off** (tp5_20 +2%, not kept) | +28.6 (tp5_20) | +4.2 (tp5_20) |
| stop loss | off | 3, 5c | **off** (every stop loses money) | +19.0 (sl3) | +0.3 |
| maximum hold | 5 s, 10 s while the spot moves | 3, 5, 10, 20 s | **unchanged** | +27.7 (t10) | +2.7 (t10) |
| order size | 5 shares | $1, 2, 3, 5, 10 | **5 shares** | | |

**Sensitivity (section 34):** dev-net range when one parameter moves across its grid.

| parameter | dev range | fills range | class |
|---|---:|---:|---|
| **quote age** | **$76.5** | 114 | high impact; the only parameter whose test result changes sign |
| time window | $68.7 | 123 | high impact (shorter windows only lose volume) |
| shock threshold | $32.7 | 101 | high impact, monotone |
| exit rule | $29.4 | – | high impact only through stop-losses and hold-to-settlement, which lose |
| MAX_ASK | $17.4 | 128 | high impact; 0.60–0.70 plateau, test negative at 0.74 and 0.95 |
| minimum edge | $15.7 | 92 | medium; larger edges trade volume for per-share profit |
| spread | $12.5 | 23 | medium (and capped by the engine at 3c) |
| spot age | $10.8 | 14 | low above 500 ms |
| price limit | $5.5 | 16 | low |

No parameter changes the sign of the dev net anywhere on its grid: the region is a plateau, not a spike.

**Where the profit comes from (section 35):** recommended configuration, c/share (fills), all periods | test.

- **Lag size is the whole story:** lag ≥ 12c makes +18.9c (85 fills) | +10.2c on the test; lag 8–12c makes +4.1c |
  −4.9c; lag 6–8c +1.8c | −6.4c. The 12c+ trades are 49% of fills and 77% of profit.
- **Big shocks:** z1 ≥ 6σ makes +22.3c (30), 4.5–6σ +13.6c (45), 3–3.5σ +6.0c (45).
- **Cheap tokens:** entry ≤ 0.20 +15.9c, 0.20–0.40 +14.0c, 0.40–0.60 +7.4c (all negative on the test).
- **Late in the window:** ≤ 90 s left +22.8c (22 fills); 90–150 s +15.1c; more than 150 s +7–8c.
- **Time of day:** 12–24 UTC +15–17c; 06–12 +10c; 00–06 +6c. The test period is 00–13 UTC only.
- **Assets:** BTC +13.1c (51), DOGE +13.7c (16), SOL +11.6c, BNB +11.4c, XRP +9.4c, ETH +8.9c (33). With 16–51 fills
  each, per-asset parameters are not supported. UP +12.2c, DOWN +10.8c: symmetric.
- **Reversals:** after a fill the best bid within 30 s averages +27.9c above the entry (84% reach +10c), the worst
  −7.3c (35% go 5c under, 24% 10c under), and 39% fall back below the entry after their best. That is why
  stop-losses lose (they sell the dip before the move) and hold-to-settlement loses (39% of the "targets" reverse).
- **Concentration:** the top 5 fills are $16.6 of $99.1 (draw 0); two of them are one moment (09-22 17:48).
- **Is it stale quotes?** Every fill here required the recorded ask at arrival + 500 ms to be within the limit with
  5 shares behind it, and the recorded book kept updating after the fill (median next change 0.5–1.2 s, > 2 s in
  1–3% of fills, 15% only for quote ages above 1 s, which the 1,000 ms gate excludes). The profit is not stale quotes;
  it is the lag itself, which the 250 ms gate discards along with the stale ones.

## 13. Position sizing (section 22)

| size | typical shares | fills (all) | fill rate | dev net | test net | all net | max DD |
|---|---:|---:|---:|---:|---:|---:|---:|
| venue minimum | 5 | 176 | 20% | +105.1 | +1.0 | +106.1 | 2.87 |
| $1 | 5 | 173 | 19% | +119.6 | −0.4 | +119.3 | 3.35 |
| $2 | 5 | 164 | 18% | +156.6 | −0.2 | +156.5 | 3.42 |
| $3 | 7 | 154 | 18% | +204.2 | −0.6 | +203.6 | 4.68 |
| $5 | 11 | 132 | 16% | +266.5 | −0.8 | +265.8 | 7.73 |
| $10 (needs the $5 order cap raised) | 23 | 95 | 13% | +280.9 | −7.1 | +273.8 | 14.38 |

Fills fall as the size grows (the top level must hold the whole order) but the archive cannot show market impact or
deeper levels, so this scaling is optimistic. Not recommended beyond 5 shares until real fills at 5 shares match the
simulator (they do not yet: docs/spot_lag_parameter_sweep.md, limitation 1).

## 14. Failure modes and the recommended paper configuration

**Failure modes found**

1. **The edge is intermittent.** It is strong for two and a half days and zero for the next half day. Nothing in the
   parameters predicts which.
2. **It lives in the right tail.** Removing the best quarter of fills makes every configuration lose.
3. **It disappears with execution.** 77% of profitable-looking signals are gone at arrival; the survivors are the
   ones with the largest lag (≥ 12c).
4. **The 250 ms quote-age gate throws away half of what survives.** It was added to stop the bot trading on a stale
   local book, but the fill simulation, which checks the venue book at arrival, says most of those signals were real
   lag, not staleness. The live bot's own 440 rejections for quote age (median age 540 ms) map to 60 archive shocks
   that pass the other gates; 28% would have filled at +21c/share, about +$18.
5. **The paper simulator inflates tick-driven bots** (docs/spot_lag_paper_history_2026-09-25.md). Not fixed.
6. **Two of the "loose" paper settings never applied:** `TPB_SPOT_LAG_MAX_SPREAD=0.05` and `MAX_SLIPPAGE=0.04` are
   capped by `EngineConfig.max_spread` 0.03 and `max_slippage` 0.02 (`spot_lag.py` takes the tighter of the two); the
   bot's log shows 76 rejections of 4–5c spreads. The `optimized` profile is subject to the same caps.

**Recommended paper configuration** (`TPB_SPOT_LAG_PROFILE=optimized`, implemented in `SpotLagConfig.PROFILES`):

| setting | baseline (unchanged, the default) | optimized |
|---|---|---|
| max_ask | 0.95 | 0.60 |
| max_quote_age_ms | 250 | 1,000 |
| max_spot_age_ms | 1,000 | 500 |
| max_slippage | 0.02 | 0.03 (engine cap 0.02 still applies) |
| max_spread | 0.03 | 0.05 (engine cap 0.03 still applies) |
| everything else (3σ, 2c edge, window, exit, budget) | | unchanged |

The profile is selected by environment only, defaults to `baseline`, changes no risk limit, removes no guard, and
explicit `TPB_SPOT_LAG_*` values still override it. Test: `tests/test_spot_lag.py::test_profile_optimized_applies_then_env_overrides`.
Nothing runs it. To paper-test it: a new pm2 app with `TPB_SPOT_LAG_PROFILE: 'optimized'` beside the existing paper
bots, at normal CPU priority.

## 15. Final ranking and answer

| | configuration | dev | test |
|---|---|---:|---:|
| best historical (training) | 3σ, 2c, +3c, 5c, ≤ 0.60 (= the locked pick) | +59.3 (250 ms gate) | −0.5 |
| best validation | 4σ, 1c, +3c, 5c, ≤ 0.60 | +55.1 | +1.1 |
| best out-of-sample | 2.5σ, 6c, +1c, 2c, ≤ 0.60 (chosen on the test; not selectable) | +39.9 | +3.4 |
| **most robust (recommended for paper)** | locked pick + quote ≤ 1,000 ms, spot ≤ 500 ms | **+105.1** | **+1.0** |

**Success criteria (section 38):** positive validation YES (+$27.8); positive untouched test YES (+$0.99); positive
walk-forward YES (+$21.6); reasonable drawdown YES ($2.87, 3% of net); robust across execution stress **NO** (every
stress turns the test negative; combined worst −$3.7); not dependent on unrealistic fills YES (every fill is at the
arrival book; MC P(all-period net > 0) = 100%); stable across nearby values YES; test edge more than noise **NO**
(MC P(test net > 0) = 50%).

**Answer to the brief's question.** After accounting for measured latency, stale quotes, spread, top-level
liquidity, slippage, failed fills, adverse selection, exit execution, fees and reversals, there IS a parameter
region with strong positive expectancy on the 51 hours used for selection (+$105, PF 8, drawdown $2.9, every Monte
Carlo run positive), and it is a plateau: 3–3.5σ shocks, a 2–4c edge, a ceiling of 0.60–0.70, the full 60–295 s
window, the production exit, and a quote-age gate of 500–1,500 ms instead of 250 ms. On the 12 untouched hours that
followed, the same region makes nothing (+$1, P(profit) 50%), and the two untouched walk-forward folds sum to zero.
So: **NO ROBUST EDGE FOUND** by the brief's standard. The assumption that breaks is stationarity, not execution: the
paper edge that disappears in execution (77% of signals) is priced in here, and what remains was real for two days
and absent on the third.

**What would be needed to continue:** (1) more untouched data, in particular 18–24 UTC on 09-25, since the test window
contains none of the strategy's best hours; (2) real fills at 5 shares to confirm the simulator's fill rate (the live
bot filled 12% against the simulator's 17–20%); (3) a paper run of the `optimized` profile beside `baseline`, judged by
the recorded book, not by the paper simulator; (4) venue timestamps in the archive so that quote age is measured, not
proxied.

## Reproduce

```
.venv/bin/python scripts/research_repriced_momentum.py cache
.venv/bin/python scripts/research_spot_lag_realistic.py build --draws 6 --tag base
.venv/bin/python scripts/research_spot_lag_realistic.py sweep --tag base
.venv/bin/python scripts/research_spot_lag_realistic.py build --draws 4 --scale 1.5 --tag lat15
.venv/bin/python scripts/research_spot_lag_realistic.py build --draws 4 --proxy 750 --tag stale750
.venv/bin/python scripts/research_spot_lag_realistic.py build --draws 4 --scale 2.0 --proxy 1000 --tag worst
.venv/bin/python scripts/analyze_spot_lag_realistic.py     # results/spot_lag_realistic_report.md, _summary.json, _locked.json
```
