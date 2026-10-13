"""Does Polymarket book flow give an earlier usable direction signal than crypto spot? (2026-10-01)

Data: scripts/record_ticks.py recordings (data/ticks/ticks_*.jsonl.gz): crypto quotes/trades (cb, by, bn), the
BTC market's UP / DOWN books at every update ('b': best bid/ask, touch sizes, depth within 2 ticks, venue time,
receipt time) and Polymarket trades ('pt'). Train = the first file (feature scales, threshold); test = the rest.

Signals compared on the TOKEN (what the bot buys), with the paper fill model: FOK at the signal side's ask + 3c,
landing 800 ms later, fee 7 % x p(1-p), 5 shares, settled at the window's outcome (data/charts).
  A  crypto flow signal (strategy/flowsignal.py, the app's settings)
  B  Polymarket book-flow signal only
  C  A confirmed by B (same direction within +/- 300 ms, either order)
Lead / lag: for every token move (UP mid +/- 2c within 2 s), when did A, B and a plain "ask pulled" marker fire
relative to the move's start.
usage: bookflow_study.py train.jsonl.gz test.jsonl.gz [...]"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import os
import statistics
import sys
from collections import defaultdict, deque

sys.path.insert(0, "/root/troll-poly-bot/src")
from troll_poly_bot.strategy.flowsignal import FlowParams, FlowSignal   # noqa: E402

ROOT = "/root/troll-poly-bot"
NAMES = {"cb": "coinbase", "by": "bybit", "bn": "binance"}
LAT = 800.0
WINDOWS = (100.0, 250.0, 500.0, 1000.0)
HORIZ = (250, 500, 1000, 2000, 5000)
FEE = 0.07
FLOW = FlowParams(ref_bp=0.6, accel_ref_bp=0.6, min_bp=0.2, gate_bp=0.15,     # the app's settings
                  min_confirm=int(os.environ.get("FLOW_MIN_CONFIRM", "2")))      # 1 = fastest valid event, no 2nd exchange


def fee(p):
    return FEE * p * (1 - p)


def outcome(slug, cache={}):
    if slug not in cache:
        p = f"{ROOT}/data/charts/{slug}.json"
        cache[slug] = json.load(open(p)).get("outcome") if os.path.exists(p) else None
    return cache[slug]


def load(path):
    mk, ev, lag = {}, [], defaultdict(list)
    with gzip.open(path, "rt") as f:
        for line in f:
            r = json.loads(line)
            k = r.get("k")
            if k == "m":
                if r.get("s") == "BTC":
                    mk[r["up"]] = ("UP", r["slug"], r["open"], r["close"])
                    mk[r["dn"]] = ("DOWN", r["slug"], r["open"], r["close"])
            elif k in ("q", "t"):
                if r.get("s") == "BTC" and r.get("x") in NAMES:
                    ev.append(r)
                    if k == "t" and r.get("et"):
                        lag[r["x"]].append(r["rt"] - r["et"])
            elif k in ("b", "pt") and r.get("tok") in mk:
                ev.append(r)
    ev.sort(key=lambda r: r["rt"])
    return mk, ev, {x: statistics.median(v) for x, v in lag.items()}


class Book:
    __slots__ = ("h",)

    def __init__(self):
        self.h = deque()                        # (rt, bb, ba, bbs, bas, bd2, ad2)

    def push(self, r):
        self.h.append((r["rt"], r.get("bb"), r.get("ba"), r.get("bbs") or 0.0, r.get("bas") or 0.0,
                       r.get("bd2") or 0.0, r.get("ad2") or 0.0))
        while self.h and self.h[0][0] < r["rt"] - 6000.0:
            self.h.popleft()

    def at(self, t):
        for i in range(len(self.h) - 1, -1, -1):
            if self.h[i][0] <= t:
                return self.h[i]
        return None

    @property
    def now(self):
        return self.h[-1] if self.h else None


def mid(b):
    if b is None:
        return None
    bb, ba = b[1], b[2]
    if bb is None and ba is None:
        return None
    if bb is None:
        return ba - 0.005
    if ba is None:
        return bb + 0.005
    return 0.5 * (bb + ba)


def features(up: Book, dn: Book, trades, t):
    """Book-flow components in the UP direction (positive = evidence the UP token rises), per window."""
    out = {}
    nu, nd = up.now, dn.now
    if nu is None or nd is None:
        return None
    for w in WINDOWS:
        pu, pd = up.at(t - w), dn.at(t - w)
        if pu is None or pd is None:
            return None
        f = {}
        # price velocity: UP ask / bid rising, DOWN ask / bid falling (cents)
        def d(a, b):
            return 0.0 if a is None or b is None else (a - b) * 100.0
        f["vel"] = (d(nu[2], pu[2]) + d(nu[1], pu[1]) - d(nd[2], pd[2]) - d(nd[1], pd[1])) / 2.0
        # ask consumption at the touch: UP asks eaten (bullish), DOWN asks eaten (bearish); a level that
        # vanished counts as fully consumed
        def cons(n, p):
            if n[2] is None or p[2] is None:
                return 0.0
            if n[2] > p[2] + 1e-9:
                return p[4]
            if abs(n[2] - p[2]) < 1e-9:
                return max(0.0, p[4] - n[4])
            return 0.0
        f["cons"] = cons(nu, pu) - cons(nd, pd)
        # executed aggressive volume in the window: buys of UP + sells of DOWN are bullish
        bu = sum(sz for (rt, p, sz, s, tok) in trades if t - w <= rt <= t and tok == "UP" and s == "BUY")
        su = sum(sz for (rt, p, sz, s, tok) in trades if t - w <= rt <= t and tok == "UP" and s == "SELL")
        bd = sum(sz for (rt, p, sz, s, tok) in trades if t - w <= rt <= t and tok == "DOWN" and s == "BUY")
        sd = sum(sz for (rt, p, sz, s, tok) in trades if t - w <= rt <= t and tok == "DOWN" and s == "SELL")
        f["exec"] = (bu - su) - (bd - sd)
        # depth change within 2 ticks: UP asks removed beyond what trades took = cancellations (bullish: makers
        # pulling UP offers); UP bids added = new liquidity (bullish); mirrored for DOWN
        dAu, dBu, dAd, dBd = nu[6] - pu[6], nu[5] - pu[5], nd[6] - pd[6], nd[5] - pd[5]
        f["cancel"] = max(0.0, -dAu - bu) - max(0.0, -dAd - bd) - (max(0.0, -dBu - su) - max(0.0, -dBd - sd))
        f["newliq"] = max(0.0, dBu) - max(0.0, dAu) - (max(0.0, dBd) - max(0.0, dAd))
        # the UP-vs-DOWN imbalance level (not a change)
        f["imb"] = ((nu[5] - nu[6]) - (nd[5] - nd[6])) / max(1.0, nu[5] + nu[6] + nd[5] + nd[6])
        out[w] = f
    return out


KEYS = ("vel", "cons", "exec", "cancel", "newliq", "imb")


class BookFlow:
    """Equal-weight sum of robust-scaled components over the four windows; a direction fires when the score
    passes ``thr`` and the previous evaluation (>= 50 ms earlier) had the same sign (persistence)."""

    def __init__(self, scale, thr, dedupe_ms=1000.0):
        self.scale, self.thr, self.dedupe = scale, thr, dedupe_ms
        self.prev = None
        self.last = {1: -1e18, -1: -1e18}
        self.scores = []

    def score(self, f):
        s = 0.0
        for w in WINDOWS:
            for k in KEYS:
                sc = self.scale[(w, k)]
                if sc > 0:
                    s += max(-3.0, min(3.0, f[w][k] / sc))
        return s / (len(WINDOWS) * len(KEYS))

    def on(self, f, t):
        s = self.score(f)
        self.scores.append(s)
        d = 1 if s >= self.thr else -1 if s <= -self.thr else 0
        fired = None
        if d and self.prev is not None and self.prev[1] == d and t - self.prev[0] >= 50.0 and t - self.last[d] >= self.dedupe:
            self.last[d] = t
            fired = d
        if self.prev is None or t - self.prev[0] >= 50.0:
            self.prev = (t, d)
        return fired, s


def run(path, scale=None, thr=None, label=""):
    mk, ev, lag = load(path)
    fs = FlowSignal(FLOW)
    books = {}
    trades = deque()
    live = None                                  # (slug, up_tok, dn_tok, open, close)
    sigA, sigB, pulls = [], [], []
    feats_sample = []                            # (t, f) for scale fitting / scoring
    mids = []                                    # (rt, up_mid) for the targets
    asks = {"UP": [], "DOWN": []}                # (rt, ask)
    bf = BookFlow(scale, thr) if scale else None
    last_eval = 0.0
    by_slug = {}
    for tok, (side, slug, o, c) in mk.items():
        by_slug.setdefault(slug, {})[side] = tok
    for r in ev:
        rt, k = r["rt"], r["k"]
        if k in ("q", "t"):
            x = r["x"]
            ti = r["et"] if r.get("et") and abs(rt - r["et"]) < 30000 else rt - lag.get(x, 0.0)
            if k == "q":
                if r.get("b") and r.get("a"):
                    res = fs.on_quote("BTC", NAMES[x], r["b"], r["a"], ti, rt)
                else:
                    res = None
            else:
                res = fs.on_trade("BTC", NAMES[x], r["p"] * r["q"], r.get("side") == 1, ti, rt)
            if res is not None:
                sigA.append((rt, res.dir, res.score))
            continue
        side, slug, o, c = mk[r["tok"]]
        if not (o <= rt < c):
            continue
        if live is None or live[0] != slug:
            live = (slug, by_slug[slug].get("UP"), by_slug[slug].get("DOWN"), o, c)
            books = {"UP": Book(), "DOWN": Book()}
            trades.clear()
        if k == "pt":
            trades.append((rt, r["p"], r["sz"], r.get("side"), side))
            while trades and trades[0][0] < rt - 2000.0:
                trades.popleft()
            continue
        b = books[side]
        prev = b.now
        b.push(r)
        if side == "UP":
            m = mid(b.now)
            if m is not None:
                mids.append((rt, m, slug))
        a = b.now[2]
        if a is not None:
            asks[side].append((rt, a, slug))
        # "ask pulled": the touch ask vanished or jumped >= 2c with no trade at it in the last 250 ms
        if prev is not None and prev[2] is not None and (a is None or a >= prev[2] + 0.02 - 1e-9):
            traded = any(abs(p - prev[2]) < 1e-9 and rt - t0 <= 250.0 for (t0, p, sz, s, tok) in trades if tok == side)
            if not traded:
                pulls.append((rt, 1 if side == "UP" else -1, slug))
        if rt - last_eval >= 25.0:
            last_eval = rt
            f = features(books["UP"], books["DOWN"], trades, rt)
            if f is None:
                continue
            if bf is None:
                if len(feats_sample) < 400_000:
                    feats_sample.append(f)
            else:
                fired, s = bf.on(f, rt)
                if fired:
                    sigB.append((rt, fired, s, slug))
    return dict(mk=mk, sigA=sigA, sigB=sigB, pulls=pulls, mids=mids, asks=asks, feats=feats_sample, bf=bf,
                span_h=(ev[-1]["rt"] - ev[0]["rt"]) / 3.6e6 if ev else 0.0, by_slug=by_slug)


def fit_scale(feats):
    scale = {}
    for w in WINDOWS:
        for k in KEYS:
            v = sorted(abs(f[w][k]) for f in feats if f[w][k] != 0.0)
            scale[(w, k)] = v[int(0.75 * len(v))] if len(v) > 100 else 0.0
    return scale


def token_moves(mids, cents=0.02, within=2000.0):
    """Starts of UP-mid moves of >= ``cents`` within ``within`` ms: (t_start, dir, slug)."""
    out, i = [], 0
    ts = [m[0] for m in mids]
    last_t = -1e18
    while i < len(mids):
        t0, m0, slug = mids[i]
        j = bisect.bisect_right(ts, t0 + within)
        seg = mids[i:j]
        hi = max(seg, key=lambda x: x[1]) if seg else None
        lo = min(seg, key=lambda x: x[1]) if seg else None
        d = 0
        if hi and hi[1] - m0 >= cents - 1e-9:
            d = 1
        elif lo and m0 - lo[1] >= cents - 1e-9:
            d = -1
        if d and t0 - last_t > within:
            out.append((t0, d, slug))
            last_t = t0
            i = j
        else:
            i += 1
    return out


def unmoved(sigs, data, max_c=0.01, look=1000.0):
    """Signals whose side's ask has NOT already risen more than ``max_c`` over the last ``look`` ms."""
    asks = data["asks"]
    ts = {s: [a[0] for a in asks[s]] for s in asks}
    out = []
    for s in sigs:
        side = "UP" if s[1] > 0 else "DOWN"
        i1 = bisect.bisect_right(ts[side], s[0]) - 1
        i0 = bisect.bisect_right(ts[side], s[0] - look) - 1
        if i1 < 0 or i0 < 0:
            continue
        if asks[side][i1][1] - asks[side][i0][1] <= max_c + 1e-9:
            out.append(s)
    return out


