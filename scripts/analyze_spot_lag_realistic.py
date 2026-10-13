"""Spot-lag realistic optimisation, analysis: walk-forward selection on the stage-1 sweep,
then gates / exits / sizes on the finalists, Monte Carlo, stress, the counterfactual waterfall,
sensitivity, attribution and the success criteria. Writes results/spot_lag_realistic_report.md,
results/spot_lag_realistic_summary.json and results/spot_lag_realistic_locked.json.

    python scripts/analyze_spot_lag_realistic.py      # after research_spot_lag_realistic.py build + sweep
RESEARCH ONLY.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import research_spot_lag_realistic as S      # noqa: E402
from troll_poly_bot.backtest.spot_shock_replay import load_asset, windows   # noqa: E402

RES = ROOT / "results"
rng = np.random.default_rng(20260925)
TRAIN_B, VAL_B, TEST_B0 = range(0, 7), range(7, 10), 10          # 6 h blocks; TEST = blocks >= 10 (after 00:45 UTC 09-25)
GRIDS = {"k": S.K_SET, "edge": S.EDGES, "slip": S.SLIPS, "spread": S.SPREADS, "max_ask": S.MAX_ASKS, "window": S.WINDOWS}
KEYS = ("k", "edge", "slip", "spread", "max_ask", "window")
MA74 = S.cfg_tuple(max_ask=0.74)
PRODC = S.cfg_tuple()


def key_of(cfg):
    return tuple(cfg[:5]) + (tuple(cfg[5]),)


def fmt_cfg(cfg):
    k, e, s, sp, m, (lo, hi), q, sa = cfg
    return f"{k:g}σ, edge {e*100:g}c, +{s*100:g}c, spread {sp*100:g}c, ask≤{m:.2f}, {lo}–{hi}s, quote≤{q}ms, spot≤{sa}ms"


# ─────────────────────────────── stage 1: the sweep by blocks ───────────────────────────────


class Sweep:
    def __init__(self, tag="base"):
        z = np.load(RES / f"spot_lag_realistic_sweep_{tag}.npz")
        self.S, self.DD = z["S"], z["DD"]
        self.cfgs = [json.loads(c) for c in z["cfgs"]]
        self.cfgs = [(c[0], c[1], c[2], c[3], c[4], tuple(c[5]), c[6], c[7]) for c in self.cfgs]
        self.index = {key_of(c): i for i, c in enumerate(self.cfgs)}
        self.bh = z["block_hours"]
        self.nb = self.S.shape[2]

    def per(self, blocks, stat):
        """Mean over draws of the block-summed statistic: 0 orders, 1 fills, 2 net, 4 wins."""
        b = [x for x in blocks if x < self.nb]
        return self.S[:, :, b, stat].sum(axis=2).mean(axis=1)

    def per_draw(self, blocks, stat=2):
        b = [x for x in blocks if x < self.nb]
        return self.S[:, :, b, stat].sum(axis=2)

    def block_dd(self, blocks):
        """Drawdown of the block-level P&L path (a lower bound on the trade-level drawdown)."""
        b = [x for x in blocks if x < self.nb]
        net = self.S[:, :, b, 2]
        cum = np.concatenate([np.zeros(net.shape[:2] + (1,)), np.cumsum(net, axis=2)], axis=2)
        return (np.maximum.accumulate(cum, axis=2) - cum).max(axis=2).mean(axis=1)

    def smooth(self, values):
        """Average of a config and its one-step grid neighbours (a plateau, not a spike)."""
        out = np.empty_like(values)
        for i, c in enumerate(self.cfgs):
            vals = [values[i]]
            kc = dict(zip(KEYS, key_of(c)))
            for p, g in GRIDS.items():
                gi = list(g).index(kc[p])
                for j in (gi - 1, gi + 1):
                    if 0 <= j < len(g):
                        nb = dict(kc, **{p: g[j]})
                        k2 = tuple(nb[x] for x in KEYS)
                        if k2 in self.index:
                            vals.append(values[self.index[k2]])
            out[i] = np.mean(vals)
        return out

    def select(self, train_b, val_b, min_fills=30, max_dd=5.0):
        """The rule used everywhere: eligible = >= min_fills training fills, training net > 0,
        block drawdown <= max_dd; ranked by neighbour-smoothed training net; the first with a
        positive validation net (>= 5 fills) wins."""
        tr_net, tr_f, tr_dd = self.per(train_b, 2), self.per(train_b, 1), self.block_dd(train_b)
        va_net, va_f = self.per(val_b, 2), self.per(val_b, 1)
        sm = self.smooth(tr_net)
        elig = (tr_f >= min_fills) & (tr_net > 0) & (tr_dd <= max_dd)
        order = np.argsort(-sm)
        for i in order:
            if elig[i] and va_f[i] >= 5 and va_net[i] > 0:
                return i, {"train_net": tr_net[i], "train_fills": tr_f[i], "smoothed": sm[i], "val_net": va_net[i], "val_fills": va_f[i]}
        return None, {}


# ─────────────────────────────── trade-level helpers ───────────────────────────────


def run(d, cfg, draws=None, **kw):
    """Simulate over draws; returns a list of (o_idx, o_fill, o_sh, o_net, o_nps)."""
    return [S.simulate(d, cfg, q, **kw) for q in (range(d.draws) if draws is None else draws)]


def period_masks(d):
    m = {"train": np.isin(d.block, list(TRAIN_B)), "validation": np.isin(d.block, list(VAL_B)),
         "test": d.block >= TEST_B0}
    m["all"] = m["train"] | m["validation"] | m["test"]
    m["dev"] = m["train"] | m["validation"]
    return m


def hours_of(d, mask):
    return len(np.unique(d.col["epoch"][mask])) / 12.0


def agg(d, runs, mask, hours):
    """Draw-averaged stats with the across-draw range of net."""
    st = [S.stats(d, *r, mask=mask, hours=hours) for r in runs]
    nets = [s["net_usdc"] for s in st]
    out = {k: float(np.nanmean([s[k] for s in st])) for k in ("orders", "fills", "fill_rate", "fills_per_h", "net_usdc", "c_share",
                                                              "win", "t", "max_dd", "worst", "streak", "sharpe", "avg_win", "avg_loss")}
    out["pf"] = float(np.nanmedian([s["pf"] for s in st]))
    out["net_lo"], out["net_hi"] = float(min(nets)), float(max(nets))
    return out


def line(name, a):
    return (f"| {name} | {a['orders']:.0f} | {a['fills']:.0f} | {a['fill_rate']:.0%} | {a['fills_per_h']:.1f} | {a['net_usdc']:+.2f} "
            f"[{a['net_lo']:+.1f}, {a['net_hi']:+.1f}] | {a['c_share']:+.1f} | {a['win']:.0%} | {a['pf']:.2f} | {a['max_dd']:.2f} | "
            f"{a['worst']:+.2f} | {a['streak']:.0f} | {a['t']:+.1f} |")


HDR = ("| candidate | orders | fills | fill rate | fills/h | net USDC [draw range] | c/share | win | PF | max DD | worst trade | "
       "losing streak | t |\n|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|")


# ─────────────────────────────── main ───────────────────────────────


def main() -> None:
    sw = Sweep("base")
    d = S.Data("base")
    M = period_masks(d)
    H = {p: hours_of(d, m) for p, m in M.items()}
    L: list[str] = []
    P = L.append
    summary: dict = {}
    P("# Spot-lag realistic optimisation: tables\n")
    P(f"Candidates (HYPE excluded): {len(d.C)}; latency draws {d.draws}; configurations in stage 1: {len(sw.cfgs)}. "
      f"Hours: train {H['train']:.1f} (blocks 0-6), validation {H['validation']:.1f} (blocks 7-9), test {H['test']:.1f} "
      f"(blocks >= 10, after 00:45 UTC 09-25). Block = 6 h from 09-22 12:45 UTC.\n")

    # ---- latency draws as used
    P("## Latency stages actually drawn (ms, all candidates x draws)\n")
    P("| stage | p50 | p75 | p90 | p95 | p99 |\n|---|---:|---:|---:|---:|---:|")
    for i, n in enumerate(S.DRAW[:5]):
        v = d.D[:, :, i].ravel()
        P(f"| {n} | " + " | ".join(f"{np.percentile(v, q):.0f}" for q in (50, 75, 90, 95, 99)) + " |")
    dec_to_arr = (d.D[:, :, S.DRAW.index("t_arr")] - d.col["t"][:, None]).ravel()
    P("| shock on this machine -> order at the venue | " + " | ".join(f"{np.percentile(dec_to_arr, q):.0f}" for q in (50, 75, 90, 95, 99)) + " |")
    qa = (d.col["qage_local"][:, None] + d.D[:, :, 2] + d.D[:, :, 1]).ravel()
    P("| quote age at the decision | " + " | ".join(f"{np.percentile(qa, q):.0f}" for q in (50, 75, 90, 95, 99)) + " |")
    P("")

    # ---- stage 1: final selection (train blocks 0-6, validation 7-9), then the untouched test
    pick, info = sw.select(TRAIN_B, VAL_B)
    best_cfg = sw.cfgs[pick]
    tr_net, va_net, te_net = sw.per(TRAIN_B, 2), sw.per(VAL_B, 2), sw.per(range(TEST_B0, sw.nb), 2)
    tr_f, va_f, te_f = sw.per(TRAIN_B, 1), sw.per(VAL_B, 1), sw.per(range(TEST_B0, sw.nb), 1)
    sm = sw.smooth(tr_net)
    elig = (tr_f >= 30) & (tr_net > 0)
    i_train = int(np.argmax(np.where(elig, tr_net, -np.inf)))
    i_val = int(np.argmax(np.where(elig & (va_f >= 5), va_net, -np.inf)))
    i_test = int(np.argmax(np.where(elig & (te_f >= 5), te_net, -np.inf)))
    lock = {"selected": list(best_cfg), "rule": "train blocks 0-6: >= 30 fills, net > 0, block DD <= 5; ranked by neighbour-smoothed "
            "train net; first with validation (blocks 7-9) net > 0 and >= 5 fills", "locked_at": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")}
    lock_file = RES / "spot_lag_realistic_locked.json"
    if lock_file.exists():
        old = json.loads(lock_file.read_text())
        if old["selected"] != json.loads(json.dumps(lock["selected"])):
            sys.exit(f"selection differs from {lock_file}; delete it to re-lock on purpose")
        lock["locked_at"] = old["locked_at"]
    lock_file.write_text(json.dumps(lock, indent=1))
    P("## Stage 1: entry grid, selected on TRAIN, filtered on VALIDATION, locked, then the untouched TEST\n")
    P(f"Locked {lock['locked_at']}: **{fmt_cfg(best_cfg)}** (smoothed train net {info['smoothed']:+.2f}, train {info['train_net']:+.2f} on "
      f"{info['train_fills']:.0f} fills, validation {info['val_net']:+.2f} on {info['val_fills']:.0f}).\n")
    P("| ranking | configuration | train net (fills) | validation net (fills) | **test net (fills)** | smoothed train |")
    P("|---|---|---:|---:|---:|---:|")
    for name, i in (("production (MAX_ASK 0.95)", sw.index[key_of(PRODC)]), ("ma74 paper bot", sw.index[key_of(MA74)]),
                    ("**selected (robust rule)**", pick), ("best raw training net", i_train), ("best validation net", i_val),
                    ("best test net (NOT selectable: chosen on the test)", i_test)):
        P(f"| {name} | {fmt_cfg(sw.cfgs[i])} | {tr_net[i]:+.2f} ({tr_f[i]:.0f}) | {va_net[i]:+.2f} ({va_f[i]:.0f}) | "
          f"**{te_net[i]:+.2f} ({te_f[i]:.0f})** | {sm[i]:+.2f} |")
    P("")
    n_pos_test = int(((te_f >= 5) & (te_net > 0)).sum())
    P(f"Of {len(sw.cfgs)} configurations, {int(elig.sum())} are eligible on training; {n_pos_test} of all have a positive test net "
      f"({n_pos_test / len(sw.cfgs):.0%}); among the eligible ones {int((elig & (te_net > 0)).sum())} ({(elig & (te_net > 0)).sum() / max(1, elig.sum()):.0%}). "
      f"Rank correlation (Spearman) of train net with test net over eligible configs: "
      f"{pd.Series(tr_net[elig]).rank().corr(pd.Series(te_net[elig]).rank()):+.2f}; validation with test: "
      f"{pd.Series(va_net[elig]).rank().corr(pd.Series(te_net[elig]).rank()):+.2f}.\n")
    # distribution of test net across the grid
    P("Test net across ALL configurations: " + ", ".join(f"p{q} {np.percentile(te_net, q):+.2f}" for q in (10, 25, 50, 75, 90)) + "\n")

    # ---- walk-forward
    P("## Walk-forward (6 h blocks; train = blocks 0..i+3, validation = block i+4, test = block i+5; the same rule each fold)\n")
    P("| fold | train blocks | val | test block (UTC start) | chosen configuration | test net (fills) | production in that block | ma74 in that block |")
    P("|---|---|---|---|---|---:|---:|---:|")
    wf_sum = wf_prod = wf_ma = 0.0
    wf_pos = 0
    nfold = 0
    for i in range(0, TEST_B0 - 5 + 1 + (sw.nb - TEST_B0)):
        tb = i + 5
        if tb >= sw.nb:
            break
        j, inf = sw.select(range(0, i + 4), [i + 4])
        t_start = dt.datetime.utcfromtimestamp((S.T0_BLOCKS + tb * S.BLOCK_MS) / 1000).strftime("%m-%d %H:%M")
        if j is None:
            P(f"| {i} | 0-{i+3} | {i+4} | {tb} ({t_start}) | none eligible | – | | |")
            continue
        net_t = sw.per([tb], 2)[j]
        f_t = sw.per([tb], 1)[j]
        pr, ma = sw.per([tb], 2)[sw.index[key_of(PRODC)]], sw.per([tb], 2)[sw.index[key_of(MA74)]]
        wf_sum += net_t
        wf_prod += pr
        wf_ma += ma
        wf_pos += net_t > 0
        nfold += 1
        mark = " **(untouched test)**" if tb >= TEST_B0 else ""
        P(f"| {i} | 0-{i+3} | {i+4} | {tb} ({t_start}){mark} | {fmt_cfg(sw.cfgs[j])} | {net_t:+.2f} ({f_t:.0f}) | {pr:+.2f} | {ma:+.2f} |")
    P(f"\nWalk-forward total: **{wf_sum:+.2f} USDC over {nfold} folds, {wf_pos} positive**; production over the same blocks {wf_prod:+.2f}; "
      f"ma74 {wf_ma:+.2f}.\n")
    summary.update(walk_forward_net=wf_sum, walk_forward_folds=nfold, walk_forward_positive=int(wf_pos), wf_prod=wf_prod, wf_ma74=wf_ma)

    # ---- finalists at trade level: stages 2-4
    finalists = {"production": PRODC, "ma74": MA74, "selected": best_cfg}
    if key_of(sw.cfgs[i_val]) not in {key_of(c) for c in finalists.values()}:
        finalists["best validation"] = sw.cfgs[i_val]
    P("## Stage 2: quote-age and spot-age gates on the selected configuration (net USDC, mean over draws)\n")
    base = finalists["selected"]
    P("| max quote age (ms) | " + " | ".join(f"spot ≤ {s} ms: train / val / test" for s in S.SAGES) + " |")
    P("|---:|" + "---|" * len(S.SAGES))
    gate_res = {}
    for q in S.QAGES:
        cells = []
        for sa in S.SAGES:
            cfg = tuple(list(base[:6]) + [q, sa])
            runs = run(d, cfg)
            a = {p: agg(d, runs, M[p], H[p]) for p in ("train", "validation", "test")}
            gate_res[(q, sa)] = a
            cells.append(f"{a['train']['net_usdc']:+.1f} ({a['train']['fills']:.0f}) / {a['validation']['net_usdc']:+.1f} / {a['test']['net_usdc']:+.1f}")
        P(f"| {q} | " + " | ".join(cells) + " |")
    # choose gates on train+val (dev), plateau: neighbours in the qage grid
    def dev(a):
        return a["train"]["net_usdc"] + a["validation"]["net_usdc"]
    best_gate = max(gate_res, key=lambda g: dev(gate_res[g]))
    # the plateau rule: the LEAST permissive quote age within 2 % of the best dev result (a
    # looser gate admits older quotes, so the tie goes to the tighter one), same for spot age
    best_dev = dev(gate_res[best_gate])
    plateau = [g for g in gate_res if dev(gate_res[g]) >= 0.98 * best_dev]
    best_gate = min(plateau, key=lambda g: (g[0], g[1]))
    P(f"\nBest on train + validation: quote ≤ {best_gate[0]} ms, spot ≤ {best_gate[1]} ms (dev {dev(gate_res[best_gate]):+.2f}, the least "
      f"permissive gate within 2 % of the best {best_dev:+.2f}; production 250 / 1000: {dev(gate_res[(250, 1000)]):+.2f}). Kept only if the gain is > 5 %.\n")
    keep_gate = dev(gate_res[best_gate]) > 1.05 * dev(gate_res[(250, 1000)])
    sel_gate = best_gate if keep_gate else (250, 1000)
    cfg2 = tuple(list(base[:6]) + list(sel_gate))

    P("## Stage 3: exits on the selected configuration (net USDC, mean over draws; hold s)\n")
    P("| exit | train net (fills) | validation | test | all c/share | hold | max DD (all) |\n|---|---:|---:|---:|---:|---:|---:|")
    exit_res = {}
    for ex in S.EXITS5:
        runs = run(d, cfg2, exit_name=ex)
        a = {p: agg(d, runs, M[p], H[p]) for p in ("train", "validation", "test", "all")}
        holds = np.concatenate([d.X[r[0][r[1]], q, S.SLOTS.index(ex), 1] for q, r in enumerate(runs)])
        exit_res[ex] = a
        P(f"| {ex} | {a['train']['net_usdc']:+.2f} ({a['train']['fills']:.0f}) | {a['validation']['net_usdc']:+.2f} | {a['test']['net_usdc']:+.2f} | "
          f"{a['all']['c_share']:+.1f} | {np.nanmean(holds):.1f} | {a['all']['max_dd']:.2f} |")
    best_exit = max(exit_res, key=lambda e: dev(exit_res[e]))
    keep_exit = dev(exit_res[best_exit]) > 1.05 * dev(exit_res["live"])
    sel_exit = best_exit if keep_exit else "live"
    P(f"\nBest on train + validation: {best_exit} ({dev(exit_res[best_exit]):+.2f} vs live {dev(exit_res['live']):+.2f}); selected: **{sel_exit}**.\n")

    P("## Stage 4: order size (existing caps: order ≤ 5 USDC, exposure ≤ 10; fills need the top level to hold the whole size)\n")
    P("| size | shares (typical) | orders | fills | fill rate | train net | validation | test | all net | max DD | note |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    size_res = {}
    for usd in S.SIZE_USD:
        note = "needs the 5 USDC order cap raised" if usd > 5 else ""
        runs = run(d, cfg2, exit_name=sel_exit, size_usd=usd, max_order=max(5.0, usd))
        a = {p: agg(d, runs, M[p], H[p]) for p in ("train", "validation", "test", "all")}
        sh = np.concatenate([r[2] for r in runs])
        size_res[usd] = a
        P(f"| {'venue min' if usd == 0 else f'${usd:g}'} | {np.median(sh):.0f} | {a['all']['orders']:.0f} | {a['all']['fills']:.0f} | {a['all']['fill_rate']:.0%} | "
          f"{a['train']['net_usdc']:+.2f} | {a['validation']['net_usdc']:+.2f} | {a['test']['net_usdc']:+.2f} | {a['all']['net_usdc']:+.2f} | {a['all']['max_dd']:.2f} | {note} |")
    P("")
    summary["recommended"] = {"config": list(cfg2), "exit": sel_exit, "size_usd": 0.0, "gate_changed": keep_gate, "exit_changed": keep_exit}

    # ---- finalists, full stats per period
    P("## Finalists by period (trade level, mean over draws; net range across draws)\n")
    fin_runs = {}
    for name, cfg in list(finalists.items()) + [("**recommended** (selected + gates + exit)", cfg2)]:
        ex = sel_exit if name.startswith("**recommended") else "live"
        fin_runs[name] = (cfg, ex, run(d, cfg, exit_name=ex))
    for p in ("train", "validation", "test", "all"):
        P(f"### {p} ({H[p]:.1f} h)\n")
        P(HDR)
        for name, (cfg, ex, runs) in fin_runs.items():
            a = agg(d, runs, M[p], H[p])
            P(line(name, a))
            summary.setdefault("finalists", {}).setdefault(name, {})[p] = a
        P("")
    rec_runs = fin_runs["**recommended** (selected + gates + exit)"][2]
    prod_runs = fin_runs["production"][2]

    # ---- per-block P&L for recommended vs production
    P("## P&L by 6 h block (mean over draws): recommended vs production\n")
    P("| block | UTC start | period | recommended net (fills) | production net (fills) |\n|---:|---|---|---:|---:|")
    for b in range(d.nblocks):
        m = d.block == b
        ar, ap = agg(d, rec_runs, m, d.block_hours[b]), agg(d, prod_runs, m, d.block_hours[b])
        per = "train" if b in TRAIN_B else "validation" if b in VAL_B else "TEST"
        P(f"| {b} | {dt.datetime.utcfromtimestamp((S.T0_BLOCKS + b * S.BLOCK_MS) / 1000):%m-%d %H:%M} | {per} | {ar['net_usdc']:+.2f} ({ar['fills']:.0f}) | {ap['net_usdc']:+.2f} ({ap['fills']:.0f}) |")
    P("")

    # ---- counterfactual waterfall
    P("## Counterfactual: where the paper edge goes (per share, mean over draws; ALL periods and TEST)\n")
    P("Each row adds one realism step to the one above. 'signal-time' = the book the bot saw when it decided.\n")
    P("| step | fills (all) | c/share (all) | net USDC (all) | fills (test) | c/share (test) | net USDC (test) |\n|---|---:|---:|---:|---:|---:|---:|")
    col = d.col
    fee = lambda p, r: r * p * (1 - p)                                     # noqa: E731
    for name_cfg, cfg in (("production", PRODC), ("recommended", cfg2)):
        P(f"| **{name_cfg}: {fmt_cfg(cfg)}** | | | | | | |")
        k, edge, slip, spread, max_ask, (lo, hi), qmax, smax = cfg
        me = d.me[edge]
        bit = 1 << S.K_SET.index(k)
        ask = col["ask"]
        sig = (((col["kmask"].astype(int) & bit) > 0) & (col["left"] >= lo) & (col["left"] <= hi) & (ask >= S.MIN_ASK)
               & (ask <= max_ask + 1e-9) & (ask - col["bid"] <= spread + 1e-9) & (col["ev"] - ask > 0) & (me >= ask - 1e-9))
        cap = np.minimum(np.round(ask + slip, 2), me)
        rate = col["rate"]
        steps = []
        f0 = sig & (ask <= cap + 1e-9) & (col["adep"] >= 5)                  # every signal fills at the signal-time ask (no budget)
        steps.append(("1. fill at the signal-time ask, exit at MID 5 s later, no fees, no latency, no budget", f0, col["mid5"] - ask))
        steps.append(("2. + exit at the BID (spread paid)", f0, col["bid5"] - ask))
        steps.append(("3. + both taker fees", f0, col["bid5"] - ask - fee(ask, rate) - fee(np.nan_to_num(col["bid5"]), rate)))
        # 4: realistic latency: the order fills only if the ARRIVAL ask is within the limit (draw 0), exit at the bid 5 s after arrival, fees
        arr = d.D[:, 0, S.DRAW.index("arr_ask")]
        xb = d.X[:, 0, 0, 3]
        f4 = sig & (arr == arr) & (arr <= cap + 1e-9) & (d.D[:, 0, S.DRAW.index("arr_adep")] >= 5) & (xb == xb)
        steps.append(("4. + latency: fill only if the ask at ARRIVAL (+500 ms venue proxy) is within the limit; exit at the bid seen 5 s after the fill",
                      f4, xb - arr - fee(arr, rate) - fee(np.nan_to_num(xb), rate)))
        f5 = f4 & ~np.isnan(d.net[:, 0, 0])
        steps.append(("5. + exit execution: FOK sell at bid − 2c with depth, 2 s retries, settlement in the last 10 s", f5, d.net[:, 0, 0]))
        for label, mask, val in steps:
            rows = []
            for pm in (M["all"], M["test"]):
                mm = mask & pm & ~np.isnan(val)
                rows.append((int(mm.sum()), float(np.nanmean(val[mm]) * 100) if mm.any() else np.nan, float(np.nansum(val[mm]) * 5)))
            P(f"| {label} | {rows[0][0]} | {rows[0][1]:+.1f} | {rows[0][2]:+.1f} | {rows[1][0]} | {rows[1][1]:+.1f} | {rows[1][2]:+.1f} |")
        # 6: + gates and budget (the full simulation)
        runs = run(d, cfg)
        a_all, a_te = agg(d, runs, M["all"], H["all"]), agg(d, runs, M["test"], H["test"])
        P(f"| 6. + quote/spot-age gates, the portfolio budget (4 positions, 10 USDC, 60/h, 1 entry per market), all draws | {a_all['fills']:.0f} | {a_all['c_share']:+.1f} | {a_all['net_usdc']:+.1f} | {a_te['fills']:.0f} | {a_te['c_share']:+.1f} | {a_te['net_usdc']:+.1f} |")
    P("\nThe live paper bots' own fills, audited against the recorded book (docs/spot_lag_paper_history_2026-09-25.md): 10 of tpb-spotlag-paper's 37 fills "
      "and 13 of ma74's 20 fills could not have filled; those 13 carried +8.78 of ma74's +12.63 USDC.\n")

    # ---- Monte Carlo on the recommended config
    P("## Monte Carlo (recommended configuration): 2,000 runs, each a random latency draw, a random fill-drop rate U(0, 30 %), "
      "extra slippage U(0, 1c) per leg, a delayed exit (t10 instead of the production exit) with probability 30 %, and a shuffled trade order\n")
    for pname in ("all", "test", "dev"):
        pnl, dds = [], []
        for _ in range(2000):
            q = int(rng.integers(d.draws))
            drop = rng.random(len(d.C)) < rng.uniform(0, 0.30)
            ex = "t10" if rng.random() < 0.30 else sel_exit
            o_idx, o_fill, _, o_net, _ = S.simulate(d, cfg2, q, exit_name=ex, slip_extra=float(rng.uniform(0, 0.01)), drop=drop)
            keep = M[pname][o_idx] & o_fill
            net = o_net[keep]
            rng.shuffle(net)
            cum = np.concatenate([[0.0], np.cumsum(net)])
            pnl.append(cum[-1])
            dds.append(float(np.max(np.maximum.accumulate(cum) - cum)))
        pnl, dds = np.array(pnl), np.array(dds)
        P(f"**{pname}** ({H[pname]:.1f} h): cumulative net P10 {np.percentile(pnl, 10):+.2f}, P25 {np.percentile(pnl, 25):+.2f}, "
          f"P50 {np.percentile(pnl, 50):+.2f}, P75 {np.percentile(pnl, 75):+.2f}, P90 {np.percentile(pnl, 90):+.2f}; P(net > 0) {(pnl > 0).mean():.0%}; "
          f"max drawdown P50 {np.percentile(dds, 50):.2f}, P90 {np.percentile(dds, 90):.2f}\n")
        summary.setdefault("monte_carlo", {})[pname] = {"p10": float(np.percentile(pnl, 10)), "p50": float(np.percentile(pnl, 50)),
                                                       "p90": float(np.percentile(pnl, 90)), "p_pos": float((pnl > 0).mean()),
                                                       "dd_p50": float(np.percentile(dds, 50)), "dd_p90": float(np.percentile(dds, 90))}

    # ---- stress
    P("## Stress tests (net USDC, mean over draws): recommended | production\n")
    P("| scenario | dev (train + validation) | test | all | all c/share (rec) |\n|---|---:|---:|---:|---:|")
    def stress_row(label, dd_, kw_rec, kw_prod=None):
        Mx = period_masks(dd_)
        Hx = {p: hours_of(dd_, m) for p, m in Mx.items()}
        rr = run(dd_, cfg2, exit_name=sel_exit, **kw_rec)
        pp = run(dd_, PRODC, **(kw_prod if kw_prod is not None else kw_rec))
        cells = []
        for p in ("dev", "test", "all"):
            a, b = agg(dd_, rr, Mx[p], Hx[p]), agg(dd_, pp, Mx[p], Hx[p])
            cells.append(f"{a['net_usdc']:+.2f} ({a['fills']:.0f}) \\| {b['net_usdc']:+.2f} ({b['fills']:.0f})")
        a = agg(dd_, rr, Mx["all"], Hx["all"])
        P(f"| {label} | " + " | ".join(cells) + f" | {a['c_share']:+.1f} |")
        summary.setdefault("stress", {})[label] = {"dev": cells[0], "test": cells[1], "all": cells[2]}
    stress_row("normal (measured latency, venue proxy 500 ms)", d, {})
    stress_row("slow network: every latency stage x1.5", S.Data("lat15"), {})
    stress_row("stale book: venue proxy 750 ms", S.Data("stale750"), {})
    stress_row("low liquidity: top-level depth halved", d, {"depth_mult": 0.5})
    stress_row("bad fills: +1c slippage on each leg", d, {"slip_extra": 0.01})
    stress_row("fees x1.5", d, {"fee_mult": 1.5})
    # adverse selection: drop the best 25 % of fills (by realised net, per draw) -- computed from the normal run
    best_drop = np.zeros(len(d.C), dtype=bool)
    nets = np.nanmean(np.where(np.isnan(d.net[:, :, 0]), np.nan, d.net[:, :, 0]), axis=1)
    thr = np.nanpercentile(nets[~np.isnan(nets)], 75)
    best_drop[nets > thr] = True
    stress_row("adverse selection: the best 25 % of fills never happen", d, {"drop": best_drop})
    stress_row("combined worst reasonable: latency x2, proxy 1000 ms, +1c slippage, depth halved", S.Data("worst"), {"slip_extra": 0.01, "depth_mult": 0.5})
    P("")

    # ---- sensitivity (one at a time from the recommended)
    P("## Sensitivity: one parameter at a time from the recommended configuration (dev = train + validation; net USDC, fills, max DD; then test net)\n")
    P("| parameter | value | dev net | fills | fill rate | max DD | test net |\n|---|---|---:|---:|---:|---:|---:|")
    sens = {}
    grids = [("k", 0, S.K_SET), ("edge", 1, S.EDGES), ("slip", 2, S.SLIPS), ("spread", 3, S.SPREADS), ("max_ask", 4, S.MAX_ASKS),
             ("window", 5, S.WINDOWS), ("qage", 6, S.QAGES), ("sage", 7, S.SAGES)]
    for pname, pos, grid in grids:
        vals = []
        for v in grid:
            c = list(cfg2)
            c[pos] = v
            runs = run(d, tuple(c), exit_name=sel_exit)
            a, t_ = agg(d, runs, M["dev"], H["dev"]), agg(d, runs, M["test"], H["test"])
            mark = " **(rec)**" if v == cfg2[pos] else ""
            P(f"| {pname} | {v}{mark} | {a['net_usdc']:+.2f} | {a['fills']:.0f} | {a['fill_rate']:.0%} | {a['max_dd']:.2f} | {t_['net_usdc']:+.2f} |")
            vals.append((a["net_usdc"], a["fills"], a["max_dd"], t_["net_usdc"]))
        v = np.array(vals)
        sens[pname] = {"dev_range": float(v[:, 0].max() - v[:, 0].min()), "fills_range": float(v[:, 1].max() - v[:, 1].min()),
                       "dd_range": float(v[:, 2].max() - v[:, 2].min()), "sign_changes_dev": int((np.sign(v[:, 0]) != np.sign(v[0, 0])).sum()),
                       "test_min": float(v[:, 3].min()), "test_max": float(v[:, 3].max())}
    for ex in S.EXITS5:
        a = exit_res[ex]
        sens.setdefault("exit", {})
    P("")
    P("| parameter | dev net range (USDC) | fills range | max DD range | test net min..max | class |\n|---|---:|---:|---:|---|---|")
    for pname, s_ in sens.items():
        if not s_ or "dev_range" not in s_:
            continue
        cls = "high impact" if s_["dev_range"] > 10 else "low impact" if s_["dev_range"] < 4 else "medium"
        if s_["sign_changes_dev"]:
            cls += ", unstable (sign changes)"
        P(f"| {pname} | {s_['dev_range']:.1f} | {s_['fills_range']:.0f} | {s_['dd_range']:.2f} | {s_['test_min']:+.1f}..{s_['test_max']:+.1f} | {cls} |")
    ex_dev = {e: dev(exit_res[e]) for e in S.EXITS5}
    P(f"| exit rule | {max(ex_dev.values()) - min(ex_dev.values()):.1f} | – | – | {min(a['test']['net_usdc'] for a in exit_res.values()):+.1f}..{max(a['test']['net_usdc'] for a in exit_res.values()):+.1f} | "
      f"{'high impact' if max(ex_dev.values()) - min(ex_dev.values()) > 10 else 'low impact'} |")
    P("")
    summary["sensitivity"] = sens

    # ---- attribution: where the recommended config's profit comes from (draw 0, all periods and test)
    P("## Where the profit comes from (recommended configuration, draw 0): c/share (fills) by bucket, ALL periods | TEST\n")
    o_idx, o_fill, _, o_net, o_nps = S.simulate(d, cfg2, 0, exit_name=sel_exit)
    f = o_idx[o_fill]
    nps = o_nps[o_fill] * 100
    slot = S.SLOTS.index(sel_exit)
    feats = {
        "asset": np.array([S.ASSETS[int(a)] for a in col["asset"][f]]),
        "direction": np.where(col["dir"][f] > 0, "UP", "DOWN"),
        "time left (s)": pd.cut(col["left"][f], [0, 90, 150, 210, 300]).astype(str),
        "lag = fair − ask (c)": pd.cut((col["ev"][f] - col["ask"][f]) * 100, [0, 4, 6, 8, 12, 100]).astype(str),
        "spot move z1 (σ)": pd.cut(col["z1"][f], [0, 3.5, 4.5, 6, 100]).astype(str),
        "spread (c)": pd.cut((col["ask"][f] - col["bid"][f]) * 100 + 1e-9, [0, 1.01, 2.01, 3.01, 5.01]).astype(str),
        "volatility σ (bps/s)": pd.cut(col["sigma_bps"][f], [0, 0.6, 1.0, 1.5, 100]).astype(str),
        "entry price": pd.cut(d.D[f, 0, S.DRAW.index("arr_ask")], [0, 0.2, 0.4, 0.6, 0.8, 1.0]).astype(str),
        "hold (s)": pd.cut(d.X[f, 0, slot, 1], [0, 5.5, 7, 10, 60, 400]).astype(str),
        "exit kind": np.where(d.X[f, 0, slot, 2] == 2.0, "settlement", "sold"),
        "hour (UTC)": pd.cut(pd.to_datetime(col["t"][f], unit="ms").hour, [-1, 5, 11, 17, 23], labels=["00-06", "06-12", "12-18", "18-24"]).astype(str),
    }
    te = M["test"][f]
    for name, v in feats.items():
        P(f"**{name}**: " + "; ".join(
            f"{b}: {nps[(v == b)].mean():+.1f} ({int((v == b).sum())}) | {nps[(v == b) & te].mean() if ((v == b) & te).any() else float('nan'):+.1f} ({int(((v == b) & te).sum())})"
            for b in sorted(set(v), key=lambda x: str(x))) + "\n")
    # concentration
    order = np.argsort(-o_net[o_fill])
    tot = o_net[o_fill].sum()
    P(f"Concentration: the top 5 fills are {o_net[o_fill][order[:5]].sum():+.2f} of {tot:+.2f} USDC; net without them {tot - o_net[o_fill][order[:5]].sum():+.2f}. "
      f"Top 5 fills: " + "; ".join(f"{dt.datetime.utcfromtimestamp(col['t'][f][i] / 1000):%m-%d %H:%M:%S} {S.ASSETS[int(col['asset'][f][i])]} "
                                    f"{'UP' if col['dir'][f][i] > 0 else 'DOWN'} @{d.D[f[i], 0, S.DRAW.index('arr_ask')]:.2f} {o_net[o_fill][i]:+.2f}" for i in order[:5]) + "\n")

    # ---- MFE / MAE for the recommended fills (draw 0) from the archive
    P("## Price path after the recommended configuration's fills (draw 0): best and worst bid vs entry within 30 s\n")
    W = {}
    for a in S.ASSETS:
        dd_, meta = load_asset(S.CACHE, a)
        for w in windows(dd_, meta):
            W[(a, int(w.epoch))] = w
    mfe, mae, tmfe, tmae, rev = [], [], [], [], []
    for i in f:
        w = W.get((S.ASSETS[int(col["asset"][i])], int(col["epoch"][i])))
        if w is None:
            continue
        sv = w.side["UP" if col["dir"][i] > 0 else "DOWN"]
        t_known = d.D[i, 0, S.DRAW.index("t_known")]
        entry = d.D[i, 0, S.DRAW.index("arr_ask")]
        j0, j1 = w.first_at(t_known), w.last_at(t_known + 30000.0)
        if j1 <= j0:
            continue
        b = sv["bid"][j0:j1 + 1]
        ok = ~np.isnan(b)
        if not ok.any():
            continue
        bb, tt = b[ok], w.t[j0:j1 + 1][ok]
        k1, k2 = int(np.argmax(bb)), int(np.argmin(bb))
        mfe.append(bb[k1] - entry); mae.append(bb[k2] - entry)
        tmfe.append((tt[k1] - t_known) / 1000); tmae.append((tt[k2] - t_known) / 1000)
        rev.append(bb[k1:].min() < entry if k1 < len(bb) - 1 else False)
    mfe, mae = np.array(mfe) * 100, np.array(mae) * 100
    P(f"fills {len(mfe)}: MFE mean {mfe.mean():+.1f}c (median {np.median(mfe):+.1f}), time to MFE median {np.median(tmfe):.1f} s; "
      f"MAE mean {mae.mean():+.1f}c (median {np.median(mae):+.1f}), time to MAE median {np.median(tmae):.1f} s; "
      f"fell back below the entry after the best: {np.mean(rev):.0%}; reached +5c {np.mean(mfe >= 5):.0%}, +10c {np.mean(mfe >= 10):.0%}; "
      f"went 5c under {np.mean(mae <= -5):.0%}, 10c under {np.mean(mae <= -10):.0%}\n")

    # ---- success criteria
    rec_all = summary["finalists"]["**recommended** (selected + gates + exit)"]
    crit = {
        "positive validation expectancy": rec_all["validation"]["net_usdc"] > 0,
        "positive untouched-test expectancy": rec_all["test"]["net_usdc"] > 0,
        "positive walk-forward expectancy": wf_sum > 0,
        "reasonable drawdown (all-period max DD <= 5 USDC and <= 50 % of net)": rec_all["all"]["max_dd"] <= 5.0 and rec_all["all"]["max_dd"] <= 0.5 * max(rec_all["all"]["net_usdc"], 1e-9),
        "robust across execution stress (test net > 0 under every single stress except adverse selection, and >= 0 under the combined worst case)": all(
            float(summary["stress"][k]["test"].split(" ")[0]) > 0 for k in summary["stress"]
            if k.startswith(("slow", "stale", "low", "bad", "fees"))) and float(
            summary["stress"]["combined worst reasonable: latency x2, proxy 1000 ms, +1c slippage, depth halved"]["test"].split(" ")[0]) >= 0,
        "test edge is more than noise (Monte Carlo P(test net > 0) >= 90 %)": summary["monte_carlo"]["test"]["p_pos"] >= 0.9,
        "not dependent on unrealistic fills (every fill is at the arrival book; Monte Carlo P(all-period net > 0) >= 90 %)": summary["monte_carlo"]["all"]["p_pos"] >= 0.9,
        "stable across nearby parameter values (no sign change in the dev net across any parameter's grid)": all(
            s_.get("sign_changes_dev", 0) == 0 for s_ in sens.values() if s_),
    }
    P("## Success criteria (recommended configuration)\n")
    P("| criterion | met |\n|---|---|")
    for k, v in crit.items():
        P(f"| {k} | {'YES' if v else '**NO**'} |")
    verdict = "ROBUST EDGE" if all(crit.values()) else "NO ROBUST EDGE FOUND"
    P(f"\n**Verdict: {verdict}** ({sum(crit.values())} of {len(crit)} criteria met)\n")
    summary["criteria"] = {k: bool(v) for k, v in crit.items()}
    summary["verdict"] = verdict
    summary["locked"] = lock
    (RES / "spot_lag_realistic_report.md").write_text("\n".join(L) + "\n")
    (RES / "spot_lag_realistic_summary.json").write_text(json.dumps(summary, indent=1, default=float))
    print("\n".join(L))


if __name__ == "__main__":
    main()
