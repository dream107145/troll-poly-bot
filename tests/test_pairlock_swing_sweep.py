"""The swing-recovery parameter sweep (scripts/pairlock/replay_swing_sweep.py): it must run the bot's own decisions
through the existing realistic fill model (scripts/pairlock/replay_swing.execute), reproduce the existing replay at the
current parameters, never look ahead, split time chronologically, and apply its policies (hedge ratio, hedge / recovery
price limits, capital cap) and stress tests exactly as documented."""
from __future__ import annotations

import glob
import sys
from dataclasses import replace

import numpy as np
import pytest

sys.path.insert(0, "/root/troll-poly-bot/scripts/pairlock")
import replay_swing as RS                                       # noqa: E402
import replay_swing_sweep as SW                                 # noqa: E402

from troll_poly_bot.strategy import pairlock_swing as sw       # noqa: E402
from troll_poly_bot.strategy.pairlock_book import PairBook, fee_per_share   # noqa: E402

OPEN = 1_790_000_000_000.0
#: (seconds after the open, UP bid, UP ask): A = UP leads at the entry, then DOWN / UP / DOWN / UP reach 0.70
DEFAULT_PATH = [(0, 0.59, 0.60), (55, 0.30, 0.31), (115, 0.69, 0.70), (175, 0.30, 0.31), (235, 0.69, 0.70)]


def synth(path=DEFAULT_PATH, outcome="UP", depth=3000.0, slug="btc-updown-5m-synthetic", open_ts=OPEN) -> dict:
    """A recorded-market lookalike: a sample every 200 ms, quotes held between the path's points, DOWN = 1 - UP."""
    t = np.arange(open_ts - 2000.0, open_ts + 300_000.0, 200.0)
    ub, ua = np.empty_like(t), np.empty_like(t)
    for k, x in enumerate(t):
        seg = [p for p in path if p[0] * 1000.0 <= x - open_ts] or [path[0]]
        ub[k], ua[k] = seg[-1][1], seg[-1][2]
    db, da = np.round(1.0 - ua, 2), np.round(1.0 - ub, 2)
    d = np.full_like(t, depth)
    return {"slug": slug, "open": open_ts, "close": open_ts + 300_000.0, "outcome": outcome, "t": t, "ub": ub, "ua": ua,
            "db": db, "da": da, "ubd": d, "uad": d.copy(), "dbd": d.copy(), "dad": d.copy()}


