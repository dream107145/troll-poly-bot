"""BTC_CONFIRMATION_MOMENTUM replay (2026-10-06): the strategy of strategy/confirmation_momentum.py run on the
200 ms BTC window archive (data/replay/paired_exit/btc.npz: composite and per-exchange spot, both tokens' touch
and depth, the venue outcome) with the CALIBRATED execution model (scripts/paired_exit/pe_lib.Exec: a taker
decided at t lands at t + 250 ms and fills only if the ask stays inside the limit through landing + 1 s, charged
the worse of the two asks; a resting bid fills only when the far side trades strictly through it).

    .venv/bin/python scripts/research_confirmation_momentum.py            # full study -> docs/ + results/
    .venv/bin/python scripts/research_confirmation_momentum.py --quick    # the baseline and ablations only

Method (the brief, sections 18-20):
  * every window is sampled on a 1 s grid; the features of the live engine are recomputed from the archive
    with the SAME pure functions (momentum_from_returns, momentum_direction, exchange_agreement,
    retrace_fraction, price_zone, estimate_win_probability, net_edge);
  * sigma = the trailing 60 s standard deviation of the composite's 1 s log returns inside the window (causal);
  * splits by window index (pe_lib.SPLITS): TRAIN 50 % -> parameters, VALIDATION 20 % -> the edge threshold and
    the model's recalibration, TEST 15 % and FORWARD 15 % -> untouched evaluation;
  * the probability model: the rule (mid + bonus) OR a logistic regression over MODEL_FEATURES fitted on TRAIN
    candidates and Platt-recalibrated on VALIDATION, written to data/replay/cm_model.json;
  * ablations A-G of section 19 on every split; metrics of section 18 with the splits by zone, momentum, time
    remaining, hour, weekday and volatility regime.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import sys
import time
from dataclasses import dataclass, asdict, replace

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/src")
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L  # noqa: E402
from troll_poly_bot.strategy import confirmation_momentum as cm  # noqa: E402

ROOT = "/root/troll-poly-bot"
EXCH = ("binance", "coinbase", "bybit")
HZ = (1, 3, 5, 10, 15)
FEE = L.fee


@dataclass
class P:
    """The replay's parameters = CMConfig's trading parameters (same names, same defaults)."""
    min_elapsed_s: float = 45.0
    max_elapsed_s: float = 210.0
    min_remaining_s: float = 30.0
    z_1s_min: float = 2.0
    z_5s_min: float = 2.0
    z_15s_min: float = 1.5
    min_exchange_agreement: int = 2
    contradiction_z: float = 1.0
    retrace_window_s: float = 15.0
    max_retrace_fraction: float = 0.50
    token_confirm_cents: float = 5.0
    token_confirm_s: float = 15.0
    zone_min: float = 0.50
    zone1_max: float = 0.65
    zone2_max: float = 0.85
    zone3_max: float = 0.93
    zone2_confirm_mult: float = 1.5
    zone3_confirm_mult: float = 2.0
    zone2_z_mult: float = 1.25
    zone3_z_mult: float = 1.5
    p_bonus_base: float = 0.03
    p_bonus_per_z: float = 0.01
    p_bonus_cap: float = 0.06
    min_net_edge: float = 0.03
    slippage_allowance: float = 0.01
    adverse_selection: float = 0.02
    latency_penalty: float = 0.005
    max_slippage: float = 0.02
    max_chase_cents: float = 3.0
    max_spread: float = 0.03
    min_depth_shares: float = 10.0
    base_order_usdc: float = 3.0
    max_order_usdc: float = 5.0
    max_market_exposure_usdc: float = 10.0
    max_entries_per_market: int = 3
    min_new_signal_cents: float = 4.0
    # ablation switches
    use_agreement: bool = True
    use_token_confirm: bool = True
    use_continuation: bool = True
    use_model: bool = True            # the fitted model when available, else the rule
    immediate_open: bool = False      # ablation A: buy the leader 3 s after the open and hold
    # execution
    lat_ms: float = 250.0
    lat_mult: float = 1.0

    def confirm_cents(self, zone):
        return self.token_confirm_cents * (self.zone2_confirm_mult if zone == 2 else self.zone3_confirm_mult if zone == 3 else 1.0)

    def z_mult(self, zone):
        return self.zone2_z_mult if zone == 2 else self.zone3_z_mult if zone == 3 else 1.0


# ------------------------------------------------------------------------- features per window


def grid_features(w):
    """Everything the decision needs, on a 1 s grid from the open to the close: composite returns and z-scores per
    horizon, per-exchange 5 s returns, the 15 s retrace fraction in both directions, both tokens' touch, mids and
    returns. All causal: a value at grid time t uses samples at or before t."""
    n_s = int(round((w.close - w.open) / 1000.0))
    tg = w.open + np.arange(0, n_s + 1) * 1000.0
    idx = np.searchsorted(w.T, tg, side="right") - 1
    ok = idx >= 0
    idx = np.clip(idx, 0, w.n - 1)

    def at(arr):
        v = arr[idx].astype(float)
        v[~ok] = np.nan
        # a sample more than 3 s old is stale
        v[(tg - w.T[idx]) > 3000.0] = np.nan
        return v

    spot = at(w.spot)
    ls = np.log(spot)
    rets = {h: np.full_like(ls, np.nan) for h in HZ}
    for h in HZ:
        rets[h][h:] = ls[h:] - ls[:-h]
    # trailing 60 s sigma of 1 s returns (at least 20 samples)
    r1 = rets[1]
    sigma = np.full_like(ls, np.nan)
    for i in range(len(ls)):
        seg = r1[max(0, i - 59):i + 1]
        seg = seg[~np.isnan(seg)]
        if seg.size >= 20:
            sigma[i] = max(float(seg.std()), 1e-6)
    z = {h: rets[h] / (sigma * math.sqrt(h)) for h in HZ}
    ex5 = {}
    for e in EXCH:
        x = at(w.X[e]) if e in w.X else np.full_like(ls, np.nan)
        lx = np.log(x)
        r = np.full_like(lx, np.nan)
        r[5:] = lx[5:] - lx[:-5]
        ex5[e] = r
    # retrace over 15 s, both directions
    retr_up = np.full_like(ls, np.nan)
    retr_dn = np.full_like(ls, np.nan)
    for i in range(15, len(ls)):
        path = spot[i - 15:i + 1]
        if np.isnan(path).any():
            continue
        retr_up[i] = cm.retrace_fraction(list(path), 1)
        retr_dn[i] = cm.retrace_fraction(list(path), -1)
    ua, ub, da, db = at(w.ua), at(w.ub), at(w.da), at(w.db)
    uad, dad = at(w.uad), at(w.dad)
    up_mid = (ua + ub) / 2.0
    tok = {}
    for h in (5, 10, 15):
        r = np.full_like(up_mid, np.nan)
        r[h:] = up_mid[h:] - up_mid[:-h]
        tok[h] = r
    return dict(tg=tg, elapsed=(tg - w.open) / 1000.0, left=(w.close - tg) / 1000.0, spot=spot, sigma=sigma, rets=rets, z=z,
                ex5=ex5, retr_up=retr_up, retr_dn=retr_dn, ua=ua, ub=ub, da=da, db=db, uad=uad, dad=dad, up_mid=up_mid, tok=tok)


# ------------------------------------------------------------------------- the strategy on one window


def run_window(w, g, p: P, model, ex: L.Exec, collect_candidates=None):
    """Returns (trades, rejects Counter, candidates). A trade: dict(side, t, px, size, fee, won, pnl, p, zone, z5, left, ...)."""
    rej = collections.Counter()
    trades = []
    if p.immediate_open:
        # ablation A: the leader 3 s after the open, one clip, hold
        i = 3
        ua, da = g["ua"][i], g["da"][i]
        if np.isnan(ua) or np.isnan(da):
            rej["NO_SPOT_DATA"] += 1
            return trades, rej, []
        side = "UP" if ua >= da else "DOWN"
        ask = ua if side == "UP" else da
        if not (p.zone_min <= ask < p.zone3_max):
            rej["TOKEN_TOO_EXPENSIVE" if ask >= p.zone3_max else "NO_TOKEN_CONFIRMATION"] += 1
            return trades, rej, []
        limit = round(ask + p.max_slippage, 2)
        size = max(5.0, math.floor(p.base_order_usdc / limit * 100) / 100)
        px, known = ex.taker_buy(w, side, g["tg"][i], limit)
        if px is None:
            rej["ORDER_NOT_EXECUTABLE"] += 1
            return trades, rej, []
        won = w.outcome == side
        fee = size * FEE(px)
        trades.append(dict(side=side, t=float(g["tg"][i]), px=px, size=size, fee=fee, won=won, pnl=(size if won else 0.0) - size * px - fee,
                           p=ask, zone=cm.price_zone(ask, p.zone_min, p.zone1_max, p.zone2_max, p.zone3_max), z5=0.0, left=float(g["left"][i]),
                           elapsed=3.0, sigma=float(np.nanmedian(g["sigma"])) if not np.isnan(np.nanmedian(g["sigma"])) else 0.0))
        return trades, rej, []
    clips = []
    spent = 0.0
    impulse = None                      # dict(dir, t0, ask0, last_qualify)
    cands = []
    cfg_for_rule = _RuleCfg(p)
    for i in range(len(g["tg"])):
        el, left = g["elapsed"][i], g["left"][i]
        if el < p.min_elapsed_s:
            rej["TOO_EARLY"] += 1
            continue
        if el > p.max_elapsed_s or left < p.min_remaining_s:
            rej["TOO_LATE"] += 1
            continue
        sig = g["sigma"][i]
        if np.isnan(g["spot"][i]) or np.isnan(sig):
            rej["NO_SPOT_DATA"] += 1
            continue
        zz = {float(h): float(g["z"][h][i]) for h in HZ}
        d, why = cm.momentum_direction(zz, p.z_1s_min, p.z_5s_min, p.z_15s_min)
        if d == 0:
            rej[why] += 1
            if impulse is not None and g["tg"][i] - impulse["last_qualify"] > 10_000.0:
                impulse = None
            continue
        if impulse is None or impulse["dir"] != d or g["tg"][i] - impulse["last_qualify"] > 10_000.0:
            ask_now = g["ua"][i] if d > 0 else g["da"][i]
            impulse = {"dir": d, "t0": float(g["tg"][i]), "ask0": float(ask_now) if not np.isnan(ask_now) else math.nan,
                       "last_qualify": float(g["tg"][i])}
        impulse["last_qualify"] = float(g["tg"][i])
        moves = {e: (None if np.isnan(g["ex5"][e][i]) else float(g["ex5"][e][i])) for e in EXCH}
        agree, worst, contra = cm.exchange_agreement(moves, d, sig * math.sqrt(5.0), p.contradiction_z)
        fresh = sum(1 for v in moves.values() if v is not None)
        if p.use_agreement:
            if fresh < p.min_exchange_agreement:
                rej["NO_SPOT_DATA"] += 1
                continue
            if agree < p.min_exchange_agreement or contra is not None:
                rej["EXCHANGE_DISAGREEMENT"] += 1
                continue
        retr = g["retr_up"][i] if d > 0 else g["retr_dn"][i]
        if p.use_continuation and (np.isnan(retr) or retr > p.max_retrace_fraction):
            rej["MOMENTUM_REVERSING"] += 1
            continue
        side = "UP" if d > 0 else "DOWN"
        ask = g["ua"][i] if d > 0 else g["da"][i]
        bid = g["ub"][i] if d > 0 else g["db"][i]
        depth = g["uad"][i] if d > 0 else g["dad"][i]
        if np.isnan(ask) or np.isnan(bid):
            rej["OTHER"] += 1
            continue
        zone = cm.price_zone(ask, p.zone_min, p.zone1_max, p.zone2_max, p.zone3_max)
        if zone == 4:
            rej["TOKEN_TOO_EXPENSIVE"] += 1
            continue
        if zone == 0:
            rej["NO_TOKEN_CONFIRMATION"] += 1
            continue
        tok_h = int(p.token_confirm_s) if int(p.token_confirm_s) in (5, 10, 15) else 15
        conf = g["tok"][tok_h][i] * d
        if p.use_token_confirm and (np.isnan(conf) or conf < p.confirm_cents(zone) / 100.0 - 1e-9):
            rej["NO_TOKEN_CONFIRMATION"] += 1
            continue
        zm = p.z_mult(zone)
        if zm > 1.0 and (zz[5.0] * d < p.z_5s_min * zm or zz[15.0] * d < p.z_15s_min * zm):
            rej["WEAK_MOMENTUM"] += 1
            continue
        spread = ask - bid
        if spread > p.max_spread + 1e-9:
            rej["SPREAD_TOO_WIDE"] += 1
            continue
        if np.isnan(depth) or depth < p.min_depth_shares:
            rej["INSUFFICIENT_DEPTH"] += 1
            continue
        mid = (ask + bid) / 2.0
        f = {"mid": mid, "logit_mid": cm._logit(mid), "z1": zz[1.0] * d, "z5": zz[5.0] * d, "z15": zz[15.0] * d, "agree": agree,
             "tok_r5": 0.0 if np.isnan(g["tok"][5][i]) else float(g["tok"][5][i] * d),
             "tok_r15": 0.0 if np.isnan(g["tok"][15][i]) else float(g["tok"][15][i] * d),
             "spread_c": spread * 100.0, "log_depth": math.log1p(float(depth)), "sqrt_left": math.sqrt(max(left, 0.0)),
             "elapsed_frac": el / 300.0, "retrace": 0.0 if np.isnan(retr) else float(retr), "log_sigma": math.log(max(sig, 1e-9))}
        won_side = w.outcome == side
        if collect_candidates is not None:
            collect_candidates.append((f, won_side, w.split, mid))
        prob = cm.estimate_win_probability(f, cfg_for_rule, model if p.use_model else None)
        limit = min(0.99, round(ask + p.max_slippage, 2))
        exec_price = ask
        fee_ps = FEE(exec_price)
        e = cm.net_edge(prob, exec_price, fee_ps, cfg_for_rule)
        cands.append(dict(t=float(g["tg"][i]), side=side, p=prob, mid=mid, ask=ask, net=e["net_edge"], zone=zone, z5=zz[5.0] * d, won=won_side))
        if e["net_edge"] < p.min_net_edge:
            rej["INSUFFICIENT_EDGE"] += 1
            continue
        if not math.isnan(impulse["ask0"]) and (ask - impulse["ask0"]) * 100.0 > p.max_chase_cents + 1e-9:
            rej["CHASED_PRICE"] += 1
            continue
        if len(clips) >= p.max_entries_per_market:
            rej["MAX_ENTRIES"] += 1
            continue
        if clips:
            last = clips[-1]
            if last["side"] != side or impulse["t0"] <= last["impulse_t0"] or mid < last["px"] + p.min_new_signal_cents / 100.0 - 1e-9:
                rej["MAX_ENTRIES"] += 1
                continue
        size = max(5.0, math.floor(p.base_order_usdc / limit * 100.0) / 100.0)
        if size * limit > p.max_order_usdc + 1e-9:
            size = math.floor(p.max_order_usdc / limit * 100.0) / 100.0
        if spent + size * limit > p.max_market_exposure_usdc + 1e-9:
            rej["MAX_EXPOSURE"] += 1
            continue
        px, known = ex.taker_buy(w, side, float(g["tg"][i]), limit)
        if px is None:
            rej["ORDER_NOT_EXECUTABLE"] += 1
            # the impulse is spent for this attempt; the next clip needs a new one
            impulse["t0"] = impulse["t0"]
            continue
        fee = size * FEE(px)
        spent += size * px + fee
        tr = dict(side=side, t=float(g["tg"][i]), px=px, size=size, fee=fee, won=won_side, pnl=(size if won_side else 0.0) - size * px - fee,
                  p=prob, zone=zone, z5=zz[5.0] * d, left=float(left), elapsed=float(el), sigma=float(sig), impulse_t0=impulse["t0"],
                  agree=agree, retrace=float(retr) if not np.isnan(retr) else math.nan, mid=mid)
        clips.append(tr)
        trades.append(tr)
    return trades, rej, cands


class _RuleCfg:
    """What estimate_win_probability / net_edge read from a config, built from P."""

    def __init__(self, p: P) -> None:
        self.p_bonus_base, self.p_bonus_per_z, self.p_bonus_cap, self.z_5s_min = p.p_bonus_base, p.p_bonus_per_z, p.p_bonus_cap, p.z_5s_min
        self.slippage_allowance, self.adverse_selection, self.latency_penalty = p.slippage_allowance, p.adverse_selection, p.latency_penalty


# ------------------------------------------------------------------------- the model


def fit_logistic(X, y, l2=5.0, iters=400, lr=0.1):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    Z1 = np.hstack([np.ones((len(Z), 1)), Z])
    beta = np.zeros(Z1.shape[1])
    for _ in range(iters):
        pz = 1.0 / (1.0 + np.exp(-Z1 @ beta))
        grad = Z1.T @ (pz - y) / len(y) + l2 * np.r_[0.0, beta[1:]] / len(y)
        beta -= lr * grad
    return beta, mu, sd


def platt(raw_logit, y, iters=300, lr=0.05):
    a, b = 0.0, 1.0
    for _ in range(iters):
        pz = 1.0 / (1.0 + np.exp(-(a + b * raw_logit)))
        ga = float(np.mean(pz - y)); gb = float(np.mean((pz - y) * raw_logit))
        a -= lr * ga; b -= lr * gb
    return a, b


def fit_model(cands):
    """cands: list of (features, won, split, mid). Fit on TRAIN, recalibrate on VALIDATION. Returns the model doc."""
    tr = [c for c in cands if c[2] == "train"]
    va = [c for c in cands if c[2] == "validation"]
    if len(tr) < 200 or len(va) < 50:
        return None
    X = np.array([[c[0][k] for k in cm.MODEL_FEATURES] for c in tr]); y = np.array([1.0 if c[1] else 0.0 for c in tr])
    beta, mu, sd = fit_logistic(X, y)
    Xv = np.array([[c[0][k] for k in cm.MODEL_FEATURES] for c in va]); yv = np.array([1.0 if c[1] else 0.0 for c in va])
    raw = np.hstack([np.ones((len(Xv), 1)), (Xv - mu) / sd]) @ beta
    a, b = platt(raw, yv)
    return {"features": list(cm.MODEL_FEATURES), "beta": [float(x) for x in beta], "mu": [float(x) for x in mu], "sd": [float(x) for x in sd],
            "platt": {"a": float(a), "b": float(b)}, "fitted_on": {"train": len(tr), "validation": len(va)},
            "train_win_rate": float(y.mean()), "validation_win_rate": float(yv.mean())}


# ------------------------------------------------------------------------- metrics


def metrics(trades, n_markets, rej, cand_count):
    if not trades:
        return {"markets": n_markets, "candidates": cand_count, "trades": 0, "fills": 0, "pnl": 0.0}
    pnl = np.array([t["pnl"] for t in trades]); cum = np.cumsum(pnl)
    dd = float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) else 0.0
    by_m = collections.defaultdict(float)
    for t in trades:
        by_m[t.get("impulse_t0", t["t"]) // 1 if False else round(t["t"] // 300_000)] += t["pnl"]
    gains = pnl[pnl > 0].sum(); losses = -pnl[pnl < 0].sum()
    return {"markets": n_markets, "candidates": cand_count, "trades": len(trades), "fills": len(trades), "fill_rate": None,
            "win_rate": round(float(np.mean([t["won"] for t in trades])), 4), "pnl": round(float(pnl.sum()), 2),
            "pnl_per_trade": round(float(pnl.mean()), 4), "pnl_per_market": round(float(pnl.sum()) / n_markets, 4),
            "avg_entry": round(float(np.mean([t["px"] for t in trades])), 4), "avg_est_prob": round(float(np.mean([t["p"] for t in trades])), 4),
            "realised_prob": round(float(np.mean([t["won"] for t in trades])), 4), "max_drawdown": round(dd, 2),
            "worst_market": round(float(min(by_m.values())), 2), "worst_trade": round(float(pnl.min()), 2),
            "profit_factor": round(float(gains / losses), 3) if losses > 0 else None,
            "exposure_utilisation": round(float(max(by_m_cost(trades).values())) / 10.0, 3) if trades else 0.0}


def by_m_cost(trades):
    c = collections.defaultdict(float)
    for t in trades:
        c[round(t["t"] // 300_000)] += t["size"] * t["px"]
    return c


def split_table(trades, key, label):
    g = collections.defaultdict(list)
    for t in trades:
        g[key(t)].append(t["pnl"])
    rows = []
    for k in sorted(g, key=lambda x: (str(type(x)), x)):
        v = np.array(g[k]); rows.append((str(k), len(v), round(float(v.sum()), 2), round(float(v.mean()), 4), round(float(np.mean([x > 0 for x in v])), 3)))
    return label, rows


# ------------------------------------------------------------------------- study


def run_set(wins, grids, p, model, ex, splits=("train", "validation", "test", "forward"), collect=None):
    out = {}
    all_trades = []
    for s in splits:
        ws = [w for w in wins if w.split == s]
        trades, rej, nc = [], collections.Counter(), 0
        for w in ws:
            tr, rj, cands = run_window(w, grids[w.k], p, model, ex, collect if s in ("train", "validation") and collect is not None else None)
            trades += tr; rej.update(rj); nc += len(cands)
        m = metrics(trades, len(ws), rej, nc); m["rejects"] = dict(rej)
        out[s] = m
        all_trades += [dict(t, split=s) for t in trades]
    return out, all_trades


def fmt_row(name, m):
    if m.get("trades", 0) == 0:
        return f"| {name} | {m['markets']} | {m.get('candidates', 0)} | 0 | - | - | 0.00 | - | - | - | - |"
    return (f"| {name} | {m['markets']} | {m['candidates']} | {m['trades']} | {m['win_rate']:.0%} | {m['avg_entry']:.3f} | "
            f"{m['pnl']:+.2f} | {m['pnl_per_trade']:+.3f} | {m['pnl_per_market']:+.3f} | {m['max_drawdown']:.1f} | {m['worst_trade']:+.2f} |")


HEAD = "| variant / split | markets | candidates | trades | win | avg entry | P&L | per trade | per market | max DD | worst trade |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    wins = L.load_asset("BTC")
    print(f"windows {len(wins)}; computing the 1 s grid features ...", flush=True)
    grids = {w.k: grid_features(w) for w in wins}
    print(f"features ready in {time.time() - t0:.0f} s", flush=True)
    ex = L.Exec()
    report = []
    results = {"generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "windows": len(wins),
               "splits": {s: sum(1 for w in wins if w.split == s) for s, _, _ in L.SPLITS},
               "period": {"from": time.strftime("%m-%d %H:%M", time.gmtime(wins[0].open / 1000)), "to": time.strftime("%m-%d %H:%M", time.gmtime(wins[-1].open / 1000))}}

    # 1. the brief's defaults with the RULE estimator (no model): how many candidates, how many trades
    base = P()
    cands_all = []
    rule_res, rule_trades = run_set(wins, grids, replace(base, use_model=False), None, ex, collect=cands_all)
    results["rule_defaults"] = rule_res
    # 2. fit the model on TRAIN candidates, recalibrate on VALIDATION
    model_doc = fit_model(cands_all)
    model = cm.CMModel(model_doc) if model_doc else None
    results["model"] = model_doc
    # 3. the edge threshold on VALIDATION (the only parameter chosen there)
    best_thr, best_val = base.min_net_edge, -1e9
    thr_rows = []
    for thr in (0.0, 0.01, 0.02, 0.03, 0.05):
        r, _ = run_set(wins, grids, replace(base, min_net_edge=thr), model, ex, splits=("validation",))
        v = r["validation"]
        thr_rows.append((thr, v.get("trades", 0), v.get("pnl", 0.0), v.get("pnl_per_trade")))
        score = v.get("pnl_per_market", -1e9) if v.get("trades", 0) >= 20 else -1e9
        if score > best_val:
            best_val, best_thr = score, thr
    results["threshold_selection"] = {"rows": thr_rows, "chosen": best_thr}
    # 4. a small TRAIN grid for the momentum / confirmation parameters (section 20: few parameters, simple rules)
    grid_rows = []
    best_p, best_score = replace(base, min_net_edge=best_thr), -1e9
    if not args.quick:
        for z5 in (1.5, 2.0, 2.5):
            for tc in (3.0, 5.0, 7.0):
                for me in (150.0, 210.0):
                    cand = replace(base, min_net_edge=best_thr, z_5s_min=z5, z_1s_min=min(2.0, z5), token_confirm_cents=tc, max_elapsed_s=me)
                    r, _ = run_set(wins, grids, cand, model, ex, splits=("train",))
                    tr = r["train"]
                    score = tr.get("pnl_per_market", -1e9) if tr.get("trades", 0) >= 40 else -1e9
                    grid_rows.append((z5, tc, me, tr.get("trades", 0), tr.get("pnl", 0.0), tr.get("pnl_per_market")))
                    if score > best_score:
                        best_score, best_p = score, cand
    results["train_grid"] = {"rows": grid_rows, "chosen": asdict(best_p)}
    # 5. the final strategy and the ablations on every split
    variants = [
        ("A immediate post-open leader, hold", replace(best_p, immediate_open=True)),
        ("B confirmation momentum (selected)", best_p),
        ("C without exchange agreement", replace(best_p, use_agreement=False)),
        ("D without token confirmation", replace(best_p, use_token_confirm=False)),
        ("E without the continuation filter", replace(best_p, use_continuation=False)),
        ("F1 zones: nothing above 0.85", replace(best_p, zone3_max=0.85)),
        ("F2 zones: allow up to 0.99", replace(best_p, zone3_max=0.99)),
        ("G realistic live latency (x2 = 500 ms)", replace(best_p, lat_mult=2.0)),
        ("H rule estimator instead of the model", replace(best_p, use_model=False)),
    ]
    abl = {}
    final_trades = []
    for name, pv in variants:
        exv = L.Exec(lat_mult=pv.lat_mult)
        r, trs = run_set(wins, grids, pv, model, exv)
        abl[name] = r
        if name.startswith("B"):
            final_trades = trs
    results["ablations"] = abl
    # 6. splits of the final strategy's trades (TEST + FORWARD = out of sample)
    oos = [t for t in final_trades if t["split"] in ("test", "forward")]
    tables = []
    if oos:
        tables.append(split_table(oos, lambda t: f"zone {t['zone']}", "token price zone"))
        tables.append(split_table(oos, lambda t: "z5 < 2.5" if t["z5"] < 2.5 else "2.5-3.5" if t["z5"] < 3.5 else ">= 3.5", "momentum magnitude"))
        tables.append(split_table(oos, lambda t: "left >= 150 s" if t["left"] >= 150 else "90-150 s" if t["left"] >= 90 else "< 90 s", "time remaining"))
        tables.append(split_table(oos, lambda t: f"{time.gmtime(t['t'] / 1000).tm_hour:02d} UTC", "hour of day"))
        tables.append(split_table(oos, lambda t: time.strftime("%a", time.gmtime(t["t"] / 1000)), "weekday"))
        sig = sorted(t["sigma"] for t in oos); lo, hi = sig[len(sig) // 3], sig[2 * len(sig) // 3]
        tables.append(split_table(oos, lambda t: "low vol" if t["sigma"] < lo else "mid vol" if t["sigma"] < hi else "high vol", "volatility regime"))
    results["oos_splits"] = {label: rows for label, rows in tables}
    # ------------------------------------------------------------------ report
    R = report.append
    R("# BTC_CONFIRMATION_MOMENTUM: replay and ablation (2026-10-06)\n")
    R(f"BTC 5-minute windows {results['period']['from']} -> {results['period']['to']} UTC, {len(wins)} windows; splits by window index: "
      + ", ".join(f"{k} {v}" for k, v in results["splits"].items()) + ". Execution: the CALIBRATED model (taker lands at +250 ms, "
      "fills only if the ask stays inside the limit through +1 s, charged the worse ask; fee 7 % x p(1-p)). Fills are all-or-nothing in this "
      "model (no partial fills), so the fill rate below is orders that filled / orders sent.\n")
    R("## 1. The brief's defaults with the rule-based estimator\n")
    R("The rule gives the token's mid plus at most 6 cents; against ~5 cents of charged costs and a 3-cent edge floor it cannot trade "
      "(tests/test_confirmation_momentum.py pins this). Candidates that passed every structural gate:\n")
    R(HEAD)
    for s, m in rule_res.items():
        R(fmt_row(f"rule defaults / {s}", m))
    R("\n## 2. The fitted model\n")
    if model_doc:
        R(f"Logistic regression over {len(cm.MODEL_FEATURES)} features fitted on {model_doc['fitted_on']['train']} TRAIN candidates "
          f"(win rate {model_doc['train_win_rate']:.3f}), Platt-recalibrated on {model_doc['fitted_on']['validation']} VALIDATION candidates "
          f"(win rate {model_doc['validation_win_rate']:.3f}); written to data/replay/cm_model.json. Coefficients (standardised): "
          + ", ".join(f"{k} {b:+.2f}" for k, b in zip(cm.MODEL_FEATURES, model_doc["beta"][1:])) + ".\n")
    else:
        R("Too few candidates to fit a model.\n")
    R("## 3. Edge threshold chosen on VALIDATION\n")
    R("| min net edge | trades | P&L | per trade |\n|---:|---:|---:|---:|")
    for thr, n, pnl, pt in thr_rows:
        R(f"| {thr:.2f} | {n} | {pnl:+.2f} | {'-' if pt is None else f'{pt:+.3f}'} |")
    R(f"\nChosen: {best_thr:.2f} (best P&L per market with at least 20 validation trades).\n")
    if grid_rows:
        R("## 4. TRAIN grid (z5 minimum x token confirmation cents x last entry second)\n")
        R("| z5 min | confirm c | max elapsed | trades | P&L | per market |\n|---:|---:|---:|---:|---:|---:|")
        for z5, tc, me, n, pnl, pm in grid_rows:
            R(f"| {z5} | {tc:.0f} | {me:.0f} | {n} | {pnl:+.2f} | {'-' if pm is None else f'{pm:+.3f}'} |")
        R(f"\nChosen: z5 >= {best_p.z_5s_min}, confirmation {best_p.token_confirm_cents:.0f}c, entries until {best_p.max_elapsed_s:.0f} s.\n")
    R("## 5. Ablation (section 19): which component carries the result\n")
    R(HEAD)
    for name, r in abl.items():
        for s in ("train", "validation", "test", "forward"):
            R(fmt_row(f"{name} / {s}", r[s]))
        R("| | | | | | | | | | | |")
    R("\n## 6. Rejection statistics of the selected strategy (all splits)\n")
    tot = collections.Counter()
    for s in abl["B confirmation momentum (selected)"].values():
        tot.update(s.get("rejects", {}))
    R("| reason | evaluations |\n|---|---:|")
    for k in cm.REJECTS:
        R(f"| {k} | {tot.get(k, 0):,} |")
    R("\n## 7. Out-of-sample splits (TEST + FORWARD trades of the selected strategy)\n")
    for label, rows in tables:
        R(f"**{label}**\n\n| bucket | trades | P&L | per trade | win |\n|---|---:|---:|---:|---:|")
        for k, n, pnl, pt, wr in rows:
            R(f"| {k} | {n} | {pnl:+.2f} | {pt:+.3f} | {wr:.0%} |")
        R("")
    doc = "\n".join(report)
    with open(f"{ROOT}/docs/confirmation_momentum_2026-10-06.md", "w") as fh:
        fh.write(doc)
    with open(f"{ROOT}/results/cm_replay_2026-10-06.json", "w") as fh:
        json.dump(results, fh, indent=1, default=str)
    if model_doc:
        with open(f"{ROOT}/data/replay/cm_model.json", "w") as fh:
            json.dump(model_doc, fh, indent=1)
    print(doc)
    print(f"\ndone in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