def evaluate(name, sigs, data, note=""):
    mids, asks = data["mids"], data["asks"]
    mts = [m[0] for m in mids]

    def mid_at(t):
        i = bisect.bisect_right(mts, t) - 1
        return mids[i][1] if i >= 0 else None

    def ask_at(side, t):
        arr = asks[side]
        i = bisect.bisect_right([a[0] for a in arr], t) - 1 if arr else -1
        return arr[i][1] if i >= 0 else None

    rows = {h: [] for h in HORIZ}
    fills, pnl, pair_cost, n = 0, [], [], 0
    for s in sigs:
        rt, d = s[0], s[1]
        slug = s[3] if len(s) > 3 else next((m[2] for m in mids[bisect.bisect_right(mts, rt) - 1:bisect.bisect_right(mts, rt)]), None)
        m0 = mid_at(rt)
        if m0 is None:
            continue
        n += 1
        for h in HORIZ:
            m1 = mid_at(rt + h)
            if m1 is not None:
                rows[h].append(d * (m1 - m0) * 100.0)
        side = "UP" if d > 0 else "DOWN"
        a0, a1 = ask_at(side, rt), ask_at(side, rt + LAT)
        if a0 is None or a1 is None or not (0.20 <= a0 <= 0.96):
            continue
        if a1 <= min(a0 + 0.03, 0.96) + 1e-9:
            fills += 1
            o = outcome(slug) if slug else None
            if o:
                won = o.upper() == side
                pnl.append((5.0 if won else 0.0) - 5.0 * a1 - 5.0 * fee(a1))
            other = ask_at("DOWN" if side == "UP" else "UP", rt + LAT)
            if other is not None:
                pair_cost.append(a1 + other + fee(a1) + fee(other))
    span = data["span_h"]
    mean = lambda v: statistics.mean(v) if v else float("nan")                                   # noqa: E731
    hit = lambda v: (sum(1 for x in v if x > 0) / len(v)) if v else float("nan")                 # noqa: E731
    print(f"  {name:34} {n:5} sig ({n / span:6.1f}/h) | token move after: "
          + " ".join(f"{h / 1000:g}s {mean(rows[h]):+.2f}c/{hit(rows[h]) * 100:.0f}%" for h in HORIZ)
          + f"\n  {'':34} fills {fills}/{n} ({fills / n * 100 if n else 0:.0f}%)  leg1 settled {len(pnl)}: {sum(pnl):+.2f} USDC"
            f" ({mean(pnl):+.3f}/fill, win {hit(pnl) * 100:.0f}%)  executable pair cost {mean(pair_cost):.3f}"
            f" (<=0.95: {sum(1 for x in pair_cost if x <= 0.95) / len(pair_cost) * 100 if pair_cost else 0:.0f}%) {note}")


