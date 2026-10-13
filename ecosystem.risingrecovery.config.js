// RISING-TOKEN RECOVERY (strategy/rising_recovery.py) -- PAPER ONLY. The user's spec of 2026-10-09: maker bids on the
// rising token; when its rise ends, the opposite token is bought ONLY as pairs costing under $1 (fees in), sized from
// the market's UP / DOWN inventory and cost; buying stops once min(U, D) - C >= the target, or at a capital / loss
// limit. Everything is held to settlement. Not backtested.
// 10-09 16:2x the user: "Don't think it should make exact pairs. Goal is make positive P&L ... buy predicted rising
// tokens continuously" -> MODE momentum: every rising token (UP or DOWN) is bought each time it rises, through the market;
// loss control by the market's spend / worst-case limits and the daily stop; a market whose worst outcome is already a
// profit stops buying. The pairing rules below stay for MODE pairs.
//
//   pm2 start ecosystem.risingrecovery.config.js --only tpb-risingrecovery-paper
//
// __main__ refuses shadow, micro-live and live for this strategy. Tagged files: data/*.risingrecovery-paper.*

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

module.exports = {
  apps: [
    { name: 'tpb-risingrecovery-paper',
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
        TPB_RTR_ENABLED: '1',
        // 10-12 the user's spot rules: MODE spot (strategy/rising_recovery.py, "SPOT MODE" in the module doc) -- init 10 sh
        // on the 1-s BTC direction, 5 sh on each confirmed reversal, 10 sh on each strike crossing, the leader rule
        // (> 0.75, only if both outcomes >= +5 after it), the near-strike rule (<= 0.05 %, only if the worst case
        // improves); 100 sh per outcome, 100 USDC per market, 1 s between orders; paper only, every decision [RTR_SPOT]
        // 10-13 the user's consolidated spec -> MODE opt ("OPT MODE" in strategy/rising_recovery.py): initial 10 sh on
        // BTC's 2-s move > 0.3 bp (once, >= 2 s after the open); then one decision per event (6-s timer, 3-s momentum timer
        // while the lower-priced token rises through 0.30-0.40, a token's book rise, a BTC 2-s reversal): candidate
        // quantities 5/10/15/20 per side simulated against the ask depth with the market's fee + 1c slippage, effective
        // price 0.10..0.90, chosen by worst-case PnL then best-case, only if it beats no-trade by 0.05; paper only
        TPB_RTR_MODE: 'opt',
        TPB_RTR_OPT_OPEN_WINDOW_S: '2',
        TPB_RTR_OPT_INIT_BPS: '0.3',
        TPB_RTR_OPT_INIT_SHARES: '10',
        TPB_RTR_OPT_INIT_MAX_S: '30',
        TPB_RTR_OPT_INIT_RETRY_MAX_S: '0',       // a failed initial order is re-sent until it fills (0 = until the close guard)
        TPB_RTR_OPT_PRICE_MIN: '0.10',
        TPB_RTR_OPT_PRICE_MAX: '0.90',
        TPB_RTR_OPT_EVAL_INTERVAL_S: '6',
        TPB_RTR_OPT_MOM_INTERVAL_S: '3',
        TPB_RTR_OPT_MOM_LOW: '0.30',
        TPB_RTR_OPT_MOM_HIGH: '0.40',
        TPB_RTR_OPT_MOM_TARGET: '0.50',
        TPB_RTR_OPT_QUANTITIES: '5,10,15,20',
        TPB_RTR_OPT_MIN_IMPROVEMENT: '0.05',
        TPB_RTR_OPT_TIE_EPS: '0.01',
        TPB_RTR_OPT_REQUIRE_POSITIVE: 'none',   // 'both' = the strict version (nearly every buy infeasible at ask prices)
        // 16:0x the user chose option 2: the same rules at half the size, the loss limits (daily 20, drawdown 60) kept
        TPB_RTR_SPOT_INIT_SHARES: '10',
        TPB_RTR_SPOT_REV_SHARES: '5',         // 50 % of 5 is under the venue's 5-share minimum: the minimum
        TPB_RTR_SPOT_MIN_MOVE_BPS: '1',
        TPB_RTR_SPOT_CONFIRM_PASSES: '2',
        TPB_RTR_SPOT_LEADER_MID: '0.75',
        TPB_RTR_SPOT_LEADER_TARGET_USDC: '5',
        TPB_RTR_SPOT_NEAR_STRIKE_PCT: '0.05',
        TPB_RTR_SPOT_MAX_SHARES: '50',
        TPB_RTR_SPOT_MAX_COST_USDC: '50',
        TPB_RTR_SPOT_MIN_INTERVAL_S: '1',
        TPB_RTR_SPOT_SLIPPAGE_ALLOWANCE: '0.01',
        // 14:5x the user: orders again until filled (want queue), the lower-priced token only while the market's
        // mark-to-market PnL >= 0, nothing before 5 s after the open
        TPB_RTR_SPOT_OPEN_DELAY_S: '10',
        TPB_RTR_SPOT_PNL_RULE: '1',
        TPB_RTR_SPOT_RETRY_LOG_S: '5',
        // 15:1x the user: the leader still rising under 0.90 is bought every 5 s up to 50 sh; after the climb the cheap
        // token is bought in the amount that makes both outcomes positive (if that exists)
        TPB_RTR_SPOT_CLIMB_ENABLED: '1',
        TPB_RTR_SPOT_CLIMB_SHARES: '5',
        TPB_RTR_SPOT_CLIMB_MAX_SHARES: '25',
        TPB_RTR_SPOT_CLIMB_INTERVAL_S: '5',
        TPB_RTR_SPOT_CLIMB_MAX_ASK: '0.90',
        TPB_RTR_SPOT_CLIMB_LOOKBACK_S: '5',
        TPB_RTR_SPOT_PAIR_TARGET_USDC: '0.10',
        // 16:1x the user: no 5-s cadence -- the leader is bought only on a +10c jump within 1 s; the cheap pair buy halved
        TPB_RTR_SPOT_CLIMB_MIN_RISE_1S: '0.10',
        // 19:1x the user: once the climbed token fell 5c from its peak and jumps again, the climb count restarts
        TPB_RTR_SPOT_CLIMB_RESET_DROP: '0.05',
        TPB_RTR_SPOT_PAIR_FRACTION: '0.5',
        // 16:4x the user: "do not buy every 5 s, only if the token rose 10c" -- the init / reversal / crossing / leader /
        // near-strike buys are OFF; the only buy trigger is a +10c-in-1-s jump of either token (then the pair rule)
        // 17:5x the user: 10 s after the open, 10 shares of the token rising at that moment
        TPB_RTR_SPOT_INIT_ENABLED: '1',
        TPB_RTR_SPOT_INIT_SIGNAL: 'token',
        // 18:3x the user: after the first buy, a resting bid on the other token for half the amount at 0.80 - the fill price
        TPB_RTR_SPOT_COVER_ENABLED: '1',
        TPB_RTR_SPOT_COVER_FRACTION: '0.5',
        TPB_RTR_SPOT_COVER_PAIR_COST: '0.80',
        // 18:5x the user: no resting opposite bid -- the other token is bought (taken) once it starts rising (+2c in 1 s)
        TPB_RTR_SPOT_COVER_MODE: 'rise',
        // 19:2x the user: keep the PnL positive by gathering rising tokens -- a token rising >= 2c in 1 s is bought (<= 10 sh
        // per order, once per rise) while the market's PnL if it wins is under +0.10; PnL marked every pass (snapshot mtm)
        TPB_RTR_SPOT_GUARD_ENABLED: '1',
        TPB_RTR_SPOT_GUARD_MIN_RISE_1S: '0.02',
        TPB_RTR_SPOT_GUARD_TARGET_USDC: '0.10',
        TPB_RTR_SPOT_GUARD_MAX_SHARES: '10',
        // 20:5x the user: when pairing, the lower-priced token stays at most 1/2 of the higher-priced token's shares
        TPB_RTR_SPOT_LOW_RATIO_MAX: '0.5',
        // 21:2x the user: the lower-priced token starting to rise in the book (ask +2c in 1 s on up-ticks) is bought 10 sh,
        // once per rise, 2 buys a market; the PnL guard then recovers with the rising high-priced token
        TPB_RTR_SPOT_LOWRISE_ENABLED: '1',
        TPB_RTR_SPOT_LOWRISE_SHARES: '10',
        TPB_RTR_SPOT_LOWRISE_MAX_PER_MARKET: '2',
        TPB_RTR_SPOT_COVER_MIN_RISE_1S: '0.02',
        TPB_RTR_SPOT_REV_ENABLED: '0',
        TPB_RTR_SPOT_CROSS_ENABLED: '0',
        TPB_RTR_SPOT_LEADER_ENABLED: '0',
        TPB_RTR_SPOT_NEAR_ENABLED: '0',
        TPB_RTR_SPOT_CLIMB_ANY_TOKEN: '1',
        TPB_RTR_MAX_SLIPPAGE: '0.01',
        // fill rate (the user, 16:5x): the maker momentum bids filled 4 of 22 (pulled as the rising bid moved away) ->
        // momentum buys and the favourite fix take (FAK at ask + 1c), the cheap hedge takes under 0.20, else bids
        TPB_RTR_BUY_MODE: 'taker',
        TPB_RTR_FIX_MODE: 'taker',
        TPB_RTR_HEDGE_MODE: 'auto',
        TPB_RTR_MAX_MARKET_FILLS: '40',
        // the user, 16:3x: "if expected P&L is negative, make positive to buy high price token" -- when the market's P&L
        // if the favourite (mid >= 0.55) wins is under +0.10, the favourite is bought until it is (limits apply)
        TPB_RTR_FIX_HIGH: '1',
        TPB_RTR_HIGH_MIN_MID: '0.55',
        TPB_RTR_HIGH_WIN_TARGET_USDC: '0.10',
        // the user, 16:4x: "if current or predicted P&L is positive, do not buy lower price token ... buy under 0.2 tokens to
        // minimize max loss of worst case" -- positive state: no momentum buys of the lower-priced token; it is bought only
        // under 0.20 as a hedge sized to even out the two outcomes (the favourite's outcome kept >= +0.10)
        TPB_RTR_HEDGE_MAX_PRICE: '0.20',
        TPB_RTR_HEDGE_ENABLED: '0',
        TPB_RTR_POSITIVE_RULE: '1',
        TPB_RTR_ENTRY_SIGNAL: 'rise',
        // no new order on a token for 3 s after one ended unfilled: its fill may still arrive (16:46 double hedge)
        TPB_RTR_END_GUARD_S: '3',
        // the user's cover rule (10-10: cheap bid on the other token 10 s after each momentum buy, taken if it turns up)
        // is built but OFF -- replay r10 over 3,624 markets: -0.26 a market (t -8.4) vs +0.04 without it; every
        // reversal-take variant -0.18..-0.22, bids only -0.02..-0.05. The user chose "fixes, cover off" (10-12)
        TPB_RTR_COVER_ENABLED: '0',
        TPB_RTR_NO_DOUBLE_LOSS: '0',
        // 1. the rising token: ask +2c over 1 s on rising updates -> a post-only bid at the bid (1c better on a 2c spread)
        TPB_RTR_LOOKBACK_MS: '1000',
        TPB_RTR_MIN_RISE: '0.02',
        TPB_RTR_CONFIRM_UPDATES: '2',
        TPB_RTR_MAX_SIGNAL_AGE_MS: '500',
        TPB_RTR_MAX_SPREAD: '0.02',
        // 18:1x optimisation (scripts/rising_recovery_replay.py, 3,624 recorded markets, 5 rounds; results/
        // rising_recovery_optimisation_2026-10-09.md): the old settings lost 0.46 a market (t -8.8, every split). Kept:
        // momentum buys only on tokens at 0.65-0.90 and only in the last 150 s; hedge off (it lost 3.6-4.2c a share).
        // Replay: +0.06 a market on train+val, -0.03 on test -- about break-even, not a proven edge
        TPB_RTR_MIN_ASK: '0.65',
        TPB_RTR_MAX_ASK: '0.90',
        TPB_RTR_ENTRY_MIN_LEFT_S: '30',
        TPB_RTR_ENTRY_MAX_LEFT_S: '150',
        TPB_RTR_ENTRY_USDC: '3',
        TPB_RTR_ENTRY_MAX_USDC: '5',
        TPB_RTR_ENTRY_MAX_REST_S: '5',
        // q = the market mid (no model): a bid must sit at or below it. The 10-09 study measured ~5.7c of adverse
        // selection on filled bids; 0 here runs the spec as written
        TPB_RTR_ENTRY_ADVERSE_SELECTION: '0',
        TPB_RTR_ENTRY_MIN_EDGE: '0',
        TPB_RTR_MAX_ENTRY_FILLS: '3',
        TPB_RTR_MAX_ENTRY_COST_USDC: '10',
        TPB_RTR_ENTRY_COOLDOWN_S: '3',
        // 2. the rise ends: 2c off the peak, no new high for 20 s, or the other token rising
        TPB_RTR_REVERSAL_DROP: '0.02',
        TPB_RTR_STALL_S: '20',
        // 3. pairs under $1: taken when the pair locks >= 2c at the ask (fee in), else a resting bid locking >= 1c
        TPB_RTR_PAIR_MARGIN: '0.01',
        TPB_RTR_PAIR_MARGIN_TAKER: '0.02',
        TPB_RTR_PAIR_TAKER: '1',
        TPB_RTR_PAIR_MIN_LEFT_S: '10',
        // the user, 10-09 15:5x: "buy high price token as 2 times of low token shares" -- after the rise ends the higher-
        // priced token is bought up to 2 x the low token's shares (the matched half still a pair under $1, the extra
        // high shares only as post-only bids at the bid), then the market is done; 1 = equal pairs
        TPB_RTR_HIGH_RATIO: '2',
        // 5. the target and the limits
        TPB_RTR_TARGET_PROFIT_USDC: '0.20',
        TPB_RTR_MAX_MARKET_COST_USDC: '100',
        TPB_RTR_MAX_MARKET_LOSS_USDC: '10',
        TPB_RTR_MAX_TOTAL_COST_USDC: '200',
        TPB_RTR_ASSETS: 'BTC',
        TPB_RTR_MAX_TOTAL_EXPOSURE_USDC: '200',
        TPB_RTR_MAX_DAILY_LOSS_USDC: '20',
        TPB_RTR_MAX_ORDER_USDC: '100',
        TPB_RTR_MAX_SIMULTANEOUS_POSITIONS: '4',
        TPB_RTR_FAST_FEED: '0',
        // this bot's own loss limits, not the Settings page's shared paper rows
        TPB_CONTROLS_IGNORE: 'risk.max_drawdown_usdc,risk.max_daily_loss_usdc',
      },
      args: '-m troll_poly_bot --balance 200 --mode paper --scenario calibrated --strategy rising_recovery --assets BTC '
          + '--max-order-usdc 100 --max-open-usdc 200 --max-daily-loss 20 --max-drawdown 60 '
          + '--data-tag risingrecovery-paper --keep-history --log-level INFO' },
  ],
};
