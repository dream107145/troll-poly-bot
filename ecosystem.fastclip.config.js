// FASTCLIP (strategy/fastclip.py) -- PAPER ONLY. Kept out of the other ecosystem files so
// nothing starts it as a side effect:
//
//   pm2 start ecosystem.fastclip.config.js --only tpb-fastclip-paper
//
// The x-moneyforwhiskas pattern (docs/trader_x-moneyforwhiskas_2026-09-29.md) at our speed:
// BTC 5-minute markets only; FAK clips of up to 16 shares / $7 on whichever side the spot is
// moving toward, while the token's ask is below the value the move implies; both sides allowed;
// every clip held to settlement. The rule is the research lock (data/replay/fastclip_locked.json).
// The research (docs/fastclip_2026-09-30.md) found it about break-even at our measured latency
// (test -9 %, forward +1.9 % of volume) and clearly positive only at zero latency: the paper
// venue's fills use the same venue_lag model, so this app checks the plumbing and the signal
// rate, not the speed question.
//
// There is no shadow or real-money app here and none can be made: __main__ refuses micro-live,
// live and shadow for this strategy whatever the environment says. It writes TAGGED files
// (data/*.fastclip-paper.*) so it runs beside the other bots without touching them.

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    // 2026-10-01 (the user's brief): BTC momentum -> reversal PAIRS under the existing app name, PAPER ONLY
    // (strategy/btc_hedge.py: first leg on a strong 5 s BTC signal, the other side after a confirmed
    // reversal when first + ask + fee + slippage <= max_pair_cost, up to 3 pairs per market, everything
    // held to settlement). Parameters = data/replay/btc_pairs_locked.json (chosen on TRAIN+VALIDATION):
    // train +40.9, validation +4.4, TEST -19.5, FORWARD -71.6 USDC (results/btc_pairs_backtest.md) -- no
    // out-of-sample edge; this app measures the live plumbing and real paper fills, nothing more.
    //   pm2 start ecosystem.fastclip.config.js --only tpb-fastclip-paper
    { name: 'tpb-fastclip-paper',
      cwd: ROOT, interpreter: 'none', script: PYTHON, autorestart: true, min_uptime: '30s', max_restarts: 10,
      restart_delay: 5000, kill_timeout: 15000, merge_logs: true, time: true, max_memory_restart: '600M',
      env: {
        PYTHONUNBUFFERED: '1',
        TPB_BTC_HEDGE_ENABLED: '1',
        TPB_BTC_HEDGE_BTC_ONLY: '1',
        TPB_BTC_HEDGE_REQUIRE_INITIAL_TRADE: '0',          // first leg on a strong signal only (no fallback)
        TPB_BTC_HEDGE_TREND_Z: '2.0',                       // momentum threshold (5 s return, sigma)
        TPB_BTC_HEDGE_REVERSAL_CONFIRMATION_SECONDS: '3',   // reversal threshold: -2 sigma for 3 s
        TPB_BTC_HEDGE_MAX_PAIR_COST: '0.95',                // first + opposite ask + fee + slippage
        TPB_BTC_HEDGE_MIN_PAIR_EDGE: '0.05',                // the pair must pay >= 5c
        TPB_BTC_HEDGE_MIN_SECONDS_BETWEEN_LEGS: '0',
        TPB_BTC_HEDGE_MAX_PAIRS_PER_MARKET: '3',
        TPB_BTC_HEDGE_INITIAL_ENTRY_SIZE: '5',              // position / pair size, shares
        TPB_BTC_HEDGE_INITIAL_OBSERVATION_SECONDS: '5',
        TPB_BTC_HEDGE_MAX_INITIAL_ENTRY_PRICE: '0.65',
        TPB_BTC_HEDGE_INITIAL_ATTEMPTS: '3',
        TPB_BTC_HEDGE_INITIAL_FAK_AFTER: '2',
        TPB_BTC_HEDGE_HEDGE_MODE: 'gated',
        TPB_BTC_HEDGE_HOLD_MATCHED_TO_SETTLEMENT: '1',
        TPB_BTC_HEDGE_MAX_SIMULTANEOUS_POSITIONS: '4',
        TPB_BTC_HEDGE_MAX_TOTAL_EXPOSURE_USDC: '40',
        TPB_BTC_HEDGE_MAX_ORDER_USDC: '10',
        TPB_BTC_HEDGE_MAX_DAILY_LOSS_USDC: '20',
      },
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario venue_lag --strategy btc_trend_reversal_hedge '
          + '--assets BTC --data-tag btcpairs-paper --keep-history --log-level INFO' },
    { name: 'tpb-fastclip-paper-clips',   // the original clip strategy, kept for reference (not started)
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
        TPB_FASTCLIP_ENABLED: '1',
        // the locked rule (the config's defaults; spelled out so a change is visible here)
        TPB_FASTCLIP_ASSETS: 'BTC',
        TPB_FASTCLIP_SOURCE: 'comp',
        TPB_FASTCLIP_TRIGGER_MS: '500',
        TPB_FASTCLIP_K_SIGMA: '1.5',
        TPB_FASTCLIP_MIN_EDGE: '0',
        TPB_FASTCLIP_LIMIT_OFFSET: '0',
        TPB_FASTCLIP_CLIP_SHARES: '16',
        TPB_FASTCLIP_CLIP_USDC: '7',
        TPB_FASTCLIP_SAME_SIDE_GAP_MS: '1000',
        TPB_FASTCLIP_MAX_ORDERS_PER_MARKET: '40',
        TPB_FASTCLIP_MAX_MARKET_USDC: '40',
        TPB_FASTCLIP_MIN_ASK: '0.10',
        TPB_FASTCLIP_MAX_ASK: '0.90',
        TPB_FASTCLIP_MAX_SPREAD: '0.03',
        TPB_FASTCLIP_MIN_TIME_REMAINING_S: '60',
        TPB_FASTCLIP_MAX_TIME_REMAINING_S: '292',
        TPB_FASTCLIP_HOLD_TO_SETTLEMENT: '1',
        // budget: a $200 paper bankroll, $40 per market, two markets at once
        TPB_FASTCLIP_MAX_SIMULTANEOUS_POSITIONS: '2',
        TPB_FASTCLIP_MAX_TOTAL_EXPOSURE_USDC: '80',
        TPB_FASTCLIP_MAX_ORDER_USDC: '7',
        TPB_FASTCLIP_MAX_BUYS_PER_HOUR: '600',
        TPB_FASTCLIP_MAX_DAILY_LOSS_USDC: '40',
      },
      args: '-m troll_poly_bot --balance 200 --mode paper --scenario venue_lag --strategy fastclip '
          + '--assets BTC --data-tag fastclip-paper --keep-history --log-level INFO' },
  ],
};
