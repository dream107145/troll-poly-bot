"""FASTCLIP research: the x-moneyforwhiskas pattern (docs/trader_x-moneyforwhiskas_2026-09-29.md),
replayed at OUR speed on the 5-minute archive.

    ionice -c3 nice -n 19 python scripts/research_fastclip.py > results/fastclip_backtest.md

RESEARCH ONLY, read-only on the archive. The trader: BTC 5-minute markets only, every window;
buys only, small clips (median 16 shares / $7), ~43 per market, whichever side BTC is moving
toward just before the token reprices, both sides in 97 % of markets, every position held to
settlement. Here, per archive point (~220 ms apart, research_repriced_momentum cache):

  trigger   the spot's log return over ``h`` ms, in sigma (sqrt-time scaled), >= ``k``, measured on
            the composite ("comp") or on whichever of Binance / Bybit / Coinbase shows the largest
            move ("lead": the first tick on the fastest source, the nearest we have to theirs)
  value     where the market's own curve says the token should be now: the side's mid 1 s ago plus
            the change in the market-implied fair value (strategy.spot_lag.MFV_COEF) since then. For
            "lead" the part of the move the composite has not shown yet is added to the analytic
            P(up) through its local slope, phi(z) * dlnS / sigma_settle, before the curve is applied
  entry     side ask in 0.10-0.90, spread <= 3c, value - ask - taker fee(ask) >= ``edge``;
            FAK at ask + ``offset``, up to 16 shares or $7 (>= the 5-share venue minimum)
  clips     one order in flight; the same side again only >= 1 s later; <= 40 orders and $40 per
            market; both sides allowed; 8 s after the open to 60 s before the close
  exit      none: 1 / 0 at settlement, the entry fee only

Execution (backtest.spot_shock_replay, validated against venue-timestamped books): presend and
half the venue round trip drawn from the measured priors; the venue book is the archived local
book 500 ms after arrival; a FAK fills min(size, top-of-book depth) at that ask if it is at or
under the limit (deeper levels are not in the archive: conservative). "zero latency" instead
fills at the book the decision read, as a trader ~1 s ahead of our feed would.

Selection: a pre-registered grid on BTC, chosen by train + validation net (>= 30 fills in each);
test and forward reported untouched. Periods as in the other studies (research cache splits).
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.backtest.spot_shock_replay import SETTLE_GUARD_MS, load_asset, mfv, windows  # noqa: E402
from troll_poly_bot.execution.latency import PRESEND_PRIOR_MS, VENUE_RTT_PRIOR_MS               # noqa: E402

CACHE = ROOT / "data/replay/rm_research_cache"
LOCK = ROOT / "data/replay/fastclip_locked.json"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
PERIOD = {"train": "train", "validation": "validation", "unseen_1": "test", "unseen_2": "test", "unseen_3": "forward"}
PERIODS = ("train", "validation", "test", "forward")

MIN_ASK, MAX_ASK, MAX_SPREAD = 0.10, 0.90, 0.03
CLIP_SHARES, CLIP_USDC, VENUE_MIN = 16.0, 7.0, 5.0
SAME_SIDE_GAP_MS = 1000.0
MAX_ORDERS, MARKET_CAP = 40, 40.0
MIN_LEFT, MAX_LEFT = 60.0, 292.0
PROXY_MS = 500.0
LOOKBACK_MS = 1000.0

GRID = {"source": ("comp", "lead"), "h": (250, 500, 1000), "k": (1.5, 2.0, 3.0), "edge": (0.0, 0.01, 0.02),
        "offset": (0.0, 0.01)}
PRICE_B = ((0.10, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90))
LEFT_B = ((240, 292), (180, 240), (120, 180), (60, 120))


def _ret(t, x, lag_ms):
    j = np.searchsorted(t, t - lag_ms, side="right") - 1
    ok = (j >= 0) & (t - t[np.maximum(j, 0)] <= lag_ms + 1500.0)
    return np.where(ok, x - x[np.maximum(j, 0)], np.nan)


def _norm_pdf_of_quantile(p):
    """phi(Phi^-1(p)), by the Acklam-free route: bisection on erf (vectorised Newton)."""
    p = np.clip(p, 1e-4, 1 - 1e-4)
    z = np.zeros_like(p)
    for _ in range(40):                                   # Newton on Phi(z) - p
        cdf = 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))
        pdf = np.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
        z = z - (cdf - p) / np.maximum(pdf, 1e-12)
    return np.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)


class Win:
    """One market's arrays, precomputed once for every configuration."""

    def __init__(self, w, extra) -> None:
        self.slug, self.split, self.close, self.won_up, self.rate = w.slug, w.split, w.close, w.won_up, w.rate
        self.t, self.left = w.t, w.left
        n = len(w.t)
        sig = np.maximum(w.sigma, 1e-7)
        self.sv = {s: w.side[s] for s in ("UP", "DOWN")}
        j1 = np.searchsorted(w.t, w.t - LOOKBACK_MS, side="right") - 1
        ok1 = (j1 >= 0) & (w.t - w.t[np.maximum(j1, 0)] <= LOOKBACK_MS + 800.0)
        self.j1, self.ok1 = np.maximum(j1, 0), ok1
        self.mfv_up = w.side["UP"]["mfv"]
        self.p = extra["p"]
        u = np.maximum(w.left, 0.0)
        var = np.where(u >= 60.0, u - 40.0, u ** 3 / 10800.0)
        self.sig_settle = sig * np.sqrt(np.maximum(var, 1.0))
        self.slope = _norm_pdf_of_quantile(self.p)
        self.z = {}                     # (source, h) -> signed z of the trigger
        self.extra_move = {}            # (source, h) -> the move the composite has not shown yet
        lp = w.lp
        for h in GRID["h"]:
            rc = _ret(w.t, lp, h)
            zc = rc / (sig * math.sqrt(h / 1000.0))
            self.z[("comp", h)] = zc
            self.extra_move[("comp", h)] = np.zeros(n)
            best_z = np.full(n, np.nan)
            best_r = np.full(n, np.nan)
            for x in ("bn", "byb", "cb"):
                px = extra[x]
                with np.errstate(invalid="ignore", divide="ignore"):
                    lx = np.log(np.where(px > 0, px, np.nan))
                rx = _ret(w.t, lx, h)
                zx = rx / (sig * math.sqrt(h / 1000.0))
                better = np.nan_to_num(np.abs(zx), nan=-1.0) > np.nan_to_num(np.abs(best_z), nan=-1.0)
                best_z = np.where(better, zx, best_z)
                best_r = np.where(better, rx, best_r)
            self.z[("lead", h)] = best_z
            self.extra_move[("lead", h)] = np.nan_to_num(best_r - rc, nan=0.0)

    def values(self, source, h):
        """(value of UP, value of DOWN) at every point: side mid 1 s ago + the curve's move."""
        dp = self.slope * self.extra_move[(source, h)] / self.sig_settle
        p_adj = np.clip(self.p + dp, 0.001, 0.999)
        mfv_now = mfv(p_adj, self.left)
        d_mfv = mfv_now - self.mfv_up[self.j1]
        mid_up, mid_dn = self.sv["UP"]["mid"], self.sv["DOWN"]["mid"]
        v_up = np.where(self.ok1, mid_up[self.j1] + d_mfv, np.nan)
        v_dn = np.where(self.ok1, mid_dn[self.j1] - d_mfv, np.nan)
        return v_up, v_dn

    def fee(self, p):
        return self.rate * p * (1.0 - p)


