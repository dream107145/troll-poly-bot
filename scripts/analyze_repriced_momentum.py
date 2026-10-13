"""Research, stage 2: analysis of the repriced-token dataset
(scripts/research_repriced_momentum.py builds it).

    python scripts/analyze_repriced_momentum.py explore     # TRAINING (+ validation) only
    python scripts/analyze_repriced_momentum.py select      # candidates: TRAIN -> VALIDATION -> LOCK
    python scripts/analyze_repriced_momentum.py evaluate    # the LOCKED spec on the unseen periods
    python scripts/analyze_repriced_momentum.py stress      # the locked spec under worse execution

Every P&L here is EXECUTED P&L: a FOK buy that fills only if the venue ask on
arrival (measured latency, book read 500 ms late) is at or under the limit, the exit
simulated with the same model, both taker fees (none on a maker fill or at
settlement). Forward MID moves and the zero-latency round trip are labelled as
diagnostics. ``explore`` and ``select`` never read an unseen period.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/replay/rm_research"
SPLITS = ("train", "validation", "unseen_1", "unseen_2", "unseen_3")
UNSEEN = ("unseen_1", "unseen_2", "unseen_3")
ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
SIZE = 5.0


class DS:
    """One dataset (a shock set), rows ordered by shock then time after it."""

    def __init__(self, name: str) -> None:
        z = np.load(DATA / f"dataset_{name}.npz")
        order = np.lexsort((z["rank"], z["shock"]))
        self.name = name
        for k in ("F", "M", "B", "O", "E", "X", "split", "epoch", "shock", "asset", "rank", "t"):
            setattr(self, k, z[k][order])
        self.features = [str(x) for x in z["features"]]
        # features are STORED as float32: 0.88 comes back as 0.8799999952, and an arrival
        # ask of exactly 0.88 would compare as above a 0.88 limit (an exact-ask fill lost).
        # Work in float64 with every price-like column back on its exact value.
        self.F = self.F.astype(np.float64)
        for n in ("ask", "bid", "mid", "spread", "fair", "gap", "overshoot", "expected", "actual", "t250", "t500",
                  "t1000", "tok_high_pb", "tok_vel", "t_acc"):
            k = self.features.index(n)
            self.F[:, k] = np.round(self.F[:, k], 6)
        self.X = self.X.astype(np.float64)
        self.X[:, :, 0] = np.round(self.X[:, :, 0], 6)       # exit prices, cent grid
        self.exits = [str(x) for x in z["exits"]]
        self.horizons = [float(x) for x in z["horizons"]]
        self.fi = {n: i for i, n in enumerate(self.features)}
        self.ei = {n: i for i, n in enumerate(self.exits)}
        self.sp = {s: self.split == i for i, s in enumerate(SPLITS)}

    def f(self, name: str) -> np.ndarray:
        return self.F[:, self.fi[name]].astype(np.float64)

    def first(self, mask: np.ndarray) -> np.ndarray:
        """The first row per shock that satisfies ``mask`` (the live engine sees rows in time order)."""
        idx = np.where(mask)[0]
        if not len(idx):
            return idx
        _, k = np.unique(self.shock[idx], return_index=True)
        return idx[k]

    def fee(self, p: np.ndarray, rows: np.ndarray) -> np.ndarray:
        return self.F[rows, self.fi["rate"]] * p * (1.0 - p)

    def trade(self, rows: np.ndarray, exit_name: str, limit: np.ndarray | None = None, slip: float = 0.0,
              spread_add: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """(filled rows, net per share) for FOK entries at ``limit`` (default the decision ask)
        sold by ``exit_name``. ``slip`` is taken on each leg; ``spread_add`` widens the
        spread (half paid in, half out)."""
        e = self.ei[exit_name]
        lim = self.F[rows, self.fi["ask"]] if limit is None else limit
        arr = self.E[rows, 2]
        ok = (self.E[rows, 3] == 1.0) & (arr <= lim + 1e-9)
        px = self.X[rows, e, 0].astype(np.float64)
        ok &= ~np.isnan(px)
        r = rows[ok]
        entry = arr[ok] + slip + spread_add / 2.0
        px = px[ok]
        flag = self.X[r, e, 2]
        taker = flag == 0.0
        px = np.where(taker, px - slip - spread_add / 2.0, px)
        net = px - entry - self.fee(entry, r) - np.where(taker, self.fee(px, r), 0.0)
        return r, net


def stats(ds: DS, orders: np.ndarray, rows: np.ndarray, net: np.ndarray) -> dict:
    out = {"orders": int(len(orders)), "fills": int(len(rows))}
    if len(orders):
        out["fill_rate"] = len(rows) / len(orders)
    if len(rows) < 2:
        return out
    ep = ds.epoch[rows]
    by = defaultdict(float)
    for e, v in zip(ep, net):
        by[e] += v
    v = np.array(list(by.values()))
    se = v.std(ddof=1) / math.sqrt(len(v)) if len(v) > 2 else float("nan")
    t = v.mean() / se if se == se and se > 0 else float("nan")
    order = np.argsort(ds.t[rows])
    cum = np.concatenate([[0.0], np.cumsum(net[order] * SIZE)])
    w, lo = net[net > 0], net[net <= 0]
    per_fill_se = se * len(v) / len(net) if se == se else float("nan")
    out.update(c=float(net.mean() * 100), t=t, win=float((net > 0).mean()), usdc=float(net.sum() * SIZE),
               dd=float(np.max(np.maximum.accumulate(cum) - cum)),
               pf=float(w.sum() / -lo.sum()) if lo.sum() < 0 else float("inf"),
               med=float(np.median(net) * 100), p25=float(np.percentile(net, 25) * 100),
               p75=float(np.percentile(net, 75) * 100), epochs=len(v),
               ci=(float((net.mean() - 1.96 * per_fill_se) * 100), float((net.mean() + 1.96 * per_fill_se) * 100)))
    return out


def cell(s: dict, min_fills: int = 10) -> str:
    if s.get("fills", 0) < min_fills:
        return f"({s.get('fills', 0)})"
    return f"{s['c']:+.2f} ({s['fills']}, t {s['t']:+.1f})"


def buckets(x: np.ndarray, edges) -> np.ndarray:
    out = np.full(len(x), -1)
    for k, (lo, hi) in enumerate(edges):
        out[(x >= lo) & (x < hi)] = k
    return out


def label(lo, hi, unit="") -> str:
    if hi >= 1e8:
        return f">= {lo:g}{unit}"
    if lo <= -1e8:
        return f"< {hi:g}{unit}"
    return f"{lo:g}-{hi:g}{unit}"


# ─────────────────────────────── explore ───────────────────────────────


PCT_EDGES = ((-0.25, 0.0), (0.0, 0.10), (0.10, 0.25), (0.25, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70),
             (0.70, 0.80), (0.80, 0.90), (0.90, 1.00), (1.00, 1.10), (1.10, 1.25), (1.25, 9.0))
SHOCK_EDGES = ((2, 3), (3, 4), (4, 5), (5, 6), (6, 8), (8, 10), (10, 1e9))
MOM_EDGES = ((-1e9, 0.0), (0.0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 1e9))
TSS_EDGES = ((0.0, 0.3), (0.3, 1.0), (1.0, 2.0), (2.0, 3.5), (3.5, 5.1))


def explore(out_path: Path) -> None:
    L: list[str] = []
    P = L.append
    for name in ("broad", "spotlag"):
        ds = DS(name)
        tr, va = ds.sp["train"], ds.sp["validation"]
        pct, z1s, z500, t500 = ds.f("pct"), ds.f("peak_z1"), ds.f("z500"), ds.f("t500")
        P(f"# Exploration: shock set `{name}` ({len(ds.shock):,} rows, {len(np.unique(ds.shock)):,} shocks)\n")
        P("TRAINING rows only unless a VALIDATION column is shown. Cells: net c/share (fills, t). Entry: the "
          "first row per shock in the cell, FOK at the decision ask. Exits named per column.\n")
        ref_exits = ("t0.5", "t1", "t2", "t3", "t5", "t10", "spot_reversal", "fair_reached", "maker_1c", "maker_2c")

        def table(title, groups, edges_label, mask_base, split_masks=(("TRAIN", tr),), exits=ref_exits, extra=None):
            P(f"## {title}\n")
            for sname, sm in split_masks:
                P(f"**{sname}**\n")
                cols = ["bucket", "shocks", "fill", "mid +1s", "mid +3s", "RT0 +1s", "RT0 +3s"] + list(exits)
                P("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols))
                for k, lab in enumerate(edges_label):
                    rows = ds.first(mask_base & sm & (groups == k))
                    if len(rows) < 5:
                        continue
                    ask = ds.F[rows, ds.fi["ask"]]
                    rt = lambda h: ds.B[rows, ds.horizons.index(h)] - ask - ds.fee(ask, rows) - \
                        ds.fee(ds.B[rows, ds.horizons.index(h)], rows)          # noqa: E731
                    fill = ((ds.E[rows, 3] == 1) & (ds.E[rows, 2] <= ask + 1e-9)).mean()
                    cells = [lab, str(len(rows)), f"{fill:.0%}",
                             f"{np.nanmean(ds.M[rows, ds.horizons.index(1.0)]) * 100:+.2f}",
                             f"{np.nanmean(ds.M[rows, ds.horizons.index(3.0)]) * 100:+.2f}",
                             f"{np.nanmean(rt(1.0)) * 100:+.2f}", f"{np.nanmean(rt(3.0)) * 100:+.2f}"]
                    for e in exits:
                        r, n = ds.trade(rows, e)
                        cells.append(cell(stats(ds, rows, r, n)))
                    P("| " + " | ".join(cells) + " |")
                P("")

        all_rows = np.ones(len(ds.shock), bool)
        rep = pct >= 0.25
        agree = (z500 > 0) & (t500 > 0)
        both = (("TRAIN", tr), ("VALIDATION", va))
        table("Repricing, full range (every row; no momentum filter)", buckets(pct, PCT_EDGES),
              [label(a * 100, b * 100, "%") for a, b in PCT_EDGES], all_rows, both)
        table("Repricing, full range, spot AND token still moving our way (500 ms)", buckets(pct, PCT_EDGES),
              [label(a * 100, b * 100, "%") for a, b in PCT_EDGES], agree, both)
        table("Shock size (peak 1 s move so far, sigma), repriced >= 25 %", buckets(z1s, SHOCK_EDGES),
              [label(a, b, "x") for a, b in SHOCK_EDGES], rep, both)
        table("Shock size, repriced >= 25 %, spot and token agreeing", buckets(z1s, SHOCK_EDGES),
              [label(a, b, "x") for a, b in SHOCK_EDGES], rep & agree, both)
        table("Spot momentum now (500 ms, sigma), repriced >= 25 %, token agreeing", buckets(z500, MOM_EDGES),
              [label(a, b) for a, b in MOM_EDGES], rep & (t500 > 0), both)
        table("Time since shock (s), repriced >= 25 %, agreeing", buckets(ds.f("tss"), TSS_EDGES),
              [label(a, b, " s") for a, b in TSS_EDGES], rep & agree, both)
        gap = ds.f("gap")
        table("Remaining gap to fair (fair - ask, cents), repriced >= 25 %, agreeing",
              buckets(gap * 100, ((-1e9, -2), (-2, 0), (0, 1), (1, 2), (2, 4), (4, 8), (8, 1e9))),
              [label(a, b, "c") for a, b in ((-1e9, -2), (-2, 0), (0, 1), (1, 2), (2, 4), (4, 8), (8, 1e9))],
              rep & agree, both)
        table("Cross-exchange: exchanges >= 2 sigma at the shock, repriced >= 25 %, agreeing",
              buckets(ds.f("n_confirm2"), ((0, 1), (1, 2), (2, 3), (3, 4))), ["0", "1", "2", "3"], rep & agree, both)
        table("First exchange to move (1.5 sigma), repriced >= 25 %, agreeing",
              buckets(ds.f("first_mover"), ((-1, 0), (0, 1), (1, 2), (2, 3))),
              ["none", "binance", "bybit", "coinbase"], rep & agree, both)
        table("Exchanges moving our way now (500 ms), repriced >= 25 %, agreeing",
              buckets(ds.f("n_conf_now"), ((0, 1), (1, 2), (2, 3), (3, 4))), ["0", "1", "2", "3"], rep & agree, both)
        table("Polymarket book imbalance of the token (bid size / both), repriced >= 25 %, agreeing",
              buckets(ds.f("imb"), ((0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))),
              ["0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1"], rep & agree, both)
        table("Ask size change over 500 ms (shares), repriced >= 25 %, agreeing",
              buckets(ds.f("ask_sz_chg"), ((-1e9, -50), (-50, -5), (-5, 5), (5, 50), (50, 1e9))),
              ["< -50", "-50..-5", "-5..5", "5..50", "> 50"], rep & agree, both)
        table("Spot trade-flow imbalance 15 s (signed), repriced >= 25 %, agreeing",
              buckets(ds.f("tfi"), ((-1.01, -0.3), (-0.3, 0.0), (0.0, 0.3), (0.3, 0.6), (0.6, 1.01))),
              ["< -0.3", "-0.3..0", "0..0.3", "0.3..0.6", "> 0.6"], rep & agree, both)
        table("Spread (cents), repriced >= 25 %, agreeing", buckets(ds.f("spread") * 100, ((0, 1.5), (1.5, 2.5), (2.5, 3.5), (3.5, 6))),
              ["1c", "2c", "3c", "4-5c"], rep & agree, both)
        table("Token price (ask), repriced >= 25 %, agreeing",
              buckets(ds.f("ask"), ((0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1))),
              ["< 0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "> 0.8"], rep & agree, both)
        table("Volatility (sigma, bps/s), repriced >= 25 %, agreeing",
              buckets(ds.f("sigma_bps"), ((0, 0.5), (0.5, 1), (1, 2), (2, 4), (4, 1e9))),
              ["< 0.5", "0.5-1", "1-2", "2-4", ">= 4"], rep & agree, both)
        table("Time left in the window (s), repriced >= 25 %, agreeing",
              buckets(ds.f("left"), ((60, 120), (120, 180), (180, 240), (240, 300))),
              ["60-120", "120-180", "180-240", "240-300"], rep & agree, both)
        table("Asset, repriced >= 25 %, agreeing", ds.asset.astype(int), list(ASSETS), rep & agree, both)
        table("Shock persistence since the shock (spot move, sigma), repriced >= 25 %, agreeing",
              buckets(ds.f("persist"), ((-1e9, 0), (0, 1), (1, 2), (2, 4), (4, 1e9))),
              ["< 0", "0-1", "1-2", "2-4", ">= 4"], rep & agree, both)
        table("Earlier same-direction shocks in the last 30 s, repriced >= 25 %, agreeing",
              buckets(ds.f("n_prior"), ((0, 1), (1, 2), (2, 1e9))), ["0", "1", ">= 2"], rep & agree, both)
        table("Entry style: token breaking its post-shock high (breakout), repriced >= 25 %, agreeing",
              buckets(ds.f("breakout"), ((0, 0.5), (0.5, 2))), ["no", "breakout"], rep & agree, both)
        table("Entry style: pullback from the post-shock high (cents), repriced >= 25 %, spot still our way",
              buckets(ds.f("tok_high_pb") * 100, ((-1e9, 0.5), (0.5, 1.5), (1.5, 3), (3, 1e9))),
              ["none", "1c", "2c", ">= 3c"], rep & (z500 > 0), both)

        # 2D interactions, TRAINING, one reference exit per table
        def two_d(title, ga, la, gb, lb, base, exit_name):
            P(f"## {title} (TRAIN, exit {exit_name})\n")
            P("| | " + " | ".join(lb) + " |\n|" + "---|" * (len(lb) + 1))
            for a, la_ in enumerate(la):
                cells = []
                for b in range(len(lb)):
                    rows = ds.first(base & tr & (ga == a) & (gb == b))
                    r, n = ds.trade(rows, exit_name)
                    cells.append(cell(stats(ds, rows, r, n)))
                P(f"| {la_} | " + " | ".join(cells) + " |")
            P("")

        pb = buckets(pct, ((0.25, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 1.1), (1.1, 9)))
        pl = ["25-50%", "50-70%", "70-90%", "90-110%", ">110%"]
        sb = buckets(z1s, ((2, 3), (3, 4), (4, 6), (6, 8), (8, 1e9)))
        sl = ["2-3x", "3-4x", "4-6x", "6-8x", ">=8x"]
        mb = buckets(z500, ((-1e9, 0), (0, 1), (1, 2), (2, 1e9)))
        ml = ["rev", "0-1", "1-2", ">=2"]
        for ex_ in ("t2", "t5"):
            two_d("Shock size x repricing", sb, sl, pb, pl, rep & (t500 > 0), ex_)
            two_d("Shock size x spot momentum", sb, sl, mb, ml, rep & (t500 > 0), ex_)
            two_d("Repricing x spot momentum", pb, pl, mb, ml, rep & (t500 > 0), ex_)
        two_d("Asset x shock size", ds.asset.astype(int), list(ASSETS), sb, sl, rep & agree, "t2")
        two_d("Asset x repricing", ds.asset.astype(int), list(ASSETS), pb, pl, rep & agree, "t2")
        two_d("Volatility x shock size", buckets(ds.f("sigma_bps"), ((0, 1), (1, 2), (2, 1e9))), ["<1", "1-2", ">=2"],
              sb, sl, rep & agree, "t2")
        two_d("Momentum x time since shock", mb, ml, buckets(ds.f("tss"), TSS_EDGES),
              [label(a, b, "s") for a, b in TSS_EDGES], rep & (t500 > 0), "t2")
        two_d("Momentum x spread", mb, ml, buckets(ds.f("spread") * 100, ((0, 1.5), (1.5, 2.5), (2.5, 6))),
              ["1c", "2c", "3c+"], rep & (t500 > 0), "t2")
        two_d("Book imbalance x momentum", buckets(ds.f("imb"), ((0, 0.3), (0.3, 0.7), (0.7, 1.01))),
              ["ask-heavy", "balanced", "bid-heavy"], mb, ml, rep & (t500 > 0), "t2")
        two_d("Cross-exchange confirmation x shock size", buckets(ds.f("n_confirm2"), ((0, 2), (2, 3), (3, 4))),
              ["0-1", "2", "3"], sb, sl, rep & agree, "t2")
        two_d("Gap to fair x time since shock", buckets(ds.f("gap") * 100, ((-1e9, 0), (0, 2), (2, 4), (4, 1e9))),
              ["<0", "0-2c", "2-4c", ">=4c"], buckets(ds.f("tss"), TSS_EDGES),
              [label(a, b, "s") for a, b in TSS_EDGES], rep & agree, "t2")

        # every exit, the repriced + agreeing population, first row per shock
        P("## Every exit: repriced >= 25 %, spot and token agreeing, first row per shock\n")
        P("| exit | TRAIN | VALIDATION |\n|---|---|---|")
        for e in ds.exits:
            cells = []
            for sm in (tr, va):
                rows = ds.first(rep & agree & sm)
                r, n = ds.trade(rows, e)
                cells.append(cell(stats(ds, rows, r, n)))
            P(f"| {e} | " + " | ".join(cells) + " |")
        P("")
    out_path.write_text("\n".join(L) + "\n")
    print(f"wrote {out_path} ({len(L)} lines)")



# ─────────────────────────────── candidates ───────────────────────────────

LOCK = DATA / "locked_spec.json"
MIN_TRAIN_FILLS = 30


def _fee(rate, p):
    return rate * p * (1.0 - p)


def dynamic_limit(ds: DS, rows: np.ndarray, margin: float = 0.01, slip: float = 0.005) -> np.ndarray:
    """The highest price on the cent grid that still leaves ``margin`` of expected value if
    the token only reaches fair: fair - p - fee(p) - fee(fair) - slip >= margin. Never below
    the decision ask (then no order: the mask drops it)."""
    fair, rate = ds.F[rows, ds.fi["fair"]], ds.F[rows, ds.fi["rate"]]
    out = np.full(len(rows), np.nan)
    for k in range(len(rows)):
        p = math.floor((fair[k] - margin) * 100) / 100
        while p >= 0.01:
            if fair[k] - p - _fee(rate[k], p) - _fee(rate[k], min(max(fair[k], .01), .99)) - slip >= margin:
                out[k] = p
                break
            p = round(p - 0.01, 2)
    return out


def rule_mask(ds: DS, spec: dict) -> np.ndarray:
    f = ds.f
    m = f("pct") >= spec.get("pct_min", 0.25)
    if "pct_max" in spec:
        m &= f("pct") <= spec["pct_max"]
    if spec.get("agree", True):
        m &= (f("z500") > 0) & (f("t500") > 0)
    for key, col, scale in (("peak_z1_min", "peak_z1", 1), ("gap_min_c", "gap", 100), ("n_conf_now_min", "n_conf_now", 1),
                            ("imb_min", "imb", 1), ("z500_min", "z500", 1)):
        if key in spec:
            m &= f(col) * scale >= spec[key]
    if spec.get("cheap"):
        m &= (f("ask") <= 0.2) | (f("ask") >= 0.8)
    if "assets" in spec:
        m &= np.isin(ds.asset, [ASSETS.index(a) for a in spec["assets"]])
    return m


# the EV model: a ridge regression of EXECUTED net P&L on decision-time features
def design(ds: DS, rows: np.ndarray) -> np.ndarray:
    f = lambda n: ds.F[rows, ds.fi[n]].astype(np.float64)     # noqa: E731
    c = lambda x, lo, hi: np.clip(np.nan_to_num(x, nan=0.0), lo, hi)   # noqa: E731
    rate, ask = f("rate"), f("ask")
    cols = [c(f("pct"), -0.25, 2.0), c(f("gap") * 100, -10, 15), c(np.log(np.maximum(f("peak_z1"), 1.0)), 0, 3),
            c(f("z250"), -5, 10), c(f("z500"), -5, 10), c(f("z1000"), -5, 10), c(f("z2000"), -5, 10),
            c(f("t250") * 100, -10, 10), c(f("t500") * 100, -10, 10), c(f("t1000") * 100, -10, 10),
            c(f("imb"), 0, 1), np.sign(np.nan_to_num(f("ask_sz_chg"))) * np.log1p(np.abs(np.nan_to_num(f("ask_sz_chg")))),
            c(f("spread") * 100, 0, 5), rate * ask * (1 - ask) * 100, ask, c(f("tss"), 0, 5), f("left") / 300.0,
            np.log(np.maximum(np.nan_to_num(f("sigma_bps"), nan=1.0), 0.05)), f("n_confirm2"), f("n_conf_now"), f("n_exch"),
            c(f("zx_min"), -5, 10), c(f("tfi"), -1, 1), c(f("obi"), -1, 1), c(f("persist"), -5, 10),
            c(f("n_prior"), 0, 3), f("is_up"), f("breakout"), c(f("tok_high_pb") * 100, 0, 10),
            c(f("acc_shock"), -5, 10), c(f("tok_vel") * 100, -20, 20)]
    fm = f("first_mover")
    cols += [(fm == k).astype(float) for k in (0, 1, 2)]
    cols += [(ds.asset[rows] == k).astype(float) for k in range(len(ASSETS))]
    return np.column_stack(cols)


class Ridge:
    def __init__(self, lam: float) -> None:
        self.lam = lam

    def fit(self, X, y):
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        Z = (X - self.mu) / self.sd
        n, k = Z.shape
        A = Z.T @ Z + self.lam * n * np.eye(k)
        self.w = np.linalg.solve(A, Z.T @ (y - y.mean()))
        self.b = y.mean()
        return self

    def predict(self, X):
        return self.b + ((X - self.mu) / self.sd) @ self.w


def fit_ev(ds: DS, fit_mask: np.ndarray, exit_name: str) -> Ridge:
    """E[net | filled, features] on FILLED rows of ``fit_mask`` (repriced >= 25 %),
    lambda by blocked cross-validation over time inside the fitting data only."""
    rows = np.where(fit_mask & (ds.f("pct") >= 0.25))[0]
    r, net = ds.trade(rows, exit_name)
    y = np.clip(net * 100, -20, 20)
    X = design(ds, r)
    ep = ds.epoch[r]
    cuts = np.quantile(ep, np.linspace(0, 1, 6))
    best, best_lam = np.inf, 1.0
    for lam in (1e-3, 1e-2, 1e-1, 1.0, 10.0):
        err = 0.0
        for b in range(5):
            te = (ep >= cuts[b]) & (ep <= cuts[b + 1])
            if te.sum() < 50 or (~te).sum() < 200:
                continue
            m = Ridge(lam).fit(X[~te], y[~te])
            err += float(((m.predict(X[te]) - y[te]) ** 2).sum())
        if err < best:
            best, best_lam = err, lam
    model = Ridge(best_lam).fit(X, y)
    model.exit_name, model.n = exit_name, len(y)
    return model


def run_spec(ds: DS, spec: dict, period_mask: np.ndarray, model: Ridge | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(order rows, filled rows, net per share) of ``spec`` inside ``period_mask``."""
    if spec["kind"] == "rule":
        cand = rule_mask(ds, spec) & period_mask
    else:
        base = period_mask & (ds.f("pct") >= spec.get("pct_min", 0.25))
        idx = np.where(base)[0]
        pred = model.predict(design(ds, idx)) if len(idx) else np.array([])
        cand = np.zeros(len(ds.shock), bool)
        cand[idx[pred >= spec["margin_c"]]] = True
    rows = ds.first(cand)
    if spec.get("limit") == "dynamic":
        lim = dynamic_limit(ds, rows)
        keep = lim >= ds.F[rows, ds.fi["ask"]] - 1e-9
        rows, lim = rows[keep], lim[keep]
        r, net = ds.trade(rows, spec["exit"], limit=lim)
    elif spec.get("limit", "ask").startswith("ask+"):
        off = float(spec["limit"][4:]) / 100.0
        r, net = ds.trade(rows, spec["exit"], limit=ds.F[rows, ds.fi["ask"]] + off)
    else:
        r, net = ds.trade(rows, spec["exit"])
    return rows, r, net


