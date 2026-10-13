# MOMENTUM_TAKER: execution realism, 2026-09-23

What changed, why, the exact rules, and what the evidence says. The rule
throughout was the brief's: optimise for trades that are **real, still
available, still worth their price after every cost, and measured**, never
for more trades. Every number below comes from the repository's own files and
logs; nothing is estimated from memory.

**Verdict first.** There is **no evidence of positive net expectancy** for momentum taking
on these markets, and strong evidence against it. On 1,152 recorded windows
(22 hours, 7 assets), replayed through the live decision and exit code with
the venue's measured latency and the quote-survival rule, the momentum signals
**lose in every period, asset, time bucket and momentum bucket**: with the
edge gate switched off the realistic out-of-sample expectancy is -0.34 USDC
per 5-share trade (about -7 c/share, t = -4.9 over epochs), win rate 16 %,
and the price moves against the fill within a second (markout -1.3 c). The old
paper engine's assumptions (fill at the decision quote, no latency) turn the
same trades into a 100 % fill rate, 43 % winners and a POSITIVE 1 s markout --
that difference is the paper-vs-live gap, reproduced. With every real cost
charged, the strict profile (and even `easy`, and every grid point down to a
zero edge requirement) places **zero orders** on the same history, and the
signals it refuses would have lost 2.0-2.3 c/share on average. The engine now
correctly declines to trade; it records every refused signal so the question
can be re-asked on new data at no cost.

---

## 1. Architecture (as found, and what it is now)

```
spot  Binance / Bybit / Coinbase WS ──> CompositeSpot (median) ──> SpotHistory, vol, TWAP
       T0 = exchange event time of the newest spot TRADE (quotes carry none)
       T1 = local receipt (clock synced to Binance)
book  Polymarket market WS ──> LiveBook per token (snapshot + price_change)
       ts = venue timestamp, recv_ts = local receipt, conn = the WS connection it came on
       quote_age(token) = now - venue time our view is valid as of   (NEW, not auto-widened)
every 200 ms  MomentumEngine.evaluate  ──> Evaluation (+ a Reason for every rejection)
       -> revalidate on the freshest book -> kill switch -> _confirm_entry checklist (NEW)
       -> marketable limit: FOK, price cap = min(ask + max_slippage, max_entry_price)
       -> PaperExchange (paper, shadow)  |  PolymarketExchange worker thread (micro-live, live)
            live worker, right before POST (NEW): kill file again, decision age, freshest-book veto
            T5 sent, T6 venue ack, T7 venue fill, RTT -> the latency model (NEW)
       -> fill -> RiskManager, forensics (T0..T10), markouts (NEW), signal fate (NEW)
every tick   exits: MomentumEngine.exit_decision on a FRESH momentum read (NEW)
settlement   Gamma outcome at close + 40 s -> ledger, earnings (tagged with strategy)
guard        entries stop on feed loss, stale data, kill rate (venue kills only), latency,
             balance drift, unknown position, FOREIGN TRADE (NEW), RECONCILE_PENDING (NEW), ...
```

The Phase 1 audit (19 questions) is answered in the session summary; the
short version of each is in section 2.

## 2. Problems discovered

