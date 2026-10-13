"""Research, stage 2: analysis of the 60-second snipe window (library: research_60s_snipe.py).

    python scripts/analyze_60s_snipe.py diagnostics   # sections on TRAINING (+ VALIDATION) only
    python scripts/analyze_60s_snipe.py select        # TRAINING -> VALIDATION -> LOCK
    python scripts/analyze_60s_snipe.py evaluate      # locked candidates + baselines, every period
    python scripts/analyze_60s_snipe.py summary       # results/60s_snipe_summary.csv, full sweep CSV

RESEARCH ONLY: reads data/replay/snipe60/, writes data/replay/snipe60/*.md|json and results/.

PRE-REGISTERED SELECTION (fixed 2026-09-25 ~01:35 UTC, before any sweep result was read)
-----------------------------------------------------------------------------------------
Pool: every (configuration, exit) row of the coarse, time-slice and fine sweeps, on TRAINING.
Eligible: at least MIN_TRAIN_FILLS (40) TRAINING fills.
Three objective criteria, each ranked on TRAINING only:
  high-volume      the most TRAINING fills among rows with >= +0.5c/share and t >= 1.5
  high-expectancy  the highest TRAINING c/share among rows with t >= 1.5
  most stable      the highest TRAINING t (epoch-clustered)
  final minute     the highest TRAINING t among rows whose entries are all inside the last 60 s
                   (window <= 60 s) -- the primary hypothesis gets its own candidate. Added at
                   ~01:39 UTC after the coarse TRAINING landscape showed every <= 60 s window
                   negative (so the three criteria above could never pick one); still before
                   any VALIDATION or unseen result was read.
  (fallbacks, in order, when a pool is empty: any positive c/share; then every eligible row)
The four criteria above rank the coarse, time-slice and fine stages.

AMENDMENT (~01:44 UTC, after reading the TRAINING + VALIDATION diagnostics, before any unseen
result): the diagnostics showed every entry below an ask of ~0.70 losing and the only positive
TRAINING pocket at high-priced favourites far from the strike, which the grid could not isolate
(MIN_ASK was fixed at 0.05). A PRICE-BAND stage (research_60s_snipe.band_grid: MIN_ASK x MAX_ASK)
is ranked by two more criteria on TRAINING:
  band             the highest CONSERVATIVE c/share (loss rate at its 95 % upper bound,
                   research_60s_snipe.conservative_c) among rows with >= 30 fills
  band final min.  the same among windows <= 60 s
The t-statistic is not used for them: favourites with no loss yet have near-zero variance. The
VALIDATION filter is applied as above but is NOT blind for these two: the diagnostics had already
shown VALIDATION by price bucket. Their only clean test is the unseen periods.
VALIDATION filter: walking down the TRAINING ranking (top 20), the first row whose VALIDATION
result has >= 10 fills and a positive c/share is LOCKED. If none passes, the TRAINING #1 is locked
anyway and flagged FAILED_VALIDATION. The lock is written with its UTC time to locked.json; the
unseen periods are read only by ``evaluate``, after the lock, and nothing is re-chosen after it.
Fine-grid seeds: the top 10 distinct entry configurations on coarse TRAINING by each criterion.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
import research_60s_snipe as S  # noqa: E402

OUT = S.OUT
SWEEP = OUT / "sweep_trainval.csv"
LOCK = OUT / "locked.json"
MIN_TRAIN_FILLS = 40
MIN_VAL_FILLS = 10
CRITERIA = ("high_volume", "high_expectancy", "stable", "final_minute")
BAND_CRITERIA = ("band", "band_final_minute")
BAND_SWEEP = OUT / "sweep_band_trainval.csv"
MIN_BAND_FILLS = 30
KEY = list(S.CFG_FIELDS) + ["exit"]
UNSEEN = (2, 3, 4)
PERIODS = (0, 1, 2, 3, 4, 5)


# ─────────────────────────────── formatting ───────────────────────────────


def fmt(v, nd=2):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "—" if v is None or math.isnan(v) else ("inf" if v > 0 else "-inf")
    if isinstance(v, (int, np.integer)):
        return f"{int(v):,}"
    if isinstance(v, float):
        return f"{v:+.{nd}f}" if nd >= 0 else f"{v:.{-nd}f}"
    return str(v)


def md(rows: list[dict], cols: list[tuple[str, str, int]]) -> str:
    """Markdown table: cols = (key, header, digits; digits < 0 = unsigned)."""
    head = "| " + " | ".join(h for _, h, _ in cols) + " |"
    sep = "|" + "|".join("---" if k in ("label", "name", "exit", "bucket", "period", "asset") else "---:"
                         for k, _, _ in cols) + "|"
    lines = [head, sep]
    for r in rows:
        lines.append("| " + " | ".join(fmt(r.get(k), d) if not isinstance(r.get(k), str) else r.get(k)
                                       for k, _, d in cols) + " |")
    return "\n".join(lines)


STAT_COLS = [("fills", "fills", 0), ("win", "win", -2), ("c_share", "c/share", 2), ("t", "t", 1),
             ("net_usdc", "net USDC", 2), ("max_dd", "max DD", -2)]
FUNNEL_COLS = [("signals", "signals", 0), ("orders", "orders", 0), ("fills", "fills", 0),
               ("fill_rate", "fill rate", -2)]


# ─────────────────────────────── selection ───────────────────────────────


def load_sweep(path: Path = SWEEP) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.drop_duplicates(subset=KEY + ["period"], keep="first")
    return df


def rank(tr: pd.DataFrame, criterion: str) -> pd.DataFrame:
    el = tr[tr.fills >= MIN_TRAIN_FILLS]
    if criterion == "high_volume":
        pools = [el[(el.c_share >= 0.5) & (el.t >= 1.5)], el[el.c_share > 0], el]
        key, asc = ["fills", "net_usdc"], [False, False]
    elif criterion == "high_expectancy":
        pools = [el[el.t >= 1.5], el[el.c_share > 0], el]
        key, asc = ["c_share", "fills"], [False, False]
    elif criterion == "final_minute":
        pools = [el[el.hi <= 60]]
        key, asc = ["t", "fills"], [False, False]
    elif criterion in BAND_CRITERIA:
        el = tr[tr.fills >= MIN_BAND_FILLS]
        pools = [el[el.hi <= 60]] if criterion == "band_final_minute" else [el]
        key, asc = ["c_cons", "fills"], [False, False]
    else:
        pools = [el]
        key, asc = ["t", "fills"], [False, False]
    for k, pool in enumerate(pools):
        if len(pool):
            out = pool.sort_values(key, ascending=asc).copy()
            out["pool"] = k
            return out
    return el.iloc[0:0]


def cfg_of(row) -> S.Cfg:
    return S.Cfg(model=str(row["model"]), hi=float(row["hi"]), lo=float(row["lo"]), min_edge=float(row["min_edge"]),
                 max_ask=float(row["max_ask"]), min_ask=float(row["min_ask"]), max_spread=float(row["max_spread"]),
                 slip_cap=float(row["slip_cap"]), sides=int(row["sides"]), attempts=int(row["attempts"]))


def fine_seeds(path: Path = SWEEP, n: int = 10) -> list[S.Cfg]:
    df = load_sweep(path)
    tr = df[(df.period == "train") & (df.stage == "coarse")]
    seeds, seen = [], set()
    for crit in CRITERIA:
        k = 0
        for _, row in rank(tr, crit).iterrows():
            c = cfg_of(row)
            if c in seen:
                continue
            seen.add(c)
            seeds.append(c)
            k += 1
            if k >= n:
                break
    return seeds


def select(path: Path = SWEEP) -> dict:
    main_df = load_sweep(path)
    band_df = load_sweep(BAND_SWEEP)
    df = pd.concat([main_df, band_df], ignore_index=True).drop_duplicates(subset=KEY + ["period"], keep="first")
    tr = df[df.period == "train"].set_index(KEY)
    va = df[df.period == "validation"].set_index(KEY)
    pools = {c: main_df[main_df.period == "train"] for c in CRITERIA}
    pools.update({c: band_df[band_df.period == "train"] for c in BAND_CRITERIA})
    out = {"locked_at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "protocol": __doc__.split(
        "PRE-REGISTERED SELECTION")[1].split("\"\"\"")[0].strip(), "rows_considered": int(len(tr)),
        "candidates": {}}
    report = ["# Selection: TRAINING -> VALIDATION -> LOCK", "",
              f"Rows considered (configuration x exit, TRAINING): {len(tr):,}; eligible (>= {MIN_TRAIN_FILLS} fills): "
              f"{int((tr.fills >= MIN_TRAIN_FILLS).sum()):,}; positive on TRAINING: "
              f"{int(((tr.fills >= MIN_TRAIN_FILLS) & (tr.c_share > 0)).sum()):,}; TRAINING t >= 2: "
              f"{int(((tr.fills >= MIN_TRAIN_FILLS) & (tr.t >= 2)).sum()):,}", ""]
    for crit in CRITERIA + BAND_CRITERIA:
        rk = rank(pools[crit], crit).head(20)
        report += [f"## {crit} (pool {int(rk['pool'].iloc[0]) if len(rk) else '-'})", "",
                   "| # | configuration | exit | TRAIN fills | TRAIN c/share | TRAIN t | TRAIN conservative c | VAL fills | VAL c/share | VAL t | passes |",
                   "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
        locked = None
        for k, (_, row) in enumerate(rk.iterrows()):
            key = tuple(row[c] for c in KEY)
            v = va.loc[key] if key in va.index else None
            vf = int(v["fills"]) if v is not None else 0
            vc = float(v["c_share"]) if v is not None and vf else math.nan
            vt = float(v["t"]) if v is not None and vf else math.nan
            ok = vf >= MIN_VAL_FILLS and vc > 0
            report.append(f"| {k + 1} | `{cfg_of(row).tag()}` | {row['exit']} | {int(row['fills'])} | "
                          f"{row['c_share']:+.2f} | {row['t']:+.2f} | {fmt(float(row.get('c_cons', math.nan)))} | "
                          f"{vf} | {fmt(vc)} | {fmt(vt, 1)} | {'yes' if ok else 'no'} |")
            if ok and locked is None:
                locked = (k, row, v, True)
        if locked is None and len(rk):
            row = rk.iloc[0]
            key = tuple(row[c] for c in KEY)
            locked = (0, row, va.loc[key] if key in va.index else None, False)
        if locked is not None:
            k, row, v, ok = locked
            out["candidates"][crit] = {
                "cfg": {f: (row[f].item() if hasattr(row[f], "item") else row[f]) for f in S.CFG_FIELDS},
                "exit": row["exit"], "train_rank": k + 1, "passed_validation": ok,
                "train": {m: float(row[m]) for m in ("fills", "c_share", "t", "net_usdc", "win")},
                "validation": {m: float(v[m]) for m in ("fills", "c_share", "t", "net_usdc", "win")} if v is not None else None}
            report += ["", f"**Locked: #{k + 1}** `{cfg_of(row).tag()}` exit {row['exit']}"
                       + ("" if ok else " — **FAILED_VALIDATION** (no top-20 row was positive on validation)"), ""]
    LOCK.write_text(json.dumps(out, indent=2, default=float))
    (OUT / "select.md").write_text("\n".join(report) + "\n")
    return out


def locked_candidates() -> dict:
    j = json.loads(LOCK.read_text())
    return {crit: (S.Cfg(**{k: (int(v) if k in ("sides", "attempts") else v) for k, v in c["cfg"].items()}), c["exit"],
                   c["passed_validation"]) for crit, c in j["candidates"].items()}


# ─────────────────────────────── shared helpers ───────────────────────────────

TIME_BUCKETS = ((180, 150), (150, 120), (120, 90), (90, 75), (75, 60), (60, 45), (45, 30), (30, 20), (20, 15),
                (15, 10), (10, 5), (5, 0))
Z_BUCKETS = ((-np.inf, -3), (-3, -2), (-2, -1), (-1, -0.5), (-0.5, 0), (0, 0.5), (0.5, 1), (1, 2), (2, 3), (3, np.inf))
PRICE_BUCKETS = ((0.05, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.95),
                 (0.95, 0.99))
#: diagnostic reference entries (fixed before any result): the canonical hypothesis, the same with
#: the basis-aware model and no spread cap, and a broad family (every final-3-minute edge >= 1c)
REFS = {
    "canonical (D, 60 s, 2c, <=0.95, spread 3c)": S.Cfg(**S.CANONICAL[0]),
    "E, 60 s, 2c, <=0.98, any spread": S.Cfg(model="e", hi=60.0, min_edge=0.02, max_ask=0.98, max_spread=1.0),
    "broad (E, 180 s, 1c, <=0.98, any spread)": S.Cfg(model="e", hi=180.0, min_edge=0.01, max_ask=0.98, max_spread=1.0),
}


def z_e(D, M) -> np.ndarray:
    c = D.c
    return c["mu"] / np.sqrt(c["sd"] ** 2 + (M.k[0] * c["sigma"]) ** 2)


def gstat(x: np.ndarray, g: np.ndarray) -> dict:
    """Mean (c), clustered t, n of a per-row value, clusters g."""
    ok = ~np.isnan(x)
    if ok.sum() == 0:
        return {"n": 0, "mean_c": math.nan, "t": math.nan}
    mu, se = S.clustered(x[ok], g[ok])
    return {"n": int(ok.sum()), "mean_c": mu * 100.0, "t": mu / se if se and se > 0 else math.nan}


def side_rows(D, M, splits=(0,), left_max: float = 180.0, model: str = "e") -> dict:
    """Every (point, side) with an ask of 1-99c in ``splits``: the market's offer, the model's
    probability for that side, and what settlement paid (scoring only)."""
    c = D.c
    parts = []
    for s in (1, -1):
        sv = D.side(s)
        m = np.isin(D.split, splits) & (sv["ask"] >= 1) & (sv["ask"] <= 99) & (c["left"] <= left_max) & (D.won >= 0)
        idx = np.where(m)[0]
        parts.append({"idx": idx, "side": np.full(len(idx), s, np.int8), "ask": sv["ask"][idx] / 100.0,
                      "bid": np.where(sv["bid"][idx] >= 1, sv["bid"][idx] / 100.0, np.nan),
                      "adep": sv["adep"][idx], "bdep": sv["bdep"][idx],
                      "p": M.side(model, s)[idx], "won": ((D.won[idx] == 1) == (s > 0)).astype(float)})
    r = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    r["left"] = c["left"][r["idx"]]
    r["win"] = D.win[r["idx"]]
    r["rate"] = D.rate[r["idx"]]
    r["fee"] = S.fee(r["rate"], r["ask"])
    r["edge"] = r["p"] - r["ask"] - r["fee"] - S.SLIP
    r["value0"] = r["won"] - r["ask"] - r["fee"]          # zero-latency settlement value at the decision ask
    r["mid"] = (r["ask"] + r["bid"]) / 2.0
    return r


def trade_frame(D, M, r: dict) -> pd.DataFrame:
    """Per filled trade: the decision-time context (all causal) and the P&L of every exit."""
    c = D.c
    i, s = r["idx"], r["side"].astype(np.int64)
    sg = c["sigma"][i]
    ask = r["dec_ask"] / 100.0
    bidc = np.where(s > 0, c["ub_c"][i], c["db_c"][i])
    ze = z_e(D, M)[i]
    med = {a: np.nanmedian(c["sigma"][(D.split == 0) & (D.asset == a)]) for a in range(len(D.assets))}
    df = pd.DataFrame({
        "split": D.split[i], "epoch": D.epoch[i], "t": c["t"][i], "asset": [D.assets[a] for a in D.asset[i]],
        "side": np.where(s > 0, "UP", "DOWN"), "left": c["left"][i], "dec_ask": ask, "entry": r["entry_c"] / 100.0,
        "edge": r["edge"], "p": r["p"], "spread_c": np.where(bidc >= 1, r["dec_ask"] - bidc, np.nan),
        "ask_sz": np.where(s > 0, c["uad"][i], c["dad"][i]), "bid_sz": np.where(s > 0, c["ubd"][i], c["dbd"][i]),
        "tok1": s * c["du1"][i], "z_side": s * ze, "vol_rel": sg / np.array([med[a] for a in D.asset[i]]),
        "trend": c["r120"][i] / (sg * math.sqrt(120.0)), "won": ((D.won[i] == 1) == (s > 0)).astype(float),
        "dp1": s * c["dp1"][i], "dp3": s * c["dp3"][i]})
    for L in S.RET_LAGS_MS:
        df[f"m{L}"] = s * c[f"r{L}"][i] / (sg * math.sqrt(L / 1000.0))
    df["imb"] = df.bid_sz / (df.bid_sz + df.ask_sz)
    for k, ex in enumerate(S.EXITS):
        df[ex] = r["R"][:, k]
    return df


def tstats(df: pd.DataFrame, col: str = "settle", boot: bool = False) -> dict:
    x = df[col].to_numpy()
    ok = ~np.isnan(x)
    st = S.trade_stats(x[ok], df.epoch.to_numpy()[ok], df.t.to_numpy()[ok], boot=boot)
    return st


def by(df: pd.DataFrame, key, col: str = "settle", order=None) -> list[dict]:
    rows = []
    groups = df.groupby(key, observed=True)
    names = order if order is not None else sorted(groups.groups)
    for name in names:
        if name not in groups.groups:
            rows.append({"bucket": str(name), "fills": 0})
            continue
        g = groups.get_group(name)
        rows.append(dict(bucket=str(name), avg_ask=g.dec_ask.mean(), avg_edge_c=g.edge.mean() * 100, **tstats(g, col)))
    return rows


def cut(x: pd.Series, edges, labels=None) -> pd.Categorical:
    labels = labels or [f"{a:g}..{b:g}" for a, b in zip(edges[:-1], edges[1:])]
    return pd.cut(x, edges, labels=labels, right=False)


BY_COLS = [("bucket", "bucket", 0), ("fills", "fills", 0), ("avg_ask", "avg ask", -3), ("avg_edge_c", "edge c", 2),
           ("win", "win", -2), ("c_share", "c/share", 2), ("t", "t", 1), ("net_usdc", "net USDC", 2)]


# ─────────────────────────────── diagnostics (TRAINING + VALIDATION) ───────────────────────────────


def diagnostics(E: S.Engine) -> str:
    D, M = E.D, E.M
    c = D.c
    out = ["# 60-second snipe: diagnostics (TRAINING and VALIDATION only)", "",
           "Unseen periods are not read here. P&L is per share after every taker fee; t is clustered by 5-minute epoch.",
           ""]
    sec = out.append

    # ---- data coverage ----
    sec("## 1. Data coverage\n")
    rows = []
    for sp, name in enumerate(S.SPLITS):
        m = D.w_split == sp
        if not m.any():
            continue
        eps = np.unique(D.w_epoch[m])
        row = {"label": name, "epochs": len(eps), "windows": int(m.sum()), "hours": len(eps) * 5 / 60,
               "from": time.strftime("%m-%d %H:%M", time.gmtime(eps.min())),
               "to": time.strftime("%m-%d %H:%M", time.gmtime(eps.max() + 300))}
        for a, an in enumerate(D.assets):
            row[an] = int(np.sum(m & (D.w_asset == a)))
        rows.append(row)
    sec(md(rows, [("label", "period", 0), ("from", "from (UTC)", 0), ("to", "to", 0), ("epochs", "epochs", 0),
                  ("windows", "windows", 0), ("hours", "hours", -1)] + [(a, a, 0) for a in D.assets]))
    sec(f"\nPoints (<= 182 s left, ~216 ms apart): {D.n:,}.\n")
    sec("### Book state by seconds left (TRAINING, per archive point)\n")
    rows = []
    tr = D.split == 0
    for hi, lo in TIME_BUCKETS:
        m = tr & (c["left"] <= hi) & (c["left"] > lo)
        ua, ub, da = c["ua_c"][m], c["ub_c"][m], c["da_c"][m]
        two = (ua >= 0) & (ub >= 0)
        fav = c["p_d"][m] >= 0.5
        fa = np.where(fav, ua, da)
        fd = np.where(fav, c["uad"][m], c["dad"][m])
        un = np.where(fav, da, ua)
        rows.append({"bucket": f"{hi}-{lo}", "pts": int(m.sum()), "two": two.mean(), "fav_ask": (fa >= 1).mean(),
                     "fav95": ((fa >= 1) & (fa <= 95)).mean(), "fav98": ((fa >= 1) & (fa <= 98)).mean(),
                     "dog_ask": (un >= 1).mean(), "spr": float(np.median((ua - ub)[two])) if two.any() else math.nan,
                     "fdep": float(np.median(fd[fa >= 1])) if (fa >= 1).any() else math.nan})
    sec(md(rows, [("bucket", "seconds left", 0), ("pts", "points", 0), ("two", "Up book two-sided", -2),
                  ("fav_ask", "favourite has an ask", -2), ("fav95", "fav. ask <= 0.95", -2),
                  ("fav98", "fav. ask <= 0.98", -2), ("dog_ask", "underdog has an ask", -2),
                  ("spr", "median spread c", -1), ("fdep", "median fav. ask size", -0)]))
    sec("\nThe favourite is the side the project model puts at >= 50 %.\n")

    # ---- settlement basis ----
    sec("## 2. What settles the market\n")
    rows = []
    for sp in (0, 1):
        for a, an in enumerate(D.assets):
            m = (D.w_split == sp) & (D.w_asset == a) & (D.w_won >= 0) & ~np.isnan(D.w_dclose)
            agree = np.mean((D.w_dclose[m] >= 0) == (D.w_won[m] == 1))
            sig = np.nanmedian(c["sigma"][(D.split == sp) & (D.asset == a)])
            rows.append({"label": f"{S.SPLITS[sp]} {an}", "n": int(m.sum()), "agree": agree,
                         "bps": M.k[0] * sig * 1e4})
    sec(md(rows, [("label", "period / asset", 0), ("n", "windows", 0),
                  ("agree", "our TWAP(close) >= strike agrees with venue", -3),
                  ("bps", "basis sd, bps (k x median sigma)", -2)]))
    sec(f"\nBasis model E: sd = k x sigma with k = {M.k[0]:.2f} fitted on all TRAINING windows "
        f"(cross-fit halves: {M.k[1]:.2f} / {M.k[2]:.2f}).\n")

    # ---- models ----
    sec("## 3. Fair-value models: calibration\n")
    sec("Brier score (log loss) per point; TRAINING is out-of-fold for the fitted models C, E, F.\n")
    y = D.won == 1
    groups = ((180, 120), (120, 60), (60, 30), (30, 10), (10, 0))
    for sp in (0, 1):
        rows = []
        for hi, lo in groups:
            m = (D.split == sp) & (D.won >= 0) & (c["left"] <= hi) & (c["left"] > lo)
            row = {"bucket": f"{hi}-{lo}", "n": int(m.sum())}
            for k in ("a", "b", "d", "e", "c", "f"):
                row[k] = f"{np.mean((M.p[k][m] - y[m]) ** 2):.4f} ({S.nll(M.p[k][m], y[m]):.3f})"
            two = m & (c["ua_c"] >= 1) & (c["ub_c"] >= 1)
            mid = (c["ua_c"][two] + c["ub_c"][two]) / 200.0
            row["n2"] = int(two.sum())
            row["mkt"] = f"{np.mean((mid - y[two]) ** 2):.4f}" if two.any() else "—"
            row["e2"] = f"{np.mean((M.p['e'][two] - y[two]) ** 2):.4f}" if two.any() else "—"
            row["d2"] = f"{np.mean((M.p['d'][two] - y[two]) ** 2):.4f}" if two.any() else "—"
            row["f2"] = f"{np.mean((M.p['f'][two] - y[two]) ** 2):.4f}" if two.any() else "—"
            rows.append(row)
        sec(f"### {S.SPLITS[sp].upper()}\n")
        sec(md(rows, [("bucket", "seconds left", 0), ("n", "points", 0), ("a", "A spot>K", 0), ("b", "B naive", 0),
                      ("d", "D project", 0), ("e", "E +basis", 0), ("c", "C empirical", 0), ("f", "F logistic", 0),
                      ("n2", "two-sided pts", 0), ("mkt", "MARKET mid", 0), ("d2", "D there", 0),
                      ("e2", "E there", 0), ("f2", "F there", 0)]))
        sec("")
    sec("### Reliability in the last 60 s (TRAINING, points with a two-sided Up book)\n")
    m = (D.split == 0) & (D.won >= 0) & (c["left"] <= 60) & (c["ua_c"] >= 1) & (c["ub_c"] >= 1)
    mid = (c["ua_c"][m] + c["ub_c"][m]) / 200.0
    rows = []
    edges = (0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, .95, 1.0001)
    for lo, hi in zip(edges[:-1], edges[1:]):
        for nm, pv in (("market", mid), ("E", M.p["e"][m])):
            b = (pv >= lo) & (pv < hi)
            if b.sum() < 50:
                continue
            g = D.win[m][b]
            rows.append({"bucket": f"{nm} {lo:.2f}-{min(hi, 1):.2f}", "n": int(b.sum()), "w": len(np.unique(g)),
                         "pred": float(pv[b].mean()), "real": float(y[m][b].mean())})
    sec(md(rows, [("bucket", "predictor / bin", 0), ("n", "points", 0), ("w", "windows", 0),
                  ("pred", "mean predicted", -3), ("real", "realised Up", -3)]))

    # ---- pricing error ----
    for model in ("e", "d"):
        R0 = side_rows(D, M, (0,), model=model)
        sec(f"\n## 4. Pricing error by seconds left, model {model.upper()} (TRAINING, every point x side with an ask)\n")
        sec("raw edge = p - price; executable edge = p - ask - fee(ask) - 0.5c. 'value at the ask' = what the "
            "settlement paid minus the ask and fee, for the rows with executable edge >= 2c, at the decision ask "
            "with NO latency (before execution). t clustered by window.\n")
        rows = []
        for hi, lo in TIME_BUCKETS:
            for s, nm in ((1, "UP"), (-1, "DOWN")):
                m = (R0["left"] <= hi) & (R0["left"] > lo) & (R0["side"] == s)
                sig = m & (R0["edge"] >= 0.02)
                v = gstat(R0["value0"][sig], R0["win"][sig])
                rows.append({"bucket": f"{hi}-{lo} {nm}", "n": int(m.sum()),
                             "pm": float(np.nanmean((R0["p"] - R0["mid"])[m])) * 100,
                             "pa": float(np.mean((R0["p"] - R0["ask"])[m])) * 100,
                             "share": float(sig.sum() / max(m.sum(), 1)), "nsig": int(sig.sum()),
                             "wsig": len(np.unique(R0["win"][sig])), "v": v["mean_c"], "vt": v["t"],
                             "err": float(np.mean((R0["won"] - R0["p"])[sig])) * 100 if sig.any() else math.nan})
        sec(md(rows, [("bucket", "seconds left / side", 0), ("n", "rows", 0), ("pm", "mean p - mid, c", 2),
                      ("pa", "mean p - ask, c", 2), ("share", "share edge >= 2c", -3), ("nsig", "edge rows", 0),
                      ("wsig", "their windows", 0), ("v", "value at the ask, c", 2), ("vt", "t", 1),
                      ("err", "won - p there, c", 2)]))

    # ---- distance ----
    R0 = side_rows(D, M, (0,), model="e")
    zs = R0["side"] * z_e(D, M)[R0["idx"]]
    sec("\n## 5. Distance from the strike (TRAINING)\n")
    sec("z = the side's expected settling-TWAP distance from the strike / its remaining sd incl. basis (model E's "
        "z). 'value at the ask' is at the decision ask with no latency, all rows (no edge filter).\n")
    for hi, lo in ((180, 60), (60, 0)):
        rows = []
        for a, b in Z_BUCKETS:
            m = (R0["left"] <= hi) & (R0["left"] > lo) & (zs >= a) & (zs < b)
            if not m.any():
                continue
            v = gstat(R0["value0"][m], R0["win"][m])
            e2 = m & (R0["edge"] >= 0.02)
            v2 = gstat(R0["value0"][e2], R0["win"][e2])
            rows.append({"bucket": f"{a:g}..{b:g}", "n": int(m.sum()), "w": len(np.unique(R0["win"][m])),
                         "ask": float(R0["ask"][m].mean()), "p": float(R0["p"][m].mean()),
                         "won": float(R0["won"][m].mean()), "v": v["mean_c"], "vt": v["t"],
                         "mkt_err": float(np.mean(R0["won"][m] - R0["ask"][m])) * 100,
                         "mod_err": float(np.mean(R0["won"][m] - R0["p"][m])) * 100,
                         "n2": int(e2.sum()), "v2": v2["mean_c"], "v2t": v2["t"]})
        sec(f"### {hi}-{lo} s left\n")
        sec(md(rows, [("bucket", "z (side)", 0), ("n", "rows", 0), ("w", "windows", 0), ("ask", "avg ask", -3),
                      ("p", "avg model p", -3), ("won", "realised win", -3), ("mkt_err", "won - ask, c", 2),
                      ("mod_err", "won - p, c", 2), ("v", "value at ask, c", 2), ("vt", "t", 1),
                      ("n2", "rows edge >= 2c", 0), ("v2", "their value, c", 2), ("v2t", "t", 1)]))
        sec("")

    # ---- executed reference strategies: price buckets, sides, assets, regimes, book, momentum, exits ----
    frames = {}
    for name, cfg in REFS.items():
        r = E.run(cfg, (0, 1), raw=True)
        frames[name] = (r, trade_frame(D, M, r))

    sec("## 6. Token-price ranges (the reference entries re-run with min/max ask = the bucket; settlement)\n")
    for name, cfg in REFS.items():
        for sp in (0, 1):
            rows = []
            for lo, hi in PRICE_BUCKETS:
                cf = replace(cfg, min_ask=lo, max_ask=hi)
                rr = E.run(cf, (sp,))
                sm = S.summarize(D, rr, sp, ("settle",))[0]
                df = trade_frame(D, M, rr)
                sm["bucket"] = f"{lo:.2f}-{hi:.2f}"
                sm["real_c"] = float((df.won - df.entry).mean() * 100) if len(df) else math.nan
                rows.append(sm)
            sec(f"### {name}, {S.SPLITS[sp].upper()}\n")
            sec(md(rows, [("bucket", "ask", 0), ("opportunities", "markets w/ signal", 0)] + FUNNEL_COLS[1:] +
                   [("avg_edge_c", "avg edge c", 2), ("real_c", "won - entry, c", 2), ("win", "win", -2),
                    ("c_share", "c/share", 2), ("t", "t", 1), ("net_usdc", "net USDC", 2), ("max_dd", "max DD", -2)]))
            sec("")

    sec("## 7. Sides, assets, regimes, book, momentum (the reference entries, settlement)\n")
    for name, (r, df) in frames.items():
        for sp in (0, 1):
            d = df[df.split == sp]
            sec(f"### {name}, {S.SPLITS[sp].upper()}: {len(d)} fills, {tstats(d)['c_share'] if len(d) else 0:+.2f} c/share\n")
            parts = [("side", d.side, None), ("asset", d.asset, None),
                     ("seconds left", cut(d.left, [0, 5, 10, 20, 30, 45, 60, 90, 120, 180.01]), None),
                     ("z (side)", cut(d.z_side, [-99, 0, 1, 2, 3, 99]), None),
                     ("vol vs asset median", cut(d.vol_rel, [0, 0.8, 1.25, 99], ["low <0.8", "mid", "high >=1.25"]), None),
                     ("trend 120 s (z)", cut(d.trend, [-99, -1.5, 1.5, 99], ["strong down", "range", "strong up"]), None),
                     ("spread c", cut(d.spread_c.fillna(99), [0, 1.5, 2.5, 5.5, 98, 100], ["1", "2", "3-5", "6+", "no bid"]), None),
                     ("ask size", cut(d.ask_sz, [0, 50, 200, 1e9], ["<50", "50-200", ">=200"]), None),
                     ("book imbalance bid/(bid+ask)", cut(d.imb.fillna(0), [0, 0.2, 0.4, 0.6, 0.8, 1.01]), None),
                     ("token 1 s move c", cut(d.tok1 * 100, [-99, -1, -0.25, 0.25, 1, 99], ["<=-1", "-1..0", "flat", "0..1", ">=1"]), None),
                     ("spot 1 s move (z, our way)", cut(d.m1000, [-99, -1, 0, 1, 2, 99]), None),
                     ("spot 10 s move (z, our way)", cut(d.m10000, [-99, -1, 0, 1, 2, 99]), None)]
            for label, key, order in parts:
                if len(d) == 0:
                    continue
                sec(f"**{label}**\n")
                sec(md(by(d, key, order=order), BY_COLS))
                sec("")

    sec("## 8. Exits: sell before expiry vs hold to settlement (the reference entries)\n")
    E3 = S.Engine(D, M, 1.0, sell_guard_ms=3000.0)
    for name, (r, df) in frames.items():
        cfg = REFS[name]
        r3 = E3.run(cfg, (0, 1))
        df3 = trade_frame(D, M, r3)
        for sp in (0, 1):
            d, d3 = df[df.split == sp], df3[df3.split == sp]
            rows = []
            msp = D.split[r["idx"]] == sp
            for ex in S.EXITS:
                st, st3 = tstats(d, ex), tstats(d3, ex)
                raw = r["RAW"][msp, S.EXIT_IX[ex]]
                hold = float(np.nanmean(raw[:, 1])) if len(raw) else math.nan
                sold = float(np.mean(raw[:, 2] == 0)) if len(raw) else math.nan
                rows.append(dict(bucket=ex, **st, hold=hold, sold=sold, c3=st3.get("c_share"), t3=st3.get("t")))
            sec(f"### {name}, {S.SPLITS[sp].upper()} (last column: exits allowed until 3 s before the close)\n")
            sec(md(rows, [("bucket", "exit", 0), ("fills", "fills", 0), ("win", "win", -2), ("c_share", "c/share", 2),
                          ("t", "t", 1), ("net_usdc", "net USDC", 2), ("max_dd", "max DD", -2),
                          ("hold", "avg hold s", -1), ("sold", "sold before close", -2),
                          ("c3", "c/share, 3 s guard", 2), ("t3", "t", 1)]))
            sec("")

    sec("## 9. Execution funnel (the reference entries)\n")
    rows = []
    for name, (r, df) in frames.items():
        for sp in (0, 1):
            mo = D.split[r["ord_idx"]] == sp
            arr, cap, dec = r["ord_arr"][mo], r["ord_cap"][mo], r["ord_dec"][mo]
            fl = r["ord_filled"][mo]
            rows.append({"label": f"{name} {S.SPLITS[sp]}", "signals": int(np.sum(D.split[r['sig_idx']] == sp)),
                         "opp": int(len(np.unique(D.win[r['sig_idx'][D.split[r['sig_idx']] == sp]]))),
                         "orders": int(mo.sum()), "fills": int(fl.sum()),
                         "k_close": int(np.sum((arr == -2) & ~fl)), "k_noask": int(np.sum((arr == -1) & ~fl)),
                         "k_gone": int(np.sum((arr > cap) & ~fl)),
                         "k_dep": int(np.sum((arr >= 1) & (arr <= cap) & ~fl)),
                         "moved": float(np.mean((arr - dec)[arr >= 1])) if (arr >= 1).any() else math.nan,
                         "movedf": float(np.mean((arr - dec)[fl])) if fl.any() else math.nan,
                         "retry": int(np.sum(r["ord_attempt"][mo] > 1))})
    sec(md(rows, [("label", "entry / period", 0), ("opp", "markets w/ signal", 0), ("signals", "signal points", 0),
                  ("orders", "orders", 0), ("retry", "of which retries", 0), ("fills", "fills", 0),
                  ("k_close", "killed: lands after the close", 0), ("k_noask", "killed: no ask", 0),
                  ("k_gone", "killed: ask above the limit", 0),
                  ("k_dep", "killed: < 5 shares", 0), ("moved", "arrival - decision ask, c (all)", 2),
                  ("movedf", "same, filled", 2)]))
    sec("")

    # ---- where the edge comes from ----
    sec("## 10. Lag or mispricing? Momentum or reversion? (TRAINING)\n")
    R0 = side_rows(D, M, (0,), model="e")
    sig = R0["edge"] >= 0.02
    i0, s0 = R0["idx"], R0["side"].astype(np.int64)
    dp1, du1, dp3 = s0 * c["dp1"][i0], s0 * c["du1"][i0], s0 * c["dp3"][i0]
    cls = np.where((dp1 >= 0.02) & (np.nan_to_num(du1, nan=0.0) < 0.5 * dp1), "lag: model +2c in 1 s, token < half",
                   np.where(np.abs(np.nan_to_num(dp3, nan=9)) < 0.005, "static: model steady 3 s", "other"))
    rows = []
    for hi, lo in ((180, 60), (60, 0)):
        for k in ("lag: model +2c in 1 s, token < half", "static: model steady 3 s", "other"):
            m = sig & (cls == k) & (R0["left"] <= hi) & (R0["left"] > lo)
            v = gstat(R0["value0"][m], R0["win"][m])
            e = gstat((R0["won"] - R0["p"])[m], R0["win"][m])
            rows.append({"bucket": f"{hi}-{lo} s: {k}", "n": v["n"], "w": len(np.unique(R0["win"][m])),
                         "ask": float(R0["ask"][m].mean()) if m.any() else math.nan,
                         "edge": float(R0["edge"][m].mean() * 100) if m.any() else math.nan,
                         "v": v["mean_c"], "vt": v["t"], "err": e["mean_c"], "et": e["t"]})
    sec("Signal rows (model E, executable edge >= 2c), valued at the decision ask with no latency:\n")
    sec(md(rows, [("bucket", "class", 0), ("n", "rows", 0), ("w", "windows", 0), ("ask", "avg ask", -3),
                  ("edge", "avg edge c", 2), ("v", "value at ask c", 2), ("vt", "t", 1),
                  ("err", "won - p, c", 2), ("et", "t", 1)]))
    sec("")
    for name, (r, df) in list(frames.items())[:2]:
        d = df[df.split == 0].copy()
        d["cls"] = np.where((d.dp1 >= 0.02) & (d.tok1.fillna(0) < 0.5 * d.dp1), "lag",
                            np.where(d.dp3.abs().fillna(9) < 0.005, "static", "other"))
        sec(f"Executed, {name}, TRAINING:\n")
        sec(md(by(d, d.cls), BY_COLS))
        sec("")
    rows = []
    for L in S.RET_LAGS_MS:
        zz = s0 * c[f"r{L}"][i0] / (c["sigma"][i0] * math.sqrt(L / 1000.0))
        for lab, a, b in (("against <= -1", -99, -1), ("-1..+1", -1, 1), ("our way >= +1", 1, 99)):
            m = sig & (zz >= a) & (zz < b) & (R0["left"] <= 60)
            e = gstat((R0["won"] - R0["p"])[m], R0["win"][m])
            v = gstat(R0["value0"][m], R0["win"][m])
            rows.append({"bucket": f"{L} ms, {lab}", "n": e["n"], "err": e["mean_c"], "et": e["t"],
                         "v": v["mean_c"], "vt": v["t"]})
    sec("Spot momentum at the signal (last 60 s, model E signals): does the outcome beat the driftless model "
        "(won - p > 0 = the move CONTINUED beyond what the model priced)?\n")
    sec(md(rows, [("bucket", "lookback, spot move (z, the side's way)", 0), ("n", "rows", 0),
                  ("err", "won - p, c", 2), ("et", "t", 1), ("v", "value at ask c", 2), ("vt", "t", 1)]))
    sec("\nThe archive's points are ~216 ms apart, so 50 ms and 100 ms lookbacks cannot be resolved here; "
        "the 100 ms lead/lag on the tick recording is in docs/spot_lag_study_2026-09-23.md (section C: the "
        "token's median lag is 100 ms with under 60 s left).\n")
    names = ["intercept", "logit E", "z 1 s", "z 5 s", "z 10 s", "Up token 1 s move", "vol surprise"]
    rows = []
    for gi, lab in enumerate(("0-10", "10-30", "30-60", "60-120", "120-180")):
        b = M.coef.get((0, gi))
        if b is None:
            continue
        rows.append({"bucket": lab, **{n: float(b[k]) for k, n in enumerate(names)}})
    sec("Model F coefficients (logistic, fitted on all TRAINING; momentum terms on the Up scale):\n")
    sec(md(rows, [("bucket", "seconds left", 0)] + [(n, n, 3) for n in names]))
    sec("")

    # ---- persistence ----
    sec("## 11. How long does an edge stay on the book? (TRAINING, model E, edge >= 2c)\n")
    full = {}
    for s in (1, -1):
        sv = D.side(s)
        ask = sv["ask"] / 100.0
        full[s] = np.where(sv["ask"] >= 1, M.side("e", s) - ask - S.fee(D.rate, ask) - S.SLIP, np.nan)
    rows = []
    for hi, lo in S.SLICES:
        m = sig & (R0["left"] <= hi) & (R0["left"] > lo)
        idx, sd_ = R0["idx"][m], s0[m]
        row = {"bucket": f"{hi}-{lo}", "n": int(m.sum())}
        for dt in (250, 500, 1000, 2000, 5000):
            w = D.win[idx]
            j = D.first_at_or_after(w, c["t"][idx] + dt)
            ok = j < D.w_end[w]
            jj = np.minimum(j, D.n - 1)
            still = np.where(sd_ > 0, full[1][jj], full[-1][jj]) >= 0.02
            row[f"s{dt}"] = float(np.mean(still & ok)) if len(idx) else math.nan
        rows.append(row)
    sec(md(rows, [("bucket", "seconds left", 0), ("n", "edge rows", 0)] +
           [(f"s{dt}", f"still >= 2c after {dt / 1000:g} s", -2) for dt in (250, 500, 1000, 2000, 5000)]))
    sec("")
    return "\n".join(out)


# ─────────────────────────────── baselines ───────────────────────────────


def custom_signals(D, idx: np.ndarray, side: np.ndarray, edge=None, p=None):
    """A signal list in the shape Engine.run_on reads: sorted by point, one side per point
    (the larger ``edge`` when both sides qualify)."""
    edge = np.zeros(len(idx)) if edge is None else np.asarray(edge, dtype=np.float64)
    p = np.full(len(idx), np.nan) if p is None else np.asarray(p, dtype=np.float64)
    o = np.lexsort((-edge, idx))
    idx, side, edge, p = idx[o], side[o], edge[o], p[o]
    keep = np.concatenate([[True], idx[1:] != idx[:-1]]) if len(idx) else np.zeros(0, bool)
    idx, side, edge, p = idx[keep], side[keep].astype(np.int8), edge[keep], p[keep]
    ask = np.where(side > 0, D.c["ua_c"][idx], D.c["da_c"][idx]).astype(np.int64)
    return SimpleNamespace(idx=idx, side=side, ask=ask, edge=edge, p=p)


def tradeable(D, s: int, cfg: S.Cfg, splits) -> np.ndarray:
    c = D.c
    sv = D.side(s)
    m = np.isin(D.split, splits) & (c["left"] > cfg.lo) & (c["left"] <= cfg.hi) & \
        (sv["ask"] >= int(round(cfg.min_ask * 100))) & (sv["ask"] <= int(round(cfg.max_ask * 100))) & \
        (sv["adep"] >= cfg.min_depth)
    if cfg.max_spread < 1.0:
        m &= (sv["bid"] >= 1) & (sv["ask"] - sv["bid"] <= int(round(cfg.max_spread * 100)))
    if cfg.sides and cfg.sides != s:
        m &= False
    return m


def random_baseline(E: S.Engine, cfg: S.Cfg, splits, seed: int) -> dict:
    """One uniformly random (point, side) per market among the pairs ``cfg`` could trade (its
    window, ask band, spread cap, depth): random time, random side, FOK at ask + 2c, one order."""
    D = E.D
    rng = np.random.default_rng(seed)
    parts = [(np.where(tradeable(D, s, cfg, splits))[0], s) for s in (1, -1)]
    idx = np.concatenate([p[0] for p in parts])
    side = np.concatenate([np.full(len(p[0]), p[1]) for p in parts])
    w = D.win[idx]
    o = np.lexsort((rng.random(len(idx)), w))
    first = np.concatenate([[True], w[o][1:] != w[o][:-1]]) if len(o) else np.zeros(0, bool)
    C = custom_signals(D, idx[o][first], side[o][first])
    return E.run_on(C, np.arange(len(C.idx)), C.ask + 2, cfg.model, attempts=1)   # exits read cfg's model


def spot_strike_baseline(E: S.Engine, cfg: S.Cfg, splits, twap: bool = False) -> dict:
    """Buy the side the spot (or the expected settling TWAP) is on, at the first point in the
    window where that side is tradeable under ``cfg``'s filters; FOK at ask + 2c, 3 attempts."""
    D = E.D
    dist = D.c["mu"] if twap else D.c["lzs"]
    parts = [(np.where(tradeable(D, s, cfg, splits) & ((dist >= 0) if s > 0 else (dist < 0)))[0], s) for s in (1, -1)]
    C = custom_signals(D, np.concatenate([p[0] for p in parts]),
                       np.concatenate([np.full(len(p[0]), p[1]) for p in parts]))
    return E.run_on(C, np.arange(len(C.idx)), C.ask + 2, cfg.model, attempts=3)   # exits read cfg's model


