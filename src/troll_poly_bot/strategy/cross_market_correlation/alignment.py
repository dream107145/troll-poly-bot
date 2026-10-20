"""Validate that two 5m UP/DOWN markets are aligned enough to compare."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(slots=True)
class MarketSnapshot:
    """Decision-time market facts required before pairing."""
    asset: str
    slug: str
    epoch: int
    open_ts_ms: float
    close_ts_ms: float
    strike: float | None
    spot: float | None
    resolution_source: str = ""
    twap_window_s: float | None = None
    fee_ok: bool = True
    active: bool = True
    tradeable: bool = True
    book_up_fresh: bool = True
    book_down_fresh: bool = True
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AlignmentResult:
    ok: bool
    reason: str = ""
    epoch_delta_s: float = 0.0
    open_delta_ms: float = 0.0
    close_delta_ms: float = 0.0


def validate_market(
    m: MarketSnapshot,
    *,
    max_data_age_ok: bool = True,
    require_strike: bool = True,
    require_spot: bool = True,
) -> AlignmentResult:
    if not m.active:
        return AlignmentResult(False, "inactive")
    if not m.tradeable:
        return AlignmentResult(False, "not_tradeable")
    if m.epoch <= 0:
        return AlignmentResult(False, "missing_epoch")
    if m.open_ts_ms <= 0 or m.close_ts_ms <= 0:
        return AlignmentResult(False, "missing_window")
    if abs((m.close_ts_ms - m.open_ts_ms) / 1000.0 - 300.0) > 1.0:
        return AlignmentResult(False, "not_5m_window")
    if require_strike and (m.strike is None or not (m.strike > 0)):
        return AlignmentResult(False, "missing_strike")
    if require_spot and (m.spot is None or not (m.spot > 0)):
        return AlignmentResult(False, "missing_spot")
    if not m.fee_ok:
        return AlignmentResult(False, "ambiguous_fees")
    if not (m.book_up_fresh and m.book_down_fresh):
        return AlignmentResult(False, "stale_books")
    if not max_data_age_ok:
        return AlignmentResult(False, "stale_market_data")
    # Resolution / TWAP: reject if explicitly marked missing; empty string allowed
    # when the venue uses the standard Chainlink crypto 5m path (known default).
    if m.resolution_source.strip().lower() in ("unknown", "ambiguous", "missing"):
        return AlignmentResult(False, "ambiguous_resolution")
    return AlignmentResult(True, "ok")


def validate_pair(
    a: MarketSnapshot,
    b: MarketSnapshot,
    *,
    max_epoch_skew_s: float = 0.0,
    require_strike: bool = True,
    require_spot: bool = True,
) -> AlignmentResult:
    """Both markets must individually validate and share (or model) the same window."""
    va = validate_market(a, require_strike=require_strike, require_spot=require_spot)
    if not va.ok:
        return AlignmentResult(False, f"a:{va.reason}")
    vb = validate_market(b, require_strike=require_strike, require_spot=require_spot)
    if not vb.ok:
        return AlignmentResult(False, f"b:{vb.reason}")
    if a.asset.upper() == b.asset.upper():
        return AlignmentResult(False, "same_asset")
    open_delta = abs(float(a.open_ts_ms) - float(b.open_ts_ms))
    close_delta = abs(float(a.close_ts_ms) - float(b.close_ts_ms))
    epoch_delta = abs(int(a.epoch) - int(b.epoch))
    # Epoch is unix seconds of window start for *-updown-5m-*; skew in seconds.
    if epoch_delta > float(max_epoch_skew_s) + 1e-9:
        return AlignmentResult(
            False, f"epoch_skew:{epoch_delta}s",
            epoch_delta_s=float(epoch_delta), open_delta_ms=open_delta, close_delta_ms=close_delta,
        )
    if open_delta > (float(max_epoch_skew_s) + 1.0) * 1000.0:
        return AlignmentResult(
            False, f"open_skew:{open_delta:.0f}ms",
            epoch_delta_s=float(epoch_delta), open_delta_ms=open_delta, close_delta_ms=close_delta,
        )
    return AlignmentResult(
        True, "ok",
        epoch_delta_s=float(epoch_delta), open_delta_ms=open_delta, close_delta_ms=close_delta,
    )


def all_unique_pairs(assets: Sequence[str]) -> list[tuple[str, str]]:
    """21 unique unordered pairs for seven assets."""
    xs = [a.strip().upper() for a in assets if a and a.strip()]
    out: list[tuple[str, str]] = []
    for i, a in enumerate(xs):
        for b in xs[i + 1:]:
            out.append((a, b))
    return out
