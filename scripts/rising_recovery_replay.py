"""Replay of the RISING-TOKEN RECOVERY engine (strategy/rising_recovery.py, momentum mode) on the recorded BTC 5-minute
books (data/charts, cached in data/replay/btc5m_books.pkl by scripts/momentum_inventory_study.py).

The real engine class is driven pass by pass (one pass per recorded point, ~217 ms): pair_cancels, then pair_requests
with the books of that moment. Its orders reach the "venue" 250 ms later:
    IOC (taker)      filled at the ask then, if <= its limit, up to the ask's depth; the fee 0.07 p (1 - p) a share
    GTC post-only    refused if it would cross; otherwise filled in full at its price once the ask comes down to it
                     (a lower bound on maker fills); cancels take effect at once
Fills are reported as live.py does (the order ended first, then the fill). Each market settles on its gamma outcome.
P&L per market, chronological TRAIN / VALIDATION / TEST (50 / 25 / 25 % of markets).

    nice -n 19 .venv/bin/python scripts/rising_recovery_replay.py --set NAME KEY=VALUE ... [--set ...] [--workers 3]
"""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import pickle
import statistics as st
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from troll_poly_bot.features.engine import FeatureEngine            # noqa: E402
from troll_poly_bot.feeds.markets import Market                     # noqa: E402
from troll_poly_bot.risk.limits import RiskConfig, RiskManager      # noqa: E402
from troll_poly_bot.strategy.engine import EngineConfig             # noqa: E402
from troll_poly_bot.strategy.rising_recovery import RisingRecoveryConfig, RisingRecoveryEngine   # noqa: E402
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce  # noqa: E402

LAT_MS = 250.0
BOOKS = None
UP = None


def fee(p: float) -> float:
    return 0.07 * p * (1.0 - p)


def _ob(token, bid, ask, adep):
    bids = [] if bid != bid else [BookLevel(bid, 100.0)]
    asks = [] if ask != ask else [BookLevel(ask, adep if adep == adep and adep > 0 else 100.0)]
    return OrderBook(token, bids=bids, asks=asks)


def engine_for(overrides: dict) -> RisingRecoveryEngine:
    sc = RisingRecoveryConfig(enabled=True, mode="momentum", max_daily_loss_usdc=1e9)
    for k, v in overrides.items():
        cur = getattr(sc, k)
        setattr(sc, k, type(cur)(v) if not isinstance(cur, bool) else str(v).lower() in ("1", "true", "yes"))
    rc = RiskConfig(max_position_usdc=50.0, max_total_exposure_usdc=1e6, max_concurrent_positions=100,
                    max_asset_exposure_usdc=1e6, max_epoch_exposure_usdc=1e6, max_daily_loss_usdc=1e9, max_drawdown_usdc=1e9)
    risk = RiskManager(cfg=rc, starting_balance=1e6)
    e = RisingRecoveryEngine(EngineConfig(strategy="rising_recovery"), sc, risk, FeatureEngine())
    return e


def run_market(e: RisingRecoveryEngine, slug: str, b: dict, up: int) -> dict:
    yes, no = slug + ":UP", slug + ":DN"
    m = Market(condition_id=slug, asset="BTC", yes_token_id=yes, no_token_id=no, strike=b.get("strike") or 0.0,
               open_ts=b["open"], close_ts=b["close"], slug=slug, min_size=5.0)
    T, ua, ub, da, db, uad, dad = b["t"], b["ua"], b["ub"], b["da"], b["db"], b["uad"], b["dad"]
    pending: list[tuple[float, object, str]] = []        # (arrival, order, side)
    resting: dict[str, tuple[object, str]] = {}
    kinds: dict[str, int] = {}
    placed: dict[str, int] = {}
    legs: list[tuple] = []                                # (kind, side, price, shares, fee, seconds left)

    def book_fill(o, q, px, fe, now):
        e.on_pair_fill_record(slug, o, SimpleNamespace(size=q, price=px, fee=fe, token_id=o.token_id), now)
        k = e.orders.get(o.order_id, {}).get("kind", "?")
        kinds[k] = kinds.get(k, 0) + 1
        legs.append((k, "UP" if o.token_id == yes else "DOWN", px, q, fe, (b["close"] - now) / 1000.0))
    for i in range(len(T)):
        now = float(T[i])
        if now < b["open"] or now >= b["close"]:
            continue
        tops = {"UP": (ub[i], ua[i], uad[i]), "DOWN": (db[i], da[i], dad[i])}
        # orders that reach the venue now
        keep = []
        for arr, o, side in pending:
            if arr > now:
                keep.append((arr, o, side))
                continue
            bid, ask, adep = tops[side]
            if o.tif is TimeInForce.IOC:
                if ask == ask and ask <= o.price + 1e-9:
                    q = min(o.size, adep if adep == adep and adep > 0 else o.size)
                    q = math.floor(q * 100.0) / 100.0
                    e.on_pair_done(o.order_id, filled=q > 0)
                    if q > 0:
                        book_fill(o, q, ask, q * fee(ask), now)
                else:
                    e.on_pair_done(o.order_id, filled=False)
            else:
                if ask == ask and o.price >= ask - 1e-9:
                    e.on_pair_done(o.order_id, filled=False)          # post-only would cross: refused
                else:
                    resting[o.order_id] = (o, side)
        pending = keep
        # resting bids the ask came down to
        for oid, (o, side) in list(resting.items()):
            ask = tops[side][1]
            if ask == ask and ask <= o.price + 1e-9:
                resting.pop(oid)
                e.on_pair_done(oid, filled=True)
                book_fill(o, o.size, o.price, 0.0, now)
        # the engine's pass
        for oid in e.pair_cancels(now) or ():
            if oid in resting:
                resting.pop(oid)
                e.on_pair_done(oid, filled=False)
            else:
                pending = [x for x in pending if x[1].order_id != oid]
                e.on_pair_done(oid, filled=False)
        books = {slug: (_ob(yes, ub[i], ua[i], uad[i]), _ob(no, db[i], da[i], dad[i]))}
        for _, side, o in e.pair_requests(now, {slug: m}, books, balance=1e6) or ():
            e.on_pair_submit(o.order_id, slug, side, o.price, o.size)
            pending.append((now + LAT_MS, o, side))
            k = e.orders.get(o.order_id, {}).get("kind", "?")
            placed[k] = placed.get(k, 0) + 1
    for oid in list(resting):
        e.on_pair_done(oid, filled=False)
    mb = e.books.get(slug)
    pnl = 0.0 if mb is None else ((mb.U if up == 1 else mb.D) - mb.C)
    if mb is not None:
        e.on_market_settled(slug, up == 1, pnl, b["close"] + 1.0)
    e._cleanup(b["close"] + 700_000.0)                                 # noqa: SLF001
    return {"slug": slug, "open": b["open"], "pnl": pnl, "cost": mb.C if mb else 0.0, "U": mb.U if mb else 0.0,
            "D": mb.D if mb else 0.0, "fills": kinds, "placed": placed, "done": mb.why_done if mb else "",
            "legs": [(k, s, px, q, fe, left, float((up == 1) == (s == "UP"))) for k, s, px, q, fe, left in legs]}


