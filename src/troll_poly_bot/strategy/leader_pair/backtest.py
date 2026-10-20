"""Event-driven backtester for LEADER_PAIR over the recorded BTC 5-minute books (data/replay/btc5m_books.pkl, the
outcomes in data/replay/btc5m_outcomes.json; built by scripts/momentum_inventory_study.py from data/charts).

The REAL engine (engine.py -> core.py) is driven point by point, exactly as live.py drives it: ``pair_cancels``,
then ``pair_requests`` with the books of that recorded moment and nothing later. An order reaches the venue
``latency_ms`` later and is matched against the book AS RECORDED at the first point after its arrival:

    IOC buy   filled at the ask then if ask <= limit, up to depth_haircut x the displayed ask depth (a partial fill
              when the book is thinner than the order), the venue's taker fee rate x p (1 - p) a share plus
              ``extra_slippage`` a share (sensitivity); otherwise it dies unfilled (and the engine is told so)
    results   reported as live.py does: the order's end first, then its fills

Data limits (stated, not hidden): the cache holds the best bid / ask of both tokens, the DEPTH AT THE BEST ASK and
the BTC spot about every 200 ms; no bid depth (the liquidation estimate values a position at the best bid, fees
and ``liq_slippage`` off), no deeper ask levels (an order larger than the touch fills only the touch), no trade
prints. Markets whose outcome is missing are skipped. Nothing here reads a later point than the one being processed.
"""
from __future__ import annotations

import json
import math
import pickle
import statistics as st
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from ...features.engine import FeatureEngine
from ...feeds.markets import Market, MarketMeta
from ...risk.limits import RiskConfig, RiskManager
from ...signals.costs import FeeSchedule
from ...types import BookLevel, OrderBook, TimeInForce
from ..engine import EngineConfig
from .config import LeaderPairConfig
from .core import State
from .engine import LeaderPairEngine


@dataclass(slots=True)
class BacktestConfig:
    latency_ms: float = 250.0
    extra_slippage: float = 0.0          # USDC a share added to every fill (sensitivity)
    depth_haircut: float = 1.0           # the fraction of the displayed ask depth an order may take
    fee_rate: float = 0.07
    stride: int = 1                      # use every k-th recorded point (1 = every point)
    balance: float = 1e6


@dataclass
class MarketResult:
    slug: str
    open: float
    up_won: bool
    pnl: float = 0.0
    gross_pnl: float = 0.0               # before fees
    cost: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0                # fill price - the ask seen at decision, x size
    cash_spent: float = 0.0
    q_up: float = 0.0
    q_down: float = 0.0
    purchases: int = 0
    orders: int = 0
    unfilled: int = 0
    partial: int = 0
    maker_fills: int = 0
    pairs_formed: float = 0.0
    pairs_qualifying: float = 0.0
    avg_pair_cost: float | None = None
    pair_margin: float = 0.0
    unmatched_up: float = 0.0
    unmatched_down: float = 0.0
    pnl_if_up: float = 0.0
    pnl_if_down: float = 0.0
    state: str = ""
    cycles: int = 0
    recovery_stops: int = 0
    falling_events: int = 0
    leader: str | None = None
    p0: float | None = None
    refusals: dict = field(default_factory=dict)
    fills: list = field(default_factory=list)       # (kind, side, price, shares, fee, seconds_left, won)
    baselines: dict = field(default_factory=dict)


def _ob(token: str, bid: float, ask: float, adep: float, haircut: float) -> OrderBook:
    bids = [] if bid != bid else [BookLevel(float(bid), 100.0)]
    size = adep * haircut if adep == adep and adep > 0 else 100.0 * haircut
    asks = [] if ask != ask else [BookLevel(float(ask), float(size))]
    return OrderBook(token, bids=bids, asks=asks)


def engine_for(scfg: LeaderPairConfig | None = None, **overrides) -> LeaderPairEngine:
    sc = scfg or LeaderPairConfig(enabled=True)
    sc.enabled = True
    for k, v in overrides.items():
        cur = getattr(sc, k)
        setattr(sc, k, (str(v).lower() in ("1", "true", "yes")) if isinstance(cur, bool) else type(cur)(v))
    rc = RiskConfig(max_position_usdc=1e6, max_total_exposure_usdc=1e6, max_concurrent_positions=1000,
                    max_asset_exposure_usdc=1e6, max_epoch_exposure_usdc=1e6, max_daily_loss_usdc=1e9, max_drawdown_usdc=1e9)
    return LeaderPairEngine(EngineConfig(strategy="leader_pair"), sc, RiskManager(cfg=rc, starting_balance=1e6), FeatureEngine())


