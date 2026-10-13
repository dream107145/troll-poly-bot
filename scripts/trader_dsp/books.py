"""Compact cache of this repo's recorded BTC 5m books (data/charts/btc-updown-5m-*.json) for the wallet study.

Usage: books.py OUT.pkl   ->  {slug: {"t": ms, "ua","ub","da","db","uad","ubd","dad","dbd","spot": float32 arrays,
                                      "open": s, "close": s}}
"""
import glob, json, pickle, sys
from multiprocessing import Pool

import numpy as np

CH = "/root/troll-poly-bot/data/charts"
K = ("ua", "ub", "da", "db", "uad", "ubd", "dad", "dbd", "spot")


def load(f):
    try:
        d = json.load(open(f))
    except (OSError, ValueError):
        return None
    P = [p for p in d.get("points") or [] if p.get("t")]
    if len(P) < 200:
        return None
    out = {"t": np.array([p["t"] for p in P], float), "open": d["open_ts"] / 1000.0, "close": d["close_ts"] / 1000.0,
           "strike": d.get("strike"), "outcome": d.get("outcome")}
    for k in K:
        out[k] = np.array([np.nan if p.get(k) is None else p[k] for p in P], np.float32)
    return d["slug"], out


if __name__ == "__main__":
    files = sorted(glob.glob(f"{CH}/btc-updown-5m-*.json"))
    with Pool(4) as pool:
        res = [r for r in pool.imap_unordered(load, files, chunksize=8) if r]
    books = dict(res)
    pickle.dump(books, open(sys.argv[1], "wb"))
    print(len(books), "markets cached")
