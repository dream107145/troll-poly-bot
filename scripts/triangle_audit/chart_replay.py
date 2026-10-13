"""Six-day replay on data/charts/btc-updown-5m-*.json (200 ms samples of the composite spot and both books):
the HIGH-ZONE trigger (ask > 0.85, higher than 5 s ago, not falling over 1 s, one buy per 5 s) and, as a
trigger-1 proxy, the 1-second spot shock (>= 3 sigma, the rule the bot ran until 14:36 UTC), each with three
hedge models and a max_pair_cost gate sweep. Chronological splits: train 50 % / validation 20 % / test 15 % /
forward 15 % of the windows, in time order.

Execution: FOK at the ask seen at the decision + 3c (capped at the band top 0.96), landing 800 ms later, filled
if the ask then is <= the limit; fee 7 % x p(1-p) per taker share; 5 shares; the venue's resting-order model
(a resting buy fills only when the ask trades strictly BELOW it, maker fee 0); settled at the window's outcome.
Hedge models:  R  resting buy of the other side at pair_cost - fill (the bot today)
               Eseq  after the fill is known (+800 ms) a FOK of the other side at its ask + 3c, landing +1600 ms
               Esim  both legs sent at the decision as FOKs, both landing +800 ms
Gate (E models): enter only if ask_side + ask_other + fee(both) + slippage(0.03) <= max_pair_cost at the decision.
"""
from __future__ import annotations

import bisect
import glob
import json
import math
import statistics
import sys
from collections import defaultdict

ROOT = "/root/troll-poly-bot"
FEE = 0.07
LAT = 800.0
LIMIT_OFF = 0.03
BAND = (0.20, 0.96)
SPLITS = (("train", 0.0, 0.5), ("validation", 0.5, 0.7), ("test", 0.7, 0.85), ("forward", 0.85, 1.0))
PAIR_COSTS = (0.93, 0.94, 0.95, 0.96, 0.97)


def fee(p):
    return FEE * p * (1 - p)


def load():
    out = []
    for f in sorted(glob.glob(f"{ROOT}/data/charts/btc-updown-5m-*.json")):
        d = json.load(open(f))
        pts = [p for p in d.get("points") or [] if p.get("spot") and p.get("ua") is not None and p.get("da") is not None]
        if len(pts) < 600 or (d.get("outcome") or "").upper() not in ("UP", "DOWN"):
            continue
        out.append((d["slug"], d["outcome"].upper(), d["close_ts"], pts))
    return out


class W:
    def __init__(self, slug, outcome, close, pts):
        self.slug, self.outcome, self.close, self.pts = slug, outcome, close, pts
        self.t = [p["t"] for p in pts]

    def ask(self, side, t):
        i = bisect.bisect_left(self.t, t)
        if i >= len(self.pts):
            return None
        return self.pts[i].get("ua" if side == "UP" else "da")

    def ask_before(self, side, t):
        i = bisect.bisect_right(self.t, t) - 1
        return self.pts[i].get("ua" if side == "UP" else "da") if i >= 0 else None

    def min_ask_after(self, side, t):
        i = bisect.bisect_left(self.t, t)
        v = [p.get("ua" if side == "UP" else "da") for p in self.pts[i:] if p.get("left", 1) > 0]
        v = [x for x in v if x is not None]
        return min(v) if v else None


def high_zone(w: W, hz_min=0.85, every=5000.0, look=5000.0):
    """(t, side, ask) decisions of the high-zone rule."""
    out, last = [], -1e18
    for p in w.pts:
        t = p["t"]
        if p.get("left") is None or p["left"] < 10.0 or t - last < every:
            continue
        for side in ("UP", "DOWN"):
            a = p.get("ua" if side == "UP" else "da")
            if a is None or a <= hz_min:
                continue
            then, one = w.ask_before(side, t - look), w.ask_before(side, t - 1000.0)
            if then is None or w.t[0] > t - look:
                continue
            if a > then + 1e-9 and (one is None or a >= one - 1e-9):
                out.append((t, side, a))
                last = t
                break
    return out


