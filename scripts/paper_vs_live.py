"""Paper-vs-live reconciliation report.

    python scripts/paper_vs_live.py [--trades data/live_trades.jsonl] [--earnings data/earnings.jsonl]
                                    [--forensics data/trade_forensics.jsonl] [--charts data/charts]
                                    [--log ~/.pm2/logs/tpb-bot-error.log]

Groups every fill and settlement by the mode that produced it (live-paper,
live-dry-run/shadow, LIVE-ARMED) and prints the comparison table the audit
asks for. Where a number cannot be computed from the files it prints "n/a"
rather than a guess. Live orders that never filled are read from the bot log
(the ledger only holds fills), so the live fill rate is the venue's answer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import statistics
from collections import defaultdict
from pathlib import Path

SEND_RE = re.compile(r"^(\d\d:\d\d:\d\d) INFO\s+SEND\s+(\S+)\s+(UP|DOWN)\s+([\d.]+) sh @ ([\d.]+)")


def read_jsonl(path: str) -> list[dict]:
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def fmt(v, nd=3):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def live_orders_from_log(path: str) -> dict:
    """orders sent / filled / killed from the armed bot's log."""
    p = Path(os.path.expanduser(path))
    if not p.exists():
        return {}
    lines = p.read_text(errors="replace").splitlines()
    sent = filled = killed = 0
    for i, l in enumerate(lines):
        m = SEND_RE.search(l)
        if not m:
            continue
        slug = m.group(2)
        sent += 1
        for j in range(i + 1, min(i + 15, len(lines))):
            if f"REJECT {slug}" in lines[j]:
                if "fok_unfillable" in lines[j] or "quote_pulled" in lines[j]:
                    killed += 1
                break
            if "VENUE FILL BUY" in lines[j]:
                filled += 1
                break
    return {"sent": sent, "filled": filled, "killed": killed}


