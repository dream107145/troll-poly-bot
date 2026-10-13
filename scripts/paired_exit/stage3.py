"""Stage 3: exit combinations x exposure caps on each asset's Stage-1 entry."""
import itertools, sys
from dataclasses import replace
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import sim as S, pe_select as SEL
from grid import run_grid
from stage2 import entry_of

EXITS = {
    "hold": dict(),
    "time30": dict(time_exit_s=30.0),
    "fairTP": dict(structure="D", tp_offsets=(0.02,), tp_fracs=(1.0,), tp_fair=0.03),
    "fairTP+time30": dict(structure="D", tp_offsets=(0.02,), tp_fracs=(1.0,), tp_fair=0.03, time_exit_s=30.0),
    "fairTP+time30+stop0.4": dict(structure="D", tp_offsets=(0.02,), tp_fracs=(1.0,), tp_fair=0.03, time_exit_s=30.0, stop=0.40),
    "fairTP+time30+thesis0.3": dict(structure="D", tp_offsets=(0.02,), tp_fracs=(1.0,), tp_fair=0.03, time_exit_s=30.0,
                                    thesis="fair", thesis_drop=0.30),
}

def configs_for(base):
    b = S.Params(**{**base, "name": "x", "structure": "C", "stop": None})
    out = []
    for (ename, ekw), cap, mo, mb in itertools.product(EXITS.items(), (5.0, 10.0, 15.0, 20.0, 25.0, 50.0), (1, 2, 3), (1, 3, 10)):
        if mo > 1 and cap < 10.0:
            continue
        out.append(replace(b, name=f"{ename} cap{cap:g} open{mo} buys{mb}", max_window_usdc=cap, max_unpaired_usdc=cap,
                           max_dir_usdc=cap, max_open=mo, max_buys=mb, **ekw))
    for (ename, ekw), cap in itertools.product((("fairTP+time30", EXITS["fairTP+time30"]), ("hold", {})), (10.0, 20.0, 25.0)):
        out.append(replace(b, name=f"{ename} sh10 cap{cap:g} open1 buys3", shares=10.0, max_window_usdc=cap,
                           max_unpaired_usdc=cap, max_dir_usdc=cap, max_open=1, max_buys=3, **ekw))
    return out

if __name__ == "__main__":
    recs = SEL.load("stage1")
    cfgs = {a: configs_for(entry_of(SEL.choose(rs)[0])) for a, rs in SEL.by_asset(recs).items()}
    run_grid("stage3", cfgs, workers=4)
