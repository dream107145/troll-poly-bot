// PENNY BOT (paper): rest 10-share bids at $0.01 on both tokens of each BTC 5m market and hold fills to settlement.
// Uses the existing preopen maker order path; a $0 daily risk limit disables that halt for this paper bot.

const path = require('path');
const ROOT = __dirname;

module.exports = {
  apps: [
    {
      name: 'tpb-penny-paper',
      cwd: ROOT,
      interpreter: 'none',
      script: path.join(ROOT, '.venv', 'bin', 'python'),
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
        TPB_CONTROLS_IGNORE: 'risk.max_daily_loss_usdc',
        TPB_POM_ENABLED: '1',
        TPB_POM_BID_PRICE: '0.01',
        TPB_POM_QUANTITY: '10',
        TPB_POM_PLACEMENT_S: '300',
        TPB_POM_CANCEL_AFTER_S: '300',
        TPB_POM_MARKET_TYPE: '5m',
        TPB_POM_MAX_TOTAL_OPEN_EXPOSURE_USDC: '25',
        TPB_POM_MAX_UNMATCHED_SHARES: '1000',
        TPB_POM_MAX_UNMATCHED_USDC: '25',
        TPB_POM_MAX_MARKETS_WITH_OPEN_POSITIONS: '100',
        TPB_POM_ASSETS: 'BTC',
      },
      args: '-m troll_poly_bot --balance 25 --mode paper --scenario calibrated --strategy preopen_maker'
        + ' --risk-daily-loss 0 --exchanges binance,bybit,coinbase --assets BTC'
        + ' --data-tag penny-paper --keep-history --log-level INFO',
    },
  ],
};
