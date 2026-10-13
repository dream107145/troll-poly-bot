// PAIR-LOCK B with the POLYMARKET-BOOK entry trigger (2026-10-02) -- PAPER ONLY. The user: "figure out new book spot
// mode instead of crypto spot".
//
//   pm2 start ecosystem.pairlockbook.config.js --only tpb-pairlockbook-paper
//
// The SAME strategy B as tpb-pairlock-paper (its env and args are read from ecosystem.pairlock.config.js below, so
// the two stay identical) with ONE change: the entry trigger. TPB_PAIRLOCK_SIGNAL_SOURCE:
//   book    the Polymarket book model only (the triangle bot's 2026-10-02 model, data/replay/paired_exit/live/
//           book_btc.json: the predicted next-1 s UP mid move from both books' touches and near-touch depth on the
//           bot's own 200 ms samples); a reading >= TPB_PAIRLOCK_BOOK_THRESHOLD cents either way is a signal
//   either  the crypto shock when it fires, else the book
//   both    a crypto shock the book confirmed on the same side within the last second
// Then the same entry gate, hedges, locks and re-entry rule as B. Own data tag: data/*.pairlock-book-paper.*
//
// Evidence (docs/pairlock_recovery_2026-10-02.md, "Book trigger"; results/pairlock_book_replay_summary.json), B on the
// same 1,649 windows with only the trigger changed, chosen on TRAIN+VALIDATION, judged on TEST+FORWARD per market:
//   crypto (B)      +0.192   book 1.5c  +0.194 (test -24 / forward +74: unstable)   book 0.75c  +0.091
//   either 1.5c     +0.292   either 2c  +0.356   both 0c +0.180
// No t above 1.4 anywhere: none of these is a demonstrated edge. The bot exists to see the book trigger on live
// books, side by side with tpb-pairlock-paper. No real-money app: __main__ refuses micro-live / live / shadow.
//
// The app name must not share a prefix with another app: pm2 --only matches by prefix.

const base = require('./ecosystem.pairlock.config.js').baseB;   // strategy B as before 10-02 20:40 (not the conditions rule)
if (!base) throw new Error('tpb-pairlock-paper not found in ecosystem.pairlock.config.js');
if (base.env.TPB_PAIRLOCK_STRATEGY !== 'B') throw new Error('this A/B is defined against strategy B');

const BOOK = {
  TPB_PAIRLOCK_SIGNAL_SOURCE: 'book',       // book | either | both  (crypto = the base bot)
  TPB_PAIRLOCK_BOOK_THRESHOLD: '1.5',       // cents of predicted next-1 s UP mid move
  TPB_PAIRLOCK_BOOK_CONFIRM: '0',           // both: the book must have shown >= this on the crypto side
  TPB_PAIRLOCK_BOOK_CONFIRM_MS: '1000',
  TPB_PAIRLOCK_BOOK_MAX_AGE_MS: '500',      // a book signal older than this at the evaluation is not traded
  TPB_PAIRLOCK_BOOK_MODEL_PATH: 'data/replay/paired_exit/live/book_btc.json',
};

// own data files, and --keep-history: a restart appends to the trade log instead of emptying it
const args = base.args.replace('--data-tag pairlock-btc-paper', '--data-tag pairlock-book-paper --keep-history');
if (args === base.args) throw new Error('could not set the data tag');

module.exports = {
  apps: [
    { ...base, name: 'tpb-pairlockbook-paper', env: { ...base.env, ...BOOK }, args },
  ],
};
