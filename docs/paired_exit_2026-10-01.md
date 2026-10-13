# Paired-Exit Momentum: quantitative redesign of the triangle paper bot (2026-10-01)

Scope: the user's 26-point brief ("buy one token and wait for the opposite order" → "paired-exit momentum").
Everything here is PAPER. Code: `strategy/paired_exit.py`, `strategy/paired_exit_engine.py`, additive hooks in
`live.py`, switches in `TriangleConfig` (`TPB_TRIANGLE_PX_*`). Research: `scripts/paired_exit/*`. Result files:
`results/paired_exit_*.md`. Tests: `tests/test_paired_exit.py` (21).

**Bottom line.** The large unpaired losses came from the resting opposite order, and removing it removes them.
On today's rule, an unpaired leg is a loser by construction: P(unpaired win) ≈ 0. The redesigned bot has a
first leg with its own exits, no forced pair, one open leg at a time and a 5-share size. That takes the worst
window from −50 to −4.25 USDC and the max drawdown from −676 to −14 (BTC, out of sample). Its profit is
**not** proven. Fixed-split out of sample it makes +0.07 USDC per window (t 1.8). The daily walk-forward gives
+0.023 per window (t 0.75). Only BTC is positive in every split, and early exits all cost expected value.
It runs on paper to collect fills; it is not a candidate for real money.

---

## 1. Existing strategy diagnosis

Today's rule (`hedge_mode resting`, `pair_cost 0.95` strict, entry model EV ≥ 0.02, 5-share FOK at ask + 3–4 ¢) buys the
predicted side. After the fill it rests a BUY of the other side at the highest tick with fill + pair < 0.95, and it holds
the first leg to settlement whether or not that pair order fills.

| evidence | paired legs | unpaired legs | net |
|---|---|---|---|
| the user's observation (6 windows) | 12, +2.81 (+0.23 each) | 3, −10.18 (−3.4 each) | −7.37 |
| the same paper run, 17 windows to 22:50 UTC | 43, +10.27 (+0.239 each) | 3, −10.18 (−3.39 each) | +0.08 |
| replay of the rule, BTC, 1,991 windows (calibrated fills) | 89 %, +0.237 | 11 %, −3.70 | −0.76 … −1.44 / window in every split |
| replay, all 7 assets, 103,598 fills | 87 %, +0.235 | 13 %, −3.63 | −0.27 / fill |

The observations in the brief hold. One unpaired loss erases about 15.5 completed pairs. The paper run's
3 of 46 unpaired legs (6.5 %) is a lucky draw against the replay's long-run 11–13 %.

## 2. Root cause of the losses

**The resting pair is adversely selected by construction.** If the first leg is going to win, the other token
is heading to 0, and its ask trades through any resting price ≥ 1 ¢ before the close. So **the pair fills on
practically every winner** and caps it at +0.235. It stays empty only when the other token rises, which means
the first leg is losing. In the replay, P(unpaired leg wins) = **0 %** on every asset (`results/paired_exit_tail.md`).

The rule needs P(unpaired loss) < 0.235 / (0.235 + 3.63) = **6.1 %** to break even. The replay gives 11–14 % in every
bucket of asset, seconds left, entry price, momentum strength, volatility tercile and entry-model EV:
EV −0.18 … −0.36 per fill everywhere. Pairs per unpaired loss available: 6.6. Needed: 15.5. No pair total fixes
this: < 0.95, < 0.97 and < 0.99 are all negative, and higher totals are worse (`results/pair_risk.md`).

Secondary causes:
- Stacking: up to 50 USDC per window gives −50 USDC worst windows.
- Sideways windows are the worst for the rule: −1.40 / window BTC, −3.16 all assets.

## 3. New strategy architecture

