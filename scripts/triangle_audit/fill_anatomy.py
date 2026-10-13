"""Triangle paper bot: why orders fill or die. Every entry order since the fresh run, matched to the
token's 200 ms ask path in data/charts and to the window's outcome."""
import json, os, bisect, statistics as st, collections, sys
os.chdir("/root/troll-poly-bot")
rows = [json.loads(l) for l in open("data/trade_forensics.triangle-btc-paper.jsonl")]
orders = {}
for r in rows:
    if r.get("order_id") and r.get("kind") == "entry" and r.get("outcome"):
        orders[r["order_id"]] = r          # last summary row wins
charts = {}
def chart(slug):
    if slug not in charts:
        p = f"data/charts/{slug}.json"
        charts[slug] = json.load(open(p)) if os.path.exists(p) else None
    return charts[slug]
def ask_at(pts, ts, key):
    i = bisect.bisect_right([p["t"] for p in pts], ts) - 1
    return pts[i].get(key) if i >= 0 else None
def ask_series(pts, t0, t1, key):
    return [p.get(key) for p in pts if t0 <= p["t"] <= t1 and p.get(key) is not None]
out = []
for oid, r in orders.items():
    c = chart(r["slug"])
    if not c or not c.get("outcome"):
        continue
    side = r["side"]; key = "ua" if side == "UP" else "da"
    pts = c["points"]; tdec = r.get("t4_decision") or r.get("t2_signal")
    won = (c["outcome"].upper() == side)
    lim = r.get("limit_price"); a0 = r.get("best_ask") or r.get("decision_price")
    fee = lambda p: 0.07 * p * (1 - p)
    o = r["outcome"]
    filled = o.startswith("FILLED") or (r.get("execution_price") is not None)
    px = r.get("execution_price") if filled else lim
    rec = dict(oid=oid, slug=r["slug"], side=side, won=won, outcome=o, filled=filled, lim=lim, a0=a0,
               px=px, left=r.get("seconds_left"), label=r.get("momentum_label"), z=r.get("momentum_z"),
               rtt=r.get("venue_rtt_ms"), lat=r.get("latency_ms"))
    for dt in (-3000, -1000, 250, 500, 1000, 2000, 5000, 10000):
        rec[f"a{dt}"] = ask_at(pts, tdec + dt, key)
    rec["max2"] = max(ask_series(pts, tdec, tdec + 2300, key) or [None]) if ask_series(pts, tdec, tdec + 2300, key) else None
    rec["pnl_at_px"] = ((1.0 if won else 0.0) - px - fee(px)) * 5 if px else None
    out.append(rec)
print(f"orders with a settled window: {len(out)} of {len(orders)}")
by = collections.Counter(r["outcome"].split(":")[-1] if not r["filled"] else "FILLED" for r in out)
print("outcomes:", dict(by))
def summ(name, rs):
    if not rs: return
    f = [r for r in rs if r["filled"]]; k = [r for r in rs if not r["filled"]]
    pf = sum(r["pnl_at_px"] for r in f); pk = sum(r["pnl_at_px"] for r in k)
    print(f"{name:34s} n={len(rs):4d} fill={len(f)/len(rs):5.0%}  filled pnl {pf:+7.2f} ({pf/max(1,len(f)):+.2f}/fill, win {sum(r['won'] for r in f)/max(1,len(f)):.0%})"
          f"  killed-if-filled-at-limit {pk:+7.2f} ({pk/max(1,len(k)):+.2f}/order, win {sum(r['won'] for r in k)/max(1,len(k)):.0%})")
summ("ALL", out)
for lab in sorted({r["label"] for r in out}, key=str):
    summ(f"trigger {lab}", [r for r in out if r["label"] == lab])
for lo, hi in ((0, .45), (.45, .6), (.6, .75), (.75, .85), (.85, 1.0)):
    summ(f"ask {lo:.2f}-{hi:.2f}", [r for r in out if r["a0"] is not None and lo <= r["a0"] < hi])
for lo, hi in ((0, 60), (60, 120), (120, 180), (180, 240), (240, 301)):
    summ(f"left {lo}-{hi}s", [r for r in out if r["left"] is not None and lo <= r["left"] < hi])
# token speed before the decision
def vel(r, k):
    a = r.get(k); return None if a is None or r["a0"] is None else round(r["a0"] - a, 3)
for lo, hi, nm in ((-9, -0.005, "fell"), (-0.005, 0.005, "flat"), (0.005, 0.025, "+1-2c"), (0.025, 9, ">=+3c")):
    summ(f"ask change last 1 s: {nm}", [r for r in out if vel(r, "a-1000") is not None and lo <= vel(r, "a-1000") < hi])
for lo, hi, nm in ((-9, -0.005, "fell"), (-0.005, 0.005, "flat"), (0.005, 0.025, "+1-2c"), (0.025, 9, ">=+3c")):
    summ(f"ask change last 3 s: {nm}", [r for r in out if vel(r, "a-3000") is not None and lo <= vel(r, "a-3000") < hi])
print()
print("kill anatomy (ask relative to the limit, from the 200 ms archive):")
for reason in ("fok_unfillable", "quote_pulled"):
    k = [r for r in out if r["outcome"].endswith(reason)]
    if not k: continue
    over250 = sum(1 for r in k if r["a250"] is not None and r["a250"] > r["lim"] + 1e-9)
    over2 = sum(1 for r in k if r["max2"] is not None and r["max2"] > r["lim"] + 1e-9)
    up5 = [r["a5000"] - r["a0"] for r in k if r["a5000"] is not None and r["a0"] is not None]
    print(f"  {reason:15s} n={len(k):3d}  ask>limit at +250ms {over250}  ask>limit within 2 s {over2}  "
          f"ask move +5 s median {st.median(up5)*100 if up5 else float('nan'):+.1f}c  win {sum(r['won'] for r in k)/len(k):.0%}")
f = [r for r in out if r["filled"]]
up5 = [r["a5000"] - r["a0"] for r in f if r["a5000"] is not None and r["a0"] is not None]
print(f"  {'FILLED':15s} n={len(f):3d}  ask move +5 s median {st.median(up5)*100 if up5 else float('nan'):+.1f}c")
rtt = [r["rtt"] for r in out if r["rtt"]]
print("paper rtt p50 %.0f ms" % st.median(rtt) if rtt else "")
json.dump(out, open("results/triangle_fill_orders.json", "w"))
