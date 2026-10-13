"""Spot-lag execution edge: earlier trigger x fill size x hold-to-settlement, under the realistic
execution model, validated TRAIN / VALIDATION / TEST / FORWARD (2026-09-29 brief).

    python scripts/research_spot_lag_fast.py build [--draws 4]   # data/replay/sl_fast/cands.npz
    python scripts/research_spot_lag_fast.py analyze             # results/spot_lag_fast_report.md

RESEARCH ONLY. Reads the research cache; changes no strategy code or process.

Triggers (all: one signal per market and direction per 2 s, 60-295 s left, the production
pricing and gates -- 2c edge, limit ask + 2c, spread <= 3c, ask <= 0.60 -- and the same fill model):
    base        the live rule: 1 s spot return >= 3 sigma, the 3 s return agrees (>= 0.5 sigma,
                sqrt(3)-scaled), accelerating
    w500_k      the 500 ms spot return >= k sigma (sqrt(0.5)-scaled) with the 1 s return in the same
                direction: the same move seen roughly half a second earlier
    w250_k      the 250 ms return (one archive point) >= k sigma: the first tick
Fill sizes: fixed 10 (FOK, the top level must hold 10), fixed 5, and ADAPTIVE (10 if the top level
holds 10, else what it holds down to 5 -- "take what the touch holds"). Exits: the production rule
(5 s, to 10 s while the spot keeps moving), and HOLD (no exit: the position settles at 1 / 0, one fee).
Execution: research_spot_lag_realistic.RealExec (measured latency samples, venue book = archive
+500 ms, FOK against top-level depth, taker fees both legs; settlement free).
"""
from __future__ import annotations

import argparse
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
from troll_poly_bot.backtest.spot_shock_replay import SETTLE_GUARD_MS, detect_shocks, load_asset, windows   # noqa: E402
import research_repriced_momentum as R                                                                 # noqa: E402
import research_spot_lag_realistic as S                                                                # noqa: E402

OUT = ROOT / "data/replay/sl_fast"
RES = ROOT / "results"
ASSETS = R.ASSETS
#: period edges (ms): TRAIN < VAL_FROM <= VALIDATION < TEST_FROM <= TEST < FWD_FROM <= FORWARD
VAL_FROM, TEST_FROM, FWD_FROM = 1790232300e3, 1790297100e3, 1790390100e3     # 09-24 06:45, 09-25 00:45, 09-26 02:35 UTC
PERIODS = ("train", "validation", "test", "forward")
EDGE, SLIP, SPREAD, MAX_ASK, MIN_ASK = 0.02, 0.02, 0.03, 0.60, 0.05
K_FAST = (2.5, 3.0, 3.5, 4.0, 5.0)
TRIGGERS = ("base",) + tuple(f"w500_{k:g}" for k in K_FAST) + tuple(f"w250_{k:g}" for k in K_FAST)
SIZES = (5, 10)
CAND = ("epoch", "asset", "t", "dir", "ask", "bid", "ev", "rate", "left", "z1", "z500", "z250", "adep", "tmask", "won")
DRAW = ("t_arr", "t_known", "arr_ask", "arr_adep", "x5", "h5", "f5", "x10", "h10", "f10", "book", "proc", "spot")


def _detect(w, r, scale, k, dedupe_ms=2000.0):
    """Shocks on a return series: |r| >= k * sigma * scale, one per direction per 2 s, 60-295 s left."""
    z = r / (w.sigma * scale)
    last = {1: -1e18, -1: -1e18}
    out = []
    for i in np.where(np.abs(z) >= k)[0]:
        if not (60.0 <= w.left[i] <= 295.0):
            continue
        d = 1 if z[i] > 0 else -1
        if w.t[i] - last[d] < dedupe_ms:
            continue
        last[d] = w.t[i]
        out.append((int(i), d))
    return out


