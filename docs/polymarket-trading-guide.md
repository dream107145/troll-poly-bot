# Polymarket Trading: Principles and Strategies

A primer for anyone working on this bot, or trading prediction markets in general.
It covers three things:

1. [General knowledge of trading and well-known trading platforms](#1-general-knowledge-of-trading-and-well-known-trading-platforms)
2. [Principle of Polymarket trading](#2-principle-of-polymarket-trading)
3. [World-famous trading strategies](#3-world-famous-trading-strategies), and how each one applies to Polymarket

For the measurements behind this bot's own strategy, read
[strategy.md](strategy.md). This guide explains the background; that document
has the numbers.

> **Not financial advice.** Most traders lose money, and prediction markets are
> no exception. Check whether Polymarket is legal where you live before you use
> it. This repository trades on paper only.

---

## 1. General knowledge of trading and well-known trading platforms

### 1.1 What trading is

Trading means buying and selling an asset to profit from changes in its price.
Investing means holding an asset for years. A trader's profit on any one trade is:

```
PnL = (exit price − entry price) × size − fees − slippage
```

Across many trades, a strategy is worth running only if its **expected value
(EV)** is positive after every cost:

```
EV per trade = P(win) × avg win − P(loss) × avg loss − costs
```

A 60% win rate can lose money and a 30% win rate can make money. The payoff
ratio matters as much as the hit rate.

### 1.2 Core vocabulary

| Term | Meaning |
|---|---|
| **Bid / Ask** | The highest price a buyer will pay and the lowest price a seller will accept. |
| **Spread** | Ask − bid. You pay it whenever you cross it. |
| **Mid** | (bid + ask) / 2. Often used as the market's "fair" estimate. |
| **Order book** | All resting limit orders at each price level. Its depth shows how much you can trade before the price moves. |
| **Market order** | Execute now at the best available price. Pays the spread. |
| **Limit order** | Execute only at your price or better. May never fill. |
| **FOK / FAK / GTC / GTD** | Fill-or-kill, fill-and-kill (IOC), good-till-cancelled, good-till-date. |
| **Maker / Taker** | A maker adds a resting order to the book. A taker removes liquidity by crossing the spread. Venues often charge takers more. |
| **Slippage** | The gap between the price you expected and the price you got. |
| **Liquidity** | How much you can trade without moving the price. |
| **Volatility** | How much prices move. Usually measured as the standard deviation of returns. |
| **Long / Short** | Profit if the price rises / falls. |
| **Leverage** | Borrowed exposure. It magnifies wins and losses and can lead to liquidation. |
| **Drawdown** | The fall from an equity peak to the next trough. |
| **Edge** | Your estimate of the true value minus the price you pay, after costs. |

### 1.3 Market participants

- **Market makers** quote both sides and earn the spread. Their risk is
  *adverse selection*: traders who know more than they do pick off their stale quotes.
- **Informed traders** have better information or better models.
- **Noise / retail traders** trade for reasons unrelated to value. Their flow
  is where most other participants' profit comes from.
- **Arbitrageurs** trade away price differences between venues or between
  related instruments.
- **Hedgers** trade to reduce a risk they already carry, and will accept a
  negative expected value to do it.

### 1.4 Well-known trading platforms

| Category | Platforms | Notes |
|---|---|---|
| **Stock exchanges** | NYSE, Nasdaq, LSE, Tokyo SE, Hong Kong SE | Regulated, central limit order books, market hours. |
| **Retail brokers** | Interactive Brokers, Charles Schwab, Fidelity, Robinhood, eToro | Route your orders to exchanges or market makers. |
| **Futures & options** | CME Group, Cboe, Eurex | Leverage, hedging, index and commodity derivatives. |
| **Forex** | Interbank market, via OANDA, IG, and MetaTrader 4/5 brokers | Traded 24/5 and decentralised. |
| **Centralised crypto exchanges (CEX)** | Binance, Coinbase, Bybit, OKX, Kraken | Spot and perpetual futures. This bot reads its spot price from Binance, Bybit and Coinbase. |
| **Decentralised exchanges (DEX)** | Uniswap, Hyperliquid, dYdX | On-chain AMMs or order books, self-custody. |
| **Prediction markets** | **Polymarket**, Kalshi, PredictIt, Manifold (play money), Betfair Exchange (sports) | Contracts that pay out based on the outcome of an event. |
| **Charting / analysis** | TradingView, Bloomberg Terminal | Data, charts and screening. They do not execute trades. |

**Prediction markets compared:**

| | Polymarket | Kalshi | PredictIt |
|---|---|---|---|
| Settlement asset | USDC (crypto stablecoin) | USD | USD |
| Regulation | Crypto-native international venue, plus a separate CFTC-regulated US entity | CFTC-regulated exchange | Operates under CFTC permission, with position limits |
| Market structure | Hybrid CLOB: matching off-chain, settlement on-chain | Central order book | Order book |
| Resolution | UMA optimistic oracle; Chainlink for crypto price markets | Exchange rulebook | Exchange rules |
| Typical strength | Deepest liquidity, widest market range | US-legal, bank rails | Politics niche |

---

## 2. Principle of Polymarket trading

### 2.1 What you are buying

Every Polymarket market is one or more **binary outcome tokens**. A market such
as *"Will BTC be up at 12:05?"* has two tokens, **YES (Up)** and **NO (Down)**.

- At resolution the winning token redeems for **$1.00** and the losing token
  for **$0.00**.
- Before resolution, each token trades anywhere between $0.00 and $1.00.
- **The price is the market's implied probability.** YES at $0.63 means the
  market thinks the event has about a 63% chance of happening.

```
Buy YES at 0.63 → if YES:  +0.37 per share   (+58.7% return)
                  if NO:   −0.63 per share   (−100%)
Breakeven win rate = price paid = 63%  (before fees)
```

That last line is the most important rule on Polymarket: **you profit only if
the true probability exceeds the price you paid, by more than your costs.**

### 2.2 How the tokens exist: complete sets

Tokens are issued with Gnosis's **Conditional Token Framework (CTF)** on the
Polygon blockchain.

- **Split:** lock $1 USDC → receive 1 YES + 1 NO.
- **Merge:** return 1 YES + 1 NO → receive $1 USDC.
- **Redeem:** after resolution, the winning token → $1 USDC.

Because of split and merge, **YES + NO should always cost about $1**. If
`ask(YES) + ask(NO) < 1`, you can buy both and lock in a profit. If
`bid(YES) + bid(NO) > 1`, you can split and sell both. This is the arbitrage
that keeps the two books consistent.

A useful consequence: **selling YES at p is economically the same as buying NO
at 1 − p.** The order book shows one unified market per outcome pair.

### 2.3 Market structure: the hybrid CLOB

- Orders are **EIP-712 signed messages**. An off-chain operator matches them.
  Matched trades **settle on-chain** through the CTF Exchange contract, so the
  operator can never move your funds without your signature.
- Order types: **GTC, GTD, FOK, FAK**. Prices move in ticks (usually $0.01;
  $0.001 near the extremes on some markets).
- APIs:
  - **Gamma API** – market discovery and metadata (slugs, questions, end
    dates, fee schedule, outcome prices).
  - **CLOB API** – order books, prices, placing and cancelling orders.
  - **WebSocket** – real-time book, price and trade updates.
- This bot uses Gamma to discover markets (`AssetRegistry`, `MarketMeta`) and
  the CLOB book for pricing and execution. See `src/troll_poly_bot/market/`.

### 2.4 Resolution

- **General markets** resolve through the **UMA Optimistic Oracle**. Someone
  proposes an outcome and posts a bond. If nobody disputes it within the
  challenge window, the outcome is final. Disputes go to a vote of UMA token
  holders.
- **Crypto up/down markets** (what this bot trades) resolve from **Chainlink**
  price data. The bot models the settlement as a TWAP around the window open
  and close (see `strategy.md`).
- **Always read the resolution rules.** Many losses come from traders who were
  right about the world but wrong about the rules: which source, which
  timestamp, what counts as "announced".

### 2.5 Costs

| Cost | Detail |
|---|---|
| **Trading fee** | Many markets charge no fee. Some, including the short crypto up/down markets, charge a **taker-only** fee. The schedule this bot measured is `fee = shares × 0.07 × p × (1 − p)`, which is highest at p = 0.50 (1.75¢ per share). Always read the market's `feeSchedule` from Gamma rather than assuming a number. |
| **Spread** | Crossing a 2¢ spread on a 50¢ token costs 4% right away. |
| **Slippage** | Thin books move against you as you fill. |
| **Gas** | Small, because Polygon is cheap. Order signing is gasless. Deposits, withdrawals and redemptions cost a little. |
| **Capital lock-up** | Money in a long-dated market earns nothing until it resolves. A 95¢ YES that resolves in a year returns about 5%, which may be worse than holding cash. |

### 2.6 The core pricing principle

```
edge = P_true − price − fee(price) − slippage − uncertainty_margin
Trade only if edge > 0, and size the trade in proportion to edge and confidence.
```

Everything else follows from it:

1. **Your probability must beat the market's.** The market price already
   combines what every other trader knows. You need information, a better
   model, or faster reaction.
2. **Calibration beats conviction.** If your "80%" trades win 65% of the time,
   you will lose money. Track a Brier score and a calibration curve.
3. **Selection bias (winner's curse).** The trades where your model disagrees
   most with the market are the ones where your model is most likely wrong.
   Shrink your estimate toward the market. This bot blends 50/50 with the mid.
4. **Correlated bets are one bet.** BTC, ETH and SOL "up" in the same
   5-minute window are nearly the same wager. This bot caps risk per epoch
   for that reason.
5. **Measure edge statistically, not anecdotally.** You need hundreds of
   independent outcomes before you can tell a 3–5¢ edge from luck.

### 2.7 Market types on Polymarket

- **Binary events** – politics, sports, macro ("Will the Fed cut in March?").
- **Multi-outcome (negative-risk) markets** – "Who will win the election?".
  Each candidate is its own YES/NO pair, and at most one YES wins, so all the
  YES prices should sum to about $1.
- **Price-threshold markets** – "BTC above $X on date Y".
- **Short-duration crypto up/down** – 5- and 15-minute windows. They are fast
  and heavy on fees and latency, and they are this bot's target.

---

## 3. World-famous trading strategies

Each strategy below has a short description and a **Polymarket angle**: how
it translates to binary outcome tokens.

### 3.1 Trend following / momentum

*Famous practitioners: Richard Dennis's "Turtle Traders", Ed Seykota, Man AHL,
Winton.*

Buy what is rising and sell what is falling. Cut losers quickly and let
winners run. Typical signals are moving-average crossovers and breakouts
(Donchian channels). It wins big in rare trends and loses small in choppy
markets.

**Polymarket angle:** probabilities tend to drift toward 0 or 1 as news comes
out. Momentum can work in slow-moving event markets, such as a poll trend or
a candidate consolidating support. It works badly on short crypto windows,
where the token absorbs a spot move within about a second.

### 3.2 Mean reversion

*Famous users: statistical-arbitrage desks, Bollinger Band traders.*

Prices that move far from their average tend to come back. Fade extremes
using RSI, z-scores or Bollinger Bands.

**Polymarket angle:** fade overreactions to headlines, such as a 20-point
jump on an unconfirmed rumour. Be careful, because in a binary market some
"extremes" are simply the correct new price.

### 3.3 Arbitrage

*Famous users: every high-frequency trading firm; Ed Thorp in convertible
bonds.*

Buy and sell the same or equivalent exposure at different prices to lock in a
risk-free profit.

**Polymarket angle:**
- **Complete-set arbitrage:** `ask(YES) + ask(NO) < $1` → buy both.
- **Negative-risk arbitrage:** in a multi-outcome market, the sum of all YES
  asks is below $1, or the sum of all NO prices is mispriced.
- **Cross-venue arbitrage:** the same event on Polymarket and on Kalshi or
  Betfair at different implied probabilities. Watch for resolution rules that
  differ in small ways, because then the "arbitrage" carries real risk.
- **Logical arbitrage:** "X wins the nomination" should not be priced below
  "X wins the presidency".

### 3.4 Market making

*Famous firms: Citadel Securities, Jane Street, Virtu, Wintermute.*

Quote a bid and an ask at the same time and earn the spread. Manage inventory
by skewing your quotes. The Avellaneda–Stoikov model is the textbook version.

**Polymarket angle:** Polymarket pays **liquidity rewards** to makers who
quote close to the mid, and taker fees fall on the other side. The risk is
adverse selection: informed traders or fast bots pick off your quotes right
before news breaks or a crypto price moves. Pull or widen your quotes ahead of
known events.

### 3.5 Fundamental / information edge

*Famous practitioners: Warren Buffett and Benjamin Graham (value investing);
in forecasting, Philip Tetlock's "superforecasters".*

Estimate true value from first principles, then buy what is cheap relative to
it.

**Polymarket angle:** this is the natural Polymarket strategy. Build your own
probability from base rates, polls, models and domain expertise, then compare
it with the price. Superforecasting habits help here: start from the base rate
(outside view), then adjust (inside view), update in small increments, and
keep a record of your calibration.

### 3.6 Event-driven / news trading

*Famous users: merger-arbitrage funds, macro desks trading data releases.*

Trade the gap between when something happens and when the price reflects it.

**Polymarket angle:** you need fast news sources such as official feeds, live
broadcasts and on-chain data. Speed matters more than depth. This edge is now
competitive, because many bots already watch the same feeds.

### 3.7 Statistical / quantitative modelling

*Famous firms: Renaissance Technologies (Medallion), Two Sigma, D. E. Shaw.*

Many small, weakly predictive signals are combined, rigorously out-of-sample
tested, and traded at scale with strict risk control.

**Polymarket angle:** this is what this bot does. It prices each crypto
up/down window as a **digital option** on a TWAP (Student-t, two-scale
volatility), blends that with the market mid, subtracts the real fee curve,
and trades only when the remaining edge clears its gates. The main lesson
from [strategy.md](strategy.md): walk-forward testing, epoch-clustered
statistics and honest settlement matter more than the model.

### 3.8 Latency arbitrage

*Famous from: Michael Lewis's* Flash Boys *and HFT colocation.*

Act on a price change in a leading market before a lagging market updates.

**Polymarket angle:** Binance, Bybit and Coinbase spot lead the Polymarket
crypto tokens. The window is now well under a second and the taker fee eats
most of it, so this bot's measurements show latency mostly turns into missed
fills rather than profit.

### 3.9 Time decay / "high-probability" harvesting

*Relative of options theta selling.*

Buy near-certain outcomes (for example YES at 97¢) shortly before resolution
to earn the last few cents.

**Polymarket angle:** this is popular and it looks safe, but it is
**negative-skew**. You win 3¢ many times and lose 97¢ once. It is only
positive-EV if the true probability exceeds 97% plus fees and capital cost.
Resolution-rule surprises and oracle disputes are where it blows up.

### 3.10 Contrarian / sentiment fading

*Famous line: "Be fearful when others are greedy" (Buffett); Sir John
Templeton.*

Trade against crowd emotion.

**Polymarket angle:** there is well-documented **favourite–longshot bias** in
betting markets. Longshots (cheap YES tokens) tend to be overpriced, and
heavy favourites slightly underpriced. Popular, emotional topics can also
drift away from fair value when one-sided retail money floods in.

### 3.11 Hedging

Cut a risk you already hold. Examples: buy "Candidate X loses" if your
business is hurt by X winning, or hedge a crypto position with a "BTC below
$Y" market.

### 3.12 Money management: what keeps every strategy alive

No strategy survives bad sizing.

- **Kelly criterion** (John Kelly, used by Ed Thorp). For a binary contract
  bought at price `p` that you believe has true probability `q`:

  ```
  f* = (q − p) / (1 − p)        fraction of bankroll to stake (buying YES)
  ```

  Full Kelly is very volatile and assumes `q` is known exactly. Use
  **fractional Kelly (¼ – ½)**. This bot uses fractional Kelly × confidence.
- **Position caps:** per market, per correlated group, and in total.
- **Stop rules:** daily loss limit and maximum drawdown.
- **Keep a journal:** entry reason, model probability, price, fees, and the
  outcome **as resolved by the venue** (never graded by your own signal).
- **Paper trade first** until the evidence statistic is convincing. This
  bot requires an epoch-clustered t-statistic above 2 before live money is
  even discussed.

---

## Quick checklist before any Polymarket trade

1. What is my probability, and where does it come from?
2. What is the price I will actually pay (ask, not mid), plus fee and slippage?
3. Is `edge = my probability − all-in price` clearly positive, beyond my
   model's uncertainty?
4. Have I read the exact resolution rules and source?
5. Is this trade correlated with positions I already hold?
6. Is the size fractional Kelly or smaller, and within my caps?
7. Will I record it and grade it against the venue's outcome?

## Further reading

- Polymarket documentation: docs.polymarket.com (CLOB, Gamma, CTF, resolution)
- Philip Tetlock & Dan Gardner, *Superforecasting*
- Edward O. Thorp, *A Man for All Markets*; *Beat the Dealer*
- Larry Harris, *Trading and Exchanges* (market microstructure)
- Ernest Chan, *Quantitative Trading*
- Curtis Faith, *Way of the Turtle*
- This repo: [strategy.md](strategy.md), [replay.md](replay.md)
