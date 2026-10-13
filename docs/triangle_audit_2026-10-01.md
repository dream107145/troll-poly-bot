# Triangle BTC paper strategy: audit and improvement study (2026-10-01)

Scripts: `scripts/triangle_audit/paper_audit.py` (the paper runs), `bookflow_study.py` (Polymarket book flow vs
crypto flow, tick recordings), `chart_replay.py` + `chart_by_time.py` (six days of 200 ms charts). Paper only;
nothing here touches real money.

## 1. What was audited

Three data sets, used for what each can answer:

| data | span | answers |
|---|---|---|
| paper runs (`data/archive/triangle-btc-paper_*`, the live ledger, the pm2 log) | 02:05–15:41 UTC today | the bot as it actually ran: trigger 1 (flow, from 14:36), trigger 2 (high zone, from 15:19), the spot-lag era before them |
| tick recordings `data/ticks` 09-23 (1.50 h, train) and 09-29 (0.75 h, test) | 2.25 h | crypto flow vs Polymarket book flow at ms resolution, lead/lag, token move after a signal |
| `data/charts` BTC windows, 1,944 with an outcome, 6 days | 4 chronological splits 50/20/15/15 % | the high zone, a trigger-1 proxy (3 σ spot shock), the hedge models, the pair-cost gate |

