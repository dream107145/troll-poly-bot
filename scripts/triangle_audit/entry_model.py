"""Triangle entry model (2026-10-01 brief): the exact buy point from the data, chronologically.

    ionice -c3 nice -n 19 python scripts/triangle_audit/entry_model.py > results/triangle_entry_model.md

Data: data/charts BTC windows (200 ms samples: composite spot, both tokens' touch + depth within 2 ticks,
crypto order flow tfi15 / obi / ofi / trade rate). Chronological splits TRAIN 50 / VALIDATION 20 / TEST 15 /
FORWARD 15 % (chart_replay.SPLITS). Candidates = every sample with >= 60 s left where the 1 s spot move is
>= 1 sigma (sigma = the PREVIOUS window's realised 1 s sigma: causal), one per direction per second, side =
the move's direction; plus the high-zone rule's decisions. Execution = chart_replay's: 5 shares FOK at
ask + 3c (capped 0.96), landing 800 ms later, filled if the ask then is <= the limit, taker fee 7 % p(1-p),
settled at the outcome.

Model: logistic P(win | features) fitted on TRAIN only (L2, standardised features);
    EV = P(win) - expected fill price - fee   (expected fill = ask + 1c, the paper run's measured markup)
The EV threshold and the small grid (max price, min seconds left) are chosen on VALIDATION; TEST and FORWARD
are reported untouched. Baselines: the 3-sigma spot-shock rule and the high-zone rule as they run today.
Hedge rule tested on every filled leg: a FOK of the other side 800 ms after the fill is known, taken only
when 1 - other_ask - fee(other) >= P(win) + margin (the certain pair beats the model's expectation).
"""
from __future__ import annotations

import bisect
import json
import math
import statistics
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/triangle_audit")
import chart_replay as cr   # noqa: E402

LAT, LIMIT_OFF, BAND_TOP, SHARES = 800.0, 0.03, 0.96, 5.0
FEE = cr.fee
LOCK = "/root/troll-poly-bot/data/replay/triangle_entry_locked.json"
FEATS = ("z1", "z3", "accel", "tfi15", "obi", "ofi", "tok_vel1", "tok_vel3", "depth_ask", "spread", "left_frac",
         "ask", "fv_gap", "pressure", "hz")


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def window_sigma(w):
    lp = [math.log(p["spot"]) * 1e4 for p in w.pts]
    r = []
    for i in range(0, len(w.pts), 5):
        j = bisect.bisect_right(w.t, w.t[i] - 1000.0) - 1
        if j >= 0 and w.t[i] - w.t[j] < 1300:
            r.append((lp[i] - lp[j]) / math.sqrt((w.t[i] - w.t[j]) / 1000.0))
    return statistics.pstdev(r) if len(r) >= 30 else None


def candidates(w, sigma, strike):
    """(t, side, ask, features) for every candidate moment of one window."""
    lp = [math.log(p["spot"]) for p in w.pts]
    t = w.t
    out = []
    last = {1: -1e18, -1: -1e18}
    hz_last = -1e18
    for i, p in enumerate(w.pts):
        tt = t[i]
        left = p.get("left")
        if left is None or left < 60.0:
            continue
        j1 = bisect.bisect_right(t, tt - 1000.0) - 1
        j3 = bisect.bisect_right(t, tt - 3000.0) - 1
        if j1 < 0 or j3 < 0 or tt - t[j1] > 1300:
            continue
        r1 = (lp[i] - lp[j1]) * 1e4
        r3 = (lp[i] - lp[j3]) * 1e4
        z1, z3 = r1 / sigma, r3 / (sigma * math.sqrt(3.0))
        of = p.get("of") or {}
        sides = []
        if abs(z1) >= 1.0:
            d = 1 if z1 > 0 else -1
            if tt - last[d] >= 1000.0:
                last[d] = tt
                sides.append(("UP" if d > 0 else "DOWN", 0))
        # the high-zone rule (as it runs: ask > 0.85, higher than 5 s ago, not falling over 1 s, one per 5 s)
        if tt - hz_last >= 5000.0:
            for side in ("UP", "DOWN"):
                a = p.get("ua" if side == "UP" else "da")
                if a is None or a <= 0.85:
                    continue
                then, one = w.ask_before(side, tt - 5000.0), w.ask_before(side, tt - 1000.0)
                if then is None or t[0] > tt - 5000.0:
                    continue
                if a > then + 1e-9 and (one is None or a >= one - 1e-9):
                    sides.append((side, 1)); hz_last = tt
                    break
        for side, hz in sides:
            d = 1 if side == "UP" else -1
            ak, bk, dk = ("ua", "ub", "uad") if side == "UP" else ("da", "db", "dad")
            ask, bid = p.get(ak), p.get(bk)
            if ask is None:
                continue
            a1 = w.ask_before(side, tt - 1000.0); a3 = w.ask_before(side, tt - 3000.0)
            te = max(left - 40.0, 1.0)
            fv_up = phi((lp[i] - math.log(strike)) / (sigma * 1e-4 * math.sqrt(te))) if strike > 0 else 0.5
            fv = fv_up if side == "UP" else 1.0 - fv_up
            ua, da = p.get("ua"), p.get("da")
            feats = {"z1": z1 * d, "z3": z3 * d, "accel": (r1 - r3 / 3.0) * d / sigma,
                     "tfi15": (of.get("tfi15") or 0.0) * d, "obi": (of.get("obi") or 0.0) * d, "ofi": (of.get("ofi") or 0.0) * d,
                     "tok_vel1": 0.0 if a1 is None else (ask - a1) * 100.0, "tok_vel3": 0.0 if a3 is None else (ask - a3) * 100.0,
                     "depth_ask": math.log1p(p.get(dk) or 0.0), "spread": 0.0 if bid is None else (ask - bid) * 100.0,
                     "left_frac": left / 300.0, "ask": ask, "fv_gap": fv - ask,
                     "pressure": 0.0 if ua is None or da is None else (ua + da - 1.0) * 100.0, "hz": float(hz)}
            out.append((tt, side, ask, feats))
    return out