```
200 ms sample (the very points the archive stores)  --live._sample_prices -> engine.observe_point
   features: spot z over 0.5/1/2/5 s, acceleration, persistence, cross-exchange agreement + dispersion,
             token mid / 1 s / 3 s token momentum, book imbalance of both tokens, crypto order flow,
             distance to strike, 30 s realised-vol ratio, reversal count        (paired_exit.features_at)
   fair P(UP) = logistic(features)  (per-asset lock file; P(DOWN) = 1 - P(UP))      (paired_exit.FairModel)
   SIGNAL     = |z1| >= threshold, one per direction per second
evaluate:  kill conditions -> band -> agreement -> EV after costs -> exposure book -> risk -> FOK
fill:      Leg(...) + its OWN exit plan (on_leg_fill), logged ENTRY
every tick (live._managed_exits -> engine.exit_actions):
   take-profit ask (maker, re-priced to fair after a confirmed cancel) | TIME_EXIT | [STOP_LOSS | THESIS_INVALIDATED |
   UNPAIRED_TIMEOUT when switched on] -> FOK sells retried at the new bid; partial sells booked without
   double counting (live._book_managed_exit); the optional pair (off) locks a leg only if it fills first, and a
   pair that fills after its leg left becomes a NEW leg with its own exit (never uncontrolled)
```

The live features equal the replay's at every sample: a test compares all 17 features on a synthetic window, and 150
samples of three real windows were also checked. The first window after a start takes its sigma from the previous
window's archived points, exactly as the replay does.

## 4. Exact entry rules (paper defaults, BTC)

1. Signal: |z1| ≥ 2.0. Here z1 is the composite 1 s log return divided by the previous window's realised 1 s sigma.
   The side is the move's direction. At most one signal per direction per second.
2. Freshness: the signal sample is ≤ 600 ms old, spot and book ≤ 1,500 ms old (`STALE_DATA`), and the measured order
   round trip is ≤ 1,500 ms (`LATENCY_REJECT`).
3. Window: 60 s ≤ time left ≤ 290 s. Ask in [0.15, 0.85]. Spread ≤ 5 ¢. Exchange dispersion ≤ 3 σ.
4. Cross-exchange confirmation: mean sign of the exchanges' 1 s returns toward the side ≥ +0.34.
5. **EV** = fair(side) − ask − taker fee(ask) − (slippage 0.34 ¢ + adverse selection 3.33 ¢) must be at least
   **+0.02**, or +0.04 with < 180 s left. The costs were measured on TRAIN (`results/paired_exit_costs.md`).
6. Exposure book (`EXPOSURE_LIMIT`): at most 1 open leg, 3 buys per window, 10 USDC per window, 5 USDC per position,
   5 USDC unpaired and 5 USDC directional.
7. Order: FOK, 5 shares, limit = ask + 3 ¢.

## 5. Exact exit rules

Every leg has these from the moment its fill is known:

- **TAKE_PROFIT**: a resting ask at max(fill + 2 ¢, fair + 3 ¢), re-priced (≥ 1 tick, ≥ 0.4 s) as fair moves. The
  replacement is placed only after the venue confirms the old ask's cancel.
- **TIME_EXIT**: at 30 s left, FOK sell at bid − 2 ¢, unless the bid is ≥ 0.95. That leaves a 5-share leg at most
  ≈ 0.25 USDC to gain by settling.
- Implemented, **off by default**, each with its measured cost:
  - `STOP_LOSS`: bid ≤ fill − X.
  - `THESIS_INVALIDATED`: fair < fill − drop, or spot z1 against the leg ≥ 2.5 with the exchanges agreeing.
  - `UNPAIRED_TIMEOUT`: max hold.
  - `LADDER_LEVELS`: several asks.
  - the optional pair.

  Urgent sells are retried at the new bid until filled or the window ends.

Why the stops are off: after a realistic fill there is no continuation left. Selling at any horizon from 0 to 60 s
loses 3–12 ¢/share on every asset and split (`results/paired_exit_markouts.md`). An early exit gives up about
2.2–4.3 ¢/share (half spread + fee), so each one costs expected value. Their only effect is variance reduction:

