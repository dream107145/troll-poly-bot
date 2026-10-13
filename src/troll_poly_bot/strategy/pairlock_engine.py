"""The pair-lock layer with a RECOVERY HEDGE and a PROFIT LOCK (2026-10-02 spec) on top of the triangle engine
(TPB_TRIANGLE_HEDGE_MODE=dynamic). Paper only: the triangle strategy has no real-money switch.

The triangle engine still produces the entry TRIGGER (the spot shock) with its own freshness gates. This layer:
  * keeps CYCLES per market, each with an authoritative PairBook built from the venue's FILLS (order id, fill id,
    price, fee, expected price); pending orders are never inventory;
  * gates the first leg by expected NET value at the executable VWAP (pairlock.entry_decision); a market that holds
    inventory never starts another cycle (REENTRY_BLOCKED);
  * watches the position every sample (pairlock.manage_position): ADVERSE_MOVE -> REVERSAL_CONFIRMED -> RECOVERY
    hedge (MODE B), or -- while not materially losing -- a PROFIT LOCK (MODE A), each on the LIVE ask levels of the
    opposite token, logged and counted separately;
  * moves every cycle through validated states (pairlock_book.TRANSITIONS) and logs every transition and decision
    event with the position's numbers;
  * at settlement reconciles three figures per market (the book, an independent rebuild from the raw fills, the
    bot's venue P&L) and halts new entries on a mismatch -- including a market the bot settled with a P&L that the
    engine holds no cycle for.
"""
from __future__ import annotations

import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field, fields, replace

from ..types import Order, Side, TimeInForce
from . import pairlock as pl
from . import pairlock_book as pb
from .engine import Evaluation, Reason, SideView
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

        for k in pl.OBSOLETE_PARAMS:
            if (env.get(ENV_PREFIX + k) or "").strip():
                log.warning("PAIRLOCK %s%s is OBSOLETE (the v2 EV-maximising hedge rule) and ignored", ENV_PREFIX, k)
        st = pairlock_strategy(env)
        if st == "B":
            raise ValueError("strategy B is the v2 engine (strategy/pairlock_engine_v2.PairLockConfig), not this one")
        params = parse(pl.PairLockParams(), ENV_PREFIX)
        if st in STRATEGY_MODES and params.hedge_mode != STRATEGY_MODES[st]:
            params = replace(params, hedge_mode=STRATEGY_MODES[st])
        return cls(model_path=(env.get(ENV_PREFIX + "MODEL_PATH") or cls.model_path).strip(),
                   entry_gate=(env.get(ENV_PREFIX + "ENTRY_GATE") or "1").strip().lower() not in ("0", "false", "no"),
                   params=params, limits=parse(pl.RiskLimits(), ENV_PREFIX + "LIM_"))


#: the 2026-10-02 replay's strategies (scripts/pairlock/replay3.py) -> this engine's hedge mode; B is the v2 engine
STRATEGY_MODES = {"A": "none", "C": "recovery", "D": "profit_lock", "E": "combined"}


def pairlock_strategy(env: dict | None = None) -> str:
    """TPB_PAIRLOCK_STRATEGY (A-E, or R = the dynamic loss-recovery engine). Unset: B when TPB_PAIRLOCK_HEDGE_MODE is a v2 mode (conditional /
    unconditional -- the env a v2 run was started with, so a restart keeps running what it ran), else the letter of
    the hedge mode (default E)."""
    env = os.environ if env is None else env
    st = (env.get(ENV_PREFIX + "STRATEGY") or "").strip().upper()
    mode = (env.get(ENV_PREFIX + "HEDGE_MODE") or "").strip().lower()
    if st:
        if st not in ("A", "B", "C", "D", "E", "R"):
            raise ValueError(f"{ENV_PREFIX}STRATEGY {st!r}: A | B | C | D | E | R (dynamic loss-recovery)")
        if st in STRATEGY_MODES and mode and mode != STRATEGY_MODES[st]:
            raise ValueError(f"{ENV_PREFIX}STRATEGY {st} is hedge mode {STRATEGY_MODES[st]}, not {mode!r}")
        return st
    if mode in ("conditional", "unconditional"):
        return "B"
    return {v: k for k, v in STRATEGY_MODES.items()}.get(mode or "combined", "E")


