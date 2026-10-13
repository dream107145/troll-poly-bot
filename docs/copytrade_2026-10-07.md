# Copy-trading PBot-6 (strategy `copytrade`) — 2026-10-07

The user asked (10-07): "Monitor PBot-6's orders. Execute the same orders with him. Do it very quickly."
PBot-6 = wallet `0x21d0a97aac03917e752857a551bbe5103a00e8d7` (see `docs/preopen_maker_2026-10-06.md` for his
decomposition: +8,152 USDC over 10 days, almost all of it from large one-sided bets of 50–400 shares that win
~50–52 % at ~0.46; his 10-share pattern is roughly break-even).

## What is public about his orders

Polymarket publishes a wallet's **fills**, not its resting orders. The only complete feed is
`data-api.polymarket.com/activity?user=<wallet>&type=TRADE` (newest first). `/trades?user=` is a sparse
history (10-07: 1 of 50 fills of the last hour). The feed lags the venue:

| measurement (10-07)                      | lag from fill timestamp to first appearance |
|------------------------------------------|---------------------------------------------|
| standalone probe (`lag_probe.py`), 1 fill| 9.4 s                                       |
| bot poller, 15:10:31 fill (15m market)   | 20.1 s                                      |

So "with him" means **9–20 s after him**, at whatever the ask is then. For his pre-open bids (filled around
T-45 s) the copy still lands before the market opens; for his in-market fills the price has usually moved.

## The bot (paper only)

`src/troll_poly_bot/strategy/copytrade.py`, env prefix `TPB_COPY_*`, flag `TPB_COPY_ENABLED`; `__main__`
refuses shadow, micro-live and live. App `tpb-copy-paper` (`ecosystem.copy.config.js`, data tag `copy-paper`,
100 USDC, calibrated paper fills).

* `WalletPoller` thread polls the activity feed every 0.7 s; the first poll only primes the seen-set, every
  later new row (key tx|asset|size|timestamp) is queued with its arrival time.
* `CopyTradeEngine.pair_requests` (reached every 0.2 s tick through live.py's pair-order hooks; the engine must
  expose `pair_cancels` or live.py returns before `pair_requests`) drains the queue and, for each BUY of his in a
  BTC 5m market we track, sends a **FOK buy at his price + 0.02**, size min(his, 10), if the fill is younger than
  60 s (`TPB_COPY_MAX_AGE_S`; 20 -> 30 -> 60 as the lag measurements came in: 4-31 s; the ask-vs-limit check is the real guard) and the current ask is not above
  the limit. Everything else is counted: MISSED_SELL, MISSED_TYPE (15m / hourly / ETH fills — he trades all of
  them: of his last 500 fills 349 were 5m, 77 15m, 74 hourly), MISSED_MARKET, MISSED_LATE, MISSED_PRICE,
  MISSED_LIMIT (40 USDC open, 4 markets), MISSED_CASH. Copies are held to settlement.
* Snapshot block `copy` (state file `data/live_state.copy-paper.json`): stats (his_fills_seen, copies_sent,
  copies_filled, copies_killed, lag_s_mean, slippage_mean, pnl), misses, poller health (polls, errors, rtt,
  queue, newest_ts), last copies. Log lines `[COPY_POLL]` (every 60th poll or whenever rows are new) and `[COPY]`.

## Backtest before the paper run (recorded books 10-05/06)

Copying his BTC 5m fills 2–10 s late with the 10-share cap lost 0.10–0.96 USDC per market on our recorded
windows. His aligned trades lost there too; the profit in his account comes from the big directional bets the
cap removes. Expectation for this bot: zero to slightly negative. The paper run is the test.

## Debugging log (10-07)

* 13:5x first start: `/trades?user=` sparse → switched to `/activity?type=TRADE`.
* 14:43 restart: engine lacked `pair_cancels`, so live.py never reached `pair_requests` → hook added.
* 15:00 restart with poller instrumentation: poller correct (40 rows/poll, 0 errors, rtt 20–100 ms); PBot-6
  simply made no fill between 14:59:11 and 15:10:31. His 15:10:31 fill (75 DOWN at 0.46, 15m market) was
  queued and consumed 20.1 s late → pipeline verified.
* 15:11 restart: untracked 15m/hourly/ETH fills now MISSED_TYPE (were MISSED_MARKET); max age 30 s.

## Paper results

(filled in below as fills arrive)
