# Spot-lag parameter sweep: MAX_ASK × exit (2026-09-25)

RESEARCH ONLY. Nothing here changed a running bot, a pm2 process or `.env`. Every number comes from
`scripts/research_spot_lag_parameter_sweep.py` and `scripts/analyze_spot_lag_parameter_sweep.py`; the full tables are in
`results/spot_lag_parameter_sweep_report.md`.

**Short answer.** The price ceiling matters a lot; the exit barely matters.

- **Fills above 0.60 earn nothing.** They make −0.3c/share against +13.9c for fills below 0.60, a difference of
  +14.2c with a 95% CI of [+10.3, +18.1]. The gap holds separately in training, validation and the unseen periods.
- **A 0.60–0.75 ceiling keeps total profit and cuts drawdown.** Profit stays the same or rises ($98 → $105–109 over
  every period), per-share quality goes up by half to double, and the worst drawdown falls by about a third.
- **No exit beats the production exit** (5 s, extended to 10 s while the spot keeps moving). 21 of the 80 alternatives
  are significantly worse.
- **Recommendation for PAPER testing only:** MAX_ASK 0.74 (the locked, training-chosen value), anywhere in 0.60–0.75 is
  defensible, and the production exit unchanged.
- **Caution:** the replay fills about twice as often as the live bot did over the same hours. If the missing live fills
  are the best ones, most of the edge goes (section 12).

## 1. Methodology

1. **Build** (`research_spot_lag_parameter_sweep.py build`):
   - Replay every 5-minute market in the archive through the production spot-lag entry rule
     (`strategy/spot_lag.py`), with a research ceiling of 0.98.
   - Every signal gets the production latency, the venue_lag fill model, and 81 exits evaluated on the same fill.
   - The dataset is built four times: measured latency, ×1.25, ×1.5 and ×2.
2. **Sweep** (`... sweep`):
   - The grid is 39 MAX_ASK values × 81 exits = **3,159 combinations**.
   - Each combination runs through the production portfolio budget in time order: 4 positions, $10 exposure at the
     limit, 60 buys per hour, 3 attempts and 1 entry per market, and a 2 s retry.
   - Each is scored on every period. Output: `results/spot_lag_parameter_sweep.csv`, one row per combination and period.
3. **Select on TRAINING, filter on VALIDATION, LOCK** (`analyze_spot_lag_parameter_sweep.py`):
   - Candidates need at least 30 training fills and a positive training net.
   - One pick per category:
     - risk-adjusted: the highest t;
     - high-volume: the most fills with t ≥ 2;
     - high-quality: the most c/share;
     - low-drawdown: net ÷ max drawdown, with t ≥ 2;
     - MAX_ASK only: the production exit;
     - exit only: MAX_ASK 0.95.
   - A pick that loses on validation, or has fewer than 5 fills there, is replaced by the next training candidate.
   - The picks were written to `results/spot_lag_parameter_sweep_locked.json` at **2026-09-25 01:01:19 UTC**, before any
     unseen column was read. A re-run refuses to change the lock.
4. **Unseen:** the locked picks and the baseline are then scored on unseen_1, unseen_2 and unseen_3 (pooled and
   separately), and on unseen_3b.
5. **Statistics:**
   - net per share, mean, median and sd;
   - a t-statistic clustered by 5-minute window and its CI;
   - a bootstrap by window (2,000 resamples);
   - results without the top 5 trades, and the top window's share of profit;
   - paired tests for exits (the same fills under each exit) and bucket tests for MAX_ASK (section 13).

## 2. Dataset

| period | UTC | 5-min windows | hours with data |
|---|---|---:|---:|
| train | 09-22 15:30 → 09-23 18:25 | 256 | 21.3 |
| validation | 09-23 18:25 → 09-24 03:00 | 103 | 8.6 |
| unseen_1 | 09-24 03:00 → 09-24 09:25 | 77 | 6.4 |
| unseen_2 | 09-24 09:25 → 09-24 16:30 | 77 | 6.4 |
| unseen_3 | 09-24 16:30 → 09-25 00:50 | 100 | 8.3 |
| unseen_3b (the part of unseen_3 after 19:15) | 09-24 19:15 → 09-25 00:50 | 67 | 5.6 |

