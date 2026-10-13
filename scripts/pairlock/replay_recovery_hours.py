"""Which GMT+9 hours does the pair-lock RECOVERY bot (strategy R, open rule) make money in? (2026-10-06)

The rules as the Settings page runs them on 2026-10-06 21:00 UTC (data/controls.json "all" + the app's env):
  open buy      2..20 s after the open, buy 10 shares of the token with the higher ask (UP when equal) if that ask is
                in [OPEN_MIN, OPEN_MAX]; limit ask + 0.02; up to 3 tries. No other-side bid.
  recovery      the side with the higher mid is S; when the held other side O has fallen >= 0.05 under its cost, S's
                ask is above 0.70 and the S-win branch is under +0.50: buy S up to +0.50 on that branch
                (x = (0.50 - branch) / (1 - ask - fee - 0.005)), steps of at most 60 sh, 2 s apart, limit ask + 0.03,
                recovery capital 90, position 300. At most MAX_REC recoveries a market (a recovery = buys toward one
                side while it stays the higher one); after the last one the market is held.
  lock          instead of a recovery when < 60 s left, S's ask > 0.90, or the plan would leave a branch under -35:
                buy the side with fewer shares until UP = DOWN.
  profit exit   after a recovery or lock fill, sell everything (IOC at the bids) once bids - exit fees - cost >= 1.00.
  nothing under 15 s left. The model-gated late buy (P >= 0.95 and an edge) is NOT replayed (no model here; rare).
Fills (the paper bots' calibrated venue, as scripts/open_buy_sim.py): an order decided at t lands at t + 250 ms and
fills only if the ask stays <= the limit until landing + 1 s, at the worse of the two asks; sells at the bid at
landing (limit bid - 0.03). Taker fee 0.07 p (1 - p) a share on every buy and sell. Bankroll 100 a market.
Data: every recorded btc-updown-5m window (data/charts/*.json, ~240 ms points).

    .venv/bin/python scripts/pairlock/replay_recovery_hours.py > results/pairlock_recovery_hours_2026-10-06.md
"""
from __future__ import annotations

import glob
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = "/root/troll-poly-bot"
CACHE = f"{ROOT}/data/replay/hourly_swings/btc_windows.npz"
TZ = 9 * 3600 * 1000
LAT, SURV = 250.0, 1000.0
EPS = 1e-9


def fee(p):
    return 0.0 if p <= 0.0 or p >= 1.0 else 0.07 * p * (1.0 - p)


# ───────────────────────── data ─────────────────────────

def _one(path):
    try:
        d = json.load(open(path))
    except Exception:  # noqa: BLE001
        return None
    out = (d.get("outcome") or "").upper()
    pts = [p for p in d.get("points") or [] if p.get("t")]
    if out not in ("UP", "DOWN") or len(pts) < 600:
        return None
    g = lambda p, k: np.nan if p.get(k) is None else float(p[k])  # noqa: E731
    a = np.array([[p["t"], g(p, "ua"), g(p, "ub"), g(p, "da"), g(p, "db")] for p in pts], dtype=np.float64)
    return d["slug"], float(d["open_ts"]), float(d["close_ts"]), out, a


def load():
    files = sorted(glob.glob(f"{ROOT}/data/charts/btc-updown-5m-*.json"))
    if os.path.exists(CACHE):
        z = np.load(CACHE, allow_pickle=False)
        meta = json.loads(str(z["meta"]))
        if meta["n_files"] == len(files):
            off, data = z["off"], z["data"]
            return [(m[0], m[1], m[2], m[3], data[off[i]:off[i + 1]]) for i, m in enumerate(meta["w"])]
    with ProcessPoolExecutor(4) as ex:
        ws = [w for w in ex.map(_one, files, chunksize=16) if w]
    off = np.cumsum([0] + [len(w[4]) for w in ws])
    np.savez(CACHE, data=np.vstack([w[4] for w in ws]), off=off,
             meta=np.array(json.dumps({"n_files": len(files), "w": [w[:4] for w in ws]})))
    return ws


# ───────────────────────── one market ─────────────────────────

