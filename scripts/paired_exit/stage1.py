"""Stage 1: the ENTRY rule on the hold-to-settlement baseline (structure C, no exits), per asset.
Exposure fixed: 5 shares, 10 USDC per window, 1 open leg, 3 buys per window."""
import itertools, sys
sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import sim as S
from grid import run_grid
cfgs = []
for k, e, off, band, ag, late in itertools.product((1.0, 2.0, 3.0), (0.0, 0.01, 0.02, 0.04), (0.02, 0.03),
                                                   ((0.15, 0.85), (0.30, 0.70), (0.50, 0.90)), (-1.0, 0.34), (0.0, 0.02)):
    cfgs.append(S.Params(name=f"k{k} e{e} off{off} band{band[0]}-{band[1]} ag{ag} late{late}", structure="C", stop=None,
                         k_sig=k, min_edge=e, cost_c=-1, limit_off=off, lo=band[0], hi=band[1], min_agree=ag,
                         late_edge_add=late))
run_grid("stage1", cfgs, workers=4)
