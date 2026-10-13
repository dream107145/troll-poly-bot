// REPRICED_MOMENTUM (strategy/repriced_momentum.py) -- PAPER ONLY. Kept out of
// ecosystem.config.js and ecosystem.spotlag.config.js so nothing starts it as a
// side effect:
//
//   pm2 start ecosystem.repricedmom.config.js --only tpb-repricedmom-paper
//
// The research verdict is NEGATIVE (docs/repriced_momentum_2026-09-24.md): no
// configuration made money on the held-out periods. This paper run exists to check
// that verdict on live books, not to find a way to trade it. There is deliberately
// no shadow or real-money app here; the engine refuses micro-live/live unless
// TPB_REPRICED_MOM_ALLOW_REAL=1, which nothing in this repository sets.
//
// It writes TAGGED files (data/*.repricedmom-paper.*), including every candidate
// with its features and REJECT_* reason (data/repriced_momentum_candidates.
// repricedmom-paper.jsonl), so it runs beside spot-lag without touching its state,
// ledger or statistics. Every ledger and forensics row carries strategy
// "repriced_momentum".
//
// The rule below is the one the research LOCKED on training data: 25-50 % repriced,
// spot >= 1 sigma over 250 ms, token following, ask not past fair, sold 2 s after
// the fill (now: a +10c take-profit, else 20 s). Its expected-edge gate (continuation >= costs + 1c) was turned OFF on
// 2026-09-24: with it the bot sent no order in 2.7 h. Without it, the replay of the
// live bot's own gates and budget (data/replay/rm_research/livesim.md) gives about
// 8-13 orders/h, 3-4 fills/h, a 30 % fill rate and about -0.8c/share -- still not a
// proven edge (docs/repriced_momentum_research_2026-09-24.md).
// The code defaults are the brief's first configuration instead (50-90 %, 1c
// overshoot, 1c edge, reversal exit) -- which the replay never lets trade: its
// expected continuation is always below the round-trip cost.

const path = require('path');

const ROOT = __dirname;
const PYTHON = path.join(ROOT, '.venv', 'bin', 'python');

// the strategy's OWN budget; the bot applies the tighter of it and its global limits
const BUDGET = {
  TPB_REPRICED_MOM_MAX_SIMULTANEOUS_POSITIONS: '1',
  TPB_REPRICED_MOM_MAX_TOTAL_EXPOSURE_USDC: '5',
  TPB_REPRICED_MOM_MAX_ORDER_USDC: '3',            // 5 shares -> asks up to 0.60
  TPB_REPRICED_MOM_MAX_DAILY_LOSS_USDC: '5',
  TPB_REPRICED_MOM_MAX_BUYS_PER_HOUR: '60',
  TPB_REPRICED_MOM_MAX_ENTRIES_PER_MARKET: '1',
  TPB_REPRICED_MOM_MAX_ATTEMPTS_PER_MARKET: '3',
};

