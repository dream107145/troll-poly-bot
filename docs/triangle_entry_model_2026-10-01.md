# Triangle BTC paper bot: the entry model, the hedge by EV, and a fresh run (2026-10-01, 17:00–18:00 UTC)

Scripts: `scripts/triangle_audit/entry_model.py` (new; results `results/triangle_entry_model.md`, lock
`data/replay/triangle_entry_locked.json`), `bookflow_study.py` (+ D mode and the 1-vs-2 exchange variant),
the 09-30 feed benchmark for per-source latency. Paper only; no live bot was touched. The previous audit
(`docs/triangle_audit_2026-10-01.md`) stands; this builds on it.

## 1. What changed and why

| change | why |
|---|---|
| **Entry model** (`TriangleConfig.entry_model`, `entry_min_ev`, `entry_max_price`, `entry_min_left_s`; `TriangleEngine.entry_features` / `entry_score`) | the exact buy point: every candidate (≥ 1 σ spot move, or the high-zone rule) is scored by a logistic P(win) fitted on TRAIN fills from 15 features (crypto 1 s / 3 s momentum, acceleration, trade-flow and book imbalance, OFI, token velocity over 1 s / 3 s, ask depth, spread, time left, price, fair-value gap, UP+DOWN pressure, high-zone flag); buy only when EV = P(win) − (ask + 1c) − fee ≥ 0.02 |
| **Hedge by EV** (`hedge_mode = "ev"`, `hedge_ev_margin`) | the second leg only when the certain pair beats the model: 1 − other ask − fee ≥ P(win) + margin. Replaces the automatic resting hedge for this run (negative in every split; the audit) |
| `ENTRY_SCORE` and `TRIANGLE_CONFIG` log lines; `pairs_skipped["ev"]`, `entry_scored`, `entry_refused_ev` in the snapshot | structured record of every scored candidate and the active configuration at start |
| trigger for this run: `signal_source = spot_lag`, `k_sigma = 1` (candidates), band 0.20–0.96 | the model was fitted on spot-move candidates; the flow signal has no archive to validate it on |
| `ecosystem.repricedmom.config.js` duplicate hedge keys removed (the later `resting` silently overrode the first) | one source of truth |
| dashboard: Hedge mode gains `ev`, "Hedge EV margin", "Entry min EV" (group Triangle entry model) | steerable without a restart; `entry_model` on/off is restart-only |
| `tests/test_triangle_model.py` (5) + `chart_replay.main` guarded | suite 1,124 pass |

Active parameters (printed by the bot at start, `TRIANGLE_CONFIG`): BTC only, candidates ≥ 1 σ 1 s spot move
(dedupe 1 s) + high zone (ask > 0.85 rising, every 5 s), entry model ON (min EV 0.02, max price 0.96, min
60 s left), 5 shares FOK at ask + 3c, hedge mode `ev` margin 0.05 (slippage 0.03), hold to settlement,
caps 5 USDC/order, 50/market, 100 open, 100 daily loss, balance 100, spot sources binance/bybit/coinbase,
book-age gate 60 ms (quote age), spot age ≤ 500 ms.

## 2. Chronological evaluation (data/charts, 1,956 BTC windows, 6 days; TRAIN 50 / VAL 20 / TEST 15 / FORWARD 15 %)

Execution everywhere: 5 shares FOK at ask + 3c (cap 0.96), landing 800 ms later, filled only if the ask then is
within the limit, taker fee 7 % p(1−p), settled at the outcome. Baseline = today's rules (3 σ spot shock + high zone).

| | per market, TEST | per market, FORWARD | per fill | win | fill rate | t (fwd) |
|---|---:|---:|---:|---:|---:|---:|
| baseline, both triggers | +0.63 | +0.39 | +0.04–0.06 | 75–77 % | 82–85 % | +1.3 |
| baseline, 3 σ spot shock alone | −0.32 | +0.04 | −0.07 / +0.01 | 53–55 % | 70–72 % | +0.1 |
| baseline, high zone alone | +1.10 | +0.44 | +0.15 / +0.07 | 92–94 % | 96–97 % | +2.0 |
| **entry model** (chosen on VALIDATION) | **+2.28** | **+2.36** | +0.10 / +0.15 | 76 % | 81–85 % | **+5.3** |