def simulate(win: Win, cfg: dict, rng: random.Random, zero_latency: bool = False, random_rule: bool = False):
    """One market, in time order. Returns the fills."""
    t, left = win.t, win.left
    n = len(t)
    if random_rule:
        sel = [i for i in range(n) if rng.random() < 0.02]
        sides = {i: ("UP" if rng.random() < 0.5 else "DOWN") for i in sel}
        v_up = v_dn = None
    else:
        z = win.z[(cfg["source"], cfg["h"])]
        hit = np.nonzero(np.nan_to_num(np.abs(z), nan=0.0) >= cfg["k"])[0]
        sel = list(hit)
        sides = {i: ("UP" if z[i] > 0 else "DOWN") for i in sel}
        v_up, v_dn = win.values(cfg["source"], cfg["h"])
    fills = []
    busy_until, last_side = -1e18, {"UP": -1e18, "DOWN": -1e18}
    orders, cost = 0, 0.0
    for i in sel:
        if not (MIN_LEFT <= left[i] <= MAX_LEFT) or t[i] < busy_until:
            continue
        side = sides[i]
        if t[i] - last_side[side] < SAME_SIDE_GAP_MS or orders >= MAX_ORDERS or cost >= MARKET_CAP:
            continue
        sv = win.sv[side]
        ask, bid = sv["ask"][i], sv["bid"][i]
        if ask != ask or bid != bid or not (MIN_ASK <= ask <= MAX_ASK) or ask - bid > MAX_SPREAD + 1e-9:
            continue
        if not random_rule:
            val = (v_up if side == "UP" else v_dn)[i]
            if val != val or val - ask - win.fee(ask) < cfg["edge"] - 1e-12:
                continue
        limit = round(ask + (0.0 if random_rule else cfg["offset"]), 2)
        size = min(CLIP_SHARES, math.floor(min(CLIP_USDC, MARKET_CAP - cost) / limit * 100.0) / 100.0)
        if size < VENUE_MIN:
            continue
        orders += 1
        last_side[side] = t[i]
        if zero_latency:
            ja, t_arr, half = i, t[i], 0.0
        else:
            pre = rng.choice(PRESEND_PRIOR_MS) * rng.uniform(.85, 1.15)
            half = rng.choice(VENUE_RTT_PRIOR_MS) * rng.uniform(.85, 1.15) / 2.0
            t_arr = t[i] + pre + half
            ja = int(np.searchsorted(t, t_arr + PROXY_MS))
        busy_until = t_arr + half
        if ja >= n or t[ja] >= win.close - SETTLE_GUARD_MS:
            continue
        a, dep = sv["ask"][ja], sv["adep"][ja]
        if a != a or a > limit + 1e-9 or not (dep > 0):
            fills.append({"i": i, "side": side, "filled": False})
            continue
        sh = min(size, float(dep))
        cost += sh * a
        won = win.won_up == (side == "UP")
        fills.append({"i": i, "side": side, "filled": True, "px": float(a), "sh": sh, "left": float(left[i]),
                      "net": ((1.0 if won else 0.0) - a) * sh - win.fee(a) * sh, "fee": win.fee(a) * sh,
                      "cost": a * sh, "won": won, "t": float(t[i])})
    return fills


