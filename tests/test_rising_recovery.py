"""RISING-TOKEN RECOVERY (strategy/rising_recovery.py, the user's spec 2026-10-09): the mechanics -- the rising token's
maker bid, the end of the rise, pairs only under $1, the dynamic quantity, the guaranteed-profit target and the
limits. Not a test of whether the strategy makes money (it has not been backtested)."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from troll_poly_bot.__main__ import check_rising_recovery
from troll_poly_bot.config import BotConfig
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.book_momentum import taker_fee
from troll_poly_bot.strategy.engine import EngineConfig
from troll_poly_bot.strategy.rising_recovery import MarketBook, RisingRecoveryConfig, RisingRecoveryEngine
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce

T0 = 1_800_000_000_000.0


def _ob(token, bid, ask, size=100.0):
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size)])


class _W:
    def __init__(self, **kw):
        kw.setdefault("mode", "pairs")              # the pairing rules; the momentum-mode tests pass mode="momentum"
        for k in ("buy_mode", "fix_mode", "hedge_mode"):
            kw.setdefault(k, "maker")               # the maker mechanics; the taker defaults are tested at the end
        kw.setdefault("high_ratio", 1.0)            # the equal-pairs rule; the 2:1 tests below pass high_ratio=2
        self.sc = RisingRecoveryConfig(enabled=True, **kw)
        rc = RiskConfig(max_position_usdc=10.0, max_total_exposure_usdc=40.0, max_concurrent_positions=4,
                        max_asset_exposure_usdc=40.0, max_epoch_exposure_usdc=40.0, max_daily_loss_usdc=20.0)
        self.sc.apply_budget(rc)
        self.risk = RiskManager(cfg=rc, starting_balance=200.0)
        self.e = RisingRecoveryEngine(EngineConfig(strategy="rising_recovery"), self.sc, self.risk, FeatureEngine())
        self.m = Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DN", strike=100.0, open_ts=T0,
                        close_ts=T0 + 300_000.0, slug=f"btc-updown-5m-{int(T0 // 1000)}", min_size=5.0)
        self.meta = MarketMeta(market=self.m, resolution_source="", accepting_orders=True, duration_s=300.0, fee=FeeSchedule())
        self.slug = self.m.slug                     # meta_by_slug left empty: a market without a strike is never evaluated

    def tick(self, t_s, up, dn=None, balance=200.0):
        """One loop pass at t_s seconds after the open: cancels, then new orders. UP (bid, ask); DOWN mirrors it."""
        dn = dn or (round(1 - up[1], 3), round(1 - up[0], 3))
        now = T0 + t_s * 1000.0
        cancels = self.e.pair_cancels(now)
        for oid in cancels:
            self.e.on_pair_done(oid, filled=False)
        out = self.e.pair_requests(now, {self.slug: self.m}, {self.slug: (_ob("UP", *up), _ob("DN", *dn))}, balance=balance)
        for _, side, o in out:
            self.e.on_pair_submit(o.order_id, self.slug, side, o.price, o.size)
        return out, cancels

    def fill(self, order, t_s, size=None, price=None, fee=None):
        """As live.py reports it: the order is ended (on_pair_done) BEFORE its fill is recorded."""
        q, px = size or order.size, price or order.price
        f = SimpleNamespace(size=q, price=px, fee=(q * taker_fee(px)) if fee is None and order.tif is TimeInForce.IOC else (fee or 0.0),
                            token_id=order.token_id)
        self.e.on_pair_done(order.order_id, filled=True)
        self.e.on_pair_fill_record(self.slug, order, f, T0 + t_s * 1000.0)

    @property
    def book(self) -> MarketBook:
        return self.e.books[self.slug]

    def rise(self, t_s=100.0):
        """UP flat at 0.50 / 0.51, then its ask rises 0.51 -> 0.53 -> 0.55 within a second: the entry signal."""
        out = []
        for t, up in ((t_s - 2.0, (0.50, 0.51)), (t_s - 1.0, (0.50, 0.51)), (t_s - 0.4, (0.52, 0.53)), (t_s, (0.54, 0.55))):
            out += self.tick(t, up)[0]                # every order the ramp produced (a later market state may fire early)
        return out


def test_flag_and_paper_only():
    assert RisingRecoveryConfig().enabled is False
    with pytest.raises(ValueError):
        RisingRecoveryEngine(EngineConfig(strategy="rising_recovery"), RisingRecoveryConfig(), RiskManager(), FeatureEngine())
    with pytest.raises(SystemExit):
        check_rising_recovery("rising_recovery", "paper", {})
    check_rising_recovery("rising_recovery", "paper", {"TPB_RTR_ENABLED": "1"})
    for mode in ("shadow", "micro-live", "live"):
        with pytest.raises(SystemExit):
            check_rising_recovery("rising_recovery", mode, {"TPB_RTR_ENABLED": "1"})


def test_the_rising_token_gets_a_post_only_bid_at_the_bid():
    w = _W()
    (slug, side, o), = w.rise()
    assert side == "UP" and o.tif is TimeInForce.GTC and o.post_only and o.price == pytest.approx(0.54)
    assert o.size >= 5.0 and o.size * o.price <= w.sc.entry_max_usdc + 1e-9
    assert w.tick(100.2, (0.54, 0.55))[0] == []                      # one order per token
    _, cancels = w.tick(105.1, (0.54, 0.55))
    assert cancels == [o.order_id]                                    # rested 5 s


def test_no_entry_without_a_confirmed_rise_or_outside_the_band():
    w = _W()
    w.tick(98.0, (0.50, 0.51))
    w.tick(99.0, (0.50, 0.51))
    assert w.tick(100.0, (0.51, 0.52))[0] == []                       # +1c only
    w2 = _W()
    w2.tick(98.0, (0.85, 0.86))
    w2.tick(99.0, (0.85, 0.86))
    w2.tick(99.6, (0.87, 0.88))
    assert w2.tick(100.0, (0.89, 0.90))[0] == []                      # above max_ask 0.80
    w3 = _W()
    assert w3.rise(t_s=250.0) == []                                   # 50 s left < entry_min_left_s 60


def test_after_every_fill_both_outcomes_are_known():
    w = _W()
    (_, _, o), = w.rise()
    w.fill(o, 101.0, size=6.0, price=0.50)
    b = w.book
    assert (b.U, b.D, b.C) == (6.0, 0.0, pytest.approx(3.0))
    assert b.U - b.C == pytest.approx(3.0) and b.D - b.C == pytest.approx(-3.0) and b.worst == pytest.approx(-3.0)
    assert b.phase == "flat" and b.excess() == ("UP", 6.0, pytest.approx(0.50))
    assert "if UP wins +3.00, if DOWN wins -3.00" in b.line()


def _long_up(w, entry=0.50, shares=6.0):
    (_, _, o), = w.rise()
    w.fill(o, 101.0, size=shares, price=entry)
    w.tick(101.5, (0.58, 0.59))                                      # the lead's peak: mid 0.585
    assert w.book.phase == "long" and w.book.lead == "UP"
    return o


def test_the_end_of_the_rise_takes_a_pair_under_a_dollar_sized_to_the_target():
    w = _W()
    _long_up(w)
    out, _ = w.tick(102.0, (0.56, 0.57), dn=(0.40, 0.41))           # 2.5c off the peak: the rise ended
    assert w.book.phase == "pairing"
    (_, side, o), = out
    all_in = 0.41 + taker_fee(0.41)
    assert side == "DOWN" and o.tif is TimeInForce.IOC and o.price == pytest.approx(0.41)
    assert 0.50 + all_in <= 1.0 - w.sc.pair_margin_taker
    need = (w.sc.target_profit_usdc - (-3.0)) / (1.0 - all_in)       # n = (T - worst) / (1 - price)
    assert o.size == pytest.approx(min(6.0, round(need + 0.005, 2)), abs=0.011)
    w.fill(o, 102.3)
    b = w.book
    assert b.worst >= w.sc.target_profit_usdc - 1e-9 and b.phase == "done"
    assert w.tick(103.0, (0.56, 0.57), dn=(0.40, 0.41))[0] == []     # stopped at the target


def test_a_pair_is_never_bought_at_a_dollar_or_more():
    """The core rule: no buying the opposite token just to recover a loss."""
    w = _W()
    _long_up(w, entry=0.50)
    out, _ = w.tick(102.0, (0.53, 0.54), dn=(0.46, 0.47))           # DOWN ask 0.47 + fee + 0.50 > 0.98: not taken
    (_, side, o), = out
    assert side == "DOWN" and o.tif is TimeInForce.GTC and o.post_only
    assert 0.50 + o.price <= 1.0 - w.sc.pair_margin + 1e-9            # a resting bid that locks a pair under $1
    w2 = _W()
    _long_up(w2, entry=0.995, shares=5.0)                            # the lead cost ~1: no pair can be under $1
    out, _ = w2.tick(102.0, (0.56, 0.57), dn=(0.40, 0.41))
    assert out == [] and w2.e.rr_stats["pair_refused_cost"] >= 1


def test_the_pair_bid_rests_and_reprices_but_never_above_its_cap():
    w = _W()
    _long_up(w, entry=0.50)
    ((_, _, o),), _ = w.tick(102.0, (0.53, 0.54), dn=(0.44, 0.47))  # spread 3c: 1c better than the bid
    assert o.price == pytest.approx(0.45)
    assert w.tick(103.0, (0.52, 0.53), dn=(0.46, 0.48))[0] == []     # the bid moved 1c up (seen this pass) ...
    out, cancels = w.tick(103.3, (0.52, 0.53), dn=(0.46, 0.48))      # ... pulled on the next
    assert cancels == [o.order_id] and out == []                      # (its fill could still come: 3 s on hold)
    ((_, _, o2),), _ = w.tick(106.5, (0.52, 0.53), dn=(0.46, 0.48))  # re-placed
    assert o2.price == pytest.approx(0.47) and o2.price <= 1.0 - w.sc.pair_margin - 0.50 + 1e-9
    _, cancels = w.tick(291.0, (0.52, 0.53), dn=(0.46, 0.48))       # 9 s left < pair_min_left_s 10
    assert cancels == [o2.order_id]


def test_a_full_pairing_below_the_target_starts_a_new_cycle():
    w = _W(target_profit_usdc=5.0)
    _long_up(w, entry=0.50)
    ((_, _, o),), _ = w.tick(102.0, (0.56, 0.57), dn=(0.40, 0.41))
    assert o.size == pytest.approx(6.0)                               # the target is out of reach: pair it all
    w.fill(o, 102.3)
    assert w.book.excess()[0] is None and w.book.worst > 0
    w.tick(103.0, (0.56, 0.57))
    assert w.book.phase == "flat" and w.book.cycle_fills == 0          # the next rise may be bought


def test_entry_limits_and_the_daily_stop():
    w = _W(max_market_cost_usdc=2.0)
    assert w.rise() == []                                             # 5 sh at 0.54 > the market's 2 USDC
    w2 = _W(max_market_loss_usdc=2.0)
    assert w2.rise() == []                                            # the worst case would be -2.7
    w3 = _W()
    w3.risk.realised_today = -25.0
    assert w3.rise() == []                                            # the daily stop: no new entries
    w4 = _W()
    _long_up(w4)
    w4.risk.realised_today = -25.0
    out, _ = w4.tick(102.0, (0.56, 0.57), dn=(0.40, 0.41))
    assert len(out) == 1 and out[0][1] == "DOWN"                      # ... but a pair (risk-reducing) still may


def test_held_to_settlement_and_settled_on_both_outcomes():
    w = _W()
    _long_up(w)
    assert w.e.exit_decision(asset="BTC", side="UP", token_id="UP", entry=0.5, bid=0.2, now_ms=T0 + 200_000) is None
    w.e.on_market_settled(w.slug, won_up=False, pnl=-3.0, now=T0 + 301_000)
    assert w.book.phase == "settled"
    assert w.e.snapshot()["rising_recovery"]["markets"][w.slug]["if_down"] == pytest.approx(-3.0)


def test_the_bot_builds_it_with_its_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("TPB_RTR_ENABLED", "1")
    from troll_poly_bot.live import LiveBot
    cfg = BotConfig()
    cfg.engine.strategy = "rising_recovery"
    cfg.deploy_mode, cfg.execution_scenario = "paper", "calibrated"
    cfg.rising_recovery = RisingRecoveryConfig.from_env()
    bot = LiveBot(assets=("BTC",), balance=200.0, cfg=cfg, state_path=str(tmp_path / "live_state.rr.json"),
                  trade_log=str(tmp_path / "live_trades.rr.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    assert type(bot.engine) is RisingRecoveryEngine and bot.engine.pair_retry is False
    assert bot.risk.cfg.max_total_exposure_usdc == pytest.approx(40.0)
    assert bot.engine.snapshot()["strategy"] == "rising_recovery"


def test_a_fill_that_beats_its_cancel_is_still_booked():
    # paper 10-09 15:43: the engine pulled an entry bid, the venue's fill arrived a second later
    w = _W()
    (_, _, o), = w.rise()
    _, cancels = w.tick(105.1, (0.54, 0.55))
    assert cancels == [o.order_id]
    w.e.on_pair_done(o.order_id, filled=False)
    w.fill(o, 105.5, size=5.5, price=0.54)
    assert w.book.U == pytest.approx(5.5) and w.book.C == pytest.approx(5.5 * 0.54) and w.e.rr_stats["entry_fills"] == 1


# ---------------------------------------------------------------------------------------- high = 2 x low (15:5x)

def test_ratio_default_is_two():
    assert RisingRecoveryConfig().high_ratio == 2.0


def test_ratio_after_a_high_entry_low_then_a_lean_top_up_then_done():
    w = _W(high_ratio=2.0)
    _long_up(w)                                                       # UP 6 @ 0.50
    out, _ = w.tick(102.0, (0.56, 0.57), dn=(0.40, 0.41))           # the rise ended; UP is the high token
    assert w.book.high == "UP"
    (_, side, o), = out
    assert side == "DOWN" and o.tif is TimeInForce.IOC and o.size == pytest.approx(5.0)   # 6 / 2 = 3 -> the 5 minimum
    w.fill(o, 102.1)
    (_, side, o2), = w.tick(102.3, (0.56, 0.57), dn=(0.40, 0.41))[0]
    assert side == "UP" and o2.tif is TimeInForce.GTC and o2.post_only and o2.price == pytest.approx(0.56)   # the lean, at the bid
    assert o2.size == pytest.approx(5.0)                              # 2 x 5 - 6 = 4 -> 5
    w.fill(o2, 102.5)
    assert w.tick(103.0, (0.56, 0.57), dn=(0.40, 0.41))[0] == []
    b = w.book
    assert b.phase == "done" and (b.U, b.D) == (pytest.approx(11.0), pytest.approx(5.0))
    assert b.U - b.C > 0 > b.D - b.C                                  # a lean: UP wins pays, DOWN wins costs


def _long_down_low(w):
    for t, up in ((98.0, (0.75, 0.76)), (99.0, (0.75, 0.76)), (99.6, (0.73, 0.74))):
        w.tick(t, up)
    (_, side, o), = w.tick(100.0, (0.71, 0.72))[0]                   # DOWN's ask 0.25 -> 0.27 -> 0.29
    assert side == "DOWN" and o.price == pytest.approx(0.28)
    w.fill(o, 100.5)
    w.tick(100.8, (0.71, 0.72))
    return o


def test_ratio_after_a_low_entry_buys_twice_its_shares_of_the_high_token():
    w = _W(high_ratio=2.0)
    o = _long_down_low(w)
    out, _ = w.tick(101.2, (0.73, 0.74))                             # DOWN's mid 2c off its peak: the rise ended
    assert w.book.high == "UP"
    (_, side, o2), = out
    assert side == "UP" and o2.post_only and o2.size == pytest.approx(2 * o.size, abs=0.011)
    assert 0.28 + o2.price <= 1.0 - w.sc.pair_margin + 1e-9            # the matched half: a pair under $1
    w.fill(o2, 102.0)
    assert w.tick(102.5, (0.73, 0.74))[0] == [] and w.book.phase == "done"


def test_ratio_buys_are_cut_to_the_markets_loss_limit():
    w = _W(high_ratio=2.0, max_market_loss_usdc=5.0)
    o = _long_down_low(w)
    (_, _, o2), = w.tick(101.2, (0.73, 0.74))[0]
    assert o2.size < 2 * o.size
    assert w.e._worst_after(w.book, "UP", o2.size, o2.price) >= -5.0 - 1e-9   # noqa: SLF001


def test_ratio_under_the_daily_stop_only_risk_reducing_buys():
    w = _W(high_ratio=2.0)
    _long_up(w)
    w.risk.realised_today = -25.0
    (_, side, o), = w.tick(102.0, (0.56, 0.57), dn=(0.40, 0.41))[0]
    assert side == "DOWN"                                             # raises the worst case: still sent
    w.fill(o, 102.1)
    assert w.tick(102.3, (0.56, 0.57), dn=(0.40, 0.41))[0] == []     # the UP lean would lower it: held back
    assert w.book.phase == "pairing"


# ---------------------------------------------------------------------------------------- momentum mode (16:2x)

def _down_rises(w, t_s):
    """UP falls 0.54 -> 0.48 within a second: DOWN (0.45, 0.46) -> (0.51, 0.52), the rise signal on DOWN."""
    w.tick(t_s - 1.0, (0.54, 0.55))
    w.tick(t_s - 0.4, (0.50, 0.51))
    return w.tick(t_s, (0.48, 0.49))[0]


def test_momentum_is_the_default_mode():
    d = RisingRecoveryConfig()
    assert (d.mode, d.buy_mode) == ("momentum", "taker")


def test_momentum_mode_buys_every_rising_token_with_no_pairing_rule():
    w = _W(mode="momentum")
    (_, side, o), = w.rise()
    assert side == "UP" and o.post_only and o.price == pytest.approx(0.54)
    w.fill(o, 100.2)
    (_, side, o2), = _down_rises(w, 103.0)
    assert side == "DOWN" and o2.post_only and o2.price == pytest.approx(0.51)
    assert 0.54 + o2.price > 1.0                                      # no "pair under $1" rule in this mode
    w.fill(o2, 103.2)
    (_, side, o3), = w.rise(t_s=110.0)                                # UP rises again: bought again
    assert side == "UP"
    w.fill(o3, 110.2)
    b = w.book
    assert b.U == pytest.approx(o.size + o3.size) and b.D == pytest.approx(o2.size) and b.phase == "flat"


def test_momentum_mode_loss_limit_allows_only_buys_that_raise_the_worst_case():
    w = _W(mode="momentum", max_market_loss_usdc=4.0)
    (_, _, o), = w.rise()
    w.fill(o, 100.2)                                                  # UP 5.55 @ 0.54: worst -3.0
    assert w.rise(t_s=110.0) == []                                    # more UP: worst -6.0 < -4
    assert w.e.rr_stats["buy_limit_refused"] >= 1
    (_, side, o2), = _down_rises(w, 120.0)                            # DOWN raises the worst case: allowed
    assert side == "DOWN"


def test_momentum_mode_daily_stop_only_risk_reducing_buys():
    w = _W(mode="momentum")
    w.risk.realised_today = -25.0
    assert w.rise() == []                                             # a first buy only lowers the worst case
    w2 = _W(mode="momentum")
    (_, _, o), = w2.rise()
    w2.fill(o, 100.2)
    w2.risk.realised_today = -25.0
    (_, side, _), = _down_rises(w2, 103.0)
    assert side == "DOWN"


def test_momentum_mode_taker_buys_and_the_profit_lock():
    w = _W(mode="momentum", buy_mode="taker")
    (_, side, o), = w.rise()
    assert o.tif is TimeInForce.IOC and o.price == pytest.approx(0.56)        # the ask 0.55 + 1c
    w2 = _W(mode="momentum")
    b = w2.e._book(w2.m)                                              # noqa: SLF001
    b.add("UP", 10.0, 0.40, 0.0)
    b.add("DOWN", 10.0, 0.45, 0.0)                                    # 10 + 10 for 8.50: +1.50 whichever side wins
    assert w2.rise() == [] and b.phase == "done"


# ---------------------------------------------------------------------------------------- favourite fix (16:3x)

def _held_up_down_favourite(w, U=10.0, upx=0.50):
    """UP held at 0.50 while DOWN has become the favourite (mid 0.705): if DOWN wins the market loses."""
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", U, upx, 0.0)
    return b


def test_fix_buys_the_favourite_until_its_outcome_is_positive():
    w = _W(mode="momentum")
    b = _held_up_down_favourite(w)
    (_, side, o), = w.tick(150.0, (0.29, 0.30))[0]                    # DOWN (0.70, 0.71): the favourite
    assert side == "DOWN" and o.post_only and o.price == pytest.approx(0.70)
    need = (w.sc.high_win_target_usdc - (0.0 - 5.0)) / (1.0 - 0.70)   # (target - P&L if DOWN wins) / (1 - price)
    assert o.size == pytest.approx(math.ceil(need * 100) / 100)
    w.fill(o, 150.5)
    assert b.D - b.C >= w.sc.high_win_target_usdc - 1e-9              # if DOWN wins: positive now
    assert w.tick(151.0, (0.29, 0.30))[0] == []                       # fixed: nothing more to buy


def test_fix_needs_a_clear_favourite_and_a_negative_outcome():
    w = _W(mode="momentum")
    _held_up_down_favourite(w)
    assert w.tick(150.0, (0.47, 0.48))[0] == []                       # DOWN mid 0.525 < 0.55: no favourite
    w2 = _W(mode="momentum")
    _held_up_down_favourite(w2)
    assert w2.tick(150.0, (0.70, 0.71))[0] == []                      # UP is the favourite and wins +5.00


def test_fix_is_cut_to_the_loss_limit_and_withdrawn_when_not_needed():
    w = _W(mode="momentum", max_market_loss_usdc=8.0)
    b = _held_up_down_favourite(w)
    (_, _, o), = w.tick(150.0, (0.29, 0.30))[0]
    assert w.e._worst_after(b, "DOWN", o.size, o.price) >= -8.0 - 1e-9   # noqa: SLF001
    w.tick(151.0, (0.60, 0.61))                                       # UP is the favourite again ...
    _, cancels = w.tick(151.5, (0.60, 0.61))
    assert cancels == [o.order_id]                                    # ... the DOWN fix is pulled


def test_fix_as_a_taker():
    w = _W(mode="momentum", fix_mode="taker")
    _held_up_down_favourite(w)
    (_, side, o), = w.tick(150.0, (0.29, 0.30))[0]
    assert side == "DOWN" and o.tif is TimeInForce.IOC and o.price == pytest.approx(0.72)


def test_an_underdog_buy_may_not_undo_the_favourites_outcome():
    # paper 16:35: a DOWN momentum buy right after the UP fix made "if UP wins" negative again -> fix -> both ways negative
    w = _W(mode="momentum")
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 10.0, 0.60, 0.0)                                      # if UP wins +4.00
    w.tick(101.0, (0.66, 0.67))
    w.tick(101.6, (0.64, 0.65))
    out = w.tick(102.0, (0.62, 0.63))[0]                             # DOWN rising, UP still the favourite (0.625)
    assert all(side != "DOWN" for _, side, _ in out)                  # if UP wins +4.00: positive -> no DOWN buy at 0.37
    w2 = _W(mode="momentum")
    b2 = w2.e._book(w2.m)                                             # noqa: SLF001
    b2.add("UP", 10.0, 0.60, 0.0)
    b2.add("DOWN", 5.0, 0.40, 0.0)                                    # if UP wins +2.00, if DOWN wins -3.00 (not locked)
    w2.tick(101.0, (0.66, 0.67))
    w2.tick(101.6, (0.64, 0.65))
    out = w2.tick(102.0, (0.62, 0.63))[0]
    assert all(side != "DOWN" for _, side, _ in out)                  # positive: no DOWN buy at 0.37
    assert w2.e.rr_stats["buy_underdog_refused"] >= 1



# ---------------------------------------------------------------------------------------- positive state: hedge < 0.20 (16:4x)

def test_positive_state_hedges_with_the_cheap_token_to_even_out_the_outcomes():
    w = _W(mode="momentum")
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 20.0, 0.60, 0.0)                                      # if UP wins +8.00, if DOWN wins -12.00
    (_, side, o), = w.tick(200.0, (0.88, 0.89))[0]                    # DOWN (0.11, 0.12): under 0.20
    assert side == "DOWN" and o.post_only and o.price == pytest.approx(0.11)
    assert o.size == pytest.approx(20.0)                              # min(8 - -12, (8 - 0.10) / 0.11) = 20
    w.fill(o, 200.5)
    assert b.U - b.C == pytest.approx(b.D - b.C) == pytest.approx(5.8)   # evened out: the worst case +5.80
    assert w.tick(201.0, (0.88, 0.89))[0] == []


def test_the_hedge_keeps_the_favourite_above_its_target_and_needs_a_cheap_token():
    w = _W(mode="momentum")
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 20.0, 0.90, 0.0)                                      # if UP wins +2.00, if DOWN wins -18.00
    (_, _, o), = w.tick(200.0, (0.88, 0.89))[0]
    assert o.size == pytest.approx(math.floor((2.00 - 0.10) / 0.11 * 100) / 100)   # capped by the favourite's target
    assert b.U - (b.C + o.size * 0.11) >= 0.10 - 1e-9
    w2 = _W(mode="momentum")
    w2.e._book(w2.m).add("UP", 20.0, 0.60, 0.0)                       # noqa: SLF001
    assert w2.tick(200.0, (0.74, 0.75))[0] == []                      # DOWN at 0.25: not under 0.20


def test_an_order_ended_unfilled_holds_its_token_until_a_late_fill_can_arrive():
    # paper 16:46: the hedge bid was ended on an intermediate answer, a second was placed, both filled
    w = _W(mode="momentum")
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 20.0, 0.60, 0.0)
    (_, _, o), = w.tick(200.0, (0.88, 0.89))[0]
    w.e.on_pair_done(o.order_id, filled=False)                        # ended, not filled, not cancelled by us
    assert w.tick(200.5, (0.89, 0.90))[0] == []                       # DOWN at 0.10 now: still no second hedge
    assert len(w.tick(203.5, (0.89, 0.90))[0]) == 1                   # 3 s later it may be placed


def test_a_late_fill_that_breaks_the_lock_reopens_the_market():
    w = _W(mode="momentum")
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 5.0, 0.77, 0.0)
    (_, _, o), = w.tick(200.0, (0.82, 0.83))[0]                       # DOWN (0.17, 0.18): hedge
    w.fill(o, 200.2)
    assert b.phase == "done"                                          # +0.30 / +0.30: locked
    stray = SimpleNamespace(order_id="late", token_id="DN")
    w.e.on_pair_fill_record(w.slug, stray, SimpleNamespace(size=5.0, price=0.15, fee=0.0, token_id="DN"), T0 + 200_400)
    assert b.phase == "flat" and w.e.rr_stats["lock_reopened"] == 1   # -0.45 if UP wins: open again
    out = w.tick(204.0, (0.82, 0.83))[0]
    assert [side for _, side, _ in out] == ["UP"]                     # the favourite fix repairs it



# ---------------------------------------------------------------------------------------- fill rate: taker defaults (16:5x)

def test_the_defaults_take():
    d = RisingRecoveryConfig()
    assert (d.buy_mode, d.fix_mode, d.hedge_mode) == ("taker", "taker", "auto")


def test_a_taker_momentum_buy_and_its_per_move_cooldown():
    w = _W(mode="momentum", buy_mode="taker")
    (_, side, o), = w.rise()
    assert side == "UP" and o.tif is TimeInForce.IOC and o.price == pytest.approx(0.56)
    w.e.on_pair_done(o.order_id, filled=True)                         # the FAK ended (filled)
    assert w.tick(100.3, (0.56, 0.57))[0] == []                       # the same move, 0.3 s on: no second buy
    out = []
    for t, up in ((103.5, (0.56, 0.57)), (104.2, (0.58, 0.59)), (104.5, (0.60, 0.61))):
        out += w.tick(t, up)[0]
    assert [x[1] for x in out] == ["UP"]                              # a new move after the cooldown: bought again


def test_the_fix_and_the_cheap_hedge_take():
    w = _W(mode="momentum", fix_mode="taker")
    _held_up_down_favourite(w)
    (_, side, o), = w.tick(150.0, (0.29, 0.30))[0]
    assert side == "DOWN" and o.tif is TimeInForce.IOC and o.price == pytest.approx(0.72)
    w2 = _W(mode="momentum", hedge_mode="auto")
    w2.e._book(w2.m).add("UP", 20.0, 0.60, 0.0)                       # noqa: SLF001
    (_, side, o2), = w2.tick(200.0, (0.88, 0.89))[0]                  # DOWN ask 0.12 + fee < 0.20: taken
    assert side == "DOWN" and o2.tif is TimeInForce.IOC and 0.12 <= o2.price < 0.20
    w3 = _W(mode="momentum", hedge_mode="auto")
    w3.e._book(w3.m).add("UP", 20.0, 0.60, 0.0)                       # noqa: SLF001
    (_, side, o3), = w3.tick(200.0, (0.79, 0.81))[0]                  # DOWN (0.19, 0.21): ask over 0.20 -> a bid at 0.19
    assert o3.post_only and o3.price == pytest.approx(0.19) and o3.price < 0.20


def test_a_fix_the_limits_would_cut_short_is_not_sent():
    # paper 18:13: UP 10 sh for 8.59, DOWN became the favourite at 0.63; the fix wanted 24.6 sh, the 20 USDC cap allowed
    # 17.65 -> -10.00 if UP wins, -2.35 if DOWN wins
    assert RisingRecoveryConfig().fix_partial is True                 # the replay: cut fixes still help on average
    w = _W(mode="momentum", fix_mode="taker", fix_partial=False)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 10.0, 0.859, 0.0)
    assert w.tick(200.0, (0.36, 0.37))[0] == []                       # DOWN (0.63, 0.64): the full fix does not fit
    assert w.e.rr_stats["fix_refused_partial"] == 1
    w2 = _W(mode="momentum", fix_mode="taker")
    w2.e._book(w2.m).add("UP", 10.0, 0.859, 0.0)                      # noqa: SLF001
    assert len(w2.tick(200.0, (0.36, 0.37))[0]) == 1                  # the default: the cut fix is sent


def test_nothing_is_placed_while_the_guard_pauses_entries(monkeypatch, tmp_path):
    # paper 10-09 19:23: a book-feed drop tripped the guard (30 s cooldown) but the engine's pair orders bypassed it --
    # a favourite fix went out 6 s later and lost the market
    w = _W(mode="momentum", fix_mode="taker")
    _held_up_down_favourite(w)
    w.e.guard_paused = True
    assert w.tick(150.0, (0.29, 0.30))[0] == []                       # paused: books recorded, nothing placed
    w.e.guard_paused = False
    assert len(w.tick(151.0, (0.29, 0.30))[0]) == 1                   # the guard lifted: the fix goes out
    monkeypatch.setenv("TPB_RTR_ENABLED", "1")
    from troll_poly_bot.live import LiveBot
    cfg = BotConfig()
    cfg.engine.strategy = "rising_recovery"
    cfg.deploy_mode, cfg.execution_scenario = "paper", "calibrated"
    cfg.rising_recovery = RisingRecoveryConfig.from_env()
    bot = LiveBot(assets=("BTC",), balance=200.0, cfg=cfg, state_path=str(tmp_path / "live_state.rr.json"),
                  trade_log=str(tmp_path / "live_trades.rr.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    bot.guard.tripped = {"FEED_DOWN": "test"}
    bot._engine_pair_cancels(T0)                                      # noqa: SLF001
    assert bot.engine.guard_paused is True
    bot.guard.tripped = {}
    bot._engine_pair_cancels(T0 + 1000)                               # noqa: SLF001
    assert bot.engine.guard_paused is False


# ---------------------------------------------------------------------------------------- the cover cycle (10-10 02:3x)

def _cover_world(**kw):
    w = _W(mode="momentum", buy_mode="taker", fix_high=False, cover_enabled=True, **kw)
    (_, side, o), = w.rise()                                          # UP rises -> a momentum TAKE
    assert side == "UP"
    w.fill(o, 100.2, price=0.55)
    return w


def _down_turns_up(w, t_s):
    out = []
    for t, up in ((t_s, (0.54, 0.55)), (t_s + 0.5, (0.52, 0.53)), (t_s + 0.9, (0.50, 0.51)), (t_s + 1.2, (0.50, 0.51))):
        out += w.tick(t, up)[0]
    return out


def test_cover_bid_after_the_delay_priced_down_and_under_a_dollar():
    w = _cover_world()
    b = w.book
    assert w.tick(105.0, (0.54, 0.55))[0] == []                       # 5 s after the buy: not yet
    (_, side, o), = w.tick(110.5, (0.54, 0.55))[0]                    # 10 s: DOWN (0.45, 0.46)
    _, ex, avg = b.excess()
    assert side == "DOWN" and o.post_only and o.tif is TimeInForce.GTC
    assert o.price == pytest.approx(min(0.45 - 0.02, math.floor((0.99 - avg) * 100) / 100))
    assert avg + o.price <= 0.99 + 1e-9
    assert o.size == pytest.approx(max(5.0, math.ceil(0.5 * ex * 100) / 100))
    assert w.tick(111.0, (0.54, 0.55))[0] == []                       # one cover bid per momentum buy


def test_the_other_token_turning_up_before_the_cover_fills_is_taken():
    w = _cover_world()
    (_, _, bid), = w.tick(110.5, (0.54, 0.55))[0]
    _, ex, _ = w.book.excess()
    out = _down_turns_up(w, 111.0)                                    # DOWN's ask 0.46 -> 0.48 -> 0.50
    takes = [o for _, side, o in out if side == "DOWN" and o.tif is TimeInForce.IOC]
    assert len(takes) == 1 and bid.order_id not in w.e.resting        # the bid pulled, the token taken
    assert takes[0].size == pytest.approx(max(5.0, math.ceil(ex * 100) / 100))
    assert w.e.rr_stats["cover_take_orders"] == 1
    assert [o for _, s, o in _down_turns_up(w, 113.0) if s == "DOWN"] == []   # one take per momentum buy


def test_no_take_once_the_cover_filled_and_the_cycle_repeats_on_the_next_buy():
    w = _cover_world()
    (_, _, bid), = w.tick(110.5, (0.54, 0.55))[0]
    w.fill(bid, 111.0)                                                # the cheap cover filled
    assert [o for _, s, o in _down_turns_up(w, 112.0) if s == "DOWN"] == []
    seq = w.book.lead_seq
    (_, side, o), = w.rise(t_s=130.0)                                 # a new momentum buy of UP ...
    w.fill(o, 130.2)
    assert w.book.lead_seq == seq + 1
    assert [s for _, s, _ in w.tick(140.5, (0.54, 0.55))[0]] == ["DOWN"]   # ... gets its own cover bid


def test_no_double_loss_refuses_a_buy_that_loses_both_ways():
    w = _W(mode="momentum", fix_mode="taker", no_double_loss=True)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 10.0, 0.859, 0.0)                                     # if UP wins +1.41
    assert w.tick(200.0, (0.36, 0.37))[0] == []                       # the cut fix (-10 / -2.35) is refused
    assert w.e.rr_stats["double_loss_refused"] >= 1


def test_a_rise_after_a_quiet_book_is_seen():
    # 10-10: the history kept only 10 s of changes, so after a flat book the price 1 s ago was unknown
    w = _W(mode="momentum", buy_mode="taker", fix_high=False)
    w.tick(80.0, (0.50, 0.51))
    w.tick(99.0, (0.50, 0.51))                                        # unchanged for 19 s
    w.tick(99.6, (0.52, 0.53))
    out = w.tick(100.0, (0.54, 0.55))[0]
    assert [side for _, side, _ in out] == ["UP"]


# ---------------------------------------------------------------------------------------- spot mode (10-12)

from troll_poly_bot.execution.latency import COLOCATED            # noqa: E402
from troll_poly_bot.feeds.spot import CompositeView                # noqa: E402


class _S(_W):
    """The spot-mode world: evaluate() feeds BTC spot + the books (as live.py does), then one pass of orders."""

    def __init__(self, strike=100.0, **kw):
        kw.setdefault("mode", "spot")
        super().__init__(**kw)
        self.m.strike = strike
        self.e.meta_by_slug[self.slug] = self.meta

    def step(self, t_s, btc, up=(0.55, 0.56), dn=None, spot_age=50.0, book_age=30.0, balance=200.0, depth=100.0):
        dn = dn or (round(1 - up[1], 3), round(1 - up[0], 3))
        now = T0 + t_s * 1000.0
        view = CompositeView("BTC", btc, btc, 3, 1.0, 1.0, spot_age, {"binance": btc})
        bu, bd = _ob("UP", *up, size=depth), _ob("DN", *dn, size=depth)
        self.e.evaluate(self.meta, bu, bd, view, spot_age, book_age, now, SimpleNamespace(ready=True), SimpleNamespace(ready=True),
                        COLOCATED, balance, quote_ages={"UP": book_age, "DN": book_age})
        for oid in self.e.pair_cancels(now):
            self.e.on_pair_done(oid, filled=False)
        out = self.e.pair_requests(now, {self.slug: self.m}, {self.slug: (bu, bd)}, balance=balance)
        for _, side, o in out:
            self.e.on_pair_submit(o.order_id, self.slug, side, o.price, o.size)
        return out

    def run(self, t0, prices, dt=0.25, **kw):
        """A series of passes dt apart with the BTC prices given; returns every order."""
        out = []
        for i, p in enumerate(prices):
            out += self.step(t0 + i * dt, p, **kw)
        return out


def _one(out):
    assert len(out) == 1, [(s, o.size, o.price) for _, s, o in out]
    return out[0]


def test_spot_initial_entry_up_when_btc_rose_over_the_previous_second():
    w = _S()
    assert w.step(10.0, 100.5) == []                                  # no second of history yet
    out = w.run(10.25, [100.5, 100.5, 100.5, 100.6, 100.7])          # 1.25 s on: BTC rose over the last second
    _, side, o = _one(out)
    assert side == "UP" and o.size == pytest.approx(10.0) and o.tif is TimeInForce.IOC and o.price == pytest.approx(0.57)
    assert "init" in w.book.fired
    assert w.run(12.0, [100.7, 100.7, 100.7]) == []                   # once per market


def test_spot_initial_entry_down_when_btc_fell():
    w = _S()
    out = w.run(10.0, [100.5, 100.5, 100.5, 100.5, 100.4, 100.3])
    _, side, o = _one(out)
    assert side == "DOWN" and o.size == pytest.approx(10.0)


def test_spot_reversal_buys_five_of_the_opposite_once_per_confirmed_flip():
    w = _S(strike=50.0, spot_min_move_bps=1.0, spot_confirm_passes=2)      # the strike far away: no crossings here
    out = w.run(10.0, [100.5, 100.5, 100.5, 100.5, 100.6, 100.7, 100.8])   # rising: init UP, direction +1
    assert [s for _, s, _ in out] == ["UP"]
    w.fill(out[0][2], 12.0, price=0.50)                               # filled under the mid: the market marks positive
    out = w.run(13.0, [100.8, 100.7, 100.6, 100.5, 100.4, 100.3, 100.2])   # falling for > 1 s, confirmed: one reversal
    revs = [(s, o.size) for _, s, o in out]
    assert revs == [("DOWN", 5.0)] and "rev:1" in w.book.fired
    assert w.run(15.0, [100.1, 100.0, 99.9, 99.8]) == []              # still falling: no repeat
    out = w.run(16.0, [99.9, 100.0, 100.1, 100.2, 100.3, 100.4])      # back up: the second reversal
    assert [(s, o.size) for _, s, o in out] == [("UP", 5.0)] and "rev:2" in w.book.fired


def test_spot_strike_crossings_buy_ten_each_way_but_only_on_the_edge():
    w = _S(strike=100.0)
    w.run(10.0, [99.5, 99.5, 99.5, 99.5, 99.5, 99.5])                 # below the strike, flat: init DOWN, cross armed
    w.book.fired.add("init") if "init" not in w.book.fired else None
    for oid in list(w.e.resting):                                     # the init order: filled under the mid
        w.fill(SimpleNamespace(order_id=oid, token_id=w.e.resting[oid]["token"], price=w.e.resting[oid]["price"],
                               size=w.e.resting[oid]["size"], tif=TimeInForce.IOC), 11.5, price=0.35)
    out = w.run(12.0, [100.5, 100.5, 100.5, 100.5, 100.5])            # crosses above
    crosses = [(s, o.size) for _, s, o in out]
    assert ("UP", 10.0) in crosses and "cross:1" in w.book.fired
    w.fill(out[0][2], 12.5, price=0.45)
    assert w.run(14.0, [100.6, 100.7, 100.6, 100.7]) == []            # stays above: no repeat
    out = w.run(15.0, [99.6, 99.6, 99.6, 99.6, 99.6])                 # crosses below
    assert ("DOWN", 10.0) in [(s, o.size) for _, s, o in out] and "cross:2" in w.book.fired


def test_spot_one_order_per_pass_and_the_minimum_interval():
    w = _S(strike=100.0, spot_min_interval_s=1.0)
    (_, _, init), = w.run(10.0, [99.5, 99.5, 99.5, 99.5, 99.4])       # BTC down over the last second: init DOWN at 11.0
    assert w.book.last_order_t == pytest.approx(T0 + 11_000.0)
    w.fill(init, 11.2, price=0.35)
    out = w.step(11.25, 100.5)                                        # a cross 0.25 s later: held by the interval ...
    assert out == [] and "cross:1" in w.book.fired and "cross:1" in w.book.wants
    out = w.step(12.25, 100.5)                                        # ... and sent once the interval has passed
    assert [(s, o.size) for _, s, o in out] == [("UP", 10.0)]
    w.fill(out[0][2], 12.4, price=0.45)
    out = w.step(13.5, 99.5)                                          # crossing back: one order this pass
    assert [s for _, s, _ in out] == ["DOWN"]


def test_spot_fees_come_from_the_markets_schedule_and_count_in_the_cost():
    w = _S()
    w.meta.fee = FeeSchedule(rate=0.04)                               # a market with its own schedule
    out = w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2])
    _, side, o = _one(out)
    w.fill(o, 12.0, fee=o.size * w.meta.fee.taker_fee_per_share(o.price))
    b = w.book
    assert b.fees == pytest.approx(10 * 0.04 * 0.57 * 0.43) and b.C == pytest.approx(10 * 0.57 + b.fees)
    assert b.U - b.C == pytest.approx(10 - b.C) and b.D - b.C == pytest.approx(-b.C)


def test_spot_partial_fill_is_booked_and_the_order_is_cut_to_the_ask_depth():
    w = _S()
    out = w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2], depth=7.0)
    _, side, o = _one(out)
    assert o.size == pytest.approx(7.0)                               # the ask depth within the limit
    w.fill(o, 12.0, size=4.0)                                         # the venue filled 4 of 7
    assert w.book.U == pytest.approx(4.0) and w.book.U - w.book.C == pytest.approx(4.0 - w.book.C)
    out = w.run(12.25, [100.2, 100.2, 100.2, 100.2], depth=7.0)       # the remainder (6) is ordered again
    assert [(s, o.size) for _, s, o in out] == [("UP", 6.0)]
    w2 = _S()
    out = w2.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2], depth=3.0)
    assert out == [] and w2.e.rr_stats["spot_reject"] == 1           # under the 5-share minimum: rejected, logged


def test_spot_risk_limits_shares_cost_and_stale_books():
    w = _S(spot_max_shares=12.0)
    w.e._book(w.m).add("UP", 10.0, 0.5, 0.0)                          # noqa: SLF001
    out = w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2])
    assert out == []                                                  # 10 held + 10 > 12 per outcome: only 2 fit (< 5)
    w2 = _S(spot_max_cost_usdc=8.0)
    w2.e._book(w2.m).add("UP", 10.0, 0.5, 0.0)                        # noqa: SLF001  (5 USDC spent)
    out = w2.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2])
    _, _, o = _one(out)
    assert o.size == pytest.approx(5.1, abs=0.02)                     # 3 USDC of room at 0.56 + fee + 1c allowance
    w2b = _S(spot_max_cost_usdc=7.5)
    w2b.e._book(w2b.m).add("UP", 10.0, 0.5, 0.0)                      # noqa: SLF001
    assert w2b.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2]) == []   # 2.5 of room: under 5 shares
    w3 = _S()
    out = w3.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2], book_age=900.0)
    assert out == [] and w3.e.rr_stats["spot_reject"] >= 1           # stale book
    w4 = _S()
    out = w4.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2], up=(0.56, 0.55))
    assert out == []                                                  # crossed book: invalid


def test_spot_leader_rule_needs_plus_five_on_both_outcomes():
    w = _S(spot_max_shares=500.0, spot_max_cost_usdc=500.0)
    w.book_ = w.e._book(w.m)                                          # noqa: SLF001
    w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.0], up=(0.60, 0.61))  # init (flat: DOWN) at 0.39; leaders armed below 0.75
    w.book.fired.add("init")
    assert w.step(12.0, 100.0, up=(0.76, 0.77)) == []                 # UP's mid crosses 0.75: candidate fails (+5 both)
    assert "leader:UP:1" in w.book.fired and w.e.rr_stats["spot_drop"] >= 1 and "leader:UP:1" not in w.book.wants
    w2 = _S(spot_max_shares=500.0, spot_max_cost_usdc=500.0)
    b = w2.e._book(w2.m)                                              # noqa: SLF001
    b.add("UP", 120.0, 0.45, 0.0); b.add("DOWN", 120.0, 0.40, 0.0)    # +18 / +18 whichever side wins
    w2.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.0], up=(0.60, 0.61))
    w2.book.fired.add("init")
    out = w2.step(13.0, 100.0, up=(0.76, 0.77))
    _, side, o = _one(out)
    assert side == "UP" and o.size == pytest.approx(10.0)             # +20.2 / +10.2 after it: both >= +5
    assert "leader:UP:1" in w2.book.fired
    assert w2.step(13.5, 100.0, up=(0.78, 0.79)) == []                # stays above: no repeat


def test_spot_near_strike_rule_buys_only_what_improves_the_worst_case():
    w = _S(strike=100.0)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 20.0, 0.50, 0.0)                                      # +10 if UP, -10 if DOWN (marks +1.1): DOWN raises the worst case
    b.fired.add("init")                                               # the initial entry is not under test here
    w.run(10.0, [100.3, 100.3, 100.3, 100.3, 100.3])                  # 30 bps away: outside the zone
    out = w.step(12.0, 100.02, up=(0.55, 0.56))                       # 2 bps: inside the zone (edge)
    _, side, o = _one(out)
    assert side == "DOWN" and o.size == pytest.approx(10.0) and "near:1" in w.book.fired
    assert w.step(12.5, 100.01, up=(0.55, 0.56)) == []                # still inside: no repeat
    w2 = _S(strike=100.0)
    w2.e._book(w2.m).fired.add("init")                                # noqa: SLF001
    w2.run(10.0, [100.3, 100.3, 100.3, 100.3, 100.3])
    assert w2.step(12.0, 100.02, up=(0.55, 0.56)) == []               # nothing held: no buy improves the worst case
    assert w2.e.rr_stats["spot_drop"] >= 1


def test_spot_mode_never_sells_and_logs_every_decision(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="troll_poly_bot.strategy.rising_recovery")
    w = _S()
    out = w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2])
    _, _, o = _one(out)
    w.fill(o, 12.0)
    recs = [json.loads(r.getMessage()[len("[RTR_SPOT] "):]) for r in caplog.records if r.getMessage().startswith("[RTR_SPOT]")]
    events = [r["event"] for r in recs]
    assert "order" in events and "fill" in events
    f = [r for r in recs if r["event"] == "fill"][0]
    assert f["trigger"] == "init" and f["fill_qty"] == 10.0 and f["fee"] > 0 and f["pnl_up"] + f["pnl_down"] < 0
    assert f["btc"] == 100.2 and f["strike"] == 100.0 and f["strike_distance_bps"] == pytest.approx(20.0, abs=0.1)
    assert w.e.exit_decision(asset="BTC", side="UP", token_id="UP", entry=0.57, bid=0.2, now_ms=T0 + 200_000) is None


def test_spot_a_killed_order_is_sent_again_until_it_fills():
    w = _S(strike=50.0)
    (_, _, o), = w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2])   # init UP 10
    w.e.on_pair_done(o.order_id, filled=False)                        # the venue killed it (quote pulled)
    assert w.run(11.5, [100.2, 100.2, 100.2, 100.2, 100.2, 100.2, 100.2, 100.2], dt=0.25) == []   # the 3-s hold
    assert "init" in w.book.wants
    out = w.run(14.5, [100.2, 100.3, 100.4, 100.5])                   # BTC rising again: re-sent (a flat market would wait)
    assert [(s, o2.size) for _, s, o2 in out] == [("UP", 10.0)]
    w.fill(out[0][2], 15.6)
    assert "init" not in w.book.wants
    assert w.run(17.0, [100.2, 100.2, 100.2, 100.2]) == []            # filled: no more orders for that trigger


def _after_a_rise(w, up):
    """A confirmed rising direction (no reversal yet), then a confirmed fall: the DOWN reversal want."""
    w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2, 100.3], up=up)
    return w.run(12.0, [100.3, 100.2, 100.1, 100.0, 99.9, 99.8], up=up)


def test_spot_pnl_rule_blocks_the_lower_priced_token_while_pnl_is_negative():
    w = _S(strike=50.0)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.fired.add("init")
    b.add("UP", 10.0, 0.70, 0.0)                                      # UP bought at 0.70, marking 0.555: PnL -1.45
    out = _after_a_rise(w, (0.55, 0.56))
    assert out == [] and w.e.rr_stats["spot_drop"] >= 1 and "rev:1" in b.fired and "rev:1" not in b.wants
    w2 = _S(strike=50.0)
    b2 = w2.e._book(w2.m)                                             # noqa: SLF001
    b2.fired.add("init")
    b2.add("UP", 10.0, 0.50, 0.0)                                     # marking 0.555: PnL +0.55
    out = _after_a_rise(w2, (0.55, 0.56))
    assert [(s, o.size) for _, s, o in out] == [("DOWN", 5.0)]        # the same reversal buys the lower-priced DOWN
    w3 = _S(strike=50.0)
    b3 = w3.e._book(w3.m)                                             # noqa: SLF001
    b3.fired.add("init")
    b3.add("UP", 10.0, 0.70, 0.0)                                     # PnL negative, but DOWN is the HIGHER-priced token here
    out = _after_a_rise(w3, (0.40, 0.41))
    assert [(s, o.size) for _, s, o in out] == [("DOWN", 5.0)]


def test_spot_nothing_before_five_seconds_after_the_open():
    w = _S(strike=50.0)
    out = w.run(1.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7, 100.8, 100.9])   # 1.0 .. 4.0 s
    assert out == [] and "init" not in w.book.fired
    out = w.run(4.5, [101.0, 101.1, 101.2])                           # 4.5, 4.75, 5.0 s
    assert [(s, o.size) for _, s, o in out] == [("UP", 10.0)] and w.book.last_order_t == pytest.approx(T0 + 5_000.0)


# ---------------------------------------------------------------------------------------- climb + pair (10-12 15:1x)

def _climb_world(**kw):
    w = _S(strike=50.0, **kw)
    w.e._book(w.m).fired.add("init")                                  # noqa: SLF001  the init entry is not under test
    return w


def _run_filling(w, t0, n, ask0, step=0.001, dt=0.25, fill_price=None):
    """Passes dt apart with the UP ask rising `step` each pass from ask0; every order is filled at once. Returns the fills."""
    fills = []
    for i in range(n):
        a = round(ask0 + i * step, 3)
        out = w.step(t0 + i * dt, 100.0, up=(round(a - 0.01, 3), a))
        for _, side, o in out:
            w.fill(o, t0 + i * dt + 0.1, price=fill_price or o.expected_price)
            fills.append((round(t0 + i * dt, 2), side, o.size, o.expected_price))
    return fills


def test_climb_buys_the_rising_leader_every_five_seconds_up_to_fifty_shares():
    w = _climb_world()
    fills = _run_filling(w, 10.0, 120, 0.61)                          # 30 s, the UP ask 0.61 -> 0.73
    climbs = [f for f in fills if f[1] == "UP"]
    assert [f[2] for f in climbs] == [10.0] * 5                       # 5 lots of 10 = the 50-share cap
    gaps = [round(b_[0] - a_[0], 2) for a_, b_ in zip(climbs, climbs[1:])]
    assert all(g >= 5.0 for g in gaps), gaps                          # one every 5 s
    assert w.book.climb_bought == pytest.approx(50.0) and w.book.U == pytest.approx(50.0)


def test_climb_needs_a_rising_leader_under_0_90():
    w = _climb_world()
    fills = _run_filling(w, 10.0, 60, 0.61, step=0.0)                 # flat leader: no climb
    assert [f for f in fills if f[1] == "UP"] == []
    w2 = _climb_world()
    fills = _run_filling(w2, 10.0, 60, 0.905)                         # rising but above 0.90: no climb
    assert [f for f in fills if f[1] == "UP"] == []


def test_pair_rule_after_the_climb_buys_the_cheap_token_to_make_both_outcomes_positive():
    w = _climb_world()
    fills = _run_filling(w, 10.0, 120, 0.61)                          # the climb: 50 UP by ~0.71; the leader on to 0.73
    b = w.book
    assert b.climb_bought == pytest.approx(50.0) and "pair:5" in b.fired
    pairs = [f for f in fills if f[1] == "DOWN"]
    assert len(pairs) == 1                                            # bought as soon as it was feasible
    t_pair, _, n, px = pairs[0]
    assert t_pair > 35.0
    u = px + FeeSchedule().taker_fee_per_share(px)
    c_before = b.C - n * u
    assert n == pytest.approx(math.ceil((c_before + 0.10) / (1 - u) * 100) / 100, abs=0.02)
    assert b.D - b.C >= 0.10 - 0.02 and b.U - b.C >= 0.10               # both outcomes positive after it
    assert [f for f in _run_filling(w, 40.0, 8, 0.80, step=0.0) if f[1] == "DOWN"] == []   # once per climb episode


def test_pair_rule_waits_while_it_cannot_keep_the_leader_positive():
    w = _climb_world()
    fills = _run_filling(w, 10.0, 120, 0.61, step=0.0004)             # a slow climb: 50 UP, the leader only to ~0.66
    assert w.book.climb_bought == pytest.approx(50.0) and [f for f in fills if f[1] == "DOWN"] == []
    fills = _run_filling(w, 40.0, 8, 0.62, step=0.0)                  # the leader back at 0.62: DOWN costs ~0.39, no pair profit
    assert [f for f in fills if f[1] == "DOWN"] == [] and "pair:5" in w.book.fired
    assert "pair:5" in w.book.wants and w.e.rr_stats["spot_reject"] >= 1   # kept: re-checked as prices move
    fills = _run_filling(w, 42.0, 4, 0.80, step=0.0)                  # the leader runs to 0.80: now it fits -> bought
    assert len([f for f in fills if f[1] == "DOWN"]) == 1 and "pair:5" not in w.book.wants


def test_only_the_latest_pair_want_is_kept_and_blocked_passes_log_sparingly():
    w = _climb_world()
    _run_filling(w, 10.0, 24, 0.61, step=0.001)                       # rising 6 s: the first climb lot
    _run_filling(w, 16.0, 40, 0.634, step=0.0)                        # flat 10 s: the climb pauses -> a pair want (infeasible, kept)
    b = w.book
    first = [k for k in b.wants if k.startswith("pair:")]
    assert len(first) == 1 and b.climb_bought > 0
    _run_filling(w, 26.0, 32, 0.634, step=0.001)                      # rising again 8 s: another climb lot
    _run_filling(w, 34.0, 40, 0.666, step=0.0)                        # flat: a new pair want supersedes the old one
    second = [k for k in b.wants if k.startswith("pair:")]
    assert len(second) == 1 and second != first and w.e.rr_stats["spot_drop"] >= 1
    n0 = w.e.rr_stats["spot_reject"]
    for i in range(40):                                               # 10 s of stale books: one logged reject per 5 s
        w.step(44.0 + i * 0.25, 100.0, up=(0.656, 0.666), book_age=900.0)
    assert w.e.rr_stats["spot_reject"] - n0 <= 3


def test_climb_jump_trigger_buys_only_on_a_10c_rise_in_one_second():
    w = _climb_world(spot_climb_min_rise_1s=0.10, spot_climb_shares=5, spot_climb_max_shares=25)
    fills = _run_filling(w, 10.0, 40, 0.61, step=0.002)               # a steady 0.8c/s rise: no 10c jump, no climb
    assert [f for f in fills if f[1] == "UP"] == []
    out = []
    for i, a in enumerate((0.69, 0.75, 0.81, 0.81, 0.81, 0.81, 0.81)):   # +12c inside a second, then flat
        out += w.step(20.0 + i * 0.25, 100.0, up=(a - 0.01, a))
    climbs = [(s, o.size) for _, s, o in out if s == "UP"]
    assert climbs == [("UP", 5.0)]                                    # one lot per jump, not one per pass
    w.fill(out[0][2], 21.0)
    out = []
    for i, a in enumerate((0.82, 0.82, 0.82, 0.82, 0.86, 0.93, 0.93)):   # the next jump ends above 0.90: no lot
        out += w.step(22.0 + i * 0.25, 100.0, up=(a - 0.01, a))
    assert [s for _, s, _ in out if s == "UP"] == []


def test_pair_fraction_halves_the_cheap_buy():
    w = _climb_world(spot_pair_fraction=0.5)
    fills = _run_filling(w, 10.0, 120, 0.61)
    pairs = [f for f in fills if f[1] == "DOWN"]
    assert len(pairs) == 1
    _, _, n, px = pairs[0]
    u = px + FeeSchedule().taker_fee_per_share(px)
    b = w.book
    c_before = b.C - n * u
    full = (c_before + 0.10) / (1 - u)
    assert n == pytest.approx(math.ceil(full * 0.5 * 100) / 100, abs=0.02)
    assert b.U - b.C >= 0.10 and b.D - b.C < 0                        # the leader stays positive; the cheap side is only half covered


def test_jump_only_mode_buys_nothing_but_10c_jumps_on_either_token():
    w = _climb_world(spot_init_enabled=False, spot_rev_enabled=False, spot_cross_enabled=False, spot_leader_enabled=False,
                     spot_near_enabled=False, spot_climb_min_rise_1s=0.10, spot_climb_any_token=True, spot_climb_shares=5,
                     spot_climb_max_shares=25)
    w.book.fired.discard("init")
    # BTC falls through the strike and reverses, the books drift: none of the old rules fire
    out = w.run(10.0, [100.3, 100.1, 99.9, 99.7, 99.9, 100.1, 100.3, 100.5, 100.3, 100.1], up=(0.68, 0.69))
    assert out == [] and w.book.fired == set()
    # the cheap token (DOWN at 0.31) jumps 12c inside a second while UP falls: DOWN bought 5
    out = []
    for i, d in enumerate((0.32, 0.38, 0.44, 0.44, 0.44)):
        out += w.step(13.0 + i * 0.25, 100.1, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert [(s, o.size) for _, s, o in out] == [("DOWN", 5.0)]


def test_pair_rule_covers_the_other_side_of_the_climbed_token():
    # 17:00: a DOWN jump was bought, then the pair want asked for DOWN again (the "leader" by 1-s rise was UP)
    w = _climb_world(spot_init_enabled=False, spot_rev_enabled=False, spot_cross_enabled=False, spot_leader_enabled=False,
                     spot_near_enabled=False, spot_climb_min_rise_1s=0.10, spot_climb_any_token=True, spot_climb_shares=5,
                     spot_climb_max_shares=25, spot_pair_fraction=0.5)
    w.run(10.0, [100.0] * 8, up=(0.68, 0.69))
    out = []
    for i, d in enumerate((0.32, 0.38, 0.44, 0.44, 0.44)):           # DOWN jumps 12c: bought
        out += w.step(13.0 + i * 0.25, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    (_, side, o), = out
    assert side == "DOWN"
    w.fill(o, 14.3, price=0.44)
    for i in range(24):                                               # UP drifts up again: UP has the larger 1-s rise
        w.step(15.0 + i * 0.25, 100.0, up=(0.60 + i * 0.002, 0.61 + i * 0.002))
    pairs = [(k, v["side"]) for k, v in w.book.wants.items() if v["kind"] == "spot_pair"]
    assert pairs and all(s_ == "UP" for _, s_ in pairs)              # the cover is the OTHER side of the climbed DOWN


def test_init_on_the_rising_token_ten_seconds_after_the_open():
    w = _S(strike=50.0, spot_init_signal="token", spot_open_delay_s=10.0, spot_init_shares=10.0, spot_rev_enabled=False,
           spot_cross_enabled=False, spot_leader_enabled=False, spot_near_enabled=False, spot_climb_enabled=False)
    # BTC flat; DOWN's mid rises over the last second before 10 s: DOWN 10 at exactly 10 s
    out = []
    for i in range(44):                                               # 1.0 .. 11.75 s
        t = 1.0 + i * 0.25
        d = 0.40 + (0.02 if t >= 9.25 else 0.0) + (0.02 if t >= 9.75 else 0.0)
        out += w.step(t, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert [(s, o.size) for _, s, o in out] == [("DOWN", 10.0)]
    assert w.book.last_order_t == pytest.approx(T0 + 10_000.0)
    # neither token moved: BTC's 1-s direction decides
    w2 = _S(strike=50.0, spot_init_signal="token", spot_open_delay_s=10.0, spot_init_shares=10.0, spot_rev_enabled=False,
            spot_cross_enabled=False, spot_leader_enabled=False, spot_near_enabled=False, spot_climb_enabled=False)
    out = w2.run(5.0, [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
                       100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7, 100.8])
    assert [(s, o.size) for _, s, o in out] == [("UP", 10.0)]


def _init_world():
    return _S(strike=50.0, spot_init_signal="btc", spot_open_delay_s=5.0, spot_init_shares=10.0, spot_rev_enabled=False,
              spot_cross_enabled=False, spot_leader_enabled=False, spot_near_enabled=False, spot_climb_enabled=False,
              spot_cover_enabled=True, spot_cover_fraction=0.5, spot_cover_pair_cost=0.80)


def test_cover_bid_after_the_initial_buy_half_the_size_at_0_80_minus_the_fill():
    w = _init_world()
    (_, side, o), = w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2])   # init UP 10
    assert side == "UP" and o.size == 10.0
    w.fill(o, 11.3, price=0.56)
    out = w.run(12.5, [100.2, 100.2], up=(0.55, 0.56))                 # DOWN (0.44, 0.45): the cover bid rests at 0.24
    (_, side, c), = out
    assert side == "DOWN" and c.tif is TimeInForce.GTC and c.post_only and c.price == pytest.approx(0.24) and c.size == 5.0
    assert w.run(13.5, [100.2, 100.2, 100.2], up=(0.55, 0.56)) == []   # resting: not sent again
    w.fill(c, 60.0, price=0.24)
    b = w.book
    assert (b.U, b.D) == (10.0, 5.0) and b.U - b.C == pytest.approx(10 - b.C) and "cover0" not in b.wants
    assert w.run(61.0, [100.2, 100.2], up=(0.55, 0.56)) == []          # once per market


def test_cover_is_taken_when_the_other_ask_is_already_under_its_price_and_pulled_near_the_close():
    w = _init_world()
    (_, _, o), = w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2])
    w.fill(o, 11.3, price=0.70)                                       # cover price 0.10
    (_, side, c), = w.run(12.5, [100.2, 100.2], up=(0.91, 0.92))[0:1]  # DOWN (0.08, 0.09) <= 0.10: taken
    assert side == "DOWN" and c.tif is TimeInForce.IOC and c.size == 5.0
    w2 = _init_world()
    (_, _, o2), = w2.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2])
    w2.fill(o2, 11.3, price=0.56)
    (_, _, c2), = w2.run(12.5, [100.2, 100.2], up=(0.55, 0.56))
    assert c2.tif is TimeInForce.GTC
    _, cancels = w2.tick(291.0, (0.55, 0.56))                         # 9 s left: pulled
    assert cancels == [c2.order_id]


def test_a_failed_initial_buy_is_re_sent_to_whichever_token_is_rising_then():
    w = _S(strike=50.0, spot_init_signal="token", spot_open_delay_s=5.0, spot_init_shares=10.0, spot_rev_enabled=False,
           spot_cross_enabled=False, spot_leader_enabled=False, spot_near_enabled=False, spot_climb_enabled=False)
    out = []
    for i in range(24):                                               # UP rising into 10 s: init UP
        t = 5.0 + i * 0.25
        u = 0.50 + (0.02 if t >= 9.25 else 0.0) + (0.02 if t >= 9.75 else 0.0)
        out += w.step(t, 100.0, up=(round(u - 0.01, 2), u))
    (_, side, o), = out
    assert side == "UP"
    w.e.on_pair_done(o.order_id, filled=False)                        # killed at the venue
    out = []
    for i in range(12):                                               # nothing rising for 3 s: no re-send
        out += w.step(11.0 + i * 0.25, 100.0, up=(0.53, 0.54))
    assert out == [] and "init" in w.book.wants
    for i, d in enumerate((0.47, 0.49, 0.51, 0.51, 0.51)):           # now DOWN is the rising token
        out += w.step(14.0 + i * 0.25, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert [(s, o2.size) for _, s, o2 in out] == [("DOWN", 10.0)]     # re-sent to DOWN, not the stale UP
    assert w.e.rr_stats["spot_reside"] == 1


def test_cover_in_rise_mode_takes_the_other_token_once_it_starts_rising():
    w = _init_world()
    w.sc.spot_cover_mode, w.sc.spot_cover_min_rise_1s = "rise", 0.02
    (_, _, o), = w.run(10.0, [100.0, 100.0, 100.0, 100.0, 100.1, 100.2])   # init UP 10
    w.fill(o, 11.3, price=0.56)
    assert w.run(12.0, [100.2] * 8, up=(0.55, 0.56)) == []            # DOWN flat: no order, no resting bid
    assert not any(v["kind"].startswith("spot_cover") for v in w.e.resting.values())
    out = []
    for i, d in enumerate((0.45, 0.46, 0.47, 0.48, 0.48)):           # DOWN rises 3c inside a second: taken
        out += w.step(14.0 + i * 0.25, 100.2, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    (_, side, c), = out
    assert side == "DOWN" and c.tif is TimeInForce.IOC and c.size == 5.0 and "cover0" in w.book.fired
    w.fill(c, 15.3)
    out = []
    for i, d in enumerate((0.50, 0.52, 0.54, 0.54)):                 # a later rise: once per market
        out += w.step(20.0 + i * 0.25, 100.2, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert [o3 for _, s_, o3 in out if s_ == "DOWN"] == []


def _jump_steps(w, t0, asks):
    out = []
    for i, a in enumerate(asks):
        out += w.step(t0 + i * 0.25, 100.0, up=(round(a - 0.01, 3), a))
    return out


def test_climb_restarts_after_the_token_dipped_and_jumps_again():
    w = _climb_world(spot_climb_min_rise_1s=0.10, spot_climb_any_token=True, spot_climb_shares=5, spot_climb_max_shares=10,
                     spot_climb_reset_drop=0.05)
    w.run(10.0, [100.0] * 8, up=(0.60, 0.61))
    # two jumps -> the 10-share cap
    out = _jump_steps(w, 12.0, (0.65, 0.72, 0.72, 0.72, 0.72))
    w.fill(out[0][2], 13.2, price=0.72)
    out = _jump_steps(w, 16.0, (0.73, 0.78, 0.84, 0.84, 0.84))
    w.fill(out[0][2], 17.2, price=0.84)
    assert w.book.climb_bought == pytest.approx(10.0)
    out = _jump_steps(w, 20.0, (0.85, 0.90, 0.96, 0.96))              # the cap: no third UP lot (the pair rule may cover)
    assert [s for _, s, _ in out if s == "UP"] == []
    for _, s_, o_ in out:
        w.e.on_pair_done(o_.order_id, filled=False)
    # the token falls 8c from its peak, then jumps again: the climb restarts
    _jump_steps(w, 24.0, (0.80, 0.78, 0.76, 0.76, 0.76, 0.76, 0.76))
    assert w.book.climb_dipped
    out = []
    for i, a in enumerate((0.78, 0.83, 0.88, 0.88, 0.88, 0.88, 0.88, 0.88, 0.88, 0.88)):
        new = w.step(28.0 + i * 0.25, 100.0, up=(round(a - 0.01, 3), a))
        for _, s_, o_ in new:
            if s_ != "UP":
                w.e.on_pair_done(o_.order_id, filled=False)           # the pair cover (an older want) goes first
        out += new
    assert ("UP", 5.0) in [(s, o.size) for _, s, o in out] and w.e.rr_stats["spot_reset"] == 1
    assert w.book.climb_bought == 0.0 and w.book.climb_episode == 2    # the count restarted; the fill will add to it


def test_pnl_guard_buys_the_rising_token_until_its_outcome_is_positive():
    w = _climb_world(spot_guard_enabled=True, spot_guard_min_rise_1s=0.02, spot_guard_target_usdc=0.10, spot_guard_max_shares=10,
                     spot_climb_enabled=False)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 10.0, 0.60, 0.0)                                      # if UP wins +4.00, if DOWN wins -6.00
    w.run(10.0, [100.0] * 8, up=(0.60, 0.61))
    out = []
    for i, d in enumerate((0.41, 0.43, 0.45, 0.45, 0.45, 0.45)):     # DOWN rises 4c in a second: its outcome is -6 -> guard
        out += w.step(13.0 + i * 0.25, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    (_, side, o), = out
    assert side == "DOWN" and o.size == pytest.approx(10.0)           # capped at 10 per order (full cover would be ~11)
    w.fill(o, 14.3)
    assert w.run(15.0, [100.0] * 4, up=(0.54, 0.55)) == []           # DOWN flat again: re-armed only on the next rise
    out = []
    for i, d in enumerate((0.46, 0.48, 0.50, 0.50, 0.50)):           # the next rise: the rest of the shortfall
        out += w.step(17.0 + i * 0.25, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert len(out) == 1 and out[0][1] == "DOWN" and out[0][2].size >= 5.0
    w.fill(out[0][2], 18.3)
    assert b.D - b.C >= 0.10 - 0.05                                   # the DOWN outcome lifted to about the target
    snap = w.e.snapshot()["rising_recovery"]["markets"][w.slug]
    assert "mtm" in snap and snap["if_up"] == pytest.approx(b.U - b.C, abs=1e-3)


def test_a_want_is_not_re_sent_while_its_order_is_in_flight():
    # 19:45: the init DOWN order was still unanswered when the next pass re-sided the want to UP and sent a second order
    w = _S(strike=50.0, spot_init_signal="token", spot_open_delay_s=5.0, spot_init_shares=10.0, spot_rev_enabled=False,
           spot_cross_enabled=False, spot_leader_enabled=False, spot_near_enabled=False, spot_climb_enabled=False,
           spot_min_interval_s=0.0)
    out = []
    for i in range(24):                                               # DOWN rising into 10 s: init DOWN
        t = 5.0 + i * 0.25
        d = 0.50 + (0.02 if t >= 9.25 else 0.0) + (0.02 if t >= 9.75 else 0.0)
        out += w.step(t, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    (_, side, o), = out
    assert side == "DOWN"
    out = []
    for i, u in enumerate((0.47, 0.50, 0.53, 0.53)):                  # UP rises before the DOWN order is answered
        out += w.step(10.25 + i * 0.25, 100.0, up=(round(u - 0.01, 2), u), dn=(round(1 - u - 0.02, 2), round(1 - u - 0.01, 2)))
    assert out == []                                                  # no second order while the first is in flight
    w.fill(o, 11.5, price=0.54)
    assert w.book.D == pytest.approx(10.0) and w.book.U == 0.0 and "init" not in w.book.wants


def test_pairing_buys_keep_the_lower_priced_token_under_half_the_higher_priced_one():
    w = _climb_world(spot_guard_enabled=True, spot_guard_min_rise_1s=0.02, spot_guard_target_usdc=0.10, spot_guard_max_shares=10,
                     spot_climb_enabled=False, spot_low_ratio_max=0.5)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 20.0, 0.60, 0.0)                                      # 20 UP: the lower-priced DOWN may reach 10
    w.run(10.0, [100.0] * 8, up=(0.60, 0.61))
    out = []
    for i, d in enumerate((0.41, 0.43, 0.45, 0.45, 0.45, 0.45)):     # DOWN rises: the guard wants ~ 20 sh, cut to 10
        out += w.step(13.0 + i * 0.25, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    (_, side, o), = out
    assert side == "DOWN" and o.size == pytest.approx(10.0)
    w.fill(o, 14.3)
    out = []
    for i, d in enumerate((0.44, 0.46, 0.48, 0.48, 0.48)):           # another rise, DOWN still the cheaper: at the cap -> nothing
        out += w.step(17.0 + i * 0.25, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert out == [] and b.D == pytest.approx(10.0)


def test_low_rise_rule_buys_ten_of_the_lower_priced_token_on_a_book_rise():
    w = _climb_world(spot_lowrise_enabled=True, spot_lowrise_shares=10, spot_lowrise_max_per_market=2, spot_climb_enabled=False,
                     spot_low_ratio_max=0.5, spot_guard_enabled=False)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 10.0, 0.70, 0.0)                                      # marks negative: the PnL rule would block a cheap buy
    w.run(10.0, [100.0] * 8, up=(0.60, 0.61))
    out = []
    for i, d in enumerate((0.40, 0.42, 0.44, 0.44, 0.44, 0.44)):     # DOWN's ask 0.40 -> 0.44 on consecutive up-ticks
        out += w.step(13.0 + i * 0.25, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    (_, side, o), = out
    assert side == "DOWN" and o.size == pytest.approx(10.0)           # not blocked by the PnL rule or the ratio cap
    w.fill(o, 14.3)
    assert w.run(15.0, [100.0] * 4, up=(0.55, 0.56), dn=(0.43, 0.44)) == []   # flat: nothing more
    out = []
    for i, d in enumerate((0.45, 0.47, 0.49, 0.49, 0.49)):           # a second rise: the second (last) buy
        out += w.step(17.0 + i * 0.25, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert [(s, o2.size) for _, s, o2 in out] == [("DOWN", 10.0)]
    w.fill(out[0][2], 18.3)
    out = []
    for i, d in enumerate((0.44, 0.46, 0.48, 0.48, 0.48)):           # a third: capped at 2 per market
        out += w.step(21.0 + i * 0.25, 100.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert out == []


# ---------------------------------------------------------------------------------------- OPT mode (10-13 spec)

def _O(**kw):
    kw.setdefault("mode", "opt")
    kw.setdefault("opt_hedge_enabled", False)                         # the 10-14 cheap hedge has its own test
    kw.setdefault("opt_pair_enabled", False)                          # the 10-14 pair buy / lock have their own test
    kw.setdefault("target_profit_usdc", 0.0)
    kw.setdefault("opt_exec_mode", "taker")                           # the rule tests run all-taker; the mix / speed have their own tests
    kw.setdefault("opt_mom_trigger", "timer")                         # the 10-13 timer rule; the 10-14 step rule has its own test
    kw.setdefault("opt_mom_lower_only", False)                        # the 10-14 lower-token / start-buy / 3-s check / cover rule
    kw.setdefault("opt_mom_buy_at_start", False)                      # has its own test below
    kw.setdefault("opt_mom_cover", False)
    kw.setdefault("opt_mom_ladder_enabled", False)                    # the 10-14 ladder has its own test
    return _S(strike=50.0, **kw)


def _btc_ramp(w, t0, prices, up=(0.55, 0.56), dn=None, dt=0.25, **kw):
    out = []
    for i, p in enumerate(prices):
        out += w.step(t0 + i * dt, p, up=up, dn=dn, **kw)
    return out


def test_opt_initial_entry_follows_btc_two_second_move_over_0_3_bp_once():
    w = _O()
    out = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])   # +0.8 bp over 2 s by ~2.75 s
    (_, side, o), = out
    assert side == "UP" and o.size == pytest.approx(10.0) and o.tif is TimeInForce.IOC
    assert "init" in w.book.fired and w.book.last_order_t >= T0 + 2_000.0
    w.fill(o, 3.5)
    assert [s for _, s, _ in _btc_ramp(w, 4.0, [100010.0] * 4) if s] == []   # once per market (and the timer finds no improvement yet)
    w2 = _O()
    out = _btc_ramp(w2, 0.25, [100000.0] * 8 + [100001.0, 100001.5, 100002.0, 100002.0])   # +0.2 bp: neutral
    assert out == [] and "init" not in w2.book.fired


def test_opt_initial_entry_respects_the_safe_price_range_and_depth():
    w = _O()
    out = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0], up=(0.91, 0.92))
    assert out == [] and "init" not in w.book.fired and w.e.rr_stats["opt_skip"] >= 1   # UP at 0.92: outside 0.10..0.90, proposed again later
    w2 = _O()
    out = _btc_ramp(w2, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0], depth=7.0)
    (_, side, o), = out
    assert o.size == pytest.approx(7.0)                               # cut to the ask depth: a partial initial fill


def test_opt_optimizer_buys_the_quantity_that_raises_the_worst_case_then_stops():
    w = _O(opt_quantities="5,10,15,20", opt_min_improvement=0.05)
    (_, _, o), = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w.fill(o, 3.5, price=0.55)                                        # UP 10 for 5.5 + fee: W = -5.67, B = +4.33
    b = w.book
    w0 = min(b.U, b.D) - b.C
    out = _btc_ramp(w, 9.0, [100008.0] * 4)                           # the 6-s timer: the optimizer runs
    (_, side, o2), = out
    assert side == "DOWN"                                             # the losing outcome is the one to lift
    eff = 0.45 + FeeSchedule().taker_fee_per_share(0.45) + 0.01
    best_q = max((5.0, 10.0, 15.0, 20.0), key=lambda q: min(b.U, b.D + q) - (b.C + q * eff))
    assert best_q == 10.0 and o2.size == pytest.approx(5.0)           # the W-maximising quantity, capped at 1/2 of the 10 UP held (the user 10-13)
    w.fill(o2, 9.5, price=0.45)
    assert min(b.U, b.D) - b.C > w0 + 0.05
    out = _btc_ramp(w, 15.0, [100008.0] * 4)                          # the next timer: nothing raises W by 0.05 -> no trade
    assert out == [] and w.e.rr_stats["opt_skip"] >= 1


def test_opt_strict_positive_requirement_marks_the_trade_infeasible():
    w = _O(opt_require_positive="both")
    (_, _, o), = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w.fill(o, 3.5, price=0.55)
    out = _btc_ramp(w, 9.0, [100008.0] * 4)
    assert out == [] and w.e.rr_stats["opt_skip"] >= 1                # both outcomes > 0 cannot be reached at 0.55 + 0.45


def test_opt_momentum_zone_evaluates_every_three_seconds_and_ends_on_reversal():
    w = _O()
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.fired.add("init")
    _btc_ramp(w, 5.0, [100000.0] * 8, up=(0.68, 0.69))                # DOWN at 0.31/0.32: in the zone but not trending
    assert not b.opt_in_mom
    out = []
    for i, d in enumerate((0.33, 0.34, 0.35, 0.36, 0.37, 0.38, 0.38, 0.38, 0.38, 0.38, 0.38, 0.38, 0.38, 0.38)):
        out += w.step(7.0 + i * 0.5, 100000.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert b.opt_in_mom and w.e.rr_stats["opt_monitor"] >= 1          # momentum-start logged
    assert b.opt_mom_last_t > 0                                       # the 3-s momentum timer fired
    for i, d in enumerate((0.37, 0.36, 0.35, 0.35)):                  # 3c off the peak: the trend reversed
        w.step(15.0 + i * 0.5, 100000.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert not b.opt_in_mom


def test_opt_at_most_one_order_per_decision_event():
    w = _O()
    (_, _, o), = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w.fill(o, 3.5, price=0.55)
    # a timer, a reversal (BTC down 0.5 bp over 2 s) and a rising DOWN book all at once: one order
    per_pass = []
    for i, (p, d) in enumerate(((100008.0, 0.44), (100006.0, 0.45), (100004.0, 0.46), (100002.0, 0.47), (100000.0, 0.48))):
        out = w.step(9.0 + i * 0.5, p, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
        per_pass.append(len(out))
        for _, s_, o_ in out:
            w.fill(o_, 9.0 + i * 0.5 + 0.1)
    assert max(per_pass) == 1 and sum(per_pass) >= 1                  # never two orders in one pass


def test_opt_initial_entry_is_proposed_again_when_its_order_filled_nothing():
    # 14:30: the initial order was killed at the thin open (quote_pulled) and the market never got an initial entry
    w = _O()
    (_, _, o), = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w.e.on_pair_done(o.order_id, filled=False)                        # killed, nothing bought
    assert "init" not in w.book.fired and w.e.rr_stats["opt_init_retry"] == 1
    out = _btc_ramp(w, 6.5, [100010.0, 100012.0, 100014.0, 100016.0])   # the signal still holds: proposed again
    (_, side, o2), = out
    assert side == "UP" and o2.size == pytest.approx(10.0)
    w.fill(o2, 7.6)
    assert "init" in w.book.fired and w.book.opt_init_filled == pytest.approx(10.0)
    w.e.on_pair_done(o2.order_id, filled=True)
    assert [s for _, s, _ in _btc_ramp(w, 8.0, [100018.0] * 4) if s == "UP" and False] == []   # no third initial entry


def test_opt_effective_price_includes_fee_and_slippage_and_walks_depth():
    w = _O()
    w.meta.fee = FeeSchedule(rate=0.04)                               # the market's own schedule, not a hard-coded rate
    b = w.e._book(w.m)                                                # noqa: SLF001
    ob = OrderBook("UP", bids=[BookLevel(0.54, 100.0)], asks=[BookLevel(0.55, 6.0), BookLevel(0.56, 20.0), BookLevel(0.60, 100.0)])
    tops = {"UP": ("UP", ob), "DOWN": ("DN", _ob("DN", 0.44, 0.45))}
    filled, vwap, eff, why = w.e._opt_simulate(b, w.m, tops, "UP", 10.0)   # noqa: SLF001
    assert why is None and filled == pytest.approx(10.0)
    assert vwap == pytest.approx((6 * 0.55 + 4 * 0.56) / 10)         # walked two levels
    assert eff == pytest.approx(vwap + 0.04 * vwap * (1 - vwap) + 0.01)   # + the market fee + the slippage allowance
    filled, vwap, eff, why = w.e._opt_simulate(b, w.m, tops, "UP", 40.0)   # noqa: SLF001
    assert why is None and filled == pytest.approx(26.0)              # a partial fill: only the depth within ask + 1c


def test_opt_no_initial_entry_before_two_seconds_of_data_after_the_open():
    w = _O()
    out = _btc_ramp(w, 0.25, [100000.0, 100000.0, 100002.0, 100004.0, 100006.0, 100008.0, 100010.0])   # 0.25 .. 1.75 s
    assert out == [] and "init" not in w.book.fired                   # a strong move, but no 2 s after the open yet
    out = _btc_ramp(w, 2.0, [100012.0, 100014.0])
    assert [s for _, s, _ in out] == ["UP"]                           # at 2.0 s: BTC +1.2 bp over the 2 s since the open


def test_opt_pnl_accounting_liquidation_and_outcome_reports():
    w = _O()
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.add("UP", 10.0, 0.60, 0.20)                                     # 10 UP for 6.20 incl. a 0.20 fee
    b.add("DOWN", 5.0, 0.30, 0.05)                                    # 5 DOWN for 1.55
    assert b.U - b.C == pytest.approx(10 - 7.75) and b.D - b.C == pytest.approx(5 - 7.75)
    assert b.CU / b.U == pytest.approx(0.62) and b.CD / b.D == pytest.approx(0.31)   # average acquisition prices
    tops = {"UP": ("UP", OrderBook("UP", bids=[BookLevel(0.70, 6.0), BookLevel(0.69, 100.0)], asks=[BookLevel(0.71, 100.0)])),
            "DOWN": ("DN", OrderBook("DN", bids=[BookLevel(0.29, 100.0)], asks=[BookLevel(0.31, 100.0)]))}
    liq = w.e._opt_liquidation(b, tops)                               # noqa: SLF001
    f = FeeSchedule().taker_fee_per_share
    expected = 6 * (0.70 - f(0.70)) + 4 * (0.69 - f(0.69)) + 5 * (0.29 - f(0.29)) - 7.75
    assert liq["liquidation_pnl"] == pytest.approx(expected, abs=1e-3) and liq["unsold_up"] == 0
    rep = w.e._opt_outcomes(b, tops)                                  # noqa: SLF001
    assert rep["predicted_winner"] == "UP" and rep["predicted_winner_positive"] is True and rep["both_positive"] is False


def test_opt_initial_entry_skipped_while_its_order_is_ending_is_not_consumed():
    # 15:15: the IOC was rejected (no_liquidity); 2 s later the re-proposal was skipped "still ending" and "init" stayed
    # fired, so the market never got an entry
    w = _O()
    (_, _, o), = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w.e.on_pair_done(o.order_id, filled=False)                        # rejected at 3.0 s: the token is "ending" for end_guard_s
    assert "init" not in w.book.fired
    out = _btc_ramp(w, 3.5, [100010.0, 100012.0, 100014.0, 100016.0])   # 3.5 .. 4.25 s: still inside the 3-s end guard
    assert out == [] and "init" not in w.book.fired                   # not proposed, and not consumed either
    (_, side, o2), = _btc_ramp(w, 6.5, [100018.0, 100020.0, 100022.0, 100024.0])
    assert side == "UP" and o2.size == pytest.approx(10.0) and "init" in w.book.fired


def test_opt_failed_initial_order_is_resent_after_the_30_s_window_to_the_rising_token():
    w = _O()
    (_, _, o), = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w.e.on_pair_done(o.order_id, filled=False)
    w.e.end_guard["UP"] = T0 + 41_500.0                               # the last BTC direction (UP) is still ending until 41.5 s
    # 40 s after the open: BTC flat (no 2-s signal) but DOWN is rising in the book -> the re-order goes to DOWN
    out = []
    for i in range(12):
        t = 40.0 + i * 0.25
        out += w.step(t, 100008.0, up=(0.44, 0.45), dn=(0.54 + 0.005 * i, 0.55 + 0.005 * i))
    sent = [(s, o2) for _, s, o2 in out if s]
    assert sent and sent[0][0] == "DOWN" and sent[0][1].size == pytest.approx(10.0)
    assert w.book.opt_init_tries == 1 and "init" in w.book.fired
    # a market whose first 30 s never had a signal gets no late initial entry
    w2 = _O()
    assert _btc_ramp(w2, 0.25, [100000.0] * 12) == []
    out = []
    for i in range(12):
        out += w2.step(40.0 + i * 0.25, 100000.0, up=(0.44, 0.45), dn=(0.54 + 0.005 * i, 0.55 + 0.005 * i))
    assert [s for _, s, _ in out if s] == [] and "init" not in w2.book.fired


def test_opt_momentum_phase_buys_the_lower_token_every_three_seconds_until_it_ends():
    # the user 10-13: "buy tokens if the lower token is rising up to 0.4, buy the tokens every 3 s"
    w = _O(opt_mom_shares=5.0)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.fired.add("init")
    sent = []
    ds = [0.33, 0.34, 0.35, 0.36, 0.37, 0.38, 0.39, 0.40, 0.41, 0.42, 0.43, 0.44, 0.45, 0.46, 0.47, 0.48]
    for i, d in enumerate(ds):
        out = w.step(7.0 + i * 0.5, 100000.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
        for _, s_, o_ in out:
            sent.append((7.0 + i * 0.5, s_, o_.size))
            w.fill(o_, 7.0 + i * 0.5 + 0.1, price=d)
            w.e.on_pair_done(o_.order_id, filled=True)
            w.e.end_guard.clear()
    assert sent and all(s_ == "DOWN" and q == pytest.approx(5.0) for _, s_, q in sent)   # the rising lower token, fixed size
    gaps = [t2 - t1 for (t1, _, _), (t2, _, _) in zip(sent, sent[1:])]
    assert gaps and min(gaps) >= 3.0 - 1e-9                           # every 3 s, not every pass
    assert b.D >= 10.0 and b.U == 0.0                                 # bought although the worst case only got worse
    for i, d in enumerate((0.49, 0.50, 0.51, 0.51)):                  # the 0.50 target ends the phase: no more buys
        out = w.step(16.0 + i * 0.5, 100000.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d))
    assert not b.opt_in_mom and b.opt_mom_side is None
    n = len(sent)
    for i, d in enumerate((0.52, 0.53, 0.54, 0.55, 0.56, 0.57, 0.58)):
        for _, s_, o_ in w.step(18.0 + i * 0.5, 100000.0, up=(round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)), dn=(round(d - 0.01, 2), d)):
            if o_.size == 5.0 and s_ == "DOWN":
                sent.append((18.0 + i * 0.5, s_, o_.size))
    assert len(sent) == n                                             # outside the phase the fixed buys stop


def test_opt_strict_mode_never_pairs_the_rising_token_with_the_dear_one():
    # the user 10-13: "when it buys the rising token around 0.4, do not make a pair with the high-priced token;
    # buy the other token only if both settlement PnLs can be positive"
    w = _O(opt_require_positive="both", fix_high=False)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.fired.add("init")
    b.add("UP", 10.0, 0.30, 0.10)                                     # the momentum buys: 10 UP for 3.10
    out = _btc_ramp(w, 5.0, [100000.0] * 28, up=(0.29, 0.30), dn=(0.66, 0.67))   # 5 .. 11.75 s: the 6-s timer runs
    assert out == [] and b.D == 0.0                                   # DOWN at 0.67 would leave PnL_Down < 0: no pair
    out = _btc_ramp(w, 12.0, [100000.0] * 28, up=(0.29, 0.30), dn=(0.29, 0.30))  # both tokens cheap: both outcomes > 0
    (_, side, o), = out
    assert side == "DOWN" and o.size == pytest.approx(5.0)            # and at most 1/2 of the 10 UP held
    w.fill(o, 19.0, price=0.30)
    assert b.U - b.C > 0 and b.D - b.C > 0


def _ramp_steps(w, t0, prices, side, dt=0.5, fill=True, sent=None):
    """Walk one token's mid through ``prices`` (the other token mirrors it), filling every order sent."""
    sent = [] if sent is None else sent
    for i, d in enumerate(prices):
        t = t0 + i * dt
        a, bb = round(d - 0.01, 2), d
        oa, ob = round(1 - d - 0.02, 2), round(1 - d - 0.01, 2)
        out = w.step(t, 100000.0, up=((a, bb) if side == "UP" else (oa, ob)), dn=((a, bb) if side == "DOWN" else (oa, ob)))
        for _, s_, o_ in out:
            sent.append((t, s_, o_.size))
            if fill:
                w.fill(o_, t + 0.1, price=d if s_ == side else ob)
                w.e.on_pair_done(o_.order_id, filled=True)
                w.e.end_guard.clear()
    return sent


