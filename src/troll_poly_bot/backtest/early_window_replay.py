"""Replay EARLY_WINDOW_LOW_ASK on the recorded windows with REALISTIC execution.

The decision is ``strategy.early_window.evaluate_entry``; the probability is
the live analytic pricer (``twap_fair_value``: Student-t, TWAP-aware variance,
the live bot's own strike, the two-scale vol estimator warmed per asset across
windows exactly as the momentum replay does). The venue is simulated with the
momentum replay's execution models (``momentum_replay.EXEC_MODELS``):

    decision at a recorded point, 0 <= seconds since the market start <= 60
      -> the order arrives presend + RTT/2 later (measured venue round trips)
      -> fill-or-kill against the first recorded book at or after arrival:
         ask <= cap with our size behind it, or KILLED
      -> quote survival: the ask must still be there ``quote_survival_ms``
         later, or QUOTE_PULLED (the recorded book lags the venue)
      -> killed orders may retry after 2 s, still only inside the entry window
      -> exits: fill-or-kill sells at the BID with the same latency, retried
         every 2 s; nothing inside the last 10 s; the rest settles 1.0 / 0.0
         on the venue's outcome

One entry per window, as the live bot does. ``entry_window_*`` in the config
can narrow the window to a single 10-second slice, which is how the per-slice
counterfactuals are run (each slice gets its own independent sample of
windows instead of the leftovers from the slices before it).
"""
from __future__ import annotations

import bisect
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from ..config import VolConfig
from ..pricing.digital import DEFAULT_PRICER
from ..pricing.twap import TwapState, twap_fair_value
from ..pricing.vol import EwmaVol, TwoScaleVol
from ..strategy.early_window import (
    EarlyWindowConfig, Quote, evaluate_entry, exit_reason, seconds_since_market_start,
)
from .momentum_replay import REALISTIC, ExecModel, Window, _q

#: modelled data ages, as in the momentum replay: the recorded book is our
#: local copy (lag p50 ~45 ms), the composite spot ~160-300 ms behind
BOOK_AGE_MS = 50.0
SPOT_AGE_MS = 300.0
#: the recorder writes a point every ~226 ms; a longer gap is an outage
RECORD_INTERVAL_MS = 250.0
#: the composite not changing at all for this long is a dead feed
SPOT_DEAD_MS = 5000.0

