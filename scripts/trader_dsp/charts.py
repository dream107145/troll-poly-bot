"""Static charts for the trader report.  Usage: charts.py PROCESSED_DIR BOOKS.pkl LAG_S OUT_DIR"""
import json, pickle, sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

P, BK, LAG, OUT = Path(sys.argv[1]), pickle.load(open(sys.argv[2], "rb")), float(sys.argv[3]), Path(sys.argv[4])
OUT.mkdir(parents=True, exist_ok=True)
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"           # categorical slots 1-3 (validated all-pairs)
INK, INK2, MUTED, GRID, SURF = "#0b0b0b", "#52514e", "#8a8984", "#e4e3df", "#fcfcfb"
plt.rcParams.update({"figure.facecolor": SURF, "axes.facecolor": SURF, "axes.edgecolor": MUTED, "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "font.size": 10,
                     "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID,
                     "grid.linewidth": 0.8, "axes.axisbelow": True, "legend.frameon": False, "lines.linewidth": 2})


def save(fig, name):
    fig.tight_layout()
    fig.savefig(OUT / name, dpi=150)
    plt.close(fig)
    print("wrote", name)


daily = pd.read_csv(P / "daily.csv", parse_dates=["date_utc"])
mk = pd.read_csv(P / "markets.csv")
rr = pd.read_csv(P / "rebates_rewards.csv")

# 1. cumulative P&L on event time
ev_m = pd.DataFrame({"ts": mk["market_open"] + 300, "m": mk["pnl"], "r": 0.0})
ev_r = pd.DataFrame({"ts": rr["ts"], "m": 0.0, "r": rr["usdc"]})
ev = pd.concat([ev_m, ev_r]).sort_values("ts")
ev["t"] = pd.to_datetime(ev["ts"], unit="s")
ev["cm"], ev["cr"] = ev["m"].cumsum(), ev["r"].cumsum()
ev["cn"] = ev["cm"] + ev["cr"]
fig, ax = plt.subplots(figsize=(9, 4.2))
for col, c, lab, dy in (("cn", INK, "net", 0), ("cm", S1, "markets, after fees", 7), ("cr", S2, "rebates + rewards", -7)):
    ax.plot(ev["t"], ev[col] / 1000, color=c, lw=2)
    ax.annotate(f"{lab}  {ev[col].iloc[-1] / 1000:+.1f}k", (ev["t"].iloc[-1], ev[col].iloc[-1] / 1000), xytext=(6, dy),
                textcoords="offset points", va="center", color=INK, fontsize=9)
ax.set_ylabel("cumulative P&L, thousand USDC")
ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
ax.set_xlim(ev["t"].iloc[0], ev["t"].iloc[-1] + pd.Timedelta(days=14))
ax.set_title("Cumulative P&L, 2026-07-17 to 2026-10-12 (UTC)", loc="left", fontsize=11)
save(fig, "cum_pnl.png")

# 2. daily: bought and P&L (two panels, one axis each)
d = daily.iloc[:-1]
fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True, gridspec_kw={"height_ratios": [1, 1.3]})
a1.bar(d["date_utc"], d["bought"] / 1000, color=S1, width=0.8)
a1.set_ylabel("bought, k USDC")
a1.set_title("Daily volume bought", loc="left", fontsize=11)
a2.bar(d["date_utc"], d["total_pnl"] / 1000, color=S1, width=0.8, label="net (markets + rebates + rewards)")
a2.plot(d["date_utc"], d["market_pnl"] / 1000, "o", color=S2, ms=4, label="markets only, after fees")
a2.axhline(0, color=MUTED, lw=1)
a2.set_ylabel("P&L, k USDC")
a2.set_title("Daily P&L", loc="left", fontsize=11)
a2.legend(loc="upper left", fontsize=9)
a2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
save(fig, "daily.png")

# 3. timing in the window, by role (share of each role's USDC per 10 s)
tr = pd.read_csv(P / "trades.csv.gz", usecols=["role", "t_from_open", "price", "cost", "shares", "outcome", "slug", "ts", "fill_id"])
fig, ax = plt.subplots(figsize=(9, 3.6))
bins = np.arange(0, 311, 10)
for r, c in (("taker", S1), ("maker", S2)):
    x = tr[tr["role"] == r]
    h, _ = np.histogram(x["t_from_open"], bins, weights=x["cost"])
    ax.step(bins[:-1], h / h.sum() * 100, where="post", color=c, label=f"{r} fills")
ax.set_xlabel("seconds after the market opened (block time)")
ax.set_ylabel("% of the role's USDC")
ax.legend(loc="upper right")
ax.set_xlim(0, 310)
ax.set_title("When in the 5-minute window it buys", loc="left", fontsize=11)
save(fig, "timing.png")

# 4. price paid, by role (USDC-weighted, 1c bins)
fig, ax = plt.subplots(figsize=(9, 3.6))
bins = np.arange(0, 1.0001, 0.01)
for r, c in (("taker", S1), ("maker", S2)):
    x = tr[tr["role"] == r]
    h, _ = np.histogram(x["price"], bins, weights=x["cost"])
    ax.step(bins[:-1], h / h.sum() * 100, where="post", color=c, label=f"{r} fills")
ax.set_xlabel("price paid per share")
ax.set_ylabel("% of the role's USDC")
ax.legend(loc="upper right")
ax.set_title("Price paid (all 527,757 fills)", loc="left", fontsize=11)
save(fig, "prices.png")

