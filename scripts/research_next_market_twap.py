"""Research: NEXT-MARKET TWAP prediction across the 5-minute market boundary.

    python scripts/research_next_market_twap.py cache      # compact arrays from data/charts
    python scripts/research_next_market_twap.py verify     # what settles the market, at the OPEN side
    python scripts/research_next_market_twap.py build      # per-boundary features, books, markouts

RESEARCH ONLY. It reads the chart archive and writes under data/replay/next_market/.
It changes no strategy configuration and no running process, and places no orders.

Hypothesis
----------
About 3 s before a new 5-minute UP/DOWN market opens, predict where its settlement
condition will land; when it opens, buy the UP or DOWN token if the executable edge
clears the costs; hold a few seconds; sell into the bid.

What settles the market (docs/60s_snipe_research.md, re-checked here by ``verify``)
-------------------------------------------------------------------------------------
UP iff TWAP60(close) >= TWAP60(open): the 60 s trailing average of the Chainlink stream
at the close against the same average at the open. The strike is therefore the trailing
average over [open - 60 s, open], and at T = open - 3 s 57 of its 60 seconds are already
observed. It is also the previous market's settlement value (the same instant).

Data (the archive the live bots write, data/charts/<slug>.json)
----------------------------------------------------------------
One point every ~224 ms from 60 s before the open to ~30 s after the close: composite
spot (Binance/Bybit/Coinbase median), each exchange's price, spot order flow, both
tokens' touch and the depth within 2 ticks of each touch. A point is what the bot had
LOCALLY at that instant (spot ~160-300 ms behind the exchanges, book ~50 ms behind the
venue), so the archive is itself a causal information set.

The cache stitches every file of one asset into ONE spot series (consecutive files
overlap by 90 s and are sampled on different ticks) and keeps each market's books from
60 s before to 70 s after its open.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import orjson

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "data/replay/next_market"
CACHE = OUT / "cache"
CHARTS = ROOT / "data/charts"
ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
BOOK_FROM_S = -60.0
BOOK_TO_S = 70.0
SPOT_COLS = ("spot", "bn", "byb", "cb", "tfi", "obi", "ofi", "rate")
BOOK_PX = ("ub", "ua", "db", "da")
BOOK_SZ = ("ubd", "uad", "dbd", "dad")
TWAP_S = 60.0


def _nan(x) -> float:
    return float("nan") if x is None else float(x)


def _cents(x) -> int:
    return -1 if x is None else int(round(float(x) * 100.0))


def _load(path: Path):
    try:
        return orjson.loads(path.read_bytes())
    except (OSError, orjson.JSONDecodeError):
        return None


def _build_asset(args) -> tuple[str, int, int]:
    asset, paths, last_open_ms, out_dir = args
    spot_t, spot = [], {k: [] for k in SPOT_COLS}
    bt, bpx, bsz = [], {k: [] for k in BOOK_PX}, {k: [] for k in BOOK_SZ}
    meta = []
    for p in paths:
        d = _load(p)
        if not d or not d.get("points"):
            continue
        o, c = float(d["open_ts"]), float(d["close_ts"])
        if o > last_open_ms:
            continue
        pts = sorted((q for q in d["points"] if q.get("t") is not None), key=lambda q: q["t"])
        b0 = len(bt)
        for q in pts:
            t = float(q["t"])
            if q.get("spot"):
                srcs = q.get("srcs") or {}
                of = q.get("of") or {}
                spot_t.append(t)
                spot["spot"].append(float(q["spot"]))
                spot["bn"].append(_nan(srcs.get("binance")))
                spot["byb"].append(_nan(srcs.get("bybit")))
                spot["cb"].append(_nan(srcs.get("coinbase")))
                spot["tfi"].append(_nan(of.get("tfi15")))
                spot["obi"].append(_nan(of.get("obi")))
                spot["ofi"].append(_nan(of.get("ofi")))
                spot["rate"].append(_nan(of.get("rate")))
            if o + BOOK_FROM_S * 1000.0 <= t <= o + BOOK_TO_S * 1000.0:
                bt.append(t)
                for k in BOOK_PX:
                    bpx[k].append(_cents(q.get(k)))
                for k in BOOK_SZ:
                    bsz[k].append(_nan(q.get(k)))
        src = d.get("outcome_source") or ""
        out = d.get("outcome")
        won = (1 if out == "UP" else 0) if (src == "venue" and out in ("UP", "DOWN")) else -1
        fee = d.get("fee") or {}
        meta.append((d["slug"], int(o // 1000), o, c, won, float(d.get("strike") or 0.0),
                     float(fee.get("rate", 0.07)), b0, len(bt), float(pts[0]["t"]), float(pts[-1]["t"])))
    t = np.asarray(spot_t)
    order = np.argsort(t, kind="stable")
    t = t[order]
    keep = np.concatenate([[True], np.diff(t) > 0]) if len(t) else np.zeros(0, bool)
    arrays = {"st": t[keep]}
    for k in SPOT_COLS:
        arrays["s_" + k] = np.asarray(spot[k], dtype=np.float64)[order][keep]
    arrays["bt"] = np.asarray(bt, dtype=np.float64)
    for k in BOOK_PX:
        arrays[k] = np.asarray(bpx[k], dtype=np.int16)
    for k in BOOK_SZ:
        arrays[k] = np.asarray(bsz[k], dtype=np.float32)
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_dir / f"{asset}.npz", **arrays, meta=np.array(meta, dtype=object))
    return asset, len(meta), int(keep.sum())


def build_cache(last_open_s: int | None = None, procs: int = 3) -> dict:
    charts = sorted(CHARTS.glob("*-updown-5m-*.json"))
    epochs = sorted({int(p.stem.rsplit("-", 1)[-1]) for p in charts})
    # a window is usable once the venue has had time to resolve it (close + 40 s)
    last = last_open_s if last_open_s is not None else epochs[-1]
    by: dict[str, list] = {}
    for p in charts:
        by.setdefault(p.stem.split("-")[0].upper(), []).append(p)
    res = {}
    with ProcessPoolExecutor(procs) as pool:
        jobs = [(a, sorted(ps), last * 1000.0, CACHE) for a, ps in sorted(by.items())]
        for asset, nm, ns in pool.map(_build_asset, jobs):
            res[asset] = (nm, ns)
            print(f"  {asset}: {nm} markets, {ns} spot points", flush=True)
    (CACHE / "manifest.json").write_text(json.dumps({"last_open_s": last, "assets": res}, indent=1))
    return res


# ─────────────────────────────── loading ───────────────────────────────


class Asset:
    """One asset's stitched spot series and its markets' books."""

    def __init__(self, asset: str, cache_dir: Path = CACHE) -> None:
        z = np.load(Path(cache_dir) / f"{asset}.npz", allow_pickle=True)
        self.asset = asset
        self.st = z["st"]
        self.s = {k: z["s_" + k] for k in SPOT_COLS}
        self.lx = np.log(self.s["spot"])
        self.bt = z["bt"]
        self.px = {k: z[k].astype(np.float64) / 100.0 for k in BOOK_PX}
        for k in BOOK_PX:
            self.px[k][z[k] < 0] = np.nan
        self.sz = {k: z[k].astype(np.float64) for k in BOOK_SZ}
        self.meta = z["meta"]
        self.by_epoch = {int(m[1]): k for k, m in enumerate(self.meta)}
        self._cum = np.concatenate([[0.0], np.cumsum(0.5 * (self.lx[1:] + self.lx[:-1]) * np.diff(self.st) / 1000.0)])

    def log_integral(self, a_ms: float, b_ms: float, x: np.ndarray | None = None) -> tuple[float, float]:
        """Integral of the linearly interpolated log price over [a, b] (value*s) and the covered seconds.
        Uses only points inside [first, last] of the series; the caller checks coverage."""
        t = self.st
        if x is None:
            x, cum = self.lx, self._cum
        else:
            cum = np.concatenate([[0.0], np.cumsum(0.5 * (x[1:] + x[:-1]) * np.diff(t) / 1000.0)])
        a, b = max(a_ms, t[0]), min(b_ms, t[-1])
        if b <= a:
            return 0.0, 0.0

        def upto(q):
            j = min(max(int(np.searchsorted(t, q, side="right")) - 1, 0), len(t) - 2)
            span = t[j + 1] - t[j]
            f = x[j] + (x[j + 1] - x[j]) * ((q - t[j]) / span if span > 0 else 0.0)
            return cum[j] + 0.5 * (x[j] + f) * (q - t[j]) / 1000.0

        return upto(b) - upto(a), (b - a) / 1000.0

    def max_gap_s(self, a_ms: float, b_ms: float) -> float:
        i0 = max(int(np.searchsorted(self.st, a_ms, side="right")) - 1, 0)
        i1 = int(np.searchsorted(self.st, b_ms, side="left"))
        seg = self.st[i0:i1 + 1]
        if len(seg) < 2:
            return float("inf")
        return float(np.max(np.diff(seg)) / 1000.0)

    def twap(self, end_ms: float, window_s: float = TWAP_S, x: np.ndarray | None = None) -> float | None:
        """Trailing time-weighted average PRICE (in log space, exponentiated) over [end - w, end]."""
        integ, cov = self.log_integral(end_ms - window_s * 1000.0, end_ms, x)
        if cov < window_s - 1.0 or self.max_gap_s(end_ms - window_s * 1000.0, end_ms) > 5.0:
            return None
        return math.exp(integ / cov)          # the mean over the covered span: no level bias

    def spot_at(self, ts_ms: float, x: np.ndarray | None = None) -> float | None:
        j = int(np.searchsorted(self.st, ts_ms, side="right")) - 1
        if j < 0 or ts_ms - self.st[j] > 5000.0:
            return None
        return float(math.exp((self.lx if x is None else x)[j]))


def load_assets(assets=ASSETS) -> dict[str, Asset]:
    return {a: Asset(a) for a in assets}


# ─────────────────────────────── settlement check ───────────────────────────────


def verify_settlement(assets=ASSETS) -> dict:
    """Which strike definition agrees with the venue's resolved outcomes?

    The close side is fixed at the 60 s trailing TWAP at the close (docs/60s_snipe_research.md).
    The open side is the question for THIS strategy: the trailing average ending at the open,
    the average over the market's first 60 s, or the spot at the open."""
    rules = ("twap_trailing_open", "twap_first_60s", "spot_open", "bot_strike")
    rows = {r: [0, 0] for r in rules}
    by_src = {}
    for a in assets:
        A = Asset(a)
        xs = {"composite": A.lx}
        for src, key in (("binance", "bn"), ("coinbase", "cb")):
            v = A.s[key]
            ok = v > 0
            if ok.sum() > 0.9 * len(v):
                # forward-fill the few holes so the series is complete
                idx = np.where(ok, np.arange(len(v)), 0)
                np.maximum.accumulate(idx, out=idx)
                xs[src] = np.log(np.where(ok, v, v[idx]).clip(1e-12))
        for m in A.meta:
            won = int(m[4])
            if won < 0:
                continue
            o, c = float(m[2]), float(m[3])
            for src, x in xs.items():
                close_tw = A.twap(c, x=x)
                if close_tw is None:
                    continue
                cand = {
                    "twap_trailing_open": A.twap(o, x=x),
                    "twap_first_60s": A.twap(o + 60_000.0, x=x),
                    "spot_open": A.spot_at(o, x=x),
                    "bot_strike": float(m[5]) or None,
                }
                for r, k in cand.items():
                    if k is None:
                        continue
                    hit = int((close_tw >= k) == bool(won))
                    s = by_src.setdefault((src, r), [0, 0])
                    s[0] += hit
                    s[1] += 1
                    if src == "composite":
                        rows[r][0] += hit
                        rows[r][1] += 1
    out = {f"{src}:{r}": {"agree": v[0] / v[1], "n": v[1]} for (src, r), v in sorted(by_src.items())}
    for k, v in out.items():
        print(f"  {k:40s} agree {v['agree']:.4f}  n={v['n']}")
    (OUT / "settlement_check.json").write_text(json.dumps(out, indent=1))
    return out


