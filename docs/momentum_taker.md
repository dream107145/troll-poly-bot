# MOMENTUM_TAKER (experimental), 2026-09-23

> **Superseded in part by [momentum_execution_2026-09-23.md](momentum_execution_2026-09-23.md)**:
> the engine now has six momentum states, exhaustion detection, a maximum
> entry price that is also the order's price cap, exit costs and a model
> uncertainty margin in the edge, per-token quote age, momentum-aware exits,
> markouts and missed-signal tracking, and a realistic replay with
> train / validation / out-of-sample periods. **`strict` is the default
> profile again**; `easy` is opt-in, for data collection only.

Selected with `--strategy momentum_taker` (or `TPB_STRATEGY=momentum_taker`). The
analytic strategy is untouched and remains the default. The engine lives in
`strategy/momentum.py`; everything else (feeds, books, risk, guard, decision
journal, forensics, paper and live exchanges, exits) is the existing code.

## Existing flow (as found)

```
spot WS (Binance/Bybit/Coinbase) -> CompositeSpot -> vol / TWAP / FeatureEngine.spot
Polymarket WS book -> LiveBook per token (local copy, lags the venue)
every 200 ms: StrategyEngine.evaluate(market, books, spot, ages, ...)
  gates -> analytic TWAP pricer blended 50/50 with the mid -> net edge vs ask -> risk.size -> Evaluation
  revalidate on the freshest book -> to_order (FOK taker or post-only maker) -> exchange.submit
  (paper: PaperExchange with the quote-survival rule; live: PolymarketExchange, caps, kill file)
_on_result -> fills -> risk.on_fill, forensics, decision journal
_take_profit each tick -> _exit_candidate (bid +0.05 / -0.20) -> FOK sell
settle_loop -> venue outcome -> settle_market -> ledger, earnings, forensics SETTLED
```

## New flow

```
same feeds and books
every 200 ms: MomentumEngine.evaluate
  MARKET_DATA_VALID / MARKET_NOT_STALE       spot age, book age, source agreement
  ENOUGH_TIME_REMAINING                      90..295 s left
  MOMENTUM_DETECTED                          z over 5 s >= 3 sigma, >= 3 spot updates,
                                             consistency >= 0.6, not decelerating
  DIRECTION_CONFIRMED                        not reversing over the last 3 s
  POLYMARKET_TOKEN_VALID                     token mid exists; re-priced 1..5 c over 3 s
                                             (< 1 c: STALE_QUOTE, the dead-quote class;
                                              > 5 c: TOKEN_ALREADY_MOVED)
  ASK_AVAILABLE / SPREAD_ACCEPTABLE          best ask inside the band, spread <= 3 c
  LIQUIDITY_SUFFICIENT                       enough shares under ask + 2 c for the order
  EXPECTED_EDGE_CALCULATED                   expected = mid + continuation(z); exec = VWAP of the asks
  NET_EDGE_ABOVE_THRESHOLD                   expected - exec - fee - slippage - latency >= 2 c
  cooldown / max trades per market
  RISK_CHECK / NO_DUPLICATE_POSITION         RiskManager (micro sizing when micro-live)
  -> marketable limit: FOK at up to ask + 2 c, never chased past it (revalidate)
exits (manage_momentum_position): take-profit +4 c on the bid, stop -6 c,
  momentum reversal (z <= -1.5 against the position), time stop 45 s
```

Every rejection is a `Reason` in `data/decisions.jsonl` and a `SIGNAL_REJECTED_*`
log line; `scripts/why_not_trade.py` prints the session summary.

## What the archive says (scripts/research_momentum.py)

325 windows, 62,185 grid seconds inside the trade window. `net (real ask)` is the
token mid H seconds later minus the ask reachable 2 s after the signal, minus the
venue fee and 0.5 c slippage; rows where that ask had moved more than 2 c are
"chased" and excluded.

| 5 s momentum | rows | windows | value@20s − ask | net@10s | net@20s | net@45s | out-of-sample net@20s |
|---|---:|---:|---:|---:|---:|---:|---:|
| z >= 2 | 4,172 | 325 | +0.4 c | -2.4 c | -1.6 c | -1.8 c | -1.2 c |
| z >= 3 | 1,088 | 272 | +2.2 c | -1.0 c | -0.6 c | -0.7 c | -1.1 c |
| z >= 4 | 191 | 89 | +7.5 c | +0.0 c | +2.3 c | +0.9 c | -0.8 c |