def candidate_specs() -> list[dict]:
    """Hypothesis-driven families (docs/repriced_momentum_research_*.md, section 4),
    not a blind grid; every one trades only ALREADY-repriced tokens (>= 25 %)."""
    S = []
    for z in (6, 8, 10):                       # H1: only huge shocks leave enough continuation
        for e in ("t5", "t10", "spot_reversal"):
            S.append(dict(kind="rule", family="H1 huge shock", set="broad", peak_z1_min=z, exit=e))
    for g in (4, 8):                           # H2: a large remaining gap to fair, cents
        for lim in ("ask", "dynamic"):
            for e in ("t2", "t5"):
                S.append(dict(kind="rule", family="H2 remaining gap", set="broad", gap_min_c=g, limit=lim, exit=e))
    for st in ("broad", "spotlag"):
        for e in ("t2", "t5", "spot_reversal"):   # H3: every exchange confirms and the token's book is bid-heavy
            S.append(dict(kind="rule", family="H3 confirmation stack", set=st, pct_max=0.9, n_conf_now_min=3,
                          imb_min=0.8, exit=e))
        for e in ("t2", "t5"):                    # H4: low-fee prices
            S.append(dict(kind="rule", family="H4 low-fee tokens", set=st, pct_max=0.9, cheap=True, exit=e))
            S.append(dict(kind="rule", family="H5 BTC/ETH confirmed", set=st, pct_max=0.9, n_conf_now_min=3,
                          assets=["BTC", "ETH"], exit=e))
    for st in ("broad", "spotlag"):
        for e in ("t1", "t2", "t5", "t10"):       # H6: the expected-value model
            for mg in (0.0, 1.0, 2.0):
                S.append(dict(kind="ev", family="H6 EV model", set=st, exit=e, margin_c=mg))
    for k, sp in enumerate(S):
        sp["id"] = f"C{k:02d}"
    return S


