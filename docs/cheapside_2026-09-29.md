# CHEAPSIDE: buying the cheap token and holding to settlement (2026-09-29)

RESEARCH plus a paper-only strategy. No process was started; live trading was not touched and this
strategy cannot be run with real money (`__main__.check_cheapside` refuses micro-live, live and shadow
whatever the environment says). Script: `scripts/research_cheapside.py` (report
`results/cheapside_backtest.md`), sanity checks `scripts/research_cheapside_checks.py`
(`results/cheapside_checks.md`), the fitted model `data/replay/cheapside_model.json`.

**Answer.** Buying the cheap side because it is cheap loses: across 4.4 million cheap-side book updates
the cheaper token settles as the winner 2–4c *less* often than its price says, in every period and in
every minute of the window. A model of the cheap side's winning probability fitted on the training
windows selects about 1 in 2,000 of those updates, and those trades made money on every period,
including the forward period no study had read: **+$197 train, +$100 validation, +$155 test, +$505
forward** (10-share trades, realistic execution, after fees). Two cautions decide what this is worth:
the forward period's profit per trade is a fifth of the earlier periods' (+$0.47 vs +$2.2–2.7), and the
model is 6c overconfident on the forward trades it takes. That is evidence of an edge that is real but
small — positive on each of the forward period's six days, not fading within it — and the paper run
below is the right next test. It is not a case for real money.

## What was built

| where | what |
|---|---|
| `strategy/cheapside.py` | `CheapSideConfig` (env prefix `TPB_CHEAPSIDE_*`, flag `TPB_CHEAPSIDE_ENABLED`), `CheapSideModel` (the fitted logistic regression), `CheapSideEngine` (a `SpotLagEngine` subclass with its own decision, budget and model; hold to settlement) |
| `config.py`, `live.py`, `__main__.py` | `cfg.cheapside`, the `--strategy cheapside` branch, `check_cheapside` (paper only, no real-money switch at all) |
| `ecosystem.cheapside.config.js` | `tpb-cheapside-paper`: tagged files (`data/*.cheapside-paper.*`), venue_lag fills, the research rules spelled out |
| `scripts/research_cheapside.py` | the backtest: candidates, the model fit and recalibration, the realistic simulation, the report |
| `scripts/research_cheapside_checks.py` | where the edge sits (time × price), the strategy with and without the model, fill plausibility, forward by day |
| `tests/test_cheapside.py` | 16 tests: flags, the refusal of real money, its own prefix (spot-lag untouched), cheap-side selection and band, the model file and its prediction, the support and edge gates, one position per market (never both sides), attempts, hold-to-settlement, the live bot's wiring and budget ceiling |

Spot-lag, momentum and repriced-momentum are unchanged (their tests: 169 passed).

## The exact rules

1. Every 5-minute market of BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, from open to close, evaluated on every
   tick (and on every fast-feed event; the paper app turns the fast feed on).
2. `cheap_side` = the side with the strictly lower best ask (a tie is no side). Only
   **0.05 ≤ ask ≤ 0.35**, **spread ≤ 3c**, the venue minimum showing at the touch, the token's book
   ≤ 1000 ms old, spot ≤ 1000 ms old, **15–295 s left**.
