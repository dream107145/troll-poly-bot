# Repriced-token momentum: redesign research (2026-09-24)

## Verdict: D, abandon the strategy

**No subset of already-repriced tokens has a continuation large and predictable enough to beat fees, spread, slippage and latency.**

The candidates tested:
- huge shocks;
- a large remaining gap to fair;
- cross-exchange confirmation;
- Polymarket book pressure;
- low-fee prices;
- BTC and ETH only;
- a transparent expected-value model on about 40 features;
- six entry styles and 22 exits, including maker exits.

Every candidate loses money on the periods it was not chosen on.

- **The data does contain a signal.** The model ranks trades well (out-of-sample rank correlation 0.4–0.5).
- **It cannot make them profitable.** The best fifth it identifies still loses about 2c per share. Walked forward through three unseen periods, it lost **−1.42, −1.45 and −1.67c per share** (pooled −1.48c, t −6.6, 262 fills).
- **The costs are structural.** The token does keep moving after a shock (+0.5c to +6c). But a round trip costs about 3.5–5c: the spread plus two taker fees. And the orders that reach the venue in time to fill are disproportionately the ones where the move stalled.
- **Better infrastructure doesn't close the gap.** With no fees at all, or with co-located execution 10× faster, the best populations still lose. Only removing fees *and* co-locating together reaches break-even, and only in narrow subsets.

Why D rather than C: C ("no robust edge found") would suggest more data might reveal one. Here the losses are consistent across 5 chronological periods, 7 assets, every exit and every feature bucket, with t-statistics of −6 to −30. More data would tighten these negative estimates, not flip them.

**Production.** Nothing about the live system changed:
- `TPB_REPRICED_MOM_ENABLED` stays false by default, and real money for this strategy is disabled.
- Spot-lag (live and paper) is untouched.
- The paper bot `tpb-repricedmom-paper` has nothing left to show; stopping it is recommended.

---

## 1. Original hypothesis

After a large spot shock the token reprices part of the way toward fair. If the spot is still moving and the token is still following, the remaining continuation might be worth trading for a few seconds.

The running version, `strategy/repriced_momentum.py`, uses this rule:
- 25–50 % repriced;
- spot at least 1σ over 250 ms;
- the token's own 500 ms move is in the same direction;
- the ask is not above fair;
- the edge, taken from a training continuation table, is at least 1c;
- a FOK buy at the ask;
- a sell after 2 s.

The first study (`docs/repriced_momentum_2026-09-24.md`) already found no edge in that design. This study redesigns the idea from scratch: new shock definitions, cross-exchange and order-book features, several entry and exit styles, and an expected-value model.

## 2. Existing strategy performance (baseline B2)

The paper rule above, evaluated with the same replay as everything else (exit 2 s):

| period | orders | fills | net c/share | t |
|---|---:|---:|---:|---:|
| training | 127 | 70 | −0.19 | −0.1 |
| validation | 51 | 24 | −2.42 | −2.0 |
| unseen 1 | 39 | 18 | −1.46 | −3.4 |
| unseen 2 | 48 | 23 | −1.84 | −1.8 |
| unseen 3 (fresh) | 19 | 8 | −2.26 | −2.3 |

It lost in every period after training. Since its paper start (17:07 UTC) the live paper bot has placed no orders. Most candidates are rejected because the token has already repriced past 50 %, or because the spot or the token is no longer moving the shock's way.

## 3. Data

**Chart archive (`data/charts`).**
- 3,108 five-minute windows over 7 assets (BNB, BTC, DOGE, ETH, HYPE, SOL, XRP), from 09-22 15:30 to 09-24 19:10 UTC.
- A point every ~216 ms.
- Each point has:
  - the composite spot price and **each exchange's own price** (Binance, Bybit, Coinbase; all three on 56–79 % of points);
  - spot order flow: 15 s trade-flow imbalance, spot book imbalance, trade rate;
  - both tokens' **top of book** (bid, ask and size on each side).
- It does **not** have Polymarket trades or depth beyond the touch.

**Tick recording (`data/ticks`).** 1.5 h (09-23 20:41–22:11) of venue-timestamped Polymarket books (two levels) and trades, plus exchange-timestamped spot. It is too short for a strategy test. It is used for the lead/lag at 100 ms resolution, from the earlier spot-lag study (section 8).

**Feature cache (`data/replay/rm_research_cache`).**
- The model probability comes from the analytic TWAP model with the measured 0.18 s information lag.
- Sigma comes from the two-scale EWMA.
- Fair value is the market-implied curve frozen on an earlier training slice.

**Periods, chronological by 5-minute epoch.** The first four are the previous study's exact split. The fifth is data no study had examined in any form.