Execution assumptions everywhere: FOK at the ask seen at the decision + 3 c (capped at 0.96), landing 800 ms later
(the paper venue's measured p50), filled only if the ask then is at or under the limit; taker fee 7 % × p(1−p) per
share; 5 shares; a resting buy fills only when the ask trades strictly below it (the venue model); settlement at
the window's outcome. 100 ms token returns are not measurable from 200 ms samples.

## 2. The paper runs, by trigger

| | spot-lag σ era 02:05–14:01 | TRIGGER 1 flow 14:36→ | TRIGGER 2 high zone 15:19→ |
|---|---|---|---|
| signals / fills (fill rate) | 264 / 260 (98 %) | 263 / 155 (59 %) | 13 / 12 (92 %) |
| avg entry, paid over the ask at the signal | 0.692, +0.050 | 0.490, +0.011 | 0.895, +0.001 |
| token ask after the fill 0.25 / 0.5 / 1 / 2 / 5 s | – | +0.001 / +0.001 / +0.002 / −0.001 / −0.010 | – |
| settled first legs, P&L, per fill, win rate | 216, −2.22, −0.01, 69 % | 92, −9.77, −0.11, 48 % | 12, +5.91, +0.49, 100 % |
| fees | 12.95 | 10.27 | 0.39 |
| hedges filled / placed, time to hedge p50 / p90 | 169/202 (84 %), 18 s / 115 s | 120/155 (77 %), 14 s / 65 s | 12/12, 22 s / 91 s |
| completed pairs: n, P&L, per pair | 139, +26.59, +0.19 | 64, +8.19, +0.13 | 12, +2.01, +0.17 |
| naked first legs: n, P&L, per leg | 77, −43.65, −0.57 | 28, −28.82, −1.03 | 0 |
| **net** | **−17.07** | **−20.62** (max DD −21.75) | **+2.01** |
| executable pair cost at entry (side ask + other ask) | – | 1.012; ≤ 0.95 in 0 % | – |

Trigger 1 by |score|: 0.70–0.75 n 109 fills −0.07/fill; 0.75–0.85 n 38 −0.40/fill; ≥ 0.85 n 4 (no settled).
By time left: 240–300 s net −15.5, 180–240 s −7.6, 120–180 s +3.8, 60–120 s +4.4 (small n).
Signal-to-move latency (tick replay, section 4): the token has moved +1.1 c at 250 ms, +2.7 c at 500 ms and +5.0 c
at 1 s after a flow signal; our order lands at ~800 ms, which is why 41 % of flow orders were killed at ask + 3 c
and the fills that remain were at +1 c.

## 3. Six-day chart replay: hedge models and the pair-cost gate (chronological splits)

Per fill, 5 shares, net of fees. "Leg 1 only" = no hedge. R = today's resting hedge at pair_cost − fill.
Eseq / Esim = the executable gate (side ask + other ask + both fees + 0.03 ≤ max_pair_cost), hedge as a FOK after
the fill (Eseq) or both legs at once (Esim).

| trigger / model | train | validation | test | forward |
|---|---|---|---|---|
| HIGH ZONE leg 1 only (fills) | +0.115 (7,909) | +0.082 (3,107) | +0.069 (2,520) | +0.035 (2,321) |
| HIGH ZONE R 0.95 | −0.036 | −0.034 | −0.040 | −0.070 |
| HIGH ZONE R 0.96 | −0.045 | −0.052 | −0.046 | −0.064 |
| HIGH ZONE Eseq/Esim gate 0.93–0.97 | 0 decisions pass | 0 | 0 | 0 |
| SPOT SHOCK proxy leg 1 only | +0.021 (5,317) | +0.016 | −0.020 | +0.054 |
| SPOT SHOCK R 0.95 / 0.96 | −0.26 / −0.26 | −0.23 / −0.23 | −0.22 / −0.23 | −0.18 / −0.18 |
| SPOT SHOCK gate 0.93–0.97 | 0 pass | 0 | 0 | 0 |

Why the resting hedge loses in every split: it is adversely selected. The other side's ask only falls to
pair_cost − fill when the bought side is winning, so the hedge fills on winners (capping the gain at ~+0.04/share
before fees) and stays naked on losers (the full loss). In the high-zone train split: 4,608 completed pairs
+991, 1,580 naked −1,215. The paper run shows the same shape (completed +8.19, naked −28.82).

Why the gate passes nothing: at the decision the two asks already sum to ~1.01–1.04 before fees, in the replay
and in all 100 real flow fills (0 of 100 pass at ≤ 0.97; at a 1.05 ceiling 16 pass, hedged at pair cost 1.007 for
+0.04 each). A pair below 1.00 exists only as a resting order that fills later, which is the adversely selected
case above. Tested values 0.93, 0.94, 0.95, 0.96, 0.97: identical (nothing passes).

High zone by time left (leg 1 only, per fill): 240–300 s +0.20/+0.07/+0.23/+0.02; 180–240 s +0.13/+0.15/+0.07/+0.09;
120–180 s +0.13/+0.11/+0.13/+0.09; 60–120 s +0.06/+0.03/−0.02/−0.02; **10–60 s +0.17/−0.17/−0.18/−0.22**
(train/val/test/forward). The rule's edge is in the middle of the window; the last minute loses out of sample.
By fill price: 0.9–0.96 is positive in every split (+0.06 to +0.11, win 95–96 %); 0.8–0.9 is −0.012 in forward.

## 4. Polymarket book flow vs crypto flow (tick recordings; the main hypothesis)

Book-flow signal: equal-weight robust-scaled sum over 100/250/500/1000 ms windows of ask velocity, ask
consumption at the touch, aggressive executed volume, cancellations beyond trades, new liquidity and the UP-vs-DOWN
depth imbalance; fires when |score| ≥ 0.70 (chosen on train) on two consecutive evaluations ≥ 50 ms apart.
A = crypto flow (the app's settings), B = book flow, C = A confirmed by B within ±300 ms.

| | A train | B train | C train | A test | B test | C test |
|---|---|---|---|---|---|---|
| signals / hour | 145 | 770 | 66 | 131 | 1,330 | 61 |
| token move after 0.25 / 0.5 / 1 / 2 s (c) | +1.1 / +2.7 / +5.0 / +6.0 | +1.0 / +1.5 / +1.9 / +2.0 | +1.6 / +4.1 / +6.7 / +7.4 | +1.2 / +2.8 / +6.7 / +7.2 | +0.9 / +1.3 / +1.6 / +1.8 | +1.7 / +4.2 / +7.5 / +7.3 |
| hit rate at 1 s | 68 % | 60 % | 90 % | 86 % | 56 % | 89 % |
| fill rate at ask + 3 c, +800 ms | 16 % | 64 % | 17 % | 21 % | 65 % | 15 % |
| first-leg P&L per fill (n) | +0.59 (24) | +0.03 (344) | +0.82 (13) | +0.37 (19) | −0.12 (534) | +0.44 (7) |
| executable pair cost | 1.034 | 1.040 | 1.032 | 1.036 | 1.040 | 1.039 |

Lead/lag against token moves (UP mid ±2 c within 2 s; 1,023 train, 664 test):

| | fired for (train / test) | median ms vs move start | before the start |
|---|---|---|---|
| A crypto flow | 24 % / 19 % | +65 / +106 | 49 % / 46 % |
| B book flow | 76 % / 89 % | −291 / −1,062 | 59 % / 73 % |
| ask pulled ≥ 2 c, no trade | 52 % / 55 % | +85 / −11 | 45 % / 52 % |
| when both A and B fired | 224 / 118 moves | B − A: −18 / −579 | B first 52 % / 67 % |

Answer to the hypothesis: Polymarket book flow does fire earlier and far more often than crypto flow, but it is
not usable on its own: it fires 770–1,330 times an hour, the token moves only +1.6–1.9 c in the next second with a
56–60 % hit rate, and its first legs are break-even (train) or losing (test). As a **confirmation** of the crypto
signal it is useful: C keeps ~45 % of A's signals, raises the 1 s hit rate from 68–86 % to ~90 % and the token
move from +5.0–6.7 c to +6.7–7.5 c, consistently on train and test. The binding constraint is not the signal but the
800 ms to land: the token has moved +2.7 c by 500 ms, so only 15–21 % of orders fill at ask + 3 c. Sample sizes for
P&L are small (7–24 fills per cell); the direction statistics rest on hundreds of signals.

The 16–21 % replay fill rate vs 59 % in today's paper run is a discrepancy: the recordings (09-23, 09-29) were
livelier and sampled the book at every update, the paper venue at 200 ms. Treat the replay fill rate as the
pessimistic bound.

## 5. Code delivered

* `TriangleConfig.hedge_mode` = `resting` (unchanged behaviour) | `executable` (the gate + a FOK hedge after the
  fill; a killed hedge leaves the leg naked, logged and counted, never re-placed as a resting order),
  `max_pair_cost`, `hedge_slippage`; dashboard controls "Hedge mode" and "Max pair cost". Tests in
  `tests/test_triangle_hedge.py`. No positive setting exists for it (section 3), so it ships off.
* Pair total: one source of truth, the ecosystem file (0.96); the dashboard override that disagreed was cleared.
* `scripts/triangle_audit/`: the three studies, re-runnable.

## 6. What the evidence supports (paper bot)

1. **Stop placing the resting hedge.** Negative in every chronological split for both triggers and in the paper
   run; it caps winners and keeps losers. Hold first legs to settlement unhedged.
2. **Do not enable the executable gate at ≤ 0.97.** It would stop all trading; above 1.00 it hedges at a certain loss.
3. **High zone only with ≥ 120 s left** (the last minute loses out of sample; 60–120 s is ~0). Expectancy is thin
   and decaying (+0.115 → +0.035 per fill train → forward): worth running, not worth leaning on.
4. **Trigger 1:** the fills that happen are good in replay (+0.37 to +0.59 per fill on 19–24 fills) but the paper
   run lost (−0.11/fill on 92). Book-flow confirmation (C) is the most promising improvement and is not yet in the
   bot; it needs a live implementation of the book-flow features and a paper run before it is trusted.
5. Everything above is paper. Nothing here supports real money.
