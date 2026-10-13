"""How to get over the resting pair's risk (2026-10-01, the user's question after the "sum under 0.95" rule).

Replays the bot's CURRENT rules on data/charts BTC windows (200 ms): entry-model candidates (lock
data/replay/triangle_entry_locked.json, EV >= 0.02, >= 60 s left), buy band 0.25-0.85, FOK 5 shares at ask + 4c,
CALIBRATED fills (ask inside the limit at +250 ms and at +1250 ms, charged the worse), one order in flight
per market, 50 USDC per market. After each fill (known at +1250 ms) a resting BUY of the other side at the
highest tick with fill + pair < TARGET, live from +1500 ms; it fills when the other ask trades BELOW it
(the paper venue's rule). Variants that act on a leg whose pair has not filled:
    stop X   : the leg's bid has fallen X below the fill -> sell it at the bid (taker fee), cancel the pair
    chase X  : same trigger -> buy the other side at its ask (taker fee): a locked pair at a small loss
    wait     : no new leg in the window while any pair is unfilled (one naked leg at a time)
    target T : pair price from fill + pair < T (0.95 now; 0.97 / 0.99 fill more often, lock less)
Chronological splits TRAIN 50 / VALIDATION 20 / TEST 15 / FORWARD 15 (chart_replay.SPLITS); X chosen on
VALIDATION only.
"""
from __future__ import annotations

import json
import math
import sys

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/triangle_audit")
import chart_replay as cr   # noqa: E402
import entry_model as em    # noqa: E402

LOCK = json.load(open("/root/troll-poly-bot/data/replay/triangle_entry_locked.json"))
FEATS, MODEL = LOCK["features"], LOCK["model"]
MIN_EV, MIN_LEFT, LO, HI, OFF, SH, CAP = 0.02, 60.0, 0.25, 0.85, 0.04, 5.0, 50.0
LAND, SURV, PAIR_LIVE, ACT = 250.0, 1250.0, 1500.0, 250.0
FEE = cr.fee


class Win:
    def __init__(self, w):
        self.w = w
        P = w.pts
        self.t = np.array([p["t"] for p in P], dtype=float)
        g = lambda k: np.array([np.nan if p.get(k) is None else p[k] for p in P], dtype=float)   # noqa: E731
        self.ask = {"UP": g("ua"), "DOWN": g("da")}
        self.bid = {"UP": g("ub"), "DOWN": g("db")}
        self.left = g("left")

    def at(self, arr, t):
        i = int(np.searchsorted(self.t, t, side="right")) - 1
        return None if i < 0 or math.isnan(arr[i]) else float(arr[i])

    def first(self, mask, t):
        i0 = int(np.searchsorted(self.t, t, side="left"))
        m = mask[i0:] & (self.left[i0:] > 0)
        k = int(np.argmax(m)) if m.size else 0
        return None if not m.size or not m[k] else i0 + k


def legs_for(win, cands):
    """The window's leg fills under the entry rule, in time order, one order in flight, the cap on legs."""
    out, busy, spent = [], -1e18, 0.0
    for t, side, ask in cands:
        if t < busy:
            continue
        lim = round(min(ask + OFF, HI + OFF), 4)
        a1, a2 = win.at(win.ask[side], t + LAND), win.at(win.ask[side], t + SURV)
        busy = t + SURV
        if a1 is None or a2 is None or a1 > lim + 1e-9 or a2 > lim + 1e-9:
            continue
        px = max(a1, a2)
        if spent + SH * px > CAP:
            break
        spent += SH * px
        out.append((t, side, px))
    return out