| period | epochs | windows opening (UTC) |
|---|---:|---|
| TRAINING | 256 | 09-22 15:30 → 09-23 18:20 |
| VALIDATION | 103 | 09-23 18:25 → 09-24 02:55 |
| UNSEEN 1 | 77 | 09-24 03:00 → 09-24 09:20 |
| UNSEEN 2 | 77 | 09-24 09:25 → 09-24 16:25 |
| UNSEEN 3 (fresh) | 33 | 09-24 16:30 → 09-24 19:10 |

Unseen 1 and 2 had been scored, but for other strategies, by the first study. Unseen 3 had never been touched.

**Candidates.** Every archive point in the 5 s after a spot shock, with a valid book, an expected move of at least 1c, repricing from −25 % to 300 %, an ask between 0.03 and 0.97, a spread of at most 5c, and at least 5 shares at the ask. There are two shock sets:
- **spotlag:** the live rule (≥ 3σ 1 s move, the 3 s move agrees, accelerating). 218,884 rows over 11,904 shocks.
- **broad:** ≥ 2σ, with the 3 s and acceleration conditions kept as features instead of filters. 610,825 rows over 34,313 shocks.

## 4. Features investigated

All features are causal. `tests/test_repriced_research.py` rewrites a window's future and checks that every feature before the rewrite is bit-for-bit unchanged.

| group | features |
|---|---|
| shock | 1 s move at the shock (z1), 3 s move, acceleration, **peak z1 so far**, duration (seconds above 1σ), same-direction shocks in the prior 30 s, persistence (spot move since the shock) |
| cross-exchange | each exchange's 1 s z at the shock, how many are ≥ 2σ, min/max, **first mover** (first to 1.5σ), how many move our way over the last 500 ms, exchanges available |
| repricing | repricing %, expected and actual move, **gap to fair (fair − ask)**, overshoot, token price, spread |
| momentum now | spot z over 250 ms / 500 ms / 1 s / 2 s; token move over 250 ms / 500 ms / 1 s; accelerations; token velocity since the shock; pullback from and breakout of the post-shock high |
| Polymarket book | ask size, bid size, **imbalance** (bid / both), ask-size change over 500 ms (liquidity disappearing), spread change |
| spot order flow | trade-flow imbalance (15 s), spot book imbalance, trade rate, signed to the shock |
| context | sigma, time left, time since shock, direction, asset, fee rate |

**Outcomes, used for diagnostics only and never as inputs:**
- the mid move at +0.25 / 0.5 / 0.75 / 1 / 1.5 / 2 / 3 / 5 / 10 s;
- the executable bid at those horizons;
- the distance to fair at +1 / 3 / 5 s;
- the maximum favourable and adverse excursion over 5 s;
- the time to the best bid and the time to a 0.5c reversal.

The archive cannot resolve 100 ms, because points are about 220 ms apart.

## 5. Shock size

Repriced ≥ 25 %, first row per shock, FOK at the ask. c/share (fills, t).

| peak shock | shocks train / val | mid +1 s (train) | zero-latency round trip +1 s | executed 2 s, TRAIN | executed 2 s, VALIDATION |
|---|---|---:|---:|---|---|
| 2–3σ | 11,107 / 4,136 | +0.54 | −3.68 | −3.90 (6,573, t −28.6) | −4.28 (2,543) |
| 3–4σ | 4,024 / 1,799 | +0.92 | −3.28 | −3.86 (2,216) | −4.36 (1,016) |
| 4–5σ | 1,518 / 702 | +1.08 | −3.11 | −3.54 (845) | −4.04 (344) |
| 5–6σ | 622 / 295 | +1.50 | −2.63 | −3.69 (340) | −2.77 (150) |
| 6–8σ | 443 / 198 | +1.69 | −2.53 | −3.20 (238) | −3.84 (98) |
| 8–10σ | 167 / 44 | +2.23 | −1.85 | −4.48 (82) | −2.59 (18) |
| ≥ 10σ | 151 / 50 | +3.16 | −1.60 | −2.27 (64, t −2.8) | −5.33 (16) |

Continuation does grow with shock size: from +0.5c to +3.2c a second later. The theoretical round trip improves with it, but stays negative even at zero latency, and executed results stay at −2 to −5c at every size.

Shock persistence and a second consecutive shock made no difference (−3.5 to −4.8c in every bucket, training and validation). Shock duration and acceleration go into the model (section 13).

## 6. Repricing (full range)

Every row, first per shock, executed at 2 s.

