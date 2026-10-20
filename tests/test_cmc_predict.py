"""Unit + historical accuracy tests for CMC strike-relative pair predictor."""
from __future__ import annotations

import json
from collections import Counter
from itertools import combinations
from pathlib import Path

import pytest

from troll_poly_bot.strategy.cross_market_correlation.predict import (
    DEFAULT_WIPE_SKIP_THRESHOLD,
    actual_payout,
    predict_opposite_pair,
    predict_wipe_risk,
    signed_bps,
    trade_label,
)

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
CHARTS_INDEX = Path(__file__).resolve().parents[1] / "data" / "charts_index.json"


def test_signed_bps_basic():
    assert signed_bps(101.0, 100.0) == pytest.approx(100.0)
    assert signed_bps(99.0, 100.0) == pytest.approx(-100.0)
    assert signed_bps(100.0, 100.0) == pytest.approx(0.0)
    assert signed_bps(100.0, 0.0) is None
    assert signed_bps(float("nan"), 100.0) is None


def test_predict_agreement_both_above():
    # Both above strike → UU → A_UP_B_DOWN pays 1
    p = predict_opposite_pair(101, 100, 102, 100, min_abs_bps=3)
    assert p is not None
    assert p.predicted_payout == 1
    assert p.same_side_of_strike
    assert p.regime == "agreement"
    assert p.confident
    assert p.expect_small_win


def test_predict_agreement_both_below():
    # Both below → DD → A_UP_B_DOWN pays 1
    p = predict_opposite_pair(99, 100, 98, 100, min_abs_bps=3)
    assert p is not None and p.predicted_payout == 1 and p.regime == "agreement"


def test_predict_favor_direction_jackpot():
    # A above, B below → UD → pay 2 for A_UP_B_DOWN
    p = predict_opposite_pair(101, 100, 99, 100, min_abs_bps=3)
    assert p is not None
    assert p.predicted_payout == 2
    assert p.regime == "favor_direction"
    assert p.expect_jackpot


def test_predict_against_direction_wipe():
    # A below, B above → DU → pay 0
    p = predict_opposite_pair(99, 100, 101, 100, min_abs_bps=3)
    assert p is not None
    assert p.predicted_payout == 0
    assert p.regime == "against_direction"
    assert p.expect_wipe


def test_predict_other_direction():
    # A above, B below → A_DOWN_B_UP pays 0
    p = predict_opposite_pair(101, 100, 99, 100, direction="A_DOWN_B_UP", min_abs_bps=0)
    assert p is not None and p.predicted_payout == 0


def test_near_strike_not_confident():
    p = predict_opposite_pair(100.01, 100, 100.02, 100, min_abs_bps=5)
    assert p is not None
    assert not p.confident
    assert p.regime == "near_strike"


def test_trade_label():
    assert trade_label(1, 0.95) == "win"
    assert trade_label(0, 0.95) == "lose"
    assert trade_label(2, 0.95) == "win"
    assert trade_label(1, 1.0) == "flat"


def test_actual_payout_truth_table():
    assert actual_payout(True, True) == 1
    assert actual_payout(True, False) == 2
    assert actual_payout(False, True) == 0
    assert actual_payout(False, False) == 1


def _load_epoch_snapshots() -> dict[int, dict[str, dict]]:
    if not CHARTS_INDEX.exists():
        pytest.skip("charts_index.json not present")
    rows = json.loads(CHARTS_INDEX.read_text())["rows"]
    epochs: dict[int, dict[str, dict]] = {}
    for slug, meta in rows.items():
        out = meta.get("outcome")
        if out not in ("UP", "DOWN"):
            continue
        parts = slug.split("-")
        asset, ep = parts[0].upper(), int(parts[-1])
        if asset not in ASSETS:
            continue
        strike, spot = meta.get("strike"), meta.get("last_spot")
        if strike is None or spot is None or float(strike) <= 0:
            continue
        epochs.setdefault(ep, {})[asset] = {
            "up": out == "UP",
            "strike": float(strike),
            "spot": float(spot),
            "up_open": meta.get("up_open"),
        }
    return epochs


@pytest.mark.parametrize("min_abs_bps,min_acc", [(0.0, 0.80), (3.0, 0.90), (5.0, 0.93)])
def test_historical_payout_accuracy(min_abs_bps: float, min_acc: float):
    """Final spot vs strike should predict payout well (defines settlement)."""
    epochs = _load_epoch_snapshots()
    n = correct = 0
    by_pay = Counter()
    for d in epochs.values():
        assets = [a for a in ASSETS if a in d]
        for a, b in combinations(assets, 2):
            da, db = d[a], d[b]
            pred = predict_opposite_pair(
                da["spot"], da["strike"], db["spot"], db["strike"],
                min_abs_bps=min_abs_bps,
            )
            if pred is None or not pred.confident:
                continue
            act = actual_payout(da["up"], db["up"])
            n += 1
            by_pay[act] += 1
            if pred.predicted_payout == act:
                correct += 1
    assert n >= 1000, f"too few samples: {n}"
    acc = correct / n
    assert acc >= min_acc, f"acc={acc:.3f} n={n} dist={dict(by_pay)}"


