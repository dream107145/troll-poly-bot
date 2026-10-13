"""Stage 2: EXIT structures on each asset's Stage-1 entry (chosen on TRAIN/VALIDATION). C exits grid, D ladders,
E optional pair, F inventory, A equal shares, B today's rule."""
import itertools, sys
from dataclasses import replace
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import sim as S, pe_select as SEL
from grid import run_grid

def entry_of(rec):
    p = dict(rec["params"]); p.pop("name"); p.pop("structure")
    p["cost_c"] = -1
    return p

def configs_for(base):
    b = S.Params(**{**base, "name": "x", "structure": "C"})
    out = []
    for st, th, hold, te in itertools.product((None, 0.10, 0.20, 0.30), (None, "fair0.10", "fair0.20", "mom2.5"),
                                              (None, 60.0, 120.0), (None, 30.0)):
        kw = {"stop": st, "max_hold_s": hold, "time_exit_s": te}
        if th and th.startswith("fair"):
            kw.update(thesis="fair", thesis_drop=float(th[4:]))
        elif th:
            kw.update(thesis="mom", k_rev=float(th[3:]))
        out.append(replace(b, name=f"C stop{st} thesis{th} hold{hold} time{te}", **kw))
    # D: take-profit ladders (5 shares: one level; 10 shares: two levels of 5), maker / taker, static / fair-anchored
    for sh, levels in ((5.0, (0.05,)), (5.0, (0.10,)), (5.0, (0.20,)), (10.0, (0.05, 0.10)), (10.0, (0.10, 0.20))):
        for mode in ("maker", "taker"):
            for st in (None, 0.20):
                for te in (None, 30.0):
                    fr = (1.0,) if len(levels) == 1 else (0.5, 0.5)
                    out.append(replace(b, name=f"D sh{sh:g} tp{levels} {mode} stop{st} time{te}", structure="D", shares=sh,
                                       tp_offsets=levels, tp_fracs=fr, tp_mode=mode, stop=st, time_exit_s=te,
                                       max_window_usdc=max(b.max_window_usdc, sh * 0.85), max_unpaired_usdc=max(b.max_unpaired_usdc, sh * 0.85),
                                       max_dir_usdc=max(b.max_dir_usdc, sh * 0.85)))
    for fo in (0.0, 0.03):
        out.append(replace(b, name=f"D sh5 tp fair+{fo} maker", structure="D", tp_offsets=(0.02,), tp_fracs=(1.0,), tp_fair=fo, stop=None))
    # E: the optional pair on top of the leg's own exits
    for tgt in (0.93, 0.95, 0.97):
        for st in (None, 0.20):
            for te in (None, 30.0):
                out.append(replace(b, name=f"E pair<{tgt} stop{st} time{te}", structure="E", pair_target=tgt, stop=st, time_exit_s=te))
    # F: equal-share pair + inventory management (10 shares each side)
    for st in (None, 0.20):
        out.append(replace(b, name=f"F sh10 stop{st}", structure="F", shares=10.0, stop=st, tp_offsets=(0.10,), tp_fracs=(1.0,),
                           max_window_usdc=20.0, max_unpaired_usdc=20.0, max_dir_usdc=20.0))
    # A: equal shares held (same entry signal, 5 + 5 shares)
    out.append(replace(b, name="A equal shares hold", structure="A", stop=None, max_window_usdc=20.0, max_unpaired_usdc=20.0, max_dir_usdc=20.0))
    # B: today's rule (old entry model, resting pair < 0.95 strict, $50/market) and its leg alone
    out.append(S.Params(name="B today (pair<0.95, hold)", structure="B", entry="old", lo=0.20, hi=0.93, limit_off=0.03, pair_target=0.95, stop=None))
    out.append(S.Params(name="B leg only (no pair, $50)", structure="B", entry="old", lo=0.20, hi=0.93, limit_off=0.03, pair_target=None, stop=None))
    return out

if __name__ == "__main__":
    recs = SEL.load("stage1")
    cfgs = {}
    for a, rs in SEL.by_asset(recs).items():
        best, _ = SEL.choose(rs)
        cfgs[a] = configs_for(entry_of(best))
    run_grid("stage2", cfgs, workers=4)