def _levels(levels) -> list[tuple[float, float]]:
    return [(float(lv.price), float(lv.size)) for lv in levels or ()]


def _depth(levels, best, ask: bool) -> float:
    if best is None:
        return 0.0
    if ask:
        return sum(lv.size for lv in levels if lv.price <= best + 0.02 + 1e-9)
    return sum(lv.size for lv in levels if lv.price >= best - 0.02 - 1e-9)


#: settled cycles of a kind of hedge, counted by what the cycle's fills were
STAT_KEYS = ("entries", "entry_fills", "entry_kills", "entries_refused", "reentry_blocked",
             "adverse_moves", "recovery_opportunities", "recovery_evaluations", "recovery_candidates",
             "recovery_rejected", "recovery_hedges_sent", "recovery_hedges_filled", "recovery_hedge_kills",
             "recovery_partial", "recovery_qty", "recovery_downside_reduction", "recovery_pair_edge_qty",
             "recovery_pair_edge_sum", "recovery_pair_cost_sum", "recovery_settled", "recovery_settlement_pnl",
             "profit_lock_opportunities", "profit_lock_rejected", "profit_lock_sent", "profit_lock_fills",
             "profit_lock_kills", "profit_lock_partial", "profit_lock_qty", "profit_lock_locked_profit",
             "profit_lock_settled", "profit_lock_settlement_pnl", "pair_locked", "recovery_hedged_held",
             "settled_cycles", "naked_settled", "pnl", "prediction", "hedging", "recovery_leg", "profit_lock_leg",
             "pairs", "directional", "fees", "slippage", "recon_checked", "recon_max_diff", "recon_failures",
             "sigma_fallbacks")