# ─────────────────────────────── the boundary dataset ───────────────────────────────

SPLIT_NAMES = ("train", "validation", "test", "forward")
#: Fixed at the first build (2026-09-25 14:05 UTC, 764 epochs to 1790342400): the oldest 60 % of
#: epochs TRAIN, the next 20 % VALIDATION, the last 20 % TEST. Everything archived after the cache
#: cutoff is FORWARD, examined only after the configuration was locked. Every asset of one epoch
#: lands in the same period (they are one bet).
TRAIN_LAST = 1790248200
VALIDATION_LAST = 1790296500
TEST_LAST = 1790342400


def split_of(epochs) -> dict[int, str]:
    out = {}
    for e in set(int(x) for x in epochs):
        out[e] = ("train" if e <= TRAIN_LAST else "validation" if e <= VALIDATION_LAST
                  else "test" if e <= TEST_LAST else "forward")
    return out


def stitched_sigma(A: Asset) -> np.ndarray:
    """The project's TwoScaleVol per-second sigma after each spot point, fed in time order and
    restarted after a gap of more than 120 s (NaN until warm)."""
    from troll_poly_bot.backtest.spot_shock_replay import _make_vol
    out = np.full(len(A.st), np.nan)
    vol, last = _make_vol(), -1e18
    px = A.s["spot"]
    for i in range(len(A.st)):
        t = float(A.st[i])
        if t - last > 120_000.0:
            vol = _make_vol()
        vol.update(float(px[i]), t)
        last = t
        if vol.ready:
            out[i] = vol.sigma_per_sec
    return out