| repriced | shocks (train) | mid +1 s | executed 2 s TRAIN | VALIDATION |
|---|---:|---:|---|---|
| −25–0 % | 961 | +1.53 | −3.21 | −3.16 |
| 0–10 % | 2,926 | +1.75 | −2.19 | −2.84 |
| 10–25 % | 2,296 | +1.74 | −3.34 | −3.03 |
| 25–40 % | 3,558 | +1.30 | −3.31 | −3.76 |
| 40–50 % | 3,748 | +0.81 | −3.56 | −3.72 |
| 50–60 % | 4,522 | +0.74 | −3.79 | −3.84 |
| 60–70 % | 5,651 | +0.60 | −3.64 | −3.74 |
| 70–80 % | 6,731 | +0.47 | −3.88 | −3.87 |
| 80–90 % | 7,536 | +0.39 | −3.85 | −4.10 |
| 90–100 % | 7,855 | +0.22 | −3.82 | −4.07 |
| 100–110 % | 7,401 | +0.02 | −3.87 | −4.36 |
| 110–125 % | 7,614 | −0.13 | −3.96 | −4.22 |
| 125 %+ | 7,719 | −0.43 | −4.19 | −4.80 |

- **Higher repricing is worse, monotonically:** less continuation, and past 100 % the token mean-reverts.
- **The least-repriced bucket (0–10 %) comes closest,** but that is spot-lag's territory (tokens that have *not* repriced), and it still loses.

**High repricing plus very strong momentum** (spot ≥ 2σ over 500 ms, token following), exit 2 s, training:

| repriced | 50–70 % | 70–90 % | 90–110 % |
|---|---:|---:|---:|
| net c/share (fills) | −3.34 (1,103) | −4.08 (1,889) | −3.72 (2,303) |

So strong momentum does **not** create a profitable subset among highly repriced tokens.

## 7. Momentum

Spot 500 ms move, repriced ≥ 25 %, token following, training:

| spot 500 ms | reversing | 0–0.5σ | 0.5–1σ | 1–2σ | ≥ 2σ |
|---|---:|---:|---:|---:|---:|
| mid +1 s | −0.09 | +0.15 | +0.11 | +0.15 | +0.90 |
| executed 2 s | −4.12 | −4.10 | −4.06 | −3.98 | −3.89 |

- **Time since the shock:** entries in the first 0.3 s are the least bad at −3.85c; every later window is −4.0 to −4.1c.
- **Token velocity, acceleration, spot acceleration:** used in the model (section 13); no single bucket is positive.

## 8. Cross-exchange

Repriced ≥ 25 %, both moving our way, executed 2 s.

| | train | validation |
|---|---:|---:|
| **0** exchanges ≥ 2σ at the shock | −4.48 | −4.80 |
| 1 | −4.22 | −4.61 |
| 2 | −3.82 | −4.53 |
| **all 3** | −3.49 | −3.73 |
| first mover: Binance | −3.95 | −4.35 |
| first mover: Bybit | −4.05 | −4.62 |
| first mover: **Coinbase** | −3.39 | −3.77 |
| exchanges moving our way *now*: 0 | −4.41 | −4.55 |
| … **3** | −3.44 | −3.63 |

- **Agreement across exchanges helps.** When all three confirm, the token moves about 1c more (+1.2c against +0.1c a second later) and the loss shrinks by about 1c. It is still −3.4 to −3.7c.
- **Coinbase-first shocks lose least,** but that is 1.6k of 14k shocks, and they still lose.
- **No single exchange's own move predicted better than the composite** (they enter the model separately).
- **Lead/lag at 100 ms** (tick recording, `docs/spot_lag_study_2026-09-23.md` §B): on the exchanges' own clocks, the token reaches half of its 5 s response 100–300 ms after the spot move. Our spot data arrives 116–147 ms after the exchange stamp, and our order takes another 130–450 ms. By the time the order arrives, **75–87 % of the repricing is done**. The repriced population is, by construction, the slow tail.

## 9. Order book

Repriced ≥ 25 %, agreeing, executed 2 s, train / validation.

| Polymarket book imbalance (bid size / both) | 0–0.2 | 0.2–0.4 | 0.4–0.6 | 0.6–0.8 | **0.8–1** |
|---|---:|---:|---:|---:|---:|
| mid +1 s | −0.83 | −0.33 | +0.13 | +0.67 | **+1.92** |
| executed 2 s | −4.06 / −4.12 | −4.26 / −4.56 | −4.15 / −4.41 | −3.93 / −3.86 | −3.74 / −3.59 |
| fill rate | 79 % | 73 % | 66 % | 58 % | **40 %** |

**Book imbalance is the strongest single predictor of continuation** (+1.9c against −0.8c). **It is also the clearest example of adverse selection.** When the bid side is heavy the token keeps rising, so fewer orders fill (40 %), and the fills that do happen are the ones where it stopped.

The other book features point the same way:
- **Ask liquidity disappearing** (ask size down more than 50 shares in 500 ms): +1.1c continuation, −3.8c executed.
- **Spread:** 1c spreads lose least (−3.7c), 4–5c lose most (−5.1c).
- **Spot trade-flow imbalance in our direction:** +1.0c continuation, still −3.7 to −4.1c executed.