def engine_baseline(E: S.Engine, lo: float, hi: float, splits) -> dict:
    """The project's CURRENT fair-value rule (strategy/engine.py EngineConfig defaults), as far as
    the archive allows: p = 0.5 model D + 0.5 the token's mid; net = p - ask - fee - 0.5c >= 2c and
    >= 0.5 x the model's sigma-error band (halved by the blend); ask 0.05-0.95; spread <= 5c;
    >= 10 shares; the stale-quote gate (spot moved >= 1 sigma over 3 s toward the side, token
    re-priced < 2c); FOK one tick through the ask; held to settlement. Its own window is 295-60 s."""
    D, M = E.D, E.M
    c = D.c
    parts = []
    for s in (1, -1):
        sv = D.side(s)
        ask, bid = sv["ask"] / 100.0, sv["bid"] / 100.0
        two = (sv["ask"] >= 1) & (sv["bid"] >= 1)
        pd_ = M.side("d", s)
        blend = 0.5 * pd_ + 0.5 * (ask + bid) / 2.0
        net = blend - ask - S.fee(D.rate, ask) - S.SLIP
        sd = c["sd"]
        band = np.abs(S.t4_cdf(c["mu"] / (sd * 0.75)) - S.t4_cdf(c["mu"] / (sd * 1.25))) / 2.0 * 0.5
        move = s * c["r3000"] / (c["sigma"] * math.sqrt(3.0))
        stale = (np.nan_to_num(move) >= 1.0) & (np.nan_to_num(s * c["du3"], nan=0.0) < 0.02)
        m = np.isin(D.split, splits) & (c["left"] > lo) & (c["left"] <= hi) & two & (sv["ask"] >= 5) & \
            (sv["ask"] <= 95) & (sv["ask"] - sv["bid"] <= 5) & (sv["adep"] >= 10) & (net >= 0.02) & \
            (net >= 0.5 * band) & ~stale
        ii = np.where(m)[0]
        parts.append((ii, np.full(len(ii), s), net[ii], blend[ii]))
    C = custom_signals(D, *(np.concatenate([p[k] for p in parts]) for k in range(4)))
    return E.run_on(C, np.arange(len(C.idx)), C.ask + 1, "d", attempts=3, raw=True)


