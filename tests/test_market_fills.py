"""The market page's fill ledger API (web/server.py market_fills, 2026-10-12): every fill of one market or of
the newest markets (whole markets only), taker/maker read from the fee, sells, settled outcomes, unknown bots."""
from __future__ import annotations

import json

import pytest

from troll_poly_bot.web import server

A, B, C = "btc-updown-5m-1791900000", "btc-updown-5m-1791900300", "btc-updown-5m-1791900600"


def _fill(slug, ts, side, size, price, fee, action="BUY", **kw):
    return {"event": "fill", "slug": slug, "ts": ts, "action": action, "side": side, "size": size, "price": price,
            "fee": fee, **kw}


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "live_trades.jsonl"
    monkeypatch.setattr(server, "LIVE_STATE", tmp_path / "live_state.json")
    monkeypatch.setattr(server, "TRADE_LOG", path)
    monkeypatch.setattr(server, "FILLS", server._FillsIndex())
    rows = [
        _fill(A, 1.0, "DOWN", 30.0, 0.46, 0.52),
        _fill(A, 2.0, "UP", 10.0, 0.40, 0.0, pair=True),                # a resting bid: no fee -> maker
        _fill(A, 3.0, "UP", 10.0, 0.60, 0.17, action="SELL", kind="PROFIT_EXIT"),
        {"event": "settle", "slug": A, "ts": 4.0, "venue_up": False, "pnl": 1.0},
        _fill(B, 5.0, "UP", 5.0, 0.50, None, pair=True),                # no fee on record: the pair flag decides
        {"event": "fill", "slug": B, "ts": 5.5, "action": "BUY", "side": "UP", "size": "x", "price": 0.5},  # torn
        _fill(C, 6.0, "UP", 5.0, 0.55, 0.09),
        {"event": "settle", "slug": C, "ts": 7.0, "unresolved": True, "pnl": 0.0},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_one_market_whole_with_taker_maker_sell_and_outcome(log):
    d = server.market_fills(server.MAIN_BOT, slug=A)
    assert [f["ts"] for f in d["fills"]] == [1.0, 2.0, 3.0]
    assert [(f["action"], f["side"], f["liq"]) for f in d["fills"]] == [
        ("BUY", "DOWN", "taker"), ("BUY", "UP", "maker"), ("SELL", "UP", "taker")]
    assert d["fills"][2]["kind"] == "PROFIT_EXIT" and d["outcomes"] == {A: "DOWN"}


def test_newest_markets_are_whole_and_unresolved_settles_have_no_outcome(log):
    d = server.market_fills(server.MAIN_BOT, markets=2)
    assert {f["slug"] for f in d["fills"]} == {B, C} and d["outcomes"] == {}
    assert [f["liq"] for f in d["fills"] if f["slug"] == B] == ["maker"]     # the torn row is skipped
    assert len(server.market_fills(server.MAIN_BOT, markets=10)["fills"]) == 5


def test_unknown_bot_and_a_missing_log(log, tmp_path):
    assert server.market_fills("../etc") is None
    log.unlink()
    assert server.market_fills(server.MAIN_BOT, slug=A) == {"bot": server.MAIN_BOT, "fills": [], "outcomes": {}}
