"""Which paper fill rule reproduces REAL Polymarket fills? Real spot-lag orders (09-29 17:58 onwards: the
current order path), each replayed on its window's 200 ms archive (data/charts): arrival = decision + RTT,
filled if the side's ask at arrival is <= the limit AND stays <= the limit for S ms (the survival window)."""
import json, os, bisect, collections, datetime
os.chdir("/root/troll-poly-bot")
rows = [json.loads(l) for l in open("data/trade_forensics.jsonl")]
summ = {}
for r in rows:
    if r.get("order_id") and r.get("kind") == "entry" and r.get("outcome") and r.get("mode") == "LIVE-ARMED":
        summ[r["order_id"] + str(r.get("t4_decision"))] = r
real = [r for r in summ.values() if not r["outcome"].endswith(("live_caps", "stale_decision"))]
t0 = datetime.datetime(2026, 9, 29, 17, 58, tzinfo=datetime.timezone.utc).timestamp() * 1000
t1 = datetime.datetime(2026, 9, 30, 19, 12, tzinfo=datetime.timezone.utc).timestamp() * 1000
cache = {}
def pts(slug):
    if slug not in cache:
        p = f"data/charts/{slug}.json"
        cache[slug] = json.load(open(p))["points"] if os.path.exists(p) else None
    return cache[slug]
def series(P, key):
    return [p["t"] for p in P], [p.get(key) for p in P]
RTT = 250.0
res = collections.defaultdict(lambda: collections.Counter())
n_era = collections.Counter()
for r in real:
    td = r.get("t4_decision") or 0
    if td < t0 or r.get("limit_price") is None:
        continue
    P = pts(r["slug"])
    if not P:
        continue
    era = "slow" if td < t1 else "fast"
    key = "ua" if r["side"] == "UP" else "da"
    ts, asks = series(P, key)
    lim = r["limit_price"]
    real_fill = r["outcome"] == "EXIT"
    n_era[era] += 1
    for S in (0, 200, 400, 1000, 2000):
        arr = td + RTT
        i = bisect.bisect_right(ts, arr) - 1
        j = bisect.bisect_right(ts, arr + S)
        window = [a for a in asks[max(i, 0):max(j, i + 1)] if a is not None]
        pred = bool(window) and max(window) <= lim + 1e-9
        res[(era, S)][(real_fill, pred)] += 1
for era in ("slow", "fast"):
    print(f"{era} feeds: {n_era[era]} real orders with an archive")
    for S in (0, 200, 400, 1000, 2000):
        c = res[(era, S)]
        n = sum(c.values()); real_f = c[(True, True)] + c[(True, False)]; pred_f = c[(True, True)] + c[(False, True)]
        agree = c[(True, True)] + c[(False, False)]
        print(f"  survival {S:5d} ms: real fill {real_f/n:4.0%}  predicted {pred_f/n:4.0%}  agree {agree/n:4.0%}  "
              f"(real fill & pred fill {c[(True, True)]}, real fill & pred kill {c[(True, False)]}, real kill & pred fill {c[(False, True)]})")
