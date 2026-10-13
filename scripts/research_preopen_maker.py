"""PREOPEN_MAKER replay (2026-10-06): resting bids on both tokens of the next BTC 5-minute market, placed P seconds
before the open and pulled at the open (+C), on the 200 ms window archive (data/replay/paired_exit/btc.npz), which
holds the last 60 s before every open: both tokens' best bid / ask and touch depth, the composite spot, the strike
(known only at the open) and the venue outcome.

    .venv/bin/python scripts/research_preopen_maker.py          -> docs/preopen_maker_2026-10-06.md, results/pom_replay_2026-10-06.json

Fill model for a resting BUY at price B (the paper venue's rule, and two variants):
  * crossed     the ask is <= B when the bid is placed: the bid executes at once as a taker at the ask (taker fee)
  * strict      (the paper venue) the ask trades STRICTLY through the bid: some later sample shows ask < B; filled at B,
                maker, no fee. Queue position is unknown: this counts every trade-through as ours
  * persist     strict and the ask stays below B for >= 2 consecutive samples (>= 400 ms): a conservative variant
  * touch       ask <= B (an optimistic upper bound; a touch is not a fill)
  * haircut     strict with a random 50 % of fills refused (queue competition stand-in)
All fills are whole-order (no partial fills can be reconstructed: no trade sizes in the archive -- stated as a limitation).
Nothing after the placement timestamp is used to decide anything; the outcome and the strike are read only to score.
"""
from __future__ import annotations

import collections
import json
import math
import sys
import time

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/src")
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L  # noqa: E402
from troll_poly_bot.strategy import preopen_maker as pm  # noqa: E402

ROOT = "/root/troll-poly-bot"
BIDS = (0.44, 0.45, 0.46, 0.47, 0.48, 0.49, 0.50)
PLACEMENTS = (60, 45, 30, 20, 10)
RULES = ("strict", "persist", "touch", "haircut")
Q = 10.0
FEE = L.fee
RNG = np.random.default_rng(11)
SETTLE_LAG_S = 120.0          # cash comes back about two minutes after the close (venue redemption)


def side_arrays(w, side):
    return (w.ua, w.ub, w.uad, w.ubd) if side == "UP" else (w.da, w.db, w.dad, w.dbd)


