"""CROSS_MARKET_CORRELATION engine: pair-wise opposite-outcome scanner.

EXPERIMENTAL, PAPER ONLY. Behind ``TPB_CMC_ENABLED=1``; ``__main__.check_cross_market_correlation``
refuses shadow / micro-live / live. Independent data tag, cash reservations, and risk
caps — does not share capital with other bots.

Statistical mode is disabled by default. Guaranteed-arbitrage labels require the
formal worst-case payout test; historical correlation alone never qualifies.
"""
from __future__ import annotations

import logging
from typing import Any

from ...features.engine import FeatureEngine
from ...risk.limits import RiskManager
from ...signals.costs import FeeSchedule
from ...types import Order, Side, TimeInForce
from ..cross_market_arb.math import LegQuote, estimate_leg
from ..engine import EngineConfig, Evaluation, Reason, SideView
from ..momentum import get_executable_price
from ..spot_lag import SpotLagEngine
from .alignment import MarketSnapshot
from .config import ENABLE_ENV, STRATEGY_ID, CrossMarketCorrelationConfig
from .execution import (
    EVT_ARM, EVT_CANCEL, EVT_CIRCUIT, EVT_RISK, EVT_SCAN, EVT_SUBMIT, EVT_UNHEDGED,
    PairPlan, PairState, as_dict, client_order_key, mark_fill, plan_from_dict,
    plan_from_scan, record_event, refresh_actuals, sides_for_direction,
)
from .model import JointOutcomeModel
from .scanner import MarketView, ScanResult, scan_all

log = logging.getLogger("cross_market_correlation")