- **Where the periods come from:** the same splits as every earlier study (`research_repriced_momentum.split_fn`).
- **unseen_3b** had not been looked at by any earlier study. It is a subset of unseen_3, so the pooled "unseen" is
  unseen_1 + unseen_2 + unseen_3.
- **Counts:**
  - 16,910 production shocks had a book;
  - 1,230 passed the entry gates, excluding HYPE (which production excludes);
  - under the baseline, those became 1,064 orders and 242 fills.
- **Assets:** BTC, ETH, SOL, XRP, DOGE and BNB, plus HYPE, shown only for reference.

## 3. Parameter ranges

- **MAX_ASK:** 0.50, 0.55, every 0.01 from 0.60 to 0.95, and 0.98, for 39 values. This covers every value the brief
  lists.
  - In the live code the effective ceiling is `min(engine.max_trade_price 0.95, MAX_ASK)` (`spot_lag.py:455`), so 0.98
    does nothing live unless `max_trade_price` is also raised.
- **Exits (81):**
  - `live`: the production exit;
  - fixed times `t0.5 … t30`: 0.5, 1, 2, 3, 5, 7, 10, 15, 20 and 30 s;
  - profit target + time limit `tp{c}_{s}`: targets 1–6, 8, 10, 12, 15 and 20c, each with a 5, 10, 20 or 30 s limit,
    or held to settlement (`_settle`);
  - event exits with a 10, 20 or 30 s cap (after the cap, a time exit). "Fair value" means the entry rule's estimate
    now: the side's mid 1 s before the decision plus the model fair value's change since then.
    - `spot_rev`: the spot's last 500 ms move goes against the position;
    - `tok_rev`: the token's mid is below its level 500 ms earlier;
    - `gap_closed`: the mid reaches the fair value;
    - `fair_below_bid`: the fair value is below the bid;
    - `edge_gone`: the fair value is no more than 0.5c above the bid.

## 4. Execution assumptions

These are the production model, unchanged from the spot-lag studies.

- **Latency:** measured order latency, with pre-send 15–250 ms (median 50) and a venue round trip of 218–661 ms
  (median 243). The book the venue sees is the archive book 500 ms after arrival (venue_lag).
- **Entry:** FOK at `min(ask + 2c, max_entry)` for 5 shares. It fills only if the book at arrival has at least 5
  shares at or below the limit; there are no partial fills.
- **Exit:** a taker sell at the bid − 2c, retried every 2 s. There are no exits in the last 10 s; the position settles
  at 1 or 0 with no fee.
- **Profit-target exits** use the live bot's TAKE_PROFIT semantics:
  - the target is checked on every archive book update, and the sell is a limit AT the bid;
  - it only fires when the net after both fees is at least 0.5c, so at mid prices a 1–3c target acts like about 4c;
  - it fills if the venue book (after latency + 500 ms) still has that bid for 5 shares, and otherwise retries after
    2 s;
  - at the time limit it becomes the normal taker exit.
- **Fees:** the taker fee is rate × p × (1 − p) at rate 0.07 on both legs. Settlement has no fee.
- **Entry gates:**
  - shock z1 ≥ 3σ, with the 3 s move agreeing and accelerating;
  - 60–295 s left in the window;
  - spread ≤ 3c;
  - ask between 0.05 and MAX_ASK;
  - minimum net edge 2c;
  - 0.5c slippage and 0.5c uncertainty in the edge.
- **Not modelled:** the live bot's quote-age skip (over 250 ms), which the archive cannot see.

## 5. Baseline (the live bot's rule: MAX_ASK 0.95, production exit)

| period | fills | win | net USDC | c/share | t | max DD |
|---|---:|---:|---:|---:|---:|---:|
| train | 115 | 77% | +60.70 | +10.56 | +5.3 | 1.97 |
| validation | 55 | 64% | +14.91 | +5.42 | +2.8 | 2.15 |
| unseen_1 | 28 | 57% | +2.65 | +1.89 | +0.9 | 1.56 |
| unseen_2 | 19 | 47% | +5.50 | +5.79 | +0.8 | 3.31 |
| unseen_3 | 25 | 72% | +14.37 | +11.50 | +1.6 | 1.80 |
| unseen_3b | 20 | 70% | +9.97 | +9.97 | +1.2 | 1.80 |
| **unseen pooled** | **72** | 60% | **+22.52** | **+6.25** | **+2.0** | 3.31 |
| all | 242 | 69% | +98.12 | +8.11 | +5.7 | 3.31 |