# ─────────────────────────────── evaluation ───────────────────────────────


def full_stats(df: pd.DataFrame, col: str) -> dict:
    """Trade statistics plus concentration: the best epoch's share of the net, and the mean with
    the top 1 / 5 / 10 % of winning trades removed."""
    st = tstats(df, col, boot=True)
    if not st.get("fills"):
        return st
    x = df[col].to_numpy()
    ep = df.epoch.to_numpy()
    tot = x.sum()
    per = pd.Series(x).groupby(ep).sum()
    st["top_epoch_share"] = float(per.max() / tot) if tot > 0 else math.nan
    o = np.argsort(-x)
    for pct in (1, 5, 10):
        k = int(math.ceil(len(x) * pct / 100.0))
        keep = np.ones(len(x), bool)
        keep[o[:k]] = False
        mu, se = S.clustered(x[keep], ep[keep])
        st[f"ex{pct}"] = mu * 100.0
        st[f"ex{pct}_t"] = mu / se if se and se > 0 else math.nan
    return st


def frame(E: S.Engine, r: dict) -> pd.DataFrame:
    df = trade_frame(E.D, E.M, r)
    df["arr_bid"] = np.where(r["arr_bid_c"] >= 1, r["arr_bid_c"] / 100.0, np.nan)
    df["cap"] = r["cap"] / 100.0
    return df


