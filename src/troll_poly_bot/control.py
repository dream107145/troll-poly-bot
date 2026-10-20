"""Tunables a running bot re-reads from disk, so the dashboard can steer it.

Why a file
----------
The dashboard runs in its own process and may not even be the parent of the
bot (you can start one from a terminal and still see it on the page). A small
JSON file in ``data/`` is the one channel both ends always share, it survives
a dashboard restart, and it is inspectable and editable by hand when the page
is not running.

What can change while the bot runs
---------------------------------
Only the values in :data:`TUNABLES`: the spot-lag strategy's own settings
(``spot_lag.*``), the shared engine and risk limits, and in real money the
venue adapter's caps (``caps.*``) and the micro-live ceilings (``micro.*``)
that everything else is clamped under. Each one is clamped to a range that
cannot wedge the bot, and anything unknown or unparseable is ignored with a
message rather than applied. Things that decide what the process *is* --
paper versus real money, the bankroll, which assets, which spot exchanges --
are start-time arguments, not tunables, and are marked ``restart=True`` so
the page can say so instead of pretending a change took effect.

Scoping
-------
Running a paper bot and a real-money bot side by side out of one directory is
normal here, and they must be steerable apart. The file therefore holds three
sections::

    {"all": {...}, "paper": {...}, "live": {...}}

A bot applies ``all`` first, then its own section, so ``live`` overrides
``all`` for the real-money process only.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTROL_PATH = Path("data/controls.json")
KILL_PATH = Path("data/KILL")
SCOPES = ("all", "paper", "live")


@dataclass(frozen=True, slots=True)
class Tunable:
    """One settable value: where it lives, what it may be, how to say it."""
    key: str                 # "engine.min_net_edge" -- group.attribute
    kind: str                # float | int | bool | choice | list
    lo: float
    hi: float
    label: str
    group: str               # UI grouping
    unit: str = ""
    help: str = ""
    #: ``live`` means the value only exists on a real-money exchange.
    applies_to: str = "any"
    step: float = 0.01
    #: ``choice`` only: the allowed values, in the spelling the bot uses
    options: tuple[str, ...] = ()
    #: strategies that never read this value; the page greys it out for them
    not_for: tuple[str, ...] = ()
    #: raising it raises a real-money limit, so the page asks before saving
    ceiling: bool = False

    @property
    def target(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def attr(self) -> str:
        return self.key.split(".", 1)[1]

    def coerce(self, value: Any) -> tuple[Any | None, str | None]:
        """(clean value, error). Clamps silently; rejects nonsense loudly."""
        if self.kind == "bool":
            if isinstance(value, bool):
                return value, None
            if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "on", "off"):
                return value.lower() in ("true", "1", "on"), None
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value), None
            return None, f"{self.key}: expected true or false, got {value!r}"
        if self.kind == "choice":
            text = str(value).strip().lower()
            for opt in self.options:
                if text == opt.lower():
                    return opt, None
            return None, f"{self.key}: expected one of {', '.join(self.options)}, got {value!r}"
        if self.kind == "list":
            # a comma list of symbols (assets). "none" or "-" is the empty list, which
            # is a setting in its own right ("exclude nothing"), unlike an empty field
            if not isinstance(value, str):
                return None, f"{self.key}: expected a comma list, got {value!r}"
            text = value.strip()
            if text.lower() in ("none", "-"):
                text = ""
            clean = ",".join(x.strip().upper() for x in text.split(",") if x.strip())
            if len(clean) > 200 or not _LIST_RE.fullmatch(clean):
                return None, f"{self.key}: letters, digits and commas only, got {value!r}"
            return clean, None
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None, f"{self.key}: expected a number, got {value!r}"
        if num != num or num in (float("inf"), float("-inf")):
            return None, f"{self.key}: not a finite number"
        num = min(max(num, self.lo), self.hi)
        return (int(round(num)), None) if self.kind == "int" else (num, None)


_LIST_RE = re.compile(r"[A-Z0-9._,-]*")

#: the spot-lag family: engine settings below marked with this do nothing for them
_SPOT = ("spot_lag", "repriced_momentum")


def _t(key, kind, lo, hi, label, group, **kw) -> Tunable:
    return Tunable(key=key, kind=kind, lo=lo, hi=hi, label=label, group=group, **kw)


#: Everything the dashboard may change on a running bot.
TUNABLES: dict[str, Tunable] = {t.key: t for t in (
    # ==== CHEAPSIDE (strategy/cheapside.py; cheapside.* reaches only that bot) ====
    _t("cheapside.portfolio_basket_enabled", "bool", 0, 1, "Seven-market basket on", "Cheapside basket",
       help="On: buy the six cheapest outcomes and the leader in the remaining market when their average ask is below the trigger."),
    _t("cheapside.portfolio_basket_market_count", "int", 2, 20, "Markets in a basket", "Cheapside basket", step=1,
       help="Must match the number of watched assets for a complete basket."),
    _t("cheapside.portfolio_basket_buy_count", "int", 1, 20, "Cheapest outcomes to buy", "Cheapside basket", step=1,
       help="Buy this many cheapest outcomes, then buy the leader in the remaining market. The count must be one below Markets in a basket."),
    _t("cheapside.portfolio_basket_max_average_ask", "float", 0.01, 0.99, "Cheap-token average trigger", "Cheapside basket",
       unit="USDC", step=0.01, help="Start a basket when the average ask of the selected cheapest outcomes is below this value."),
    _t("cheapside.portfolio_basket_shares", "float", 5.0, 1000.0, "Shares per cheap outcome", "Cheapside basket",
       unit="shares", step=1, help="Target shares for each of the cheap legs; the venue minimum is five."),
    _t("cheapside.portfolio_basket_target_pnl_usdc", "float", 0.0, 50.0, "Protected settlement target", "Cheapside basket",
       unit="USDC", step=0.01, help="Target PnL when all six cheap legs lose and the favourite wins."),
    _t("cheapside.portfolio_basket_min_leader_ask", "float", 0.01, 0.99, "Favourite ask at least", "Cheapside basket gates",
       unit="USDC", step=0.01, help="Do not arm when the favourite ask is below this (default 0.55)."),
    _t("cheapside.portfolio_basket_max_leader_ask", "float", 0.01, 0.99, "Favourite ask at most (arm)", "Cheapside basket gates",
       unit="USDC", step=0.01, help="Do not arm when the favourite ask is above this (default 0.78)."),
    _t("cheapside.portfolio_basket_abort_leader_ask", "float", 0.01, 0.99, "Favourite abort ask", "Cheapside basket gates",
       unit="USDC", step=0.01, help="Stop new cheap-leg buys if the favourite ask reaches this (default 0.82)."),
    _t("cheapside.portfolio_basket_max_leader_unit_cost", "float", 0.50, 0.99, "Favourite max all-in unit", "Cheapside basket gates",
       unit="USDC", step=0.01, help="Abort/reject when limit+fee unit cost exceeds this (default 0.81; covers ask 0.78+1c+fee)."),
    _t("cheapside.portfolio_basket_arm_min_time_remaining_s", "float", 0.0, 300.0, "Arm / cheap-leg cutoff", "Cheapside basket gates",
       unit="s left", step=1, help="Minimum seconds remaining to arm or start a new cheap-leg purchase (default 120)."),
    _t("cheapside.portfolio_basket_max_quote_skew_ms", "float", 0.0, 5000.0, "Max quote skew", "Cheapside basket gates",
       unit="ms", step=50, help="Reject when cross-market quote timestamps differ by more than this."),
    _t("cheapside.portfolio_basket_cash_buffer_usdc", "float", 0.0, 100.0, "Cash safety buffer", "Cheapside basket risk",
       unit="USDC", step=0.5, help="Keep this much cash uncommitted after planned basket spend."),
    _t("cheapside.portfolio_basket_max_cash_utilization", "float", 0.1, 1.0, "Max cash utilization", "Cheapside basket risk",
       unit="frac", step=0.01, help="Fraction of available cash that may be reserved for a basket."),
    _t("cheapside.portfolio_basket_max_notional_usdc", "float", 0.0, 10000.0, "Max basket notional", "Cheapside basket risk",
       unit="USDC", step=1, help="Reject before entry if cheap+leader notional exceeds this (0 disables)."),
    _t("cheapside.portfolio_basket_max_leader_notional_usdc", "float", 0.0, 10000.0, "Max leader notional", "Cheapside basket risk",
       unit="USDC", step=1, help="Reject if required favourite notional exceeds this (0 disables)."),
    _t("cheapside.portfolio_basket_max_leader_multiple", "float", 0.0, 100.0, "Max leader/cheap multiple", "Cheapside basket risk",
       unit="x", step=0.5, help="Reject if leader notional / cheap cost exceeds this (0 disables)."),
    _t("cheapside.portfolio_basket_max_all_lose_usdc", "float", 0.0, 10000.0, "Max all-seven-lose loss", "Cheapside basket risk",
       unit="USDC", step=1, help="Reject if |all-lose PnL| exceeds this (0 disables)."),
    _t("cheapside.portfolio_basket_max_orphan_usdc", "float", 0.0, 10000.0, "Max orphan exposure", "Cheapside basket risk",
       unit="USDC", step=1, help="Alert threshold for unhedged filled cost after abort (0 disables)."),
    _t("cheapside.portfolio_basket_max_concurrent_exposure_usdc", "float", 0.0, 100000.0, "Max concurrent exposure", "Cheapside basket risk",
       unit="USDC", step=1, help="Hard cap applied on the order path (0 disables)."),
    _t("cheapside.portfolio_basket_cheap_retry_ms", "float", 50.0, 5000.0, "Cheap-leg retry interval", "Cheapside basket",
       unit="ms", step=50, help="Delay between FAK retries on an unfinished cheap leg."),
    _t("cheapside.portfolio_min_time_remaining_s", "float", 0.0, 300.0, "Leader recovery cutoff", "Cheapside basket",
       unit="s left", step=1, help="Favourite recovery orders stop below this many seconds remaining."),
    _t("cheapside.portfolio_repair_target_usdc", "float", 0.0, 500.0, "Repair P&L target", "Cheapside recovery",
       unit="USDC", step=0.1, help="Target for the separate portfolio leader-repair rule when basket mode is off."),
    _t("cheapside.portfolio_repair_cooldown_s", "float", 0.2, 60.0, "Repair retry delay", "Cheapside recovery",
       unit="s", step=0.1, help="Minimum delay between portfolio repair order attempts."),
    _t("cheapside.require_support", "bool", 0, 1, "Require spot support", "Cheapside individual entries",
       help="Require spot movement and short-horizon agreement to support buying the cheap side."),
    _t("cheapside.force_one_per_market", "bool", 0, 1, "Force one cheap-side entry", "Cheapside individual entries",
       help="Use a bounded paper fallback when the normal model gates fail."),
    _t("cheapside.leader_buy_enabled", "bool", 0, 1, "Individual leader buys on", "Cheapside individual entries",
       help="Enable the separate model-confirmed favourite entry rule."),
    _t("cheapside.leader_min_win_probability", "float", 0.50, 0.99, "Leader minimum win probability", "Cheapside individual entries",
       unit="prob", step=0.01, help="Model probability required by the separate individual leader entry rule."),
    _t("cheapside.leader_min_ask", "float", 0.01, 0.99, "Individual leader ask at least", "Cheapside individual entries",
       unit="USDC", step=0.01, help="Lower price bound for the separate individual leader entry rule."),
    _t("cheapside.leader_max_ask", "float", 0.01, 0.99, "Individual leader ask at most", "Cheapside individual entries",
       unit="USDC", step=0.01, help="Upper price bound for the separate individual leader entry rule."),

    # ==== CROSS_MARKET_ARB (strategy/cross_market_arb/; TPB_CMA_* at start): cma.* reaches only that bot ====
    _t("cma.entry_mode", "choice", 0, 1, "Entry mode", "Cross-market arb",
       options=("maker", "taker", "both"),
       help="maker: GTC pair bids when UP+DOWN sum clears the floor (live path). taker: FAK at asks (rare). both: try maker then taker."),
    _t("cma.maximum_maker_sum", "float", 0.50, 1.00, "Max maker UP+DOWN sum", "Cross-market arb",
       unit="USDC", step=0.01,
       help="Arm maker pair bids only when UP bid + DOWN bid is at or below this (0.97 locks $0.15 on 5 shares)."),
    _t("cma.minimum_net_profit", "float", 0.0, 50.0, "Min guaranteed net profit", "Cross-market arb",
       unit="USDC", step=0.01, help="Reject unless min settlement payout minus all-in cost clears this (aligned with max maker sum × shares)."),
    _t("cma.maker_shares", "float", 5.0, 200.0, "Maker shares per side", "Cross-market arb",
       unit="shares", step=1.0, help="GTC bid size on each side of a maker pair (venue minimum 5)."),
    _t("cma.max_maker_markets", "int", 1, 14, "Max maker markets armed", "Cross-market arb",
       step=1, help="How many markets may hold resting maker pair bids at once."),
    _t("cma.maker_improve_min_spread", "float", 0.01, 0.50, "Improve bid when spread ≥", "Cross-market arb",
       unit="USDC", step=0.01, help="When the book spread is at least this wide, post one tick above the best bid."),
    _t("cma.maker_cancel_time_remaining_s", "float", 0.0, 300.0, "Cancel maker pairs below", "Cross-market arb",
       unit="s left", step=1.0, help="Pull resting maker pairs once fewer than this many seconds remain."),
    _t("cma.markets", "list", 0, 1, "Markets (assets)", "Cross-market arb",
       help="Comma list of 5m UP/DOWN assets to watch (e.g. BTC,ETH,SOL,XRP,DOGE,BNB,HYPE)."),
    _t("cma.exclude_assets", "list", 0, 1, "Never these assets", "Cross-market arb",
       help="Comma list of assets never traded (Market page skip chips). Type none to exclude nothing."),
    _t("cma.maximum_basket_notional", "float", 0.0, 10000.0, "Max basket notional", "Cross-market arb",
       unit="USDC", step=1.0, help="Hard cap on planned acquisition cost for one opportunity."),
    _t("cma.maximum_market_exposure", "float", 0.0, 10000.0, "Max exposure per market", "Cross-market arb",
       unit="USDC", step=1.0, help="Max notional allocated to any single market's legs."),
    _t("cma.max_shares_per_leg", "float", 5.0, 500.0, "Max shares per leg", "Cross-market arb",
       unit="shares", step=1.0, help="Optimizer / taker path size cap per market side."),
    _t("cma.share_step", "float", 1.0, 50.0, "Share step", "Cross-market arb",
       unit="shares", step=1.0, help="Share sizing granularity for the optimizer."),
    _t("cma.maximum_quote_age_ms", "float", 0.0, 5000.0, "Max quote age", "Cross-market arb gates",
       unit="ms", step=50.0, help="Reject when any required book quote is older than this (default 1000)."),
    _t("cma.maximum_timestamp_skew_ms", "float", 0.0, 5000.0, "Max cross-market skew", "Cross-market arb gates",
       unit="ms", step=50.0, help="Reject when quote ages/timestamps across markets differ by more than this."),
    _t("cma.maximum_spread", "float", 0.0, 0.50, "Max spread", "Cross-market arb gates",
       unit="USDC", step=0.01, help="Reject a market whose UP or DOWN spread exceeds this."),
    _t("cma.maximum_slippage", "float", 0.0, 0.20, "Max slippage / limit offset", "Cross-market arb gates",
       unit="USDC", step=0.005, help="Limit may sit this far above the visible ask; also used in cost model."),
    _t("cma.min_time_remaining_s", "float", 0.0, 300.0, "Min time remaining", "Cross-market arb gates",
       unit="s left", step=1.0, help="Do not arm when any market has fewer seconds left than this plus the execution buffer."),
    _t("cma.max_time_remaining_s", "float", 0.0, 300.0, "Max time remaining", "Cross-market arb gates",
       unit="s left", step=1.0, help="Do not arm maker pairs when more than this many seconds remain (skip stale early book)."),
    _t("cma.execution_buffer_s", "float", 0.0, 120.0, "Execution buffer", "Cross-market arb gates",
       unit="s", step=1.0, help="Extra seconds before close reserved for cancel / reconcile."),
    _t("cma.maximum_incomplete_exposure", "float", 0.0, 10000.0, "Max incomplete exposure", "Cross-market arb risk",
       unit="USDC", step=1.0, help="Abort when filled-only worst-case loss exceeds this."),
    _t("cma.daily_loss_limit", "float", 0.0, 10000.0, "Daily loss circuit breaker", "Cross-market arb risk",
       unit="USDC", step=1.0, help="Halt new opportunities once realised losses today reach this."),
    _t("cma.minimum_cash_reserve", "float", 0.0, 10000.0, "Minimum cash reserve", "Cross-market arb risk",
       unit="USDC", step=0.5, help="Cash that must remain uncommitted after reservations."),
    _t("cma.maximum_capital_per_opportunity", "float", 0.0, 10000.0, "Max capital per opportunity", "Cross-market arb risk",
       unit="USDC", step=1.0, help="Tighter of this and max basket notional binds on the scan."),
    _t("cma.maximum_total_outstanding_notional", "float", 0.0, 100000.0, "Max outstanding notional", "Cross-market arb risk",
       unit="USDC", step=1.0, help="Sum of reserved / remaining plan notionals across concurrent plans."),
    _t("cma.maximum_concurrent_opportunities", "int", 1, 14, "Max concurrent opportunities", "Cross-market arb risk",
       step=1, help="How many incomplete portfolios may be armed at once."),
    _t("cma.maximum_worst_case_loss_during_execution", "float", 0.0, 10000.0, "Max worst-case exec loss", "Cross-market arb risk",
       unit="USDC", step=1.0, help="Reject if reserved capital (full loss if nothing settles) exceeds this."),
    _t("cma.residual_exposure_policy", "choice", 0, 1, "Residual exposure policy", "Cross-market arb",
       options=("hold", "alert_only"),
       help="When partial fills destroy the guarantee: hold inventory (default) or alert only. Never auto-dumps."),
    _t("cma.leg_retry_ms", "float", 50.0, 5000.0, "Leg retry interval", "Cross-market arb",
       unit="ms", step=50.0, help="Delay before retrying an unfinished taker leg."),
    _t("cma.revalidate_before_submit", "bool", 0, 1, "Revalidate before submit", "Cross-market arb",
       help="Re-check the opportunity immediately before each order submission."),
    _t("cma.hold_to_settlement", "bool", 0, 1, "Hold to settlement", "Cross-market arb",
       help="Keep filled inventory through settlement (no early exit)."),

    # ==== CROSS_MARKET_CORRELATION (strategy/cross_market_correlation/; TPB_CMC_*): cmc.* reaches only that bot ====
    _t("cmc.exclude_assets", "list", 0, 1, "Never these assets", "Cross-market correlation",
       help="Comma list of assets never traded (Market page skip chips). Type none to exclude nothing."),
    _t("cmc.lower_side_only_assets", "list", 0, 1, "Lower-side only assets", "Cross-market correlation",
       help="Comma list of assets that may only be the cheaper leg of a pair (Market page chips). "
            "Type none to clear."),
    _t("cmc.skip_pairs_mode_enabled", "bool", 0, 1, "Skip high-wipe pairs", "Cross-market correlation",
       help="When on, skip opposite-pair directions the wipe-risk model flags as likely full losses "
            "(Market page Skip pairs mode). Uses spot/strike geometry + book alignment."),
    _t("cmc.wipe_skip_threshold", "float", 0.0, 1.0, "Wipe-skip probability", "Cross-market correlation",
       unit="prob", step=0.01,
       help="Skip a pair direction when calibrated p_wipe is at least this (default 0.55). "
            "Only applies when Skip pairs mode is on."),
    _t("cmc.minimum_combined_cost", "float", 0.0, 1.0, "Min combined leg cost", "Cross-market correlation",
       unit="USDC", step=0.01,
       help="Reject a pair when the two legs' unit costs sum to less than this (default 0.60)."),
    _t("cmc.maximum_combined_cost", "float", 0.5, 1.5, "Max combined leg cost", "Cross-market correlation",
       unit="USDC", step=0.01,
       help="Reject a pair when the two legs' unit costs sum to more than this."),
    _t("cmc.max_shares_per_market_side", "float", 0.0, 200.0, "Max shares per market side", "Cross-market correlation",
       unit="shares", step=1.0,
       help="Cap filled shares of one (market, UP|DOWN) in the epoch (stops stacking the same leg)."),
    _t("cmc.max_net_shares_per_asset", "float", 0.0, 200.0, "Max net shares per asset", "Cross-market correlation",
       unit="shares", step=1.0,
       help="Cap |UP−DOWN| net shares per asset; hedges that reduce the net are still allowed."),
    _t("cmc.leader_large_min_price", "float", 0.5, 0.99, "Leader ask for large size", "Cross-market correlation",
       unit="USDC", step=0.01,
       help="When the higher ask of the two legs is above this, both legs use leader_large_shares."),
    _t("cmc.leader_large_shares", "float", 5.0, 50.0, "Shares when leader is expensive", "Cross-market correlation",
       unit="shares", step=1.0,
       help="Both-leg size when the leader ask clears leader_large_min_price (default 10)."),
    _t("cmc.leader_block_max_price", "float", 0.0, 0.95, "Block if leader ask under", "Cross-market correlation",
       unit="USDC", step=0.01,
       help="With a downtrend on the leader token, reject the pair when its ask is under this (default 0.60)."),
    _t("cmc.size_shares", "float", 5.0, 50.0, "Default shares per leg", "Cross-market correlation",
       unit="shares", step=1.0, help="Base size for both legs when the leader is not above the large-size threshold."),
    _t("cmc.kill_switch", "bool", 0, 1, "Kill switch (stop new entries)", "Cross-market correlation",
       help="When on, the bot stops arming new pair trades. Does not enable live trading."),
    _t("cmc.guaranteed_mode_enabled", "bool", 0, 1, "Guaranteed-arbitrage mode", "Cross-market correlation",
       help="Allow GUARANTEED_ARBITRAGE only when min(P_s-C)>floor over every settlement scenario."),
    _t("cmc.statistical_mode_enabled", "bool", 0, 1, "Statistical correlation mode", "Cross-market correlation",
       help="OFF by default. Enable only after out-of-sample validation. Never implies risk-free."),
    _t("cmc.minimum_conservative_ev", "float", 0.0, 5.0, "Min conservative EV", "Cross-market correlation",
       unit="USDC", step=0.01, help="Reject statistical candidates unless EV minus uncertainty haircut clears this."),
    _t("cmc.guaranteed_min_net_profit", "float", 0.0, 5.0, "Min guaranteed net profit", "Cross-market correlation",
       unit="USDC", step=0.01, help="Worst-case payout minus all-in cost must exceed this for the guaranteed label."),
    _t("cmc.min_sample_size", "int", 1, 5000, "Min synchronized samples", "Cross-market correlation model",
       step=1, help="Pairs with fewer historical synchronized settlements are unqualified."),
    _t("cmc.max_probability_uncertainty", "float", 0.0, 1.0, "Max model uncertainty", "Cross-market correlation model",
       unit="prob", step=0.01, help="Reject when disagreement-rate uncertainty exceeds this."),
    _t("cmc.maximum_capital_per_pair", "float", 0.0, 10000.0, "Max capital per pair", "Cross-market correlation risk",
       unit="USDC", step=1.0, help="Hard cap on reserved cash for one matched pair."),
    _t("cmc.maximum_total_open_exposure", "float", 0.0, 100000.0, "Max total open exposure", "Cross-market correlation risk",
       unit="USDC", step=1.0, help="Sum of reserved / remaining plan notionals."),
    _t("cmc.maximum_unhedged_exposure", "float", 0.0, 10000.0, "Max unhedged exposure", "Cross-market correlation risk",
       unit="USDC", step=1.0, help="Abort when one-leg residual notional exceeds this."),
    _t("cmc.maximum_daily_realized_loss", "float", 0.0, 10000.0, "Daily loss circuit breaker", "Cross-market correlation risk",
       unit="USDC", step=1.0, help="Halt new pairs once realised losses today reach this."),
    _t("cmc.minimum_book_depth", "float", 0.0, 500.0, "Min book depth", "Cross-market correlation gates",
       unit="shares", step=1.0, help="Reject legs whose executable ask depth is below this."),
    _t("cmc.maximum_quote_age_ms", "float", 0.0, 5000.0, "Max quote age", "Cross-market correlation gates",
       unit="ms", step=50.0, help="Reject when any required book quote is older than this."),
    _t("cmc.min_time_remaining_s", "float", 0.0, 300.0, "Min time remaining", "Cross-market correlation gates",
       unit="s left", step=1.0, help="Do not arm when either market is closer to settlement than this plus the buffer."),
    _t("cmc.unhedged_policy", "choice", 0, 1, "Unhedged residual policy", "Cross-market correlation",
       options=("cancel_alert", "hold", "hedge_if_edge"),
       help="When only one leg fills: alert and stop (default), hold, or attempt hedge only if armed limit still has edge."),

    # ==== LEADER_PAIR (strategy/leader_pair/, cfg.leader_pair; TPB_LP_* at start): lp.* reaches only a leader_pair bot ====
    _t("lp.init_shares", "float", 5.0, 200.0, "Rule 1: initial leader shares", "Leader-pair rule 1", unit="shares", step=1.0,
       help="Shares of the initial leader bought init_delay_s after the open (the spec: 10)."),
    _t("lp.leader_rule", "choice", 0, 1, "Rule 1: leader by", "Leader-pair rule 1", options=("mid", "ask", "bid"),
       help="Which price picks the leader at the entry moment: the token with the higher mid, ask or bid."),
    _t("lp.init_delay_s", "float", 0.0, 120.0, "Rule 1: entry delay after the open", "Leader-pair rule 1", unit="s", step=1.0,
       help="Rule 1: entry delay after the open. In s."),
    _t("lp.init_max_s", "float", 5.0, 290.0, "Rule 1: no entry later than", "Leader-pair rule 1", unit="s", step=1.0,
       help="Seconds after the open past which an unfilled initial entry is given up (the market is skipped)."),
    _t("lp.init_min_ask", "float", 0.05, 0.95, "Rule 1: leader ask at least", "Leader-pair rule 1", unit="USDC", step=0.01,
       help="Rule 1: leader ask at least. In USDC."),
    _t("lp.init_max_ask", "float", 0.05, 0.99, "Rule 1: leader ask at most", "Leader-pair rule 1", unit="USDC", step=0.01,
       help="Rule 1: leader ask at most. In USDC."),
    _t("lp.pair_threshold", "float", 0.50, 1.00, "Rule 2: pair qualifies at a cost of at most", "Leader-pair pairs", unit="USDC",
       step=0.01, help="Fee-inclusive cost of one Up share plus one Down share (FIFO lots); the pair buy below uses it too."),
    _t("lp.pair_buy_enabled", "bool", 0, 1, "Pair buy on", "Leader-pair pairs",
       help="The user 10-14: when the token opposite the unmatched shares can be bought so the pairs cost at most the threshold, the completing amount is bought."),
    _t("lp.pair_buy_cooldown_s", "float", 0.0, 60.0, "Between pair buys", "Leader-pair pairs", unit="s", step=0.5,
       help="Seconds between two pair buys in one market."),
    _t("lp.lock_target_usdc", "float", 0.0, 500.0, "Lock the market once the worst outcome reaches", "Leader-pair pairs", unit="USDC", step=0.5,
       help="The user 10-14: once min(PnL if UP, PnL if DOWN) is at least this (5), the market is LOCKED and buys nothing more. 0 = off."),
    _t("lp.accum_interval_s", "float", 1.0, 60.0, "Rule 3: accumulation interval", "Leader-pair accumulation", unit="s", step=1.0,
       help="One decision per slot of this length, anchored at the initial fill (the spec: 5 s)."),
    _t("lp.accum_shares", "float", 5.0, 200.0, "Rule 3: shares per accumulation buy", "Leader-pair accumulation", unit="shares", step=1.0,
       help="Rule 3: shares per accumulation buy. In shares."),
    _t("lp.opp_ceiling", "float", 0.05, 0.95, "Rule 3: opposite ask ceiling", "Leader-pair accumulation", unit="USDC", step=0.01,
       help="The opposite token is bought only while P_ref < ask <= this; above it the sequence ends (the spec: 0.50)."),
    _t("lp.accum_eval", "choice", 0, 1, "Rule 3: decision timing", "Leader-pair accumulation", options=("slot_start", "any_tick"),
       help="slot_start: decided on the first tick of each slot. any_tick: at most one buy per slot, on the first tick that qualifies."),
    _t("lp.accum_resequence", "bool", 0, 1, "Rule 3: new sequence after the ceiling", "Leader-pair accumulation",
       help="On: once the opposite rose above the ceiling and came back under it, a new sequence starts (previous-fill reference reset)."),
    _t("lp.max_accum_sequences", "int", 1, 20, "Rule 3: sequences per market at most", "Leader-pair accumulation", step=1,
       help="Rule 3: sequences per market at most."),
    _t("lp.leader_reversal", "float", 0.05, 0.95, "Rule 4: leader reversal threshold", "Leader-pair falling / recovery", unit="USDC", step=0.01,
       help="LEADER_FALLING needs the leader under this and falling (the spec: 0.50)."),
    _t("lp.signal_delta_s", "float", 0.5, 60.0, "Rules 4/5: observation interval", "Leader-pair falling / recovery", unit="s", step=0.5,
       help="The price now is compared with the price this long ago."),
    _t("lp.fall_min_drop", "float", 0.0, 0.20, "Rule 4: minimum fall", "Leader-pair falling / recovery", unit="USDC", step=0.005,
       help="Noise filter: the fall over the interval must be at least this."),
    _t("lp.fall_confirm_ticks", "int", 3, 40, "Rule 4: confirmations", "Leader-pair falling / recovery", step=1,
       help="Consecutive evaluations (every signal_tick_ms) the fall must hold before LEADER_FALLING."),
    _t("lp.recovery_min_rise", "float", 0.0, 0.20, "Rule 5: minimum rise", "Leader-pair falling / recovery", unit="USDC", step=0.005,
       help="Rule 5: minimum rise. In USDC."),
    _t("lp.recovery_confirm_ticks", "int", 3, 40, "Rule 5: confirmations", "Leader-pair falling / recovery", step=1,
       help="Rule 5: confirmations."),
    _t("lp.signal_tick_ms", "float", 100.0, 2000.0, "Rules 4/5: evaluation spacing", "Leader-pair falling / recovery", unit="ms", step=50.0,
       help="Rules 4/5: evaluation spacing. In ms."),
    _t("lp.max_signal_spread", "float", 0.01, 0.20, "Rules 4/5: no signal when the spread is over", "Leader-pair falling / recovery", unit="USDC", step=0.01,
       help="Rules 4/5: no signal when the spread is over. In USDC."),
    _t("lp.falling_buy", "choice", 0, 1, "Rule 4: buys while the leader falls", "Leader-pair falling / recovery", options=("improve_worst", "none"),
       help="improve_worst: the opposite token is bought (fixed size) when it raises the worst settlement outcome. none: nothing."),
    _t("lp.falling_shares", "float", 5.0, 200.0, "Rule 4: shares per falling buy", "Leader-pair falling / recovery", unit="shares", step=1.0,
       help="Rule 4: shares per falling buy. In shares."),
    _t("lp.falling_opp_max_ask", "float", 0.05, 0.99, "Rule 4: opposite ask at most", "Leader-pair falling / recovery", unit="USDC", step=0.01,
       help="Rule 4: opposite ask at most. In USDC."),
    _t("lp.recovery_shares", "float", 5.0, 200.0, "Rule 5: shares per recovery buy", "Leader-pair falling / recovery", unit="shares", step=1.0,
       help="Rule 5: shares per recovery buy. In shares."),
    _t("lp.recovery_interval_s", "float", 1.0, 60.0, "Rule 5: recovery interval", "Leader-pair falling / recovery", unit="s", step=1.0,
       help="Rule 5: recovery interval. In s."),
    _t("lp.recovery_max_ask", "float", 0.05, 0.99, "Rule 5: leader ask at most", "Leader-pair falling / recovery", unit="USDC", step=0.01,
       help="Rule 5: leader ask at most. In USDC."),
    _t("lp.recovery_stop_pnl", "float", -50.0, 50.0, "Rule 5: stop once PnL_live is over", "Leader-pair falling / recovery", unit="USDC", step=0.05,
       help="Recovery buys stop as soon as the conservative liquidation P&L is strictly above this (the spec: 0)."),
    _t("lp.liq_slippage", "float", 0.0, 0.10, "Rule 5: liquidation slippage allowance", "Leader-pair falling / recovery", unit="USDC", step=0.005,
       help="Per share, taken off the bid (with the taker fee) when PnL_live is estimated."),
    _t("lp.fix_enabled", "bool", 0, 1, "Favourite fix on", "Leader-pair favourite fix",
       help="The user's 10-13 rule: the higher-priced token in the band below with a negative P&L should it win is bought up to the target."),
    _t("lp.fix_min_ask", "float", 0.05, 0.99, "Favourite ask at least", "Leader-pair favourite fix", unit="USDC", step=0.01,
       help="The fix buys only while the higher-priced token's ask is at or above this (0.60)."),
    _t("lp.fix_max_ask", "float", 0.05, 0.99, "Favourite ask at most", "Leader-pair favourite fix", unit="USDC", step=0.01,
       help="... and at or below this (0.80)."),
    _t("lp.fix_target_usdc", "float", 0.0, 500.0, "Lift the favourite's outcome to", "Leader-pair favourite fix", unit="USDC", step=0.5,
       help="Shares are sized so the settlement P&L if the favourite wins reaches this (5). Cut by the hard limits, then bought again."),
    _t("lp.fix_cooldown_s", "float", 0.0, 60.0, "Between fix orders", "Leader-pair favourite fix", unit="s", step=0.5,
       help="Seconds between two fix orders in one market."),
    _t("lp.fix_max_left_s", "float", 0.0, 300.0, "Fix only with at most ... left", "Leader-pair favourite fix", unit="s", step=5.0,
       help="The user 10-14: no fix buy before this many seconds remain (210)."),
    _t("lp.fix_require_rising", "bool", 0, 1, "Fix only while the favourite rises", "Leader-pair favourite fix",
       help="The favourite's mid must be above its mid fix_rise_window_s ago by fix_min_rise."),
    _t("lp.fix_rise_window_s", "float", 0.5, 60.0, "Fix: rise window", "Leader-pair favourite fix", unit="s", step=0.5,
       help="The window the rise is measured over."),
    _t("lp.fix_min_rise", "float", 0.0, 0.20, "Fix: minimum rise", "Leader-pair favourite fix", unit="USDC", step=0.005,
       help="The rise over the window must be at least this."),
    _t("lp.hedge_enabled", "bool", 0, 1, "Cheap-token hedge on", "Leader-pair cheap hedge",
       help="The user 10-14: the lower-priced token is bought when the worst outcome is negative, keeping the best outcome at the floor."),
    _t("lp.hedge_max_ask", "float", 0.01, 0.50, "Hedge: lower token ask at most", "Leader-pair cheap hedge", unit="USDC", step=0.01,
       help="The hedge buys only while the lower-priced token's ask is at or under this (0.20)."),
    _t("lp.hedge_min_best_usdc", "float", 0.0, 100.0, "Hedge: best outcome stays at least", "Leader-pair cheap hedge", unit="USDC", step=0.5,
       help="The hedge is sized so the best settlement outcome never drops under this (+2)."),
    _t("lp.exec_mode", "choice", 0, 1, "Execution", "Leader-pair execution", options=("taker",),
       help="Taker-only fill-and-kill orders at a strict maximum price."),
    _t("lp.max_market_capital_usdc", "float", 1.0, 150.0, "Capital per market at most", "Leader-pair limits", unit="USDC", step=1.0,
       help="Fee-inclusive cost one market may reach; the market stops buying at the cap (RISK_LIMIT_STOPPED)."),
    _t("lp.max_total_capital_usdc", "float", 1.0, 500.0, "Capital in all open markets at most", "Leader-pair limits", unit="USDC", step=1.0,
       help="Capital in all open markets at most. In USDC."),
    _t("lp.max_purchases_per_cycle", "int", 0, 6, "Additional purchases per cycle at most", "Leader-pair limits", step=1,
       help="Additional purchases per cycle at most."),
    _t("lp.max_purchases_per_market", "int", 1, 20, "Purchases per market at most", "Leader-pair limits", step=1,
       help="Purchases per market at most."),
    _t("lp.max_settlement_loss_usdc", "float", 0.5, 20.0, "Worst settlement outcome at most", "Leader-pair limits", unit="USDC", step=0.5,
       help="Before every order both settlement outcomes after it must stay above minus this."),
    _t("lp.max_spread", "float", 0.01, 0.30, "Spread at most", "Leader-pair limits", unit="USDC", step=0.01,
       help="Spread at most. In USDC."),
    _t("lp.max_slippage", "float", 0.0, 0.02, "Slippage at most (the IOC limit above the ask)", "Leader-pair limits", unit="USDC", step=0.01,
       help="Slippage at most (the IOC limit above the ask). In USDC."),
    _t("lp.min_depth_shares", "float", 5.0, 1000.0, "Ask depth at least", "Leader-pair limits", unit="shares", step=1.0,
       help="Ask depth at least. In shares."),
    _t("lp.min_time_remaining_s", "float", 20.0, 290.0, "No order with less time left than", "Leader-pair limits", unit="s", step=1.0,
       help="No order with less time left than. In s."),
    _t("lp.max_book_age_ms", "float", 100.0, 30000.0, "Book stale after", "Leader-pair limits", unit="ms", step=100.0,
       help="Book stale after. In ms."),
    _t("lp.min_trade_price", "float", 0.01, 0.50, "Lowest ask bought", "Leader-pair limits", unit="USDC", step=0.01,
       help="Lowest ask bought. In USDC."),
    _t("lp.max_trade_price", "float", 0.50, 0.95, "Highest ask bought", "Leader-pair limits", unit="USDC", step=0.01,
       help="Highest ask bought. In USDC."),
    # ==== RISING-TOKEN RECOVERY (strategy/rising_recovery.py, cfg.rising_recovery; TPB_RTR_* at start): rtr.* reaches only that bot ====
    _t("rtr.mode", "choice", 0, 1, "Mode", "Rising-recovery mode", options=("opt", "spot", "momentum", "pairs"),
       help="opt: the 10-13 consolidated spec. spot: the 10-12 spot rules. momentum / pairs: the 10-09 rules."),
    _t("rtr.opt_open_window_s", "float", 0.5, 30.0, "Opt: initial signal needs BTC for", "Rising-recovery opt mode", unit="s", step=0.5,
       help="Opt: initial signal needs BTC for. In s."),
    _t("rtr.opt_init_bps", "float", 0.0, 20.0, "Opt: initial BTC move at least", "Rising-recovery opt mode", unit="bp", step=0.1,
       help="Opt: initial BTC move at least. In bp."),
    _t("rtr.opt_init_shares", "float", 5.0, 200.0, "Opt: initial shares", "Rising-recovery opt mode", unit="shares", step=1.0,
       help="Opt: initial shares. In shares."),
    _t("rtr.opt_init_max_s", "float", 2.0, 290.0, "Opt: no initial entry later than", "Rising-recovery opt mode", unit="s", step=1.0,
       help="Opt: no initial entry later than. In s."),
    _t("rtr.opt_init_retry_max_s", "float", 0.0, 290.0, "Opt: re-send a failed initial order until", "Rising-recovery opt mode", unit="s", step=1.0,
       help="Seconds after the open; 0 = until the close guard."),
    _t("rtr.opt_price_min", "float", 0.01, 0.95, "Opt: effective price at least", "Rising-recovery opt mode", unit="USDC", step=0.01,
       help="Opt: effective price at least. In USDC."),
    _t("rtr.opt_price_max", "float", 0.05, 0.99, "Opt: effective price at most", "Rising-recovery opt mode", unit="USDC", step=0.01,
       help="Opt: effective price at most. In USDC."),
    _t("rtr.opt_eval_interval_s", "float", 0.5, 60.0, "Opt: decision timer", "Rising-recovery opt mode", unit="s", step=0.5,
       help="Opt: decision timer. In s."),
    _t("rtr.opt_mom_interval_s", "float", 0.5, 60.0, "Opt: momentum timer", "Rising-recovery opt mode", unit="s", step=0.5,
       help="Opt: momentum timer. In s."),
    _t("rtr.opt_mom_low", "float", 0.01, 0.95, "Opt: momentum zone from", "Rising-recovery opt mode", unit="USDC", step=0.01,
       help="Opt: momentum zone from. In USDC."),
    _t("rtr.opt_mom_high", "float", 0.05, 0.99, "Opt: momentum zone to", "Rising-recovery opt mode", unit="USDC", step=0.01,
       help="Opt: momentum zone to. In USDC."),
    _t("rtr.opt_mom_target", "float", 0.05, 0.99, "Opt: momentum target (ends the phase)", "Rising-recovery opt mode", unit="USDC", step=0.01,
       help="Opt: momentum target (ends the phase). In USDC."),
    _t("rtr.opt_mom_ladder_enabled", "bool", 0, 1, "Opt: momentum ladder on", "Rising-recovery opt mode",
       help="The user 10-14: inside the ladder range a momentum buy needs the mid at least one step above the last purchase; the reference survives across rises."),
    _t("rtr.opt_mom_ladder_low", "float", 0.05, 0.95, "Opt: ladder range from", "Rising-recovery opt mode", unit="USDC", step=0.01,
       help="Outside the range the last-purchase reference is reset (0.40)."),
    _t("rtr.opt_mom_ladder_high", "float", 0.05, 0.99, "Opt: ladder range to", "Rising-recovery opt mode", unit="USDC", step=0.01,
       help="Outside the range the last-purchase reference is reset (0.60)."),
    _t("rtr.opt_mom_ladder_reset", "float", 0.01, 0.95, "Opt: ladder reset level", "Rising-recovery opt mode", unit="USDC", step=0.01,
       help="The last-purchase reference while the price is outside the range (0.35): the first buy back inside needs reset + step."),
    _t("rtr.opt_mom_lower_only", "bool", 0, 1, "Opt: momentum on the lower-priced token only", "Rising-recovery opt mode",
       help="The user 10-14: the phase follows the lower-priced token only (off: whichever token rises inside the zone)."),
    _t("rtr.opt_mom_buy_at_start", "bool", 0, 1, "Opt: buy the moment the token starts rising", "Rising-recovery opt mode",
       help="The first momentum buy goes out when the rise is detected (off: only after the first step)."),
    _t("rtr.opt_mom_check_s", "float", 0.5, 60.0, "Opt: check the rise this long after each buy", "Rising-recovery opt mode", unit="s", step=0.5,
       help="Still rising at the check = wait for the next step; stalled or falling = cover with the top-side token and end the phase."),
    _t("rtr.opt_mom_check_min_rise", "float", 0.0, 0.20, "Opt: 'still rising' means the mid rose at least", "Rising-recovery opt mode", unit="USDC", step=0.005,
       help="Measured from the mid at the buy (1c)."),
    _t("rtr.opt_mom_cover", "bool", 0, 1, "Opt: cover a stalled rise with the top-side token", "Rising-recovery opt mode",
       help="When the lower token stalls or reverses after a momentum buy, the top-side token is bought (a pair with it)."),
    _t("rtr.opt_mom_cover_ratio", "float", 0.0, 5.0, "Opt: cover size x the last momentum buy", "Rising-recovery opt mode", step=0.1,
       help="1.0 = the same number of shares as the last lower-token buy."),
    _t("rtr.opt_mom_trigger", "choice", 0, 1, "Opt: momentum buys on", "Rising-recovery opt mode", options=("step", "timer"),
       help="step (the user 10-14): a buy each time the rising token's mid is opt_mom_step above the last momentum buy. timer: every opt_mom_interval_s."),
    _t("rtr.opt_mom_step", "float", 0.005, 0.30, "Opt: momentum step", "Rising-recovery opt mode", unit="USDC", step=0.005,
       help="The rise of the mid, from the last momentum buy (or the phase start), that triggers the next buy (0.03)."),
    _t("rtr.opt_mom_shares", "float", 0.0, 200.0, "Opt: momentum shares per buy (0 = optimizer)", "Rising-recovery opt mode", unit="shares", step=1.0,
       help="Opt: momentum shares per buy (0 = optimizer). In shares."),
    _t("rtr.opt_opposite_delay_s", "float", 0.0, 120.0, "Opt: no opposite buy within ... of the first fill", "Rising-recovery opt mode", unit="s", step=1.0,
       help="Opt: no opposite buy within ... of the first fill. In s."),
    _t("rtr.opt_opposite_ratio", "float", 0.0, 5.0, "Opt: opposite shares at most this x the leader's", "Rising-recovery opt mode", step=0.05,
       help="Opt: opposite shares at most this x the leader's."),
    _t("rtr.opt_quantities", "list", 0, 1, "Opt: candidate quantities", "Rising-recovery opt mode",
       help="Comma list of share counts the optimizer tries, e.g. 5,10,15,20 (no-trade is always a candidate)."),
    _t("rtr.opt_min_improvement", "float", 0.0, 20.0, "Opt: a buy must raise the worst case by", "Rising-recovery opt mode", unit="USDC", step=0.01,
       help="Opt: a buy must raise the worst case by. In USDC."),
    _t("rtr.opt_tie_eps", "float", 0.0, 5.0, "Opt: worst cases equal within", "Rising-recovery opt mode", unit="USDC", step=0.005,
       help="Opt: worst cases equal within. In USDC."),
    _t("rtr.opt_require_positive", "choice", 0, 1, "Opt: only buys that leave both outcomes > 0", "Rising-recovery opt mode", options=("none", "both"),
       help="Opt: only buys that leave both outcomes > 0."),
    _t("rtr.fix_high", "bool", 0, 1, "Favourite fix on", "Rising-recovery favourite fix",
       help="The user's 10-13 rule (opt mode): the higher-priced token in the band below with a negative P&L should it win is bought up to the target."),
    _t("rtr.fix_min_ask", "float", 0.05, 0.99, "Favourite ask at least", "Rising-recovery favourite fix", unit="USDC", step=0.01,
       help="The fix buys only while the higher-priced token's ask is at or above this (0.60)."),
    _t("rtr.fix_max_ask", "float", 0.05, 0.99, "Favourite ask at most", "Rising-recovery favourite fix", unit="USDC", step=0.01,
       help="... and at or below this (0.80)."),
    _t("rtr.high_win_target_usdc", "float", 0.0, 500.0, "Lift the favourite's outcome to", "Rising-recovery favourite fix", unit="USDC", step=0.5,
       help="Shares are sized so the P&L if the favourite wins reaches this (5), within the per-outcome share and per-market cost caps."),
    _t("rtr.fix_max_left_s", "float", 0.0, 300.0, "Fix only with at most ... left", "Rising-recovery favourite fix", unit="s", step=5.0,
       help="The user 10-14: no fix buy before this many seconds remain (210)."),
    _t("rtr.fix_require_rising", "bool", 0, 1, "Fix only while the favourite rises", "Rising-recovery favourite fix",
       help="The favourite's mid must be above its mid fix_rise_window_s ago by fix_min_rise."),
    _t("rtr.fix_rise_window_s", "float", 0.5, 60.0, "Fix: rise window", "Rising-recovery favourite fix", unit="s", step=0.5,
       help="The window the rise is measured over."),
    _t("rtr.fix_min_rise", "float", 0.0, 0.20, "Fix: minimum rise", "Rising-recovery favourite fix", unit="USDC", step=0.005,
       help="The rise over the window must be at least this."),
    _t("rtr.opt_hedge_enabled", "bool", 0, 1, "Opt: cheap-token hedge on", "Rising-recovery favourite fix",
       help="The user 10-14: the lower-priced token is bought when the worst outcome is negative, keeping the best outcome at the floor."),
    _t("rtr.hedge_max_price", "float", 0.01, 0.50, "Hedge: lower token ask at most", "Rising-recovery favourite fix", unit="USDC", step=0.01,
       help="The hedge buys only while the lower-priced token's ask is at or under this (0.20)."),
    _t("rtr.opt_hedge_min_best_usdc", "float", 0.0, 100.0, "Hedge: best outcome stays at least", "Rising-recovery favourite fix", unit="USDC", step=0.5,
       help="The hedge is sized so the best settlement outcome never drops under this (+2)."),
    _t("rtr.opt_exec_mode", "choice", 0, 1, "Opt: execution", "Rising-recovery orders", options=("speed", "mixed", "taker"),
       help="speed (the user 10-14): taker when the token moved fast over the last second, else a resting maker bid. mixed: the maker-fraction split. taker: all taken."),
    _t("rtr.opt_speed_window_ms", "float", 100.0, 10000.0, "Opt: speed window", "Rising-recovery orders", unit="ms", step=100.0,
       help="The mid's move is measured over this window."),
    _t("rtr.opt_speed_threshold", "float", 0.0, 0.30, "Opt: fast when the mid moved at least", "Rising-recovery orders", unit="USDC", step=0.005,
       help="At or above this move over the window the buy is taken; below it the buy rests as a maker bid (2c)."),
    _t("rtr.opt_maker_fraction", "float", 0.0, 1.0, "Opt: maker part of every buy (mixed)", "Rising-recovery orders", step=0.05,
       help="This fraction of each opt-mode buy rests as a post-only bid (no fee if filled); the rest is taken now. 0 = all taker."),
    _t("rtr.opt_maker_rest_s", "float", 1.0, 120.0, "Opt: maker part rests at most", "Rising-recovery orders", unit="s", step=1.0,
       help="The bid is pulled after this long, near the close, or when a newer buy on the token replaces it."),
    _t("rtr.buy_mode", "choice", 0, 1, "Buy mode", "Rising-recovery orders", options=("taker", "maker"),
       help="taker: fill-and-kill at the ask + max slippage. maker: a post-only bid at the bid."),
    _t("rtr.max_slippage", "float", 0.0, 0.20, "Taker limit above the ask", "Rising-recovery orders", unit="USDC", step=0.01,
       help="Taker limit above the ask. In USDC."),
    _t("rtr.max_spread", "float", 0.01, 0.30, "Spread at most", "Rising-recovery orders", unit="USDC", step=0.01,
       help="Spread at most. In USDC."),
    _t("rtr.end_guard_s", "float", 0.0, 30.0, "No new order on a token after one ended unfilled for", "Rising-recovery orders", unit="s", step=0.5,
       help="No new order on a token after one ended unfilled for. In s."),
    _t("rtr.max_market_fills", "int", 1, 500, "Fills per market at most", "Rising-recovery orders", step=1,
       help="Fills per market at most."),
    _t("rtr.lookback_ms", "float", 100.0, 10000.0, "Book signal: lookback", "Rising-recovery orders", unit="ms", step=100.0,
       help="Book signal: lookback. In ms."),
    _t("rtr.min_rise", "float", 0.0, 0.30, "Book signal: minimum rise", "Rising-recovery orders", unit="USDC", step=0.005,
       help="Book signal: minimum rise. In USDC."),
    _t("rtr.confirm_updates", "int", 1, 10, "Book signal: rising updates in a row", "Rising-recovery orders", step=1,
       help="Book signal: rising updates in a row."),
    _t("rtr.max_signal_age_ms", "float", 50.0, 5000.0, "Book signal: at most this old", "Rising-recovery orders", unit="ms", step=50.0,
       help="Book signal: at most this old. In ms."),
    _t("rtr.target_profit_usdc", "float", 0.0, 500.0, "Lock the market once the worst outcome reaches", "Rising-recovery limits", unit="USDC", step=0.5,
       help="The user 10-14 (every mode): the market is done once min(UP, DOWN) - cost reaches this (5): no more buys. 0 = off in opt mode."),
    _t("rtr.opt_pair_enabled", "bool", 0, 1, "Opt: pair buy on", "Rising-recovery limits",
       help="The user 10-14: when the token opposite the unpaired lots can be bought so the pairs cost at most the threshold, the completing amount is bought."),
    _t("rtr.opt_pair_threshold", "float", 0.50, 1.00, "Opt: pair buy at a pair cost of at most", "Rising-recovery limits", unit="USDC", step=0.01,
       help="Lot unit cost + this token's ask + its fee (0.95)."),
    _t("rtr.max_market_cost_usdc", "float", 1.0, 5000.0, "Cost per market at most", "Rising-recovery limits", unit="USDC", step=1.0,
       help="Cost per market at most. In USDC."),
    _t("rtr.max_market_loss_usdc", "float", 0.5, 5000.0, "Worst outcome per market at most", "Rising-recovery limits", unit="USDC", step=0.5,
       help="Worst outcome per market at most. In USDC."),
    _t("rtr.max_total_cost_usdc", "float", 1.0, 20000.0, "Cost in all open markets at most", "Rising-recovery limits", unit="USDC", step=1.0,
       help="Cost in all open markets at most. In USDC."),
    _t("rtr.spot_max_shares", "float", 5.0, 2000.0, "Spot / opt: shares per outcome at most", "Rising-recovery limits", unit="shares", step=5.0,
       help="Spot / opt: shares per outcome at most. In shares."),
    _t("rtr.spot_max_cost_usdc", "float", 1.0, 5000.0, "Spot / opt: cost per market at most", "Rising-recovery limits", unit="USDC", step=1.0,
       help="Spot / opt: cost per market at most. In USDC."),
    _t("rtr.spot_slippage_allowance", "float", 0.0, 0.10, "Spot / opt: slippage allowance per share", "Rising-recovery limits", unit="USDC", step=0.005,
       help="Spot / opt: slippage allowance per share. In USDC."),
    _t("rtr.spot_open_delay_s", "float", 0.0, 120.0, "Spot: no buy before ... after the open", "Rising-recovery limits", unit="s", step=1.0,
       help="Spot: no buy before ... after the open. In s."),
    # ==== the spot-lag strategy (cfg.spot_lag; TPB_SPOT_LAG_* at start) ====
    # ---- the signal ------------------------------------------------------
    _t("spot_lag.k_sigma", "float", 1.0, 10.0, "Shock size", "Spot-lag signal", unit="sigma", step=0.1,
       help="How big a spot move must be, in units of the asset's recent volatility, over the shock "
            "window. Lower fires more often, on smaller moves."),
    _t("spot_lag.shock_window_s", "float", 0.1, 1.0, "Shock window", "Spot-lag signal", unit="s", step=0.05,
       help="The window the shock is measured over. 1.0 is the tested rule; shorter sees the same move "
            "earlier, before the token has repriced."),
    _t("spot_lag.k3_sigma", "float", 0.0, 5.0, "3-second agreement", "Spot-lag signal", unit="sigma", step=0.1,
       help="The 3-second spot return must point the same way by at least this many sigma."),
    _t("spot_lag.require_acceleration", "bool", 0, 1, "Require acceleration", "Spot-lag signal",
       help="The 1-second move must be faster than the 3-second trend."),
    _t("spot_lag.fv_trigger_cents", "float", 0.0, 50.0, "Fair-value trigger", "Spot-lag signal", unit="cents", step=1.0,
       help="Also signal when the token's fair value moved this many cents in the last second, even "
            "without a big spot shock (near the strike a small spot move reprices the token a lot). 0 = off."),
    _t("spot_lag.trend_window_s", "float", 0.0, 300.0, "Trend window", "Spot-lag signal", unit="s", step=5.0,
       help="Only trade a shock that continues the spot trend over this many seconds before it. 0 = off."),
    _t("spot_lag.trend_min_bps", "float", -50.0, 50.0, "Trend minimum", "Spot-lag signal", unit="bps", step=0.5,
       help="The trend over the window must be at least this far in the shock's direction (basis points)."),
    _t("spot_lag.shock_sigma_1s", "bool", 0, 1, "Shock vs 1-second volatility", "Spot-lag signal",
       help="Judge the shock against 1-second volatility. Off: against the 5-minute pricing volatility, "
            "which runs up to 3x larger after sustained moves and hides fast moves (the rule before 2026-09-30)."),
    _t("spot_lag.dedupe_s", "float", 0.0, 60.0, "Signal cooldown", "Spot-lag signal", unit="s", step=0.5,
       help="At most one signal per market and direction in this long."),
    _t("spot_lag.info_lag_s", "float", 0.0, 2.0, "Model information lag", "Spot-lag signal", unit="s",
       step=0.01, help="How stale the model assumes its spot price is (the measured feed lag)."),
    _t("spot_lag.assets", "list", 0, 1, "Only these assets", "Spot-lag signal",
       help="Comma list, e.g. BTC,ETH. Empty or none means every asset the bot watches; it only "
            "watches the assets it was started with."),
    _t("spot_lag.exclude_assets", "list", 0, 1, "Never these assets", "Spot-lag signal",
       help="Comma list of assets never traded. Type none to exclude nothing."),
    _t("spot_lag.max_time_remaining_s", "float", 10.0, 300.0, "Start entering at", "Spot-lag signal",
       unit="s left", step=5,
       help="No entries earlier than this many seconds before the close. 295 is almost the whole window."),
    _t("spot_lag.min_time_remaining_s", "float", 0.0, 290.0, "Stop entering at", "Spot-lag signal",
       unit="s left", step=5,
       help="No new entries with less than this left in the 5-minute window."),

    # ---- the entry price ---------------------------------------------------
    _t("spot_lag.min_net_edge", "float", 0.0, 0.2, "Minimum net edge", "Spot-lag entry", unit="prob",
       step=0.005,
       help="Required edge per share after both fees, the exit's half spread, slippage and uncertainty."),
    _t("spot_lag.slippage", "float", 0.0, 0.05, "Slippage charge", "Spot-lag entry", unit="prob", step=0.001,
       help="Charged against the edge on every entry."),
    _t("spot_lag.uncertainty", "float", 0.0, 0.05, "Uncertainty charge", "Spot-lag entry", unit="prob",
       step=0.001, help="Charged against the edge for model error."),
    _t("spot_lag.max_slippage", "float", 0.0, 0.10, "Limit above the ask", "Spot-lag entry", unit="prob",
       step=0.005,
       help="How far above the seen ask the order's limit may sit. The engine's maximum slippage, and "
            "with real money its ceiling, still bound it."),
    _t("spot_lag.max_spread", "float", 0.005, 0.2, "Maximum spread", "Spot-lag entry", unit="prob",
       step=0.005,
       help="Skip a book wider than this. The engine's maximum spread, and with real money its "
            "ceiling, still bound it."),
    _t("spot_lag.min_ask", "float", 0.01, 0.98, "Minimum ask", "Spot-lag entry", step=0.01,
       help="Never buy a token priced below this."),
    _t("spot_lag.max_ask", "float", 0.02, 0.99, "Maximum ask", "Spot-lag entry", step=0.01,
       help="Never buy a token priced above this. Most signals arrive above 0.70."),
    _t("spot_lag.max_quote_age_ms", "float", 50.0, 5000.0, "Max token quote age", "Spot-lag entry",
       unit="ms", step=50, help="Refuse to buy on a token quote older than this."),
    _t("spot_lag.max_spot_age_ms", "float", 50.0, 5000.0, "Max spot age", "Spot-lag entry",
       unit="ms", step=50, help="Refuse to buy on a spot price older than this."),

    # ---- the order -----------------------------------------------------------
    _t("spot_lag.order_type", "choice", 0, 1, "Order type", "Spot-lag orders", options=("FOK", "FAK"),
       help="FOK buys the whole size or nothing. FAK takes what the book holds under the limit, up to "
            "the size, and cancels the rest. Exits are always FOK."),
    _t("spot_lag.size_mode", "choice", 0, 1, "Sizing", "Spot-lag orders", options=("fixed", "touch"),
       help="fixed sends the full size or nothing. touch sends what the book shows under the limit, "
            "down to the venue minimum."),
    _t("spot_lag.size_shares", "float", 5.0, 1000.0, "Shares per order", "Spot-lag orders", unit="shares",
       step=1, help="Never below the market's 5-share minimum. The per-order cap still trims it."),
    _t("spot_lag.order_size_usdc", "float", 0.0, 500.0, "Target notional", "Spot-lag orders", unit="USDC",
       step=0.5, help="Size by money instead of shares. 0 uses the share count above."),

    # ---- getting out -----------------------------------------------------------
    _t("spot_lag.take_profit_cents", "float", 0.0, 50.0, "Take profit at", "Spot-lag exits", unit="cents",
       step=0.5, help="Sell at the bid once it is this many cents above the entry. 0 turns it off."),
    _t("spot_lag.stop_loss_cents", "float", 0.0, 50.0, "Stop loss at", "Spot-lag exits", unit="cents",
       step=0.5, help="Sell through the bid once it is this many cents below the entry. 0 turns it off."),
    _t("spot_lag.hold_s", "float", 1.0, 300.0, "Maximum hold", "Spot-lag exits", unit="s", step=1,
       help="Sell this long after the fill when neither price exit has fired."),
    _t("spot_lag.extend_z1", "float", 0.0, 10.0, "Extend while spot moves", "Spot-lag exits", unit="sigma",
       step=0.1, help="Keep holding past the maximum while the 1-second spot move is at least this "
                      "strong in our direction."),
    _t("spot_lag.extend_max_hold_s", "float", 0.0, 300.0, "Extended hold limit", "Spot-lag exits",
       unit="s", step=1, help="The longest the extension may hold. At or below the maximum hold it is off."),
    _t("spot_lag.exit_max_cross", "float", 0.0, 0.2, "Exit may cross", "Spot-lag exits", unit="prob",
       step=0.005, help="A stop or timed exit may sell this far below the bid to get the whole size done."),
    _t("spot_lag.hold_to_settlement", "bool", 0, 1, "Hold to settlement", "Spot-lag exits",
       help="Never sell on the timer; positions settle at 1 or 0. The price exits still apply if set."),

    # ---- how fast it reacts -------------------------------------------------------
    _t("spot_lag.event_driven", "bool", 0, 1, "Event-driven", "Spot-lag speed",
       help="Evaluate the moment a spot quote shows a shock, instead of on the 200 ms timer."),
    _t("spot_lag.event_trigger_frac", "float", 0.1, 1.0, "Pre-check at", "Spot-lag speed", unit="x shock",
       step=0.05, help="A quote triggers an evaluation at this fraction of the shock size. The full "
                       "gates still decide."),
    _t("spot_lag.event_min_interval_ms", "float", 0.0, 1000.0, "Min gap between evaluations",
       "Spot-lag speed", unit="ms", step=10,
       help="At most one event evaluation per asset in this long, so a burst of quotes cannot flood it."),

    # ---- the fast feed ----------------------------------------------------------------
    _t("spot_lag.fast_feed", "bool", 0, 1, "Fast feed", "Spot-lag speed",
       help="Watch every raw exchange quote at tick resolution and evaluate the moment a short-horizon "
            "return is abnormal, instead of waiting for the 1 s return. Pair with a shock window below 1 s."),
    _t("spot_lag.fast_horizons_ms", "list", 0, 1, "Fast horizons", "Spot-lag speed",
       help="Comma list of horizons in ms the fast feed watches, e.g. 50,100,250,500."),
    _t("spot_lag.fast_spread_floor", "float", 0.0, 5.0, "Fast spread floor", "Spot-lag speed", unit="x spread",
       step=0.1, help="A short-horizon move must also exceed this many times the quoting exchange's spread."),
    _t("spot_lag.fast_dedupe_ms", "float", 0.0, 5000.0, "Fast detection gap", "Spot-lag speed", unit="ms",
       step=50, help="At most one fast detection per asset and direction in this long."),
    _t("spot_lag.fast_history_gap_ms", "float", 5.0, 200.0, "Fast history sampling", "Spot-lag speed", unit="ms",
       step=5, help="How densely the spot history is sampled with the fast feed on (200 ms otherwise)."),

    # ---- the strategy's own budget -------------------------------------------------
    _t("spot_lag.max_simultaneous_positions", "int", 1, 20, "Open positions", "Spot-lag budget", step=1,
       help="Open positions plus working entries, all markets together. Written into the risk limits."),
    _t("spot_lag.max_buys_per_hour", "int", 1, 1000, "Buys per hour", "Spot-lag budget", step=1,
       help="BUY orders in any trailing hour, killed ones included."),
    _t("spot_lag.max_entries_per_market", "int", 1, 10, "Fills per market", "Spot-lag budget", step=1,
       help="Filled entries per 5-minute market. Above 1, an exited market may be entered again."),
    _t("spot_lag.max_attempts_per_market", "int", 1, 50, "Attempts per market", "Spot-lag budget", step=1,
       help="BUY orders per market, killed ones included."),
    _t("spot_lag.max_order_usdc", "float", 1.0, 500.0, "Cap per order (spot-lag budget)", "Spot-lag budget", unit="USDC",
       step=0.5, help="Worst-case cost of one order (limit x size). Written into the risk limits."),
    _t("spot_lag.max_total_exposure_usdc", "float", 1.0, 5000.0, "Total open", "Spot-lag budget",
       unit="USDC", step=1, help="Open positions plus working entries at worst case. Written into the "
                                 "risk limits."),
    _t("spot_lag.max_daily_loss_usdc", "float", 1.0, 5000.0, "Daily loss budget (spot-lag)", "Spot-lag budget",
       unit="USDC", step=1, help="Stop entering for the day at this realised loss. Written into the "
                                 "risk limits."),

    # ==== the shared engine (analytic / momentum strategies, plus a few bounds) ====
    # ---- what counts as an edge -----------------------------------------
    _t("engine.min_net_edge", "float", 0.0, 0.5, "Minimum net edge (analytic strategy only)", "Edge",
       unit="prob", step=0.005, not_for=_SPOT,
       help="Required edge per share AFTER fee and slippage. Lower trades more and thinner."),
    _t("engine.market_blend", "float", 0.0, 1.0, "Market blend", "Edge",
       unit="weight", step=0.05, not_for=_SPOT,
       help="Weight on the venue mid in the blended probability. 1.0 trusts the market entirely."),
    _t("engine.expected_slippage", "float", 0.0, 0.1, "Expected slippage", "Edge", step=0.001, not_for=_SPOT,
       help="Charged against the edge before the gate. Raise it if fills come in worse than quoted."),
    _t("engine.confidence_gate_mult", "float", 0.0, 5.0, "Confidence gate", "Edge", step=0.1, not_for=_SPOT,
       help="Edge after costs must beat this many model error bands, or the trade is noise."),
    _t("engine.uncertainty_charge", "float", 0.0, 5.0, "Uncertainty charge (analytic strategy only)", "Edge", step=0.1, not_for=_SPOT,
       help="Fraction of the model's error band subtracted from the edge."),

    # ---- which books are worth taking -----------------------------------
    _t("engine.min_trade_price", "float", 0.01, 0.5, "Minimum price", "Book filters", step=0.01,
       help="Never pay less than this per share. Below it the fee curve and the tick grid dominate."),
    _t("engine.max_trade_price", "float", 0.5, 0.99, "Maximum price", "Book filters", step=0.01,
       help="Never pay more than this per share."),
    _t("engine.max_spread", "float", 0.005, 0.5, "Maximum spread (ceiling, all strategies)", "Book filters", step=0.005,
       help="Skip a book wider than this. A wide book means the exit costs more than the edge."),
    _t("engine.max_slippage", "float", 0.0, 0.2, "Maximum slippage", "Book filters", step=0.005,
       help="Bound on how far above the ask any strategy's limit may sit."),
    _t("engine.min_depth_shares", "float", 0.0, 1000.0, "Minimum depth", "Book filters",
       unit="shares", step=1, not_for=_SPOT,
       help="Resting shares required at our limit before we size anything."),
    _t("engine.cross_ticks", "int", 0, 5, "Cross ticks", "Book filters", unit="ticks", step=1, not_for=_SPOT,
       help="How many ticks through the ask the limit is placed, to make a fill-or-kill land."),

    # ---- how the entry is placed -----------------------------------------
    _t("engine.maker_entry", "bool", 0, 1, "Rest the entry", "Entry style", not_for=_SPOT,
       help="Post a limit order above the bid and wait, instead of hitting the ask. Fee-free; "
            "fills when the market comes to it, which is also when it may be leaving you."),
    _t("engine.maker_improve_ticks", "int", 0, 3, "Ticks above bid", "Entry style", unit="ticks", step=1,
       not_for=_SPOT, help="Where the resting order sits. 0 joins the bid; it is always kept below the ask."),
    _t("engine.maker_ttl_s", "float", 5.0, 120.0, "Resting time", "Entry style", unit="s", step=5,
       not_for=_SPOT, help="Cancel an unfilled resting order after this long."),

    # ---- when in the window ---------------------------------------------
    _t("engine.trade_window_start_s", "float", 10.0, 300.0, "Window opens at", "Timing",
       unit="s left", step=5, not_for=_SPOT,
       help="Seconds remaining when trading may start. 295 is the whole window."),
    _t("engine.trade_window_end_s", "float", 0.0, 290.0, "Window closes at", "Timing",
       unit="s left", step=5, not_for=_SPOT,
       help="Stop this many seconds before resolution. Books go one-sided near the end."),
    _t("engine.max_spot_age_ms", "float", 100.0, 20000.0, "Max spot age (ceiling, all strategies)", "Timing", unit="ms", step=100,
       help="Refuse to trade on a spot price older than this."),
    _t("engine.max_book_age_ms", "float", 100.0, 20000.0, "Max book age", "Timing", unit="ms", step=100,
       help="Refuse to trade on an order book older than this."),
    _t("engine.max_source_deviation_bps", "float", 1.0, 200.0, "Spot sources may disagree", "Timing",
       unit="bps", step=1,
       help="Skip when the spot exchanges disagree by more than this. Wider trusts a split feed."),
    _t("engine.latency_safety_margin_ms", "float", 0.0, 5000.0, "Latency margin", "Timing", unit="ms",
       step=50, help="Added to the measured order round trip when checking there is time left to trade."),

    # ---- getting out ------------------------------------------------------
    _t("engine.take_profit_enabled", "bool", 0, 1, "Take profit", "Exits", not_for=_SPOT,
       help="Sell into a risen bid instead of holding to the oracle print."),
    _t("engine.take_profit_delta", "float", 0.005, 0.9, "Take profit at (analytic strategy only)", "Exits", unit="prob", step=0.005,
       not_for=_SPOT, help="Bid must rise this far above our average entry before we bank it."),
    _t("engine.stop_loss_enabled", "bool", 0, 1, "Stop loss", "Exits", not_for=_SPOT,
       help="Cut a position whose window has decided against us."),
    _t("engine.stop_cross_ticks", "int", 0, 5, "Stop crosses", "Exits", unit="ticks", step=1,
       help="How far below the bid a stop-loss or timed exit is first priced, so a downtick in flight "
            "does not kill it."),
    _t("engine.stop_loss_delta", "float", 0.01, 0.99, "Stop loss at (analytic strategy only)", "Exits", unit="prob", step=0.01,
       not_for=_SPOT, help="Sell when the bid falls this far below our average entry."),

    # ---- how big ----------------------------------------------------------
    _t("risk.kelly_fraction", "float", 0.0, 1.0, "Kelly fraction", "Sizing", step=0.01,
       help="Fraction of the full Kelly stake. Smaller bets, more of them."),
    _t("risk.min_order_usdc", "float", 0.5, 100.0, "Minimum order", "Sizing", unit="USDC", step=0.5,
       help="Below this a trade is not worth the fee. Note the venue also demands 5 shares."),
    _t("risk.max_position_usdc", "float", 0.5, 1000.0, "Max per market", "Sizing", unit="USDC", step=0.5,
       help="Cap on one window's stake. Divided by the price, it must still buy 5 shares."),
    _t("risk.max_asset_exposure_usdc", "float", 1.0, 5000.0, "Max per asset", "Sizing",
       unit="USDC", step=1,
       help="Cap across every open window of one asset."),
    _t("risk.max_epoch_exposure_usdc", "float", 1.0, 5000.0, "Max per epoch", "Sizing",
       unit="USDC", step=1,
       help="Cap across all assets sharing a 5-minute window. They are one correlated bet."),
    _t("risk.max_total_exposure_usdc", "float", 1.0, 10000.0, "Max total open", "Sizing",
       unit="USDC", step=1,
       help="Cap on everything open at once."),
    _t("risk.max_concurrent_positions", "int", 1, 50, "Max open positions", "Sizing", step=1,
       help="How many windows we may be in simultaneously."),

    # ---- when to stop -----------------------------------------------------
    _t("risk.max_daily_loss_usdc", "float", 1.0, 5000.0, "Daily loss limit (circuit breaker)", "Circuit breakers",
       unit="USDC", step=1,
       help="Strategy-level halt once the day's realised losses reach this."),
    _t("risk.max_drawdown_usdc", "float", 1.0, 5000.0, "Drawdown limit", "Circuit breakers",
       unit="USDC", step=1,
       help="Halt once equity falls this far from its peak."),

    # ---- the real-money envelope: the venue adapter's own caps -------------
    _t("caps.max_order_usdc", "float", 0.5, 500.0, "Cap per order (real-money caps)", "Real-money caps",
       unit="USDC", step=0.5, applies_to="live", ceiling=True,
       help="Hard cap checked at the venue adapter. Must exceed 5 shares at your price, "
            "or every order is refused."),
    _t("caps.max_open_usdc", "float", 1.0, 2000.0, "Cap on open notional", "Real-money caps",
       unit="USDC", step=1, applies_to="live", ceiling=True,
       help="Hard cap on everything open at the venue."),
    _t("caps.max_daily_loss_usdc", "float", 1.0, 2000.0, "Cap on daily loss", "Real-money caps",
       unit="USDC", step=1, applies_to="live", ceiling=True,
       help="Halt the venue adapter for the rest of the run at this realised loss."),
    _t("caps.max_orders_per_hour", "int", 1, 1000, "Cap on orders per hour", "Real-money caps",
       unit="orders", step=1, applies_to="live", ceiling=True,
       help="Rate limit enforced before anything is signed."),

    # ---- the real-money envelope: micro-live ceilings (TPB_MICRO_* at start) ----
    # ==== pair-lock R's open rule (TPB_PAIRLOCK_REC_* at start; only the pair-lock recovery bot has these) ====
    _t("pairlock.pair_sum", "float", 0.50, 1.00, "Pair sum (open buy + other-side bid)", "Pair-lock open rule", unit="USDC",
       step=0.01, help="In the first seconds after a market opens the bot buys the higher-priced token, then rests a bid "
            "for the OTHER token at this sum minus the price paid (0.97 - 0.62 = 0.35). If it fills, the pair cost this much "
            "and pays 1.00. If the other token becomes the higher price first, the bid is pulled and the loss-recovery "
            "logic takes over. Applies to new buys; a bid already resting keeps its price."),
    _t("pairlock.open_window_s", "float", 1.0, 60.0, "Open buy window", "Pair-lock open rule", unit="s", step=1.0,
       help="The first buy is only within this many seconds after the market opens."),
    _t("pairlock.open_shares", "float", 5.0, 100.0, "Open buy size", "Pair-lock open rule", unit="shares", step=1.0,
       help="Shares of the higher-priced token bought at the open."),
    _t("pairlock.open_min_price", "float", 0.50, 0.95, "Open buy: leader at least", "Pair-lock open rule", unit="USDC", step=0.01,
       help="The higher-priced token is bought only while its ask is at least this (2026-10-06 replay: 0.65 skipped two "
            "thirds of markets and cut the worst day from -146 to -38). Checked every tick inside the open window."),
    _t("pairlock.open_max_price", "float", 0.50, 0.99, "Open buy: leader at most", "Pair-lock open rule", unit="USDC", step=0.01,
       help="... and at most this (a leader already near 1.00 leaves nothing to earn)."),
    _t("pairlock.max_recoveries", "int", 0, 20, "Recoveries per market", "Pair-lock open rule", unit="times", step=1,
       help="How many times the bot recovers a falling token in one market (0 = no limit). One recovery is the buys "
            "toward one side while it stays the higher price; after the last one the market is held to settlement."),
    _t("pairlock.max_market_loss", "float", 0.0, 100.0, "Max loss per market", "Pair-lock open rule", unit="USDC", step=0.5,
       help="0 = off. When a loss recovery would leave the other outcome worse than minus this, the bot stops chasing "
            "and LOCKS the market instead: it buys the side with fewer shares until UP = DOWN, so the market's result is "
            "fixed whoever wins, and makes no more recovery buys there. The late buy is trimmed so it cannot break this."),
    _t("pairlock.pair_bid", "bool", 0, 1, "Other-side bid after the first buy", "Pair-lock open rule",
       help="On: after the first buy the bot rests a bid for the other token at the pair sum minus the price paid. "
            "Off: no bid; the position is managed by the loss recovery alone."),
    _t("pairlock.recovery_wait_above", "float", 0.50, 1.00, "Recovery: only under", "Pair-lock open rule", unit="USDC", step=0.01,
       help="A recovery is bought only while the token it buys costs LESS than this (1 = off). At or above it the bot waits."),
    _t("pairlock.profit_exit", "float", 0.0, 20.0, "Profit exit after a recovery", "Pair-lock open rule", unit="USDC", step=0.1,
       help="After a recovery, once the whole position's mark-to-market profit (every token at its bid, all costs and "
            "fees in) reaches this, the bot sells every token it holds in the market at the bid. 0 = off (hold to settlement)."),
    _t("pairlock.recovery_sell", "bool", 0, 1, "Sell the falling side when recovering", "Pair-lock open rule",
       help="On: once a recovery buy toward one side has filled, every token held of the other (falling) side is sold at "
            "its bid, fill-and-kill, while the bid is above 0.01. Not after a lock. Off: the falling side is held to settlement."),
    _t("pairlock.open_delay_s", "float", 0.0, 60.0, "Open buy: not before", "Pair-lock open rule", unit="s", step=1.0,
       help="The first buy waits this many seconds after the market opens (the books settle down after the open)."),
    _t("pairlock.recovery_min_price", "float", 0.0, 0.99, "Recovery: lowest price", "Pair-lock open rule", unit="USDC", step=0.01,
       help="A recovery is bought only once the token it buys costs MORE than this (0 = off: as soon as it turns "
            "higher). Below it the bot waits; a swing that turns back first needs no recovery."),
    _t("pairlock.recovery_max_price", "float", 0.50, 1.00, "Recovery: highest price", "Pair-lock open rule", unit="USDC", step=0.01,
       help="When the token the recovery would buy costs more than this, the bot locks the market instead (1 = off). "
            "At high prices a recovery needs many shares for little gain per share."),
    _t("pairlock.lock_left_s", "float", 0.0, 300.0, "Recovery: lock with time left under", "Pair-lock open rule", unit="s", step=5.0,
       help="With fewer seconds left than this, the bot locks the market instead of recovering (0 = off)."),
    _t("pairlock.even_out_from", "int", 0, 20, "Recovery: even out from recovery number", "Pair-lock open rule", unit="#", step=1,
       help="From this recovery on (2 = the second, third, ...), a recovery buys the rising token only until the market "
            "holds as many of it as of the falling token (UP = DOWN), so both outcomes pay the same and the doubling "
            "stops. Earlier recoveries keep the usual target sizing. 0 = off."),
    _t("pairlock.ladder_window_s", "float", 0.0, 300.0, "Late ladder: time left under", "Pair-lock open rule", unit="s", step=5.0,
       help="In a market the bot holds, with fewer seconds left than this, it buys the higher-priced token while its "
            "price is inside the ladder band, once every ladder interval. 0 = off."),
    _t("pairlock.ladder_min_price", "float", 0.50, 0.99, "Late ladder: lowest price", "Pair-lock open rule", unit="USDC", step=0.01,
       help="The ladder buys only while the higher token's ask is at least this."),
    _t("pairlock.ladder_max_price", "float", 0.50, 0.99, "Late ladder: highest price", "Pair-lock open rule", unit="USDC", step=0.01,
       help="... and at most this; the order's limit is never above it."),
    _t("pairlock.ladder_interval_s", "float", 1.0, 120.0, "Late ladder: every", "Pair-lock open rule", unit="s", step=1.0,
       help="Seconds between two ladder buys in one market."),
    _t("pairlock.ladder_shares", "float", 5.0, 100.0, "Late ladder: shares per buy", "Pair-lock open rule", unit="shares", step=1.0,
       help="Shares per ladder buy (trimmed so the other outcome stays within the max loss per market)."),
    # ==== the swing recovery (the user's 2026-10-07 spec; TPB_PAIRLOCK_SWING_* at start; acts only in a bot started with
    #      TPB_PAIRLOCK_SWING_MODE=1 -- the paper recovery bot; the mode itself is not a dashboard switch). The strategy's
    #      rows apply to markets entered after the change; the capital cap binds at once.
    _t("pairlock.swing_max_recovery", "int", 0, 2, "Recovery depth (directional swings)", "Pair-lock swing recovery", unit="swings",
       step=1, help="How many swings get a directional recovery buy before the bot switches to the two-sided hedge. 2 (default): "
            "swing 1 and swing 2 recover, swing 3 and every later swing only even the inventory out (UP = DOWN). 1: swing 2+ "
            "hedges. 0: the first swing already hedges. Sizes exist for swings 1 and 2 only, so 2 is the deepest."),
    _t("pairlock.swing_initial_shares", "float", 5.0, 100.0, "Swing 0: initial shares", "Pair-lock swing recovery", unit="shares",
       step=1.0, help="Shares of the leading token bought about 20 s after the market opens (default 10)."),
    _t("pairlock.swing_entry_min", "float", 0.05, 0.95, "Swing 0: entry VWAP at least", "Pair-lock swing recovery", unit="USDC",
       step=0.01, help="The executable VWAP of the initial shares (walked through the asks, not the mid) must be at least this."),
    _t("pairlock.swing_entry_max", "float", 0.05, 0.95, "Swing 0: entry VWAP at most", "Pair-lock swing recovery", unit="USDC",
       step=0.01, help="... and at most this; it is also the entry order's limit."),
    _t("pairlock.swing_entry_delay_s", "float", 0.0, 120.0, "Swing 0: not before", "Pair-lock swing recovery", unit="s", step=1.0,
       help="The initial buy waits this many seconds after the market opens (default 20)."),
    _t("pairlock.swing_entry_window_s", "float", 0.0, 290.0, "Swing 0: not after", "Pair-lock swing recovery", unit="s", step=1.0,
       help="No initial buy later than this many seconds after the open (default 30); no entry in that window = no trade."),
    _t("pairlock.swing_recovery_price", "float", 0.50, 0.99, "Swing: recovery price", "Pair-lock swing recovery", unit="USDC",
       step=0.01, help="A swing is counted when the token against the position leads (by the hysteresis) and its best ask has "
            "reached this price (default 0.70). The recovery is bought at the executable ask, never assumed to be this price."),
    _t("pairlock.swing_target_profit", "float", 0.0, 20.0, "Recovery target profit", "Pair-lock swing recovery", unit="USDC",
       step=0.5, help="Swing 1 and swing 2 buy the SMALLEST whole number of shares of the new leader that makes the market pay "
            "more than this if that token wins, after every cost so far and the new order's fee, priced on the live ask book "
            "(10 A for 6.00, B at 0.74: 37 shares at 3.00). Swing 2 starts from what swing 1 left. Not a fixed size."),
    _t("pairlock.swing_max_recovery_shares", "float", 5.0, 1000.0, "Max recovery shares", "Pair-lock swing recovery", unit="shares",
       step=5.0, help="A recovery whose size would exceed this (one swing, all its orders) is refused: nothing is bought."),
    _t("pairlock.swing_max_recovery_price", "float", 0.50, 0.99, "Recovery: highest ask level", "Pair-lock swing recovery",
       unit="USDC", step=0.01, help="A recovery that would need ask levels above this price is refused (nothing is bought): "
            "near 1.00 a share earns too little to recover with."),
    _t("pairlock.swing_two_sided_hedge", "bool", 0, 1, "Swing 3+: two-sided hedge", "Pair-lock swing recovery",
       help="On: past the recovery depth the bot buys the side with fewer shares until UP = DOWN (default path: 66 B, 90/90, "
            "-35 whoever wins). Off: it holds the position to resolution unchanged."),
    _t("pairlock.swing_max_market_capital", "float", 10.0, 1000.0, "Max capital per market", "Pair-lock swing recovery",
       unit="USDC", step=1.0, help="No order is placed if its worst case (every share at its limit, fee in) would take the "
            "market's spending above this. The default path needs 125.00 before fees; fees add about 2.70."),
    _t("pairlock.swing_leader_hysteresis", "float", 0.0, 0.50, "Leader hysteresis", "Pair-lock swing recovery", unit="USDC",
       step=0.01, help="The other token becomes the leader only once its mid exceeds the current leader's mid by this much, so "
            "tiny moves around 0.50 are not counted as leadership changes."),
    # ==== the pair-lock BTC bot's conditions rule (tpb-pairlock-paper; TPB_PAIRLOCK_REC_* at start; only that bot) ====
    _t("pairbtc.pair_sum", "float", 0.50, 1.00, "Pair sum (buy + other-side bid)", "Pair-lock BTC conditions rule", unit="USDC",
       step=0.01, help="After the conditions buy fills, the bot rests a bid for the OTHER token at this sum minus the price "
            "paid (0.97 - 0.55 = 0.42), until the close. If it fills, the pair cost this much and pays 1.00."),
    _t("pairbtc.open_shares", "float", 5.0, 100.0, "Buy size", "Pair-lock BTC conditions rule", unit="shares", step=1.0,
       help="Shares of the higher-priced token bought when every condition holds (the pair bid is for what filled)."),
    _t("pairbtc.cond_min_price", "float", 0.0, 1.0, "Higher token: lowest price", "Pair-lock BTC conditions rule", unit="USDC",
       step=0.01, help="The higher-priced token's ask must be at least this."),
    _t("pairbtc.cond_max_price", "float", 0.0, 1.0, "Higher token: highest price", "Pair-lock BTC conditions rule", unit="USDC",
       step=0.01, help="... and at most this."),
    _t("pairbtc.cond_min_left_s", "float", 0.0, 300.0, "Least time left", "Pair-lock BTC conditions rule", unit="s", step=5.0,
       help="The market must end at least this many seconds later."),
    _t("pairbtc.cond_spot_lo", "float", -100.0, 100.0, "BTC - strike above", "Pair-lock BTC conditions rule", unit="USD", step=0.5,
       help="BTC minus the strike must be strictly above this ..."),
    _t("pairbtc.cond_spot_hi", "float", -100.0, 100.0, "BTC - strike below", "Pair-lock BTC conditions rule", unit="USD", step=0.5,
       help="... and strictly below this (0 and 5: BTC 0-5 USD over the strike)."),
    _t("pairbtc.cond_max_entries", "int", 1, 50, "Buys per market", "Pair-lock BTC conditions rule", unit="buys", step=1,
       help="1 = one buy per market. More: once EVERY held token is paired (its pair bid filled), the bot buys again "
            "whenever the conditions hold, each buy with its own pair bid, up to this many per market."),
    _t("pairbtc.recovery_enabled", "bool", 0, 1, "Loss recovery", "Pair-lock BTC conditions rule",
       help="On: when the other-side bid has not filled and the other token becomes the higher price, the recovery buys "
            "the higher token (sized so its winning branch reaches the recovery target). Off: the bid just rests."),
    _t("pairbtc.pair_rescue_price", "float", 0.0, 0.99, "Pair rescue: other side above", "Pair-lock BTC conditions rule",
       unit="USDC", step=0.01, help="While bought shares are unpaired and the other token's price (mid) rises above this, the "
            "resting bid is cancelled and the bot buys that token for exactly the unpaired shares (UP = DOWN). 0 = off."),
    _t("pairbtc.pair_reprice_drop", "float", 0.0, 0.5, "Opposite bid: re-place lower after a fall of", "Pair-lock BTC conditions rule",
       unit="USDC", step=0.01, help="Once the opposite token's price (mid) has fallen this much since its bid was placed, the bid "
            "is cancelled and placed again LOWER by the same fall, for the shares still unpaired (a trailing bid). 0 = off."),
    _t("pairbtc.pair_reprice_max", "int", 0, 20, "Opposite bid: re-placements per bid", "Pair-lock BTC conditions rule",
       unit="times", step=1, help="At most this many lower re-placements of one opposite bid."),
    _t("pairbtc.pair_pull", "bool", 0, 1, "Pull the bid when the other side turns higher", "Pair-lock BTC conditions rule",
       help="On: that unfilled bid is cancelled at that moment and the recovery takes over. Off: it rests until the close "
            "(and the recovery waits for it)."),
    _t("pairbtc.target_profit", "float", 0.0, 10.0, "Recovery target", "Pair-lock BTC conditions rule", unit="USDC", step=0.1,
       help="The P&L the recovery lifts the higher token's winning branch to, fees and slippage included."),
    _t("pairbtc.cond_rising_window_s", "float", 0.0, 14.0, "Buy only a rising token: over", "Pair-lock BTC conditions rule",
       unit="s", step=1.0, help="The higher-priced token is bought only if its mid now is above its mid this many seconds ago "
            "(by more than the rise below); a flat or falling token is not bought. 0 = off."),
    _t("pairbtc.cond_rising_min", "float", 0.0, 0.10, "Rising token: rise more than", "Pair-lock BTC conditions rule", unit="USDC",
       step=0.005, help="How much the token's mid must have risen over that window (0 = any rise)."),
    _t("pairbtc.cond_rising_long_s", "float", 0.0, 14.0, "Rising token: also over", "Pair-lock BTC conditions rule", unit="s",
       step=1.0, help="The token must also be higher than this many seconds ago (a longer trend; 0 = off)."),
    _t("pairbtc.cond_rising_last_s", "float", 0.0, 5.0, "Rising token: not falling in the last", "Pair-lock BTC conditions rule",
       unit="s", step=0.5, help="... and not lower than it was this many seconds ago (0 = off)."),
    _t("pairbtc.cond_rising_spot", "bool", 0, 1, "Rising token: BTC moving its way", "Pair-lock BTC conditions rule",
       help="... and BTC moving the token's way over the longer window (UP: BTC up, DOWN: BTC down)."),
    _t("pairbtc.cond_high_min", "float", 0.0, 0.99, "High band: ask above", "Pair-lock BTC conditions rule", unit="USDC", step=0.01,
       help="ALSO buy the higher-priced token when its ask is above this and under the next value (rising, enough time left; "
            "the BTC - strike band does not apply), again after each fill, with no other-side bid. 0 / 0 = off."),
    _t("pairbtc.cond_high_max", "float", 0.0, 0.99, "High band: ask under", "Pair-lock BTC conditions rule", unit="USDC", step=0.01,
       help="The high band's upper bound (0 = the high band is off)."),
    _t("pairbtc.cond_high_max_entries", "int", 1, 50, "High band: repeat count", "Pair-lock BTC conditions rule", unit="buys",
       step=1, help="How many high-band buys one market may get (buys per market). Each waits for the previous one to fill, "
            "and with the opposite bid on, for that bid to fill too."),
    _t("pairbtc.cond_high_pair_bid", "bool", 0, 1, "High band: opposite bid", "Pair-lock BTC conditions rule",
       help="On: after each high-band fill a resting bid for the other token at the pair sum minus the price paid "
            "(0.95 - 0.88 = 0.07); the next high-band buy waits until it fills. Off: no bid."),
    _t("pairbtc.cond_first_buy_delay_s", "float", 0.0, 120.0, "First buy: not before", "Pair-lock BTC conditions rule", unit="s",
       step=1.0, help="A market's first buy (by any entry rule) waits at least this many seconds after the market opens. "
            "Later buys in the market are not delayed. 0 = at once."),
    _t("pairbtc.cond_open_buy", "bool", 0, 1, "Market open buy mode", "Pair-lock BTC conditions rule",
       help="On: within the first seconds after a market opens (next row), while the market holds nothing, buy the "
            "higher-priced token if its ask is in the open-buy band, with its opposite bid as usual. The BTC - strike band "
            "does not apply to this buy, but the rising checks DO: the token bought must be rising now. The other entry "
            "rules keep working beside it."),
    _t("pairbtc.cond_open_window_s", "float", 1.0, 120.0, "Market open buy: within", "Pair-lock BTC conditions rule", unit="s",
       step=1.0, help="Seconds after the market opens in which the open buy may happen (default 20)."),
    _t("pairbtc.cond_open_min", "float", 0.05, 0.99, "Market open buy: ask at least", "Pair-lock BTC conditions rule", unit="USDC",
       step=0.01, help="The higher token's ask must be at least this (default 0.50) ..."),
    _t("pairbtc.cond_open_max", "float", 0.05, 0.99, "Market open buy: ask at most", "Pair-lock BTC conditions rule", unit="USDC",
       step=0.01, help="... and at most this (default 0.70)."),
    _t("pairbtc.cond_after_buy", "bool", 0, 1, "After-open buy mode", "Pair-lock BTC conditions rule",
       help="On: the regular conditions entry (ask band, BTC - strike, time left, rising checks; the token chosen by "
            "'Entry buys the LOWER token') buys during the market. Off: it never buys."),
    _t("pairbtc.cond_after_delay_s", "float", 0.0, 280.0, "After-open buy: not before", "Pair-lock BTC conditions rule", unit="s",
       step=1.0, help="The after-open buy happens only this many seconds or more after the market opens (0 = from the open; "
            "20 = after the market-open buy window)."),
    _t("pairbtc.cond_high_buy", "bool", 0, 1, "High price buy mode (over 0.85)", "Pair-lock BTC conditions rule",
       help="On: the high band buys (the higher token with its ask inside the high band, default 0.85-0.92, repeated up to "
            "the repeat count). Off: it never buys."),
    _t("pairbtc.cond_buy_lower", "bool", 0, 1, "Entry buys the LOWER token", "Pair-lock BTC conditions rule",
       help="On: the conditions are judged on the higher-priced token, then the other (lower-priced) token is bought, "
            "with its opposite bid for the higher token. The market-open buy then also buys the lower token, and only "
            "while it is rising (the rising checks). Off: the higher token is bought."),
    _t("pairbtc.cond_high_buy_lower", "bool", 0, 1, "High band buys the LOWER token", "Pair-lock BTC conditions rule",
       help="On: when the higher token is in the high band, the other (lower-priced, ~0.08-0.15) token is bought instead."),
    _t("pairbtc.late_enabled", "bool", 0, 1, "Late buy", "Pair-lock BTC conditions rule",
       help="On: in the last seconds buy the token priced above the late-buy price. Off: no late buy (and so no late hedge)."),
    _t("pairbtc.late_window_s", "float", 0.0, 60.0, "Late buy: time left under", "Pair-lock BTC conditions rule", unit="s", step=1.0,
       help="The late buy is only when fewer seconds than this are left (the bot sends no buy under 10 s)."),
    _t("pairbtc.late_min_mid", "float", 0.5, 0.99, "Late buy: price above", "Pair-lock BTC conditions rule", unit="USDC", step=0.01,
       help="... and the higher token's price (mid) is above this."),
    _t("pairbtc.late_shares", "float", 5.0, 100.0, "Late buy size", "Pair-lock BTC conditions rule", unit="shares", step=1.0,
       help="Shares of the higher token the late buy takes (one order per market)."),
    _t("pairbtc.late_hedge_trigger", "float", 0.0, 0.99, "Late hedge: price falls under", "Pair-lock BTC conditions rule",
       unit="USDC", step=0.01, help="After the late buy, if its token's price (mid) falls under this, the bot bids for the "
            "other token. 0 = off."),
    _t("pairbtc.late_hedge_price", "float", 0.01, 0.99, "Late hedge bid", "Pair-lock BTC conditions rule", unit="USDC", step=0.01,
       help="The price of that bid, for the late shares, until the close."),
    _t("pairbtc.max_recoveries", "int", 0, 20, "Recoveries per market", "Pair-lock BTC conditions rule", unit="times", step=1,
       help="How many times the bot recovers a falling token in one market (0 = no limit). One recovery is the buys "
            "toward one side while it stays the higher price; after the last one the market is held to settlement."),
    _t("pairbtc.max_market_loss", "float", 0.0, 100.0, "Max loss per market", "Pair-lock BTC conditions rule", unit="USDC", step=0.5,
       help="0 = off. When a loss recovery would leave the other outcome worse than minus this, the bot stops chasing "
            "and LOCKS the market instead: it buys the side with fewer shares until UP = DOWN, so the market's result is "
            "fixed whoever wins, and makes no more recovery buys there. The late buy is trimmed so it cannot break this."),
    _t("pairbtc.recovery_min_price", "float", 0.0, 0.99, "Recovery: lowest price", "Pair-lock BTC conditions rule", unit="USDC", step=0.01,
       help="A recovery is bought only once the token it buys costs MORE than this (0 = off: as soon as it turns "
            "higher). Below it the bot waits; a swing that turns back first needs no recovery."),
    _t("pairbtc.recovery_max_price", "float", 0.50, 1.00, "Recovery: highest price", "Pair-lock BTC conditions rule", unit="USDC", step=0.01,
       help="When the token the recovery would buy costs more than this, the bot locks the market instead (1 = off). "
            "At high prices a recovery needs many shares for little gain per share."),
    _t("pairbtc.lock_left_s", "float", 0.0, 300.0, "Recovery: lock with time left under", "Pair-lock BTC conditions rule", unit="s", step=5.0,
       help="With fewer seconds left than this, the bot locks the market instead of recovering (0 = off)."),
    # ---- the UNFILLED-LEG RECOVERY (strategy/pairlock_ulr.py; the user's 2026-10-05 spec)
    _t("pairbtc.ulr_enabled", "bool", 0, 1, "Unfilled-leg recovery", "Pair-lock BTC unfilled-leg recovery",
       help="On: a first leg whose other-side bid did not fill gets a PARTIAL opposite position as a trade with its own "
            "take-profit, stop and signal exits once the market clearly moves against it. While on, the loss recovery "
            "above does not buy and the bid is never pulled."),
    _t("pairbtc.ulr_min_move", "float", 0.0, 0.5, "Opposite ask above the bid by", "Pair-lock BTC unfilled-leg recovery", unit="USDC",
       step=0.01, help="The hedge counts as failed once the other token's ask is this far above the resting bid's price."),
    _t("pairbtc.ulr_min_time_remaining_s", "float", 10.0, 300.0, "No new recovery under", "Pair-lock BTC unfilled-leg recovery",
       unit="s", step=5.0, help="No recovery trade is opened with fewer seconds left than this."),
    _t("pairbtc.ulr_initial_ratio", "float", 0.05, 1.0, "Recovery size ratio", "Pair-lock BTC unfilled-leg recovery", unit="x",
       step=0.05, help="Shares bought = this x the unhedged first-leg shares (0.30: 6 of 20), never under the venue minimum."),
    _t("pairbtc.ulr_take_profit", "float", 0.01, 0.5, "Recovery take profit", "Pair-lock BTC unfilled-leg recovery", unit="USDC",
       step=0.01, help="Sell the recovery when its token's bid is this far above the entry (and the sale nets a profit after fees)."),
    _t("pairbtc.ulr_stop_loss", "float", 0.01, 0.5, "Recovery stop loss", "Pair-lock BTC unfilled-leg recovery", unit="USDC",
       step=0.01, help="Sell the recovery when its token's bid is this far under the entry."),
    _t("pairbtc.ulr_expected_move", "float", 0.0, 0.5, "Expected move", "Pair-lock BTC unfilled-leg recovery", unit="USDC", step=0.01,
       help="The move the trade bets on. It must exceed the spread + both fees + slippage + the safety margin, or no entry."),
    _t("pairbtc.ulr_exit_before_close_s", "float", 10.0, 120.0, "Sell the recovery with time left under", "Pair-lock BTC unfilled-leg recovery",
       unit="s", step=5.0, help="An open recovery is sold at the bid once fewer seconds than this are left (the bot sends no order under 10 s)."),
    _t("pairbtc.ulr_require_spot", "bool", 0, 1, "Require spot confirmation", "Pair-lock BTC unfilled-leg recovery",
       help="On: BTC must be moving against the first leg too (fresh spot data); stale spot blocks the entry."),
    _t("pairbtc.ulr_signal_exit", "bool", 0, 1, "Exit on signal reversal", "Pair-lock BTC unfilled-leg recovery",
       help="On: sell the recovery when its token's momentum turns down and the first leg's turns up, before TP or SL."),
    _t("pairbtc.ulr_max_position_size", "float", 5.0, 200.0, "Recovery: max shares", "Pair-lock BTC unfilled-leg recovery", unit="shares",
       step=1.0, help="A recovery position never exceeds this many shares."),
    _t("pairbtc.ulr_max_total_exposure", "float", 0.0, 500.0, "Recovery: max open USDC", "Pair-lock BTC unfilled-leg recovery", unit="USDC",
       step=5.0, help="All open recovery positions together may not cost more than this."),
    # ==== the triangle strategy (cfg.triangle; TPB_TRIANGLE_* at start; only a triangle bot has these) ====
    _t("triangle.pair_cost", "float", 0.0, 1.00, "Pair total (Up + Down)", "Triangle pair", unit="USDC", step=0.01,
       help="After every buy, the bot places a resting buy of the OTHER side at this total minus the price "
            "paid (e.g. 0.96 - 0.70 = 0.26). If it fills, the pair cost this much and pays 1.00 at settlement. "
            "Buys stop one cent below it, so every buy can get its pair. Applies to new buys; pairs already "
            "resting keep their price. 0 = no pair orders at all (every buy unhedged)."),
    _t("triangle.hedge_mode", "choice", 0, 1, "Hedge mode", "Triangle pair", options=("resting", "executable", "ev"),
       help="resting: after a buy, a resting buy of the other side at the pair total minus the price paid (it may "
            "never fill). executable: buy only when the pair can be completed now (both asks + fees + slippage "
            "within the max pair cost), then buy the other side at once with a fill-or-kill. ev: after a buy, "
            "take the other side (fill-or-kill) only when the certain pair beats the entry model's P(win) by the margin."),
    _t("triangle.hedge_ev_margin", "float", 0.0, 0.5, "Hedge EV margin", "Triangle pair", unit="prob", step=0.01,
       help="ev mode: 1 - other ask - fee must exceed P(win) by this much before the second leg is bought."),
    _t("triangle.entry_min_ev", "float", -0.2, 0.5, "Entry min EV", "Triangle entry model", unit="prob", step=0.005,
       help="With the entry model on: buy only when P(win) - expected fill - fee is at least this (per share). "
            "The locked study's threshold applies until this is set."),
    _t("triangle.max_pair_cost", "float", 0.5, 1.0, "Max pair cost (executable mode)", "Triangle pair", unit="USDC", step=0.01,
       help="Executable mode only: the side's ask + the other side's ask + both fees + slippage must be at most "
            "this, or the triangle is not bought."),
    _t("triangle.flow_max_token_move", "float", -1.0, 0.2, "No chasing: max rise already seen", "Triangle pair", unit="USDC", step=0.01,
       help="Skip a signal when the token's ask has already risen by more than this over the last second (the "
            "reprice has happened; buying now is the top). 0 = skip on any rise, -1 = off."),
    _t("triangle.limit_offset", "float", 0.0, 0.99, "Max price above the ask", "Triangle pair", unit="USDC", step=0.01,
       help="A triangle's buy is fill-or-kill at the side's ask seen at the triangle plus this. 0.03 = never "
            "pay more than 3 cents over that ask; a bigger number fills more orders but pays more (0.99 = "
            "market order). Orders the market moves past are killed and their triangle is drawn hollow."),
    _t("triangle.min_price", "float", 0.0, 0.5, "Lowest price to buy", "Triangle pair", unit="USDC", step=0.01,
       help="Never buy a token whose ask is below this (0.20 = nothing under 20 cents). The other-side ask that follows a buy is exempt."),
    _t("triangle.max_price", "float", 0.5, 1.0, "Highest price to buy", "Triangle pair", unit="USDC", step=0.01,
       help="Never buy a token whose ask is above this, and the order's limit never exceeds it. With the "
            "pair total on, buys also stop one cent below the pair total."),
    _t("triangle.hz_enabled", "bool", 0, 1, "Buy high tokens that keep rising", "Triangle pair",
       help="A token whose ask is above the high-zone price and higher than 5 seconds ago buys the order size "
            "every few seconds, with or without a signal; each buy gets its other-side ask."),
    _t("triangle.hz_min_price", "float", 0.5, 0.99, "High zone starts at", "Triangle pair", unit="USDC", step=0.01,
       help="The ask a token must be above for the high-zone buying."),
    _t("triangle.hz_every_s", "float", 1.0, 60.0, "High zone: buy every", "Triangle pair", unit="s", step=1.0,
       help="Seconds between high-zone buys in one market."),
    _t("triangle.wait_for_pair", "bool", 0, 1, "Wait for the pair before buying", "Triangle pair",
       help="On: no new buy while any pair order is still resting. Off: every triangle buys at once."),
    _t("micro.max_order_usdc", "float", 1.0, 100.0, "Ceiling per order", "Real-money ceilings",
       unit="USDC", step=0.5, applies_to="live", ceiling=True,
       help="No per-order cap anywhere above may exceed this."),
    _t("micro.max_open_usdc", "float", 1.0, 1000.0, "Ceiling on open notional", "Real-money ceilings",
       unit="USDC", step=1, applies_to="live", ceiling=True,
       help="No exposure cap anywhere above may exceed this."),
    _t("micro.max_daily_loss_usdc", "float", 1.0, 1000.0, "Ceiling on daily loss", "Real-money ceilings",
       unit="USDC", step=1, applies_to="live", ceiling=True,
       help="No daily-loss limit anywhere above may exceed this."),
    _t("micro.max_orders_per_hour", "int", 1, 1000, "Ceiling on buys per hour", "Real-money ceilings",
       step=1, applies_to="live", ceiling=True,
       help="No hourly order limit above may exceed this."),
    _t("micro.max_concurrent_positions", "int", 1, 20, "Ceiling on open positions", "Real-money ceilings",
       step=1, applies_to="live", ceiling=True,
       help="No open-position limit above may exceed this."),
    _t("micro.max_spread", "float", 0.005, 0.2, "Ceiling on spread", "Real-money ceilings", unit="prob",
       step=0.005, applies_to="live", ceiling=True,
       help="No maximum spread above may exceed this."),
    _t("micro.max_slippage", "float", 0.0, 0.1, "Ceiling on slippage", "Real-money ceilings", unit="prob",
       step=0.005, applies_to="live", ceiling=True,
       help="No maximum slippage above may exceed this."),
)}

#: a spot-lag budget value, and the risk limit it is written into when set
BUDGET_TO_RISK: dict[str, str] = {
    "spot_lag.max_simultaneous_positions": "risk.max_concurrent_positions",
    "spot_lag.max_total_exposure_usdc": "risk.max_total_exposure_usdc",
    "spot_lag.max_daily_loss_usdc": "risk.max_daily_loss_usdc",
    "spot_lag.max_order_usdc": "risk.max_position_usdc",
}

#: Start-time only. Listed so the page can show them and say a restart is due.
START_ARGS: dict[str, dict[str, Any]] = {
    "balance": {"label": "Bankroll", "kind": "float", "unit": "USDC",
                "help": "What the risk caps scale to. In live mode it is capped to the account balance."},
    "assets": {"label": "Assets", "kind": "text",
               "help": "Comma separated, e.g. BTC,ETH. Empty means every asset the venue lists."},
    "exchanges": {"label": "Spot exchanges", "kind": "text",
                  "help": "Sources for the composite spot price."},
}

GROUP_ORDER = ("Cheapside basket", "Cheapside basket gates", "Cheapside basket risk",
               "Cheapside recovery", "Cheapside individual entries",
               "Cross-market arb", "Cross-market arb gates", "Cross-market arb risk",
               "Cross-market correlation", "Cross-market correlation model",
               "Cross-market correlation gates", "Cross-market correlation risk",
               "Spot-lag signal", "Spot-lag entry", "Spot-lag orders", "Spot-lag exits", "Spot-lag speed",
               "Spot-lag budget",
               "Leader-pair rule 1", "Leader-pair pairs", "Leader-pair accumulation", "Leader-pair falling / recovery", "Leader-pair favourite fix", "Leader-pair cheap hedge", "Leader-pair execution", "Leader-pair limits",
               "Rising-recovery mode", "Rising-recovery favourite fix", "Rising-recovery opt mode", "Rising-recovery orders", "Rising-recovery limits",
               "Pair-lock BTC conditions rule", "Pair-lock BTC unfilled-leg recovery", "Pair-lock swing recovery", "Pair-lock open rule", "Triangle pair", "Triangle entry model", "Edge", "Book filters", "Entry style", "Timing", "Exits", "Sizing",
               "Circuit breakers", "Real-money caps", "Real-money ceilings")


def spec() -> list[dict[str, Any]]:
    """The tunable catalogue, as the dashboard renders it."""
    out = []
    for group in GROUP_ORDER:
        for t in TUNABLES.values():
            if t.group != group:
                continue
            out.append({"key": t.key, "kind": t.kind, "lo": t.lo, "hi": t.hi, "step": t.step,
                        "label": t.label, "group": t.group, "unit": t.unit, "help": t.help,
                        "applies_to": t.applies_to, "options": list(t.options),
                        "not_for": list(t.not_for), "ceiling": t.ceiling})
    return out


def validate(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(values that will be applied, complaints about the rest)."""
    clean: dict[str, Any] = {}
    errors: list[str] = []
    if not isinstance(patch, dict):
        return {}, ["expected an object of key -> value"]
    for key, raw in patch.items():
        tunable = TUNABLES.get(key)
        if tunable is None:
            errors.append(f"{key}: not a tunable")
            continue
        value, err = tunable.coerce(raw)
        if err:
            errors.append(err)
        else:
            clean[key] = value
    return clean, errors