// the locked research rule (data/replay/repriced_momentum_params.json)
const LOCKED = {
  TPB_REPRICED_MOM_MIN_PCT: '0.25',
  TPB_REPRICED_MOM_MAX_PCT: '0.50',
  TPB_REPRICED_MOM_MOMENTUM_HORIZON_MS: '250',
  TPB_REPRICED_MOM_MIN_SPOT_Z: '1.0',
  TPB_REPRICED_MOM_MAX_OVERSHOOT_CENTS: '0',
  TPB_REPRICED_MOM_MIN_EDGE_CENTS: '1',
  // 2026-09-24 19:5x: OFF. With it on the bot sent no order in 2.7 h: the expected continuation
  // (1.5-4c) never covers a round trip (4-6c). Off: ~8-13 orders/h, ~3-4 fills/h, fill ~30 %,
  // about -0.8c/share on the archive (livesim.md); limit stays AT the ask
  TPB_REPRICED_MOM_EDGE_GATE: '0',
  TPB_REPRICED_MOM_EXIT_MODE: 'timer',
  // Exit (the user's choice, live from 2026-09-24 20:54 UTC): sell once the bid is +10c above the entry
  // (at the bid), else after 20 s. Replay of the live bot with the +2c limit: take-profit hit
  // 29 %, win 40 %, avg hold 17 s, -0.60c/share (t -0.6) against -0.75c for the previous
  // +3c / 10 s exit (20:10 UTC) -- the same within noise, not a proven edge
  TPB_REPRICED_MOM_TAKE_PROFIT_CENTS: '10',
  TPB_REPRICED_MOM_HOLD_S: '20',
  // 2026-09-24 20:2x (the user's choice): shocks evaluated the moment the spot quote
  // arrives, not on the 200 ms tick (live.py _maybe_shock_eval, as spot-lag paper).
  // No rule changes. Live A/B 19:45-20:19: 310 shocks seen vs 146, 135 reached the
  // 25-50 % zone vs 55, first-look repricing median 56 % vs 75 %. Orders were not
  // shown to rise: the zone's remaining gates (token agreement, book age) still bind.
  TPB_REPRICED_MOM_EVENT_DRIVEN: '1',
  // 2026-09-24 20:4x (the user's choice, for fill rate): the FOK limit may be up to 2c above
  // the ask (it still fills at the venue's ask on arrival, never above the limit). Replay of
  // the live bot with the +3c take-profit: fill rate 29 % -> 42 %, fills/h 3.7 -> 5.0, net
  // +0.10 -> -0.84c/share. With 5 shares at <= $3, asks now stop at 0.58
  TPB_REPRICED_MOM_LIMIT_OFFSET_CENTS: '2',
};