def simulate(w, bid, placement_s, cancel_after_s, rule, qty=Q):
    """One window, one parameter set. Returns the market record (classification, fills, pair, pnl, features)."""
    t_place = w.open - placement_s * 1000.0
    t_cancel = w.open + cancel_after_s * 1000.0
    ip = int(np.searchsorted(w.T, t_place, side="left"))
    if ip >= w.n or w.T[ip] > w.open:
        return None
    ic = int(np.searchsorted(w.T, t_cancel, side="left"))
    rec = {"slug": w.slug, "open": w.open, "day": w.day, "outcome": w.outcome, "strike": w.strike, "placement_s": placement_s, "bid": bid,
           "spot_at_placement": float(w.spot[ip]) if not np.isnan(w.spot[ip]) else None, "fills": {}, "book_at_placement": {}}
    for side in ("UP", "DOWN"):
        ask, bidarr, adep, bdep = side_arrays(w, side)
        a0, b0 = ask[ip], bidarr[ip]
        rec["book_at_placement"][side] = (None if np.isnan(b0) else float(b0), None if np.isnan(a0) else float(a0))
        fill = None
        if not np.isnan(a0) and a0 <= bid + 1e-9:
            fill = {"t": float(w.T[ip]), "price": float(a0), "crossed": True, "idx": ip}
        else:
            seg = ask[ip + 1:max(ic, ip + 1)]
            if seg.size:
                if rule == "touch":
                    m = (seg <= bid + 1e-9) & ~np.isnan(seg)
                else:
                    m = (seg < bid - 1e-9) & ~np.isnan(seg)
                    if rule == "persist" and m.size > 1:
                        m2 = m.copy(); m2[:-1] &= m[1:]; m2[-1] = False
                        m = m2
                if m.any():
                    j = ip + 1 + int(np.argmax(m))
                    if rule == "haircut" and RNG.random() < 0.5:
                        fill = None
                    else:
                        fill = {"t": float(w.T[j]), "price": float(bid), "crossed": False, "idx": j}
        if fill is not None:
            j = fill["idx"]
            fill.update({"s_before_open": (w.open - fill["t"]) / 1000.0, "book_at_fill": (None if np.isnan(bidarr[j]) else float(bidarr[j]), None if np.isnan(ask[j]) else float(ask[j])),
                         "spot_at_fill": None if np.isnan(w.spot[j]) else float(w.spot[j]), "depth_ask": None if np.isnan(adep[j]) else float(adep[j]),
                         "depth_bid": None if np.isnan(bdep[j]) else float(bdep[j]), "fee": qty * FEE(fill["price"]) if fill["crossed"] else 0.0})
            # spot and token before / after the fill (5 s), the token's path after
            jb = int(np.searchsorted(w.T, fill["t"] - 5000.0)); ja = min(int(np.searchsorted(w.T, fill["t"] + 5000.0)), w.n - 1)
            fill["spot_ret_before_bps"] = None if np.isnan(w.spot[jb]) or np.isnan(w.spot[j]) else float((w.spot[j] / w.spot[jb] - 1) * 1e4)
            fill["spot_ret_after_bps"] = None if np.isnan(w.spot[ja]) or np.isnan(w.spot[j]) else float((w.spot[ja] / w.spot[j] - 1) * 1e4)
            mids = L.mid_of(ask, bidarr)
            fill["tok_before"] = None if np.isnan(mids[jb]) else float(mids[jb]); fill["tok_at"] = None if np.isnan(mids[j]) else float(mids[j])
            fill["tok_after"] = None if np.isnan(mids[ja]) else float(mids[ja])
            path = {}
            for lab, t in (("open", w.open), ("open+30", w.open + 30_000.0), ("open+60", w.open + 60_000.0)):
                k = min(int(np.searchsorted(w.T, t, side="right")) - 1, w.n - 1)
                path[lab] = None if k < 0 or np.isnan(mids[k]) else float(mids[k])
            fill["tok_path"] = path
            rec["fills"][side] = fill
    u = qty if "UP" in rec["fills"] else 0.0
    d = qty if "DOWN" in rec["fills"] else 0.0
    rec["class"] = pm.classify(u, d, qty)
    cost_u = u * rec["fills"]["UP"]["price"] if u else 0.0
    cost_d = d * rec["fills"]["DOWN"]["price"] if d else 0.0
    fees = sum(f["fee"] for f in rec["fills"].values())
    payout = u if w.outcome == "UP" else d
    rec["pnl"] = payout - cost_u - cost_d - fees
    rec["cost"] = cost_u + cost_d
    rec["fees"] = fees
    rec["pair"] = pm.pair_economics(u, cost_u, d, cost_d, fees) if (u and d) else None
    if rec["class"] in ("UP_ONLY", "DOWN_ONLY"):
        side = "UP" if u else "DOWN"
        f = rec["fills"][side]
        rec["one_sided"] = {"side": side, "price": f["price"], "won": w.outcome == side, "pnl": rec["pnl"], "implied_at_fill": f["tok_at"],
                            "crossed": f["crossed"]}
    return rec


def inventory_path(recs, qty=Q):
    """Capital locked over time: resting bids (2 x qty x bid) from placement to cancel/fill, filled cost until close + lag.
    Returns (max locked, max unmatched shares, utilisation series)."""
    events = []
    for r in recs:
        t0 = r["open"] - r["placement_s"] * 1000.0
        events.append((t0, +2 * qty * r["bid"], 0))
        # resting capital released at the open (cancel) or converted into position cost at the fill
        events.append((r["open"], -2 * qty * r["bid"], 0))
        for side, f in r["fills"].items():
            events.append((f["t"], +qty * f["price"], 0))
            events.append((r["open"] + 300_000.0 + SETTLE_LAG_S * 1000.0, -qty * f["price"], 0))
        if r["class"] in ("UP_ONLY", "DOWN_ONLY"):
            t = next(iter(r["fills"].values()))["t"]
            events.append((t, 0, +qty)); events.append((r["open"] + 300_000.0, 0, -qty))
    events.sort()
    locked = unm = 0.0; mx = mxu = 0.0
    for _, dc, du in events:
        locked += dc; unm += du
        mx = max(mx, locked); mxu = max(mxu, unm)
    return mx, mxu


