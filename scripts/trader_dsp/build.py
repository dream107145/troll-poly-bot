"""Build the cleaned ledgers for one wallet from the raw data-api + gamma + Polygon files.

Usage: build.py RAW_DIR OUT_DIR

Accounting (see the report for the reasoning):
  * one row per fill = one exchange OrderFilled event of an order signed by the wallet.  The data-api record and
    the on-chain event are matched by (tx hash, token, shares); fills that exist only on-chain are added from the
    chain with their block time; role (taker / maker) and fee come from the event (taker = exchange contract and
    fee > 0 = taker order), cost = USDC paid incl. fee.
  * position = (market, outcome token).  The wallet never sells, merges or splits, so a position only grows
    until the market resolves; settlement value = shares x (1 if the token won else 0).
  * settled P&L of a market = settlement value of both tokens - all USDC paid for them (fees included).
    It is realised when the market resolves, whether or not the winner was redeemed; redemptions are only the
    conversion of winning shares into cash and are reconciled separately.
  * rebates and rewards are cash credited to the wallet; daily rebates are attributed to the UTC day before the
    payment (they are paid at 00:10 / 00:45 UTC for the previous day's volume).
"""
import gzip, json, sys, collections, time
from pathlib import Path

import numpy as np
import pandas as pd

RAW, OUT = Path(sys.argv[1]), Path(sys.argv[2])
CUTOFF = 1791766500          # end of the data-api download (2026-10-12 00:55:00 UTC); later chain fills are dropped
OUT.mkdir(parents=True, exist_ok=True)
WALLET = "c53375ff94e96100f2b30a4b5775db35218d69a9"
EXCHANGE = "e111180000d2663c0091e4f400237545b87b996b"


def jl(p):
    return [json.loads(l) for l in gzip.open(p, "rt")]


def words(x):
    d = x["data"][2:]
    return [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]


# ------------------------------------------------------------------ markets
M = json.load(open(RAW / "markets.json"))
mk = {}
for s, m in M.items():
    names, px = json.loads(m["outcomes"]), json.loads(m["outcomePrices"])
    toks = json.loads(m["clobTokenIds"])
    win = names[px.index("1")] if m["closed"] and "1" in px and set(px) <= {"0", "1"} else None
    mk[s] = {"slug": s, "asset": s.split("-")[0].upper(), "duration": s.split("-")[2], "open": int(s.rsplit("-", 1)[1]),
             "condition_id": m["conditionId"], "winner": win, "closed_time": m["closedTime"],
             "uma_status": m["umaResolutionStatus"], "token_up": toks[names.index("Up")], "token_down": toks[names.index("Down")]}
tok2 = {}
for s, m in mk.items():
    tok2[m["token_up"]] = (s, "Up")
    tok2[m["token_down"]] = (s, "Down")

# ------------------------------------------------------------------ activity
A = jl(RAW / "activity.jsonl.gz")
T = [a for a in A if a["type"] == "TRADE"]
print(f"activity {len(A)} records, {len(T)} trades", flush=True)

# ------------------------------------------------------------------ chain fills of the wallet's orders
C = []
for x in jl(RAW / "chain" / "fills_as_order_maker.jsonl.gz"):
    w = words(x)
    C.append({"tx": x["tx"], "li": x["li"], "block": x["block"], "side_code": w[0], "token": str(w[1]),
              "usdc_ex_fee": w[2] / 1e6, "shares": w[3] / 1e6, "fee": w[4] / 1e6,
              "role": "taker" if x["topics"][3][-40:] == EXCHANGE else "maker",
              "counterparty": None if x["topics"][3][-40:] == EXCHANGE else "0x" + x["topics"][3][-40:]})
by_tx = collections.defaultdict(list)
for c in C:
    by_tx[c["tx"]].append(c)