**Not testable:** Polymarket trade direction and aggressive-buyer flow, and depth beyond the touch, are not in the archive. The 1.5 h tick recording has them but cannot support an out-of-sample test. Record Polymarket trades in the chart recorder if this is ever to be revisited.

## 10. Entry experiments

| entry | how it was tested | executed (train) |
|---|---|---|
| A. Immediate | first qualifying row after the shock | −3.85c (first 0.3 s) |
| B. Confirmation | the same conditions still true 0.3–5 s later | −4.0 to −4.1c |
| C. Pullback | token ≥ 1c / 2c / 3c below its post-shock high, spot still our way | −4.13 / −4.43 / −4.19c |
| D. Breakout | token at a new post-shock high | −4.10c (against −3.90c for no breakout) |
| E. Limit / "cheap to fair" | enter only when the ask is ≥ 4c or ≥ 8c below fair | −4.53c (4–8c), −2.40c (≥ 8c, 114 fills, fill rate 29 %) |
| F. Dynamic cap | limit = the highest price still leaving 1c after both fees if the token reaches fair | fill rate 47–58 %; −3.0 to −4.0c (C11, C12, C15, C16) |
| + crossing | limit at ask + 1c (first study) | more fills, no better |
| + maker entry | resting bid (first study) | 10–19 % fills, worse (adverse selection) |

Pullbacks catch the reversal, and breakouts pay the top. The "cheap to fair" entry has the best economics on paper: +5.9c continuation in a second, and a positive zero-latency round trip. But only 29 % of its orders fill, and the fills lose.

## 11. Exit experiments

22 exits, repriced ≥ 25 %, agreeing, first row per shock (broad set):

| exit | train | validation |
|---|---:|---:|
| 0.25 / 0.5 / 0.75 / 1 / 1.5 / 2 / 3 / 5 / 8 / 10 s | −3.94 / −3.92 / −3.97 / −3.96 / −3.90 / −3.94 / −3.84 / −3.74 / −3.83 / −3.81 | −4.48 / −4.49 / −4.46 / −4.39 / −4.38 / −4.42 / −4.30 / −4.36 / −4.15 / −4.01 |
| spot reversal / token reversal / momentum collapse | −3.88 / −3.84 / −3.92 | −4.41 / −4.42 / −4.50 |
| fair reached / fair exceeded | −3.71 / −3.68 | −4.22 / −4.22 |
| book reversal / spread expansion / cross-exchange lost | −3.68 / −3.82 / −3.89 | −4.14 / −4.33 / −4.47 |
| **maker** sell at +1c / +2c / +3c / at fair (else taker at 5 s) | −4.23 / −4.14 / −4.02 / −4.10 | −4.88 / −4.76 / −4.67 / −4.75 |

The exit moves the result by at most about 0.3c. The entry is what loses.

The maker exits are counted conservatively: a resting sell fills only when the venue bid trades **through** it. They do *worse*, because the fee saved on the winners is less than what the misses cost when the fallback exit happens later and lower.

## 12. Execution analysis: where the edge disappears

All periods, first row per shock, per share. The table walks from the mid move to the executed result. MFE is the maximum favourable excursion: the best executable bid within 5 s, minus the decision ask.

| population | shocks | mid +2 s | MFE − fees (perfect hindsight) | round trip, zero latency, **no fees** | round trip, zero latency | **executed** | executed at half fees | executed, **no fees** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| repriced ≥ 25 %, agreeing | 31,787 | +0.65 | −0.04 | −1.32 | −3.67 | **−4.10** | −2.98 | −1.85 |
| repriced 25–50 %, agreeing | 5,808 | +2.37 | +1.61 | +0.41 | −1.89 | **−3.25** | −2.21 | −1.17 |
| repriced 50–90 %, agreeing | 17,146 | +1.16 | +0.41 | −0.74 | −3.15 | **−3.83** | −2.69 | −1.54 |
| shock ≥ 8σ, repriced ≥ 25 % | 478 | +2.24 | +2.18 | +0.22 | −1.81 | **−4.28** | −3.29 | −2.30 |
| gap to fair ≥ 4c | 3,516 | +3.81 | +3.96 | +1.88 | −1.08 | **−3.99** | −2.52 | −1.04 |
| EV model top decile | 6,314 | +0.60 | +0.15 | −0.59 | −1.58 | **−1.88** | −1.42 | −0.96 |

**Faster execution** (the same orders re-simulated, with the venue book lag shrunk in proportion):

| population | measured (book +500 ms) | ×0.5 (+250 ms) | ×0.25 (+125 ms) | ×0.1 (+50 ms, co-located) |
|---|---:|---:|---:|---:|
| repriced ≥ 25 %, agreeing | −4.14 (59 % fill) | −4.28 | −4.18 | −4.23 (70 %) |
| gap to fair ≥ 4c | −4.05 (34 %) | −3.52 | −2.52 | −2.64 (47 %) |
| shock ≥ 8σ | −4.00 (47 %) | −4.15 | −3.14 | −3.22 (53 %) |
| EV top decile | −1.89 (61 %) | −1.91 | −1.82 | −1.84 (73 %) |

