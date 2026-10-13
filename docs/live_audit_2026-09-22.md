# Paper-vs-live audit, 2026-09-22

Every number here was computed from files in this repository: `data/live_trades.jsonl`,
`data/earnings.jsonl`, `data/live_state.json`, `data/charts/*.json` (234 archived
windows, 15 traded) and the pm2 log of the armed bot (`~/.pm2/logs/tpb-bot-error.log`,
48 real orders sent between 17:23 and 20:59 UTC). Nothing is estimated from memory.
The scripts that reproduce the tables are `scripts/paper_vs_live.py` and
`scripts/loss_attribution.py`.

**The one-paragraph version.** The paper bot's profit is not a strategy edge, it is a
simulator artefact. The paper engine fills orders against the bot's *own* local copy of
the Polymarket book, and that copy lags the venue's matching engine by one to five
seconds. When spot moves, the venue's makers re-price within the same second; the local
book still shows the old quote for seconds. The paper engine "buys" that dead quote and
banks the re-price seconds later (every one of the 20 paper exits was a take-profit
inside ten seconds of entry). The live bot sends the same order to the venue and it is
killed: **27 of the 29 real orders sent in the direction of the preceding spot move were
fill-or-kill rejections; the only such fill was the only live winner.** The orders that
*do* fill live are the ones where the maker is happy to sell, which is when the market
is moving against the model (both such fills lost immediately). That is adverse
selection, and no sizing, calibration or latency work changes it. The fix is (1) a paper
engine that refuses quotes which do not survive the local book's lag, (2) a gate that
does not send orders into a quote that has not yet re-priced after a spot move, and
(3) instrumentation and capital protection so that this is measured rather than argued.

---

## 1. Architecture report

```
market/discovery.py    AssetRegistry probes 28 slugs on Gamma every 30 min; MarketMeta per window
feeds/spot.py          Binance + Bybit + Coinbase bookTicker/trade WS -> CompositeSpot (median, deviation gate)
live.py                LiveBot: one asyncio loop, 200 ms trade tick, 100 ms state write, 200 ms sampler
  ClockSync            NTP-style offset against api.binance.com/time, every 300 s
  LiveBook             per-token book from a WS snapshot + price_change deltas (no sequence check)
  EmpiricalLatency     bootstrap sampler of measured spot/book/order latencies (paper only)
pricing/twap.py        analytic digital option on the 60 s TWAP settlement, Student-t(4)
pricing/vol.py         TwoScaleVol (1 s fast EWMA x slow ratio)
features/engine.py     24 causal features; features/orderflow.py OFI score (drift zero until |t|>=2)
strategy/engine.py     StrategyEngine.evaluate: gates -> Reason | intent; p_used = 0.5 model + 0.5 mid
signals/costs.py       fee 0.07 p(1-p) (verified), expected_slippage, uncertainty charge
risk/limits.py         RiskManager: fractional Kelly x confidence, per-market/asset/epoch/total caps,
                       daily loss, drawdown (no consecutive-loss halt)
execution/paper.py     PaperExchange: 3-clock matching against the LOCAL book, FOK, contention, fees
execution/polymarket.py PolymarketExchange: FOK market orders via polymarket-client SecureClient,
                       LiveCaps (order/open/hourly/daily), kill file, cash reconcile every 30 s,
                       resting (maker) orders, redemption via relayer
live.py exits          _exit_candidate: take-profit at bid +0.05, stop-loss at bid -0.20 (2 ticks through)
live.py settlement     Gamma outcomePrices at close+40 s, 5 retries, else own TWAP proxy; archive window
control.py             data/controls.json tunables (all/paper/live scope), KILL file
web/                   dashboard (http.server + hand-rolled WS), auth, earnings, chart archive
backtest/, scripts/    archive loader, 1 s grid, replay (hold-to-settle only), walk-forward models
```

### Paper pipeline

```
WS book (venue publish ts) -> LiveBook (local, lag unknown) -> engine.evaluate (200 ms tick)
  -> revalidate on the same local book -> PaperExchange.submit (latency draw)
  -> _execute_on_arrival: match against the SAME local book at arrival time
  -> fills at the touch, fee charged, position; exits via the same path
  -> settle from Gamma outcome; ledger + earnings rows tagged live-paper
```

### Live pipeline

```
same feeds, same engine, same local book -> PolymarketExchange.submit (caps, kill file)
  -> worker thread: sign FOK market order (amount USDC, max_price) -> post_order
  -> venue matches against ITS book: 44 of 48 orders were rejected (FOK kill 40, caps 1,
     min size 1, api error 2 halts); 3 fills
  -> fill booked from the venue reply; cash reconciled every 30 s (cash only)
  -> exits: same FOK path; settle from Gamma; winners redeemed via relayer
```

## 2. Paper-vs-live gap report