- **Unseen:** pooled bootstrap CI [+0.7, +12.6]c; +2.75c without the top 5 trades.
- **Degradation:** the rule is positive but weakens from training (+10.6c) to validation (+5.4c) and unseen (+6.3c).

## 6. Results: the locked candidates out of sample

| candidate (chosen on train, filtered on validation) | MAX_ASK | exit | unseen fills | c/share | net USDC | t | bootstrap 95% CI | ex top-5 | u1 / u2 / u3 / u3b c/share |
|---|---:|---|---:|---:|---:|---:|---|---:|---|
| **current baseline** | 0.95 | live | 72 | +6.25 | +22.52 | 2.0 | [+0.7, +12.6] | +2.75 | +1.9 / +5.8 / +11.5 / +10.0 |
| risk-adjusted (= low-drawdown) | 0.64 | tp2_settle | 41 | +12.56 | +25.75 | 2.2 | [+2.5, +24.8] | +6.10 | +1.1 / +24.0 / +21.4 / +22.1 |
| high-volume | 0.95 | gap_closed_30 | 72 | +6.16 | +22.19 | 1.9 | [+0.5, +13.1] | +2.50 | +1.8 / +7.1 / +10.3 / +8.7 |
| high-quality | 0.50 | t20 | 31 | +14.63 | +22.68 | 2.4 | [+4.6, +27.9] | +6.34 | +6.5 / +22.5 / +19.2 / +17.1 |
| **MAX_ASK only** | **0.74** | live | **55** | **+10.53** | **+28.97** | **2.9** | **[+4.2, +18.2]** | **+6.27** | +2.4 / +14.9 / +17.3 / +15.2 |
| exit only | 0.95 | edge_gone_10 | 72 | +6.61 | +23.78 | 2.1 | [+0.9, +13.3] | +3.03 | +2.2 / +7.5 / +10.8 / +10.4 |

**What changes the result is the ceiling, not the exit:**

- **A lower ceiling helps.** Every candidate with a ceiling below 0.95 beats the baseline per share in every unseen
  period. The one exception is the risk-adjusted pick in unseen_1: +1.1c against +1.9c.
- **Changing only the exit does nothing.** The two picks that keep MAX_ASK 0.95 (high-volume and exit only) are within
  0.4c of the baseline.
- **tp2_settle's gain comes from its ceiling.** At the same ceiling of 0.64, the production exit makes +13.3c unseen
  against tp2_settle's +12.6c, and the paired difference is −0.4c [−2.4, +1.4].
  - It also carries a tail: every position that failed to reach +2c was held to settlement. All 12 such settlements in
    the budget-free set were total losses, averaging −65.7c/share.
- **The best out-of-sample profit and t come from MAX_ASK 0.74 with the production exit:** +$29.0 and t 2.9, with a
  bootstrap CI clear of zero.

## 7. MAX_ASK analysis (production exit)

