"""One JSON with the headline results, the cash-flow identity and the reconciliation with Polymarket's figures.

Usage: summary.py PROCESSED_DIR RAW_DIR  ->  PROCESSED_DIR/metrics_summary.json
"""
import json, sys
from pathlib import Path

import numpy as np
import pandas as pd

P, RAW = Path(sys.argv[1]), Path(sys.argv[2])
B = json.load(open(P / "metrics_behavior.json"))
mk = pd.read_csv(P / "markets.csv")
rr = pd.read_csv(P / "rebates_rewards.csv")
bal = pd.read_csv(P / "daily_balance_flows.csv")
ext = json.load(open(RAW / "chain" / "external_flows.json")) if (RAW / "chain" / "external_flows.json").exists() else []
S = {"wallet": "0xc53375ff94e96100f2b30a4b5775db35218d69a9", "name": "dangerously-skip-permissions",
     "profile": json.load(open(RAW / "profile.json")), "cutoff_utc": "2026-10-12 00:55:00",
     "coverage": B["coverage"], "totals": B["performance"]["totals"]}

# cash identity
flows = bal[bal["external_flow"].abs() > 1]
S["cash"] = {"deposits": round(flows.loc[flows["external_flow"] > 0, "external_flow"].sum(), 2),
             "withdrawals": round(flows.loc[flows["external_flow"] < 0, "external_flow"].sum(), 2),
             "trading_cash_to_cutoff": round(float(bal["trading_cash_cum"].iloc[-1]), 2),
             "pusd_balance_at_cutoff_block": round(float(bal["pusd_balance"].iloc[-1]), 2),
             "identity_residual_all_days_max_abs_excluding_flow_days": round(float(bal.loc[bal["external_flow"].abs() <= 1, "external_flow"].abs().max()), 6),
             "flows_by_day": [{"day_utc": str(pd.Timestamp(r["anchor_utc"]) - pd.Timedelta(days=1))[:10], "usdc": round(r["external_flow"], 2)}
                              for _, r in flows.iterrows()],
             "flow_transactions": ext,
             "balance_at_day_boundaries": {"min": round(bal["pusd_balance"].min(), 2), "median": round(bal["pusd_balance"].median(), 2),
                                           "mean": round(bal["pusd_balance"].mean(), 2), "max": round(bal["pusd_balance"].max(), 2)}}
S["cash"]["identity_check"] = round(S["cash"]["deposits"] + S["cash"]["withdrawals"] + S["cash"]["trading_cash_to_cutoff"]
                                    - S["cash"]["pusd_balance_at_cutoff_block"], 4)

# reconciliation with displayed figures
mk["close"] = mk["market_open"] + 300
s = mk.sort_values("close")
up = pd.DataFrame(json.load(open(RAW / "user_pnl_all_1d.json")))
cpre = (s["pnl"] + s["fees"]).cumsum().values
i = np.searchsorted(s["close"].values, up["t"].values, side="right") - 1
ours = np.where(i >= 0, cpre[np.clip(i, 0, None)], 0.0)
lb = {k: json.load(open(RAW / f"v1lb_{k}.json"))[0] for k in ("ALL", "MONTH", "WEEK", "DAY")}
end = 1791766500
win = {}
for name, days in (("30d", 30), ("7d", 7), ("1d", 1)):
    m = mk[mk["close"] > end - days * 86400]
    win[name] = {"pnl_before_fees": round((m["pnl"] + m["fees"]).sum(), 2), "pnl_after_fees": round(m["pnl"].sum(), 2),
                 "bought_plus_redeemed": round((m["cost"] + m["redeemed_usdc"]).sum(), 2)}
S["reconciliation"] = {
    "profile_chart_user_pnl_last": {"t": int(up["t"].iloc[-1]), "value": float(up["p"].iloc[-1]), "ours_before_fees_same_time": round(float(ours[-1]), 2),
                                    "corr_daily_points": round(float(np.corrcoef(up["p"], ours)[0, 1]), 6), "points": len(up)},
    "leaderboard": {k: {"pnl": float(v["pnl"]), "vol": float(v["vol"]), "rank": v["rank"]} for k, v in lb.items()},
    "ours_all": {"pnl_after_fees": S["totals"]["market_pnl_after_fees"], "pnl_before_fees": S["totals"]["market_pnl_before_fees"],
                 "bought_plus_redeemed": round(S["totals"]["bought_usdc_incl_fees"] + S["totals"]["redeemed_usdc"], 2),
                 "net_incl_rebates": S["totals"]["net_pnl"]},
    "ours_windows": win,
    "value_endpoint": json.load(open(RAW / "value.json"))[0]["value"], "traded_endpoint": json.load(open(RAW / "traded.json"))["traded"],
}
json.dump(S, open(P / "metrics_summary.json", "w"), indent=1, default=str)
print(json.dumps({k: S[k] for k in ("totals", "cash", "reconciliation")}, indent=1, default=str)[:6000])
