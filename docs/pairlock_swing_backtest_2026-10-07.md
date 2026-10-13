# Swing recovery (limited-depth directional recovery + two-sided hedge at swing 3+): backtest, 2026-10-07

Strategy: `strategy/pairlock_swing.py` (the recovery paper bot's swing mode, `TPB_PAIRLOCK_SWING_MODE=1`).
Script: `scripts/pairlock/replay_swing.py`. Full numbers: `results/pairlock_swing_backtest_2026-10-07.json`.

## What was run

* **Data**: every recorded BTC 5-minute window, `data/charts/btc-updown-5m-*.json`, 2026-09-22 15:40 → 2026-10-07 18:05 UTC:
  **3,216 markets processed**. Each has the best bid/ask of both tokens about 5 times a second, the depth within 2 cents
  of the touch, and the venue's outcome.
* **Decisions**: the bot's own code (`pairlock_swing.entry_decision` / `evaluate` / `observe`) on every sample, with the
  spec's defaults:
  * Swing 0: 10 shares at an executable VWAP of 0.55–0.60, 20–30 s after the open.
  * A swing is recognised when the token against the position leads by 0.04 (mid against mid) and its ask is ≥ 0.70.
  * Swing 1 buys 24 shares, swing 2 buys 80. Swing 3+ hedges to UP = DOWN.
  * Capital cap 125. Order limit = best ask + 0.03. No orders in the last 15 s. 2 s between orders.
* **Fills**: modelled four ways (the decision path is the same code in every mode):

| Mode | Fill model |
|---|---|
| **Theoretical** | Every order fills in full at once: the entry at the decision's best ask, every swing and hedge buy at exactly 0.70. No fees, no depth or capital limit. The spec's arithmetic on each market's real swing path. **Not a realistic result.** |
| **Executable VWAP** | Fills at once at the VWAP of the order's size, walked through the recorded asks (best ask + 2 cents; the recorded depth split evenly). No fees. Depth and capital bounds apply. |
| **After fees** | As above, plus the venue's taker fee 0.07·p·(1−p) per share. |
| **After configured slippage** ("realistic") | The paper bot's execution model, scenario `calibrated`. The order arrives 143 ms after the decision. 80 % of the touch level has been taken by faster traders. Fill-and-kill up to the limit. The book 1 s later must still fill the size at or under the limit, else the order is killed. Charged at the worse of the two VWAPs. Fees in. No new order until the result is known (+1.1 s); swings are still tracked. |

## Results (capital cap 125 unless stated)

| | Theoretical | Executable VWAP | VWAP + fees | Realistic (paper fill model) |
|---|---:|---:|---:|---:|
| Markets processed | 3,216 | 3,216 | 3,216 | 3,216 |
| Markets entered | 1,587 | 1,588 | 1,588 | 1,450 (246 of 1,702 entry orders killed) |
| **Swing 0** count / P&L | 725 / **+3,076.5** | 725 / +3,069.9 | 725 / +2,946.4 | 641 / +2,598.5 |
| **Swing 1** count / P&L | 561 / **+739.8** | 562 / +397.6 | 562 / +115.7 | 527 / **−181.3** |
| **Swing 2** count / P&L | 198 / **+2,052.8** | 198 / +1,270.1 | 198 / +963.5 | 189 / +286.2 |
| **Swing 3+** count / P&L | 103 / **−3,557.3** | 103 / −4,067.4 | 103 / −4,292.4 | 93 / **−4,102.7** |
| **Total P&L** | **+2,311.8** | +670.2 | **−266.8** | **−1,399.2** |
| Average per entered market | +1.457 (t 5.5) | +0.422 (t 1.5) | −0.168 (t −0.6) | **−0.965 (t −2.9)** |
| Win rate / loss rate | 92.9 % / 7.1 % | 86.8 % / 13.1 % | 84.1 % / 15.9 % | 73.5 % / 26.5 % |
| Maximum drawdown | −152.4 | −249.5 | −571.9 | −1,434.2 |
| Maximum / average capital used | 125.0 / 28.3 | 125.0 / 28.9 | 125.0 / 29.3 | 125.0 / 30.3 |
| Fees paid | 0 | 0 | 944.7 | 821.1 |
| Average per market by bucket (0 / 1 / 2 / 3+) | +4.24 / +1.32 / +10.37 / −34.54 | +4.23 / +0.71 / +6.42 / −39.49 | +4.06 / +0.21 / +4.87 / −41.67 | +4.05 / −0.34 / +1.52 / −44.12 |
| Avg fill price: entry / swing 1 / swing 2 / hedge | .574 / .700 / .700 / .700 | .574 / .722 / .737 / .759 | same | .575 / **.741 / .767 / .784** |
| Positive days | 14 / 16 | — | 8 / 16 | **1 / 16** |

The bucket is the deepest swing a market reached. Swing 3+ is 6.5 % of entered markets and costs more than everything
else earns once prices are real.

### Stability (chronological quarters, average P&L per entered market)

| | Q1 09-22→09-26 | Q2 09-26→09-30 | Q3 09-30→10-03 | Q4 10-03→10-07 |
|---|---:|---:|---:|---:|
| Theoretical | +1.61 | +1.34 | +1.53 | +1.33 |
| VWAP + fees | +0.13 | −0.09 | −0.12 | −0.61 |
| Realistic | −0.82 | −1.03 | −0.60 | −1.45 |

Nothing in the strategy is fitted, so there is no train/test split. The realistic result is negative in every quarter
and on 15 of 16 days.

### Capital cap and the hedge

* **At the spec's 125**, the default path (125.00 before fees) needs about 127.7 once fees are in.
  * With VWAP + fees, 99 of 103 hedges stop short of UP = DOWN; the cap is never exceeded.
  * Realistic: 86 of 93 hedges stop short. The swing buys cost more than 0.70, so the room left for the hedge shrinks.
  * Worst realistic market: −78.0. It hedged to only 47 of the 90 shares on the other side, then that side won.
* **At a 150 cap**, 74 of 93 realistic hedges are complete. The result is slightly worse: −1,495.2 (−1.03 / market).
  The hedge buys the swing-3 leader at 0.78 on average, and that leader went on to win 71 % of the time (66 of 93).
  Each completed hedge therefore locks in a loss of about −44 (theory: −35).
* **Hedge off** (`ENABLE_TWO_SIDED_HEDGE=0`, cap 150), for comparison: −952.6 (−0.66 / market). Swing 3+ alone is
  −3,656: 24 wins of about +11 and 69 losses of about −55.

## Why theory and execution differ

* **Swing buys cost more than 0.70.** By the time a swing is recognised and the order lands, the new leader is still
  rising: swing 1 fills at 0.741 and swing 2 at 0.767 on average. That pushes swing 1's average to −0.34 and cuts
  swing 2's from +10.4 to +1.5.
* **Fees** cost about 0.57 per market.
* **The entry is about zero-EV on its own.** Holding the first leg alone would have made +9 over 1,450 markets
  (realistic).

## Limits of this backtest

* Depth beyond 2 cents of the touch is not recorded and is taken as zero. The 2-cent depth is split evenly over three
  price levels, an assumption. Large orders (80 / 66 shares) may fill worse or better than modelled.
* The realistic mode is the paper bot's calibrated fill model. It was calibrated on spot-lag orders and has not been
  validated for 80-share orders.
* Outcomes are the venue's (`outcome` in the recordings). Markets whose recording ends early miss their last swing.
  This explains the few swing-2 markets lost to a late reversal with no swing-3 hedge.
