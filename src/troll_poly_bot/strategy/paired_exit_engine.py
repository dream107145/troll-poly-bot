"""The paired-exit strategy inside the triangle engine (``TPB_TRIANGLE_PX_ENABLED=1``).

TriangleEngine mixes this in. With ``px_enabled`` off nothing here runs (``manages_exits`` is False, every
hook returns early) and the triangle rule behaves as before. With it on:

  * the live bot feeds every 200 ms sample (``observe_point``): the features, the fair P(UP) and the signal
    are computed on the same points the archive stores (strategy/paired_exit.py)
  * ``px_evaluate`` replaces the triangle trigger with the paired-exit entry: signal -> EV gate after costs ->
    kill conditions -> exposure book
  * ``on_leg_fill`` arms each leg's own exit plan; ``exit_actions`` (called by the live bot every tick) returns
    the orders that plan needs: urgent FOK sells (STOP_LOSS / THESIS_INVALIDATED / UNPAIRED_TIMEOUT /
    TIME_EXIT), take-profit ladder asks and their cancels, the optional pair's cancel
  * ``on_exit_fill`` / ``on_exit_result`` / ``on_pair_done`` / ``on_market_settled`` keep the legs' books
  * ``persist_state`` / ``restore_state`` carry the legs and the exposure book across a restart
"""
from __future__ import annotations

import logging
import math
import os
import time

from ..types import Order, Side, TimeInForce
from . import paired_exit as px
from .engine import Evaluation, Reason, SideView

log = logging.getLogger(__name__)

EPS = 1e-6


def _hms(ms: float) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(ms / 1000.0))


