// PREOPEN_MAKER (strategy/preopen_maker.py) -- PAPER ONLY. Its own ecosystem file so nothing starts it as a side effect:
//
//   pm2 start ecosystem.pom.config.js --only tpb-pom-paper
//
// Resting GTC bids on BOTH tokens of the next BTC 5-minute market, placed 45 s before its open and pulled at the
// open, held to settlement (the pattern of wallet PBot-6; the user's brief of 2026-10-06; the replay in
// docs/preopen_maker_2026-10-06.md). No taker orders, no re-pricing, no spot signal. The paper venue fills a resting
// bid only when the ask trades STRICTLY THROUGH it (a pessimistic maker model: no queue position is known), so this
// bot's fills are a lower bound on what the venue would give.
//
// There is no shadow or real-money app here and none can be made: __main__ refuses micro-live, live and shadow for
// this strategy whatever the environment says.
// It writes TAGGED files (data/*.pom-paper.*) so it runs beside the other bots without touching their state.

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

const POM = {
  TPB_POM_ENABLED: '1',
  TPB_POM_BID_PRICE: '0.47',
  TPB_POM_QUANTITY: '10',
  TPB_POM_PLACEMENT_S: '45',             // seconds before the open
  TPB_POM_CANCEL_AFTER_S: '0',           // unfilled bids pulled at the open
  TPB_POM_MARKET_TYPE: '5m',
  TPB_POM_MIN_PAIR_EDGE: '0',
  // inventory
  TPB_POM_MAX_TOTAL_OPEN_EXPOSURE_USDC: '40',
  TPB_POM_MAX_UNMATCHED_SHARES: '40',
  TPB_POM_MAX_UNMATCHED_USDC: '20',
  TPB_POM_MAX_MARKETS_WITH_OPEN_POSITIONS: '4',
  TPB_POM_MAX_DAILY_LOSS_USDC: '20',
  TPB_POM_ASSETS: 'BTC',
};

module.exports = {
  apps: [
    { name: 'tpb-pom-paper',
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
      env: { PYTHONUNBUFFERED: '1', ...POM },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario calibrated --strategy preopen_maker'
          + ' --exchanges binance,bybit,coinbase --assets BTC --data-tag pom-paper --keep-history --log-level INFO',
    },
  ],
};
