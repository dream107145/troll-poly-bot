"""Polymarket order flow vs crypto spot: which one moves the TOKEN first? (2026-10-02 brief, items 4-6)

    nice -n 19 python scripts/pairlock/orderflow_study.py data/ticks/ticks_*.jsonl.gz > results/pairlock2_orderflow.md

Data: the tick recordings (scripts/record_ticks.py): every BTC UP / DOWN book update (best bid / ask, touch sizes,
depth within 2 ticks, venue time, receipt time), Polymarket trades, and Coinbase / Bybit / Binance quotes and
trades with each exchange's own event time and our receipt time.

Every 50 ms (receipt clock, i.e. what the bot could know then) we compute, over windows of 50 / 100 / 250 / 500 /
1000 / 2000 ms:
  * Polymarket order flow, signed toward UP (UP-side flow minus DOWN-side flow): price velocity, ask consumption,
    executed aggressive volume, cancellations, new liquidity, depth imbalance;
  * the crypto composite (median of the three exchanges' mids) return in bp;
and the token's FUTURE UP-mid change over 250 / 500 / 1000 / 2000 ms (cents). Reported:
  1. correlation of each signal with the future token move, per window and horizon (does it PREDICT?);
  2. the lead / lag structure: corr(signal at t, token move over (t, t+h]) vs corr(token move over (t-h, t],
     spot move over (t, t+h]) -- does the token lead spot or follow it;
  3. an event study: for every token move of >= 2c within 1 s, when the first strong PM-flow reading and the first
     strong spot move happened relative to the move's start (median, share before the start);
  4. per exchange: delivery lag (receipt - event time) and which exchange moved first on strong spot moves.
"""
from __future__ import annotations

import bisect
import math
import statistics
import sys
from collections import defaultdict, deque

import numpy as np

import gzip
import json


class bf:                                       # the two helpers of triangle_audit/bookflow_study.py, copied: that
    """module runs its whole study at import (no __main__ guard)."""
    NAMES = {"cb": "coinbase", "by": "bybit", "bn": "binance"}

    @staticmethod
    def load(path):
        mk, ev, lag = {}, [], defaultdict(list)
        with gzip.open(path, "rt") as f:
            for line in f:
                r = json.loads(line)
                k = r.get("k")
                if k == "m":
                    if r.get("s") == "BTC":
                        mk[r["up"]] = ("UP", r["slug"], r["open"], r["close"])
                        mk[r["dn"]] = ("DOWN", r["slug"], r["open"], r["close"])
                elif k in ("q", "t"):
                    if r.get("s") == "BTC" and r.get("x") in bf.NAMES:
                        ev.append(r)
                        if r.get("et"):
                            lag[bf.NAMES[r["x"]]].append(r["rt"] - r["et"])
                elif k in ("b", "pt") and r.get("tok") in mk:
                    ev.append(r)
        ev.sort(key=lambda r: r["rt"])
        return mk, ev, {x: statistics.median(v) for x, v in lag.items()}

    @staticmethod
    def mid(b):
        if b is None:
            return None
        bb, ba = b[1], b[2]
        if bb is None and ba is None:
            return None
        if bb is None:
            return ba - 0.005
        if ba is None:
            return bb + 0.005
        return 0.5 * (bb + ba)

WINS = (50.0, 100.0, 250.0, 500.0, 1000.0, 2000.0)
HOR = (250.0, 500.0, 1000.0, 2000.0)
STEP = 50.0
KEYS = ("vel", "cons", "exec", "cancel", "newliq", "imb")


class FastBook:
    """bookflow_study.Book with O(log n) lookups (its deque indexing made every lookup quadratic)."""
    __slots__ = ("ts", "rows", "lo")

    def __init__(self):
        self.ts, self.rows, self.lo = [], [], 0

    def push(self, r):
        self.ts.append(r["rt"])
        self.rows.append((r["rt"], r.get("bb"), r.get("ba"), r.get("bbs") or 0.0, r.get("bas") or 0.0,
                          r.get("bd2") or 0.0, r.get("ad2") or 0.0))
        cut = bisect.bisect_left(self.ts, r["rt"] - 6000.0, self.lo)
        self.lo = cut
        if self.lo > 4096:                                    # compact now and then
            self.ts, self.rows, self.lo = self.ts[self.lo:], self.rows[self.lo:], 0

    def at(self, t):
        i = bisect.bisect_right(self.ts, t, self.lo) - 1
        return self.rows[i] if i >= self.lo else None

    @property
    def now(self):
        return self.rows[-1] if len(self.rows) > self.lo else None