def build_asset(args):
    asset, draws = args
    d, meta = load_asset(R.CACHE, asset)
    ex = S.RealExec(seed=sum(map(ord, asset)) * 17 + 1, scale=1.0, proxy_ms=500.0)
    C, D = [], []
    for m, w in zip(meta, windows(d, meta)):
        if len(w) < 30:
            continue
        t = w.t
        r1, r500, r250 = w.ret(1000.0), w.ret(500.0), w.ret(250.0)
        z1, z500, z250 = r1 / w.sigma, r500 / (w.sigma * math.sqrt(0.5)), r250 / (w.sigma * math.sqrt(0.25))
        k1000 = w.lagged_index(1000.0)
        marks: dict[tuple[int, int], int] = {}
        for i, dr in detect_shocks(w, k=3.0, k3=0.5, accel=True):
            marks[(i, dr)] = marks.get((i, dr), 0) | 1
        for b, k in enumerate(K_FAST):
            for i, dr in _detect(w, r500, math.sqrt(0.5), k):
                if r1[i] == r1[i] and r1[i] * dr >= 0:                     # the 1 s return agrees in sign
                    marks[(i, dr)] = marks.get((i, dr), 0) | (1 << (1 + b))
            for i, dr in _detect(w, r250, math.sqrt(0.25), k):
                marks[(i, dr)] = marks.get((i, dr), 0) | (1 << (1 + len(K_FAST) + b))
        won_up = w.won_up
        for (i, dr), mask in sorted(marks.items()):
            side = "UP" if dr > 0 else "DOWN"
            sv = w.side[side]
            mid, fvs, ask, bid, adep = sv["mid"], sv["mfv"], sv["ask"], sv["bid"], sv["adep"]
            i0 = k1000[i]
            if i0 < 0 or t[i] - t[i0] > 1600.0 or mid[i0] != mid[i0] or fvs[i0] != fvs[i0] or ask[i] != ask[i] \
                    or bid[i] != bid[i] or fvs[i] != fvs[i]:
                continue
            a0, b0 = float(ask[i]), float(bid[i])
            ev = min(max(float(mid[i0]) + float(fvs[i] - fvs[i0]), 0.0), 1.0)
            if not (MIN_ASK <= a0 <= MAX_ASK and a0 - b0 <= SPREAD + 1e-9 and ev - a0 > 0):
                continue
            won = -1.0 if won_up is None else float((dr > 0) == bool(won_up))
            C.append([w.epoch, ASSETS.index(asset), float(t[i]), dr, a0, b0, ev, w.rate, float(w.left[i]),
                      float(dr * z1[i]) if z1[i] == z1[i] else np.nan, float(dr * z500[i]) if z500[i] == z500[i] else np.nan,
                      float(dr * z250[i]) if z250[i] == z250[i] else np.nan, float(adep[i] or 0.0), float(mask), won])
            Dn = np.full((draws, len(DRAW)), np.nan)
            for q in range(draws):
                L = ex.draw()
                t_dec = t[i] + L["proc"]
                t_arr = t_dec + L["pre"] + L["rtt"] / 2.0
                t_known = t_arr + L["rtt"] / 2.0
                ja = w.first_at(t_arr + 500.0)
                row = [t_arr, t_known, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, L["book"], L["proc"], L["spot"]]
                if ja < len(w) and t[ja] < w.close - SETTLE_GUARD_MS and ask[ja] == ask[ja]:
                    row[2], row[3] = float(ask[ja]), float(adep[ja] or 0.0)
                    for s_i, size in enumerate(SIZES):
                        if row[3] < size:
                            continue
                        ex.size = float(size)
                        got = S._live_exit(w, ex, sv, t_arr, t_known, dr, r1, None if won < 0 else won)
                        if got is not None:
                            row[4 + 3 * s_i], row[5 + 3 * s_i], row[6 + 3 * s_i] = got[0], got[1], got[2]
                    ex.size = 5.0
                Dn[q] = row
            D.append(Dn)
    return np.array(C, dtype=np.float64).reshape(-1, len(CAND)), np.array(D).reshape(-1, draws, len(DRAW))


def build(draws: int, procs: int = 3) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(procs) as pool:
        parts = list(pool.map(build_asset, [(a, draws) for a in ASSETS]))
    C = np.concatenate([p[0] for p in parts if len(p[0])])
    D = np.concatenate([p[1] for p in parts if len(p[1])])
    o = np.argsort(C[:, CAND.index("t")], kind="stable")
    out = OUT / "cands.npz"
    np.savez_compressed(out, C=C[o], D=D[o])
    return out