def leadlag(data, sigA, sigB):
    moves = token_moves(data["mids"])
    out = {"A": [], "B": [], "pull": []}
    cover = {"A": 0, "B": 0, "pull": 0}
    for t0, d, slug in moves:
        for name, sigs in (("A", sigA), ("B", sigB), ("pull", data["pulls"])):
            first = next((s[0] for s in sigs if s[1] == d and t0 - 3000.0 <= s[0] <= t0 + 2000.0), None)
            if first is not None:
                cover[name] += 1
                out[name].append(first - t0)
    n = len(moves)
    print(f"  token moves (UP mid +/-2c within 2 s): {n}")
    for name, lab in (("A", "crypto flow signal"), ("B", "PM book-flow signal"), ("pull", "PM ask pulled (>=2c, no trade)")):
        v = out[name]
        if v:
            v.sort()
            print(f"    {lab:32} fired for {cover[name]}/{n} ({cover[name] / n * 100:.0f}%) | ms vs move start: "
                  f"p25 {v[len(v) // 4]:+.0f} p50 {v[len(v) // 2]:+.0f} p75 {v[3 * len(v) // 4]:+.0f} | before the start: "
                  f"{sum(1 for x in v if x < 0) / len(v) * 100:.0f}%")
    # pairwise: when both A and B fire for the same move, which is first
    both = []
    for t0, d, slug in moves:
        a = next((s[0] for s in sigA if s[1] == d and t0 - 3000.0 <= s[0] <= t0 + 2000.0), None)
        b = next((s[0] for s in sigB if s[1] == d and t0 - 3000.0 <= s[0] <= t0 + 2000.0), None)
        if a is not None and b is not None:
            both.append(b - a)
    if both:
        both.sort()
        print(f"    both fired: {len(both)} moves; B minus A ms p50 {both[len(both) // 2]:+.0f}; B first in "
              f"{sum(1 for x in both if x < 0) / len(both) * 100:.0f}%")


