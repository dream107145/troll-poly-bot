// CHEAPSIDE (strategy/cheapside.py) -- PAPER ONLY. Kept out of the other ecosystem
// files so nothing starts it as a side effect:
//
//   pm2 start ecosystem.cheapside.config.js --only tpb-cheapside-paper
//
// Monitors the seven configured crypto 5m markets. When the six cheapest outcomes average under $0.14,
// buys 5 shares of each and the remaining market's leader, sized to recover the six cheap legs' worst case.
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
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_ENABLED: '1',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MARKET_COUNT: '7',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_BUY_COUNT: '6',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_AVERAGE_ASK: '0.14',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_SHARES: '5',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_TARGET_PNL_USDC: '0.10',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MIN_LEADER_ASK: '0.55',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_LEADER_ASK: '0.78',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_ABORT_LEADER_ASK: '0.82',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_LEADER_UNIT_COST: '0.81',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_ARM_MIN_TIME_REMAINING_S: '120',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_QUOTE_SKEW_MS: '1000',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_CASH_BUFFER_USDC: '1',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_CASH_UTILIZATION: '0.95',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_NOTIONAL_USDC: '80',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_LEADER_NOTIONAL_USDC: '60',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_LEADER_MULTIPLE: '15',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_ALL_LOSE_USDC: '80',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_ORPHAN_USDC: '40',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_MAX_CONCURRENT_EXPOSURE_USDC: '120',
        TPB_CHEAPSIDE_PORTFOLIO_BASKET_CHEAP_RETRY_MS: '250',
        // Forward checks found the 0.05-0.20 bands unreliable; keep the model-selected cheap side at 0.20+.
        TPB_CHEAPSIDE_MIN_ASK: '0.20',
        // Require the model edge and spot confirmation. The fallback that traded despite failed gates lost badly
        // in research and was responsible for recent negative-edge fills.
        TPB_CHEAPSIDE_FORCE_ONE_PER_MARKET: '0',
        // Keep the researched cheap-side ceiling and tight-book limit after negative forward results.
        TPB_CHEAPSIDE_MAX_ASK: '0.35',
        TPB_CHEAPSIDE_MAX_SPREAD: '0.03',
        // Allow new model entries through the first three minutes (120-300 s left); the historical
        // review supports entries with at least 120 s remaining. Portfolio repair keeps its own wider window.
        TPB_CHEAPSIDE_MAX_TIME_REMAINING_S: '300',
        TPB_CHEAPSIDE_MIN_TIME_REMAINING_S: '120',
        TPB_CHEAPSIDE_PORTFOLIO_MIN_TIME_REMAINING_S: '30',
        // 2026-10-07 the user: more orders and fills (the 10-06 run: 15 orders, 1 fill, 14 fok_unfillable). Edge 5c -> 3c,
        // the limit 1c -> 3c above the ask (the engine's paper cap of 2c still binds), fill-and-kill instead of all-or-nothing
        // 2026-10-08: edge back to the backtest's 5c. The 10-07/08 run scored on settled outcomes: 3c broke even
        // (-0.6c/share over 376 markets), 5c made +7.1c/share (140 markets); skipped signals lost ~2c/share
        TPB_CHEAPSIDE_MIN_NET_EDGE: '0.05',
        TPB_CHEAPSIDE_MAX_SLIPPAGE: '0.01',
        TPB_CHEAPSIDE_SIZE_SHARES: '5',                // venue minimum; helps thin books pass the fillability check
        TPB_CHEAPSIDE_ORDER_TYPE: 'FAK',             // partial fills kept (the venue minimum 5 sh); was FOK
        TPB_CHEAPSIDE_REQUIRE_SUPPORT: '1',
        // The independently triggered leader entry was not part of the validated cheap-side strategy.
        TPB_CHEAPSIDE_LEADER_BUY_ENABLED: '0',
        TPB_CHEAPSIDE_LEADER_MIN_WIN_PROBABILITY: '0.85',
        TPB_CHEAPSIDE_LEADER_MIN_ASK: '0.50',
        TPB_CHEAPSIDE_LEADER_MAX_ASK: '0.70',
        TPB_CHEAPSIDE_HOLD_TO_SETTLEMENT: '1',
        // Cheap legs keep the $5 clip; the recovery leader may size above it to cover the cheap basket's worst case.
        TPB_CHEAPSIDE_PORTFOLIO_REPAIR_TARGET_USDC: '2',
        TPB_CHEAPSIDE_PORTFOLIO_REPAIR_COOLDOWN_S: '2',
        TPB_CHEAPSIDE_MAX_ENTRIES_PER_MARKET: '1', // one model entry; recovery buys are separately risk-limited
        TPB_CHEAPSIDE_MAX_ATTEMPTS_PER_MARKET: '3',
        TPB_CHEAPSIDE_MAX_ORDER_USDC: '5',
        TPB_CHEAPSIDE_MAX_SIMULTANEOUS_POSITIONS: '14',  // two seven-market epochs can overlap while prior windows settle
        // Total, per-asset, and per-epoch exposure ceilings are disabled for the basket; available paper balance binds.
        TPB_CHEAPSIDE_MAX_TOTAL_EXPOSURE_USDC: '1000000000',
        TPB_CHEAPSIDE_MAX_DAILY_LOSS_USDC: '0',
        TPB_CHEAPSIDE_ASSETS: 'BTC,ETH,SOL,XRP,DOGE,BNB,HYPE',
        // the fast feed gives the 50 / 100 ms diagnostics; the model does not read them. OFF since 2026-10-07: with it
        // and 7 assets the bot was a "slow consumer" of the Polymarket book feed (108 drops, 46 FEED_DOWN stops in 2.4 h)
        TPB_CHEAPSIDE_FAST_FEED: '0',
        // 2026-10-08: the drawdown limit is this bot's own (--max-drawdown below), not the Settings page's paper row
        // (30 USDC for every paper bot). It measures from the all-time peak and never resets, so on 10-07 21:23 one
        // losing hour (peak 138.6 -> 99.5) halted the bot for good: 15 h without an order
        TPB_CONTROLS_IGNORE: 'risk.max_daily_loss_usdc,risk.max_drawdown_usdc,risk.max_total_exposure_usdc',
      },
      // scenario: calibrated (the paper venue fitted on 193 real orders, the one the other paper bots run); was venue_lag
      // Binance repeatedly trips the global feed guard with a backlogged websocket. Bybit supplies all configured
      // assets; Coinbase stays as a second source. Removing Binance lets the paper strategy act on healthy books.
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario calibrated --strategy cheapside '
          + '--exchanges bybit,coinbase '
          + '--max-order-usdc 5 --max-open-usdc 1000000000 --risk-daily-loss 0 --max-drawdown 15 '
          + '--data-tag cheapside-paper --keep-history --log-level INFO' },
  ],
};
