"""BTC_TREND_REVERSAL_HEDGE (strategy/btc_hedge.py): the flag and the absence of a real-money
switch; BTC only; the observation period; the mandatory initial entry by trend or by the cheaper
ask, its retries and the FAK fallback; that stale data and risk limits still refuse it; the
reversal needing several observations; the price-gated hedge sized to the unmatched quantity;
the MARKET_END record; hold-to-settlement; and the live bot's hooks.
"""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from troll_poly_bot.__main__ import check_btc_hedge
from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import Market, MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.live import LiveBot
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.btc_hedge import BtcHedgeConfig, BtcHedgeEngine
from troll_poly_bot.strategy.engine import EngineConfig, Reason
from troll_poly_bot.strategy.spot_lag import SpotLagConfig
from troll_poly_bot.types import BookLevel, OrderBook, TimeInForce

T0 = 1_800_000_000_000.0
SIGMA = 1e-4
FEE = FeeSchedule()


def _cfg(**kw) -> BtcHedgeConfig:
    base = dict(enabled=True)
    base.update(kw)
    return BtcHedgeConfig(**base)


def _book(token, bid, ask, size=100.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)])


class _Vol:
    ready = True
    sigma_per_sec = SIGMA


class _World:
    def __init__(self, **scfg) -> None:
        rc = RiskConfig(max_position_usdc=10.0, max_total_exposure_usdc=100.0, max_concurrent_positions=4,
                        max_asset_exposure_usdc=100.0, max_epoch_exposure_usdc=100.0, max_daily_loss_usdc=40.0)
        self.scfg = _cfg(**scfg)
        self.scfg.apply_budget(rc)
        self.risk = RiskManager(cfg=rc, starting_balance=100.0)
        self.e = BtcHedgeEngine(EngineConfig(strategy="btc_trend_reversal_hedge"), self.scfg, self.risk, FeatureEngine())

    def meta(self, asset="BTC", open_ts=T0) -> MarketMeta:
        return MarketMeta(market=Market(condition_id=f"c-{asset}-{open_ts:.0f}", asset=asset,
                                        yes_token_id=f"{asset}-{open_ts:.0f}-UP", no_token_id=f"{asset}-{open_ts:.0f}-DN",
                                        strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                                        slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}", min_size=5.0),
                          resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)

    def path(self, asset, until, bps_per_s=0.0, span_s=12.0):
        """A spot path ending at ``until`` drifting ``bps_per_s`` over its last ``span_s`` seconds."""
        t = until - 20_000.0
        while t <= until:
            drift = bps_per_s * max(0.0, (t - (until - span_s * 1000.0)) / 1000.0)
            self.e.features.update_spot(asset, 100.0 * math.exp(drift * 1e-4), t)
            t += 100.0

    def evaluate(self, meta, now, up_ask=0.50, dn_ask=0.52, spread=0.02, depth=100.0, spot_age=200.0, balance=100.0):
        m = meta.market
        view = CompositeView(m.asset, 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
        up = _book(m.yes_token_id, round(up_ask - spread, 4), up_ask, depth)
        dn = _book(m.no_token_id, round(dn_ask - spread, 4), dn_ask, depth)
        return self.e.evaluate(meta, up, dn, view, spot_age, 100.0, now, _Vol(), SimpleNamespace(ready=True), COLOCATED,
                               balance, quote_ages=None)

    def fill(self, meta, side, price, size, now):
        self.e.on_entry_submit(meta.market.slug, now)
        self.e.on_fill_detail(meta.market.slug, side, price, size, FEE.taker_fee_per_share(price) * size, now)
        self.risk.on_fill(meta.market.slug, "BTC", int(T0 // 1000), side, price * size, size)


# ─────────────────────────────── flags ───────────────────────────────


def test_off_by_default_and_real_money_is_refused():
    assert BtcHedgeConfig().enabled is False
    with pytest.raises(ValueError, match="TPB_BTC_HEDGE_ENABLED"):
        BtcHedgeEngine(EngineConfig(), BtcHedgeConfig(), RiskManager(cfg=RiskConfig(), starting_balance=50.0))
    with pytest.raises(SystemExit, match="TPB_BTC_HEDGE_ENABLED"):
        check_btc_hedge("btc_trend_reversal_hedge", "paper", env={})
    check_btc_hedge("btc_trend_reversal_hedge", "paper", env={"TPB_BTC_HEDGE_ENABLED": "1"})
    for mode in ("micro-live", "live", "shadow"):
        with pytest.raises(SystemExit, match="paper only"):
            check_btc_hedge("btc_trend_reversal_hedge", mode, env={"TPB_BTC_HEDGE_ENABLED": "1", "TPB_SPOT_LAG_ALLOW_REAL": "1"})
    check_btc_hedge("spot_lag", "live", env={})
    with pytest.raises(ValueError, match="hedge_mode"):
        BtcHedgeEngine(EngineConfig(), _cfg(hedge_mode="maybe"), RiskManager(cfg=RiskConfig(), starting_balance=50.0))


def test_the_briefs_configuration_names_read_from_its_own_prefix():
    env = {"TPB_BTC_HEDGE_ENABLED": "1", "TPB_BTC_HEDGE_BTC_ONLY": "1", "TPB_BTC_HEDGE_REQUIRE_INITIAL_TRADE": "1",
           "TPB_BTC_HEDGE_INITIAL_ENTRY_SIZE": "5", "TPB_BTC_HEDGE_INITIAL_OBSERVATION_SECONDS": "12",
           "TPB_BTC_HEDGE_MAX_PAIR_COST": "0.95", "TPB_BTC_HEDGE_MAX_INITIAL_ENTRY_PRICE": "0.6",
           "TPB_BTC_HEDGE_REVERSAL_CONFIRMATION_SECONDS": "4", "TPB_BTC_HEDGE_MAX_HEDGE_SIZE": "0",
           "TPB_BTC_HEDGE_HOLD_MATCHED_TO_SETTLEMENT": "1", "TPB_SPOT_LAG_MAX_ASK": "0.60"}
    c = BtcHedgeConfig.from_env(env)
    assert c.enabled and c.btc_only and c.require_initial_trade and c.initial_entry_size == 5.0
    assert c.initial_observation_seconds == 12.0 and c.max_pair_cost == 0.95 and c.max_initial_entry_price == 0.6
    assert c.reversal_confirmation_seconds == 4.0 and c.max_hedge_size == 0.0 and c.hold_matched_to_settlement
    assert SpotLagConfig.from_env(env).enabled is False


# ─────────────────────────────── the initial entry ───────────────────────────────


def test_only_btc_and_only_after_the_observation_period():
    w = _World()
    w.path("BTC", T0 + 5_000.0)
    assert w.evaluate(w.meta("ETH"), T0 + 5_000.0).reason is Reason.ASSET_FILTERED
    ev = w.evaluate(w.meta(), T0 + 5_000.0)
    assert ev.reason is Reason.OUTSIDE_TIME_WINDOW and "observing" in ev.detail


def test_a_trend_picks_its_side_and_no_trend_picks_the_cheaper_ask():
    w = _World()
    now = T0 + 15_000.0
    w.path("BTC", now, bps_per_s=+1.0)                 # +1 bp/s over 12 s: z5 ~ +2.2, z1 > 0
    ev = w.evaluate(w.meta(), now, up_ask=0.55, dn_ask=0.47)
    assert ev.reason is None and ev.side == "UP" and ev.features["btc_hedge"]["reason"] == "TREND_UP"
    assert ev.size == 5.0 and ev.limit_price == pytest.approx(0.56)
    assert w.e.to_order(ev, now).tif is TimeInForce.FOK
    w2 = _World()
    w2.path("BTC", now)                                 # flat
    ev = w2.evaluate(w2.meta(), now, up_ask=0.55, dn_ask=0.47)
    assert ev.reason is None and ev.side == "DOWN" and ev.features["btc_hedge"]["reason"] == "CHEAPEST_SIDE_FALLBACK"
    w3 = _World()
    w3.path("BTC", now, bps_per_s=-1.0)
    ev = w3.evaluate(w3.meta(), now, up_ask=0.40, dn_ask=0.62)
    assert ev.reason is None and ev.side == "DOWN" and ev.features["btc_hedge"]["reason"] == "TREND_DOWN"


def test_without_the_mandatory_trade_only_a_trend_enters():
    now = T0 + 15_000.0
    w = _World(require_initial_trade=False)
    w.path("BTC", now)
    assert w.evaluate(w.meta(), now, up_ask=0.55, dn_ask=0.47).reason is Reason.WEAK_MOMENTUM   # no fallback
    later = now + 15_000.0                                # a fresh, trending path (history never rewinds)
    w.path("BTC", later, bps_per_s=+1.0)
    ev = w.evaluate(w.meta(), later, up_ask=0.55, dn_ask=0.47)
    assert ev.reason is None and ev.features["btc_hedge"]["reason"] == "TREND_UP"


def test_stale_data_the_price_cap_and_risk_limits_still_refuse_the_mandatory_trade():
    now = T0 + 15_000.0
    w = _World()
    w.path("BTC", now)
    assert w.evaluate(w.meta(), now, spot_age=5_000.0).reason is Reason.STALE_DATA
    assert w.evaluate(w.meta(), now, up_ask=0.70, dn_ask=0.72).reason is Reason.PRICE_BAND      # both above 0.65
    assert w.evaluate(w.meta(), now, up_ask=0.50, dn_ask=0.52, depth=2.0).reason is Reason.LOW_LIQUIDITY   # 2 + 2 < 5
    w.risk.cfg.max_daily_loss_usdc = 1.0
    w.risk.realised_today = -2.0
    assert w.evaluate(w.meta(), now).reason is Reason.RISK_LIMIT


def test_kills_are_retried_then_sent_as_fak_then_given_up():
    w = _World(initial_attempts=4, initial_fak_after=2)
    meta = w.meta()
    now = T0 + 15_000.0
    w.path("BTC", now)
    for k in range(4):
        w.path("BTC", now + k * 2_100.0)                    # the spot stays fresh through the retries
        ev = w.evaluate(meta, now + k * 2_100.0)
        assert ev.reason is None, (k, ev.reason, ev.detail)
        assert w.e.to_order(ev, now).tif is (TimeInForce.IOC if k >= 2 else TimeInForce.FOK)
        w.e.on_entry_submit(meta.market.slug, now + k * 2_100.0)
        assert w.evaluate(meta, now + k * 2_100.0 + 100.0).reason is Reason.COOLDOWN         # one order in flight
        w.e.on_entry_done(meta.market.slug, now + k * 2_100.0 + 500.0)
    w.path("BTC", now + 9_000.0)
    assert w.evaluate(meta, now + 9_000.0).reason is Reason.MARKET_ATTEMPTS
    assert w.e.markets[meta.market.slug].attempts == 4


# ─────────────────────────────── reversal and hedge ───────────────────────────────


def _held(w, meta, now, side="UP", price=0.52):
    w.path("BTC", now, bps_per_s=+1.0 if side == "UP" else -1.0)
    ev = w.evaluate(meta, now, up_ask=0.52, dn_ask=0.50)
    assert ev.reason is None and ev.side == side
    w.fill(meta, side, price, 5.0, now + 400.0)
    return now + 400.0


def test_a_reversal_needs_several_observations_then_the_hedge_is_price_gated():
    w = _World(max_pair_cost=0.97, reversal_confirmation_seconds=3.0)
    meta = w.meta()
    t = _held(w, meta, T0 + 15_000.0)                   # 5 UP @ 0.52
    st = w.e.markets[meta.market.slug]
    assert w.evaluate(meta, t + 200.0, up_ask=0.55, dn_ask=0.47).reason is Reason.ALREADY_POSITIONED
    # BTC turns down: the first observation against is not a reversal
    w.path("BTC", t + 8_000.0, bps_per_s=-1.5, span_s=6.0)
    assert w.evaluate(meta, t + 8_000.0, up_ask=0.50, dn_ask=0.52).reason is Reason.ALREADY_POSITIONED
    assert st.reversal is False and st.against_since is not None
    # three seconds of it: confirmed; DOWN at 0.52 makes the pair 0.52 + 0.52 + fee + 1c > 0.97: WAIT
    w.path("BTC", t + 11_500.0, bps_per_s=-1.5, span_s=9.5)
    ev = w.evaluate(meta, t + 11_500.0, up_ask=0.50, dn_ask=0.52)
    assert st.reversal is True and ev.reason is Reason.EDGE_TOO_SMALL and "WAIT" in ev.detail
    # later the DOWN ask is 0.42: pair = 0.52 + 0.42 + fee(0.42) + 0.01 = 0.967 -> HEDGE, sized to the unmatched 5
    w.path("BTC", t + 14_000.0, bps_per_s=-1.5, span_s=12.0)
    ev = w.evaluate(meta, t + 14_000.0, up_ask=0.60, dn_ask=0.42)
    assert ev.reason is None and ev.side == "DOWN" and ev.size == 5.0
    assert ev.features["btc_hedge"]["pair_cost"] == pytest.approx(0.52 + 0.42 + FEE.taker_fee_per_share(0.42) + 0.01, abs=1e-4)
    w.fill(meta, "DOWN", 0.42, 5.0, t + 14_400.0)
    # matched: nothing more is bought on either side
    w.path("BTC", t + 17_000.0, bps_per_s=-1.5, span_s=15.0)
    ev = w.evaluate(meta, t + 17_000.0, up_ask=0.60, dn_ask=0.30)
    assert ev.reason is Reason.ALREADY_POSITIONED
    assert st.unmatched == pytest.approx(0.0)


def test_hedge_modes():
    now = T0 + 15_000.0
    for mode, want in (("none", Reason.ALREADY_POSITIONED), ("unconditional", None), ("gated", Reason.EDGE_TOO_SMALL)):
        w = _World(hedge_mode=mode, max_pair_cost=0.90)
        meta = w.meta()
        t = _held(w, meta, now)
        for dt in (8_000.0, 9_500.0, 12_000.0):             # a reversal needs several observations against
            w.path("BTC", t + dt, bps_per_s=-1.5, span_s=dt / 1000.0 - 2.0)
            ev = w.evaluate(meta, t + dt, up_ask=0.50, dn_ask=0.52)
        assert ev.reason is want, (mode, ev.reason, ev.detail)


def test_the_hedge_never_exceeds_the_unmatched_quantity():
    w = _World(max_pair_cost=0.99)
    meta = w.meta()
    t = _held(w, meta, T0 + 15_000.0)
    w.fill(meta, "DOWN", 0.40, 3.0, t + 1_000.0)          # a partial hedge already: 2 unmatched
    for dt in (8_000.0, 9_500.0, 12_000.0):
        w.path("BTC", t + dt, bps_per_s=-1.5, span_s=dt / 1000.0 - 2.0)
        ev = w.evaluate(meta, t + dt, up_ask=0.60, dn_ask=0.40)
    assert ev.reason is Reason.SIZE_ZERO                  # 2 shares is below the venue minimum: no order


def test_market_end_records_both_legs_and_the_settlement():
    w = _World(max_pair_cost=0.99)
    meta = w.meta()
    t = _held(w, meta, T0 + 15_000.0)                     # 5 UP @ 0.52
    w.fill(meta, "DOWN", 0.42, 5.0, t + 5_000.0)
    w.e.on_market_settled(meta.market.slug, True, 0.0, t + 300_000.0)
    rec = w.e.ended[-1]
    fees = FEE.taker_fee_per_share(0.52) * 5 + FEE.taker_fee_per_share(0.42) * 5
    assert rec["initial_side"] == "UP" and rec["initial_filled"] == 5.0 and rec["hedge_filled"] == 5.0
    assert rec["matched_qty"] == 5.0 and rec["unmatched_qty"] == 0.0
    assert rec["first_leg_cost"] == pytest.approx(2.6) and rec["second_leg_cost"] == pytest.approx(2.1)
    assert rec["payout"] == pytest.approx(5.0) and rec["fees"] == pytest.approx(fees, abs=1e-4)
    assert rec["net_pnl"] == pytest.approx(5.0 - 4.7 - fees, abs=1e-4)
    # unhedged and lost
    w2 = _World()
    meta2 = w2.meta(open_ts=T0 + 600_000.0)
    t2 = _held(w2, meta2, T0 + 615_000.0)
    w2.e.on_market_settled(meta2.market.slug, False, -2.6, t2 + 300_000.0)
    rec = w2.e.ended[-1]
    assert rec["unmatched_qty"] == 5.0 and rec["payout"] == 0.0 and rec["net_pnl"] == pytest.approx(-2.6 - FEE.taker_fee_per_share(0.52) * 5, abs=1e-4)


def test_positions_are_never_sold_before_settlement():
    w = _World()
    for bid in (0.01, 0.5, 0.99):
        assert w.e.exit_decision(asset="BTC", side="UP", token_id="t", entry=0.5, bid=bid, now_ms=T0) is None


def test_the_live_bot_runs_it_with_its_hooks(tmp_path):
    cfg = BotConfig()
    cfg.engine.strategy = "btc_trend_reversal_hedge"
    cfg.execution_scenario = "venue_lag"
    cfg.deploy_mode = "paper"
    cfg.btc_hedge = _cfg()
    bot = LiveBot(assets=("BTC",), balance=100.0, cfg=cfg, state_path=str(tmp_path / "s.json"),
                  trade_log=str(tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"), kill_file=str(tmp_path / "KILL"))
    assert type(bot.engine) is BtcHedgeEngine and bot.engine.stacks_positions is True
    assert bot.risk.cfg.max_concurrent_positions == 2 and bot.risk.cfg.max_position_usdc <= 10.0
    assert callable(getattr(bot.engine, "on_fill_detail")) and callable(getattr(bot.engine, "on_market_settled"))
    assert bot.engine.snapshot()["strategy"] == "btc_trend_reversal_hedge"
    assert cfg.spot_lag.enabled is False and cfg.fastclip.enabled is False


# ─────────────────── pairs: repeat, leg gap, minimum pair edge (2026-10-01) ───────────────────

def test_a_second_pair_starts_after_the_first_is_matched_and_the_gap_has_passed():
    w = _World(max_pair_cost=0.99, max_pairs_per_market=2, min_seconds_between_legs=5.0, require_initial_trade=False)
    meta = w.meta()
    t = _held(w, meta, T0 + 15_000.0)                     # pair 1 first leg: 5 UP @ 0.52
    st = w.e.markets[meta.market.slug]
    for dt in (8_000.0, 9_500.0, 12_000.0):
        w.path("BTC", t + dt, bps_per_s=-1.5, span_s=dt / 1000.0 - 2.0)
        ev = w.evaluate(meta, t + dt, up_ask=0.60, dn_ask=0.40)
    assert ev.reason is None and ev.side == "DOWN"        # the hedge, 5 s after the first leg
    w.fill(meta, "DOWN", 0.40, 5.0, t + 12_400.0)
    assert st.pairs == 1 and st.unmatched == pytest.approx(0.0)
    # inside the leg gap: nothing new; after it, a fresh DOWN trend opens pair 2's first leg
    w.path("BTC", t + 14_000.0, bps_per_s=-1.5, span_s=6.0)
    assert w.evaluate(meta, t + 14_000.0, up_ask=0.60, dn_ask=0.40).reason is Reason.ALREADY_POSITIONED
    w.path("BTC", t + 19_000.0, bps_per_s=-1.5, span_s=17.0)
    ev = w.evaluate(meta, t + 19_000.0, up_ask=0.60, dn_ask=0.40)
    assert ev.reason is None and ev.side == "DOWN" and st.first_qty == 0.0 and len(st.pair_log) == 1, (ev.reason, ev.detail)
    w.fill(meta, "DOWN", 0.40, 5.0, t + 19_400.0)
    assert st.side == "DOWN" and st.first_qty == 5.0 and st.dn_qty == 10.0 and st.up_qty == 5.0
    # settlement from the per-side totals: UP wins -> 5 UP pay, 10 DOWN do not
    w.e.on_market_settled(meta.market.slug, True, 0.0, t + 300_000.0)
    rec = w.e.ended[-1]
    assert rec["payout"] == pytest.approx(5.0) and rec["total_cost"] == pytest.approx(2.6 + 2.0 + 2.0) and rec["pairs"] == 1


def test_a_hedge_waits_for_the_leg_gap_and_the_minimum_pair_edge():
    w = _World(max_pair_cost=0.99, min_pair_edge=0.05, min_seconds_between_legs=20.0)
    meta = w.meta()
    t = _held(w, meta, T0 + 15_000.0)
    for dt in (8_000.0, 9_500.0, 12_000.0):
        w.path("BTC", t + dt, bps_per_s=-1.5, span_s=dt / 1000.0 - 2.0)
        ev = w.evaluate(meta, t + dt, up_ask=0.60, dn_ask=0.40)
    assert ev.reason is Reason.COOLDOWN and "waits" in ev.detail        # the reversal is confirmed, the gap is not over
    w.path("BTC", t + 21_000.0, bps_per_s=-1.5, span_s=19.0)
    ev = w.evaluate(meta, t + 21_000.0, up_ask=0.60, dn_ask=0.44)      # pair 0.52 + 0.44 + fee + 1c = 0.987 > 0.95 cap
    assert ev.reason is Reason.EDGE_TOO_SMALL and "> 0.95" in ev.detail
    ev = w.evaluate(meta, t + 21_200.0, up_ask=0.60, dn_ask=0.40)      # 0.947 <= 0.95
    assert ev.reason is None and ev.side == "DOWN"


def test_one_pair_by_default_then_hold():
    w = _World(max_pair_cost=0.99)
    meta = w.meta()
    t = _held(w, meta, T0 + 15_000.0)
    w.fill(meta, "DOWN", 0.40, 5.0, t + 5_000.0)
    w.path("BTC", t + 9_000.0, bps_per_s=-1.5, span_s=6.0)
    ev = w.evaluate(meta, t + 9_000.0, up_ask=0.60, dn_ask=0.40)
    assert ev.reason is Reason.ALREADY_POSITIONED and "1 pair" in ev.detail


def test_the_hedge_may_add_to_an_open_market_under_the_risk_manager():
    """2026-10-01 01:37 UTC: 18 hedges were refused RISK_LIMITS (ALREADY_POSITIONED) by a process running an
    older live.py; the engine stacks positions, so the risk check must accept the opposite side."""
    from troll_poly_bot.risk.limits import RiskConfig, RiskManager
    assert BtcHedgeEngine.stacks_positions is True
    r = RiskManager(cfg=RiskConfig(max_position_usdc=10.0, max_asset_exposure_usdc=40.0, max_epoch_exposure_usdc=40.0,
                                   max_total_exposure_usdc=40.0, max_concurrent_positions=4), starting_balance=100.0)
    r.on_fill("btc-x", "BTC", 1, "UP", 3.15, 5.0)                       # the first leg
    v = r.check("btc-x", "BTC", 1, 2.1, add_to_open=BtcHedgeEngine.stacks_positions)
    assert v.ok, (v.reason, v.detail)
    assert not r.check("btc-x", "BTC", 1, 2.1, add_to_open=False).ok    # the one-position-per-market default