| # | Difference | Evidence | Effect |
|---|---|---|---|
| 1 | **Paper matches against the local book; the venue matches against its own.** `book_p50 = 40 ms` measures venue-publish-time to receipt, not event-to-receipt; the venue's WS publish is itself delayed. | For 30 killed live orders the local book still showed the quote 0.5 s later in 13 cases, 2 s later in 5, 3 s later in 3. Paper filled 23/23 of the same signal class; live filled 1/29. | Paper wins that cannot exist. |
| 2 | **Paper never models adverse selection**: a quote is filled whether or not the maker would have honoured it. | The three live fills all came from the minority "against the spot move" class (2 of 3 such orders filled vs 1 of 29 the other way). | Live fills are the maker's choice, not ours. |
| 3 | **Exits were never simulated in any research replay** (`backtest/replay.py` is hold-to-settle). Take-profit +0.05 / stop-loss -0.20 exist only live and in the live paper bot. | 20/20 paper exits were take-profits within 10 s; 2/3 live exits were stop-losses into a collapsed bid (0.34 -> 0.13, 0.45 -> 0.08). | The shipped exit policy is untested against history. |
| 4 | **Paper spot and venue price are on different clocks.** Spot p95 latency 1.8 s; the venue's makers see spot faster than the bot. | Live fills happened after spot had already moved against the bot's stale composite (SOL: +1.6 bps before, +7.7 bps after). | The "edge" is the bot's own information lag. |
| 5 | **Depth**: paper takes the recorded level size; the venue's FOK requires the whole size at max_price on the venue book. | 27 kills with local depth >= 10 shares. | Same as 1. |
| 6 | **Fees**: paper charges the schedule; live estimates the same schedule but never reads the venue's deduction. | Not yet measurable (3 fills). | Unknown fee error. |
| 7 | **Order round trip**: live 231-621 ms per fill, paper draws from the paper model (`order_rtt_p50 32 ms` in the live state file is the paper sampler; the live adapter does not feed `EmpiricalLatency`). | State file. | Live latency is not in the staleness gates. |
| 8 | **Hourly order budget**: live caps 60 orders/hour; kills count. Paper has no cap. | `live_caps` reject at 20:11. | Live misses signals paper takes. |
| 9 | **Consecutive API-error halt** triggered by killed FOKs (fixed in the working tree). | `LIVE TRADING HALTED: 5 consecutive API errors` 17:48. | Live stopped for 12 minutes. |
| 10 | Live-paper resets the ledger on restart; live keeps it. Earnings file was corrupted by test fixtures once (`.bak-204040`). | files | Reporting only. |

## 3. Loss attribution report

Live settled trades: 3 (n far too small for a rate). Per trade, net of fees:

| trade | entry | exit | net PnL | category | evidence |
|---|---|---|---|---|---|
| SOL DOWN 1790108700 | 0.34 @195 s left, 500 ms after the token fell 0.45 -> 0.34 | stop 0.13 @190 s (first stop attempt was an API error) | -1.62 | ADVERSE_SELECTION (stale spot) + STOP into collapse | spot +1.6 bps in the prior 3 s, +7.7 bps in the next 5 s; outcome UP; holding would have lost -2.46 |
| BTC DOWN 1790108700 | 0.45 @77 s left | stop 0.08 @53 s | -2.06 | ADVERSE_SELECTION + STOP into collapse | archive missing (window not saved); venue outcome not retrievable |
| BNB DOWN 1790105400 | 0.70 @119 s (filled 5c better than sent) | take-profit 0.81 @114 s | +0.47 | the one "with move" fill | mid moved -0.04 then +0.10 |

Order-level attribution (48 real orders, the statistically meaningful set):

| class | orders | filled | FOK killed | other | if filled: hold PnL/share, win rate | mid move in next 2 s |
|---|---:|---:|---:|---:|---|---:|
| sent WITH the prior 3 s spot move | 29 | 1 | 27 | 1 (min size) | +0.50, 86 % | +0.15 |
| sent AGAINST it | 3 | 1 | 2 | 0 | -0.01, 33 % | -0.10 |
| no archive to classify | 16 | 1 | 11 | 4 | | |

Loss share by category, live: EXECUTION/STALE_QUOTE (unfillable signals) explains 100 % of the
gap between paper (+$46 on 23 fills) and live (-$3.20 on 3 fills); ADVERSE_SELECTION explains
both losing fills; STOP_LOSS exit slippage cost ~0.15/share against a hold that would have lost
more. MODEL_ERROR cannot be separated from adverse selection with n = 3. LATENCY, SPREAD,
LIQUIDITY and PARTIAL_FILL contributed nothing measurable (no partial fills exist: FOK).

## 4. Risk report

Present: per-order / open / hourly / daily-loss caps outside the strategy (`LiveCaps`), kill
file, strategy daily loss and drawdown, per-epoch correlated exposure, 5-error API halt,
never-chase revalidation, entry and exit retry throttles.

Weaknesses found:

1. **No position reconstruction after restart.** `PolymarketExchange.__init__` starts with
   empty positions; `reconcile()` compares cash only. A restart with a token position leaves
   it unmanaged (no stop, no take-profit, no settlement PnL) and the risk budget unaware.