# block -> unix time: anchor on the matched API records (block time == API timestamp), interpolate the rest
rows, used = [], set()
unmatched_api = 0
for a in T:
    cands = [c for c in by_tx.get(a["transactionHash"], []) if (c["tx"], c["li"]) not in used
             and c["token"] == a["asset"] and abs(c["shares"] - a["size"]) < 1e-4]
    c = cands[0] if cands else None
    if c is None:
        unmatched_api += 1
    else:
        used.add((c["tx"], c["li"]))
    s, out = tok2.get(a["asset"], (a["slug"], a["outcome"]))
    rows.append({"ts": a["timestamp"], "slug": a["slug"], "outcome": a["outcome"], "token_id": a["asset"],
                 "condition_id": a["conditionId"], "side": a["side"], "price": a["price"], "shares": a["size"],
                 "usdc_api": a["usdcSize"], "tx": a["transactionHash"], "log_index": c["li"] if c else None,
                 "block": c["block"] if c else None, "fee_chain": c["fee"] if c else None,
                 "usdc_ex_fee_chain": c["usdc_ex_fee"] if c else None, "role_chain": c["role"] if c else None,
                 "counterparty": c["counterparty"] if c else None, "source": "api+chain" if c else "api"})
anchor = sorted({(r["block"], r["ts"]) for r in rows if r["block"]})
ab, at_ = np.array([b for b, _ in anchor], float), np.array([t for _, t in anchor], float)
extra = [c for c in C if (c["tx"], c["li"]) not in used]
late = 0
for c in extra:
    s, out = tok2.get(c["token"], (None, None))
    ts = int(round(np.interp(c["block"], ab, at_, right=np.nan) if c["block"] <= ab[-1] else 10 ** 10))
    if ts > CUTOFF or s is None:
        late += 1
        continue
    rows.append({"ts": ts, "slug": s, "outcome": out, "token_id": c["token"],
                 "condition_id": mk[s]["condition_id"] if s else None, "side": "BUY" if c["side_code"] == 0 else "SELL",
                 "price": round(c["usdc_ex_fee"] / c["shares"], 6) if c["shares"] else None, "shares": c["shares"],
                 "usdc_api": None, "tx": c["tx"], "log_index": c["li"], "block": c["block"], "fee_chain": c["fee"],
                 "usdc_ex_fee_chain": c["usdc_ex_fee"], "role_chain": c["role"], "counterparty": c["counterparty"],
                 "source": "chain-only (missing from data-api)"})
print(f"matched {len(used)} chain fills; api-only {unmatched_api}; chain-only {len(extra) - late} in period, "
      f"{late} after the cutoff (dropped)", flush=True)

tr = pd.DataFrame(rows)
tr["fee_api"] = tr["usdc_api"] - tr["price"] * tr["shares"]
tr["fee"] = tr["fee_chain"].fillna(tr["fee_api"])
tr["cost"] = (tr["usdc_ex_fee_chain"] + tr["fee_chain"]).fillna(tr["usdc_api"])
tr["role"] = tr["role_chain"].fillna(pd.Series(np.where(tr["fee_api"] > 1e-6, "taker", "maker"), index=tr.index))
tr["asset"] = tr["slug"].str.split("-").str[0].str.upper()
tr["market_open"] = tr["slug"].str.rsplit("-", n=1).str[1].astype(int)
tr["t_from_open"] = tr["ts"] - tr["market_open"]
tr["secs_to_close"] = tr["market_open"] + 300 - tr["ts"]
tr["winner"] = tr["slug"].map(lambda s: mk[s]["winner"])
tr["won"] = tr["outcome"] == tr["winner"]
tr["payout"] = np.where(tr["won"], tr["shares"], 0.0)
tr["pnl_at_settlement"] = tr["payout"] - tr["cost"]
tr["p_up_implied"] = np.where(tr["outcome"] == "Up", tr["price"], 1 - tr["price"])
tr["time_utc"] = pd.to_datetime(tr["ts"], unit="s", utc=True)
tr = tr.sort_values(["ts", "slug", "tx", "log_index"], na_position="last").reset_index(drop=True)
tr["fill_id"] = np.arange(len(tr))
# fee check: crypto_fees_v2 = 0.07 * p * (1 - p) per share, taker only
tr["fee_model"] = np.where(tr["role"] == "taker", 0.07 * tr["price"] * (1 - tr["price"]) * tr["shares"], 0.0)
cols = ["fill_id", "time_utc", "ts", "asset", "slug", "market_open", "t_from_open", "secs_to_close", "condition_id",
        "token_id", "outcome", "side", "role", "price", "shares", "cost", "fee", "fee_model", "usdc_api", "fee_api",
        "winner", "won", "payout", "pnl_at_settlement", "p_up_implied", "tx", "log_index", "block", "counterparty", "source"]
