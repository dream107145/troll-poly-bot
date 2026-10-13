// TRIANGLE BOOK-SIGNAL A/B (2026-10-02) -- PAPER ONLY. The user: "Add new mode which detect token price direction
// from Polymarket order book. I think it's fast more."
//
//   pm2 start ecosystem.trianglebook.config.js --only tpb-trianglebook-paper
//
// The SAME paired-exit triangle as tpb-repricedmom-paper (its env and args are read from
// ecosystem.repricedmom.config.js below, so the two stay identical) with ONE change: the entry signal comes from
// the Polymarket order book instead of crypto spot (TPB_TRIANGLE_PX_SIGNAL_SOURCE=book). The book model
// (data/replay/paired_exit/live/book_btc.json, scripts/book_signal/fit_live_book.py) predicts the UP token's mid
// move over the next second from both books' touches and near-touch depth on the bot's own 200 ms samples; a
// reading >= 1.5c (either way) is a signal, then the same fair-value EV gate, exits, caps and kill conditions.
// Cross-exchange agreement is off (it is a crypto filter). Own data tag: data/*.triangle-book-paper.*
//
// Evidence (results/book_signal_part1-3.md, docs/book_signal_2026-10-02.md): the book reads the next second better
// than spot (corr +0.28 vs +0.12, 81-85 % sign hit on its strongest 10 %) and is first on 60-74 % of 2c token moves,
// but as this strategy's trigger it earned LESS per trade than crypto in the daily walk-forward (+0.089 vs +0.191 per
// fill, +0.050 vs +0.070 per market). This bot exists to check that on live books, side by side with the crypto bot.
// No real-money app: __main__ refuses micro-live / live / shadow for the triangle strategy.
//
// The app name must not share a prefix with another app: pm2 --only matches by prefix.

const base = require('./ecosystem.repricedmom.config.js').apps.find((a) => a.name === 'tpb-repricedmom-paper');
if (!base) throw new Error('tpb-repricedmom-paper not found in ecosystem.repricedmom.config.js');

const BOOK = {
  TPB_TRIANGLE_PX_SIGNAL_SOURCE: 'book',    // crypto | book | both | either
  TPB_TRIANGLE_PX_BOOK_THRESHOLD: '1.5',    // |predicted next-1 s UP mid move| in cents
  TPB_TRIANGLE_PX_MIN_AGREE: '-1',          // the crypto-exchange agreement filter: off for a book signal
};

const args = base.args.replace('--data-tag triangle-btc-paper', '--data-tag triangle-book-paper');
if (args === base.args) throw new Error('could not set the data tag');

module.exports = {
  apps: [
    { ...base, name: 'tpb-trianglebook-paper', env: { ...base.env, ...BOOK }, args },
  ],
};
