"""CROSS_MARKET_ARB engine: seven-market guaranteed-profit portfolio scanner.

EXPERIMENTAL, PAPER ONLY. Behind ``TPB_CMA_ENABLED=1``; ``__main__.check_cross_market_arb``
refuses shadow / micro-live / live. Independent data tag, cash reservations, and risk
caps — does not share capital with other bots.
"""
from __future__ import annotations

import logging
from typing import Any

from ...features.engine import FeatureEngine
from ...risk.limits import RiskManager
from ...signals.costs import FeeSchedule
from ...types import Order, Side, TimeInForce
from ..engine import EngineConfig, Evaluation, Reason
from ..momentum import get_executable_price
from ..spot_lag import SpotLagEngine
from .config import ENABLE_ENV, STRATEGY_ID, CrossMarketArbConfig
from .execution import (
    EVT_CAPITAL_RESERVE, EVT_CIRCUIT, EVT_COMPLETE, EVT_INCOMPLETE, EVT_OPPORTUNITY,
    EVT_ORDER_CANCEL, EVT_ORDER_SUBMIT, EVT_PREFLIGHT_PASS, EVT_PREFLIGHT_REJECT,
    EVT_RECONCILE, EVT_RISK_REJECT, ArbState, PortfolioPlan, as_dict, client_order_key,
    mark_fill, plan_from_candidate, plan_from_dict, record_event, refresh_actuals,
)
from .math import LegQuote, estimate_leg, is_guaranteed_profit, portfolio_metrics
from .optimizer import MarketBooks, OptimizeResult, optimize_portfolio

log = logging.getLogger("cross_market_arb")