class CrossMarketCorrelationEngine(SpotLagEngine):
    """SpotLag surface for live.py; decisions come from the pair scanner."""

    feature_key = "cross_market_correlation"
    stacks_positions = True
    entry_retry_ms = 200.0
    budget_is_ceiling = True
    #: Strike/spot gates live in alignment.validate_market; allow evaluate to collect books first.
    requires_strike = False
    requires_market_open = True
    wants_books = True
    one_pair_per_market_side = False
    fill_at_touch = True

    def __init__(self, cfg: EngineConfig, scfg: CrossMarketCorrelationConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(
                f"the cross_market_correlation strategy is behind a feature flag: set {ENABLE_ENV}=1")
        if not isinstance(scfg, CrossMarketCorrelationConfig):
            raise TypeError("CrossMarketCorrelationEngine needs a CrossMarketCorrelationConfig")
        scfg.max_slippage = float(scfg.maximum_slippage)
        scfg.max_spread = float(scfg.maximum_spread)
        scfg.max_quote_age_ms = float(scfg.maximum_quote_age_ms)
        scfg.max_daily_loss_usdc = float(scfg.maximum_daily_realized_loss)
        scfg.assets = scfg.markets or scfg.assets
        super().__init__(cfg, scfg, risk, features)
        self.scfg: CrossMarketCorrelationConfig = scfg
        risk.cfg.max_concurrent_positions = max(
            risk.cfg.max_concurrent_positions, 2 * int(scfg.maximum_simultaneous_positions))
        self.market_quotes: dict[str, dict] = {}
        self.plans: dict[str, PairPlan] = {}
        self.active_plan_id: str | None = None
        self.state: str = PairState.IDLE.value
        self.last_status: dict[str, Any] = {}
        self.reject_counts: dict[str, int] = {}
        self.last_candidates: list[dict] = []
        self.stats = {
            "pairs_scanned": 0,
            "candidates_evaluated": 0,
            "guaranteed_found": 0,
            "statistical_found": 0,
            "rejected": 0,
            "orders_submitted": 0,
            "orders_filled": 0,
            "pairs_matched": 0,
            "pairs_unhedged": 0,
            "risk_rejects": 0,
            "circuit_trips": 0,
        }
        self._order_seq: dict[str, int] = {}
        self._daily_halt = False
        self.guard_paused = False
        self.risk_halted = False
        self._last_scan_log_ms = 0.0
        #: pair_key -> earliest ms we may arm again (stops arm/abort spin on RISK_LIMIT)
        self._arm_cooldown_until: dict[str, float] = {}
        #: (slug, side) -> [(recv_ms, mid), ...] for leader downtrend checks (~30s kept).
        self._mid_hist: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self.model = JointOutcomeModel(
            prior_strength=scfg.prior_strength,
            prior_agreement_bias=scfg.prior_agreement_bias,
            min_sample_size=scfg.min_sample_size,
        )
        if scfg.history_path:
            n = self.model.load_json(scfg.history_path)
            log.info("cmc loaded %d historical pair rows from %s", n, scfg.history_path)

    # ------------------------------------------------------------------ persistence

    def persist_state(self) -> dict:
        return {
            "cmc_plans": {k: {**as_dict(p), "client_order_keys": list(p.client_order_keys)}
                          for k, p in self.plans.items()},
            "cmc_active": self.active_plan_id,
            "cmc_state": self.state,
            "cmc_stats": dict(self.stats),
            "cmc_order_seq": dict(self._order_seq),
            "cmc_daily_halt": self._daily_halt,
            "cmc_model": self.model.summary(),
        }

    def restore_state(self, doc: dict | None, now: float) -> None:
        doc = doc or {}
        self.plans = {}
        for k, raw in (doc.get("cmc_plans") or {}).items():
            plan = plan_from_dict(raw)
            if plan.matched:
                plan.state = PairState.MATCHED.value
            elif plan.unhedged:
                plan.state = PairState.UNHEDGED.value
            elif plan.state in (
                PairState.LEG1_SUBMITTED.value, PairState.LEG2_SUBMITTED.value,
                PairState.LEG1_FILLED.value, PairState.ARMED.value,
            ):
                plan.state = PairState.UNHEDGED.value if plan.unhedged_shares() > 1e-9 else PairState.ARMED.value
                refresh_actuals(plan)
                record_event(plan, "RECONCILE", now, reason="process_restart")
            # Drop zero-fill terminal junk left by prior arm/abort loops.
            if plan.state in (
                PairState.CANCELLED.value, PairState.REJECTED.value,
            ) and plan.leg_a().filled_shares < 1e-9 and plan.leg_b().filled_shares < 1e-9:
                continue
            self.plans[str(k)] = plan
        self.active_plan_id = doc.get("cmc_active")
        self.state = str(doc.get("cmc_state") or PairState.IDLE.value)
        self.stats.update(doc.get("cmc_stats") or {})
        self._order_seq = {str(k): int(v) for k, v in (doc.get("cmc_order_seq") or {}).items()}
        self._daily_halt = bool(doc.get("cmc_daily_halt"))
        # Terminal / parked plans must not block scanning after a restart.
        ap = self._active_plan()
        if ap is not None and ap.state in (
            PairState.UNHEDGED.value, PairState.CANCELLED.value,
            PairState.MATCHED.value, PairState.COMPLETE.value, PairState.REJECTED.value,
        ):
            self.active_plan_id = None
            self.state = PairState.IDLE.value

    # ------------------------------------------------------------------ fills / settle

    def on_fill_detail(self, slug: str, side: str, price: float, size: float, fee: float,
                       now: float) -> None:
        plan = self._active_plan()
        if plan is None:
            return
        row = mark_fill(plan, slug, side, size, price, fee, now)
        if row is None:
            return
        self.stats["orders_filled"] += 1
        log.info("cmc FILL plan=%s %s %s size=%.4f px=%.4f unhedged=%.4f",
                 plan.plan_id, slug, side, size, price, plan.unhedged_shares())
        if plan.matched:
            self.stats["pairs_matched"] += 1
            self.active_plan_id = None
            self.state = PairState.IDLE.value
            # Stop immediately re-arming the same directional pair (was stacking
            # favorite+longshot legs across the epoch).
            pair_key = f"{plan.asset_a}|{plan.asset_b}|{plan.direction}|{plan.epoch}"
            cool = float(getattr(self.scfg, "pair_match_cooldown_ms", 30_000.0) or 0.0)
            if cool > 0:
                self._arm_cooldown_until[pair_key] = max(
                    self._arm_cooldown_until.get(pair_key, 0.0), now + cool)
        elif plan.unhedged:
            self._handle_unhedged(plan, now)

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        self.market_quotes.pop(slug, None)
        # Learn synchronized outcomes when both legs of a pair settle.
        for pid, plan in list(self.plans.items()):
            if slug not in (plan.slug_a, plan.slug_b):
                continue
            # Stash settlement bits on the plan via extras in events
            record_event(plan, "SETTLE_LEG", now, slug=slug, won_up=won_up, pnl=pnl)
            settles = [e for e in plan.events if e.get("event") == "SETTLE_LEG"]
            by_slug = {e["slug"]: bool(e.get("won_up")) for e in settles}
            if plan.slug_a in by_slug and plan.slug_b in by_slug:
                self.model.observe(plan.asset_a, plan.asset_b,
                                   by_slug[plan.slug_a], by_slug[plan.slug_b])
                self.plans.pop(pid, None)
                if self.active_plan_id == pid:
                    self.active_plan_id = None
                    self.state = PairState.IDLE.value

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap,
                 latency, balance, orderflow=None, quote_ages=None) -> Evaluation:
        m = meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        if not self.scfg.asset_ok(m.asset):
            ev.reason = Reason.ASSET_FILTERED
            return ev
        if self.scfg.kill_switch:
            self.state = PairState.RISK_HALT.value
            ev.reason = Reason.RISK_LIMIT
            ev.detail = "kill_switch"
            self.last_status = {"mode": "kill_switch", "paper": True, "live": False}
            return ev

        ages = quote_ages or {}
        strike = getattr(m, "strike", None)
        if strike is None or float(strike or 0) <= 0:
            strike = getattr(meta, "strike", None)
        spot_px = None
        if spot is not None:
            spot_px = float(getattr(spot, "price", spot) or 0) or None
        self.market_quotes[m.slug] = {
            "t": now, "meta": meta, "up": book_up, "down": book_down,
            "valid": True, "quote_ages": ages, "book_age_ms": book_age_ms,
            "spot": spot_px,
            "strike": float(strike) if strike is not None and float(strike) > 0 else None,
            "spot_age_ms": spot_age_ms,
        }

        if self._daily_halt or self._circuit_tripped(now):
            self.state = PairState.RISK_HALT.value
            self.last_status = {"mode": "circuit", "why": "daily_loss_limit"}
            ev.reason = Reason.RISK_LIMIT
            ev.detail = "daily circuit breaker"
            return ev

        plan = self._active_plan()
        if plan is not None and (
            not self.scfg.asset_ok(plan.asset_a) or not self.scfg.asset_ok(plan.asset_b)
        ):
            # Market-page skip list (or exclude_assets) changed under a working plan:
            # stop new legs; inventory already filled is held to settlement.
            if plan.state in (PairState.ARMED.value, PairState.LEG1_SUBMITTED.value,
                             PairState.LEG1_FILLED.value, PairState.LEG2_SUBMITTED.value):
                log.warning("cmc ABORT plan=%s reason=asset_skipped %s/%s",
                            plan.plan_id, plan.asset_a, plan.asset_b)
                self._abort_plan(plan, now, "asset_skipped")
            plan = None
        if plan is not None and plan.state in (
            PairState.ARMED.value, PairState.LEG1_SUBMITTED.value,
            PairState.LEG1_FILLED.value, PairState.LEG2_SUBMITTED.value,
            PairState.UNHEDGED.value,
        ):
            out = self._execute_next(plan, now, balance)
            if out is not None:
                return out
            # Re-read: cancel_alert releases the active slot; matched/cancelled end the plan.
            plan = self._active_plan()
            if plan is not None and (plan.matched or plan.state in (
                PairState.CANCELLED.value, PairState.UNHEDGED.value,
            )):
                plan = None

        if plan is not None and not plan.matched and plan.state not in (
            PairState.CANCELLED.value, PairState.UNHEDGED.value,
            PairState.MATCHED.value, PairState.COMPLETE.value, PairState.REJECTED.value,
        ):
            ev.reason = Reason.COOLDOWN
            ev.detail = f"plan {plan.plan_id} state={plan.state}"
            self.last_status = {"mode": "busy", "plan": plan.plan_id, "state": plan.state}
            return ev

        self.state = PairState.SCANNING.value
        result = self._scan(now, balance)
        self.stats["pairs_scanned"] += result.n_pairs
        self.stats["candidates_evaluated"] += result.evaluated
        self._publish_candidates(result)

        if result.best is None or result.best.best is None:
            why = self._scan_reject_why(result)
            self.reject_counts[why] = self.reject_counts.get(why, 0) + 1
            self.stats["rejected"] += 1
            self.state = PairState.IDLE.value
            self.last_status = {
                "mode": "scanning", "why": why,
                "statistical_mode": self.scfg.statistical_mode_enabled,
                "guaranteed_mode": self.scfg.guaranteed_mode_enabled,
                "rejects": dict(self.reject_counts),
                "n_pairs": result.n_pairs,
                "paper": True, "live": False,
            }
            if now - self._last_scan_log_ms > 30_000:
                self._last_scan_log_ms = now
                log.info("cmc SCAN idle why=%s pairs=%d statistical=%s",
                         why, result.n_pairs, self.scfg.statistical_mode_enabled)
            return self._reject(ev, Reason.EDGE_TOO_SMALL, why)

        best_row = result.best
        cand = best_row.best
        assert cand is not None
        if cand.label == "GUARANTEED_ARBITRAGE":
            self.stats["guaranteed_found"] += 1
        else:
            self.stats["statistical_found"] += 1

        side_a, side_b = sides_for_direction(cand.direction)
        views = {v.asset: v for v in self._collect_views(now) or []}
        va, vb = views.get(best_row.asset_a), views.get(best_row.asset_b)
        if va is None or vb is None:
            return self._reject(ev, Reason.EDGE_TOO_SMALL, "views_missing")

        # Limits from current executable estimates
        qa = va.up if side_a == "UP" else va.down
        qb = vb.up if side_b == "UP" else vb.down
        la = estimate_leg(qa, cand.shares, self.scfg.maximum_slippage)
        lb = estimate_leg(qb, cand.shares, self.scfg.maximum_slippage)
        if la is None or lb is None:
            return self._reject(ev, Reason.LOW_LIQUIDITY, "reprice_depth")

        epoch = int(m.open_ts // 1000) if m.open_ts else int(va.snapshot.epoch)
        plan = plan_from_scan(
            asset_a=best_row.asset_a, asset_b=best_row.asset_b,
            slug_a=best_row.slug_a, slug_b=best_row.slug_b,
            direction=cand.direction, label=cand.label,
            shares=cand.shares, limit_a=la.limit, limit_b=lb.limit,
            side_a=side_a, side_b=side_b,
            reserved=cand.total_cost, modeled_ev=cand.conservative_ev,
            modeled_worst=cand.worst_case_net, probs=cand.probs,
            epoch=epoch, now_ms=now,
        )
        record_event(plan, EVT_SCAN, now, label=cand.label, ev=cand.ev,
                     conservative_ev=cand.conservative_ev, worst=cand.worst_case_net)
        pair_key = f"{plan.asset_a}|{plan.asset_b}|{plan.direction}|{plan.epoch}"
        cool_until = self._arm_cooldown_until.get(pair_key, 0.0)
        if now < cool_until:
            self.state = PairState.IDLE.value
            return self._reject(ev, Reason.COOLDOWN, f"arm_cooldown:{pair_key}")

        ok, reason = self._preflight(plan, now, balance)
        if not ok:
            plan.state = PairState.REJECTED.value
            plan.reject_reason = reason
            record_event(plan, EVT_RISK, now, reason=reason)
            self.stats["risk_rejects"] += 1
            self.stats["rejected"] += 1
            if reason.startswith("risk_manager"):
                self._arm_cooldown_until[pair_key] = now + 5_000.0
            self.last_status = {"mode": "preflight_reject", "why": reason, "label": cand.label}
            self.state = PairState.IDLE.value
            ev.reason = Reason.RISK_LIMIT
            ev.detail = reason
            return ev

        record_event(plan, EVT_ARM, now, reserved=plan.reserved_cash, label=cand.label)
        plan.state = PairState.ARMED.value
        self.plans[plan.plan_id] = plan
        self.active_plan_id = plan.plan_id
        self.state = PairState.ARMED.value
        log.info("cmc ARMED plan=%s %s/%s %s label=%s cost=%.4f cons_ev=%.4f worst=%.4f",
                 plan.plan_id, plan.asset_a, plan.asset_b, plan.direction, plan.label,
                 plan.modeled_cost, plan.modeled_ev, plan.modeled_worst_case)
        out = self._execute_next(plan, now, balance)
        return out if out is not None else self._idle_eval(ev, "armed_waiting")

    def revalidate(self, ev: Evaluation, book, min_edge: float | None = None) -> Reason | None:
        info = (ev.features or {}).get("cross_market_correlation", {})
        if not info.get("cmc_leg"):
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
        info = (ev.features or {}).get("cross_market_correlation", {})
        return (f"{ev.side} cross_market_correlation plan={info.get('plan_id', '')} "
                f"label={info.get('label', '')} ev={info.get('modeled_ev', 0):+.3f}")

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = STRATEGY_ID
        snap["cross_market_correlation_config"] = self.scfg.as_dict()
        snap["cross_market_correlation"] = {
            "state": self.state,
            "active_plan_id": self.active_plan_id,
            "last_status": dict(self.last_status),
            "reject_counts": dict(self.reject_counts),
            "stats": dict(self.stats),
            "daily_halt": self._daily_halt,
            "kill_switch": bool(self.scfg.kill_switch),
            "paper_mode": True,
            "live_mode": False,
            "statistical_mode_enabled": bool(self.scfg.statistical_mode_enabled),
            "guaranteed_mode_enabled": bool(self.scfg.guaranteed_mode_enabled),
            "model": self.model.summary(),
            "candidates": list(self.last_candidates[:42]),
        }
        snap["cross_market_correlation_plans"] = {k: as_dict(p) for k, p in self.plans.items()}
        for k, v in self.reject_counts.items():
            self.rejections[str(k)] = int(v)
        return snap

    # ------------------------------------------------------------------ internals

    def _active_plan(self) -> PairPlan | None:
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
        loss = -min(0.0, float(getattr(self.risk, "realised_today", 0.0)))
        if self.scfg.maximum_daily_realized_loss > 0 and loss >= self.scfg.maximum_daily_realized_loss - 1e-9:
            if not self._daily_halt:
                self._daily_halt = True
                self.stats["circuit_trips"] += 1
                log.error("cmc DAILY_CIRCUIT_BREAKER loss=%.4f limit=%.4f",
                          loss, self.scfg.maximum_daily_realized_loss)
                plan = self._active_plan()
                if plan is not None:
                    record_event(plan, EVT_CIRCUIT, now, loss=loss)
            return True
        return False

    def _quote_for(self, slug: str, side: str, meta, book, ages: dict,
                   fallback_age_ms: float = 0.0) -> LegQuote | None:
        if book is None or book.best_ask is None or book.best_bid is None:
            return None
        token = meta.market.yes_token_id if side == "UP" else meta.market.no_token_id
        fee: FeeSchedule = getattr(meta, "fee", None) or FeeSchedule()
        levels = tuple((float(l.price), float(l.size)) for l in (book.asks or [])[:12])
        depth = sum(sz for px, sz in levels if px <= book.best_ask + self.scfg.maximum_slippage + 1e-9)
        age = float(ages.get(token, 0.0) or 0.0)
        if age <= 0.0:
            # Fall back to book timestamp age, then the evaluate-time book_age.
            book_ts = float(getattr(book, "ts", 0.0) or 0.0)
            age = fallback_age_ms if book_ts <= 0 else max(0.0, fallback_age_ms)
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

    @staticmethod
    def _quote_is_live(q: dict, now: float) -> bool:
        meta = q.get("meta")
        if meta is None:
            return False
        m = meta.market
        return float(m.open_ts or 0) <= now < float(m.close_ts or 0)

    def _collect_views(self, now: float) -> list[MarketView] | None:
        want = set(self.scfg.asset_list())
        by_asset: dict[str, dict] = {}
        for slug, q in self.market_quotes.items():
            meta = q.get("meta")
            if meta is None:
                continue
            asset = meta.market.asset.upper()
            if asset not in want or not self.scfg.asset_ok(asset):
                continue
            # Prefer the currently open window over the next/previous one so we
            # do not overwrite a live book with an empty next-window quote.
            prev = by_asset.get(asset)
            if prev is None:
                by_asset[asset] = q
                continue
            q_live, p_live = self._quote_is_live(q, now), self._quote_is_live(prev, now)
            if q_live and not p_live:
                by_asset[asset] = q
            elif q_live == p_live and float(q.get("t", 0)) >= float(prev.get("t", 0)):
                by_asset[asset] = q
        if not by_asset:
            return None
        views: list[MarketView] = []
        max_age = min(self.scfg.maximum_quote_age_ms, self.scfg.max_market_data_age_ms)
        for asset in self.scfg.asset_list():
            if not self.scfg.asset_ok(asset):
                continue
            q = by_asset.get(asset)
            if q is None:
                continue
            meta = q["meta"]
            m = meta.market
            if not self._quote_is_live(q, now):
                continue
            # Prefer live.py quote ages (stream-as-of). Do not overwrite them with
            # now-venue_ts: quiet books keep an old venue ts and falsely look stale,
            # which is the usual books_filtered:7_quotes death spiral.
            ages_map = dict(q.get("quote_ages") or {})
            book_age = float(q.get("book_age_ms") or 0.0)
            for tok, book in ((m.yes_token_id, q.get("up")), (m.no_token_id, q.get("down"))):
                if book is None:
                    continue
                existing = ages_map.get(tok)
                if existing is not None and float(existing) >= 0.0:
                    continue
                recv = float(getattr(book, "recv_ts", 0.0) or 0.0)
                ts = float(getattr(book, "ts", 0.0) or 0.0)
                ref = recv if recv > 0 else ts
                if ref > 0:
                    ages_map[tok] = max(0.0, now - ref)
                elif book_age > 0:
                    ages_map[tok] = book_age
            up = self._quote_for(m.slug, "UP", meta, q.get("up"), ages_map, book_age)
            dn = self._quote_for(m.slug, "DOWN", meta, q.get("down"), ages_map, book_age)
            if up is None or dn is None:
                continue
            fresh_up = up.quote_age_ms <= max_age
            fresh_dn = dn.quote_age_ms <= max_age
            if not (fresh_up and fresh_dn):
                continue
            if (up.ask - up.bid) > self.scfg.maximum_spread + 1e-9 or (dn.ask - dn.bid) > self.scfg.maximum_spread + 1e-9:
                continue
            if up.depth < self.scfg.minimum_book_depth or dn.depth < self.scfg.minimum_book_depth:
                continue
            # Prefer live market strike once StrikeTracker stamps it.
            strike = q.get("strike")
            if (strike is None or float(strike) <= 0) and float(getattr(m, "strike", 0) or 0) > 0:
                strike = float(m.strike)
            spot = q.get("spot")
            left = m.seconds_remaining(now)
            snap = MarketSnapshot(
                asset=asset, slug=m.slug,
                epoch=int(m.open_ts // 1000) if m.open_ts else 0,
                open_ts_ms=float(m.open_ts or 0),
                close_ts_ms=float(m.close_ts or (m.open_ts or 0) + 300_000),
                strike=float(strike) if strike is not None and float(strike) > 0 else None,
                spot=float(spot) if spot is not None and float(spot) > 0 else None,
                resolution_source=str(getattr(meta, "resolution_source", "") or ""),
                twap_window_s=float(getattr(meta, "twap_lookback_s", 0) or 0) or None,
                fee_ok=True,
                active=True, tradeable=bool(getattr(meta, "accepting_orders", True)),
                book_up_fresh=fresh_up, book_down_fresh=fresh_dn,
            )
            mid_up = 0.5 * (up.bid + up.ask)
            mid_dn = 0.5 * (dn.bid + dn.ask)
            self._note_mid(m.slug, "UP", now, mid_up)
            self._note_mid(m.slug, "DOWN", now, mid_dn)
            views.append(MarketView(
                asset=asset, slug=m.slug, snapshot=snap, up=up, down=dn,
                time_remaining_s=left, implied_p_up=mid_up,
                up_mids=tuple(self._mid_hist.get((m.slug, "UP"), ())),
                down_mids=tuple(self._mid_hist.get((m.slug, "DOWN"), ())),
                now_ms=now,
            ))
        return views or None

    def _note_mid(self, slug: str, side: str, now_ms: float, mid: float) -> None:
        key = (slug, side)
        hist = self._mid_hist.setdefault(key, [])
        if hist and abs(hist[-1][1] - mid) < 1e-6 and now_ms - hist[-1][0] < 200.0:
            hist[-1] = (now_ms, mid)
        else:
            hist.append((now_ms, mid))
        cutoff = now_ms - 45_000.0
        while len(hist) > 1 and hist[0][0] < cutoff:
            hist.pop(0)
        if len(hist) > 240:
            del hist[:-240]

    def _reserved_cash(self, exclude_plan: str | None = None) -> float:
        total = 0.0
        working = (
            PairState.ARMED.value, PairState.LEG1_SUBMITTED.value,
            PairState.LEG1_FILLED.value, PairState.LEG2_SUBMITTED.value,
        )
        for pid, plan in self.plans.items():
            if exclude_plan and pid == exclude_plan:
                continue
            if plan.matched or plan.state not in working:
                continue
            total += max(plan.reserved_cash, plan.remaining_notional())
        return total

    def _exposure_by_asset(self) -> dict[str, float]:
        """USDC at risk per asset — includes matched fills still held to settlement."""
        out: dict[str, float] = {}
        for plan in self.plans.values():
            if plan.state in (PairState.CANCELLED.value, PairState.REJECTED.value):
                # Keep residual filled cost if an abort left inventory.
                for leg in plan.legs:
                    if leg.filled_cost > 1e-9:
                        out[leg.asset] = out.get(leg.asset, 0.0) + leg.filled_cost
                continue
            for leg in plan.legs:
                if plan.matched or plan.state in (
                    PairState.MATCHED.value, PairState.COMPLETE.value, PairState.UNHEDGED.value,
                ):
                    out[leg.asset] = out.get(leg.asset, 0.0) + leg.filled_cost
                else:
                    out[leg.asset] = out.get(leg.asset, 0.0) + max(
                        leg.filled_cost,
                        (leg.target_shares - leg.filled_shares) * leg.limit + leg.filled_cost)
        return out

    def _filled_inventory(self) -> tuple[dict[tuple[str, str], float], dict[str, float]]:
        """Epoch inventory still held: (slug, side) → shares, and asset → net UP−DOWN shares."""
        inv: dict[tuple[str, str], float] = {}
        net: dict[str, float] = {}
        for plan in self.plans.values():
            for leg in plan.legs:
                if leg.filled_shares <= 1e-9:
                    continue
                key = (leg.slug, leg.side)
                inv[key] = inv.get(key, 0.0) + leg.filled_shares
                delta = leg.filled_shares if leg.side == "UP" else -leg.filled_shares
                net[leg.asset] = net.get(leg.asset, 0.0) + delta
        return inv, net

    def _scan(self, now: float, balance: float) -> ScanResult:
        views = self._collect_views(now)
        if not views:
            # Distinguish empty quote bag vs filtered-out books for the dashboard.
            n_quotes = len(self.market_quotes)
            why = "no_market_quotes" if n_quotes == 0 else f"books_filtered:{n_quotes}_quotes"
            res = ScanResult()
            res.rows = []  # keep empty; reject why comes from _scan_reject_why
            self.last_status = {**(self.last_status or {}), "collect": why, "n_quotes": n_quotes}
            return res
        parked = (
            PairState.IDLE.value, PairState.REJECTED.value,
            PairState.CANCELLED.value, PairState.COMPLETE.value,
            PairState.MATCHED.value, PairState.UNHEDGED.value,
        )
        active = sum(1 for p in self.plans.values() if not p.matched and p.state not in parked)
        if active >= self.scfg.maximum_simultaneous_positions:
            return ScanResult(n_pairs=0)
        inv, nets = self._filled_inventory()
        return scan_all(
            views, self.model, self.scfg, shares=self.scfg.size_shares,
            inventory=inv, net_by_asset=nets,
        )

    def _publish_candidates(self, result: ScanResult) -> None:
        rows = []
        for row in result.rows:
            for c in row.candidates or []:
                rows.append({
                    "asset_a": row.asset_a, "asset_b": row.asset_b,
                    "slug_a": row.slug_a, "slug_b": row.slug_b,
                    "direction": c.direction, "label": c.label,
                    "reject_reason": c.reject_reason or row.reject_reason,
                    "cost_per_pair": round(c.cost_per_pair, 4),
                    "total_cost": round(c.total_cost, 4),
                    "ev": round(c.ev, 4),
                    "conservative_ev": round(c.conservative_ev, 4),
                    "worst_case_net": round(c.worst_case_net, 4),
                    "q_ud": round(c.probs.q_ud, 4),
                    "q_du": round(c.probs.q_du, 4),
                    "agreement": round(c.probs.agreement_rate, 4),
                    "n": c.probs.n,
                    "uncertainty": round(c.probs.uncertainty, 4),
                    "fill_prob": round(c.fill_prob_est, 3),
                    "alignment": row.alignment.reason,
                })
        # Prefer non-rejected first
        rows.sort(key=lambda r: (0 if r["label"] != "REJECTED" else 1, -r["conservative_ev"]))
        self.last_candidates = rows[:42]

    def _scan_reject_why(self, result: ScanResult) -> str:
        if result.n_pairs == 0:
            return str((self.last_status or {}).get("collect") or "no_ready_pairs")
        reasons: dict[str, int] = {}
        for row in result.rows:
            why = row.reject_reason or (row.best.reject_reason if row.best else "no_best")
            # Keep "a:missing_strike" readable; collapse numeric tails only.
            key = why
            if ":" in why:
                head, tail = why.split(":", 1)
                key = head if tail.replace(".", "").isdigit() else why.replace(":", "_")
            reasons[key or "unknown"] = reasons.get(key or "unknown", 0) + 1
        if not reasons:
            return "no_edge"
        return max(reasons.items(), key=lambda kv: kv[1])[0]

    def _preflight(self, plan: PairPlan, now: float, balance: float) -> tuple[bool, str]:
        views = self._collect_views(now)
        if not views:
            return False, "books"
        by_slug = {v.slug: v for v in views}
        if plan.slug_a not in by_slug or plan.slug_b not in by_slug:
            return False, "missing_pair_books"
        outstanding = self._reserved_cash(exclude_plan=plan.plan_id) + plan.reserved_cash
        if (self.scfg.maximum_total_open_exposure > 0
                and outstanding > self.scfg.maximum_total_open_exposure + 1e-9):
            return False, f"total_exposure:{outstanding:.2f}"
        if plan.reserved_cash > self.scfg.maximum_capital_per_pair + 1e-9:
            return False, f"capital_per_pair:{plan.reserved_cash:.2f}"
        spendable = balance - self._reserved_cash(exclude_plan=plan.plan_id) - self.scfg.minimum_cash_reserve
        if plan.reserved_cash > spendable + 1e-9:
            return False, f"cash:{plan.reserved_cash:.2f}>{spendable:.2f}"
        exp = self._exposure_by_asset()
        for leg in plan.legs:
            projected = exp.get(leg.asset, 0.0) + leg.target_shares * leg.limit
            if projected > self.scfg.maximum_exposure_per_asset + 1e-9:
                return False, f"asset_exposure:{leg.asset}"
            if leg.target_shares * leg.limit > self.scfg.maximum_exposure_per_market + 1e-9:
                return False, f"market_exposure:{leg.slug}"
        # Re-price: allow limit bumps within the pair's combined-cost ceiling.
        units: list[float] = []
        for leg in plan.legs:
            v = by_slug[leg.slug]
            q = v.up if leg.side == "UP" else v.down
            planned = estimate_leg(q, leg.target_shares, self.scfg.maximum_slippage)
            if planned is None:
                return False, f"depth:{leg.asset}:{leg.side}"
            if planned.limit > leg.limit + 1e-9:
                leg.limit = planned.limit
            units.append(planned.unit_all_in)
        unit_sum = sum(units)
        if unit_sum > self.scfg.maximum_combined_cost + 1e-9:
            return False, f"reprice_cost:{unit_sum:.4f}"
        if unit_sum < self.scfg.minimum_combined_cost - 1e-9:
            return False, f"reprice_cost_low:{unit_sum:.4f}<{self.scfg.minimum_combined_cost}"
        repriced = sum(u * plan.legs[i].target_shares for i, u in enumerate(units))
        plan.reserved_cash = repriced
        plan.modeled_cost = repriced
        # Shared-market: do not double-count the same slug across plans
        used_slugs = set()
        terminal = (
            PairState.CANCELLED.value, PairState.REJECTED.value,
            PairState.MATCHED.value, PairState.COMPLETE.value,
            PairState.UNHEDGED.value,  # parked residual; slot released under cancel_alert
        )
        for pid, other in self.plans.items():
            if pid == plan.plan_id or other.matched:
                continue
            if other.state in terminal:
                continue
            used_slugs.update({other.slug_a, other.slug_b})
        if plan.slug_a in used_slugs or plan.slug_b in used_slugs:
            return False, "shared_market_overlap"
        # Fail closed against RiskManager before arming (avoids ARMED→ABORT spin).
        for leg in plan.legs:
            stake = max(0.0, leg.target_shares - leg.filled_shares) * leg.limit
            if stake <= 0:
                continue
            verdict = self.risk.check(leg.slug, leg.asset, plan.epoch, stake, add_to_open=True)
            if not verdict.ok:
                detail = getattr(verdict, "detail", "") or ""
                return False, f"risk_manager:{verdict.reason}:{detail}" if detail else f"risk_manager:{verdict.reason}"
        return True, "ok"

    def _execute_next(self, plan: PairPlan, now: float, balance: float) -> Evaluation | None:
        policy = (self.scfg.unhedged_policy or "cancel_alert").strip().lower()
        if plan.state == PairState.UNHEDGED.value:
            self._handle_unhedged(plan, now)
            # cancel_alert / hold: do not chase; hedge_if_edge may still work pending legs below.
            if policy != "hedge_if_edge" or not plan.pending_legs():
                return None
            plan.state = PairState.LEG2_SUBMITTED.value

        pending = plan.pending_legs()
        if not pending:
            if plan.unhedged_shares() > 1e-6:
                plan.state = PairState.UNHEDGED.value
                plan.unhedged = True
                self._handle_unhedged(plan, now)
            return None

        if self.scfg.revalidate_before_submit:
            ok, reason = self._preflight(plan, now, balance)
            if not ok and plan.leg_a().filled_shares < 1e-9 and plan.leg_b().filled_shares < 1e-9:
                return self._abort_plan(plan, now, reason)
            actual = refresh_actuals(plan)
            if (plan.unhedged_notional() > self.scfg.maximum_unhedged_exposure + 1e-9
                    and not pending):
                return self._abort_plan(plan, now, "unhedged_cap")
            # After one leg fills, do not chase if retries are exhausted and EV is gone.
            if plan.unhedged_shares() > 1e-6 and actual.get("ev", 0) < 0 and len(pending) == 1:
                rem = pending[0]
                if rem.retries >= self.scfg.max_leg_retries:
                    return self._abort_plan(plan, now, "edge_destroyed_after_leg1")

        # Prefer finishing the unhedged side first; else deterministic A then B
        pending.sort(key=lambda l: (0 if l.filled_shares > 0 else 1, 0 if l.role == "A" else 1))
        # Actually: if one filled, do the other first
        if plan.unhedged_shares() > 1e-6:
            a, b = plan.leg_a(), plan.leg_b()
            if a.filled_shares > b.filled_shares:
                pending = [x for x in pending if x.role == "B"] or pending
            elif b.filled_shares > a.filled_shares:
                pending = [x for x in pending if x.role == "A"] or pending

        leg = pending[0]
        if leg.last_submit_ms and (now - leg.last_submit_ms) < self.scfg.leg_retry_ms:
            return None
        if leg.retries >= self.scfg.max_leg_retries:
            return self._abort_plan(plan, now, f"retry_cap:{leg.asset}")
        if leg.last_submit_ms and (now - leg.last_submit_ms) > self.scfg.leg_timeout_ms and leg.retries > 0:
            # Timeout without fill — count as retry
            pass

        qrow = self.market_quotes.get(leg.slug)
        if qrow is None:
            return self._abort_plan(plan, now, f"missing_quote:{leg.slug}")
        meta = qrow["meta"]
        book = qrow["up"] if leg.side == "UP" else qrow["down"]
        need = max(0.0, leg.target_shares - leg.filled_shares)
        min_sz = float(getattr(meta.market, "min_size", 5.0) or 5.0)
        if 0 < need < min_sz:
            if need <= 0.05:
                leg.done = True
                return self._execute_next(plan, now, balance)
            need = min_sz
        if need + 1e-12 < min_sz:
            leg.done = True
            return self._execute_next(plan, now, balance)

        seq_key = f"{plan.plan_id}:{leg.slug}:{leg.side}"
        seq = self._order_seq.get(seq_key, 0)
        cok = client_order_key(plan.plan_id, leg.slug, leg.side, seq)
        if cok in plan.client_order_keys and (now - leg.last_submit_ms) < 5_000:
            return None
        self._order_seq[seq_key] = seq + 1
        cok = client_order_key(plan.plan_id, leg.slug, leg.side, seq)
        plan.client_order_keys.add(cok)

        ask = book.best_ask if book is not None else None
        if ask is None:
            return self._abort_plan(plan, now, f"no_ask:{leg.asset}")
        # Price moved: bump the armed limit within the pair cost ceiling, do not burn retries.
        if ask > leg.limit + 1e-9:
            bumped = min(1.0 - 0.01, round(ask + self.scfg.maximum_slippage, 4))
            other = plan.leg_b() if leg.role == "A" else plan.leg_a()
            if bumped + other.limit > self.scfg.maximum_combined_cost + 1e-9:
                leg.last_submit_ms = now
                record_event(plan, EVT_CANCEL, now, reason="price_moved", slug=leg.slug, ask=ask)
                return None
            leg.limit = bumped
            record_event(plan, EVT_CANCEL, now, reason="limit_bumped", slug=leg.slug, ask=ask, limit=bumped)

        limit = min(leg.limit, min(1.0 - 0.01, round(ask + self.scfg.maximum_slippage, 4)))
        exec_px, avail = get_executable_price(book, need, limit)
        if exec_px is None or avail + 1e-9 < min(need, min_sz):
            leg.last_submit_ms = now
            record_event(plan, EVT_CANCEL, now, reason="insufficient_depth", slug=leg.slug)
            return None

        notional = need * limit
        if notional > self.scfg.max_order_usdc + 1e-9 or notional > self.scfg.max_order_size * limit + 1e-9:
            return self._abort_plan(plan, now, f"max_order:{notional:.2f}")
        spendable = balance - self._reserved_cash(exclude_plan=plan.plan_id) - self.scfg.minimum_cash_reserve
        spendable += plan.remaining_notional()
        if notional > spendable + 1e-9:
            return self._abort_plan(plan, now, "cash_path")

        verdict = self.risk.check(leg.slug, leg.asset, plan.epoch, notional, add_to_open=True)
        if not verdict.ok:
            self.stats["risk_rejects"] += 1
            return self._abort_plan(plan, now, f"risk_manager:{verdict.reason}")

        leg.last_submit_ms = now
        leg.retries += 1
        self.stats["orders_submitted"] += 1
        if plan.unhedged_shares() < 1e-9 and leg.role == "A":
            plan.state = PairState.LEG1_SUBMITTED.value
        else:
            plan.state = PairState.LEG2_SUBMITTED.value
        token = meta.market.yes_token_id if leg.side == "UP" else meta.market.no_token_id
        bid = float(book.best_bid) if book is not None and book.best_bid is not None else max(0.01, ask - 0.02)
        record_event(plan, EVT_SUBMIT, now, slug=leg.slug, side=leg.side,
                     shares=need, limit=limit, client_key=cok)
        left = meta.market.seconds_remaining(now)
        p_model = 0.5 + 0.5 * float(plan.probs.get("agreement", 0.5))
        if leg.side == "DOWN":
            p_model = 1.0 - p_model
        ev = Evaluation(
            slug=meta.market.slug, asset=meta.market.asset, seconds_left=left, reason=None,
            side=leg.side, token_id=token, limit_price=limit, expected_price=ask,
            size=need, net_edge=plan.modeled_ev, confidence=max(0.0, 1.0 - float(plan.probs.get("uncertainty", 1))),
            max_entry_price=limit, p_used=p_model, p_market=ask,
        )
        ev.sides = [SideView(
            side=leg.side, token_id=token, p_model=p_model, p_market=ask,
            ask=ask, bid=bid, depth_at_limit=float(avail), costs=None, reason=None,
            entry_px=ask,
        )]
        ev.features = {
            "cross_market_correlation": {
                "cmc_leg": True,
                "plan_id": plan.plan_id,
                "client_order_key": cok,
                "label": plan.label,
                "direction": plan.direction,
                "modeled_ev": plan.modeled_ev,
                "modeled_worst_case": plan.modeled_worst_case,
                "modeled_cost": plan.modeled_cost,
                "state": plan.state,
                "role": leg.role,
            }
        }
        return ev

    def _abort_plan(self, plan: PairPlan, now: float, reason: str) -> Evaluation | None:
        plan.reject_reason = reason
        pair_key = f"{plan.asset_a}|{plan.asset_b}|{plan.direction}|{plan.epoch}"
        if "risk_manager" in reason or reason.startswith("retry_cap"):
            self._arm_cooldown_until[pair_key] = now + 5_000.0
        if plan.unhedged_shares() > 1e-6:
            plan.state = PairState.UNHEDGED.value
            plan.unhedged = True
            record_event(plan, EVT_UNHEDGED, now, reason=reason)
            self._handle_unhedged(plan, now)
        else:
            plan.state = PairState.CANCELLED.value
            record_event(plan, EVT_CANCEL, now, reason=reason)
            if self.active_plan_id == plan.plan_id:
                self.active_plan_id = None
            self.state = PairState.IDLE.value
            # Zero-fill cancels are noise — drop them so persist/scan stay lean.
            self.plans.pop(plan.plan_id, None)
        log.warning("cmc ABORT plan=%s reason=%s unhedged=%.4f",
                    plan.plan_id, reason, plan.unhedged_shares())
        return None

    def _handle_unhedged(self, plan: PairPlan, now: float) -> None:
        policy = (self.scfg.unhedged_policy or "cancel_alert").strip().lower()
        notion = plan.unhedged_notional()
        if not plan.unhedged_alerted:
            plan.unhedged_alerted = True
            self.stats["pairs_unhedged"] += 1
            log.error("cmc UNHEDGED plan=%s shares=%.4f notional=%.4f policy=%s — operator alert",
                      plan.plan_id, plan.unhedged_shares(), notion, policy)
            record_event(plan, EVT_UNHEDGED, now, policy=policy, notional=notion)
        if policy == "hold":
            return
        if policy == "hedge_if_edge":
            # Keep the plan active so pending opposite legs can still fill within armed limits.
            return
        # cancel_alert (default): stop new legs on this plan, keep inventory, free the slot
        # so the scanner can arm other pairs (otherwise one residual blocks forever).
        if self.active_plan_id == plan.plan_id:
            self.active_plan_id = None
        self.state = PairState.IDLE.value
        self.last_status = {
            "mode": "unhedged_alert",
            "plan": plan.plan_id,
            "unhedged_shares": plan.unhedged_shares(),
            "unhedged_notional": notion,
            "policy": policy,
        }