def run_market(e: LeaderPairEngine, slug: str, b: dict, up_won: bool, bcfg: BacktestConfig = BacktestConfig()) -> MarketResult:
    """Replay one recorded market through the engine. ``b`` is one entry of the book cache."""
    yes, no = slug + ":UP", slug + ":DN"
    m = Market(condition_id=slug, asset="BTC", yes_token_id=yes, no_token_id=no, strike=float(b.get("strike") or 0.0),
               open_ts=float(b["open"]), close_ts=float(b["close"]), slug=slug, min_size=5.0)
    rate = float(b.get("fee_rate") or bcfg.fee_rate)
    sched = FeeSchedule(rate=rate)
    e.meta_by_slug[slug] = MarketMeta(market=m, resolution_source="", accepting_orders=True, duration_s=300.0, fee=sched)
    T, ua, ub, da, db, uad, dad = b["t"], b["ua"], b["ub"], b["da"], b["db"], b["uad"], b["dad"]
    res = MarketResult(slug=slug, open=float(b["open"]), up_won=bool(up_won))
    pending: list[tuple[float, object, str]] = []
    resting: dict[str, tuple[object, str]] = {}            # maker parts on the book: filled in full once the ask comes down
    expected: dict[str, float] = {}

    def fee(p: float) -> float:
        return sched.taker_fee_per_share(p)

    for i in range(0, len(T), max(1, int(bcfg.stride))):
        now = float(T[i])
        if now < b["open"]:
            continue
        if now >= b["close"]:
            break
        tops = {"UP": (ub[i], ua[i], uad[i]), "DOWN": (db[i], da[i], dad[i])}
        keep = []
        for arr, o, side in pending:
            if arr > now:
                keep.append((arr, o, side))
                continue
            bid, ask, adep = tops[side]
            if o.tif is TimeInForce.GTC:
                # a post-only maker part: refused if it would cross on arrival, else it rests (a lower bound on maker
                # fills: filled in full, fee-free, at its price once the recorded ask is at or under it)
                if ask == ask and ask <= o.price + 1e-9:
                    e.on_pair_done(o.order_id, filled=False)
                    res.unfilled += 1
                else:
                    resting[o.order_id] = (o, side)
                continue
            q = 0.0
            if ask == ask and ask <= o.price + 1e-9:
                depth = adep * bcfg.depth_haircut if adep == adep and adep > 0 else o.size
                q = math.floor(min(o.size, depth) * 100.0) / 100.0
            if q > 0.0:
                px = float(ask) + bcfg.extra_slippage
                fe = fee(px) * q
                e.on_pair_done(o.order_id, filled=True)
                e.on_pair_fill_record(slug, o, SimpleNamespace(size=q, price=px, fee=fe, token_id=o.token_id), now)
                info = e.orders.get(o.order_id, {})
                res.fills.append((info.get("kind", "?"), side, px, q, fe, (b["close"] - now) / 1000.0,
                                  float(up_won == (side == "UP"))))
                res.slippage += (px - expected.get(o.order_id, px)) * q
                res.purchases += 1
                if q + 1e-9 < o.size:
                    res.partial += 1
            else:
                e.on_pair_done(o.order_id, filled=False)
                res.unfilled += 1
        pending = keep
        for oid, (o, side) in list(resting.items()):
            ask = tops[side][1]
            if ask == ask and ask <= o.price + 1e-9:
                resting.pop(oid)
                e.on_pair_done(oid, filled=True)
                e.on_pair_fill_record(slug, o, SimpleNamespace(size=o.size, price=o.price, fee=0.0, token_id=o.token_id), now)
                info = e.orders.get(o.order_id, {})
                res.fills.append((info.get("kind", "?"), side, o.price, o.size, 0.0, (b["close"] - now) / 1000.0,
                                  float(up_won == (side == "UP"))))
                res.purchases += 1
                res.maker_fills += 1
        e.set_book_age(slug, now, 0.0)
        for oid in e.pair_cancels(now) or ():
            if oid in resting:
                resting.pop(oid)
                e.on_pair_done(oid, filled=False)
            else:
                pending = [x for x in pending if x[1].order_id != oid]
                e.on_pair_done(oid, filled=False)
        books = {slug: (_ob(yes, ub[i], ua[i], uad[i], 1.0), _ob(no, db[i], da[i], dad[i], 1.0))}
        for _, side, o in e.pair_requests(now, {slug: m}, books, balance=bcfg.balance) or ():
            e.on_pair_submit(o.order_id, slug, side, o.price, o.size)
            expected[o.order_id] = float(o.expected_price or o.price)
            pending.append((now + bcfg.latency_ms, o, side))
            res.orders += 1
    for _, o, _ in pending:
        e.on_pair_done(o.order_id, filled=False)
        res.unfilled += 1
    for oid in list(resting):
        e.on_pair_done(oid, filled=False)
        res.unfilled += 1
    ms = e.markets.get(slug)
    if ms is not None:
        pf = ms.portfolio
        t = pf.totals
        res.pnl = (t.q_up if up_won else t.q_down) - t.c_total
        res.gross_pnl = res.pnl + t.fees
        res.cost, res.fees, res.cash_spent = t.c_total, t.fees, t.cash_spent
        res.q_up, res.q_down = t.q_up, t.q_down
        res.pairs_formed, res.pairs_qualifying = pf.formed_pairs(), pf.qualifying_pairs()
        res.avg_pair_cost, res.pair_margin = pf.avg_pair_cost(), pf.pair_margin()
        res.unmatched_up, res.unmatched_down = t.unmatched_up, t.unmatched_down
        res.pnl_if_up, res.pnl_if_down = t.pnl_if_up, t.pnl_if_down
        res.state, res.cycles, res.recovery_stops = ms.state.value, ms.cycle, ms.recovery_stops
        res.falling_events, res.leader, res.p0 = ms.falling_events, ms.leader, ms.p0
        res.refusals = dict(ms.refusals)
        e.on_market_settled(slug, bool(up_won), res.pnl, float(b["close"]) + 1.0)
    e._cleanup(float(b["close"]) + 1e9)                           # noqa: SLF001
    e.market_data.pop(slug, None)
    res.baselines = baselines(b, up_won, e.scfg, bcfg)
    return res