| MAX_ASK | rejected by MAX_ASK | orders | fills | fills/h | fill rate | win | net USDC (all) | c/share (all) | max DD | PF | train c/share | unseen c/share (fills) | u1 / u2 / u3 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 0.50 | 52% | 556 | 112 | 2.2 | 20% | 83% | +85.94 | +15.35 | 1.02 | 14.7 | +17.77 | +15.02 (31) | +4.4 / +22.7 / +22.7 |
| 0.55 | 45% | 636 | 137 | 2.7 | 22% | 82% | +93.89 | +13.71 | 1.06 | 12.5 | +15.44 | +14.82 (34) | +4.4 / +20.3 / +22.0 |
| **0.60** | 37% | 715 | 153 | 3.0 | 21% | 82% | +105.21 | +13.75 | 1.94 | 13.7 | +14.59 | **+15.10 (38)** | +5.4 / +20.3 / +23.5 |
| 0.65 | 29% | 794 | 169 | 3.3 | 21% | 80% | +108.92 | +12.89 | 2.15 | 12.5 | +14.15 | +12.98 (43) | +2.6 / +20.3 / +23.1 |
| 0.70 | 21% | 874 | 188 | 3.7 | 22% | 77% | +104.67 | +11.13 | 2.15 | 9.3 | +12.56 | +11.25 (50) | +2.7 / +14.2 / +22.1 |
| **0.74** | 16% | 919 | 200 | 3.9 | 22% | 76% | +106.46 | +10.65 | 2.15 | 8.3 | +12.26 | **+10.53 (55)** | +2.4 / +14.9 / +17.3 |
| 0.80 | 9% | 986 | 220 | 4.3 | 22% | 73% | +103.78 | +9.43 | 2.15 | 6.0 | +11.41 | +8.43 (63) | +2.0 / +11.9 / +13.2 |
| 0.85 | 5% | 1023 | 229 | 4.5 | 22% | 72% | +102.54 | +8.96 | 2.15 | 5.5 | +10.88 | +8.00 (66) | – |
| 0.90 | 2% | 1049 | 237 | 4.6 | 23% | 70% | +100.91 | +8.52 | 2.15 | 5.1 | +10.65 | +7.18 (70) | +2.2 / +8.6 / +11.5 |
| **0.95 (baseline)** | 0% | 1064 | 242 | 4.7 | 23% | 69% | +98.12 | +8.11 | 3.31 | 4.6 | +10.56 | **+6.25 (72)** | +1.9 / +5.8 / +11.5 |
| 0.98 | 0% | 1067 | 243 | 4.7 | 23% | 68% | +98.09 | +8.07 | 3.31 | 4.6 | +10.56 | +6.16 (73) | – |

**Volume against quality:**

- **Quality falls steadily with the ceiling.** Unseen c/share drops from +15.1 at 0.60 to +6.3 at 0.95. Training shows
  the same shape, from +14.6 to +10.6.
- **Profit is flat from 0.60 to 0.75 ($104–109), then falls.** Above about 0.75 the extra fills add volume but no
  money, and above about 0.80 they subtract it.
- **The fill rate does not depend on the ceiling:** 20–23% throughout. The ceiling removes orders; it does not make
  the rest more or less likely to fill.
- **Every ceiling from 0.50 to 0.98 is positive in all three unseen periods**, and so are 86–99% of the sensible exits
  at each ceiling (every exit except t0.5, t1 and the spot reversals). The strategy stays profitable in replay at any ceiling; lower ceilings are simply better per share.
- **The marginal fills lose.** Fills with an ask above 0.60 made −3.4c/share unseen (36 fills); fills above 0.75 made
  −7.1c (19 fills). Section 13 has the tests.
- **Robust range: 0.60–0.75.**
  - Every value in it beats the baseline in each unseen period and on total profit.
  - 0.74 is the value training picks on t-stat, with validation agreeing.
  - Below 0.55 the per-share numbers stay high, but fills fall by more than half and total profit drops ($86 at 0.50).

### Why cheap tokens do better (by entry price, no ceiling, production exit)

| ask | fills | win | c/share | train | unseen | edge ev − ask | best move within 30 s |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0.05–0.20 | 31 | 84% | +16.6 | +25.2 (17) | +6.5 (10) | +14.9 | +34.9 |
| 0.20–0.40 | 47 | 89% | +16.3 | +13.6 (30) | +25.5 (12) | +14.0 | +32.3 |
| 0.40–0.60 | 64 | 78% | +10.8 | +8.7 (33) | +13.1 (15) | +13.1 | +23.3 |
| 0.60–0.70 | 37 | 57% | +2.2 | +5.4 (14) | −2.5 (8) | +10.6 | +14.3 |
| 0.70–0.80 | 38 | 53% | +0.4 | +1.7 (12) | −0.7 (17) | +9.3 | +11.5 |
| 0.80–0.90 | 18 | 33% | −2.2 | −0.7 (8) | −4.0 (6) | +7.6 | +9.1 |
| 0.90–0.95 | 7 | 14% | −9.4 | −0.5 (1) | −15.5 (4) | +6.4 | +6.2 |

- **Moves are smaller near the top.** A token near 0.9 has at most 10c of room, so the post-shock move shrinks
  (+35c → +6c within 30 s). The costs do not shrink with it: two taker fees, the spread and the 2c sell concession.