class PairLockEngine(TriangleEngine):
    """TriangleEngine + the conditional dynamic hedge / pair-lock layer (v2)."""

    #: a separate bot: the shared control file's triangle.* values (set for the triangle bot) do not steer it
    isolated_controls = True

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
        self.stats = {k: 0.0 if k in ("recovery_qty", "recovery_downside_reduction", "recovery_pair_edge_qty",
                                      "recovery_pair_edge_sum", "recovery_pair_cost_sum", "recovery_settlement_pnl",
                                      "profit_lock_qty", "profit_lock_locked_profit", "profit_lock_settlement_pnl",
                                      "pnl", "prediction", "hedging", "recovery_leg", "profit_lock_leg", "pairs",
                                      "directional", "fees", "slippage", "recon_max_diff") else 0
                      for k in STAT_KEYS}
        self.refused_why: dict[str, int] = {}
        self.decisions: deque = deque(maxlen=40)
        self._reentry_log: dict[str, float] = {}
        log.info("PAIRLOCK_CONFIG gate=%s %s | limits %s", self.plcfg.entry_gate,
                 " ".join(f"{k}={v}" for k, v in sorted(vars(self.prm).items())),
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
        held = [c for c in cs if c.holds]
        return pl.MarketExposure(capital=cap, shares=sh, worst_case=sum(c.book.worst_case_pnl for c in cs if c.book.fills),
                                 cycles=len(self.cycles.get(slug, [])), held=bool(held),
                                 held_state=held[-1].state.value if held else "")

    #: a rejected evaluation repeating for the same reason is logged at most this often (the events are all kept
    #: on the cycle)
    REPEAT_LOG_MS = 30_000.0
    _QUIET = ("RECOVERY_HEDGE_EVALUATION", "RECOVERY_HEDGE_REJECTED", "PROFIT_LOCK_EVALUATION", "PROFIT_LOCK_REJECTED")

    def _line(self, slug: str, cyc: pl.Cycle, name: str, state: str, reason: str, extra: str = "") -> None:
        b = cyc.book
        ub, db = self.last_bids.get(slug, (None, None))
        p = self.last_p.get(slug)
        log.info("[PAIRLOCK] %s %s | state %s | UP %g DOWN %g matched %g unmatched UP %g DOWN %g | total cost %.3f | "
                 "avg UP %s DOWN %s | pair cost %s edge/sh %s locked %+.3f | expected %s worst %+.3f MTM %s | fees %.3f "
                 "slip %+.3f | %s%s", name, slug, state, b.up_qty, b.down_qty, b.matched_qty, b.unmatched_up,
                 b.unmatched_down, b.total_cost, _f3(b.up_avg_cost), _f3(b.down_avg_cost), _f3(b.pair_cost_per_share),
                 _f3(b.net_pair_edge), b.locked_pair_pnl, "-" if p is None else f"{b.expected_settlement_pnl(p):+.3f}",
                 b.worst_case_pnl, "-" if ub is None and db is None else f"{b.mark_to_market_pnl(ub, db):+.3f}",
                 b.fees, b.slippage, reason, extra)

    def _log_cycle(self, slug: str, cyc: pl.Cycle, extra: str = "") -> None:
        """Every new transition and audit event (brief item 25), each with the position's numbers. An evaluation
        rejected again for the same reason within REPEAT_LOG_MS is counted, not printed."""
        k = id(cyc)
        t0, e0 = self._logged.get(k, (0, 0))
        new_t, new_e = cyc.transitions[t0:], cyc.events[e0:]
        rows = [(tr.t, 0, tr) for tr in new_t] + [(ev.t, 1, ev) for ev in new_e]
        names = {ev.name for ev in new_e}
        quiet = bool(new_e) and names <= set(self._QUIET) and all(
            tr.to in ("RECOVERY_EVALUATION", "PROFIT_LOCK_EVALUATION") or tr.frm in ("RECOVERY_EVALUATION",
                                                                                      "PROFIT_LOCK_EVALUATION")
            for tr in new_t)
        if quiet:
            rej = [ev for ev in new_e if ev.name.endswith("REJECTED")]
            key = (k, rej[-1].reason.split(":")[0][:60] if rej else "")
            rl = self.__dict__.setdefault("_repeat_log", {})
            last = rl.get(key)
            if rej and last is not None and new_e[-1].t - last[0] < self.REPEAT_LOG_MS:
                rl[key] = (last[0], last[1] + 1)
                self._logged[k] = (len(cyc.transitions), len(cyc.events))
                return
            if last is not None and last[1]:
                extra = f"{extra} | {last[1]} similar rejected evaluations since the last line"
            rl[key] = (new_e[-1].t, 0)
        for _t, kind, x in sorted(rows, key=lambda r: (r[0], r[1])):
            if kind == 0:
                self._line(slug, cyc, f"STATE {x.frm} -> {x.to}", x.to, x.reason, extra)
            else:
                self._line(slug, cyc, x.name, x.state, x.reason, extra)
        self._logged[k] = (len(cyc.transitions), len(cyc.events))

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
        before = cyc.state
        pl.on_order_done(cyc, p["oid"], filled, now, p.get("why", ""), prm=self.prm)
        if p["kind"] == "entry" and not filled:
            cs = self.cycles.get(slug, [])
            if cyc in cs and not cyc.book.fills:
                cs.remove(cyc)
        if p["kind"] != "entry" and filled and p.get("filled_qty", 0.0) < p["q"] - 1e-9:
            self.stats["recovery_partial" if p["kind"] == "recovery" else "profit_lock_partial"] += 1
        if before is not cyc.state and cyc.state is pb.State.PAIR_LOCKED:
            self.stats["pair_locked"] += 1
        self._log_cycle(slug, cyc)

    # ───────────────────────────── the evaluation ─────────────────────────────

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        m = meta.market
        self._record(meta, book_up, book_down, spot, now)
        self._finalize(m.slug, now)
        cyc0 = self._cycle(m.slug)
        if (cyc0 is not None and cyc0.state in (pb.State.RECOVERY_EVALUATION, pb.State.PROFIT_LOCK_EVALUATION)
                and m.slug not in self.pending):
            pl._move(cyc0, pl.watch_state(cyc0, self.prm), now, "hedge decision never sent")   # noqa: SLF001
            self._log_cycle(m.slug, cyc0)
        self.candidates.pop(m.slug, None)                                    # a candidate the bot did not send
        ev = super().evaluate(meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                              balance, orderflow, quote_ages)
        cyc = self._cycle(m.slug)
        if cyc is not None and cyc.state in pb.WATCH_STATES and m.slug not in self.pending:
            hev = self._manage(meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc)
            if hev is not None:
                if ev.tradeable:
                    self._refuse_pl(ev, f"REENTRY_BLOCKED: a hedge goes first ({cyc.state.value})", Reason.ALREADY_POSITIONED)
                return hev
        if not ev.tradeable:
            return ev
        if self.accounting_halt:
            return self._refuse_pl(ev, f"accounting halt: {self.accounting_halt}", Reason.RISK_LIMIT)
        return self._gate_entry(ev, meta, book_up, book_down, now, spot_age_ms, quote_ages)

    def _gate_entry(self, ev: Evaluation, meta, book_up, book_down, now: float, spot_age_ms=None,
                    quote_ages=None) -> Evaluation:
        m = meta.market
        side = ev.side
        book = book_up if side == "UP" else book_down
        ask, bid = book.best_ask, book.best_bid
        prev = self._cycle(m.slug)
        if m.slug in self.pending:
            return self._refuse_pl(ev, "an order is in flight in this market", Reason.ALREADY_POSITIONED)
        mkt = self._exposure(m.slug)
        if mkt.held:
            self.stats["reentry_blocked"] += 1
            if now - self._reentry_log.get(m.slug, -1e18) >= self.REPEAT_LOG_MS and prev is not None:
                self._reentry_log[m.slug] = now
                prev.note("REENTRY_BLOCKED", now, f"a new {side} trigger: the market holds inventory "
                          f"({prev.state.value}); no new cycle until it settles")
                self._log_cycle(m.slug, prev)
        f = self._features(meta, now)
        if f is None and not mkt.held:
            return self._refuse_pl(ev, "pairlock features not ready (history / sigma)", Reason.MODEL_NOT_READY)
        prm = self.prm if self.plcfg.entry_gate else replace(self.prm, entry_edge=-1.0)
        dec, why = pl.entry_decision(f or {}, side, ask, bid, _levels(book.asks), self.plmodel, prm, self.lim,
                                     mkt, m.seconds_remaining(now), prev)
        tri = ev.features.get(STRATEGY_ID) or {}
        if dec is None:
            self.stats["entries_refused"] += 1
            return self._refuse_pl(ev, why, self._reason_for(why))
        size = dec.shares
        limit = min(ev.limit_price, dec.limit)
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit, add_to_open=True)
        if not verdict.ok:
            return self._refuse_pl(ev, f"risk: {verdict.detail}", Reason(verdict.reason))
        tok = m.yes_token_id if side == "UP" else m.no_token_id
        cyc = pl.Cycle(book=pb.PairBook(m.slug), a_side=side, started_t=now)
        cyc.entry_info = {"decision_t": now, "token": tok, "qty": size, "ask": ask, "executable_vwap": round(dec.info["vwap"], 4),
                          "limit": limit, "fee_per_share": round(dec.info["fee"], 5), "model_p": round(dec.p, 4),
                          "ev": round(dec.ev, 4), "p_fav": dec.info.get("p_fav"),
                          "signal": {k: tri.get(k) for k in ("trigger", "direction", "z1", "z3", "r1_bps", "trend_bps",
                                                             "score", "flow_age_ms") if k in tri},
                          "spot_age_ms": None if spot_age_ms is None else round(float(spot_age_ms), 1),
                          "quote_age_ms": None if (quote_ages or {}).get(tok) is None else round(float(quote_ages[tok]), 1)}
        self.candidates[m.slug] = cyc
        ev.size, ev.limit_price, ev.max_entry_price = size, limit, limit
        tri.update({"size": size, "limit": limit, "pairlock": {"kind": "entry", "p_win": round(dec.p, 4),
                                                               "ev": round(dec.ev, 4), "vwap": round(dec.info["vwap"], 4),
                                                               "p_fav": dec.info.get("p_fav")}})
        ev.size_note = f"{size:g} sh, EV {dec.ev:+.3f}/sh (P {dec.p:.3f} at VWAP {dec.info['vwap']:.3f})"
        self.decisions.append({"t": round(now), "slug": m.slug, "action": "entry", "side": side, "ask": ask,
                               "p": round(dec.p, 4), "ev": round(dec.ev, 4)})
        return ev

    def _manage(self, meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc: pl.Cycle) -> Evaluation | None:
        m = meta.market
        if book_up is None or book_down is None:
            return None
        qa = quote_ages or {}
        mv = pl.MarketView(bid_up=book_up.best_bid, ask_up=book_up.best_ask, bid_down=book_down.best_bid,
                           ask_down=book_down.best_ask, asks_up=_levels(book_up.asks), asks_down=_levels(book_down.asks),
                           bids_up=_levels(book_up.bids), bids_down=_levels(book_down.bids),
                           quote_age_up_ms=qa.get(m.yes_token_id), quote_age_down_ms=qa.get(m.no_token_id),
                           spot_age_ms=spot_age_ms)
        f = self._features(meta, now)
        ep0, n0 = cyc.episode, len(cyc.events)
        dec, why, rec = pl.manage_position(cyc, f, mv, now, m.seconds_remaining(now), self.plmodel, self.prm,
                                           self.lim, self._exposure(m.slug))
        self.stats["adverse_moves"] += sum(1 for x in cyc.events[n0:] if x.name == "ADVERSE_MOVE_DETECTED")
        if cyc.episode != ep0:
            self.stats["recovery_opportunities"] += 1
        if rec is not None:
            if rec["kind"] == "recovery":
                self.stats["recovery_evaluations"] += 1
                if rec.get("acceptable"):
                    self.stats["recovery_candidates"] += 1
                if rec.get("decision") == "REJECTED":
                    self.stats["recovery_rejected"] += 1
            else:
                self.stats["profit_lock_opportunities"] += 1
                if rec.get("decision") == "REJECTED":
                    self.stats["profit_lock_rejected"] += 1
        self._log_cycle(m.slug, cyc)
        if dec is None:
            return None
        light = dec.side
        book_l = book_up if light == "UP" else book_down
        tok_l = m.yes_token_id if light == "UP" else m.no_token_id
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
        ev.side, ev.token_id, ev.limit_price = light, tok_l, dec.limit
        ev.expected_price = dec.info.get("vwap") or book_l.best_ask
        ev.max_entry_price = dec.limit
        ev.size, ev.net_edge, ev.confidence = dec.shares, dec.ev / max(dec.shares, 1e-9), 1.0
        ev.p_used = dec.p if light == "UP" else 1.0 - dec.p
        ev.size_note = f"{dec.kind} {dec.shares:g} sh of {light}: {dec.reason}"
        mk = {"t": round(now), "dir": 1 if light == "UP" else -1, "z1": None, "z3": None, "r1_bps": None,
              "trigger": "hedge", "ok": True, "why": "", "hedge": dict(info["pairlock"])}
        self.shock_marks.setdefault(m.slug, deque(maxlen=self.SHOCK_MARKS_PER_MARKET)).append(mk)
        self.decisions.append({"t": round(now), "slug": m.slug, "action": dec.kind, "side": light, "q": dec.shares,
                               "pair_cost": dec.info.get("pair_cost"), "worst_before": dec.info.get("worst_before"),
                               "worst_after": dec.info.get("worst_after"),
                               "expected_before": dec.info.get("expected_before"),
                               "expected_after": dec.info.get("expected_after")})
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
            kind = f["pairlock"]["kind"]
            o = Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size, tif=TimeInForce.FOK,
                      expected_price=ev.expected_price,
                      tag=f"{ev.side} pairlock {kind} q={ev.size:g} vwap={f['pairlock'].get('vwap')}")
            cyc = self._cycle(ev.slug)
            pl.on_hedge_sent(cyc, o.order_id, ev.side, ev.size, ev.limit_price, now, kind, f["pairlock"])
            self.pending[ev.slug] = {"oid": o.order_id, "kind": kind, "cycle": cyc, "q": ev.size,
                                     "limit": ev.limit_price, "done": False, "filled_qty": 0.0,
                                     "expected": ev.expected_price, "info": dict(f["pairlock"])}
            self.stats["recovery_hedges_sent" if kind == "recovery" else "profit_lock_sent"] += 1
            self._log_cycle(ev.slug, cyc)
            return o
        o = super().to_order(ev, now_ms)
        cyc = self.candidates.pop(ev.slug, None) or pl.Cycle(book=pb.PairBook(ev.slug), a_side=ev.side, started_t=now)
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
                    f"pair cost {pk.get('pair_cost')}, worst {pk.get('worst_before')} -> {pk.get('worst_after')}, "
                    f"expected {pk.get('expected_before')} -> {pk.get('expected_after')}, state {pk.get('state')}")
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
                rec = p["kind"] == "recovery"
                self.stats[("recovery_hedges_filled" if rec else "profit_lock_fills") if filled else
                           ("recovery_hedge_kills" if rec else "profit_lock_kills")] += 1
                if not filled:
                    for mk in reversed(self.shock_marks.get(slug, ())):
                        if mk.get("trigger") == "hedge" and mk.get("ok"):
                            mk["ok"], mk["why"] = False, f"hedge not filled: {why}"
                            break
            if not filled:
                self._finalize(slug, p["cycle"].last_action_t if p["kind"] != "entry" else time.time() * 1000.0)
            return

    def entry_refused(self, ev: Evaluation, why: str) -> None:
        """The bot did not send this evaluation's order (guard, freshest book, kill switch, pre-send check)."""
        if self._is_hedge(ev):
            cyc = self._cycle(ev.slug)
            p = self.pending.get(ev.slug)
            if p is not None and p["kind"] != "entry" and not p.get("done"):
                self.pending.pop(ev.slug, None)                # to_order ran, but the order never left
                cyc.book.resolve_pending(p["oid"])
                cyc.pending_hedge = None
                cyc.hedge_actions = max(0, cyc.hedge_actions - 1)
                self.stats["recovery_hedges_sent" if p["kind"] == "recovery" else "profit_lock_sent"] -= 1
            if cyc is not None and cyc.state in (pb.State.RECOVERY_EVALUATION, pb.State.PROFIT_LOCK_EVALUATION,
                                                 pb.State.RECOVERY_HEDGE_PENDING, pb.State.PROFIT_LOCK_PENDING):
                cyc.note("HEDGE_NOT_SENT", cyc.last_eval_t, why)
                pl._move(cyc, pl.watch_state(cyc, self.prm), cyc.last_eval_t, f"hedge not sent: {why}")   # noqa: SLF001
                self._log_cycle(ev.slug, cyc)
            for mk in reversed(self.shock_marks.get(ev.slug, ())):
                if mk.get("trigger") == "hedge" and mk.get("ok"):
                    mk["ok"], mk["why"] = False, f"hedge not sent: {why}"
                    break
            return
        self.candidates.pop(ev.slug, None)
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
        side = cyc.a_side if p["kind"] == "entry" else cyc.b_side
        n = sum(1 for f in cyc.book.fills if f.order_id == order.order_id) + 1
        rec = pb.FillRecord(market_id=slug, ts=now, token=order.token_id, side=side, qty=float(fill.size),
                            price=float(fill.price), fee=float(fill.fee), order_id=order.order_id,
                            fill_id=f"{order.order_id}-{n}", action="BUY",
                            expected_price=p.get("expected"), kind=p["kind"])
        worst0 = cyc.book.worst_case_pnl
        pl.on_fill(cyc, rec, self.prm, now)
        p["filled_qty"] = p.get("filled_qty", 0.0) + float(fill.size)
        if p["kind"] == "recovery":
            self.stats["recovery_qty"] += float(fill.size)
            self.stats["recovery_downside_reduction"] += cyc.book.worst_case_pnl - worst0
            if cyc.recovery_edges:
                q_, e_ = cyc.recovery_edges[-1]
                self.stats["recovery_pair_edge_qty"] += q_
                self.stats["recovery_pair_edge_sum"] += q_ * e_
                self.stats["recovery_pair_cost_sum"] += q_ * (1.0 - e_)
        elif p["kind"] == "profit_lock":
            self.stats["profit_lock_qty"] += float(fill.size)
        if p["filled_qty"] + 1e-9 >= p["q"]:
            p["done"] = True
            self._finalize(slug, now)
        else:
            self._log_cycle(slug, cyc)

    def observe_point(self, slug: str, market, point: dict, now: float) -> None:
        """Every 200 ms sample (live._sample_prices): a closed market's cycles wait for the outcome."""
        super().observe_point(slug, market, point, now)
        if now < market.close_ts:
            return
        for cyc in self.cycles.get(slug, []):
            if cyc.state not in (pb.State.SETTLEMENT_PENDING, pb.State.MARKET_RESOLVED, pb.State.SETTLED) and cyc.holds:
                if pl.on_close(cyc, now, self.prm):
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
        """Every market the bot settles (also the ones the engine never traded). Three figures must agree within
        RECON_TOL: each cycle's book P&L and its independent rebuild from the raw fills, and the market total
        against the bot's (venue) P&L. A market the bot booked P&L on that the engine holds nothing for is a
        failure too: a market can never disappear from the engine."""
        winner = "UP" if won_up else "DOWN"
        total = 0.0
        held = False
        worst_rebuild = 0.0
        for cyc in self.cycles.pop(slug, []):
            if not cyc.book.fills:
                continue
            held = True
            st_before = cyc.state
            r = pl.on_settled(cyc, winner, now)
            self._log_cycle(slug, cyc)
            total += r["pnl"]
            worst_rebuild = max(worst_rebuild, abs(r["pnl"] - r["pnl_reconstructed"]), abs(r["pnl"] - r["pnl_from_cash"]))
            self.stats["settled_cycles"] += 1
            if cyc.book.unmatched >= self.prm.min_size - 1e-9:
                self.stats["naked_settled"] += 1
            if st_before is pb.State.RECOVERY_HEDGED:
                self.stats["recovery_hedged_held"] += 1
            for k in ("prediction", "hedging", "recovery_leg", "profit_lock_leg", "pairs", "directional"):
                self.stats[k] += r[k]
            if cyc.recovery_qty > 0:
                self.stats["recovery_settled"] += 1
                self.stats["recovery_settlement_pnl"] += r["pnl"]
            if cyc.profit_lock_qty > 0:
                self.stats["profit_lock_settled"] += 1
                self.stats["profit_lock_settlement_pnl"] += r["pnl"]
                self.stats["profit_lock_locked_profit"] += cyc.profit_lock_locked
            self.stats["pnl"] += r["pnl"]
            self.stats["fees"] += cyc.book.fees
            self.stats["slippage"] += cyc.book.slippage
            self.settled_cycles.append({"slug": slug, "winner": winner, "state": st_before.value,
                                        "a_side": cyc.a_side, **cyc.book.summary(), "pnl": round(r["pnl"], 4),
                                        "prediction": round(r["prediction"], 4), "hedging": round(r["hedging"], 4),
                                        "recovery_leg": round(r["recovery_leg"], 4),
                                        "profit_lock_leg": round(r["profit_lock_leg"], 4),
                                        "pairs": round(r["pairs"], 4), "recovery_qty": cyc.recovery_qty,
                                        "profit_lock_qty": cyc.profit_lock_qty, "t": round(now)})
        if not held and abs(pnl) <= RECON_TOL:
            self._drop(slug)
            return
        diff = max(abs(total - pnl), worst_rebuild)
        self.stats["recon_checked"] += 1
        self.stats["recon_max_diff"] = max(self.stats["recon_max_diff"], diff)
        self.realised += total
        if not held or diff > RECON_TOL:
            self.stats["recon_failures"] += 1
            why = (f"{slug}: the bot booked {pnl:+.4f} on a market the engine holds no cycle for" if not held else
                   f"{slug}: engine {total:+.4f} vs venue {pnl:+.4f}, rebuild diff {worst_rebuild:.6f}")
            self.accounting_halt = why
            log.error("[PAIRLOCK] RECONCILIATION_FAILURE %s (tolerance %.2f): new entries halted", why, RECON_TOL)
        else:
            log.info("[PAIRLOCK] RECONCILED %s: engine %+.4f = venue %+.4f = rebuilt from fills (diff %.6f)", slug,
                     total, pnl, diff)
        self._drop(slug)

    def _drop(self, slug: str) -> None:
        self.pending.pop(slug, None)
        self.candidates.pop(slug, None)
        self.hist.pop(slug, None)
        self.sigma.pop(slug, None)
        self.last_p.pop(slug, None)
        self._reentry_log.pop(slug, None)

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
            cs = [pl.Cycle.from_json(c, self.prm) for c in d.get("cycles") or []]
            # past its close only a cycle waiting for the outcome is kept: a late resolution settles it
            cs = [c for c in cs if c.book.fills and (not closed or c.state is pb.State.SETTLEMENT_PENDING)]
            if cs:
                self.cycles[slug] = cs
                self.close_ts[slug] = d.get("close_ts")
                for c in cs:
                    self._logged[id(c)] = (len(c.transitions), len(c.events))
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
                rem = b.unmatched
                label = (f"{b.matched_qty:g} matched pairs locked at {b.locked_pair_pnl:+.2f}"
                         + (f"; {rem:g} {b.heavy_side} unmatched = DIRECTIONAL" if rem > 1e-6 else "")
                         if b.matched_qty > 1e-9 else "")
                open_.append({"slug": slug, "state": c.state.value, "a_side": c.a_side, **s,
                              "hedge_actions": c.hedge_actions, "recovery_qty": c.recovery_qty,
                              "profit_lock_qty": c.profit_lock_qty, "p_up": None if p is None else round(p, 4),
                              "locked_label": label, "position": {k: v for k, v in (c.last_obs or {}).items()
                                                                  if k != "signals"}})
        st = self.stats
        n_e = st["recovery_pair_edge_qty"]
        snap["pairlock"] = {
            "version": 3, "stats": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in st.items()},
            "recovery": {"opportunities": st["recovery_opportunities"], "evaluations": st["recovery_evaluations"],
                         "candidates": st["recovery_candidates"], "rejected": st["recovery_rejected"],
                         "sent": st["recovery_hedges_sent"], "filled": st["recovery_hedges_filled"],
                         "kills": st["recovery_hedge_kills"], "partial": st["recovery_partial"],
                         "qty": round(st["recovery_qty"], 4),
                         "downside_reduction": round(st["recovery_downside_reduction"], 4),
                         "avg_pair_cost": None if n_e <= 0 else round(st["recovery_pair_cost_sum"] / n_e, 4),
                         "avg_pair_edge": None if n_e <= 0 else round(st["recovery_pair_edge_sum"] / n_e, 4),
                         "settled": st["recovery_settled"], "settlement_pnl": round(st["recovery_settlement_pnl"], 4),
                         "leg_pnl": round(st["recovery_leg"], 4)},
            "profit_lock": {"opportunities": st["profit_lock_opportunities"], "rejected": st["profit_lock_rejected"],
                            "sent": st["profit_lock_sent"], "filled": st["profit_lock_fills"],
                            "kills": st["profit_lock_kills"], "partial": st["profit_lock_partial"],
                            "qty": round(st["profit_lock_qty"], 4), "locked_profit": round(st["profit_lock_locked_profit"], 4),
                            "settled": st["profit_lock_settled"],
                            "settlement_pnl": round(st["profit_lock_settlement_pnl"], 4),
                            "leg_pnl": round(st["profit_lock_leg"], 4)},
            "baseline": {"no_hedge_pnl": round(st["prediction"], 4), "actual_pnl": round(st["pnl"], 4),
                         "hedging_effect": round(st["hedging"], 4)},
            "refused": dict(self.refused_why), "open": open_, "settled": list(self.settled_cycles)[-20:],
            "decisions": list(self.decisions), "model": self.plmodel.meta.get("fitted_on"),
            "entry_gate": self.plcfg.entry_gate, "accounting_halt": self.accounting_halt,
            "totals": {"realised_pnl": round(self.realised, 4), "unrealised_mtm_pnl": round(mtm, 4),
                       "total_pnl": round(self.realised + mtm, 4), "expected_pnl_open": round(exp_pnl, 4),
                       "worst_case_open": round(worst, 4), "fees": round(st["fees"], 4),
                       "slippage": round(st["slippage"], 4), "capital_deployed": round(deployed, 4),
                       "capital_remaining_per_market": self.lim.max_market_capital},
            "params": {k: v for k, v in vars(self.prm).items() if k != "fractions"},
            "limits": dict(vars(self.lim))}
        return snap


def _f3(x) -> str:
    return "-" if x is None else f"{x:.3f}"
