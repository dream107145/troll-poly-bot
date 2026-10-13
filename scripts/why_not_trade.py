"""Why didn't we trade? The session summary from the decision journal.

    python scripts/why_not_trade.py [--decisions data/decisions.jsonl] [--since <epoch seconds>]
                                    [--forensics data/trade_forensics.jsonl] [--last-signals 20]

Counts every signal (an evaluation that priced a side) by its rejection
label, every order event, and prints the block the experiment asks for:

    Signals detected:             127
    Rejected:
      Low edge:                    61
      ...
    Orders submitted:              13
    Orders accepted:               12
    Orders filled:                  5
    Orders expired:                 7
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def rows(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions", default="data/decisions.jsonl")
    ap.add_argument("--forensics", default="data/trade_forensics.jsonl")
    ap.add_argument("--since", type=float, default=0.0, help="epoch seconds; 0 = whole file")
    ap.add_argument("--last-signals", type=int, default=15)
    args = ap.parse_args()

    since_ms = args.since * 1000.0
    dec = [r for r in rows(args.decisions) if r.get("timestamp", 0) >= since_ms]
    signals = [r for r in dec if str(r.get("decision", "")).startswith("SIGNAL")]
    orders = [r for r in dec if str(r.get("decision", "")).startswith("ORDER")]
    by_label = Counter(r["decision"] for r in signals)
    by_reason = Counter(r.get("rejection_reason") or "TRADE" for r in signals)
    by_order = Counter(r["decision"] for r in orders)
    # note: signals are journal RECORDS (transitions + 5 s heartbeats), not every 200 ms evaluation

    print(f"Decision records:             {len(dec)}")
    print(f"Signals recorded:             {len(signals)}   (transitions and 5 s heartbeats inside the trade window)")
    print(f"  tradeable (SIGNAL_GENERATED): {by_label.get('SIGNAL_GENERATED', 0)}")
    print("Rejected:")
    labels = [("Low edge", "SIGNAL_REJECTED_LOW_EDGE"), ("Low confidence", "SIGNAL_REJECTED_LOW_CONFIDENCE"),
              ("Low liquidity / spread", "SIGNAL_REJECTED_LIQUIDITY"), ("Stale quote", "SIGNAL_REJECTED_STALE_QUOTE"),
              ("Risk limit", "SIGNAL_REJECTED_RISK"), ("Position limit", "SIGNAL_REJECTED_POSITION_LIMIT"),
              ("Size (min order vs caps)", "SIGNAL_REJECTED_SIZE"), ("Stale data", "SIGNAL_REJECTED_STALE_DATA"),
              ("Market state", "SIGNAL_REJECTED_MARKET_STATE"), ("Model", "SIGNAL_REJECTED_MODEL"),
              ("Guard", "SIGNAL_REJECTED_GUARD"), ("Revalidation", "SIGNAL_REJECTED_REVALIDATION"),
              ("Throttle", "SIGNAL_REJECTED_THROTTLE"),
              ("Weak momentum", "SIGNAL_REJECTED_WEAK_MOMENTUM"),
              ("Momentum reversing", "SIGNAL_REJECTED_MOMENTUM_REVERSING"),
              ("Momentum exhausted", "SIGNAL_REJECTED_MOMENTUM_EXHAUSTED"),
              ("Volatility spike", "SIGNAL_REJECTED_VOLATILITY_SPIKE"),
              ("Token already moved", "SIGNAL_REJECTED_TOKEN_ALREADY_MOVED"),
              ("Ask above max entry (chase)", "SIGNAL_REJECTED_PRICE_CHASE"),
              ("Cooldown", "SIGNAL_REJECTED_COOLDOWN"), ("Max trades", "SIGNAL_REJECTED_MAX_TRADES"),
              ("Entry confirmation", "SIGNAL_REJECTED_CONFIRMATION"),
              ("Kill switch", "SIGNAL_REJECTED_KILL_SWITCH"), ("Other", "SIGNAL_REJECTED_OTHER")]
    for name, key in labels:
        if by_label.get(key):
            print(f"  {name + ':':28} {by_label[key]:6d}")
    known = {k for _, k in labels} | {"SIGNAL_GENERATED"}
    for key, n in by_label.most_common():
        if key not in known:                     # a label added later is never silently dropped
            print(f"  {key + ':':28} {n:6d}")
    print("Raw reasons:")
    for reason, n in by_reason.most_common():
        print(f"  {reason + ':':28} {n:6d}")
    print()
    for name, key in [("Orders created", "ORDER_CREATED"), ("Orders submitted", "ORDER_SUBMITTED"),
                      ("Orders accepted", "ORDER_ACCEPTED"), ("Orders partially filled", "ORDER_PARTIALLY_FILLED"),
                      ("Orders filled", "ORDER_FILLED"), ("Orders expired", "ORDER_EXPIRED"),
                      ("Orders cancelled", "ORDER_CANCELLED"), ("Orders rejected", "ORDER_REJECTED")]:
        print(f"{name + ':':30} {by_order.get(key, 0):6d}")

    trades = [t for t in rows(args.forensics) if t.get("event") == "trade" and t.get("closed_ts", 0) >= since_ms]
    fills = [t for t in trades if t.get("kind") == "entry" and t.get("execution_price") is not None]
    print(f"\nForensics: {len(trades)} closed order records, {len(fills)} real entry fills")
    for t in fills[-10:]:
        print(f"  {t.get('slug'):26} {t.get('side'):4} entry {t.get('entry_price')} exit {t.get('exit_price')} "
              f"pnl {t.get('pnl_after_fees')} paper {t.get('paper_outcome')} @ {t.get('paper_fill_price')} "
              f"paper_pnl {t.get('paper_pnl')} lat {t.get('latency_ms')} ms")

    print(f"\nLast {args.last_signals} signal records:")
    for r in signals[-args.last_signals:]:
        print(f"  {r.get('market'):26} {str(r.get('side')):4} {r.get('decision'):32} ask {r.get('market_price')} "
              f"p {r.get('model_probability')} mkt {r.get('market_probability')} net {r.get('estimated_net_edge')} "
              f"spread {r.get('spread')} depth {r.get('liquidity')} left {r.get('time_remaining')} {r.get('detail') or ''}")


if __name__ == "__main__":
    main()
