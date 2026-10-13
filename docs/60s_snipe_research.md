# The 60-second snipe window: research report (2026-09-25)

## Verdict: no meaningful edge

**The 60-second snipe window has no exploitable edge on this data, before or after execution costs.**

- **The strategy as hypothesised loses.** Project fair-value model, entries in the last 60 s, ≥ 2c executable edge, MAX_ASK 0.95, held to settlement: **−3.84c/share on the three unseen periods** (352 fills, t −2.05, 95 % CI −7.5 to −0.2c). It loses in every unseen period, on every asset, on both sides, and in TRAIN and VALIDATION too. It does worse than buying at random in the same window (−2.20c).
- **The market is better informed than the models.** On the books that can actually be traded, the Polymarket mid is better calibrated than every model tested, at every horizon, and the gap widens toward the close. What looks like edge in the last minute is the model being wrong. Among its ≥ 2c signals, the outcome falls about 12c short of the model's own probability on average (5–20c by bucket).
- **There is no spot/token lag left to take.** The late "edges" stay on the book for seconds, and the rows where the token lags the model lose even at zero latency. At zero latency the hypothesised strategy is about break-even (−0.36c); measured latency and adverse selection cost another 3.5c.
- **Every candidate the pre-registered protocol locked is negative out of sample**, from −1.1c to −3.3c/share:
  - The three broad TRAIN winners failed VALIDATION outright. Their TRAIN profit was a down-drift in 09-22/23 that a model fitted on TRAIN frequencies absorbed.
- **The one real-looking pocket fails.** High-priced favourites (ask 0.90–0.98) far from the strike in the last minute were positive on TRAIN and VALIDATION. Out of sample:
  - the locked rule made −2.81c on 27 fills, with 4 losses at ~94c each;
  - across all 739 such configurations, the median fell from +4.9c on VALIDATION to −4.1c on unseen data.

  One loss wipes out 14–20 wins there, and the sample cannot pin the loss rate down.

**Classification: "No meaningful edge"** for the 60-second strategy, and **"edge in training (and validation) that fails out of sample"** for its favourite-band variant and the broad fair-value family.

Reject it as a live strategy. Nothing was enabled, and no live process or setting was touched.

### Final result table (the three unseen periods pooled, 21.1 h, 5-share orders)

| Strategy | Window | MAX ASK | Entry Edge | Exit | Fills | Win Rate | Net P&L | P&L/Share | Drawdown | OOS (unseen 1 / 2 / 3, c/share) |
| -------- | -----: | ------: | ---------: | ---- | ----: | -------: | ------: | --------: | -------: | --- |
| 60-second strategy (pre-registered: model D, spread ≤ 3c) | 60–0 s | 0.95 | 2c | settle | 352 | 34 % | −$67.56 | −3.84c | $70.16 | −6.16 / −0.37 / −4.76; t −2.1 |
| Locked high-volume (model C, any spread) | 180–0 s | 0.98 | 0.5c | settle | 1,451 | 59 % | −$78.50 | −1.08c | $185.35 | +2.49 / −1.14 / −3.92; failed VALIDATION |
| Locked high-expectancy (model D, spread 1c, limit at the ask) | 180–2 s | 0.50 | 15c | stop: edge < 0 at entry price | 94 | 20 % | −$6.67 | −1.42c | $16.41 | −0.45 / −1.39 / −2.35; failed VALIDATION |
| Locked most-stable-on-TRAIN (model C) | 180–0 s | 0.80 | 10c | settle | 813 | 48 % | −$99.30 | −2.44c | $217.65 | +5.19 / −5.34 / −5.74; failed VALIDATION |
| Locked final-minute (model F, spread 1c, limit at the ask) | 45–2 s | 0.90 | 8c | sell after 30 s | 113 | 19 % | −$18.55 | −3.28c | $19.40 | −9.15 / +1.88 / −3.87 |
| Locked favourite band (model D, ask 0.93–0.95) | 45–0 s | 0.95 | 1c | stop: fair value < bid | 27 | 85 % | −$3.80 | −2.81c | $8.64 | +1.72 / +4.69 / −7.63 |
| Random entry (20 seeds) | 60–0 s | 0.95 | — | settle | 433 / seed | 31 % | −$47.60 | −2.20c | $60.62 | −2.03 / −3.07 / −1.66 |
| Buy the side the spot is on | 60–0 s | 0.95 | — | settle | 442 | 83 % | +$45.00 | +2.04c | $49.38 | +6.65 / +5.75 / −5.15; TRAIN −2.10, t +1.0 |
| **Current baseline:** the project's fair-value rule | 180–60 s (its own) | 0.95 | 2c (blend) | settle | 600 | 52 % | −$55.09 | −1.84c | $80.12 | −2.68 / −1.41 / −1.51 |
| No trade | — | — | — | — | 0 | — | $0 | 0 | $0 | 0 |

- **High-volume candidate** (locked high-volume). It trades most, ~69 fills an hour across the seven assets, but not in the last minute: 99 % of its fills come with 120–180 s left. It made +3.8c on TRAIN (t 2.4) and −2.2c on VALIDATION, so it failed validation. Unseen: −1.1c (t −0.7). Its TRAIN profit was the down-drift that model C learned. It is the least-bad locked rule per share, and still worse than not trading.
- **High-expectancy candidate** (locked high-expectancy). +9.2c on TRAIN from 89 fills of cheap tokens (ask ≤ 0.50) with a 15c edge. **It is a latency artifact:** unseen it makes +6.8c when orders fill instantly, and −1.4c to −3.8c at any realistic latency. Half its orders are killed; the fills are the adverse ones.
- **Most stable out-of-sample candidate.** The high-expectancy rule has the smallest spread across the three unseen periods (−0.45, −1.39, −2.35c). It is stable, but stably negative. **No strategy is positive in all three unseen periods.** "Buy the side the spot is on" is the only positive pooled line (+2.0c, t 1.0), but it lost on TRAIN (−2.1c) and in unseen 3 (−5.2c). That is directional regime, not an edge.
- **Current baseline.** The project's own fair-value rule, which deliberately stops at 60 s, makes −1.84c unseen in its own window. Moved into the last 60 s it makes −4.48c. **The existing design choice to stay out of the final minute is confirmed.**

### Answers to the ten questions

1. **Does a genuine exploitable pricing error exist in the final 60 s?** No. The market is calibrated to within about a cent across most of the range, and better than any model. Two small deviations exist, and neither is exploitable:
   - the underdog just on the losing side of the strike is ~8c overpriced (TRAIN, §14), but the complementary buy was +1.3c before latency (t 0.8);
   - near-certain favourites (z > 3) are ~1.4c cheap, but rare (86 markets in 21 h) and exposed to the settlement-basis tail.