| # | problem | evidence | fixed by |
|---|---|---|---|
| 1 | The paper engine's order latency came from **Gamma HTTP GETs** (p50 35 ms), not orders. Real POSTs: 218-661 ms (p50 243). Paper orders "arrived" ~15x too early, into books the venue had already changed. | `live_state.json` `order_rtt_p50 35.3`; every `VENUE FILL ... rtt` in the pm2 logs | venue-only order bucket, measured prior, seeding from live forensics |
| 2 | Killed FOK orders never recorded their round trip (exception path) | 61 kills, 11 RTTs in the logs | `_record_rtt` on every POST |
| 3 | "Book age" was the fresher of two tokens' last change; no receipt time; the gate **auto-widened** to 2.5x measured p95 with no ceiling (72 s seen under CPU starvation) | state file, paper run 14:15 | per-token quote age (venue + receipt + connection); momentum gate not widened; engine widening capped at 2x |
| 4 | The "freshest book" recheck read the same book in the same synchronous tick: it could never see a change. The real gap -- queue + signing in the worker, incl. a metadata fetch per new token -- was unchecked. | code; SDK `resolve_market` | last look in the worker right before POST; metadata prewarm at discovery |
| 5 | The kill file was checked when an order was QUEUED, on the real exchange only; paper ignored it | `_precheck` | checked in every mode before submit, and again right before POST |
| 6 | `controls.json` could loosen micro-live (daily loss 10 -> 40 happened at 12:56; per-order cap up to 500) | pm2 log `CONTROL Daily loss limit: 10 -> 40` | `MicroCaps.enforce` after every control change |
| 7 | Graduation counted only early-exited trades; held-to-bell losers vanished from the evidence; strategies mixed | `graduation._rows` | all settled rows, marked estimates excluded, per-strategy |
| 8 | Shadow (`--live` dry run) **cancelled other processes' orders** on a shared account at start-up, and never exercised exits | `authenticate()` | read-only shadow; shadow redesigned (section 4) |
| 9 | Nothing measured adverse selection or missed opportunities | -- | markouts, signal outcome tracker |
| 10 | Edge ignored the **exit**: a pre-settlement exit is a second taker fee plus the spread. At 0.50 that is ~2.25 c the entry test never saw. | fee schedule 0.07 p(1-p) | `exit_cost` in the edge and the max entry price |
| 11 | Momentum: one 5 s window drove everything; no exhaustion, no max entry price, no uncertainty; `easy` (measured -1.6 c/share) was the default | `strategy/momentum.py` | sections 5-6 |
| 12 | Every rejection -- incl. our own kill switch / dry run -- counted as a venue kill in the guard's kill rate | `_on_result` | only venue kills count |
| 13 | Tests could write the real data dir through default relative paths (the guard only matched fixture-shaped slugs in five files) | `tests/conftest.py` | the test session runs in a temporary cwd; all `data/*.jsonl` checked |
| 14 | **Account not proven dedicated**: at 12:59 a 6.33-share buy (-5.07 USDC) hit this bot's account (wallet 0xa8b3...62F3) that no bot on this server sent (the other repo's bots use a different wallet); the guard latched BALANCE_DRIFT (correctly) | pm2 logs, preflight | FOREIGN_TRADE latch from the venue's own trade list |

## 3. Files changed

New: `execution/outcomes.py` (markouts, signal fates), `backtest/momentum_replay.py`,
`scripts/replay_momentum.py`, `scripts/momentum_report.py`,
`ecosystem.momentum.config.js`, tests `test_momentum_v2.py`,
`test_execution_realism.py`, `test_account_isolation.py`, `test_momentum_e2e.py`, this document.

Changed: `strategy/momentum.py` (rewritten engine), `live.py` (quote age,
confirmation, signal/markout wiring, exits, shadow twin, latency, kill switch,
micro ceilings), `execution/polymarket.py` (pre-post gate, timeline, RTT,
prewarm, foreign trades, read-only dry run), `execution/paper.py` (arrival
hook, timeline), `execution/forensics.py` (T0..T10 + segments),
`execution/decisions.py` (full momentum/execution record), `execution/latency.py`
(measured priors), `risk/guard.py` (FOREIGN_TRADE, RECONCILE_PENDING),
`graduation.py`, `signals/costs.py`, `strategy/engine.py` (reasons, fields),
`types.py`, `__main__.py`, `tests/conftest.py`, `.env.example`.

## 4. Execution architecture

**Displayed vs executable.** The executable price is the VWAP of the ask
levels for the intended size under the cap (`get_executable_price`); the mid,
last trade, bid and fair value are never used as an execution price. The book
is the live WebSocket copy -- a REST snapshot per order would add a round trip
and be older, not fresher.

**Marketable limit.** Every entry is a fill-or-kill market order with
`max_price = cap`, `cap = min(best ask + max_slippage, max_entry_price)`. The
venue enforces the no-chase rule: if the ask has moved past the cap on
arrival, the order dies.

**Three looks before money moves.**
1. `revalidate` -- the executable VWAP for the size is still under the cap
   and the max entry price, and the edge survives the move.
2. `_confirm_entry` (event loop) -- spot fresh, the traded token's quote
   fresh, time remaining, balance, no open order, no position, risk limits,
   guard, kill switch, direction. The checklist is stored with the order.
3. `_pre_post_refusal` (venue worker, immediately before POST) -- kill file
   again, decision age <= `TPB_MAX_ENTRY_DECISION_AGE_MS` (1000), and a veto
   from the freshest book (ask above cap / not enough under it). The ask seen
   here is recorded as the price at submission.

