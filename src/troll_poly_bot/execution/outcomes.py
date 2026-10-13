"""What happened AFTER: post-fill markouts and the fate of every signal.

Two questions a fill rate cannot answer:

1. **Adverse selection.** Are we filled right before the market moves against
   us? ``MarkoutTracker`` samples the token's book at fixed offsets after
   every fill (100 ms ... 30 s) and records the move of the mid against the
   fill price, signed so that a NEGATIVE markout is a move against us. A
   strategy whose fills are consistently followed by negative markouts is
   being picked off, whatever its fill rate.

2. **Missed opportunities.** For every signal that reached pricing -- traded
   or not -- ``SignalOutcomeTracker`` records what the strategy saw and then
   what the venue did: whether the price was still there when an order
   would have arrived (the hypothetical arrival, decision + the measured
   order latency), and what the position would have been worth at the
   holding horizon. A skipped signal whose price was gone at arrival was
   UNFILLABLE (skipping it cost nothing); one that would have lost is a
   GOOD_MISS; one that would have made money is a BAD_MISS. That separates
   "we refused a good trade" from "we refused a trade nobody could get",
   which is the difference between a gate to loosen and a gate to keep.

Both write append-only JSONL (one line per fill / per signal, when its last
sample is in) and never raise into the trading loop. Samples are taken on
the bot's own local book: the times recorded are the real sample times, so
a late sample is visible as such, never silently relabelled.
"""
from __future__ import annotations

import heapq
import itertools
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("outcomes")

#: ms after a fill at which the book is sampled
MARKOUT_OFFSETS_MS: tuple[float, ...] = (100.0, 250.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0, 30000.0)
#: seconds after a signal at which its hypothetical position is valued
SIGNAL_HORIZONS_S: tuple[float, ...] = (5.0, 10.0, 20.0, 45.0)

#: sampler(token_id) -> (best_bid, best_ask, ask depth under ``cap`` or None)
Sampler = Callable[[str, float | None], tuple[float | None, float | None, float | None] | None]


def _mid(bid, ask):
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return bid if bid is not None else ask


def _r(v, nd=5):
    return None if v is None else round(float(v), nd)


class _Timed:
    """A heap of (due_ms, seq, payload) with an O(1) 'anything due?' check."""

    def __init__(self) -> None:
        self._heap: list = []
        self._seq = itertools.count()

    def push(self, due: float, payload) -> None:
        heapq.heappush(self._heap, (due, next(self._seq), payload))

    @property
    def next_due(self) -> float:
        return self._heap[0][0] if self._heap else float("inf")

    def pop_due(self, now: float):
        while self._heap and self._heap[0][0] <= now:
            yield heapq.heappop(self._heap)[2]

    def __len__(self) -> int:
        return len(self._heap)