def book_at(A: Asset, k: int, ts: float) -> int:
    """Index of market k's last book point at or before ``ts`` (-1 if none)."""
    b0, b1 = int(A.meta[k][7]), int(A.meta[k][8])
    j = int(np.searchsorted(A.bt[b0:b1], ts, side="right")) - 1
    return b0 + j if j >= 0 else -1


BOOK_FEATS = ("u_bid", "u_ask", "d_bid", "d_ask", "u_bdep", "u_adep", "d_bdep", "d_adep", "book_age_s",
              "mkt_p", "mkt_du3", "mkt_du10")


def _mkt_p(A: Asset, j: int) -> float:
    """The market's own P(UP) at book point j: the Up mid, else the complement of the Down mid."""
    ub, ua, db, da = (A.px[k][j] for k in BOOK_PX)
    if ub == ub and ua == ua:
        return (ub + ua) / 2.0
    if db == db and da == da:
        return 1.0 - (db + da) / 2.0
    return float("nan")


def book_features(A: Asset, k: int, ts: float) -> dict:
    j = book_at(A, k, ts)
    f = {c: float("nan") for c in BOOK_FEATS}
    if j < 0:
        return f
    f.update(u_bid=A.px["ub"][j], u_ask=A.px["ua"][j], d_bid=A.px["db"][j], d_ask=A.px["da"][j],
             u_bdep=A.sz["ubd"][j], u_adep=A.sz["uad"][j], d_bdep=A.sz["dbd"][j], d_adep=A.sz["dad"][j],
             book_age_s=(ts - A.bt[j]) / 1000.0, mkt_p=_mkt_p(A, j))
    for lag, name in ((3000.0, "mkt_du3"), (10000.0, "mkt_du10")):
        j0 = book_at(A, k, ts - lag)
        if j0 >= 0 and ts - lag - A.bt[j0] < 1500.0:
            f[name] = f["mkt_p"] - _mkt_p(A, j0)
    return f


