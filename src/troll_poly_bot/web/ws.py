"""A minimal RFC 6455 server, sized for one job: pushing state to the console.

Hand-rolled rather than pulled in as a dependency because the dashboard runs on
``http.server`` and must stay on ONE port -- the box it is deployed to has a
single hole in the firewall, and a second listener would need a second one. The
``websockets`` package in this project is a *client*, used for the venue feeds;
its server is asyncio, and this server is a thread per connection.

Scope, deliberately small:
  * text and binary frames, close, ping, pong
  * permessage-deflate (RFC 7692) without context takeover, when the browser offers it (every current one does):
    a 670 KB state is ~95 KB on the wire, and the browser inflates it natively
  * the three payload-length encodings
  * client->server unmasking (browsers always mask)
  * continuation frames are REJECTED, not reassembled: the only thing the page
    ever sends is a one-line JSON subscription, and silently mishandling a
    fragment is worse than refusing it

Nothing here is a general-purpose websocket library. It is the subset the
dashboard needs, with the parts it does not need closed off rather than faked.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
import zlib

#: RFC 6455 section 1.3. Constant, not a secret.
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: A client frame larger than this is a bug or an attack; the page sends ~50 B.
MAX_CLIENT_FRAME = 64 * 1024


class WSError(Exception):
    """Protocol violation or a closed connection. Always fatal to the socket."""


def accept_key(client_key: str) -> str:
    """The Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key."""
    digest = hashlib.sha1(client_key.strip().encode() + GUID).digest()
    return base64.b64encode(digest).decode()


def handshake_response(client_key: str, extensions: str | None = None) -> bytes:
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept_key(client_key).encode() + b"\r\n"
        + (b"Sec-WebSocket-Extensions: " + extensions.encode() + b"\r\n" if extensions else b"")
        + b"\r\n"
    )


#: What the server answers to a permessage-deflate offer: no context takeover either way, so every message is
#: compressed (and inflated) on its own -- no per-connection compressor state to keep or to get out of step.
PMD_RESPONSE = "permessage-deflate; server_no_context_takeover; client_no_context_takeover"
_TAIL = b"\x00\x00\xff\xff"


def negotiate_deflate(offer_header: str | None) -> str | None:
    """The Sec-WebSocket-Extensions answer for a client's offer, or None (no compression)."""
    for offer in (offer_header or "").split(","):
        if offer.split(";")[0].strip().lower() == "permessage-deflate":
            return PMD_RESPONSE
    return None


def deflate_message(data: bytes, level: int = 6) -> bytes:
    """One message's payload for a frame with RSV1 set (RFC 7692 7.2.1)."""
    c = zlib.compressobj(level, zlib.DEFLATED, -15)
    out = c.compress(data) + c.flush(zlib.Z_SYNC_FLUSH)
    return out[:-4] if out.endswith(_TAIL) else out


def inflate_message(data: bytes, limit: int = 1 << 20) -> bytes:
    """A compressed client message back to its bytes (bounded: a client message is a one-line JSON)."""
    d = zlib.decompressobj(-15)
    out = d.decompress(data + _TAIL, limit)
    if d.unconsumed_tail:
        raise WSError("client message inflates past the limit")
    return out


def encode_frame(payload: bytes, opcode: int = OP_TEXT, mask: bool = False, rsv1: bool = False) -> bytes:
    """Build a single final frame. Servers send unmasked; `mask` is for tests. ``rsv1``: the payload is
    permessage-deflate compressed (data frames only)."""
    b0 = 0x80 | opcode | (0x40 if rsv1 else 0)
    if len(payload) < 126:
        header = struct.pack("!BB", b0, len(payload) | (0x80 if mask else 0))
    elif len(payload) < (1 << 16):
        header = struct.pack("!BBH", b0, 126 | (0x80 if mask else 0), len(payload))
    else:
        header = struct.pack("!BBQ", b0, 127 | (0x80 if mask else 0), len(payload))
    if not mask:
        return header + payload
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return header + key + masked


def _read_exactly(readfn, n: int) -> bytes:
    """`readfn` may return short reads; a websocket frame cannot tolerate that."""
    buf = b""
    while len(buf) < n:
        chunk = readfn(n - len(buf))
        if not chunk:
            raise WSError("connection closed mid-frame")
        buf += chunk
    return buf


def read_frame(readfn) -> tuple[int, bytes]:
    """Read one client frame. Returns (opcode, payload). Raises WSError."""
    opcode, payload, _rsv1 = read_frame_ex(readfn)
    return opcode, payload


def read_frame_ex(readfn) -> tuple[int, bytes, bool]:
    """Read one client frame: (opcode, payload, compressed). ``compressed`` = RSV1 (permessage-deflate)."""
    b0, b1 = _read_exactly(readfn, 2)
    rsv1 = bool(b0 & 0x40)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F

    if opcode == OP_CONT or not fin:
        # see the module docstring: refused rather than half-supported
        raise WSError("fragmented frames are not supported")
    if length == 126:
        (length,) = struct.unpack("!H", _read_exactly(readfn, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _read_exactly(readfn, 8))
    if length > MAX_CLIENT_FRAME:
        raise WSError(f"client frame too large: {length}")
    if not masked:
        # RFC 6455 5.1: a server MUST close on an unmasked client frame
        raise WSError("client frame was not masked")

    key = _read_exactly(readfn, 4)
    payload = _read_exactly(readfn, length) if length else b""
    return opcode, bytes(b ^ key[i % 4] for i, b in enumerate(payload)), rsv1


def close_frame(code: int = 1000, reason: str = "") -> bytes:
    return encode_frame(struct.pack("!H", code) + reason.encode(), OP_CLOSE)