2. **Is it large enough to survive spread, fees, slippage and latency?** No. At zero latency the hypothesised strategy is already about zero (−0.36c). Measured latency makes it −3.84c. The fills are adversely selected: on TRAIN and VALIDATION they land 2–3c below the decision ask.
3. **Spot/token lag or probability mispricing?** Neither is tradable.
   - The lag rows lose even at zero latency (−4.2c in the last 60 s): a token that does not follow the model is carrying information.
   - The static disagreements are about zero before costs.
   - The model's "edge" is its own error: the Chainlink basis plus information the market has and the model does not.
4. **Does holding to settlement beat selling quickly?** For these entries, yes, but only as the least-bad exit: it saves the second fee and the exit spread. No exit (54 tested) turns a losing entry into a winner.
5. **Does MAX_ASK materially affect profitability?** Not in the final minute: the model's trades there are mostly underdogs, so MAX_ASK 0.60 → 0.98 moves the result by under 1c. MIN_ASK is the parameter that matters: everything under ~0.70 loses. Even the high band fails out of sample.
6. **Which token-price range has the strongest out-of-sample edge?** None has a positive one in the final minute. The 0.90–0.99 favourites won on TRAIN and VALIDATION and lost on unseen data (§16). The only near-zero pocket is favourites ≥ 0.90 bought over the whole last 3 minutes and held (median +0.3c across 434 configurations): not the final minute, and within noise.
7. **Which time window has the strongest out-of-sample edge?** No window is positive. Across the grid, settlement-held entries are least bad at 90–120 s (median −1.2 to −1.5c unseen) and worst at 20–30 s (median −6.3 to −6.9c). **Later entry is worse, not better.**
8. **Across assets or only one?** No asset works. The hypothesised strategy loses on all seven out of sample (−0.6 to −9.2c).
9. **Does it survive realistic latency?** There is nothing to survive. Latency ×1.25, ×1.5 and ×2 leave every candidate negative, and zero latency does not rescue the final-minute rules.
10. **Does it survive removal of extreme winners?** Removing the top 1 / 5 / 10 % of winners takes the hypothesised strategy from −3.8c to −4.9 / −8.1 / −11.3c. Every candidate gets worse. There was no positive result to be carried by outliers.


---

## 1. Strategy hypothesis

In the last ~60 s of a 5-minute UP/DOWN market, the token price may stray from the probability implied by:
- the latest spot;
- the strike;
- the time left;
- short-term volatility.

The strategy buys the token whose **executable** edge clears a threshold. Executable edge is the model probability − ask − taker fee − a 0.5c slippage allowance. It then either sells before expiry or holds to settlement.

The study tests three things:
- whether that mispricing exists at all;
- whether it survives the spread, fees, slippage and this machine's latency on data it was not tuned on;
- where it lives: time left, token price, asset, side, regime.

Nothing in the live system was touched. No order was placed, no process restarted, no environment variable changed.

## 2. Dataset

**Chart archive (`data/charts`).** The live bots record one JSON file per market, 3,507 markets across 7 assets, from 2026-09-22 15:30 to 2026-09-25 01:15 UTC. Each file holds:
- the composite spot (Binance, Bybit, Coinbase) and each exchange's own price;
- both tokens' top of book (bid, ask and size on each side, `None` when absent);
- spot order flow;
- the live bot's strike;
- the venue's resolved outcome.

A point is recorded about every 216 ms, from 60 s before the open to about 30 s after the close. **Since the sampler fix in `docs/replay.md`, the archive runs through the bell.** The one-sided books of a decided market are recorded as served, so a replay cannot buy a winner nobody offered.

**What the archive cannot resolve:**
- anything faster than ~216 ms (the 50 ms and 100 ms momentum lookbacks);
- Polymarket trades;
- depth beyond the touch.

The 100 ms lead/lag in the final minute was measured on the 1.5 h tick recording (`docs/spot_lag_study_2026-09-23.md` §C): the token's median lag is 100 ms with under 60 s left.

**Feature cache (`data/replay/snipe60/cache`, built by `scripts/research_60s_snipe.py cache`).** Every point with ≤ 182 s left, 2,748,750 points. All features are causal:
- the project's `TwoScaleVol` sigma, stitched across each asset's windows;
- the expected settling TWAP and its remaining sd, with the 0.18 s information lag;
- the project fair value `twap_fair_value`, vectorised. It matches the live function to 1e-11 (test);
- spot returns at 250 ms – 10 s;
- realised vol over 30 s and 60 s;
- the 60 s and 120 s trend;
- the token's and the model's 1 s and 3 s moves;
- the books and order flow.

`tests/test_snipe60_research.py` rewrites a window's future at 150, 60, 20 and 3 s before the close. It checks that every feature at or before the cut is bit-for-bit unchanged.

**What settles the market (verified before anything was built).** The venue resolves UP iff the 60 s TWAP at the close ≥ the 60 s TWAP at the open, as `pricing/twap.py` assumes. Against 3,254 venue-resolved windows:

| rule, on our composite spot | agrees with the venue |
|---|---:|
| TWAP(close) ≥ TWAP(open) | **98.3 %** (Binance alone 98.8 %) |
| spot(close) ≥ TWAP(open) | 92.8 % |
| spot(close) ≥ spot(open) | 88.9 % |

In the 330 windows where the spot rule and the TWAP rule disagree, the TWAP rule is right 318 times. So inside the last minute **part of the settling average is already locked**, and its uncertainty collapses as u³ rather than √u. The remaining ~1.5 % is the **basis** between our composite and Chainlink's stream. Fitted on TRAINING windows, it behaves like noise with sd = 1.5 × the per-second spot sigma, about 1–3 bps (§5).

## 3. Data coverage

The periods are the project's established ones (`docs/repriced_momentum_research_2026-09-24.md`), reused exactly, plus the windows archived while this study ran:

| period | from (UTC) | to | epochs | markets | hours | used for |
|---|---|---|---:|---:|---:|---|
| TRAIN | 09-22 15:30 | 09-23 18:25 | 256 | 1,408 | 21.3 | choosing parameters |
| VALIDATION | 09-23 18:25 | 09-24 03:00 | 103 | 564 | 8.6 | filtering |
| UNSEEN 1 | 09-24 03:00 | 09-24 09:25 | 77 | 475 | 6.4 | locked test |
| UNSEEN 2 | 09-24 09:25 | 09-24 16:30 | 77 | 456 | 6.4 | locked test |
| UNSEEN 3 | 09-24 16:30 | 09-25 00:50 | 100 | 577 | 8.3 | locked test |
| FORWARD | 09-25 00:50 | 09-25 01:15 | 5 | 27 | 0.4 | archived during the study |

Every period has all 7 assets (BNB, BTC, DOGE, ETH, HYPE, SOL, XRP; 53–219 markets each per period). Earlier studies had read the unseen periods, but for other strategies. None had tested final-minute entries on this archive.

**What the book looks like as the close approaches** (TRAIN, every point; "favourite" = the side the project model puts ≥ 50 %):