- **The fee is largest in the middle.** p(1−p) peaks at 0.50, but the move shrinks faster than the fee, so above about
  0.65 the costs take the whole move.

## 8. Exit analysis

**The price after a fill** (288 fills, ceiling 0.98, before the budget):

- **Peak within 30 s:** the best bid minus the entry averages +21.3c (median +17c).
- **Reversals:** 39% came back below the entry after that peak.
- **How often and how fast targets were reached within 30 s:**

| target | reached within 30 s | median time |
|---|---:|---:|
| +1c | 96% | 1.7 s |
| +5c | 88% | 1.8 s |
| +10c | 76% | 2.1 s |
| +15c | 58% | 4.3 s |
| +20c | 45% | 5.2 s |

**Money left on the table:**

- **Every exit leaves 11–16c/share.** The production exit realises +10.3c gross of the +21.3c available and leaves
  11.1c.
- **The gap cannot be closed with a rule.** No rule knows in advance which fills will run.
- **Target exits do not capture it either:**
  - a small target caps the winners;
  - a large one waits through reversals;
  - held to settlement, the positions that miss their target lose everything.

**Paired test** (each exit minus the production exit, on the same 249 budget-free fills, clustered bootstrap):

| exit | train | unseen | all | 95% CI (all) |
|---|---:|---:|---:|---|
| gap_closed_30 | +1.44 | −0.07 | +0.35 | [−1.0, +1.7] |
| edge_gone_30 | +1.52 | −0.11 | +0.33 | [−1.3, +1.7] |
| gap_closed_10 | +0.62 | +0.95 | +0.23 | [−0.7, +1.2] |
| edge_gone_10 | +0.74 | +0.34 | +0.04 | [−0.8, +0.9] |
| t10 | +0.34 | −0.27 | −0.00 | [−1.3, +1.2] |
| t5 | −0.53 | +0.20 | −0.35 | [−0.7, +0.0] |
| tp3_10 | −0.38 | +0.17 | −0.35 | [−1.3, +0.6] |
| tp2_settle | −0.38 | −0.46 | −0.41 | [−2.4, +1.4] |
| tp10_20 | −0.14 | −0.16 | −0.51 | [−2.1, +0.9] |
| t20 | −0.75 | −1.25 | −1.26 | [−3.7, +1.0] |
| tp10_settle | −1.78 | −0.20 | −1.77 | [−4.6, +0.8] |
| tp8_settle | −2.06 | −1.50 | −2.28 | [−4.9, −0.0] |
| spot_rev_20 | −5.62 | −2.52 | −4.73 | [−6.2, −3.4] |
| t1 | −7.13 | −3.37 | −5.58 | [−7.5, −3.8] |
| t0.5 | −12.85 | −9.25 | −10.71 | [−13.4, −8.5] |

- **None of the 80 alternatives is significantly better.**
- **21 are significantly worse:**
  - every hold of 3 s or less (t0.5, t1, t2, t3);
  - every target capped at 5 s;
  - tp8_settle;
  - all spot-reversal and token-reversal exits. They cut the position during the first wiggle, before the typical
    +10c move at 2–5 s.
- **The best exits are statistically the same as the production exit:** 5–10 s holds, the edge_gone and gap_closed
  event exits, and the targets with a 10–30 s limit.
- **Best robust exit range: a 5–10 s hold.** The production 5 s, extended to 10 s while the spot keeps moving, is
  already in it; keep it.

## 9. MAX_ASK × exit interaction

The table gives each exit's effect (the exit minus the production exit) on fills with an ask at or below 0.60, the same
on fills above 0.60, and the difference of the two, which is the interaction.

| exit | ask ≤ 0.60 | ask > 0.60 | interaction | 95% CI |
|---|---:|---:|---:|---|
| t0.5 | −15.45 | −3.66 | −11.79 | [−15.5, −8.2] |
| t1 | −7.96 | −2.03 | −5.94 | [−9.1, −3.0] |
| spot_rev_10 | −6.82 | −2.16 | −4.66 | [−7.2, −2.2] |
| tp12_settle | −0.39 | −5.64 | +5.24 | [−1.1, +12.4] |
| tp8_settle | −0.59 | −4.81 | +4.23 | [−1.2, +10.3] |
| tp3_settle | +0.18 | −3.07 | +3.25 | [−1.3, +8.3] |