def simulate(win, cands, target=0.95, mode="rest", x=None, wait=False):
    """Net USDC of one window under one hedge variant."""
    w = win.w
    won = w.outcome
    net, busy, spent, working = 0.0, -1e18, 0.0, []     # working: (pair_fill_time or inf)
    for t, side, ask in cands:
        if t < busy:
            continue
        if wait and any(pt > t for pt in working):
            continue
        lim = round(min(ask + OFF, HI + OFF), 4)
        a1, a2 = win.at(win.ask[side], t + LAND), win.at(win.ask[side], t + SURV)
        busy = t + SURV
        if a1 is None or a2 is None or a1 > lim + 1e-9 or a2 > lim + 1e-9:
            continue
        px = max(a1, a2)
        if spent + SH * px > CAP:
            break
        spent += SH * px
        leg = -SH * px - SH * FEE(px)
        other = "DOWN" if side == "UP" else "UP"
        if mode == "none":
            net += leg + (SH if won == side else 0.0)
            continue
        pp = math.floor((target - px - 1e-9) / 0.01) * 0.01
        t_pair = None
        if pp >= 0.01 - 1e-9:
            i = win.first(win.ask[other] < pp - 1e-9, t + PAIR_LIVE)
            t_pair = None if i is None else win.t[i]
        t_hit = None
        if mode in ("stop", "chase"):
            i = win.first(win.bid[side] <= px - x + 1e-9, t + SURV)
            t_hit = None if i is None else win.t[i]
        if t_pair is not None and (t_hit is None or t_pair <= t_hit):
            net += leg - SH * pp + SH                         # locked: pays 1 whichever side wins
            working.append(t_pair)
            continue
        if t_hit is not None:
            if mode == "stop":
                b = win.at(win.bid[side], t_hit + ACT)
                if b is not None and b > 0:
                    net += leg + SH * b - SH * FEE(b)
                    working.append(t_hit)
                    continue
            else:
                oa = win.at(win.ask[other], t_hit + ACT)
                if oa is not None and oa <= 0.99:
                    net += leg - SH * oa - SH * FEE(oa) + SH
                    working.append(t_hit)
                    continue
        net += leg + (SH if won == side else 0.0)              # naked to settlement
        working.append(float("inf"))
    return net


def main():
    wins = [cr.W(*x) for x in cr.load()]
    n = len(wins)
    prev, data = None, []
    for k, w in enumerate(wins):
        sig = em.window_sigma(w)
        strike = 0.0
        try:
            strike = float(json.load(open(f"{cr.ROOT}/data/charts/{w.slug}.json")).get("strike") or 0.0)
        except Exception:
            pass
        if prev is not None and w.outcome in ("UP", "DOWN"):
            split = next(s for s, lo, hi in cr.SPLITS if int(lo * n) <= k < int(hi * n))
            cands = []
            for t, side, ask, f in em.candidates(w, prev, strike):
                if f["hz"] or not (LO <= ask <= HI) or (w.close - t) / 1000.0 < MIN_LEFT:
                    continue
                p = float(em.predict(MODEL, np.array([[f[q] for q in FEATS]]))[0])
                e = min(ask + 0.01, 0.96)
                if p - e - FEE(e) >= MIN_EV:
                    cands.append((t, side, ask))
            if cands:
                data.append((split, Win(w), sorted(cands)))
        if sig:
            prev = sig
    variants = [("leg only, no pair", dict(mode="none")), ("pair < 0.95 (now)", dict(mode="rest"))]
    variants += [(f"pair < {T:.2f}", dict(mode="rest", target=T)) for T in (0.97, 0.99)]
    variants += [("pair < 0.95 + one naked leg at a time", dict(mode="rest", wait=True))]
    variants += [(f"pair < 0.95 + stop {x:.2f}", dict(mode="stop", x=x)) for x in (0.10, 0.15, 0.20, 0.30)]
    variants += [(f"pair < 0.95 + chase {x:.2f}", dict(mode="chase", x=x)) for x in (0.10, 0.15, 0.20, 0.30)]
    variants += [(f"pair < 0.95 + wait + stop {x:.2f}", dict(mode="stop", x=x, wait=True)) for x in (0.10, 0.20)]
    print("# Resting-pair risk: variants on the bot's current rules\n")
    print(f"{len(data)} traded BTC windows (of {n}); per-window net USDC\n")
    print("| variant | split | windows | net $ | per window | losing windows | worst window | 5 % worst avg |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    res = {}
    for name, kw in variants:
        for s, _, _ in cr.SPLITS:
            v = np.array([simulate(win, c, **kw) for sp, win, c in data if sp == s])
            res[(name, s)] = v
            q = np.sort(v)[: max(1, len(v) // 20)]
            print(f"| {name} | {s} | {len(v)} | {v.sum():+.1f} | {v.mean():+.3f} | {np.mean(v < 0):.0%} | {v.min():+.2f} | {q.mean():+.2f} |")
    json.dump({f"{a}|{b}": [float(x) for x in v] for (a, b), v in res.items()},
              open("/root/troll-poly-bot/results/pair_risk_windows.json", "w"))


if __name__ == "__main__":
    main()