2. **Kill file blocks exits too.** `_precheck` refuses every order while `data/KILL` exists,
   including a stop-loss. "Halt orders" leaves open positions with no way out.
3. **No halt on execution failure rate.** 27 consecutive FOK kills spend the hourly budget and
   are never treated as a signal that the strategy is unexecutable.
4. **No halt on feed disconnect / stale data for exits**; staleness only gates entries.
5. **No unexpected-balance or unknown-position detection.** A venue cash change the bot's fills
   do not explain is silently attributed to redemptions or deposits.
6. **No latency or slippage halt.** Live RTT is not even recorded in the latency sampler.
7. **Three daily-loss numbers disagree**: `LiveCaps` 10 (CLI default), `RiskConfig` 20 scaled,
   `controls.json` live scope 40. The binding one is `LiveCaps` (10), which is right, but the
   dashboard can raise `caps.max_order_usdc` up to 500 at runtime (`control.py:182`).
8. `consecutive_losses` is counted but never acted on.
9. The stop-loss is a taker into a one-sided, collapsing book, sent 2 ticks through the bid,
   with no floor on how much of the stake it may realise; it has never been replayed.

## 5. Execution report

| metric | paper (23 fills) | live (3 fills / 48 orders) |
|---|---:|---:|
| fill rate | 100 % of orders in the ledger (kills are not logged in paper) | 6 % (3/48); 33 % in the last session (4/12 incl. exits) |
| round trip | 24-171 ms (sampler) | 231-621 ms (venue reply) |
| slippage vs seen ask | -0.005/share mean | -0.017/share (BNB filled 5c better) |
| fees | schedule | schedule estimate, venue deduction unread |
| spread at entry | 1-2 ticks | 1-2 ticks |
| depth at touch (local) | 20-400 shares | 20-400 shares (local); venue: unknown, FOK killed |
| time from entry to exit | 1-10 s (paper TP) | 5 s and 24 s (stops), 5 s (TP) |

The latency that matters is not the order round trip; it is the gap between the venue's book
and the bot's copy of it, which is unmeasured. The bot needs an *event-time* book latency
measurement (venue trade prints vs local book) and per-order forensics (sent price vs venue
best at the venue's ack time).

## 6. Model report

* Only calibration in production: `p_used = 0.5 * p_analytic + 0.5 * mid`. No Platt/isotonic
  layer is loaded live.
* Rolling model-vs-mid bias is small (BNB -0.001, BTC +0.015 over ~7,800 looks), so the model
  is not globally miscalibrated. The trades are selected where the disagreement is enormous:
  paper entries had `p_used` 0.47 on average against a market 0.38, with single entries at
  model 0.70 vs market 0.145. These are exactly the seconds after a spot move, before the
  local book has re-priced: the model is "right" only because the quote is dead.
* OOS evidence from the archive (docs/strategy.md I): blend Brier beats the market by 0.0026,
  every fitted model is worse than the market. Per-epoch t-stat below 2 on every replay.
* Live model-vs-market by time bucket (state file): bias inside +-0.01 in every bucket.
* Conclusion: there is no evidence of an edge that survives execution. The calibration curve
  by bucket cannot be computed on 3 live fills; the paper calibration is contaminated by the
  simulator artefact and must be recomputed after the simulator fix.

## 7. Prioritised improvement plan

| # | change | expected impact | confidence | difficulty | risk reduction |
|---|---|---|---|---|---|
| 1 | Paper engine: quote-survival rule (kill a fill whose quote vanishes from the local book within H ms), scenario presets, depth-at-arrival | removes the artefact that produced 100 % of paper profit; at H = 2 s paper fill rate drops from 78 % to 26 % (live 9 %), H = 3 s to 17 % | high (measured) | low | high (stops false confidence) |
| 2 | Entry gate STALE_QUOTE: do not send when spot moved > k sigma in the last N s and the token has not re-priced | 27 of 29 such orders were killed live; stops burning the hourly cap and stops the adverse-selected residue from filling | high (measured) | low | medium |
| 3 | Trade forensics (lifecycle record per order with timestamps, sent vs venue price, net PnL) | makes 4-6 measurable | high | medium | medium |
| 4 | Guard / kill switch: feed down, stale data, FOK-kill rate, latency, balance drift, unknown position; exits allowed under KILL | prevents unmanaged positions and repeated unexecutable orders | high | medium | high |
| 5 | Restart reconciliation from venue positions and open orders | closes weakness 1 | high | medium | high |
| 6 | Deployment modes PAPER / SHADOW / MICRO_LIVE / LIVE with a graduation check | prevents scaling on n = 3 | high | low | high |
| 7 | Paper-vs-live and loss-attribution scripts | reproducible evidence | high | low | low |
| 8 | Replay exits in the backtest | the shipped exit policy is untested | medium | medium | medium |
| 9 | Event-time book latency measurement | quantifies gap 1 | medium | medium | low |

Not adopted: widening the FOK cross (refuted in docs/replay.md), maker entries as the default
(adverse selection replaces slippage; the paper matcher's pessimistic maker rule already says
so), raising size or frequency.
