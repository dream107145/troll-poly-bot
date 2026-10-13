"""The start of an abnormal spot move, seen at tick resolution (2026-09-29).

The spot-lag signal is a 1 s return in units of sigma. By the time a 1 s return
is 3 sigma, the first 100 ms of the move are 900 ms old. This detector watches
every raw quote from every exchange, per asset, and fires when the return over
a SHORT horizon (50 / 100 / 250 / 500 ms) is abnormal for that horizon:

    z_h = r_h / (sigma_per_sec * sqrt(h))  >=  k

with the same ``k`` the strategy uses for the 1 s return, so no new threshold
is introduced, and a microstructure floor: the return must also exceed
``spread_floor`` times the quoting exchange's own relative spread, or a one-tick
bounce on a wide DOGE or XRP book would count at the 50 ms horizon.

It also keeps, per asset:

- **trade intensity**: trades in the last 500 ms against the trailing-minute
  rate, reported with every detection (not gated: it is evidence, not a rule);
- **exchange lead/lag**: for every detection, when each exchange's own mid
  crossed half the move relative to the first one to do so, as a slow average
  per (asset, exchange) -- measured, not assumed.

Nothing here decides a trade. A detection asks the live bot to evaluate the
asset's markets NOW; the strategy's own gates (its shock window, the 1 s
agreement, net edge, price band, budget) still decide, exactly as on a tick.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class FastMove:
    asset: str
    direction: int              # +1 up, -1 down
    horizon_ms: float           # the shortest horizon that was abnormal
    z: float                    # return over that horizon in horizon-scaled sigma
    ret_bps: float
    exchange: str               # the quote that fired
    t_ms: float                 # receipt time of that quote
    intensity: float            # trades in the last 500 ms / the trailing-minute rate per 500 ms
    n_trades_500ms: int


@dataclass(slots=True)
class _Ring:
    t: deque = field(default_factory=deque)
    lp: deque = field(default_factory=deque)

    def at(self, ts: float) -> float | None:
        """Log mid as of ``ts`` (last sample at or before it); None before the first."""
        if not self.t or self.t[0] > ts:
            return None
        # rings are short (a few hundred samples over keep_ms): a backward scan is
        # cheaper than bisect on a deque, whose indexing is not O(1)
        for i in range(len(self.t) - 1, -1, -1):
            if self.t[i] <= ts:
                return self.lp[i]
        return None


@dataclass(slots=True)
class _Pending:
    """A detection whose exchange lead/lag is still being measured."""
    t_fire: float
    direction: int
    half_ret: float
    horizon_ms: float
    base: dict            # exchange -> log mid at t_fire - horizon
    crossed: dict         # exchange -> ms after t_fire when its mid crossed half the move


class FastMoveDetector:
    """Per-asset multi-horizon move-start detection over raw exchange quotes."""

    def __init__(self, horizons_ms: tuple[float, ...] = (50.0, 100.0, 250.0, 500.0), k_sigma: float = 3.0,
                 trigger_frac: float = 1.0, spread_floor: float = 1.5, dedupe_ms: float = 500.0,
                 keep_ms: float = 3_000.0, lead_halflife: int = 20, lead_window_ms: float = 1_500.0) -> None:
        self.horizons = tuple(sorted(float(h) for h in horizons_ms if h > 0))
        self.k = float(k_sigma)
        self.trigger_frac = float(trigger_frac)
        self.spread_floor = float(spread_floor)
        self.dedupe_ms = float(dedupe_ms)
        self.keep_ms = float(max(keep_ms, (max(self.horizons) if self.horizons else 500.0) * 2.0))
        self.lead_window_ms = float(lead_window_ms)
        self._lead_alpha = 1.0 - 0.5 ** (1.0 / max(1, lead_halflife))
        self._rings: dict[str, dict[str, _Ring]] = {}
        self._trades: dict[str, deque] = {}
        self._last_fire: dict[tuple[str, int], float] = {}
        self._pending: dict[str, list[_Pending]] = {}
        #: asset -> exchange -> average ms after the first exchange (0 = it is the first)
        self.lead_ms: dict[str, dict[str, float]] = {}
        self.lead_n: dict[str, dict[str, int]] = {}
        #: asset -> exchange -> detections it quoted through but never confirmed in time
        self.lead_miss: dict[str, dict[str, int]] = {}
        #: detections no second exchange confirmed inside the lead window
        self.false_starts: int = 0
        self.fired: int = 0
        self.by_horizon: dict[float, int] = {h: 0 for h in self.horizons}

    # --------------------------------------------------------------- feeding

    def on_trade(self, asset: str, t_ms: float) -> None:
        d = self._trades.setdefault(asset, deque())
        d.append(t_ms)
        cutoff = t_ms - 60_000.0
        while d and d[0] < cutoff:
            d.popleft()

    def intensity(self, asset: str, now_ms: float) -> tuple[float, int]:
        d = self._trades.get(asset)
        if not d:
            return 0.0, 0
        n500 = 0
        for i in range(len(d) - 1, -1, -1):
            if d[i] < now_ms - 500.0:
                break
            n500 += 1
        span_s = max(1.0, min(60.0, (now_ms - d[0]) / 1000.0))
        base_per_500 = len(d) / span_s * 0.5
        return (n500 / base_per_500 if base_per_500 > 0 else float(n500)), n500

    def on_quote(self, asset: str, exchange: str, bid: float, ask: float, t_ms: float,
                 sigma_per_sec: float | None) -> FastMove | None:
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return None
        mid = 0.5 * (bid + ask)
        lm = math.log(mid)
        ring = self._rings.setdefault(asset, {}).setdefault(exchange, _Ring())
        ring.t.append(t_ms)
        ring.lp.append(lm)
        cutoff = t_ms - self.keep_ms
        while ring.t and ring.t[0] < cutoff:
            ring.t.popleft()
            ring.lp.popleft()
        self._resolve_pending(asset, exchange, lm, t_ms)
        if not sigma_per_sec or sigma_per_sec <= 0.0:
            return None
        floor = self.spread_floor * (ask - bid) / mid
        need = self.trigger_frac * self.k
        for h in self.horizons:
            lp0 = ring.at(t_ms - h)
            if lp0 is None:
                continue
            r = lm - lp0
            z = r / (sigma_per_sec * math.sqrt(h / 1000.0))
            if abs(z) < need or abs(r) < floor:
                continue
            d = 1 if r > 0 else -1
            key = (asset, d)
            if t_ms - self._last_fire.get(key, -math.inf) < self.dedupe_ms:
                return None
            self._last_fire[key] = t_ms
            self.fired += 1
            self.by_horizon[h] = self.by_horizon.get(h, 0) + 1
            inten, n500 = self.intensity(asset, t_ms)
            self._open_pending(asset, exchange, t_ms, d, r, h)
            return FastMove(asset=asset, direction=d, horizon_ms=h, z=z, ret_bps=r * 1e4, exchange=exchange,
                            t_ms=t_ms, intensity=inten, n_trades_500ms=n500)
        return None

    # ------------------------------------------------------- exchange lead/lag

    def _open_pending(self, asset: str, exchange: str, t_fire: float, d: int, r: float, h: float) -> None:
        base = {}
        for x, ring in self._rings.get(asset, {}).items():
            v = ring.at(t_fire - h)
            if v is not None:
                base[x] = v
        p = _Pending(t_fire=t_fire, direction=d, half_ret=0.5 * abs(r), horizon_ms=h, base=base, crossed={exchange: 0.0})
        self._pending.setdefault(asset, []).append(p)

    def _resolve_pending(self, asset: str, exchange: str, lm: float, t_ms: float) -> None:
        plist = self._pending.get(asset)
        if not plist:
            return
        keep = []
        for p in plist:
            if exchange not in p.crossed and exchange in p.base:
                if p.direction * (lm - p.base[exchange]) >= p.half_ret:
                    p.crossed[exchange] = t_ms - p.t_fire
            if t_ms - p.t_fire >= self.lead_window_ms:
                self._close_pending(asset, p)
            else:
                keep.append(p)
        if len(keep) != len(plist):
            self._pending[asset] = keep

    def _close_pending(self, asset: str, p: _Pending) -> None:
        if len(p.crossed) < 2:
            # a move only one exchange showed inside the window is a false start (or
            # an exchange that does not quote the asset): nothing to compare, and
            # charging the others the whole window would make every lag look huge
            self.false_starts += 1
            return
        first = min(p.crossed.values())
        lead = self.lead_ms.setdefault(asset, {})
        n = self.lead_n.setdefault(asset, {})
        miss = self.lead_miss.setdefault(asset, {})
        for x in p.base:
            ms = p.crossed.get(x)
            if ms is None:
                miss[x] = miss.get(x, 0) + 1        # quoted, but never crossed half the move in time
                continue
            rel = ms - first
            prev = lead.get(x)
            lead[x] = rel if prev is None else prev + self._lead_alpha * (rel - prev)
            n[x] = n.get(x, 0) + 1

    # ------------------------------------------------------------- reporting

    def snapshot(self) -> dict:
        return {
            "fired": self.fired,
            "by_horizon_ms": {f"{h:g}": c for h, c in self.by_horizon.items()},
            "lead_ms": {a: {x: round(v, 1) for x, v in d.items()} for a, d in self.lead_ms.items()},
            "lead_n": {a: dict(d) for a, d in self.lead_n.items()},
            "lead_miss": {a: dict(d) for a, d in self.lead_miss.items()},
            "false_starts": self.false_starts,
        }
