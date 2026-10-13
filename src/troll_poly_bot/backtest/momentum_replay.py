"""Replay MOMENTUM_TAKER on the recorded windows with REALISTIC execution.

The live decision code runs unchanged -- ``MomentumEngine.evaluate`` for
entries, ``MomentumEngine.exit_decision`` for exits, on the same features --
over the bot's own recordings (``data/charts/<slug>.json``: touch, depth
within two ticks, per-exchange spot and order flow every ~230 ms). Only the
venue is simulated, and it is simulated the way live trading actually went:

    signal at T4 (a recorded point)
      -> order reaches the matching engine at T4 + presend + RTT/2, drawn from
         the round trips MEASURED on the real account (p50 243 ms, not 17 ms)
      -> it matches against the first recorded book AT OR AFTER that arrival:
         the ask must still be at or under the cap with the size behind it,
         or the fill-or-kill is killed (the quote moved or vanished in flight)
      -> quote survival: the fill only counts if that quote is still on the
         recorded book ``quote_survival_ms`` later -- the recorded book lags
         the venue, and on 2026-09-22 this rule reproduced 25 of 30 live kills
      -> taker fee on the fill; the position is managed point by point by the
         live exit logic; exits are fill-or-kill sells with the same latency,
         retried every 2 s; whatever is open 10 s before the bell settles 1/0
         on the venue's outcome

``OPTIMISTIC`` is the old paper engine (fill at the decision quote, no
latency, no survival) and exists only to measure how much of any result is
the simulator. Nothing here chooses a threshold unless asked (``grid``), and
then only on the TRAINING period; the report always shows the validation and
out-of-sample periods it did not see.
"""
from __future__ import annotations

import bisect
import json
import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import numpy as np

from ..config import VolConfig
from ..execution.latency import COLOCATED, PRESEND_PRIOR_MS, VENUE_RTT_PRIOR_MS
from ..features.engine import FeatureEngine
from ..feeds.markets import MarketMeta
from ..feeds.spot import CompositeView
from ..pricing.twap import TwapState
from ..pricing.vol import EwmaVol, TwoScaleVol
from ..risk.limits import RiskConfig, RiskManager
from ..signals.costs import FeeSchedule
from ..strategy.engine import EngineConfig
from ..strategy.momentum import MomentumConfig, MomentumEngine
from ..types import BookLevel, Market, OrderBook

TICK = 0.01


@dataclass(frozen=True, slots=True)
class ExecModel:
    name: str
    rtt_ms: tuple[float, ...] = VENUE_RTT_PRIOR_MS
    presend_ms: tuple[float, ...] = PRESEND_PRIOR_MS
    quote_survival_ms: float = 2000.0
    exit_retry_ms: float = 2000.0
    #: fraction of the displayed touch size assumed gone to faster players
    touch_contention: float = 0.0
    #: urgent exits sell this far through the bid (live: stop_cross_ticks = 2)
    stop_cross: float = 0.02


REALISTIC = ExecModel("realistic")
OPTIMISTIC = ExecModel("optimistic", rtt_ms=(0.0,), presend_ms=(0.0,), quote_survival_ms=0.0)
CONSERVATIVE = ExecModel("conservative", rtt_ms=tuple(x * 1.5 for x in VENUE_RTT_PRIOR_MS),
                         presend_ms=tuple(x * 1.5 for x in PRESEND_PRIOR_MS), quote_survival_ms=3000.0,
                         touch_contention=0.5)
EXEC_MODELS = {m.name: m for m in (REALISTIC, OPTIMISTIC, CONSERVATIVE)}


# ─────────────────────────────── the data ─────────────────────────────────


COLS = ("spot", "up", "ub", "ubd", "ua", "uad", "db", "dbd", "da", "dad", "tfi", "nsrc")
#: per-point extras the momentum replay does not iterate: each exchange's own price
#: (binance / bybit / coinbase) and the spot book imbalance and trade rate
EXTRA_COLS = ("bn", "byb", "cb", "obi", "orate")


