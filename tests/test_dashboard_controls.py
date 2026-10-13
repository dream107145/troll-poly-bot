"""Every bot variable from the dashboard (2026-09-29): the spot-lag strategy's own
settings, its budget and the real-money envelope, applied to a running bot."""
from __future__ import annotations

import json

import pytest

from troll_poly_bot.config import BotConfig
from troll_poly_bot.control import (
    BUDGET_TO_RISK, TUNABLES, ControlFile, _coherent, apply_to, current_values, spec, validate,
)
from troll_poly_bot.execution.polymarket import LiveCaps
from troll_poly_bot.graduation import MicroCaps
from troll_poly_bot.strategy.spot_lag import SpotLagConfig


def _scfg(**kw) -> SpotLagConfig:
    c = SpotLagConfig(enabled=True)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _bot(tmp_path, mode="paper", strategy="spot_lag", micro=None, **scfg):
    from troll_poly_bot.live import LiveBot
    cfg = BotConfig()
    cfg.engine.strategy = strategy
    cfg.deploy_mode = mode
    cfg.spot_lag = _scfg(**scfg)
    if micro is not None:
        micro.apply(cfg)
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"),
                  trade_log=str(tmp_path / "t.jsonl"), earnings_path=str(tmp_path / "e.jsonl"))
    if micro is not None:
        bot.micro_caps = micro
        bot.exchange.caps = LiveCaps(max_order_usdc=micro.max_order_usdc, max_open_usdc=micro.max_open_usdc,
                                     max_daily_loss_usdc=micro.max_daily_loss_usdc,
                                     max_orders_per_hour=micro.max_orders_per_hour)
    bot.controls = ControlFile(tmp_path / "controls.json")
    return bot


def _write(bot, **scopes):
    bot.controls.path.write_text(json.dumps(scopes))
    bot._apply_controls()                                                    # noqa: SLF001


# ─────────────────────────────── the catalogue ───────────────────────────────


def test_every_spot_lag_setting_a_running_bot_reads_is_on_the_page():
    exposed = {t.attr for t in TUNABLES.values() if t.target == "spot_lag"}
    # only the feature flag stays off the page: it decides whether the engine exists at all
    start_only = {"enabled"}
    missing = set(SpotLagConfig.__dataclass_fields__) - exposed - start_only    # type: ignore[attr-defined]
    assert not missing, f"not steerable from the dashboard: {sorted(missing)}"
    micro = {t.attr for t in TUNABLES.values() if t.target == "micro"}
    assert micro == {"max_order_usdc", "max_open_usdc", "max_daily_loss_usdc", "max_orders_per_hour",
                     "max_concurrent_positions", "max_spread", "max_slippage"}
    rows = {r["key"]: r for r in spec()}
    assert rows["spot_lag.order_type"]["options"] == ["FOK", "FAK"]
    assert rows["micro.max_order_usdc"]["ceiling"] and rows["caps.max_order_usdc"]["ceiling"]
    assert "spot_lag" in rows["engine.take_profit_enabled"]["not_for"]
    assert not rows["engine.max_spread"]["not_for"]                  # spot-lag is bounded by it


def test_choices_and_lists_are_normalised_and_nonsense_refused():
    clean, errors = validate({"spot_lag.order_type": "fak", "spot_lag.size_mode": "Touch",
                              "spot_lag.exclude_assets": " hype, doge ", "spot_lag.assets": "none"})
    assert clean == {"spot_lag.order_type": "FAK", "spot_lag.size_mode": "touch",
                     "spot_lag.exclude_assets": "HYPE,DOGE", "spot_lag.assets": ""} and not errors
    clean, errors = validate({"spot_lag.order_type": "GTC", "spot_lag.exclude_assets": "BTC; rm -rf",
                              "spot_lag.assets": 5})
    assert clean == {} and len(errors) == 3


