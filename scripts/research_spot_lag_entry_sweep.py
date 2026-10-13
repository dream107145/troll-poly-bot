"""Spot-lag ENTRY parameter sweep: shock size x net edge x price limit x spread x MAX_ASK x time left,
with the production exit and the realistic execution model. TRAINING chooses, VALIDATION
eliminates, the picks are LOCKED, then the unseen periods -- the protocol of
scripts/research_spot_lag_parameter_sweep.py, which settled MAX_ASK x exit.

    python scripts/research_spot_lag_entry_sweep.py build      # data/replay/sl_entry/cands.npz
    python scripts/research_spot_lag_entry_sweep.py sweep      # results/spot_lag_entry_sweep.csv
    python scripts/research_spot_lag_entry_sweep.py analyze    # _locked.json, _report.md

RESEARCH ONLY: reads the research feature cache (scripts/research_repriced_momentum.py cache),
writes data/replay/sl_entry/ and results/. Changes no strategy code, configuration or process.

Exactness. The fill model (backtest/spot_shock_replay.Execution.buy) fills a FOK at the venue's
ask on arrival when that ask is within the limit and the top level holds 5 shares. So ONE
simulated order per shock, sent with no limit, records the arrival ask and whether the depth
was there; any limit L then fills iff depth_ok and arrival_ask <= L, at the same price and the
same moment -- and the production exit (5 s, held to 10 s while the spot keeps moving) does not
depend on the limit. Each shock threshold k is detected on its own (detect_shocks, with its own
2 s dedupe, exactly as the engine applies it) and marked in a bitmask, so every k is exact too.
Only the time-left window is applied as a filter to the widest window's shocks (the engine checks
it before the dedupe; the difference is a shock at the window edge).
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import sys
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from troll_poly_bot.backtest.spot_shock_replay import SETTLE_GUARD_MS, SIZE, detect_shocks, load_asset, windows  # noqa: E402
import research_repriced_momentum as R                                               # noqa: E402
import research_spot_lag_parameter_sweep as S                                        # noqa: E402

CACHE = R.CACHE
OUT = ROOT / "data/replay/sl_entry"
RES = ROOT / "results"
ASSETS = R.ASSETS
SPLITS = R.SPLITS
#: the last window the parameter sweep read (09-25 00:45 UTC start); later windows = UNSEEN 4,
#: data no study has examined
FRESH4_AFTER = 1790297100
UNSEEN = ("unseen_1", "unseen_2", "unseen_3")
PERIODS = ("train", "validation") + UNSEEN + ("unseen_4",)
SLIP, UNC, MIN_ASK = 0.005, 0.005, 0.05
# the grid
K_SET = (2.5, 3.0, 3.5, 4.0, 5.0)
EDGES = (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08)
SLIPS = (0.0, 0.01, 0.02, 0.03, 0.04)
SPREADS = (0.02, 0.03, 0.04, 0.05)
MAX_ASKS = (0.50, 0.55, 0.60, 0.65, 0.70, 0.74, 0.80, 0.95)
WINDOWS = ((60, 295), (90, 295), (120, 295), (60, 240), (60, 200), (90, 240))
# loosest gates: a shock outside these can never trade in any configuration
LOOSE_SPREAD, LOOSE_ASK = max(SPREADS), 0.98
BASELINES = {
    "live bot (production)": (3.0, 0.02, 0.02, 0.03, 0.95, (60, 295)),
    "tpb-spotlag-paper-ma74": (3.0, 0.02, 0.02, 0.03, 0.74, (60, 295)),
    "tpb-spotlag-paper": (3.0, 0.01, 0.04, 0.05, 0.60, (60, 295)),
}
COLS = ("split", "epoch", "asset", "t", "dir", "ask", "bid", "ev", "rate", "left", "z1", "z3", "sigma_bps", "tok1",
        "kmask", "lat_ms", "known_ms", "arr_ask", "fill_ok", "xpx", "xhold", "xflag", "fresh4")


# ─────────────────────────────── build ───────────────────────────────


def build_asset(asset: str) -> np.ndarray:
    d, meta = load_asset(CACHE, asset)
    ex = R.ScaledExecution(seed=sum(map(ord, asset)) * 11 + 5, scale=1.0)
    rows = []
    for m, w in zip(meta, windows(d, meta)):
        if len(w) < 30:
            continue
        t = w.t
        r1 = R._ret(t, w.lp, 1000.0)
        k1000 = w.lagged_index(1000.0)
        marks: dict[tuple[int, int], int] = {}
        for b, k in enumerate(K_SET):
            for i, dr in detect_shocks(w, k=k, k3=0.5, accel=True):
                marks[(i, dr)] = marks.get((i, dr), 0) | (1 << b)
        z1a = w.ret(1000.0) / w.sigma
        z3a = w.ret(3000.0) / (w.sigma * math.sqrt(3.0))
        won_up = w.won_up
        for (i, dr), mask in sorted(marks.items()):
            sv = w.side["UP" if dr > 0 else "DOWN"]
            mid, fvs, ask, bid = sv["mid"], sv["mfv"], sv["ask"], sv["bid"]
            i0 = k1000[i]
            if i0 < 0 or t[i] - t[i0] > 1600.0 or mid[i0] != mid[i0] or fvs[i0] != fvs[i0] or ask[i] != ask[i] \
                    or bid[i] != bid[i] or fvs[i] != fvs[i]:
                continue
            a0, b0 = float(ask[i]), float(bid[i])
            ev = min(max(float(mid[i0]) + float(fvs[i] - fvs[i0]), 0.0), 1.0)
            loose = MIN_ASK <= a0 <= LOOSE_ASK and a0 - b0 <= LOOSE_SPREAD + 1e-9 and ev - a0 > 0
            row = [SPLITS.index(w.split) if w.split in SPLITS else -1, w.epoch, ASSETS.index(asset), float(t[i]), dr,
                   a0, b0, ev, w.rate, float(w.left[i]), float(dr * z1a[i]), float(dr * z3a[i]), float(w.sigma[i] * 1e4),
                   float(dr * (mid[i] - mid[i0])) if mid[i] == mid[i] else np.nan, float(mask)]
            won = None if won_up is None else float((dr > 0) == bool(won_up))
            if not loose:
                continue
            fl = ex.buy(w, sv, float(t[i]), 1.0)          # no limit: records the arrival ask and the depth
            arr = fl["arrival_ask"] if fl["arrival_ask"] is not None else np.nan
            xpx = xhold = xflag = np.nan
            if fl["filled"]:
                z1s = dr * r1 / w.sigma
                t_arr, t_known = fl["t_arr"], fl["t_known"]
                j = w.first_at(t_known)
                t_sell = None
                while j < len(w) and t[j] < w.close - SETTLE_GUARD_MS:
                    held = (t[j] - t_arr) / 1000.0
                    if held >= 5.0 and not (held < 10.0 and z1s[j] == z1s[j] and z1s[j] >= 0.5):
                        t_sell = float(t[j])
                        break
                    j += 1
                got = S._sell_at(w, ex, sv, t_sell, won) if t_sell is not None else \
                    ((won, "SETTLE", w.close, np.nan, np.nan) if won is not None else None)
                if got is not None and got[0] is not None:
                    xpx, xhold, xflag = float(got[0]), (got[2] - t_arr) / 1000.0, 2.0 if got[1] == "SETTLE" else 0.0
            row += [fl["latency_ms"], fl["t_known"] - t[i], arr, 1.0 if fl["filled"] else 0.0, xpx, xhold, xflag,
                    1.0 if w.epoch > FRESH4_AFTER else 0.0]
            rows.append(row)
    return np.array(rows, dtype=np.float64).reshape(-1, len(COLS))


def build(procs: int = 3) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(procs) as pool:
        parts = list(pool.map(build_asset, ASSETS))
    C = np.concatenate([p for p in parts if len(p)])
    C = C[np.argsort(C[:, COLS.index("t")], kind="stable")]
    for n in ("ask", "bid", "arr_ask", "xpx"):
        C[:, COLS.index(n)] = np.round(C[:, COLS.index(n)], 6)
    out = OUT / "cands.npz"
    np.savez_compressed(out, C=C, cols=np.array(COLS))
    return out


# ─────────────────────────────── sweep ───────────────────────────────


class Data:
    def __init__(self) -> None:
        z = np.load(OUT / "cands.npz")
        self.C = z["C"]
        self.c = {n: i for i, n in enumerate(COLS)}
        C, c = self.C, self.c
        keep = C[:, c["asset"]] != ASSETS.index("HYPE")          # production excludes HYPE
        self.C = C = C[keep]
        self.col = {n: C[:, i] for n, i in c.items()}
        a, b, ev, rate = self.col["ask"], self.col["bid"], self.col["ev"], self.col["rate"]
        evc = np.clip(ev, 0.01, 0.99)
        fixed = SLIP + UNC + rate * evc * (1 - evc) + (a - b) / 2.0
        self.me = {e: self._max_entry(ev - fixed - e, rate) for e in EDGES}
        # period of each candidate
        split = self.col["split"].astype(int)
        per = np.array([SPLITS[s] if s >= 0 else "none" for s in split], dtype=object)
        per[self.col["fresh4"] == 1.0] = "unseen_4"
        self.period = per
        self.hours = {p: len(np.unique(self.col["epoch"][per == p])) / 12.0 for p in PERIODS}
        fee = lambda p: rate * p * (1 - p)                                              # noqa: E731
        arr, xpx, flag = self.col["arr_ask"], self.col["xpx"], self.col["xflag"]
        self.net = xpx - arr - fee(arr) - np.where(flag == 0.0, fee(xpx), 0.0)
        self.tradable = (self.col["fill_ok"] == 1.0) & ~np.isnan(xpx)

    @staticmethod
    def _max_entry(budget: np.ndarray, rate: np.ndarray) -> np.ndarray:
        """Highest cent p with p + fee(p) <= budget (vectorised S._max_entry; fee <= 1.75c, so at most 3 steps)."""
        p0 = np.floor((np.minimum(budget, 0.99) + 1e-9) * 100) / 100
        out = np.full(budget.shape, np.nan)
        for s in (3, 2, 1, 0):
            p = np.round(p0 - s * 0.01, 2)
            ok = (p >= 0.01) & (p + rate * p * (1 - p) <= budget + 1e-12)
            out = np.where(ok, p, out)
        return out


def configs():
    return list(itertools.product(K_SET, EDGES, SLIPS, SPREADS, MAX_ASKS, WINDOWS))


def simulate(d: Data, cfg, positions=4, exposure=10.0, per_hour=60, attempts_max=3, retry_ms=2000.0):
    """Signals of one configuration through the production budget in time order. Returns
    (idx of orders, filled flags) -- the same rules as research_spot_lag_parameter_sweep.simulate."""
    k, edge, slip, spread, max_ask, (lo, hi) = cfg
    col = d.col
    me = d.me[edge]
    bit = 1 << K_SET.index(k)
    ask = col["ask"]
    sig = (((col["kmask"].astype(int) & bit) > 0) & (col["left"] >= lo) & (col["left"] <= hi)
           & (ask >= MIN_ASK) & (ask <= max_ask + 1e-9) & (ask - col["bid"] <= spread + 1e-9)
           & (col["ev"] - ask > 0) & (me >= ask - 1e-9))
    idx = np.nonzero(sig)[0]
    cap = np.minimum(np.round(ask[idx] + slip, 2), me[idx])
    fill = d.tradable[idx] & (col["arr_ask"][idx] <= cap + 1e-9)
    t, asset, epoch = col["t"][idx], col["asset"][idx], col["epoch"][idx]
    known, lat, hold, arr = col["known_ms"][idx], col["lat_ms"][idx], col["xhold"][idx], col["arr_ask"][idx]
    busy: list = []
    buys: deque = deque()
    att: dict = defaultdict(int)
    entered, last = set(), {}
    o_idx, o_fill = [], []
    for j in range(len(idx)):
        t0 = t[j]
        mk = (asset[j], epoch[j])
        if mk in entered or att[mk] >= attempts_max or t0 - last.get(mk, -1e18) < retry_ms:
            continue
        if busy:
            busy = [b for b in busy if b[0] > t0]
        if len(busy) >= positions or sum(b[1] for b in busy) + cap[j] * SIZE > exposure + 1e-9:
            continue
        while buys and buys[0] <= t0 - 3_600_000.0:
            buys.popleft()
        if len(buys) >= per_hour:
            continue
        buys.append(t0)
        att[mk] += 1
        last[mk] = t0
        o_idx.append(idx[j])
        o_fill.append(bool(fill[j]))
        if fill[j]:
            entered.add(mk)
            busy.append((t0 + lat[j] + hold[j] * 1000.0, arr[j] * SIZE))
        else:
            busy.append((t0 + known[j], cap[j] * SIZE))
    return np.array(o_idx, dtype=int), np.array(o_fill, dtype=bool)


def score(d: Data, o_idx, o_fill, period_mask=None) -> dict:
    if period_mask is not None:
        keep = period_mask[o_idx]
        o_idx, o_fill = o_idx[keep], o_fill[keep]
    orders = len(o_idx)
    f = o_idx[o_fill]
    out = {"orders": orders, "fills": len(f)}
    if len(f) == 0:
        return out
    net = d.net[f]
    ep = d.col["epoch"][f]
    u, inv = np.unique(ep, return_inverse=True)
    v = np.bincount(inv, weights=net)
    se = v.std(ddof=1) / math.sqrt(len(v)) if len(v) > 2 else float("nan")
    cum = np.concatenate([[0.0], np.cumsum(net * SIZE)])
    out.update(net_usdc=float(net.sum() * SIZE), c_share=float(net.mean() * 100), win=float((net > 0).mean()),
               t=float(v.mean() / se) if se == se and se > 0 else float("nan"),
               max_dd=float(np.max(np.maximum.accumulate(cum) - cum)), avg_ask=float(d.col["arr_ask"][f].mean()))
    return out


def _sweep_chunk(cfgs):
    d = Data()
    masks = {p: d.period == p for p in PERIODS}
    masks["unseen"] = np.isin(d.period, UNSEEN)
    masks["all"] = np.isin(d.period, SPLITS)
    rows = []
    for cfg in cfgs:
        o_idx, o_fill = simulate(d, cfg)
        k, edge, slip, spread, max_ask, (lo, hi) = cfg
        base = {"k": k, "edge": edge, "slip": slip, "spread": spread, "max_ask": max_ask, "min_left": lo, "max_left": hi}
        for p, m in masks.items():
            s = score(d, o_idx, o_fill, m)
            hours = sum(d.hours[q] for q in UNSEEN) if p == "unseen" else \
                sum(d.hours[q] for q in SPLITS) if p == "all" else d.hours[p]
            rows.append({**base, "period": p, **s, "fills_per_h": s["fills"] / hours if hours else float("nan")})
    return rows


def sweep(procs: int = 3) -> Path:
    import pandas as pd
    cf = configs()
    chunks = [cf[i::procs * 4] for i in range(procs * 4)]
    with ProcessPoolExecutor(procs) as pool:
        parts = list(pool.map(_sweep_chunk, chunks))
    df = pd.DataFrame([r for p in parts for r in p])
    RES.mkdir(exist_ok=True)
    out = RES / "spot_lag_entry_sweep.csv"
    df.to_csv(out, index=False)
    return out


# ─────────────────────────────── select, lock, report ───────────────────────────────

KEYS = ["k", "edge", "slip", "spread", "max_ask", "min_left", "max_left"]


def _neighbours_mean(tr, col):
    """Training metric averaged over the configuration and its grid neighbours (one step in one
    parameter): a pick on a plateau, not on a spike."""
    grids = {"k": K_SET, "edge": EDGES, "slip": SLIPS, "spread": SPREADS, "max_ask": MAX_ASKS}
    val = tr[col].to_dict()
    out = {}
    for key in tr.index:
        cfg = dict(zip(KEYS, key))
        vals = [val[key]]
        for p, g in grids.items():
            i = g.index(cfg[p])
            for j in (i - 1, i + 1):
                if 0 <= j < len(g):
                    nb = dict(cfg, **{p: g[j]})
                    kk = tuple(nb[x] for x in KEYS)
                    if kk in val:
                        vals.append(val[kk])
        out[key] = float(np.nanmean(vals))
    return out


def select(df):
    tr = df[df.period == "train"].set_index(KEYS)
    va = df[df.period == "validation"].set_index(KEYS)
    tr = tr.assign(t=tr.t.fillna(-99), net_usdc=tr.net_usdc.fillna(0))
    tr["t_smooth"] = tr.index.map(_neighbours_mean(tr, "t"))
    elig = tr[(tr.fills >= 30) & (tr.net_usdc > 0)]

    def pick(frame, key):
        for idx in frame.sort_values(key, ascending=False).index:
            v = va.loc[idx]
            if v.fills >= 5 and (v.net_usdc if v.net_usdc == v.net_usdc else 0) > 0:
                return idx
        return None
    sig = elig[elig.t >= 2.0]
    live = {"k": 3.0, "slip": 0.02, "spread": 0.03, "min_left": 60, "max_left": 295}
    only_edge = elig[np.all([elig.index.get_level_values(p) == v for p, v in live.items()], axis=0)]
    cats = {
        "risk-adjusted (training t)": pick(elig, "t"),
        "plateau (neighbour-averaged training t)": pick(elig, "t_smooth"),
        "high-volume (most fills, t >= 2)": pick(sig, "fills"),
        "high-quality (c/share)": pick(elig, "c_share"),
        "edge and MAX_ASK only (rest as live)": pick(only_edge, "t"),
    }
    return {k: [float(x) for x in v] if v is not None else None for k, v in cats.items()}


def analyze() -> None:
    import pandas as pd
    df = pd.read_csv(RES / "spot_lag_entry_sweep.csv")
    for c in ("min_left", "max_left"):
        df[c] = df[c].astype(int)
    lock_file = RES / "spot_lag_entry_sweep_locked.json"
    chosen = select(df)
    if lock_file.exists():
        old = json.loads(lock_file.read_text())
        if old["candidates"] != chosen:
            sys.exit(f"selection differs from the lock in {lock_file}; delete it to re-lock on purpose")
        locked_at = old["locked_at"]
    else:
        locked_at = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        lock_file.write_text(json.dumps({"candidates": chosen, "locked_at": locked_at, "keys": KEYS,
                                         "rule": "TRAINING >= 30 fills, net > 0; VALIDATION >= 5 fills and net > 0"},
                                        indent=1))
    d = Data()
    cands = dict(BASELINES)
    cands.update({n: (v[0], v[1], v[2], v[3], v[4], (int(v[5]), int(v[6]))) for n, v in chosen.items() if v})
    L: list[str] = []
    P = L.append
    P("# Spot-lag entry sweep: tables\n")
    P(f"Candidates (shocks at k >= 2.5 passing the loosest gates, HYPE excluded): {len(d.C)}. Configurations: "
      f"{len(configs())}. Hours: " + ", ".join(f"{p} {d.hours[p]:.1f}" for p in PERIODS) + "\n")
    P(f"Locked {locked_at}: `{json.dumps(chosen)}`\n")
    P("Configuration = (k sigma, min net edge, max slippage, max spread, MAX_ASK, time left)\n")
    rng = np.random.default_rng(20260925)
    P("## Candidates by period (c/share, fills)\n")
    P("| candidate | config | train | validation | unseen_1 | unseen_2 | unseen_3 | **unseen pooled** | t | bootstrap 95% CI | "
      "unseen_4 (fresh) | fills/h (all) |")
    P("|---|---|---|---|---|---|---|---|---:|---|---|---:|")
    masks = {p: d.period == p for p in PERIODS}
    masks["unseen"] = np.isin(d.period, UNSEEN)
    masks["all"] = np.isin(d.period, SPLITS)
    hours_all = sum(d.hours[q] for q in SPLITS)
    for name, cfg in cands.items():
        o_idx, o_fill = simulate(d, cfg)
        cells = []
        for p in ("train", "validation") + UNSEEN + ("unseen",):
            s = score(d, o_idx, o_fill, masks[p])
            cells.append(f"{s.get('c_share', float('nan')):+.1f} ({s['fills']})")
        s = score(d, o_idx, o_fill, masks["unseen"])
        f = o_idx[o_fill & masks["unseen"][o_idx]]
        ci = "–"
        if len(f) >= 5:
            ep = d.col["epoch"][f]
            u, inv = np.unique(ep, return_inverse=True)
            v = np.bincount(inv, weights=d.net[f])
            boots = [rng.choice(v, len(v)).sum() / len(f) * 100 for _ in range(2000)]
            ci = f"[{np.percentile(boots, 2.5):+.1f}, {np.percentile(boots, 97.5):+.1f}]"
        s4 = score(d, o_idx, o_fill, masks["unseen_4"])
        sa = score(d, o_idx, o_fill, masks["all"])
        cells_s = " | ".join(cells[:-1]) + f" | **{cells[-1]}**"
        P(f"| {name} | {cfg} | {cells_s} | {s.get('t', float('nan')):+.1f} | {ci} | "
          f"{s4.get('c_share', float('nan')):+.1f} ({s4['fills']}) | {sa['fills'] / hours_all:.1f} |")
    P("")

    # one parameter at a time, from the live rule and from the ma74 bot
    for base_name in ("live bot (production)", "tpb-spotlag-paper-ma74"):
        base = cands[base_name]
        P(f"## One parameter at a time from {base_name} {base} (c/share, fills: train | unseen pooled | all)\n")
        P("| parameter | value | train | unseen | all | fills/h | max DD (all) |")
        P("|---|---|---|---|---|---:|---:|")
        grids = [("k", 0, K_SET), ("edge", 1, EDGES), ("slip", 2, SLIPS), ("spread", 3, SPREADS),
                 ("max_ask", 4, MAX_ASKS), ("window", 5, WINDOWS)]
        for pname, pos, grid in grids:
            for val in grid:
                cfg = list(base)
                cfg[pos] = val
                o_idx, o_fill = simulate(d, tuple(cfg))
                c = [score(d, o_idx, o_fill, masks[p]) for p in ("train", "unseen", "all")]
                mark = " **(current)**" if val == base[pos] else ""
                P(f"| {pname} | {val}{mark} | " + " | ".join(f"{x.get('c_share', float('nan')):+.1f} ({x['fills']})" for x in c)
                  + f" | {c[2]['fills'] / hours_all:.1f} | {c[2].get('max_dd', float('nan')):.2f} |")
        P("")

    # how often does the training ranking hold out of sample?
    tr = df[df.period == "train"].set_index(KEYS)
    un = df[df.period == "unseen"].set_index(KEYS)
    ok = tr[(tr.fills >= 30) & (tr.net_usdc > 0)]
    both = ok.join(un[["c_share", "fills", "net_usdc"]], rsuffix="_un")
    P("## Does training rank predict unseen? (configs with >= 30 training fills)\n")
    P(f"Spearman rank correlation, training c/share vs unseen c/share: "
      f"{both.c_share.rank().corr(both.c_share_un.rank()):+.2f}; training t vs unseen c/share: "
      f"{both.t.rank().corr(both.c_share_un.rank()):+.2f} (n = {len(both)})\n")
    both["dec"] = pd.qcut(both.t.rank(method="first"), 10, labels=False)
    P("| training-t decile | configs | train c/share (median) | unseen c/share (median) | unseen fills (median) |")
    P("|---:|---:|---:|---:|---:|")
    for dcl, g in both.groupby("dec"):
        P(f"| {dcl + 1} | {len(g)} | {g.c_share.median():+.1f} | {g.c_share_un.median():+.1f} | {g.fills_un.median():.0f} |")
    P("")

    # the paper bot's own run against the replay of its configuration
    t0 = dt.datetime(2026, 9, 25, 1, 44).timestamp() * 1000
    o_idx, o_fill = simulate(d, cands["tpb-spotlag-paper-ma74"])
    m = d.col["t"][o_idx] >= t0
    ff = o_idx[o_fill & m]
    P("## The ma74 paper bot's own run, replayed\n")
    P(f"Replay of its configuration from 01:44 UTC to the end of the data: {int(m.sum())} orders, {len(ff)} fills, "
      f"net {d.net[ff].sum() * SIZE:+.2f} USDC" + (", trades: " + "; ".join(
          f"{dt.datetime.utcfromtimestamp(d.col['t'][i] / 1000):%H:%M:%S} {ASSETS[int(d.col['asset'][i])]} "
          f"{'UP' if d.col['dir'][i] > 0 else 'DOWN'} @{d.col['arr_ask'][i]:.2f} -> {d.col['xpx'][i]:.2f} "
          f"({d.net[i] * 100:+.1f}c)" for i in ff) if len(ff) else "") + "\n")
    out = RES / "spot_lag_entry_sweep_report.md"
    out.write_text("\n".join(L) + "\n")
    print("\n".join(L))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--procs", type=int, default=3)
    s = sub.add_parser("sweep")
    s.add_argument("--procs", type=int, default=3)
    sub.add_parser("analyze")
    args = ap.parse_args()
    if args.cmd == "build":
        print("built", build(args.procs))
    elif args.cmd == "sweep":
        print("wrote", sweep(args.procs))
    else:
        analyze()


if __name__ == "__main__":
    main()
