"""Multi-source BTC feed benchmark: which source reaches THIS machine first, does it
predict the next move, and does it buy time at the venue? (2026-09-30)

    python scripts/research_feed_benchmark.py [--ticks 'data/ticks/feedbench_*.jsonl.gz'] \
        [--summary results/feed_benchmark_summary.json] > results/feed_benchmark_report.md

READ-ONLY research over scripts/record_feed_benchmark.py recordings.

Clocks. Every cross-source comparison uses OUR receipt time (``rt``, one process,
one clock). Exchange event times (``et``) are on each venue's own clock and are
used only for that venue's delivery delay and staleness -- never to order one
venue against another.

Anti-bias. The recording is split by time into DEV (first 50 %), VALIDATION
(next 25 %) and TEST (last 25 %). The only free parameters -- the confirmation
fraction (half the move), the lead window (30 ms) and the pipeline delay
(135 ms decision -> venue, 31 ms venue -> us) -- were fixed before this recording
from the 2026-09-29 studies and are not tuned here; the thresholds (1, 2, 5,
10 bp) are the brief's. Tables that could be read as a selection are shown for
DEV and TEST separately.

Definitions
-----------
consensus     median, on a 50 ms grid in receipt time, of the basis-adjusted log
              mids of every quoting source (chainlink excluded); an event is a
              1 s consensus return beyond a threshold, 5 s apart
detection     a source detects an event when its own mid (receipt time) has
              moved half the event's size from its level 1.5 s before the
              consensus crossing; the first source to do so is the leader
composite     a price built from a subset of sources in receipt time: the
              current live source (Coinbase alone), the previous live rule
              (median of Binance, Bybit, Coinbase), lead mode over those three
              and over every source, microprice / liquidity / VWAP variants
venue         the Polymarket BTC 5-minute token on the event's side; its ask
              from the recorded book (venue-stamped and receipt-stamped); an
              order decided at t lands at t + 135 ms and is judged against the
              book we had received by then + 31 ms
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import orjson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.signals.costs import FeeSchedule      # noqa: E402

THRESHOLDS_BP = (1.0, 2.0, 5.0, 10.0)
GRID_MS = 50.0
CONFIRM_FRAC = 0.5          # fixed before this study (09-29 lead/lag analysis)
DETECT_WINDOW_MS = 1_500.0
EVENT_GAP_MS = 5_000.0
LEAD_WINDOW_MS = 30.0       # the live bot's TPB_SPOT_LEAD_WINDOW_MS
PIPELINE_MS = 135.0         # decision -> order at the venue (14 ms presend + ~120 ms one way), 09-29 forensics
BOOK_LAG_MS = 31.0          # venue stamp -> our receipt, 09-30 forensics median
SLIPPAGE = 0.04             # the live limit: ask + 4c
MIN_SHARES = 5.0
EXIT_S = 20.0
MAX_ASK = 0.70
MIN_ASK = 0.05
LEGACY3 = ("binance", "bybit", "coinbase")
REFERENCE = {"pm_chainlink"}
FEE = FeeSchedule()


# ══════════════════════════════════════ load ══════════════════════════════════════

class Data:
    def __init__(self) -> None:
        self.q: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.t: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.d: dict[str, list] = defaultdict(list)
        self.books: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.markets: list[dict] = []
        self.verdicts: Counter = Counter()
        self.lag: list[float] = []
        self.health: list[dict] = []
        self.n = 0
        self.t0 = math.inf
        self.t1 = -math.inf


def load(paths: list[str], asset: str = "BTC") -> Data:
    D = Data()
    for path in paths:
        with gzip.open(path, "rb") as fh:
            try:
                for line in fh:
                    try:
                        r = orjson.loads(line)
                    except orjson.JSONDecodeError:
                        continue
                    D.n += 1
                    k = r.get("k")
                    if k == "q":
                        if r.get("s") != asset or not r.get("b") or not r.get("a"):
                            continue
                        x = r["x"]
                        qq = D.q[x]
                        qq["rt"].append(r["rt"]); qq["bid"].append(r["b"]); qq["ask"].append(r["a"])
                        qq["bq"].append(r.get("bq") or 0.0); qq["aq"].append(r.get("aq") or 0.0)
                        qq["et"].append(r.get("et") or math.nan)
                        D.verdicts[(x, "q", r.get("v", "ok"))] += 1
                        D.t0, D.t1 = min(D.t0, r["rt"]), max(D.t1, r["rt"])
                    elif k == "t":
                        if r.get("s") != asset:
                            continue
                        x = r["x"]
                        tt = D.t[x]
                        tt["rt"].append(r["rt"]); tt["p"].append(r["p"]); tt["q"].append(r["q"])
                        tt["side"].append(r["side"]); tt["et"].append(r.get("et") or math.nan)
                        D.verdicts[(x, "t", r.get("v", "ok"))] += 1
                    elif k == "d":
                        if r.get("s") == asset:
                            D.d[r["x"]].append((r["rt"], r["bids"], r["asks"]))
                    elif k == "b":
                        b = D.books[r["tok"]]
                        b["rt"].append(r["rt"]); b["vt"].append(r.get("vt") or r["rt"])
                        b["bb"].append(r.get("bb") or 0.0); b["ba"].append(r.get("ba") or 1.0)
                        b["bbs"].append(r.get("bbs") or 0.0); b["bas"].append(r.get("bas") or 0.0)
                    elif k == "m":
                        if r.get("s") == asset:
                            D.markets.append(r)
                    elif k == "lag":
                        D.lag.append(r["ms"])
                    elif k == "h":
                        D.health.append(r)
            except (EOFError, gzip.BadGzipFile, OSError):
                pass                            # a recording still being written
    return D


class Series:
    """One source's quotes as arrays, receipt-time sorted."""

    def __init__(self, cols: dict[str, list]) -> None:
        rt = np.asarray(cols["rt"], float)
        order = np.argsort(rt, kind="stable")
        self.rt = rt[order]
        self.bid = np.asarray(cols["bid"], float)[order]
        self.ask = np.asarray(cols["ask"], float)[order]
        self.bq = np.asarray(cols["bq"], float)[order]
        self.aq = np.asarray(cols["aq"], float)[order]
        self.et = np.asarray(cols["et"], float)[order]
        self.mid = 0.5 * (self.bid + self.ask)
        self.lm = np.log(self.mid)
        tot = self.bq + self.aq
        with np.errstate(invalid="ignore", divide="ignore"):
            micro = np.where(tot > 0, (self.bid * self.aq + self.ask * self.bq) / np.where(tot > 0, tot, 1.0), self.mid)
        self.lmicro = np.log(micro)
        self.basis = 0.0                      # log level vs the pivot, set later

    def idx_at(self, t) -> np.ndarray:
        """Index of the last quote at or before t (-1 before the first)."""
        return np.searchsorted(self.rt, t, "right") - 1

    def lm_at(self, t: float) -> float | None:
        i = int(np.searchsorted(self.rt, t, "right")) - 1
        return float(self.lm[i]) if i >= 0 else None


