"""Execution scenarios for the paper engine.

Why this exists
---------------
The paper engine matches orders against the bot's OWN copy of the venue book.
That copy lags the venue's matching engine: ``book_p50`` measures the venue's
publish timestamp against local receipt, not the event itself, and on
2026-09-22 the local book still showed a quote 0.5 s after the venue had
killed a fill-or-kill against it in 13 of 30 cases, 2 s after in 5, 3 s after
in 3 (docs/live_audit_2026-09-22.md). The paper bot "filled" 23 of 23 such
orders and took profit on every one; the live bot filled 1 of 29.

A scenario says how much of the displayed book to believe:

``quote_survival_ms``
    A matched quote must still be on the local book this long after the
    order arrived, or the fill is refused (``QUOTE_PULLED``). The rule reads
    the local book's lag as an upper bound on how long a quote we can see may
    already have been gone at the venue. Measured on the archive: 2 s takes
    the paper fill rate on the live signal set from 78 % to 26 % (live: 9 %)
    and reproduces 25 of 30 live kills; 3 s gives 17 % and 27 of 30.
``contention``
    Fraction of the touch eaten by faster participants before we land
    (overrides the latency profile's value when not None).
``reject_prob``
    Independent chance the venue kills an otherwise fillable order: outages,
    self-trade prevention, size rounding, things no book model sees.
``latency_multiplier``
    Scales every submit/ack draw.
``depth_haircut``
    Fraction of displayed size assumed to be real.
``survive_to_limit``
    How the survival check reads the later book. False (the original rule): every level we matched
    must still be there, so a 1-cent tick up inside our limit kills the order. True: the later book
    must still fill the matched size at or under the order's LIMIT, and the fill is charged at the
    worse of the two books -- the later book stands in for the venue's book at arrival, and the
    venue fills a limit order against whatever it holds up to the limit.
``book_delay_ms``
    Match a taker order against the local book this long AFTER it arrived,
    instead of against the book at arrival plus a survival check. The local
    book shows the venue's state late; read that much later it approximates
    what was really there at arrival. On the 2026-09-23 tick recording, 500 ms
    reproduced the venue-truth P&L of 694 spot-shock orders (-1.77 vs -1.61c
    at 1 s, -1.64 vs -1.58c with active exits), while the 2 s survival rule
    kept only 37 % of real fills and dropped the ones that went on to win
    (docs/spot_lag_study_2026-09-23.md, section G).

None of the presets is a claim about the venue. OPTIMISTIC is the old engine
(kept for tests of other mechanics); REALISTIC is calibrated to the measured
kill rate; CONSERVATIVE and STRESS must both be survived before live capital
is raised.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionScenario:
    name: str
    quote_survival_ms: float = 0.0
    contention: float | None = None
    reject_prob: float = 0.0
    latency_multiplier: float = 1.0
    depth_haircut: float = 1.0
    book_delay_ms: float = 0.0
    survive_to_limit: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "quote_survival_ms": self.quote_survival_ms,
                "contention": self.contention, "reject_prob": self.reject_prob,
                "latency_multiplier": self.latency_multiplier, "depth_haircut": self.depth_haircut,
                "book_delay_ms": self.book_delay_ms, "survive_to_limit": self.survive_to_limit}


OPTIMISTIC = ExecutionScenario("optimistic")
REALISTIC = ExecutionScenario("realistic", quote_survival_ms=2000.0)
CONSERVATIVE = ExecutionScenario("conservative", quote_survival_ms=3000.0, contention=0.8,
                                 reject_prob=0.10, latency_multiplier=1.5, depth_haircut=0.5)
STRESS = ExecutionScenario("stress", quote_survival_ms=5000.0, contention=0.9,
                           reject_prob=0.30, latency_multiplier=2.5, depth_haircut=0.25)

#: validated against venue-timestamped books (tick recording 2026-09-23); the
#: book read 500 ms late already contains what faster participants took
VENUE_LAG = ExecutionScenario("venue_lag", contention=0.0, book_delay_ms=500.0)

#: 2026-10-01: calibrated on REAL Polymarket orders since the 09-29 order-path work (193 spot-lag orders
#: replayed on their 200 ms archives, arrival = decision + 250 ms). The ask must be inside the limit at
#: arrival and still inside it 1 s later. Real vs predicted fill rate: slow feeds (spot age ~165 ms) 10 % vs
#: 11 %, agreeing on 97 % of orders; fast feeds (Deribit/Kraken/Bitstamp, ~40 ms) 50 % vs 44 %, 90 %.
#: REALISTIC's rule (2 s, every matched level) agrees on 96 % / 82 %: it kills real fills whenever the
#: ask ticks up inside the limit after the fill, which is most winning momentum buys
#: (scripts/triangle_audit/fill_calibration.py; docs/triangle_fill_rate_2026-10-01.md).
CALIBRATED = ExecutionScenario("calibrated", quote_survival_ms=1000.0, survive_to_limit=True)

SCENARIOS: dict[str, ExecutionScenario] = {s.name: s for s in (OPTIMISTIC, REALISTIC, CONSERVATIVE, STRESS,
                                                               VENUE_LAG, CALIBRATED)}


def scenario_named(name: str) -> ExecutionScenario:
    try:
        return SCENARIOS[name.strip().lower()]
    except KeyError:
        raise ValueError(f"unknown execution scenario {name!r}; choose from {sorted(SCENARIOS)}") from None