def metrics(recs):
    n = len(recs)
    if n == 0:
        return {}
    pnl = np.array([r["pnl"] for r in recs])
    traded = [r for r in recs if r["class"] != "NO_FILLS"]
    one = [r for r in recs if r["class"] in ("UP_ONLY", "DOWN_ONLY")]
    both = [r for r in recs if r["class"] == "BOTH"]
    cum = np.cumsum(pnl); dd = float(np.max(np.maximum.accumulate(cum) - cum)) if n else 0.0
    streak = best = 0
    for v in pnl:
        streak = streak + 1 if v < 0 else 0
        best = max(best, streak)
    mx_locked, mx_unm = inventory_path(recs)
    orders = 2 * n
    fills = sum(len(r["fills"]) for r in recs)
    one_w = sum(1 for r in one if r["one_sided"]["won"])
    out = {"markets": n, "orders": orders, "fills": fills, "fill_rate_per_order": fills / orders, "markets_with_fill": len(traded) / n,
           "one_sided_rate": len(one) / n, "pair_rate": len(both) / n, "one_sided_n": len(one), "one_sided_win": one_w / len(one) if one else None,
           "one_sided_avg_price": float(np.mean([r["one_sided"]["price"] for r in one])) if one else None,
           "one_sided_implied": float(np.mean([r["one_sided"]["implied_at_fill"] for r in one if r["one_sided"]["implied_at_fill"] is not None])) if one else None,
           "one_sided_pnl": float(sum(r["pnl"] for r in one)), "pair_n": len(both),
           "pair_cost_mean": float(np.mean([r["pair"]["combined_cost_per_share"] for r in both])) if both else None,
           "pair_cost_median": float(np.median([r["pair"]["combined_cost_per_share"] for r in both])) if both else None,
           "pair_gross": float(sum(r["pair"]["guaranteed_gross"] for r in both)), "pair_fees": float(sum(r["fees"] for r in both)),
           "pair_net": float(sum(r["pair"]["net_guaranteed"] for r in both)),
           "pnl": float(pnl.sum()), "pnl_per_market": float(pnl.mean()), "pnl_median": float(np.median(pnl)),
           "ev_per_share": float(pnl.sum() / max(fills * Q, 1)), "ev_per_order": float(pnl.sum() / orders), "max_drawdown": dd,
           "longest_losing_streak": best, "max_capital_locked": mx_locked, "max_unmatched_shares": mx_unm,
           "crossed_fills": sum(1 for r in recs for f in r["fills"].values() if f["crossed"]),
           "worst_market": float(pnl.min()), "best_market": float(pnl.max()), "top2_share": float(np.sort(pnl)[-2:].sum() / pnl.sum()) if pnl.sum() > 0 else None}
    byday = collections.defaultdict(float)
    for r in recs:
        byday[r["day"]] += r["pnl"]
    out["days"] = len(byday); out["worst_day"] = float(min(byday.values())); out["best_day"] = float(max(byday.values()))
    out["days_positive"] = sum(1 for v in byday.values() if v > 0) / len(byday)
    return out


def run(wins, bid, placement_s, cancel_after_s, rule):
    recs = [simulate(w, bid, placement_s, cancel_after_s, rule) for w in wins]
    return [r for r in recs if r is not None]


def fmt_pct(v):
    return "-" if v is None else f"{v:.0%}"


