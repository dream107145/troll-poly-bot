# Market-open microstructure: can the pre-open prediction be traded after the open? (2026-09-25)

## Verdict: no tradable edge was found. Nothing here should be paper-traded.

The previous study showed a real but small prediction (58–59 % directional accuracy at T − 3 s) that could not be bought at the open because market makers pull their quotes. This study asked whether waiting for the book to recover, resting inside the spread, anchoring on the pre-open price, blending it with the model, trading either side by expected value, or gating on how fast liquidity is recovering, turns that prediction into positive net expectancy. On the same execution model, with fees, spread, slippage, latency, fill probability and adverse selection all charged:

- **Waiting does not create an edge; it removes the reason to trade.** Once the book is tight (BTC at once, ETH ~0.5 s, SOL ~1.5 s, the others 2–2.5 s or never), the ask already contains the prediction. A taker buy of the model's side then marks out at about **−1c at every horizon from 50 ms to 30 s** (the half-spread, with no drift after it), and nets **−4.4 to −5.1c/share** after two fees at every delay from 0 to 5 s (350–1,560 trades per row).
- **Resting orders are worse, not better.** Calibrated on 4,853 real trades from the tick recording, a resting bid around the open fills 6–30 % of the time, and **85–95 % of those fills are the ask falling down through the order**. Post-fill markouts are −1.4 to −4.6c before any exit cost, at every level from the bid to 10c under the pre-open fair. In the archive replay every maker variant loses 3–5.5c/share on development data and −3.5c on TEST (23 fills).
- **There is no opening mispricing to fade.** The "shock" is a 30c-wide spread around an unchanged centre. From +1 s the pre-open fair explains none of the mid's later movement (β 0.00–0.05, z ≤ 2) while the model still does (z 6–11); executable dislocations against the pre-open fair produced 28 development fills with 0 wins.
- **The pre-open price and the model carry the same information.** As predictors of the outcome they are equivalent (VALIDATION Brier 0.2403 vs 0.2397); fixed blends match them and the TRAIN-fitted adaptive blend is worse out of sample.
- **The sweep found nothing.** Of 126,720 configuration × exit rows (4 anchors × 10 delays × 8 thresholds × 2 side rules × 4 spread caps × 3 score gates, plus 6 maker levels × 5 resting times × 4 posting times × 3 gates × 2 fallbacks, × 12 exits), 200 (1.1 % of those with ≥ 30 TRAIN trades) were positive on TRAIN and **0 passed the overfitting defenses** (t ≥ 1.5, gross > 0, no asset > 60 %, no side > 80 %, positive without the best market, positive neighbours, VALIDATION ≥ 10 trades and > 0).
- **TEST (09-25 00:40–13:20 UTC, 957 markets) and FORWARD (13:25–15:20 UTC, 152 markets, archived after the previous study):** every family is negative or has 0–6 trades. The only positive TEST cells are settlement holds (6–31 trades, bootstrap intervals from −46c to +31c), and their FORWARD result is empty or −55c.

Nothing was enabled. `TPB_NEXT_MARKET_TWAP_ENABLED` is false, the strategy module has no real-money path, no live process or configuration was touched, and no order was placed.

### Answers to the thirteen questions