def _coherent(values: dict[str, Any]) -> list[str]:
    """Pairs that would contradict each other. Reported, not silently fixed."""
    warn = []
    lo, hi = values.get("engine.min_trade_price"), values.get("engine.max_trade_price")
    if lo is not None and hi is not None and lo >= hi:
        warn.append("minimum price is not below maximum price")
    start, end = values.get("engine.trade_window_start_s"), values.get("engine.trade_window_end_s")
    if start is not None and end is not None and start <= end:
        warn.append("the trade window opens at or before it closes, so nothing can trade")
    floor, cap = values.get("risk.min_order_usdc"), values.get("risk.max_position_usdc")
    if floor is not None and cap is not None and floor > cap:
        warn.append("the minimum order is larger than the per-market cap, so nothing can size")
    order_cap = values.get("caps.max_order_usdc")
    if order_cap is not None and floor is not None and order_cap < floor:
        warn.append(f"the real-money cap of {order_cap:g} USDC is below the {floor:g} USDC "
                    "minimum order, so every order will be refused")
    lo, hi = values.get("spot_lag.min_ask"), values.get("spot_lag.max_ask")
    if lo is not None and hi is not None and lo >= hi:
        warn.append("the spot-lag minimum ask is not below its maximum ask, so nothing can trade")
    start, end = values.get("spot_lag.max_time_remaining_s"), values.get("spot_lag.min_time_remaining_s")
    if start is not None and end is not None and start <= end:
        warn.append("spot-lag starts entering at or after it stops, so nothing can trade")
    shares, ask = values.get("spot_lag.size_shares"), values.get("spot_lag.max_ask")
    for key in ("spot_lag.max_order_usdc", "micro.max_order_usdc", "caps.max_order_usdc"):
        cap = values.get(key)
        if cap is not None and cap < 5 * 0.5:
            warn.append(f"{TUNABLES[key].label} of {cap:g} USDC cannot buy the 5-share minimum above "
                        "0.50, so most orders will be refused")
        elif cap is not None and shares is not None and ask is not None and shares * ask > cap + 1e-9:
            warn.append(f"{shares:g} shares at up to {ask:g} costs {shares * ask:.2f} USDC, above the "
                        f"{cap:g} USDC {TUNABLES[key].label.lower()}: orders there are trimmed or refused")
    markets = values.get("cheapside.portfolio_basket_market_count")
    cheap_count = values.get("cheapside.portfolio_basket_buy_count")
    if markets is not None and cheap_count is not None and cheap_count + 1 != markets:
        warn.append("Cheapside basket buys the selected cheap outcomes plus one leader, so the cheap count must be one below the market count")
    leader_min = values.get("cheapside.leader_min_ask")
    leader_max = values.get("cheapside.leader_max_ask")
    if leader_min is not None and leader_max is not None and leader_min >= leader_max:
        warn.append("Cheapside individual leader minimum ask must be below its maximum ask")
    bmin = values.get("cheapside.portfolio_basket_min_leader_ask")
    bmax = values.get("cheapside.portfolio_basket_max_leader_ask")
    babort = values.get("cheapside.portfolio_basket_abort_leader_ask")
    if bmin is not None and bmax is not None and bmin >= bmax:
        warn.append("Cheapside basket favourite arm minimum ask must be below its maximum ask")
    if bmax is not None and babort is not None and bmax > babort:
        warn.append("Cheapside basket favourite abort ask should be at or above the arm maximum")
    return warn