Sub-buckets at z >= 3: token re-priced 1..5 c in 3 s: -1.2 c; 60..120 s left:
+0.7 c (ask near 0.51, win rate 0.53); spread <= 3 c: -0.6 c; SOL was the only
asset with a positive net (+0.7 c, 849 rows, 50 windows).

**Verdict: the archive shows no positive execution-adjusted expectancy for this
strategy at any threshold with a usable sample.** The gross continuation is
real (about 1 c per sigma above 2 at 20 s) but smaller than the fee plus the
spread. The defaults encode the measured continuation and the real costs, so the
engine trades only when a move is far larger than the archive typically shows.
Shadow mode exists to find out whether live books behave differently from the
recorded ones; nothing here claims they do.

## Profiles

`TPB_MOMENTUM_PROFILE=strict` (the default: the archive-calibrated values
below) or `easy` (opt-in, data collection only; it was the default for part of
2026-09-23 and was reverted because its measured net is negative). The easy profile:
2-sigma move over 5 s, no consistency or acceleration requirement, at least 2
spot updates, token re-priced 0.5 to 8 c, continuation 2 c per sigma above 2
(cap 8 c), net edge at least 0.5 c, 60 s or more left, ask at most 0.85,
30 s cooldown, two trades per market. The archive's expected net for this
bucket is about -1.6 c per share at the reachable ask, roughly -0.08 USDC per
5-share micro order. It exists to collect execution data on more fills, not
because the recorded data says it pays. Both profiles refuse asks above
`MAX_ASK` (0.85): 4 of the 5 signals the strict profile found live were at
0.89-0.95, where the token can gain a few cents against nearly the whole stake.

The dashboard's market chart now draws every ask the strategy looked at as a
coloured point: green generated an order, orange low edge, purple the token
had already moved, red a stale quote, blue liquidity, grey anything else.
Hovering a point shows the ask, the expected value, the net edge and the reason.

## Configuration (`TPB_MOMENTUM_*`, strict profile values)

| variable | default | basis |
|---|---:|---|
| LOOKBACK_S | 5 | the window with the clearest separation between buckets |
| Z_THRESHOLD | 3.0 | smallest bucket with positive gross continuation and > 1,000 rows |
| CONSISTENCY_MIN | 0.6 | the median; 0.8 halves the rows with no net gain |
| REQUIRE_ACCELERATION | true | decelerating rows were -0.9 c net (n = 105) |
| REVERSAL_Z | 0.5 | last 3 s against the move |
| MIN_UPDATES | 3 | no trade from one tick |
| MIN_TIME_REMAINING_S / MAX | 90 / 295 | room for the 45 s thesis and an exit |
| MIN_REPRICE / MAX_REPRICE | 0.01 / 0.05 | dead quote below, chased above |
| CONTINUATION_PER_SIGMA / BASE_Z / CAP | 0.01 / 2.0 / 0.05 | measured at 20 s |
| MIN_NET_EDGE | 0.02 | same bar as the analytic engine |
| MAX_SPREAD / MAX_SLIPPAGE | 0.03 / 0.02 | micro-live caps |
| EXPECTED_SLIPPAGE / LATENCY_COST_PER_S | 0.005 / 0.01 | measured reaction lag charged |
| TAKE_PROFIT / STOP_LOSS | 0.04 / 0.06 | provisional: the horizon's typical move and a bounded thesis failure; not tuned |
| MAX_HOLD_S | 45 | the horizon the continuation was measured over |
| EXIT_REVERSAL_Z | 1.5 | provisional |
| COOLDOWN_S / MAX_TRADES_PER_MARKET | 60 / 1 | overtrading protection |

## Running it

```bash
# paper (realistic scenario), separate data files so the analytic paper bot keeps its own
python -m troll_poly_bot --balance 100 --strategy momentum_taker --data-tag momentum

# SHADOW: real account, orders signed and never posted
python -m troll_poly_bot --balance 50 --mode shadow --strategy momentum_taker --data-tag momentum

# MICRO-LIVE: real orders at the micro caps (5 USDC/order, 10 open, 10/day loss, 1 position, 5/hour)
python -m troll_poly_bot --balance 50 --mode micro-live --strategy momentum_taker --data-tag momentum

python scripts/why_not_trade.py --decisions data/decisions.momentum.jsonl --forensics data/trade_forensics.momentum.jsonl
```

`--mode live` is refused for any strategy until `graduation.assess()` passes on
real epochs; the micro caps force the taker path (`TPB_MICRO_MAKER_ENTRY` is
ignored by this engine, which always sends a capped marketable limit).
