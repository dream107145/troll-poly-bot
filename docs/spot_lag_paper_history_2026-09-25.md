# Spot-lag paper bots: history and audit (09-24 14:30 → 09-25 12:50 UTC)

**Answer.** Over 22 hours tpb-spotlag-paper made +$14.81 on 42 fills (+7.1c/share, t 1.3). Most of that came from
one moment: four positions at 23:12:40 made $10.27.

- **The latest 12 hours are flat, in paper and in replay.** Its current settings made +$0.25 on 17 fills. The replay
  of the same settings over the same hours made +$0.44.
- **The edge fell away in data no study had seen.** From 09-25 00:50 to 12:50 UTC, every spot-lag setting replays at
  about 0c/share. The MAX_ASK 0.74 pick falls from +11.0c/share on the earlier unseen data to +0.2c.
- **The ma74 paper bot's +$12.63 is mostly a simulation artifact.** 13 of its 20 fills could not have filled
  according to the recorded order book, and those 13 carry $8.78 of its profit. The replay of its settings over the
  same hours lost $1.31.

## 1. History by setting period

| period (UTC) | settings | hours | orders | fills | fill rate | win | net | c/share |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 09-24 14:30–16:40 | live rule, 200 ms tick | 2.2 | 13 | 1 | 8% | 0% | −$0.52 | −10.4 |
| 16:40–21:04 | live rule, event-driven | 4.4 | 87 | 5 | 6% | 100% | +$4.32 | +17.3 |
| 21:04–00:46 | loose (1c edge, +4c limit, 5c spread), MAX_ASK 0.95 | 3.7 | 177 | 19 | 11% | 42% | +$10.76 | +11.3 |
| 09-25 00:46–12:50 | loose, MAX_ASK 0.60 | 12.1 | 317 | 17 | 5% | 53% | +$0.25 | +0.3 |
| **all** | | 22.3 | 594 | 42 | 7% | 52% | **+$14.81** | **+7.1** (t 1.3) |

- **One moment made most of it.** At 23:12:40, ETH, SOL, BNB and XRP DOWN filled together and made $10.27. The other 38
  fills made +$4.54 (+2.4c/share).
- **By entry price:** 0.20–0.40 made +14.7c/share (12 fills, mostly the 23:12 event); 0.40–0.60 made +4.4c (19);
  above 0.60 made +0.4 to +3.4c (8).
- **Exits:** every exit was the 5 s timer (median hold 6.1 s). Fills landed at the decision ask (median slippage 0c).

## 2. The two paper bots over the same hours (02:28–12:50)

| | tpb-spotlag-paper (loose, 0.60, event-driven) | tpb-spotlag-paper-ma74 (live rule, 0.74, tick) |
|---|---:|---:|
| orders / fills | 272 / 14 (5%) | 127 / 20 (16%) |
| paper net, c/share | +$1.11, +1.6 | +$12.63, +12.6 |
| **replay of the same settings, same hours** | +$0.44, +0.25 (00:46–12:50) | **−$1.31, −0.9** |

**Paired shocks.** On 70 shocks both bots traded the same market and side within 2 s of each other.

- The event-driven bot decided about 106 ms earlier, saw the same ask and set a slightly higher limit.
- It filled 3 of the 70; ma74 filled 12.
- On a real venue that is backwards: an earlier order with a higher limit fills at least as often.

**What the recorded order book shows.** Seven of those shocks were in BTC. In five of them the recorded ask jumped above
both bots' limits within 250–320 ms of the decision:

- 06:36 0.52 → 0.67
- 07:13 0.38 → 0.72
- 08:11 0.26 → 0.34
- 09:15 0.32 → 0.49
- 09:40 0.50 → 0.61

The paper model matches an order about 0.7 s after the decision (arrival plus 500 ms), so these could not fill. ma74
filled all five at the pre-shock price. In the two shocks where the ask stayed flat (08:26 and 11:30), both bots
filled.

**Every order checked against the recorded order book:**

| | tpb-spotlag-paper | ma74 |
|---|---:|---:|
| paper fills the book agrees with | 27 of 37 (+$12.41) | 7 of 20 (+$2.71) |
| paper fills the book says could not fill | 10 (+$0.51) | **13 (+$8.78)** |
| paper rejections the book says would have filled | 34 of 458 | 5 of 100 |
| fill rate according to the book | 12% | 10% |

