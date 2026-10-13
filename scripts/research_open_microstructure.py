"""Research: the MARKET-OPEN MICROSTRUCTURE of the 5-minute UP/DOWN markets, and whether a
pre-open prediction can be traded AFTER the open instead of at it.

    python scripts/research_open_microstructure.py ticks       # event streams around the open (tick recording)
    python scripts/research_open_microstructure.py snapshots   # the market-open state machine (archive grid)
    python scripts/research_open_microstructure.py liquidity   # spread/depth recovery curves
    python scripts/research_open_microstructure.py shock       # opening shock and pre-open anchor tests
    python scripts/research_open_microstructure.py dev         # strategies A-H on TRAIN/VALIDATION
    python scripts/research_open_microstructure.py sweep       # parameter sweep (TRAIN/VALIDATION)
    python scripts/research_open_microstructure.py lock        # freeze candidates (never reads TEST)
    python scripts/research_open_microstructure.py final       # TEST + FORWARD, read once
    python scripts/research_open_microstructure.py figures     # PNG figures under results/open_micro_figs

RESEARCH ONLY. Reads data/charts (via data/replay/next_market/*), data/ticks; writes data/replay/open_micro/
and results/open_micro_*. Changes no strategy configuration and no running process; places no orders.
TPB_NEXT_MARKET_TWAP_ENABLED stays false.

Data and what it can resolve
----------------------------
* Archive grid (analyze_next_market_twap.Data): every market, both tokens' touch and 2-tick depth and the
  composite spot as-of every 50 ms; the source is sampled every ~224 ms, so 50/100 ms offsets are one
  archive step and depth beyond 2 ticks does NOT exist.
* Tick recording (data/ticks/ticks_20260923_204148.jsonl.gz, 90 min, 126 markets, inside TRAIN): every
  token book change (touch, touch size, 2-tick depth) and every token trade (price, size, taker side),
  venue-timestamped. This is the only source of sub-250 ms resolution, trades and queue position.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import orjson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_next_market_twap as A  # noqa: E402
import research_next_market_twap as RB  # noqa: E402

OUT = ROOT / "data/replay/open_micro"
RES = ROOT / "results"
FIGS = RES / "open_micro_figs"
TICKS = ROOT / "data/ticks/ticks_20260923_204148.jsonl.gz"
ASSETS = RB.ASSETS
SNAP_S = (-30, -20, -10, -5, -3, -2, -1, 0, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10, 30)
BUCKETS_MS = ((0, 50), (50, 100), (100, 250), (250, 500), (500, 750), (750, 1000), (1000, 1500), (1500, 2000),
              (2000, 3000), (3000, 5000))
SIZES_USD = (1.0, 2.0, 5.0, 10.0)


# ─────────────────────────────── tick recording: event streams around the open ───────────────────────────────


def extract_ticks(window_s: float = 30.0) -> dict:
    """Per market and side: book events and trades within +-window_s of the open, venue time relative to
    the open in ms. Saved to OUT/ticks_boundary.json."""
    tok = {}
    books, trades = defaultdict(list), defaultdict(list)
    with gzip.open(TICKS, "rb") as fh:
        for line in fh:
            k = line[5:7]
            if k == b'"m':
                d = orjson.loads(line)
                tok[d["up"]] = (d["slug"], d["s"], "UP", d["open"])
                tok[d["dn"]] = (d["slug"], d["s"], "DOWN", d["open"])
            elif k == b'"b' or k == b'"p':
                d = orjson.loads(line)
                m = tok.get(d["tok"])
                if m is None:
                    continue
                rel = (d.get("vt") or d["rt"]) - m[3]
                if abs(rel) > window_s * 1000.0:
                    continue
                key = m[0] + "|" + m[2]
                if d["k"] == "b":
                    books[key].append([rel, d.get("bb"), d.get("ba"), d.get("bbs"), d.get("bas"), d.get("bd2"), d.get("ad2"),
                                       d["rt"] - m[3]])
                else:
                    trades[key].append([rel, d["p"], d["sz"], 1 if d["side"] == "BUY" else -1])
    out = {}
    for key in set(books) | set(trades):
        slug, side = key.split("|")
        asset = slug.split("-")[0].upper()
        out[key] = {"slug": slug, "asset": asset, "side": side, "epoch": int(slug.rsplit("-", 1)[1]),
                    "books": sorted(books.get(key, [])), "trades": sorted(trades.get(key, []))}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ticks_boundary.json").write_bytes(orjson.dumps(out))
    nb = sum(len(v["books"]) for v in out.values())
    nt = sum(len(v["trades"]) for v in out.values())
    print(f"  {len(out)} token streams, {nb} book events, {nt} trades within +-{window_s:g} s of the open")
    return out


def load_ticks() -> dict:
    return orjson.loads((OUT / "ticks_boundary.json").read_bytes())


def _asof(events, rel_ms, col):
    """Value of column ``col`` of the last event at or before rel_ms (None if none or None there)."""
    lo, hi = 0, len(events)
    while lo < hi:
        mid = (lo + hi) // 2
        if events[mid][0] <= rel_ms:
            lo = mid + 1
        else:
            hi = mid
    return events[lo - 1][col] if lo else None


def tick_liquidity_curve() -> dict:
    """Event-resolution spread and depth after the open, by asset and side, from the tick recording."""
    T = load_ticks()
    rows = []
    grid = np.arange(-3000, 5001, 10)
    per = defaultdict(lambda: defaultdict(list))        # (asset, side) -> bucket -> spreads
    ttt = defaultdict(list)                              # time to spread <= x c
    for key, v in T.items():
        b = v["books"]
        if not b:
            continue
        first_tight = {2: None, 3: None, 5: None}
        for g in grid:
            bb, ba = _asof(b, g, 1), _asof(b, g, 2)
            if bb is None or ba is None or _asof(b, g, 0) is None:
                continue
            sp = round((ba - bb) * 100)
            for lo, hi in BUCKETS_MS:
                if lo <= g < hi:
                    per[(v["asset"], v["side"])][(lo, hi)].append((sp, _asof(b, g, 6) or 0.0))
            if g >= 0:
                for c in first_tight:
                    if first_tight[c] is None and sp <= c:
                        first_tight[c] = g
        for c, t in first_tight.items():
            ttt[(v["asset"], v["side"], c)].append(t if t is not None else np.inf)
    out = {"buckets": [], "time_to": []}
    for (asset, side), bk in sorted(per.items()):
        for (lo, hi), vals in sorted(bk.items()):
            sp = np.array([x[0] for x in vals]); dp = np.array([x[1] for x in vals])
            out["buckets"].append({"asset": asset, "side": side, "lo_ms": lo, "hi_ms": hi, "n": len(sp),
                                   "spread_p25": float(np.percentile(sp, 25)), "spread_p50": float(np.median(sp)),
                                   "spread_p75": float(np.percentile(sp, 75)), "spread_p90": float(np.percentile(sp, 90)),
                                   "depth2_p50": float(np.median(dp)), "p_zero_depth": float(np.mean(dp <= 0)),
                                   "p_spread_le2": float(np.mean(sp <= 2)), "p_spread_le5": float(np.mean(sp <= 5))})
    for (asset, side, c), ts in sorted(ttt.items()):
        ts = np.array(ts)
        fin = ts[np.isfinite(ts)]
        out["time_to"].append({"asset": asset, "side": side, "spread_le_c": c, "n": len(ts),
                               "p_reached_5s": float(np.mean(np.isfinite(ts))),
                               "median_ms": float(np.median(fin)) if len(fin) else None,
                               "p75_ms": float(np.percentile(fin, 75)) if len(fin) else None})
    (OUT / "tick_liquidity.json").write_text(json.dumps(out, indent=1))
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("ticks", "maker_calibration", "snapshots", "liquidity", "shock", "dev", "markouts", "sweep", "lock", "final", "figures"):
        sub.add_parser(c)
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if args.cmd == "ticks":
        extract_ticks()
        r = tick_liquidity_curve()
        for row in r["buckets"]:
            if row["asset"] in ("BTC", "HYPE") and row["side"] == "UP":
                print(row)
        for row in r["time_to"]:
            if row["side"] == "UP":
                print(row)
    else:
        globals()["cmd_" + args.cmd]()



# ─────────────────────────────── maker fills, calibrated on the tick recording ───────────────────────────────

ONE_WAY_MS = 230.0          # presend p50 48 + RTT/2 p50 121 + ~60 ms venue processing: order arrival after decision
CANCEL_MS = 230.0


def pre_open_fair_ticks(b, side_books_other=None) -> float | None:
    """The last pre-open mid with spread <= 5c (cents), from the event stream (None if none in [-30, 0) s)."""
    best = None
    for ev in b:
        if ev[0] >= 0:
            break
        if ev[1] is not None and ev[2] is not None and ev[2] - ev[1] <= 0.0501:
            best = (ev[1] + ev[2]) / 2.0
    return best


def maker_fill_ticks(v_this, v_other, t_post_ms: float, price: float, rest_ms: float, size: float):
    """Simulate a resting BUY on one token posted at venue-relative time t_post (arrives ONE_WAY later)
    at ``price`` for ``rest_ms``. Returns dict(filled, t_fill, how, queue_ahead, arrival_bid, arrival_ask).

    Fill rules (conservative, price-time priority):
      * crossed: the order would cross the ask on arrival -> not a maker order (returned as 'CROSS').
      * cross-through: the token's ask falls to <= price while resting -> filled at price.
      * traded-through: taker SELL prints on this token at <= price, or taker BUY prints on the other token at
        >= 1 - price (the same trade seen from the complementary book), accumulate; the order fills when that
        volume exceeds the queue ahead of it (the displayed size at its price on arrival; the 2-tick depth if it
        rests inside the spread's second tick; unfillable-by-trade deeper than that) plus its own size.
    No fill from a print at a worse price than ours, and nothing after the rest time."""
    b, tr = v_this["books"], v_this["trades"]
    tro = v_other["trades"] if v_other else []
    t_arr = t_post_ms + ONE_WAY_MS
    t_end = t_arr + rest_ms
    bb, ba = _asof(b, t_arr, 1), _asof(b, t_arr, 2)
    out = {"t_post": t_post_ms, "price": price, "arrival_bid": bb, "arrival_ask": ba, "filled": False, "how": "NONE",
           "t_fill": None, "queue": None}
    if bb is None or ba is None:
        out["how"] = "NO_BOOK"
        return out
    if price >= ba - 1e-9:
        out["how"] = "CROSS"
        return out
    if abs(price - bb) < 1e-9:
        q = float(_asof(b, t_arr, 3) or 0.0)
    elif price > bb:
        q = 0.0
    elif price >= bb - 0.0201:
        q = float(_asof(b, t_arr, 5) or 0.0)
    else:
        q = float("inf")
    out["queue"] = q
    vol = 0.0
    events = [(t, "S", p, s) for t, p, s, sd in tr if sd < 0] + [(t, "S", 1.0 - p, s) for t, p, s, sd in tro if sd > 0]
    events += [(ev[0], "B", ev[2], 0.0) for ev in b if ev[2] is not None]
    events.sort()
    for t, kind, p, s in events:
        if t < t_arr:
            continue
        if t > t_end:
            break
        if kind == "B":
            if p <= price + 1e-9:
                out.update(filled=True, how="CROSS_THROUGH", t_fill=t)
                return out
        elif p <= price + 1e-9:
            vol += s
            if vol >= q + size:
                out.update(filled=True, how="TRADED_THROUGH", t_fill=t)
                return out
    out["vol_at_or_below"] = vol
    return out


def markout_ticks(v, t_ms: float, horizons_ms=(50, 100, 250, 500, 1000, 2000, 3000, 5000, 10000, 30000)):
    return {h: _asof(v["books"], t_ms + h, 1) for h in horizons_ms}


def cmd_maker_calibration() -> dict:
    """Resting-bid fill rates and post-fill markouts on the tick recording, by posting time, price level
    (relative to the arrival bid) and resting time. Both sides, every market."""
    T = load_ticks()
    by_slug = defaultdict(dict)
    for key, v in T.items():
        by_slug[v["slug"]][v["side"]] = v
    rows = []
    for slug, sides in by_slug.items():
        for side, v in sides.items():
            other = sides.get("DOWN" if side == "UP" else "UP")
            fair = pre_open_fair_ticks(v["books"])
            for t_post in (0.0, 250.0, 500.0, 1000.0, 2000.0):
                for level in ("bid", "bid+1", "bid+2", "fair-2", "fair-5", "fair-10"):
                    bb = _asof(v["books"], t_post + ONE_WAY_MS, 1)
                    if bb is None:
                        continue
                    if level.startswith("bid"):
                        price = round(bb + 0.01 * (int(level[4:]) if "+" in level else 0), 2)
                    else:
                        if fair is None:
                            continue
                        price = round(fair - 0.01 * int(level[5:]), 2)
                    for rest in (250.0, 500.0, 1000.0, 2000.0, 3000.0):
                        r = maker_fill_ticks(v, other, t_post, price, rest, 10.0)
                        mo = markout_ticks(v, r["t_fill"]) if r["filled"] else {}
                        rows.append({"slug": slug, "asset": v["asset"], "side": side, "t_post": t_post, "level": level,
                                     "price": price, "rest": rest, "fair": fair, **{k: r[k] for k in ("filled", "how", "t_fill", "queue", "arrival_bid", "arrival_ask")},
                                     **{f"mo_{h}": (None if x is None else round(x - price, 4)) for h, x in mo.items()}})
    import csv
    with open(OUT / "maker_calibration_ticks.csv", "w", newline="") as fh:
        fields = list(rows[0].keys()) + [f"mo_{h}" for h in (50, 100, 250, 500, 1000, 2000, 3000, 5000, 10000, 30000)]
        w = csv.DictWriter(fh, fieldnames=list(dict.fromkeys(fields)))
        w.writeheader(); w.writerows(rows)
    # summary
    summ = defaultdict(lambda: [0, 0, 0, 0, []])
    for r in rows:
        k = (r["level"], r["rest"])
        s = summ[k]
        s[0] += 1
        if r["how"] == "CROSS":
            s[3] += 1
        if r["filled"]:
            s[1] += 1
            if r["how"] == "TRADED_THROUGH":
                s[2] += 1
            if r.get("mo_3000") is not None:
                s[4].append(r["mo_3000"])
    print("level, rest_ms: n, crossed-on-arrival %, filled % (of non-crossed), of which traded-through, mean 3 s markout c")
    for (lvl, rest), s in sorted(summ.items()):
        nc = s[0] - s[3]
        print(f"  {lvl:8s} {rest:6.0f}: n {s[0]:4d} cross {100 * s[3] / s[0]:5.1f}%  fill {100 * s[1] / max(nc, 1):5.1f}%  "
              f"traded {s[2]:3d}  mo3s {100 * np.mean(s[4]) if s[4] else float('nan'):+6.2f}c (n {len(s[4])})")
    return rows



# ─────────────────────────────── the market-open state machine (archive grid) ───────────────────────────────


def _preopen_fair(D: A.Data, g: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per grid row: the last pre-open Up mid with spread <= 5c in [-10 s, 0), its bid and ask (NaN if none)."""
    idx = A.gi(np.round(np.arange(-10.0, 0.0, 0.05), 3))
    ub = D.px["ub"][g][:, idx].astype(np.float64); ua = D.px["ua"][g][:, idx].astype(np.float64)
    ok = (ub >= 0) & (ua >= 0) & (ua - ub <= 5)
    last = np.where(ok.any(1), ok.shape[1] - 1 - np.argmax(ok[:, ::-1], 1), -1)
    rows = np.arange(len(g))
    b = np.where(last >= 0, ub[rows, np.maximum(last, 0)], np.nan) / 100.0
    a = np.where(last >= 0, ua[rows, np.maximum(last, 0)], np.nan) / 100.0
    return (b + a) / 2.0, b, a


def _vwap_usd(ask, dep, usd):
    """VWAP of a $usd buy against the 3-level model (NaN when the order cannot fill or is under 5 shares)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        sh = np.floor(usd / ask * 100.0) / 100.0
    q, vw = A._fill_buy(ask, dep, ask + 0.02, np.where(np.isnan(sh), 0.0, sh), partial=False)
    return np.where((q > 0) & (sh >= A.MIN_SHARES), vw, np.nan)


def _rate(D: A.Data, g, s_end, span_s=1.0):
    """Book updates in the last ``span_s`` s before s_end: distinct changes of the Up touch or depth on the grid."""
    idx = A.gi(np.round(np.arange(s_end - span_s, s_end + 1e-9, 0.05), 3))
    v = np.stack([D.px["ub"][g][:, idx], D.px["ua"][g][:, idx], D.sz["ubd"][g][:, idx].astype(np.int32),
                  D.sz["uad"][g][:, idx].astype(np.int32)], -1)
    return (np.abs(np.diff(v, axis=1)).sum(-1) > 0).sum(1).astype(float)


def cmd_snapshots() -> None:
    """One row per market x snapshot offset: results/open_micro_snapshots.csv."""
    import csv
    D = A.Data()
    P = dict(np.load(A.OUT / "preds_final.npz")) if (A.OUT / "preds_final.npz").exists() else dict(np.load(A.OUT / "preds.npz"))
    r3 = np.where(D.tau == -3.0)[0]
    g = D.g[r3]
    fair, fb, fa = _preopen_fair(D, g)
    fields = ["slug_epoch", "asset", "epoch", "split", "outcome", "t_s", "p_up_model", "p_down_model", "p_up_preopen",
              "preopen_up_bid", "preopen_up_ask", "up_bid", "up_ask", "down_bid", "down_ask", "up_spread", "down_spread",
              "up_bid_depth2", "up_ask_depth2", "down_bid_depth2", "down_ask_depth2",
              "up_vwap_1", "up_vwap_2", "up_vwap_5", "up_vwap_10", "down_vwap_1", "down_vwap_2", "down_vwap_5", "down_vwap_10",
              "book_updates_1s", "book_imbalance", "spot", "spot_ret_1s", "spot_ret_5s", "sigma", "dist_strike_bps",
              "dist_preopen_mid", "market_age_s", "time_left_s", "book_age_s", "spot_age_s_at_pred"]
    rows = []
    for k, s in enumerate(SNAP_S):
        tq = np.full(len(g), float(s))
        ub, ua = D.price("ub", r3, tq), D.price("ua", r3, tq)
        db, da = D.price("db", r3, tq), D.price("da", r3, tq)
        ubd, uad = D.depth("ubd", r3, tq), D.depth("uad", r3, tq)
        dbd, dad = D.depth("dbd", r3, tq), D.depth("dad", r3, tq)
        x = D.x[g, A.gi(tq)]; x1 = D.x[g, A.gi(tq - 1.0)]; x5 = D.x[g, A.gi(tq - 5.0)]
        rate = _rate(D, g, float(s))
        mid = (ub + ua) / 2.0
        for i, r in enumerate(r3):
            rows.append([RB.ASSETS[D.asset[r]].lower() + "-" + str(D.epoch[r]), RB.ASSETS[D.asset[r]], int(D.epoch[r]),
                         RB.SPLIT_NAMES[D.split[r]], "UP" if D.y[r] == 1 else "DOWN", s,
                         round(float(P["D_twap"][r]), 4), round(1 - float(P["D_twap"][r]), 4), fair[i], fb[i], fa[i],
                         ub[i], ua[i], db[i], da[i], ua[i] - ub[i], da[i] - db[i], ubd[i], uad[i], dbd[i], dad[i],
                         *[_vwap_usd(ua[i:i + 1], uad[i:i + 1], u)[0] for u in SIZES_USD],
                         *[_vwap_usd(da[i:i + 1], dad[i:i + 1], u)[0] for u in SIZES_USD],
                         rate[i], (ubd[i] - uad[i]) / max(ubd[i] + uad[i], 1e-9) if ubd[i] == ubd[i] and uad[i] == uad[i] else np.nan,
                         math.exp(x[i]) if x[i] == x[i] else np.nan, (x[i] - x1[i]) * 1e4, (x[i] - x5[i]) * 1e4,
                         D.f("sigma")[r], (x[i] - D.f("lk_bot")[r]) * 1e4, mid[i] - fair[i], s, 300.0 - s,
                         D.age[g[i], A.gi(tq[i:i + 1])[0]], D.f("spot_age_s")[r]])
    RES.mkdir(exist_ok=True)
    with open(RES / "open_micro_snapshots.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(fields)
        for row in rows:
            w.writerow(["" if (isinstance(v, float) and v != v) else (round(v, 6) if isinstance(v, float) else v) for v in row])
    print(f"  {len(rows)} snapshot rows ({len(r3)} markets x {len(SNAP_S)} offsets)")


# ─────────────────────────────── liquidity recovery curve (archive) ───────────────────────────────


def cmd_liquidity() -> dict:
    """Spread, depth, executable-liquidity probability and time-to-recovery by asset and side, from the archive
    grid (>= 250 ms buckets are archive-resolved; the finer ones repeat the same ~224 ms sample)."""
    D = A.Data()
    r3 = np.where((D.tau == -3.0) & (D.split <= 1))[0]
    g = D.g[r3]
    out = {"buckets": [], "time_to": [], "by_offset": []}
    offs = np.round(np.arange(-3.0, 5.0 + 1e-9, 0.05), 3)
    for side, (bk, ak, adk) in (("UP", ("ub", "ua", "uad")), ("DOWN", ("db", "da", "dad"))):
        bid = D.px[bk][g][:, A.gi(offs)].astype(np.float64); ask = D.px[ak][g][:, A.gi(offs)].astype(np.float64)
        dep = D.sz[adk][g][:, A.gi(offs)].astype(np.float64)
        two = (bid >= 0) & (ask >= 0)
        sp = np.where(two, ask - bid, np.nan)
        exe = (ask >= 0) & (dep >= A.MIN_SHARES)
        usd = np.where(ask >= 0, dep * ask / 100.0, 0.0)
        for a in range(len(ASSETS)):
            m = D.asset[r3] == a
            for lo, hi in BUCKETS_MS:
                cols = (offs * 1000 >= lo) & (offs * 1000 < hi)
                s = sp[m][:, cols]; d = dep[m][:, cols]
                out["buckets"].append({"asset": ASSETS[a], "side": side, "lo_ms": lo, "hi_ms": hi, "n_markets": int(m.sum()),
                                       "spread_p25": float(np.nanpercentile(s, 25)), "spread_p50": float(np.nanmedian(s)),
                                       "spread_p75": float(np.nanpercentile(s, 75)), "spread_p90": float(np.nanpercentile(s, 90)),
                                       "depth2_p50": float(np.nanmedian(d)), "p_no_ask": float(np.mean(ask[m][:, cols] < 0)),
                                       "p_zero_depth": float(np.mean(~(d > 0))), "p_executable_5sh": float(np.mean(exe[m][:, cols]))})
            post = offs >= 0
            for c in (2, 3, 5):
                hit = (sp[m][:, post] <= c + 1e-9)
                t = np.where(hit.any(1), np.argmax(hit, 1) * 50.0, np.inf)
                fin = t[np.isfinite(t)]
                out["time_to"].append({"asset": ASSETS[a], "side": side, "kind": f"spread_le_{c}c", "n": int(m.sum()),
                                       "p_reached_5s": float(np.mean(np.isfinite(t))), "median_ms": float(np.median(fin)) if len(fin) else None,
                                       "p75_ms": float(np.percentile(fin, 75)) if len(fin) else None})
            for u in (1, 5, 10):
                hit = usd[m][:, post] >= u
                t = np.where(hit.any(1), np.argmax(hit, 1) * 50.0, np.inf)
                fin = t[np.isfinite(t)]
                out["time_to"].append({"asset": ASSETS[a], "side": side, "kind": f"depth2_ge_${u}", "n": int(m.sum()),
                                       "p_reached_5s": float(np.mean(np.isfinite(t))), "median_ms": float(np.median(fin)) if len(fin) else None,
                                       "p75_ms": float(np.percentile(fin, 75)) if len(fin) else None})
        for j, o in enumerate(offs):
            out["by_offset"].append({"side": side, "offset_s": float(o), "spread_p50": float(np.nanmedian(sp[:, j])),
                                     "spread_p90": float(np.nanpercentile(sp[:, j], 90)), "depth2_p50": float(np.nanmedian(dep[:, j])),
                                     "p_executable_5sh": float(np.mean(exe[:, j])),
                                     **{f"spread_p50_{ASSETS[a]}": float(np.nanmedian(sp[D.asset[r3] == a, j])) for a in range(len(ASSETS))}})
    (OUT / "liquidity_curve.json").write_text(json.dumps(out, indent=1))
    return out


# ─────────────────────────────── opening shock and the pre-open anchor ───────────────────────────────


def cmd_shock() -> dict:
    """opening_shock = post-open executable price - pre-open fair (the last tight pre-open mid). Distribution,
    reversal vs continuation, and which anchor the later price moves toward. TRAIN + VALIDATION."""
    D = A.Data()
    P = dict(np.load(A.OUT / "preds.npz"))
    r3 = np.where((D.tau == -3.0) & (D.split <= 1) & (D.y >= 0))[0]
    g = D.g[r3]
    fair, _, _ = _preopen_fair(D, g)
    pm = P["D_twap"][r3]
    have = ~np.isnan(fair)
    out = {"coverage": float(have.mean()), "n": int(have.sum()), "shock": [], "revert": [], "anchor": []}
    def mid_at(s):
        return D.mkt_p(r3, np.full(len(r3), s))
    for s in (0.0, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0):
        ua, ub = D.price("ua", r3, np.full(len(r3), s)), D.price("ub", r3, np.full(len(r3), s))
        da, db = D.price("da", r3, np.full(len(r3), s)), D.price("db", r3, np.full(len(r3), s))
        sa = ua - fair; sb = ub - fair                       # Up side: ask above fair = shock up
        sda = da - (1 - fair)
        for lab, v in (("up_ask_minus_fair", sa), ("up_bid_minus_fair", sb), ("down_ask_minus_fair_down", sda)):
            ok = have & ~np.isnan(v)
            row = {"t_s": s, "measure": lab, "n": int(ok.sum()), "p10": float(np.percentile(v[ok], 10)), "p25": float(np.percentile(v[ok], 25)),
                   "p50": float(np.median(v[ok])), "p75": float(np.percentile(v[ok], 75)), "p90": float(np.percentile(v[ok], 90)),
                   "p_abs_ge_3c": float(np.mean(np.abs(v[ok]) >= 0.03)), "p_abs_ge_10c": float(np.mean(np.abs(v[ok]) >= 0.10))}
            for a in range(len(ASSETS)):
                ma = ok & (D.asset[r3] == a)
                row[f"p50_{ASSETS[a]}"] = float(np.median(v[ma])) if ma.any() else None
            out["shock"].append(row)
    # reversion: regress mid(t+h) - mid(t) on (fair - mid(t)) and on (model - mid(t)); mids from two-sided books only
    for s in (0.25, 0.5, 1.0, 2.0, 3.0):
        m0 = mid_at(s)
        for h in (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0):
            m1 = mid_at(s + h)
            ok = have & ~np.isnan(m0) & ~np.isnan(m1)
            dev_f = fair[ok] - m0[ok]; dev_m = pm[ok] - m0[ok]; dy = m1[ok] - m0[ok]
            X = np.column_stack([np.ones(ok.sum()), dev_f, dev_m])
            beta, *_ = np.linalg.lstsq(X, dy, rcond=None)
            resid = dy - X @ beta
            cov = np.linalg.inv(X.T @ X) * (resid @ resid) / max(ok.sum() - 3, 1)
            bf = np.linalg.lstsq(np.column_stack([np.ones(ok.sum()), dev_f]), dy, rcond=None)[0][1]
            bm = np.linalg.lstsq(np.column_stack([np.ones(ok.sum()), dev_m]), dy, rcond=None)[0][1]
            big = ok & (np.abs(fair - m0) >= 0.03)
            frac = 1.0 - (m1[big] - fair[big]) / (m0[big] - fair[big])
            out["revert"].append({"t_s": s, "h_s": h, "n": int(ok.sum()), "beta_fair_alone": float(bf), "beta_model_alone": float(bm),
                                  "beta_fair_joint": float(beta[1]), "z_fair_joint": float(beta[1] / math.sqrt(cov[1, 1])),
                                  "beta_model_joint": float(beta[2]), "z_model_joint": float(beta[2] / math.sqrt(cov[2, 2])),
                                  "n_dev_ge_3c": int(big.sum()), "median_frac_reverted": float(np.median(frac)) if big.any() else None,
                                  "p_reverted_half": float(np.mean(frac >= 0.5)) if big.any() else None,
                                  "p_continued": float(np.mean(frac < 0)) if big.any() else None})
    # anchors as predictors of the outcome (TRAIN+VAL): pre-open fair vs model vs blends; adaptive fitted on TRAIN
    y = D.y[r3]
    tr = D.split[r3] == 0; va = D.split[r3] == 1
    ok = have
    lm, lf = A.logit(pm), A.logit(np.where(have, fair, 0.5))
    b, _ = A.fit_logistic(np.column_stack([lm, lf])[tr & ok], y[tr & ok], lam=1e-3)
    adapt = A.pred_logistic(b, np.column_stack([lm, lf]))
    out["adaptive_coefs"] = {"intercept": float(b[0]), "logit_model": float(b[1]), "logit_preopen": float(b[2])}
    for name, p in (("model", pm), ("preopen_fair", fair), ("blend_75_25", 0.75 * pm + 0.25 * fair), ("blend_50_50", 0.5 * pm + 0.5 * fair),
                    ("blend_25_75", 0.25 * pm + 0.75 * fair), ("adaptive(TRAIN-fit)", adapt)):
        for sp, mm in (("train", tr), ("validation", va)):
            k = mm & ok & ~np.isnan(p)
            out["anchor"].append({"anchor": name, "split": sp, "n": int(k.sum()), "acc": A.acc(p[k], y[k]), "brier": A.brier(p[k], y[k]),
                                  "logloss": A.logloss(p[k], y[k]), "ece": A.ece(p[k], y[k])})
    (OUT / "opening_shock.json").write_text(json.dumps(out, indent=1))
    return out


# ─────────────────────────────── the strategy simulator (extends analyze_next_market_twap.Sim) ───────────────────────────────

ANCHORS = ("D_twap", "PRE", "B75", "B50", "B25", "ADAPT")
HAZARD_PER_S = 0.015        # traded-through fills while resting at the touch (tick recording: ~53 in 1,188 x 3 s)


@dataclass
class MCfg(A.Cfg):
    delay: float = -1.0           # >= 0: decide at exactly this many s after the open (else scan cfg.win)
    side_rule: str = "model"      # model | ev | UP | DOWN
    maker_level: str = ""         # "" = taker; bid | bid+1 | bid+2 | fair-2 | fair-5 | fair-10 (cents below the anchor)
    rest: float = 1.0             # maker resting time, s
    hazard: float = HAZARD_PER_S  # traded-through hazard per second at the touch (0 = cross-through only)
    then_taker: int = 0           # maker: if unfilled when the spread is <= 2c, cross as a taker (if the edge holds)
    min_score: float = -9.0       # liquidity-recovery score gate (TRAIN quantile units; -9 = off)
    kappa_src: str = ""           # "" = the P source's own TRAIN convergence table

    def key(self):
        return tuple(getattr(self, f) for f in MCFG_FIELDS)


MCFG_FIELDS = A.CFG_FIELDS + ("delay", "side_rule", "maker_level", "rest", "hazard", "then_taker", "min_score")


class MSim(A.Sim):
    """A.Sim plus: pre-open anchors and blends as probability sources, per-source convergence tables, the
    liquidity-recovery score, and a resting-order fill model."""

    def __init__(self, D: A.Data, preds: dict, seed: int = 12345) -> None:
        super().__init__(D, preds, seed)
        n = len(D.y)
        # pre-open fair per market -> every dataset row of that market
        rows = np.arange(n)
        fair, fb, fa = _preopen_fair(D, D.g[rows])
        self.fair = fair
        pm = preds["D_twap"]
        self.P["PRE"] = fair
        for w, name in ((0.75, "B75"), (0.5, "B50"), (0.25, "B25")):
            self.P[name] = w * pm + (1 - w) * fair
        tr = (D.split == 0) & (D.tau == -3.0) & (D.y >= 0) & ~np.isnan(fair) & ~np.isnan(pm)
        b, _ = A.fit_logistic(np.column_stack([A.logit(pm[tr]), A.logit(fair[tr])]), D.y[tr], lam=1e-3)
        self.adapt_coefs = b
        ok = ~np.isnan(fair) & ~np.isnan(pm)
        self.P["ADAPT"] = np.where(ok, A.pred_logistic(b, np.column_stack([A.logit(np.where(ok, pm, .5)), A.logit(np.where(ok, fair, .5))])), np.nan)
        self.kappa_by = {"D_twap": self.kappa}
        for src in ANCHORS[1:]:
            self.kappa_by[src] = self._kappa_for(D, self.P[src])
        self._score_fit(D)

    def _kappa_for(self, D, p):
        tr = np.where((D.split == 0) & (D.tau == -3.0) & (D.y >= 0) & ~np.isnan(p))[0]
        kap = np.zeros((len(self.k_offsets), len(self.k_holds)))
        for i, e in enumerate(self.k_offsets):
            q0 = D.mkt_p(tr, e)
            for j, h in enumerate(self.k_holds):
                if h <= 0:
                    continue
                dq = D.mkt_p(tr, e + h) - q0; gap = p[tr] - q0
                ok = ~np.isnan(dq) & ~np.isnan(gap)
                kap[i, j] = max(0.0, float(np.sum(dq[ok] * gap[ok]) / max(np.sum(gap[ok] ** 2), 1e-12)))
        return kap

    def kappa_src_at(self, src, e, h):
        i = np.clip(np.searchsorted(self.k_offsets, e, side="right") - 1, 0, len(self.k_offsets) - 1)
        j = int(np.argmin(np.abs(self.k_holds - h)))
        return self.kappa_by.get(src, self.kappa)[i, j]

    # ── liquidity-recovery features and score ──

    def recovery_features(self, rows, up, e):
        """At decision offsets e (per row): spread now, 250 ms ago, 1 s ago; depth now / 250 ms ago; book updates
        in the last 1 s; |mid - pre-open fair|; book age. NaN-safe (median-filled by the fitted scaler)."""
        D = self.D
        def sp(off):
            return self.side_px(rows, up, e + off, "ask") - self.side_px(rows, up, e + off, "bid")
        s0, s1, s2 = sp(0.0), sp(-0.25), sp(-1.0)
        d0, d1 = self.side_px(rows, up, e, "adep"), self.side_px(rows, up, e - 0.25, "adep")
        upd = _rate(D, D.g[rows], 0.0) if np.isscalar(e) else np.array([_rate(D, D.g[rows[i:i + 1]], float(e[i]))[0] for i in range(len(rows))])
        mid = (self.side_px(rows, up, e, "ask") + self.side_px(rows, up, e, "bid")) / 2.0
        fair_side = np.where(up, self.fair[rows], 1 - self.fair[rows])
        age = D.age[D.g[rows], A.gi(e)]
        return np.column_stack([s0, s1 - s0, s2 - s0, np.log1p(np.nan_to_num(d0)), np.log1p(np.nan_to_num(d0)) - np.log1p(np.nan_to_num(d1)),
                                upd, np.abs(mid - fair_side), age])

    SCORE_NAMES = ("spread", "spread_decay_250ms", "spread_decay_1s", "log_depth", "depth_growth_250ms", "book_updates_1s",
                   "dist_from_preopen_fair", "book_age_s")

    def _score_fit(self, D):
        """OLS on TRAIN: 3 s bid markout of a taker buy of the MODEL side, decided 1 s after the open, on the
        recovery features. The score is the fitted markout; gates use its TRAIN quantiles."""
        tr = np.where((D.split == 0) & (D.tau == -3.0) & (D.y >= 0))[0]
        up = self.P["D_twap"][tr] >= 0.5
        e = np.full(len(tr), 1.0)
        X = self.recovery_features(tr, up, e)
        ask0 = self.side_px(tr, up, e, "ask"); bid3 = self.side_px(tr, up, e + 3.0, "bid")
        yv = bid3 - ask0
        ok = ~np.isnan(yv) & ~np.isnan(X).any(1) & ~np.isinf(X).any(1)
        self.score_med = np.nanmedian(X[ok], 0)
        Xf = np.where(np.isnan(X), self.score_med, X)
        self.score_mu, self.score_sd = Xf[ok].mean(0), Xf[ok].std(0) + 1e-12
        Z = np.column_stack([np.ones(ok.sum()), (Xf[ok] - self.score_mu) / self.score_sd])
        beta, *_ = np.linalg.lstsq(Z, yv[ok], rcond=None)
        self.score_beta = beta
        s = Z @ beta
        self.score_q = np.percentile(s, [10, 25, 50, 75, 90])
        self.score_train_corr = float(np.corrcoef(s, yv[ok])[0, 1])

    def score(self, rows, up, e):
        X = self.recovery_features(rows, up, e)
        Xf = np.where(np.isnan(X) | np.isinf(X), self.score_med, X)
        return np.column_stack([np.ones(len(rows)), (Xf - self.score_mu) / self.score_sd]) @ self.score_beta


def enter2(S: MSim, cfg: MCfg, rows: np.ndarray) -> dict:
    """Decision + fill for the extended configurations. Same output keys as A.enter (so A.simulate's exits apply)."""
    D = S.D
    p_up = S.P[cfg.model][rows]
    ok = ~np.isnan(p_up)
    rows, p_up = rows[ok], p_up[ok]
    n = len(rows)
    if cfg.delay >= 0:
        E = np.array([cfg.delay])
    elif cfg.win < 0:
        E = np.array([cfg.tau])
    else:
        E = np.round(np.arange(0.0, cfg.win + 1e-9, A.GSTEP_S), 3)
    Em = np.broadcast_to(E, (n, len(E)))

    def side_eval(up):
        p_side = np.where(up, p_up, 1.0 - p_up)
        ask = S.side_px(rows, up, Em, "ask"); bid = S.side_px(rows, up, Em, "bid"); dep = S.side_px(rows, up, Em, "adep")
        with np.errstate(invalid="ignore", divide="ignore"):
            shares = np.floor(cfg.usd / ask * 100.0) / 100.0
            valid = (~np.isnan(ask) & ~np.isnan(bid) & (ask <= cfg.max_ask + 1e-9) & (ask >= cfg.min_ask - 1e-9)
                     & (ask - bid <= cfg.max_spread + 1e-9) & (shares >= A.MIN_SHARES) & (shares <= cfg.depth_frac * dep + 1e-9)
                     & (p_side >= cfg.min_prob)[:, None])
            mid = (ask + bid) / 2.0
            q, vw = A._fill_buy(ask.ravel(), dep.ravel(), (ask + 0.02).ravel(), shares.ravel(), partial=False)
            vw = vw.reshape(ask.shape)
            paid = vw + cfg.adverse
            fin = A.fee(D.rate[rows][:, None], np.clip(paid, 0.01, 0.99))
            if cfg.maker_level:
                fin = 0.0 * fin                                   # maker entries pay no taker fee
            if cfg.edge_hold <= 0:
                edge = p_side[:, None] - paid - fin
            else:
                kap = S.kappa_src_at(cfg.kappa_src or cfg.model, Em, cfg.edge_hold)
                exb = np.clip(mid + kap * (p_side[:, None] - mid) - S.half_spread_at(Em + cfg.edge_hold), 0.0, 1.0)
                edge = exb - paid - fin - A.fee(D.rate[rows][:, None], exb)
            if cfg.min_edge > -9:
                valid &= edge >= cfg.min_edge - 1e-12
        return p_side, ask, bid, dep, shares, edge, valid

    if cfg.side_rule in ("UP", "DOWN"):
        up = np.full(n, cfg.side_rule == "UP")
        ev = [side_eval(up)]
        pick = np.zeros(n, int)
    elif cfg.side_rule == "ev":
        ev = [side_eval(np.ones(n, bool)), side_eval(np.zeros(n, bool))]
        e_up = np.where(ev[0][6], ev[0][5], -np.inf).max(1); e_dn = np.where(ev[1][6], ev[1][5], -np.inf).max(1)
        pick = (e_dn > e_up).astype(int)
        up = pick == 0
    else:
        up = p_up >= 0.5
        ev = [side_eval(up)]
        pick = np.zeros(n, int)
    # assemble per-row from the picked side
    def take(k):
        return np.where(pick[:, None] == 0, ev[0][k], ev[1][k]) if len(ev) == 2 else ev[0][k]
    p_side = np.where(pick == 0, ev[0][0], ev[1][0]) if len(ev) == 2 else ev[0][0]
    ask, bid, dep, shares, edge, valid = (take(k) for k in range(1, 7))
    if cfg.min_score > -9:
        sc = np.column_stack([S.score(rows, up, np.full(n, float(e))) for e in E])
        valid &= sc >= S.score_q[int(cfg.min_score)] if 0 <= cfg.min_score < 5 else sc >= cfg.min_score
    has = valid.any(1); first = np.argmax(valid, 1); sel = np.where(has)[0]; k = first[sel]
    o = {"rows": rows[sel], "up": up[sel], "p_side": p_side[sel], "e": E[k], "ask_d": ask[sel, k], "bid_d": bid[sel, k],
         "dep_d": dep[sel, k], "shares": shares[sel, k], "edge_d": edge[sel, k], "n_candidates": n, "maker_price": np.full(len(sel), np.nan),
         "how": np.array(["TAKER"] * len(sel), dtype=object)}
    r = o["rows"]; ow, ack, px = S.latency(r, cfg, 0)
    t_a = o["e"] + ow / 1000.0
    if not cfg.maker_level:
        ask_a = S.side_px(r, o["up"], t_a + px / 1000.0, "ask"); dep_a = S.side_px(r, o["up"], t_a + px / 1000.0, "adep")
        limit = {"fok0": o["ask_d"], "fak0": o["ask_d"], "fok1": o["ask_d"] + 0.01, "fok2": o["ask_d"] + 0.02}.get(cfg.entry, o["ask_d"])
        q, vw = A._fill_buy(ask_a, dep_a, limit, o["shares"], partial=cfg.entry == "fak0")
        o.update(t_arr=t_a, ask_a=ask_a, qty=q, vw_in=vw, t_known=t_a + ack / 1000.0, lat_ms=ow + px)
        return o
    # ── resting order ──
    fair_side = np.where(o["up"], S.fair[r], 1 - S.fair[r])
    if cfg.maker_level.startswith("bid"):
        price = np.round(o["bid_d"] + 0.01 * (int(cfg.maker_level[4:]) if "+" in cfg.maker_level else 0), 2)
    else:
        price = np.round(fair_side - 0.01 * int(cfg.maker_level[5:]), 2)
    price = np.minimum(price, o["ask_d"] - 0.01)            # never post at or through the ask: that is a taker order
    steps = np.round(np.arange(0.0, cfg.rest + 1e-9, A.GSTEP_S), 3)
    T = t_a[:, None] + steps[None, :] + px / 1000.0
    ask_t = S.side_px(r, o["up"], T, "ask"); bid_t = S.side_px(r, o["up"], T, "bid")
    cross = ask_t <= price[:, None] + 1e-9                 # the ask came down to us: filled at our price
    at_touch = ~cross & (np.abs(bid_t - price[:, None]) < 1e-9)
    H = np.cumsum(at_touch * cfg.hazard * A.GSTEP_S, 1)
    thr = -np.log(np.clip(S.u_noise[r], 1e-9, 1.0))
    trade_fill = H >= thr[:, None]
    hit = cross | trade_fill
    filled = hit.any(1); j = np.argmax(hit, 1)
    t_fill = t_a + steps[j]
    qty = np.where(filled, o["shares"], 0.0)
    vw = np.where(filled, price, np.nan)
    how = np.where(filled, np.where(cross[np.arange(len(r)), j], "CROSS_THROUGH", "TRADED_THROUGH"), "UNFILLED")
    if cfg.then_taker:
        # unfilled and the book recovered (spread <= 2c) at the end of the rest: cross if the taker edge still clears
        t_end = t_a + cfg.rest
        a2 = S.side_px(r, o["up"], t_end + px / 1000.0, "ask"); b2 = S.side_px(r, o["up"], t_end + px / 1000.0, "bid"); d2 = S.side_px(r, o["up"], t_end + px / 1000.0, "adep")
        tight = ~filled & ~np.isnan(a2) & ~np.isnan(b2) & (a2 - b2 <= 0.02 + 1e-9)
        q2, vw2 = A._fill_buy(a2, d2, a2, o["shares"], partial=False)
        fee2 = A.fee(D.rate[r], np.clip(vw2 + cfg.adverse, 0.01, 0.99))
        edge2 = np.clip(fair_side * 0 + (a2 + b2) / 2 + S.kappa_src_at(cfg.kappa_src or cfg.model, t_end, cfg.edge_hold) * (o["p_side"] - (a2 + b2) / 2) - S.half_spread_at(t_end + cfg.edge_hold), 0, 1) - vw2 - cfg.adverse - fee2
        edge2 = edge2 - A.fee(D.rate[r], np.clip(edge2 + vw2, 0.01, 0.99)) * 0
        go = tight & (q2 > 0) & (edge2 >= max(cfg.min_edge, 0.0))
        qty = np.where(go, q2, qty); vw = np.where(go, vw2, vw); t_fill = np.where(go, t_end, t_fill)
        how = np.where(go, "THEN_TAKER", how)
        filled = filled | go
    o.update(t_arr=t_fill, ask_a=ask_t[:, 0], qty=qty, vw_in=vw, t_known=t_fill + ack / 1000.0, lat_ms=ow + px,
             maker_price=price, how=how.astype(object))
    return o


def simulate2(S: MSim, cfg: MCfg, splits, exits=A.EXITS):
    o = enter2(S, cfg, A.candidate_rows(S, replace(cfg, model=cfg.model), splits) if cfg.model in S.P else np.zeros(0, int))
    return A.simulate(S, cfg, splits, exits, orders=o)


def run2(S, cfg, splits, ex):
    res = simulate2(S, cfg, splits, [ex])
    tt = A.trade_table(S, res, A.exit_name(ex))
    # maker entries paid no taker fee: correct the fee columns the generic table charged
    if cfg.maker_level:
        o, f = res["orders"], res["filled"]
        mk = np.array([h != "THEN_TAKER" for h in o["how"][f]])
        fin = A.fee(S.D.rate[tt["rows"]], tt["entry"]) * mk
        tt["fees"] = tt["fees"] - tt["qty"] * fin
        tt["net"] = tt["net"] + tt["qty"] * fin
        tt["net_c"] = tt["net_c"] + fin * 100.0
        tt["maker"] = mk
    return res, tt, A.summarize(tt, len(res["orders"]["rows"]))



# ─────────────────────────────── strategies A-H, markouts, sweep, lock, final ───────────────────────────────

E3 = ("t", 3.0)


def strategy_set() -> list[tuple[str, MCfg, tuple]]:
    """The required comparison, one execution model for all. Thresholds are the pre-registered 2c net edge."""
    base = MCfg(model="D_twap", tau=-3.0, min_edge=0.02)
    return [
        ("A original immediate taker (T+0, no filter, 3 s)", MCfg(model="D_twap", tau=-3.0, win=0.0), E3),
        ("A' immediate taker + 2c edge gate", replace(base, win=0.0), E3),
        ("B delayed taker, decide at +0.5 s, edge >= 2c", replace(base, delay=0.5), E3),
        ("B delayed taker, +1 s", replace(base, delay=1.0), E3),
        ("B delayed taker, +2 s", replace(base, delay=2.0), E3),
        ("B delayed taker, +3 s", replace(base, delay=3.0), E3),
        ("B delayed taker, first pass in 0-3 s (last study's P3)", replace(base, win=3.0), E3),
        ("C resting maker at the bid, post at open, rest 1 s", replace(base, maker_level="bid", rest=1.0, min_edge=0.0), E3),
        ("C resting maker at the bid, rest 3 s", replace(base, maker_level="bid", rest=3.0, min_edge=0.0), E3),
        ("C resting maker 5c under pre-open fair, rest 2 s", replace(base, maker_level="fair-5", rest=2.0, min_edge=0.0), E3),
        ("C maker at the bid 1 s, then taker if the book is tight", replace(base, maker_level="bid", rest=1.0, min_edge=0.0, then_taker=1), E3),
        ("D pre-open fair anchor, taker, first pass 0-3 s, edge >= 0c", replace(base, model="PRE", win=3.0, min_edge=0.0), E3),
        ("D pre-open fair anchor, +1 s, edge >= 1c", replace(base, model="PRE", delay=1.0, min_edge=0.01), E3),
        ("E model vs executable price, both sides by EV, +1 s, edge >= 2c", replace(base, delay=1.0, side_rule="ev"), E3),
        ("F 50/50 blend, +1 s, edge >= 1c", replace(base, model="B50", delay=1.0, min_edge=0.01), E3),
        ("F adaptive blend (TRAIN-fit), +1 s, edge >= 1c", replace(base, model="ADAPT", delay=1.0, min_edge=0.01), E3),
        ("G + liquidity-recovery score >= TRAIN median, +1 s, edge >= 0c", replace(base, delay=1.0, min_edge=0.0, min_score=2), E3),
        ("G score >= TRAIN p75, first pass 0-3 s, edge >= 0c", replace(base, win=3.0, min_edge=0.0, min_score=3), E3),
        ("H settlement-only control: model side at +1 s, ask <= 0.60, hold to settlement", MCfg(model="D_twap", tau=-3.0, delay=1.0, max_ask=0.60, edge_hold=0.0), ("settle",)),
        ("H settlement-only control, 2c edge gate", MCfg(model="D_twap", tau=-3.0, delay=1.0, max_ask=0.60, edge_hold=0.0, min_edge=0.02), ("settle",)),
        ("random side, +1 s, 3 s (baseline)", MCfg(model="R_random", tau=-3.0, delay=1.0), E3),
    ]


def perf_rows(S, items, splits):
    rows = []
    for lab, c, ex in items:
        res, tt, sm = run2(S, c, splits, ex)
        how = res["orders"]["how"][res["filled"]]
        mk = int(np.sum(how != "THEN_TAKER") if len(how) and c.maker_level else 0)
        rows.append(A.perf_row(lab, sm) + [mk])
    return rows


def cmd_dev() -> None:
    S = load_msim()
    L = ["# Strategies A-H on TRAIN + VALIDATION (one execution model; $5 orders; c/share net of fees)\n"]
    hdr = A.PERF_HDR + ["maker fills"]
    for sp, lab in (((0, 1), "TRAIN + VALIDATION"), ((0,), "TRAIN"), ((1,), "VALIDATION")):
        L.append(f"\n## {lab}\n"); L.append(A.md_table(hdr, perf_rows(S, strategy_set(), sp)))
    # delay curve for the model-side taker, several edge thresholds
    L.append("\n## Delayed taker: decision delay x minimum net edge (TRAIN + VALIDATION, 3 s exit)\n")
    rows = []
    for d in (0, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5):
        r = [f"+{d:g} s"]
        for me in (0.0, 0.01, 0.02, 0.03, 0.05, 0.10):
            _, _, sm = run2(S, MCfg(model="D_twap", tau=-3.0, delay=float(d), min_edge=me), (0, 1), E3)
            r.append(f"{sm.get('c_share', float('nan')):+.2f} ({sm.get('trades', 0)})")
        rows.append(r)
    L.append(A.md_table(["delay", "edge >= 0c", ">= 1c", ">= 2c", ">= 3c", ">= 5c", ">= 10c"], rows))
    L.append("\nSame with no edge gate, direction only, spread <= 3c:\n")
    rows = []
    for d in (0, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5):
        r = [f"+{d:g} s"]
        for h in (1.0, 3.0, 10.0, 30.0):
            _, _, sm = run2(S, MCfg(model="D_twap", tau=-3.0, delay=float(d), max_spread=0.03), (0, 1), ("t", h))
            r.append(f"{sm.get('c_share', float('nan')):+.2f} ({sm.get('trades', 0)})")
        _, _, sm = run2(S, MCfg(model="D_twap", tau=-3.0, delay=float(d), max_spread=0.03), (0, 1), ("settle",))
        r.append(f"{sm.get('c_share', float('nan')):+.2f} ({sm.get('trades', 0)})")
        rows.append(r)
    L.append(A.md_table(["delay", "1 s", "3 s", "10 s", "30 s", "settlement"], rows))
    # maker grid
    L.append("\n## Resting maker: level x resting time (model side, post at the open, TRAIN + VALIDATION, 3 s exit)\n")
    rows = []
    for lvl in ("bid", "bid+1", "bid+2", "fair-2", "fair-5", "fair-10"):
        r = [lvl]
        for rest in (0.25, 0.5, 1.0, 2.0, 3.0):
            res, tt, sm = run2(S, MCfg(model="D_twap", tau=-3.0, maker_level=lvl, rest=rest), (0, 1), E3)
            r.append(f"{sm.get('c_share', float('nan')):+.2f} ({sm.get('trades', 0)}/{sm['orders']})")
        rows.append(r)
    L.append(A.md_table(["level", "rest 0.25 s", "0.5 s", "1 s", "2 s", "3 s"], rows))
    L.append("\nSame, hazard x5 (traded-through fills five times as likely) and posting 1 s after the open:\n")
    rows = []
    for lvl in ("bid", "bid+1", "fair-5"):
        r = [lvl]
        for rest in (0.5, 1.0, 3.0):
            _, _, sm = run2(S, MCfg(model="D_twap", tau=-3.0, maker_level=lvl, rest=rest, hazard=5 * HAZARD_PER_S), (0, 1), E3)
            r.append(f"{sm.get('c_share', float('nan')):+.2f} ({sm.get('trades', 0)})")
        for rest in (0.5, 1.0, 3.0):
            _, _, sm = run2(S, MCfg(model="D_twap", tau=-3.0, maker_level=lvl, rest=rest, delay=1.0), (0, 1), E3)
            r.append(f"{sm.get('c_share', float('nan')):+.2f} ({sm.get('trades', 0)})")
        rows.append(r)
    L.append(A.md_table(["level", "x5 hazard 0.5 s", "1 s", "3 s", "post +1 s: 0.5 s", "1 s", "3 s"], rows))
    # both sides and pair cost
    L.append("\n## Both sides and the pair cost (TRAIN + VALIDATION)\n")
    rows = []
    for lab, c in (("model side", MCfg(model="D_twap", tau=-3.0, delay=1.0, min_edge=0.0)),
                   ("best EV of the two sides", MCfg(model="D_twap", tau=-3.0, delay=1.0, min_edge=0.0, side_rule="ev")),
                   ("UP only", MCfg(model="D_twap", tau=-3.0, delay=1.0, min_edge=0.0, side_rule="UP")),
                   ("DOWN only", MCfg(model="D_twap", tau=-3.0, delay=1.0, min_edge=0.0, side_rule="DOWN"))):
        _, _, sm = run2(S, c, (0, 1), E3)
        rows.append(A.perf_row(lab, sm))
    L.append(A.md_table(A.PERF_HDR, rows))
    D = S.D
    r3 = np.where((D.split <= 1) & (D.tau == -3.0))[0]
    prs = []
    for s in (-3, 0, 0.25, 0.5, 1, 2, 3, 5):
        pc = D.price("ua", r3, np.full(len(r3), float(s))) + D.price("da", r3, np.full(len(r3), float(s)))
        ok = ~np.isnan(pc)
        prs.append([f"{s:+g} s", int(ok.sum()), float(np.median(pc[ok])), float(np.percentile(pc[ok], 10)), float(np.mean(pc[ok] < 1.0)),
                    float(np.mean(pc[ok] < 0.97))])
    L.append("\nUP ask + DOWN ask (the cost of buying both sides; a pair bought under 1.00 pays 1.00 at settlement, less two fees ~3.5c):\n")
    L.append(A.md_table(["offset", "n", "median", "p10", "P(sum < 1.00)", "P(sum < 0.97)"], prs))
    # sizes
    L.append("\n## Order size (delayed taker +1 s, edge >= 0c, spread <= 3c; TRAIN + VALIDATION)\n")
    rows = []
    for u in SIZES_USD:
        _, _, sm = run2(S, MCfg(model="D_twap", tau=-3.0, delay=1.0, min_edge=0.0, max_spread=0.03, usd=u), (0, 1), E3)
        rows.append(A.perf_row(f"${u:g}", sm))
    L.append(A.md_table(A.PERF_HDR, rows))
    L.append("\n## Recovery score: TRAIN fit\n")
    L.append("OLS of the 3 s bid markout of a model-side taker buy decided 1 s after the open on: " + ", ".join(S.SCORE_NAMES) + ".")
    L.append(f"Standardised coefficients: {np.round(S.score_beta[1:], 4).tolist()}; TRAIN in-sample correlation {S.score_train_corr:.3f}; "
             f"quantiles (10/25/50/75/90): {np.round(S.score_q * 100, 2).tolist()} c.")
    (OUT / "dev_strategies.md").write_text("\n".join(L))
    print("\n".join(L))


def load_msim(final: bool = False) -> MSim:
    D = A.Data()
    P = dict(np.load(A.OUT / ("preds_final.npz" if final else "preds.npz")))
    rng = np.random.default_rng(99)
    P["R_random"] = np.where(rng.random(len(D.y)) < 0.5, 0.6, 0.4)
    return MSim(D, P)


def cmd_markouts() -> None:
    """Executable markout after every fill (bid VWAP-able later minus the fill price), taker vs maker, by side,
    asset, spread/depth/confidence bucket. TRAIN + VALIDATION. Writes results/open_micro_markouts.csv."""
    import csv
    S = load_msim(); D = S.D
    H = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0)
    items = [("taker +0 s (no gate)", MCfg(model="D_twap", tau=-3.0, win=0.0)),
             ("taker +1 s (no gate, spread<=3c)", MCfg(model="D_twap", tau=-3.0, delay=1.0, max_spread=0.03)),
             ("taker +1 s edge>=2c", MCfg(model="D_twap", tau=-3.0, delay=1.0, min_edge=0.02)),
             ("maker at bid, rest 1 s", MCfg(model="D_twap", tau=-3.0, maker_level="bid", rest=1.0)),
             ("maker at bid, rest 3 s", MCfg(model="D_twap", tau=-3.0, maker_level="bid", rest=3.0)),
             ("maker fair-5, rest 2 s", MCfg(model="D_twap", tau=-3.0, maker_level="fair-5", rest=2.0))]
    recs = []
    L = ["# Post-fill executable markouts (bid at fill + h minus fill price, cents; TRAIN + VALIDATION)\n"]
    for lab, c in items:
        res = simulate2(S, c, (0, 1), [E3])
        o, f = res["orders"], res["filled"]
        r, up = o["rows"][f], o["up"][f]
        fill_px, t0 = o["vw_in"][f], o["t_arr"][f]
        mo = {h: S.side_px(r, up, t0 + h, "bid") - fill_px for h in H}
        conf = np.abs(2 * S.P["D_twap"][r] - 1)
        sp = o["ask_d"][f] - o["bid_d"][f]
        dep = o["dep_d"][f]
        for i in range(len(r)):
            recs.append({"strategy": lab, "asset": ASSETS[D.asset[r[i]]], "epoch": int(D.epoch[r[i]]), "side": "UP" if up[i] else "DOWN",
                         "how": o["how"][f][i], "fill": round(float(fill_px[i]), 4), "spread_at_decision": round(float(sp[i]), 2),
                         "depth2_at_decision": float(dep[i]), "confidence": round(float(conf[i]), 3),
                         **{f"mo_{h}": (None if x != x else round(float(x) * 100, 2)) for h, x in ((h, mo[h][i]) for h in H)}})
        def line(name, m):
            if m.sum() < 5:
                return [name, int(m.sum())] + ["—"] * len(H)
            return [name, int(m.sum())] + [f"{np.nanmean(mo[h][m]) * 100:+.2f}" for h in H]
        rows = [line("all", np.ones(len(r), bool)), line("UP", up), line("DOWN", ~up)]
        for a in range(len(ASSETS)):
            rows.append(line(ASSETS[a], D.asset[r] == a))
        rows += [line("spread <= 2c", sp <= 0.0201), line("spread > 2c", sp > 0.0201),
                 line("depth2 < 50", dep < 50), line("depth2 >= 50", dep >= 50),
                 line("confidence < 0.2", conf < 0.2), line("confidence >= 0.2", conf >= 0.2)]
        L.append(f"\n## {lab}: {len(r)} fills\n")
        L.append(A.md_table(["subset", "fills"] + [f"+{h:g}s" for h in H], rows))
    with open(RES / "open_micro_markouts.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(recs[0].keys())); w.writeheader(); w.writerows(recs)
    (OUT / "dev_markouts.md").write_text("\n".join(L))
    print("\n".join(L))


# ── sweep ──

SWEEP_EXITS = [("t", h) for h in (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0)] + [("settle",), ("tp", 0.03, 30.0), ("sl", 0.03, 10.0),
                                                                            ("mom", 30.0), ("edge", 30.0)]


def sweep_configs() -> list[MCfg]:
    out = []
    for model in ("D_twap", "PRE", "B50", "ADAPT"):
        for delay in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0):
            for me in (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.07, 0.10):
                for side in ("model", "ev"):
                    for ms in (0.02, 0.03, 0.05, 9.0):
                        for sc in (-9.0, 2.0, 3.0):
                            out.append(MCfg(model=model, tau=-3.0, delay=delay, min_edge=me, side_rule=side, max_spread=ms, min_score=sc))
        for lvl in ("bid", "bid+1", "bid+2", "fair-2", "fair-5", "fair-10"):
            for rest in (0.25, 0.5, 1.0, 2.0, 3.0):
                for post in (0.0, 0.25, 0.5, 1.0):
                    for me in (-9.0, 0.0, 0.02):
                        for tt in (0, 1):
                            out.append(MCfg(model=model, tau=-3.0, delay=post, maker_level=lvl, rest=rest, min_edge=me, then_taker=tt))
    return out


_MS: MSim | None = None


def _init():
    global _MS
    _MS = load_msim()


def eval2(S, cfg, splits=(0, 1), exits=SWEEP_EXITS) -> list[dict]:
    res = simulate2(S, cfg, splits, exits)
    o = res["orders"]; osplit = S.D.split[o["rows"]]
    rows = []
    for ex in exits:
        name = A.exit_name(ex)
        tt = A.trade_table(S, res, name)
        if cfg.maker_level:
            mk = np.array([h != "THEN_TAKER" for h in o["how"][res["filled"]]])
            fin = A.fee(S.D.rate[tt["rows"]], tt["entry"]) * mk
            tt["fees"] -= tt["qty"] * fin; tt["net"] += tt["qty"] * fin; tt["net_c"] += fin * 100.0
        tsplit = S.D.split[tt["rows"]]
        for sp in splits:
            m = tsplit == sp
            sm = A.summarize({k: v[m] for k, v in tt.items()}, int(np.sum(osplit == sp)))
            sm["win_rate"] = sm.pop("win", float("nan"))
            sub = {k: v[m] for k, v in tt.items()}
            if sm.get("trades"):
                by_a = np.bincount(sub["asset"], weights=sub["net"], minlength=7); by_s = [sub["net"][sub["up"].astype(bool)].sum(), sub["net"][~sub["up"].astype(bool)].sum()]
                sm["top_asset_share"] = float(np.max(np.abs(by_a)) / max(np.sum(np.abs(by_a)), 1e-9))
                sm["up_share"] = float(np.mean(sub["up"]))
                sm["net_wo_best"] = float(sub["net"].sum() - sub["net"].max())
                sm["gross_c"] = float(np.mean((sub["exit"] - sub["entry"]) * 100))
            rows.append({**{f: getattr(cfg, f) for f in MCFG_FIELDS}, "exit": name, "split": RB.SPLIT_NAMES[sp], **sm})
    return rows


def _chunk(cfgs):
    out = []
    for c in cfgs:
        out += eval2(_MS, c)
    return out


SUM2 = ("orders", "trades", "fill", "win_rate", "avg_entry", "avg_exit", "hold_s", "gross", "fees", "slip", "net", "net_trade",
        "c_share", "t", "dd", "sharpe_day", "shares", "top_asset_share", "up_share", "net_wo_best", "gross_c")


def cmd_sweep(procs: int = 3, chunk: int = 40) -> None:
    import csv
    from concurrent.futures import ProcessPoolExecutor
    cfgs = sweep_configs()
    parts = [cfgs[i:i + chunk] for i in range(0, len(cfgs), chunk)]
    rows = []
    with ProcessPoolExecutor(procs, initializer=_init) as pool:
        for k, r in enumerate(pool.map(_chunk, parts)):
            rows += r
            if k % 25 == 0:
                print(f"  {min((k + 1) * chunk, len(cfgs))}/{len(cfgs)} configs", flush=True)
    fields = list(MCFG_FIELDS) + ["exit", "split"] + list(SUM2)
    with open(RES / "open_micro_sweep.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    print(f"  wrote {len(rows)} rows")



# ─────────────────────────────── lock (TRAIN/VALIDATION only) and final (TEST + FORWARD) ───────────────────────────────

LOCK = RES / "open_micro_locked.json"
KEY2 = ("model", "delay", "min_edge", "side_rule", "max_spread", "min_score", "maker_level", "rest", "then_taker", "exit")
NEIGH = {"delay": (0.0, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0), "min_edge": (-9.0, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.07, 0.10),
         "max_spread": (0.02, 0.03, 0.05, 9.0), "rest": (0.25, 0.5, 1.0, 2.0, 3.0), "min_score": (-9.0, 2.0, 3.0)}


def read_sweep():
    import csv
    out = []
    with open(RES / "open_micro_sweep.csv") as fh:
        for r in csv.DictReader(fh):
            for k, v in r.items():
                if k in ("model", "entry", "lat", "sides", "exit", "split", "side_rule", "maker_level"):
                    continue
                r[k] = float(v) if v not in ("", None) else float("nan")
            out.append(r)
    return out


def defenses(tr: dict, va: dict | None) -> list[str]:
    """Why a configuration is rejected (empty = passes). TRAIN and VALIDATION rows of the sweep."""
    why = []
    if tr["trades"] < 30: why.append("few trades")
    if not tr["c_share"] > 0: why.append("TRAIN <= 0")
    if not tr["t"] >= 1.5: why.append("TRAIN t < 1.5")
    if not tr.get("gross_c", 0) > 0: why.append("gross <= 0")
    if tr.get("top_asset_share", 1) > 0.6: why.append("one asset > 60 % of |P&L|")
    if not 0.2 <= tr.get("up_share", 0.5) <= 0.8: why.append("one side > 80 %")
    if not tr.get("net_wo_best", 0) > 0: why.append("best market carries it")
    if va is None or va["trades"] < 10: why.append("VALIDATION < 10 trades")
    elif not va["c_share"] > 0: why.append("VALIDATION <= 0")
    return why


def cmd_lock() -> None:
    import datetime
    rows = read_sweep()
    idx = {tuple(r[k] for k in KEY2) + (r["split"],): r for r in rows}
    keys = sorted({tuple(r[k] for k in KEY2) for r in rows})
    tr_rows = [(k, idx[k + ("train",)], idx.get(k + ("validation",))) for k in keys if k + ("train",) in idx]
    stats = {"configs": len(keys), "train_volume": 0, "train_positive": 0, "pass_defenses": 0, "pass_neighbours": 0}
    surv, near = [], []
    for k, tr, va in tr_rows:
        if tr["trades"] >= 30:
            stats["train_volume"] += 1
            if tr["c_share"] > 0:
                stats["train_positive"] += 1
        why = defenses(tr, va)
        if why:
            if tr["trades"] >= 30 and tr["c_share"] > 0 and va and va["c_share"] > 0:
                near.append((k, tr, va, why))
            continue
        stats["pass_defenses"] += 1
        d = dict(zip(KEY2, k))
        nb = []
        for ax, grid in NEIGH.items():
            if d[ax] not in grid:
                continue
            i = grid.index(d[ax])
            for j in (i - 1, i + 1):
                if 0 <= j < len(grid):
                    nk = tuple(grid[j] if kk == ax else d[kk] for kk in KEY2)
                    r = idx.get(nk + ("train",))
                    if r and r["trades"] >= 30:
                        nb.append(r["c_share"])
        if len(nb) >= 3 and np.median(nb) > 0:
            stats["pass_neighbours"] += 1
            surv.append((min(tr["c_share"], va["c_share"]), k, tr, va, float(np.median(nb))))
    surv.sort(key=lambda x: -x[0])
    locked = [{"label": "survivor", "key": dict(zip(KEY2, k)), "train": tr, "validation": va, "neighbour_median_c": nb}
              for _, k, tr, va, nb in surv[:5]]
    # what the best-looking TRAIN rules do out of sample, whether or not they survived
    for fam, cond in (("best TRAIN taker", lambda k: k[6] == ""), ("best TRAIN maker", lambda k: k[6] != "")):
        best = sorted(((tr["c_share"], k, tr, va) for k, tr, va in tr_rows if tr["trades"] >= 30 and cond(k)), key=lambda x: -x[0])[:2]
        for c, k, tr, va in best:
            locked.append({"label": fam, "key": dict(zip(KEY2, k)), "train": tr, "validation": va, "defenses_failed": defenses(tr, va)})
    out = {"locked_at_utc": datetime.datetime.utcnow().isoformat(timespec="seconds"), "stats": stats, "locked": locked,
           "near_misses": [{"key": dict(zip(KEY2, k)), "train_c": tr["c_share"], "train_trades": tr["trades"],
                            "val_c": va["c_share"], "val_trades": va["trades"], "failed": why} for k, tr, va, why in near[:30]]}
    LOCK.write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps(stats, indent=1))
    for it in locked:
        print(it["label"], it["key"], "TRAIN", int(it["train"]["trades"]), round(it["train"]["c_share"], 2),
              "VAL", int(it["validation"]["trades"]) if it["validation"] else 0, round(it["validation"]["c_share"], 2) if it["validation"] else None,
              it.get("defenses_failed", ""))


def cfg_of(key: dict) -> tuple[MCfg, tuple]:
    ex = next(e for e in A.EXITS + SWEEP_EXITS if A.exit_name(e) == key["exit"])
    return MCfg(model=key["model"], tau=-3.0, delay=key["delay"], min_edge=key["min_edge"], side_rule=key["side_rule"],
                max_spread=key["max_spread"], min_score=key["min_score"], maker_level=key["maker_level"], rest=key["rest"],
                then_taker=int(key["then_taker"])), ex


def stats_block(tt: dict, n_orders: int) -> dict:
    """Item 18: count, mean, median, sd, bootstrap CI (by epoch), profit factor, drawdown, worst/best market & asset."""
    D = A.Data.__new__(A.Data)  # not used
    if not len(tt["net"]):
        return {"trades": 0}
    sm = A.summarize(tt, n_orders)
    lo, hi = A.boot_ci(tt["net_c"], tt["epoch"])
    wins = tt["net"][tt["net"] > 0].sum(); losses = -tt["net"][tt["net"] < 0].sum()
    ep = {}
    for e, a, v in zip(tt["epoch"], tt["asset"], tt["net"]):
        ep[(int(e), ASSETS[a])] = ep.get((int(e), ASSETS[a]), 0.0) + v
    by_asset = {ASSETS[a]: float(tt["net"][tt["asset"] == a].sum()) for a in range(len(ASSETS)) if (tt["asset"] == a).any()}
    worst = min(ep.items(), key=lambda x: x[1]); best = max(ep.items(), key=lambda x: x[1])
    return {**sm, "median_c": float(np.median(tt["net_c"])), "sd_c": float(np.std(tt["net_c"])), "ci95_c": [lo, hi],
            "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
            "worst_market": f"{worst[0][1]}-{worst[0][0]} ${worst[1]:+.2f}", "best_market": f"{best[0][1]}-{best[0][0]} ${best[1]:+.2f}",
            "worst_asset": min(by_asset.items(), key=lambda x: x[1]), "best_asset": max(by_asset.items(), key=lambda x: x[1]),
            "up_share": float(np.mean(tt["up"]))}


def robustness2(S, cfg, ex, splits, seed=7):
    """A.robustness with the extended entry (delay, side rule, maker)."""
    D = S.D
    res, tt, sm = run2(S, cfg, splits, ex); n = len(res["orders"]["rows"])
    rows = [A.perf_row("base", sm)]
    for k in (1.25, 1.5, 2.0):
        rows.append(A.perf_row(f"latency x{k:g}", run2(S, replace(cfg, lat_scale=k), splits, ex)[2]))
    for x in (300.0, 1000.0):
        rows.append(A.perf_row(f"random extra delay 0-{x:.0f} ms per order", run2(S, replace(cfg, lat_extra=x), splits, ex)[2]))
    for d in (0.25, 0.5):
        rows.append(A.perf_row(f"fill rate -{d * 100:.0f} % (random fills dropped)", A.summarize(A.perturbed(tt, D, drop=d, seed=seed), n)))
    rows.append(A.perf_row("best 10 % of fills never happen", A.summarize(A.perturbed(tt, D, drop=0.10, drop_best=True), n)))
    rows.append(A.perf_row("spread +1c (entry +0.5c, exit -0.5c)", A.summarize(A.perturbed(tt, D, 0.005, 0.005), n)))
    rows.append(A.perf_row("slippage +1c on entry", A.summarize(A.perturbed(tt, D, 0.01, 0.0), n)))
    rows.append(A.perf_row("exit 1c worse", A.summarize(A.perturbed(tt, D, 0.0, 0.01), n)))
    rows.append(A.perf_row("everything above at once (x1.5, +1c/+1c, -25 % fills)",
                           A.summarize(A.perturbed(run2(S, replace(cfg, lat_scale=1.5), splits, ex)[1], D, 0.01, 0.01, drop=0.25, seed=seed), n)))
    rng = np.random.default_rng(seed)
    for sd in (0.25, 0.5, 1.0):
        S2 = MSim.__new__(MSim); S2.__dict__.update(S.__dict__); S2.P = dict(S.P)
        S2.P[cfg.model] = A.sigm(A.logit(S.P[cfg.model]) + rng.normal(0.0, sd, len(S.P[cfg.model])))
        rows.append(A.perf_row(f"prediction noise (logit sd {sd:g})", run2(S2, cfg, splits, ex)[2]))
    return A.md_table(A.PERF_HDR, rows)


def cmd_final() -> None:
    lk = json.loads(LOCK.read_text())
    S = load_msim(final=True); D = S.D
    items = []
    for i, it in enumerate(lk["locked"]):
        c, e = cfg_of(it["key"])
        k = it["key"]
        items.append((f"L{i + 1} {it['label']}: {k['model']} delay {k['delay']:g}s edge {k['min_edge']:g} {k['side_rule']} spread<={k['max_spread']:g} "
                      f"score {k['min_score']:g} maker '{k['maker_level']}' rest {k['rest']:g} then_taker {k['then_taker']:g}, {k['exit']}", c, e))
    items += strategy_set()
    L = ["# Final: TEST and FORWARD (read once, after the lock)\n"]
    has_fwd = bool(np.any(D.split == 3))
    for sp, lab in ((2, "TEST"), (3, "FORWARD")):
        if sp == 3 and not has_fwd:
            continue
        L.append(f"\n## {lab}: all strategies\n")
        L.append(A.md_table(A.PERF_HDR + ["maker fills"], perf_rows(S, items, (sp,))))
    L.append("\n## Item 18 statistics (TEST) for the locked and pre-registered rules\n")
    rows = []
    for lab, c, e in items:
        res, tt, sm = run2(S, c, (2,), e)
        st = stats_block(tt, len(res["orders"]["rows"]))
        if not st.get("trades"):
            rows.append([lab, 0] + ["—"] * 11); continue
        rows.append([lab, st["trades"], st["c_share"], st["median_c"], st["sd_c"], f"[{st['ci95_c'][0]:+.2f}, {st['ci95_c'][1]:+.2f}]",
                     st["profit_factor"], f"${st['dd']:.2f}", st["worst_market"], st["best_market"],
                     f"{st['worst_asset'][0]} ${st['worst_asset'][1]:+.2f}", f"{st['best_asset'][0]} ${st['best_asset'][1]:+.2f}", st["up_share"]])
    L.append(A.md_table(["strategy", "trades", "mean c", "median c", "sd c", "95 % CI (epoch bootstrap)", "profit factor", "max DD",
                         "worst market", "best market", "worst asset", "best asset", "UP share"], rows))
    # chronological stability for the locked rules + P3-equivalent
    L.append("\n## Chronological stability (walk-forward folds of TRAIN+VALIDATION, then TEST, then FORWARD)\n")
    for lab, c, e in items[:min(5, len(lk["locked"]))] + [items[len(lk["locked"]) + 6]]:
        L.append(f"\n{lab}:\n")
        rows = []
        eps = np.unique(D.epoch[D.split <= 1]); cuts = np.array_split(eps, 5)
        res, tt, _ = run2(S, c, (0, 1, 2, 3), e); o = res["orders"]
        for k, cc in enumerate(cuts):
            m = (tt["epoch"] >= cc[0]) & (tt["epoch"] <= cc[-1]); om = (D.epoch[o["rows"]] >= cc[0]) & (D.epoch[o["rows"]] <= cc[-1])
            rows.append(A.perf_row(f"fold {k + 1}", A.summarize({kk: v[m] for kk, v in tt.items()}, int(om.sum()))))
        for sp, nm in ((2, "TEST"), (3, "FORWARD")):
            m = D.split[tt["rows"]] == sp; om = D.split[o["rows"]] == sp
            rows.append(A.perf_row(nm, A.summarize({kk: v[m] for kk, v in tt.items()}, int(om.sum()))))
        L.append(A.md_table(A.PERF_HDR, rows))
    # robustness on TEST for the top locked rule and the delayed-taker hypothesis
    for lab, c, e in items[:1] + [items[len(lk["locked"]) + 3], items[len(lk["locked"]) + 6], items[len(lk["locked"]) + 7]]:
        L.append(f"\n## Robustness on TEST: {lab}\n")
        L.append(robustness2(S, c, e, (2,)))
        L.append(f"\n## Robustness on TRAIN + VALIDATION: {lab}\n")
        L.append(robustness2(S, c, e, (0, 1)))
    # by asset / side / regime on TEST for the delayed taker and the top locked rule
    for lab, c, e in items[:1] + [items[len(lk["locked"]) + 3], items[len(lk["locked"]) + 7]]:
        res, tt, sm = run2(S, c, (2,), e)
        b = A.breakdowns(S, tt, len(res["orders"]["rows"]), D)
        L.append(f"\n## TEST breakdowns: {lab}\n"); L.append(b["asset"]); L.append(""); L.append(b["side"]); L.append("")
        L.append(A.regime_table(S, tt, len(res["orders"]["rows"])))
    # prediction timestamp
    L.append("\n## TEST: prediction time (delayed taker +1 s, edge >= 0c, spread <= 3c, 3 s exit)\n")
    rows = []
    for tau in (-10.0, -5.0, -3.0, -2.0, -1.0, 0.0):
        _, _, sm = run2(S, MCfg(model="D_twap", tau=tau, delay=1.0, min_edge=0.0, max_spread=0.03), (2,), E3)
        rows.append(A.perf_row(f"T{tau:+g}s", sm))
    L.append(A.md_table(A.PERF_HDR, rows))
    (OUT / "final_report.md").write_text("\n".join(L))
    print("\n".join(L))
    # machine-readable: trade-level, market-level and walk-forward rows for every strategy above, all periods
    import csv
    trades, markets, wf = [], [], []
    eps = np.unique(D.epoch[D.split <= 1]); cuts = np.array_split(eps, 5)
    for lab, c, e in items:
        res, tt, _ = run2(S, c, (0, 1, 2, 3), e); o = res["orders"]
        for i in range(len(tt["net"])):
            r = tt["rows"][i]
            trades.append({"strategy": lab, "asset": ASSETS[tt["asset"][i]], "epoch": int(tt["epoch"][i]), "split": RB.SPLIT_NAMES[D.split[r]],
                           "side": "UP" if tt["up"][i] else "DOWN", "decision_s": round(float(tt["e"][i]), 3), "fill_s": round(float(tt["t_arr"][i]), 3),
                           "entry": round(float(tt["entry"][i]), 4), "exit": round(float(tt["exit"][i]), 4), "qty": round(float(tt["qty"][i]), 2),
                           "hold_s": round(float(tt["hold"][i]), 3), "gross": round(float(tt["gross"][i]), 4), "fees": round(float(tt["fees"][i]), 4),
                           "net": round(float(tt["net"][i]), 4), "net_c": round(float(tt["net_c"][i]), 3), "settled": bool(tt["settled"][i])})
        for sp in (0, 1, 2, 3):
            m = D.split[tt["rows"]] == sp; om = D.split[o["rows"]] == sp
            sm = A.summarize({kk: v[m] for kk, v in tt.items()}, int(om.sum()))
            markets.append({"strategy": lab, "period": RB.SPLIT_NAMES[sp], **{k: sm.get(k) for k in A.SUM_FIELDS if k in sm}})
        for k, cc in enumerate(cuts):
            m = (tt["epoch"] >= cc[0]) & (tt["epoch"] <= cc[-1]); om = (D.epoch[o["rows"]] >= cc[0]) & (D.epoch[o["rows"]] <= cc[-1])
            sm = A.summarize({kk: v[m] for kk, v in tt.items()}, int(om.sum()))
            wf.append({"strategy": lab, "fold": k + 1, "epoch_from": int(cc[0]), "epoch_to": int(cc[-1]), **{kk: sm.get(kk) for kk in A.SUM_FIELDS if kk in sm}})
    for name, rows in (("open_micro_trades.csv", trades), ("open_micro_market_results.csv", markets), ("open_micro_walkforward.csv", wf)):
        with open(RES / name, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list({k: 1 for r in rows for k in r})); w.writeheader(); w.writerows(rows)


# ─────────────────────────────── figures ───────────────────────────────


def cmd_figures() -> None:
    import csv
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    FIGS.mkdir(parents=True, exist_ok=True)
    C = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a", "yellow": "#eda100", "magenta": "#e87ba4", "green": "#008300",
         "violet": "#4a3aa7", "red": "#e34948", "ink": "#0b0b0b", "muted": "#898781", "grid": "#e1e0d9"}
    order = ["blue", "orange", "aqua", "yellow", "magenta", "green", "violet", "red"]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.edgecolor": "#c3c2b7", "axes.grid": True, "grid.color": C["grid"], "grid.linewidth": 0.8,
                         "figure.facecolor": "white", "axes.facecolor": "#fcfcfb"})
    liq = json.load(open(OUT / "liquidity_curve.json")); tl = json.load(open(OUT / "tick_liquidity.json"))
    sh = json.load(open(OUT / "opening_shock.json"))

    def save(fig, name):
        fig.tight_layout(); fig.savefig(FIGS / name, dpi=150); plt.close(fig)

    # 1 spread vs ms after the open, per asset (archive) + event-level medians (tick recording)
    fig, ax = plt.subplots(figsize=(9, 4.6))
    off = [r for r in liq["by_offset"] if r["side"] == "UP"]
    x = [r["offset_s"] * 1000 for r in off]
    for i, a in enumerate(ASSETS):
        ax.plot(x, [r[f"spread_p50_{a}"] for r in off], color=C[order[i]], lw=2, label=a)
    ax.axvline(0, color=C["muted"], lw=1); ax.set_xlim(-3000, 5000); ax.set_xlabel("ms from the open"); ax.set_ylabel("median spread, cents")
    ax.set_title("1. Spread after the open, by asset (UP token, archive, TRAIN+VALIDATION)"); ax.legend(ncol=4, frameon=False)
    save(fig, "01_spread_vs_ms.png")
    # 2 depth vs ms
    fig, ax = plt.subplots(figsize=(9, 4.6))
    for i, a in enumerate(ASSETS):
        bk = [r for r in liq["buckets"] if r["asset"] == a and r["side"] == "UP"]
        ax.plot([(r["lo_ms"] + r["hi_ms"]) / 2 for r in bk], [r["depth2_p50"] for r in bk], color=C[order[i]], lw=2, marker="o", ms=4, label=a)
    ax.set_xscale("log"); ax.set_xlabel("ms after the open (bucket centre, log)"); ax.set_ylabel("median ask depth within 2 ticks, shares")
    ax.set_title("2. Depth after the open, by asset (archive buckets)"); ax.legend(ncol=4, frameon=False)
    save(fig, "02_depth_vs_ms.png")
    # 3 opening shock distribution
    snap = {}
    with open(RES / "open_micro_snapshots.csv") as fh:
        for r in csv.DictReader(fh):
            if r["split"] in ("train", "validation") and r["t_s"] in ("0", "1", "3") and r["p_up_preopen"] and r["up_ask"]:
                snap.setdefault(r["t_s"], []).append((float(r["up_ask"]) - float(r["p_up_preopen"])) * 100)
    fig, ax = plt.subplots(figsize=(9, 4.6))
    for k, (t, col) in enumerate((("0", "blue"), ("1", "orange"), ("3", "aqua"))):
        ax.hist(np.clip(snap[t], -30, 60), bins=90, range=(-30, 60), histtype="step", lw=2, color=C[col], label=f"UP ask at +{t} s minus pre-open fair")
    ax.axvline(0, color=C["muted"], lw=1); ax.set_xlabel("cents"); ax.set_ylabel("markets"); ax.legend(frameon=False)
    ax.set_title("3. Opening shock: executable ask minus the last tight pre-open mid (TRAIN+VALIDATION)")
    save(fig, "03_opening_shock.png")
    # 4 markout curves
    mo = {}
    with open(RES / "open_micro_markouts.csv") as fh:
        for r in csv.DictReader(fh):
            mo.setdefault(r["strategy"], []).append([float(r[f"mo_{h}"]) if r[f"mo_{h}"] else np.nan for h in (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0)])
    fig, ax = plt.subplots(figsize=(9, 4.6))
    hs = [0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30]
    for k, name in enumerate(mo):
        ax.plot(hs, np.nanmean(np.array(mo[name]), 0), color=C[order[k % 8]], lw=2, marker="o", ms=4, label=f"{name} (n={len(mo[name])})")
    ax.axhline(0, color=C["muted"], lw=1); ax.set_xscale("log"); ax.set_xlabel("seconds after the fill (log)"); ax.set_ylabel("mean bid minus fill, cents (before exit fee)")
    ax.set_title("4. Post-fill executable markout, taker vs maker (TRAIN+VALIDATION)"); ax.legend(frameon=False, fontsize=8)
    save(fig, "04_markout_curves.png")
    # 5 maker vs taker, 8 by asset, 9 by holding, 10 by prediction time: from the simulator
    S = load_msim(); D = S.D
    fig, ax = plt.subplots(figsize=(9, 4.6))
    labs, vals, errs = [], [], []
    for lab, c in (("taker +0 s, edge 2c", MCfg(model="D_twap", tau=-3.0, win=0.0, min_edge=0.02)), ("taker +0.5 s, edge 2c", MCfg(model="D_twap", tau=-3.0, delay=0.5, min_edge=0.02)),
                   ("taker +1 s, edge 2c", MCfg(model="D_twap", tau=-3.0, delay=1.0, min_edge=0.02)), ("taker 0-3 s, edge 2c", MCfg(model="D_twap", tau=-3.0, win=3.0, min_edge=0.02)),
                   ("maker bid, 1 s", MCfg(model="D_twap", tau=-3.0, maker_level="bid", rest=1.0)), ("maker bid, 3 s", MCfg(model="D_twap", tau=-3.0, maker_level="bid", rest=3.0)),
                   ("maker bid+1, 1 s", MCfg(model="D_twap", tau=-3.0, maker_level="bid+1", rest=1.0)), ("maker fair-5, 2 s", MCfg(model="D_twap", tau=-3.0, maker_level="fair-5", rest=2.0))):
        _, tt, sm = run2(S, c, (0, 1), E3)
        labs.append(f"{lab}\n(n={sm.get('trades', 0)})"); vals.append(sm.get("c_share", 0.0)); lo, hi = A.boot_ci(tt["net_c"], tt["epoch"]) if sm.get("trades") else (0, 0); errs.append([vals[-1] - lo, hi - vals[-1]])
    ax.bar(range(len(vals)), vals, color=[C["blue"]] * 4 + [C["orange"]] * 4, width=0.6)
    ax.errorbar(range(len(vals)), vals, yerr=np.array(errs).T, fmt="none", ecolor=C["ink"], capsize=3, lw=1)
    ax.set_xticks(range(len(vals))); ax.set_xticklabels(labs, fontsize=8, rotation=25, ha="right"); ax.axhline(0, color=C["muted"], lw=1); ax.set_ylabel("net c/share (95 % CI)")
    ax.set_title("5. Maker vs taker entry, model side, 3 s exit (TRAIN+VALIDATION)")
    save(fig, "05_maker_vs_taker.png")
    # 6 pre-open fair vs executable price at +1 s
    r3 = np.where((D.split <= 1) & (D.tau == -3.0))[0]
    fair, _, _ = _preopen_fair(D, D.g[r3]); ua1 = D.price("ua", r3, np.full(len(r3), 1.0)); ub1 = D.price("ub", r3, np.full(len(r3), 1.0))
    ok = ~np.isnan(fair) & ~np.isnan(ua1) & ~np.isnan(ub1) & (ua1 - ub1 <= 0.03)
    fig, ax = plt.subplots(figsize=(6.4, 6))
    ax.scatter(fair[ok], ua1[ok], s=6, alpha=0.35, color=C["blue"], label="UP ask at +1 s (spread <= 3c)")
    ax.plot([0, 1], [0, 1], color=C["muted"], lw=1); ax.set_xlabel("pre-open fair (last tight pre-open mid)"); ax.set_ylabel("executable UP ask 1 s after the open")
    ax.set_title(f"6. Pre-open fair value vs executable price (n={int(ok.sum())})"); ax.legend(frameon=False)
    save(fig, "06_preopen_vs_executable.png")
    # 7 expected edge vs actual P&L
    res = simulate2(S, MCfg(model="D_twap", tau=-3.0, win=3.0, min_edge=-9.0, max_spread=0.05), (0, 1), [E3])
    tt = A.trade_table(S, res, A.exit_name(E3)); ed = res["orders"]["edge_d"][res["filled"]] * 100
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(ed, tt["net_c"], s=6, alpha=0.3, color=C["blue"])
    bins = np.array([-15, -8, -5, -3, -2, -1, 0, 1, 2, 3, 5, 8, 15])
    for lo, hi in zip(bins[:-1], bins[1:]):
        m = (ed >= lo) & (ed < hi)
        if m.sum() >= 8:
            ax.plot([(lo + hi) / 2], [tt["net_c"][m].mean()], marker="s", color=C["orange"], ms=7)
    ax.axhline(0, color=C["muted"], lw=1); ax.plot([-15, 15], [-15, 15], color=C["muted"], lw=1, ls=":")
    ax.set_xlim(-15, 15); ax.set_ylim(-30, 30); ax.set_xlabel("expected net edge at the decision, cents"); ax.set_ylabel("realised net P&L, c/share (3 s exit)")
    ax.set_title(f"7. Expected edge vs realised P&L (taker 0-3 s, spread <= 5c, n={len(ed)}; squares: bin means)", fontsize=10)
    save(fig, "07_edge_vs_pnl.png")
    # 8 by asset
    fig, ax = plt.subplots(figsize=(9, 4.6))
    for k, (lab, c) in enumerate((("taker +1 s, spread<=3c, no gate", MCfg(model="D_twap", tau=-3.0, delay=1.0, max_spread=0.03)),
                                  ("taker 0-3 s, edge 2c", MCfg(model="D_twap", tau=-3.0, win=3.0, min_edge=0.02)),
                                  ("maker bid, 1 s", MCfg(model="D_twap", tau=-3.0, maker_level="bid", rest=1.0)))):
        _, tt, _ = run2(S, c, (0, 1), E3)
        v = [tt["net_c"][tt["asset"] == a].mean() if (tt["asset"] == a).sum() >= 5 else np.nan for a in range(7)]
        ax.bar(np.arange(7) + (k - 1) * 0.27, v, width=0.27, color=C[order[k]], label=lab)
    ax.set_xticks(range(7)); ax.set_xticklabels(ASSETS); ax.axhline(0, color=C["muted"], lw=1); ax.set_ylabel("net c/share (TRAIN+VALIDATION)")
    ax.set_title("8. Performance by asset (3 s exit; bars omitted under 5 trades)"); ax.legend(frameon=False, fontsize=8)
    save(fig, "08_by_asset.png")
    # 9 by holding period
    fig, ax = plt.subplots(figsize=(9, 4.6))
    holds = [0.25, 0.5, 1, 2, 3, 5, 10, 30]
    for k, (lab, c) in enumerate((("taker +1 s, spread<=3c", MCfg(model="D_twap", tau=-3.0, delay=1.0, max_spread=0.03)),
                                  ("taker 0-3 s, edge 2c", MCfg(model="D_twap", tau=-3.0, win=3.0, min_edge=0.02)),
                                  ("maker bid, 1 s", MCfg(model="D_twap", tau=-3.0, maker_level="bid", rest=1.0)))):
        o = enter2(S, c, A.candidate_rows(S, c, (0, 1)))
        v = []
        for h in holds:
            res = A.simulate(S, c, (0, 1), [("t", h)], orders=o); tt = A.trade_table(S, res, A.exit_name(("t", h)))
            if c.maker_level:
                fin = A.fee(D.rate[tt["rows"]], tt["entry"]); tt["net_c"] += fin * 100
            v.append(tt["net_c"].mean() if len(tt["net_c"]) else np.nan)
        ax.plot(holds, v, marker="o", ms=4, lw=2, color=C[order[k]], label=f"{lab} (n={len(o['rows'][o['qty'] > 0])})")
    ax.set_xscale("log"); ax.axhline(0, color=C["muted"], lw=1); ax.set_xlabel("holding time, s (log)"); ax.set_ylabel("net c/share (TRAIN+VALIDATION)")
    ax.set_title("9. Performance by holding period"); ax.legend(frameon=False, fontsize=8)
    save(fig, "09_by_holding.png")
    # 10 by prediction timestamp
    fig, ax = plt.subplots(figsize=(9, 4.6))
    taus = [-10, -5, -3, -2, -1, 0]
    for k, (lab, kw) in enumerate((("taker +1 s, spread<=3c, no gate", dict(delay=1.0, max_spread=0.03)), ("taker 0-3 s, edge 2c", dict(win=3.0, min_edge=0.02)))):
        v = [run2(S, MCfg(model="D_twap", tau=float(t), **kw), (0, 1), E3)[2].get("c_share", np.nan) for t in taus]
        ax.plot(taus, v, marker="o", ms=5, lw=2, color=C[order[k]], label=lab)
    ax.axhline(0, color=C["muted"], lw=1); ax.set_xlabel("prediction time, s before the open"); ax.set_ylabel("net c/share (TRAIN+VALIDATION)")
    ax.set_title("10. Performance by prediction timestamp"); ax.legend(frameon=False, fontsize=8)
    save(fig, "10_by_prediction_time.png")
    print("figures:", sorted(p.name for p in FIGS.glob("*.png")))


if __name__ == "__main__":
    main()
