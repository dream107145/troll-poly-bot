"""The user's simple strategy (2026-10-05): in the first 10 seconds after a 5-minute market opens, buy the HIGHER-priced
token if its price is under 0.60; hold it to the close.

Data: the dashboard's 200 ms window archive (data/replay/open_buy/btc.npz up to now; the other assets from
data/replay/paired_exit/<asset>.npz, 09-22 .. 10-01). Higher token = the higher ask (equal asks: the higher bid).

Fills, two ways:
  * realistic -- the paper bots' "calibrated" venue (fitted on 193 real orders): the order decided at t lands at
    t + 250 ms, fills only if the ask stays at or under the limit until landing + 1 s, and pays the worse of the two
    asks; limit = ask + 0.02 but never 0.60 or more. A killed order is retried while the 10 s last.
  * ideal -- filled at the ask the bot saw, always (an upper bound).
Fee: Polymarket's taker fee 0.07 x p x (1 - p) per share. 10 shares a trade.

    .venv/bin/python scripts/open_buy_sim.py > results/open_buy_sim_2026-10-05.md
"""
from __future__ import annotations

import math
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L   # noqa: E402

SHARES = 10.0
WINDOW_MS = 10_000.0
CAP = 0.60                 # "under 0.6"
LAT_MS, SURVIVE_MS = 250.0, 1000.0


def fee(p):
    return 0.07 * p * (1.0 - p)


def higher(w, i):
    ua, da = w.ua[i], w.da[i]
    if math.isnan(ua) or math.isnan(da):
        return None
    if abs(ua - da) < 1e-9:
        ub, db = w.ub[i], w.db[i]
        if math.isnan(ub) or math.isnan(db) or abs(ub - db) < 1e-9:
            return None
        return "UP" if ub > db else "DOWN"
    return "UP" if ua > da else "DOWN"


def taker_buy(w, side, t, limit):
    """The calibrated paper venue: (price, known time) or (None, known time)."""
    arr = w.ua if side == "UP" else w.da
    land = t + LAT_MS
    i0 = max(int(np.searchsorted(w.T, land, side="right")) - 1, 0)
    i1 = int(np.searchsorted(w.T, land + SURVIVE_MS, side="right"))
    seg = arr[i0:max(i1, i0 + 1)]
    if seg.size == 0 or np.isnan(seg).any() or seg.max() > limit + 1e-9:
        return None, land + SURVIVE_MS
    return min(max(float(arr[i0]), float(arr[min(max(i1 - 1, i0), w.n - 1)])), limit), land + SURVIVE_MS


def simulate(w, realistic=True):
    """One market: a trade dict or None (no token under the cap / never filled)."""
    i = int(np.searchsorted(w.T, w.open))
    next_t = w.open
    tried = False
    while i < w.n and w.T[i] <= w.open + WINDOW_MS:
        if w.T[i] < next_t:
            i += 1
            continue
        side = higher(w, i)
        ask = None if side is None else float((w.ua if side == "UP" else w.da)[i])
        if side is None or not (ask < CAP - 1e-9):
            i += 1
            continue
        tried = True
        if not realistic:
            return {"side": side, "price": ask, "t": (w.T[i] - w.open) / 1000.0}
        limit = min(round(ask + 0.02, 2), CAP - 0.01)
        px, known = taker_buy(w, side, w.T[i], limit)
        if px is not None:
            return {"side": side, "price": px, "t": (w.T[i] - w.open) / 1000.0, "ask": ask}
        next_t = known
        i += 1
    return {"killed": True} if tried else None


def stats(trades, n_markets):
    tr = [t for t in trades if "price" in t]
    if not tr:
        return None
    pnl = np.array([SHARES * ((1.0 if t["won"] else 0.0) - t["price"] - fee(t["price"])) for t in tr])
    eq = np.cumsum(pnl)
    dd = float(np.min(eq - np.maximum.accumulate(eq))) if len(eq) else 0.0
    streak = best = 0
    for p in pnl:
        streak = streak + 1 if p < 0 else 0
        best = max(best, streak)
    price = np.array([t["price"] for t in tr])
    se = pnl.std() / math.sqrt(len(pnl)) if len(pnl) > 1 else float("nan")
    return {"markets": n_markets, "trades": len(tr), "killed": sum(1 for t in trades if t.get("killed")),
            "avg_price": float(price.mean()), "win": float(np.mean([t["won"] for t in tr])),
            "breakeven": float(np.mean(price + np.array([fee(p) for p in price]))),
            "per_trade": float(pnl.mean()), "total": float(pnl.sum()), "t": float(pnl.mean() / se) if se > 0 else float("nan"),
            "dd": dd, "streak": best, "worst_day": None}