# ─────────────────────────────── simulation ───────────────────────────────


class Data:
    def __init__(self) -> None:
        z = np.load(OUT / "cands.npz")
        C, D = z["C"], z["D"]
        keep = C[:, CAND.index("asset")] != ASSETS.index("HYPE")
        self.C, self.D = C[keep], D[keep]
        self.col = {n: self.C[:, i] for i, n in enumerate(CAND)}
        self.d = {n: i for i, n in enumerate(DRAW)}
        self.draws = self.D.shape[1]
        for n in ("ask", "bid"):
            self.col[n][:] = np.round(self.col[n], 6)
        self.D[:, :, self.d["arr_ask"]] = np.round(self.D[:, :, self.d["arr_ask"]], 6)
        a, b, ev, rate = self.col["ask"], self.col["bid"], self.col["ev"], self.col["rate"]
        evc = np.clip(ev, 0.01, 0.99)
        fixed = 0.005 + 0.005 + rate * evc * (1 - evc) + (a - b) / 2.0
        self.me = S.Data._max_entry(ev - fixed - EDGE, rate)
        self.cap = np.minimum(np.round(a + SLIP, 2), self.me)
        t = self.col["t"]
        self.period = np.where(t < VAL_FROM, "train", np.where(t < TEST_FROM, "validation", np.where(t < FWD_FROM, "test", "forward")))
        ep = self.col["epoch"]
        self.hours = {p: len(np.unique(ep[self.period == p])) / 12.0 for p in PERIODS}
        # net per share per (candidate, draw) for each size and exit
        arr = self.D[:, :, self.d["arr_ask"]]
        rate2 = rate[:, None]
        fee = lambda p: rate2 * p * (1 - p)                                   # noqa: E731
        self.net = {}
        for size in SIZES:
            px, flag = self.D[:, :, self.d[f"x{size}"]], self.D[:, :, self.d[f"f{size}"]]
            self.net[("live", size)] = px - arr - fee(arr) - np.where(flag == 0.0, fee(np.nan_to_num(px)), 0.0)
        won = self.col["won"][:, None]
        hold = np.where(won >= 0, won - arr - fee(arr), np.nan)
        for size in SIZES:
            self.net[("hold", size)] = np.broadcast_to(hold, arr.shape)

    def signal_mask(self, trigger: str) -> np.ndarray:
        bit = 1 << TRIGGERS.index(trigger)
        col = self.col
        return (((col["tmask"].astype(int) & bit) > 0) & (col["ask"] >= MIN_ASK) & (col["ask"] <= MAX_ASK + 1e-9)
                & (col["ask"] - col["bid"] <= SPREAD + 1e-9) & (col["ev"] - col["ask"] > 0) & (self.me >= col["ask"] - 1e-9))


