"""Which trigger sees the moves that move the TOKEN? Spot sigma-shock vs fair-value move (2026-09-30).

    python scripts/research_detection.py [--hours 30] > results/detection_report.md

READ-ONLY research over data/charts (the bot's own composite spot and both tokens' touch, ~200 ms).

The live trigger is a 1 s spot return >= 3 sigma. Over the 30 h before this study 78 % of fast token
moves (bid 2 s later >= ask now + 5c) had no such shock within +-2 s: near the strike the token's
sensitivity to spot is large, so a 1-sigma spot move reprices it by 5c+. The alternative judged here
triggers on the FAIR VALUE of the token moving, which is spot x sensitivity in the token's own units:

    fv = Phi( ln(S/K) / (sigma * sqrt(T_eff)) ),   T_eff = left - 40 s   (the 60 s settlement average
                                                                          has 1/3 of a point's variance)
    FV trigger:    |fv(t) - fv(t - 1 s)| >= X cents, the token's ask has moved less than half of it
    sigma trigger: |1 s spot return| >= 3 one-second sigma, the same lag condition

Both are judged at the ASK when they fire: the bid 1 / 5 / 20 s later minus that ask minus taker fees
on both legs (c/share), and how long after the trigger the token actually repriced (the race an order
~285 ms from decision to the venue has to win). DEV = first half of the hours, TEST = second half;
X is read off DEV only.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import math
import statistics as st
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.config import BotConfig                       # noqa: E402
from troll_poly_bot.pricing.vol import EwmaVol, TwoScaleVol, shock_sigma   # noqa: E402
from troll_poly_bot.signals.costs import FeeSchedule              # noqa: E402

FEE = FeeSchedule().taker_fee_per_share
ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")
XS = (3.0, 5.0, 8.0, 12.0)
DEDUPE_MS = 5_000.0


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def new_vol():
    v = BotConfig().vol
    return TwoScaleVol(fast=EwmaVol(grid_ms=v.grid_ms, halflife_s=v.halflife_s, floor_per_sec=v.floor_per_sec,
                                    ceil_per_sec=v.ceil_per_sec),
                       slow=EwmaVol(grid_ms=v.slow_grid_ms, halflife_s=v.slow_halflife_s, floor_per_sec=v.floor_per_sec,
                                    ceil_per_sec=v.ceil_per_sec), default_ratio=v.default_ratio)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=30.0)
    args = ap.parse_args()
    now = time.time() * 1000.0
    t_from = now - args.hours * 3.6e6
    t_mid = t_from + (now - t_from) / 2.0
    sig_rows: dict[str, list] = defaultdict(list)         # trigger -> [(part, mk1, mk5, mk20, react_ms)]
    for asset in ASSETS:
        files = sorted(f for f in glob.glob(str(ROOT / f"data/charts/{asset}-updown-5m-*.json"))
                       if int(f.split("-")[-1][:-5]) * 1000 > t_from)
        vol = new_vol()
        for f in files:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            K = d.get("strike") or 0.0
            pts = [p for p in d.get("points") or [] if p.get("spot") and p.get("ua") is not None and p.get("da") is not None
                   and p.get("ub") is not None and p.get("db") is not None]
            if len(pts) < 50 or K <= 0:
                continue
            t = [p["t"] for p in pts]
            lp = [math.log(p["spot"]) for p in pts]
            fv, ssig = [], []
            for p, x in zip(pts, lp):
                vol.update(p["spot"], p["t"])
                ok = vol.ready and p.get("left") is not None
                s = vol.sigma_per_sec if ok else None
                te = (p["left"] - 40.0) if ok else None
                fv.append(phi((x - math.log(K)) / (s * math.sqrt(te))) if ok and te and te > 1 else None)
                ssig.append(shock_sigma(vol) if ok else None)
            last = defaultdict(lambda: -1e18)
            for i, p in enumerate(pts):
                if not (60 <= p["left"] <= 295) or t[i] < t_from:
                    continue
                j = bisect.bisect_right(t, t[i] - 1000.0) - 1
                if j < 0 or fv[i] is None or fv[j] is None or ssig[i] is None:
                    continue
                dfv = (fv[i] - fv[j]) * 100.0                        # cents, UP token
                z = (lp[i] - lp[j]) / ssig[i]
                part = "dev" if t[i] < t_mid else "test"
                cands = [(f"fv>={x:g}c", dfv, x) for x in XS] + [("sigma>=3", z, 3.0)]
                for name, val, thr in cands:
                    if abs(val) < thr or t[i] - last[name] < DEDUPE_MS:
                        continue
                    d_ = 1 if val > 0 else -1
                    ak, bk = ("ua", "ub") if d_ > 0 else ("da", "db")
                    a0, aj = p[ak], pts[j][ak]
                    if not (0.05 <= a0 <= 0.70):
                        continue
                    # the token has not caught up: its ask moved less than half the fair-value move
                    fv_move = abs(dfv) if name.startswith("fv") else None
                    if fv_move is not None and (a0 - aj) * 100.0 >= fv_move / 2.0:
                        continue
                    if fv_move is None and (a0 - aj) * 100.0 >= 2.0:
                        continue
                    last[name] = t[i]
                    # the engine's edge gate, approximated: the side's fair value clears the ask by
                    # 2c after the entry fee (the live bot uses its TWAP pricer, blended with the mid)
                    fvs = fv[i] if d_ > 0 else 1.0 - fv[i]
                    gated = fvs - a0 - FEE(a0) >= 0.02
                    mk = []
                    for h in (1000.0, 5000.0, 20000.0):
                        k = bisect.bisect_right(t, t[i] + h) - 1
                        b = pts[k][bk] if k > i else None
                        mk.append(None if b is None else (b - a0 - FEE(a0) - FEE(b)) * 100.0)
                    # when the token's ask actually repriced (+2c) after the trigger
                    react = None
                    for k in range(i + 1, min(len(pts), bisect.bisect_right(t, t[i] + 5000.0))):
                        if pts[k][ak] >= a0 + 0.02 - 1e-9:
                            react = t[k] - t[i]
                            break
                    sig_rows[name].append((part, *mk, react))
                    if gated:
                        sig_rows[name + " + edge"].append((part, *mk, react))
    hours = (now - t_from) / 3.6e6
    out = [f"# Trigger comparison over {hours:.0f} h, {len(ASSETS)} assets\n",
           "Markouts at the ask when the trigger fires, after taker fees on both legs (mean ± 95 % CI, c/share). "
           "`reprice` = ms until the token's ask rose 2c (a real order needs ~285 ms to reach the venue): "
           "`> 300 ms` is the share an order could have beaten.\n",
           "| trigger | split | signals/h | 1 s | 5 s | 20 s | repriced within 5 s | reprice p50 | > 300 ms |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|"]

    def m(xs):
        xs = [x for x in xs if x is not None]
        if len(xs) < 2:
            return "—"
        return f"{st.mean(xs):+.1f} ±{1.96 * st.stdev(xs) / math.sqrt(len(xs)):.1f}"

    for name in ["sigma>=3", "sigma>=3 + edge"] + [n for x in XS for n in (f"fv>={x:g}c", f"fv>={x:g}c + edge")]:
        for part in ("dev", "test"):
            rows = [r for r in sig_rows[name] if r[0] == part]
            if not rows:
                continue
            re = sorted(r[4] for r in rows if r[4] is not None)
            beat = sum(1 for r in rows if r[4] is None or r[4] > 300) / len(rows)
            out.append(f"| {name} | {part.upper()} | {len(rows) / (hours / 2):.0f} | {m([r[1] for r in rows])} | "
                       f"{m([r[2] for r in rows])} | {m([r[3] for r in rows])} | {len(re) / len(rows):.0%} | "
                       f"{re[len(re) // 2] if re else '—'} | {beat:.0%} |")
    print("\n".join(out))


if __name__ == "__main__":
    main()