class _Writer:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.write_errors = 0
        self.written = 0

    def write(self, row: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
            self.written += 1
        except OSError:
            self.write_errors += 1


# ───────────────────────────── fill markouts ─────────────────────────────


@dataclass
class _FillRec:
    row: dict
    remaining: int
    samples: dict = field(default_factory=dict)


class MarkoutTracker:
    def __init__(self, path: str | Path, offsets_ms: tuple[float, ...] = MARKOUT_OFFSETS_MS,
                 max_pending: int = 2000) -> None:
        self.out = _Writer(path)
        self.offsets = tuple(offsets_ms)
        self.max_pending = max_pending
        self._timed = _Timed()
        self._open: dict[str, _FillRec] = {}
        self.completed: list[dict] = []           # newest last, bounded

    @property
    def next_due(self) -> float:
        return self._timed.next_due

    def on_fill(self, *, order_id: str, token_id: str, side: str, price: float, size: float, now_ms: float,
                bid: float | None, ask: float | None, **context) -> None:
        """``side`` is BUY or SELL (what WE did). ``context`` rides along:
        asset, slug, kind (entry / take_profit / ...), mode, signal_id, the
        prices at signal / send / ack, spot at fill."""
        if len(self._open) >= self.max_pending or order_id in self._open:
            return
        key = order_id
        row = {"order_id": order_id, "token_id": token_id, "side": side, "price": _r(price), "size": _r(size, 4),
               "fill_ts": now_ms, "bid_at_fill": _r(bid), "ask_at_fill": _r(ask), "mid_at_fill": _r(_mid(bid, ask)),
               **{k: v for k, v in context.items() if v is not None}}
        self._open[key] = _FillRec(row=row, remaining=len(self.offsets))
        for off in self.offsets:
            self._timed.push(now_ms + off, (key, off))

    def attach(self, order_id: str, **fields) -> None:
        """The order's fate, learned after its markout clock started (a paper
        fill is provisional until the quote-survival check; a live result
        reaches the loop a tick after the POST returned)."""
        rec = self._open.get(order_id)
        clean = {k: v for k, v in fields.items() if v is not None}
        if rec is not None:
            rec.row.update(clean)
        elif clean:
            self.out.write({"event": "markout_amend", "order_id": order_id, **clean})

    def due(self, now_ms: float, sampler: Sampler) -> None:
        if now_ms < self._timed.next_due:
            return
        for key, off in self._timed.pop_due(now_ms):
            rec = self._open.get(key)
            if rec is None:
                continue
            q = None
            try:
                q = sampler(rec.row["token_id"], None)
            except Exception:                         # noqa: BLE001
                q = None
            bid, ask = (q[0], q[1]) if q else (None, None)
            rec.samples[int(off)] = {"t": round(now_ms - rec.row["fill_ts"]), "bid": _r(bid), "ask": _r(ask),
                                     "mid": _r(_mid(bid, ask))}
            rec.remaining -= 1
            if rec.remaining <= 0:
                self._finish(key, rec)

    def _finish(self, key: str, rec: _FillRec) -> None:
        self._open.pop(key, None)
        row = dict(rec.row)
        sign = 1.0 if row["side"] == "BUY" else -1.0
        px = row["price"]
        mid0 = row.get("mid_at_fill")
        marks, moves = {}, {}
        for off, s in sorted(rec.samples.items()):
            m = s.get("mid")
            if m is not None and px is not None:
                # value of what we bought (sold) at the mid, vs what we paid (got)
                marks[str(off)] = round(sign * (m - px), 5)
            if m is not None and mid0 is not None:
                # the market's own move after the fill, in our direction
                moves[str(off)] = round(sign * (m - mid0), 5)
        row.update({"event": "markout", "samples": {str(k): v for k, v in sorted(rec.samples.items())},
                    "markout": marks, "mid_move": moves})
        # adverse selection: the move against us in the first second, per share
        m1 = moves.get("1000")
        row["adverse_1s"] = None if m1 is None else round(-m1, 5)
        self.out.write(row)
        self.completed.append(row)
        if len(self.completed) > 500:
            del self.completed[:100]

    def summary(self) -> dict:
        rows = self.completed
        out: dict = {"fills": len(rows), "pending": len(self._open), "path": str(self.out.path)}
        for off in ("100", "250", "500", "1000", "2000", "5000"):
            vals = [r["mid_move"][off] for r in rows if off in r.get("mid_move", {})]
            if vals:
                out[f"mid_move_{off}ms"] = round(sum(vals) / len(vals), 5)
        return out


# ─────────────────────────── signal outcomes ─────────────────────────────


@dataclass
class _SigRec:
    row: dict
    remaining: int
    arrival: dict = field(default_factory=dict)
    horizons: dict = field(default_factory=dict)


class SignalOutcomeTracker:
    """One record per signal: what we saw, what we decided, what happened."""

    def __init__(self, path: str | Path, horizons_s: tuple[float, ...] = SIGNAL_HORIZONS_S,
                 primary_horizon_s: float = 20.0, min_gap_ms: float = 2000.0, max_pending: int = 5000,
                 fee_per_share: Callable[[float], float] | None = None) -> None:
        self.out = _Writer(path)
        self.horizons = tuple(horizons_s)
        self.primary = primary_horizon_s
        self.min_gap_ms = min_gap_ms
        self.max_pending = max_pending
        self.fee = fee_per_share or (lambda p: 0.07 * p * (1.0 - p))
        self._timed = _Timed()
        self._open: dict[str, _SigRec] = {}
        self._last: dict[str, tuple[float, str]] = {}     # slug -> (ts, reason) of the last record
        self.counts: dict[str, int] = {}
        self.completed: list[dict] = []

    @property
    def next_due(self) -> float:
        return self._timed.next_due

    def should_record(self, slug: str, reason: str, now_ms: float, traded: bool) -> bool:
        """Every traded signal; a rejected one on a change of reason or every
        ``min_gap_ms`` -- one opportunity is not two hundred ticks of it."""
        if traded:
            return True
        last = self._last.get(slug)
        return last is None or last[1] != reason or now_ms - last[0] >= self.min_gap_ms

    def on_signal(self, *, signal_id: str, slug: str, token_id: str, side: str, decision: str, reason: str,
                  now_ms: float, ask: float | None, cap: float | None, size: float, arrival_ms: float,
                  **context) -> None:
        if len(self._open) >= self.max_pending or signal_id in self._open:
            return
        self._last[slug] = (now_ms, reason)
        row = {"event": "signal", "signal_id": signal_id, "slug": slug, "token_id": token_id, "side": side,
               "decision": decision, "reason": reason, "ts": now_ms, "ask": _r(ask), "cap": _r(cap),
               "size": _r(size, 4), "arrival_ms": round(arrival_ms, 1),
               **{k: v for k, v in context.items() if v is not None}}
        self._open[signal_id] = _SigRec(row=row, remaining=1 + len(self.horizons))
        self._timed.push(now_ms + max(0.0, arrival_ms), (signal_id, "arrival"))
        for h in self.horizons:
            self._timed.push(now_ms + h * 1000.0, (signal_id, h))

    def attach(self, signal_id: str, **fields) -> None:
        """Late facts about a signal still being followed (the order's fate)."""
        rec = self._open.get(signal_id)
        if rec is not None:
            rec.row.update({k: v for k, v in fields.items() if v is not None})

    def due(self, now_ms: float, sampler: Sampler) -> None:
        if now_ms < self._timed.next_due:
            return
        for sid, what in self._timed.pop_due(now_ms):
            rec = self._open.get(sid)
            if rec is None:
                continue
            cap = rec.row.get("cap")
            try:
                q = sampler(rec.row["token_id"], cap if what == "arrival" else None)
            except Exception:                         # noqa: BLE001
                q = None
            bid, ask, depth = q if q else (None, None, None)
            s = {"t": round(now_ms - rec.row["ts"]), "bid": _r(bid), "ask": _r(ask)}
            if what == "arrival":
                s["depth_under_cap"] = _r(depth, 2)
                rec.arrival = s
            else:
                rec.horizons[what] = s
            rec.remaining -= 1
            if rec.remaining <= 0:
                self._finish(sid, rec)

    def _finish(self, sid: str, rec: _SigRec) -> None:
        self._open.pop(sid, None)
        row = dict(rec.row)
        arr = rec.arrival
        size = row.get("size") or 0.0
        cap = row.get("cap")
        a_ask = arr.get("ask")
        available = (a_ask is not None and cap is not None and a_ask <= cap + 1e-9
                     and (arr.get("depth_under_cap") or 0.0) + 1e-9 >= size)
        row["arrival"] = arr
        row["price_still_available"] = bool(available)
        row["ask_moved_at_arrival"] = None if (a_ask is None or row.get("ask") is None) else round(a_ask - row["ask"], 5)
        values = {}
        for h, s in sorted(rec.horizons.items()):
            b = s.get("bid")
            if available and b is not None:
                entry = a_ask
                # round trip at the touch: buy the arrival ask, sell the bid at h,
                # both taker fees -- what a taker would actually have banked
                values[str(int(h))] = round(b - entry - self.fee(entry) - self.fee(b), 5)
        row["horizons"] = {str(int(h)): s for h, s in sorted(rec.horizons.items())}
        row["hypothetical_net"] = values
        prim = values.get(str(int(self.primary)))
        if not available:
            label = "UNFILLABLE"
        elif prim is None:
            label = "UNKNOWN"
        else:
            label = "WOULD_WIN" if prim > 0 else "WOULD_LOSE"
        traded = row.get("decision") == "BUY"
        filled = row.get("filled")
        if not traded:
            klass = {"UNFILLABLE": "UNFILLABLE_SKIP", "WOULD_WIN": "BAD_MISS", "WOULD_LOSE": "GOOD_MISS"}.get(label, "UNKNOWN")
        elif filled:
            klass = "TRADED"
        elif filled is False:
            klass = {"UNFILLABLE": "KILLED_UNFILLABLE", "WOULD_WIN": "KILLED_BAD_MISS",
                     "WOULD_LOSE": "KILLED_GOOD_MISS"}.get(label, "KILLED_UNKNOWN")
        else:
            klass = "SENT_UNRESOLVED"
        row["hypothetical"] = label
        row["classification"] = klass
        self.counts[klass] = self.counts.get(klass, 0) + 1
        self.out.write(row)
        self.completed.append(row)
        if len(self.completed) > 500:
            del self.completed[:100]

    def on_settle(self, slug: str, up_won: bool | None, now_ms: float) -> None:
        """The window's outcome, for hold-to-settlement analysis (joined on slug)."""
        if up_won is None:
            return
        self.out.write({"event": "settle", "slug": slug, "up_won": bool(up_won), "ts": now_ms})

    def summary(self) -> dict:
        return {"pending": len(self._open), "classified": dict(self.counts), "path": str(self.out.path),
                "write_errors": self.out.write_errors}
