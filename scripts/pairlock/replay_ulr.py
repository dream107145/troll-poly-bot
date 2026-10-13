"""Replay of the UNFILLED-LEG RECOVERY (strategy/pairlock_ulr.py) on the recorded BTC 5-minute windows (data/charts).

Two arms on the SAME windows, the SAME first legs and the SAME pair bids:

    baseline   the pair-lock BTC conditions rule as configured (buy 20 of the higher token when its ask is 0.50-0.60,
               >= 120 s left, 0 < BTC - strike < 5; then a resting bid for the other token at pair_sum - fill, held
               until the close; a new buy once every held token is paired), NO recovery, no late buy
    ulr        the same, plus the unfilled-leg recovery with the conservative initial parameters

Execution mirrors the paper engine's calibrated scenario (scripts/pairlock/replay.py): a fill-and-kill buy arrives
143 ms after the decision, takes at most 20 % of the touch depth (the measured contention), must still be fillable at
or under its limit 1 s later and is charged the worse of the two asks; the result is known 112 ms after that. A
resting bid fills only when the other side's best ask trades STRICTLY through its price (PaperExchange._match_resting),
for the whole size, at the bid, with no maker fee. Sells mirror buys on the bid side. Taker fee 7 % x p(1 - p).

    python scripts/pairlock/replay_ulr.py [--days N] [--out results/pairlock_ulr_replay_summary.json]
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, "/root/troll-poly-bot/src")
from troll_poly_bot.strategy import pairlock_book as pb              # noqa: E402
from troll_poly_bot.strategy import pairlock_recovery as rc          # noqa: E402
from troll_poly_bot.strategy import pairlock_ulr as ulr              # noqa: E402
from troll_poly_bot.strategy.pairlock import MarketView              # noqa: E402

ROOT = "/root/troll-poly-bot"
LAT_MS, ACK_MS, SURVIVAL_MS, CONTENTION = 143.0, 112.0, 1000.0, 0.80
SPLITS = (("train", 0.0, 0.5), ("validation", 0.5, 0.7), ("test", 0.7, 0.85), ("forward", 0.85, 1.0))
BANKROLL = 100.0

# the bot's configuration (ecosystem.pairlock.config.js, CONDITIONS + the ULR block); the dashboard overrides are NOT applied
PRM = rc.RecoveryParams(entry_mode="conditions", open_shares=20.0, open_limit_offset=0.02, open_max_tries=3, cond_min_price=0.50,
                        cond_max_price=0.60, cond_min_left_s=120.0, cond_spot_lo=0.0, cond_spot_hi=5.0, cond_max_entries=10,
                        pair_sum=0.97, pair_pull=False, recovery_enabled=False, late_enabled=False, ulr_enabled=True)
MIN_SIZE = 5.0


class Win:
    __slots__ = ("slug", "open", "close", "strike", "outcome", "t", "ub", "ua", "db", "da", "ubd", "uad", "dbd", "dad", "spot", "day")

    def __init__(self, d: dict) -> None:
        self.slug, self.open, self.close = d["slug"], float(d["open_ts"]), float(d["close_ts"])
        self.strike, self.outcome = float(d["strike"]), d["outcome"].upper()
        self.day = time.strftime("%Y-%m-%d", time.gmtime(self.open / 1000.0))
        pts = [p for p in d["points"] if p.get("t") is not None and self.open <= p["t"] < self.close]
        self.t = [float(p["t"]) for p in pts]
        g = lambda k: [p.get(k) for p in pts]   # noqa: E731
        self.ub, self.ua, self.db, self.da, self.spot = g("ub"), g("ua"), g("db"), g("da"), g("spot")
        self.ubd, self.uad = [float(x or 0.0) for x in g("ubd")], [float(x or 0.0) for x in g("uad")]
        # the DOWN book's own depth where recorded; the books mirror otherwise (DOWN ask depth = UP bid depth)
        self.dbd = [float(x) if x is not None else None for x in g("dbd")]
        self.dad = [float(x) if x is not None else None for x in g("dad")]

    def idx(self, t: float) -> int:
        return bisect.bisect_left(self.t, t)

    def book(self, side: str, i: int):
        """(ask, bid, ask depth, bid depth) of ``side`` at sample i."""
        if side == "UP":
            return self.ua[i], self.ub[i], self.uad[i], self.ubd[i]
        da, db = self.da[i], self.db[i]
        dad = self.dad[i] if self.dad[i] is not None else self.ubd[i]
        dbd = self.dbd[i] if self.dbd[i] is not None else self.uad[i]
        return da, db, dad, dbd

    def view(self, i: int) -> MarketView:
        ua, ub, uad, ubd = self.book("UP", i)
        da, db, dad, dbd = self.book("DOWN", i)
        lv = lambda px, d: [] if px is None else [(round(min(px + k * 0.01, 0.99), 2), d / 3.0) for k in range(3)]   # noqa: E731
        lb = lambda px, d: [] if px is None else [(round(max(px - k * 0.01, 0.01), 2), d / 3.0) for k in range(3)]   # noqa: E731
        return MarketView(bid_up=ub, ask_up=ua, bid_down=db, ask_down=da, asks_up=lv(ua, uad), asks_down=lv(da, dad),
                          bids_up=lb(ub, ubd), bids_down=lb(db, dbd), spot_age_ms=200.0)

    def hist_mid(self, side: str, i: int, window_ms: float):
        """mid at sample i minus the mid window_ms earlier (None if the window reaches before the open)."""
        j = self.idx(self.t[i] - window_ms)
        if j >= i or self.t[j] > self.t[i] - 0.5 * window_ms:
            return None
        a, b = (self.ua, self.ub) if side == "UP" else (self.da, self.db)
        if None in (a[i], b[i], a[j], b[j]):
            return None
        return 0.5 * (a[i] + b[i]) - 0.5 * (a[j] + b[j])

    def spot_d(self, i: int, window_ms: float):
        j = self.idx(self.t[i] - window_ms)
        if j >= i or self.t[j] > self.t[i] - 0.5 * window_ms or self.spot[i] is None or self.spot[j] is None:
            return None
        return float(self.spot[i]) - float(self.spot[j])


def load_windows(days: int | None):
    fs = sorted(f for f in os.listdir(f"{ROOT}/data/charts") if f.startswith("btc-updown-5m-") and f.endswith(".json"))
    if days:
        cutoff = time.time() - days * 86400.0
        fs = [f for f in fs if int(f.rsplit("-", 1)[1].split(".")[0]) >= cutoff]
    for f in fs:
        try:
            d = json.load(open(f"{ROOT}/data/charts/{f}"))
        except (OSError, ValueError):
            continue
        if not d.get("points") or (d.get("outcome") or "").upper() not in ("UP", "DOWN") or not d.get("strike"):
            continue
        w = Win(d)
        if len(w.t) > 600:
            yield w


# ───────────────────────────── execution ─────────────────────────────


def fak(w: Win, i_dec: int, side: str, q: float, limit: float, sell: bool = False):
    """A fill-and-kill order decided at sample i_dec: (filled qty, price, sample the result is known at)."""
    t_arr = w.t[i_dec] + LAT_MS
    j = w.idx(t_arr)
    if j >= len(w.t):
        return 0.0, None, len(w.t) - 1
    ask, bid, dask, dbid = w.book(side, j)
    px, depth = (bid, dbid) if sell else (ask, dask)
    ok = px is not None and ((px >= limit - 1e-9) if sell else (px <= limit + 1e-9))
    avail = depth * (1.0 - CONTENTION)
    if not ok or avail + 1e-9 < MIN_SIZE and avail < q:
        return 0.0, None, min(j + 1, len(w.t) - 1)
    k = w.idx(t_arr + SURVIVAL_MS)
    if k >= len(w.t):
        return 0.0, None, len(w.t) - 1
    ask2, bid2, dask2, dbid2 = w.book(side, k)
    px2, depth2 = (bid2, dbid2) if sell else (ask2, dask2)
    ok2 = px2 is not None and ((px2 >= limit - 1e-9) if sell else (px2 <= limit + 1e-9))
    if not ok2:
        return 0.0, None, min(w.idx(t_arr + SURVIVAL_MS + ACK_MS), len(w.t) - 1)
    qty = math.floor(min(q, avail, depth2) * 100.0) / 100.0
    if qty <= 0.0:
        return 0.0, None, min(k + 1, len(w.t) - 1)
    price = min(px, px2) if sell else max(px, px2)
    return qty, price, min(w.idx(t_arr + SURVIVAL_MS + ACK_MS), len(w.t) - 1)


def tok(side: str) -> str:
    return "UPTOKEN" if side == "UP" else "DOWNTOKEN"


# ───────────────────────────── one window ─────────────────────────────


def run_window(w: Win, prm: rc.RecoveryParams, use_ulr: bool, counts: Counter) -> dict:
    book = pb.PairBook(w.slug)
    cyc = rc.RecoveryCycle(book=book, a_side="UP", started_t=w.t[0], target=0.0, open_entry=True)
    pair = None                     # {side, price, size, filled, i_live}
    u: ulr.ULR | None = None
    tries, entries, oid = 0, 0, 0
    i, n = 0, len(w.t)
    win_ms = prm.ulr_momentum_window_s * 1000.0
    positions = []                  # one record per pair bid (an unhedged-or-hedged first leg)
    cash_free = BANKROLL
    open_ulr_exposure = 0.0
    while i < n:
        t = w.t[i]
        left = (w.close - t) / 1000.0
        mv = w.view(i)
        # the resting pair bid: fills when the other side's ask trades strictly through it
        if pair is not None and pair["filled"] + 1e-9 < pair["size"] and i >= pair["i_live"]:
            opp_ask = mv.ask(pair["side"])
            if opp_ask is not None and opp_ask < pair["price"] - 1e-9:
                q = pair["size"] - pair["filled"]
                oid += 1
                book.add_fill(pb.FillRecord(w.slug, t, tok(pair["side"]), pair["side"], q, pair["price"], 0.0, f"pair-{oid}",
                                            f"pair-{oid}-1", "BUY", pair["price"], "pair"))
                pair["filled"] += q
                cash_free -= q * pair["price"]
                if u is not None:
                    ulr.hedge_filled(u, q, t)
                counts["pair_fills"] += 1
        held = book.up_qty + book.down_qty > 1e-9
        paired = book.up_qty > 1e-9 and abs(book.up_qty - book.down_qty) <= 1e-6 and pair is not None and pair["filled"] + 1e-9 >= pair["size"]
        # the first leg (the conditions rule), or an add-on once everything held is paired
        if (not held or paired) and entries < prm.cond_max_entries and tries < prm.open_max_tries * (entries + 1):
            side, why = rc.conditions_entry_side(mv, prm, left, w.spot[i], w.strike, 0.0 if paired else (book.up_qty + book.down_qty))
            if side is not None:
                tries += 1
                ask = mv.ask(side)
                limit = min(0.99, round(ask + prm.open_limit_offset, 2))
                qf, px, k = fak(w, i, side, prm.open_shares, limit)
                counts["entry_orders"] += 1
                if qf > 0.0:
                    oid += 1
                    fee = qf * pb.fee_per_share(px)
                    book.add_fill(pb.FillRecord(w.slug, w.t[k], tok(side), side, qf, px, fee, f"entry-{oid}", f"entry-{oid}-1", "BUY", ask, "entry"))
                    cash_free -= qf * px + fee
                    entries += 1
                    counts["entry_fills"] += 1
                    cyc.a_side = side
                    bid_px = rc.pair_bid_price(prm, px)
                    other = rc.OTHER[side]
                    if u is not None and u.state not in (ulr.HEDGED, ulr.RECOVERY_CLOSED):
                        positions.append(("abandoned", u))
                    u = ulr.start(f"{w.slug}:{side}:{entries}", side, qf)
                    if bid_px is None or qf < MIN_SIZE:
                        pair = {"side": other, "price": 0.0, "size": qf, "filled": 0.0, "i_live": n}
                        ulr.hedge_skipped(u, "price" if bid_px is None else "size", w.t[k])
                    else:
                        pair = {"side": other, "price": bid_px, "size": qf, "filled": 0.0, "i_live": w.idx(w.t[k] + LAT_MS)}
                        ulr.hedge_placed(u, bid_px, qf, w.t[k])
                    counts["pair_bids"] += 1
                i = k + 1
                continue
        # the unfilled-leg recovery
        if use_ulr and u is not None:
            if u.state in (ulr.HEDGE_PENDING, ulr.UNHEDGED_RECOVERY):
                mo, mg = w.hist_mid(u.recovery_leg, i, win_ms), w.hist_mid(u.original_leg, i, win_ms)
                dec, rec = ulr.evaluate_entry(u, book, mv, t, left, prm, mo, mg, w.spot_d(i, win_ms), 200.0, 1000.0,
                                              max(0.0, cash_free), open_ulr_exposure, MIN_SIZE)
                counts["ulr_" + rec["code"]] += 1
                if dec is not None:
                    oid += 1
                    ulr.entry_sent(u, f"ulr-{oid}", dec["q"], t)
                    qf, px, k = fak(w, i, dec["side"], dec["q"], dec["limit"])
                    counts["ulr_entry_orders"] += 1
                    if qf > 0.0:
                        fee = qf * pb.fee_per_share(px)
                        book.add_fill(pb.FillRecord(w.slug, w.t[k], tok(dec["side"]), dec["side"], qf, px, fee, f"ulr-{oid}", f"ulr-{oid}-1",
                                                    "BUY", dec["vwap"], "ulr"))
                        ulr.entry_filled(u, qf, px, fee, w.t[k])
                        cash_free -= qf * px + fee
                        open_ulr_exposure += qf * px
                        counts["ulr_entry_fills"] += 1
                    ulr.entry_done(u, w.t[k], "killed" if qf <= 0.0 else "")
                    i = k + 1
                    continue
            elif u.state == ulr.RECOVERY_OPEN:
                mo, mg = w.hist_mid(u.recovery_leg, i, win_ms), w.hist_mid(u.original_leg, i, win_ms)
                dec, rec = ulr.evaluate_exit(u, mv, t, left, prm, mo, mg, 10.0)
                if dec is not None:
                    oid += 1
                    ulr.exit_sent(u, f"ulrx-{oid}", dec["kind"], dec["q"], t)
                    qf, px, k = fak(w, i, dec["side"], dec["q"], dec["limit"], sell=True)
                    counts["ulr_exit_orders"] += 1
                    if qf > 0.0:
                        fee = qf * pb.fee_per_share(px)
                        book.add_fill(ulr.exit_fill_record(u, w.slug, tok(dec["side"]), f"ulrx-{oid}", 1, qf, px, fee, w.t[k]))
                        ulr.exit_filled(u, qf, px, fee, w.t[k])
                        cash_free += qf * px - fee
                        open_ulr_exposure = max(0.0, open_ulr_exposure - qf * (u.entry_avg or px))
                        counts["ulr_exit_fills"] += 1
                        counts["ulr_exit_" + dec["kind"]] += 1
                    ulr.exit_done(u, w.t[k], qf > 0.0, "killed" if qf <= 0.0 else "")
                    i = k + 1
                    continue
        i += 1
    pnl = book.resolution_pnl(w.outcome) if book.fills else 0.0
    out = {"slug": w.slug, "day": w.day, "outcome": w.outcome, "pnl": pnl, "entries": entries, "fills": len(book.fills),
           "up_qty": book.up_qty, "down_qty": book.down_qty, "positions": []}
    if u is not None and u.state not in (ulr.HEDGED, ulr.RECOVERY_CLOSED) or (u is not None and u.entries):
        positions.append(("last", u))
    elif u is not None:
        positions.append(("last", u))
    for _, pu in positions:
        fin = ulr.settle(pu, book, w.outcome, w.close)
        out["positions"].append({k: fin[k] for k in ("position_id", "hedged", "hedged_late", "recovered", "original_pnl", "recovery_pnl",
                                                    "combined_pnl", "improvement", "exit_kind", "attempts", "entries",
                                                    "recovery_held_at_close", "recovery_fees")})
    return out


# ───────────────────────────── the comparison ─────────────────────────────


def summarize(base: list, treat: list, counts_b: Counter, counts_u: Counter) -> dict:
    b_pnl = sum(r["pnl"] for r in base)
    u_pnl = sum(r["pnl"] for r in treat)
    pos_u = [p for r in treat for p in r["positions"]]
    pos_b = [p for r in base for p in r["positions"]]
    unhedged_b = [p for p in pos_b if p["hedged"] + 1e-6 < 20.0]
    unhedged_u = [p for p in pos_u if p["hedged"] + 1e-6 < 20.0 or p["recovered"]]
    rec = [p for p in pos_u if p["recovered"]]
    imp = [p["improvement"] for p in rec]
    traded = [r for r in base if r["fills"]]
    out = {"windows": len(base), "windows_traded": len(traded), "first_legs": counts_b["entry_fills"], "pair_bids": counts_b["pair_bids"],
           "pair_fills": counts_b["pair_fills"], "unhedged_positions": len(unhedged_b),
           "baseline_pnl": round(b_pnl, 2), "baseline_pnl_unhedged": round(sum(p["original_pnl"] for p in unhedged_b), 2),
           "ulr_arm_pnl": round(u_pnl, 2), "improvement_total": round(u_pnl - b_pnl, 2),
           "recovery_entries_sent": counts_u["ulr_entry_orders"], "recovery_fills": counts_u["ulr_entry_fills"],
           "recovered_positions": len(rec), "recovery_pnl": round(sum(imp), 2),
           "avg_recovery_pnl": round(statistics.fmean(imp), 4) if imp else None,
           "avg_loss_without_recovery": round(statistics.fmean(p["original_pnl"] for p in rec), 4) if rec else None,
           "avg_loss_with_recovery": round(statistics.fmean(p["combined_pnl"] for p in rec), 4) if rec else None,
           "worst_recovery_loss": round(min(imp), 4) if imp else None, "best_recovery": round(max(imp), 4) if imp else None,
           "recovery_wins": sum(1 for x in imp if x > 0), "recovery_losses": sum(1 for x in imp if x < 0),
           "tp": counts_u["ulr_exit_" + ulr.R_TAKE_PROFIT], "sl": counts_u["ulr_exit_" + ulr.R_STOP_LOSS],
           "signal_exits": counts_u["ulr_exit_" + ulr.R_SIGNAL_REVERSAL], "emergency_exits": counts_u["ulr_exit_" + ulr.R_EMERGENCY_EXIT],
           "held_to_resolution": sum(1 for p in rec if p["recovery_held_at_close"] > 1e-6),
           "exit_orders": counts_u["ulr_exit_orders"], "exit_fills": counts_u["ulr_exit_fills"],
           "recovery_fees": round(sum(p["recovery_fees"] for p in rec), 2),
           "reasons": {k[4:]: v for k, v in counts_u.items() if k.startswith("ulr_RECOVERY")}}
    if len(imp) > 1:
        sd = statistics.stdev(imp)
        out["t_stat"] = round(statistics.fmean(imp) / (sd / math.sqrt(len(imp))), 2) if sd > 0 else None
    # the recovered positions by how the first leg ended: hedged after all (the pair bid filled later: a whipsaw) or not
    for label, grp in (("recovered_hedged_late", [p for p in rec if p["hedged"] + 1e-6 >= 20.0 or p["hedged_late"]]),
                       ("recovered_unhedged", [p for p in rec if not (p["hedged"] + 1e-6 >= 20.0 or p["hedged_late"])])):
        g = [p["improvement"] for p in grp]
        out[label] = {"n": len(grp), "recovery_pnl": round(sum(g), 2), "avg": round(statistics.fmean(g), 4) if g else None,
                      "avg_original_pnl": round(statistics.fmean(p["original_pnl"] for p in grp), 4) if grp else None,
                      "avg_combined_pnl": round(statistics.fmean(p["combined_pnl"] for p in grp), 4) if grp else None,
                      "wins": sum(1 for x in g if x > 0), "losses": sum(1 for x in g if x < 0)}
    # the unhedged positions WITHOUT a recovery (the rule never fired): their loss stays
    no_rec = [p for p in pos_u if not p["recovered"] and p["hedged"] + 1e-6 < 20.0]
    out["unhedged_without_recovery"] = {"n": len(no_rec), "pnl": round(sum(p["original_pnl"] for p in no_rec), 2),
                                        "avg": round(statistics.fmean(p["original_pnl"] for p in no_rec), 4) if no_rec else None}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--out", default=f"{ROOT}/results/pairlock_ulr_replay_summary.json")
    ap.add_argument("--set", action="append", default=[], help="override a RecoveryParams field, e.g. ulr_signal_exit=0")
    args = ap.parse_args()
    global PRM
    for kv in args.set:
        k, v = kv.split("=", 1)
        cur = getattr(PRM, k)
        val = (v.lower() in ("1", "true", "yes")) if isinstance(cur, bool) else type(cur)(v)
        PRM = rc.RecoveryParams(**{**vars(PRM), k: val})
    t0 = time.time()
    base, treat, cb, cu = [], [], Counter(), Counter()
    for w in load_windows(args.days or None):
        base.append(run_window(w, PRM, False, cb))
        treat.append(run_window(w, PRM, True, cu))
    n = len(base)
    by_split = {}
    for name, lo, hi in SPLITS:
        a, b = int(n * lo), int(n * hi)
        by_split[name] = summarize(base[a:b], treat[a:b], Counter(), cu_split(treat[a:b]))
    res = {"generated": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "windows": n, "days": sorted({r["day"] for r in base}),
           "overrides": args.set,
           "params": {k: v for k, v in vars(PRM).items() if k.startswith(("ulr_", "cond_", "open_", "pair_"))},
           "execution": {"lat_ms": LAT_MS, "ack_ms": ACK_MS, "survival_ms": SURVIVAL_MS, "contention": CONTENTION,
                         "maker_fill": "the other side's ask strictly through the bid, whole size, no fee", "taker_fee": "7% x p(1-p)"},
           "all": summarize(base, treat, cb, cu), "splits": by_split,
           "per_day": per_day(base, treat), "runtime_s": round(time.time() - t0, 1)}
    recs = [p for r in treat for p in r["positions"] if p["recovered"]]
    res["recovered_positions"] = recs[-200:]
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(res, open(args.out, "w"), indent=1, default=str)
    a = res["all"]
    print(f"windows {n}  traded {a['windows_traded']}  first legs {a['first_legs']}  pair bids {a['pair_bids']}  pair fills {a['pair_fills']}"
          f"  unhedged {a['unhedged_positions']}")
    print(f"baseline P&L {a['baseline_pnl']:+.2f} (unhedged legs {a['baseline_pnl_unhedged']:+.2f})  ulr arm {a['ulr_arm_pnl']:+.2f}  "
          f"improvement {a['improvement_total']:+.2f}")
    print(f"recovery: sent {a['recovery_entries_sent']} filled {a['recovery_fills']} positions {a['recovered_positions']}  P&L {a['recovery_pnl']:+.2f}"
          f"  avg {a['avg_recovery_pnl']}  t {a.get('t_stat')}  wins/losses {a['recovery_wins']}/{a['recovery_losses']}  worst {a['worst_recovery_loss']}")
    print(f"exits: TP {a['tp']} SL {a['sl']} signal {a['signal_exits']} emergency {a['emergency_exits']} held {a['held_to_resolution']}"
          f"  (sell orders {a['exit_orders']}, filled {a['exit_fills']})")
    print("reasons:", dict(sorted(a["reasons"].items(), key=lambda kv: -kv[1])))
    for name, s in by_split.items():
        print(f"  {name:10s} windows {s['windows']:4d} unhedged {s['unhedged_positions']:3d} base {s['baseline_pnl']:+8.2f} ulr {s['ulr_arm_pnl']:+8.2f} "
              f"recovered {s['recovered_positions']:3d} rec P&L {s['recovery_pnl']:+7.2f} TP {s['tp']} SL {s['sl']} sig {s['signal_exits']} "
              f"emg {s['emergency_exits']} held {s['held_to_resolution']}")
    print(f"written {args.out} in {res['runtime_s']} s")


def cu_split(rows: list) -> Counter:
    c = Counter()
    for r in rows:
        for p in r["positions"]:
            if p["recovered"]:
                c["ulr_entry_fills"] += 1
                if p["exit_kind"] in ulr.EXIT_KINDS and p["recovery_held_at_close"] <= 1e-6:
                    c["ulr_exit_" + p["exit_kind"]] += 1
    return c


def per_day(base: list, treat: list) -> dict:
    out = defaultdict(lambda: {"windows": 0, "baseline": 0.0, "ulr": 0.0, "recovered": 0})
    for b, u in zip(base, treat):
        d = out[b["day"]]
        d["windows"] += 1
        d["baseline"] += b["pnl"]
        d["ulr"] += u["pnl"]
        d["recovered"] += sum(1 for p in u["positions"] if p["recovered"])
    return {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in sorted(out.items())}


if __name__ == "__main__":
    main()
