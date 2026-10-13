"""Deployment modes and the graduation check between them.

    BACKTEST   scripts/research_*.py on the archive
    PAPER      simulated fills against the local book under an execution scenario
    SHADOW     real account, orders signed and NOT posted (``--live`` without ``--armed``)
    MICRO_LIVE real orders, capped at MICRO caps regardless of what the CLI asks
    LIVE       real orders at the CLI caps; only after graduation
    HALT       nothing is sent (kill file / guard)

Graduation from MICRO_LIVE to LIVE needs evidence, not a good week. The
criteria are per-EPOCH (every asset in one 5-minute window is one bet) and
are computed from the append-only earnings file, which tags each settled
window with the mode that produced it. Nothing here can be satisfied by paper
results: only ``LIVE-ARMED`` rows count, because those are the only rows the
venue's matching engine agreed to.

``TPB_GRADUATION_OVERRIDE=I_ACCEPT_UNPROVEN_EDGE`` bypasses the check. It is
an explicit act, logged, never the default.
"""
from __future__ import annotations

import json
import math
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("backtest", "paper", "shadow", "micro-live", "live", "halt")
OVERRIDE_PHRASE = "I_ACCEPT_UNPROVEN_EDGE"


@dataclass(frozen=True, slots=True)
class MicroCaps:
    """What MICRO_LIVE enforces whatever the command line says.

    Read from the environment (``TPB_MICRO_*``) so nothing is hard-coded in
    the trading path. The order cap is 5 USDC because the venue's smallest
    valid order is 5 shares with at least 1 USDC of notional: a 1 USDC cap
    would refuse every token priced above 0.20. The bot still sends the
    SMALLEST valid size (``RiskConfig.micro_fixed_size``), the cap is only
    the ceiling that size may reach at high prices.
    """
    max_order_usdc: float = 5.0
    max_open_usdc: float = 10.0
    max_daily_loss_usdc: float = 10.0
    max_orders_per_hour: int = 5
    max_concurrent_positions: int = 1
    max_slippage: float = 0.02
    max_spread: float = 0.03
    #: rest a post-only limit instead of hitting the ask (the taker path
    #: showed adverse selection on every fill it got)
    maker_entry: bool = True
    #: the smallest order the venue accepts: 5 shares and 1 USDC of notional
    min_shares: float = 5.0
    min_notional_usdc: float = 1.0

    @classmethod
    def from_env(cls, env: dict | None = None) -> "MicroCaps":
        env = os.environ if env is None else env
        d = cls()

        def num(name, default, cast=float):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return cast(raw)
            except ValueError:
                return default

        return cls(
            max_order_usdc=num("TPB_MICRO_MAX_ORDER_USD", d.max_order_usdc),
            max_open_usdc=num("TPB_MICRO_MAX_OPEN_EXPOSURE_USD", d.max_open_usdc),
            max_daily_loss_usdc=num("TPB_MICRO_MAX_DAILY_LOSS_USD", d.max_daily_loss_usdc),
            max_orders_per_hour=num("TPB_MICRO_MAX_ORDERS_PER_HOUR", d.max_orders_per_hour, int),
            max_concurrent_positions=num("TPB_MICRO_MAX_CONCURRENT_POSITIONS", d.max_concurrent_positions, int),
            max_slippage=num("TPB_MICRO_MAX_SLIPPAGE", d.max_slippage),
            max_spread=num("TPB_MICRO_MAX_SPREAD", d.max_spread),
            maker_entry=(env.get("TPB_MICRO_MAKER_ENTRY", "1").strip().lower() not in ("0", "false", "no")),
            min_shares=num("TPB_MICRO_MIN_SHARES", d.min_shares),
            min_notional_usdc=num("TPB_MICRO_MIN_NOTIONAL_USD", d.min_notional_usdc),
        )

    def apply(self, cfg) -> list[str]:
        """Write the micro settings into a BotConfig. Returns what changed."""
        changed = []

        def put(obj, name, value):
            old = getattr(obj, name)
            if old != value:
                setattr(obj, name, value)
                changed.append(f"{type(obj).__name__}.{name}: {old} -> {value}")

        put(cfg.engine, "max_spread", min(cfg.engine.max_spread, self.max_spread))
        put(cfg.engine, "max_slippage", self.max_slippage)
        put(cfg.engine, "maker_entry", self.maker_entry)
        put(cfg.risk, "max_concurrent_positions", self.max_concurrent_positions)
        put(cfg.risk, "max_position_usdc", self.max_order_usdc)
        put(cfg.risk, "max_total_exposure_usdc", min(cfg.risk.max_total_exposure_usdc, self.max_open_usdc))
        put(cfg.risk, "max_daily_loss_usdc", min(cfg.risk.max_daily_loss_usdc, self.max_daily_loss_usdc))
        put(cfg.risk, "micro_fixed_size", True)
        put(cfg.risk, "micro_min_shares", self.min_shares)
        put(cfg.risk, "micro_min_notional_usdc", self.min_notional_usdc)
        return changed

    def enforce(self, cfg, caps=None) -> list[str]:
        """Clamp every value the micro caps bound back under its ceiling.

        ``apply`` runs once at start; this runs after every dashboard control
        change. Without it ``data/controls.json`` raised a micro-live bot's
        daily-loss limit from 10 to 40 on 2026-09-23 and could lift the
        per-order cap to 500. It only ever LOWERS a value.
        """
        clamped: list[str] = []

        def ceil(obj, name, limit):
            if obj is None or not hasattr(obj, name):
                return
            old = getattr(obj, name)
            if old > limit:
                setattr(obj, name, limit)
                clamped.append(f"{type(obj).__name__}.{name}: {old} -> {limit} (micro-live ceiling)")

        ceil(cfg.engine, "max_spread", self.max_spread)
        ceil(cfg.engine, "max_slippage", self.max_slippage)
        ceil(cfg.risk, "max_concurrent_positions", self.max_concurrent_positions)
        ceil(cfg.risk, "max_position_usdc", self.max_order_usdc)
        ceil(cfg.risk, "max_total_exposure_usdc", self.max_open_usdc)
        ceil(cfg.risk, "max_daily_loss_usdc", self.max_daily_loss_usdc)
        if not cfg.risk.micro_fixed_size:
            cfg.risk.micro_fixed_size = True
            clamped.append("RiskConfig.micro_fixed_size: False -> True (micro-live ceiling)")
        mom = getattr(cfg, "momentum", None)
        ceil(mom, "max_spread", self.max_spread)
        ceil(mom, "max_slippage", self.max_slippage)
        ceil(caps, "max_order_usdc", self.max_order_usdc)
        ceil(caps, "max_open_usdc", self.max_open_usdc)
        ceil(caps, "max_daily_loss_usdc", self.max_daily_loss_usdc)
        ceil(caps, "max_orders_per_hour", self.max_orders_per_hour)
        return clamped


