"""SPOT_LAG: after a large spot shock, buy the token that has not caught up yet,
and sell it a few seconds later.

EXPERIMENTAL and behind a flag: selected with ``--strategy spot_lag``
(``TPB_STRATEGY``) and refused unless ``TPB_SPOT_LAG_ENABLED=1``. It
subclasses the momentum engine, so every downstream consumer (risk, the
entry confirmation checklist, decision journal, forensics, guard, the paper
and live exchanges and the exit machinery) is reused unchanged.

Evidence (docs/spot_lag_study_2026-09-23.md): the broad version -- buy any
token that looks behind spot -- loses in every period. This narrow version was
+3.5c/share out of sample (about 140 fills, t = +2.7, archive with a fill model
checked against the venue book) but is concentrated in BTC and not yet
confirmed on real fills. Run it in paper or shadow first.

Per market, every evaluation (the live bot's 200 ms tick)::

    z1 = 1 s spot log-return / sigma          >= k_sigma          (a spot shock)
    z3 = 3 s log-return / (sigma * sqrt 3)    agrees by >= k3_sigma
    accelerating: the last second carries more than a third of the 3 s move
    at most one signal per market and direction per ``dedupe_s``
    MFV(p)    = logistic(a + b * logit(p_model)): the market's OWN pricing curve
                (fitted per seconds-left bucket on the training archive), fed
                the analytic TWAP model's p_model
    ev        = side mid 1 s ago + (MFV now - MFV 1 s ago)   where the token should be
    lag       = ev - ask                                    executable ask only
    net edge  = lag - fee(ask) - exit cost (fee(ev) + spread/2) - slippage - uncertainty
    trade iff net edge >= min_net_edge; FOK at cap = min(ask + max_slippage, max entry)

Exit: ``hold_s`` after the fill, a FOK sell at the bid minus two ticks (the live
bot's stop path); inside the last 10 s the position is held to settlement.
"""
from __future__ import annotations

import bisect
import math
import os
from collections import deque
from dataclasses import dataclass

from ..features.engine import FeatureEngine
from ..pricing.vol import shock_sigma
from ..pricing.twap import twap_fair_value
from ..risk.limits import RiskManager
from ..types import Order, Side, TimeInForce
from .engine import EngineConfig, Evaluation, Reason, SideView
from .momentum import (
    MomentumConfig, MomentumEngine, MomentumSignal, calculate_net_edge, depth_under, exit_cost_estimate,
    get_executable_price, max_entry_price,
)

#: logit(Up mid) = a + b * logit(p_model), per seconds-left bucket (lo, hi]; fitted
#: on the training third of the 5-minute archive (09-22 15:30 -> 09-23 08:35 UTC,
#: quiet points) and frozen for every evaluation since
MFV_COEF: tuple[tuple[float, float, float, float], ...] = (
    (240.0, 300.0, -0.00516, 0.74394),
    (180.0, 240.0, 0.01201, 0.83442),
    (120.0, 180.0, 0.00974, 0.87316),
    (90.0, 120.0, -0.01039, 0.90782),
    (60.0, 90.0, -0.00085, 0.92390),
    (30.0, 60.0, 0.01608, 0.95148),
    (0.0, 30.0, -0.10100, 0.93414),
)
ENABLE_ENV = "TPB_SPOT_LAG_ENABLED"


def market_implied_fair(p_model: float, seconds_left: float) -> float | None:
    """Where the market puts the Up token for this model probability, once it has caught up."""
    p = min(max(p_model, 0.01), 0.99)
    x = math.log(p / (1.0 - p))
    for lo, hi, a, b in MFV_COEF:
        if lo < seconds_left <= hi:
            return 1.0 / (1.0 + math.exp(-(a + b * x)))
    return None


