"""Every REAL spot-lag entry order, followed through the archive: was it sent at the wrong time? (2026-09-30)

    python scripts/research_live_orders.py [--out data/replay/live_orders.json] > results/live_orders_report.md

READ-ONLY research over data/trade_forensics.jsonl (mode LIVE-ARMED), data/trade_journal.jsonl and
data/charts/<slug>.json (both tokens' best bid / ask every ~200 ms).

For each order, on the token it bought:
    pre_1s / pre_3s   how far the ask had ALREADY moved in our favour over the 1 s / 3 s before the
                      decision (cents): large = we bought after the market repriced
    mk_<h>            the bid h seconds after the decision minus the decision ask, minus taker fees on
                      both legs (cents per share): what a fill at the decision ask would have made
    mk_settle         settlement (1 / 0) minus the decision ask minus the entry fee
Filled orders also carry their real round-trip P&L per share from the journal.

Periods: DEV = before 2026-09-29 00:00 UTC, TEST = from then on. Any rule read off DEV is judged on TEST.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from troll_poly_bot.signals.costs import FeeSchedule     # noqa: E402

FEE = FeeSchedule()
TEST_FROM = 1790640000000.0          # 2026-09-29 00:00 UTC
HORIZONS = (1, 5, 20, 60)


def fee(p: float) -> float:
    return FEE.taker_fee_per_share(p) if p and 0 < p < 1 else 0.0


def load_orders():
    rows = []
    for line in open(ROOT / "data/trade_forensics.jsonl", encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") == "trade" and r.get("kind") == "entry" and r.get("mode") == "LIVE-ARMED":
            rows.append(r)
    journal = {}
    for line in open(ROOT / "data/trade_journal.jsonl", encoding="utf-8"):
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        if j.get("mode") == "LIVE-ARMED":
            journal[j["trade_id"]] = j
    return rows, journal


class Path_:
    def __init__(self, doc: dict, side: str) -> None:
        b, a = ("ub", "ua") if side == "UP" else ("db", "da")
        pts = [p for p in doc.get("points") or [] if p.get(b) is not None and p.get(a) is not None]
        self.t = [p["t"] for p in pts]
        self.bid = [p[b] for p in pts]
        self.ask = [p[a] for p in pts]
        self.spot = [p.get("spot") for p in pts]

    def at(self, arr, t: float):
        i = bisect.bisect_right(self.t, t) - 1
        if i < 0 or (t - self.t[i]) > 2_000.0:
            return None
        return arr[i]


def analyse(rows, journal):
    cache: dict[str, dict | None] = {}
    out = []
    for r in rows:
        slug, side = r.get("slug"), r.get("side")
        t = r.get("t4_decision") or r.get("t2_signal")
        ask0 = r.get("best_ask")
        if not slug or side not in ("UP", "DOWN") or not t or not ask0:
            continue
        if slug not in cache:
            try:
                cache[slug] = json.loads((ROOT / f"data/charts/{slug}.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cache[slug] = None
        doc = cache[slug]
        if doc is None:
            continue
        p = Path_(doc, side)
        if len(p.t) < 20:
            continue
        a1, a3 = p.at(p.ask, t - 1_000.0), p.at(p.ask, t - 3_000.0)
        rec = {"id": r["order_id"], "t": t, "asset": r.get("asset"), "side": side, "ask": ask0,
               "limit": r.get("limit_price"), "left": r.get("seconds_left"), "z": abs(r.get("momentum_z") or 0.0),
               "spread": r.get("spread"), "book_age": r.get("quote_age_ms"), "spot_move": abs(r.get("spot_move_bps") or 0.0),
               "reprice": r.get("market_reprice"),
               "pre_1s": None if a1 is None else (ask0 - a1) * 100.0,
               "pre_3s": None if a3 is None else (ask0 - a3) * 100.0,
               "filled": r.get("execution_price") is not None, "fill": r.get("execution_price"),
               "period": "test" if t >= TEST_FROM else "dev",
               "hour": int((t / 3.6e6) % 24)}
        cost_in = fee(ask0)
        for h in HORIZONS:
            b = p.at(p.bid, t + h * 1000.0)
            rec[f"mk_{h}"] = None if b is None else (b - ask0 - cost_in - fee(b)) * 100.0
        outcome = str(doc.get("outcome") or "").upper()
        if outcome in ("UP", "DOWN"):
            rec["mk_settle"] = ((1.0 if outcome == side else 0.0) - ask0 - cost_in) * 100.0
        else:
            rec["mk_settle"] = None
        j = journal.get(r["order_id"])
        if j and j.get("quantity"):
            rec["real_c"] = j["realized_pnl"] / j["quantity"] * 100.0
            rec["real_usd"] = j["realized_pnl"]
            rec["exit_reason"] = j.get("exit_reason")
        out.append(rec)
    return out


def m(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "—"
    mu = st.mean(xs)
    se = st.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else float("nan")
    return f"{mu:+.1f} ±{1.96 * se:.1f}" if len(xs) > 1 else f"{mu:+.1f}"


def table(recs, key, buckets, title, out):
    out.append(f"### {title}\n")
    out.append("| bucket | orders | filled | fill rate | would-be fill, 5 s | 20 s | 60 s | settle | real fills ¢/share | real USDC |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, lo, hi in buckets:
        sub = [r for r in recs if r.get(key) is not None and lo <= r[key] < hi]
        if not sub:
            continue
        fl = [r for r in sub if r["filled"]]
        out.append(f"| {label} | {len(sub)} | {len(fl)} | {len(fl) / len(sub):.0%} | {m([r['mk_5'] for r in sub])} | "
                   f"{m([r['mk_20'] for r in sub])} | {m([r['mk_60'] for r in sub])} | {m([r['mk_settle'] for r in sub])} | "
                   f"{m([r.get('real_c') for r in fl])} | {sum(r.get('real_usd') or 0 for r in fl):+.2f} |")
    out.append("")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data/replay/live_orders.json"))
    args = ap.parse_args()
    rows, journal = load_orders()
    recs = analyse(rows, journal)
    Path(args.out).write_text(json.dumps(recs), encoding="utf-8")
    out = ["# Real spot-lag orders, followed through the archive\n",
           f"{len(recs)} of {len(rows)} real entry orders matched to an archived window; "
           f"{sum(r['filled'] for r in recs)} filled. Markouts are cents per share after taker fees on both legs, "
           "for a fill at the DECISION ask (mean ± 95 % CI). `real` = the actual round trips.\n"]
    out.append("## Filled vs killed (adverse selection)\n")
    out.append("| orders | n | 1 s | 5 s | 20 s | 60 s | settle |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for label, sub in (("filled", [r for r in recs if r["filled"]]), ("killed", [r for r in recs if not r["filled"]]),
                       ("all", recs)):
        out.append(f"| {label} | {len(sub)} | " + " | ".join(m([r[f'mk_{h}'] for r in sub]) for h in HORIZONS)
                   + f" | {m([r['mk_settle'] for r in sub])} |")
    out.append("")
    for part in ("dev", "test"):
        sub = [r for r in recs if r["period"] == part]
        out.append(f"## {part.upper()} ({len(sub)} orders, {sum(r['filled'] for r in sub)} filled)\n")
        table(sub, "pre_1s", [("ask fell ≥ 1c (buying against the move)", -99, -1), ("flat (−1c..+1c)", -1, 1.0001),
                              ("already up 1–3c", 1.0001, 3.0001), ("already up 3–6c", 3.0001, 6.0001),
                              ("already up > 6c", 6.0001, 99)], "How far the ask had already moved our way in the last 1 s", out)
        table(sub, "pre_3s", [("≤ 0c", -99, 0.0001), ("0–3c", 0.0001, 3.0001), ("3–6c", 3.0001, 6.0001),
                              ("6–10c", 6.0001, 10.0001), ("> 10c", 10.0001, 99)], "…over the last 3 s", out)
        table(sub, "ask", [("< 0.30", 0, 0.30), ("0.30–0.45", 0.30, 0.45), ("0.45–0.55", 0.45, 0.55),
                           ("0.55–0.65", 0.55, 0.65), ("≥ 0.65", 0.65, 1)], "Entry price (ask)", out)
        table(sub, "left", [("60–120 s", 60, 120), ("120–180 s", 120, 180), ("180–240 s", 180, 240), ("240–300 s", 240, 300)],
              "Seconds left in the window", out)
        table(sub, "z", [("3.0–3.5σ", 0, 3.5), ("3.5–4σ", 3.5, 4), ("4–5σ", 4, 5), ("≥ 5σ", 5, 99)], "Shock size |z|", out)
    out += exits_and_limits(rows, journal)
    print("\n".join(out))



# ═══════════════════════ exits and the limit allowance, on the real orders ═══════════════════════

EXIT_LAG_MS = 300.0        # exit decision -> sold: the live exit's order round trip (~270 ms) on the 200 ms grid


def sim_exit(p: "Path_", t_fill: float, entry: float, tp: float | None, sl: float | None, hold_s: float | None,
             outcome_value: float | None) -> float | None:
    """Cents per share after both fees: first sample where the bid is tp above / sl below the entry, or the hold
    time is up; sold at the bid EXIT_LAG_MS later (so a gapping book is paid for). hold_s None = to settlement."""
    i = bisect.bisect_right(p.t, t_fill)
    end = math.inf if hold_s is None else t_fill + hold_s * 1000.0
    for k in range(i, len(p.t)):
        b = p.bid[k]
        t = p.t[k]
        hit = (tp is not None and b >= entry + tp / 100.0 - 1e-9) or (sl is not None and b <= entry - sl / 100.0 + 1e-9)
        if hit or t >= end:
            x = p.at(p.bid, t + EXIT_LAG_MS)
            x = b if x is None else x
            return (x - entry - fee(entry) - fee(x)) * 100.0
    if outcome_value is None:
        return None
    return (outcome_value - entry - fee(entry)) * 100.0


def exits_and_limits(rows, journal) -> list[str]:
    fr = {r["order_id"]: r for r in rows}
    cache: dict[str, dict | None] = {}

    def doc(slug):
        if slug not in cache:
            try:
                cache[slug] = json.loads((ROOT / f"data/charts/{slug}.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cache[slug] = None
        return cache[slug]

    fills = []
    for oid, j in journal.items():
        r = fr.get(oid)
        if r is None or r.get("execution_price") is None:
            continue
        d = doc(r["slug"])
        if d is None:
            continue
        p = Path_(d, r["side"])
        if len(p.t) < 20:
            continue
        t_fill = r.get("t7_venue_fill") or r.get("t6_venue_ack") or r.get("t5_sent")
        oc = str(d.get("outcome") or "").upper()
        fills.append({"p": p, "t": t_fill, "e": r["execution_price"], "real": j["realized_pnl"] / j["quantity"] * 100.0,
                      "ov": None if oc not in ("UP", "DOWN") else (1.0 if oc == r["side"] else 0.0),
                      "period": "test" if t_fill >= TEST_FROM else "dev"})
    out = ["## Exits, replayed on the real fills\n",
           f"{len(fills)} real fills with an archived path. Each exit rule is replayed from the real fill price: sell at "
           f"the bid {EXIT_LAG_MS:.0f} ms after the first archive point that hits the take-profit / stop-loss / hold time "
           "(a stop that gaps is paid in full), taker fees on both legs; `settle` holds to the 1 / 0 outcome.\n"]
    cur = [sim_exit(f["p"], f["t"], f["e"], 10, 5, 20, f["ov"]) for f in fills]
    ok = [(c, f["real"]) for c, f in zip(cur, fills) if c is not None]
    out.append(f"Calibration: the live rule (TP 10 / SL 5 / 20 s) replayed gives {st.mean(c for c, _ in ok):+.2f}¢/share on "
               f"these fills; their real result was {st.mean(r for _, r in ok):+.2f}¢/share "
               f"(median |replay − real| {st.median(abs(c - r) for c, r in ok):.1f}¢).\n")
    grid = []
    for tp in (None, 5, 10, 15, 20, 30):
        for sl in (None, 5, 8, 10, 15, 20, 30):
            for hold in (10, 20, 30, 60, 120, None):
                res = {}
                for part in ("dev", "test"):
                    v = [sim_exit(f["p"], f["t"], f["e"], tp, sl, hold, f["ov"]) for f in fills if f["period"] == part]
                    v = [x for x in v if x is not None]
                    res[part] = (st.mean(v) if v else float("nan"), len(v),
                                 st.stdev(v) / math.sqrt(len(v)) * 1.96 if len(v) > 1 else float("nan"))
                grid.append(((tp, sl, hold), res))
    grid.sort(key=lambda g: -g[1]["dev"][0])
    lab = lambda v, u: "off" if v is None else f"{v}{u}"  # noqa: E731
    out.append("Top 15 by DEV (chosen on DEV only), with TEST alongside; the live rule last.\n")
    out.append("| take-profit | stop-loss | max hold | DEV ¢/share | n | TEST ¢/share | n |")
    out.append("|---|---|---|---:|---:|---:|---:|")
    live = next(g for g in grid if g[0] == (10, 5, 20))
    for (tp, sl, hold), res in grid[:15] + [live]:
        d, t = res["dev"], res["test"]
        out.append(f"| {lab(tp, 'c')} | {lab(sl, 'c')} | {'settle' if hold is None else f'{hold} s'} | "
                   f"{d[0]:+.1f} ±{d[2]:.1f} | {d[1]} | {t[0]:+.1f} ±{t[2]:.1f} | {t[1]} |")
    out.append("")
    # the limit allowance: what the killed orders would have cost at the ask when they reached the venue
    out.append("## The limit allowance: could a higher limit have caught the good orders?\n")
    out.append("For every killed order, the venue's ask when the order arrived (archive at sent + half the measured "
               "round trip) above the decision ask, and the result of a fill THERE: the bid 5 / 20 s later minus that "
               "ask minus fees. A higher limit only helps if these are positive.\n")
    out.append("| arrival ask above decision ask | killed orders | at arrival: 5 s | 20 s | settle |")
    out.append("|---|---:|---:|---:|---:|")
    kill = []
    for r in rows:
        if r.get("execution_price") is not None or not r.get("t5_sent") or not r.get("best_ask"):
            continue
        d = doc(r["slug"])
        if d is None:
            continue
        p = Path_(d, r["side"])
        if len(p.t) < 20:
            continue
        ta = r["t5_sent"] + (r.get("venue_rtt_ms") or 270.0) / 2.0
        a = p.at(p.ask, ta)
        if a is None:
            continue
        oc = str(d.get("outcome") or "").upper()
        ov = None if oc not in ("UP", "DOWN") else (1.0 if oc == r["side"] else 0.0)
        mk = {}
        for h in (5, 20):
            b = p.at(p.bid, ta + h * 1000.0)
            mk[h] = None if b is None else (b - a - fee(a) - fee(b)) * 100.0
        mk["s"] = None if ov is None else (ov - a - fee(a)) * 100.0
        kill.append(((a - r["best_ask"]) * 100.0, mk))
    for label, lo, hi in (("≤ 4c (inside the limit: killed for depth or latency)", -99, 4.0001), ("4–6c", 4.0001, 6.0001),
                          ("6–10c", 6.0001, 10.0001), ("10–15c", 10.0001, 15.0001), ("> 15c", 15.0001, 99)):
        sub = [mk for gap, mk in kill if lo <= gap < hi]
        if sub:
            out.append(f"| {label} | {len(sub)} | {m([x[5] for x in sub])} | {m([x[20] for x in sub])} | {m([x['s'] for x in sub])} |")
    out.append("")
    return out


if __name__ == "__main__":
    main()
