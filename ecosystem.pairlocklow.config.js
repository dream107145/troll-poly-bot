// tpb-pairlocklow-paper: the LOW-PRICE FIRST LEG + DYNAMIC PARTIAL RECOVERY mode of the pair-lock recovery engine
// (the user's spec, 2026-10-07; strategy/pairlock_recovery.py low_entry_side / calculate_recovery_quantity /
// recovery_target_mode). A separate paper app with its own data tag: the recovery paper bot and the conditions bot
// keep their rules.
//
//   pm2 start ecosystem.pairlocklow.config.js --only tpb-pairlocklow-paper
//
// Env and args are the recovery paper bot's (ecosystem.pairlockrecovery.config.js) with the overrides below:
//   first leg   entry mode "low": the CHEAPER token, ask in LOW_PRICE_MIN..MAX, 295..120 s left, spread <= 0.05, the
//               10-share VWAP within 0.03 of the ask, the pair-lock model's EV/share >= LOW_MIN_EDGE (P(side) -
//               VWAP - fee - measured slippage - adverse selection), >= LOW_MIN_SIGNALS of the 4 flow signals toward
//               the side, notional <= LOW_MAX_NOTIONAL. No pair bid.
//   recovery    RECOVERY_TARGET_MODE break_even: once the other token is the higher price (>= RECOVERY_MIN_PRICE) and
//               the first leg sits >= MIN_PRICE_MOVE under its cost, buy the minimum opposite quantity that lifts the
//               worse outcome to >= 0 at the executable VWAP (x = (0 - branch) / (1 - VWAP - fee - slip); the dearer
//               the opposite token, the MORE shares), in steps that must each raise the worse outcome by >=
//               MIN_PNL_IMPROVEMENT; shares already held count; caps: order 60, capital 90, position 300, loss limits
//               35 / lock under 60 s. No even-out, no late ladder, no late buy, no profit exit, no recovery sell: the
//               four effects of the spec's item 21 are measured separately.
// Dashboard: TPB_CONTROLS_IGNORE 'pairlock.*' -- the Settings page's shared pairlock.* rows (the recovery bots' even-out,
// ladder, leader band ...) are NOT applied to this bot; its parameters are this file's. Risk/engine rows still apply.
// Paper only: never a live variant without the user.

const paper = require('./ecosystem.pairlockrecovery.config.js').apps.find((a) => a.name === 'tpb-pairlockrecovery-paper');
if (!paper) throw new Error('tpb-pairlockrecovery-paper not found');

const LOW = {
  TPB_CONTROLS_IGNORE: 'pairlock.*',
  TPB_PAIRLOCK_REC_ENTRY_MODE: 'low',
  TPB_PAIRLOCK_REC_LOW_PRICE_MIN: '0.05',
  TPB_PAIRLOCK_REC_LOW_PRICE_MAX: '0.45',
  TPB_PAIRLOCK_REC_LOW_MIN_EDGE: '0.02',
  TPB_PAIRLOCK_REC_LOW_MAX_SPREAD: '0.05',
  TPB_PAIRLOCK_REC_LOW_MAX_SLIPPAGE: '0.03',
  TPB_PAIRLOCK_REC_LOW_MIN_LEFT_S: '120',
  TPB_PAIRLOCK_REC_LOW_MAX_LEFT_S: '295',
  TPB_PAIRLOCK_REC_LOW_REQUIRE_FLOW_SIGNAL: '1',
  TPB_PAIRLOCK_REC_LOW_MIN_SIGNALS: '2',
  TPB_PAIRLOCK_REC_LOW_MAX_NOTIONAL: '10',
  TPB_PAIRLOCK_REC_LOW_MAX_TRIES: '3',
  TPB_PAIRLOCK_REC_OPEN_SHARES: '10',
  TPB_PAIRLOCK_REC_OPEN_LIMIT_OFFSET: '0.02',
  TPB_PAIRLOCK_REC_PAIR_BID: '0',
  // the recovery
  TPB_PAIRLOCK_REC_RECOVERY_TARGET_MODE: 'break_even',
  TPB_PAIRLOCK_REC_PARTIAL_TARGET: '0.5',
  TPB_PAIRLOCK_REC_MIN_PNL_IMPROVEMENT: '0.05',
  TPB_PAIRLOCK_REC_RECOVERY_MIN_PRICE: '0.55',   // the spec's RECOVERY_OPPOSITE_MIN_PRICE
  TPB_PAIRLOCK_MIN_PRICE_MOVE: '0.01',           // the first leg must sit this far under its cost before a recovery
  TPB_PAIRLOCK_REC_MAX_RECOVERIES: '0',
  TPB_PAIRLOCK_REC_EVEN_OUT_FROM: '0',
  TPB_PAIRLOCK_REC_LADDER_WINDOW_S: '0',
  TPB_PAIRLOCK_REC_LATE_ENABLED: '0',
  TPB_PAIRLOCK_REC_PROFIT_EXIT: '0',
  TPB_PAIRLOCK_REC_RECOVERY_SELL: '0',
  TPB_PAIRLOCK_REC_EDGE_GATE: '0',
  TPB_PAIRLOCK_REC_STEP_FILL: '1',
};

const args = paper.args.replace('--data-tag pairlock-recovery-paper', '--data-tag pairlock-low-paper');
if (args === paper.args) throw new Error('could not derive the low-price args from the paper app');

module.exports = {
  apps: [
    { ...paper, name: 'tpb-pairlocklow-paper', env: { ...paper.env, ...LOW }, args },
  ],
};