| BTC variant (Stage 2/3) | train | val | test | fwd | walk-forward OOS |
|---|---:|---:|---:|---:|---:|
| fair TP + time exit (**chosen**) | +0.135 | +0.038 | +0.064 | +0.081 | +0.023 (t 0.75) |
| + stop 0.40 | +0.099 | +0.010 | +0.022 | +0.071 | −0.016 (t −0.65), worst window −4.95 vs −4.25 |
| stop 0.10 / 0.20 / 0.30 | +0.07 / +0.09 / +0.10 | −0.07 / −0.04 / −0.02 | 0.00 / +0.01 / 0.00 | −0.03 / +0.02 / +0.03 | |
| thesis: fair −0.10 / −0.20 / momentum 2.5σ | | −0.05 … −0.00 | | | |
| max hold 60 s | +0.04 | −0.09 | −0.02 | −0.03 | |

The hard bound on a leg's loss is its size: 5 shares × ask ≤ 0.85 = 4.25 USDC, one leg at a time.

## 6. Risk controls

- Every cap in the brief: `PX_MAX_WINDOW_USDC`, `PX_MAX_POSITION_USDC`, `PX_MAX_UNPAIRED_USDC`, `PX_MAX_DIRECTIONAL_USDC`,
  `PX_MAX_OPEN_POSITIONS`, `PX_MAX_BUYS_PER_WINDOW`, `PX_MAX_HOLD_SECONDS`, `PX_MAX_DAILY_LOSS` (20),
  `PX_MAX_CONSECUTIVE_LOSSES` (6 → 15 min pause).
- **Stacking protection**: the window, unpaired and directional exposure are recomputed before every order. A buy that
  raises |directional| over the cap is refused; one that reduces it is allowed.
- **Kill conditions** stop new entries; exits keep running:
  - daily loss;
  - an unpaired breach (a hard halt);
  - a loss streak;
  - stale spot, signal or book;
  - order round trip > 1.5 s;
  - spread > 5 ¢;
  - exchange dispersion > 6 σ (raised from 3 σ on 10-02 00:12 UTC: at 3 σ it refused 14 of 24 live signals in the first hour);
  - 3 unexpected settlements: the venue outcome is missing or contradicts the TWAP proxy (a hard halt).
- Log tags: `[PX] ENTRY_SIGNAL / ENTRY / PAIR_ORDER / PAIR_ORDER_FILLED / PAIR_ORDER_CANCELLED / TAKE_PROFIT /
  STOP_LOSS / THESIS_INVALIDATED / TIME_EXIT / UNPAIRED_TIMEOUT / EXPOSURE_LIMIT / STALE_DATA / LATENCY_REJECT /
  LEG_CLOSED / KILL_*`.

## 7. Backtest methodology

- **Data.** `data/charts`, 13,161 windows across BTC, ETH, SOL, XRP, DOGE, BNB and HYPE, 2026-09-22 15:40 → 10-01 21:25 UTC.
  Samples every 200 ms: composite and per-exchange spot, both tokens' touch and depth, crypto order flow, and the
  venue outcome. Cached by `scripts/paired_exit/cache.py`.
- **Execution.** The paper bot's CALIBRATED scenario, fitted on 193 real orders:
  - Taker orders land after 250 ms. A buy fills only if the ask stays inside the limit through landing + 1 s, and it
    is charged the worse of the two prices. Sells are the mirror image.
  - A killed exit is retried at the new bid.
  - Taker fee is 7 % · p(1−p) per share; maker fee 0, with no rebate counted.
  - Resting orders fill only when the far touch trades strictly through them, and only after LAT.
  - Partial fills are not modelled, because FOK makes them impossible. Fill uncertainty is stressed (§10).
- **Splits.** Chronological per asset: TRAIN 50 / VALIDATION 20 / TEST 15 / FORWARD 15 %.
  - Stage 1 (entry rule, 288 configs) and Stage 3 (exits × caps, 294 configs) were run on TRAIN.
  - Stage 2 (exit structures A–F, about 155) was run on each asset's Stage-1 entry.
  - Selection: the top 10 by TRAIN t of window P&L (≥ 80 fills), then the best VALIDATION $/window. TEST and FORWARD
    were never used to choose anything.
- **Walk-forward** (`results/paired_exit_walkforward*.md`). For each test day d: re-fit the model, re-measure costs and
  re-optimise the entry (72 configs) on all days < d−1, choose on day d−1, then trade day d untouched.