// 2026-10-01 (the user's brief): the app name tpb-repricedmom-paper now runs the TRIANGLE rule, PAPER ONLY
// (strategy/triangle.py): every FILLED sudden-move triangle the dashboard draws on the crypto chart (a
// spot-lag signal: >= 3 sigma 1 s shock, 3 s agreeing, accelerating, 30 s trend not against it) buys ONE
// share of its side -- ▲ Up, ▼ Down -- FOK at 0.99 (a market order), every share held to settlement. Spot-lag's
// post-signal gates (one entry per market, lag / edge, price band, spread, size) are NOT applied. The
// signal settings, sources and assets are the paper mirror's (tpb-spotlag-paper-mirror), so the filled
// triangles are the same ones its chart shows. No study stands behind this rule: it is measured here.
// Own files: data/*.triangle-paper.* (the repriced-momentum history keeps its own tag). The budget is wide
// on purpose (the rule is "every triangle"): 1 share per order, <= 50 USDC per market, 400 USDC open,
// 250 USDC daily loss on a 500 USDC paper bankroll. __main__ refuses micro-live/live/shadow for it.
//   pm2 start ecosystem.repricedmom.config.js --only tpb-repricedmom-paper
const TRIANGLE = {
  PYTHONUNBUFFERED: '1',
  TPB_TRIANGLE_ENABLED: '1',
  // the paper mirror's signal (TriangleConfig defaults; spelled out so a change is visible here)
  TPB_TRIANGLE_K_SIGMA: '1',                 // 10-01 entry model: every >= 1 sigma move is a CANDIDATE; the model decides
  TPB_TRIANGLE_TREND_MIN_BPS: '0',
  TPB_TRIANGLE_FV_TRIGGER_CENTS: '0',
  TPB_TRIANGLE_MAX_SPOT_AGE_MS: '500',
  TPB_TRIANGLE_EXCLUDE_ASSETS: 'HYPE',
  TPB_TRIANGLE_EVENT_DRIVEN: '1',
  TPB_SPOT_LEAD_WINDOW_MS: '0',
  TPB_BOOK_SKIP_ASSETS: 'HYPE',
  // the order: 1 share, held to settlement.
  // 2026-10-01 ~02:05 UTC (the user: "improve fill rate, ideal fill rate is 100%"): the FOK limit is 0.99, the
  // venue's maximum -- in effect a market order; it fills at whatever the ask is when it lands. With ask + 2c,
  // 21 of the first 59 orders (36 %) were killed: the paper order takes ~770 ms (p50) to land and on the kills
  // the ask had risen 6-18c by then (the triangle's move repricing the token). Those extra fills are bought
  // at that higher price.
  // 2026-10-01 ~02:04 UTC (the user): BTC only, 5 shares per order (the venue minimum), a 100 USDC bankroll,
  // fill rate up: FAK (fill-and-kill: takes whatever the book holds up to 5 shares at <= 0.99, so a thin book
  // gives a partial fill instead of a kill), new data tag triangle-btc-paper (a fresh 100 USDC ledger)
  TPB_TRIANGLE_SHARES: '5',
  // 2026-10-01 ~14:10 UTC (the user: "trade ALL sudden-move triangles, hollow ones too; price paid is wrong"):
  // - every sudden-move detection (|z1| >= 3 sigma) is an order: the trend filter, the 3 s agreement, the
  //   acceleration test and the 2 s dedupe are off (one order per direction per market per second = one per
  //   triangle drawn), and the buy no longer waits for a pair
  // - the price: FOK at the ask seen at the triangle + 3c (was 0.94: fills landed up to 15c higher). Orders
  //   the market moves past are killed and their triangle is drawn hollow. Dashboard: "Max price above the ask"
  // 2026-10-01 ~15:30 UTC (the user's strategy): the triangle IS a cross-exchange momentum + order-flow signal
  // (strategy/flowsignal.py): every Coinbase / Bybit / Binance quote and trade scores a direction at once,
  //   score = .35 cross-exchange momentum + .30 aggressive trade flow + .20 acceleration + .15 first mover,
  //   |score| >= 0.70 and every part agreeing -> UP / DOWN. Windows 20 / 50 / 100 ms. Scale calibrated on
  //   recorded ticks (09-23, 09-29): ~130-145 signals / hour, 93-96 % followed by a move the same way within
  //   0.5-1 s. Spot-lag's own 1-second shock is no longer traded or drawn (SIGNAL_SOURCE=spot_lag restores it).
  TPB_TRIANGLE_SIGNAL_SOURCE: 'spot_lag',    // 10-01: the entry model was fitted on spot-move candidates (results/triangle_entry_model.md); flow is unvalidated on the archive
  TPB_TRIANGLE_FLOW_EXCHANGES: 'coinbase,bybit,binance',
  TPB_TRIANGLE_FLOW_WINDOWS_MS: '20,50,100',
  TPB_TRIANGLE_FLOW_REF_BP: '0.6',
  TPB_TRIANGLE_FLOW_ACCEL_REF_BP: '0.6',
  TPB_TRIANGLE_FLOW_MIN_BP: '0.2',
  TPB_TRIANGLE_FLOW_GATE_BP: '0.15',
  TPB_TRIANGLE_FLOW_THRESHOLD: '0.70',
  TPB_TRIANGLE_FLOW_DEDUPE_MS: '1000',
  // 2026-10-01 ~16:55 UTC (the user: "the bot buys after the rise ended; the signal is too late"): NO CHASING -- a
  // signal is skipped when the token's ask already rose more than 1c in the last second (the reprice happened)
  TPB_TRIANGLE_FLOW_MAX_TOKEN_MOVE: '0.01',
  TPB_TRIANGLE_FLOW_TOKEN_LOOKBACK_MS: '1000',
  // 2026-10-01 ~15:15 UTC (the user): (1) never buy a token priced under 0.05 or over 0.96; (2) a token above
  // 0.85 that is still pricing up buys 5 shares every 5 s (with its other-side ask). The base engine only looks
  // at markets with >= 60 s left, but the high zone mostly lives in the last minute: the window opens at 10 s and
  // flow signals keep their own 60 s minimum (FLOW_MIN_LEFT_S).
  // 2026-10-01 ~16:50 UTC (the user: "buy tokens only in price range 0.45-0.55"); was 0.05-0.96, then 0.20-0.96.
  // Note: the high-zone rule (ask > 0.85) can never buy inside this band, so it is idle while the band holds.
  TPB_TRIANGLE_MIN_PRICE: '0.20',
  TPB_TRIANGLE_MAX_PRICE: '0.96',           // 10-01: the study's band; the model's own max price is below
  TPB_TRIANGLE_HZ_ENABLED: '1',
  TPB_TRIANGLE_HZ_MIN_PRICE: '0.85',
  TPB_TRIANGLE_HZ_EVERY_S: '5',
  TPB_TRIANGLE_HZ_LOOKBACK_S: '5',
  TPB_TRIANGLE_MIN_TIME_REMAINING_S: '10',
  TPB_TRIANGLE_FLOW_MIN_LEFT_S: '60',
  TPB_TRIANGLE_LIMIT_OFFSET: '0.03',
  TPB_TRIANGLE_WAIT_FOR_PAIR: '0',
  TPB_TRIANGLE_TREND_WINDOW_S: '0',
  TPB_TRIANGLE_K3_SIGMA: '-99',
  TPB_TRIANGLE_REQUIRE_ACCELERATION: '0',
  TPB_TRIANGLE_DEDUPE_S: '1',
  // 2026-10-01 12:50 UTC (the user: "it doesn't always place the other side's order, very risky; it doesn't
  // buy for each triangle"): FOK -- 5 shares or nothing, so every fill can carry a 5-share pair (FAK never
  // partially filled in 384 fills, so nothing is lost). With the pair on, buys stop at 0.94 (0.95 - one tick:
  // TriangleConfig.pair_room_required) and the limit is capped there, so every fill gets a pair >= 0.01.
  TPB_TRIANGLE_ORDER_TYPE: 'FOK',
  TPB_TRIANGLE_PAIR_ROOM_REQUIRED: '1',
  // entries resume 5 s (not 30 s) after a feed trip clears; paper only. 10 of 214 filled triangles in
  // 9.7 h fell in the 30 s pause after a Polymarket book-feed reconnect
  TPB_GUARD_COOLDOWN_S: '5',
  // ---- 2026-10-01 ENTRY MODEL (the brief: "find the exact buy point"): scripts/triangle_audit/entry_model.py,
  // lock data/replay/triangle_entry_locked.json. Logistic P(win) on crypto momentum / acceleration / order flow,
  // token velocity, depth, spread, time left, price, fair-value gap, UP+DOWN pressure; buy when
  // EV = P(win) - (ask + 1c) - fee >= 0.02. Chosen on VALIDATION; TEST +2.28 / FORWARD +2.36 USDC per market
  // (t 4.2 / 5.3) against today's rules +0.63 / +0.39. The archive is 200 ms: real fills may be worse.
  TPB_TRIANGLE_ENTRY_MODEL: '1',
  TPB_TRIANGLE_ENTRY_MODEL_PATH: 'data/replay/triangle_entry_locked.json',
  TPB_TRIANGLE_ENTRY_MIN_EV: '0.02',
  TPB_TRIANGLE_ENTRY_MAX_PRICE: '0.96',
  TPB_TRIANGLE_ENTRY_MIN_LEFT_S: '60',
  // ---- 2026-10-01 ~20:20 UTC (the user: "when you buy one side token, set an order on the opposite side; the sum
  // of the two must be under 0.95"): RESTING pair again (was 'ev' from 17:37). After every fill a GTC BUY of the
  // other side at the highest tick with fill + pair < 0.95 (PAIR_COST_STRICT), same shares, until the close; buys
  // stop at 0.93 so every fill gets one. Told the user: the audit measured the resting pair negative in every
  // split (it fills when the first leg is winning and stays empty when it is losing). Their decision.
  TPB_TRIANGLE_HEDGE_MODE: 'resting',
  TPB_TRIANGLE_HEDGE_EV_MARGIN: '0.05',
  TPB_TRIANGLE_HEDGE_SLIPPAGE: '0.03',
  TPB_TRIANGLE_ASSETS: 'BTC',
  // 2026-10-01 ~02:20 UTC (the user: "when the bot buys a token at a price, make an order for the other token
  // at 0.95 - that price"): after each fill, a resting (GTC) BUY of the other side at 0.95 - the average fill
  // price (rounded down to the tick), the same shares, until the market closes. Filled, the pair cost 0.95
  // and pays 1.00 at settlement. The paper venue fills a resting buy only once the ask trades BELOW it.
  // Resting pair orders live in the paper venue's memory: a restart drops them.
  // ONE SOURCE OF TRUTH for the pair total (2026-10-01 audit, item 10): this file. The dashboard override that
  // carried 0.96 while this said 0.95 was cleared; the dashboard shows the running value and may still change it
  // deliberately (a CONTROL line in the log says so).
  TPB_TRIANGLE_PAIR_COST: '0.95',
  TPB_TRIANGLE_PAIR_COST_STRICT: '1',
  TPB_TRIANGLE_MAX_PAIR_COST: '0.96',           // (executable mode only; hedge mode / slippage are set once, above)
  TPB_TRIANGLE_HOLD_TO_SETTLEMENT: '1',
  // budget
  TPB_TRIANGLE_MAX_ORDER_USDC: '5',           // 5 shares x 0.99
  TPB_TRIANGLE_MAX_MARKET_USDC: '50',
  TPB_TRIANGLE_MAX_SIMULTANEOUS_POSITIONS: '50',
  TPB_TRIANGLE_MAX_TOTAL_EXPOSURE_USDC: '100',
  TPB_TRIANGLE_MAX_DAILY_LOSS_USDC: '100',
};

