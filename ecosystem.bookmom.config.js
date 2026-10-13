// BOOK_MOMENTUM (strategy/book_momentum.py) -- PAPER ONLY. The user's plan of 2026-10-09 ("Small-bankroll Polymarket
// BTC 5-minute trading plan"): a book-momentum TAKER plus selective MAKER bids on the Up / Down tokens of BTC 5-minute
// markets, short holds (+2c net take-profit, 10 s max, reversal, 1 USDC max loss), one shared budget (positions +
// resting bids <= 40 USDC, 2 positions, 6 USDC daily stop), markouts at 100 ms / 500 ms / 1 s / 3 s. Not backtested
// (the plan's phase-2 replay is still to do); this is its phase-3 paper run. 10-09 12:5x UTC: edge inputs replaced by
// the measured ones -- with them the gate refuses nearly every signal (see the edge block below).
//
//   pm2 start ecosystem.bookmom.config.js --only tpb-bookmom-paper
//
// __main__ refuses shadow, micro-live and live for this strategy. Tagged files: data/*.bookmom-paper.*

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    { name: 'tpb-bookmom-paper',
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
        TPB_BOOKMOM_ENABLED: '1',
        // the signal (the plan's section 2; hypotheses, not optimised)
        TPB_BOOKMOM_LOOKBACK_MS: '500',
        TPB_BOOKMOM_MIN_RISE: '0.015',
        TPB_BOOKMOM_CONFIRM_UPDATES: '2',
        TPB_BOOKMOM_MAX_SIGNAL_AGE_MS: '250',
        TPB_BOOKMOM_MAX_SPREAD: '0.02',
        TPB_BOOKMOM_MIN_TIME_REMAINING_S: '30',
        TPB_BOOKMOM_MAX_TIME_REMAINING_S: '295',
        // 10-09: a 0.13 token gapped to 0.035 straight through the 1 USDC stop (-1.49); was 0.05 .. 0.95
        TPB_BOOKMOM_MIN_ASK: '0.20',
        TPB_BOOKMOM_MAX_ASK: '0.80',
        // the edge: expected exit = bid + CONTINUATION_FLAT + CONTINUATION x the rise; taker pays both fees, a maker
        // bid pays the exit fee and gives back ADVERSE_SELECTION. MEASURED 10-09 on this bot's first 3,938 signal
        // episodes (scripts/bookmom_signal_study.py, results/bookmom_signal_study_2026-10-09.md): the mid moves
        // +1.2c in 10 s whatever the rise; a filled bid finds the bid 5.7c lower 10 s on. The plan's 1.0 x rise and
        // 1c made the first run (0 of 10 round trips won, -6.69 in 11 min) look like +5..16c of edge.
        TPB_BOOKMOM_CONTINUATION: '0',
        TPB_BOOKMOM_CONTINUATION_FLAT: '0.012',
        TPB_BOOKMOM_SLIPPAGE: '0.005',
        TPB_BOOKMOM_SAFETY_MARGIN: '0.005',
        TPB_BOOKMOM_ADVERSE_SELECTION: '0.057',
        // the taker module: 2-4 USDC, fill-and-kill at ask + 1c
        TPB_BOOKMOM_TAKER_ENABLED: '1',
        TPB_BOOKMOM_TAKER_USDC: '3',
        TPB_BOOKMOM_TAKER_MAX_USDC: '4',
        TPB_BOOKMOM_MAX_SLIPPAGE: '0.01',
        TPB_BOOKMOM_ORDER_TYPE: 'FAK',
        // the maker module: 2-4 USDC post-only bids at the bid, cancelled after 5 s / on reversal / when the bid moves 1c away
        TPB_BOOKMOM_MAKER_ENABLED: '1',
        TPB_BOOKMOM_MAKER_USDC: '3',
        TPB_BOOKMOM_MAKER_MAX_USDC: '4',
        TPB_BOOKMOM_MAKER_MAX_REST_S: '5',
        TPB_BOOKMOM_MAKER_REPRICE: '0.01',
        TPB_BOOKMOM_MAKER_CANCEL_DROP: '0.01',
        // exits (section 4)
        TPB_BOOKMOM_TAKE_PROFIT_NET: '0.02',
        TPB_BOOKMOM_MAX_HOLD_S: '10',
        TPB_BOOKMOM_REVERSAL_DROP: '0.015',
        TPB_BOOKMOM_MAX_LOSS_USDC: '1',
        TPB_BOOKMOM_REENTRY_COOLDOWN_S: '2',
        // the shared budget (sections 1 and 4)
        TPB_BOOKMOM_ASSETS: 'BTC',
        TPB_BOOKMOM_MAX_SIMULTANEOUS_POSITIONS: '2',
        TPB_BOOKMOM_MAX_TOTAL_EXPOSURE_USDC: '40',
        TPB_BOOKMOM_MAX_DAILY_LOSS_USDC: '6',
        TPB_BOOKMOM_MAX_ORDER_USDC: '4',
        TPB_BOOKMOM_FAST_FEED: '0',
        // this bot's own loss limits, not the Settings page's shared paper rows (daily 120 / drawdown 30)
        TPB_CONTROLS_IGNORE: 'risk.max_drawdown_usdc,risk.max_daily_loss_usdc',
      },
      args: '-m troll_poly_bot --balance 200 --mode paper --scenario calibrated --strategy book_momentum --assets BTC '
          + '--max-order-usdc 4 --max-open-usdc 40 --max-daily-loss 6 --max-drawdown 40 '
          + '--data-tag bookmom-paper --keep-history --log-level INFO' },
  ],
};
