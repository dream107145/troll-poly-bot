// COPYTRADE (strategy/copytrade.py) -- PAPER ONLY. Follows one Polymarket wallet's published fills (data-api
// /activity?user=&type=TRADE, the complete newest-first feed; /trades?user= is a sparse history) a few seconds after they
// happen and buys the same token at his price + a small premium, FOK, held to settlement (the user, 2026-10-07:
// "monitor PBot-6's orders, execute the same orders with him, quickly").
//
//   pm2 start ecosystem.copy.config.js --only tpb-copy-paper
//
// The venue publishes a wallet's FILLS, not its resting orders, so every copy is late by the data API's delay (measured
// 2026-10-07; see docs/copytrade_2026-10-07.md) and pays the ask at that moment. Backtest on our recorded books: copying
// PBot-6's BTC fills 2-10 s late with a 10-share cap lost 0.1-1.0 USDC a market -- the paper run is the test.
// There is no shadow or real-money app here and none can be made: __main__ refuses micro-live, live and shadow.
// It writes TAGGED files (data/*.copy-paper.*).

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

const COPY = {
  TPB_COPY_ENABLED: '1',
  TPB_COPY_WALLET: '0x21d0a97aac03917e752857a551bbe5103a00e8d7',   // PBot-6
  TPB_COPY_POLL_S: '0.7',
  TPB_COPY_SIZE_CAP: '10',
  TPB_COPY_SIZE_MODE: 'match',          // min(his size, cap)
  TPB_COPY_MAX_PREMIUM: '0.02',         // our limit = his price + 2c
  TPB_COPY_MAX_AGE_S: '60',             // a fill seen later than this is not copied (data API lag measured 4-31 s on 10-07; the ask check is the real guard)
  TPB_COPY_MARKET_TYPES: '5m',
  TPB_COPY_ASSETS: 'BTC',
  TPB_COPY_COPY_SELLS: '0',
  TPB_COPY_MAX_TOTAL_OPEN_EXPOSURE_USDC: '40',
  TPB_COPY_MAX_MARKETS_WITH_OPEN_POSITIONS: '4',
  TPB_COPY_MAX_DAILY_LOSS_USDC: '20',
};

module.exports = {
  apps: [
    { name: 'tpb-copy-paper',
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
      env: { PYTHONUNBUFFERED: '1', ...COPY },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario calibrated --strategy copytrade'
          + ' --exchanges binance,bybit,coinbase --assets BTC --data-tag copy-paper --keep-history --log-level INFO',
    },
  ],
};