- **Where it is significant, it is only for exits that are already rejected.** The CI excludes zero for 8 of 80 exits,
  against about 4 expected by chance. All 8 are exits that leave too early (t0.5, t1, the spot reversals). They cost
  more on cheap tokens because cheap tokens move more.
- **For the exits worth considering, the interaction is not significant.**
  - Targets held to settlement look 3–5c less bad on cheap tokens, but every CI includes zero.
  - The two-dimensional grid (the sweep CSV) shows the same thing: at 0.60 and at 0.95 the ranking of the sensible
    exits is noise around the production exit.
- **So the two parameters can be set independently.** Pick the ceiling, and keep the production exit.

## 10. Asset analysis (production exit)

| asset | 0.60: fills | c/share | share of profit | 0.95: fills | c/share | share of profit |
|---|---:|---:|---:|---:|---:|---:|
| BTC | 48 | +14.2 | 32% | 62 | +12.3 | 39% |
| BNB | 27 | +12.9 | 16% | 43 | +7.6 | 17% |
| ETH | 26 | +12.3 | 15% | 52 | +5.2 | 14% |
| SOL | 21 | +15.5 | 15% | 34 | +5.8 | 10% |
| XRP | 18 | +12.9 | 11% | 31 | +6.7 | 11% |
| DOGE | 13 | +15.4 | 10% | 19 | +9.0 | 9% |
| HYPE (excluded in production) | 20 | −2.7 | – | 33 | −2.9 | – |

- **The ceiling helps every included asset.** ETH and SOL gain most, going from +5–6c to +12–15c.
- **No asset carries the result.** BTC is the largest contributor, and the ceiling makes the profit less
  BTC-dependent (39% → 32%).
- **HYPE loses at every ceiling.** The production exclusion is right.
- **Per-asset ceilings are not justified.** 13–48 fills per asset cannot support separate values.

## 11. Market-condition analysis

This asks whether the right ceiling depends on the market. The table gives c/share (fills) over all periods, by entry
price bucket.

| condition | bucket | ask ≤ 0.60 | 0.60–0.80 | > 0.80 |
|---|---|---|---|---|
| shock z1 | 3–4 / 4–6 / ≥ 6 | +9.9 / +14.6 / +22.5 | +2.1 / −0.8 / −0.2 | −3.7 / −5.6 / −2.0 |
| time left | 60–120 / 120–200 / 200–300 s | +22.1 / +15.1 / +9.4 | −0.4 / +1.4 / +0.6 | −6.3 / +2.3 / – |
| spread | ≤ 1.5 / 1.5–2.5 / 2.5–3.5c | +14.3 / +9.2 / +15.4 | +0.7 / −0.4 / +2.9 | −6.1 / +1.1 / −1.5 |
| lag (ev − ask) | 4–8 / ≥ 8c | +8.1 / +14.9 | −3.3 / +2.3 | −4.5 / −2.1 |
| volatility | < 0.6 / 0.6–1.2 / ≥ 1.2 bps/s | +12.8 / +14.5 / +15.4 | −2.3 / +5.5 / −0.6 | −5.5 / −2.4 / −4.7 |
| direction | DOWN / UP | +14.0 / +13.6 | +0.9 / +0.3 | −4.5 / −2.7 |

- **The ordering never flips.** In every condition, tokens at or below 0.60 make money, 0.60–0.80 are about flat, and
  above 0.80 lose. No condition turns expensive tokens reliably profitable. The best case, 0.60–0.80 at medium
  volatility (+5.5c on 26 fills), is one cell of 20 and was not tested separately.