- **Fair model.** It barely beats the market: the log-loss gain over the token mid alone is ≈ 0.001 per sample (0.3 %),
  and is inconsistent out of sample. All momentum coefficients are ≈ 0 (`results/paired_exit_model.md`). The token
  price is close to the probability, and that is why the edge is thin.

## 8. Results (BTC, every metric: `results/paired_exit_comparison.md`)

| OUT-OF-SAMPLE (test + forward, 598 markets) | A equal shares | B today | C stop .20 + time | **D fair TP + time** | D' static ladder 10 sh | E D + optional pair | F pair + inventory |
|---|---:|---:|---:|---:|---:|---:|---:|
| P&L per market $ | −0.302 | −1.098 | +0.011 | **+0.073** | −0.078 | −0.034 | −0.806 |
| t (per market) | −4.8 | −4.1 | +0.4 | **+1.8** | −2.5 | −1.3 | −12.5 |
| EXPECTED_VALUE_PER_ENTRY $ | −0.350 | −0.147 | +0.035 | **+0.240** | −0.265 | −0.095 | −0.782 |
| win rate | 38 % | 90 % | 55 % | 80 % | 69 % | 91 % | 40 % |
| avg winner / avg loser $ | +2.17 / −1.90 | +0.24 / −3.63 | +1.20 / −1.37 | **+1.10 / −3.12** | +0.62 / −2.22 | +0.24 / −3.29 | +0.91 / −1.91 |
| SMALL_WIN / LARGE_LOSS · wins per loss | 1.14 · 0.9 | 0.065 · **15.4** | 0.88 · 1.1 | 0.354 · **2.8** | 0.28 · 3.6 | 0.07 · 13.8 | 0.47 · 2.1 |
| worst market · max DD $ | −4.0 · −186 | **−50.1 · −676** | −3.2 · −14 | **−4.25 · −14** | −3.6 · −53 | −4.2 · −23 | −6.9 · −484 |
| profit factor | 0.70 | 0.59 | 1.06 | **1.38** | 0.62 | 0.69 | 0.32 |
| fill rate · fills | 100 % · 517 | 71 % · 4,462 | 29 % · 187 | 30 % · 181 | 30 % · 177 | 32 % · 212 | 100 % · 616 |
| settled / stopped / TP / pair-locked | 100/0/0/0 % | 10/0/0/90 % | 53/45/0/0 % | 39/0/52/0 % (9 % time) | 0/31/69/0 % | 8/0/0/90 % | 4/40/39/0 % |

Answers to the brief:
- **A vs B vs C** (item 5). Buying both sides loses by construction: the two asks plus fees sum to more than 1. The
  winner alone (C0 / D) is the only positive structure. Adding the opposite as a secondary order (E) turns a positive
  structure negative, because it again locks winners at +0.235.
- **Ladders.** With the venue's 5-share minimum, a 5-share leg can carry one level. Static ladders (+5/+10/+20 ¢, maker
  or taker, 5 or 10 shares) are negative in every split. The fair-anchored single level is the only take-profit that
  keeps EV.
- **Losing-side inventory (F)** is the worst structure: −0.77 … −1.25 per market. It pays both spreads twice.

## 9. Out-of-sample results

- Fixed splits (D, BTC): TRAIN +0.135 / VALIDATION +0.038 / TEST +0.064 / FORWARD +0.081 USDC per market (t 4.0 / 0.65 / 1.8 combined).
- Walk-forward, 7 test days, 1,544 windows: **+0.023 per window, t +0.75**. Positive on 3 of 7 days, worst window
  −4.25. The same entries held to settlement: +0.033.
- In-sample t ≈ 4 shrinks to walk-forward t ≈ 0.75. Part of the in-sample edge is selection and regime luck.
- Other assets: none has a configuration positive in all four splits (ETH 0, SOL 0, DOGE 0, XRP 11 of 288, BNB 66,
  HYPE 36). The edge is concentrated in BTC, and the bot trades BTC only.

## 10. Stress tests (BTC, TEST + FORWARD, $ per market: `results/paired_exit_stress.md`)

