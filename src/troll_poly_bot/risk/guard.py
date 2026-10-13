"""The guard: an automatic kill switch for NEW entries.

The strategy's own gates say whether one trade is worth taking. The guard
says whether the bot should be taking trades at all right now. It trips on:

    feed down / silent          a spot exchange or the venue book stopped talking
    stale data                  the freshest spot or book is older than the gate
    execution failure rate      most recent orders were killed (the strategy is
                                sending into quotes that do not exist; 27 of 29
                                on 2026-09-22)
    latency                     order round trip p95 above the limit
    balance drift               venue cash moved by more than our fills explain
    unknown position            the venue holds a token we did not buy (restart
                                without reconciliation, or a fill we missed)
    pnl mismatch                a trade reported profitable that netted a loss
    consecutive losses          a losing streak longer than the limit
    venue halted                the live adapter halted itself
    foreign trade               the venue lists a trade on this account that no
                                order of this process explains (another bot, a
                                human, a second process on the same account)
    reconcile pending           live: no position reconciliation has yet covered
                                the markets being traded (the first one runs
                                before discovery and proves nothing)

Policy for existing positions when tripped: they stay managed. Take-profit,
stop-loss and settlement keep running; only entries stop. Conditions that
reflect the environment (feeds, staleness, latency, kill rate) clear
themselves when the measurement recovers. Conditions that reflect broken
state (balance drift, pnl mismatch) LATCH until the process is restarted
after a human has looked. An unknown position stays in force for as long as
the venue keeps reporting it.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class GuardConfig:
    enabled: bool = True
    max_feed_silence_s: float = 15.0
    #: staleness of the freshest spot/book across assets, ms; 0 = use engine gates only
    max_data_age_ms: float = 10_000.0
    #: execution failure rate over the last ``kill_window`` orders
    kill_window: int = 10
    min_orders_for_kill_rate: int = 8
    max_kill_rate: float = 0.8
    max_order_rtt_p95_ms: float = 3_000.0
    max_balance_drift_usdc: float = 1.0
    halt_on_unknown_position: bool = True
    max_consecutive_losses: int = 5
    #: how long an environmental trip stays in force after the cause clears
    cooldown_s: float = 30.0


LATCHING = {"BALANCE_DRIFT", "PNL_MISMATCH", "FOREIGN_TRADE"}


@dataclass
class Guard:
    cfg: GuardConfig = field(default_factory=GuardConfig)
    tripped: dict[str, str] = field(default_factory=dict)
    latched: dict[str, str] = field(default_factory=dict)
    since: float = 0.0
    last_clear: float = 0.0
    _cleared_from: str = ""
    trips_total: int = 0
    outcomes: deque = field(default_factory=lambda: deque(maxlen=50))   # True = filled

    # ---------------------------------------------------------------- inputs

    def record_order_outcome(self, filled: bool) -> None:
        self.outcomes.append(bool(filled))

    def kill_rate(self) -> float | None:
        n = min(len(self.outcomes), self.cfg.kill_window)
        if n < self.cfg.min_orders_for_kill_rate:
            return None
        recent = list(self.outcomes)[-n:]
        return 1.0 - sum(recent) / n

    def latch(self, code: str, detail: str) -> None:
        if code not in self.latched:
            self.trips_total += 1
        self.latched[code] = detail

    def update(self, now_s: float | None = None, *, feeds: dict | None = None,
               data_age_ms: float | None = None, rtt_p95_ms: float | None = None,
               balance_drift_usdc: float | None = None, unknown_positions: list | None = None,
               consecutive_losses: int = 0, pnl_mismatches: int = 0,
               venue_halted: str | None = None, foreign_trades: list | None = None,
               reconciled: bool | None = None) -> dict[str, str]:
        """Re-evaluate every condition. Returns the active trips."""
        now = time.time() if now_s is None else now_s
        c = self.cfg
        trips: dict[str, str] = {}
        if not c.enabled:
            self.tripped = {}
            return {}
        for name, st in (feeds or {}).items():
            connected = st.get("connected", True)
            silence = st.get("last_msg_age_s")
            if connected is False:
                trips["FEED_DOWN"] = f"{name}: {st.get('last_error') or 'disconnected'}"
            elif silence is not None and silence > c.max_feed_silence_s:
                trips["FEED_SILENT"] = f"{name}: no message for {silence:.0f}s"
        if c.max_data_age_ms > 0 and data_age_ms is not None and data_age_ms > c.max_data_age_ms:
            trips["STALE_DATA"] = f"freshest data {data_age_ms:.0f}ms old"
        kr = self.kill_rate()
        if kr is not None and kr >= c.max_kill_rate:
            trips["EXECUTION_FAILURE_RATE"] = f"{kr:.0%} of the last {min(len(self.outcomes), c.kill_window)} orders failed"
        if rtt_p95_ms is not None and rtt_p95_ms > c.max_order_rtt_p95_ms:
            trips["LATENCY"] = f"order rtt p95 {rtt_p95_ms:.0f}ms"
        if balance_drift_usdc is not None and abs(balance_drift_usdc) > c.max_balance_drift_usdc:
            self.latch("BALANCE_DRIFT", f"venue cash moved {balance_drift_usdc:+.2f} USDC unexplained")
        if c.halt_on_unknown_position and unknown_positions:
            # not latched: the venue re-reports every 30 s, so the condition
            # clears itself only when the venue no longer shows the position
            trips["UNKNOWN_POSITION"] = ", ".join(str(u)[:16] for u in unknown_positions[:4])
        if pnl_mismatches > 0:
            self.latch("PNL_MISMATCH", f"{pnl_mismatches} trade(s) reported with the wrong sign")
        if c.max_consecutive_losses > 0 and consecutive_losses >= c.max_consecutive_losses:
            trips["CONSECUTIVE_LOSSES"] = f"{consecutive_losses} in a row"
        if venue_halted:
            trips["VENUE_HALTED"] = venue_halted
        if foreign_trades:
            self.latch("FOREIGN_TRADE", f"{len(foreign_trades)} trade(s) on the account not placed by this bot: "
                       + ", ".join(f"{t.get('side')} {t.get('size')} @ {t.get('price')}" for t in foreign_trades[:3]))
        if reconciled is False:
            trips["RECONCILE_PENDING"] = "no venue position check has covered the live markets yet"
        trips.update(self.latched)
        was_tripped = [k for k in self.tripped if k != "COOLDOWN"]
        if trips and not was_tripped:
            self.since = now
            self.trips_total += 1
        if not trips and was_tripped:
            self.last_clear = now
            self._cleared_from = ",".join(was_tripped)
        if not trips and self.last_clear and now - self.last_clear < c.cooldown_s:
            trips = {"COOLDOWN": f"{c.cooldown_s - (now - self.last_clear):.0f}s after {self._cleared_from}"}
        self.tripped = trips
        return trips

    # --------------------------------------------------------------- verdict

    @property
    def entries_allowed(self) -> bool:
        return not self.tripped

    def snapshot(self) -> dict:
        return {"entries_allowed": self.entries_allowed, "tripped": dict(self.tripped),
                "latched": dict(self.latched), "since": self.since, "trips_total": self.trips_total,
                "kill_rate": self.kill_rate(), "orders_seen": len(self.outcomes)}