def load(assets):
    per = defaultdict(list)
    for asset in assets:
        d, meta = load_asset(CACHE, asset)
        span = {str(r[0]): (int(r[6]), int(r[7])) for r in meta}
        for w in windows(d, meta):
            p = PERIOD.get(w.split)
            if p is None or w.won_up is None:
                continue
            s, e = span[w.slug]
            extra = {k: d[k][s:e] for k in ("p", "bn", "byb", "cb")}
            per[p].append((asset, Win(w, extra)))
        del d
        print(f"… {asset}", file=sys.stderr)
    for p in per:
        per[p].sort(key=lambda x: x[1].close)
    return per


def run(per, cfg, periods=PERIODS, draws=1, **kw):
    """Per period: summary dict averaged over draws, and the pooled fills (with market ids)."""
    out = {}
    for p in periods:
        agg = defaultdict(float)
        pooled = []
        for draw in range(draws):
            rng = random.Random(1000 * draw + 7)
            mk_net, fills_all, orders = [], [], 0
            for asset, win in per[p]:
                fs = simulate(win, cfg, rng, **kw)
                orders += len(fs)
                got = [f for f in fs if f["filled"]]
                for f in got:
                    f["asset"], f["market"] = asset, win.slug
                fills_all += got
                if got:
                    sides = {f["side"] for f in got}
                    up = [f for f in got if f["side"] == "UP"]
                    dn = [f for f in got if f["side"] == "DOWN"]
                    sh_up, sh_dn = sum(f["sh"] for f in up), sum(f["sh"] for f in dn)
                    mk_net.append({"net": sum(f["net"] for f in got), "both": len(sides) == 2,
                                   "sum_px": (sum(f["cost"] for f in up) / sh_up + sum(f["cost"] for f in dn) / sh_dn)
                                   if sh_up and sh_dn else None,
                                   "unhedged": abs(sh_up - sh_dn) / max(sh_up, sh_dn), "cost": sum(f["cost"] for f in got),
                                   "close": win.close})
            nets = [m["net"] for m in sorted(mk_net, key=lambda m: m["close"])]
            cum = peak = dd = 0.0
            for x in nets:
                cum += x; peak = max(peak, cum); dd = max(dd, peak - cum)
            agg["markets"] += len(per[p]); agg["traded"] += len(mk_net); agg["orders"] += orders
            agg["fills"] += len(fills_all); agg["shares"] += sum(f["sh"] for f in fills_all)
            agg["volume"] += sum(f["cost"] for f in fills_all); agg["fees"] += sum(f["fee"] for f in fills_all)
            agg["net"] += sum(nets); agg["dd"] += dd
            agg["mk_win"] += sum(1 for x in nets if x > 0)
            agg["both"] += sum(1 for m in mk_net if m["both"])
            sp = [m["sum_px"] for m in mk_net if m["sum_px"] is not None]
            agg["sum_px"] += float(np.median(sp)) if sp else float("nan")
            agg["unhedged"] += float(np.median([m["unhedged"] for m in mk_net])) if mk_net else float("nan")
            agg["mk_cost"] += float(np.median([m["cost"] for m in mk_net])) if mk_net else 0.0
            pooled += fills_all
        out[p] = ({k: v / draws for k, v in agg.items()}, pooled)
    return out