@dataclass(slots=True)
class SpotLagConfig:
    #: the environment this config reads (a subclass, e.g. repriced_momentum, has its own)
    ENV_PREFIX = "TPB_SPOT_LAG_"
    ENABLE_ENV_NAME = ENABLE_ENV
    #: named parameter sets, chosen with <ENV_PREFIX>PROFILE (default "baseline" = the
    #: dataclass defaults). "optimized" is the 2026-09-25 realistic sweep's locked pick
    #: (docs/spot_lag_parameter_optimization_2026-09-25.md): +105 USDC over 51 h of train +
    #: validation but +1 USDC over the 12 h untouched test -- PAPER ONLY, no robust edge was
    #: found. Explicit <ENV_PREFIX>* values still override a profile's. The engine-level caps
    #: (EngineConfig.max_spread 0.03 / max_slippage 0.02, and MicroCaps in micro-live) still
    #: bound max_spread / max_slippage, so the profile's 5c / 3c act as 3c / 2c in practice.
    PROFILES = {"baseline": {},
                "optimized": {"max_slippage": 0.03, "max_spread": 0.05, "max_ask": 0.60,
                              "max_quote_age_ms": 1000.0, "max_spot_age_ms": 500.0}}
    #: the feature flag; the engine refuses to exist without it
    enabled: bool = False
    #: the spot shock: 1 s log return in units of sigma
    k_sigma: float = 3.0
    #: the shock window in seconds. 1.0 = the tested rule (1 s return >= k_sigma, the 3 s
    #: return agrees, accelerating). Shorter (0.5, 0.25): the return over that window,
    #: sqrt(window)-scaled, >= k_sigma, with the 1 s return in the same direction -- the
    #: same move seen earlier, before the token has repriced (docs/spot_lag_fast_2026-09-29.md).
    shock_window_s: float = 1.0
    #: the 3 s return must agree by this many sigma (sqrt(3)-scaled)
    k3_sigma: float = 0.5
    require_acceleration: bool = True
    #: judge the shock against the 1-second volatility (pricing.vol.shock_sigma). False = the
    #: two-scale sigma that prices the 5-minute outcome, up to 3x larger (the rule until 2026-09-30)
    shock_sigma_1s: bool = False
    #: FAIR-VALUE TRIGGER (2026-09-30), cents; 0 = off. A signal also fires when the token's
    #: market-implied fair value moved this much in the last second, even without a k_sigma spot
    #: shock: near the strike the token is so sensitive that a 1-2 sigma spot move reprices it by
    #: 5-15c, and 78 % of fast token moves (5c in 2 s) over 30 h had no 3-sigma spot shock at all.
    #: Every later gate (lag, edge, band, spread, book age, budget) still applies.
    #: scripts/research_detection.py: with the edge gate, 12c -> +5.2 / +4.9 c/share at 5 s
    #: (DEV / TEST) against +2.2 / +2.5 for the sigma trigger.
    fv_trigger_cents: float = 0.0
    #: TREND FILTER (2026-10-01, the user's instruction "buy only when the crypto price trend is
    #: upward"): the spot return over ``trend_window_s`` BEFORE the shock second must point the
    #: shock's way by at least ``trend_min_bps``. 0 = off (the shock alone decides).
    trend_window_s: float = 0.0
    trend_min_bps: float = 0.0
    #: one signal per market and direction per this long
    dedupe_s: float = 2.0
    #: net edge after both fees, the exit's half spread, slippage and uncertainty
    min_net_edge: float = 0.02
    slippage: float = 0.005
    uncertainty: float = 0.005
    #: the FOK cap may sit this far above the seen ask (never above max entry)
    max_slippage: float = 0.02
    max_spread: float = 0.03
    min_ask: float = 0.05
    max_ask: float = 0.95
    min_time_remaining_s: float = 60.0
    max_time_remaining_s: float = 295.0
    #: shares per order (never below the market's minimum)
    size_shares: float = 5.0
    #: "fixed": the order is ``size_shares`` or nothing (FOK needs that much under the
    #: limit); "touch": take what the book holds under the limit, down to the venue
    #: minimum -- a 10-share target fills 7 when 7 are there instead of being refused
    size_mode: str = "fixed"
    #: entry order type. "FOK": all of ``size_shares`` at the cap or better, or nothing.
    #: "FAK" (fill-and-kill, IOC): whatever the book holds at the cap when the order
    #: lands, up to ``size_shares``; the rest is cancelled. Sent when at least the venue
    #: minimum shows under the cap. Exits stay FOK. docs/fak_2026-09-29.md
    order_type: str = "FOK"
    #: sell this long after the fill
    hold_s: float = 5.0
    #: ... unless the spot is still moving our way: while the 1 s spot return, in
    #: sigma, is at least ``extend_z1`` in the position's direction, keep holding,
    #: at most to ``extend_max_hold_s``. Exit search 2026-09-24 (38 rules, archive,
    #: chosen on training): the value of holding peaks at 5-6 s; this was the only
    #: rule ahead of the plain 5 s timer both in training (+0.32c/share, t 1.8) and
    #: out of sample (+0.24c, t 1.2, 3 of 4 periods). Small and not proven; the
    #: repricing is simply not finished while the spot is still moving.
    #: extend_max_hold_s <= hold_s turns it off.
    extend_z1: float = 0.5
    extend_max_hold_s: float = 10.0
    #: the exit may walk this far below the bid to get the size done
    exit_max_cross: float = 0.02
    #: price exits, checked before the timer on every tick (0 = off). TAKE_PROFIT
    #: when the best bid is at least this many cents above the entry (sold AT the
    #: bid); STOP_LOSS when it is at least this many cents below (sold through the
    #: bid like a time stop). The timer above is then the maximum hold, so a
    #: position that reaches neither never rides to a 1 / 0 settlement. Realistic
    #: replay 2026-09-25 (docs/spot_lag_parameter_optimization_2026-09-25.md):
    #: +10c / -5c / 20 s made +98 USDC on the selection period (timer alone +105)
    #: and +4.9 on the untouched test (timer +1.0); a 3c stop costs a quarter of
    #: the profit because a third of the winners dip 5c before they move.
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    #: never sell: every position settles at 1 / 0 (one fee, no spread, no exit cross).
    #: Realistic replay 2026-09-29 (docs/spot_lag_fast_2026-09-29.md): +254 vs +169 USDC
    #: on the 82 h forward period for the live rule, at twice the drawdown. The take-profit
    #: and stop-loss above still apply if set; the timer does not.
    hold_to_settlement: bool = False
    #: the traded token's venue-timestamped quote age (live.quote_age_ms); in the
    #: 2 s after a spot move the feed's p95 delay is ~650 ms, and a stale local
    #: book is where most of the apparent lag comes from
    max_quote_age_ms: float = 250.0
    max_spot_age_ms: float = 1000.0
    #: the model's information lag, seconds (measured spot feed lag p50)
    info_lag_s: float = 0.18
    #: comma lists; HYPE lost in training (-4.1c, t -3.0) and out of sample
    assets: str = ""
    exclude_assets: str = "HYPE"
    # --- portfolio: every 5-minute market (asset x window) is its own opportunity.
    # These are GLOBAL budgets across all markets. The live bot applies the
    # tighter of these and its own risk limits (in micro-live: graduation.MicroCaps,
    # raised per app with TPB_MICRO_*), so nothing here can lift a real-money ceiling.
    #: open positions plus working entries, all markets together
    max_simultaneous_positions: int = 4
    #: BUY orders sent in any trailing hour, all markets, killed ones included
    max_buys_per_hour: int = 60
    #: FILLED entries per market (a market that has had its entry takes no more)
    max_entries_per_market: int = 1
    #: BUY orders sent per market, killed ones included (a kill does not end the
    #: market's chance, a later signal in it may try again)
    max_attempts_per_market: int = 3
    #: target notional per order; 0 = the venue's minimum (``size_shares`` shares)
    order_size_usdc: float = 0.0
    #: worst-case notional (limit x size) of one order
    max_order_usdc: float = 5.0
    #: open positions plus working entries at their worst case, all markets
    max_total_exposure_usdc: float = 10.0
    max_daily_loss_usdc: float = 10.0
    # --- event-driven evaluation (live.LiveBot._maybe_shock_eval) ---------------
    #: evaluate an asset's markets the moment a spot quote shows a shock, instead
    #: of waiting for the next 200 ms tick and the 100 ms spot-model throttle
    #: (~150 ms on average; on the 2026-09-23 tick recording the token has
    #: repriced ~1c more of a 10c shock response in that time). Off by default.
    event_driven: bool = False
    #: the quote-level pre-check fires at this fraction of k_sigma; the engine's
    #: own gates (k_sigma, z3, acceleration, net edge) still decide
    event_trigger_frac: float = 0.9
    #: at most one event evaluation per asset per this long, so a burst of
    #: quotes during a shock cannot flood the engine
    event_min_interval_ms: float = 50.0
    # --- fast feed (features/fastmove.py; live.LiveBot._on_quote) -------------------
    #: watch every raw exchange quote at tick resolution and evaluate the asset's
    #: markets the moment the return over a short horizon is abnormal for that
    #: horizon (k_sigma, sqrt-scaled), instead of waiting for the 1 s return. The
    #: engine's own gates still decide; pair it with ``shock_window_s`` < 1 so the
    #: gate can pass on the same short return. Spot history is then sampled every
    #: ``fast_history_gap_ms`` instead of 200 ms so those returns are exact.
    fast_feed: bool = False
    fast_horizons_ms: str = "50,100,250,500"
    #: the return must also exceed this many times the quoting exchange's spread
    fast_spread_floor: float = 1.5
    fast_dedupe_ms: float = 500.0
    fast_history_gap_ms: float = 20.0

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SpotLagConfig":
        env = os.environ if env is None else env
        cfg = cls()
        profile = (env.get(cls.ENV_PREFIX + "PROFILE") or "baseline").strip().lower()
        if profile not in cls.PROFILES:
            raise ValueError(f"{cls.ENV_PREFIX}PROFILE={profile!r}: known profiles {sorted(cls.PROFILES)}")
        for name, value in cls.PROFILES[profile].items():
            setattr(cfg, name, value)
        cfg.enabled = (env.get(cls.ENABLE_ENV_NAME) or "").strip().lower() in ("1", "true", "yes")
        for name in cfg.__dataclass_fields__:                      # type: ignore[attr-defined]
            if name == "enabled":
                continue
            key = cls.ENV_PREFIX + name.upper()
            raw = (env.get(key) or "").strip()
            cur = getattr(cfg, name)
            if isinstance(cur, str) and key in env:
                setattr(cfg, name, raw)                   # an empty list is a setting too ("exclude nothing")
                continue
            if not raw:
                continue
            try:
                if isinstance(cur, bool):
                    setattr(cfg, name, raw.lower() not in ("0", "false", "no"))
                elif isinstance(cur, str):
                    setattr(cfg, name, raw)
                elif isinstance(cur, int):
                    setattr(cfg, name, int(float(raw)))
                else:
                    setattr(cfg, name, float(raw))
            except ValueError:
                continue
        return cfg

    def apply_budget(self, risk_cfg, order_ceiling: float | None = None) -> list[str]:
        """Write the portfolio budget into a RiskConfig.

        Positions, total exposure and daily loss only ever TIGHTEN: the tighter of
        the two wins, so a micro-live ceiling already in there stays. The per-order
        cap is SET to ``max_order_usdc`` (never above ``order_ceiling``, micro-live's
        order ceiling): the default per-market cap is scaled to the balance, 2.5 USDC
        at a 50 USDC bankroll, below the venue's 5-share minimum at most prices --
        paper would refuse what micro-live (which sets 5) sends.
        """
        changed = []
        order_cap = self.max_order_usdc if order_ceiling is None else min(self.max_order_usdc, order_ceiling)
        for attr, new_of in (("max_concurrent_positions", lambda old: min(old, int(self.max_simultaneous_positions))),
                             ("max_total_exposure_usdc", lambda old: min(old, self.max_total_exposure_usdc)),
                             ("max_daily_loss_usdc", lambda old: min(old, self.max_daily_loss_usdc)),
                             ("max_position_usdc", lambda old: order_cap)):
            old = getattr(risk_cfg, attr)
            new = new_of(old)
            if new != old:
                setattr(risk_cfg, attr, new)
                changed.append(f"{attr}: {old} -> {new}")
        return changed

    def asset_ok(self, asset: str) -> bool:
        a = asset.upper()
        allow = {x.strip().upper() for x in self.assets.split(",") if x.strip()}
        deny = {x.strip().upper() for x in self.exclude_assets.split(",") if x.strip()}
        return (not allow or a in allow) and a not in deny

    def momentum_mirror(self) -> MomentumConfig:
        """The MomentumConfig the shared live-bot plumbing reads: the entry
        confirmation's freshness and time checks, revalidation's edge bar and
        the exit's cross limit."""
        return MomentumConfig(
            profile="spot_lag", max_quote_age_ms=self.max_quote_age_ms, max_spot_age_ms=self.max_spot_age_ms,
            min_time_remaining_s=self.min_time_remaining_s, max_time_remaining_s=self.max_time_remaining_s,
            min_net_edge=self.min_net_edge, expected_slippage=self.slippage, latency_cost_per_s=0.0,
            max_slippage=self.max_slippage, max_spread=self.max_spread, max_ask=self.max_ask,
            max_hold_s=self.hold_s, exit_max_cross=self.exit_max_cross, cooldown_s=0.0,
            max_trades_per_market=1_000_000)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}   # type: ignore[attr-defined]