| scenario | D (chosen) | C hold | C + stop .20 | B today |
|---|---:|---:|---:|---:|
| base | +0.073 | +0.063 | +0.013 | −1.098 |
| latency +50 / +100 / +200 % | +0.068 / +0.080 / +0.059 | +0.062 / +0.067 / +0.053 | +0.015 / +0.024 / +0.033 | ≈ −1.09 |
| fill probability −10 / −25 / −50 % | +0.071 / +0.053 / +0.029 | +0.055 / +0.061 / +0.046 | +0.030 / +0.033 / −0.006 | −1.04 / −0.93 / −0.66 |
| spread +1 / +2 ¢ | +0.054 / +0.023 | +0.063 / +0.031 | +0.034 / +0.002 | −1.25 / −1.56 |
| slippage +1 / +2 ¢ | +0.068 / +0.060 | +0.052 / +0.045 | +0.002 / −0.007 | −1.34 / −1.52 |
| 2 s quote survival (stricter fills) | +0.047 | +0.056 | +0.008 | −1.06 |
| resting orders fill on touch | +0.090 | +0.063 | +0.013 | −0.87 |
| book 1 s stale | +0.087 | +0.076 | +0.020 | −0.48 |
| spot 200 / 500 ms late | +0.025 / +0.052 | +0.026 / +0.076 | −0.001 / −0.010 | −1.12 / −1.07 |
| binance / coinbase / bybit missing | +0.079 / +0.079 / +0.076 | +0.091 / +0.068 / +0.067 | ≈ +0.02 | −1.10 |

D's sign survives every stress, and its worst window stays −4.2. The base itself has a standard error of about
±0.04 per market, so these differences are within noise. "Survives" means "does not turn clearly negative", not
"is profitable".

## 11. Failure cases

- **Regimes** (hindsight labels): BTC D makes +0.22 per market in strong-trend windows, −0.02 in weak trends, −0.03
  sideways, and 0.00 in high-volatility windows. The money comes from trend windows, and nothing causal identifies them
  in advance (`results/paired_exit_tail.md` §2).
- **Days**: 4 of the 7 walk-forward test days are negative (−0.01 … −0.06 per window).
- **The model** is the market price plus small corrections; a regime shift in the momentum–outcome relation would not
  be noticed by the model.
- **Not testable here**: the brief's 100 / 250 ms horizons. The archive is 200 ms; the live features use the same
  sampling so that live matches the replay.
- **Train/live differences**: the live bot's feed set (Deribit, Kraken, Bitstamp, Coinbase, Binance) is faster than
  the Binance/Bybit/Coinbase composite in 97 % of the archive. Paper fills will show whether that helps or shifts
  the signal.
- **Maker take-profit realism**: the paper venue's trade-through rule is conservative for fill rate, but it does not
  see queue position or a real taker lifting our ask.

## 12. Parameter sensitivity

- Entry (BTC, 288 configs):
  - 106 are positive in all four splits; the median config makes +0.07 / +0.04 / −0.00 / +0.03 per market.
  - The edge is broad but thin, and no single knob carries it.
  - k = 1–3, min edge 0–0.04, agreement on or off, and band 0.15–0.85 vs 0.30–0.70 all land within ±0.05 per market.
- Exits: every stop or thesis threshold and every static ladder is lower than hold, on every split (§5).
- Caps:
  - 2 open legs: +0.13 / +0.06 / +0.09 / +0.09, but the worst window doubles to −8.6.
  - 10 shares: +0.27 / +0.07 / +0.15 / +0.13, worst −8.6, max DD −39.
  - With one leg at a time, the window cap (5–50 USDC) does not bind.
  - Max buys 1 vs 3: 3 is better (a killed FOK may retry).

## 13. Recommended defaults for PAPER testing

The defaults live in `ecosystem.repricedmom.config.js`, block `PAIRED_EXIT`; §4 to §6 above give the reasons.

