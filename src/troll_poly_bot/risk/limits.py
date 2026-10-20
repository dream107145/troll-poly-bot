"""Hard limits and sizing. Nothing here ever increases size after a loss.

Sizing is fractional Kelly on the *net* edge, scaled by the model's confidence
and capped every way that matters:

    stake = balance * kelly_fraction * f_star * confidence
    f_star = (p - c) / (1 - c)        Kelly for a binary at price c

then clamped by per-market, per-asset, per-epoch, gross and share caps.

Correlation
-----------
All crypto 5m markets in the same 300s window are one bet: measured on the
archive, 13 of 14 multi-asset epochs traded the same direction and per-epoch
PnL correlated 0.3-0.6 across assets. So exposure is also capped per EPOCH,
and a second same-direction position in an epoch is scaled down by
``(1 - correlation)`` before the caps apply.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

#: Operator day boundary for daily realised PnL (no DST).
GMT9 = timezone(timedelta(hours=9), "GMT+9")
#: Rolling window for drawdown reference (balance 24h ago).
DRAWDOWN_LOOKBACK_S = 24 * 3600.0
#: Keep a little more than 24h of samples so the lookback always has a hit.
BALANCE_HISTORY_KEEP_S = 26 * 3600.0
#: Min gap between stored balance samples (live ticks are ~200ms).
BALANCE_SAMPLE_MIN_GAP_S = 30.0


def gmt9_day_id(now_s: float | None = None) -> int:
    """Calendar day id in GMT+9 as YYYYMMDD."""
    ts = time.time() if now_s is None else float(now_s)
    return int(datetime.fromtimestamp(ts, tz=GMT9).strftime("%Y%m%d"))


@dataclass(slots=True)
class RiskConfig:
    #: Sizing is deliberately SMALL rather than the caps being LOOSE.
    #: The epoch cap divided by the per-market cap is the real limit on how
    #: many windows we can be in at once: at 30/10 that was three. Halving the
    #: per-market stake makes it six, so the bot trades roughly twice as often
    #: without a single aggregate cap moving -- same money at risk, spread over
    #: more windows. That also feeds the evidence statistic, which is clustered
    #: per epoch and needs epochs, not dollars, to ever reach |t| ~ 2.
    kelly_fraction: float = 0.15           # was 0.25: smaller bets, more of them
    #: Multiply the Kelly stake by the model confidence in [0, 1].
    confidence_scaling: bool = True
    max_position_usdc: float = 5.0         # per market (was 10.0)
    max_asset_exposure_usdc: float = 15.0  # across a single asset's open markets
    max_epoch_exposure_usdc: float = 30.0  # all assets, same 300s window -- UNCHANGED
    max_total_exposure_usdc: float = 50.0  # UNCHANGED
    max_concurrent_positions: int = 10   # more windows open at once, each smaller
    max_daily_loss_usdc: float = 20.0
    max_drawdown_usdc: float = 30.0
    min_order_usdc: float = 2.0
    max_shares_per_order: float = 100.0
    #: Assumed correlation between same-direction bets in one epoch.
    epoch_correlation: float = 0.5
    #: Never take more than this fraction of the resting depth at the limit.
    max_depth_fraction: float = 0.5
    #: MICRO-LIVE: ignore Kelly and send the SMALLEST valid order every time
    #: (the venue's minimum shares and minimum notional). Never scales with
    #: the edge, the balance or recent results.
    micro_fixed_size: bool = False
    micro_min_shares: float = 5.0
    micro_min_notional_usdc: float = 1.0


@dataclass(slots=True)
class OpenPosition:
    slug: str
    asset: str
    epoch: int
    side: str          # "UP" | "DOWN"
    usdc: float
    shares: float


@dataclass(slots=True)
class RiskVerdict:
    ok: bool
    reason: str = ""
    detail: str = ""


@dataclass
class RiskManager:
    cfg: RiskConfig = field(default_factory=RiskConfig)
    starting_balance: float = 100.0
    open: dict[str, OpenPosition] = field(default_factory=dict)
    realised_total: float = 0.0
    realised_today: float = 0.0
    peak_equity: float = 0.0
    #: GMT+9 calendar day as YYYYMMDD; 0 = unset (first _roll_day assigns it).
    day: int = 0
    consecutive_losses: int = 0
    #: markets with an entry order working (resting or in flight): they count
    #: toward the concurrent-position cap, or four resting orders can all
    #: fill under a cap of one
    pending: set = field(default_factory=set)
    #: slug -> (asset, epoch, worst-case USDC) of each working entry: they count
    #: toward EXPOSURE as well, or several markets signalling in one tick each
    #: see the same free budget and together fill past it
    pending_usdc: dict = field(default_factory=dict)
    #: Optional marked account balance/equity from the live loop (cash + inventory).
    _marked_balance: float | None = field(default=None, repr=False)
    #: (unix_s, balance) samples for the rolling 24h drawdown reference.
    _balance_history: list = field(default_factory=list, repr=False)
    _last_balance_sample_ts: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self.peak_equity = self.starting_balance
        now = time.time()
        # Seed history with the initial bankroll so early drawdown has a floor.
        self._balance_history = [(now, float(self.starting_balance))]
        self._last_balance_sample_ts = now
        self._roll_day(now)

    # ---------------------------------------------------------- bookkeeping

    def note_balance(self, balance: float, now_s: float | None = None) -> None:
        """Push the account's current balance/equity for rolling 24h drawdown."""
        if balance is None:
            return
        now = time.time() if now_s is None else float(now_s)
        bal = float(balance)
        self._marked_balance = bal
        if (not self._balance_history
                or now - self._last_balance_sample_ts >= BALANCE_SAMPLE_MIN_GAP_S
                or now < self._last_balance_sample_ts):
            self._balance_history.append((now, bal))
            self._balance_history.sort(key=lambda x: x[0])
            self._last_balance_sample_ts = max(self._last_balance_sample_ts, now)
            cutoff = max(now, self._balance_history[-1][0]) - BALANCE_HISTORY_KEEP_S
            while len(self._balance_history) > 1 and self._balance_history[0][0] < cutoff:
                self._balance_history.pop(0)

    def balance_history_export(self) -> list[list[float]]:
        """Compact [[ts_s, balance], ...] for persistence."""
        return [[float(ts), float(bal)] for ts, bal in self._balance_history[-2048:]]

    def balance_history_restore(self, rows: list | None) -> None:
        if not rows:
            return
        hist: list[tuple[float, float]] = []
        for row in rows:
            try:
                hist.append((float(row[0]), float(row[1])))
            except (TypeError, ValueError, IndexError):
                continue
        if not hist:
            return
        hist.sort(key=lambda x: x[0])
        cutoff = time.time() - BALANCE_HISTORY_KEEP_S
        self._balance_history = [(t, b) for t, b in hist if t >= cutoff]
        if self._balance_history:
            self._last_balance_sample_ts = self._balance_history[-1][0]

    def _roll_day(self, now_s: float) -> None:
        d = gmt9_day_id(now_s)
        if d == self.day:
            return
        self.day = d
        self.realised_today = 0.0

    def on_submit(self, slug: str, usdc: float = 0.0, asset: str = "", epoch: int = 0) -> None:
        """An entry order is working. ``usdc`` is its worst case (limit x size)."""
        self.pending.add(slug)
        if usdc > 0.0:
            self.pending_usdc[slug] = (asset, epoch, usdc)

    def on_done(self, slug: str) -> None:
        """The entry order finished (filled, killed, expired or cancelled)."""
        self.pending.discard(slug)
        self.pending_usdc.pop(slug, None)

    def on_fill(self, slug: str, asset: str, epoch: int, side: str, usdc: float, shares: float,
                ends_entry: bool = True) -> None:
        """``ends_entry`` False: a fill of an order that never held the market's working-entry slot
        (triangle's resting pair order), which must not release an entry still working there."""
        if ends_entry:
            self.pending.discard(slug)
            self.pending_usdc.pop(slug, None)
        pos = self.open.get(slug)
        if pos is None:
            self.open[slug] = OpenPosition(slug, asset, epoch, side, usdc, shares)
        else:
            pos.usdc += usdc
            pos.shares += shares

    def on_correction(self, pnl: float, now_s: float | None = None, same_day: bool = True) -> None:
        """A late correction to an already-booked window (its real outcome replacing a mark): the
        realised totals move, nothing else -- no position closes, the loss streak is untouched.
        ``same_day`` False: the window belonged to an earlier day, so today's figure is left alone."""
        self._roll_day(time.time() if now_s is None else now_s)
        self.realised_total += pnl
        if same_day:
            self.realised_today += pnl
        self.peak_equity = max(self.peak_equity, self.starting_balance + self.realised_total)

    def on_settle(self, slug: str, pnl: float, now_s: float | None = None) -> None:
        self._roll_day(time.time() if now_s is None else now_s)
        self.open.pop(slug, None)
        self.realised_total += pnl
        self.realised_today += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        eq = self.starting_balance + self.realised_total
        self.peak_equity = max(self.peak_equity, eq)

    # ------------------------------------------------------------- exposure

    def _working(self):
        """Working entries not yet on the book as positions: (asset, epoch, usdc)."""
        return [v for s, v in self.pending_usdc.items() if s in self.pending and s not in self.open]

    def exposure_total(self) -> float:
        return sum(p.usdc for p in self.open.values()) + sum(v[2] for v in self._working())

    def exposure_asset(self, asset: str) -> float:
        return (sum(p.usdc for p in self.open.values() if p.asset == asset)
                + sum(v[2] for v in self._working() if v[0] == asset))

    def exposure_epoch(self, epoch: int) -> float:
        return (sum(p.usdc for p in self.open.values() if p.epoch == epoch)
                + sum(v[2] for v in self._working() if v[1] == epoch))

    def capacity_total(self) -> float:
        """USDC still free under the global exposure cap (open + working entries)."""
        return max(0.0, self.cfg.max_total_exposure_usdc - self.exposure_total())

    def same_direction_in_epoch(self, epoch: int, side: str, asset: str) -> float:
        return sum(p.usdc for p in self.open.values()
                   if p.epoch == epoch and p.side == side and p.asset != asset)

    @property
    def current_equity(self) -> float:
        """Bankroll after realised PnL (open inventory is still in exposure, not MTM)."""
        return self.starting_balance + self.realised_total

    @property
    def current_balance(self) -> float:
        """Marked account balance when the live loop supplies it; else realised equity."""
        if self._marked_balance is not None:
            return float(self._marked_balance)
        return self.current_equity

    def balance_24h_ago(self, now_s: float | None = None) -> float:
        """Balance recorded ~24 hours ago; initial bankroll if history is shorter."""
        now = time.time() if now_s is None else float(now_s)
        target = now - DRAWDOWN_LOOKBACK_S
        ref: float | None = None
        for ts, bal in self._balance_history:
            if ts <= target + 1e-9:
                ref = float(bal)
            else:
                break
        if ref is not None:
            return ref
        return float(self.starting_balance)

    @property
    def drawdown_reference(self) -> float:
        """Balance from 24 hours ago (or initial balance before 24h of samples)."""
        return self.balance_24h_ago()

    @property
    def drawdown(self) -> float:
        """``balance_24h_ago − current_balance``.

        Positive when below the 24h-ago balance; negative when ahead.
        Until 24h of samples exist, the reference is the initial bankroll.
        """
        return self.drawdown_reference - self.current_balance

    # ---------------------------------------------------------------- gates

    def halted(self, now_s: float | None = None) -> RiskVerdict:
        now = time.time() if now_s is None else float(now_s)
        self._roll_day(now)
        if self.cfg.max_daily_loss_usdc > 0 and self.realised_today <= -self.cfg.max_daily_loss_usdc:
            return RiskVerdict(False, "RISK_LIMIT", f"daily loss {self.realised_today:+.2f}")
        dd = self.balance_24h_ago(now) - self.current_balance
        if self.cfg.max_drawdown_usdc > 0 and dd >= self.cfg.max_drawdown_usdc:
            return RiskVerdict(False, "RISK_LIMIT", f"drawdown {dd:.2f}")
        return RiskVerdict(True)

    def check(self, slug: str, asset: str, epoch: int, stake: float, add_to_open: bool = False) -> RiskVerdict:
        """May a new entry of ``stake`` USDC go out in ``slug``? ``add_to_open`` lets an engine
        that buys a market in several clips (strategy/fastclip.py) add to a position it already
        holds there; every other cap still applies, and a market already open does not count
        as a new position. Off by default: one position per market."""
        h = self.halted()
        if not h.ok:
            return h
        adding = add_to_open and slug in self.open
        if slug in self.open and not adding:
            return RiskVerdict(False, "ALREADY_POSITIONED", slug)
        working = len(self.open) + len(self.pending - set(self.open) - {slug})
        if not adding and working >= self.cfg.max_concurrent_positions:
            return RiskVerdict(False, "RISK_LIMIT", f"{len(self.open)} positions open, {len(self.pending)} orders working")
        if stake > self.cfg.max_position_usdc + 1e-9:
            return RiskVerdict(False, "RISK_LIMIT", "position cap")
        if self.exposure_asset(asset) + stake > self.cfg.max_asset_exposure_usdc + 1e-9:
            return RiskVerdict(False, "RISK_LIMIT", f"{asset} exposure cap")
        if self.exposure_epoch(epoch) + stake > self.cfg.max_epoch_exposure_usdc + 1e-9:
            return RiskVerdict(False, "RISK_LIMIT", "epoch (correlated) exposure cap")
        if self.exposure_total() + stake > self.cfg.max_total_exposure_usdc + 1e-9:
            return RiskVerdict(False, "RISK_LIMIT", "total exposure cap")
        return RiskVerdict(True)

    # --------------------------------------------------------------- sizing

    def size(self, p: float, price: float, confidence: float, balance: float,
             asset: str, epoch: int, side: str, available_shares: float,
             min_size: float = 5.0) -> tuple[float, str]:
        """Shares to buy, and why it came out that way. 0 means no trade."""
        c = self.cfg
        if price <= 0.0 or price >= 1.0 or balance <= 0.0:
            return 0.0, "bad price"
        f_star = (p - price) / (1.0 - price)
        if f_star <= 0.0:
            return 0.0, "no kelly edge"
        if c.micro_fixed_size:
            return self._smallest_valid(price, balance, asset, epoch, available_shares, min_size)
        conf = min(max(confidence, 0.0), 1.0) if c.confidence_scaling else 1.0
        stake = balance * c.kelly_fraction * f_star * conf
        # a same-direction bet already on in this epoch is the same bet
        if self.same_direction_in_epoch(epoch, side, asset) > 0.0:
            stake *= (1.0 - c.epoch_correlation)
        caps = {
            "position cap": c.max_position_usdc,
            "asset cap": c.max_asset_exposure_usdc - self.exposure_asset(asset),
            "epoch cap": c.max_epoch_exposure_usdc - self.exposure_epoch(epoch),
            "total cap": c.max_total_exposure_usdc - self.exposure_total(),
            "balance": balance,
        }
        binding = "kelly"
        for name, cap in caps.items():
            if cap < stake:
                stake, binding = cap, name
        if stake < c.min_order_usdc:
            return 0.0, f"below min order ({binding})"
        shares = min(stake / price, c.max_shares_per_order, available_shares * c.max_depth_fraction)
        shares = math.floor(shares * 100.0) / 100.0
        if shares < min_size:
            return 0.0, f"below venue min size ({binding})"
        return shares, binding

    def _smallest_valid(self, price: float, balance: float, asset: str, epoch: int,
                        available_shares: float, min_size: float) -> tuple[float, str]:
        """The venue's minimum order at this price, if the caps allow it."""
        c = self.cfg
        shares = max(min_size, c.micro_min_shares, math.ceil(c.micro_min_notional_usdc / price * 100.0) / 100.0)
        shares = math.ceil(shares * 100.0) / 100.0
        stake = shares * price
        if shares > available_shares * c.max_depth_fraction:
            return 0.0, "micro: not enough depth for the minimum order"
        caps = {
            "position cap": c.max_position_usdc,
            "asset cap": c.max_asset_exposure_usdc - self.exposure_asset(asset),
            "epoch cap": c.max_epoch_exposure_usdc - self.exposure_epoch(epoch),
            "total cap": c.max_total_exposure_usdc - self.exposure_total(),
            "balance": balance,
        }
        for name, cap in caps.items():
            if stake > cap + 1e-9:
                return 0.0, f"micro: minimum order {stake:.2f} over {name}"
        return shares, "micro minimum"

    def snapshot(self) -> dict:
        return {
            "open_positions": len(self.open),
            "orders_working": len(self.pending),
            "exposure_total": round(self.exposure_total(), 2),
            "exposure_working": round(sum(v[2] for v in self._working()), 2),
            "max_total_exposure": self.cfg.max_total_exposure_usdc,
            "max_concurrent_positions": self.cfg.max_concurrent_positions,
            "exposure_by_asset": {a: round(self.exposure_asset(a), 2)
                                  for a in sorted({p.asset for p in self.open.values()})},
            "realised_today": round(self.realised_today, 2),
            "drawdown": round(self.drawdown, 2),
            "drawdown_reference": round(self.drawdown_reference, 2),
            "balance_24h_ago": round(self.balance_24h_ago(), 2),
            "day_gmt9": self.day,
            "peak_equity": round(self.peak_equity, 2),
            "consecutive_losses": self.consecutive_losses,
            "halted": (lambda v: None if v.ok else v.detail)(self.halted()),
        }
