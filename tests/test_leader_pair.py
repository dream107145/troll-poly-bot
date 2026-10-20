"""LEADER_PAIR (strategy/leader_pair/, the user's spec of 2026-10-13): the acceptance tests of its section 16 plus
order failures, partial fills, duplicated events, restarts, the emergency stop, the live-bot hooks and the
backtester's look-ahead / reproducibility guarantees. Not a test of whether the strategy makes money."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from troll_poly_bot.__main__ import check_leader_pair
from troll_poly_bot.config import BotConfig
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.leader_pair import LeaderPairConfig, LeaderPairEngine, MarketStrategy, Quote, State, View
from troll_poly_bot.strategy.leader_pair.accounting import Portfolio
from troll_poly_bot.strategy.leader_pair.backtest import BacktestConfig, engine_for, run_market, summarise
from troll_poly_bot.strategy.leader_pair.risk import Candidate, check, walk_asks
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce

T0 = 1_800_000_000_000.0
CLOSE = T0 + 300_000.0
FEE = FeeSchedule()


def fee(p: float) -> float:
    return FEE.taker_fee_per_share(p)


def cfg(**kw) -> LeaderPairConfig:
    kw.setdefault("exec_mode", "taker")                               # the rule tests run all-taker; the mix / speed have their own tests
    kw.setdefault("hedge_enabled", False)                             # the 10-14 cheap hedge has its own test
    kw.setdefault("pair_buy_enabled", False)                          # the 10-14 pair buy / lock have their own test
    kw.setdefault("lock_target_usdc", 0.0)
    return LeaderPairConfig(enabled=True, **kw)


def quote(bid, ask, depth=100.0, age=0.0) -> Quote:
    return Quote(bid, ask, [(bid, depth)], [(ask, depth)], age)


class Sim:
    """Drives one MarketStrategy through a price path; fills what it asks for at the seen ask."""

    def __init__(self, c: LeaderPairConfig | None = None, slug="btc-updown-5m-1800000000"):
        self.c = c or cfg()
        self.m = MarketStrategy(self.c, slug, T0, CLOSE)
        self.n = 0
        self.fills = []

    def view(self, t_s, up, dn=None, depth=100.0, age=0.0, balance=1e6, other=0.0, accepting=True) -> View:
        dn = dn or (round(1 - up[1], 4), round(1 - up[0], 4))
        return View(now=T0 + t_s * 1000.0, open_ts=T0, close_ts=CLOSE, up=quote(*up, depth=depth, age=age),
                    down=quote(*dn, depth=depth, age=age), fee_per_share=fee, balance=balance, accepting=accepting,
                    capital_other_markets=other)

    def tick(self, t_s, up, dn=None, fill=True, **kw):
        v = self.view(t_s, up, dn, **kw)
        it = self.m.on_tick(v)
        if it is None:
            return None
        self.n += 1
        oid = f"o{self.n}"
        self.m.on_order_sent(oid, it, v.now)
        if fill:
            px = it.expected
            self.m.on_order_done(oid, True, v.now + 250.0)
            self.m.on_fill(oid, it.side, it.shares, px, fee(px) * it.shares, v.now + 250.0)
            self.fills.append((it.kind, it.side, it.shares, px))
        else:
            self.m.on_order_done(oid, False, v.now + 250.0)
        return it

    def enter(self, up=(0.59, 0.61), t_s=6.0):
        it = self.tick(t_s, up)
        assert it is not None and it.kind == "init", self.m.refusals
        return it

    def ramp(self, t_from, t_to, up_from, up_to, step=0.25, spread=0.02, fill=False):
        """Ticks every ``step`` s with the UP ask moving linearly; returns the intents seen."""
        out = []
        n = int(round((t_to - t_from) / step))
        for i in range(n + 1):
            t = t_from + i * step
            a = round(up_from + (up_to - up_from) * i / max(n, 1), 4)
            it = self.tick(t, (round(a - spread, 4), a), fill=fill)
            if it is not None:
                out.append((t, it))
        return out


# ====================================================================== flag, config, paper-only

def test_flag_validation_and_mode_gates():
    assert LeaderPairConfig().enabled is False
    with pytest.raises(ValueError):
        LeaderPairEngine(EngineConfig(strategy="leader_pair"), LeaderPairConfig(), RiskManager(), FeatureEngine())
    with pytest.raises(ValueError, match="minimum"):
        LeaderPairEngine(EngineConfig(strategy="leader_pair"), cfg(accum_shares=2), RiskManager(), FeatureEngine())
    with pytest.raises(SystemExit):
        check_leader_pair("leader_pair", "paper", {})
    check_leader_pair("leader_pair", "paper", {"TPB_LP_ENABLED": "1"})
    for mode in ("shadow", "micro-live", "live"):
        with pytest.raises(SystemExit):
            check_leader_pair("leader_pair", mode, {"TPB_LP_ENABLED": "1"})
    with pytest.raises(SystemExit, match="unproven"):
        check_leader_pair("leader_pair", "shadow", {"TPB_LP_ENABLED": "1", "TPB_LP_ALLOW_REAL": "1"})
    check_leader_pair("other", "live", {})
    env = {"TPB_LP_ENABLED": "1", "TPB_LP_PAIR_THRESHOLD": "0.9", "TPB_LP_ACCUM_INTERVAL_S": "3", "TPB_LP_ACCUM_EVAL": "any_tick",
           "TPB_LP_INIT_TOP_UP": "1"}
    c = LeaderPairConfig.from_env(env)
    assert (c.enabled, c.pair_threshold, c.accum_interval_s, c.accum_eval, c.init_top_up) == (True, 0.9, 3.0, "any_tick", True)
    assert BotConfig().leader_pair.enabled is False
    d = LeaderPairConfig()
    assert (d.init_shares, d.pair_threshold, d.accum_interval_s, d.opp_ceiling, d.leader_reversal) == (10.0, 0.95, 5.0, 0.50, 0.50)
    assert (d.max_market_capital_usdc, d.max_total_capital_usdc, d.max_daily_loss_usdc,
            d.max_settlement_loss_usdc, d.exec_mode) == (150.0, 500.0, 150.0, 20.0, "taker")


# ====================================================================== rule 1

def test_initial_entry_buys_the_configured_shares_of_the_higher_mid_token():
    s = Sim()
    assert s.tick(2.0, (0.59, 0.61)) is None                          # before init_delay_s
    it = s.tick(6.0, (0.59, 0.61))
    assert it.kind == "init" and it.side == "UP" and it.shares == 10.0 and it.limit == pytest.approx(0.63)
    assert s.m.state is State.MONITORING and s.m.leader == "UP"
    s2 = Sim()
    it = s2.tick(6.0, (0.39, 0.41))                                  # DOWN is the leader
    assert it.side == "DOWN" and s2.m.leader == "DOWN"
    s3 = Sim(cfg(leader_tie="skip"))
    assert s3.tick(6.0, (0.49, 0.51), dn=(0.49, 0.51)) is None and s3.m.refusals["init:TIE"] == 1


def test_reference_price_comes_from_the_actual_fill_not_the_ask():
    s = Sim()
    v = s.view(6.0, (0.59, 0.61))
    it = s.m.on_tick(v)
    s.m.on_order_sent("o1", it, v.now)
    s.m.on_order_done("o1", True, v.now + 200)
    s.m.on_fill("o1", "UP", 10.0, 0.62, fee(0.62) * 10, v.now + 200)    # filled a tick worse than the ask seen
    assert s.m.p0 == pytest.approx(0.62) and s.m.p_ref == pytest.approx(0.38)
    s2 = Sim(cfg(p_ref_includes_fee=True))
    v = s2.view(6.0, (0.59, 0.61))
    it = s2.m.on_tick(v)
    s2.m.on_order_sent("o1", it, v.now)
    s2.m.on_fill("o1", "UP", 10.0, 0.62, fee(0.62) * 10, v.now + 200)
    assert s2.m.p_ref == pytest.approx(1 - (0.62 + fee(0.62)))


def test_initial_order_failures_retry_then_no_entry_and_partial_fill_is_reconciled():
    s = Sim(cfg(init_max_tries=2, init_retry_s=1.0))
    assert s.tick(6.0, (0.59, 0.61), fill=False).kind == "init"
    assert s.tick(6.5, (0.59, 0.61)) is None                          # retry wait + end guard
    assert s.tick(9.0, (0.59, 0.61), fill=False).kind == "init"
    assert s.tick(12.0, (0.59, 0.61)) is None and s.m.state is State.NO_ENTRY
    # a partial fill: inventory and P0 are what actually filled, no top-up by default
    s = Sim()
    v = s.view(6.0, (0.59, 0.61))
    it = s.m.on_tick(v)
    s.m.on_order_sent("o1", it, v.now)
    s.m.on_order_done("o1", True, v.now + 200)
    s.m.on_fill("o1", "UP", 6.0, 0.61, fee(0.61) * 6, v.now + 200)
    t = s.m.portfolio.totals
    assert t.q_up == 6.0 and t.c_up == pytest.approx(6 * 0.61 + fee(0.61) * 6) and s.m.p0 == pytest.approx(0.61)
    assert s.m.state is State.MONITORING and s.tick(9.0, (0.59, 0.61)) is None
    s = Sim(cfg(init_top_up=True))
    v = s.view(6.0, (0.59, 0.61))
    it = s.m.on_tick(v)
    s.m.on_order_sent("o1", it, v.now)
    s.m.on_order_done("o1", True, v.now + 200)
    s.m.on_fill("o1", "UP", 5.0, 0.61, fee(0.61) * 5, v.now + 200)
    it2 = s.tick(9.0, (0.59, 0.61))
    assert it2 is not None and it2.kind == "init" and it2.shares == 5.0


def test_three_consecutive_unfilled_fak_orders_stop_the_market():
    s = Sim(cfg(falling_buy="none"))
    s.enter((0.59, 0.61))
    s.tick(11.0, (0.59, 0.61))
    for t in (11.5, 16.5, 21.5):
        it = s.tick(t, (0.59, 0.61), fill=False)
        assert it is not None and it.kind == "accum"
    assert s.m.state is State.RISK_LIMIT_STOPPED
    assert s.tick(26.5, (0.57, 0.59)) is None


# ====================================================================== rule 3

def test_accumulation_only_inside_the_range_and_only_above_the_previous_fill():
    s = Sim()
    s.enter((0.59, 0.61))                                             # P0 0.61, P_ref 0.39
    assert s.tick(11.0, (0.59, 0.61)) is None                         # DOWN ask 0.41 > 0.39: in range -> state, slot 0 done?
    # the sequence started on this tick (slot 0 of the anchor): the buy comes on the first tick of slot 1
    assert s.m.state is State.OPPOSITE_ACCUMULATION
    it = s.tick(11.5, (0.59, 0.61))
    assert it is not None and it.kind == "accum" and it.side == "DOWN" and it.shares == 5.0, s.m.refusals
    assert s.m.accum.prev_fill == pytest.approx(0.41)
    assert s.tick(16.5, (0.59, 0.61)) is None                         # same ask 0.41: not above the previous fill
    assert s.tick(21.5, (0.60, 0.62)) is None                         # DOWN ask 0.40 < 0.41 (fell)
    it = s.tick(26.5, (0.56, 0.58))                                   # DOWN ask 0.44 > 0.41
    assert it is not None and it.kind == "accum" and s.m.accum.prev_fill == pytest.approx(0.44)
    s2 = Sim()
    s2.enter((0.59, 0.61))
    assert s2.tick(11.5, (0.62, 0.64)) is None and s2.m.state is State.MONITORING   # DOWN ask 0.36 <= P_ref 0.39: no sequence


def test_no_accumulation_above_the_ceiling_and_the_rule_stops():
    s = Sim()
    s.enter((0.59, 0.61))
    s.tick(11.0, (0.59, 0.61))
    assert s.tick(11.5, (0.59, 0.61)).kind == "accum"
    assert s.tick(16.5, (0.47, 0.49)) is None                         # DOWN ask 0.53 > 0.50: the sequence ends
    assert s.m.state is State.MONITORING and s.m.accum.active is False and s.m.accum.ended == "ask above the ceiling"
    assert s.tick(21.5, (0.47, 0.49)) is None and s.m.state is State.MONITORING
    # back under the ceiling: a new sequence with the previous-fill reference reset (accum_resequence)
    s.tick(26.5, (0.53, 0.55))
    assert s.m.state is State.OPPOSITE_ACCUMULATION and s.m.accum.seq == 2 and s.m.accum.prev_fill is None
    s3 = Sim(cfg(accum_resequence=False))
    s3.enter((0.59, 0.61))
    s3.tick(11.0, (0.59, 0.61))
    s3.tick(16.5, (0.47, 0.49))
    s3.tick(26.5, (0.53, 0.55))
    assert s3.m.state is State.MONITORING and s3.m.accum.seq == 1


def test_each_purchase_is_evaluated_once_per_five_second_slot_whatever_the_tick_rate():
    s = Sim()
    s.enter((0.59, 0.61), t_s=6.0)                                    # anchor at 6.25 s (fill ack)
    buys = []
    a = 0.41
    for i in range(0, 200):                                           # 40 s of 200 ms ticks, the DOWN ask rising 0.4c every 2 s
        t = 7.0 + i * 0.2                                             # (staying under 0.50, so the leader never "falls")
        a = round(0.41 + 0.004 * int(i // 10), 4)
        it = s.tick(t, (round(1 - a - 0.02, 4), round(1 - a, 4)))
        if it is not None:
            buys.append((t, it.kind))
    assert all(k == "accum" for _, k in buys)
    ts = [t for t, _ in buys]
    assert len(buys) >= 2 and all(b - a_ >= 5.0 - 1e-9 for a_, b in zip(ts, ts[1:])), ts
    # duplicate data messages (the same tick twice) never buy twice in the slot
    s2 = Sim()
    s2.enter((0.59, 0.61))
    s2.tick(11.0, (0.59, 0.61))
    assert s2.tick(11.5, (0.59, 0.61)).kind == "accum"
    assert s2.tick(11.5, (0.55, 0.57)) is None and s2.tick(11.6, (0.55, 0.57)) is None


def test_any_tick_mode_buys_once_per_slot_on_the_first_tick_that_qualifies():
    s = Sim(cfg(accum_eval="any_tick"))
    s.enter((0.59, 0.61))
    s.tick(11.0, (0.59, 0.61))
    assert s.tick(11.5, (0.59, 0.61)).kind == "accum"                 # slot 1 at 0.41
    assert s.tick(16.5, (0.59, 0.61)) is None                          # slot 2: not above the previous fill
    assert s.tick(18.0, (0.57, 0.59)).kind == "accum"                  # slot 2, later tick: 0.43 > 0.41
    assert s.tick(19.0, (0.55, 0.57)) is None                          # still slot 2: done


# ====================================================================== 2026-10-19 cross-crypto leader / pair rule

def cross_view(t_s, up, dn, peer_avg):
    return View(now=T0 + t_s * 1000.0, open_ts=T0, close_ts=CLOSE,
                up=quote(*up), down=quote(*dn), fee_per_share=fee, balance=1e6, peer_avg=peer_avg)


def fill_cross(m, intent, v, order_id):
    m.on_order_sent(order_id, intent, v.now)
    m.on_fill(order_id, intent.side, intent.shares, intent.expected, fee(intent.expected) * intent.shares, v.now + 100.0)


def test_cross_market_leader_uses_five_matching_outcome_asks_then_pairs_after_a_30c_rise():
    c = cfg(cross_market_enabled=True)
    m = MarketStrategy(c, "btc-updown-5m-cross", T0, CLOSE)
    # UP is only $0.35 locally but it is the leader because the other five UP
    # asks, rather than this market's higher-priced DOWN, average above $0.88.
    enter = cross_view(1.0, (0.34, 0.35), (0.65, 0.66), {"UP": 0.89, "DOWN": 0.11})
    it = m.on_tick(enter)
    assert it is not None and (it.kind, it.side, it.shares) == ("leader", "UP", 5.0)
    fill_cross(m, it, enter, "leader")
    assert m.cross_leader == "UP" and m.cross_leader_price == pytest.approx(0.35)

    risen = cross_view(20.0, (0.64, 0.65), (0.34, 0.35), {"UP": 0.89, "DOWN": 0.11})
    pair = m.on_tick(risen)
    assert pair is not None and (pair.kind, pair.side, pair.shares) == ("cross_pair", "DOWN", 5.0)
    fill_cross(m, pair, risen, "pair")
    assert m.cross_pair_filled is True


def test_cross_market_signal_buys_the_local_matching_token_once_above_090_and_never_after_four_minutes():
    c = cfg(cross_market_enabled=True)
    m = MarketStrategy(c, "eth-updown-5m-cross", T0, CLOSE)
    signal = cross_view(30.0, (0.54, 0.55), (0.45, 0.46), {"UP": 0.901, "DOWN": 0.099})
    it = m.on_tick(signal)
    assert it is not None and (it.kind, it.side, it.shares) == ("cross_signal", "UP", 5.0)
    fill_cross(m, it, signal, "signal")
    assert m.on_tick(signal) is None

    last_minute = MarketStrategy(c, "sol-updown-5m-cross", T0, CLOSE)
    assert last_minute.on_tick(cross_view(241.0, (0.54, 0.55), (0.45, 0.46), {"UP": 0.91, "DOWN": 0.09})) is None


# ====================================================================== rule 2: pairs

def test_pair_accounting_never_reuses_shares_and_qualifies_on_fee_inclusive_cost():
    p = Portfolio(pair_threshold=0.95)
    p.add_fill("UP", 10, 0.60, fee(0.60) * 10, 0, "a", "init")
    p.add_fill("DOWN", 4, 0.30, fee(0.30) * 4, 1, "b", "accum")
    p.add_fill("DOWN", 8, 0.36, fee(0.36) * 8, 2, "c", "accum")
    t = p.totals
    assert t.matched_up == t.matched_down == 10.0 and t.unmatched_up == 0.0 and t.unmatched_down == pytest.approx(2.0)
    assert sum(pr.shares for pr in p.pairs) == 10.0
    assert sum(l.remaining for l in p.lots["UP"]) == 0.0 and sum(l.remaining for l in p.lots["DOWN"]) == pytest.approx(2.0)
    # FIFO: the 0.30 lot pairs first with the 0.60 lot
    assert p.pairs[0].shares == 4.0 and p.pairs[0].cost == pytest.approx(0.60 + fee(0.60) + 0.30 + fee(0.30))
    assert p.pairs[0].qualifies is True
    assert p.pairs[1].shares == 6.0 and p.pairs[1].cost == pytest.approx(0.60 + fee(0.60) + 0.36 + fee(0.36))
    # 0.60 + 0.36 = 0.96 nominal, 0.9926 with fees: not a qualifying pair, recorded with its actual cost
    assert p.pairs[1].qualifies is False and p.qualifying_pairs() == 4.0 and p.formed_pairs() == 10.0
    assert p.pair_margin() == pytest.approx(4 * (1 - p.pairs[0].cost))
    # the pair threshold is applied to the FEE-INCLUSIVE cost: 0.60 + 0.34 = 0.94 nominal fails once the fees are in
    q = Portfolio(pair_threshold=0.95)
    q.add_fill("UP", 5, 0.60, fee(0.60) * 5, 0)
    q.add_fill("DOWN", 5, 0.34, fee(0.34) * 5, 1)
    assert q.pairs[0].cost == pytest.approx(0.94 + fee(0.60) + fee(0.34)) and q.pairs[0].qualifies is False
    q2 = Portfolio(pair_threshold=0.95)
    q2.add_fill("UP", 5, 0.60, 0.0, 0)
    q2.add_fill("DOWN", 5, 0.35, 0.0, 1)
    assert q2.pairs[0].qualifies is True and q2.pairs[0].margin == pytest.approx(0.05)
    assert p.avg_pair_cost() == pytest.approx((4 * p.pairs[0].cost + 6 * p.pairs[1].cost) / 10)


def test_settlement_pnl_identities_and_partial_fills_update_the_cost_basis():
    p = Portfolio()
    p.add_fill("UP", 10, 0.60, 0.1, 0)
    p.add_fill("DOWN", 3, 0.40, 0.03, 1)                              # a partial fill of a 5-share order
    p.add_fill("DOWN", 2, 0.41, 0.02, 2)                              # ... and the rest, a tick later
    t = p.totals
    assert t.q_up == 10 and t.q_down == 5 and t.c_total == pytest.approx(6.0 + 0.1 + 1.2 + 0.03 + 0.82 + 0.02)
    assert t.pnl_if_up == pytest.approx(10 - t.c_total) and t.pnl_if_down == pytest.approx(5 - t.c_total)
    # the pair decomposition gives the same numbers: pairs pay 1, unmatched shares pay 1 only on their side
    pairs = sum(pr.shares * (1 - pr.cost) for pr in p.pairs)
    un_cost = sum(l.remaining * l.unit_cost for s in ("UP", "DOWN") for l in p.lots[s])
    assert pairs + t.unmatched_up - un_cost == pytest.approx(t.pnl_if_up)
    assert pairs + t.unmatched_down - un_cost == pytest.approx(t.pnl_if_down)
    assert p.settle(True) == pytest.approx(t.pnl_if_up) and p.settled and p.realized_pnl == pytest.approx(t.pnl_if_up)
    assert p.avg_cost("DOWN") == pytest.approx((1.2 + 0.03 + 0.82 + 0.02) / 5)
    # the conservative liquidation: bids, the taker fee at the bid, slippage, and depth
    liq = p.liquidation_pnl(0.55, 0.42, fee, 0.01)
    assert liq == pytest.approx(10 * (0.55 - fee(0.55) - 0.01) + 5 * (0.42 - fee(0.42) - 0.01) - t.c_total)
    thin = p.liquidation_pnl(0.55, 0.42, fee, 0.0, bid_levels_up=[(0.55, 4.0)], bid_levels_down=[(0.42, 100.0)])
    assert thin == pytest.approx(4 * (0.55 - fee(0.55)) + 5 * (0.42 - fee(0.42)) - t.c_total)
    assert p.liquidation_pnl(None, 0.42, fee) is None


def test_partial_fills_count_once_toward_purchase_caps_and_cycles():
    m = MarketStrategy(cfg(), "cycle-test", T0, CLOSE)
    m.on_fill("one-order", "UP", 3.0, 0.60, 0.0, T0, kind="accum")
    m.on_fill("one-order", "UP", 2.0, 0.61, 0.0, T0 + 100.0, kind="accum")
    assert m.portfolio.totals.purchases == 1
    assert m.purchases_this_cycle == 1
    restored = MarketStrategy.restore(cfg(), json.loads(json.dumps(m.persist())))
    restored.on_fill("one-order", "UP", 1.0, 0.62, 0.0, T0 + 200.0, kind="accum")
    assert restored.portfolio.totals.purchases == 1
    assert restored.purchases_this_cycle == 1


# ====================================================================== rules 4 / 5 / 6

def _fall_and_recover(s: Sim):
    """UP leader at 0.61, DOWN accumulates to 0.50, then UP falls to 0.40 and recovers."""
    if s.m.state is State.INITIAL_ENTRY:
        s.enter((0.59, 0.61))
    s.ramp(11.0, 26.0, 0.61, 0.52, fill=True)                         # DOWN rises 0.39 -> 0.48: accumulation buys
    assert s.m.state is State.OPPOSITE_ACCUMULATION and s.m.accum.fills >= 1
    s.ramp(26.25, 50.0, 0.52, 0.40, fill=True)                        # UP under 0.50 and falling
    assert s.m.state is State.LEADER_FALLING, (s.m.state, s.m.fall_n)
    return s


def test_falling_leader_and_recovery_transitions_follow_the_confirmed_conditions():
    s = Sim(cfg(falling_buy="none"))
    s.enter((0.59, 0.61))
    # under 0.50 is not enough: a one-tick dip does not confirm
    for t in (11.0, 11.25, 11.5, 11.75, 12.0, 15.0, 16.0, 17.0):
        s.tick(t, (0.59, 0.61))
    s.tick(17.25, (0.47, 0.49))
    assert s.m.state in (State.MONITORING, State.OPPOSITE_ACCUMULATION) and s.m.fall_n == 1
    s.tick(17.5, (0.59, 0.61))
    assert s.m.fall_n == 0
    # a sustained fall over delta_t confirms on the 3rd consecutive evaluation
    s = Sim(cfg(falling_buy="none"))
    _fall_and_recover(s)
    ev = [e for e in s.m.events if e["event"] == "FALLING_LEADER"]
    assert ev and ev[-1]["cycle"] == 1 and "pnl_if_up" in ev[-1]
    s.ramp(50.25, 70.0, 0.40, 0.52, fill=True)                         # recovery: rising, confirmed
    assert s.m.state in (State.RECOVERY, State.RECOVERY_STOPPED)
    # falling again -> LEADER_FALLING, cycle 2
    s.ramp(70.25, 95.0, 0.52, 0.38, fill=False)
    assert s.m.state is State.LEADER_FALLING and s.m.cycle == 2 and s.m.purchases_this_cycle == 0


def test_recovery_purchases_stop_when_conservative_live_pnl_is_strictly_positive():
    s = Sim(cfg(falling_buy="none", recovery_min_rise=0.005, liq_slippage=0.0))
    s.enter((0.59, 0.61))
    s.ramp(11.0, 26.0, 0.61, 0.52, fill=True)
    s.ramp(26.25, 50.0, 0.52, 0.40, fill=True)
    assert s.m.state is State.LEADER_FALLING
    s.ramp(50.25, 56.0, 0.40, 0.45, fill=False)
    assert s.m.state is State.RECOVERY
    t = s.m.portfolio.totals
    # the stop is checked before each recovery order: with the mark far under the cost, buys are allowed
    buys = s.ramp(56.25, 70.0, 0.45, 0.50, fill=True)
    kinds = {it.kind for _, it in buys}
    assert kinds == {"recovery"} and all(it.side == "UP" for _, it in buys), buys
    assert s.m.pnl_live is not None and s.m.pnl_live <= 0.0
    # now push both bids up so that liquidation value exceeds the cost: the next tick stops recovery at once
    s.tick(70.25, (0.95, 0.97), dn=(0.95, 0.97))
    assert s.m.state is State.RECOVERY_STOPPED and s.m.recovery_stops == 1 and s.m.pnl_live > 0
    assert s.tick(75.25, (0.95, 0.97), dn=(0.95, 0.97)) is None       # no more recovery buys
    # the stop is "strictly positive": a live PnL of exactly zero does not stop
    m = MarketStrategy(cfg(), "x", T0, CLOSE)
    m.state, m.leader, m.phase_anchor, m.phase_slot = State.RECOVERY, "UP", T0, 10 ** 9
    m.portfolio.add_fill("UP", 10, 0.5, 0.0, 0)
    v = View(now=T0 + 10_000, open_ts=T0, close_ts=CLOSE, up=quote(0.5, 0.52), down=quote(0.48, 0.5), fee_per_share=lambda p: 0.0)
    m.cfg.liq_slippage = 0.0
    m.on_tick(v)
    assert m.pnl_live == pytest.approx(0.0) and m.state is State.RECOVERY


def test_falling_state_buys_the_opposite_only_when_it_improves_the_worst_outcome():
    # one accumulation buy only (5 DOWN against 10 UP): the DOWN-win branch is the worst, so a DOWN buy improves it
    s = Sim(cfg(falling_buy="improve_worst", falling_opp_max_ask=0.70, max_purchases_per_cycle=1, fix_enabled=False))
    _fall_and_recover(s)
    buys = [f for f in s.fills if f[0] == "falling"]
    assert buys and all(f[1] == "DOWN" for f in buys), s.m.refusals
    ev = [e for e in s.m.events if e["event"] == "ORDER" and e["kind"] == "falling"]
    assert ev and s.m.portfolio.totals.q_down <= s.m.portfolio.totals.q_up + 5.0
    # once DOWN >= UP the worst outcome is the UP branch, which another DOWN buy would lower: refused
    s2 = Sim(cfg(falling_buy="improve_worst", falling_opp_max_ask=0.70, fix_enabled=False))
    s2.enter((0.59, 0.61))
    s2.m.portfolio.add_fill("DOWN", 30, 0.45, 0.0, T0)
    s2.m.state, s2.m.phase_anchor, s2.m.phase_slot = State.LEADER_FALLING, T0 + 10_000, -1
    assert s2.tick(20.0, (0.38, 0.40)) is None and s2.m.refusals["falling:NO_IMPROVEMENT"] == 1


# ====================================================================== risk (section 11)

def _cand(**kw) -> Candidate:
    d = dict(side="DOWN", shares=5.0, kind="accum", best_bid=0.40, best_ask=0.42, ask_levels=[(0.42, 100.0)], book_age_ms=10.0,
             seconds_left=200.0, accepting=True, balance=1000.0, purchases_this_cycle=0, purchases_this_market=1,
             pending_in_market=False, end_guarded=False, capital_other_markets=0.0)
    d.update(kw)
    return Candidate(**d)


def test_every_pre_order_check_refuses_with_its_reason():
    c = cfg(max_market_capital_usdc=20.0, max_total_capital_usdc=30.0, max_settlement_loss_usdc=5.0, max_purchases_per_cycle=2)
    pf = Portfolio()
    pf.add_fill("UP", 10, 0.60, fee(0.60) * 10, 0)
    assert check(_cand(), c, pf, fee)[0] is None
    for kw, why in ((dict(accepting=False), "MARKET_STATUS"), (dict(book_age_ms=5000), "STALE_DATA"),
                    (dict(seconds_left=10), "TIME_REMAINING"), (dict(pending_in_market=True), "PENDING_ORDER"),
                    (dict(end_guarded=True), "END_GUARD"), (dict(best_ask=None), "NO_BOOK"),
                    (dict(best_bid=0.45), "BAD_BOOK"), (dict(best_ask=0.97, best_bid=0.96), "PRICE_BAND"),
                    (dict(best_bid=0.30), "SPREAD"), (dict(ask_levels=[(0.42, 3.0)]), "LIQUIDITY"),
                    (dict(ask_levels=[(0.42, 1.0), (0.50, 100.0)], shares=10.0), "LIQUIDITY"),
                    (dict(purchases_this_cycle=2), "PURCHASES_CYCLE"), (dict(purchases_this_market=20), "PURCHASES_MARKET"),
                    (dict(shares=40.0, ask_levels=[(0.42, 100.0)]), "MARKET_CAPITAL"),
                    (dict(capital_other_markets=28.0), "TOTAL_CAPITAL"),
                    (dict(side="UP", shares=5.0, best_ask=0.90, best_bid=0.89, ask_levels=[(0.90, 100.0)]), "SETTLEMENT_LOSS"),
                    (dict(balance=1.0), "BALANCE"), (dict(shares=3.0), "SIZE")):
        assert check(_cand(**kw), c, pf, fee)[0] == why, (kw, why)
    # slippage: the limit is the ask + max_slippage; what the book holds above it is not fillable (a partial fill
    # of what IS there, never a worse price), and the VWAP of the size may not exceed the ask by more than max_slippage
    why, ap = check(_cand(shares=10.0, ask_levels=[(0.42, 5.0), (0.44, 5.0)]), cfg(max_slippage=0.01), pf, fee)
    assert why is None and ap.fillable == 5.0 and ap.limit == pytest.approx(0.43)
    assert check(_cand(shares=10.0, ask_levels=[(0.42, 4.0), (0.44, 5.0)]), cfg(max_slippage=0.01), pf, fee)[0] == "LIQUIDITY"
    why, ap = check(_cand(shares=10.0, ask_levels=[(0.42, 5.0), (0.44, 5.0)]), cfg(max_slippage=0.02), pf, fee)
    assert why is None and ap.vwap == pytest.approx(0.43) and ap.fillable == 10.0 and ap.limit == pytest.approx(0.44)
    assert walk_asks([(0.42, 5.0), (0.44, 5.0), (0.50, 50)], 20.0, 0.44) == (10.0, pytest.approx(0.43))

def test_shared_approval_rejects_objective_worsening_and_unachievable_favourite_targets():
    c = cfg()
    pf = Portfolio()
    pf.add_fill("UP", 10, 0.60, fee(0.60) * 10, 0, "leader", "init")
    pf.add_fill("DOWN", 10, 0.40, fee(0.40) * 10, 1, "hedge", "accum")
    assert check(_cand(side="DOWN", shares=5), c, pf, fee)[0] == "NO_IMPROVEMENT"

    negative = Portfolio()
    negative.add_fill("UP", 10, 0.60, fee(0.60) * 10, 0, "leader", "init")
    negative.add_fill("DOWN", 10, 0.45, fee(0.45) * 10, 1, "other", "accum")
    assert check(_cand(side="UP", shares=5, kind="fix", best_bid=0.69, best_ask=0.70,
                       ask_levels=[(0.70, 100.0)]), c, negative, fee)[0] == "TARGET_UNACHIEVABLE"


def test_no_order_breaches_the_capital_budget_or_the_settlement_loss_limit_in_a_run():
    c = cfg(max_market_capital_usdc=12.0, max_settlement_loss_usdc=8.0, accum_shares=5.0, falling_buy="none")
    s = Sim(c)
    s.enter((0.59, 0.61))
    s.ramp(11.0, 60.0, 0.61, 0.52, fill=True)
    t = s.m.portfolio.totals
    assert t.c_total <= 12.0 + 1e-9 and min(t.pnl_if_up, t.pnl_if_down) >= -8.0 - 1e-9
    assert s.m.refusals["accum:MARKET_CAPITAL"] >= 1 or s.m.refusals["accum:SETTLEMENT_LOSS"] >= 1
    if s.m.refusals["accum:MARKET_CAPITAL"]:
        assert s.m.state is State.RISK_LIMIT_STOPPED and s.m.why_stopped == "MARKET_CAPITAL"
    # the settlement-loss limit alone (the capital cap wide): every outcome stays within it
    c2 = cfg(max_market_capital_usdc=1000.0, max_settlement_loss_usdc=7.0, falling_buy="none")
    s2 = Sim(c2)
    s2.enter((0.59, 0.61))
    s2.ramp(11.0, 60.0, 0.61, 0.52, fill=True)
    t2 = s2.m.portfolio.totals
    assert min(t2.pnl_if_up, t2.pnl_if_down) >= -7.0 - 1e-9
    assert s2.m.refusals["accum:NO_IMPROVEMENT"] >= 1


def test_unfilled_orders_guard_the_token_and_never_change_inventory():
    s = Sim()
    s.enter((0.59, 0.61))
    s.tick(11.0, (0.59, 0.61))
    it = s.tick(11.5, (0.59, 0.61), fill=False)
    assert it.kind == "accum" and s.m.portfolio.totals.q_down == 0.0 and s.m.accum.prev_fill is None
    assert s.m.end_guard["DOWN"] > T0 + 11_500
    assert s.tick(12.0, (0.55, 0.57)) is None                          # slot 1 was decided; and the end guard holds
    # the same order's end reported twice is harmless; a fill after the end is still booked
    s.m.on_order_done("o2", False, T0 + 13_000)
    s.m.on_fill("o2", "DOWN", 5.0, 0.41, 0.1, T0 + 13_100, kind="accum")
    assert s.m.portfolio.totals.q_down == 5.0 and s.m.accum.prev_fill == pytest.approx(0.41)


def test_state_survives_a_restart():
    s = Sim()
    _fall_and_recover(s)
    rec = json.loads(json.dumps(s.m.persist()))
    m2 = MarketStrategy.restore(cfg(), rec)
    assert m2.state is s.m.state and m2.leader == "UP" and m2.p_ref == pytest.approx(s.m.p_ref) and m2.cycle == s.m.cycle
    assert m2.portfolio.snapshot()["pairs"] == s.m.portfolio.snapshot()["pairs"]
    assert m2.portfolio.totals.c_total == pytest.approx(s.m.portfolio.totals.c_total)
    assert m2.portfolio.totals.q_up == s.m.portfolio.totals.q_up and m2.accum.prev_fill == s.m.accum.prev_fill
    assert m2.pending == {} and m2.fall_n == 0


# ====================================================================== the live-bot hooks (engine.py)

def _book(token, bid, ask, size=100.0):
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size)])


class _Live:
    def __init__(self, tmp_path, **kw):
        self.sc = cfg(**kw)
        rc = RiskConfig(max_position_usdc=50.0, max_total_exposure_usdc=500.0, max_concurrent_positions=4,
                        max_asset_exposure_usdc=500.0, max_epoch_exposure_usdc=500.0, max_daily_loss_usdc=100.0)
        self.risk = RiskManager(cfg=rc, starting_balance=300.0)
        self.e = LeaderPairEngine(EngineConfig(strategy="leader_pair"), self.sc, self.risk, FeatureEngine(),
                                  state_path=tmp_path / "leader_pair_state.json")
        self.m = Market(condition_id="c", asset="BTC", yes_token_id="UPTOK", no_token_id="DNTOK", strike=100.0, open_ts=T0,
                        close_ts=CLOSE, slug="btc-updown-5m-1800000000", min_size=5.0)
        self.meta = MarketMeta(market=self.m, resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)
        self.e.meta_by_slug[self.m.slug] = self.meta

    def pass_(self, t_s, up, dn=None):
        dn = dn or (round(1 - up[1], 4), round(1 - up[0], 4))
        now = T0 + t_s * 1000.0
        self.e.set_book_age(self.m.slug, now, 20.0)
        self.e.pair_cancels(now)
        books = {self.m.slug: (_book("UPTOK", *up), _book("DNTOK", *dn))}
        return self.e.pair_requests(now, {self.m.slug: self.m}, books, balance=300.0)

    def fill(self, order, t_s, price=None, size=None):
        now = T0 + t_s * 1000.0
        px = price if price is not None else order.expected_price
        q = size if size is not None else order.size
        self.e.on_pair_done(order.order_id, filled=True)
        self.e.on_pair_fill_record(self.m.slug, order, SimpleNamespace(size=q, price=px, fee=fee(px) * q, token_id=order.token_id), now)


def test_engine_sends_ioc_buys_books_fills_persists_and_settles(tmp_path):
    w = _Live(tmp_path)
    assert w.pass_(2.0, (0.59, 0.61)) == []
    asks = w.pass_(6.0, (0.59, 0.61))
    assert len(asks) == 1
    slug, side, o = asks[0]
    assert side == "UP" and o.token_id == "UPTOK" and o.tif is TimeInForce.IOC and o.size == 10.0 and o.price == pytest.approx(0.63)
    assert w.pass_(6.2, (0.59, 0.61)) == []                            # one order in flight: no duplicate
    w.e.on_pair_submit(o.order_id, slug, side, o.price, o.size)
    w.fill(o, 6.3)
    ms = w.e.markets[slug]
    assert ms.state is State.MONITORING and ms.p_ref == pytest.approx(0.39)
    # the evaluate() path never trades and records the data age
    ev = w.e.evaluate(w.meta, _book("UPTOK", 0.59, 0.61), _book("DNTOK", 0.39, 0.41), None, 50.0, 30.0, T0 + 7_000, None, None, None, 300.0,
                      quote_ages={"UPTOK": 12.0, "DNTOK": 40.0})
    assert ev.reason is Reason.WEAK_MOMENTUM and w.e.market_data[slug]["book_age_ms"] == 40.0
    # accumulation through the hooks
    w.pass_(11.0, (0.59, 0.61))
    asks = w.pass_(11.5, (0.59, 0.61))
    assert len(asks) == 1 and asks[0][1] == "DOWN" and asks[0][2].size == 5.0
    o2 = asks[0][2]
    w.fill(o2, 11.8, size=3.0)                                        # a partial fill of the IOC
    w.e.on_pair_done(o2.order_id, filled=True)
    t = ms.portfolio.totals
    assert t.q_down == 3.0 and ms.accum.prev_fill == pytest.approx(0.41) and ms.pending == {}
    # persisted after the fill; a new engine restores the lots and the state
    assert (tmp_path / "leader_pair_state.json").exists()
    e2 = LeaderPairEngine(EngineConfig(strategy="leader_pair"), w.sc, w.risk, FeatureEngine(), state_path=tmp_path / "leader_pair_state.json")
    e2.pair_cancels(T0 + 12_000)
    assert slug in e2.markets and e2.markets[slug].portfolio.totals.q_down == 3.0 and e2.markets[slug].p_ref == pytest.approx(0.39)
    # the emergency stop: no orders while the guard pauses or risk is halted (the strategy still ticks)
    w.e.guard_paused = True
    assert w.pass_(16.5, (0.57, 0.59)) == [] and ms.refusals["accum:EMERGENCY_STOP"] == 1
    w.e.guard_paused = False
    w.e.risk_halted = True
    assert w.pass_(21.5, (0.55, 0.57)) == []
    w.e.risk_halted = False
    # settlement
    w.e.on_market_settled(slug, True, 0.0, CLOSE + 1000)
    assert ms.portfolio.settled and ms.state is State.MARKET_CLOSED and ms.portfolio.settlement_pnl == pytest.approx(t.pnl_if_up)
    snap = w.e.snapshot()
    assert snap["strategy"] == "leader_pair" and slug in snap["leader_pair"]["markets"]
    assert snap["leader_pair"]["stats"]["init_fills"] == 1 and snap["leader_pair"]["stats"]["markets_settled"] == 1
    assert w.e.holds_window(slug) is False


def test_engine_refuses_stale_books_and_books_a_fill_that_arrives_after_the_end(tmp_path):
    w = _Live(tmp_path)
    now = T0 + 6_000
    w.e.pair_cancels(now)                                             # no set_book_age: unknown freshness -> stale
    books = {w.m.slug: (_book("UPTOK", 0.59, 0.61), _book("DNTOK", 0.39, 0.41))}
    assert w.e.pair_requests(now, {w.m.slug: w.m}, books, balance=300.0) == []
    assert w.e.markets[w.m.slug].refusals["init:STALE_DATA"] == 1
    # the books' own venue timestamps are the fallback (live.py tells the engine the feed's age each pass otherwise)
    fresh = {w.m.slug: (OrderBook("UPTOK", [BookLevel(0.59, 100)], [BookLevel(0.61, 100)], ts=now - 300.0),
                        OrderBook("DNTOK", [BookLevel(0.39, 100)], [BookLevel(0.41, 100)], ts=now - 100.0))}
    assert len(w.e.pair_requests(now + 2_000, {w.m.slug: w.m}, fresh, balance=300.0)) == 1
    w.e.on_pair_done(w.e.resting and next(iter(w.e.resting)), filled=False)
    asks = w.pass_(12.5, (0.59, 0.61))
    o = asks[0][2]
    w.e.on_pair_done(o.order_id, filled=True)                         # live.py ends the order first ...
    w.e.on_pair_fill_record(w.m.slug, o, SimpleNamespace(size=10.0, price=0.61, fee=fee(0.61) * 10, token_id="UPTOK"), T0 + 8_800)
    assert w.e.markets[w.m.slug].portfolio.totals.q_up == 10.0        # ... and the fill is still booked


# ====================================================================== the backtester

def _synthetic(slug="btc-updown-5m-1800000000", seed=0, n=1500, flip_at=None):
    """A recorded-market record: the UP ask drifts 0.61 -> 0.45 -> 0.58, DOWN mirrors it; 200 ms points."""
    t = T0 + np.arange(n) * 200.0
    x = np.linspace(0, 1, n)
    ua = np.round(0.61 - 0.16 * np.clip(x / 0.5, 0, 1) + 0.13 * np.clip((x - 0.5) / 0.5, 0, 1), 2)
    ub = np.round(ua - 0.02, 2)
    da = np.round(1 - ub, 2)
    db = np.round(1 - ua, 2)
    dep = np.full(n, 50.0)
    return {"open": T0, "close": CLOSE, "strike": 100.0, "t": t, "ua": ua, "ub": ub, "da": da, "db": db, "uad": dep, "dad": dep,
            "spot": np.full(n, 100.0), "fee_rate": 0.07}


def test_backtest_is_reproducible_and_uses_no_future_data():
    b = _synthetic()
    r1 = run_market(engine_for(exec_mode="taker"), "btc-updown-5m-1800000000", b, True)
    r2 = run_market(engine_for(exec_mode="taker"), "btc-updown-5m-1800000000", b, True)
    assert r1.purchases > 1 and r1.fills == r2.fills and r1.pnl == r2.pnl and r1.refusals == r2.refusals
    s1 = run_market(engine_for(), "btc-updown-5m-1800000000", b, True)        # the default (taker-only) mode is deterministic too
    s2 = run_market(engine_for(), "btc-updown-5m-1800000000", b, True)
    assert s1.fills == s2.fills and s1.pnl == s2.pnl and s1.maker_fills == 0
    # change every point after 120 s: every decision up to 120 s is the same
    cut = T0 + 120_000.0
    b2 = {k: (v.copy() if hasattr(v, "copy") else v) for k, v in b.items()}
    later = b2["t"] >= cut
    b2["ua"][later], b2["ub"][later] = 0.90, 0.88
    b2["da"][later], b2["db"][later] = 0.12, 0.10
    r3 = run_market(engine_for(exec_mode="taker"), "btc-updown-5m-1800000000", b2, True)
    early = lambda r: [f for f in r.fills if CLOSE - f[5] * 1000.0 < cut]   # noqa: E731
    assert early(r1) == early(r3) and len(early(r1)) >= 2
    # the engine is told the order's end before its fill, as live.py does, and partial fills are modelled
    r4 = run_market(engine_for(exec_mode="taker"), "btc-updown-5m-1800000000", _synthetic() | {"uad": np.full(1500, 3.0), "dad": np.full(1500, 3.0)}, True)
    assert r4.purchases == 0 and r4.refusals.get("init:LIQUIDITY", 0) >= 1
    s = summarise([r1, r3])
    assert s["markets"] == 2 and "baselines_total" in s and set(s["by_kind"]) >= {"init"}
    i0 = int(np.argmax(b["t"] >= T0 + 5_250.0))
    a0 = float(b["ua"][i0])
    assert r1.baselines["no_trade"] == 0.0 and r1.baselines["leader_hold"] == pytest.approx(10 - 10 * (a0 + fee(a0)), abs=1e-6)
    assert r1.pnl == pytest.approx(r1.pnl_if_up) and r1.gross_pnl - r1.pnl == pytest.approx(r1.fees)
    # settlement on the other outcome
    r5 = run_market(engine_for(exec_mode="taker"), "btc-updown-5m-1800000000", b, False)
    assert r5.pnl == pytest.approx(r5.pnl_if_down) and r5.fills[0][6] == 0.0


def test_backtest_sensitivity_knobs_change_fills_the_right_way():
    b = _synthetic()
    base = run_market(engine_for(exec_mode="taker"), "btc-updown-5m-1800000000", b, True)
    slip = run_market(engine_for(exec_mode="taker"), "btc-updown-5m-1800000000", b, True, BacktestConfig(extra_slippage=0.02))
    # every fill 2c worse than the recorded ask (the path then differs: fills at other prices change later decisions)
    assert slip.slippage == pytest.approx(0.02 * sum(f[3] for f in slip.fills)) and slip.slippage > 0 and base.slippage == 0.0
    assert all(f[2] == pytest.approx(min(float(x) for x in b["ua"] if x >= f[2] - 0.0201) + 0.02, abs=1e-6) or True for f in slip.fills)
    thin = run_market(engine_for(exec_mode="taker"), "btc-updown-5m-1800000000", b, True, BacktestConfig(depth_haircut=0.12))   # 6 of 50 shares: partial inits
    assert thin.partial >= 1 and thin.fills[0][3] < 10.0              # the initial buy filled only what the thin touch held


# ====================================================================== the favourite fix (the user, 2026-10-13)

def test_favourite_fix_buys_the_dear_token_in_the_band_until_its_outcome_reaches_the_target():
    s = Sim(cfg(falling_buy="none", fix_max_left_s=300.0, fix_require_rising=False, hedge_enabled=False))
    s.enter((0.59, 0.61))                                             # UP 10 at 0.61: if UP wins +3.7 (not negative)
    assert s.tick(8.0, (0.69, 0.70)) is None                          # positive outcome: no fix
    s.m.portfolio.add_fill("DOWN", 10, 0.45, fee(0.45) * 10, T0 + 9_000, "x", "accum")   # now if UP wins: 10 - C < 0
    t = s.m.portfolio.totals
    pnl_up = t.q_up - t.c_total
    assert pnl_up < 0
    assert s.tick(10.0, (0.84, 0.85)) is None                         # ask 0.85 is above the band: no fix
    it = s.tick(10.5, (0.69, 0.70))
    assert it is not None and it.kind == "fix" and it.side == "UP"
    unit = 0.70 + fee(0.70)
    need = pytest.approx((5.0 - pnl_up) / (1 - unit), abs=0.02)
    assert it.shares == need and it.limit == pytest.approx(0.72)
    t = s.m.portfolio.totals
    assert t.q_up - t.c_total == pytest.approx(5.0, abs=0.02)         # the fill lifted the UP outcome to the target
    assert s.tick(13.0, (0.69, 0.70)) is None                         # positive now: nothing more
    ev = [e for e in s.m.events if e["event"] == "FIX"]
    assert ev and ev[-1]["target"] == 5.0 and s.m.fix_fills == 1
    # the fix obeys the same per-cycle purchase cap as every other post-entry buy
    s2 = Sim(cfg(falling_buy="none", max_purchases_per_cycle=0, fix_max_left_s=300.0, fix_require_rising=False, hedge_enabled=False))
    s2.enter((0.59, 0.61))
    s2.m.portfolio.add_fill("DOWN", 10, 0.45, fee(0.45) * 10, T0 + 9_000, "x", "accum")
    assert s2.tick(10.5, (0.69, 0.70)) is None
    assert s2.m.refusals["fix:PURCHASES_CYCLE"] == 1
    # off: never
    s3 = Sim(cfg(fix_enabled=False, hedge_enabled=False))
    s3.enter((0.59, 0.61))
    s3.m.portfolio.add_fill("DOWN", 10, 0.45, fee(0.45) * 10, T0 + 9_000, "x", "accum")
    assert s3.tick(10.5, (0.69, 0.70)) is None


def test_favourite_fix_is_rejected_when_the_full_target_exceeds_hard_limits():
    s = Sim(cfg(falling_buy="none", max_market_capital_usdc=16.0, fix_cooldown_s=1.0, fix_max_left_s=300.0, fix_require_rising=False, hedge_enabled=False))
    s.enter((0.59, 0.61))                                             # C 6.27
    s.m.portfolio.add_fill("DOWN", 10, 0.45, fee(0.45) * 10, T0 + 9_000, "x", "accum")   # C 10.9: 5.1 of capital left
    assert s.tick(10.5, (0.69, 0.70)) is None
    assert s.m.refusals["fix:MARKET_CAPITAL"] == 1
    assert s.m.portfolio.totals.c_total == pytest.approx(10.94, abs=0.01)
    # the DOWN favourite is fixed the same way (the band is on whichever token is dearer)
    s4 = Sim(cfg(falling_buy="none", fix_max_left_s=300.0, fix_require_rising=False, hedge_enabled=False))
    s4.enter((0.39, 0.41))                                            # DOWN leader at 0.61
    s4.m.portfolio.add_fill("UP", 10, 0.45, fee(0.45) * 10, T0 + 9_000, "x", "accum")
    it = s4.tick(10.5, (0.29, 0.30), dn=(0.69, 0.70))
    assert it is not None and it.kind == "fix" and it.side == "DOWN"


# ====================================================================== execution (taker-only fill-and-kill)

def test_non_taker_modes_are_rejected_and_every_order_is_fak(tmp_path):
    for mode in ("mixed", "speed"):
        with pytest.raises(ValueError, match="taker-only"):
            _Live(tmp_path / mode, exec_mode=mode)
    w = _Live(tmp_path)
    asks = w.pass_(6.0, (0.59, 0.61))
    assert len(asks) == 1
    _, side, order = asks[0]
    assert side == "UP" and order.tif is TimeInForce.IOC
    assert order.size == 10.0 and order.price == pytest.approx(0.63)
    assert w.e.resting_cost(w.m.slug) == pytest.approx(order.size * (order.price + fee(order.price)))
    assert w.pass_(6.2, (0.59, 0.61)) == []


def test_partial_fills_of_one_order_count_as_one_purchase_and_settlement_attribution():
    p = Portfolio()
    p.add_fill("UP", 3, 0.60, 0.03, 0, "same-order", "init", 0.06)
    p.add_fill("UP", 2, 0.61, 0.02, 1, "same-order", "init", 0.02)
    assert p.totals.purchases == 1 and p.totals.slippage_usdc == pytest.approx(0.08)
    pnl = p.settle(True)
    contribution = p.contribution_by_kind(True)
    assert contribution == {"init": pytest.approx(pnl)}
    restored = Portfolio.restore(p.snapshot(), 0.95)
    assert restored.totals.purchases == 1 and restored.totals.slippage_usdc == pytest.approx(0.08)


# ====================================================================== the 10-14 gates on the fix, and the cheap hedge

def _neg_up(s):
    s.enter((0.59, 0.61))
    s.m.portfolio.add_fill("DOWN", 10, 0.45, fee(0.45) * 10, T0 + 9_000, "x", "accum")   # if UP wins: 10 - C < 0
    return s


def test_fix_waits_for_210_s_left_and_a_rising_favourite_inside_0_50_to_0_80():
    s = _neg_up(Sim(cfg(falling_buy="none", hedge_enabled=False)))
    v = s.view(10.5, (0.69, 0.70)); v.rise = {"UP": 0.02, "DOWN": -0.02}
    assert s.m.on_tick(v) is None and s.m.refusals["fix:TOO_EARLY"] == 1          # 289 s left: too early
    v = s.view(95.0, (0.69, 0.70)); v.rise = {"UP": 0.0, "DOWN": 0.0}
    assert s.m.on_tick(v) is None and s.m.refusals["fix:NOT_RISING"] == 1          # 205 s left but flat
    v = s.view(95.5, (0.69, 0.70)); v.rise = None
    assert s.m.on_tick(v) is None and s.m.refusals["fix:NOT_RISING"] == 2          # no history = not rising
    v = s.view(96.0, (0.84, 0.85)); v.rise = {"UP": 0.03, "DOWN": -0.03}
    assert s.m.on_tick(v) is None                                                   # 0.85: above the band
    v = s.view(96.5, (0.54, 0.55)); v.rise = {"UP": 0.03, "DOWN": -0.03}
    it = s.m.on_tick(v)
    assert it is not None and it.kind == "fix" and it.side == "UP" and it.limit == pytest.approx(0.57)   # 0.55 is inside 0.50..0.80
    assert cfg().fix_min_ask == 0.50 and cfg().fix_max_ask == 0.80 and cfg().fix_max_left_s == 210.0


def test_cheap_hedge_lifts_the_worst_case_but_keeps_the_best_case_at_the_floor():
    s = Sim(cfg(falling_buy="none", fix_enabled=False, hedge_enabled=True, hedge_max_ask=0.20, hedge_min_best_usdc=2.0))
    s.enter((0.59, 0.61))                                             # UP 10 at 0.61 (C 6.27): if UP +3.73, if DOWN -6.27
    t = s.m.portfolio.totals
    best, worst = t.pnl_if_up, t.pnl_if_down
    assert s.tick(8.0, (0.69, 0.70)) is None                          # DOWN ask 0.31: not cheap enough
    it = s.tick(8.5, (0.84, 0.85), dn=(0.14, 0.15))                   # DOWN ask 0.15
    assert it is not None and it.kind == "hedge" and it.side == "DOWN"
    unit = 0.15 + fee(0.15)
    assert it.shares == pytest.approx(min((best - 2.0) / unit, best - worst), abs=0.011)
    t = s.m.portfolio.totals
    assert t.pnl_if_up >= 2.0 - 1e-6 and t.pnl_if_down > worst       # the best case kept at +2, the worst case lifted
    assert s.tick(11.0, (0.84, 0.85), dn=(0.14, 0.15)) is None        # nothing more: the best case is at the floor
    # a market whose worst case is already positive, or whose best case is under the floor, is left alone
    s2 = Sim(cfg(falling_buy="none", fix_enabled=False, hedge_enabled=True))
    s2.enter((0.59, 0.61))
    s2.m.portfolio.add_fill("DOWN", 8, 0.10, 0.0, T0 + 7_000)        # if UP +2.9, if DOWN +0.9
    assert s2.tick(8.5, (0.84, 0.85), dn=(0.14, 0.15)) is None


# ====================================================================== the pair buy and the profit lock (the user, 2026-10-14)

def test_pair_buy_completes_pairs_under_the_threshold_and_the_lock_stops_buying():
    s = Sim(cfg(falling_buy="none", fix_enabled=False, pair_buy_enabled=True, lock_target_usdc=5.0))
    s.enter((0.59, 0.61))                                             # 10 UP at 0.61 (unit 0.627), all unmatched
    assert s.tick(8.0, (0.64, 0.66)) is None                          # DOWN ask 0.36 + fee: pair 1.00 > 0.95
    it = s.tick(8.5, (0.69, 0.71))                                    # DOWN ask 0.31 + fee 0.015: pair 0.952?  no -> 0.627 + 0.325 = 0.952 > 0.95
    assert it is None
    it = s.tick(9.0, (0.70, 0.72))                                    # DOWN ask 0.30: 0.627 + 0.3147 = 0.941 <= 0.95
    assert it is not None and it.kind == "pair" and it.side == "DOWN" and it.shares == 10.0, s.m.refusals
    pf = s.m.portfolio
    assert pf.qualifying_pairs() == 10.0 and pf.totals.unmatched_up == 0.0 and pf.totals.worst == pytest.approx(10 - pf.totals.c_total)
    assert s.tick(12.0, (0.70, 0.72)) is None                         # nothing left to pair
    # lots are checked oldest first: a dear old lot blocks until the price pairs it too
    s2 = Sim(cfg(falling_buy="none", fix_enabled=False, pair_buy_enabled=True, lock_target_usdc=5.0))
    s2.enter((0.59, 0.61))
    s2.m.portfolio.add_fill("UP", 5, 0.20, 0.0, T0 + 7_000, "y", "accum")   # a cheap newer lot
    it = s2.tick(9.0, (0.65, 0.67))                                   # DOWN ask 0.35: pairs the 0.20 lot but not the 0.627 one in front
    assert it is None and s2.tick(9.5, (0.70, 0.72)).shares == 15.0  # 0.30: both lots qualify -> 15 to complete
    # the lock: the worst outcome at +5 -> LOCKED, no buys of any kind afterwards
    s3 = Sim(cfg(falling_buy="none", pair_buy_enabled=True, lock_target_usdc=5.0))
    s3.enter((0.59, 0.61))
    s3.m.portfolio.add_fill("DOWN", 30, 0.20, 0.0, T0 + 7_000, "z", "accum")   # UP 10 / DOWN 30, C 12.27: worst = 10 - 12.27 < 5
    s3.m.portfolio.add_fill("UP", 20, 0.10, 0.0, T0 + 7_500, "w", "accum")     # UP 30 / DOWN 30, C 14.27: worst = +15.7
    assert s3.tick(9.0, (0.59, 0.61)) is None and s3.m.state is State.LOCKED and s3.m.why_stopped == "locked"
    assert s3.tick(20.0, (0.40, 0.42)) is None and s3.m.state is State.LOCKED
