"""SPOT_LAG across many 5-minute markets at once: every market (asset x window) is an
independent opportunity; one entry per market; global exposure, concurrency, hourly,
daily-loss, spread, slippage and fill limits; independent exits; restart restore; and
the kill switch. Two layers are tested:

* the engine's gates, driven market by market (``_World``), and
* the live bot's real trading loop in paper mode (``_Bot``): entries go through
  ``LiveBot._tick`` into the paper exchange (venue_lag fills, measured-latency draws),
  exits through its timed-exit path, bookkeeping through ``_on_result``.
"""
from __future__ import annotations

import json
import math
import random

import pytest

from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.execution.paper import OrderResult, RejectReason
from troll_poly_bot.features.engine import FeatureEngine
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.live import LiveBot, LiveBook, LiveMarket
from troll_poly_bot.pricing.twap import TwapState
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.engine import EngineConfig, Evaluation, Reason, SideView
from troll_poly_bot.strategy.spot_lag import SpotLagConfig, SpotLagEngine, skip_reason_text
from troll_poly_bot.types import BookLevel, Market, Order, OrderBook, Side, TimeInForce

T0 = 1_800_000_000_000.0             # a window open, ms
SIGMA = 1e-4
FEE = FeeSchedule()


class _Vol:
    ready = True
    sigma_per_sec = SIGMA


def _cfg(**kw) -> SpotLagConfig:
    base = dict(enabled=True, exclude_assets="")
    base.update(kw)
    return SpotLagConfig(**base)


def _book(token: str, bid: float, ask: float, size: float = 100.0) -> OrderBook:
    return OrderBook(token, bids=[BookLevel(bid, size)], asks=[BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)])


# ───────────────────────── the engine, market by market ─────────────────────────


class _World:
    """Spot paths per asset and one engine; each market gets a shock at ``t``."""

    def __init__(self, scfg: SpotLagConfig | None = None, risk_cfg: RiskConfig | None = None) -> None:
        rc = risk_cfg or RiskConfig(max_position_usdc=5.0, max_total_exposure_usdc=50.0, max_concurrent_positions=10,
                                    max_asset_exposure_usdc=50.0, max_epoch_exposure_usdc=50.0,
                                    max_daily_loss_usdc=10.0, micro_fixed_size=True)
        self.scfg = scfg or _cfg()
        self.scfg.apply_budget(rc)
        self.risk = RiskManager(cfg=rc, starting_balance=50.0)
        self.e = SpotLagEngine(EngineConfig(strategy="spot_lag"), self.scfg, self.risk, FeatureEngine())
        self.px: dict[str, float] = {}
        self.t: dict[str, float] = {}
        self.twap: dict[str, TwapState] = {}

    def meta(self, asset: str, open_ts: float) -> MarketMeta:
        strike = self.px.get(asset, 100.0)
        return MarketMeta(market=Market(condition_id=f"c-{asset}-{open_ts:.0f}", asset=asset,
                                        yes_token_id=f"{asset}-{open_ts:.0f}-UP", no_token_id=f"{asset}-{open_ts:.0f}-DN",
                                        strike=strike, open_ts=open_ts, close_ts=open_ts + 300_000.0,
                                        slug=f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}", min_size=5.0),
                          resolution_source="", accepting_orders=True, duration_s=300.0, fee=FEE)

    def _feed(self, asset: str, until: float, jump_start: float | None = None, jump_bps: float = 0.0) -> None:
        tw = self.twap.setdefault(asset, TwapState())
        px0 = self.px.setdefault(asset, 100.0)
        t = self.t.get(asset, until - 70_000.0)
        while t < until:
            t += 200.0
            if jump_start is not None and t > jump_start:
                frac = min(1.0, (t - jump_start) / 800.0)
                px = px0 * math.exp(jump_bps * 1e-4 * frac)
            else:
                px = px0
            self.e.features.update_spot(asset, px, t)
            tw.update(px, t)
        self.t[asset] = t
        if jump_start is not None:
            self.px[asset] = px0 * math.exp(jump_bps * 1e-4)

    def evaluate(self, meta: MarketMeta, now: float, up=(0.50, 0.52), balance: float = 50.0):
        m = meta.market
        view = CompositeView(m.asset, 100.0, 100.0, 3, 1.0, 1.0, 50.0, {"binance": 100.0})
        down = (round(1 - up[1], 2), round(1 - up[0], 2))
        return self.e.evaluate(meta, _book(m.yes_token_id, *up), _book(m.no_token_id, *down), view, 200.0, 100.0,
                               now, _Vol(), self.twap[m.asset], COLOCATED, balance)

    def signal(self, meta: MarketMeta, t: float, jump_bps: float = 6.0, up=(0.50, 0.52), balance: float = 50.0):
        """History 1 s before ``t`` on a flat, then a shock that ends at ``t``."""
        asset = meta.market.asset
        self.px[asset] = meta.market.strike              # a fresh window starts at its strike
        self._feed(asset, t - 1000.0)
        self.evaluate(meta, t - 1000.0, up=(0.50, 0.52), balance=balance)
        self._feed(asset, t, jump_start=t - 800.0, jump_bps=jump_bps)
        return self.evaluate(meta, t, up=up, balance=balance)

    # what the live bot does around the engine
    def submit(self, ev: Evaluation, t: float) -> None:
        epoch = int(ev.slug.rsplit("-", 1)[1])
        self.risk.on_submit(ev.slug, usdc=ev.limit_price * ev.size, asset=ev.asset, epoch=epoch)
        self.e.on_entry_submit(ev.slug, t)

    def fill(self, ev: Evaluation, t: float) -> None:
        epoch = int(ev.slug.rsplit("-", 1)[1])
        self.risk.on_fill(ev.slug, ev.asset, epoch, ev.side, ev.expected_price * ev.size, ev.size)
        self.e.on_entry_fill(ev.slug, ev.token_id, t)

    def kill(self, ev: Evaluation) -> None:
        self.risk.on_done(ev.slug)

    def close(self, ev: Evaluation, pnl: float, t: float) -> None:
        self.risk.on_settle(ev.slug, pnl, t / 1000.0)
        self.e.on_exit(ev.slug, ev.token_id, t)