- **The dynamic ceiling does not survive.** The rule was "0.60, or up to 0.80/0.95 when the lag is at least 4, 6, 8
  or 10c".
  - The pick chosen on training was "0.60, or up to 0.80 with a lag of at least 8c". Unseen it made +10.58c on 53
    fills.
  - The flat 0.74 ceiling made +10.53c on 55 fills.
  - It adds nothing, so it is not recommended (a budget-free comparison; the report's market-condition section).

## 12. Stress testing

The table gives unseen pooled c/share (fills). The latency rows use datasets rebuilt with scaled latency; the other rows
are applied to each trade.

| scenario | baseline 0.95/live | **0.74/live** | 0.64/tp2_settle | 0.50/t20 | 0.95/edge_gone_10 |
|---|---:|---:|---:|---:|---:|
| as measured | +6.25 (72) | **+10.53 (55)** | +12.56 (41) | +14.63 (31) | +6.61 (72) |
| latency +25% | +5.17 | +9.89 | +10.62 | +13.44 | +5.18 |
| latency +50% | +4.83 | +9.50 | +12.61 | +14.01 | +5.46 |
| latency ×2 | +5.38 | +9.68 | +11.97 | +13.70 | +6.13 |
| spread +2c | +4.24 | +8.52 | +10.61 | +12.61 | +4.59 |
| slippage +1c per leg | +4.24 | +8.52 | +10.61 | +12.61 | +4.59 |
| fees ×1.5 | +5.01 | +9.22 | +11.26 | +13.43 | +5.36 |
| fees ×0.5 | +7.50 | +11.85 | +13.87 | +15.83 | +7.85 |
| 25% of fills lost at random | +5.76 | +10.48 | +13.48 | +15.03 | +5.13 |
| **the best 25% of fills lost** | **−1.86** | **+1.15** | +0.56 | +2.00 | −1.36 |

- **Latency, spread, slippage and fees each cost 1–2c/share.** Every lower-ceiling candidate stays well above the
  baseline under all of them.
- **Losing fills at random does not matter.**
- **Losing the best fills does.** Losing the best quarter is the adverse-selection case: the orders the venue fills
  are the ones where the price did not run.
  - In that case the baseline is negative out of sample, and the lower ceilings are close to zero.
  - The edge lives in the right tail. If live execution misses the fast movers, the lower ceiling limits the damage
    but does not rescue the strategy.

## 13. Statistical confidence

### Does MAX_ASK matter?

Yes. The test compares fills above the ceiling with fills below it (budget-free, MAX_ASK 0.95 fills, production exit,
bootstrap by window).

| split | period | below: fills, c/share | above: fills, c/share | difference | 95% CI |
|---:|---|---|---|---:|---|
| 0.60 | train | 84, +14.17 | 36, +2.74 | +11.43 | [+6.7, +16.6] |
| 0.60 | validation | 27, +11.28 | 28, −0.23 | +11.52 | [+5.5, +17.6] |
| 0.60 | unseen | 38, +14.96 | 36, −3.39 | +18.35 | [+9.5, +28.6] |
| 0.60 | all | 149, +13.85 | 100, −0.30 | +14.15 | [+10.3, +18.1] |
| 0.75 | train | 107, +12.14 | 13, −0.76 | +12.90 | [+7.2, +19.5] |
| 0.75 | validation | 43, +6.96 | 12, −0.08 | +7.03 | [+1.0, +13.2] |
| 0.75 | unseen | 55, +10.57 | 19, −7.09 | +17.66 | [+9.4, +27.2] |

The effect is significant on its own in every period, including training alone. It is not an artefact of choosing on
the unseen data.

### Does the exit matter?

- **No alternative is significantly better than the production exit.** The best is +0.35c [−1.0, +1.7].
- **21 of 80 are significantly worse**, as section 8 shows. Only about 2 would be expected by chance.

### Interaction

No, not for any exit worth using (section 9).

### Locked candidates

- **MAX_ASK only (0.74/live) has the strongest out-of-sample record:**
  - t 2.9;
  - clustered CI [+3.3, +17.8];
  - bootstrap CI [+4.2, +18.2];
  - +6.3c without the top 5 trades;
  - its best single window is 28% of unseen profit.
- **The baseline's out-of-sample record is weaker:** t 2.0, bootstrap CI [+0.7, +12.6], +2.75c without the top 5
  trades, and 36% of its profit from one window.

### By period

- **unseen_1 is weak for every setting** (+1.9 to +5.4c), and there the ceilings barely differ.
- **The advantage of a lower ceiling comes mostly from unseen_2 and unseen_3.** With about 20–40 fills per period, one
  period can easily be flat.

## 14. Limitations

1. **The replay fills about twice as often as live.**
   - Over the live bot's own run (09-24 15:23 → 09-25 00:22), the replay of its exact configuration (1 position,
     5 buys per hour, MAX_ASK 0.95) placed 40 orders and filled 6 (15%) for +$3.64.
   - The live bot placed 36 orders and filled 3 (8%) for −$0.13. Some of the gap is the quote-age skip, which the
     archive cannot see.
   - If the missing live fills are the good ones, the "best 25% lost" row in section 12 is closer to the truth than
     the headline.