Train +0.65/market, validation +1.44 (where the threshold was chosen), test +2.28, forward +2.36: the gain
survives out of sample and grows, not shrinks. Max drawdown test −729 / forward −394 USDC on ~6,500 / 4,700
fills; worst single fill −4.6 USDC. By time left (TEST+FORWARD): 180–240 s best (+0.163/fill), 60–120 s
weakest (+0.074); by price: 0.70–0.85 (+0.156, 81 % win) and 0.85–0.96 (+0.126, 91 %) carry it, 0.45–0.55
is +0.159 on only 377 fills. Both triggers contribute (spot move +0.123/fill, high zone +0.116).

What the model actually learned (standardised coefficients): price +1.72 dominates — P(win) is mostly the
token's own price, i.e. the market is roughly right; everything else is a small correction: z3 +0.07, OBI
+0.08, high zone +0.13, token velocity over 3 s −0.09 (a token that already ran is worse), ask depth −0.05.
The EV gate is therefore "buy when the price is below what P(win | price, flow) implies by ≥ 2c + fee".
The 3-cent constant markup and the 800 ms landing are built into the EV.

**Second leg by EV**: at margin 0.00 it hedges ~0.5 % of fills and loses 10–24 USDC per split; at 0.05 it
hedges ~0.1 % and is ±1. The data has no profitable second leg: the executable pair costs 1.03–1.04 at the
decision (all four studies agree). The triangle is not a source of value; the first leg is.

## 3. Crypto feed latency (measured, ms after each venue's own stamp; the paper bot's live figures in brackets)

| source | p50 | p90 | p95 | p99 |
|---|---:|---:|---:|---:|
| Deribit / Kraken / Bitstamp | 33 / 38 / 49 | 172 / 60 / 66 | 273 / 70 / 70 | 342 / 95 / 78 |
| Coinbase [72] | 71 | 82 | 96 | 162 |
| Bybit [106] | 118 | 125 | 131 | 146 |
| Binance [161] | 152 | 238 | 271 | 406 |
| Polymarket book (venue stamp → us) [30] | 34 | 54 | 76 | 155 |

The flow signal already processes each exchange independently, dates every tick by its information time
and takes the first mover; no exchange waits for another. Requiring 2 confirming exchanges vs 1 (`min_confirm`): 219 vs 217 signals on train, identical fills and P&L on both files — the signal's other gates (first mover with followers, trade flow, acceleration) already imply agreement, so the second exchange costs no latency and adds no accuracy. Keep 2 (it is free insurance against a single stale feed).
Signal → order: presend 31 ms, venue round trip 224 ms (paper), so an order lands ~250–300 ms after the
decision in paper, ~800 ms measured end to end in the earlier paper runs.

## 4. Polymarket book flow (tick recordings, 2.25 h; train 09-23, test 09-29)

| | signals/h | token move 0.5 s / 1 s | hit 1 s | fill rate at ask+3c | leg-1 per fill (n) |
|---|---:|---:|---:|---:|---:|
| A crypto flow only | 145 / 131 | +2.7 / +5.0c, +2.8 / +6.7c | 68 % / 86 % | 16 % / 21 % | +0.59 (24) / +0.37 (19) |
| B book flow only | 770 / 1,330 | +1.5 / +1.9c, +1.3 / +1.6c | 60 % / 56 % | 64 % / 65 % | +0.03 (344) / **−0.12 (534)** |
| C crypto + book confirmation (±300 ms) | 66 / 61 | +4.1 / +6.7c, +4.2 / +7.5c | 90 % / 89 % | 17 % / 15 % | +0.82 (13) / +0.44 (7) |
| D book-first, crypto confirms ≤ 300 ms | 17 / 21 | +3.6 / +6.9c, +4.7 / +8.0c | 88 % / 94 % | 27 % / 19 % | +1.95 (5) / +1.91 (3) |