ASSETS = ("BTC", "ETH", "SOL", "XRP")


def test_1_2_3_markets_are_independent_and_positions_coexist():
    w = _World()
    t = T0 + 120_000.0
    evs = [w.signal(w.meta(a, T0), t) for a in ASSETS]
    assert all(ev.tradeable for ev in evs), [(ev.asset, ev.reason, ev.detail) for ev in evs]
    for ev in evs:
        w.submit(ev, t)
        w.fill(ev, t)
    # four markets, four positions, one per market -- BTC open did not block ETH, ETH did not block SOL
    assert sorted(w.risk.open) == sorted(ev.slug for ev in evs)
    assert len({ev.token_id for ev in evs}) == 4 and len({ev.slug for ev in evs}) == 4


def test_btc_position_does_not_block_eth_but_the_same_market_is_entered_once():
    w = _World()
    t = T0 + 120_000.0
    btc = w.meta("BTC", T0)
    ev = w.signal(btc, t)
    w.submit(ev, t)
    w.fill(ev, t)
    eth = w.signal(w.meta("ETH", T0), t + 3000.0)
    assert eth.tradeable, (eth.reason, eth.detail)
    # 4. the entered market takes no second initial entry, even after its position closed
    w.close(ev, 0.1, t + 6000.0)
    again = w.signal(btc, t + 60_000.0)
    assert again.reason is Reason.MARKET_ENTERED
    assert skip_reason_text(again.reason, "") == "already entered this market"
    # the next BTC window is a new market
    assert w.signal(w.meta("BTC", T0 + 300_000.0), t + 300_000.0).tradeable


def test_5_twelve_plus_markets_an_hour_each_get_their_entry():
    w = _World()
    entered = 0
    for k in range(14):                                   # 14 consecutive BTC windows = 70 minutes
        open_ts = T0 + k * 300_000.0
        ev = w.signal(w.meta("BTC", open_ts), open_ts + 120_000.0)
        assert ev.tradeable, (k, ev.reason, ev.detail)
        w.submit(ev, open_ts + 120_000.0)
        w.fill(ev, open_ts + 120_000.0)
        w.close(ev, 0.05, open_ts + 126_000.0)
        entered += 1
    assert entered == 14 and w.e.buys_last_hour(T0 + 13 * 300_000.0 + 120_000.0) >= 12


