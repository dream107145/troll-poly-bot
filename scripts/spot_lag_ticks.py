"""Spot -> token lead/lag and executable edge, from scripts/record_ticks.py output.

    python scripts/spot_lag_ticks.py [--ticks 'data/ticks/*.jsonl.gz'] [--pickle out.pkl] > report.md

READ-ONLY research: reads recordings and the chart archive (strikes and outcomes
of settled windows); places nothing, changes nothing.

Three clocks, never mixed within one measurement:
    EXCHANGE A   Bybit spot book stamp ``et``      vs Polymarket venue stamp ``vt``
    EXCHANGE B   Binance spot trade stamp ``et``   vs Polymarket venue stamp ``vt``
    LOCAL        spot received here ``rt``         vs token book received here ``rt``
The execution section judges every simulated order against the VENUE book
(the last token update whose venue stamp is <= the order's arrival time),
never against the local copy the bot would have been looking at.

Market-implied fair value: logit(Up mid) = a + b * logit(p_model) per
time-to-expiry bucket, fitted on the training third of the 5-minute archive
(1,532 windows, 2026-09-22 15:30 -> 09-23 14:20 UTC; quiet points only).
See docs/spot_lag_study_2026-09-23.md.
"""
from __future__ import annotations

import argparse, bisect, glob, gzip, json, math, random, sys, zlib
from collections import defaultdict
from pathlib import Path
import numpy as np
import orjson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.config import VolConfig                                    # noqa: E402
from troll_poly_bot.execution.latency import PRESEND_PRIOR_MS, VENUE_RTT_PRIOR_MS   # noqa: E402
from troll_poly_bot.pricing.twap import TwapState, twap_fair_value            # noqa: E402
from troll_poly_bot.pricing.vol import EwmaVol, TwoScaleVol                    # noqa: E402

#: (a, b) per seconds-left bucket; fitted on the archive's training period
COEF = {"240-300": (-0.00516, 0.74394), "180-240": (0.01201, 0.83442), "120-180": (0.00974, 0.87316),
        "90-120": (-0.01039, 0.90782), "60-90": (-0.00085, 0.92390), "30-60": (0.01608, 0.95148),
        "0-30": (-0.10100, 0.93414)}
BUCKETS = ((240, 300), (180, 240), (120, 180), (90, 120), (60, 90), (30, 60), (0, 30))
OFFS = (-1000, -500, -250, 0, 25, 50, 100, 150, 250, 500, 750, 1000, 2000, 3000, 5000)
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
fee = lambda p: 0.07 * p * (1 - p)  # noqa: E731


def read(paths):
    meta, spot_l, spot_x, books, ptr, clk = {}, defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list), []
    spot_t = defaultdict(list)
    for path in paths:
        try:
            with gzip.open(path, "rb") as fh:
                for line in fh:
                    try:
                        r = orjson.loads(line)
                    except orjson.JSONDecodeError:
                        continue
                    k = r["k"]
                    if k == "b":
                        if r["bb"] is not None and r["ba"] is not None:
                            books[r["tok"]].append((r["vt"] or r["rt"], r["rt"], r["bb"], r["ba"], r["bbs"], r["bas"],
                                                    r["bd2"], r["ad2"]))
                    elif k == "q":
                        if r["b"] and r["a"]:
                            mid = (r["b"] + r["a"]) / 2
                            if r["x"] == "bn" or (r["x"] == "by" and r["s"] == "HYPE"):
                                spot_l[r["s"]].append((r["rt"], mid))
                            if r["x"] == "by" and r.get("et"):
                                spot_x[r["s"]].append((r["et"], r["rt"], mid))
                    elif k == "t":
                        if r["x"] == "bn" and r.get("et") and r["p"]:
                            spot_t[r["s"]].append((r["et"], r["rt"], r["p"]))
                    elif k == "pt":
                        ptr[r["tok"]].append((r["vt"], r["rt"], r["p"], r["sz"], r["side"]))
                    elif k == "m":
                        meta[r["slug"]] = r
                    elif k == "clk":
                        clk.append(r)
        except (EOFError, OSError, zlib.error):
            pass                                   # a file still being written: read what is complete
    to_np = lambda xs: np.array(xs, dtype=float) if xs else np.zeros((0, 3))  # noqa: E731
    return (meta, {a: to_np(v) for a, v in spot_l.items()}, {a: to_np(v) for a, v in spot_x.items()},
            {t: to_np(v) for t, v in books.items()}, {t: v for t, v in ptr.items()}, clk,
            {a: to_np(v) for a, v in spot_t.items()})


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * (len(v) - 1)))] if v else float("nan")