def main():
    t0 = time.time()
    wins = L.load_asset("BTC")
    days = sorted({w.day for w in wins})
    res = {"windows": len(wins), "days": len(days), "period": [time.strftime("%m-%d %H:%M", time.gmtime(wins[0].open / 1000)), time.strftime("%m-%d %H:%M", time.gmtime(wins[-1].open / 1000))]}
    R = []
    R.append("# PREOPEN_MAKER (PBot-6 pattern): replay on the BTC 5-minute archive (2026-10-06)\n")
    R.append(f"{len(wins)} BTC windows, {res['period'][0]} -> {res['period'][1]} UTC, {len(days)} UTC days; every window has the 60 s before its open. "
             "Bids of 10 shares on BOTH tokens, placed P s before the open, pulled at the open. Fill rules: see the script header; "
             "**strict** = the paper venue's rule (the ask trades strictly through the bid), **persist** = strict for >= 400 ms, **haircut** = strict with half the fills refused, "
             "**touch** = an optimistic upper bound. Maker fills pay no fee; a bid that crosses the ask at placement executes as a taker at the ask with the taker fee. "
             "Partial fills cannot be reconstructed (no trade sizes in the archive). No decision uses anything after the placement timestamp.\n")
    # ---- 1. the bid table (P = 45 s, strict)
    R.append("## 1. Bid price (placement T-45 s, strict fills)\n")
    R.append("| bid | fill rate / order | markets with a fill | one-sided | pairs | one-sided win | implied at fill | avg pair cost | EV / share | EV / order | P&L | max DD | worst day | crossed |\n|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    grid = {}
    for b in BIDS:
        m = metrics(run(wins, b, 45, 0, "strict")); grid[("strict", b, 45)] = m
        R.append(f"| {b:.2f} | {m['fill_rate_per_order']:.1%} | {m['markets_with_fill']:.0%} | {fmt_pct(m['one_sided_rate'])} | {fmt_pct(m['pair_rate'])} | {fmt_pct(m['one_sided_win'])} | "
                 f"{'-' if m['one_sided_implied'] is None else f'{m[chr(111)+chr(110)+chr(101)+chr(95)+chr(115)+chr(105)+chr(100)+chr(101)+chr(100)+chr(95)+chr(105)+chr(109)+chr(112)+chr(108)+chr(105)+chr(101)+chr(100)]:.3f}'} | "
                 f"{'-' if m['pair_cost_mean'] is None else f'{m[chr(112)+chr(97)+chr(105)+chr(114)+chr(95)+chr(99)+chr(111)+chr(115)+chr(116)+chr(95)+chr(109)+chr(101)+chr(97)+chr(110)]:.3f}'} | "
                 f"{m['ev_per_share']:+.4f} | {m['ev_per_order']:+.3f} | {m['pnl']:+.1f} | {m['max_drawdown']:.1f} | {m['worst_day']:+.1f} | {m['crossed_fills']} |")
    # ---- 2. the timing table (bid 0.47, strict)
    R.append("\n## 2. Placement time (bid 0.47, strict fills)\n")
    R.append("| placement | fill rate / order | one-sided | pairs | one-sided win | implied | EV / order | P&L | max DD |\n|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in PLACEMENTS:
        m = metrics(run(wins, 0.47, p, 0, "strict")); grid[("strict", 0.47, p)] = m
        R.append(f"| T-{p} | {m['fill_rate_per_order']:.1%} | {fmt_pct(m['one_sided_rate'])} | {fmt_pct(m['pair_rate'])} | {fmt_pct(m['one_sided_win'])} | "
                 f"{'-' if m['one_sided_implied'] is None else format(m['one_sided_implied'], '.3f')} | {m['ev_per_order']:+.3f} | {m['pnl']:+.1f} | {m['max_drawdown']:.1f} |")
    # ---- 3. fill rules (bid 0.47, T-45)
    R.append("\n## 3. Fill model sensitivity (bid 0.47, T-45)\n")
    R.append("| rule | fill rate / order | one-sided | pairs | one-sided win | P&L | pair net | one-sided P&L |\n|---|---:|---:|---:|---:|---:|---:|---:|")
    for rule in RULES:
        m = metrics(run(wins, 0.47, 45, 0, rule)); grid[(rule, 0.47, 45)] = m
        R.append(f"| {rule} | {m['fill_rate_per_order']:.1%} | {fmt_pct(m['one_sided_rate'])} | {fmt_pct(m['pair_rate'])} | {fmt_pct(m['one_sided_win'])} | {m['pnl']:+.1f} | {m['pair_net']:+.1f} | {m['one_sided_pnl']:+.1f} |")
    # ---- 4. one-sided analysis, UP and DOWN apart, with the comparison to a random purchase at the same price
    base = run(wins, 0.47, 45, 0, "strict")
    R.append("\n## 4. One-sided fills (bid 0.47, T-45, strict): does the fill carry information?\n")
    R.append("| side | n | won | price | token mid at fill | mid at open | mid +30 s | mid +60 s | P&L | EV / share at the fill price | EV if it were a random 0.47 purchase |\n|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    # the random-purchase benchmark: the win rate of the same token among ALL windows whose mid at T-45 was within 3c of 0.47
    bench = {}
    for side in ("UP", "DOWN"):
        wins_s = []
        for w in wins:
            ip = int(np.searchsorted(w.T, w.open - 45_000.0, side="left"))
            if ip >= w.n: continue
            ask, bidarr, _, _ = side_arrays(w, side)
            mid = L.mid_of(ask, bidarr)[ip]
            if not np.isnan(mid) and abs(mid - 0.47) <= 0.03:
                wins_s.append(w.outcome == side)
        bench[side] = (float(np.mean(wins_s)) if wins_s else None, len(wins_s))
    for side in ("UP", "DOWN"):
        g = [r for r in base if r["class"] == f"{side}_ONLY"]
        if not g:
            continue
        f = [r["fills"][side] for r in g]
        won = np.mean([r["one_sided"]["won"] for r in g]); price = np.mean([x["price"] for x in f])
        at = np.mean([x["tok_at"] for x in f if x["tok_at"] is not None]); op = np.mean([x["tok_path"]["open"] for x in f if x["tok_path"]["open"] is not None])
        p30 = np.mean([x["tok_path"]["open+30"] for x in f if x["tok_path"]["open+30"] is not None]); p60 = np.mean([x["tok_path"]["open+60"] for x in f if x["tok_path"]["open+60"] is not None])
        pb, nb = bench[side]
        R.append(f"| {side} only | {len(g)} | {won:.0%} | {price:.3f} | {at:.3f} | {op:.3f} | {p30:.3f} | {p60:.3f} | {sum(r['pnl'] for r in g):+.1f} | {pm.one_sided_ev(won, price):+.3f} | "
                 f"{'-' if pb is None else f'{pm.one_sided_ev(pb, 0.47):+.3f} (win {pb:.0%}, n {nb})'} |")
    R.append("\nReading: the 'mid at fill' column is what the market thought the token was worth when our bid filled; if the win rate "
             "sits far above it the fill carries information, if it sits at it the fill is a random liquidity event, if below it is adverse selection.\n")
    # ---- 5. pair analysis
    both = [r for r in base if r["class"] == "BOTH"]
    R.append("## 5. Pairs (bid 0.47, T-45, strict)\n")
    if both:
        cps = [r["pair"]["combined_cost_per_share"] for r in both]
        R.append(f"pairs {len(both)} of {len(base)} markets; combined cost per share mean {np.mean(cps):.3f}, median {np.median(cps):.3f}, min {min(cps):.3f}, max {max(cps):.3f}; "
                 f"guaranteed gross {sum(r['pair']['guaranteed_gross'] for r in both):.1f}, fees {sum(r['fees'] for r in both):.2f}, net guaranteed {sum(r['pair']['net_guaranteed'] for r in both):+.2f}; "
                 f"capital locked per pair {np.mean([r['pair']['capital_locked'] for r in both]):.2f} USDC for about {300 + SETTLE_LAG_S:.0f} s.\n")
        R.append("| pair cost threshold | pairs at or under | net guaranteed |\n|---:|---:|---:|")
        for th in (0.94, 0.95, 0.96, 0.97, 0.98, 0.99, 1.00):
            g = [r for r in both if r["pair"]["combined_cost_per_share"] <= th + 1e-9]
            R.append(f"| {th:.2f} | {len(g)} | {sum(r['pair']['net_guaranteed'] for r in g):+.2f} |")
        crossed_pairs = sum(1 for r in both if any(f["crossed"] for f in r["fills"].values()))
        R.append(f"\n{crossed_pairs} of the {len(both)} pairs include a leg that crossed the ask at placement (a taker fill at the ask, fee charged).\n")
    else:
        R.append("No market filled on both sides.\n")
    # ---- 6. fill selection: what differs between filled and unfilled sides
    R.append("## 6. Fill selection (bid 0.47, T-45, strict): filled vs not filled\n")
    R.append("| group | n | spot 5 s before fill (bps) | spot 5 s after (bps) | token mid before -> at -> after | ask depth | spread | spot - strike at fill (bps) | prev market same direction | s before open |\n|---|---:|---:|---:|---|---:|---:|---:|---:|---:|")
    prev_out = {}
    for k in range(1, len(wins)):
        prev_out[wins[k].slug] = wins[k - 1].outcome
    for lab, sel in (("filled, won", lambda r, s, f: r["outcome"] == s), ("filled, lost", lambda r, s, f: r["outcome"] != s)):
        rows = []
        for r in base:
            for s, f in r["fills"].items():
                if sel(r, s, f):
                    rows.append((r, s, f))
        if not rows:
            continue
        def avg(key):
            v = [f[key] for _, _, f in rows if f.get(key) is not None]; return float(np.mean(v)) if v else float("nan")
        dist = [((f["spot_at_fill"] / r["strike"]) - 1) * 1e4 * (1 if s == "UP" else -1) for r, s, f in rows if f["spot_at_fill"] and r["strike"]]
        same = [prev_out.get(r["slug"]) == s for r, s, f in rows if r["slug"] in prev_out]
        spreads = [f["book_at_fill"][1] - f["book_at_fill"][0] for _, _, f in rows if None not in f["book_at_fill"]]
        R.append(f"| {lab} | {len(rows)} | {avg('spot_ret_before_bps'):+.2f} | {avg('spot_ret_after_bps'):+.2f} | {avg('tok_before'):.3f} -> {avg('tok_at'):.3f} -> {avg('tok_after'):.3f} | {avg('depth_ask'):.0f} | "
                 f"{np.mean(spreads) if spreads else float('nan'):.3f} | {np.mean(dist) if dist else float('nan'):+.1f} | {np.mean(same) if same else float('nan'):.0%} | {avg('s_before_open'):.0f} |")
    R.append("\n'spot - strike at fill' is signed toward the filled side (positive = the spot already favoured the side we bought); the strike itself is known only at the open, so this row is diagnostic, not a usable signal.\n")
    # ---- 7. previous market vs strike (causal check)
    R.append("## 7. Previous market, strike and a causal pre-open lean\n")
    diffs = []; acc = []
    for k in range(1, len(wins)):
        a, b = wins[k - 1], wins[k]
        if b.open - a.close > 1000.0:
            continue
        ia = int(np.searchsorted(a.T, a.close - 60_000.0)); seg = a.spot[ia:]
        seg = seg[~np.isnan(seg)]
        if seg.size < 10 or np.isnan(a.spot[-1]):
            continue
        twap = float(seg.mean()); close = float(a.spot[-1])
        diffs.append((close - b.strike, twap - b.strike, (twap - b.strike) / b.strike * 1e4, (close - b.strike) / b.strike * 1e4))
        # the causal lean at T-45 of market b: spot now vs the trailing 60 s TWAP ending now
        ip = int(np.searchsorted(b.T, b.open - 45_000.0, side="left"))
        jt = int(np.searchsorted(b.T, b.open - 105_000.0, side="left"))
        if ip < b.n and jt < ip:
            s = b.spot[jt:ip + 1]; s = s[~np.isnan(s)]
            if s.size >= 5 and not np.isnan(b.spot[ip]):
                lean = "UP" if b.spot[ip] > s.mean() else "DOWN"
                acc.append(lean == b.outcome)
    if diffs:
        d = np.array(diffs)
        R.append(f"Consecutive markets {len(d)}: |previous close - next strike| median {np.median(np.abs(d[:, 3])):.2f} bps, |previous 60 s TWAP - next strike| median {np.median(np.abs(d[:, 2])):.2f} bps "
                 f"(the strike IS the trailing 60 s TWAP at the open, so the previous market's closing TWAP equals the next strike up to the last 60 s of drift). "
                 f"A causal lean at T-45 (spot above its trailing 60 s TWAP -> UP) predicts the outcome {np.mean(acc):.1%} of the time (n {len(acc)}).\n")
    # ---- 8. walk-forward over days: choose (bid, placement) on the previous 5 days, trade the next
    R.append("## 8. Walk-forward (strict fills): parameters chosen on the previous 5 days, applied to the next day\n")
    R.append("| day | chosen bid | chosen placement | train P&L / market | OOS markets | OOS P&L |\n|---|---:|---:|---:|---:|---:|")
    cache = {}
    def pnl_by_day(bid, p):
        key = (bid, p)
        if key not in cache:
            recs = run(wins, bid, p, 0, "strict")
            bd = collections.defaultdict(list)
            for r in recs:
                bd[r["day"]].append(r["pnl"])
            cache[key] = bd
        return cache[key]
    oos_total = 0.0; oos_rows = []
    for i in range(5, len(days)):
        train, test = days[i - 5:i], days[i]
        best, best_v = None, -1e18
        for b in BIDS:
            for p in PLACEMENTS:
                bd = pnl_by_day(b, p)
                tr = [v for dday in train for v in bd.get(dday, [])]
                v = float(np.mean(tr)) if len(tr) >= 50 else -1e18
                if v > best_v:
                    best_v, best = v, (b, p)
        bd = pnl_by_day(*best)
        oos = bd.get(test, [])
        oos_total += sum(oos); oos_rows.append((test, best, best_v, len(oos), sum(oos)))
        R.append(f"| {time.strftime('%m-%d', time.gmtime(test * 86400))} | {best[0]:.2f} | T-{best[1]} | {best_v:+.4f} | {len(oos)} | {sum(oos):+.2f} |")
    R.append(f"\n**Walk-forward out-of-sample total: {oos_total:+.2f} USDC over {sum(r[3] for r in oos_rows)} markets.**\n")
    # ---- 9. capital
    m = grid[("strict", 0.47, 45)]
    R.append("## 9. Capital (bid 0.47, T-45, strict)\n")
    R.append(f"Max capital locked at once (resting bids + open positions until settlement + 2 min): {m['max_capital_locked']:.1f} USDC; max unmatched one-sided shares outstanding: {m['max_unmatched_shares']:.0f}. "
             f"Daily turnover at 10 shares: about {2 * Q * 0.47 * len(wins) / len(days):.0f} USDC of bids a day, {m['fills'] / len(days) * Q * 0.47:.0f} USDC filled a day. "
             f"A 100 USDC bankroll covers the strategy at this size; the bottleneck is the number of markets open at once, not the cash.\n")
    # ---- 10. drawdown and concentration
    R.append("## 10. Drawdown and concentration (bid 0.47, T-45, strict)\n")
    R.append(f"Max drawdown {m['max_drawdown']:.2f}; worst day {m['worst_day']:+.2f}, best day {m['best_day']:+.2f}, days positive {m['days_positive']:.0%}; longest losing streak {m['longest_losing_streak']} markets; "
             f"worst market {m['worst_market']:+.2f}, best market {m['best_market']:+.2f}; share of the total made by the best 2 markets: {'-' if m['top2_share'] is None else format(m['top2_share'], '.0%')}.\n")
    res["grid"] = {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in grid.items()}
    res["walk_forward"] = {"rows": [(time.strftime('%m-%d', time.gmtime(d * 86400)), b, p, v, n, s) for d, (b, p), v, n, s in oos_rows], "oos_total": oos_total}
    res["bench"] = bench
    doc = "\n".join(R)
    open(f"{ROOT}/docs/preopen_maker_2026-10-06.md", "w").write(doc)
    json.dump(res, open(f"{ROOT}/results/pom_replay_2026-10-06.json", "w"), indent=1, default=str)
    print(doc)
    print(f"\ndone in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
