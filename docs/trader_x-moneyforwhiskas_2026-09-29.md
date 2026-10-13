# Trader analysis: x-MoneyForWhiskas (2026-09-29)

**Answer.** A high-frequency bot that trades only Bitcoin 5-minute Up/Down markets, every window.

- **How it trades:** it buys whichever side BTC is about to move toward, a second or so before the market makers
  reprice, in small clips. It never sells; every position is held to settlement.
- **Profit:** about 1% of volume after fees, plus taker rebates worth almost as much again.
- **Direction does not matter to it:** it buys both sides in 97% of markets.

Public profile: `polymarket.com/@x-moneyforwhiskas`, wallet `0x3048d65321be3497164cdfc2996f94f98a2e7537`,
account created 2026-06-08, bio "Trading to pay for my cat's food."

## Headline numbers

| | value | source |
|---|---:|---|
| markets traded (all time) | 31,132 | data-api `/traded` |
| volume, all time / 30 d / 7 d | $34.4M / $16.4M / $3.07M | lb-api |
| profit, all time / 30 d / 7 d | +$141k / +$206k / +$48.9k | lb-api (so it lost ~$65k before the last 30 days) |
| portfolio value now | $1,297 | data-api `/value` (capital recycles every 5 minutes) |
| taker tier | 6, "Obsidian" | gamma public profile |

Sample analysed: every activity record from 09-26 11:36 to 09-29 12:43 UTC (45,853 records via data-api
`/activity`), matched to this repo's recorded order books (`data/charts/btc-updown-5m-*.json`).

## What it does

| | |
|---|---|
| market | **BTC 5-minute Up/Down only** (100% of 44,985 trades; a few ETH 5-minute trades 20 days ago; 100% BTC in June, July and August samples) |
| coverage | 871 markets in 72 h: essentially every window |
| orders | **buys only**; no sells, no merges; 862 redemptions after settlement |
| both sides | Up and Down both bought in **97%** of markets (22,029 Up, 22,956 Down buys) |
| size | median $7 (16 shares), mean $12, max $91; ~43 buys per market; median $404 committed per market |
| timing | first buy ~8 s after the open, last ~171 s in; buys spread across 0–240 s, only 7% in the last minute |
| prices | 0.10–0.90 across the range, most between 0.20 and 0.70 |
| taker / maker | **59% of fills, 79% of dollars taker** (the fee is inside the fill cost); 41% of fills resting bids |

## Why it makes money

**The taker buys anticipate the reprice.** Each buy was matched to the recorded book: fill price = the recorded
ask about 3 s before the on-chain timestamp, which is the block-time lag.

- **After a taker buy, the bought token's mid rises +1.3c in 1 s and +2.0c in 3 s, and stays there** (+2.0c at
  30 s, +2.1c at 60 s).
- This is true whatever the token did beforehand: +1.8 to +2.5c after a fall, a flat spell or a rise.
- At random times with the same prior move, the next 5 s average −0.2 to +0.1c. The token only rises because they
  were early.
- **BTC spot moves their way after the buy in our feed:** +0.13 bps after 1 s, +0.26 after 3 s. They see the
  move on a faster source than our composite, around a second earlier, within the ±1 s precision of chain
  timestamps.
- **The maker fills carry no such signal:** +0.3c after 5 s, −0.6c after 60 s. They are resting bids on both sides
  (median fill = the bid).

**Holding to settlement.** It pays one taker fee per share, never an exit fee, spread or cross. The two sides
partly hedge each other:

- the average Up price plus the average Down price per market is 0.986 (median);
- that sum is below $1 in 60% of markets;
- the unhedged leftover is a median 24% of the bigger side.

**Results in the sample** (699 settled markets with a recorded outcome; fees included):

| day | markets | market P&L | return on cost | win rate | rebates paid |
|---|---:|---:|---:|---:|---:|
| 09-26 | 121 | −$166 | −0.6% | 45% | – |
| 09-27 | 244 | +$1,628 | +1.8% | 50% | $1,051 |
| 09-28 | 218 | +$3,352 | +2.0% | 56% | $1,468 |
| 09-29 (to 12:43) | 116 | −$888 | −0.9% | 44% | $3,225 |
| **total** | 699 | **+$3,926** | **+1.0%** | 50% | $5,744 |

- **Per market:** median −$0.09, sd $89, worst −$442, best +$607. It is not arbitrage; it is a small edge repeated
  thousands of times.
- **Fees and rebates:** about $12.9k in taker fees over the three days, and $5.7k back in taker and maker rebates.
  **The rebates are more than half of the profit**, so the volume tier is part of the strategy.

## What it means for our spot-lag bot

It is the same idea, executed differently:

1. **Speed.** It acts on the first tick of a spot move on a fast source. Our bot waits for a 3σ 1 s shock in its
   composite feed; by then the token has repriced, which is the fill-rate problem (6–12% of orders filled, 2% when
   the book is over 120 ms old).
2. **Small clips, many attempts.** 16 shares at a time, ~43 per market, taking what the touch holds. Our 10-share
   FOK misses whenever the touch has fewer than 10 shares.
3. **No exit.** One fee and a rebate, against our two fees plus a 2c cross on the sell. It relies on the two sides
   hedging each other instead of a timed exit.
4. **Volume tier and rebates** turn a thin edge into a business; at our size that lever does not exist.

## Caveats

- **Three days of activity.** Outcomes were recorded for 699 of 871 markets.
- **Timestamp precision.** The ~3 s chain-timestamp offset was estimated from the fills themselves, so the
  lead-time figures are good to about ±1 s.
- **Leaderboard figures are Polymarket's own accounting.** Their 7-day +$48.9k is larger than the settled-market P&L
  measured here (part of the gap is markets missing from our archive and rebates).
