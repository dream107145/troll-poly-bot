"""Which of the 7 five-minute Up/Down markets suits the pair-lock RECOVERY bot? (2026-10-05)

The bot buys the higher-priced token at the open and rests a bid for the other token at PAIR_SUM - the fill. It
earns when the open leader stays ahead (the bid fills: a locked pair); it loses when the lead flips back and forth
(each flip is a recovery buy, each roughly double the last). So a good market for it is one where the lead FLIPS
LITTLE -- above all late -- and the book is tight and deep enough for the recovery steps.

Per market (the 200 ms window archive, data/replay/paired_exit/<asset>.npz, the common period of all 7):
  * open leader wins      the token with the higher price 1 s after the open is the winner
  * pair fills first      the open rule: buy the leader at its ask, bid the other at floor(0.95 - ask); the bid fills
                          (the other's ask trades strictly through it) before the leader's price is first overtaken
  * overtaken first       the other token's mid rises above the leader's before the bid fills (the bot pulls the bid
                          and the recovery starts)
  * swings                lead changes of >= 5c: the UP mid crosses from >= 0.55 to <= 0.45 or back (the bot's
                          MIN_PRICE_MOVE); 3+ swings = the chains behind the big losses; late = in the last 60 s
  * spread / depth / jumpiness   median touch spread (c), near-touch ask depth (shares), median |1 s mid move| (c)

    .venv/bin/python scripts/market_stability.py > results/market_stability_2026-10-05.md
"""
from __future__ import annotations

import math
import sys
import time

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L   # noqa: E402

PAIR_SUM = 0.95
SWING = 0.05


def window_stats(w):
    mu = L.mid_of(w.ua, w.ub)
    md = L.mid_of(w.da, w.db)
    mid = np.where(np.isnan(mu), 1.0 - md, np.where(np.isnan(md), mu, (mu + 1.0 - md) / 2.0))
    T, left = w.T, w.left
    i0 = int(np.searchsorted(T, w.open + 1000.0))
    if i0 >= w.n - 50 or math.isnan(mid[i0]):
        return None
    lead = "UP" if mid[i0] >= 0.5 else "DOWN"
    if abs(mid[i0] - 0.5) < 1e-9:
        lead = "UP" if (w.ub[i0] or 0) >= (w.db[i0] or 0) else "DOWN"
    ask_l = (w.ua if lead == "UP" else w.da)[i0]
    ask_o = w.da if lead == "UP" else w.ua
    r = {"lead_wins": w.outcome == lead, "open_price": float(ask_l)}
    # the pair bid and the first overtaking
    t_fill = t_over = None
    if not math.isnan(ask_l) and 0.50 <= ask_l <= 0.95:
        bid = math.floor((PAIR_SUM - ask_l) * 100.0 + 1e-9) / 100.0
        m = (ask_o[i0 + 1:] < bid - 1e-9) & ~np.isnan(ask_o[i0 + 1:])
        if bid >= 0.01 and m.any():
            t_fill = T[i0 + 1 + int(np.argmax(m))]
    held = mid if lead == "UP" else 1.0 - mid
    ov = (held[i0 + 1:] < 0.5 - 1e-9) & ~np.isnan(held[i0 + 1:])
    if ov.any():
        t_over = T[i0 + 1 + int(np.argmax(ov))]
    r["eligible"] = not math.isnan(ask_l) and 0.50 <= ask_l <= 0.95
    r["pair_first"] = t_fill is not None and (t_over is None or t_fill < t_over)
    r["overtaken_first"] = t_over is not None and (t_fill is None or t_over <= t_fill)
    # swings of >= 5c around 0.5
    state, swings, late = None, 0, 0
    for i in range(i0, w.n):
        x = mid[i]
        if math.isnan(x) or left[i] < 10.0:
            continue
        s = "UP" if x >= 0.5 + SWING else "DOWN" if x <= 0.5 - SWING else None
        if s is None:
            continue
        if state is not None and s != state:
            swings += 1
            late += int(left[i] < 60.0)
        state = s
    r["swings"], r["late_swings"] = swings, late
    sp = np.concatenate([(w.ua - w.ub)[i0:], (w.da - w.db)[i0:]])
    r["spread"] = float(np.nanmedian(sp)) * 100.0
    r["depth"] = float(np.nanmedian(np.concatenate([w.uad[i0:], w.dad[i0:]])))
    j = np.searchsorted(T, T[i0:] - 1000.0)
    d1 = np.abs(mid[i0:] - mid[np.maximum(j, 0)]) * 100.0
    r["jump"] = float(np.nanmedian(d1))
    r["jump_p95"] = float(np.nanpercentile(d1, 95))
    return r


def main():
    data = {a: L.load_asset(a) for a in L.ASSETS}
    lo = max(min(w.open for w in ws) for ws in data.values())
    hi = min(max(w.open for w in ws) for ws in data.values())
    print("# Which 5-minute market suits the pair-lock recovery bot?\n")
    fmt = lambda ms: time.strftime('%m-%d %H:%M', time.gmtime(ms / 1000))   # noqa: E731
    print(f"Common period of all 7 markets: {fmt(lo)} - {fmt(hi)} UTC. "
          f"Open rule as the bot (higher token 1 s after the open, ask 0.50-0.95, bid at {PAIR_SUM} - ask). "
          f"A swing = the UP price crossing from >= {0.5 + SWING:.2f} to <= {0.5 - SWING:.2f} or back.\n")
    rows = []
    for a, ws in data.items():
        st = [s for s in (window_stats(w) for w in ws if lo <= w.open <= hi) if s is not None]
        el = [s for s in st if s["eligible"]]
        n = len(st)
        sw = np.array([s["swings"] for s in st])
        rows.append({"asset": a, "n": n, "lead_wins": np.mean([s["lead_wins"] for s in st]),
                     "pair_first": np.mean([s["pair_first"] for s in el]) if el else float("nan"),
                     "over_first": np.mean([s["overtaken_first"] for s in el]) if el else float("nan"),
                     "swings": sw.mean(), "sw2": np.mean(sw >= 2), "sw3": np.mean(sw >= 3),
                     "late": np.mean([s["late_swings"] > 0 for s in st]),
                     "spread": np.median([s["spread"] for s in st]), "depth": np.median([s["depth"] for s in st]),
                     "jump": np.median([s["jump"] for s in st]), "jump95": np.median([s["jump_p95"] for s in st])})
    print("| market | windows | open leader wins | pair bid fills first | overtaken first (recovery) | swings / window | "
          "2+ swings | 3+ swings | a swing in the last 60 s | spread c | near-touch ask depth | median 1 s move c | 95th pct 1 s move c |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in sorted(rows, key=lambda r: (r["sw3"], -r["pair_first"])):
        print(f"| {r['asset']} | {r['n']} | {r['lead_wins']:.0%} | {r['pair_first']:.0%} | {r['over_first']:.0%} | "
              f"{r['swings']:.2f} | {r['sw2']:.0%} | {r['sw3']:.0%} | {r['late']:.0%} | {r['spread']:.1f} | {r['depth']:.0f} | "
              f"{r['jump']:.2f} | {r['jump95']:.2f} |")
    print("\nSorted by the share of windows with 3+ swings (the chains behind the big losses), then by pair fills.")


if __name__ == "__main__":
    main()