**Paper** (`PaperExchange`, scenario `realistic`): the order reaches the
matching engine after presend + RTT/2 drawn from **measured venue round
trips** (prior p50 243 ms; replaced by the live runs' own `venue_rtt_ms` once
25 exist); it matches the book as it is then (FOK: all or nothing, walks
levels, touch contention); the fill only counts if the quote is still on the
book 2 s later (reproduced 25/30 live kills on 2026-09-22); fees on the
venue's schedule; exits the same way.

**Shadow** (`--mode shadow`): realistic paper positions, exits and PnL, PLUS
every order copied through the real-money path in dry run -- account
reconciliation, signing, the pre-post checks -- and never posted. Each order
records `live_path` (`dry_run` = would have posted; `stale_decision`,
`price_moved`, `kill_switch`, caps ... = why not). Read-only on the account.

**Micro-live** (`--mode micro-live`): real orders at `MicroCaps` (5 USDC per
order, 10 open, 10/day loss, 5 orders/hour, 1 position), the smallest valid
size every time, ceilings the dashboard cannot lift, and a paper twin on every
order for same-order comparison.

## 5. Momentum algorithm

Windows 1, 2, 3, 5 (main), 10, 15, 30 s on the composite spot (sampled >= 200 ms).
Per window: log return, z = r / (sigma sqrt(t)), velocity (bps/s in the move's
direction). Plus: acceleration (last 5 s vs the 5 s before), consistency (share
of the last 10 one-second steps in the direction), persistence (share of the
3/5/10/15 s windows agreeing), choppiness (sd of 1 s steps around their mean /
sigma), 15 s taker trade-flow imbalance, and the token's own 1.5 s move.

States: `NEUTRAL` (|z5| < 1), `WEAK_UP/DOWN` (1 <= z5 < 3), `STRONG_UP/DOWN`,
`REVERSING` (the last 3 s against the move, or the move is exhausted).

Exhaustion (entry is skipped): retrace >= 40 % of the 10 s extreme move;
stall (last 2 s velocity < 25 % of the 5 s velocity); deceleration; the
token's mid falling >= 1 c in 1.5 s against spot (book reversal); choppiness
>= 3x the estimate (volatility spike). The brief's example
(+0.01 ... +0.14, +0.13, +0.10 %) is labelled REVERSING at +0.13 already
(`test_the_brief_example_is_exhausted_not_bought`).

Score (0-1): 0.4 strength + 0.2 consistency + 0.2 persistence + 0.1
acceleration + 0.1 flow, halved when not STRONG. Heuristic; it scales sizing
and the uncertainty margin, never the fair value.

## 6. Entry formula

```
fair         = mid(side) + min(cap, per_sigma * max(0, |z5| - 2))       [calibration table if given]
uncertainty  = 0.003 + 0.5 * continuation * (2 - score) + 0.01 * (choppiness - 1)+ + asset extra (+ calib SE)
exit_cost    = fee(fair) + spread / 2
latency_cost = 0.01 per second of (reaction lag + spot age)
costs(p)     = fee(p) + 0.005 slippage + latency_cost + uncertainty + exit_cost
max_entry    = highest tick p with fair - p - costs(p) >= min_net_edge (0.02)
cap          = min(ask + 0.02, max_entry)
trade iff    STRONG, not exhausted, fresh data, token re-priced 1-5 c in 3 s, ask <= 0.85,
             spread <= 0.03, ask <= max_entry, depth under cap >= 10, size <= 50 % of that depth,
             VWAP - ask <= 0.01, net(VWAP at size) >= 0.02, cooldown / max trades / risk / guard pass
```

Size: the smallest valid order in micro-live; otherwise fractional Kelly x
confidence (score x edge/(edge+uncertainty)), the smallest valid size when
confidence < 0.5, always inside every risk cap.

## 7. Exit logic (`exit_decision`, evaluated on a fresh momentum read)

| exit | fires when | order |
|---|---|---|
| TAKE_PROFIT | bid >= entry + 0.04, and the round trip clears both fees | FOK at the bid |
| STOP_LOSS | bid <= entry - 0.06 | FOK walking the bids to cover the size, at most 5 c through the touch |
| LIQUIDITY_FAILURE | bid depth within 2 ticks < 1.5x the position | same (leave while a bid exists) |
| MARKET_ABNORMALITY | spread >= 0.10, or spot / quote data >= 3 s old | same |
| MOMENTUM_REVERSAL | z5 against the position <= -1.5, or the move in our direction is actively reversing | same |
| TIME_STOP | held >= 20 s, not in profit, momentum faded below 1 sigma; or held >= 45 s | same |