def outcome(w, t, side, ask0, limit_off=LIMIT_OFF, band_top=BAND_TOP):
    limit = min(ask0 + limit_off, band_top)
    if ask0 > limit + 1e-9:
        return None
    a1 = w.ask(side, t + LAT)
    if a1 is None or a1 > limit + 1e-9:
        return {"filled": False}
    won = w.outcome == side
    net = (SHARES if won else 0.0) - SHARES * a1 - SHARES * FEE(a1)
    other = "DOWN" if side == "UP" else "UP"
    o2 = w.ask(other, t + 2 * LAT)
    a2 = w.ask(side, t + 2000.0)
    return {"filled": True, "fill": a1, "won": won, "net": net, "fee": SHARES * FEE(a1), "other_ask_1600": o2,
            "tok_move_2s": None if a2 is None else (a2 - a1) * 100.0}


def fit_logistic(X, y, l2=1.0, iters=200):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    Z = np.hstack([np.ones((len(Z), 1)), Z])
    wgt = np.zeros(Z.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-Z @ wgt))
        g = Z.T @ (p - y) + l2 * np.r_[0.0, wgt[1:]]
        H = (Z * (p * (1 - p))[:, None]).T @ Z + l2 * np.diag(np.r_[0.0, np.ones(len(wgt) - 1)])
        step = np.linalg.solve(H, g)
        wgt -= step
        if np.abs(step).max() < 1e-8:
            break
    return {"mu": mu.tolist(), "sd": sd.tolist(), "w": wgt.tolist()}


def predict(model, X):
    Z = (X - np.asarray(model["mu"])) / np.asarray(model["sd"])
    Z = np.hstack([np.ones((len(Z), 1)), Z])
    return 1.0 / (1.0 + np.exp(-Z @ np.asarray(model["w"])))


def stats(rows):
    f = [r for r in rows if r["o"]["filled"]]
    nets = [r["o"]["net"] for r in f]
    eq = peak = dd = 0.0
    for r in sorted(f, key=lambda r: r["t"]):
        eq += r["o"]["net"]; peak = max(peak, eq); dd = min(dd, eq - peak)
    mean = statistics.mean(nets) if nets else float("nan")
    se = (statistics.stdev(nets) / math.sqrt(len(nets))) if len(nets) > 1 else float("nan")
    mk = [w for w in {r["slug"] for r in rows}]
    return {"decisions": len(rows), "fills": len(f), "fill_rate": len(f) / len(rows) if rows else float("nan"),
            "net": sum(nets), "per_fill": mean, "t": mean / se if se and se > 0 else float("nan"),
            "win": (sum(1 for x in nets if x > 0) / len(nets)) if nets else float("nan"),
            "avg_entry": statistics.mean(r["o"]["fill"] for r in f) if f else float("nan"),
            "fees": sum(r["o"]["fee"] for r in f), "dd": dd, "worst": min(nets) if nets else float("nan"),
            "markets": len(mk), "per_market": sum(nets) / len(mk) if mk else float("nan"),
            "slip": statistics.mean((r["o"]["fill"] - r["ask"]) * 100.0 for r in f) if f else float("nan")}


