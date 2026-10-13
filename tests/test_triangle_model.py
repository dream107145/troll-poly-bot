"""The triangle ENTRY MODEL and the EV hedge (2026-10-01 brief): a locked logistic model scores every
candidate; buys need EV >= the threshold; the second leg is taken only when the certain pair beats P(win)."""
from __future__ import annotations

import json

import pytest

from test_triangle import _cfg, _engine, _shock
from troll_poly_bot.strategy.engine import Reason
from troll_poly_bot.strategy.triangle import TriangleConfig, TriangleEngine
from troll_poly_bot.types import TimeInForce

FEATS = ["z1", "z3", "accel", "tfi15", "obi", "ofi", "tok_vel1", "tok_vel3", "depth_ask", "spread", "left_frac",
         "ask", "fv_gap", "pressure", "hz"]


def _lock(tmp_path, intercept=0.0, w_z1=1.0, ev_min=0.0, max_price=0.96, min_left=60.0):
    mdl = {"mu": [0.0] * len(FEATS), "sd": [1.0] * len(FEATS), "w": [intercept] + [w_z1] + [0.0] * (len(FEATS) - 1)}
    path = tmp_path / "lock.json"
    path.write_text(json.dumps({"features": FEATS, "model": mdl, "ev_min": ev_min, "max_price": max_price, "min_left_s": min_left}))
    return str(path)


def test_off_by_default_and_a_missing_lock_trades_every_triangle(tmp_path, caplog):
    assert TriangleConfig(enabled=True).entry_model is False
    e, ev = _shock(6.0, scfg=_cfg(entry_model=True, entry_model_path=str(tmp_path / "missing.json")))
    assert e.model is None and ev.tradeable and "ENTRY_MODEL not loaded" in caplog.text


def test_the_model_scores_the_candidate_and_the_ev_gate_decides(tmp_path):
    # a strong 1 s move (z1 = 6) with weight 1 -> P(win) = sigmoid(6) ~ 0.998: EV = 0.998 - 0.53 - fee -> buys
    e, ev = _shock(6.0, scfg=_cfg(entry_model=True, entry_model_path=_lock(tmp_path, w_z1=1.0), entry_min_ev=0.10))
    assert e.model is not None and ev.tradeable, (ev.reason, ev.detail)
    info = ev.features["triangle"]
    assert info["p_win"] > 0.99 and info["entry_ev"] == pytest.approx(info["p_win"] - 0.53 - 0.07 * 0.53 * 0.47, abs=1e-3)
    assert set(info["entry_feats"]) == set(FEATS) and info["entry_feats"]["z1"] == pytest.approx(6.0, abs=0.3)
    # the same move with a model that thinks it is a coin flip: EV = 0.5 - 0.53 - fee < 0 -> refused, logged why
    e2, ev2 = _shock(6.0, scfg=_cfg(entry_model=True, entry_model_path=_lock(tmp_path, w_z1=0.0), entry_min_ev=0.0))
    assert ev2.reason is Reason.EDGE_TOO_SMALL and "entry EV" in ev2.detail and e2.entry_refused_ev == 1
    (mark,) = e2.shock_marks_for("btc-updown-5m-1")
    assert mark["ok"] is False and "entry EV" in mark["why"]            # drawn hollow with the reason


def test_the_price_and_time_limits_are_explicit_config_not_the_lock(tmp_path):
    e, ev = _shock(6.0, scfg=_cfg(entry_model=True, entry_model_path=_lock(tmp_path, max_price=0.50), entry_max_price=0.50))
    assert ev.reason is Reason.PRICE_BAND and "entry model" in ev.detail      # ask 0.52 > 0.50
    e, ev = _shock(6.0, scfg=_cfg(entry_model=True, entry_model_path=_lock(tmp_path, max_price=0.50)))
    assert ev.tradeable                                                        # the lock's 0.50 is informational
    e, ev = _shock(6.0, scfg=_cfg(entry_model=True, entry_model_path=_lock(tmp_path), entry_min_left_s=200.0))
    assert ev.reason is Reason.OUTSIDE_TIME_WINDOW                            # 180 s left < 200


def test_the_ev_hedge_takes_the_second_leg_only_when_the_certain_pair_beats_p_win(tmp_path):
    e, ev = _shock(6.0, scfg=_cfg(entry_model=True, entry_model_path=_lock(tmp_path, w_z1=0.2), pair_cost=0.96,
                                  hedge_mode="ev", hedge_ev_margin=0.02, shares=5.0, max_order_usdc=5.0))
    assert ev.tradeable
    m = e.last["btc-updown-5m-1"]
    p_win = ev.features["triangle"]["p_win"]                                  # sigmoid(1.2) ~ 0.77
    market = type("M", (), {"slug": "btc-updown-5m-1", "tick_size": 0.01, "yes_token_id": "UP", "no_token_id": "DOWN",
                            "min_size": 5.0, "close_ts": 0.0})()
    # other ask 0.40: certain = 0.60 - fee(0.40) = 0.583 < 0.77 + 0.02 -> no hedge, the leg stays naked
    assert e.pair_order(market, "UP", 5.0, 0.53, 0.0, other_ask=0.40) is None
    assert e.pairs_skipped.get("ev") == 1 and e.pairs_naked == 1
    # other ask 0.15: certain = 0.85 - 0.009 = 0.841 >= 0.79 -> FOK hedge at 0.15 + 0.03
    made = e.pair_order(market, "UP", 5.0, 0.53, 0.0, other_ask=0.15)
    assert made is not None and made[0] == "DOWN" and made[1].tif is TimeInForce.FOK and made[1].price == pytest.approx(0.18)
    assert e.pairs_placed == 1 and 0.75 < p_win < 0.80


def test_pair_room_is_not_required_in_ev_mode(tmp_path):
    # resting mode caps buys at pair_cost - 1 tick; the EV hedge has no resting price, so a 0.95 ask may trade
    e, ev = _shock(6.0, up_now=(0.93, 0.95), scfg=_cfg(entry_model=True, entry_model_path=_lock(tmp_path), pair_cost=0.96,
                                                        hedge_mode="ev", max_price=0.96))
    assert ev.tradeable, (ev.reason, ev.detail)