// ---- 2026-10-01 ~22:45 UTC PAIRED-EXIT MOMENTUM (the user's redesign brief; docs/paired_exit_2026-10-01.md,
// scripts/paired_exit/*, results/paired_exit_*.md). TPB_TRIANGLE_PX_ENABLED=1 REPLACES the triangle trigger, the old
// entry model and the resting pair: the old rule's pair filled when the first leg was winning (+0.24) and stayed empty
// when it was losing (-3.6): replay -0.76..-1.44 USDC per window in every split, every asset.
// Every value below was chosen on TRAIN / VALIDATION (BTC) and checked on TEST / FORWARD and a daily walk-forward:
//   entry   |1 s spot z| >= 2 (previous window's sigma), exchanges agreeing >= 0.34, fair P(side) from the logistic
//           model data/replay/paired_exit/live/fair_btc.json (fitted on all archived BTC windows), EV = fair - ask -
//           fee - 0.37c slippage... - 3.3c adverse selection >= +0.02 (+0.04 under 180 s left), ask 0.15-0.85,
//           60-290 s left, FOK 5 shares at ask + 3c
//   exits   each leg's OWN: a resting take-profit ask at max(fill + 2c, fair + 3c), re-priced as fair moves, and the
//           time exit at 30 s left unless the bid is >= 0.95. NO price stop / thesis exit / max hold: every one of them
//           lost money on validation and in the walk-forward (a 40c stop: -0.016 vs +0.023 per window OOS) -- the
//           size cap bounds a leg's loss at 5 x 0.85 = 4.25 USDC. The switches exist (PX_STOP_LOSS, PX_THESIS_MODE,
//           PX_MAX_HOLD_SECONDS) and are OFF on purpose.
//   pair    the opposite token is OFF (structure E lost in every split); PX_PAIR_ENABLED=1 makes it an option
//   caps    one open leg at a time, <= 3 buys and <= 10 USDC per window, <= 5 USDC unpaired / directional
//   kill    20 USDC daily loss, 6 losses in a row -> 15 min pause, stale spot / book / signal, round trip > 1.5 s,
//           spread > 5c, exchanges disagreeing > 6 sigma, 3 unexpected settlements
// What the study says it is worth: TEST +0.06 / FORWARD +0.08 USDC per window, walk-forward +0.023 (t 0.75) --
// NOT a proven edge. It removes the -50 USDC windows (worst window -4.25). PAPER ONLY, BTC only.
const PAIRED_EXIT = {
  TPB_TRIANGLE_PX_ENABLED: '1',
  TPB_TRIANGLE_PX_MODEL_DIR: 'data/replay/paired_exit/live',
  TPB_TRIANGLE_PX_CHART_DIR: 'data/charts',
  TPB_TRIANGLE_PX_MOMENTUM_THRESHOLD: '2.0',
  TPB_TRIANGLE_PX_MIN_AGREE: '0.34',
  TPB_TRIANGLE_PX_ENTRY_MIN_EDGE: '0.02',
  TPB_TRIANGLE_PX_ENTRY_MIN_CONFIDENCE: '0',
  TPB_TRIANGLE_PX_COST_SLIPPAGE: '0.0034',
  TPB_TRIANGLE_PX_COST_ADVERSE: '0.0333',
  TPB_TRIANGLE_PX_COST_LATENCY: '0',
  TPB_TRIANGLE_PX_MIN_PRICE: '0.15',
  TPB_TRIANGLE_PX_MAX_PRICE: '0.85',
  TPB_TRIANGLE_PX_LIMIT_OFFSET: '0.03',
  TPB_TRIANGLE_PX_SHARES: '5',
  TPB_TRIANGLE_PX_MIN_LEFT_S: '60',
  TPB_TRIANGLE_PX_MAX_LEFT_S: '290',
  TPB_TRIANGLE_PX_LATE_LEFT_S: '180',
  TPB_TRIANGLE_PX_LATE_EDGE_ADD: '0.02',
  TPB_TRIANGLE_PX_MAX_WINDOW_USDC: '10',
  TPB_TRIANGLE_PX_MAX_POSITION_USDC: '5',
  TPB_TRIANGLE_PX_MAX_UNPAIRED_USDC: '5',
  TPB_TRIANGLE_PX_MAX_DIRECTIONAL_USDC: '5',
  TPB_TRIANGLE_PX_MAX_OPEN_POSITIONS: '1',
  TPB_TRIANGLE_PX_MAX_BUYS_PER_WINDOW: '3',
  TPB_TRIANGLE_PX_STOP_LOSS: '0',
  TPB_TRIANGLE_PX_STOP_CROSS: '0.02',
  TPB_TRIANGLE_PX_MAX_HOLD_SECONDS: '0',
  TPB_TRIANGLE_PX_THESIS_MODE: '',
  TPB_TRIANGLE_PX_THESIS_DROP: '0.30',
  TPB_TRIANGLE_PX_REVERSAL_THRESHOLD: '2.5',
  TPB_TRIANGLE_PX_TIME_EXIT_SECONDS: '30',
  TPB_TRIANGLE_PX_SETTLE_KEEP: '0.95',
  TPB_TRIANGLE_PX_LADDER_LEVELS: '0.02',
  TPB_TRIANGLE_PX_LADDER_SIZE_PERCENTAGES: '100',
  TPB_TRIANGLE_PX_LADDER_MODE: 'maker',
  TPB_TRIANGLE_PX_LADDER_FAIR_OFFSET: '0.03',
  TPB_TRIANGLE_PX_LADDER_REPRICE_S: '0.4',
  TPB_TRIANGLE_PX_PAIR_ENABLED: '0',
  TPB_TRIANGLE_PX_PAIR_TARGET: '0.95',
  TPB_TRIANGLE_PX_MAX_DAILY_LOSS: '20',
  TPB_TRIANGLE_PX_MAX_CONSECUTIVE_LOSSES: '6',
  TPB_TRIANGLE_PX_LOSS_PAUSE_S: '900',
  TPB_TRIANGLE_PX_MAX_DATA_AGE_MS: '1500',
  TPB_TRIANGLE_PX_MAX_SIGNAL_AGE_MS: '600',
  TPB_TRIANGLE_PX_MAX_EXECUTION_LATENCY_MS: '1500',
  TPB_TRIANGLE_PX_MAX_SPREAD: '0.05',
  // 2026-10-02 00:12 UTC (the user's OK): 3 -> 6. At 3 the check refused 14 of 24 live signals in the first hour
  // (values 3.0-3.7: it divides the exchanges' 1 s return spread by the window's sigma, and BTC sigma was low,
  // 0.19-0.25 bp/s). It was never part of the tested rules; at 6 it only catches feed glitches (~0.2-1 % of
  // archived signals)
  TPB_TRIANGLE_PX_MAX_DISAGREEMENT: '6',
  TPB_TRIANGLE_PX_MAX_SETTLE_MISMATCHES: '3',
  // the old entry model is not used in paired-exit mode (it would only be loaded)
  TPB_TRIANGLE_ENTRY_MODEL: '0',
};

