"""Audit of the triangle BTC paper runs (2026-10-01) from their own records: per trigger, first-leg quality,
hedge quality, settlement P&L. Data: data/archive/triangle-btc-paper_*/ + data/ (the live run), the pm2 log
([SIGNAL] lines carry the trigger), data/charts/<slug>.json (outcomes), live_state history (200 ms token books).

usage: paper_audit.py [--json out.json]"""
from __future__ import annotations

import bisect
import glob
import json
import math
import os
import re
import statistics
import sys
import time
from collections import defaultdict

ROOT = "/root/troll-poly-bot"
LOG = os.path.expanduser("~/.pm2/logs/tpb-repricedmom-paper-error.log")
RUNS = sorted(glob.glob(f"{ROOT}/data/archive/triangle-btc-paper_*")) + [f"{ROOT}/data"]
FEE_RATE = 0.07
HORIZONS = (250, 500, 1000, 2000, 5000)          # ms; the history is sampled every ~200 ms (100 ms is not measurable)


def fee(p, n=5.0):
    return FEE_RATE * p * (1 - p) * n


def hms(ms):
    return time.strftime("%H:%M:%S", time.gmtime(ms / 1000.0))


# ------------------------------------------------------------------ the log: trigger per signal second
def load_signals():
    """(hh:mm:ss, side) -> trigger, from the [SIGNAL] lines; the day is 2026-10-01 for every run."""
    sig = {}
    rx = re.compile(r"^2026-10-01T(\d\d:\d\d:\d\d): .*\[SIGNAL\] BTC \S+ (UP|DOWN) TRIANGLE . (FLOW|HIGH ZONE|z1)(.*)$")
    for line in open(LOG, errors="replace"):
        m = rx.match(line)
        if not m:
            continue
        t, side, kind, rest = m.groups()
        trig = {"FLOW": "flow", "HIGH ZONE": "high_zone", "z1": "spot_lag"}[kind]
        score = None
        if kind == "FLOW":
            sm = re.search(r"score ([+-]\d\.\d\d)", rest)
            score = float(sm.group(1)) if sm else None
        sig[(t, side)] = (trig, score)
    return sig


def outcome(slug, cache={}):
    if slug not in cache:
        p = f"{ROOT}/data/charts/{slug}.json"
        cache[slug] = json.load(open(p)).get("outcome") if os.path.exists(p) else None
    return cache[slug]


def history(run, slug, cache={}):
    key = (run, slug)
    if key not in cache:
        p = f"{run}/live_state.triangle-btc-paper.history/{slug}.jsonl"
        pts = [json.loads(l) for l in open(p)] if os.path.exists(p) else []
        if not pts:                                    # settled windows move to data/charts (same 200 ms samples)
            c = f"{ROOT}/data/charts/{slug}.json"
            pts = json.load(open(c)).get("points") or [] if os.path.exists(c) else []
        cache[key] = ([x["t"] for x in pts], pts)
    return cache[key]


def ask_at(run, slug, side, t):
    ts, pts = history(run, slug)
    i = bisect.bisect_left(ts, t)
    if i >= len(pts):
        return None
    return pts[i].get("ua" if side == "UP" else "da")


ASK_AT_SIGNAL = {}


def load_asks(run):
    p = f"{run}/signal_outcomes.triangle-btc-paper.jsonl"
    if not os.path.exists(p):
        return
    for l in open(p):
        r = json.loads(l)
        if r.get("event") == "signal" and r.get("signal_id") and r.get("ask") is not None:
            ASK_AT_SIGNAL[r["signal_id"]] = r["ask"]


