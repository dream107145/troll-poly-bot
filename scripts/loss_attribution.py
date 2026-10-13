"""Classify every losing live trade and every unfilled live order.

    python scripts/loss_attribution.py [--trades data/live_trades.jsonl] [--charts data/charts]
                                       [--log ~/.pm2/logs/tpb-bot-error.log] [--mode LIVE-ARMED]

Categories (one primary per trade, from the recorded books, never guessed):

    STALE_QUOTE        the order was sent into a quote that had not re-priced a
                       spot move (killed live; "filled" in paper)
    ADVERSE_SELECTION  filled, and the token moved against us within 2 s of the
                       fill while spot was already moving against the model
    SLIPPAGE           fill price worse than the seen ask by more than a tick
    BAD_EXIT           an exit realised a worse price than the bid it was sent at
    MODEL_ERROR        held to settlement and lost with no execution defect
    STALE_DATA         the decision used spot older than the gate
    EXECUTION_ERROR    API errors on the order path
    OTHER

Loss share per category is the sum of net PnL of trades in that category over
the total loss. Unfilled orders carry no PnL but are counted, because they
are where the paper profit went.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from collections import defaultdict
from pathlib import Path

SEND_RE = re.compile(r"^(\d\d:\d\d:\d\d) INFO\s+SEND\s+(\S+)\s+(UP|DOWN)\s+([\d.]+) sh @ ([\d.]+)")


def load_window(charts: str, slug: str) -> dict | None:
    p = Path(charts) / f"{slug}.json"
    if not p.exists():
        return None
    try:
        return json.load(open(p))
    except (OSError, json.JSONDecodeError):
        return None


def at(points: list[dict], t: float, offset_ms: float = 0.0) -> dict | None:
    c = [p for p in points if p["t"] >= t + offset_ms]
    return c[0] if c else None


def before(points: list[dict], t: float) -> dict | None:
    c = [p for p in points if p["t"] <= t]
    return c[-1] if c else None


def spot_move_bps(points: list[dict], t: float, window_ms: float = 3000.0) -> float | None:
    a, b = before(points, t - window_ms), before(points, t)
    if not a or not b or not a.get("spot") or not b.get("spot"):
        return None
    return (b["spot"] / a["spot"] - 1.0) * 1e4


def classify_fill(buy: dict, sells: list[dict], settle: dict | None, win: dict | None) -> tuple[str, str]:
    side, t, px = buy["side"], buy["ts"], buy["price"]
    key = side.lower()
    if buy.get("slippage", 0.0) > 0.015:
        return "SLIPPAGE", f"paid {buy['slippage']:+.3f} over the seen ask"
    if win is None:
        return "OTHER", "window not archived"
    pts = win["points"]
    p0, p2 = before(pts, t), at(pts, t, 2000)
    sm = spot_move_bps(pts, t)
    move2 = (p2[key] - p0[key]) if (p0 and p2) else None
    against = sm is not None and ((sm > 0) != (side == "UP"))
    if move2 is not None and move2 < -0.05 and against:
        return "ADVERSE_SELECTION", f"mid {move2:+.3f} within 2 s, spot {sm:+.1f} bps before"
    for s in sells:
        if s.get("price") is not None and s.get("entry") is not None:
            b = before(pts, s["ts"])
            bid = b.get("db" if side == "DOWN" else "ub") if b else None
            if bid is not None and s["price"] < bid - 0.015:
                return "BAD_EXIT", f"{s.get('kind')} sold {s['price']:.3f} vs bid {bid:.3f}"
    if settle is not None and settle.get("pnl", 0.0) < 0 and not sells:
        return "MODEL_ERROR", f"held to the print, outcome {win.get('outcome')}"
    return "OTHER", ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="data/live_trades.jsonl")
    ap.add_argument("--charts", default="data/charts")
    ap.add_argument("--log", default="~/.pm2/logs/tpb-bot-error.log")
    ap.add_argument("--mode", default="LIVE-ARMED")
    args = ap.parse_args()

    rows = []
    for line in Path(args.trades).read_text(encoding="utf-8").splitlines() if Path(args.trades).exists() else []:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    rows = [r for r in rows if r.get("mode") == args.mode]
    by_slug: dict[str, dict] = defaultdict(lambda: {"buys": [], "sells": [], "settle": None})
    for r in rows:
        g = by_slug[r["slug"]]
        if r.get("event") == "fill" and r.get("action") == "BUY":
            g["buys"].append(r)
        elif r.get("event") == "fill" and r.get("action") == "SELL":
            g["sells"].append(r)
        elif r.get("event") == "settle":
            g["settle"] = r

    losses: dict[str, float] = defaultdict(float)
    details = []
    total_loss = 0.0
    for slug, g in by_slug.items():
        if not g["buys"]:
            continue
        pnl = (g["settle"] or {}).get("pnl")
        if pnl is None or pnl >= 0:
            continue
        win = load_window(args.charts, slug)
        cat, why = classify_fill(g["buys"][0], g["sells"], g["settle"], win)
        losses[cat] += pnl
        total_loss += pnl
        details.append((slug, pnl, cat, why))

    # unfilled orders from the log
    unfilled = defaultdict(int)
    p = Path(os.path.expanduser(args.log))
    if p.exists():
        lines = p.read_text(errors="replace").splitlines()
        for i, l in enumerate(lines):
            m = SEND_RE.search(l)
            if not m:
                continue
            hms, slug, side, _, _ = m.groups()
            res = None
            for j in range(i + 1, min(i + 15, len(lines))):
                if f"REJECT {slug}" in lines[j]:
                    res = lines[j].split()[-1]
                    break
                if "VENUE FILL BUY" in lines[j]:
                    res = "FILL"
                    break
            if res in (None, "FILL"):
                continue
            if res not in ("fok_unfillable", "quote_pulled"):
                unfilled[f"EXECUTION_ERROR:{res}"] += 1
                continue
            win = load_window(args.charts, slug)
            if win is None:
                unfilled["FOK_KILLED (no archive)"] += 1
                continue
            epoch = int(slug.rsplit("-", 1)[-1])
            day = dt.datetime.utcfromtimestamp(epoch).date()
            h, mi, s = map(int, hms.split(":"))
            t = dt.datetime(day.year, day.month, day.day, h, mi, s, tzinfo=dt.timezone.utc).timestamp() * 1000.0
            sm = spot_move_bps(win["points"], t + 500)
            if sm is not None and (sm > 0) == (side == "UP"):
                unfilled["STALE_QUOTE (killed)"] += 1
            else:
                unfilled["FOK_KILLED (other)"] += 1

    print(f"# Loss attribution, mode {args.mode}\n")
    print("| trade | net PnL | category | evidence |")
    print("|---|---:|---|---|")
    for slug, pnl, cat, why in details:
        print(f"| {slug} | {pnl:+.4f} | {cat} | {why} |")
    print("\n| category | share of total loss | USDC |")
    print("|---|---:|---:|")
    for cat, v in sorted(losses.items(), key=lambda kv: kv[1]):
        share = (v / total_loss) if total_loss else 0.0
        print(f"| {cat} | {share:.0%} | {v:+.4f} |")
    print(f"\ntotal loss {total_loss:+.4f} over {len(details)} losing trade(s)")
    if unfilled:
        print("\nUnfilled live orders (no PnL, but where the paper profit went):")
        for k, v in sorted(unfilled.items(), key=lambda kv: -kv[1]):
            print(f"  {k:32} {v}")


if __name__ == "__main__":
    main()
