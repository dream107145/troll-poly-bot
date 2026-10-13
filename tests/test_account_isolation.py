"""One account, one bot: foreign activity, reconciliation before trading,
micro-live ceilings the dashboard cannot lift, and data that never mixes."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from troll_poly_bot import earnings as earnings_log
from troll_poly_bot.config import BotConfig
from troll_poly_bot.control import ControlFile
from troll_poly_bot.execution.paper import FeeModel
from troll_poly_bot.execution.polymarket import LiveCaps, PolymarketExchange
from troll_poly_bot.graduation import MicroCaps, assess
from troll_poly_bot.live import LiveBot
from troll_poly_bot.risk.guard import Guard
from troll_poly_bot.signals.costs import FeeSchedule

from test_polymarket_exchange import FakeClient

REPO = Path(__file__).resolve().parents[1]


class AccountClient(FakeClient):
    def __init__(self, trades=(), positions=None, open_orders=(), **kw):
        super().__init__(**kw)
        self.trades = list(trades)
        self.positions = positions or []
        self.open_orders = list(open_orders)
        self.cancelled: list = []

    def list_account_trades(self, **kw):
        self.trades_query = kw
        return list(self.trades)

    def list_positions(self):
        return list(self.positions)

    def list_open_orders(self):
        return [{"id": oid} for oid in self.open_orders]

    def cancel_order(self, *, order_id):
        self.cancelled.append(order_id)


def _trade(tid, taker, role="TAKER", makers=(), when=None, size=6.33):
    return SimpleNamespace(id=tid, asset_id="TOK", side="BUY", size=size, price=0.8, trader_side=role,
                           taker_order_id=taker, maker_orders=[SimpleNamespace(order_id=m) for m in makers],
                           matched_at=when or datetime.now(timezone.utc))


def _venue(client, armed=True, tmp_path=None):
    ex = PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=20.0,
                            caps=LiveCaps(kill_file=str(tmp_path / "KILL") if tmp_path else "KILL-none"),
                            armed=armed, clock=lambda: 1000.0, wallet=FakeClient.wallet)
    ex.authenticate()
    return ex


# ───────────────────────────── foreign activity ─────────────────────────────


def test_a_trade_this_process_did_not_place_is_foreign():
    client = AccountClient()
    ex = _venue(client)
    ex.own_venue_ids.add("0xours")
    ex.started_s = datetime.now(timezone.utc).timestamp() - 60.0
    client.trades = [
        _trade("t1", "0xours"),                                               # our own taker fill
        _trade("t2", "0xsomeone"),                                            # not ours: the 12:59 case
        _trade("t3", "0xabc", role="MAKER", makers=("0xours", "0xother")),    # we were a maker in it
        _trade("t4", "0xold", when=datetime(2020, 1, 1, tzinfo=timezone.utc)),  # before this process
    ]
    new = ex.check_foreign_trades()
    assert [t["trade_id"] for t in new] == ["t2"]
    assert client.trades_query["after"] == str(int(ex.started_s))
    assert ex.check_foreign_trades() == []                                     # counted once
    assert ex.snapshot()["foreign_trades"][0]["trade_id"] == "t2"


def test_the_guard_latches_on_foreign_activity():
    g = Guard()
    g.update(0.0, foreign_trades=[{"side": "BUY", "size": 6.33, "price": 0.8}])
    assert "FOREIGN_TRADE" in g.tripped and not g.entries_allowed
    g.update(100.0, foreign_trades=[])
    assert "FOREIGN_TRADE" in g.tripped                                        # latched until a human looks


def test_balance_drift_and_unknown_positions_stop_entries():
    g = Guard()
    g.update(0.0, balance_drift_usdc=-5.07)
    assert "BALANCE_DRIFT" in g.tripped
    g2 = Guard()
    g2.update(0.0, unknown_positions=["TOK"])
    assert "UNKNOWN_POSITION" in g2.tripped
    g2.update(1000.0, unknown_positions=[])
    assert "UNKNOWN_POSITION" not in g2.tripped                                # clears when the venue does


def test_entries_wait_for_a_reconciliation_that_could_see_the_markets():
    g = Guard()
    g.update(0.0, reconciled=False)
    assert "RECONCILE_PENDING" in g.tripped
    g.update(100.0, reconciled=True)
    assert "RECONCILE_PENDING" not in g.tripped


def test_a_position_check_before_discovery_proves_nothing():
    client = AccountClient(positions=[{"asset_id": "TOK", "size": 6.33}])
    ex = _venue(client)
    ex.watched_tokens = lambda: set()
    rep = ex.reconcile_positions()
    assert rep["checked"] and rep["watched_n"] == 0 and rep["legacy_positions"]
    ex.watched_tokens = lambda: {"TOK"}
    rep = ex.reconcile_positions()
    assert rep["watched_n"] == 1 and rep["unknown_positions"][0]["token_id"] == "TOK"


# ─────────────────────────── restart and shadow ───────────────────────────


def test_an_armed_restart_cancels_stray_orders_and_a_dry_run_never_does():
    armed = AccountClient(open_orders=["0xleft-over"])
    _venue(armed, armed=True)
    assert armed.cancelled == ["0xleft-over"]
    shadow = AccountClient(open_orders=["0xanother-bots-order"])
    _venue(shadow, armed=False)
    assert shadow.cancelled == []                     # a shadow process must not touch a shared account


# ───────────────────────────── micro-live ceilings ─────────────────────────────


def test_the_dashboard_cannot_lift_the_micro_live_caps(tmp_path):
    cfg = BotConfig()
    cfg.deploy_mode = "micro-live"
    MicroCaps().apply(cfg)
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"))
    bot.exchange.caps = LiveCaps(max_order_usdc=5.0, max_open_usdc=10.0, max_daily_loss_usdc=10.0,
                                 max_orders_per_hour=5)
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"live": {}, "paper": {"risk.max_daily_loss_usdc": 40.0,
                                                      "risk.max_concurrent_positions": 10,
                                                      "caps.max_order_usdc": 500.0,
                                                      "engine.max_spread": 0.2}}))
    bot.controls = ControlFile(path)
    bot._apply_controls()
    m = MicroCaps()
    assert cfg.risk.max_daily_loss_usdc <= m.max_daily_loss_usdc
    assert cfg.risk.max_concurrent_positions <= m.max_concurrent_positions
    assert cfg.engine.max_spread <= m.max_spread
    assert bot.exchange.caps.max_order_usdc <= m.max_order_usdc
    assert any("micro-live ceiling" in e for e in bot.control_errors)


def test_enforce_only_ever_lowers():
    cfg = BotConfig()
    cfg.risk.max_daily_loss_usdc = 2.0
    cfg.engine.max_spread = 0.01
    MicroCaps().enforce(cfg)
    assert cfg.risk.max_daily_loss_usdc == 2.0 and cfg.engine.max_spread == 0.01


# ───────────────────────────── graduation evidence ─────────────────────────────


def _earn(tmp_path, rows):
    p = tmp_path / "earnings.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_graduation_counts_held_to_the_bell_and_drops_estimates(tmp_path):
    rows = [
        {"slug": "btc-updown-5m-1790000000", "pnl": -2.0, "exited": False, "mode": "LIVE-ARMED", "strategy": "momentum_taker"},
        {"slug": "btc-updown-5m-1790000300", "pnl": 1.0, "exited": True, "mode": "LIVE-ARMED", "strategy": "momentum_taker"},
        {"slug": "btc-updown-5m-1790000600", "pnl": 9.0, "exited": False, "mode": "LIVE-ARMED", "marked": True,
         "strategy": "momentum_taker"},
        {"slug": "eth-updown-5m-1790000900", "pnl": 5.0, "exited": True, "mode": "LIVE-ARMED", "strategy": "analytic"},
    ]
    v = assess(_earn(tmp_path, rows), strategy="momentum_taker")
    assert v.stats["trades"] == 2 and v.stats["total"] == -1.0          # the held loser counts; the mark does not
    assert assess(_earn(tmp_path, rows), strategy="analytic").stats["total"] == 5.0


def test_earnings_rows_say_which_strategy_and_mode_earned_them(tmp_path):
    cfg = BotConfig()
    cfg.engine.strategy = "momentum_taker"
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"))
    bot._ledger_append({"ts": 1.0, "event": "settle", "slug": "btc-updown-5m-1790000000", "pnl": 0.5})
    bot._ledger_append({"ts": 2.0, "event": "settle", "slug": "btc-updown-5m-1790000300", "pnl": 0.1, "marked": True})
    rows = [json.loads(l) for l in (tmp_path / "e.jsonl").read_text().splitlines()]
    assert rows[0]["strategy"] == "momentum_taker" and rows[0]["mode"] == "live-paper" and rows[0]["marked"] is False
    assert rows[1]["marked"] is True


# ─────────────────────────────── data isolation ───────────────────────────────


def test_the_test_session_does_not_run_in_the_repository():
    assert Path.cwd().resolve() != REPO
    assert not Path("data/earnings.jsonl").resolve().is_relative_to(REPO / "data")
    assert not Path(earnings_log.EARNINGS_LOG).resolve().is_relative_to(REPO / "data")


def test_a_bot_built_with_default_paths_writes_nowhere_real():
    bot = LiveBot(assets=("BTC",))                                             # every default path
    bot._ledger_append({"ts": 1.0, "event": "settle", "slug": "btc-updown-5m-1", "pnl": 1.0})
    assert Path(bot.earnings_path).resolve().is_relative_to(Path.cwd().resolve())
    assert not Path(bot.earnings_path).resolve().is_relative_to(REPO)
    assert not bot.kill_path.resolve().is_relative_to(REPO)


def test_paper_shadow_and_live_rows_are_labelled_apart(tmp_path):
    paper = LiveBot(assets=("BTC",), state_path=str(tmp_path / "a.json"), trade_log=str(tmp_path / "a.jsonl"),
                    earnings_path=str(tmp_path / "e.jsonl"))
    assert paper.mode_label == "live-paper"
    shadow = LiveBot(assets=("BTC",), state_path=str(tmp_path / "b.json"), trade_log=str(tmp_path / "b.jsonl"),
                     earnings_path=str(tmp_path / "e.jsonl"), dry_run_twin=_venue(AccountClient(), armed=False))
    assert shadow.mode_label == "shadow-paper"
    live = LiveBot(assets=("BTC",), state_path=str(tmp_path / "c.json"), trade_log=str(tmp_path / "c.jsonl"),
                   earnings_path=str(tmp_path / "e.jsonl"), exchange=_venue(AccountClient(), armed=True))
    assert live.mode_label == "LIVE-ARMED"
    assert len({paper.mode_label, shadow.mode_label, live.mode_label}) == 3


def test_shadow_reports_a_dirty_account_without_stopping_its_decisions(tmp_path):
    client = AccountClient(trades=[_trade("t9", "0xsomeone-else")])
    dry = _venue(client, armed=False)
    dry.started_s = datetime.now(timezone.utc).timestamp() - 60.0
    dry.check_foreign_trades()
    bot = LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"),
                  earnings_path=str(tmp_path / "e.jsonl"), dry_run_twin=dry)
    now = bot.clock.now()
    bot._update_guard(now)
    assert "FOREIGN_TRADE" in bot.account_check.tripped           # micro-live would be blocked ...
    assert "FOREIGN_TRADE" not in bot.guard.tripped               # ... shadow keeps measuring
    assert bot.snapshot()["account_check"]["tripped"]