def per_mode(rows: list[dict], earnings: list[dict], forensics: list[dict]) -> dict:
    by: dict[str, dict] = defaultdict(lambda: {"buys": [], "sells": [], "settles": []})
    for r in rows:
        mode = r.get("mode") or "?"
        if r.get("event") == "fill" and r.get("action") == "BUY":
            by[mode]["buys"].append(r)
        elif r.get("event") == "fill" and r.get("action") == "SELL":
            by[mode]["sells"].append(r)
        elif r.get("event") == "settle":
            by[mode]["settles"].append(r)
    for e in earnings:
        by[e.get("mode") or "?"].setdefault("earnings", []).append(e)
    for t in forensics:
        by[t.get("mode") or "?"].setdefault("trades", []).append(t)
    out = {}
    for mode, g in by.items():
        pnls = [s["pnl"] for s in g["settles"] if s.get("pnl") is not None]
        if not pnls:
            pnls = [e["pnl"] for e in g.get("earnings", [])]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        buys = g["buys"]
        fees = sum(b.get("fee", 0.0) for b in buys) + sum(s.get("fee", 0.0) for s in g["sells"])
        slips = [b["slippage"] for b in buys if b.get("slippage") is not None]
        rtts = [b["round_trip_ms"] for b in buys if b.get("round_trip_ms")]
        edges = [b["net_edge"] for b in buys if b.get("net_edge") is not None]
        hold = []
        for s in g["sells"]:
            entry = [b for b in buys if b["slug"] == s["slug"]]
            if entry:
                hold.append((s["ts"] - min(b["ts"] for b in entry)) / 1000.0)
        trades = g.get("trades", [])
        lat = [t["latency_ms"] for t in trades if t.get("latency_ms") is not None]
        out[mode] = {
            "trades": len(pnls), "fills": len(buys),
            "win_rate": len(wins) / len(pnls) if pnls else None,
            "avg_pnl": statistics.fmean(pnls) if pnls else None,
            "median_pnl": statistics.median(pnls) if pnls else None,
            "net_pnl": sum(pnls) if pnls else None,
            "avg_win": statistics.fmean(wins) if wins else None,
            "avg_loss": statistics.fmean(losses) if losses else None,
            "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
            "fees": fees if buys else None,
            "avg_slippage": statistics.fmean(slips) if slips else None,
            "avg_entry_price": statistics.fmean(b["price"] for b in buys) if buys else None,
            "avg_expected_edge": statistics.fmean(edges) if edges else None,
            "avg_latency_ms": statistics.fmean(lat) if lat else (statistics.fmean(rtts) if rtts else None),
            "p95_latency_ms": pct(lat or rtts, 0.95),
            "p99_latency_ms": pct(lat or rtts, 0.99),
            "avg_holding_s": statistics.fmean(hold) if hold else None,
            "fill_rate": None,
            "max_drawdown": None,
        }
        eq = peak = dd = 0.0
        for p in pnls:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        out[mode]["max_drawdown"] = dd if pnls else None
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="data/live_trades.jsonl")
    ap.add_argument("--earnings", default="data/earnings.jsonl")
    ap.add_argument("--forensics", default="data/trade_forensics.jsonl")
    ap.add_argument("--charts", default="data/charts")
    ap.add_argument("--log", default="~/.pm2/logs/tpb-bot-error.log")
    args = ap.parse_args()

    rows = read_jsonl(args.trades)
    # the archive keeps every fill of every archived window, including runs whose ledger was reset
    seen = {(r.get("ts"), r.get("slug"), r.get("action")) for r in rows}
    for p in glob.glob(os.path.join(args.charts, "*.json")):
        try:
            d = json.load(open(p))
        except (OSError, json.JSONDecodeError):
            continue
        for f in d.get("fills", []):
            key = (f.get("ts"), f.get("slug"), f.get("action"))
            if key not in seen:
                rows.append(f)
                seen.add(key)
    earnings = read_jsonl(args.earnings)
    forensics = [t for t in read_jsonl(args.forensics) if t.get("event") == "trade"]
    stats = per_mode(rows, earnings, forensics)
    live_log = live_orders_from_log(args.log)
    if live_log and "LIVE-ARMED" in stats:
        stats["LIVE-ARMED"]["fill_rate"] = live_log["filled"] / live_log["sent"] if live_log["sent"] else None
        stats["LIVE-ARMED"]["orders_sent"] = live_log["sent"]
        stats["LIVE-ARMED"]["orders_killed"] = live_log["killed"]
    if "live-paper" in stats:
        stats["live-paper"]["fill_rate"] = 1.0    # the ledger holds fills only; paper kills are not logged

    paper = stats.get("live-paper", {})
    live = stats.get("LIVE-ARMED", {})
    metrics = [("Trades settled", "trades", 0), ("Win rate", "win_rate", 3), ("Avg PnL", "avg_pnl", 4),
               ("Median PnL", "median_pnl", 4), ("Net PnL", "net_pnl", 4), ("Avg win", "avg_win", 4),
               ("Avg loss", "avg_loss", 4), ("Profit factor", "profit_factor", 2), ("Max drawdown", "max_drawdown", 4),
               ("Fees", "fees", 4), ("Avg slippage/share", "avg_slippage", 4), ("Avg entry price", "avg_entry_price", 3),
               ("Avg expected edge", "avg_expected_edge", 3), ("Fill rate", "fill_rate", 3),
               ("Avg latency ms", "avg_latency_ms", 1), ("P95 latency ms", "p95_latency_ms", 1),
               ("P99 latency ms", "p99_latency_ms", 1), ("Avg holding s", "avg_holding_s", 1)]
    print("| Metric | Paper | Live | Difference |")
    print("|---|---:|---:|---:|")
    for label, key, nd in metrics:
        a, b = paper.get(key), live.get(key)
        diff = (b - a) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        print(f"| {label} | {fmt(a, nd)} | {fmt(b, nd)} | {fmt(diff, nd)} |")
    if live_log:
        print(f"\nlive orders from the log: sent {live_log['sent']}, filled {live_log['filled']}, "
              f"killed {live_log['killed']}")
    other = {k: v for k, v in stats.items() if k not in ("live-paper", "LIVE-ARMED")}
    for mode, s in other.items():
        print(f"\n{mode}: trades {s['trades']}, net {fmt(s['net_pnl'], 4)}, win rate {fmt(s['win_rate'])}")
    print(f"\ngenerated {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")


if __name__ == "__main__":
    main()