def make_vol():
    v = VolConfig()
    return TwoScaleVol(fast=EwmaVol(grid_ms=v.grid_ms, halflife_s=v.halflife_s, floor_per_sec=v.floor_per_sec,
                                    ceil_per_sec=v.ceil_per_sec),
                       slow=EwmaVol(grid_ms=v.slow_grid_ms, halflife_s=v.slow_halflife_s,
                                    floor_per_sec=v.floor_per_sec, ceil_per_sec=v.ceil_per_sec),
                       default_ratio=v.default_ratio)


def logit(p):
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def mfv(p, left):
    for lo, hi in BUCKETS:
        if lo < left <= hi:
            a, b = COEF[f"{lo}-{hi}"]
            return 1 / (1 + math.exp(-(a + b * logit(p))))
    return float("nan")



CLOCKS = (("x", "EXCHANGE clock A: Bybit book stamp -> Polymarket venue stamp"),
          ("xt", "EXCHANGE clock B: Binance trade stamp -> Polymarket venue stamp"),
          ("l", "LOCAL clock: spot received here -> token book received here (what the bot sees)"))


def main():
    import pickle
    import warnings
    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ticks", default=str(ROOT / "data/ticks/*.jsonl.gz"), help="glob of recordings")
    ap.add_argument("--pickle", default=None, help="also save per-event / per-signal records here")
    args = ap.parse_args()
    paths = sorted(glob.glob(args.ticks))
    if not paths:
        sys.exit(f"no recordings match {args.ticks}")
    meta, spot_l, spot_x, books, ptr, clk, spot_t = read(paths)
    idx = json.loads((ROOT / "data/charts_index.json").read_text())["rows"]
    strikes, outcome = {}, {}
    for slug in meta:
        row = idx.get(slug) or {}
        s, o = row.get("strike"), row.get("outcome")
        p = ROOT / f"data/charts/{slug}.json"
        if (not s or not o) and p.exists():
            try:
                doc = json.loads(p.read_text())
                s, o = s or doc.get("strike"), o or doc.get("outcome")
            except Exception:                                  # noqa: BLE001
                pass
        if s:
            strikes[slug] = s
        if o in ("UP", "DOWN"):
            outcome[slug] = o
    t_lo = min(v[0, 0] for v in spot_l.values() if len(v)); t_hi = max(v[-1, 0] for v in spot_l.values() if len(v))
    print(f"# Tick-level study\n\nrecording {(t_hi - t_lo) / 60000:.1f} min "
          f"({time_s(t_lo)}-{time_s(t_hi)} UTC), {len(meta)} windows seen, {len(strikes)} with an archived strike, "
          f"{len(outcome)} with a venue outcome; token book records {sum(len(v) for v in books.values()):,}; "
          f"this machine's clock vs Binance {np.mean([c['off'] for c in clk]):+.1f} ms (NTP-style, {len(clk)} syncs)")

    wins = defaultdict(list)
    for slug, m in meta.items():
        wins[m["s"]].append((m["open"], m["close"], m["up"], m["dn"], slug))
    for a in wins:
        wins[a].sort()

    def window_at(a, t):
        for o, c, up, dn, slug in wins.get(a, []):
            if o <= t < c:
                return o, c, up, dn, slug
        return None

    bk_v = {t: v[np.argsort(v[:, 0], kind="stable")] for t, v in books.items() if len(v)}
    bk_r = {t: v[np.argsort(v[:, 1], kind="stable")] for t, v in books.items() if len(v)}
    kv = {t: v[:, 0].copy() for t, v in bk_v.items()}; kr = {t: v[:, 1].copy() for t, v in bk_r.items()}

    def book_at(tok, t, clock):
        venue = clock != "l"
        arr = (bk_v if venue else bk_r).get(tok)
        if arr is None:
            return None
        i = bisect.bisect_right((kv if venue else kr)[tok], t) - 1
        return None if i < 0 else arr[i]

    def mid_at(tok, t, clock):
        b = book_at(tok, t, clock)
        return None if b is None else (b[2] + b[3]) / 2

    def spot_series(a, clock):
        s = {"x": spot_x, "xt": spot_t, "l": spot_l}[clock].get(a)
        if s is None or not len(s):
            return None, None
        s = s[np.argsort(s[:, 0], kind="stable")]
        return s[:, 0], (s[:, 1] if clock == "l" else s[:, 2])

    # ---------------------------------------------------------------- A. observation latency
    print("\n## A. How late each feed reaches this machine (receive time minus the source's own stamp), ms\n")
    print("| feed | n | min | p1 | p25 | median | p75 | p95 | p99 |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    feeds = (("Bybit spot order book (stamp = book update)", np.concatenate([v[:, 1] - v[:, 0] for v in spot_x.values() if len(v)])),
             ("Binance spot trades (stamp = trade time)", np.concatenate([v[:, 1] - v[:, 0] for v in spot_t.values() if len(v)])),
             ("Polymarket token book (stamp = venue event)", np.concatenate([v[:, 1] - v[:, 0] for v in books.values() if len(v)])))
    for name, v in feeds:
        v = v[(v > -5000) & (v < 20000)]
        print(f"| {name} | {len(v):,} | {v.min():.0f} | {np.percentile(v, 1):.0f} | {np.percentile(v, 25):.0f} | "
              f"{np.median(v):.0f} | {np.percentile(v, 75):.0f} | {np.percentile(v, 95):.0f} | {np.percentile(v, 99):.0f} |")

    # ---------------------------------------------------------------- B/C. event study
    results = {}
    ev_times = defaultdict(list)                      # local-clock event times, for the burst-latency split below
    for clock, _ in CLOCKS:
        rows = []
        for a in ASSETS:
            ts, px = spot_series(a, clock)
            if ts is None or len(ts) < 1000:
                continue
            lp = np.log(px)
            grid = np.arange(ts[0], ts[-1], 1000.0)
            g = lp[np.clip(np.searchsorted(ts, grid, side="right") - 1, 0, None)]
            r = np.diff(g)
            sigma = 1.4826 * np.median(np.abs(r - np.median(r))) or np.std(r)
            j = np.searchsorted(ts, ts - 1000.0, side="right") - 1
            r1 = np.where(j >= 0, lp - lp[np.maximum(j, 0)], 0.0)
            last = -1e18
            for k_ in np.where(np.abs(r1) >= 2.0 * sigma)[0]:
                t0 = ts[k_]
                if t0 - last < 3000.0:
                    continue
                w = window_at(a, t0)
                if w is None:
                    continue
                o, c, up, dn, slug = w
                left = (c - t0) / 1000.0
                if not (5 <= left <= 295):
                    continue
                last = t0
                if clock == "l":
                    ev_times[a].append(t0)
                d = 1 if r1[k_] > 0 else -1
                base = mid_at(up, t0 - 1000.0, clock)
                if base is None:
                    continue
                resp = {}
                for h in OFFS:
                    m = mid_at(up, t0 + h, clock)
                    resp[h] = None if m is None else d * (m - base)
                if resp[5000] is None:
                    continue
                bk = book_at(up, t0, clock)
                gk = int(np.searchsorted(grid, t0)) - 1          # the last full second before the move
                rv = float(np.std(r[max(0, gk - 60):max(0, gk)])) if gk >= 20 else float("nan")
                rows.append({"asset": a, "z": abs(r1[k_]) / sigma, "left": left, "dir": d, "resp": resp,
                             "spread": None if bk is None else bk[3] - bk[2], "t0": t0,
                             "vol_ratio": rv / sigma if sigma else float("nan"),
                             "depth": None if bk is None else min(bk[4], bk[5])})
        results[clock] = rows

    # token-feed latency around spot events vs at other times (local-clock events)
    burst, quiet = [], []
    for a, tlist in ev_times.items():
        tl = np.array(sorted(tlist))
        for o, c, up, dn, slug in wins.get(a, []):
            for tok in (up, dn):
                arr = bk_r.get(tok)
                if arr is None:
                    continue
                i = np.searchsorted(tl, arr[:, 1], side="right") - 1
                near = (i >= 0) & (arr[:, 1] - tl[np.maximum(i, 0)] <= 2000.0)
                lat = arr[:, 1] - arr[:, 0]
                burst.append(lat[near]); quiet.append(lat[~near])
    if burst:
        b_, q_ = np.concatenate(burst), np.concatenate(quiet)
        print("\nPolymarket feed delay in the 2 s after a >= 2 sigma spot move vs the rest of the time, ms:\n")
        print("| period | n | median | p75 | p90 | p95 | p99 |\n|---|---:|---:|---:|---:|---:|---:|")
        for name, v in (("0-2 s after a spot move", b_), ("all other times", q_)):
            print(f"| {name} | {len(v):,} | {np.median(v):.0f} | {np.percentile(v, 75):.0f} | {np.percentile(v, 90):.0f} | "
                  f"{np.percentile(v, 95):.0f} | {np.percentile(v, 99):.0f} |")

    lag_tables = {}
    for clock, lab in CLOCKS:
        rows = results[clock]
        print(f"\n## B. Token response to a >= 2 sigma 1 s spot move -- {lab}\n")
        print("Up-token mid change since 1 s BEFORE the move was detected, signed in the move's direction, cents.\n")
        print("| asset | events | " + " | ".join(f"{h:+d} ms" for h in OFFS) + " | 50% of 5 s response by |")
        print("|---|---:" + "|---:" * len(OFFS) + "|---:|")
        for a in list(ASSETS) + ["ALL"]:
            rr = [r for r in rows if a == "ALL" or r["asset"] == a]
            if len(rr) < 5:
                continue
            means = {h: np.nanmean([r["resp"][h] for r in rr if r["resp"][h] is not None]) for h in OFFS}
            full = means[5000]
            t50 = next((h for h in OFFS if full > 0 and means[h] >= 0.5 * full), None)
            print(f"| {a} | {len(rr)} | " + " | ".join(f"{means[h] * 100:+.2f}" for h in OFFS)
                  + f" | {'' if t50 is None else f'{t50:+d} ms'} |")

        print(f"\nPer-event lag to half the 5 s response (events whose 5 s response >= 1c), ms -- {lab.split(':')[0]}:\n")
        print("| asset | n | p25 | median | p75 | p90 | p95 | already >= half when the move is seen |\n"
              "|---|---:|---:|---:|---:|---:|---:|---:|")
        lagd = defaultdict(list)
        for r in rows:
            full = r["resp"][5000]
            if full is None or full < 0.01:
                continue
            t50 = next((h for h in OFFS if r["resp"][h] is not None and r["resp"][h] >= 0.5 * full), 5000)
            lagd[r["asset"]].append(t50); lagd["ALL"].append(t50)
            r["t50"] = t50
        lag_tables[clock] = lagd
        for a in list(ASSETS) + ["ALL"]:
            v = lagd.get(a, [])
            if len(v) < 5:
                continue
            print(f"| {a} | {len(v)} | {pct(v, .25):+.0f} | {pct(v, .5):+.0f} | {pct(v, .75):+.0f} | {pct(v, .9):+.0f} | "
                  f"{pct(v, .95):+.0f} | {np.mean([x <= 0 for x in v]):.0%} |")
        print(f"\nMedian lag by condition -- {lab.split(':')[0]}:\n")
        conds = (("time left", lambda r: "a 295-240s" if r["left"] >= 240 else "b 240-180s" if r["left"] >= 180 else
                  "c 180-120s" if r["left"] >= 120 else "d 120-90s" if r["left"] >= 90 else "e 90-60s" if r["left"] >= 60
                  else "f <60s"),
                 ("move strength", lambda r: "a 2-3 sigma" if r["z"] < 3 else "b 3-4 sigma" if r["z"] < 4 else "c >=4 sigma"),
                 ("direction", lambda r: "up" if r["dir"] > 0 else "down"),
                 ("60 s realised vol vs the recording's typical", lambda r: "?" if r["vol_ratio"] != r["vol_ratio"] else
                  "a calm (<0.8x)" if r["vol_ratio"] < 0.8 else "b normal" if r["vol_ratio"] < 1.25 else "c hot (>1.25x)"),
                 ("liquidity (smaller of best bid / ask size, shares)", lambda r: "?" if r["depth"] is None else
                  "a thin <50" if r["depth"] < 50 else "b 50-200" if r["depth"] < 200 else "c deep >=200"),
                 ("spread", lambda r: "?" if r["spread"] is None else "a 1c" if r["spread"] <= 0.011 else
                  "b 2c" if r["spread"] <= 0.021 else "c >2c"))
        for cname, fn in conds:
            g = defaultdict(list)
            for r in rows:
                if "t50" in r:
                    g[fn(r)].append(r["t50"])
            print(f"- {cname}: " + "; ".join(f"{k.split(' ', 1)[-1]} {pct(v, .5):+.0f} ms (n {len(v)})" for k, v in sorted(g.items())))

    # ---------------------------------------------------------------- D. impulse response, 50 ms bins
    print("\n## D. Impulse response over ALL data (not just events): token mid change per unit spot return, 50 ms bins\n")
    print("Cumulative share of the -0.5..+5 s response reached by each lag (negative lag = the token moved BEFORE the "
          "spot move was stamped / seen). OLS of the Up-mid change in bin t+L on the spot log return in bin t.\n")
    lags = list(range(-500, 5001, 50))
    show = (-250, 0, 50, 100, 150, 250, 500, 750, 1000, 2000)
    impulse = {}
    for clock, lab in CLOCKS:
        print(f"| asset ({lab.split(':')[0]}) | " + " | ".join(f"{L}ms" for L in show) + " | 50% at | 90% at | peak bin |")
        print("|---" + "|---:" * (len(show) + 3) + "|")
        for a in ASSETS:
            ts, px = spot_series(a, clock)
            if ts is None or len(ts) < 1000:
                continue
            bins = np.arange(ts[0], ts[-1], 50.0)
            lpb = np.log(px[np.clip(np.searchsorted(ts, bins, side="right") - 1, 0, None)])
            sret = np.diff(lpb)
            tm = np.full(len(bins), np.nan)
            for o, c, up, dn, slug in wins.get(a, []):
                arr = bk_v.get(up) if clock != "l" else bk_r.get(up)
                if arr is None:
                    continue
                keys = kv[up] if clock != "l" else kr[up]
                inw = (bins >= o) & (bins < c - 5000)
                ii = np.searchsorted(keys, bins[inw], side="right") - 1
                vals = np.where(ii >= 0, (arr[np.maximum(ii, 0), 2] + arr[np.maximum(ii, 0), 3]) / 2, np.nan)
                tm[inw] = vals
            dm = np.diff(tm)
            betas = []
            for L in lags:
                sh = L // 50
                if sh >= 0:
                    x, y = sret[:len(sret) - sh], dm[sh:]
                else:
                    x, y = sret[-sh:], dm[:len(dm) + sh]
                ok = ~np.isnan(x) & ~np.isnan(y) & (x != 0)
                betas.append(float(np.sum(x[ok] * y[ok]) / np.sum(x[ok] ** 2)) if ok.sum() >= 100 else 0.0)
            betas = np.array(betas)
            cum = np.cumsum(betas)
            tot = cum[-1]
            share = lambda L: cum[lags.index(L)] / tot if tot else float("nan")  # noqa: E731
            t50 = next((L for L in lags if share(L) >= 0.5), None)
            t90 = next((L for L in lags if share(L) >= 0.9), None)
            peak = lags[int(np.argmax(betas))]
            impulse[(clock, a)] = (lags, betas.tolist())
            print(f"| {a} | " + " | ".join(f"{share(L):.0%}" for L in show) + f" | {t50} ms | {t90} ms | {peak} ms |")
        print()

    # ---------------------------------------------------------------- E/F/G. the trade, at tick resolution
    print("\n## E/F/G. The spot-lag entry at tick resolution\n")
    print("Signal (same rule as the archive backtest): |1 s return| >= 2 sigma on the LOCAL spot feed, 3 s return agrees "
          "(>= 0.5 sigma), accelerating, 60-295 s left, 2 s dedupe per direction; token = the side the move favours. "
          "ev = token mid 1 s before + change in the market-implied fair value over that second. "
          "Lag score = ev - ask. The LOCAL book is what the bot sees; the VENUE book is the true book at the "
          "same instant (last update with venue stamp <= that time).\n")
    sim = []
    rng = random.Random(3)
    for a in ASSETS:
        ts, px = spot_series(a, "l")
        if ts is None:
            continue
        lp = np.log(px)
        vol, tw = make_vol(), TwapState()
        grid = np.arange(ts[0], ts[-1], 100.0)
        gi = np.clip(np.searchsorted(ts, grid, side="right") - 1, 0, None)
        pgrid = np.full(len(grid), np.nan); sgrid = np.full(len(grid), np.nan)
        for n_, (t, i) in enumerate(zip(grid, gi)):
            vol.update(px[i], t); tw.update(px[i], t)
            w = window_at(a, t)
            if w is None or not vol.ready or not tw.ready:
                continue
            strike = strikes.get(w[4])
            if not strike:
                continue
            fv = twap_fair_value(state=tw, strike=strike, sigma_per_sec=vol.sigma_per_sec, now_ms=t, close_ts_ms=w[1],
                                 info_lag_s=0.18)
            pgrid[n_] = fv.p_up; sgrid[n_] = vol.sigma_per_sec
        j1 = np.searchsorted(ts, ts - 1000.0, side="right") - 1
        j3 = np.searchsorted(ts, ts - 3000.0, side="right") - 1
        r1 = np.where(j1 >= 0, lp - lp[np.maximum(j1, 0)], 0.0)
        r3 = np.where(j3 >= 0, lp - lp[np.maximum(j3, 0)], 0.0)
        gidx = np.searchsorted(grid, ts, side="right") - 1
        sig_at = np.where(gidx >= 0, sgrid[np.maximum(gidx, 0)], np.nan)
        cand = np.where(np.abs(r1) >= 2.0 * np.nan_to_num(sig_at, nan=1e9))[0]
        last = {1: -1e18, -1: -1e18}
        for k_ in cand:
            gi_ = gidx[k_]
            if gi_ < 10 or np.isnan(pgrid[gi_]):
                continue
            sig = sgrid[gi_]
            d = 1 if r1[k_] > 0 else -1
            t0 = ts[k_]
            if t0 - last[d] < 2000.0 or r3[k_] * d < 0.5 * sig * math.sqrt(3) or not (r1[k_] * d > r3[k_] * d / 3):
                continue
            w = window_at(a, t0)
            if w is None:
                continue
            o, c, up, dn, slug = w
            left = (c - t0) / 1000.0
            if not (60 <= left <= 295):
                continue
            last[d] = t0
            tok = up if d > 0 else dn
            g0 = bisect.bisect_right(grid, t0 - 1000.0) - 1
            if g0 < 0 or np.isnan(pgrid[g0]):
                continue
            f_now = mfv(pgrid[gi_], left); f_bef = mfv(pgrid[g0], left + 1.0)
            if d < 0:
                f_now, f_bef = 1 - f_now, 1 - f_bef
            m_bef = mid_at(tok, t0 - 1000.0, "l")
            bl = book_at(tok, t0, "l")
            bx0 = book_at(tok, t0, "x")
            if m_bef is None or bl is None or bx0 is None:
                continue
            ev = m_bef + (f_now - f_bef)
            ask_l, bid_l = bl[3], bl[2]
            if not (0.05 <= ask_l <= 0.95) or ask_l - bid_l > 0.03:
                continue
            exit_cost = fee(min(max(ev, .01), .99)) + (ask_l - bid_l) / 2
            net_at = lambda px_: ev - px_ - fee(px_) - exit_cost - 0.005 - 0.005  # noqa: E731
            max_entry = max((q / 100 for q in range(1, 100) if net_at(q / 100) >= 0.01), default=0.0)
            won = outcome.get(slug)
            rec = {"asset": a, "slug": slug, "left": left, "z": abs(r1[k_]) / sig, "d": d, "t0": t0, "ev": ev,
                   "ask_local": ask_l, "bid_local": bid_l, "ask_venue_now": bx0[3], "lag_local": ev - ask_l,
                   "lag_venue_now": ev - bx0[3], "net_local": net_at(ask_l), "cap": min(ask_l + 0.02, max_entry),
                   "win_settle": None if won is None else float((won == "UP") == (d > 0)),
                   "implied_1s": f_now - f_bef, "token_1s": (bl[2] + bl[3]) / 2 - m_bef}
            lat_draw = (rng.choice(PRESEND_PRIOR_MS) * rng.uniform(.85, 1.15)
                        + rng.choice(VENUE_RTT_PRIOR_MS) * rng.uniform(.85, 1.15) / 2)
            for scen, lat in (("0ms", 0.0), ("50ms", 50.0), ("measured", lat_draw)):
                t_arr = t0 + lat
                bx = book_at(tok, t_arr, "x")
                if bx is None:
                    continue
                trades = [x for x in ptr.get(tok, []) if t0 < x[0] <= t_arr]
                rec[scen] = {"lat": lat, "ask_arr": bx[3], "lag_arr": ev - bx[3],
                             "race_trades": len(trades), "race_buy_sz": sum(x[3] for x in trades if x[4] == "BUY")}
                # "fill": the strategy's own cap (max entry for a 1c net edge); "chase": any price up to local ask + 2c
                for kind, cap in (("fill", rec["cap"]), ("chase", ask_l + 0.02)):
                    fill_px = None
                    if bx[3] <= cap + 1e-9:
                        if bx[5] >= 5:
                            fill_px = bx[3]
                        elif bx[7] >= 5 and bx[3] + 0.01 <= cap + 1e-9:
                            fill_px = bx[3] + 0.01
                    rec[scen][kind] = fill_px
                    if fill_px is None:
                        continue
                    for hh in (1000, 5000, 20000):
                        bb = book_at(tok, t_arr + hh, "x")
                        ok = bb is not None and t_arr + hh < c - 5000
                        rec[scen][f"{kind}_net_{hh}"] = (bb[2] - fill_px - fee(fill_px) - fee(bb[2])) if ok else None
                    rec[scen][f"{kind}_net_settle"] = None if won is None else rec["win_settle"] - fill_px - fee(fill_px)
            sim.append(rec)
    if args.pickle:
        with open(args.pickle, "wb") as fh:
            pickle.dump({"sim": sim, "events": results, "impulse": impulse,
                         "lags": {k: dict(v) for k, v in lag_tables.items()}}, fh)
    if not sim:
        print("no signals")
        return
    print(f"signals: {len(sim)} over {len({r['slug'] for r in sim})} windows\n")
    tm_ = np.mean([r["token_1s"] for r in sim]); im_ = np.mean([r["implied_1s"] for r in sim])
    print(f"Over the signal's 1 s: implied fair-value move {im_ * 100:+.2f}c, token mid's own move on the LOCAL book "
          f"{tm_ * 100:+.2f}c ({tm_ / im_:.0%} already repriced when the bot sees the move)\n")
    pops = (("all signals", lambda r: True),
            ("lag score > 0 locally", lambda r: r["lag_local"] > 0),
            ("lag score >= 2c locally", lambda r: r["lag_local"] >= 0.02),
            ("lag score > 5c locally", lambda r: r["lag_local"] > 0.05),
            ("ELIGIBLE: net edge >= 1c locally", lambda r: r["net_local"] >= 0.01))
    print("| population | n | lag, LOCAL book at signal | lag, VENUE book at signal | venue at arrival, 0 ms | 50 ms | measured |\n"
          "|---|---:|---:|---:|---:|---:|---:|")
    for name, fn in pops:
        pop = [r for r in sim if fn(r)]
        if not pop:
            print(f"| {name} | 0 | | | | | |"); continue
        cell = lambda scen: f"{np.mean([r[scen]['lag_arr'] for r in pop if scen in r]) * 100:+.2f}c"  # noqa: E731
        print(f"| {name} | {len(pop)} | {np.mean([r['lag_local'] for r in pop]) * 100:+.2f}c | "
              f"{np.mean([r['lag_venue_now'] for r in pop]) * 100:+.2f}c | {cell('0ms')} | {cell('50ms')} | {cell('measured')} |")
    ph = [r for r in sim if r["lag_local"] > 0]
    if ph:
        print(f"\nOf the {len(ph)} signals where the LOCAL book shows a positive lag, "
              f"{np.mean([r['lag_venue_now'] <= 0 for r in ph]):.0%} show NO lag on the VENUE book at that same instant "
              f"(the 'lag' is our own token-feed delay), and {np.mean([r['measured']['lag_arr'] <= 0 for r in ph if 'measured' in r]):.0%} "
              "have none left when a measured-latency order lands.")
    if ph:
        dl = np.array([r["lag_local"] - r["lag_venue_now"] for r in ph])
        print(f"Paired, same signals: local lag minus venue lag at the same instant = mean {dl.mean() * 100:+.2f}c, "
              f"median {np.median(dl) * 100:+.2f}c (positive = the venue ask was already higher than the ask the bot saw).")
    print("\nFOK 5-share order filled against the VENUE book at arrival. Two caps: STRATEGY = min(local ask + 2c, max entry "
          "for a 1c net edge) -- only eligible signals can fill; CHASE = local ask + 2c regardless of edge. Net per share "
          "after both fees; exits sell at the venue bid after N s, or hold to settlement (n in brackets).\n")
    print("| population | cap | latency | orders | fills | fill rate | taker trades in the race (mean) | net @1 s | net @5 s | "
          "net @20 s | net @settle | win @settle |\n|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, fn in pops:
        pop = [r for r in sim if fn(r)]
        for kind in ("fill", "chase"):
            if kind == "fill" and not name.startswith("ELIGIBLE"):
                continue
            for scen in ("0ms", "50ms", "measured"):
                rr = [r[scen] for r in pop if scen in r]
                f_ = [x for x in rr if x.get(kind) is not None]
                def m(key):
                    v = [x[f"{kind}_{key}"] for x in f_ if x.get(f"{kind}_{key}") is not None]
                    return f"{np.mean(v) * 100:+.2f}c ({len(v)})" if v else ""
                ws = [x[f"{kind}_net_settle"] + x[kind] + fee(x[kind]) for x in f_ if x.get(f"{kind}_net_settle") is not None]
                print(f"| {name} | {'STRATEGY' if kind == 'fill' else 'CHASE'} | {scen} | {len(rr)} | {len(f_)} | "
                      f"{len(f_) / max(1, len(rr)):.0%} | {np.mean([x['race_trades'] for x in rr]) if rr else 0:.1f} | "
                      f"{m('net_1000')} | {m('net_5000')} | {m('net_20000')} | {m('net_settle')} | "
                      f"{(f'{np.mean(ws):.0%}' if ws else '')} |")
    print("\nBy asset (all signals):\n")
    print("| asset | signals | lag local | lag venue at signal | lag venue at measured arrival | eligible | eligible fills (measured) |\n"
          "|---|---:|---:|---:|---:|---:|---:|")
    for a in ASSETS:
        pop = [r for r in sim if r["asset"] == a]
        if not pop:
            continue
        el = [r for r in pop if r["net_local"] >= 0.01]
        print(f"| {a} | {len(pop)} | {np.mean([r['lag_local'] for r in pop]) * 100:+.2f}c | "
              f"{np.mean([r['lag_venue_now'] for r in pop]) * 100:+.2f}c | "
              f"{np.mean([r['measured']['lag_arr'] for r in pop if 'measured' in r]) * 100:+.2f}c | {len(el)} | "
              f"{sum(1 for r in el if (r.get('measured') or {}).get('fill') is not None)} |")


def time_s(ms):
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(ms / 1000).strftime("%H:%M")


if __name__ == "__main__":
    main()