| seconds left | Up book two-sided | favourite has an ask | favourite ask ≤ 0.95 | underdog has an ask | median favourite ask size |
|---|---:|---:|---:|---:|---:|
| 180–150 | 98 % | 98 % | 82 % | 100 % | 128 |
| 120–90 | 84 % | 84 % | 56 % | 98 % | 116 |
| 75–60 | 62 % | 62 % | 37 % | 95 % | 80 |
| **60–45** | **42 %** | **42 %** | **23 %** | 91 % | 61 |
| 45–30 | 25 % | 25 % | 12 % | 87 % | 57 |
| 30–20 | 13 % | 13 % | 6 % | 82 % | 54 |
| 15–10 | 5 % | 5 % | 2 % | 76 % | 17 |
| 5–0 | 0 % | 1 % | 1 % | 69 % | — |

**The favourite is usually not for sale in the final minute.** Once a window is decided the winner has bids up to 0.99 and no asks, as `docs/replay.md` found at the raw book. At 60–45 s the favourite's ask exists at 42 % of points. At 30–20 s it exists at 13 %, and under 10 s at about 1 %. What is left to buy late is mostly the underdog, and the underdog is where every model is least reliable (§5).

## 4. Market selection

All 7 assets are included, with no assumption about which to trade: every result is broken down by asset.

Research filters on every entry:
- ask between MIN_ASK (default 0.05) and MAX_ASK;
- at least 5 shares at the ask;
- a spread filter, swept from 1c to none. A book with no bid counts as infinitely wide.

Orders are 5 shares, the size every earlier study used. No market was excluded for any other reason, and every market has a venue outcome.

## 5. Fair-value methodology

Six probability models of `P(settling TWAP ≥ strike)`:

| model | what it is | fitted on |
|---|---|---|
| **A** spot vs strike | 1 if spot ≥ strike else 0 | — |
| **B** naive spot digital | Φ(ln(S/K) / σ√(u + lag)): assumes spot settlement | — |
| **D** the project model | `twap_fair_value`: Student-t(4) on the settling-TWAP distance, the observed part of the averaging window locked in | — |
| **E** D + settlement basis | adds basis noise sd = k·σ to D's sd; k = 1.50 by maximum likelihood on the venue outcomes | TRAIN windows |
| **C** historical empirical | P(Up) per (seconds-left group × E's z bin), TRAIN frequencies shrunk toward E (the repo's `CalibratedPricer` idea) | TRAIN |
| **F** logistic recalibration | per seconds-left group: logit(E), spot moves over 1/5/10 s, the Up token's 1 s move, a vol surprise, asset effects | TRAIN |

The fitted models are **cross-fitted inside TRAIN**: each half is predicted by a fit on the other half, so TRAIN numbers are out-of-fold too. Every later period uses the fit on all of TRAIN.

**Calibration** (Brier score per point; lower is better. The market column uses the Up mid, which exists only on two-sided books, so the fair comparison is the "there" columns):

| seconds left | period | B naive | D project | E + basis | two-sided points | **MARKET mid** | D there | E there | F there |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 180–120 | TRAIN | 0.1260 | 0.1222 | 0.1223 | 335,724 | **0.1258** | 0.1273 | 0.1273 | 0.1275 |
| 60–30 | TRAIN | 0.0444 | 0.0293 | 0.0299 | 57,028 | **0.0805** | 0.0855 | 0.0870 | 0.0919 |
| 30–10 | TRAIN | 0.0400 | 0.0161 | 0.0171 | 10,188 | **0.0808** | 0.1172 | 0.1218 | 0.1235 |
| 10–0 | TRAIN | 0.0516 | 0.0144 | 0.0147 | 669 | **0.1263** | 0.2351 | 0.2218 | 0.1584 |
| 60–30 | VALIDATION | 0.0398 | 0.0233 | 0.0240 | 22,446 | **0.0666** | 0.0724 | 0.0745 | 0.0757 |
| 30–10 | VALIDATION | 0.0338 | 0.0094 | 0.0112 | 3,200 | **0.0604** | 0.0676 | 0.0881 | 0.0965 |

What the calibration shows:
- **The TWAP structure matters.** The naive spot digital (B) is far worse than the TWAP model (D) in the last minute: Brier 0.044 against 0.029 at 60–30 s. **The market knows this.** It correlates with D, not B.
- **On the books that can actually be traded, the market beats every model at every horizon.** The gap widens toward the close: 0.081 against 0.117 at 30–10 s.
- **The basis matters for confidence, not for ranking.** D's log loss in the last 10 s is 0.146 against E's 0.067: D makes confident misses when the Chainlink basis flips a near-strike window. On Brier alone they tie.
- **C and F never beat D or E out of fold.** Momentum and asset terms add nothing to calibration.

**In the last 60 s the model overprices the side it disagrees with the market about.** Among points where model E sees ≥ 2c of executable edge, the outcome runs about 12c below the model's own probability on average, 5–20c by bucket (TRAIN, §9).

## 6. Entry timing

Each slice was run on its own, with entries only inside it (model D, edge ≥ 2c, ask ≤ 0.95, any spread, held to settlement; c/share, fills in brackets):

| slice (s left) | TRAIN | VALIDATION |
|---|---:|---:|
| 120–90 | −2.69 (753) | −2.10 (301) |
| 90–60 | −0.97 (613) | −5.32 (244) |
| **60–45** | **−4.76 (389)** | **−6.78 (142)** |
| 45–30 | −5.11 (217) | −4.32 (80) |
| 30–20 | −2.85 (113) | −4.97 (36) |
| 20–10 | −10.54 (48) | +9.02 (12) |
| 10–5 | −16.08 (14) | +4.92 (2) |
| 5–0 | +18.27 (4) | −6.39 (1) |

Model E gives the same picture: every slice from 120 s down to 10 s loses in both periods. The few positive cells have 1–16 fills and flip sign between periods.

- **Where mispricing appears.** Model edge ≥ 2c exists at 10–18 % of point-sides in every slice (§8). It is not concentrated in the last minute.
- **How fast it disappears.** Before 60 s, 70 % of edge points still show ≥ 2c a second later and 55 % five seconds later. **Late edges are more persistent, not less:** at 20–10 s, 94 % are still there after 1 s and 86 % after 5 s. An edge that sits on the book for seconds is not a lag waiting for a fast taker. It is a disagreement, and the market is right about it (§5).
- **Does later entry help?** It improves the model's accuracy (Brier falls tenfold), but the market improves just as fast. Late entries also run out of things to buy: the favourite's ask disappears (§3). Under 20 s, orders per market collapse (0.02 orders per market in the last 10 s).

Later is not better. **The entry-timing sweep has no positive slice in either period.** Across 12 windows × 12 thresholds × 5 MAX_ASK × 2 spread caps × 4 models, windows ≤ 60 s were positive in 0.2–4 % of TRAIN rows. The best TRAIN t was 0.57.

## 7. MAX_ASK analysis

