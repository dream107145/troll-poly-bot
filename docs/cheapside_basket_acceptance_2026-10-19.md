# Cheapside 7-Market Basket — Acceptance Report (2026-10-19)

## Implementation plan (executed)

1. Keep `CheapSideEngine._portfolio_basket` as the single execution path (no parallel bot/engine).
2. Extract pure math into `strategy/cheapside_basket.py` (sizing, 128-scenario PnL, preflight).
3. Add configurable preflight/risk knobs; expose via `control.py` + `ecosystem.cheapside.config.js`.
4. Freeze the armed plan; reserve hedge cash; abort new cheap legs on hedge/time/depth/risk failure.
5. Explicit basket state machine + structured events; restart restore is idempotent.
6. Unit/integration tests + synthetic scenario replay (archive lacks 7-market depth).

## Changed files

| File | Change |
|------|--------|
| `src/troll_poly_bot/strategy/cheapside_basket.py` | **New** — state labels, preflight, sizing, 128-scenario settlement |
| `src/troll_poly_bot/strategy/cheapside.py` | Basket state machine, gates, reservation, abort/orphan, logging |
| `src/troll_poly_bot/control.py` | Dashboard tunables + coherence warnings for favourite band |
| `ecosystem.cheapside.config.js` | Defaults aligned to spec (0.55–0.78, abort 0.82, arm 120s, target $0.10, risk caps) |
| `tests/test_cheapside_basket.py` | **New** — sizing/fees/settlement/preflight/state/abort/restart |
| `tests/test_cheapside.py` | Updated basket expectations; FAK/IOC assertions matched to engine |
| `scripts/replay_cheapside_basket.py` | **New** — A/B/C scenario replay |
| `data/replay/cheapside_basket_replay.json` | Replay output |
| `docs/cheapside_basket_acceptance_2026-10-19.md` | This report |

## Configuration defaults

| Knob | Default |
|------|---------|
| `portfolio_basket_max_average_ask` | 0.20 (signal) |
| `portfolio_basket_shares` | 5 |
| `portfolio_basket_target_pnl_usdc` | 0.10 |
| `portfolio_basket_min_leader_ask` / `max` | 0.55 / 0.78 |
| `portfolio_basket_abort_leader_ask` | 0.82 |
| `portfolio_basket_max_leader_unit_cost` | 0.80 |
| `portfolio_basket_arm_min_time_remaining_s` | 120 |
| `portfolio_basket_max_quote_skew_ms` | 1000 |
| `portfolio_basket_cash_buffer_usdc` | 1.0 |
| `portfolio_basket_max_cash_utilization` | 0.95 |
| `portfolio_basket_max_notional_usdc` | 80 |
| `portfolio_basket_max_leader_notional_usdc` | 60 |
| `portfolio_basket_max_leader_multiple` | 15 |
| `portfolio_basket_max_all_lose_usdc` | 80 |
| `portfolio_basket_max_orphan_usdc` | 40 |
| `portfolio_basket_max_concurrent_exposure_usdc` | 120 |
| `portfolio_basket_cheap_retry_ms` | 250 |
| `portfolio_basket_orphan_policy` | `hold` (no auto-dump) |
| `portfolio_min_time_remaining_s` | 30 (leader recovery only) |

## Mathematical assumptions

- All-in unit cost = limit + worst taker fee on probes in `[ask, limit]` (and 0.50 if spanned).
- Leader size: `ceil_cents((cheap_cost + target − already_pnl) / (1 − unit))`, venue min 5 shares.
- **Protected scenario** = all six cheap sides lose AND favourite wins. This is **not** a guaranteed hedge.
- **All-seven-lose** = all six cheap lose AND favourite loses; loss ≈ total acquisition cost.
- Settlement helper enumerates all \(2^7 = 128\) outcome combinations for a complete basket.
- Fees use the venue `FeeSchedule` (`rate * (p(1−p))^exponent`).
- Orphan policy retains filled cost; does not market-sell at any price.