Momentum still strong in our favour holds the position; a stall or partial
retrace after entry is "weakening" -- it does not pay a round trip on its
own; the time stop closes a thesis that faded. KILL stops entries only;
KILL_ALL stops exits too.

## 8. Quote freshness rules

* `quote_age = now - max(token's last venue timestamp, newest venue timestamp processed on the current connection)`;
  no snapshot on the current connection = no book.
* Momentum entries: quote age <= **500 ms** (healthy measured p95 99-124 ms),
  spot age <= **1000 ms**. Not auto-widened.
* The engine's generic gates follow measured latency at most up to 2x their base.
* Measured on the live feed today: book lag p50 36-48 ms, p95 99-124 ms;
  spot feed p50 150-180 ms, p95 265-370 ms (unloaded). Under the CPU load seen
  at 14:13 the Polymarket reader fell behind ("1013 slow consumer") -- the
  quote-age gate is what stops trading on that.

## 9. Latency measurements

Measured on this machine (n in brackets). Median / p75 / p95 / p99, ms.

| segment | value | source |
|---|---|---|
| spot feed (exchange trade time -> received), 30 s snapshots over the day | 159 / 178 / 268 / 3,072 (1,418) | micro-live bot STATUS lines |
| Polymarket book (venue stamp -> received) | 34 / 42 / 105 / 231 (1,418) | same |
| venue POST round trip (order sent -> reply) | 243 / 297 / 661 / 661 (11; min 218) | every `VENUE FILL ... rtt` in the pm2 logs |
| decision -> result, live fills | 359, 372, 554, 621 (FOK); 1,199-7,597 (resting) | `FILL ... rtt` lines |
| decision -> result, OLD paper fills | 24-117 (30 fills, p50 ~37) | same lines, paper runs: the simulator artefact |
| decision -> POST (queue + signing) | not measured before today; prior 15-250, recorded per order from now on | `presend_ms`, `sign_ms` |
| T0 -> T10, per order | recorded from now on in `trade_forensics*.jsonl`; `scripts/momentum_report.py` prints the percentiles | -- |

Under CPU saturation (load 8-10 on 6 cores at 14:13) spot lag reached
p50 16 s in a niced process and the venue dropped the book feed ("1013 slow
consumer"): the quote-age and spot-age gates are what keep a starved process
from trading.

## 10-11. Slippage and fill model

Paper/replay: FOK fills iff, at the simulated arrival, the ask is <= the cap
with the size behind it (touch contention applied), AND the quote is still
there 2 s later. Price = the arrival ask (VWAP over levels in the live
engine). Slippage = fill - decision VWAP. Exits: FOK sell fills at the arrival
bid if it is >= the order's floor with the size behind it, else retried every
2 s; within 10 s of the bell the position settles 1/0.

## 12. Risk controls

Per order (micro 5 USDC), open exposure (10), daily loss (10, venue adapter;
strategy 10), drawdown, max consecutive losses (guard, 5), max trades per
market (1) and cooldown (60 s), one position (micro), max slippage (cap),
max spread (3 c), max quote age (500 ms), max spot age (1 s), max decision age
(1 s), venue RTT p95 (3 s), hourly order budget (5), kill switch in every mode
immediately before submission and again before POST, foreign-trade /
balance-drift / unknown-position / reconcile-pending halts.

## 13. Configuration

`TPB_MOMENTUM_<FIELD>` for every `MomentumConfig` field (see `.env.example`),
`TPB_MOMENTUM_PROFILE=strict|easy` (strict default), `TPB_MAX_ENTRY_DECISION_AGE_MS`,
`TPB_MICRO_*` ceilings, `--scenario`, `--mode`, `--data-tag`, `--kill-file`.

## 14. Tests

639 tests pass. New: `test_momentum_v2.py` (states, exhaustion incl. the
brief's example, VWAP example, max entry, chase prevention, exit costs,
uncertainty, liquidity, freshness, asset filter, every exit kind, bid-walking
stop), `test_execution_realism.py` (venue latency vs Gamma, arrival matching,
disappearing / changing ask, quote survival, FOK never partial, GTC partial +
cancel, quote age, kill switch in paper and before POST, stale decision,
book veto, timeline, markouts, signal fates, in-flight dedupe, shadow path,
widening cap, kill-rate), `test_account_isolation.py` (foreign trades, guard
latches, reconcile pending, read-only shadow, micro ceilings, graduation,
earnings tags, cwd isolation), `test_momentum_e2e.py` (one trade end to end).

## 15-17. Results

### Realistic paper: the archive replay (`scripts/replay_momentum.py`)

1,152 windows, 2026-09-22 15:30 -> 09-23 14:35 UTC, split by epoch:
TRAIN 566 windows / 105 epochs (15:30-05:35), VALIDATION 286 / 53
(05:35-10:00), OUT-OF-SAMPLE 300 / 53 (10:00-14:35). 5-share orders (the
micro-live size), both taker fees charged on every fill.

**Deployable profiles, every real cost in the gate: zero orders.**
`strict` 0, `easy` 0, and a training-period grid of z in {2.5, 3, 3.5, 4} x
minimum net edge in {0, 0.01, 0.02}: 0 orders at every point -- "no grid
point had >= 20 trades and positive expectancy; nothing selected, nothing
validated". What the gate refused (strict, sampled every 2 s per reason per
window; the hypothetical is a taker buy at the ARRIVAL ask, sold at the bid
20 s later, both fees):