class SpotLagEngine(MomentumEngine):
    """Same surface as the momentum engine; the spot-lag decision inside."""

    #: FOK outcomes that are this strategy working as designed, not the venue
    #: failing: the cap is strict and the lag is often gone by arrival, so about
    #: 80 % of orders are expected to die this way (study, section G). The live
    #: bot keeps them out of the guard's execution-failure rate, which would
    #: otherwise trip on the strategy's normal behaviour and never clear.
    #: Venue rejects and missing liquidity still count.
    expected_kills: frozenset[str] = frozenset({"fok_unfillable", "quote_pulled"})

    def __init__(self, cfg: EngineConfig, scfg: SpotLagConfig, risk: RiskManager,
                 features: FeatureEngine | None = None) -> None:
        if not scfg.enabled:
            raise ValueError(f"the spot_lag strategy is behind a feature flag: set {ENABLE_ENV}=1")
        super().__init__(cfg, scfg.momentum_mirror(), risk, features)
        self.scfg = scfg
        # Every piece of per-market state is keyed by the market's slug
        # ("<asset>-updown-5m-<window epoch>"), which names ONE asset in ONE 5-minute
        # window; positions and entry times are keyed by token id (one side of one
        # market). Nothing here is per asset only, so a BTC position cannot block ETH,
        # and BTC 14:05 cannot block BTC 14:10.
        #: slug -> (t, Up mid, MFV of Up) at each evaluation, the last ~10 s
        self._hist: dict[str, deque] = {}
        #: (slug, direction) -> when the last signal fired
        self._last_signal: dict[tuple[str, int], float] = {}
        #: slug -> BUY orders sent in that market (``self.trades`` counts FILLED entries)
        self.attempts: dict[str, int] = {}
        #: slug -> close time, to forget a market's counters once it is over
        self._close_ts: dict[str, float] = {}
        #: send times of BUY orders, all markets, for the hourly budget
        self._buy_times: deque = deque()
        self.last_spot_lag: dict[str, dict] = {}
        #: asset -> the sigma the last shock was judged against (1-second scale by default)
        self.last_shock_sigma: dict[str, float] = {}
        #: SHOCK MARKS (2026-10-01, the user's request): every detection -- the moment the engine
        #: saw a sudden spot move -- per market, with whether it became a signal and why not:
        #: the dashboard draws them on the spot chart so the detector can be checked by eye
        self.shock_marks: dict[str, deque] = {}

    SHOCK_MARKS_PER_MARKET = 300

    def _note_shock(self, slug: str, now: float, d: int, info: dict) -> dict | None:
        """Record a detection, one per market and direction per second; returns the mark."""
        dq = self.shock_marks.setdefault(slug, deque(maxlen=self.SHOCK_MARKS_PER_MARKET))
        for mk in reversed(dq):
            if mk["dir"] == d and now - mk["t"] < 1_000.0:
                return None
            if now - mk["t"] >= 1_000.0:
                break
        mk = {"t": round(now), "dir": d, "z1": info.get("z1"), "z3": info.get("z3"), "r1_bps": info.get("r1_bps"),
              "trigger": info.get("trigger", "sigma"), "ok": False, "why": ""}
        dq.append(mk)
        return mk

    def shock_marks_for(self, slug: str) -> list[dict]:
        return list(self.shock_marks.get(slug, ()))

    def shock_sigma(self, vol) -> float:
        """The per-second sigma a spot move is judged against (see ``shock_sigma_1s``)."""
        return shock_sigma(vol) if self.scfg.shock_sigma_1s else vol.sigma_per_sec

    #: the live bot's structured [MARKET]/[SIGNAL]/[ORDER]/[POSITION]/[SKIP] log lines
    structured_logs = True
    #: the live bot persists this engine's open positions and restores them on restart
    restores_positions = True

    @property
    def allows_reentry(self) -> bool:
        """A market that has been exited may be entered again only if configured."""
        return int(self.scfg.max_entries_per_market) > 1

    # ------------------------------------------------------------ portfolio

    def buys_last_hour(self, now_ms: float) -> int:
        while self._buy_times and self._buy_times[0] <= now_ms - 3_600_000.0:
            self._buy_times.popleft()
        return len(self._buy_times)

    def on_entry_submit(self, slug: str, now_ms: float) -> None:
        """A BUY order for ``slug`` went to the exchange (whatever becomes of it)."""
        self.attempts[slug] = self.attempts.get(slug, 0) + 1
        self._buy_times.append(now_ms)

    def restore_entry(self, slug: str, token_id: str, entry_ms: float, close_ts: float = 0.0) -> None:
        """A position carried over a restart: the market counts as entered and
        the timed exit runs from the original fill time."""
        self.trades[slug] = max(self.trades.get(slug, 0), 1)
        self.attempts[slug] = max(self.attempts.get(slug, 0), 1)
        self.entry_ms[token_id] = entry_ms
        if close_ts:
            self._close_ts[slug] = close_ts

    def _forget_closed(self, now_ms: float) -> None:
        for slug in [s for s, c in self._close_ts.items() if now_ms > c + 60_000.0]:
            self._close_ts.pop(slug, None)
            self._hist.pop(slug, None)
            self._last_signal.pop((slug, 1), None)
            self._last_signal.pop((slug, -1), None)
            self.attempts.pop(slug, None)
            self.trades.pop(slug, None)
            self.last_exit_ms.pop(slug, None)

    def portfolio(self, now_ms: float) -> dict:
        r = self.risk
        return {"open_positions": len(r.open), "working_entries": len(r.pending - set(r.open)),
                "max_simultaneous_positions": r.cfg.max_concurrent_positions,
                "exposure_usdc": round(r.exposure_total(), 2), "max_total_exposure_usdc": r.cfg.max_total_exposure_usdc,
                "buys_last_hour": self.buys_last_hour(now_ms), "max_buys_per_hour": int(self.scfg.max_buys_per_hour),
                "markets_entered": sum(1 for v in self.trades.values() if v > 0),
                "markets_attempted": len(self.attempts)}

    def _history_at(self, slug: str, t_ms: float, max_gap_ms: float = 600.0) -> tuple[float, float] | None:
        h = self._hist.get(slug)
        if not h:
            return None
        ts = [x[0] for x in h]
        k = bisect.bisect_right(ts, t_ms) - 1
        if k < 0 or t_ms - ts[k] > max_gap_ms:
            return None
        return h[k][1], h[k][2]

    # --------------------------------------------------------------- entry

    def evaluate(self, meta, book_up, book_down, spot, spot_age_ms, book_age_ms, now, vol, twap, latency,
                 balance, orderflow=None, quote_ages=None) -> Evaluation:
        cfg, sc, m = self.cfg, self.scfg, meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)
        if not sc.asset_ok(m.asset):
            return self._reject(ev, Reason.ASSET_FILTERED, f"{m.asset} excluded (assets={sc.assets!r}, "
                                                           f"exclude={sc.exclude_assets!r})")
        if spot is None or spot_age_ms is None:
            return self._reject(ev, Reason.STALE_DATA, "no spot")
        max_spot = min(cfg.max_spot_age_ms, sc.max_spot_age_ms)
        if spot_age_ms > max_spot:
            return self._reject(ev, Reason.STALE_DATA, f"spot {spot_age_ms:.0f}ms > {max_spot:.0f}")
        if book_age_ms is None or book_age_ms > cfg.max_book_age_ms:
            return self._reject(ev, Reason.STALE_DATA, "book" if book_age_ms is None else f"book {book_age_ms:.0f}ms")
        if spot.n_sources >= 2 and spot.deviation_bps > cfg.max_source_deviation_bps:
            return self._reject(ev, Reason.DATA_INCONSISTENT, f"sources differ {spot.deviation_bps:.1f} bps")
        if m.strike <= 0.0:
            return self._reject(ev, Reason.NO_STRIKE)
        if vol is None or not getattr(vol, "ready", False) or twap is None or not twap.ready:
            return self._reject(ev, Reason.MODEL_NOT_READY)
        sigma = vol.sigma_per_sec                 # prices the 5-minute outcome (two-scale)
        self.last_sigma[m.asset] = sigma
        ssig = self.shock_sigma(vol)              # judges a 1-second move
        self.last_shock_sigma[m.asset] = ssig

        # the market-implied fair value, recorded every tick so "1 s ago" exists
        mid_up = book_up.mid if book_up is not None else None
        if mid_up is None and book_down is not None and book_down.mid is not None:
            mid_up = 1.0 - book_down.mid
        ev.p_market = mid_up
        fv = twap_fair_value(state=twap, strike=m.strike, sigma_per_sec=sigma, now_ms=now,
                             close_ts_ms=m.close_ts, info_lag_s=sc.info_lag_s)
        ev.p_analytic = fv.p_up
        mfv_now = market_implied_fair(fv.p_up, left)
        before = self._history_at(m.slug, now - 1000.0)
        if mid_up is not None and mfv_now is not None:
            h = self._hist.setdefault(m.slug, deque())
            if not h or now > h[-1][0]:
                h.append((now, mid_up, mfv_now))
            while h and h[0][0] < now - 10_000.0:
                h.popleft()
        # a market's counters live until a minute after ITS close, never shorter:
        # forgetting an entered market early would let it be entered twice
        self._close_ts[m.slug] = m.close_ts
        self._forget_closed(now)

        if not (sc.min_time_remaining_s <= left <= sc.max_time_remaining_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
        need_ms = latency.round_trip_ms() + cfg.latency_safety_margin_ms
        if left * 1000.0 < need_ms:
            return self._reject(ev, Reason.CANNOT_LAND_IN_TIME)

        # SPOT SHOCK
        hist = self.features.spot.get(m.asset)
        r1 = hist.logret(now, 1.0) if hist is not None else None
        r3 = hist.logret(now, 3.0) if hist is not None else None
        if r1 is None or r3 is None or ssig <= 0.0:
            return self._reject(ev, Reason.MODEL_NOT_READY, "spot history")
        fast = sc.shock_window_s < 1.0 - 1e-9
        if fast:
            rw = hist.logret(now, sc.shock_window_s)
            if rw is None:
                return self._reject(ev, Reason.MODEL_NOT_READY, "spot history")
            zw = rw / (ssig * math.sqrt(sc.shock_window_s))
            z1 = zw                                   # the shock statistic the gates below read
        else:
            z1 = r1 / ssig
        z3 = r3 / (ssig * math.sqrt(3.0))
        # the fair-value trigger (``fv_trigger_cents``): the token's own expected move, in cents
        fv_move = (mfv_now - before[1]) if (sc.fv_trigger_cents > 0.0 and before is not None
                                            and mfv_now is not None) else None
        by_fv = fv_move is not None and abs(z1) < sc.k_sigma and abs(fv_move) * 100.0 >= sc.fv_trigger_cents
        shock = abs(z1) >= sc.k_sigma or by_fv
        d = (1 if fv_move > 0 else -1) if by_fv else (1 if z1 > 0 else -1)
        info = {"z1": round(z1, 3), "z3": round(z3, 3), "r1_bps": round(r1 * 1e4, 2), "sigma_bps_s": round(sigma * 1e4, 3),
                "shock_sigma_bps_s": round(ssig * 1e4, 3), "trigger": "fv" if by_fv else "sigma"}
        if fv_move is not None:
            info["fv_move_c"] = round(fv_move * 100.0, 2)
        ev.features = {"spot_lag": info,
                       # the shared journals read a "momentum" block; describe the shock in it
                       "momentum": {"label": ("SHOCK_UP" if d > 0 else "SHOCK_DOWN") if shock else "NONE",
                                    "z": {"main": round(z1, 3), "3s": round(z3, 3)}, "score": round(abs(z1), 3)}}
        ev.spot_move_bps = r1 * 1e4
        self.last_signal[m.asset] = MomentumSignal(direction=d if shock else 0,
                                                   label=ev.features["momentum"]["label"],
                                                   z={"main": z1, "3s": z3}, sigma_per_sec=ssig)
        if not shock:
            return self._reject(ev, Reason.WEAK_MOMENTUM, f"z1 {z1:+.2f} < {sc.k_sigma}")
        mark = self._note_shock(m.slug, now, d, info)

        def skipped(reason, why):
            if mark is not None:
                mark["why"] = why
            return self._reject(ev, reason, why)
        if sc.trend_window_s > 0.0:
            # the trend filter: the move over the window before the shock second, the shock's way
            lp_a = hist.at(now - 1000.0 - sc.trend_window_s * 1000.0)
            lp_b = hist.at(now - 1000.0)
            if lp_a is None or lp_b is None:
                return skipped(Reason.MODEL_NOT_READY, "spot history (trend)")
            trend_bps = (lp_b - lp_a) * 1e4 * d
            info["trend_bps"] = round(trend_bps, 2)
            if mark is not None:
                mark["trend_bps"] = info["trend_bps"]
            if trend_bps < sc.trend_min_bps:
                return skipped(Reason.WEAK_MOMENTUM,
                               f"{sc.trend_window_s:g} s trend {trend_bps:+.1f} bps against the shock (min {sc.trend_min_bps:g})")
        if by_fv:
            # the fair value must have moved WITH the spot, not on time decay alone
            if r1 * d <= 0.0:
                return skipped(Reason.WEAK_MOMENTUM, f"fair value {fv_move * 100:+.1f}c without a spot move")
        elif fast:
            # the tested fast rule: the 1 s return must not point the other way
            if r1 * d < 0.0:
                return skipped(Reason.WEAK_MOMENTUM, f"1 s return {r1 * 1e4:+.1f} bps against the shock")
        else:
            if z3 * d < sc.k3_sigma:
                return skipped(Reason.WEAK_MOMENTUM, f"z3 {z3:+.2f} does not agree")
            if sc.require_acceleration and not (r1 * d > r3 * d / 3.0):
                return skipped(Reason.MOMENTUM_EXHAUSTED, "not accelerating")
        key = (m.slug, d)
        last = self._last_signal.get(key)
        if last is not None and now - last < sc.dedupe_s * 1000.0:
            return skipped(Reason.COOLDOWN, f"signal {now - last:.0f}ms after the last one")
        self._last_signal[key] = now
        # a NEW signal: from here on every refusal is a skipped opportunity (the
        # live bot logs it as [SKIP] with this market and the reason)
        info["signal"] = True
        if mark is not None:
            mark["ok"] = True

        # ONE MARKET = ONE OPPORTUNITY; the budgets below are global
        if self.trades.get(m.slug, 0) >= int(sc.max_entries_per_market):
            return self._reject(ev, Reason.MARKET_ENTERED,
                                f"{self.trades[m.slug]} entry already in this market (max {int(sc.max_entries_per_market)})")
        if self.attempts.get(m.slug, 0) >= int(sc.max_attempts_per_market):
            return self._reject(ev, Reason.MARKET_ATTEMPTS,
                                f"{self.attempts[m.slug]} buy attempts already in this market "
                                f"(max {int(sc.max_attempts_per_market)})")
        if self.buys_last_hour(now) >= int(sc.max_buys_per_hour):
            return self._reject(ev, Reason.HOURLY_BUY_LIMIT,
                                f"{len(self._buy_times)} buys in the last hour (max {int(sc.max_buys_per_hour)})")

        side = "UP" if d > 0 else "DOWN"
        token = m.yes_token_id if side == "UP" else m.no_token_id
        book = book_up if side == "UP" else book_down
        qa = (quote_ages or {}).get(token) if quote_ages is not None else book_age_ms
        if quote_ages is not None and qa is None:
            return self._reject(ev, Reason.STALE_DATA, f"no valid {side} book on the current feed connection")
        if qa is not None and qa > sc.max_quote_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"{side} quote {qa:.0f}ms > {sc.max_quote_age_ms:.0f}")

        # EXPECTED TOKEN VALUE: where the market's own curve says it should be now
        if mid_up is None or mfv_now is None or before is None:
            return self._reject(ev, Reason.MODEL_NOT_READY, "no 1 s token / fair-value history")
        mid_b, mfv_b = before
        if side == "UP":
            ev_px, mid_side, reprice = mid_b + (mfv_now - mfv_b), mid_up, mid_up - mid_b
        else:
            ev_px, mid_side, reprice = (1.0 - mid_b) + ((1.0 - mfv_now) - (1.0 - mfv_b)), 1.0 - mid_up, mid_b - mid_up
        ev_px = min(max(ev_px, 0.0), 1.0)
        ev.market_reprice = mid_up - mid_b
        ev.p_used = ev_px if side == "UP" else 1.0 - ev_px
        info.update({"ev": round(ev_px, 4), "mfv_move": round((mfv_now - mfv_b) * d, 4), "token_move": round(reprice, 4)})
        sv = SideView(side=side, token_id=token, p_model=ev_px, p_market=mid_side, ask=None, bid=None,
                      depth_at_limit=0.0, costs=None, reason=None)
        ev.sides = [sv]
        if book is None or book.best_ask is None or book.best_bid is None:
            sv.reason = Reason.NO_OFFER
            return self._reject(ev, Reason.NO_OFFER)
        ask, bid = book.best_ask, book.best_bid
        sv.ask, sv.bid = ask, bid
        if not (max(cfg.min_trade_price, sc.min_ask) <= ask <= min(cfg.max_trade_price, sc.max_ask)):
            sv.reason = Reason.PRICE_BAND
            return self._reject(ev, Reason.PRICE_BAND, f"ask {ask:.2f}")
        spread = ask - bid
        # the tighter of our own limits and the engine's, which micro-live clamps
        # (graduation.MicroCaps) and the dashboard may lower but never raise past them
        max_spread = min(sc.max_spread, cfg.max_spread)
        max_slip = min(sc.max_slippage, cfg.max_slippage)
        if spread > max_spread + 1e-9:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"spread {spread:.3f}")
        lag = ev_px - ask
        info.update({"lag": round(lag, 4), "ask": ask, "bid": bid})
        if lag <= 0.0:
            sv.reason = Reason.TOKEN_ALREADY_MOVED
            return self._reject(ev, Reason.TOKEN_ALREADY_MOVED, f"ask {ask:.2f} >= expected {ev_px:.3f}")

        # COSTS -> the most this signal may pay
        exit_cost = exit_cost_estimate(ev_px, spread, meta.fee)
        fixed = sc.slippage + sc.uncertainty + exit_cost
        tick = m.tick_size or 0.01
        max_entry = max_entry_price(ev_px, meta.fee, fixed, sc.min_net_edge, tick)
        ev.max_entry_price = max_entry
        cap = min(1.0 - tick, round(ask + max_slip, 4))
        if max_entry is not None:
            cap = min(cap, max_entry)
        info.update({"exit_cost": round(exit_cost, 4), "max_entry": max_entry, "cap": round(cap, 4),
                     "quote_age_ms": None if qa is None else round(qa)})
        ev.features["execution"] = {"fair": round(ev_px, 4), "exit_cost": round(exit_cost, 4),
                                    "uncertainty": sc.uncertainty, "max_entry": max_entry, "cap": round(cap, 4),
                                    "best_ask": ask, "best_bid": bid, "spread": round(spread, 4),
                                    "quote_age_ms": None if qa is None else round(qa),
                                    "spot_age_ms": round(spot_age_ms)}
        # SIZE: the venue minimum (or ORDER_SIZE_USDC worth), then bounded -- at the
        # worst case, the FOK limit -- by the per-order cap and by what is left of the
        # GLOBAL exposure budget (open positions + working entries in every market).
        # Reduced to fit if it is still a valid order, refused if not: never sent past it.
        venue_min = float(m.min_size) if m.min_size else float(sc.size_shares)
        min_notional = getattr(self.risk.cfg, "micro_min_notional_usdc", 1.0) or 0.0
        px_for_size = max(cap, 0.01)
        venue_min = max(venue_min, math.ceil(min_notional / px_for_size * 100.0) / 100.0)
        if sc.order_size_usdc > 0.0:
            size = max(venue_min, math.floor(sc.order_size_usdc / px_for_size * 100.0) / 100.0)
        else:
            size = max(venue_min, float(sc.size_shares))
        per_order = min(sc.max_order_usdc, self.risk.cfg.max_position_usdc)
        room = self.risk.capacity_total()
        bound = min(per_order, room)
        if size * px_for_size > bound + 1e-9:
            fit = math.floor(bound / px_for_size * 100.0) / 100.0
            info["size_reduced_from"] = size
            if fit + 1e-9 < venue_min:
                if room < per_order:
                    return self._reject(ev, Reason.EXPOSURE_CAP,
                                        f"{room:.2f} of {self.risk.cfg.max_total_exposure_usdc:.2f} USDC exposure left; "
                                        f"the minimum order is {venue_min * px_for_size:.2f}")
                return self._reject(ev, Reason.RISK_LIMIT,
                                    f"the minimum order {venue_min * px_for_size:.2f} USDC exceeds the order cap {per_order:.2f}")
            size = fit
        exec_probe, _ = get_executable_price(book, size, ask + max_slip)
        if exec_probe is not None:
            sv.costs = calculate_net_edge(ev_px, exec_probe, bid, meta.fee, self.mcfg, 0.0, sc.uncertainty, exit_cost)
            sv.entry_px = exec_probe
        if max_entry is None or ask > max_entry + 1e-9:
            sv.reason = Reason.ASK_ABOVE_MAX_ENTRY
            return self._reject(ev, Reason.ASK_ABOVE_MAX_ENTRY,
                                f"ask {ask:.2f} > max entry {'none' if max_entry is None else f'{max_entry:.2f}'} "
                                f"(expected {ev_px:.3f}, lag {lag:+.3f})")
        depth = depth_under(book, cap)
        sv.depth_at_limit = depth
        exec_price, avail = get_executable_price(book, size, cap)
        if exec_price is None and sc.size_mode == "touch" and avail + 1e-9 >= venue_min:
            # take what the book holds under the limit instead of refusing the order
            info["size_reduced_from"] = size
            size = math.floor(avail * 100.0) / 100.0
            exec_price, avail = get_executable_price(book, size, cap)
        if exec_price is None and self._fak and avail + 1e-9 >= venue_min:
            # a FAK takes what is there when it lands: price the edge on what shows
            # now, keep the full size as the order (and the worst case it reserves)
            visible = math.floor(avail * 100.0) / 100.0
            exec_price, _ = get_executable_price(book, visible, cap)
            info["fak_visible_shares"] = visible
        if exec_price is None:
            sv.reason = Reason.LOW_LIQUIDITY
            return self._reject(ev, Reason.LOW_LIQUIDITY, f"{avail:.0f} shares under {cap:.2f}, need {size:.0f}")
        cb = calculate_net_edge(ev_px, exec_price, bid, meta.fee, self.mcfg, 0.0, sc.uncertainty, exit_cost)
        sv.costs, sv.entry_px = cb, exec_price
        if cb.net_edge < sc.min_net_edge:
            sv.reason = Reason.EDGE_TOO_SMALL
            return self._reject(ev, Reason.EDGE_TOO_SMALL,
                                f"expected {ev_px:.3f} exec {exec_price:.3f} net {cb.net_edge:+.4f} < {sc.min_net_edge}")
        epoch = int(m.open_ts // 1000)
        # the worst case (the limit) is what counts against the global budget
        verdict = self.risk.check(m.slug, m.asset, epoch, size * cap)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)
        if size * cap > balance + 1e-9:
            return self._reject(ev, Reason.SIZE_ZERO, f"balance {balance:.2f} < {size * cap:.2f}")
        info.update({"size": size, "worst_case_usdc": round(size * cap, 4), "exposure_room": round(room, 4)})
        ev.side, ev.token_id, ev.limit_price, ev.expected_price = side, token, cap, exec_price
        ev.size, ev.net_edge, ev.confidence = size, cb.net_edge, 1.0
        ev.size_note = f"{size:g} shares ({size * cap:.2f} USDC worst case, {room:.2f} of budget left)"
        self.last[m.slug] = ev
        self.last_spot_lag[m.asset] = {"slug": m.slug, "side": side, "t": now, **info}
        return ev

    @property
    def _fak(self) -> bool:
        return str(self.scfg.order_type or "FOK").strip().upper() in ("FAK", "IOC")

    def to_order(self, ev: Evaluation, now_ms: float | None = None) -> Order:
        """A marketable limit at up to the cap, never a market order: fill-or-kill, or
        fill-and-kill (IOC) when ``order_type`` is FAK."""
        sl = (ev.features or {}).get("spot_lag", {})
        return Order(token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
                     tif=TimeInForce.IOC if self._fak else TimeInForce.FOK,
                     expected_price=ev.expected_price, fair_at_decision=ev.p_used,
                     tag=f"{ev.side} spot_lag z1={sl.get('z1', 0):+.2f} lag={sl.get('lag', 0):+.3f} "
                         f"edge={ev.net_edge:+.3f} cap={ev.limit_price:.2f}")

    # ---------------------------------------------------------------- exits

    def exit_decision(self, *, asset: str, side: str, token_id: str, entry: float, bid: float, now_ms: float,
                      sig: MomentumSignal | None = None, spread: float | None = None,
                      bid_depth: float | None = None, size: float | None = None,
                      data_age_ms: float | None = None) -> str | None:
        """TAKE_PROFIT / STOP_LOSS on the bid against the entry when configured
        (``take_profit_cents`` / ``stop_loss_cents``); else TIME_STOP once the
        position has been held ``hold_s`` -- later, up to ``extend_max_hold_s``,
        while the spot is still moving the position's way (the token has not
        finished repricing). A position with no recorded entry time (e.g.
        inherited across a restart) is past its few-second thesis by definition,
        so it is closed at once."""
        sc = self.scfg
        opened = self.entry_ms.get(token_id)
        if opened is None and not sc.hold_to_settlement:
            return "TIME_STOP"
        rise = bid - entry
        if sc.take_profit_cents > 0.0 and rise >= sc.take_profit_cents / 100.0 - 1e-9:
            return "TAKE_PROFIT"
        if sc.stop_loss_cents > 0.0 and rise <= -sc.stop_loss_cents / 100.0 + 1e-9:
            return "STOP_LOSS"
        if sc.hold_to_settlement:
            return None
        held = (now_ms - opened) / 1000.0
        if held < sc.hold_s:
            return None
        if held < sc.extend_max_hold_s:
            z1 = self.spot_z1(asset, now_ms)
            want = 1 if side == "UP" else -1
            if z1 is not None and z1 * want >= sc.extend_z1:
                return None
        return "TIME_STOP"

    def spot_z1(self, asset: str, now_ms: float) -> float | None:
        """The last second's spot log return in units of the asset's sigma."""
        hist = self.features.spot.get(asset)
        sigma = self.last_shock_sigma.get(asset) or self.last_sigma.get(asset)
        r1 = hist.logret(now_ms, 1.0) if hist is not None else None
        if r1 is None or not sigma:
            return None
        return r1 / sigma

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["strategy"] = "spot_lag"
        snap["spot_lag_config"] = self.scfg.as_dict()
        snap["spot_lag"] = dict(self.last_spot_lag)
        snap["spot_lag_portfolio"] = self.portfolio(self._buy_times[-1] if self._buy_times else 0.0)
        return snap