# 5. event study around fills
f = pd.read_csv(P / "micro" / "fills_with_book.csv.gz")
H = [-30, -10, -5, -3, -2, -1, 0, 1, 2, 3, 5, 10, 30, 60]
mm = json.load(open(P / "metrics_micro.json"))
fig, ax = plt.subplots(figsize=(9, 3.8))
for r, c, data in (("taker orders", S1, f[f["role"] == "taker"].drop_duplicates("tx")), ("maker fills", S2, f[f["role"] == "maker"])):
    ys = [((data[f"mid_{h:+d}"] - data["mid0"]).mean() * 100) if h else 0.0 for h in H]
    ax.plot(H, ys, "-o", color=c, ms=4, label=f"{r} (n={len(data):,})")
rb = mm["event_study"]["random_moments_same_markets_price_0.1_0.9"]
rx = [-10, -3, -1, 0, 1, 3, 10, 60]
ry = [-rb["dmid_-10_c"]["mean"], -rb["dmid_-3_c"]["mean"], -rb["dmid_-1_c"]["mean"], 0, rb["dmid_+1_c"]["mean"],
      rb["dmid_+3_c"]["mean"], rb["dmid_+10_c"]["mean"], rb["dmid_+60_c"]["mean"]]
ax.plot(rx, ry, "-o", color=S3, ms=4, label="random moments, same markets")
ax.axvline(0, color=MUTED, lw=1, ls="--")
ax.set_xscale("symlog", linthresh=3)
ax.set_xticks(H)
ax.set_xticklabels([str(h) for h in H])
ax.set_xlabel(f"seconds from the estimated match time (block time - {LAG:g} s)")
ax.set_ylabel("bought token's mid vs at the fill, cents")
ax.legend(loc="upper left", fontsize=9)
ax.set_title("Token price around the wallet's BTC fills (recorded books, 09-22 to 10-11)", loc="left", fontsize=11)
save(fig, "event_study.png")

# 6. entry lift
hz = mm["entry_hazard"]
fig, axs = plt.subplots(1, 3, figsize=(11, 3.6), sharey=True)
for ax, key, title in ((axs[0], "by_r1_token_mid_change_1s", "token mid change, last 1 s"),
                       (axs[1], "by_spot_1s_toward_token_bps", "BTC spot, last 1 s, toward token (bps)"),
                       (axs[2], "by_mid", "token mid")):
    k = list(hz[key].keys())
    v = [hz[key][x]["lift"] for x in k]
    ax.bar(range(len(k)), v, color=S1, width=0.8)
    ax.axhline(1, color=MUTED, lw=1, ls="--")
    iv = [x.strip("(]").split(", ") for x in k]
    lab = [f"<{b}" if i == 0 and key != "by_mid" else f">{a}" if i == len(iv) - 1 and key != "by_mid" else f"{a}..{b}"
           for i, (a, b) in enumerate(iv)]
    ax.set_xticks(range(len(k)))
    ax.set_xticklabels(lab, rotation=60, fontsize=7.5)
    ax.set_title(title, fontsize=10)
axs[0].set_ylabel("taker-order rate / average rate")
fig.suptitle("Chance the wallet takes a token in the next second, by book state (1-s grid, 1.69M token-seconds)", x=0.01, ha="left", fontsize=11)
save(fig, "entry_lift.png")

# 7. example market
seq = mm["representative_markets"]["typical"]["slug"]
b = BK[seq]
t = (b["t"] / 1000 - b["open"])
fig, ax = plt.subplots(figsize=(9, 4))
ax.plot(t, (b["ua"] + b["ub"]) / 2, color=S1, lw=1.5, label="Up mid")
ax.plot(t, (b["da"] + b["db"]) / 2, color=S2, lw=1.5, label="Down mid")
x = tr[tr["slug"] == seq].copy()
x["tm"] = x["ts"] + 0.5 - LAG - b["open"]
for side, c in (("Up", S1), ("Down", S2)):
    for role, mkr in (("taker", "o"), ("maker", "o")):
        y = x[(x["outcome"] == side) & (x["role"] == role)]
        ax.scatter(y["tm"], y["price"], s=np.clip(y["shares"], 8, 120), marker=mkr,
                   facecolors=c if role == "taker" else SURF, edgecolors=c, linewidths=1.5, zorder=3)
ax.scatter([], [], s=40, facecolors=INK2, edgecolors=INK2, label="taker buy (size = shares)")
ax.scatter([], [], s=40, facecolors=SURF, edgecolors=INK2, linewidths=1.5, label="maker buy")
ax.set_xlim(0, 300)
ax.set_ylim(0, 1)
ax.set_xlabel("seconds after the open")
ax.set_ylabel("price")
w = mk[mk["slug"] == seq].iloc[0]
ax.set_title(f"{seq}: {int(w['fills'])} fills, {w['cost']:.0f} USDC, winner {w['winner']}, P&L {w['pnl']:+.2f}", loc="left", fontsize=11)
ax.legend(loc="center left", fontsize=8.5, bbox_to_anchor=(1.0, 0.5))
save(fig, "example_market.png")

# 8. per-market P&L distribution
fig, ax = plt.subplots(figsize=(9, 3.4))
v = mk["pnl"].clip(-400, 400)
ax.hist(v, bins=np.arange(-400, 401, 10), color=S1, rwidth=0.85)
ax.axvline(0, color=MUTED, lw=1)
ax.set_xlabel("P&L per market after fees, USDC (clipped at +/-400)")
ax.set_ylabel("markets")
ax.set_title(f"Per-market P&L: {len(mk):,} markets, {100 * (mk['pnl'] > 0).mean():.1f} % positive, median {mk['pnl'].median():+.2f}", loc="left", fontsize=11)
save(fig, "market_pnl_hist.png")
