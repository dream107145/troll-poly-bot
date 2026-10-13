// SPOT_LAG (strategy/spot_lag.py), kept OUT of ecosystem.config.js on purpose so
// `pm2 start ecosystem.config.js` never starts it as a side effect. Start the stage
// you mean, by name:
//
//   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-paper     # no account, no money
//   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-paper-ma74  # the same, live rule, MAX_ASK 0.74
//   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-shadow    # real account, never posts
//   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-live      # REAL MONEY, micro-live
//
// MULTI-MARKET (2026-09-24): every 5-minute market (asset x window) is its own
// opportunity -- at most one entry per market, several positions at once, and
// every limit GLOBAL across all of them. The budget is explicit below (BUDGET);
// the bot applies the tighter of it and its own risk limits. In micro-live the
// MicroCaps ceilings (TPB_MICRO_*) bound everything as well; tpb-spotlag-live
// raises exactly two of them, the position count and the hourly buys, to the
// budget -- the order cap (5), open exposure (10) and daily loss (10) are unchanged.
//
// tpb-spotlag-live places REAL orders. It needs TPB_SPOT_LAG_ENABLED=1 AND
// TPB_SPOT_LAG_ALLOW_REAL=1 (set below) and TPB_LIVE_ACK in .env. No autorestart:
// a real-money bot that died waits for a human. A dedicated account: the guard
// latches on any trade this process did not place. It writes the DEFAULT files
// (data/live_state.json, data/live_trades.jsonl, data/open_positions.json, ...),
// which the dashboard (tpb-web) shows -- so does tpb-momentum-live: never run both.
// Open positions and today's realised PnL are saved to data/open_positions.json
// and restored on restart (never across paper/live).
//
// tpb-spotlag-paper writes TAGGED files (data/*.spotlag-paper.*) so it can run
// beside the real-money bot for comparison. --keep-history: a restart appends.
//
// Fill model: --scenario venue_lag matches every order against the local book
// 500 ms after it arrived (validated against venue-timestamped books), not the
// 2 s quote-survival rule, which drops exactly this strategy's winners.

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

// the portfolio budget -- global across every market (strategy/spot_lag.py)
const BUDGET = {
  TPB_SPOT_LAG_MAX_SIMULTANEOUS_POSITIONS: '4',   // open positions + working entries
  TPB_SPOT_LAG_MAX_BUYS_PER_HOUR: '60',           // BUY orders sent, killed ones included
  TPB_SPOT_LAG_MAX_ENTRIES_PER_MARKET: '1',       // filled entries per 5-minute market
  TPB_SPOT_LAG_MAX_ATTEMPTS_PER_MARKET: '3',      // BUY orders per market, killed included
  TPB_SPOT_LAG_ORDER_SIZE_USDC: '0',              // 0 = the venue minimum, 5 shares (what was tested)
  TPB_SPOT_LAG_MAX_ORDER_USDC: '5',               // worst case (limit x size) per order
  TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC: '10',     // all open positions + working entries
  TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC: '10',
};

const common = {
  cwd: ROOT,
  interpreter: 'none',
  script: PYTHON,
  autorestart: true,
  min_uptime: '30s',
  max_restarts: 10,
  restart_delay: 5000,
  kill_timeout: 15000,
  merge_logs: true,
  time: true,
  max_memory_restart: '600M',
  env: { PYTHONUNBUFFERED: '1', TPB_SPOT_LAG_ENABLED: '1', ...BUDGET },
};