def real_windows(k=14):
    fs = sorted(glob.glob("/root/troll-poly-bot/data/charts/btc-updown-5m-*.json"))
    if len(fs) < 100:
        pytest.skip("no recorded BTC markets on this machine")
    out = []
    for fn in fs[5::max(1, len(fs) // (3 * k))]:
        w = RS.window_from_file(fn)
        if w is not None:
            out.append(w)
        if len(out) >= k:
            break
    return out


def _orders(monkeypatch, w, c, stress=SW.NO_STRESS):
    """Every order the sweep sends for ``w``: (decision time, kind, side, shares, limit)."""
    log = []
    orig = RS.execute

    def spy(mode, w_, i, dec, prm, fees, ask_shift=0.0):
        log.append((float(w_["t"][i]), dec.kind, dec.side, round(dec.shares, 4), dec.limit))
        return orig(mode, w_, i, dec, prm, fees, ask_shift)

    monkeypatch.setattr(RS, "execute", spy)
    r = SW.run_window(dict(w), c, stress)
    monkeypatch.setattr(RS, "execute", orig)
    return r, log


# ───────────────────────────── parameter injection, determinism, the baseline ─────────────────────────────

def test_parameters_reach_the_bots_params():
    c = SW.Cand(entry_start=25, entry_end=35, entry_min=0.52, entry_max=0.62, hysteresis=0.06, trigger=0.66, s1=15, s2=40,
                hedge_ratio=0.5, capital=150)
    p = c.prm()
    assert (p.swing_entry_delay_s, p.swing_entry_window_s, p.swing_entry_min, p.swing_entry_max, p.swing_leader_hysteresis,
            p.swing_recovery_price, p.swing1_shares, p.swing2_shares, p.swing_max_market_capital, p.swing_two_sided_hedge) == \
        (25, 35, 0.52, 0.62, 0.06, 0.66, 15, 40, 150, True)
    assert SW.Cand(s1=18, s2_mult=2.5).prm().swing2_shares == 45.0
    assert SW.Cand(hedge_ratio=0.0).prm().swing_two_sided_hedge is False
    # the sweep's baseline IS the bot's current spec defaults (RecoveryParams' own swing defaults)
    base, bot = SW.BASE.prm(), RS.BASE
    for f in ("swing_initial_shares", "swing_entry_min", "swing_entry_max", "swing_entry_delay_s", "swing_entry_window_s",
              "swing_recovery_price", "swing1_shares", "swing2_shares", "swing_max_recovery", "swing_max_market_capital",
              "swing_leader_hysteresis"):
        assert getattr(base, f) == getattr(bot, f), f


def test_replay_is_deterministic():
    w = synth()
    a, b = SW.run_window(dict(w), SW.BASE), SW.run_window(dict(w), SW.BASE)
    assert a == b and a["traded"] == 1 and a["swings"] == 4 and a["bucket"] == "3+"


def test_the_baseline_reproduces_the_existing_replay_market_by_market():
    """The fast path (jumping between swing conditions) makes the same decisions as replay_swing.run_window, which
    evaluates every sample: identical P&L, capital and swing count on recorded markets and the synthetic path."""
    ws = real_windows() + [synth(), synth(outcome="DOWN"), synth(depth=60.0)]
    traded = 0
    for w in ws:
        old, new = RS.run_window(dict(w), "realistic", RS.BASE), SW.run_window(dict(w), SW.BASE)
        assert old["traded"] == new["traded"], w["slug"]
        if old["traded"]:
            traded += 1
            assert (old["pnl"], old["capital"], old["swings"]) == pytest.approx((new["pnl"], new["capital"], new["swings"])), w["slug"]
            assert old["bucket"] == new["bucket"]
    assert traded >= 4
    # and the default synthetic path is the spec's: 10 / 24 / 80 then the hedge, held to the outcome
    r = SW.run_window(synth(), replace(SW.BASE, capital=150.0))
    assert (r["entry_qty"], r["s1_qty"], r["s2_qty"]) == (10.0, 24.0, 80.0) and r["hedge_qty"] == pytest.approx(66.0)
    assert r["inventory"] == 180.0 and r["balanced"] == 1


# ───────────────────────────── no look-ahead, chronological split ─────────────────────────────

def test_the_outcome_never_changes_a_decision(monkeypatch):
    up, log_up = _orders(monkeypatch, synth(outcome="UP"), SW.BASE)
    dn, log_dn = _orders(monkeypatch, synth(outcome="DOWN"), SW.BASE)
    assert log_up == log_dn and up["capital"] == dn["capital"] and up["inventory"] == dn["inventory"]
    assert up["pnl"] != dn["pnl"]


def test_the_future_never_changes_an_earlier_decision(monkeypatch):
    w = synth()
    later = dict(w)
    cut = OPEN + 150_000.0                                           # rewrite the book after 150 s: a different future
    m = w["t"] >= cut
    later["ub"], later["ua"] = w["ub"].copy(), w["ua"].copy()
    later["db"], later["da"] = w["db"].copy(), w["da"].copy()
    later["ub"][m], later["ua"][m], later["db"][m], later["da"][m] = 0.95, 0.96, 0.04, 0.05
    _, a = _orders(monkeypatch, w, SW.BASE)
    _, b = _orders(monkeypatch, later, SW.BASE)
    early = lambda log: [x for x in log if x[0] < cut - 2000.0]     # noqa: E731  (fills known 1.3 s after the decision)
    assert early(a) == early(b) and len(early(a)) >= 3 and a != b


def test_the_split_is_chronological_never_shuffled():
    day = 86_400_000.0
    t0 = 1_790_640_000_000.0                                         # 2026-09-29 00:00 UTC
    ws = [{"slug": f"m{k}", "open": t0 + k * day / 3} for k in range(30)]   # 09-29 .. 10-08: past the brief's dates
    sp = SW.split_of(ws)
    order = [sp[w["slug"]] for w in sorted(ws, key=lambda w: w["open"])]
    assert order == sorted(order, key=["train", "validation", "test"].index)          # train, then validation, then test
    assert [order.count(p) for p in ("train", "validation", "test")] == [18, 6, 6]   # the 60 / 20 / 20 fallback, in time order
    ws2 = [{"slug": f"d{k}", "open": t0 - 7 * day + k * day} for k in range(16)]      # 09-22 .. 10-07: the brief's dates
    sp2 = SW.split_of(ws2)
    assert sp2["d0"] == "train" and sp2["d9"] == "train" and sp2["d10"] == "validation" and sp2["d13"] == "test"


# ───────────────────────────── the realistic fill model ─────────────────────────────

def test_fills_are_the_arrival_books_walked_vwap():
    """143 ms after the decision the book has moved up a cent: the order walks THAT book (20 % of the touch left to
    us), and 1 s later the book must still fill it at or under the limit; the fill is the worse of the two VWAPs."""
    w = synth(depth=300.0)                                           # 100 shares per level, 20 at the touch for us
    i = int(np.searchsorted(w["t"], OPEN + 55_000.0))
    w["da"][i + 1:] = 0.71                                           # the DOWN ask ticks up right after the decision
    dec = sw.SwingOrder("recovery", "DOWN", 24.0, 0.73, 0.70, fee_per_share(0.70), 24.0, 1, "", {})
    r = RS.execute("realistic", w, i, dec, SW.BASE.prm(), fee_per_share)
    assert r["status"] == "filled" and r["qty"] == 24.0
    assert r["vwap"] == pytest.approx((20 * 0.71 + 4 * 0.72) / 24)  # not 0.70: the arrival book, its touch mostly taken
    assert r["fee"] == pytest.approx(20 * fee_per_share(0.71) + 4 * fee_per_share(0.72))


def test_partial_fills_take_only_what_the_book_holds():
    w = synth(depth=30.0)                                            # 10 per level within 2 c: 2 at the touch for us
    i = int(np.searchsorted(w["t"], OPEN + 55_000.0))
    dec = sw.SwingOrder("recovery", "DOWN", 24.0, 0.73, 0.70, 0.0, 24.0, 1, "", {})
    r = RS.execute("realistic", w, i, dec, SW.BASE.prm(), fee_per_share)
    assert r["status"] == "filled" and r["qty"] == pytest.approx(22.0) and r["qty"] < dec.shares
    out = SW.run_window(synth(depth=30.0), SW.BASE)
    assert out["traded"] and out["s1_partial"] >= 1 and out["s1_qty"] < 24.0 + 1e-9


# ───────────────────────────── policies ─────────────────────────────

def test_no_market_ever_spends_above_the_capital_cap():
    for cap in (100.0, 125.0, 150.0):
        c = replace(SW.BASE, capital=cap)
        for w in real_windows(8) + [synth(), synth(path=DEFAULT_PATH[:4]), synth(depth=200.0)]:
            r = SW.run_window(dict(w), c)
            if r["traded"]:
                assert r["capital"] <= cap + 1e-6, (w["slug"], cap, r["capital"])


@pytest.mark.parametrize("ratio,expect", [(0.0, 0.0), (0.25, 16.5), (0.5, 33.0), (0.75, 49.5), (1.0, 66.0)])
def test_the_hedge_buys_its_ratio_of_the_imbalance(ratio, expect):
    r = SW.run_window(synth(), replace(SW.BASE, capital=200.0, hedge_ratio=ratio))
    assert r["hedge_qty"] == pytest.approx(expect, abs=0.011) and r["s2_qty"] == 80.0
    assert r["inventory"] == pytest.approx(90.0 + 24.0 + expect, abs=0.011)


def test_the_hedge_ratio_each_swing_hedges_again():
    once = SW.run_window(synth(), replace(SW.BASE, capital=200.0, hedge_ratio=0.5, hedge_mode="once"))
    each = SW.run_window(synth(), replace(SW.BASE, capital=200.0, hedge_ratio=0.5, hedge_mode="each"))
    assert once["hedge_qty"] == pytest.approx(33.0) and each["hedge_qty"] == pytest.approx(33.0 + 16.5)   # swing 4: half of 33


def test_the_hedge_price_limit_waits_instead_of_paying_up():
    r = SW.run_window(synth(path=DEFAULT_PATH[:4]), replace(SW.BASE, capital=200.0, hedge_max=0.68))   # DOWN 0.70 at swing 3
    assert r["hedge_qty"] == 0.0 and r["policy_hedge_max"] == 1
    r = SW.run_window(synth(), replace(SW.BASE, capital=200.0, hedge_max=0.68))       # swing 4 makes DOWN cheap: hedged then
    assert r["hedge_qty"] == pytest.approx(66.0) and r["hedge_gross"] / r["hedge_qty"] <= 0.68
    late = DEFAULT_PATH[:4] + [(200, 0.33, 0.34)]                                       # DOWN eases to 0.67 later
    r = SW.run_window(synth(path=late), replace(SW.BASE, capital=200.0, hedge_max=0.68))
    assert r["hedge_qty"] == pytest.approx(66.0)


def test_recovery_price_limits_use_the_executable_vwap():
    levels = [(0.68, 10.0), (0.70, 10.0), (0.72, 100.0)]
    q = SW._vwap_capped_qty(levels, 24.0, 0.69)
    vw, _ = sw.walk(levels, q)
    assert q == pytest.approx(20.0) and vw == pytest.approx(0.69)                       # 10 @ .68 + 10 @ .70 = .69
    assert SW._vwap_capped_qty(levels, 24.0, 0.70) == pytest.approx(24.0)               # (6.8 + 7 + 4 x .72) / 24 = .695
    # in a run: the swing ask is 0.70 > 0.69, so swing 1 waits ("reduce" finds nothing under the limit) ...
    r = SW.run_window(synth(), replace(SW.BASE, rec_max=0.69))
    assert r["s1_qty"] == 0.0 and r["s2_qty"] == 0.0 and r["policy_rec_cap"] == 1
    # ... and buys once the price comes back under the limit while the swing stands
    dip = DEFAULT_PATH[:2] + [(70, 0.33, 0.34)] + DEFAULT_PATH[2:]
    r = SW.run_window(synth(path=dip), replace(SW.BASE, rec_max=0.69))
    assert r["s1_qty"] == pytest.approx(24.0) and r["s1_gross"] / r["s1_qty"] <= 0.69 + 1e-9
    r = SW.run_window(synth(path=dip), replace(SW.BASE, rec_max=0.69, rec_policy="reject"))
    assert r["s1_qty"] == pytest.approx(24.0)


def test_the_break_even_price_comes_from_the_inventory():
    b = PairBook("m")
    from troll_poly_bot.strategy.pairlock_book import FillRecord
    b.add_fill(FillRecord("m", 0.0, "up", "UP", 10.0, 0.60, 0.0, "o", "o-1", "BUY", 0.6, "entry"))
    assert SW.breakeven_price(b, "DOWN", 24.0, lambda p: 0.0) == pytest.approx((24 - 6) / 24, abs=1e-6)   # 0.75
    p = SW.breakeven_price(b, "DOWN", 24.0, fee_per_share)
    assert p + fee_per_share(p) == pytest.approx(0.75, abs=1e-6) and p < 0.75
    b.add_fill(FillRecord("m", 0.0, "down", "DOWN", 24.0, 0.70, 0.0, "o2", "o2-1", "BUY", 0.7, "recovery"))
    assert SW.breakeven_price(b, "UP", 80.0, lambda p: 0.0) == pytest.approx((10 + 80 - 22.8) / 80, abs=1e-6)   # 0.84


# ───────────────────────────── stress tests ─────────────────────────────

def test_stress_calculations():
    w = synth()
    base = SW.run_window(dict(w), replace(SW.BASE, capital=200.0))
    qty = base["entry_qty"] + base["s1_qty"] + base["s2_qty"] + base["hedge_qty"]
    up = SW.run_window(dict(w), replace(SW.BASE, capital=200.0), SW.Stress(price_add=0.02))
    assert up["inventory"] == base["inventory"]                                            # the same fills ...
    assert up["pnl"] == pytest.approx(base["pnl"] - 0.02 * qty)                            # ... each 2 cents dearer
    fee = SW.run_window(dict(w), replace(SW.BASE, capital=200.0), SW.Stress(fee_mult=1.5))
    assert fee["fees"] == pytest.approx(1.5 * base["fees"]) and fee["pnl"] < base["pnl"]
    none = SW.run_window(dict(w), SW.BASE, SW.Stress(kill=1.0))
    assert none["traded"] == 0 and none["entry_kills"] >= 1                                # every fill killed
    some = [SW.run_window(synth(slug=f"s{k}"), SW.BASE, SW.Stress(kill=0.3)) for k in range(30)]
    kills = sum(r.get("entry_kills", 0) + r.get("s1_kills", 0) + r.get("s2_kills", 0) + r.get("hedge_kills", 0) for r in some)
    sent = sum(r.get("entry_sent", 0) + r.get("s1_sent", 0) + r.get("s2_sent", 0) + r.get("hedge_sent", 0) for r in some)
    assert 0.15 < kills / sent < 0.45                                                      # ~30 % of fills removed
    slip = SW.run_window(dict(w), SW.BASE, SW.Stress(ask_shift=0.02))
    assert slip["entry_kills"] >= 1 or slip.get("entry_gross", 0) / max(slip.get("entry_qty", 1), 1) > 0.60   # 0.62 > the 0.60 limit


def test_selection_never_reads_the_test_period():
    per = {p: SW.stats([]) for p in ("train", "validation", "trainval", "test", "all")}
    a = SW.selection_score(per)
    per["test"] = {**per["test"], "pnl_per_market": 99.0, "total_pnl": 1e6}
    assert SW.selection_score(per) == a
