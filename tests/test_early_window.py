"""EARLY_WINDOW_LOW_ASK: the first-minute entry window, every entry gate, the
ask-only entry price, and the replay's realistic fills on synthetic windows."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from troll_poly_bot.backtest.early_window_replay import EarlyWindowReplay, bucket_of, summarize
from troll_poly_bot.backtest.momentum_replay import COLS, OPTIMISTIC, REALISTIC, Window
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy import early_window as ew
from troll_poly_bot.strategy.early_window import (
    TARGET_OR_TIME, EarlyWindowConfig, Quote, entry_window_reason, evaluate_entry, exit_reason,
    seconds_since_market_start,
)

OPEN = 1_790_000_000_000.0          # 12:00:00
CLOSE = OPEN + 300_000.0
FEE = FeeSchedule()
CFG = EarlyWindowConfig()


def _quotes(up_bid=0.34, up_ask=0.35, up_depth=200.0, dn_bid=0.65, dn_ask=0.66, dn_depth=200.0):
    return {"UP": Quote(bid=up_bid, bid_depth=up_depth, ask=up_ask, ask_depth=up_depth),
            "DOWN": Quote(bid=dn_bid, bid_depth=dn_depth, ask=dn_ask, ask_depth=dn_depth)}


def _eval(cfg=CFG, now=OPEN + 30_000.0, quotes=None, p_up=0.50, book_age=50.0, spot_age=300.0, n_sources=3,
          dev=0.0, unc=0.0):
    return evaluate_entry(cfg, now_ms=now, open_ts_ms=OPEN, close_ts_ms=CLOSE, quotes=quotes or _quotes(),
                          book_age_ms=book_age, spot_age_ms=spot_age, n_sources=n_sources, source_deviation_bps=dev,
                          p_up_model=p_up, model_uncertainty=unc, fee=FEE, one_way_ms=150.0)


# ─────────────────────────────── timing ──────────────────────────────────


@pytest.mark.parametrize("offset_s, allowed", [
    (-1.0, False),      # 11:59:59  no buy
    (-0.001, False),
    (0.0, True),        # 12:00:00  allowed
    (10.0, True),
    (30.0, True),
    (59.0, True),
    (60.0, True),       # 12:01:00  final allowed entry
    (60.001, False),
    (61.0, False),      # 12:01:01  no new buy
])
def test_entry_window_is_the_first_minute_inclusive(offset_s, allowed):
    s = seconds_since_market_start(OPEN, OPEN + offset_s * 1000.0)
    assert (entry_window_reason(s, CFG) is None) is allowed
    d = _eval(now=OPEN + offset_s * 1000.0)
    assert d.ok is allowed
    if not allowed:
        assert d.reason == (ew.BEFORE_MARKET_START if offset_s < 0 else ew.ENTRY_WINDOW_CLOSED)


def test_pre_open_is_refused_even_with_a_huge_edge():
    d = _eval(now=OPEN - 500.0, p_up=0.95)
    assert not d.ok and d.reason == ew.BEFORE_MARKET_START


def test_bucket_labels():
    assert bucket_of(0.0) == "0-10s"
    assert bucket_of(9.999) == "0-10s"
    assert bucket_of(10.0) == "10-20s"
    assert bucket_of(60.0) == "50-60s"
    assert bucket_of(-0.1) == "pre-open"
    assert bucket_of(60.1) == "after 60s"


# ─────────────────────────────── entry gates ─────────────────────────────


def test_a_cheap_ask_with_model_edge_is_bought_at_the_ask():
    d = _eval(p_up=0.50)
    assert d.ok and d.side == "UP"
    assert d.ask == 0.35                     # the executable ask, not the mid (0.345)
    assert d.model_edge == pytest.approx(0.5 * 0.50 + 0.5 * 0.345 - 0.35)
    assert d.expected_net > 0
    assert 0.35 <= d.limit_price <= 0.35 + CFG.max_slippage + 1e-9
    assert d.size == CFG.order_size


def test_ask_must_be_strictly_below_the_threshold():
    q = _quotes(up_bid=0.39, up_ask=0.40, dn_bid=0.60, dn_ask=0.61)
    d = _eval(quotes=q, p_up=0.9)
    assert not d.ok and d.reason == ew.ASK_NOT_LOW
    q = _quotes(up_bid=0.38, up_ask=0.39, dn_bid=0.61, dn_ask=0.62)
    assert _eval(quotes=q, p_up=0.9).ok


def test_the_mid_is_never_the_entry_price():
    # the mid (0.385) is under the threshold, the ask (0.41) is not: no trade
    q = _quotes(up_bid=0.36, up_ask=0.41, dn_bid=0.59, dn_ask=0.64)
    d = _eval(quotes=q, p_up=0.9, cfg=replace(CFG, max_spread=0.10, max_spread_pct=1.0))
    assert not d.ok and d.reason == ew.ASK_NOT_LOW


@pytest.mark.parametrize("kwargs, reason", [
    ({"book_age": 5_000.0}, ew.STALE_BOOK),
    ({"book_age": None}, ew.STALE_BOOK),
    ({"spot_age": 5_000.0}, ew.STALE_SPOT),
    ({"n_sources": 0}, ew.DATA_INCONSISTENT),
    ({"n_sources": 3, "dev": 40.0}, ew.DATA_INCONSISTENT),
    ({"p_up": None}, ew.MODEL_NOT_READY),
    ({"quotes": _quotes(up_bid=0.31, up_ask=0.35)}, ew.SPREAD_TOO_WIDE),
    ({"quotes": _quotes(up_bid=None, up_ask=0.35)}, ew.SPREAD_TOO_WIDE),
    ({"quotes": _quotes(up_bid=0.08, up_ask=0.10)}, ew.SPREAD_PCT_TOO_WIDE),     # 2 c on 10 c = 20 %
    ({"quotes": _quotes(up_depth=6.0)}, ew.THIN_ASK),
    ({"quotes": _quotes(up_depth=9.0), "cfg": replace(CFG, min_ask_depth=5.0)}, ew.EXECUTION_NOT_REALISTIC),
    ({"quotes": _quotes(up_bid=0.02, up_ask=0.03), "p_up": 0.5}, ew.ASK_TOO_LOW),
    ({"p_up": 0.36}, ew.EDGE_TOO_SMALL),
    ({"p_up": 0.43, "unc": 0.10}, ew.NET_NOT_POSITIVE),
    ({"quotes": _quotes(up_bid=0.35, up_ask=0.35)}, ew.BOOK_INVALID),
])
def test_every_gate_rejects_with_its_reason(kwargs, reason):
    d = _eval(**kwargs)
    assert not d.ok
    assert d.reason == reason


def test_expected_net_charges_every_cost():
    d = _eval(p_up=0.50)
    c = d.costs
    assert c["entry_fee"] == pytest.approx(FEE.taker_fee_per_share(0.35), abs=1e-5)
    assert c["exit_fee"] == pytest.approx(FEE.taker_fee_per_share(0.40), abs=1e-5)
    assert c["slippage"] == CFG.expected_slippage
    assert c["latency"] == pytest.approx(0.01 * (300.0 + 150.0) / 1000.0, abs=1e-5)
    assert d.expected_net == pytest.approx(d.model_edge - c["total"], abs=1e-4)


def test_the_cap_never_reaches_a_price_with_non_positive_net():
    d = _eval(p_up=0.50)
    net_at_cap = d.p_side - d.limit_price - ew.expected_costs(CFG, FEE, d.limit_price, 0.45, 0.0)["total"]
    assert net_at_cap > CFG.min_expected_net


def test_down_side_is_bought_when_it_is_the_cheap_one():
    q = _quotes(up_bid=0.64, up_ask=0.65, dn_bid=0.34, dn_ask=0.35)
    d = _eval(quotes=q, p_up=0.50)
    assert d.ok and d.side == "DOWN" and d.ask == 0.35


# ─────────────────────────────── exits ───────────────────────────────────


def test_take_profit_needs_the_bid_at_target_and_net_after_both_fees():
    assert exit_reason(CFG, FEE, entry=0.35, bid=0.39, held_s=5, secs_left=200) is None
    assert exit_reason(CFG, FEE, entry=0.35, bid=0.40, held_s=5, secs_left=200) == "TAKE_PROFIT"
    assert exit_reason(CFG, FEE, entry=0.35, bid=0.40, held_s=5, secs_left=9) is None     # rides to the bell


def test_time_exit_only_in_target_or_time_mode():
    assert exit_reason(CFG, FEE, entry=0.35, bid=0.33, held_s=61, secs_left=200) is None
    cfg = replace(CFG, exit_mode=TARGET_OR_TIME)
    assert exit_reason(cfg, FEE, entry=0.35, bid=0.33, held_s=61, secs_left=200) == "TIME_EXIT"


# ─────────────────────────────── replay ──────────────────────────────────


def _window(up_bid, up_ask, depth=200.0, up_won=True, step_ms=250.0, strike=100.0, spot=100.0) -> Window:
    """Synthetic window from 60 s before the open to the close. ``up_bid`` /
    ``up_ask`` are functions of seconds since the open; DOWN mirrors UP."""
    ts = np.arange(OPEN - 60_000.0, CLOSE + 1.0, step_ms)
    n = len(ts)
    cols = {c: np.full(n, np.nan) for c in COLS}
    for i, t in enumerate(ts):
        s = (t - OPEN) / 1000.0
        b, a = up_bid(s), up_ask(s)
        # a tiny wiggle so the spot feed is never "dead"; strike = spot
        cols["spot"][i] = spot * (1.0 + 1e-6 * (i % 2))
        cols["ub"][i], cols["ua"][i] = b, a
        cols["db"][i], cols["da"][i] = round(1.0 - a, 4), round(1.0 - b, 4)
        cols["ubd"][i] = cols["uad"][i] = cols["dbd"][i] = cols["dad"][i] = depth
        cols["up"][i] = (b + a) / 2.0
        cols["nsrc"][i] = 3
    return Window(slug="btc-updown-5m-1790000000", asset="BTC", open_ts=OPEN, close_ts=CLOSE, up_won=up_won,
                  fee=FEE, t=[float(x) for x in ts], cols=cols, strike=strike)


def _warm(rep: EarlyWindowReplay):
    """A ready vol estimator with a realistic sigma (the synthetic spot is flat)."""
    class _Vol:
        ready = True
        sigma_per_sec = 1e-4

        def update(self, *_):
            pass
    st = rep._asset_state("BTC", OPEN - 60_000.0)
    st["vol"] = _Vol()
    st["last_t"] = OPEN - 60_001.0          # contiguous with the window: no restart
    return rep


def test_replay_never_orders_before_the_open_or_after_60s():
    # cheap UP token with a model edge the whole time, pre-open included
    w = _window(lambda s: 0.34, lambda s: 0.35)
    rep = _warm(EarlyWindowReplay(CFG, OPTIMISTIC))
    rep.run([w])
    assert rep.funnel["pre-open"][ew.BEFORE_MARKET_START] > 0
    assert rep.orders, "expected an entry inside the first minute"
    assert all(0.0 <= o["s_decision"] <= 60.0 for o in rep.orders)
    assert rep.orders[0]["s_decision"] == pytest.approx(0.0, abs=0.3)


def test_replay_fills_at_the_arrival_ask_and_takes_profit_at_the_bid():
    # the token re-prices from 0.35 to 0.45 at 20 s
    w = _window(lambda s: 0.34 if s < 20 else 0.44, lambda s: 0.35 if s < 20 else 0.45)
    rep = _warm(EarlyWindowReplay(CFG, REALISTIC))
    rep.run([w])
    assert len(rep.trades) == 1
    t = rep.trades[0]
    assert t["fill_px"] == 0.35 and t["side"] == "UP"
    assert t["exit_kind"] == "TAKE_PROFIT" and t["exit_px"] == 0.44
    fees = (FEE.taker_fee_per_share(0.35) + FEE.taker_fee_per_share(0.44)) * 5
    assert t["pnl"] == pytest.approx((0.44 - 0.35) * 5 - fees, abs=1e-4)
    assert t["target_hit"] and t["mfe"] == pytest.approx(0.09)
    s = summarize(rep.trades, rep.orders, rep.opportunities)
    assert s["fills"] == 1 and s["target_hit_rate"] == 1.0


def test_replay_kills_an_order_whose_quote_vanishes_in_flight():
    # the cheap ask exists for one recorded point only
    w = _window(lambda s: 0.34 if 5.0 <= s < 5.2 else 0.59, lambda s: 0.35 if 5.0 <= s < 5.2 else 0.60)
    rep = _warm(EarlyWindowReplay(CFG, REALISTIC))
    rep.run([w])
    assert rep.orders and rep.orders[0]["outcome"] in ("KILLED", "QUOTE_PULLED")
    assert not rep.trades


def test_replay_settles_a_held_loser_at_zero():
    w = _window(lambda s: 0.34 if s < 60 else 0.10, lambda s: 0.35 if s < 60 else 0.11, up_won=False)
    rep = _warm(EarlyWindowReplay(CFG, REALISTIC))
    rep.run([w])
    t = rep.trades[0]
    assert t["exit_kind"] == "SETTLED" and t["exit_px"] == 0.0
    assert t["pnl"] == pytest.approx(-0.35 * 5 - FEE.taker_fee_per_share(0.35) * 5, abs=1e-4)
    assert t["mae_to_close"] == pytest.approx(0.10 - 0.35)
