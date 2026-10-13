"""STRATEGY B -- the pair-lock v2 engine (the conditional dynamic hedge, EV-based) EXACTLY as it ran on the paper bot
from 2026-10-02 14:19 UTC, restored when the user chose strategy B after the recovery-hedge replay
(docs/pairlock_recovery_2026-10-02.md). It runs on the frozen v2 decision code (strategy/pairlock_v2.py,
strategy/pairlock_book_v2.py): same states, same MIN_HEDGE_EV rule, same re-entry after a fully matched pair.
Selected by TPB_PAIRLOCK_STRATEGY=B (or a v2 hedge mode, see live.py). Do not edit the decision logic here: the
current strategy is strategy/pairlock_engine.py.

The original v2 docstring follows.

The conditional dynamic hedge / pair-lock layer (v2, 2026-10-02) on top of the triangle engine
(TPB_TRIANGLE_HEDGE_MODE=dynamic). Paper only: the triangle strategy has no real-money switch.

The triangle engine still produces the entry TRIGGER (the spot shock) with its own freshness gates. This layer:
  * keeps CYCLES per market, each with an authoritative PairBook built from the venue's FILLS (order id, fill id,
    price, fee, expected price); pending orders are never inventory;
  * moves every cycle through validated states (pairlock_book.TRANSITIONS):
        NO_POSITION -> INITIAL_ENTRY_CANDIDATE -> INITIAL_ENTRY -> MONITORING <-> ADVERSE_MOVE -> HEDGE_EVALUATION
        -> NO_HEDGE | PARTIAL_HEDGE | FULL_HEDGE -> PAIR_FORMED -> PAIR_LOCKED -> SETTLEMENT_PENDING -> SETTLED
    and logs each transition with the position's numbers;
  * gates entries by expected NET value at the executable VWAP (pairlock.entry_decision);
  * evaluates hedges (pairlock.hedge_step -> pairlock_book.evaluate_hedges) on the LIVE ask levels of the opposite
    token: every candidate size of the UNMATCHED exposure, hedge_EV = P(fill) x (E[P&L after] - E[P&L without]);
  * locks a pair only when it is fully matched, nothing is pending and its NET edge / locked P&L clear the minimums;
  * at settlement reconciles its own P&L with the bot's (the paper venue's) and halts new entries on a mismatch.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field, fields, replace

from ..types import Order, Side, TimeInForce
from . import pairlock_book_v2 as pb
from . import pairlock_v2 as pl
from .engine import Evaluation, Reason, SideView
from .pairlock_booksignal import BookTrigger, BookTriggerParams
from .triangle import STRATEGY_ID, TriangleEngine

log = logging.getLogger(__name__)

ENV_PREFIX = "TPB_PAIRLOCK_"
RECON_TOL = 0.01                     # USDC: engine P&L vs the venue's, per market


@dataclass
class PairLockConfig:
    """The layer's settings: pairlock.PairLockParams (TPB_PAIRLOCK_<FIELD>) and pairlock.RiskLimits
    (TPB_PAIRLOCK_LIM_<FIELD>), plus the model file."""
    model_path: str = "data/replay/pairlock2_model.json"
    entry_gate: bool = True
    params: pl.PairLockParams = field(default_factory=pl.PairLockParams)
    limits: pl.RiskLimits = field(default_factory=pl.RiskLimits)
    #: the entry trigger's source (TPB_PAIRLOCK_SIGNAL_SOURCE; default crypto = B unchanged), see pairlock_booksignal
    book: BookTriggerParams = field(default_factory=BookTriggerParams)

    @classmethod
    def from_env(cls, env: dict | None = None) -> "PairLockConfig":
        env = os.environ if env is None else env

        def parse(dc, prefix):
            kw = {}
            for f in fields(dc):
                raw = (env.get(prefix + f.name.upper()) or "").strip()
                if not raw:
                    continue
                cur = getattr(dc, f.name)
                try:
                    if isinstance(cur, bool):
                        kw[f.name] = raw.lower() not in ("0", "false", "no", "off")
                    elif isinstance(cur, int):
                        kw[f.name] = int(float(raw))
                    elif isinstance(cur, float):
                        kw[f.name] = float(raw)
                    elif isinstance(cur, tuple):
                        kw[f.name] = tuple(float(x) for x in raw.split(",") if x.strip())
                    else:
                        kw[f.name] = raw
                except ValueError:
                    log.warning("PAIRLOCK ignoring %s%s=%r", prefix, f.name.upper(), raw)
            return replace(dc, **kw)

        return cls(model_path=(env.get(ENV_PREFIX + "MODEL_PATH") or cls.model_path).strip(),
                   entry_gate=(env.get(ENV_PREFIX + "ENTRY_GATE") or "1").strip().lower() not in ("0", "false", "no"),
                   params=parse(pl.PairLockParams(), ENV_PREFIX),
                   limits=parse(pl.RiskLimits(), ENV_PREFIX + "LIM_"),
                   book=parse(BookTriggerParams(), ENV_PREFIX))


def _levels(levels) -> list[tuple[float, float]]:
    return [(float(lv.price), float(lv.size)) for lv in levels or ()]


def _depth(levels, best, ask: bool) -> float:
    if best is None:
        return 0.0
    if ask:
        return sum(lv.size for lv in levels if lv.price <= best + 0.02 + 1e-9)
    return sum(lv.size for lv in levels if lv.price >= best - 0.02 - 1e-9)


EVENT = {"INITIAL_ENTRY": "INITIAL_ENTRY", "ADVERSE_MOVE": "ADVERSE_MOVE", "HEDGE_EVALUATION": "HEDGE_EVALUATION",
         "NO_HEDGE": "HEDGE_REJECTED", "PARTIAL_HEDGE": "PARTIAL_HEDGE", "FULL_HEDGE": "FULL_HEDGE",
         "PAIR_FORMED": "PAIR_FORMED", "PAIR_LOCKED": "PAIR_LOCKED", "SETTLEMENT_PENDING": "SETTLEMENT_PENDING",
         "SETTLED": "SETTLED", "MONITORING": "MONITORING", "NO_POSITION": "ENTRY_NOT_FILLED",
         "INITIAL_ENTRY_CANDIDATE": "INITIAL_ENTRY_CANDIDATE"}


class PairLockEngineV2(TriangleEngine):
    """TriangleEngine + the conditional dynamic hedge / pair-lock layer (v2) = STRATEGY B."""

    #: a separate bot: the shared control file's triangle.* values (set for the triangle bot) do not steer it
    isolated_controls = True
    #: which strategy of the 2026-10-02 replay this engine is (logged at start)
    strategy_label = "B"

    def __init__(self, cfg, scfg, risk, features=None, plcfg: PairLockConfig | None = None) -> None:
        super().__init__(cfg, scfg, risk, features)
        self.plcfg = plcfg or PairLockConfig.from_env()
        self.prm, self.lim = self.plcfg.params, self.plcfg.limits
        try:
            self.plmodel = pl.ProbModel.load(self.plcfg.model_path)
            log.info("PAIRLOCK model %s (version %s, fitted on %s)", self.plcfg.model_path,
                     self.plmodel.meta.get("version"), self.plmodel.meta.get("fitted_on"))
        except (OSError, ValueError, KeyError) as exc:
            self.plmodel = pl.ProbModel.market_only()
            log.error("PAIRLOCK model not loaded from %s (%s): the market price is the probability (no edge)",
                      self.plcfg.model_path, exc)
        self.cycles: dict[str, list[pl.Cycle]] = {}
        self.settled_cycles: deque = deque(maxlen=60)
        self.candidates: dict[str, pl.Cycle] = {}
        self.hist: dict[str, pl.History] = {}
        self.spot_hist: dict[str, pl.History] = {}
        self.sigma: dict[str, float] = {}
        self.close_ts: dict[str, float] = {}
        self.last_p: dict[str, float] = {}
        self.last_bids: dict[str, tuple] = {}
        #: the order in flight per market: {"oid", "kind", "cycle", "q", "limit", "done", "filled_qty", "expected"}
        self.pending: dict[str, dict] = {}
        self._logged: dict[int, int] = {}
        self.realised = 0.0
        self.accounting_halt = ""
        self.stats = {"entries": 0, "entry_fills": 0, "entry_kills": 0, "entries_refused": 0, "hedges": 0,
                      "hedge_fills": 0, "hedge_kills": 0, "evaluations": 0, "hedge_rejected": 0, "pair_formed": 0,
                      "locks": 0, "settled_cycles": 0, "naked_settled": 0, "pnl": 0.0, "prediction": 0.0,
                      "hedging": 0.0, "pairs": 0.0, "directional": 0.0, "fees": 0.0, "slippage": 0.0,
                      "recon_checked": 0, "recon_max_diff": 0.0, "sigma_fallbacks": 0}
        self.stats.update({"trigger_crypto": 0, "trigger_book": 0, "trigger_both": 0, "crypto_ignored": 0})
        self.refused_why: dict[str, int] = {}
        self.decisions: deque = deque(maxlen=40)
        #: the Polymarket-book entry trigger (None in the default crypto mode: B exactly as it ran)
        self.booksig: BookTrigger | None = None
        if self.plcfg.book.signal_source != "crypto":
            self.booksig = BookTrigger(self.plcfg.book)          # a missing model file raises: no silent crypto mode
            log.info("PAIRLOCK BOOK TRIGGER source=%s threshold=%.2fc confirm=%.2fc/%.0fms max_age=%.0fms model=%s (%s)",
                     self.plcfg.book.signal_source, self.plcfg.book.book_threshold, self.plcfg.book.book_confirm,
                     self.plcfg.book.book_confirm_ms, self.plcfg.book.book_max_age_ms, self.plcfg.book.book_model_path,
                     self.booksig.model.meta.get("fitted_to"))
        log.info("PAIRLOCK STRATEGY B (v2 conditional EV hedge, strategy/pairlock_engine_v2.py)")
        log.info("PAIRLOCK_CONFIG gate=%s %s | limits %s", self.plcfg.entry_gate,
                 " ".join(f"{k}={v}" for k, v in sorted(vars(self.prm).items()) if k != "fractions"),
                 " ".join(f"{k}={v}" for k, v in sorted(vars(self.lim).items())))

    # ───────────────────────────── state helpers ─────────────────────────────

    def _cycle(self, slug: str) -> pl.Cycle | None:
        cs = self.cycles.get(slug)
        return cs[-1] if cs else None

    def _exposure(self, slug: str) -> pl.MarketExposure:
        cs = [c for c in self.cycles.get(slug, []) if c.book.fills or c.book.pending]
        p = self.pending.get(slug)
        cap = sum(c.book.cash_spent for c in cs)
        sh = sum(c.book.up_qty + c.book.down_qty for c in cs)
        if p is not None and not p.get("done"):
            cap += p.get("q", 0.0) * (p.get("limit", 0.0) + pb.fee_per_share(p.get("limit", 0.0)))
            sh += p.get("q", 0.0)
        return pl.MarketExposure(capital=cap, shares=sh, worst_case=sum(c.book.worst_case_pnl for c in cs if c.book.fills),
                                 cycles=len(self.cycles.get(slug, [])))

    #: a rejected evaluation (HEDGE_EVALUATION -> NO_HEDGE -> back) repeating for the same reason is logged at most
    #: this often; the transitions themselves are all recorded on the cycle
    REPEAT_LOG_MS = 30_000.0

    def _log_cycle(self, slug: str, cyc: pl.Cycle, extra: str = "") -> None:
        """One line per new transition (brief item 42), with the position's numbers at that moment. A rejected
        evaluation that repeats for the same reason within REPEAT_LOG_MS is counted, not printed."""
        k = id(cyc)
        start = self._logged.get(k, 0)
        b = cyc.book
        ub, db = self.last_bids.get(slug, (None, None))
        p = self.last_p.get(slug)
        new = cyc.transitions[start:]
        evals = [tr for tr in new if tr.to == "HEDGE_EVALUATION"]
        rejected_only = bool(new) and evals and all(tr.to in ("HEDGE_EVALUATION", "NO_HEDGE") or tr.frm == "NO_HEDGE"
                                                     for tr in new)
        if rejected_only:
            key = (k, evals[-1].reason)
            last = getattr(self, "_repeat_log", {}).get(key)
            if last is not None and new[-1].t - last[0] < self.REPEAT_LOG_MS:
                self._repeat_log[key] = (last[0], last[1] + 1)
                self._logged[k] = len(cyc.transitions)
                return
            n_sup = 0 if last is None else last[1]
            self.__dict__.setdefault("_repeat_log", {})[key] = (new[-1].t, 0)
            if n_sup:
                extra = f"{extra} | {n_sup} similar rejected evaluations since the last line"
        for tr in new:
            ev = EVENT.get(tr.to, tr.to)
            log.info("[PAIRLOCK] %s %s | %s -> %s | UP %g DOWN %g matched %g unmatched UP %g DOWN %g | avg UP %s DOWN %s "
                     "| pair cost %s edge %s net %s locked %+.3f | fees %.3f slip %+.3f | expected %s worst %+.3f "
                     "MTM %s | capital %.2f | %s%s", ev, slug, tr.frm, tr.to, b.up_qty, b.down_qty, b.matched_qty,
                     b.unmatched_up, b.unmatched_down, _f3(b.up_avg_cost), _f3(b.down_avg_cost),
                     _f3(b.pair_cost_per_share), _f3(b.pair_edge), _f3(b.net_pair_edge), b.locked_pair_pnl, b.fees,
                     b.slippage, "-" if p is None else f"{b.expected_settlement_pnl(p):+.3f}", b.worst_case_pnl,
                     "-" if ub is None and db is None else f"{b.mark_to_market_pnl(ub, db):+.3f}", b.cash_spent,
                     tr.reason, extra)
        self._logged[k] = len(cyc.transitions)

    def _record(self, meta, book_up, book_down, spot, now: float) -> None:
        m = meta.market
        price = getattr(spot, "price", None) if spot is not None else None
        srcs = getattr(spot, "sources", None) if spot is not None else None
        self.close_ts[m.slug] = m.close_ts
        sh = self.spot_hist.setdefault(m.asset, pl.History(keep_ms=420_000.0))
        if price and (not sh.t or now - sh.t[-1] >= 200.0):
            sh.append(now, None, None, None, None, 0.0, 0.0, price)
        h = self.hist.setdefault(m.slug, pl.History(keep_ms=15_000.0))
        ub = None if book_up is None else book_up.best_bid
        db = None if book_down is None else book_down.best_bid
        self.last_bids[m.slug] = (ub, db)
        if h.t and now - h.t[-1] < 200.0:
            return
        ua = None if book_up is None else book_up.best_ask
        da = None if book_down is None else book_down.best_ask
        h.append(now, ub, ua, db, da, 0.0 if book_up is None else _depth(book_up.bids, ub, False),
                 0.0 if book_up is None else _depth(book_up.asks, ua, True), price, srcs)

    def _sigma(self, m, now: float) -> float | None:
        s = self.sigma.get(m.slug)
        if s is not None:
            return s
        if now < m.open_ts:
            return None
        s = pl.realized_sigma(self.spot_hist.get(m.asset, pl.History()), m.open_ts, 300.0)
        if s is None:
            s = self.last_shock_sigma.get(m.asset)
            if not s:
                return None
            self.stats["sigma_fallbacks"] += 1
            log.info("PAIRLOCK %s: no 300 s of spot before the open (restart?): sigma from the shock model %.2e",
                     m.slug, s)
        self.sigma[m.slug] = s
        return s

    def _features(self, meta, now: float) -> dict | None:
        m = meta.market
        h = self.hist.get(m.slug)
        sig = self._sigma(m, now)
        if h is None or len(h) == 0 or sig is None or m.strike <= 0.0:
            return None
        f = pl.features(h, len(h) - 1, m.strike, sig, m.close_ts)
        if f is not None:
            self.last_p[m.slug] = self.plmodel.p_up(f)
        return f

    @staticmethod
    def _reason_for(why: str) -> Reason:
        if why.startswith("entry EV") or why.startswith("P(favourable"):
            return Reason.EDGE_TOO_SMALL
        if why in ("spread", "depth", "no ask"):
            return Reason.LOW_LIQUIDITY if why != "no ask" else Reason.NO_OFFER
        if "outside" in why:
            return Reason.PRICE_BAND
        if why.endswith("s left"):
            return Reason.OUTSIDE_TIME_WINDOW
        if why.startswith("market ") or "venue minimum" in why:
            return Reason.EXPOSURE_CAP
        return Reason.ALREADY_POSITIONED

    def _refuse_pl(self, ev: Evaluation, why: str, reason: Reason = Reason.EDGE_TOO_SMALL) -> Evaluation:
        key = why.split(":")[0][:40]
        self.refused_why[key] = self.refused_why.get(key, 0) + 1
        f = (ev.features or {}).get(STRATEGY_ID) or {}
        mk = self._mark_of(ev.slug, f.get("side", ""), f.get("t"))
        self._hollow(mk, f"not bought: {why}")
        ev.side = ev.token_id = None
        return self._reject(ev, reason, why)

    def _finalize(self, slug: str, now: float) -> None:
        """An order whose final result is in (and whose fills have all been applied): close its pending entry."""
        p = self.pending.get(slug)
        if p is None or not p.get("done"):
            return
        self.pending.pop(slug, None)
        cyc = p["cycle"]
        filled = p.get("filled_qty", 0.0) > 1e-9
        pl.on_order_done(cyc, p["oid"], filled, now, p.get("why", ""), prm=self.prm)
        if p["kind"] == "entry" and not filled:
            cs = self.cycles.get(slug, [])
            if cyc in cs and not cyc.book.fills:
                cs.remove(cyc)
        if cyc.state is pb.State.PAIR_FORMED:
            pl.try_lock(cyc, self.prm, now)
        self._log_cycle(slug, cyc)

    # ───────────────────────────── the evaluation ─────────────────────────────

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        m = meta.market
        self._record(meta, book_up, book_down, spot, now)
        self._finalize(m.slug, now)
        cyc0 = self._cycle(m.slug)
        if (cyc0 is not None and cyc0.state in (pb.State.PARTIAL_HEDGE, pb.State.FULL_HEDGE)
                and m.slug not in self.pending):
            cyc0.to(cyc0.watch_state(), now, "hedge decision never sent")      # a decision the bot did not send
            self._log_cycle(m.slug, cyc0)
        cand = self.candidates.pop(m.slug, None)
        if cand is not None:                                                 # a candidate the bot did not send
            cand.to(pb.State.NO_POSITION, now, "entry not sent")
        ev = super().evaluate(meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                              balance, orderflow, quote_ages)
        cyc = self._cycle(m.slug)
        if cyc is not None and cyc.state in pb.HEDGE_WATCH_STATES and m.slug not in self.pending:
            hev = self._hedge(meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc)
            if hev is not None:
                if ev.tradeable:
                    self._refuse_pl(ev, f"a hedge goes first in this cycle ({cyc.state.value})", Reason.ALREADY_POSITIONED)
                return hev
        if self.booksig is not None:
            ev = self._book_trigger(ev, meta, book_up, book_down, now)
        if not ev.tradeable:
            return ev
        if self.accounting_halt:
            return self._refuse_pl(ev, f"accounting halt: {self.accounting_halt}", Reason.RISK_LIMIT)
        res = self._gate_entry(ev, meta, book_up, book_down, now)
        if res.tradeable and self.booksig is not None:
            tri = res.features.get(STRATEGY_ID) or {}
            self.stats["trigger_" + str(tri.get("source") or "crypto")] += 1
            if tri.get("source") == "book":
                res.net_edge = float((tri.get("pairlock") or {}).get("ev") or 0.0)   # the gate's EV, for the logs
        return res

    def _book_trigger(self, ev: Evaluation, meta, book_up, book_down, now: float) -> Evaluation:
        """Apply the signal source to this evaluation: keep the crypto shock, replace it with the book's signal
        (a new evaluation through the same pair-lock gate), or refuse it. A refusal BEFORE the signal stage
        (stale spot, no strike, outside the time window: no "spot_lag" block) stands whatever the book says."""
        m = meta.market
        if "spot_lag" not in (ev.features or {}):
            return ev
        crypto_dir = (1 if ev.side == "UP" else -1) if ev.tradeable else None
        src = self.plcfg.book.signal_source
        pick = self.booksig.take(m.slug, now, crypto_dir)
        if pick is None:
            if ev.tradeable:
                self.stats["crypto_ignored"] += 1
                why = (f"crypto trigger not used: signal source {src}"
                       + (" (the book did not confirm the side)" if src == "both" else ""))
                return self._refuse_pl(ev, why, Reason.WEAK_MOMENTUM)
            return ev
        d, trigger, score = pick
        if trigger in ("crypto", "both"):
            info = ev.features.get(STRATEGY_ID) or {}
            info.update({"source": trigger, "book": None if score is None else round(score, 3)})
            return ev
        side = "UP" if d > 0 else "DOWN"
        mark = {"t": round(now), "dir": d, "z1": None, "z3": None, "r1_bps": None, "trigger": "book", "ok": True,
                "why": "", "book": round(score, 3)}
        self.shock_marks.setdefault(m.slug, deque(maxlen=self.SHOCK_MARKS_PER_MARKET)).append(mark)
        self.triangles += 1
        new = Evaluation(slug=m.slug, asset=m.asset, seconds_left=ev.seconds_left, reason=None)
        new.p_analytic, new.p_market = ev.p_analytic, ev.p_market
        new.spot_move_bps, new.market_reprice = ev.spot_move_bps, ev.market_reprice
        info = {"signal": True, "direction": side, "side": side, "t": mark["t"], "trigger": "book", "source": "book",
                "book": round(score, 3), "z1": None,
                "spot_lag_verdict": ev.reason.value if ev.reason is not None else "ORDER"}
        new.features = {**(ev.features or {}), STRATEGY_ID: info}
        book = book_up if side == "UP" else book_down
        token = m.yes_token_id if side == "UP" else m.no_token_id
        if book is None or book.best_ask is None:
            return self._refuse_pl(new, f"no {side} ask", Reason.NO_OFFER)
        ask, bid = book.best_ask, book.best_bid
        sc, tick = self.scfg, (m.tick_size or 0.01)
        if ask < sc.min_price - 1e-9 or ask > sc.max_price + 1e-9:
            return self._refuse_pl(new, f"ask {ask:.2f} outside {sc.min_price:.2f}-{sc.max_price:.2f}", Reason.PRICE_BAND)
        limit = min(1.0 - tick, sc.max_price, round(ask + max(sc.limit_offset, 0.0), 4))
        info.update({"ask": ask, "bid": bid, "limit": limit})
        p_up = ev.p_analytic if ev.p_analytic is not None else ev.p_market
        p_side = None if p_up is None else (p_up if side == "UP" else 1.0 - p_up)
        new.features["execution"] = {"fair": None if p_side is None else round(p_side, 4), "cap": round(limit, 4),
                                     "best_ask": ask, "best_bid": bid,
                                     "spread": None if bid is None else round(ask - bid, 4)}
        depth = sum(lv.size for lv in book.asks if lv.price <= limit + 1e-9)
        new.sides = [SideView(side=side, token_id=token, p_model=p_side if p_side is not None else ask, p_market=ask,
                              ask=ask, bid=bid, depth_at_limit=depth, costs=None, reason=None, entry_px=ask)]
        new.side, new.token_id, new.limit_price, new.expected_price = side, token, limit, ask
        new.max_entry_price = limit
        new.p_used = p_up
        new.size, new.net_edge, new.confidence = float(sc.shares), 0.0, 1.0
        new.size_note = f"book trigger {score:+.2f}c"
        self.last[m.slug] = new
        return new

    def _gate_entry(self, ev: Evaluation, meta, book_up, book_down, now: float) -> Evaluation:
        m = meta.market
        side = ev.side
        book = book_up if side == "UP" else book_down
        ask, bid = book.best_ask, book.best_bid
        f = self._features(meta, now)
        if f is None:
            return self._refuse_pl(ev, "pairlock features not ready (history / sigma)", Reason.MODEL_NOT_READY)
        prm = self.prm if self.plcfg.entry_gate else replace(self.prm, entry_edge=-1.0)
        prev = self._cycle(m.slug)
        if m.slug in self.pending:
            return self._refuse_pl(ev, "an order is in flight in this market", Reason.ALREADY_POSITIONED)
        dec, why = pl.entry_decision(f, side, ask, bid, _levels(book.asks), self.plmodel, prm, self.lim,
                                     self._exposure(m.slug), m.seconds_remaining(now), prev)
        tri = ev.features.get(STRATEGY_ID) or {}
        if dec is None:
            self.stats["entries_refused"] += 1
            return self._refuse_pl(ev, why, self._reason_for(why))
        size = dec.shares
        limit = min(ev.limit_price, dec.limit)
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit, add_to_open=True)
        if not verdict.ok:
            return self._refuse_pl(ev, f"risk: {verdict.detail}", Reason(verdict.reason))
        cyc = pl.Cycle(book=pb.PairBook(m.slug), a_side=side, started_t=now)
        cyc.to(pb.State.INITIAL_ENTRY_CANDIDATE, now,
               f"{side} P(win) {dec.p:.3f} EV {dec.ev:+.3f}/sh at VWAP {dec.info['vwap']:.3f}"
               + ("" if dec.info.get("p_fav") is None else f", P(favourable {self.plmodel.short.get('horizon_s', 0):g} s) "
                                                          f"{dec.info['p_fav']:.2f}"))
        self.candidates[m.slug] = cyc
        ev.size, ev.limit_price, ev.max_entry_price = size, limit, limit
        tri.update({"size": size, "limit": limit, "pairlock": {"kind": "entry", "p_win": round(dec.p, 4),
                                                               "ev": round(dec.ev, 4), "vwap": round(dec.info["vwap"], 4),
                                                               "p_fav": dec.info.get("p_fav")}})
        ev.size_note = f"{size:g} sh, EV {dec.ev:+.3f}/sh (P {dec.p:.3f} at VWAP {dec.info['vwap']:.3f})"
        self.decisions.append({"t": round(now), "slug": m.slug, "action": "entry", "side": side, "ask": ask,
                               "p": round(dec.p, 4), "ev": round(dec.ev, 4)})
        return ev

    def _hedge(self, meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc: pl.Cycle) -> Evaluation | None:
        m = meta.market
        if spot_age_ms is None or spot_age_ms > self.lim.max_spot_age_ms:
            return None
        heavy = cyc.book.heavy_side
        if heavy is None:
            return None
        light = "DOWN" if heavy == "UP" else "UP"
        book_l = book_up if light == "UP" else book_down
        if book_up is None or book_down is None or book_l is None:
            return None
        tok_l = m.yes_token_id if light == "UP" else m.no_token_id
        qa = (quote_ages or {}).get(tok_l)
        if qa is not None and qa > self.lim.max_quote_age_ms:
            return None
        f = self._features(meta, now)
        if f is None:
            return None
        n0 = len(cyc.transitions)
        dec, why, rec = pl.hedge_step(cyc, f, book_up.best_bid, book_down.best_bid, book_l.best_ask, book_l.best_bid,
                                      _levels(book_l.asks), now, m.seconds_remaining(now), self.plmodel, self.prm,
                                      self.lim, self._exposure(m.slug))
        if rec is not None:
            self.stats["evaluations"] += 1
            if rec.get("decision") == "NO_HEDGE":
                self.stats["hedge_rejected"] += 1
        if len(cyc.transitions) != n0:
            best = None if rec is None else max((c for c in rec["candidates"] if c["feasible"]),
                                                key=lambda c: c["hedge_ev"], default=None)
            self._log_cycle(m.slug, cyc, "" if best is None else
                            f" | best candidate {best['label']} {best['q']:g} sh VWAP {best['vwap']} hedge_EV "
                            f"{best['hedge_ev']:+.4f}")
        if dec is None:
            return None
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=m.seconds_remaining(now), reason=None)
        info = {"signal": True, "direction": light, "side": light, "t": round(now), "trigger": "hedge",
                "size": dec.shares, "ask": book_l.best_ask, "bid": book_l.best_bid, "limit": dec.limit,
                "pairlock": {"kind": dec.kind, "state": cyc.state.value, "q": dec.shares,
                             **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in dec.info.items()}}}
        ev.features = {STRATEGY_ID: info,
                       "execution": {"best_ask": book_l.best_ask, "best_bid": book_l.best_bid, "cap": dec.limit,
                                     "spread": None if book_l.best_bid is None else round(book_l.best_ask - book_l.best_bid, 4)}}
        ev.sides = [SideView(side=light, token_id=tok_l, p_model=dec.p, p_market=book_l.best_ask, ask=book_l.best_ask,
                             bid=book_l.best_bid, depth_at_limit=_depth(book_l.asks, book_l.best_ask, True), costs=None,
                             reason=None, entry_px=book_l.best_ask)]
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = light, tok_l, dec.limit, dec.info.get("vwap") or book_l.best_ask
        ev.max_entry_price = dec.limit
        ev.size, ev.net_edge, ev.confidence = dec.shares, dec.ev / max(dec.shares, 1e-9), 1.0
        ev.p_used = dec.p if light == "UP" else 1.0 - dec.p
        ev.size_note = (f"{dec.kind} {dec.shares:g} sh of {light} (unmatched {cyc.book.unmatched:g}), VWAP "
                        f"{dec.info.get('vwap', 0):.3f}, hedge_EV {dec.ev:+.4f}")
        mk = {"t": round(now), "dir": 1 if light == "UP" else -1, "z1": None, "z3": None, "r1_bps": None,
              "trigger": "hedge", "ok": True, "why": "", "hedge": dict(info["pairlock"])}
        self.shock_marks.setdefault(m.slug, deque(maxlen=self.SHOCK_MARKS_PER_MARKET)).append(mk)
        self.decisions.append({"t": round(now), "slug": m.slug, "action": "hedge", "side": light, "kind": dec.kind,
                               "q": dec.shares, "hedge_ev": round(dec.ev, 4),
                               "pair_cost_after": dec.info.get("pair_cost_after")})
        self.last[m.slug] = ev
        return ev

    @staticmethod
    def _is_hedge(ev: Evaluation) -> bool:
        return ((ev.features or {}).get(STRATEGY_ID) or {}).get("trigger") == "hedge"

    def revalidate(self, ev: Evaluation, book, min_edge: float | None = None) -> Reason | None:
        if self._is_hedge(ev):
            return Reason.NO_OFFER if book is None or book.best_ask is None else None
        return super().revalidate(ev, book, min_edge)

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        now = now_ms or 0.0
        if self._is_hedge(ev):
            f = ev.features[STRATEGY_ID]
            o = Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size, tif=TimeInForce.FOK,
                      expected_price=ev.expected_price,
                      tag=f"{ev.side} pairlock {f['pairlock']['kind']} q={ev.size:g} vwap={f['pairlock'].get('vwap')}")
            cyc = self._cycle(ev.slug)
            pl.on_hedge_sent(cyc, o.order_id, ev.side, ev.size, ev.limit_price, now)
            self.pending[ev.slug] = {"oid": o.order_id, "kind": "hedge", "cycle": cyc, "q": ev.size,
                                     "limit": ev.limit_price, "done": False, "filled_qty": 0.0,
                                     "expected": ev.expected_price}
            self.stats["hedges"] += 1
            return o
        o = super().to_order(ev, now_ms)
        cyc = self.candidates.pop(ev.slug, None) or pl.Cycle(book=pb.PairBook(ev.slug), a_side=ev.side, started_t=now)
        if cyc.state is pb.State.NO_POSITION:
            cyc.to(pb.State.INITIAL_ENTRY_CANDIDATE, now, "entry")
        self.cycles.setdefault(ev.slug, []).append(cyc)
        pl.on_entry_sent(cyc, o.order_id, ev.size, ev.limit_price, now)
        self.pending[ev.slug] = {"oid": o.order_id, "kind": "entry", "cycle": cyc, "q": ev.size,
                                 "limit": ev.limit_price, "done": False, "filled_qty": 0.0,
                                 "expected": ev.expected_price}
        self.stats["entries"] += 1
        self._log_cycle(ev.slug, cyc)
        return o

    def signal_text(self, ev: Evaluation) -> str:
        f = (ev.features or {}).get(STRATEGY_ID, {})
        if self._is_hedge(ev):
            pk = f.get("pairlock") or {}
            return (f"PAIRLOCK {pk.get('kind')} {ev.side} {ev.size:g} sh at VWAP {pk.get('vwap')} (limit {ev.limit_price}); "
                    f"P(hedge side) {pk.get('p_b', 0):.3f}, hedge_EV {pk.get('hedge_ev', 0):+.4f}, pair cost after "
                    f"{pk.get('pair_cost_after')}, reversal {pk.get('score', 0):+.2f}, state {pk.get('state')}")
        txt = super().signal_text(ev)
        pk = f.get("pairlock")
        if pk:
            txt += f"; PAIRLOCK entry EV {pk.get('ev', 0):+.3f}/sh P(win) {pk.get('p_win', 0):.3f} VWAP {pk.get('vwap')}"
        return txt

    # ───────────────────────────── results ─────────────────────────────

    def pair_order(self, market, side, shares, avg_px, now_ms, other_ask=None):
        return None                       # hedges are decided by the layer, never placed automatically

    def entry_result(self, order_id: str, filled: bool, why: str = "") -> None:
        """The order's final result. Its fills (if any) are applied right after this call (on_fill_record); the
        pending entry is closed once they are (on the next evaluation, or at once when the order filled in full)."""
        super().entry_result(order_id, filled, why)
        for slug, p in list(self.pending.items()):
            if p["oid"] != order_id:
                continue
            p["done"] = True
            p["why"] = why
            if p["kind"] == "entry":
                self.stats["entry_fills" if filled else "entry_kills"] += 1
            else:
                self.stats["hedge_fills" if filled else "hedge_kills"] += 1
                if not filled:
                    log.warning("[PAIRLOCK] HEDGE_NOT_FILLED %s (%s): an incomplete hedge, %g sh still unmatched", slug,
                                why, p["cycle"].book.unmatched)
                    for mk in reversed(self.shock_marks.get(slug, ())):
                        if mk.get("trigger") == "hedge" and mk.get("ok"):
                            mk["ok"], mk["why"] = False, f"hedge not filled: {why}"
                            break
            if not filled:
                self._finalize(slug, p["cycle"].last_action_t if p["kind"] == "hedge" else time.time() * 1000.0)
            return

    def entry_refused(self, ev: Evaluation, why: str) -> None:
        """The bot did not send this evaluation's order (guard, freshest book, kill switch, pre-send check)."""
        if self._is_hedge(ev):
            cyc = self._cycle(ev.slug)
            p = self.pending.get(ev.slug)
            if p is not None and p["kind"] == "hedge" and not p.get("done"):
                self.pending.pop(ev.slug, None)                # to_order ran, but the order never left
                cyc.book.resolve_pending(p["oid"])
                cyc.hedge_actions = max(0, cyc.hedge_actions - 1)
            if cyc is not None and cyc.state in (pb.State.PARTIAL_HEDGE, pb.State.FULL_HEDGE):
                cyc.to(cyc.watch_state(), cyc.last_eval_t, f"hedge not sent: {why}")
                self._log_cycle(ev.slug, cyc)
            for mk in reversed(self.shock_marks.get(ev.slug, ())):
                if mk.get("trigger") == "hedge" and mk.get("ok"):
                    mk["ok"], mk["why"] = False, f"hedge not sent: {why}"
                    break
            return
        cand = self.candidates.pop(ev.slug, None)
        if cand is not None:
            cand.to(pb.State.NO_POSITION, cand.started_t, f"entry not sent: {why}")
        p = self.pending.get(ev.slug)
        if p is not None and p["kind"] == "entry" and not p.get("done"):
            self.pending.pop(ev.slug, None)
            cs = self.cycles.get(ev.slug, [])
            if p["cycle"] in cs and not p["cycle"].book.fills:
                cs.remove(p["cycle"])
        super().entry_refused(ev, why)

    def on_fill_record(self, slug: str, order, fill, now: float) -> None:
        """Every venue fill of this engine's orders, one by one (live._on_result): the authoritative inventory."""
        p = self.pending.get(slug)
        if p is None or p["oid"] != order.order_id:
            log.error("[PAIRLOCK] %s: a fill of %s with no matching order in flight (%g sh @ %.3f): NOT tracked -- "
                      "accounting halt", slug, order.order_id, fill.size, fill.price)
            self.accounting_halt = f"untracked fill {order.order_id}"
            return
        cyc = p["cycle"]
        side = cyc.a_side if p["kind"] == "entry" else ("DOWN" if cyc.book.heavy_side == "UP" else "UP")
        n = sum(1 for f in cyc.book.fills if f.order_id == order.order_id) + 1
        rec = pb.FillRecord(market_id=slug, ts=now, token=order.token_id, side=side, qty=float(fill.size),
                            price=float(fill.price), fee=float(fill.fee), order_id=order.order_id,
                            fill_id=f"{order.order_id}-{n}", action="BUY",
                            expected_price=p.get("expected"), kind=p["kind"])
        pl.on_fill(cyc, rec, self.prm, now)
        p["filled_qty"] = p.get("filled_qty", 0.0) + float(fill.size)
        if p["kind"] == "hedge":
            log.info("[PAIRLOCK] HEDGE_FILLED %s %g %s @ %.3f (expected %s, fee %.4f) -> UP %g DOWN %g matched %g "
                     "unmatched %g", slug, fill.size, side, fill.price, p.get("expected"), fill.fee, cyc.book.up_qty,
                     cyc.book.down_qty, cyc.book.matched_qty, cyc.book.unmatched)
        if p["filled_qty"] + 1e-9 >= p["q"]:
            p["done"] = True
            self._finalize(slug, now)
        else:
            self._log_cycle(slug, cyc)

    def observe_point(self, slug: str, market, point: dict, now: float) -> None:
        """Every 200 ms sample (live._sample_prices): the book trigger scores it; a closed market's cycles wait
        for the outcome."""
        super().observe_point(slug, market, point, now)
        if self.booksig is not None and now < market.close_ts:
            self.booksig.observe(slug, market, point, now)
        if now < market.close_ts:
            return
        for cyc in self.cycles.get(slug, []):
            if cyc.state not in (pb.State.SETTLEMENT_PENDING, pb.State.SETTLED) and cyc.book.fills:
                if pl.on_close(cyc, now):
                    self._log_cycle(slug, cyc)

    def holds_window(self, slug: str) -> bool:
        """True while a cycle with fills on ``slug`` has not settled (live._queue_ledger_late)."""
        return any(c.book.fills for c in self.cycles.get(slug, []))

    def forget_closed_windows(self, now: float, keep=()) -> None:
        """At start, after the late handover: a restored window long past its close that no late
        resolution will settle (a fresh run's empty trade log, or never abandoned) is dropped."""
        for slug in [s for s in self.cycles if s not in keep and self.close_ts.get(s)
                     and float(self.close_ts[s]) + 600_000.0 <= now]:
            self.cycles.pop(slug, None)
            log.warning("[PAIRLOCK] %s: restored cycle(s) past the close dropped (no late resolution pending)", slug)

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        winner = "UP" if won_up else "DOWN"
        total = 0.0
        held = False
        for cyc in self.cycles.pop(slug, []):
            if not cyc.book.fills:
                continue
            held = True
            r = pl.on_settled(cyc, winner, now)
            self._log_cycle(slug, cyc, f" | P&L {r['pnl']:+.4f} (from cash {r['pnl_from_cash']:+.4f}) = prediction "
                                       f"{r['prediction']:+.3f} + hedging {r['hedging']:+.3f}; pairs {r['pairs']:+.3f}, "
                                       f"directional {r['directional']:+.3f}")
            total += r["pnl"]
            self.stats["settled_cycles"] += 1
            if cyc.book.unmatched >= self.prm.min_size - 1e-9:
                self.stats["naked_settled"] += 1
            for k in ("prediction", "hedging", "pairs", "directional"):
                self.stats[k] += r[k]
            self.stats["pnl"] += r["pnl"]
            self.stats["fees"] += cyc.book.fees
            self.stats["slippage"] += cyc.book.slippage
            self.settled_cycles.append({"slug": slug, "winner": winner, "state": cyc.state.value,
                                        "a_side": cyc.a_side, **cyc.book.summary(), "pnl": round(r["pnl"], 4),
                                        "prediction": round(r["prediction"], 4), "hedging": round(r["hedging"], 4),
                                        "pairs": round(r["pairs"], 4), "t": round(now)})
        if held:
            diff = abs(total - pnl)
            self.stats["recon_checked"] += 1
            self.stats["recon_max_diff"] = max(self.stats["recon_max_diff"], diff)
            self.realised += total
            if diff > RECON_TOL:
                self.accounting_halt = f"{slug}: engine P&L {total:+.4f} != venue {pnl:+.4f}"
                log.error("[PAIRLOCK] RECONCILIATION_MISMATCH %s: engine %+.4f vs venue %+.4f (diff %.4f > %.2f): new "
                          "entries halted", slug, total, pnl, diff, RECON_TOL)
            else:
                log.info("[PAIRLOCK] RECONCILED %s: engine %+.4f = venue %+.4f (diff %.6f)", slug, total, pnl, diff)
        self.pending.pop(slug, None)
        self.candidates.pop(slug, None)
        self.hist.pop(slug, None)
        self.sigma.pop(slug, None)
        self.last_p.pop(slug, None)
        if self.booksig is not None:
            self.booksig.forget(slug)

    # ───────────────────────────── restarts ─────────────────────────────

    def persist_state(self) -> dict:
        return {"pairlock_v2": {slug: {"close_ts": self.close_ts.get(slug), "cycles": [c.to_json() for c in cs]}
                                for slug, cs in self.cycles.items() if any(c.book.fills for c in cs)},
                "pairlock_stats": dict(self.stats), "pairlock_realised": self.realised}

    def restore_state(self, doc: dict | None, now: float) -> None:
        doc = doc or {}
        for slug, d in (doc.get("pairlock_v2") or {}).items():
            closed = d.get("close_ts") and float(d["close_ts"]) + 600_000.0 <= now
            if closed and float(d["close_ts"]) + 24 * 3600_000.0 <= now:
                continue
            cs = [pl.Cycle.from_json(c) for c in d.get("cycles") or []]
            # past its close only a cycle waiting for the outcome is kept: a late resolution settles it
            cs = [c for c in cs if c.book.fills and (not closed or c.state is pb.State.SETTLEMENT_PENDING)]
            if cs:
                self.cycles[slug] = cs
                self.close_ts[slug] = d.get("close_ts")
                for c in cs:
                    self._logged[id(c)] = len(c.transitions)
                log.warning("[PAIRLOCK] %s: %d cycle(s) restored from their fills: %s", slug, len(cs),
                            ", ".join(f"{c.state.value} UP {c.book.up_qty:g} DOWN {c.book.down_qty:g} matched "
                                      f"{c.book.matched_qty:g}" for c in cs))
        if doc.get("pairlock_v2") is None and (doc.get("pairlock") or {}):
            log.warning("[PAIRLOCK] v1 cycles in the saved state are NOT restored (fresh v2 run expected)")

    # ───────────────────────────── the dashboard ─────────────────────────────

    def snapshot(self) -> dict:
        snap = super().snapshot()
        open_, deployed, mtm, exp_pnl, worst = [], 0.0, 0.0, 0.0, 0.0
        for slug, cs in self.cycles.items():
            ub, db = self.last_bids.get(slug, (None, None))
            p = self.last_p.get(slug)
            for c in cs:
                if not c.book.fills and not c.book.pending:
                    continue
                b = c.book
                s = b.summary(ub, db, p)
                deployed += b.cash_spent
                mtm += s.get("mark_to_market_pnl") or 0.0
                exp_pnl += s.get("expected_pnl") or 0.0
                worst += b.worst_case_pnl
                open_.append({"slug": slug, "state": c.state.value, "a_side": c.a_side, **s,
                              "hedge_actions": c.hedge_actions, "p_up": None if p is None else round(p, 4),
                              "locked_label": (f"{b.matched_qty:g} matched pairs locked at {b.locked_pair_pnl:+.2f}"
                                               if c.state is pb.State.PAIR_LOCKED else "")})
        snap["pairlock"] = {
            "version": 2, "strategy": "B", "signal_source": self.plcfg.book.signal_source,
            "book_trigger": None if self.booksig is None else self.booksig.snapshot(),
            "stats": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.stats.items()},
            "refused": dict(self.refused_why), "open": open_, "settled": list(self.settled_cycles)[-20:],
            "decisions": list(self.decisions), "model": self.plmodel.meta.get("fitted_on"),
            "entry_gate": self.plcfg.entry_gate, "accounting_halt": self.accounting_halt,
            "totals": {"realised_pnl": round(self.realised, 4), "unrealised_mtm_pnl": round(mtm, 4),
                       "total_pnl": round(self.realised + mtm, 4), "expected_pnl_open": round(exp_pnl, 4),
                       "worst_case_open": round(worst, 4), "fees": round(self.stats["fees"], 4),
                       "slippage": round(self.stats["slippage"], 4), "capital_deployed": round(deployed, 4),
                       "capital_remaining_per_market": self.lim.max_market_capital},
            "params": {k: v for k, v in vars(self.prm).items() if k != "fractions"},
            "limits": dict(vars(self.lim))}
        return snap


def _f3(x) -> str:
    return "-" if x is None else f"{x:.3f}"
