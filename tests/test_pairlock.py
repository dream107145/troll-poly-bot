"""strategy/pairlock.py: the features use no future data, the reversal score's direction, the entry EV at the
executable VWAP, the position manager (2026-10-02 spec): adverse move -> confirmed reversal -> recovery hedge, the
profit lock, the validated states, the never-chase rule, the limits -- and the replay's fill model against the paper
venue it mirrors. The spec's numbered tests 1-20 are in tests/test_pairlock_recovery.py."""
from __future__ import annotations

import importlib.util
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from troll_poly_bot.strategy import pairlock as pl
from troll_poly_bot.strategy import pairlock_book as pb
from troll_poly_bot.strategy.pairlock_book import FillRecord, PairBook, State, fee_per_share

M = "btc-updown-5m-1"


def _model(intercept=0.0, slope=1.0):
    m = pl.ProbModel(beta=[intercept, slope] + [0.0] * (len(pl.FEATURES) - 1))
    m.exec = {"entry_fill_rate": 1.0, "entry_slip": 0.0, "entry_adv": 0.0, "hedge_fill_rate": 1.0, "hedge_slip": 0.0}
    return m


def _feat(**kw):
    f = {k: 0.0 for k in pl.FEATURES}
    f.update({"logit_mid": 0.0, "mid_up": 0.5, "left": 150.0})
    f.update(kw)
    return f


def _cycle(side="UP", q=10.0, px=0.50, t=0.0):
    c = pl.Cycle(book=PairBook(M), a_side=side, started_t=t)
    pl.on_entry_sent(c, "e1", q, px + 0.03, t)
    pl.on_fill(c, FillRecord(M, t, side.lower(), side, q, px, q * fee_per_share(px), "e1", "e1-1", kind="entry"),
               pl.PairLockParams(), t)
    pl.on_order_done(c, "e1", True, t)
    assert c.state is State.DIRECTIONAL_POSITION
    return c


LV = [(0.50, 1e6)]


# ───────────────────────────── features: past data only ─────────────────────────────


def _hist(n=400, seed=1):
    rng = np.random.default_rng(seed)
    h = pl.History()
    s, u = 100_000.0, 0.5
    for k in range(n):
        s *= math.exp(rng.normal(0, 2e-5))
        u = min(0.95, max(0.05, u + rng.normal(0, 0.005)))
        b, a = round(u - 0.005, 2), round(u + 0.005, 2)
        srcs = {"binance": s * (1 + rng.normal(0, 1e-6)), "coinbase": s, "bybit": s * (1 + rng.normal(0, 1e-6))}
        h.append(1_000_000.0 + 200.0 * k, b, a, round(1 - a, 2), round(1 - b, 2), 1000.0 + 10 * (k % 7),
                 800.0 + 5 * (k % 3), s, srcs)
    return h


def test_features_use_no_future_data():
    h = _hist()
    i = 120
    f = pl.features(h, i, 100_000.0, 1e-4, 1_000_000.0 + 300_000.0)
    g = _hist()
    for k in range(i + 1, len(g)):                                                  # scramble the future
        g.spot[k] *= 1.05
        g.ub[k], g.ua[k] = 0.10, 0.11
        g.uad[k] = 0.0
        g.srcs[k] = {"binance": 1.0}
    assert pl.features(g, i, 100_000.0, 1e-4, 1_000_000.0 + 300_000.0) == f
    assert f is not None and set(pl.FEATURES) <= set(f)
    assert pl.features(h, 5, 100_000.0, 1e-4, 1_300_000.0) is None                 # 10 s of history not there yet


def test_agreement_and_depth_flow_features():
    h = pl.History()
    for k in range(80):
        t = 1_000_000.0 + 200.0 * k
        up = k >= 75
        s = 100.0 * (1.001 if up else 1.0)
        h.append(t, 0.49, 0.51, 0.49, 0.51, 1000.0, 1000.0 - (600.0 if up else 0.0), s,
                 {"binance": s, "coinbase": s, "bybit": s})
    f = pl.features(h, len(h) - 1, 100.0, 1e-4, 1_300_000.0)
    assert f["agree"] == 1.0                                       # every exchange up over the last second
    assert f["pmflow"] == pytest.approx(0.6)                        # 60 % of the UP asks gone near the touch


