// CROSS_MARKET_ARB (strategy/cross_market_arb/) -- PAPER ONLY. Kept out of the other
// ecosystem files so nothing starts it as a side effect:
//
//   pm2 start ecosystem.crossmarketarb.config.js --only tpb-crossmarketarb-paper
//
// Monitors BTC, ETH, SOL, XRP, DOGE, BNB, HYPE 5m UP/DOWN markets and arms a portfolio
// only when min settlement payout over all 128 outcome combinations exceeds all-in
// acquisition cost by more than the configured minimum_net_profit.
//
// __main__ refuses shadow, micro-live and live for this strategy. Tagged files:
// data/*.crossmarketarb-paper.* so it runs beside other bots without sharing state.
//
// enabled defaults false in CrossMarketArbConfig; TPB_CMA_ENABLED=1 is required to start.

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    { name: 'tpb-crossmarketarb-paper',
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
        TPB_CMA_ENABLED: '1',
        TPB_CMA_MODE: 'paper',
        // Live ask_up+ask_down is almost always >= 1.01 after fees (no taker arb).
        // Maker GTC bids on both sides can lock guaranteed profit when bid sum clears the floor.
        TPB_CMA_ENTRY_MODE: 'maker',
        TPB_CMA_MAKER_SHARES: '5',
        TPB_CMA_MAX_MAKER_MARKETS: '3',
        TPB_CMA_MAXIMUM_CONCURRENT_OPPORTUNITIES: '3',
        TPB_CMA_MARKETS: 'BTC,ETH,SOL,XRP,DOGE,BNB,HYPE',
        TPB_CMA_ASSETS: 'BTC,ETH,SOL,XRP,DOGE,BNB,HYPE',
        // Arm only when UP+DOWN maker bids sum to <= 0.97 (lock >= $0.15 on 5sh).
        TPB_CMA_MAXIMUM_MAKER_SUM: '0.97',
        TPB_CMA_MINIMUM_NET_PROFIT: '0.15',
        TPB_CMA_MAXIMUM_BASKET_NOTIONAL: '80',
        TPB_CMA_MAXIMUM_MARKET_EXPOSURE: '40',
        TPB_CMA_MAXIMUM_QUOTE_AGE_MS: '1000',
        TPB_CMA_MAXIMUM_TIMESTAMP_SKEW_MS: '1000',
        TPB_CMA_MAXIMUM_SPREAD: '0.05',
        TPB_CMA_MAXIMUM_SLIPPAGE: '0.01',
        TPB_CMA_MAX_SLIPPAGE: '0.01',
        TPB_CMA_MAX_SPREAD: '0.05',
        TPB_CMA_MAX_QUOTE_AGE_MS: '1000',
        TPB_CMA_MAXIMUM_INCOMPLETE_EXPOSURE: '40',
        TPB_CMA_DAILY_LOSS_LIMIT: '15',
        TPB_CMA_MINIMUM_CASH_RESERVE: '5',
        TPB_CMA_MAXIMUM_CAPITAL_PER_OPPORTUNITY: '80',
        TPB_CMA_MAXIMUM_TOTAL_OUTSTANDING_NOTIONAL: '120',
        TPB_CMA_MAXIMUM_WORST_CASE_LOSS_DURING_EXECUTION: '40',
        TPB_CMA_MIN_TIME_REMAINING_S: '60',
        TPB_CMA_EXECUTION_BUFFER_S: '15',
        TPB_CMA_MAX_SHARES_PER_LEG: '25',
        TPB_CMA_SHARE_STEP: '5',
        TPB_CMA_ORDER_TYPE: 'FAK',
        TPB_CMA_HOLD_TO_SETTLEMENT: '1',
        TPB_CMA_RESIDUAL_EXPOSURE_POLICY: 'hold',
        TPB_CMA_FAST_FEED: '0',
        TPB_CONTROLS_IGNORE: 'risk.max_daily_loss_usdc,risk.max_drawdown_usdc,risk.max_total_exposure_usdc',
      },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario calibrated --strategy cross_market_arb '
          + '--exchanges bybit,coinbase '
          + '--max-order-usdc 50 --max-open-usdc 120 --risk-daily-loss 15 --max-drawdown 20 '
          + '--data-tag crossmarketarb-paper --keep-history --log-level INFO' },
  ],
};