1. **Does the 58.9 % prediction contain economically usable information?** It contains information (about 5c/share over a random side in the unfiltered case, and a z of 6–11 in explaining the mid's next move from +1 s), but **not usable information**: the gap between the model and the executable price, once a price exists, is smaller than the spread plus two fees, and nothing in the entry, exit, side, timing or filter space recovers it.
2. **At what exact time does executable liquidity become usable?** *Size* is available at T + 0 for 73–100 % of markets (the stub quotes carry it). A *price* within 2c of the touch: BTC at T + 0 (its book never widens), ETH 350–600 ms, SOL 1.3–1.5 s, XRP ~1.5–1.8 s, BNB ~2.1–2.5 s, DOGE ~2.3–2.6 s (71 % reach it within 5 s), HYPE ~2.0–2.5 s (33–61 % reach it). Event-level and archive figures agree.
3. **Does waiting improve net expectancy or eliminate the edge?** It eliminates the edge. The 2c-gated taker finds fewer and fewer trades as the delay grows (23 → 30 → 18 → 0 at 0 / 0.5 / 1 / 2 s) because the price catches up with the model; what it does find loses 1.5–4.4c. The ungated taker loses the same 4.4–5.1c at every delay: it always pays the spread and fees and never earns a drift.
4. **Does resting liquidity improve EV after adverse selection?** No. The maker saves the entry fee (~1.6c) and loses more than that to adverse selection: the bid keeps falling after a cross-through fill (markout −3.7c at 0.5 s, −1.9 to −2.6c at 2–30 s). Every level and resting time is negative on TRAIN, VALIDATION and TEST; hazard × 5 and "maker then taker" do not change the sign.
5. **Is the pre-open price a better fair-value predictor than the model?** Neither is better. Brier 0.2434 vs 0.2469 (TRAIN), 0.2403 vs 0.2397 (VALIDATION); blends 0.2394–0.2397 on VALIDATION; the adaptive blend 0.2472. The two encode the same thing (spot vs the forming strike).
6. **Does the opening liquidity shock create temporary mispricing?** No. The median UP ask at T + 0 is 4.5c above the pre-open fair and the bid 4c below it: a wide spread, not a moved price. 53–67 % of markets show |ask − fair| ≥ 3c at some point in the first 5 s, but the ask is above fair, i.e. the "mispricing" is the cost of crossing.
7. **Does it revert or continue?** The empty-book mid "reverts" as the book re-forms (β 0.5 from +0.25 s), which is not a tradable price move. Real deviations at +1 s (≥ 3c, n ≈ 230–270) revert half or more only 35 % of the time and continue 43 % of the time; median fraction reverted 0.12 over 3 s.
8. **Are BTC/ETH materially different?** Yes, in liquidity: their books are tight from T + 0 / +0.5 s, they take 83–91 % of all fills on tight books, and they lose the least (−4.4c and −4.3c on the +1 s taker, −0.2 to −4.5c on the gated rules). They are not different in sign: no BTC or ETH cut is positive out of sample.
9. **Does UP or DOWN behave differently?** Not consistently. Development: UP −4.8c, DOWN −4.1c (+1 s taker); maker UP −2.7c, DOWN −4.5c on TEST; the settlement family that looked DOWN-heavy in the previous study is UP-heavy here (67–82 %). The sign never depends on the side.
10. **Is there any strategy with positive net expectancy that survives TEST and FORWARD?** **No.** The best-looking TRAIN rules (settlement holds, +5 to +8c on 32–48 trades, t < 1.5) are +1.5c / −17c / −29c on TEST with 6–11 trades and 0 / −46c / −61c on FORWARD. The 2c-gated settlement control is +10c on 31 TEST trades (CI −6 to +27c) after −5.7c on TRAIN, and −55c on its 2 FORWARD trades.
11. **What minimum edge is required after fees and realistic execution?** The formula's expected edge is not realised at any level: across 1,467 development fills, realised P&L is −5c/share for expected edges of −12c and −2c for expected edges of +6c (figure 7). Requiring 0.5c to 10c only reduces the count. The honest answer is that no threshold on this signal is high enough, because the information the threshold measures is already in the price by the time the price can be paid.
12. **What exact market conditions must exist before a trade is allowed?** None were found under which a trade has positive expectancy. Every regime cut (time of day, distance from strike, confidence, pre-open confidence, opening spread, opening depth, volatility, momentum, asset, side, recovery-score decile) loses 3.4–7.1c/share on development data.
13. **What conditions should cause the bot to do nothing?** All of them. Specifically, the book wider than 2c (the whole first 1–2.5 s on every asset but BTC and ETH), any resting order (it fills only against you), any anchor-based dislocation (it is stale data, not price), and, once the book is tight, the model's side at the ask (no drift after the fill).


---

## 1. What the existing code and data can and cannot answer

Everything here reuses the previous study's pipeline (`scripts/research_next_market_twap.py`, `scripts/analyze_next_market_twap.py`, `strategy/next_market_twap.py`). Nothing in it was rewritten; the new script `scripts/research_open_microstructure.py` extends the simulator (`MSim`, `enter2`) and adds the analyses.

| what the task asks for | where it comes from | resolution and caveats |
|---|---|---|
| market lifecycle, open/close timestamps | `data/charts/<slug>.json` (`open_ts`, `close_ts`), the venue's resolved outcome | exact |
| order book replay | the 50 ms as-of grid of the archive (both tokens' touch and 2-tick depth, `data/replay/next_market/grid.npz`) | the archive samples every ~224 ms; 50 ms and 100 ms offsets are one archive step |
| historical depth | depth within 2 ticks of each touch only | **depth at 5 and 10 ticks does not exist in any recording**; the 3-level split (25 / 37.5 / 37.5 %) is the tick recording's median |
| individual trades | `data/ticks/ticks_20260923_204148.jsonl.gz`: every token trade with price, size and taker side, venue-timestamped | 90 minutes, 126 markets, 4,853 trades within ±30 s of an open; inside TRAIN |
| market-maker quote withdrawal / re-entry | the same recording: every book change with touch sizes | event resolution, the same 90 minutes |
| execution simulator | `analyze_next_market_twap.Sim`: measured latency draws, 500 ms venue proxy, 3-level FOK/FAK fills, taker sells at bid − 2c retried every 2 s, taker fee 0.07·p·(1 − p) | validated on 694 venue-timestamped spot-shock orders (`docs/spot_lag_study_2026-09-23.md`) |
| latency | 41 measured presend and venue-RTT samples (one-way p50 181 ms, p90 379 ms) | + 500 ms venue proxy; explicit-delay and ×1.25/1.5/2 sensitivities |
| splits | pinned by epoch: TRAIN ≤ 09-24 11:10, VALIDATION ≤ 09-25 00:35, TEST ≤ 09-25 13:20 UTC, **FORWARD = after that** | FORWARD here: 09-25 13:25–15:20 UTC, 24 epochs, 152 markets; archived after the previous study, never examined before the lock |
| fills | FOK at the arrival ask VWAP within the limit; FAK partial; **new:** resting orders (§5) | never at the decision quote |

**Data volume.** 4,684 markets over 785 epochs (09-22 15:30 to 09-25 15:20 UTC). Snapshots at all 20 offsets: 4,533 markets × 20 = 90,660 rows in `results/open_micro_snapshots.csv`.

## 2. The market-open state machine

`results/open_micro_snapshots.csv` holds, per market and offset (T − 30 s … T + 30 s, the 20 required points): the model's P(UP)/P(DOWN) at T − 3 s, the pre-open fair (the last pre-open Up mid with a spread ≤ 5c in [−10 s, 0)), both tokens' bid/ask, spreads, 2-tick depth on both sides, the $1/$2/$5/$10 VWAP against the 3-level depth model (blank when the order cannot fill or is under the 5-share venue minimum), book updates in the last second, book imbalance, spot, 1 s and 5 s spot returns, σ, distance from the strike, distance from the pre-open mid, market age, time left, book age and the spot age at the prediction. Trades, trade direction and trade volume exist only in the tick recording (§5) and are not in this table.

Pre-open fair value exists for 71.9 % of markets; the rest never showed a two-sided book ≤ 5c wide in the 10 s before the open (mostly HYPE, BNB, DOGE and XRP).

## 3. The liquidity recovery curve

### Liquidity by bucket after the open (archive, UP token, TRAIN+VALIDATION): median spread c / p90 / median 2-tick depth / P(executable >= 5 shares)

| bucket | BNB | BTC | DOGE | ETH | HYPE | SOL | XRP |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0-50 ms | 78 / 78 / 705 / 100% | 1 / 3 / 447 / 100% | 40 / 76 / 26 / 100% | 11 / 23 / 36 / 98% | 64 / 78 / 204 / 99% | 4 / 5 / 63 / 88% | 39 / 78 / 10 / 73% |
| 50-100 ms | 78 / 78 / 705 / 100% | 1 / 3 / 443 / 100% | 40 / 76 / 26 / 100% | 11 / 24 / 36 / 97% | 64 / 78 / 204 / 99% | 4 / 6 / 63 / 88% | 39 / 78 / 10 / 73% |
| 100-250 ms | 78 / 78 / 705 / 100% | 1 / 3 / 355 / 100% | 40 / 76 / 26 / 100% | 10 / 22 / 40 / 97% | 64 / 78 / 204 / 99% | 4 / 14 / 63 / 88% | 38 / 78 / 10 / 73% |
| 250-500 ms | 36 / 78 / 37 / 100% | 1 / 2 / 185 / 100% | 29 / 58 / 36 / 100% | 5 / 13 / 47 / 98% | 65 / 78 / 204 / 99% | 8 / 16 / 42 / 87% | 21 / 78 / 34 / 73% |
| 500-750 ms | 18 / 36 / 28 / 100% | 1 / 1 / 212 / 100% | 16 / 32 / 50 / 100% | 3 / 6 / 45 / 99% | 65 / 78 / 204 / 99% | 8 / 12 / 48 / 88% | 12 / 23 / 56 / 73% |
| 750-1000 ms | 17 / 26 / 22 / 100% | 1 / 1 / 228 / 100% | 15 / 24 / 47 / 100% | 2 / 5 / 40 / 99% | 66 / 78 / 204 / 99% | 8 / 12 / 50 / 88% | 11 / 18 / 52 / 73% |
| 1000-1500 ms | 16 / 24 / 19 / 100% | 1 / 1 / 290 / 100% | 14 / 21 / 51 / 100% | 2 / 5 / 48 / 99% | 65 / 78 / 204 / 99% | 7 / 11 / 49 / 88% | 10 / 16 / 52 / 73% |
| 1500-2000 ms | 13 / 19 / 29 / 100% | 1 / 1 / 645 / 100% | 11 / 18 / 90 / 100% | 1 / 2 / 111 / 99% | 31 / 78 / 21 / 99% | 3 / 9 / 76 / 88% | 4 / 13 / 63 / 74% |
| 2000-3000 ms | 4 / 13 / 36 / 100% | 1 / 1 / 1294 / 100% | 4 / 11 / 96 / 100% | 1 / 1 / 224 / 100% | 6 / 21 / 15 / 99% | 1 / 4 / 82 / 88% | 1 / 5 / 69 / 75% |
| 3000-5000 ms | 3 / 6 / 42 / 100% | 1 / 1 / 1510 / 100% | 4 / 7 / 108 / 100% | 1 / 1 / 257 / 100% | 5 / 11 / 15 / 99% | 1 / 2 / 98 / 89% | 1 / 2 / 74 / 76% |

DOWN token, same:

| bucket | BNB | BTC | DOGE | ETH | HYPE | SOL | XRP |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0-50 ms | 78 / 78 / 705 / 100% | 1 / 3 / 482 / 100% | 40 / 76 / 26 / 100% | 11 / 23 / 35 / 98% | 64 / 78 / 204 / 99% | 4 / 5 / 63 / 88% | 39 / 78 / 10 / 73% |
| 250-500 ms | 36 / 78 / 37 / 100% | 1 / 2 / 199 / 100% | 29 / 58 / 33 / 100% | 5 / 13 / 40 / 99% | 65 / 78 / 204 / 99% | 8 / 16 / 36 / 88% | 21 / 78 / 35 / 73% |
| 1000-1500 ms | 16 / 24 / 29 / 100% | 1 / 1 / 279 / 100% | 14 / 21 / 56 / 100% | 2 / 5 / 50 / 99% | 65 / 78 / 204 / 99% | 7 / 11 / 50 / 88% | 10 / 16 / 68 / 73% |
| 2000-3000 ms | 4 / 13 / 36 / 100% | 1 / 1 / 1225 / 100% | 4 / 11 / 90 / 100% | 1 / 1 / 215 / 100% | 6 / 21 / 15 / 99% | 1 / 4 / 82 / 88% | 1 / 5 / 61 / 75% |
| 3000-5000 ms | 3 / 6 / 41 / 100% | 1 / 1 / 1506 / 100% | 4 / 7 / 100 / 100% | 1 / 1 / 249 / 99% | 5 / 11 / 15 / 99% | 1 / 2 / 95 / 89% | 1 / 2 / 66 / 76% |

### Event-level (tick recording, 2026-09-23, 18 markets per asset): median spread c / p90 / P(spread <= 2c)

| bucket | BNB | BTC | DOGE | ETH | HYPE | SOL | XRP |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0-50 ms | 78 / 78 / 0% | 1 / 3 / 84% | 40 / 40 / 0% | 9 / 22 / 11% | 78 / 78 / 0% | 4 / 18 / 29% | 30 / 40 / 0% |
| 50-100 ms | 78 / 78 / 0% | 1 / 2 / 91% | 40 / 40 / 0% | 5 / 10 / 17% | 78 / 78 / 0% | 16 / 27 / 13% | 30 / 40 / 0% |
| 100-250 ms | 39 / 78 / 0% | 1 / 3 / 88% | 40 / 40 / 0% | 5 / 8 / 22% | 78 / 78 / 0% | 6 / 24 / 31% | 30 / 40 / 0% |
| 250-500 ms | 18 / 28 / 0% | 1 / 2 / 95% | 17 / 23 / 4% | 3 / 7 / 40% | 78 / 78 / 0% | 6 / 14 / 36% | 14 / 24 / 0% |
| 500-750 ms | 17 / 27 / 0% | 1 / 1 / 98% | 15 / 22 / 6% | 3 / 6 / 45% | 78 / 78 / 0% | 7 / 13 / 30% | 14 / 19 / 1% |
| 750-1000 ms | 15 / 22 / 0% | 1 / 1 / 100% | 13 / 20 / 6% | 2 / 4 / 52% | 78 / 78 / 0% | 8 / 13 / 22% | 12 / 16 / 0% |
| 1000-1500 ms | 14 / 19 / 6% | 1 / 1 / 99% | 13 / 23 / 7% | 1 / 3 / 78% | 78 / 78 / 0% | 7 / 12 / 29% | 11 / 15 / 18% |
| 1500-2000 ms | 7 / 16 / 14% | 1 / 1 / 100% | 7 / 14 / 8% | 1 / 2 / 96% | 5 / 78 / 13% | 1 / 6 / 75% | 2 / 11 / 66% |
| 2000-3000 ms | 3 / 7 / 37% | 1 / 1 / 100% | 4 / 7 / 15% | 1 / 1 / 99% | 4 / 7 / 27% | 1 / 2 / 90% | 1 / 2 / 90% |
| 3000-5000 ms | 2 / 3 / 83% | 1 / 1 / 100% | 3 / 6 / 31% | 1 / 1 / 100% | 4 / 6 / 23% | 1 / 2 / 97% | 1 / 2 / 98% |

### Time to recovery after the open (median ms; share reaching it within 5 s)

| asset | tick: <=2c | <=3c | <=5c | archive: <=2c | <=3c | <=5c | depth >= $1 | $5 | $10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BNB | 2105 (100%) | 2010 (100%) | 1825 (100%) | 2500 (86%) | 2350 (94%) | 2200 (98%) | 0 (100%) | 0 (100%) | 0 (100%) |
| BTC | 0 (100%) | 0 (100%) | 0 (100%) | 0 (100%) | 0 (100%) | 0 (100%) | 0 (100%) | 0 (100%) | 0 (100%) |
| DOGE | 2250 (67%) | 1895 (78%) | 1690 (94%) | 2600 (71%) | 2250 (88%) | 2000 (97%) | 0 (100%) | 0 (100%) | 0 (100%) |
| ETH | 350 (100%) | 185 (100%) | 50 (100%) | 600 (100%) | 450 (100%) | 350 (100%) | 0 (100%) | 0 (100%) | 0 (100%) |
| HYPE | 1980 (61%) | 1870 (94%) | 1530 (100%) | 2525 (33%) | 2400 (56%) | 2100 (76%) | 0 (99%) | 0 (99%) | 0 (99%) |
| SOL | 1275 (100%) | 60 (100%) | 0 (100%) | 1500 (89%) | 725 (89%) | 0 (89%) | 0 (89%) | 0 (89%) | 0 (89%) |
| XRP | 1485 (100%) | 1475 (100%) | 1445 (100%) | 1800 (77%) | 1700 (77%) | 1600 (77%) | 0 (77%) | 0 (77%) | 250 (77%) |

What the curves say:
- **BTC's book never leaves.** 1c wide at every offset, hundreds of shares within 2 ticks, executable 100 % of the time.
- **ETH re-forms fastest** (≤ 5c within ~50 ms at event resolution, ≤ 2c by 350–600 ms), then **SOL** (≤ 5c at once, ≤ 2c at 1.3–1.5 s).
- **BNB, DOGE, XRP and HYPE** start at a 40–78c stub and take 1.5–2.6 s to reach 2c; DOGE and HYPE reach 2c in only 33–71 % of markets within 5 s.
- Depth is not the constraint for $5 orders: within 2 ticks there are usually 20–700 shares even at T + 0 (the stub quotes carry size). What is missing is a *price*, not size.
- The two sides are symmetric (complementary books).

**Answer to "at what exact time does executable liquidity become usable":** it depends on the asset and it is not a single moment. Executable *size* exists at T + 0 for 73–100 % of markets; a *tight* price (≤ 2c) exists from T + 0 on BTC, from ~0.5 s on ETH, ~1.5 s on SOL, and ~2–2.5 s (or never) on the others. By the time the price is tight it already contains the prediction (§6, §8).

## 4. Opening shock and the pre-open anchor

### Opening shock (TRAIN+VALIDATION, markets with a tight pre-open book: 71.9 %, n 2461)

| ask at | n | p10 | p50 | p90 | |shock|>=3c | |shock|>=10c | p50 BNB | p50 BTC | p50 DOGE | p50 ETH | p50 HYPE | p50 SOL | p50 XRP |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| +0 s | 2461 | +0.5 | +4.5 | +35.5 | 53% | 38% | +32.8 | +0.5 | +19.5 | +3.5 | +4.8 | +2.0 | +18.0 |
| +0.25 s | 2461 | +0.5 | +5.0 | +33.5 | 57% | 37% | +30.0 | +0.5 | +17.0 | +2.5 | +4.8 | +2.0 | +16.5 |
| +0.5 s | 2461 | -1.0 | +2.5 | +14.5 | 54% | 18% | +8.5 | +0.5 | +8.5 | +1.0 | +4.3 | +2.7 | +6.5 |
| +1 s | 2461 | -2.5 | +2.5 | +11.0 | 57% | 14% | +6.5 | +0.5 | +6.5 | +0.5 | +4.3 | +3.0 | +4.5 |
| +2 s | 2461 | -5.5 | +1.5 | +8.5 | 54% | 11% | +2.5 | +0.5 | +3.5 | +0.5 | +0.8 | +1.0 | +1.5 |
| +3 s | 2461 | -7.5 | +0.5 | +9.0 | 59% | 13% | +1.0 | +0.5 | +2.5 | +0.5 | -0.2 | +0.8 | +1.5 |
| +5 s | 2461 | -9.0 | +1.0 | +10.5 | 67% | 19% | +1.5 | +0.5 | +2.5 | +0.5 | -6.2 | +1.0 | +1.5 |

UP bid minus pre-open fair:

| bid at | n | p10 | p50 | p90 |
|---|---:|---:|---:|---:|
| +0 s | 2461 | -34.5 | -5.5 | -0.5 |
| +0.25 s | 2461 | -32.5 | -5.5 | -0.5 |
| +0.5 s | 2461 | -15.5 | -3.5 | +0.5 |
| +1 s | 2461 | -13.5 | -3.5 | +1.5 |
| +2 s | 2461 | -10.5 | -1.5 | +4.5 |
| +3 s | 2461 | -9.5 | -1.0 | +6.5 |
| +5 s | 2461 | -11.5 | -0.5 | +8.5 |

### Reversion: mid(t+h) - mid(t) regressed on (pre-open fair - mid(t)) and (model - mid(t)), jointly

| from t | horizon | n | beta pre-open | z | beta model | z | n |dev|>=3c | median fraction reverted | P(>= half reverted) | P(continued) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| +0.25 s | +1 s | 2461 | 0.494 | 20.0 | 0.288 | 15.7 | 1048 | 0.75 | 65% | 15% |
| +0.25 s | +3 s | 2461 | 0.527 | 15.6 | 0.407 | 16.2 | 1048 | 1.00 | 68% | 19% |
| +0.25 s | +10 s | 2461 | 0.467 | 8.8 | 0.436 | 11.0 | 1048 | 0.94 | 62% | 26% |
| +0.25 s | +30 s | 2461 | 0.469 | 6.0 | 0.412 | 7.1 | 1048 | 0.86 | 57% | 34% |
| +0.5 s | +1 s | 2461 | 0.232 | 10.9 | 0.207 | 12.7 | 973 | 0.13 | 31% | 29% |
| +0.5 s | +3 s | 2461 | 0.222 | 6.9 | 0.329 | 13.3 | 973 | 0.33 | 44% | 37% |
| +0.5 s | +10 s | 2461 | 0.162 | 3.2 | 0.332 | 8.5 | 973 | 0.21 | 43% | 43% |
| +0.5 s | +30 s | 2461 | 0.200 | 2.6 | 0.305 | 5.2 | 973 | 0.18 | 45% | 47% |
| +1 s | +1 s | 2461 | 0.039 | 2.0 | 0.166 | 10.9 | 1068 | 0.07 | 28% | 38% |
| +1 s | +3 s | 2461 | 0.020 | 0.6 | 0.249 | 10.1 | 1068 | 0.12 | 35% | 43% |
| +1 s | +10 s | 2461 | 0.012 | 0.2 | 0.233 | 6.0 | 1068 | 0.10 | 39% | 46% |
| +1 s | +30 s | 2461 | 0.079 | 1.1 | 0.224 | 3.8 | 1068 | 0.09 | 44% | 48% |
| +2 s | +1 s | 2461 | 0.005 | 0.3 | 0.067 | 4.4 | 1249 | 0.00 | 16% | 39% |
| +2 s | +3 s | 2461 | -0.020 | -0.7 | 0.100 | 4.1 | 1249 | 0.00 | 27% | 46% |
| +2 s | +10 s | 2461 | -0.019 | -0.5 | 0.083 | 2.2 | 1249 | 0.00 | 35% | 49% |
| +2 s | +30 s | 2461 | 0.012 | 0.2 | 0.055 | 0.9 | 1249 | -0.14 | 39% | 52% |

### Anchors as predictors of the outcome (markets with a tight pre-open book)

| anchor | period | n | accuracy % | Brier | log loss | ECE c |
|---|---:|---:|---:|---:|---:|---:|
| model | train | 1864 | 55.5 | 0.2469 | 0.6874 | 6.6 |
| model | validation | 597 | 57.5 | 0.2397 | 0.6721 | 5.7 |
| preopen_fair | train | 1864 | 55.2 | 0.2434 | 0.6798 | 5.0 |
| preopen_fair | validation | 597 | 56.6 | 0.2403 | 0.6731 | 7.0 |
| blend_75_25 | train | 1864 | 55.4 | 0.2455 | 0.6842 | 5.8 |
| blend_75_25 | validation | 597 | 57.5 | 0.2394 | 0.6713 | 4.9 |
| blend_50_50 | train | 1864 | 55.5 | 0.2445 | 0.6819 | 5.1 |
| blend_50_50 | validation | 597 | 57.5 | 0.2394 | 0.6712 | 4.9 |
| blend_25_75 | train | 1864 | 55.4 | 0.2438 | 0.6805 | 5.1 |
| blend_25_75 | validation | 597 | 57.3 | 0.2397 | 0.6718 | 5.2 |
| adaptive(TRAIN-fit) | train | 1864 | 58.5 | 0.2408 | 0.6745 | 2.7 |
| adaptive(TRAIN-fit) | validation | 597 | 54.8 | 0.2472 | 0.6871 | 9.5 |

adaptive blend coefficients (TRAIN): {'intercept': -0.207, 'logit_model': -0.048, 'logit_preopen': 1.026}

- **The shock is almost entirely the missing book.** At T + 0 the UP ask sits a median 4.5c above the pre-open fair (p90 +35c), the UP bid a median 4c below it. That is a wide spread around an unchanged centre, not a displaced price. By +2–3 s the ask is within 0.5–1.5c of the pre-open fair at the median.
- **"Reversion" exists only from the empty book.** Regressing the later change in the mid on the gap to the pre-open fair: from t = +0.25 s the mid moves back toward the fair (β ≈ 0.5, z 15–21), because the 30c-wide mid is not a price and re-forms. From t = +1 s the pre-open anchor explains nothing further (β 0.00–0.05, z ≤ 2). The model's gap still predicts the mid's next move from +1 s (β 0.17–0.25, z 6–11): the tight book converges to the model, not to the pre-open fair.
- **Continuation vs reversal** for deviations ≥ 3c at +1 s: the median fraction reverted over the next 3 s is 0.12, 35 % revert at least half, 43 % continue. Not a mean-reverting dislocation.
- **As a predictor of the outcome, the pre-open fair is about as good as the model** (Brier 0.2434 vs 0.2469 on TRAIN, 0.2403 vs 0.2397 on VALIDATION). Fixed blends match; the adaptive blend fitted on TRAIN (weights: model −0.05, pre-open 1.03) is *worse* on VALIDATION (Brier 0.2472). The two carry the same information.
- **Executable dislocations against the pre-open fair are not there to buy.** With the pre-open fair as the anchor, the executable ask is below fair − costs almost never; the "first pass, edge ≥ 0c" version found 28 fills on TRAIN+VALIDATION and every one lost (0 % win, −7.3c/share). Those are the cases where the *fair* was stale, not the ask.

## 5. Execution model, taker and maker

**Taker** (unchanged, one model for every strategy): decision on the as-of book → order arrives one-way latency later (measured draws) → matched against the archived book 500 ms later (the validated venue proxy) → FOK at the arrival VWAP within the limit or nothing; FAK takes what is inside the limit; exits are FOK sells at the decision bid − 2c, retried every 2 s; both taker fees.

**Maker, calibrated on the tick recording.** For a resting BUY posted at time t (it reaches the venue 230 ms later, the median decision-to-arrival) at price p for R seconds:
- it would *cross* if p ≥ the arrival ask: that is a taker order and is counted separately;
- queue ahead = the displayed size at p on arrival (if p is the best bid), 0 if p improves the bid, the 2-tick depth if p is one or two ticks below the bid, unfillable-by-trade deeper than that;
- **cross-through fill**: the token's ask falls to ≤ p while resting (the market comes down through us);
- **traded-through fill**: taker SELL prints on the token at ≤ p, plus taker BUY prints on the other token at ≥ 1 − p (the same trade seen from the complementary book), accumulate past the queue ahead plus our size.

| level | rest ms | orders | would cross on arrival | filled (of the rest) | of which traded through (queue) | 0.5 s markout c | 3 s markout c |
|---|---:|---:|---:|---:|---:|---:|---:|
| bid | 250 | 1188 | 0% | 5.6% | 9% | -3.25 | -2.67 |
| bid | 500 | 1188 | 0% | 9.8% | 10% | -3.19 | -2.52 |
| bid | 1000 | 1188 | 0% | 17.8% | 11% | -3.15 | -2.70 |
| bid | 2000 | 1188 | 0% | 25.5% | 15% | -2.95 | -2.43 |
| bid | 3000 | 1188 | 0% | 28.6% | 16% | -2.89 | -2.26 |
| bid+1 | 250 | 1188 | 28% | 8.4% | 18% | -2.51 | -2.42 |
| bid+1 | 500 | 1188 | 28% | 12.8% | 20% | -2.51 | -2.55 |
| bid+1 | 1000 | 1188 | 28% | 21.5% | 20% | -2.61 | -2.62 |
| bid+1 | 2000 | 1188 | 28% | 26.9% | 19% | -2.48 | -2.10 |
| bid+1 | 3000 | 1188 | 28% | 29.8% | 18% | -2.43 | -1.98 |
| bid+2 | 250 | 1188 | 34% | 9.0% | 11% | -2.67 | -1.41 |
| bid+2 | 500 | 1188 | 34% | 13.0% | 16% | -3.13 | -2.44 |
| bid+2 | 1000 | 1188 | 34% | 21.8% | 18% | -3.35 | -2.84 |
| bid+2 | 2000 | 1188 | 34% | 28.9% | 16% | -3.09 | -2.32 |
| bid+2 | 3000 | 1188 | 34% | 32.6% | 15% | -2.96 | -2.26 |
| fair-10 | 250 | 1080 | 1% | 0.9% | 10% | -1.40 | -0.10 |
| fair-10 | 500 | 1080 | 1% | 1.9% | 10% | -3.50 | -2.30 |
| fair-10 | 1000 | 1080 | 1% | 5.4% | 14% | -2.54 | -1.88 |
| fair-10 | 2000 | 1080 | 1% | 8.6% | 12% | -2.59 | -1.99 |
| fair-10 | 3000 | 1080 | 1% | 9.4% | 11% | -2.60 | -2.11 |
| fair-2 | 250 | 1080 | 12% | 4.5% | 14% | -6.19 | -4.63 |
| fair-2 | 500 | 1080 | 12% | 8.4% | 11% | -5.75 | -4.06 |
| fair-2 | 1000 | 1080 | 12% | 17.6% | 10% | -5.37 | -4.13 |
| fair-2 | 2000 | 1080 | 12% | 30.5% | 7% | -4.81 | -3.69 |
| fair-2 | 3000 | 1080 | 12% | 33.7% | 7% | -4.69 | -3.64 |
| fair-5 | 250 | 1080 | 6% | 2.5% | 8% | -4.72 | -4.08 |
| fair-5 | 500 | 1080 | 6% | 5.1% | 8% | -4.56 | -3.48 |
| fair-5 | 1000 | 1080 | 6% | 11.5% | 9% | -4.69 | -3.90 |
| fair-5 | 2000 | 1080 | 6% | 19.8% | 8% | -4.61 | -3.78 |
| fair-5 | 3000 | 1080 | 6% | 22.9% | 7% | -4.56 | -3.59 |

- **Resting orders fill when the market moves against them.** At every level and resting time, 85–95 % of the fills are cross-throughs; queue-based fills (someone hitting our bid) are 5–15 %.
- **The post-fill markout is negative everywhere**, −1.4 to −4.6c after 3 s, before any exit fee. A resting bid 10c under the pre-open fair fills 9 % of the time in 3 s and is still −2c three seconds later.
- The archive replay uses the same two mechanisms: cross-through exactly (the ask ≤ our price on the proxied book) and a traded-through hazard of 1.5 %/s while our price is the best bid (the recording's 53 traded-through fills over 1,188 × 3 s). Maker entries pay no taker fee (the schedule is taker-only). Exits remain taker sells. Sensitivities: hazard × 5, and "maker, then taker if the book is tight".

Queue position cannot be reconstructed for the archive, and only approximately for the recording (displayed size at our level at posting). The results are therefore conservative on the fill *rate* (a real order might fill from hidden or later-arriving volume) but not on the fill *quality*: the extra fills would be traded-throughs, whose markouts in the recording are as negative as the cross-throughs.


## 6. Strategies A–H on development data (TRAIN + VALIDATION, 09-22 15:30 to 09-25 00:35 UTC, 611 epochs)

One execution model, $5 orders, c/share net of both fees, decisions from the T − 3 s prediction unless stated. The 2c gate is the previous study's pre-registered net edge (expected exit bid − ask VWAP − 0.5c adverse − both fees, with the TRAIN convergence table of the anchor used).

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD | maker fills |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A original immediate taker (T+0, no filter, 3 s) | 2045 | 72.36 | 9.584 | 0.6556 | 0.558 | $-1228.27 | $455.69 | $-1101.99 | $-1683.96 | $-0.823 | -12.89 | -50.24 | $1684.44 | 0 |
| A' immediate taker + 2c edge gate | 23 | 40.35 | 39.13 | 0.6135 | 0.6243 | $+1.29 | $6.05 | $-1.64 | $-4.77 | $-0.207 | -2.067 | -1.742 | $5.72 | 0 |
| B delayed taker, decide at +0.5 s, edge >= 2c | 30 | 58.82 | 43.33 | 0.5987 | 0.6157 | $+3.94 | $8.10 | $-3.55 | $-4.16 | $-0.139 | -1.489 | -1.19 | $5.98 | 0 |
| B delayed taker, +1 s | 18 | 45 | 44.44 | 0.58 | 0.5673 | $-2.29 | $4.99 | $0.55 | $-7.29 | $-0.405 | -4.387 | -1.547 | $7.92 | 0 |
| B delayed taker, +2 s | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| B delayed taker, +3 s | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| B delayed taker, first pass in 0-3 s (last study's P3) | 71 | 44.65 | 33.8 | 0.5591 | 0.5621 | $+0.51 | $21.14 | $-6.36 | $-20.63 | $-0.291 | -2.93 | -2.875 | $21.51 | 0 |
| C resting maker at the bid, post at open, rest 1 s | 58 | 25.22 | 37.93 | 0.5574 | 0.5445 | $-6.82 | $8.46 | $-10.33 | $-15.28 | $-0.263 | -2.94 | -2.939 | $15.28 | 58 |
| C resting maker at the bid, rest 3 s | 89 | 38.7 | 32.58 | 0.5503 | 0.5323 | $-14.79 | $13.21 | $-15.04 | $-28.00 | $-0.315 | -3.463 | -4.104 | $28.00 | 89 |
| C resting maker 5c under pre-open fair, rest 2 s | 22 | 9.565 | 18.18 | 0.5068 | 0.479 | $-5.33 | $3.34 | $-11.27 | $-8.67 | $-0.394 | -4.479 | -3.382 | $9.44 | 22 |
| C maker at the bid 1 s, then taker if the book is tight | 118 | 51.3 | 32.2 | 0.5687 | 0.5605 | $-9.04 | $26.05 | $-8.90 | $-35.09 | $-0.297 | -3.285 | -5.365 | $35.49 | 58 |
| D pre-open fair anchor, taker, first pass 0-3 s, edge >= 0c | 28 | 49.12 | 0 | 0.5256 | 0.4859 | $-10.63 | $8.91 | $-5.15 | $-19.54 | $-0.698 | -7.324 | -6.586 | $19.54 | 0 |
| D pre-open fair anchor, +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| E model vs executable price, both sides by EV, +1 s, edge >= 2c | 18 | 45 | 44.44 | 0.58 | 0.5673 | $-2.29 | $4.99 | $0.55 | $-7.29 | $-0.405 | -4.387 | -1.547 | $7.92 | 0 |
| F 50/50 blend, +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| F adaptive blend (TRAIN-fit), +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| G + liquidity-recovery score >= TRAIN median, +1 s, edge >= 0c | 54 | 55.67 | 29.63 | 0.5622 | 0.5632 | $-1.08 | $16.00 | $-4.64 | $-17.08 | $-0.316 | -3.148 | -3.566 | $17.08 | 0 |
| G score >= TRAIN p75, first pass 0-3 s, edge >= 0c | 129 | 53.53 | 27.13 | 0.536 | 0.5285 | $-11.03 | $40.36 | $-8.24 | $-51.39 | $-0.398 | -4.048 | -5.534 | $51.39 | 0 |
| H settlement-only control: model side at +1 s, ask <= 0.60, hold to settlement | 837 | 67.23 | 49.1 | 0.5275 | 0.491 | $-254.77 | $132.75 | $-158.44 | $-387.52 | $-0.463 | -5.373 | -2.607 | $447.01 | 0 |
| H settlement-only control, 2c edge gate | 145 | 60.67 | 46.9 | 0.4908 | 0.469 | $-26.80 | $24.94 | $-23.81 | $-51.74 | $-0.357 | -3.91 | -0.8019 | $90.97 | 0 |
| random side, +1 s, 3 s (baseline) | 2215 | 72.08 | 10.38 | 0.5493 | 0.483 | $-1102.62 | $644.71 | $-764.86 | $-1747.32 | $-0.789 | -9.858 | -41.13 | $1747.32 | 0 |

Reading it:
- **A (immediate taker)** reproduces the previous study: −12.9c/share unfiltered; with the 2c gate it trades 23 times and loses 2.1c.
- **B (delayed taker).** Waiting changes the *kind* of loss, not its sign. At +0.5 s the gate finds 30 fills at −1.5c; at +1 s, 18 fills at −4.4c; from +2 s the gate never fires, because by then the ask contains the prediction. Without the gate (direction only, spread ≤ 3c) every delay from 0 to 5 s loses 4.4–5.1c/share on 350–1,560 trades: the two fees (~3.3c) plus the spread, with no drift after the fill.
- **C (resting maker).** Every level and every resting time loses 3–5.5c/share on 22–710 fills, with 100 % of archive fills being cross-throughs. Posting later (+1 s) or making traded-through fills five times as likely (hazard × 5) does not change the sign; "maker, then taker if the book tightens" adds taker losses to maker losses.
- **D (pre-open anchor).** 28 fills, 0 wins. The pre-open fair is never *above* the executable ask by more than the costs except when the fair is stale.
- **E (both sides by EV).** Chooses exactly the trades the model side chooses: when the model favours UP, the DOWN ask is never cheap enough for the other side to have the higher EV.
- **F (blends).** No fills at a 1c gate; at lower gates they behave like D.
- **G (recovery score).** A gate at the TRAIN median or p75 does not turn the taker positive (−3.1 to −4.0c). §10 shows why: the score predicts the *spread*, and the spread is a cost either way.
- **H (settlement control).** The model side bought at +1 s and held to the bell loses 5.4c/share on 837 trades (t −2.6): the market's price at +1 s is better calibrated than the model.
- **Random side** at +1 s loses 9.9c; the model's 58 % accuracy is worth about 5c/share over random in the unfiltered case, and that is spent entirely on the spread and fees.

### Delay × threshold (the core of the hypothesis)

| delay | edge >= 0c | >= 1c | >= 2c | >= 3c | >= 5c | >= 10c |
|---|---:|---:|---:|---:|---:|---:|
| +0 s | -2.77 (54) | -2.15 (35) | -2.07 (23) | -2.37 (15) | -1.96 (9) | -5.06 (3) |
| +0.05 s | -3.34 (58) | -2.17 (37) | -2.00 (24) | -2.55 (17) | -1.96 (9) | -5.06 (3) |
| +0.1 s | -3.22 (64) | -1.95 (39) | -1.56 (28) | -1.69 (19) | -2.19 (9) | -8.80 (2) |
| +0.25 s | -4.89 (71) | -3.85 (48) | -2.52 (35) | -2.19 (21) | -0.28 (11) | -0.24 (1) |
| +0.5 s | -2.89 (67) | -2.06 (53) | -1.49 (30) | -1.36 (18) | +0.43 (7) | +nan (0) |
| +0.75 s | -3.33 (82) | -2.09 (56) | -3.15 (32) | -2.72 (20) | -2.57 (9) | +nan (0) |
| +1 s | -3.89 (56) | -4.26 (35) | -4.39 (18) | -6.49 (10) | +3.30 (3) | +nan (0) |
| +1.5 s | -3.95 (77) | -3.58 (44) | -2.21 (24) | -2.99 (13) | -3.01 (5) | +nan (0) |
| +2 s | -3.55 (8) | +3.81 (1) | +nan (0) | +nan (0) | +nan (0) | +nan (0) |
| +3 s | +nan (0) | +nan (0) | +nan (0) | +nan (0) | +nan (0) | +nan (0) |
| +5 s | +nan (0) | +nan (0) | +nan (0) | +nan (0) | +nan (0) | +nan (0) |

Same with no edge gate, direction only, spread <= 3c:

| delay | 1 s | 3 s | 10 s | 30 s | settlement |
|---|---:|---:|---:|---:|---:|
| +0 s | -4.75 (348) | -4.54 (348) | -4.73 (348) | -3.35 (348) | -5.70 (348) |
| +0.05 s | -4.91 (351) | -4.56 (351) | -4.77 (351) | -3.72 (351) | -6.86 (351) |
| +0.1 s | -4.87 (342) | -4.55 (342) | -4.61 (342) | -3.67 (342) | -7.06 (342) |
| +0.25 s | -4.64 (349) | -4.38 (349) | -4.47 (349) | -4.18 (349) | -5.31 (349) |
| +0.5 s | -4.71 (486) | -4.44 (486) | -4.40 (486) | -4.17 (486) | -3.03 (486) |
| +0.75 s | -4.81 (553) | -4.62 (553) | -4.61 (553) | -3.92 (553) | -3.11 (553) |
| +1 s | -4.62 (574) | -4.47 (574) | -4.10 (574) | -3.94 (574) | -4.03 (574) |
| +1.5 s | -4.65 (751) | -4.34 (751) | -4.67 (751) | -4.27 (751) | -5.74 (751) |
| +2 s | -4.77 (1157) | -4.78 (1157) | -4.93 (1157) | -4.72 (1157) | -5.47 (1157) |
| +3 s | -4.95 (1484) | -5.09 (1484) | -4.92 (1484) | -4.38 (1484) | -3.00 (1484) |
| +5 s | -5.07 (1560) | -4.78 (1560) | -4.64 (1560) | -4.94 (1560) | -4.39 (1560) |

No cell with more than 7 trades is positive. The three positive cells (0.4c, 3.3c, 3.8c) have 1–7 trades.

### Maker grid

| level | rest 0.25 s | 0.5 s | 1 s | 2 s | 3 s |
|---|---:|---:|---:|---:|---:|
| bid | -5.01 (167/2826) | -4.56 (195/2826) | -3.60 (263/2826) | -4.08 (389/2826) | -4.15 (447/2826) |
| bid+1 | -4.42 (236/2826) | -4.16 (277/2826) | -4.24 (359/2826) | -4.20 (520/2826) | -4.30 (596/2826) |
| bid+2 | -4.46 (273/2826) | -4.29 (321/2826) | -4.30 (421/2826) | -4.23 (599/2826) | -4.22 (674/2826) |
| fair-2 | -5.46 (213/2826) | -5.39 (248/2826) | -5.07 (363/2826) | -5.13 (617/2826) | -5.14 (710/2826) |
| fair-5 | -4.35 (71/2826) | -4.22 (102/2826) | -4.49 (158/2826) | -4.99 (287/2826) | -5.18 (394/2826) |
| fair-10 | -3.09 (22/2826) | -4.13 (28/2826) | -4.48 (46/2826) | -4.81 (94/2826) | -5.42 (132/2826) |

Same, hazard x5 (traded-through fills five times as likely) and posting 1 s after the open:

| level | x5 hazard 0.5 s | 1 s | 3 s | post +1 s: 0.5 s | 1 s | 3 s |
|---|---:|---:|---:|---:|---:|---:|
| bid | -0.95 (220) | -0.51 (294) | -1.85 (495) | -4.24 (366) | -4.40 (485) | -4.51 (765) |
| bid+1 | -4.01 (288) | -4.03 (373) | -4.14 (614) | -4.19 (500) | -4.27 (642) | -4.32 (941) |
| fair-5 | -3.67 (109) | -4.08 (167) | -4.89 (409) | -4.84 (200) | -5.20 (284) | -5.24 (509) |

## 7. Markouts after every fill

### taker +0 s (no gate): 2045 fills

| subset | fills | +0.05s | +0.1s | +0.25s | +0.5s | +1s | +2s | +3s | +5s | +10s | +30s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 2045 | -29.32 | -28.07 | -23.45 | -20.50 | -19.56 | -11.86 | -10.14 | -9.75 | -9.26 | -8.95 |
| UP | 1069 | -28.23 | -27.13 | -23.16 | -20.54 | -19.72 | -11.76 | -10.10 | -9.54 | -9.11 | -7.88 |
| DOWN | 976 | -30.52 | -29.10 | -23.76 | -20.45 | -19.38 | -11.96 | -10.18 | -9.98 | -9.43 | -10.11 |
| BNB | 490 | -37.85 | -35.41 | -26.54 | -20.49 | -18.93 | -13.38 | -11.28 | -10.63 | -10.12 | -10.21 |
| BTC | 286 | -0.59 | -0.59 | -0.63 | -1.14 | -1.04 | -1.09 | -1.14 | -1.16 | -1.12 | +0.30 |
| DOGE | 379 | -32.48 | -29.88 | -21.80 | -17.20 | -15.96 | -10.85 | -9.85 | -9.86 | -9.01 | -9.10 |
| ETH | 268 | -6.02 | -5.41 | -3.85 | -3.19 | -3.03 | -2.22 | -1.22 | -0.71 | -0.45 | -0.20 |
| HYPE | 306 | -67.36 | -67.35 | -67.31 | -67.27 | -66.11 | -34.42 | -29.37 | -28.26 | -27.47 | -27.28 |
| SOL | 88 | -4.52 | -4.84 | -5.34 | -5.80 | -5.16 | -3.02 | -2.43 | -3.04 | -2.54 | -1.51 |
| XRP | 228 | -27.67 | -26.64 | -19.32 | -13.53 | -12.63 | -8.25 | -7.10 | -6.82 | -6.58 | -6.09 |
| spread <= 2c | 291 | -0.61 | -0.62 | -0.87 | -1.42 | -1.37 | -1.19 | -1.34 | -1.55 | -1.40 | -0.42 |
| spread > 2c | 1754 | -34.08 | -32.62 | -27.19 | -23.66 | -22.57 | -13.63 | -11.60 | -11.11 | -10.57 | -10.36 |
| depth2 < 50 | 718 | -23.96 | -22.82 | -18.76 | -16.37 | -15.51 | -8.86 | -7.48 | -7.06 | -6.50 | -5.81 |
| depth2 >= 50 | 1327 | -32.22 | -30.91 | -25.99 | -22.73 | -21.75 | -13.48 | -11.58 | -11.21 | -10.76 | -10.64 |
| confidence < 0.2 | 1397 | -27.28 | -26.31 | -21.93 | -19.27 | -18.53 | -11.98 | -10.63 | -10.25 | -9.61 | -8.98 |
| confidence >= 0.2 | 648 | -33.72 | -31.87 | -26.71 | -23.15 | -21.78 | -11.61 | -9.09 | -8.68 | -8.52 | -8.88 |

### taker +1 s (no gate, spread<=3c): 574 fills

| subset | fills | +0.05s | +0.1s | +0.25s | +0.5s | +1s | +2s | +3s | +5s | +10s | +30s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 574 | -0.81 | -0.90 | -1.09 | -1.28 | -1.31 | -1.04 | -1.00 | -1.02 | -0.80 | -0.50 |
| UP | 303 | -0.75 | -0.85 | -1.02 | -1.28 | -1.38 | -1.21 | -1.19 | -1.25 | -1.10 | +0.08 |
| DOWN | 271 | -0.88 | -0.95 | -1.16 | -1.27 | -1.24 | -0.85 | -0.79 | -0.77 | -0.47 | -1.15 |
| BNB | 0 | — | — | — | — | — | — | — | — | — | — |
| BTC | 308 | -0.52 | -0.58 | -0.80 | -1.07 | -1.23 | -1.12 | -0.92 | -0.94 | -0.64 | -0.04 |
| DOGE | 2 | — | — | — | — | — | — | — | — | — | — |
| ETH | 215 | -0.94 | -1.02 | -1.18 | -1.30 | -1.32 | -0.84 | -0.66 | -0.77 | -0.73 | -0.82 |
| HYPE | 0 | — | — | — | — | — | — | — | — | — | — |
| SOL | 37 | -2.09 | -2.06 | -2.23 | -1.85 | -1.85 | -1.25 | -2.55 | -1.82 | -1.09 | +0.29 |
| XRP | 12 | -1.76 | -2.84 | -2.92 | -4.34 | -1.51 | -1.59 | -4.09 | -5.01 | -5.51 | -9.01 |
| spread <= 2c | 486 | -0.71 | -0.80 | -0.97 | -1.23 | -1.27 | -1.08 | -1.06 | -1.11 | -0.88 | -0.72 |
| spread > 2c | 88 | -1.38 | -1.42 | -1.74 | -1.56 | -1.55 | -0.85 | -0.70 | -0.53 | -0.33 | +0.72 |
| depth2 < 50 | 152 | -0.88 | -0.94 | -1.24 | -1.36 | -1.20 | -0.67 | -0.69 | -0.75 | -0.73 | -0.69 |
| depth2 >= 50 | 422 | -0.79 | -0.88 | -1.03 | -1.25 | -1.35 | -1.18 | -1.11 | -1.12 | -0.83 | -0.43 |
| confidence < 0.2 | 393 | -0.79 | -0.88 | -1.02 | -1.27 | -1.41 | -1.19 | -1.25 | -1.29 | -0.91 | -0.40 |
| confidence >= 0.2 | 181 | -0.85 | -0.94 | -1.23 | -1.28 | -1.11 | -0.72 | -0.46 | -0.44 | -0.56 | -0.72 |

### taker +1 s edge>=2c: 18 fills

| subset | fills | +0.05s | +0.1s | +0.25s | +0.5s | +1s | +2s | +3s | +5s | +10s | +30s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 18 | -1.44 | -1.28 | -1.11 | -1.33 | -0.89 | +0.11 | +0.39 | +0.50 | -0.94 | +1.61 |
| UP | 11 | -1.09 | -1.00 | -0.55 | -1.27 | -0.36 | +0.36 | +0.09 | -0.55 | -3.18 | +0.91 |
| DOWN | 7 | -2.00 | -1.71 | -2.00 | -1.43 | -1.71 | -0.29 | +0.86 | +2.14 | +2.57 | +2.71 |
| BNB | 0 | — | — | — | — | — | — | — | — | — | — |
| BTC | 10 | -0.40 | -0.40 | -0.90 | -1.20 | -0.80 | +1.20 | +1.70 | +1.90 | +3.30 | +5.00 |
| DOGE | 0 | — | — | — | — | — | — | — | — | — | — |
| ETH | 7 | -1.14 | -0.71 | -1.00 | -1.29 | -0.71 | -1.00 | -0.57 | -0.57 | -2.43 | +1.57 |
| HYPE | 0 | — | — | — | — | — | — | — | — | — | — |
| SOL | 1 | — | — | — | — | — | — | — | — | — | — |
| XRP | 0 | — | — | — | — | — | — | — | — | — | — |
| spread <= 2c | 10 | -0.30 | -0.30 | -0.80 | -1.00 | -0.60 | +1.00 | +1.20 | +1.20 | +2.50 | +7.70 |
| spread > 2c | 8 | -2.88 | -2.50 | -1.50 | -1.75 | -1.25 | -1.00 | -0.63 | -0.38 | -5.25 | -6.00 |
| depth2 < 50 | 3 | — | — | — | — | — | — | — | — | — | — |
| depth2 >= 50 | 15 | -1.73 | -1.60 | -1.27 | -1.33 | -1.47 | +0.40 | +0.87 | +1.20 | -0.00 | +1.40 |
| confidence < 0.2 | 2 | — | — | — | — | — | — | — | — | — | — |
| confidence >= 0.2 | 16 | -1.63 | -1.44 | -1.13 | -1.38 | -0.88 | -0.00 | +0.50 | +0.50 | -0.94 | +1.19 |

### maker at bid, rest 1 s: 263 fills

| subset | fills | +0.05s | +0.1s | +0.25s | +0.5s | +1s | +2s | +3s | +5s | +10s | +30s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 263 | -0.82 | -0.83 | -1.32 | -3.68 | -2.54 | -1.89 | -1.90 | -2.02 | -2.79 | -2.25 |
| UP | 147 | -0.64 | -0.65 | -1.03 | -3.68 | -2.49 | -2.36 | -2.21 | -2.51 | -4.05 | -3.67 |
| DOWN | 116 | -1.04 | -1.04 | -1.69 | -3.69 | -2.61 | -1.30 | -1.52 | -1.40 | -1.20 | -0.45 |
| BNB | 1 | — | — | — | — | — | — | — | — | — | — |
| BTC | 162 | -0.14 | -0.18 | -0.52 | -2.60 | -2.43 | -2.21 | -2.35 | -2.66 | -3.07 | -2.38 |
| DOGE | 1 | — | — | — | — | — | — | — | — | — | — |
| ETH | 32 | -1.03 | -1.12 | -1.34 | -3.84 | -3.69 | -4.44 | -4.28 | -4.03 | -5.84 | -8.22 |
| HYPE | 6 | -2.67 | -2.67 | -2.67 | -4.50 | +24.67 | +31.33 | +31.67 | +33.67 | +33.17 | +27.17 |
| SOL | 58 | -2.05 | -1.98 | -3.16 | -6.43 | -4.95 | -3.26 | -2.97 | -3.14 | -4.21 | -2.28 |
| XRP | 3 | — | — | — | — | — | — | — | — | — | — |
| spread <= 2c | 168 | -0.49 | -0.51 | -1.08 | -3.36 | -2.99 | -2.69 | -2.84 | -3.12 | -3.66 | -3.06 |
| spread > 2c | 95 | -1.40 | -1.39 | -1.76 | -4.26 | -1.75 | -0.48 | -0.25 | -0.07 | -1.25 | -0.81 |
| depth2 < 50 | 41 | -2.73 | -2.63 | -3.63 | -6.29 | -4.29 | -1.68 | -1.15 | -0.07 | -1.37 | +0.88 |
| depth2 >= 50 | 222 | -0.46 | -0.49 | -0.90 | -3.20 | -2.22 | -1.93 | -2.05 | -2.38 | -3.05 | -2.82 |
| confidence < 0.2 | 170 | -0.88 | -0.88 | -1.50 | -3.82 | -2.93 | -2.32 | -2.47 | -2.66 | -3.53 | -2.58 |
| confidence >= 0.2 | 93 | -0.70 | -0.72 | -1.00 | -3.44 | -1.84 | -1.11 | -0.87 | -0.84 | -1.44 | -1.65 |

### maker at bid, rest 3 s: 447 fills

| subset | fills | +0.05s | +0.1s | +0.25s | +0.5s | +1s | +2s | +3s | +5s | +10s | +30s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 447 | -0.31 | -0.32 | -0.83 | -3.49 | -3.00 | -2.58 | -2.62 | -2.56 | -3.10 | -2.54 |
| UP | 243 | -0.20 | -0.22 | -0.69 | -3.52 | -3.03 | -2.93 | -2.81 | -3.11 | -4.19 | -3.17 |
| DOWN | 204 | -0.44 | -0.45 | -1.00 | -3.45 | -2.97 | -2.18 | -2.40 | -1.90 | -1.79 | -1.79 |
| BNB | 9 | -0.11 | -0.11 | -1.78 | -9.00 | -10.44 | -10.67 | -10.00 | -7.67 | -8.67 | -5.67 |
| BTC | 224 | +0.13 | +0.10 | -0.25 | -2.54 | -2.73 | -2.65 | -2.81 | -3.04 | -3.41 | -2.23 |
| DOGE | 4 | — | — | — | — | — | — | — | — | — | — |
| ETH | 58 | -0.02 | -0.07 | -0.38 | -3.19 | -3.02 | -3.16 | -3.43 | -2.98 | -3.86 | -5.72 |
| HYPE | 7 | -3.57 | -3.57 | -3.57 | -5.14 | +19.57 | +24.86 | +24.86 | +26.57 | +26.29 | +20.86 |
| SOL | 136 | -0.90 | -0.89 | -1.66 | -4.54 | -3.94 | -3.01 | -2.98 | -2.88 | -3.38 | -2.54 |
| XRP | 9 | -0.78 | -1.00 | -2.11 | -5.67 | -4.67 | -3.78 | -3.00 | -1.11 | -4.22 | -1.00 |
| spread <= 2c | 241 | -0.17 | -0.18 | -0.70 | -3.22 | -3.24 | -3.20 | -3.35 | -3.54 | -4.06 | -3.08 |
| spread > 2c | 206 | -0.47 | -0.49 | -0.99 | -3.80 | -2.72 | -1.86 | -1.77 | -1.41 | -1.97 | -1.91 |
| depth2 < 50 | 96 | -0.79 | -0.80 | -1.58 | -4.37 | -3.43 | -2.20 | -1.67 | -0.86 | -1.67 | -0.45 |
| depth2 >= 50 | 351 | -0.18 | -0.19 | -0.62 | -3.25 | -2.89 | -2.69 | -2.88 | -3.02 | -3.49 | -3.11 |
| confidence < 0.2 | 301 | -0.32 | -0.33 | -0.89 | -3.59 | -3.33 | -2.99 | -3.21 | -3.18 | -3.74 | -3.16 |
| confidence >= 0.2 | 146 | -0.28 | -0.32 | -0.71 | -3.28 | -2.32 | -1.75 | -1.40 | -1.28 | -1.77 | -1.26 |

### maker fair-5, rest 2 s: 287 fills

| subset | fills | +0.05s | +0.1s | +0.25s | +0.5s | +1s | +2s | +3s | +5s | +10s | +30s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 287 | -4.14 | -4.14 | -4.00 | -5.44 | -4.67 | -3.82 | -3.52 | -3.27 | -3.64 | -2.53 |
| UP | 159 | -3.60 | -3.58 | -3.68 | -5.60 | -5.16 | -4.18 | -3.89 | -3.92 | -4.63 | -2.70 |
| DOWN | 128 | -4.80 | -4.82 | -4.41 | -5.23 | -4.06 | -3.38 | -3.05 | -2.46 | -2.42 | -2.32 |
| BNB | 24 | -8.75 | -8.75 | -8.67 | -8.83 | -9.04 | -7.50 | -7.25 | -7.46 | -7.00 | -6.79 |
| BTC | 54 | +2.83 | +2.76 | +0.93 | -2.56 | -3.00 | -3.13 | -3.74 | -3.94 | -3.93 | -2.67 |
| DOGE | 54 | -9.48 | -9.43 | -8.57 | -6.63 | -6.91 | -5.22 | -5.46 | -5.20 | -3.91 | -3.09 |
| ETH | 59 | -0.41 | -0.36 | -1.05 | -3.61 | -3.02 | -2.36 | -1.27 | -1.02 | -2.53 | -2.69 |
| HYPE | 0 | — | — | — | — | — | — | — | — | — | — |
| SOL | 47 | +1.34 | +1.21 | -0.70 | -5.26 | -4.43 | -3.68 | -3.77 | -3.49 | -4.36 | -2.79 |
| XRP | 49 | -13.41 | -13.33 | -8.84 | -8.00 | -4.14 | -3.12 | -1.78 | -0.86 | -2.06 | +0.76 |
| spread <= 2c | 66 | +2.65 | +2.59 | +0.71 | -3.74 | -3.64 | -3.91 | -4.15 | -4.35 | -4.20 | -3.03 |
| spread > 2c | 221 | -6.16 | -6.14 | -5.41 | -5.94 | -4.98 | -3.79 | -3.33 | -2.95 | -3.48 | -2.38 |
| depth2 < 50 | 122 | -4.98 | -4.92 | -5.20 | -5.66 | -4.88 | -3.36 | -3.28 | -2.96 | -3.05 | -1.96 |
| depth2 >= 50 | 165 | -3.51 | -3.56 | -3.12 | -5.27 | -4.52 | -4.16 | -3.70 | -3.50 | -4.08 | -2.96 |
| confidence < 0.2 | 206 | -4.14 | -4.12 | -4.05 | -5.46 | -4.86 | -4.31 | -3.75 | -3.16 | -3.83 | -2.26 |
| confidence >= 0.2 | 81 | -4.14 | -4.17 | -3.88 | -5.38 | -4.19 | -2.57 | -2.93 | -3.56 | -3.19 | -3.22 |

- **Taker at T + 0:** −29c after 50 ms, −10c after 3 s: the fill is inside a 30c-wide book that re-forms around a centre far from the ask paid. BTC is the exception (−0.6c: it pays a 1c spread and nothing else).
- **Taker at +1 s on a tight book:** −0.8c at 50 ms, −1.3c at 1 s, −1.0c at 3 s, −0.5c at 30 s. That is the half-spread paid, with **no favourable drift at any horizon**. UP and DOWN identical; BTC and ETH identical; high-confidence signals no better than low.
- **Maker fills:** −0.8c at 50 ms (the fill itself is at the bid), then **−3.7c at 0.5 s** and −1.9 to −2.6c after: the bid goes on falling after a cross-through fill. Deeper resting levels (fair − 5c) are worse, not better (−4 to −5c). HYPE's 6–7 maker fills at +25c are stub-quote artefacts (a 0.11 bid filled by a 0.89 → 0.11 collapse of the empty book).
- The edge-gated taker's 18 fills go from −1.4c to +0.4c at 3 s and +1.6c at 30 s before the exit fee; net of it they are −1.5 to −4c. That is the least-bad row in the study and it is still below zero.

`results/open_micro_markouts.csv` has every fill with its ten markouts, side, asset, spread, depth and confidence.


## 8. Both sides, and the pair

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| model side | 56 | 56 | 28.57 | 0.5645 | 0.558 | $-4.12 | $16.50 | $-2.20 | $-20.62 | $-0.368 | -3.888 | -3.415 | $20.62 |
| best EV of the two sides | 56 | 56 | 28.57 | 0.5645 | 0.558 | $-4.12 | $16.50 | $-2.20 | $-20.62 | $-0.368 | -3.888 | -3.415 | $20.62 |
| UP only | 34 | 54.84 | 23.53 | 0.5641 | 0.5467 | $-5.40 | $10.01 | $-0.79 | $-15.41 | $-0.453 | -5.022 | -3.09 | $15.41 |
| DOWN only | 22 | 57.89 | 36.36 | 0.565 | 0.5755 | $+1.28 | $6.49 | $-1.41 | $-5.21 | $-0.237 | -2.136 | -1.635 | $5.81 |

UP ask + DOWN ask (the cost of buying both sides; a pair bought under 1.00 pays 1.00 at settlement, less two fees ~3.5c):

| offset | n | median | p10 | P(sum < 1.00) | P(sum < 0.97) |
|---|---:|---:|---:|---:|---:|
| -3 s | 3222 | 1.05 | 1.01 | 0 | 0 |
| +0 s | 3222 | 1.3 | 1.01 | 0 | 0 |
| +0.25 s | 3223 | 1.25 | 1.01 | 0 | 0 |
| +0.5 s | 3227 | 1.11 | 1.01 | 0 | 0 |
| +1 s | 3227 | 1.11 | 1.01 | 0 | 0 |
| +2 s | 3230 | 1.02 | 1.01 | 0 | 0 |
| +3 s | 3234 | 1.02 | 1.01 | 0 | 0 |
| +5 s | 3249 | 1.01 | 1.01 | 0 | 0 |

- The EV rule and the model rule pick the same trades: when the model puts 60–80 % on one side, the other side's ask is 0.20–0.40 and its EV is deeply negative.
- The pair (UP ask + DOWN ask) costs a median 1.30 at the open and 1.01–1.02 once the book is tight; it is never below 1.00. A bought pair would need a sum under ~0.965 to pay for two taker fees. There is no parity trade.
- UP-only and DOWN-only versions of the delayed taker both lose (−5.0c and −2.1c on 34 and 22 trades); the difference is within noise here and reverses in other cuts (§11).

## 9. Order size

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| $1 | 0 | — | — | — | — | — | — | — | — | — | — | — | — |
| $2 | 3 | 100 | 0 | 0.3767 | 0.3667 | $-0.15 | $0.49 | $-0.25 | $-0.64 | $-0.214 | -4.263 | -3.839 | $0.64 |
| $5 | 47 | 55.29 | 31.91 | 0.5636 | 0.5712 | $+1.73 | $13.92 | $-3.87 | $-12.19 | $-0.259 | -2.472 | -3.126 | $12.19 |
| $10 | 41 | 53.95 | 36.59 | 0.5691 | 0.5813 | $+6.56 | $24.02 | $-6.70 | $-17.47 | $-0.426 | -2.003 | -2.537 | $17.47 |

$1 and $2 orders fall under the venue's 5-share minimum at any ask above 0.20 / 0.40, so they can be placed only on cheap tokens (3 fills, all losing). $5 → $10 halves the fill rate and does not improve the per-share result; the 2-tick depth is thin during the recovery (median 40–60 shares), and beyond it the book is unrecorded.

## 10. The liquidity-recovery score and regimes

The score is an OLS fit on TRAIN of the 3 s bid markout of a model-side taker buy decided 1 s after the open on: spread, spread decay over 250 ms and 1 s, log depth, depth growth, book updates in the last second, distance from the pre-open fair, book age. Standardised coefficients (cents): spread −3.5, log depth −0.8, spread decay 1 s −0.4, everything else |≤ 0.3|.

| period | target | n | correlation with the score |
|---|---:|---:|---:|
| TRAIN (in-sample fit) | 3 s bid markout | 2422 | 0.755 |
| TRAIN (in-sample fit) | 10 s bid markout | 2422 | 0.620 |
| TRAIN (in-sample fit) | spread 3 s later | 2437 | -0.487 |
| VALIDATION | 3 s bid markout | 805 | 0.745 |
| VALIDATION | 10 s bid markout | 805 | 0.578 |
| VALIDATION | spread 3 s later | 807 | -0.595 |

By score quartile (TRAIN quantiles), VALIDATION, taker at +1 s, model side, 3 s exit, spread <= 3c, no edge gate:

| score bucket | trades | net c/share | t | win % |
|---|---:|---:|---:|---:|
| < p10 | 0 | +nan | +nan | — |
| p10-p25 | 0 | +nan | +nan | — |
| p25-p50 | 0 | +nan | +nan | — |
| p50-p75 | 71 | -7.10 | -7.9 | 8% |
| p75-p90 | 51 | -5.83 | -8.5 | 10% |
| >= p90 | 34 | -4.55 | -5.0 | 9% |

- **It validates as a predictor** (VALIDATION correlation 0.75 with the 3 s markout, −0.60 with the spread 3 s later), because it is, almost entirely, the spread at the decision.
- **It does not create an edge.** Its top decile on VALIDATION loses 4.6c/share (34 trades, t −5). A better-recovered book means paying a smaller spread to lose the fees; the drift after the fill is zero in every bucket.
- Used as a *veto* it is what "spread ≤ 2c" already does.

| regime | trades | net c/share | t | net P&L |
|---|---:|---:|---:|---:|
| UTC 00-06 | 131 | -3.73 | -7.4 | $-46.00 |
| UTC 06-12 | 142 | -3.61 | -7.0 | $-44.15 |
| UTC 12-18 | 150 | -5.39 | -9.0 | $-72.58 |
| UTC 18-24 | 151 | -5.00 | -11.1 | $-66.47 |
| |z| distance from strike: low | 190 | -4.89 | -12.0 | $-88.12 |
| mid | 194 | -4.82 | -9.9 | $-83.62 |
| high | 190 | -3.68 | -9.5 | $-57.46 |
| model confidence: low | 190 | -4.89 | -12.0 | $-88.12 |
| mid | 194 | -4.82 | -9.9 | $-83.62 |
| high | 190 | -3.68 | -9.5 | $-57.46 |
| pre-open fair: none | 3 | +0.87 | +0.2 | $+0.50 |
| pre-open |2p-1| < 0.2 | 420 | -4.84 | -15.8 | $-187.61 |
| pre-open |2p-1| >= 0.2 | 151 | -3.53 | -7.6 | $-42.09 |
| opening spread (T+0) <= 2c | 306 | -4.82 | -14.5 | $-133.39 |
| opening spread 3-10c | 145 | -4.17 | -7.6 | $-53.49 |
| opening spread > 10c | 122 | -4.00 | -7.8 | $-42.74 |
| opening depth2 < 50 | 163 | -4.36 | -10.0 | $-61.36 |
| opening depth2 >= 50 | 410 | -4.53 | -15.1 | $-168.25 |
| 5-min realised vol: low | 152 | -4.20 | -10.7 | $-57.69 |
| mid | 155 | -4.58 | -8.9 | $-64.07 |
| high | 152 | -4.44 | -9.0 | $-59.13 |
| |60 s momentum|: low | 152 | -4.48 | -10.6 | $-63.91 |
| mid | 155 | -5.30 | -9.7 | $-72.47 |
| high | 152 | -3.43 | -7.6 | $-44.50 |
| BNB | 0 | — | — | $+0.00 |
| BTC | 308 | -4.36 | -13.8 | $-120.63 |
| DOGE | 2 | -4.96 | -1.1 | $-0.62 |
| ETH | 215 | -4.27 | -10.4 | $-82.79 |
| HYPE | 0 | — | — | $+0.00 |
| SOL | 37 | -5.95 | -6.8 | $-19.25 |
| XRP | 12 | -6.17 | -5.1 | $-5.91 |
| UP | 303 | -4.76 | -12.7 | $-129.39 |
| DOWN | 271 | -4.13 | -10.1 | $-99.81 |

Every regime with more than a handful of trades loses 3.4–6.2c/share. Time of day, distance from the strike, model confidence (the same variable), pre-open confidence, opening spread and depth, realised volatility, momentum, asset and side: none turns the sign. The three markets without a pre-open fair are not a regime.


## 11. The sweep, the lock and the defenses

10,560 configurations × 12 exits on TRAIN and VALIDATION (`results/open_micro_sweep.csv`, 253,440 rows; ~1 h at nice 19):
- taker: anchor ∈ {model, pre-open, 50/50, adaptive} × delay ∈ {0, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5 s} × min edge ∈ {0, 1, 2, 3, 4, 5, 7, 10c} × side rule ∈ {model, best EV} × max spread ∈ {2, 3, 5c, any} × score gate ∈ {off, TRAIN median, p75};
- maker: anchor × level ∈ {bid, bid+1, bid+2, fair−2, fair−5, fair−10} × rest ∈ {0.25, 0.5, 1, 2, 3 s} × post at ∈ {0, 0.25, 0.5, 1 s} × gate ∈ {none, 0c, 2c} × then-taker ∈ {no, yes};
- exits: 0.5, 1, 2, 3, 5, 10, 30 s, settlement, +3c target, −3c stop, momentum reversal, edge gone.

| | count |
|---|---:|
| configuration × exit rows | 126,720 |
| with ≥ 30 TRAIN trades | 18,648 |
| positive on TRAIN | 200 (1.1 %) |
| passing the defenses (t ≥ 1.5, gross > 0, no asset > 60 % of \|P&L\|, side share 20–80 %, positive without the best market, VALIDATION ≥ 10 trades and > 0) | **0** |
| … and with a positive one-step neighbourhood | 0 |

`results/open_micro_locked.json` (locked 16:11 UTC, before TEST was read) therefore carries no survivor. It carries the four best-looking TRAIN rules, each with the defenses it failed, so the report can show what they do out of sample:

| rule | TRAIN | VALIDATION | failed |
|---|---:|---:|---|
| L1/L2 model, +0.25 s, 1c edge, best EV side, spread ≤ 3c, **settlement** | +5.1c (32) | +39.9c (4) | t < 1.5; one asset > 60 %; VAL < 10 trades |
| L3 adaptive anchor, maker fair−10 posted +0.5 s, rest 0.25 s then taker, **settlement** | +8.2c (32) | −19.0c (10) | t < 1.5; VAL ≤ 0 |
| L4 pre-open anchor, same maker rule, **settlement** | +7.1c (48) | −5.2c (13) | t < 1.5; VAL ≤ 0 |

Every rule that looked good on TRAIN is a settlement hold. No short-hold rule was among the top of anything.

## 12. TEST and FORWARD (read once)

### TEST (957 markets, 12.7 h)

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD | maker fills |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| L1 best TRAIN taker: D_twap delay 0.25s edge 0.01 ev spread<=0.03 score -9 maker '' rest 1 then_taker 0, settlement | 6 | 25 | 66.67 | 0.6367 | 0.6667 | $-0.21 | $0.73 | $-1.05 | $-0.95 | $-0.158 | 1.471 | 0.06991 | $9.79 | 0 |
| L2 best TRAIN taker: D_twap delay 0.25s edge 0.01 ev spread<=0.03 score 2 maker '' rest 1 then_taker 0, settlement | 6 | 25 | 66.67 | 0.6367 | 0.6667 | $-0.21 | $0.73 | $-1.05 | $-0.95 | $-0.158 | 1.471 | 0.06991 | $9.79 | 0 |
| L3 best TRAIN maker: ADAPT delay 0.5s edge -9 model spread<=9 score -9 maker 'fair-10' rest 0.25 then_taker 1, settlement | 7 | 1.124 | 28.57 | 0.4543 | 0.2857 | $-10.94 | $0.17 | $-4.24 | $-11.10 | $-1.586 | -17.1 | -0.9382 | $16.41 | 6 |
| L4 best TRAIN maker: PRE delay 0.5s edge -9 model spread<=9 score -9 maker 'fair-10' rest 0.25 then_taker 1, settlement | 11 | 1.763 | 18.18 | 0.4645 | 0.1818 | $-27.62 | $0.81 | $-7.54 | $-28.43 | $-2.584 | -29.06 | -2.16 | $33.73 | 6 |
| A original immediate taker (T+0, no filter, 3 s) | 568 | 70.47 | 8.099 | 0.6439 | 0.5563 | $-324.87 | $130.88 | $-290.95 | $-455.74 | $-0.802 | -11.95 | -23.3 | $455.88 | 0 |
| A' immediate taker + 2c edge gate | 3 | 18.75 | 66.67 | 0.561 | 0.6 | $+0.96 | $0.86 | $-0.70 | $+0.10 | $+0.033 | 0.6394 | 0.4625 | $0.19 | 0 |
| B delayed taker, decide at +0.5 s, edge >= 2c | 3 | 37.5 | 0 | 0.5967 | 0.6033 | $+0.18 | $0.84 | $-0.26 | $-0.67 | $-0.223 | -2.547 | -5.613 | $0.67 | 0 |
| B delayed taker, +1 s | 3 | 42.86 | 33.33 | 0.71 | 0.7267 | $+0.38 | $0.60 | $-0.16 | $-0.21 | $-0.072 | -0.967 | -0.6237 | $0.36 | 0 |
| B delayed taker, +2 s | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | 0 |
| B delayed taker, +3 s | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| B delayed taker, first pass in 0-3 s (last study's P3) | 15 | 30 | 33.33 | 0.5182 | 0.5344 | $+2.36 | $4.78 | $-4.04 | $-2.42 | $-0.161 | -1.624 | -1.807 | $2.90 | 0 |
| C resting maker at the bid, post at open, rest 1 s | 23 | 39.66 | 21.74 | 0.553 | 0.5352 | $-3.66 | $3.48 | $-3.00 | $-7.14 | $-0.310 | -3.478 | -3.731 | $7.44 | 23 |
| C resting maker at the bid, rest 3 s | 24 | 41.38 | 20.83 | 0.5596 | 0.5425 | $-3.66 | $3.58 | $-3.07 | $-7.24 | $-0.301 | -3.393 | -3.789 | $7.54 | 24 |
| C resting maker 5c under pre-open fair, rest 2 s | 6 | 10.34 | 33.33 | 0.53 | 0.5383 | $+0.62 | $0.87 | $-2.75 | $-0.25 | $-0.042 | -0.8468 | -0.2831 | $1.26 | 6 |
| C maker at the bid 1 s, then taker if the book is tight | 31 | 53.45 | 29.03 | 0.5835 | 0.5768 | $-2.05 | $5.29 | $-3.42 | $-7.34 | $-0.237 | -2.69 | -3.329 | $7.64 | 23 |
| D pre-open fair anchor, taker, first pass 0-3 s, edge >= 0c | 4 | 33.33 | 25 | 0.4425 | 0.433 | $-0.52 | $1.53 | $0.50 | $-2.05 | $-0.513 | -4.318 | -1.123 | $2.72 | 0 |
| D pre-open fair anchor, +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| E model vs executable price, both sides by EV, +1 s, edge >= 2c | 3 | 42.86 | 33.33 | 0.71 | 0.7267 | $+0.38 | $0.60 | $-0.16 | $-0.21 | $-0.072 | -0.967 | -0.6237 | $0.36 | 0 |
| F 50/50 blend, +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| F adaptive blend (TRAIN-fit), +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| G + liquidity-recovery score >= TRAIN median, +1 s, edge >= 0c | 16 | 64 | 25 | 0.5962 | 0.6125 | $+2.17 | $4.39 | $-1.45 | $-2.21 | $-0.138 | -1.496 | -1.232 | $2.75 | 0 |
| G score >= TRAIN p75, first pass 0-3 s, edge >= 0c | 23 | 37.1 | 17.39 | 0.5248 | 0.5181 | $-1.09 | $7.38 | $-3.95 | $-8.47 | $-0.368 | -3.984 | -3.109 | $9.40 | 0 |
| H settlement-only control: model side at +1 s, ask <= 0.60, hold to settlement | 220 | 69.84 | 52.73 | 0.5296 | 0.5273 | $+1.00 | $34.78 | $-40.23 | $-33.78 | $-0.154 | -1.964 | -0.4835 | $84.86 | 0 |
| H settlement-only control, 2c edge gate | 31 | 54.39 | 61.29 | 0.4937 | 0.6129 | $+34.36 | $5.24 | $-6.58 | $+29.13 | $+0.940 | 10.19 | 1.209 | $12.01 | 0 |
| random side, +1 s, 3 s (baseline) | 601 | 71.72 | 9.484 | 0.5419 | 0.4743 | $-334.79 | $177.53 | $-185.32 | $-512.31 | $-0.852 | -10.01 | -25.08 | $512.31 | 0 |

### FORWARD (152 markets, 2 h)

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD | maker fills |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| L1 best TRAIN taker: D_twap delay 0.25s edge 0.01 ev spread<=0.03 score -9 maker '' rest 1 then_taker 0, settlement | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | 0 |
| L2 best TRAIN taker: D_twap delay 0.25s edge 0.01 ev spread<=0.03 score 2 maker '' rest 1 then_taker 0, settlement | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | 0 |
| L3 best TRAIN maker: ADAPT delay 0.5s edge -9 model spread<=9 score -9 maker 'fair-10' rest 0.25 then_taker 1, settlement | 3 | 2.941 | 0 | 0.4467 | 0 | $-13.00 | $0.32 | $-1.99 | $-13.32 | $-4.440 | -45.81 | -6.613 | $13.32 | 1 |
| L4 best TRAIN maker: PRE delay 0.5s edge -9 model spread<=9 score -9 maker 'fair-10' rest 0.25 then_taker 1, settlement | 2 | 1.961 | 0 | 0.595 | 0 | $-8.70 | $0.25 | $-1.29 | $-8.95 | $-4.476 | -61.18 | -24.8 | $8.95 | 0 |
| A original immediate taker (T+0, no filter, 3 s) | 93 | 66.43 | 7.527 | 0.6379 | 0.5702 | $-42.60 | $20.97 | $-60.19 | $-63.57 | $-0.684 | -10.01 | -7.4 | $63.71 | 0 |
| A' immediate taker + 2c edge gate | 1 | 50 | 0 | 0.51 | 0.41 | $-0.98 | $0.34 | $0.20 | $-1.32 | $-1.317 | -13.44 | — | $1.32 | 0 |
| B delayed taker, decide at +0.5 s, edge >= 2c | 1 | 50 | 0 | 0.51 | 0.41 | $-0.98 | $0.34 | $-0.10 | $-1.32 | $-1.317 | -13.44 | — | $1.32 | 0 |
| B delayed taker, +1 s | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | 0 |
| B delayed taker, +2 s | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| B delayed taker, +3 s | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| B delayed taker, first pass in 0-3 s (last study's P3) | 1 | 16.67 | 0 | 0.51 | 0.41 | $-0.98 | $0.34 | $0.20 | $-1.32 | $-1.317 | -13.44 | — | $1.32 | 0 |
| C resting maker at the bid, post at open, rest 1 s | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | 0 |
| C resting maker at the bid, rest 3 s | 2 | 16.67 | 0 | 0.5 | 0.395 | $-2.04 | $0.32 | $0.19 | $-2.36 | $-1.180 | -12.17 | -8.235 | $2.36 | 2 |
| C resting maker 5c under pre-open fair, rest 2 s | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | 0 |
| C maker at the bid 1 s, then taker if the book is tight | 1 | 8.333 | 0 | 0.53 | 0.41 | $-1.18 | $0.34 | $0.20 | $-1.51 | $-1.513 | -15.44 | — | $1.51 | 0 |
| D pre-open fair anchor, taker, first pass 0-3 s, edge >= 0c | 1 | 50 | 0 | 0.57 | 0.47 | $-0.85 | $0.29 | $-0.25 | $-1.14 | $-1.140 | -13.46 | — | $1.14 | 0 |
| D pre-open fair anchor, +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| E model vs executable price, both sides by EV, +1 s, edge >= 2c | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | 0 |
| F 50/50 blend, +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| F adaptive blend (TRAIN-fit), +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — | — | 0 |
| G + liquidity-recovery score >= TRAIN median, +1 s, edge >= 0c | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — | 0 |
| G score >= TRAIN p75, first pass 0-3 s, edge >= 0c | 3 | 30 | 33.33 | 0.5633 | 0.6 | $+0.94 | $0.87 | $-0.94 | $+0.07 | $+0.024 | 0.4007 | 0.4882 | $0.08 | 0 |
| H settlement-only control: model side at +1 s, ask <= 0.60, hold to settlement | 20 | 62.5 | 50 | 0.5454 | 0.5 | $-9.02 | $3.10 | $-2.34 | $-12.12 | $-0.606 | -6.262 | -0.4616 | $27.95 | 0 |
| H settlement-only control, 2c edge gate | 2 | 33.33 | 0 | 0.53 | 0 | $-9.71 | $0.32 | $-0.29 | $-10.03 | $-5.013 | -54.73 | -13.74 | $10.03 | 0 |
| random side, +1 s, 3 s (baseline) | 90 | 64.75 | 16.67 | 0.5091 | 0.4718 | $-29.87 | $27.74 | $-34.90 | $-57.61 | $-0.640 | -6.982 | -8.538 | $58.06 | 0 |

### Statistics on TEST (item 18)

| strategy | trades | mean c | median c | sd c | 95 % CI (epoch bootstrap) | profit factor | max DD | worst market | best market | worst asset | best asset | UP share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| L1 best TRAIN taker: D_twap delay 0.25s edge 0.01 ev spread<=0.03 score -9 maker '' rest 1 then_taker 0, settlement | 6 | 1.471 | 29.02 | 42.49 | [-45.89, +31.32] | 0.9033 | $9.79 | BTC-1790301900 $-4.96 | BTC-1790310900 $+2.42 | BTC $-3.26 | ETH $+2.31 | 0.6667 |
| L2 best TRAIN taker: D_twap delay 0.25s edge 0.01 ev spread<=0.03 score 2 maker '' rest 1 then_taker 0, settlement | 6 | 1.471 | 29.02 | 42.49 | [-45.89, +31.32] | 0.9033 | $9.79 | BTC-1790301900 $-4.96 | BTC-1790310900 $+2.42 | BTC $-3.26 | ETH $+2.31 | 0.6667 |
| L3 best TRAIN maker: ADAPT delay 0.5s edge -9 model spread<=9 score -9 maker 'fair-10' rest 0.25 then_taker 1, settlement | 7 | -17.1 | -40 | 44.65 | [-46.29, +23.59] | 0.4865 | $16.41 | SOL-1790304900 $-4.80 | BTC-1790302800 $+5.30 | SOL $-9.43 | BTC $+5.30 | 0.4286 |
| L4 best TRAIN maker: PRE delay 0.5s edge -9 model spread<=9 score -9 maker 'fair-10' rest 0.25 then_taker 1, settlement | 11 | -29.06 | -45.72 | 38.99 | [-48.52, +2.36] | 0.2701 | $33.73 | SOL-1790304900 $-4.80 | BTC-1790302800 $+5.30 | ETH $-12.15 | BTC $-3.45 | 0.8182 |
| A original immediate taker (T+0, no filter, 3 s) | 568 | -11.95 | -9.696 | 10.71 | [-12.96, -10.98] | 0.02628 | $455.88 | HYPE-1790339400 $-2.93 | BTC-1790334000 $+0.76 | BNB $-141.38 | SOL $-17.62 | 0.5299 |
| A' immediate taker + 2c edge gate | 3 | 0.6394 | 0.9918 | 1.955 | [-1.91, +2.84] | 1.523 | $0.19 | BTC-1790313900 $-0.19 | SOL-1790307900 $+0.20 | BTC $-0.19 | SOL $+0.29 | 0.6667 |
| B delayed taker, decide at +0.5 s, edge >= 2c | 3 | -2.547 | -2.491 | 0.6418 | [-3.36, -1.79] | 0 | $0.67 | BTC-1790329200 $-0.28 | BTC-1790301900 $-0.12 | BTC $-0.67 | BTC $-0.67 | 1 |
| B delayed taker, +1 s | 3 | -0.967 | -1.478 | 2.193 | [-3.36, +1.94] | 0.4095 | $0.36 | BTC-1790329200 $-0.28 | BTC-1790310900 $+0.15 | BTC $-0.21 | BTC $-0.21 | 0.6667 |
| B delayed taker, +2 s | 0 | — | — | — | — | — | — | — | — | — | — | — |
| B delayed taker, +3 s | 0 | — | — | — | — | — | — | — | — | — | — | — |
| B delayed taker, first pass in 0-3 s (last study's P3) | 15 | -1.624 | -2.441 | 4.309 | [-3.34, -0.07] | 0.4234 | $2.90 | SOL-1790302800 $-0.75 | BTC-1790305200 $+0.78 | ETH $-1.44 | XRP $+0.03 | 0.6667 |
| C resting maker at the bid, post at open, rest 1 s | 23 | -3.478 | -3.744 | 4.437 | [-5.34, -1.69] | 0.1404 | $7.44 | BTC-1790320800 $-1.31 | SOL-1790334300 $+0.38 | BTC $-6.46 | SOL $-0.14 | 0.5652 |
| C resting maker at the bid, rest 3 s | 24 | -3.393 | -3.73 | 4.362 | [-5.28, -1.72] | 0.1387 | $7.54 | BTC-1790320800 $-1.31 | SOL-1790334300 $+0.38 | BTC $-6.56 | SOL $-0.14 | 0.5417 |
| C resting maker 5c under pre-open fair, rest 2 s | 6 | -0.8468 | -1.604 | 6.688 | [-6.08, +4.11] | 0.8518 | $1.26 | BTC-1790320800 $-1.04 | BTC-1790305200 $+0.79 | BTC $-0.68 | SOL $+0.43 | 0.5 |
| C maker at the bid 1 s, then taker if the book is tight | 31 | -2.69 | -3.36 | 4.495 | [-4.29, -1.14] | 0.2275 | $7.64 | BTC-1790320800 $-1.31 | BTC-1790330400 $+0.40 | BTC $-6.67 | SOL $-0.14 | 0.5484 |
| D pre-open fair anchor, taker, first pass 0-3 s, edge >= 0c | 4 | -4.318 | -6.193 | 6.661 | [-9.92, +3.17] | 0.2458 | $2.72 | SOL-1790304900 $-1.14 | BTC-1790302800 $+0.67 | SOL $-1.14 | BTC $+0.02 | 0.75 |
| D pre-open fair anchor, +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — |
| E model vs executable price, both sides by EV, +1 s, edge >= 2c | 3 | -0.967 | -1.478 | 2.193 | [-3.36, +1.94] | 0.4095 | $0.36 | BTC-1790329200 $-0.28 | BTC-1790310900 $+0.15 | BTC $-0.21 | BTC $-0.21 | 0.6667 |
| F 50/50 blend, +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — |
| F adaptive blend (TRAIN-fit), +1 s, edge >= 1c | 0 | — | — | — | — | — | — | — | — | — | — | — |
| G + liquidity-recovery score >= TRAIN median, +1 s, edge >= 0c | 16 | -1.496 | -2.9 | 4.431 | [-3.44, +1.09] | 0.4517 | $2.75 | ETH-1790332200 $-0.69 | BTC-1790302800 $+1.08 | BTC $-1.52 | ETH $-0.69 | 0.6875 |
| G score >= TRAIN p75, first pass 0-3 s, edge >= 0c | 23 | -3.984 | -4.457 | 6.16 | [-6.58, -1.70] | 0.1945 | $9.40 | ETH-1790338500 $-1.70 | BTC-1790302800 $+0.87 | ETH $-4.74 | XRP $-0.08 | 0.6957 |
| H settlement-only control: model side at +1 s, ask <= 0.60, hold to settlement | 220 | -1.964 | 38.32 | 50.04 | [-9.42, +6.00] | 0.9345 | $84.86 | BTC-1790332200 $-5.20 | BTC-1790328000 $+6.14 | ETH $-39.73 | XRP $+10.24 | 0.5455 |
| H settlement-only control, 2c edge gate | 31 | 10.19 | 43.27 | 47.58 | [-5.82, +26.62] | 1.495 | $12.01 | BTC-1790332200 $-5.20 | BTC-1790328000 $+6.14 | ETH $-0.30 | HYPE $+9.32 | 0.7097 |
| random side, +1 s, 3 s (baseline) | 601 | -10.01 | -8.145 | 10.16 | [-10.80, -9.25] | 0.02977 | $512.31 | HYPE-1790324700 $-4.29 | ETH-1790312100 $+1.04 | HYPE $-126.69 | XRP $-35.46 | 0.4709 |

- **The pre-registered families** (A–H at their pre-registered thresholds) are all negative or empty on TEST, and all negative or empty on FORWARD.
- **The locked rules:** L1 +1.5c on 6 TEST trades (t 0.07, CI [−46, +31]), 0 FORWARD trades; L3 −17c and L4 −29c on TEST, −46c and −61c on FORWARD.
- **The gated settlement control** (+10c on 31 TEST trades, CI [−6, +27]) is the same BTC/ETH-at-≤0.60 settlement family that was −5.7c on TRAIN; on FORWARD it is −55c on 2 trades. It is a coin flip on direction with a fee saved, not a strategy.

## 13. Chronological stability and robustness

### Walk-forward folds (TRAIN+VALIDATION), TEST and FORWARD

L1 best TRAIN taker: D_twap delay 0.25s edge 0.01 ev spread<=0.03 score -9 maker '' rest 1 then_taker 0, settlement:

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fold 1 | 10 | 47.62 | 60 | 0.594 | 0.6 | $-0.34 | $1.39 | $-0.92 | $-1.74 | $-0.174 | -1.042 | -0.06647 | $14.78 |
| fold 2 | 10 | 66.67 | 60 | 0.604 | 0.6 | $+0.37 | $1.37 | $-0.52 | $-1.00 | $-0.100 | -2.015 | -0.1245 | $10.29 |
| fold 3 | 5 | 50 | 60 | 0.576 | 0.6 | $+1.70 | $0.71 | $-0.95 | $+0.99 | $+0.198 | 0.7665 | 0.03002 | $9.74 |
| fold 4 | 7 | 35 | 85.71 | 0.5714 | 0.8571 | $+17.71 | $1.03 | $-0.50 | $+16.68 | $+2.382 | 26.89 | 1.94 | $4.98 |
| fold 5 | 4 | 44.44 | 100 | 0.585 | 1 | $+15.39 | $0.58 | $-0.00 | $+14.81 | $+3.701 | 39.89 | 6.203 | $0.00 |
| TEST | 6 | 25 | 66.67 | 0.6367 | 0.6667 | $-0.21 | $0.73 | $-1.05 | $-0.95 | $-0.158 | 1.471 | 0.06991 | $9.79 |
| FORWARD | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — |

L2 best TRAIN taker: D_twap delay 0.25s edge 0.01 ev spread<=0.03 score 2 maker '' rest 1 then_taker 0, settlement:

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fold 1 | 10 | 47.62 | 60 | 0.594 | 0.6 | $-0.34 | $1.39 | $-0.92 | $-1.74 | $-0.174 | -1.042 | -0.06647 | $14.78 |
| fold 2 | 10 | 66.67 | 60 | 0.604 | 0.6 | $+0.37 | $1.37 | $-0.52 | $-1.00 | $-0.100 | -2.015 | -0.1245 | $10.29 |
| fold 3 | 5 | 50 | 60 | 0.576 | 0.6 | $+1.70 | $0.71 | $-0.95 | $+0.99 | $+0.198 | 0.7665 | 0.03002 | $9.74 |
| fold 4 | 7 | 35 | 85.71 | 0.5714 | 0.8571 | $+17.71 | $1.03 | $-0.50 | $+16.68 | $+2.382 | 26.89 | 1.94 | $4.98 |
| fold 5 | 4 | 44.44 | 100 | 0.585 | 1 | $+15.39 | $0.58 | $-0.00 | $+14.81 | $+3.701 | 39.89 | 6.203 | $0.00 |
| TEST | 6 | 25 | 66.67 | 0.6367 | 0.6667 | $-0.21 | $0.73 | $-1.05 | $-0.95 | $-0.158 | 1.471 | 0.06991 | $9.79 |
| FORWARD | 0 | 0 | — | — | — | — | — | — | — | — | — | — | — |

L3 best TRAIN maker: ADAPT delay 0.5s edge -9 model spread<=9 score -9 maker 'fair-10' rest 0.25 then_taker 1, settlement:

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fold 1 | 11 | 2.371 | 45.45 | 0.4745 | 0.4545 | $-4.07 | $0.59 | $-6.88 | $-4.66 | $-0.423 | -2.562 | -0.1793 | $13.12 |
| fold 2 | 6 | 1.351 | 83.33 | 0.49 | 0.8333 | $+19.54 | $0.17 | $-3.94 | $+19.37 | $+3.229 | 34.05 | 1.872 | $4.66 |
| fold 3 | 9 | 2.013 | 55.56 | 0.4356 | 0.5556 | $+8.42 | $0.15 | $-8.08 | $+8.27 | $+0.919 | 11.81 | 0.5593 | $12.12 |
| fold 4 | 6 | 1.237 | 50 | 0.5267 | 0.5 | $+0.10 | $0.32 | $-3.50 | $-0.22 | $-0.037 | -3.246 | -0.1633 | $8.76 |
| fold 5 | 10 | 2.188 | 30 | 0.485 | 0.3 | $-21.28 | $0.57 | $-6.41 | $-21.85 | $-2.185 | -19.01 | -1.206 | $29.70 |
| TEST | 7 | 1.124 | 28.57 | 0.4543 | 0.2857 | $-10.94 | $0.17 | $-4.24 | $-11.10 | $-1.586 | -17.1 | -0.9382 | $16.41 |
| FORWARD | 3 | 2.941 | 0 | 0.4467 | 0 | $-13.00 | $0.32 | $-1.99 | $-13.32 | $-4.440 | -45.81 | -6.613 | $13.32 |

L4 best TRAIN maker: PRE delay 0.5s edge -9 model spread<=9 score -9 maker 'fair-10' rest 0.25 then_taker 1, settlement:

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fold 1 | 15 | 3.247 | 46.67 | 0.5013 | 0.4667 | $-7.39 | $0.82 | $-7.21 | $-8.21 | $-0.547 | -4.119 | -0.3347 | $18.20 |
| fold 2 | 10 | 2.237 | 70 | 0.519 | 0.7 | $+16.27 | $0.60 | $-3.42 | $+15.67 | $+1.567 | 17.41 | 1.144 | $9.47 |
| fold 3 | 7 | 1.566 | 71.43 | 0.4943 | 0.7143 | $+13.19 | $0.46 | $-4.94 | $+12.73 | $+1.819 | 21.25 | 1.036 | $5.15 |
| fold 4 | 18 | 3.659 | 55.56 | 0.4978 | 0.5556 | $+10.56 | $1.61 | $-9.88 | $+8.95 | $+0.497 | 4.818 | 0.418 | $26.40 |
| fold 5 | 11 | 2.381 | 45.45 | 0.5182 | 0.4545 | $-8.42 | $0.59 | $-7.60 | $-9.01 | $-0.819 | -6.991 | -0.3681 | $22.34 |
| TEST | 11 | 1.763 | 18.18 | 0.4645 | 0.1818 | $-27.62 | $0.81 | $-7.54 | $-28.43 | $-2.584 | -29.06 | -2.16 | $33.73 |
| FORWARD | 2 | 1.961 | 0 | 0.595 | 0 | $-8.70 | $0.25 | $-1.29 | $-8.95 | $-4.476 | -61.18 | -24.8 | $8.95 |

B delayed taker, first pass in 0-3 s (last study's P3):

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fold 1 | 26 | 59.09 | 50 | 0.5745 | 0.6051 | $+7.09 | $7.43 | $-4.36 | $-0.35 | $-0.013 | -0.154 | -0.1303 | $2.90 |
| fold 2 | 14 | 50 | 21.43 | 0.6101 | 0.5779 | $-3.56 | $3.75 | $-0.47 | $-7.31 | $-0.522 | -6.468 | -2.102 | $7.48 |
| fold 3 | 9 | 39.13 | 0 | 0.461 | 0.4262 | $-3.65 | $3.26 | $-0.02 | $-6.91 | $-0.768 | -6.849 | -5.68 | $6.91 |
| fold 4 | 14 | 35 | 42.86 | 0.5433 | 0.5595 | $+1.49 | $4.32 | $-1.14 | $-2.83 | $-0.202 | -1.612 | -0.794 | $4.20 |
| fold 5 | 8 | 33.33 | 25 | 0.5575 | 0.5525 | $-0.86 | $2.38 | $-0.37 | $-3.24 | $-0.405 | -3.659 | -2.156 | $3.61 |
| TEST | 15 | 30 | 33.33 | 0.5182 | 0.5344 | $+2.36 | $4.78 | $-4.04 | $-2.42 | $-0.161 | -1.624 | -1.807 | $2.90 |
| FORWARD | 1 | 16.67 | 0 | 0.51 | 0.41 | $-0.98 | $0.34 | $0.20 | $-1.32 | $-1.317 | -13.44 | — | $1.32 |

### Robustness (TEST)

**TEST, B delayed taker, first pass in 0-3 s (last study's P3)**

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base | 15 | 30 | 33.33 | 0.5182 | 0.5344 | $+2.36 | $4.78 | $-4.04 | $-2.42 | $-0.161 | -1.624 | -1.807 | $2.90 |
| latency x1.25 | 15 | 30 | 26.67 | 0.5122 | 0.5369 | $+3.49 | $4.82 | $-5.31 | $-1.32 | $-0.088 | -0.7969 | -1.117 | $2.23 |
| latency x1.5 | 15 | 30 | 33.33 | 0.5115 | 0.5389 | $+3.81 | $4.81 | $-5.14 | $-1.00 | $-0.067 | -0.5189 | -0.6827 | $2.27 |
| latency x2 | 16 | 32 | 31.25 | 0.54 | 0.5503 | $+0.66 | $4.84 | $-2.61 | $-4.18 | $-0.261 | -2.186 | -1.972 | $5.13 |
| random extra delay 0-300 ms per order | 15 | 30 | 26.67 | 0.5133 | 0.5373 | $+3.45 | $4.82 | $-4.99 | $-1.36 | $-0.091 | -0.8647 | -1.559 | $1.97 |
| random extra delay 0-1000 ms per order | 17 | 34 | 35.29 | 0.5253 | 0.5538 | $+4.48 | $5.30 | $-5.33 | $-0.82 | $-0.048 | -0.3995 | -0.493 | $1.85 |
| fill rate -25 % (random fills dropped) | 13 | 26 | 30.77 | 0.5364 | 0.549 | $+1.41 | $3.96 | $-3.69 | $-2.55 | $-0.196 | -1.968 | -2.177 | $3.03 |
| fill rate -50 % (random fills dropped) | 7 | 14 | 28.57 | 0.5929 | 0.6124 | $+1.51 | $1.97 | $-1.33 | $-0.46 | $-0.066 | -1.264 | -0.7951 | $0.94 |
| best 10 % of fills never happen | 13 | 26 | 23.08 | 0.5302 | 0.5336 | $+0.15 | $4.02 | $-3.48 | $-3.87 | $-0.298 | -2.884 | -3.024 | $3.87 |
| spread +1c (entry +0.5c, exit -0.5c) | 15 | 30 | 20 | 0.5232 | 0.5294 | $+0.90 | $4.78 | $-4.04 | $-3.88 | $-0.259 | -2.624 | -2.923 | $4.19 |
| slippage +1c on entry | 15 | 30 | 20 | 0.5282 | 0.5344 | $+0.90 | $4.78 | $-4.04 | $-3.88 | $-0.259 | -2.62 | -2.918 | $4.19 |
| exit 1c worse | 15 | 30 | 20 | 0.5182 | 0.5244 | $+0.90 | $4.78 | $-4.04 | $-3.88 | $-0.259 | -2.628 | -2.927 | $4.18 |
| everything above at once (x1.5, +1c/+1c, -25 % fills) | 13 | 26 | 23.08 | 0.5226 | 0.5249 | $-0.05 | $4.13 | $-4.81 | $-4.18 | $-0.321 | -3.016 | -3.015 | $4.64 |
| prediction noise (logit sd 0.25) | 47 | 43.12 | 12.77 | 0.5258 | 0.4973 | $-13.39 | $14.82 | $-6.97 | $-28.21 | $-0.600 | -6.178 | -8.018 | $28.21 |
| prediction noise (logit sd 0.5) | 137 | 53.73 | 10.95 | 0.5309 | 0.4934 | $-47.89 | $42.74 | $-18.68 | $-90.63 | $-0.662 | -7.082 | -12.53 | $90.77 |
| prediction noise (logit sd 1) | 264 | 60 | 10.23 | 0.5359 | 0.4804 | $-127.57 | $79.97 | $-66.35 | $-207.54 | $-0.786 | -8.856 | -19.38 | $207.54 |

**TEST, C resting maker at the bid, post at open, rest 1 s**

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base | 23 | 39.66 | 21.74 | 0.553 | 0.5352 | $-3.66 | $3.48 | $-3.00 | $-7.14 | $-0.310 | -3.478 | -3.731 | $7.44 |
| latency x1.25 | 21 | 36.21 | 28.57 | 0.5567 | 0.5448 | $-2.20 | $3.14 | $-3.33 | $-5.34 | $-0.254 | -2.876 | -2.44 | $5.82 |
| latency x1.5 | 22 | 37.93 | 27.27 | 0.5636 | 0.5573 | $-1.22 | $3.22 | $-4.45 | $-4.44 | $-0.202 | -2.301 | -1.707 | $5.59 |
| latency x2 | 18 | 31.03 | 33.33 | 0.57 | 0.5622 | $-1.55 | $2.60 | $-4.50 | $-4.14 | $-0.230 | -2.423 | -1.882 | $4.99 |
| random extra delay 0-300 ms per order | 23 | 39.66 | 26.09 | 0.5622 | 0.5504 | $-2.41 | $3.40 | $-4.32 | $-5.81 | $-0.253 | -2.85 | -2.658 | $6.30 |
| random extra delay 0-1000 ms per order | 20 | 34.48 | 30 | 0.568 | 0.5585 | $-1.79 | $2.91 | $-4.20 | $-4.69 | $-0.235 | -2.615 | -2.452 | $4.85 |
| fill rate -25 % (random fills dropped) | 19 | 32.76 | 15.79 | 0.5542 | 0.5337 | $-3.51 | $5.75 | $-2.15 | $-9.26 | $-0.487 | -5.435 | -5.182 | $9.40 |
| fill rate -50 % (random fills dropped) | 13 | 22.41 | 15.38 | 0.5446 | 0.5192 | $-3.01 | $4.01 | $-0.98 | $-7.02 | $-0.540 | -5.962 | -4.316 | $7.16 |
| best 10 % of fills never happen | 20 | 34.48 | 5 | 0.557 | 0.529 | $-5.01 | $6.03 | $-2.09 | $-11.04 | $-0.552 | -6.184 | -6.97 | $11.04 |
| spread +1c (entry +0.5c, exit -0.5c) | 23 | 39.66 | 8.696 | 0.558 | 0.5302 | $-5.70 | $6.96 | $-3.00 | $-12.66 | $-0.550 | -6.174 | -6.622 | $12.71 |
| slippage +1c on entry | 23 | 39.66 | 8.696 | 0.563 | 0.5352 | $-5.70 | $6.95 | $-3.00 | $-12.65 | $-0.550 | -6.168 | -6.611 | $12.70 |
| exit 1c worse | 23 | 39.66 | 8.696 | 0.553 | 0.5252 | $-5.70 | $6.97 | $-3.00 | $-12.67 | $-0.551 | -6.18 | -6.632 | $12.72 |
| everything above at once (x1.5, +1c/+1c, -25 % fills) | 18 | 31.03 | 5.556 | 0.5861 | 0.5439 | $-6.71 | $5.18 | $-2.55 | $-11.89 | $-0.661 | -7.551 | -6.795 | $11.89 |
| prediction noise (logit sd 0.25) | 42 | 37.84 | 11.9 | 0.5267 | 0.4888 | $-14.91 | $6.60 | $-7.35 | $-21.51 | $-0.512 | -5.464 | -6.563 | $21.51 |
| prediction noise (logit sd 0.5) | 58 | 27.36 | 22.41 | 0.5169 | 0.4904 | $-15.34 | $9.02 | $-14.34 | $-24.36 | $-0.420 | -4.299 | -4.503 | $24.66 |
| prediction noise (logit sd 1) | 82 | 25.79 | 25.61 | 0.4957 | 0.4682 | $-24.01 | $13.10 | $-25.83 | $-37.11 | $-0.453 | -4.383 | -6.829 | $37.42 |

**TEST, L1 best TRAIN taker: D_twap delay 0.25s edge 0.01 ev spread<=0.03 score -9 maker '' rest 1 then_taker 0, settlement**

| Strategy | Trades | Fill % | Win % | Avg Entry | Avg Exit | Gross P&L | Fees | Slippage | Net P&L | Net/Trade | c/share | t | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| base | 6 | 25 | 66.67 | 0.6367 | 0.6667 | $-0.21 | $0.73 | $-1.05 | $-0.95 | $-0.158 | 1.471 | 0.06991 | $9.79 |
| latency x1.25 | 8 | 33.33 | 75 | 0.6049 | 0.75 | $+9.02 | $1.07 | $-1.46 | $+7.95 | $+0.994 | 12.92 | 0.7863 | $9.90 |
| latency x1.5 | 6 | 25 | 83.33 | 0.6317 | 0.8333 | $+11.14 | $0.75 | $-1.05 | $+10.39 | $+1.732 | 18.58 | 0.9673 | $4.89 |
| latency x2 | 8 | 33.33 | 87.5 | 0.63 | 0.875 | $+17.16 | $1.01 | $-1.13 | $+16.15 | $+2.019 | 22.91 | 1.639 | $4.82 |
| random extra delay 0-300 ms per order | 7 | 29.17 | 71.43 | 0.6257 | 0.7143 | $+3.36 | $0.88 | $-1.44 | $+2.48 | $+0.355 | 7.294 | 0.4017 | $9.79 |
| random extra delay 0-1000 ms per order | 8 | 33.33 | 75 | 0.6075 | 0.75 | $+8.90 | $1.06 | $-1.34 | $+7.83 | $+0.979 | 12.66 | 0.7816 | $9.73 |
| fill rate -25 % (random fills dropped) | 5 | 20.83 | 60 | 0.62 | 0.6 | $-2.08 | $0.64 | $-0.86 | $-2.72 | $-0.544 | -3.553 | -0.1367 | $9.79 |
| fill rate -50 % (random fills dropped) | 4 | 16.67 | 50 | 0.6075 | 0.5 | $-4.54 | $0.52 | $-0.86 | $-5.06 | $-1.266 | -12.3 | -0.3588 | $9.79 |
| best 10 % of fills never happen | 5 | 20.83 | 60 | 0.634 | 0.6 | $-2.75 | $0.62 | $-0.77 | $-3.37 | $-0.673 | -4.917 | -0.2243 | $9.79 |
| spread +1c (entry +0.5c, exit -0.5c) | 6 | 25 | 66.67 | 0.6417 | 0.6667 | $-0.45 | $0.73 | $-1.05 | $-1.18 | $-0.197 | 0.9805 | 0.0466 | $9.89 |
| slippage +1c on entry | 6 | 25 | 66.67 | 0.6467 | 0.6667 | $-0.69 | $0.72 | $-1.05 | $-1.41 | $-0.235 | 0.4906 | 0.02332 | $9.98 |
| exit 1c worse | 6 | 25 | 66.67 | 0.6367 | 0.6667 | $-0.21 | $0.73 | $-1.05 | $-0.95 | $-0.158 | 1.471 | 0.06991 | $9.79 |
| everything above at once (x1.5, +1c/+1c, -25 % fills) | 5 | 20.83 | 80 | 0.648 | 0.8 | $+7.48 | $0.60 | $-0.97 | $+6.87 | $+1.375 | 13.66 | 0.5796 | $4.96 |
| prediction noise (logit sd 0.25) | 26 | 49.06 | 53.85 | 0.5437 | 0.5385 | $+1.35 | $4.06 | $-2.65 | $-2.71 | $-0.104 | -2.199 | -0.2057 | $20.47 |
| prediction noise (logit sd 0.5) | 57 | 61.29 | 57.89 | 0.5417 | 0.5789 | $+18.55 | $8.93 | $-6.29 | $+9.62 | $+0.169 | 2.056 | 0.3105 | $46.40 |
| prediction noise (logit sd 1) | 78 | 58.21 | 51.28 | 0.5064 | 0.5128 | $-7.00 | $13.12 | $-9.43 | $-20.12 | $-0.258 | -1.051 | -0.1928 | $73.98 |

- The delayed taker (0–3 s window, 2c gate) is negative in 5 of 5 walk-forward folds, on TEST and on FORWARD, and under every degradation.
- The maker is negative everywhere; removing its best 10 % of fills takes TEST from −3.5c to −6.2c.
- L1's TEST result flips from +1.5c to +23c or −12c under latency × 2 or a 50 % fill cut: six trades, no information.
- Adding noise to the prediction (logit sd 0.5) makes the taker rules *more* negative (−4.8 to −7.1c) because they trade more; it makes L1 *positive* (+2.1c on 57 trades) because the noise turns a 6-trade rule into a random settlement bet at ≤ 0.60, which in this TEST window happened to win. That is what a regime looks like.

## 14. Figures

`results/open_micro_figs/`:
1. `01_spread_vs_ms.png`: median spread by asset, −3 s to +5 s.
2. `02_depth_vs_ms.png`: median 2-tick depth by bucket after the open.
3. `03_opening_shock.png`: UP ask minus pre-open fair at +0, +1, +3 s.
4. `04_markout_curves.png`: post-fill executable markout, taker vs maker.
5. `05_maker_vs_taker.png`: net c/share with bootstrap intervals, eight entry rules.
6. `06_preopen_vs_executable.png`: pre-open fair vs the UP ask 1 s after the open on tight books.
7. `07_edge_vs_pnl.png`: expected net edge at the decision vs realised P&L, 1,467 fills.
8. `08_by_asset.png`, 9. `09_by_holding.png`, 10. `10_by_prediction_time.png`.

## 15. Files and reproduction

| file | what |
|---|---|
| `scripts/research_open_microstructure.py` | the whole study: `ticks`, `maker_calibration`, `snapshots`, `liquidity`, `shock`, `dev`, `markouts`, `sweep`, `lock`, `final`, `figures` |
| `results/open_micro_snapshots.csv` | the state machine: 4,533 markets × 20 offsets |
| `results/open_micro_markouts.csv` | every development fill of six entry rules with ten markouts |
| `results/open_micro_sweep.csv` | 253,440 sweep rows (TRAIN and VALIDATION) |
| `results/open_micro_locked.json` | the lock, the defense statistics, near misses |
| `results/open_micro_trades.csv`, `open_micro_market_results.csv`, `open_micro_walkforward.csv` | trade-level, period-level and fold-level results for every locked and pre-registered rule, all periods |
| `data/replay/open_micro/` | tick streams, tick liquidity curve, maker calibration (per order), archive liquidity curve, opening shock, development tables, `final_report.md` |
| `results/open_micro_figs/*.png` | the ten figures |

Run in order: `ticks`, `maker_calibration`, `snapshots`, `liquidity`, `shock`, `dev`, `markouts`, `sweep` (~1 h), `lock`, `final`, `figures`; always under `nice -n 19` (never renice a bot). The pipeline is deterministic (seeded latency draws, common random numbers). The previous study's `cache`/`build`/`grid` steps must have been run first (`data/replay/next_market/`).

**Limitations.** 224 ms archive resolution (the 50/100 ms rows repeat the same sample; event resolution comes only from the 90-minute recording); depth beyond 2 ticks unrecorded; queue position approximate; the venue proxy validated on spot-shock orders rather than on opens; 2.9 days of data. None of these limits changes the sign of a result that is negative in every family, every period and every degradation.