def stress_rows(E: S.Engine, r: dict, df: pd.DataFrame, exit_name: str, splits=UNSEEN) -> list[dict]:
    """Cost stresses on the SAME fills (pooled over ``splits``): wider spread and more slippage
    re-price both legs and drop a fill whose worse price breaks its limit; lost fills."""
    k = S.EXIT_IX[exit_name]
    m = df.split.isin(splits).to_numpy()
    d = df[m]
    raw = r["RAW"][m, k] if r.get("RAW") is not None else None
    rate = E.D.rate[r["idx"][m]]
    out = [dict(label="as locked", **tstats(d, exit_name))]
    ent, dec, cap = d.entry.to_numpy(), d.dec_ask.to_numpy(), d.cap.to_numpy()
    spr = np.nan_to_num(ent - d.arr_bid.to_numpy(), nan=0.01)
    px, flag, dbid = raw[:, 0], raw[:, 2], raw[:, 3]
    sold = flag == 0

    def repriced(e2, px2, label):
        ok = e2 <= cap + 1e-9
        pnl = np.where(sold, px2 - e2 - S.fee(rate, e2) - S.fee(rate, px2), px - e2 - S.fee(rate, e2))
        dd = d.assign(_x=np.where(ok, pnl, np.nan))
        st = tstats(dd, "_x")
        return dict(label=label, dropped=int((~ok).sum()), **st)
    for f in (0.25, 0.5):
        out.append(repriced(ent + f * spr, px - f * spr, f"spread +{f:.0%} (both legs)"))
    for f in (0.25, 0.5):
        e2 = np.where(ent > dec, dec + (1 + f) * (ent - dec), ent)
        px2 = np.where(sold & (px < dbid), dbid - (1 + f) * (dbid - px), px)
        out.append(repriced(e2, px2, f"slippage +{f:.0%} (adverse moves in flight)"))
    x = d[exit_name].to_numpy()
    rng = np.random.default_rng(3)
    for frac in (0.25, 0.5):
        means = []
        for _ in range(50):
            keep = rng.random(len(x)) >= frac
            means.append(np.mean(x[keep]) * 100 if keep.any() else math.nan)
        out.append({"label": f"{frac:.0%} of fills lost at random (50 draws)", "fills": int(len(x) * (1 - frac)),
                    "c_share": float(np.nanmean(means)), "t": math.nan})
    if len(x):
        keep = x < np.quantile(x, 0.75)
        out.append(dict(label="the best 25 % of fills lost", **tstats(d[keep], exit_name)))
    return out


