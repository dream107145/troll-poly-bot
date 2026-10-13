// tpb-pairlock-paper: the PAIR-LOCK strategies of 2026-10-02 (B = v2 EV hedge, running; A/C/D/E = recovery-hedge spec), PAPER ONLY, a separate bot
// from tpb-repricedmom-paper (the user: "do not implement it in the current triangle paper bot; use another new bot").
//
//   pm2 start ecosystem.pairlock.config.js            (this file defines ONLY this app)
//
// Engine: the triangle engine's triggers with the pair-lock layer on top (TPB_TRIANGLE_HEDGE_MODE=dynamic ->
// strategy/pairlock_engine.py; the math in strategy/pairlock.py, the same functions as scripts/pairlock/replay.py).
// Own files: data/*.pairlock-btc-paper.*. The research and the parameter choice: docs/pairlock_2026-10-01.md.
// __main__ refuses micro-live / live / shadow for the triangle strategy: there is no real-money switch.
const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

const ENV = {
  PYTHONUNBUFFERED: '1',
  // ---- the trigger: the replay's (1 s spot shock >= 2.5 sigma, the 3 s move agreeing, one per direction a second)
  TPB_TRIANGLE_ENABLED: '1',
  TPB_TRIANGLE_SIGNAL_SOURCE: 'spot_lag',
  TPB_TRIANGLE_K_SIGMA: '2.5',
  TPB_TRIANGLE_K3_SIGMA: '0',
  TPB_TRIANGLE_REQUIRE_ACCELERATION: '0',
  TPB_TRIANGLE_TREND_WINDOW_S: '0',
  TPB_TRIANGLE_TREND_MIN_BPS: '0',
  TPB_TRIANGLE_FV_TRIGGER_CENTS: '0',
  TPB_TRIANGLE_DEDUPE_S: '1',
  TPB_TRIANGLE_MAX_SPOT_AGE_MS: '500',
  TPB_TRIANGLE_EVENT_DRIVEN: '1',
  TPB_TRIANGLE_EXCLUDE_ASSETS: 'HYPE',
  TPB_TRIANGLE_ASSETS: 'BTC',
  TPB_SPOT_LEAD_WINDOW_MS: '0',
  TPB_BOOK_SKIP_ASSETS: 'HYPE',
  TPB_GUARD_COOLDOWN_S: '5',
  // ---- the triangle engine's own extras OFF: the pair-lock layer decides entries and hedges
  TPB_TRIANGLE_HZ_ENABLED: '0',
  TPB_TRIANGLE_ENTRY_MODEL: '0',
  TPB_TRIANGLE_PAIR_COST: '0',
  TPB_TRIANGLE_WAIT_FOR_PAIR: '0',
  TPB_TRIANGLE_FLOW_MAX_TOKEN_MOVE: '-1',
  TPB_TRIANGLE_MIN_PRICE: '0.20',
  TPB_TRIANGLE_MAX_PRICE: '0.96',
  TPB_TRIANGLE_MIN_TIME_REMAINING_S: '10',
  TPB_TRIANGLE_SHARES: '10',
  TPB_TRIANGLE_LIMIT_OFFSET: '0.03',
  TPB_TRIANGLE_ORDER_TYPE: 'FAK',          // fill-and-kill (2026-10-02): take what the book holds at <= the limit; all-or-nothing FOK killed
                                            // 45 of 88 entries and the killed ones were the best (+0.15-0.22/sh at their limit vs +0.05 filled)
  TPB_TRIANGLE_HOLD_TO_SETTLEMENT: '1',
  TPB_TRIANGLE_HEDGE_MODE: 'dynamic',
  // ---- the triangle budget (the bot's RiskManager; the layer's own hard limits are below)
  TPB_TRIANGLE_MAX_ORDER_USDC: '10',
  TPB_TRIANGLE_MAX_MARKET_USDC: '20',
  TPB_TRIANGLE_MAX_SIMULTANEOUS_POSITIONS: '4',
  TPB_TRIANGLE_MAX_TOTAL_EXPOSURE_USDC: '40',
  TPB_TRIANGLE_MAX_DAILY_LOSS_USDC: '40',
  // ---- the pair-lock layer v2 (2026-10-02 brief; docs/pairlock_v2_2026-10-02.md). Accounting, states and the hedge
  // candidates: strategy/pairlock_book.py; decisions: strategy/pairlock.py; replay: scripts/pairlock/replay3.py.
  // Every value below was chosen on TRAIN (09-22 .. 09-27) -> VALIDATION (.. 09-29) and reported untouched on TEST /
  // FORWARD. The hedge settings sit in a FLAT region: every neighbour is ~0 on TRAIN -- no hedge setting has a
  // demonstrated edge; the bot runs C so that paper fills can confirm or refute it.
};

