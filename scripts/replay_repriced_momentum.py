"""Research replay: does REPRICED-TOKEN MOMENTUM have an edge?

    python scripts/replay_repriced_momentum.py [--rebuild] [--out data/replay/repriced_momentum_research.md]

Hypothesis (docs/repriced_momentum_2026-09-24.md): after a large spot shock, once the
token has already repriced part of the way to where the market's own curve says it
should be, a spot move that is STILL continuing -- with the token still following --
leaves a short continuation worth trading after costs.

Protocol (no lookahead, no tuning on unseen data):
  1. shocks: the spot-lag rule (>= 3 sigma 1 s spot move, 3 s agrees, accelerating)
  2. candidates: every archive point in the 5 s after a shock, with causal features
  3. every candidate is executed with the validated venue_lag model (spot_shock_replay):
     FOK at a limit = the decision ask (never crossing further), measured latency, the
     book read 500 ms late, both fees; and sold by each of 11 exits
  4. TRAINING ONLY: the repricing x momentum matrix, the expected-continuation table
     (forward token moves), a grid of rules -> ONE rule chosen by training t -> LOCKED
  5. the locked rule, the brief's own first configuration and the baselines are scored
     on validation and two later unseen periods, never used for any choice
READ-ONLY: reads data/charts (via a feature cache), writes a markdown report and the
locked parameters (data/replay/repriced_momentum_params.json).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.backtest.spot_shock_replay import (  # noqa: E402
    SETTLE_GUARD_MS, SIZE, Execution, build_features, detect_shocks, load_asset, windows,
)

CACHE = ROOT / "data/replay/spot_shock_cache"
ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
SPOT_LAG_ASSETS = tuple(a for a in ASSETS if a != "HYPE")
WINDOW_MS = 5000.0
MAX_HOLD_EVENT_S = 8.0
MAKER_REST_MS = 2000.0
SLIPPAGE = 0.005                      # the cost model's slippage allowance (spot-lag's own)
FIXED = (1, 2, 3, 5, 8, 10)
EVENTS = ("spot reversal", "token reversal", "agreement lost", "fair reached", "any event")
EXITS = tuple(f"hold {h} s" for h in FIXED) + tuple(f"{e} (max 8 s)" for e in EVENTS)
EX_AGREE = EXITS.index("agreement lost (max 8 s)")
EX_HOLD = {h: EXITS.index(f"hold {h} s") for h in FIXED}
HORIZONS = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0)
EXIT_HORIZON = {**{EX_HOLD[h]: HORIZONS.index(float(h)) for h in FIXED},
                **{EXITS.index(f"{e} (max 8 s)"): HORIZONS.index(8.0) for e in EVENTS}}
PCT_BUCKETS = ((0.25, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0),
               (1.0, 1.1), (1.1, 1.25), (1.25, 9.0))
STRENGTH = ((-99.0, 0.0), (0.0, 0.5), (0.5, 1.0), (1.0, 99.0))     # spot 500 ms move, sigma units
SNAME = ("reversing (< 0)", "weak (0-0.5 sigma)", "medium (0.5-1 sigma)", "strong (>= 1 sigma)")
SPLITS = ("train", "validation", "unseen_a", "unseen_b")
UNSEEN = ("validation", "unseen_a", "unseen_b")
FEAT = ("pct", "expected", "actual", "fair", "ask", "bid", "mid", "spread", "overshoot", "adep",
        "s250", "s500", "s1", "s2", "t250", "t500", "t1", "s_acc", "t_acc", "tss", "z1", "left")
FILLCOLS = ("filled", "entry", "arrival_ask", "latency", "quote_gone", "p1_filled", "p1_entry",
            "mk_filled", "mk_entry", "mk_wait")
OUT = ("net", "exit_px", "hold", "fees", "spread_cost", "slip")


def _lag_diff(x: np.ndarray, idx: np.ndarray) -> np.ndarray:
    return np.where(idx >= 0, x - x[np.maximum(idx, 0)], np.nan)


def _row(w, entry, entry_fee, px, how, t_out, t_arr, jd, dec_bid, mid, jdec, ask):
    if px is None:
        return [np.nan] * len(OUT)
    fees = entry_fee + (0.0 if how == "SETTLE" else w.fee(px))
    spread_cost = float(ask[jdec]) - float(mid[jdec])
    slip = entry - float(ask[jdec])
    if how != "SETTLE" and jd >= 0 and mid[jd] == mid[jd] and dec_bid is not None:
        spread_cost += float(mid[jd]) - dec_bid
        slip += dec_bid - px
    return [px - entry - fees, px, (t_out - t_arr) / 1000.0, fees, spread_cost, slip]


def run_asset(asset: str) -> dict:
    d, meta = load_asset(CACHE, asset)
    ex = Execution(seed=sum(map(ord, asset)))
    feats, fills, outs, fwd, rt, keys, base = [], [], [], [], [], [], []
    n_shocks = defaultdict(int)
    for w in windows(d, meta):
        if len(w) < 30:
            continue
        r250, r500, r1, r2 = (w.ret(x) for x in (250.0, 500.0, 1000.0, 2000.0))
        k250, k500, k1000 = (w.lagged_index(x) for x in (250.0, 500.0, 1000.0))
        guard = w.last_at(w.close - SETTLE_GUARD_MS - 1.0)
        for i, dr in detect_shocks(w):
            n_shocks[w.split] += 1
            side = "UP" if dr > 0 else "DOWN"
            sv = w.side[side]
            mid, bid, ask, fvs, adep = sv["mid"], sv["bid"], sv["ask"], sv["mfv"], sv["adep"]
            i0 = k1000[i]
            if i0 < 0 or mid[i0] != mid[i0] or fvs[i0] != fvs[i0]:
                continue
            mid_b, fv_b = float(mid[i0]), float(fvs[i0])
            won = None if w.won_up is None else float((side == "UP") == w.won_up)
            sid = f"{w.slug}:{int(w.t[i])}:{side}"
            z1_shock = abs(r1[i] / w.sigma[i])
            tok250, tok500, tok1 = _lag_diff(mid, k250), _lag_diff(mid, k500), _lag_diff(mid, k1000)
            spot_rev = dr * r500 < 0                                 # the spot turned against us
            tok_rev = tok500 < 0                                     # the token turned against us
            fair_hit = bid >= mid_b + (fvs - fv_b) - 1e-9            # the bid reached the expected price
            trig = {"spot reversal": spot_rev, "token reversal": tok_rev, "agreement lost": spot_rev | tok_rev,
                    "fair reached": fair_hit, "any event": spot_rev | tok_rev | fair_hit}

            def sell_fixed(entry, entry_fee, t_arr, t_known, t_dec, jdec):
                t_dec = max(t_known, t_dec)
                jd = w.last_at(t_dec)
                b = float(bid[jd]) if jd >= 0 and bid[jd] == bid[jd] else entry
                px, how, t_out, dec_bid = ex.sell(w, sv, t_dec, b - 0.02, won)
                return _row(w, entry, entry_fee, px, how, t_out, t_arr, jd, dec_bid, mid, jdec, ask)

            def exits_for(entry, entry_fee, t_arr, t_known, jdec) -> list:
                res = [sell_fixed(entry, entry_fee, t_arr, t_known, t_arr + h * 1000.0, jdec) for h in FIXED]
                j_from = w.first_at(t_known)
                j_to = min(w.last_at(t_arr + MAX_HOLD_EVENT_S * 1000.0), guard)
                for name in EVENTS:
                    seg = trig[name][j_from:j_to + 1] & ~np.isnan(bid[j_from:j_to + 1])
                    if len(seg) and seg.any():
                        jj = j_from + int(np.argmax(seg))
                        take_profit = bool(fair_hit[jj]) and name in ("fair reached", "any event")
                        floor = float(bid[jj]) - (0.0 if take_profit else 0.02)
                        px, how, t_out, dec_bid = ex.sell(w, sv, float(w.t[jj]), floor, won)
                        res.append(_row(w, entry, entry_fee, px, how, t_out, t_arr, jj, dec_bid, mid, jdec, ask))
                    else:                                            # nothing fired: the maximum hold
                        res.append(sell_fixed(entry, entry_fee, t_arr, t_known, t_arr + MAX_HOLD_EVENT_S * 1000.0,
                                              jdec))
                return res

            # ---- baseline A: the spot-lag live entry at the shock, its own exit (HYPE excluded, as live) ----
            if asset in SPOT_LAG_ASSETS and ask[i] == ask[i] and bid[i] == bid[i] and 0.05 <= ask[i] <= 0.95 \
                    and ask[i] - bid[i] <= 0.03 + 1e-9:
                ev = mid_b + (fvs[i] - fv_b)
                a0 = float(ask[i])
                fixed = 0.01 + w.fee(min(max(ev, .01), .99)) + float(ask[i] - bid[i]) / 2.0
                if ev - a0 > 0 and ev - a0 - w.fee(a0) - fixed >= 0.02:
                    me = _max_entry(w, ev, fixed, 0.02)
                    if me is not None and me >= a0 - 1e-9:
                        fl = ex.buy(w, sv, float(w.t[i]), min(a0 + 0.02, me))
                        rec = {"split": w.split, "epoch": w.epoch, "asset": asset, "filled": fl["filled"],
                               "t": float(w.t[i])}
                        if fl["filled"]:
                            z1s = dr * r1 / w.sigma
                            j = w.first_at(fl["t_known"])
                            t_sell = None
                            while j < len(w) and w.t[j] < w.close - SETTLE_GUARD_MS:
                                held = (w.t[j] - fl["t_arr"]) / 1000.0
                                if held >= 5.0 and not (held < 10.0 and z1s[j] == z1s[j] and z1s[j] >= 0.5):
                                    t_sell = float(w.t[j])
                                    break
                                j += 1
                            if t_sell is None:
                                px, how = (won, "SETTLE") if won is not None else (None, "UNKNOWN")
                            else:
                                jd = w.last_at(t_sell)
                                px, how, _, _ = ex.sell(w, sv, t_sell, float(bid[jd]) - 0.02, won)
                            if px is not None:
                                rec["net"] = px - fl["entry"] - w.fee(fl["entry"]) - (0.0 if how == "SETTLE"
                                                                                    else w.fee(px))
                        base.append(rec)

            # ---- candidate entry points in the 5 s after the shock ----
            j_last = min(w.last_at(w.t[i] + WINDOW_MS), len(w) - 1)
            for j in range(i, j_last + 1):
                if w.left[j] < 60.0:
                    break
                a, b, m = ask[j], bid[j], mid[j]
                if a != a or b != b or m != m or fvs[j] != fvs[j]:
                    continue
                expected = float(fvs[j] - fv_b)
                if expected < 0.01:                                  # no meaningful expected move
                    continue
                sg = float(w.sigma[j])
                fair = mid_b + expected
                f = {"pct": (m - mid_b) / expected, "expected": expected, "actual": float(m - mid_b), "fair": fair,
                     "ask": float(a), "bid": float(b), "mid": float(m), "spread": float(a - b),
                     "overshoot": float(a - fair), "adep": float(adep[j] or 0.0),
                     "s250": dr * r250[j] / (sg * 0.5), "s500": dr * r500[j] / (sg * math.sqrt(0.5)),
                     "s1": dr * r1[j] / sg, "s2": dr * r2[j] / (sg * math.sqrt(2.0)),
                     "t250": float(tok250[j]), "t500": float(tok500[j]), "t1": float(tok1[j]),
                     "s_acc": dr * (r500[j] - (r1[j] - r500[j])) / sg,
                     "t_acc": float(tok500[j] - (tok1[j] - tok500[j])),
                     "tss": (w.t[j] - w.t[i]) / 1000.0, "z1": z1_shock, "left": float(w.left[j])}
                if not (0.25 <= f["pct"] <= 3.0) or not (0.05 <= a <= 0.95) or f["spread"] > 0.03 + 1e-9 \
                        or f["adep"] < SIZE:
                    continue
                t_dec = float(w.t[j])
                # forward token moves: side mid, and a zero-latency round trip ask(t) -> bid(t + h)
                fm, fr = [], []
                for h in HORIZONS:
                    jh = w.last_at(t_dec + h * 1000.0)
                    ok = j < jh <= guard and mid[jh] == mid[jh]
                    fm.append(float(mid[jh] - m) if ok else np.nan)
                    fr.append(float(bid[jh] - a - w.fee(a) - w.fee(bid[jh])) if ok else np.nan)
                # mode 1: FOK at the decision ask (the strategy)
                fl = ex.buy(w, sv, t_dec, float(a))
                o1 = exits_for(fl["entry"], w.fee(fl["entry"]), fl["t_arr"], fl["t_known"], j) if fl["filled"] else None
                # mode 2 (diagnostic): FOK at ask + 1c
                f2 = ex.buy(w, sv, t_dec, float(a) + 0.01)
                o2 = exits_for(f2["entry"], w.fee(f2["entry"]), f2["t_arr"], f2["t_known"], j) if f2["filled"] else None
                # mode 3 (diagnostic): a passive bid at the decision bid, resting 2 s, filled
                # when the (venue-proxied) ask comes down to it; maker: no entry fee
                pre, half = ex.latency()
                t_arr = t_dec + pre + half
                k0 = w.first_at(t_arr + ex.proxy_ms)
                k1 = min(w.last_at(t_arr + MAKER_REST_MS + ex.proxy_ms), guard)
                o3, mk = None, (0.0, np.nan, np.nan)
                if k0 <= k1:
                    hit = ask[k0:k1 + 1] <= b + 1e-9
                    if hit.any():
                        kf = k0 + int(np.argmax(hit))
                        t_fill = float(w.t[kf]) - ex.proxy_ms
                        o3 = exits_for(float(b), 0.0, t_fill, t_fill + half, j)
                        mk = (1.0, float(b), t_fill - t_dec)
                nanrow = [[np.nan] * len(OUT)] * len(EXITS)
                feats.append([f[k] for k in FEAT])
                fills.append([1.0 if fl["filled"] else 0.0, fl.get("entry", np.nan),
                              fl["arrival_ask"] if fl["arrival_ask"] is not None else np.nan,
                              fl["latency_ms"], 1.0 if fl["quote_gone"] else 0.0,
                              1.0 if f2["filled"] else 0.0, f2.get("entry", np.nan), *mk])
                outs.append([o1 or nanrow, o2 or nanrow, o3 or nanrow])
                fwd.append(fm)
                rt.append(fr)
                keys.append((w.split, w.epoch, asset, sid, j - i, t_dec, w.slug))
    return {"feats": np.array(feats, dtype=float), "fills": np.array(fills, dtype=float),
            "outs": np.array(outs, dtype=np.float32), "fwd": np.array(fwd, dtype=float),
            "rt": np.array(rt, dtype=float), "keys": keys, "base": base, "n_shocks": dict(n_shocks)}


def _max_entry(w, ev, fixed, min_edge):
    budget = ev - fixed - min_edge
    p = math.floor((min(budget, 0.99) + 1e-9) * 100) / 100
    while p >= 0.01:
        if p + w.fee(p) <= budget + 1e-12:
            return round(p, 2)
        p = round(p - 0.01, 2)
    return None


# ─────────────────────────────── analysis ───────────────────────────────


class Data:
    def __init__(self, parts: list[dict]) -> None:
        keep = [p for p in parts if len(p["feats"])]
        self.F = np.vstack([p["feats"] for p in keep])
        self.fill = np.vstack([p["fills"] for p in keep])
        self.O = np.concatenate([p["outs"] for p in keep])
        self.fwd = np.vstack([p["fwd"] for p in keep])
        self.rt = np.vstack([p["rt"] for p in keep])
        keys = [k for p in keep for k in p["keys"]]
        self.split = np.array([k[0] for k in keys])
        self.epoch = np.array([k[1] for k in keys])
        self.asset = np.array([k[2] for k in keys])
        uniq = {s: n for n, s in enumerate(dict.fromkeys(k[3] for k in keys))}
        self.shock = np.array([uniq[k[3]] for k in keys])
        self.jrank = np.array([k[4] for k in keys])
        self.t = np.array([k[5] for k in keys])
        self.slug = np.array([k[6] for k in keys])
        order = np.lexsort((self.jrank, self.shock))                # by shock, then time after it
        for name in ("F", "fill", "O", "fwd", "rt", "split", "epoch", "asset", "shock", "jrank", "t", "slug"):
            setattr(self, name, getattr(self, name)[order])
        self.col = {k: i for i, k in enumerate(FEAT)}
        self.fcol = {k: i for i, k in enumerate(FILLCOLS)}
        self.base = [b for p in parts for b in p["base"]]
        self.n_shocks = Counter()
        for p in parts:
            self.n_shocks.update(p["n_shocks"])

    def f(self, name):
        return self.F[:, self.col[name]]

    def first(self, mask):
        """Index of the FIRST candidate per shock that satisfies ``mask`` -- the live
        engine sees the points in time order and enters at the first that qualifies."""
        idx = np.where(mask)[0]
        if not len(idx):
            return idx
        _, first = np.unique(self.shock[idx], return_index=True)
        return idx[first]


def bucket_of(x, buckets):
    out = np.full(len(x), -1)
    for k, (lo, hi) in enumerate(buckets):
        out[(x >= lo) & (x < hi)] = k
    return out


def pct_label(k):
    lo, hi = PCT_BUCKETS[k]
    return f"{lo*100:.0f}+ %" if hi > 5 else f"{lo*100:.0f}-{hi*100:.0f} %"


def stats(D: Data, idx: np.ndarray, ex: int, mode: int = 0) -> dict:
    """Every metric the brief asks for, over the orders at ``idx`` sold by exit ``ex``."""
    fc = {0: "filled", 1: "p1_filled", 2: "mk_filled"}[mode]
    ec = {0: "entry", 1: "p1_entry", 2: "mk_entry"}[mode]
    filled = idx[D.fill[idx, D.fcol[fc]] == 1.0]
    o = D.O[filled, mode, ex, :].astype(float)
    ok = ~np.isnan(o[:, 0])
    filled, o = filled[ok], o[ok]
    out = {"orders": len(idx), "fills": len(filled),
           "fill_rate": len(filled) / len(idx) if len(idx) else float("nan")}
    if len(filled) < 2:
        return out
    net = o[:, 0]
    by_ep = defaultdict(float)
    for e, v in zip(D.epoch[filled], net):
        by_ep[e] += v
    v = np.array(list(by_ep.values()))
    se = v.std(ddof=1) / math.sqrt(len(v)) if len(v) > 2 else float("nan")
    t = v.mean() / se if se == se and se > 0 else float("nan")
    per_fill_se = se * len(v) / len(net) if se == se else float("nan")
    order = np.argsort(D.t[filled])
    cum = np.concatenate([[0.0], np.cumsum(net[order] * SIZE)])
    wins, losses = net[net > 0], net[net <= 0]
    entry = D.fill[filled, D.fcol[ec]]
    out.update({
        "idx": filled, "net_arr": net,
        "wins": int((net > 0).sum()), "losses": int((net <= 0).sum()), "win_rate": float((net > 0).mean()),
        "avg_entry": float(entry.mean()), "avg_exit": float(np.nanmean(o[:, 1])), "avg_hold": float(np.nanmean(o[:, 2])),
        "gross": float((o[:, 1] - entry).sum() * SIZE), "fees": float(o[:, 3].sum() * SIZE),
        "spread_cost": float(o[:, 4].sum() * SIZE), "slippage": float(o[:, 5].sum() * SIZE),
        "net_usdc": float(net.sum() * SIZE), "c_share": float(net.mean() * 100), "c_trade": float(net.mean() * 100 * SIZE),
        "max_dd": float(np.max(np.maximum.accumulate(cum) - cum)),
        "pf": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "median": float(np.median(net) * 100), "p25": float(np.percentile(net, 25) * 100),
        "p75": float(np.percentile(net, 75) * 100), "t": t,
        "ci95": (float((net.mean() - 1.96 * per_fill_se) * 100), float((net.mean() + 1.96 * per_fill_se) * 100)),
        "epochs": len(v)})
    return out


STATS_HEAD = ("| strategy | orders | fills | fill rate | win rate | net c/share | t | 95% CI c/share | "
              "net USDC (5 sh) | max DD USDC | profit factor | avg hold | p25 / median / p75 c |\n"
              "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---|")


def fmt_stats_row(name, s):
    if s.get("fills", 0) < 2:
        return f"| {name} | {s.get('orders', 0)} | {s.get('fills', 0)} |" + " |" * 10
    return (f"| {name} | {s['orders']} | {s['fills']} | {s['fill_rate']:.0%} | {s['win_rate']:.0%} | "
            f"{s['c_share']:+.2f} | {s['t']:+.1f} | [{s['ci95'][0]:+.1f}, {s['ci95'][1]:+.1f}] | "
            f"{s['net_usdc']:+.2f} | {s['max_dd']:.2f} | {s['pf']:.2f} | {s['avg_hold']:.1f} s | "
            f"{s['p25']:+.1f} / {s['median']:+.1f} / {s['p75']:+.1f} |")


def portfolio(D: Data, idx: np.ndarray, ex: int, max_positions=1, max_order_usdc=3.0, max_exposure_usdc=5.0,
              max_daily_loss_usdc=5.0) -> dict:
    """The momentum budget, applied in time order across every asset: a position (or a
    working order) blocks new entries until it is closed; an order may not cost more than
    ``max_order_usdc`` at its limit; trading stops for the UTC day at the loss limit."""
    order = idx[np.argsort(D.t[idx])]
    busy: list[float] = []
    day_pnl: dict[str, float] = defaultdict(float)
    taken, skipped = [], Counter()
    for n in order:
        t0 = D.t[n]
        day = dt.datetime.utcfromtimestamp(t0 / 1000.0).strftime("%Y-%m-%d")
        busy = [b for b in busy if b > t0]
        limit = D.f("ask")[n]
        if limit * SIZE > max_order_usdc + 1e-9:
            skipped["order cap"] += 1
            continue
        if len(busy) >= max_positions or (len(busy) + 1) * limit * SIZE > max_exposure_usdc + 1e-9:
            skipped["position / exposure cap"] += 1
            continue
        if day_pnl[day] <= -max_daily_loss_usdc + 1e-9:
            skipped["daily loss stop"] += 1
            continue
        lat = D.fill[n, D.fcol["latency"]]
        if D.fill[n, D.fcol["filled"]] != 1.0 or D.O[n, 0, ex, 0] != D.O[n, 0, ex, 0]:
            busy.append(t0 + lat)
            taken.append((n, None))
            continue
        net = float(D.O[n, 0, ex, 0]) * SIZE
        busy.append(t0 + lat + float(D.O[n, 0, ex, 2]) * 1000.0)
        day_pnl[day] += net
        taken.append((n, net))
    fills = [v for _, v in taken if v is not None]
    cum = np.concatenate([[0.0], np.cumsum(fills)]) if fills else np.zeros(1)
    return {"orders": len(taken), "fills": len(fills), "net": float(sum(fills)),
            "max_dd": float(np.max(np.maximum.accumulate(cum) - cum)), "skipped": dict(skipped),
            "days": {k: round(v, 2) for k, v in sorted(day_pnl.items())}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rebuild", action="store_true", help="rebuild the feature cache from data/charts")
    ap.add_argument("--out", default=str(ROOT / "data/replay/repriced_momentum_research.md"))
    ap.add_argument("--params", default=str(ROOT / "data/replay/repriced_momentum_params.json"))
    args = ap.parse_args()
    if args.rebuild or not (CACHE / "splits.json").exists():
        build_features(ROOT / "data/charts", CACHE)
    splits = json.loads((CACHE / "splits.json").read_text())
    with ProcessPoolExecutor(3) as pool:
        parts = list(pool.map(run_asset, ASSETS))
    D = Data(parts)
    L: list[str] = []
    P = L.append

    def fmt_ep(e):
        return dt.datetime.utcfromtimestamp(e).strftime("%m-%d %H:%M")

    P("# Repriced-token momentum: research replay\n")
    P("## Data and splits\n")
    P("| split | 5-min epochs | from | to (last window open) | spot shocks (all assets) |\n|---|---:|---|---|---:|")
    for s in SPLITS:
        lo, hi, n = splits[s]
        P(f"| {s} | {n} | {fmt_ep(lo)} | {fmt_ep(hi)} UTC | {D.n_shocks.get(s, 0):,} |")
    P(f"\n{len(D.shock):,} candidate entry points (every archive point in the 5 s after a shock with a valid book, "
      f">= 1c expected move, 25-300 % repriced, spread <= 3c, ask 0.05-0.95, >= 5 shares at the ask) over "
      f"{len(set(D.shock.tolist())):,} shocks.\n")

    tr = D.split == "train"
    un = np.isin(D.split, UNSEEN)
    pct, over = D.f("pct"), D.f("overshoot")
    mom = {"250 ms": D.f("s250"), "500 ms": D.f("s500"), "1 s": D.f("s1")}
    t500 = D.f("t500")
    pb = bucket_of(pct, PCT_BUCKETS)
    sb = bucket_of(D.f("s500"), STRENGTH)
    agree = (t500 > 0) & (D.f("s500") > 0)                          # spot still moving our way, token following

    # ---------------- forward token moves (no execution): the continuation itself ----------------
    P("## Forward token moves after the decision (no execution; spot continuing and token agreeing)\n")
    P("Mean change of the side's MID (cents) h seconds after the decision point, then what a ZERO-latency trader "
      "would net buying the ask and selling the bid h later after both fees. First qualifying moment per shock.\n")
    cont_table = {}
    for lab, sm in (("TRAINING", tr), ("UNSEEN (validation + A + B)", un)):
        P(f"### {lab}\n")
        P("| repriced | n | " + " | ".join(f"mid +{h:g} s" for h in HORIZONS[:7]) + " | round trip +1 s | +3 s | +8 s |")
        P("|---|---:|" + "---:|" * 10)
        for k in range(len(PCT_BUCKETS)):
            idx = D.first((pb == k) & agree & sm)
            if len(idx) < 5:
                continue
            fm = np.nanmean(D.fwd[idx], axis=0) * 100
            rr = np.nanmean(D.rt[idx], axis=0) * 100
            P(f"| {pct_label(k)} | {len(idx)} | " + " | ".join(f"{x:+.2f}" for x in fm[:7])
              + f" | {rr[2]:+.2f} | {rr[4]:+.2f} | {rr[6]:+.2f} |")
        P("")
    # the expected-continuation table: TRAINING mean forward mid move per (repricing, strength, horizon)
    for k in range(len(PCT_BUCKETS)):
        for g in range(len(STRENGTH)):
            idx = D.first((pb == k) & (sb == g) & (t500 > 0) & tr)
            if len(idx) >= 20:
                cont_table[(k, g)] = np.nanmean(D.fwd[idx], axis=0)
    P("### Expected continuation table (TRAINING, mid move in cents, token agreeing) -- what the edge gate uses\n")
    P("| repriced | " + " | ".join(f"{SNAME[g]} +1 s / +3 s / +8 s" for g in range(len(STRENGTH))) + " |")
    P("|---|" + "---|" * len(STRENGTH))
    for k in range(len(PCT_BUCKETS)):
        cells = []
        for g in range(len(STRENGTH)):
            c = cont_table.get((k, g))
            cells.append("—" if c is None else f"{c[2]*100:+.2f} / {c[4]*100:+.2f} / {c[6]*100:+.2f}")
        P(f"| {pct_label(k)} | " + " | ".join(cells) + " |")
    P("")

    def edge_for(ex: int) -> np.ndarray:
        """expected continuation (TRAINING table, the exit's horizon) minus the costs at the
        decision: the spread (half in, half out), both taker fees, the slippage allowance."""
        h = EXIT_HORIZON[ex]
        cont = np.array([cont_table[(pb[n], sb[n])][h] if (pb[n], sb[n]) in cont_table else np.nan
                         for n in range(len(pb))])
        ask, mid, rate = D.f("ask"), D.f("mid"), 0.07
        exit_px = np.clip(mid + np.nan_to_num(cont), 0.01, 0.99)
        cost = D.f("spread") + rate * ask * (1 - ask) + rate * exit_px * (1 - exit_px) + SLIPPAGE
        return cont - cost
    edges = {e: edge_for(e) for e in range(len(EXITS))}

    # ---------------- the matrix ----------------
    P("## Repricing x momentum (the brief's key table)\n")
    P("Entry at the first moment a shock's token is in the cell with the token agreeing (500 ms token move > 0); "
      "FOK at the ask; exit: momentum reversal (spot or token) or 8 s. Cells: shocks reaching the cell / fills / "
      "net c per share / net USDC at 5 shares.\n")
    for lab, sm in (("TRAINING", tr), ("UNSEEN (validation + A + B)", un)):
        P(f"### {lab}\n")
        P("| repriced | " + " | ".join(SNAME) + " |\n|---|" + "---|" * len(SNAME))
        for k in range(len(PCT_BUCKETS)):
            cells = []
            for g in range(len(STRENGTH)):
                s = stats(D, D.first((pb == k) & (sb == g) & (t500 > 0) & sm), EX_AGREE)
                cells.append("—" if s.get("fills", 0) < 2 else
                             f"{s['orders']} / {s['fills']} / **{s['c_share']:+.2f}c** / {s['net_usdc']:+.1f}")
            P(f"| {pct_label(k)} | " + " | ".join(cells) + " |")
        P("")
    P("### Does any exit rescue a repricing bucket? (TRAINING, spot continuing + token agreeing, net c/share)\n")
    P("| repriced | " + " | ".join(EXITS) + " |\n|---|" + "---:|" * len(EXITS))
    for k in range(len(PCT_BUCKETS)):
        idx = D.first((pb == k) & agree & tr)
        P(f"| {pct_label(k)} | " + " | ".join(
            f"{s['c_share']:+.2f}" if s.get("fills", 0) >= 2 else "—" for s in (stats(D, idx, e) for e in range(len(EXITS))))
          + " |")
    P("")

    # ---------------- the grid, TRAINING ONLY ----------------
    zones = ((0.25, 0.5), (0.4, 0.7), (0.5, 0.8), (0.5, 0.9), (0.6, 0.9), (0.7, 1.0))
    ms = (0.0, 0.5, 1.0)
    overs = (0.0, 0.005, 0.01, 0.02)
    gates = (None, 0.0, 0.01)
    results = []
    for (lo, hi) in zones:
        zmask = (pct >= lo) & (pct <= hi) & (t500 > 0)
        for hname, sx in mom.items():
            for m in ms:
                mmask = zmask & (sx > m) if m == 0.0 else zmask & (sx >= m)
                for osx in overs:
                    omask = mmask & (over <= osx + 1e-9)
                    for e in range(len(EXITS)):
                        for g in gates:
                            mask = omask if g is None else (omask & (edges[e] >= g))
                            s = stats(D, D.first(mask & tr), e)
                            s.pop("idx", None)
                            s.pop("net_arr", None)
                            results.append({"zone": (lo, hi), "hor": hname, "mom": m, "over": osx, "exit": e,
                                            "gate": g, "train": s, "mask": mask})
    eligible = [r for r in results if r["train"].get("fills", 0) >= 30
                and r["train"].get("t", float("nan")) == r["train"].get("t")]
    best = max(eligible, key=lambda r: (r["train"]["t"], r["train"]["c_share"]))
    locked = {"min_pct": best["zone"][0], "max_pct": best["zone"][1], "momentum_horizon": best["hor"],
              "min_spot_z": best["mom"], "max_overshoot": best["over"], "exit": EXITS[best["exit"]],
              "min_edge": best["gate"], "chosen_by": "highest TRAINING t-stat (clustered by epoch), >= 30 training fills",
              "configs_searched": len(results), "training_fills": best["train"]["fills"],
              "training_c_share": round(best["train"]["c_share"], 3), "training_t": round(best["train"]["t"], 2),
              "continuation_table_cents": {f"{PCT_BUCKETS[k][0]}-{PCT_BUCKETS[k][1]}|{STRENGTH[g][0]}-{STRENGTH[g][1]}":
                                           [round(x * 100, 3) for x in v] for (k, g), v in sorted(cont_table.items())},
              "continuation_horizons_s": list(HORIZONS),
              "train_epochs": splits["train"], "fitted": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
    Path(args.params).write_text(json.dumps(locked, indent=1))
    P("## Rule selection (TRAINING ONLY)\n")
    P(f"{len(results):,} configurations: repricing zone {len(zones)} x spot-momentum horizon {len(mom)} x threshold "
      f"{len(ms)} x overshoot {len(overs)} x exit {len(EXITS)} x expected-edge gate {len(gates)}; the token must agree "
      f"in all. {len(eligible):,} had >= 30 training fills. Chosen by training t-stat and **locked** before any "
      "other period was scored:\n")
    P("`" + json.dumps({k: v for k, v in locked.items() if k not in ("continuation_table_cents", "continuation_horizons_s")})
      + "`\n")
    n_pos_tr = sum(1 for r in eligible if r["train"]["c_share"] > 0)
    P(f"Configurations with a POSITIVE training result: {n_pos_tr} of {len(eligible)}.\n")

    # ---------------- locked rule, the brief's first configuration, baselines: every period ----------------
    lk = best
    naive = (pct >= 0.5) & (pct <= 0.9)
    initial = naive & (D.f("s500") > 0) & (t500 > 0) & (over <= 0.01 + 1e-9)
    rows = [("B. naive: every token 50-90 % repriced (exit: reversal / 8 s)", naive, EX_AGREE, 0),
            ("B'. naive, sold after 5 s", naive, EX_HOLD[5], 0),
            ("C-init. the brief's first rule: 50-90 %, spot continuing, token agrees, overshoot <= 1c, "
             "edge >= 1c (exit: reversal / 8 s)", initial & (edges[EX_AGREE] >= 0.01), EX_AGREE, 0),
            ("C-init without the edge gate", initial, EX_AGREE, 0),
            ("C0. locked zone + overshoot, NO momentum filter", (pct >= lk["zone"][0]) & (pct <= lk["zone"][1])
             & (over <= lk["over"] + 1e-9), lk["exit"], 0),
            ("**C. repriced + momentum, the locked rule**", lk["mask"], lk["exit"], 0),
            ("C at ask + 1c (diagnostic: crossing further)", lk["mask"], lk["exit"], 1),
            ("C passive at the bid, 2 s (diagnostic: maker)", lk["mask"], lk["exit"], 2)]
    P("## Results by period (every rule fixed before these periods were scored)\n")
    by_split = {}
    for sp in SPLITS:
        spm = D.split == sp
        P(f"### {'TRAINING' if sp == 'train' else 'VALIDATION' if sp == 'validation' else f'UNSEEN ({sp})'}\n")
        P(STATS_HEAD)
        bs = [b for b in D.base if b["split"] == sp]
        bf = [b for b in bs if b["filled"] and "net" in b]
        if len(bf) > 2:
            bn = np.array([b["net"] for b in bf])
            by = defaultdict(float)
            for b in bf:
                by[b["epoch"]] += b["net"]
            v = np.array(list(by.values()))
            tA = v.mean() / (v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 2 else float("nan")
            P(f"| A. spot-lag (its live entry and exit) | {len(bs)} | {len(bf)} | {len(bf)/max(1, len(bs)):.0%} | "
              f"{(bn > 0).mean():.0%} | {bn.mean()*100:+.2f} | {tA:+.1f} | | {bn.sum()*SIZE:+.2f} | | | | |")
        for name, mask, e, mode in rows:
            s = stats(D, D.first(mask & spm), e, mode)
            by_split[(name, sp)] = s
            P(fmt_stats_row(name, s))
        P("| D. no trade | 0 | 0 | | | 0.00 | | | 0.00 | 0.00 | | | |\n")

    # ---------------- pooled unseen, in detail ----------------
    def detail(title, mask, e):
        s = stats(D, D.first(mask & un), e)
        P(f"## {title}: all unseen periods pooled\n")
        if s.get("fills", 0) < 2:
            P("no fills\n")
            return s
        P(f"signals {sum(D.n_shocks.get(x, 0) for x in UNSEEN):,} shocks, of which {s['orders']} produced an order; "
          f"fills {s['fills']} ({s['fill_rate']:.0%}); wins {s['wins']} / losses {s['losses']} ({s['win_rate']:.0%}); "
          f"average entry {s['avg_entry']:.3f}, exit {s['avg_exit']:.3f}, hold {s['avg_hold']:.1f} s\n")
        P(f"gross {s['gross']:+.2f} USDC; fees {s['fees']:.2f}; spread cost {s['spread_cost']:.2f}; slippage "
          f"{s['slippage']:+.2f}; **net {s['net_usdc']:+.2f} USDC** = {s['c_share']:+.2f} c/share = {s['c_trade']:+.2f} "
          f"c/trade; t {s['t']:+.1f}; 95 % CI [{s['ci95'][0]:+.2f}, {s['ci95'][1]:+.2f}] c/share; max drawdown "
          f"{s['max_dd']:.2f} USDC; profit factor {s['pf']:.2f}; trades p25 / median / p75 {s['p25']:+.1f} / "
          f"{s['median']:+.1f} / {s['p75']:+.1f} c/share\n")
        filled, net = s["idx"], s["net_arr"]

        def breakdown(name, keyfn):
            g = defaultdict(list)
            for n, v in zip(filled, net):
                g[keyfn(n)].append(v)
            P(f"| {name} | fills | net c/share | net USDC |\n|---|---:|---:|---:|")
            for k in sorted(g):
                P(f"| {k} | {len(g[k])} | {np.mean(g[k])*100:+.2f} | {np.sum(g[k])*SIZE:+.2f} |")
            P("")
        breakdown("asset", lambda n: D.asset[n])
        breakdown("repriced", lambda n: pct_label(pb[n]) if pb[n] >= 0 else "?")
        breakdown("shock size", lambda n: "a. 3-4 sigma" if D.f("z1")[n] < 4 else "b. 4-6 sigma" if D.f("z1")[n] < 6
                  else "c. >= 6 sigma")
        breakdown("hold", lambda n: (lambda h: "a. < 2 s" if h < 2 else "b. 2-5 s" if h < 5 else "c. 5-10 s" if h < 10
                                     else "d. >= 10 s")(float(D.O[n, 0, e, 2])))
        breakdown("spot momentum (500 ms)", lambda n: SNAME[sb[n]])
        return s
    sC = detail("The locked rule (C)", lk["mask"], lk["exit"])
    sI = detail("The brief's first rule without the edge gate (C-init)", initial, EX_AGREE)

    # ---------------- portfolio under the momentum budget ----------------
    P("## Under the momentum budget (1 position, $3 order, $5 exposure, $5 daily loss), unseen periods\n")
    P("| rule | orders | fills | net USDC | max drawdown USDC | skipped | net by day |\n|---|---:|---:|---:|---:|---|---|")
    for name, mask, e in (("C (locked)", lk["mask"], lk["exit"]), ("C-init without the gate", initial, EX_AGREE),
                          ("B naive 50-90 %", naive, EX_AGREE)):
        pf = portfolio(D, D.first(mask & un), e)
        P(f"| {name} | {pf['orders']} | {pf['fills']} | {pf['net']:+.2f} | {pf['max_dd']:.2f} | {pf['skipped']} | "
          f"{pf['days']} |")
    P("")

    # ---------------- fill-rate / latency ----------------
    P("## Fill rate and latency: does the momentum survive the delay?\n")
    P("| rule (all periods) | orders | filled | killed: ask above the limit on arrival | mean ask move decision -> "
      "arrival | latency | mid move +1 s, FILLED | mid move +1 s, KILLED | +3 s filled | +3 s killed |")
    P("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, mask in (("C (locked)", lk["mask"]), ("C-init without the gate", initial), ("B naive 50-90 %", naive)):
        idx = D.first(mask)
        fl = D.fill[idx, D.fcol["filled"]] == 1.0
        arr = D.fill[idx, D.fcol["arrival_ask"]] - D.f("ask")[idx]
        P(f"| {name} | {len(idx)} | {fl.mean():.0%} | {np.nanmean(D.fill[idx, D.fcol['quote_gone']] * ~fl):.0%} | "
          f"{np.nanmean(arr)*100:+.2f}c | {np.nanmean(D.fill[idx, D.fcol['latency']]):.0f} ms | "
          f"{np.nanmean(D.fwd[idx[fl], 2])*100:+.2f}c | {np.nanmean(D.fwd[idx[~fl], 2])*100:+.2f}c | "
          f"{np.nanmean(D.fwd[idx[fl], 4])*100:+.2f}c | {np.nanmean(D.fwd[idx[~fl], 4])*100:+.2f}c |")
    P("")

    # ---------------- cost decomposition and failure cases ----------------
    P("## Where the money goes (C-init without the gate, unseen, per share)\n")
    if sI.get("fills", 0) >= 2:
        idx = sI["idx"]
        e = EX_AGREE
        entry = D.fill[idx, D.fcol["entry"]]
        exitp = D.O[idx, 0, e, 1].astype(float)
        P(f"- the token's mid 1 s / 3 s after the decision, FILLED orders only: {np.nanmean(D.fwd[idx, 2])*100:+.2f}c / "
          f"{np.nanmean(D.fwd[idx, 4])*100:+.2f}c (every candidate: see the forward-move table -- the fills are "
          "the ones where the move stalled)")
        P(f"- paid over the decision mid on entry (half spread + arrival slippage): "
          f"{np.nanmean(entry - D.f('mid')[idx])*100:+.2f}c")
        P(f"- price change entry -> exit fill: {np.nanmean(exitp - entry)*100:+.2f}c")
        P(f"- fees (both legs): {np.nanmean(D.O[idx, 0, e, 3].astype(float))*100:.2f}c")
        P(f"- net: {np.nanmean(D.O[idx, 0, e, 0].astype(float))*100:+.2f}c\n")
    if sC.get("fills", 0) >= 2:
        P("### The locked rule's ten worst unseen trades\n")
        P("| market | asset | repriced | spot 500 ms | entry | exit | hold | net c/share |\n"
          "|---|---|---:|---:|---:|---:|---:|---:|")
        for n, v in sorted(zip(sC["idx"], sC["net_arr"]), key=lambda x: x[1])[:10]:
            P(f"| {D.slug[n]} {dt.datetime.utcfromtimestamp(D.t[n]/1000).strftime('%H:%M:%S')} | {D.asset[n]} | "
              f"{pct[n]:.0%} | {D.f('s500')[n]:+.2f} sigma | {D.fill[n, D.fcol['entry']]:.2f} | "
              f"{float(D.O[n, 0, lk['exit'], 1]):.2f} | {float(D.O[n, 0, lk['exit'], 2]):.1f} s | {v*100:+.1f} |")
        P("")

    # ---------------- overfitting checks ----------------
    P("## Overfitting checks\n")
    tr_c, un_c = [], []
    for r in eligible:
        su = stats(D, D.first(r["mask"] & un), r["exit"])
        if su.get("fills", 0) >= 10:
            tr_c.append(r["train"]["c_share"])
            un_c.append(su["c_share"])
    tr_c, un_c = np.array(tr_c), np.array(un_c)
    if len(tr_c) > 3:
        from scipy.stats import spearmanr  # type: ignore
        rho = spearmanr(tr_c, un_c).correlation
        P(f"- {len(un_c)} configurations with >= 30 training and >= 10 unseen fills: unseen net per share positive in "
          f"**{int((un_c > 0).sum())} ({(un_c > 0).mean():.1%})** of them (median {np.median(un_c):+.2f}c, best {un_c.max():+.2f}c, worst "
          f"{un_c.min():+.2f}c). The best of them *in hindsight* (chosen on the unseen data itself, which is not "
          f"allowed) would still be {un_c.max():+.2f}c.")
        P(f"- rank correlation of training vs unseen net per share across configurations: {rho:+.2f}.")
        P(f"- the locked rule: training {best['train']['c_share']:+.2f}c/share (t {best['train']['t']:+.1f}), unseen "
          f"{sC.get('c_share', float('nan')):+.2f}c/share.")
        seen = [by_split.get(("**C. repriced + momentum, the locked rule**", sp), {}).get("c_share") for sp in UNSEEN]
        P("- locked rule per held-out period: " + ", ".join(
            f"{sp} {v:+.2f}c" if v is not None else f"{sp} n/a" for sp, v in zip(UNSEEN, seen)) + "\n")
    Path(args.out).write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