MAX_ASK barely matters for the hypothesis as stated. The trades the model finds in the last minute are mostly underdogs (average decision ask 0.23–0.44), so raising MAX_ASK adds few fills:

| MAX_ASK | TRAIN fills | TRAIN c/share | VAL fills | VAL c/share |
|---:|---:|---:|---:|---:|
| 0.60 | 288 | −3.32 | 114 | −7.18 |
| 0.80 | 321 | −3.58 | 122 | −5.32 |
| 0.90 | 347 | −3.96 | 132 | −4.63 |
| 0.95 | 375 | −4.03 | 141 | −3.91 |
| 0.98 | 377 | −4.00 | 142 | −3.86 |

(Canonical entry: model D, 60 s, 2c, spread ≤ 3c, settlement.)

**What matters is MIN_ASK: the token-price range.** Re-running the entries with the ask confined to one bucket (settlement):

| ask bucket | canonical TRAIN | canonical VAL | E, 60 s, any spread TRAIN | E, 60 s VAL | broad E, 180 s TRAIN | broad VAL |
|---|---:|---:|---:|---:|---:|---:|
| 0.05–0.20 | −2.34 (251) | −5.66 (95) | −4.01 (348) | −5.20 (138) | −4.02 (701) | −1.54 (316) |
| 0.20–0.40 | −5.30 (137) | −17.29 (49) | −5.93 (194) | −13.94 (82) | −3.72 (613) | −8.67 (275) |
| 0.40–0.60 | −10.43 (98) | −2.83 (35) | −4.11 (137) | −12.00 (52) | −2.00 (565) | −5.41 (265) |
| 0.60–0.70 | −7.39 (74) | +13.93 (23) | −6.14 (95) | +4.59 (34) | −3.83 (578) | −0.85 (245) |
| 0.70–0.80 | −0.71 (96) | +5.75 (24) | +0.26 (102) | −4.22 (22) | −0.44 (684) | +2.85 (271) |
| 0.80–0.90 | −0.41 (110) | −0.48 (27) | −1.92 (101) | −1.49 (27) | −0.40 (752) | +2.71 (280) |
| **0.90–0.95** | **+1.95 (77)** | **+8.20 (24)** | **+6.72 (49)** | **+7.21 (10)** | −1.05 (570) | +0.43 (208) |
| **0.95–0.99** | **+5.61 (23)** | **+4.48 (5)** | **+4.48 (15)** | +4.67 (2) | −1.01 (334) | −2.62 (126) |

c/share (fills).

**Cheap and expensive tokens behave completely differently.**
- Everything under ~0.70 loses in every entry family and both periods. These are underdogs and coin-flips where the model disagrees with a better-informed market.
- The only positive pocket is **high-priced favourites (0.90–0.99) in the final minute.** It holds in both periods but on very few fills: 15–77 in TRAIN's 21 hours.
- The broad 180 s family's favourites do *not* profit. The pocket needs the last-minute TWAP lock-in *and* a model edge.

This finding is what motivated the price-band stage of the sweep (§16).

## 8. Edge threshold analysis

Minimum executable edge, canonical entry (model D, 60 s, ask ≤ 0.95, spread ≤ 3c, settlement):

| min edge | TRAIN fills | TRAIN c/share | USDC/fill | USDC/order | USDC/market | USDC/h | VAL fills | VAL c/share |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.5c | 430 | −3.53 | −0.18 | −0.11 | −0.054 | −3.56 | 159 | −2.29 |
| 1c | 408 | −3.74 | −0.19 | −0.12 | −0.054 | −3.58 | 153 | −1.38 |
| 2c | 375 | −4.03 | −0.20 | −0.12 | −0.054 | −3.54 | 141 | −3.91 |
| 3c | 334 | −3.37 | −0.17 | −0.10 | −0.040 | −2.64 | 128 | −5.68 |
| 5c | 267 | −0.93 | −0.05 | −0.03 | −0.009 | −0.58 | 97 | −3.46 |
| 8c | 189 | −5.27 | −0.26 | −0.14 | −0.035 | −2.33 | 73 | +0.59 |
| 10c | 147 | −5.37 | −0.27 | −0.13 | −0.028 | −1.85 | 63 | +0.08 |
| 15c | 91 | −5.78 | −0.29 | −0.14 | −0.019 | −1.23 | 39 | −0.28 |

**A higher threshold does not buy a better trade.** The per-fill result is flat to worse from 0.5c to 15c. The largest "edges" are the model's largest disagreements with the market, and those are its largest errors.

**Zero-latency check (§9):** rows with ≥ 2c edge are worth −1 to −10c at the decision ask even before any latency. The exceptions are DOWN rows in TRAIN, a regime effect that reverses in VALIDATION.

## 9. Momentum, lag and mispricing

**Momentum lookbacks.** At 250 ms – 10 s (signed to the side the model picks, last 60 s, model E signals, TRAIN), the outcome falls short of the model by 5–16c in every bucket:
- **Recent moves in our direction make it worse.** A 10 s move ≥ 1σ our way: −15.9c. Against: −4.7c.
- The fitted logistic (F) agrees: the 10 s spot-move coefficient is negative at 10–60 s left (−0.36 to −0.55 on the logit).

So relative to what the driftless model extrapolates, **short spot moves partly revert**, or the composite spot overshoots the Chainlink stream. The token's own last-second move gets a large positive coefficient (+1.4 to +2.2). **The token carries information the model lacks.**

50 ms and 100 ms lookbacks are below the archive's resolution. On the tick recording the token's median lag is 100 ms with under 60 s left, and our spot arrives 116–147 ms after the exchange stamp. Any sub-second lag is gone before this machine sees it (`docs/spot_lag_study_2026-09-23.md` §B–C).

**Lag or mispricing?** Signal rows (model E, edge ≥ 2c), valued at the decision ask with no latency (TRAIN):

| class | rows | value at the ask | t | outcome − model p |
|---|---:|---:|---:|---:|
| 180–60 s, **lag** (model +2c in 1 s, token less than half) | 8,053 | +1.98c | +1.5 | −5.2c |
| 180–60 s, static (model steady 3 s) | 37,982 | −1.05c | −0.8 | −6.1c |
| 60–0 s, **lag** | 2,936 | **−4.17c** | −2.0 | −19.5c |
| 60–0 s, static | 9,664 | +0.36c | +0.4 | −8.7c |

**The last-minute edge is neither a tradable lag nor a tradable mispricing.**
- The "lag" rows are worse in the final minute (−4.2c at zero latency). The token's failure to follow the model is information, not delay.
- The static rows are roughly zero *before* any execution cost.
- Everything the model calls edge is, on average, the model being wrong: the outcome falls 5–20c short of the model's probability.

## 10. Order-book conditions

