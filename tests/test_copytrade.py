"""COPYTRADE (strategy/copytrade.py): the flag and the refusal of real money, its own env prefix, the poller's
de-duplication and priming, the copy of a followed BUY as a FOK at his price + premium with the size cap, every miss
reason (sell, unknown market, wrong type, too late, price gone, limits, cash), fills and settlement, the snapshot."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from troll_poly_bot.__main__ import check_copytrade, check_spot_lag
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.strategy.copytrade import MISSES, CopyConfig, CopyTradeEngine, WalletPoller
from troll_poly_bot.strategy.engine import EngineConfig
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.types import BookLevel, OrderBook, Side, TimeInForce

T_OPEN = 1_800_000_000_000.0
SLUG = "btc-updown-5m-1800000000"
UP, DOWN = "tok-up", "tok-down"
W = "0x21d0a97aac03917e752857a551bbe5103a00e8d7"


def _market(slug=SLUG, open_ts=T_OPEN, yes=UP, no=DOWN, dur_s=300.0):
    return Market(condition_id="c", asset="BTC", yes_token_id=yes, no_token_id=no, strike=0.0, open_ts=open_ts,
                  close_ts=open_ts + dur_s * 1000.0, slug=slug, min_size=5.0)


def _book(token, bid, ask):
    return OrderBook(token, bids=[BookLevel(bid, 100.0)], asks=[BookLevel(ask, 100.0)])


def _engine(**kw):
    cfg = CopyConfig(enabled=True, **kw)
    risk = RiskManager(cfg=RiskConfig(max_position_usdc=50.0, max_total_exposure_usdc=100.0, max_concurrent_positions=8), starting_balance=100.0)
    poller = WalletPoller(cfg.wallet, cfg.poll_s, fetch=lambda: [])
    return CopyTradeEngine(EngineConfig(strategy="copytrade"), cfg, risk, FeatureEngine(), poller=poller, start_poller=False)


def _row(side="BUY", token=UP, size=30.0, price=0.47, ts=None, outcome="Up", slug=SLUG, tx="0xabc"):
    return {"transactionHash": tx, "asset": token, "size": size, "price": price, "timestamp": ts if ts is not None else T_OPEN / 1000.0 - 40.0,
            "side": side, "outcome": outcome, "slug": slug}


def _prime(e):
    e.poller.ingest([], T_OPEN / 1000.0 - 100.0)          # an empty first poll primes nothing; prime explicitly:
    e.poller.seen["primed"] = 0.0


def _request(e, now, rows, seen_at=None, up=(0.46, 0.48), dn=(0.51, 0.53), balance=100.0, m=None):
    m = m or _market()
    e.poller.ingest(rows, seen_at if seen_at is not None else now / 1000.0)
    books = {m.slug: (_book(m.yes_token_id, *up), _book(m.no_token_id, *dn))}
    return e.pair_requests(now, {m.slug: m}, books, balance=balance)


def test_off_by_default_and_refused_for_real_money():
    assert CopyConfig().enabled is False
    with pytest.raises(ValueError, match="TPB_COPY_ENABLED"):
        CopyTradeEngine(EngineConfig(strategy="copytrade"), CopyConfig(), RiskManager(cfg=RiskConfig(), starting_balance=50.0), FeatureEngine(), start_poller=False)
    with pytest.raises(SystemExit, match="TPB_COPY_ENABLED"):
        check_copytrade("copytrade", "paper", env={})
    check_copytrade("copytrade", "paper", env={"TPB_COPY_ENABLED": "1"})
    for mode in ("micro-live", "live", "shadow"):
        with pytest.raises(SystemExit, match="paper only"):
            check_copytrade("copytrade", mode, env={"TPB_COPY_ENABLED": "1"})
    check_copytrade("spot_lag", "live", env={})


def test_env_prefix_is_its_own():
    env = {"TPB_COPY_ENABLED": "1", "TPB_COPY_WALLET": "0xdead", "TPB_COPY_SIZE_CAP": "20", "TPB_COPY_MAX_PREMIUM": "0.03",
           "TPB_COPY_MARKET_TYPES": "5m,15m", "TPB_COPY_MAX_AGE_S": "9", "TPB_SPOT_LAG_MAX_ASK": "0.60"}
    c = CopyConfig.from_env(env)
    assert c.enabled and c.wallet == "0xdead" and c.size_cap == 20 and c.max_premium == 0.03 and c.types() == {"5m", "15m"} and c.max_age_s == 9
    assert SpotLagConfig.from_env(env).enabled is False
    with pytest.raises(SystemExit):
        check_spot_lag("spot_lag", "paper", env=env)


def test_the_poller_primes_on_its_first_poll_and_never_queues_a_trade_twice():
    p = WalletPoller(W, 0.5, fetch=lambda: [])
    assert p.ingest([_row(tx="0x1"), _row(tx="0x2")], 1.0) == 0 and not p.queue          # first poll: primed, nothing queued
    assert p.ingest([_row(tx="0x3"), _row(tx="0x1")], 2.0) == 1 and len(p.queue) == 1
    assert p.ingest([_row(tx="0x3")], 3.0) == 0
    q = p.queue[0]
    assert q["tx"] == "0x3" and q["seen_at"] == 2.0 and q["side"] == "BUY" and q["token"] == UP and q["size"] == 30.0 and q["price"] == 0.47


def test_a_followed_buy_is_copied_as_a_fok_at_his_price_plus_premium_with_the_size_cap():
    e = _engine()
    _prime(e)
    now = T_OPEN - 35_000.0                                            # his fill was at T-40 s, seen 5 s later
    out = _request(e, now, [_row(size=30.0, price=0.47)])
    assert len(out) == 1
    slug, side, o = out[0]
    assert slug == SLUG and side == "UP" and o.side is Side.BUY and o.tif is TimeInForce.FOK and o.price == pytest.approx(0.49) and o.size == 10.0
    rec = e.last_copies[-1]
    assert rec["copy"] == "SENT" and rec["lag_s"] == pytest.approx(5.0) and rec["ask_at_decision"] == 0.48
    assert e.stats["his_fills_seen"] == 1 and e.stats["copies_sent"] == 1
    # the fill and the settlement
    e.on_pair_submit(o.order_id, SLUG, "UP", o.price, o.size)
    e.on_pair_fill_record(SLUG, SimpleNamespace(order_id=o.order_id), SimpleNamespace(size=10.0, price=0.48, fee=0.17), now + 300.0)
    assert e.stats["copies_filled"] == 1 and e.last_copies[-1]["slippage_vs_his"] == pytest.approx(0.01)
    e.on_market_settled(SLUG, True, 0.0, T_OPEN + 400_000.0)
    assert e.stats["settled"] == 1 and e.stats["pnl"] == pytest.approx(10 - 4.8 - 0.17)
    # fixed size mode and a DOWN copy
    e2 = _engine(size_mode="fixed", size_cap=7.0)
    _prime(e2)
    out = _request(e2, now, [_row(token=DOWN, outcome="Down", size=3.0, price=0.52)], dn=(0.51, 0.53))
    assert out[0][1] == "DOWN" and out[0][2].size == 7.0 and out[0][2].price == pytest.approx(0.54)


def test_every_miss_reason_is_recorded_and_nothing_is_sent():
    now = T_OPEN - 35_000.0
    e = _engine(); _prime(e)
    assert _request(e, now, [_row(side="SELL")]) == [] and e.misses["MISSED_SELL"] == 1
    e = _engine(); _prime(e)
    assert _request(e, now, [_row(token="unknown-token", slug="btc-updown-5m-1")]) == [] and e.misses["MISSED_MARKET"] == 1
    e = _engine(); _prime(e)                                             # his 15m / ETH / hourly fills are untracked BY DESIGN: type misses
    assert _request(e, now, [_row(token="t15", slug="btc-updown-15m-1", tx="0x15"), _row(token="te", slug="eth-updown-5m-1", tx="0xe"),
                             _row(token="th", slug="bitcoin-up-or-down-october-7-2026-11am-et", tx="0xh")]) == []
    assert e.misses["MISSED_TYPE"] == 3 and e.misses["MISSED_MARKET"] == 0
    e = _engine(); _prime(e)                                             # a 15-minute market when only 5m is followed
    m15 = _market("btc-updown-15m-1800000000", yes="u15", no="d15", dur_s=900.0)
    e.poller.ingest([_row(token="u15", slug=m15.slug)], now / 1000.0)
    assert e.pair_requests(now, {m15.slug: m15}, {m15.slug: (_book("u15", 0.46, 0.48), _book("d15", 0.51, 0.53))}, balance=100.0) == []
    assert e.misses["MISSED_TYPE"] == 1
    e = _engine(); _prime(e)                                             # seen 30 s after the fill: too late
    assert _request(e, now, [_row(ts=T_OPEN / 1000.0 - 65.0)]) == [] and e.misses["MISSED_LATE"] == 1
    e = _engine(); _prime(e)                                             # the ask has run 3c past his price: not chased
    assert _request(e, now, [_row(price=0.47)], up=(0.49, 0.51)) == [] and e.misses["MISSED_PRICE"] == 1
    e = _engine(max_total_open_exposure_usdc=3.0); _prime(e)
    assert _request(e, now, [_row()]) == [] and e.misses["MISSED_LIMIT"] == 1
    e = _engine(); _prime(e)
    assert _request(e, now, [_row()], balance=2.0) == [] and e.misses["MISSED_CASH"] == 1
    assert set(e.snapshot()["copy"]["misses"]) == set(MISSES)


def test_the_cancel_hook_exists_because_live_reaches_pair_requests_through_it():
    e = _engine()
    assert e.pair_cancels(T_OPEN) == [] and callable(getattr(e, "pair_requests", None))


def test_a_killed_copy_counts_as_missed_price():
    e = _engine(); _prime(e)
    now = T_OPEN - 35_000.0
    (slug, side, o), = _request(e, now, [_row()])
    e.on_pair_submit(o.order_id, slug, side, o.price, o.size)
    e.on_pair_done(o.order_id, filled=False, killed=True)
    assert e.stats["copies_killed"] == 1 and e.last_copies[-1]["copy"] == "KILLED" and e.misses["MISSED_PRICE"] == 1
    snap = e.snapshot()
    assert snap["strategy"] == "copytrade" and snap["copy"]["stats"]["copies_sent"] == 1 and snap["copy"]["poller"]["alive"] is False