The theoretical edge disappears in three steps:
1. **Spread.** Going from the mid to the ask and back to the bid costs 1–2c. The zero-latency, no-fee round trip is positive only where the token is far from fair (gap ≥ 4c, +1.9c) or still early (25–50 %, +0.4c).
2. **Fees.** 0.07·p(1−p) on each leg is about 3.5c at mid prices. That turns every population negative even at zero latency.
3. **Latency and adverse selection.** 1–3c more. Orders that arrive after the token has moved are killed, and the ones that fill are the ones where it did not move.

**What it would take.** For the best population (gap ≥ 4c), fees would have to go to zero (−1.0c) **and** execution would have to be co-located (worth about +1.4c here). Together that is roughly break-even: a few tenths of a cent per share, 5 shares at a time, on a population of about 70 signals an hour across the seven assets. Neither lever alone is enough.

## 13. Walk-forward results

**The expected-value model.** A ridge regression of *executed* net P&L (winsorised at ±20c) on about 40 decision-time features, including one-hot asset and first mover. The penalty is chosen by chronological cross-validation inside the fitting data only.

Validation predictive power (fit on training):

| set / exit | rank corr, predicted vs realised | realised net by predicted fifth (low → high) |
|---|---:|---|
| broad, 1 s | +0.50 | −6.11 / −4.90 / −4.21 / −3.39 / **−2.12** |
| broad, 2 s | +0.39 | −6.10 / −4.94 / −4.10 / −3.38 / **−2.05** |
| broad, 5 s | +0.27 | −6.01 / −4.86 / −3.85 / −3.22 / **−1.94** |
| spotlag, 1 s | +0.51 | −6.04 / −4.70 / −4.15 / −3.23 / **−2.02** |

The model ranks real, out-of-sample differences: its top fifth loses 4c less than its bottom fifth. Part of that is mechanical (fee size depends on price, and spread varies), part is genuine continuation (book imbalance, cross-exchange agreement, gap to fair). **No slice is positive.** On validation its top 10 %, 5 % and 1 % lose 1.2–2.2c, whichever shock set or exit is used.

**Walk-forward procedure** (locked before any unseen period was read):
- Refit on *all* earlier periods.
- Threshold at the 99th percentile of the in-sample predictions.
- Trade the first qualifying row per shock with a FOK at the ask.
- Sell after 5 s.

| test period | fit on | threshold | orders | fills | net c/share | t |
|---|---|---:|---:|---:|---:|---:|
| validation | training | −1.22c | 150 | 60 | −1.18 | −2.4 |
| unseen 1 | training + validation | −1.20c | 170 | 84 | −1.42 | −3.6 |
| unseen 2 | … + unseen 1 | −1.25c | 239 | 121 | −1.45 | −4.2 |
| unseen 3 | … + unseen 2 | −1.29c | 112 | 57 | −1.67 | −3.9 |
| **all unseen** | | | 521 | 262 | **−1.48** | **−6.6** |

The model predicts, correctly, that even its best trades lose about 1.2–1.3c. Realised results are slightly worse.

## 14. Unseen results

**Selection** (`data/replay/rm_research/select.md`):
- 55 candidates in 6 hypothesis families, not a blind grid.
- 31 had at least 30 training fills. **One** was positive on training: shocks ≥ 10σ, held 10 s, +0.79c, t 0.5. It lost 5.5c on validation.
- The five best by training t went to validation. **All five lost** (−3.6 to −5.5c).

The locking rule was fixed in advance: the best validation result, which could still lose.
- **Locked rule C13:** remaining gap ≥ 8c (broad set), FOK at the ask, exit 2 s. Training −2.40c, validation −3.72c; it did not pass validation.
- **Locked walk-forward procedure:** the expected-value model (section 13). It did not pass validation either (−1.18c).

| | unseen 1 | unseen 2 | unseen 3 (fresh) | pooled |
|---|---:|---:|---:|---:|
| **C13 gap ≥ 8c** | −3.44 (28 fills) | +2.43 (29) | +13.69 (3) | **+0.25** (60, t +0.1, 95 % CI −4.7 to +5.2) |
| **EV walk-forward** | −1.42 (84) | −1.45 (121) | −1.67 (57) | **−1.48** (262, t −6.6) |

C13's pooled +0.25c is noise:
- it is negative on training and validation;
- over validation and unseen together it is −1.47c on 106 fills;
- the unseen-3 figure is 3 trades;
- by asset it swings from ETH −6.6c to BTC +12.6c.

## 15. Baseline comparison

