"""The decision journal: why did we, or did we not, trade?

Every evaluation that got as far as pricing a side is a *signal*. A signal
either becomes an order or is rejected for one reason. This journal writes a
decision record for every signal transition (a market's reason changing, or
a heartbeat while it stays rejected inside the trade window), keeps session
counters, and logs each transition with an explicit label:

    SIGNAL_GENERATED              tradeable, an order follows
    SIGNAL_REJECTED_LOW_EDGE      net edge under the required edge
    SIGNAL_REJECTED_LOW_CONFIDENCE
    SIGNAL_REJECTED_SPREAD        spread over the cap
    SIGNAL_REJECTED_LIQUIDITY     depth at the limit under the minimum
    SIGNAL_REJECTED_STALE_QUOTE   spot moved, the token has not re-priced
    SIGNAL_REJECTED_RISK          a risk cap, daily loss or drawdown
    SIGNAL_REJECTED_POSITION_LIMIT already in this market / concurrent cap
    SIGNAL_REJECTED_SIZE          smallest valid size does not fit the caps
    SIGNAL_REJECTED_MARKET_STATE  outside the window, decided book, bad regime,
                                  price band, no offer, stale data
    SIGNAL_REJECTED_MODEL         model not ready or failed its sanity check
    SIGNAL_REJECTED_GUARD         the guard has stopped entries
    SIGNAL_REJECTED_THROTTLE      entry retry throttle / order in flight
    SIGNAL_REJECTED_REVALIDATION  the book moved between decision and send
    SIGNAL_REJECTED_KILL_SWITCH   data/KILL exists (checked in every mode)

Order events (ORDER_SUBMITTED, ORDER_ACCEPTED, ORDER_FILLED, ORDER_REJECTED,
ORDER_EXPIRED ...) are counted here too, fed by the live bot's result
handler, so the session summary reads end to end:

    signals detected 127 | rejected: low edge 61, liquidity 18, ...
    orders submitted 13, accepted 12, filled 5, expired 7
"""
from __future__ import annotations

import json
import logging
import math
from collections import Counter, deque
from pathlib import Path

#: how many decision points the chart keeps per market (the journal keeps all)
MARKS_PER_MARKET = 400
#: orders kept per market for the chart (a busy market sends a few dozen)
ORDER_MARKS_PER_MARKET = 60

log = logging.getLogger("decisions")

REASON_LABEL = {
    "EDGE_TOO_SMALL": "LOW_EDGE",
    "LOW_CONFIDENCE": "LOW_CONFIDENCE",
    "LOW_LIQUIDITY": "LIQUIDITY",
    "STALE_QUOTE": "STALE_QUOTE",
    "RISK_LIMIT": "RISK",
    "ALREADY_POSITIONED": "POSITION_LIMIT",
    "SIZE_ZERO": "SIZE",
    "OUTSIDE_TIME_WINDOW": "MARKET_STATE",
    "CANNOT_LAND_IN_TIME": "MARKET_STATE",
    "STALE_DATA": "STALE_DATA",
    "DATA_INCONSISTENT": "STALE_DATA",
    "NO_STRIKE": "MARKET_STATE",
    "MODEL_NOT_READY": "MODEL",
    "MODEL_SANITY": "MODEL",
    "BAD_REGIME": "MARKET_STATE",
    "NO_OFFER": "MARKET_STATE",
    "PRICE_BAND": "MARKET_STATE",
    "HIGH_SLIPPAGE": "REVALIDATION",
    "GUARD": "GUARD",
    "THROTTLE": "THROTTLE",
    "REVALIDATION": "REVALIDATION",
    "KILL_SWITCH": "KILL_SWITCH",
    # MOMENTUM_TAKER
    "WEAK_MOMENTUM": "WEAK_MOMENTUM",
    "MOMENTUM_REVERSING": "MOMENTUM_REVERSING",
    "TOKEN_ALREADY_MOVED": "TOKEN_ALREADY_MOVED",
    "COOLDOWN": "COOLDOWN",
    "MAX_TRADES": "MAX_TRADES",
    "MOMENTUM_EXHAUSTED": "MOMENTUM_EXHAUSTED",
    "VOLATILITY_SPIKE": "VOLATILITY_SPIKE",
    "ASSET_FILTERED": "MARKET_STATE",
    "ASK_ABOVE_MAX_ENTRY": "PRICE_CHASE",
    "CONFIRMATION": "CONFIRMATION",
}

#: reasons that mean "not an opportunity at all"; they are counted but only
#: written on a transition, never on the heartbeat
QUIET = {"OUTSIDE_TIME_WINDOW", "NO_STRIKE", "MODEL_NOT_READY", "CANNOT_LAND_IN_TIME", "WEAK_MOMENTUM",
         "ASSET_FILTERED"}

