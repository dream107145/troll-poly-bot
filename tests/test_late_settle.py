"""Late resolution of abandoned windows (2026-10-01). Gamma's markets?slug= hides a market once it is
closed, so a result published after the close was never seen and the window was booked at its last mark
("unresolved" in the order history). The lookup now asks again with closed=true, and an abandoned
window keeps being asked for its outcome; the correction (real - mark) is booked and recorded."""
from __future__ import annotations

import json

import pytest

from troll_poly_bot import ledger
from troll_poly_bot.live import LiveBot

T = 1_790_877_900_000.0
SLUG = "btc-updown-5m-1790877900"


def _bot(tmp_path, trade_log=None):
    # --keep-history, as the paper apps run: a restart appends to the trade log instead of clearing it
    return LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), reset_history=False,
                   trade_log=str(trade_log or tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))


def _fill(side, price, size=5.0, ts=T + 10_000.0):
    fee = 0.07 * price * (1 - price) * size
    return {"ts": ts, "event": "fill", "action": "BUY", "slug": SLUG, "asset": "BTC", "side": side, "price": price,
            "size": size, "cost": price * size, "fee": fee}


FILLS = [_fill("UP", 0.56), _fill("UP", 0.53), _fill("DOWN", 0.636), _fill("DOWN", 0.70), _fill("UP", 0.8065), _fill("UP", 0.85)]
REAL_UP = sum((5.0 if f["side"] == "UP" else 0.0) - f["cost"] - f["fee"] for f in FILLS)     # the 18:05 window, UP won


async def test_the_outcome_lookup_asks_again_for_a_closed_market(tmp_path):
    bot = _bot(tmp_path)
    asked = []

    async def get_json(url):
        asked.append(url)
        if "closed=true" in url:
            return [{"outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]', "closed": True,
                     "umaResolutionStatus": "resolved"}], None
        return [], 12.0                                   # what Gamma answers for a closed market
    bot._get_json = get_json
    assert await bot._venue_outcome(SLUG) is True
    assert len(asked) == 2 and asked[1].endswith("&closed=true")


def test_the_real_window_pnl_comes_from_its_fills(tmp_path):
    bot = _bot(tmp_path)
    for f in FILLS:
        bot._ledger_append(dict(f))
    assert bot._window_pnl_from_ledger(SLUG, True) == pytest.approx(REAL_UP)
    assert REAL_UP == pytest.approx(-0.84, abs=0.01)
    assert bot._window_pnl_from_ledger("btc-updown-5m-1", True) is None


async def test_an_abandoned_window_is_corrected_when_the_venue_resolves_it(tmp_path):
    bot = _bot(tmp_path)
    for f in FILLS:
        bot._ledger_append(dict(f))
    bot.clock.now = lambda: T + 400_000.0
    bal0, rt0 = bot.exchange.balance, bot.risk.realised_total
    bot._late[SLUG] = {"asset": "BTC", "epoch": 1790877900, "marked": -0.99, "marked_ts": T + 360_000.0, "credit": True,
                       "stats": True, "next_ms": 0.0, "until": T + 7_500_000.0, "chart": None}

    async def outcome(slug):
        return True
    bot._venue_outcome = outcome
    await bot._late_settle_tick(T + 400_000.0)
    delta = REAL_UP - (-0.99)
    assert SLUG not in bot._late
    assert bot.exchange.balance == pytest.approx(bal0 + delta)          # the rest of the payout follows the mark
    assert bot.risk.realised_total == pytest.approx(rt0 + delta)
    row = bot.ledger[-1]
    assert row["event"] == "settle" and row["resolved_late"] is True and row["venue_up"] is True
    assert row["pnl"] == pytest.approx(delta, abs=1e-4) and row["window_pnl"] == pytest.approx(REAL_UP, abs=1e-4)
    assert row["marked_pnl"] == pytest.approx(-0.99)
    # every sum over the log stays right: mark + correction = the real result
    assert sum(r["pnl"] for r in [{"pnl": -0.99}, row]) == pytest.approx(REAL_UP, abs=1e-4)