def test_the_reversal_score_points_toward_the_other_side():
    f = _feat(tv1=-10.0, tv3=-10.0, tacc=-5.0, mom1=-3.0, mom3=-3.0, imb=-0.5, pmflow=-0.5, agree=-1.0)
    sc = {k: 1.0 for k in pl.REVERSAL}
    assert pl.reversal_score(f, "UP", sc) > 0 > pl.reversal_score(f, "DOWN", sc)


# ───────────────────────────── entry ─────────────────────────────


def test_entry_ev_is_at_the_executable_vwap_not_the_best_ask():
    f = _feat(logit_mid=math.log(0.70 / 0.30))
    m = _model()                                                     # P(UP) = 0.70
    prm, lim = pl.PairLockParams(entry_edge=0.0), pl.RiskLimits()
    thin = [(0.50, 3.0), (0.55, 3.0), (0.62, 10.0)]
    dec, _ = pl.entry_decision(f, "UP", 0.50, 0.49, thin, m, prm, lim, pl.MarketExposure(), 200.0, None)
    vw = (3 * 0.50 + 3 * 0.55 + 4 * 0.62) / 10
    assert dec is not None and dec.info["vwap"] == pytest.approx(vw)
    assert dec.ev == pytest.approx(0.70 - vw - dec.info["fee"])
    assert dec.limit == pytest.approx(0.65)                          # the deepest level used + the 3c offset
    dec, why = pl.entry_decision(f, "UP", 0.50, 0.49, [(0.50, 3.0)], m, prm, lim, pl.MarketExposure(), 200.0, None)
    assert dec is None and why == "depth"                            # never assume the ask has unlimited size
    dec, why = pl.entry_decision(f, "UP", 0.50, 0.49, LV, m, replace(prm, entry_edge=0.30), lim, pl.MarketExposure(),
                                 200.0, None)
    assert dec is None and "entry EV" in why


def test_entry_gates_time_band_spread_capital_and_an_open_cycle():
    f = _feat(logit_mid=math.log(0.70 / 0.30))
    m, prm, lim = _model(), pl.PairLockParams(entry_edge=0.0), pl.RiskLimits()
    E = pl.MarketExposure
    assert "s left" in pl.entry_decision(f, "UP", 0.50, 0.49, LV, m, prm, lim, E(), 30.0, None)[1]
    assert "outside" in pl.entry_decision(f, "UP", 0.98, 0.97, LV, m, prm, lim, E(), 200.0, None)[1]
    assert pl.entry_decision(f, "UP", 0.50, 0.40, LV, m, prm, lim, E(), 200.0, None)[1] == "spread"
    assert "capital" in pl.entry_decision(f, "UP", 0.50, 0.49, LV, m, prm, lim, E(capital=19.0), 200.0, None)[1]
    open_ = _cycle()
    assert "REENTRY_BLOCKED" in pl.entry_decision(f, "UP", 0.50, 0.49, LV, m, prm, lim, E(), 200.0, open_)[1]
    assert "REENTRY_BLOCKED" in pl.entry_decision(f, "UP", 0.50, 0.49, LV, m, prm, lim, E(held=True), 200.0, None)[1]
    m.short = {"features": list(pl.SHORT), "beta": [-5.0] + [0.0] * len(pl.SHORT), "horizon_s": 10}
    dec, why = pl.entry_decision(f, "UP", 0.50, 0.49, LV, m, replace(prm, entry_mode="both", short_min=0.5), lim, E(),
                                 200.0, None)
    assert dec is None and "P(favourable)" in why


# ───────────────────────────── the position manager ─────────────────────────────

