"""Tables for the swing-recovery sweep report from results/pairlock_swing_sweep.json (scripts/pairlock/replay_swing_sweep.py).

    .venv/bin/python scripts/pairlock/swing_sweep_report.py [results/pairlock_swing_sweep.json] > tables.md
"""
from __future__ import annotations

import json
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/root/troll-poly-bot/results/pairlock_swing_sweep.json"
D = json.load(open(PATH))
CANDS = [c for c in D["candidates"] if c["stress"] == "none"]
BASE = D["baselines"]["A current (10/24/80, hedge 100%, cap 125)"]["cand"]


def f(x, nd=3, sign=True):
    if x is None or x == "":
        return "—"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def find(**kw):
    """The no-stress candidates equal to the baseline except ``kw``."""
    want = {**BASE, **kw}
    return next((c for c in CANDS if all(c["cand"][k] == v for k, v in want.items())), None)


def short(c):
    s2 = f"{c['s2_mult']:g}x" if c["s2_mult"] else f"{c['s2']:g}"
    h = f"{c['hedge_ratio']:.0%}" + (f" {c['hedge_mode']}" if 0 < c["hedge_ratio"] < 1 else "") + (f" ≤{c['hedge_max']:.2f}" if c["hedge_max"] else "")
    r = (("≤" + (f"{c['rec_max']:.2f}" if c["rec_max"] else "") + (" BE" if c["rec_dynamic"] else "") + f" {c['rec_policy']}")
         if c["rec_max"] or c["rec_dynamic"] else "—")
    return {"entry": f"{c['entry_start']:g}–{c['entry_end']:g}s {c['entry_min']:.2f}–{c['entry_max']:.2f}", "s1": f"{c['s1']:g}",
            "s2": s2, "trig": f"{c['trigger']:.2f}", "hyst": f"{c['hysteresis']:.2f}", "hedge": h, "rec": r, "cap": f"{c['capital']:g}"}


def per_line(per):
    return " | ".join(f(per[p]["pnl_per_market"]) for p in ("train", "validation", "test", "all"))