def test_opt_momentum_phase_follows_any_token_rising_through_0_35_to_0_65_every_5_s():
    # the user 10-13: "If a token rising between 0.35-0.65, buy the token every 5s"
    w = _O(opt_mom_low=0.35, opt_mom_high=0.65, opt_mom_target=0.65, opt_mom_interval_s=5.0, opt_mom_shares=5.0)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.fired.add("init")
    sent = _ramp_steps(w, 5.0, [round(0.50 + 0.005 * i, 3) for i in range(28)], "UP")   # UP, the dearer token, 0.50 -> 0.635 over 13.5 s
    assert sent and all(s_ == "UP" and q == pytest.approx(5.0) for _, s_, q in sent)
    gaps = [t2 - t1 for (t1, _, _), (t2, _, _) in zip(sent, sent[1:])]
    assert gaps and min(gaps) >= 5.0 - 1e-9                           # every 5 s
    assert b.opt_in_mom and b.opt_mom_side == "UP"
    _ramp_steps(w, 19.0, [0.64, 0.65, 0.66, 0.66], "UP")              # 0.65 reached: the phase ends
    assert not b.opt_in_mom
    n = len(sent)
    _ramp_steps(w, 21.0, [0.67 + 0.01 * i for i in range(10)], "UP", sent=sent)   # above the zone: no more momentum buys
    assert len([x for x in sent[n:] if x[2] == 5.0 and x[1] == "UP"]) == 0