| refused because | signals | price gone at arrival | would have lost | would have won | mean hypothetical |
|---|---:|---:|---:|---:|---:|
| ask outside 0.05-0.85 | 343 | 6 | 226 | 109 | -1.97 c |
| ask above the max entry price | 241 | 1 | 147 | 90 | -2.34 c |
| liquidity | 46 | 0 | 30 | 15 | -2.11 c |

**Diagnostic, NOT deployable: the edge gate switched off** (no exit-cost or
uncertainty charge, any net edge; every other gate on), to measure the signal
itself under each execution model:

| execution / period | orders | fill rate | trades | win | expectancy / trade | PnL | max DD | 1 s markout | t (epochs) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| realistic / train | 74 | 58 % | 43 | 21 % | -0.248 | -10.66 | 10.66 | -0.24 c | -4.26 |
| realistic / validation | 58 | 47 % | 27 | 26 % | -0.289 | -7.81 | 7.81 | -0.69 c | -5.28 |
| **realistic / out-of-sample** | 64 | 48 % | 31 | 16 % | **-0.344** | **-10.65** | 10.82 | -1.32 c | **-4.87** |
| optimistic (old paper) / out-of-sample | 54 | 100 % | 54 | 43 % | -0.174 | -9.39 | 9.85 | +1.66 c | -3.21 |
| conservative / out-of-sample | 65 | 38 % | 25 | 4 % | -0.389 | -9.72 | 9.72 | -1.24 c | -6.03 |

Realistic order outcomes, all periods: 101 filled, 75 quote pulled (the
local book still showed a quote the venue had already moved), 20 killed on
arrival. Out-of-sample exits: 14 stop-losses (-12.8 c/share, held 4.3 s), 13
momentum reversals (-4.0 c, 3.6 s), 4 take-profits (+3.1 c, 16 s), 1
liquidity failure. By asset (all periods): BTC -0.16, DOGE -0.19, ETH -0.23,
SOL -0.30, XRP -0.32, HYPE -0.37, BNB -0.43 per trade; by time left: early
200-295 s -0.28, middle 120-200 s -0.29; by z: 3-4 -0.29, 4-5 -0.28, >=5
-0.37. Nothing is positive anywhere with a usable sample, so there is no asset
or time filter to switch on: filtering a uniformly negative signal only
changes how much it loses.

The one number that separates the simulators is the **markout**: the old
engine's fills are followed by a +1.7 c move in our favour (it "bought" quotes
that were already gone and re-priced toward us), the realistic engine's by
-1.3 c (the fills we can actually get are the ones the market is about to
move against). That is the adverse selection seen live on 2026-09-22.

Calibration fitted on TRAIN only (`data/momentum_calibration.json`, not
enabled): the token mid 20 s after a momentum signal moved +1.7 c (z 2-3),
+2.6 c (z 3-4), +4.6 c (z >= 4) in the signal's direction. The continuation
is real -- and smaller than the ~3.5 c of two taker fees plus the spread
needed to capture it, before the adverse selection at the fill.

### Live paper runs (today)

Two realistic-paper bots (strict, easy) were started at 14:12 and stopped
after 5 minutes: the machine was saturated (load 9.6 on 6 cores; see
section 9), their spot lag was 10-22 s, every evaluation was correctly
refused as STALE_DATA, and they were slowing the real-money processes. Paper
on this host needs spare CPU to measure anything but the host.

### Shadow

