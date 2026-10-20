// LEADER_PAIR cross-crypto mode -- PAPER ONLY. For BTC, ETH, SOL, XRP, DOGE, and BNB, the executable ask of the
// matching outcome in the other five markets is averaged. From the open through 4:00: when that average is >0.88,
// buy 5 of the local matching-outcome leader while its ask is 0.30-0.60; after that leader's actual fill price rises by
// $0.30, buy 5 of its opposite token. Independently, when the other-five matching-outcome average is >0.90 and the
// local ask is strictly 0.50-0.60, buy 5 of that token. Every fill is held to settlement; no new buy starts in the
// final minute. Existing hard caps remain: 150 USDC a market and 500 account-wide.
//
//   pm2 start ecosystem.leaderpair.config.js --only tpb-leaderpair-paper
//
// __main__ refuses shadow, micro-live, and live until cost-inclusive OOS results are positive. Tagged files:
// data/*.leaderpair-paper.*, state data/leader_pair_state.leaderpair-paper.json

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    { name: 'tpb-leaderpair-paper',
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
        TPB_LP_ENABLED: '1',
        TPB_LP_ASSETS: 'BTC,ETH,SOL,XRP,DOGE,BNB',
        TPB_LP_CROSS_MARKET_ENABLED: '1',
        TPB_LP_CROSS_PEER_COUNT: '5',
        TPB_LP_CROSS_START_S: '0',
        TPB_LP_CROSS_END_S: '240',
        TPB_LP_CROSS_LEADER_SHARES: '5',
        TPB_LP_CROSS_LEADER_MIN_ASK: '0.30',
        TPB_LP_CROSS_LEADER_MAX_ASK: '0.60',
        TPB_LP_CROSS_PEER_LEADER_AVG: '0.88',
        TPB_LP_CROSS_PAIR_RISE: '0.30',
        TPB_LP_CROSS_PAIR_SHARES: '5',
        TPB_LP_CROSS_PEER_SIGNAL_AVG: '0.90',
        TPB_LP_CROSS_SIGNAL_MIN_ASK: '0.50',
        TPB_LP_CROSS_SIGNAL_MAX_ASK: '0.60',
        TPB_LP_CROSS_SIGNAL_SHARES: '5',
        // rule 1
        TPB_LP_INIT_SHARES: '10',
        TPB_LP_LEADER_RULE: 'mid',
        TPB_LP_INIT_DELAY_S: '5',
        TPB_LP_INIT_MAX_S: '60',
        TPB_LP_INIT_MIN_ASK: '0.50',
        TPB_LP_INIT_MAX_ASK: '0.90',
        // rule 2
        TPB_LP_PAIR_THRESHOLD: '0.95',
        // 10-14 the user: "if buying a certain token can make pairs under 0.95 in total, calculate the amount and buy it;
        // if the locked PnL is over $5, lock buying" (lp.pair_buy_enabled / lp.lock_target_usdc on the page)
        TPB_LP_PAIR_BUY_ENABLED: '1',
        TPB_LP_PAIR_BUY_COOLDOWN_S: '2',
        TPB_LP_LOCK_TARGET_USDC: '5',
        // rule 3
        TPB_LP_ACCUM_INTERVAL_S: '5',
        TPB_LP_ACCUM_SHARES: '5',
        TPB_LP_OPP_CEILING: '0.50',
        TPB_LP_ACCUM_EVAL: 'slot_start',
        // rules 4 / 5
        TPB_LP_LEADER_REVERSAL: '0.50',
        TPB_LP_SIGNAL_DELTA_S: '5',
        TPB_LP_FALL_MIN_DROP: '0.01',
        TPB_LP_FALL_CONFIRM_TICKS: '3',
        TPB_LP_RECOVERY_MIN_RISE: '0.01',
        TPB_LP_RECOVERY_CONFIRM_TICKS: '3',
        TPB_LP_FALLING_BUY: 'improve_worst',
        TPB_LP_FALLING_SHARES: '5',
        TPB_LP_FALLING_OPP_MAX_ASK: '0.65',
        TPB_LP_RECOVERY_SHARES: '5',
        TPB_LP_RECOVERY_INTERVAL_S: '5',
        TPB_LP_RECOVERY_MAX_ASK: '0.95',
        TPB_LP_LIQ_SLIPPAGE: '0.01',
        // 10-13 21:xx the user: "if the price is in 0.6-0.8 and the settlement PnL of the leader is negative, buy leader
        // tokens and make the settlement PnL +$5. Do not change the lower-price token purchase behaviour" -> the favourite
        // fix, before every other rule (lp.fix_* on the Variables page)
        TPB_LP_FIX_ENABLED: '1',
        // 10-14 the user: "do not buy high price tokens ... before 210 s left or above 0.85; buy only when the token rises
        // from 0.5 to 0.8 (+5); if the lower token's price is low, reduce the worst-case loss by buying lower tokens, but
        // do not make the best-case PnL under +2"
        TPB_LP_FIX_MIN_ASK: '0.50',
        TPB_LP_FIX_MAX_ASK: '0.80',
        TPB_LP_FIX_TARGET_USDC: '5',
        TPB_LP_FIX_COOLDOWN_S: '2',
        TPB_LP_FIX_MAX_LEFT_S: '210',
        TPB_LP_FIX_REQUIRE_RISING: '1',
        TPB_LP_FIX_RISE_WINDOW_S: '5',
        TPB_LP_FIX_MIN_RISE: '0.01',
        TPB_LP_HEDGE_ENABLED: '1',
        TPB_LP_HEDGE_MAX_ASK: '0.20',
        TPB_LP_HEDGE_MIN_BEST_USDC: '2',
        // All orders are taker-only fill-and-kill orders at a strict limit.
        TPB_LP_EXEC_MODE: 'taker',
        // hard limits (section 11)
        TPB_LP_MAX_MARKET_CAPITAL_USDC: '150',
        TPB_LP_MAX_TOTAL_CAPITAL_USDC: '500',
        TPB_LP_MAX_PURCHASES_PER_CYCLE: '6',
        TPB_LP_MAX_PURCHASES_PER_MARKET: '20',
        TPB_LP_MAX_SETTLEMENT_LOSS_USDC: '20',
        TPB_LP_MAX_SPREAD: '0.03',
        TPB_LP_MAX_SLIPPAGE: '0.02',
        TPB_LP_MIN_DEPTH_SHARES: '5',
        TPB_LP_MIN_TIME_REMAINING_S: '20',
        TPB_LP_MAX_BOOK_AGE_MS: '2500',
        // the shared budget the risk manager sees
        TPB_LP_MAX_TOTAL_EXPOSURE_USDC: '500',
        TPB_LP_MAX_DAILY_LOSS_USDC: '150',
        TPB_LP_MAX_ORDER_USDC: '150',
        TPB_LP_MAX_SIMULTANEOUS_POSITIONS: '6',
        // this bot's own loss limits, not the Settings page's shared paper rows
        TPB_CONTROLS_IGNORE: 'risk.max_drawdown_usdc,risk.max_daily_loss_usdc',
      },
      args: '-m troll_poly_bot --balance 500 --mode paper --scenario calibrated --strategy leader_pair --assets BTC,ETH,SOL,XRP,DOGE,BNB '
          + '--max-order-usdc 150 --max-open-usdc 500 --max-daily-loss 150 --risk-daily-loss 150 --max-drawdown 300 '
          + '--data-tag leaderpair-paper --keep-history --log-level INFO' },
  ],
};