2. **The samples are small.** There are 21 hours and 55–72 fills unseen per setting. The t-statistics of 2–3 are
   modest, and the CIs are wide (±7c).
3. **The unseen data is not fully blind for the ceiling question.** The 0.60 cut came from an earlier look at price
   buckets over most of this data (the user's question of 2026-09-24). The locked value, 0.74, was picked on
   training and validation only. unseen_3b (after 19:15 on 09-24) had not been examined.
4. **The data covers about 2.5 days of one market regime.** There are no weekends and no macro events.
5. **Depth is simplified.** The model assumes 5 shares at the limit and FOK with no partial fills, which matches the
   5-share orders tested. Larger sizes were not tested and would fill worse.
6. **Profit targets need a 0.5c net profit after both fees, as the live bot does.** The low targets (1–3c) therefore
   behave like about 4c. A pure "sell at +1c" is not what the bot can do without paying to exit.

## 15. Conclusion

- **MAX_ASK is the parameter that matters.**
  - Tokens above about 0.60–0.75 make no money after costs: small moves, the same fees.
  - Lowering the ceiling from 0.95 to 0.60–0.75 keeps total replay profit, raises per-share net by half to double, and
    cuts the worst drawdown by a third.
  - The effect is significant in every period on its own.
- **The exit barely matters.** The production 5–10 s exit is as good as anything among 80 alternatives. The fast
  exits and the reversal exits are clearly worse.
- **The interaction does not matter** among sensible exits.
- **Recommended for PAPER testing: MAX_ASK 0.74, with the production entry and exit unchanged.**
  - 0.74 is the locked, training-chosen value; anywhere in 0.60–0.75 is supported.
  - The running tpb-spotlag-paper uses 0.60, but it also runs looser entry gates (4c slippage, 1c edge, 5c spread).
    It is therefore not a clean test of the ceiling alone.
  - A clean test needs a paper process with only MAX_ASK changed. After the study, with the user's OK,
    `tpb-spotlag-paper-ma74` was started at 01:43 UTC on 2026-09-25 (`ecosystem.spotlag.config.js`). It runs the
    live rule exactly (tick-driven, ask + 2c, 2c edge, 3c spread, 4 positions, 60 buys per hour) with MAX_ASK 0.74,
    at nice 10. Trades go to `data/live_trades.spotlag-ma74.jsonl`.
    - If the replay holds, it should show about 3.9 fills an hour at about +10c/share.
    - It counts against the replay if the fill rate is far below 22% (the live bot's 8%), or if the fills lose at the
      prices the replay says win.
- **Not recommended:**
  - MAX_ASK ≥ 0.80;
  - MAX_ASK 0.98 (it does nothing live, because `max_trade_price` caps at 0.95);
  - MAX_ASK ≤ 0.50 (volume halves and total profit falls);
  - any exit of 3 s or less;
  - spot- or token-reversal exits;
  - targets held to settlement (every missed target became a total loss);
  - the dynamic lag-based ceiling;
  - per-asset ceilings.
- **Nothing here justifies a real-money change.** The replay's live fill rate problem (limitation 1) has to be
  resolved on paper first.

## Reproduce

```
.venv/bin/python scripts/research_repriced_momentum.py cache          # the archive cache (3,480 windows)
for s in 1 1.25 1.5 2; do .venv/bin/python scripts/research_spot_lag_parameter_sweep.py build --latency-scale $s; done
.venv/bin/python scripts/research_spot_lag_parameter_sweep.py sweep  # -> results/spot_lag_parameter_sweep.csv
.venv/bin/python scripts/analyze_spot_lag_parameter_sweep.py         # -> _summary.csv, _locked.json, _report.md
```
