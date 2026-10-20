#!/usr/bin/env python3
"""Historical basket replay on recorded Cheapside multi-asset book tapes.

Uses ``data/live_state.cheapside-paper.history`` plus archived runs under
``data/archive/cheapside-paper_*/``. Each history point carries executable
``ua``/``da`` asks and touch depth — not invented fills.

Compares average-cheap-ask triggers (default 0.20 vs 0.15) under the current
preflight gates, with fees from FeeSchedule and sequential FAK-style fills at
the recorded ask (+ configured slippage cap).

Outcomes: resolved settle events when present; else inferred from the last
valid mid in that market's tape (up_mid >= 0.5 => UP won).

Usage:
  .venv/bin/python scripts/replay_cheapside_basket_history.py
  .venv/bin/python scripts/replay_cheapside_basket_history.py --thresholds 0.20,0.15,0.12
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import orjson

from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.cheapside_basket import (
    LegQuote, leader_shares_for_target, run_preflight, unit_cost,
)

ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
ASSET_UP = {a: a.upper() for a in ASSETS}
FEE = FeeSchedule()
SLUG_RE = re.compile(r"^(btc|eth|sol|xrp|doge|bnb|hype)-updown-5m-(\d+)\.jsonl$")
SLIPPAGE = 0.01
SHARES = 5.0
TARGET = 0.10
ARM_MIN_LEFT = 120.0
MAX_SKEW_MS = 1000.0
MAX_AGE_MS = 1000.0  # relative alignment budget vs reference stamp


@dataclass
class Point:
    t: float
    cheap_ask: float
    cheap_side: str
    leader_side: str
    ua: float
    da: float
    ub: float | None
    db: float | None
    left: float
    uad: float
    dad: float
    spot: float
    up_mid: float
    down_mid: float


@dataclass
class BasketTrade:
    epoch: int
    threshold: float
    armed_t: float
    avg_cheap: float
    leader_asset: str
    leader_side: str
    leader_ask: float
    cheap_legs: list
    cheap_cost: float
    leader_shares: float
    leader_cost: float
    total_cost: float
    protected_pnl: float
    all_lose_pnl: float
    actual_pnl: float
    winners: dict
    aborted: bool = False
    abort_reason: str = ""
    complete: bool = False


@dataclass
class ThresholdSummary:
    threshold: float
    epochs_scanned: int = 0
    signals: int = 0
    armed: int = 0
    completed: int = 0
    aborted: int = 0
    preflight_rejects: int = 0
    net_pnl: float = 0.0
    worst_pnl: float = 0.0
    best_pnl: float = 0.0
    protected_hit_rate: float = 0.0  # fraction where protected scenario would win
    avg_cheap_at_arm: float = 0.0
    capital_peak: float = 0.0
    trades: list = field(default_factory=list)


def history_dirs() -> list[Path]:
    dirs = [ROOT / "data/live_state.cheapside-paper.history"]
    dirs += sorted((ROOT / "data/archive").glob("cheapside-paper_*/live_state.cheapside-paper.history"))
    return [d for d in dirs if d.exists()]


def index_epochs() -> dict[int, dict[str, Path]]:
    """epoch -> asset -> best (largest) history file."""
    best: dict[int, dict[str, Path]] = defaultdict(dict)
    sizes: dict[tuple[int, str], int] = {}
    for d in history_dirs():
        for f in d.glob("*.jsonl"):
            m = SLUG_RE.match(f.name)
            if not m:
                continue
            asset, epoch = m.group(1), int(m.group(2))
            key = (epoch, asset)
            sz = f.stat().st_size
            if sz >= sizes.get(key, -1):
                sizes[key] = sz
                best[epoch][asset] = f
    return {e: a for e, a in best.items() if set(a) == set(ASSETS)}


def load_series(path: Path) -> list[Point]:
    out: list[Point] = []
    for line in path.open("rb"):
        try:
            r = orjson.loads(line)
        except Exception:
            continue
        ua, da = r.get("ua"), r.get("da")
        if ua is None or da is None or ua == da:
            continue
        ub, db = r.get("ub"), r.get("db")
        if ub is not None and ua <= ub:
            continue
        if db is not None and da <= db:
            continue
        if (ua - (ub if ub is not None else ua - 0.02)) > 0.03 + 1e-9:
            continue
        if (da - (db if db is not None else da - 0.02)) > 0.03 + 1e-9:
            continue
        cheap_side = "UP" if ua < da else "DOWN"
        up_mid = float(r.get("up") or (ua + (ub or ua)) / 2)
        down_mid = float(r.get("down") or (da + (db or da)) / 2)
        leader_side = "UP" if up_mid >= down_mid else "DOWN"
        out.append(Point(
            t=float(r["t"]), cheap_ask=min(ua, da), cheap_side=cheap_side,
            leader_side=leader_side, ua=float(ua), da=float(da),
            ub=float(ub) if ub is not None else None,
            db=float(db) if db is not None else None,
            left=float(r.get("left") or 0.0),
            uad=float(r.get("uad") or 0.0), dad=float(r.get("dad") or 0.0),
            spot=float(r.get("spot") or 0.0),
            up_mid=up_mid, down_mid=down_mid,
        ))
    return out


def nearest(series: list[Point], t: float, max_skew_ms: float = MAX_SKEW_MS) -> Point | None:
    if not series:
        return None
    ts = [p.t for p in series]
    j = bisect.bisect_left(ts, t)
    cands = []
    if j < len(series):
        cands.append(series[j])
    if j > 0:
        cands.append(series[j - 1])
    best = min(cands, key=lambda p: abs(p.t - t))
    return best if abs(best.t - t) <= max_skew_ms else None


def load_resolved_outcomes() -> dict[str, str]:
    """slug -> 'UP'/'DOWN' from resolved settle events."""
    out: dict[str, str] = {}
    paths = [ROOT / "data/live_trades.cheapside-paper.jsonl"]
    paths += list((ROOT / "data/archive").glob("cheapside-paper_*/live_trades.cheapside-paper.jsonl"))
    for p in paths:
        if not p.exists():
            continue
        for line in p.open():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event") != "settle" or r.get("unresolved"):
                continue
            slug = r.get("slug") or ""
            # Some settles carry won_up / side
            if "won_up" in r:
                out[slug] = "UP" if r["won_up"] else "DOWN"
            elif r.get("outcome") in ("UP", "DOWN"):
                out[slug] = r["outcome"]
            elif "pnl" in r and r.get("side") in ("UP", "DOWN"):
                # Infer from PnL sign + side when explicit flag missing
                pass
    return out


def infer_winner(series: list[Point]) -> str | None:
    """Use late-tape mid: strongly priced side is the winner."""
    for p in reversed(series):
        if p.up_mid >= 0.90:
            return "UP"
        if p.down_mid >= 0.90:
            return "DOWN"
    for p in reversed(series):
        if p.up_mid >= 0.55:
            return "UP"
        if p.down_mid >= 0.55:
            return "DOWN"
    return None


def leg_cost(ask: float, shares: float) -> tuple[float, float]:
    limit = min(0.99, round(ask + SLIPPAGE, 4))
    u = unit_cost(ask, limit, FEE.taker_fee_per_share)
    return u, u * shares


def simulate_epoch(epoch: int, files: dict[str, Path], threshold: float,
                   resolved: dict[str, str], balance: float = 200.0) -> tuple[list[BasketTrade], dict]:
    series = {a: load_series(files[a]) for a in ASSETS}
    winners = {}
    for a in ASSETS:
        slug = f"{a}-updown-5m-{epoch}"
        winners[a] = resolved.get(slug) or infer_winner(series[a])
    stats = {"samples": 0, "trigger_hits": 0, "preflight_rejects": 0, "missing_winner": sum(v is None for v in winners.values())}

    # Reference timeline: BTC points with enough time left to arm.
    ref = [p for p in series["btc"] if p.left >= ARM_MIN_LEFT]
    trades: list[BasketTrade] = []
    armed_once = False

    i = 0
    while i < len(ref):
        p0 = ref[i]
        stats["samples"] += 1
        snaps = {}
        ok = True
        for a in ASSETS:
            pt = nearest(series[a], p0.t)
            if pt is None or pt.left < ARM_MIN_LEFT:
                ok = False
                break
            snaps[a] = pt
        if not ok:
            i += 1
            continue

        ranked = sorted(
            ((a, snaps[a]) for a in ASSETS),
            key=lambda x: (x[1].cheap_ask, x[0]),
        )
        cheap6 = ranked[:6]
        leader_a, leader_p = ranked[6]
        avg = sum(p.cheap_ask for _, p in cheap6) / 6.0
        if avg >= threshold:
            i += 1
            continue
        stats["trigger_hits"] += 1

        # One basket per epoch in this study (matches live one-armed-basket rule).
        if armed_once:
            break

        cheap_quotes, fee_fns = [], []
        for a, p in cheap6:
            book_ask = p.ua if p.cheap_side == "UP" else p.da
            book_bid = (p.ub if p.cheap_side == "UP" else p.db) or book_ask - 0.02
            depth = p.uad if p.cheap_side == "UP" else p.dad
            cheap_quotes.append(LegQuote(
                slug=f"{a}-{epoch}", asset=ASSET_UP[a], side=p.cheap_side,
                ask=book_ask, bid=book_bid, depth=depth, fee_per_share_at_limit=0.0,
                quote_age_ms=abs(p.t - p0.t), spread=book_ask - book_bid,
            ))
            fee_fns.append(FEE.taker_fee_per_share)
        la = leader_p.ua if leader_p.leader_side == "UP" else leader_p.da
        lb = (leader_p.ub if leader_p.leader_side == "UP" else leader_p.db) or la - 0.02
        ldepth = leader_p.uad if leader_p.leader_side == "UP" else leader_p.dad
        lq = LegQuote(
            slug=f"{leader_a}-{epoch}", asset=ASSET_UP[leader_a], side=leader_p.leader_side,
            ask=la, bid=lb, depth=ldepth, fee_per_share_at_limit=0.0,
            quote_age_ms=abs(leader_p.t - p0.t), spread=la - lb,
        )
        pf = run_preflight(
            cheap_quotes=cheap_quotes, leader_quote=lq, shares_per_cheap=SHARES,
            target_pnl=TARGET, slippage=SLIPPAGE, fee_fn_leader=FEE.taker_fee_per_share,
            fee_fns_cheap=fee_fns, available_cash=balance, cash_buffer_usdc=1.0,
            cash_util_max=0.95, min_leader_ask=0.55, max_leader_ask=0.78,
            max_leader_unit=0.80, max_basket_notional=80.0, max_leader_notional=60.0,
            max_leader_multiple=15.0, max_all_lose=80.0, max_spread=0.03,
            max_quote_age_ms=MAX_AGE_MS, max_quote_skew_ms=MAX_SKEW_MS,
            min_time_remaining_s=ARM_MIN_LEFT, time_remaining_s=min(p.left for p in snaps.values()),
            avg_cheap_ask=avg, max_average_ask=threshold,
        )
        if not pf.ok:
            stats["preflight_rejects"] += 1
            i += 1
            continue

        # Execute cheap legs sequentially along the tape (+250 ms steps).
        t_exec = p0.t
        cheap_cost = 0.0
        cheap_legs = []
        aborted = False
        abort_reason = ""
        for a, p_arm in cheap6:
            # Advance ~250ms and re-read ask for this asset
            t_exec += 250.0
            pt = nearest(series[a], t_exec, max_skew_ms=2000.0) or p_arm
            ask = pt.ua if p_arm.cheap_side == "UP" else pt.da
            depth = pt.uad if p_arm.cheap_side == "UP" else pt.dad
            # Abort checks (full policy)
            lead_now = nearest(series[leader_a], t_exec, max_skew_ms=2000.0) or leader_p
            lead_ask = lead_now.ua if leader_p.leader_side == "UP" else lead_now.da
            if lead_ask > 0.82 + 1e-9:
                aborted, abort_reason = True, f"favorite ask {lead_ask:.3f} > 0.82"
                break
            lead_unit = unit_cost(lead_ask, min(0.99, round(lead_ask + SLIPPAGE, 4)), FEE.taker_fee_per_share)
            if lead_unit > 0.80 + 1e-9:
                aborted, abort_reason = True, f"favorite unit {lead_unit:.3f} > 0.80"
                break
            if depth + 1e-9 < SHARES:
                aborted, abort_reason = True, f"insufficient depth on {a}"
                break
            _, cost = leg_cost(ask, SHARES)
            cheap_cost += cost
            cheap_legs.append({"asset": ASSET_UP[a], "side": p_arm.cheap_side, "ask": ask, "cost": cost})

        if aborted and not cheap_legs:
            stats["preflight_rejects"] += 1
            i += 1
            continue

        leader_cost = leader_shares = 0.0
        complete = False
        if not aborted:
            t_exec += 200.0
            lead_now = nearest(series[leader_a], t_exec, max_skew_ms=2000.0) or leader_p
            lead_ask = lead_now.ua if leader_p.leader_side == "UP" else lead_now.da
            if lead_ask > 0.82 + 1e-9 or lead_ask < 0.55 - 1e-9:
                aborted, abort_reason = True, f"leader band fail at {lead_ask:.3f}"
            else:
                lead_unit = unit_cost(lead_ask, min(0.99, round(lead_ask + SLIPPAGE, 4)), FEE.taker_fee_per_share)
                if lead_unit > 0.80 + 1e-9:
                    aborted, abort_reason = True, f"leader unit {lead_unit:.3f}"
                else:
                    need, prot = leader_shares_for_target(cheap_cost, lead_unit, TARGET)
                    depth = lead_now.uad if leader_p.leader_side == "UP" else lead_now.dad
                    if depth + 1e-9 < need:
                        aborted, abort_reason = True, f"leader depth {depth:.1f} < {need:.2f}"
                    else:
                        leader_shares = need
                        leader_cost = need * lead_unit
                        complete = True

        # Settlement
        actual = -cheap_cost - leader_cost
        for leg in cheap_legs:
            a = leg["asset"].lower()
            if winners.get(a) == leg["side"]:
                actual += SHARES
        if complete and winners.get(leader_a) == leader_p.leader_side:
            actual += leader_shares
        protected = leader_shares - cheap_cost - leader_cost if complete else -cheap_cost - leader_cost
        all_lose = -(cheap_cost + leader_cost)

        trades.append(BasketTrade(
            epoch=epoch, threshold=threshold, armed_t=p0.t, avg_cheap=avg,
            leader_asset=ASSET_UP[leader_a], leader_side=leader_p.leader_side,
            leader_ask=la, cheap_legs=cheap_legs, cheap_cost=cheap_cost,
            leader_shares=leader_shares, leader_cost=leader_cost,
            total_cost=cheap_cost + leader_cost, protected_pnl=protected,
            all_lose_pnl=all_lose, actual_pnl=actual,
            winners={ASSET_UP[a]: winners[a] for a in ASSETS},
            aborted=aborted, abort_reason=abort_reason, complete=complete,
        ))
        armed_once = True
        break

    return trades, stats


def summarize(threshold: float, all_trades: list[BasketTrade], epoch_stats: list[dict]) -> ThresholdSummary:
    s = ThresholdSummary(threshold=threshold)
    s.epochs_scanned = len(epoch_stats)
    s.signals = sum(x["trigger_hits"] for x in epoch_stats)
    s.preflight_rejects = sum(x["preflight_rejects"] for x in epoch_stats)
    s.trades = all_trades
    s.armed = len(all_trades)
    s.completed = sum(1 for t in all_trades if t.complete)
    s.aborted = sum(1 for t in all_trades if t.aborted)
    pnls = [t.actual_pnl for t in all_trades]
    s.net_pnl = round(sum(pnls), 4) if pnls else 0.0
    s.worst_pnl = round(min(pnls), 4) if pnls else 0.0
    s.best_pnl = round(max(pnls), 4) if pnls else 0.0
    s.avg_cheap_at_arm = round(sum(t.avg_cheap for t in all_trades) / len(all_trades), 4) if all_trades else 0.0
    s.capital_peak = round(max((t.total_cost for t in all_trades), default=0.0), 4)
    # How often the protected scenario was the actual outcome
    prot_hits = 0
    for t in all_trades:
        if not t.complete:
            continue
        cheap_lose = all(t.winners.get(leg["asset"]) != leg["side"] for leg in t.cheap_legs)
        lead_win = t.winners.get(t.leader_asset) == t.leader_side
        if cheap_lose and lead_win:
            prot_hits += 1
    s.protected_hit_rate = round(prot_hits / s.completed, 4) if s.completed else 0.0
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", default="0.20,0.15")
    ap.add_argument("--out", default=str(ROOT / "data/replay/cheapside_basket_avg_threshold.json"))
    args = ap.parse_args()
    thresholds = [float(x) for x in args.thresholds.split(",") if x.strip()]
    epochs = index_epochs()
    resolved = load_resolved_outcomes()
    print(f"complete 7-asset epochs: {len(epochs)}  resolved settle slugs: {len(resolved)}")
    if not epochs:
        print("No synchronized seven-market history found.", file=sys.stderr)
        return 2

    summaries = {}
    for thr in thresholds:
        trades: list[BasketTrade] = []
        estat = []
        for epoch in sorted(epochs):
            tlist, st = simulate_epoch(epoch, epochs[epoch], thr, resolved)
            trades.extend(tlist)
            estat.append(st)
        key = f"{thr:.2f}"
        summaries[key] = asdict(summarize(thr, trades, estat))
        # Don't dump full trade legs into huge output — keep compact
        summaries[key]["trades"] = [
            {k: v for k, v in asdict(t).items() if k != "cheap_legs"}
            | {"n_cheap_filled": len(t.cheap_legs)}
            for t in trades
        ]

    doc = {
        "source": [str(d) for d in history_dirs()],
        "epochs": sorted(epochs),
        "n_epochs": len(epochs),
        "assumptions": {
            "fills": "recorded ua/da ask + 1c slippage cap + FeeSchedule taker fee",
            "alignment": f"cross-asset quote skew <= {MAX_SKEW_MS}ms",
            "arm_min_left_s": ARM_MIN_LEFT,
            "shares_per_cheap": SHARES,
            "target_pnl": TARGET,
            "gates": "favourite 0.55-0.78, unit<=0.80, abort 0.82, cash/depth/notional caps",
            "outcomes": "resolved settle events when present, else late-tape mid inference",
            "limitation": (
                f"Only {len(epochs)} complete seven-market epochs are in local history/archives. "
                "This is too small for a strong profitability claim; treat as directional evidence."
            ),
        },
        "summaries": summaries,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    print("\n=== Average-ask trigger comparison (historical) ===")
    for thr in thresholds:
        s = summaries[f"{thr:.2f}"]
        print(f"\nthreshold < {thr:.2f}")
        print(f"  epochs={s['epochs_scanned']}  signal_hits={s['signals']}  "
              f"preflight_reject={s['preflight_rejects']}")
        print(f"  armed={s['armed']}  completed={s['completed']}  aborted={s['aborted']}")
        print(f"  net_pnl={s['net_pnl']:+.4f}  best={s['best_pnl']:+.4f}  "
              f"worst={s['worst_pnl']:+.4f}  capital_peak={s['capital_peak']:.2f}")
        print(f"  avg_cheap_at_arm={s['avg_cheap_at_arm']:.4f}  "
              f"protected_scenario_hit_rate={s['protected_hit_rate']:.2%}")
    print(f"\nwrote {out}")
    print("LIMITATION:", doc["assumptions"]["limitation"])

    a, b = summaries.get("0.20"), summaries.get("0.15")
    if a and b:
        print("\n--- Verdict ---")
        print(f"0.20 net PnL {a['net_pnl']:+.4f} on {a['completed']} completes / {a['armed']} armed")
        print(f"0.15 net PnL {b['net_pnl']:+.4f} on {b['completed']} completes / {b['armed']} armed")
        if b["armed"] == 0:
            print("0.15 armed no baskets after gates — no profitability evidence.")
        elif b["net_pnl"] > 0:
            print("0.15 is profitable on this tiny historical sample.")
        elif b["net_pnl"] > a["net_pnl"]:
            print("0.15 is less bad than 0.20 here, but not profitable.")
        else:
            print("0.15 is not profitable on the available historical sample.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