def run_asset(ws, realistic=True):
    out = []
    for w in ws:
        r = simulate(w, realistic)
        if r is None:
            continue
        if "price" in r:
            r["won"] = w.outcome == r["side"]
        r["day"] = time.strftime("%m-%d", time.gmtime(w.open / 1000))
        r["open"] = w.open
        out.append(r)
    return out


def load(asset):
    if asset == "BTC":
        keep = L.CACHE
        L.CACHE = "/root/troll-poly-bot/data/replay/open_buy"
        try:
            return L.load_asset("BTC")
        finally:
            L.CACHE = keep
    return L.load_asset(asset)


ROW = ("| {name} | {markets} | {trades} ({share:.0%}) | {avg_price:.3f} | {win:.1%} | {breakeven:.1%} | "
       "{per_trade:+.3f} | {total:+.2f} | {t:+.2f} | {dd:+.2f} | {streak} |")
HEAD = ("| market | markets | trades (share of markets) | avg price paid | won | needed to break even | "
        "$ per trade (10 sh) | total $ | t | max drawdown $ | longest losing run |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")


def fmt(name, s):
    return ROW.format(name=name, share=s["trades"] / max(s["markets"], 1), **s)


def main():
    data = {a: load(a) for a in L.ASSETS}
    btc = data["BTC"]
    first, last = btc[0].open, btc[-1].open
    f = lambda ms: time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000))   # noqa: E731
    print("# Buy the higher token in the first 10 s if it is under 0.60, hold to the close\n")
    print(f"10 shares a trade, taker fee 0.07 p(1-p). BTC: {len(btc)} markets {f(first)} - {f(last)} UTC; the other "
          "markets 09-22 - 10-01 (nothing has recorded them since).\n")
    res = {}
    for realistic in (True, False):
        print(f"## {'Realistic fills (the paper bots venue)' if realistic else 'Ideal fills (at the ask seen, always) -- an upper bound'}\n")
        print(HEAD)
        for a, ws in data.items():
            tr = run_asset(ws, realistic)
            s = stats(tr, len(ws))
            if s:
                print(fmt(a, s))
            res[(a, realistic)] = tr
        print()
    tr = [t for t in res[("BTC", True)] if "price" in t]
    print("## BTC in detail (realistic fills)\n")
    print("By the price paid:\n")
    print("| price paid | trades | won | needed | $ per trade |\n|---|---:|---:|---:|---:|")
    for lo, hi in ((0.0, 0.52), (0.52, 0.54), (0.54, 0.56), (0.56, 0.58), (0.58, 0.60)):
        b = [t for t in tr if lo <= t["price"] < hi]
        if b:
            pn = [SHARES * ((1 if t["won"] else 0) - t["price"] - fee(t["price"])) for t in b]
            print(f"| {lo:.2f}-{hi:.2f} | {len(b)} | {np.mean([t['won'] for t in b]):.1%} | "
                  f"{np.mean([t['price'] + fee(t['price']) for t in b]):.1%} | {np.mean(pn):+.3f} |")
    print("\nBy day:\n")
    print("| day (UTC) | trades | won | $ total | $ per trade |\n|---|---:|---:|---:|---:|")
    byd = defaultdict(list)
    for t in tr:
        byd[t["day"]].append(t)
    for d in sorted(byd):
        b = byd[d]
        pn = [SHARES * ((1 if t["won"] else 0) - t["price"] - fee(t["price"])) for t in b]
        print(f"| {d} | {len(b)} | {np.mean([t['won'] for t in b]):.0%} | {sum(pn):+.2f} | {np.mean(pn):+.3f} |")
    pos = sum(1 for d in byd if sum(SHARES * ((1 if t['won'] else 0) - t['price'] - fee(t['price'])) for t in byd[d]) > 0)
    print(f"\nDays positive: {pos} of {len(byd)}.")


if __name__ == "__main__":
    main()