3. `model_probability` = P(the cheap side settles as the winner) from a logistic regression on 15
   causal inputs, signed to the cheap side: logit(ask), logit(analytic TWAP fair value) (spot distance
   from the strike against the settling average's remaining variance), the spot's 250 ms / 500 ms /
   1 s / 3 s returns in sigma, their sign consistency (0–3) and acceleration, log realised vol,
   √(time left), the strike distance in sigma·√t, the 15 s trade-flow imbalance and book imbalance,
   the spread and the touch depth. Fitted on **train** windows only, Platt-recalibrated on **validation**.
   The archive cannot resolve 50 / 100 ms returns (points are ~220 ms apart), so they are diagnostics
   from the fast feed, not inputs.
4. **Support**: the 1 s spot return points the cheap side's way and 2 of the 3 short horizons agree.
5. **Edge**: `model_p − limit − taker fee(limit) − 0.5c ≥ 0.05`, the limit being ask + 1c.
6. **Order**: FOK for 10 shares (≤ $3.50; cap $5), one position per market, at most 3 orders per
   market ≥ 2 s apart, 60 buys/h, 4 markets at once, $20 total, $10 daily loss.
7. **Exit**: none. Every position settles at 1 / 0 (one fee, no exit). The kill file and the risk halts
   stop new entries; nothing sells early. Both sides are never bought (one position per market).
8. Thresholds (band, 3c, 5c, 1c, 10 shares, support) were fixed before the simulation and not tuned
   on any period.

## Periods

The research cache's chronological split by 5-minute epoch: **train** 09-22 12:30 → 09-23 15:20 UTC
(1,408 windows), **validation** → 09-23 23:55 (564), **test** = unseen_1 + unseen_2 → 09-24 16:25 (931),
**forward** = every window after that → 09-29 16:20 (8,102): no study had examined the forward
windows in any form.

## Does the model know more than the price?

On every candidate (all periods; the ask is the market's own probability):

| period | candidates | actual win rate | mean ask | Brier ask → model | log loss ask → model |
|---|---:|---:|---:|---:|---:|
| train | 584,891 | 0.178 | 0.196 | 0.1374 → 0.1363 | 0.4359 → 0.4329 |
| validation | 202,795 | 0.176 | 0.201 | 0.1358 → 0.1341 | 0.4322 → 0.4262 |
| test | 381,134 | 0.156 | 0.197 | 0.1245 → 0.1229 | 0.4046 → 0.3981 |
| forward | 3,251,452 | 0.163 | 0.197 | 0.1298 → 0.1297 | 0.4183 → 0.4167 |

A little, everywhere; almost nothing on forward in aggregate. The price is a good forecast of the cheap
side's chances; the model's value is in the tail it selects.

## Calibration by price bucket (all candidates)

`model − actual`, in cents (mean model p vs the actual settlement rate of the cheap side):

| ask | train | validation | test | forward (actual) |
|---|---:|---:|---:|---:|
| $0.05–0.10 | −0.8 | −0.7 | +0.9 | +0.2 (0.043) |
| $0.10–0.15 | −1.8 | +0.4 | +0.6 | +0.4 (0.086) |
| $0.15–0.20 | −1.4 | +1.3 | +1.5 | +0.6 (0.135) |
| $0.20–0.25 | −1.9 | +0.1 | +1.1 | +1.2 (0.186) |
| $0.25–0.30 | −1.7 | +0.2 | +0.2 | +2.3 (0.233) |
| $0.30–0.35 | −1.1 | −1.1 | +2.5 | +3.4 (0.283) |

Well calibrated to about 1c on validation and test; on forward it drifts optimistic by 2–3c at the
top of the band, where the strategy trades. **The cheap side's actual settlement probability sits
below its ask in every bucket and period** (e.g. forward: a $0.30–0.35 token wins 28.3 % of the time).

## Where the edge sits (sanity check 1: no model, no execution)

Actual win rate − ask, in cents, by time left × ask, forward period (the other periods read the same):

| time left | $0.05–0.10 | $0.10–0.15 | $0.15–0.20 | $0.20–0.25 | $0.25–0.30 | $0.30–0.35 |
|---|---:|---:|---:|---:|---:|---:|
| 240–300 s | −2.9 | −2.3 | −0.4 | −0.9 | −2.7 | −3.6 |
| 180–240 s | −2.5 | −3.0 | −3.7 | −4.3 | −3.9 | −4.1 |
| 120–180 s | −2.9 | −4.1 | −4.6 | −4.0 | −4.8 | −4.1 |
| 60–120 s | −2.6 | −3.1 | −3.5 | −4.2 | −2.8 | −2.3 |
| 15–60 s | −1.4 | −2.3 | −4.6 | −5.5 | −5.7 | −6.2 |

Negative everywhere: **cheap is not value**, in no minute and at no price. The profit below comes
from the model's selection, not from the market misprising the open or the tail.

## The strategy, realistic execution

FOK 10 shares at ask + 1c; the order leaves after the measured presend and reaches the venue after
half the measured round trip (drawn per order); the venue book is the archived local book 500 ms after
arrival; a fill needs the ask at or under the limit with 10 shares behind it; taker fee on the entry,
settlement free. Averages over 3 latency draws:

| period | markets | candidates | +support | eligible | orders | fills | fill rate | gross $ | fees $ | **net $** | net/trade | win rate | max DD $ | max exposure $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| train | 1,408 | 584,891 | 164,534 | 235 | 156 | 89 | 57 % | +209 | 12 | **+197** | +2.22 | 52 % | 42 | 15.1 |
| validation | 564 | 202,795 | 52,229 | 101 | 77 | 40 | 52 % | +106 | 6 | **+100** | +2.51 | 56 % | 11 | 7.9 |
| test | 931 | 381,134 | 106,786 | 153 | 109 | 58 | 54 % | +163 | 8 | **+155** | +2.66 | 55 % | 14 | 13.6 |
| forward | 8,102 | 3,251,452 | 811,214 | 2,356 | 1,651 | 1,069 | 65 % | +661 | 156 | **+505** | +0.47 | 36 % | 111 | 19.5 |

Entry price mean 0.28–0.30; model p mean 0.41–0.42 on the trades. **Model p vs actual on the trades:**
train 0.409 → 0.523, validation 0.409 → 0.558, test 0.422 → 0.554 (the model is *under*-confident
where it trades), **forward 0.425 → 0.365 (6c over-confident)**. t-statistics of the per-trade net:
7.3 / 5.5 / 7.4 / 5.6. Unfilled orders are almost all `QUOTE_GONE` (the ask moved above the limit
before arrival); 3 of 2,800 on forward lacked depth. Max exposure never exceeded the $20 budget; the
positions are unhedged by design (one side, held to settlement).

**Fill plausibility (check 3):** the filled ask stayed at or under the limit for a median 2.2 s
(train) to 4.0 s (forward) after the arrival read; 18–25 % were gone within a second, 25–44 % still
there after 5 s. These are quotes that rested, not ghosts of the local book.

**With and without the model (check 2, net $, 3 draws):**

| rule | train | validation | test | forward |
|---|---:|---:|---:|---:|
| **the strategy** (model, edge ≥ 5c) | +196 (89 fills, 58 %) | +96 (40, 52 %) | +147 (57, 52 %) | +508 (1,067, 65 %) |
| no model: every supported cheap ask | −88 (1,392, 80 %) | −57 (559, 85 %) | −294 (919, 79 %) | −3,499 (7,848, 80 %) |
| no model, first minute only | −108 (1,067, 82 %) | +77 (408, 85 %) | −146 (726, 79 %) | −2,071 (5,747, 81 %) |
| no model, ask ≥ 0.20, first minute | −104 (1,054, 82 %) | +79 (399, 85 %) | −147 (720, 79 %) | −2,018 (5,562, 81 %) |
| model, edge ≥ 5c, first minute only | +174 (75, 63 %) | +92 (35, 65 %) | +153 (46, 57 %) | +559 (764, 69 %) |

Without the model every version loses (the one exception, validation, is 408 fills of a rule that
loses three times as much on the other three periods). The model is the strategy.

## Regimes (fills, all draws pooled; net $ per 10-share trade)

**By entry price.** Positive above $0.20 in every period. $0.05–0.10 loses in every period it trades
(0 % win rate on test and forward: the model says 0.17–0.19, reality 0.00–0.03). $0.10–0.20 is small
and mixed.

| entry | train | validation | test | forward |
|---|---:|---:|---:|---:|
| $0.05–0.10 | +2.22 (10) | — | −0.64 (12) | −0.76 (45) |
| $0.10–0.15 | −1.40 (11) | +8.83 (3) | −1.06 (3) | +3.22 (31) |
| $0.15–0.20 | +3.33 (12) | +2.99 (6) | −1.69 (3) | +1.08 (71) |
| $0.20–0.25 | +4.30 (30) | +2.38 (13) | +4.06 (14) | +1.16 (204) |
| $0.25–0.30 | +1.77 (28) | +1.41 (28) | +2.86 (35) | +0.57 (635) |
| $0.30–0.35 | +2.64 (118) | +3.78 (46) | +2.50 (83) | +0.49 (1,588) |

**By time left.** The first minute (240–300 s left) holds 85 % of the fills and all of the profit:
+2.28 / +2.67 / +3.38 / +0.75 per trade. Entries under 60 s left lose in train, test and forward
(−1.48 / −0.15 / −0.20); 180–240 s is −0.37 on forward (504 fills).

**By momentum (1 s spot return in sigma).** "Flat" (|z| < 0.5) is positive everywhere (+1.96 / +2.52 /
+2.93 / +0.54); "strong with" (z > 2) is positive on train, validation and forward (+4.30 / +4.35 /
+2.09) but −3.39 on 6 test fills; "with" (0.5–2) is positive on train / validation / test and
**−0.20 on forward** (618 fills). No momentum regime is reliably better than the others; the support
gate as specified is kept.

**By asset.** Forward: SOL +1.31, XRP +1.53, DOGE +1.79, HYPE +1.97 per trade; BTC +0.19, ETH +0.14;
**BNB −0.31 (685 fills)**. Earlier periods do not single out BNB (train +4.80, test +2.38), so this
is not a filter that train / validation would justify.

**Forward by day (check 2b):**

| day (UTC) | orders | fills | net $ | net / fill |
|---|---:|---:|---:|---:|
| 09-24 (from 16:25) | 57 | 33 | +30 | +0.91 |
| 09-25 | 263 | 157 | +112 | +0.71 |
| 09-26 | 527 | 412 | +117 | +0.28 |
| 09-27 | 461 | 297 | +81 | +0.27 |
| 09-28 | 193 | 95 | +113 | +1.18 |
| 09-29 (to 16:20) | 147 | 71 | +55 | +0.76 |

Positive on every day; the busy days (09-26 / 27, when the rule fired 2–3× as often) earned the least
per fill. No trend within the forward period: the edge is smaller than in the earlier periods, not
fading day by day.

## Evidence of a real edge?

For: positive net on four chronological periods with the model fitted only on the first and thresholds
fixed in advance; t 5.5–7.4; the profit survives the measured latency, the arrival-book proxy that
drops spot-lag's winners, FOK depth and fees; filled quotes rested for seconds; and the edge is not the
open's mispricing (check 1) but the model's selection (check 2).

Against: the forward period, four times longer than train + validation + test together, earns a
fifth per trade; the model is 6c optimistic on forward's trades and 2–3c optimistic on forward's
$0.25–0.35 candidates; the forward win rate is 36 % on entries averaging 0.30, so the margin after
fees is about 4.5c per share — one more cent of adverse selection erases it. BNB alone lost $211 on
forward. And every fill here is a fill against a recorded local book; on real books this strategy's
orders would be resting-quote takes in the first minute of a window, where the paper record of
fill rates is thin.

**Verdict: evidence of a real but small edge (about 4–5c per share after fees on forward, positive on
every forward day), worth a paper test; not proven on real fills, and not a candidate for real money
on this evidence.**

## Recommended next paper-test configuration

Run `tpb-cheapside-paper` exactly as `ecosystem.cheapside.config.js` sets it (the pre-registered
rules, nothing tuned on test or forward):

    pm2 start ecosystem.cheapside.config.js --only tpb-cheapside-paper

Judge it after ~48 h on (a) fills per hour (the replay says ~9/h at a 65 % fill rate), (b) the
recorded-book audit of its orders against `data/charts` (not its dashboard P&L), (c) model p vs
actual settlement on its fills, by price bucket, and (d) forward net per trade. Two filters are
supported by train and validation alone and could be a second, parallel paper app if the first shows
life: **ask ≥ 0.20** (the $0.05–0.20 buckets are ≤ 0 on train and validation) and **≥ 120 s left**
(under 60 s is negative on train). Nothing else is justified without looking at test / forward.