tr[cols].to_csv(OUT / "trades.csv.gz", index=False, compression="gzip")
print("trades", len(tr), "cost", round(tr["cost"].sum(), 2), "fees", round(tr["fee"].sum(), 2), flush=True)

# ------------------------------------------------------------------ redemptions
R = pd.DataFrame([{"ts": a["timestamp"], "slug": a["slug"], "condition_id": a["conditionId"], "shares": a["size"],
                   "usdc": a["usdcSize"], "outcome_field": a["outcome"], "tx": a["transactionHash"]}
                  for a in A if a["type"] == "REDEEM"])
R["time_utc"] = pd.to_datetime(R["ts"], unit="s", utc=True)
R.to_csv(OUT / "redemptions.csv", index=False)

# ------------------------------------------------------------------ positions (market x token)
g = tr.groupby(["slug", "outcome"])
pos = g.agg(token_id=("token_id", "first"), asset=("asset", "first"), market_open=("market_open", "first"),
            fills=("fill_id", "size"), taker_fills=("role", lambda r: (r == "taker").sum()),
            shares=("shares", "sum"), cost=("cost", "sum"), fees=("fee", "sum"),
            first_fill=("ts", "min"), last_fill=("ts", "max"), winner=("winner", "first")).reset_index()
pos["avg_price_ex_fee"] = (pos["cost"] - pos["fees"]) / pos["shares"]
pos["avg_cost_per_share"] = pos["cost"] / pos["shares"]
pos["won"] = pos["outcome"] == pos["winner"]
pos["settlement_value"] = np.where(pos["won"], pos["shares"], 0.0)
pos["pnl"] = pos["settlement_value"] - pos["cost"]
red = R.groupby("slug").agg(redeemed_usdc=("usdc", "sum"), redeem_ts=("ts", "min"), redeem_records=("tx", "size"))
pos = pos.merge(red, left_on="slug", right_index=True, how="left")
pos.loc[~pos["won"], ["redeemed_usdc"]] = 0.0           # a redemption pays the winning token only
pos["redeemed_usdc"] = pos["redeemed_usdc"].fillna(0.0)
pos["status"] = np.select([pos["won"] & (pos["redeem_records"] > 0), pos["won"], ~pos["won"]],
                          ["won, redeemed", "won, NOT redeemed", "lost (worth 0)"], "?")
pos["hold_to_resolution_s"] = pos["market_open"] + 300 - pos["first_fill"]
pos.to_csv(OUT / "positions.csv", index=False)

