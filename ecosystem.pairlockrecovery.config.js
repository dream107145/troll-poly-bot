// STRATEGY R: DYNAMIC LOSS-RECOVERY / REBALANCING (the user's 2026-10-02 spec) -- PAPER ONLY, NOT STARTED by default.
// Since 10-02 ~20:30 UTC its first leg is THE OPEN RULE (below), not the crypto trigger.
//
//   pm2 start ecosystem.pairlockrecovery.config.js --only tpb-pairlockrecovery-paper
//
// The same first leg as tpb-pairlock-paper (its env and args are read from ecosystem.pairlock.config.js: the crypto
// trigger and strategy B's entry gate), then strategy/pairlock_recovery_engine.py manages the market as ONE portfolio:
// a confirmed reversal toward side S while the other side's inventory has moved against it by MIN_PRICE_MOVE ->
//   x = (RECOVERY_TARGET_PROFIT - P&L of the S-win branch) / (1 - executable VWAP(x) - fee - slippage)
// bought in steps of at most MAX_RECOVERY_ORDER_SIZE, each step requiring p_S - VWAP - fee - slippage >= MIN_RECOVERY_EDGE,
// the whole plan inside MAX_TOTAL_POSITION / MAX_RECOVERY_CAPITAL / the bankroll, and the depth within MAX_SLIPPAGE.
// Works both ways any number of times (UP-heavy -> DOWN-heavy -> UP-heavy ...): after a recovery, when the first token
// rises again the engine buys it again, sized to lift ITS branch to the target, and so on at every swing (the user,
// 10-02: "repeat this process"); each swing roughly doubles the position, so the bankroll ends the sequence.
// No independent re-entry while holding.
// Backtest and the sequence table: docs/pairlock_dynamic_recovery_2026-10-02.md, results/pairlock_recovery_*.
// Own data tag pairlock-recovery-paper, --keep-history (a restart appends to the trade log).
// The app name must not share a prefix with another app: pm2 --only matches by prefix.

// baseB: tpb-pairlock-paper as it was before its conditions rule (10-02 20:40), so that rule never reaches this bot
const base = require('./ecosystem.pairlock.config.js').baseB;
if (!base || base.name !== 'tpb-pairlock-paper') throw new Error('baseB not found in ecosystem.pairlock.config.js');