def test_contradictory_spot_lag_settings_are_reported():
    warn = _coherent({"spot_lag.min_ask": 0.6, "spot_lag.max_ask": 0.5,
                      "spot_lag.max_time_remaining_s": 60, "spot_lag.min_time_remaining_s": 90,
                      "spot_lag.size_shares": 10, "spot_lag.max_order_usdc": 5.0})
    assert len(warn) == 2
    warn = _coherent({"spot_lag.size_shares": 10, "spot_lag.max_ask": 0.70, "micro.max_order_usdc": 6.0})
    assert any("7.00 USDC" in w for w in warn)


# ─────────────────────────────── applying ───────────────────────────────


def test_strategy_and_envelope_values_reach_the_objects_the_bot_uses():
    cfg = BotConfig()
    scfg = _scfg()
    micro = MicroCaps()
    changed = apply_to({"spot_lag.order_type": "FAK", "spot_lag.k_sigma": 2.5,
                        "spot_lag.hold_to_settlement": True, "spot_lag.exclude_assets": "",
                        "micro.max_order_usdc": 8.0}, cfg, None, strategy=scfg, micro=micro)
    assert scfg.order_type == "FAK" and scfg.k_sigma == 2.5 and scfg.hold_to_settlement is True
    assert scfg.exclude_assets == "" and scfg.asset_ok("HYPE")
    assert micro.max_order_usdc == 8.0                        # frozen, but this is its one writer
    assert len(changed) == 5 and "Never these assets: HYPE -> (none)" in changed
    now = current_values(cfg, None, strategy=scfg, micro=micro)
    assert now["spot_lag.order_type"] == "FAK" and now["micro.max_order_usdc"] == 8.0
    # a bot without a strategy config or an envelope neither reports nor applies them
    assert not any(k.startswith(("spot_lag.", "micro.")) for k in current_values(cfg, None))
    assert apply_to({"spot_lag.k_sigma": 4.0, "micro.max_order_usdc": 3.0}, cfg, None) == []


def test_a_running_spot_lag_bot_changes_without_a_restart(tmp_path):
    bot = _bot(tmp_path)
    assert not bot._shock_driven                                            # noqa: SLF001
    _write(bot, paper={"spot_lag.event_driven": True, "spot_lag.max_quote_age_ms": 900,
                       "spot_lag.exit_max_cross": 0.05, "spot_lag.max_entries_per_market": 2})
    assert bot._shock_driven                                                # noqa: SLF001
    # the momentum mirror the shared plumbing reads follows the strategy config
    assert bot.engine.mcfg.max_quote_age_ms == 900 and bot.engine.mcfg.exit_max_cross == 0.05
    assert bot._reentry                                                     # noqa: SLF001
    snap = bot.snapshot()["controls"]
    assert snap["values"]["spot_lag.event_driven"] is True
    assert snap["restart_only"]["strategy"] == "spot_lag"


def test_raising_the_budget_raises_the_risk_limit_it_maps_to(tmp_path):
    bot = _bot(tmp_path, max_total_exposure_usdc=10.0, max_simultaneous_positions=2)
    assert bot.risk.cfg.max_total_exposure_usdc <= 10.0
    _write(bot, paper={"spot_lag.max_total_exposure_usdc": 30.0, "spot_lag.max_simultaneous_positions": 3})
    assert bot.risk.cfg.max_total_exposure_usdc == 30.0 and bot.risk.cfg.max_concurrent_positions == 3
    # both set: the tighter wins
    _write(bot, paper={"spot_lag.max_total_exposure_usdc": 30.0, "risk.max_total_exposure_usdc": 20.0})
    assert bot.risk.cfg.max_total_exposure_usdc == 20.0
    assert set(BUDGET_TO_RISK) <= set(TUNABLES) and set(BUDGET_TO_RISK.values()) <= set(TUNABLES)


def test_a_removed_override_returns_to_the_start_value(tmp_path):
    bot = _bot(tmp_path, k_sigma=3.0, max_total_exposure_usdc=10.0)
    start_risk = bot.risk.cfg.max_total_exposure_usdc
    _write(bot, paper={"spot_lag.k_sigma": 2.0, "spot_lag.max_total_exposure_usdc": 40.0,
                       "engine.max_spot_age_ms": 3000.0})
    assert bot.engine.scfg.k_sigma == 2.0 and bot.risk.cfg.max_total_exposure_usdc == 40.0
    _write(bot, paper={})
    assert bot.engine.scfg.k_sigma == 3.0
    assert bot.risk.cfg.max_total_exposure_usdc == start_risk
    assert bot._base_spot_age == BotConfig().engine.max_spot_age_ms          # noqa: SLF001
    assert any("Shock size: 2 -> 3" in c for c in bot.control_applied)


