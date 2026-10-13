"""STRATEGY R -- the DYNAMIC LOSS-RECOVERY / REBALANCING engine (the user's 2026-10-02 spec) on the triangle engine's
entry trigger and strategy B's entry gate. Paper only (__main__ refuses micro-live / live / shadow for the triangle
strategy). Selected by TPB_PAIRLOCK_STRATEGY=R (live.py).

One portfolio per market (strategy/pairlock_recovery.RecoveryCycle on the fills-based PairBook). The first leg is
B's: the crypto spot shock, then pairlock_v2.entry_decision (EV at the executable VWAP, P(favourable 10 s)). From the
first fill every further buy in the market is a RECOVERY decision (pairlock_recovery.evaluate_recovery) at every
evaluation: a new entry trigger while the market holds inventory is refused (REENTRY_BLOCKED) -- re-entering the
original side happens through the recovery arithmetic (the whole position), never as an independent cycle.

Every evaluation that reaches the direction gate is logged as one JSON line ([RECOVERY_EVAL] ...) with the spec's
item-13 fields; a rejection repeating for the same reason is printed once per LOG_REPEAT_MS and counted. Settlement
reconciles the book's P&L, the cash view, a rebuild from the raw fills and the bot's (venue) P&L; a difference over
RECON_TOL halts new entries.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import fields, replace

from ..types import Order, Side, TimeInForce
from . import pairlock_book as pb
from . import pairlock_recovery as rc
from . import pairlock_swing as sw
from . import pairlock_ulr as ulr
from . import pairlock_v2 as plv2
from .engine import Evaluation, Reason, SideView
from .paired_exit import ExitAction
from .pairlock import MarketView
from .pairlock_engine import _depth, _levels
from .pairlock_engine_v2 import ENV_PREFIX, RECON_TOL, PairLockConfig
from .triangle import STRATEGY_ID, TriangleEngine

log = logging.getLogger(__name__)
PEXIT_DUST_BID = 0.01      # the profit exit neither values nor sells a token whose bid is at the floor
EXIT_REJECT_BACKOFF_MS = 10_000.0   # a sell the venue refused is not tried again for this long (plus the cooldown)


def sell_qty(q: float, min_size: float) -> float:
    """What a sell order may ask for: the holding ROUNDED DOWN to the venue's hundredths (2026-10-06 00:32: 26.4789 held,
    26.48 asked, the venue refused 'not enough balance'), and nothing under the venue's minimum order (a 0.003 remnant
    rounds to 0.00 and is refused for its tick size; it settles with the market)."""
    q = math.floor(float(q) * 100.0 + 1e-9) / 100.0
    return q if q >= max(float(min_size or 0.0), 0.01) - 1e-9 else 0.0

REC_PREFIX = ENV_PREFIX + "REC_"           # TPB_PAIRLOCK_REC_<FIELD> -> RecoveryParams
#: a pair bid pulled to be re-placed lower: the lower bid waits for the venue's word that the old one ended, at most
#: this long (the real venue answers within a second or two; a restart loses the answer)
REPRICE_CANCEL_WAIT_MS = 35_000.0
#: the spec's names -> RecoveryParams fields (both spellings are accepted in the env)
SPEC_NAMES = {"RECOVERY_TARGET_PROFIT": "target_profit", "MAX_RECOVERY_ORDER_SIZE": "max_order_size",
              "MAX_TOTAL_POSITION": "max_total_position", "MAX_RECOVERY_CAPITAL": "max_capital",
              "MAX_SLIPPAGE": "max_slippage", "MIN_RECOVERY_EDGE": "min_edge", "MIN_PRICE_MOVE": "min_price_move",
              "RECOVERY_COOLDOWN": "cooldown_ms", "MAX_RECOVERY_ACTIONS": "max_actions",
              "RECOVERY_MIN_CONFIRMATIONS": "min_confirmations"}
SWING_PREFIX = ENV_PREFIX + "SWING_"
#: the swing spec's (2026-10-07) names -> RecoveryParams fields. Env: TPB_PAIRLOCK_<NAME> for the names that start with
#: SWING (TPB_PAIRLOCK_SWING1_SHARES, TPB_PAIRLOCK_SWING_RECOVERY_PRICE), TPB_PAIRLOCK_SWING_<NAME> for the others
#: (TPB_PAIRLOCK_SWING_MODE, TPB_PAIRLOCK_SWING_MAX_MARKET_CAPITAL, ...); TPB_PAIRLOCK_REC_<FIELD> works too
SWING_SPEC_NAMES = {"MODE": "swing_mode", "INITIAL_SHARES": "swing_initial_shares", "INITIAL_ENTRY_MIN": "swing_entry_min",
                    "INITIAL_ENTRY_MAX": "swing_entry_max", "INITIAL_ENTRY_DELAY_S": "swing_entry_delay_s",
                    "INITIAL_ENTRY_WINDOW_S": "swing_entry_window_s", "SWING_RECOVERY_PRICE": "swing_recovery_price",
                    "SWING1_SHARES": "swing1_shares", "SWING2_SHARES": "swing2_shares",
                    "MAX_RECOVERY_SWING": "swing_max_recovery", "ENABLE_TWO_SIDED_HEDGE": "swing_two_sided_hedge",
                    "MAX_MARKET_CAPITAL": "swing_max_market_capital", "LEADER_HYSTERESIS": "swing_leader_hysteresis",
                    "SIZING": "swing_sizing", "TARGET_RECOVERY_PROFIT": "swing_target_profit",
                    "MAX_RECOVERY_SHARES": "swing_max_recovery_shares", "MAX_RECOVERY_PRICE": "swing_max_recovery_price"}


def swing_env_name(spec_name: str) -> str:
    return ENV_PREFIX + spec_name if spec_name.startswith("SWING") else SWING_PREFIX + spec_name


def recovery_params_from_env(env: dict | None = None) -> rc.RecoveryParams:
    env = os.environ if env is None else env
    kw = {}
    names = [(f.name.upper(), f.name, REC_PREFIX + f.name.upper()) for f in fields(rc.RecoveryParams)]
    names += [(k, n, None) for k, n in SPEC_NAMES.items()] + [(k, n, swing_env_name(k)) for k, n in SWING_SPEC_NAMES.items()]
    base = rc.RecoveryParams()
    for key, name, alt in names:
        if key in SPEC_NAMES:
            raw = env.get(REC_PREFIX + key) or env.get(ENV_PREFIX + key)
        else:
            raw = env.get(alt) if alt is not None else None
            if raw is None or not str(raw).strip():
                continue
        raw = (raw or "").strip()
        if not raw:
            continue
        cur = getattr(base, name)
        try:
            kw[name] = (raw.lower() not in ("0", "false", "no", "off")) if isinstance(cur, bool) else \
                raw.lower() if isinstance(cur, str) else int(float(raw)) if isinstance(cur, int) else float(raw)
        except ValueError:
            log.warning("PAIRLOCK recovery: ignoring %s=%r", key, raw)
    return replace(base, **kw)


class PairLockRecoveryEngine(TriangleEngine):
    """TriangleEngine + B's entry gate + the dynamic loss-recovery manager = STRATEGY R."""

    isolated_controls = True
    strategy_label = "R"
    #: live.py: the open rule's pair bid follows a fill-and-kill buy's PARTIAL fill too (sized to what filled)
    pair_after_partial = True
    LOG_REPEAT_MS = 5_000.0

    def __init__(self, cfg, scfg, risk, features=None, plcfg: PairLockConfig | None = None,
                 rprm: rc.RecoveryParams | None = None) -> None:
        super().__init__(cfg, scfg, risk, features)
        self.plcfg = plcfg or PairLockConfig.from_env()
        self.prm, self.lim = self.plcfg.params, self.plcfg.limits          # B's entry gate and freshness limits
        self.rprm = rprm or recovery_params_from_env()
        try:
            self.plmodel = plv2.ProbModel.load(self.plcfg.model_path)
            log.info("PAIRLOCK model %s (version %s)", self.plcfg.model_path, self.plmodel.meta.get("version"))
        except (OSError, ValueError, KeyError) as exc:
            self.plmodel = plv2.ProbModel.market_only()
            log.error("PAIRLOCK model not loaded from %s (%s): the market price is the probability", self.plcfg.model_path, exc)
        self.cycles: dict[str, rc.RecoveryCycle] = {}
        self.candidates: dict[str, rc.RecoveryCycle] = {}
        self.hist: dict[str, plv2.History] = {}
        self.spot_hist: dict[str, plv2.History] = {}
        self.sigma: dict[str, float] = {}
        self.close_ts: dict[str, float] = {}
        self.last_p: dict[str, float] = {}
        self.last_bids: dict[str, tuple] = {}
        self.pending: dict[str, dict] = {}           # slug -> the order in flight
        self._decisions: dict[str, rc.RecoveryDecision] = {}
        self._repeat: dict[tuple, list] = {}
        self._balance = 0.0
        self.realised = 0.0
        self.accounting_halt = ""
        self.settled_cycles: deque = deque(maxlen=60)
        self.decisions: deque = deque(maxlen=40)
        self.stats = {k: 0 for k in ("entries", "entry_fills", "entry_kills", "entries_refused", "reentry_blocked",
                                     "evaluations", "opportunities", "recovery_sent", "recovery_fills", "recovery_kills",
                                     "recovery_partial", "recovery_up", "recovery_down", "settled_cycles", "recovered",
                                     "reached_target", "ended_negative", "recon_checked", "recon_failures",
                                     "sigma_fallbacks", "pair_locked", "late_evaluations", "late_candidates", "late_sent",
                                     "late_fills", "late_kills")}
        self.stats.update({k: 0.0 for k in ("recovery_qty", "recovery_cost", "late_qty", "late_cost", "late_leg", "pnl", "first_legs", "recovery_effect",
                                            "fees", "slippage", "recon_max_diff", "min_settlement_sum")})
        self.stats.update({k: 0 for k in ("open_sent", "open_fills", "open_kills", "pairs_placed", "pairs_filled",
                                          "pairs_cancelled", "pairs_expired", "pairs_skipped", "cond_addons", "pairs_repriced")})
        self.stats.update({"pair_qty": 0.0, "pair_leg": 0.0, "late_hedges_sent": 0, "late_hedge_fills": 0, "late_hedge_qty": 0.0})
        # the unfilled-leg recovery (pairlock_ulr): counts, and the settlement figures of every unhedged position
        self.stats.update({k: 0 for k in ("ulr_candidates", "ulr_signals", "ulr_sent", "ulr_fills", "ulr_kills", "ulr_exits_sent",
                                          "ulr_exit_fills", "ulr_exit_kills", "ulr_tp", "ulr_sl", "ulr_signal_exits", "ulr_emergency",
                                          "ulr_resolution", "ulr_skipped", "ulr_positions", "ulr_recovered", "ulr_hedged")})
        self.stats.update({k: 0.0 for k in ("ulr_qty", "ulr_pnl", "ulr_fees", "ulr_original_pnl", "ulr_original_pnl_recovered",
                                            "ulr_combined_pnl", "ulr_worst")})
        self.ulr_reasons: dict[str, int] = {}
        self.ulr_settled: deque = deque(maxlen=60)
        self._ulr_action_t: dict[str, float] = {}      # position id -> the evaluation time of its last exit action
        self._pexit: dict[str, tuple[str, str, str]] = {}   # order id -> (slug, side, kind) of a PROFIT_EXIT / RECOVERY_SELL in flight
        self._exit_venue: dict[str, float] = {}        # slug -> the bot's flat-market P&L, while sells of that exit are still to book
        self._manual_sells: dict[str, tuple[str, str]] = {}   # order id -> (slug, side): a dashboard Sell sent, not booked yet
        self._pexit_t: dict[str, float] = {}           # slug -> when its last profit-exit sells were sent
        self._rsell_t: dict[str, float] = {}           # slug -> when its last falling-side sale was sent
        self.stats.update({"profit_exits": 0, "profit_exit_fills": 0, "profit_exit_pnl": 0.0,
                           "recovery_sells": 0, "recovery_sell_fills": 0, "recovery_sell_proceeds": 0.0})
        if self.rprm.ulr_enabled and (self.rprm.recovery_enabled or self.rprm.pair_pull):
            log.warning("PAIRLOCK ULR: the unfilled-leg recovery is on, so the strategy-R recovery does not buy and the pair "
                        "bid is never pulled (recovery_enabled=%s pair_pull=%s are overridden)", self.rprm.recovery_enabled,
                        self.rprm.pair_pull)
        self.stats.update({"lock_sent": 0, "lock_fills": 0, "lock_kills": 0, "lock_qty": 0.0, "locked_markets": 0,
                           "locked_settled": 0, "locked_pnl": 0.0, "late_trimmed": 0})
        self.stats.update({"ladder_sent": 0, "ladder_fills": 0, "ladder_trimmed": 0, "ladder_qty": 0.0, "ladder_cost": 0.0,
                           "even_out_sent": 0, "low_evaluations": 0, "low_candidates": 0})
        if self.rprm.entry_mode == "conditions":
            self.strategy_label = "R-cond"                      # the pairlock-btc bot's conditions rule (10-02 20:40)
        # swing mode (the user's 2026-10-07 spec, strategy/pairlock_swing.py): counts per swing bucket at settlement
        self.stats.update({"swing_entries_sent": 0, "swing_entry_fills": 0, "swing_recognised": 0, "hedge_sent": 0,
                           "hedge_fills": 0, "hedge_kills": 0, "hedge_qty": 0.0, "hedge_cost": 0.0, "swing_settled": 0})
        for b in ("0", "1", "2", "3+"):
            self.stats.update({f"swing{b}_markets": 0, f"swing{b}_pnl": 0.0, f"swing{b}_wins": 0})
        self.swing_settled: deque = deque(maxlen=60)
        if self.rprm.swing_mode:
            self.strategy_label = "R-swing"
        self.quotes: dict[str, tuple] = {}            # slug -> (up bid, up ask, down bid, down ask) of the last evaluation
        self._high_count: dict[str, int] = {}         # slug -> high-band buys that filled (the user, 2026-10-08)
        self._open_tries: dict[str, int] = {}
        self._pair_due: set[str] = set()              # slugs whose open buy just filled: live.py asks pair_order next
        self.rejections: dict[str, int] = {}
        log.info("PAIRLOCK STRATEGY R (dynamic loss-recovery, strategy/pairlock_recovery_engine.py)")
        log.info("RECOVERY_CONFIG %s | entry gate %s", " ".join(f"{k}={v}" for k, v in sorted(vars(self.rprm).items())),
                 " ".join(f"{k}={v}" for k, v in sorted(vars(self.prm).items()) if k in ("entry_edge", "entry_mode", "short_min",
                                                                                      "entry_shares", "band_lo", "band_hi")))
        if self.rprm.swing_mode:
            p = self.rprm
            sizing = (f"swing 1 / 2 = the smallest size whose win pays > TARGET_RECOVERY_PROFIT {p.swing_target_profit:.2f} "
                      f"(walked asks + fees, whole inventory; MAX_RECOVERY_SHARES {p.swing_max_recovery_shares:g}, "
                      f"MAX_RECOVERY_PRICE {p.swing_max_recovery_price:.2f})" if p.swing_sizing == "target" else
                      f"swing 1 = {p.swing1_shares:g} sh, swing 2 = {p.swing2_shares:g} sh (fixed sizes)")
            log.warning("SWING MODE (strategy/pairlock_swing.py): swing 0 = %g sh of the leader at VWAP %.2f-%.2f, %g-%g s after "
                        "the open; %s, of the new leader at ask >= %.2f (hysteresis %.2f); recovery depth %d, then %s; "
                        "MAX_MARKET_CAPITAL %.2f; MAX_SLIPPAGE %.2f, MIN_TIME_LEFT %g s, cooldown %g ms. The open rule, recovery "
                        "arithmetic, locks, even-out, late buys, ladder, profit exit, falling-side sale and pair bid are OFF in "
                        "this mode.", p.swing_initial_shares, p.swing_entry_min, p.swing_entry_max, p.swing_entry_delay_s,
                        p.swing_entry_window_s, sizing, p.swing_recovery_price, p.swing_leader_hysteresis, p.swing_max_recovery,
                        "the two-sided hedge" if p.swing_two_sided_hedge else "hold (hedge disabled)",
                        p.swing_max_market_capital, p.max_slippage, p.min_time_left_s, p.cooldown_ms)
            path = sw.plan_path(p)
            log.warning("SWING MODE worst path at the reference prices: %s", "; ".join(
                f"{r['step']} {r['shares']:g} {r['side']} -> A {r['up_to']['A']:g} / B {r['up_to']['B']:g}, cost {r['cost']:.2f}"
                for r in path))
            worst_pos = path[-1]["up_to"]["A"] + path[-1]["up_to"]["B"]
            if p.max_total_position < worst_pos - 1e-9:
                log.warning("SWING MODE: MAX_TOTAL_POSITION %g sh is under the plan's %g sh (swing 2 / the hedge will be cut "
                            "short): set TPB_PAIRLOCK_MAX_TOTAL_POSITION", p.max_total_position, worst_pos)
            if 2.0 * p.swing_max_market_capital > self.scfg.max_total_exposure_usdc + 1e-9:
                log.warning("SWING MODE: the triangle budget's total exposure %.0f USDC is under 2 x MAX_MARKET_CAPITAL (the "
                            "previous market's capital is held until its resolution): orders may be refused by the risk limits",
                            self.scfg.max_total_exposure_usdc)

    # ───────────────────────────── samples, features ─────────────────────────────

    def _record(self, meta, book_up, book_down, spot, now: float) -> None:
        m = meta.market
        price = getattr(spot, "price", None) if spot is not None else None
        srcs = getattr(spot, "sources", None) if spot is not None else None
        self.close_ts[m.slug] = m.close_ts
        sh = self.spot_hist.setdefault(m.asset, plv2.History(keep_ms=420_000.0))
        if price and (not sh.t or now - sh.t[-1] >= 200.0):
            sh.append(now, None, None, None, None, 0.0, 0.0, price)
        h = self.hist.setdefault(m.slug, plv2.History(keep_ms=15_000.0))
        ub = None if book_up is None else book_up.best_bid
        db = None if book_down is None else book_down.best_bid
        self.last_bids[m.slug] = (ub, db)
        self.quotes[m.slug] = (ub, None if book_up is None else book_up.best_ask, db,
                               None if book_down is None else book_down.best_ask)
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
        s = plv2.realized_sigma(self.spot_hist.get(m.asset, plv2.History()), m.open_ts, 300.0)
        if s is None:
            s = self.last_shock_sigma.get(m.asset)
            if not s:
                return None
            self.stats["sigma_fallbacks"] += 1
        self.sigma[m.slug] = s
        return s

    def _features(self, meta, now: float) -> dict | None:
        m = meta.market
        h = self.hist.get(m.slug)
        sig = self._sigma(m, now)
        if h is None or len(h) == 0 or sig is None or m.strike <= 0.0:
            return None
        f = plv2.features(h, len(h) - 1, m.strike, sig, m.close_ts)
        if f is not None:
            self.last_p[m.slug] = self.plmodel.p_up(f)
        return f

    def _refuse_pl(self, ev: Evaluation, why: str, reason: Reason) -> Evaluation:
        f = (ev.features or {}).get(STRATEGY_ID) or {}
        self._hollow(self._mark_of(ev.slug, f.get("side", ""), f.get("t")), f"not bought: {why}")
        ev.side = ev.token_id = None
        return self._reject(ev, reason, why)

    # ───────────────────────────── the evaluation ─────────────────────────────

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        m = meta.market
        self._balance = float(balance or 0.0)
        self._record(meta, book_up, book_down, spot, now)
        self._finalize(m.slug, now)
        self.candidates.pop(m.slug, None)
        ev = super().evaluate(meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                              balance, orderflow, quote_ages)
        cyc = self.cycles.get(m.slug)
        left = m.seconds_remaining(now)
        if self.rprm.swing_mode:
            return self._swing(meta, book_up, book_down, spot_age_ms, quote_ages, now, ev, cyc, left)
        if (self.rprm.late_enabled and self.rprm.late_min_left_s <= left <= self.rprm.late_window_s and m.slug not in self.pending
                and book_up is not None and book_down is not None and not self.accounting_halt):
            late = self._late(meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc)
            if late is not None:
                if ev.tradeable:
                    self._refuse_pl(ev, "the late buy goes first", Reason.ALREADY_POSITIONED)
                return late
        if cyc is not None and cyc.holds and not self._can_add(cyc):   # a fully paired market may buy again (conditions)
            if (self.rprm.ulr_enabled and cyc.ulr is not None and m.slug not in self.pending and cyc.state is rc.RState.HOLDING
                    and book_up is not None and book_down is not None and not self.accounting_halt):
                u = self._ulr_entry(meta, book_up, book_down, spot_age_ms, now, cyc)
                if u is not None:
                    if ev.tradeable:
                        self._refuse_pl(ev, "the unfilled-leg recovery decides this market", Reason.ALREADY_POSITIONED)
                    return u
            if (self.rprm.cond_high_buy and self.rprm.cond_high_max > 0.0 and self._high_ready(cyc, m.slug) and not self.accounting_halt
                    and book_up is not None and book_down is not None and self.rprm.cond_min_left_s <= left):
                op = self._open(meta, book_up, book_down, now, getattr(spot, "price", None) if spot is not None else None,
                                high_only=True)
                if op is not None:
                    if ev.tradeable:
                        self._refuse_pl(ev, "the high band buys in this market", Reason.ALREADY_POSITIONED)
                    return op
            if (self.rprm.pair_rescue_price > 0.0 and m.slug not in self.pending and cyc.state is rc.RState.HOLDING
                    and book_up is not None and book_down is not None and not self.accounting_halt):
                r = self._rescue(meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc)
                if r is not None:
                    if ev.tradeable:
                        self._refuse_pl(ev, "the pair rescue buys in this market", Reason.ALREADY_POSITIONED)
                    return r
            gate = self._recovery_gate(cyc)
            if gate and ev.tradeable:
                return self._refuse_pl(ev, gate, Reason.ALREADY_POSITIONED)
            if (not gate and m.slug not in self.pending and cyc.state is rc.RState.HOLDING and book_up is not None
                    and book_down is not None):
                rev = self._recover(meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc)
                if rev is None and self.rprm.ladder_window_s > 0.0 and not self.accounting_halt:
                    rev = self._ladder(meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc)
                if rev is not None:
                    if ev.tradeable:
                        self._refuse_pl(ev, "REENTRY_BLOCKED: the recovery decides every buy in this market", Reason.ALREADY_POSITIONED)
                    return rev
            if ev.tradeable:
                self.stats["reentry_blocked"] += 1
                return self._refuse_pl(ev, f"REENTRY_BLOCKED: the market holds {cyc.book.up_qty:g} UP / "
                                           f"{cyc.book.down_qty:g} DOWN; only the recovery arithmetic buys here",
                                       Reason.ALREADY_POSITIONED)
            return ev
        if self.rprm.entry_mode in ("open", "conditions", "low"):
            cond = self.rprm.entry_mode == "conditions"
            low = self.rprm.entry_mode == "low"
            if ev.tradeable:
                ev = self._refuse_pl(ev, "conditions mode: only the conditions rule buys" if cond else
                                     "low-price mode: only the low-price first leg buys" if low else
                                     f"open mode: the first buy is only {self.rprm.open_delay_s:g}-{self.rprm.open_window_s:g} s after the open",
                                     Reason.ALREADY_POSITIONED)
            elapsed = (now - m.open_ts) / 1000.0
            in_window = (self.rprm.low_min_left_s <= left <= self.rprm.low_max_left_s if low else
                         now >= m.open_ts if cond else self.rprm.open_delay_s <= elapsed <= self.rprm.open_window_s)
            if ((cyc is None or self._can_add(cyc)) and m.slug not in self.pending and not self.accounting_halt
                    and book_up is not None and book_down is not None and in_window):
                op = self._open(meta, book_up, book_down, now, getattr(spot, "price", None) if spot is not None else None)
                if op is not None:
                    return op
            return ev
        if not ev.tradeable:
            return ev
        if self.accounting_halt:
            return self._refuse_pl(ev, f"accounting halt: {self.accounting_halt}", Reason.RISK_LIMIT)
        if m.slug in self.pending or (cyc is not None and cyc.state is rc.RState.INITIAL_ENTRY):
            return self._refuse_pl(ev, "an order is in flight in this market", Reason.ALREADY_POSITIONED)
        return self._gate_entry(ev, meta, book_up, book_down, now)

    def _gate_entry(self, ev: Evaluation, meta, book_up, book_down, now: float) -> Evaluation:
        m = meta.market
        side = ev.side
        book = book_up if side == "UP" else book_down
        ask, bid = book.best_ask, book.best_bid
        f = self._features(meta, now)
        if f is None:
            return self._refuse_pl(ev, "pairlock features not ready (history / sigma)", Reason.MODEL_NOT_READY)
        prm = self.prm if self.plcfg.entry_gate else replace(self.prm, entry_edge=-1.0)
        dec, why = plv2.entry_decision(f, side, ask, bid, _levels(book.asks), self.plmodel, prm, self.lim,
                                       plv2.MarketExposure(), m.seconds_remaining(now), None)
        if dec is None:
            self.stats["entries_refused"] += 1
            return self._refuse_pl(ev, why, Reason.EDGE_TOO_SMALL if "EV" in why or "favourable" in why else Reason.LOW_LIQUIDITY)
        size = dec.shares
        limit = min(ev.limit_price, dec.limit)
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit, add_to_open=True)
        if not verdict.ok:
            return self._refuse_pl(ev, f"risk: {verdict.detail}", Reason(verdict.reason))
        cyc = rc.RecoveryCycle(book=pb.PairBook(m.slug), a_side=side, started_t=now, target=self.rprm.target_profit)
        self.candidates[m.slug] = cyc
        tri = ev.features.get(STRATEGY_ID) or {}
        ev.size, ev.limit_price, ev.max_entry_price = size, limit, limit
        tri.update({"size": size, "limit": limit, "pairlock": {"kind": "entry", "p_win": round(dec.p, 4), "ev": round(dec.ev, 4),
                                                               "vwap": round(dec.info["vwap"], 4)}})
        ev.size_note = f"{size:g} sh, EV {dec.ev:+.3f}/sh (P {dec.p:.3f} at VWAP {dec.info['vwap']:.3f})"
        return ev

    @property
    def manages_exits(self) -> bool:
        """live.py asks the engine for sells (exit_actions): the unfilled-leg recovery's, and the profit exit's."""
        return (bool(self.rprm.swing_mode) or bool(self.rprm.ulr_enabled) or self.rprm.profit_exit > 0.0
                or bool(self.rprm.recovery_sell))

    def _recovery_gate(self, cyc: rc.RecoveryCycle) -> str:
        """The open rule: while the other side's bid rests (or once it filled: the pair is locked), the recovery
        arithmetic does not buy. It takes over only after pair_cancels pulled the bid (the other side became the
        higher price). '' = the recovery may run."""
        if self.rprm.ulr_enabled:
            return "unfilled-leg recovery mode: the strategy-R arithmetic does not buy"
        if not self.rprm.recovery_enabled:
            return "the loss recovery is off in this bot"
        if not cyc.open_entry or cyc.pair is None:
            return ""
        st = cyc.pair.get("state")
        if st in ("working", "repricing", "reprice_sent"):
            return (f"the {cyc.pair['side']} pair bid {cyc.pair['size']:g} sh at {cyc.pair['price']:.2f} is working "
                    f"(sum {self.rprm.pair_sum:.2f}); recovery only once it is pulled")
        if st == "filled":
            return f"pair locked: {cyc.pair['side']} {cyc.pair.get('filled', 0):g} sh at {cyc.pair['price']:.2f}"
        return ""

    def _can_add(self, cyc: rc.RecoveryCycle) -> bool:
        """The conditions rule buys again in a market only when every held token is paired (the user, 10-02 21:00)."""
        h = cyc.late_hedge
        return (self.rprm.entry_mode == "conditions" and cyc.state is rc.RState.HOLDING and not cyc.book.pending
                and rc.fully_paired(cyc) and rc.entry_count(cyc) < self.rprm.cond_max_entries
                and (h is None or h.get("state") not in ("sent", "working")))

    def _rise(self, m, now: float) -> tuple[dict | None, float | None]:
        """The rising condition's inputs: each token's mid change over the short / long / last windows, and BTC's move."""
        prm = self.rprm
        if prm.cond_rising_window_s <= 0.0:
            return None, None
        h = self.hist.get(m.slug)
        rise = {s: {k: (ulr.momentum(h, s, now, w * 1000.0) if w > 0.0 else None)
                    for k, w in (("short", prm.cond_rising_window_s), ("long", prm.cond_rising_long_s),
                                 ("last", prm.cond_rising_last_s))} for s in ("UP", "DOWN")}
        w = max(prm.cond_rising_long_s, prm.cond_rising_window_s)
        spot = ulr.spot_delta(self.spot_hist.get(m.asset), now, w * 1000.0) if prm.cond_rising_spot else None
        return rise, spot

    def _high_ready(self, cyc: rc.RecoveryCycle, slug: str) -> bool:
        """The high band may buy (again) here: its last order is done (filled), fewer than cond_high_max_entries filled."""
        return (self.rprm.entry_mode == "conditions" and self.rprm.cond_high_buy and self.rprm.cond_high_max > 0.0
                and cyc.state is rc.RState.HOLDING
                and not cyc.book.pending and slug not in self.pending
                and self._high_count.get(slug, 0) < self.rprm.cond_high_max_entries
                and (not self.rprm.cond_high_pair_bid or cyc.pair is None or cyc.pair.get("state") == "filled"))

    def _open(self, meta, book_up, book_down, now: float, spot_price: float | None = None,
              high_only: bool = False) -> Evaluation | None:
        """THE OPEN RULE's first leg (in the first open_window_s seconds, buy the higher-priced token) or, in
        conditions mode, THE CONDITIONS RULE's (pairlock_recovery.conditions_entry_side)."""
        m = meta.market
        prm = self.rprm
        if self._open_tries.get(m.slug, 0) >= (prm.low_max_tries if prm.entry_mode == "low" else prm.open_max_tries):
            return None
        mv = MarketView(bid_up=book_up.best_bid, ask_up=book_up.best_ask, bid_down=book_down.best_bid, ask_down=book_down.best_ask,
                        asks_up=_levels(book_up.asks), asks_down=_levels(book_down.asks), bids_up=_levels(book_up.bids),
                        bids_down=_levels(book_down.bids))
        if prm.entry_mode == "low":
            # THE LOW-PRICE FIRST LEG (the user's spec, 2026-10-07): the cheaper token, through strategy B's EV gate
            side, lrec = rc.low_entry_side(mv, prm, m.seconds_remaining(now), self._features(meta, now), self.plmodel, self.prm)
            self.stats["low_evaluations"] += 1
            if side is None:
                self._log_eval(m.slug, {"t": now, **lrec})
                return None
            self.stats["low_candidates"] += 1
            return self._open_order(meta, book_up, book_down, now, mv, side, lrec["why"])
        if prm.entry_mode == "conditions":
            rise, spot_move = self._rise(m, now)
            cyc_now = self.cycles.get(m.slug)
            since_open = (now - m.open_ts) / 1000.0
            if cyc_now is None and since_open < prm.cond_first_buy_delay_s - 1e-9:
                return None                                       # the first buy waits cond_first_buy_delay_s after the open
            if prm.cond_open_buy and cyc_now is None and not high_only:
                oside, owhy = rc.conditions_open_side(mv, prm, (now - m.open_ts) / 1000.0, rise, spot_move)
                if oside is not None:
                    return self._open_order(meta, book_up, book_down, now, mv, oside, owhy)
            if prm.cond_high_buy and prm.cond_high_max > 0.0 and (cyc_now is None or self._high_ready(cyc_now, m.slug)):
                hside, hwhy = rc.conditions_high_side(mv, prm, m.seconds_remaining(now), rise, spot_move)
                if hside is not None:
                    return self._open_order(meta, book_up, book_down, now, mv, hside, hwhy, high=True)
                if high_only:
                    self._log_eval(m.slug, {"t": now, "kind": "conditions_high", "decision": "REJECTED", "why": hwhy,
                                            "up_ask": mv.ask_up, "down_ask": mv.ask_down})
                    return None
            elif high_only:
                return None
            if not prm.cond_after_buy:
                return None                                       # the after-open (regular conditions) buy is switched off
            elapsed = (now - m.open_ts) / 1000.0
            if elapsed < prm.cond_after_delay_s - 1e-9:
                self._log_eval(m.slug, {"t": now, "kind": "conditions", "decision": "REJECTED", "up_ask": mv.ask_up,
                                        "down_ask": mv.ask_down, "why": f"after-open buy: {elapsed:.0f} s after the open < "
                                                                        f"{prm.cond_after_delay_s:g} s"})
                return None
            side, cwhy = rc.conditions_entry_side(mv, prm, m.seconds_remaining(now), spot_price, m.strike, 0.0, rise, spot_move)
            if side is None:
                self._log_eval(m.slug, {"t": now, "kind": "conditions", "decision": "REJECTED", "why": cwhy,
                                        "up_ask": mv.ask_up, "down_ask": mv.ask_down, "spot": spot_price, "strike": m.strike})
                return None
            return self._open_order(meta, book_up, book_down, now, mv, side, f"conditions rule: {cwhy}")
        side, why = rc.open_entry_side(mv, prm)
        if side is None:
            self._log_eval(m.slug, {"t": now, "kind": "open", "decision": "REJECTED", "why": why,
                                    "up_ask": mv.ask_up, "down_ask": mv.ask_down})
            return None
        if mv.ask_up == mv.ask_down and mv.bid_up == mv.bid_down:
            self._log_eval(m.slug, {"t": now, "kind": "open", "decision": "REJECTED", "up_ask": mv.ask_up,
                                    "down_ask": mv.ask_down, "why": "UP and DOWN at the same price: no higher token yet"})
            return None
        if mv.ask_up == mv.ask_down:
            side = "UP" if (mv.bid_up or 0.0) > (mv.bid_down or 0.0) else "DOWN"
        return self._open_order(meta, book_up, book_down, now, mv, side,
                                f"open rule: {side} is the higher token (ask {mv.ask(side):.2f} vs {mv.ask(rc.OTHER[side]):.2f}) "
                                f"{(now - m.open_ts) / 1000.0:.1f} s after the open")

    def _open_order(self, meta, book_up, book_down, now: float, mv: MarketView, side: str, why: str,
                    high: bool = False) -> Evaluation | None:
        m = meta.market
        prm = self.rprm
        ask = mv.ask(side)
        limit = min(0.99, round(ask + prm.open_limit_offset, 2))
        q = float(prm.open_shares)
        if m.min_size and q + 1e-9 < float(m.min_size):
            q = float(m.min_size)
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), q * limit, add_to_open=True)
        if not verdict.ok:
            self._log_eval(m.slug, {"t": now, "kind": "open", "decision": "REJECTED", "why": f"risk: {verdict.detail}"})
            return None
        free = max(0.0, self._balance - sum(p.get("q", 0.0) * p.get("limit", 0.0) for p in self.pending.values()))
        if q * limit > free + 1e-9:
            self._log_eval(m.slug, {"t": now, "kind": "open", "decision": "REJECTED", "why": f"cost {q * limit:.2f} > free {free:.2f}"})
            return None
        cyc = self.cycles.get(m.slug)
        add_on = cyc is not None
        if high:
            why = (f"high band buy #{self._high_count.get(m.slug, 0) + 1}"
                   + ("" if prm.cond_high_pair_bid else " (no pair bid)") + f": {why}")
        elif add_on:
            why = f"add-on #{rc.entry_count(cyc) + 1} (all {cyc.book.up_qty:g} UP / {cyc.book.down_qty:g} DOWN paired): {why}"
        if not add_on:
            cyc = rc.RecoveryCycle(book=pb.PairBook(m.slug), a_side=side, started_t=now, target=prm.target_profit, open_entry=True)
        self.candidates[m.slug] = cyc
        o = rc.outcomes(cyc.book)
        pair_px = rc.pair_bid_price(prm, ask)
        reason = why if high and not prm.cond_high_pair_bid else (f"{why}; then a {rc.OTHER[side]} bid at {prm.pair_sum:.2f} - fill"
                                                                  + (f" (~{pair_px:.2f})" if pair_px is not None else ""))
        rec = {"t": now, "kind": prm.entry_mode, "decision": "BUY", "why": reason, "up_ask": mv.ask_up, "down_ask": mv.ask_down,
               "pnl_up_win": o["pnl_up_win"], "pnl_down_win": o["pnl_down_win"],
               "after_pnl_up_win": o["pnl_up_win"] + q * (1.0 if side == "UP" else 0.0) - q * ask,
               "after_pnl_down_win": o["pnl_down_win"] + q * (1.0 if side == "DOWN" else 0.0) - q * ask, "edge_per_share": 0.0,
               "high_band": high}
        self._log_eval(m.slug, rec)
        dec = rc.RecoveryDecision(side, q, limit, ask, q, reason, rec, kind="entry")
        f = self._features(meta, now)
        # in the first second of a market the model has no features yet: the UP mid stands in (live.py logs p_used)
        p_up = self.plmodel.p_up(f) if f is not None else rc._mid(mv.bid_up, mv.ask_up)
        return self._order_evaluation(meta, book_up, book_down, now, cyc, dec, rec, p_up, "open")

    def _recover(self, meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc: rc.RecoveryCycle) -> Evaluation | None:
        m = meta.market
        qa = quote_ages or {}
        mv = MarketView(bid_up=book_up.best_bid, ask_up=book_up.best_ask, bid_down=book_down.best_bid, ask_down=book_down.best_ask,
                        asks_up=_levels(book_up.asks), asks_down=_levels(book_down.asks), bids_up=_levels(book_up.bids),
                        bids_down=_levels(book_down.bids), quote_age_up_ms=qa.get(m.yes_token_id),
                        quote_age_down_ms=qa.get(m.no_token_id), spot_age_ms=spot_age_ms)
        fresh = ((spot_age_ms is None or spot_age_ms <= self.lim.max_spot_age_ms)
                 and all(x <= self.lim.max_quote_age_ms for x in (mv.quote_age_up_ms, mv.quote_age_down_ms) if x is not None))
        f = self._features(meta, now)
        p_up = None if f is None else self.plmodel.p_up(f)
        free = max(0.0, self._balance - sum(p.get("q", 0.0) * p.get("limit", 0.0) for p in self.pending.values()))
        dec, rec = rc.evaluate_recovery(cyc, p_up, f, mv, now, m.seconds_remaining(now), self.plmodel, self.rprm, free, fresh)
        if rec.get("decision") == "LOCKED":
            self.stats["locked_markets"] += 1
            log.warning("[LOCKED] %s %s", m.slug, rec["why"])
        if "confirmations" in rec or rec.get("decision") in ("LOCK", "LOCKED"):
            self.stats["evaluations"] += 1
            if rec.get("losing_side_move", -1.0) >= self.rprm.min_price_move and rec.get("confirmations", 0) >= self.rprm.min_confirmations:
                self.stats["opportunities"] += 1
            self._log_eval(m.slug, rec)
            if rec["decision"] not in ("BUY", "LOCK", "LOCKED"):
                key = rec["why"].split("(")[0][:40]
                self.rejections[key] = self.rejections.get(key, 0) + 1
        if len(cyc.events) > 200:                                   # the full record is in the log; keep the cycle light
            del cyc.events[:-120]
        if dec is None:
            return None
        side = dec.side
        book = book_up if side == "UP" else book_down
        tok = m.yes_token_id if side == "UP" else m.no_token_id
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=m.seconds_remaining(now), reason=None)
        info = {"signal": True, "direction": side, "side": side, "t": round(now), "trigger": "recovery", "size": dec.shares,
                "ask": book.best_ask, "bid": book.best_bid, "limit": dec.limit,
                "pairlock": {"kind": dec.kind, "side": side, "q": dec.shares, "plan_qty": dec.plan_qty, "vwap": round(dec.vwap, 4),
                             "pnl_up_win": round(rec["pnl_up_win"], 4), "pnl_down_win": round(rec["pnl_down_win"], 4),
                             "after_pnl_up_win": round(rec["after_pnl_up_win"], 4),
                             "after_pnl_down_win": round(rec["after_pnl_down_win"], 4),
                             "expected_improvement": rec.get("expected_improvement"), "improves_min": rec["improves_min"]}}
        ev.features = {STRATEGY_ID: info, "execution": {"best_ask": book.best_ask, "best_bid": book.best_bid, "cap": dec.limit,
                                                         "spread": None if book.best_bid is None else round(book.best_ask - book.best_bid, 4)}}
        p_side = None if p_up is None else (p_up if side == "UP" else 1.0 - p_up)
        ev.sides = [SideView(side=side, token_id=tok, p_model=p_side if p_side is not None else book.best_ask, p_market=book.best_ask,
                             ask=book.best_ask, bid=book.best_bid, depth_at_limit=_depth(book.asks, book.best_ask, True), costs=None,
                             reason=None, entry_px=book.best_ask)]
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, tok, dec.limit, dec.vwap
        ev.max_entry_price = dec.limit
        ev.size, ev.net_edge, ev.confidence = dec.shares, float(rec.get("edge_per_share") or 0.0), 1.0
        ev.p_used = p_up
        ev.size_note = f"recovery {dec.shares:g} sh of {side}: {dec.reason}"
        mk = {"t": round(now), "dir": 1 if side == "UP" else -1, "z1": None, "z3": None, "r1_bps": None, "trigger": "recovery",
              "ok": True, "why": "", "recovery": dict(info["pairlock"])}
        self.shock_marks.setdefault(m.slug, deque(maxlen=self.SHOCK_MARKS_PER_MARKET)).append(mk)
        self.decisions.append({"t": round(now), "slug": m.slug, "action": "recovery", "side": side, "q": dec.shares,
                               "plan_qty": dec.plan_qty, "after_pnl_up_win": info["pairlock"]["after_pnl_up_win"],
                               "after_pnl_down_win": info["pairlock"]["after_pnl_down_win"]})
        self._decisions[m.slug] = dec
        self.last[m.slug] = ev
        return ev

    # ───────────────────────────── swing mode (pairlock_swing, the user's 2026-10-07 spec) ─────────────────────────────

    @staticmethod
    def _view(m, book_up, book_down, spot_age_ms=None, quote_ages=None) -> MarketView:
        qa = quote_ages or {}
        return MarketView(bid_up=book_up.best_bid, ask_up=book_up.best_ask, bid_down=book_down.best_bid, ask_down=book_down.best_ask,
                          asks_up=_levels(book_up.asks), asks_down=_levels(book_down.asks), bids_up=_levels(book_up.bids),
                          bids_down=_levels(book_down.bids), quote_age_up_ms=qa.get(m.yes_token_id),
                          quote_age_down_ms=qa.get(m.no_token_id), spot_age_ms=spot_age_ms)

    def _free(self) -> float:
        return max(0.0, self._balance - sum(p.get("q", 0.0) * p.get("limit", 0.0) for p in self.pending.values()))

    def _swing(self, meta, book_up, book_down, spot_age_ms, quote_ages, now, ev, cyc, left) -> Evaluation:
        """SWING MODE: swing 0's entry, each swing's directional recovery and the swing-3+ hedge (pairlock_swing).
        Nothing else buys or sells in this mode. The leader / swings are tracked at every sample, also while an order
        is in flight; orders go out only with none in flight and fresh quotes."""
        m = meta.market
        prm = self.rprm
        if ev.tradeable:
            ev = self._refuse_pl(ev, "swing mode: only the swing strategy buys", Reason.ALREADY_POSITIONED)
        if book_up is None or book_down is None:
            return ev
        mv = self._view(m, book_up, book_down, spot_age_ms, quote_ages)
        fresh = all(x <= self.lim.max_quote_age_ms for x in (mv.quote_age_up_ms, mv.quote_age_down_ms) if x is not None)
        st = cyc.swing if cyc is not None else None
        can_act = fresh and not self.accounting_halt and m.slug not in self.pending
        if st is not None and fresh and (not can_act or cyc.state is not rc.RState.HOLDING):
            new = sw.observe(st, mv, now)
            if new is not None:
                self._swing_seen(m.slug, cyc, new)
        if not can_act:
            return ev
        elapsed = (now - m.open_ts) / 1000.0
        free = self._free()
        if cyc is None or (not cyc.holds and not cyc.book.fills):
            if now < m.open_ts or self._open_tries.get(m.slug, 0) >= prm.open_max_tries:
                return ev
            dec, rec = sw.entry_decision(mv, elapsed, left, prm, free, now=now)
            self._log_swing(m.slug, rec)
            if dec is None:
                return ev
            verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), dec.shares * dec.limit, add_to_open=True)
            if not verdict.ok:
                self._log_swing(m.slug, {**rec, "decision": "REJECTED", "why": f"risk: {verdict.detail}"})
                return ev
            cyc = rc.RecoveryCycle(book=pb.PairBook(m.slug), a_side=dec.side, started_t=now, target=0.0, open_entry=True)
            cyc.swing = sw.SwingState.start(dec.side, now, prm)
            return self._swing_order(meta, book_up, book_down, now, cyc, dec, rec, mv)
        if st is None or cyc.state is not rc.RState.HOLDING:
            return ev                                       # (a cycle restored from before swing mode is held as it is)
        dec, rec = sw.evaluate(st, cyc.book, mv, now, left, elapsed, prm, free)
        if rec.get("new_swing"):
            self._swing_seen(m.slug, cyc, rec["new_swing"])
        self._log_swing(m.slug, rec)
        if dec is None:
            return ev
        return self._swing_order(meta, book_up, book_down, now, cyc, dec, rec, mv)

    def _swing_order(self, meta, book_up, book_down, now, cyc, dec: "sw.SwingOrder", rec: dict, mv: MarketView) -> Evaluation:
        m = meta.market
        if dec.kind == "entry":
            self.candidates[m.slug] = cyc                   # to_order's open-buy path takes it from here
        rd = rc.RecoveryDecision(dec.side, dec.shares, dec.limit, dec.vwap, dec.plan_qty, dec.reason,
                                 {**rec, "swing_number": dec.swing}, kind=dec.kind)
        p_up = sw.mids(mv)["UP"]                             # no model in this strategy: the UP mid is the probability shown
        if p_up is None:
            p_up = dec.vwap if dec.side == "UP" else 1.0 - dec.vwap
        return self._order_evaluation(meta, book_up, book_down, now, cyc, rd, rd.record, p_up,
                                      "open" if dec.kind == "entry" else "recovery")

    def _swing_seen(self, slug: str, cyc: rc.RecoveryCycle, row: dict) -> None:
        """A swing was recognised: always logged (it changes what the market may buy)."""
        st, b = cyc.swing, cyc.book
        self.stats["swing_recognised"] += 1
        cyc.events.append({"event": "SWING", **row})
        log.warning("[SWING] %s swing %d: %s -> %s (%s ask %.2f, mid %.3f vs %.3f) => %s, action: %s | UP %g / DOWN %g, cost %.2f: "
                    "UP-win %+.2f DOWN-win %+.2f", slug, row["n"], row["from"], row["to"], row["to"], row["ask"],
                    row.get("mid_to") or 0.0, row.get("mid_from") or 0.0, st.phase.value,
                    row.get("action") + (f" {row['target']:g} {row['to']}" if row.get("target") else ""),
                    b.up_qty, b.down_qty, b.total_cost, b.up_qty - b.total_cost, b.down_qty - b.total_cost)

    def _log_swing(self, slug: str, rec: dict) -> None:
        """[SWING_EVAL] JSON per evaluation; a reason repeating within LOG_REPEAT_MS (numbers aside) is counted."""
        import re
        key = (slug, "swing", rec.get("decision"), rec.get("phase"), re.sub(r"[-+]?\d[\d.]*", "#", str(rec.get("why")))[:70])
        last = self._repeat.get(key)
        if rec.get("decision") != "BUY" and last is not None and rec["t"] - last[0] < self.LOG_REPEAT_MS:
            last[1] += 1
            return
        n_sup = 0 if last is None else last[1]
        self._repeat[key] = [rec["t"], 0]
        row = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in rec.items()}
        row["market"] = slug
        if n_sup:
            row["repeated"] = n_sup
        log.info("[SWING_EVAL] %s", json.dumps(row, default=str))

    def _swing_state_log(self, slug: str, cyc: rc.RecoveryCycle, mv: MarketView | None = None) -> dict:
        acc = sw.accounting(cyc.swing, cyc.book, self.rprm, mv)
        log.info("[SWING_STATE] %s %s", slug, json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in acc.items()},
                                                          default=str))
        return acc

    def _rescue(self, meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc: rc.RecoveryCycle) -> Evaluation | None:
        """THE PAIR RESCUE (the user, 2026-10-08): shares are unpaired (the opposite bid has not filled them) and the
        token they lack is priced (mid) above pair_rescue_price -> buy that token for exactly the unpaired shares, so
        UP = DOWN. Waits while the bid still works (pair_cancels pulls it first). A kind "lock" order: fill-and-kill
        within MAX_SLIPPAGE of the ask, MAX_RECOVERY_ORDER_SIZE per order, the rest on the next evaluation."""
        m, prm, b = meta.market, self.rprm, cyc.book
        gap = b.up_qty - b.down_qty
        need = math.floor(abs(gap) * 100.0 + 1e-6) / 100.0
        if need < prm.min_size - 1e-9:
            return None
        if cyc.pair is not None and cyc.pair.get("state") in ("working", "repricing", "reprice_sent"):
            return None
        light = "DOWN" if gap > 0 else "UP"
        mv = self._view(m, book_up, book_down, spot_age_ms, quote_ages)
        mid = rc._mid(mv.bid(light), mv.ask(light))
        if mid is None or mid <= prm.pair_rescue_price + 1e-9:
            return None
        if m.seconds_remaining(now) < prm.min_time_left_s or now - cyc.last_action_t < prm.cooldown_ms:
            return None
        o = rc.outcomes(b)
        rec = {"t": now, "kind": "pair_rescue", "decision": "REJECTED", "side": light, "mid": mid, "unpaired": need,
               "up_qty": b.up_qty, "down_qty": b.down_qty, "pnl_up_win": o["pnl_up_win"], "pnl_down_win": o["pnl_down_win"]}
        st = rc._evening_step(light, need, mv, prm, self._free(), rec)
        if isinstance(st, str):
            self._log_eval(m.slug, {**rec, "why": f"pair rescue: {st}"})
            return None
        q, vw, fps, limit = st
        step = rc.simulate_buy(b, light, q, vw, fps, prm.slippage_allowance, None)
        reason = (f"PAIR RESCUE: {light} mid {mid:.3f} > {prm.pair_rescue_price:.2f} with {need:g} sh unpaired "
                  f"(UP {b.up_qty:g} / DOWN {b.down_qty:g}): buy {q:g} {light} at VWAP {vw:.3f} -> UP-win {step['pnl_up_win']:+.2f} "
                  f"DOWN-win {step['pnl_down_win']:+.2f}")
        rec.update({"decision": "LOCK", "why": reason, "limit": limit, "order_qty": q, "after_pnl_up_win": step["pnl_up_win"],
                    "after_pnl_down_win": step["pnl_down_win"], "improves_min": True, "lock_reason": "pair rescue"})
        self._log_eval(m.slug, rec)
        cyc.lock_reason = cyc.lock_reason or f"pair rescue above {prm.pair_rescue_price:.2f}"
        dec = rc.RecoveryDecision(light, q, limit, vw, need, reason, rec, kind="lock")
        return self._order_evaluation(meta, book_up, book_down, now, cyc, dec, rec, rc._mid(mv.bid_up, mv.ask_up), "recovery")

    # ───────────────────────────── the unfilled-leg recovery (pairlock_ulr) ─────────────────────────────

    def _ulr_signals(self, m, now: float) -> tuple[float | None, float | None, float | None, str]:
        """(opposite momentum, first-leg momentum, spot move) over the momentum window, for the market's ULR."""
        c = self.cycles.get(m.slug)
        u = ulr.ULR.from_json(c.ulr) if c is not None and c.ulr else None
        if u is None:
            return None, None, None, ""
        win = self.rprm.ulr_momentum_window_s * 1000.0
        h = self.hist.get(m.slug)
        return (ulr.momentum(h, u.recovery_leg, now, win), ulr.momentum(h, u.original_leg, now, win),
                ulr.spot_delta(self.spot_hist.get(m.asset), now, win), u.recovery_leg)

    def _ulr(self, cyc: rc.RecoveryCycle) -> ulr.ULR | None:
        return ulr.ULR.from_json(cyc.ulr) if cyc.ulr else None

    def _ulr_save(self, cyc: rc.RecoveryCycle, u: ulr.ULR) -> None:
        cyc.ulr = u.to_json()

    def _ulr_exposure(self) -> float:
        out = 0.0
        for c in self.cycles.values():
            u = self._ulr(c)
            if u is not None and u.state in (ulr.RECOVERY_OPEN, ulr.RECOVERY_EXIT_PENDING, ulr.RECOVERY_ENTRY_PENDING):
                out += u.entry_gross - u.exit_proceeds if u.entry_qty > 0 else 0.0
        return max(0.0, out)

    def _ulr_count(self, code: str) -> None:
        self.ulr_reasons[code] = self.ulr_reasons.get(code, 0) + 1

    def _ulr_entry(self, meta, book_up, book_down, spot_age_ms, now, cyc: rc.RecoveryCycle) -> Evaluation | None:
        m = meta.market
        u = self._ulr(cyc)
        if u is None or u.state == ulr.HEDGED:
            return None
        if u.state in (ulr.RECOVERY_CLOSED, ulr.RECOVERY_OPEN, ulr.RECOVERY_EXIT_PENDING):
            if not u.reasons or u.reasons[-1][1] != ulr.R_ALREADY_USED:      # said once per position, not every tick
                u.note(now, ulr.R_ALREADY_USED, f"{u.state}: {u.entries} recovery entry on this position (max {self.rprm.ulr_max_entries})")
                self._ulr_count(ulr.R_ALREADY_USED)
                self._ulr_save(cyc, u)
            return None
        mv = MarketView(bid_up=book_up.best_bid, ask_up=book_up.best_ask, bid_down=book_down.best_bid, ask_down=book_down.best_ask,
                        asks_up=_levels(book_up.asks), asks_down=_levels(book_down.asks), bids_up=_levels(book_up.bids),
                        bids_down=_levels(book_down.bids), spot_age_ms=spot_age_ms)
        mom_opp, mom_orig, spot_d, _ = self._ulr_signals(m, now)
        free = max(0.0, self._balance - sum(p.get("q", 0.0) * p.get("limit", 0.0) for p in self.pending.values()))
        dec, rec = ulr.evaluate_entry(u, cyc.book, mv, now, m.seconds_remaining(now), self.rprm, mom_opp, mom_orig, spot_d,
                                      spot_age_ms, float(self.lim.max_spot_age_ms), free, self._ulr_exposure(),
                                      float(m.min_size or self.rprm.min_size))
        if u.state == ulr.UNHEDGED_RECOVERY or dec is not None:
            self.stats["ulr_candidates"] += 1
        rec["market"] = m.slug
        self._log_eval(m.slug, rec)
        self._ulr_count(rec["code"])
        if dec is None:
            self.stats["ulr_skipped"] += 1
            self._ulr_save(cyc, u)
            return None
        self.stats["ulr_signals"] += 1
        self._ulr_save(cyc, u)
        o = rc.outcomes(cyc.book)
        step = rc.simulate_buy(cyc.book, dec["side"], dec["q"], dec["vwap"], rec["fee_in"], 0.0, None)
        rec.update({"pnl_up_win": o["pnl_up_win"], "pnl_down_win": o["pnl_down_win"], "after_pnl_up_win": step["pnl_up_win"],
                    "after_pnl_down_win": step["pnl_down_win"], "improves_min": step["min_settlement_pnl"] > o["min_settlement_pnl"],
                    "expected_improvement": None, "edge_per_share": rec.get("edge")})
        d = rc.RecoveryDecision(dec["side"], dec["q"], dec["limit"], dec["vwap"], dec["q"], dec["why"], rec, kind="ulr")
        return self._order_evaluation(meta, book_up, book_down, now, cyc, d, rec, None, "ulr")

    def _profit_exit_actions(self, market, book_up, book_down, now: float, held: dict) -> list:
        """THE PROFIT EXIT: after a recovery, the whole position sold at the bids once its mark-to-market profit
        (every token at its bid, minus everything paid incl. fees) reaches profit_exit."""
        prm = self.rprm
        cyc = self.cycles.get(market.slug)
        if prm.profit_exit <= 0.0 or cyc is None or not cyc.holds or book_up is None or book_down is None:
            return []
        if not (cyc.recoveries or cyc.lock_steps):
            return []                                         # only after a recovery
        if now - self._pexit_t.get(market.slug, -1e18) < prm.cooldown_ms or market.slug in self.pending:
            return []
        if any(s == market.slug for s, *_ in self._pexit.values()):
            return []                                         # its sells are still in flight
        b = cyc.book
        bids = {"UP": book_up.best_bid, "DOWN": book_down.best_bid}
        qty = {"UP": min(b.up_qty, float(held.get(market.yes_token_id, b.up_qty))),
               "DOWN": min(b.down_qty, float(held.get(market.no_token_id, b.down_qty)))}
        # a side with no bid, or a bid at the 0.01 floor, is the loser near resolution: worth nothing, counted at
        # nothing, and not sold (an IOC sell at 0.01 only fails; it settles at 0 with the market)
        dust = {s: bids[s] is None or bids[s] <= PEXIT_DUST_BID + 1e-9 for s in ("UP", "DOWN")}
        sell = {s: 0.0 if dust[s] else sell_qty(qty[s], getattr(market, "min_size", 0.0)) for s in ("UP", "DOWN")}
        if all(sell[s] <= 0.0 for s in ("UP", "DOWN")):
            return []                                         # nothing the venue would take: it settles
        value = sum(qty[s] * (bids[s] or 0.0) for s in ("UP", "DOWN") if not dust[s])
        fees_out = sum(qty[s] * pb.fee_per_share(bids[s] or 0.0) for s in ("UP", "DOWN") if not dust[s])
        mtm = value - fees_out - b.total_cost + b.realised_exit_pnl
        if mtm < prm.profit_exit - 1e-9:
            return []
        out = []
        for side in ("UP", "DOWN"):
            if sell[side] <= 0.0:
                continue
            token = market.yes_token_id if side == "UP" else market.no_token_id
            limit = max(0.01, round(bids[side] - prm.max_slippage, 2))
            out.append(ExitAction(kind="PROFIT_EXIT", leg_id=f"pexit:{market.slug}:{side}", token_id=token, side=side,
                                  shares=sell[side], price=limit, tif="IOC" if self._fak else "FOK",
                                  detail=f"profit exit: UP {qty['UP']:g} @ bid {bids['UP']} + DOWN {qty['DOWN']:g} @ bid {bids['DOWN']} = "
                                         f"{value:.2f} - fees {fees_out:.2f} - cost {b.total_cost:.2f} = {mtm:+.2f} >= {prm.profit_exit:.2f}"))
        if out:
            self._pexit_t[market.slug] = now
            self.stats["profit_exits"] += 1
            cyc.events.append({"t": now, "event": "PROFIT_EXIT_SENT", "mtm": mtm, "up": qty["UP"], "down": qty["DOWN"],
                               "bid_up": bids["UP"], "bid_down": bids["DOWN"]})
            log.warning("[PROFIT_EXIT] %s: selling UP %g @>=%.2f, DOWN %g @>=%.2f: mark-to-market %+.2f >= %.2f after %d recovery fills",
                        market.slug, qty["UP"], bids["UP"] or 0, qty["DOWN"], bids["DOWN"] or 0, mtm, prm.profit_exit, len(cyc.recoveries))
        return out

    def _recovery_sell_actions(self, market, book_up, book_down, now: float, held: dict) -> list:
        """SELL THE FALLING SIDE: after a recovery fill toward one side, the tokens held of the other side go at the bid."""
        prm = self.rprm
        cyc = self.cycles.get(market.slug)
        if not prm.recovery_sell or cyc is None or not cyc.holds or not cyc.recoveries or book_up is None or book_down is None:
            return []
        if cyc.locked or cyc.lock_steps:
            return []                                         # a lock bought that side on purpose
        if now - self._rsell_t.get(market.slug, -1e18) < prm.cooldown_ms or market.slug in self.pending:
            return []
        if any(s == market.slug for s, *_ in self._pexit.values()):
            return []
        toward = cyc.recoveries[-1]["side"]
        side = "DOWN" if toward == "UP" else "UP"
        b = cyc.book
        book = book_up if side == "UP" else book_down
        token = market.yes_token_id if side == "UP" else market.no_token_id
        qty = sell_qty(min(b.qty(side), float(held.get(token, b.qty(side)))), getattr(market, "min_size", 0.0))
        bid = book.best_bid
        if qty <= 0.0 or bid is None or bid <= PEXIT_DUST_BID + 1e-9:
            return []
        limit = max(0.01, round(bid - prm.max_slippage, 2))
        self._rsell_t[market.slug] = now
        self.stats["recovery_sells"] += 1
        cyc.events.append({"t": now, "event": "RECOVERY_SELL_SENT", "side": side, "qty": qty, "bid": bid, "toward": toward})
        log.warning("[RECOVERY_SELL] %s: selling the falling side, %g %s @>=%.2f (bid %.2f) after the recovery toward %s",
                    market.slug, qty, side, limit, bid, toward)
        return [ExitAction(kind="RECOVERY_SELL", leg_id=f"rsell:{market.slug}:{side}", token_id=token, side=side,
                           shares=qty, price=limit, tif="IOC" if self._fak else "FOK",
                           detail=f"falling side after the recovery toward {toward}: {qty:g} {side} at bid {bid:.2f}")]

    def exit_actions(self, market, book_up, book_down, now: float, held: dict, states: dict) -> list:
        """live.py (_managed_exits, every tick): the profit exit's sells, the falling side's sale after a recovery, or
        the ULR recovery position's sell. Swing mode holds every token to resolution: nothing."""
        if self.rprm.swing_mode:
            return []
        pe = self._profit_exit_actions(market, book_up, book_down, now, held)
        if pe:
            return pe
        rs = self._recovery_sell_actions(market, book_up, book_down, now, held)
        if rs:
            return rs
        cyc = self.cycles.get(market.slug)
        u = self._ulr(cyc) if cyc is not None else None
        if u is None or u.state != ulr.RECOVERY_OPEN or book_up is None or book_down is None:
            return []
        mv = MarketView(bid_up=book_up.best_bid, ask_up=book_up.best_ask, bid_down=book_down.best_bid, ask_down=book_down.best_ask,
                        bids_up=_levels(book_up.bids), bids_down=_levels(book_down.bids))
        mom_opp, mom_orig, _, _ = self._ulr_signals(market, now)
        dec, rec = ulr.evaluate_exit(u, mv, now, market.seconds_remaining(now), self.rprm, mom_opp, mom_orig,
                                     float(getattr(self.scfg, "min_time_remaining_s", 10.0) or 10.0))
        self._ulr_save(cyc, u)
        if dec is None:
            if rec["code"] not in (ulr.R_HOLD, ulr.R_COOLDOWN):
                rec["market"] = market.slug
                self._log_eval(market.slug, rec)
            return []
        token = market.no_token_id if dec["side"] == "DOWN" else market.yes_token_id
        q = min(dec["q"], float(held.get(token, dec["q"])))
        if q < 1e-6:
            return []
        rec["market"] = market.slug
        self._log_eval(market.slug, rec)
        self._ulr_action_t[u.position_id] = now
        return [ExitAction(kind=dec["kind"], leg_id=u.position_id, token_id=token, side=dec["side"], shares=round(q, 2),
                           price=dec["limit"], tif="IOC" if self._fak else "FOK", detail=dec["why"])]

    def _ulr_by_position(self, position_id: str) -> tuple[str | None, rc.RecoveryCycle | None, ulr.ULR | None]:
        for slug, c in self.cycles.items():
            u = self._ulr(c)
            if u is not None and u.position_id == position_id:
                return slug, c, u
        return None, None, None

    def _ulr_by_exit(self, order_id: str) -> tuple[str | None, rc.RecoveryCycle | None, ulr.ULR | None]:
        for slug, c in self.cycles.items():
            u = self._ulr(c)
            if u is not None and u.exit_oid == order_id:
                return slug, c, u
        return None, None, None

    def on_exit_order(self, leg_id: str, kind: str, order_id: str) -> None:
        if kind in ("PROFIT_EXIT", "RECOVERY_SELL") and leg_id.startswith(("pexit:", "rsell:")):
            _, slug, side = leg_id.split(":", 2)
            self._pexit[order_id] = (slug, side, kind)
            return
        slug, cyc, u = self._ulr_by_position(leg_id)
        if u is None:
            return
        ulr.exit_sent(u, order_id, kind, u.open_qty, self._ulr_action_t.pop(leg_id, time.time() * 1000.0))
        self._ulr_save(cyc, u)
        self.stats["ulr_exits_sent"] += 1
        log.info("[ULR_EXIT_SENT] %s %s: sell %g %s (%s)", slug, kind, u.open_qty, u.recovery_leg, order_id)

    def on_manual_sell_order(self, slug: str, side: str, order_id: str, qty: float, now: float) -> None:
        """live.py (_manual_sell): the dashboard's Sell left. Held until its fill is booked (on_external_fill) or it ends
        unfilled (on_exit_result), so a market the venue reports flat meanwhile waits for it (on_market_exited): the
        Market page's "Sell all tokens" sells both sides, and both can fill before live.py books either."""
        self._manual_sells[order_id] = (slug, side)

    def on_exit_fill(self, order_id: str, size: float, avg: float, fee: float, now: float) -> str:
        """live.py (_book_managed_exit): a sell filled (whole or part); into the market's book as a SELL."""
        if order_id in self._pexit:
            slug, side, kind = self._pexit.pop(order_id)
            fk = kind.lower()                                 # "profit_exit" | "recovery_sell"
            cyc = self.cycles.get(slug)
            if cyc is not None:
                n = sum(1 for f in cyc.book.fills if f.kind == fk) + 1
                token = next((f.token for f in cyc.book.fills if f.side == side), side)
                q = min(float(size), cyc.book.qty(side))
                if q > 1e-9:
                    cyc.book.add_fill(pb.FillRecord(market_id=slug, ts=now, token=token, side=side, qty=q, price=float(avg), fee=float(fee),
                                                    order_id=order_id, fill_id=f"{order_id}-{n}", action="SELL", expected_price=None,
                                                    kind=fk))
                self.stats[f"{fk}_fills"] += 1
                if fk == "recovery_sell":
                    self.stats["recovery_sell_proceeds"] += q * float(avg) - float(fee)
                cyc.events.append({"t": now, "event": f"{kind}_FILLED", "side": side, "qty": size, "price": avg, "fee": fee,
                                   "realised": cyc.book.realised_exit_pnl, "up": cyc.book.up_qty, "down": cyc.book.down_qty})
                o = rc.outcomes(cyc.book)
                log.warning("[%s_FILLED] %s sold %g %s @ %.4f fee %.4f -> UP %g DOWN %g left, realised %+.3f | UP-win %+.3f DOWN-win %+.3f",
                            kind, slug, size, side, avg, fee, cyc.book.up_qty, cyc.book.down_qty, cyc.book.realised_exit_pnl,
                            o["pnl_up_win"], o["pnl_down_win"])
            return kind
        slug, cyc, u = self._ulr_by_exit(order_id)
        if u is None:
            log.error("[ULR] a sell fill of %s with no recovery position: accounting halt", order_id)
            self.accounting_halt = f"untracked sell {order_id}"
            return "ULR_EXIT"
        n = sum(1 for f in cyc.book.fills if f.order_id == order_id) + 1
        token = next((f.token for f in cyc.book.fills if f.side == u.recovery_leg), "")
        cyc.book.add_fill(ulr.exit_fill_record(u, slug, token, order_id, n, size, avg, fee, now))
        ulr.exit_filled(u, size, avg, fee, now)
        ulr.exit_done(u, now, True, "")
        kind = u.exit_kind
        self.stats["ulr_exit_fills"] += 1
        self.stats["ulr_fees"] += float(fee)
        self.stats[{ulr.R_TAKE_PROFIT: "ulr_tp", ulr.R_STOP_LOSS: "ulr_sl", ulr.R_SIGNAL_REVERSAL: "ulr_signal_exits",
                    ulr.R_EMERGENCY_EXIT: "ulr_emergency"}.get(kind, "ulr_exit_fills")] += 1 if kind in ulr.EXIT_KINDS else 0
        o = rc.outcomes(cyc.book)
        log.info("[ULR_SOLD] %s %s %g %s @ %.4f fee %.4f (entry %.4f): recovery realised %+.4f, %g still held | UP-win %+.3f "
                 "DOWN-win %+.3f", slug, kind, size, u.recovery_leg, avg, fee, u.entry_avg or 0.0, u.realized_pnl, u.open_qty,
                 o["pnl_up_win"], o["pnl_down_win"])
        cyc.events.append({"t": now, "event": "ULR_SOLD", "kind": kind, "qty": size, "price": avg, "fee": fee,
                           "realised": u.realized_pnl, "open_qty": u.open_qty})
        self._ulr_save(cyc, u)
        return kind

    def on_exit_result(self, order_id: str, reason: str, now: float) -> None:
        """live.py: a sell ended with no fill (killed / refused)."""
        if self._manual_sells.pop(order_id, None) is not None:
            return
        if order_id in self._pexit:
            slug, side, kind = self._pexit.pop(order_id)
            # an unfilled fill-and-kill (the bids moved) is retried after the cooldown; a refusal backs off
            killed = str(reason).lower() in ("", "killed", "unfilled", "expired", "quote_pulled", "fok_unfillable", "no_liquidity",
                                             "price_moved", "venue_reject")
            if not killed:                                    # refused by the venue: back off before asking again
                t = self._pexit_t if kind == "PROFIT_EXIT" else self._rsell_t
                t[slug] = now + EXIT_REJECT_BACKOFF_MS
            log.warning("[%s_NOT_FILLED] %s %s: %s (tried again after the %s while its condition holds)", kind, slug, side, reason,
                        "cooldown" if killed else f"cooldown + {EXIT_REJECT_BACKOFF_MS / 1000:.0f} s back-off")
            return
        slug, cyc, u = self._ulr_by_exit(order_id)
        if u is None:
            return
        ulr.exit_done(u, now, False, reason)
        self.stats["ulr_exit_kills"] += 1
        self._ulr_save(cyc, u)
        log.warning("[ULR_EXIT_NOT_FILLED] %s %s sell of %g %s: %s (retried)", slug, u.exit_kind, u.open_qty, u.recovery_leg, reason)

    def _late(self, meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc) -> Evaluation | None:
        """The last-seconds buy of the market's winner (pairlock_recovery.evaluate_late)."""
        m = meta.market
        qa = quote_ages or {}
        mv = MarketView(bid_up=book_up.best_bid, ask_up=book_up.best_ask, bid_down=book_down.best_bid, ask_down=book_down.best_ask,
                        asks_up=_levels(book_up.asks), asks_down=_levels(book_down.asks), bids_up=_levels(book_up.bids),
                        bids_down=_levels(book_down.bids), quote_age_up_ms=qa.get(m.yes_token_id),
                        quote_age_down_ms=qa.get(m.no_token_id), spot_age_ms=spot_age_ms)
        fresh = ((spot_age_ms is None or spot_age_ms <= self.lim.max_spot_age_ms)
                 and all(x <= self.lim.max_quote_age_ms for x in (mv.quote_age_up_ms, mv.quote_age_down_ms) if x is not None))
        f = self._features(meta, now)
        p_up = None if f is None else self.plmodel.p_up(f)
        probe = cyc if cyc is not None else rc.RecoveryCycle(book=pb.PairBook(m.slug), a_side="UP", started_t=now,
                                                              target=self.rprm.target_profit)
        free = max(0.0, self._balance - sum(p.get("q", 0.0) * p.get("limit", 0.0) for p in self.pending.values()))
        dec, rec = rc.evaluate_late(probe, p_up, mv, now, m.seconds_remaining(now), self.rprm, free, fresh)
        self.stats["late_evaluations"] += 1
        if "winner" in rec:
            self._log_eval(m.slug, {**rec, "decision": rec["decision"], "why": rec["why"]})
        if dec is None:
            if cyc is None:
                probe.events.clear()
            return None
        if cyc is None:
            probe.a_side = dec.side
            self.cycles[m.slug] = probe
            cyc = probe
        self.stats["late_candidates"] += 1
        return self._order_evaluation(meta, book_up, book_down, now, cyc, dec, rec, p_up, "late")

    def _ladder(self, meta, book_up, book_down, spot_age_ms, quote_ages, now, cyc) -> Evaluation | None:
        """The late ladder's buy in a held market (pairlock_recovery.evaluate_ladder): a late order."""
        m = meta.market
        left = m.seconds_remaining(now)
        prm = self.rprm
        if not prm.ladder_min_left_s <= left <= prm.ladder_window_s:
            return None
        qa = quote_ages or {}
        mv = MarketView(bid_up=book_up.best_bid, ask_up=book_up.best_ask, bid_down=book_down.best_bid, ask_down=book_down.best_ask,
                        asks_up=_levels(book_up.asks), asks_down=_levels(book_down.asks), bids_up=_levels(book_up.bids),
                        bids_down=_levels(book_down.bids), quote_age_up_ms=qa.get(m.yes_token_id),
                        quote_age_down_ms=qa.get(m.no_token_id), spot_age_ms=spot_age_ms)
        fresh = all(x <= self.lim.max_quote_age_ms for x in (mv.quote_age_up_ms, mv.quote_age_down_ms) if x is not None)
        free = max(0.0, self._balance - sum(p.get("q", 0.0) * p.get("limit", 0.0) for p in self.pending.values()))
        dec, rec = rc.evaluate_ladder(cyc, mv, now, left, prm, free, fresh)
        if dec is None:
            if rec.get("in_band"):                      # in the band but not bought: worth a (deduplicated) line
                self._log_eval(m.slug, rec)
            return None
        self._log_eval(m.slug, rec)
        # no model here: the market's UP mid is the probability the order carries (live.py logs p and 1 - p for DOWN;
        # None raised "int - NoneType" there on the first DOWN ladder buy, 2026-10-06 21:43)
        p_up = (mv.bid_up + mv.ask_up) / 2.0 if mv.bid_up is not None and mv.ask_up is not None else (
            dec.vwap if dec.side == "UP" else 1.0 - dec.vwap)
        return self._order_evaluation(meta, book_up, book_down, now, cyc, dec, rec, p_up, "late")

    def _order_evaluation(self, meta, book_up, book_down, now, cyc, dec, rec, p_up, trigger: str) -> Evaluation:
        m = meta.market
        side = dec.side
        book = book_up if side == "UP" else book_down
        tok = m.yes_token_id if side == "UP" else m.no_token_id
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=m.seconds_remaining(now), reason=None)
        pk = {"kind": trigger, "side": side, "q": dec.shares, "plan_qty": dec.plan_qty, "vwap": round(dec.vwap, 4),
              "pnl_up_win": round(rec["pnl_up_win"], 4), "pnl_down_win": round(rec["pnl_down_win"], 4),
              "after_pnl_up_win": round(rec["after_pnl_up_win"], 4), "after_pnl_down_win": round(rec["after_pnl_down_win"], 4),
              "expected_improvement": rec.get("expected_improvement"), "improves_min": rec.get("improves_min")}
        info = {"signal": True, "direction": side, "side": side, "t": round(now), "trigger": trigger, "size": dec.shares,
                "ask": book.best_ask, "bid": book.best_bid, "limit": dec.limit, "pairlock": pk}
        ev.features = {STRATEGY_ID: info, "execution": {"best_ask": book.best_ask, "best_bid": book.best_bid, "cap": dec.limit,
                                                         "spread": None if book.best_bid is None else round(book.best_ask - book.best_bid, 4)}}
        p_side = None if p_up is None else (p_up if side == "UP" else 1.0 - p_up)
        ev.sides = [SideView(side=side, token_id=tok, p_model=p_side if p_side is not None else book.best_ask, p_market=book.best_ask,
                             ask=book.best_ask, bid=book.best_bid, depth_at_limit=_depth(book.asks, book.best_ask, True), costs=None,
                             reason=None, entry_px=book.best_ask)]
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, tok, dec.limit, dec.vwap
        ev.max_entry_price = dec.limit
        ev.size, ev.net_edge, ev.confidence = dec.shares, float(rec.get("edge_per_share") or 0.0), 1.0
        ev.p_used = p_up
        ev.size_note = f"{trigger} {dec.shares:g} sh of {side}: {dec.reason}"
        mk = {"t": round(now), "dir": 1 if side == "UP" else -1, "z1": None, "z3": None, "r1_bps": None, "trigger": trigger,
              "ok": True, "why": "", "recovery": dict(pk)}
        self.shock_marks.setdefault(m.slug, deque(maxlen=self.SHOCK_MARKS_PER_MARKET)).append(mk)
        self.decisions.append({"t": round(now), "slug": m.slug, "action": trigger, "side": side, "q": dec.shares,
                               "plan_qty": dec.plan_qty, "after_pnl_up_win": pk["after_pnl_up_win"],
                               "after_pnl_down_win": pk["after_pnl_down_win"]})
        self._decisions[m.slug] = dec
        self.last[m.slug] = ev
        return ev

    def _log_eval(self, slug: str, rec: dict) -> None:
        """Spec item 13: one JSON line per evaluation; the same rejection repeating within LOG_REPEAT_MS is counted."""
        key = (slug, rec["decision"], rec["why"].split("(")[0][:60])
        last = self._repeat.get(key)
        if rec["decision"] not in ("BUY", "LOCK", "LOCKED") and last is not None and rec["t"] - last[0] < self.LOG_REPEAT_MS:
            last[1] += 1
            return
        n_sup = 0 if last is None else last[1]
        self._repeat[key] = [rec["t"], 0]
        row = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in rec.items() if k != "signals"}
        row["market"] = slug
        row["signals"] = [k for k, v in (rec.get("signals") or {}).items() if v]
        if n_sup:
            row["repeated"] = n_sup
        log.info("[RECOVERY_EVAL] %s", json.dumps(row, default=str))

    # ───────────────────────────── orders ─────────────────────────────

    @staticmethod
    def _is_recovery(ev: Evaluation) -> bool:
        return ((ev.features or {}).get(STRATEGY_ID) or {}).get("trigger") in ("recovery", "late", "open", "ulr")

    @staticmethod
    def _is_open(ev: Evaluation) -> bool:
        return ((ev.features or {}).get(STRATEGY_ID) or {}).get("trigger") == "open"

    def revalidate(self, ev: Evaluation, book, min_edge: float | None = None) -> Reason | None:
        if self._is_recovery(ev):
            return Reason.NO_OFFER if book is None or book.best_ask is None else None
        return super().revalidate(ev, book, min_edge)

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        now = now_ms or 0.0
        if self._is_open(ev):
            dec = self._decisions.pop(ev.slug)
            cyc = self.candidates.pop(ev.slug, None) or rc.RecoveryCycle(book=pb.PairBook(ev.slug), a_side=ev.side, started_t=now,
                                                                         target=self.rprm.target_profit, open_entry=True)
            if cyc.holds and not self.rprm.swing_mode:      # an add-on buy: its pair bid is for THIS side's fill
                cyc.a_side = ev.side
                self.stats["cond_addons"] += 1
            self.cycles[ev.slug] = cyc
            o = Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                      tif=TimeInForce.IOC if self._fak else TimeInForce.FOK, expected_price=ev.expected_price,
                      tag=f"{ev.side} open buy q={ev.size:g} <= {ev.limit_price:.2f}")
            rc.on_entry_sent(cyc, o.order_id, ev.size, ev.limit_price, now)
            self.pending[ev.slug] = {"oid": o.order_id, "kind": "entry", "cycle": cyc, "side": ev.side, "q": ev.size,
                                     "limit": ev.limit_price, "done": False, "filled_qty": 0.0, "expected": ev.expected_price,
                                     "open": True, "swing_no": 0, "high": bool(dec.record.get("high_band"))}
            if cyc.swing is not None:
                sw.on_sent(cyc.swing, "entry", 0, now)
                self.stats["swing_entries_sent"] += 1
            self._open_tries[ev.slug] = self._open_tries.get(ev.slug, 0) + 1
            self.stats["entries"] += 1
            self.stats["open_sent"] += 1
            log.info("[OPEN_SENT] %s %s %g sh %s <= %.2f | %s", ev.slug, ev.side, ev.size, o.tif.value, ev.limit_price, dec.reason)
            return o
        if self._is_recovery(ev):
            dec = self._decisions.pop(ev.slug)
            cyc = self.cycles[ev.slug]
            # fill-and-kill when TPB_TRIANGLE_ORDER_TYPE=FAK (2026-10-02: half the all-or-nothing FOKs were killed, and
            # the killed ones were the best signals); every partial fill is applied as it comes (on_fill_record)
            o = Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                      tif=TimeInForce.IOC if self._fak else TimeInForce.FOK,
                      expected_price=ev.expected_price, tag=f"{ev.side} {dec.kind} q={ev.size:g} plan={dec.plan_qty:g} vwap={dec.vwap:.3f}")
            rc.on_recovery_sent(cyc, dec, o.order_id, now)
            ladder = dec.kind == "late" and bool(dec.record.get("ladder"))
            swing_no = int(dec.record.get("swing_number") or 0)
            self.pending[ev.slug] = {"oid": o.order_id, "kind": dec.kind, "cycle": cyc, "side": ev.side, "q": ev.size,
                                     "limit": ev.limit_price, "done": False, "filled_qty": 0.0, "expected": dec.vwap,
                                     "ladder": ladder, "swing_no": swing_no}
            if cyc.swing is not None:
                sw.on_sent(cyc.swing, dec.kind, swing_no, now)
            if ladder:
                self.stats["ladder_sent"] += 1
                self.stats["ladder_trimmed"] += int("trimmed_from" in dec.record)
                log.info("[LADDER_SENT] %s %s %g sh %s <= %.2f | %s", ev.slug, ev.side, ev.size, o.tif.value, ev.limit_price,
                         dec.reason)
            elif dec.kind == "late":
                self.stats["late_sent"] += 1
                self.stats["late_trimmed"] += int("trimmed_from" in dec.record)
                log.info("[LATE_SENT] %s %s %g sh %s <= %.2f | %s", ev.slug, ev.side, ev.size, o.tif.value, ev.limit_price,
                         dec.reason)
            elif dec.kind == "lock":
                self.stats["lock_sent"] += 1
                log.warning("[LOCK_SENT] %s %s %g sh %s <= %.2f | %s", ev.slug, ev.side, ev.size, o.tif.value, ev.limit_price,
                            dec.reason)
            elif dec.kind == "hedge":
                self.stats["hedge_sent"] += 1
                log.warning("[HEDGE_SENT] %s %s %g sh %s <= %.2f | %s", ev.slug, ev.side, ev.size, o.tif.value, ev.limit_price,
                            dec.reason)
            elif dec.kind == "ulr":
                u = self._ulr(cyc)
                if u is not None:
                    ulr.entry_sent(u, o.order_id, ev.size, now)
                    self._ulr_save(cyc, u)
                self.stats["ulr_sent"] += 1
                log.info("[ULR_SENT] %s %s %g sh %s <= %.2f | %s", ev.slug, ev.side, ev.size, o.tif.value, ev.limit_price, dec.reason)
            else:
                self.stats["recovery_sent"] += 1
                self.stats["even_out_sent"] += int(bool(dec.record.get("even_out")))
                self.stats["recovery_up" if ev.side == "UP" else "recovery_down"] += 1
                log.info("[RECOVERY_SENT] %s %s %g sh %s <= %.2f (plan %g sh at VWAP %.3f) | %s", ev.slug, ev.side, ev.size,
                         o.tif.value, ev.limit_price, dec.plan_qty, dec.vwap, dec.reason)
            return o
        o = super().to_order(ev, now_ms)
        cyc = self.candidates.pop(ev.slug, None) or rc.RecoveryCycle(book=pb.PairBook(ev.slug), a_side=ev.side, started_t=now,
                                                                     target=self.rprm.target_profit)
        self.cycles[ev.slug] = cyc
        rc.on_entry_sent(cyc, o.order_id, ev.size, ev.limit_price, now)
        self.pending[ev.slug] = {"oid": o.order_id, "kind": "entry", "cycle": cyc, "side": ev.side, "q": ev.size,
                                 "limit": ev.limit_price, "done": False, "filled_qty": 0.0, "expected": ev.expected_price}
        self.stats["entries"] += 1
        return o

    def signal_text(self, ev: Evaluation) -> str:
        if self._is_open(ev):
            return f"OPEN buy {ev.size:g} {ev.side} limit {ev.limit_price}: {ev.size_note}"
        if ((ev.features or {}).get(STRATEGY_ID) or {}).get("trigger") == "ulr":
            return f"UNFILLED-LEG RECOVERY buy {ev.size:g} {ev.side} limit {ev.limit_price}: {ev.size_note}"
        if self._is_recovery(ev):
            pk = ((ev.features or {}).get(STRATEGY_ID) or {}).get("pairlock") or {}
            return (f"RECOVERY buy {ev.size:g} {ev.side} (plan {pk.get('plan_qty')}) at VWAP {pk.get('vwap')} limit {ev.limit_price}: "
                    f"UP-win {pk.get('pnl_up_win')} -> {pk.get('after_pnl_up_win')}, DOWN-win {pk.get('pnl_down_win')} -> "
                    f"{pk.get('after_pnl_down_win')}, E[P&L] {pk.get('expected_improvement') or 0.0:+.3f}, improves min {pk.get('improves_min')}")
        return super().signal_text(ev)

    def pair_order(self, market, side, shares, avg_px, now_ms, other_ask=None):
        """THE OPEN RULE's second leg (live.py, right after the open buy's fill): a resting GTC bid for the other
        side at pair_sum - the fill's average price, for the shares that filled, until the close. Recovery and
        late buys get no pair."""
        if market.slug not in self._pair_due:
            return None
        self._pair_due.discard(market.slug)
        if not self.rprm.pair_bid or self.rprm.swing_mode:      # swing mode: no other-side bid either                       # the user, 10-05 16:05 (recovery bot): no other-side bid at all
            return None
        cyc = self.cycles.get(market.slug)
        if cyc is None:
            return None
        other = rc.OTHER[side]
        px = rc.pair_bid_price(self.rprm, avg_px)
        # the user, 2026-10-08: the opposite order is a MAKER bid (post-only, resting) -- but when the other token's ask is
        # ALREADY at or under the price wanted, it is BOUGHT at once (fill-and-kill, limit = that price): a post-only bid
        # there would cross the book and be refused by the venue, leaving the shares unpaired while they are cheap
        take = px is not None and other_ask is not None and float(other_ask) <= px + 1e-9
        size = round(shares, 2)
        why = ("price" if px is None else "size" if market.min_size and size + 1e-9 < float(market.min_size) else None)
        if why is not None:
            self.stats["pairs_skipped"] += 1
            cyc.pair = {"oid": "", "side": other, "price": px or 0.0, "size": size, "filled": 0.0, "state": "skipped"}
            log.warning("[PAIR] %s: no %s bid (%s): %g sh bought at %.3f, sum %.2f", market.slug, other, why, shares,
                        avg_px, self.rprm.pair_sum)
            if self.rprm.ulr_enabled:
                u = ulr.start(f"{market.slug}:{side}:{rc.entry_count(cyc)}", side, size)
                ulr.hedge_skipped(u, why, now_ms)
                self._ulr_save(cyc, u)
            return None
        token = market.no_token_id if side == "UP" else market.yes_token_id
        if take:
            log.info("[PAIR] %s: the %s ask %.2f is at or under the %.2f wanted: buying it now (fill-and-kill)", market.slug,
                     other, float(other_ask), px)
            # limit AT the ask (not the price wanted): the venue spends size x ask and returns exactly ``size`` shares,
            # the same number as the first leg's (a lower limit than px is never worse)
            return other, Order(token_id=token, side=Side.BUY, price=round(float(other_ask), 2), size=size, tif=TimeInForce.IOC,
                                expected_price=float(other_ask),
                                tag=f"{other} open-rule pair TAKEN at <= {px:.2f} (ask {float(other_ask):.2f}) for the fill at {avg_px:.3f}")
        return other, Order(token_id=token, side=Side.BUY, price=px, size=size, tif=TimeInForce.GTC,
                            expires_ts=market.close_ts, expected_price=px, post_only=True,
                            tag=f"{other} open-rule pair {avg_px:.3f} + {px:.2f} = {avg_px + px:.3f} (sum {self.rprm.pair_sum:.2f})")

    @staticmethod
    def _is_hedge(cyc, order_id: str, side: str | None = None, price: float | None = None) -> bool:
        h = cyc.late_hedge if cyc is not None else None
        if h is None or h.get("state") not in ("sent", "working", "filled", "expired"):
            return False
        if h.get("oid") == order_id:
            return True
        # a restart re-places it under a placeholder id, then a new one (live.py _place_restored_pairs)
        return (side == h["side"] and price is not None and abs(float(price) - float(h["price"])) < 1e-9
                and h.get("state") in ("sent", "working"))

    def on_pair_submit(self, order_id: str, slug: str, side: str, price: float, shares: float) -> None:
        cyc = self.cycles.get(slug)
        if cyc is None:
            return
        if self._is_hedge(cyc, order_id, side, price):
            cyc.late_hedge.update({"oid": order_id, "state": "working"})
            return
        prev = cyc.pair or {}
        # a restart's placeholder / re-placed order keeps what had filled; a NEW pair (after a filled one) starts at 0
        same = prev.get("state") in ("working", "dropped", "repricing", "reprice_sent") and prev.get("side") == side
        carried = float(prev.get("filled") or 0.0) if same else 0.0
        repriced = prev.get("state") in ("repricing", "reprice_sent")
        # the pair's TARGET stays the first leg's shares when a bid is re-placed for what is still unpaired
        size = float(prev["size"]) if repriced and prev.get("size") else float(shares)
        cyc.pair = {"oid": order_id, "side": side, "price": float(price), "size": size, "filled": carried, "state": "working",
                    "ref": self._pair_mid(slug, side), "reprices": int(prev.get("reprices") or 0) if same else 0,
                    "old_oids": list(prev.get("old_oids") or ()) if same else []}
        if repriced:
            log.info("[PAIR_REPRICED] %s %s bid re-placed: %g sh GTC at %.2f (was %.2f), re-placement %d", slug, side, shares,
                     price, float(prev.get("price") or 0.0), cyc.pair["reprices"])
            return
        if not order_id.startswith(("restored-", "retry-")):
            self.stats["pairs_placed"] += 1
        if self.rprm.ulr_enabled:
            # the unfilled-leg recovery watches THIS pair bid: a new position per bid (an add-on's bid replaces a hedged one)
            u = self._ulr(cyc)
            if u is None or u.state in (ulr.HEDGED, ulr.RECOVERY_CLOSED):
                u = ulr.start(f"{slug}:{cyc.a_side}:{rc.entry_count(cyc)}", cyc.a_side, float(shares))
            if u.state in (ulr.HEDGE_PENDING, ulr.UNHEDGED_RECOVERY) or u.hedge_target is None:
                # a bid for THIS unhedged leg (first placement, or re-placed after a restart); an open recovery trade's
                # state is never replaced by a later bid
                ulr.hedge_placed(u, float(price), float(shares), time.time() * 1000.0)
            self._ulr_save(cyc, u)
        cyc.events.append({"t": time.time() * 1000.0, "event": "PAIR_PLACED", "side": side, "qty": shares, "price": price})
        log.info("[PAIR_PLACED] %s %s %g sh GTC at %.2f (sum %.2f) until the close", slug, side, shares, price, self.rprm.pair_sum)

    def _pair_mid(self, slug: str, side: str) -> float | None:
        q = self.quotes.get(slug)
        if q is None:
            return None
        ub, ua, db, da = q
        return rc._mid(ub, ua) if side == "UP" else rc._mid(db, da)

    def _pair_cycle(self, order_id: str) -> tuple[str | None, rc.RecoveryCycle | None]:
        for slug, cyc in self.cycles.items():
            if cyc.pair is not None and cyc.pair.get("oid") == order_id:
                return slug, cyc
        return None, None

    def _hedge_cycle(self, order_id: str) -> tuple[str | None, rc.RecoveryCycle | None]:
        for slug, cyc in self.cycles.items():
            if cyc.late_hedge is not None and cyc.late_hedge.get("oid") == order_id:
                return slug, cyc
        return None, None

    def drop_pair(self, order_id: str) -> None:
        _, cyc = self._pair_cycle(order_id)
        if cyc is not None and cyc.pair.get("state") == "working":
            cyc.pair["state"] = "dropped"

    def on_pair_done(self, order_id: str, filled: bool, killed: bool = False) -> None:
        """Idempotent: live.py reports a pull (filled=False) at the cancel and again if the venue's fill wins the race."""
        hslug, hcyc = self._hedge_cycle(order_id)
        if hcyc is not None:
            h = hcyc.late_hedge
            if filled or h.get("filled", 0.0) > 1e-9:
                h["state"] = "filled"
            elif h.get("state") in ("sent", "working"):
                h["state"] = "expired"
                log.info("[LATE_HEDGE_EXPIRED] %s %s bid at %.2f ended unfilled", hslug, h["side"], h["price"])
            return
        slug, cyc = self._pair_cycle(order_id)
        if cyc is None:
            return
        st = cyc.pair.get("state")
        if st in ("repricing", "reprice_sent"):
            # the pulled bid's END (the real venue reports it once the cancel landed, with any fill that beat it):
            # the lower bid may go now, for what is still unpaired (its fills arrive right after this call)
            cyc.pair["cancel_done"] = True
            return
        if filled or cyc.pair.get("filled", 0.0) > 1e-9:
            if st != "filled":
                cyc.pair["state"] = "filled"
                self.stats["pairs_filled"] += 1
                self._open_tries.pop(slug, None)                # an add-on buy may try again
            return
        if st == "working":
            cyc.pair["state"] = "expired"
            self.stats["pairs_expired"] += 1
            log.info("[PAIR_EXPIRED] %s %s bid at %.2f ended unfilled", slug, cyc.pair["side"], cyc.pair["price"])

    def on_pair_fill_record(self, slug: str, order, fill, now: float) -> None:
        """A fill of the pair bid (live.py): into the market's book as kind "pair" (the late hedge: "late_hedge")."""
        cyc = self.cycles.get(slug)
        if cyc is not None and cyc.late_hedge is not None and cyc.late_hedge.get("oid") == order.order_id:
            h = cyc.late_hedge
            n = sum(1 for f in cyc.book.fills if f.order_id == order.order_id) + 1
            rc.on_fill(cyc, pb.FillRecord(market_id=slug, ts=now, token=order.token_id, side=h["side"], qty=float(fill.size),
                                          price=float(fill.price), fee=float(fill.fee), order_id=order.order_id,
                                          fill_id=f"{order.order_id}-{n}", action="BUY", expected_price=h["price"],
                                          kind="late_hedge"), now)
            self.stats["late_hedge_fills"] += 1
            self.stats["late_hedge_qty"] += float(fill.size)
            o = rc.outcomes(cyc.book)
            log.info("[LATE_HEDGE_FILLED] %s %g %s @ %.4f -> UP %g DOWN %g | UP-win %+.3f DOWN-win %+.3f", slug, fill.size,
                     h["side"], fill.price, cyc.book.up_qty, cyc.book.down_qty, o["pnl_up_win"], o["pnl_down_win"])
            return
        if cyc is None or cyc.pair is None:
            log.error("[RECOVERY] %s: a pair fill of %s with no open-rule cycle (%g sh @ %.3f): accounting halt",
                      slug, order.order_id, fill.size, fill.price)
            self.accounting_halt = f"untracked pair fill {order.order_id}"
            return
        side = cyc.pair["side"]
        n = sum(1 for f in cyc.book.fills if f.order_id == order.order_id) + 1
        rec = pb.FillRecord(market_id=slug, ts=now, token=order.token_id, side=side, qty=float(fill.size),
                            price=float(fill.price), fee=float(fill.fee), order_id=order.order_id, fill_id=f"{order.order_id}-{n}",
                            action="BUY", expected_price=cyc.pair["price"], kind="pair")
        rc.on_fill(cyc, rec, now)
        self.stats["pair_qty"] += float(fill.size)
        if order.order_id != cyc.pair.get("oid") and order.order_id in (cyc.pair.get("old_oids") or ()):
            # the venue filled the bid that was pulled to be re-placed (the race): the bid working now is too big
            cyc.pair["shrink"] = True
            log.warning("[PAIR_REPRICE_RACE] %s: the pulled bid %s filled %g sh; the bid working now is re-sized", slug,
                        order.order_id, fill.size)
        u = self._ulr(cyc)
        if u is not None:
            ulr.hedge_filled(u, float(fill.size), now)
            self._ulr_save(cyc, u)
        if cyc.pair.get("filled", 0.0) + 1e-9 >= cyc.pair["size"] and cyc.pair.get("state") != "filled":
            cyc.pair["state"] = "filled"
            self.stats["pairs_filled"] += 1
            self._open_tries.pop(slug, None)
        o = rc.outcomes(cyc.book)
        log.info("[PAIR_FILLED] %s %g %s @ %.4f -> UP %g DOWN %g | UP-win %+.3f DOWN-win %+.3f (locked %+.3f)", slug, fill.size, side,
                 fill.price, cyc.book.up_qty, cyc.book.down_qty, o["pnl_up_win"], o["pnl_down_win"], o["min_settlement_pnl"])

    def pair_cancels(self, now: float) -> list[str]:
        """THE OPEN RULE's hand-over (live.py cancels each id): the pair bid has not filled and the OTHER side is now
        the higher price than the held side -> pull the bid; the loss-recovery logic decides from here."""
        out = self._pair_reprices(now)
        rescue = float(self.rprm.pair_rescue_price or 0.0)
        if not (self.rprm.pair_pull or rescue > 0.0) or self.rprm.ulr_enabled:   # ULR: the bid rests until the close
            return out
        for slug, cyc in self.cycles.items():
            pr = cyc.pair
            if pr is None or pr.get("state") != "working" or not pr.get("oid") or pr["oid"].startswith(("restored-", "retry-")):
                continue
            q = self.quotes.get(slug)
            if q is None:
                continue
            ub, ua, db, da = q
            mids = {"UP": rc._mid(ub, ua), "DOWN": rc._mid(db, da)}
            held, other = cyc.a_side, pr["side"]
            if mids[held] is None or mids[other] is None:
                continue
            flip = self.rprm.pair_pull and mids[other] > mids[held] + 1e-9
            to_rescue = rescue > 0.0 and mids[other] > rescue + 1e-9
            if not (flip or to_rescue):
                continue
            pr["state"] = "cancelled"
            self.stats["pairs_cancelled"] += 1
            cyc.events.append({"t": now, "event": "PAIR_CANCELLED", "side": other, "mid_held": mids[held], "mid_other": mids[other]})
            log.warning("[PAIR_CANCEL] %s %s bid at %.2f pulled: %s %.3f is now above %s -> %s", slug, other, pr["price"], other,
                        mids[other], f"the rescue price {rescue:.2f}" if to_rescue else f"the held {held} {mids[held]:.3f}",
                        "rescue buy of the unpaired shares" if to_rescue else "loss recovery")
            out.append(pr["oid"])
        return out

    def _pair_reprices(self, now: float) -> list[str]:
        """THE TRAILING OPPOSITE BID (the user, 2026-10-08: "if the price falls much, cancel the ask and recreate a lower
        one"): the bid's token has fallen (mid) by >= pair_reprice_drop since the bid was placed -> pull it; pair_requests
        re-places it lower by the same fall, for the shares still unpaired (at most pair_reprice_max times per bid). A
        pulled bid the venue filled anyway (the race) makes the bid working now too big: it is re-placed at its price,
        sized to what is still unpaired."""
        out = []
        drop = float(self.rprm.pair_reprice_drop or 0.0)
        for slug, cyc in self.cycles.items():
            pr = cyc.pair
            if pr is None or not pr.get("oid") or pr["oid"].startswith(("restored-", "retry-")):
                continue
            if pr.get("shrink") and pr.get("state") in ("working", "filled"):
                pr.pop("shrink", None)
                left = round(float(pr["size"]) - float(pr.get("filled") or 0.0), 2)
                pr["old_oids"] = [*pr.get("old_oids", ()), pr["oid"]]
                if left >= 0.01:
                    pr.update({"state": "repricing", "next_price": float(pr["price"]), "t_cancel": now, "cancel_done": False})
                else:
                    pr["state"] = "filled"
                log.warning("[PAIR_RESIZE] %s %s bid %s pulled: %s", slug, pr["side"], pr["oid"],
                            f"re-placed for the {left:g} sh still unpaired" if left >= 0.01 else "the pair is complete")
                out.append(pr["oid"])
                continue
            if drop <= 0.0 or pr.get("state") != "working" or int(pr.get("reprices") or 0) >= int(self.rprm.pair_reprice_max):
                continue
            mid = self._pair_mid(slug, pr["side"])
            if mid is None:
                continue
            if pr.get("ref") is None:                            # a bid placed before this rule (or without a quote): from now
                pr["ref"] = mid
                continue
            fall = float(pr["ref"]) - mid
            if fall + 1e-9 < drop:
                continue
            price = math.floor((float(pr["price"]) - fall) * 100.0 + 1e-9) / 100.0
            left = round(float(pr["size"]) - float(pr.get("filled") or 0.0), 2)
            if price < 0.01 or price > float(pr["price"]) - 0.01 + 1e-9 or left < max(0.01, float(self.rprm.min_size or 0.0)):
                continue                                         # (under the venue's minimum the bid could not be re-placed)
            pr["old_oids"] = [*pr.get("old_oids", ()), pr["oid"]]
            pr.update({"state": "repricing", "next_price": price, "reprices": int(pr.get("reprices") or 0) + 1,
                       "t_cancel": now, "cancel_done": False})
            self.stats["pairs_repriced"] += 1
            cyc.events.append({"t": now, "event": "PAIR_REPRICE", "side": pr["side"], "from": pr["price"], "to": price,
                               "mid": mid, "ref": pr["ref"]})
            log.warning("[PAIR_REPRICE] %s %s bid at %.2f pulled: its mid fell %.3f -> %.3f (>= %.2f): re-placing %g sh at %.2f",
                        slug, pr["side"], pr["price"], pr["ref"], mid, drop, left, price)
            out.append(pr["oid"])
        return out

    def _reprice_orders(self, now: float, markets: dict | None) -> list[tuple[str, str, Order]]:
        """The re-placed bids of _pair_reprices: post-only GTC for the unpaired shares, or a fill-and-kill buy AT the ask
        when the ask is already at or under the new price."""
        out = []
        for slug, cyc in self.cycles.items():
            pr = cyc.pair
            if pr is None or pr.get("state") != "repricing":
                continue
            m = (markets or {}).get(slug)
            if m is None:
                continue
            if now >= m.close_ts:
                pr["state"] = "expired"
                continue
            if not pr.get("cancel_done", True) and now - float(pr.get("t_cancel") or now) < REPRICE_CANCEL_WAIT_MS:
                continue                                         # the old bid's cancel is not confirmed yet
            size = round(float(pr["size"]) - float(pr.get("filled") or 0.0), 2)
            if size < 0.01:
                pr["state"] = "filled"                           # the pulled bid filled in full before its cancel
                self.stats["pairs_filled"] += 1
                log.info("[PAIR_REPRICE] %s %s: the pulled bid filled in full before its cancel; nothing to re-place",
                         slug, pr["side"])
                continue
            if m.min_size and size + 1e-9 < float(m.min_size):
                pr["state"] = "expired"
                log.warning("[PAIR_REPRICE] %s %s: %g sh still unpaired is under the venue minimum %g: not re-placed",
                            slug, pr["side"], size, float(m.min_size))
                continue
            q = self.quotes.get(slug)
            ask = None if q is None else (q[1] if pr["side"] == "UP" else q[3])
            px = float(pr["next_price"])
            token = m.yes_token_id if pr["side"] == "UP" else m.no_token_id
            pr["state"] = "reprice_sent"                         # on_pair_submit turns it into the working bid
            if ask is not None and float(ask) <= px + 1e-9:
                o = Order(token_id=token, side=Side.BUY, price=round(float(ask), 2), size=size, tif=TimeInForce.IOC,
                          expected_price=float(ask), tag=f"{pr['side']} re-priced pair TAKEN at <= {px:.2f} (ask {float(ask):.2f})")
            else:
                o = Order(token_id=token, side=Side.BUY, price=px, size=size, tif=TimeInForce.GTC, expires_ts=m.close_ts,
                          expected_price=px, post_only=True, tag=f"{pr['side']} pair re-priced lower to {px:.2f}")
            out.append((slug, pr["side"], o))
        return out

    def pair_requests(self, now: float, markets: dict | None = None) -> list[tuple[str, str, Order]]:
        """THE LATE HEDGE (live.py places each as a pair order): after a late buy, its token's mid fell under
        late_hedge_trigger -> a GTC bid for the OTHER token at late_hedge_price, for the late shares, until the close.
        ``markets``: slug -> Market (token ids, close). Also the trailing opposite bid's re-placements."""
        out = self._reprice_orders(now, markets)
        for slug, cyc in self.cycles.items():
            h = cyc.late_hedge
            if h is None or h.get("state") != "armed" or h.get("qty", 0.0) <= 1e-9:
                continue
            m = (markets or {}).get(slug)
            q = self.quotes.get(slug)
            if m is None or q is None or now >= m.close_ts:
                continue
            ub, ua, db, da = q
            mid = rc._mid(ub, ua) if h["held"] == "UP" else rc._mid(db, da)
            if mid is None or mid >= h["trigger"] - 1e-9:
                continue
            token = m.no_token_id if h["held"] == "UP" else m.yes_token_id
            o = Order(token_id=token, side=Side.BUY, price=float(h["price"]), size=round(h["qty"], 2), tif=TimeInForce.GTC,
                      expires_ts=m.close_ts, expected_price=float(h["price"]),
                      tag=f"{h['side']} late hedge: {h['held']} mid {mid:.3f} < {h['trigger']:.2f}")
            h.update({"oid": o.order_id, "state": "sent", "t": now, "mid_at_trigger": mid})
            self.stats["late_hedges_sent"] += 1
            cyc.events.append({"t": now, "event": "LATE_HEDGE_SENT", "side": h["side"], "qty": h["qty"], "price": h["price"], "mid": mid})
            log.warning("[LATE_HEDGE] %s %s mid %.3f fell under %.2f: buying %g %s at <= %.2f", slug, h["held"], mid, h["trigger"],
                        h["qty"], h["side"], h["price"])
            out.append((slug, h["side"], o))
        return out

    def entry_result(self, order_id: str, filled: bool, why: str = "") -> None:
        super().entry_result(order_id, filled, why)
        for slug, p in list(self.pending.items()):
            if p["oid"] != order_id:
                continue
            p["done"], p["why"] = True, why
            if p["kind"] == "entry":
                self.stats["entry_fills" if filled else "entry_kills"] += 1
                if p.get("open"):
                    self.stats["open_fills" if filled else "open_kills"] += 1
                if self.rprm.swing_mode and filled:
                    self.stats["swing_entry_fills"] += 1
            elif p["kind"] == "late":
                self.stats["late_fills" if filled else "late_kills"] += 1
                self.stats["ladder_fills"] += int(bool(filled and p.get("ladder")))
                if not filled:
                    log.warning("[LATE_NOT_FILLED] %s %s %g sh (%s)", slug, p["side"], p["q"], why)
            elif p["kind"] == "lock":
                self.stats["lock_fills" if filled else "lock_kills"] += 1
                if not filled:
                    log.warning("[LOCK_NOT_FILLED] %s %s %g sh (%s): retried at the next evaluation", slug, p["side"], p["q"], why)
            elif p["kind"] == "hedge":
                self.stats["hedge_fills" if filled else "hedge_kills"] += 1
                if not filled:
                    log.warning("[HEDGE_NOT_FILLED] %s %s %g sh (%s): the inventory is unchanged, re-evaluated after the "
                                "cooldown", slug, p["side"], p["q"], why)
            elif p["kind"] == "ulr":
                self.stats["ulr_fills" if filled else "ulr_kills"] += 1
                if not filled:
                    log.warning("[ULR_NOT_FILLED] %s %s %g sh (%s): an attempt, the position is unchanged", slug, p["side"], p["q"], why)
            else:
                self.stats["recovery_fills" if filled else "recovery_kills"] += 1
                if not filled:
                    log.warning("[RECOVERY_NOT_FILLED] %s %s %g sh (%s): the position is unchanged", slug, p["side"], p["q"], why)
            if not filled:
                self._finalize(slug, time.time() * 1000.0)
            return

    def entry_refused(self, ev: Evaluation, why: str) -> None:
        if self._is_open(ev):
            self._decisions.pop(ev.slug, None)
            self.candidates.pop(ev.slug, None)
            p = self.pending.get(ev.slug)
            if p is not None and p["kind"] == "entry" and not p.get("done"):
                self.pending.pop(ev.slug, None)
                self.stats["open_sent"] -= 1
                self.stats["entries"] -= 1
                cyc = self.cycles.get(ev.slug)
                if cyc is not None and not cyc.holds:
                    self.cycles.pop(ev.slug, None)
                elif cyc is not None:                           # an add-on that never left: back to the paired position
                    cyc.book.resolve_pending(p["oid"])
                    if cyc.state is rc.RState.INITIAL_ENTRY:
                        cyc.to(rc.RState.HOLDING, cyc.last_action_t, f"add-on not sent: {why}")
                    if cyc.swing is not None:
                        sw.on_not_sent(cyc.swing, "entry", 0)
                    else:
                        self.stats["cond_addons"] -= 1
                if cyc is not None and cyc.swing is not None:
                    self.stats["swing_entries_sent"] -= 1
            log.info("[OPEN_NOT_SENT] %s %s: %s", ev.slug, ev.side, why)
            return
        if self._is_recovery(ev):
            self._decisions.pop(ev.slug, None)
            p = self.pending.get(ev.slug)
            cyc = self.cycles.get(ev.slug)
            if p is not None and p["kind"] != "entry" and not p.get("done"):
                self.pending.pop(ev.slug, None)                   # to_order ran, but the order never left
                if cyc is not None:
                    cyc.book.resolve_pending(p["oid"])
                    cyc.pending = None
                    if p["kind"] == "late" and p.get("ladder"):
                        cyc.ladder_orders = max(0, cyc.ladder_orders - 1)
                        cyc.ladder_last_t = -1e18
                    elif p["kind"] == "late":
                        cyc.late_orders = max(0, cyc.late_orders - 1)
                    elif p["kind"] == "lock":
                        cyc.lock_steps = max(0, cyc.lock_steps - 1)
                    else:
                        cyc.actions = max(0, cyc.actions - 1)
                    if cyc.swing is not None:
                        sw.on_not_sent(cyc.swing, p["kind"], int(p.get("swing_no") or 0))
                    if cyc.state is rc.RState.RECOVERY_PENDING:
                        cyc.to(rc.RState.HOLDING if cyc.holds else rc.RState.WAITING, cyc.last_action_t, f"not sent: {why}")
                    if not cyc.holds and not cyc.book.fills:
                        self.cycles.pop(ev.slug, None)
                self.stats[{"late": "late_sent", "lock": "lock_sent", "hedge": "hedge_sent"}.get(p["kind"], "recovery_sent")] -= 1
            for mk in reversed(self.shock_marks.get(ev.slug, ())):
                if mk.get("trigger") == "recovery" and mk.get("ok"):
                    mk["ok"], mk["why"] = False, f"not sent: {why}"
                    break
            return
        self.candidates.pop(ev.slug, None)
        p = self.pending.get(ev.slug)
        if p is not None and p["kind"] == "entry" and not p.get("done"):
            self.pending.pop(ev.slug, None)
            if ev.slug in self.cycles and not self.cycles[ev.slug].holds:
                self.cycles.pop(ev.slug, None)
        super().entry_refused(ev, why)

    def _finalize(self, slug: str, now: float) -> None:
        p = self.pending.get(slug)
        if p is None or not p.get("done"):
            return
        self.pending.pop(slug, None)
        cyc = p["cycle"]
        filled = p.get("filled_qty", 0.0) > 1e-9
        rc.on_order_done(cyc, p["oid"], filled, now, p.get("why", ""))
        if p["kind"] == "ulr":
            u = self._ulr(cyc)
            if u is not None:
                ulr.entry_done(u, now, p.get("why", ""))
                self._ulr_save(cyc, u)
        if p["kind"] in ("entry", "late") and not filled and not cyc.holds:
            self.cycles.pop(slug, None)
        if p["kind"] == "recovery" and filled and p["filled_qty"] < p["q"] - 1e-9:
            self.stats["recovery_partial"] += 1

    def on_fill_record(self, slug: str, order, fill, now: float) -> None:
        p = self.pending.get(slug)
        if p is None or p["oid"] != order.order_id:
            log.error("[RECOVERY] %s: a fill of %s with no matching order in flight (%g sh @ %.3f): accounting halt",
                      slug, order.order_id, fill.size, fill.price)
            self.accounting_halt = f"untracked fill {order.order_id}"
            return
        cyc = p["cycle"]
        n = sum(1 for f in cyc.book.fills if f.order_id == order.order_id) + 1
        rec = pb.FillRecord(market_id=slug, ts=now, token=order.token_id, side=p["side"], qty=float(fill.size),
                            price=float(fill.price), fee=float(fill.fee), order_id=order.order_id, fill_id=f"{order.order_id}-{n}",
                            action="BUY", expected_price=p.get("expected"), kind=p["kind"])
        rc.on_fill(cyc, rec, now)
        if cyc.swing is not None:
            sw.on_fill(cyc.swing, p["kind"], p["side"], float(fill.size), float(fill.price), float(fill.fee), now,
                       int(p.get("swing_no") or 0))
        p["filled_qty"] = p.get("filled_qty", 0.0) + float(fill.size)
        if p.get("high"):
            if not p.get("high_counted"):
                p["high_counted"] = True
                self._high_count[slug] = self._high_count.get(slug, 0) + 1
                self._open_tries.pop(slug, None)                 # the next high-band buy may try again
            if self.rprm.cond_high_pair_bid and (cyc.pair is None or cyc.pair.get("state") == "filled"):
                self._pair_due.add(slug)                         # its opposite bid, sized to what filled (pair_after_partial)
        elif p.get("open") and (cyc.pair is None or cyc.pair.get("state") == "filled"):
            self._pair_due.add(slug)
        if p["kind"] == "late" and not p.get("ladder") and self.rprm.late_hedge_trigger > 0.0:
            h = cyc.late_hedge
            if h is None or h.get("state") != "armed":
                h = cyc.late_hedge = {"held": p["side"], "side": rc.OTHER[p["side"]], "qty": 0.0, "trigger": self.rprm.late_hedge_trigger,
                                      "price": self.rprm.late_hedge_price, "oid": "", "filled": 0.0, "state": "armed"}
            h["qty"] += float(fill.size)
        if p["kind"] == "late":
            self.stats["late_qty"] += float(fill.size)
            self.stats["late_cost"] += float(fill.size) * float(fill.price) + float(fill.fee)
            if p.get("ladder"):
                self.stats["ladder_qty"] += float(fill.size)
                self.stats["ladder_cost"] += float(fill.size) * float(fill.price) + float(fill.fee)
            o = rc.outcomes(cyc.book)
            log.info("[%s] %s %g %s @ %.4f fee %.4f -> UP %g DOWN %g | UP-win %+.3f DOWN-win %+.3f",
                     "LADDER_FILLED" if p.get("ladder") else "LATE_FILLED", slug, fill.size,
                     p["side"], fill.price, fill.fee, cyc.book.up_qty, cyc.book.down_qty, o["pnl_up_win"], o["pnl_down_win"])
        if p["kind"] == "ulr":
            u = self._ulr(cyc)
            if u is not None:
                ulr.entry_filled(u, float(fill.size), float(fill.price), float(fill.fee), now)
                self._ulr_save(cyc, u)
            self.stats["ulr_qty"] += float(fill.size)
            self.stats["ulr_fees"] += float(fill.fee)
            o = rc.outcomes(cyc.book)
            log.info("[ULR_FILLED] %s %g %s @ %.4f fee %.4f -> UP %g DOWN %g | UP-win %+.3f DOWN-win %+.3f (the recovery trade is "
                     "open: TP %.2f SL %.2f)", slug, fill.size, p["side"], fill.price, fill.fee, cyc.book.up_qty, cyc.book.down_qty,
                     o["pnl_up_win"], o["pnl_down_win"], self.rprm.ulr_take_profit, self.rprm.ulr_stop_loss)
        if p["kind"] == "lock":
            self.stats["lock_qty"] += float(fill.size)
            o = rc.outcomes(cyc.book)
            log.warning("[LOCK_FILLED] %s %g %s @ %.4f fee %.4f -> UP %g DOWN %g | UP-win %+.3f DOWN-win %+.3f", slug, fill.size,
                        p["side"], fill.price, fill.fee, cyc.book.up_qty, cyc.book.down_qty, o["pnl_up_win"], o["pnl_down_win"])
        if p["kind"] == "hedge":
            self.stats["hedge_qty"] += float(fill.size)
            self.stats["hedge_cost"] += float(fill.size) * float(fill.price) + float(fill.fee)
            b = cyc.book
            st = cyc.swing
            A = st.initial_token if st is not None else cyc.a_side
            log.warning("[HEDGE_FILLED] %s %g %s @ %.4f fee %.4f -> A=%s %g / B=%s %g (UP %g / DOWN %g), total cost %.2f: guaranteed "
                        "payout %.2f, locked P&L %+.2f | UP-win %+.3f DOWN-win %+.3f", slug, fill.size, p["side"], fill.price, fill.fee,
                        A, b.qty(A), rc.OTHER[A], b.qty(rc.OTHER[A]), b.up_qty, b.down_qty, b.total_cost,
                        min(b.up_qty, b.down_qty), min(b.up_qty, b.down_qty) - b.total_cost + b.realised_exit_pnl,
                        b.resolution_pnl("UP"), b.resolution_pnl("DOWN"))
        if p["kind"] == "recovery":
            self.stats["recovery_qty"] += float(fill.size)
            self.stats["recovery_cost"] += float(fill.size) * float(fill.price) + float(fill.fee)
            o = rc.outcomes(cyc.book)
            log.info("[RECOVERY_FILLED] %s %g %s @ %.4f fee %.4f -> UP %g DOWN %g | UP-win %+.3f DOWN-win %+.3f min %+.3f | cost %.3f",
                     slug, fill.size, p["side"], fill.price, fill.fee, cyc.book.up_qty, cyc.book.down_qty, o["pnl_up_win"],
                     o["pnl_down_win"], o["min_settlement_pnl"], cyc.book.total_cost)
        if cyc.swing is not None:
            self._swing_state_log(slug, cyc)
        if p["filled_qty"] + 1e-9 >= p["q"]:
            p["done"] = True
            self._finalize(slug, now)

    def on_external_fill(self, slug: str, side: str, qty: float, price: float, fee: float, action: str, now: float,
                         kind: str = "external") -> None:
        """A fill that is not one of this engine's orders -- the dashboard's Buy / Sell, or a trade made on
        polymarket.com -- goes into the market's PairBook as kind "external", so the inventory the recovery
        arithmetic works from and the settlement reconciliation match the account."""
        cyc = self.cycles.get(slug)
        if cyc is None:
            cyc = rc.RecoveryCycle(book=pb.PairBook(slug), a_side=side, started_t=now, target=self.rprm.target_profit)
            cyc.to(rc.RState.INITIAL_ENTRY, now, f"{kind}: first fill from outside the engine")
            self.cycles[slug] = cyc
        n = sum(1 for f in cyc.book.fills if f.kind == "external") + 1
        token = next((f.token for f in cyc.book.fills if f.side == side), side)
        if action == "SELL" and kind == "MANUAL_SELL":
            # its order is booked now (live.py gives no order id here: the oldest one sent for this side)
            oid = next((o for o, (s, sd) in self._manual_sells.items() if s == slug and sd == side), None)
            if oid is not None:
                self._manual_sells.pop(oid)
        if action == "SELL":
            # the venue rounds to hundredths: a sale of "14.54" of the 14.538 we hold is the whole holding
            held = cyc.book.qty(side)
            if held <= 1e-9:
                log.warning("[EXTERNAL_FILL] %s %s: a sale of %g %s with none held in the engine's book: not booked", slug, kind, qty, side)
                return
            qty = min(float(qty), held)
        cyc.book.add_fill(pb.FillRecord(market_id=slug, ts=now, token=token, side=side, qty=float(qty), price=float(price),
                                        fee=float(fee), order_id=f"{kind.lower()}-{n}", fill_id=f"{kind.lower()}-{n}-1",
                                        action=action, expected_price=None, kind="external"))
        if cyc.state in (rc.RState.INITIAL_ENTRY, rc.RState.WAITING) and cyc.holds:
            if cyc.state is rc.RState.WAITING:
                cyc.to(rc.RState.INITIAL_ENTRY, now, kind)
            cyc.to(rc.RState.HOLDING, now, f"{kind} filled")
        o = rc.outcomes(cyc.book)
        cyc.events.append({"t": now, "event": f"{kind}_FILLED", "action": action, "side": side, "qty": qty, "price": price, "fee": fee,
                           **{k: o[k] for k in ("pnl_up_win", "pnl_down_win", "min_settlement_pnl")}})
        self.stats["external_fills"] = self.stats.get("external_fills", 0) + 1
        log.warning("[EXTERNAL_FILL] %s %s %s %g @ %.4f fee %.4f -> UP %g DOWN %g | UP-win %+.3f DOWN-win %+.3f", slug, kind, action,
                    qty, price, fee, cyc.book.up_qty, cyc.book.down_qty, o["pnl_up_win"], o["pnl_down_win"])

    # ───────────────────────────── settlement ─────────────────────────────

    def observe_point(self, slug: str, market, point: dict, now: float) -> None:
        super().observe_point(slug, market, point, now)
        if now < market.close_ts:
            return
        cyc = self.cycles.get(slug)
        if cyc is not None and cyc.state not in (rc.RState.SETTLEMENT_PENDING, rc.RState.SETTLED) and cyc.holds:
            rc.on_close(cyc, now)
        if cyc is not None and cyc.swing is not None and cyc.swing.phase is not sw.Phase.RESOLUTION and cyc.holds:
            sw.on_close(cyc.swing, now)

    def holds_window(self, slug: str) -> bool:
        c = self.cycles.get(slug)
        return c is not None and bool(c.book.fills)

    def forget_closed_windows(self, now: float, keep=()) -> None:
        for slug in [s for s in self.cycles if s not in keep and self.close_ts.get(s) and float(self.close_ts[s]) + 600_000.0 <= now]:
            self.cycles.pop(slug, None)
            log.warning("[RECOVERY] %s: restored cycle past the close dropped (no late resolution pending)", slug)

    def on_market_exited(self, slug: str, pnl: float, now: float) -> None:
        """live.py: every token of the market was sold (the profit exit): the market's result is realised now. The
        cycle settles on its realised exits, reconciled against the bot's figure; the market's later resolution finds
        no cycle and books nothing more.

        A profit exit sells both sides in one tick, and the venue can fill both before live.py books either: it then
        reports the market flat (with the whole P&L) while booking the FIRST sell, and once more (P&L 0, the positions
        are already cleared) with the second. The cycle is reconciled only once every sell of this exit is in its
        book; until then the bot's figures are summed (2026-10-06 21:44: DOWN-only +8.27 vs +1.21 halted entries)."""
        if slug not in self.cycles:
            return
        self._exit_venue[slug] = self._exit_venue.get(slug, 0.0) + float(pnl)
        waiting = ([oid for oid, (s, *_rest) in self._pexit.items() if s == slug]
                   + [oid for oid, (s, _side) in self._manual_sells.items() if s == slug])
        if waiting:
            log.info("[RECOVERY] %s is flat at the venue; %d more sell(s) of this exit to book before it is reconciled",
                     slug, len(waiting))
            return
        pnl = self._exit_venue.pop(slug)
        cyc = self.cycles.pop(slug)
        b = cyc.book
        mine = b.realised_exit_pnl
        s = self.stats
        s["settled_cycles"] += 1
        s["pnl"] += mine
        s["profit_exit_pnl"] += mine
        s["fees"] += b.fees
        s["recon_checked"] += 1
        diff = abs(mine - pnl)
        s["recon_max_diff"] = max(s["recon_max_diff"], diff)
        self.realised += mine
        self.settled_cycles.append({"slug": slug, "winner": "SOLD", "state": "EXITED", "a_side": cyc.a_side, **b.summary(),
                                    "pnl": round(mine, 4), "prediction": 0.0, "hedging": 0.0, "recovery_leg": 0.0, "profit_lock_leg": 0.0,
                                    "pairs": 0.0, "recoveries": [(x["side"], x["qty"], round(x["price"], 4)) for x in cyc.recoveries],
                                    "t": round(now)})
        if diff > RECON_TOL:
            s["recon_failures"] += 1
            self.accounting_halt = f"{slug}: sold out, engine {mine:+.4f} vs venue {pnl:+.4f}"
            log.error("[RECOVERY] RECONCILIATION_FAILURE %s: new entries halted", self.accounting_halt)
        else:
            log.warning("[RECOVERY_EXITED] %s sold out: P&L %+.4f (venue %+.4f) after %d recovery fills", slug, mine, pnl, len(cyc.recoveries))
        self._drop(slug)

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        winner = "UP" if won_up else "DOWN"
        cyc = self.cycles.pop(slug, None)
        held = cyc is not None and bool(cyc.book.fills)
        if not held:
            if abs(pnl) > RECON_TOL:
                self.stats["recon_failures"] += 1
                self.accounting_halt = f"{slug}: the bot booked {pnl:+.4f} on a market the engine holds no cycle for"
                log.error("[RECOVERY] RECONCILIATION_FAILURE %s: new entries halted", self.accounting_halt)
            self._drop(slug)
            return
        r = rc.on_settled(cyc, winner, now)
        b = cyc.book
        diff = max(abs(r["pnl"] - pnl), abs(r["pnl"] - r["pnl_from_cash"]), abs(r["pnl"] - r["pnl_reconstructed"]))
        s = self.stats
        s["settled_cycles"] += 1
        s["pnl"] += r["pnl"]
        s["first_legs"] += r["first_leg_pnl"]
        s["recovery_effect"] += r["recovery_effect"]
        s["late_leg"] += r["late_leg_pnl"]
        s["pair_leg"] += r.get("pair_leg_pnl", 0.0)
        s["fees"] += b.fees
        s["slippage"] += b.slippage
        s["min_settlement_sum"] += min(r["pnl_up_win"], r["pnl_down_win"])
        s["recovered"] += int(r["recovered"])
        s["reached_target"] += int(r["reached_target"])
        s["ended_negative"] += int(r["pnl"] < 0)
        s["recon_checked"] += 1
        if cyc.locked or cyc.lock_steps:
            s["locked_settled"] += 1
            s["locked_pnl"] += r["pnl"]
        s["recon_max_diff"] = max(s["recon_max_diff"], diff)
        if cyc.swing is not None:
            fin = sw.on_settled(cyc.swing, b, winner, now)
            bk = fin["bucket"]
            s["swing_settled"] += 1
            s[f"swing{bk}_markets"] += 1
            s[f"swing{bk}_pnl"] += fin["pnl"]
            s[f"swing{bk}_wins"] += int(fin["pnl"] > 0)
            self.swing_settled.append({"slug": slug, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in fin.items()}})
            log.warning("[SWING_SETTLED] %s %s won: swing %d (bucket %s, path %s, A=%s): UP %g / DOWN %g, cost %.2f -> payout %.2f, "
                        "P&L %+.2f (if UP won %+.2f / if DOWN won %+.2f)%s", slug, winner, fin["swing_number"], bk,
                        " ".join(fin["path"]) or "-", fin["initial_token"], fin["up_qty"], fin["down_qty"], fin["total_cost"],
                        fin["payout"], fin["pnl"], fin["pnl_if_up_wins"], fin["pnl_if_down_wins"],
                        "; hedged" + (" (balanced)" if fin["balanced"] else " (uneven)") if fin["hedged"] else "")
        u = self._ulr(cyc)
        if u is not None:
            fin = ulr.settle(u, b, winner, now)
            self._ulr_save(cyc, u)
            s["ulr_positions"] += 1
            s["ulr_hedged"] += int(u.state == ulr.HEDGED or (u.hedge_size > 0 and u.hedge_filled + 1e-6 >= u.hedge_size))
            s["ulr_original_pnl"] += fin["original_pnl"]
            if fin["recovered"]:
                s["ulr_recovered"] += 1
                s["ulr_pnl"] += fin["recovery_pnl"]
                s["ulr_original_pnl_recovered"] += fin["original_pnl"]
                s["ulr_combined_pnl"] += fin["combined_pnl"]
                s["ulr_worst"] = min(s["ulr_worst"], fin["recovery_pnl"])
                if fin["recovery_held_at_close"] > 1e-6:
                    s["ulr_resolution"] += 1
            self.ulr_settled.append({"slug": slug, "t": round(now), **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in fin.items()}})
            log.info("[ULR_SETTLED] %s %s won: original %+.4f, recovery %+.4f (%s), combined %+.4f, improvement %+.4f | %s",
                     slug, winner, fin["original_pnl"], fin["recovery_pnl"], fin["exit_kind"] or "no recovery", fin["combined_pnl"],
                     fin["improvement"], u.state)
        self.realised += r["pnl"]
        self.settled_cycles.append({"slug": slug, "winner": winner, "state": "SETTLED", "a_side": cyc.a_side, **b.summary(),
                                    "pnl": round(r["pnl"], 4), "prediction": round(r["first_leg_pnl"], 4),
                                    "hedging": round(r["recovery_effect"], 4), "recovery_leg": round(r["recovery_effect"], 4),
                                    "profit_lock_leg": 0.0, "pairs": round(b.locked_pair_pnl, 4),
                                    "recoveries": [(x["side"], x["qty"], round(x["price"], 4)) for x in cyc.recoveries], "t": round(now)})
        log.info("[RECOVERY_SETTLED] %s %s won: P&L %+.4f (first leg alone %+.4f, pair bid %+.4f on %g sh, recovery effect %+.4f, "
                 "late buys %+.4f on %d fills; %d recovery fills; UP-win %+.3f DOWN-win %+.3f) reached target %s%s", slug, winner, r["pnl"],
                 r["first_leg_pnl"], r.get("pair_leg_pnl", 0.0), r.get("pair_filled", 0.0), r["recovery_effect"], r["late_leg_pnl"],
                 r["late_fills"], len(cyc.recoveries), r["pnl_up_win"], r["pnl_down_win"], r["reached_target"],
                 f"; LOCKED ({cyc.lock_reason}, {cyc.lock_steps} lock orders)" if (cyc.locked or cyc.lock_steps) else "")
        if diff > RECON_TOL:
            s["recon_failures"] += 1
            self.accounting_halt = f"{slug}: engine {r['pnl']:+.4f} vs venue {pnl:+.4f} / cash {r['pnl_from_cash']:+.4f} / rebuilt {r['pnl_reconstructed']:+.4f}"
            log.error("[RECOVERY] RECONCILIATION_FAILURE %s (tolerance %.2f): new entries halted", self.accounting_halt, RECON_TOL)
        else:
            log.info("[RECOVERY] RECONCILED %s: engine %+.4f = venue %+.4f = cash = rebuilt (diff %.6f)", slug, r["pnl"], pnl, diff)
        self._drop(slug)

    def _drop(self, slug: str) -> None:
        self._open_tries.pop(slug, None)
        self._high_count.pop(slug, None)
        self._pair_due.discard(slug)
        for d in (self.pending, self.candidates, self.hist, self.sigma, self.last_p, self._decisions, self.quotes):
            d.pop(slug, None)

    # ───────────────────────────── restarts, dashboard ─────────────────────────────

    def persist_state(self) -> dict:
        return {"pairlock_recovery": {slug: {"close_ts": self.close_ts.get(slug), "cycle": c.to_json()}
                                      for slug, c in self.cycles.items() if c.book.fills},
                "pairlock_recovery_stats": dict(self.stats), "pairlock_recovery_realised": self.realised}

    def restore_state(self, doc: dict | None, now: float) -> None:
        doc = doc or {}
        for slug, d in (doc.get("pairlock_recovery") or {}).items():
            closed = d.get("close_ts") and float(d["close_ts"]) + 600_000.0 <= now
            if closed and float(d["close_ts"]) + 24 * 3600_000.0 <= now:
                continue
            c = rc.RecoveryCycle.from_json(d["cycle"])
            if not c.book.fills or (closed and c.state is not rc.RState.SETTLEMENT_PENDING):
                continue
            self.cycles[slug] = c
            self.close_ts[slug] = d.get("close_ts")
            if c.ulr:
                u = ulr.ULR.from_json(c.ulr, restart=True)       # an order in flight at the stop resolved while we were down
                c.ulr = u.to_json()
                log.warning("[ULR] %s restored: %s, recovery %g %s open", slug, u.state, u.open_qty, u.recovery_leg)
            log.warning("[RECOVERY] %s restored from its fills: %s UP %g DOWN %g (%d recovery fills)", slug, c.state.value,
                        c.book.up_qty, c.book.down_qty, len(c.recoveries))
        if doc.get("pairlock_recovery") is None and doc.get("pairlock_v2"):
            log.warning("[RECOVERY] a strategy-B state file: its cycles are NOT restored (fresh run expected)")

    def _ulr_snapshot(self) -> dict:
        st = self.stats
        n_rec = st["ulr_recovered"]
        open_ = []
        for slug, c in self.cycles.items():
            u = self._ulr(c)
            if u is None or u.state in (ulr.HEDGED, ulr.RECOVERY_CLOSED):
                continue
            open_.append({"slug": slug, "position_id": u.position_id, "state": u.state, "original_leg": u.original_leg,
                          "recovery_leg": u.recovery_leg, "unhedged_qty": round(u.unhedged_qty, 2), "hedge_target": u.hedge_target,
                          "entry_qty": round(u.entry_qty, 2), "entry_avg": None if u.entry_avg is None else round(u.entry_avg, 4),
                          "open_qty": round(u.open_qty, 2), "attempts": u.attempts, "last_reason": u.reasons[-1] if u.reasons else None})
        return {"enabled": bool(self.rprm.ulr_enabled), "candidates": st["ulr_candidates"], "signals": st["ulr_signals"],
                "entries": st["ulr_sent"], "fills": st["ulr_fills"], "kills": st["ulr_kills"], "exits": st["ulr_exits_sent"],
                "exit_fills": st["ulr_exit_fills"], "exit_kills": st["ulr_exit_kills"], "take_profits": st["ulr_tp"],
                "stop_losses": st["ulr_sl"], "signal_exits": st["ulr_signal_exits"], "emergency_exits": st["ulr_emergency"],
                "held_to_resolution": st["ulr_resolution"], "skipped": st["ulr_skipped"],
                "unhedged_positions_settled": st["ulr_positions"] - st["ulr_hedged"], "hedged_positions_settled": st["ulr_hedged"],
                "recovered_positions": n_rec, "recovery_pnl": round(st["ulr_pnl"], 4), "recovery_fees": round(st["ulr_fees"], 4),
                "original_unhedged_pnl": round(st["ulr_original_pnl"], 4),
                "original_pnl_of_recovered": round(st["ulr_original_pnl_recovered"], 4),
                "combined_pnl_of_recovered": round(st["ulr_combined_pnl"], 4),
                "avg_recovery_pnl": round(st["ulr_pnl"] / n_rec, 4) if n_rec else None,
                "avg_loss_without_recovery": round(st["ulr_original_pnl_recovered"] / n_rec, 4) if n_rec else None,
                "avg_loss_with_recovery": round(st["ulr_combined_pnl"] / n_rec, 4) if n_rec else None,
                "recovery_improvement": round(st["ulr_pnl"], 4), "worst_recovery_loss": round(st["ulr_worst"], 4),
                "reasons": dict(self.ulr_reasons), "open": open_, "settled": list(self.ulr_settled)[-20:],
                "params": {k: v for k, v in vars(self.rprm).items() if k.startswith("ulr_")}}

    def _swing_snapshot(self) -> dict:
        """Swing mode (pairlock_swing): the parameters, settled markets by the deepest swing reached, and every open
        market's accounting (inventory, both resolution outcomes, capital)."""
        st, p = self.stats, self.rprm
        out = {"enabled": bool(p.swing_mode),
               "params": {k: v for k, v in vars(p).items() if k.startswith("swing")}
               | {"max_slippage": p.max_slippage, "min_time_left_s": p.min_time_left_s, "cooldown_ms": p.cooldown_ms,
                  "max_total_position": p.max_total_position},
               "recognised_swings": st["swing_recognised"], "entries_sent": st["swing_entries_sent"],
               "entry_fills": st["swing_entry_fills"], "hedges_sent": st["hedge_sent"], "hedge_fills": st["hedge_fills"],
               "hedge_kills": st["hedge_kills"], "hedge_qty": round(st["hedge_qty"], 2), "hedge_cost": round(st["hedge_cost"], 4),
               "settled": st["swing_settled"],
               "by_swing": {b: {"markets": st[f"swing{b}_markets"], "pnl": round(st[f"swing{b}_pnl"], 4),
                                "wins": st[f"swing{b}_wins"]} for b in ("0", "1", "2", "3+")},
               "open": [], "recent": list(self.swing_settled)[-20:]}
        if not p.swing_mode:
            return out
        for slug, c in self.cycles.items():
            if c.swing is None or not c.book.fills:
                continue
            ub, ua, db, da = self.quotes.get(slug, (None, None, None, None))
            mv = MarketView(bid_up=ub, ask_up=ua, bid_down=db, ask_down=da)
            acc = sw.accounting(c.swing, c.book, p, mv)
            out["open"].append({"slug": slug, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in acc.items()}})
        return out

    def snapshot(self) -> dict:
        snap = super().snapshot()
        open_, deployed, mtm, exp_pnl, worst = [], 0.0, 0.0, 0.0, 0.0
        for slug, c in self.cycles.items():
            if not c.book.fills and not c.book.pending:
                continue
            ub, db = self.last_bids.get(slug, (None, None))
            p = self.last_p.get(slug)
            b = c.book
            s = b.summary(ub, db, p)
            deployed += b.cash_spent
            mtm += s.get("mark_to_market_pnl") or 0.0
            exp_pnl += s.get("expected_pnl") or 0.0
            worst += b.worst_case_pnl
            open_.append({"slug": slug, "state": c.swing.phase.value if c.swing is not None else c.state.value,
                          "a_side": c.a_side, **s, "hedge_actions": c.actions,
                          "recovery_qty": sum(x["qty"] for x in c.recoveries), "profit_lock_qty": 0.0,
                          "p_up": None if p is None else round(p, 4),
                          "locked_label": sw.label(c.swing, b, self.rprm) if c.swing is not None else
                          f"UP-win {b.resolution_pnl('UP'):+.2f} / DOWN-win {b.resolution_pnl('DOWN'):+.2f}; "
                          f"{len(c.recoveries)} recovery fills"})
        st = self.stats
        n = st["settled_cycles"]
        snap["pairlock"] = {
            "version": 4, "strategy": "R",
            "stats": {**{k: (round(v, 6) if isinstance(v, float) else v) for k, v in st.items()}, "pair_locked": 0,
                      "reentry_blocked": st["reentry_blocked"]},
            "recovery": {"evaluations": st["evaluations"], "opportunities": st["opportunities"], "sent": st["recovery_sent"],
                         "filled": st["recovery_fills"], "kills": st["recovery_kills"], "partial": st["recovery_partial"],
                         "up_orders": st["recovery_up"], "down_orders": st["recovery_down"], "qty": round(st["recovery_qty"], 2),
                         "avg_price": round(st["recovery_cost"] / st["recovery_qty"], 4) if st["recovery_qty"] else None,
                         "settled": n, "recovered_pct": round(100.0 * st["recovered"] / n, 1) if n else None,
                         "reached_target_pct": round(100.0 * st["reached_target"] / n, 1) if n else None,
                         "ended_negative_pct": round(100.0 * st["ended_negative"] / n, 1) if n else None,
                         "avg_min_settlement": round(st["min_settlement_sum"] / n, 4) if n else None,
                         "settlement_pnl": round(st["pnl"], 4), "leg_pnl": round(st["recovery_effect"], 4),
                         "downside_reduction": None, "avg_pair_cost": None, "candidates": st["opportunities"],
                         "rejected": sum(self.rejections.values())},
            "profit_lock": {"opportunities": st["late_evaluations"], "rejected": st["late_evaluations"] - st["late_candidates"],
                            "sent": st["late_sent"], "filled": st["late_fills"], "kills": st["late_kills"], "partial": 0,
                            "qty": round(st["late_qty"], 2), "locked_profit": round(st["late_leg"], 4), "settled": n,
                            "settlement_pnl": round(st["late_leg"], 4), "leg_pnl": round(st["late_leg"], 4), "label": "LATE BUYS"},
            "baseline": {"no_hedge_pnl": round(st["first_legs"], 4), "actual_pnl": round(st["pnl"], 4),
                         "hedging_effect": round(st["recovery_effect"], 4)},
            "ulr": self._ulr_snapshot(),
            "swing": self._swing_snapshot(),
            "refused": dict(self.rejections), "open": open_, "settled": list(self.settled_cycles)[-20:],
            "decisions": list(self.decisions), "model": self.plmodel.meta.get("fitted_on"), "entry_gate": self.plcfg.entry_gate,
            "accounting_halt": self.accounting_halt,
            "totals": {"realised_pnl": round(self.realised, 4), "unrealised_mtm_pnl": round(mtm, 4),
                       "total_pnl": round(self.realised + mtm, 4), "expected_pnl_open": round(exp_pnl, 4),
                       "worst_case_open": round(worst, 4), "fees": round(st["fees"], 4), "slippage": round(st["slippage"], 4),
                       "capital_deployed": round(deployed, 4), "capital_remaining_per_market": self.rprm.max_capital},
            "params": {**{k: v for k, v in vars(self.prm).items() if k in ("entry_edge", "entry_mode", "short_min", "entry_shares",
                                                                           "band_lo", "band_hi")},
                       **{"recovery_" + k: v for k, v in vars(self.rprm).items()}},
            "limits": dict(vars(self.lim))}
        return snap
