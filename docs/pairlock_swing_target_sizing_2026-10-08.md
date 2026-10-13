# Swing recovery: target-profit recovery sizing — 2026-10-08

## The rule

Swing 1 and swing 2 no longer buy a fixed 24 / 80 shares. At each recovery the bot buys the **smallest whole number of
shares x** of the new leader with

    profit if it wins = held + x − (total cost so far + walked-ask cost of x)  >  TARGET_RECOVERY_PROFIT

How the size is worked out:
* **Inventory and cost:** the market's whole current inventory and every cost paid so far (both sides, fees in). Swing 2 starts from what swing 1 left.
* **Pricing:** the live ask levels, walked share by share. The quantity and its VWAP come from the same walk, so a size that reaches dearer levels is priced there.
* **Fee:** the Polymarket fee, 0.07·p·(1−p) per share at each level's price.
* **Spread:** not added. The asks already contain it, so it is reported on its own (`spread`, `spread_cost_vs_mid` in the `[SWING_EVAL]` record).

**Example:** 10 A bought for 6.00 and B at 0.74 → 37 shares, and the B win pays +3.12 (36 would pay +2.88). With the entry's own fee the cost is 6.17, which makes it 38.

**All or nothing.** The size is **refused** (nothing bought, tried again on the next sample while the swing stands) when the whole size does not fit:
* the visible depth;
* `MAX_RECOVERY_SHARES` (one swing's recovery, all its orders; default 150);
* `MAX_RECOVERY_PRICE` (no ask level above it; default 0.85);
* the market capital cap (worst case: every share at the order's limit plus fee);
* the position cap;
* the bankroll.

The order's limit is the deepest level the size needs, plus `MAX_SLIPPAGE` (0.03) while the capital cap allows it.

**Partial fills.** If a fill-and-kill order fills only partly, the next sample sizes again from the new inventory.

**Unchanged:** the hedge at swing 3+, the swing definition, the entry and every other rule.

**Settings:**
* Env: `TPB_PAIRLOCK_SWING_SIZING=target`, `TPB_PAIRLOCK_SWING_TARGET_RECOVERY_PROFIT=3.00`, `TPB_PAIRLOCK_SWING_MAX_RECOVERY_SHARES=150`, `TPB_PAIRLOCK_SWING_MAX_RECOVERY_PRICE=0.85`.
* Variables page: "Recovery target profit", "Max recovery shares", "Recovery: highest ask level".
* The "Swing 1/2 shares" rows are gone. `swing_sizing="fixed"` remains only so the 2026-10-07 backtest and sweep reproduce.

Code is in `strategy/pairlock_swing.py`: `target_quantity`, `_target_recovery`, and `projected_additional_capital` / `plan_path`. The tests are `tests/test_pairlock_swing_target.py` (20); every size there is checked against a brute-force search.

## Targets $1–$5 on recorded markets (realistic execution)

* **Script:** `scripts/pairlock/replay_swing_targets.py`. It runs the bot's decisions on every sample with the realistic fill model: +143 ms arrival, walked asks, fill-and-kill, 1 s survival check, fees.
* **Data:** the same frozen 3,219-market snapshot and periods as the 2026-10-07 sweep.
* **Other settings:** cap 125, hedge 100 %.
* **Results:** `results/pairlock_swing_targets_2026-10-08.json`.

| Sizing | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Total | Swing-1 size | Swing-2 bought / reached, size | By bucket S0 / S1 / S2 / S3+ |
|---|--:|--:|--:|--:|--:|---|---|---|
| fixed 24 / 80 | −0.361 | −0.396 | −0.870 | −0.435 | −1,399 | 24 @ 0.741 | 280/283, 79 @ 0.767 | +2,599 / −181 / +287 / −4,103 |
| target $1 | −0.208 | −0.163 | −1.117 | −0.314 | −1,009 | 30 @ 0.733 | 240/283, 76 @ 0.731 | +2,599 / +612 / −701 / −3,519 |
| target $2 | −0.203 | −0.158 | −1.307 | −0.333 | −1,072 | 34 @ 0.734 | 227/283, 85 @ 0.722 | +2,599 / +1,098 / −894 / −3,874 |
| target $3 | −0.265 | −0.137 | −1.050 | −0.334 | −1,075 | 38 @ 0.734 | 212/283, 95 @ 0.709 | +2,599 / +1,589 / −1,266 / −3,997 |
| target $4 | −0.372 | −0.121 | −0.912 | −0.380 | −1,223 | 43 @ 0.734 | 202/283, 102 @ 0.694 | +2,599 / +2,080 / −1,763 / −4,138 |
| target $5 | −0.337 | −0.197 | −1.025 | −0.391 | −1,258 | 47 @ 0.734 | 177/283, 107 @ 0.670 | +2,599 / +2,569 / −2,472 / −3,953 |

**Reading:**
* Every target is still **negative in every period**.
* All targets beat the fixed sizes over the whole history: −0.31 to −0.39 per market, against −0.44. In the test period only target $4 does (−0.91 vs −0.87 fixed).
* What the target does:
  * It makes swing-1 markets pay as designed (+612 to +2,569).
  * It moves the losses into swing 2. A larger target needs a larger swing-2 size, which the 125 cap refuses more often (283 reached; 280 bought at fixed sizes, 177 at a $5 target). A refused swing 2 leaves the market a bet on the wrong side.
  * Swing 3+ still loses about −38 to −44 per market.
* The swing-2 VWAP falls as the target rises (0.731 → 0.670) because the swings that are bought are the cheaper ones: the expensive ones are refused.
* No target makes the strategy profitable; the conclusion of the 2026-10-07 sweep stands.