@dataclass(slots=True)
class GraduationCriteria:
    min_epochs: int = 300
    min_t: float = 2.0
    min_positive_epoch_frac: float = 0.5
    max_drawdown_frac_of_total: float = 1.0   # drawdown may not exceed the total gained
    mode_tags: tuple[str, ...] = ("LIVE-ARMED",)


@dataclass(slots=True)
class Verdict:
    ready: bool
    reasons: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _rows(path: Path) -> list[dict]:
    """Every settled trade on disk, whichever way it closed.

    This used to keep only ``exited`` rows, so a position held to the bell
    -- the losing stop that never fired, the winner held to 1.00 -- was
    silently left out of the evidence that decides whether real capital
    scales. Rows marked to an estimated price (an unresolved window) are
    not evidence either way and stay out.
    """
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("slug") and not r.get("marked"):
            out.append(r)
    return out


def epoch_stats(rows: list[dict]) -> dict:
    per: dict[int, float] = {}
    for r in rows:
        try:
            epoch = int(str(r["slug"]).rsplit("-", 1)[-1])
        except (ValueError, KeyError):
            continue
        per[epoch] = per.get(epoch, 0.0) + float(r.get("pnl") or 0.0)
    vals = list(per.values())
    n = len(vals)
    if n == 0:
        return {"epochs": 0, "trades": len(rows)}
    mean = statistics.fmean(vals)
    se = statistics.stdev(vals) / math.sqrt(n) if n > 1 else float("nan")
    eq, peak, dd = 0.0, 0.0, 0.0
    for e in sorted(per):
        eq += per[e]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"epochs": n, "trades": len(rows), "mean_per_epoch": round(mean, 4),
            "se": None if n < 2 else round(se, 4),
            "t": None if n < 2 or not se else round(mean / se, 2),
            "positive_epochs": sum(1 for v in vals if v > 0), "total": round(sum(vals), 4),
            "max_drawdown": round(dd, 4)}


def assess(earnings_path: str | Path = "data/earnings.jsonl",
           criteria: GraduationCriteria | None = None, strategy: str | None = None) -> Verdict:
    """``strategy``: only that strategy's rows count. Evidence for one decision
    engine says nothing about another; rows written before the field existed
    carry no strategy and count for none."""
    c = criteria or GraduationCriteria()
    rows = [r for r in _rows(Path(earnings_path)) if r.get("mode") in c.mode_tags
            and (strategy is None or r.get("strategy") == strategy)]
    st = epoch_stats(rows)
    reasons: list[str] = []
    n = st.get("epochs", 0)
    if n < c.min_epochs:
        reasons.append(f"{n} live epochs, need {c.min_epochs}")
    t = st.get("t")
    if t is None or t < c.min_t:
        reasons.append(f"evidence t = {t}, need >= {c.min_t}")
    if n and st["positive_epochs"] / n < c.min_positive_epoch_frac:
        reasons.append(f"{st['positive_epochs']}/{n} epochs positive, need {c.min_positive_epoch_frac:.0%}")
    if st.get("total", 0.0) <= 0.0:
        reasons.append("total live PnL is not positive")
    elif st.get("max_drawdown", 0.0) > c.max_drawdown_frac_of_total * st["total"]:
        reasons.append(f"max drawdown {st['max_drawdown']:.2f} exceeds the total {st['total']:.2f}")
    return Verdict(ready=not reasons, reasons=reasons, stats=st)


def override_present(env: dict | None = None) -> bool:
    env = os.environ if env is None else env
    return (env.get("TPB_GRADUATION_OVERRIDE") or "").strip() == OVERRIDE_PHRASE