def _build_rows(args):
    asset, split = args
    from troll_poly_bot.strategy.next_market_twap import PRED_OFFSETS_S, SpotView, boundary_features
    A = Asset(asset)
    sig = stitched_sigma(A)
    ex = {}
    for key in ("bn", "byb", "cb"):
        v = A.s[key]
        ex[key] = np.where(v > 0, np.log(np.where(v > 0, v, 1.0)), np.nan)
    view = SpotView(t=A.st, lx=A.lx, ex=ex, flow={k: A.s[k] for k in ("tfi", "obi", "ofi", "rate")})
    won_by_epoch = {int(m[1]): int(m[4]) for m in A.meta}
    rows, keys = [], None
    for k, m in enumerate(A.meta):
        slug, ep, o, c, won, strike, rate = m[0], int(m[1]), float(m[2]), float(m[3]), int(m[4]), float(m[5]), float(m[6])
        pk = A.by_epoch.get(ep - 300)
        prev_strike = float(A.meta[pk][5]) if pk is not None else None
        prev2 = won_by_epoch.get(ep - 600, -1)
        k_ours = A.twap(o)
        c_ours = A.twap(c)
        for tau in PRED_OFFSETS_S:
            tp = o + tau * 1000.0
            j = int(np.searchsorted(A.st, tp, side="right")) - 1
            s_now = float(sig[j]) if j >= 0 else float("nan")
            f = boundary_features(view, o, tp, s_now, prev_strike=prev_strike)
            if f is None:
                continue
            f.update(book_features(A, k, tp))
            f["prev2_up"] = float(prev2)
            f["lk_bot"] = math.log(strike) if strike > 0 else float("nan")
            f["lk_ours"] = math.log(k_ours) if k_ours else float("nan")
            f["lc_ours"] = math.log(c_ours) if c_ours else float("nan")
            if keys is None:
                keys = sorted(f)
            rows.append(([f[x] for x in keys], (ASSETS.index(asset), ep, k, tau, won, SPLIT_NAMES.index(split.get(ep, "test")), rate)))
    X = np.asarray([r[0] for r in rows], dtype=np.float64)
    M = np.asarray([r[1] for r in rows], dtype=np.float64)
    return asset, keys, X, M