def spot_shock(w: W, k=3.0):
    """Trigger-1 proxy: the 1 s composite spot move >= k x the window's realised 1 s sigma (previous window's
    sigma is not available here: a mild look-ahead on the scale only), one per direction per second, >= 60 s left."""
    lp = [math.log(p["spot"]) * 1e4 for p in w.pts]
    r = []
    for i in range(0, len(w.pts), 5):
        j = bisect.bisect_right(w.t, w.t[i] - 1000.0) - 1
        if j >= 0 and w.t[i] - w.t[j] < 1300:
            r.append((lp[i] - lp[j]) / math.sqrt((w.t[i] - w.t[j]) / 1000.0))
    if len(r) < 30:
        return []
    sig = statistics.pstdev(r) or 1e-9
    out, last = [], {1: -1e18, -1: -1e18}
    for i, p in enumerate(w.pts):
        t = p["t"]
        if p.get("left") is None or p["left"] < 60.0:
            continue
        j = bisect.bisect_right(w.t, t - 1000.0) - 1
        if j < 0 or t - w.t[j] > 1300:
            continue
        z = (lp[i] - lp[j]) / sig
        if abs(z) < k:
            continue
        d = 1 if z > 0 else -1
        if t - last[d] < 1000.0:
            continue
        last[d] = t
        side = "UP" if d > 0 else "DOWN"
        a = p.get("ua" if side == "UP" else "da")
        if a is not None:
            out.append((t, side, a))
    return out


def simulate(w: W, decisions, pair_cost, model, gate=None):
    """Per decision: dict(leg1 fill, hedge fill, pnl, fees, ...)."""
    rows = []
    for t, side, a0 in decisions:
        other = "DOWN" if side == "UP" else "UP"
        if not (BAND[0] <= a0 <= BAND[1]):
            continue
        room = pair_cost - 0.01 if model == "R" else 1.0
        limit = min(a0 + LIMIT_OFF, BAND[1], room)
        if a0 > limit + 1e-9:
            continue
        o0 = w.ask_before(other, t)
        if gate is not None:
            if o0 is None or a0 + o0 + fee(a0) + fee(o0) + 0.03 > gate + 1e-9:
                rows.append({"gated": True})
                continue
        a1 = w.ask(side, t + LAT)
        row = {"gated": False, "t": t, "side": side, "ask0": a0, "filled": False, "hedge": None}
        if a1 is None or a1 > limit + 1e-9:
            rows.append(row)
            continue
        won = w.outcome == side
        row.update(filled=True, fill=a1, leg1=(5.0 if won else 0.0) - 5.0 * a1 - 5.0 * fee(a1), fee1=5.0 * fee(a1))
        if model == "R":
            hp = math.floor((pair_cost - a1) * 100 + 1e-6) / 100.0
            if hp >= 0.01:
                mn = w.min_ask_after(other, t + LAT)
                if mn is not None and mn < hp - 1e-9:
                    row["hedge"] = {"price": hp, "fee": 0.0, "pnl": (0.0 if won else 5.0) - 5.0 * hp}
        elif model == "Eseq":
            o1 = w.ask(other, t + LAT)
            o2 = w.ask(other, t + 2 * LAT)
            if o1 is not None and o2 is not None and o2 <= min(o1 + LIMIT_OFF, BAND[1]) + 1e-9:
                row["hedge"] = {"price": o2, "fee": 5.0 * fee(o2), "pnl": (0.0 if won else 5.0) - 5.0 * o2 - 5.0 * fee(o2)}
        elif model == "Esim":
            o1 = w.ask(other, t + LAT)
            if o0 is not None and o1 is not None and o1 <= min(o0 + LIMIT_OFF, BAND[1]) + 1e-9:
                row["hedge"] = {"price": o1, "fee": 5.0 * fee(o1), "pnl": (0.0 if won else 5.0) - 5.0 * o1 - 5.0 * fee(o1)}
        rows.append(row)
    return rows