const RECOVERY = {
  TPB_PAIRLOCK_STRATEGY: 'R',
  // ---- the spec's parameters (TPB_PAIRLOCK_<SPEC NAME>; the engine also accepts TPB_PAIRLOCK_REC_<field>)
  TPB_PAIRLOCK_RECOVERY_TARGET_PROFIT: '0.50',   // USDC the favoured branch is lifted to, fees + slippage in (10-share legs)
  TPB_PAIRLOCK_MAX_RECOVERY_ORDER_SIZE: '60',    // shares per recovery order (a larger plan proceeds in steps)
  TPB_PAIRLOCK_MAX_TOTAL_POSITION: '300',        // shares, both sides, one market (the user, 10-02: keep rebalancing as the price swings)
  TPB_PAIRLOCK_MAX_RECOVERY_CAPITAL: '90',       // USDC spent on recovery buys in one market (the paper bankroll is 100)
  TPB_PAIRLOCK_MAX_SLIPPAGE: '0.03',             // the VWAP (and the FOK limit) at most this above the best ask
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
  TPB_PAIRLOCK_REC_MAX_MARKET_LOSS: '35',        // 2026-10-05 the user: 8 -> 25 -> 35 (17:30 UTC)
  TPB_PAIRLOCK_REC_RECOVERY_MAX_PRICE: '1.0',    // off (2026-10-05): it would block the recoveries above 0.70
  TPB_PAIRLOCK_REC_LOCK_LEFT_S: '60',
  // ---- RECOVER ONCE (the user, 2026-10-05: "recover the falling token only once, to reduce losses"): one recovery
  //      per market (its steps toward one side count as one); after it the market is held to settlement.
  //      Settings page: Recoveries per market (0 = no limit).
  TPB_PAIRLOCK_REC_MAX_RECOVERIES: '0',          // 2026-10-05 the user: "repeat the recovery, not once" (0 = no limit)
  // ---- RECOVER ONLY ON A CLEAR SWING (the user, 2026-10-05): a recovery is bought only once the token it buys has
  //      an ask above this; below it the bot waits. Settings page: Recovery: lowest price.
  TPB_PAIRLOCK_REC_RECOVERY_MIN_PRICE: '0.70',   // the user, 2026-10-05 16:15: recover only once the opposite token's ask is
                                                 // OVER 0.70 ("to prevent too many recoveries"); below it the bot waits
  // ---- 2026-10-05 16:05 the user: no other-side bid after the first buy (the recovery alone manages the position)
  TPB_PAIRLOCK_REC_PAIR_BID: '0',
  TPB_PAIRLOCK_REC_RECOVERY_WAIT_ABOVE: '1.0',   // off
  TPB_PAIRLOCK_REC_EDGE_GATE: '0',
  TPB_PAIRLOCK_REC_STEP_FILL: '1',
  TPB_PAIRLOCK_MIN_RECOVERY_EDGE: '0.0',         // per share: P(S) - VWAP - fee - slippage allowance
  TPB_PAIRLOCK_MIN_PRICE_MOVE: '0.05',           // the other side's bid under its average cost
  TPB_PAIRLOCK_RECOVERY_COOLDOWN: '2000',        // ms between recovery orders
  TPB_PAIRLOCK_MAX_RECOVERY_ACTIONS: '30',       // steps, not swings (2026-10-05: was 10)       // each swing of the price may add a step; the bankroll is the real limit
  TPB_PAIRLOCK_RECOVERY_MIN_CONFIRMATIONS: '3',  // reversal signals toward S, of 4
  TPB_PAIRLOCK_REC_SLIPPAGE_ALLOWANCE: '0.005',
  TPB_PAIRLOCK_REC_MIN_TIME_LEFT_S: '15',
  TPB_PAIRLOCK_REC_DEPTH_HAIRCUT: '0',           // 2026-10-05: was 0.8 (a step sized to 20 % of the book)         // 80 % of every ask level assumed taken by faster traders (as the replay)
  TPB_PAIRLOCK_REC_REQUIRE_MIN_IMPROVEMENT: '0', // 1 = also require MIN_SETTLEMENT_PNL to rise (stops at the balanced size)
  // ---- THE OPEN RULE (the user, 10-02 20:00): in the first 10 s after a market opens buy the HIGHER-priced token,
  //      then rest a GTC bid for the OTHER token at PAIR_SUM - the fill price (default 0.97; settable on the dashboard's
  //      Variables page as pairlock.pair_sum). If that bid has not filled and the other token becomes the higher price,
  //      the bid is pulled and the loss-recovery arithmetic above takes over, its side = the higher-priced token
  //      (DIRECTION=price; the 4 reversal signals are not used). The spot-shock trigger buys nothing in this mode.
  TPB_PAIRLOCK_REC_ENTRY_MODE: 'open',
  TPB_PAIRLOCK_REC_OPEN_WINDOW_S: '10',
  TPB_PAIRLOCK_REC_OPEN_DELAY_S: '5',           // 2026-10-05 the user: start buying 5 s after the market opens
  TPB_PAIRLOCK_REC_PROFIT_EXIT: '1.50',         // 2026-10-05 the user: after a recovery, sell everything once the position is over this at the bids (0.50; 2026-10-06 00:05 raised to 1.50)
  TPB_PAIRLOCK_REC_RECOVERY_SELL: '1',          // 2026-10-05 the user: "sell falling tokens when recover" (the other side's tokens, at the bid, after a recovery fill)
  // 2026-10-06 21:40 the user (paper bot): "when recovering, buy the same amount as the falling tokens, on the second,
  // third recovery" -> from recovery 2 on, buy the rising token only until UP = DOWN (Settings: pairlock.even_out_from);
  // 21:50 the user: "first recovery should be same with second recovery" -> from recovery 1 (every recovery evens out)
  TPB_PAIRLOCK_REC_EVEN_OUT_FROM: '1',
  // ... and "if < 90 s left, buy high price tokens in price 0.85-0.95 every 20 s" (held markets only; Settings pairlock.ladder_*)
  TPB_PAIRLOCK_REC_LADDER_WINDOW_S: '90',
  TPB_PAIRLOCK_REC_LADDER_MIN_LEFT_S: '10',
  TPB_PAIRLOCK_REC_LADDER_MIN_PRICE: '0.85',
  TPB_PAIRLOCK_REC_LADDER_MAX_PRICE: '0.95',
  TPB_PAIRLOCK_REC_LADDER_INTERVAL_S: '20',
  TPB_PAIRLOCK_REC_LADDER_SHARES: '10',
  TPB_PAIRLOCK_REC_OPEN_MIN_PRICE: '0.50',
  TPB_PAIRLOCK_REC_OPEN_MAX_PRICE: '0.95',
  TPB_PAIRLOCK_REC_OPEN_SHARES: '10',
  TPB_PAIRLOCK_REC_OPEN_LIMIT_OFFSET: '0.02',
  TPB_PAIRLOCK_REC_OPEN_MAX_TRIES: '3',
  TPB_PAIRLOCK_REC_PAIR_SUM: '0.97',
  TPB_PAIRLOCK_REC_DIRECTION: 'price',
  // ---- LATE BUY (the user, 10-02: "at the last few seconds, buy winning tokens to increase profits more"): in the last
  //      20..10 s (orders under TPB_TRIANGLE_MIN_TIME_REMAINING_S 10 are refused by the bot) buy one clip of the side the
  //      model gives >= LATE_MIN_P, if its ask <= LATE_MAX_PRICE and P - VWAP - fee - slippage >= LATE_MIN_EDGE
  TPB_PAIRLOCK_REC_LATE_ENABLED: '1',
  TPB_PAIRLOCK_REC_LATE_WINDOW_S: '20',
  TPB_PAIRLOCK_REC_LATE_MIN_LEFT_S: '10',
  TPB_PAIRLOCK_REC_LATE_MIN_P: '0.95',
  TPB_PAIRLOCK_REC_LATE_MAX_PRICE: '0.97',
  TPB_PAIRLOCK_REC_LATE_MIN_EDGE: '0.0',
  TPB_PAIRLOCK_REC_LATE_SHARES: '10',
  TPB_PAIRLOCK_REC_LATE_MAX_ORDERS: '2',
  TPB_PAIRLOCK_REC_LATE_MAX_CAPITAL: '20',
  // ---- this app's own budget. 2026-10-07 swing mode: one market may spend up to MAX_MARKET_CAPITAL (125 + fees), its
  //      swing-2 order is 80 shares (~56-79 USDC), and the previous market's capital stays tied up until its resolution
  //      arrives (minutes after the close), so: order 100, market 150 (asset cap = 2 x market), total 300. Was 60/100/100.
  TPB_TRIANGLE_MAX_ORDER_USDC: '100',
  TPB_TRIANGLE_MAX_MARKET_USDC: '150',
  TPB_TRIANGLE_MAX_SIMULTANEOUS_POSITIONS: '4',
  TPB_TRIANGLE_MAX_TOTAL_EXPOSURE_USDC: '300',
  TPB_TRIANGLE_MAX_DAILY_LOSS_USDC: '120',   // 2026-10-06 01:18 the user: new daily-loss cap (was 60; the risk manager's own limit, which halted the live bot at -61.26)
  // ---- 2026-10-07 the user's LIMITED-DEPTH DIRECTIONAL RECOVERY WITH A TWO-SIDED HEDGE AT SWING 3+ (strategy/pairlock_swing.py).
  //      It REPLACES every rule above that buys or sells after the first leg (open rule, recovery arithmetic, lock, even-out,
  //      late buy, ladder, profit exit, falling-side sale, pair bid): those stay in the code and in this env for the other
  //      modes and for the live bot (ecosystem.pairlockrecovery.live.config.js pins swing mode OFF; __main__ refuses it on
  //      real money). Swing 0: 10 sh of the leader at an executable VWAP 0.55-0.60, 20-30 s after the open. Swing = the
  //      token against the position leads by the hysteresis with its ask at 0.70. Swing 1: 24 sh, swing 2: 80 sh of the
  //      new leader. Swing 3+: buy the side with fewer shares until UP = DOWN; never another directional buy.
  //      Variables page: group "Pair-lock swing recovery" (pairlock.swing_*).
  TPB_PAIRLOCK_SWING_MODE: '1',
  TPB_PAIRLOCK_SWING_INITIAL_SHARES: '10',
  TPB_PAIRLOCK_SWING_INITIAL_ENTRY_MIN: '0.55',
  TPB_PAIRLOCK_SWING_INITIAL_ENTRY_MAX: '0.60',
  TPB_PAIRLOCK_SWING_INITIAL_ENTRY_DELAY_S: '20',
  TPB_PAIRLOCK_SWING_INITIAL_ENTRY_WINDOW_S: '30',
  TPB_PAIRLOCK_SWING_RECOVERY_PRICE: '0.70',
  // 2026-10-08 the user: NO fixed recovery sizes -- swing 1 / swing 2 buy the smallest whole number of shares whose win pays more
  // than TARGET_RECOVERY_PROFIT from the whole inventory and cost so far, priced on the walked ask book with fees (swing 2 starts
  // from what swing 1 left); refused (nothing bought) past MAX_RECOVERY_SHARES / MAX_RECOVERY_PRICE / the depth / the capital cap
  TPB_PAIRLOCK_SWING_SIZING: 'target',
  TPB_PAIRLOCK_SWING_TARGET_RECOVERY_PROFIT: '3.00',
  TPB_PAIRLOCK_SWING_MAX_RECOVERY_SHARES: '150',
  TPB_PAIRLOCK_SWING_MAX_RECOVERY_PRICE: '0.85',
  TPB_PAIRLOCK_SWING_MAX_RECOVERY_SWING: '2',
  TPB_PAIRLOCK_SWING_ENABLE_TWO_SIDED_HEDGE: '1',
  TPB_PAIRLOCK_SWING_MAX_MARKET_CAPITAL: '125',   // the spec's default; fees (~2.7 on the default path) make the full 66-share
                                                  // hedge need ~128: raise it (e.g. 150) on the Variables page for the full hedge
  TPB_PAIRLOCK_SWING_LEADER_HYSTERESIS: '0.04',
  // 2026-10-07 the user: this bot ignores the dashboard's shared drawdown row (paper scope 30: one swing-3 loss, ~-44, would
  // halt it and refuse the hedges of markets still open) and keeps its own budget: drawdown 2 x the daily loss = 240
  TPB_CONTROLS_IGNORE: 'risk.max_drawdown_usdc',
};

// 2026-10-06 01:05 the user: "start the paper bot in the same logic and parameters as the live bot" -- the paper bankroll is the
// live account's cash at that moment (the strategy settings are shared through the controls file's "all" scope already)
// 2026-10-06 13:50 the user: start the paper bot from 100. 2026-10-07 swing mode: one market may need 125+ (and the previous
// market's capital is held until its resolution), so 100 could never fund the swing-3 hedge: 300.
const PAPER_BALANCE = '300';
const args = base.args.replace('--data-tag pairlock-btc-paper', '--data-tag pairlock-recovery-paper --keep-history')
  .replace(/--balance \S+/, `--balance ${PAPER_BALANCE}`);
if (args === base.args) throw new Error('could not set the data tag');

module.exports = {
  apps: [
    { ...base, name: 'tpb-pairlockrecovery-paper', env: { ...base.env, ...RECOVERY }, args },
  ],
};
