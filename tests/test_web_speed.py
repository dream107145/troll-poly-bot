"""Dashboard speed (2026-10-05, the user: "too slow when switching bots or pages; show trade history asap"):
the live stream is permessage-deflate compressed, pushed at most every WS_MIN_PUSH_S (a market switch at once), and
with patch=1 sends only the parts of the state that changed after its first frame; HTTP JSON and static files are
gzip-compressed, and static files answer 304 when unchanged."""
from __future__ import annotations

import asyncio
import gzip
import json
import time
import urllib.error
import urllib.request

import pytest
import websockets

from test_ws_live import SLUG, _recv, _recv_until, live_server  # noqa: F401  (the fixture)

from troll_poly_bot.web import server as srv
from troll_poly_bot.web import ws


# ───────────────────────────── the codec ─────────────────────────────


def test_deflate_round_trip_and_negotiation():
    data = json.dumps({"price_history": [{"t": i, "up": 0.5} for i in range(2000)]}).encode()
    packed = ws.deflate_message(data)
    assert len(packed) < len(data) / 4 and not packed.endswith(b"\x00\x00\xff\xff")
    assert ws.inflate_message(packed) == data
    assert ws.negotiate_deflate("permessage-deflate; client_max_window_bits") == ws.PMD_RESPONSE
    assert ws.negotiate_deflate("x-webkit-deflate-frame") is None and ws.negotiate_deflate(None) is None
    assert b"Sec-WebSocket-Extensions: permessage-deflate" in ws.handshake_response("k", ws.PMD_RESPONSE)
    assert b"Sec-WebSocket-Extensions" not in ws.handshake_response("k")


def test_a_compressed_frame_sets_rsv1_and_reads_back():
    payload = ws.deflate_message(b'{"chart": "btc"}')
    frame = ws.encode_frame(payload, mask=True, rsv1=True)
    assert frame[0] & 0x40
    view = memoryview(frame)
    pos = [0]

    def readfn(n):
        out = bytes(view[pos[0]:pos[0] + n])
        pos[0] += n
        return out
    opcode, body, packed = ws.read_frame_ex(readfn)
    assert opcode == ws.OP_TEXT and packed and ws.inflate_message(body) == b'{"chart": "btc"}'


# ───────────────────────────── the stream ─────────────────────────────


@pytest.mark.asyncio
async def test_the_stream_is_compressed_when_the_browser_offers_it(live_server):
    url, writer, headers = live_server
    async with websockets.connect(f"{url}?chart={SLUG}", additional_headers=headers) as conn:   # offers deflate
        assert "permessage-deflate" in (conn.response.headers.get("Sec-WebSocket-Extensions") or "")
        first = await _recv(conn)
        assert [p["t"] for p in first["price_history"][SLUG]] == [1, 2, 3]
    async with websockets.connect(f"{url}?chart={SLUG}", additional_headers=headers, compression=None) as conn:
        assert not conn.response.headers.get("Sec-WebSocket-Extensions")
        assert (await _recv(conn))["equity"] == 100.0                                       # plain still works


@pytest.mark.asyncio
async def test_patch_mode_sends_only_what_changed_and_merges_back(live_server):
    url, writer, headers = live_server
    async with websockets.connect(f"{url}?chart={SLUG}&patch=1", additional_headers=headers) as conn:
        base = await _recv(conn)
        assert "_patch" not in base and base["equity"] == 100.0           # the first frame is the whole state
        writer.set([1, 2, 3, 4])
        msg = await _recv_until(conn, lambda m: (m.get("price_history") or {}).get(SLUG))
        assert msg["_patch"] == 1 and msg["bot"] == "main"
        assert "equity" not in msg                                         # unchanged: not resent
        assert [p["t"] for p in msg["price_history"][SLUG]] == [4]
        merged = {**base, **{k: v for k, v in msg.items() if k not in ("_patch", "_del")}}
        assert merged["equity"] == 100.0 and merged["history_partial"] is True


@pytest.mark.asyncio
async def test_pushes_are_throttled_but_a_market_switch_goes_at_once(live_server, monkeypatch):
    url, writer, headers = live_server
    monkeypatch.setattr(srv, "WS_MIN_PUSH_S", 0.3)
    async with websockets.connect(f"{url}?chart={SLUG}", additional_headers=headers) as conn:
        await _recv(conn)
        stamps = []
        for _ in range(4):                                                 # the writer rewrites every 20 ms
            await _recv(conn)
            stamps.append(time.monotonic())
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert min(gaps) >= 0.25, gaps                                    # not every write
        monkeypatch.setattr(srv, "WS_MIN_PUSH_S", 5.0)
        await asyncio.sleep(0.4)
        while True:                                                        # drain
            try:
                await _recv(conn, timeout=0.2)
            except asyncio.TimeoutError:
                break
        t0 = time.monotonic()
        await conn.send(json.dumps({"chart": "eth-updown-5m-1"}))
        msg = await _recv_until(conn, lambda m: "eth-updown-5m-1" in m["price_history"], timeout=3.0)
        assert time.monotonic() - t0 < 1.0 and len(msg["price_history"]["eth-updown-5m-1"]) == 2


# ───────────────────────────── HTTP ─────────────────────────────


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, dict(r.headers), r.read()


def test_json_and_static_are_gzipped_and_static_revalidates(live_server):
    url, writer, headers = live_server
    http = url.replace("ws://", "http://").replace("/ws", "")
    writer.set(list(range(1, 400)))
    time.sleep(0.1)
    st, h, body = _get(f"{http}/api/live?chart={SLUG}", {**headers, "Accept-Encoding": "gzip"})
    assert st == 200 and h.get("Content-Encoding") == "gzip"
    assert len(json.loads(gzip.decompress(body))["price_history"][SLUG]) == 399
    st, h, body = _get(f"{http}/api/live?chart={SLUG}", headers)                  # no gzip asked: plain
    assert "Content-Encoding" not in h and json.loads(body)["equity"] == 100.0
    st, h, body = _get(f"{http}/static/app.js", {**headers, "Accept-Encoding": "gzip"})
    assert st == 200 and h.get("Content-Encoding") == "gzip" and h.get("ETag") and h.get("Cache-Control") == "no-cache"
    assert b"scheduleRender" in gzip.decompress(body)
    try:
        st2, _h2, body2 = _get(f"{http}/static/app.js", {**headers, "If-None-Match": h["ETag"]})
    except urllib.error.HTTPError as exc:                                           # urllib raises on 304
        st2, body2 = exc.code, b""
    assert st2 == 304 and body2 == b""
