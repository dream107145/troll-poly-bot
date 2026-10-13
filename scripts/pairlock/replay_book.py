"""Strategy B (the v2 EV hedge, frozen) with the POLYMARKET BOOK signal as the entry trigger instead of the crypto spot
shock (2026-10-02, the user: "figure out new book spot mode instead of crypto spot"). Everything after the trigger is
B unchanged (entry gate, hedges, locks, re-entry): replay2.run_window with a different trigger array.

    crypto        the replay's spot-shock proxy (1 s move >= 2.5 sigma, 3 s agreeing) -- B as replayed (+146.14)
    book  X       |book model| >= X cents, the side is its sign
    either X      crypto when it fires, else book >= X
    both  C       a crypto trigger the book showed (same side, >= C cents) within the last second
All triggers: at most one per direction per second (as the crypto proxy).

The book model is strategy/paired_exit.BookModel on paired_exit.book_features_at, with the TRAIN-only fit of the
book-signal study (data/replay/book_signal/book_model_train.json, windows 09-22 -> 09-27 13:20, inside this cache's
TRAIN): VALIDATION / TEST / FORWARD are out of sample for it. The archive's DOWN book mirrors UP's exactly (prices
and depths, every sample), so the cache's UP fields give both tokens.

    PAIRLOCK_OUT=<v2 cache dir> OUT3=<dir> python scripts/pairlock/replay_book.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
sys.path.insert(0, "/root/troll-poly-bot/src")
import replay3 as R3                                       # noqa: E402  (loads replay2 = strategy B, the cache)
from troll_poly_bot.strategy import paired_exit as px     # noqa: E402
from troll_poly_bot.strategy import pairlock_v2 as plv2   # noqa: E402

R, R2 = R3.R, R3.R2
OUT3 = os.environ.get("OUT3", "/tmp/pairlock_book")
BOOK_MODEL = "/root/troll-poly-bot/data/replay/book_signal/book_model_train.json"
SPLITS = R3.SPLITS


def _x(v):
    v = float(v)
    return None if v != v else v


def book_scores(w, bm: px.BookModel) -> np.ndarray:
    """The book model's score at every sample i0..i1-1, from samples <= i only (live: the 200 ms points)."""
    pts = []
    for i in range(len(w.t)):
        ub, ua, ubd, uad = _x(w.ub[i]), _x(w.ua[i]), _x(w.ubd[i]), _x(w.uad[i])
        pts.append({"t": float(w.t[i]), "ub": ub, "ua": ua, "ubd": ubd, "uad": uad,
                    "db": None if ua is None else round(1.0 - ua, 4), "da": None if ub is None else round(1.0 - ub, 4),
                    "dbd": uad, "dad": ubd})
    out = np.full(w.i1 - w.i0, np.nan)
    for k, i in enumerate(range(w.i0, w.i1)):
        out[k] = bm.score(px.book_features_at(pts, i))
    return out


def dedupe(w, raw: np.ndarray) -> np.ndarray:
    out = np.zeros(len(raw), dtype=np.int8)
    last = {1: -1e18, -1: -1e18}
    for k, i in enumerate(range(w.i0, w.i1)):
        d = int(raw[k])
        if d and w.t[i] - last[d] >= 1000.0:
            last[d] = w.t[i]
            out[k] = d
    return out


def trig_for(w, kind: str, x: float) -> np.ndarray:
    crypto = R.triggers(w, 2.5)
    if kind == "crypto":
        return crypto
    s = np.nan_to_num(_BOOK[w.slug], nan=0.0)
    book = np.where(np.abs(s) >= x, np.sign(s), 0).astype(np.int8)
    if kind == "book":
        return dedupe(w, book)
    if kind == "either":
        return dedupe(w, np.where(crypto != 0, crypto, book))
    if kind == "both":
        out = np.zeros_like(crypto)
        for k, i in enumerate(range(w.i0, w.i1)):
            d = int(crypto[k])
            if not d:
                continue
            j = k
            while j >= 0 and w.t[w.i0 + j] >= w.t[i] - 1000.0:
                if s[j] * d >= x:
                    out[k] = d
                    break
                j -= 1
        return out
    raise ValueError(kind)


_WS = None
_M2 = None
_BOOK: dict[str, np.ndarray] = {}            # slug -> book score per sample (the cache's Win has __slots__)


def _job(arg):
    kind, x, split = arg
    recs = []
    for w in R.split(_WS)[split]:
        recs.append(R3.run_window_v2(w, _M2, trig_for(w, kind, x)))
    return kind, x, split, recs


CONFIGS = [("crypto", 0.0)] + [("book", x) for x in (0.75, 1.0, 1.5, 2.0)] + \
          [("either", x) for x in (1.0, 1.5, 2.0)] + [("both", x) for x in (0.0, 0.5)]


def main():
    global _WS, _M2
    import multiprocessing as mp
    t0 = time.time()
    _WS = R.load_cache()
    _M2 = plv2.ProbModel.load(R3.MODEL_PATH)
    bm = px.BookModel.load(BOOK_MODEL)
    for w in _WS:
        _BOOK[w.slug] = book_scores(w, bm)
        if w.trig is None or w.trig[0] != 2.5:
            w.trig = (2.5, R.triggers(w, 2.5))
    print(f"book scores for {len(_WS)} windows in {time.time() - t0:.0f} s", flush=True)
    args = [(k, x, s) for k, x in CONFIGS for s in SPLITS]
    with mp.get_context("fork").Pool(int(os.environ.get("PROCS", "3"))) as pool:
        res = pool.map(_job, args, chunksize=1)
    by = defaultdict(dict)
    for kind, x, split, recs in res:
        by[f"{kind} {x:g}" if kind != "crypto" else "crypto"][split] = recs
    out = {}
    for name, d in by.items():
        out[name] = {s: R3.summarize(d[s]) for s in SPLITS}
        out[name]["train+validation"] = R3.summarize(d["train"] + d["validation"])
        out[name]["test+forward"] = R3.summarize(d["test"] + d["forward"])
        out[name]["all"] = R3.summarize([r for s in SPLITS for r in d[s]])
    os.makedirs(OUT3, exist_ok=True)
    json.dump(out, open(f"{OUT3}/replay_book_summary.json", "w"), indent=1, default=float)
    keys = ("train", "validation", "test", "forward", "train+validation", "test+forward", "all")
    print("| trigger | " + " | ".join(keys) + " | entries filled | hedges filled | 2nd cycles |")
    for name, o in out.items():
        a = o["all"]
        print(f"| {name} | " + " | ".join(f"{o[k]['net']:+.2f} ({o[k]['per_market']:+.3f}, t {o[k]['t']:.2f}, n {o[k]['markets']})"
                                          for k in keys)
              + f" | {a['entry_fills']} | {a['hedges_filled']} | {a['markets_with_2_cycles']} |")
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
