"""Scan all unique asset pairs × both trade directions (up to 42 candidates)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..cross_market_arb.math import LegQuote, estimate_leg
from .alignment import AlignmentResult, MarketSnapshot, all_unique_pairs, validate_pair
from .config import CrossMarketCorrelationConfig
from .math import JointProbs, PairCandidateMetrics, classify_candidate, normalize_probs
from .model import JointOutcomeModel, independence_baseline
from .predict import DEFAULT_WIPE_SKIP_THRESHOLD, predict_wipe_risk

#: (recv_ms, mid) samples for short-horizon token trend checks.
MidHist = tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class MarketView:
    asset: str
    slug: str
    snapshot: MarketSnapshot
    up: LegQuote
    down: LegQuote
    time_remaining_s: float
    implied_p_up: float = 0.5  # mid-based or model
    up_mids: MidHist = ()
    down_mids: MidHist = ()
    now_ms: float = 0.0


def _quote_mid(q: LegQuote) -> float:
    return 0.5 * (float(q.bid) + float(q.ask))


def _is_downtrend(
    mids: MidHist,
    now_ms: float,
    lookback_s: float,
    min_drop: float,
) -> bool:
    """True when the latest mid is at least ``min_drop`` below the mid ~lookback ago."""
    if not mids or lookback_s <= 0:
        return False
    now_mid = float(mids[-1][1])
    target = (now_ms if now_ms > 0 else float(mids[-1][0])) - float(lookback_s) * 1000.0
    past: float | None = None
    for ts, mid in mids:
        if float(ts) <= target + 1e-9:
            past = float(mid)
        else:
            break
    if past is None:
        return False
    return now_mid <= past - float(min_drop) + 1e-12


def _mids_for(view: MarketView, side: str) -> MidHist:
    return view.up_mids if side == "UP" else view.down_mids


def _lower_side_only_gate(
    asset_a: str,
    ask_a: float,
    asset_b: str,
    ask_b: float,
    cfg: CrossMarketCorrelationConfig,
) -> str:
    """Assets flagged lower-side-only may only take the cheaper ask of the pair."""
    only = cfg.lower_side_only_set()
    if not only:
        return ""
    a, b = asset_a.upper(), asset_b.upper()
    if a in only and ask_a > ask_b + 1e-12:
        return f"lower_side_only:{a}:{ask_a:.3f}>{ask_b:.3f}"
    if b in only and ask_b > ask_a + 1e-12:
        return f"lower_side_only:{b}:{ask_b:.3f}>{ask_a:.3f}"
    return ""


def _wipe_skip_gate(
    va: MarketView,
    vb: MarketView,
    direction: str,
    cfg: CrossMarketCorrelationConfig,
) -> str:
    """When Skip pairs mode is on, reject directions with high calibrated wipe risk.

    Missing spot/strike → no prediction → allow (do not block the whole book when
    paper joins mid-window without a stamped strike).
    """
    if not cfg.skip_pairs_mode_enabled:
        return ""
    sa, ka = va.snapshot.spot, va.snapshot.strike
    sb, kb = vb.snapshot.spot, vb.snapshot.strike
    if sa is None or ka is None or sb is None or kb is None:
        return ""
    thr = float(cfg.wipe_skip_threshold)
    if thr <= 0.0:
        thr = float(DEFAULT_WIPE_SKIP_THRESHOLD)
    risk = predict_wipe_risk(
        float(sa), float(ka), float(sb), float(kb),
        asset_a=va.asset, asset_b=vb.asset, direction=direction,  # type: ignore[arg-type]
        ask_up_a=float(va.up.ask), ask_up_b=float(vb.up.ask),
    )
    if risk is None or not risk.should_skip(thr):
        return ""
    return f"wipe_skip:p={risk.p_wipe:.3f}>={thr:.2f}"


def _inventory_side_gate(
    slug: str,
    side: str,
    shares: float,
    inventory: dict[tuple[str, str], float],
    cfg: CrossMarketCorrelationConfig,
) -> str:
    """Cap how many shares of one market-side we accumulate in an epoch."""
    held = float(inventory.get((slug, side), 0.0))
    cap = float(cfg.max_shares_per_market_side)
    if cap > 0 and held + shares > cap + 1e-9:
        return f"side_cap:{slug}:{side}:{held:.1f}+{shares:.1f}>{cap}"
    return ""


def _inventory_net_gate(
    asset: str,
    side: str,
    shares: float,
    net_by_asset: dict[str, float],
    cfg: CrossMarketCorrelationConfig,
) -> str:
    """Reject legs that push |UP−DOWN| net shares past the asset cap (hedges OK)."""
    cap = float(cfg.max_net_shares_per_asset)
    if cap <= 0:
        return ""
    net = float(net_by_asset.get(asset, 0.0))
    delta = float(shares) if side == "UP" else -float(shares)
    new = net + delta
    # Allow reductions / flips toward flat; block only worsening past the cap.
    if abs(new) <= abs(net) + 1e-9:
        return ""
    if abs(new) > cap + 1e-9:
        return f"net_cap:{asset}:{net:+.1f}->{new:+.1f}>{cap}"
    return ""


def _pair_shares_and_leader_gate(
    q_a: LegQuote,
    q_b: LegQuote,
    mids_a: MidHist,
    mids_b: MidHist,
    now_ms: float,
    cfg: CrossMarketCorrelationConfig,
    base_shares: float,
) -> tuple[float, str]:
    """Return (shares, reject_reason). Empty reason means the pair may proceed."""
    ask_a, ask_b = float(q_a.ask), float(q_b.ask)
    if ask_a >= ask_b:
        leader_ask, leader_mids = ask_a, mids_a
    else:
        leader_ask, leader_mids = ask_b, mids_b
    if (leader_ask < float(cfg.leader_block_max_price) - 1e-12
            and _is_downtrend(leader_mids, now_ms, cfg.leader_downtrend_lookback_s,
                             cfg.leader_downtrend_min_drop)):
        return base_shares, (
            f"leader_downtrend:{leader_ask:.3f}<{cfg.leader_block_max_price}"
        )
    shares = float(base_shares)
    if leader_ask > float(cfg.leader_large_min_price) + 1e-12:
        shares = float(cfg.leader_large_shares)
    shares = min(shares, float(cfg.max_shares_per_leg), float(cfg.max_order_size))
    return shares, ""


@dataclass(slots=True)
class PairScanRow:
    asset_a: str
    asset_b: str
    slug_a: str
    slug_b: str
    alignment: AlignmentResult
    candidates: list[PairCandidateMetrics] = field(default_factory=list)
    best: PairCandidateMetrics | None = None
    reject_reason: str = ""


@dataclass(slots=True)
class ScanResult:
    rows: list[PairScanRow] = field(default_factory=list)
    best: PairScanRow | None = None
    evaluated: int = 0
    rejected: int = 0
    n_pairs: int = 0


def _unit_cost(leg_a, leg_b) -> float:
    return float(leg_a.unit_all_in) + float(leg_b.unit_all_in)


def _fill_prob(depth_a: float, depth_b: float, shares: float, min_depth: float) -> float:
    if shares <= 0:
        return 0.0
    if depth_a + 1e-9 < shares or depth_b + 1e-9 < shares:
        return 0.0
    # Crude: full depth >> size → high fill chance; near min_depth → lower.
    ratio = min(depth_a, depth_b) / max(shares, min_depth)
    return max(0.0, min(1.0, 0.55 + 0.45 * min(ratio / 3.0, 1.0)))


def _classify_kwargs(cfg: CrossMarketCorrelationConfig) -> dict:
    return dict(
        guaranteed_mode=cfg.guaranteed_mode_enabled,
        statistical_mode=cfg.statistical_mode_enabled,
        guaranteed_min_net=cfg.guaranteed_min_net_profit,
        minimum_conservative_ev=cfg.minimum_conservative_ev,
        uncertainty_z=cfg.uncertainty_z,
        max_uncertainty=cfg.max_probability_uncertainty,
        min_sample_size=cfg.min_sample_size,
        minimum_expected_net_profit=cfg.minimum_expected_net_profit,
        allow_under_sampled_prior=cfg.allow_under_sampled_prior,
        maximum_combined_cost=cfg.maximum_combined_cost,
        minimum_combined_cost=cfg.minimum_combined_cost,
    )


def _probs_for_pair(
    va: MarketView, vb: MarketView, model: JointOutcomeModel, cfg: CrossMarketCorrelationConfig,
) -> JointProbs:
    hist = model.estimate(va.asset, vb.asset)
    if (hist.n >= cfg.min_sample_size
            or not cfg.use_book_implied_when_under_sampled
            or not cfg.allow_under_sampled_prior):
        return hist
    # Thin history: blend historical posterior with book-implied independence.
    implied = independence_baseline(va.implied_p_up, vb.implied_p_up)
    w = min(1.0, hist.n / max(cfg.min_sample_size, 1))
    p_uu, p_ud, p_du, p_dd = normalize_probs(
        w * hist.p_uu + (1 - w) * implied.p_uu,
        w * hist.p_ud + (1 - w) * implied.p_ud,
        w * hist.p_du + (1 - w) * implied.p_du,
        w * hist.p_dd + (1 - w) * implied.p_dd,
    )
    # Keep uncertainty elevated while under-sampled so conservative EV still bites.
    unc = max(hist.uncertainty, implied.uncertainty * (1 - w))
    return JointProbs(p_uu, p_ud, p_du, p_dd, n=hist.n, uncertainty=unc)


def score_pair(
    va: MarketView,
    vb: MarketView,
    model: JointOutcomeModel,
    cfg: CrossMarketCorrelationConfig,
    shares: float,
    *,
    inventory: dict[tuple[str, str], float] | None = None,
    net_by_asset: dict[str, float] | None = None,
) -> PairScanRow:
    inv = inventory or {}
    nets = net_by_asset or {}
    align = validate_pair(
        va.snapshot, vb.snapshot,
        max_epoch_skew_s=cfg.maximum_epoch_skew_s,
        require_strike=cfg.require_strike,
        require_spot=cfg.require_spot,
    )
    row = PairScanRow(
        asset_a=va.asset, asset_b=vb.asset, slug_a=va.slug, slug_b=vb.slug,
        alignment=align,
    )
    if not align.ok:
        row.reject_reason = align.reason
        return row
    # Pair-local quote age skew (not global across all seven books).
    ages = [va.up.quote_age_ms, va.down.quote_age_ms, vb.up.quote_age_ms, vb.down.quote_age_ms]
    if (max(ages) - min(ages)) > cfg.maximum_timestamp_skew_ms + 1e-9:
        row.reject_reason = f"quote_skew:{max(ages) - min(ages):.0f}ms"
        row.alignment = AlignmentResult(False, row.reject_reason)
        return row

    probs = _probs_for_pair(va, vb, model, cfg)
    kw = _classify_kwargs(cfg)
    cands: list[PairCandidateMetrics] = []
    now_ms = float(va.now_ms or vb.now_ms or 0.0)

    def _reject(direction: str, q_shares: float, reason: str) -> None:
        cands.append(classify_candidate(
            direction, q_shares, 9.99, probs, fill_prob_est=0.0, **kw))
        cands[-1].reject_reason = reason
        cands[-1].label = "REJECTED"

    def _score_dir(direction: str, q_a: LegQuote, q_b: LegQuote, depth_a: float, depth_b: float,
                   side_a: str, side_b: str, liq_tag: str) -> None:
        q_shares, gate = _pair_shares_and_leader_gate(
            q_a, q_b, _mids_for(va, side_a), _mids_for(vb, side_b),
            now_ms, cfg, shares,
        )
        if gate:
            _reject(direction, q_shares, gate)
            return
        low_gate = _lower_side_only_gate(
            va.asset, float(q_a.ask), vb.asset, float(q_b.ask), cfg)
        if low_gate:
            _reject(direction, q_shares, low_gate)
            return
        wipe_gate = _wipe_skip_gate(va, vb, direction, cfg)
        if wipe_gate:
            _reject(direction, q_shares, wipe_gate)
            return
        for slug, side, asset in (
            (va.slug, side_a, va.asset), (vb.slug, side_b, vb.asset),
        ):
            side_gate = _inventory_side_gate(slug, side, q_shares, inv, cfg)
            if side_gate:
                _reject(direction, q_shares, side_gate)
                return
            net_gate = _inventory_net_gate(asset, side, q_shares, nets, cfg)
            if net_gate:
                _reject(direction, q_shares, net_gate)
                return
        leg_a = estimate_leg(q_a, q_shares, cfg.maximum_slippage)
        leg_b = estimate_leg(q_b, q_shares, cfg.maximum_slippage)
        if leg_a is None or leg_b is None:
            _reject(direction, q_shares, liq_tag)
            return
        cost = _unit_cost(leg_a, leg_b)
        fp = _fill_prob(depth_a, depth_b, q_shares, cfg.minimum_book_depth)
        cands.append(classify_candidate(
            direction, q_shares, cost, probs, fill_prob_est=fp,
            leg_a_unit=leg_a.unit_all_in, leg_b_unit=leg_b.unit_all_in, **kw))

    # Candidate 1: A UP + B DOWN
    _score_dir("A_UP_B_DOWN", va.up, vb.down, va.up.depth, vb.down.depth,
               "UP", "DOWN", "insufficient_liquidity_c1")
    # Candidate 2: A DOWN + B UP
    _score_dir("A_DOWN_B_UP", va.down, vb.up, va.down.depth, vb.up.depth,
               "DOWN", "UP", "insufficient_liquidity_c2")

    row.candidates = cands
    tradable = [c for c in cands if c.label != "REJECTED"]
    if not tradable:
        reasons = [c.reject_reason or c.label for c in cands]
        row.reject_reason = ";".join(reasons)
        return row
    # Prefer guaranteed, then hedges (reduce |net|), then highest conservative EV.
    tradable.sort(key=lambda c: (
        0 if c.label == "GUARANTEED_ARBITRAGE" else 1,
        _hedge_rank(c.direction, va.asset, vb.asset, nets),
        -c.conservative_ev,
        c.total_cost,
    ))
    row.best = tradable[0]
    return row


def _hedge_rank(direction: str, asset_a: str, asset_b: str, nets: dict[str, float]) -> int:
    """0 = reduces existing imbalance on either asset; 1 = neutral/worsens."""
    side_a, side_b = (("UP", "DOWN") if direction == "A_UP_B_DOWN" else ("DOWN", "UP"))
    score = 0
    for asset, side in ((asset_a, side_a), (asset_b, side_b)):
        net = float(nets.get(asset, 0.0))
        if abs(net) < 1e-9:
            continue
        # Buying the opposite of current net is a hedge.
        if (net > 0 and side == "DOWN") or (net < 0 and side == "UP"):
            score -= 1
        else:
            score += 1
    return 0 if score < 0 else 1


def scan_all(
    views: Sequence[MarketView],
    model: JointOutcomeModel,
    cfg: CrossMarketCorrelationConfig,
    *,
    shares: float | None = None,
    inventory: dict[tuple[str, str], float] | None = None,
    net_by_asset: dict[str, float] | None = None,
) -> ScanResult:
    by_asset = {v.asset.upper(): v for v in views}
    assets = cfg.asset_list()
    pairs = all_unique_pairs(assets)
    q = float(shares if shares is not None else cfg.size_shares)
    result = ScanResult(n_pairs=len(pairs))
    best_row: PairScanRow | None = None
    inv = inventory or {}
    nets = net_by_asset or {}

    for a, b in pairs:
        va, vb = by_asset.get(a), by_asset.get(b)
        if va is None or vb is None:
            row = PairScanRow(
                asset_a=a, asset_b=b, slug_a="", slug_b="",
                alignment=AlignmentResult(False, "missing_market"),
                reject_reason="missing_market",
            )
            result.rows.append(row)
            result.rejected += 1
            continue
        # Time remaining gate
        if (va.time_remaining_s < cfg.min_time_remaining_s + cfg.execution_buffer_s
                or vb.time_remaining_s < cfg.min_time_remaining_s + cfg.execution_buffer_s):
            row = PairScanRow(
                asset_a=a, asset_b=b, slug_a=va.slug, slug_b=vb.slug,
                alignment=AlignmentResult(False, "too_close_to_settle"),
                reject_reason="too_close_to_settle",
            )
            result.rows.append(row)
            result.rejected += 1
            continue
        if (va.time_remaining_s > cfg.maximum_time_before_settlement_s
                or vb.time_remaining_s > cfg.maximum_time_before_settlement_s):
            row = PairScanRow(
                asset_a=a, asset_b=b, slug_a=va.slug, slug_b=vb.slug,
                alignment=AlignmentResult(False, "too_early"),
                reject_reason="too_early",
            )
            result.rows.append(row)
            result.rejected += 1
            continue

        row = score_pair(va, vb, model, cfg, q, inventory=inv, net_by_asset=nets)
        result.evaluated += len(row.candidates)
        result.rows.append(row)
        if row.best is None:
            result.rejected += 1
            continue
        if best_row is None or _row_rank(row, nets) < _row_rank(best_row, nets):
            best_row = row

    result.best = best_row
    return result


def _row_rank(row: PairScanRow, nets: dict[str, float] | None = None) -> tuple:
    c = row.best
    assert c is not None
    return (
        0 if c.label == "GUARANTEED_ARBITRAGE" else 1,
        _hedge_rank(c.direction, row.asset_a, row.asset_b, nets or {}),
        -c.conservative_ev,
        c.total_cost,
    )