def _init(books_path, up_path):
    global BOOKS, UP
    BOOKS = pickle.load(open(books_path, "rb"))
    UP = json.load(open(up_path))


def _chunk(args):
    overrides, slugs = args
    e = engine_for(overrides)
    return [run_market(e, s, BOOKS[s], UP[s]) for s in slugs]


def summarise(rows: list[dict], cuts) -> dict:
    out = {}
    for name, sel in (("train", lambda r: r["open"] < cuts[0]), ("val", lambda r: cuts[0] <= r["open"] < cuts[1]),
                      ("test", lambda r: r["open"] >= cuts[1]), ("all", lambda r: True)):
        rs = [r for r in rows if sel(r)]
        traded = [r for r in rs if r["cost"] > 0]
        p = [r["pnl"] for r in rs]
        out[name] = {"markets": len(rs), "traded": len(traded), "pnl": round(sum(p), 2),
                     "per_market": round(st.mean(p), 4) if p else None,
                     "se": round(st.pstdev(p) / math.sqrt(len(p)), 4) if len(p) > 1 else None,
                     "t": round(st.mean(p) / (st.pstdev(p) / math.sqrt(len(p))), 2) if len(p) > 1 and st.pstdev(p) > 0 else None,
                     "win_markets": sum(r["pnl"] > 0 for r in traded), "spend": round(sum(r["cost"] for r in rs), 1),
                     "worst": round(min(p), 2) if p else None}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", nargs="+", action="append", default=[], metavar=("NAME", "KEY=VALUE"))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="only the first N markets (a quick check)")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "rising_recovery_replay.jsonl")
    ap.add_argument("--books", type=Path, default=ROOT / "data/replay/btc5m_books.pkl",
                    help="the book cache (data/replay/btc5m_books_fwd.pkl = the markets recorded after 10-09 13:40)")
    a = ap.parse_args()
    books_path, up_path = a.books, ROOT / "data/replay/btc5m_outcomes.json"
    _init(books_path, up_path)
    slugs = sorted((s for s in BOOKS if s in UP), key=lambda s: BOOKS[s]["open"])
    if a.limit:
        slugs = slugs[:a.limit]
    opens = [BOOKS[s]["open"] for s in slugs]
    cuts = (opens[len(opens) // 2], opens[(3 * len(opens)) // 4])
    sets = a.set or [["defaults"]]
    n = a.workers
    chunks = [slugs[i * len(slugs) // n:(i + 1) * len(slugs) // n] for i in range(n)]
    ctx = mp.get_context("fork")
    with ctx.Pool(n) as pool:
        for spec in sets:
            name, kv = spec[0], dict(x.split("=", 1) for x in spec[1:])
            t0 = time.time()
            rows = [r for part in pool.map(_chunk, [(kv, c) for c in chunks]) for r in part]
            s = summarise(rows, cuts)
            fills, placed = {}, {}
            for r in rows:
                for k, v in r["fills"].items():
                    fills[k] = fills.get(k, 0) + v
                for k, v in r["placed"].items():
                    placed[k] = placed.get(k, 0) + v
            by = {}
            for r in rows:
                for k, sd, px, q, fe, left, won in r["legs"]:
                    for key in (k, f"{k} px {min(int(px * 10), 9) / 10:.1f}", f"{k} left {'>200' if left > 200 else '100-200' if left > 100 else '<100'}"):
                        x = by.setdefault(key, [0.0, 0.0, 0])
                        x[0] += q; x[1] += q * won - q * px - fe; x[2] += 1
            attribution = {k: {"shares": round(v[0], 1), "pnl": round(v[1], 2), "c_per_share": round(100 * v[1] / v[0], 2), "fills": v[2]}
                           for k, v in sorted(by.items()) if v[0] > 0}
            rec = {"name": name, "overrides": kv, "summary": s, "fills": fills, "placed": placed,
                   "attribution": attribution, "seconds": round(time.time() - t0)}
            with a.out.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"{name:28s} " + " | ".join(f"{k} {v['per_market']:+.3f}/mkt t {v['t']} ({v['traded']} traded, total {v['pnl']:+.0f})"
                                              for k, v in s.items() if v["per_market"] is not None)
                  + f" | fills {fills} placed {placed} [{rec['seconds']} s]", flush=True)


if __name__ == "__main__":
    main()