def flow(up, dn, trades, t, w):
    nu, nd = up.now, dn.now
    pu, pd = up.at(t - w), dn.at(t - w)
    if None in (nu, nd, pu, pd):
        return None

    def d(a, b):
        return 0.0 if a is None or b is None else (a - b) * 100.0

    def cons(n, p):
        if n[2] is None or p[2] is None:
            return 0.0
        if n[2] > p[2] + 1e-9:
            return p[4]
        if abs(n[2] - p[2]) < 1e-9:
            return max(0.0, p[4] - n[4])
        return 0.0
    bu = su = bd = sd = 0.0
    for (rt, p, sz, s, tok) in reversed(trades):
        if rt < t - w:
            break
        if rt > t:
            continue
        if tok == "UP":
            bu, su = (bu + sz, su) if s == "BUY" else (bu, su + sz)
        else:
            bd, sd = (bd + sz, sd) if s == "BUY" else (bd, sd + sz)
    dAu, dBu, dAd, dBd = nu[6] - pu[6], nu[5] - pu[5], nd[6] - pd[6], nd[5] - pd[5]
    return {"vel": (d(nu[2], pu[2]) + d(nu[1], pu[1]) - d(nd[2], pd[2]) - d(nd[1], pd[1])) / 2.0,
            "cons": cons(nu, pu) - cons(nd, pd),
            "exec": (bu - su) - (bd - sd),
            "cancel": max(0.0, -dAu - bu) - max(0.0, -dAd - bd) - (max(0.0, -dBu - su) - max(0.0, -dBd - sd)),
            "newliq": max(0.0, dBu) - max(0.0, dAu) - (max(0.0, dBd) - max(0.0, dAd)),
            "imb": ((nu[5] - nu[6]) - (nd[5] - nd[6])) / max(1.0, nu[5] + nu[6] + nd[5] + nd[6])}


def run(path):
    mk, ev, lag = bf.load(path)
    by_slug = defaultdict(dict)
    for tok, (side, slug, o, c) in mk.items():
        by_slug[slug][side] = (tok, o, c)
    books = defaultdict(FastBook)
    trades = deque()
    xmid = {}                                   # exchange -> (rt, et, mid)
    spot_hist = []                              # (rt, composite log-mid)
    spot_t = []
    rows = []                                   # (t, slug, {w: flow}, {w: spot ret bp}, up_mid)
    first_mover = []
    t_next = None
    cur = None
    for r in ev:
        t = r["rt"]
        k = r["k"]
        if k == "b":
            books[r["tok"]].push(r)
        elif k == "pt":
            side, slug, o, c = mk[r["tok"]]
            trades.append((t, r.get("p"), float(r.get("sz") or 0.0), (r.get("side") or "").upper(), side))
            while trades and trades[0][0] < t - 3000.0:
                trades.popleft()
        elif k == "q" and r.get("b") and r.get("a"):
            xmid[r["x"]] = (t, r.get("et") or t, 0.5 * (r["b"] + r["a"]))
            mids = [v[2] for x, v in xmid.items() if t - v[0] < 2000.0]
            if mids:
                spot_hist.append((t, math.log(statistics.median(mids))))
                spot_t.append(t)
                if len(spot_t) > 20000:                  # keep the last part only (6 s is all a lookup needs)
                    del spot_t[:10000]
                    del spot_hist[:10000]
        if t_next is None:
            t_next = t
        while t >= t_next:
            tt = t_next
            t_next += STEP
            live = [s for s, d in by_slug.items() if "UP" in d and d["UP"][1] <= tt < d["UP"][2] - 5000.0]
            if not live:
                continue
            slug = live[0]
            up, dn = books.get(by_slug[slug]["UP"][0]), books.get(by_slug[slug].get("DOWN", (None,))[0])
            if up is None or dn is None or up.now is None or dn.now is None:
                continue
            m = bf.mid(up.now)
            if m is None or not spot_hist:
                continue
            fl, sp = {}, {}
            st = spot_t
            s_now = spot_hist[-1][1]
            for w in WINS:
                f = flow(up, dn, trades, tt, w)
                if f is None:
                    break
                fl[w] = f
                j = bisect.bisect_right(st, tt - w) - 1
                sp[w] = (s_now - spot_hist[j][1]) * 1e4 if j >= 0 else 0.0
            if len(fl) == len(WINS):
                rows.append((tt, slug, fl, sp, m))
    return rows, lag, by_slug