# ---------------------------------------------------------------------- baselines (no engine)

def baselines(b: dict, up_won: bool, sc: LeaderPairConfig, bcfg: BacktestConfig) -> dict:
    """1. buy the initial leader and hold; 2. buy the opposite token then and hold; 3. no trade; 4. pair-only:
    the leader entry, then every accumulation slot the opposite is bought ONLY when the pair (leader's average
    fee-inclusive cost + opposite ask + its fee) costs at most pair_threshold -- no momentum rules, no recovery."""
    T, ua, ub, da, db, uad, dad = b["t"], b["ua"], b["ub"], b["da"], b["db"], b["uad"], b["dad"]
    rate = float(b.get("fee_rate") or bcfg.fee_rate)
    fee = lambda p: rate * p * (1.0 - p)                          # noqa: E731
    out = {"no_trade": 0.0, "leader_hold": 0.0, "opposite_hold": 0.0, "pair_only": 0.0, "pair_only_pairs": 0.0}
    entry_t = b["open"] + sc.init_delay_s * 1000.0 + bcfg.latency_ms
    i0 = next((i for i in range(len(T)) if T[i] >= entry_t), None)
    if i0 is None or ua[i0] != ua[i0] or da[i0] != da[i0] or ub[i0] != ub[i0] or db[i0] != db[i0]:
        return out
    mu, md = (ua[i0] + ub[i0]) / 2.0, (da[i0] + db[i0]) / 2.0
    if abs(mu - md) < 1e-12:
        return out
    lead = "UP" if mu > md else "DOWN"
    la, oa = (ua[i0], da[i0]) if lead == "UP" else (da[i0], ua[i0])
    if not (sc.init_min_ask - 1e-9 <= la <= sc.init_max_ask + 1e-9):
        return out
    q = sc.init_shares
    lead_won = up_won == (lead == "UP")
    out["leader_hold"] = q * (1.0 if lead_won else 0.0) - q * (la + fee(la))
    out["opposite_hold"] = q * (0.0 if lead_won else 1.0) - q * (oa + fee(oa))
    # pair-only
    lead_unit = la + fee(la)
    q_lead, c_lead, q_opp, c_opp, slot_done, n = q, q * lead_unit, 0.0, 0.0, -1, 0
    for i in range(i0, len(T)):
        now = T[i]
        if now >= b["close"]:
            break
        slot = int((now - T[i0]) // (sc.accum_interval_s * 1000.0))
        if slot <= slot_done:
            continue
        slot_done = slot
        ask = da[i] if lead == "UP" else ua[i]
        dep = dad[i] if lead == "UP" else uad[i]
        if ask != ask or dep != dep or dep < sc.accum_shares:
            continue
        if c_lead / q_lead + ask + fee(ask) <= sc.pair_threshold + 1e-12 and n < sc.max_purchases_per_market and q_opp < q_lead:
            take = min(sc.accum_shares, q_lead - q_opp)
            if take < 5.0:
                continue
            q_opp += take
            c_opp += take * (ask + fee(ask))
            n += 1
    out["pair_only"] = (q_lead if lead_won else q_opp) - c_lead - c_opp
    out["pair_only_pairs"] = min(q_lead, q_opp)
    return out


# ---------------------------------------------------------------------- the dataset

def load_cache(books_path: Path, outcomes_path: Path, limit: int = 0, since: float | None = None) -> tuple[dict, dict, list[str]]:
    books = pickle.load(open(books_path, "rb"))
    ups = json.load(open(outcomes_path))
    slugs = sorted((s for s in books if s in ups and (since is None or books[s]["open"] >= since)), key=lambda s: books[s]["open"])
    if limit:
        slugs = slugs[:limit]
    return books, ups, slugs


def split_cuts(opens: list[float], train: float = 0.5, val: float = 0.25) -> tuple[float, float]:
    """Chronological TRAIN / VALIDATION / TEST cut points (open timestamps)."""
    n = len(opens)
    return opens[int(n * train)], opens[int(n * (train + val))]


def split_of(open_ts: float, cuts: tuple[float, float]) -> str:
    return "train" if open_ts < cuts[0] else "val" if open_ts < cuts[1] else "test"


# ---------------------------------------------------------------------- metrics

def _dd(rows: list[MarketResult]) -> float:
    eq = peak = mdd = 0.0
    for r in sorted(rows, key=lambda r: r.open):
        eq += r.pnl
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return mdd


def summarise(rows: list[MarketResult]) -> dict:
    """The spec's section-13 figures for one set of markets."""
    if not rows:
        return {"markets": 0}
    traded = [r for r in rows if r.purchases > 0]
    p = [r.pnl for r in rows]
    costs = [r.cost for r in traded]
    refusals = Counter()
    for r in rows:
        refusals.update(r.refusals)
    by_left, by_kind, by_price = {}, {}, {}
    for r in rows:
        for kind, side, px, q, fe, left, won in r.fills:
            for key, bucket in ((by_left, ">240" if left > 240 else "180-240" if left > 180 else "120-180" if left > 120 else "60-120" if left > 60 else "<60"),
                                (by_kind, kind), (by_price, f"{min(int(px * 10), 9) / 10:.1f}")):
                x = bucket if key is not by_price else bucket
                acc = key.setdefault(x, [0.0, 0.0, 0])
                acc[0] += q
                acc[1] += q * won - q * px - fe
                acc[2] += 1
    attr = lambda d: {k: {"shares": round(v[0], 1), "pnl": round(v[1], 2), "c_per_share": round(100 * v[1] / v[0], 2), "fills": v[2]}   # noqa: E731
                      for k, v in sorted(d.items()) if v[0] > 0}
    total_cost = sum(costs)
    net = sum(p)
    base = {k: round(sum(r.baselines.get(k, 0.0) for r in rows), 2) for k in ("no_trade", "leader_hold", "opposite_hold", "pair_only")}
    base_per = {k: round(v / len(rows), 4) for k, v in base.items()}
    states = Counter(r.state for r in rows)
    sd = st.pstdev(p) if len(p) > 1 else 0.0
    return {
        "markets": len(rows), "traded": len(traded), "entered_pct": round(100 * len(traded) / len(rows), 1),
        "net_pnl": round(net, 2), "gross_pnl": round(sum(r.gross_pnl for r in rows), 2),
        "fees": round(sum(r.fees for r in rows), 2), "slippage": round(sum(r.slippage for r in rows), 2),
        "per_market": round(net / len(rows), 4), "per_traded_market": round(net / len(traded), 4) if traded else None,
        "se": round(sd / math.sqrt(len(p)), 4) if len(p) > 1 else None,
        "t": round((net / len(p)) / (sd / math.sqrt(len(p))), 2) if len(p) > 1 and sd > 0 else None,
        "win_rate": round(sum(r.pnl > 0 for r in traded) / len(traded), 4) if traded else None,
        "max_drawdown": round(_dd(rows), 2), "worst_market": round(min(p), 2), "best_market": round(max(p), 2),
        "worst_settlement_outcome": round(min(min(r.pnl_if_up, r.pnl_if_down) for r in traded), 2) if traded else None,
        "max_capital_market": round(max(costs), 2) if costs else 0.0, "avg_capital_market": round(st.mean(costs), 2) if costs else 0.0,
        "turnover": round(sum(r.cash_spent for r in rows), 2), "return_on_turnover_pct": round(100 * net / total_cost, 3) if total_cost else None,
        "gross_return_on_turnover_pct": round(100 * sum(r.gross_pnl for r in rows) / total_cost, 3) if total_cost else None,
        "purchases": sum(r.purchases for r in rows), "orders": sum(r.orders for r in rows), "unfilled": sum(r.unfilled for r in rows),
        "partial_fills": sum(r.partial for r in rows), "maker_fills": sum(r.maker_fills for r in rows),
        "pairs_formed": round(sum(r.pairs_formed for r in rows), 1), "pairs_qualifying": round(sum(r.pairs_qualifying for r in rows), 1),
        "avg_pair_cost": round(sum(r.avg_pair_cost * r.pairs_formed for r in rows if r.avg_pair_cost) / max(1e-9, sum(r.pairs_formed for r in rows)), 4) if any(r.pairs_formed for r in rows) else None,
        "pair_margin_total": round(sum(r.pair_margin for r in rows), 2),
        "unmatched_at_expiry": round(sum(r.unmatched_up + r.unmatched_down for r in rows), 1),
        "markets_with_unmatched": sum((r.unmatched_up + r.unmatched_down) > 1e-9 for r in traded),
        "recovery_stops": sum(r.recovery_stops for r in rows), "markets_with_recovery_stop": sum(r.recovery_stops > 0 for r in rows),
        "falling_events": sum(r.falling_events for r in rows), "cycles": sum(r.cycles for r in rows),
        "limit_activations": {k: v for k, v in refusals.items() if k.split(":")[-1] in
                              ("MARKET_CAPITAL", "TOTAL_CAPITAL", "SETTLEMENT_LOSS", "PURCHASES_CYCLE", "PURCHASES_MARKET", "BALANCE")},
        "refusals": dict(refusals), "final_states": dict(states),
        "baselines_total": base, "baselines_per_market": base_per,
        "by_time_left": attr(by_left), "by_kind": attr(by_kind), "by_price": attr(by_price),
    }


def result_row(r: MarketResult) -> dict:
    d = {k: v for k, v in r.__dict__.items() if k != "fills"}
    d["fills"] = [list(f) for f in r.fills]
    return d


def run(books: dict, ups: dict, slugs: list[str], scfg: LeaderPairConfig | None = None, bcfg: BacktestConfig = BacktestConfig(),
        overrides: dict | None = None, progress=None) -> list[MarketResult]:
    e = engine_for(scfg, **(overrides or {}))
    out = []
    t0 = time.time()
    for n, s in enumerate(slugs):
        out.append(run_market(e, s, books[s], bool(ups[s]), bcfg))
        if progress and (n + 1) % 200 == 0:
            progress(n + 1, len(slugs), time.time() - t0)
    return out


def report_splits(rows: list[MarketResult], cuts: tuple[float, float]) -> dict:
    out = {}
    for name in ("train", "val", "test", "all"):
        sel = [r for r in rows if name == "all" or split_of(r.open, cuts) == name]
        out[name] = summarise(sel)
    return out


__all__ = ["BacktestConfig", "MarketResult", "engine_for", "run_market", "run", "baselines", "load_cache", "split_cuts",
           "split_of", "summarise", "report_splits", "result_row", "State"]
