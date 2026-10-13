# Polymarket order-book direction signal for the triangle bot (2026-10-02)

The user: "so it detect crypto price fall from crypto source right? Add new mode which detect token price direction
from Polymarket order book. I think it's fast more."

## What the triangle bot does today

The triangle bot's paired-exit strategy reads crypto spot only. It takes the median of Deribit, Kraken, Bitstamp,
Coinbase and Binance every 200 ms. A signal is a 1-second move of at least 2 sigma, using the previous window's
volatility, with at least a third of the exchanges agreeing. The Polymarket book is used for the price check
(fair value minus ask minus fee minus costs ≥ 0.02) and for the fill, but not to decide direction.

## The new mode

`TPB_TRIANGLE_PX_SIGNAL_SOURCE` takes one of four values:

- `crypto` (the default; behaviour is unchanged);
- `book`;
- `both` (a crypto signal that the book confirmed within the last second);
- `either` (crypto when it fires, otherwise the book).

The book signal is a ridge model of the UP-equivalent token mid's move over the next second, in cents. Its inputs
come from both tokens' best bid and ask and the depth within 2 ticks of each touch, on the same 200 ms samples the
bot already records. Each input is measured over 200 ms, 400 ms and 1 s:

- **Mid velocity**: how fast the UP-equivalent mid is moving.
- **Ask consumption**: asks taken or pulled at the touch.
- **Bid consumption**: bids taken or pulled at the touch.
- **New liquidity**: new near-touch depth on each side.

Three more are measured at the current sample only:

- **Depth imbalance** near the touch.
- **Microprice minus mid**: where the depth-weighted price sits against the mid.
- **Spread change**.

A reading of ≥ `TPB_TRIANGLE_PX_BOOK_THRESHOLD` cents, either way, is a signal. Everything after the signal is
unchanged: the fair-value EV gate, the exits, the caps and the kill conditions.

Code and data:

- **Pure logic**: `strategy/paired_exit.py` (`book_features_at`, `BookModel`, `pick_signal`).
- **Engine**: `strategy/paired_exit_engine.py` (`observe_point`).
- **Configuration**: `TriangleConfig.px_signal_source`, `px_book_threshold`, `px_book_confirm` and
  `px_book_confirm_ms`.
- **Live model**: `data/replay/paired_exit/live/book_btc.json`, fitted on all 2,046 BTC windows by
  `scripts/book_signal/fit_live_book.py`.
- **Tests**: `tests/test_book_signal.py`. They check that the live features equal the study's at every sample, that
  the live trigger fires on the replay's candidates in all 4 modes, and the engine's behaviour in each mode.

## Evidence (scripts/book_signal/, results/book_signal_part1-3.md)

Data: 2,046 BTC 5-minute windows of the dashboard archive at 200 ms. The splits are those of the paired-exit study:
TRAIN 995, VALIDATION 398, TEST 299 and FORWARD 299. On top of them, FORWARD2 holds 55 windows archived after that
study's cache (10-01 21:25 UTC onward), which no study had seen. The book model and the fair model are fitted on
TRAIN only. Fills follow the calibrated paper scenario: the order lands 250 ms later, must survive 1 s, and is filled
at the worse of the two books, with fee 0.07·p·(1−p).

### 1. As a predictor, the book is better and earlier than spot

| split | corr(book, next 1 s) | corr(spot z1, next 1 s) | book sign hit, strongest 10 % | spot sign hit, strongest 10 % |
|---|---:|---:|---:|---:|
| train | +0.282 | +0.101 | 85 % | 65 % |
| validation | +0.284 | +0.124 | 81 % | 63 % |
| test | +0.314 | +0.129 | 84 % | 63 % |
| forward | +0.288 | +0.133 | 84 % | 67 % |
| forward2 | +0.278 | +0.103 | 85 % | 63 % |

On 2¢-in-1-second token moves where both fired, the book (at ≥ 1¢) fired first on 60–64 % of moves. The median lead
was +0.2 to +0.4 s, and it was +0.4 to +0.8 s at lower thresholds. The biggest weight is the microprice, then
new liquidity and velocity.

