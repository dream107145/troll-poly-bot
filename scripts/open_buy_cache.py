"""BTC 200 ms window cache up to now for the open-buy simulation (2026-10-05): the paired-exit cache format
(scripts/paired_exit/cache.py) rebuilt from every archived BTC window into data/replay/open_buy/btc.npz.

    nice -n 19 .venv/bin/python scripts/open_buy_cache.py
"""
import sys

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import cache as C   # noqa: E402

C.OUT = "/root/troll-poly-bot/data/replay/open_buy"
print(C.build("btc"), flush=True)