def load_run(run, sig):
    """Order ids restart at ord-000001 after every restart (several per archive), so an order is keyed by
    (market, id, creation time) and results / pairs attach to the latest creation before them."""
    created = defaultdict(list)                   # (market, id) -> [ORDER_CREATED rows, in time order]
    rows = []
    for l in open(f"{run}/decisions.triangle-btc-paper.jsonl"):
        r = json.loads(l)
        rows.append(r)
        if r.get("decision") == "ORDER_CREATED":
            created[(r["market"], r["order_id"])].append(r)
    orders, res = {}, defaultdict(list)
    for r in rows:
        d = r.get("decision")
        if d == "ORDER_CREATED":
            orders[(r["market"], r["order_id"], r["timestamp"])] = r
        elif d in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED", "ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_EXPIRED"):
            lst = created.get((r.get("market"), r["order_id"])) or [c for v in created.values() for c in v if c["order_id"] == r["order_id"]]
            prior = [c for c in lst if c["timestamp"] <= r["timestamp"] + 1.0]
            if prior:
                c = prior[-1]
                res[(c["market"], c["order_id"], c["timestamp"])].append(r)
    pairs = {}
    for key, o in orders.items():
        if o.get("pair_of"):
            lst = [c for c in created.get((o["market"], o["pair_of"]), []) if c["timestamp"] <= o["timestamp"] + 1.0]
            if lst:
                c = lst[-1]
                pairs[(c["market"], c["order_id"], c["timestamp"])] = (key, o)
    entries = []
    for key, o in orders.items():
        if o.get("pair_of"):
            continue
        oid = key
        t = o["timestamp"]
        trig, score = sig.get((hms(t), o["side"]), (None, None))
        if trig is None:                               # the log second may be the one before
            trig, score = sig.get((hms(t - 1000), o["side"]), ("unknown", None))
        fr = [x for x in res[oid] if x["decision"] in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED")]
        kill = [x for x in res[oid] if x["decision"] == "ORDER_REJECTED"]
        e = {"run": run, "id": o["order_id"], "slug": o["market"], "side": o["side"], "t": t, "limit": o["price"], "trig": trig,
             "score": score, "filled": bool(fr), "fill_px": fr[0]["price"] if fr else None,
             "fill_t": fr[0]["timestamp"] if fr else None, "kill": kill[0].get("reason") if kill else None,
             "outcome": outcome(o["market"])}
        e["ask_at_signal"] = ASK_AT_SIGNAL.get(o.get("signal_id"))
        p = pairs.get(oid)
        if p:
            pid, po = p
            pf = [x for x in res[pid] if x["decision"] == "ORDER_FILLED"]
            e["pair"] = {"price": po["price"], "t": po["timestamp"], "filled": bool(pf),
                         "fill_t": pf[0]["timestamp"] if pf else None,
                         "opp_ask_at_entry": ask_at(run, o["market"], po["side"], e["fill_t"] or t)}
        else:
            e["pair"] = None
        # the token's ask after the first leg (what the bought side did next)
        if e["fill_t"]:
            base = ask_at(run, e["slug"], e["side"], e["fill_t"])
            e["ret"] = {}
            for h in HORIZONS:
                a = ask_at(run, e["slug"], e["side"], e["fill_t"] + h)
                e["ret"][h] = None if (a is None or base is None) else round(a - base, 4)
        entries.append(e)
    return entries


def pnl_of(e):
    """Settlement P&L of the first leg (and the hedge) in USDC, 5 shares."""
    if not e["filled"] or e["outcome"] is None:
        return None, None
    won = e["outcome"].upper() == e["side"]
    leg1 = (5.0 if won else 0.0) - 5.0 * e["fill_px"] - fee(e["fill_px"])
    hedge = None
    if e["pair"] and e["pair"]["filled"]:
        hedge = (0.0 if won else 5.0) - 5.0 * e["pair"]["price"]            # maker: no fee in this schedule
    return leg1, hedge


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else float("nan")


def table(entries, label):
    n = len(entries)
    f = [e for e in entries if e["filled"]]
    settled = [e for e in f if e["outcome"] is not None]
    l1 = [pnl_of(e)[0] for e in settled]
    hed = [pnl_of(e)[1] for e in settled if e["pair"] and e["pair"]["filled"]]
    with_pair = [e for e in f if e["pair"]]
    hfill = [e for e in with_pair if e["pair"]["filled"]]
    tth = [(e["pair"]["fill_t"] - e["pair"]["t"]) / 1000.0 for e in hfill]
    pc = [e["fill_px"] + e["pair"]["price"] for e in hfill]
    exe = [e["fill_px"] + e["pair"]["opp_ask_at_entry"] for e in with_pair if e["pair"]["opp_ask_at_entry"]]
    comp = [sum(pnl_of(e)) for e in settled if e["pair"] and e["pair"]["filled"]]
    naked = [pnl_of(e)[0] for e in settled if not (e["pair"] and e["pair"]["filled"])]
    wins = [x for x in l1 if x > 0]
    out = {"label": label, "signals": n, "fills": len(f), "fill_rate": len(f) / n if n else None,
           "avg_entry": mean([e["fill_px"] for e in f]),
           "avg_paid_over_ask": mean([e["fill_px"] - e["ask_at_signal"] for e in f if e["ask_at_signal"]]),
           "ret_ms": {h: mean([e["ret"].get(h) for e in f if e.get("ret")]) for h in HORIZONS},
           "settled": len(settled), "leg1_pnl": sum(l1), "leg1_per_fill": mean(l1), "win_rate": len(wins) / len(l1) if l1 else None,
           "fees": sum(fee(e["fill_px"]) for e in f),
           "pairs_placed": len(with_pair), "pairs_filled": len(hfill), "hedge_fill_rate": len(hfill) / len(with_pair) if with_pair else None,
           "time_to_hedge_s_p50": statistics.median(tth) if tth else None, "time_to_hedge_s_p90": sorted(tth)[int(0.9 * len(tth))] if tth else None,
           "pair_cost_completed": mean(pc), "executable_pair_cost_at_entry": mean(exe),
           "exe_le_095": sum(1 for x in exe if x <= 0.95) / len(exe) if exe else None,
           "completed_pairs": len(comp), "completed_pnl": sum(comp), "completed_per_pair": mean(comp),
           "naked_n": len(naked), "naked_pnl": sum(naked), "naked_per_leg": mean(naked),
           "net_pnl": sum(l1) + sum(hed)}
    return out


def fmt(o):
    g = lambda v, f="{:+.2f}": "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f.format(v)   # noqa: E731
    r = o["ret_ms"]
    return (f"{o['label']:36} sig {o['signals']:4} fills {o['fills']:4} ({g(o['fill_rate'], '{:.0%}')}) entry {g(o['avg_entry'], '{:.3f}')} "
            f"paid {g(o['avg_paid_over_ask'], '{:+.3f}')} over ask | ask after fill: "
            + " ".join(f"{h / 1000:g}s {g(r[h], '{:+.3f}')}" for h in HORIZONS)
            + f"\n{'':36} settled {o['settled']:4} leg1 {g(o['leg1_pnl'])} ({g(o['leg1_per_fill'])}/fill, win {g(o['win_rate'], '{:.0%}')}) fees {g(o['fees'])}"
            f" | hedges {o['pairs_filled']}/{o['pairs_placed']} ({g(o['hedge_fill_rate'], '{:.0%}')}) t50 {g(o['time_to_hedge_s_p50'], '{:.1f}s')} t90 {g(o['time_to_hedge_s_p90'], '{:.1f}s')}"
            f" pair cost {g(o['pair_cost_completed'], '{:.3f}')} (executable at entry {g(o['executable_pair_cost_at_entry'], '{:.3f}')}, <=0.95: {g(o['exe_le_095'], '{:.0%}')})"
            f"\n{'':36} completed {o['completed_pairs']:4} {g(o['completed_pnl'])} ({g(o['completed_per_pair'])}/pair) | naked {o['naked_n']:4} {g(o['naked_pnl'])} ({g(o['naked_per_leg'])}/leg) | NET {g(o['net_pnl'])}")


def main():
    sig = load_signals()
    entries = []
    for run in RUNS:
        if os.path.exists(f"{run}/decisions.triangle-btc-paper.jsonl"):
            load_asks(run)
            entries += load_run(run, sig)
    entries.sort(key=lambda e: e["t"])
    print(f"{len(entries)} first-leg orders across {len(RUNS)} runs; triggers: "
          + ", ".join(f"{k} {v}" for k, v in sorted(__import__('collections').Counter(e['trig'] for e in entries).items())))
    eras = [("spot-lag sigma era 02:05-14:01 (all gates)", lambda e: e["trig"] == "spot_lag" and e["t"] < 1790863260000),
            ("spot-lag, all sudden moves 14:01-14:36", lambda e: e["trig"] == "spot_lag" and e["t"] >= 1790863260000),
            ("TRIGGER 1 flow signal (14:36 ->)", lambda e: e["trig"] == "flow"),
            ("TRIGGER 2 high zone (15:19 ->)", lambda e: e["trig"] == "high_zone"),
            ("unknown trigger", lambda e: e["trig"] == "unknown")]
    rows = []
    for label, pred in eras:
        es = [e for e in entries if pred(e)]
        if not es:
            continue
        o = table(es, label)
        rows.append(o)
        print(fmt(o))
    # flow: by score strength and by time left
    flow = [e for e in entries if e["trig"] == "flow"]
    if flow:
        print("\nTRIGGER 1 by |score|:")
        for lo, hi in ((0.70, 0.75), (0.75, 0.85), (0.85, 1.5)):
            es = [e for e in flow if e["score"] is not None and lo <= abs(e["score"]) < hi]
            if es:
                print(fmt(table(es, f"  |score| {lo:.2f}-{hi:.2f}")))
        print("\nTRIGGER 1 by seconds left at the signal:")
        for lo, hi in ((240, 300), (180, 240), (120, 180), (60, 120)):
            es = [e for e in flow if lo <= (int(e["slug"].rsplit("-", 1)[1]) * 1000 + 300_000 - e["t"]) / 1000 < hi]
            if es:
                print(fmt(table(es, f"  {lo}-{hi} s left")))
    # max drawdown of the flow era on settled leg1+hedge
    for label, trig in (("TRIGGER 1", "flow"), ("TRIGGER 2", "high_zone")):
        es = [e for e in entries if e["trig"] == trig and e["filled"] and e["outcome"]]
        es.sort(key=lambda e: e["fill_t"])
        eq = peak = dd = 0.0
        for e in es:
            l1, h = pnl_of(e)
            eq += l1 + (h or 0.0)
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        print(f"\n{label}: cumulative net {eq:+.2f} over {len(es)} settled fills, max drawdown {dd:+.2f}")
    flow_f = [e for e in entries if e["trig"] == "flow" and e["filled"] and e["outcome"]]
    if flow_f:
        print("\nEXECUTABLE PAIR-COST GATE applied to the actual TRIGGER 1 fills (hedge = FOK of the other side at its ask + 3c,"
              " landing 800 ms after the fill; gate uses the asks at the decision + both fees + 0.03 slippage):")
        print(f"{'max_pair_cost':14}{'pass':>6}{'of':>5}{'hedged':>8}{'naked':>7}{'pair cost':>11}{'net':>9}{'per pass':>10}{'| today (resting) net':>22}")
        for mpc in (0.93, 0.94, 0.95, 0.96, 0.97, 1.05):
            passed = hedged = 0; net = 0.0; pcs = []
            for e in flow_f:
                a0 = e["ask_at_signal"]; o0 = ask_at(e["run"], e["slug"], "DOWN" if e["side"] == "UP" else "UP", e["t"])
                if a0 is None or o0 is None or a0 + o0 + fee(a0) / 5 + fee(o0) / 5 + 0.03 > mpc + 1e-9:
                    continue
                passed += 1
                l1, _ = pnl_of(e)
                won = e["outcome"].upper() == e["side"]
                other = "DOWN" if e["side"] == "UP" else "UP"
                o1, o2 = ask_at(e["run"], e["slug"], other, e["fill_t"]), ask_at(e["run"], e["slug"], other, e["fill_t"] + 800.0)
                if o1 is not None and o2 is not None and o2 <= min(o1 + 0.03, 0.99) + 1e-9:
                    hedged += 1
                    net += l1 + (0.0 if won else 5.0) - 5.0 * o2 - fee(o2)
                    pcs.append(e["fill_px"] + o2)
                else:
                    net += l1
            today = sum((pnl_of(e)[0] or 0.0) + (pnl_of(e)[1] or 0.0) for e in flow_f)
            print(f"{mpc:<14.2f}{passed:>6}{len(flow_f):>5}{hedged:>8}{passed - hedged:>7}{mean(pcs):>11.3f}{net:>+9.2f}{(net / passed if passed else float('nan')):>+10.3f}{today:>+22.2f}")
    if "--json" in sys.argv:
        json.dump({"rows": rows, "entries": entries}, open(sys.argv[sys.argv.index("--json") + 1], "w"), default=str)


main()
