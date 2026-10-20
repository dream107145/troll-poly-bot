"""CROSS_MARKET_CORRELATION: pair-wise opposite-outcome trading. Paper only."""
from .config import ENABLE_ENV, STRATEGY_ID, CrossMarketCorrelationConfig
from .engine import CrossMarketCorrelationEngine
from .execution import PairPlan, PairState
from .math import (
    JointProbs, classify_candidate, ev_c1, ev_c2, is_guaranteed_arbitrage,
    payoff_a_down_b_up, payoff_a_up_b_down,
)
from .model import JointOutcomeModel
from .predict import (
    DEFAULT_WIPE_SKIP_THRESHOLD, PairPrediction, WipeRisk,
    predict_opposite_pair, predict_wipe_risk, signed_bps,
)
from .scanner import ScanResult, scan_all

__all__ = [
    "ENABLE_ENV", "STRATEGY_ID", "CrossMarketCorrelationConfig",
    "CrossMarketCorrelationEngine", "PairPlan", "PairState",
    "JointProbs", "JointOutcomeModel", "ScanResult", "scan_all",
    "classify_candidate", "ev_c1", "ev_c2", "is_guaranteed_arbitrage",
    "payoff_a_down_b_up", "payoff_a_up_b_down",
    "PairPrediction", "WipeRisk", "predict_opposite_pair", "predict_wipe_risk",
    "signed_bps", "DEFAULT_WIPE_SKIP_THRESHOLD",
]