@dataclass(slots=True)
class Window:
    """One recorded window, columnar: ``t`` is a list (bisect), every other
    field a float64 array with NaN where the recording had nothing."""
    slug: str
    asset: str
    open_ts: float
    close_ts: float
    up_won: bool | None
    fee: FeeSchedule
    t: list[float]
    cols: dict
    #: the live bot's strike (60 s TWAP at the open); 0 when it never struck
    strike: float = 0.0

    @property
    def epoch(self) -> int:
        return int(self.open_ts // 1000)

    def __len__(self) -> int:
        return len(self.t)


def _nan(v) -> float:
    return math.nan if v is None else float(v)


def load_window(path: str | Path) -> Window | None:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pts = [p for p in d.get("points") or [] if p.get("t") is not None and p.get("spot")]
    if len(pts) < 100 or not d.get("slug"):
        return None
    pts.sort(key=lambda p: p["t"])
    # the venue's recorded outcome when there was one; else the closing book
    # (0.99/0.01 at the bell); undecidable windows stay None and never settle
    up_won = None
    if d.get("outcome_source") == "venue" and d.get("outcome") in ("UP", "DOWN"):
        up_won = d["outcome"] == "UP"
    else:
        tail = [p for p in pts if (p.get("left") or 99) <= 5.0 and p.get("up") is not None]
        if tail:
            last = tail[-1]["up"]
            up_won = True if last >= 0.9 else False if last <= 0.1 else None
    cols = {c: np.empty(len(pts)) for c in COLS + EXTRA_COLS}
    for i, p in enumerate(pts):
        of = p.get("of") or {}
        for c in COLS[:-2]:
            cols[c][i] = _nan(p.get(c))
        cols["tfi"][i] = _nan(of.get("tfi15"))
        srcs = p.get("srcs") or {}
        cols["nsrc"][i] = len(srcs) or 1
        cols["bn"][i], cols["byb"][i], cols["cb"][i] = (_nan(srcs.get(x)) for x in ("binance", "bybit", "coinbase"))
        cols["obi"][i], cols["orate"][i] = _nan(of.get("obi")), _nan(of.get("rate"))
    fee = d.get("fee") or {}
    return Window(slug=d["slug"], asset=d["asset"], open_ts=float(d["open_ts"]), close_ts=float(d["close_ts"]),
                  up_won=up_won, fee=FeeSchedule(rate=float(fee.get("rate", 0.07)),
                                                 exponent=float(fee.get("exponent", 1.0))),
                  t=[float(p["t"]) for p in pts], cols=cols, strike=float(d.get("strike") or 0.0))


def load_windows(chart_dir: str | Path = "data/charts") -> list[Window]:
    out = [w for w in (load_window(p) for p in sorted(Path(chart_dir).glob("*.json"))) if w is not None]
    out.sort(key=lambda w: (w.open_ts, w.asset))
    return out


def split_by_time(windows: list[Window], fractions=(0.5, 0.25, 0.25)) -> dict[str, list[Window]]:
    """TRAINING / VALIDATION / OUT-OF-SAMPLE, chronological by 5-minute epoch
    (every asset of one epoch lands in the same period: they are one bet)."""
    epochs = sorted({w.epoch for w in windows})
    n = len(epochs)
    a = int(n * fractions[0])
    b = int(n * (fractions[0] + fractions[1]))
    period = {e: ("train" if i < a else "validation" if i < b else "test") for i, e in enumerate(epochs)}
    out: dict[str, list[Window]] = {"train": [], "validation": [], "test": []}
    for w in windows:
        out[period[w.epoch]].append(w)
    return out


def _f(x) -> float | None:
    x = float(x)
    return None if math.isnan(x) else x


def _q(w: Window, i: int, side: str):
    """(bid, bid depth, ask, ask depth, mid) of one token at point ``i``."""
    c = w.cols
    if side == "UP":
        b, bd, a, ad = _f(c["ub"][i]), _f(c["ubd"][i]), _f(c["ua"][i]), _f(c["uad"][i])
    else:
        b, bd, a, ad = _f(c["db"][i]), _f(c["dbd"][i]), _f(c["da"][i]), _f(c["dad"][i])
    mid = (b + a) / 2.0 if (b is not None and a is not None) else None
    return b, bd, a, ad, mid


def _book(token: str, bid, bid_sz, ask, ask_sz, ts: float, contention: float = 0.0) -> OrderBook:
    bids = [BookLevel(bid, bid_sz or 0.0)] if bid is not None else []
    asks = [BookLevel(ask, (ask_sz or 0.0) * (1.0 - contention))] if ask is not None else []
    return OrderBook(token_id=token, bids=bids, asks=asks, ts=ts, recv_ts=ts)


# ─────────────────────────────── the replay ───────────────────────────────


@dataclass(slots=True)
class _Pos:
    side: str
    token: str
    size: float
    entry: float
    entry_fee: float
    fill_ts: float
    signal: dict
    next_check: float = 0.0
    exit_attempts: int = 0


class MomentumReplay:
    """One pass over a list of windows with one configuration."""

    #: modelled data ages: the recorded book is our local copy (book lag p50
    #: ~45 ms) and the composite spot is ~160 ms behind the exchanges
    BOOK_AGE_MS = 50.0
    SPOT_AGE_MS = 300.0

    def __init__(self, mcfg: MomentumConfig, exec_model: ExecModel = REALISTIC, seed: int = 7,
                 risk_cfg: RiskConfig | None = None, record_skips: bool = True,
                 skip_gap_ms: float = 2000.0, skip_horizon_s: float = 20.0) -> None:
        self.mcfg = mcfg
        self.exec = exec_model
        self.rng = random.Random(seed)
        # micro-live sizing: the smallest valid order, so paper and micro-live compare
        self.risk_cfg = risk_cfg or RiskConfig(micro_fixed_size=True, max_position_usdc=5.0,
                                               max_total_exposure_usdc=1e9, max_epoch_exposure_usdc=1e9,
                                               max_asset_exposure_usdc=1e9, max_concurrent_positions=1_000_000,
                                               max_daily_loss_usdc=1e9, max_drawdown_usdc=1e9, min_order_usdc=1.0)
        self.record_skips = record_skips
        self.skip_gap_ms = skip_gap_ms
        self.skip_horizon_s = skip_horizon_s
        self.vcfg = VolConfig()
        self._state: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.orders: list[dict] = []
        self.skips: list[dict] = []
        self.latency = replace(COLOCATED, name="replay")      # the strategy's own view: tiny, replaced below

    # ---------------------------------------------------------------- helpers

    def _draw(self, values: tuple[float, ...]) -> float:
        return self.rng.choice(values) * (self.rng.uniform(0.85, 1.15) if len(values) > 1 else 1.0)

    def one_way_ms(self) -> float:
        return self._draw(self.exec.presend_ms) + self._draw(self.exec.rtt_ms) / 2.0

    def _make_vol(self):
        v = self.vcfg
        fast = EwmaVol(grid_ms=v.grid_ms, halflife_s=v.halflife_s, floor_per_sec=v.floor_per_sec,
                       ceil_per_sec=v.ceil_per_sec)
        slow = EwmaVol(grid_ms=v.slow_grid_ms, halflife_s=v.slow_halflife_s, floor_per_sec=v.floor_per_sec,
                       ceil_per_sec=v.ceil_per_sec)
        return TwoScaleVol(fast=fast, slow=slow, default_ratio=v.default_ratio)

    def _asset_state(self, asset: str, first_t: float) -> dict:
        st = self._state.get(asset)
        if st is None or first_t - st["last_t"] > 120_000.0:
            # a gap in the recording: the estimators start again, as a restart would
            risk = RiskManager(cfg=self.risk_cfg, starting_balance=100.0)
            eng = MomentumEngine(EngineConfig(strategy="momentum_taker"), self.mcfg, risk, FeatureEngine())
            st = {"engine": eng, "vol": self._make_vol(), "last_t": -math.inf}
            self._state[asset] = st
        return st

    def _index_at_or_after(self, w: Window, ts: float) -> int | None:
        k = bisect.bisect_left(w.t, ts)
        return k if k < len(w.t) else None

    # ------------------------------------------------------------------ run

    def run(self, windows: Iterable[Window]) -> "MomentumReplay":
        for w in windows:
            self._run_window(w)
        return self

    def _meta(self, w: Window) -> MarketMeta:
        m = Market(condition_id=w.slug, asset=w.asset, yes_token_id=f"{w.slug}:UP", no_token_id=f"{w.slug}:DOWN",
                   strike=1.0, open_ts=w.open_ts, close_ts=w.close_ts, tick_size=TICK, min_size=5.0, slug=w.slug)
        return MarketMeta(market=m, resolution_source="", accepting_orders=True, duration_s=300.0, fee=w.fee)

    def _run_window(self, w: Window) -> None:
        st = self._asset_state(w.asset, w.t[0])
        eng: MomentumEngine = st["engine"]
        vol = st["vol"]
        meta = self._meta(w)
        m = meta.market
        twap = TwapState()
        spot, tfi, nsrc = w.cols["spot"], w.cols["tfi"], w.cols["nsrc"]
        pos: _Pos | None = None
        exited = False                          # live: an exited market is done for the window
        busy_until = -math.inf
        last_skip: dict[str, float] = {}
        mid_rtt = sorted(self.exec.rtt_ms)[len(self.exec.rtt_ms) // 2]
        mid_pre = sorted(self.exec.presend_ms)[len(self.exec.presend_ms) // 2]
        lat_profile = replace(self.latency, submit_base=mid_pre + mid_rtt / 2.0, ack_base=mid_rtt / 2.0,
                              md_spot_base=160.0, submit_jitter=0.0, ack_jitter=0.0, md_spot_jitter=0.0)
        for i in range(len(w)):
            t = w.t[i]
            px_spot = float(spot[i])
            if t > st["last_t"]:
                eng.features.update_spot(w.asset, px_spot, t)
                vol.update(px_spot, t)
                st["last_t"] = t
            if t >= w.close_ts:
                break
            left = (w.close_ts - t) / 1000.0
            ub, _, ua, _, mid_up = _q(w, i, "UP")
            if mid_up is not None:
                eng.features.update_market(w.slug, mid_up, t)       # the re-pricing history, every point
            f = float(tfi[i])
            of = None if math.isnan(f) else {"tfi_15s": f}
            # ---------------- manage an open position ----------------
            if pos is not None:
                if left <= 10.0 or t < pos.next_check:
                    continue                    # too late to exit (rides to the bell) / not yet learned
                b, bd, a, ad, _ = _q(w, i, pos.side)
                if b is None:
                    continue
                sig = eng.momentum_now(w.asset, t, of)
                spread = (a - b) if a is not None else None
                kind = eng.exit_decision(asset=w.asset, side=pos.side, token_id=pos.token, entry=pos.entry,
                                         bid=b, now_ms=t, sig=sig, spread=spread, bid_depth=bd, size=pos.size,
                                         data_age_ms=self.SPOT_AGE_MS)
                if kind is None:
                    continue
                pos.exit_attempts += 1
                floor = b if kind == "TAKE_PROFIT" else max(TICK, round(b - self.exec.stop_cross, 4))
                j = self._index_at_or_after(w, t + self.one_way_ms())
                filled_px = None
                if j is not None and w.t[j] < w.close_ts:
                    b2, bd2, _, _, _ = _q(w, j, pos.side)
                    if b2 is not None and b2 >= floor - 1e-9 and (bd2 or 0.0) >= pos.size:
                        filled_px = b2              # a FOK sell fills at the bids at or above its floor
                if filled_px is None:
                    pos.next_check = t + self.exec.exit_retry_ms
                    continue
                self._close(w, eng, pos, kind, filled_px, w.t[j])
                pos, exited = None, True
                continue
            if exited or t < busy_until:
                continue
            # ---------------- look for an entry ----------------
            _, ubd, _, uad, _ = _q(w, i, "UP")
            db, dbd, da, dad, _ = _q(w, i, "DOWN")
            up = _book(m.yes_token_id, ub, ubd, ua, uad, t)
            dn = _book(m.no_token_id, db, dbd, da, dad, t)
            view = CompositeView(w.asset, px_spot, px_spot, int(nsrc[i]), 0.0, 0.0, 0.0, {})
            ev = eng.evaluate(meta, up, dn, view, self.SPOT_AGE_MS, self.BOOK_AGE_MS, t, vol, twap, lat_profile,
                              100.0, orderflow=of,
                              quote_ages={m.yes_token_id: self.BOOK_AGE_MS, m.no_token_id: self.BOOK_AGE_MS})
            if not ev.tradeable:
                if self.record_skips and ev.sides and ev.sides[0].ask is not None:
                    reason = ev.reason.value if ev.reason else ""
                    if t - last_skip.get(reason, -math.inf) >= self.skip_gap_ms:
                        last_skip[reason] = t
                        self._record_skip(w, i, ev, reason)
                continue
            # ---------------- send it: the venue decides at arrival ----------------
            arrive = t + self.one_way_ms()
            sig = dict(ev.features.get("momentum") or {})
            exe = dict(ev.features.get("execution") or {})
            rec = {"slug": w.slug, "asset": w.asset, "epoch": w.epoch, "side": ev.side, "decision_ts": t,
                   "secs_left": round(left, 1), "ask": exe.get("best_ask"), "cap": ev.limit_price,
                   "max_entry": ev.max_entry_price, "vwap": ev.expected_price, "size": ev.size,
                   "fair": exe.get("fair"), "net_edge": round(ev.net_edge, 5), "z": (sig.get("z") or {}).get("main"),
                   "label": sig.get("label"), "score": sig.get("score"), "spread": exe.get("spread"),
                   "uncertainty": exe.get("uncertainty"), "exit_cost": exe.get("exit_cost")}
            eng.risk.on_submit(w.slug)
            j = self._index_at_or_after(w, arrive)
            outcome, fill_px = "KILLED", None
            if j is None or w.t[j] >= w.close_ts:
                outcome = "MARKET_CLOSED"
            else:
                _, _, a2, ad2, _ = _q(w, j, ev.side)
                avail = (ad2 or 0.0) * (1.0 - self.exec.touch_contention)
                if a2 is not None and a2 <= ev.limit_price + 1e-9 and avail + 1e-9 >= ev.size:
                    fill_px, outcome = a2, "FILLED"
                    if self.exec.quote_survival_ms > 0:
                        k = self._index_at_or_after(w, w.t[j] + self.exec.quote_survival_ms)
                        if k is not None:
                            _, _, a3, ad3, _ = _q(w, k, ev.side)
                            if a3 is None or a3 > fill_px + 1e-9 or (ad3 or 0.0) + 1e-9 < ev.size:
                                outcome, fill_px = "QUOTE_PULLED", None
            rec.update({"arrival_ts": w.t[j] if j is not None else None, "outcome": outcome,
                        "arrival_ask": _q(w, j, ev.side)[2] if j is not None else None})
            self.orders.append(rec)
            eng.risk.on_done(w.slug)
            learn = (w.t[j] if j is not None else arrive) + self.exec.quote_survival_ms + \
                self._draw(self.exec.rtt_ms) / 2.0
            if fill_px is None:
                busy_until = max(learn, t + 2000.0)          # the live entry retry throttle
                continue
            fee = w.fee.taker_fee_per_share(fill_px) * ev.size
            eng.risk.on_fill(w.slug, w.asset, w.epoch, ev.side, fill_px * ev.size, ev.size)
            eng.on_entry_fill(w.slug, ev.token_id, w.t[j])
            rec["fill_px"] = fill_px
            rec["slippage"] = round(fill_px - (ev.expected_price or fill_px), 5)
            rec["markout"] = self._markouts(w, j, ev.side)
            pos = _Pos(side=ev.side, token=ev.token_id, size=ev.size, entry=fill_px, entry_fee=fee,
                       fill_ts=w.t[j], signal=rec, next_check=learn)
        if pos is not None:
            self._settle(w, eng, pos)

    def _markouts(self, w: Window, j: int, side: str) -> dict:
        """Mid move in our direction after the fill (adverse selection)."""
        m0 = _q(w, j, side)[4]
        out = {}
        if m0 is None:
            return out
        for off in (250, 500, 1000, 2000, 5000, 10000):
            k = self._index_at_or_after(w, w.t[j] + off)
            if k is None:
                continue
            mk = _q(w, k, side)[4]
            if mk is not None:
                out[str(off)] = round(mk - m0, 5)
        return out

    def _close(self, w: Window, eng: MomentumEngine, pos: _Pos, kind: str, px: float, ts: float) -> None:
        exit_fee = w.fee.taker_fee_per_share(px) * pos.size
        pnl = (px - pos.entry) * pos.size - pos.entry_fee - exit_fee
        self._book_trade(w, eng, pos, kind, px, ts, pnl, pos.entry_fee + exit_fee)

    def _settle(self, w: Window, eng: MomentumEngine, pos: _Pos) -> None:
        if w.up_won is None:
            pnl, px, kind = None, None, "UNRESOLVED"
        else:
            won = (pos.side == "UP") == w.up_won
            px = 1.0 if won else 0.0
            pnl = (px - pos.entry) * pos.size - pos.entry_fee
            kind = "SETTLED"
        self._book_trade(w, eng, pos, kind, px, w.close_ts, pnl, pos.entry_fee)

    def _book_trade(self, w, eng, pos, kind, px, ts, pnl, fees) -> None:
        eng.risk.on_settle(w.slug, pnl or 0.0, ts / 1000.0)
        eng.on_exit(w.slug, pos.token, ts)
        row = dict(pos.signal)
        row.update({"exit_kind": kind, "exit_px": px, "exit_ts": ts, "hold_s": round((ts - pos.fill_ts) / 1000.0, 1),
                    "fees": round(fees, 5), "pnl": None if pnl is None else round(pnl, 5),
                    "pnl_per_share": None if pnl is None else round(pnl / pos.size, 5),
                    "exit_attempts": pos.exit_attempts})
        self.trades.append(row)

    def _record_skip(self, w: Window, i: int, ev, reason: str) -> None:
        """A priced signal we did NOT take: was the price there at arrival, and
        what would a taker round trip have made at the horizon?"""
        sv = ev.sides[0]
        t = w.t[i]
        cap = ev.max_entry_price if ev.max_entry_price is not None else sv.ask
        j = self._index_at_or_after(w, t + self.one_way_ms())
        row = {"slug": w.slug, "asset": w.asset, "epoch": w.epoch, "side": sv.side, "ts": t, "reason": reason,
               "ask": sv.ask, "cap": cap, "secs_left": round((w.close_ts - t) / 1000.0, 1),
               "z": ((ev.features.get("momentum") or {}).get("z") or {}).get("main")}
        if j is None:
            row["class"] = "UNKNOWN"
            self.skips.append(row)
            return
        _, _, a2, ad2, _ = _q(w, j, sv.side)
        # a skip is judged at the price it was actually offered at arrival
        # (the ask), whatever our max-entry rule said -- that is the question
        available = a2 is not None and (ad2 or 0.0) >= 5.0
        row["available"] = available
        row["arrival_ask"] = a2
        k = self._index_at_or_after(w, t + self.skip_horizon_s * 1000.0)
        if not available:
            row["class"] = "UNFILLABLE_SKIP"
        elif k is None or w.t[k] >= w.close_ts - 10_000.0:
            row["class"] = "UNKNOWN"
        else:
            b3 = _q(w, k, sv.side)[0]
            if b3 is None:
                row["class"] = "UNKNOWN"
            else:
                net = b3 - a2 - w.fee.taker_fee_per_share(a2) - w.fee.taker_fee_per_share(b3)
                row["hypothetical_net"] = round(net, 5)
                row["class"] = "BAD_MISS" if net > 0 else "GOOD_MISS"
        self.skips.append(row)


# ───────────────────────────── statistics ────────────────────────────────


def summarize(trades: list[dict], orders: list[dict] | None = None) -> dict:
    """Per-trade and per-EPOCH statistics (every asset in a window is one bet)."""
    orders = orders or []
    done = [t for t in trades if t.get("pnl") is not None]
    out: dict = {"orders": len(orders), "fills": len(trades),
                 "fill_rate": round(len(trades) / len(orders), 3) if orders else None}
    if orders:
        by = defaultdict(int)
        for o in orders:
            by[o["outcome"]] += 1
        out["order_outcomes"] = dict(by)
    if not done:
        out.update({"trades": 0})
        return out
    pnl = [t["pnl"] for t in done]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    per_epoch: dict[int, float] = defaultdict(float)
    for t in done:
        per_epoch[t["epoch"]] += t["pnl"]
    ev = list(per_epoch.values())
    se = statistics.stdev(ev) / math.sqrt(len(ev)) if len(ev) > 1 else float("nan")
    eq = peak = dd = 0.0
    for t in sorted(done, key=lambda r: r["exit_ts"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    slips = [t["slippage"] for t in done if t.get("slippage") is not None]
    m1 = [t["markout"].get("1000") for t in done if t.get("markout") and t["markout"].get("1000") is not None]
    kinds = defaultdict(int)
    for t in done:
        kinds[t["exit_kind"]] += 1
    out.update({
        "trades": len(done), "win_rate": round(len(wins) / len(done), 3),
        "pnl": round(sum(pnl), 4), "expectancy": round(statistics.fmean(pnl), 5),
        "expectancy_per_share": round(statistics.fmean([t["pnl_per_share"] for t in done]), 5),
        "avg_win": round(statistics.fmean(wins), 5) if wins else None,
        "avg_loss": round(statistics.fmean(losses), 5) if losses else None,
        "avg_entry": round(statistics.fmean([t["fill_px"] for t in done]), 4),
        "avg_exit": round(statistics.fmean([t["exit_px"] for t in done if t["exit_px"] is not None]), 4),
        "fees": round(sum(t["fees"] for t in done), 4),
        "avg_slippage": round(statistics.fmean(slips), 5) if slips else None,
        "markout_1s": round(statistics.fmean(m1), 5) if m1 else None,
        "max_drawdown": round(dd, 4), "epochs": len(ev),
        "t_epoch": None if len(ev) < 2 or not se or math.isnan(se) else round(statistics.fmean(ev) / se, 2),
        "exit_kinds": dict(kinds),
        "avg_hold_s": round(statistics.fmean([t["hold_s"] for t in done]), 1),
    })
    return out


def group(trades: list[dict], key) -> dict[str, dict]:
    g: dict[str, list] = defaultdict(list)
    for t in trades:
        g[key(t)].append(t)
    return {k: summarize(v) for k, v in sorted(g.items())}


def time_bucket(left: float) -> str:
    return "early 200-295s" if left >= 200 else "middle 120-200s" if left >= 120 else "late 60-120s" if left >= 60 else "last <60s"


def z_bucket(z) -> str:
    if z is None:
        return "?"
    z = abs(z)
    return "z>=5" if z >= 5 else "4-5" if z >= 4 else "3-4" if z >= 3 else "2-3" if z >= 2 else "<2"


def fit_calibration(windows: list[Window], mcfg: MomentumConfig, horizon_s: float = 20.0,
                    sample_gap_ms: float = 2000.0) -> dict:
    """Continuation of the token mid after momentum, per (asset, z bucket),
    clustered by WINDOW (points of one window are not independent). Fit on a
    training period only; the engine charges the standard error as uncertainty."""
    from ..strategy.momentum import detect_momentum
    per: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    rp = MomentumReplay(mcfg, record_skips=False)
    for w in windows:
        st = rp._asset_state(w.asset, w.t[0])
        fe, vol = st["engine"].features, st["vol"]
        spot = w.cols["spot"]
        last = -math.inf
        for i in range(len(w)):
            t = w.t[i]
            if t > st["last_t"]:
                fe.update_spot(w.asset, float(spot[i]), t)
                vol.update(float(spot[i]), t)
                st["last_t"] = t
            left = (w.close_ts - t) / 1000.0
            if not (mcfg.min_time_remaining_s <= left <= mcfg.max_time_remaining_s) or t - last < sample_gap_ms:
                continue
            if not vol.ready:
                continue
            sig = detect_momentum(fe.spot.get(w.asset), t, vol.sigma_per_sec, mcfg)
            if abs(sig.z_main) < 2.0 or sig.direction == 0:
                continue
            side = "UP" if sig.direction > 0 else "DOWN"
            m0 = _q(w, i, side)[4]
            k = bisect.bisect_left(w.t, t + horizon_s * 1000.0)
            if m0 is None or k >= len(w.t):
                continue
            mh = _q(w, k, side)[4]
            if mh is None:
                continue
            last = t
            per[f"{w.asset}|{_zb(sig.z_main)}"][w.slug].append(mh - m0)
            per[f"*|{_zb(sig.z_main)}"][w.slug].append(mh - m0)
    rows = {}
    for key, by_w in per.items():
        means = [statistics.fmean(v) for v in by_w.values()]
        n = len(means)
        rows[key] = {"n_windows": n, "n_points": sum(len(v) for v in by_w.values()),
                     "mean": round(statistics.fmean(means), 5) if means else 0.0,
                     "se": round(statistics.stdev(means) / math.sqrt(n), 5) if n > 1 else None}
    return {"horizon_s": horizon_s, "rows": rows}


def _zb(z: float) -> str:
    z = abs(z)
    return "4+" if z >= 4 else "3-4" if z >= 3 else "2-3" if z >= 2 else "<2"