def row(p, s):
    f, v = s["fills"], s["volume"]
    return (p, f"{s['markets']:,.0f}", f"{s['traded']:,.0f}", f"{s['orders']:,.0f}", f"{f:,.0f}",
            f"{100 * f / s['orders']:.0f}%" if s["orders"] else "—", f"{s['orders'] / max(s['traded'], 1):.1f}",
            f"${v:,.0f}", f"${s['fees']:,.0f}", f"**{s['net']:+,.2f}**", f"{100 * s['net'] / v:+.2f}%" if v else "—",
            f"{100 * s['mk_win'] / max(s['traded'], 1):.0f}%", f"{100 * s['both'] / max(s['traded'], 1):.0f}%",
            f"{s['sum_px']:.3f}" if s["sum_px"] == s["sum_px"] else "—", f"${s['dd']:,.0f}")


HEAD = ["period", "markets", "traded", "orders", "fills", "fill rate", "orders/mkt", "volume", "fees", "net $",
        "net / volume", "markets won", "both sides", "Up+Down avg px", "max DD"]


def table(rows, head):
    print("| " + " | ".join(head) + " |")
    print("|" + "|".join("---:" if i else "---" for i in range(len(head))) + "|")
    for r in rows:
        print("| " + " | ".join(str(x) for x in r) + " |")
    print()


