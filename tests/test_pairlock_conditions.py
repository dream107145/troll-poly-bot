"""THE CONDITIONS RULE of tpb-pairlock-paper (the user, 2026-10-02 20:33 UTC; strategy/pairlock_recovery_engine.py in
entry mode "conditions"): buy 20 of the higher-priced token when its ask is 0.50-0.60, >= 120 s are left, 0 < BTC -
strike < 5 and the market holds nothing, then a bid for the other token at pair_sum - fill that is never pulled (the
recovery is off); with < 20 s left and the higher token's mid > 0.93 buy 30 of it, and if its mid then falls under
0.90 bid 0.20 for 30 of the other token. The settings are pairbtc.* on the dashboard, never the recovery bot's."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_pairlock_engine_v2 import _model
from test_pairlock_recovery_engine import _deep, _eng, _fill
from test_spot_lag import NOW, SLUG, T0, _book, _meta, _Vol

from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.strategy import pairlock_recovery as rc
from troll_poly_bot.types import Fill, Side, TimeInForce

CLOSE = T0 + 300_000.0
PRM = dict(entry_mode="conditions", cond_open_buy=False, open_shares=20.0, cond_min_price=0.50, cond_max_price=0.60, cond_min_left_s=120.0,
           cond_spot_lo=0.0, cond_spot_hi=5.0, pair_sum=0.97, pair_pull=False, recovery_enabled=False,
           late_trigger="price", late_min_mid=0.93, late_max_price=0.99, late_shares=30.0, late_max_orders=1,
           late_max_capital=30.0, late_hedge_trigger=0.90, late_hedge_price=0.20, max_total_position=120.0)


def _ceng(**kw):
    e = _eng(rprm=rc.RecoveryParams(**{**PRM, **kw}))
    e.hist.clear()
    for t in range(int(T0 - 2000), int(T0), 200):
        e._record(_meta("BTC"), _book("UP", 0.50, 0.52), _book("DOWN", 0.48, 0.50), _view(100.0), float(t))   # noqa: SLF001
    return e


def _view(btc):
    return CompositeView("BTC", btc, btc, 3, 1.0, 1.0, 50.0, {"binance": btc, "coinbase": btc, "bybit": btc})


def _ev(e, now, up, down, btc=102.0):
    from test_pairlock_engine_v2 import _feed_to
    tw = _feed_to(e, now)
    return e.evaluate(_meta("BTC"), _deep("UP", *up), _deep("DOWN", *down), _view(btc), 200.0, 100.0, now, _Vol(), tw,
                      COLOCATED, 500.0)


def _buy(e, t, up=(0.54, 0.55), down=(0.45, 0.46), btc=102.0, price=0.55, size=None):
    ev = _ev(e, t, up, down, btc)
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "open", (ev.reason, ev.detail)
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, price, size if size is not None else ev.size, t + 300.0)
    return ev, o


def test_all_four_conditions_buy_20_of_the_higher_token():
    e = _ceng()
    ev, o = _buy(e, T0 + 30_000.0)
    assert ev.side == "UP" and ev.size == 20.0 and o.tif is TimeInForce.FOK and e.strategy_label == "R-cond"
    assert e.cycles[SLUG].book.up_qty == 20.0 and "BTC - strike +2.00" in ev.size_note
    # each condition alone blocks it
    t = T0 + 30_000.0
    assert not _ev(_ceng(), t, (0.61, 0.62), (0.38, 0.39)).tradeable                    # higher token 0.62 > 0.60
    dn = _ev(_ceng(), t, (0.47, 0.48), (0.51, 0.52))                                   # DOWN the higher token, 0.52
    assert dn.tradeable and dn.side == "DOWN", (dn.reason, dn.detail)
    assert not _ev(_ceng(), T0 + 181_000.0, (0.54, 0.55), (0.45, 0.46)).tradeable        # 119 s left < 120
    assert not _ev(_ceng(), t, (0.54, 0.55), (0.45, 0.46), btc=105.0).tradeable          # BTC - strike = 5, not < 5
    assert not _ev(_ceng(), t, (0.54, 0.55), (0.45, 0.46), btc=100.0).tradeable          # = 0, not > 0
    assert not _ev(_ceng(), t, (0.54, 0.55), (0.45, 0.46), btc=99.0).tradeable           # under the strike
    # 4. the market holds tokens: no second buy
    assert not _ev(e, T0 + 60_000.0, (0.54, 0.55), (0.45, 0.46)).tradeable
    assert e.stats["open_sent"] == 1


def test_the_pair_bid_rests_and_is_never_pulled_and_the_recovery_is_off():
    e = _ceng()
    t = T0 + 30_000.0
    _buy(e, t, price=0.55)
    m = _meta("BTC").market
    side, po = e.pair_order(m, "UP", 20.0, 0.55, t + 300.0)
    assert side == "DOWN" and po.price == pytest.approx(0.42) and po.size == 20.0 and po.tif is TimeInForce.GTC
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    e.plmodel = _model(0.0)
    from test_pairlock_recovery_engine import _feat
    e._features = lambda meta, now: _feat(0.30)                     # noqa: SLF001
    ev = _ev(e, t + 20_000.0, (0.30, 0.31), (0.69, 0.70))           # DOWN far above UP
    assert not ev.tradeable and e.pair_cancels(t + 20_000.0) == []    # not pulled, no recovery buy
    assert e.cycles[SLUG].pair["state"] == "working" and e.stats["recovery_sent"] == 0


def test_the_late_buy_takes_30_above_093_and_the_hedge_bids_020_under_090():
    e = _ceng()
    t = CLOSE - 15_000.0
    m = _meta("BTC").market
    assert not _ev(e, t - 10_000.0, (0.95, 0.96), (0.04, 0.05)).tradeable          # 25 s left: not yet
    assert not _ev(e, t - 500.0, (0.92, 0.93), (0.07, 0.08)).tradeable             # mid 0.925 not above 0.93
    ev = _ev(e, t, (0.95, 0.96), (0.04, 0.05))
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "late" and ev.side == "UP" and ev.size == 30.0, (ev.reason, ev.detail)
    assert ev.limit_price == pytest.approx(0.99)
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.96, 30.0, t + 300.0)
    cyc = e.cycles[SLUG]
    assert cyc.book.up_qty == 30.0 and cyc.late_hedge["state"] == "armed" and cyc.late_hedge["qty"] == 30.0
    assert not _ev(e, t + 2000.0, (0.95, 0.96), (0.04, 0.05)).tradeable            # one late order per market
    assert e.pair_requests(t + 2000.0, {SLUG: m}) == []                            # mid 0.955: no hedge
    _ev(e, t + 3000.0, (0.85, 0.87), (0.13, 0.15))                                  # UP mid 0.86 < 0.90
    reqs = e.pair_requests(t + 3000.0, {SLUG: m})
    assert len(reqs) == 1
    slug, side, ho = reqs[0]
    assert (slug, side) == (SLUG, "DOWN") and ho.price == pytest.approx(0.20) and ho.size == 30.0 and ho.token_id == "DOWN"
    assert ho.tif is TimeInForce.GTC and ho.expires_ts == m.close_ts
    assert e.pair_requests(t + 3200.0, {SLUG: m}) == []                            # once
    e.on_pair_submit(ho.order_id, SLUG, side, ho.price, ho.size)
    assert cyc.pair is None and cyc.late_hedge["state"] == "working"               # not mistaken for a pair bid
    e.on_pair_done(ho.order_id, filled=True)                                        # live.py: the result, then its fills
    f = Fill(ho.order_id, "DOWN", Side.BUY, 0.14, 30.0, 30.0 * 0.07 * 0.14 * 0.86, t + 3500.0, t + 3500.0, t + 3500.0,
             expected_price=0.20)
    e.on_pair_fill_record(SLUG, ho, f, t + 3500.0)
    assert cyc.book.down_qty == 30.0 and e.stats["late_hedge_fills"] == 1 and not e.accounting_halt
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    pnl = cyc.book.resolution_pnl("DOWN")
    e.on_market_settled(SLUG, False, pnl, m.close_ts + 40_000.0)
    assert not e.accounting_halt and e.stats["late_leg"] == pytest.approx(pnl)        # the late buy + its hedge


def test_the_settings_are_this_bots_own_keys():
    from troll_poly_bot.control import TUNABLES, apply_to, current_values
    from troll_poly_bot.live import LiveBot
    cond, rec = _ceng(), _eng(rprm=rc.RecoveryParams(entry_mode="open", direction="price"))
    tc = LiveBot._control_targets(SimpleNamespace(engine=cond, micro_caps=None))          # noqa: SLF001
    tr = LiveBot._control_targets(SimpleNamespace(engine=rec, micro_caps=None))           # noqa: SLF001
    assert tc["pairbtc"] is cond.rprm and tc["pairlock"] is None
    assert tr["pairlock"] is rec.rprm and tr["pairbtc"] is None
    cfg = SimpleNamespace(engine=None, risk=None)
    assert current_values(cfg, None, **tc)["pairbtc.pair_sum"] == pytest.approx(0.97)
    assert "pairbtc.pair_sum" not in current_values(cfg, None, **tr)
    vals = {"pairbtc.pair_sum": TUNABLES["pairbtc.pair_sum"].coerce(0.95)[0], "pairbtc.late_shares": 25.0}
    apply_to(vals, cfg, None, **tc)
    apply_to(vals, cfg, None, **tr)                                                         # the recovery bot ignores them
    assert cond.rprm.pair_sum == pytest.approx(0.95) and cond.rprm.late_shares == 25.0
    assert rec.rprm.pair_sum == pytest.approx(0.97) and rec.rprm.late_shares == 10.0


def test_live_places_the_hedges_the_engine_asks_for():
    from troll_poly_bot.live import LiveBot
    from troll_poly_bot.types import Order
    o = Order(token_id="DOWN", side=Side.BUY, price=0.20, size=30.0, tif=TimeInForce.GTC, tag="hedge")
    lm = SimpleNamespace(meta=SimpleNamespace(market=_meta("BTC").market))
    placed = []
    eng = SimpleNamespace(pair_cancels=lambda now: [], pair_requests=lambda now, markets: [(SLUG, "DOWN", o)] if SLUG in markets else [])
    bot = SimpleNamespace(engine=eng, markets={SLUG: lm}, _pairs_open={}, _persist_positions=lambda: None,
                          _place_pair=lambda lm_, side, order, entry_id, now, parent="": placed.append((side, order, entry_id)))
    LiveBot._engine_pair_cancels(bot, NOW)                                                  # noqa: SLF001
    assert placed == [("DOWN", o, f"engine-{SLUG}")]


def _pair_fill(e, po, t, price=None):
    f = Fill(po.order_id, po.token_id, Side.BUY, price if price is not None else po.price, po.size, 0.0, t, t, t,
             expected_price=po.price)
    e.on_pair_done(po.order_id, filled=True)                         # live.py: the result first, then its fills
    e.on_pair_fill_record(SLUG, po, f, t)


def test_once_every_pair_filled_it_buys_again_under_the_conditions():
    e = _ceng(cond_max_entries=3)
    m = _meta("BTC").market
    t = T0 + 20_000.0
    _buy(e, t, price=0.55)                                           # 20 UP
    side, po = e.pair_order(m, "UP", 20.0, 0.55, t + 300.0)
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    cyc = e.cycles[SLUG]
    assert not _ev(e, t + 2000.0, (0.54, 0.55), (0.45, 0.46)).tradeable          # the pair bid still works: no buy
    _pair_fill(e, po, t + 3000.0)                                     # 20 DOWN at 0.42: all paired
    assert rc.fully_paired(cyc) and cyc.book.down_qty == 20.0
    assert not _ev(e, t + 4000.0, (0.61, 0.62), (0.38, 0.39)).tradeable          # paired, but 0.62 > 0.60
    assert not _ev(e, t + 4500.0, (0.54, 0.55), (0.45, 0.46), btc=106.0).tradeable   # BTC 6 over the strike
    ev2, o2 = _buy(e, t + 5000.0, up=(0.45, 0.46), down=(0.55, 0.56), price=0.56)   # DOWN the higher token now
    assert ev2.side == "DOWN" and "add-on #2" in ev2.size_note and cyc.a_side == "DOWN"
    assert cyc.book.up_qty == 20.0 and cyc.book.down_qty == 40.0 and e.stats["cond_addons"] == 1
    side2, po2 = e.pair_order(m, "DOWN", 20.0, 0.56, t + 5300.0)
    assert side2 == "UP" and po2.price == pytest.approx(0.41) and po2.size == 20.0       # its own pair: 0.97 - 0.56
    e.on_pair_submit(po2.order_id, SLUG, side2, po2.price, po2.size)
    assert cyc.pair["filled"] == 0.0 and cyc.pair["state"] == "working"
    assert not _ev(e, t + 6000.0, (0.45, 0.46), (0.55, 0.56)).tradeable          # unpaired again: no buy
    _pair_fill(e, po2, t + 7000.0)
    assert rc.fully_paired(cyc) and cyc.book.up_qty == 40.0
    _buy(e, t + 8000.0, price=0.55)                                    # the 3rd buy
    side3, po3 = e.pair_order(m, "UP", 20.0, 0.55, t + 8300.0)
    e.on_pair_submit(po3.order_id, SLUG, side3, po3.price, po3.size)
    _pair_fill(e, po3, t + 9000.0)
    assert rc.entry_count(cyc) == 3 and not _ev(e, t + 10_000.0, (0.54, 0.55), (0.45, 0.46)).tradeable   # COND_MAX_ENTRIES
    # settlement: three locked pairs, reconciled
    e.observe_point(SLUG, m, {"t": m.close_ts + 1000.0, "spot": 100.0}, m.close_ts + 1000.0)
    pnl = cyc.book.resolution_pnl("UP")
    assert pnl == pytest.approx(cyc.book.resolution_pnl("DOWN")) and pnl > 0
    e.on_market_settled(SLUG, True, pnl, m.close_ts + 40_000.0)
    assert not e.accounting_halt and e.stats["pairs_filled"] == 3


def test_a_killed_add_on_leaves_the_paired_position_and_one_entry_is_the_default():
    e = _ceng(cond_max_entries=2)
    m = _meta("BTC").market
    t = T0 + 20_000.0
    _buy(e, t, price=0.55)
    side, po = e.pair_order(m, "UP", 20.0, 0.55, t + 300.0)
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    _pair_fill(e, po, t + 1000.0)
    ev = _ev(e, t + 2000.0, (0.54, 0.55), (0.45, 0.46))
    assert ev.tradeable
    o = e.to_order(ev, t + 2000.0)
    e.entry_result(o.order_id, False, "killed")
    cyc = e.cycles[SLUG]
    assert cyc.state is rc.RState.HOLDING and rc.fully_paired(cyc) and cyc.book.up_qty == 20.0 and SLUG not in e.pending
    assert _ev(e, t + 3000.0, (0.54, 0.55), (0.45, 0.46)).tradeable               # tries again
    # the default (cond_max_entries 1) never adds
    e1 = _ceng()
    _buy(e1, t, price=0.55)
    s1, p1 = e1.pair_order(m, "UP", 20.0, 0.55, t + 300.0)
    e1.on_pair_submit(p1.order_id, SLUG, s1, p1.price, p1.size)
    _pair_fill(e1, p1, t + 1000.0)
    assert not _ev(e1, t + 2000.0, (0.54, 0.55), (0.45, 0.46)).tradeable


def test_with_recovery_on_an_unfilled_bid_is_pulled_and_the_loss_recovered():
    """The user, 10-03 01:55: the recovery of tpb-pairlockrecovery-paper in this bot (pair_pull + recovery_enabled)."""
    import math
    from test_pairlock_recovery_engine import _feat
    from troll_poly_bot.strategy.pairlock_book import fee_per_share
    e = _ceng(pair_pull=True, recovery_enabled=True, direction="price", cond_max_entries=10, max_order_size=60.0, max_capital=90.0)
    m = _meta("BTC").market
    t = T0 + 20_000.0
    _buy(e, t, price=0.55)                                           # 20 UP
    side, po = e.pair_order(m, "UP", 20.0, 0.55, t + 300.0)
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    e.plmodel = _model(0.0)
    e._features = lambda meta, now: _feat(0.30)                     # noqa: SLF001  P(UP) 0.30
    cyc = e.cycles[SLUG]
    assert not _ev(e, t + 5000.0, (0.50, 0.51), (0.48, 0.49)).tradeable and e.pair_cancels(t + 5000.0) == []   # UP still higher
    t1 = t + 20_000.0
    assert not _ev(e, t1, (0.40, 0.41), (0.59, 0.60)).tradeable       # DOWN now higher; the bid still works this tick
    assert e.pair_cancels(t1) == [po.order_id] and cyc.pair["state"] == "cancelled"
    e.on_pair_done(po.order_id, filled=False)
    ev = _ev(e, t1 + 300.0, (0.40, 0.41), (0.59, 0.60))
    assert ev.tradeable and ev.features["triangle"]["trigger"] == "recovery" and ev.side == "DOWN", (ev.reason, ev.detail)
    x = math.ceil((0.5 + cyc.book.total_cost) / (1 - 0.60 - fee_per_share(0.60) - 0.005) * 100) / 100
    assert ev.size == pytest.approx(x)
    o = e.to_order(ev, t1 + 300.0)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.60, ev.size, t1 + 600.0)
    out = rc.outcomes(cyc.book)
    assert out["pnl_down_win"] >= 0.5 - 1e-6                         # DOWN winning now makes the target, not -11
    assert not e._can_add(cyc)                                        # noqa: SLF001  no add-on after a pulled bid


# ───────────────────────────── 2026-10-08: buy only a RISING token ─────────────────────────────

def test_the_conditions_rule_buys_only_a_rising_token():
    from dataclasses import replace as _rep
    from test_pairlock_dynamic_recovery import mv as _mv
    from troll_poly_bot.strategy import pairlock_recovery as _rc
    prm = _rep(_rc.RecoveryParams(entry_mode="conditions"), cond_rising_window_s=3.0, cond_rising_min=0.0)
    view = _mv(0.54, 0.55, 0.44, 0.45)                       # UP is the higher token at 0.55
    args = (view, prm, 200.0, 100_002.0, 100_000.0, 0.0)
    assert _rc.conditions_entry_side(*args, {"UP": 0.02, "DOWN": -0.02})[0] == "UP"           # rising: bought
    for r, why in ((-0.01, "not rising"), (0.0, "not rising"), (None, "no 3 s price history")):
        side, msg = _rc.conditions_entry_side(*args, {"UP": r, "DOWN": 0.0})
        assert side is None and why in msg, msg                                               # falling / flat / unknown
    assert _rc.conditions_entry_side(*args, {"UP": 0.004, "DOWN": 0.0})[0] == "UP"
    assert _rc.conditions_entry_side(view, _rep(prm, cond_rising_min=0.005), 200.0, 100_002.0, 100_000.0, 0.0,
                                     {"UP": 0.004})[0] is None
    assert _rc.conditions_entry_side(view, _rep(prm, cond_rising_window_s=0.0), 200.0, 100_002.0, 100_000.0, 0.0)[0] == "UP"


def test_the_btc_bot_config_has_the_rising_rule_and_no_late_buy():
    import json
    import subprocess
    env = json.loads(subprocess.check_output(["node", "-e", "console.log(JSON.stringify(require('./ecosystem.pairlock.config.js')"
                                              ".apps.find(a => a.name === 'tpb-pairlock-paper').env))"], cwd="/root/troll-poly-bot"))
    from troll_poly_bot.strategy.pairlock_recovery_engine import recovery_params_from_env
    p = recovery_params_from_env(env)
    assert p.entry_mode == "conditions" and p.cond_rising_window_s == 3.0 and p.late_enabled is False
    assert (p.cond_rising_long_s, p.cond_rising_last_s, p.cond_rising_spot, p.cond_high_min, p.cond_high_max) == (10.0, 1.0, True, 0.85, 0.92)
    assert (p.ulr_enabled, p.recovery_enabled, p.pair_pull, p.pair_rescue_price) == (False, False, False, 0.0)
    assert p.cond_buy_lower and not p.cond_high_buy_lower and p.cond_high_pair_bid and p.cond_high_max_entries == 5


# ───────────────────────────── 2026-10-08: the bid rests; above 0.75 the unpaired shares are bought ─────────────────────────────

def test_the_bid_rests_through_a_flip_and_the_rescue_buys_the_unpaired_shares_above_075():
    e = _ceng(pair_pull=False, recovery_enabled=False, pair_rescue_price=0.75, late_enabled=False)
    m = _meta("BTC").market
    t = T0 + 20_000.0
    _buy(e, t, price=0.55, size=20.0)                                 # 20 UP
    side, po = e.pair_order(m, "UP", 20.0, 0.55, t + 300.0)
    assert side == "DOWN" and po.price == pytest.approx(0.42)
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    cyc = e.cycles[SLUG]
    # DOWN turns the higher price but stays under 0.75: the bid keeps resting, nothing is bought
    assert not _ev(e, t + 5000.0, (0.40, 0.41), (0.59, 0.60)).tradeable and e.pair_cancels(t + 5000.0) == []
    assert cyc.pair["state"] == "working"
    # DOWN above 0.75: the bid is pulled, then the 20 unpaired shares of DOWN are bought
    t1 = t + 30_000.0
    assert not _ev(e, t1, (0.21, 0.22), (0.77, 0.78)).tradeable       # the bid still works this tick
    assert e.pair_cancels(t1) == [po.order_id] and cyc.pair["state"] == "cancelled"
    e.on_pair_done(po.order_id, filled=False)
    ev = _ev(e, t1 + 300.0, (0.21, 0.22), (0.77, 0.78))
    assert ev.tradeable and ev.side == "DOWN" and ev.size == 20.0, (ev.reason, ev.detail)
    o = e.to_order(ev, t1 + 300.0)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.78, 20.0, t1 + 600.0)
    assert cyc.book.up_qty == cyc.book.down_qty == 20.0 and e.stats["lock_sent"] == 1
    assert not _ev(e, t1 + 5000.0, (0.21, 0.22), (0.77, 0.78)).tradeable   # paired: nothing more


def test_a_partly_filled_bid_rescues_only_what_is_still_unpaired():
    e = _ceng(pair_pull=False, recovery_enabled=False, pair_rescue_price=0.75, late_enabled=False)
    m = _meta("BTC").market
    t = T0 + 20_000.0
    _buy(e, t, price=0.55, size=20.0)
    side, po = e.pair_order(m, "UP", 20.0, 0.55, t + 300.0)
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    f = Fill(po.order_id, po.token_id, Side.BUY, po.price, 10.0, 0.0, t + 2000.0, t + 2000.0, t + 2000.0, expected_price=po.price)
    e.on_pair_fill_record(SLUG, po, f, t + 2000.0)                   # the bid fills 10 of 20 and keeps working
    cyc = e.cycles[SLUG]
    assert cyc.pair["state"] == "working" and cyc.book.down_qty == 10.0
    unpaired = cyc.book.up_qty - cyc.book.down_qty
    t1 = t + 30_000.0
    _ev(e, t1, (0.21, 0.22), (0.77, 0.78))
    e.pair_cancels(t1)
    e.on_pair_done(po.order_id, filled=False)
    ev = _ev(e, t1 + 300.0, (0.21, 0.22), (0.77, 0.78))
    assert unpaired == 10.0 and ev.tradeable and ev.side == "DOWN" and ev.size == pytest.approx(unpaired), (ev.reason, ev.detail)


# ───────────────────────────── 2026-10-08 (2): stricter rising, the high band, strikes kept over a restart ─────────────────────────────

def _strict(**kw):
    from dataclasses import replace as _rep
    return _rep(rc.RecoveryParams(entry_mode="conditions"), cond_rising_window_s=3.0, cond_rising_long_s=10.0,
                cond_rising_last_s=1.0, cond_rising_spot=True, **kw)


def test_the_rising_check_needs_short_long_last_and_btc():
    up = {"short": 0.02, "long": 0.04, "last": 0.0}
    assert rc.rising_check("UP", {"UP": up}, +3.0, _strict()) == ("", ", rising +0.020/3s +0.040/10s +0.000/1s BTC +3.00")
    for r, spot, why in (({**up, "long": -0.01}, 3.0, "not rising"),          # up 3 s but down over 10 s: the user's case
                         ({**up, "short": 0.0}, 3.0, "not rising"),
                         ({**up, "last": -0.01}, 3.0, "is falling"),            # turning down right now
                         (up, -1.0, "not moving UP's way"),                     # BTC falling under an UP buy
                         (up, None, "no 10 s BTC history"),
                         ({**up, "long": None}, 3.0, "no 10 s price history")):
        bad, _ = rc.rising_check("UP", {"UP": r}, spot, _strict())
        assert why in bad, (r, spot, bad)
    assert rc.rising_check("DOWN", {"DOWN": up}, -2.0, _strict())[0] == ""      # DOWN wants BTC falling


def test_the_high_band_buys_the_rising_leader_between_085_and_092():
    from test_pairlock_dynamic_recovery import mv as _mv
    prm = _strict(cond_high_min=0.85, cond_high_max=0.92)
    ok = {"UP": {"short": 0.01, "long": 0.03, "last": 0.0}}
    assert rc.conditions_high_side(_mv(0.87, 0.88, 0.12, 0.13), prm, 200.0, ok, 5.0)[0] == "UP"
    for ask in (0.85, 0.92, 0.84, 0.93):                                       # strictly inside the band
        assert rc.conditions_high_side(_mv(ask - 0.01, ask, round(1 - ask - 0.01, 2), round(1 - ask, 2)), prm, 200.0, ok, 5.0)[0] is None
    assert rc.conditions_high_side(_mv(0.87, 0.88, 0.12, 0.13), prm, 100.0, ok, 5.0)[0] is None        # < 120 s left
    assert rc.conditions_high_side(_mv(0.87, 0.88, 0.12, 0.13), prm, 200.0, ok, -5.0)[0] is None       # BTC against it
    assert rc.conditions_high_side(_mv(0.87, 0.88, 0.12, 0.13), _strict(), 200.0, ok, 5.0)[1] == "high band off"


def test_the_high_band_repeats_after_each_fill_without_a_pair_bid():
    e = _ceng(pair_pull=False, recovery_enabled=False, late_enabled=False, cond_high_min=0.85, cond_high_max=0.92,
              cond_high_max_entries=3, open_shares=10.0, max_total_position=500.0)
    e._rise = lambda m, now: ({"UP": {"short": 0.01}, "DOWN": {"short": -0.01}}, None)    # noqa: SLF001  rising UP
    e.rprm.cond_rising_window_s = 3.0
    m = _meta("BTC").market
    t = T0 + 30_000.0
    for k in range(3):
        ev = _ev(e, t, (0.87, 0.88), (0.12, 0.13), btc=150.0)                   # BTC far over the strike: the band ignores it
        assert ev.tradeable and ev.side == "UP" and ev.size == 10.0 and "high band buy #%d" % (k + 1) in ev.size_note, (ev.reason, ev.detail)
        o = e.to_order(ev, t)
        assert not _ev(e, t + 100.0, (0.87, 0.88), (0.12, 0.13), btc=150.0).tradeable   # waits for this one to fill
        e.entry_result(o.order_id, True)
        _fill(e, o, 0.88, 10.0, t + 300.0)
        assert e.pair_order(m, "UP", 10.0, 0.88, t + 400.0) is None             # no other-side bid
        t += 5000.0
    assert e.cycles[SLUG].book.up_qty == 30.0
    assert not _ev(e, t, (0.87, 0.88), (0.12, 0.13), btc=150.0).tradeable       # 3 per market


def test_a_strike_survives_a_restart(tmp_path):
    from troll_poly_bot.feeds.markets import StrikeTracker
    meta = _meta("BTC")
    a = StrikeTracker(tolerance_ms=3000.0)
    a.attach_store(tmp_path / "strikes.x.json")
    a.register(meta)
    a.on_price("BTC", 101.25, T0 + 500.0)
    assert a.poll(T0 + 1000.0) and meta.market.strike == 101.25
    meta2 = _meta("BTC")                                                       # a new process, 2 minutes into the market
    meta2.market.strike = 0.0
    b = StrikeTracker(tolerance_ms=3000.0)
    b.attach_store(tmp_path / "strikes.x.json")
    b.register(meta2)
    b.on_price("BTC", 99.0, T0 + 120_000.0)                                   # the live price now is not the strike
    assert b.poll(T0 + 120_500.0) and meta2.market.strike == 101.25 and meta2.has_strike
    c = StrikeTracker(tolerance_ms=3000.0)                                     # without a saved strike: skipped as before
    c.register(_meta("BTC"))
    c.on_price("BTC", 99.0, T0 + 120_000.0)
    assert c.poll(T0 + 120_500.0) == []



# ───────────────────────────── 2026-10-08 (3): buy the LOWER token ─────────────────────────────

def test_the_entry_buys_the_lower_token_when_asked():
    from dataclasses import replace as _rep
    from test_pairlock_dynamic_recovery import mv as _mv
    base = _rep(rc.RecoveryParams(entry_mode="conditions"), cond_rising_window_s=3.0)
    view = _mv(0.54, 0.55, 0.44, 0.45)                                  # UP the higher token at 0.55, DOWN at 0.45
    rise = {"UP": {"short": -0.02}, "DOWN": {"short": 0.02}}            # DOWN rising, UP falling
    args = (view, base, 200.0, 100_002.0, 100_000.0, 0.0, rise)
    assert rc.conditions_entry_side(*args)[0] is None                    # higher token bought: UP is falling
    side, why = rc.conditions_entry_side(view, _rep(base, cond_buy_lower=True), 200.0, 100_002.0, 100_000.0, 0.0, rise)
    assert side == "DOWN" and "LOWER token DOWN at 0.45" in why
    hv = _mv(0.87, 0.88, 0.12, 0.13)
    hp = _rep(base, cond_high_min=0.85, cond_high_max=0.92, cond_high_buy_lower=True)
    assert rc.conditions_high_side(hv, hp, 200.0, rise)[0] == "DOWN"
    assert rc.conditions_high_side(_mv(0.12, 0.13, 0.87, 0.88), hp, 200.0, rise)[0] is None   # UP the lower one, falling


def test_through_the_engine_the_lower_token_is_bought_and_its_bid_is_for_the_higher():
    e = _ceng(pair_pull=False, recovery_enabled=False, late_enabled=False, cond_buy_lower=True, open_shares=10.0)
    m = _meta("BTC").market
    ev, o = _buy(e, T0 + 30_000.0, price=0.46, size=10.0)
    assert ev.side == "DOWN" and ev.size == 10.0
    side, po = e.pair_order(m, "DOWN", 10.0, 0.46, T0 + 30_400.0)
    assert side == "UP" and po.price == pytest.approx(0.51)             # 0.97 - 0.46
    assert e.pair_cancels(T0 + 31_000.0) == []                          # the bid rests (no pull, no rescue)



def test_a_high_band_buy_gets_its_opposite_bid_and_the_repeat_waits_for_it():
    e = _ceng(pair_pull=False, recovery_enabled=False, late_enabled=False, cond_high_min=0.85, cond_high_max=0.92,
              cond_high_max_entries=3, cond_high_pair_bid=True, pair_sum=0.95, open_shares=10.0, max_total_position=500.0)
    e._rise = lambda m, now: ({"UP": {"short": 0.01}, "DOWN": {"short": -0.01}}, None)    # noqa: SLF001  rising UP
    e.rprm.cond_rising_window_s = 3.0
    m = _meta("BTC").market
    t = T0 + 30_000.0
    ev = _ev(e, t, (0.87, 0.88), (0.12, 0.13), btc=150.0)
    assert ev.tradeable and ev.side == "UP" and "then a DOWN bid" in ev.size_note, (ev.reason, ev.detail)
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.88, 10.0, t + 300.0)
    side, po = e.pair_order(m, "UP", 10.0, 0.88, t + 400.0)
    assert side == "DOWN" and po.price == pytest.approx(0.07) and po.size == 10.0      # 0.95 - 0.88
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    assert not _ev(e, t + 5000.0, (0.87, 0.88), (0.12, 0.13), btc=150.0).tradeable    # the bid works: no repeat yet
    _pair_fill(e, po, t + 6000.0)
    ev = _ev(e, t + 9000.0, (0.87, 0.88), (0.12, 0.13), btc=150.0)
    assert ev.tradeable and "high band buy #2" in ev.size_note                         # filled: the repeat goes



def test_market_open_buy_takes_the_higher_token_in_the_first_20_s():
    from test_pairlock_dynamic_recovery import mv as _mv
    prm = rc.RecoveryParams(entry_mode="conditions")
    assert prm.cond_open_buy and (prm.cond_open_window_s, prm.cond_open_min, prm.cond_open_max) == (20.0, 0.50, 0.70)
    assert rc.conditions_open_side(_mv(0.64, 0.65, 0.35, 0.36), prm, 12.0)[0] == "UP"
    assert rc.conditions_open_side(_mv(0.34, 0.35, 0.64, 0.65), prm, 12.0)[0] == "DOWN"
    assert rc.conditions_open_side(_mv(0.64, 0.65, 0.35, 0.36), prm, 21.0)[0] is None          # after 20 s
    assert rc.conditions_open_side(_mv(0.71, 0.72, 0.28, 0.29), prm, 12.0)[0] is None          # 0.72 > 0.70
    assert rc.conditions_open_side(_mv(0.48, 0.49, 0.50, 0.51), prm, 12.0)[0] == "DOWN"        # 0.51 >= 0.50
    e = _ceng(cond_open_buy=True, pair_pull=False, recovery_enabled=False, late_enabled=False, open_shares=10.0)
    t = T0 + 10_000.0
    ev = _ev(e, t, (0.64, 0.65), (0.35, 0.36), btc=150.0)                   # BTC far from the band: the open buy ignores it
    assert ev.tradeable and ev.side == "UP" and ev.size == 10.0 and "market open buy" in ev.size_note, (ev.reason, ev.detail)
    o = e.to_order(ev, t)
    e.entry_result(o.order_id, True)
    _fill(e, o, 0.65, 10.0, t + 300.0)
    side, po = e.pair_order(_meta("BTC").market, "UP", 10.0, 0.65, t + 400.0)
    assert side == "DOWN" and po.price == pytest.approx(0.32)                # its opposite bid (0.97 - 0.65)
    assert not _ev(e, t + 5000.0, (0.64, 0.65), (0.35, 0.36), btc=150.0).tradeable   # once: the market holds tokens



def test_the_after_open_and_high_price_modes_switch_their_rules_off():
    t = T0 + 30_000.0
    on = _ceng(pair_pull=False, recovery_enabled=False, late_enabled=False)
    assert _ev(on, t, (0.54, 0.55), (0.45, 0.46)).tradeable                            # the regular entry
    off = _ceng(pair_pull=False, recovery_enabled=False, late_enabled=False, cond_after_buy=False)
    assert not _ev(off, t, (0.54, 0.55), (0.45, 0.46)).tradeable
    kw = dict(pair_pull=False, recovery_enabled=False, late_enabled=False, cond_high_min=0.85, cond_high_max=0.92)
    hi = _ceng(**kw)
    assert _ev(hi, t, (0.87, 0.88), (0.12, 0.13), btc=150.0).tradeable                  # the high band
    hoff = _ceng(**kw, cond_high_buy=False)
    assert not _ev(hoff, t, (0.87, 0.88), (0.12, 0.13), btc=150.0).tradeable



def test_the_after_open_buy_waits_its_seconds():
    e = _ceng(pair_pull=False, recovery_enabled=False, late_enabled=False, cond_after_delay_s=45.0)
    assert not _ev(e, T0 + 30_000.0, (0.54, 0.55), (0.45, 0.46)).tradeable            # 30 s < 45 s
    assert _ev(e, T0 + 50_000.0, (0.54, 0.55), (0.45, 0.46)).tradeable                # 50 s



# ───────────────────────────── 2026-10-08: the opposite bid is a MAKER order ─────────────────────────────

def test_the_opposite_order_rests_as_a_maker_or_buys_at_once_when_the_ask_is_already_cheap_enough():
    e = _ceng(pair_pull=False, recovery_enabled=False, late_enabled=False, pair_sum=0.95)
    m = _meta("BTC").market
    _buy(e, T0 + 30_000.0, price=0.55)
    side, po = e.pair_order(m, "UP", 20.0, 0.55, T0 + 30_400.0, other_ask=0.46)
    assert side == "DOWN" and po.price == pytest.approx(0.40) and po.post_only and po.tif is TimeInForce.GTC   # 0.95 - 0.55
    e2 = _ceng(pair_pull=False, recovery_enabled=False, late_enabled=False, pair_sum=0.95)
    _buy(e2, T0 + 30_000.0, price=0.55)
    side, po = e2.pair_order(m, "UP", 20.0, 0.55, T0 + 30_400.0, other_ask=0.38)       # the ask is UNDER the 0.40 wanted
    assert po.tif is TimeInForce.IOC and po.price == pytest.approx(0.38) and not po.post_only   # bought now, AT the 0.38 ask
    e3 = _ceng(pair_pull=False, recovery_enabled=False, late_enabled=False, pair_sum=0.95)
    _buy(e3, T0 + 30_000.0, price=0.55)
    _, po = e3.pair_order(m, "UP", 20.0, 0.55, T0 + 30_400.0, other_ask=0.40)          # at the price: taken too
    assert po.tif is TimeInForce.IOC


def test_the_paper_venue_never_fills_a_post_only_order_as_a_taker():
    from troll_poly_bot.execution.latency import INSTANT, LatencyModel
    from troll_poly_bot.execution.paper import PaperExchange, RejectReason
    from troll_poly_bot.types import BookLevel, Order, OrderBook
    ex = PaperExchange(LatencyModel(INSTANT, seed=1), starting_balance=100.0)
    book = OrderBook("DOWN", bids=[BookLevel(0.36, 100.0)], asks=[BookLevel(0.38, 100.0)])
    results = []
    for price in (0.40, 0.37):                                                       # crossing, resting
        o = Order(token_id="DOWN", side=Side.BUY, price=price, size=10.0, tif=TimeInForce.GTC, post_only=True)
        ex.submit(o, 0.0)
        results.append(o)
    out = []
    for t in range(0, 3000, 50):
        out += ex.step(float(t), lambda tok: book)
    by = {r.order.order_id: r for r in out}
    cross, rest = results
    assert by[cross.order_id].reject_reason is RejectReason.POST_ONLY_CROSS and not by[cross.order_id].fills
    assert rest.order_id in ex._resting                                              # noqa: SLF001  resting, a maker
    assert not (by.get(rest.order_id) and by[rest.order_id].fills)                  # and not filled on arrival



def test_the_first_buy_waits_10_s_after_the_open():
    e = _ceng(cond_open_buy=True, cond_first_buy_delay_s=10.0, pair_pull=False, recovery_enabled=False, late_enabled=False)
    assert not _ev(e, T0 + 6_000.0, (0.64, 0.65), (0.35, 0.36), btc=150.0).tradeable       # 6 s: too early
    ev = _ev(e, T0 + 11_000.0, (0.64, 0.65), (0.35, 0.36), btc=150.0)
    assert ev.tradeable and "market open buy" in ev.size_note                             # 11 s: the open buy


# ───────────────────────────── 2026-10-08 (3): the trailing opposite bid ─────────────────────────────

def _placed(e, t, **kw):
    m = _meta("BTC").market
    _buy(e, t, price=0.55, size=20.0)                                 # 20 UP
    side, po = e.pair_order(m, "UP", 20.0, 0.55, t + 300.0)
    e.on_pair_submit(po.order_id, SLUG, side, po.price, po.size)
    return m, side, po


def test_the_opposite_bid_is_re_placed_lower_by_the_fall_for_the_unpaired_shares():
    e = _ceng(pair_reprice_drop=0.05, pair_reprice_max=2, late_enabled=False)
    t = T0 + 20_000.0
    m, side, po = _placed(e, t)
    cyc = e.cycles[SLUG]
    assert side == "DOWN" and po.price == pytest.approx(0.42) and po.post_only
    assert cyc.pair["ref"] == pytest.approx(0.455)                     # DOWN mid when the bid was placed
    _ev(e, t + 2000.0, (0.56, 0.57), (0.42, 0.43))                     # DOWN mid 0.425: fell 0.03 < 0.05
    assert e.pair_cancels(t + 2000.0) == [] and cyc.pair["state"] == "working"
    _ev(e, t + 4000.0, (0.60, 0.61), (0.38, 0.39))                     # DOWN mid 0.385: fell 0.07
    assert e.pair_cancels(t + 4000.0) == [po.order_id] and cyc.pair["state"] == "repricing"
    e.on_pair_done(po.order_id, filled=False)                          # live.py reports the pull: the pair goes on
    reqs = e.pair_requests(t + 4000.0, {SLUG: m})
    assert len(reqs) == 1
    slug, s2, o2 = reqs[0]
    assert (slug, s2) == (SLUG, "DOWN") and o2.price == pytest.approx(0.35) and o2.size == 20.0
    assert o2.tif is TimeInForce.GTC and o2.post_only and o2.token_id == m.no_token_id
    e.on_pair_submit(o2.order_id, SLUG, s2, o2.price, o2.size)
    assert cyc.pair["oid"] == o2.order_id and cyc.pair["state"] == "working" and cyc.pair["reprices"] == 1
    assert cyc.pair["ref"] == pytest.approx(0.385) and e.stats["pairs_repriced"] == 1 and e.stats["pairs_placed"] == 1
    assert e.pair_requests(t + 4200.0, {SLUG: m}) == []                # once
    # it fills: the pair is complete, UP = DOWN
    _pair_fill(e, o2, t + 6000.0)
    assert cyc.pair["state"] == "filled" and cyc.book.up_qty == cyc.book.down_qty == 20.0


def test_a_partly_filled_bid_is_re_placed_for_the_rest_and_the_count_is_capped():
    e = _ceng(pair_reprice_drop=0.05, pair_reprice_max=1, late_enabled=False)
    t = T0 + 20_000.0
    m, side, po = _placed(e, t)
    cyc = e.cycles[SLUG]
    f = Fill(po.order_id, po.token_id, Side.BUY, po.price, 8.0, 0.0, t + 1000.0, t + 1000.0, t + 1000.0, expected_price=po.price)
    e.on_pair_fill_record(SLUG, po, f, t + 1000.0)                    # 8 of 20 fill
    _ev(e, t + 4000.0, (0.60, 0.61), (0.38, 0.39))
    assert e.pair_cancels(t + 4000.0) == [po.order_id]
    e.on_pair_done(po.order_id, filled=False)
    assert cyc.pair["state"] == "repricing"                            # a partial fill does not end the pair
    (_, s2, o2), = e.pair_requests(t + 4000.0, {SLUG: m})
    assert o2.size == 12.0 and o2.price == pytest.approx(0.35)
    e.on_pair_submit(o2.order_id, SLUG, s2, o2.price, o2.size)
    assert cyc.pair["size"] == 20.0 and cyc.pair["filled"] == 8.0      # the target stays the first leg's 20
    _ev(e, t + 8000.0, (0.70, 0.71), (0.28, 0.29))                     # fell another 0.10: the cap (1) is reached
    assert e.pair_cancels(t + 8000.0) == [] and cyc.pair["state"] == "working"


def test_the_ask_already_under_the_new_price_is_bought_at_once():
    e = _ceng(pair_reprice_drop=0.05, late_enabled=False)
    t = T0 + 20_000.0
    m, side, po = _placed(e, t)
    _ev(e, t + 4000.0, (0.64, 0.65), (0.33, 0.34))                     # DOWN mid 0.335: fell 0.12 -> 0.30, ask 0.34 > 0.30
    e.pair_cancels(t + 4000.0)
    e.on_pair_done(po.order_id, filled=False)
    (_, _, o2), = e.pair_requests(t + 4000.0, {SLUG: m})
    assert o2.price == pytest.approx(0.30) and o2.tif is TimeInForce.GTC
    e2 = _ceng(pair_reprice_drop=0.05, late_enabled=False)
    m, side, po = _placed(e2, t)
    _ev(e2, t + 4000.0, (0.60, 0.61), (0.36, 0.37))                    # mid 0.365: fell 0.09 -> 0.33
    e2.pair_cancels(t + 4000.0)
    e2.on_pair_done(po.order_id, filled=False)
    e2.quotes[SLUG] = (0.60, 0.61, 0.32, 0.33)                         # the ask dropped to 0.33 meanwhile
    (_, _, o3), = e2.pair_requests(t + 4000.0, {SLUG: m})
    assert o3.tif is TimeInForce.IOC and o3.price == pytest.approx(0.33) and not o3.post_only


def test_off_by_default_and_a_pulled_bid_that_filled_anyway_shrinks_the_new_one():
    e = _ceng(late_enabled=False)                                      # pair_reprice_drop 0: the bid just rests
    t = T0 + 20_000.0
    m, side, po = _placed(e, t)
    _ev(e, t + 4000.0, (0.70, 0.71), (0.28, 0.29))
    assert e.pair_cancels(t + 4000.0) == [] and e.cycles[SLUG].pair["state"] == "working"
    e = _ceng(pair_reprice_drop=0.05, late_enabled=False)
    m, side, po = _placed(e, t)
    cyc = e.cycles[SLUG]
    _ev(e, t + 4000.0, (0.60, 0.61), (0.38, 0.39))
    e.pair_cancels(t + 4000.0)
    e.on_pair_done(po.order_id, filled=False)
    (_, s2, o2), = e.pair_requests(t + 4000.0, {SLUG: m})
    e.on_pair_submit(o2.order_id, SLUG, s2, o2.price, o2.size)
    # the venue's fill of the pulled bid wins the race: 5 of its shares
    f = Fill(po.order_id, po.token_id, Side.BUY, po.price, 5.0, 0.0, t + 4100.0, t + 4100.0, t + 4100.0, expected_price=po.price)
    e.on_pair_fill_record(SLUG, po, f, t + 4100.0)
    assert e.pair_cancels(t + 4200.0) == [o2.order_id]                 # the 20-share bid is too big now
    e.on_pair_done(o2.order_id, filled=False)
    (_, _, o3), = e.pair_requests(t + 4200.0, {SLUG: m})
    assert o3.size == 15.0 and o3.price == pytest.approx(o2.price)


# ───────────────────────────── 2026-10-08 (4): with "buy the LOWER token", the open buy takes the rising lower token ─────────────────────────────

def test_open_buy_with_buy_lower_takes_the_lower_token_only_while_it_rises():
    from test_pairlock_dynamic_recovery import mv as _mv
    prm = rc.RecoveryParams(entry_mode="conditions", cond_buy_lower=True, cond_rising_window_s=3.0)
    book = _mv(0.64, 0.65, 0.35, 0.36)                                         # UP the higher token, in 0.50-0.70
    up = {"UP": {"short": -0.02}, "DOWN": {"short": 0.02}}
    side, why = rc.conditions_open_side(book, prm, 12.0, up, None)
    assert side == "DOWN" and "LOWER token DOWN" in why
    falling = {"UP": {"short": 0.02}, "DOWN": {"short": -0.02}}
    side, why = rc.conditions_open_side(book, prm, 12.0, falling, None)
    assert side is None and "not rising" in why
    flat = {"UP": {"short": 0.0}, "DOWN": {"short": 0.0}}
    assert rc.conditions_open_side(book, prm, 12.0, flat, None)[0] is None
    assert rc.conditions_open_side(book, prm, 12.0, None, None)[0] is None             # no history yet: no buy
    assert rc.conditions_open_side(_mv(0.71, 0.72, 0.28, 0.29), prm, 12.0, up, None)[0] is None   # band on the higher token
    off = rc.RecoveryParams(entry_mode="conditions", cond_buy_lower=False, cond_rising_window_s=3.0)
    assert rc.conditions_open_side(book, off, 12.0, falling, None)[0] == "UP"          # off: the higher token, rising
    assert rc.conditions_open_side(book, off, 12.0, up, None)[0] is None               # 2026-10-09: ... never a falling one
    assert rc.conditions_open_side(book, off, 12.0, flat, None)[0] is None


def test_the_engine_open_buy_buys_the_rising_lower_token():
    kw = dict(cond_open_buy=True, cond_buy_lower=True, cond_rising_window_s=1.0, pair_pull=False, recovery_enabled=False,
              late_enabled=False, open_shares=10.0)
    e = _ceng(**kw)
    t = T0 + 10_000.0
    for k in range(5):                                                           # DOWN 0.30 -> 0.34 over 1 s, UP falling
        _ev(e, t - 1000.0 + 250.0 * k, (0.68 - 0.01 * k, 0.69 - 0.01 * k), (0.30 + 0.01 * k, 0.31 + 0.01 * k), btc=150.0)
    ev = _ev(e, t, (0.63, 0.64), (0.35, 0.36), btc=150.0)
    assert ev.tradeable and ev.side == "DOWN" and "market open buy" in ev.size_note, (ev.reason, ev.detail)
    e2 = _ceng(**kw)
    for k in range(5):                                                           # DOWN falling: no open buy of it
        _ev(e2, t - 1000.0 + 250.0 * k, (0.58 + 0.01 * k, 0.59 + 0.01 * k), (0.40 - 0.01 * k, 0.41 - 0.01 * k), btc=150.0)
    ev2 = _ev(e2, t, (0.63, 0.64), (0.35, 0.36), btc=150.0)
    assert not (ev2.tradeable and "market open buy" in (ev2.size_note or "")), (ev2.reason, ev2.detail)


def test_the_lower_bid_waits_for_the_venue_to_confirm_the_old_one_ended():
    """The real venue (2026-10-08 20:41): the old bid must be gone before the lower one goes, its last fills counted."""
    e = _ceng(pair_reprice_drop=0.05, late_enabled=False)
    t = T0 + 20_000.0
    m, side, po = _placed(e, t)
    cyc = e.cycles[SLUG]
    _ev(e, t + 4000.0, (0.60, 0.61), (0.38, 0.39))
    assert e.pair_cancels(t + 4000.0) == [po.order_id]
    assert e.pair_requests(t + 4000.0, {SLUG: m}) == []               # cancel sent, not confirmed: no second bid
    assert e.pair_requests(t + 5000.0, {SLUG: m}) == []
    # the venue's answer: the old bid had matched 7 before the cancel landed (result first, then its fills)
    e.on_pair_done(po.order_id, filled=True)
    f = Fill(po.order_id, po.token_id, Side.BUY, po.price, 7.0, 0.0, t + 5100.0, t + 5100.0, t + 5100.0, expected_price=po.price)
    e.on_pair_fill_record(SLUG, po, f, t + 5100.0)
    assert cyc.pair["state"] == "repricing" and cyc.pair["filled"] == 7.0
    (_, s2, o2), = e.pair_requests(t + 5200.0, {SLUG: m})
    assert o2.size == 13.0 and o2.price == pytest.approx(0.35)
    e.on_pair_submit(o2.order_id, SLUG, s2, o2.price, o2.size)
    assert cyc.pair["size"] == 20.0 and cyc.pair["filled"] == 7.0 and cyc.pair["state"] == "working"
    assert e.pair_cancels(t + 5300.0) == []                            # its fill was the old bid's own: no resize


def test_a_pulled_bid_that_filled_in_full_is_not_re_placed_and_no_answer_waits_35_s():
    e = _ceng(pair_reprice_drop=0.05, late_enabled=False)
    t = T0 + 20_000.0
    m, side, po = _placed(e, t)
    cyc = e.cycles[SLUG]
    _ev(e, t + 4000.0, (0.60, 0.61), (0.38, 0.39))
    e.pair_cancels(t + 4000.0)
    e.on_pair_done(po.order_id, filled=True)
    f = Fill(po.order_id, po.token_id, Side.BUY, po.price, 20.0, 0.0, t + 4100.0, t + 4100.0, t + 4100.0, expected_price=po.price)
    e.on_pair_fill_record(SLUG, po, f, t + 4100.0)
    assert e.pair_requests(t + 4200.0, {SLUG: m}) == [] and cyc.pair["state"] == "filled"
    assert cyc.book.up_qty == cyc.book.down_qty == 20.0
    e2 = _ceng(pair_reprice_drop=0.05, late_enabled=False)
    m, side, po = _placed(e2, t)
    _ev(e2, t + 4000.0, (0.60, 0.61), (0.38, 0.39))
    e2.pair_cancels(t + 4000.0)
    assert e2.pair_requests(t + 38_000.0, {SLUG: m}) == []             # 34 s: still waiting
    assert len(e2.pair_requests(t + 40_000.0, {SLUG: m})) == 1          # 36 s: the answer was lost, re-place
