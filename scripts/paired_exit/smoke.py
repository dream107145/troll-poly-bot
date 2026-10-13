import sys, time
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import run as R, sim as S, pe_lib as L
t = time.time()
wins = R.load("BTC")
print("load", round(time.time() - t, 1), len(wins))
cfgs = [
    S.Params(name="B today", structure="B", entry="old", lo=0.20, hi=0.93, limit_off=0.03, pair_target=0.95, stop=None),
    S.Params(name="B leg only", structure="B", entry="old", lo=0.20, hi=0.93, limit_off=0.03, pair_target=None, stop=None),
    S.Params(name="C0 hold", structure="C", stop=None, min_edge=0.02),
    S.Params(name="C stop10", structure="C", stop=0.10, min_edge=0.02),
    S.Params(name="D ladder", structure="D", stop=0.10, tp_offsets=(0.05, 0.10), tp_fracs=(0.5, 0.5), shares=10, max_window_usdc=10, max_unpaired_usdc=10, max_dir_usdc=10, min_edge=0.02),
    S.Params(name="E", structure="E", stop=0.10, pair_target=0.95, min_edge=0.02),
    S.Params(name="A", structure="A", stop=None, min_edge=-9, max_window_usdc=20, max_unpaired_usdc=20),
    S.Params(name="F", structure="F", stop=0.10, shares=10, tp_offsets=(0.05,), max_window_usdc=20, max_unpaired_usdc=20, max_dir_usdc=20, min_edge=0.02),
]
print(R.head())
for p in cfgs:
    t = time.time()
    res = R.simulate(wins, p)
    for s, (pn, legs, n) in res.items():
        print(R.row(p.name, s, R.metrics(pn, legs, n)))
    print("#", p.name, round(time.time() - t, 1), "s", file=sys.stderr)