#: every reversal signal toward DOWN for a UP position (and the composite score far above any threshold)
AGAINST_UP = dict(tv1=-5.0, tv3=-5.0, tacc=-3.0, mom1=-3.0, mom3=-3.0, imb=-0.5, pmflow=-0.5, agree=-1.0)
RELAXED = dict(max_acceptable_pair_cost=1.30)          # a cap above 1: the only way a recovery can qualify (see below)


def mv(bid_up, ask_up=None, depth=1e6, down_levels=None, up_levels=None, spot_age=None, qa=None):
    """Both tokens of one sample, mirrored the way the venue's books are (DOWN ask = 1 - UP bid)."""
    ask_up = round(bid_up + 0.01, 2) if ask_up is None else ask_up
    bid_dn, ask_dn = round(1 - ask_up, 2), round(1 - bid_up, 2)
    return pl.MarketView(bid_up=bid_up, ask_up=ask_up, bid_down=bid_dn, ask_down=ask_dn,
                         asks_up=up_levels or [(ask_up, depth)], asks_down=down_levels or [(ask_dn, depth)],
                         bids_up=[(bid_up, depth)], bids_down=[(bid_dn, depth)], quote_age_up_ms=qa,
                         quote_age_down_ms=qa, spot_age_ms=spot_age)


def step(c, f, view, now, prm, model=None, left=150.0, lim=None, mkt=None):
    return pl.manage_position(c, f, view, now, left, model or _model(), prm, lim or pl.RiskLimits(),
                              mkt or pl.MarketExposure(capital=c.book.cash_spent, shares=c.book.up_qty,
                                                       held=True))


def test_a_loss_alone_never_reaches_a_recovery_evaluation():
    prm = pl.PairLockParams(**RELAXED)
    c = _cycle("UP", 10, 0.50)
    neutral = _feat(logit_mid=math.log(0.4 / 0.6))
    for k in range(10):                                            # 10 c under water for 2 s, no reversal signal
        dec, why, rec = step(c, neutral, mv(0.40), 1000.0 + 200.0 * k, prm)
        assert dec is None and rec is None
    assert c.state is State.ADVERSE_MOVE and not c.reversal_confirmed
    assert [e.name for e in c.events].count("ADVERSE_MOVE_DETECTED") == 1
    with pytest.raises(ValueError):
        pl.PairLockParams(recovery_min_confirmations=1)              # the invariant cannot be configured away
    with pytest.raises(ValueError):
        pl.PairLockParams(recovery_min_loss=0.0)


def test_a_reversal_without_a_material_loss_is_not_adverse():
    prm = pl.PairLockParams(**RELAXED)
    c = _cycle("UP", 10, 0.50)
    f = _feat(logit_mid=math.log(0.48 / 0.52), **AGAINST_UP)
    for k in range(5):                                               # 2c under (one or two ticks): not material
        dec, _, rec = step(c, f, mv(0.48), 1000.0 + 200.0 * k, prm)
        assert dec is None and rec is None
    assert c.state is State.DIRECTIONAL_POSITION


def test_the_reversal_must_hold_for_the_confirmation_window_with_k_of_4_signals():
    prm = pl.PairLockParams(**RELAXED, recovery_confirmation_window_ms=400.0, recovery_min_confirmations=3)
    c = _cycle("UP", 10, 0.50)
    f = _feat(logit_mid=math.log(0.4 / 0.6), **AGAINST_UP)
    assert step(c, f, mv(0.40), 1000.0, prm)[2] is None and c.state is State.ADVERSE_MOVE
    assert step(c, f, mv(0.40), 1200.0, prm)[2] is None                    # 200 ms < 400 ms
    dec, why, rec = step(c, f, mv(0.40), 1400.0, prm)
    assert rec is not None and rec["kind"] == "recovery"
    ev = next(e for e in c.events if e.name == "REVERSAL_CONFIRMED")
    assert "confirmation=4/4" in ev.reason and "original=UP" in ev.reason and "opposite=DOWN" in ev.reason
    # only 2 of 4 signals (token falling + score; spot and order flow not against): never confirmed
    c2 = _cycle("UP", 10, 0.50)
    two = _feat(logit_mid=math.log(0.4 / 0.6), tv1=-9.0, tv3=-9.0, tacc=-9.0, mom1=0.5, mom3=0.5, imb=0.2, pmflow=0.2)
    for k in range(6):
        assert step(c2, two, mv(0.40), 1000.0 + 200.0 * k, prm)[2] is None
    assert c2.state is State.ADVERSE_MOVE and c2.last_obs["confirmations"] == 2
    # a stale quote breaks the confirmation clock
    c3 = _cycle("UP", 10, 0.50)
    step(c3, f, mv(0.40), 1000.0, prm)
    step(c3, f, mv(0.40, qa=5000.0), 1200.0, prm)
    assert step(c3, f, mv(0.40), 1400.0, prm)[2] is None and c3.rev_since == 1400.0