## Test results

```
.venv/bin/python -m pytest tests/test_cheapside_basket.py tests/test_cheapside.py -q --noconftest
34 passed in ~1.0s
```

Note: full `conftest.py` session setup scans large `data/*.jsonl` (can hang under load). Tests were run with `--noconftest` after verifying logic via direct imports; production bots were not stopped.

## Historical / scenario replay

**Limitation:** `data/replay/` has `btc5m_books.pkl` only — no synchronized seven-market depth/FAK tape. Replay is synthetic with realistic fees, executable asks, partial FAK fills, skew/stale/cash/depth/blow-off cases.

| Variant | Armed | Complete | Aborted | Orphans | Orders | Net PnL | Worst loss | Max DD | Capital peak |
|---------|------:|---------:|--------:|--------:|-------:|--------:|-----------:|-------:|-------------:|
| A baseline (loose gates) | 6 | 1 | 0 | 0 | 37 | −41.36 | −26.69 | 41.36 | 26.69 |
| B preflight only | 4 | 0 | 1 | 1 | 24 | −11.48 | −3.19 | 11.48 | 3.19 |
| C full (gates+abort) | 4 | 0 | 1 | 1 | 21 | −9.89 | −3.19 | 9.89 | 3.19 |

Blow-off scenario (favourite 0.70 → 0.86 mid-basket):

| Variant | Result | Settlement PnL |
|---------|--------|----------------|
| A | COMPLETE (chased leader) | −26.69 |
| B | ORPHAN after more cheap fills | −3.19 |
| C | ORPHAN earlier (cheap-leg abort) | −1.59 |

**Honest verdict:** Under these stress scenarios the strategy remains **unprofitable in aggregate**. Gates reduce trade count and bound blow-off losses; they do not create edge. Do not enable live trading from this work.

## Acceptance criteria PASS/FAIL

| # | Criterion | Verdict |
|---|-----------|---------|
| 1 | Preserve existing 7-market basket strategy / infra | **PASS** |
| 2A | Favourite ask 0.55–0.78; unit ≤ 0.80; executable asks+fees | **PASS** |
| 2B | Cash/hedge feasibility, reserve, depth, round-up sizing | **PASS** |
| 2C | Arm / new cheap-leg cutoff default 120s, configurable | **PASS** |
| 2D | Book quality: both books, age ≤1000ms, spread ≤$0.03, skew | **PASS** |
| 2 | Avg cheap ask &lt; 0.20 signal retained | **PASS** |
| 3 | Lock plan; one armed basket; no reshuffle | **PASS** |
| 4 | FAK cheap legs, retry ~250ms, hedge monitor, abort, immediate leader path | **PASS** |
| 5 | State machine + orphan/restart idempotent; no auto-dump | **PASS** |
| 6 | Risk limits on order path (not dashboard-only) | **PASS** |
| 7 | 128-scenario settlement; protected ≠ all-seven-lose | **PASS** |
| 8 | Replay A/B/C with realistic fees; archive limitation documented | **PASS** (synthetic) |
| 9 | Config/dashboard tunables + structured events | **PASS** |
| 10 | Modify in place; tests run; no live auto-enable | **PASS** |
| — | Strategy profitable after realistic costs | **FAIL** (not claimed) |

## Unresolved risks

1. No multi-asset historical book archive → live fill quality unknown.
2. Orphan `hold` policy leaves unhedged settlement risk until market resolve.
3. Paper risk ceilings are still largely disabled for basket recovery (`max_total_exposure_usdc` lifted); basket-specific caps are the real binders.
4. Leader recovery after partial cheap fills can still leave large orphan notionals if abort fires late.
5. Concurrent second-epoch baskets are blocked while one is active; overlapping settlement epochs rely on plan expiry/cleanup.

## Live trading

Not started. Ecosystem remains paper-only; `__main__` still refuses real-money modes for Cheapside.
