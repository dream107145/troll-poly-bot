"""End-to-end latency per asset from the live bot's forensics: exchange event -> spot
received -> decision -> POST -> venue reply -> fill, with the fill rate.

    python scripts/latency_by_asset.py [--since 2026-09-29T17:58] [--path data/trade_forensics.jsonl]

READ-ONLY. Stages come from execution/forensics.py (t0..t7). Medians unless marked."""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * (len(v) - 1)))] if v else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--path", default="data/trade_forensics.jsonl")
    ap.add_argument("--since", default=None, help="UTC, e.g. 2026-09-29T17:58")
    ap.add_argument("--mode", default="LIVE-ARMED")
    args = ap.parse_args()
    since = 0.0
    if args.since:
        since = (time.mktime(time.strptime(args.since, "%Y-%m-%dT%H:%M")) - time.timezone) * 1000.0
    rows = []
    for line in open(args.path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("event") != "trade" or r.get("kind") != "entry" or r.get("mode") != args.mode:
            continue
        if (r.get("closed_ts") or 0) < since:
            continue
        rows.append(r)
    by = defaultdict(list)
    for r in rows:
        by[r["asset"]].append(r)
        by["ALL"].append(r)
    stages = (("exch->recv", lambda r: r["t1_spot_recv"] - r["t0_spot_event"] if r.get("t0_spot_event") and r.get("t1_spot_recv") else None),
              ("recv->decide", lambda r: r["t4_decision"] - r["t1_spot_recv"] if r.get("t4_decision") and r.get("t1_spot_recv") else None),
              ("decide->sent", lambda r: r.get("presend_ms")),
              ("sign", lambda r: r.get("sign_ms")),
              ("venue rtt", lambda r: r.get("venue_rtt_ms")),
              ("exch->sent", lambda r: r["t5_sent"] - r["t0_spot_event"] if r.get("t0_spot_event") and r.get("t5_sent") else None),
              ("book age", lambda r: r.get("quote_age_ms")))
    print(f"entries: {len(rows)} (mode {args.mode}{', since ' + args.since if args.since else ''})\n")
    print("| asset | n | filled | " + " | ".join(n for n, _ in stages) + " |")
    print("|---|---:|---:|" + "---:|" * len(stages))
    for a in sorted(by, key=lambda k: (k == "ALL", k)):
        rr = by[a]
        filled = sum(1 for r in rr if (r.get("execution_price") is not None) or str(r.get("outcome", "")).startswith(("FILLED", "EXIT")))
        cells = []
        for _, fn in stages:
            v = [fn(r) for r in rr]
            v = [x for x in v if x is not None]
            cells.append(f"{pct(v, .5):.0f} ms (p90 {pct(v, .9):.0f})" if v else "-")
        print(f"| {a} | {len(rr)} | {filled} ({filled / len(rr):.0%}) | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
