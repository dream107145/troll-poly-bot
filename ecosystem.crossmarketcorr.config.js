// CROSS_MARKET_CORRELATION (strategy/cross_market_correlation/) -- PAPER ONLY. Kept out of the other
// ecosystem files so nothing starts it as a side effect:
//
//   pm2 start ecosystem.crossmarketcorr.config.js --only tpb-crossmarketcorr-paper
//
// Scans 21 unique pairs among BTC/ETH/SOL/XRP/DOGE/BNB/HYPE 5m UP/DOWN (×2 directions = 42 candidates).
// Paper mode enables STATISTICAL trading on cheap combined costs under a prior / book-implied model.
// GUARANTEED_ARBITRAGE still requires the formal worst-case payout proof (correlation alone never qualifies).
//
// __main__ refuses shadow, micro-live and live for this strategy. Tagged files:
// data/*.crossmarketcorr-paper.* so it runs beside other bots without sharing state.

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    { name: 'tpb-crossmarketcorr-paper',
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
        TPB_CMC_ENABLED: '1',
        TPB_CMC_MODE: 'paper',
        TPB_CMC_MARKETS: 'BTC,ETH,SOL,XRP,DOGE,BNB,HYPE',
        TPB_CMC_ASSETS: 'BTC,ETH,SOL,XRP,DOGE,BNB,HYPE',
        TPB_CMC_GUARANTEED_MODE_ENABLED: '1',
        // Paper: trade statistical candidates when combined ask+fees clears the cost ceiling.
        TPB_CMC_STATISTICAL_MODE_ENABLED: '1',
        TPB_CMC_ALLOW_UNDER_SAMPLED_PRIOR: '1',
        // Agreement prior (not book-implied): under thin history EV ≈ 1 - combined_cost.
        TPB_CMC_USE_BOOK_IMPLIED_WHEN_UNDER_SAMPLED: '0',
        TPB_CMC_REQUIRE_STRIKE: '0',
        TPB_CMC_REQUIRE_SPOT: '0',
        // Cost must clear under ≈$1 under the agreement prior (EV ≈ 1 − cost).
        // Floor 0.60: allow cheaper hedges; wipe-skip mode cuts the longshot wipe risk.
        TPB_CMC_MINIMUM_COMBINED_COST: '0.60',
        TPB_CMC_MAXIMUM_COMBINED_COST: '0.98',
        // Skip opposite directions the wipe model flags as likely full losses.
        TPB_CMC_SKIP_PAIRS_MODE_ENABLED: '1',
        TPB_CMC_WIPE_SKIP_THRESHOLD: '0.55',
        TPB_CMC_MINIMUM_CONSERVATIVE_EV: '0.0',
        TPB_CMC_GUARANTEED_MIN_NET_PROFIT: '0.01',
        TPB_CMC_MINIMUM_EXPECTED_NET_PROFIT: '0.0',
        TPB_CMC_MAX_SHARES_PER_MARKET_SIDE: '15',
        TPB_CMC_MAX_NET_SHARES_PER_ASSET: '20',
        TPB_CMC_PAIR_MATCH_COOLDOWN_MS: '30000',
        TPB_CMC_LEADER_LARGE_MIN_PRICE: '0.75',
        TPB_CMC_MIN_SAMPLE_SIZE: '30',
        TPB_CMC_MAX_PROBABILITY_UNCERTAINTY: '1.0',
        TPB_CMC_PRIOR_STRENGTH: '4',
        TPB_CMC_UNCERTAINTY_Z: '0.0',
        TPB_CMC_PRIOR_AGREEMENT_BIAS: '0.70',
        TPB_CMC_SIZE_SHARES: '5',
        TPB_CMC_SHARE_STEP: '5',
        TPB_CMC_MAX_SHARES_PER_LEG: '25',
        TPB_CMC_MAXIMUM_CAPITAL_PER_PAIR: '40',
        TPB_CMC_MAXIMUM_TOTAL_OPEN_EXPOSURE: '120',
        TPB_CMC_MAXIMUM_EXPOSURE_PER_ASSET: '40',
        TPB_CMC_MAXIMUM_EXPOSURE_PER_MARKET: '40',
        TPB_CMC_MAXIMUM_UNHEDGED_EXPOSURE: '25',
        TPB_CMC_MAXIMUM_SIMULTANEOUS_POSITIONS: '6',
        // Paper: peak equity ratchets on wins; a later lose-streak vs that peak
        // used to halt the bot for the rest of the session (drawdown 25).
        TPB_CMC_MAXIMUM_DAILY_REALIZED_LOSS: '200',
        TPB_CMC_MAXIMUM_TOTAL_DRAWDOWN: '100',
        TPB_CMC_DAILY_LOSS_LIMIT: '200',
        TPB_CMC_MINIMUM_CASH_RESERVE: '5',
        // Quiet books keep an old venue ts; 3s was filtering all 7 markets often.
        TPB_CMC_MAXIMUM_QUOTE_AGE_MS: '10000',
        TPB_CMC_MAX_MARKET_DATA_AGE_MS: '10000',
        TPB_CMC_MAXIMUM_TIMESTAMP_SKEW_MS: '8000',
        TPB_CMC_MAXIMUM_SPREAD: '0.20',
        TPB_CMC_MAXIMUM_SLIPPAGE: '0.03',
        TPB_CMC_MINIMUM_BOOK_DEPTH: '5',
        TPB_CMC_MIN_TIME_REMAINING_S: '20',
        TPB_CMC_MAXIMUM_TIME_BEFORE_SETTLEMENT_S: '300',
        TPB_CMC_EXECUTION_BUFFER_S: '8',
        TPB_CMC_MAX_LEG_RETRIES: '20',
        TPB_CMC_LEG_RETRY_MS: '400',
        TPB_CMC_ORDER_TYPE: 'FAK',
        TPB_CMC_HOLD_TO_SETTLEMENT: '1',
        TPB_CMC_UNHEDGED_POLICY: 'cancel_alert',
        TPB_CMC_KILL_SWITCH: '0',
        TPB_CMC_FAST_FEED: '0',
        TPB_CMC_MAX_QUOTE_AGE_MS: '10000',
        TPB_CMC_MAX_SPOT_AGE_MS: '8000',
        TPB_CONTROLS_IGNORE: 'risk.max_daily_loss_usdc,risk.max_drawdown_usdc,risk.max_total_exposure_usdc',
      },
      args: '-m troll_poly_bot --balance 500 --mode paper --scenario calibrated --strategy cross_market_correlation '
          + '--exchanges bybit,coinbase '
          + '--max-order-usdc 40 --max-open-usdc 120 --risk-daily-loss 200 --max-drawdown 100 '
          + '--data-tag crossmarketcorr-paper --keep-history --log-level INFO' },
  ],
};
