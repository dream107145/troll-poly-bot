# Spot-lag execution edge: what transfers from the fast BTC-5m traders (2026-09-29)

RESEARCH plus two engine switches, both off by default. No process was started; live trading was not
touched. Script: `scripts/research_spot_lag_fast.py`; tables: `results/spot_lag_fast_report.md`.

**Answer.** Of the four things the fast traders do (act on the first tick, buy small clips, buy both sides,
never sell), the one that transfers with evidence is **small clips**: taking what the touch holds instead of
refusing an order that cannot fill in full. It is implemented (`size_mode = touch`) and improves net profit by
5–10% in every period, with no extra drawdown. **Holding to settlement** is the largest raw gain (+50% forward)
but doubles the drawdown and loses its edge under the $20 daily stop, so it is implemented but not used.
**An earlier trigger** cannot be shown to help on our data. **Buying both sides** is a different strategy.

## What was inspected

- Strategy `strategy/spot_lag.py`: 1 s spot return ≥ 3σ, 3 s agreement, acceleration; one signal per market and
  direction per 2 s; FOK at min(ask + 2c, max entry) for `size_shares`; refused when the book under the limit
  holds fewer shares than the order.
- Execution `execution/polymarket.py`: FOK POSTs, measured round trip 219 ms median; the live fill rate depends
  on book freshness (26% under 60 ms, ~2% above 120 ms; `docs/book_feed_2026-09-28.md`).
- Fees: taker 0.07·p·(1−p) per share per leg; settlement free; rebates only at volume tiers we do not reach.
- Previous results: `docs/spot_lag_parameter_optimization_2026-09-25.md` (no robust edge on the 12 h untouched
  test; the edge returned in the next 13 h), `docs/trader_x-moneyforwhiskas_2026-09-29.md`.

## What was tested

Realistic execution model (`research_spot_lag_realistic.RealExec`): every latency stage drawn from the live
bot's measured samples, the venue book read as the archive 500 ms after arrival, FOK against the top level,
taker fees on both legs, the production budget (4 positions, $6 per order, $24 exposure, 60 buys/h, 1 entry and 3
attempts per market). Gates as the live bot runs them: 2c edge, limit ask + 2c, spread ≤ 3c, ask ≤ 0.60.

| period | UTC | hours |
|---|---|---:|
| train | 09-22 12:45 → 09-24 06:45 | 33.7 |
| validation | 09-24 06:45 → 09-25 00:45 | 17.3 |
| test | 09-25 00:45 → 09-26 02:35 | 25.8 |
| **forward** | **09-26 02:35 → 09-29 13:30** (no study had read it) | **82.3** |

Grid: 11 triggers (the live rule; the 500 ms and 250 ms returns at 2.5–5σ with the 1 s return in the same
direction) × 3 sizes (fixed 10, fixed 5, adaptive: 10 if the top level holds 10, else what it holds down to 5)
× 2 exits (the production 5–10 s exit; hold to settlement) = 66 configurations, 4 latency draws each. Selection
on train + validation only.

## Results (net USDC, mean over draws; fills in brackets)

| change from the live rule (fixed 10, timed exit) | train | validation | test | **forward** | forward max DD |
|---|---:|---:|---:|---:|---:|
| **baseline** | +128 (114) | +49 (24) | +30 (55) | **+169 (200)** | 15.3 |
| **size: adaptive** | +142 (128) | +51 (25) | +33 (60) | **+178 (221)** | 16.8 |
| size: fixed 5 | +81 | +26 | +19 | +93 | 9.2 |
| exit: hold to settlement | +204 | +34 | +77 | +254 (199) | 35.8 |
| adaptive + hold | +227 | +31 | +80 | +294 (220) | 35.2 |
| trigger 500 ms, 4σ | +129 | +47 | +34 | +173 | 8.2 |
| trigger 500 ms, 3σ | +126 | +44 | +43 | +125 | 20.6 |
| trigger 250 ms, 4σ | +113 | +36 | +37 | +138 | 16.0 |

**With the live bot's $20 daily-loss halt applied** (no new entries after −$20 in a UTC day):

| | train | validation | test | forward | halted days (forward) | worst day |
|---|---:|---:|---:|---:|---:|---:|
| baseline | +128 | +49 | +30 | +169 | 0 | −7 |
| **adaptive size** | **+142** | **+51** | **+33** | **+178** | 0 | −7 |
| hold to settlement | +204 | +34 | +77 | +140 | 1 | −24 |
| adaptive + hold | +227 | +31 | +80 | +152 | 1 | −24 |

