"""DATA_ONLY multi-source BTC feed recorder for the feed benchmark (2026-09-30).

    python scripts/record_feed_benchmark.py --minutes 240 [--assets BTC] [--out data/ticks]

READ-ONLY: public market-data websockets only. No account, no key, no order.
Analyse with scripts/research_feed_benchmark.py.

Every quote, trade and (for a few venues) depth snapshot from every source the
bot can run (feeds/spot.py + feeds/sources.py), plus the Polymarket 5-minute
books for the same assets (scripts/record_ticks.py's recorder, unchanged), all
in ONE process so that every receipt time comes from the same clock. The four
timestamps the brief asks to keep apart are all kept:

    et   the EXCHANGE's own event time (its clock; absent where it sends none)
    rt   our receipt time, epoch ms, clock-corrected against Binance (ClockSync)
    mt   our receipt time on the monotonic clock, ms (ordering within this run)
    vt   the VENUE's timestamp on a Polymarket book message (record kind ``b``)

Record kinds (gzip JSONL), in addition to record_ticks.py's ``b``/``pt``/``m``/``clk``:
    q    quote  x s b a bq aq et rt mt seq v
    t    trade  x s p q side et rt mt seq v
    d    depth  x s bids asks et rt seq          (top 10, best first)
    lag  loop lag sample: rt ms                 (how late a 100 ms timer fired)
    h    health every 60 s: rt feeds{...} streams{...}
``v`` is the validator's verdict for that message (ok / dup / out_of_order /
gap / stale); duplicates are recorded and flagged, not dropped, so the report
can count them.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from record_ticks import Recorder, Writer                                              # noqa: E402
from troll_poly_bot.feeds.sources import ADAPTERS, DepthSnapshot, StreamValidator     # noqa: E402
from troll_poly_bot.feeds.spot import (                                               # noqa: E402
    FeedStatus, SpotQuote, TradeTick, run_binance, run_bybit, run_coinbase, run_kraken,
)

log = logging.getLogger("feedbench")

#: the four existing sources first, then the candidates; binance_depth is depth only
SOURCES = ("binance", "bybit", "coinbase", "kraken", "okx", "okx_swap", "bitstamp", "binance_futures",
           "bybit_linear", "deribit", "hyperliquid", "coinbase_adv", "gemini", "bitfinex", "pm_chainlink",
           "binance_depth")


class FeedRecorder:
    def __init__(self, rec: Recorder, sources: tuple[str, ...]) -> None:
        self.rec = rec
        self.w = rec.w
        self.sources = sources
        self.health: dict[str, FeedStatus] = {}
        self.validator = StreamValidator()
        self.n: dict[str, int] = {}

    def _count(self, k: str) -> None:
        self.n[k] = self.n.get(k, 0) + 1

    def on_quote(self, q: SpotQuote) -> None:
        v = self.validator.check(q.exchange, q.asset, "q", q.sequence, q.event_ts_ms, q.ts_ms)
        self.w.put({"k": "q", "x": q.exchange, "s": q.asset, "b": q.bid, "a": q.ask, "bq": q.bid_size, "aq": q.ask_size,
                    "et": q.event_ts_ms, "rt": q.ts_ms, "mt": time.monotonic() * 1000.0, "seq": q.sequence, "v": v})
        self._count(f"q_{q.exchange}")

    def on_trade(self, t: TradeTick) -> None:
        v = self.validator.check(t.exchange, t.asset, "t", t.sequence, t.event_ts_ms, t.ts_ms)
        self.w.put({"k": "t", "x": t.exchange, "s": t.asset, "p": t.price, "q": t.qty, "side": 1 if t.aggressor_buy else -1,
                    "et": t.event_ts_ms, "rt": t.ts_ms, "mt": time.monotonic() * 1000.0, "seq": t.sequence, "v": v})
        self._count(f"t_{t.exchange}")

    def on_depth(self, d: DepthSnapshot) -> None:
        self.w.put({"k": "d", "x": d.exchange, "s": d.asset, "bids": d.bids[:10], "asks": d.asks[:10],
                    "et": d.event_ts_ms, "rt": d.ts_ms, "seq": d.sequence})
        self._count(f"d_{d.exchange}")

    async def run_source(self, name: str, stop: asyncio.Event) -> None:
        assets, clock = self.rec.assets, self.rec.now
        kw = dict(on_trade=self.on_trade, health=self.health)
        try:
            if name == "binance":
                await run_binance(assets, self.on_quote, clock, stop, **kw)
            elif name == "bybit":
                await run_bybit(assets, self.on_quote, clock, stop, **kw)
            elif name == "coinbase":
                await run_coinbase(assets, self.on_quote, clock, stop, **kw)
            elif name == "kraken":
                await run_kraken(assets, self.on_quote, clock, stop, **kw)
            else:
                # depth only where the venue offers a cheap snapshot stream (OBI study)
                depth = self.on_depth if name in ("okx", "bitstamp", "binance_depth") else None
                await ADAPTERS[name](assets, self.on_quote, clock, stop, on_depth=depth, **kw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                                    # noqa: BLE001 -- one source never stops the rest
            log.error("source %s died: %s: %s", name, type(exc).__name__, exc)

    async def lag_loop(self, stop: asyncio.Event) -> None:
        """How late a 100 ms timer fires: the recorder's own processing delay, which the
        receipt times include. Reported so a busy loop cannot pass for a slow exchange."""
        while not stop.is_set():
            t0 = time.monotonic()
            await asyncio.sleep(0.1)
            late = (time.monotonic() - t0 - 0.1) * 1000.0
            if late > 2.0:
                self.w.put({"k": "lag", "rt": self.rec.now(), "ms": round(late, 2)})
            await asyncio.sleep(0.9)

    async def health_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for _ in range(60):
                if stop.is_set():
                    return
                await asyncio.sleep(1)
            now = self.rec.now()
            self.w.put({"k": "h", "rt": now, "feeds": {k: v.as_dict(now) for k, v in self.health.items()},
                        "streams": self.validator.snapshot()})
            down = [k for k, v in self.health.items() if not v.connected]
            log.info("sources %d up%s | %s", len(self.health) - len(down), f", down: {down}" if down else "",
                     {k: v for k, v in sorted(self.n.items())})


async def main_async(args) -> None:
    assets = tuple(a.strip().upper() for a in args.assets.split(",") if a.strip())
    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    path = Path(args.out) / f"feedbench_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}.jsonl.gz"
    rec = Recorder(Writer(path), assets)
    fr = FeedRecorder(rec, sources)
    await rec.clock.resync(samples=5)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, rec.stop.set)
    loop.call_later(args.minutes * 60.0, rec.stop.set)
    tasks = [asyncio.create_task(c) for c in (rec.clock_loop(), rec.discover(), rec.poly(), rec.flush_loop(),
                                               fr.lag_loop(rec.stop), fr.health_loop(rec.stop))]
    tasks += [asyncio.create_task(fr.run_source(s, rec.stop)) for s in sources]
    log.info("recording %s from %d sources for %.0f min -> %s", ",".join(assets), len(sources), args.minutes, path)
    await rec.stop.wait()
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(t, timeout=5)
    rec.w.close()
    log.info("done: %d records -> %s  %s", rec.w.n, path, {**rec.counts, **fr.n})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--minutes", type=float, default=240.0)
    ap.add_argument("--out", default="data/ticks")
    ap.add_argument("--assets", default="BTC")
    ap.add_argument("--sources", default=",".join(SOURCES))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
