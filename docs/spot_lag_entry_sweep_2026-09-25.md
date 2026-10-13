# Spot-lag entry sweep and the MAX_ASK 0.74 paper bot (2026-09-25)

**Answer.** The parameters stay as they are; the paper bot's weak start was a CPU-priority problem, now fixed.

- **The poor start was an execution fault, not the parameters.** tpb-spotlag-paper-ma74 had run 40 minutes: 7 orders,
  1 fill, −$0.60.
  - I had started it at low CPU priority (nice 10) on an overloaded machine (load about 7 on 6 cores). It fell behind
    its own feeds and traded a stale catch-up burst that the replay never sees.
  - It now runs at normal priority, with the same parameters.
- **A new sweep of 38,400 entry configurations** re-selected the bot's exact parameters, using training data only.
  - Every tighter rule makes more per share and trades less, for the same or lower total profit.
  - Nothing looser survived.

## 1. What happened to the paper bot

| | since 01:44 UTC |
|---|---|
| orders / fills | 7 / 1 (5 FOK orders arrived after the price moved) |
| the one trade | SOL DOWN, bought 0.73 at 01:51:33, sold 0.64 after 8 s: −$0.60 |
| replay of the same configuration, 01:44–02:10 | 4 orders, 1 fill (BTC DOWN 0.50 → 0.52, −1.5c) |

**The SOL trade came from stale data.**

- At 01:51:33 the bot logged all three spot feeds as 770–860 ms behind, excluded them, and let them back in within the
  same second. It fired the signal in that catch-up burst: z1 −3.65, ask 0.73, expected 0.793, a +6.3c lag.
- The archive at the same moment has the SOL DOWN ask at 0.78 with expected 0.781. The token had already repriced, so
  there was no lag, and the replay did not trade.
- All three exchanges falling behind at once is the bot's own processing, not the exchanges.

**The bot was starved of CPU** (01:44–02:27 UTC):

| bot | "feed behind" warnings | spot lag median / p90 | loop delay p99 (median) |
|---|---:|---|---:|
| tpb-spotlag-paper-ma74 (nice 10) | 82 | 224 / 293 ms | 121 ms |
| tpb-spotlag-paper | 12 | 194 / 232 ms | 66 ms |
| tpb-spotlag-live | 9 | 188 / 207 ms | 72 ms |

- **Fix:** restarted at 02:28 UTC at normal priority, like every other bot. The parameters are unchanged.
  `ecosystem.spotlag.config.js` records why.
- **Untested guard:** the engine's spot-age gate (`TPB_SPOT_LAG_MAX_SPOT_AGE_MS`, 1000 ms) let a composite spot about
  380 ms old through. A lower gate would block catch-up bursts, but the archive cannot show feed stalls, so it cannot
  be tested in replay. It was not changed.

## 2. The entry sweep

- **Grid:** shock threshold k (2.5–5σ) × minimum net edge (0–8c) × price limit above the ask (0–4c) × maximum spread
  (2–5c) × MAX_ASK (0.50–0.95) × time left (six windows). That is 38,400 configurations.
- **Fixed:** the production exit (5 s, held to 10 s while the spot keeps moving), the production budget (4 positions,
  60 buys per hour, 1 entry and 3 attempts per market), and HYPE excluded.
- **Why it is exact:** the fill model fills a FOK at the arrival ask when the ask is within the limit and the top
  level holds 5 shares. So one simulated order per shock, sent without a limit, gives the exact outcome for every
  limit.
  - Each k is detected with its own 2 s dedupe, as the engine does.
  - The pipeline reproduces the previous sweep: live rule unseen +6.3c on 77 fills here, against +6.25c on 72 before.
- **Protocol:** as in `docs/spot_lag_parameter_sweep.md`.
  - Training chooses, from configurations with at least 30 fills and a positive net.
  - Validation eliminates any pick with fewer than 5 fills or a loss.
  - The picks were locked at 2026-09-25 02:25:09 UTC, then scored on the unseen periods.
  - unseen_4 (09-25 00:50–02:10 UTC, 16 windows) is data no study had read.

**Locked picks** (c/share, with fills in brackets). Configuration = k, edge, price limit, spread, MAX_ASK, seconds left.

| pick | configuration | train | validation | unseen pooled | t | bootstrap 95% CI | unseen profit | unseen_4 | fills/h |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|
| live bot (reference) | 3σ, 2c, +2c, 3c, 0.95, 60–295 | +10.3 (117) | +5.0 (56) | +6.3 (77) | 2.1 | [+1.0, +12.8] | $24.3 | +6.8 (5) | 4.9 |
| **ma74 bot = "edge and MAX_ASK only" pick** | 3σ, 2c, +2c, 3c, 0.74, 60–295 | +12.3 (103) | +6.4 (44) | **+11.0 (58)** | 3.1 | [+5.0, +18.6] | **$31.9** | +12.4 (3) | 4.0 |
| tpb-spotlag-paper (reference) | 3σ, 1c, +4c, 5c, 0.60, 60–295 | +11.4 (105) | +6.6 (46) | +10.9 (62) | 3.5 | [+5.6, +17.3] | $33.8 | +4.0 (5) | 4.2 |
| risk-adjusted (best training t) | 2.5σ, 4c, +4c, 4c, 0.60, 90–240 | +12.2 (52) | +12.8 (20) | +13.5 (32) | 2.6 | [+5.2, +24.7] | $21.6 | +10.2 (3) | 2.0 |
| plateau (neighbour-averaged t) | 2.5σ, 4c, +4c, 3c, 0.60, 90–240 | +12.2 (51) | +12.8 (20) | +15.1 (29) | 2.7 | [+6.8, +27.8] | $21.9 | +10.2 (3) | 2.0 |
| high-quality (best c/share) | 4σ, 1c, +3c, 2c, 0.50, 60–200 | +27.5 (30) | +8.7 (10) | +18.2 (23) | 3.6 | [+9.7, +29.0] | $20.9 | −28.0 (1) | 1.2 |
| high-volume (most fills, t ≥ 2) | 2.5σ, 0c, +4c, 5c, 0.95, 90–295 | +2.2 (231) | +0.4 (106) | +0.5 (179) | 0.4 | [−1.7, +2.9] | $4.5 | −2.2 (15) | 10.1 |