| | training | validation | unseen 1 | unseen 2 | unseen 3 |
|---|---:|---:|---:|---:|---:|
| B1 no trade | 0 | 0 | 0 | 0 | 0 |
| B2 current repriced-momentum rule | −0.19 | −2.42 | −1.46 | −1.84 | −2.26 |
| B3 naive: any 50–90 % repriced token | −3.66 | −3.87 | −3.53 | −3.93 | −3.29 |
| B4 spot-lag (its own rule and exit, evaluated separately) | +11.19 | +3.81 | +0.71 | +3.48 | +20.07 (7 fills) |
| C13 locked rule | −2.40 | −3.72 | −3.44 | +2.43 | +13.69 (3 fills) |
| EV walk-forward | — | −1.18 | −1.42 | −1.45 | −1.67 |

Net c/share, exit 2 s except B4 and the EV walk-forward.

**No trading (B1) beats every repriced-token rule** in every period after training. Only C13 has two positive unseen periods, and those rest on 29 and 3 trades. Spot-lag (B4) is the only strategy positive in every period. It trades the *opposite* population, tokens that have not repriced yet.

## 16. Risk analysis

**Under the strategy's own budget** (1 position, $3 per order, so asks ≤ 0.60; $5 daily loss) on the unseen periods:
- **C13:** 22 fills, +0.82 USDC, max drawdown 2.46. 89 orders were skipped by the order cap.
- **EV walk-forward:** 75 fills, **−5.10 USDC**, max drawdown 5.29. It hit the $5 daily stop.

**Stress tests** (validation and unseen pooled):

| scenario | C13 | EV walk-forward |
|---|---:|---:|
| as locked | −1.47 (106) | −1.43 (322, t −6.9) |
| +0.5c / +1c slippage per leg | −2.47 / −3.46 | −2.42 / −3.41 |
| spread wider by 1c / 2c | −2.47 / −3.46 | −2.42 / −3.41 |
| 25 % of fills lost at random / the best 25 % lost | −0.95 / −6.53 | −1.24 / −2.41 |
| 20 % of quotes missing | −0.71 | −1.40 |
| re-simulated, measured latency (control) | −1.00 | −1.36 |
| latency +10 / +25 / +50 % | −1.22 / −1.75 / −2.05 | −1.44 / −1.32 / −1.38 |
| venue book read 750 / 1000 ms late | −2.61 / −1.58 | −1.35 / −1.59 |

There is no edge to protect. Worse execution adds roughly its own cost.

## 17. Failure cases

1. **Adverse selection is built in.** Every feature that predicts continuation (bid-heavy book, ask liquidity disappearing, large gap to fair, three exchanges confirming) also predicts that the ask runs away before the order arrives. Fill rates fall from 70–80 % to 29–40 % exactly where continuation is largest. The fills that remain are the stalls.
2. **Fees dominate.** About 3.5c per round trip at mid prices, against a median 1–2 s continuation of 0.5–1c. Low-fee prices (< 0.2 or > 0.8) lose least (−2.5c) but have the smallest moves.
3. **The repriced population is late by definition.** 75–87 % of the repricing is done before an order can land (section 8).
4. **Small-sample mirages.** Shocks ≥ 10σ (+0.8 to +1.4c on training, around 60 fills) and huge shocks with low repricing (+3.8c on training, 24 fills) looked promising. The first lost on validation. The second was a post-hoc look outside the locked candidates: it was positive on validation (8 fills) but near zero on unseen (−1.1c on 10 fills, +2.3c on 6). It is also spot-lag territory, not repriced tokens.
5. **A bug found and fixed during this study.**
   - Features were stored as float32. A decision ask of 0.88 read back as 0.8799999952, so an order arriving with the ask unchanged at 0.88 was scored as *unfilled*. That understated every fill rate and removed exactly the neutral fills.
   - It was caught when the latency re-simulation disagreed with the stored dataset.
   - The fix works in float64, with prices rounded back to the cent grid, and is covered by a regression test.
   - **Every phase was re-run from scratch** with the same pre-registered candidates and selection rules. The buggy evaluation of the unseen periods had already been computed; it is kept, labelled, in `data/replay/rm_research/*_BUGGY_float32.*`. The conclusion did not change: before the fix the locked candidates were −2.18c and −2.23c on unseen data; after it, +0.25c (noise) and −1.48c.

## 18. Final strategy specification

**None is recommended.** For the record, the closest candidates are:
- **C13 (rule).** Shock ≥ 2σ, token ≥ 25 % repriced, spot and token moving our way over 500 ms, ask ≥ 8c below fair, FOK at the ask, sell after 2 s. Unseen: noise around zero (+0.25c, 95 % CI ±5c). Negative on training and validation.
- **EV walk-forward.** The ridge expected-value model, top 1 % of in-sample predictions, FOK at the ask, 5 s exit. Consistently −1.4 to −1.7c.