module.exports = {
  apps: [
    { name: 'tpb-repricedmom-paper',
      cwd: ROOT, interpreter: 'none', script: PYTHON, autorestart: true, min_uptime: '30s', max_restarts: 10,
      restart_delay: 5000, kill_timeout: 15000, merge_logs: true, time: true, max_memory_restart: '600M',
      env: { ...TRIANGLE, ...PAIRED_EXIT },
      // 2026-10-01 ~17:05 UTC (the user: "optimize the model as fast"): execution scenario venue_lag -> realistic.
      // venue_lag added an arbitrary 500 ms matching delay on top of the ~224 ms measured venue round trip, so paper
      // orders landed ~770 ms after the decision; realistic lands them at the measured round trip and kills an
      // order when its quote has left the book within 2 s (calibrated to the measured kill rate, 09-23 study).
      // 2026-10-01 ~20:00 UTC (the user: "why the fill rate? improve it", docs/triangle_fill_rate_2026-10-01.md):
      // (1) scenario realistic -> calibrated. realistic killed an order whenever the exact ask it matched left the
      //     book within 2 s, even when the new ask was still inside the limit (the venue fills those): 29 of 47
      //     kills. calibrated (1 s, judged against the limit, worse-price fill) reproduces 193 REAL orders' fills
      //     (97 % / 90 % agreement slow / fast feeds; realistic 96 % / 82 %).
      // (2) sources binance,bybit,coinbase -> deribit,kraken,bitstamp,coinbase,binance. The composite is the
      //     median: with 3 sources it moved on the 2nd quote (Bybit, ~106 ms); with 5 on the 3rd (Bitstamp ~49 ms).
      //     Real orders with this feed set (09-30 19:12-21:50): fill rate 47-75 % vs 10 % before, spot age 37-120
      //     vs 164 ms. Binance stays for its trade flow (the entry model's order-flow features).
      args: '-m troll_poly_bot --balance 100 --mode paper --scenario calibrated --strategy triangle '
          + '--exchanges deribit,kraken,bitstamp,coinbase,binance --assets BTC '
          + '--data-tag triangle-btc-paper --keep-history --log-level INFO' },
    // the original repriced-momentum app (tpb-repricedmom-paper-orig) was removed 2026-10-01: pm2's --only
    // matched it by name prefix and started it next to the triangle bot. It is in git history.
  ],
};