- **Why ma74's fills are wrong:** its own book was stale. For every one of its BTC fills, its local mid had not moved
  at +100, +250, +500 or +1000 ms, and jumped at +2 s. The recorded book moved at about +280 ms.
- **What happened in the process:** its Polymarket book copy freezes for 1–2 s after a shock. The paper exchange
  matches against that frozen copy (`live._true_book`) and never checks how old it is.
- **What is still unexplained:** why the tick-driven process stalls more than the event-driven one.
- **The rates that agree:** the recorded book gives a 10–12% fill rate for both bots. That matches the real-money
  bot's 12% (6 of 49 orders, 09-24 15:23 → 09-25 02:44).

## 3. The edge in data no study had seen (09-25 00:50–12:50, 11.9 hours)

Replay results in c/share (fills in brackets), using the production budget.

| settings | train | validation | unseen 1–3 | **fresh 12 h** | fresh $ | fresh 95% CI | fresh win |
|---|---:|---:|---:|---:|---:|---|---:|
| live rule, 0.95 | +10.3 (117) | +5.0 (56) | +6.3 (77) | −0.3 (50) | −$0.69 | [−3.7, +3.7] | 40% |
| live rule, 0.74 (ma74) | +12.3 (103) | +6.4 (44) | +11.0 (58) | +0.2 (35) | +$0.32 | [−3.5, +4.4] | 37% |
| live rule, 0.60 | +14.9 (84) | +9.3 (31) | +14.7 (42) | +2.4 (25) | +$3.03 | [−2.3, +8.0] | 40% |
| loose, 0.60 (tpb-spotlag-paper) | +11.4 (105) | +6.6 (46) | +10.9 (62) | +0.2 (36) | +$0.44 | [−3.5, +4.5] | 33% |
| 4c edge, 0.74 | +15.9 (76) | +8.4 (33) | +13.9 (43) | +3.6 (23) | +$4.17 | [−1.2, +9.3] | 52% |
| 4σ, 0.74 | +14.9 (75) | +4.9 (37) | +12.7 (52) | −1.8 (22) | −$1.93 | [−5.2, +2.8] | 27% |

- **The drop is real, not noise.** For MAX_ASK 0.74, the fresh 95% CI [−3.5, +4.4] does not overlap the earlier unseen
  CI [+5.0, +18.6]. Win rates fell from 70–80% to 27–52%.
- **Time of day explains part of it.** With every fill counted (no budget), 00–06 UTC is the weakest block in each
  period: +7.9, +2.9 and +2.3c/share. But 06–12 UTC made +11.0 and +10.8c before and −0.3c in the fresh period. The
  fresh data has only 0.8 hours of 12–18 UTC and none of 18–24, the strongest hours before.
- **The ranking holds, but the level does not.** The stricter settings (0.60, 4c edge) stay on top, but no setting is
  clearly positive in the fresh 12 hours.

## 4. What this means

1. **Treat the paper P&L of a tick-driven process as unreliable** until the paper exchange refuses to match against a
   stale book. It could check the book's age at match time against the venue timestamp. That is a code change in
   `execution/paper.py` / `live._true_book`, not made.
2. **Judge settings by the replay and by the order-book audit.** The replay says the strategy has made nothing since
   00:50 UTC. The US afternoon and evening, the strongest hours before, have not yet occurred in the fresh data.
3. **Do not treat the event-driven bot's low fill rate as evidence against event-driven mode.** Its fill simulation is
   the one that agrees with the recorded book.
4. **Nothing here supports putting real money back on.** tpb-spotlag-live has been stopped since 09-25 02:44 UTC. It
   was stopped outside this session and had no open positions.

## Reproduce

- **Order and trade tables:** `data/trade_forensics.<tag>.jsonl` (entries), joined to `data/live_trades.<tag>.jsonl`
  by `signal_id`.
- **Order-book audit:** for each order, the recorded ask and depth at `t7_venue_fill + 500 ms` for fills, and at
  decision + 700 ms for rejections. The source is the research feature cache (`research_repriced_momentum.py cache`,
  rebuilt 12:55 UTC).
- **Replay:** `research_spot_lag_entry_sweep.py build`, then `simulate()` with each setting, sliced by time.