def test_a_recovery_hedge_may_lower_the_expected_pnl_if_it_buys_enough_downside():
    prm = pl.PairLockParams(**RELAXED, recovery_confirmation_window_ms=0.0, risk_aversion=0.25)
    c = _cycle("UP", 10, 0.50)
    f = _feat(logit_mid=math.log(0.4 / 0.6), **AGAINST_UP)               # P(DOWN) = 0.60, DOWN ask 0.60
    dec, why, rec = step(c, f, mv(0.40), 1000.0, prm)
    assert dec is not None and dec.kind == "recovery" and dec.side == "DOWN" and dec.shares == 10.0
    i = dec.info
    assert i["expected_after"] < i["expected_before"]                      # raw EV falls ...
    assert i["worst_after"] - i["worst_before"] == pytest.approx(i["downside_reduction"])
    assert i["utility"] == pytest.approx(i["expected_after"] + 0.25 * i["downside_reduction"])
    assert i["utility"] > i["expected_before"]                             # ... the utility rises: accepted
    assert c.state is State.RECOVERY_EVALUATION
    # with no risk aversion the same hedge is pure EV and is rejected
    c2 = _cycle("UP", 10, 0.50)
    dec, why, rec = step(c2, f, mv(0.40), 1000.0, replace(prm, risk_aversion=0.0))
    assert dec is None and "utility" in why and c2.state is State.REVERSAL_CONFIRMED
    assert [e.name for e in c2.events][-2:] == ["RECOVERY_HEDGE_EVALUATION", "RECOVERY_HEDGE_REJECTED"]


def test_the_strict_cap_blocks_every_recovery_because_the_books_mirror():
    """DOWN ask = 1 - UP bid: after the original fell by L per share, any hedge pairs at 1 + L + fees. With the
    default MAX_ACCEPTABLE_PAIR_COST 1.00 a materially losing position can never be recovery-hedged."""
    prm = pl.PairLockParams(recovery_confirmation_window_ms=0.0)
    assert prm.max_acceptable_pair_cost == 1.00
    f = _feat(logit_mid=math.log(0.4 / 0.6), **AGAINST_UP)
    for entry, bid in ((0.50, 0.40), (0.80, 0.70), (0.30, 0.20), (0.90, 0.60)):
        c = _cycle("UP", 10, entry)
        dec, why, rec = step(c, f, mv(bid), 1000.0, prm)
        assert dec is None and "MAX_ACCEPTABLE_PAIR_COST" in why
        for cand in rec["candidates"]:
            if cand["q"] > 0:
                assert cand["incr_pair_cost"] == pytest.approx(
                    entry + fee_per_share(entry) + (1 - bid) + prm.slippage_allowance + fee_per_share(1 - bid))
                assert cand["incr_pair_cost"] > 1.0 + (entry - bid)