META_COLS = ("asset", "epoch", "mk", "tau", "y", "split", "fee_rate")


def build_dataset(procs: int = 3) -> None:
    eps = []
    for a in ASSETS:
        z = np.load(CACHE / f"{a}.npz", allow_pickle=True)
        eps += [int(m[1]) for m in z["meta"]]
    split = split_of(eps)
    b = sorted(split)
    bounds = {s: (min(e for e in b if split[e] == s), max(e for e in b if split[e] == s),
                  sum(1 for e in b if split[e] == s)) for s in SPLIT_NAMES}
    print("splits:", bounds, flush=True)
    Xs, Ms, keys = [], [], None
    with ProcessPoolExecutor(procs) as pool:
        for asset, k, X, M in pool.map(_build_rows, [(a, split) for a in ASSETS]):
            if keys is None:
                keys = k
            assert k == keys
            Xs.append(X)
            Ms.append(M)
            print(f"  {asset}: {len(X)} rows", flush=True)
    np.savez_compressed(OUT / "dataset.npz", X=np.concatenate(Xs), M=np.concatenate(Ms),
                        cols=np.array(keys), meta_cols=np.array(META_COLS))
    (OUT / "splits.json").write_text(json.dumps(bounds, indent=1))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cache")
    c.add_argument("--last-open", type=int, default=None, help="last market open (epoch s) to include")
    c.add_argument("--procs", type=int, default=3)
    sub.add_parser("verify")
    b = sub.add_parser("build")
    b.add_argument("--procs", type=int, default=3)
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if args.cmd == "cache":
        build_cache(args.last_open, args.procs)
    elif args.cmd == "verify":
        verify_settlement()
    elif args.cmd == "build":
        build_dataset(args.procs)


if __name__ == "__main__":
    main()