def simulate(d: Data, trigger: str, size_mode: str, exit_name: str, draw: int, qage_max: float = 1000.0,
             positions=4, exposure=24.0, per_hour=60, attempts_max=3, retry_ms=2000.0, max_order=6.0):
    """The production budget in time order. Returns (order idx, filled, shares, net USDC per order)."""
    col, Dq = d.col, d.D[:, draw]
    qage = Dq[:, d.d["proc"]] + Dq[:, d.d["book"]]          # the archive point is the book change itself
    sig = d.signal_mask(trigger) & (qage <= qage_max)
    idx = np.nonzero(sig)[0]
    cap = d.cap[idx]
    arr_ask, arr_dep = Dq[idx, d.d["arr_ask"]], Dq[idx, d.d["arr_adep"]]
    target = 5.0 if size_mode == "fixed5" else 10.0
    if size_mode == "adaptive":
        shares = np.where(arr_dep >= 10, 10.0, np.where(arr_dep >= 5, np.floor(arr_dep), 0.0))
    else:
        shares = np.where(arr_dep >= target, target, 0.0)
    size_key = np.where(shares >= 10, 10, 5)
    netps = np.where(size_key == 10, d.net[(exit_name, 10)][idx, draw], d.net[(exit_name, 5)][idx, draw])
    fill = (arr_ask == arr_ask) & (arr_ask <= cap + 1e-9) & (shares >= 5) & ~np.isnan(netps)
    t, asset, epoch = col["t"][idx], col["asset"][idx], col["epoch"][idx]
    known, t_arr = Dq[idx, d.d["t_known"]], Dq[idx, d.d["t_arr"]]
    hold_s = np.where(size_key == 10, Dq[idx, d.d["h10"]], Dq[idx, d.d["h5"]])
    if exit_name == "hold":
        hold_s = np.full(len(idx), 300.0)
    busy, buys, att, entered, last = [], deque(), defaultdict(int), set(), {}
    o_idx, o_fill, o_sh, o_net = [], [], [], []
    for j in range(len(idx)):
        want = target if not fill[j] else shares[j]
        if cap[j] * min(want, 10.0) > max_order + 1e-9:
            continue
        t0 = t[j]
        mk = (asset[j], epoch[j])
        if mk in entered or att[mk] >= attempts_max or t0 - last.get(mk, -1e18) < retry_ms:
            continue
        if busy:
            busy = [b for b in busy if b[0] > t0]
        if len(busy) >= positions or sum(b[1] for b in busy) + cap[j] * 10.0 > exposure + 1e-9:
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
            o_sh.append(shares[j])
            o_net.append(netps[j] * shares[j])
            busy.append((t0 + (t_arr[j] - t0) + hold_s[j] * 1000.0, arr_ask[j] * shares[j]))
        else:
            o_sh.append(0.0)
            o_net.append(0.0)
            busy.append((known[j], cap[j] * 10.0))
    return np.array(o_idx, dtype=int), np.array(o_fill, dtype=bool), np.array(o_sh), np.array(o_net)


def stats(d: Data, runs, mask) -> dict:
    """Mean over draws of orders, fills, shares, net; per-window t on the pooled draws."""
    out = defaultdict(list)
    for o_idx, o_fill, o_sh, o_net in runs:
        k = mask[o_idx]
        f = o_fill & k
        out["orders"].append(k.sum()); out["fills"].append(f.sum()); out["net"].append(o_net[k].sum())
        out["shares"].append(o_sh[f].mean() if f.any() else np.nan)
        out["cps"].append((o_net[f] / o_sh[f]).mean() * 100 if f.any() else np.nan)
        out["win"].append((o_net[f] > 0).mean() if f.any() else np.nan)
        ep = d.col["epoch"][o_idx[f]]
        if f.sum() > 3:
            u, inv = np.unique(ep, return_inverse=True)
            v = np.bincount(inv, weights=o_net[f])
            out["t"].append(v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 and v.std(ddof=1) > 0 else np.nan)
        cum = np.concatenate([[0.0], np.cumsum(o_net[f])])
        out["dd"].append(float(np.max(np.maximum.accumulate(cum) - cum)))
    r = {k: float(np.nanmean(v)) if len(v) else np.nan for k, v in out.items()}
    r["net_lo"], r["net_hi"] = float(min(out["net"])), float(max(out["net"]))
    return r