- **The ma74 bot is already the training-optimal choice.** The "edge and MAX_ASK only" pick keeps the live rule's
  structure and tunes only those two. Training alone chose exactly the running values: 2c edge and MAX_ASK 0.74.
- **The tighter picks make more per share but less money.** They make +13.5 to +18.2c/share unseen on half the fills
  or fewer. That is $21–22 of unseen profit against $31.9 for the current settings, and their CIs overlap it.
- **The loosest pick earns nothing.** The high-volume pick doubles the fills and makes +0.5c/share.
- **The other paper bot's looser settings are equal in total.** tpb-spotlag-paper (1c edge, +4c limit, 5c spread,
  MAX_ASK 0.60) makes $33.8 unseen and $108.6 over all periods, against $31.9 and $109.3 for the ma74 bot.

## 3. One parameter at a time from the ma74 bot

The table gives unseen c/share (fills), and unseen profit in dollars.

| parameter | looser | **current** | tighter |
|---|---|---|---|
| shock threshold k | 2.5σ: +6.0 (70) $21.0 | **3σ: +11.0 (58) $31.9** | 3.5σ: +11.8 (54) $31.9 · 4σ: +12.7 (52) $33.0 · 5σ: +14.8 (38) $28.1 |
| minimum net edge | 0c: +4.6 (105) $24.2 · 1c: +7.5 (75) $28.1 | **2c** | 3c: +12.5 (47) $29.4 · 4c: +13.9 (43) $29.9 · 6c: +17.6 (33) $29.0 · 8c: +20.8 (27) $28.1 |
| price limit above ask | 3c: +11.2 (59) $33.0 · 4c: +10.2 (62) $31.6 | **2c** | 1c: +12.5 (51) $31.9 · 0c: +11.5 (47) $27.0 |
| maximum spread | 4c: +10.8 (59) · 5c: +11.3 (64) | **3c** | 2c: +10.2 (53) |
| MAX_ASK | 0.80: +8.4 (67) $28.1 · 0.95: +6.3 (77) $24.3 | **0.74** | 0.65: +12.9 (48) $31.0 · 0.60: +14.7 (42) $30.9 · 0.50: +15.1 (34) $25.7 |
| seconds left | 90–295: +8.8 (51) · 120–295: +9.1 (48) | **60–295** | 60–240: +12.5 (49) $30.6 · 60–200: +14.1 (39) $27.5 |

- **Per share against volume.** Every tighter setting raises profit per share and lowers fills by about the same
  proportion. Total unseen profit stays within $27–33 in every direction, and no neighbour beats the current setting
  by more than $1.1 (4σ, or a 3c limit).
- **Training shows the same flat plateau.** The current setting makes $63.3 on training. The best one-step neighbours
  make $65.6 (3.5σ), $65.5 (MAX_ASK 0.65) and $64.7 (a 3c limit), all within 4%. Tighter moves make less: $55.9 at
  4σ and $60.4 at a 4c edge. Picking any of these would be picking noise.
- **Looser is worse.** A 2.5σ shock, a 0–1c edge, or MAX_ASK 0.80 or higher loses both per share and in total.
- **To change it, pick a trade-off.** Retuning only moves the bot along this curve. If fewer, better trades matter
  more than total profit, a 4c edge (+13.9c/share, 3.0 fills/h) or 4σ (+12.7c/share, lower drawdown) are the
  defensible options. They are not better in total.

## 4. Limitations

- **The numbers above are replay.** The bot's own run is the only live-data evidence: one trade in 40 minutes, lost
  to a CPU fault. The replay has filled about twice as often as the live bot (docs/spot_lag_parameter_sweep.md,
  limitation 1).
- **Rank agreement is inflated.** Training and unseen c/share rank-correlate at +0.89 across configurations, but
  neighbouring configurations share most of their trades, so this is not independent confirmation.
- **The MAX_ASK question is not fully blind.** The unseen periods were already used for it in the previous study.
  unseen_4 (1.3 hours, 3–15 fills per pick) is too short to decide anything.
- **Feed stalls are invisible to the replay.** The archive cannot show them, so it cannot price the kind of trade that
  caused the loss.

## Reproduce

```
.venv/bin/python scripts/research_repriced_momentum.py cache
.venv/bin/python scripts/research_spot_lag_entry_sweep.py build
.venv/bin/python scripts/research_spot_lag_entry_sweep.py sweep     # results/spot_lag_entry_sweep.csv
.venv/bin/python scripts/research_spot_lag_entry_sweep.py analyze   # _locked.json, _report.md
```
