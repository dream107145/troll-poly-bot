"""High-resolution tick recorder for spot -> token lead/lag research.

    nice -n 10 python scripts/record_ticks.py --minutes 70 [--out data/ticks]

READ-ONLY: public market-data websockets only. No account, no key, no order.

The 5-minute archive (data/charts) samples every ~233 ms, which cannot resolve
a lag measured in tens of milliseconds. This records every event, each with
the two clocks that must never be mixed:

    et / vt   the EXCHANGE's own event time (Bybit book ``ts``, Binance trade
              ``T``, Coinbase ``time``, Polymarket ``timestamp``)
    rt        when THIS machine received it, clock-corrected against Binance
              (live.ClockSync, re-synced every 5 min and written as ``clk``)

Binance's bookTicker stream carries no event time at all; its quotes have
``rt`` only. Polymarket records are written only when a token's top of book
(best bid/ask, the size there, or depth within 2 ticks) changes.

Record kinds (gzip JSONL, one object per line):
    q   spot quote     x s b a bq aq [et] rt
    t   spot trade     x s p q side et rt
    b   token book     tok bb ba bbs bas bd2 ad2 vt rt
    pt  token trade    tok p sz side vt rt
    m   market meta    slug s up dn open close
    clk clock sync     off rtt rt
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import json
import logging
import signal
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import orjson
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from troll_poly_bot.feeds.markets import parse_market                     # noqa: E402
from troll_poly_bot.feeds.polymarket import GAMMA_BASE, window_epoch      # noqa: E402
from troll_poly_bot.feeds.spot import BINANCE_UNLISTED, BINANCE_WS, BYBIT_WS, COINBASE_WS   # noqa: E402
from troll_poly_bot.live import CLOB_WS, ClockSync                        # noqa: E402

ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
log = logging.getLogger("record")


class Writer:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = gzip.open(path, "ab", compresslevel=4)
        self.buf: list[bytes] = []
        self.n = 0
        self.path = path

    def put(self, row: dict) -> None:
        self.buf.append(orjson.dumps(row))
        self.n += 1

    def flush(self) -> None:
        if self.buf:
            self.fh.write(b"\n".join(self.buf) + b"\n")
            self.buf.clear()

    def close(self) -> None:
        self.flush()
        self.fh.close()


class Recorder:
    def __init__(self, out: Writer, assets: tuple[str, ...]) -> None:
        self.w = out
        self.assets = assets
        self.clock = ClockSync()
        self.stop = asyncio.Event()
        self.tokens: dict[str, str] = {}          # token -> slug
        self.books: dict[str, list[dict]] = {}    # token -> [bids, asks]
        self.best: dict[str, list] = {}           # token -> [best bid, best ask] reported by the venue
        self.last: dict[str, tuple] = {}          # token -> last written top
        self.resub = asyncio.Event()
        self.counts: dict[str, int] = {}

    def now(self) -> float:
        return self.clock.now()

    def _count(self, k: str) -> None:
        self.counts[k] = self.counts.get(k, 0) + 1

    # ----------------------------------------------------------- discovery
    async def discover(self) -> None:
        seen: set[str] = set()
        while not self.stop.is_set():
            now_s = self.now() / 1000.0
            found: dict[str, str] = {}
            for off in (0, 1):
                ep = window_epoch(now_s, off)
                for a in self.assets:
                    slug = f"{a.lower()}-updown-5m-{ep}"
                    try:
                        row = await asyncio.to_thread(self._gamma, slug)
                    except Exception as exc:                      # noqa: BLE001
                        log.debug("gamma %s: %s", slug, exc)
                        continue
                    meta = parse_market(row) if row else None
                    if meta is None:
                        continue
                    m = meta.market
                    found[m.yes_token_id] = slug
                    found[m.no_token_id] = slug
                    if slug not in seen:
                        seen.add(slug)
                        self.w.put({"k": "m", "slug": slug, "s": a, "up": m.yes_token_id, "dn": m.no_token_id,
                                    "open": m.open_ts, "close": m.close_ts})
            # keep windows that have not closed yet; drop the rest
            if found and set(found) != set(self.tokens):
                self.tokens = found
                self.resub.set()
            await asyncio.sleep(15)

    @staticmethod
    def _gamma(slug: str):
        url = f"{GAMMA_BASE}/markets?slug={urllib.parse.quote(slug)}"
        req = urllib.request.Request(url, headers={"User-Agent": "troll-poly-bot-recorder/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.load(r)
        return rows[0] if isinstance(rows, list) and rows else None

    # ------------------------------------------------------------ polymarket
    def _top(self, tok: str) -> tuple | None:
        bids, asks = self.books.get(tok, ({}, {}))
        rb, ra = self.best.get(tok, (None, None))
        bl = sorted((p for p in bids if rb is None or p <= rb + 1e-9), reverse=True)
        al = sorted(p for p in asks if ra is None or p >= ra - 1e-9)
        bb = bl[0] if bl else None
        ba = al[0] if al else None
        bd2 = sum(bids[p] for p in bl if bb is not None and p >= bb - 0.02 - 1e-9)
        ad2 = sum(asks[p] for p in al if ba is not None and p <= ba + 0.02 + 1e-9)
        return (bb, ba, bids.get(bb, 0.0) if bb is not None else 0.0,
                asks.get(ba, 0.0) if ba is not None else 0.0, round(bd2, 2), round(ad2, 2))

    def _emit_top(self, tok: str, vt: float, rt: float) -> None:
        top = self._top(tok)
        if top is None or top == self.last.get(tok):
            return
        self.last[tok] = top
        bb, ba, bbs, bas, bd2, ad2 = top
        self.w.put({"k": "b", "tok": tok, "bb": bb, "ba": ba, "bbs": bbs, "bas": bas, "bd2": bd2, "ad2": ad2,
                    "vt": vt, "rt": rt})
        self._count("b")

    @staticmethod
    def _levels(raw) -> dict[float, float]:
        out = {}
        for lv in raw or []:
            try:
                p, s = float(lv["price"]), float(lv["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if s > 0:
                out[p] = s
        return out

    def _on_poly(self, raw) -> None:
        if not raw or raw[0] not in (91, 123, "[", "{"):
            return
        try:
            msgs = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return
        rt = self.now()
        if isinstance(msgs, dict):
            msgs = [msgs]
        for m in msgs:
            ev = m.get("event_type")
            try:
                vt = float(m.get("timestamp"))
            except (TypeError, ValueError):
                vt = None
            if ev == "book":
                tok = str(m.get("asset_id"))
                if tok not in self.tokens:
                    continue
                self.books[tok] = [self._levels(m.get("bids")), self._levels(m.get("asks"))]
                self.best[tok] = [None, None]
                self._emit_top(tok, vt, rt)
            elif ev == "price_change":
                touched = set()
                for ch in m.get("price_changes") or []:
                    tok = str(ch.get("asset_id"))
                    if tok not in self.books:
                        continue
                    try:
                        p, s = float(ch["price"]), float(ch["size"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    side = ch.get("side")
                    book = self.books[tok][0] if side == "BUY" else self.books[tok][1] if side == "SELL" else None
                    if book is None:
                        continue
                    if s <= 0:
                        book.pop(p, None)
                    else:
                        book[p] = s
                    rb, ra = ch.get("best_bid"), ch.get("best_ask")
                    try:
                        self.best[tok] = [float(rb) if rb not in (None, "") else None,
                                          float(ra) if ra not in (None, "") else None]
                    except (TypeError, ValueError):
                        pass
                    touched.add(tok)
                for tok in touched:
                    self._emit_top(tok, vt, rt)
            elif ev == "last_trade_price":
                tok = str(m.get("asset_id"))
                if tok in self.tokens:
                    self.w.put({"k": "pt", "tok": tok, "p": _f(m.get("price")), "sz": _f(m.get("size")),
                                "side": m.get("side"), "vt": vt, "rt": rt})
                    self._count("pt")

    async def poly(self) -> None:
        while not self.stop.is_set():
            toks = list(self.tokens)
            if not toks:
                await asyncio.sleep(1)
                continue
            try:
                async with websockets.connect(CLOB_WS, open_timeout=20, ping_interval=10,
                                              max_size=8 * 1024 * 1024, max_queue=2048) as ws:
                    self.books.clear(); self.best.clear(); self.last.clear()
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    self.resub.clear()
                    log.info("polymarket connected (%d tokens)", len(toks))
                    async for raw in ws:
                        if self.stop.is_set() or self.resub.is_set():
                            break
                        self._on_poly(raw)
            except Exception as exc:                              # noqa: BLE001
                self._count("poly_reconnect")
                log.warning("polymarket dropped: %s", str(exc)[:120])
                await asyncio.sleep(1)

    # ------------------------------------------------------------------ spot
    async def binance(self) -> None:
        syms = {f"{a.lower()}usdt": a for a in self.assets if a not in BINANCE_UNLISTED}
        streams = "/".join(f"{s}@bookTicker" for s in syms) + "/" + "/".join(f"{s}@aggTrade" for s in syms)
        while not self.stop.is_set():
            try:
                async with websockets.connect(BINANCE_WS + streams, open_timeout=20, ping_interval=15) as ws:
                    log.info("binance connected")
                    async for raw in ws:
                        if self.stop.is_set():
                            break
                        rt = self.now()
                        msg = orjson.loads(raw)
                        st, d = msg.get("stream", ""), msg.get("data") or {}
                        a = syms.get(st.split("@")[0])
                        if a is None:
                            continue
                        if st.endswith("@bookTicker"):
                            self.w.put({"k": "q", "x": "bn", "s": a, "b": _f(d.get("b")), "a": _f(d.get("a")),
                                        "bq": _f(d.get("B")), "aq": _f(d.get("A")), "rt": rt})
                            self._count("q_bn")
                        elif st.endswith("@aggTrade"):
                            self.w.put({"k": "t", "x": "bn", "s": a, "p": _f(d.get("p")), "q": _f(d.get("q")),
                                        "side": -1 if d.get("m") else 1, "et": _f(d.get("T")), "rt": rt})
                            self._count("t_bn")
            except Exception as exc:                              # noqa: BLE001
                self._count("bn_reconnect")
                log.warning("binance dropped: %s", str(exc)[:120])
                await asyncio.sleep(1)

    async def bybit(self) -> None:
        syms = {f"{a}USDT": a for a in self.assets}
        while not self.stop.is_set():
            try:
                async with websockets.connect(BYBIT_WS, open_timeout=20, ping_interval=None) as ws:
                    args = [f"orderbook.1.{s}" for s in syms] + [f"publicTrade.{s}" for s in syms]
                    for i in range(0, len(args), 10):
                        await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 10]}))
                    log.info("bybit connected")
                    state: dict[str, list] = {}
                    last_ping = time.monotonic()
                    while not self.stop.is_set():
                        if time.monotonic() - last_ping > 20:
                            await ws.send(json.dumps({"op": "ping"}))
                            last_ping = time.monotonic()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=25)
                        except asyncio.TimeoutError:
                            continue
                        rt = self.now()
                        msg = orjson.loads(raw)
                        topic = msg.get("topic", "")
                        if topic.startswith("orderbook.1."):
                            sym = topic.split(".")[-1]
                            a = syms.get(sym)
                            d = msg.get("data") or {}
                            s = state.setdefault(sym, [None, None, None, None])
                            try:
                                if d.get("b"):
                                    s[0], s[2] = float(d["b"][0][0]), float(d["b"][0][1])
                                if d.get("a"):
                                    s[1], s[3] = float(d["a"][0][0]), float(d["a"][0][1])
                            except (IndexError, TypeError, ValueError):
                                continue
                            if a and s[0] and s[1]:
                                self.w.put({"k": "q", "x": "by", "s": a, "b": s[0], "a": s[1], "bq": s[2], "aq": s[3],
                                            "et": _f(msg.get("ts")), "rt": rt})
                                self._count("q_by")
                        elif topic.startswith("publicTrade."):
                            a = syms.get(topic.split(".")[-1])
                            for d in msg.get("data") or []:
                                self.w.put({"k": "t", "x": "by", "s": a, "p": _f(d.get("p")), "q": _f(d.get("v")),
                                            "side": 1 if str(d.get("S", "")).lower() == "buy" else -1,
                                            "et": _f(d.get("T")), "rt": rt})
                                self._count("t_by")
            except Exception as exc:                              # noqa: BLE001
                self._count("by_reconnect")
                log.warning("bybit dropped: %s", str(exc)[:120])
                await asyncio.sleep(1)

    async def coinbase(self) -> None:
        prods = {f"{a}-USD": a for a in self.assets}
        while not self.stop.is_set():
            try:
                async with websockets.connect(COINBASE_WS, open_timeout=20, ping_interval=15) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "product_ids": list(prods),
                                              "channels": ["ticker", "heartbeat"]}))
                    log.info("coinbase connected")
                    async for raw in ws:
                        if self.stop.is_set():
                            break
                        rt = self.now()
                        msg = orjson.loads(raw)
                        if msg.get("type") != "ticker":
                            continue
                        a = prods.get(msg.get("product_id", ""))
                        if a is None:
                            continue
                        et = None
                        with contextlib.suppress(Exception):
                            et = datetime.fromisoformat(str(msg["time"]).replace("Z", "+00:00")).timestamp() * 1000.0
                        self.w.put({"k": "q", "x": "cb", "s": a, "b": _f(msg.get("best_bid")), "a": _f(msg.get("best_ask")),
                                    "bq": _f(msg.get("best_bid_size")), "aq": _f(msg.get("best_ask_size")),
                                    "et": et, "rt": rt})
                        self._count("q_cb")
            except Exception as exc:                              # noqa: BLE001
                self._count("cb_reconnect")
                log.warning("coinbase dropped: %s", str(exc)[:120])
                await asyncio.sleep(1)

    # ---------------------------------------------------------------- driver
    async def clock_loop(self) -> None:
        while not self.stop.is_set():
            await self.clock.resync(samples=5)
            self.w.put({"k": "clk", "off": self.clock.offset_ms, "rtt": self.clock.rtt_ms, "rt": self.now()})
            log.info("clock offset %+.1f ms (rtt %.0f ms)", self.clock.offset_ms, self.clock.rtt_ms)
            for _ in range(300):
                if self.stop.is_set():
                    return
                await asyncio.sleep(1)

    async def flush_loop(self) -> None:
        last_report = time.monotonic()
        while not self.stop.is_set():
            await asyncio.sleep(2)
            self.w.flush()
            if time.monotonic() - last_report > 60:
                last_report = time.monotonic()
                log.info("records %d  %s", self.w.n, {k: v for k, v in sorted(self.counts.items())})

    async def run(self, minutes: float) -> None:
        await self.clock.resync(samples=5)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.stop.set)
        loop.call_later(minutes * 60.0, self.stop.set)
        tasks = [asyncio.create_task(c) for c in (self.clock_loop(), self.discover(), self.poly(), self.binance(),
                                                   self.bybit(), self.coinbase(), self.flush_loop())]
        await self.stop.wait()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=5)
        self.w.close()
        log.info("done: %d records -> %s  %s", self.w.n, self.w.path, self.counts)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=70.0)
    ap.add_argument("--out", default="data/ticks")
    ap.add_argument("--assets", default=",".join(ASSETS))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    path = Path(args.out) / f"ticks_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}.jsonl.gz"
    rec = Recorder(Writer(path), tuple(a.strip().upper() for a in args.assets.split(",") if a.strip()))
    asyncio.run(rec.run(args.minutes))


if __name__ == "__main__":
    main()
