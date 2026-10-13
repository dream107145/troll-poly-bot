# EARLY_WINDOW_LOW_ASK: buying cheap tokens in the first minute, 2026-09-24

**Verdict: REJECTED.** After realistic execution costs the apparent edge is
gone. The rule as specified is statistically zero (+0.4 c/share, t = 0.10 over
epochs, 26 fills in 46 hours). The broad version of the trade, every cheap ask
in the first minute, loses 7.1 c/share with t = -10.4. Where the rule enters
inside the first minute makes no difference to P&L. Nothing has been enabled,
wired into `live.py` or given a real-money path.

Reproduce: `python scripts/replay_early_window.py` (about 10 min on 5 cores).
Full tables: `data/replay/early_window_replay_2026-09-24.md`. Raw trades:
the `.json` next to it.

## The rule

`strategy/early_window.py`, research only. `seconds_since_market_start = (now - open_ts) / 1000`.
A new entry is allowed only when `0 <= s <= 60` (both ends inclusive: 12:00:00 and
12:01:00 are allowed, 11:59:59 and 12:01:01 are not). The rule also requires all of:

| condition | default | rejection reason |
|---|---|---|
| executable ASK < threshold (and >= 0.05) | 0.40 | ASK_NOT_LOW / ASK_TOO_LOW |
| spread | <= 0.03 | SPREAD_TOO_WIDE (no bid counts as too wide) |
| spread / ask | <= 10 % | SPREAD_PCT_TOO_WIDE |
| ask depth within 2 ticks | >= 10 shares | THIN_ASK |
| our 5 shares <= 50 % of that depth, >= 5 s left | | EXECUTION_NOT_REALISTIC |
| book fresh and not crossed | <= 1000 ms | STALE_BOOK / BOOK_INVALID |
| spot fresh, sources agree | <= 1500 ms | STALE_SPOT / DATA_INCONSISTENT |
| model probability available | | MODEL_NOT_READY |
| p_side - ask (p = 50/50 blend of the TWAP pricer and the mid, as live) | >= 0.03 | EDGE_TOO_SMALL |
| p_side - ask - entry fee - exit fee - 0.5 c slippage - latency - 0.5 x model band | > 0 | NET_NOT_POSITIVE |

The entry price is the executable ASK. The mid is used only inside the blended
probability, never as a price. The order is a fill-or-kill capped at ask + 1 c,
and never at a price where the expected net stops being positive. Exit: sell
into the BID once it is 5 c above entry and the round trip clears both fees;
otherwise the position is held to the 1.0 / 0.0 settlement. All of these values
were fixed before the replay was first run.

## The test

`backtest/early_window_replay.py` replays 2,742 recorded windows: 486 epochs,
7 assets, 2026-09-22 15:30 to 2026-09-24 13:30 UTC, with a book point every
~226 ms. The probability comes from the live pricer, using the live bot's own
strike. Execution uses the momentum replay's measured model: venue round trips
(p50 243 ms), fill-or-kill against the book at arrival, and a 2 s
quote-survival rule. Exits pay the bid through the same latency. Statistics are
clustered by 5-minute epoch, because every asset in one window is one bet.

## Results

| run | fills | fill rate | win | net / share | total net (5-share orders) | 95 % CI net/trade | t (epochs) |
|---|---:|---:|---:|---:|---:|---|---:|
| **the rule, realistic** | 26 | 32 % | 81 % | **+0.004** | +0.49 | [-0.35, +0.37] | **+0.10** |
| the rule, conservative | 23 | 30 % | 74 % | +0.000 | +0.05 | [-0.52, +0.49] | +0.01 |
| the rule, optimistic (old paper engine) | 86 | 100 % | 90 % | +0.028 | +12.16 | [-0.04, +0.31] | +1.52 |
| every cheap ask (no model gate) | 2,450 | 53 % | 71 % | **-0.071** | -869 | [-0.42, -0.29] | **-10.4** |
| every cheap ask, sell at bid after 60 s | 2,446 | 53 % | 57 % | -0.065 | -800 | [-0.38, -0.28] | -12.6 |

**1. Cheap first-minute tokens do not reprice enough.** Without the model gate,
73 % of fills see the bid reach entry + 5 c. That still loses, because the
payoff is lopsided. A winner banks about +0.24 per 5 shares (5 c minus two taker
fees). A loser that never reaches the target rides to settlement and loses
about -1.83. Breaking even needs an 88 % target-hit rate. The market is also
calibrated at these prices: tokens bought at 0.35-0.40 won at settlement
34-36 % of the time, so there is no mispricing to collect either.

**2. The model gate's small positive result comes from the simulator.** 68 %
of the gated orders never filled: 32 were killed on arrival and 22 were filled
but the quote had gone 2 s later. The model finds edge mostly in quotes that are
about to vanish, the same adverse selection the live audit found on 2026-09-22.
Assume every order fills at the decision quote, as the old paper engine did,
and the same rule shows +2.8 c/share. Under realistic execution it is +0.4 c
with an interval spanning ±35 c per trade.

