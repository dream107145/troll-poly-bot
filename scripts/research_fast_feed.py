"""Fast feed vs the 1 s rule on the tick recording (scripts/record_ticks.py output).

    python scripts/research_fast_feed.py [--ticks 'data/ticks/*.jsonl.gz'] > results/fast_feed_report.md

READ-ONLY research. Replays every raw exchange quote per asset through
  A  current   : composite = median of the exchanges; |1 s return| >= k sigma, 3 s agrees, accelerating
  B  lead      : the same gates on the lead composite (sources dated by their measured lag, 30 ms window)
  C  fast-250  : FastMoveDetector (50/100/250/500 ms horizons) fires; engine gate = 250 ms return >= k sigma
                 (sqrt-scaled) with the 1 s return in the same direction, on the lead composite
  D  fast-500  : as C with the 500 ms window
  E  fast-1s   : detector fires; the 1 s gate of A on the lead composite (the pure trigger-timing effect)
and for every signal the order the live bot would send: FAK up to 10 shares at min(ask + 2c, 0.70), judged
against the VENUE book (venue stamp <= arrival) 135 ms after detection (the live bot's measured 14 ms
presend + half of its 243 ms round trip); exit at +20 s at the bid, take-profit +10c, stop-loss -5c, taker
fees both legs. Gates identical across rules: 0.05 <= ask <= 0.70, spread <= 5c, net edge >= 2c on the TWAP
fair value, 60-295 s left, 1 fill and 3 attempts per market, 2 s per direction.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import gzip
import json
import math
import statistics as st
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np
import orjson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.config import VolConfig                                    # noqa: E402
from troll_poly_bot.features.fastmove import FastMoveDetector                  # noqa: E402
from troll_poly_bot.pricing.twap import TwapState, twap_fair_value            # noqa: E402
from troll_poly_bot.pricing.vol import EwmaVol, TwoScaleVol                    # noqa: E402

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
K = 3.0
PRESEND_MS, HALF_RTT_MS = 14.0, 121.0
SIZE, MAX_ASK, MIN_ASK, MAX_SPREAD, MIN_EDGE, MAX_SLIP = 10.0, 0.70, 0.05, 0.05, 0.02, 0.02
HOLD_MS, TP, SL = 20_000.0, 0.10, 0.05
fee = lambda p: 0.07 * p * (1 - p)  # noqa: E731


def read(paths):
    quotes, trades, books, meta = defaultdict(list), defaultdict(list), defaultdict(list), {}
    lag = defaultdict(list)
    for path in paths:
        try:
            with gzip.open(path, "rb") as fh:
                for line in fh:
                    try:
                        r = orjson.loads(line)
                    except orjson.JSONDecodeError:
                        continue
                    k = r["k"]
                    if k == "q":
                        if r["b"] and r["a"]:
                            quotes[r["s"]].append((r["rt"], r["x"], r["b"], r["a"]))
                    elif k == "t":
                        trades[r["s"]].append(r["rt"])
                        if r.get("et"):
                            lag[r["x"]].append(r["rt"] - r["et"])
                    elif k == "b":
                        if r["bb"] is not None and r["ba"] is not None:
                            books[r["tok"]].append((r["vt"] or r["rt"], r["rt"], r["bb"], r["ba"], r["bbs"], r["bas"],
                                                    r["bd2"], r["ad2"]))
                    elif k == "m":
                        meta[r["slug"]] = r
        except (EOFError, OSError, zlib.error):
            pass
    for a in quotes:
        quotes[a].sort(key=lambda q: q[0])
        trades[a].sort()
    return quotes, trades, {t: np.array(v, dtype=float) for t, v in books.items()}, meta, \
        {x: float(np.median(v)) for x, v in lag.items()}


def make_vol():
    v = VolConfig()
    return TwoScaleVol(fast=EwmaVol(grid_ms=v.grid_ms, halflife_s=v.halflife_s, floor_per_sec=v.floor_per_sec,
                                    ceil_per_sec=v.ceil_per_sec),
                       slow=EwmaVol(grid_ms=v.slow_grid_ms, halflife_s=v.slow_halflife_s,
                                    floor_per_sec=v.floor_per_sec, ceil_per_sec=v.ceil_per_sec),
                       default_ratio=v.default_ratio)


def composite_series(qs, lag_ms, lead_window_ms):
    """(t, plain median, lead price) at every quote, with a static basis per exchange."""
    last = {}
    ratios = defaultdict(list)
    for t, x, b, a in qs:
        m = 0.5 * (b + a)
        last[x] = (t, m)
        if x != "bn" and "bn" in last and t - last["bn"][0] < 200.0:
            ratios[x].append(math.log(m / last["bn"][1]))
    basis = {x: (st.median(v) if v else 0.0) for x, v in ratios.items()}
    basis["bn"] = 0.0
    last = {}
    T, P, L = [], [], []
    for t, x, b, a in qs:
        last[x] = (t, 0.5 * (b + a) * math.exp(-basis.get(x, 0.0)))
        fresh = [(tt, xx, mm) for xx, (tt, mm) in last.items() if t - tt <= 3000.0]
        prices = [mm for _, _, mm in fresh]
        med = st.median(prices)
        lead = med
        if len(fresh) > 1:
            info = [tt - lag_ms.get(xx, 0.0) for tt, xx, _ in fresh]
            newest = max(info)
            sel = [p for p, ti in zip(prices, info) if ti >= newest - lead_window_ms]
            lp = st.median(sel)
            lead = lp if abs(lp - med) / med * 1e4 <= 25.0 else med
        T.append(t); P.append(med); L.append(lead)
    return np.array(T), np.array(P), np.array(L)


RULES = ("A current", "B lead", "C fast-250", "D fast-500", "E fast-1s")


def process(path, lead_window, sig_rows, covered_min):
    """One recording at a time: two at once held ~4 GB of book tuples and was OOM-killed."""
    import gc
    quotes, trades, books, meta, lag_ms = read([path])
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
    wins = defaultdict(list)
    for slug, m in meta.items():
        wins[m["s"]].append((m["open"], m["close"], m["up"], m["dn"], slug))
    for a in wins:
        wins[a].sort()
    bk_v = {t: v[np.argsort(v[:, 0], kind="stable")] for t, v in books.items() if len(v)}
    bk_r = {t: v[np.argsort(v[:, 1], kind="stable")] for t, v in books.items() if len(v)}
    kv = {t: v[:, 0].copy() for t, v in bk_v.items()}
    kr = {t: v[:, 1].copy() for t, v in bk_r.items()}

    def book_at(tok, t, venue):
        arr = (bk_v if venue else bk_r).get(tok)
        if arr is None:
            return None
        i = bisect.bisect_right((kv if venue else kr)[tok], t) - 1
        return None if i < 0 else arr[i]

    def window_at(a, t):
        for o, c, up, dn, slug in wins.get(a, []):
            if o <= t < c:
                return o, c, up, dn, slug
        return None

    # minutes actually covered (several recordings days apart: sum the gaps under a minute)
    qb = quotes.get("BTC") or max(quotes.values(), key=len)
    tq = np.array([q[0] for q in qb])
    covered = float(np.sum(np.clip(np.diff(tq), 0.0, 60_000.0))) / 60000.0
    print(f"\n### {Path(path).name}: {covered:.0f} min, {len(meta)} windows, {len(strikes)} with a strike\n")
    # the recording carries no Coinbase trades, so its lag cannot be measured here: use the
    # 2026-09-29 live measurement scaled to this recording's Binance figure (53 / 125)
    if "cb" not in lag_ms and "bn" in lag_ms:
        lag_ms["cb"] = lag_ms["bn"] * 53.0 / 125.0
    print("Feed delay used (trade stamp -> receipt, median ms; cb scaled from the live measurement): "
          + ", ".join(f"{x} {v:.0f}" for x, v in sorted(lag_ms.items(), key=lambda kv_: kv_[1])) + "\n")

    for a in ASSETS:
        qs = quotes.get(a)
        if not qs or len(qs) < 2000:
            continue
        T, PM, PL = composite_series(qs, lag_ms, lead_window)
        LM, LL = np.log(PM), np.log(PL)
        # vol + fair value on a 100 ms grid of the plain composite (as the live model does)
        vol, tw = make_vol(), TwapState()
        grid = np.arange(T[0], T[-1], 100.0)
        gi = np.clip(np.searchsorted(T, grid, side="right") - 1, 0, None)
        sgrid = np.full(len(grid), np.nan); pgrid = np.full(len(grid), np.nan)
        for n_, (t, i) in enumerate(zip(grid, gi)):
            vol.update(PM[i], t); tw.update(PM[i], t)
            w = window_at(a, t)
            if w is None or not vol.ready or not tw.ready:
                continue
            strike = strikes.get(w[4])
            if not strike:
                continue
            fv = twap_fair_value(state=tw, strike=strike, sigma_per_sec=vol.sigma_per_sec, now_ms=t,
                                 close_ts_ms=w[1], info_lag_s=0.18)
            pgrid[n_] = fv.p_up; sgrid[n_] = vol.sigma_per_sec

        def sigma_at(t):
            g = int(np.searchsorted(grid, t, side="right")) - 1
            return sgrid[g] if g >= 0 else np.nan

        def p_up_at(t):
            g = int(np.searchsorted(grid, t, side="right")) - 1
            return pgrid[g] if g >= 0 else np.nan

        def ret(series, t, h_ms):
            i = int(np.searchsorted(T, t, side="right")) - 1
            j = int(np.searchsorted(T, t - h_ms, side="right")) - 1
            return None if j < 0 or i < 0 else series[i] - series[j]

        # --- A / B candidates: at every quote
        def gate_1s(series, t, d_hint=None):
            r1, r3 = ret(series, t, 1000.0), ret(series, t, 3000.0)
            s_ = sigma_at(t)
            if r1 is None or r3 is None or not (s_ > 0):
                return None
            z1 = r1 / s_
            if abs(z1) < K:
                return None
            d = 1 if z1 > 0 else -1
            if r3 * d < 0.5 * s_ * math.sqrt(3) or not (r1 * d > r3 * d / 3):
                return None
            return d, z1

        def gate_w(series, t, w_ms):
            rw, r1 = ret(series, t, w_ms), ret(series, t, 1000.0)
            s_ = sigma_at(t)
            if rw is None or r1 is None or not (s_ > 0):
                return None
            zw = rw / (s_ * math.sqrt(w_ms / 1000.0))
            if abs(zw) < K:
                return None
            d = 1 if zw > 0 else -1
            if r1 * d < 0:
                return None
            return d, zw

        cands = {r: [] for r in RULES}
        # A and B: scan quotes (vectorised pre-filter on the 1 s return to keep it fast)
        for name, series in (("A current", LM), ("B lead", LL)):
            j = np.searchsorted(T, T - 1000.0, side="right") - 1
            r1 = np.where(j >= 0, series - series[np.maximum(j, 0)], 0.0)
            sg = np.array([sigma_at(t) for t in T[::50]])
            sg_full = np.repeat(sg, 50)[:len(T)]
            pre = np.where(np.abs(r1) >= K * np.nan_to_num(sg_full, nan=1e9))[0]
            for k_ in pre:
                g = gate_1s(series, T[k_])
                if g:
                    cands[name].append((T[k_], g[0], g[1]))
        # C / D / E: the detector on raw quotes
        det = FastMoveDetector(k_sigma=K, trigger_frac=1.0, spread_floor=1.5, dedupe_ms=500.0)
        ti = 0
        tr = trades.get(a, [])
        fires = []
        for t, x, b, a_ in qs:
            while ti < len(tr) and tr[ti] <= t:
                det.on_trade(a, tr[ti]); ti += 1
            s_ = sigma_at(t)
            mv = det.on_quote(a, x, b, a_, t, s_ if s_ > 0 else None)
            if mv is not None:
                fires.append(mv)
        for mv in fires:
            for name, w in (("C fast-250", 250.0), ("D fast-500", 500.0)):
                g = gate_w(LL, mv.t_ms, w)
                if g and g[0] == mv.direction:
                    cands[name].append((mv.t_ms, g[0], g[1]))
            g = gate_1s(LL, mv.t_ms)
            if g and g[0] == mv.direction:
                cands["E fast-1s"].append((mv.t_ms, g[0], g[1]))
        snap = det.snapshot()
        lead_snap, lead_n = snap["lead_ms"].get(a, {}), snap["lead_n"].get(a, {})
        confirmed = len(fires) - snap["false_starts"]
        print(f"- {a}: {len(qs):,} quotes, detector fired {len(fires)} times "
              f"({', '.join(f'{h} ms: {c}' for h, c in snap['by_horizon_ms'].items())}), "
              f"{confirmed} confirmed by a second exchange within 1.5 s; "
              f"lag behind the first exchange to show a confirmed move, ms: "
              + ", ".join(f"{x} {v:+.0f} (n {lead_n.get(x, 0)})" for x, v in sorted(lead_snap.items(), key=lambda kv_: kv_[1])))

        # --- the order pipeline, identical for every rule
        for name in RULES:
            last_sig = {}
            attempts = defaultdict(int); filled_mkt = set()
            for t, d, z in sorted(cands[name]):
                w = window_at(a, t)
                if w is None:
                    continue
                o, c, up, dn, slug = w
                left = (c - t) / 1000.0
                if not (60.0 <= left <= 295.0):
                    continue
                key = (slug, d)
                if t - last_sig.get(key, -1e18) < 2000.0:
                    continue
                last_sig[key] = t
                row = {"asset": a, "slug": slug, "dir": d, "t": t, "z": z, "order": False, "fill": None}
                sig_rows[name].append(row)
                if slug in filled_mkt or attempts[slug] >= 3:
                    row["why"] = "budget"; continue
                tok = up if d > 0 else dn
                bl = book_at(tok, t, venue=False)
                if bl is None:
                    row["why"] = "no book"; continue
                bb, ba = bl[2], bl[3]
                if not (MIN_ASK <= ba <= MAX_ASK):
                    row["why"] = "price band"; continue
                if ba - bb > MAX_SPREAD + 1e-9:
                    row["why"] = "spread"; continue
                pu = p_up_at(t)
                if not (pu == pu):
                    row["why"] = "model"; continue
                p_tok = pu if d > 0 else 1.0 - pu
                limit = min(round(ba + MAX_SLIP, 2), MAX_ASK)
                edge = p_tok - limit - fee(limit) - fee(p_tok) - 0.005
                if edge < MIN_EDGE:
                    row["why"] = "edge"; continue
                row["order"] = True; attempts[slug] += 1
                t_arr = t + PRESEND_MS + HALF_RTT_MS
                bv = book_at(tok, t_arr, venue=True)
                if bv is None or bv[3] > limit + 1e-9:
                    row["why"] = "killed"; continue
                avail = bv[5] + (bv[7] if limit >= bv[3] + 0.02 - 1e-9 else 0.0)
                shares = min(SIZE, avail)
                if shares < 5.0:
                    row["why"] = "killed"; continue
                entry = bv[3]
                filled_mkt.add(slug)
                # exit
                arr = bk_v.get(tok); keys = kv[tok]
                i0 = bisect.bisect_right(keys, t_arr)
                i1 = bisect.bisect_right(keys, t_arr + HOLD_MS)
                exit_px = None
                for i in range(i0, i1):
                    bid = arr[i, 2]
                    if bid >= entry + TP:
                        exit_px = bid; break
                    if bid <= entry - SL:
                        exit_px = max(0.01, bid - 0.01); break
                if exit_px is None:
                    b20 = book_at(tok, t_arr + HOLD_MS, venue=True)
                    if b20 is not None and b20[0] > t_arr:
                        exit_px = b20[2]
                    elif slug in outcome:
                        exit_px = 1.0 if (outcome[slug] == "UP") == (d > 0) else 0.0
                    else:
                        row["why"] = "no exit book"; row["order"] = False; filled_mkt.discard(slug); continue
                pnl = shares * (exit_px - entry - fee(entry) - (fee(exit_px) if 0 < exit_px < 1 else 0.0))
                row["fill"] = {"shares": shares, "entry": entry, "exit": exit_px, "pnl": pnl}

    covered_min.append(covered)
    del quotes, trades, books
    gc.collect()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ticks", default=str(ROOT / "data/ticks/*.jsonl.gz"))
    ap.add_argument("--lead-window", type=float, default=30.0)
    args = ap.parse_args()
    paths = sorted(glob.glob(args.ticks))
    if not paths:
        sys.exit(f"no recordings match {args.ticks}")
    sig_rows = {r: [] for r in RULES}          # dicts: asset, slug, dir, t, ...
    covered_min = []
    print(f"# Fast feed vs the 1 s rule, {len(paths)} tick recording(s)\n")
    for path in paths:
        process(path, args.lead_window, sig_rows, covered_min)
    print(f"\nTotal covered: {sum(covered_min):.0f} min\n")
    # ------------------------------------------------------------------ report
    print("\n## Signals, orders, fills and P&L per rule\n")
    print("| rule | signals | orders | fills | fill rate | shares | P&L USDC | P&L / fill |\n|---|---:|---:|---:|---:|---:|---:|---:|")
    for name in RULES:
        rows = sig_rows[name]
        orders = [r for r in rows if r["order"] or r.get("why") == "killed"]
        fills = [r for r in rows if r["fill"]]
        pnl = sum(r["fill"]["pnl"] for r in fills)
        sh = sum(r["fill"]["shares"] for r in fills)
        print(f"| {name} | {len(rows)} | {len(orders)} | {len(fills)} | {len(fills) / max(1, len(orders)):.0%} | "
              f"{sh:.0f} | {pnl:+.2f} | {pnl / max(1, len(fills)):+.3f} |")

    print("\n## Detection lead over rule A, per asset (same market and direction, ms; negative = earlier)\n")
    base = defaultdict(list)
    for r in sig_rows["A current"]:
        base[(r["slug"], r["dir"])].append(r["t"])
    print("| asset | " + " | ".join(n for n in RULES[1:]) + " |\n|---|" + "---:|" * (len(RULES) - 1))
    per_asset = {n: defaultdict(list) for n in RULES[1:]}
    for name in RULES[1:]:
        for r in sig_rows[name]:
            ts = base.get((r["slug"], r["dir"]))
            if not ts:
                continue
            nearest = min(ts, key=lambda x: abs(x - r["t"]))
            if -2000.0 <= r["t"] - nearest <= 500.0:
                per_asset[name][r["asset"]].append(r["t"] - nearest)
                per_asset[name]["ALL"].append(r["t"] - nearest)
    for a in list(ASSETS) + ["ALL"]:
        cells = []
        for name in RULES[1:]:
            v = per_asset[name].get(a, [])
            cells.append(f"{np.median(v):+.0f} (n {len(v)})" if v else "-")
        print(f"| {a} | " + " | ".join(cells) + " |")

    print("\n## Fills and P&L per asset (rule A -> rule C -> rule D)\n")
    print("| asset | A fills / orders, P&L | C fills / orders, P&L | D fills / orders, P&L |\n|---|---:|---:|---:|")
    for a in ASSETS:
        cells = []
        for name in ("A current", "C fast-250", "D fast-500"):
            rows = [r for r in sig_rows[name] if r["asset"] == a]
            orders = [r for r in rows if r["order"] or r.get("why") == "killed"]
            fills = [r for r in rows if r["fill"]]
            cells.append(f"{len(fills)} / {len(orders)}, {sum(r['fill']['pnl'] for r in fills):+.2f}")
        print(f"| {a} | " + " | ".join(cells) + " |")

    print("\n## Why signals did not become fills (rule A vs C)\n")
    for name in ("A current", "C fast-250"):
        c = defaultdict(int)
        for r in sig_rows[name]:
            c[r.get("why", "filled")] += 1
        print(f"- {name}: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv_: -kv_[1])))


if __name__ == "__main__":
    main()