def evaluate() -> str:
    D = S.Data()
    M = S.Models(D)
    E = S.Engine(D, M)
    locked = locked_candidates()
    lk = json.loads(LOCK.read_text())
    out = ["# 60-second snipe: walk-forward evaluation", "",
           f"Locked {lk['locked_at_utc']} UTC (data/replay/snipe60/locked.json); this evaluation ran "
           f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC. Nothing was re-chosen after the lock.", ""]
    sec = out.append
    strat: dict[str, tuple] = {"60-second strategy (canonical, pre-registered)": (S.Cfg(**S.CANONICAL[0]), "settle", None)}
    names: dict[tuple, list] = {}
    for crit, (cfg, ex, ok) in locked.items():
        names.setdefault((cfg, ex, ok), []).append(crit)
    for (cfg, ex, ok), crits in names.items():          # two criteria can lock the same rule
        strat["locked " + " = ".join(crits)] = (cfg, ex, ok)
    all_sp = PERIODS
    runs = {name: E.run(cfg, all_sp, raw=True) for name, (cfg, ex, ok) in strat.items()}
    frames = {name: frame(E, r) for name, r in runs.items()}

    # ---- baselines ----
    base_cfg = S.Cfg(model="e", hi=60.0, min_edge=0.0, max_ask=0.95, max_spread=0.03)
    base: dict[str, tuple] = {}
    base["no trade"] = None
    rnd = [frame(E, random_baseline(E, base_cfg, all_sp, seed)) for seed in range(20)]
    base["random entry (last 60 s, ask 0.05-0.95, 20 seeds)"] = ("random", rnd)
    for name, (cfg, ex, ok) in strat.items():
        if name.startswith("locked"):
            m = [frame(E, random_baseline(E, cfg, all_sp, 100 + seed)) for seed in range(20)]
            base[f"random, matched to {name} (its window, ask band, spread; 20 seeds)"] = ("random", m, ex)
    base["spot > strike (last 60 s, ask 0.05-0.95)"] = ("one", frame(E, spot_strike_baseline(E, base_cfg, all_sp)))
    base["expected TWAP > strike (last 60 s, ask 0.05-0.95)"] = ("one", frame(E, spot_strike_baseline(E, base_cfg, all_sp, True)))
    for name, (cfg, ex, ok) in strat.items():
        if name.startswith("locked band"):
            base[f"spot > strike, matched to {name}"] = ("one", frame(E, spot_strike_baseline(E, cfg, all_sp, True)), ex)
    eng = engine_baseline(E, 60.0, 180.0, all_sp)
    base["current fair-value rule (its window, 180-60 s here)"] = ("one", frame(E, eng))
    base["current fair-value rule moved into the last 60 s"] = ("one", frame(E, engine_baseline(E, 0.0, 60.0, all_sp)))

    # ---- per-period table ----
    sec("## Walk-forward: every strategy and baseline, by period\n")
    sec("c/share (fills, t). UNSEEN = unseen_1 + unseen_2 + unseen_3 pooled; FORWARD = windows archived "
        "while this study ran. Random baselines: the mean over 20 seeds (fills: per seed).\n")
    per_rows, summary_rows = [], []
    periods = [(sp, S.SPLITS[sp]) for sp in PERIODS] + [(UNSEEN, "unseen (pooled)")]

    def cell(d, col):
        st = tstats(d, col)
        return st, (f"{st['c_share']:+.2f} ({st['fills']}, {st['t']:+.1f})" if st.get("fills") else "— (0)")

    def add(name, dfs, col, cfg=None):
        row = {"label": name}
        for sp, pname in periods:
            spl = [sp] if isinstance(sp, int) else list(sp)
            if len(dfs) == 1:
                st, txt = cell(dfs[0][dfs[0].split.isin(spl)], col)
            else:
                sts = [tstats(d[d.split.isin(spl)], col) for d in dfs]
                cs = [s_.get("c_share", math.nan) for s_ in sts if s_.get("fills")]
                st = {"fills": float(np.mean([s_.get("fills", 0) for s_ in sts])), "c_share": float(np.mean(cs)) if cs else math.nan,
                      "net_usdc": float(np.mean([s_.get("net_usdc", 0.0) for s_ in sts])), "t": math.nan,
                      "win": float(np.mean([s_.get("win", math.nan) for s_ in sts if s_.get("fills")])) if cs else math.nan,
                      "max_dd": float(np.mean([s_.get("max_dd", 0.0) for s_ in sts]))}
                txt = f"{st['c_share']:+.2f} ({st['fills']:.0f}/seed, sd {np.std(cs):.2f})" if cs else "—"
            row[pname] = txt
            summary_rows.append({"strategy": name, "period": pname, "exit": col,
                                 **({k: getattr(cfg, k) for k in S.CFG_FIELDS} if cfg else {}),
                                 **{k: st.get(k) for k in ("fills", "win", "net_usdc", "c_share", "median_c", "t",
                                                           "max_dd", "pf", "losses", "c_cons")}})
        per_rows.append(row)

    for name, (cfg, ex, ok) in strat.items():
        add(name + ("" if ok is None or ok else " [FAILED_VALIDATION]") + f" — exit {ex}", [frames[name]], ex, cfg)
    for name, b in base.items():
        if b is None:
            row = {"label": name}
            for _, pname in periods:
                row[pname] = "0 (0)"
            per_rows.append(row)
            continue
        col = b[2] if len(b) > 2 else "settle"
        add(name + f" — exit {col}", b[1] if b[0] == "random" else [b[1]], col)
    sec(md(per_rows, [("label", "strategy", 0)] + [(p, p, 0) for _, p in periods]))
    sec("")
    pd.DataFrame(summary_rows).to_csv(ROOT / "results/60s_snipe_summary.csv", index=False)

    # ---- statistics, concentration, breakdowns ----
    sec("## Statistics on the unseen periods (pooled)\n")
    rows = []
    for name, (cfg, ex, ok) in strat.items():
        d = frames[name][frames[name].split.isin(UNSEEN)]
        rows.append(dict(label=name, exit=ex, **full_stats(d, ex)))
    sec(md(rows, [("label", "strategy", 0), ("exit", "exit", 0), ("fills", "n", 0), ("c_share", "mean c/share", 2),
                  ("median_c", "median", 2), ("sd_c", "sd", -1), ("ci_lo", "95 % CI lo (clustered)", 2),
                  ("ci_hi", "hi", 2), ("boot_lo", "bootstrap lo", 2), ("boot_hi", "hi", 2), ("t", "t", 2),
                  ("max_dd", "max DD USDC", -2), ("pf", "PF", -2), ("losses", "losing trades", 0),
                  ("c_cons", "conservative c", 2), ("top_epoch_share", "best epoch share of net", -2),
                  ("ex1", "ex top 1 %", 2), ("ex5", "ex top 5 %", 2), ("ex10", "ex top 10 %", 2)]))
    sec("")
    for name, (cfg, ex, ok) in strat.items():
        d = frames[name][frames[name].split.isin(UNSEEN)]
        if len(d) == 0:
            continue
        sec(f"### {name}: `{cfg.tag()}` exit {ex} — unseen breakdowns\n")
        for label, key in (("unseen period", d.split.map(lambda x: S.SPLITS[x])), ("asset", d.asset), ("side", d.side),
                           ("entry ask", cut(d.dec_ask, [0, .2, .4, .6, .7, .8, .9, .95, .99, 1.01])),
                           ("seconds left", cut(d.left, [0, 5, 10, 20, 30, 45, 60, 90, 120, 180.01])),
                           ("vol vs asset median", cut(d.vol_rel, [0, 0.8, 1.25, 99], ["low", "mid", "high"])),
                           ("trend 120 s", cut(d.trend, [-99, -1.5, 1.5, 99], ["strong down", "range", "strong up"])),
                           ("z (side)", cut(d.z_side, [-99, 0, 1, 2, 3, 99]))):
            sec(f"**{label}**\n")
            sec(md(by(d, key, ex), BY_COLS))
            sec("")

    # ---- latency ----
    sec("## Latency sensitivity (same signals; every order and exit re-simulated)\n")
    sec("Scale multiplies the pre-send delay, the order's one-way trip and the venue-book lag proxy "
        "(500 ms); 0 = the decision book fills instantly (signal = fill).\n")
    rows = []
    engines = {1.0: E}
    for sc in (0.0, 1.25, 1.5, 2.0):
        engines[sc] = S.Engine(D, M, scale=sc)
    for name, (cfg, ex, ok) in strat.items():
        for sc in (0.0, 1.0, 1.25, 1.5, 2.0):
            rr = engines[sc].run(cfg, all_sp)
            dd = frame(engines[sc], rr)
            row = {"label": f"{name} x{sc:g}"}
            for sp, pname in ((1, "validation"), (2, "unseen_1"), (3, "unseen_2"), (4, "unseen_3"), (UNSEEN, "unseen")):
                spl = [sp] if isinstance(sp, int) else list(sp)
                st, txt = cell(dd[dd.split.isin(spl)], ex)
                row[pname] = txt
            m = D.split[rr["ord_idx"]] >= 2
            row["fill_rate"] = float(rr["ord_filled"][m].mean()) if m.any() else math.nan
            rows.append(row)
    sec(md(rows, [("label", "strategy x latency", 0), ("fill_rate", "unseen fill rate", -2), ("validation", "validation", 0),
                  ("unseen_1", "unseen_1", 0), ("unseen_2", "unseen_2", 0), ("unseen_3", "unseen_3", 0),
                  ("unseen", "unseen pooled", 0)]))
    sec("")

    # ---- stress ----
    sec("## Stress tests (unseen pooled, the same fills unless stated)\n")
    variants = {"venue book read 300 ms late (proxy)": dict(proxy=300.0), "venue book read 1000 ms late": dict(proxy=1000.0),
                "buys refused in the last 10 s (the existing model)": dict(buy_guard=10_000.0),
                "exits allowed until 3 s before the close": dict(sell_guard=3000.0)}
    alt = {}
    for vn, kw in variants.items():
        e2 = S.Engine(D, M, scale=1.0, sell_guard_ms=kw.get("sell_guard", S.SELL_GUARD_MS),
                      buy_guard_ms=kw.get("buy_guard", 0.0), proxy_scales=False)
        if "proxy" in kw:
            e2.O = S.make_orders(D, 1.0, proxy_ms=kw["proxy"], proxy_scales=False)
            e2.X = S.Exits(D, M, 1.0, proxy_ms=kw["proxy"], proxy_scales=False)
        alt[vn] = e2
    for name, (cfg, ex, ok) in strat.items():
        rows = stress_rows(E, runs[name], frames[name], ex)
        for vn, e2 in alt.items():
            rr = e2.run(cfg, UNSEEN)
            rows.append(dict(label=vn, **tstats(frame(e2, rr), ex)))
        sec(f"### {name} (exit {ex})\n")
        sec(md(rows, [("label", "stress", 0), ("fills", "fills", 0), ("dropped", "fills lost to the limit", 0),
                      ("win", "win", -2), ("c_share", "c/share", 2), ("t", "t", 1), ("net_usdc", "net USDC", 2),
                      ("max_dd", "max DD", -2)]))
        sec("")
    return "\n".join(out)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select")
    sub.add_parser("diagnostics")
    sub.add_parser("evaluate")
    a = ap.parse_args(argv)
    if a.cmd == "select":
        out = select()
        print(json.dumps(out["candidates"], indent=1, default=float))
    elif a.cmd == "diagnostics":
        D = S.Data()
        E = S.Engine(D, S.Models(D))
        (OUT / "diagnostics.md").write_text(diagnostics(E))
        print("wrote", OUT / "diagnostics.md")
    elif a.cmd == "evaluate":
        (OUT / "evaluate.md").write_text(evaluate())
        print("wrote", OUT / "evaluate.md", "and results/60s_snipe_summary.csv")


if __name__ == "__main__":
    main()