def analyse(rows):
    T = np.array([r[0] for r in rows])
    slug = [r[1] for r in rows]
    M = np.array([r[4] for r in rows])
    SL = np.array([hash(x) for x in slug])
    out = {}
    # future token move (cents) per horizon, same market only
    fut = {}
    past = {}
    for h in HOR:
        j = np.searchsorted(T, T + h, side="right") - 1
        ok = SL[np.arange(len(T))] == SL[j]
        fut[h] = np.where(ok, (M[j] - M) * 100.0, np.nan)
        jb = np.searchsorted(T, T - h, side="right") - 1
        okb = (jb >= 0) & (SL[np.arange(len(T))] == SL[np.maximum(jb, 0)])
        past[h] = np.where(okb, (M - M[np.maximum(jb, 0)]) * 100.0, np.nan)
    S = {w: np.array([r[3][w] for r in rows]) for w in WINS}
    F = {(w, k): np.array([r[2][w][k] for r in rows]) for w in WINS for k in KEYS}
    # the spot's future move, to test token -> spot
    spot_fut = {}
    for h in HOR:
        # reconstruct the composite path from the 1000 ms return series is not exact; use S at the later sample
        j = np.searchsorted(T, T + h, side="right") - 1
        spot_fut[h] = np.where(j < len(T), np.nan, np.nan)
    out["n"] = len(rows)

    def corr(a, b):
        m = ~(np.isnan(a) | np.isnan(b))
        if m.sum() < 100 or np.std(a[m]) == 0 or np.std(b[m]) == 0:
            return float("nan")
        return float(np.corrcoef(a[m], b[m])[0, 1])

    def hit(a, b, q=0.9):
        m = ~(np.isnan(a) | np.isnan(b)) & (a != 0)
        if m.sum() < 100:
            return float("nan"), 0
        thr = np.quantile(np.abs(a[m]), q)
        s = m & (np.abs(a) >= thr) & (b != 0)
        if s.sum() < 30:
            return float("nan"), int(s.sum())
        return float(np.mean(np.sign(a[s]) == np.sign(b[s]))), int(s.sum())
    out["corr"] = {(src, w, h): corr(F[(w, src)] if src in KEYS else S[w], fut[h]) for src in KEYS + ("spot",)
                   for w in WINS for h in HOR}
    out["hit"] = {(src, w, h): hit(F[(w, src)] if src in KEYS else S[w], fut[h]) for src in KEYS + ("spot",)
                  for w in (100.0, 250.0, 500.0, 1000.0) for h in (500.0, 1000.0)}
    # does the token's recent move predict ITS OWN future (momentum) and the spot's recent move?
    out["token_mom"] = {(h, h2): corr(past[h], fut[h2]) for h in (250.0, 1000.0) for h2 in (500.0, 1000.0)}
    # token -> spot: past token move vs the spot return over the NEXT window (spot at t+h minus spot at t)
    sp_next = {}
    for h in (250.0, 500.0, 1000.0):
        j = np.searchsorted(T, T + h, side="right") - 1
        okj = SL[np.arange(len(T))] == SL[j]
        sp_next[h] = np.where(okj, S[1000.0][j] - S[1000.0] if False else S[h][j], np.nan)   # spot ret over (t, t+h]
    out["token_to_spot"] = {h: corr(past[h], sp_next[h]) for h in (250.0, 500.0, 1000.0)}
    out["spot_to_token"] = {h: corr(S[h], fut[h]) for h in (250.0, 500.0, 1000.0)}
    # event study: token moves >= 2c within 1 s
    ev = []
    i = 0
    n = len(T)
    flow_sig = F[(250.0, "cons")] + F[(250.0, "exec")] / 50.0
    thr_f = float(np.nanquantile(np.abs(flow_sig), 0.9))          # computed ONCE (was inside the loop)
    thr_s = float(np.nanquantile(np.abs(S[250.0]), 0.9))
    while i < n:
        j = np.searchsorted(T, T[i] + 1000.0, side="right") - 1
        if j > i and slug[i] == slug[j] and abs(M[j] - M[i]) >= 0.02 - 1e-9:
            d = 1 if M[j] > M[i] else -1
            # first strong signal readings within [t-2 s, t+1 s] in the move's direction
            lo = np.searchsorted(T, T[i] - 2000.0)
            hi = j + 1
            sc_flow = flow_sig[lo:hi]
            st_spot = S[250.0][lo:hi]
            tf = next((T[lo + k] for k in range(hi - lo) if d * sc_flow[k] >= thr_f), None)
            ts = next((T[lo + k] for k in range(hi - lo) if d * st_spot[k] >= thr_s), None)
            ev.append((T[i], None if tf is None else tf - T[i], None if ts is None else ts - T[i]))
            i = j + 1
        else:
            i += 1
    out["events"] = ev
    return out