def test_historical_win_lose_at_cost_095():
    """Agreement (pred payout=1) ⇒ high win rate at cost 0.95; wipe calls are elevated."""
    epochs = _load_epoch_snapshots()
    cost = 0.95
    pred1_n = pred1_wins = 0
    pred0_n = pred0_losses = 0
    label_n = label_ok = 0
    for d in epochs.values():
        assets = [a for a in ASSETS if a in d]
        for a, b in combinations(assets, 2):
            da, db = d[a], d[b]
            pred = predict_opposite_pair(
                da["spot"], da["strike"], db["spot"], db["strike"],
                min_abs_bps=3.0,
            )
            if pred is None or not pred.confident:
                continue
            act = actual_payout(da["up"], db["up"])
            act_lbl = trade_label(act, cost)
            pred_lbl = trade_label(pred.predicted_payout, cost)
            label_n += 1
            if pred_lbl == act_lbl:
                label_ok += 1
            if pred.predicted_payout == 1:
                pred1_n += 1
                if act_lbl == "win":
                    pred1_wins += 1
            elif pred.predicted_payout == 0:
                pred0_n += 1
                if act_lbl == "lose":
                    pred0_losses += 1
    assert pred1_n >= 500
    win_rate = pred1_wins / pred1_n
    # Agreement regime: almost always pay ≥1 → win at cost 0.95
    assert win_rate >= 0.90, f"pred=1 win_rate={win_rate:.3f} n={pred1_n}"
    # Overall win/lose label follows prediction well (jackpot≈win even if pay 1 vs 2)
    assert label_ok / label_n >= 0.90, f"label_acc={label_ok / label_n:.3f}"
    # Wipe calls: last_spot can disagree with venue print, so precision is lower
    # than agreement — but still clearly above the ~9% base loss rate in-agreement.
    if pred0_n >= 100:
        wipe_prec = pred0_losses / pred0_n
        assert wipe_prec >= 0.65, f"pred=0 lose_rate={wipe_prec:.3f} n={pred0_n}"


def test_open_book_baseline_is_weak():
    """up_open>0.5 baseline should be clearly worse than strike-relative model."""
    epochs = _load_epoch_snapshots()
    n = book_ok = strike_ok = 0
    for d in epochs.values():
        assets = [
            a for a in ASSETS
            if a in d and d[a].get("up_open") is not None
        ]
        for a, b in combinations(assets, 2):
            da, db = d[a], d[b]
            act = actual_payout(da["up"], db["up"])
            book_pay = (
                (1 if float(da["up_open"]) > 0.5 else 0)
                + (1 if float(db["up_open"]) <= 0.5 else 0)
            )
            pred = predict_opposite_pair(
                da["spot"], da["strike"], db["spot"], db["strike"], min_abs_bps=0.0,
            )
            if pred is None:
                continue
            n += 1
            if book_pay == act:
                book_ok += 1
            if pred.predicted_payout == act:
                strike_ok += 1
    assert n >= 1000
    assert strike_ok / n >= book_ok / n + 0.15


def test_wipe_risk_flags_known_wipe_geometry():
    """A below strike, B above → high wipe risk for A_UP_B_DOWN."""
    risk = predict_wipe_risk(
        99.0, 100.0, 101.0, 100.0,
        asset_a="BTC", asset_b="ETH",
        ask_up_a=0.20, ask_up_b=0.80,
    )
    assert risk is not None
    assert risk.against_direction
    assert risk.against_strength_bps >= 100.0 - 1e-9  # min(100,100) wait spots are 1% = 100bps
    assert risk.p_wipe >= 0.55
    assert risk.should_skip()


def test_wipe_risk_low_when_same_side():
    risk = predict_wipe_risk(
        101.0, 100.0, 102.0, 100.0,
        asset_a="BTC", asset_b="ETH",
    )
    assert risk is not None
    assert not risk.against_direction
    assert risk.p_wipe < 0.35


def test_improved_wipe_model_beats_baseline_on_holdout():
    """Time-split: calibrated p_wipe@0.55 should beat against&str>=3 on F1."""
    epochs = _load_epoch_snapshots()
    eps = sorted(epochs)
    assert len(eps) >= 200
    cut = eps[int(len(eps) * 0.7)]
    test_eps = {e for e in eps if e >= cut}

    def eval_pred(pred_fn) -> tuple[float, float, float, int]:
        flags = hits = wipes = 0
        for ep in test_eps:
            d = epochs[ep]
            assets = [a for a in ASSETS if a in d]
            for a, b in combinations(sorted(assets), 2):
                for direction in ("A_UP_B_DOWN", "A_DOWN_B_UP"):
                    da, db = d[a], d[b]
                    wipe = actual_payout(da["up"], db["up"], direction) == 0
                    if wipe:
                        wipes += 1
                    if pred_fn(a, b, da, db, direction):
                        flags += 1
                        if wipe:
                            hits += 1
        if flags == 0 or wipes == 0:
            return 0.0, 0.0, 0.0, 0
        prec, rec = hits / flags, hits / wipes
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        return prec, rec, f1, flags

    def baseline(a, b, da, db, direction):
        risk = predict_wipe_risk(
            da["spot"], da["strike"], db["spot"], db["strike"],
            asset_a=a, asset_b=b, direction=direction,
            ask_up_a=da.get("up_open"), ask_up_b=db.get("up_open"),
        )
        return bool(
            risk and risk.against_direction and risk.against_strength_bps >= 3.0
        )

    def improved(a, b, da, db, direction):
        risk = predict_wipe_risk(
            da["spot"], da["strike"], db["spot"], db["strike"],
            asset_a=a, asset_b=b, direction=direction,
            ask_up_a=da.get("up_open"), ask_up_b=db.get("up_open"),
        )
        return bool(risk and risk.should_skip(DEFAULT_WIPE_SKIP_THRESHOLD))

    bp, br, bf1, bn = eval_pred(baseline)
    ip, ir, if1, i_n = eval_pred(improved)
    assert bn >= 50 and i_n >= 50
    assert if1 >= bf1 + 0.04, (
        f"improved F1={if1:.3f} (p={ip:.2f} r={ir:.2f}) "
        f"baseline F1={bf1:.3f} (p={bp:.2f} r={br:.2f})"
    )
    assert ir >= br, f"recall should not regress: {ir:.3f} vs {br:.3f}"