module.exports = {
  apps: [
    // paper -- live books and spot, the venue's measured order latency, venue_lag fills.
    // EVENT_DRIVEN (2026-09-24): shocks evaluated the moment the quote arrives, not
    // on the 200 ms tick (docs/latency_2026-09-24.md). Paper only, to compare with
    // tpb-spotlag-live (tick-driven) before it goes anywhere near real money.
    // ENTRY, PAPER ONLY (live from 2026-09-24 21:04 UTC, the user's request: more orders and more
    // profitable fills): price limit ask + 4c (still never above the max entry that
    // leaves the edge), minimum net edge 1c (was 2c), spread up to 5c (was 3c). Replay of
    // this bot's gates and budget over five periods (data/replay/rm_research/slsim.md):
    // orders 20 -> 26 / h, fills 5.0 -> 6.9 / h, +8.1c -> +5.1c per share, positive in
    // every period; 16:30-19:10 UTC fills 1.5 -> 3.6 / h. The live and shadow apps keep
    // the tested rule (ask + 2c, 2c edge, 3c spread).
    { ...common, name: 'tpb-spotlag-paper',
      env: { ...common.env, TPB_SPOT_LAG_EVENT_DRIVEN: '1',
             TPB_SPOT_LAG_MAX_SLIPPAGE: '0.04', TPB_SPOT_LAG_MIN_NET_EDGE: '0.01',
             TPB_SPOT_LAG_MAX_SPREAD: '0.05',
             // 2026-09-25 (the user's instruction): no tokens above 0.60. Replay: asks above
             // 0.80 lose in every period, 0.60-0.80 are about flat; with this bot's settings
             // orders 26 -> 17 / h, fills 6.9 -> 4.3 / h, +5.1c -> +10.4c per share
             TPB_SPOT_LAG_MAX_ASK: '0.60' },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy spot_lag '
          + '--data-tag spotlag-paper --keep-history --log-level INFO' },
    // MAX_ASK test, PAPER ONLY (2026-09-25, the user's OK after docs/spot_lag_parameter_sweep.md): the
    // tested rule exactly as tpb-spotlag-live trades it (tick-driven, ask + 2c, 2c edge, 3c spread) with
    // ONE change, the price ceiling 0.95 -> 0.74 (the pick locked on training). Replay at this budget:
    // about 3.9 fills / h and +10.5c per share out of sample (0.95: 4.7 / h, +6.3c).
    // NORMAL CPU priority, like the other bots: its first 40 minutes ran under nice 10 and it fell
    // behind its own feeds (82 "feed behind" warnings vs 9-12 for the other spot-lag bots, loop p99
    // 121 vs ~70 ms) and traded a stale catch-up burst (SOL 01:51:33, -$0.60) the replay never sees.
    // The entry sweep (docs/spot_lag_entry_sweep_2026-09-25.md) re-selected these parameters.
    { ...common, name: 'tpb-spotlag-paper-ma74',
      env: { ...common.env, TPB_SPOT_LAG_MAX_ASK: '0.74' },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy spot_lag '
          + '--data-tag spotlag-ma74 --keep-history --log-level INFO' },
    // EXECUTION-EDGE candidate, PAPER ONLY, NOT started automatically (2026-09-29,
    // docs/spot_lag_fast_2026-09-29.md): exactly the live bot's rule with ONE change the
    // realistic replay supports on TRAIN / VALIDATION / TEST / FORWARD -- SIZE_MODE touch:
    // 10 shares when the book holds 10 under the limit, else what it holds down to 5, instead
    // of refusing the order (+5-10 % net in every period, no extra drawdown). Hold-to-settlement
    // (TPB_SPOT_LAG_HOLD_TO_SETTLEMENT=1) exists too but is NOT used: it doubles the drawdown and
    // loses its edge under the 20 USDC daily stop. Judge this bot by the recorded-book audit
    // (docs/spot_lag_paper_history_2026-09-25.md), not by its paper P&L: the paper venue can
    // fill against a briefly frozen local book.
    //   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-paper-touch
    { ...common, name: 'tpb-spotlag-paper-touch',
      env: { ...common.env, TPB_BOOK_SKIP_ASSETS: 'HYPE', TPB_SPOT_LAG_PROFILE: 'optimized',
             TPB_SPOT_LAG_TAKE_PROFIT_CENTS: '10', TPB_SPOT_LAG_STOP_LOSS_CENTS: '5',
             TPB_SPOT_LAG_HOLD_S: '20', TPB_SPOT_LAG_EXTEND_MAX_HOLD_S: '0',
             TPB_SPOT_LAG_SIZE_SHARES: '10', TPB_SPOT_LAG_SIZE_MODE: 'touch',
             TPB_SPOT_LAG_MAX_ORDER_USDC: '6', TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC: '24',
             TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC: '20' },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy spot_lag '
          + '--max-order-usdc 6 --max-open-usdc 24 --max-daily-loss 20 '
          + '--data-tag spotlag-touch --keep-history --log-level INFO' },
    // FAK entries, PAPER ONLY (2026-09-29, the user's request; docs/fak_2026-09-29.md): the live
    // bot's exact settings with ONE change -- entries are fill-and-kill (IOC): whatever the book
    // holds under the limit when the order lands, up to 10 shares, instead of all-or-nothing.
    // Replay: ~2 % of kills rescued, ~+10 % fills. Judge it by the recorded-book audit, not by its
    // dashboard P&L.   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-paper-fak
    { ...common, name: 'tpb-spotlag-paper-fak',
      env: { ...common.env, TPB_BOOK_SKIP_ASSETS: 'HYPE', TPB_SPOT_LAG_PROFILE: 'optimized',
             TPB_SPOT_LAG_EVENT_DRIVEN: '1',
             TPB_SPOT_LAG_TAKE_PROFIT_CENTS: '10', TPB_SPOT_LAG_STOP_LOSS_CENTS: '5',
             TPB_SPOT_LAG_HOLD_S: '20', TPB_SPOT_LAG_EXTEND_MAX_HOLD_S: '0',
             TPB_SPOT_LAG_SIZE_SHARES: '10', TPB_SPOT_LAG_ORDER_TYPE: 'FAK',
             TPB_SPOT_LAG_MAX_ORDER_USDC: '6', TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC: '24',
             TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC: '20' },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy spot_lag '
          + '--data-tag spotlag-fak --keep-history --log-level INFO' },
    // 2026-09-29 LEAD paper A/B against tpb-spotlag-paper-fak (identical otherwise): the composite
    // spot follows the nearest exchange's quote (Coinbase, ~55 ms from this VPS) instead of the
    // median that waits for a second exchange (~120 ms later at 3-sigma shocks on the day's
    // recording). Judge by shock decision age and fills, then the recorded-book audit.
    //   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-paper-lead
    { ...common, name: 'tpb-spotlag-paper-lead',
      env: { ...common.env, TPB_BOOK_SKIP_ASSETS: 'HYPE', TPB_SPOT_LAG_PROFILE: 'optimized',
             TPB_SPOT_LAG_EVENT_DRIVEN: '1',
             TPB_SPOT_LEAD_WINDOW_MS: '30',   // under the 45 ms Coinbase -> Bybit lag gap
             TPB_SPOT_LAG_TAKE_PROFIT_CENTS: '10', TPB_SPOT_LAG_STOP_LOSS_CENTS: '5',
             TPB_SPOT_LAG_HOLD_S: '20', TPB_SPOT_LAG_EXTEND_MAX_HOLD_S: '0',
             TPB_SPOT_LAG_SIZE_SHARES: '10', TPB_SPOT_LAG_ORDER_TYPE: 'FAK',
             TPB_SPOT_LAG_MAX_ORDER_USDC: '6', TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC: '24',
             TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC: '20' },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy spot_lag '
          + '--data-tag spotlag-lead --keep-history --log-level INFO' },
    // 2026-09-29 FAST FEED paper (docs/fast_feed_2026-09-29.md; NOT started): every raw exchange quote at tick
    // resolution, a detection on the 50-500 ms horizons evaluates the asset at once, and the engine judges the
    // 250 ms return (sqrt-scaled, same k) with the 1 s return agreeing. Otherwise the live bot's settings.
    //   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-paper-fast
    { ...common, name: 'tpb-spotlag-paper-fast',
      env: { ...common.env, TPB_BOOK_SKIP_ASSETS: 'HYPE', TPB_SPOT_LAG_PROFILE: 'optimized',
             TPB_SPOT_LAG_EVENT_DRIVEN: '1', TPB_SPOT_LEAD_WINDOW_MS: '30',
             TPB_SPOT_LAG_FAST_FEED: '1', TPB_SPOT_LAG_SHOCK_WINDOW_S: '0.25',
             TPB_SPOT_LAG_MAX_ASK: '0.70',
             TPB_SPOT_LAG_TAKE_PROFIT_CENTS: '10', TPB_SPOT_LAG_STOP_LOSS_CENTS: '5',
             TPB_SPOT_LAG_HOLD_S: '20', TPB_SPOT_LAG_EXTEND_MAX_HOLD_S: '0',
             TPB_SPOT_LAG_SIZE_SHARES: '10', TPB_SPOT_LAG_ORDER_TYPE: 'FAK',
             TPB_SPOT_LAG_MAX_ORDER_USDC: '6', TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC: '24',
             TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC: '20' },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy spot_lag '
          + '--data-tag spotlag-fast --keep-history --log-level INFO' },
    // 2026-09-30 MULTI-SOURCE feed paper (docs/feed_benchmark_2026-09-30.md; NOT started): the live bot's
    // settings with the composite fed by the sources the 10-minute benchmark showed reaching this VPS
    // first (Deribit perpetual 33 ms, Kraken 38, Bitstamp 46, Coinbase 71; OKX and Bybit linear as
    // confirmation) in lead mode, instead of Coinbase alone. PAPER ONLY: the benchmark had no 5 bp
    // event at all, so nothing here is evidence for real money. Judge by shock_decision_age_ms,
    // fills per order and the recorded-book audit against tpb-spotlag-live over the same hours.
    //   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-paper-multisrc
    { ...common, name: 'tpb-spotlag-paper-multisrc',
      env: { ...common.env, TPB_BOOK_SKIP_ASSETS: 'HYPE', TPB_SPOT_LAG_PROFILE: 'optimized',
             TPB_SPOT_LAG_EVENT_DRIVEN: '1', TPB_SPOT_LEAD_WINDOW_MS: '30',
             TPB_SPOT_LAG_TAKE_PROFIT_CENTS: '10', TPB_SPOT_LAG_STOP_LOSS_CENTS: '5',
             TPB_SPOT_LAG_HOLD_S: '20', TPB_SPOT_LAG_EXTEND_MAX_HOLD_S: '0',
             TPB_SPOT_LAG_SIZE_SHARES: '10', TPB_SPOT_LAG_SIZE_MODE: 'touch', TPB_SPOT_LAG_ORDER_TYPE: 'FAK',
             TPB_SPOT_LAG_MAX_ASK: '0.70', TPB_MICRO_MAX_SPREAD: '0.05',
             TPB_SPOT_LAG_MAX_ORDER_USDC: '6', TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC: '24',
             TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC: '20' },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy spot_lag '
          + '--max-order-usdc 6 --max-open-usdc 24 --max-daily-loss 20 '
          + '--exchanges coinbase,deribit,kraken,bitstamp,okx,bybit_linear --assets BTC '
          + '--data-tag spotlag-multisrc --keep-history --log-level INFO' },
    // shadow -- the same paper fills, plus every order taken through the real
    // account's path (reconciled, signed, pre-post checked) and NEVER posted
    { ...common, name: 'tpb-spotlag-shadow',
      args: '-m troll_poly_bot --balance 50 --mode shadow --scenario venue_lag --strategy spot_lag '
          + '--data-tag spotlag-shadow --keep-history --log-level INFO' },
    // REAL MONEY at the micro-live caps, two of them raised to the budget
    // 2026-09-26 02:40 UTC, the user's instruction ("start live spotlag bot in real money, buy 10
    // shares per order, stop bot if loss reach to $20"): the `optimized` profile
    // (docs/spot_lag_parameter_optimization_2026-09-25.md: MAX_ASK 0.60, quote age <= 1000 ms,
    // spot age <= 500 ms; realistic replay +20 USDC over the 25.5 untouched hours at 5 shares),
    // 10 shares per order (<= 6 USDC at the 0.60 ceiling), 4 x 6 = 24 USDC exposure, and a
    // 20 USDC stop: daily loss AND drawdown from the start balance (data/controls.json, live scope).
    // 2026-10-01 00:20 UTC (the user's instruction: "switch the live bot to paper mode"): the live app's
    // exact settings and sources, paper fills (venue_lag), tagged files data/*.spotlag-paper.*.
    //   pm2 start ecosystem.spotlag.config.js --only tpb-spotlag-paper-mirror
    { ...common, name: 'tpb-spotlag-paper-mirror',
      env: { ...common.env, /* paper: no real-money flag */
             // 2026-09-29 (the user's OK, docs/latency_2026-09-29.md): orders signed and POSTed
             // from 4 parallel threads the moment they are decided, and shocks evaluated the
             // moment the spot quote arrives instead of on the 200 ms tick
             TPB_LIVE_ORDER_LANES: '4',
             TPB_SPOT_LAG_EVENT_DRIVEN: '1',
             // 2026-09-29 17:30 UTC (the user's instruction, "restart with the fastest codebase"):
             // the composite spot follows the nearest exchange's quote (docs/latency_2026-09-29b.md)
             TPB_SPOT_LEAD_WINDOW_MS: '0',    // 10-01 audit: OFF (fast-feed replay: 2.6x signals, -0.03/fill vs +2.01)
             // 2026-09-29 (the user's instruction): FAK entries -- take whatever the book holds under
             // the limit when the order lands, up to 10 shares (docs/fak_2026-09-29.md). Exits stay FOK.
             TPB_SPOT_LAG_ORDER_TYPE: 'FAK',
             // 2026-09-29 (the user's OK, to raise the order count): spreads up to 5c (the real-money
             // micro cap was 3c and overrode the strategy's 5c; replay +7 % orders, profit neutral or
             // slightly better) and tokens up to 0.70 (the profile's 0.60; replay +20 % orders,
             // total profit about flat)
             TPB_SPOT_LAG_MAX_ASK: '0.60',      // 10-01 audit: the locked pick; 0.70 and spread 5c were 'about flat' guesses
             // 2026-09-30 21:16 UTC (the user's instruction, "restart the bot with all 7 assets"): HYPE is
             // traded again -- no book skip, and the strategy's default HYPE exclusion cleared
             TPB_SPOT_LAG_EXCLUDE_ASSETS: 'HYPE', TPB_BOOK_SKIP_ASSETS: 'HYPE',   // 10-01 audit: HYPE loses (09-23 study)
             // 2026-09-30 (the user's instruction, "the bot doesn't detect fast price movement well ... fix
             // it"): also signal when the token's fair value moves 12c in a second without a 3-sigma spot
             // shock (78 % of fast token moves had none; scripts/research_detection.py: +5c vs +2.4c per
             // share at 5 s, DEV and TEST). Dashboard: Spot-lag signal -> Fair-value trigger, 0 = off.
             // 10-01 01:00 UTC: OFF. The newest 18 h turned it negative (-4.9c/share at 20 s on 119 signals) and a
             // 0.8 bp dip 87 s before the close bought XRP DOWN at 0.19 -> 0.04. Keep 0 until re-validated.
             TPB_SPOT_LAG_FV_TRIGGER_CENTS: '0',
             // 2026-10-01 (the user's instruction: "buy only when the crypto price trend is upward"): a shock
             // must continue the 2-minute spot trend. 30 h archive, 1,373 signals, DEV/TEST: against a
             // 120 s downtrend +0.6c/share at 5 s; flat or rising +4 to +9c. 10-60 s windows: no effect.
             TPB_SPOT_LAG_TREND_WINDOW_S: '30', TPB_SPOT_LAG_TREND_MIN_BPS: '0',   // 10-01: 30 s at the user's instruction
             TPB_SPOT_LAG_PROFILE: 'optimized',
             // 2026-09-28 (the user's instruction): price exits. Sell at the bid once it is
             // 10c above the entry, or through the bid once it is 5c below; a position that
             // reaches neither is sold after 20 s (never held to a 1 / 0 settlement).
             // Realistic replay: +98 USDC selection / +4.9 untouched test (timer: +105 / +1.0).
             TPB_SPOT_LAG_TAKE_PROFIT_CENTS: '10',
             TPB_SPOT_LAG_STOP_LOSS_CENTS: '5',
             TPB_SPOT_LAG_HOLD_S: '20',
             TPB_SPOT_LAG_EXTEND_MAX_HOLD_S: '0',           // the spot-momentum extension is off: the cap is the cap
             TPB_SPOT_LAG_SIZE_SHARES: '10',
             TPB_SPOT_LAG_MAX_ORDER_USDC: '6',
             TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC: '24',
             TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC: '20',
             TPB_MICRO_MAX_ORDER_USD: '6',
             TPB_MICRO_MAX_OPEN_EXPOSURE_USD: '24',
             TPB_MICRO_MAX_DAILY_LOSS_USD: '20',
             TPB_MICRO_MAX_CONCURRENT_POSITIONS: '4',     // was 1
             TPB_MICRO_MAX_ORDERS_PER_HOUR: '60' },       // was 5 (BUYs only; sells are never capped)
      // the adapter's own caps are CLI arguments (micro-live only clamps them DOWN): without
      // these, 77 orders of 10 shares at 0.55-0.60 were refused "order 6.00 > max_order_usdc 5.00"
      // 2026-09-30 (the user's instruction): Coinbase is the only spot source (was binance,bybit,
      // coinbase). Fastest feed here (~67 ms) and USD like Chainlink, but thin on BNB / DOGE /
      // HYPE (a live price 14-42 % of the time), whose markets then mostly refuse as stale data.
      // Same day, the user's choice: BTC only. Coinbase's ticker updates on trades only; measured
      // spot age median 0.2 s on BTC vs 0.5-0.75 s on ETH/SOL/XRP/HYPE and 7-53 s on DOGE/BNB.
      // 2026-09-30 ~19:10 UTC (the user's instruction, "you do it"): the composite also takes the three
      // lowest-delay sources of docs/feed_benchmark_2026-09-30.md -- Deribit perpetual (33 ms), Kraken (38),
      // Bitstamp (46) beside Coinbase (71); Coinbase stays the level reference (reference_order). The
      // benchmark had no >= 5 bp event, so this is a live trial, not an evidence-backed change.
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy spot_lag '
          + '--max-order-usdc 6 --max-open-usdc 24 --max-daily-loss 20 '
          // 2026-09-30 21:16 UTC (the user's instruction): all 7 assets. Binance added back as a 5th source:
          // without it BNB had 1 quote in 30 s (Coinbase only; Kraken, Bitstamp and Deribit do not list it)
          // 10-01 audit: the sources and assets every spot-lag study was run on
          + '--exchanges binance,bybit,coinbase --assets BTC,ETH,SOL,XRP,DOGE,BNB '
          + '--data-tag spotlag-paper --keep-history --log-level INFO' },
    { ...common, name: 'tpb-spotlag-live', autorestart: false,
      env: { ...common.env, TPB_SPOT_LAG_ALLOW_REAL: '1',
             // 2026-09-29 (the user's OK, docs/latency_2026-09-29.md): orders signed and POSTed
             // from 4 parallel threads the moment they are decided, and shocks evaluated the
             // moment the spot quote arrives instead of on the 200 ms tick
             TPB_LIVE_ORDER_LANES: '4',
             TPB_SPOT_LAG_EVENT_DRIVEN: '1',
             // 2026-09-29 17:30 UTC (the user's instruction, "restart with the fastest codebase"):
             // the composite spot follows the nearest exchange's quote (docs/latency_2026-09-29b.md)
             TPB_SPOT_LEAD_WINDOW_MS: '0',    // 10-01 audit: OFF (fast-feed replay: 2.6x signals, -0.03/fill vs +2.01)
             // 2026-09-29 (the user's instruction): FAK entries -- take whatever the book holds under
             // the limit when the order lands, up to 10 shares (docs/fak_2026-09-29.md). Exits stay FOK.
             TPB_SPOT_LAG_ORDER_TYPE: 'FAK',
             // 2026-09-29 (the user's OK, to raise the order count): spreads up to 5c (the real-money
             // micro cap was 3c and overrode the strategy's 5c; replay +7 % orders, profit neutral or
             // slightly better) and tokens up to 0.70 (the profile's 0.60; replay +20 % orders,
             // total profit about flat)
             TPB_SPOT_LAG_MAX_ASK: '0.60',      // 10-01 audit: the locked pick; 0.70 and spread 5c were 'about flat' guesses
             // 2026-09-30 21:16 UTC (the user's instruction, "restart the bot with all 7 assets"): HYPE is
             // traded again -- no book skip, and the strategy's default HYPE exclusion cleared
             TPB_SPOT_LAG_EXCLUDE_ASSETS: 'HYPE', TPB_BOOK_SKIP_ASSETS: 'HYPE',   // 10-01 audit: HYPE loses (09-23 study)
             // 2026-09-30 (the user's instruction, "the bot doesn't detect fast price movement well ... fix
             // it"): also signal when the token's fair value moves 12c in a second without a 3-sigma spot
             // shock (78 % of fast token moves had none; scripts/research_detection.py: +5c vs +2.4c per
             // share at 5 s, DEV and TEST). Dashboard: Spot-lag signal -> Fair-value trigger, 0 = off.
             // 10-01 01:00 UTC: OFF. The newest 18 h turned it negative (-4.9c/share at 20 s on 119 signals) and a
             // 0.8 bp dip 87 s before the close bought XRP DOWN at 0.19 -> 0.04. Keep 0 until re-validated.
             TPB_SPOT_LAG_FV_TRIGGER_CENTS: '0',
             // 2026-10-01 (the user's instruction: "buy only when the crypto price trend is upward"): a shock
             // must continue the 2-minute spot trend. 30 h archive, 1,373 signals, DEV/TEST: against a
             // 120 s downtrend +0.6c/share at 5 s; flat or rising +4 to +9c. 10-60 s windows: no effect.
             TPB_SPOT_LAG_TREND_WINDOW_S: '30', TPB_SPOT_LAG_TREND_MIN_BPS: '0',   // 10-01: 30 s at the user's instruction
             TPB_SPOT_LAG_PROFILE: 'optimized',
             // 2026-09-28 (the user's instruction): price exits. Sell at the bid once it is
             // 10c above the entry, or through the bid once it is 5c below; a position that
             // reaches neither is sold after 20 s (never held to a 1 / 0 settlement).
             // Realistic replay: +98 USDC selection / +4.9 untouched test (timer: +105 / +1.0).
             TPB_SPOT_LAG_TAKE_PROFIT_CENTS: '10',
             TPB_SPOT_LAG_STOP_LOSS_CENTS: '5',
             TPB_SPOT_LAG_HOLD_S: '20',
             TPB_SPOT_LAG_EXTEND_MAX_HOLD_S: '0',           // the spot-momentum extension is off: the cap is the cap
             TPB_SPOT_LAG_SIZE_SHARES: '10',
             TPB_SPOT_LAG_MAX_ORDER_USDC: '6',
             TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC: '24',
             TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC: '20',
             TPB_MICRO_MAX_ORDER_USD: '6',
             TPB_MICRO_MAX_OPEN_EXPOSURE_USD: '24',
             TPB_MICRO_MAX_DAILY_LOSS_USD: '20',
             TPB_MICRO_MAX_CONCURRENT_POSITIONS: '4',     // was 1
             TPB_MICRO_MAX_ORDERS_PER_HOUR: '60' },       // was 5 (BUYs only; sells are never capped)
      // the adapter's own caps are CLI arguments (micro-live only clamps them DOWN): without
      // these, 77 orders of 10 shares at 0.55-0.60 were refused "order 6.00 > max_order_usdc 5.00"
      // 2026-09-30 (the user's instruction): Coinbase is the only spot source (was binance,bybit,
      // coinbase). Fastest feed here (~67 ms) and USD like Chainlink, but thin on BNB / DOGE /
      // HYPE (a live price 14-42 % of the time), whose markets then mostly refuse as stale data.
      // Same day, the user's choice: BTC only. Coinbase's ticker updates on trades only; measured
      // spot age median 0.2 s on BTC vs 0.5-0.75 s on ETH/SOL/XRP/HYPE and 7-53 s on DOGE/BNB.
      // 2026-09-30 ~19:10 UTC (the user's instruction, "you do it"): the composite also takes the three
      // lowest-delay sources of docs/feed_benchmark_2026-09-30.md -- Deribit perpetual (33 ms), Kraken (38),
      // Bitstamp (46) beside Coinbase (71); Coinbase stays the level reference (reference_order). The
      // benchmark had no >= 5 bp event, so this is a live trial, not an evidence-backed change.
      args: '-m troll_poly_bot --balance 50 --mode micro-live --scenario venue_lag --strategy spot_lag '
          + '--max-order-usdc 6 --max-open-usdc 24 --max-daily-loss 20 '
          // 2026-09-30 21:16 UTC (the user's instruction): all 7 assets. Binance added back as a 5th source:
          // without it BNB had 1 quote in 30 s (Coinbase only; Kraken, Bitstamp and Deribit do not list it)
          // 10-01 audit: the sources and assets every spot-lag study was run on
          + '--exchanges binance,bybit,coinbase --assets BTC,ETH,SOL,XRP,DOGE,BNB '
          + '--keep-history --log-level INFO' },
  ],
};
