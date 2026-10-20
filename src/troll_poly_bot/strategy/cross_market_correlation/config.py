"""CROSS_MARKET_CORRELATION configuration (``TPB_CMC_*``). Paper-only; disabled by default."""
from __future__ import annotations

from dataclasses import dataclass

from ..spot_lag import SpotLagConfig

ENABLE_ENV = "TPB_CMC_ENABLED"
STRATEGY_ID = "cross_market_correlation"
DEFAULT_ASSETS = "BTC,ETH,SOL,XRP,DOGE,BNB,HYPE"


@dataclass(slots=True)
class CrossMarketCorrelationConfig(SpotLagConfig):
    ENV_PREFIX = "TPB_CMC_"
    ENABLE_ENV_NAME = ENABLE_ENV
    PROFILES = {"baseline": {}}

    #: Master switch; from_env also reads ENABLE_ENV. Default OFF.
    enabled: bool = False
    #: Deployment mode label (process still uses --mode). Never auto-enables live.
    mode: str = "paper"

    markets: str = DEFAULT_ASSETS
    assets: str = DEFAULT_ASSETS
    exclude_assets: str = ""
    #: Comma list of assets that may only be the cheaper leg of a pair (lower ask).
    #: Blocks high-price purchases on risky alts while still allowing them as the
    #: cheap hedge side. Mutually exclusive with ``exclude_assets`` in the UI.
    lower_side_only_assets: str = ""
    #: When on, reject opposite-pair directions whose calibrated wipe risk
    #: (``predict_wipe_risk``) clears ``wipe_skip_threshold``. Market-page toggle.
    skip_pairs_mode_enabled: bool = True
    #: Skip when ``p_wipe >=`` this (held-out default ~0.55 → ~+2pp win rate).
    wipe_skip_threshold: float = 0.55

    #: Operating modes. Guaranteed mode uses worst-case payout proof only.
    #: Statistical mode trades on conservative EV (not risk-free). Paper ecosystem enables it.
    guaranteed_mode_enabled: bool = True
    statistical_mode_enabled: bool = False

    #: Guaranteed: require min(P_s - C) > this over every valid settlement scenario.
    guaranteed_min_net_profit: float = 0.01
    #: Statistical: minimum conservative EV after costs and uncertainty haircut.
    minimum_conservative_ev: float = 0.0
    #: Minimum expected net profit in USDC for a sized position (after fees/slippage).
    minimum_expected_net_profit: float = 0.0
    #: All-in unit cost ceiling for a matched pair (A leg + B leg). Under a symmetric
    #: prior, EV ≈ 1 - cost, so cost must clear this before samples are required.
    maximum_combined_cost: float = 0.98
    #: Floor on the same sum: reject pairs whose two leg unit costs add to less than
    #: this (cheap longshot baskets wipe the stake when wrong).
    minimum_combined_cost: float = 0.60
    #: Cap filled+pending shares on one (slug, side) within the epoch.
    max_shares_per_market_side: float = 15.0
    #: Cap |UP−DOWN| net shares per asset from filled inventory this epoch.
    max_net_shares_per_asset: float = 20.0
    #: After a pair matches, wait this long before re-arming the same pair_key.
    pair_match_cooldown_ms: float = 30_000.0

    #: Historical model gates
    min_sample_size: int = 50
    max_probability_uncertainty: float = 0.15
    #: When under-sampled, still allow statistical trades that clear EV gates under the prior.
    allow_under_sampled_prior: bool = False
    #: Blend book-implied marginals into the joint prior when history is thin.
    use_book_implied_when_under_sampled: bool = True
    #: Dirichlet / Laplace prior strength for joint-outcome shrinkage.
    prior_strength: float = 4.0
    #: Prior mass on agreement cells (UU+DD); rest split across disagreements.
    prior_agreement_bias: float = 0.55
    #: Haircut applied to point EV: EV_cons = EV - z * uncertainty.
    uncertainty_z: float = 1.0
    #: Hard strike/spot gates (paper often joins mid-window without a stamped strike).
    require_strike: bool = True
    require_spot: bool = True

    #: Execution / book gates
    maximum_quote_age_ms: float = 2000.0
    maximum_timestamp_skew_ms: float = 2000.0
    maximum_epoch_skew_s: float = 0.0  # 5m windows must share the same epoch by default
    maximum_spread: float = 0.12
    maximum_slippage: float = 0.015
    minimum_book_depth: float = 5.0
    max_slippage: float = 0.015
    max_spread: float = 0.12
    max_quote_age_ms: float = 2000.0
    slippage: float = 0.015
    max_market_data_age_ms: float = 2000.0

    #: Sizing
    size_shares: float = 5.0
    share_step: float = 5.0
    #: When the pair's leader (higher ask of the two legs) is above this, both
    #: legs size to ``leader_large_shares`` instead of ``size_shares``.
    leader_large_min_price: float = 0.75
    leader_large_shares: float = 10.0
    #: Skip the pair when the leader ask is under this AND that leader token is
    #: in a short-horizon downtrend (mid falling over ``leader_downtrend_lookback_s``).
    leader_block_max_price: float = 0.60
    leader_downtrend_lookback_s: float = 20.0
    #: Mid must have fallen by at least this many dollars over the lookback.
    leader_downtrend_min_drop: float = 0.02
    max_shares_per_leg: float = 25.0
    max_order_size: float = 25.0
    maximum_matched_position_size: float = 25.0
    maximum_capital_per_pair: float = 40.0
    maximum_total_open_exposure: float = 120.0
    maximum_exposure_per_asset: float = 40.0
    maximum_exposure_per_market: float = 40.0
    maximum_unhedged_exposure: float = 25.0
    maximum_simultaneous_positions: int = 6
    max_simultaneous_positions: int = 12
    max_order_usdc: float = 40.0
    max_total_exposure_usdc: float = 120.0

    #: Risk circuit breakers
    maximum_daily_realized_loss: float = 15.0
    maximum_total_drawdown: float = 25.0
    daily_loss_limit: float = 15.0
    max_daily_loss_usdc: float = 15.0
    minimum_cash_reserve: float = 5.0

    #: Timing
    min_time_remaining_s: float = 30.0
    max_time_remaining_s: float = 300.0
    #: Upper bound on seconds-remaining for new entries (300 = allow from window open).
    maximum_time_before_settlement_s: float = 300.0
    execution_buffer_s: float = 10.0
    #: One-leg / residual handling: hold | cancel_alert | hedge_if_edge
    unhedged_policy: str = "cancel_alert"
    leg_retry_ms: float = 250.0
    leg_timeout_ms: float = 3000.0
    max_leg_retries: int = 20
    revalidate_before_submit: bool = True
    order_type: str = "FAK"
    hold_to_settlement: bool = True
    take_profit_cents: float = 0.0
    stop_loss_cents: float = 0.0
    hold_s: float = 300.0
    extend_max_hold_s: float = 0.0
    max_entries_per_market: int = 2
    max_attempts_per_market: int = 8
    max_buys_per_hour: int = 120
    kill_switch: bool = False
    fast_feed: bool = False

    #: Path to optional historical joint-outcome JSON (pair -> counts). Empty = none loaded.
    history_path: str = ""

    def asset_list(self) -> tuple[str, ...]:
        raw = self.markets or self.assets
        return tuple(a.strip().upper() for a in raw.split(",") if a.strip())

    def lower_side_only_set(self) -> set[str]:
        return {a.strip().upper() for a in self.lower_side_only_assets.split(",") if a.strip()}

    def apply_budget(self, risk_cfg, order_ceiling: float | None = None) -> list[str]:
        self.max_total_exposure_usdc = min(
            self.max_total_exposure_usdc, self.maximum_total_open_exposure)
        self.max_daily_loss_usdc = min(self.max_daily_loss_usdc, self.maximum_daily_realized_loss,
                                       self.daily_loss_limit)
        self.max_simultaneous_positions = max(
            self.max_simultaneous_positions, 2 * int(self.maximum_simultaneous_positions))
        changed = SpotLagConfig.apply_budget(self, risk_cfg, order_ceiling)
        # Push CMC budgets onto RiskManager in both directions. SpotLag defaults
        # (e.g. max_position=5, max_epoch=30) otherwise silently block paper fills
        # long before CMC's own pair caps engage.
        for attr, cap in (
            ("max_total_exposure_usdc", self.maximum_total_open_exposure),
            ("max_daily_loss_usdc", self.maximum_daily_realized_loss),
            ("max_position_usdc", self.maximum_exposure_per_market),
            ("max_asset_exposure_usdc", self.maximum_exposure_per_asset),
            ("max_epoch_exposure_usdc", self.maximum_total_open_exposure),
            ("max_drawdown_usdc", self.maximum_total_drawdown),
        ):
            if not hasattr(risk_cfg, attr):
                continue
            old = float(getattr(risk_cfg, attr))
            if abs(old - float(cap)) > 1e-9:
                setattr(risk_cfg, attr, float(cap))
                changed.append(f"{attr}: {old} -> {cap} (cmc)")
        required = max(2 * int(self.maximum_simultaneous_positions),
                       int(self.max_simultaneous_positions))
        if risk_cfg.max_concurrent_positions < required:
            old = risk_cfg.max_concurrent_positions
            risk_cfg.max_concurrent_positions = required
            changed.append(f"max_concurrent_positions: {old} -> {required} (cmc legs)")
        return changed
