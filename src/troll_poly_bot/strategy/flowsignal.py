"""Cross-exchange momentum + order-flow direction signal (2026-10-01, the user's spec).

Every WebSocket quote and trade of the chosen exchanges (Coinbase, Bybit, Binance) updates this state and
the score is recomputed at once -- no candle, no 1-second average:

    score = 0.35 * cross_exchange_momentum + 0.30 * aggressive_trade_flow
          + 0.20 * acceleration            + 0.15 * first_mover_strength

    score >= +threshold -> UP, score <= -threshold -> DOWN, else no trade; and only when every part agrees
    (first mover, momentum, order flow, cross-exchange confirmation, acceleration not against it).

Prices are used as RETURNS (mid over the last 20 / 50 / 100 ms), so Coinbase's USD and Binance's / Bybit's
USDT quotes need no conversion: a constant basis cancels. Each tick is dated by the exchange's own event time
(or its receipt time minus the source's measured delivery delay), so "who moved first" compares information
times, not network luck.

Pure and deterministic given the timestamps it is fed: the live bot and the tick replay run the same code.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class FlowParams:
    exchanges: tuple[str, ...] = ("coinbase", "bybit", "binance")
    #: Delta-price windows (ms); the primary one also sets the acceleration bins
    windows_ms: tuple[float, ...] = (20.0, 50.0, 100.0)
    primary_ms: float = 50.0
    #: a move of this many bp over 100 ms is full scale (1.0); other windows scale by sqrt(window / 100 ms)
    ref_bp: float = 2.0
    #: an exchange confirms a direction when it moved at least this many bp that way
    min_bp: float = 0.5
    #: exchanges that must confirm (of those with a fresh quote)
    min_confirm: int = 2
    #: at least this many exchanges must have a fresh quote at all
    min_fresh: int = 2
    #: the cheap pre-check: nothing is scored unless an exchange moved this many bp over 100 ms
    gate_bp: float = 0.3
    trade_window_ms: float = 250.0
    #: aggressive volume (USD) in the window that counts as a fully confident flow reading
    trade_full_usd: float = 20_000.0
    #: acceleration full scale: the move over the latest bin minus the oldest of three, in bp
    accel_ref_bp: float = 2.0
    first_lookback_ms: float = 200.0
    #: an exchange "has moved" once it is this far (bp) from its price at the start of the lookback
    first_cross_bp: float = 0.5
    w_cx: float = 0.35
    w_flow: float = 0.30
    w_acc: float = 0.20
    w_first: float = 0.15
    threshold: float = 0.70
    #: one signal per asset and direction per this many ms
    dedupe_ms: float = 1000.0
    #: a quote older than this (information time) is not used
    stale_ms: float = 1500.0
    #: how long a signal can still be taken by a market's evaluation
    valid_ms: float = 500.0
    #: history kept per exchange (ms)
    keep_ms: float = 1500.0


@dataclass(slots=True)
class FlowResult:
    asset: str
    dir: int                      # +1 UP, -1 DOWN
    score: float
    cx: float
    flow: float
    acc: float
    first: float
    first_mover: str
    confirm: int                  # exchanges that confirm the direction
    fresh: int
    t_info: float                 # information time of the tick that completed it
    t_local: float                # local time it was computed
    moves_bp: dict = field(default_factory=dict)       # exchange -> bp over the best window
    id: int = 0

    def as_dict(self) -> dict:
        return {"score": round(self.score, 3), "cx": round(self.cx, 3), "flow": round(self.flow, 3),
                "acc": round(self.acc, 3), "first": round(self.first, 3), "first_mover": self.first_mover,
                "confirm": self.confirm, "fresh": self.fresh,
                "moves_bp": {k: round(v, 2) for k, v in self.moves_bp.items()}}


class _AssetState:
    __slots__ = ("q", "seen", "trades", "last_fire", "newest")

    def __init__(self) -> None:
        self.q: dict[str, deque] = {}                 # exchange -> deque[(t_info, mid)], price CHANGES only
        self.seen: dict[str, float] = {}              # exchange -> information time of its newest quote
        self.trades: deque = deque()                  # (t_info, signed notional)
        self.last_fire: dict[int, float] = {}
        self.newest = 0.0                             # newest information time seen


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _at(dq: deque, t: float) -> float | None:
    """The mid of the last tick at or before ``t`` (None when the history starts later)."""
    for i in range(len(dq) - 1, -1, -1):
        ts, mid = dq[i]
        if ts <= t:
            return mid
    return None


class FlowSignal:
    def __init__(self, params: FlowParams | None = None) -> None:
        self.p = params or FlowParams()
        self._st: dict[str, _AssetState] = {}
        self._seq = 0
        self.recent: dict[str, deque] = {}            # asset -> deque[FlowResult] (signals, for takers)
        self._taken: set[tuple[str, int]] = set()
        #: counters for the dashboard / the replay
        self.ticks = 0
        self.scored = 0
        self.fired = 0
        #: the parts of the latest scored tick (what the gates saw), for diagnostics and the replay
        self.last_parts: dict = {}

    # ---------------------------------------------------------------- inputs

    def on_quote(self, asset: str, exchange: str, bid: float, ask: float, t_info: float,
                 now_local: float) -> FlowResult | None:
        if exchange not in self.p.exchanges or not (bid > 0.0 and ask >= bid):
            return None
        st = self._st.get(asset)
        if st is None:
            st = self._st[asset] = _AssetState()
        dq = st.q.get(exchange)
        if dq is None:
            dq = st.q[exchange] = deque()
        t_info = max(t_info, st.seen.get(exchange, t_info))      # an out-of-order stamp: keep it monotone
        st.seen[exchange] = t_info
        mid = 0.5 * (bid + ask)
        if not dq or mid != dq[-1][1]:
            dq.append((t_info, mid))                  # a price CHANGE; an unchanged quote only refreshes `seen`
        horizon = t_info - self.p.keep_ms
        while len(dq) > 2 and dq[0][0] < horizon and dq[1][0] <= horizon:
            dq.popleft()
        if t_info > st.newest:
            st.newest = t_info
        self.ticks += 1
        return self._score(asset, st, now_local)

    def on_trade(self, asset: str, exchange: str, notional: float, aggressor_buy: bool, t_info: float,
                 now_local: float) -> FlowResult | None:
        if exchange not in self.p.exchanges or notional <= 0.0:
            return None
        st = self._st.get(asset)
        if st is None:
            st = self._st[asset] = _AssetState()
        st.trades.append((t_info, notional if aggressor_buy else -notional))
        if t_info > st.newest:
            st.newest = t_info
        horizon = st.newest - max(self.p.keep_ms, self.p.trade_window_ms)
        while st.trades and st.trades[0][0] < horizon:
            st.trades.popleft()
        self.ticks += 1
        return self._score(asset, st, now_local)

    # ---------------------------------------------------------------- the score

    def _score(self, asset: str, st: _AssetState, now_local: float) -> FlowResult | None:
        p = self.p
        T = st.newest
        fresh = [x for x in p.exchanges if st.q.get(x) and T - st.seen[x] <= p.stale_ms]
        if len(fresh) < p.min_fresh:
            return None
        # cheap pre-check: has any exchange moved enough over 100 ms to matter at all
        gate = False
        for x in fresh:
            dq = st.q[x]
            b = _at(dq, T - 100.0)
            if b is not None and abs(dq[-1][1] / b - 1.0) * 1e4 >= p.gate_bp:
                gate = True
                break
        if not gate:
            return None
        self.scored += 1

        # ---- cross-exchange momentum (and who confirms)
        best: dict[str, float] = {}                   # exchange -> its signed normalised move (best window)
        move_bp: dict[str, float] = {}
        for x in fresh:
            dq = st.q[x]
            last = dq[-1][1]
            top, top_bp = 0.0, 0.0
            for w in p.windows_ms:
                b = _at(dq, T - w)
                if b is None:
                    continue
                bp = (last / b - 1.0) * 1e4
                n = _clip(bp / (p.ref_bp * math.sqrt(w / 100.0)))
                if abs(n) > abs(top):
                    top, top_bp = n, bp
            best[x], move_bp[x] = top, top_bp
        cx = sum(best.values()) / len(fresh)
        if cx == 0.0:
            return None
        d = 1 if cx > 0 else -1
        confirm = sum(1 for x in fresh if move_bp[x] * d >= p.min_bp)

        # ---- aggressive trade flow
        buy = sell = 0.0
        t0 = T - p.trade_window_ms
        for ts, sn in reversed(st.trades):
            if ts < t0:
                break
            if sn > 0:
                buy += sn
            else:
                sell -= sn
        tot = buy + sell
        flow = 0.0 if tot <= 0.0 else ((buy - sell) / tot) * min(1.0, tot / p.trade_full_usd)

        # ---- acceleration: three consecutive bins of the primary window, consensus move per bin
        w = p.primary_ms
        bins = [0.0, 0.0, 0.0]
        for x in fresh:
            dq = st.q[x]
            pr = [dq[-1][1]] + [_at(dq, T - w * k) for k in (1, 2, 3)]
            for k in range(3):
                if pr[k + 1] is not None and pr[k] is not None:
                    bins[k] += (pr[k] / pr[k + 1] - 1.0) * 1e4
        m0, m2 = bins[0] / len(fresh), bins[2] / len(fresh)
        acc = _clip((m0 - m2) / p.accel_ref_bp)

        # ---- first mover: the exchange that left its lookback-start price first, and who followed
        L = p.first_lookback_ms
        first_name, first_t, first_dir, first_bp = "", math.inf, 0, 0.0
        crossed: dict[str, tuple[float, int]] = {}
        for x in fresh:
            dq = st.q[x]
            base = _at(dq, T - L)
            if base is None:
                continue
            thr = p.first_cross_bp * 1e-4
            for ts, mid in dq:
                if ts <= T - L:
                    continue
                r = mid / base - 1.0
                if abs(r) >= thr:
                    crossed[x] = (ts, 1 if r > 0 else -1)
                    break
            if x in crossed and crossed[x][0] < first_t:
                first_name, first_t, first_dir = x, crossed[x][0], crossed[x][1]
                first_bp = (dq[-1][1] / base - 1.0) * 1e4
        fm = 0.0
        if first_name:
            followers = sum(1 for x, (ts, sg) in crossed.items() if x != first_name and sg == first_dir
                            and ts >= first_t)
            f = followers / max(1, len(fresh) - 1)
            mag = min(1.0, abs(first_bp) / (p.ref_bp * math.sqrt(L / 100.0)))
            fm = first_dir * mag * (0.5 + 0.5 * f)

        score = p.w_cx * cx + p.w_flow * flow + p.w_acc * acc + p.w_first * fm
        self.last_parts = {"t": T, "score": score, "cx": cx, "flow": flow, "acc": acc, "first": fm,
                           "first_mover": first_name, "confirm": confirm, "fresh": len(fresh)}
        if abs(score) < p.threshold:
            return None
        # every part must agree: first mover, momentum, order flow, confirmation; acceleration not against
        if not (fm * d > 0 and flow * d > 0 and confirm >= p.min_confirm and acc * d >= 0.0 and score * d > 0):
            return None
        last_fire = st.last_fire.get(d)
        if last_fire is not None and now_local - last_fire < p.dedupe_ms:
            return None
        st.last_fire[d] = now_local
        self._seq += 1
        self.fired += 1
        res = FlowResult(asset=asset, dir=d, score=score, cx=cx, flow=flow, acc=acc, first=fm,
                         first_mover=first_name, confirm=confirm, fresh=len(fresh), t_info=T,
                         t_local=now_local, moves_bp=move_bp, id=self._seq)
        rq = self.recent.setdefault(asset, deque(maxlen=64))
        rq.append(res)
        return res

    # ---------------------------------------------------------------- consumers

    def take(self, asset: str, slug: str, now_local: float) -> FlowResult | None:
        """The newest signal of this asset still valid and not yet taken by this market."""
        for res in reversed(self.recent.get(asset, ())):
            if now_local - res.t_local > self.p.valid_ms:
                break
            if (slug, res.id) in self._taken:
                continue
            self._taken.add((slug, res.id))
            if len(self._taken) > 5000:
                self._taken = {k for k in self._taken if k[1] > self._seq - 500}
            return res
        return None
