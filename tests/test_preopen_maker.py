"""PREOPEN_MAKER (strategy/preopen_maker.py): the flag and the refusal of real money, its own env prefix, the pure
pieces (classification, pair economics, one-sided EV), the pre-open bids through the pair hooks (two GTC bids at
T-45 s, not earlier, never a taker order), the inventory limits, fills -> classification and settlement P&L, and the
cancel of what still rests at the open."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from troll_poly_bot.__main__ import check_preopen_maker, check_spot_lag
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.strategy.engine import EngineConfig
from troll_poly_bot.strategy.preopen_maker import (
    CLASSES, POMConfig, PreopenMakerEngine, classify, one_sided_ev, pair_economics,
)
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.types import BookLevel, OrderBook, Side, TimeInForce

T_OPEN = 1_800_000_000_000.0
SLUG = "btc-updown-5m-1800000000"


def _market(slug=SLUG, open_ts=T_OPEN):
    return Market(condition_id="c", asset="BTC", yes_token_id=f"UP-{slug}", no_token_id=f"DOWN-{slug}", strike=0.0,
                  open_ts=open_ts, close_ts=open_ts + 300_000.0, slug=slug, min_size=5.0)


def _book(token, bid, ask, size=100.0):
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size)])


def _engine(**kw):
    cfg = POMConfig(enabled=True, **kw)
    risk = RiskManager(cfg=RiskConfig(max_position_usdc=50.0, max_total_exposure_usdc=100.0, max_concurrent_positions=8),
                       starting_balance=100.0)
    return PreopenMakerEngine(EngineConfig(strategy="preopen_maker"), cfg, risk, FeatureEngine())


def _request(e, now, m=None, up=(0.53, 0.55), dn=(0.45, 0.47), balance=100.0):
    m = m or _market()
    books = {m.slug: (_book(m.yes_token_id, *up), _book(m.no_token_id, *dn))}
    return e.pair_requests(now, {m.slug: m}, books, balance=balance)


def _place(e, now, m=None, **kw):
    m = m or _market()
    out = _request(e, now, m, **kw)
    for slug, side, o in out:
        e.on_pair_submit(f"oid-{side}-{slug[-4:]}", slug, side, o.price, o.size)
    return out


def _fill(e, m, side, qty, price, now):
    oid = f"oid-{side}-{m.slug[-4:]}"
    e.on_pair_fill_record(m.slug, SimpleNamespace(order_id=oid), SimpleNamespace(size=qty, price=price, fee=0.0), now)
    if e.markets[m.slug]["filled"][side] >= e.scfg.quantity - 1e-9:
        e.on_pair_done(oid, filled=True)


# ───────────────────────────── flags and isolation ─────────────────────────────


def test_off_by_default_and_refused_for_real_money():
    assert POMConfig().enabled is False
    with pytest.raises(ValueError, match="TPB_POM_ENABLED"):
        PreopenMakerEngine(EngineConfig(strategy="preopen_maker"), POMConfig(), RiskManager(cfg=RiskConfig(), starting_balance=50.0), FeatureEngine())
    with pytest.raises(SystemExit, match="TPB_POM_ENABLED"):
        check_preopen_maker("preopen_maker", "paper", env={})
    check_preopen_maker("preopen_maker", "paper", env={"TPB_POM_ENABLED": "1"})
    for mode in ("micro-live", "live", "shadow"):
        with pytest.raises(SystemExit, match="paper only"):
            check_preopen_maker("preopen_maker", mode, env={"TPB_POM_ENABLED": "1", "TPB_POM_ALLOW_REAL": "1"})
    check_preopen_maker("spot_lag", "live", env={})


def test_env_prefix_is_its_own():
    env = {"TPB_POM_ENABLED": "1", "TPB_POM_BID_PRICE": "0.46", "TPB_POM_QUANTITY": "20", "TPB_POM_PLACEMENT_S": "30",
           "TPB_POM_CANCEL_AFTER_S": "5", "TPB_POM_MARKET_TYPE": "15m", "TPB_POM_MAX_UNMATCHED_SHARES": "30", "TPB_SPOT_LAG_MAX_ASK": "0.60"}
    c = POMConfig.from_env(env)
    assert c.enabled and c.bid_price == 0.46 and c.quantity == 20 and c.placement_s == 30 and c.cancel_after_s == 5
    assert c.market_type == "15m" and c.max_unmatched_shares == 30 and c.order_type == "GTC"
    assert SpotLagConfig.from_env(env).enabled is False
    with pytest.raises(SystemExit):
        check_spot_lag("spot_lag", "paper", env=env)
    assert POMConfig(bid_price=0.47, bid_price_down=0.45).price_for("DOWN") == 0.45 and POMConfig(bid_price=0.47).price_for("UP") == 0.47


# ───────────────────────────── the pure pieces ─────────────────────────────


def test_classification_covers_the_seven_classes():
    q = 10.0
    assert classify(0, 0, q) == "NO_FILLS" and classify(10, 0, q) == "UP_ONLY" and classify(0, 10, q) == "DOWN_ONLY"
    assert classify(10, 10, q) == "BOTH" and classify(4, 0, q) == "PARTIAL_UP" and classify(0, 7, q) == "PARTIAL_DOWN"
    assert classify(10, 3, q) == "PARTIAL_BOTH" and classify(2, 2, q) == "PARTIAL_BOTH"
    assert set(CLASSES) == {"NO_FILLS", "UP_ONLY", "DOWN_ONLY", "BOTH", "PARTIAL_UP", "PARTIAL_DOWN", "PARTIAL_BOTH"}


def test_pair_economics_and_one_sided_ev():
    pe = pair_economics(10, 4.7, 10, 4.9)
    assert pe["paired"] == 10 and pe["combined_cost_per_share"] == pytest.approx(0.96)
    assert pe["guaranteed_gross"] == 10 and pe["net_guaranteed"] == pytest.approx(0.4) and pe["capital_locked"] == pytest.approx(9.6)
    pe = pair_economics(10, 4.7, 4, 1.96, fees=0.1)                        # partly paired
    assert pe["paired"] == 4 and pe["combined_cost_per_share"] == pytest.approx(0.96) and pe["net_guaranteed"] == pytest.approx(4 - 4 * 0.96 - 0.1)
    assert pair_economics(10, 4.7, 0, 0)["paired"] == 0
    assert one_sided_ev(0.63, 0.47) == pytest.approx(0.63 * 0.53 - 0.37 * 0.47)
    assert one_sided_ev(0.47, 0.47) == pytest.approx(0.0)                   # a fair price has zero EV


# ───────────────────────────── the engine through the hooks ─────────────────────────────


def test_two_gtc_bids_at_t_minus_45_and_not_before_and_never_a_taker():
    e = _engine()
    m = _market()
    assert _request(e, T_OPEN - 60_000.0, m) == []                           # too early: nothing, no record
    assert SLUG not in e.markets
    out = _place(e, T_OPEN - 44_000.0, m)
    assert [(s, o.side, o.tif, o.price, o.size) for _, s, o in out] == [("UP", Side.BUY, TimeInForce.GTC, 0.47, 10.0), ("DOWN", Side.BUY, TimeInForce.GTC, 0.47, 10.0)]
    assert all(o.expires_ts == m.close_ts for _, _, o in out)
    rec = e.markets[SLUG]
    assert rec["placed"] and rec["book_at_placement"]["UP"] == (0.53, 0.55) and rec["orders"]["UP"]["state"] == "resting"
    assert e.stats["markets_bid"] == 1 and e.stats["orders"] == 2
    # the same market again: nothing more (no chase, no re-price)
    assert _request(e, T_OPEN - 30_000.0, m) == []
    # evaluate never trades
    ev = e.evaluate(SimpleNamespace(market=m, fee=None), _book(m.yes_token_id, 0.5, 0.52), _book(m.no_token_id, 0.48, 0.5),
                    SimpleNamespace(price=100_000.0), 50.0, 50.0, T_OPEN + 10_000.0, None, None, None, 100.0)
    assert not ev.tradeable
    # a 15-minute market is not this bot's
    m15 = Market(condition_id="c2", asset="BTC", yes_token_id="U15", no_token_id="D15", strike=0.0, open_ts=T_OPEN,
                 close_ts=T_OPEN + 900_000.0, slug="btc-updown-15m-1800000000", min_size=5.0)
    assert e.pair_requests(T_OPEN - 40_000.0, {m15.slug: m15}, {m15.slug: (_book("U15", 0.5, 0.52), _book("D15", 0.48, 0.5))}, balance=100.0) == []


def test_no_books_means_try_again_and_too_late_is_recorded():
    e = _engine()
    m = _market()
    assert e.pair_requests(T_OPEN - 40_000.0, {m.slug: m}, {m.slug: (None, None)}, balance=100.0) == []
    assert e.skips["NO_BOOK"] == 1 and SLUG not in e.markets
    assert _request(e, T_OPEN + 5_000.0, m) == []                             # discovered after the open: too late
    assert e.skips["TOO_LATE"] == 1 and e.markets[SLUG]["placed"] is False


def test_fills_classify_the_market_and_settlement_books_pairs_and_one_sided_legs():
    e = _engine()
    m = _market()
    _place(e, T_OPEN - 45_000.0, m)
    _fill(e, m, "UP", 10.0, 0.47, T_OPEN - 20_000.0)
    assert e.markets[SLUG]["orders"]["UP"]["state"] == "filled" and e.stats["fills"] == 1
    # DOWN still resting at the open: cancelled
    ids = e.pair_cancels(T_OPEN + 100.0)
    assert ids == [f"oid-DOWN-{SLUG[-4:]}"]
    e.on_pair_done(ids[0], filled=False)
    assert e.markets[SLUG]["orders"]["DOWN"]["state"] == "cancelled" and e.stats["cancelled_unfilled"] == 1
    e.on_market_settled(SLUG, True, 0.0, m.close_ts + 60_000.0)
    r = e.last_settled[-1]
    assert r["class"] == "UP_ONLY" and r["pnl"] == pytest.approx(10 * (1 - 0.47)) and e.stats["one_sided"] == 1 and e.stats["one_sided_won"] == 1
    # a BOTH market: paired, guaranteed
    e2 = _engine()
    m2 = _market("btc-updown-5m-1800000300", T_OPEN + 300_000.0)
    _place(e2, m2.open_ts - 45_000.0, m2)
    _fill(e2, m2, "UP", 10.0, 0.47, m2.open_ts - 20_000.0)
    _fill(e2, m2, "DOWN", 10.0, 0.47, m2.open_ts - 10_000.0)
    e2.on_market_settled(m2.slug, False, 0.0, m2.close_ts + 60_000.0)
    r = e2.last_settled[-1]
    assert r["class"] == "BOTH" and r["pnl"] == pytest.approx(10 - 9.4) and r["pair_cost_per_share"] == pytest.approx(0.94)
    assert e2.stats["pairs"] == 1 and e2.stats["pair_pnl"] == pytest.approx(0.6) and e2.stats["one_sided"] == 0
    # a partial DOWN with a full UP, DOWN wins: partly paired, the rest one-sided and lost
    e3 = _engine()
    m3 = _market("btc-updown-5m-1800000600", T_OPEN + 600_000.0)
    _place(e3, m3.open_ts - 45_000.0, m3)
    _fill(e3, m3, "UP", 10.0, 0.47, m3.open_ts - 20_000.0)
    _fill(e3, m3, "DOWN", 4.0, 0.47, m3.open_ts - 5_000.0)
    assert e3.stats["partial_fills"] == 1
    e3.on_market_settled(m3.slug, False, 0.0, m3.close_ts + 60_000.0)
    r = e3.last_settled[-1]
    assert r["class"] == "PARTIAL_BOTH" and r["pnl"] == pytest.approx(4.0 - 14 * 0.47)


def test_inventory_limits_stop_new_bids_and_say_why():
    e = _engine(max_markets_with_open_positions=1)
    m1 = _market()
    _place(e, T_OPEN - 45_000.0, m1)
    _fill(e, m1, "UP", 10.0, 0.47, T_OPEN - 20_000.0)                        # one market with an open position
    m2 = _market("btc-updown-5m-1800000300", T_OPEN + 300_000.0)
    assert _request(e, m2.open_ts - 45_000.0, m2) == []
    assert e.skips["MAX_MARKETS_WITH_OPEN_POSITIONS"] == 1 and e.markets[m2.slug]["placed"] is False
    e4 = _engine(max_unmatched_shares=5.0)
    _place(e4, T_OPEN - 45_000.0, m1)
    _fill(e4, m1, "DOWN", 10.0, 0.47, T_OPEN - 20_000.0)                      # 10 unmatched DOWN
    assert _request(e4, m2.open_ts - 45_000.0, m2) == [] and e4.skips["MAX_UNMATCHED_SHARES"] == 1
    e5 = _engine()
    assert _request(e5, T_OPEN - 45_000.0, m1, balance=5.0) == [] and e5.skips["INSUFFICIENT_CASH"] == 1
    e6 = _engine(max_total_open_exposure_usdc=5.0)
    assert _request(e6, T_OPEN - 45_000.0, m1) == [] and e6.skips["MAX_TOTAL_OPEN_EXPOSURE"] == 1


def test_snapshot_carries_stats_classes_skips_and_inventory():
    e = _engine()
    snap = e.snapshot()
    assert snap["strategy"] == "preopen_maker" and set(snap["pom"]["classes"]) == set(CLASSES)
    assert snap["pom"]["inventory"]["unmatched_shares"] == 0.0 and snap["pom"]["stats"]["orders"] == 0