def test_real_money_values_stay_under_the_envelope_unless_it_is_raised(tmp_path):
    micro = MicroCaps(max_order_usdc=6.0, max_open_usdc=24.0, max_daily_loss_usdc=20.0,
                      max_orders_per_hour=60, max_concurrent_positions=4, max_spread=0.05)
    bot = _bot(tmp_path, mode="micro-live", micro=micro, max_order_usdc=6.0, max_total_exposure_usdc=24.0,
               max_daily_loss_usdc=20.0, max_simultaneous_positions=4)
    bot.control_scope = "live"
    _write(bot, live={"spot_lag.max_daily_loss_usdc": 50.0, "spot_lag.max_buys_per_hour": 500,
                      "caps.max_daily_loss_usdc": 50.0})
    assert bot.risk.cfg.max_daily_loss_usdc == 20.0 and bot.exchange.caps.max_daily_loss_usdc == 20.0
    assert bot.engine.scfg.max_buys_per_hour == 60
    assert any("micro-live ceiling" in e for e in bot.control_errors)
    # the envelope itself raised: the same request now goes through
    _write(bot, live={"spot_lag.max_daily_loss_usdc": 50.0, "caps.max_daily_loss_usdc": 50.0,
                      "micro.max_daily_loss_usdc": 50.0})
    assert bot.micro_caps.max_daily_loss_usdc == 50.0
    assert bot.risk.cfg.max_daily_loss_usdc == 50.0 and bot.exchange.caps.max_daily_loss_usdc == 50.0
    # and taken away again: back under the start envelope
    _write(bot, live={})
    assert bot.micro_caps.max_daily_loss_usdc == 20.0
    assert bot.risk.cfg.max_daily_loss_usdc == 20.0 and bot.exchange.caps.max_daily_loss_usdc == 20.0


def test_a_paper_bot_ignores_the_envelope_and_the_repriced_budget_stays_a_ceiling(tmp_path):
    bot = _bot(tmp_path)
    _write(bot, paper={"micro.max_order_usdc": 50.0})
    assert bot.micro_caps is None and not bot.control_applied
    assert "micro.max_order_usdc" not in bot.snapshot()["controls"]["values"]


@pytest.mark.parametrize("key", sorted(k for k in TUNABLES if k.startswith("spot_lag.")))
def test_every_spot_lag_value_round_trips_through_a_running_bot(tmp_path, key):
    t = TUNABLES[key]
    bot = _bot(tmp_path)
    cur = getattr(bot.engine.scfg, t.attr)
    if t.kind == "bool":
        want = not cur
    elif t.kind == "choice":
        want = next(o for o in t.options if o != cur)
    elif t.kind == "list":
        want = "" if cur else "DOGE"
    else:
        want = t.lo if cur != t.lo else t.hi
    _write(bot, paper={key: want})
    assert bot.snapshot()["controls"]["values"][key] == want, bot.control_errors


def test_the_dashboard_payload_carries_the_new_kinds(tmp_path, monkeypatch):
    from troll_poly_bot.web import server
    monkeypatch.setattr(server, "CONTROLS", ControlFile(tmp_path / "controls.json"))
    payload = json.loads(json.dumps(server._controls_payload()))            # noqa: SLF001
    rows = {r["key"]: r for r in payload["spec"]}
    assert rows["spot_lag.order_type"]["kind"] == "choice" and rows["spot_lag.exclude_assets"]["kind"] == "list"
    assert [r["group"] for r in payload["spec"]][0] == "Spot-lag signal"
    res = server.CONTROLS.replace_scope("live", {"spot_lag.order_type": "fak", "micro.max_order_usdc": 7})
    assert res["ok"] and res["values"] == {"spot_lag.order_type": "FAK", "micro.max_order_usdc": 7.0}