def summarize(rows):
    dec = [r for r in rows if not r.get("gated")]
    gated = sum(1 for r in rows if r.get("gated"))
    f = [r for r in dec if r["filled"]]
    comp = [r for r in f if r["hedge"]]
    naked = [r for r in f if not r["hedge"]]
    net = sum(r["leg1"] for r in f) + sum(r["hedge"]["pnl"] for r in comp)
    fees = sum(r["fee1"] for r in f) + sum(r["hedge"]["fee"] for r in comp)
    eq = peak = dd = 0.0
    for r in sorted(f, key=lambda r: r["t"]):
        eq += r["leg1"] + (r["hedge"]["pnl"] if r["hedge"] else 0.0)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    wins = sum(1 for r in f if r["leg1"] + (r["hedge"]["pnl"] if r["hedge"] else 0.0) > 0)
    return {"decisions": len(dec), "gated": gated, "fills": len(f), "completed": len(comp), "naked": len(naked),
            "pair_cost": statistics.mean(r["fill"] + r["hedge"]["price"] for r in comp) if comp else float("nan"),
            "fees": fees, "net": net, "per_fill": net / len(f) if f else float("nan"),
            "comp_pnl": sum(r["leg1"] + r["hedge"]["pnl"] for r in comp), "naked_pnl": sum(r["leg1"] for r in naked),
            "win": wins / len(f) if f else float("nan"), "dd": dd}


def line(label, s):
    g = lambda v, f="{:+.2f}": "-" if isinstance(v, float) and math.isnan(v) else f.format(v)   # noqa: E731
    return (f"{label:30} dec {s['decisions']:5} gated {s['gated']:5} fills {s['fills']:5} completed {s['completed']:5} naked {s['naked']:5} "
            f"pair {g(s['pair_cost'], '{:.3f}')} fees {g(s['fees'])} | NET {g(s['net'])} ({g(s['per_fill'], '{:+.3f}')}/fill) "
            f"completed {g(s['comp_pnl'])} naked {g(s['naked_pnl'])} win {g(s['win'], '{:.0%}')} maxDD {g(s['dd'])}")


def main():
    wins = [W(*x) for x in load()]
    n = len(wins)
    print(f"{n} BTC windows with an outcome, {wins[0].slug[-10:]} .. {wins[-1].slug[-10:]}")
    for trig_name, trig in (("HIGH ZONE (trigger 2)", high_zone), ("SPOT SHOCK 3-sigma (trigger-1 proxy)", spot_shock)):
        print(f"\n===== {trig_name} =====")
        decs = {w.slug: trig(w) for w in wins}
        print("decisions per window:", round(sum(len(v) for v in decs.values()) / n, 2))
        for sname, lo, hi in SPLITS:
            ws = wins[int(lo * n):int(hi * n)]
            print(f"\n-- {sname}: {len(ws)} windows ({ws[0].slug[-10:]} .. {ws[-1].slug[-10:]})")
            rows = [r for w in ws for r in simulate(w, decs[w.slug], 1.0, "none")]
            print(line("leg 1 only, no hedge", summarize(rows)))
            for pc in (0.95, 0.96):
                rows = [r for w in ws for r in simulate(w, decs[w.slug], pc, "R")]
                print(line(f"R resting hedge, pair {pc:.2f}", summarize(rows)))
            for model in ("Eseq", "Esim"):
                for pc in PAIR_COSTS:
                    rows = [r for w in ws for r in simulate(w, decs[w.slug], pc, model, gate=pc)]
                    print(line(f"{model} gate max_pair {pc:.2f}", summarize(rows)))


if __name__ == "__main__":
    main()
