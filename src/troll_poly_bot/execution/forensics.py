"""Trade forensics: one structured, timestamped record per order and position.

Every order the strategy sends passes through these stages:

    MARKET_DATA -> SIGNAL -> DECISION -> RISK_CHECK -> ORDER_SUBMITTED
    -> ORDER_ACKNOWLEDGED | ORDER_REJECTED -> PARTIAL_FILL / FULL_FILL
    -> POSITION_OPEN -> POSITION_MANAGED -> POSITION_CLOSED -> SETTLED

Each stage writes one event line to ``data/trade_forensics.jsonl`` and, when
the record closes, one consolidated ``trade`` line carrying everything the
audit asks for: prices, probabilities, edge, spread, estimated and realised
slippage, fees, per-stage latencies, holding time, reasons, versions and the
PnL AFTER fees. ``won`` is derived from that PnL, never from the caller: a
trade that netted less than zero is a loss in this file whatever the ledger
said, and a caller-reported PnL that disagrees in sign is flagged
``pnl_mismatch`` for the guard.

The recorder never raises into the trading loop: a disk error is counted and
the bot keeps trading.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("forensics")

STAGES = (
    "MARKET_DATA", "SIGNAL", "DECISION", "RISK_CHECK", "ORDER_SUBMITTED",
    "ORDER_ACKNOWLEDGED", "ORDER_REJECTED", "PARTIAL_FILL", "FULL_FILL",
    "POSITION_OPEN", "POSITION_MANAGED", "POSITION_CLOSED", "SETTLED",
)

#: fields a consolidated trade record always carries (None when unknown)
TRADE_FIELDS = (
    "market_id", "condition_id", "token_id", "slug", "asset", "epoch", "side", "kind",
    "entry_price", "exit_price", "quantity", "signal_probability", "market_probability",
    "edge", "spread", "estimated_slippage", "realized_slippage", "fees",
    "latency_ms", "fill_latency_ms", "position_duration_s",
    "pnl", "pnl_after_fees", "won", "reason_for_entry", "reason_for_exit",
    "model_version", "strategy", "strategy_version", "mode", "outcome", "pnl_mismatch",
    "decision_price", "execution_price", "spot_move_bps", "market_reprice", "seconds_left",
    "paper_outcome", "paper_fill_price", "paper_fill_quantity", "paper_exit_price", "paper_pnl",
    # the signal, the price we would pay at most, and how fresh the inputs were
    "signal_id", "limit_price", "best_ask", "best_bid", "max_entry_price", "quote_age_ms", "spot_age_ms",
    "momentum_label", "momentum_z", "momentum_score", "preflight",
    # the timeline (epoch ms): T0 spot event at the exchange, T1 spot received,
    # T2 signal, T3 book (venue stamp / received), T4 decision, T5 sent,
    # T6 venue ack, T7 venue fill, T8 exit decision, T9 exit sent, T10 exit fill
    "t0_spot_event", "t1_spot_recv", "t2_signal", "t3_book_venue", "t3_book_recv", "t4_decision",
    "t5_sent", "t6_venue_ack", "t7_venue_fill", "t8_exit_decision", "t9_exit_sent", "t10_exit_fill",
    "venue_order_id", "sign_ms", "sent_ask", "venue_rtt_ms",
    # the exchange whose price was the newest the decision used (T0/T1 are its stamps)
    "spot_source",
    # derived from the timeline, ms
    "spot_feed_latency_ms", "book_latency_ms", "signal_processing_ms", "presend_ms", "venue_ack_ms",
    "venue_fill_latency_ms", "decision_to_fill_ms", "data_to_fill_ms", "exit_decision_to_fill_ms",
)

#: (field, later stamp, earlier stamp): the latency segments of Phase 4
SEGMENTS = (
    ("spot_feed_latency_ms", "t1_spot_recv", "t0_spot_event"),
    ("book_latency_ms", "t3_book_recv", "t3_book_venue"),
    ("presend_ms", "t5_sent", "t4_decision"),
    ("venue_ack_ms", "t6_venue_ack", "t5_sent"),
    ("venue_fill_latency_ms", "t7_venue_fill", "t5_sent"),
    ("decision_to_fill_ms", "t7_venue_fill", "t4_decision"),
    ("data_to_fill_ms", "t7_venue_fill", "t0_spot_event"),
    ("exit_decision_to_fill_ms", "t10_exit_fill", "t8_exit_decision"),
)


def latency_segments(f: dict) -> dict:
    """The derived latencies of one record; None where a stamp is missing."""
    out = {}
    for name, later, earlier in SEGMENTS:
        a, b = f.get(later), f.get(earlier)
        out[name] = round(a - b, 1) if (a is not None and b is not None) else None
    inputs = [x for x in (f.get("t1_spot_recv"), f.get("t3_book_recv")) if x is not None]
    t4 = f.get("t4_decision")
    out["signal_processing_ms"] = round(t4 - max(inputs), 1) if (t4 is not None and inputs) else None
    return out


@dataclass(slots=True)
class TradeRecord:
    order_id: str
    fields: dict = field(default_factory=dict)
    stages: dict = field(default_factory=dict)          # stage -> ts (ms)
    closed: bool = False


class TradeForensics:
    def __init__(self, path: str | Path = "data/trade_forensics.jsonl",
                 model_version: str = "", strategy_version: str = "", mode: str = "",
                 journal_path: str | Path | None = None) -> None:
        self.path = Path(path)
        #: the append-only trade journal: one line per COMPLETED position
        self.journal_path = Path(journal_path) if journal_path else self.path.with_name("trade_journal.jsonl")
        self.model_version = model_version
        self.strategy_version = strategy_version
        self.mode = mode
        self.records: dict[str, TradeRecord] = {}
        self.closed: list[dict] = []                    # consolidated records, newest last (bounded)
        self.by_token: dict[str, str] = {}              # open position token -> entry order id
        self.write_errors = 0
        self.pnl_mismatches = 0
        self.counts: dict[str, int] = {}

    # ------------------------------------------------------------- recording

    def open(self, order_id: str, **fields) -> TradeRecord:
        rec = self.records.get(order_id)
        if rec is None:
            rec = TradeRecord(order_id=order_id)
            self.records[order_id] = rec
        rec.fields.update({k: v for k, v in fields.items() if v is not None})
        return rec

    def stage(self, order_id: str, stage: str, ts: float, **fields) -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        rec = self.open(order_id, **fields)
        rec.stages.setdefault(stage, ts)
        self.counts[stage] = self.counts.get(stage, 0) + 1
        self._write({"event": "stage", "order_id": order_id, "stage": stage, "ts": ts,
                     **{k: v for k, v in fields.items() if v is not None}})

    def bind_position(self, token_id: str, order_id: str) -> None:
        self.by_token[token_id] = order_id

    def entry_for(self, token_id: str) -> str | None:
        return self.by_token.get(token_id)

    def close(self, order_id: str, ts: float, reported_pnl: float | None = None, **fields) -> dict | None:
        """Consolidate and write the trade line. Returns the record written."""
        rec = self.records.pop(order_id, None)
        if rec is None or rec.closed:
            return None
        rec.closed = True
        rec.fields.update({k: v for k, v in fields.items() if v is not None})
        f = rec.fields
        st = rec.stages
        out: dict = {k: f.get(k) for k in TRADE_FIELDS}
        out.update({"event": "trade", "order_id": order_id, "closed_ts": ts,
                    "stages": dict(st), "model_version": f.get("model_version", self.model_version),
                    "strategy_version": f.get("strategy_version", self.strategy_version),
                    "mode": f.get("mode", self.mode)})
        sub = st.get("ORDER_SUBMITTED")
        ack = st.get("ORDER_ACKNOWLEDGED") or st.get("ORDER_REJECTED") or st.get("FULL_FILL")
        fill = st.get("FULL_FILL") or st.get("PARTIAL_FILL")
        if sub is not None and ack is not None:
            out["latency_ms"] = round(ack - sub, 1)
        if sub is not None and fill is not None:
            out["fill_latency_ms"] = round(fill - sub, 1)
        opened = st.get("POSITION_OPEN") or fill
        closed = st.get("POSITION_CLOSED") or st.get("SETTLED")
        if opened is not None and closed is not None:
            out["position_duration_s"] = round((closed - opened) / 1000.0, 1)
        for k, v in latency_segments(f).items():
            out[k] = v
        dp, xp = f.get("decision_price"), f.get("execution_price")
        if dp is not None and xp is not None:
            out["realized_slippage"] = round(xp - dp, 5)
        # PnL after fees is the only PnL that decides a win
        pnl_after = f.get("pnl_after_fees")
        if pnl_after is None and f.get("pnl") is not None:
            pnl_after = f["pnl"] - (f.get("fees") or 0.0)
        out["pnl_after_fees"] = None if pnl_after is None else round(pnl_after, 4)
        out["won"] = None if pnl_after is None else bool(pnl_after > 0)
        out["pnl_mismatch"] = False
        if reported_pnl is not None and pnl_after is not None and (reported_pnl > 0) != (pnl_after > 0) \
                and abs(reported_pnl - pnl_after) > 1e-6:
            out["pnl_mismatch"] = True
            self.pnl_mismatches += 1
            log.error("PNL MISMATCH %s: reported %+.4f, after fees %+.4f", order_id, reported_pnl, pnl_after)
        self.closed.append(out)
        if len(self.closed) > 500:
            del self.closed[:100]
        for tok, oid in list(self.by_token.items()):
            if oid == order_id:
                self.by_token.pop(tok, None)
        self._write(out)
        if out.get("kind") == "entry" and ("POSITION_CLOSED" in st or "SETTLED" in st):
            self._journal(out)
        return out

    def _journal(self, out: dict) -> None:
        """A completed trade: a real fill that was later closed or settled."""
        row = {
            "trade_id": out["order_id"], "market": out.get("slug"), "asset": out.get("asset"),
            "side": out.get("side"), "entry": out.get("entry_price"), "exit": out.get("exit_price"),
            "quantity": out.get("quantity"), "fees": out.get("fees"), "slippage": out.get("realized_slippage"),
            "model_probability": out.get("signal_probability"), "market_probability": out.get("market_probability"),
            "expected_edge": out.get("edge"), "realized_pnl": out.get("pnl_after_fees"),
            "execution_latency_ms": out.get("latency_ms"), "fill_latency_ms": out.get("fill_latency_ms"),
            "holding_s": out.get("position_duration_s"), "exit_reason": out.get("reason_for_exit"),
            "result": "WIN" if out.get("won") else ("LOSS" if out.get("won") is False else "UNKNOWN"),
            "mode": out.get("mode"), "strategy_version": out.get("strategy_version"),
            "paper_outcome": out.get("paper_outcome"), "paper_fill_price": out.get("paper_fill_price"),
            "paper_exit_price": out.get("paper_exit_price"), "paper_pnl": out.get("paper_pnl"),
            "closed_ts": out.get("closed_ts"),
        }
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            self.write_errors += 1

    def amend(self, order_id: str, ts: float, **fields) -> None:
        """Late information for a record that has already closed. Written as
        its own line; ``load_trades`` folds it into the trade."""
        clean = {k: v for k, v in fields.items() if v is not None}
        for rec in self.closed:
            if rec.get("order_id") == order_id:
                rec.update(clean)
        self._write({"event": "amend", "order_id": order_id, "ts": ts, **clean})

    # -------------------------------------------------------------- reporting

    def snapshot(self) -> dict:
        return {"open": len(self.records), "closed": len(self.closed), "stage_counts": dict(self.counts),
                "pnl_mismatches": self.pnl_mismatches, "write_errors": self.write_errors,
                "path": str(self.path)}

    def _write(self, row: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError:
            self.write_errors += 1


def load_trades(path: str | Path) -> list[dict]:
    """Consolidated trade records from a forensics file (stage lines skipped)."""
    out: list[dict] = []
    p = Path(path)
    if not p.exists():
        return out
    by_id: dict[str, dict] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "trade":
            out.append(row)
            by_id[row.get("order_id", "")] = row
        elif row.get("event") == "amend" and row.get("order_id") in by_id:
            by_id[row["order_id"]].update({k: v for k, v in row.items() if k not in ("event", "order_id", "ts")})
    return out