def test_the_fok_limit_never_allows_a_fill_that_pairs_above_the_cap():
    prm = pl.PairLockParams(max_acceptable_pair_cost=1.15, recovery_confirmation_window_ms=0.0)
    c = _cycle("UP", 10, 0.50)
    f = _feat(logit_mid=math.log(0.4 / 0.6), **AGAINST_UP)
    dec, _, _ = step(c, f, mv(0.40), 1000.0, prm)
    assert dec is not None
    worst_fill = 0.50 + fee_per_share(0.50) + dec.limit + fee_per_share(dec.limit)
    assert worst_fill <= 1.15 + 1e-9 and dec.limit < 0.60 + prm.hedge_offset
    # a size that needs a level above the cap is not acceptable at all
    c2 = _cycle("UP", 10, 0.50)
    deep = mv(0.40, down_levels=[(0.60, 4.0), (0.66, 100.0)])
    dec, why, rec = step(c2, f, deep, 1000.0, prm)
    assert dec is None and ("the size needs" in why or "MAX_ACCEPTABLE_PAIR_COST" in why)


def test_candidates_are_fractions_of_the_hedgeable_quantity_capped_by_the_max_fraction():
    prm = pl.PairLockParams(**RELAXED, recovery_confirmation_window_ms=0.0, recovery_max_hedge_fraction=0.6)
    c = _cycle("UP", 10, 0.50)
    f = _feat(logit_mid=math.log(0.4 / 0.6), **AGAINST_UP)
    dec, _, rec = step(c, f, mv(0.40), 1000.0, prm)
    assert [x["q"] for x in rec["candidates"]] == [0.0, 1.2, 2.4, 3.6, 4.8, 6.0]
    assert dec is not None and dec.shares == 6.0                        # 1.2 .. 4.8 are under the venue minimum


def test_the_reevaluation_interval_the_action_limit_and_the_time_left():
    prm = pl.PairLockParams(**RELAXED, recovery_confirmation_window_ms=0.0, risk_aversion=0.0, reeval_ms=5000.0)
    f = _feat(logit_mid=math.log(0.4 / 0.6), **AGAINST_UP)
    c = _cycle("UP", 10, 0.50)
    assert step(c, f, mv(0.40), 1000.0, prm)[2]["decision"] == "REJECTED"
    assert step(c, f, mv(0.40), 3000.0, prm)[1] == "re-evaluation interval after a rejected recovery"
    assert step(c, f, mv(0.40), 6500.0, prm)[2] is not None
    c.hedge_actions = pl.RiskLimits().max_hedge_actions
    assert step(c, f, mv(0.40), 20_000.0, prm)[1] == "hedge actions exhausted"
    c2 = _cycle("UP", 10, 0.50)
    assert step(c2, f, mv(0.40), 1000.0, prm, left=5.0)[1] == "5 s left"


def test_never_chase_the_loss():
    """Two identical positions except for the size of the loss: the candidates' sizes and the change each one makes
    are identical; the pair-cost cap can only BLOCK the bigger loss's hedge, never enlarge it."""
    prm = pl.PairLockParams(max_acceptable_pair_cost=10.0, recovery_confirmation_window_ms=0.0)
    wide = pl.RiskLimits(max_market_capital=100.0, max_market_loss=100.0)
    f = _feat(logit_mid=math.log(0.3 / 0.7), **AGAINST_UP)
    out = []
    for entry_px in (0.50, 0.90):
        c = _cycle("UP", 10, entry_px)
        dec, _, rec = step(c, f, mv(0.29), 1000.0, prm, lim=wide, mkt=pl.MarketExposure())
        out.append((dec.shares, round(dec.info["expected_after"] - dec.info["expected_before"], 9),
                    round(dec.info["downside_reduction"], 9)))
    assert out[0] == out[1]
    tight = replace(prm, max_acceptable_pair_cost=1.15)
    small, big = _cycle("UP", 10, 0.50), _cycle("UP", 10, 0.90)
    d_small = step(small, f, mv(0.29), 1000.0, tight, lim=wide, mkt=pl.MarketExposure())[0]
    d_big = step(big, f, mv(0.29), 1000.0, tight, lim=wide, mkt=pl.MarketExposure())[0]
    assert d_big is None and (d_small is None or d_small.shares <= 10.0)