ORDER_EVENTS = ("ORDER_CREATED", "ORDER_SUBMITTED", "ORDER_ACCEPTED", "ORDER_PARTIALLY_FILLED",
                "ORDER_FILLED", "ORDER_CANCELLED", "ORDER_EXPIRED", "ORDER_REJECTED")


def label_for(reason: str | None) -> str:
    if reason is None:
        return "SIGNAL_GENERATED"
    return "SIGNAL_REJECTED_" + REASON_LABEL.get(reason, "OTHER")


def _f(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 5)


class DecisionJournal:
    def __init__(self, path: str | Path = "data/decisions.jsonl", heartbeat_s: float = 5.0,
                 log_transitions: bool = True) -> None:
        self.path = Path(path)
        self.heartbeat_ms = heartbeat_s * 1000.0
        self.log_transitions = log_transitions
        self.signals = 0
        self.rejected: Counter = Counter()          # label -> count (signals only)
        self.reasons: Counter = Counter()           # raw reason -> count
        self.orders: Counter = Counter()            # ORDER_* -> count
        self.last_reason: dict[str, str | None] = {}
        self.last_write_ms: dict[str, float] = {}
        self.records = 0
        self.write_errors = 0
        #: per market: the ask the decision looked at, when, and what was decided,
        #: for the dashboard to draw as coloured points on the price chart
        self.marks: dict[str, deque] = {}
        #: per market: every ORDER sent (entries and exits) -- when, side, limit, size
        #: and how it ended -- the dashboard's order points
        self.order_marks: dict[str, deque] = {}
        self._order_index: dict[str, dict] = {}     # order id -> its mark (until evicted)

    def marks_for(self, slug: str) -> list[dict]:
        return list(self.marks.get(slug, ()))

    def order_marks_for(self, slug: str) -> list[dict]:
        return [dict(m) for m in self.order_marks.get(slug, ())]

    def forget(self, slug: str) -> None:
        self.marks.pop(slug, None)
        for m in self.order_marks.pop(slug, ()):
            self._order_index.pop(m["id"], None)
        self.last_reason.pop(slug, None)
        self.last_write_ms.pop(slug, None)

    # --------------------------------------------------------------- signals

    def record(self, ev, *, now_ms: float, mode: str, risk_status: str = "", override: str | None = None,
               detail: str = "") -> dict | None:
        """One evaluation. Returns the record written, or None when nothing was."""
        reason = override or (None if ev.reason is None else ev.reason.value)
        is_signal = ev.reason is None or bool(ev.sides) or override is not None
        if is_signal:
            self.signals += 1
            self.reasons[reason or "TRADE"] += 1
            if reason is not None:
                self.rejected[REASON_LABEL.get(reason, "OTHER")] += 1
        key = ev.slug
        changed = self.last_reason.get(key, "__unset__") != reason
        due = now_ms - self.last_write_ms.get(key, 0.0) >= self.heartbeat_ms
        quiet = reason in QUIET
        if not (changed or (due and is_signal and not quiet)):
            return None
        self.last_reason[key] = reason
        self.last_write_ms[key] = now_ms
        row = self._row(ev, now_ms, mode, risk_status, reason, detail or ev.detail)
        self._write(row)
        if is_signal and row.get("market_price") is not None:
            self.marks.setdefault(key, deque(maxlen=MARKS_PER_MARKET)).append({
                "t": round(now_ms), "side": row["side"], "ask": row["market_price"],
                "decision": row["decision"], "net": row.get("estimated_net_edge"),
                "p": row.get("model_probability"), "detail": (row.get("detail") or "")[:80]})
        if self.log_transitions and changed and (is_signal or reason is None):
            side = row.get("side") or ""
            log.info("%-32s %-26s %-4s ask %s p_model %s p_mkt %s net_edge %s spread %s depth %s %5.0fs left  %s",
                     row["decision"], ev.slug, side, row.get("market_price"), row.get("model_probability"),
                     row.get("market_probability"), row.get("estimated_net_edge"), row.get("spread"),
                     row.get("liquidity"), ev.seconds_left, row.get("detail") or "")
        return row

    def _row(self, ev, now_ms, mode, risk_status, reason, detail) -> dict:
        # the side we would have traded, or the better-looking of the two
        side = None
        if ev.side:
            side = next((s for s in ev.sides if s.side == ev.side), None)
        elif ev.sides:
            priced = [s for s in ev.sides if s.costs is not None]
            side = max(priced, key=lambda s: s.costs.net_edge) if priced else ev.sides[0]
        c = side.costs if side is not None and side.costs is not None else None
        spread = None
        if side is not None and side.ask is not None and side.bid is not None:
            spread = side.ask - side.bid
        return {
            "timestamp": now_ms, "market": ev.slug, "asset": ev.asset,
            "token": side.token_id if side is not None else None,
            "side": side.side if side is not None else None,
            "market_price": _f(side.ask) if side is not None else None,
            "model_probability": _f(side.p_model) if side is not None else _f(ev.p_used),
            "market_probability": _f(side.p_market) if side is not None else _f(ev.p_market),
            "raw_edge": _f(c.gross_edge) if c else None,
            "estimated_net_edge": _f(c.net_edge) if c else None,
            "spread": _f(spread), "liquidity": _f(side.depth_at_limit) if side is not None else None,
            "expected_slippage": _f(c.slippage) if c else None,
            "time_remaining": _f(ev.seconds_left), "strategy_mode": mode, "risk_status": risk_status,
            "decision": label_for(reason), "rejection_reason": reason, "detail": detail,
            "size": _f(ev.size) if ev.size else None, "limit_price": _f(ev.limit_price) if ev.limit_price else None,
            "spot_move_bps": _f(ev.spot_move_bps), "market_reprice": _f(ev.market_reprice),
            "max_entry_price": _f(getattr(ev, "max_entry_price", None)),
            "vwap": _f(side.entry_px) if side is not None else None,
            "costs": c.as_dict() if c else None,
            # the momentum engine's full picture: state, windows, exhaustion,
            # and the execution economics (fair, costs, cap, VWAP, quote age)
            "momentum": (ev.features or {}).get("momentum") if isinstance(ev.features, dict) else None,
            "execution": (ev.features or {}).get("execution") if isinstance(ev.features, dict) else None,
        }

    # ---------------------------------------------------------------- orders

    def order_event(self, event: str, order_id: str, now_ms: float, **fields) -> None:
        if event not in ORDER_EVENTS:
            raise ValueError(f"unknown order event {event!r}")
        self.orders[event] += 1
        self._write({"timestamp": now_ms, "decision": event, "order_id": order_id,
                     **{k: v for k, v in fields.items() if v is not None}})
        self._mark_order(event, order_id, now_ms, fields)
        if self.log_transitions:
            log.info("%-32s %s %s", event, order_id[:12],
                     " ".join(f"{k}={v}" for k, v in fields.items() if v is not None))

    def _mark_order(self, event: str, order_id: str, now_ms: float, fields: dict) -> None:
        market = fields.get("market")
        if event == "ORDER_CREATED":
            if not market:
                return
            dq = self.order_marks.setdefault(market, deque(maxlen=ORDER_MARKS_PER_MARKET))
            if len(dq) == dq.maxlen:
                self._order_index.pop(dq[0]["id"], None)
            mark = {"id": order_id, "t": now_ms, "side": fields.get("side"), "kind": fields.get("kind") or "entry",
                    "limit": _f(fields.get("price")), "size": _f(fields.get("size")), "outcome": "sent"}
            dq.append(mark)
            self._order_index[order_id] = mark
            return
        mark = self._order_index.get(order_id)
        if mark is None:
            return
        if event in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
            mark.update(outcome="filled" if event == "ORDER_FILLED" else "partial", t_done=now_ms)
            if fields.get("price") is not None:
                mark["price"] = _f(fields.get("price"))
            if fields.get("size") is not None:
                mark["filled"] = _f(fields.get("size"))
        elif event in ("ORDER_REJECTED", "ORDER_EXPIRED", "ORDER_CANCELLED"):
            mark.update(outcome=event.split("_", 1)[1].lower(), t_done=now_ms, reason=fields.get("reason"))

    # --------------------------------------------------------------- summary

    def summary(self) -> dict:
        return {
            "signals": self.signals,
            "rejected": dict(self.rejected.most_common()),
            "reasons": dict(self.reasons.most_common()),
            "orders": {e: self.orders.get(e, 0) for e in ORDER_EVENTS},
            "records": self.records, "write_errors": self.write_errors, "path": str(self.path),
        }

    def report_text(self) -> str:
        rej = ", ".join(f"{k.lower().replace('_', ' ')} {v}" for k, v in self.rejected.most_common()) or "none"
        o = self.orders
        return (f"signals {self.signals} | rejected: {rej} | orders submitted {o.get('ORDER_SUBMITTED', 0)}, "
                f"accepted {o.get('ORDER_ACCEPTED', 0)}, filled {o.get('ORDER_FILLED', 0)}, "
                f"partial {o.get('ORDER_PARTIALLY_FILLED', 0)}, expired {o.get('ORDER_EXPIRED', 0)}, "
                f"cancelled {o.get('ORDER_CANCELLED', 0)}, rejected {o.get('ORDER_REJECTED', 0)}")

    def _write(self, row: dict) -> None:
        self.records += 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            self.write_errors += 1
