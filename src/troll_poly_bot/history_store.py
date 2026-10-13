"""Live chart points, one append-only JSONL file per market.

Why not in the state file
-------------------------
The bot used to put every live market's full history (``price_history``) into
``data/live_state.json``, rewritten four times a second. With 14 markets that
was 5.1 MB, 99.8 % of the file, and ``orjson.dumps`` of it took ~56 ms. orjson
holds the GIL while it serialises, so running it in a thread did not help: the
event loop froze for ~56 ms several times a second, and every spot and book
message that arrived meanwhile waited. Measured on 2026-09-24: the network
delivered Binance trades 129 ms after the trade; the bot saw them 270-410 ms
after, and 41 % of its busy CPU was this one write.

Here each new point costs one small ``orjson.dumps`` and one buffered append.
The state file keeps only ``history_meta`` (point count per market), and the
dashboard server reads the one market it is drawing from
``<state stem>.history/<slug>.jsonl``.

Layout: ``data/live_state.json`` -> ``data/live_state.history/<slug>.jsonl``
(tagged runs: ``data/live_state.spot.json`` -> ``data/live_state.spot.history/``).
"""
from __future__ import annotations

import re
from pathlib import Path

import orjson

#: slugs come from the venue (``btc-updown-5m-1790260200``); the web server
#: also takes one from a query string, so anything else is refused outright
_SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def history_dir(state_path: str | Path) -> Path:
    p = Path(state_path)
    return p.with_name(p.stem + ".history")


def safe_slug(slug: str | None) -> bool:
    return bool(slug) and _SAFE_SLUG.match(slug) is not None


class HistoryStore:
    """The writer: the bot appends each sampled point, flushes once per tick."""

    def __init__(self, state_path: str | Path) -> None:
        self.dir = history_dir(state_path)
        self._fh: dict[str, object] = {}
        self.errors = 0
        self.reset()

    def reset(self) -> None:
        """A run starts with an empty history, as the in-memory deques do."""
        self.close()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            for f in self.dir.glob("*.jsonl"):
                f.unlink(missing_ok=True)
        except OSError:
            self.errors += 1

    def append(self, slug: str, point: dict) -> None:
        if not safe_slug(slug):
            return
        try:
            fh = self._fh.get(slug)
            if fh is None:
                fh = (self.dir / f"{slug}.jsonl").open("ab")
                self._fh[slug] = fh
            fh.write(orjson.dumps(point) + b"\n")
        except (OSError, TypeError):
            self.errors += 1

    def flush(self) -> None:
        for fh in self._fh.values():
            try:
                fh.flush()
            except OSError:
                self.errors += 1

    def drop(self, slug: str) -> None:
        """The market left the live set (it is archived to data/charts/)."""
        fh = self._fh.pop(slug, None)
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        if safe_slug(slug):
            try:
                (self.dir / f"{slug}.jsonl").unlink(missing_ok=True)
            except OSError:
                self.errors += 1

    def close(self) -> None:
        for fh in self._fh.values():
            try:
                fh.close()
            except OSError:
                pass
        self._fh.clear()


def read_points(state_path: str | Path, slug: str | None, limit: int = 2000) -> list[dict] | None:
    """The reader: one market's points, oldest first, at most ``limit`` (the
    bot's in-memory cap). None when the market has no file. A torn last line
    (the writer mid-append) is skipped, never fatal."""
    if not safe_slug(slug):
        return None
    try:
        raw = (history_dir(state_path) / f"{slug}.jsonl").read_bytes()
    except OSError:
        return None
    out: list[dict] = []
    for line in raw.splitlines()[-limit:]:
        try:
            out.append(orjson.loads(line))
        except orjson.JSONDecodeError:
            continue
    return out
