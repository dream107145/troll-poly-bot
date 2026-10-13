"""Gamma market metadata + resolution for every slug in the activity file (batches of 40 slugs).

closed=true is required to see resolved markets (plain markets?slug= hides them); slugs still missing are
fetched again without it (open / unresolved markets).
"""
import gzip, json, sys, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

ACT, OUT = sys.argv[1], sys.argv[2]
KEEP = ("slug", "conditionId", "question", "closed", "outcomes", "outcomePrices", "umaResolutionStatus", "closedTime",
        "endDate", "eventStartTime", "clobTokenIds", "feeType", "feeSchedule", "feesEnabled", "makerBaseFee",
        "takerBaseFee", "makerRebatesFeeShareBps", "volume", "negRisk", "orderMinSize", "orderPriceMinTickSize",
        "resolutionSource", "automaticallyResolved")


def get(slugs, closed):
    q = "&".join("slug=" + urllib.parse.quote(s) for s in slugs)
    u = f"https://gamma-api.polymarket.com/markets?limit=100&{q}" + ("&closed=true" if closed else "")
    for k in range(8):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "tpb-research"}), timeout=60) as r:
                return json.loads(r.read())
        except Exception:                              # noqa: BLE001
            time.sleep(2 * (k + 1))
    raise RuntimeError("gamma failed")


slugs = sorted({json.loads(l).get("slug") for l in gzip.open(ACT, "rt")} - {"", None})
print(len(slugs), "slugs", flush=True)
out = {}
for closed in (True, False):
    todo = [s for s in slugs if s not in out]
    batches = [todo[i:i + 40] for i in range(0, len(todo), 40)]
    with ThreadPoolExecutor(4) as ex:
        for res in ex.map(lambda b: get(b, closed), batches):
            for m in res:
                out[m["slug"]] = {k: m.get(k) for k in KEEP}
    print(f"closed={closed}: {len(out)} found", flush=True)
json.dump(out, open(OUT, "w"))
print("missing:", [s for s in slugs if s not in out][:20], flush=True)