For executed trades of the model-E 60 s entry (TRAIN; VALIDATION agrees in sign), nothing in the book turns it positive:
- **Spread:** 1c −0.8c/share (199 fills); 2c −5.5c; 3–5c −4.7c; 6c+ −4.6c.
- **Ask size:** < 50 shares −3.0c; 50–200 −3.2c; ≥ 200 −2.6c.
- **Imbalance** bid/(bid + ask): −1.7 to −4.0c in every quintile.
- **The token's last second:** falling ≥ 1c −4.0c (262 fills); flat −0.0c (57); rising ≥ 1c −2.1c.

The narrowest spread is the least bad, and the edge does not survive the spread anywhere. When one side dominates, or liquidity leaves, the entries lose about as much as elsewhere.

**Execution funnel (canonical entry, TRAIN):**
- 12,605 signal points in 437 markets gave 615 orders, 178 of them retries, and 375 fills (61 %).
- Of the 240 kills, 237 found the ask above the limit on arrival.
- **Filled orders paid 2.0c *less* than the decision ask; killed orders saw it 1.7c higher.** The fills are the ones the market moved away from: adverse selection, the same pattern every earlier study found.
- Polymarket trades are not in the archive, so "recent trades" could not be tested.

## 11. Exit analysis: selling before expiry

54 exits were simulated from the same fills. Sells are FOK at the bid − 2c, retried every 2 s, and an exit due inside the last 10 s settles.
- time stops: 1–30 s;
- take-profits: +1, +2, +3, +5 and +10c, each with a 1–30 s limit or held to settlement;
- six invalidation stops: spot crosses the strike, the expected TWAP falls halfway back toward the strike, fair value below the bid, edge at the entry price negative, token 1 s reversal ≥ 2c, spot 1 s reversal ≥ 1.5σ.

Canonical entry, TRAIN / VALIDATION (c/share):

| exit | TRAIN | VALIDATION |
|---|---:|---:|
| hold to settlement | −4.03 (t −2.0) | −3.91 (t −1.2) |
| sell after 1 / 5 / 30 s | −4.92 / −4.74 / −2.06 | similar |
| +2c target, else 10 s | −4.96 | |
| +5c target, else settlement | −4.20 | |
| +10c target, else settlement | −3.38 | |
| stop: spot crosses strike | −3.27 | |
| stop: fair value < bid | −3.64 | |

**No exit turns a losing entry into a winning one.** Selling quickly locks in the spread and a second fee on top of the entry's loss. The broad 180 s family shows the same ordering (settlement −2.3 / −3.3c; every quick exit −3.5 to −4.4c; t to −13). Allowing exits until 3 s before the close instead of 10 s changes nothing (`data/replay/snipe60/diagnostics.md` §8).

## 12. Settlement analysis

For entries in the last minute, holding to settlement is the least bad exit in both periods.
- **Losing entries:** it saves the second fee and the exit spread.
- **The favourite pocket:** holding is natural. The token is already at 0.90–0.98, the entry fee is 0.1–0.6c, and there is no exit fee.

The P&L then depends entirely on the **loss rate**:

| entry ask | a win pays | a loss costs | break-even loss rate | one loss wipes out |
|---:|---:|---:|---:|---:|
| 0.90 | +9.4c | −90.6c | 9.4 % | 10 wins |
| 0.93 | +6.5c | −93.5c | 6.5 % | 14 wins |
| 0.95 | +4.7c | −95.3c | 4.7 % | 20 wins |
| 0.98 | +1.9c | −98.1c | 1.9 % | 53 wins |

With 30–80 trades a sample cannot distinguish a 1 % loss rate from a 6 % one. That is why favourite rules were ranked by conservative EV (§16) and not by t.

## 13. UP vs DOWN, assets, regimes (TRAIN / VALIDATION)

**UP vs DOWN is a regime, not a structure.**
- TRAIN (09-22/23) had a down-drift. The canonical entry's UP trades lost 8.6c and its DOWN trades made +1.0c. The broad family: UP −9.0c, DOWN +4.2c.
- In VALIDATION it reversed: canonical UP −0.3c, DOWN −8.6c.
- In the last-minute reliability table on TRAIN, realised Up ran below both the market and the model at every mid-range probability.

A model fitted on TRAIN frequencies (C) absorbs the drift. That is why C topped the TRAIN ranking and failed VALIDATION (§16).

**Assets.**
- Canonical entry: TRAIN from −7.3c (BTC) to +4.1c (XRP, the only positive asset); VALIDATION from −12.8c (SOL) to +5.8c (ETH).
- Broad family, VALIDATION: SOL is the only positive asset (+1.6c).

No asset is positive in both periods for any broad entry.

**Regimes** (canonical, TRAIN): high vol −6.5c, mid −3.3c, low −3.3c; strong up +2.7c (24 fills), range −3.3c, strong down −6.8c (12). No regime is positive in both periods on a meaningful count. Distance from the strike is the only variable that sorts outcomes consistently (§14).

## 14. Distance from the strike

z = the side's expected settling-TWAP distance from the strike / its remaining sd including the basis (model E). Every point-side with an ask, TRAIN, valued at the decision ask with no latency:

| z (side) | 60–0 s rows | avg ask | realised win | won − ask | value after fee | rows with edge ≥ 2c | their value |
|---|---:|---:|---:|---:|---:|---:|---:|
| < −3 | 176,330 | 0.011 | 0.002 | −0.83c | −0.90c | 0 | — |
| −2 … −1 | 41,321 | 0.041 | 0.032 | −0.90c | −1.16c | 14,804 | −0.26c |
| −1 … −0.5 | 20,430 | 0.138 | 0.111 | −2.75c | −3.46c | 12,046 | −0.60c |
| **−0.5 … 0** | 17,729 | 0.349 | 0.266 | **−8.36c** | **−9.67c** (t −5.9) | 7,642 | −9.38c |
| 0 … 0.5 | 16,852 | 0.682 | 0.707 | +2.56c | +1.29c | 3,339 | −3.59c |
| 0.5 … 1 | 16,262 | 0.872 | 0.872 | +0.01c | −0.69c | 2,151 | +0.09c |
| 1 … 2 | 23,396 | 0.961 | 0.957 | −0.36c | −0.61c | 1,465 | +1.76c |
| 2 … 3 | 7,121 | 0.984 | 0.985 | +0.16c | +0.05c | 316 | +1.72c |
| **> 3** | 1,126 | 0.984 | 0.998 | +1.47c | **+1.36c** (t +4.6) | 137 | **+4.63c** |

**Where Polymarket is most wrong:**
- **Just on the losing side of the strike (−0.5 < z < 0), the underdog is overpriced by ~8c** in the last minute: it asks 0.35 and wins 27 %. Selling it (buying the favourite at 0.65–0.70) is the mirror image and is positive (+2.6c). But at 0 < z < 0.5 the model's own ≥ 2c signals lose (−3.6c), because they pick the wrong side of that bucket.
- **Far from the strike (z > 3) the favourite is slightly underpriced:** 0.984 against a 99.8 % win rate, +1.4c after fees. This is the snipe in the literal sense: a near-certain token still offered a cent or two cheap. It is rare: 1,126 point-sides in 21 hours, 86 markets.
- Between those two, the market is calibrated to within a cent.