### Why each conclusion

- **Adaptive size (take what the touch holds).** 5% of signals find fewer than 10 shares at the touch; the live
  bot refuses those ("not enough depth under the cap, need 10"). Filling 5–9 shares instead adds 8–10% more
  fills at the same per-share profit: +11% train, +4% validation, +8% test, +5% forward. Fixed 5 shares halves
  the profit, so the size itself matters; the gain is from not refusing.
- **Hold to settlement.** Per share it is better (+17c vs +13c dev, +13c vs +8c forward): one fee instead of two,
  no spread, no 2c cross, and the winners run to $1. But each position becomes a $6 binary bet: per-fill p10 is
  −$5, losing streaks reach 9, the forward drawdown is $36 against a $24 budget, and the $20 daily stop would
  have halted one forward day and cut the forward result to +$152, below the timed exit's +$178. Validation is
  also worse (+31 vs +51). It is the fast traders' most visible habit, but they run it on both sides with
  hundreds of clips per day; on one side at our size it is variance, not edge.
- **Earlier trigger.** Whatever the trigger, the ask at arrival is already above the limit for ~80% of signals,
  with a mean move of +11 to +13c by arrival. The 500 ms and 250 ms triggers find the same shocks a step earlier
  in *our* recorded data but arrive at the venue just as late, so they add fills only at lower quality (250 ms:
  +2.8 to +7.3c/share forward vs +8.4c). The archive is sampled at ~220 ms from a composite feed that is itself
  ~130 ms behind the exchanges, so a sub-200 ms edge cannot be measured here. The live evidence (fills need a
  book under 60 ms old) says the latency lever is the machine and the feed, not the rule.
- **Both sides.** Their maker side is a hedge, not a signal (no post-fill drift). It needs a two-sided fill
  pipeline and rebate-tier volume; not this strategy.

## What changed in the code (all default-off; tests in `tests/test_spot_lag.py`)

| switch | default | effect |
|---|---|---|
| `TPB_SPOT_LAG_SIZE_MODE=touch` | `fixed` | when the book under the limit holds fewer than `size_shares`, send what it holds (≥ the venue minimum) instead of refusing |
| `TPB_SPOT_LAG_HOLD_TO_SETTLEMENT=1` | off | never time-stop; take-profit / stop-loss still apply if set |
| `TPB_SPOT_LAG_SHOCK_WINDOW_S=0.5` | `1.0` | the shock over a shorter window, √window-scaled, with the 1 s return in the same direction; the event pre-check uses the same window |

Paper app `tpb-spotlag-paper-touch` in `ecosystem.spotlag.config.js`: the live bot's settings plus
`size_mode=touch`. Not started.

## Realistic fill assumptions

- A buy fills only if the recorded ask 500 ms after the simulated arrival is within the limit and the top level
  then holds the order's size; no partial fills, never the decision price. Adaptive size uses that same arrival
  depth. The live engine walks levels under the cap (slightly more permissive than the sim).
- Fill rates in the sim are 17–20% for the timed exit; the live bot has shown 6–12%, and its fills concentrate
  on fresh books. The sim's quote-age proxy admits older quotes than the venue actually fills, so every number
  here is optimistic in level; the *differences* between configurations use the same fills and are what the
  conclusions rest on.
- Settlement outcomes are the venue's, and settlement is fee-free.

## Remaining weaknesses

1. **Fill-rate level.** The bot fills half as often as the simulator; the adaptive gain (+5–10%) is small
   against that gap.
2. **Book staleness at decision time** (150–200 ms during shocks since 09-29 06:00) is the binding constraint and
   is outside this change.
3. **Small forward sample per period** for the fast triggers; their ranking is noise.
4. **Hold-to-settlement is untested with a take-profit overlay** (sell at +10c, else settle), which might keep the
   per-share gain with less variance; not measured.
5. **The paper venue inflates fills for tick-driven bots**; the paper app must be judged by the recorded-book
   audit, not its dashboard P&L.

## Safe to paper-test?

Yes, for `tpb-spotlag-paper-touch`: it only changes order size downward on thin books, within every existing cap
(order ≤ $6, exposure ≤ $24, daily loss ≤ $20), and its fills are inspectable. Not recommended for the live bot
until the paper audit confirms the 5–9-share fills happen at the venue. Hold-to-settlement should not be paper-
or live-tested at these risk limits.
