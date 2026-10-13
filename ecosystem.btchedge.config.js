// BTC_TREND_REVERSAL_HEDGE (strategy/btc_hedge.py) -- PAPER ONLY. Kept out of the other
// ecosystem files so nothing starts it as a side effect:
//
//   pm2 start ecosystem.btchedge.config.js --only tpb-btchedge-paper
//
// One mandatory 5-share buy in every BTC 5-minute market on the short-term BTC direction (the
// cheaper ask when there is no trend), a hedge of the other side after a confirmed reversal
// only when the pair costs <= MAX_PAIR_COST, everything held to settlement. Research:
// docs/btc_hedge_2026-09-30.md. No shadow or real-money app exists and none can be made:
// __main__ refuses micro-live, live and shadow for this strategy. Tagged files
// (data/*.btchedge-paper.*).

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    { name: 'tpb-btchedge-paper',
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
        TPB_BTC_HEDGE_ENABLED: '1',
        TPB_BTC_HEDGE_BTC_ONLY: '1',
        TPB_BTC_HEDGE_REQUIRE_INITIAL_TRADE: '1',
        TPB_BTC_HEDGE_INITIAL_ENTRY_SIZE: '5',
        TPB_BTC_HEDGE_INITIAL_OBSERVATION_SECONDS: '10',
        TPB_BTC_HEDGE_MAX_PAIR_COST: '0.97',            // see data/replay/btc_hedge_locked.json
        TPB_BTC_HEDGE_MAX_INITIAL_ENTRY_PRICE: '0.65',
        TPB_BTC_HEDGE_REVERSAL_CONFIRMATION_SECONDS: '3',
        TPB_BTC_HEDGE_MAX_HEDGE_SIZE: '0',              // 0 = the unmatched first-leg quantity
        TPB_BTC_HEDGE_HOLD_MATCHED_TO_SETTLEMENT: '1',
        TPB_BTC_HEDGE_HEDGE_MODE: 'gated',              // gated | unconditional | none
        TPB_BTC_HEDGE_TREND_Z: '1.0',
        TPB_BTC_HEDGE_INITIAL_ATTEMPTS: '8',
        TPB_BTC_HEDGE_INITIAL_FAK_AFTER: '3',
        TPB_BTC_HEDGE_ADD_WHILE_TRENDING: '0',
        TPB_BTC_HEDGE_MAX_SIMULTANEOUS_POSITIONS: '2',
        TPB_BTC_HEDGE_MAX_TOTAL_EXPOSURE_USDC: '40',
        TPB_BTC_HEDGE_MAX_ORDER_USDC: '10',
        TPB_BTC_HEDGE_MAX_DAILY_LOSS_USDC: '20',
      },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy btc_trend_reversal_hedge '
          + '--assets BTC --data-tag btchedge-paper --keep-history --log-level INFO' },
  ],
};
