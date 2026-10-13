// CHEAPSIDE (strategy/cheapside.py) -- PAPER ONLY. Kept out of the other ecosystem
// files so nothing starts it as a side effect:
//
//   pm2 start ecosystem.cheapside.config.js --only tpb-cheapside-paper
//
// Buys the cheaper token of a 5-minute market (ask 0.05-0.35) when the fitted model
// (data/replay/cheapside_model.json, scripts/research_cheapside.py) says it is worth at
// least 5c more than it costs after fees, and holds it to settlement. No take-profit, no
// stop-loss. The research (docs/cheapside_2026-09-29.md) is positive on every archive
// period but its profit sits almost entirely in the first minute of each window; this
// paper run exists to check that on live books, and its fills must be judged by the
// recorded-book audit, not by its dashboard P&L.
//
// There is no shadow or real-money app here and none can be made: __main__ refuses
// micro-live, live and shadow for this strategy whatever the environment says.
//
// It writes TAGGED files (data/*.cheapside-paper.*) so it runs beside the other bots
// without touching their state, ledger or statistics.

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    { name: 'tpb-cheapside-paper',
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
      env: {
        PYTHONUNBUFFERED: '1',
        TPB_CHEAPSIDE_ENABLED: '1',
        // the research rules (the config's defaults; spelled out so a change is visible here)
        TPB_CHEAPSIDE_MIN_ASK: '0.05',
        TPB_CHEAPSIDE_MAX_ASK: '0.35',
        TPB_CHEAPSIDE_MAX_SPREAD: '0.03',
        // 2026-10-07 the user: more orders and fills (the 10-06 run: 15 orders, 1 fill, 14 fok_unfillable). Edge 5c -> 3c,
        // the limit 1c -> 3c above the ask (the engine's paper cap of 2c still binds), fill-and-kill instead of all-or-nothing
        // 2026-10-08: edge back to the backtest's 5c. The 10-07/08 run scored on settled outcomes: 3c broke even
        // (-0.6c/share over 376 markets), 5c made +7.1c/share (140 markets); skipped signals lost ~2c/share
        TPB_CHEAPSIDE_MIN_NET_EDGE: '0.05',
        TPB_CHEAPSIDE_MAX_SLIPPAGE: '0.03',
        TPB_CHEAPSIDE_SIZE_SHARES: '10',
        TPB_CHEAPSIDE_ORDER_TYPE: 'FAK',             // partial fills kept (the venue minimum 5 sh); was FOK
        TPB_CHEAPSIDE_REQUIRE_SUPPORT: '1',
        TPB_CHEAPSIDE_HOLD_TO_SETTLEMENT: '1',
        // budget: one position per market, at most 5 USDC each, 4 markets at once
        TPB_CHEAPSIDE_MAX_ENTRIES_PER_MARKET: '1',
        TPB_CHEAPSIDE_MAX_ATTEMPTS_PER_MARKET: '3',
        TPB_CHEAPSIDE_MAX_ORDER_USDC: '5',
        TPB_CHEAPSIDE_MAX_SIMULTANEOUS_POSITIONS: '8',   // 2026-10-07: was 4 / 20 / 10 (7 assets signal together)
        TPB_CHEAPSIDE_MAX_TOTAL_EXPOSURE_USDC: '40',
        TPB_CHEAPSIDE_MAX_BUYS_PER_HOUR: '60',
        TPB_CHEAPSIDE_MAX_DAILY_LOSS_USDC: '20',
        TPB_CHEAPSIDE_ASSETS: 'BTC,ETH,SOL,XRP,DOGE,BNB,HYPE',
        // the fast feed gives the 50 / 100 ms diagnostics; the model does not read them. OFF since 2026-10-07: with it
        // and 7 assets the bot was a "slow consumer" of the Polymarket book feed (108 drops, 46 FEED_DOWN stops in 2.4 h)
        TPB_CHEAPSIDE_FAST_FEED: '0',
        // 2026-10-08: the drawdown limit is this bot's own (--max-drawdown below), not the Settings page's paper row
        // (30 USDC for every paper bot). It measures from the all-time peak and never resets, so on 10-07 21:23 one
        // losing hour (peak 138.6 -> 99.5) halted the bot for good: 15 h without an order
        TPB_CONTROLS_IGNORE: 'risk.max_drawdown_usdc',
      },
      // scenario: calibrated (the paper venue fitted on 193 real orders, the one the other paper bots run); was venue_lag
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario calibrated --strategy cheapside '
          + '--max-order-usdc 5 --max-open-usdc 40 --max-daily-loss 20 --max-drawdown 80 '
          + '--data-tag cheapside-paper --keep-history --log-level INFO' },
  ],
};
