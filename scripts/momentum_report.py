"""Evidence report from what the bot RECORDED: latency, execution, adverse
selection, missed opportunities, and the same signals across modes.

    python scripts/momentum_report.py [--tag momentum] [--tag momentum-shadow] [--tag ""]
                                      [--data data] [--since-hours 24]

Each ``--tag`` names a bot's files (``data/<file>.<tag>.jsonl``; ``""`` is
the untagged set): trade_forensics, markouts, signal_outcomes, decisions.
Rows are grouped by the ``mode`` that wrote them (live-paper, shadow-paper,
LIVE-ARMED ...), never summed across modes. Every number comes from the
files; "n/a" means the files cannot answer it yet.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import defaultdict
from pathlib import Path

SEGMENTS = ("spot_feed_latency_ms", "book_latency_ms", "signal_processing_ms", "presend_ms", "sign_ms",
            "venue_ack_ms", "venue_fill_latency_ms", "decision_to_fill_ms", "data_to_fill_ms",
            "exit_decision_to_fill_ms")


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def fname(data: Path, base: str, tag: str) -> Path:
    return data / (f"{base}.{tag}.jsonl" if tag else f"{base}.jsonl")


def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def f(v, fmt="{:+.4f}"):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return fmt.format(v) if isinstance(v, (int, float)) else str(v)


def mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.fmean(vals) if vals else None


def load_trades(path: Path, since_ms: float) -> list[dict]:
    """Consolidated forensics trade records, amendments folded in."""
    by_id: dict[str, dict] = {}
    for r in rows(path):
        if r.get("event") == "trade":
            if (r.get("closed_ts") or 0) >= since_ms:
                by_id[r.get("order_id", "")] = r
        elif r.get("event") == "amend" and r.get("order_id") in by_id:
            by_id[r["order_id"]].update({k: v for k, v in r.items() if k not in ("event", "order_id", "ts")})
    return list(by_id.values())


def time_bucket(left):
    if left is None:
        return "?"
    return "early 200-295s" if left >= 200 else "middle 120-200s" if left >= 120 else "late 60-120s" if left >= 60 else "last <60s"


def bucket(v, edges, fmt="{:g}"):
    if v is None:
        return "?"
    for lo in sorted(edges, reverse=True):
        if v >= lo:
            return f">={fmt.format(lo)}"
    return f"<{fmt.format(min(edges))}"


def trade_stats(entries: list[dict]) -> dict:
    orders = len(entries)
    fills = [e for e in entries if e.get("execution_price") is not None
             or (e.get("entry_price") is not None and not str(e.get("outcome") or "").startswith("REJECTED"))]
    closed = [e for e in fills if e.get("pnl_after_fees") is not None]
    pnl = [e["pnl_after_fees"] for e in closed]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    eq = peak = dd = 0.0
    for e in sorted(closed, key=lambda r: r.get("closed_ts") or 0):
        eq += e["pnl_after_fees"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"orders": orders, "fills": len(fills), "fill_rate": len(fills) / orders if orders else None,
            "closed": len(closed), "win_rate": len(wins) / len(closed) if closed else None,
            "avg_entry": mean([e.get("entry_price") or e.get("execution_price") for e in fills]),
            "avg_exit": mean([e.get("exit_price") for e in closed]),
            "slippage": mean([e.get("realized_slippage") for e in fills]),
            "fees": sum(e.get("fees") or 0.0 for e in closed),
            "avg_win": mean(wins), "avg_loss": mean(losses), "pnl": sum(pnl) if pnl else None,
            "expectancy": mean(pnl), "max_dd": dd if closed else None,
            "expected_edge": mean([e.get("edge") for e in fills]),
            "realized_edge_per_share": mean([(e["pnl_after_fees"] / e["quantity"]) for e in closed if e.get("quantity")])}


HEAD = ("| | orders | fills | fill rate | closed | win | avg entry | avg exit | slippage | fees | avg win | avg loss "
        "| PnL | expectancy | max DD | expected edge/sh | realized/sh |\n" + "|---" * 17 + "|")


def trow(name, s):
    return (f"| {name} | {s['orders']} | {s['fills']} | {f(s['fill_rate'], '{:.0%}')} | {s['closed']} | "
            f"{f(s['win_rate'], '{:.0%}')} | {f(s['avg_entry'], '{:.3f}')} | {f(s['avg_exit'], '{:.3f}')} | "
            f"{f(s['slippage'])} | {f(s['fees'], '{:.3f}')} | {f(s['avg_win'])} | {f(s['avg_loss'])} | "
            f"{f(s['pnl'], '{:+.3f}')} | {f(s['expectancy'])} | {f(s['max_dd'], '{:.3f}')} | "
            f"{f(s['expected_edge'])} | {f(s['realized_edge_per_share'])} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", action="append", default=None)
    ap.add_argument("--data", default="data")
    ap.add_argument("--since-hours", type=float, default=0.0)
    args = ap.parse_args()
    data = Path(args.data)
    tags = args.tag if args.tag is not None else ["momentum", ""]
    since_ms = (time.time() - args.since_hours * 3600.0) * 1000.0 if args.since_hours > 0 else 0.0

    trades, marks, sigs, settles = [], [], [], {}
    for tag in tags:
        for t in load_trades(fname(data, "trade_forensics", tag), since_ms):
            t["_tag"] = tag
            trades.append(t)
        for m in rows(fname(data, "markouts", tag)):
            if m.get("event") == "markout" and (m.get("fill_ts") or 0) >= since_ms:
                m["_tag"] = tag
                marks.append(m)
        for s in rows(fname(data, "signal_outcomes", tag)):
            if s.get("event") == "settle":
                settles[s["slug"]] = s["up_won"]
            elif s.get("event") == "signal" and (s.get("ts") or 0) >= since_ms:
                s["_tag"] = tag
                sigs.append(s)
    entries = [t for t in trades if t.get("kind") == "entry"]
    exits = [t for t in trades if t.get("kind") not in (None, "entry")]
    print(f"# Momentum evidence report\n\ntags {tags}; {len(entries)} entry orders, {len(exits)} exit orders, "
          f"{len(marks)} markouts, {len(sigs)} followed signals"
          + (f", last {args.since_hours:g} h" if since_ms else ""))

    # ---- 1. latency -------------------------------------------------------
    print("\n## Latency (ms): median / p75 / p95 / p99 (n)\n")
    print("| mode | " + " | ".join(SEGMENTS) + " |\n|---" + "|---" * len(SEGMENTS) + "|")
    by_mode = defaultdict(list)
    for t in trades:
        by_mode[t.get("mode") or "?"].append(t)
    for mode, ts in sorted(by_mode.items()):
        cells = []
        for seg in SEGMENTS:
            v = [t[seg] for t in ts if isinstance(t.get(seg), (int, float))]
            cells.append("n/a" if not v else f"{pct(v, .5):.0f} / {pct(v, .75):.0f} / {pct(v, .95):.0f} / "
                                             f"{pct(v, .99):.0f} ({len(v)})")
        print(f"| {mode} | " + " | ".join(cells) + " |")

    # ---- 2. execution by mode --------------------------------------------
    print("\n## Execution by mode (entries; PnL after both fees)\n\n" + HEAD)
    for mode, ts in sorted(by_mode.items()):
        es = [t for t in ts if t.get("kind") == "entry"]
        if es:
            print(trow(mode, trade_stats(es)))
    ex_kinds = defaultdict(int)
    for t in exits:
        ex_kinds[(t.get("mode"), t.get("kind"), str(t.get("outcome") or "FILLED")[:24])] += 1
    if ex_kinds:
        print("\nexit orders by mode / kind / outcome: " + ", ".join(f"{k}: {v}" for k, v in sorted(ex_kinds.items())))

    # ---- 3. the same signals across paths --------------------------------
    print("\n## Same signals, different execution\n")
    twin = [t for t in entries if t.get("paper_outcome")]
    if twin:
        m = defaultdict(int)
        for t in twin:
            live = "filled" if t.get("execution_price") is not None else "not filled"
            paper = "filled" if t.get("paper_outcome") == "FILLED" else "not filled"
            m[(live, paper)] += 1
        print("LIVE order vs its paper twin (same order, same book):\n\n| live \\ paper | filled | not filled |\n|---|---:|---:|")
        for live in ("filled", "not filled"):
            print(f"| {live} | {m[(live, 'filled')]} | {m[(live, 'not filled')]} |")
    shadow = [t for t in entries if t.get("live_path")]
    if shadow:
        m = defaultdict(int)
        for t in shadow:
            m[(t.get("live_path"), "filled" if t.get("execution_price") is not None else "not filled")] += 1
        print("\nSHADOW: what the real path would have done with each order vs the realistic paper fill:\n")
        print("| real path | paper filled | paper not filled |\n|---|---:|---:|")
        for path in sorted({k[0] for k in m}):
            print(f"| {path} | {m[(path, 'filled')]} | {m[(path, 'not filled')]} |")
    if not twin and not shadow:
        print("no paired records yet (live runs carry paper_outcome; shadow runs carry live_path)")

    # ---- 4. adverse selection -------------------------------------------
    print("\n## Adverse selection: the mid's move in OUR direction after a fill (negative = against us)\n")
    offs = ("100", "250", "500", "1000", "2000", "5000", "10000", "30000")
    print("| mode / kind | n | " + " | ".join(f"{o} ms" for o in offs) + " | share against @1s |\n|---|---:" + "|---:" * (len(offs) + 1) + "|")
    g = defaultdict(list)
    for mk in marks:
        g[(mk.get("mode") or "?", mk.get("kind") or "entry", bool(mk.get("filled", True)))].append(mk)
    for (mode, kind, filled), ms in sorted(g.items()):
        cells = [f(mean([x["mid_move"].get(o) for x in ms])) for o in offs]
        m1 = [x["mid_move"].get("1000") for x in ms if x.get("mid_move", {}).get("1000") is not None]
        share = sum(1 for v in m1 if v < 0) / len(m1) if m1 else None
        label = f"{mode} / {kind}" + ("" if filled else " (not filled)")
        print(f"| {label} | {len(ms)} | " + " | ".join(cells) + f" | {f(share, '{:.0%}')} |")

    # ---- 5. missed opportunities ------------------------------------------
    print("\n## Signals and their fate (hypothetical taker round trip at the primary horizon, both fees)\n")
    classes = ("TRADED", "UNFILLABLE_SKIP", "GOOD_MISS", "BAD_MISS", "KILLED_UNFILLABLE", "KILLED_GOOD_MISS",
               "KILLED_BAD_MISS", "UNKNOWN")
    print("| decision / reason | n | " + " | ".join(classes) + " | mean hypothetical |\n|---|---:" + "|---:" * (len(classes) + 1) + "|")
    by_reason = defaultdict(list)
    for s in sigs:
        by_reason[(s.get("decision"), s.get("reason") or "")].append(s)
    for (dec, reason), ss in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        c = defaultdict(int)
        for s in ss:
            c[s.get("classification", "UNKNOWN")] += 1
        hyp = mean([next(iter(s.get("hypothetical_net", {}).values()), None) if s.get("hypothetical_net") else None
                    for s in ss])
        print(f"| {dec} {reason} | {len(ss)} | " + " | ".join(str(c.get(k, 0)) for k in classes) + f" | {f(hyp)} |")

    # ---- 6. conditions: what produces profitable fills? ---------------------
    print("\n## What conditions? Entries by condition (descriptive; small samples are not evidence)\n")
    groups = (
        ("asset", lambda t: t.get("asset") or "?"),
        ("time to expiry", lambda t: time_bucket(t.get("seconds_left"))),
        ("momentum label", lambda t: t.get("momentum_label") or "?"),
        ("momentum z", lambda t: bucket(abs(t["momentum_z"]) if t.get("momentum_z") is not None else None, (2, 3, 4, 5))),
        ("spread", lambda t: bucket(t.get("spread"), (0.01, 0.02, 0.03), "{:.2f}")),
        ("quote age ms", lambda t: bucket(t.get("quote_age_ms"), (50, 100, 250, 500))),
        ("decision->fill ms", lambda t: bucket(t.get("decision_to_fill_ms"), (100, 250, 500, 1000))),
        ("entry price", lambda t: bucket(t.get("entry_price") or t.get("decision_price"), (0.2, 0.4, 0.6, 0.8), "{:.1f}")),
        ("expected edge", lambda t: bucket(t.get("edge"), (0.0, 0.01, 0.02, 0.04), "{:.2f}")),
    )
    for title, key in groups:
        gg = defaultdict(list)
        for t in entries:
            gg[(t.get("mode") or "?", key(t))].append(t)
        if not gg:
            continue
        print(f"\n### by {title}\n\n{HEAD}")
        for (mode, k), es in sorted(gg.items()):
            print(trow(f"{mode} {k}", trade_stats(es)))
    # the same conditions on EVERY followed signal (traded or not): the larger sample
    print("\n## Followed signals by condition: share whose price was there at arrival, and the mean hypothetical\n")
    for title, key in (("asset", lambda s: s.get("asset") or "?"),
                       ("time to expiry", lambda s: time_bucket(s.get("secs_left"))),
                       ("momentum z", lambda s: bucket(abs(s["momentum_z"]) if s.get("momentum_z") is not None else None, (2, 3, 4, 5))),
                       ("quote age ms", lambda s: bucket(s.get("quote_age_ms"), (50, 100, 250, 500)))):
        gg = defaultdict(list)
        for s in sigs:
            gg[key(s)].append(s)
        print(f"\n| {title} | signals | available at arrival | mean hypothetical | would win |\n|---|---:|---:|---:|---:|")
        for k, ss in sorted(gg.items()):
            av = [s for s in ss if s.get("price_still_available")]
            hyp = [next(iter(s["hypothetical_net"].values())) for s in av if s.get("hypothetical_net")]
            print(f"| {k} | {len(ss)} | {f(len(av) / len(ss), '{:.0%}')} | {f(mean(hyp))} | "
                  f"{f(sum(1 for h in hyp if h > 0) / len(hyp) if hyp else None, '{:.0%}')} |")


if __name__ == "__main__":
    main()