A 2-minute shadow preflight of the new code on the real account (14:36):
no errors; account balance 73.57 USDC; 4 foreign-trade checks, none found;
position reconciliation covered all 28 live-market tokens (13 legacy
positions in resolved markets, correctly not treated as unknown); balance
drift 0; guard and account check clear; spot lag p50 178 ms, book 34 ms.
No shadow trading session has been run: with the strict profile the replay
says it would place no orders.

### Micro-live (the old code, before today's changes)

Momentum micro-live since 02:31 UTC: one round trip -- bought 5.06 @ 0.90
(ask; cap 0.91), sold @ 0.89 on MOMENTUM_REVERSAL 26 s later, **-0.12 USDC**
after fees. The new engine would have refused it (ask above 0.85, and a
continuation at 0.90 cannot pay the exit costs). Its paper twin had called
that order QUOTE_PULLED: in this one case the realistic model was more
pessimistic than the venue. Since 12:59 entries were stopped by BALANCE_DRIFT
(a -5.07 USDC trade on the account this bot did not place). The account's
other real trades today (analytic engine, resting entries): +0.26, -1.03,
-1.08. n = 4 in total: not evidence of anything.

## 18. Remaining weaknesses

1. **The strategy has no measured edge.** Everything above is the bot
   declining to trade a signal that loses after real costs. Nothing here
   makes it profitable, and nothing should be loosened to make it trade.
2. The archive is 22 hours of one regime; 53 out-of-sample epochs. The
   negative result is strong (t about -5) but the market can change; the
   signal outcome tracker re-measures it on every live signal at no cost.
3. Recorded depth is "within two ticks of the touch", so the replay fills at
   the touch; real VWAP on a thin level can be worse (5-share orders make
   this small).
4. The quote-survival rule (2 s) is calibrated on 30 live kills; T0 exists
   only for spot TRADES (quotes carry no exchange time); decision -> POST is
   measured from now on, its prior is a guess.
5. Model uncertainty uses priors (base 0.3 c, half the claimed continuation,
   vol, asset); it does not yet depend on time to expiry. The calibration
   table can add a measured standard error once validated.
6. Stop-loss and momentum-reversal exits are taker sells into a moving book;
   the replay shows them costing 4-13 c/share. Maker exits are not built.
7. The host is CPU-saturated by the other bots; the Polymarket reader drops
   ("1013 slow consumer") under load. The gates stop trading on it, but a
   co-located, less loaded host is a precondition for any latency-sensitive
   strategy.
8. The account is not proven dedicated: a -5.07 USDC trade at 12:59 came from
   no bot on this server (the other repo trades a different wallet). The
   FOREIGN_TRADE latch will stop entries if it happens again.

## 19-21. Commands

```bash
# the evidence on history (train / validation / out-of-sample)
python scripts/replay_momentum.py --grid --write-calibration data/momentum_calibration.json

# STAGE 1: realistic paper
python -m troll_poly_bot --balance 50 --mode paper --scenario realistic --strategy momentum_taker --data-tag momentum-paper
#   or: pm2 start ecosystem.momentum.config.js --only tpb-momentum-paper

# STAGE 2: shadow (reads the account, signs, never posts, cancels nothing)
python -m troll_poly_bot --balance 50 --mode shadow --strategy momentum_taker --data-tag momentum-shadow
#   or: pm2 start ecosystem.momentum.config.js --only tpb-momentum-shadow

# STAGE 3: micro-live (REAL orders; dedicated account; TPB_LIVE_ACK in .env).
# Untagged: it writes data/live_state.json, which the dashboard shows.
python -m troll_poly_bot --balance 50 --mode micro-live --strategy momentum_taker --keep-history
#   or: pm2 start ecosystem.momentum.config.js --only tpb-momentum-live

# the report, all stages side by side
python scripts/momentum_report.py --tag momentum-paper --tag momentum-shadow --tag ""
python scripts/why_not_trade.py --decisions data/decisions.momentum-paper.jsonl
touch data/KILL        # stop new entries (every mode); data/KILL_ALL stops exits too
```

## 22. Normal live trading stays disabled

`--mode live` is refused unless `graduation.assess(strategy=...)` passes on
real epochs of THAT strategy (300 epochs, per-epoch t >= 2, positive total,
drawdown bounded) or `TPB_GRADUATION_OVERRIDE=I_ACCEPT_UNPROVEN_EDGE` is set.
`--live --armed` without `--mode` is micro-live. Nothing in the code moves a
bot from one stage to the next; the momentum stages live in their own pm2
file so `pm2 start ecosystem.config.js` cannot start one by accident.