// ---- STRATEGY B (the user's choice, 2026-10-02 after the A-E replay): the v2 EV-based conditional hedge, exactly as
// it ran from 14:19 UTC (strategy/pairlock_engine_v2.py on the frozen strategy/pairlock_v2.py): adverse = bid 3c
// under cost, reversal score >= 1.5 held 400 ms, a hedge only if P(fill) x (E[after] - E[without]) > 0.05, locks at
// net edge >= 0.05, pair cost <= 1.15, and a NEW cycle allowed after a fully matched pair (max 2 per market).
// Replay (docs/pairlock_recovery_2026-10-02.md): +146.14 on 745 markets (+0.196, t 1.34), of which +32 from re-entries.
const PAIRLOCK_B = {
  TPB_PAIRLOCK_STRATEGY: 'B',
  TPB_PAIRLOCK_ADVERSE_MIN: '0.03',
  TPB_PAIRLOCK_BAND_HI: '0.96',
  TPB_PAIRLOCK_BAND_LO: '0.20',
  TPB_PAIRLOCK_CONFIRM_MS: '400',
  TPB_PAIRLOCK_DEPTH_HAIRCUT: '0.8',
  TPB_PAIRLOCK_ENTRY_EDGE: '0.02',
  TPB_PAIRLOCK_ENTRY_GATE: '1',
  TPB_PAIRLOCK_ENTRY_MODE: 'both',
  TPB_PAIRLOCK_ENTRY_OFFSET: '0.03',
  TPB_PAIRLOCK_ENTRY_SHARES: '10',
  TPB_PAIRLOCK_FRACTIONS: '0,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0',
  TPB_PAIRLOCK_HEDGE_COOLDOWN_MS: '1000',
  TPB_PAIRLOCK_HEDGE_MODE: 'conditional',
  TPB_PAIRLOCK_HEDGE_OFFSET: '0.03',
  TPB_PAIRLOCK_LIM_MAX_DAILY_LOSS: '40',
  TPB_PAIRLOCK_LIM_MAX_DRAWDOWN: '60',
  TPB_PAIRLOCK_LIM_MAX_HEDGE_ACTIONS: '3',
  TPB_PAIRLOCK_LIM_MAX_HEDGE_POSITION: '10',
  TPB_PAIRLOCK_LIM_MAX_INITIAL_POSITION: '10',
  TPB_PAIRLOCK_LIM_MAX_MARKET_CAPITAL: '20',
  TPB_PAIRLOCK_LIM_MAX_MARKET_LOSS: '10',
  TPB_PAIRLOCK_LIM_MAX_PAIR_COST: '1.15',
  TPB_PAIRLOCK_LIM_MAX_QUOTE_AGE_MS: '2000',
  TPB_PAIRLOCK_LIM_MAX_SPOT_AGE_MS: '1000',
  TPB_PAIRLOCK_LIM_MAX_SPREAD: '0.05',
  TPB_PAIRLOCK_LIM_MAX_TOTAL_POSITION: '40',
  TPB_PAIRLOCK_LIM_MIN_TIME_REMAINING_S: '10',
  TPB_PAIRLOCK_LOCK_OPPORTUNITIES: '1',
  TPB_PAIRLOCK_MAX_CYCLES_PER_MARKET: '2',
  TPB_PAIRLOCK_MIN_HEDGE_EV: '0.05',
  TPB_PAIRLOCK_MIN_LEFT_ENTRY_S: '60',
  TPB_PAIRLOCK_MIN_LEFT_HEDGE_S: '10',
  TPB_PAIRLOCK_MIN_LOCKED_PROFIT: '0.05',
  TPB_PAIRLOCK_MIN_PAIR_EDGE: '0.05',
  TPB_PAIRLOCK_MIN_SIZE: '5',
  TPB_PAIRLOCK_MODEL_PATH: 'data/replay/pairlock2_model.json',
  TPB_PAIRLOCK_REEVAL_MS: '5000',
  TPB_PAIRLOCK_REV_THR: '1.5',
  TPB_PAIRLOCK_SHORT_MIN: '0.55',
};