### 2. As a trade, every raw trigger loses to the spread, fee and latency

Buying on every trigger at the paper fill, the token mid 1 s later is 2.4–2.6¢ under the price paid for book
triggers, against 2.8–3.1¢ for crypto triggers. Book triggers fire 270–1,800 times per hour of markets, against
60–200 for crypto. The edge has to come from the fair-value gate, as it does for crypto.

### 3. In the strategy, today's settings with only the trigger changed

| trigger | TEST+FORWARD $/market (t) | $/fill | FORWARD2 $/market |
|---|---:|---:|---:|
| crypto z ≥ 2 (today) | +0.073 (1.79) | +0.240 | +0.232 |
| book ≥ 1.0¢ | +0.037 (0.64) | +0.075 | +0.160 |
| book ≥ 1.5¢ | +0.048 (0.94) | +0.117 | +0.169 |
| book ≥ 2.0¢ | +0.037 (1.40) | +0.306 | −0.024 |
| both (book ≥ 0.5¢ confirms) | +0.068 (1.66) | +0.224 | +0.232 |
| either (book ≥ 1.5¢) | +0.085 (1.92) | +0.249 | +0.183 |
| either (book ≥ 2.0¢) | +0.081 (2.06) | +0.279 | +0.201 |

Picking each mode's own parameters on TRAIN and VALIDATION gave these untouched out-of-sample results (TEST, FORWARD
and FORWARD2):

| mode | $/market | t | fills |
|---|---:|---:|---:|
| book (≥ 0.75¢) | +0.066 | 1.18 | 338 |
| both | +0.038 | 1.02 | 169 |
| crypto | −0.006 | −0.21 | 72 |
| either | −0.004 | −0.12 | 94 |

That re-selection over-fits for crypto: today's own settings did +0.086/market on the same data.

### 4. Daily walk-forward (both models re-fitted every day on all earlier days, fixed rules)

| trigger | $/market | t | $/fill | days positive |
|---|---:|---:|---:|---:|
| crypto (today) | +0.070 | 2.38 | +0.191 | 7/8 |
| book ≥ 1.0¢ | +0.053 | 1.23 | +0.079 | 6/8 |
| book ≥ 1.5¢ | +0.050 | 1.29 | +0.089 | 6/8 |
| book ≥ 2.0¢ | +0.019 | 0.76 | +0.078 | 7/8 |
| both (book ≥ 0¢ confirms) | +0.070 | 2.36 | +0.190 | 7/8 |
| either (book ≥ 1.5¢) | +0.084 | 2.65 | +0.202 | 7/8 |
| either (book ≥ 2.0¢) | +0.069 | 2.30 | +0.182 | 7/8 |

## Verdict

- **Faster:** yes. The book usually shows the token's move before spot does, and it is the better 1-second
  predictor.
- **More profitable alone:** no. As the only trigger it earns less than half of crypto's profit per fill
  (+0.08–0.09 against +0.19 in the walk-forward), because it also fires on moves the market makers are already
  pricing. Those trades still pass the fair-value gate, but by less.
- **Combined:** "either" (crypto, plus the book when crypto is silent) is the best rule in the walk-forward,
  +0.084 against +0.070 per market. The gain is small and not significant.
- **Book as a filter:** "both" changes almost nothing, because the book nearly always agrees with a real crypto
  signal.

None of these numbers is proof of profit. The walk-forward t of today's own rule is 2.4 over 8 days.

## Deployment (paper only)

`tpb-trianglebook-paper` (`ecosystem.trianglebook.config.js`, data tag `triangle-book-paper`) runs the same bot as
`tpb-repricedmom-paper`. Its env and args are read from that file, with `PX_SIGNAL_SOURCE=book`,
`PX_BOOK_THRESHOLD=1.5` and agreement off. That makes it a side-by-side test on the same markets. The running crypto
bot was not touched: the new switch defaults to `crypto`. Switching the new bot to `either` is one env line plus a
restart.

Speed note: both bots evaluate on the 200 ms samples, as the study did. A book signal evaluated on every book
websocket update could act up to ~200 ms sooner, but the archive cannot validate that. It is not built.