## 15. Execution analysis and latency sensitivity

**The execution model** is the project's validated one (`backtest/spot_shock_replay.Execution`), reimplemented vectorised and with common random numbers across latency scales:
- Every order leaves after a pre-send delay (15–250 ms prior) and reaches the venue half a round trip later (measured RTT, p50 243 ms).
- The venue's book is read as the archived local book 500 ms after arrival. This proxy reproduced the venue-truth P&L of 694 real-book orders.
- Buys are fill-or-kill at a limit of min(ask + 2c, the highest price keeping the edge), 5 shares, no partial fills. The fill price is the arrival ask.
- A killed order may be followed by the next signal at least 2 s after the kill is known, up to 3 orders per market. One position per market.
- Sells are FOK at the bid − 2c (a take-profit sells at the bid it saw), retried every 2 s. An exit due inside the last 10 s settles 1/0.
- Taker fee 0.07·p(1 − p) on every taker leg.

The pipeline tracks each stage separately: candidate → signal → order → fill → exit → completed trade (funnel in §10; `diagnostics.md` §9).

**The one change from the existing model:** it refused any buy landing in the last 10 s, because it was built for strategies that stop at 60 s. Here a buy fills if its proxied book point is before the close, since the final seconds are the question. Restoring the 10 s refusal changes nothing (§18). Almost nothing is bought there anyway: the favourite is not offered (§3).

**Latency sensitivity.** The same signals, with every order and exit re-simulated. The scale multiplies the pre-send delay, the order's trip and the book-lag proxy; ×0 fills at the decision book (signal = fill). Pooled unseen c/share (fills):

| strategy | ×0 | ×1 (measured) | ×1.25 | ×1.5 | ×2 | unseen fill rate at ×1 |
|---|---:|---:|---:|---:|---:|---:|
| 60-second strategy (pre-registered) | −0.36 (413) | −3.84 (352) | −3.94 | −4.28 | −2.57 | 59 % |
| locked high-volume | −1.12 | −1.08 (1,451) | −1.12 | −1.24 | −1.10 | 83 % |
| locked high-expectancy | **+6.76 (164, t 2.1)** | −1.42 (94) | −3.33 | −3.79 | −3.69 | 47 % |
| locked most-stable-on-TRAIN | −1.26 | −2.44 (813) | −2.54 | −2.70 | −3.33 | 60 % |
| locked final-minute | −3.12 | −3.28 (113) | −3.32 | −3.84 | −3.78 | 58 % |
| locked favourite band | −1.05 (61) | −2.81 (27) | −5.17 | −4.30 | −5.43 | 36 % |

- **No candidate survives realistic latency, and most lose even at zero latency.** The final-minute rules are negative at ×0.
- The high-expectancy rule shows the classic artifact: instant execution makes it +6.8c (t 2.1); any real latency makes it negative. Half its orders are killed, and the ones that fill are those the market moved away from.
- For the pre-registered strategy, measured latency costs about 3.5c/share against instant execution.
- ×2 looks "better" than ×1.5 for the pre-registered rule only because slower orders are killed more often, and the kills were losers. It is still −2.6c.

## 16. Walk-forward results

**Protocol** (`scripts/analyze_60s_snipe.py`, docstring). It was written at ~01:35 UTC, before any sweep result was read, and **locked at 01:58:18 UTC** (`data/replay/snipe60/locked.json`):
- Parameters are chosen on TRAIN only.
- VALIDATION is a pass/fail filter: walking down the TRAIN ranking, the first row with ≥ 10 VALIDATION fills and a positive VALIDATION c/share is locked. If none of the top 20 passes, the TRAIN #1 is locked anyway and flagged.
- The unseen periods were read only by `evaluate`, after the lock. Nothing was re-chosen afterwards.

**The grid, coarse → fine:**

| stage | configurations (× 13 exits) |
|---|---:|
| coarse: 4 models × 12 windows (180 … 5 s) × 12 edges (0.5 … 15c) × 5 MAX_ASK × 2 spread caps | 5,760 |
| time slices: 8 slices × 6 edges × 3 MAX_ASK × 2 spreads × 4 models | 1,152 |
| fine: around 16 TRAIN seeds; all 11 MAX_ASK (0.50 … 0.98) × min time remaining 0/2/5/10 s × spread 1/2/3/5c/none × FOK limit ask + 0/1/2c | 10,560 |
| price band: MIN_ASK × MAX_ASK (0.50 … 0.99) × 8 windows × 5 edges × 2 spreads × 4 models | 11,520 |

The 13 exits are:
- settlement;
- sell after 2 / 5 / 10 / 30 s;
- +1c / 5 s, +2c / 10 s, +3c / 30 s, +5c else settle, +10c else settle;
- three invalidation stops.

In all, 372,944 TRAIN rows. 283,023 had ≥ 40 fills, 39,877 were positive and 3,125 had t ≥ 2.

**Two changes after the protocol was written, both before any unseen result**, stated in the docstring:
1. **~01:39 UTC:** a **final-minute** criterion was added: the highest TRAIN t among windows ≤ 60 s. The coarse TRAIN landscape showed every ≤ 60 s window negative, so the original three criteria could never pick the hypothesis itself.
2. **~01:44 UTC:** the **price-band** stage and its two criteria were added: MIN_ASK × MAX_ASK, ranked by conservative c/share, the loss rate taken at its 95 % upper bound. The diagnostics had shown the only positive pocket at high-priced favourites. **Those diagnostics included VALIDATION price buckets, so the VALIDATION filter was not blind for the band candidates. For them the unseen periods are the only clean test.**

**What the protocol locked** (TRAIN → VALIDATION; full ranking in `data/replay/snipe60/select.md`):