| setting | value |
|---|---|
| assets | BTC only |
| size and limit | 5 shares, FOK at ask + 3 ¢ |
| price band | 0.15–0.85 |
| signal | \|z1\| ≥ 2, exchange agreement ≥ 0.34 |
| entry costs | slippage 0.0034, adverse selection 0.0333 |
| EV threshold | ≥ +0.02 (+0.02 more with < 180 s left) |
| entry window | 60–290 s left |
| take-profit | fair + 3 ¢ (at least fill + 2 ¢), resting |
| time exit | 30 s left, unless bid ≥ 0.95 |
| stop / thesis / max hold / pair | off |
| exposure | 1 open leg, 3 buys / window, 10 USDC / window, 5 USDC unpaired |
| daily loss | 20 USDC |
| loss streak | 6 losses → 15 min pause |
| bankroll | paper, 100 USDC, fresh ledger since 2026-10-01 22:51 UTC |

To judge it, use `data/live_trades.triangle-btc-paper.jsonl` and the `[PX]` log lines. Compare against the replay
after ≥ 300 fills, not after a few windows: the replay expects ≈ 0.3 fills per window and +0.02 … +0.07 per window.

## 14. What should NOT be enabled in LIVE

- Real money, for any of this. The walk-forward t of 0.75 is not evidence (`validation-bar`).
- The resting opposite order, on any asset, at any pair total, and the optional pair (E).
- Equal-share structures (A, F) and static take-profit ladders.
- More than one open leg, or more than 5 shares, until paper fills confirm the replay.
- ETH, SOL, XRP, DOGE, BNB, HYPE: no robust configuration.
- Price stops, thesis exits and max-hold exits as profit tools. They are variance tools at a measured EV cost, to be
  switched on only if the user values that trade-off.

## 15. Next experiments

1. Paper fills vs the replay: ≥ 300 BTC fills.
   - Fill rate: replay 30 %.
   - Take-profit share: replay 52 %.
   - Adverse selection: replay 3.3 ¢.
   - EV per entry: replay +0.24.
   - Re-measure the cost constants from the paper fills.
2. Tick-level 100 / 250 ms horizons on the tick recordings (2.25 h today: too short). Record more ticks first.
3. A causal trend filter, using the window's running trend and the previous windows' realised trend. The edge sits in
   trend windows.
4. Feed-set parity: run 3-source vs 5-source composites side by side on paper, or rebuild the archive's features with
   the live set.
5. Maker take-profit realism: compare paper TP fills with the real book's trades once a trade feed is archived.

## Implementation notes

- **Code.**
  - `strategy/paired_exit.py` (pure): features, fair model, legs, exit planner, exposure book, kill monitor.
  - `strategy/paired_exit_engine.py`: a mixin on `TriangleEngine`, inert unless `TPB_TRIANGLE_PX_ENABLED=1`.
  - `live.py` hooks, all behind `engine.manages_exits` or getattr checks, so every other strategy keeps its old path:
    - `observe_point` on each 200 ms sample;
    - `_managed_exits` replaces `_take_profit` for managing engines;
    - `_book_managed_exit` books partial sells (the market's P&L is booked once, when it goes flat or at settlement);
    - `on_leg_fill`, `px_on_pair_done`, `on_settle_check`.
  - `TriangleConfig.apply_budget` lets one leg reach `PX_MAX_POSITION_USDC`.
- **Fixed on the way.** The dashboard's "Entry min EV" control never reached the triangle entry model: the engine
  copied the value at start. The thresholds are now read from the config at use.
- **Tests.** 21 new; the full suite passes (1,189 + the 2 added after it).
- **Restart, 22:51 UTC.**
  - New buys were paused through the price-band controls, then the bot waited until it held nothing (22:50:43).
  - The old run's files were moved to `data/archive/triangle-btc-paper_20261001_225100/`.
  - The temporary controls were removed (the control file is byte-for-byte the same values as before), and the bot
    started fresh.
  - Re-reading the control file applied the dashboard's paper drawdown limit (60) to the pair-lock paper bot, which
    had been running with 80 since its start.
- **Restarted at 22:56:57 UTC, holding nothing.** The first run (22:51) rolled sigma on a market's first sample, which
  is taken 60 s before that market opens, so it used an unfinished previous window. Sigma now rolls on the new window's
  first own sample. After a restart, a previous window seen only partly is replaced by its archived file. Two tests
  cover this.
