"""CROSS_MARKET_ARB configuration (``TPB_CMA_*``). Paper-only; disabled by default."""
from __future__ import annotations

from dataclasses import dataclass

from ..spot_lag import SpotLagConfig

ENABLE_ENV = "TPB_CMA_ENABLED"
STRATEGY_ID = "cross_market_arb"
DEFAULT_ASSETS = "BTC,ETH,SOL,XRP,DOGE,BNB,HYPE"


@dataclass(slots=True)
class CrossMarketArbConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_CMA_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}

    #: Master switch; from_env also reads ENABLE_ENV. Default OFF.
    enabled: bool = False
    #: Deployment mode label for this bot's own docs/status (process still uses --mode).
    mode: str = "paper"

    markets: str = DEFAULT_ASSETS
    assets: str = DEFAULT_ASSETS
    exclude_assets: str = ""

    #: Guaranteed net profit floor: min_payout - all_in_cost must exceed this.
    minimum_net_profit: float = 0.15
    #: Maker pair arm only when UP bid + DOWN bid is at or below this (0.97 => $0.15 lock on 5sh).
    maximum_maker_sum: float = 0.97
    maximum_basket_notional: float = 80.0
    maximum_market_exposure: float = 40.0
    maximum_quote_age_ms: float = 1000.0
    maximum_timestamp_skew_ms: float = 1000.0
    maximum_spread: float = 0.05
    maximum_slippage: float = 0.01
    maximum_incomplete_exposure: float = 40.0
    daily_loss_limit: float = 15.0
    minimum_cash_reserve: float = 5.0

    #: Extra risk controls enforced on the order path.
    maximum_capital_per_opportunity: float = 80.0
    maximum_total_outstanding_notional: float = 120.0
    maximum_concurrent_opportunities: int = 3
    maximum_worst_case_loss_during_execution: float = 40.0
    max_shares_per_leg: float = 25.0
    share_step: float = 5.0

    #: Entry style: maker (GTC pair bids — the path that can actually clear on live
    #: books), taker (FAK at asks — only when ask_up+ask_down+fees < 1), or both.
    entry_mode: str = "maker"
    #: Shares per side for a maker pair lock (venue minimum 5).
    maker_shares: float = 5.0
    #: Improve the bid by one tick when the spread is at least this wide.
    maker_improve_min_spread: float = 0.02
    #: Cancel resting maker pairs once fewer than this many seconds remain.
    maker_cancel_time_remaining_s: float = 30.0
    #: Max maker pair markets armed at once.
    max_maker_markets: int = 3

    #: Residual exposure when partial fills destroy the guarantee: hold | alert_only
    residual_exposure_policy: str = "hold"
    #: Seconds remaining required to arm / continue execution.
    min_time_remaining_s: float = 60.0
    max_time_remaining_s: float = 295.0
    #: Extra buffer before close for reconcile.
    execution_buffer_s: float = 15.0

    order_type: str = "FAK"
    max_slippage: float = 0.01
    max_spread: float = 0.05
    max_quote_age_ms: float = 1000.0
    slippage: float = 0.01
    size_shares: float = 5.0
    size_mode: str = "fixed"
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0

    max_simultaneous_positions: int = 14
    max_entries_per_market: int = 2
    max_attempts_per_market: int = 6
    max_order_usdc: float = 50.0
    max_total_exposure_usdc: float = 120.0
    max_daily_loss_usdc: float = 15.0
    max_buys_per_hour: int = 120

    #: Retry unfinished legs
    leg_retry_ms: float = 250.0
    #: Revalidate opportunity immediately before each order submission
    revalidate_before_submit: bool = True
    fast_feed: bool = False

    def asset_list(self) -> tuple[str, ...]:
        raw = self.markets or self.assets
        return tuple(a.strip().upper() for a in raw.split(",") if a.strip())

    def apply_budget(self, risk_cfg, order_ceiling: float | None = None) -> list[str]:
        # Align shared risk manager with this bot's dedicated caps without
        # loosening ceilings already set tighter elsewhere.
        self.max_total_exposure_usdc = min(
            self.max_total_exposure_usdc, self.maximum_total_outstanding_notional)
        self.max_daily_loss_usdc = min(self.max_daily_loss_usdc, self.daily_loss_limit)
        changed = SpotLagConfig.apply_budget(self, risk_cfg, order_ceiling)
        required = max(2 * len(self.asset_list()), int(self.max_simultaneous_positions))
        if risk_cfg.max_concurrent_positions < required:
            old = risk_cfg.max_concurrent_positions
            risk_cfg.max_concurrent_positions = required
            changed.append(f"max_concurrent_positions: {old} -> {required} (cma legs)")
        for attr, cap in (
            ("max_total_exposure_usdc", self.maximum_total_outstanding_notional),
            ("max_daily_loss_usdc", self.daily_loss_limit),
            ("max_position_usdc", self.maximum_market_exposure),
            ("max_asset_exposure_usdc", self.maximum_market_exposure),
        ):
            old = getattr(risk_cfg, attr)
            if old > cap:
                setattr(risk_cfg, attr, cap)
                changed.append(f"{attr}: {old} -> {cap} (cma)")
        return changed