| criterion | locked rule | TRAIN | VALIDATION | status |
|---|---|---:|---:|---|
| high-volume | C, 180–0 s, 0.5c, ask ≤ 0.98, any spread, settle | +3.76c (1,350, t 2.4) | −2.15c (547) | **failed**: all top 20 negative on VALIDATION |
| high-expectancy | D, 180–2 s, 15c, ask ≤ 0.50, spread 1c, limit at the ask, stop when edge < 0 | +9.23c (89, t 1.8) | −2.99c (41) | **failed**: all top 20 negative |
| most stable | C, 180–0 s, 10c, ask ≤ 0.80, any spread, settle | +6.65c (786, t 2.9) | −1.09c (353) | **failed**: all top 20 negative |
| final minute | F, 45–2 s, 8c, ask ≤ 0.90, spread 1c, limit at the ask, sell after 30 s | +3.77c (124, t 1.0) | +1.17c (34) | passed (TRAIN #17) |
| price band (both) | D, 45–0 s, 1c, ask 0.93–0.95, any spread, stop when fair value < bid | +5.07c (39) | +4.60c (11) | passed (not blind) |

**Unseen results** (c/share, fills, t):

| strategy | unseen 1 | unseen 2 | unseen 3 | **pooled** | forward (0.4 h) |
|---|---:|---:|---:|---:|---:|
| 60-second strategy (pre-registered) | −6.16 (110) | −0.37 (109) | −4.76 (133) | **−3.84 (352, t −2.1)** | −23.6 (8) |
| locked high-volume | +2.49 (452) | −1.14 (439) | −3.92 (560) | **−1.08 (1,451, t −0.7)** | −1.4 (26) |
| locked high-expectancy | −0.45 (30) | −1.39 (32) | −2.35 (32) | **−1.42 (94, t −0.4)** | +1.1 (3) |
| locked most-stable-on-TRAIN | +5.19 (236) | −5.34 (261) | −5.74 (316) | **−2.44 (813, t −1.0)** | −4.4 (18) |
| locked final-minute | −9.15 (31) | +1.88 (40) | −3.87 (42) | **−3.28 (113, t −1.1)** | +28.2 (4) |
| locked favourite band | +1.72 (6) | +4.69 (6) | −7.63 (15) | **−2.81 (27, t −0.6)** | — |
| random entry, last 60 s (20 seeds) | −2.03 | −3.07 | −1.66 | −2.20 (433/seed) | |
| random, matched to the favourite band (20 seeds) | −2.76 | −2.18 | −2.97 | −2.65 (196/seed) | |
| random, matched to high-volume | −1.40 | −1.23 | −1.74 | −1.48 (1,373/seed) | |
| buy the side the spot is on, last 60 s | +6.65 (139) | +5.75 (141) | −5.15 (162) | +2.04 (442, t 1.0) | −9.6 (9) |
| same with the expected TWAP | +6.01 | +6.11 | −5.75 | +1.73 (436, t 0.9) | |
| the project's fair-value rule, 180–60 s | −2.68 (183) | −1.41 (182) | −1.51 (235) | −1.84 (600, t −1.0) | |
| the same rule in the last 60 s | −4.26 | −4.88 | −4.34 | −4.48 (225, t −1.8) | |

**The whole grid, scored on the unseen periods after the lock** (descriptive only; nothing was chosen from it):
- Of 284,245 configurations with enough fills, **5.5 % are positive unseen**; the median is −3.6c.
- **Of the 158 coarse configurations with TRAIN t ≥ 2, none is positive unseen.** Of the 213 TRAIN-positive time slices, none.
- High-priced favourites in the final minute (MIN_ASK ≥ 0.90, window ≤ 60 s, 739 configurations): median **+0.2c** on TRAIN, **+4.9c** on VALIDATION (86 % positive), **−4.1c** on unseen (9 % positive). VALIDATION was the lucky period.

The model- and rule-based entries do no better than random entries with the same filters:
- the favourite band: −2.81c against −2.65c for random favourites in the same band;
- high-volume: −1.08c against −1.48c.

The model adds nothing measurable.

## 17. Statistical analysis

Unseen periods pooled. The CI is cluster-robust by 5-minute epoch, because every asset in one window is one bet. The bootstrap resamples epochs (2,000 draws).

| strategy | n | mean c/share | median | sd | 95 % CI | bootstrap 95 % | t | max DD | PF | losing trades |
|---|---:|---:|---:|---:|---|---|---:|---:|---:|---:|
| 60-second strategy (pre-registered) | 352 | −3.84 | −6.39 | 33.2 | −7.50 … −0.18 | −7.64 … −0.21 | −2.05 | $70.16 | 0.73 | 232 |
| locked high-volume | 1,451 | −1.08 | +5.61 | 37.1 | −4.02 … +1.85 | −3.98 … +1.64 | −0.72 | $185.35 | 0.93 | 599 |
| locked high-expectancy | 94 | −1.42 | −9.23 | 32.4 | −7.85 … +5.01 | −7.23 … +5.68 | −0.43 | $16.41 | 0.88 | 75 |
| locked most-stable-on-TRAIN | 813 | −2.44 | −12.74 | 45.2 | −7.29 … +2.40 | −7.20 … +2.59 | −0.99 | $217.65 | 0.89 | 424 |
| locked final-minute | 113 | −3.28 | −8.52 | 31.8 | −9.15 … +2.58 | −9.02 … +2.60 | −1.10 | $19.40 | 0.75 | 91 |
| locked favourite band | 27 | −2.81 | +4.54 | 24.8 | −12.36 … +6.73 | −13.49 … +4.50 | −0.58 | $8.64 | 0.58 | 4 |

- **The pre-registered 60-second strategy is significantly negative.** Its whole interval is below zero.
- None of the others' intervals excludes zero, and every point estimate is negative.
- **Nothing is carried by a handful of markets.** No candidate has a positive total, so there is no winning epoch to be concentrated in.
- The favourite band's positive median (+4.5c) against its negative mean is the loss-tail shape of §12. Its 4 losses (15 %) are far above the ~5.6 % break-even at a 0.94 entry. Its conservative c/share is −10.8c.

**By period, asset and regime** (full tables in `data/replay/snipe60/evaluate.md`):
- **Pre-registered strategy, unseen:** every unseen period is negative, and so is every asset (BNB −0.6, HYPE −2.3, XRP −2.3, SOL −3.3, BTC −4.8, ETH −5.5, DOGE −9.2c).
  - Sides: UP −2.8c, DOWN −5.0c.
  - Vol regimes: low −6.7c (135 fills), mid −4.3c, high +2.0c (77).
  - Trend: range −4.6c; strong up +23.8c on 7 fills.
  - z < 0 (buying the underdog): −6.2c, t −3.4.
- **Favourite band, unseen:** −7.5c on BNB and −16.4c on XRP (the losses), positive on the other five on 1–7 fills each.

None of these cells is large enough to be a rule, and none was positive on TRAIN and VALIDATION as well.

## 18. Stress testing

The same unseen fills, re-priced; "fills lost" = the worse price broke the order's limit:

| stress | 60-second strategy | high-volume | final-minute | favourite band |
|---|---:|---:|---:|---:|
| as locked | −3.84 (352) | −1.08 (1,451) | −3.28 (113) | −2.81 (27) |
| spread +25 % (both legs) | −4.16 (257) | −1.98 (1,104) | −3.34 (67) | −2.37 (13) |
| spread +50 % | −4.70 (236) | −2.22 (1,023) | −3.48 (64) | −3.79 (12) |
| slippage +25 % (adverse moves in flight) | −3.76 (317) | −1.31 (1,350) | −3.29 | −3.91 (22) |
| slippage +50 % | −3.78 (317) | −1.33 (1,350) | −3.30 | −3.96 (22) |
| 25 % / 50 % of fills lost at random | −3.73 / −3.98 | −1.16 / −1.01 | −3.60 / −3.71 | −2.40 / −2.37 |
| the best 25 % of fills lost | −18.14 | −15.42 | −18.29 | −9.01 |
| venue book read 300 / 1,000 ms late | −4.26 / −3.71 | −0.84 / −1.12 | −0.88 / −4.02 | −1.60 / −4.29 |
| buys refused in the last 10 s (the existing model) | −3.84 | −1.08 | −3.22 | −3.14 |
| exits allowed until 3 s before the close | −3.84 | −1.08 | −2.29 | −2.77 |
| latency ×1.25 / ×1.5 / ×2 | −3.94 / −4.28 / −2.57 | −1.12 / −1.24 / −1.10 | −3.32 / −3.84 / −3.78 | −5.17 / −4.30 / −5.43 |
| without the top 1 / 5 / 10 % of winners | −4.94 / −8.08 / −11.31 | −1.96 / −4.83 / −7.82 | −4.98 / −8.14 / −12.13 | −3.25 / −3.64 / −4.06 |

There is no edge to protect. Worse execution costs roughly its own size, and no stress changes a sign.

## 19. Limitations

- **One regime, ~57 hours.** TRAIN (09-22/23) had a down-drift strong enough to make UP-side signals lose 6–10c and DOWN-side signals win 3–5c. A model fitted on TRAIN frequencies learned it. A calmer or trending market could change sizes, but not the fee and adverse-selection arithmetic or the market's better calibration.
- **The fill model is a validated proxy**, not the venue: the local book 500 ms late, calibrated on spot-shock orders.
  - It was not re-validated on final-minute favourites, whose asks are thin (median 17–61 shares late).
  - Five shares, no partial fills and no queue position. Larger orders would do worse.
- **Last-second venue behaviour is assumed.** Buys are allowed until the proxied book point before the close. Whether the venue matches orders in the final seconds was not verified. Refusing the last 10 s changes nothing (§18).
- **The settlement basis is modelled, not observed.** Our composite TWAP misses the venue outcome 1–3 % of the time, depending on the asset. A strategy reading Chainlink's own stream could avoid most of that basis. That is the one input that might change the favourite-snipe arithmetic. It was not available here and was not tested.
- **Not resolvable in this archive:** 50 ms and 100 ms momentum (cited from the tick recording), Polymarket trade flow, and depth beyond the touch.
- **Multiple testing.** 372,944 TRAIN rows were searched. The protocol guards against it: TRAIN ranking, a VALIDATION filter, and unseen periods read only after the lock. But the price-band amendment was made after VALIDATION diagnostics were seen (§16).
- **Unseen 1–3 had been read by earlier studies**, but for other strategies (spot-lag, repriced momentum); final-minute entries had not been tested on them. The FORWARD period (27 markets archived during the study) is too short to say anything.
- **The project's current fair-value rule is an approximation** of `strategy/engine.py`: the blend, costs, confidence and stale-quote gates, price, spread and depth limits, and cross. Its take-profit logic was not replayed; it was held to settlement.

## 20. Final conclusion

**No meaningful edge.** The hypothesis fails for three structural reasons. Each was measured, not assumed:

1. **The market prices the TWAP lock-in better than any model.** Polymarket is better calibrated than the project model, a basis-aware version, an empirical table and a momentum-aware logistic, at every horizon on every tradable book. The disagreements the models find are their own errors, and the largest disagreements are the largest errors.
2. **There is nothing cheap left to buy.** Once a window is decided the favourite is not offered: its ask exists at 42 % of points at 60–45 s and about 1 % under 10 s. What remains buyable late is the underdog, which is where the models are most wrong.
3. **What looks tradable is adversely selected.** Late edges persist for seconds, which fits a disagreement the market is right about. The fills that do happen land after the ask has fallen, and latency adds about 3.5c/share.

The single pocket that survived TRAIN and VALIDATION, high-priced favourites far from the strike, lost out of sample. Its economics (win 5c, lose 94c) make it a bet on the settlement-basis tail that 30–80 trades cannot size.

**Recommendation:** do not build a final-minute snipe engine, in paper or live. The existing engine's choice to stop at 60 s (`EngineConfig.trade_window_end_s`) is confirmed.

Revisit only if one of these changes, then re-run the deterministic pipeline:
- **a Chainlink Data Streams feed** becomes available to the bot, removing the settlement basis;
- **co-located execution** becomes available;
- **Polymarket's taker fees** change materially.

---

## Reproduce

```bash
python scripts/research_60s_snipe.py cache                    # ~2 min: data/replay/snipe60/cache/
python scripts/research_60s_snipe.py sweep --stage coarse     # TRAIN + VALIDATION only
python scripts/research_60s_snipe.py sweep --stage slices
python scripts/research_60s_snipe.py sweep --stage fine
python scripts/research_60s_snipe.py sweep --stage band
python scripts/analyze_60s_snipe.py diagnostics               # data/replay/snipe60/diagnostics.md
python scripts/analyze_60s_snipe.py select                    # the lock: data/replay/snipe60/locked.json, select.md
python scripts/analyze_60s_snipe.py evaluate                  # data/replay/snipe60/evaluate.md, results/60s_snipe_summary.csv
python scripts/research_60s_snipe.py sweep-unseen             # after the lock: results/60s_snipe_sweep.csv
pytest tests/test_snipe60_research.py
```

Every random draw is seeded, so every stage is deterministic. The cache must be rebuilt from the same archive to reproduce these numbers: more archived windows change the FORWARD period and the latency draws of later assets.

## Deliverables

| file | what |
|---|---|
| `scripts/research_60s_snipe.py` | cache (causal features, vectorised TWAP fair value), the fair-value models, the vectorised FOK order simulation, the per-market entry walk, 54 exits, statistics, the sweep stages |
| `scripts/analyze_60s_snipe.py` | diagnostics, the pre-registered selection, walk-forward evaluation, baselines, latency and stress |
| `tests/test_snipe60_research.py` | 11 tests (below) |
| `results/60s_snipe_sweep.csv` | the coarse, time-slice and price-band grids × 13 exits, one row per configuration and exit, fills / c-share / t / net per period. The unseen and forward columns were computed after the lock. The fine stage and the long format: `data/replay/snipe60/sweep_long_all_periods.csv.gz` |
| `results/60s_snipe_summary.csv` | every strategy and baseline by period |
| `data/replay/snipe60/` | `diagnostics.md`, `select.md`, `locked.json`, `evaluate.md`, the train/validation sweep CSVs |
| `docs/60s_snipe_research.md` | this report |

The tests check that:
- the vectorised fair value equals `twap_fair_value`;
- a rewritten future leaves every earlier feature bit-for-bit unchanged;
- the FOK limit arithmetic is correct;
- the walk's retry throttle and one-position rule hold;
- an order landing after the close is killed;
- settlement P&L charges only the entry fee;
- the conservative EV and the clustered SE are correct;
- the split keeps the established periods;
- the zero-latency take-profit terminates.

**Safety.** No strategy code, configuration, environment variable, pm2 process or order was touched. All research jobs ran at `nice 19` beside the live bots.