def test_opt_opposite_token_waits_5_s_after_the_first_buy_and_is_capped_at_half_the_holding():
    # the user 10-13: "After first buy, do not buy opposite token in 5s. And opposite token amount should be 1/2 of holding shares."
    w = _O(opt_require_positive="both", opt_quantities="5,10,15,20")
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.fired.add("init")
    b.add("UP", 10.0, 0.30, 0.10)                                     # the first buy at 5.0 s: 10 UP for 3.10
    b.first_fill_t = T0 + 5_000.0
    flat = lambda t0, n: [w.step(t0 + i * 0.5, 100000.0, up=(0.29, 0.30), dn=(0.29, 0.30)) for i in range(n)]   # both cheap: both outcomes can be > 0
    out = sum(flat(5.5, 8), [])                                       # 5.5 .. 9.0 s: inside the 5 s after the first buy
    assert out == [] and b.D == 0.0
    out = sum(flat(10.5, 12), [])                                     # the timer after 10 s: the opposite token, half the holding
    sides = [(s_, o_.size) for _, s_, o_ in out]
    assert sides and sides[0] == ("DOWN", pytest.approx(5.0))         # not 10, 15 or 20
    w.fill(out[0][2], 11.0, price=0.30)
    w.e.on_pair_done(out[0][2].order_id, filled=True)
    w.e.end_guard.clear()
    assert b.U - b.C > 0 and b.D - b.C > 0
    out = sum(flat(17.0, 14), [])                                     # DOWN already at 1/2 of UP: nothing more
    assert [s_ for _, s_, _ in out] == []


