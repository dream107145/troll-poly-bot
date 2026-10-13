"""BTC 200 ms window cache for the book-signal study (2026-10-02): the paired-exit cache format (cache.py) rebuilt
with every archived window up to now, written to data/replay/book_signal/btc.npz so the paired-exit study's own
cache (and its splits) stay untouched.

    nice -n 19 .venv/bin/python scripts/book_signal/cache_btc.py
"""
import sys

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import cache as C   # noqa: E402

C.OUT = "/root/troll-poly-bot/data/replay/book_signal"
print(C.build("btc"), flush=True)