def fmt(s):
    g = lambda v, f="{:+.2f}": "—" if isinstance(v, float) and math.isnan(v) else f.format(v)   # noqa: E731
    return (f"{s['decisions']} | {s['fills']} ({g(s['fill_rate'], '{:.0%}')}) | {g(s['net'])} | {g(s['per_fill'], '{:+.3f}')} | "
            f"{g(s['per_market'], '{:+.3f}')} | {g(s['win'], '{:.0%}')} | {g(s['avg_entry'], '{:.3f}')} | {g(s['slip'], '{:+.1f}')}c | "
            f"{g(s['fees'])} | {g(s['dd'])} | {g(s['worst'])} | {g(s['t'], '{:+.1f}')}")


HEAD = "| rule | split | decisions | fills (rate) | net $ | per fill | per market | win | avg entry | slip | fees | max DD | worst | t |"
SEP = "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"


def main():
    wins = [cr.W(*x) for x in cr.load()]
    n = len(wins)
    rows = []
    prev_sigma = None
    for k, w in enumerate(wins):
        sig = window_sigma(w)
        strike = 0.0
        try:
            strike = float(json.load(open(f"{cr.ROOT}/data/charts/{w.slug}.json")).get("strike") or 0.0)
        except Exception:
            pass
        if prev_sigma is not None:
            split = next(s for s, lo, hi in cr.SPLITS if int(lo * n) <= k < int(hi * n))
            for t, side, ask, feats in candidates(w, prev_sigma, strike):
                o = outcome(w, t, side, ask)
                if o is None:
                    continue
                rows.append({"slug": w.slug, "t": t, "side": side, "ask": ask, "f": feats, "o": o, "split": split,
                             "left": (w.close - t) / 1000.0})
        if sig:
            prev_sigma = sig
    print("# Triangle entry model: the buy point from the data\n")
    print(f"{n} BTC windows, {len(rows)} candidate moments ({sum(1 for r in rows if r['f']['hz'])} high-zone, "
          f"{sum(1 for r in rows if not r['f']['hz'])} spot-move); splits " + ", ".join(f"{s} {sum(1 for r in rows if r['split'] == s)}" for s, _, _ in cr.SPLITS) + ".\n")
    # ---- baselines: today's triggers
    print("## 1. Baselines (today's rules, leg 1 held to settlement, no hedge)\n")
    print(HEAD); print(SEP)
    base3 = [r for r in rows if not r["f"]["hz"] and r["f"]["z1"] >= 3.0]
    basehz = [r for r in rows if r["f"]["hz"] and r["left"] >= 120]
    for name, sub in (("spot shock >= 3 sigma", base3), ("high zone (>= 120 s left)", basehz), ("both (today's bot)", base3 + basehz)):
        for s, _, _ in cr.SPLITS:
            print(f"| {name} | {s} | " + fmt(stats([r for r in sub if r["split"] == s])) + " |")
    # ---- the model
    tr = [r for r in rows if r["split"] == "train" and r["o"]["filled"]]
    X = np.array([[r["f"][k] for k in FEATS] for r in tr]); y = np.array([1.0 if r["o"]["won"] else 0.0 for r in tr])
    model = fit_logistic(X, y)
    print("\n## 2. The entry model (logistic P(win), fitted on TRAIN fills only)\n")
    print("| feature | standardised coefficient |"); print("|---|---:|")
    for k, c in zip(("intercept",) + FEATS, model["w"]):
        print(f"| {k} | {c:+.3f} |")
    for r in rows:
        p = float(predict(model, np.array([[r["f"][k] for k in FEATS]]))[0])
        exp_fill = min(r["ask"] + 0.01, BAND_TOP)
        r["p"] = p; r["ev"] = p - exp_fill - FEE(exp_fill)
    # ---- threshold + small grid on VALIDATION
    print("\n## 3. Threshold and grid on VALIDATION only (net $ / fills)\n")
    best = None
    print("| min EV | max price | min left | validation net | fills | train net | fills |"); print("|---:|---:|---:|---:|---:|---:|---:|")
    for ev_min in (-0.05, 0.0, 0.02, 0.04, 0.06, 0.08):
        for maxp in (0.70, 0.85, 0.96):
            for minleft in (60, 120):
                sel = lambda r: r["ev"] >= ev_min and r["ask"] <= maxp and r["left"] >= minleft   # noqa: E731
                v = stats([r for r in rows if r["split"] == "validation" and sel(r)])
                tr_s = stats([r for r in rows if r["split"] == "train" and sel(r)])
                print(f"| {ev_min:+.2f} | {maxp} | {minleft} | {v['net']:+.1f} | {v['fills']} | {tr_s['net']:+.1f} | {tr_s['fills']} |")
                if v["fills"] >= 60 and tr_s["net"] > 0 and (best is None or v["net"] > best[0]):
                    best = (v["net"], ev_min, maxp, minleft)
    ev_min, maxp, minleft = best[1:] if best else (0.0, 0.96, 60)
    json.dump({"features": FEATS, "model": model, "ev_min": ev_min, "max_price": maxp, "min_left_s": minleft,
               "rule": "max validation net with >= 60 fills and positive train net"}, open(LOCK, "w"), indent=1)
    print(f"\nChosen on validation (locked to {LOCK.split('/')[-1]}): min EV {ev_min:+.2f}, max price {maxp}, min left {minleft} s\n")
    print("## 4. The chosen entry rule, every split (TEST / FORWARD untouched)\n")
    print(HEAD); print(SEP)
    chosen = [r for r in rows if r["ev"] >= ev_min and r["ask"] <= maxp and r["left"] >= minleft]
    for s, _, _ in cr.SPLITS:
        print(f"| entry model | {s} | " + fmt(stats([r for r in chosen if r["split"] == s])) + " |")
    # ---- by time left and price zone (TEST + FORWARD)
    print("\n## 5. Chosen rule by seconds left and by price zone (TEST + FORWARD fills)\n")
    oos = [r for r in chosen if r["split"] in ("test", "forward") and r["o"]["filled"]]
    print("| bucket | fills | net $ | per fill | win |"); print("|---|---:|---:|---:|---:|")
    for lab, lo, hi in (("240-300 s", 240, 301), ("180-240 s", 180, 240), ("120-180 s", 120, 180), ("60-120 s", 60, 120)):
        sub = [r for r in oos if lo <= r["left"] < hi]
        if sub:
            s = stats(sub); print(f"| {lab} | {s['fills']} | {s['net']:+.1f} | {s['per_fill']:+.3f} | {s['win']:.0%} |")
    for lab, lo, hi in (("0.20-0.45", 0.2, 0.45), ("0.45-0.55", 0.45, 0.55), ("0.55-0.70", 0.55, 0.70), ("0.70-0.85", 0.70, 0.85), ("0.85-0.96", 0.85, 0.97)):
        sub = [r for r in oos if lo <= r["o"]["fill"] < hi]
        if sub:
            s = stats(sub); print(f"| price {lab} | {s['fills']} | {s['net']:+.1f} | {s['per_fill']:+.3f} | {s['win']:.0%} |")
    for lab, f in (("trigger: spot move", lambda r: not r["f"]["hz"]), ("trigger: high zone", lambda r: r["f"]["hz"])):
        sub = [r for r in oos if f(r)]
        if sub:
            s = stats(sub); print(f"| {lab} | {s['fills']} | {s['net']:+.1f} | {s['per_fill']:+.3f} | {s['win']:.0%} |")
    # ---- the hedge EV rule on the chosen fills
    print("\n## 6. Second leg by expected value (chosen fills; hedge FOK at the other ask 800 ms after the fill is known)\n")
    print("| margin | split | fills | hedged | pair cost | net with hedge $ | net without $ | delta |"); print("|---:|---|---:|---:|---:|---:|---:|---:|")
    for margin in (0.0, 0.02, 0.05):
        for s, _, _ in cr.SPLITS:
            sub = [r for r in chosen if r["split"] == s and r["o"]["filled"]]
            hedged, pc, net_h, net_0 = 0, [], 0.0, 0.0
            for r in sub:
                o = r["o"]; net_0 += o["net"]
                oa = o["other_ask_1600"]
                if oa is not None and oa <= BAND_TOP and 1.0 - oa - FEE(oa) >= r["p"] + margin:
                    hedged += 1; pc.append(o["fill"] + oa)
                    net_h += SHARES - SHARES * o["fill"] - SHARES * oa - SHARES * FEE(o["fill"]) - SHARES * FEE(oa)
                else:
                    net_h += o["net"]
            print(f"| {margin:.2f} | {s} | {len(sub)} | {hedged} | {statistics.mean(pc) if pc else float('nan'):.3f} | {net_h:+.1f} | {net_0:+.1f} | {net_h - net_0:+.1f} |")


if __name__ == "__main__":
    main()