def baselines() -> list[dict]:
    return [dict(id="B2", kind="rule", family="B2 current strategy", set="spotlag", current=True, exit="t2"),
            dict(id="B3", kind="rule", family="B3 naive 50-90 %", set="spotlag", pct_min=0.5, pct_max=0.9, agree=False,
                 exit="t2")]


def current_mask(ds: DS) -> np.ndarray:
    """The running paper rule: 25-50 %, spot >= 1 sigma over 250 ms, token following,
    ask not past fair, expected edge (frozen training table, 2 s) >= 1c."""
    sys.path.insert(0, str(ROOT / "src"))
    from troll_poly_bot.strategy.repriced_momentum import expected_continuation
    f = ds.f
    m = (f("pct") >= 0.25) & (f("pct") <= 0.5) & (f("z250") >= 1.0) & (f("t500") > 0) & (f("overshoot") <= 1e-9)
    idx = np.where(m)[0]
    ok = np.zeros(len(m), bool)
    for k in idx:
        cont = expected_continuation(float(ds.F[k, ds.fi["pct"]]), float(ds.F[k, ds.fi["z500"]]), 2.0)
        if cont is None:
            continue
        ask, bid, mid, rate = (float(ds.F[k, ds.fi[n]]) for n in ("ask", "bid", "mid", "rate"))
        ex_px = min(max(mid + cont, 0.01), 0.99)
        edge = cont - (ask - bid) - _fee(rate, ask) - _fee(rate, ex_px) - 0.005
        ok[k] = edge >= 0.01 - 1e-9
    return ok


def run_any(dss: dict, spec: dict, period: str, models: dict | None = None):
    ds = dss[spec["set"]]
    pm = ds.sp[period] if isinstance(period, str) else period
    if spec.get("current"):
        rows = ds.first(current_mask(ds) & pm)
        r, net = ds.trade(rows, spec["exit"])
        return ds, rows, r, net
    model = None if spec["kind"] == "rule" else (models or {}).get((spec["set"], spec["exit"]))
    rows, r, net = run_spec(ds, spec, pm, model)
    return ds, rows, r, net


def fmt_row(name, s):
    if s.get("fills", 0) < 2:
        return f"| {name} | {s.get('orders', 0)} | {s.get('fills', 0)} |" + " |" * 9
    return (f"| {name} | {s['orders']} | {s['fills']} | {s.get('fill_rate', 0):.0%} | {s['win']:.0%} | **{s['c']:+.2f}** | "
            f"{s['t']:+.1f} | [{s['ci'][0]:+.1f}, {s['ci'][1]:+.1f}] | {s['usdc']:+.2f} | {s['dd']:.2f} | {s['pf']:.2f} |")