The conditions under which the idea could work are stated in section 12: taker fees near zero **and** co-located execution, together, in the gap ≥ 4c population. Neither holds today.

## 19. Why the strategy should work

The economic story is sound as far as it goes. The Polymarket token does lag spot:
- Half of its 5 s response comes within 100–300 ms.
- The rest arrives over several seconds.
- Continuation is stronger after bigger shocks, when all exchanges agree, and when the Polymarket book is bid-heavy.
- It is stable across periods: the forward-move tables match between training and unseen data within tenths of a cent.

## 20. Evidence that it actually works

**There is none.** What was checked:
- 5 chronological periods, 7 assets, 2 shock definitions and 6 hypothesis families, including an expected-value model with real out-of-sample ranking power.
- 6 entry styles and 22 exits.
- Latency from 0.1× to 1.5× the measured value, and fee rates from zero to the current 7 %.
- Exploration: about 5,500 executed table cells. After the fix, 9 cells with 30 or more fills are positive. None has a t-statistic above 1.1, and 5 of them are in the 0–10 % spot-lag region or are validation cells with 61 fills. That is about what chance produces when most cells are strongly negative.
- Selection: 1 of 31 eligible candidates positive on training, 0 of 5 on validation, 0 of 2 locked candidates robustly positive on unseen data.

## 21. Remaining uncertainty

- **One regime.** About 52 hours of data. A calmer or much more volatile market could change continuation sizes, though the fee arithmetic would not change.
- **The fill model.** It is a validated proxy: it reproduced venue-truth P&L on 694 spot-shock orders. It was not validated specifically on repriced entries.
- **Maker fills.** Their conservative rule (the bid must trade *through* the resting price) probably understates maker exits. They lost more than taker exits, so even an optimistic rule would have to swing about 1c to matter.
- **Untested inputs.** Polymarket trade flow and depth beyond the touch are not in the archive.
- **The fee schedule.** A fee cut is the one external change that would justify re-running this study. `scripts/research_repriced_momentum.py cache && build` then `scripts/analyze_repriced_momentum.py explore|select|evaluate|diagnostics|stress` reproduces everything deterministically.

---

## Addendum: the paper bot's settings (2026-09-24, 19:55 UTC)

**Why zero orders.** The paper bot ran the rule locked in the first study, and in its first 2.7 hours it sent **no orders**:
- 607 shocks were seen.
- 73 % were stopped as repriced past 50 %.
- 11 % were stopped because the spot or the token had turned.
- The 3 % that reached the **expected-edge gate** were all stopped there. The continuation (1.5–4c) never covered a round trip (4–6c).

**The ladder.** `scripts/analyze_repriced_momentum.py livesim` replays the bot's own gates, its 2 s retry, 3 attempts per market, 1 position, the $3 order cap and the $5 daily stop. Figures are over all five periods; the daily stop is off in the first column (on in brackets):

| settings | orders / h | fills / h | fill rate | net per share | USDC / day at 5 shares |
|---|---:|---:|---:|---:|---:|
| L0 as it ran (edge gate on) | 1.3 | 0.5 | 42 % | +2.7c (24 fills, noise) | +1.7 |
| **L1: edge gate off** | 12.7 (8.5) | 3.8 (2.6) | 30 % | **−0.8c** | −3.7 (−1.4) |
| L1, limit 1c / 2c above the ask | 12.4 / 11.9 | 4.5 / 5.1 | 36 % / 43 % | −1.1 / −1.2c | −6.1 / −7.6 |
| L1 with a $5 order cap | 22.2 | 7.2 | 33 % | −2.0c | −17.7 |
| 25–90 % or 25–100 % repriced, gate off | 47–51 | 21–24 | 45–48 % | −3.7 to −4.0c | −93 to −115 (the $5 stop ends each day within about an hour) |

**Applied to paper:** L1. The edge gate is off and everything else is unchanged, including a limit at the ask; the original brief said not to cross the spread just to raise the fill rate. In the first 8 minutes it sent 2 orders: one killed, one filled and sold 2 s later (−$0.62).

**It remains unproven.** L1 was picked for its order rate among settings evaluated on every period, so its −0.8c is not an out-of-sample result. Its per-period results are −1.9, +0.5, +0.1, +1.9 and −1.0c.


## Addendum: take-profit exits (2026-09-24, live in paper from 20:10 UTC)

**The setting.** "Sell once the price has risen X cents":
- `TPB_REPRICED_MOM_TAKE_PROFIT_CENTS` sets X;
- `TPB_REPRICED_MOM_STOP_LOSS_CENTS` is an optional stop;
- `TPB_REPRICED_MOM_HOLD_S` is the fallback time limit (≤ 0 means hold to settlement).

The live bot sells **at the bid**, and only once the trade also clears its 0.5c minimum after both fees. At mid prices a +2c or +3c target therefore effectively waits for about +4c.