def test_no_hedging_in_strategy_a_and_each_mode_only_its_own_decision():
    f_rev = _feat(logit_mid=math.log(0.4 / 0.6), **AGAINST_UP)
    f_up = _feat(logit_mid=math.log(0.9 / 0.1))
    for mode, rec_ok, lock_ok in (("none", False, False), ("recovery", True, False), ("profit_lock", False, True),
                                  ("combined", True, True)):
        prm = pl.PairLockParams(**RELAXED, hedge_mode=mode, recovery_confirmation_window_ms=0.0)
        c = _cycle("UP", 10, 0.50)
        assert (step(c, f_rev, mv(0.40), 1000.0, prm)[0] is not None) is rec_ok, mode
        c = _cycle("UP", 10, 0.80)
        assert (step(c, f_up, mv(0.88), 1000.0, prm)[0] is not None) is lock_ok, mode


# ───────────────────────────── the replay's fill model == the paper venue ─────────────────────────────


def _replay2():
    import sys
    sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
    spec = importlib.util.spec_from_file_location("pairlock_replay2", "/root/troll-poly-bot/scripts/pairlock/replay2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("path,limit,want,px_want", [
    ([0.50, 0.51, 0.51] + [0.52] * 12, 0.53, "filled", 0.52),       # a tick up inside the limit: filled at the worse
    ([0.50, 0.51, 0.51] + [0.58] * 12, 0.53, "quote_pulled", None),  # past the limit 1 s later
    ([0.50, 0.50, 0.50] + [0.50] * 12, 0.53, "filled", 0.50),
    ([0.50, 0.56, 0.56] + [0.56] * 12, 0.53, "fok_unfillable", None),  # the ask ran past the limit before arrival
])
def test_the_v2_replay_fill_model_matches_the_paper_venue(path, limit, want, px_want, monkeypatch):
    from troll_poly_bot.execution.latency import INSTANT, LatencyModel
    from troll_poly_bot.execution.paper import FeeModel, PaperExchange
    from troll_poly_bot.execution.scenarios import scenario_named
    from troll_poly_bot.signals.costs import FeeSchedule
    from troll_poly_bot.types import BookLevel, Order, OrderBook, Side, TimeInForce
    rp = _replay2()
    monkeypatch.setattr(rp, "HAIRCUT", 0.0)
    t0 = 1_000_000.0
    ts = np.array([t0 + 213.0 * k for k in range(len(path))])
    # depth within two ticks 1500 -> three levels of 500: a 10-share order uses the first level only
    w = SimpleNamespace(t=ts, ua=np.array(path), ub=np.array(path) - 0.01, uad=np.full(len(path), 1500.0),
                        ubd=np.full(len(path), 1500.0), da=1 - np.array(path) + 0.01, db=1 - np.array(path),
                        close=t0 + 300_000.0)
    status, px, fps, _ = rp.fok(w, 0, "UP", 10.0, limit)
    prof = replace(INSTANT, name="replay", submit_base=rp.LAT_MS, ack_base=rp.ACK_MS, contention=0.0)
    ex = PaperExchange(LatencyModel(prof), FeeModel.from_schedule(FeeSchedule()), starting_balance=100.0,
                       scenario=scenario_named("calibrated"), seed=0)
    o = Order(token_id="UP", side=Side.BUY, price=limit, size=10.0, tif=TimeInForce.FOK)
    ex.submit(o, float(ts[0]))
    got = None
    for k in range(1, len(ts)):
        book = OrderBook("UP", bids=[BookLevel(round(path[k] - 0.01, 2), 500.0)], asks=[BookLevel(path[k], 500.0)])
        for r in ex.step(float(ts[k]), lambda tok, b=book: b, lambda tok: t0 + 300_000.0):
            got = r
    assert got is not None
    venue = "filled" if got.fills else got.reject_reason.value
    assert venue == status == want
    if want == "filled":
        assert px == got.fills[0].price == px_want
        assert fps == pytest.approx(fee_per_share(px_want))
