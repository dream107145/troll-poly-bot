"""Today's triangle paper run (resting pair < 0.95 strict): every first leg, whether its pair filled, its result."""
import json, sys
from collections import defaultdict
path = sys.argv[1] if len(sys.argv) > 1 else "data/live_trades.triangle-btc-paper.jsonl"
rows = [json.loads(l) for l in open(path)]
outcome = {}
for r in rows:
    if r.get("event") == "settle" and r.get("venue_up") is not None:
        outcome[r["slug"]] = "UP" if r["venue_up"] else "DOWN"
legs = {}
pairs = defaultdict(list)
for r in rows:
    if r.get("event") != "fill" or r.get("action") != "BUY":
        continue
    if r.get("pair"):
        pairs[r.get("pair_of")].append(r)
    else:
        legs[r.get("order_id") or r.get("signal_id") or f"{r['slug']}-{r['ts']}"] = r
# pair rows name their parent by order id; entry rows may not carry it: match by slug + time order
by_slug = defaultdict(list)
for k, r in legs.items():
    by_slug[r["slug"]].append((r["ts"], k, r))
pf = {pr["pair_of"]: pr for prs in pairs.values() for pr in prs}
out = []
for slug, lst in by_slug.items():
    won = outcome.get(slug)
    if won is None:
        continue
    lst.sort()
    pair_rows = sorted([pr for prs in pairs.values() for pr in prs if pr["slug"] == slug], key=lambda r: r["ts"])
    for ts, k, r in lst:
        fee = r.get("fee") or 0.0
        leg = (5.0 if r["side"] == won else 0.0) - r["price"] * r["size"] - fee
        p = next((pr for pr in pair_rows if pr["ts"] > ts and pr["side"] != r["side"] and not pr.get("_used")), None)
        if p is not None:
            p["_used"] = True
            pnl = leg + (5.0 if p["side"] == won else 0.0) - p["price"] * p["size"]
            out.append(("paired", pnl, r["price"], p["price"]))
        else:
            out.append(("unpaired", leg, r["price"], None))
pa = [x[1] for x in out if x[0] == "paired"]; un = [x[1] for x in out if x[0] == "unpaired"]
print(f"windows settled {len(set(r['slug'] for r in rows if r.get('event') == 'settle'))}; first legs {len(out)}")
print(f"paired {len(pa)}: total {sum(pa):+.2f}, avg {sum(pa)/max(len(pa),1):+.3f}")
print(f"unpaired {len(un)}: total {sum(un):+.2f}, avg {sum(un)/max(len(un),1):+.3f}, losers {sum(1 for x in un if x < 0)}")
print(f"net {sum(pa)+sum(un):+.2f}")