HEAD = ("| strategy | orders | fills | fill rate | win | net c/share | t | 95% CI | net USDC | max DD | PF |\n"
        "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|")


def select(out_path: Path) -> None:
    """TRAIN chooses, VALIDATION refines, the choice is LOCKED to disk. No unseen row is read."""
    L: list[str] = []
    P = L.append
    dss = {n: DS(n) for n in ("broad", "spotlag")}
    models = {}
    for st, ds in dss.items():
        for e in ("t1", "t2", "t5", "t10"):
            models[(st, e)] = fit_ev(ds, ds.sp["train"], e)
    specs = candidate_specs()
    P("# Candidate selection (TRAINING chooses, VALIDATION refines; no unseen data read)\n")
    P(f"{len(specs)} candidates in 6 hypothesis families. Each trades the FIRST qualifying row of a shock, FOK, "
      "net of both fees under the venue_lag execution model.\n")
    P("## EV model fit (TRAINING, filled rows, target = executed net c/share, winsorised +-20c)\n")
    P("| set | exit | rows | lambda | validation rank corr (pred vs realised, filled rows) | validation: realised net by predicted quintile (low -> high) |")
    P("|---|---|---:|---:|---:|---|")
    for (st, e), m in models.items():
        ds = dss[st]
        rows = np.where(ds.sp["validation"] & (ds.f("pct") >= 0.25))[0]
        r, net = ds.trade(rows, e)
        pr = m.predict(design(ds, r))
        from scipy.stats import spearmanr
        rho = spearmanr(pr, net).correlation
        q = np.quantile(pr, [0.2, 0.4, 0.6, 0.8])
        qb = np.searchsorted(q, pr)
        qs = " / ".join(f"{net[qb == k].mean() * 100:+.2f}" for k in range(5))
        P(f"| {st} | {e} | {m.n:,} | {m.lam:g} | {rho:+.3f} | {qs} |")
    P("")
    res = []
    for sp in specs:
        ds, rows, r, net = run_any(dss, sp, "train", models)
        st = stats(ds, rows, r, net)
        res.append((sp, st))
    P("## TRAINING results, every candidate\n")
    P("| id | family | spec | " + HEAD.split("\n")[0][2:].split(" | ", 1)[1])
    P("|---|---|---|" + "---:|" * 10)
    for sp, st in res:
        desc = {k: v for k, v in sp.items() if k not in ("id", "family", "kind")}
        P(fmt_row(f"{sp['id']} | {sp['family']} | `{json.dumps(desc)}`", st))
    P("")
    elig = [(sp, st) for sp, st in res if st.get("fills", 0) >= MIN_TRAIN_FILLS and st.get("t") == st.get("t")]
    top = sorted(elig, key=lambda x: -x[1]["t"])[:5]
    P(f"{len(elig)} candidates had >= {MIN_TRAIN_FILLS} training fills; {sum(1 for _, st in elig if st['c'] > 0)} were "
      "positive on training. The five best by training t go to validation.\n")
    P("## VALIDATION, the five finalists\n")
    P(HEAD)
    val = []
    for sp, st in top:
        ds, rows, r, net = run_any(dss, sp, "validation", models)
        sv = stats(ds, rows, r, net)
        val.append((sp, st, sv))
        P(fmt_row(f"{sp['id']} {sp['family']} (train {st['c']:+.2f}c, t {st['t']:+.1f})", sv))
    P("")
    # refine on validation: the EV margin for an EV finalist
    refined = []
    for sp, st, sv in val:
        if sp["kind"] == "ev":
            for mg in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0):
                sp2 = dict(sp, margin_c=mg, id=sp["id"] + f"m{mg:g}")
                ds, rows, r, net = run_any(dss, sp2, "validation", models)
                refined.append((sp2, st, stats(ds, rows, r, net)))
    if refined:
        P("## VALIDATION refinement: the EV model's margin\n")
        P(HEAD)
        for sp2, st, sv in refined:
            P(fmt_row(f"{sp2['id']} margin {sp2['margin_c']:g}c", sv))
        P("")
    pool = [(sp, sv) for sp, _, sv in val] + [(sp, sv) for sp, _, sv in refined]
    pool = [(sp, sv) for sp, sv in pool if sv.get("fills", 0) >= 20]
    chosen, sv = max(pool, key=lambda x: (x[1]["c"], x[1]["t"])) if pool else (top[0][0], {})
    passed = bool(sv) and sv.get("c", -1) > 0 and sv.get("t", 0) > 1.0
    base_id = chosen["id"].split("m")[0]
    tr_st = next((st for sp, st in res if sp["id"] == base_id), {})
    lock = {"spec": chosen, "validation": {k: sv.get(k) for k in ("fills", "c", "t")},
            "training": {k: tr_st.get(k) for k in ("fills", "c", "t")},
            "passed_validation": passed, "rule": "best validation net c/share among the 5 training finalists "
            "(and the EV margins), >= 20 validation fills; passing = positive with t > 1",
            "locked_at": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
    # the EV procedure: a fixed margin in cents almost never fires (predicted EV < 0 nearly
    # everywhere), so the threshold is a QUANTILE of the training predictions; set, exit and
    # quantile chosen by the validation mean (>= 30 fills), then locked with the rule above
    P("## VALIDATION: the EV model traded above a quantile of its training predictions\n")
    P("| set | exit | quantile | threshold (c) | training | validation |\n|---|---|---:|---:|---|---|")
    best_ev = None
    for st, ds in dss.items():
        rep = ds.f("pct") >= 0.25
        for e in ("t1", "t2", "t5", "t10"):
            m = models[(st, e)]
            tr_idx = np.where(ds.sp["train"] & rep)[0]
            ptr = m.predict(design(ds, tr_idx))
            va_idx = np.where(ds.sp["validation"] & rep)[0]
            pva = m.predict(design(ds, va_idx))
            for q in (0.9, 0.95, 0.99, 0.998):
                thr = float(np.quantile(ptr, q))
                cells = []
                for idx, pr, sm in ((tr_idx, ptr, "train"), (va_idx, pva, "validation")):
                    cand = np.zeros(len(ds.shock), bool)
                    cand[idx[pr >= thr]] = True
                    rows = ds.first(cand)
                    r, net = ds.trade(rows, e)
                    cells.append(stats(ds, rows, r, net))
                P(f"| {st} | {e} | {q:g} | {thr:+.2f} | {cell(cells[0], 5)} | {cell(cells[1], 5)} |")
                sv_ = cells[1]
                if sv_.get("fills", 0) >= 30 and (best_ev is None or sv_["c"] > best_ev[3]["c"]):
                    best_ev = (st, e, q, sv_)
    P("")
    st, e, q, sv_ = best_ev
    lock["ev_procedure"] = {"set": st, "exit": e, "quantile": q,
                            "rule": "ridge EV model refit on ALL data before the period; trade the first row per shock "
                                    "with a prediction at or above the quantile of the in-sample predictions; FOK at the ask",
                            "chosen_by": "best validation mean with >= 30 fills over set x exit x quantile",
                            "validation": {k: sv_.get(k) for k in ("fills", "c", "t")},
                            "passed_validation": bool(sv_["c"] > 0 and sv_["t"] > 1.0),
                            "locked_at": lock["locked_at"]}
    P(f"EV procedure LOCKED: `{json.dumps({k: lock['ev_procedure'][k] for k in ('set', 'exit', 'quantile')})}`; "
      f"validation {sv_['fills']} fills, {sv_['c']:+.2f} c/share, t {sv_['t']:+.1f}; passed: "
      f"**{lock['ev_procedure']['passed_validation']}**\n")
    LOCK.write_text(json.dumps(lock, indent=1, default=float))
    P(f"## LOCKED\n\n`{json.dumps(chosen)}`\n\nvalidation: {sv.get('fills')} fills, {sv.get('c', float('nan')):+.2f} "
      f"c/share, t {sv.get('t', float('nan')):+.1f}. Passed validation (positive, t > 1): **{passed}**.\n")
    out_path.write_text("\n".join(L) + "\n")
    print("\n".join(L))



# ─────────────────────────────── evaluate (post-lock) ───────────────────────────────


def ev_walk_forward(ds: DS, proc: dict) -> dict:
    """The locked EV procedure, walk-forward: for each period, refit on EVERY earlier
    period, threshold at the in-sample prediction quantile, trade the period."""
    out = {}
    rep = ds.f("pct") >= 0.25
    for k, sp in enumerate(SPLITS):
        if k == 0:
            continue
        fit = np.zeros(len(ds.shock), bool)
        for prev in SPLITS[:k]:
            fit |= ds.sp[prev]
        m = fit_ev(ds, fit, proc["exit"])
        fit_idx = np.where(fit & rep)[0]
        thr = float(np.quantile(m.predict(design(ds, fit_idx)), proc["quantile"]))
        idx = np.where(ds.sp[sp] & rep)[0]
        cand = np.zeros(len(ds.shock), bool)
        if len(idx):
            cand[idx[m.predict(design(ds, idx)) >= thr]] = True
        rows = ds.first(cand)
        r, net = ds.trade(rows, proc["exit"])
        out[sp] = (rows, r, net, thr, m.n)
    return out


def baseline_spotlag(period: str) -> dict:
    b = json.loads((DATA / "baseline_spotlag.json").read_text())
    rows = [x for x in b if x["split"] == period and x.get("order")]
    fills = [x for x in rows if x.get("filled") and "net" in x]
    if len(fills) < 3:
        return {"orders": len(rows), "fills": len(fills)}
    net = np.array([x["net"] for x in fills])
    by = defaultdict(float)
    for x in fills:
        by[x["epoch"]] += x["net"]
    v = np.array(list(by.values()))
    t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 else float("nan")
    return {"orders": len(rows), "fills": len(fills), "fill_rate": len(fills) / max(1, len(rows)), "c": net.mean() * 100,
            "t": t, "win": float((net > 0).mean()), "usdc": net.sum() * SIZE}


def portfolio(ds: DS, rows: np.ndarray, net_rows: np.ndarray, net: np.ndarray, exit_name: str, max_order=3.0,
              max_pos=1, max_loss=5.0) -> dict:
    """The strategy's own budget in time order across assets: 1 position, $3 per order
    (5 shares -> asks <= 0.60), $5 daily loss."""
    import datetime as dt
    netmap = dict(zip(net_rows.tolist(), net.tolist()))
    busy, day, taken, skipped = [], defaultdict(float), [], defaultdict(int)
    e = ds.ei[exit_name]
    for r in rows[np.argsort(ds.t[rows])]:
        t0 = ds.t[r]
        busy = [b for b in busy if b > t0]
        d = dt.datetime.utcfromtimestamp(t0 / 1000).strftime("%m-%d")
        if ds.F[r, ds.fi["ask"]] * SIZE > max_order + 1e-9:
            skipped["order cap"] += 1
            continue
        if len(busy) >= max_pos:
            skipped["position cap"] += 1
            continue
        if day[d] <= -max_loss:
            skipped["daily loss"] += 1
            continue
        lat = ds.E[r, 0]
        if int(r) in netmap:
            v = netmap[int(r)] * SIZE
            busy.append(t0 + lat + float(ds.X[r, e, 1]) * 1000.0)
            day[d] += v
            taken.append(v)
        else:
            busy.append(t0 + lat)
    cum = np.concatenate([[0.0], np.cumsum(taken)]) if taken else np.zeros(1)
    return {"fills": len(taken), "usdc": float(sum(taken)), "dd": float(np.max(np.maximum.accumulate(cum) - cum)),
            "skipped": dict(skipped), "days": {k: round(v, 2) for k, v in sorted(day.items())}}


def evaluate(out_path: Path) -> None:
    lock = json.loads(LOCK.read_text())
    L: list[str] = []
    P = L.append
    dss = {n: DS(n) for n in ("broad", "spotlag")}
    spec, proc = lock["spec"], lock["ev_procedure"]
    P("# Evaluation of the LOCKED candidates (read after locking)\n")
    P(f"Locked rule: `{json.dumps(spec)}` (locked {lock['locked_at']}; passed validation: {lock['passed_validation']})\n")
    P(f"Locked EV procedure: `{json.dumps({k: proc[k] for k in ('set', 'exit', 'quantile')})}` (locked {proc['locked_at']})\n")
    wf = ev_walk_forward(dss[proc["set"]], proc)
    per = {}
    for sp in SPLITS:
        P(f"## {sp.upper()}{' (selection data)' if sp in ('train', 'validation') else ''}\n")
        P(HEAD)
        P("| B1 no trade | 0 | 0 | | | 0.00 | | | 0.00 | 0.00 | |")
        for b in baselines():
            ds, rows, r, net = run_any(dss, b, sp)
            s_ = stats(ds, rows, r, net)
            per[(b["id"], sp)] = s_
            P(fmt_row(f"{b['family']} (exit {b['exit']})", s_))
        bs = baseline_spotlag(sp)
        per[("B4", sp)] = bs
        if bs.get("fills", 0) >= 3:
            P(f"| B4 spot-lag (its own rule and exit) | {bs['orders']} | {bs['fills']} | {bs['fill_rate']:.0%} | "
              f"{bs['win']:.0%} | **{bs['c']:+.2f}** | {bs['t']:+.1f} | | {bs['usdc']:+.2f} | | |")
        ds, rows, r, net = run_any(dss, spec, sp)
        s_ = stats(ds, rows, r, net)
        per[("C", sp)] = s_
        P(fmt_row(f"**C locked rule: {spec['family']}**", s_))
        if sp in wf:
            rows, r, net, thr, n = wf[sp]
            s_ = stats(dss[proc["set"]], rows, r, net)
            per[("EV", sp)] = s_
            P(fmt_row(f"**EV walk-forward** (fit on {n:,} earlier rows, threshold {thr:+.2f}c)", s_))
        P("")
    # pooled unseen
    P("## All unseen periods pooled\n")
    P(HEAD)
    for name, getter in (("C locked rule", lambda: run_any(dss, spec, np.isin(dss[spec["set"]].split,
                                                                                  [SPLITS.index(x) for x in UNSEEN])))
                         ,):
        ds, rows, r, net = getter()
        sC = stats(ds, rows, r, net)
        P(fmt_row(name, sC))
        pooledC = (ds, rows, r, net)
    dsE = dss[proc["set"]]
    rowsE = np.concatenate([wf[s][0] for s in UNSEEN])
    rE = np.concatenate([wf[s][1] for s in UNSEEN])
    nE = np.concatenate([wf[s][2] for s in UNSEEN])
    sE = stats(dsE, rowsE, rE, nE)
    P(fmt_row("EV walk-forward", sE))
    P("")
    for name, (ds, rows, r, net), ex in (("C locked rule", pooledC, spec["exit"]), ("EV walk-forward", (dsE, rowsE, rE, nE),
                                                                                    proc["exit"])):
        P(f"### {name}: by asset (unseen pooled)\n")
        P("| asset | fills | net c/share | net USDC |\n|---|---:|---:|---:|")
        for a in range(len(ASSETS)):
            k = ds.asset[r] == a
            if k.sum():
                P(f"| {ASSETS[a]} | {int(k.sum())} | {net[k].mean() * 100:+.2f} | {net[k].sum() * SIZE:+.2f} |")
        pf = portfolio(ds, rows, r, net, ex)
        P(f"\nUnder the strategy's own budget (1 position, $3 per order, $5 daily loss): {pf['fills']} fills, "
          f"{pf['usdc']:+.2f} USDC, max drawdown {pf['dd']:.2f}; skipped {pf['skipped']}; by day {pf['days']}\n")
    Path(DATA / "evaluate_summary.json").write_text(json.dumps({f"{k[0]}|{k[1]}": {kk: vv for kk, vv in v.items()
                                                                                  if kk in ("orders", "fills", "c", "t")}
                                                                 for k, v in per.items()}, default=float, indent=1))
    out_path.write_text("\n".join(L) + "\n")
    print("\n".join(L))


# ─────────────────────────────── diagnostics (post-lock) ───────────────────────────────


def diagnostics(out_path: Path) -> None:
    """Where the theoretical edge disappears: continuation -> spread -> fees -> latency."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import research_repriced_momentum as R
    from concurrent.futures import ProcessPoolExecutor
    L: list[str] = []
    P = L.append
    ds = DS("broad")
    rep, agree = ds.f("pct") >= 0.25, (ds.f("z500") > 0) & (ds.f("t500") > 0)
    m_ev = fit_ev(ds, ds.sp["train"] | ds.sp["validation"], "t2")
    idx = np.where(rep)[0]
    pred = np.full(len(ds.shock), np.nan)
    pred[idx] = m_ev.predict(design(ds, idx))
    q90 = np.nanquantile(pred[ds.sp["train"] | ds.sp["validation"]], 0.9)
    pops = {"repriced >= 25 %, spot + token agreeing": rep & agree,
            "repriced 25-50 %, agreeing": rep & agree & (ds.f("pct") < 0.5),
            "repriced 50-90 %, agreeing": agree & (ds.f("pct") >= 0.5) & (ds.f("pct") <= 0.9),
            "shock >= 8x, repriced >= 25 %, agreeing": rep & agree & (ds.f("peak_z1") >= 8),
            "gap to fair >= 4c, repriced >= 25 %, agreeing": rep & agree & (ds.f("gap") >= 0.04),
            "EV model top decile (fit train+validation)": rep & (pred >= q90)}
    allp = np.ones(len(ds.shock), bool)
    P("# Where the edge disappears (all periods, first row per shock; post-lock diagnostics)\n")
    P("Per share, cents. **mid +h**: the token's mid h s after the decision. **MFE**: best executable bid within 5 s minus "
      "the decision ask (perfect hindsight). **RT0**: buy the decision ask, sell the bid 2 s later, zero latency, both "
      "fees. **executed**: the venue_lag model, exit 2 s. Fee columns re-price the executed trades at other fee rates.\n")
    P("| population | shocks | mid +0.5 s | mid +2 s | MFE - fees | RT0 2 s | RT0 2 s, no fees | executed 2 s | fill | "
      "executed at half fees | executed, no fees |")
    P("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    h05, h2 = ds.horizons.index(0.5), ds.horizons.index(2.0)
    for name, mask in pops.items():
        rows = ds.first(mask & allp)
        ask = ds.F[rows, ds.fi["ask"]]
        rate = ds.F[rows, ds.fi["rate"]]
        bid2 = ds.B[rows, h2]
        rt0 = bid2 - ask - rate * ask * (1 - ask) - rate * bid2 * (1 - bid2)
        rt0nf = bid2 - ask
        mfe = ds.O[rows, 3]
        mfe_net = mfe - rate * ask * (1 - ask) - rate * (ask + mfe) * (1 - ask - mfe)
        r, net = ds.trade(rows, "t2")
        e = ds.ei["t2"]
        arr = ds.E[r, 2]
        px = ds.X[r, e, 0].astype(float)
        flag = ds.X[r, e, 2]
        rr = ds.F[r, ds.fi["rate"]]
        fees = rr * arr * (1 - arr) + np.where(flag == 0, rr * px * (1 - px), 0)
        P(f"| {name} | {len(rows):,} | {np.nanmean(ds.M[rows, h05]) * 100:+.2f} | {np.nanmean(ds.M[rows, h2]) * 100:+.2f} | "
          f"{np.nanmean(mfe_net) * 100:+.2f} | {np.nanmean(rt0) * 100:+.2f} | {np.nanmean(rt0nf) * 100:+.2f} | "
          f"{net.mean() * 100:+.2f} | {len(r) / max(1, len(rows)):.0%} | {(net + fees / 2).mean() * 100:+.2f} | "
          f"{(net + fees).mean() * 100:+.2f} |")
    P("")
    # latency counterfactual: re-run the same orders faster
    P("## Faster execution (the same orders re-simulated; exit 2 s; fees as now)\n")
    P("| population | measured latency, book +500 ms | latency x0.5, book +250 ms | x0.25, +125 ms | x0.1, +50 ms (co-located) |")
    P("|---|---:|---:|---:|---:|")
    rng = np.random.default_rng(3)
    for name in ("repriced >= 25 %, spot + token agreeing", "gap to fair >= 4c, repriced >= 25 %, agreeing",
                 "shock >= 8x, repriced >= 25 %, agreeing", "EV model top decile (fit train+validation)"):
        rows = ds.first(pops[name])
        if len(rows) > 6000:
            rows = np.sort(rng.choice(rows, 6000, replace=False))
        cells = []
        for scale, proxy in ((1.0, 500.0), (0.5, 250.0), (0.25, 125.0), (0.1, 50.0)):
            jobs = []
            for a in range(len(ASSETS)):
                k = rows[ds.asset[rows] == a]
                req = [(int(ds.epoch[x]), float(ds.t[x]), int(ds.rank[x]), bool(ds.F[x, ds.fi["is_up"]]), int(x)) for x in k]
                if req:
                    jobs.append((ASSETS[a], req, scale, proxy, 11, ("t2",)))
            with ProcessPoolExecutor(3) as pool:
                res = {}
                for part in pool.map(R.resimulate, jobs):
                    res.update(part)
            nets = []
            for x in rows:
                arr, ok, lat, X = res[int(x)]
                px, hold, flag = X["t2"]
                ask = ds.F[x, ds.fi["ask"]]
                if ok and arr <= ask + 1e-9 and px == px:
                    rt = ds.F[x, ds.fi["rate"]]
                    nets.append(px - arr - rt * arr * (1 - arr) - (rt * px * (1 - px) if flag == 0 else 0))
            cells.append(f"{np.mean(nets) * 100:+.2f} (fill {len(nets) / len(rows):.0%})" if nets else "-")
        P(f"| {name} | " + " | ".join(cells) + " |")
    P("")
    out_path.write_text("\n".join(L) + "\n")
    print("\n".join(L))


# ─────────────────────────────── stress (post-lock) ───────────────────────────────


def stress(out_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import research_repriced_momentum as R
    from concurrent.futures import ProcessPoolExecutor
    lock = json.loads(LOCK.read_text())
    spec, proc = lock["spec"], lock["ev_procedure"]
    dss = {n: DS(n) for n in ("broad", "spotlag")}
    L: list[str] = []
    P = L.append
    P("# Stress tests of the locked candidates (validation + unseen periods pooled)\n")
    wf = ev_walk_forward(dss[proc["set"]], proc)
    cands = []
    ds = dss[spec["set"]]
    held = np.isin(ds.split, [SPLITS.index(x) for x in ("validation",) + UNSEEN])
    _, rowsC, _, _ = run_any(dss, spec, held)
    cands.append(("C locked rule", ds, rowsC, spec["exit"]))
    rowsE = np.concatenate([wf[s][0] for s in ("validation",) + UNSEEN])
    cands.append(("EV walk-forward", dss[proc["set"]], rowsE, proc["exit"]))
    P("| scenario | " + " | ".join(n for n, *_ in cands) + " |\n|---|" + "---:|" * len(cands))

    def line(name, fn):
        cells = []
        for _, ds, rows, ex in cands:
            r, net = fn(ds, rows, ex)
            s_ = stats(ds, rows, r, net)
            cells.append(cell(s_, 5))
        P(f"| {name} | " + " | ".join(cells) + " |")

    line("as locked", lambda ds, rows, ex: ds.trade(rows, ex))
    for sl in (0.005, 0.01):
        line(f"+{sl * 100:g}c slippage per leg", lambda ds, rows, ex, sl=sl: ds.trade(rows, ex, slip=sl))
    for sa in (0.01, 0.02):
        line(f"spread wider by {sa * 100:g}c", lambda ds, rows, ex, sa=sa: ds.trade(rows, ex, spread_add=sa))
    rng = np.random.default_rng(5)

    def drop_random(ds, rows, ex, frac=0.25):
        r, net = ds.trade(rows, ex)
        keep = rng.random(len(r)) >= frac
        return r[keep], net[keep]

    def drop_best(ds, rows, ex, frac=0.25):
        r, net = ds.trade(rows, ex)
        if not len(r):
            return r, net
        keep = net <= np.quantile(net, 1 - frac)
        return r[keep], net[keep]

    def missing_quotes(ds, rows, ex, frac=0.2):
        keep = rng.random(len(rows)) >= frac
        return ds.trade(rows[keep], ex)
    line("25 % of fills lost at random", drop_random)
    line("the best 25 % of fills lost (adverse)", drop_best)
    line("20 % of quotes missing (orders never sent)", missing_quotes)

    def resim(scale, proxy):
        def fn(ds, rows, ex):
            jobs = []
            for a in range(len(ASSETS)):
                k = rows[ds.asset[rows] == a]
                req = [(int(ds.epoch[x]), float(ds.t[x]), int(ds.rank[x]), bool(ds.F[x, ds.fi["is_up"]]), int(x)) for x in k]
                if req:
                    jobs.append((ASSETS[a], req, scale, proxy, 17, (ex,)))
            res = {}
            with ProcessPoolExecutor(3) as pool:
                for part in pool.map(R.resimulate, jobs):
                    res.update(part)
            rr, nets = [], []
            for x in rows:
                arr, ok, lat, X = res[int(x)]
                px, hold, flag = X[ex]
                ask = ds.F[x, ds.fi["ask"]]
                if ok and arr <= ask + 1e-9 and px == px:
                    rt = ds.F[x, ds.fi["rate"]]
                    rr.append(x)
                    nets.append(px - arr - rt * arr * (1 - arr) - (rt * px * (1 - px) if flag == 0 else 0))
            return np.array(rr, dtype=int), np.array(nets)
        return fn
    line("re-simulated, measured latency (control)", resim(1.0, 500.0))
    for sc in (1.1, 1.25, 1.5):
        line(f"latency +{(sc - 1) * 100:.0f} %", resim(sc, 500.0))
    for px in (750.0, 1000.0):
        line(f"stale book: venue read {px:.0f} ms late", resim(1.0, px))
    P("")
    out_path.write_text("\n".join(L) + "\n")
    print("\n".join(L))



# ─────────────────────────────── live-bot simulation ───────────────────────────────

#: the paper bot's settings (TPB_REPRICED_MOM_*), from strict (what runs) to loose
LADDER = (
    ("L0 running now: 25-50 %, spot >= 1 sigma (250 ms), 0c past fair, edge gate 1c",
     dict(min_pct=0.25, max_pct=0.50, horizon="z250", min_spot_z=1.0, max_over_c=0.0, edge_gate=True)),
    ("L1 = L0 without the edge gate",
     dict(min_pct=0.25, max_pct=0.50, horizon="z250", min_spot_z=1.0, max_over_c=0.0, edge_gate=False)),
    ("L2 25-90 %, spot still our way (500 ms), <= 1c past fair, no edge gate",
     dict(min_pct=0.25, max_pct=0.90, horizon="z500", min_spot_z=0.0, max_over_c=1.0, edge_gate=False)),
    ("L3 25-100 %, spot still our way, <= 2c past fair, no edge gate",
     dict(min_pct=0.25, max_pct=1.00, horizon="z500", min_spot_z=0.0, max_over_c=2.0, edge_gate=False)),
    ("L4 = L3 with the per-order cap raised from $3 to $5 (asks up to 0.95)",
     dict(min_pct=0.25, max_pct=1.00, horizon="z500", min_spot_z=0.0, max_over_c=2.0, edge_gate=False, max_order=5.0)),
    ("L1+1c = L1 with the limit 1c above the ask",
     dict(min_pct=0.25, max_pct=0.50, horizon="z250", min_spot_z=1.0, max_over_c=0.0, edge_gate=False, limit_off_c=1.0)),
    ("L1+2c = L1 with the limit 2c above the ask",
     dict(min_pct=0.25, max_pct=0.50, horizon="z250", min_spot_z=1.0, max_over_c=0.0, edge_gate=False, limit_off_c=2.0)),
)
PERIOD_HOURS = {"train": 256 / 12, "validation": 103 / 12, "unseen_1": 77 / 12, "unseen_2": 77 / 12}


def live_mask(ds: DS, c: dict) -> np.ndarray:
    """The engine's own gates (strategy/repriced_momentum.py), row by row."""
    f = ds.f
    mom = f(c["horizon"])
    m = (f("pct") >= c["min_pct"]) & (f("pct") <= c["max_pct"]) & (mom > 0) & (mom >= c["min_spot_z"])
    m &= (f("t500") > 0) & (f("spread") <= 0.03 + 1e-9) & (f("ask") >= 0.05) & (f("ask") <= 0.95)
    m &= f("overshoot") <= c["max_over_c"] / 100.0 + 1e-9
    if c["edge_gate"]:
        m &= edge_ok(ds, m, c.get("min_edge_c", 1.0) / 100.0, c.get("hold_s", 2.0))
    return m


def edge_ok(ds: DS, mask: np.ndarray, min_edge: float, horizon_s: float) -> np.ndarray:
    """The engine's edge gate: the frozen training continuation for this repricing and spot
    strength, minus the spread, both taker fees and the slippage allowance, >= ``min_edge``."""
    sys.path.insert(0, str(ROOT / "src"))
    from troll_poly_bot.strategy.repriced_momentum import expected_continuation
    ok = np.zeros(len(mask), bool)
    for k in np.where(mask)[0]:
        cont = expected_continuation(float(ds.F[k, ds.fi["pct"]]), float(ds.F[k, ds.fi["z500"]]), horizon_s)
        if cont is None:
            continue
        ask, bid, mid, rate = (float(ds.F[k, ds.fi[n]]) for n in ("ask", "bid", "mid", "rate"))
        ex_px = min(max(mid + cont, 0.01), 0.99)
        ok[k] = cont - (ask - bid) - _fee(rate, ask) - _fee(rate, ex_px) - 0.005 >= min_edge - 1e-9
    return ok


def live_sim(ds: DS, c: dict, exit_name: str = "t2", max_positions=1, max_order=3.0, max_daily_loss=5.0,
             max_attempts=3, retry_ms=2000.0, max_per_hour=60, exit_fn=None) -> dict:
    """The live bot, in time order across every market: the engine's gates, a 2 s retry after
    a killed order, <= 3 attempts and 1 filled entry per market, 1 position at a time,
    the per-order cap at the limit (5 shares), the daily loss stop, 60 buys an hour.
    Orders fill by the venue_lag model (arrival ask <= limit); exits by ``exit_name``."""
    import datetime as dt
    max_order = c.get("max_order", max_order)
    off = c.get("limit_off_c", 0.0) / 100.0
    rows = np.where(live_mask(ds, c))[0]
    rows = rows[np.argsort(ds.t[rows], kind="stable")]
    e = ds.ei[exit_name]
    attempts, entered, last_try = defaultdict(int), set(), {}
    busy_until, day_pnl, buys = 0.0, defaultdict(float), []
    out = defaultdict(lambda: {"orders": 0, "fills": 0, "net": [], "epochs": [], "skip": defaultdict(int)})
    for r in rows:
        t0 = ds.t[r]
        sp = SPLITS[ds.split[r]]
        o = out[sp]
        mk = (int(ds.asset[r]), int(ds.epoch[r]))
        if mk in entered:
            continue
        if attempts[mk] >= max_attempts:
            continue
        if t0 - last_try.get(mk, -1e18) < retry_ms:
            continue
        if t0 < busy_until:
            o["skip"]["one position at a time"] += 1
            continue
        ask = ds.F[r, ds.fi["ask"]]
        limit = round(ask + off, 2)
        if limit * SIZE > max_order + 1e-9:
            o["skip"]["order cap"] += 1
            continue
        day = dt.datetime.utcfromtimestamp(t0 / 1000).strftime("%m-%d")
        if day_pnl[day] <= -max_daily_loss + 1e-9:
            o["skip"]["daily loss stop"] += 1
            continue
        buys = [b for b in buys if b > t0 - 3_600_000.0]
        if len(buys) >= max_per_hour:
            o["skip"]["hourly buy cap"] += 1
            continue
        buys.append(t0)
        attempts[mk] += 1
        last_try[mk] = t0
        o["orders"] += 1
        arr, ok = ds.E[r, 2], ds.E[r, 3]
        px = ds.X[r, e, 0]
        if exit_fn is not None and ok == 1.0 and arr <= limit + 1e-9:
            got = exit_fn(r)                              # (exit px, hold s, flag, kind) or None
            px = np.nan if got is None else got[0]
        if ok == 1.0 and arr <= limit + 1e-9 and px == px:
            if exit_fn is not None:
                px, hold, flag, kind = got
                o.setdefault("kinds", defaultdict(int))[kind] += 1
                o.setdefault("holds", []).append(hold)
            else:
                flag, hold = ds.X[r, e, 2], float(ds.X[r, e, 1])
            rate = ds.F[r, ds.fi["rate"]]
            net = px - arr - rate * arr * (1 - arr) - (rate * px * (1 - px) if flag == 0 else 0.0)
            entered.add(mk)
            o["fills"] += 1
            o["net"].append(net)
            o["epochs"].append(int(ds.epoch[r]))
            day_pnl[day] += net * SIZE
            busy_until = t0 + ds.E[r, 0] + hold * 1000.0
        else:
            busy_until = t0 + ds.E[r, 1]                   # until the kill comes back
    return out


def livesim(out_path: Path) -> None:
    livesim_table(out_path, daily_stop=True)
    livesim_table(out_path.with_name("livesim_nostop.md"), daily_stop=False)


def livesim_table(out_path: Path, daily_stop: bool) -> None:
    ds = DS("spotlag")
    hours = dict(PERIOD_HOURS, unseen_3=len(np.unique(ds.epoch[ds.sp["unseen_3"]])) / 12)
    L: list[str] = []
    P = L.append
    P("# The paper bot's settings, strict to loose: orders, fills and P&L"
      + ("" if daily_stop else " (daily loss stop OFF: the underlying rates)") + "\n")
    P("Replays the live engine's gates on the archive (the live spot-lag shock rule), with its retry and budget "
      "rules; FOK at the decision ask filled by the venue_lag model; sold 2 s after the fill; 5 shares. Not modelled: "
      "the live quote-age gate (rejects ~1-4 % of shocks live).\n")
    P("| settings | period | hours | orders | orders / h | fills | fills / h | fill rate | net c/share | t | USDC | USDC / day |")
    P("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    summary = {}
    for name, c in LADDER:
        res = live_sim(ds, c, max_daily_loss=5.0 if daily_stop else 1e9)
        tot = {"orders": 0, "fills": 0, "net": [], "epochs": [], "h": 0.0}
        for sp in SPLITS:
            o = res[sp]
            h = hours[sp]
            net = np.array(o["net"])
            tot["orders"] += o["orders"]
            tot["fills"] += o["fills"]
            tot["net"] += o["net"]
            tot["epochs"] += o["epochs"]
            tot["h"] += h
            P(_sim_row(name if sp == "train" else "", sp, h, o["orders"], net, o["epochs"]))
        net = np.array(tot["net"])
        P(_sim_row("", "**all**", tot["h"], tot["orders"], net, tot["epochs"]))
        skips = defaultdict(int)
        for sp in SPLITS:
            for k, v in res[sp]["skip"].items():
                skips[k] += v
        summary[name] = dict(skips)
    P("\nSignals the budget turned away (all periods): " + "; ".join(f"{n.split(' ')[0]}: {dict(v)}" for n, v in summary.items())
      + "\n")
    out_path.write_text("\n".join(L) + "\n")
    print("\n".join(L))


def _sim_row(name, sp, h, orders, net, epochs):
    if len(net) < 2:
        return f"| {name} | {sp} | {h:.1f} | {orders} | {orders / h:.1f} | {len(net)} | | | | | | |"
    by = defaultdict(float)
    for e, v in zip(epochs, net):
        by[e] += v
    v = np.array(list(by.values()))
    t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 and v.std(ddof=1) > 0 else float("nan")
    usdc = net.sum() * SIZE
    return (f"| {name} | {sp} | {h:.1f} | {orders} | {orders / h:.1f} | {len(net)} | {len(net) / h:.1f} | "
            f"{len(net) / max(1, orders):.0%} | **{net.mean() * 100:+.2f}** | {t:+.1f} | {usdc:+.2f} | {usdc / h * 24:+.2f} |")



# ─────────────────────────────── take-profit exits ───────────────────────────────


def tpsim(out_path: Path) -> None:
    """Sell once the bid is up a chosen amount: every target x fallback, simulated tick by
    tick for every order the live bot would have filled (ExitLab), with its budget."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import research_repriced_momentum as R
    ds = DS("spotlag")
    lab = R.ExitLab()
    hours = dict(PERIOD_HOURS, unseen_3=len(np.unique(ds.epoch[ds.sp["unseen_3"]])) / 12)
    L1 = dict(min_pct=0.25, max_pct=0.50, horizon="z250", min_spot_z=1.0, max_over_c=0.0, edge_gate=False)
    L3 = dict(min_pct=0.25, max_pct=1.00, horizon="z500", min_spot_z=0.0, max_over_c=2.0, edge_gate=False)

    def fn(tp_c, sl_c, max_hold):
        def f(r):
            return lab.run(int(ds.asset[r]), int(ds.epoch[r]), bool(ds.F[r, ds.fi["is_up"]]), float(ds.E[r, 2]),
                           float(ds.t[r] + ds.E[r, 0]), float(ds.t[r] + ds.E[r, 1]), tp_c / 100.0, sl_c / 100.0, max_hold)
        return f
    L: list[str] = []
    P = L.append
    P("# Take-profit exits: sell once the bid is up X cents (else a fallback)\n")
    P("Every order the live bot's gates would send (L1 = the running settings, L3 = 25-100 %), FOK at the ask, "
      "filled by the venue_lag model; then, tick by tick: TAKE_PROFIT = FOK sell AT the bid once bid >= entry + X "
      "and the trade clears 0.5c after both fees; STOP_LOSS = sell through the bid; TIME_STOP at the maximum hold; "
      "no exit in the last 10 s (settles 1 / 0, no fee). One position at a time, $3 per order, 5 shares; the $5 "
      "daily stop is OFF here so every setting is measured on the same days.\n")
    P("| entry | exit | fills | fills / h | win rate | take-profit hit | avg hold | net c/share | t | USDC / day | "
      "train / val / u1 / u2 / u3 c/share |")
    P("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    tot_h = sum(hours.values())
    exits = [("sell after 2 s (now)", None)]
    for mh, mh_lab in ((10, "10 s"), (60, "60 s"), (0, "settlement")):
        for tp in (2, 3, 5, 8):
            exits.append((f"+{tp}c, else {mh_lab}", (tp, 0, mh)))
    exits += [("+5c, stop -5c, else settlement", (5, 5, 0)), ("+10c, stop -10c, else settlement", (10, 10, 0)),
              ("hold to settlement", (0, 0, 0))]
    for ename, spec in ((n, s_) for n, s_ in (("L1", L1), ("L3", L3))):
        for xname, x in exits:
            res = live_sim(ds, spec, max_daily_loss=1e9, exit_fn=None if x is None else fn(*x))
            net = np.concatenate([np.array(res[s]["net"]) for s in SPLITS])
            eps = sum((res[s]["epochs"] for s in SPLITS), [])
            if len(net) < 3:
                continue
            by = defaultdict(float)
            for ep, v in zip(eps, net):
                by[ep] += v
            v = np.array(list(by.values()))
            t = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 else float("nan")
            kinds = defaultdict(int)
            holds = []
            for s in SPLITS:
                for k, n in res[s].get("kinds", {}).items():
                    kinds[k] += n
                holds += res[s].get("holds", [])
            tp_hit = kinds.get("TAKE_PROFIT", 0) / len(net) if x is not None else float("nan")
            per = " / ".join(f"{np.mean(res[s]['net']) * 100:+.1f}" if len(res[s]["net"]) else "-" for s in SPLITS)
            hold = f"{np.mean(holds):.0f} s" if holds else "2 s"
            P(f"| {ename} | {xname} | {len(net)} | {len(net) / tot_h:.1f} | {(net > 0).mean():.0%} | "
              f"{'' if x is None else f'{tp_hit:.0%}'} | {hold} | **{net.mean() * 100:+.2f}** | {t:+.1f} | "
              f"{net.sum() * SIZE / tot_h * 24:+.2f} | {per} |")
    P("")
    out_path.write_text("\n".join(L) + "\n")
    print("\n".join(L))



# ─────────────────────────────── spot-lag paper settings ───────────────────────────────


class SL:
    """The spot-lag signal set (research_repriced_momentum.py spotlag)."""

    def __init__(self, k: float) -> None:
        z = np.load(DATA / f"spotlag_signals_k{k:g}.npz")
        self.r = z["rows"]
        self.c = {str(n): i for i, n in enumerate(z["cols"])}
        order = np.argsort(self.r[:, self.c["t"]], kind="stable")
        self.r = self.r[order]
        for n in ("ask", "bid", "arr_ask"):
            self.r[:, self.c[n]] = np.round(self.r[:, self.c[n]], 6)

    def col(self, n):
        return self.r[:, self.c[n]]


def _max_entry(ev, rate, fixed, min_edge):
    budget = ev - fixed - min_edge
    p = math.floor((min(budget, 0.99) + 1e-9) * 100) / 100
    while p >= 0.01:
        if p + rate * p * (1 - p) <= budget + 1e-12:
            return round(p, 2)
        p = round(p - 0.01, 2)
    return None


def sl_sim(d: SL, v: dict, exit_name: str = "live", positions=4, exposure=10.0, per_hour=60, attempts_max=3,
           retry_ms=2000.0, periods=None) -> dict:
    """The spot-lag paper bot on the archive: its gates at every signal, the price cap
    min(ask + max slippage, max entry), FOK filled iff the venue ask on arrival <= cap,
    its exit, and its budget in time order across every market."""
    C = d.c
    out = defaultdict(lambda: {"orders": 0, "fills": 0, "net": [], "epochs": []})
    busy, buys = [], []
    att, entered, last = defaultdict(int), set(), {}
    hype = ASSETS.index("HYPE")
    for row in d.r:
        sp = SPLITS[int(row[C["split"]])]
        if periods is not None and sp not in periods:
            continue
        if not v.get("hype", False) and int(row[C["asset"]]) == hype:
            continue
        ask, bid, ev, rate = row[C["ask"]], row[C["bid"]], row[C["ev"]], row[C["rate"]]
        if not (0.05 <= ask <= v.get("max_ask", 0.95)) or ask - bid > v["spread_c"] / 100.0 + 1e-9 or ev - ask <= 0:
            continue
        fixed = 0.01 + rate * min(max(ev, .01), .99) * (1 - min(max(ev, .01), .99)) + (ask - bid) / 2.0
        me = _max_entry(ev, rate, fixed, v["edge_c"] / 100.0)
        if me is None or me < ask - 1e-9:
            continue
        cap = me if v["slip_c"] is None else min(round(ask + v["slip_c"] / 100.0, 2), me)
        t0 = row[C["t"]]
        mk = (int(row[C["asset"]]), int(row[C["epoch"]]))
        if mk in entered or att[mk] >= attempts_max or t0 - last.get(mk, -1e18) < retry_ms:
            continue
        busy = [b for b in busy if b[0] > t0]
        if len(busy) >= positions or sum(b[1] for b in busy) + cap * SIZE > exposure + 1e-9:
            continue
        buys = [b for b in buys if b > t0 - 3_600_000.0]
        if len(buys) >= per_hour:
            continue
        buys.append(t0)
        att[mk] += 1
        last[mk] = t0
        o = out[sp]
        o["orders"] += 1
        arr, ok = row[C["arr_ask"]], row[C["arr_ok"]]
        px, hold, flag = row[C[f"{exit_name}_px"]], row[C[f"{exit_name}_hold"]], row[C[f"{exit_name}_flag"]]
        if ok == 1.0 and arr <= cap + 1e-9 and px == px:
            net = px - arr - rate * arr * (1 - arr) - (rate * px * (1 - px) if flag == 0 else 0.0)
            entered.add(mk)
            o["fills"] += 1
            o["net"].append(net)
            o["epochs"].append(int(row[C["epoch"]]))
            busy.append((t0 + row[C["lat_ms"]] + hold * 1000.0, arr * SIZE))
        else:
            busy.append((t0 + row[C["known_ms"]], cap * SIZE))
    return out


def _agg(res, periods, hours):
    net = np.concatenate([np.array(res[s]["net"]) for s in periods if len(res[s]["net"])]) if any(
        len(res[s]["net"]) for s in periods) else np.array([])
    orders = sum(res[s]["orders"] for s in periods)
    h = sum(hours[s] for s in periods)
    eps = sum((res[s]["epochs"] for s in periods), [])
    out = {"orders_h": orders / h, "fills_h": len(net) / h, "fill": len(net) / max(1, orders), "n": len(net)}
    if len(net) >= 3:
        by = defaultdict(float)
        for e, x in zip(eps, net):
            by[e] += x
        v = np.array(list(by.values()))
        out.update(c=net.mean() * 100, t=v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 else float("nan"),
                   win_h=float((net > 0).sum()) / h, usd_day=net.sum() * SIZE / h * 24)
    return out


def slsim(out_path: Path) -> None:
    ds = {k: SL(k) for k in (3.0, 2.5)}
    ep = {s: len(np.unique(ds[3.0].col("epoch")[ds[3.0].col("split") == i])) for i, s in enumerate(SPLITS)}
    hours = {s: ep[s] / 12 for s in SPLITS}
    L: list[str] = []
    P = L.append
    P("# The spot-lag paper bot's settings: orders, fills and P&L\n")
    P("The live rule at every signal, the price cap min(ask + max slippage, max entry), FOK filled by the venue_lag "
      "model, the live exit (5 s, held to 10 s while the spot keeps moving), and the paper bot's budget (4 positions, "
      "$10 exposure, 60 buys / h, 3 attempts and 1 entry per market, 2 s retry). Selection on TRAINING + VALIDATION "
      "only: highest USDC / day with both periods positive.\n")
    grid = []
    for k in (3.0, 2.5):
        for slip in (2.0, 4.0, None):
            for edge in (2.0, 1.0):
                for spread in (3.0, 5.0):
                    for hype in (False, True):
                        grid.append(dict(k=k, slip_c=slip, edge_c=edge, spread_c=spread, hype=hype))
    fit_p, un_p = ("train", "validation"), ("unseen_1", "unseen_2", "unseen_3")

    def label(v):
        return (f"k {v['k']:g}, cap ask+{v['slip_c']:g}c" if v["slip_c"] is not None else f"k {v['k']:g}, cap = max entry") + \
               f", edge {v['edge_c']:g}c, spread {v['spread_c']:g}c{', +HYPE' if v['hype'] else ''}"
    rows = []
    for v in grid:
        res = sl_sim(ds[v["k"]], v)
        rows.append((v, _agg(res, fit_p, hours), _agg(res, un_p, hours), res))
    P("| settings | TRAIN+VAL orders / h | fills / h | fill | c/share | USDC / day | UNSEEN orders / h | fills / h | fill | "
      "c/share | t | profitable fills / h | USDC / day |")
    P("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    cur = next(r for r in rows if r[0] == dict(k=3.0, slip_c=2.0, edge_c=2.0, spread_c=3.0, hype=False))
    ok = [r for r in rows if all(_agg(r[3], (s,), hours).get("c", -1) > 0 for s in fit_p)]
    best = max(ok, key=lambda r: r[1].get("usd_day", -1e9))
    for r in sorted(rows, key=lambda r: -r[1].get("usd_day", -1e9)):
        v, a, u, _ = r
        tag = " **(running now)**" if r is cur else (" **(chosen)**" if r is best else "")
        P(f"| {label(v)}{tag} | {a['orders_h']:.1f} | {a['fills_h']:.1f} | {a['fill']:.0%} | {a.get('c', float('nan')):+.2f} | "
          f"{a.get('usd_day', float('nan')):+.1f} | {u['orders_h']:.1f} | {u['fills_h']:.1f} | {u['fill']:.0%} | "
          f"{u.get('c', float('nan')):+.2f} | {u.get('t', float('nan')):+.1f} | {u.get('win_h', float('nan')):.1f} | "
          f"{u.get('usd_day', float('nan')):+.1f} |")
    P("")
    n_pos = sum(1 for r in rows if r[2].get("c", -1) > 0)
    P(f"{len(ok)} of {len(rows)} settings were positive in both training and validation; {n_pos} of {len(rows)} are "
      "positive on the unseen periods.\n")
    for name, r in (("running now", cur), ("chosen", best)):
        v, _, _, res = r
        P(f"### {name}: {label(v)}, by period\n")
        P("| period | orders / h | fills / h | fill | c/share | t | USDC / day |\n|---|---:|---:|---:|---:|---:|---:|")
        for sp in SPLITS:
            a = _agg(res, (sp,), hours)
            P(f"| {sp} | {a['orders_h']:.1f} | {a['fills_h']:.1f} | {a['fill']:.0%} | {a.get('c', float('nan')):+.2f} | "
              f"{a.get('t', float('nan')):+.1f} | {a.get('usd_day', float('nan')):+.1f} |")
        P("")
    v = best[0]
    P(f"### Exits for the chosen entry ({label(v)})\n")
    P("| exit | TRAIN+VAL c/share | USDC / day | UNSEEN c/share | t | USDC / day |\n|---|---:|---:|---:|---:|---:|")
    for e, en in (("live", "5 s, held to 10 s while the spot keeps moving (running)"), ("tp5_10", "+5c, else 10 s"),
                  ("tp5_20", "+5c, else 20 s"), ("tp10_20", "+10c, else 20 s")):
        res = sl_sim(ds[v["k"]], v, exit_name=e)
        a, u = _agg(res, fit_p, hours), _agg(res, un_p, hours)
        P(f"| {en} | {a.get('c', float('nan')):+.2f} | {a.get('usd_day', float('nan')):+.1f} | {u.get('c', float('nan')):+.2f} | "
          f"{u.get('t', float('nan')):+.1f} | {u.get('usd_day', float('nan')):+.1f} |")
    P("")
    out_path.write_text("\n".join(L) + "\n")
    print("\n".join(L))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("explore", "select", "evaluate", "diagnostics", "stress", "livesim", "tpsim", "slsim"))
    args = ap.parse_args()
    if args.cmd == "explore":
        explore(DATA / "explore.md")
    elif args.cmd == "select":
        select(DATA / "select.md")
    elif args.cmd == "evaluate":
        evaluate(DATA / "evaluate.md")
    elif args.cmd == "diagnostics":
        diagnostics(DATA / "diagnostics.md")
    elif args.cmd == "stress":
        stress(DATA / "stress.md")
    elif args.cmd == "livesim":
        livesim(DATA / "livesim.md")
    elif args.cmd == "tpsim":
        tpsim(DATA / "tpsim.md")
    elif args.cmd == "slsim":
        slsim(DATA / "slsim.md")


if __name__ == "__main__":
    main()
