"""End to end in paper: signal -> order -> simulated arrival and quote
survival -> fill -> markouts -> momentum take-profit -> forensics timeline ->
the signal's recorded fate. One controllable clock drives all of it."""
from __future__ import annotations

import json
import math

from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.scenarios import scenario_named
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.live import LiveBook, LiveBot, LiveMarket
from troll_poly_bot.pricing.twap import TwapState
from troll_poly_bot.strategy.momentum import MomentumConfig
from troll_poly_bot.types import Market

SLUG = "btc-updown-5m-1790000000"


class _Vol:
    ready = True
    sigma_per_sec = 1e-4

    def update(self, *a):
        pass


def test_a_momentum_trade_end_to_end_in_paper(tmp_path):
    cfg = BotConfig()
    cfg.engine.strategy = "momentum_taker"
    cfg.execution_scenario = "realistic"
    cfg.momentum = MomentumConfig(min_net_edge=0.01, continuation_cap=0.08, continuation_per_sigma=0.02,
                                  include_exit_costs=False, uncertainty_base=0.0, uncertainty_frac=0.0,
                                  uncertainty_vol=0.0, take_profit=0.04)
    cfg.risk.micro_fixed_size = True
    cfg.risk.max_position_usdc = 50.0
    cfg.risk.min_order_usdc = 1.0
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"), kill_file=str(tmp_path / "KILL"))
    assert bot.exchange.scenario is scenario_named("realistic")
    clock = {"now": 1_790_000_100_000.0}
    bot.clock.now = lambda: clock["now"]
    now = clock["now"]
    market = Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN", strike=100.0,
                    open_ts=now - 100_000.0, close_ts=now + 200_000.0, tick_size=0.01, min_size=5.0, slug=SLUG)
    lm = LiveMarket(meta=MarketMeta(market=market, resolution_source="", accepting_orders=True, duration_s=300))
    for token in ("UP", "DOWN"):
        lm.books[token] = LiveBook(token)
    bot.markets[SLUG] = lm
    bot.token_index["UP"] = bot.token_index["DOWN"] = (SLUG, lm)

    def set_books(up_bid, up_ask, t):
        lm.books["UP"].bids, lm.books["UP"].asks = {up_bid: 200.0}, {up_ask: 100.0}
        lm.books["DOWN"].bids, lm.books["DOWN"].asks = {round(1 - up_ask, 2): 200.0}, {round(1 - up_bid, 2): 100.0}
        for b in lm.books.values():
            b.ts = b.recv_ts = t
        bot._advance_book_clock(t, t)

    # a strong accelerating up-move over 10 s, the UP token re-priced 0.50 -> 0.52
    for i in range(41):
        t = now - 10_000.0 + i * 250.0
        px = 100.0 * math.exp(20e-4 * ((i / 40) ** 2))
        bot.engine.features.update_spot("BTC", px, t)
    for t in range(int(now - 60_000), int(now - 10_000), 250):
        bot.engine.features.update_spot("BTC", 100.0, t - 60_000)
    bot.twap["BTC"] = TwapState()
    bot.engine.features.update_market(SLUG, 0.50, now - 4000.0)
    bot.spot["BTC"], bot.spot_ts["BTC"] = 100.2, now - 100.0
    bot.vol["BTC"] = _Vol()
    bot.composite = type("C", (), {"view": staticmethod(lambda a, t: CompositeView("BTC", 100.2, 100.2, 3, 1.0, 1.0,
                                                                                   50.0, {"binance": 100.2})),
                                   "counts": {}})()
    set_books(0.51, 0.53, now)

    bot._tick()
    assert lm.submitted == 1, bot.engine.last[SLUG].as_dict()
    order_id = next(iter(bot.fair_by_order))
    assert bot.fair_by_order[order_id]["signal_id"]

    # the quote survives; time passes in 100 ms steps until the fill is learned
    for _ in range(40):
        clock["now"] += 100.0
        bot.spot_ts["BTC"] = clock["now"] - 100.0
        set_books(0.51, 0.53, clock["now"])
        bot._tick()
    pos = bot.exchange.positions["UP"]
    assert pos.shares > 0 and lm.inflight == 0

    # the bid rises 4c: the momentum take-profit sells into it
    for _ in range(40):
        clock["now"] += 100.0
        bot.spot_ts["BTC"] = clock["now"] - 100.0
        set_books(0.58, 0.59, clock["now"])
        bot._tick()
    assert lm.exited and pos.shares == 0.0

    # the record carries the timeline and the fate; the markouts ran
    lines = [json.loads(l) for l in (tmp_path / "trade_forensics.jsonl").read_text().splitlines()]
    entry = next(r for r in lines if r.get("event") == "trade" and r.get("kind") == "entry")
    assert entry["t4_decision"] and entry["t5_sent"] and entry["t7_venue_fill"]
    assert entry["decision_to_fill_ms"] > 50.0 and entry["reason_for_exit"] == "TAKE_PROFIT"
    assert entry["pnl_after_fees"] > 0 and entry["signal_id"] and entry["max_entry_price"] >= entry["limit_price"]
    assert entry["preflight"]["KILL_SWITCH"] and entry["t8_exit_decision"]
    for _ in range(320):                                   # let 30 s of markouts come due
        clock["now"] += 100.0
        set_books(0.58, 0.59, clock["now"])
        bot._mark_due(clock["now"])
    assert bot.markouts.completed and bot.markouts.completed[0]["filled"] is True
    for _ in range(300):                                   # and the signal's 45 s horizon
        clock["now"] += 100.0
        bot._mark_due(clock["now"])
    fate = next(r for r in bot.outcomes.completed if r["decision"] == "BUY")
    assert fate["filled"] is True and fate["classification"] == "TRADED"
