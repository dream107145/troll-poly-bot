# Trader analysis: dangerously-skip-permissions (2026-10-09)

Wallet `0xc53375ff94e96100f2b30a4b5775db35218d69a9` (pseudonym Lone-Easel-Election), account created 2026-07-16,
taker tier 6 "Obsidian", 23,283 markets traded all time, portfolio value 1,039 USDC.

**Answer.** A high-frequency BTC 5-minute bot that buys the side whose token is already rising, in 5–100-share
clips, on both sides of almost every market, plus small resting bids. It never sells and holds everything to
settlement. Over the last 30 days it made **+102.5k USDC on 4.0M bought: +47.4k from the markets (+1.19 % of
volume, after 99.9k of taker fees) and +55.1k from rebates.** Every one of the 31 days was positive, but only
because of the rebates: the market P&L alone was negative on 6 days.

## Data

| | |
|---|---|
| activity | every record 2026-09-09 00:10 → 10-09 00:31 UTC: 254,741 (data-api `/activity`, 15-min windows split at the 500 cap; no window lost) |
| outcomes | gamma `markets?closed=true`, 8,306 of 8,306 traded markets resolved |
| positions | data-api `/positions` (1,723 open) and `/closed-positions` (5,000 cap = the last 10 days) |
| microstructure | 92,591 fills in 3,261 BTC markets matched to this repo's recorded order books (`data/charts`, 09-22 → 10-08); block timestamps aligned to the book at −2 s (best match) |
| taker / maker | from the fill itself: `usdcSize − price × size` is the taker fee (0.07·p·(1−p)); no fee = maker fill |

## What it trades

| | |
|---|---|
| markets | **BTC 5-minute Up/Down: 99.5 % of trades** (245,242); ETH 5-minute 0.5 % (1,290). Nothing else. |
| coverage | ~285 of the 288 BTC markets a day (8,306 markets in 30 days) |
| order flow | **buys only** (246,532); no sells, no merges; 8,148 redemptions; 61 rebate payments |
| volume | 3.99M USDC bought in 30 d; 50–265k a day; 4–15k fills a day |
| per market | median 23 fills (p90 65), median 349 USDC (p90 1,063, max 3,514) |
| both sides | Up and Down both bought in **95.1 %** of markets; consecutive buys switch side 39 % of the time |
| timing | all through the window: block time p10 22 s, median 125 s, p90 239 s after the open; ~10–14 % in each 30-s slice, only 1.5 % in the last 30 s |
| prices | 0.10–0.90, centred on 0.30–0.70 (16–17 % in each decile 0.4–0.6); almost nothing above 0.90 |
| taker | **65.6 % of fills, 84.5 % of dollars**; typical sizes 50, 5, 60, 100, 10 shares; 43 % of fills come in same-second bursts (one order walking the book) |
| maker | 34.4 % of fills, 15.5 % of dollars; tiny sizes (5, 2, 6, 1, 3 shares): resting bids on both tokens |
| unredeemed | 1,696 open positions worth ~0 (cost 36,038): losers it never redeems (nothing to collect) |

## Why it makes money

### 1. Taker buys ride Polymarket's own repricing (momentum, book-driven)

Token mid of the bought token around each taker fill (match = block time − 2 s; cents):

| | before the fill | | | after the fill | | | | |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| window | −10→0 s | −3→0 s | −1→0 s | 0→+1 s | 0→+3 s | 0→+10 s | 0→+30 s | 0→+60 s |
| **taker fills** | +0.76 | +1.10 | +1.26 | **+1.60** | **+1.87** | +1.80 | +1.94 | +1.89 |
| random moments, same markets | | +0.09 | +0.03 | +0.03 | +0.11 | | +0.96* | |

\*the 30-s baseline drifts up because a token surviving to a later sample tends to be the rising one.

- 55 % of taker fills come after the bought token rose ≥ 1c in the prior 3 s (28 % at random moments).
- The rise continues for 1–3 s after the fill and **does not revert** (still +1.9c at 60 s): it buys the start of a
  reprice and keeps the move.
- BTC spot barely leads it: +0.15 bps toward the bought side in the prior second, +0.19 in the next, +0.43 bps by
  60 s. The trigger is the Polymarket book itself (order flow / token velocity), not a faster spot feed.
- The fill vs the recorded ask: 48 % below, 20 % at, 32 % above. It often takes asks that our recording already
  shows gone (stale asks lifted before the book updates), and walks up 1–2 levels in bursts.

Result: taker fills +26.8k on 3.38M (+0.79 % after fees), positive in 8 of 9 price deciles.

### 2. Resting bids: small, adverse, still profitable

Maker fills are 2–6-share bids on both tokens. After them the token's median mid falls 0.5–1c over 30–60 s
(adverse selection), yet they made +20.6k on 0.62M (+3.34 %): they fill at the bid, pay no taker fee, and earn the
maker rebate (3.7k this month).

### 3. Rebates: more than half the profit

Taker rebates 51.4k + maker rebates 3.7k = **55.1k, 54 % of the 30-day total**. Six days had a negative market P&L
and still ended positive (09-15, 09-21, 09-22, 09-28, 10-02). The rebate is paid on its 3.4M of taker volume at the
top tier; a small account would not get it at this rate.

### 4. The hedge costs money; the edge is in the leftover lean

| | |
|---|---|
| average Up price + average Down price per market | median **1.004**, below 1 in 47.7 % |
| matched (hedged) shares | 84 % of the average side → **−13.3k** (the pair premium) |
| unhedged leftover | median 29 % of the bigger side → **+60.7k** |
| bigger side wins | 52.3 % of markets |

By size of the lean: under 10 % of the bigger side −7.0k (50.1 % win), 10–25 % +4.0k (52.9 %), 25–50 % **+34.3k
(53.7 %)**, over 50 % +16.1k (51.6 %). Buying both sides is a by-product of following whichever token moves, not a
deliberate pair lock.

## Daily record (market P&L + rebates)

All 31 days positive; total +102.5k; best days 10-06 / 10-07 (+7.0k / +7.5k), worst 09-28 (+276). Volume and
profit rose in the last week (10-05 → 10-08: 175–263k bought a day, +2.8k to +7.5k).

## What it needs to work

- **Latency of about a second or better**: the reprice it buys continues only 1–3 s (+1.6c in 1 s, +1.9c in 3 s).
- **Volume tier for the rebate**: 54 % of the profit; at a low tier the same trades earn ~+1.2 % of volume.
- **Capital ~1–4k**: one market at a time (median 349, max 3,514 USDC), recycled every 5 minutes through
  redemptions.
- No prediction model needed and no exit logic: the per-market result is close to a coin flip (52 % positive,
  median +2.31, p10 −95, p90 +111); the edge is ~1 % of every dollar, repeated 8,300 times.

## Comparison with x-MoneyForWhiskas (docs/trader_x-moneyforwhiskas_2026-09-29.md)

Same family: BTC 5m only, buys only, both sides, hold to settlement, ~+1 % of volume, rebates ≈ half the profit,
taker tier 6. Differences: larger clips (median 22 vs 16 shares, up to 100), ~2.3× the volume, and its trigger is
the token's own velocity rather than a spot move.

## Caveats

- Block timestamps are only accurate to ~1 s; "before / after" within ±1 s of the fill overlap.
- The microstructure covers 38 % of fills (BTC markets this repo recorded, 09-22 → 10-08).
- P&L is per market from fills and the resolved outcome; unredeemed winners are counted at payout.
