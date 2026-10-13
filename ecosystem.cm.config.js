// BTC_CONFIRMATION_MOMENTUM (strategy/confirmation_momentum.py) -- PAPER ONLY. Kept in its own ecosystem file so
// nothing starts it as a side effect:
//
//   pm2 start ecosystem.cm.config.js --only tpb-cm-paper
//
// Waits for a confirmed BTC spot move (1/5/15 s z-scores, 2 of 3 exchanges agreeing, no retrace past half the
// impulse), requires the leading token to have followed it, and buys that token in small independent clips held to
// settlement (the user's brief, 2026-10-06; docs/confirmation_momentum_2026-10-06.md). The final-30-second LOTTERY
// is a separate flag (TPB_CM_LOTTERY_ENABLED), off here; its P&L is kept apart in the engine's statistics.
//
// There is no shadow or real-money app here and none can be made: __main__ refuses micro-live, live and shadow for
// this strategy whatever the environment says. Enabling it for real money is a separate, explicit decision.
//
// Parameters = the replay's recommendation (docs/confirmation_momentum_2026-10-06.md, section 9): zones 2-3 only, one clip,
// the rule estimator with the measured zone-2 bonus, no adverse-selection charge, no edge floor.
// Paper fills use the CALIBRATED scenario (fitted on 193 real orders: the ask must be inside the limit at arrival
// and still inside it 1 s later; a resting bid fills only when the far side trades strictly through it).
// It writes TAGGED files (data/*.cm-paper.*) so it runs beside the other bots without touching their state.

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

const CM = {
  TPB_CM_ENABLED: '1',
  // timing (s after the open / before the close)
  TPB_CM_MIN_ELAPSED_S: '45',
  TPB_CM_MAX_ELAPSED_S: '210',
  TPB_CM_MIN_REMAINING_S: '30',
  // spot
  TPB_CM_MAX_SPOT_AGE_MS: '500',
  TPB_CM_EXCHANGES: 'binance,coinbase,bybit',
  TPB_CM_AGREEMENT_HORIZON_S: '5',
  // momentum (z-scores of the 1 / 5 / 15 s returns against the realised per-second volatility)
  TPB_CM_Z_1S_MIN: '2.0',
  TPB_CM_Z_5S_MIN: '2.0',
  TPB_CM_Z_15S_MIN: '1.5',
  TPB_CM_MIN_EXCHANGE_AGREEMENT: '2',
  TPB_CM_CONTRADICTION_Z: '1.0',
  // continuation
  TPB_CM_RETRACE_WINDOW_S: '15',
  TPB_CM_MAX_RETRACE_FRACTION: '0.50',
  // token confirmation and price zones
  TPB_CM_TOKEN_CONFIRM_CENTS: '5',
  TPB_CM_TOKEN_CONFIRM_S: '15',
  TPB_CM_ZONE_MIN: '0.65',                 // replay: zone 1 (0.50-0.65) has no edge
  TPB_CM_ZONE1_MAX: '0.65',
  TPB_CM_ZONE2_MAX: '0.85',
  TPB_CM_ZONE3_MAX: '0.93',
  TPB_CM_ZONE2_CONFIRM_MULT: '1.5',
  TPB_CM_ZONE3_CONFIRM_MULT: '2.0',
  // probability and edge (rule-based estimator unless data/replay/cm_model.json exists)
  TPB_CM_MODEL_PATH: 'data/replay/cm_model.NOT_USED.json',   // the fitted model is kept for offline comparison only
  TPB_CM_P_BONUS_BASE: '0.08',             // the measured zone-2 edge (win rate - ask, every split)
  TPB_CM_P_BONUS_PER_Z: '0.01',
  TPB_CM_P_BONUS_CAP: '0.08',
  TPB_CM_MIN_NET_EDGE: '0.00',
  TPB_CM_SLIPPAGE_ALLOWANCE: '0.01',
  TPB_CM_ADVERSE_SELECTION: '0.00',       // measured: filled clips win as often as candidates
  TPB_CM_LATENCY_PENALTY: '0.005',
  // execution
  TPB_CM_ORDER_TYPE: 'FAK',
  TPB_CM_MAX_SLIPPAGE: '0.02',
  TPB_CM_MAX_CHASE_CENTS: '3',
  TPB_CM_MAX_SPREAD: '0.03',
  TPB_CM_MIN_DEPTH_SHARES: '10',
  // sizing and clips
  TPB_CM_BASE_ORDER_USDC: '3',
  TPB_CM_MAX_ORDER_USDC: '5',
  TPB_CM_MAX_MARKET_EXPOSURE_USDC: '10',
  TPB_CM_MAX_ENTRIES_PER_MARKET: '1',     // extra clips added nothing in the replay
  TPB_CM_MIN_NEW_SIGNAL_CENTS: '4',
  // risk
  TPB_CM_ASSETS: 'BTC',
  TPB_CM_MAX_SIMULTANEOUS_POSITIONS: '3',
  TPB_CM_MAX_BUYS_PER_HOUR: '60',
  TPB_CM_MAX_TOTAL_EXPOSURE_USDC: '20',
  TPB_CM_MAX_DAILY_LOSS_USDC: '10',
  // the separate lottery experiment: OFF
  TPB_CM_LOTTERY_ENABLED: '0',
  TPB_CM_LOTTERY_MAX_RISK_USDC: '2',
  TPB_CM_LOTTERY_MIN_PRICE: '0.01',
  TPB_CM_LOTTERY_MAX_PRICE: '0.03',
  TPB_CM_LOTTERY_MAX_REMAINING_S: '30',
};

module.exports = {
  apps: [
    { name: 'tpb-cm-paper',
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
      env: { PYTHONUNBUFFERED: '1', ...CM },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario calibrated --strategy confirmation_momentum'
          + ' --exchanges binance,bybit,coinbase --assets BTC --data-tag cm-paper --keep-history --log-level INFO',
    },
  ],
};