class Book:
    def __init__(self):
        self.q = {"UP": 0.0, "DOWN": 0.0}
        self.cost = {"UP": 0.0, "DOWN": 0.0}   # incl. fees
        self.realised = 0.0

    def total(self):
        return self.cost["UP"] + self.cost["DOWN"]

    def branch(self, s):
        return self.q[s] - self.total() + self.realised

    def avg(self, s):
        return self.cost[s] / self.q[s] if self.q[s] > EPS else None

    def buy(self, s, q, px):
        self.q[s] += q
        self.cost[s] += q * (px + fee(px))


def mid(b, a):
    if not math.isnan(b) and not math.isnan(a):
        return 0.5 * (a + b)
    return a if not math.isnan(a) else (b if not math.isnan(b) else None)


def simulate(w, P):
    slug, t_open, t_close, outcome, A = w
    T, UA, UB, DA, DB = A[:, 0], A[:, 1], A[:, 2], A[:, 3], A[:, 4]
    n = len(T)
    ask = {"UP": UA, "DOWN": DA}
    bid = {"UP": UB, "DOWN": DB}
    OTHER = {"UP": "DOWN", "DOWN": "UP"}

    def at(t):
        return max(int(np.searchsorted(T, t, side="right")) - 1, 0)

    def taker_buy(s, t, limit):
        i0, i1 = at(t + LAT), at(t + LAT + SURV)
        seg = ask[s][i0:i1 + 1]
        if seg.size == 0 or np.isnan(seg).any() or seg.max() > limit + EPS:
            return None
        return min(max(float(ask[s][i0]), float(ask[s][i1])), limit)

    b = Book()
    r = {"slug": slug, "open": t_open, "entered": False, "recoveries": 0, "rec_buys": 0, "locked": False,
         "lock_buys": 0, "exit": False, "pnl": 0.0}
    tries, next_t = 0, t_open
    i = int(np.searchsorted(T, t_open + P["open_delay"] * 1000))
    # the open buy
    while i < n and T[i] <= t_open + P["open_window"] * 1000 and tries < 3:
        if T[i] < next_t:
            i += 1
            continue
        ua, da = UA[i], DA[i]
        if math.isnan(ua) or math.isnan(da):
            i += 1
            continue
        s = "UP" if ua >= da else "DOWN"
        a = ask[s][i]
        if not (P["open_min"] - EPS <= a <= P["open_max"] + EPS):
            i += 1
            continue
        tries += 1
        px = taker_buy(s, T[i], min(0.99, round(a + 0.02, 2)))
        next_t = T[i] + LAT + SURV
        if px is not None:
            b.buy(s, P["shares"], px)
            r.update(entered=True, side=s, price=px)
            break
        i += 1
    if not r["entered"]:
        return r
    i = int(np.searchsorted(T, next_t))
    rec_open, rec_side, episodes, lock_steps, locked = False, None, 0, 0, False
    last_act, busy, rec_cost, actions, acted = -1e18, -1e18, 0.0, 0, False
    while i < n:
        t = T[i]
        left = (t_close - t) / 1000.0
        if left <= 0:
            break
        if t - last_act < 2000.0:
            i += 1
            continue
        # profit exit (only after a recovery / lock fill)
        if acted and P["profit_exit"] > 0:
            val = fees = 0.0
            for s in ("UP", "DOWN"):
                bb = bid[s][i]
                if b.q[s] > EPS and not math.isnan(bb) and bb > 0.01 + EPS:
                    val += b.q[s] * bb
                    fees += b.q[s] * fee(bb)
            if val - fees - b.total() + b.realised >= P["profit_exit"] - EPS:
                j = at(t + LAT)
                for s in ("UP", "DOWN"):
                    bb = bid[s][j]
                    if b.q[s] > EPS and not math.isnan(bb) and bb > 0.01 + EPS and bb >= bid[s][i] - 0.03 - EPS:
                        b.realised += b.q[s] * (bb - fee(bb))
                        b.q[s] = 0.0
                if b.q["UP"] < EPS and b.q["DOWN"] < EPS:
                    r.update(exit=True, pnl=b.realised - b.total())
                    return r
                last_act = t
                i += 1
                continue
        if locked or left < 15.0:
            i += 1
            continue
        mn = min(b.branch("UP"), b.branch("DOWN"))
        if mn >= 0.5 - EPS:
            i += 1
            continue
        order = None
        if lock_steps > 0:
            order = "lock"
        else:
            mu, md = mid(UB[i], UA[i]), mid(DB[i], DA[i])
            if mu is None or md is None or abs(mu - md) < EPS:
                i += 1
                continue
            S = "UP" if mu > md else "DOWN"
            O = OTHER[S]
            if rec_open and S != rec_side:
                rec_open = False
            bo, ao = bid[O][i], b.avg(O)
            if b.q[O] <= EPS or math.isnan(bo) or ao is None or ao - bo < 0.05 - EPS:
                i += 1
                continue
            br = b.branch(S)
            if br >= 0.5 - EPS:
                if rec_open and S == rec_side:
                    rec_open = False
                i += 1
                continue
            best = ask[S][i]
            if math.isnan(best) or best <= 0.70 + EPS:
                i += 1
                continue
            if P["max_rec"] > 0 and not (rec_open and S == rec_side) and episodes >= P["max_rec"]:
                i += 1                       # held
                continue
            pps = 1.0 - best - fee(best) - 0.005
            q_req = None if pps <= EPS else math.ceil((0.5 - br) / pps * 100.0) / 100.0
            why_lock = ""
            if actions >= 30 or left < 60.0 or best > 0.90 + EPS or mn < -35 - EPS or q_req is None:
                why_lock = "lock"
            else:
                tc = b.total() + q_req * (best + fee(best) + 0.005)
                other = b.q[O] - tc + b.realised
                if other < -35 - EPS:
                    why_lock = "lock"
            order = why_lock or "rec"
        if order == "lock":
            gap = b.q["UP"] - b.q["DOWN"]
            side = "DOWN" if gap > 0 else "UP"
            need = math.floor(abs(gap) * 100 + 1e-6) / 100
            if need < 5.0 - EPS:
                locked = True
                r["locked"] = True
                i += 1
                continue
            best = ask[side][i]
            if math.isnan(best):
                i += 1
                continue
            limit = min(0.99, round(best + 0.03, 2))
            q = min(need, 60.0, math.floor(100.0 / (limit + fee(limit) + 0.005) * 100) / 100)
            px = taker_buy(side, t, limit)
            last_act, busy, actions = t, t + LAT + SURV, actions + 1
            if px is not None:
                b.buy(side, q, px)
                lock_steps += 1
                r["lock_buys"] += 1
                acted = True
                if abs(b.q["UP"] - b.q["DOWN"]) < 5.0:
                    locked = True
                    r["locked"] = True
        else:
            limit = min(0.99, round(best + 0.03, 2))
            unit = limit + fee(limit) + 0.005
            q = min(max(q_req, 5.0), 60.0, 300.0 - b.q["UP"] - b.q["DOWN"], (90.0 - rec_cost) / unit, 100.0 / unit)
            q = math.floor(max(q, 0.0) * 100) / 100
            if q < 5.0 - EPS:
                i += 1
                continue
            px = taker_buy(S, t, limit)
            last_act, busy, actions = t, t + LAT + SURV, actions + 1
            if px is not None:
                if not (rec_open and S == rec_side):
                    episodes += 1
                    rec_open, rec_side = True, S
                b.buy(S, q, px)
                rec_cost += q * (px + fee(px))
                r["rec_buys"] += 1
                r["recoveries"] = episodes
                acted = True
        i = max(int(np.searchsorted(T, busy)), i + 1) if busy > t else i + 1
    r["pnl"] = b.q[outcome] - b.total() + b.realised
    return r