# ------------------------------------------------------------------ markets
rowsm = []
for s, xs in tr.groupby("slug", sort=False):
    q = xs.groupby("outcome")["shares"].sum()
    c = xs.groupby("outcome")["cost"].sum()
    qu, qd = float(q.get("Up", 0.0)), float(q.get("Down", 0.0))
    cu, cd = float(c.get("Up", 0.0)), float(c.get("Down", 0.0))
    w = mk[s]["winner"]
    payout = qu if w == "Up" else qd if w == "Down" else np.nan
    big = "Up" if qu > qd else "Down" if qd > qu else "tie"
    hedged = min(qu, qd)
    au, ad = (cu / qu if qu else np.nan), (cd / qd if qd else np.nan)
    sides = "".join("U" if o == "Up" else "D" for o in xs["outcome"])
    rowsm.append({
        "slug": s, "asset": mk[s]["asset"], "market_open": mk[s]["open"], "winner": w, "fills": len(xs),
        "taker_fills": int((xs["role"] == "taker").sum()), "shares_up": qu, "shares_down": qd, "cost_up": cu,
        "cost_down": cd, "cost": cu + cd, "fees": xs["fee"].sum(), "avg_cost_up": au, "avg_cost_down": ad,
        "pair_cost": au + ad if qu and qd else np.nan, "both_sides": bool(qu and qd), "bigger_side": big,
        "bigger_side_won": big == w, "hedged_shares": hedged, "unhedged_shares": abs(qu - qd),
        "lean_frac": abs(qu - qd) / max(qu, qd), "payout": payout, "pnl": payout - (cu + cd),
        "pnl_hedged_part": hedged - hedged * ((au if qu else 0) + (ad if qd else 0)) if qu and qd else 0.0,
        "first_fill_s": xs["t_from_open"].min(), "last_fill_s": xs["t_from_open"].max(),
        "side_switches": sum(a != b for a, b in zip(sides, sides[1:])), "first_side": "Up" if sides[0] == "U" else "Down",
        "sequence": sides if len(sides) <= 400 else sides[:400] + "...",
    })
mkt = pd.DataFrame(rowsm).sort_values("market_open").reset_index(drop=True)
mkt["pnl_lean_part"] = mkt["pnl"] - mkt["pnl_hedged_part"]
mkt["date"] = pd.to_datetime(mkt["market_open"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
mkt = mkt.merge(red.rename(columns={"redeemed_usdc": "redeemed_usdc"}), left_on="slug", right_index=True, how="left")
mkt["redeemed_usdc"] = mkt["redeemed_usdc"].fillna(0.0)
mkt.to_csv(OUT / "markets.csv", index=False)

# ------------------------------------------------------------------ other cash events
O = pd.DataFrame([{"ts": a["timestamp"], "type": a["type"], "usdc": a["usdcSize"], "tx": a["transactionHash"]}
                  for a in A if a["type"] in ("TAKER_REBATE", "MAKER_REBATE", "REWARD")])
O["time_utc"] = pd.to_datetime(O["ts"], unit="s", utc=True)
O["attributed_day"] = (O["time_utc"] - pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
O.loc[O["type"] == "REWARD", "attributed_day"] = O["time_utc"].dt.strftime("%Y-%m-%d")
O.to_csv(OUT / "rebates_rewards.csv", index=False)

# ------------------------------------------------------------------ daily / weekly
d = mkt.groupby("date").agg(markets=("slug", "size"), fills=("fills", "sum"), bought=("cost", "sum"),
                            fees=("fees", "sum"), market_pnl=("pnl", "sum"), btc_markets=("asset", lambda a: (a == "BTC").sum()))
for t in ("TAKER_REBATE", "MAKER_REBATE", "REWARD"):
    d[t.lower()] = O[O["type"] == t].groupby("attributed_day")["usdc"].sum()
d = d.fillna(0.0)
d["total_pnl"] = d["market_pnl"] + d["taker_rebate"] + d["maker_rebate"] + d["reward"]
d["cum_total_pnl"] = d["total_pnl"].cumsum()
d["market_pnl_pct_of_bought"] = d["market_pnl"] / d["bought"] * 100
d.index.name = "date_utc"
d.to_csv(OUT / "daily.csv")
dd = d.copy()
dd.index = pd.to_datetime(dd.index)
wk = dd.drop(columns=["cum_total_pnl", "market_pnl_pct_of_bought"]).resample("W-SUN").sum()
wk["market_pnl_pct_of_bought"] = wk["market_pnl"] / wk["bought"] * 100
wk.index = wk.index.strftime("week ending %Y-%m-%d")
wk.to_csv(OUT / "weekly.csv")
print(d.tail(3).to_string(), flush=True)
print("done", flush=True)
