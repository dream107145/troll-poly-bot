"""Binance vs Bybit vs Coinbase as the bot's spot sources: a head-to-head benchmark.

    python scripts/research_exchange_benchmark.py archive [--chainlink PATH] > results/exchange_benchmark_archive.md
    python scripts/research_exchange_benchmark.py ticks [--ticks 'data/ticks/*.jsonl.gz'] > results/exchange_benchmark_ticks.md

READ-ONLY research: reads recordings, writes a report to stdout.

``archive`` -- what each exchange says about how a window SETTLES, from every
window in data/charts (each exchange's price, sampled with the composite about
every 200 ms) and, where it overlaps, the Chainlink stream Polymarket settles
on (the Bo-BOTT recorder's data/chainlink_ticks.jsonl):
  * coverage: the share of samples in which each exchange had a live price;
  * settlement agreement: the venue resolves UP iff the 60 s mean of Chainlink
    at the close >= the 60 s mean before the open (verified 146/147 in
    Bo-BOTT/scripts/research_settlement.py). The same rule on each exchange's
    own price, against the venue's recorded outcome;
  * Chainlink tracking: level basis, the error of each exchange's settlement
    margin against Chainlink's, and the lag at which each exchange's 1 s
    returns best match Chainlink's.

``ticks`` -- how each exchange reaches THIS machine, from scripts/record_ticks.py
recordings (every quote and trade, with the exchange's own time and ours):
  * delivery delay (exchange event time -> our receipt, clock-corrected),
    message rates, gaps and reconnect-sized silences;
  * price discovery: at every large 1 s move of the cross-exchange median,
    which exchange moved first on the exchanges' own clocks, and which reached
    us first;
  * signal value: after each exchange's own shocks, how the Polymarket token
    moved in the shock's direction 0.5-5 s later.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import gzip
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import orjson

EXCH = ("binance", "bybit", "coinbase")
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
ROOT = Path(__file__).resolve().parents[1]


def utc(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M")


def pct(v: float | None, d: int = 1) -> str:
    return "—" if v is None else f"{100 * v:.{d}f}%"


def q(xs, p):
    return float(np.percentile(xs, p)) if len(xs) else float("nan")


# ═══════════════════════════════════ archive ═══════════════════════════════════

def mean_over(t: np.ndarray, p: np.ndarray, a: float, b: float, max_gap_ms: float = 2000.0) -> float | None:
    """Trapezoid time-mean over [a, b] -- the settlement research's definition --
    or None when the series does not cover the window (under 50 s, or a hole
    longer than ``max_gap_ms``: an exchange that was absent has no opinion)."""
    i, j = np.searchsorted(t, a, "left"), np.searchsorted(t, b, "right")
    tt, pp = t[i:j], p[i:j]
    if len(tt) < 10 or tt[-1] - tt[0] < 50_000 or np.max(np.diff(tt)) > max_gap_ms:
        return None
    return float(np.trapezoid(pp, tt) / (tt[-1] - tt[0]))


def load_window(path: str):
    d = orjson.loads(Path(path).read_bytes())
    pts = d.get("points") or []
    series = {}
    for x in EXCH + ("composite",):
        tt, pp = [], []
        for pt in pts:
            v = pt.get("spot") if x == "composite" else (pt.get("srcs") or {}).get(x)
            t = pt.get("t")
            if isinstance(v, (int, float)) and v > 0 and t:
                tt.append(t)
                pp.append(v)
        series[x] = (np.asarray(tt, float), np.asarray(pp, float))
    return d, series, len(pts)


def load_chainlink(path: Path):
    by: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    with path.open("rb") as fh:
        for line in fh:
            try:
                r = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue
            by[r["a"]].append((float(r["ts"]), float(r["p"]), float(r.get("r") or 0)))
    out = {}
    for a, rows in by.items():
        rows.sort()
        arr = np.asarray(rows)
        # one tick per Chainlink second (the stream repeats a second on reconnect)
        _, keep = np.unique(arr[:, 0], return_index=True)
        arr = arr[keep]
        out[a] = (arr[:, 0], arr[:, 1], arr[:, 2])
    return out


def archive(args) -> None:
    files = sorted(glob.glob(str(ROOT / "data/charts/*.json")))
    cl = load_chainlink(Path(args.chainlink)) if args.chainlink and Path(args.chainlink).exists() else {}
    cl_span = {a: (t[0], t[-1]) for a, (t, _, _) in cl.items()}

    present = defaultdict(Counter)       # asset -> exchange -> samples with a live price
    samples = Counter()
    rows = []                            # one per window
    tracking = defaultdict(list)         # exchange -> (asset, open basis bps, close basis bps, margin err bps)
    cl_rows = []                         # the Chainlink rule itself vs the venue (validation)
    # per asset, every exchange's samples in the Chainlink period, for the lag scan
    lagser = defaultdict(lambda: defaultdict(list))

    for n, f in enumerate(files):
        try:
            d, series, npts = load_window(f)
        except (OSError, orjson.JSONDecodeError):
            continue
        a, y = d.get("asset"), d.get("outcome")
        o, c = d.get("open_ts"), d.get("close_ts")
        if a not in ASSETS or y not in ("UP", "DOWN") or not o or not c:
            continue
        samples[a] += npts
        for x in EXCH:
            present[a][x] += len(series[x][0])
        up = y == "UP"
        row = {"asset": a, "y": up, "close": c}
        for x in EXCH + ("composite",):
            t, p = series[x]
            m0, m1 = mean_over(t, p, o - 60_000, o), mean_over(t, p, c - 60_000, c)
            row[x] = None if m0 is None or m1 is None else ((m1 / m0 - 1) * 1e4, m0, m1)
        # the Chainlink stream itself, where recorded
        if a in cl and cl_span[a][0] <= o - 60_000 and c <= cl_span[a][1]:
            ct, cp, _ = cl[a]
            k0, k1 = mean_over(ct, cp, o - 60_000, o, 5000), mean_over(ct, cp, c - 60_000, c, 5000)
            if k0 and k1:
                cm = (k1 / k0 - 1) * 1e4
                row["chainlink"] = (cm, k0, k1)
                cl_rows.append((a, (k1 >= k0) == up, cm))
                for x in EXCH + ("composite",):
                    v = row[x]
                    if v is not None:
                        tracking[x].append((a, (v[1] / k0 - 1) * 1e4, (v[2] / k1 - 1) * 1e4, v[0] - cm))
                for x in EXCH:
                    t, p = series[x]
                    lagser[a][x].append((t, p))
        rows.append(row)
        if n % 2000 == 0:
            print(f"… {n}/{len(files)}", file=sys.stderr)

    first, last = min(r["close"] for r in rows), max(r["close"] for r in rows)
    print("# Exchange benchmark, part 1: coverage and settlement (archive)\n")
    print(f"{len(rows):,} venue-resolved 5-minute windows, {utc(first)} → {utc(last)} UTC "
          f"(data/charts). Each exchange's price is the one the bot held when it sampled, about "
          f"every 200 ms; a missing price means the exchange had no fresh quote then.\n")

    # ── coverage
    print("## 1. Coverage: share of samples with a live price\n")
    print("| asset | " + " | ".join(EXCH) + " |")
    print("|---|" + "---:|" * len(EXCH))
    for a in ASSETS:
        if samples[a]:
            print(f"| {a} | " + " | ".join(pct(present[a][x] / samples[a]) for x in EXCH) + " |")

    # ── settlement agreement
    print("\n## 2. Settlement agreement: the venue's rule on each exchange's own price\n")
    print("UP iff the 60 s mean at the close ≥ the 60 s mean before the open. An exchange without "
          "a full 60 s of price at either end has no call for that window.\n")
    print("| asset | windows | " + " | ".join(f"{x}" for x in EXCH + ("composite",)) + " |")
    print("|---|---:|" + "---:|" * (len(EXCH) + 1))

    def agree(rs, x):
        called = [r for r in rs if r[x] is not None]
        if not called:
            return None, 0
        ok = sum((r[x][0] >= 0) == r["y"] for r in called)
        return ok / len(called), len(called)

    for a in ASSETS + ("all",):
        rs = [r for r in rows if a == "all" or r["asset"] == a]
        if not rs:
            continue
        cells = []
        for x in EXCH + ("composite",):
            v, k = agree(rs, x)
            cells.append("—" if v is None else f"{pct(v, 2)} ({k:,})")
        print(f"| {'**all**' if a == 'all' else a} | {len(rs):,} | " + " | ".join(cells) + " |")

    # same windows for all three, by closeness of the call
    both = [r for r in rows if all(r[x] is not None for x in EXCH)]
    print(f"\n**Head to head on the {len(both):,} windows where all three have a call** "
          "(BTC, ETH, SOL, XRP almost entirely), by how close the composite's call was:\n")
    print("| |composite margin| | windows | " + " | ".join(EXCH) + " | composite |")
    print("|---|---:|" + "---:|" * (len(EXCH) + 1))
    for lo, hi in ((0, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 1e9), (0, 1e9)):
        rs = [r for r in both if r["composite"] and lo <= abs(r["composite"][0]) < hi]
        if not rs:
            continue
        lab = "all" if (lo, hi) == (0, 1e9) else (f"≥ {lo:g} bps" if hi > 1e8 else f"{lo:g}–{hi:g} bps")
        print(f"| {lab} | {len(rs):,} | " + " | ".join(pct(agree(rs, x)[0], 1) for x in EXCH + ("composite",)) + " |")

    dis = [r for r in both if len({r[x][0] >= 0 for x in EXCH}) > 1]
    if dis:
        right = Counter()
        for r in dis:
            for x in EXCH:
                right[x] += (r[x][0] >= 0) == r["y"]
        print(f"\nIn the **{len(dis):,} windows where the three exchanges disagreed** about the outcome, "
              "each was right: " + ", ".join(f"{x} {right[x]:,} ({pct(right[x] / len(dis))})" for x in EXCH) + ".")

    # ── Chainlink
    if cl_rows:
        ok = sum(r[1] for r in cl_rows)
        t0 = min(v[0][0] for v in cl.values())
        t1 = max(v[0][-1] for v in cl.values())
        print(f"\n## 3. Against Chainlink (the settlement stream), {utc(t0)} → {utc(t1)} UTC\n")
        print(f"{len(cl_rows):,} windows fall inside the Chainlink recording. The rule on Chainlink itself "
              f"matches the venue in **{ok:,} ({pct(ok / len(cl_rows), 2)})** — the ceiling any exchange can reach.\n")
        print("Level basis is exchange − Chainlink at the opening 60 s mean (USDT pairs sit above a USD index). "
              "Margin error is the exchange's settlement margin (close mean vs open mean, bps) minus Chainlink's: "
              "the part of a call that can flip a close window.\n")
        print("| exchange | windows | level basis, median | margin error, median abs | p90 abs | sd | calls matching Chainlink |")
        print("|---|---:|---:|---:|---:|---:|---:|")
        for x in EXCH + ("composite",):
            tr = tracking[x]
            if not tr:
                continue
            ob = [v[1] for v in tr]
            me = np.array([v[3] for v in tr])
            same = 0
            for r in rows:
                if r.get("chainlink") and r[x] is not None:
                    same += (r[x][0] >= 0) == (r["chainlink"][0] >= 0)
            print(f"| {x} | {len(tr):,} | {st.median(ob):+.2f} bps | {st.median(abs(me)):.2f} bps | "
                  f"{q(abs(me), 90):.2f} bps | {me.std():.2f} bps | {pct(same / len(tr), 2)} |")

        # by asset: margin error
        print("\nMargin error, median abs (bps), by asset:\n")
        print("| asset | " + " | ".join(EXCH + ("composite",)) + " |")
        print("|---|" + "---:|" * (len(EXCH) + 1))
        for a in ASSETS:
            cells = []
            for x in EXCH + ("composite",):
                me = [abs(v[3]) for v in tracking[x] if v[0] == a]
                cells.append(f"{st.median(me):.2f} ({len(me)})" if me else "—")
            print(f"| {a} | " + " | ".join(cells) + " |")

        # lag: at what shift do the exchange's 1 s returns best match Chainlink's?
        print("\n**Timing against Chainlink.** Correlation of 1 s log returns, Chainlink second *s* against "
              "the exchange price as the bot held it at *s + lag* (receipt time), lag from −3 s to +3 s. "
              "A positive peak means Chainlink's timestamp runs ahead of our view of the exchange; "
              "a negative one, that Chainlink stamps a price the exchange already showed us earlier.\n")
        print("| asset | " + " | ".join(f"{x}: peak lag / corr" for x in EXCH) + " |")
        print("|---|" + "---|" * len(EXCH))
        lags = np.arange(-3000, 3001, 100)
        for a in ASSETS:
            if a not in cl or a not in lagser:
                continue
            ct, cp, _ = cl[a]
            cells = []
            for x in EXCH:
                parts = lagser[a].get(x) or []
                if not parts:
                    cells.append("—")
                    continue
                t = np.concatenate([p[0] for p in parts])
                p = np.concatenate([p[1] for p in parts])
                order = np.argsort(t, kind="stable")
                t, p = t[order], p[order]
                t, keep = np.unique(t, return_index=True)
                p = p[keep]
                inside = (ct >= t[0] + 3000) & (ct <= t[-1] - 3000)
                cts, cps = ct[inside], cp[inside]
                if len(cts) < 600:
                    cells.append("—")
                    continue
                rc = np.diff(np.log(cps))
                ok = np.diff(cts) == 1000
                best = (None, -2.0)
                for lag in lags:
                    i = np.searchsorted(t, cts + lag, "right") - 1
                    fresh = (cts + lag) - t[np.clip(i, 0, None)] <= 1500
                    px = p[np.clip(i, 0, None)]
                    rx = np.diff(np.log(px))
                    m = ok & fresh[1:] & fresh[:-1] & (rx != 0)
                    if m.sum() < 300:
                        continue
                    cc = float(np.corrcoef(rc[m], rx[m])[0, 1])
                    if cc > best[1]:
                        best = (int(lag), cc)
                cells.append("—" if best[0] is None else f"{best[0]:+d} ms / {best[1]:.2f}")
            print(f"| {a} | " + " | ".join(cells) + " |")


# ════════════════════════════════════ ticks ════════════════════════════════════

def ticks(args) -> None:
    raise SystemExit("ticks: see below")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("archive")
    a.add_argument("--chainlink", default="/root/Bo-BOTT/data/chainlink_ticks.jsonl")
    t = sub.add_parser("ticks")
    t.add_argument("--ticks", default=str(ROOT / "data/ticks/*.jsonl.gz"))
    args = ap.parse_args()
    {"archive": archive, "ticks": ticks}[args.cmd](args)


if __name__ == "__main__":
    main()