def confirm(sigA, sigB, within=300.0):
    out = []
    tb = [s[0] for s in sigB]
    for a in sigA:
        i = bisect.bisect_left(tb, a[0] - within)
        ok = False
        while i < len(sigB) and sigB[i][0] <= a[0] + within:
            if sigB[i][1] == a[1]:
                ok = True
                break
            i += 1
        if ok:
            out.append(a)
    return out


def confirm_d(sigB, sigA, within=300.0):
    """D: Polymarket-first -- a book-flow signal confirmed by a crypto signal in the same direction within
    ``within`` ms AFTER it (the entry time is the crypto confirmation)."""
    out = []
    ta = [s[0] for s in sigA]
    for b in sigB:
        i = bisect.bisect_left(ta, b[0])
        while i < len(sigA) and sigA[i][0] <= b[0] + within:
            if sigA[i][1] == b[1]:
                out.append(sigA[i])
                break
            i += 1
    return sorted(set(out))


def main():
    paths = sys.argv[1:]
    train = run(paths[0])
    scale = fit_scale(train["feats"])
    print(f"TRAIN {os.path.basename(paths[0])}: {train['span_h']:.2f} h, {len(train['feats'])} feature samples; scales (75th pct of |x|):")
    for w in WINDOWS:
        print("   ", f"{w:g} ms", {k: round(scale[(w, k)], 3) for k in KEYS})
    # threshold grid on TRAIN by the 1 s token move hit rate x count
    best = None
    for thr in ((0.7,) if os.environ.get("THR_ONLY") else (0.15, 0.25, 0.35, 0.5, 0.7)):
        d = run(paths[0], scale, thr)
        sig = d["sigB"]
        mts = [m[0] for m in d["mids"]]
        hits = []
        for s in sig:
            i0 = bisect.bisect_right(mts, s[0]) - 1
            i1 = bisect.bisect_right(mts, s[0] + 1000) - 1
            if i0 >= 0 and i1 > i0:
                hits.append(s[1] * (d["mids"][i1][1] - d["mids"][i0][1]))
        hr = sum(1 for x in hits if x > 0) / len(hits) if hits else 0
        mv = statistics.mean(hits) * 100 if hits else 0
        print(f"  thr {thr:.2f}: {len(sig)} B signals ({len(sig) / d['span_h']:.0f}/h), 1 s token move {mv:+.2f}c, hit {hr * 100:.0f}%")
        if hits and len(sig) >= 20 and (best is None or mv * math.sqrt(len(hits)) > best[0]):
            best = (mv * math.sqrt(len(hits)), thr, d)
    thr = best[1] if best else 0.35
    print(f"  chosen threshold {thr} (train)\n")
    for i, p in enumerate(paths):
        d = best[2] if (i == 0 and best and best[1] == thr) else run(p, scale, thr)
        print(f"{'TRAIN' if i == 0 else 'TEST'} {os.path.basename(p)}: {d['span_h']:.2f} h")
        A, B = d["sigA"], d["sigB"]
        C = confirm(A, B)
        evaluate("A crypto flow", A, d)
        evaluate("A, token not yet moved (<=1c/1s)", unmoved(A, d, 0.01), d)
        evaluate("A, token not yet moved (<=0c/1s)", unmoved(A, d, 0.0), d)
        evaluate("A, not moved (<=1c / 500 ms)", unmoved(A, d, 0.01, 500.0), d)
        evaluate("B PM book flow", B, d)
        evaluate("C crypto AND book flow (300 ms)", C, d)
        evaluate("C, token not yet moved (<=1c/1s)", unmoved(C, d, 0.01), d)
        evaluate("D PM-first, crypto confirms <=300 ms", confirm_d(B, A), d)
        evaluate("D, token not yet moved (<=1c/1s)", unmoved(confirm_d(B, A), d, 0.01), d)
        leadlag(d, A, B)
        print()


main()
