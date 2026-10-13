"""The Polymarket ORDER-BOOK entry trigger for the pair-lock engines (2026-10-02, the user: "figure out new book spot
mode instead of crypto spot").

The book model (strategy/paired_exit.BookModel on paired_exit.book_features_at, the triangle bot's 2026-10-02 study)
predicts the UP token's mid move over the next second, in cents, from both books' touches and near-touch depth on
the bot's own 200 ms samples. A reading of at least ``book_threshold`` cents (either way) is a signal; its sign is
the side. ``signal_source`` says how it combines with the crypto spot shock the triangle engine already produces:

    crypto   the spot shock only (strategy B as it ran; this module is not even built)
    book     the book only: a crypto shock is NOT a signal
    either   the crypto shock when it fires, else the book
    both     a crypto shock that the book showed on the same side (>= book_confirm cents) within book_confirm_ms

One signal per market, direction and second (paired_exit.Signals), as the replay (scripts/pairlock/replay_book.py).
Everything after the trigger -- the entry gate, hedges, locks -- is the engine's and unchanged.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from . import paired_exit as px

SOURCES = px.SIGNAL_SOURCES          # ("crypto", "book", "both", "either")


@dataclass
class BookTriggerParams:
    """TPB_PAIRLOCK_<FIELD> (pairlock_engine_v2.PairLockConfig.from_env)."""
    signal_source: str = "crypto"
    book_threshold: float = 1.5                 # cents of predicted next-1 s UP mid move
    book_confirm: float = 0.0                   # both: the book must have shown >= this (cents) on the crypto side
    book_confirm_ms: float = 1000.0
    book_max_age_ms: float = 500.0              # a pending book signal older than this is dropped, not traded
    book_model_path: str = "data/replay/paired_exit/live/book_btc.json"

    def __post_init__(self) -> None:
        if self.signal_source not in SOURCES:
            raise ValueError(f"signal_source {self.signal_source!r}: one of {SOURCES}")
        if self.book_threshold <= 0.0:
            raise ValueError("book_threshold must be > 0 cents")


class BookTrigger:
    """Scores every 200 ms sample of a market and hands the engine its entry signal (``take``)."""

    def __init__(self, prm: BookTriggerParams, model: px.BookModel | None = None) -> None:
        self.prm = prm
        self.model = model if model is not None else px.BookModel.load(prm.book_model_path)
        self.bufs: dict[str, px.PointBuffer] = {}
        self.recent: dict[str, deque] = {}          # slug -> (t, score) within book_confirm_ms (mode "both")
        self.last: dict[str, dict] = {}             # slug -> the latest score
        self.pending: dict[str, dict] = {}          # slug -> an unconsumed book signal
        self.signals = px.Signals()
        self.n_scored = 0
        self.n_signals = 0
        self.n_stale = 0

    # ---- every sample

    def observe(self, slug: str, market, point: dict, now: float) -> float | None:
        buf = self.bufs.get(slug)
        if buf is None:
            buf = self.bufs[slug] = px.PointBuffer(market.open_ts, market.close_ts)
        if not buf.add(point):
            return None
        score = self.model.score(px.book_features_at(buf.points))
        if score is None or math.isnan(score):
            return None
        t = float(point["t"])
        self.n_scored += 1
        self.last[slug] = {"t": t, "score": score}
        rec = self.recent.setdefault(slug, deque())
        rec.append((t, score))
        while rec and t - rec[0][0] > self.prm.book_confirm_ms:
            rec.popleft()
        if self.prm.signal_source in ("book", "either") and abs(score) >= self.prm.book_threshold:
            d = 1 if score > 0 else -1
            if self.signals.take(slug, d, t):
                self.pending[slug] = {"t": t, "dir": d, "score": score}
                self.n_signals += 1
        return score

    # ---- the engine's evaluation

    def take(self, slug: str, now: float, crypto_dir: int | None) -> tuple[int, str, float | None] | None:
        """(direction, what fired, the book score) for this evaluation, or None. ``crypto_dir`` is the crypto
        shock's side when the engine's own trigger fired at this evaluation, else None."""
        src = self.prm.signal_source
        if src == "crypto":
            return None if crypto_dir is None else (crypto_dir, "crypto", None)
        if src == "both":
            if crypto_dir is None:
                return None
            seen = [b * crypto_dir for tb, b in self.recent.get(slug, ()) if now - tb <= self.prm.book_confirm_ms]
            if seen and max(seen) >= self.prm.book_confirm:
                return crypto_dir, "both", max(seen) * crypto_dir
            return None
        p = self.pending.get(slug)
        if p is not None and now - p["t"] > self.prm.book_max_age_ms:
            self.pending.pop(slug, None)
            self.n_stale += 1
            p = None
        if src == "either" and crypto_dir is not None:
            self.pending.pop(slug, None)                 # the crypto shock takes this second (as the replay)
            return crypto_dir, "crypto", None
        if p is None:
            return None
        self.pending.pop(slug, None)
        return p["dir"], "book", p["score"]

    def forget(self, slug: str) -> None:
        for d in (self.bufs, self.recent, self.last, self.pending):
            d.pop(slug, None)

    def snapshot(self) -> dict:
        return {"source": self.prm.signal_source, "threshold_cents": self.prm.book_threshold,
                "confirm_cents": self.prm.book_confirm, "scored": self.n_scored, "signals": self.n_signals,
                "stale": self.n_stale, "model": self.model.meta,
                "last": {s: round(v["score"], 3) for s, v in self.last.items()}}