_SKIP_TEXT = {
    Reason.MARKET_ENTERED: "already entered this market",
    Reason.MARKET_ATTEMPTS: "buy attempts for this market used up",
    Reason.HOURLY_BUY_LIMIT: "hourly buy limit reached",
    Reason.EXPOSURE_CAP: "insufficient bankroll: global exposure budget",
    Reason.TOKEN_ALREADY_MOVED: "token already repriced (no lag left)",
    Reason.ASK_ABOVE_MAX_ENTRY: "slippage limit exceeded: ask above max entry",
    Reason.EDGE_TOO_SMALL: "net edge too small after costs",
    Reason.PRICE_BAND: "price outside the 0.05-0.95 band",
    Reason.SIZE_ZERO: "insufficient cash balance",
    Reason.STALE_DATA: "stale data",
    Reason.NO_OFFER: "no ask on the book",
    Reason.RISK_LIMIT: "risk limit",
    Reason.ALREADY_POSITIONED: "already holding this market",
    Reason.COOLDOWN: "duplicate signal",
}


def skip_reason_text(reason: Reason | None, detail: str = "") -> str:
    """A refusal in words, for the [SKIP] log line."""
    if reason is Reason.LOW_LIQUIDITY:
        text = "spread too wide" if "spread" in detail else "not enough depth under the cap"
    elif reason is Reason.RISK_LIMIT and ("exposure" in detail or "position" in detail):
        text = "insufficient bankroll: " + detail
    else:
        text = _SKIP_TEXT.get(reason, reason.value if reason is not None else "unknown")
    return f"{text} ({detail})" if detail and not text.endswith(detail) else text