class ControlFile:
    """Read and write ``data/controls.json``. Safe to call from either process."""

    def __init__(self, path: Path | str = CONTROL_PATH) -> None:
        self.path = Path(path)
        self._mtime: float | None = None

    # ------------------------------------------------------------- reading

    def read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"updated_ts": None, **{s: {} for s in SCOPES}}
        if not isinstance(raw, dict):
            return {"updated_ts": None, **{s: {} for s in SCOPES}}
        out: dict[str, Any] = {"updated_ts": raw.get("updated_ts")}
        for scope in SCOPES:
            section = raw.get(scope)
            out[scope] = section if isinstance(section, dict) else {}
        return out

    def changed(self) -> bool:
        """Has the file been written since we last looked? Cheap enough to poll."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._mtime = mtime
            return True
        return False

    def merged(self, scope: str) -> tuple[dict[str, Any], list[str]]:
        """``all`` overlaid with one scope's own section, validated."""
        raw = self.read()
        combined = dict(raw.get("all") or {})
        if scope in SCOPES and scope != "all":
            combined.update(raw.get(scope) or {})
        return validate(combined)

    # ------------------------------------------------------------- writing

    def write_scope(self, scope: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge ``patch`` into one scope. Returns what was stored plus notes."""
        if scope not in SCOPES:
            return {"ok": False, "errors": [f"unknown scope {scope!r}"], "values": {}, "warnings": []}
        clean, errors = validate(patch)
        raw = self.read()
        section = dict(raw.get(scope) or {})
        section.update(clean)
        payload = {"updated_ts": time.time(),
                   **{s: (section if s == scope else (raw.get(s) or {})) for s in SCOPES}}
        self._atomic_write(payload)
        merged, _ = self.merged("live" if scope == "live" else "paper" if scope == "paper" else "all")
        return {"ok": not errors, "errors": errors, "values": clean,
                "warnings": _coherent(merged or clean), "scope": scope}

    def replace_scope(self, scope: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Set one scope to exactly ``patch``, so emptied fields are removed.

        ``write_scope`` merges, which can only ever add overrides. The page
        needs to be able to take one away again, and does that by sending the
        whole section it is showing.
        """
        if scope not in SCOPES:
            return {"ok": False, "errors": [f"unknown scope {scope!r}"], "values": {}, "warnings": []}
        clean, errors = validate(patch)
        raw = self.read()
        payload = {"updated_ts": time.time(),
                   **{s: (clean if s == scope else (raw.get(s) or {})) for s in SCOPES}}
        self._atomic_write(payload)
        merged, _ = self.merged(scope)
        return {"ok": not errors, "errors": errors, "values": clean,
                "warnings": _coherent(merged), "scope": scope}

    def clear_scope(self, scope: str) -> dict[str, Any]:
        """Drop one scope's overrides so the bot falls back to its defaults."""
        if scope not in SCOPES:
            return {"ok": False, "errors": [f"unknown scope {scope!r}"]}
        raw = self.read()
        payload = {"updated_ts": time.time(),
                   **{s: ({} if s == scope else (raw.get(s) or {})) for s in SCOPES}}
        self._atomic_write(payload)
        return {"ok": True, "errors": [], "scope": scope,
                "note": "the bot keeps the values already applied until it restarts"}

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def _holders(cfg, exchange=None, strategy=None, micro=None, triangle=None, pairlock=None,
             pairbtc=None, lp=None, rtr=None, cheapside=None, cma=None, cmc=None) -> dict[str, Any]:
    """Where each tunable target lives. ``strategy`` is the running spot-lag config
    (``engine.scfg``) and ``micro`` the micro-live envelope; both are passed only by
    a bot that has them, so a paper bot never reports or applies a ceiling it lacks."""
    return {"engine": getattr(cfg, "engine", None),
            "risk": getattr(cfg, "risk", None),
            "caps": getattr(exchange, "caps", None),
            "spot_lag": strategy,
            "micro": micro,
            "triangle": triangle,
            "pairlock": pairlock,
            "pairbtc": pairbtc,
            # LEADER_PAIR's and RISING-RECOVERY's own configs (2026-10-13): each bot passes only its own
            "lp": lp,
            "rtr": rtr,
            "cheapside": cheapside,
            "cma": cma,
            "cmc": cmc}


def _clean(t: Tunable, value: Any) -> Any:
    if t.kind == "bool":
        return bool(value)
    if t.kind in ("choice", "list"):
        return str(value)
    return int(value) if t.kind == "int" else float(value)


def current_values(cfg, exchange=None, strategy=None, micro=None, triangle=None, pairlock=None,
                   pairbtc=None, lp=None, rtr=None, cheapside=None, cma=None, cmc=None) -> dict[str, Any]:
    """What the bot is running with right now, keyed like the tunables."""
    out: dict[str, Any] = {}
    holders = _holders(cfg, exchange, strategy, micro, triangle, pairlock, pairbtc, lp, rtr, cheapside, cma, cmc)
    for key, t in TUNABLES.items():
        holder = holders.get(t.target)
        if holder is None or not hasattr(holder, t.attr):
            continue
        out[key] = _clean(t, getattr(holder, t.attr))
    return out


def _fmt(t: Tunable, v: Any) -> str:
    if t.kind == "bool":
        return str(bool(v)).lower()
    if t.kind in ("choice", "list"):
        return str(v) or "(none)"
    return f"{v:g}"


def apply_to(values: dict[str, Any], cfg, exchange=None, strategy=None, micro=None,
             triangle=None, pairlock=None, pairbtc=None, lp=None, rtr=None, cheapside=None,
             cma=None, cmc=None) -> list[str]:
    """Set the validated values on the live objects. Returns what changed.

    TPB_CONTROLS_IGNORE (comma-separated keys, or ``group.*``): dashboard rows this bot leaves alone -- its own
    config decides them (2026-10-07: the low-price pair-lock app runs the spec's parameters, not the shared
    pairlock.* rows the other pair-lock bots are steered with)."""
    changed: list[str] = []
    holders = _holders(cfg, exchange, strategy, micro, triangle, pairlock, pairbtc, lp, rtr, cheapside, cma, cmc)
    ignore = {x.strip() for x in os.environ.get("TPB_CONTROLS_IGNORE", "").split(",") if x.strip()}
    for key, value in values.items():
        t = TUNABLES.get(key)
        if t is None:
            continue
        if key in ignore or f"{key.split('.', 1)[0]}.*" in ignore:
            continue
        holder = holders.get(t.target)
        if holder is None or not hasattr(holder, t.attr):
            continue
        before = getattr(holder, t.attr)
        if isinstance(before, bool) or t.kind == "bool":
            same = bool(before) == bool(value)
        elif t.kind in ("choice", "list"):
            same = str(before) == str(value)
        else:
            same = abs(float(before) - float(value)) < 1e-12
        if same:
            continue
        try:
            setattr(holder, t.attr, value)
        except dataclasses.FrozenInstanceError:
            # the micro envelope is frozen so nothing in the trading path can move it
            # by accident; this, the operator's explicit setting, is its one writer
            if t.target != "micro":
                continue
            object.__setattr__(holder, t.attr, value)
        except (AttributeError, TypeError, ValueError):
            continue
        changed.append(f"{t.label}: {_fmt(t, before)} -> {_fmt(t, value)}")
    return changed


# ------------------------------------------------------------- kill switch


def kill_active(path: Path | str = KILL_PATH) -> bool:
    return Path(path).exists()


def set_kill(active: bool, path: Path | str = KILL_PATH) -> bool:
    """Create or remove the kill file. Returns the state afterwards."""
    p = Path(path)
    try:
        if active:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"halted from the dashboard at {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                         encoding="utf-8")
        else:
            p.unlink(missing_ok=True)
    except OSError:
        pass
    return p.exists()