BUCKETS = ((0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (50, 60))


def bucket_of(s: float) -> str:
    """The 10-second slice of the first minute; 60.0 exactly joins 50-60."""
    for lo, hi in BUCKETS:
        if lo <= s < hi or (hi == 60 and s == 60.0):
            return f"{lo}-{hi}s"
    return "pre-open" if s < 0 else "after 60s"


@dataclass(slots=True)
class _Pos:
    side: str
    size: float
    entry: float
    entry_fee: float
    fill_ts: float
    rec: dict
    next_check: float
    mfe: float = 0.0
    mae: float = 0.0
    target_hit_ts: float | None = None
    exit_attempts: int = 0


@dataclass
class EarlyWindowReplay:
    cfg: EarlyWindowConfig
    exec: ExecModel = REALISTIC
    seed: int = 7
    #: record every low-ask side-point in the first minute (market conditions)
    record_market: bool = True
    trades: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    opportunities: list[dict] = field(default_factory=list)
    #: bucket -> reason -> decision points rejected for it
    funnel: dict = field(default_factory=lambda: defaultdict(Counter))
    #: bucket -> sums over every side-point with ask < threshold
    market: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(float)))
    windows_seen: int = 0

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.vcfg = VolConfig()
        self._state: dict[str, dict] = {}

    # ---------------------------------------------------------------- helpers

    def _draw(self, values: tuple[float, ...]) -> float:
        return self.rng.choice(values) * (self.rng.uniform(0.85, 1.15) if len(values) > 1 else 1.0)

    def one_way_ms(self) -> float:
        return self._draw(self.exec.presend_ms) + self._draw(self.exec.rtt_ms) / 2.0

    def _asset_state(self, asset: str, first_t: float) -> dict:
        st = self._state.get(asset)
        if st is None or first_t - st["last_t"] > 120_000.0:
            v = self.vcfg
            fast = EwmaVol(grid_ms=v.grid_ms, halflife_s=v.halflife_s, floor_per_sec=v.floor_per_sec,
                           ceil_per_sec=v.ceil_per_sec)
            slow = EwmaVol(grid_ms=v.slow_grid_ms, halflife_s=v.slow_halflife_s, floor_per_sec=v.floor_per_sec,
                           ceil_per_sec=v.ceil_per_sec)
            st = {"vol": TwoScaleVol(fast=fast, slow=slow, default_ratio=v.default_ratio), "last_t": -math.inf,
                  "last_px": None, "last_change": -math.inf}
            self._state[asset] = st
        return st

    @staticmethod
    def _at_or_after(w: Window, ts: float) -> int | None:
        k = bisect.bisect_left(w.t, ts)
        return k if k < len(w.t) else None

    @staticmethod
    def _quote(w: Window, i: int, side: str) -> Quote:
        b, bd, a, ad, _ = _q(w, i, side)
        return Quote(bid=b, bid_depth=bd or 0.0, ask=a, ask_depth=ad or 0.0)

    # ------------------------------------------------------------------ run

    def run(self, windows: Iterable[Window]) -> "EarlyWindowReplay":
        for w in windows:
            self._run_window(w)
        return self

    def _strike(self, w: Window, twap: TwapState) -> float:
        """The live bot's recorded strike; else the 60 s TWAP at the open, only
        when the recording fully covers that minute (never a partial window)."""
        if w.strike > 0.0:
            return w.strike
        if twap.fully_covered(w.open_ts):
            return twap.trailing_twap(w.open_ts) or 0.0
        return 0.0

    def _run_window(self, w: Window) -> None:
        cfg = self.cfg
        self.windows_seen += 1
        st = self._asset_state(w.asset, w.t[0])
        vol = st["vol"]
        twap = TwapState()
        spot = w.cols["spot"]
        nsrc = w.cols["nsrc"]
        strike: float | None = None
        pos: _Pos | None = None
        done = False                            # one entry per window
        busy_until = -math.inf
        opp_buckets: set[str] = set()
        prev_t = -math.inf
        for i in range(len(w)):
            t = w.t[i]
            px = float(spot[i])
            if t > st["last_t"]:
                vol.update(px, t)
                if px != st["last_px"]:
                    st["last_px"], st["last_change"] = px, t
                st["last_t"] = t
            twap.update(px, t)
            gap = t - prev_t
            prev_t = t
            if strike is None and t >= w.open_ts:
                strike = self._strike(w, twap)
            if t >= w.close_ts:
                break
            s = seconds_since_market_start(w.open_ts, t)
            left = (w.close_ts - t) / 1000.0

            if pos is not None:
                self._manage(w, i, pos, left)
                if pos.rec.get("exit_kind"):
                    pos, done = None, True
                continue
            if done or s > cfg.entry_window_end_s + 1.0:
                continue
            b = bucket_of(s)
            quotes = {"UP": self._quote(w, i, "UP"), "DOWN": self._quote(w, i, "DOWN")}
            if self.record_market and 0.0 <= s <= 60.0:
                self._record_market(b, quotes)
            if t < busy_until:
                continue
            p_up = unc = None
            if strike and vol.ready:
                fv = twap_fair_value(state=twap, strike=strike, sigma_per_sec=vol.sigma_per_sec, now_ms=t,
                                     close_ts_ms=w.close_ts, info_lag_s=SPOT_AGE_MS / 1000.0,
                                     pricer=DEFAULT_PRICER)
                p_up, unc = fv.p_up, fv.uncertainty
            book_age = BOOK_AGE_MS + max(0.0, gap - RECORD_INTERVAL_MS) if gap < math.inf else None
            spot_age = SPOT_AGE_MS if t - st["last_change"] <= SPOT_DEAD_MS else t - st["last_change"]
            one_way = self.one_way_ms()
            d = evaluate_entry(cfg, now_ms=t, open_ts_ms=w.open_ts, close_ts_ms=w.close_ts, quotes=quotes,
                               book_age_ms=book_age, spot_age_ms=spot_age, n_sources=int(nsrc[i]),
                               source_deviation_bps=0.0, p_up_model=p_up, model_uncertainty=unc or 0.0,
                               fee=w.fee, one_way_ms=one_way)
            self.funnel[b][d.reason or "ENTRY"] += 1
            if not d.ok:
                continue
            rec = {"slug": w.slug, "asset": w.asset, "epoch": w.epoch, "bucket": b, "side": d.side,
                   "decision_ts": t, "s_decision": round(s, 3), "secs_left": round(left, 1), "ask": d.ask,
                   "bid": d.bid, "spread": d.spread, "spread_pct": round(d.spread_pct, 4), "ask_depth": d.ask_depth,
                   "p_up_model": round(d.p_up_model, 4), "p_side": round(d.p_side, 4),
                   "model_edge": round(d.model_edge, 4), "expected_net": round(d.expected_net, 4),
                   "cap": d.limit_price, "size": d.size, "costs": d.costs,
                   "sigma_bps_s": round(vol.sigma_per_sec * 1e4, 4) if vol.ready else None}
            rec.update(self._token_noise(w, i, d.side))
            if b not in opp_buckets:
                opp_buckets.add(b)
                self.opportunities.append(dict(rec))
            # ---------------- the venue decides at arrival ----------------
            arrive = t + one_way
            j = self._at_or_after(w, arrive)
            outcome, fill_px = "KILLED", None
            if j is None or w.t[j] >= w.close_ts:
                outcome = "MARKET_CLOSED"
            else:
                q2 = self._quote(w, j, d.side)
                avail = q2.ask_depth * (1.0 - self.exec.touch_contention)
                if q2.ask is not None and q2.ask <= d.limit_price + 1e-9 and avail + 1e-9 >= d.size:
                    fill_px, outcome = q2.ask, "FILLED"
                    if self.exec.quote_survival_ms > 0:
                        k = self._at_or_after(w, w.t[j] + self.exec.quote_survival_ms)
                        if k is not None:
                            q3 = self._quote(w, k, d.side)
                            if q3.ask is None or q3.ask > fill_px + 1e-9 or q3.ask_depth + 1e-9 < d.size:
                                outcome, fill_px = "QUOTE_PULLED", None
            rec.update({"outcome": outcome, "arrival_ts": w.t[j] if j is not None else None,
                        "arrival_ask": self._quote(w, j, d.side).ask if j is not None else None})
            self.orders.append(dict(rec))
            learn = (w.t[j] if j is not None else arrive) + self.exec.quote_survival_ms + \
                self._draw(self.exec.rtt_ms) / 2.0
            if fill_px is None:
                busy_until = max(learn, t + 2000.0)
                continue
            rec.update({"fill_px": fill_px, "fill_ts": w.t[j], "s_fill": round((w.t[j] - w.open_ts) / 1000.0, 3),
                        "slippage": round(fill_px - d.ask, 5)})
            pos = _Pos(side=d.side, size=d.size, entry=fill_px, entry_fee=w.fee.taker_fee_per_share(fill_px) * d.size,
                       fill_ts=w.t[j], rec=rec, next_check=learn)
        if pos is not None:
            self._settle(w, pos)

    # ---------------------------------------------------------- positions

    def _manage(self, w: Window, i: int, pos: _Pos, left: float) -> None:
        t = w.t[i]
        if t < pos.fill_ts:
            return
        q = self._quote(w, i, pos.side)
        if q.bid is not None:
            # excursions of what we could SELL at, while an exit is still possible
            if left > self.cfg.exit_min_secs_left:
                pos.mfe = max(pos.mfe, q.bid - pos.entry)
                pos.mae = min(pos.mae, q.bid - pos.entry)
                if pos.target_hit_ts is None and q.bid >= pos.entry + self.cfg.take_profit - 1e-9:
                    pos.target_hit_ts = t
        if t < pos.next_check:
            return
        kind = exit_reason(self.cfg, w.fee, entry=pos.entry, bid=q.bid, held_s=(t - pos.fill_ts) / 1000.0,
                           secs_left=left)
        if kind is None:
            return
        pos.exit_attempts += 1
        floor = q.bid if kind == "TAKE_PROFIT" else max(0.01, round(q.bid - self.cfg.urgent_cross, 4))
        j = self._at_or_after(w, t + self.one_way_ms())
        if j is not None and w.t[j] < w.close_ts:
            q2 = self._quote(w, j, pos.side)
            if q2.bid is not None and q2.bid >= floor - 1e-9 and q2.bid_depth >= pos.size:
                exit_fee = w.fee.taker_fee_per_share(q2.bid) * pos.size
                pnl = (q2.bid - pos.entry) * pos.size - pos.entry_fee - exit_fee
                self._book(pos, kind, q2.bid, w.t[j], pnl, pos.entry_fee + exit_fee, w)
                return
        pos.next_check = t + self.exec.exit_retry_ms

    def _settle(self, w: Window, pos: _Pos) -> None:
        if w.up_won is None:
            self._book(pos, "UNRESOLVED", None, w.close_ts, None, pos.entry_fee, w)
            return
        px = 1.0 if (pos.side == "UP") == w.up_won else 0.0
        self._book(pos, "SETTLED", px, w.close_ts, (px - pos.entry) * pos.size - pos.entry_fee, pos.entry_fee, w)

    @staticmethod
    def _token_noise(w: Window, i: int, side: str, lookback_ms: float = 30_000.0) -> dict:
        """How jumpy the token we are buying was BEFORE the decision: the range
        of its mid over the lookback, the sd of its 1-second mid changes, and
        its net move over the last 10 s (falling into the entry or not)."""
        t = w.t[i]
        k = bisect.bisect_left(w.t, t - lookback_ms)
        mids, last_s, steps, m10 = [], None, [], None
        for j in range(k, i + 1):
            m = _q(w, j, side)[4]
            if m is None:
                continue
            mids.append(m)
            if m10 is None and w.t[j] >= t - 10_000.0:
                m10 = m
            if last_s is None or w.t[j] - last_s[0] >= 1000.0:
                if last_s is not None:
                    steps.append(m - last_s[1])
                last_s = (w.t[j], m)
        if len(mids) < 2:
            return {"token_range_30s": None, "token_sd_1s": None, "token_move_10s": None}
        return {"token_range_30s": round(max(mids) - min(mids), 4),
                "token_sd_1s": round(statistics.pstdev(steps), 5) if len(steps) >= 2 else None,
                "token_move_10s": round(mids[-1] - m10, 4) if m10 is not None else None}

    def _excursion_to_close(self, w: Window, pos: _Pos) -> tuple[float, float]:
        """Best and worst BID minus entry from the fill to the last exit
        opportunity, whatever the exit did: did the token re-price at all?"""
        hi = lo = 0.0
        k = self._at_or_after(w, pos.fill_ts)
        end = w.close_ts - self.cfg.exit_min_secs_left * 1000.0
        while k is not None and k < len(w.t) and w.t[k] < end:
            b = _q(w, k, pos.side)[0]
            if b is not None:
                hi, lo = max(hi, b - pos.entry), min(lo, b - pos.entry)
            k += 1
        return hi, lo

    def _book(self, pos: _Pos, kind: str, px, ts: float, pnl, fees: float, w: Window) -> None:
        r = pos.rec
        mfe_full, mae_full = self._excursion_to_close(w, pos)
        r.update({"mfe_to_close": round(mfe_full, 4), "mae_to_close": round(mae_full, 4)})
        r.update({"exit_kind": kind, "exit_px": px, "exit_ts": ts, "hold_s": round((ts - pos.fill_ts) / 1000.0, 1),
                  "fees": round(fees, 5), "pnl": None if pnl is None else round(pnl, 5),
                  "pnl_per_share": None if pnl is None else round(pnl / pos.size, 5),
                  "mfe": round(pos.mfe, 4), "mae": round(pos.mae, 4), "target_hit": pos.target_hit_ts is not None,
                  "target_hit_s": None if pos.target_hit_ts is None else round((pos.target_hit_ts - pos.fill_ts) / 1000.0, 1),
                  "exit_attempts": pos.exit_attempts, "won_at_settle": None if w.up_won is None else
                  (pos.side == "UP") == w.up_won})
        self.trades.append(r)

    def _record_market(self, b: str, quotes: dict[str, Quote]) -> None:
        for q in quotes.values():
            if q.ask is None or not q.ask < self.cfg.low_price_threshold:
                continue
            m = self.market[b]
            m["n"] += 1
            m["ask"] += q.ask
            m["depth"] += q.ask_depth
            if q.bid is not None:
                m["n_bid"] += 1
                m["bid"] += q.bid
                m["spread"] += q.ask - q.bid
                m["spread_pct"] += (q.ask - q.bid) / q.ask


