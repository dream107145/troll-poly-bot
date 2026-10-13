"""Compact cache of data/charts/*-updown-5m-*.json for the paired-exit study (2026-10-01).

    nice -n 19 python scripts/paired_exit/cache.py      # -> data/replay/paired_exit/<asset>.npz

One npz per asset: every window's 200 ms samples concatenated (``off`` gives each window's slice) plus
per-window metadata. Only windows with a venue/proxy outcome and >= 600 samples are kept.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = "/root/troll-poly-bot"
OUT = f"{ROOT}/data/replay/paired_exit"
ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
EXCH = ("binance", "bybit", "coinbase", "deribit", "kraken", "bitstamp")
COLS = ("t", "left", "spot", "ua", "ub", "da", "db", "uad", "ubd", "dad", "dbd", "tfi15", "obi", "ofi", "rate")


def one(path: str):
    try:
        d = json.load(open(path))
    except Exception:
        return None
    out = (d.get("outcome") or "").upper()
    pts = [p for p in d.get("points") or [] if p.get("spot") and p.get("t")]
    if out not in ("UP", "DOWN") or len(pts) < 600:
        return None
    n = len(pts)
    a = np.full((n, len(COLS) + len(EXCH)), np.nan, dtype=np.float64)
    for i, p in enumerate(pts):
        of = p.get("of") or {}
        src = p.get("srcs") or {}
        row = (p.get("t"), p.get("left"), p.get("spot"), p.get("ua"), p.get("ub"), p.get("da"), p.get("db"),
               p.get("uad"), p.get("ubd"), p.get("dad"), p.get("dbd"), of.get("tfi15"), of.get("obi"), of.get("ofi"),
               of.get("rate")) + tuple(src.get(x) for x in EXCH)
        a[i] = [np.nan if v is None else v for v in row]
    fills = d.get("fills") or []
    meta = {"slug": d["slug"], "asset": (d.get("asset") or "").upper(), "strike": float(d.get("strike") or 0.0),
            "open": float(d.get("open_ts") or 0.0), "close": float(d.get("close_ts") or 0.0), "outcome": out,
            "source": d.get("outcome_source"), "n_fills": len(fills)}
    return meta, a


def build(asset: str) -> str:
    files = sorted(glob.glob(f"{ROOT}/data/charts/{asset}-updown-5m-*.json"))
    metas, arrs = [], []
    for f in files:
        r = one(f)
        if r is not None:
            metas.append(r[0]); arrs.append(r[1])
    off = np.cumsum([0] + [len(a) for a in arrs])
    np.savez_compressed(f"{OUT}/{asset}.npz", data=np.vstack(arrs).astype(np.float64), off=off,
                        cols=np.array(COLS + tuple(f"x_{e}" for e in EXCH)),
                        meta=np.array(json.dumps(metas)))
    return f"{asset}: {len(metas)} windows of {len(files)} files, {off[-1]} samples"


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    todo = sys.argv[1:] or ASSETS
    with ProcessPoolExecutor(max_workers=4) as ex:
        for line in ex.map(build, todo):
            print(line, flush=True)