// ---- strategies A / C / D / E: the recovery-hedge / profit-lock engine (strategy/pairlock_engine.py), the spec
// defaults. TPB_PAIRLOCK_STRATEGY sets the hedge mode: A none, C recovery, D profit_lock, E combined.
const PAIRLOCK_NEW = {
  TPB_PAIRLOCK_MODEL_PATH: 'data/replay/pairlock2_model.json',   // P(UP) + P(favourable in 10 s) + execution costs
  TPB_PAIRLOCK_ENTRY_GATE: '1',
  TPB_PAIRLOCK_ENTRY_EDGE: '0.02',          // EV per share at the executable VWAP, after fee, slippage, adverse selection
  TPB_PAIRLOCK_ENTRY_MODE: 'both',          // ... AND P(the token's bid in 10 s >= the entry ask) >= SHORT_MIN
  TPB_PAIRLOCK_SHORT_MIN: '0.55',
  TPB_PAIRLOCK_ENTRY_SHARES: '10',
  TPB_PAIRLOCK_MIN_SIZE: '5',
  TPB_PAIRLOCK_BAND_LO: '0.20',
  TPB_PAIRLOCK_BAND_HI: '0.96',
  TPB_PAIRLOCK_ENTRY_OFFSET: '0.03',
  TPB_PAIRLOCK_MIN_LEFT_ENTRY_S: '60',
  TPB_PAIRLOCK_MAX_CYCLES_PER_MARKET: '2',
  // ---- position management (2026-10-02 recovery-hedge / profit-lock spec; strategy/pairlock.py manage_position).
  // Defaults set a priori, NOT optimised. The v2 EV rule (MIN_HEDGE_EV, REV_THR, CONFIRM_MS, ADVERSE_MIN,
  // LOCK_OPPORTUNITIES, LIM_MAX_PAIR_COST) no longer exists; the engine warns if one is still set.
  TPB_PAIRLOCK_HEDGE_MODE: 'combined',      // E: recovery hedge (MODE B) + profit lock (MODE A); none | recovery | profit_lock
  TPB_PAIRLOCK_FRACTIONS: '0,0.2,0.4,0.6,0.8,1.0',   // candidate sizes: 2/4/6/8/10 of a 10-share position
  TPB_PAIRLOCK_SLIPPAGE_ALLOWANCE: '0.005', // per share added to the opposite VWAP in every hedge decision
  // MODE B -- recovery: materially losing AND a confirmed reversal (>= 3 of 4 signals held 400 ms, fresh data)
  TPB_PAIRLOCK_RECOVERY_MIN_LOSS: '0.50',            // USDC unrealised at the executable bid
  TPB_PAIRLOCK_RECOVERY_MIN_ADVERSE_MOVE: '0.05',    // avg cost - bid, per share
  TPB_PAIRLOCK_RECOVERY_REVERSAL_THRESHOLD: '1.5',   // the composite reversal score (one of the 4 signals)
  TPB_PAIRLOCK_RECOVERY_CONFIRMATION_WINDOW_MS: '400',
  TPB_PAIRLOCK_RECOVERY_MIN_CONFIRMATIONS: '3',
  TPB_PAIRLOCK_RECOVERY_MIN_TIME_LEFT_S: '10',
  TPB_PAIRLOCK_RECOVERY_MAX_HEDGE_FRACTION: '1.0',
  TPB_PAIRLOCK_MAX_ACCEPTABLE_PAIR_COST: '1.00',     // the paired portion, fees + slippage in. NOTE: the books mirror
                                                      // (DOWN ask = 1 - UP bid), so a materially losing position pairs
                                                      // at >= 1 + its loss: at 1.00 no recovery hedge can qualify
  TPB_PAIRLOCK_MIN_RECOVERY_DOWNSIDE_REDUCTION: '0.50',   // USDC of worst case
  TPB_PAIRLOCK_RISK_AVERSION: '0.25',       // utility = expected P&L + RA x downside reduction
  // MODE A -- profit lock: only while NOT materially losing
  TPB_PAIRLOCK_PROFIT_LOCK_MIN_EDGE: '0.05',
  TPB_PAIRLOCK_PROFIT_LOCK_MAX_COST: '0.95',
  TPB_PAIRLOCK_PROFIT_LOCK_RISK_AVERSION: '0.25',
  TPB_PAIRLOCK_PROFIT_LOCK_MIN_TIME_LEFT_S: '10',
  // PAIR_LOCKED = fully matched AND net edge >= MIN_PAIR_EDGE AND locked >= MIN_LOCKED_PROFIT
  TPB_PAIRLOCK_MIN_PAIR_EDGE: '0.05',
  TPB_PAIRLOCK_MIN_LOCKED_PROFIT: '0.05',
  TPB_PAIRLOCK_HEDGE_OFFSET: '0.03',        // FOK limit = last level + 3c, capped so no fill can pair above the cap
  TPB_PAIRLOCK_HEDGE_COOLDOWN_MS: '1000',
  TPB_PAIRLOCK_REEVAL_MS: '5000',
  TPB_PAIRLOCK_DEPTH_HAIRCUT: '0.8',        // 80 % of every ask level assumed taken by faster traders (as the replay)
  // ---- hard risk limits (brief item 30); they override every signal
  TPB_PAIRLOCK_LIM_MAX_MARKET_CAPITAL: '20',
  TPB_PAIRLOCK_LIM_MAX_INITIAL_POSITION: '10',
  TPB_PAIRLOCK_LIM_MAX_TOTAL_POSITION: '40',
  TPB_PAIRLOCK_LIM_MAX_HEDGE_POSITION: '10',
  TPB_PAIRLOCK_LIM_MAX_HEDGE_ACTIONS: '3',
  TPB_PAIRLOCK_LIM_MAX_MARKET_LOSS: '10',
  TPB_PAIRLOCK_LIM_MAX_DAILY_LOSS: '40',
  TPB_PAIRLOCK_LIM_MAX_DRAWDOWN: '60',
  TPB_PAIRLOCK_LIM_MAX_SPREAD: '0.05',
  TPB_PAIRLOCK_LIM_MAX_QUOTE_AGE_MS: '2000',
  TPB_PAIRLOCK_LIM_MAX_SPOT_AGE_MS: '1000',
  TPB_PAIRLOCK_LIM_MIN_TIME_REMAINING_S: '10',
};

