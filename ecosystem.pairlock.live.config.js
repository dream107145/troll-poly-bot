// tpb-pairlockbtc-live: the BTC pair-lock CONDITIONS strategy of tpb-pairlock-paper on the REAL account (the user,
// 2026-10-08: "start the live bot with the same parameters and strategy").
//
//   pm2 start ecosystem.pairlock.live.config.js --only tpb-pairlockbtc-live
//
// The env and args are the paper app's (ecosystem.pairlock.config.js, tpb-pairlock-paper) -- the same strategy code and the
// same parameters; the dashboard's pairbtc.* rows (scope "all") steer both bots alike -- with:
//   --mode micro-live          real orders, inside the micro-live ceilings below (full --mode live stays refused for pair-lock R)
//   TPB_PAIRLOCK_ALLOW_REAL=1  the explicit real-money switch for pair-lock R (__main__.check_triangle)
//   TPB_MICRO_*                the ceilings (the same as tpb-pairlockrecovery-live); the CLI caps are the same numbers
// Own data: data/*.pairlock-btc-live.* (a live run never resets its trade log). Credentials: .env (TPB_POLY_*).
// Do NOT run it beside tpb-pairlockrecovery-live: both trade the same wallet.
// The app name must not share a prefix with another app: pm2 --only matches by prefix.

const paper = require('./ecosystem.pairlock.config.js').apps.find((a) => a.name === 'tpb-pairlock-paper');
if (!paper) throw new Error('tpb-pairlock-paper not found');

const MODE = 'micro-live';

const LIVE = {
  TPB_PAIRLOCK_ALLOW_REAL: '1',
  TPB_LIVE_RECONCILE_S: '5',             // the venue's cash / positions re-read every 5 s (manual trading synced)
  TPB_MICRO_MAX_ORDER_USD: '60',
  TPB_MICRO_MAX_OPEN_EXPOSURE_USD: '100',
  TPB_MICRO_MAX_DAILY_LOSS_USD: '20',       // 2026-10-08 21:30 UTC the user: daily loss 20 (was 120)
  TPB_MICRO_MAX_ORDERS_PER_HOUR: '240',
  TPB_MICRO_MAX_CONCURRENT_POSITIONS: '4',
  TPB_MICRO_MAKER_ENTRY: '0',            // the buys are fill-and-kill takers (the opposite bids are the strategy's own GTC pair orders)
  TPB_MICRO_MAX_SLIPPAGE: '0.03',
  TPB_MICRO_MAX_SPREAD: '0.05',
  TPB_TRIANGLE_MAX_DAILY_LOSS_USDC: '20',  // the strategy's own daily-loss budget (the dashboard cannot lower it): 20 like the caps
  TPB_PAIRLOCK_REC_OPEN_SHARES: '5',     // 2026-10-08 the user: 5 shares per buy (the dashboard's pairbtc.open_shares is 5 too)
  TPB_CONTROLS_IGNORE: '',               // the dashboard's risk rows (scope "live") apply in full
};

const args = paper.args
  .replace('--mode paper --scenario calibrated', `--mode ${MODE}`)
  .replace('--data-tag pairlock-btc-paper', '--data-tag pairlock-btc-live')
  .replace(' --keep-history', '')
  + ' --max-order-usdc 60 --max-open-usdc 100 --max-daily-loss 20 --max-orders-per-hour 240';
if (args === paper.args || !args.includes(`--mode ${MODE}`) || !args.includes('pairlock-btc-live')) {
  throw new Error('could not derive the live args from the paper app');
}

module.exports = {
  apps: [
    { ...paper, name: 'tpb-pairlockbtc-live', env: { ...paper.env, ...LIVE }, args },
  ],
};