class CrossMarketArbEngine(SpotLagEngine):
    """SpotLag surface for live.py plumbing; decisions come from the portfolio optimizer.

    Live crypto 5m books almost never offer taker arb (ask_up+ask_down >= 1.01 after
    fees). Guaranteed profit is captured by resting maker bids on both sides when
    bid_up+bid_down clears the configured net-profit floor (makers pay no fee).
    """

    feature_key = "cross_market_arb"
    stacks_positions = True
    entry_retry_ms = 200.0
    budget_is_ceiling = True
    requires_strike = False
    requires_market_open = True
    #: live.py pair-order hooks (same path as preopen_maker / rising_recovery)
    wants_books = True
    one_pair_per_market_side = True
    fill_at_touch = True

    def __init__(self, cfg: EngineConfig, scfg: CrossMarketArbConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the cross_market_arb strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, CrossMarketArbConfig):
            raise TypeError("CrossMarketArbEngine needs a CrossMarketArbConfig")
        # Keep SpotLag plumbing fields aligned with the dedicated CMA knobs.
        scfg.max_slippage = float(scfg.maximum_slippage)
        scfg.max_spread = float(scfg.maximum_spread)
        scfg.max_quote_age_ms = float(scfg.maximum_quote_age_ms)
        scfg.max_daily_loss_usdc = float(scfg.daily_loss_limit)
        scfg.assets = scfg.markets or scfg.assets
        super().__init__(cfg, scfg, risk, features)
        self.scfg: CrossMarketArbConfig = scfg
        n = max(2, len(scfg.asset_list()))
        risk.cfg.max_concurrent_positions = max(risk.cfg.max_concurrent_positions, 2 * n)
        self.market_quotes: dict[str, dict] = {}
        self.plans: dict[str, PortfolioPlan] = {}
        self.active_plan_id: str | None = None
        self.state: str = ArbState.IDLE.value
        self.last_status: dict[str, Any] = {}
        self.reject_counts: dict[str, int] = {}
        self.stats = {
            "opportunities_detected": 0,
            "opportunities_rejected": 0,
            "orders_submitted": 0,
            "orders_filled": 0,
            "portfolios_complete": 0,
            "portfolios_incomplete": 0,
            "risk_rejects": 0,
            "circuit_trips": 0,
            "maker_pairs_armed": 0,
            "maker_pairs_complete": 0,
            "maker_pairs_incomplete": 0,
        }
        self._order_seq: dict[str, int] = {}
        self._daily_halt = False
        #: slug -> maker pair record (resting GTC bids on UP+DOWN)
        self.maker_markets: dict[str, dict] = {}
        self._by_order: dict[str, tuple[str, str]] = {}
        self.guard_paused = False
        self.risk_halted = False
        self._last_scan_log_ms = 0.0

    # ------------------------------------------------------------------ persistence

    def persist_state(self) -> dict:
        return {
            "cma_plans": {k: {**as_dict(p), "client_order_keys": list(p.client_order_keys)}
                          for k, p in self.plans.items()},
            "cma_active": self.active_plan_id,
            "cma_state": self.state,
            "cma_stats": dict(self.stats),
            "cma_order_seq": dict(self._order_seq),
            "cma_daily_halt": self._daily_halt,
            "cma_maker_markets": dict(self.maker_markets),
        }

    def restore_state(self, doc: dict | None, now: float) -> None:
        doc = doc or {}
        self.plans = {}
        for k, raw in (doc.get("cma_plans") or {}).items():
            plan = plan_from_dict(raw)
            # Reconcile on restart: never re-arm completed / incomplete plans as fresh.
            if plan.complete:
                plan.state = ArbState.COMPLETE.value
            elif plan.incomplete:
                plan.state = ArbState.INCOMPLETE.value
            elif plan.state in (ArbState.EXECUTING.value, ArbState.PREFLIGHT.value,
                               ArbState.OPPORTUNITY_FOUND.value, ArbState.RECONCILING.value):
                plan.state = ArbState.RECONCILING.value
                refresh_actuals(plan, [l.slug for l in plan.legs])
                record_event(plan, EVT_RECONCILE, now, reason="process_restart")
            self.plans[str(k)] = plan
        self.active_plan_id = doc.get("cma_active")
        self.state = str(doc.get("cma_state") or ArbState.IDLE.value)
        self.stats.update(doc.get("cma_stats") or {})
        self._order_seq = {str(k): int(v) for k, v in (doc.get("cma_order_seq") or {}).items()}
        self._daily_halt = bool(doc.get("cma_daily_halt"))
        self.maker_markets = {str(k): v for k, v in (doc.get("cma_maker_markets") or {}).items()}
        self._by_order = {}
        for slug, rec in self.maker_markets.items():
            for side, o in (rec.get("orders") or {}).items():
                oid = o.get("order_id")
                if oid:
                    self._by_order[str(oid)] = (slug, str(side))

    # ------------------------------------------------------------------ fills / settle

    def on_fill_detail(self, slug: str, side: str, price: float, size: float, fee: float,
                       now: float) -> None:
        plan = self._active_plan()
        if plan is None:
            return
        slugs = [l.slug for l in plan.legs]
        row = mark_fill(plan, slug, side, size, price, fee, now, slugs)
        if row is None:
            return
        self.stats["orders_filled"] += 1
        log.info("cma FILL plan=%s %s %s size=%.4f px=%.4f min_profit=%s",
                 plan.plan_id, slug, side, size, price, plan.actual_min_profit)
        self._after_fill(plan, now)

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        self.market_quotes.pop(slug, None)
        rec = self.maker_markets.pop(slug, None)
        if rec is not None:
            for side, o in (rec.get("orders") or {}).items():
                oid = o.get("order_id")
                if oid:
                    self._by_order.pop(str(oid), None)
        for pid, plan in list(self.plans.items()):
            if any(l.slug == slug for l in plan.legs):
                if plan.complete or plan.incomplete:
                    self.plans.pop(pid, None)
                if self.active_plan_id == pid:
                    self.active_plan_id = None
                    self.state = ArbState.IDLE.value

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap,
                 latency, balance, orderflow=None, quote_ages=None) -> Evaluation:
        m = meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        if not self.scfg.asset_ok(m.asset):
            ev.reason = Reason.ASSET_FILTERED
            return ev

        ages = quote_ages or {}
        self.market_quotes[m.slug] = {
            "t": now, "meta": meta, "up": book_up, "down": book_down,
            "valid": True, "quote_ages": ages, "book_age_ms": book_age_ms,
        }

        if self._daily_halt or self._circuit_tripped(now):
            self.state = ArbState.RISK_HALT.value
            self.last_status = {"mode": "circuit", "why": "daily_loss_limit"}
            ev.reason = Reason.RISK_LIMIT
            ev.detail = "daily circuit breaker"
            return ev

        plan = self._active_plan()
        if plan is not None and plan.state in (
            ArbState.EXECUTING.value, ArbState.RECONCILING.value, ArbState.PREFLIGHT.value,
            ArbState.OPPORTUNITY_FOUND.value,
        ):
            out = self._execute_next(plan, now, balance)
            if out is not None:
                return out
            # No order this tick — fall through to idle scan only if plan finished.
            if plan.complete or plan.incomplete:
                plan = None

        if plan is not None and not (plan.complete or plan.incomplete):
            ev.reason = Reason.COOLDOWN
            ev.detail = f"plan {plan.plan_id} state={plan.state}"
            self.last_status = {"mode": "busy", "plan": plan.plan_id, "state": plan.state}
            return ev

        mode = (self.scfg.entry_mode or "maker").strip().lower()
        # Maker path places GTC bids via pair_requests; evaluate only reports status.
        if mode in ("maker", "both"):
            best = self._best_maker_pair_diag()
            armed = sum(1 for r in self.maker_markets.values() if not r.get("done"))
            self.last_status = {
                "mode": "maker_scan",
                "why": best.get("why", "waiting_pair_requests"),
                "best_maker_sum": best.get("sum"),
                "best_maker_profit": best.get("profit"),
                "maker_armed": armed,
                "entry_mode": mode,
                "rejects": dict(self.reject_counts),
            }
            if mode == "maker":
                self.state = ArbState.SCANNING.value if armed == 0 else ArbState.EXECUTING.value
                return self._reject(ev, Reason.WEAK_MOMENTUM,
                                    f"maker pair bids via pair_requests ({best.get('why', 'idle')})")

        self.state = ArbState.SCANNING.value
        result = self._scan(now, balance)
        if result.best is None:
            self.state = ArbState.IDLE.value
            why = self._taker_reject_why(result)
            self.reject_counts[why] = self.reject_counts.get(why, 0) + 1
            self.stats["opportunities_rejected"] += 1
            self.last_status = {
                "mode": "scanning", "why": why, "evaluated": result.evaluated,
                "rejects": dict(self.reject_counts), "entry_mode": mode,
                "best_ask_sum": self._best_ask_sum(),
            }
            if now - self._last_scan_log_ms > 30_000:
                self._last_scan_log_ms = now
                log.info("cma SCAN idle why=%s best_ask_sum=%s maker_diag=%s",
                         why, self.last_status.get("best_ask_sum"), self._best_maker_pair_diag())
            return self._reject(ev, Reason.EDGE_TOO_SMALL, why)

        self.stats["opportunities_detected"] += 1
        epoch = int(m.open_ts // 1000) if m.open_ts else 0
        # Prefer epoch from quote set if aligned
        for q in self.market_quotes.values():
            mm = q.get("meta")
            if mm is not None:
                epoch = int(mm.market.open_ts // 1000)
                break
        plan = plan_from_candidate(result.best, epoch, now)
        record_event(plan, EVT_OPPORTUNITY, now,
                     min_profit=result.best.metrics.min_net_profit,
                     cost=result.best.metrics.acquisition_cost,
                     n_legs=len(result.best.legs))
        self.state = ArbState.PREFLIGHT.value
        plan.state = ArbState.PREFLIGHT.value
        ok, reason = self._preflight(plan, now, balance)
        if not ok:
            plan.state = ArbState.PREFLIGHT_REJECT.value
            plan.reject_reason = reason
            record_event(plan, EVT_PREFLIGHT_REJECT, now, reason=reason)
            self.stats["opportunities_rejected"] += 1
            self.stats["risk_rejects"] += 1
            self.last_status = {"mode": "preflight_reject", "why": reason}
            self.state = ArbState.IDLE.value
            ev.reason = Reason.RISK_LIMIT
            ev.detail = reason
            return ev

        record_event(plan, EVT_PREFLIGHT_PASS, now, reserved=plan.reserved_cash)
        record_event(plan, EVT_CAPITAL_RESERVE, now, reserved=plan.reserved_cash)
        plan.state = ArbState.EXECUTING.value
        self.state = ArbState.EXECUTING.value
        self.plans[plan.plan_id] = plan
        self.active_plan_id = plan.plan_id
        log.info("cma ARMED plan=%s legs=%d cost=%.4f min_profit=%.4f",
                 plan.plan_id, len(plan.legs), plan.modeled_cost, plan.modeled_min_profit)
        out = self._execute_next(plan, now, balance)
        return out if out is not None else self._idle_eval(ev, "armed_waiting")

    def revalidate(self, ev: Evaluation, book, min_edge: float | None = None) -> Reason | None:
        info = (ev.features or {}).get("cross_market_arb", {})
        if not info.get("cma_leg"):
            return super().revalidate(ev, book, min_edge)
        if book is None or book.best_ask is None:
            return Reason.NO_OFFER
        exec_price, _ = get_executable_price(book, ev.size, ev.limit_price)
        if exec_price is None:
            return Reason.HIGH_SLIPPAGE if book.best_ask > ev.limit_price + 1e-9 else Reason.LOW_LIQUIDITY
        if ev.max_entry_price is not None and exec_price > ev.max_entry_price + 1e-9:
            return Reason.ASK_ABOVE_MAX_ENTRY
        return None

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        tif = TimeInForce.IOC if self.scfg.order_type.upper() == "FAK" else TimeInForce.FOK
        return Order(
            token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
            tif=tif, decision_ts=now_ms or 0.0,
        )

    def signal_text(self, ev: Evaluation) -> str:
        info = (ev.features or {}).get("cross_market_arb", {})
        return (f"{ev.side} cross_market_arb plan={info.get('plan_id', '')} "
                f"min_profit={info.get('modeled_min_profit', 0):+.3f}")

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = STRATEGY_ID
        snap["cross_market_arb_config"] = self.scfg.as_dict()
        snap["cross_market_arb"] = {
            "state": self.state,
            "active_plan_id": self.active_plan_id,
            "last_status": dict(self.last_status),
            "reject_counts": dict(self.reject_counts),
            "stats": dict(self.stats),
            "daily_halt": self._daily_halt,
            "maker_markets": {
                k: {
                    "asset": v.get("asset"), "state": v.get("state"),
                    "prices": v.get("prices"), "filled": v.get("filled"),
                    "target": v.get("target"), "modeled_profit": v.get("modeled_profit"),
                }
                for k, v in self.maker_markets.items() if not v.get("done")
            },
        }
        snap["cross_market_arb_plans"] = {k: as_dict(p) for k, p in self.plans.items()}
        # Surface CMA reject reasons on the STATUS "why not trading" line.
        for k, v in self.reject_counts.items():
            self.rejections[str(k)] = int(v)
        return snap

    # ------------------------------------------------------------------ internals

    def _active_plan(self) -> PortfolioPlan | None:
        if not self.active_plan_id:
            return None
        return self.plans.get(self.active_plan_id)

    def _idle_eval(self, ev: Evaluation, why: str) -> Evaluation:
        ev.reason = Reason.COOLDOWN
        ev.detail = why
        return ev

    def _circuit_tripped(self, now: float) -> bool:
        if self._daily_halt:
            return True
        # RiskManager tracks realised_today; halt when past dedicated daily limit.
        loss = -min(0.0, float(getattr(self.risk, "realised_today", 0.0)))
        if self.scfg.daily_loss_limit > 0 and loss >= self.scfg.daily_loss_limit - 1e-9:
            if not self._daily_halt:
                self._daily_halt = True
                self.stats["circuit_trips"] += 1
                log.error("cma DAILY_CIRCUIT_BREAKER loss=%.4f limit=%.4f", loss, self.scfg.daily_loss_limit)
                plan = self._active_plan()
                if plan is not None:
                    record_event(plan, EVT_CIRCUIT, now, loss=loss)
            return True
        return False

    def _quote_for(self, slug: str, side: str, meta, book, ages: dict) -> LegQuote | None:
        if book is None or book.best_ask is None or book.best_bid is None:
            return None
        token = meta.market.yes_token_id if side == "UP" else meta.market.no_token_id
        fee: FeeSchedule = getattr(meta, "fee", None) or FeeSchedule()
        levels = tuple((float(l.price), float(l.size)) for l in (book.asks or [])[:12])
        depth = sum(sz for px, sz in levels if px <= book.best_ask + self.scfg.maximum_slippage + 1e-9)
        age = float(ages.get(token, 0.0) or 0.0)
        return LegQuote(
            slug=slug, asset=meta.market.asset, side=side,
            ask=float(book.best_ask), bid=float(book.best_bid),
            depth=float(depth), fee_fn=fee.taker_fee_per_share,
            tick=float(getattr(meta.market, "tick_size", 0.01) or 0.01),
            min_size=float(getattr(meta.market, "min_size", 5.0) or 5.0),
            quote_age_ms=age,
            quote_ts_ms=float(getattr(book, "ts", 0.0) or 0.0),
            levels=levels,
        )

    def _collect_books(self, now: float) -> tuple[list[MarketBooks] | None, str]:
        want = set(self.scfg.asset_list())
        by_asset: dict[str, dict] = {}
        for slug, q in self.market_quotes.items():
            meta = q.get("meta")
            if meta is None:
                continue
            asset = meta.market.asset.upper()
            if asset not in want:
                continue
            # Prefer freshest quote per asset
            prev = by_asset.get(asset)
            if prev is None or float(q.get("t", 0)) >= float(prev.get("t", 0)):
                by_asset[asset] = q
        if not by_asset:
            return None, "missing_markets:all"
        # Trade any ready subset (taker multi-market arb does not require all seven).
        books: list[MarketBooks] = []
        ages: list[float] = []
        for asset in self.scfg.asset_list():
            q = by_asset.get(asset)
            if q is None:
                continue
            meta = q["meta"]
            ages_map = q.get("quote_ages") or {}
            up = self._quote_for(meta.market.slug, "UP", meta, q.get("up"), ages_map)
            dn = self._quote_for(meta.market.slug, "DOWN", meta, q.get("down"), ages_map)
            if up is None or dn is None:
                continue
            if up.quote_age_ms > self.scfg.maximum_quote_age_ms or dn.quote_age_ms > self.scfg.maximum_quote_age_ms:
                continue
            if (up.ask - up.bid) > self.scfg.maximum_spread + 1e-9 or (dn.ask - dn.bid) > self.scfg.maximum_spread + 1e-9:
                continue
            left = meta.market.seconds_remaining(now)
            if left < self.scfg.min_time_remaining_s + self.scfg.execution_buffer_s:
                continue
            ages.extend([up.quote_age_ms, dn.quote_age_ms])
            books.append(MarketBooks(asset=asset, slug=meta.market.slug, up=up, down=dn,
                                     time_remaining_s=left))
        if not books:
            missing = sorted(want - set(by_asset))
            return None, f"no_ready_books:{','.join(missing) if missing else 'gates'}"
        # Skew uses quote ages (local freshness), not venue book.ts which differs across markets.
        if ages and (max(ages) - min(ages)) > self.scfg.maximum_timestamp_skew_ms + 1e-9:
            return None, f"quote_skew:{max(ages) - min(ages):.0f}ms"
        return books, ""

    def _reserved_cash(self, exclude_plan: str | None = None) -> float:
        total = 0.0
        for pid, plan in self.plans.items():
            if exclude_plan and pid == exclude_plan:
                continue
            if plan.complete or plan.incomplete:
                continue
            if plan.state in (ArbState.EXECUTING.value, ArbState.PREFLIGHT.value,
                             ArbState.OPPORTUNITY_FOUND.value, ArbState.RECONCILING.value):
                total += max(plan.reserved_cash, plan.remaining_notional())
        return total

    def _scan(self, now: float, balance: float) -> OptimizeResult:
        books, err = self._collect_books(now)
        if books is None:
            return OptimizeResult(None, state="NO_OPPORTUNITY")
        # Concurrent opportunity cap
        active = sum(1 for p in self.plans.values()
                     if not p.complete and not p.incomplete
                     and p.state not in (ArbState.IDLE.value, ArbState.PREFLIGHT_REJECT.value,
                                        ArbState.CANCELLED.value, ArbState.COMPLETE.value))
        if active >= self.scfg.maximum_concurrent_opportunities:
            return OptimizeResult(None, state="NO_OPPORTUNITY")
        spendable = max(0.0, balance - self._reserved_cash() - self.scfg.minimum_cash_reserve)
        return optimize_portfolio(
            books,
            minimum_net_profit=self.scfg.minimum_net_profit,
            maximum_basket_notional=min(self.scfg.maximum_basket_notional,
                                        self.scfg.maximum_capital_per_opportunity),
            maximum_market_exposure=self.scfg.maximum_market_exposure,
            available_cash=spendable + self.scfg.minimum_cash_reserve,
            minimum_cash_reserve=self.scfg.minimum_cash_reserve,
            max_shares_per_leg=self.scfg.max_shares_per_leg,
            share_step=self.scfg.share_step,
            slippage=self.scfg.maximum_slippage,
        )

    def _preflight(self, plan: PortfolioPlan, now: float, balance: float) -> tuple[bool, str]:
        books, err = self._collect_books(now)
        if books is None:
            return False, err or "books"
        # Outstanding notional
        outstanding = self._reserved_cash(exclude_plan=plan.plan_id) + plan.reserved_cash
        if (self.scfg.maximum_total_outstanding_notional > 0
                and outstanding > self.scfg.maximum_total_outstanding_notional + 1e-9):
            return False, f"outstanding_notional:{outstanding:.2f}"
        spendable = balance - self._reserved_cash(exclude_plan=plan.plan_id) - self.scfg.minimum_cash_reserve
        if plan.reserved_cash > spendable + 1e-9:
            return False, f"cash:{plan.reserved_cash:.2f}>{spendable:.2f}"
        # Re-price legs against current books
        by_slug = {b.slug: b for b in books}
        repriced_cost = 0.0
        for leg in plan.legs:
            b = by_slug.get(leg.slug)
            if b is None:
                return False, f"missing:{leg.slug}"
            q = b.up if leg.side == "UP" else b.down
            planned = estimate_leg(q, leg.target_shares, self.scfg.maximum_slippage)
            if planned is None:
                return False, f"depth:{leg.asset}:{leg.side}"
            if planned.limit > leg.limit + 1e-9:
                # tighten? reject if worse than armed limit
                return False, f"reprice:{leg.asset}:{leg.side}"
            repriced_cost += planned.total_cost
            leg.limit = min(leg.limit, planned.limit)
        plan.reserved_cash = repriced_cost
        plan.modeled_cost = repriced_cost
        # Worst-case loss during execution = full reserved capital if nothing settles
        if (self.scfg.maximum_worst_case_loss_during_execution > 0
                and repriced_cost > self.scfg.maximum_worst_case_loss_during_execution + 1e-9):
            return False, f"worst_case_loss:{repriced_cost:.2f}"
        return True, "ok"

    def _execute_next(self, plan: PortfolioPlan, now: float, balance: float) -> Evaluation | None:
        if plan.state == ArbState.RECONCILING.value:
            plan.state = ArbState.EXECUTING.value
        pending = plan.pending_legs()
        if not pending:
            return self._complete_or_incomplete(plan, now)

        if self.scfg.revalidate_before_submit:
            ok, reason = self._preflight(plan, now, balance)
            if not ok:
                return self._abort_plan(plan, now, reason)

            # After partial fills, require the *filled* book still to be on track OR
            # remaining legs still able to restore guarantee.
            actual = refresh_actuals(plan, [l.slug for l in plan.legs])
            if plan.position_legs() and actual["min_net_profit"] < -self.scfg.maximum_incomplete_exposure:
                return self._abort_plan(plan, now, "incomplete_exposure")

        # Pick next unfinished leg (deterministic: asset order then UP before DOWN)
        order_assets = {a: i for i, a in enumerate(self.scfg.asset_list())}
        pending.sort(key=lambda l: (order_assets.get(l.asset, 99), 0 if l.side == "UP" else 1, l.slug))
        leg = pending[0]
        if leg.last_submit_ms and (now - leg.last_submit_ms) < self.scfg.leg_retry_ms:
            return None

        qrow = self.market_quotes.get(leg.slug)
        if qrow is None:
            return self._abort_plan(plan, now, f"missing_quote:{leg.slug}")
        meta = qrow["meta"]
        book = qrow["up"] if leg.side == "UP" else qrow["down"]
        need = max(0.0, leg.target_shares - leg.filled_shares)
        min_sz = float(getattr(meta.market, "min_size", 5.0) or 5.0)
        if 0 < need < min_sz:
            # Cannot top up below venue minimum without overshooting — mark done if dust
            if need <= 0.05:
                leg.done = True
                return self._execute_next(plan, now, balance)
            need = min_sz
        if need + 1e-12 < min_sz:
            leg.done = True
            return self._execute_next(plan, now, balance)

        # Duplicate-order prevention
        seq_key = f"{plan.plan_id}:{leg.slug}:{leg.side}"
        seq = self._order_seq.get(seq_key, 0)
        cok = client_order_key(plan.plan_id, leg.slug, leg.side, seq)
        if cok in plan.client_order_keys and (now - leg.last_submit_ms) < 5_000:
            return None
        self._order_seq[seq_key] = seq + 1
        cok = client_order_key(plan.plan_id, leg.slug, leg.side, seq)
        plan.client_order_keys.add(cok)

        token = meta.market.yes_token_id if leg.side == "UP" else meta.market.no_token_id
        ask = book.best_ask if book is not None else None
        if ask is None:
            return self._abort_plan(plan, now, f"no_ask:{leg.asset}")
        limit = min(leg.limit, min(1.0 - 0.01, round(ask + self.scfg.maximum_slippage, 4)))
        exec_px, avail = get_executable_price(book, need, limit)
        if exec_px is None or avail + 1e-9 < min(need, min_sz):
            record_event(plan, EVT_ORDER_CANCEL, now, reason="insufficient_depth",
                         slug=leg.slug, side=leg.side)
            # Retry later; do not force trade
            leg.last_submit_ms = now
            return None

        # Risk path: exposure + cash
        notional = need * limit
        if notional > self.scfg.max_order_usdc + 1e-9:
            return self._abort_plan(plan, now, f"max_order:{notional:.2f}")
        spendable = balance - self._reserved_cash(exclude_plan=plan.plan_id) - self.scfg.minimum_cash_reserve
        # Allow spending from this plan's reservation
        spendable += plan.remaining_notional()
        if notional > spendable + 1e-9:
            return self._abort_plan(plan, now, "cash_path")

        leg.last_submit_ms = now
        self.stats["orders_submitted"] += 1
        record_event(plan, EVT_ORDER_SUBMIT, now, slug=leg.slug, side=leg.side,
                     shares=need, limit=limit, client_key=cok)
        left = meta.market.seconds_remaining(now)
        ev = Evaluation(slug=meta.market.slug, asset=meta.market.asset, seconds_left=left, reason=None,
                        side=leg.side, token_id=token, limit_price=limit, expected_price=ask,
                        size=need, net_edge=plan.modeled_min_profit, confidence=1.0,
                        max_entry_price=limit)
        ev.features = {
            "cross_market_arb": {
                "cma_leg": True,
                "plan_id": plan.plan_id,
                "client_order_key": cok,
                "modeled_min_profit": plan.modeled_min_profit,
                "modeled_cost": plan.modeled_cost,
                "state": plan.state,
            }
        }
        self.last_status = {"mode": "executing", "plan": plan.plan_id, "leg": f"{leg.asset}:{leg.side}",
                            "need": need}
        return ev

    def _after_fill(self, plan: PortfolioPlan, now: float) -> None:
        slugs = [l.slug for l in plan.legs]
        actual = refresh_actuals(plan, slugs)
        pending = plan.pending_legs()
        if not pending:
            self._complete_or_incomplete(plan, now)
            return
        # If guarantee already destroyed beyond policy, stop claiming hedged.
        if actual["min_net_profit"] + 1e-12 < self.scfg.minimum_net_profit:
            # Still try to finish remaining legs only if modeled remaining can restore it;
            # otherwise mark incomplete under residual policy (never auto-dump).
            remaining_cost = plan.remaining_notional()
            # Optimistic: remaining equal-pair contribution cannot exceed remaining shares
            # counted at $1 — use reserved remainder as upper bound on added min payout.
            optimistic = actual["min_net_profit"] + remaining_cost * 0.0  # payout from unfinished unknown
            # Conservative: without assuming future fills help min payout beyond what equal
            # pairs add — recompute using only filled legs. If already below -max incomplete, abort.
            if actual["min_net_profit"] < -self.scfg.maximum_incomplete_exposure:
                self._abort_plan(plan, now, "guarantee_broken")

    def _complete_or_incomplete(self, plan: PortfolioPlan, now: float) -> Evaluation | None:
        slugs = [l.slug for l in plan.legs]
        actual = refresh_actuals(plan, slugs)
        ok = actual["min_net_profit"] > self.scfg.minimum_net_profit + 1e-12
        plan.reserved_cash = 0.0
        if ok and not plan.pending_legs():
            plan.complete = True
            plan.state = ArbState.COMPLETE.value
            self.state = ArbState.COMPLETE.value
            self.stats["portfolios_complete"] += 1
            record_event(plan, EVT_COMPLETE, now, **actual)
            log.info("cma COMPLETE plan=%s min_profit=%.4f cost=%.4f",
                     plan.plan_id, actual["min_net_profit"], actual["acquisition_cost"])
            self.active_plan_id = None
            self.state = ArbState.IDLE.value
        else:
            plan.incomplete = True
            plan.state = ArbState.INCOMPLETE.value
            self.state = ArbState.INCOMPLETE.value
            self.stats["portfolios_incomplete"] += 1
            record_event(plan, EVT_INCOMPLETE, now, policy=self.scfg.residual_exposure_policy, **actual)
            log.warning("cma INCOMPLETE plan=%s policy=%s min_profit=%s",
                        plan.plan_id, self.scfg.residual_exposure_policy, actual["min_net_profit"])
            self.active_plan_id = None
            self.state = ArbState.IDLE.value
        return None

    def _abort_plan(self, plan: PortfolioPlan, now: float, reason: str) -> Evaluation | None:
        plan.reject_reason = reason
        record_event(plan, EVT_ORDER_CANCEL, now, reason=reason)
        record_event(plan, EVT_RISK_REJECT, now, reason=reason)
        # Cancel remaining: mark unfinished legs done without buying more / selling.
        for leg in plan.legs:
            if not leg.done and leg.filled_shares <= 1e-9:
                leg.done = True
        filled = plan.position_legs()
        if filled:
            plan.incomplete = True
            plan.state = ArbState.INCOMPLETE.value
            self.stats["portfolios_incomplete"] += 1
            actual = refresh_actuals(plan, [l.slug for l in plan.legs])
            record_event(plan, EVT_INCOMPLETE, now, policy=self.scfg.residual_exposure_policy,
                         reason=reason, **actual)
        else:
            plan.state = ArbState.CANCELLED.value
            plan.complete = False
        plan.reserved_cash = 0.0
        self.active_plan_id = None
        self.state = ArbState.IDLE.value
        self.stats["risk_rejects"] += 1
        self.last_status = {"mode": "abort", "why": reason, "plan": plan.plan_id}
        log.warning("cma ABORT plan=%s reason=%s", plan.plan_id, reason)
        ev = Evaluation(slug=plan.legs[0].slug if plan.legs else "", asset="",
                        seconds_left=0.0, reason=Reason.RISK_LIMIT, detail=reason)
        return ev

    # ------------------------------------------------------------------ maker pair bids (pair_requests)

    @staticmethod
    def _maker_bid_price(bid: float, ask: float, improve_min_spread: float, tick: float = 0.01) -> float | None:
        """Post-only bid: join the bid, or improve one tick when the spread is wide enough."""
        if bid is None or ask is None or bid <= 0 or ask <= bid:
            return None
        spread = ask - bid
        px = bid + tick if spread + 1e-12 >= improve_min_spread else bid
        px = round(px, 2)
        if px >= ask - 1e-12:  # would cross -> taker
            px = round(bid, 2)
        if px <= 0 or px >= ask - 1e-12:
            return None
        return px

    def _maker_pair_prices(self, up_book, down_book) -> tuple[float, float] | None:
        sc = self.scfg
        if up_book is None or down_book is None:
            return None
        pu = self._maker_bid_price(up_book.best_bid, up_book.best_ask, sc.maker_improve_min_spread)
        pd = self._maker_bid_price(down_book.best_bid, down_book.best_ask, sc.maker_improve_min_spread)
        if pu is None or pd is None:
            return None
        return pu, pd

    def _maker_profit(self, pu: float, pd: float, shares: float) -> float:
        # Makers pay no taker fee on these markets; min payout = shares.
        return shares * (1.0 - pu - pd)

    def _best_maker_pair_diag(self) -> dict:
        sc = self.scfg
        shares = float(sc.maker_shares)
        need = sc.minimum_net_profit / max(shares, 1e-9)
        best = {"why": "no_books", "sum": None, "profit": None, "asset": None}
        best_profit = -1e18
        for slug, q in self.market_quotes.items():
            meta = q.get("meta")
            if meta is None or not sc.asset_ok(meta.market.asset):
                continue
            prices = self._maker_pair_prices(q.get("up"), q.get("down"))
            if prices is None:
                continue
            pu, pd = prices
            profit = self._maker_profit(pu, pd, shares)
            pair_sum = pu + pd
            max_sum = float(getattr(sc, "maximum_maker_sum", 0.97) or 0.97)
            ok = pair_sum <= max_sum + 1e-12 and profit + 1e-12 >= sc.minimum_net_profit
            if profit > best_profit:
                best_profit = profit
                best = {"why": "ok" if ok else "maker_sum_too_high",
                        "sum": round(pair_sum, 4), "profit": round(profit, 4),
                        "asset": meta.market.asset, "prices": (pu, pd)}
        if best["sum"] is None:
            best["why"] = "no_books"
        return best

    def _best_ask_sum(self) -> float | None:
        best = None
        for q in self.market_quotes.values():
            up, dn = q.get("up"), q.get("down")
            if up is None or dn is None or up.best_ask is None or dn.best_ask is None:
                continue
            s = float(up.best_ask) + float(dn.best_ask)
            best = s if best is None else min(best, s)
        return None if best is None else round(best, 4)

    def _taker_reject_why(self, result: OptimizeResult) -> str:
        if result.rejected:
            # Prefer substantive reasons over the single_sided audit noise.
            for r in result.rejected:
                if r.reason not in ("single_sided",):
                    return r.reason
            return result.rejected[0].reason
        ask = self._best_ask_sum()
        if ask is not None and ask >= 1.0:
            return "taker_ask_sum_ge_1"
        return "no_opportunity"

    def _maker_resting_notional(self) -> float:
        total = 0.0
        for rec in self.maker_markets.values():
            if rec.get("done"):
                continue
            prices = rec.get("prices") or {}
            filled = rec.get("filled") or {}
            target = float(rec.get("target") or 0)
            for side in ("UP", "DOWN"):
                need = max(0.0, target - float(filled.get(side, 0.0)))
                total += need * float(prices.get(side, 0.0))
        return total

    def pair_requests(self, now: float, markets: dict | None = None, books: dict | None = None,
                      balance: float | None = None) -> list[tuple[str, str, Order]]:
        """Rest GTC bids on both sides when maker prices lock guaranteed profit."""
        sc = self.scfg
        mode = (sc.entry_mode or "maker").strip().lower()
        if mode not in ("maker", "both"):
            return []
        if self.guard_paused or self.risk_halted or self._daily_halt or self._circuit_tripped(now):
            return []
        out: list[tuple[str, str, Order]] = []
        shares = float(sc.maker_shares)
        armed = sum(1 for r in self.maker_markets.values() if not r.get("done"))
        spendable = None if balance is None else max(
            0.0, float(balance) - self._maker_resting_notional() - self._reserved_cash()
            - sc.minimum_cash_reserve)

        for slug, m in (markets or {}).items():
            if m.asset.upper() not in set(sc.asset_list()):
                continue
            prev = self.maker_markets.get(slug)
            if prev is not None and not prev.get("done"):
                continue
            if prev is not None and prev.get("done"):
                # Allow re-arm after a clean cancel (no inventory).
                fu = float((prev.get("filled") or {}).get("UP", 0.0))
                fd = float((prev.get("filled") or {}).get("DOWN", 0.0))
                if fu > 1e-9 or fd > 1e-9:
                    continue
                self.maker_markets.pop(slug, None)
            left = m.seconds_remaining(now)
            if left < sc.min_time_remaining_s + sc.execution_buffer_s:
                self.reject_counts["maker_time"] = self.reject_counts.get("maker_time", 0) + 1
                continue
            if left > sc.max_time_remaining_s:
                continue
            if armed >= int(sc.max_maker_markets) or armed >= int(sc.maximum_concurrent_opportunities):
                self.reject_counts["maker_concurrent"] = self.reject_counts.get("maker_concurrent", 0) + 1
                break
            bk = (books or {}).get(slug) or (None, None)
            up_b, dn_b = bk if isinstance(bk, tuple) else (None, None)
            prices = self._maker_pair_prices(up_b, dn_b)
            if prices is None:
                self.reject_counts["maker_no_book"] = self.reject_counts.get("maker_no_book", 0) + 1
                continue
            pu, pd = prices
            # Spread gate on each side
            if up_b.best_ask - up_b.best_bid > sc.maximum_spread + 1e-9:
                continue
            if dn_b.best_ask - dn_b.best_bid > sc.maximum_spread + 1e-9:
                continue
            profit = self._maker_profit(pu, pd, shares)
            pair_sum = pu + pd
            max_sum = float(getattr(sc, "maximum_maker_sum", 0.97) or 0.97)
            if pair_sum > max_sum + 1e-12 or profit + 1e-12 < sc.minimum_net_profit:
                self.reject_counts["maker_sum_too_high"] = self.reject_counts.get("maker_sum_too_high", 0) + 1
                continue
            cost = shares * (pu + pd)
            if sc.maximum_market_exposure > 0 and cost > sc.maximum_market_exposure + 1e-9:
                self.reject_counts["maker_exposure"] = self.reject_counts.get("maker_exposure", 0) + 1
                continue
            if sc.maximum_capital_per_opportunity > 0 and cost > sc.maximum_capital_per_opportunity + 1e-9:
                continue
            if spendable is not None and cost > spendable + 1e-9:
                self.reject_counts["maker_cash"] = self.reject_counts.get("maker_cash", 0) + 1
                continue
            outstanding = self._maker_resting_notional() + cost
            if (sc.maximum_total_outstanding_notional > 0
                    and outstanding > sc.maximum_total_outstanding_notional + 1e-9):
                self.reject_counts["maker_outstanding"] = self.reject_counts.get("maker_outstanding", 0) + 1
                continue

            rec = {
                "slug": slug, "asset": m.asset, "state": ArbState.EXECUTING.value,
                "target": shares, "prices": {"UP": pu, "DOWN": pd},
                "filled": {"UP": 0.0, "DOWN": 0.0}, "cost": {"UP": 0.0, "DOWN": 0.0},
                "orders": {}, "modeled_profit": profit, "modeled_cost": cost,
                "t_armed": now, "done": False, "complete": False,
            }
            self.maker_markets[slug] = rec
            for side, tok, px in (("UP", m.yes_token_id, pu), ("DOWN", m.no_token_id, pd)):
                o = Order(
                    token_id=tok, side=Side.BUY, price=px, size=shares,
                    tif=TimeInForce.GTC, expires_ts=m.close_ts, expected_price=px,
                    post_only=True, fill_at_touch=self.fill_at_touch,
                    tag=f"{side} cma maker pair {px:.2f} x {shares:g} sum={pu + pd:.2f} "
                        f"lock={profit:+.3f}",
                )
                rec["orders"][side] = {"order_id": None, "price": px, "size": shares,
                                       "state": "sent", "t": now}
                out.append((slug, side, o))
            armed += 1
            if spendable is not None:
                spendable -= cost
            self.stats["opportunities_detected"] += 1
            self.stats["maker_pairs_armed"] += 1
            self.stats["orders_submitted"] += 2
            self.state = ArbState.EXECUTING.value
            log.info("cma MAKER_ARM %s %s bids UP<=%.2f DOWN<=%.2f x %.0f lock=%+.3f (sum=%.2f)",
                     m.asset, slug, pu, pd, shares, profit, pu + pd)
        return out

    def pair_cancels(self, now: float) -> list[str]:
        """Cancel resting maker pairs that are no longer valid or near expiry."""
        sc = self.scfg
        out: list[str] = []
        for slug, rec in list(self.maker_markets.items()):
            if rec.get("done"):
                continue
            # Resolve close time from market_quotes or order expiry via left time in quotes
            q = self.market_quotes.get(slug)
            left = None
            if q and q.get("meta") is not None:
                left = q["meta"].market.seconds_remaining(now)
            cancel = False
            reason = ""
            if left is not None and left < sc.maker_cancel_time_remaining_s:
                cancel, reason = True, "time"
            # If opportunity evaporated and neither side filled, pull both.
            # Use *resting* bid prices — fresh join prices moving to sum>=1 does not
            # invalidate a pair already locked at sum 0.97–0.98.
            filled_u = float((rec.get("filled") or {}).get("UP", 0.0))
            filled_d = float((rec.get("filled") or {}).get("DOWN", 0.0))
            if not cancel and filled_u <= 1e-9 and filled_d <= 1e-9:
                prices = rec.get("prices") or {}
                pu = float(prices.get("UP") or 0.0)
                pd = float(prices.get("DOWN") or 0.0)
                max_sum = float(getattr(sc, "maximum_maker_sum", 0.97) or 0.97)
                if pu > 0 and pd > 0:
                    profit = self._maker_profit(pu, pd, float(rec.get("target") or sc.maker_shares))
                    if pu + pd > max_sum + 1e-12 or profit + 1e-12 < sc.minimum_net_profit:
                        cancel, reason = True, "edge_gone"
                elif q is not None:
                    # No recorded prices yet — fall back to current join check.
                    fresh = self._maker_pair_prices(q.get("up"), q.get("down"))
                    if fresh is not None:
                        profit = self._maker_profit(
                            fresh[0], fresh[1], float(rec.get("target") or sc.maker_shares))
                        if (fresh[0] + fresh[1] > max_sum + 1e-12
                                or profit + 1e-12 < sc.minimum_net_profit):
                            cancel, reason = True, "edge_gone"
            if not cancel:
                continue
            for side, o in (rec.get("orders") or {}).items():
                if o.get("state") in ("resting", "sent") and o.get("order_id"):
                    out.append(o["order_id"])
                    o["state"] = "cancelling"
            if filled_u <= 1e-9 and filled_d <= 1e-9:
                rec["done"] = True
                rec["state"] = ArbState.CANCELLED.value
                self.reject_counts[f"maker_cancel_{reason}"] = self.reject_counts.get(f"maker_cancel_{reason}", 0) + 1
                log.info("cma MAKER_CANCEL %s reason=%s", slug, reason)
            else:
                # Partial inventory remains under residual policy.
                rec["done"] = True
                rec["state"] = ArbState.INCOMPLETE.value
                self.stats["maker_pairs_incomplete"] += 1
                log.warning("cma MAKER_INCOMPLETE %s reason=%s filled=%s policy=%s",
                            slug, reason, rec.get("filled"), sc.residual_exposure_policy)
        return out

    def on_pair_submit(self, order_id: str, slug: str, side: str, price: float, shares: float) -> None:
        rec = self.maker_markets.get(slug)
        if rec is None:
            rec = self.maker_markets.setdefault(slug, {
                "slug": slug, "asset": "", "state": ArbState.EXECUTING.value,
                "target": shares, "prices": {}, "filled": {"UP": 0.0, "DOWN": 0.0},
                "cost": {"UP": 0.0, "DOWN": 0.0}, "orders": {}, "modeled_profit": 0.0,
                "modeled_cost": 0.0, "t_armed": None, "done": False, "complete": False,
            })
        o = rec["orders"].setdefault(side, {"price": price, "size": shares, "t": None})
        o.update({"order_id": order_id, "state": "resting", "price": price, "size": shares})
        rec["prices"][side] = float(price)
        self._by_order[order_id] = (slug, side)

    def drop_pair(self, order_id: str) -> None:
        ref = self._by_order.pop(order_id, None)
        if ref is None:
            return
        slug, side = ref
        rec = self.maker_markets.get(slug)
        if rec is None:
            return
        o = (rec.get("orders") or {}).get(side)
        if o is not None:
            o["state"] = "done"

    def on_pair_fill_record(self, slug: str, order, fill, now: float) -> None:
        rec = self.maker_markets.get(slug)
        if rec is None:
            return
        side = "UP"
        for s, o in (rec.get("orders") or {}).items():
            if o.get("order_id") == getattr(order, "order_id", None):
                side = s
                break
        else:
            # Infer from token if needed
            q = self.market_quotes.get(slug)
            if q and q.get("meta") is not None:
                tok = getattr(order, "token_id", "")
                side = "UP" if tok == q["meta"].market.yes_token_id else "DOWN"
        sz = float(getattr(fill, "size", 0.0) or 0.0)
        px = float(getattr(fill, "price", 0.0) or 0.0)
        fee = float(getattr(fill, "fee", 0.0) or 0.0)
        rec["filled"][side] = float(rec["filled"].get(side, 0.0)) + sz
        rec["cost"][side] = float(rec["cost"].get(side, 0.0)) + px * sz + fee
        self.stats["orders_filled"] += 1
        log.info("cma MAKER_FILL %s %s size=%.4f px=%.4f filled=%s",
                 slug, side, sz, px, rec["filled"])
        self._maker_maybe_complete(slug, now)

    def on_pair_done(self, order_id: str, filled: bool, killed: bool = False) -> None:
        ref = self._by_order.get(order_id)
        if ref is None:
            return
        slug, side = ref
        rec = self.maker_markets.get(slug)
        if rec is None:
            return
        o = (rec.get("orders") or {}).get(side)
        if o is not None:
            o["state"] = "filled" if filled else ("cancelled" if killed else "done")
        self._maker_maybe_complete(slug, rec.get("t_armed") or 0.0)

    def _maker_maybe_complete(self, slug: str, now: float) -> None:
        rec = self.maker_markets.get(slug)
        if rec is None or rec.get("done"):
            return
        target = float(rec.get("target") or 0.0)
        fu = float(rec["filled"].get("UP", 0.0))
        fd = float(rec["filled"].get("DOWN", 0.0))
        paired = min(fu, fd)
        cost = float(rec["cost"].get("UP", 0.0)) + float(rec["cost"].get("DOWN", 0.0))
        # Both sides reached target: guaranteed profit = paired - cost_of_paired approx
        if fu + 1e-9 >= target and fd + 1e-9 >= target:
            profit = paired - cost
            rec["done"] = True
            rec["complete"] = profit > self.scfg.minimum_net_profit + 1e-12
            rec["state"] = ArbState.COMPLETE.value if rec["complete"] else ArbState.INCOMPLETE.value
            if rec["complete"]:
                self.stats["maker_pairs_complete"] += 1
                self.stats["portfolios_complete"] += 1
            else:
                self.stats["maker_pairs_incomplete"] += 1
                self.stats["portfolios_incomplete"] += 1
            log.info("cma MAKER_DONE %s paired=%.2f cost=%.4f profit=%+.4f state=%s",
                     slug, paired, cost, profit, rec["state"])
            self.state = ArbState.IDLE.value
            return
        # Both order states terminal with shortfall -> incomplete.
        # Paper/live may call on_pair_done(filled=True) before on_pair_fill_record;
        # wait until fill sizes catch up (next fill callback re-enters here).
        states = [(rec.get("orders") or {}).get(s, {}).get("state") for s in ("UP", "DOWN")]
        if all(s in ("filled", "cancelled", "done") for s in states):
            for s, st in zip(("UP", "DOWN"), states):
                if st == "filled" and float(rec["filled"].get(s, 0.0)) + 1e-9 < target:
                    return
            rec["done"] = True
            rec["complete"] = False
            rec["state"] = ArbState.INCOMPLETE.value
            self.stats["maker_pairs_incomplete"] += 1
            self.stats["portfolios_incomplete"] += 1
            log.warning("cma MAKER_INCOMPLETE %s filled=%s cost=%s policy=%s",
                        slug, rec["filled"], rec["cost"], self.scfg.residual_exposure_policy)
            self.state = ArbState.IDLE.value