def test_the_hourly_buy_budget_is_global_and_explicit():
    w = _World(_cfg(max_buys_per_hour=12))
    for k in range(12):
        open_ts = T0 + k * 300_000.0
        ev = w.signal(w.meta("BTC", open_ts), open_ts + 120_000.0)
        assert ev.tradeable
        w.submit(ev, open_ts + 120_000.0)
        w.kill(ev)                                       # killed attempts count too
    # 12 buys since T0+120 s; the 13th inside the same hour is refused, in ANY market
    ev = w.signal(w.meta("ETH", T0 + 3_000_000.0), T0 + 3_000_000.0 + 120_000.0)
    assert ev.reason is Reason.HOURLY_BUY_LIMIT
    # an hour after the first one the budget has room again
    later = T0 + 120_000.0 + 3_600_000.0 + 1000.0
    ev = w.signal(w.meta("SOL", later - 120_000.0), later)
    assert ev.tradeable, (ev.reason, ev.detail)


def test_a_market_has_a_limited_number_of_attempts():
    w = _World(_cfg(max_attempts_per_market=2))
    m = w.meta("BTC", T0)
    for k in range(2):
        ev = w.signal(m, T0 + 60_000.0 + k * 10_000.0)
        assert ev.tradeable
        w.submit(ev, T0 + 60_000.0 + k * 10_000.0)
        w.kill(ev)                                        # 11. a kill frees the market's slot...
    third = w.signal(m, T0 + 90_000.0)
    assert third.reason is Reason.MARKET_ATTEMPTS         # ...but not forever
    assert w.signal(w.meta("ETH", T0), T0 + 90_000.0).tradeable   # and never another market's


def test_6_the_global_exposure_budget_counts_working_orders_and_is_never_exceeded():
    # 5 shares at a cap near 0.54 is ~2.7 USDC worst case; a 6 USDC budget fits two
    w = _World(_cfg(max_total_exposure_usdc=6.0))
    t = T0 + 120_000.0
    a = w.signal(w.meta("BTC", T0), t)
    b = w.signal(w.meta("ETH", T0), t)
    assert a.tradeable and b.tradeable
    w.submit(a, t)
    w.submit(b, t)                                        # both still in flight: not filled yet
    c = w.signal(w.meta("SOL", T0), t)
    assert c.reason is Reason.EXPOSURE_CAP and "exposure left" in c.detail
    assert "insufficient bankroll" in skip_reason_text(c.reason, c.detail)
    assert w.risk.exposure_total() <= 6.0 + 1e-9


