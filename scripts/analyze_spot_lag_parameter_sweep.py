"""Spot-lag parameter sweep, analysis: select on TRAINING, filter on VALIDATION, LOCK, then
the unseen periods; every table of docs/spot_lag_parameter_sweep.md.

    python scripts/analyze_spot_lag_parameter_sweep.py            # after research_spot_lag_parameter_sweep.py
                                                                  # build (x4 latencies) and sweep
Outputs: results/spot_lag_parameter_sweep_summary.csv, results/spot_lag_parameter_sweep_locked.json,
results/spot_lag_parameter_sweep_report.md (the tables the doc is written from).
RESEARCH ONLY.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import research_spot_lag_parameter_sweep as S      # noqa: E402

RES = ROOT / "results"
BASE = (0.95, "live")
UNSEEN = ("unseen_1", "unseen_2", "unseen_3")
MIN_TRAIN_FILLS = 30
rng = np.random.default_rng(20260925)


def load_sweep() -> pd.DataFrame:
    return pd.read_csv(RES / "spot_lag_parameter_sweep.csv")


def piv(df, period):
    return df[df.period == period].set_index(["max_ask", "exit"])


# ─────────────────────────────── selection (TRAINING, VALIDATION) ───────────────────────────────


def select(df: pd.DataFrame) -> dict:
    """Categories chosen on TRAINING (>= 30 fills, net > 0); a pick losing on VALIDATION is replaced
    by the next-ranked training candidate of its category. Reads no unseen column."""
    tr, va = piv(df, "train"), piv(df, "validation")
    elig = tr[(tr.fills >= MIN_TRAIN_FILLS) & (tr.net_usdc > 0) & tr.t_stat.notna()].copy()
    elig["ret_dd"] = elig.net_usdc / elig.max_dd_usdc.clip(lower=0.5)

    def pick(frame, key, ascending=False):
        for idx in frame.sort_values(key, ascending=ascending).index:
            if idx in va.index and va.loc[idx, "fills"] >= 5 and va.loc[idx, "net_usdc"] > 0:
                return idx
        return None
    sig = elig[elig.t_stat >= 2.0]
    cats = {
        "risk-adjusted (MAX_ASK + exit)": pick(elig, "t_stat"),
        "high-volume": pick(sig, "fills"),
        "high-quality": pick(elig, "net_c_share"),
        "low-drawdown": pick(sig, "ret_dd"),
        "MAX_ASK only (live exit)": pick(elig[elig.index.get_level_values(1) == "live"], "t_stat"),
        "exit only (MAX_ASK 0.95)": pick(elig[elig.index.get_level_values(0) == 0.95], "t_stat"),
    }
    return {k: (float(v[0]), str(v[1])) if v is not None else None for k, v in cats.items()}


# ─────────────────────────────── trade-level evaluation ───────────────────────────────


def trades(d, ma, ex, **kw):
    return [r for r in S.simulate(d, ma, ex, **kw) if r["status"] == "filled"]


def period_recs(recs, p):
    return S.split_recs(recs, p) if p != "unseen" else [r for r in recs if S.SPLITS[r["split"]] in UNSEEN]


def stats_block(recs, hours) -> dict:
    m = S.metrics(recs, hours)
    f = [r for r in recs if r["status"] == "filled"]
    if len(f) >= 5:
        by = defaultdict(float)
        for r in f:
            by[r["epoch"]] += r["net"]
        eps = np.array(list(by.values()))
        boots = [rng.choice(eps, len(eps), replace=True).sum() / len(f) * 100 for _ in range(2000)]
        m["boot_lo"], m["boot_hi"] = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
        net = np.array(sorted((r["net"] for r in f), reverse=True))
        m["net_c_share_ex_top5"] = float(net[5:].mean() * 100) if len(net) > 10 else float("nan")
        m["top_epoch_share"] = float(max(eps) / eps.sum()) if eps.sum() > 0 else float("nan")
    return m


def fmt(m, keys=("fills", "win_rate", "net_usdc", "net_c_share", "t_stat", "max_dd_usdc")):
    out = []
    for k in keys:
        v = m.get(k, float("nan"))
        if k == "win_rate" or k == "fill_rate":
            out.append(f"{v:.0%}" if v == v else "–")
        elif k in ("fills", "orders", "signals"):
            out.append(f"{int(v)}" if v == v else "0")
        elif k == "net_c_share":
            out.append(f"{v:+.2f}c" if v == v else "–")
        elif k == "t_stat":
            out.append(f"{v:+.1f}" if v == v else "–")
        else:
            out.append(f"{v:+.2f}" if v == v else "–")
    return out


# ─────────────────────────────── does it matter? (formal tests) ───────────────────────────────


def _boot(groups: dict, stat, n: int = 2000) -> tuple[float, float, float]:
    """Point estimate and 95 % CI of stat(list of records), resampling whole epochs (markets are
    correlated within a 5-minute window)."""
    keys = list(groups)
    est = stat([r for k in keys for r in groups[k]])
    vals = []
    for _ in range(n):
        pick = rng.choice(len(keys), len(keys), replace=True)
        v = stat([r for i in pick for r in groups[keys[i]]])
        if v == v:
            vals.append(v)
    return est, float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def _by_epoch(rows):
    g = defaultdict(list)
    for r in rows:
        g[r["epoch"]].append(r)
    return g


def _mean(rows, key, cond=lambda r: True):
    v = [r[key] for r in rows if cond(r)]
    return float(np.mean(v)) * 100 if v else float("nan")


def significance(d, P) -> None:
    """Budget-free fills (the per-market rules only, which do not depend on the exit), so every exit
    is scored on the SAME fills and the comparisons are paired. Epoch-clustered bootstrap CIs."""
    free = dict(positions=10**6, exposure=1e12, per_hour=10**6)
    base = {r["n"]: r for r in trades(d, 0.95, "live", **free)}
    P("## Does it matter? Formal tests (budget-free, MAX_ASK 0.95 fills, epoch-clustered 95 % bootstrap CIs)\n")
    P(f"Fills: {len(base)}. c/share net. 'unseen' = unseen_1 + unseen_2 + unseen_3 pooled.\n")
    groups = {"train": ("train",), "validation": ("validation",), "unseen": UNSEEN, "all": S.SPLITS}

    P("### MAX_ASK: are fills above the ceiling worse than fills below it? (production exit)\n")
    P("| split at | period | fills below | c/share below | fills above | c/share above | difference (below - above) | 95 % CI |")
    P("|---:|---|---:|---:|---:|---:|---:|---|")
    for cut in (0.60, 0.70, 0.75, 0.80):
        for name, ps in groups.items():
            rows = [r for r in base.values() if S.SPLITS[r["split"]] in ps]
            lo = [r for r in rows if r["ask"] <= cut + 1e-9]
            hi = [r for r in rows if r["ask"] > cut + 1e-9]
            est, a, b = _boot(_by_epoch(rows), lambda rr: _mean(rr, "net", lambda r: r["ask"] <= cut + 1e-9)
                              - _mean(rr, "net", lambda r: r["ask"] > cut + 1e-9))
            P(f"| {cut:.2f} | {name} | {len(lo)} | {_mean(lo, 'net'):+.2f} | {len(hi)} | {_mean(hi, 'net'):+.2f} | "
              f"{est:+.2f} | [{a:+.1f}, {b:+.1f}] |")
    P("")

    # paired exit differences on the same fills
    paired = {}
    for ex in S.EXITS:
        if ex == "live":
            continue
        rows = []
        for r in trades(d, 0.95, ex, **free):
            b = base.get(r["n"])
            if b is not None:
                rows.append({"epoch": r["epoch"], "split": r["split"], "ask": r["ask"], "diff": r["net"] - b["net"]})
        paired[ex] = rows
    P("### Exit: each exit minus the production exit, on the same fills\n")
    res = {}
    for ex, rows in paired.items():
        out = {}
        for name, ps in groups.items():
            rr = [r for r in rows if S.SPLITS[r["split"]] in ps]
            out[name] = _boot(_by_epoch(rr), lambda x: _mean(x, "diff"), n=600)
        res[ex] = out
    better = [ex for ex, o in res.items() if o["all"][1] > 0]
    worse = [ex for ex, o in res.items() if o["all"][2] < 0]
    P(f"{len(res)} alternative exits. CI (all periods) entirely above 0: {len(better)} "
      f"({', '.join(better) or 'none'}); entirely below 0: {len(worse)} ({', '.join(worse) or 'none'}). "
      f"With {len(res)} comparisons about {0.025 * len(res):.0f} would clear one side by chance.\n")
    P("| exit | train diff | unseen diff | all diff | all 95 % CI |")
    P("|---|---:|---:|---:|---|")
    order = sorted(res, key=lambda ex: -res[ex]["all"][0])
    for ex in order[:10] + ["..."] + order[-8:]:
        if ex == "...":
            P("| ... | | | | |")
            continue
        o = res[ex]
        P(f"| {ex} | {o['train'][0]:+.2f} | {o['unseen'][0]:+.2f} | {o['all'][0]:+.2f} | "
          f"[{o['all'][1]:+.1f}, {o['all'][2]:+.1f}] |")
    P("")

    P("### MAX_ASK x exit: does the exit's effect depend on the token price? (all periods)\n")
    P("(exit minus production exit) on fills with ask <= 0.60, the same on fills with ask > 0.60, and the "
      "difference of the two (the interaction)\n")
    P("| exit | ask <= 0.60 | ask > 0.60 | interaction | 95 % CI |")
    P("|---|---:|---:|---:|---|")
    cond_lo, cond_hi = (lambda r: r["ask"] <= 0.60 + 1e-9), (lambda r: r["ask"] > 0.60 + 1e-9)
    n_sig = 0
    inter = {}
    for ex, rows in paired.items():
        est, a, b = _boot(_by_epoch(rows), lambda x: _mean(x, "diff", cond_lo) - _mean(x, "diff", cond_hi), n=600)
        inter[ex] = (_mean(rows, "diff", cond_lo), _mean(rows, "diff", cond_hi), est, a, b)
        n_sig += (a > 0) or (b < 0)
    for ex in sorted(inter, key=lambda k: -abs(inter[k][2]))[:12]:
        lo, hi, est, a, b = inter[ex]
        P(f"| {ex} | {lo:+.2f} | {hi:+.2f} | {est:+.2f} | [{a:+.1f}, {b:+.1f}] |")
    P(f"\nInteraction CI excludes 0 for {n_sig} of {len(inter)} exits (about {0.05 * len(inter):.0f} expected by chance).\n")


def main() -> None:
    df = load_sweep()
    d = S.Data(1.0)
    hours = S.hours_by_period(d)
    hours["unseen"] = sum(hours[p] for p in UNSEEN)
    hours["all"] = sum(hours[p] for p in S.SPLITS)
    L: list[str] = []
    P = L.append

    # ---------- selection and LOCK (training and validation only) ----------
    chosen = select(df)
    lock = {"candidates": {k: v for k, v in chosen.items()}, "baseline": list(BASE),
            "rule": "TRAINING: >= 30 fills, net > 0; categories by training t-stat / fills (t >= 2) / net per share / "
                    "net-to-drawdown (t >= 2); VALIDATION removes any pick with net <= 0 (next training candidate)",
            "locked_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")}
    lock_file = RES / "spot_lag_parameter_sweep_locked.json"
    if lock_file.exists():      # a re-run keeps the original lock -- and refuses to change it silently
        old = json.loads(lock_file.read_text())
        if {k: list(v) if v else None for k, v in chosen.items()} != old["candidates"]:
            sys.exit(f"selection differs from the lock in {lock_file}; delete it to re-lock on purpose")
        lock["locked_at"] = old["locked_at"]
    lock_file.write_text(json.dumps(lock, indent=1))
    cands = {"current baseline": BASE}
    for k, v in chosen.items():
        if v is not None:
            cands[k] = v

    # ---------- summary CSV: every combination, train / validation / each unseen / pooled unseen ----------
    rows = []
    g = df.set_index(["max_ask", "exit", "period"])
    for (ma, ex), _ in df.groupby(["max_ask", "exit"]):
        r = {"max_ask": ma, "exit": ex}
        for p in ("train", "validation") + UNSEEN + ("unseen_3b", "all"):
            x = g.loc[(ma, ex, p)]
            r.update({f"{p}_fills": x.fills, f"{p}_net_usdc": x.net_usdc, f"{p}_net_c_share": x.net_c_share,
                      f"{p}_t": x.t_stat})
        f = sum(g.loc[(ma, ex, p)].fills for p in UNSEEN)
        n = sum(np.nan_to_num(g.loc[(ma, ex, p)].net_usdc) for p in UNSEEN)
        r.update(unseen_fills=f, unseen_net_usdc=n, unseen_net_c_share=n / (f * S.SIZE) * 100 if f else np.nan,
                 unseen_periods_positive=int(sum(np.nan_to_num(g.loc[(ma, ex, p)].net_usdc) > 0 for p in UNSEEN)))
        rows.append(r)
    summ = pd.DataFrame(rows)
    summ.to_csv(RES / "spot_lag_parameter_sweep_summary.csv", index=False)

    P("# Spot-lag parameter sweep: tables\n")
    P(f"Signals (production entry gates, research ceiling 0.98, HYPE excluded): {int(((d.col('signal') == 1) & (d.col('asset') != S.ASSETS.index('HYPE'))).sum())}; "
      f"candidates (every production shock with a book): {len(d.C)}. Hours: " +
      ", ".join(f"{p} {hours[p]:.1f}" for p in S.PERIODS) + "\n")
    P("## Locked candidates (chosen on TRAINING, filtered on VALIDATION)\n")
    P(f"`{json.dumps(lock['candidates'])}` locked {lock['locked_at']}\n")

    # ---------- the locked candidates on every period ----------
    P("## Locked candidates and baseline, by period\n")
    P("| candidate | MAX_ASK | exit | period | fills | win | net USDC | c/share | t | max DD |\n|---|---:|---|---|---:|---:|---:|---:|---:|---:|")
    cand_recs = {}
    for name, (ma, ex) in cands.items():
        recs = S.simulate(d, ma, ex)
        cand_recs[name] = recs
        for p in ("train", "validation") + UNSEEN + ("unseen_3b", "unseen", "all"):
            rr = period_recs(recs, p) if p not in ("all",) else recs
            m = S.metrics(rr, hours[p])
            P(f"| {name} | {ma:.2f} | {ex} | {p} | " + " | ".join(fmt(m)) + " |")
    P("")

    # ---------- statistical validation, unseen pooled ----------
    P("## Statistical validation (unseen periods pooled)\n")
    P("| candidate | n | mean c/share | median | sd | 95% CI (clustered) | 95% bootstrap CI | t | ex top-5 trades | top epoch share | per unseen period c/share |")
    P("|---|---:|---:|---:|---:|---|---|---:|---:|---:|---|")
    for name, recs in cand_recs.items():
        u = period_recs(recs, "unseen")
        m = stats_block(u, hours["unseen"])
        per = " / ".join((lambda mm: f"{mm['net_c_share']:+.1f} ({mm['fills']})" if mm.get("fills", 0) else "–")(
            S.metrics(S.split_recs(recs, p), hours[p])) for p in UNSEEN + ("unseen_3b",))
        P(f"| {name} | {m.get('fills', 0)} | {m.get('net_c_share', float('nan')):+.2f} | {m.get('median_c_share', float('nan')):+.2f} | "
          f"{m.get('sd_c_share', float('nan')):.2f} | [{m.get('ci95_lo', float('nan')):+.1f}, {m.get('ci95_hi', float('nan')):+.1f}] | "
          f"[{m.get('boot_lo', float('nan')):+.1f}, {m.get('boot_hi', float('nan')):+.1f}] | {m.get('t_stat', float('nan')):+.1f} | "
          f"{m.get('net_c_share_ex_top5', float('nan')):+.2f} | {m.get('top_epoch_share', float('nan')):.0%} | {per} |")
    P("")

    # ---------- MAX_ASK sweep, production exit ----------
    P("## MAX_ASK sweep (production exit: 5 s, held to 10 s while the spot keeps moving)\n")
    P("| MAX_ASK | signals | rejected by MAX_ASK | orders | fills | fill rate | win | gross | fees | spread | slippage | net USDC | "
      "c/share | median | max DD | PF | hold | USDC/market | USDC/h | USDC/fill | USDC/order | train c/share | unseen c/share (fills) |")
    P("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for ma in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.82, 0.85, 0.87, 0.90, 0.92, 0.95, 0.98):
        recs = S.simulate(d, ma, "live")
        m = S.metrics(recs, hours["all"])
        mt = S.metrics(S.split_recs(recs, "train"), hours["train"])
        mu = S.metrics(period_recs(recs, "unseen"), hours["unseen"])
        P(f"| {ma:.2f} | {m['signals']} | {m['rejected_max_ask_pct']:.0%} | {m['orders']} | {m['fills']} | {m['fill_rate']:.0%} | "
          f"{m.get('win_rate', 0):.0%} | {m.get('gross_usdc', 0):+.2f} | {m.get('fees_usdc', 0):.2f} | {m.get('spread_usdc', 0):.2f} | "
          f"{m.get('slippage_usdc', 0):+.2f} | {m.get('net_usdc', 0):+.2f} | {m.get('net_c_share', 0):+.2f} | {m.get('median_c_share', 0):+.2f} | "
          f"{m.get('max_dd_usdc', 0):.2f} | {m.get('profit_factor', 0):.2f} | {m.get('avg_hold_s', 0):.1f} s | {m.get('usdc_per_market', 0):+.3f} | "
          f"{m.get('usdc_per_hour', 0):+.3f} | {m.get('usdc_per_fill', 0):+.3f} | {m.get('usdc_per_order', 0):+.3f} | "
          f"{mt.get('net_c_share', float('nan')):+.2f} | {mu.get('net_c_share', float('nan')):+.2f} ({mu.get('fills', 0)}) |")
    P("")
    # per asset for the MAX_ASK sweep ends
    for ma in (0.60, 0.80, 0.95):
        recs = trades(d, ma, "live", hype=True)
        P(f"### MAX_ASK {ma:.2f}, production exit, by asset (HYPE shown as if included)\n")
        P("| asset | fills | c/share | net USDC | share of total |\n|---|---:|---:|---:|---:|")
        tot = sum(r["net"] for r in recs if r["asset"] != S.ASSETS.index("HYPE"))
        for a, an in enumerate(S.ASSETS):
            ra = [r["net"] for r in recs if r["asset"] == a]
            if ra:
                P(f"| {an} | {len(ra)} | {np.mean(ra) * 100:+.2f} | {sum(ra) * S.SIZE:+.2f} | "
                  f"{(sum(ra) / tot if an != 'HYPE' and tot else float('nan')):.0%} |")
        P("")

    # ---------- exits ----------
    P("## Exits (MAX_ASK 0.95 and 0.60)\n")
    P("| exit | 0.95: fills | win | c/share | train | unseen | hold | max DD | 0.60: fills | c/share | train | unseen | hold |")
    P("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for ex in S.EXITS:
        cells = []
        for ma in (0.95, 0.60):
            a = g.loc[(ma, ex, "all")]
            t_ = g.loc[(ma, ex, "train")]
            uf = sum(g.loc[(ma, ex, p)].fills for p in UNSEEN)
            unet = sum(np.nan_to_num(g.loc[(ma, ex, p)].net_usdc) for p in UNSEEN)
            uc = unet / (uf * S.SIZE) * 100 if uf else float("nan")
            if ma == 0.95:
                cells += [f"{int(a.fills)}", f"{a.win_rate:.0%}", f"{a.net_c_share:+.2f}", f"{t_.net_c_share:+.2f}", f"{uc:+.2f}",
                          f"{a.avg_hold_s:.1f} s", f"{a.max_dd_usdc:.2f}"]
            else:
                cells += [f"{int(a.fills)}", f"{a.net_c_share:+.2f}", f"{t_.net_c_share:+.2f}", f"{uc:+.2f}", f"{a.avg_hold_s:.1f} s"]
        P(f"| {ex} | " + " | ".join(cells) + " |")
    P("")

    # ---------- exit behaviour: the price path after a fill ----------
    fillmask = (d.col("filled") == 1) & (d.col("asset") != S.ASSETS.index("HYPE"))
    Pp = d.P[fillmask]
    P("## What the price did after a fill (production orders, MAX_ASK 0.98, before the budget)\n")
    P(f"fills {int(fillmask.sum())}; best bid within 30 s minus entry: mean {np.nanmean(Pp[:, d.p['mfe30']]) * 100:+.2f}c, "
      f"median {np.nanmedian(Pp[:, d.p['mfe30']]) * 100:+.2f}c; worst: mean {np.nanmean(Pp[:, d.p['mae30']]) * 100:+.2f}c; "
      f"best-to-close of trading: mean {np.nanmean(Pp[:, d.p['mfe_all']]) * 100:+.2f}c; came back below the entry after its 30 s best: "
      f"{np.nanmean(Pp[:, d.p['rev_after_mfe']]):.0%}\n")
    P("| target (bid - entry) | reached within 30 s | median time to reach |\n|---|---:|---:|")
    for c in S.PATH_TARGETS_C:
        P(f"| +{c}c | {np.nanmean(Pp[:, d.p[f'hit{c}']]):.0%} | {np.nanmedian(Pp[:, d.p[f't_hit{c}']]):.1f} s |")
    P("")
    P("| exit (MAX_ASK 0.98, no budget) | realised gross c/share | best available within 30 s | left on the table | forced (time) exits |")
    P("|---|---:|---:|---:|---:|")
    fi = np.where(fillmask)[0]
    for ex in ("live", "t1", "t2", "t5", "t10", "t20", "t30", "tp3_10", "tp5_20", "tp10_20", "tp10_settle", "tp20_settle",
               "spot_rev_20", "tok_rev_20", "gap_closed_20", "edge_gone_20"):
        e = d.e[ex]
        px = d.X[fi, e, 0]
        ok = ~np.isnan(px)
        gross = px[ok] - d.col("entry")[fi][ok]
        mfe = Pp[ok, d.p["mfe30"]]
        forced = "–"
        if ex.startswith("tp"):
            c = int(ex[2:].split("_")[0]) / 100.0
            forced = f"{np.mean(gross < c - 1e-9):.0%}"
        P(f"| {ex} | {np.mean(gross) * 100:+.2f} | {np.nanmean(mfe) * 100:+.2f} | {np.nanmean(mfe - gross) * 100:+.2f} | {forced} |")
    P("")

    # ---------- price buckets ----------
    P("## By token price (production orders and exit, no MAX_ASK, HYPE excluded)\n")
    P("| ask | signals | fills | fill rate | win | net USDC | c/share | train c/share | unseen c/share | edge (ev - ask) | spread | best move 30 s | exit gross |")
    P("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    recs98 = S.simulate(d, 0.98, "live")
    for lo, hi in ((0.05, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 0.98)):
        rr = [r for r in recs98 if lo <= d.C[r["n"], d.c["ask"]] < hi + (1e-9 if hi == 0.98 else 0)]
        f = [r for r in rr if r["status"] == "filled"]
        orders = sum(r["status"] in ("filled", "killed") for r in rr)
        if not rr:
            continue
        net = np.array([r["net"] for r in f]) if f else np.array([np.nan])
        ft = [r["net"] for r in f if S.SPLITS[r["split"]] == "train"]
        fu = [r["net"] for r in f if S.SPLITS[r["split"]] in UNSEEN]
        ns = [r["n"] for r in f]
        P(f"| {lo:.2f}-{hi:.2f} | {len(rr)} | {len(f)} | {len(f) / max(1, orders):.0%} | {np.mean(net > 0):.0%} | {np.nansum(net) * S.SIZE:+.2f} | "
          f"{np.nanmean(net) * 100:+.2f} | {np.mean(ft) * 100 if ft else float('nan'):+.2f} ({len(ft)}) | {np.mean(fu) * 100 if fu else float('nan'):+.2f} ({len(fu)}) | "
          f"{np.mean(d.col('ev')[ns] - d.col('ask')[ns]) * 100 if ns else float('nan'):+.2f} | "
          f"{np.mean(d.col('ask')[ns] - d.col('bid')[ns]) * 100 if ns else float('nan'):.2f} | "
          f"{np.nanmean(d.P[ns, d.p['mfe30']]) * 100 if ns else float('nan'):+.2f} | {np.mean([r['gross'] for r in f]) * 100 if f else float('nan'):+.2f} |")
    P("")

    # ---------- market conditions ----------
    P("## By market condition (production exit): does the right MAX_ASK depend on it?\n")
    P("net c/share (fills) for asks <= 0.60 / 0.60-0.80 / > 0.80, all periods; then train | unseen for the whole bucket\n")
    conds = {
        "shock size (z1)": ("z1", ((3, 4), (4, 6), (6, 1e9)), lambda x: x),
        "time left (s)": ("left", ((60, 120), (120, 200), (200, 300)), lambda x: x),
        "spread (c)": (None, ((0, 1.5), (1.5, 2.5), (2.5, 3.5)), lambda n: (d.C[n, d.c["ask"]] - d.C[n, d.c["bid"]]) * 100),
        "lag ev - ask (c)": (None, ((0, 4), (4, 8), (8, 100)), lambda n: (d.C[n, d.c["ev"]] - d.C[n, d.c["ask"]]) * 100),
        "spot 3 s move (sigma)": ("z3", ((0, 2), (2, 4), (4, 1e9)), lambda x: x),
        "token move last 1 s (c)": (None, ((-100, 0.001), (0.001, 2), (2, 100)), lambda n: d.C[n, d.c["tok1"]] * 100),
        "direction": ("dir", ((-1.5, 0), (0, 1.5)), lambda x: x),
        "volatility (sigma bps/s)": ("sigma_bps", ((0, 0.6), (0.6, 1.2), (1.2, 1e9)), lambda x: x),
    }
    for title, (col, bks, fn) in conds.items():
        P(f"**{title}**\n")
        P("| bucket | asks <= 0.60 | 0.60-0.80 | > 0.80 | all asks: train | unseen |\n|---|---|---|---|---|---|")
        for lo, hi in bks:
            def val(n):
                return fn(d.C[n, d.c[col]]) if col else fn(n)
            f = [r for r in recs98 if r["status"] == "filled" and lo <= val(r["n"]) < hi]
            cells = []
            for alo, ahi in ((0, 0.60), (0.60, 0.80), (0.80, 1.0)):
                v = [r["net"] for r in f if alo < r["ask"] <= ahi + 1e-9]
                cells.append(f"{np.mean(v) * 100:+.1f} ({len(v)})" if v else "–")
            vt = [r["net"] for r in f if S.SPLITS[r["split"]] == "train"]
            vu = [r["net"] for r in f if S.SPLITS[r["split"]] in UNSEEN]
            lab = ("DOWN" if lo < 0 else "UP") if title == "direction" else f"{lo:g}-{hi:g}" if hi < 1e8 else f">= {lo:g}"
            P(f"| {lab} | " + " | ".join(cells) + f" | {np.mean(vt) * 100 if vt else float('nan'):+.1f} ({len(vt)}) | "
              f"{np.mean(vu) * 100 if vu else float('nan'):+.1f} ({len(vu)}) |")
        P("")
    # a research-only dynamic ceiling, chosen on TRAINING: asks above 0.60 only when the lag is large
    P("### A research-only dynamic ceiling: MAX_ASK 0.60, or higher when the lag (ev - ask) is at least L cents\n")
    P("| rule | train fills | train c/share | unseen fills | unseen c/share |\n|---|---:|---:|---:|---:|")
    best_dyn = None
    for L_c in (4, 6, 8, 10):
        for hi in (0.80, 0.95):
            f = [r for r in recs98 if r["status"] == "filled" and (r["ask"] <= 0.60 + 1e-9 or
                 (r["ask"] <= hi + 1e-9 and (d.C[r["n"], d.c["ev"]] - r["ask"]) * 100 >= L_c))]
            vt = [r["net"] for r in f if S.SPLITS[r["split"]] == "train"]
            vu = [r["net"] for r in f if S.SPLITS[r["split"]] in UNSEEN]
            key = (np.sum(vt), f"<= 0.60, or <= {hi:.2f} with lag >= {L_c}c")
            if best_dyn is None or key[0] > best_dyn[0]:
                best_dyn = key
            P(f"| <= 0.60, or <= {hi:.2f} with lag >= {L_c}c | {len(vt)} | {np.mean(vt) * 100:+.2f} | {len(vu)} | {np.mean(vu) * 100:+.2f} |")
    P(f"\n(budget-free; chosen on training total: {best_dyn[1]})\n")

    # ---------- stress ----------
    P("## Stress tests (locked candidates and baseline): unseen pooled c/share (fills) | all periods c/share\n")
    scen = [("as measured", 1.0, {}), ("latency +25 %", 1.25, {}), ("latency +50 %", 1.5, {}), ("latency x2", 2.0, {}),
            ("spread +1c", 1.0, {"spread_add": 0.01}), ("spread +2c", 1.0, {"spread_add": 0.02}),
            ("slippage +0.5c per leg", 1.0, {"slip_leg": 0.005}), ("slippage +1c per leg", 1.0, {"slip_leg": 0.01}),
            ("25 % of fills lost at random", 1.0, {"drop": "random"}), ("the best 25 % of fills lost", 1.0, {"drop": "best"}),
            ("fees x1.5", 1.0, {"fee_mult": 1.5}), ("fees x0.5", 1.0, {"fee_mult": 0.5})]
    dsets = {1.0: d}
    for s in (1.25, 1.5, 2.0):
        dsets[s] = S.Data(s)
    P("| scenario | " + " | ".join(cands) + " |\n|---|" + "---|" * len(cands))
    for sname, sc, kw in scen:
        cells = []
        for name, (ma, ex) in cands.items():
            dd = dsets[sc]
            kw2 = {k: v for k, v in kw.items() if k != "drop"}
            if "drop" in kw:
                keep = np.ones(len(dd.C), bool)
                fi_ = np.where(dd.col("filled") == 1)[0]
                if kw["drop"] == "random":
                    keep[fi_[rng.random(len(fi_)) < 0.25]] = False
                else:
                    e = dd.e[ex]
                    gains = dd.X[fi_, e, 0] - dd.col("entry")[fi_]
                    thr = np.nanquantile(gains, 0.75)
                    keep[fi_[np.nan_to_num(gains, nan=-9) >= thr]] = False
                kw2["fill_keep"] = keep
            recs = S.simulate(dd, ma, ex, **kw2)
            mu = S.metrics(period_recs(recs, "unseen"), hours["unseen"])
            ma_ = S.metrics(recs, hours["all"])
            cells.append(f"{mu.get('net_c_share', float('nan')):+.2f} ({mu.get('fills', 0)}) \\| {ma_.get('net_c_share', float('nan')):+.2f}")
        P(f"| {sname} | " + " | ".join(cells) + " |")
    P("")

    # ---------- the realism check: the live bot's own run ----------
    t0 = dt.datetime(2026, 9, 24, 15, 23, 45).timestamp() * 1000
    t1 = dt.datetime(2026, 9, 25, 0, 22, 0).timestamp() * 1000
    recs = S.simulate(d, 0.95, "live", positions=1, per_hour=5)
    rr = [r for r in recs if t0 <= d.C[r["n"], d.c["t"]] <= t1]
    orders = sum(r["status"] in ("filled", "killed") for r in rr)
    f = [r for r in rr if r["status"] == "filled"]
    significance(d, P)

    P("## Realism check: the replay of the live bot's own configuration over its own run\n")
    P(f"09-24 15:23 -> 09-25 00:22 UTC, production rule with the running ceilings (1 position, 5 buys / h): replay "
      f"{orders} orders, {len(f)} fills ({len(f) / max(1, orders):.0%}), net {sum(r['net'] for r in f) * S.SIZE:+.2f} USDC. "
      "The live bot: 36 orders, 3 fills (8 %), -0.13 USDC (its log; it also skips on quote age > 250 ms, which the archive "
      "cannot see).\n")

    (RES / "spot_lag_parameter_sweep_report.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