# ───────────────────────────── statistics ────────────────────────────────


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def max_drawdown(trades: list[dict]) -> float:
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda r: r["exit_ts"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def epoch_bootstrap_ci(trades: list[dict], n_boot: int = 2000, seed: int = 11) -> tuple[float, float] | None:
    """95 % interval of the mean PnL per trade, resampling whole 5-minute
    epochs (every asset in one epoch is one correlated bet)."""
    by: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        by[t["epoch"]].append(t["pnl"])
    groups = list(by.values())
    if len(groups) < 5:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        s = n = 0.0
        for _ in range(len(groups)):
            g = groups[rng.randrange(len(groups))]
            s += sum(g)
            n += len(g)
        means.append(s / n)
    means.sort()
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot)]


def summarize(trades: list[dict], orders: list[dict] | None = None, opportunities: list[dict] | None = None,
              target: float = 0.05) -> dict:
    orders = orders or []
    opportunities = opportunities or []
    done = [t for t in trades if t.get("pnl") is not None]
    out: dict = {"opportunities": len(opportunities), "orders": len(orders), "fills": len(trades)}
    out["fill_rate_orders"] = round(len(trades) / len(orders), 3) if orders else None
    out["fill_rate_opps"] = round(len(trades) / len(opportunities), 3) if opportunities else None
    if orders:
        out["order_outcomes"] = dict(Counter(o["outcome"] for o in orders))
    src = opportunities or orders
    if src:
        out.update({"avg_ask": _mean([o["ask"] for o in src]), "avg_bid": _mean([o["bid"] for o in src]),
                    "avg_spread": _mean([o["spread"] for o in src]),
                    "avg_spread_pct": _mean([o["spread_pct"] for o in src]),
                    "avg_depth": _mean([o["ask_depth"] for o in src]),
                    "avg_model_edge": _mean([o["model_edge"] for o in src]),
                    "avg_expected_net": _mean([o["expected_net"] for o in src])})
    if not done:
        out["trades"] = 0
        return out
    pnl = [t["pnl"] for t in done]
    per_epoch: dict[int, float] = defaultdict(float)
    for t in done:
        per_epoch[t["epoch"]] += t["pnl"]
    ev = list(per_epoch.values())
    se = statistics.stdev(ev) / math.sqrt(len(ev)) if len(ev) > 1 else float("nan")
    ci = epoch_bootstrap_ci(done)
    out.update({
        "trades": len(done),
        "win_rate": len([x for x in pnl if x > 0]) / len(done),
        "avg_fill": _mean([t["fill_px"] for t in done]),
        "avg_slippage": _mean([t["slippage"] for t in done]),
        "target_hit_rate": len([t for t in done if t["target_hit"]]) / len(done),
        "take_profit_rate": len([t for t in done if t["exit_kind"] == "TAKE_PROFIT"]) / len(done),
        "settle_win_rate": _mean([1.0 if t["won_at_settle"] else 0.0 for t in done]),
        "avg_mfe": _mean([t["mfe"] for t in done]), "avg_mae": _mean([t["mae"] for t in done]),
        "avg_mfe_to_close": _mean([t["mfe_to_close"] for t in done]),
        "avg_mae_to_close": _mean([t["mae_to_close"] for t in done]),
        "reprice_rate": len([t for t in done if t["mfe_to_close"] >= target - 1e-9]) / len(done),
        "avg_hold_s": _mean([t["hold_s"] for t in done]),
        "avg_pnl": statistics.fmean(pnl), "total_pnl": sum(pnl),
        "avg_pnl_per_share": _mean([t["pnl_per_share"] for t in done]),
        "fees": sum(t["fees"] for t in done),
        "max_drawdown": max_drawdown(done),
        "epochs": len(ev),
        "t_epoch": None if len(ev) < 2 or not se or math.isnan(se) else statistics.fmean(ev) / se,
        "ci95_pnl": ci,
        "exit_kinds": dict(Counter(t["exit_kind"] for t in done)),
    })
    return out


def by_bucket(trades, orders, opportunities, key="bucket") -> dict[str, dict]:
    labels = [f"{lo}-{hi}s" for lo, hi in BUCKETS]
    g = lambda rows: {b: [r for r in rows if r[key] == b] for b in labels}  # noqa: E731
    tb, ob, pb = g(trades), g(orders), g(opportunities)
    return {b: summarize(tb[b], ob[b], pb[b]) for b in labels}
