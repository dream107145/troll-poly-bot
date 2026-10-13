// LATE_CONFIDENCE (strategy/late_confidence.py) -- PAPER ONLY. The user's spec, 2026-10-08 ("Late High-Confidence
// Recovery"): in the last 30 s of every 5-minute crypto market, buy the token priced above 0.98 when the settling
// TWAP is >= 2 standard deviations past the strike (volatility-adjusted) and the TWAP model's P(win) beats the limit
// + fee + a safety margin; 20 shares, fill-and-kill, one per market, two markets at once, held to resolution, no
// stop-loss, never more than the cash. Not backtested (the spec's price x time-left x distance sweep is not run).
//
//   pm2 start ecosystem.lateconf.config.js --only tpb-lateconf-paper
//
// __main__ refuses shadow, micro-live and live for this strategy whatever the environment says.
// Tagged files: data/*.lateconf-paper.*

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    { name: 'tpb-lateconf-paper',
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
        TPB_LATECONF_ENABLED: '1',
        // the entry (the spec)
        TPB_LATECONF_MIN_ASK: '0.95',            // the winning token's ask must be ABOVE this (2026-10-08 14:02 the user: 0.98 -> 0.95)
        TPB_LATECONF_MAX_ASK: '0.999',
        TPB_LATECONF_MAX_TIME_REMAINING_S: '60',   // 2026-10-08 14:20 the user: 30 s -> 1 min
        TPB_LATECONF_MIN_TIME_REMAINING_S: '2',
        TPB_LATECONF_MIN_Z: '2',                 // settling-TWAP distance from the strike, in standard deviations
        TPB_LATECONF_SAFETY_MARGIN: '0.005',     // P(win) - limit - fee >= this
        TPB_LATECONF_SPREAD_IN_EDGE: '0',        // 1 = the spec's literal rule; impossible on a 1-cent book (see the module)
        TPB_LATECONF_MAX_SPREAD: '0.03',
        TPB_LATECONF_MAX_SLIPPAGE: '0.002',      // limit = ask + 0.2c, capped at MAX_ASK, never under the ask (10-08: was 1c
                                                 // capped at 1 - the listed 0.01 tick, which sat under 0.999 asks)
        TPB_LATECONF_SIZE_SHARES: '20',
        TPB_LATECONF_ORDER_TYPE: 'FAK',
        TPB_LATECONF_HOLD_TO_SETTLEMENT: '1',
        // budget: 40 USDC bankroll, 20 shares (~19.8 USDC) a position, two at once, one per market
        TPB_LATECONF_MAX_ENTRIES_PER_MARKET: '1',
        TPB_LATECONF_MAX_ATTEMPTS_PER_MARKET: '3',
        TPB_LATECONF_MAX_ORDER_USDC: '20',
        TPB_LATECONF_MAX_SIMULTANEOUS_POSITIONS: '2',
        TPB_LATECONF_MAX_TOTAL_EXPOSURE_USDC: '40',
        TPB_LATECONF_MAX_BUYS_PER_HOUR: '60',
        TPB_LATECONF_MAX_DAILY_LOSS_USDC: '40',
        TPB_LATECONF_ASSETS: 'BTC,ETH,SOL,XRP,DOGE,BNB,HYPE',
        TPB_LATECONF_FAST_FEED: '0',
        // this bot's own drawdown / daily-loss limits (the bankroll, below), not the Settings page's shared paper rows
        TPB_CONTROLS_IGNORE: 'risk.max_drawdown_usdc,risk.max_daily_loss_usdc',
      },
      args: '-m troll_poly_bot --balance 40 --mode paper --scenario calibrated --strategy late_confidence '
          + '--max-order-usdc 20 --max-open-usdc 40 --max-daily-loss 40 --max-drawdown 40 '
          + '--data-tag lateconf-paper --keep-history --log-level INFO' },
  ],
};