def test_6_randomised_many_markets_never_exceed_the_budgets():
    rng = random.Random(11)
    w = _World(_cfg(max_total_exposure_usdc=10.0, max_simultaneous_positions=3, max_buys_per_hour=1000))
    live = []
    peak_exp, peak_n = 0.0, 0
    t = T0 + 60_000.0
    for step in range(300):
        t += rng.choice((200.0, 1000.0, 3000.0))
        open_ts = T0 + ((t - T0) // 300_000.0) * 300_000.0
        if t - open_ts > 240_000.0:
            continue
        asset = rng.choice(ASSETS)
        ev = w.signal(w.meta(asset, open_ts), t, up=(0.50, rng.choice((0.52, 0.53, 0.55))))
        if ev.tradeable:
            w.submit(ev, t)
            if rng.random() < 0.3:
                w.fill(ev, t)
                live.append(ev)
            else:
                w.kill(ev)
        while live and rng.random() < 0.3:
            w.close(live.pop(0), rng.uniform(-0.3, 0.3), t)
        peak_exp = max(peak_exp, w.risk.exposure_total())
        peak_n = max(peak_n, len(w.risk.open) + len(w.risk.pending - set(w.risk.open)))
    assert peak_exp <= 10.0 + 1e-9 and peak_n <= 3


def test_12_a_sold_position_frees_exposure_for_another_market():
    w = _World(_cfg(max_total_exposure_usdc=6.0))
    t = T0 + 120_000.0
    a, b = w.signal(w.meta("BTC", T0), t), w.signal(w.meta("ETH", T0), t)
    for ev in (a, b):
        w.submit(ev, t)
        w.fill(ev, t)
    assert w.signal(w.meta("SOL", T0), t).reason is Reason.EXPOSURE_CAP
    w.close(a, 0.08, t + 5500.0)
    assert w.signal(w.meta("XRP", T0), t + 6000.0).tradeable


def test_the_concurrency_cap_is_global():
    w = _World(_cfg(max_simultaneous_positions=2))
    t = T0 + 120_000.0
    a, b = w.signal(w.meta("BTC", T0), t), w.signal(w.meta("ETH", T0), t)
    for ev in (a, b):
        w.submit(ev, t)
        w.fill(ev, t)
    c = w.signal(w.meta("SOL", T0), t)
    assert c.reason is Reason.RISK_LIMIT and "positions open" in c.detail


def test_7_the_daily_loss_limit_stops_every_market():
    w = _World(_cfg(max_daily_loss_usdc=10.0))
    w.risk.on_settle("x", -10.0)
    ev = w.signal(w.meta("BTC", T0), T0 + 120_000.0)
    assert ev.reason is Reason.RISK_LIMIT and "daily loss" in ev.detail


def test_8_9_spread_and_slippage_protection_still_hold():
    w = _World()
    wide = w.signal(w.meta("BTC", T0), T0 + 120_000.0, up=(0.48, 0.52))          # 4c spread > 3c
    assert wide.reason is Reason.LOW_LIQUIDITY and skip_reason_text(wide.reason, wide.detail).startswith("spread too wide")
    ok = w.signal(w.meta("ETH", T0), T0 + 120_000.0)
    assert ok.tradeable and ok.limit_price <= 0.52 + 0.02 + 1e-9 and ok.limit_price <= ok.max_entry_price + 1e-9
    w2 = _World(_cfg(max_slippage=0.01))
    tight = w2.signal(w2.meta("BTC", T0), T0 + 120_000.0)
    assert tight.tradeable and tight.limit_price <= 0.52 + 0.01 + 1e-9


def test_order_size_usdc_and_the_per_order_cap():
    w = _World(_cfg(order_size_usdc=4.0, max_order_usdc=5.0))
    ev = w.signal(w.meta("BTC", T0), T0 + 120_000.0)
    assert ev.tradeable and ev.size * ev.limit_price <= 5.0 + 1e-9 and ev.size > 5.0   # ~4 USDC, not 5 shares
    w2 = _World(_cfg(max_order_usdc=2.0))                  # the 5-share minimum (~2.7 USDC) cannot fit
    ev2 = w2.signal(w2.meta("BTC", T0), T0 + 120_000.0)
    assert ev2.reason is Reason.RISK_LIMIT and "order cap" in ev2.detail


def test_budget_only_tightens_risk_limits():
    rc = RiskConfig(max_concurrent_positions=1, max_total_exposure_usdc=10.0, max_daily_loss_usdc=10.0,
                    max_position_usdc=5.0)
    _cfg(max_simultaneous_positions=4, max_total_exposure_usdc=50.0, max_daily_loss_usdc=40.0,
         max_order_usdc=8.0).apply_budget(rc, order_ceiling=5.0)
    # a micro-live ceiling already in the config stays: the budget cannot lift it
    assert (rc.max_concurrent_positions, rc.max_total_exposure_usdc, rc.max_daily_loss_usdc) == (1, 10.0, 10.0)
    assert rc.max_position_usdc == 5.0                     # never above the micro-live order ceiling
    rc2 = RiskConfig(max_position_usdc=2.5)                 # the default scaled to a 50 USDC bankroll
    _cfg().apply_budget(rc2)
    assert (rc2.max_concurrent_positions, rc2.max_total_exposure_usdc, rc2.max_position_usdc) == (4, 10.0, 5.0)


def test_budget_values_come_from_the_environment():
    cfg = SpotLagConfig.from_env({"TPB_SPOT_LAG_ENABLED": "1", "TPB_SPOT_LAG_MAX_SIMULTANEOUS_POSITIONS": "6",
                                  "TPB_SPOT_LAG_MAX_BUYS_PER_HOUR": "24", "TPB_SPOT_LAG_MAX_ENTRIES_PER_MARKET": "1",
                                  "TPB_SPOT_LAG_ORDER_SIZE_USDC": "3", "TPB_SPOT_LAG_MAX_TOTAL_EXPOSURE_USDC": "20",
                                  "TPB_SPOT_LAG_MAX_DAILY_LOSS_USDC": "8"})
    assert (cfg.max_simultaneous_positions, cfg.max_buys_per_hour, cfg.max_entries_per_market) == (6, 24, 1)
    assert (cfg.order_size_usdc, cfg.max_total_exposure_usdc, cfg.max_daily_loss_usdc) == (3.0, 20.0, 8.0)
    assert isinstance(cfg.max_buys_per_hour, int)


# ─────────────────────────── the live bot's loop, paper ───────────────────────────


class _Bot:
    """A paper LiveBot with spot_lag, a fake clock and hand-fed markets. The engine's
    signal is stubbed so the test drives the BOT's plumbing: the per-market loop,
    confirmation, submission, venue_lag fills, the timed exit and the bookkeeping."""

    def __init__(self, tmp_path, scenario="venue_lag", **scfg) -> None:
        cfg = BotConfig()
        cfg.engine.strategy = "spot_lag"
        cfg.execution_scenario = scenario
        cfg.deploy_mode = "paper"
        cfg.spot_lag = _cfg(**scfg)
        self.clock_t = T0 + 100_000.0
        self.tmp = tmp_path
        self.bot = LiveBot(assets=ASSETS, balance=50.0, cfg=cfg, state_path=str(tmp_path / "live_state.json"),
                           trade_log=str(tmp_path / "live_trades.jsonl"), earnings_path=str(tmp_path / "e.jsonl"),
                           kill_file=str(tmp_path / "KILL"))
        self.bot.clock.now = lambda: self.clock_t
        self.want: set[str] = set()                     # slugs whose next evaluation signals
        eng = self.bot.engine
        real_eval = eng.evaluate

        def evaluate(meta, book_up, book_down, *a, **kw):
            m = meta.market
            now = kw["now"] if "now" in kw else a[4]
            ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=m.seconds_remaining(now), reason=None)
            if m.slug not in self.want:
                return eng._reject(ev, Reason.WEAK_MOMENTUM)
            self.want.discard(m.slug)
            ev.features = {"spot_lag": {"signal": True, "z1": 3.5, "ev": 0.60, "ask": 0.52, "lag": 0.08}}
            if eng.trades.get(m.slug, 0) >= int(eng.scfg.max_entries_per_market):
                return eng._reject(ev, Reason.MARKET_ENTERED, "entered")
            if not self.bot.risk.check(m.slug, m.asset, int(m.open_ts // 1000), 5 * 0.54).ok:
                return eng._reject(ev, Reason.RISK_LIMIT, self.bot.risk.check(m.slug, m.asset,
                                                                               int(m.open_ts // 1000), 5 * 0.54).detail)
            sv = SideView(side="UP", token_id=m.yes_token_id, p_model=0.60, p_market=0.51, ask=0.52, bid=0.50,
                          depth_at_limit=100.0, costs=None, reason=None, entry_px=0.52)
            ev.sides = [sv]
            ev.side, ev.token_id, ev.limit_price, ev.expected_price = "UP", m.yes_token_id, 0.54, 0.52
            ev.size, ev.net_edge, ev.confidence, ev.p_used = 5.0, 0.03, 1.0, 0.60
            return ev

        eng.evaluate = evaluate
        eng.revalidate = lambda ev, book, min_edge=None: None
        self._real_eval = real_eval

    def add_market(self, asset: str, open_ts: float, bid=0.50, ask=0.52) -> LiveMarket:
        slug = f"{asset.lower()}-updown-5m-{int(open_ts // 1000)}"
        m = Market(condition_id=f"c-{slug}", asset=asset, yes_token_id=f"{slug}-UP", no_token_id=f"{slug}-DN",
                   strike=100.0, open_ts=open_ts, close_ts=open_ts + 300_000.0, tick_size=0.01, min_size=5.0, slug=slug)
        lm = LiveMarket(meta=MarketMeta(market=m, resolution_source="", accepting_orders=True, duration_s=300.0,
                                        fee=FEE))
        for tok, (b, a) in ((m.yes_token_id, (bid, ask)), (m.no_token_id, (round(1 - ask, 2), round(1 - bid, 2)))):
            lb = LiveBook(tok)
            lb.apply_snapshot({"bids": [{"price": str(b), "size": "100"}], "asks": [{"price": str(a), "size": "100"}],
                               "timestamp": str(int(self.clock_t))})
            lm.books[tok] = lb
            self.bot.token_index[tok] = (slug, lm)
        self.bot.markets[slug] = lm
        self.bot.twap.setdefault(asset, TwapState())
        return lm

    def tick(self, dt_ms: float = 200.0) -> None:
        self.clock_t += dt_ms
        for lm in self.bot.markets.values():            # books and spot stay fresh
            for lb in lm.books.values():
                lb.ts = lb.recv_ts = self.clock_t
        for a in ASSETS:
            self.bot.spot_ts[a] = self.clock_t
        self.bot._tick()                                   # noqa: SLF001

    def run(self, ms: float) -> None:
        for _ in range(int(ms // 200)):
            self.tick()

    @property
    def submitted(self) -> int:
        return self.bot.exchange.n_submitted


def test_the_loop_enters_several_markets_in_one_tick_and_manages_each(tmp_path):
    b = _Bot(tmp_path)
    lms = [b.add_market(a, T0) for a in ("BTC", "ETH", "SOL")]
    b.want |= {lm.meta.market.slug for lm in lms}
    b.tick()
    assert b.submitted == 3                                # one BUY per market, same tick, none blocked
    b.run(2_000)                                           # latency + venue_lag delay, then the fills land
    assert sorted(b.bot.risk.open) == sorted(lm.meta.market.slug for lm in lms)
    assert all(b.bot.exchange.positions[lm.meta.market.yes_token_id].shares == 5.0 for lm in lms)
    b.run(8_000)                                           # the 5 s hold passes: each is sold on its own
    assert not b.bot.risk.open and all(lm.exited for lm in lms)
    assert b.bot.engine.trades == {lm.meta.market.slug: 1 for lm in lms}


def test_one_entry_per_market_and_the_next_window_trades(tmp_path):
    b = _Bot(tmp_path)
    first = b.add_market("BTC", T0)
    b.want.add(first.meta.market.slug)
    b.tick()
    b.run(10_000)                                          # filled and sold
    assert first.exited and b.submitted == 2               # buy + sell
    b.want.add(first.meta.market.slug)
    b.run(1_000)
    assert b.submitted == 2                                # the exited market is not evaluated again
    nxt = b.add_market("BTC", T0 + 300_000.0)
    b.clock_t = T0 + 300_000.0 + 100_000.0
    b.want.add(nxt.meta.market.slug)
    b.tick()
    assert b.submitted == 3                                # the next BTC window is its own market


def test_10_11_a_killed_order_blocks_nothing_and_leaves_no_position(tmp_path):
    b = _Bot(tmp_path)
    a = b.add_market("BTC", T0)
    e = b.add_market("ETH", T0)
    b.want |= {a.meta.market.slug, e.meta.market.slug}
    b.tick()
    # BTC's ask runs away while its order is in flight: venue_lag matches 500 ms after
    # arrival against the book then, so the FOK is killed -- no assumed fill
    lb = a.books[a.meta.market.yes_token_id]
    lb.apply_snapshot({"bids": [{"price": "0.60", "size": "100"}], "asks": [{"price": "0.62", "size": "100"}],
                       "timestamp": str(int(b.clock_t))})
    b.run(2_000)
    assert a.meta.market.slug not in b.bot.risk.open and not b.bot.risk.pending
    assert b.bot.exchange.positions[a.meta.market.yes_token_id].shares == 0.0
    assert e.meta.market.slug in b.bot.risk.open           # ETH filled regardless
    assert b.bot.guard.entries_allowed and len(b.bot.guard.outcomes) == 1   # the expected kill is not a failure
    sol = b.add_market("SOL", T0)
    b.want.add(sol.meta.market.slug)
    b.tick()
    assert b.submitted == 3                                # and a third market still trades


def test_14_the_kill_switch_stops_buys_but_not_exits(tmp_path):
    b = _Bot(tmp_path)
    a = b.add_market("BTC", T0)
    b.want.add(a.meta.market.slug)
    b.tick()
    b.run(2_000)
    assert a.meta.market.slug in b.bot.risk.open
    (tmp_path / "KILL").write_text("halt")
    e = b.add_market("ETH", T0)
    b.want.add(e.meta.market.slug)
    b.tick()
    assert b.submitted == 1                                # no new BUY
    assert b.bot.exec_rejections.get("KILL_SWITCH") == 1
    b.run(8_000)
    assert a.meta.market.slug not in b.bot.risk.open and a.exited   # the open position still got out
    (tmp_path / "KILL").unlink()


def test_13_a_restart_restores_every_open_position(tmp_path):
    b = _Bot(tmp_path)
    lms = [b.add_market(a, T0) for a in ("BTC", "ETH")]
    b.want |= {lm.meta.market.slug for lm in lms}
    b.tick()
    b.run(2_000)
    assert len(b.bot.risk.open) == 2
    b.bot.risk.realised_today = -3.0                      # earlier losses today
    b.bot._persist_positions()                             # noqa: SLF001
    doc = json.loads((tmp_path / "open_positions.json").read_text())
    assert doc["mode"] == "live-paper" and len(doc["positions"]) == 2
    cash_before = b.bot.exchange.balance

    b2 = _Bot(tmp_path)                                    # a new process, same files
    r = b2.bot.risk
    assert sorted(r.open) == sorted(lm.meta.market.slug for lm in lms)
    assert r.realised_today == pytest.approx(-3.0)          # the daily loss budget survives
    assert r.exposure_total() == pytest.approx(2 * 5 * 0.52)
    assert b2.bot.exchange.balance == pytest.approx(50.0 - (50.0 - cash_before))
    eng = b2.bot.engine
    for lm in lms:
        m = lm.meta.market
        assert b2.bot.exchange.positions[m.yes_token_id].shares == 5.0
        assert eng.trades[m.slug] == 1 and m.yes_token_id in eng.entry_ms
    # the restored markets are entered: no second entry; the timed exit sells them
    b2.clock_t = b.clock_t                                 # the restart happens after the fills
    for lm in lms:
        b2.add_market(lm.meta.market.asset, T0)
    b2.want |= {lm.meta.market.slug for lm in lms}
    b2.run(6_000)
    assert not b2.bot.risk.open and b2.submitted == 2      # two SELLs, no BUY
    assert json.loads((tmp_path / "open_positions.json").read_text())["positions"] == []


def test_restore_refuses_another_modes_file_and_closed_markets(tmp_path):
    (tmp_path / "open_positions.json").write_text(json.dumps({
        "version": 1, "mode": "LIVE-ARMED", "day": 0, "realised_today": -9.0,
        "positions": [{"slug": "btc-updown-5m-1", "token_id": "X", "shares": 5.0, "cost_basis": 2.6,
                       "side": "UP", "asset": "BTC", "epoch": 1, "usdc": 2.6, "entry_ms": 1.0,
                       "close_ts": T0 + 300_000.0}]}))
    b = _Bot(tmp_path)                                     # a PAPER run never adopts real-money state
    assert not b.bot.risk.open and b.bot.risk.realised_today == 0.0
    (tmp_path / "open_positions.json").write_text(json.dumps({
        "version": 1, "mode": "live-paper", "day": 0, "realised_today": 0.0,
        "positions": [{"slug": "btc-updown-5m-1", "token_id": "X", "shares": 5.0, "cost_basis": 2.6,
                       "side": "UP", "asset": "BTC", "epoch": 1, "usdc": 2.6, "entry_ms": 1.0,
                       "close_ts": 1.0}]}))                   # its market closed while we were down
    b = _Bot(tmp_path)
    assert not b.bot.risk.open


def test_live_restore_drops_what_the_venue_does_not_hold(tmp_path):
    b = _Bot(tmp_path)
    b.bot._restored_meta["btc-x"] = {"token_id": "T1"}      # noqa: SLF001
    b.bot.risk.open["btc-x"] = b.bot.risk.open.get("btc-x") or __import__(
        "troll_poly_bot.risk.limits", fromlist=["OpenPosition"]).OpenPosition("btc-x", "BTC", 1, "UP", 2.6, 5.0)
    b.bot.exchange.positions["T1"].shares = 5.0
    dropped = b.bot._drop_unconfirmed_restores({"checked": True, "watched_n": 4,   # noqa: SLF001
                                                "missing_positions": [{"token_id": "T1"}]})
    assert dropped == {"T1"} and "btc-x" not in b.bot.risk.open
    assert b.bot.exchange.positions["T1"].shares == 0.0


def test_the_live_adapter_rechecks_open_exposure_before_posting():
    from troll_poly_bot.execution.paper import FeeModel
    from troll_poly_bot.execution.polymarket import LiveCaps, PolymarketExchange
    from troll_poly_bot.types import Position

    ex = PolymarketExchange.__new__(PolymarketExchange)
    ex.caps = LiveCaps(max_order_usdc=5.0, max_open_usdc=6.0, kill_file="/nonexistent/KILL")
    ex.positions = {"A": Position("A", shares=5.0, cost_basis=2.7)}
    ex.open_orders = {}
    ex.clock = lambda: 1000.0
    ex.max_entry_decision_age_ms = 1e9
    ex.pre_post_check = None
    import threading as _threading
    ex._lock, ex._reservations = _threading.RLock(), {}       # noqa: SLF001 (the order-lane bookkeeping)
    order = Order(token_id="B", side=Side.BUY, price=0.70, size=5.0, tif=TimeInForce.FOK)
    order.decision_ts = 1000.0
    why = ex._pre_post_refusal(order)                        # noqa: SLF001
    assert why is not None and why[0] is RejectReason.LIVE_CAPS and "re-checked" in why[1]
    small = Order(token_id="B", side=Side.BUY, price=0.60, size=5.0, tif=TimeInForce.FOK)
    small.decision_ts = 1000.0
    assert ex._pre_post_refusal(small) is None               # noqa: SLF001  (2.7 + 3.0 <= 6.0)
    sell = Order(token_id="A", side=Side.SELL, price=0.50, size=5.0, tif=TimeInForce.FOK)
    assert ex._pre_post_refusal(sell) is None                # noqa: SLF001  (exits never capped)
    assert FeeModel is not None


def test_structured_log_lines(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="live")
    b = _Bot(tmp_path, max_simultaneous_positions=1)
    a, e = b.add_market("BTC", T0), b.add_market("ETH", T0)
    b.want |= {a.meta.market.slug, e.meta.market.slug}
    b.tick()
    b.run(2_000)
    text = caplog.text
    assert "[SIGNAL] BTC" in text and "[ORDER] BTC" in text and "buy submitted" in text
    assert "[POSITION] BTC" in text and " open: " in text
    assert "[SKIP] ETH" in text and "positions open" in text     # the one-position budget, said out loud


def test_micro_live_hourly_ceiling_bounds_the_engine_budget(tmp_path, monkeypatch):
    from troll_poly_bot.graduation import MicroCaps
    cfg = BotConfig()
    cfg.engine.strategy = "spot_lag"
    cfg.deploy_mode = "micro-live"
    cfg.spot_lag = _cfg(max_buys_per_hour=60)
    monkeypatch.setattr(MicroCaps, "from_env", classmethod(lambda cls, env=None: cls(max_orders_per_hour=5)))
    bot = LiveBot(assets=ASSETS, balance=50.0, cfg=cfg, state_path=str(tmp_path / "live_state.json"),
                  trade_log=str(tmp_path / "live_trades.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))
    assert bot.engine.scfg.max_buys_per_hour == 5