class PairedExitMixin:
    """Paired-exit state and hooks; inert unless ``self.px`` is set (``_px_init``)."""

    px: px.PxParams | None = None

    # ------------------------------------------------------------------ setup

    def _px_init(self, scfg) -> None:
        self.px = None
        if not getattr(scfg, "px_enabled", False):
            return
        lv = tuple(float(x) for x in str(scfg.px_ladder_levels).split(",") if x.strip())
        fr = tuple(float(x) / (100.0 if float(x) > 1.0 else 1.0)
                   for x in str(scfg.px_ladder_size_percentages).split(",") if x.strip())
        self.px = px.PxParams(
            momentum_threshold=scfg.px_momentum_threshold, signal_source=str(scfg.px_signal_source or "crypto"),
            book_threshold=scfg.px_book_threshold, book_confirm=scfg.px_book_confirm,
            book_confirm_ms=scfg.px_book_confirm_ms, min_agree=scfg.px_min_agree,
            entry_min_edge=scfg.px_entry_min_edge, entry_min_confidence=scfg.px_entry_min_confidence,
            cost_slippage=scfg.px_cost_slippage, cost_adverse=scfg.px_cost_adverse, cost_latency=scfg.px_cost_latency,
            lo=scfg.px_min_price, hi=scfg.px_max_price, limit_offset=scfg.px_limit_offset, shares=scfg.px_shares,
            min_left_s=scfg.px_min_left_s, max_left_s=scfg.px_max_left_s, late_left_s=scfg.px_late_left_s,
            late_edge_add=scfg.px_late_edge_add, max_window_usdc=scfg.px_max_window_usdc,
            max_position_usdc=scfg.px_max_position_usdc, max_unpaired_usdc=scfg.px_max_unpaired_usdc,
            max_directional_usdc=scfg.px_max_directional_usdc, max_open_positions=int(scfg.px_max_open_positions),
            max_buys_per_window=int(scfg.px_max_buys_per_window), stop_loss=scfg.px_stop_loss,
            stop_cross=scfg.px_stop_cross, max_hold_s=scfg.px_max_hold_seconds, thesis_mode=scfg.px_thesis_mode,
            thesis_drop=scfg.px_thesis_drop, reversal_threshold=scfg.px_reversal_threshold,
            time_exit_s=scfg.px_time_exit_seconds, settle_keep=scfg.px_settle_keep, ladder_levels=lv,
            ladder_fracs=fr, ladder_mode=scfg.px_ladder_mode, ladder_fair_offset=scfg.px_ladder_fair_offset,
            ladder_reprice_s=scfg.px_ladder_reprice_s, pair_enabled=scfg.px_pair_enabled,
            pair_target=scfg.px_pair_target, max_daily_loss=scfg.px_max_daily_loss,
            max_consecutive_losses=int(scfg.px_max_consecutive_losses), loss_pause_s=scfg.px_loss_pause_s,
            max_data_age_ms=scfg.px_max_data_age_ms, max_signal_age_ms=scfg.px_max_signal_age_ms,
            max_execution_latency_ms=scfg.px_max_execution_latency_ms, max_spread=scfg.px_max_spread,
            max_disagreement=scfg.px_max_disagreement, max_settle_mismatches=int(scfg.px_max_settle_mismatches))
        if self.px.signal_source not in px.SIGNAL_SOURCES:
            raise ValueError(f"TPB_TRIANGLE_PX_SIGNAL_SOURCE={self.px.signal_source!r}: one of {px.SIGNAL_SOURCES}")
        self.px_models: dict[str, px.FairModel] = {}
        self.px_book_models: dict[str, px.BookModel | None] = {}
        self.px_book_recent: dict[str, list[tuple[float, float]]] = {}   # slug -> [(t, book score)] for "both"
        self.px_model_dir = scfg.px_model_dir
        self.px_chart_dir = getattr(scfg, "px_chart_dir", "data/charts")
        self.px_legs: dict[str, px.Leg] = {}
        self.px_exposure = px.ExposureBook()
        self.px_kill = px.KillMonitor()
        self.px_signals = px.Signals()
        self.px_buf: dict[str, px.PointBuffer] = {}
        self.px_asset_of: dict[str, str] = {}
        self.px_strike: dict[str, float] = {}
        self.px_sigma: dict[str, float] = {}             # asset -> the previous window's sigma
        self.px_sigma_from: dict[str, str] = {}
        self.px_last: dict[str, dict] = {}               # slug -> {t, F, p_up}
        self.px_pending: dict[str, dict] = {}            # slug -> the fresh signal awaiting evaluate
        self.px_orders: dict[str, str] = {}              # exit / ladder order id -> leg id
        self.px_pair_leg: dict[str, str] = {}            # pair order id -> leg id
        self._px_pair_for: str = ""                      # the leg whose pair order is being placed now
        self.px_seq = 0
        self.px_counts: dict[str, int] = {}
        self.px_closed: list[dict] = []                  # the last closed legs, for the dashboard
        log.info("[PX] PAIRED_EXIT_CONFIG %s", " ".join(f"{k}={getattr(self.px, k)}" for k in self.px.__slots__))

    @property
    def manages_exits(self) -> bool:
        """The live bot asks this engine for every exit (``exit_actions``) instead of its own TP / SL."""
        return self.px is not None

    def _px_count(self, code: str) -> None:
        self.px_counts[code] = self.px_counts.get(code, 0) + 1

    def _px_model(self, asset: str) -> px.FairModel | None:
        m = self.px_models.get(asset)
        if m is None:
            path = os.path.join(self.px_model_dir, f"fair_{asset.lower()}.json")
            try:
                m = px.FairModel.load(path)
            except (OSError, ValueError, KeyError) as exc:
                log.error("[PX] fair model for %s not loaded from %s (%s): no entries for it", asset, path, exc)
                return None
            self.px_models[asset] = m
            log.info("[PX] fair model %s loaded from %s (%s)", asset, path, m.meta)
        return m

    def _px_book_model(self, asset: str) -> px.BookModel | None:
        if asset in self.px_book_models:
            return self.px_book_models[asset]
        path = os.path.join(self.px_model_dir, f"book_{asset.lower()}.json")
        try:
            m = px.BookModel.load(path)
            log.info("[PX] book model %s loaded from %s (%s)", asset, path, m.meta)
        except (OSError, ValueError, KeyError) as exc:
            m = None
            log.error("[PX] book model for %s not loaded from %s (%s): no book signals for it", asset, path, exc)
        self.px_book_models[asset] = m
        return m

    # ------------------------------------------------------------------ samples

    def observe_point(self, slug: str, market, point: dict, now: float) -> None:
        """Every 200 ms sample of a live market (live._sample_prices): features, fair value, the signal."""
        if self.px is None:
            return
        buf = self.px_buf.get(slug)
        if buf is None:
            buf = self.px_buf[slug] = px.PointBuffer(market.open_ts, market.close_ts)
            self.px_asset_of[slug] = market.asset
        self.px_strike[slug] = market.strike
        first = not buf.points
        if not buf.add(point):
            return                              # (a sample before the open is not this window's)
        if first:
            # the window's first own sample: the previous window is complete now -- its sigma is the scale
            self._px_roll_sigma(market.asset, slug, market.open_ts)
        if market.asset not in self.px_sigma:
            self._px_sigma_from_archive(market.asset, market.open_ts, now)
        sigma = self.px_sigma.get(market.asset)
        if not sigma or not market.strike:
            return
        F = px.features_at(buf.points, market.strike, sigma, market.close_ts)
        model = self._px_model(market.asset)
        p_up = model.p_up(F) if model is not None else None
        src = self.px.signal_source
        score = None
        if src != "crypto":
            bm = self._px_book_model(market.asset)
            if bm is not None:
                score = bm.score(px.book_features_at(buf.points))
                if src == "both":
                    rec = self.px_book_recent.setdefault(slug, [])
                    rec.append((point["t"], score))
                    while rec and point["t"] - rec[0][0] > self.px.book_confirm_ms:
                        rec.pop(0)
        self.px_last[slug] = {"t": point["t"], "F": F, "p_up": p_up, "book": score}
        if p_up is None:
            return
        z1 = F.get("z1")
        pick = px.pick_signal(src, z1, score, self.px, self.px_book_recent.get(slug), point["t"])
        if pick is None:
            return
        d, trigger = pick
        if not (self.px.min_left_s <= F["left"] <= self.px.max_left_s):
            return
        if self.px_signals.take(slug, d, point["t"]):
            self.px_pending[slug] = {"t": point["t"], "dir": d, "z1": z1, "p_up": p_up, "F": F, "book": score,
                                     "trigger": trigger}

    def _px_sigma_from_archive(self, asset: str, open_ts: float, now: float) -> None:
        """After a start the previous window was not observed: read its archived points (data/charts, written at its
        settlement) -- the very samples the study's sigma comes from. Tried every 10 s until the file exists."""
        last = getattr(self, "_px_archive_try", {}).get(asset, 0.0)
        if now - last < 10_000.0:
            return
        self.__dict__.setdefault("_px_archive_try", {})[asset] = now
        path = os.path.join(self.px_chart_dir, f"{asset.lower()}-updown-5m-{int(open_ts // 1000) - 300}.json")
        try:
            import json
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            pts = sorted((p for p in doc.get("points") or [] if p.get("spot") and p.get("t")), key=lambda p: p["t"])
        except (OSError, ValueError):
            return
        sig = px.window_sigma(pts) if len(pts) >= 150 else None
        if sig:
            self.px_sigma[asset] = sig
            self.px_sigma_from[asset] = os.path.basename(path)
            log.info("[PX] %s sigma %.4f bp/sqrt(s) from the archived previous window %s (%d samples)", asset, sig,
                     os.path.basename(path), len(pts))

    def _px_roll_sigma(self, asset: str, slug: str, open_ts: float) -> None:
        """A new window of ``asset`` opened: the previous window's realised sigma becomes the scale (the replay
        uses the PREVIOUS window's sigma, so the first window after a start is not traded)."""
        prev = [s for s, b in self.px_buf.items() if s != slug and self.px_asset_of.get(s) == asset
                and b.close_ts <= open_ts + 1.0 and b.points]
        if prev:
            last = max(prev, key=lambda s: self.px_buf[s].close_ts)
            b = self.px_buf[last]
            if b.points[0]["t"] > b.open_ts + 5_000.0 or b.close_ts < open_ts - 1.0:
                # we joined that window late (a restart) or it is not the one right before: its points are not the
                # replay's -- wait for its archived file (_px_sigma_from_archive) instead
                self.px_sigma.pop(asset, None)
                log.info("[PX] %s: previous window %s seen only from %s: sigma from its archive once written", asset,
                         last, _hms(b.points[0]["t"]))
                return
            sig = px.window_sigma(b.points)
            if sig:
                self.px_sigma[asset] = sig
                self.px_sigma_from[asset] = last
                log.info("[PX] %s sigma %.4f bp/sqrt(s) from %s (%d samples)", asset, sig, last,
                         len(self.px_buf[last].points))
        for s in [s for s, b in self.px_buf.items() if b.close_ts < open_ts - 600_000.0]:
            self.px_buf.pop(s, None)
            self.px_last.pop(s, None)
            self.px_pending.pop(s, None)
            self.px_book_recent.pop(s, None)

    # ------------------------------------------------------------------ entry

    def px_evaluate(self, meta, book_up, book_down, base: Evaluation, now: float, spot_age_ms, book_age_ms,
                    latency, balance, quote_ages) -> Evaluation:
        prm = self.px
        m = meta.market
        sig = self.px_pending.get(m.slug)
        if sig is None:
            return self._reject(base, Reason.WEAK_MOMENTUM, "no paired-exit signal")
        if now - sig["t"] > prm.max_signal_age_ms:
            self.px_pending.pop(m.slug, None)
            self._px_count("STALE_DATA")
            log.info("[PX] STALE_DATA %s: signal %.0f ms old > %.0f", m.slug, now - sig["t"], prm.max_signal_age_ms)
            return self._reject(base, Reason.STALE_DATA, f"signal {now - sig['t']:.0f} ms old")
        self.px_pending.pop(m.slug, None)
        d = sig["dir"]
        side = "UP" if d > 0 else "DOWN"
        F = sig["F"]
        fair = sig["p_up"] if d > 0 else 1.0 - sig["p_up"]
        mark = self._px_mark(m.slug, sig)
        self.triangles += 1
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=base.seconds_left, reason=None)
        ev.p_analytic, ev.p_market = base.p_analytic, base.p_market
        ev.spot_move_bps, ev.market_reprice = base.spot_move_bps, base.market_reprice
        z1v = F.get("z1")
        info = {"signal": True, "direction": side, "side": side, "t": mark["t"], "trigger": "paired_exit",
                "source": sig.get("trigger", "crypto"),
                "z1": None if z1v is None or math.isnan(z1v) else round(z1v, 3),
                "book": None if sig.get("book") is None else round(sig["book"], 3),
                "agree": None if math.isnan(F.get("agree", float("nan"))) else round(F["agree"], 2),
                "fair": round(fair, 4), "left": round(F["left"], 1)}
        ev.features = {**(base.features or {}), "triangle": info}
        book = book_up if side == "UP" else book_down
        token = m.yes_token_id if side == "UP" else m.no_token_id
        if book is None or book.best_ask is None:
            return self._refuse(ev, Reason.NO_OFFER, f"no {side} ask", mark)
        ask, bid = book.best_ask, book.best_bid
        spread = None if bid is None else ask - bid
        p_age = now - (self.px_last.get(m.slug) or {}).get("t", 0.0)
        qa = None if not quote_ages else quote_ages.get(token)
        lat = latency.round_trip_ms() if latency is not None and hasattr(latency, "round_trip_ms") else None
        ok, code, why = self.px_kill.check(prm, now, unpaired=self.px_exposure.unpaired_usdc(self.px_legs),
                                           spot_age_ms=spot_age_ms, point_age_ms=p_age,
                                           book_age_ms=qa if qa is not None else book_age_ms, latency_ms=lat,
                                           spread=spread, disp=F.get("disp"))
        if not ok:
            self.px_kill.trip(code)
            self._px_count(code)
            log.info("[PX] %s %s %s: %s", code, m.slug, side, why)
            reason = {"STALE_DATA": Reason.STALE_DATA, "STALE_BOOK": Reason.STALE_DATA,
                      "ABNORMAL_SPREAD": Reason.LOW_LIQUIDITY}.get(code, Reason.RISK_LIMIT)
            return self._refuse(ev, reason, f"{code}: {why}", mark)
        if not (prm.lo <= ask <= prm.hi):
            return self._refuse(ev, Reason.PRICE_BAND, f"{side} ask {ask:.2f} outside {prm.lo:.2f}-{prm.hi:.2f}", mark)
        ag = F.get("agree")
        if prm.min_agree > -1.0 and (ag is None or math.isnan(ag) or ag * d < prm.min_agree):
            return self._refuse(ev, Reason.WEAK_MOMENTUM, f"exchanges agree {ag} < {prm.min_agree:+.2f}", mark)
        fee = meta.fee.taker_fee_per_share(ask)
        edge = fair - ask - fee - prm.entry_cost
        need = prm.entry_min_edge + (prm.late_edge_add if F["left"] < prm.late_left_s else 0.0)
        info.update({"ask": ask, "bid": bid, "fee": round(fee, 4), "edge": round(edge, 4), "need": round(need, 4)})
        if edge < need or fair < prm.entry_min_confidence:
            self._px_count("EDGE_TOO_SMALL")
            return self._refuse(ev, Reason.EDGE_TOO_SMALL,
                                f"EV {edge:+.3f} (fair {fair:.3f} - ask {ask:.2f} - fee {fee:.3f} - costs "
                                f"{prm.entry_cost:.3f}) < {need:+.3f}", mark)
        tick = m.tick_size or 0.01
        limit = min(round(ask + prm.limit_offset, 4), 1.0 - tick)
        size = float(prm.shares)
        ok, why = self.px_exposure.check(self.px_legs, prm, m.slug, side, size * limit)
        if not ok:
            self._px_count("EXPOSURE_LIMIT")
            log.info("[PX] EXPOSURE_LIMIT %s %s: %s", m.slug, side, why)
            return self._refuse(ev, Reason.EXPOSURE_CAP, f"EXPOSURE_LIMIT: {why}", mark)
        verdict = self.risk.check(m.slug, m.asset, int(m.open_ts // 1000), size * limit, add_to_open=True)
        if not verdict.ok:
            return self._refuse(ev, Reason(verdict.reason), verdict.detail, mark)
        if size * limit > balance + 1e-9:
            return self._refuse(ev, Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * limit:.2f}", mark)
        info.update({"limit": limit, "size": size})
        ev.features["execution"] = {"fair": round(fair, 4), "cap": round(limit, 4), "best_ask": ask, "best_bid": bid,
                                    "spread": None if spread is None else round(spread, 4),
                                    "quote_age_ms": None if qa is None else round(qa),
                                    "spot_age_ms": None if spot_age_ms is None else round(spot_age_ms)}
        depth = sum(lvl.size for lvl in book.asks if lvl.price <= limit + 1e-9)
        ev.sides = [SideView(side=side, token_id=token, p_model=fair, p_market=ask, ask=ask, bid=bid,
                             depth_at_limit=depth, costs=None, reason=None, entry_px=ask)]
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, limit, ask
        ev.max_entry_price = limit
        ev.p_used = sig["p_up"]
        ev.size, ev.net_edge, ev.confidence = size, edge, 1.0
        ev.size_note = f"{size:g} sh paired-exit leg ({size * limit:.2f} USDC worst case)"
        self.orders_decided += 1
        self.last[m.slug] = ev
        self._px_count("ENTRY_SIGNAL")
        log.info("[PX] ENTRY_SIGNAL %s %s [%s] z1 %s book %s agree %s fair %.3f ask %.2f fee %.3f costs %.3f EV %+.3f "
                 ">= %+.3f left %.0fs -> FOK %g sh at <= %.2f", m.slug, side, info["source"], info["z1"], info["book"],
                 info["agree"], fair, ask, fee, prm.entry_cost, edge, need, F["left"], size, limit)
        return ev

    def _px_mark(self, slug: str, sig: dict) -> dict:
        from collections import deque
        dq = self.shock_marks.setdefault(slug, deque(maxlen=self.SHOCK_MARKS_PER_MARKET))
        z1 = sig.get("z1")
        bk = sig.get("book")
        mk = {"t": round(sig["t"]), "dir": sig["dir"], "z1": None if z1 is None or math.isnan(z1) else round(z1, 3),
              "z3": None, "r1_bps": None, "trigger": "paired_exit", "source": sig.get("trigger", "crypto"),
              "book": None if bk is None else round(bk, 3), "ok": True, "why": ""}
        dq.append(mk)
        return mk

    def on_entry_submit(self, slug: str, now: float) -> None:
        super().on_entry_submit(slug, now)           # spot-lag's own per-market / hourly counts
        if self.px is not None:
            self.px_exposure.window(slug).buys += 1

    # ------------------------------------------------------------------ legs

    def on_leg_fill(self, order_id: str, market, side: str, token_id: str, shares: float, avg_px: float,
                    fees: float, now: float) -> None:
        """An entry filled: the leg and its own exit plan exist from now."""
        if self.px is None or shares <= EPS:
            return
        self.px_seq += 1
        lid = f"leg-{self.px_seq:05d}"
        last = self.px_last.get(market.slug) or {}
        p_up = last.get("p_up")
        fair = None if p_up is None else (p_up if side == "UP" else 1.0 - p_up)
        leg = px.Leg(leg_id=lid, slug=market.slug, asset=market.asset, side=side, token_id=token_id, shares=shares,
                     fill_px=avg_px, t_fill=now, close_ts=market.close_ts,
                     fair_entry=float("nan") if fair is None else fair, remaining=shares)
        leg.realised = -fees
        self.px_legs[lid] = leg
        self.px_exposure.window(market.slug).spent += shares * avg_px
        self._px_pair_for = lid
        prm = self.px
        plan = []
        if prm.stop_loss > 0:
            plan.append(f"stop bid <= {avg_px - prm.stop_loss:.2f}")
        if prm.thesis_mode:
            plan.append(f"thesis {prm.thesis_mode} (fair < {avg_px - prm.thesis_drop:.2f}"
                        + (f" / spot z1 against >= {prm.reversal_threshold:g} with agreement" if prm.thesis_mode in ("mom", "both") else "") + ")")
        if prm.max_hold_s > 0:
            plan.append(f"max hold {prm.max_hold_s:.0f} s (until {_hms(now + prm.max_hold_s * 1000)})")
        if prm.time_exit_s > 0:
            plan.append(f"time exit at {prm.time_exit_s:.0f} s left unless bid >= {prm.settle_keep:.2f}")
        if prm.ladder_levels:
            plan.append("ladder " + ", ".join(f"{p:.2f}" for p in px.ladder_prices(leg, prm, fair)))
        if prm.pair_enabled:
            plan.append(f"optional pair < {prm.pair_target:.2f}")
        self._px_count("ENTRY")
        log.info("[PX] ENTRY %s %s %s %.2f sh @ %.3f (fair %s, %.2f USDC); exit plan: %s", lid, market.slug, side, shares,
                 avg_px, "-" if fair is None else f"{fair:.3f}", shares * avg_px, "; ".join(plan) or "settle (no exits)")

    def exit_actions(self, market, book_up, book_down, now: float, held: dict[str, float],
                     order_states: dict[str, str]) -> list[px.ExitAction]:
        """What every open leg of this market needs now. ``held``: token -> shares the venue position holds;
        ``order_states``: our working exit / ladder order id -> state ("RESTING", "IN_FLIGHT", ... or "GONE")."""
        if self.px is None:
            return []
        prm = self.px
        out: list[px.ExitAction] = []
        legs = [lg for lg in self.px_legs.values() if lg.slug == market.slug and lg.state in ("OPEN", "EXITING")]
        if not legs:
            return out
        last = self.px_last.get(market.slug) or {}
        fresh = last and now - last.get("t", 0.0) <= 1500.0
        p_up = last.get("p_up") if fresh else None
        F = last.get("F") or {}
        left = max((market.close_ts - now) / 1000.0, 0.0)
        min_size = float(market.min_size or 5.0)
        for leg in legs:
            book = book_up if leg.side == "UP" else book_down
            bid = None if book is None else book.best_bid
            fair = None if p_up is None else (p_up if leg.side == "UP" else 1.0 - p_up)
            z1 = F.get("z1") if fresh else None
            agree = F.get("agree") if fresh else None
            # orders the venue no longer works
            for o in list(leg.ladder):
                if (o.get("order_id") and not o.get("filled")
                        and order_states.get(o["order_id"], "GONE") in ("GONE", "CANCELLED", "EXPIRED", "REJECTED")):
                    o["order_id"], o["cancelling"] = "", False
            if leg.exit_order and order_states.get(leg.exit_order, "GONE") in ("GONE", "CANCELLED"):
                leg.exit_order = ""
            if leg.state == "OPEN":
                trig = px.plan_exit(leg, prm, now, bid, fair, z1, agree, left)
                if trig is not None:
                    kind, detail = trig
                    leg.state, leg.exit_kind = "EXITING", kind
                    self._px_count(kind)
                    log.warning("[PX] %s %s %s %s: %s -> selling %.2f sh", kind, leg.leg_id, leg.slug, leg.side, detail,
                                leg.remaining)
                    for o in leg.ladder:
                        if o.get("order_id") and not o.get("filled") and not o.get("cancelling"):
                            o["cancelling"] = True
                            out.append(px.ExitAction("LADDER_CANCEL", leg.leg_id, leg.token_id, leg.side,
                                                     order_id=o["order_id"], detail=kind))
                    if leg.pair_order:
                        out.append(px.ExitAction("PAIR_CANCEL", leg.leg_id, leg.token_id, leg.side,
                                                 order_id=leg.pair_order, detail=kind))
                        self._px_count("PAIR_ORDER_CANCELLED")
                        log.info("[PX] PAIR_ORDER_CANCELLED %s %s: the leg exits (%s)", leg.leg_id, leg.pair_order, kind)
                    leg.next_try_ms = now + (250.0 if any(o.get("order_id") and not o.get("filled") for o in leg.ladder)
                                             else 0.0)
                else:
                    out.extend(self._px_ladder(leg, prm, now, bid, fair, min_size))
                    continue
            # EXITING: one urgent FOK sell at a time, retried at the new bid after a kill
            if leg.exit_order or now < leg.next_try_ms:
                continue
            resting = sum(o["shares"] for o in leg.ladder if o.get("order_id") and not o.get("filled"))
            if resting > EPS:
                # a ladder cancel not confirmed yet: those shares may still sell; wait for the venue
                leg.next_try_ms = now + 250.0
                continue
            avail = min(leg.remaining, held.get(leg.token_id, leg.remaining))
            if avail + EPS < min_size:
                if leg.exit_tries == 0:
                    log.warning("[PX] %s %s: %.2f sh < the venue minimum %.0f: cannot be sold, settles", leg.leg_id,
                                leg.slug, avail, min_size)
                leg.exit_tries += 1
                leg.next_try_ms = now + 5000.0
                continue
            if bid is None or bid <= 0.0:
                leg.next_try_ms = now + 500.0
                continue
            tick = market.tick_size or 0.01
            price = max(tick, round(bid - prm.stop_cross, 2))
            out.append(px.ExitAction(leg.exit_kind, leg.leg_id, leg.token_id, leg.side, shares=round(avail, 2),
                                     price=price, tif="FOK", detail=f"bid {bid:.2f}"))
            leg.exit_tries += 1
        return out

    def _px_ladder(self, leg: px.Leg, prm: px.PxParams, now: float, bid, fair, min_size: float) -> list[px.ExitAction]:
        """Take-profit ladder of an OPEN leg: place missing asks, re-price stale ones (maker), or sell a level at
        the bid once reached (taker)."""
        if not prm.ladder_levels:
            return []
        out = []
        if not leg.ladder:
            alloc = px.ladder_allocation(leg.shares, prm.ladder_fracs or tuple(1.0 / len(prm.ladder_levels)
                                                                                 for _ in prm.ladder_levels), min_size)
            prices = px.ladder_prices(leg, prm, fair)
            leg.ladder = [{"price": p, "shares": s, "order_id": "", "filled": False, "placed_ms": 0.0}
                          for p, s in zip(prices, alloc)]
        prices = px.ladder_prices(leg, prm, fair)
        for j, o in enumerate(leg.ladder):
            if o.get("filled"):
                continue
            want = prices[min(j, len(prices) - 1)]
            sh = min(o["shares"], leg.remaining)
            if sh + EPS < min_size:
                continue
            if prm.ladder_mode == "taker":
                if not leg.exit_order and bid is not None and bid >= want - 1e-9:
                    out.append(px.ExitAction("TAKE_PROFIT", leg.leg_id, leg.token_id, leg.side, shares=sh,
                                             price=round(want - 0.01, 2), tif="FOK", detail=f"level {want:.2f}"))
                    o["pending"] = True
                    leg.exit_order = "pending"
                    break
                continue
            if not o.get("order_id"):
                out.append(px.ExitAction("LADDER_PLACE", leg.leg_id, leg.token_id, leg.side, shares=sh, price=want,
                                         tif="GTC", detail=f"level {j + 1}"))
                o["price"], o["placed_ms"], o["order_id"], o["cancelling"] = want, now, "pending", False
            elif (not o.get("cancelling") and o["order_id"] != "pending" and abs(o["price"] - want) >= 0.01 - 1e-9
                  and now - o.get("placed_ms", 0.0) >= prm.ladder_reprice_s * 1000.0):
                # re-price: cancel first; the replacement is placed only once the venue confirms the cancel (the
                # order's state leaves RESTING), so two asks for the same shares never rest together
                out.append(px.ExitAction("LADDER_CANCEL", leg.leg_id, leg.token_id, leg.side, order_id=o["order_id"],
                                         detail=f"re-price {o['price']:.2f} -> {want:.2f}"))
                o["cancelling"] = True
        return out

    def on_exit_order(self, leg_id: str, kind: str, order_id: str) -> None:
        """The live bot sent an exit / ladder order for a leg (the id it got)."""
        leg = self.px_legs.get(leg_id)
        if leg is None:
            return
        self.px_orders[order_id] = leg_id
        if kind == "LADDER_PLACE":
            for o in leg.ladder:
                if o.get("order_id") == "pending":
                    o["order_id"] = order_id
                    break
        else:
            leg.exit_order = order_id

    def on_exit_fill(self, order_id: str, shares: float, avg_px: float, fee: float, now: float) -> str:
        """An exit / ladder sell filled. Returns the exit kind for the ledger."""
        lid = self.px_orders.pop(order_id, None)
        leg = self.px_legs.get(lid or "")
        if leg is None:
            return "EXIT"
        leg.remaining = max(0.0, leg.remaining - shares)
        leg.realised += shares * avg_px - fee
        kind = leg.exit_kind or "TAKE_PROFIT"
        hit = next((o for o in leg.ladder if o.get("order_id") == order_id), None)
        if hit is not None:
            hit["filled"] = True
            kind = "TAKE_PROFIT"
        elif leg.exit_order == order_id:
            leg.exit_order = ""
            if leg.state == "OPEN":
                kind = "TAKE_PROFIT"                 # a taker ladder level
                for o in leg.ladder:
                    if o.get("pending"):
                        o["filled"], o["pending"] = True, False
                        break
        self._px_count(kind if kind != "TAKE_PROFIT" or hit is None else "TAKE_PROFIT")
        log.info("[PX] %s %s %s %s sold %.2f sh @ %.3f (fill %.3f): %.2f sh left", kind, leg.leg_id, leg.slug, leg.side,
                 shares, avg_px, leg.fill_px, leg.remaining)
        if leg.remaining <= EPS:
            self._px_close(leg, kind, now)
        return kind

    def on_exit_result(self, order_id: str, reason: str, now: float) -> None:
        """An exit / ladder order ended unfilled (killed FOK, cancelled or expired ask)."""
        lid = self.px_orders.pop(order_id, None)
        leg = self.px_legs.get(lid or "")
        if leg is None:
            return
        if leg.exit_order == order_id:
            leg.exit_order = ""
            for o in leg.ladder:
                o.pop("pending", None)
            if leg.state == "EXITING":
                log.info("[PX] %s %s exit sell not filled (%s), try %d: re-sending at the new bid", leg.leg_id, leg.slug,
                         reason, leg.exit_tries)
        for o in leg.ladder:
            if o.get("order_id") == order_id and not o.get("filled"):
                o["order_id"] = ""

    def _px_close(self, leg: px.Leg, kind: str, now: float, settle_value: float | None = None) -> None:
        if leg.state == "CLOSED":
            return
        if settle_value is not None and leg.remaining > EPS:
            leg.realised += leg.remaining * settle_value
            leg.remaining = 0.0
        pnl = leg.realised - leg.shares * leg.fill_px
        leg.state, leg.exit_kind = "CLOSED", kind
        self.px_kill.on_result(pnl, now, self.px)
        self.px_closed.append({"leg": leg.leg_id, "slug": leg.slug, "side": leg.side, "shares": leg.shares,
                               "fill": leg.fill_px, "kind": kind, "pnl": round(pnl, 4),
                               "held_s": round((now - leg.t_fill) / 1000.0, 1)})
        self.px_closed = self.px_closed[-50:]
        log.info("[PX] LEG_CLOSED %s %s %s %s: pnl %+.2f after %.0f s", leg.leg_id, leg.slug, leg.side, kind, pnl,
                 (now - leg.t_fill) / 1000.0)

    # ------------------------------------------------------------------ the optional pair

    def px_pair_order(self, market, side: str, shares: float, avg_px: float) -> tuple[str, Order] | None:
        prm = self.px
        lid = self._px_pair_for
        self._px_pair_for = ""
        if not prm.pair_enabled or not lid:
            return None
        tick = market.tick_size or 0.01
        pp = round(math.floor((prm.pair_target - avg_px - 1e-9) / tick) * tick, 4)
        if pp < tick - 1e-9 or (market.min_size and shares + 1e-9 < float(market.min_size)):
            return None
        other = "DOWN" if side == "UP" else "UP"
        token = market.no_token_id if side == "UP" else market.yes_token_id
        leg = self.px_legs.get(lid)
        if leg is not None:
            leg.pair_price = pp
        self._px_pair_pending = lid
        self._px_count("PAIR_ORDER")
        log.info("[PX] PAIR_ORDER %s %s: optional resting buy %s %.2f sh at %.2f (pair %.3f < %.2f); the leg keeps its "
                 "own exits", lid, market.slug, other, shares, pp, avg_px + pp, prm.pair_target)
        return other, Order(token_id=token, side=Side.BUY, price=pp, size=round(shares, 2), tif=TimeInForce.GTC,
                            expires_ts=market.close_ts, expected_price=pp,
                            tag=f"{other} optional pair for {lid}: {avg_px:.3f} + {pp:.2f}")

    def px_on_pair_submit(self, order_id: str) -> None:
        lid = getattr(self, "_px_pair_pending", "")
        self._px_pair_pending = ""
        leg = self.px_legs.get(lid)
        if leg is not None:
            leg.pair_order = order_id
            self.px_pair_leg[order_id] = lid

    def px_on_pair_done(self, order_id: str, filled: bool, fill_px: float | None, shares: float | None,
                        token_id: str | None, now: float) -> None:
        lid = self.px_pair_leg.pop(order_id, None)
        leg = self.px_legs.get(lid or "")
        if leg is None:
            return
        leg.pair_order = ""
        if not filled:
            self._px_count("PAIR_ORDER_CANCELLED")
            log.info("[PX] PAIR_ORDER_CANCELLED %s %s: ended unfilled (the leg's own exits run)", lid, leg.slug)
            return
        q = shares or leg.remaining
        if leg.state == "OPEN" and q + EPS >= leg.remaining:
            leg.state = "PAIRED"
            self._px_count("PAIR_ORDER_FILLED")
            log.info("[PX] PAIR_ORDER_FILLED %s %s: %.2f sh locked at %.3f + %.2f: held to settlement", lid, leg.slug,
                     leg.remaining, leg.fill_px, fill_px or leg.pair_price)
            return
        # the pair filled after the leg began exiting (a cancel race) or for more shares than are left:
        # those shares are a NEW naked position of the other side -- it gets its own exit plan at once
        other = "DOWN" if leg.side == "UP" else "UP"
        self._px_count("PAIR_LATE_FILL")
        log.warning("[PX] PAIR_ORDER_FILLED late for %s %s (%s): %.2f %s sh @ %.2f become a leg of their own", lid,
                    leg.slug, leg.state, q, other, fill_px or leg.pair_price)
        self.px_seq += 1
        nl = px.Leg(leg_id=f"leg-{self.px_seq:05d}", slug=leg.slug, asset=leg.asset, side=other,
                    token_id=token_id or "", shares=q, fill_px=fill_px or leg.pair_price, t_fill=now,
                    close_ts=leg.close_ts, remaining=q)
        nl.state, nl.exit_kind = "EXITING", "UNPAIRED_TIMEOUT"
        self.px_legs[nl.leg_id] = nl

    # ------------------------------------------------------------------ settlement / state

    def on_market_settled(self, slug: str, won_up: bool, pnl: float, now: float) -> None:
        parent = getattr(super(), "on_market_settled", None)
        if callable(parent):
            parent(slug, won_up, pnl, now)
        if self.px is None:
            return
        for leg in [lg for lg in self.px_legs.values() if lg.slug == slug and lg.state != "CLOSED"]:
            won = (leg.side == "UP") == bool(won_up)
            if leg.state == "PAIRED":
                # the locked pair: this leg's shares pay 1 with the pair's (the pair leg is booked by the venue)
                leg.realised += leg.remaining * 1.0 - leg.remaining * (leg.pair_price or 0.0)
                leg.remaining = 0.0
                self._px_close(leg, "PAIR_SETTLED", now)
            else:
                self._px_close(leg, "SETTLE_WIN" if won else "SETTLE_LOSS", now, 1.0 if won else 0.0)
        buf = self.px_buf.get(slug)
        strike = self.px_strike.get(slug) or 0.0
        proxy = None
        if buf is not None and buf.points and strike > 0:
            last = buf.points[-1].get("spot")
            if last and abs(math.log(last / strike)) * 1e4 >= 5.0:
                proxy = last > strike
        self.px_kill.on_settlement(bool(won_up) if won_up is not None else None, proxy, self.px, slug)
        cutoff = now - 3600_000.0
        for lid in [k for k, lg in self.px_legs.items() if lg.state == "CLOSED" and lg.close_ts < cutoff]:
            self.px_legs.pop(lid, None)
        self.px_exposure.forget(now - 600_000.0, lambda s: (self.px_buf.get(s).close_ts if self.px_buf.get(s) else None))

    def px_persist(self) -> dict | None:
        if self.px is None:
            return None
        return {"legs": [lg.as_dict() for lg in self.px_legs.values() if lg.state != "CLOSED"],
                "windows": {s: {"spent": w.spent, "buys": w.buys} for s, w in self.px_exposure.windows.items()},
                "sigma": dict(self.px_sigma), "seq": self.px_seq,
                "kill": {"realised_day": {str(k): v for k, v in self.px_kill.realised_day.items()},
                         "streak": self.px_kill.streak, "paused_until": self.px_kill.paused_until}}

    def px_restore(self, doc: dict | None, now: float) -> None:
        if self.px is None or not doc:
            return
        for d in doc.get("legs") or []:
            try:
                leg = px.Leg.from_dict(d)
            except (KeyError, TypeError):
                continue
            if leg.close_ts <= now:
                continue
            leg.exit_order = ""
            leg.pair_order = ""
            for o in leg.ladder:
                o["order_id"] = ""                 # the paper venue forgot its resting asks: placed again
            self.px_legs[leg.leg_id] = leg
            log.warning("[PX] restored %s %s %s %.2f sh @ %.3f (%s)", leg.leg_id, leg.slug, leg.side, leg.remaining,
                        leg.fill_px, leg.state)
        for s, w in (doc.get("windows") or {}).items():
            wb = self.px_exposure.window(s)
            wb.spent, wb.buys = float(w.get("spent", 0.0)), int(w.get("buys", 0))
        self.px_sigma.update({k: float(v) for k, v in (doc.get("sigma") or {}).items()})
        self.px_seq = max(self.px_seq, int(doc.get("seq") or 0))
        k = doc.get("kill") or {}
        self.px_kill.realised_day = {int(a): float(b) for a, b in (k.get("realised_day") or {}).items()}
        self.px_kill.streak = int(k.get("streak") or 0)
        self.px_kill.paused_until = float(k.get("paused_until") or 0.0)

    def px_snapshot(self) -> dict | None:
        if self.px is None:
            return None
        legs = [lg for lg in self.px_legs.values() if lg.state != "CLOSED"]
        return {"params": {k: getattr(self.px, k) for k in self.px.__slots__},
                "open_legs": [{"leg": lg.leg_id, "slug": lg.slug, "side": lg.side, "shares": lg.remaining,
                               "fill": lg.fill_px, "state": lg.state, "exit": lg.exit_kind,
                               "held_s": None} for lg in legs],
                "unpaired_usdc": round(self.px_exposure.unpaired_usdc(self.px_legs), 4),
                "sigma": dict(self.px_sigma), "counts": dict(self.px_counts),
                "book_score": {s: round(v["book"], 3) for s, v in self.px_last.items() if v.get("book") is not None},
                "kill": {"halted": self.px_kill.halted, "streak": self.px_kill.streak,
                         "paused_until": self.px_kill.paused_until, "trips": dict(self.px_kill.trips),
                         "realised_today": round(self.px_kill.realised_day.get(int(time.time() // 86400), 0.0), 4)},
                "closed": list(self.px_closed[-20:])}
