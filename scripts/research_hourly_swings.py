"""When is BTC calm? Swings by hour of day in GMT+9 (2026-10-06).

Two views, both bucketed by the GMT+9 (UTC+9, no DST) hour a bar / window STARTS in:

  A. BTC spot -- Binance BTCUSDT 1-minute klines, last DAYS days (cached under data/replay/hourly_swings/)
       sigma1m      RMS of 1-minute log returns, bp
       |5m| med     median |close/open - 1| of the clock-aligned 5-minute bars (the Polymarket windows), bp
       range5m      median (high - low) / open of those bars, bp
       big5m        share of the hour's 5-minute bars in the top 10 % of all |5m| moves (10 % = average)
       volume       mean BTC traded per minute
     split weekday / weekend (GMT+9 calendar day) and first half / second half (is the profile persistent?)

  B. Polymarket btc-updown-5m windows we recorded (data/charts/*.json, ~240 ms points)
       flips        lead changes per window: the UP mid crossing 0.55 -> 0.45 or back (the 5c hysteresis of
                    scripts/market_stability.py)
       late flip    share of windows with a flip in the last 60 s
       comeback     share of windows whose eventual winner traded <= 0.25 at some point after the first 60 s
       range        median (max - min) of the UP mid over the window, c
       |spot|       median |spot at close / spot at open - 1|, bp

    .venv/bin/python scripts/research_hourly_swings.py > results/hourly_swings_2026-10-06.md
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys
import time
import urllib.request

import numpy as np

DAYS = 180
TZ_MS = 9 * 3600 * 1000
CACHE = "/root/troll-poly-bot/data/replay/hourly_swings/btcusdt_1m.npy"
API = "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1000&startTime={}"


def fetch_klines(days: int) -> np.ndarray:
    """[open_ms, open, high, low, close, volume] per closed minute, oldest first."""
    end = (int(time.time() * 1000) // 60000) * 60000
    start = end - days * 86400 * 1000
    have = np.load(CACHE) if os.path.exists(CACHE) else np.empty((0, 6))
    have = have[have[:, 0] >= start] if len(have) else have
    t = int(have[-1, 0]) + 60000 if len(have) else start
    rows = []
    while t < end:
        for _ in range(5):
            try:
                with urllib.request.urlopen(API.format(t), timeout=20) as r:
                    batch = json.loads(r.read())
                break
            except Exception as e:  # noqa: BLE001
                print(f"retry {t}: {e}", file=sys.stderr)
                time.sleep(2)
        else:
            raise SystemExit("binance unreachable")
        batch = [b for b in batch if b[0] < end]
        if not batch:
            break
        rows += [[b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])] for b in batch]
        t = batch[-1][0] + 60000
    out = np.vstack([have, np.array(rows)]) if rows else have
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    np.save(CACHE, out)
    return out


def jst(ms):
    """GMT+9 hour of day, weekday (0 = Monday) and day number of epoch-ms timestamps."""
    ms = np.asarray(ms, dtype=np.int64) + TZ_MS
    day = ms // 86400000
    return (ms // 3600000) % 24, (day + 3) % 7, day   # 1970-01-01 was a Thursday


def spot_table(k: np.ndarray):
    T = k[:, 0].astype(np.int64)
    o, h, lo, c, v = k[:, 1], k[:, 2], k[:, 3], k[:, 4], k[:, 5]
    # 1-minute returns between consecutive minutes only
    r1 = np.log(c[1:] / c[:-1]) * 1e4
    ok1 = np.diff(T) == 60000
    T1, r1 = T[1:][ok1], r1[ok1]
    # clock-aligned 5-minute bars, complete ones only
    b = T // 300000
    starts = np.flatnonzero(np.r_[True, b[1:] != b[:-1]])
    ends = np.r_[starts[1:], len(T)]
    full = (ends - starts == 5) & (T[starts] % 300000 == 0)
    s, e = starts[full], ends[full] - 1
    T5 = T[s]
    ret5 = np.abs(c[e] / o[s] - 1) * 1e4
    hi5 = np.maximum.reduceat(h, starts)[full]
    lo5 = np.minimum.reduceat(lo, starts)[full]
    rng5 = (hi5 - lo5) / o[s] * 1e4
    big = ret5 >= np.quantile(ret5, 0.9)
    return dict(T=T, v=v, T1=T1, r1=r1, T5=T5, ret5=ret5, rng5=rng5, big=big, big_bp=float(np.quantile(ret5, 0.9)))


def spot_by_hour(S, mask1=None, mask5=None, maskv=None):
    h1, _, _ = jst(S["T1"])
    h5, _, _ = jst(S["T5"])
    hv, _, _ = jst(S["T"])
    m1 = np.ones(len(h1), bool) if mask1 is None else mask1
    m5 = np.ones(len(h5), bool) if mask5 is None else mask5
    mv = np.ones(len(hv), bool) if maskv is None else maskv
    rows = []
    for hr in range(24):
        a = m1 & (h1 == hr)
        f = m5 & (h5 == hr)
        g = mv & (hv == hr)
        rows.append(dict(
            hour=hr,
            sigma1m=float(np.sqrt(np.mean(S["r1"][a] ** 2))),
            med5=float(np.median(S["ret5"][f])),
            rng5=float(np.median(S["rng5"][f])),
            big=float(np.mean(S["big"][f])),
            vol=float(np.mean(S["v"][g])),
            n5=int(f.sum()),
        ))
    return rows


def window_metrics(path: str):
    d = json.load(open(path))
    t0, t1 = d["open_ts"], d["close_ts"]
    pts = [p for p in d["points"] if t0 <= p["t"] <= t1 and p.get("up") is not None and p.get("spot")]
    if len(pts) < 200 or d.get("outcome") not in ("UP", "DOWN"):
        return None
    if pts[0]["t"] - t0 > 5000 or t1 - pts[-1]["t"] > 5000:
        return None
    t = np.array([p["t"] for p in pts], float)
    up = np.array([p["up"] for p in pts], float)
    spot = np.array([p["spot"] for p in pts], float)
    # lead state with 5c hysteresis, from 1 s after the open
    i0 = int(np.searchsorted(t, t0 + 1000))
    state = 1 if up[i0] >= 0.5 else -1
    flips, late = 0, False
    for ti, u in zip(t[i0:], up[i0:]):
        if state == 1 and u <= 0.45:
            state, flips = -1, flips + 1
            late |= ti >= t1 - 60000
        elif state == -1 and u >= 0.55:
            state, flips = 1, flips + 1
            late |= ti >= t1 - 60000
    win = up if d["outcome"] == "UP" else 1 - up
    after = t >= t0 + 60000
    return dict(
        open=int(t0),
        flips=flips,
        late=late,
        comeback=bool(after.any() and win[after].min() <= 0.25),
        range=float((up.max() - up.min()) * 100),
        spot=float(abs(spot[-1] / spot[0] - 1) * 1e4),
    )


def poly_by_hour(W):
    h, _, _ = jst([w["open"] for w in W])
    rows = []
    for hr in range(24):
        ws = [w for w, x in zip(W, h) if x == hr]
        if not ws:
            rows.append(dict(hour=hr, n=0))
            continue
        rows.append(dict(
            hour=hr, n=len(ws),
            flips=float(np.mean([w["flips"] for w in ws])),
            late=float(np.mean([w["late"] for w in ws])),
            comeback=float(np.mean([w["comeback"] for w in ws])),
            range=float(np.median([w["range"] for w in ws])),
            spot=float(np.median([w["spot"] for w in ws])),
        ))
    return rows


def spearman(a, b):
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def main():
    k = fetch_klines(DAYS)
    S = spot_table(k)
    first, last = k[0, 0], k[-1, 0]
    fmt = lambda ms: time.strftime("%Y-%m-%d %H:%M", time.gmtime((ms + TZ_MS) / 1000))  # noqa: E731
    print("# BTC swings by hour of day, GMT+9 (2026-10-06)\n")
    print(f"Spot: Binance BTCUSDT 1-minute klines {fmt(first)} -> {fmt(last)} GMT+9 ({len(k):,} minutes, "
          f"{len(S['T5']):,} complete 5-minute bars). 'big' = |5m| >= {S['big_bp']:.1f} bp, the top 10 % of all "
          f"5-minute moves (so 10 % is average).\n")

    all_ = spot_by_hour(S)
    _, wd1, _ = jst(S["T1"])
    _, wd5, _ = jst(S["T5"])
    _, wdv, _ = jst(S["T"])
    wk = spot_by_hour(S, wd1 < 5, wd5 < 5, wdv < 5)
    we = spot_by_hour(S, wd1 >= 5, wd5 >= 5, wdv >= 5)
    mid = first + (last - first) / 2
    h1a = spot_by_hour(S, S["T1"] < mid, S["T5"] < mid, S["T"] < mid)
    h2a = spot_by_hour(S, S["T1"] >= mid, S["T5"] >= mid, S["T"] >= mid)
    rec = last - 30 * 86400000
    r30 = spot_by_hour(S, S["T1"] >= rec, S["T5"] >= rec, S["T"] >= rec)
    day_sigma = np.mean([r["sigma1m"] for r in all_])

    print(f"## A. Spot, all {DAYS} days\n")
    print("| GMT+9 hour | sigma1m bp | vs day avg | |5m| med bp | 5m range med bp | big 5m | BTC/min | "
          "sigma1m weekday | sigma1m weekend | sigma1m last 30 d |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r, a, b, c in zip(all_, wk, we, r30):
        print(f"| {r['hour']:02d}:00 | {r['sigma1m']:.2f} | {r['sigma1m'] / day_sigma - 1:+.0%} | {r['med5']:.1f} | "
              f"{r['rng5']:.1f} | {r['big']:.1%} | {r['vol']:.1f} | {a['sigma1m']:.2f} | {b['sigma1m']:.2f} | "
              f"{c['sigma1m']:.2f} |")
    s1, s2 = [r["sigma1m"] for r in h1a], [r["sigma1m"] for r in h2a]
    print(f"\nPersistence: Spearman rank correlation of the hourly sigma1m profile, first {DAYS // 2} days vs last "
          f"{DAYS // 2} days = {spearman(s1, s2):.2f}; weekday vs weekend = "
          f"{spearman([r['sigma1m'] for r in wk], [r['sigma1m'] for r in we]):.2f}; all vs last 30 d = "
          f"{spearman([r['sigma1m'] for r in all_], [r['sigma1m'] for r in r30]):.2f}.\n")
    order = sorted(all_, key=lambda r: r["sigma1m"])
    print("Calmest 6 hours (sigma1m): " + ", ".join(f"{r['hour']:02d}" for r in order[:6]) +
          ". Wildest 6: " + ", ".join(f"{r['hour']:02d}" for r in order[-6:][::-1]) + ".\n")

    # minute-of-hour profile inside the busiest hours (scheduled releases at :30?)
    mh = ((S["T1"] + TZ_MS) // 60000) % 60
    print("Minute-of-hour sigma1m (all hours): " + ", ".join(
        f":{m:02d} {np.sqrt(np.mean(S['r1'][mh == m] ** 2)):.2f}" for m in (0, 1, 2, 15, 29, 30, 31, 32, 45, 59)) + "\n")

    files = sorted(glob.glob("/root/troll-poly-bot/data/charts/btc-updown-5m-*.json"))
    W = [w for w in (window_metrics(f) for f in files) if w]
    W.sort(key=lambda w: w["open"])
    P = poly_by_hour(W)
    print(f"## B. Polymarket btc-updown-5m windows, {fmt(W[0]['open'])} -> {fmt(W[-1]['open'])} GMT+9 "
          f"({len(W):,} usable of {len(files):,} recorded)\n")
    print(f"All windows: flips {np.mean([w['flips'] for w in W]):.2f}/window, late flip "
          f"{np.mean([w['late'] for w in W]):.1%}, comeback {np.mean([w['comeback'] for w in W]):.1%}, range "
          f"{np.median([w['range'] for w in W]):.0f}c, |spot| {np.median([w['spot'] for w in W]):.1f} bp.\n")
    print("| GMT+9 hour | windows | flips/window | late flip | comeback | UP range med c | |spot| med bp |")
    print("|---|---|---|---|---|---|---|")
    for r in P:
        if not r["n"]:
            print(f"| {r['hour']:02d}:00 | 0 | | | | | |")
            continue
        print(f"| {r['hour']:02d}:00 | {r['n']} | {r['flips']:.2f} | {r['late']:.1%} | {r['comeback']:.1%} | "
              f"{r['range']:.0f} | {r['spot']:.1f} |")
    ok = [r for r in P if r["n"] >= 30]
    print(f"\nSpearman across hours (n >= 30 windows): spot sigma1m vs flips = "
          f"{spearman([all_[r['hour']]['sigma1m'] for r in ok], [r['flips'] for r in ok]):.2f}; "
          f"window |spot| vs flips = {spearman([r['spot'] for r in ok], [r['flips'] for r in ok]):.2f}; "
          f"window |spot| vs 180-d |5m| = {spearman([r['spot'] for r in ok], [all_[r['hour']]['med5'] for r in ok]):.2f}.")
    # sampling noise of an hourly flips mean
    sd = np.std([w["flips"] for w in W])
    n = np.median([r["n"] for r in ok])
    print(f"Noise: flips sd {sd:.2f}/window, so one hour's mean (~{n:.0f} windows) has a standard error of "
          f"~{sd / math.sqrt(n):.2f}.")
    json.dump(dict(spot=all_, weekday=wk, weekend=we, last30=r30, poly=P),
              open("/root/troll-poly-bot/data/replay/hourly_swings/summary.json", "w"), indent=1)


if __name__ == "__main__":
    main()