def table_top():
    print("| Rank | Entry | S1 | S2 | Trigger | Hyst | Hedge | Recovery limit | Cap | Score | Train /mkt | Valid. /mkt | Test /mkt "
          "| All /mkt | Test P&L | Max DD | P95 capital | Neighbours >0 |")
    print("|---:|---|--:|--:|--:|--:|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for t in D["top20"]:
        s, p = short(t["cand"]), t["per"]
        nb = t.get("neighbourhood") or {}
        print(f"| {t['rank']} | {s['entry']} | {s['s1']} | {s['s2']} | {s['trig']} | {s['hyst']} | {s['hedge']} | {s['rec']} | {s['cap']} "
              f"| {f(t['score'])} | {per_line(p)} | {f(p['test']['total_pnl'], 1)} | {f(p['all']['max_drawdown'], 0)} "
              f"| {f(p['all']['p95_capital'], 0, False)} | {f((nb.get('positive_share') or 0) * 100, 0, False)}% of {nb.get('neighbours', 0)} |")


def table_baselines():
    print("| Baseline | Score | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Total | Traded | Win % | Max DD | Worst | P95 cap | "
          "S0 | S1 | S2 | S3+ (markets: P&L) |")
    print("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|---|---|---|")
    for name, b in D["baselines"].items():
        a = b["per"]["all"]
        bs = a["by_swing"]
        print(f"| {name} | {f(b['score'])} | {per_line(b['per'])} | {f(a['total_pnl'], 1)} | {a['traded']} | {a['win_rate']:.1%} "
              f"| {f(a['max_drawdown'], 0)} | {f(a['worst_market'], 1)} | {f(a['p95_capital'], 0, False)} | "
              + " | ".join(f"{bs[k]['markets']}: {f(bs[k]['pnl'], 0)}" for k in ("0", "1", "2", "3+")) + " |")


def detail(name, c):
    p = c["per"]
    print(f"\n**{name}**: `{c.get('label') or short(c['cand'])}`\n")
    print("| Period | Markets | Traded | P&L | /market | /traded | Median | Win % | Max DD | Worst | P1 | P5 | Losing streak | Avg cap | P95 cap |")
    print("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for per in ("train", "validation", "test", "all"):
        a = p[per]
        print(f"| {per} | {a['markets']} | {a['traded']} | {f(a['total_pnl'], 1)} | {f(a['pnl_per_market'])} | {f(a['pnl_per_traded'])} "
              f"| {f(a['median_pnl'], 2)} | {a['win_rate']:.1%} | {f(a['max_drawdown'], 0)} | {f(a['worst_market'], 1)} | {f(a['p01_pnl'], 1)} "
              f"| {f(a['p05_pnl'], 1)} | {a['max_losing_streak']} | {f(a['avg_capital'], 1, False)} | {f(a['p95_capital'], 0, False)} |")
    a = p["all"]
    print("\n| Bucket | Markets | Share | P&L | Per market |")
    print("|---|--:|--:|--:|--:|")
    for k in ("0", "1", "2", "3+"):
        b = a["by_swing"][k]
        print(f"| Swing {k} | {b['markets']} | {(b['share'] or 0):.1%} | {f(b['pnl'], 1)} | {f(b['avg'], 2)} |")
    print("\n| Order class | Orders | Fill rate | Partial | Shares | Avg VWAP | Avg trigger ask |")
    print("|---|--:|--:|--:|--:|--:|--:|")
    trig = {"s1": a.get("avg_s1_trigger_ask"), "s2": a.get("avg_s2_trigger_ask"), "hedge": a.get("avg_s3_trigger_ask"), "entry": None}
    for k, lab in (("entry", "entry"), ("s1", "swing 1"), ("s2", "swing 2"), ("hedge", "hedge")):
        print(f"| {lab} | {a[k + '_orders']:.0f} | {f(a[k + '_fill_rate'], 2, False) if a[k + '_fill_rate'] is not None else '—'} "
              f"| {a[k + '_partial']:.0f} | {a[k + '_qty']:.0f} | {f(a['avg_' + k + '_vwap'], 4, False)} | {f(trig[k], 4, False)} |")
    print(f"\nEntry: {a['entry_opportunity_markets']} markets offered an entry, {a['traded']} filled, {a['missed_entries']} missed "
          f"(killed / not filled); capital-bound markets {a['capital_bound_markets']}; P&L per USDC of capital {f(a['pnl_per_dollar'], 4)}; "
          f"max inventory {a['max_inventory']:.0f} sh; first leg alone {f(a['first_leg_alone_pnl'], 1)}.")


def slice_table(title, rows):
    print(f"\n**{title}**\n")
    print("| Value | Train /mkt | Valid. /mkt | Test /mkt | All /mkt | Traded | S1 VWAP | S2 VWAP | Hedge VWAP | Max DD | Score |")
    print("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for lab, c in rows:
        if c is None:
            continue
        a = c["per"]["all"]
        print(f"| {lab} | {per_line(c['per'])} | {a['traded']} | {f(a['avg_s1_vwap'], 3, False)} | {f(a['avg_s2_vwap'], 3, False)} "
              f"| {f(a['avg_hedge_vwap'], 3, False)} | {f(a['max_drawdown'], 0)} | {f(c['score'])} |")


def sensitivity():
    slice_table("Swing 1 size (swing 2 = 80, hedge 100 %)", [(f"{v:g}", find(s1=v)) for v in D["grid"]["s1"]])
    slice_table("Swing 2 size (swing 1 = 24)", [(f"{v:g}", find(s2=v)) for v in D["grid"]["s2"]])
    slice_table("Swing 2 = multiple of swing 1 = 24", [(f"{v:g}x", find(s2_mult=v)) for v in D["grid"]["s2_mult"]])
    slice_table("Swing trigger (hysteresis 0.04)", [(f"{v:.2f}", find(trigger=v)) for v in D["grid"]["trigger"]])
    slice_table("Hysteresis (trigger 0.70)", [(f"{v:.2f}", find(hysteresis=v)) for v in D["grid"]["hysteresis"]])
    slice_table("Hedge ratio (no hedge price limit; 'once')", [(f"{v:.0%}", find(hedge_ratio=v)) for v in D["grid"]["hedge_ratio"]])
    slice_table("Hedge price limit (ratio 100 %)", [(f"{v:.2f}" if v else "none", find(hedge_max=v)) for v in D["grid"]["hedge_max"]])
    slice_table("Recovery price limit (VWAP, reduce)", [(f"{v:.2f}", find(rec_max=v)) for v in D["grid"]["rec_max"]]
                + [("break-even only", find(rec_dynamic=True))] + [(f"min({v:.2f}, break-even)", find(rec_max=v, rec_dynamic=True))
                                                                   for v in (0.68, 0.70, 0.72)])
    slice_table("Recovery price limit (VWAP, reject)", [(f"{v:.2f}", find(rec_max=v, rec_policy="reject")) for v in D["grid"]["rec_max"]])
    slice_table("Capital cap", [(f"{v:g}", find(capital=v)) for v in D["grid"]["capital"]])
    slice_table("Entry start (window 10 s, band 0.55–0.60)", [(f"{v:g}s", find(entry_start=v, entry_end=v + 10)) for v in D["grid"]["entry_start"]])
    slice_table("Entry band (20–30 s)", [(f"{a:.2f}–{b:.2f}", find(entry_min=a, entry_max=b)) for a in D["grid"]["entry_min"]
                                          for b in D["grid"]["entry_max"] if a < b])


def stress():
    print("| Rank | None | " + " | ".join(n for n in D["top20"][0]["stress"]) + " |")
    print("|---:|--:|" + "--:|" * len(D["top20"][0]["stress"]))
    for t in D["top20"][:10]:
        print(f"| {t['rank']} | {f(t['per']['all']['pnl_per_market'])} | "
              + " | ".join(f(v["pnl_per_market"]) for v in t["stress"].values()) + " |")


if __name__ == "__main__":
    print(f"Data: {D['markets']} markets {D['first']} .. {D['last']} UTC (last {D.get('last_market')}); periods {D['periods']}; "
          f"{len(CANDS)} parameter sets + {len(D['candidates']) - len(CANDS)} stress runs in {D['seconds'] / 60:.0f} min.\n")
    print("## Baselines\n")
    table_baselines()
    print("\n## Top 20\n")
    table_top()
    detail("Baseline A", {"per": D["baselines"]["A current (10/24/80, hedge 100%, cap 125)"]["per"], "cand": BASE,
                          "label": "current strategy"})
    detail("Rank 1", D["top20"][0])
    print("\n## Sensitivity\n")
    sensitivity()
    print("\n## Stress (P&L per market, all periods)\n")
    stress()