# ───────────────────────── report ─────────────────────────

def hour(ms):
    return int(((ms + TZ) // 3600000) % 24)


def summarise(rs):
    tr = [r for r in rs if r["entered"]]
    if not tr:
        return None
    p = np.array([r["pnl"] for r in tr])
    return {"n": len(tr), "mean": float(p.mean()), "total": float(p.sum()),
            "se": float(p.std(ddof=1) / math.sqrt(len(p))) if len(p) > 1 else float("nan"),
            "win": float(np.mean(p > 0)), "worst": float(p.min()),
            "rec": float(np.mean([r["recoveries"] > 0 for r in tr])),
            "lock": float(np.mean([r["locked"] for r in tr]))}


CURRENT = dict(open_delay=2.0, open_window=20.0, open_min=0.70, open_max=0.85, shares=10.0, max_rec=2, profit_exit=1.0)
VARIANTS = {
    "current: leader 0.70-0.85, 2 recoveries": CURRENT,
    "leader 0.50-0.85, 2 recoveries": {**CURRENT, "open_min": 0.50},
    "leader 0.70-0.85, 1 recovery": {**CURRENT, "max_rec": 1},
}
BLOCKS = [("00-02", (0, 1, 2)), ("03-05", (3, 4, 5)), ("06-08", (6, 7, 8)), ("09-11", (9, 10, 11)),
          ("12-14", (12, 13, 14)), ("15-17", (15, 16, 17)), ("18-20", (18, 19, 20)), ("21-23", (21, 22, 23))]


def main():
    ws = load()
    ws.sort(key=lambda w: w[1])
    split = ws[0][1] + (ws[-1][1] - ws[0][1]) / 2
    f = lambda ms: time.strftime("%m-%d %H:%M", time.gmtime((ms + TZ) / 1000))  # noqa: E731
    print("# Pair-lock recovery bot: profit by GMT+9 hour (replay, 2026-10-06)\n")
    print(f"{len(ws):,} recorded BTC 5-minute markets {f(ws[0][1])} -> {f(ws[-1][1])} GMT+9. Halves split at "
          f"{f(split)}. $ per market traded (10-share open buy).\n")
    out = {}
    for name, P in VARIANTS.items():
        rs = [simulate(w, P) for w in ws]
        out[name] = rs
        s = summarise(rs)
        print(f"## {name}\n")
        print(f"All: {s['n']} markets traded of {len(ws)}, {s['mean']:+.3f} ± {s['se']:.3f} a market, total "
              f"{s['total']:+.1f}, won {s['win']:.0%}, recovered {s['rec']:.0%}, locked {s['lock']:.0%}, worst "
              f"{s['worst']:+.1f}.\n")
        print("| GMT+9 hours | markets | $ / market | ± se | total $ | 1st half $/mkt | 2nd half $/mkt | won | recovered | worst $ |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for lab, hs in BLOCKS:
            sub = [r for r in rs if hour(r["open"]) in hs]
            a = summarise(sub)
            h1 = summarise([r for r in sub if r["open"] < split])
            h2 = summarise([r for r in sub if r["open"] >= split])
            print(f"| {lab} | {a['n']} | {a['mean']:+.3f} | {a['se']:.3f} | {a['total']:+.1f} | "
                  f"{h1['mean'] if h1 else float('nan'):+.3f} | {h2['mean'] if h2 else float('nan'):+.3f} | "
                  f"{a['win']:.0%} | {a['rec']:.0%} | {a['worst']:+.1f} |")
        print("\n| hour | markets | $ / market | ± se | total $ | 1st half | 2nd half |\n|---|---:|---:|---:|---:|---:|---:|")
        for h in range(24):
            sub = [r for r in rs if hour(r["open"]) == h]
            a = summarise(sub)
            h1 = summarise([r for r in sub if r["open"] < split])
            h2 = summarise([r for r in sub if r["open"] >= split])
            if a:
                print(f"| {h:02d} | {a['n']} | {a['mean']:+.3f} | {a['se']:.3f} | {a['total']:+.1f} | "
                      f"{h1['mean'] if h1 else float('nan'):+.3f} | {h2['mean'] if h2 else float('nan'):+.3f} |")
        hm1 = [summarise([r for r in rs if hour(r["open"]) == h and r["open"] < split]) for h in range(24)]
        hm2 = [summarise([r for r in rs if hour(r["open"]) == h and r["open"] >= split]) for h in range(24)]
        x = [a["mean"] for a, b2 in zip(hm1, hm2) if a and b2]
        y = [b2["mean"] for a, b2 in zip(hm1, hm2) if a and b2]
        rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
        print(f"\nDo the hours repeat? Spearman of the 24 hourly means, 1st half vs 2nd half: "
              f"{np.corrcoef(rx, ry)[0, 1]:+.2f}.\n")
    json.dump({k: v for k, v in out.items()}, open(f"{ROOT}/results/pairlock_recovery_hours_2026-10-06.json", "w"))


if __name__ == "__main__":
    main()