def main(paths):
    print("# Polymarket order flow vs crypto spot: does order flow move before the token, and before spot?\n")
    for p in paths:
        rows, lag, _ = run(p)
        if len(rows) < 1000:
            print(f"{p}: {len(rows)} samples, skipped\n")
            continue
        a = analyse(rows)
        hours = (rows[-1][0] - rows[0][0]) / 3_600_000.0
        print(f"## {p.split('/')[-1]}: {a['n']} samples every {STEP:.0f} ms over {hours:.2f} h\n")
        print("Exchange delivery lag (receipt - exchange event time, median ms): "
              + ", ".join(f"{x} {v:.0f}" for x, v in sorted(lag.items(), key=lambda kv: kv[1])) + "\n")
        print("**1. Correlation with the UP token's FUTURE mid move** (rows: signal over the past window; "
              "columns: horizon)\n")
        print("| signal | window ms | " + " | ".join(f"+{h:.0f} ms" for h in HOR) + " |")
        print("|---|---:|" + "---:|" * len(HOR))
        for src in ("spot",) + KEYS:
            for w in WINS:
                print(f"| {src} | {w:.0f} | " + " | ".join(f"{a['corr'][(src, w, h)]:+.3f}" for h in HOR) + " |")
        print("\n**Sign hit rate of the strongest 10 % of readings** (future move != 0)\n")
        print("| signal | window ms | +500 ms | +1000 ms |")
        print("|---|---:|---:|---:|")
        for src in ("spot",) + KEYS:
            for w in (100.0, 250.0, 500.0, 1000.0):
                h1, n1 = a["hit"][(src, w, 500.0)]
                h2, n2 = a["hit"][(src, w, 1000.0)]
                print(f"| {src} | {w:.0f} | {h1:.0%} (n {n1}) | {h2:.0%} (n {n2}) |")
        print("\n**2. Who leads**: corr(spot move over the last h, token move over the next h) vs "
              "corr(token move over the last h, spot move over the next h)\n")
        print("| h ms | spot -> token | token -> spot |")
        print("|---:|---:|---:|")
        for h in (250.0, 500.0, 1000.0):
            print(f"| {h:.0f} | {a['spot_to_token'][h]:+.3f} | {a['token_to_spot'][h]:+.3f} |")
        ev = a["events"]
        fl = [x[1] for x in ev if x[1] is not None]
        sp = [x[2] for x in ev if x[2] is not None]
        print(f"\n**3. Token moves of >= 2c within 1 s: {len(ev)}**. First strong reading in the move's direction "
              f"(strongest 10 % of the 250 ms window), relative to the move's start:\n")
        print(f"- Polymarket flow (ask consumption + executed volume): found for {len(fl)} ({len(fl) / max(len(ev), 1):.0%}); "
              f"median {statistics.median(fl) if fl else float('nan'):+.0f} ms; before the start {sum(1 for x in fl if x < 0) / max(len(fl), 1):.0%}")
        print(f"- crypto spot (250 ms return): found for {len(sp)} ({len(sp) / max(len(ev), 1):.0%}); "
              f"median {statistics.median(sp) if sp else float('nan'):+.0f} ms; before the start {sum(1 for x in sp if x < 0) / max(len(sp), 1):.0%}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