**3. Timing inside the first minute.** Here each 10-second slice ran on its
own, with entry allowed only inside that slice:

| slice | every cheap ask: fills | net / trade | t | fill rate | avg depth | avg spread | rule: fills | rule: net / trade |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0-10 s | 1,090 | -0.35 | -6.9 | 71 % | 192 | 1.7 c | 9 | -0.35 |
| 10-20 s | 1,371 | -0.34 | -8.0 | 77 % | 326 | 1.4 c | 12 | +0.34 |
| 20-30 s | 1,586 | -0.36 | -8.8 | 82 % | 323 | 1.4 c | 3 | -0.19 |
| 30-40 s | 1,670 | -0.34 | -9.1 | 81 % | 338 | 1.4 c | 6 | -0.51 |
| 40-50 s | 1,756 | -0.33 | -9.3 | 84 % | 359 | 1.4 c | 6 | -0.30 |
| 50-60 s | 1,792 | -0.33 | -9.8 | 86 % | 339 | 1.3 c | 5 | +0.10 |

The book **is** different right after the start: in the first 10 s depth is
about 40 % thinner, the spread is wider and more orders are killed. **P&L is
not different.** Every slice loses 33-36 c per 5-share trade and every t is
below -6.9. The rule's per-slice numbers rest on 3-12 fills each, and every
interval includes zero. The +0.34 in 10-20 s is not evidence of a better
moment to buy.

**4. Nothing is stable.** The rule's chronological thirds: -1.2, -3.2 and +18 c/share
(the last on 3 fills). By asset, from -10 to +20 c/share on 1-8 fills each.
Threshold sensitivity (not used to choose anything): 0.30 gives -4.7 c/share,
0.35 gives -4.7, 0.40 gives +0.4, 0.45 gives +2.2 (t 0.68). The sign flips with
the threshold, which is what noise looks like.

**5. One variant to watch, not to trade.** Same entry, no target, held to
settlement: +10 c/share on 69 fills, t = 1.51, 95 % CI [-0.15, +1.15] per trade.
It was one of ten variants looked at, so its t overstates the evidence. It is
the analytic engine's settlement bet made only in the first minute. That is a
hypothesis for forward shadow data, not evidence.

**6. Choosing assets or noise regimes does not rescue it**
(`scripts/early_window_by_noise.py`, `data/replay/early_window_by_noise_2026-09-24.md`).
The broad trade (every cheap ask, ~350 fills per asset) was split by what was
known at entry: asset, spot volatility (absolute and against the asset's own
median), and the token's own noise (30 s mid range, 1 s mid-change sd, and the
last 10 s move).

* Every asset loses on its own, from -5.8 c/share (SOL) to -10.6 c (HYPE), t -6 to -9.
  The noisiest token books (HYPE: 22.5 c median 30 s range) hit the target
  least often (63 %).
* Noise helps the target-hit rate a little: the top spot-vol tercile hits 76 %
  against 71 % in the bottom. That is far from the 88 % break-even. Every
  tercile of every noise measure loses 6-8 c/share (t -6.8 to -8.6).
* Honest selection: tercile cut points and buckets were chosen on the first
  half of the epochs and scored on the second. There were 64 candidates (assets,
  noise terciles, asset x noise cells, each with >= 30 training fills).
  * With the +5 c target, **none was positive even in training**.
  * Held to settlement, 11 were positive in training. Pooled, they made
    -3.6 c/share on the held-out half. The best training cell (SOL x high token
    range, +9.3 c) made -2.8 c out of sample.
* The rule's own 26 fills happen at much higher spot volatility than the broad
  trade (median 1.1-6 bps/s against 0.5-1.5). The model gate finds its "edge"
  in fast markets, which is where the quotes vanish before the order arrives
  (section 2).

## Timing rule, verified

In the default run, 655,574 decision points before the open were all refused
as BEFORE_MARKET_START and 11,368 after 60 s as ENTRY_WINDOW_CLOSED. Zero
orders were decided outside `0 <= s <= 60`. Tests in
`tests/test_early_window.py` cover every boundary from the example.

## Limitations

* The recorder keeps the touch and the depth within 2 ticks, not the full
  ladder. Fills are priced at the arrival ask, which is fine for 5 shares and
  optimistic for anything larger.
* Spot freshness is a proxy: the composite not changing for 5 s counts as a dead
  feed, with a modelled 300 ms age otherwise. Cross-exchange deviation is not
  recorded per point, so that gate is inactive in the replay.
* 46 hours and 486 epochs. That is enough to reject the broad trade (t = -10).
  It is far too few to prove or disprove a 26-fill rule either way. Section 1
  says why that should not be read as "needs more data": the unconditional
  trade is clearly negative, and the gate only helps under fills that did not
  happen.
