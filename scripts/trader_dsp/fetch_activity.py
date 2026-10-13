"""Every data-api /activity record of one wallet between two unix times.

15-minute windows; a window that returns the 500-record cap is split in half until it does not, and a single
second that still hits the cap is paged with `offset`.  Windows never overlap, so nothing is de-duplicated here:
identical records inside one response are kept (they can be separate fills) and audited later.
Writes gzipped JSON lines plus a coverage log of every request window.
"""
import gzip, json, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

W, OUT, START, END = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
URL = ("https://data-api.polymarket.com/activity?user={w}&limit=500&offset={o}&start={s}&end={e}"
       "&sortBy=TIMESTAMP&sortDirection=ASC")
DROP = ("name", "pseudonym", "bio", "profileImage", "profileImageOptimized", "icon", "proxyWallet")


def get(s, e, o=0):
    for k in range(8):
        try:
            req = urllib.request.Request(URL.format(w=W, s=s, e=e, o=o), headers={"User-Agent": "tpb-research"})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
            if isinstance(d, list):
                return d
        except Exception:                      # noqa: BLE001
            pass
        time.sleep(2 * (k + 1))
    raise RuntimeError(f"failed {s}-{e} offset {o}")


def window(s, e, log):
    d = get(s, e)
    if len(d) >= 500 and e > s:
        m = (s + e) // 2
        return window(s, m, log) + window(m + 1, e, log)
    if len(d) >= 500:                          # one second with >= 500 records: page it
        o = 500
        while True:
            more = get(s, e, o)
            d += more
            if len(more) < 500:
                break
            o += 500
        log.append({"start": s, "end": e, "n": len(d), "paged": True})
        return d
    log.append({"start": s, "end": e, "n": len(d)})
    return d


def job(w):
    log = []
    return window(*w, log), log


wins = [(t, min(t + 899, END)) for t in range(START, END + 1, 900)]
n, t0 = 0, time.time()
cov = []
with gzip.open(OUT, "wt") as fh, ThreadPoolExecutor(8) as ex:
    for i, (recs, log) in enumerate(ex.map(job, wins)):
        cov += log
        for x in recs:
            fh.write(json.dumps({k: v for k, v in x.items() if k not in DROP}) + "\n")
            n += 1
        if i % 500 == 0:
            print(f"{i}/{len(wins)} windows, {n} records, {time.time() - t0:.0f} s", flush=True)
json.dump({"wallet": W, "start": START, "end": END, "fetched_at": int(time.time()), "records": n,
           "windows": cov}, open(OUT.replace(".jsonl.gz", "_coverage.json"), "w"))
print(f"done: {n} records in {time.time() - t0:.0f} s", flush=True)