**The simulation.** `analyze_repriced_momentum.py tpsim` runs every order the live bot would have filled, tick by tick. The bot's own gates select them, one position is held at a time with the $3 cap, and the daily stop is off:

| exit, on the running entry rule (L1) | win rate | avg hold | net per share | t | train / val / u1 / u2 / u3 |
|---|---:|---:|---:|---:|---|
| sell after 2 s (before) | 26 % | 2 s | −0.82c | −1.0 | −1.9 / +0.5 / +0.1 / +1.9 / −1.0 |
| **+3c, else 10 s (now running)** | 49 % | 8 s | **+0.28c** | +0.3 | −0.6 / +1.3 / +0.9 / +1.6 / +1.9 |
| +2c, else 10 s | 52 % | 8 s | +0.26c | +0.3 | −0.7 / +1.6 / +0.9 / +1.8 / +1.4 |
| +5c, else 60 s | 63 % | 37 s | +0.57c | +0.4 | +0.7 / −1.4 / +2.3 / +0.7 / +0.7 |
| +8c, else 60 s | 54 % | 43 s | −0.39c | −0.3 | |
| +3c, else settlement | 75 % | 62 s | −0.00c | −0.0 | |
| +5c, stop −5c, else settlement | 45 % | 17 s | −0.00c | −0.0 | |
| hold to settlement | 32 % | 205 s | +0.23c | +0.1 | … / −29.1 (u3) |

- **On the running entry, a take-profit turns a small loss into roughly break-even.**
- **On the wider zone (L3), every take-profit still loses 2.7–3.7c.**
- **None is statistically meaningful.** The largest t is 0.4, and 15 variants were compared on every period, so the best of them is expected to look better than it is.

**Why a take-profit cannot create an edge by itself.** It raises the win rate by selling small rises early. The trades that do not rise are still sold at the fallback. If the token price does not drift, any rule for when to sell gives the same average: what was paid, minus the spread and fees. Only a real continuation can make the average positive. Here that continuation is small.

**A bug found by the tests.** A take-profit that the bot refused (fees would eat it) kept the position from ever reaching its time stop. The time limit now takes precedence, and a live-bot test covers it.

**Limit above the ask (live in paper from 20:43 UTC, the user's choice for fill rate).**

The live bot's replay with the +3c take-profit:

| limit | fill rate | fills / h | net per share |
|---|---:|---:|---:|
| at the ask | 29 % | 3.7 | +0.10c |
| ask + 1c | 36 % | 4.4 | −0.74c |
| ask + 2c (now running) | 42 % | 5.0 | −0.84c |

More fills cost about 0.9c per share: roughly break-even becomes a small expected loss. The $5 daily stop bounds it.

**Exit +10c, else 20 s (live in paper from 20:54 UTC, the user's choice).**

Replay with the +2c limit:

| exit | take-profit hit | win rate | avg hold | net per share |
|---|---:|---:|---:|---:|
| +3c, else 10 s (before) | 43 % | 46 % | 8 s | −0.75c |
| **+10c, else 20 s (now running)** | 29 % | 40 % | 17 s | **−0.60c** (t −0.6) |
| +10c, else 10 s | 20 % | 36 % | 10 s | −0.94c |
| +5c, else 20 s | 43 % | 48 % | 15 s | −0.37c |

The new exit is the same as the old one within noise, still a small expected loss.

## Deliverables

| file | what |
|---|---|
| `scripts/research_repriced_momentum.py` | `cache` (features and the period split), `build` (the candidate dataset: features, outcomes, one realistic order, 22 exits), `resimulate` (the same orders at other latencies) |
| `scripts/analyze_repriced_momentum.py` | `explore` (training and validation tables), `select` (train → validation → lock), `evaluate` (locked candidates, baselines, walk-forward), `diagnostics` (the edge decomposition, faster execution), `stress` |
| `backtest/momentum_replay.py`, `backtest/spot_shock_replay.py` | the chart loader and feature cache now carry each exchange's price and the spot order flow (additive; nothing that existed changed) |
| `tests/test_repriced_research.py` | 9 tests: no look-ahead (a rewritten future leaves every feature unchanged), causal returns, the fresh-period split, the float32 fill regression, the maker trade-through rule, latency scaling, the dynamic limit, one entry per shock, the ridge fit |
| `data/replay/rm_research/` | `explore.md`, `select.md`, `locked_spec.json`, `evaluate.md`, `diagnostics.md`, `stress.md`, plus the labelled buggy first run |

The live strategy's entry logic was not changed. Its exits gained an optional take-profit and stop-loss (addendum above). The paper bot's settings are in `ecosystem.repricedmom.config.js`.

**Real money:**
- **repriced_momentum: DISABLED.** The flag defaults to false, and there is no live app for it.
- **Spot-lag live: unchanged and untouched.**
