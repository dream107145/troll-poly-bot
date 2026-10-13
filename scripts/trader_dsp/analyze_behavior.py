"""Behaviour and performance statistics from the cleaned ledgers (trade history only, full period).

Usage: analyze_behavior.py PROCESSED_DIR RAW_DIR
Writes PROCESSED_DIR/metrics_behavior.json and a few tables; prints a readable summary.
"""
import json, sys, collections
from pathlib import Path

import numpy as np
import pandas as pd

P, RAW = Path(sys.argv[1]), Path(sys.argv[2])
tr = pd.read_csv(P / "trades.csv.gz")
mk = pd.read_csv(P / "markets.csv")
pos = pd.read_csv(P / "positions.csv")
red = pd.read_csv(P / "redemptions.csv")
rr = pd.read_csv(P / "rebates_rewards.csv")
daily = pd.read_csv(P / "daily.csv")
OUT = {}


def pct(v, ps=(1, 5, 10, 25, 50, 75, 90, 95, 99)):
    v = np.asarray(v, float)
    v = v[~np.isnan(v)]
    return {f"p{p}": round(float(np.percentile(v, p)), 4) for p in ps} | {"mean": round(float(v.mean()), 4), "n": int(len(v))}


def clean(v):
    if isinstance(v, dict):
        return {(" ".join(map(str, k)) if isinstance(k, tuple) else str(k)): clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


def say(k, v):
    v = clean(v)
    OUT[k] = v
    print(f"\n## {k}\n{json.dumps(v, indent=1, default=str)[:3000]}")


tr["time"] = pd.to_datetime(tr["ts"], unit="s", utc=True)
tr["date"] = tr["time"].dt.strftime("%Y-%m-%d")
tr["month"] = tr["time"].dt.strftime("%Y-%m")
tr["is_taker"] = tr["role"] == "taker"

# ------------------------------------------------------------------ 1. coverage
days = pd.date_range(tr["time"].min().normalize(), tr["time"].max().normalize(), freq="D").strftime("%Y-%m-%d")
traded_days = set(tr["date"])
cov = {"first_fill_utc": str(tr["time"].min()), "last_fill_utc": str(tr["time"].max()),
       "calendar_days": len(days), "days_with_fills": len(traded_days),
       "days_without_fills": [d for d in days if d not in traded_days],
       "fills": len(tr), "markets": int(mk["slug"].nunique()), "positions": len(pos),
       "fills_by_source": tr["source"].value_counts().to_dict()}
for a, g in mk.groupby("asset"):
    t = pd.to_datetime(g["market_open"], unit="s", utc=True)
    cov[f"{a}_markets"] = len(g)
    cov[f"{a}_first_last"] = [str(t.min()), str(t.max())]
# share of the 288 daily BTC slots that were traded, per full day
btc = mk[mk["asset"] == "BTC"].copy()
btc["date"] = pd.to_datetime(btc["market_open"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
per_day = btc.groupby("date").size()
cov["btc_slots_traded_per_day"] = pct(per_day.values[1:-1] / 288 * 100, (5, 25, 50, 75, 95))
eth = mk[mk["asset"] == "ETH"].copy()
eth["date"] = pd.to_datetime(eth["market_open"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
cov["eth_markets_per_day"] = eth.groupby("date").size().to_dict()
say("coverage", cov)

# hour of day (UTC) activity
tr["hour"] = tr["time"].dt.hour
hod = tr.groupby("hour").agg(fills=("fill_id", "size"), usdc=("cost", "sum"))
hod["markets"] = tr.groupby("hour")["slug"].nunique()
hod.to_csv(P / "by_hour_utc.csv")
say("hour_of_day_usdc_share_pct", (hod["usdc"] / hod["usdc"].sum() * 100).round(2).to_dict())

# ------------------------------------------------------------------ 2. execution: taker / maker, fees
ex = {}
for r, g in tr.groupby("role"):
    ex[r] = {"fills": len(g), "fill_share_pct": round(len(g) / len(tr) * 100, 2), "usdc": round(g["cost"].sum(), 2),
             "usdc_share_pct": round(g["cost"].sum() / tr["cost"].sum() * 100, 2), "fees": round(g["fee"].sum(), 2),
             "shares": round(g["shares"].sum(), 2)}
tk = tr[tr["is_taker"]]
ex["fee_vs_model"] = {"chain_fee_total": round(tk["fee"].sum(), 2), "model_0.07pq_total": round(tk["fee_model"].sum(), 2),
                      "abs_diff_p99": round(float(np.percentile((tk["fee"] - tk["fee_model"]).abs(), 99)), 5),
                      "fee_pct_of_taker_notional": round(tk["fee"].sum() / (tk["cost"] - tk["fee"]).sum() * 100, 3)}
# api fee (usdcSize - price*size) vs chain fee
both = tr[tr["fee_api"].notna()]
ex["api_vs_chain_fee_absdiff_max"] = round(float((both["fee_api"] - both["fee"]).abs().max()), 6)
ex["api_vs_chain_cost_absdiff_sum"] = round(float((both["usdc_api"] - both["cost"]).abs().sum()), 4)
# multiple wallet fills in one tx
txn = tr.groupby("tx").size()
ex["fills_per_tx"] = txn.value_counts().sort_index().to_dict()
# same-second same-token clusters of taker orders (separate orders fired back to back)
cl = tk.groupby(["slug", "outcome", "ts"]).size()
ex["taker_orders_in_same_second_same_token"] = {"clusters_gt1": int((cl > 1).sum()),
                                                "share_of_taker_fills_in_clusters_pct": round(cl[cl > 1].sum() / len(tk) * 100, 2),
                                                "cluster_size_dist": cl.value_counts().sort_index().head(10).to_dict()}
# maker counterparties (the taker who hit the wallet's resting order)
mkf = tr[tr["role"] == "maker"]
cp = mkf.groupby("counterparty")["cost"].agg(["size", "sum"]).sort_values("sum", ascending=False)
ex["maker_counterparties"] = {"distinct": int(len(cp)), "top5_share_of_maker_usdc_pct": round(cp["sum"].head(5).sum() / cp["sum"].sum() * 100, 2),
                              "top5": {k[:10]: [int(v["size"]), round(v["sum"], 0)] for k, v in cp.head(5).iterrows()}}
say("execution", ex)

# ------------------------------------------------------------------ 3. timing
tm = {"t_from_open_all": pct(tr["t_from_open"]), "t_from_open_taker": pct(tk["t_from_open"]),
      "t_from_open_maker": pct(mkf["t_from_open"]),
      "first_fill_s": pct(mk["first_fill_s"]), "last_fill_s": pct(mk["last_fill_s"]),
      "fills_before_open": int((tr["t_from_open"] < 0).sum()), "fills_after_close": int((tr["t_from_open"] >= 300).sum())}
b = pd.cut(tr["t_from_open"], np.arange(-30, 331, 30))
tb = tr.groupby(b, observed=False).agg(fills=("fill_id", "size"), usdc=("cost", "sum"), taker_share=("is_taker", "mean"),
                                       avg_price=("price", "mean"), pnl=("pnl_at_settlement", "sum"))
tb["usdc_share_pct"] = tb["usdc"] / tb["usdc"].sum() * 100
tb["pnl_pct_of_usdc"] = tb["pnl"] / tb["usdc"] * 100
tb.to_csv(P / "by_time_in_window.csv")
tb.index = tb.index.astype(str)
tm["by_30s_bucket"] = tb.round(3).to_dict(orient="index")
# gaps between consecutive orders (distinct tx) in a market
o = tr.drop_duplicates(["slug", "tx"]).sort_values(["slug", "ts"])
gap = o.groupby("slug")["ts"].diff().dropna()
tm["gap_between_orders_in_market_s"] = pct(gap)
tm["fills_per_market"] = pct(mk["fills"])
tm["orders_per_market"] = pct(o.groupby("slug").size())
tm["fills_per_day"] = pct(daily["fills"].values[1:-1])
tm["markets_per_day"] = pct(daily["markets"].values[1:-1])
say("timing", tm)

# ------------------------------------------------------------------ 4. prices
pr = {"price_all": pct(tr["price"]), "price_taker": pct(tk["price"]), "price_maker": pct(mkf["price"])}
bins = np.round(np.arange(0, 1.0001, 0.05), 2)
pb = tr.groupby([pd.cut(tr["price"], bins, include_lowest=True), "role"], observed=False).agg(
    fills=("fill_id", "size"), usdc=("cost", "sum"), shares=("shares", "sum"), payout=("payout", "sum"),
    pnl=("pnl_at_settlement", "sum")).reset_index()
pb["win_rate_pct"] = pb["payout"] / pb["shares"] * 100
pb["pnl_pct_of_usdc"] = pb["pnl"] / pb["usdc"] * 100
pb.to_csv(P / "by_price_bucket_role.csv", index=False)
pr["share_fills_price_0.10_0.90_pct"] = round(tr["price"].between(0.10, 0.90).mean() * 100, 2)
pr["share_usdc_price_gt_0.90_pct"] = round(tr.loc[tr["price"] > 0.90, "cost"].sum() / tr["cost"].sum() * 100, 3)
pr["share_usdc_price_lt_0.10_pct"] = round(tr.loc[tr["price"] < 0.10, "cost"].sum() / tr["cost"].sum() * 100, 3)
pr["favourite_share_taker_pct"] = round((tk["price"] > 0.5).mean() * 100, 2)
pr["favourite_share_maker_pct"] = round((mkf["price"] > 0.5).mean() * 100, 2)
pr["up_share_of_usdc_pct"] = round(tr.loc[tr["outcome"] == "Up", "cost"].sum() / tr["cost"].sum() * 100, 2)
say("prices", pr)

# ------------------------------------------------------------------ 5. sizes
sz = {"shares_taker": pct(tk["shares"]), "shares_maker": pct(mkf["shares"]),
      "usdc_taker": pct(tk["cost"]), "usdc_maker": pct(mkf["cost"]),
      "common_taker_sizes": {str(k): int(v) for k, v in tk["shares"].round(2).value_counts().head(12).items()},
      "common_maker_sizes": {str(k): int(v) for k, v in mkf["shares"].round(2).value_counts().head(12).items()},
      "taker_integer_share_pct": round((tk["shares"] - tk["shares"].round()).abs().lt(1e-6).mean() * 100, 2),
      "maker_integer_share_pct": round((mkf["shares"] - mkf["shares"].round()).abs().lt(1e-6).mean() * 100, 2),
      "market_cost": pct(mk["cost"]), "market_cost_max": round(mk["cost"].max(), 2)}
mon = tr.groupby(["month", "role"])["shares"].agg(["median", "mean", lambda s: s.quantile(0.9)]).round(2)
mon.columns = ["median", "mean", "p90"]
sz["shares_by_month_role"] = {f"{a} {b}": v for (a, b), v in mon.to_dict(orient="index").items()}
mm = mk.copy()
mm["month"] = pd.to_datetime(mm["market_open"], unit="s", utc=True).dt.strftime("%Y-%m")
sz["market_cost_by_month"] = mm.groupby("month")["cost"].describe(percentiles=[0.5, 0.9]).round(1)[["count", "50%", "90%", "max"]].to_dict(orient="index")
# weekly common taker sizes (does the clip size change over time?)
tk2 = tk.copy()
tk2["week"] = tk2["time"].dt.strftime("%G-W%V")
sz["top3_taker_sizes_by_week"] = {w: [f"{k:g}" for k in g["shares"].round(2).value_counts().head(3).index] for w, g in tk2.groupby("week")}
say("sizes", sz)

# ------------------------------------------------------------------ 6. sequences: adds, switches
seq = {}
o = tr.sort_values(["slug", "ts", "fill_id"])
# collapse fills of one taker order (same tx) into an order
orders = o.groupby(["slug", "tx", "outcome", "role"], sort=False).agg(ts=("ts", "first"), shares=("shares", "sum"),
                                                                     cost=("cost", "sum"), price=("price", "mean")).reset_index()
orders = orders.sort_values(["slug", "ts"]).reset_index(drop=True)
prev_side = orders.groupby("slug")["outcome"].shift()
seq["orders"] = len(orders)
seq["consecutive_orders_switch_side_pct"] = round((orders["outcome"] != prev_side)[prev_side.notna()].mean() * 100, 2)
# same-side adds: price vs the previous order on the same token
orders["prev_same_px"] = orders.groupby(["slug", "outcome"])["price"].shift()
orders["prev_same_ts"] = orders.groupby(["slug", "outcome"])["ts"].shift()
adds = orders[orders["prev_same_px"].notna()]
d = adds["price"] - adds["prev_same_px"]
seq["same_token_adds"] = {"n": len(adds), "higher_price_pct": round((d > 0.005).mean() * 100, 2),
                          "same_price_pct": round((d.abs() <= 0.005).mean() * 100, 2),
                          "lower_price_pct": round((d < -0.005).mean() * 100, 2),
                          "price_change_c": pct(d * 100, (10, 25, 50, 75, 90))}
for r in ("taker", "maker"):
    a2 = adds[adds["role"] == r]
    d2 = a2["price"] - a2["prev_same_px"]
    seq[f"same_token_adds_{r}"] = {"n": len(a2), "higher_pct": round((d2 > 0.005).mean() * 100, 2),
                                   "lower_pct": round((d2 < -0.005).mean() * 100, 2)}
# side switch: implied P(Up) moved by >= 3c since the previous order, in which direction relative to the new side
orders["pu"] = np.where(orders["outcome"] == "Up", orders["price"], 1 - orders["price"])
orders["prev_pu"] = orders.groupby("slug")["pu"].shift()
orders["prev_side"] = prev_side
sw = orders[(orders["outcome"] != orders["prev_side"]) & orders["prev_side"].notna()]
mv = (sw["pu"] - sw["prev_pu"]) * np.where(sw["outcome"] == "Up", 1, -1)
seq["side_switches"] = {"n": len(sw), "new_side_had_risen_3c_pct": round((mv >= 0.03).mean() * 100, 2),
                        "new_side_had_fallen_3c_pct": round((mv <= -0.03).mean() * 100, 2),
                        "note": "implied P(Up) from fill prices; a taker buy of Up prices off the Up ask and of Down off the Up bid, so a switch carries ~1 spread of mechanical move toward the new side"}
mk_first_big = (mk["first_side"] == mk["bigger_side"]).mean()
seq["first_side_ends_bigger_pct"] = round(mk_first_big * 100, 2)
last_side = tr.sort_values("ts").groupby("slug")["outcome"].last()
mk2 = mk.set_index("slug").loc[last_side.index]
seq["last_order_side_is_bigger_pct"] = round((last_side == mk2["bigger_side"]).mean() * 100, 2)
seq["last_order_side_won_pct"] = round((last_side == mk2["winner"]).mean() * 100, 2)
seq["first_order_side_won_pct"] = round((mk2["first_side"] == mk2["winner"]).mean() * 100, 2)
say("sequences", seq)

# ------------------------------------------------------------------ 7. hedging / pairs
hd = {"markets_both_sides_pct": round(mk["both_sides"].mean() * 100, 2),
      "pair_cost": pct(mk["pair_cost"], (10, 25, 50, 75, 90)),
      "pair_cost_below_1_pct": round((mk["pair_cost"] < 1).mean() * 100, 2),
      "hedged_share_of_bigger_side": pct(mk["hedged_shares"] / mk[["shares_up", "shares_down"]].max(axis=1), (10, 25, 50, 75, 90)),
      "lean_frac": pct(mk["lean_frac"], (10, 25, 50, 75, 90)),
      "bigger_side_won_pct": round(mk.loc[mk["bigger_side"] != "tie", "bigger_side_won"].mean() * 100, 2),
      "pnl_hedged_part": round(mk["pnl_hedged_part"].sum(), 2), "pnl_lean_part": round(mk["pnl_lean_part"].sum(), 2)}
# when is the opposite side first bought, relative to the first fill
fo = tr.groupby(["slug", "outcome"])["ts"].min().unstack()
first_side_ts = fo.min(axis=1)
second_ts = fo.max(axis=1)
dd = (second_ts - first_side_ts)[fo.notna().all(axis=1)]
hd["delay_first_to_opposite_side_s"] = pct(dd, (10, 25, 50, 75, 90))
lb = pd.cut(mk["lean_frac"], [0, 0.1, 0.25, 0.5, 1.0001], include_lowest=True)
hd["by_lean"] = mk.groupby(lb, observed=False).agg(markets=("slug", "size"), bigger_won=("bigger_side_won", "mean"),
                                                   pnl=("pnl", "sum"), cost=("cost", "sum")).round(3).rename(index=str).to_dict(orient="index")
say("hedging", hd)

# ------------------------------------------------------------------ 8. exit / redemption
mk["close"] = mk["market_open"] + 300
rd = red.merge(mk[["slug", "close"]], on="slug", how="left")
rd["delay"] = rd["ts"] - rd["close"]
won_pos = pos[pos["won"]]
openp = json.load(open(RAW / "positions.json"))
op = pd.DataFrame(openp)
ext = {"sells": int((tr["side"] != "BUY").sum()), "merges_splits": 0,
       "redeem_records": len(red), "redeemed_usdc": round(red["usdc"].sum(), 2),
       "redeem_delay_after_close_s": pct(rd["delay"], (10, 25, 50, 75, 90, 99)),
       "winning_positions": len(won_pos), "winning_positions_unredeemed": int((won_pos["status"] == "won, NOT redeemed").sum()),
       "unredeemed_winner_value": round(won_pos.loc[won_pos["status"] == "won, NOT redeemed", "settlement_value"].sum(), 2),
       "settlement_value_of_winners": round(won_pos["settlement_value"].sum(), 2),
       "api_positions_now": len(op), "api_positions_now_cur_value": round(float(op["currentValue"].sum()), 2) if len(op) else 0,
       "api_positions_now_cost": round(float(op["initialValue"].sum()), 2) if len(op) else 0,
       "api_positions_now_redeemable": int(op["redeemable"].sum()) if len(op) else 0,
       "api_positions_curprice_counts": op["curPrice"].round(3).value_counts().head(5).to_dict() if len(op) else {}}
say("exit", ext)

# ------------------------------------------------------------------ 9. risk / sizing reaction
rk = {}
for a, g in mk.sort_values("market_open").groupby("asset"):
    g = g.reset_index(drop=True)
    prev = g["pnl"].shift()
    ratio = g["cost"] / g["cost"].rolling(48, min_periods=12).median().shift()
    rk[f"{a}_cost_vs_trailing_median_after_loss"] = round(float(ratio[prev < 0].median()), 3)
    rk[f"{a}_cost_vs_trailing_median_after_win"] = round(float(ratio[prev > 0].median()), 3)
    big_loss = prev < prev.quantile(0.05)
    rk[f"{a}_cost_vs_trailing_median_after_worst5pct_loss"] = round(float(ratio[big_loss].median()), 3)
    rk[f"{a}_corr_prev_pnl_next_cost"] = round(float(np.corrcoef(prev[1:], g["cost"][1:])[0, 1]), 4)
slot = mk.groupby("market_open")["cost"].sum()
rk["cost_per_5min_slot"] = pct(slot, (50, 90, 99))
rk["cost_per_5min_slot_max"] = round(slot.max(), 2)
rk["market_pnl"] = pct(mk["pnl"], (1, 5, 10, 25, 50, 75, 90, 95, 99))
rk["markets_positive_pct"] = round((mk["pnl"] > 0).mean() * 100, 2)
rk["market_pnl_pct_of_cost"] = pct(mk["pnl"] / mk["cost"] * 100, (5, 25, 50, 75, 95))
# next-day volume after a losing market-P&L day
dl = daily.iloc[1:-1].reset_index(drop=True)
nxt = dl["bought"].shift(-1) / dl["bought"]
rk["next_day_volume_ratio_after_negative_market_day"] = round(float(nxt[dl["market_pnl"] < 0].median()), 3)
rk["next_day_volume_ratio_after_positive_market_day"] = round(float(nxt[dl["market_pnl"] > 0].median()), 3)
# streaks of losing markets (BTC sequence)
s = (mk[mk["asset"] == "BTC"].sort_values("market_open")["pnl"] < 0).astype(int).values
best = cur = 0
for v in s:
    cur = cur + 1 if v else 0
    best = max(best, cur)
rk["longest_losing_market_streak_btc"] = int(best)
say("risk", rk)

# ------------------------------------------------------------------ 10. performance
pf = {}
tot_cost, fees = tr["cost"].sum(), tr["fee"].sum()
settle = tr["payout"].sum()
reb = rr.groupby("type")["usdc"].sum().to_dict()
mpnl = mk["pnl"].sum()
pf["totals"] = {"bought_usdc_incl_fees": round(tot_cost, 2), "taker_fees": round(fees, 2),
                "bought_ex_fees": round(tot_cost - fees, 2), "settlement_value": round(settle, 2),
                "redeemed_usdc": round(red["usdc"].sum(), 2),
                "market_pnl_after_fees": round(mpnl, 2), "market_pnl_before_fees": round(mpnl + fees, 2),
                "market_pnl_pct_of_bought": round(mpnl / tot_cost * 100, 3),
                **{k.lower(): round(v, 2) for k, v in reb.items()},
                "net_pnl": round(mpnl + sum(reb.values()), 2),
                "unrealised_pnl_open_positions": 0.0}
pf["by_asset"] = mk.groupby("asset").agg(markets=("slug", "size"), bought=("cost", "sum"), fees=("fees", "sum"),
                                         pnl=("pnl", "sum")).assign(pnl_pct=lambda x: x["pnl"] / x["bought"] * 100).round(2).to_dict(orient="index")
pf["by_role_fill_level"] = tr.groupby("role").agg(bought=("cost", "sum"), fees=("fee", "sum"), payout=("payout", "sum"),
                                                  pnl=("pnl_at_settlement", "sum")).assign(pnl_pct=lambda x: x["pnl"] / x["bought"] * 100).round(2).to_dict(orient="index")
pf["by_outcome_token"] = tr.groupby("outcome").agg(bought=("cost", "sum"), shares=("shares", "sum"), payout=("payout", "sum"),
                                                   pnl=("pnl_at_settlement", "sum")).assign(avg_cost=lambda x: x["bought"] / x["shares"], win_rate=lambda x: x["payout"] / x["shares"]).round(4).to_dict(orient="index")
pf["avg_entry_price_ex_fee_by_outcome_asset"] = tr.assign(nx=tr["cost"] - tr["fee"]).groupby(["asset", "outcome"]).apply(
    lambda g: round(g["nx"].sum() / g["shares"].sum(), 4), include_groups=False).to_dict()
pf["by_month"] = tr.groupby("month").agg(bought=("cost", "sum"), fees=("fee", "sum"), pnl=("pnl_at_settlement", "sum")).assign(
    pnl_pct=lambda x: x["pnl"] / x["bought"] * 100).round(2).to_dict(orient="index")
# positions: won / lost
pf["positions"] = {"total": len(pos), "won": int(pos["won"].sum()), "lost": int((~pos["won"]).sum()),
                   "won_cost": round(pos.loc[pos["won"], "cost"].sum(), 2), "lost_cost": round(pos.loc[~pos["won"], "cost"].sum(), 2),
                   "position_cost": pct(pos["cost"], (10, 25, 50, 75, 90, 99)), "avg_position_cost": round(pos["cost"].mean(), 2)}
pf["markets"] = {"total": len(mk), "pnl_positive": int((mk["pnl"] > 0).sum()), "pnl_negative": int((mk["pnl"] < 0).sum()),
                 "pnl_zero": int((mk["pnl"] == 0).sum())}
top = mk.sort_values("pnl")
cols = ["slug", "fills", "cost", "shares_up", "shares_down", "avg_cost_up", "avg_cost_down", "winner", "pnl"]
pf["largest_losses"] = top.head(10)[cols].round(3).to_dict(orient="records")
pf["largest_gains"] = top.tail(10)[cols].round(3).iloc[::-1].to_dict(orient="records")
# daily stats
dv = daily.iloc[:-1]                   # drop the partial last day
pf["daily"] = {"days": len(dv), "total_pnl_positive_days": int((dv["total_pnl"] > 0).sum()),
               "market_pnl_positive_days": int((dv["market_pnl"] > 0).sum()),
               "total_pnl_mean": round(dv["total_pnl"].mean(), 2), "total_pnl_std": round(dv["total_pnl"].std(), 2),
               "total_pnl_sharpe_daily": round(dv["total_pnl"].mean() / dv["total_pnl"].std(), 3),
               "best_day": dv.loc[dv["total_pnl"].idxmax(), ["date_utc", "total_pnl"]].to_dict(),
               "worst_day": dv.loc[dv["total_pnl"].idxmin(), ["date_utc", "total_pnl"]].to_dict(),
               "worst_market_pnl_day": dv.loc[dv["market_pnl"].idxmin(), ["date_utc", "market_pnl"]].to_dict()}
# drawdown on the event-time equity curve: market P&L at resolution (close), rebates/rewards at payment
ev = pd.concat([pd.DataFrame({"ts": mk["market_open"] + 300, "x": mk["pnl"], "k": "market"}),
                pd.DataFrame({"ts": rr["ts"], "x": rr["usdc"], "k": "rebate"})]).sort_values("ts")
for name, e in (("total", ev), ("market_only", ev[ev["k"] == "market"])):
    eq = e["x"].cumsum().values
    peak = np.maximum.accumulate(eq)
    ddv = eq - peak
    i = int(np.argmin(ddv))
    j = int(np.argmax(eq[:i + 1])) if i > 0 else 0
    rec = np.where(eq[i:] >= peak[i])[0]
    pf[f"max_drawdown_{name}"] = {"usdc": round(float(ddv[i]), 2),
                                  "peak_time": str(pd.to_datetime(e["ts"].values[j], unit="s", utc=True)),
                                  "trough_time": str(pd.to_datetime(e["ts"].values[i], unit="s", utc=True)),
                                  "recovered_time": str(pd.to_datetime(e["ts"].values[i + rec[0]], unit="s", utc=True)) if len(rec) else "not recovered"}
# capital: running cash from trading only (buys out at fill, redemptions / rebates in at payment)
cf = pd.concat([pd.DataFrame({"ts": tr["ts"], "x": -tr["cost"]}), pd.DataFrame({"ts": red["ts"], "x": red["usdc"]}),
                pd.DataFrame({"ts": rr["ts"], "x": rr["usdc"]})]).sort_values("ts", kind="stable")
cash = cf["x"].cumsum().values
k = int(np.argmin(cash))
pf["capital"] = {"min_running_trading_cash": round(float(cash[k]), 2),
                 "min_running_trading_cash_time": str(pd.to_datetime(cf["ts"].values[k], unit="s", utc=True)),
                 "final_running_trading_cash": round(float(cash[-1]), 2)}
# open exposure: cost of positions bought and not yet resolved, sampled at every fill
ivals = mk[["market_open", "cost"]].copy()
ivals["start"] = mk["market_open"] + mk["first_fill_s"]
ivals["end"] = mk["market_open"] + 300
evs = pd.concat([pd.DataFrame({"ts": tr["ts"], "x": tr["cost"]}), pd.DataFrame({"ts": ivals["end"] + 0.5, "x": -ivals["cost"]})]).sort_values("ts", kind="stable")
expo = evs["x"].cumsum().values
pf["open_exposure_usdc"] = {"max": round(float(expo.max()), 2), **pct(expo[expo > 1], (50, 90, 99))}
pf["turnover"] = {"bought_per_day_mean": round(tot_cost / len(dv), 2),
                  "bought_over_max_exposure": round(tot_cost / float(expo.max()), 1)}
say("performance", pf)

json.dump(OUT, open(P / "metrics_behavior.json", "w"), indent=1, default=str)
orders.to_csv(P / "orders.csv.gz", index=False, compression="gzip")
print("\nwritten", P / "metrics_behavior.json")