def analyze() -> None:
    d = Data()
    masks = {p: d.period == p for p in PERIODS}
    masks["dev"] = masks["train"] | masks["validation"]
    L: list[str] = []
    P = L.append
    P("# Spot-lag execution edge: trigger x size x exit (realistic execution)\n")
    P(f"Candidates {len(d.C)}, draws {d.draws}. Hours: " + ", ".join(f"{p} {d.hours[p]:.1f}" for p in PERIODS) +
      ". Gates: 2c edge, limit ask + 2c, spread <= 3c, ask <= 0.60, quote age <= 1000 ms (proxy), budget 4 x 6 USDC, 60 buys / h.\n")
    cfgs = list(itertools.product(TRIGGERS, ("fixed10", "adaptive", "fixed5"), ("live", "hold")))
    rows = {}
    for cfg in cfgs:
        runs = [simulate(d, *cfg, q) for q in range(d.draws)]
        rows[cfg] = {p: stats(d, runs, masks[p]) for p in PERIODS + ("dev",)}
    json.dump({" | ".join(k): v for k, v in rows.items()}, open(RES / "spot_lag_fast_results.json", "w"), indent=1, default=float)

    def cell(s):
        return f"{s['net']:+.1f} ({s['fills']:.0f}, {s['cps']:+.1f}c)"
    P("## All configurations: net USDC (fills, c/share) by period; selection uses TRAIN + VALIDATION only\n")
    P("| trigger | size | exit | train | validation | **dev** | test | **forward** | fwd win | fwd t | fwd max DD |")
    P("|---|---|---|---|---|---|---|---|---:|---:|---:|")
    for cfg in cfgs:
        r = rows[cfg]
        P(f"| {cfg[0]} | {cfg[1]} | {cfg[2]} | {cell(r['train'])} | {cell(r['validation'])} | **{cell(r['dev'])}** | {cell(r['test'])} | "
          f"**{cell(r['forward'])}** | {r['forward']['win']:.0%} | {r['forward']['t']:+.1f} | {r['forward']['dd']:.2f} |")
    P("")
    base = rows[("base", "fixed10", "live")]
    # selection: best dev net among configs positive on both train and validation with >= 30 dev fills; plateau on k
    elig = [c for c in cfgs if rows[c]["train"]["net"] > 0 and rows[c]["validation"]["net"] > 0 and rows[c]["dev"]["fills"] >= 30]
    best = max(elig, key=lambda c: rows[c]["dev"]["net"]) if elig else None
    P("## Selection (locked before reading TEST / FORWARD): the best dev net among configurations positive on both train and validation\n")
    if best:
        P(f"Selected: **{best}** dev {rows[best]['dev']['net']:+.1f} vs baseline (base, fixed10, live) {base['dev']['net']:+.1f}.\n")
        P("| | dev | test | forward |\n|---|---:|---:|---:|")
        for name, c in (("baseline", ("base", "fixed10", "live")), ("selected", best)):
            r = rows[c]
            P(f"| {name} | {cell(r['dev'])} | {cell(r['test'])} | {cell(r['forward'])} [{r['forward']['net_lo']:+.1f}, {r['forward']['net_hi']:+.1f}] |")
        P("")
    # one-factor views from the baseline
    P("## One factor at a time from the baseline (dev | test | forward net USDC)\n")
    P("| change | dev | test | forward |\n|---|---:|---:|---:|")
    for label, c in (("size: adaptive (10, else what the touch holds >= 5)", ("base", "adaptive", "live")), ("size: fixed 5", ("base", "fixed5", "live")),
                     ("exit: hold to settlement", ("base", "fixed10", "hold"))) + tuple((f"trigger: {tr}", (tr, "fixed10", "live")) for tr in TRIGGERS[1:]):
        r = rows[c]
        P(f"| {label} | {r['dev']['net']:+.1f} | {r['test']['net']:+.1f} | {r['forward']['net']:+.1f} |")
    P("")
    # fill-rate / freshness diagnostics for base vs the best fast trigger
    P("## Why: the ask at arrival vs the ask at the decision (draw 0, fixed 10, dev + test)\n")
    P("| trigger | signals | arrival ask <= limit | arrival ask above limit | top level < 10 shares | mean ask move by arrival (c) |\n|---|---:|---:|---:|---:|---:|")
    for tr in TRIGGERS:
        m = d.signal_mask(tr) & (masks["dev"] | masks["test"])
        arr, dep = d.D[m, 0, d.d["arr_ask"]], d.D[m, 0, d.d["arr_adep"]]
        cap = d.cap[m]
        ok = (arr == arr)
        P(f"| {tr} | {m.sum()} | {np.mean(arr[ok] <= cap[ok] + 1e-9):.0%} | {np.mean(arr[ok] > cap[ok] + 1e-9):.0%} | {np.mean(dep[ok] < 10):.0%} | "
          f"{np.nanmean(arr[ok] - d.col['ask'][m][ok]) * 100:+.2f} |")
    (RES / "spot_lag_fast_report.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--draws", type=int, default=4)
    b.add_argument("--procs", type=int, default=3)
    sub.add_parser("analyze")
    args = ap.parse_args()
    if args.cmd == "build":
        print("built", build(args.draws, args.procs))
    else:
        analyze()


if __name__ == "__main__":
    main()