Does the book move before the token reprices? Yes: the book-flow signal fires before the move's start for
59 % / 73 % of token moves, median −291 / −1,062 ms, and before the crypto signal in 52 % / 67 % of moves
where both fire. But alone it is break-even to losing (56–60 % hit, 1.6–1.9c moves). As confirmation (C) or
as the first stage (D) it raises the hit rate to ~90 % and the move to +7–8c. D is the strongest per fill on
both files but rests on 5 and 3 settled fills: not evidence yet. None of these is in the live bot; the
entry model's `tok_vel1/3` and depth features are the 200 ms proxy of the same information.

## 5. Fresh run (archived to `data/archive/triangle-btc-paper_20261001_173651/`; started 17:36:52 UTC)

Confirmed at start: `ENTRY_MODEL loaded … 15 features; active min EV +0.020, max price 0.96, min left 60 s`,
`TRIANGLE_CONFIG …` printed, feeds binance/bybit/coinbase/polymarket connected, equity 100.00, no positions or
pairs restored. First 16 minutes (through the 17:45 window's settlement):

| | |
|---|---|
| candidates scored / refused by EV | 127 / 93 (73 %) |
| orders / fills / FOK kills | 14 / 3 / 6 (fill rate 21 %) |
| fills | UP 5 @ 0.77, UP 5 @ 0.72 (17:45 window), DOWN 5 @ 0.593 (17:50 window) |
| hedge by EV | 3 checks, 0 hedges (certain pair never ≥ P(win) + 0.05) |
| settled | 17:45 window DOWN: **−7.58** (both UP legs lost) |
| equity | 91.09 |
| rejections | WEAK_MOMENTUM 3,263 (below 1 σ), COOLDOWN 1,534, OUTSIDE_TIME_WINDOW 312, EDGE_TOO_SMALL 93 (the EV gate), ASK_ABOVE_MAX_ENTRY 59, TOKEN_ALREADY_MOVED 53 |
| feed latency / book age | spot p50 115 ms (coinbase 72, bybit 106, binance 161); book age p50 30 ms, p95 38 ms |

Two windows is not a result; it is the shape of the run: the gate is selective (3 in 4 candidates refused),
the fill rate is the replay's pessimistic bound (21 %, not the 80 % the 200 ms archive shows), and the first
settled window lost on two high-priced UP legs.

## 6. Answers to the brief's five questions

1. **Best entries**: a token priced 0.70–0.96 whose P(win | price, 3 s crypto momentum, book imbalance, high
   zone) exceeds the ask by ≥ 2c + fee, with 120–240 s left, when the token has NOT already run over the last
   3 s. Spot-move and high-zone candidates contribute equally per fill.
2. **Book flow before repricing**: yes, it leads (−0.3 to −1.1 s median) for 59–73 % of token moves, but it is
   worthless alone; it adds value only as confirmation of the crypto signal (C, D) — tiny samples for P&L.
3. **Does the triangle create value?** No. The executable pair costs 1.03–1.04 in every data set; the resting
   hedge is adversely selected (negative in all four splits for both triggers); the EV hedge fires on ~0.1 % of
   fills. All of the expected value is in the first leg held to settlement.
4. **What prevents $1/market**: (a) the 800 ms order landing vs a token that moves +2.7c in 500 ms — the real
   fill rate is 15–21 % against 80 % in the 200 ms archive, so the replay's +2.3/market shrinks to roughly a
   quarter of that at the measured fill rate, and the fills that happen are the adversely selected ones (the
   09-30 real-order study: −12c/share of edge lost at the fill); (b) the $50/market and $100 total caps bind at
   ~13 fills per window; (c) a 5-share leg has only ~+0.15 USDC of expected value, so one settled loss (−3.8)
   needs 25 wins.
5. **Robust vs overfit**: robust — "hold the first leg, do not hedge" (four studies, every split), the price
   dependence of P(win), the high-zone middle-of-window edge, refusing tokens that already ran. Likely
   overfit or unproven — the exact EV threshold (0.02 vs 0.00 differ by ±10 % on validation), the D mode
   (5 + 3 fills), any claim that the archive fill rate transfers to the venue.

Verdict: the entry model is the best robust rule the data supports, out of sample positive with t ≈ 5 on the
archive; **the $1/market benchmark is not demonstrated under realistic execution**, because the archive cannot
resolve the sub-second repricing that decides fills. The fresh paper run is the test of that gap.