def build_series(D: Data) -> dict[str, Series]:
    out = {}
    for x, cols in D.q.items():
        if len(cols["rt"]) >= 50:
            out[x] = Series(cols)
    pivot = "binance" if "binance" in out else next(iter(out))
    grid = np.arange(D.t0, D.t1, 1000.0)
    ip = out[pivot].idx_at(grid)
    for x, s in out.items():
        ix = s.idx_at(grid)
        ok = (ix >= 0) & (ip >= 0)
        if ok.sum() > 10:
            s.basis = float(np.median(s.lm[ix[ok]] - out[pivot].lm[ip[ok]]))
    return out


# ═══════════════════════════════════ consensus ═══════════════════════════════════

def consensus(series: dict[str, Series], t0: float, t1: float, sources: tuple[str, ...] | None = None,
              max_age_ms: float = 3_000.0, mode: str = "median", lag: dict[str, float] | None = None,
              lead_window_ms: float = 0.0, use_micro: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Grid (ms) and log price of a composite over ``sources`` in receipt time.

    mode: median | liquidity (top-size weighted) | lead (median of the sources whose information
    time -- receipt minus their measured delay -- is within ``lead_window_ms`` of the newest).
    """
    grid = np.arange(t0, t1, GRID_MS)
    names = [x for x in (sources or series) if x in series and x not in REFERENCE]
    n = len(grid)
    vals = np.full((len(names), n), np.nan)
    ages = np.full((len(names), n), np.inf)
    weights = np.zeros((len(names), n))
    for k, x in enumerate(names):
        s = series[x]
        ix = s.idx_at(grid)
        ok = ix >= 0
        src = s.lmicro if use_micro else s.lm
        vals[k, ok] = src[ix[ok]] - s.basis
        age = grid[ok] - s.rt[ix[ok]]
        if lag is not None:
            age = age + lag.get(x, 0.0)
        ages[k, ok] = age
        weights[k, ok] = (s.bq[ix[ok]] + s.aq[ix[ok]])
    fresh = ages <= max_age_ms
    vals = np.where(fresh, vals, np.nan)
    if mode == "lead" and lead_window_ms > 0.0:
        newest = np.nanmin(np.where(fresh, ages, np.nan), axis=0)          # smallest information age
        inwin = fresh & (ages <= newest[None, :] + lead_window_ms)
        vals = np.where(inwin, vals, np.nan)
    with np.errstate(all="ignore"):
        if mode == "liquidity":
            w = np.where(np.isnan(vals), 0.0, weights)
            c = np.nansum(np.where(np.isnan(vals), 0.0, vals) * w, axis=0) / np.where(w.sum(axis=0) > 0, w.sum(axis=0), np.nan)
        else:
            c = np.nanmedian(vals, axis=0)
    return grid, c


def vwap_series(D: Data, t0: float, t1: float, basis: dict[str, float] | None = None, window_ms: float = 1_000.0,
                exclude=("pm_chainlink",)) -> tuple[np.ndarray, np.ndarray]:
    """Trade VWAP over the last second across every source, each source's prints brought to the
    pivot's level with its basis (USD and USDT venues sit bps apart; unadjusted, the VWAP would jump
    whenever the mix of venues printing changes)."""
    rows = []
    for x, cols in D.t.items():
        if x in exclude or not cols["rt"]:
            continue
        adj = math.exp(-(basis or {}).get(x, 0.0))
        rows.append(np.column_stack([cols["rt"], np.asarray(cols["p"], float) * adj, cols["q"]]))
    if not rows:
        return np.arange(t0, t1, GRID_MS), np.full(int((t1 - t0) // GRID_MS) + 1, np.nan)
    a = np.vstack(rows)
    a = a[np.argsort(a[:, 0], kind="stable")]
    rt, p, q = a[:, 0], a[:, 1], a[:, 2]
    pq = np.cumsum(p * q)
    cq = np.cumsum(q)
    grid = np.arange(t0, t1, GRID_MS)
    hi = np.searchsorted(rt, grid, "right")
    lo = np.searchsorted(rt, grid - window_ms, "right")
    with np.errstate(all="ignore"):
        num = pq[hi - 1] - np.where(lo > 0, pq[lo - 1], 0.0)
        den = cq[hi - 1] - np.where(lo > 0, cq[lo - 1], 0.0)
        v = np.where((hi > 0) & (den > 0), num / np.where(den > 0, den, 1.0), np.nan)
    # carry the last value forward through quiet 50 ms slots
    idx = np.where(~np.isnan(v), np.arange(len(v)), 0)
    np.maximum.accumulate(idx, out=idx)
    v = v[idx]
    return grid, np.log(v)


def returns_bp(grid: np.ndarray, c: np.ndarray, horizon_ms: float) -> np.ndarray:
    k = int(round(horizon_ms / GRID_MS))
    r = np.full_like(c, np.nan)
    r[k:] = (c[k:] - c[:-k]) * 1e4
    return r


def find_events(grid: np.ndarray, r1: np.ndarray, thr_bp: float) -> list[tuple[float, int, float]]:
    """(time, direction, size_bp) where |1 s return| first exceeds thr, events 5 s apart."""
    out = []
    last = -math.inf
    hit = np.where(np.abs(r1) >= thr_bp)[0]
    for i in hit:
        t = grid[i]
        if t - last < EVENT_GAP_MS:
            continue
        last = t
        out.append((float(t), 1 if r1[i] > 0 else -1, float(abs(r1[i]))))
    return out


def detect(s: Series, t_event: float, direction: int, size_bp: float) -> float | None:
    """Receipt time at which this source's mid had moved CONFIRM_FRAC of the event from its level
    DETECT_WINDOW_MS before the consensus crossing, or None."""
    i0 = int(np.searchsorted(s.rt, t_event - DETECT_WINDOW_MS, "right")) - 1
    if i0 < 0:
        return None
    base = s.lm[i0]
    need = CONFIRM_FRAC * size_bp / 1e4
    lo, hi = i0 + 1, int(np.searchsorted(s.rt, t_event + DETECT_WINDOW_MS, "right"))
    seg = direction * (s.lm[lo:hi] - base)
    j = np.argmax(seg >= need) if hi > lo else 0
    if hi > lo and seg[j] >= need:
        return float(s.rt[lo + j])
    return None


def composite_detect(grid: np.ndarray, c: np.ndarray, t_event: float, direction: int, size_bp: float) -> float | None:
    i0 = int(np.searchsorted(grid, t_event - DETECT_WINDOW_MS, "right")) - 1
    if i0 < 0 or np.isnan(c[i0]):
        return None
    base = c[i0]
    need = CONFIRM_FRAC * size_bp / 1e4
    hi = int(np.searchsorted(grid, t_event + DETECT_WINDOW_MS, "right"))
    seg = direction * (c[i0 + 1:hi] - base)
    ok = np.where(seg >= need)[0]
    return float(grid[i0 + 1 + ok[0]]) if len(ok) else None


# ═══════════════════════════════════ helpers ═══════════════════════════════════

def q(xs, p):
    xs = sorted(x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x)))
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1)))] if xs else math.nan


def f0(v):
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.0f}"


def f1(v):
    return "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.1f}"


def pct(a, b):
    return f"{a / b:.0%}" if b else "—"


def utc(ms: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def split_of(t: float, t0: float, t1: float) -> str:
    f = (t - t0) / max(1.0, t1 - t0)
    return "dev" if f < 0.5 else "val" if f < 0.75 else "test"


# ═══════════════════════════════════ sections ═══════════════════════════════════

def section_delivery(D: Data, series: dict[str, Series], minutes: float, out: list[str]) -> dict:
    out.append("## 1. Delivery: what each source sends and how late it arrives\n")
    out.append("Delay is `receipt − exchange event time`, on the exchange's clock (its offset to ours is inside the "
               "number; Hyperliquid stamps block time). Binance spot's `bookTicker` carries no event time: its trades "
               "stand in. Stale = event time more than 2 s before receipt; dup / out-of-order per the validator "
               "(`feeds/sources.py`). Gaps in sequence numbers are informational -- most venues number every book "
               "change, not every message -- and are not shown.\n")
    out.append("| source | quotes/min | trades/min | delay p50 | p90 | p95 | p99 | max | stale | dup | out-of-order | silences > 5 s |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    summary = {}
    for x in sorted(series, key=lambda k: q(_delays(D, k), .5)):
        s = series[x]
        d = _delays(D, x)
        nq = len(s.rt)
        nt = len(D.t.get(x, {}).get("rt", []))
        tot = sum(v for (xx, kind, _), v in D.verdicts.items() if xx == x)
        stale = sum(v for (xx, kind, vv), v in D.verdicts.items() if xx == x and vv == "stale")
        dup = sum(v for (xx, kind, vv), v in D.verdicts.items() if xx == x and vv == "dup")
        ooo = sum(v for (xx, kind, vv), v in D.verdicts.items() if xx == x and vv == "out_of_order")
        gaps = int(np.sum(np.diff(s.rt) > 5_000.0)) if nq > 1 else 0
        summary[x] = {"quotes_per_min": nq / minutes, "trades_per_min": nt / minutes, "delay_p50": q(d, .5),
                      "delay_p90": q(d, .9), "delay_p95": q(d, .95), "delay_p99": q(d, .99),
                      "delay_max": max(d) if d else math.nan, "stale_pct": stale / tot if tot else 0.0,
                      "dup_pct": dup / tot if tot else 0.0, "ooo_pct": ooo / tot if tot else 0.0, "silences": gaps}
        out.append(f"| {x} | {nq / minutes:.0f} | {nt / minutes:.0f} | {f0(q(d, .5))} | {f0(q(d, .9))} | {f0(q(d, .95))} | "
                   f"{f0(q(d, .99))} | {f0(max(d) if d else math.nan)} | {pct(stale, tot)} | {pct(dup, tot)} | "
                   f"{pct(ooo, tot)} | {gaps} |")
    if D.lag:
        out.append(f"\nRecorder loop lag (a 100 ms timer firing late, samples over 2 ms): {len(D.lag)} samples, "
                   f"p50 {q(D.lag, .5):.1f} ms, p99 {q(D.lag, .99):.1f} ms, max {max(D.lag):.1f} ms over the recording; "
                   f"receipt times carry at most this much of our own delay.")
    else:
        out.append("\nRecorder loop lag: no 100 ms timer fired more than 2 ms late.")
    out.append("")
    return summary


def _delays(D: Data, x: str) -> list[float]:
    cols = D.q.get(x)
    rt = np.asarray(cols["rt"], float) if cols else np.zeros(0)
    et = np.asarray(cols["et"], float) if cols else np.zeros(0)
    ok = ~np.isnan(et)
    if ok.sum() < 20:
        tc = D.t.get(x)
        if tc:
            rt = np.asarray(tc["rt"], float); et = np.asarray(tc["et"], float); ok = ~np.isnan(et)
    return list((rt[ok] - et[ok])) if ok.sum() else []


def section_events(D: Data, series: dict[str, Series], grid, c, out: list[str], live_source: str) -> dict:
    out.append("## 2. Shock events: which source tells this machine first\n")
    out.append(f"Events are consensus 1 s returns beyond the threshold ({', '.join(f'{t:g}' for t in THRESHOLDS_BP)} bp), "
               f"5 s apart. A source detects one when its own mid has moved half the event's size (receipt time). "
               f"`first` = share of events it detected before every other source; `lag` = ms after the first source "
               f"(0 when it is the first); `vs {live_source}` = ms before (−) or after (+) the live bot's current "
               f"source; `missed` = never crossed half the move within ±1.5 s.\n")
    r1 = returns_bp(grid, c, 1_000.0)
    result = {}
    for thr in THRESHOLDS_BP:
        events = find_events(grid, r1, thr)
        if len(events) < 3:
            out.append(f"### {thr:g} bp: {len(events)} events — too few to tabulate\n")
            continue
        per = defaultdict(list)          # source -> lags vs first
        vs_live = defaultdict(list)
        vs_bn = defaultdict(list)
        firsts = Counter()
        missed = Counter()
        n_used = 0
        arrival_orders = []
        for t, d, size in events:
            det = {x: detect(s, t, d, size) for x, s in series.items() if x not in REFERENCE}
            got = {x: v for x, v in det.items() if v is not None}
            if len(got) < 2:
                continue
            n_used += 1
            t_first = min(got.values())
            first = min(got, key=got.get)
            firsts[first] += 1
            arrival_orders.append(sorted(got, key=got.get))
            for x in det:
                if det[x] is None:
                    missed[x] += 1
                    continue
                per[x].append(det[x] - t_first)
                if live_source in got and x != live_source:
                    vs_live[x].append(det[x] - got[live_source])
                if "binance" in got and x != "binance":
                    vs_bn[x].append(det[x] - got["binance"])
        out.append(f"### {thr:g} bp: {len(events)} events, {n_used} with two or more detections "
                   f"({utc(events[0][0])} → {utc(events[-1][0])})\n")
        out.append(f"| source | detected | first | lag p50 | p90 | p95 | p99 | max | vs {live_source} p50 | vs binance p50 | missed |")
        out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        rows = {}
        for x in sorted(per, key=lambda k: (-(firsts[k] / n_used), q(per[k], .5))):
            v = per[x]
            rows[x] = {"detected": len(v), "first_pct": firsts[x] / n_used, "lag_p50": q(v, .5), "lag_p90": q(v, .9),
                       "lag_p95": q(v, .95), "lag_p99": q(v, .99), "lag_max": max(v),
                       "vs_live_p50": q(vs_live[x], .5) if vs_live[x] else None,
                       "vs_binance_p50": q(vs_bn[x], .5) if vs_bn[x] else None, "missed": missed[x]}
            out.append(f"| {x} | {len(v)} | {firsts[x] / n_used:.0%} | {f0(q(v, .5))} | {f0(q(v, .9))} | {f0(q(v, .95))} | "
                       f"{f0(q(v, .99))} | {f0(max(v))} | {f0(rows[x]['vs_live_p50']) if x != live_source else '0'} | "
                       f"{f0(rows[x]['vs_binance_p50']) if x != 'binance' else '0'} | {missed[x]} |")
        out.append("")
        result[f"{thr:g}"] = {"events": len(events), "used": n_used, "sources": rows}
    return result


def section_direction_and_regime(D: Data, series: dict[str, Series], grid, c, out: list[str]) -> dict:
    out.append("## 3. Leader by direction, size and volatility regime\n")
    out.append("First-arrival share per source (2 bp events unless noted). Volatility terciles come from the "
               "rolling 5-minute standard deviation of consensus 1 s returns at the event; halves of the recording "
               "are shown so a leader that only led for an hour does not pass for a rule.\n")
    r1 = returns_bp(grid, c, 1_000.0)
    vol = _rolling_vol(r1, int(300_000 / GRID_MS))
    events2 = find_events(grid, r1, 2.0)
    events5 = find_events(grid, r1, 5.0)
    events1 = find_events(grid, r1, 1.0)
    vols = np.array([vol[int(np.searchsorted(grid, t))] for t, _, _ in events2]) if events2 else np.zeros(0)
    terc = np.nanpercentile(vols, [33.3, 66.7]) if len(vols) > 5 else np.array([0, 0])
    buckets: dict[str, list] = {
        "all 2 bp": events2, "up 2 bp": [e for e in events2 if e[1] > 0], "down 2 bp": [e for e in events2 if e[1] < 0],
        "small (1–2 bp)": [e for e in events1 if e[2] < 2.0], "large (≥ 5 bp)": events5,
        "low vol": [e for e, v in zip(events2, vols) if v <= terc[0]],
        "mid vol": [e for e, v in zip(events2, vols) if terc[0] < v <= terc[1]],
        "high vol": [e for e, v in zip(events2, vols) if v > terc[1]],
        "first half": [e for e in events2 if e[0] < (D.t0 + D.t1) / 2], "second half": [e for e in events2 if e[0] >= (D.t0 + D.t1) / 2],
        "DEV": [e for e in events2 if split_of(e[0], D.t0, D.t1) == "dev"],
        "TEST": [e for e in events2 if split_of(e[0], D.t0, D.t1) == "test"],
    }
    names = [x for x in series if x not in REFERENCE]
    share: dict[str, dict[str, float]] = {}
    medlag: dict[str, dict[str, float]] = {}
    for label, evs in buckets.items():
        firsts = Counter()
        lags = defaultdict(list)
        n = 0
        for t, d, size in evs:
            det = {x: detect(series[x], t, d, size) for x in names}
            got = {x: v for x, v in det.items() if v is not None}
            if len(got) < 2:
                continue
            n += 1
            tf = min(got.values())
            firsts[min(got, key=got.get)] += 1
            for x, v in got.items():
                lags[x].append(v - tf)
        share[label] = {x: (firsts[x] / n if n else math.nan) for x in names}
        medlag[label] = {x: (q(lags[x], .5) if lags[x] else math.nan) for x in names}
        share[label]["_n"] = n
    top = sorted(names, key=lambda x: -share["all 2 bp"].get(x, 0))[:8]
    out.append("| regime | events | " + " | ".join(top) + " |")
    out.append("|---|---:|" + "---:|" * len(top))
    for label in buckets:
        n = share[label]["_n"]
        out.append(f"| {label} | {n} | " + " | ".join(
            f"{share[label][x]:.0%} / {f0(medlag[label][x])}" if n else "—" for x in top) + " |")
    out.append("\nCells: first-arrival share / median lag behind the first source, ms.\n")
    # rolling 30-minute leader table
    out.append("### Rolling 30-minute windows (2 bp events): first-arrival share\n")
    out.append("| window (UTC) | events | " + " | ".join(top[:6]) + " |")
    out.append("|---|---:|" + "---:|" * 6)
    w = 1_800_000.0
    t = D.t0
    roll = []
    while t < D.t1:
        evs = [e for e in events2 if t <= e[0] < t + w]
        firsts = Counter()
        n = 0
        for te, d, size in evs:
            got = {x: v for x in names if (v := detect(series[x], te, d, size)) is not None}
            if len(got) >= 2:
                n += 1
                firsts[min(got, key=got.get)] += 1
        if n:
            row = {x: firsts[x] / n for x in top[:6]}
            roll.append({"start": utc(t), "n": n, **row})
            out.append(f"| {utc(t)[11:16]} | {n} | " + " | ".join(f"{row[x]:.0%}" for x in top[:6]) + " |")
        t += w
    out.append("")
    return {"share": share, "median_lag": medlag, "rolling": roll, "top": top}


def _rolling_vol(r1: np.ndarray, win: int) -> np.ndarray:
    x = np.where(np.isnan(r1), 0.0, r1)
    x2 = x * x
    c1 = np.cumsum(x); c2 = np.cumsum(x2)
    n = np.arange(1, len(x) + 1, dtype=float)
    lo = np.maximum(0, np.arange(len(x)) - win)
    cnt = n - lo
    s1 = c1 - np.where(lo > 0, c1[lo - 1], 0.0)
    s2 = c2 - np.where(lo > 0, c2[lo - 1], 0.0)
    with np.errstate(invalid="ignore"):
        return np.sqrt(np.maximum(s2 / cnt - (s1 / cnt) ** 2, 0.0))


def section_predictive(D: Data, series: dict[str, Series], grid, c, out: list[str]) -> dict:
    out.append("## 4. Predictive value: a source's own shocks, and the microstructure features\n")
    out.append("**4a. A source's own 1 s shock as a signal.** For each source, the moments its OWN 1 s return "
               "(receipt time, 100 ms grid) first crosses the threshold, 5 s apart. `same sign` = the consensus moved "
               "the same way over the next 1 s; `confirmed` = the consensus 1 s return reached the threshold within "
               "1.5 s (the shock was real); `lead` = ms before (−) the consensus crossing. DEV / TEST.\n")
    r1c = returns_bp(grid, c, 1_000.0)
    fwd1 = np.full_like(c, np.nan)
    k1 = int(1_000 / GRID_MS)
    fwd1[:-k1] = (c[k1:] - c[:-k1]) * 1e4
    res = {}
    out.append("| source | thr | DEV n | same sign | confirmed | lead p50 | TEST n | same sign | confirmed | lead p50 |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for x in sorted(series):
        if x in REFERENCE:
            continue
        s = series[x]
        ix = s.idx_at(grid)
        lm = np.where(ix >= 0, s.lm[np.maximum(ix, 0)], np.nan)
        r1s = returns_bp(grid, lm, 1_000.0)
        for thr in (1.0, 2.0, 5.0):
            evs = find_events(grid, r1s, thr)
            cells = []
            row = {}
            for part in ("dev", "test"):
                sub = [e for e in evs if split_of(e[0], D.t0, D.t1) == part]
                same = conf = 0
                leads = []
                for t, d, _ in sub:
                    i = int(np.searchsorted(grid, t))
                    if i >= len(grid) or np.isnan(fwd1[i]):
                        continue
                    same += int(np.sign(fwd1[i]) == d)
                    j_hi = min(len(grid), i + int(1_500 / GRID_MS))
                    j_lo = max(0, i - int(1_500 / GRID_MS))
                    seg = r1c[j_lo:j_hi] * d
                    hit = np.where(seg >= thr)[0]
                    if len(hit):
                        conf += 1
                        leads.append(t - grid[j_lo + hit[0]])
                n = len(sub)
                row[part] = {"n": n, "same_sign": same / n if n else math.nan, "confirmed": conf / n if n else math.nan,
                             "lead_p50": q(leads, .5) if leads else math.nan}
                cells.append(f"{n} | {pct(same, n)} | {pct(conf, n)} | {f0(q(leads, .5) if leads else math.nan)}")
            res[f"{x}@{thr:g}"] = row
            out.append(f"| {x} | {thr:g} | " + " | ".join(cells) + " |")
    out.append("")
    # 4b features
    out.append("**4b. Microstructure features vs the next consensus move** (100 ms grid; Spearman rank correlation "
               "with the forward 1 s and 5 s consensus return, and the directional accuracy of the feature's sign "
               "in its top decile of |value|). Velocity = 200 ms return, acceleration = its change, trade imbalance = "
               "buy share of 1 s traded volume, OBI = (bid − ask size) / (bid + ask size) over the top 1 / 5 / 10 "
               "levels (Binance spot, OKX, Bitstamp depth streams), divergence = source mid − Binance mid, "
               "basis-adjusted, bp. DEV / TEST.\n")
    step = int(100 / GRID_MS)
    g = grid[::step]; cg = c[::step]
    k1, k5 = 10, 50
    f1 = np.full_like(cg, np.nan); f5 = np.full_like(cg, np.nan)
    f1[:-k1] = (cg[k1:] - cg[:-k1]) * 1e4
    f5[:-k5] = (cg[k5:] - cg[:-k5]) * 1e4
    feats: dict[str, np.ndarray] = {}
    for x in sorted(series):
        if x in REFERENCE:
            continue
        s = series[x]
        ix = s.idx_at(g)
        lm = np.where(ix >= 0, s.lm[np.maximum(ix, 0)] - s.basis, np.nan)
        v = np.full_like(lm, np.nan); v[2:] = (lm[2:] - lm[:-2]) * 1e4 / 0.2
        a = np.full_like(lm, np.nan); a[2:] = v[2:] - v[:-2]
        feats[f"velocity {x}"] = v
        feats[f"acceleration {x}"] = a
        if x != "binance" and "binance" in series:
            sb = series["binance"]
            ib = sb.idx_at(g)
            lb = np.where(ib >= 0, sb.lm[np.maximum(ib, 0)], np.nan)
            feats[f"divergence {x} − binance"] = (lm - lb) * 1e4
    # trade imbalance
    for x, cols in D.t.items():
        if x in REFERENCE or len(cols["rt"]) < 100:
            continue
        rt = np.asarray(cols["rt"], float); order = np.argsort(rt, kind="stable"); rt = rt[order]
        vol = (np.asarray(cols["p"], float) * np.asarray(cols["q"], float))[order]
        side = np.asarray(cols["side"], float)[order]
        buy = np.cumsum(np.where(side > 0, vol, 0.0)); tot = np.cumsum(vol)
        hi = np.searchsorted(rt, g, "right"); lo = np.searchsorted(rt, g - 1_000.0, "right")
        b = buy[np.maximum(hi - 1, 0)] - np.where(lo > 0, buy[lo - 1], 0.0)
        tt = tot[np.maximum(hi - 1, 0)] - np.where(lo > 0, tot[lo - 1], 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            feats[f"trade imbalance {x}"] = np.where((hi > 0) & (tt > 0), b / tt - 0.5, np.nan)
    # OBI
    for x, rows in D.d.items():
        if len(rows) < 100:
            continue
        rt = np.asarray([r[0] for r in rows], float)
        for depth in (1, 5, 10):
            obi = np.full(len(rows), np.nan)
            for i, (_, bids, asks) in enumerate(rows):
                bs = sum(q for _, q in bids[:depth]); as_ = sum(q for _, q in asks[:depth])
                if bs + as_ > 0:
                    obi[i] = (bs - as_) / (bs + as_)
            ix = np.searchsorted(rt, g, "right") - 1
            feats[f"OBI top{depth} {x}"] = np.where(ix >= 0, obi[np.maximum(ix, 0)], np.nan)
    from scipy.stats import spearmanr
    out.append("| feature | DEV n | ρ 1 s | ρ 5 s | top-decile accuracy 1 s | TEST n | ρ 1 s | ρ 5 s | top-decile accuracy 1 s |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    fres = {}
    parts = np.array([split_of(t, D.t0, D.t1) for t in g])
    for name, v in feats.items():
        cells = []
        row = {}
        for part in ("dev", "test"):
            m = (parts == part) & ~np.isnan(v) & ~np.isnan(f1) & ~np.isnan(f5)
            n = int(m.sum())
            if n < 200:
                cells.append(f"{n} | — | — | —"); row[part] = {"n": n}; continue
            rho1 = spearmanr(v[m], f1[m]).correlation
            rho5 = spearmanr(v[m], f5[m]).correlation
            av = np.abs(v[m]); cut = np.nanpercentile(av, 90)
            top = av >= cut
            acc = float(np.mean(np.sign(v[m][top]) == np.sign(f1[m][top]))) if top.sum() else math.nan
            row[part] = {"n": n, "rho1": rho1, "rho5": rho5, "acc_top_decile": acc}
            cells.append(f"{n} | {rho1:+.3f} | {rho5:+.3f} | {acc:.0%}")
        fres[name] = row
        out.append(f"| {name} | " + " | ".join(cells) + " |")
    out.append("")
    return {"own_shocks": res, "features": fres}


def composites(D: Data, series: dict[str, Series], lag: dict[str, float]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    t0, t1 = D.t0, D.t1
    allsrc = tuple(x for x in series if x not in REFERENCE)
    spot = tuple(x for x in allsrc if x not in ("okx_swap", "binance_futures", "bybit_linear", "deribit", "hyperliquid"))
    out = {
        "coinbase alone (live now)": consensus(series, t0, t1, ("coinbase",)),
        "median of 3 (previous live)": consensus(series, t0, t1, LEGACY3),
        "lead mode, 3 sources (live 09-29)": consensus(series, t0, t1, LEGACY3, mode="lead", lag=lag, lead_window_ms=LEAD_WINDOW_MS),
        "median of all spot": consensus(series, t0, t1, spot),
        "median of all (spot + perps)": consensus(series, t0, t1, allsrc),
        "microprice median, all": consensus(series, t0, t1, allsrc, use_micro=True),
        "liquidity-weighted, all": consensus(series, t0, t1, allsrc, mode="liquidity"),
        "lead mode, all sources": consensus(series, t0, t1, allsrc, mode="lead", lag=lag, lead_window_ms=LEAD_WINDOW_MS),
        "trade VWAP 1 s, all": vwap_series(D, t0, t1, {x: s.basis for x, s in series.items()}),
    }
    fastest = min((x for x in allsrc if x in lag), key=lambda x: lag[x], default=None)
    if fastest:
        out[f"{fastest} alone (lowest delay)"] = consensus(series, t0, t1, (fastest,))
    return out


def section_fair_price(D: Data, series: dict[str, Series], grid, c, comps: dict, out: list[str]) -> dict:
    out.append("## 5. Fair-price representations: reaction speed against robustness\n")
    out.append("Each composite is built in receipt time from a subset of sources. `reaction` = ms after the FIRST "
               "individual source at consensus events (2 bp / 5 bp; the composite's own half-crossing); `false moves` = "
               "the composite's own 2 bp 1 s shocks that fewer than three individual sources confirmed within 1.5 s, "
               "per hour and as a share of its shocks; `noise` = standard deviation of its 100 ms returns, bp. "
               "DEV / TEST for the false-move share.\n")
    r1 = returns_bp(grid, c, 1_000.0)
    ev2 = find_events(grid, r1, 2.0)
    ev5 = find_events(grid, r1, 5.0)
    names = [x for x in series if x not in REFERENCE]
    hours = (D.t1 - D.t0) / 3.6e6
    out.append("| composite | reaction 2 bp p50 | p90 | missed | reaction 5 bp p50 | p90 | own 2 bp shocks/h | false/h | false share DEV | TEST | noise |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    res = {}
    for name, (g2, cc) in comps.items():
        react = {2.0: [], 5.0: []}
        miss = 0
        for thr, evs in ((2.0, ev2), (5.0, ev5)):
            for t, d, size in evs:
                got = [v for x in names if (v := detect(series[x], t, d, size)) is not None]
                if len(got) < 2:
                    continue
                tc = composite_detect(g2, cc, t, d, size)
                if tc is None:
                    miss += thr == 2.0
                else:
                    react[thr].append(tc - min(got))
        r1c = returns_bp(g2, cc, 1_000.0)
        own = find_events(g2, r1c, 2.0)
        false = {"dev": [0, 0], "test": [0, 0], "all": [0, 0]}
        for t, d, size in own:
            n_conf = sum(1 for x in names if detect(series[x], t, d, size) is not None)
            part = split_of(t, D.t0, D.t1)
            for key in (part, "all"):
                if key in false:
                    false[key][1] += 1
                    false[key][0] += n_conf < 3
        step = int(100 / GRID_MS)
        r100 = np.diff(cc[::step]) * 1e4
        noise = float(np.nanstd(r100))
        res[name] = {"react2_p50": q(react[2.0], .5), "react2_p90": q(react[2.0], .9), "missed2": miss,
                     "react5_p50": q(react[5.0], .5), "react5_p90": q(react[5.0], .9),
                     "own_shocks_per_h": len(own) / hours, "false_per_h": false["all"][0] / hours,
                     "false_share_dev": false["dev"][0] / false["dev"][1] if false["dev"][1] else math.nan,
                     "false_share_test": false["test"][0] / false["test"][1] if false["test"][1] else math.nan,
                     "noise_bp": noise}
        r = res[name]
        out.append(f"| {name} | {f0(r['react2_p50'])} | {f0(r['react2_p90'])} | {miss} | {f0(r['react5_p50'])} | "
                   f"{f0(r['react5_p90'])} | {r['own_shocks_per_h']:.1f} | {r['false_per_h']:.1f} | "
                   f"{pct(false['dev'][0], false['dev'][1])} | {pct(false['test'][0], false['test'][1])} | {noise:.2f} |")
    out.append("")
    return res


def section_polymarket(D: Data, series: dict[str, Series], grid, c, comps: dict, out: list[str]) -> dict:
    out.append("## 6. The venue: does an earlier BTC signal buy usable time on Polymarket?\n")
    out.append("For every consensus event the BTC 5-minute token on the event's side (UP for an up-move). "
               f"`pre ask` = its best ask when the first source detected the move; `reprice` = the ask rising a tick "
               f"or more within 5 s, timed from that first detection (receipt time of the venue message). For each "
               f"feed variant an order decided at its detection lands {PIPELINE_MS:.0f} ms later and is judged against "
               f"the book we had received by then + {BOOK_LAG_MS:.0f} ms: `window` = ms between the variant's detection "
               f"and the reprice (negative: the token had already moved); `fillable` = the ask still within "
               f"{SLIPPAGE * 100:.0f}c of the pre ask with ≥ {MIN_SHARES:.0f} shares when the order lands; `paid` = "
               f"that ask; `markout` = the bid {EXIT_S:.0f} s after landing minus paid minus the taker fee, cents per "
               f"share over fillable orders. Tokens outside {MIN_ASK:.2f}–{MAX_ASK:.2f} (the live price band) are "
               f"reported separately as `out of band`.\n")
    toks = {}
    for m in D.markets:
        toks[m["slug"]] = m
    books = {}
    for tok, cols in D.books.items():
        rt = np.asarray(cols["rt"], float); order = np.argsort(rt, kind="stable")
        books[tok] = {"rt": rt[order], "vt": np.asarray(cols["vt"], float)[order],
                      "bb": np.asarray(cols["bb"], float)[order], "ba": np.asarray(cols["ba"], float)[order],
                      "bbs": np.asarray(cols["bbs"], float)[order], "bas": np.asarray(cols["bas"], float)[order]}

    def book_at(tok: str, t: float):
        b = books.get(tok)
        if b is None:
            return None
        i = int(np.searchsorted(b["rt"], t, "right")) - 1
        return None if i < 0 else (float(b["bb"][i]), float(b["ba"][i]), float(b["bbs"][i]), float(b["bas"][i]))

    def market_for(t: float):
        for m in D.markets:
            if m["open"] <= t < m["close"]:
                return m
        return None

    names = [x for x in series if x not in REFERENCE]
    variants = {
        "first source (any)": None,
        "coinbase alone (live now)": comps["coinbase alone (live now)"],
        "median of 3 (previous live)": comps["median of 3 (previous live)"],
        "lead mode, 3 sources (live 09-29)": comps["lead mode, 3 sources (live 09-29)"],
        "lead mode, all sources": comps["lead mode, all sources"],
        "median of all (spot + perps)": comps["median of all (spot + perps)"],
    }
    r1 = returns_bp(grid, c, 1_000.0)
    res = {}
    for thr in THRESHOLDS_BP:
        events = find_events(grid, r1, thr)
        rows = []
        for t, d, size in events:
            m = market_for(t)
            if m is None:
                continue
            tok = m["up"] if d > 0 else m["dn"]
            det = {x: v for x in names if (v := detect(series[x], t, d, size)) is not None}
            if len(det) < 2:
                continue
            t_first = min(det.values())
            pre = book_at(tok, t_first)
            if pre is None or pre[1] <= 0 or pre[1] >= 1:
                continue
            pre_ask = pre[1]
            b = books[tok]
            j = int(np.searchsorted(b["rt"], t_first, "right"))
            hi = int(np.searchsorted(b["rt"], t_first + 5_000.0, "right"))
            seg = b["ba"][j:hi]
            k = np.where(seg >= pre_ask + 0.01 - 1e-9)[0]
            t_move = float(b["rt"][j + k[0]]) if len(k) else None
            row = {"t": t, "thr": thr, "size": size, "pre_ask": pre_ask, "t_first": t_first, "t_move": t_move,
                   "in_band": MIN_ASK <= pre_ask <= MAX_ASK, "seconds_left": (m["close"] - t) / 1000.0,
                   "part": split_of(t, D.t0, D.t1), "variants": {}}
            for vname, comp in variants.items():
                if comp is None:
                    td = t_first
                else:
                    td = composite_detect(comp[0], comp[1], t, d, size)
                if td is None:
                    row["variants"][vname] = None
                    continue
                land = td + PIPELINE_MS
                bk = book_at(tok, land + BOOK_LAG_MS)
                fillable = bk is not None and bk[1] <= pre_ask + SLIPPAGE + 1e-9 and bk[3] >= MIN_SHARES and bk[1] < 1.0
                paid = bk[1] if fillable else None
                exit_b = book_at(tok, land + EXIT_S * 1000.0)
                markout = None
                if fillable and exit_b is not None and exit_b[0] > 0:
                    markout = (exit_b[0] - paid - FEE.taker_fee_per_share(paid)) * 100.0
                row["variants"][vname] = {"td": td, "window": (t_move - td) if t_move else None, "fillable": fillable,
                                          "paid": paid, "markout_c": markout}
            rows.append(row)
        if len(rows) < 3:
            out.append(f"### {thr:g} bp: {len(rows)} events with a venue book — too few\n")
            continue
        n_band = sum(r["in_band"] for r in rows)
        repriced = [r for r in rows if r["t_move"] is not None]
        resp = [r["t_move"] - r["t_first"] for r in repriced]
        out.append(f"### {thr:g} bp: {len(rows)} events with a venue book ({n_band} in band); token repriced within 5 s "
                   f"in {len(repriced)} ({len(repriced) / len(rows):.0%}); response after the first source: "
                   f"p25 {f0(q(resp, .25))}, p50 {f0(q(resp, .5))}, p75 {f0(q(resp, .75))}, p90 {f0(q(resp, .9))} ms\n")
        out.append("| feed variant | split | events | detect vs first p50 | window p50 | window p90 | window > 0 | fillable | paid − pre p50 | markout p50 | markout mean | n markout |")
        out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        tres = {}
        for vname in variants:
            for part in ("dev", "test", "all"):
                sub = [r for r in rows if r["in_band"] and (part == "all" or r["part"] == part) and r["variants"].get(vname)]
                if not sub:
                    continue
                dvf = [r["variants"][vname]["td"] - r["t_first"] for r in sub]
                win = [r["variants"][vname]["window"] for r in sub if r["variants"][vname]["window"] is not None]
                fill = sum(r["variants"][vname]["fillable"] for r in sub)
                paid = [r["variants"][vname]["paid"] - r["pre_ask"] for r in sub if r["variants"][vname]["paid"] is not None]
                mk = [r["variants"][vname]["markout_c"] for r in sub if r["variants"][vname]["markout_c"] is not None]
                tres[f"{vname}|{part}"] = {"n": len(sub), "detect_vs_first_p50": q(dvf, .5), "window_p50": q(win, .5),
                                           "window_p90": q(win, .9), "window_pos": sum(w > 0 for w in win) / len(win) if win else math.nan,
                                           "fillable": fill / len(sub), "paid_minus_pre_p50": q(paid, .5) if paid else math.nan,
                                           "markout_p50": q(mk, .5) if mk else math.nan,
                                           "markout_mean": st.mean(mk) if mk else math.nan, "n_markout": len(mk)}
                r = tres[f"{vname}|{part}"]
                out.append(f"| {vname} | {part.upper()} | {len(sub)} | {f0(r['detect_vs_first_p50'])} | {f0(r['window_p50'])} | "
                           f"{f0(r['window_p90'])} | {pct(sum(w > 0 for w in win), len(win))} | {r['fillable']:.0%} | "
                           f"{f1(r['paid_minus_pre_p50'] * 100) if paid else '—'}c | {f1(r['markout_p50'])}c | "
                           f"{f1(r['markout_mean'])}c | {len(mk)} |")
        out.append("")
        res[f"{thr:g}"] = {"events": len(rows), "in_band": n_band, "repriced": len(repriced), "response_ms": {
            "p25": q(resp, .25), "p50": q(resp, .5), "p75": q(resp, .75), "p90": q(resp, .9)}, "variants": tres}
    return res


# ═══════════════════════════════════════ main ═══════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default=str(ROOT / "data/ticks/feedbench_*.jsonl.gz"))
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--live-source", default="coinbase")
    ap.add_argument("--summary", default=None)
    args = ap.parse_args()
    paths = sorted(glob.glob(args.ticks))
    if not paths:
        raise SystemExit(f"no recordings match {args.ticks}")
    D = load(paths, args.asset)
    series = build_series(D)
    if not series:
        raise SystemExit("no quotes")
    minutes = (D.t1 - D.t0) / 60_000.0
    out: list[str] = []
    out.append(f"# Multi-source BTC feed benchmark ({args.asset})\n")
    out.append(f"Recordings: {', '.join(Path(p).name for p in paths)} — {utc(D.t0)} → {utc(D.t1)} ({minutes:.0f} min, "
               f"{D.n:,} records, {len(series)} quoting sources). DEV = first 50 %, VALIDATION = next 25 %, TEST = last 25 % "
               f"({utc(D.t0 + 0.75 * (D.t1 - D.t0))} onward).\n")
    delivery = section_delivery(D, series, minutes, out)
    lag = {x: v["delay_p50"] for x, v in delivery.items() if not math.isnan(v["delay_p50"])}
    grid, c = consensus(series, D.t0, D.t1)
    events = section_events(D, series, grid, c, out, args.live_source)
    regime = section_direction_and_regime(D, series, grid, c, out)
    pred = section_predictive(D, series, grid, c, out)
    comps = composites(D, series, lag)
    fair = section_fair_price(D, series, grid, c, comps, out)
    venue = section_polymarket(D, series, grid, c, comps, out)
    print("\n".join(out))
    if args.summary:
        Path(args.summary).write_text(json.dumps({
            "recordings": [Path(p).name for p in paths], "t0": D.t0, "t1": D.t1, "minutes": minutes,
            "delivery": delivery, "events": events, "regime": {k: v for k, v in regime.items() if k != "rolling"},
            "rolling": regime["rolling"], "predictive": pred, "fair_price": fair, "venue": venue,
            "parameters": {"confirm_frac": CONFIRM_FRAC, "lead_window_ms": LEAD_WINDOW_MS, "pipeline_ms": PIPELINE_MS,
                           "book_lag_ms": BOOK_LAG_MS, "slippage": SLIPPAGE, "min_shares": MIN_SHARES, "exit_s": EXIT_S},
        }, indent=1, default=lambda v: None if isinstance(v, float) and math.isnan(v) else v), encoding="utf-8")


if __name__ == "__main__":
    main()
