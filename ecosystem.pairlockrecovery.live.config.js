// tpb-pairlockrecovery-live: the recovery bot's strategy, logic and parameters on the REAL account (the user,
// 2026-10-05 17:30 UTC: "start the live bot in the same strategy and logic, parameters").
//
//   pm2 start ecosystem.pairlockrecovery.live.config.js --only tpb-pairlockrecovery-live
//
// The env and args are the paper recovery bot's (ecosystem.pairlockrecovery.config.js), with:
//   --mode shadow      MODE below. shadow: connects to the real account, builds and signs every order through the
//                      real-money path, never posts one (a dry run of the real order path on the live book).
//                      micro-live: posts them. Full --mode live stays refused for this strategy.
//   TPB_PAIRLOCK_ALLOW_REAL=1   the explicit real-money switch for pair-lock R (__main__.check_triangle)
//   TPB_MICRO_*                 the micro-live ceilings, set to what the strategy needs (a 10-share buy at <= 0.95 is
//                               9.5 USDC; a recovery step up to 60 shares; 90 USDC of recovery capital per market);
//                               the CLI caps below are the same numbers (micro-live clamps them to these ceilings)
// Own data: data/*.pairlock-recovery-live.* (a live run never resets its trade log). Credentials: .env (TPB_POLY_*).
// The paper bot (tpb-pairlockrecovery-paper) keeps running beside it for comparison.
// The app name must not share a prefix with another app: pm2 --only matches by prefix.

const paper = require('./ecosystem.pairlockrecovery.config.js').apps.find((a) => a.name === 'tpb-pairlockrecovery-paper');
if (!paper) throw new Error('tpb-pairlockrecovery-paper not found');

//: 'shadow' (nothing posted) or 'micro-live' (real orders, the ceilings below)
const MODE = 'micro-live';   // 2026-10-05 the user: "start with real orders"

const LIVE = {
  TPB_PAIRLOCK_ALLOW_REAL: '1',
  TPB_LIVE_RECONCILE_S: '5',             // the venue's cash / positions / trades re-read every 5 s (manual trading synced)
  TPB_MICRO_MAX_ORDER_USD: '60',
  TPB_MICRO_MAX_OPEN_EXPOSURE_USD: '100',
  TPB_TRIANGLE_MAX_DAILY_LOSS_USDC: '120', // the risk manager's daily-loss limit (the triangle budget); same figure as the micro ceiling below
  TPB_MICRO_MAX_DAILY_LOSS_USD: '120',   // 2026-10-06 01:15 the user: a new daily-loss cap after the 60 halted the bot at -61.26
  TPB_MICRO_MAX_ORDERS_PER_HOUR: '240',
  TPB_MICRO_MAX_CONCURRENT_POSITIONS: '4',
  TPB_MICRO_MAKER_ENTRY: '0',            // this strategy's buys are fill-and-kill takers
  TPB_MICRO_MAX_SLIPPAGE: '0.03',
  TPB_MICRO_MAX_SPREAD: '0.05',
  // 2026-10-06 the paper bot's new rules (even out from recovery 2, the late ladder) stay OFF on real money until the user says
  TPB_PAIRLOCK_REC_EVEN_OUT_FROM: '0',
  TPB_PAIRLOCK_REC_LADDER_WINDOW_S: '0',
  // 2026-10-07 the paper bot's SWING MODE is paper only (__main__ refuses it on real money anyway) -- and the budget the
  // paper bot was given for it (100 / 150 / 300 USDC, balance 300) stays off this account: the live values as before
  TPB_PAIRLOCK_SWING_MODE: '0',
  TPB_PAIRLOCK_REC_SWING_MODE: '0',
  TPB_TRIANGLE_MAX_ORDER_USDC: '60',
  TPB_TRIANGLE_MAX_MARKET_USDC: '100',
  TPB_TRIANGLE_MAX_TOTAL_EXPOSURE_USDC: '100',
  TPB_CONTROLS_IGNORE: '',               // the paper bot's drawdown exemption (2026-10-07) is not for real money: the dashboard's applies
};

const args = paper.args
  .replace(/--balance \S+/, '--balance 100')       // the live bankroll figure as before the paper bot's 2026-10-07 change
  .replace('--mode paper --scenario calibrated', `--mode ${MODE}`)
  .replace('--data-tag pairlock-recovery-paper --keep-history', '--data-tag pairlock-recovery-live')
  + ' --max-order-usdc 60 --max-open-usdc 100 --max-daily-loss 120 --max-orders-per-hour 240';
if (args === paper.args || !args.includes(`--mode ${MODE}`) || !args.includes('pairlock-recovery-live')) {
  throw new Error('could not derive the live args from the paper app');
}

module.exports = {
  apps: [
    { ...paper, name: 'tpb-pairlockrecovery-live', env: { ...paper.env, ...LIVE }, args },
  ],
};