def regimes(fills, label):
    if not fills:
        return
    print(f"**{label}: by entry price** (net $ per share, pooled draws)\n")
    rows = []
    for lo, hi in PRICE_B:
        fs = [f for f in fills if lo <= f["px"] < hi]
        if fs:
            sh = sum(f["sh"] for f in fs)
            rows.append((f"{lo:.2f}–{hi:.2f}", len(fs), f"{sum(f['won'] for f in fs) / len(fs):.3f}",
                         f"{sum(f['px'] * f['sh'] for f in fs) / sh:.3f}", f"{100 * sum(f['net'] for f in fs) / sh:+.2f}c"))
    table(rows, ["price", "fills", "win rate", "avg price", "net/share"])
    print(f"**{label}: by time left**\n")
    rows = []
    for lo, hi in LEFT_B:
        fs = [f for f in fills if lo <= f["left"] < hi or (hi == 292 and f["left"] == 292)]
        if fs:
            sh = sum(f["sh"] for f in fs)
            rows.append((f"{lo}–{hi} s", len(fs), f"{100 * sum(f['net'] for f in fs) / sh:+.2f}c"))
    table(rows, ["time left", "fills", "net/share"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=3)
    args = ap.parse_args()

    per = load(("BTC",))
    print("# FASTCLIP backtest: the x-moneyforwhiskas pattern at our speed\n")
    print("Markets (BTC, with a venue outcome): " + ", ".join(f"{p} {len(per[p]):,}" for p in PERIODS) + ".\n")

    # ── the grid, on BTC, one latency draw
    keys = list(GRID)
    results = []
    for combo in itertools.product(*GRID.values()):
        cfg = dict(zip(keys, combo))
        r = run(per, cfg, draws=1)
        results.append((cfg, {p: r[p][0] for p in PERIODS}))
    elig = [(c, s) for c, s in results if s["train"]["fills"] >= 30 and s["validation"]["fills"] >= 30]
    chosen, _ = max(elig, key=lambda cs: cs[1]["train"]["net"] + cs[1]["validation"]["net"]) if elig else results[0]
    LOCK.write_text(json.dumps({"chosen": chosen, "rule": "max train+validation net, >= 30 fills in each",
                                "grid": GRID}, indent=1))

    print("## 1. The grid (BTC, one latency draw): net $ per period\n")
    print(f"{len(results)} configurations. Chosen before test / forward were looked at: the highest train + "
          f"validation net with >= 30 fills in each (`data/replay/fastclip_locked.json`).\n")
    top = sorted(results, key=lambda cs: -(cs[1]["train"]["net"] + cs[1]["validation"]["net"]))
    rows = []
    for c, s in top[:15] + [x for x in top[-5:]]:
        tag = "**chosen**" if c == chosen else ""
        rows.append((f"{c['source']} {c['h']}ms k{c['k']:g} e{c['edge']:.2f} +{c['offset']:.2f} {tag}",
                     *(f"{s[p]['net']:+.0f} ({s[p]['fills']:.0f})" for p in PERIODS)))
    table(rows, ["config (top 15 and bottom 5 by train+validation)", *PERIODS])
    pos = {p: sum(1 for _, s in results if s[p]["net"] > 0) for p in PERIODS}
    print("Configurations with a positive net: " + ", ".join(f"{p} {pos[p]}/{len(results)}" for p in PERIODS) + ".\n")
    both_oos = sum(1 for _, s in results if s["test"]["net"] > 0 and s["forward"]["net"] > 0)
    print(f"Positive on BOTH test and forward: {both_oos}/{len(results)}.\n")

    # ── the chosen rule, several draws, and the two references
    print(f"## 2. The chosen rule: {chosen}\n")
    r = run(per, chosen, draws=args.draws)
    table([row(p, r[p][0]) for p in PERIODS], HEAD)
    print(f"Averages over {args.draws} latency draws. Up+Down avg px: the median, over markets where both sides were "
          "bought, of the average Up price plus the average Down price (the trader: 0.986).\n")
    for p in ("test", "forward"):
        regimes(r[p][1], p)

    print("## 3. The same rule with zero latency (a trader ~1 s ahead of our feed)\n")
    rz = run(per, chosen, draws=1, zero_latency=True)
    table([row(p, rz[p][0]) for p in PERIODS], HEAD)

    print("## 4. No trigger: random clips at the same size and budget (no value gate)\n")
    rr = run(per, chosen, draws=args.draws, random_rule=True)
    table([row(p, rr[p][0]) for p in PERIODS], HEAD)

    print("## 5. The chosen rule on every asset (the trader trades BTC only)\n")
    del per
    allp = load(ASSETS)
    ra = run(allp, chosen, draws=1)
    table([row(p, ra[p][0]) for p in PERIODS], HEAD)
    by = defaultdict(lambda: defaultdict(float))
    for p in ("test", "forward"):
        for f in ra[p][1]:
            by[f["asset"]][p + "_net"] += f["net"]; by[f["asset"]][p + "_vol"] += f["cost"]
    rows = [(a, *(f"{by[a][p + '_net']:+.0f} ({100 * by[a][p + '_net'] / by[a][p + '_vol']:+.1f}%)" if by[a][p + "_vol"] else "—"
                  for p in ("test", "forward"))) for a in ASSETS]
    table(rows, ["asset", "test net (of volume)", "forward net (of volume)"])


if __name__ == "__main__":
    main()
