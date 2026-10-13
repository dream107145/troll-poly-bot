"""Calibrate MOMENTUM_TAKER on the archive before choosing any threshold.

    python scripts/research_momentum.py [--charts data/charts] [--horizon 20]

Question: after an unusually fast spot move, does buying the token at the
CURRENT EXECUTABLE ASK carry positive expected value at a short horizon,
after the venue fee, the spread and a realistic quote-survival rule?

Rows are 1 s grid seconds of every archived window with a two-sided touch.
For each row and each side the spot momentum argues for:

    z_k        spot log return over k s in units of the causal 60 s sigma
    consist    fraction of the last 10 one-second steps in the move's direction
    accel      ret_5 - (ret_10 - ret_5), bps
    reprice3   change of the token mid over the last 3 s, in the move's direction
    ask_naive  the local ask at t (the dead-quote trap)
    ask_real   the local ask at t+2 s (what a taker can actually reach once the
               local book has caught up); rows where it moved more than
               ``max_slippage`` above ask_naive are "chased away" and skipped
    value_H    the token mid H seconds later
    settle     1/0 at the bell

Everything is grouped by the momentum bucket, the token's own re-pricing and
the time left, and split chronologically (first half of windows calibrates,
second half checks). No threshold is chosen here; the tables are the
evidence the configuration cites.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from troll_poly_bot.backtest.archive import load_archives   # noqa: E402
from troll_poly_bot.signals.costs import FeeSchedule        # noqa: E402

FEE = FeeSchedule()
SLIP = 0.005
LOOKBACKS = (1, 3, 5, 10, 15, 30)


def rows_for(w, horizon: int, max_slippage: float):
    g = w.grid
    n = len(g.t)
    lp = np.log(g.spot)
    out = []
    for i in range(61, n - horizon - 3):
        if not (60.0 <= g.left[i] <= 295.0):
            continue
        if np.isnan(lp[i]) or np.isnan(g.up[i]):
            continue
        r1 = np.diff(lp[i - 60:i + 1])
        r1 = r1[~np.isnan(r1)]
        if len(r1) < 30:
            continue
        sigma = float(np.std(r1))
        if sigma <= 0:
            continue
        rets = {}
        ok = True
        for k in LOOKBACKS:
            a, b = lp[i - k], lp[i]
            if np.isnan(a):
                ok = False
                break
            rets[k] = float(b - a)
        if not ok:
            continue
        z = {k: rets[k] / (sigma * math.sqrt(k)) for k in LOOKBACKS}
        direction = 1 if rets[5] > 0 else -1
        steps = np.diff(lp[i - 10:i + 1])
        consist = float(np.mean(np.sign(steps) == direction)) if len(steps) else math.nan
        accel = (rets[5] - (rets[10] - rets[5])) * 1e4
        reversing = (rets[3] * direction) < 0 and abs(z[3]) > 0.5
        side = "UP" if direction > 0 else "DOWN"
        # the token on the momentum side, at t and later
        if side == "UP":
            ask_naive, ask_real, bid = g.ua[i], g.ua[i + 2], g.ub[i]
            mid_now, mid_prev3, mid_prev10 = g.up[i], g.up[i - 3], g.up[i - 10]
            value_h = g.up[i + horizon]
            settle = 1.0 if w.up_won else 0.0
        else:
            ask_naive, ask_real, bid = g.da[i], g.da[i + 2], g.db[i]
            mid_now, mid_prev3, mid_prev10 = 1 - g.up[i], 1 - g.up[i - 3], 1 - g.up[i - 10]
            value_h = 1 - g.up[i + horizon]
            settle = 0.0 if w.up_won else 1.0
        if any(np.isnan(x) for x in (ask_naive, ask_real, bid, mid_prev3, mid_prev10, value_h)):
            continue
        if not (0.05 <= ask_naive <= 0.95):
            continue
        out.append({
            "slug": w.slug, "asset": w.asset, "t": g.t[i], "left": g.left[i], "side": side,
            "z1": z[1] * direction, "z3": z[3] * direction, "z5": z[5] * direction, "z10": z[10] * direction,
            "z15": z[15] * direction, "z30": z[30] * direction,
            "consist": consist, "accel": accel * direction, "reversing": reversing,
            "reprice3": mid_now - mid_prev3, "reprice10": mid_now - mid_prev10,
            "spread": ask_naive - bid, "ask_naive": ask_naive, "ask_real": ask_real,
            "chased": ask_real > ask_naive + max_slippage + 1e-9,
            "value_h": value_h, "settle": settle,
        })
    return out


def net(row, price_key: str, value_key: str) -> float:
    ask = row[price_key]
    return row[value_key] - ask - FEE.taker_fee_per_share(ask) - SLIP


def table(rows, title, keyfn, horizon):
    groups = defaultdict(list)
    for r in rows:
        groups[keyfn(r)].append(r)
    print(f"\n### {title}")
    print(f"| bucket | rows | windows | ask | value@{horizon}s − ask | net@{horizon}s (naive ask) | net@{horizon}s (real ask, unchased) | chased | settle net | win |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for key in sorted(groups, key=lambda k: str(k)):
        g = groups[key]
        real = [r for r in g if not r["chased"]]
        n_w = len({r["slug"] for r in g})
        v = np.mean([r["value_h"] - r["ask_naive"] for r in g])
        nn = np.mean([net(r, "ask_naive", "value_h") for r in g])
        nr = np.mean([net(r, "ask_real", "value_h") for r in real]) if real else float("nan")
        ns = np.mean([net(r, "ask_real", "settle") for r in real]) if real else float("nan")
        win = np.mean([r["settle"] for r in real]) if real else float("nan")
        print(f"| {key} | {len(g)} | {n_w} | {np.mean([r['ask_naive'] for r in g]):.3f} | {v:+.4f} | {nn:+.4f} | "
              f"{nr:+.4f} | {1 - len(real) / len(g):.0%} | {ns:+.4f} | {win:.2f} |")


def zbucket(z):
    for lo in (4, 3, 2, 1.5, 1):
        if z >= lo:
            return f"z5>={lo}"
    return "z5<1"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--charts", default="data/charts")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--max-slippage", type=float, default=0.02)
    args = ap.parse_args()
    windows = [w for w in load_archives(args.charts) if w.outcome is not None and w.covers_open]
    windows.sort(key=lambda w: w.open_ts)
    print(f"windows {len(windows)}  horizon {args.horizon}s  fee {FEE.rate} p(1-p)  slippage {SLIP}  max_slippage {args.max_slippage}")
    half = len(windows) // 2
    for name, ws in (("ALL", windows), ("FIRST HALF (calibrate)", windows[:half]), ("SECOND HALF (check)", windows[half:])):
        rows = [r for w in ws for r in rows_for(w, args.horizon, args.max_slippage)]
        print(f"\n## {name}: {len(ws)} windows, {len(rows)} rows")
        table(rows, "by 5 s momentum (all rows)", lambda r: zbucket(r["z5"]), args.horizon)
        strong = [r for r in rows if r["z5"] >= 2.0]
        table(strong, "z5>=2 by token re-pricing over the last 3 s (has the quote caught up?)",
              lambda r: "reprice3<0.01" if r["reprice3"] < 0.01 else "0.01..0.05" if r["reprice3"] < 0.05 else "reprice3>=0.05",
              args.horizon)
        table(strong, "z5>=2 by consistency of the last 10 steps",
              lambda r: "consist>=0.8" if r["consist"] >= 0.8 else "consist>=0.6" if r["consist"] >= 0.6 else "consist<0.6",
              args.horizon)
        table(strong, "z5>=2 by acceleration", lambda r: "accel>0" if r["accel"] > 0 else "accel<=0", args.horizon)
        table(strong, "z5>=2 by reversing flag", lambda r: "reversing" if r["reversing"] else "not reversing", args.horizon)
        table(strong, "z5>=2 by time left", lambda r: "60-120s" if r["left"] < 120 else "120-180s" if r["left"] < 180 else "180-295s", args.horizon)
        table(strong, "z5>=2 by spread", lambda r: "spread<=0.02" if r["spread"] <= 0.02 else "spread<=0.03" if r["spread"] <= 0.03 else "spread>0.03", args.horizon)
        table(strong, "z5>=2 by asset", lambda r: r["asset"], args.horizon)
        best = [r for r in strong if 0.01 <= r["reprice3"] < 0.05 and r["consist"] >= 0.6 and not r["reversing"]
                and r["spread"] <= 0.03 and r["left"] >= 90]
        table(best, "CANDIDATE RULE: z5>=2, quote re-priced 1-5c in 3 s, consistent, not reversing, spread<=3c, >=90 s left",
              lambda r: zbucket(r["z5"]), args.horizon)


if __name__ == "__main__":
    main()