//: which of the two blocks the app runs ('B', or 'A' / 'C' / 'D' / 'E')
const STRATEGY = 'B';
const PAIRLOCK = STRATEGY === 'B' ? PAIRLOCK_B : { ...PAIRLOCK_NEW, TPB_PAIRLOCK_STRATEGY: STRATEGY };

//: the app as it ran strategy B until 2026-10-02 20:40 UTC. ecosystem.pairlockrecovery.config.js builds the recovery bot
//: on THIS (baseB), not on the app below, so the conditions rule never leaks into the recovery bot's env.
const B_APP = {
  name: 'tpb-pairlock-paper',
  cwd: ROOT, interpreter: 'none', script: PYTHON, autorestart: true, min_uptime: '30s', max_restarts: 10,
  restart_delay: 5000, kill_timeout: 15000, merge_logs: true, time: true, max_memory_restart: '600M',
  env: { ...ENV, ...PAIRLOCK },
  args: '-m troll_poly_bot --balance 100 --mode paper --scenario calibrated --strategy triangle '
      + '--exchanges binance,bybit,coinbase --assets BTC --data-tag pairlock-btc-paper --log-level INFO',
};

// ---- THE CONDITIONS RULE (the user, 2026-10-02 20:33 UTC; this bot only, NOT the recovery bot): the engine is
// strategy/pairlock_recovery_engine.py (TPB_PAIRLOCK_STRATEGY=R) in entry mode "conditions", the loss recovery OFF.
//   1. buy OPEN_SHARES (20) of the HIGHER-priced token when: its ask is in COND_MIN..MAX_PRICE (0.50-0.60), the market
//      ends >= COND_MIN_LEFT_S (120 s) later, COND_SPOT_LO < BTC - strike < COND_SPOT_HI (0 < d < 5 USD) and the market
//      holds no tokens; right after the fill a GTC bid for the OTHER token at PAIR_SUM - the fill (0.97, dashboard
//      "Pair-lock BTC conditions rule" -> pairbtc.pair_sum) for what filled, resting until the close
//   1b. (the user, 10-03 01:55) the LOSS RECOVERY of tpb-pairlockrecovery-paper: if that bid has not filled and the other
//      token becomes the higher price, the bid is pulled and the recovery arithmetic buys the higher token
//      (x = (TARGET - its branch) / (1 - VWAP - fee - slip), steps <= 60 sh, same limits as the recovery bot)
//   2. with < LATE_WINDOW_S (20 s) left (the bot sends no buy under 10 s) and the higher token's mid > LATE_MIN_MID
//      (0.93): buy LATE_SHARES (30) of it, once; if its mid then falls under LATE_HEDGE_TRIGGER (0.90), a GTC bid for
//      the OTHER token at LATE_HEDGE_PRICE (0.20) for the late shares, until the close
const CONDITIONS = {
  TPB_PAIRLOCK_STRATEGY: 'R',
  TPB_PAIRLOCK_REC_ENTRY_MODE: 'conditions',
  TPB_PAIRLOCK_REC_OPEN_SHARES: '10',           // 2026-10-08 synced with the dashboard (the user: 5 -> 10 at 15:41)
  TPB_PAIRLOCK_REC_OPEN_LIMIT_OFFSET: '0.02',   // fill-and-kill at the ask + 2c
  TPB_PAIRLOCK_REC_OPEN_MAX_TRIES: '3',         // a killed buy leaves the market flat; it may try again (3 per market)
  TPB_PAIRLOCK_REC_COND_MIN_PRICE: '0.50',
  TPB_PAIRLOCK_REC_COND_MAX_PRICE: '0.60',
  TPB_PAIRLOCK_REC_COND_MIN_LEFT_S: '120',
  TPB_PAIRLOCK_REC_COND_SPOT_LO: '0',
  TPB_PAIRLOCK_REC_COND_SPOT_HI: '20',         // 2026-10-08 synced with the dashboard (the user: 10 -> 20 at 15:35)
  // 2026-10-08 the user: "buy tokens only rising one, not pricing down token" -- the higher-priced token is bought only if its
  // mid now is above its mid COND_RISING_WINDOW_S ago (by more than COND_RISING_MIN); flat or falling: not bought
  TPB_PAIRLOCK_REC_COND_RISING_WINDOW_S: '3',
  TPB_PAIRLOCK_REC_COND_RISING_MIN: '0',
  // 2026-10-08 the user: "it still buys downward tokens" -> also rising over 10 s, not falling in the last 1 s, and BTC moving
  // the token's way over those 10 s
  TPB_PAIRLOCK_REC_COND_RISING_LONG_S: '10',
  TPB_PAIRLOCK_REC_COND_RISING_LAST_S: '1',
  TPB_PAIRLOCK_REC_COND_RISING_SPOT: '1',
  // 2026-10-08 the user: "or the high price token is over 0.85 and under 0.92; repeat this once filled": the higher token with
  // its ask in (0.85, 0.92), rising, >= 120 s left (no BTC - strike band), no pair bid, bought again after each fill, 5 per market
  // 2026-10-08 the user: "when entry, buy the lower token instead of the high price token" (conditions judged on the higher
  // token, the other one bought; the rising checks on the token bought) -- the normal entry and the high band
  TPB_PAIRLOCK_REC_COND_BUY_LOWER: '1',          // 2026-10-08: also the market-open (first) buy: the LOWER token, only while it is rising
  // 2026-10-08 the user: MARKET OPEN BUY mode (default on): within 20 s after the open, while the market holds nothing, buy the
  // higher-priced token if its ask is in 0.50-0.70 (its opposite bid as usual; no BTC - strike / rising checks)
  TPB_PAIRLOCK_REC_COND_FIRST_BUY_DELAY_S: '10',  // 2026-10-08 the user: the first buy at least 10 s after the market opens
  TPB_PAIRLOCK_REC_COND_OPEN_BUY: '1',
  TPB_PAIRLOCK_REC_COND_AFTER_BUY: '1',          // 2026-10-08: the regular conditions entry on/off (Settings: After-open buy mode)
  TPB_PAIRLOCK_REC_COND_AFTER_DELAY_S: '0',      // ... and not before this many seconds after the open (Settings: After-open buy: not before)
  TPB_PAIRLOCK_REC_COND_HIGH_BUY: '1',           // 2026-10-08: the high band on/off (Settings: High price buy mode)
  TPB_PAIRLOCK_REC_COND_OPEN_WINDOW_S: '20',
  TPB_PAIRLOCK_REC_COND_OPEN_MIN: '0.50',
  TPB_PAIRLOCK_REC_COND_OPEN_MAX: '0.70',
  TPB_PAIRLOCK_REC_COND_HIGH_BUY_LOWER: '0',     // the user: "when it buys the high price token over 0.85": the high band buys the HIGH token
  TPB_PAIRLOCK_REC_COND_HIGH_PAIR_BID: '1',      // ... and "creates the opposite bid" (pair sum - fill); the next high buy waits for it to fill
  TPB_PAIRLOCK_REC_COND_HIGH_MIN: '0.85',
  TPB_PAIRLOCK_REC_COND_HIGH_MAX: '0.92',
  TPB_PAIRLOCK_REC_COND_HIGH_MAX_ENTRIES: '5',
  TPB_PAIRLOCK_REC_COND_MAX_ENTRIES: '5',       // 2026-10-08 synced with the dashboard (was 10). The user, 10-02 21:00: once ALL pairs filled, buy again under the conditions
                                                // (each buy with its own pair bid); the market / bankroll caps stop it first
  TPB_PAIRLOCK_REC_PAIR_SUM: '0.95',            // 2026-10-08 synced with the dashboard (was 0.97)
  // ---- UNFILLED-LEG RECOVERY (the user's 2026-10-05 spec; strategy/pairlock_ulr.py, docs/pairlock_ulr_2026-10-05.md).
  //      A first leg whose pair bid did not fill gets a PARTIAL opposite position as a TRADE once the market clearly
  //      moves against it (opposite ask > 0.50 and >= MIN_MOVE above the bid, opposite momentum up, first leg down,
  //      spot confirming, >= MIN_TIME_REMAINING left, expected move > spread + fees + slippage + margin), sized
  //      INITIAL_RATIO x the unhedged shares, sold at TAKE_PROFIT / STOP_LOSS / signal reversal / before the close; one
  //      entry per position, never an equal-cost pair held to settlement. While it is on, the strategy-R recovery below
  //      does NOT buy and the pair bid is never pulled (it rests until the close), so PAIR_PULL / RECOVERY_ENABLED are 0.
  //      Settings page: "Pair-lock BTC unfilled-leg recovery" (pairbtc.ulr_*). NOT YET RUNNING: the bot was not restarted
  //      with this block (the user reviews the report first).
  // 2026-10-08: OFF -- the bot is started with the strategy-R recovery it last ran (10-05); the ULR replay lost in every split
  TPB_PAIRLOCK_REC_ULR_ENABLED: '0',
  TPB_PAIRLOCK_REC_ULR_MIN_MOVE: '0.08',
  TPB_PAIRLOCK_REC_ULR_MIN_TIME_REMAINING_S: '60',
  TPB_PAIRLOCK_REC_ULR_INITIAL_RATIO: '0.30',
  TPB_PAIRLOCK_REC_ULR_TAKE_PROFIT: '0.08',
  TPB_PAIRLOCK_REC_ULR_STOP_LOSS: '0.04',
  TPB_PAIRLOCK_REC_ULR_MAX_ENTRIES: '1',
  TPB_PAIRLOCK_REC_ULR_MAX_ATTEMPTS: '3',
  TPB_PAIRLOCK_REC_ULR_REQUIRE_SPOT: '1',
  TPB_PAIRLOCK_REC_ULR_SIGNAL_EXIT: '1',
  TPB_PAIRLOCK_REC_ULR_OPPOSITE_MIN_PRICE: '0.50',
  TPB_PAIRLOCK_REC_ULR_MOMENTUM_WINDOW_S: '3',
  TPB_PAIRLOCK_REC_ULR_MOMENTUM_MIN: '0.01',
  TPB_PAIRLOCK_REC_ULR_EXPECTED_MOVE: '0.08',
  TPB_PAIRLOCK_REC_ULR_SAFETY_MARGIN: '0.01',
  TPB_PAIRLOCK_REC_ULR_MAX_SLIPPAGE: '0.02',
  TPB_PAIRLOCK_REC_ULR_EXIT_BEFORE_CLOSE_S: '20',
  TPB_PAIRLOCK_REC_ULR_COOLDOWN_MS: '2000',
  TPB_PAIRLOCK_REC_ULR_MAX_POSITION_SIZE: '30',
  TPB_PAIRLOCK_REC_ULR_MAX_TOTAL_EXPOSURE: '30',
  TPB_PAIRLOCK_REC_ULR_MAX_TRADES_PER_MARKET: '1',
  TPB_PAIRLOCK_REC_PAIR_PULL: '0',              // 2026-10-08 the user: the opposite bid must REST (pulling it at the first flip
                                                // removed it within a second); only the pair rescue below pulls it
  TPB_PAIRLOCK_REC_PAIR_RESCUE_PRICE: '0',      // 2026-10-08 the user: "stop recovery action when the opposite token is over 0.75":
                                                // the rescue buy is OFF (it was 0.75); the opposite bid just rests until the close
  TPB_PAIRLOCK_REC_RECOVERY_ENABLED: '0',       // 2026-10-08: the pair rescue replaces the strategy-R recovery
  TPB_PAIRLOCK_REC_PAIR_REPRICE_DROP: '0',      // 2026-10-08 21:20 UTC the user: "do not set the ask again, keep the first ask until it fills":
                                                // the trailing opposite bid (re-place lower after a fall) is OFF
  TPB_PAIRLOCK_REC_PAIR_REPRICE_MAX: '3',       // ... at most 3 re-placements per bid
  // ---- the recovery: the same values as tpb-pairlockrecovery-paper (ecosystem.pairlockrecovery.config.js)
  TPB_PAIRLOCK_REC_DIRECTION: 'price',          // the recovery side = the higher-priced token (no reversal signals)
  TPB_PAIRLOCK_REC_TARGET_PROFIT: '0.50',
  TPB_PAIRLOCK_REC_MAX_ORDER_SIZE: '60',
  TPB_PAIRLOCK_REC_MAX_CAPITAL: '90',
  TPB_PAIRLOCK_REC_MAX_SLIPPAGE: '0.03',
  // ---- RECOVER AGAIN, EVERY SWING (the user, 2026-10-05: "once it recovered a token, if the token falls again, it
  //      must be recovered again too"). The logs showed recoveries refused by the model edge at nearly every
  //      crossing, and the last swing of every big loss skipped because the WHOLE plan had to fit the book within
  //      MAX_SLIPPAGE and every cap. Now: no edge gate (the price rule decides), each step buys what fits now and
  //      the next step follows; the haircut is off (orders are fill-and-kill: they take what the book holds)
  //      and the action cap counts steps, so 10 -> 30 (capital / position / bankroll are the limits).
  // ---- LOSS LIMITS (the user, 2026-10-05: "keep my Settings, reduce the large losses"). Where a recovery is due but
  //      would leave the other outcome under -MAX_MARKET_LOSS, or its token costs more than RECOVERY_MAX_PRICE, or
  //      fewer than LOCK_LEFT_S seconds are left, or MAX_ACTIONS is used up, the bot LOCKS the market: it buys the
  //      side with fewer shares until UP = DOWN, fixing the result whoever wins. The late buy is trimmed to the
  //      limit. Settings page: Max loss per market / Recovery: highest price / Recovery: lock with time left under.
  TPB_PAIRLOCK_REC_MAX_MARKET_LOSS: '8',
  TPB_PAIRLOCK_REC_RECOVERY_MAX_PRICE: '0.75',
  TPB_PAIRLOCK_REC_LOCK_LEFT_S: '60',
  // ---- RECOVER ONCE (the user, 2026-10-05: "recover the falling token only once, to reduce losses"): one recovery
  //      per market (its steps toward one side count as one); after it the market is held to settlement.
  //      Settings page: Recoveries per market (0 = no limit).
  TPB_PAIRLOCK_REC_MAX_RECOVERIES: '1',
  TPB_PAIRLOCK_REC_EDGE_GATE: '0',
  TPB_PAIRLOCK_REC_STEP_FILL: '1',
  TPB_PAIRLOCK_REC_MIN_EDGE: '0.0',
  TPB_PAIRLOCK_REC_MIN_PRICE_MOVE: '0.05',
  TPB_PAIRLOCK_REC_COOLDOWN_MS: '2000',
  TPB_PAIRLOCK_REC_MAX_ACTIONS: '30',            // steps, not swings (2026-10-05: was 10)
  TPB_PAIRLOCK_REC_MIN_CONFIRMATIONS: '3',
  TPB_PAIRLOCK_REC_SLIPPAGE_ALLOWANCE: '0.005',
  TPB_PAIRLOCK_REC_MIN_TIME_LEFT_S: '15',
  TPB_PAIRLOCK_REC_REQUIRE_MIN_IMPROVEMENT: '0',
  TPB_PAIRLOCK_REC_LATE_ENABLED: '0',           // 2026-10-08 the user: late buy DISABLED (so no late hedge either)
  TPB_PAIRLOCK_REC_LATE_TRIGGER: 'price',
  TPB_PAIRLOCK_REC_LATE_WINDOW_S: '40',         // (late buy off) synced with the dashboard (was 20)
  TPB_PAIRLOCK_REC_LATE_MIN_LEFT_S: '10',
  TPB_PAIRLOCK_REC_LATE_MIN_MID: '0.93',
  TPB_PAIRLOCK_REC_LATE_MAX_PRICE: '0.99',
  TPB_PAIRLOCK_REC_LATE_SHARES: '20',           // (late buy off) synced with the dashboard (was 30)
  TPB_PAIRLOCK_REC_LATE_MAX_ORDERS: '1',
  TPB_PAIRLOCK_REC_LATE_MAX_CAPITAL: '30',
  TPB_PAIRLOCK_REC_LATE_HEDGE_TRIGGER: '0.75',  // (late buy off) synced with the dashboard (was 0.90)
  TPB_PAIRLOCK_REC_LATE_HEDGE_PRICE: '0.40',    // (late buy off) synced with the dashboard (was 0.20)
  TPB_PAIRLOCK_REC_MAX_TOTAL_POSITION: '500',   // 10 pairs of 20 + 30 late + 30 hedge
  TPB_PAIRLOCK_REC_DEPTH_HAIRCUT: '0',          // 2026-10-05: was 0.8 (a step sized to 20 % of the book)
  // ---- the budget: 20 sh at <= 0.62 and a 30-share late buy at <= 0.99 are over B's 10 USDC order cap
  TPB_TRIANGLE_MAX_ORDER_USDC: '60',            // a recovery step is up to 60 shares (as the recovery bot)
  TPB_TRIANGLE_MAX_MARKET_USDC: '100',           // a filled pair ties up ~19.4 USDC until settlement
  TPB_TRIANGLE_MAX_TOTAL_EXPOSURE_USDC: '100',
  TPB_TRIANGLE_MAX_DAILY_LOSS_USDC: '60',
};

module.exports = {
  apps: [
    // 2026-10-08 --keep-history: a restart mid-market restored the position but not its fills in the trade log, so the
    // settlement reconciliation failed (engine +0.16 vs log +2.95) and halted entries; with it the log continues
    { ...B_APP, env: { ...B_APP.env, ...CONDITIONS }, args: B_APP.args.includes('--keep-history') ? B_APP.args : `${B_APP.args} --keep-history` },
  ],
  baseB: B_APP,
};