async def test_no_outcome_keeps_asking_then_gives_up(tmp_path):
    bot = _bot(tmp_path)
    bot._late[SLUG] = {"asset": "BTC", "epoch": 1, "marked": 1.0, "marked_ts": T, "credit": True, "stats": True,
                       "next_ms": 0.0, "until": T + 1_000.0}

    async def outcome(slug):
        return None
    bot._venue_outcome = outcome
    await bot._late_settle_tick(T + 500.0)
    assert SLUG in bot._late and bot._late[SLUG]["next_ms"] == pytest.approx(T + 500.0 + bot.LATE_RETRY_MS)
    await bot._late_settle_tick(T + 2_000_000.0)
    assert SLUG not in bot._late                                          # past its deadline: the mark stands


def test_at_start_the_trade_logs_abandoned_windows_are_queued_once(tmp_path):
    path = tmp_path / "trades.jsonl"
    rows = FILLS + [{"ts": T + 361_000.0, "event": "settle", "slug": SLUG, "asset": "BTC", "epoch": 1790877900,
                     "unresolved": True, "reason": "no venue outcome", "marked": True, "pnl": -0.9898},
                    # an older abandon that was already corrected is not queued again
                    {"ts": T + 1.0, "event": "settle", "slug": "btc-updown-5m-1", "unresolved": True, "pnl": 1.0},
                    {"ts": T + 2.0, "event": "settle", "slug": "btc-updown-5m-1", "resolved_late": True, "pnl": 0.5}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    import time
    real_time = time.time
    time.time = lambda: (T + 400_000.0) / 1000.0
    try:
        bot = _bot(tmp_path, trade_log=path)
    finally:
        time.time = real_time
    assert list(bot._late) == [SLUG]
    it = bot._late[SLUG]
    assert it["marked"] == pytest.approx(-0.9898) and it["credit"] is False and it["stats"] is False


def test_the_history_round_trip_reads_mark_plus_correction():
    rows = [dict(FILLS[0]),
            {"ts": T + 361_000.0, "event": "settle", "slug": SLUG, "unresolved": True, "pnl": -0.99},
            {"ts": T + 600_000.0, "event": "settle", "slug": SLUG, "resolved_late": True, "pnl": 0.15, "window_pnl": -0.84}]
    (trip,) = ledger.round_trips(rows)
    assert trip["pnl"] == pytest.approx(-0.84)


# ── a window restored into after a restart (2026-10-01 19:00: reaped 48 s after its close, unsettled) ──

def _strikeless(bot, shares=5.0):
    from troll_poly_bot.feeds.markets import MarketMeta
    from troll_poly_bot.live import LiveMarket
    from troll_poly_bot.types import Market, Position
    market = Market(condition_id="c", asset="BTC", yes_token_id="Y", no_token_id="N", strike=0.0,
                    open_ts=T, close_ts=T + 300_000.0, tick_size=0.01, min_size=5.0, slug=SLUG)
    lm = LiveMarket(meta=MarketMeta(market=market, resolution_source="", accepting_orders=True, duration_s=300))
    bot.markets[SLUG] = lm
    if shares:
        bot.exchange.positions["Y"] = Position(token_id="Y", shares=shares, cost_basis=shares * 0.7)
        bot.risk.on_fill(SLUG, "BTC", 1790877900, "UP", shares * 0.7, shares)   # as a restored position is
    return lm


def test_a_window_without_a_strike_that_holds_a_position_is_not_reaped_early(tmp_path):
    bot = _bot(tmp_path)
    _strikeless(bot)
    bot._reap(T + 300_000.0 + 60_000.0)               # 60 s after the close: settlement has not run yet
    assert SLUG in bot.markets
    bot._reap(T + 300_000.0 + 250_000.0)              # past the full grace it goes, and is late-resolved
    assert SLUG not in bot.markets and SLUG in bot._late


def test_a_watched_window_without_a_strike_still_goes_after_30_s(tmp_path):
    bot = _bot(tmp_path)
    _strikeless(bot, shares=0.0)
    bot._reap(T + 300_000.0 + 31_000.0)
    assert SLUG not in bot.markets and SLUG not in bot._late


async def test_a_window_without_a_strike_that_holds_a_position_settles_on_the_venue_outcome(tmp_path):
    bot = _bot(tmp_path)
    lm = _strikeless(bot)

    async def outcome(slug):
        return True
    bot._venue_outcome = outcome
    bot.clock.now = lambda: T + 300_000.0 + bot.SETTLE_DELAY_MS + 1.0
    bot._stop.clear()

    async def one_pass(_):
        bot._stop.set()
    import asyncio
    real_sleep = asyncio.sleep
    asyncio.sleep = one_pass
    try:
        await bot.settle_loop()
    finally:
        asyncio.sleep = real_sleep
    assert lm.settled and lm.outcome == "UP"
    row = bot.ledger[-1]
    assert row["event"] == "settle" and row.get("venue_up") is True and not row.get("unresolved")


def test_the_other_side_of_a_both_sides_window_reads_settled_not_open():
    rows = [_fill("DOWN", 0.40, ts=T + 1_000.0), _fill("UP", 0.80, ts=T + 2_000.0),
            {"ts": T + 340_000.0, "event": "settle", "slug": SLUG, "venue_up": False, "pnl": -2.29}]
    down, up = ledger.round_trips(rows)
    assert down["status"] == "settled" and down["pnl"] is None and up["pnl"] == pytest.approx(-2.29)


def test_history_counts_a_corrected_window_once_by_its_real_result(tmp_path):
    from troll_poly_bot.web import server
    path = tmp_path / "trades.jsonl"
    rows = [_fill("UP", 0.56),
            {"ts": T + 361_000.0, "event": "settle", "slug": SLUG, "unresolved": True, "pnl": 0.10},
            {"ts": T + 600_000.0, "event": "settle", "slug": SLUG, "resolved_late": True, "pnl": -0.30,
             "window_pnl": -0.20}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    h = server._history(1, 50, view="trips", trade_log=path)
    assert h["settled"] == 1 and h["wins"] == 0 and h["losses"] == 1
    assert h["realised_pnl"] == pytest.approx(-0.20)
    ev = server._history(1, 50, view="events", trade_log=path)
    mark = [r for r in ev["rows"] if r.get("unresolved")][0]
    assert mark["corrected_late"] is True


async def test_a_late_resolution_settles_the_engine_too(tmp_path):
    # 2026-10-02: the pair-lock engine never heard about late-resolved windows (cycles stuck pending,
    # panel P&L +2.44 vs ledger +15.50); the late booking now calls on_market_settled with the real result
    bot = _bot(tmp_path)
    for f in FILLS:
        bot._ledger_append(dict(f))
    bot.clock.now = lambda: T + 400_000.0
    seen = []
    bot.engine.on_market_settled = lambda slug, up, pnl, now: seen.append((slug, up, round(pnl, 6)))
    bot._late[SLUG] = {"asset": "BTC", "epoch": 1790877900, "marked": -0.99, "marked_ts": T + 360_000.0, "credit": True,
                       "stats": True, "next_ms": 0.0, "until": T + 7_500_000.0, "chart": None}

    async def outcome(slug):
        return True
    bot._venue_outcome = outcome
    await bot._late_settle_tick(T + 400_000.0)
    assert seen == [(SLUG, True, round(REAL_UP, 6))]


def test_at_start_a_window_already_resolved_late_settles_the_engines_restored_cycles(tmp_path):
    path = tmp_path / "trades.jsonl"
    rows = FILLS + [{"ts": T + 361_000.0, "event": "settle", "slug": SLUG, "unresolved": True, "pnl": -0.99},
                    {"ts": T + 420_000.0, "event": "settle", "slug": SLUG, "resolved_late": True, "venue_up": True,
                     "window_pnl": round(REAL_UP, 4), "pnl": round(REAL_UP + 0.99, 4)}]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    bot = _bot(tmp_path, trade_log=path)
    seen = []
    bot.engine.holds_window = lambda slug: slug == SLUG
    bot.engine.on_market_settled = lambda slug, up, pnl, now: seen.append((slug, up, pnl))
    bot._queue_ledger_late()
    assert seen == [(SLUG, True, round(REAL_UP, 4))] and SLUG not in bot._late


def test_trading_prices_before_the_official_resolution_are_not_an_outcome():
    """2026-10-02 19:20 window: at 19:25:43 Gamma's row still showed DOWN at 0.9+ (the last trading prices); the
    official resolution at 19:26:27 was UP. The bot settled on the first read and booked -49.13 instead of +0.87."""
    from troll_poly_bot.live import outcome_of_row
    base = {"outcomes": '["Up", "Down"]'}
    assert outcome_of_row({**base, "outcomePrices": '["0.04", "0.96"]', "closed": False}) is None
    assert outcome_of_row({**base, "outcomePrices": '["0", "1"]', "closed": False}) is None          # not closed yet
    assert outcome_of_row({**base, "outcomePrices": '["0", "1"]', "closed": True,
                           "umaResolutionStatus": "proposed"}) is None                                 # not final
    assert outcome_of_row({**base, "outcomePrices": '["0.03", "0.97"]', "closed": True,
                           "umaResolutionStatus": "resolved"}) is None                                 # not 1 / 0
    assert outcome_of_row({**base, "outcomePrices": '["1", "0"]', "closed": True, "umaResolutionStatus": "resolved"}) is True
    assert outcome_of_row({**base, "outcomePrices": '["0", "1"]', "closed": True, "umaResolutionStatus": "resolved"}) is False
    assert outcome_of_row({**base, "outcomePrices": '["1", "0"]', "closed": True}) is True             # no status field


async def test_a_position_restored_from_before_this_runs_trade_log_settles_on_the_late_result(tmp_path):
    """2026-10-06 00:06: the live bot restarted on fresh data with a 29.46 DOWN winner of a closed market on its books.
    The late lookup resolved the market but found no fills in the (new) trade log and corrected nothing, so the
    position stayed on the books for good and the guard read it as missing. Now the restored tokens settle on the
    result and the window's P&L is what they paid less what they cost."""
    from troll_poly_bot.types import Position
    bot = _bot(tmp_path)
    bot.clock.now = lambda: T + 400_000.0
    bot.exchange.positions["D"] = Position(token_id="D", shares=29.46, cost_basis=20.0, fees_paid=0.92)
    bot.exchange.positions["U"] = Position(token_id="U", shares=0.0049, cost_basis=3.76, fees_paid=0.0)
    seen = []
    bot.engine.on_market_settled = lambda slug, up, pnl, now: seen.append((slug, up, round(pnl, 4)))
    bot._late[SLUG] = {"asset": "BTC", "epoch": 1790877900, "marked": 0.0, "marked_ts": T + 300_000.0, "credit": False,
                       "stats": True, "next_ms": 0.0, "until": T + 7_500_000.0, "tokens": {"D": "DOWN", "U": "UP"}}

    async def outcome(slug):
        return False                                                      # DOWN won
    bot._venue_outcome = outcome
    bal0 = bot.exchange.balance
    await bot._late_settle_tick(T + 400_000.0)
    real = (29.46 - 20.0 - 0.92) + (0.0 - 3.76 - 0.0)
    assert SLUG not in bot._late
    assert bot.exchange.positions["D"].shares == 0.0 and bot.exchange.positions["U"].shares == 0.0   # settled, not "missing"
    row = bot.ledger[-1]
    assert row["event"] == "settle" and row["resolved_late"] is True and row["venue_up"] is False
    assert row["window_pnl"] == pytest.approx(real, abs=1e-4) and row["pnl"] == pytest.approx(real, abs=1e-4)
    assert seen == [(SLUG, False, round(real, 4))]                       # the engine's restored cycle settles too
    assert bot.exchange.balance == pytest.approx(bal0 + 29.46)           # the paper exchange pays the winner into cash here
    # with nothing on the books and no fills, still nothing to correct
    bot._late[SLUG] = {**bot._late.get(SLUG, {}), "asset": "BTC", "epoch": 1, "marked": 0.0, "marked_ts": T, "credit": False,
                       "stats": True, "next_ms": 0.0, "until": T + 7_500_000.0, "tokens": {"D": "DOWN"}}
    n = len(bot.ledger)
    await bot._late_settle_tick(T + 400_000.0)
    assert len(bot.ledger) == n and SLUG not in bot._late
