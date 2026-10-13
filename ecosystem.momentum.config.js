// MOMENTUM_TAKER, three stages, kept OUT of ecosystem.config.js on purpose:
// `pm2 start ecosystem.config.js` must never start a real-money momentum bot
// as a side effect. Nothing moves between stages on its own -- start exactly
// the one you mean, by name:
//
//   pm2 start ecosystem.momentum.config.js --only tpb-momentum-paper    # STAGE 1
//   pm2 start ecosystem.momentum.config.js --only tpb-momentum-shadow   # STAGE 2
//   pm2 start ecosystem.momentum.config.js --only tpb-momentum-live     # STAGE 3 (real money)
//
// Each stage writes its own tagged files (data/*.momentum-<stage>.jsonl) so the
// report can compare the SAME signals across stages:
//   python scripts/momentum_report.py --tag momentum-paper --tag momentum-shadow --tag ""

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

const common = {
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
  env: { PYTHONUNBUFFERED: '1' },
};

module.exports = {
  apps: [
    // STAGE 1: realistic paper -- live books, the venue's measured order latency, quote survival
    { ...common, name: 'tpb-momentum-paper',
      args: '-m troll_poly_bot --balance 50 --mode paper --scenario realistic --strategy momentum_taker '
          + '--data-tag momentum-paper --keep-history --log-level INFO' },
    // STAGE 2: shadow -- realistic paper fills, plus every order taken through the
    // real account's path (reconciled, signed, pre-post checked) and NEVER posted
    { ...common, name: 'tpb-momentum-shadow',
      args: '-m troll_poly_bot --balance 50 --mode shadow --strategy momentum_taker '
          + '--data-tag momentum-shadow --keep-history --log-level INFO' },
    // STAGE 3: micro-live -- REAL orders at the MicroCaps ceilings (5 USDC/order,
    // 1 position, 10/day loss, 5 orders/hour). Needs TPB_LIVE_ACK in .env and a
    // DEDICATED account: the guard latches on any trade this process did not place.
    // No data tag: it writes the default data/live_state.json, which is the file
    // the dashboard (tpb-web) shows. No autorestart: a real-money bot that died
    // waits for a human (`pm2 logs tpb-momentum-live`, then `pm2 restart`).
    { ...common, name: 'tpb-momentum-live', autorestart: false,
      args: '-m troll_poly_bot --balance 50 --mode micro-live --strategy momentum_taker '
          + '--keep-history --log-level INFO' },
  ],
};
