"""Deployment modes and the graduation check."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from troll_poly_bot import graduation
from troll_poly_bot.__main__ import resolve_mode
from troll_poly_bot.graduation import GraduationCriteria, MicroCaps, assess, epoch_stats


def _args(**kw):
    base = dict(mode=None, live=False, armed=False, max_order_usdc=5.0, max_open_usdc=25.0,
                max_daily_loss=10.0, max_orders_per_hour=60)
    base.update(kw)
    return SimpleNamespace(**base)


def test_flags_map_onto_modes():
    assert resolve_mode(_args()) == "paper"
    assert resolve_mode(_args(live=True)) == "shadow"
    a = _args(live=True, armed=True)
    assert resolve_mode(a) == "micro-live"


def test_micro_live_clamps_the_caps_whatever_was_asked():
    a = _args(mode="micro-live", max_order_usdc=50.0, max_open_usdc=200.0, max_daily_loss=40.0,
              max_orders_per_hour=600)
    resolve_mode(a)
    m = MicroCaps()
    assert (a.max_order_usdc, a.max_open_usdc, a.max_daily_loss, a.max_orders_per_hour) == \
        (m.max_order_usdc, m.max_open_usdc, m.max_daily_loss_usdc, m.max_orders_per_hour)
    assert a.live and a.armed
    small = _args(mode="micro-live", max_order_usdc=0.5)
    resolve_mode(small)
    assert small.max_order_usdc == 0.5                  # a smaller cap is kept


def test_incoherent_combinations_are_refused():
    with pytest.raises(SystemExit):
        resolve_mode(_args(mode="paper", live=True))
    with pytest.raises(SystemExit):
        resolve_mode(_args(mode="shadow", armed=True))


def _earnings(tmp_path, rows):
    p = tmp_path / "earnings.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_live_is_refused_without_graduation(tmp_path, monkeypatch):
    p = _earnings(tmp_path, [{"slug": "btc-updown-5m-100", "pnl": 1.0, "exited": True, "mode": "LIVE-ARMED"}])
    monkeypatch.setattr(graduation, "assess", lambda **kw: assess(p))
    monkeypatch.setattr("troll_poly_bot.__main__.os.environ", {})
    with pytest.raises(SystemExit) as exc:
        resolve_mode(_args(mode="live"), env={})
    assert "graduation" in str(exc.value)
    assert resolve_mode(_args(mode="live"), env={"TPB_GRADUATION_OVERRIDE": graduation.OVERRIDE_PHRASE}) == "live"


def test_assess_needs_epochs_t_and_positive_total(tmp_path):
    rows = []
    for e in range(400):
        rows.append({"slug": f"btc-updown-5m-{e * 300}", "pnl": 1.0 if e % 4 else -0.5, "exited": True,
                     "mode": "LIVE-ARMED"})
        rows.append({"slug": f"eth-updown-5m-{e * 300}", "pnl": 0.5, "exited": True, "mode": "LIVE-ARMED"})
    p = _earnings(tmp_path, rows)
    v = assess(p, GraduationCriteria(min_epochs=300, min_t=2.0))
    assert v.ready, v.reasons
    assert v.stats["epochs"] == 400 and v.stats["trades"] == 800
    v2 = assess(p, GraduationCriteria(min_epochs=1000))
    assert not v2.ready and "need 1000" in v2.reasons[0]


def test_paper_rows_never_count(tmp_path):
    rows = [{"slug": f"btc-updown-5m-{e}", "pnl": 5.0, "exited": True, "mode": "live-paper"} for e in range(500)]
    p = _earnings(tmp_path, rows)
    v = assess(p)
    assert not v.ready and v.stats["epochs"] == 0


def test_epoch_stats_cluster_every_asset_in_a_window():
    rows = [{"slug": "btc-updown-5m-1", "pnl": 2.0}, {"slug": "eth-updown-5m-1", "pnl": -1.0},
            {"slug": "btc-updown-5m-2", "pnl": 1.0}]
    st = epoch_stats(rows)
    assert st["epochs"] == 2 and st["total"] == 2.0 and st["positive_epochs"] == 2


def test_missing_file_is_not_ready(tmp_path):
    v = assess(tmp_path / "nothing.jsonl")
    assert not v.ready and v.stats["epochs"] == 0