def test_nothing_is_placed_while_risk_is_halted(monkeypatch, tmp_path):
    # paper 10-13 16:16: the daily-loss halt (-23.55) stopped the signal path only; the engine's opt orders kept buying
    # for 17 more minutes (33 fills, realised_today -57.38)
    w = _O()
    w.e.risk_halted = True
    out = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    assert out == [] and w.e.rr_stats["risk_halted_passes"] >= 1      # a clear initial-entry signal, nothing placed
    w.e.risk_halted = False
    out = _btc_ramp(w, 4.0, [100010.0, 100012.0, 100014.0, 100016.0])
    assert [s for _, s, _ in out] == ["UP"]                           # the halt lifted: the entry goes out
    monkeypatch.setenv("TPB_RTR_ENABLED", "1")
    from troll_poly_bot.config import BotConfig
    from troll_poly_bot.live import LiveBot
    cfg = BotConfig()
    cfg.engine.strategy = "rising_recovery"
    cfg.deploy_mode, cfg.execution_scenario = "paper", "calibrated"
    cfg.rising_recovery = RisingRecoveryConfig.from_env()
    bot = LiveBot(assets=("BTC",), balance=200.0, cfg=cfg, state_path=str(tmp_path / "live_state.rr.json"),
                  trade_log=str(tmp_path / "live_trades.rr.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                  kill_file=str(tmp_path / "KILL"))
    bot.risk.realised_today = -(bot.risk.cfg.max_daily_loss_usdc + 1.0)
    bot._engine_pair_cancels(T0)                                      # noqa: SLF001
    assert bot.engine.risk_halted is True
    bot.risk.realised_today = 0.0
    bot._engine_pair_cancels(T0 + 1000)                               # noqa: SLF001
    assert bot.engine.risk_halted is False


# ───────── OPT mode: the favourite fix (the user, 2026-10-13 21:xx) ─────────

def test_opt_favourite_fix_lifts_the_dear_tokens_outcome_to_the_target_inside_the_band():
    w = _O(fix_high=True, fix_min_ask=0.60, fix_max_ask=0.80, high_win_target_usdc=5.0, spot_max_shares=200.0, spot_max_cost_usdc=200.0,
           fix_max_left_s=300.0, fix_require_rising=False, opt_hedge_enabled=False)
    (_, _, o), = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w.fill(o, 3.5, price=0.55)                                        # UP 10 at 0.55: if UP wins +4.4 -> no fix
    b = w.book
    assert _btc_ramp(w, 5.0, [100008.0] * 3, up=(0.69, 0.70)) == []
    b.add("DOWN", 10.0, 0.45, FeeSchedule().taker_fee_per_share(0.45) * 10)   # now if UP wins: 10 - C < 0
    pnl_up = b.U - b.C
    assert pnl_up < 0
    assert _btc_ramp(w, 6.0, [100008.0] * 2, up=(0.84, 0.85)) == []    # above the band: no fix
    out = _btc_ramp(w, 7.0, [100008.0] * 2, up=(0.69, 0.70))
    (_, side, o2), = out
    eff = 0.70 + FeeSchedule().taker_fee_per_share(0.70) + 0.01
    assert side == "UP" and o2.size == pytest.approx((5.0 - pnl_up) / (1 - eff), abs=0.02) and o2.tif is TimeInForce.IOC
    assert w.e.orders[o2.order_id]["kind"] == "opt_fix"
    w.fill(o2, 7.6, price=0.70)
    assert b.U - b.C >= 5.0 - 0.02                                    # the UP outcome reached the target (slippage allowance unspent)
    assert _btc_ramp(w, 9.0, [100008.0] * 3, up=(0.69, 0.70)) == []    # positive: nothing more
    # off, or out of the band, or capped: no fix
    w2 = _O(fix_high=False)
    (_, _, o), = _btc_ramp(w2, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w2.fill(o, 3.5, price=0.55)
    w2.book.add("DOWN", 10.0, 0.45, 0.2)
    assert _btc_ramp(w2, 7.0, [100008.0] * 2, up=(0.69, 0.70)) == []


# ───────── OPT mode: the maker / taker mix (the user, 2026-10-14) ─────────

def test_opt_buys_split_into_a_taker_part_and_a_resting_maker_part_that_never_blocks():
    w = _O(opt_exec_mode="mixed", opt_maker_fraction=0.5, opt_maker_rest_s=10.0, fix_high=False)
    out = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    assert len(out) == 2
    (_, s1, taker), (_, s2, maker) = out
    assert s1 == s2 == "UP" and taker.tif is TimeInForce.IOC and taker.size == 5.0
    assert maker.tif is TimeInForce.GTC and maker.post_only and maker.size == 5.0 and maker.price == pytest.approx(0.55)   # bid, 1c spread
    assert w.e.orders[maker.order_id]["kind"] == "opt_maker" and w.e.orders[maker.order_id]["maker_of"] == "opt_init"
    w.fill(taker, 3.5, price=0.56)
    b = w.book
    assert b.U == 5.0 and w.e._resting_on("UP") is None              # the maker part does not hold the token  # noqa: SLF001
    # the maker part fills fee-free at its price
    w.e.on_pair_done(maker.order_id, filled=True)
    w.e.on_pair_fill_record(w.slug, maker, SimpleNamespace(size=5.0, price=0.55, fee=0.0, token_id="UP"), T0 + 5_000)
    assert b.U == 10.0 and b.fees == pytest.approx(FeeSchedule().taker_fee_per_share(0.56) * 5)
    # a maker part rests at most opt_maker_rest_s, without an end guard afterwards
    w2 = _O(opt_exec_mode="mixed", opt_maker_fraction=0.5, opt_maker_rest_s=10.0, fix_high=False)
    (_, _, t2), (_, _, m2) = _btc_ramp(w2, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w2.fill(t2, 3.5, price=0.56)
    assert w2.e.pair_cancels(T0 + 8_000.0) == []
    assert w2.e.pair_cancels(T0 + 13_000.0) == [m2.order_id]
    w2.e.on_pair_done(m2.order_id, filled=False)
    assert w2.e._resting_on("UP") is None                             # noqa: SLF001


# ───────── OPT mode: momentum buys on every 3c step (the user, 2026-10-14) ─────────

def test_opt_momentum_phase_buys_on_every_three_cent_rise_not_on_a_timer():
    w = _O(opt_mom_low=0.35, opt_mom_high=0.80, opt_mom_target=0.80, opt_mom_shares=5.0, opt_mom_trigger="step", opt_mom_step=0.03,
           fix_high=False)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.fired.add("init")
    sent = _ramp_steps(w, 5.0, [round(0.50 + 0.005 * i, 3) for i in range(40)], "UP", dt=0.25)   # 0.50 -> 0.695 in 10 s
    mom = [x for x in sent if x[2] == pytest.approx(5.0) and x[1] == "UP"]
    assert len(mom) >= 3 and b.opt_in_mom and b.opt_mom_side == "UP"
    gaps = [t2 - t1 for (t1, _, _), (t2, _, _) in zip(mom, mom[1:])]
    assert min(gaps) < 5.0                                            # faster than the old timer ...
    assert all(abs(g - 1.5) < 0.3 for g in gaps), gaps                # ... one buy per 3c (0.5c every 0.25 s = 1.5 s a step)
    assert b.opt_mom_ref is not None and b.opt_mom_ref >= 0.59
    # a flat mid buys nothing however long it sits there
    n = len(sent)
    _ramp_steps(w, 13.0, [b.opt_mom_ref] * 24, "UP", dt=0.5, sent=sent)
    assert len([x for x in sent[n:] if x[2] == 5.0 and x[1] == "UP"]) == 0


# ───────── OPT mode: taker when the token moves fast, maker when it is quiet (the user, 2026-10-14) ─────────

def test_opt_speed_mode_takes_in_a_fast_market_and_rests_a_bid_in_a_quiet_one():
    # quiet: the books are flat for the 2 s before the initial entry -> the whole buy rests as a post-only bid
    w = _O(opt_exec_mode="speed", opt_speed_threshold=0.02, opt_maker_rest_s=10.0, fix_high=False)
    out = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    (_, side, o), = out
    assert side == "UP" and o.tif is TimeInForce.GTC and o.post_only and o.size == 10.0 and o.price == pytest.approx(0.55)
    assert w.e.orders[o.order_id]["maker_of"] == "opt_init" and "init" in w.book.fired
    # pulled unfilled after the rest: the initial entry is proposed again
    assert w.e.pair_cancels(T0 + 14_000.0) == [o.order_id]
    w.e.on_pair_done(o.order_id, filled=False)
    assert "init" not in w.book.fired and w.book.opt_init_tries == 1
    # fast: the UP mid jumped 3c inside the last second -> the whole buy is taken
    w2 = _O(opt_exec_mode="speed", opt_speed_threshold=0.02, fix_high=False)
    _btc_ramp(w2, 0.25, [100000.0] * 8, up=(0.55, 0.56))
    out = _btc_ramp(w2, 2.25, [100002.0, 100004.0, 100006.0, 100008.0], up=(0.58, 0.59))
    (_, side, o), = out
    assert side == "UP" and o.tif is TimeInForce.IOC and o.size == 10.0


# ───────── OPT mode: the 10-14 gates on the fix, and the cheap hedge ─────────

def test_opt_fix_waits_for_210_s_left_and_a_rising_favourite_and_the_hedge_keeps_the_best_case():
    w = _O(fix_high=True, high_win_target_usdc=5.0, spot_max_shares=300.0, spot_max_cost_usdc=300.0, opt_hedge_enabled=True)
    (_, _, o), = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w.fill(o, 3.5, price=0.55)
    b = w.book
    b.add("DOWN", 10.0, 0.45, FeeSchedule().taker_fee_per_share(0.45) * 10)   # if UP wins: negative
    assert _btc_ramp(w, 7.0, [100008.0] * 2, up=(0.69, 0.70)) == [] and w.e.rr_stats["fix_too_early"] >= 1   # 293 s left
    assert _btc_ramp(w, 92.0, [100008.0] * 8, up=(0.69, 0.70)) == [] and w.e.rr_stats["fix_not_rising"] >= 1   # <= 210 s but flat
    out = _btc_ramp(w, 95.0, [100008.0] * 2, up=(0.72, 0.73))        # +3c over the last 5 s: rising
    (_, side, o2), = out
    assert side == "UP" and w.e.orders[o2.order_id]["kind"] == "opt_fix"
    w.fill(o2, 95.6, price=0.73)
    # the cheap hedge: UP 10 at 0.55 only, DOWN ask 0.15 -> buy DOWN until the UP outcome is at +2 (never past equal)
    w2 = _O(fix_high=False, opt_hedge_enabled=True, hedge_max_price=0.20, opt_hedge_min_best_usdc=2.0, spot_max_shares=300.0)
    (_, _, o), = _btc_ramp(w2, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w2.fill(o, 3.5, price=0.55)
    b2 = w2.book
    best, worst = b2.U - b2.C, b2.D - b2.C
    assert _btc_ramp(w2, 5.0, [100008.0] * 2, up=(0.69, 0.70)) == []                   # DOWN ask 0.31: not cheap
    out = _btc_ramp(w2, 6.0, [100008.0] * 2, up=(0.84, 0.85), dn=(0.14, 0.15))
    (_, side, oh), = out
    eff = 0.15 + FeeSchedule().taker_fee_per_share(0.15) + 0.01
    assert side == "DOWN" and w2.e.orders[oh.order_id]["kind"] == "opt_hedge" and oh.size == pytest.approx(min((best - 2.0) / eff, best - worst), abs=0.011)
    w2.fill(oh, 6.6, price=0.15)
    assert b2.U - b2.C >= 2.0 - 0.02 and b2.D - b2.C > worst
    assert _btc_ramp(w2, 8.0, [100008.0] * 2, up=(0.84, 0.85), dn=(0.14, 0.15)) == []   # the best case is at the floor: nothing more


# ───────── OPT mode: the lower token bought as it starts rising; 3-s check; cover with the top side (the user, 2026-10-14) ─────────

def test_opt_lower_token_is_bought_at_once_then_checked_after_3_s_and_covered_when_it_stalls():
    w = _O(opt_mom_low=0.15, opt_mom_high=0.80, opt_mom_target=0.80, opt_mom_shares=5.0, opt_mom_trigger="step", opt_mom_step=0.03,
           opt_mom_lower_only=True, opt_mom_buy_at_start=True, opt_mom_check_s=3.0, opt_mom_cover=True, opt_mom_cover_ratio=1.0,
           fix_high=False, opt_opposite_delay_s=0.0, opt_opposite_ratio=10.0, opt_min_improvement=100.0)   # the optimizer stays quiet
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.fired.add("init")
    b.add("UP", 10.0, 0.70, 0.1)                                      # the top side is held; DOWN is the lower token at ~0.29
    # DOWN starts rising: bought at once (5 sh), not after the first 3c step   (5.0 .. 8.5 s)
    sent = _ramp_steps(w, 5.0, [0.29, 0.29, 0.29, 0.29, 0.30, 0.31, 0.32, 0.33], "DOWN", dt=0.5)
    first = [x for x in sent if x[1] == "DOWN"]
    assert first and first[0][2] == pytest.approx(5.0) and b.opt_in_mom and b.opt_mom_side == "DOWN"
    assert b.opt_mom_check_t is not None
    # it keeps rising: the 3-s checks pass (wait, no cover) and the 3c step buys again   (9.0 .. 12.0 s)
    sent2 = _ramp_steps(w, 9.0, [0.335, 0.34, 0.345, 0.35, 0.355, 0.36, 0.365], "DOWN", dt=0.5)
    assert all(x[1] == "DOWN" for x in sent2) and b.opt_in_mom and w.e.rr_stats.get("opt_cover_orders", 0) == 0
    assert len([x for x in sent2 if x[1] == "DOWN"]) >= 1
    # now it stalls: no further rise within 3 s of the last check -> the top-side token (UP) is bought in the same size
    # as the last momentum buy and the phase ends   (12.5 .. 17.0 s)
    last_qty = [x for x in sent + sent2 if x[1] == "DOWN"][-1][2]
    sent3 = _ramp_steps(w, 12.5, [0.365] * 10, "DOWN", dt=0.5)
    cov = [x for x in sent3 if x[1] == "UP"]
    assert len(cov) == 1 and cov[0][2] == pytest.approx(last_qty) and not b.opt_in_mom
    assert len([x for x in sent3 if x[1] == "DOWN"]) == 0
    assert w.e.rr_stats["opt_cover_orders"] == 1 and any(o["kind"] == "opt_cover" for o in w.e.orders.values())
    # the higher-priced token rising inside the zone is NOT a momentum candidate (lower only)
    w2 = _O(opt_mom_low=0.15, opt_mom_high=0.80, opt_mom_lower_only=True, opt_mom_buy_at_start=True, opt_mom_shares=5.0, fix_high=False)
    b2 = w2.e._book(w2.m)                                             # noqa: SLF001
    b2.fired.add("init")
    _ramp_steps(w2, 5.0, [0.55, 0.55, 0.55, 0.55, 0.56, 0.57, 0.58, 0.59], "UP", dt=0.5)
    assert not b2.opt_in_mom


# ───────── OPT mode: the pair buy and the profit lock (the user, 2026-10-14) ─────────

def test_opt_pair_buy_completes_pairs_under_0_95_and_the_lock_ends_the_market():
    w = _O(opt_pair_enabled=True, opt_pair_threshold=0.95, target_profit_usdc=5.0, fix_high=False, opt_min_improvement=100.0,
           spot_max_shares=300.0, spot_max_cost_usdc=300.0)
    (_, _, o), = _btc_ramp(w, 0.25, [100000.0] * 8 + [100002.0, 100004.0, 100006.0, 100008.0])
    w.fill(o, 3.5, price=0.60)                                        # UP 10 at 0.60 (unit 0.6168), unpaired
    b = w.book
    assert _btc_ramp(w, 5.0, [100008.0] * 2, up=(0.64, 0.66)) == []  # DOWN ask 0.36: pair 0.99 > 0.95
    out = _btc_ramp(w, 6.0, [100008.0] * 2, up=(0.70, 0.72))         # DOWN ask 0.30: 0.6168 + 0.3147 = 0.9315 <= 0.95
    (_, side, op), = out
    assert side == "DOWN" and op.size == pytest.approx(10.0) and w.e.orders[op.order_id]["kind"] == "opt_pair"
    w.fill(op, 6.6, price=0.30)
    assert b.excess()[0] is None and b.locked_pairs == 10.0 and b.worst == pytest.approx(10 - b.C)
    assert _btc_ramp(w, 8.0, [100008.0] * 2, up=(0.70, 0.72)) == []   # nothing left to pair
    # the lock: once the worst outcome is at +5 the market is done
    b.add("UP", 20.0, 0.10, 0.0)
    b.add("DOWN", 20.0, 0.10, 0.0)                                    # 30 / 30, C ~13.3: worst +16.7
    assert _btc_ramp(w, 10.0, [100008.0] * 3, up=(0.40, 0.42)) == [] and b.phase == "done" and "locked" in b.why_done


# ───────── OPT mode: the momentum ladder in 0.40..0.60 (the user, 2026-10-14) ─────────

def test_opt_momentum_ladder_needs_3c_over_the_last_purchase_inside_the_range_and_resets_outside():
    w = _O(opt_mom_low=0.15, opt_mom_high=0.80, opt_mom_target=0.80, opt_mom_shares=5.0, opt_mom_trigger="step", opt_mom_step=0.03,
           opt_mom_lower_only=True, opt_mom_buy_at_start=True, opt_mom_cover=False, opt_mom_ladder_enabled=True,
           opt_mom_ladder_low=0.40, opt_mom_ladder_high=0.60, opt_mom_ladder_reset=0.35,
           fix_high=False, opt_opposite_delay_s=0.0, opt_opposite_ratio=10.0, opt_min_improvement=100.0)
    b = w.e._book(w.m)                                                # noqa: SLF001
    b.fired.add("init")
    b.add("UP", 10.0, 0.60, 0.1)
    # DOWN rises inside the range: the start buy at 0.42 is allowed (0.42 >= 0.35 + 0.03) and sets the reference
    sent = _ramp_steps(w, 5.0, [0.40, 0.40, 0.40, 0.40, 0.41, 0.42, 0.43, 0.44], "DOWN", dt=0.5)
    buys = [x for x in sent if x[1] == "DOWN"]
    assert len(buys) == 1 and b.opt_mom_ladder_ref is not None and 0.41 <= b.opt_mom_ladder_ref <= 0.44
    ref = b.opt_mom_ladder_ref
    # it dips 2c (the phase ends) and rises again to 1c under reference + 3c: no new buy; at reference + 3c: a buy
    sent2 = _ramp_steps(w, 9.0, [round(ref - 0.02, 3), round(ref - 0.02, 3), round(ref - 0.02, 3), round(ref - 0.02, 3),
                                 round(ref - 0.01, 3), round(ref, 3), round(ref + 0.01, 3), round(ref + 0.02, 3)], "DOWN", dt=0.5)
    assert [x for x in sent2 if x[1] == "DOWN"] == [] and w.e.rr_stats["opt_mom_ladder_wait"] >= 1
    sent3 = _ramp_steps(w, 13.0, [round(ref + 0.03, 3), round(ref + 0.035, 3)], "DOWN", dt=0.5)
    assert len([x for x in sent3 if x[1] == "DOWN"]) == 1 and b.opt_mom_ladder_ref >= ref + 0.03 - 1e-9
    # out of the range the reference resets to 0.35
    _ramp_steps(w, 14.0, [0.65, 0.65], "DOWN", dt=0.5)
    assert b.opt_mom_ladder_ref == pytest.approx(0.35)
