"""Local dashboard server.

Stdlib only, bound to loopback. This drives the *real* Python model -- the same
pricer, paper engine and latency model the tests cover -- rather than a second
implementation in JavaScript that would quietly drift out of agreement with it.

    python -m troll_poly_bot.web

Runs are executed on a worker thread so the UI stays responsive, and can be
cancelled: the sim takes a progress callback that aborts when it returns False.
"""
from __future__ import annotations

import contextlib
import gzip
import json
import os
import logging
import mimetypes
import re
import threading
import urllib.parse
import time
import traceback
import uuid
import subprocess
import sys
import webbrowser
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import orjson

from .. import archive, control, earnings, ledger
from ..execution.latency import PROFILES
from ..feeds import account
from ..history_store import read_points
from ..sim import SimResult, run_sim
from . import auth, login, ws
from .schema import apply_params, build_schema
from ..signals.costs import FeeSchedule

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
LIVE_STATE = Path("data/live_state.json")
TRADE_LOG = Path("data/live_trades.jsonl")

#: The operator's clock. The page prints every time in it (TZ_SHIFT_MS in
#: app.js) and calendar buckets start on its midnight, whatever the server's
#: own timezone is. GMT+9 has no daylight saving, so a fixed offset is exact.
DISPLAY_TZ = timezone(timedelta(hours=9), "GMT+9")

# ─────────────────────────────── several bots ───────────────────────────────
# Every bot process writes its own pair of files: the untagged default
# (data/live_state.json, data/live_trades.jsonl -- the "main" bot, which is the
# one this server can start and stop) and, per --data-tag X,
# data/live_state.X.json and data/live_trades.X.jsonl. The page picks one with
# ?bot=<id>. Ids come from a query string, so they are matched against a strict
# pattern and only ever turned into those two names: never a path.
MAIN_BOT = "main"
#: the repository (ecosystem.*.config.js, data/): the server runs from it; derived from this file when it does not
REPO_ROOT = next((d for d in (Path.cwd(), Path(__file__).resolve().parents[3]) if (d / "pyproject.toml").exists()), Path.cwd())
_BOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_STATE_NAME = re.compile(r"^live_state\.([A-Za-z0-9][A-Za-z0-9_-]{0,39})\.json$")
#: a state file untouched this long belongs to a bot that is not running
BOT_STALE_AFTER_S = 90.0


def bot_paths(bot: str | None) -> tuple[Path, Path] | None:
    """(state file, trade log) for a bot id, or None for an id that cannot be one."""
    if not bot or bot == MAIN_BOT:
        return LIVE_STATE, TRADE_LOG
    if not _BOT_ID.match(bot):
        return None
    return (LIVE_STATE.with_name(f"live_state.{bot}.json"),
            TRADE_LOG.with_name(f"live_trades.{bot}.jsonl"))


class _BotIndex:
    """Every bot that has a state file, summarised; a file is re-read only when
    its mtime or size changes, so listing never parses the same state twice."""

    FIELDS = ("mode", "deploy_mode", "strategy", "event_driven", "execution_scenario", "equity", "pnl",
              "starting_balance", "uptime_s", "live", "account_sync", "balance", "started")

    def __init__(self) -> None:
        self._cache: dict[str, tuple[tuple[float, int], dict]] = {}
        self._lock = threading.Lock()

    def _summary(self, path: Path, stamp: tuple[float, int]) -> dict:
        with self._lock:
            hit = self._cache.get(str(path))
            if hit is not None and hit[0] == stamp:
                return hit[1]
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            d = None
        if not isinstance(d, dict):
            # torn mid-write: keep the last good summary rather than blank the picker
            return hit[1] if hit is not None else {}
        out = {k: d.get(k) for k in self.FIELDS}
        out["positions"] = len(d.get("open_positions") or [])
        with self._lock:
            self._cache[str(path)] = (stamp, out)
        return out

    def list(self) -> list[dict]:
        rows = []
        try:
            files = sorted(LIVE_STATE.parent.glob("live_state*.json"))
        except OSError:
            files = []
        for p in files:
            if p.name == LIVE_STATE.name:
                bot = MAIN_BOT
            else:
                m = _STATE_NAME.match(p.name)
                if m is None:
                    continue
                bot = m.group(1)
            try:
                st = p.stat()
            except OSError:
                continue
            age = time.time() - st.st_mtime
            rows.append({"id": bot, "running": age < BOT_STALE_AFTER_S, "stale_s": round(age),
                         "managed": bot == MAIN_BOT, "last_ts": round(st.st_mtime, 3),
                         **self._summary(p, (st.st_mtime, st.st_size))})
        # most recent first (the user, 2026-10-07): the running bots by their start, the newest run on top, then
        # the stopped ones by when they stopped (their state file's last write); the name breaks a tie
        rows.sort(key=lambda r: (not r["running"],
                                 -(float(r.get("started") or 0.0) if r["running"] else r["last_ts"]), r["id"]))
        return rows


BOTS = _BotIndex()


def _filter_live(payload: dict, chart: str | None, since: int | None = None) -> dict:
    """Trim the state to what one page needs.

    The bot writes every live market's full 5-minute history ten times a
    second; the page only ever draws ONE of them. Sending all of them at that
    rate is most of the bytes for nothing -- the full state is ~9 MB, one
    market ~375 KB. ``history_meta`` keeps the point count per market so the
    picker and the archive exporter still know what exists without receiving
    it.

    ``since`` trims further to the points the client does not have yet, which
    is what makes a 100 ms poll affordable: a steady-state tick is a handful
    of points rather than the whole window. ``history_partial`` tells the
    client whether to append or replace. When the client is further behind
    than the buffer reaches, every point is new, so a full replace is both
    what it gets and what it wants.
    """
    hist = payload.get("price_history") or {}
    # a current bot writes the counts itself and no points (history_store.py);
    # an older one wrote every market's points into the state
    if "history_meta" not in payload:
        payload["history_meta"] = {slug: len(pts) for slug, pts in hist.items()}
    # the decision marks ride with the chart the same way: only the selected market's
    marks = payload.get("signal_marks") or {}
    payload["signal_marks"] = {chart: marks[chart]} if chart and chart in marks else {}
    shocks = payload.get("shock_marks") or {}
    payload["shock_marks"] = {chart: shocks[chart]} if chart and chart in shocks else {}
    # Order marks are NOT trimmed: the order list beside the chart shows every
    # market's orders as they are created and filled, and they are small -- a
    # few dozen per market that traded, none for the rest.
    payload["order_marks"] = {slug: m for slug, m in (payload.get("order_marks") or {}).items() if m}
    if chart is None:
        # nothing selected: the page draws no chart, so it needs no history
        payload["price_history"] = {}
        return payload
    if chart not in hist:
        payload["price_history"] = {}
        return payload
    pts = hist[chart]
    if since is not None:
        fresh = [p for p in pts if p.get("t", 0) > since]
        # an empty delta for a market we DO have is still {slug: []}, not {}:
        # it means "nothing new, keep what you hold", not "this market is gone"
        payload["history_partial"] = len(fresh) < len(pts)
        pts = fresh
    payload["price_history"] = {chart: pts}
    return payload


DEFAULT_HISTORY_PAGE_SIZE = 25
MAX_HISTORY_PAGE_SIZE = 200


def _qs_int(qs: dict[str, list[str]], key: str, default: int) -> int:
    """One query param, coerced to int. Garbage or absent falls back quietly --
    a malformed ``?page=`` should not 500 a dashboard poll."""
    try:
        return int((qs.get(key) or [default])[0])
    except (TypeError, ValueError):
        return default


def _history(page: int = 1, page_size: int = DEFAULT_HISTORY_PAGE_SIZE,
             view: str = "trips", mode: str = "all", trade_log: Path | None = None) -> dict:
    """Every fill and settlement the live bot has ever written, across restarts.

    The dashboard state is per-process: a restart starts a fresh exchange at
    its --balance with an empty ledger, and stop() removes the state file. The
    trade log is append-only and survives all of that, so it is the record --
    and it only grows, across days or weeks of paper trading. The table is
    therefore paginated rather than hard-capped: KPIs and the equity curve
    still summarise every row on disk, but ``rows`` returns one page, newest
    first, so the operator can page back through the whole history instead
    of only ever seeing the most recent slice of it.
    """
    trade_log = trade_log or TRADE_LOG
    rows = ledger.load(trade_log)
    if mode in ("paper", "live"):
        want_live = mode == "live"
        rows = [r for r in rows if bool(r.get("live")) == want_live]
    settles = [r for r in rows if r.get("event") == "settle"]
    fills = [r for r in rows if r.get("event") == "fill"]
    # A late resolution is a correction row for a window already booked at its mark: it adds to
    # realised P&L, but it is not another settled window, and the window wins or loses by its
    # real result (mark + correction), not by the sign of either row.
    late = {r.get("slug"): r for r in settles if r.get("resolved_late")}
    windows = [r for r in settles if not r.get("resolved_late")]

    def _window_pnl(r: dict) -> float:
        fix = late.get(r.get("slug")) if r.get("unresolved") else None
        if fix is not None and fix.get("window_pnl") is not None:
            return float(fix["window_pnl"])
        return float(r.get("pnl") or 0.0)
    realised, curve = 0.0, []
    for r in settles:
        pnl = float(r.get("pnl") or 0.0)
        realised += pnl
        curve.append({"ts": r.get("ts"), "slug": r.get("slug"),
                      "pnl": round(pnl, 4), "cum": round(realised, 4)})

    # One position is several events -- often two partial entries, a sell and
    # a settlement -- which in a flat list reads as the same trade repeated.
    # The default view folds them into round trips, each with its own buy and
    # sell time; the raw event stream stays available underneath.
    trips = ledger.round_trips(rows)
    view = view if view in ("trips", "events") else "trips"
    if view == "events":
        rows = [dict(r, corrected_late=True) if (r.get("event") == "settle" and r.get("unresolved")
                                                 and r.get("slug") in late) else r for r in rows]
    listed = (trips[::-1] if view == "trips" else rows[::-1])   # newest first

    page_size = min(max(page_size, 1), MAX_HISTORY_PAGE_SIZE)
    total_rows = len(listed)
    total_pages = max(1, -(-total_rows // page_size))          # ceil div
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    return {
        "fills": len(fills),
        "settled": len(windows),
        "wins": sum(1 for r in windows if _window_pnl(r) > 0),
        "losses": sum(1 for r in windows if _window_pnl(r) <= 0),
        "realised_pnl": round(realised, 4),
        "curve": curve,
        "view": view,
        "mode": mode,
        "modes": sorted({str(r.get("mode")) for r in ledger.load(trade_log) if r.get("mode")}),
        "trips_summary": ledger.summary(trips),
        "rows": listed[start:start + page_size],
        "events_total": len(rows),
        "trips_total": len(trips),
        "page": page,
        "page_size": page_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
    }


# ─────────────────────────────── one trade ───────────────────────────────
# Clicking a ledger row opens that trade: its window's price path, the fills
# from the trade log, and every order the bot sent on the window. Orders come
# from two places. A window still in the bot's live set carries them in the
# state file (``order_marks``, updated as each is created, filled or killed);
# every order that has finished is also a ``trade`` record in the bot's
# forensics file, which is all that is left once the window is reaped.

#: the point fields the trade chart draws; an archived point carries far more
#: (per-exchange spot, depth, order flow) that the dialog never reads
POINT_FIELDS = ("t", "left", "up", "down", "ub", "ua", "db", "da", "spot")
_WINDOW_RE = re.compile(r"-updown-(\d+)m-(\d+)$")


def decisions_path(trade_log: Path) -> Path:
    """``live_trades.X.jsonl`` -> ``decisions.X.jsonl``: the bot's decision journal, one line per order event."""
    name = trade_log.name
    if name.startswith("live_trades"):
        return trade_log.with_name("decisions" + name[len("live_trades"):])
    return trade_log.with_name("decisions.jsonl")


#: the journal's reject events -> the order list's outcome words
_DONE_EVENTS = {"ORDER_REJECTED": "rejected", "ORDER_EXPIRED": "expired", "ORDER_CANCELLED": "cancelled"}


class _DecisionIndex:
    """Every order of every market from the bot's decision journal (``ORDER_CREATED`` .. ``ORDER_FILLED`` /
    ``ORDER_REJECTED``), in the order-mark shape. The journal is written for EVERY order the moment it happens,
    so it is complete where the forensics file is not: a forensics ``trade`` line exists only for an order that
    was rejected or whose position the bot closed itself; a buy held to settlement, a pair bid, a recovery buy
    never get one (2026-10-05: 40 filled orders, 0 trade lines). Re-read only when the file changes."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[tuple[float, int], dict[str, list[dict]]]] = {}
        self._lock = threading.Lock()

    def _load(self, path: Path) -> dict[str, list[dict]]:
        try:
            st = path.stat()
        except OSError:
            return {}
        stamp = (st.st_mtime, st.st_size)
        with self._lock:
            hit = self._cache.get(str(path))
        if hit is not None and hit[0] == stamp:
            return hit[1]
        by_id: dict[str, dict] = {}
        try:
            with path.open("rb") as fh:
                for line in fh:
                    if b'"ORDER_' not in line:
                        continue
                    try:
                        row = orjson.loads(line)
                    except orjson.JSONDecodeError:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                    if not isinstance(row, dict):
                        continue
                    ev, oid, t = row.get("decision"), row.get("order_id"), _finite(row.get("timestamp"))
                    if not oid or not ev:
                        continue
                    o = by_id.get(oid)
                    if ev == "ORDER_CREATED":
                        o = by_id.setdefault(oid, {"id": oid, "slug": row.get("market"), "t": t, "side": row.get("side"),
                                                   # a resting other-side bid is journaled with pair_of: its own kind
                                                   "kind": "pair" if row.get("pair_of") else str(row.get("kind") or "entry").lower(), "limit": _finite(row.get("price")),
                                                   "size": _finite(row.get("size")), "tif": row.get("tif"), "outcome": "sent",
                                                   "price": None, "filled": 0.0, "notional": 0.0, "t_done": None, "reason": None,
                                                   "pair_of": row.get("pair_of"), "resting": False})
                        if o["slug"] is None:
                            o["slug"] = row.get("market")
                        continue
                    if o is None:
                        continue
                    if ev == "ORDER_SUBMITTED":
                        o["t_sent"] = t
                    elif ev == "ORDER_ACCEPTED":
                        if row.get("resting"):
                            o["resting"] = True
                            o["outcome"] = "resting" if o["outcome"] == "sent" else o["outcome"]
                    elif ev in ("ORDER_FILLED", "ORDER_PARTIALLY_FILLED"):
                        q, px = _finite(row.get("size")), _finite(row.get("price"))
                        if q and px is not None:
                            o["notional"] += q * px
                            o["filled"] += q
                            o["price"] = o["notional"] / o["filled"]
                        elif px is not None:
                            o["price"] = px
                        o["outcome"] = "filled" if ev == "ORDER_FILLED" else "partial"
                        o["t_done"] = t
                    elif ev in _DONE_EVENTS:
                        if o["outcome"] in ("filled", "partial"):
                            # a partly filled fill-and-kill: the rest was killed; what filled stands
                            o["t_done"] = o["t_done"] or t
                        else:
                            o["outcome"] = _DONE_EVENTS[ev]
                            o["reason"] = row.get("reason") or o.get("reason")
                            o["t_done"] = t
        except OSError:
            return {}
        by_slug: dict[str, list[dict]] = {}
        for o in by_id.values():
            if o.get("slug"):
                by_slug.setdefault(o["slug"], []).append(o)
        for rows in by_slug.values():
            rows.sort(key=lambda o: o.get("t") or 0.0)
        with self._lock:
            self._cache[str(path)] = (stamp, by_slug)
        return by_slug

    def orders(self, path: Path, slug: str) -> list[dict]:
        return [dict(o) for o in self._load(path).get(slug, [])]


DECISIONS = _DecisionIndex()


def _close_stale(orders: list[dict], close_ts: float, running: bool) -> list[dict]:
    """An order still "sent" / "resting" after its window closed did not fill: a resting bid the engine pulled
    (the paper venue drops a cancelled order without a result, so the journal never saw its end) or one that
    expired at the close. Shown as cancelled, not as working."""
    if time.time() * 1000.0 < close_ts + 60_000.0:
        return orders
    for o in orders:
        if o.get("outcome") in ("sent", "resting"):
            o["outcome"] = "cancelled"
            o["reason"] = o.get("reason") or "not filled: pulled by the bot or expired at the close"
            o["t_done"] = o.get("t_done") or close_ts
    return orders


def result_check(rows: list[dict], outcome: str | None) -> dict | None:
    """The window's result recomputed from its fills alone -- payout of the winning side's shares, plus what
    sells returned, minus every buy's cost and every fee -- next to what the bot booked (the settle row, plus
    the late correction when the outcome came after the mark), so the dialog can show whether the two agree."""
    fills = [r for r in rows if r.get("event") == "fill"]
    settles = [r for r in rows if r.get("event") == "settle"]
    if not fills:
        return None
    shares = {"UP": 0.0, "DOWN": 0.0}
    cost = proceeds = fees = 0.0
    buys = sells = 0
    for f in fills:
        side = str(f.get("side") or "")
        q, c = float(f.get("size") or 0.0), float(f.get("cost") or 0.0)
        fees += float(f.get("fee") or 0.0)
        if str(f.get("action") or "BUY").upper() == "SELL":
            shares[side] = shares.get(side, 0.0) - q
            proceeds += -c                      # a sell's cost is recorded as a credit
            sells += 1
        else:
            shares[side] = shares.get(side, 0.0) + q
            cost += c
            buys += 1
    booked = None
    marked = False
    for srow in settles:
        if srow.get("resolved_late"):
            booked = _finite(srow.get("window_pnl"))
            if booked is None:
                booked = (booked or 0.0) + float(srow.get("pnl") or 0.0)
            marked = False
        else:
            booked = float(srow.get("pnl") or 0.0) if booked is None else booked
            marked = bool(srow.get("marked") or srow.get("unresolved"))
    payout = shares.get(outcome, 0.0) if outcome in ("UP", "DOWN") else None
    check = None if payout is None else payout + proceeds - cost - fees
    return {"shares_up": round(shares["UP"], 4), "shares_down": round(shares["DOWN"], 4), "buys": buys, "sells": sells,
            "cost": round(cost, 4), "proceeds": round(proceeds, 4), "fees": round(fees, 4),
            "payout": None if payout is None else round(payout, 4), "outcome": outcome,
            "check_pnl": None if check is None else round(check, 4), "booked_pnl": None if booked is None else round(booked, 4),
            "marked": marked and outcome is None,
            "agree": None if check is None or booked is None or marked else abs(check - booked) < 0.005}


def commands_path(trade_log: Path) -> Path:
    """``live_trades.X.jsonl`` -> ``commands.X.jsonl``: the dashboard's requests to the bot (a manual sell), one JSON
    line each, appended here and read by the bot's control loop within ~2 s."""
    name = trade_log.name
    if name.startswith("live_trades"):
        return trade_log.with_name("commands" + name[len("live_trades"):])
    return trade_log.with_name("commands.jsonl")


def request_buy(bot: str, body: dict, user: str = "") -> dict:
    """The dashboard's Buy: ask the bot to buy ``shares`` of one side of one market at the ask (fill-and-kill).
    Validated here, executed by the bot (live.LiveBot._poll_commands)."""
    paths = bot_paths(bot)
    if paths is None:
        return {"error": "unknown bot"}
    slug = str(body.get("slug") or "")
    side = str(body.get("side") or "").upper()
    try:
        shares = float(body.get("shares") or 0.0)
    except (TypeError, ValueError):
        shares = 0.0
    if not archive.SLUG_RE.match(slug) or side not in ("UP", "DOWN") or not (0.0 < shares <= 1_000.0):
        return {"error": "a market slug, a side (UP/DOWN) and 1-1000 shares are needed"}
    state, age = _read_state(paths[0])
    if state is None or age is None or age > BOT_STALE_AFTER_S:
        return {"error": "that bot is not running"}
    cmd = {"id": f"buy-{int(time.time() * 1000)}", "ts": time.time() * 1000.0, "action": "buy", "slug": slug,
           "side": side, "shares": round(shares, 2), "by": user or "dashboard"}
    try:
        with commands_path(paths[1]).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(cmd) + "\n")
    except OSError as exc:
        return {"error": f"could not write the command: {exc}"}
    return {"ok": True, "command": cmd}


def request_sell(bot: str, body: dict, user: str = "") -> dict:
    """The dashboard's Sell button: ask the bot to sell ``shares`` of one side of one market at the bid (a
    fill-and-kill). Validated here, executed by the bot (live.LiveBot._poll_commands); the result shows up in
    the order list like any other order."""
    paths = bot_paths(bot)
    if paths is None:
        return {"error": "unknown bot"}
    slug = str(body.get("slug") or "")
    side = str(body.get("side") or "").upper()
    try:
        shares = float(body.get("shares") or 0.0)
    except (TypeError, ValueError):
        shares = 0.0
    if not archive.SLUG_RE.match(slug) or side not in ("UP", "DOWN") or not (0.0 < shares <= 10_000.0):
        return {"error": "a market slug, a side (UP/DOWN) and a positive number of shares are needed"}
    state, age = _read_state(paths[0])
    if state is None or age is None or age > BOT_STALE_AFTER_S:
        return {"error": "that bot is not running"}
    cmd = {"id": f"sell-{int(time.time() * 1000)}", "ts": time.time() * 1000.0, "action": "sell", "slug": slug,
           "side": side, "shares": round(shares, 2), "by": user or "dashboard"}
    path = commands_path(paths[1])
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(cmd) + "\n")
    except OSError as exc:
        return {"error": f"could not write the command: {exc}"}
    return {"ok": True, "command": cmd}


def forensics_path(trade_log: Path) -> Path:
    """``live_trades.X.jsonl`` -> ``trade_forensics.X.jsonl``, as the bot names it."""
    name = trade_log.name
    if name.startswith("live_trades"):
        return trade_log.with_name("trade_forensics" + name[len("live_trades"):])
    return trade_log.with_name("trade_forensics.jsonl")


def order_from_forensics(r: dict) -> dict:
    """One consolidated forensics ``trade`` record, in the shape of a live order
    mark, so the page draws both sources with the same code."""
    st = r.get("stages") or {}
    raw = str(r.get("outcome") or "")
    if st.get("FULL_FILL") is not None:
        outcome, done = "filled", st["FULL_FILL"]
    elif st.get("PARTIAL_FILL") is not None:
        outcome, done = "partial", st["PARTIAL_FILL"]
    elif raw.upper().startswith("REJECTED") or st.get("ORDER_REJECTED") is not None:
        outcome, done = "rejected", st.get("ORDER_REJECTED") or r.get("closed_ts")
    else:
        # finished with neither a fill nor a rejection on record
        outcome, done = (raw.split(":", 1)[0].lower() or "unfilled"), r.get("closed_ts")
    limit = r.get("limit_price")
    if limit is None:
        limit = r.get("decision_price")
    return {
        "id": r.get("order_id"),
        "slug": r.get("slug"),
        "t": st.get("ORDER_SUBMITTED") or r.get("t5_sent") or r.get("t9_exit_sent") or st.get("DECISION"),
        "side": r.get("side"),
        "kind": str(r.get("kind") or "entry").lower(),
        "limit": _finite(limit),
        "size": _finite(r.get("quantity")),
        "outcome": outcome,
        "price": _finite(r.get("execution_price")) if outcome in ("filled", "partial") else None,
        # the taker fee this fill paid: the venue's crypto schedule, rate x p(1-p) per share
        "fee": _order_fee(r) if outcome in ("filled", "partial") else None,
        "t_done": done,
        "reason": raw.split(":", 1)[1] if ":" in raw else None,
        # how stale the inputs were at the decision (live.quote_age_ms / spot age), and the
        # timeline the order list's hover shows: exchange trade -> decision -> sent
        "book_age_ms": _finite(r.get("quote_age_ms")),
        "spot_age_ms": _finite(r.get("spot_age_ms")),
        "spot_src": r.get("spot_source"),
        "t_event": _finite(r.get("t0_spot_event")),
        "t_sent": _finite(r.get("t5_sent") or r.get("t9_exit_sent")),
    }


_FEE = FeeSchedule()


def _order_fee(r: dict) -> float | None:
    p, q = _finite(r.get("execution_price")), _finite(r.get("quantity"))
    if p is None or q is None or not (0.0 < p < 1.0):
        return None
    return round(_FEE.taker_fee_per_share(p) * q, 4)


def merge_orders(finished: list[dict], marks: list[dict]) -> list[dict]:
    """Forensics records and live marks, one row per order id, oldest first.

    A live mark is the fresher of the two while an order is working, so it
    overlays the record; a finished record is never walked back to "sent"."""
    out: dict[str, dict] = {}
    for o in finished:
        if o.get("id"):
            out[o["id"]] = o
    for m in marks:
        oid = m.get("id")
        if not oid:
            continue
        mark = {**m, "kind": str(m.get("kind") or "entry").lower()}
        prev = out.get(oid)
        if prev is None:
            out[oid] = mark
        elif not (mark.get("outcome") == "sent" and prev.get("outcome") != "sent"):
            out[oid] = {**prev, **{k: v for k, v in mark.items() if v is not None}}
    return sorted(out.values(), key=lambda o: o.get("t") or 0.0)


class _ForensicsIndex:
    """Each bot's finished orders by market, re-read only when its file changes.

    The file also carries one line per order stage; only the consolidated
    ``trade`` lines are parsed, which is what keeps a re-read cheap."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[tuple[float, int], list[dict]]] = {}
        self._lock = threading.Lock()

    def _load(self, path: Path) -> list[dict]:
        """Every finished order in the file, oldest first."""
        try:
            st = path.stat()
        except OSError:
            return []
        stamp = (st.st_mtime, st.st_size)
        with self._lock:
            hit = self._cache.get(str(path))
        if hit is None or hit[0] != stamp:
            rows: list[dict] = []
            try:
                with path.open("rb") as fh:
                    for line in fh:
                        if b'"trade"' not in line:
                            continue
                        try:
                            row = orjson.loads(line)
                        except orjson.JSONDecodeError:
                            try:                            # NaN, or a torn last line
                                row = json.loads(line)
                            except ValueError:
                                continue
                        if isinstance(row, dict) and row.get("event") == "trade" and row.get("slug"):
                            rows.append(order_from_forensics(row))
            except OSError:
                return []
            rows.sort(key=lambda o: o.get("t") or 0.0)
            hit = (stamp, rows)
            with self._lock:
                self._cache[str(path)] = hit
        return hit[1]

    def orders(self, path: Path, slug: str) -> list[dict]:
        """One market's finished orders."""
        return [dict(o) for o in self._load(path) if o.get("slug") == slug]

    def recent(self, path: Path, limit: int) -> list[dict]:
        """The newest ``limit`` finished orders across every market."""
        return [dict(o) for o in self._load(path)[-limit:]] if limit > 0 else []


FORENSICS = _ForensicsIndex()


class _ArchivedWindows:
    """The last few archived windows opened, cut down to what the trade chart
    draws. One is ~500 KB of JSON; a dialog re-polling a closed trade must not
    parse it again every time."""

    KEEP = 8

    def __init__(self) -> None:
        self._docs: dict[tuple[str, float], dict] = {}
        self._lock = threading.Lock()

    def get(self, slug: str) -> dict | None:
        path = ARCHIVE.one(slug)
        if path is None:
            return None
        try:
            key = (str(path), path.stat().st_mtime)
        except OSError:
            return None
        with self._lock:
            if key in self._docs:
                return self._docs[key]
        try:
            doc = orjson.loads(path.read_bytes())
        except (OSError, orjson.JSONDecodeError):
            return None
        if not isinstance(doc, dict):
            return None
        out = {k: doc.get(k) for k in ("strike", "outcome", "outcome_source")}
        out["points"] = [{k: p[k] for k in POINT_FIELDS if k in p}
                         for p in doc.get("points") or [] if isinstance(p, dict)]
        with self._lock:
            self._docs[key] = out
            while len(self._docs) > self.KEEP:
                self._docs.pop(next(iter(self._docs)))
        return out


ARCHIVED = _ArchivedWindows()


def _read_state(path: Path, attempts: int = 3) -> tuple[dict | None, float | None]:
    """The bot's state and its age in seconds; (None, None) when it cannot be read.
    The bot rewrites the file ten times a second, so a torn read is retried."""
    for i in range(attempts):
        try:
            state = orjson.loads(path.read_bytes())
            age = time.time() - path.stat().st_mtime
        except OSError:
            return None, None
        except orjson.JSONDecodeError:
            time.sleep(0.015 * (i + 1))
            continue
        return (state, age) if isinstance(state, dict) else (None, None)
    return None, None


DEFAULT_ORDERS = 150
MAX_ORDERS = 500


class _FillsIndex:
    """Each bot's fills and settled outcomes from its trade log, re-read only when the file changes:
    the market page's fill ledger (2026-10-12, the user's layout) polls it every few seconds.

    TAKER or MAKER is read from the fee: the venue charges takers rate x p(1-p) per share and makers
    nothing, so a fill with a fee crossed the book and one without rested on it (a pair bid). A row
    with no fee on record falls back to its pair flag."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[tuple[float, int], list[dict], dict[str, str]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _fill(r: dict) -> dict | None:
        size, price = _finite(r.get("size")), _finite(r.get("price"))
        if size is None or price is None or size <= 0.0:
            return None
        fee = _finite(r.get("fee"))
        liq = ("maker" if r.get("pair") else "taker") if fee is None else ("taker" if fee > 1e-9 else "maker")
        return {"ts": _finite(r.get("ts")), "slug": r.get("slug"),
                "action": "SELL" if str(r.get("action") or "").upper() == "SELL" else "BUY",
                "side": str(r.get("side") or "").upper(), "size": size, "price": price,
                "fee": fee or 0.0, "liq": liq, "kind": r.get("kind")}

    def load(self, path: Path) -> tuple[list[dict], dict[str, str]]:
        try:
            st = path.stat()
        except OSError:
            return [], {}
        stamp = (st.st_mtime, st.st_size)
        with self._lock:
            hit = self._cache.get(str(path))
        if hit is None or hit[0] != stamp:
            fills: list[dict] = []
            outcomes: dict[str, str] = {}
            for r in ledger.load(path):
                slug = r.get("slug")
                if not slug:
                    continue
                if r.get("event") == "fill":
                    try:
                        f = self._fill(r)
                    except (TypeError, ValueError):         # a hand-edited or torn row
                        f = None
                    if f is not None:
                        fills.append(f)
                elif r.get("event") == "settle" and not r.get("unresolved"):
                    up = r.get("venue_up") if r.get("venue_up") is not None else r.get("ours_up")
                    if up is not None:
                        outcomes[slug] = "UP" if up else "DOWN"
            fills.sort(key=lambda f: f.get("ts") or 0.0)
            hit = (stamp, fills, outcomes)
            with self._lock:
                self._cache[str(path)] = hit
        return hit[1], hit[2]


FILLS = _FillsIndex()
DEFAULT_FILL_MARKETS = 10
MAX_FILL_MARKETS = 50


def market_fills(bot: str = MAIN_BOT, slug: str | None = None, markets: int = DEFAULT_FILL_MARKETS) -> dict | None:
    """Every fill of one market (``slug``), or of the newest ``markets`` markets, oldest first, with each
    settled market's outcome: the market page's fill ledger. Whole markets only, so the page's running
    "held after" and "overall after this fill" columns always start from a market's first fill."""
    paths = bot_paths(bot)
    if paths is None:
        return None
    fills, outcomes = FILLS.load(Path(paths[1]))
    if slug:
        rows = [f for f in fills if f.get("slug") == slug]
    else:
        n = min(max(markets, 1), MAX_FILL_MARKETS)
        newest: list[str] = []
        for f in reversed(fills):
            if f.get("slug") not in newest:
                newest.append(f["slug"])
                if len(newest) >= n:
                    break
        keep = set(newest)
        rows = [f for f in fills if f.get("slug") in keep]
    return {"bot": bot, "fills": [dict(f) for f in rows],
            "outcomes": {s: outcomes[s] for s in {f["slug"] for f in rows} if s in outcomes}}


def recent_orders(bot: str = MAIN_BOT, limit: int = DEFAULT_ORDERS) -> dict | None:
    """The newest orders across every market, oldest first: what the order list
    beside the live chart starts from. The live push then carries each order as
    it changes, but only for markets still in the bot's live set -- it drops a
    window about four minutes after it closes -- so without this the list would
    start empty and forget each window soon after it ended."""
    paths = bot_paths(bot)
    if paths is None:
        return None
    state_path, trade_log = paths
    limit = min(max(limit, 1), MAX_ORDERS)
    state, _ = _read_state(state_path)
    marks = [{**m, "slug": slug}
             for slug, ms in ((state or {}).get("order_marks") or {}).items() for m in (ms or [])]
    orders = merge_orders(FORENSICS.recent(forensics_path(trade_log), limit), marks)
    return {"bot": bot, "orders": orders[-limit:]}


def trade_detail(slug: str, bot: str = MAIN_BOT, since: int | None = None,
                 have: str | None = None) -> dict | None:
    """Everything the trade dialog draws for one window of one bot, or None
    for a slug or bot id that cannot be one.

    ``since``/``have`` make the dialog's poll cheap: a client that holds the
    ``have`` source up to time ``since`` gets only newer points of a window
    that is still running, and no points at all (``points: null``, "keep
    yours") for one that is archived and can no longer change.
    """
    paths = bot_paths(bot)
    m = _WINDOW_RE.search(slug or "")
    if paths is None or m is None or not archive.SLUG_RE.match(slug):
        return None
    state_path, trade_log = paths
    open_ts = int(m.group(2)) * 1000.0
    close_ts = open_ts + int(m.group(1)) * 60_000.0

    rows = [r for r in ledger.load(trade_log) if r.get("slug") == slug]
    settle = next((r for r in reversed(rows) if r.get("event") == "settle"), None)

    state, age = _read_state(state_path)
    running = state is not None and age is not None and age < BOT_STALE_AFTER_S
    market = None
    marks: list[dict] = []
    if state is not None:
        market = next((x for x in (state.get("live_markets") or []) if x.get("slug") == slug), None) \
            or next((x for x in (state.get("closed_markets") or []) if x.get("slug") == slug), None)
        marks = (state.get("order_marks") or {}).get(slug) or []

    # the archive once the window is settled (complete, and it never changes
    # again); until then the bot's own history file, which grows as we watch
    arch = ARCHIVED.get(slug)
    source, pts = None, None
    if arch is not None:
        source, pts = "archive", arch["points"]
    else:
        hist = read_points(state_path, slug)
        if hist is not None:
            source, pts = "live", [{k: p[k] for k in POINT_FIELDS if k in p} for p in hist]

    partial = False
    if pts is not None:
        if since is not None and have == source and source == "archive":
            pts = None
        else:
            pts = [p for p in pts if open_ts <= (p.get("t") or 0) <= close_ts]
            if since is not None and have == source:
                fresh = [p for p in pts if p["t"] > since]
                partial = len(fresh) < len(pts) or not pts
                pts = fresh

    outcome = (arch or {}).get("outcome") or (market or {}).get("outcome")
    if not outcome and settle is not None:
        up = settle.get("venue_up") if settle.get("venue_up") is not None else settle.get("ours_up")
        outcome = None if up is None else ("UP" if up else "DOWN")
    return {
        "slug": slug,
        "bot": bot,
        "asset": slug.split("-", 1)[0].upper(),
        "open_ts": open_ts,
        "close_ts": close_ts,
        "strike": (market or {}).get("strike") or (arch or {}).get("strike"),
        "outcome": outcome,
        # still moving: the window is running (or awaiting settlement) on a live bot
        "live": running and source == "live",
        "running": running,
        "secs_left": (market or {}).get("secs_left"),
        "source": source,
        "points": pts,
        "partial": partial,
        "fills": [r for r in rows if r.get("event") == "fill"],
        "settle": settle,
        "trips": ledger.round_trips(rows),
        # the decision journal has every order; the forensics record (fees, ages, the latency path) and the live
        # marks overlay it where they exist
        "orders": _close_stale(merge_orders(merge_orders(DECISIONS.orders(decisions_path(trade_log), slug),
                                                         FORENSICS.orders(forensics_path(trade_log), slug)), marks),
                               close_ts, running),
        "result": result_check(rows, outcome),
        "now": time.time() * 1000.0,
    }


class _EarningsCache:
    """Calendar-bucketed PnL, re-read only when the log actually grows.

    The earnings log is append-only and never truncated, so it is the one file
    here that grows without bound. Parsing it on every dashboard poll would be
    wasteful; parsing it when its size or mtime changes is exact, because
    appends always move both.
    """

    def __init__(self) -> None:
        self._rows: list[earnings.Settlement] = []
        self._stamp: tuple[float, int] | None = None
        self._lock = threading.Lock()

    def _fresh(self) -> list[earnings.Settlement]:
        try:
            st = earnings.EARNINGS_LOG.stat()
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            stamp = (0.0, 0)
        with self._lock:
            if stamp != self._stamp:
                self._rows = earnings.load()
                self._stamp = stamp
            return self._rows

    def get(self, period: str, scope: str, span: int | None) -> dict:
        return earnings.aggregate(self._fresh(), period=period, scope=scope, span=span,
                                  tz=DISPLAY_TZ)


EARNINGS = _EarningsCache()


def _portfolio_now(scope: str) -> dict:
    """What the bots in ``scope`` are worth right now: a real account's venue cash + positions (account_sync.portfolio),
    a paper bot's equity. Only RUNNING bots count (a stopped experiment's last paper equity is not a balance)."""
    total, bots = 0.0, []
    for b in BOTS.list():
        if not b.get("running"):
            continue
        is_live = bool(b.get("live"))
        if scope == "live" and not is_live or scope == "paper" and is_live:
            continue
        acc = b.get("account_sync") or {}
        val = acc.get("portfolio") if isinstance(acc, dict) and acc.get("portfolio") is not None else b.get("equity")
        if val is None:
            continue
        total += float(val)
        bots.append({"bot": b["id"], "live": is_live, "portfolio": round(float(val), 4)})
    return {"portfolio": round(total, 4), "bots": bots}


def with_balances(out: dict) -> dict:
    """The balance line of the earnings page (the user, 2026-10-06: "current portfolio is $1.27, not -$89.49"): each
    bucket's end-of-period BALANCE is today's portfolio walked back through the realised P&L logged after that
    moment, so the line always ends at what the account is worth now and moves exactly by what was earned or lost.
    Deposits and withdrawals are not in the earnings log, so periods before one are offset by its amount."""
    anchor = _portfolio_now(out.get("scope", "all"))
    all_time = float((out.get("totals") or {}).get("all_time_pnl") or 0.0)
    now_v = anchor["portfolio"] if anchor["bots"] else None
    for b in out.get("buckets") or []:
        b["balance"] = None if now_v is None else round(now_v - (all_time - float(b.get("cum") or 0.0)), 4)
    out["balance_now"] = now_v
    out["balance_bots"] = anchor["bots"]
    out["balance_opening"] = None if now_v is None else round(now_v - (all_time - float(out.get("opening_cum") or 0.0)), 4)
    return out
CONTROLS = control.ControlFile()
ARCHIVE = archive.ArchiveIndex()


def _controls_payload() -> dict:
    """The catalogue, what is stored per scope, and the kill switch."""
    stored = CONTROLS.read()
    return {
        "spec": control.spec(),
        "start_args": control.START_ARGS,
        "scopes": list(control.SCOPES),
        "stored": {s: stored.get(s) or {} for s in control.SCOPES},
        "updated_ts": stored.get("updated_ts"),
        "kill_active": control.kill_active(),
        "kill_file": str(control.KILL_PATH),
    }


CHART_DIR = Path("data/charts")
#: A market slug, and nothing else — this value becomes a filename.
SLUG_RE = re.compile(r"^[a-z0-9]+-updown-\d+m-\d+$")
MAX_PNG_BYTES = 4 * 1024 * 1024
MAX_TRADE_ROWS = 1500          # downsample the scatter; the browser gains nothing from 40k


class Job:
    def __init__(self, job_id: str, params: dict[str, Any], profiles: list[str]) -> None:
        self.id = job_id
        self.params = params
        self.profiles = profiles
        self.status = "queued"          # queued | running | done | error | cancelled
        self.progress = 0.0
        self.stage = ""
        self.results: list[dict] = []
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.cancel = threading.Event()

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "progress": round(self.progress, 3),
            "stage": self.stage,
            "results": self.results,
            "error": self.error,
            "elapsed_s": round((self.finished or time.time()) - self.started, 2),
        }


def _finite(v: float | None) -> float | None:
    """NaN and inf are not JSON. A single one anywhere breaks the entire
    payload, so nothing numeric leaves this module without passing through."""
    if v is None:
        return None
    v = float(v)
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _downsample(rows: list[dict], limit: int) -> list[dict]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)]


def serialise(result: SimResult, starting_balance: float) -> dict[str, Any]:
    cal = result.calibration()
    return {
        "profile": result.profile_name,
        "reaction_lag_ms": round(result.reaction_lag_ms, 1),
        "pnl": round(result.pnl, 2),
        "final_equity": round(result.final_equity, 2),
        "return_pct": round(100.0 * result.pnl / max(starting_balance, 1e-9), 2),
        "max_drawdown": _finite(round(result.max_drawdown(), 2)),
        "n_trades": result.n_trades,
        "n_windows_traded": result.n_windows_traded,
        "latency": {k: _finite(round(v, 4)) for k, v in result.latency.items()},
        "skips": result.skips,
        "calibration": {k: _finite(round(v, 5)) for k, v in cal.items()} if cal else {},
        "equity_curve": result.equity_curve,
        "trades": _downsample(
            [
                {
                    "fair": _finite(t["fair"]),
                    "price": round(t["price"], 4),
                    "size": round(t["size"], 2),
                    "won": t.get("won", 0.0),
                    "secs_left": round(t["secs_left"], 2),
                }
                for t in result.trades
            ],
            MAX_TRADE_ROWS,
        ),
    }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in ("done", "error", "cancelled"):
            return False
        job.cancel.set()
        return True

    def submit(self, params: dict[str, Any], profiles: list[str]) -> Job:
        job = Job(uuid.uuid4().hex[:12], params, profiles)
        with self._lock:
            self._jobs[job.id] = job
            # keep the map from growing without bound across a long session
            if len(self._jobs) > 40:
                for old in sorted(self._jobs.values(), key=lambda j: j.started)[:10]:
                    if old.status in ("done", "error", "cancelled"):
                        self._jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        job.status = "running"
        try:
            sim_cfg, bot_cfg = apply_params(job.params)
            chosen = [p for p in job.profiles if p in PROFILES] or ["home_broadband"]
            total = len(chosen)

            for i, name in enumerate(chosen):
                if job.cancel.is_set():
                    job.status = "cancelled"
                    return
                job.stage = f"{name} ({i + 1}/{total})"

                def progress(done: int, n: int, _i=i) -> bool:
                    job.progress = (_i + done / max(n, 1)) / total
                    return not job.cancel.is_set()

                result = run_sim(sim_cfg, bot_cfg, PROFILES[name], progress=progress)
                job.results.append(serialise(result, bot_cfg.starting_balance))

            job.progress = 1.0
            job.status = "cancelled" if job.cancel.is_set() else "done"
        except Exception as exc:                      # noqa: BLE001 - surfaced to the UI
            log.exception("job %s failed", job.id)
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
        finally:
            job.finished = time.time()


MANAGER = JobManager()


class BotController:
    """Starts, stops and restarts the live paper trader as a child process.

    Run as a separate process rather than in-thread on purpose: the trader owns
    long-lived websockets and its own asyncio loop, and a hung feed should never
    be able to take the dashboard down with it. Stop is also then a real kill
    rather than a cooperative flag the bot might be too busy to notice.
    """

    #: A bot this server did not start (e.g. launched from a terminal) is
    #: detected from the state file, but cannot be stopped from here -- we do
    #: not own the handle. The UI says so rather than pretending.
    STALE_AFTER_S = 90.0

    #: Start-time arguments this page may set. ``--live`` and ``--armed`` are
    #: deliberately absent: arming real money stays a command-line act, so a
    #: stray click on a web page can never start spending.
    FLAGS: dict[str, tuple[str, str]] = {
        "balance": ("--balance", "num"),
        "assets": ("--assets", "text"),
        "exchanges": ("--exchanges", "text"),
        "min_edge": ("--min-edge", "num"),
        "blend": ("--blend", "num"),
        "take_profit": ("--take-profit", "num"),
        "stop_loss": ("--stop-loss", "num"),
    }

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.last_error: str | None = None
        self.args: dict[str, Any] = {"balance": 100.0, "assets": "",
                                     "exchanges": "binance,bybit,coinbase",
                                     "keep_history": False}
        self._lock = threading.Lock()

    def _external_alive(self) -> bool:
        try:
            return (time.time() - LIVE_STATE.stat().st_mtime) < self.STALE_AFTER_S
        except OSError:
            return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            owned = self.proc is not None and self.proc.poll() is None
            if self.proc is not None and self.proc.poll() is not None and self.started_at:
                # it exited on its own; surface that instead of silently idling
                code = self.proc.returncode
                if code not in (0, None) and not self.last_error:
                    self.last_error = f"bot exited with code {code}"
                self.proc = None
                self.started_at = None
            external = (not owned) and self._external_alive()
            return {
                "running": owned or external,
                "managed": owned,
                "pid": self.proc.pid if owned and self.proc else None,
                "uptime_s": round(time.time() - self.started_at, 1)
                if owned and self.started_at else None,
                "args": dict(self.args),
                "error": self.last_error,
            }

    def _absorb(self, opts: dict[str, Any] | None) -> None:
        """Keep only the flags we know, coerced. Anything else is ignored."""
        for key, (_, kind) in self.FLAGS.items():
            if opts is None or key not in opts:
                continue
            raw = opts[key]
            if raw is None or (isinstance(raw, str) and not raw.strip() and kind == "num"):
                self.args.pop(key, None)
                continue
            if kind == "num":
                try:
                    self.args[key] = float(raw)
                except (TypeError, ValueError):
                    continue
            else:
                self.args[key] = str(raw).strip()
        if opts is not None and "keep_history" in opts:
            self.args["keep_history"] = bool(opts["keep_history"])

    def _command(self) -> list[str]:
        cmd = [sys.executable, "-m", "troll_poly_bot"]
        for key, (flag, _) in self.FLAGS.items():
            value = self.args.get(key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue           # an empty --assets means "every listed asset"
            cmd += [flag, f"{value:g}" if isinstance(value, float) else str(value)]
        if self.args.get("keep_history"):
            cmd.append("--keep-history")
        return cmd

    def start(self, opts: dict[str, Any] | None = None) -> dict:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "reason": "already running"}
        if self._external_alive():
            return {"ok": False,
                    "reason": "a bot is already running that this server did not "
                              "start; stop it in its own terminal first"}
        self._absorb(opts)
        cmd = self._command()
        try:
            log_path = Path("data/live_bot.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = log_path.open("a", encoding="utf-8")
            with self._lock:
                self.proc = subprocess.Popen(
                    cmd, stdout=fh, stderr=subprocess.STDOUT,
                    cwd=str(Path.cwd()),
                )
                self.started_at = time.time()
                self.last_error = None
            log.info("started live bot pid=%s %s", self.proc.pid, " ".join(cmd[2:]))
            return {"ok": True, "pid": self.proc.pid}
        except Exception as exc:                        # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("could not start bot")
            return {"ok": False, "reason": self.last_error}

    def stop(self, timeout: float = 8.0) -> dict:
        with self._lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            if self._external_alive():
                return {"ok": False,
                        "reason": "the running bot was not started here, so it "
                                  "cannot be stopped from this page"}
            return {"ok": False, "reason": "not running"}
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()                                 # it ignored terminate
            proc.wait(timeout=timeout)
        with self._lock:
            self.proc = None
            self.started_at = None
        # the state file is now stale; drop it so the UI does not show a ghost
        try:
            LIVE_STATE.unlink()
        except OSError:
            pass
        log.info("stopped live bot")
        return {"ok": True}

    def restart(self, opts: dict[str, Any] | None = None) -> dict:
        self.stop()
        time.sleep(0.6)
        return self.start(opts)


BOT = BotController()


# ───────────────────────────── pm2 bots: Start / Stop / Restart ─────────────────────────────
# Every bot but "main" is a pm2 app (ecosystem.*.config.js) that writes data/*.<tag>.*; the page's Start / Stop /
# Restart drive it through pm2 (the user, 2026-10-06 00:57: "enable Start, Stop, Restart buttons"). The bot id is the
# data tag; it is resolved to an app NAME from pm2's own list, or from the ecosystem files for an app pm2 has
# forgotten, and only that name reaches pm2 (argv, no shell). Never `pm2 save` / `pm2 resurrect` (a stale dump).
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
_PM2_TIMEOUT_S = 25.0
_ECO_CACHE: dict[str, Any] = {"t": 0.0, "apps": []}


def _pm2(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["pm2", *args], capture_output=True, text=True, timeout=_PM2_TIMEOUT_S, cwd=str(REPO_ROOT))
    except FileNotFoundError:
        return 127, "pm2 is not installed on this machine"
    except subprocess.TimeoutExpired:
        return 124, "pm2 did not answer in time"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _pm2_apps() -> list[dict]:
    """pm2's own list: name, status and the --data-tag of each app."""
    code, out = _pm2("jlist")
    if code != 0:
        return []
    try:
        rows = json.loads(out[out.index("["):])
    except (ValueError, json.JSONDecodeError):
        return []
    apps = []
    for a in rows:
        env = a.get("pm2_env") or {}
        args = env.get("args") or []
        args = " ".join(args) if isinstance(args, list) else str(args)
        m = re.search(r"--data-tag\s+(\S+)", args)
        apps.append({"name": str(a.get("name") or ""), "status": str(env.get("status") or ""), "tag": m.group(1) if m else None,
                     "cwd": str(env.get("pm_cwd") or "")})
    return apps


def _ecosystem_apps() -> list[dict]:
    """(file, name, tag) of every app in the repo's ecosystem.*.config.js, read through node; cached a minute."""
    if time.time() - _ECO_CACHE["t"] < 60.0:
        return _ECO_CACHE["apps"]
    script = ("const fs=require('fs');const out=[];for(const f of fs.readdirSync('.').filter(x=>/^ecosystem\..*config\.js$/.test(x))){"
              "try{const m=require('./'+f);for(const a of (m.apps||[])){const t=(String(a.args||'').match(/--data-tag (\S+)/)||[])[1]||null;"
              "out.push({file:f,name:a.name,tag:t})}}catch(e){}}console.log(JSON.stringify(out))")
    apps: list[dict] = []
    try:
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=_PM2_TIMEOUT_S, cwd=str(REPO_ROOT))
        if r.returncode == 0:
            apps = [a for a in json.loads(r.stdout or "[]") if a.get("name")]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        apps = []
    _ECO_CACHE.update(t=time.time(), apps=apps)
    return apps


def _resolve_pm2_bot(bot: str) -> tuple[dict | None, dict | None, str]:
    """The pm2 app and the ecosystem app for a bot id (its data tag), or why neither is known."""
    if not _TAG_RE.match(bot or ""):
        return None, None, f"unknown bot {bot!r}"
    live = [a for a in _pm2_apps() if a.get("tag") == bot and a["cwd"].rstrip("/") == str(REPO_ROOT).rstrip("/")]
    live = live or [a for a in _pm2_apps() if a.get("tag") == bot]
    eco = [a for a in _ecosystem_apps() if a.get("tag") == bot]
    if len(live) > 1:
        names = ", ".join(a["name"] for a in live)
        online = [a for a in live if a["status"] == "online"]
        live = online[:1] if len(online) == 1 else live[:1]
        log.warning("bot %s is several pm2 apps (%s); using %s", bot, names, live[0]["name"])
    if not live and not eco:
        return None, None, f"\"{bot}\" is not a pm2 app of this repository (no ecosystem file declares --data-tag {bot})"
    return (live[0] if live else None), (eco[0] if eco else None), ""


def pm2_control(action: str, bot: str) -> dict:
    """Start / stop / restart a pm2 bot by its data tag. ``pm2 start <ecosystem> --only <name>`` brings an app pm2 has
    forgotten (deleted) back with its current config; a stopped app pm2 still lists is resumed by name."""
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "reason": "unknown action"}
    app, eco, why = _resolve_pm2_bot(bot)
    if why:
        return {"ok": False, "reason": why}
    name = (app or eco)["name"]
    running = bool(app and app["status"] == "online")
    if action == "start" and running:
        return {"ok": True, "reason": f"{name} is already running", "running": True, "app": name}
    if action in ("stop", "restart") and app is None:
        return {"ok": False, "reason": f"{name} is not in pm2's list", "running": False, "app": name}
    if action == "stop":
        argv = ["stop", name]
    elif action == "restart":
        argv = ["restart", name]
    elif app is not None:
        argv = ["start", name]
    else:
        argv = ["start", eco["file"], "--only", name]
    code, out = _pm2(*argv)
    ok = code == 0
    after = next((a for a in _pm2_apps() if a["name"] == name), None)
    now_running = bool(after and after["status"] == "online")
    log.warning("pm2 %s for bot %s (%s): %s", " ".join(argv), bot, "ok" if ok else f"exit {code}", "online" if now_running else "stopped")
    tail = "\n".join(line for line in out.strip().splitlines()[-4:])
    return {"ok": ok, "reason": "" if ok else f"pm2 {' '.join(argv)} failed (exit {code}): {tail[-300:]}",
            "running": now_running, "app": name, "pm2": tail[-600:]}


class _AccountCache:
    """A real account, read on a timer rather than on every request.

    The Data API is public and free, which is exactly why this must not hammer
    it: the panel refreshes on a 15 s timer and several open tabs would
    otherwise multiply that. Reads are served from here; one thread at a time
    goes out to the network, and a failure keeps serving the last good
    snapshot with its age attached so the page can say the number is stale
    rather than silently showing an old one as current.
    """

    TTL_S = 10.0

    def __init__(self) -> None:
        self.wallet: str | None = os.environ.get("TPB_POLYMARKET_WALLET") or None
        self._snap: dict | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict:
        if not self.wallet:
            return {"configured": False,
                    "hint": "set TPB_POLYMARKET_WALLET to your Polymarket proxy "
                            "wallet address, or pass --wallet"}
        with self._lock:
            fresh = self._snap is not None and (time.time() - self._at) < self.TTL_S
            if not fresh:
                try:
                    self._snap = account.snapshot(self.wallet)
                    self._at = time.time()
                except account.AccountError as exc:
                    if self._snap is None:
                        return {"configured": True, "wallet": self.wallet,
                                "error": str(exc)}
                    self._snap = dict(self._snap, error=str(exc))
            snap = dict(self._snap or {})
        snap["configured"] = True
        snap["age_s"] = round(time.time() - self._at, 1)
        return snap


ACCOUNT = _AccountCache()

#: How often the push loop stats the state file. The bot writes it at 10 Hz, so
#: this only decides how much of a 100 ms tick is spent waiting -- not the rate.
WS_WATCH_S = 0.02
#: Idle keepalive. Proxies drop a silent socket long before this matters, and a
#: dead peer is otherwise only noticed when the bot next writes.
WS_PING_S = 20.0
#: At most one state push per this many seconds per page (2026-10-05: the bot writes 10x a second and every write
#: went out whole, ~0.3 MB/s per tab, which queued every click behind it on a remote link). The latest state is
#: always the one sent; a market switch from the page is pushed at once.
WS_MIN_PUSH_S = 0.25
#: Responses at least this large are gzip-compressed when the browser accepts it.
GZIP_MIN_BYTES = 1024

#: path -> (etag, body, gzip body) of the static files served
_STATIC_CACHE: dict[str, tuple[str, bytes, bytes]] = {}

#: The admin identity, the live sessions and the login throttle.
#: ``CREDENTIALS`` stays None until :func:`serve` loads it, so importing this
#: module (as the tests do) never needs an environment.
CREDENTIALS: auth.Credentials | None = None
SESSIONS = auth.Sessions()
LOGIN_LIMITER = auth.RateLimiter()
COOKIE_SECURE = False

#: Reachable without a session. Everything else needs one -- including the
#: static bundle, which is the console itself.
PUBLIC_PATHS = frozenset({
    "/login", "/static/favicon.svg", "/static/favicon.png",
})


class _Subscription:
    """What one connected page is watching. Shared across its two threads."""

    __slots__ = ("chart", "since", "closed", "lock", "dirty")

    def __init__(self) -> None:
        self.chart: str | None = None
        self.since: int | None = None
        self.closed = False
        self.lock = threading.Lock()
        self.dirty = False                 # the page changed its market: push now, not at the next write

    def select(self, chart: str | None) -> None:
        with self.lock:
            self.chart = chart
            # a different market shares no points with the old one, so the next
            # push must be a full history rather than a delta
            self.since = None
            self.dirty = True

    def snapshot(self) -> tuple[str | None, int | None]:
        with self.lock:
            return self.chart, self.since

    def advance(self, since: int | None) -> None:
        with self.lock:
            if since is not None:
                self.since = since


class Handler(BaseHTTPRequestHandler):
    server_version = "troll-poly-bot"

    def log_message(self, fmt: str, *args) -> None:       # quieter console
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------ helpers

    def _gzip_ok(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _send_body(self, body: bytes, ctype: str, status: int = 200, cache: str = "no-store",
                   etag: str | None = None, gz: bytes | None = None) -> None:
        """One response, gzip-compressed when the browser accepts it and it is worth it (JSON shrinks 4-7x)."""
        if len(body) >= GZIP_MIN_BYTES and self._gzip_ok():
            body = gz if gz is not None else gzip.compress(body, compresslevel=5)
            enc = True
        else:
            enc = False
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Vary", "Accept-Encoding")
        if enc:
            self.send_header("Content-Encoding", "gzip")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send_body(json.dumps(payload).encode(), "application/json", status)

    def _raw(self, body: bytes, ctype: str, cache: str = "no-store") -> None:
        if len(body) >= GZIP_MIN_BYTES and self._gzip_ok() and ctype.startswith(("application/json", "text/")):
            self._send_body(body, ctype, cache=cache)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _chart_png(self, name: str) -> None:
        """One archived window's rendered chart.

        The slug is matched against the market-slug pattern rather than
        sanitised, so no crafted name can reach outside the chart directory.
        These files never change once written, so they may be cached.
        """
        slug = name[:-4] if name.endswith(".png") else name
        target = ARCHIVE.png(slug)
        if target is None:
            self._json({"error": "not found"}, 404)
            return
        self._raw(target.read_bytes(), "image/png", cache="max-age=86400")

    def _static(self, rel: str) -> None:
        # resolve() + relative_to keeps a crafted path from escaping the dir
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json({"error": "forbidden"}, 403)
            return
        if not target.is_file():
            self._json({"error": "not found", "path": rel}, 404)
            return
        # the page's code and styles: revalidated on every load (no-cache) but answered 304 when unchanged, and
        # gzip-compressed once per version (app.js 196 KB -> 59 KB)
        st = target.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if etag in (self.headers.get("If-None-Match") or ""):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        hit = _STATIC_CACHE.get(str(target))
        if hit is None or hit[0] != etag:
            body = target.read_bytes()
            hit = (etag, body, gzip.compress(body, compresslevel=6))
            _STATIC_CACHE[str(target)] = hit
        self._send_body(hit[1], ctype, cache="no-cache", etag=etag, gz=hit[2])

    def _save_chart(self, slug: str) -> None:
        """Store a PNG the dashboard rendered for a closed window.

        The slug becomes a filename, so it is matched against a strict pattern
        rather than sanitised — anything that is not exactly a market slug is
        refused outright.
        """
        slug = urllib.parse.unquote(slug).strip()
        if not SLUG_RE.match(slug):
            self._json({"error": "bad slug"}, 400)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_PNG_BYTES:
            self._json({"error": "bad length"}, 400)
            return
        body = self.rfile.read(length)
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):        # must really be a PNG
            self._json({"error": "not a png"}, 400)
            return
        try:
            CHART_DIR.mkdir(parents=True, exist_ok=True)
            (CHART_DIR / f"{slug}.png").write_bytes(body)
        except OSError as exc:
            self._json({"error": str(exc)}, 500)
            return
        log.info("saved chart %s.png (%.0f KB)", slug, len(body) / 1024)
        self._json({"ok": True, "path": f"data/charts/{slug}.png",
                    "bytes": len(body)})

    # ------------------------------------------------------------- auth

    def _session(self) -> dict | None:
        """The caller's session, or None. Never raises on a junk cookie."""
        token = auth.parse_cookies(self.headers.get("Cookie")).get(auth.COOKIE)
        return SESSIONS.get(token)

    def _client_addr(self) -> str:
        """Who is knocking, for the login throttle.

        The socket peer, deliberately -- NOT X-Forwarded-For. A header any
        client can set would let one attacker present as a fresh address per
        request and walk straight through the limiter. Behind a reverse proxy
        every login therefore shares the proxy's bucket, which throttles a
        distributed attempt harder rather than softer.
        """
        return self.client_address[0] if self.client_address else "?"

    def _redirect(self, location: str, *, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _login_page(self, error: str = "", *, email: str = "",
                    next_url: str = "/", status: int = 200) -> None:
        body = login.render(error, email=email, next_url=next_url)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _safe_next(self, raw: str | None) -> str:
        """Where to land after signing in.

        Only a path on this server: an open redirect here would turn the login
        page into a credible way to bounce someone off to another host. A
        value starting with ``//`` is a protocol-relative URL to elsewhere, so
        it is refused along with anything carrying a scheme.
        """
        if not raw or not raw.startswith("/") or raw.startswith("//"):
            return "/"
        return raw

    def _require_session(self, path: str) -> bool:
        """True when the request may proceed.

        When it may not, the response has already been sent: a browser asking
        for a page is redirected to the login form, anything else gets a 401
        it can act on. ``fetch`` following a 303 to an HTML page would other-
        wise hand the dashboard's JSON parser a login document.
        """
        if path in PUBLIC_PATHS:
            return True
        if self._session() is not None:
            return True
        wants_html = (
            self.command == "GET"
            and not path.startswith("/api/")
            and "text/html" in (self.headers.get("Accept") or "")
        )
        if wants_html:
            nxt = urllib.parse.quote(self.path, safe="/?=&#")
            self._redirect(f"/login?next={nxt}")
        else:
            self._json({"error": "auth required", "login": "/login"}, 401)
        return False

    def _do_login(self) -> None:
        """Check credentials from the login form and start a session."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8192:                                   # a form, not an upload
            self._login_page("Request too large.", status=400)
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        one = lambda k: (form.get(k) or [""])[0]            # noqa: E731
        email, password = one("email"), one("password")
        next_url = self._safe_next(one("next"))
        addr = self._client_addr()

        wait = LOGIN_LIMITER.blocked_for(addr)
        if wait > 0:
            log.warning("login throttled for %s (%.0fs left)", addr, wait)
            self._login_page(
                f"Too many failed attempts. Try again in {int(wait) + 1} seconds.",
                email=email, next_url=next_url, status=429)
            return

        if CREDENTIALS is None or not CREDENTIALS.check(email, password):
            LOGIN_LIMITER.record_failure(addr)
            time.sleep(auth.FAILURE_DELAY_S)
            log.warning("failed login for %r from %s", email, addr)
            # one message for both halves: which of the two was wrong is not
            # something an unauthenticated caller gets to learn
            self._login_page("Wrong email or password.",
                             email=email, next_url=next_url, status=401)
            return

        LOGIN_LIMITER.record_success(addr)
        token = SESSIONS.create(CREDENTIALS.email)
        log.info("signed in: %s from %s", CREDENTIALS.email, addr)
        self._redirect(next_url,
                       cookie=auth.cookie_header(token, secure=COOKIE_SECURE))

    def _do_logout(self) -> None:
        token = auth.parse_cookies(self.headers.get("Cookie")).get(auth.COOKIE)
        SESSIONS.destroy(token)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", auth.clear_cookie_header(secure=COOKIE_SECURE))
        body = json.dumps({"ok": True}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ---------------------------------------------------------------- GET

    # ----------------------------------------------------------- websocket

    def _ws_send(self, sock_lock: threading.Lock, data: bytes) -> None:
        with sock_lock:
            self.wfile.write(data)
            self.wfile.flush()

    def _ws_reader(self, sub: _Subscription, sock_lock: threading.Lock) -> None:
        """Client -> server: only ever a subscription change, a ping or a close.

        Its own thread because the push loop must not block on a client that
        never speaks, and a blocking read is the only way to notice one that
        hangs up.
        """
        try:
            while not sub.closed:
                opcode, payload, packed = ws.read_frame_ex(self.rfile.read)
                if packed and opcode in (ws.OP_TEXT, ws.OP_BINARY):
                    payload = ws.inflate_message(payload)
                if opcode == ws.OP_CLOSE:
                    break
                if opcode == ws.OP_PING:
                    self._ws_send(sock_lock, ws.encode_frame(payload, ws.OP_PONG))
                    continue
                if opcode != ws.OP_TEXT:
                    continue
                try:
                    msg = json.loads(payload.decode() or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(msg, dict) and "chart" in msg:
                    chart = msg["chart"]
                    sub.select(chart if isinstance(chart, str) and chart else None)
        except (ws.WSError, OSError, ValueError):
            pass
        finally:
            sub.closed = True

    def _ws_live(self) -> None:
        """Push the live state as it is written, instead of being polled for it.

        Same payload as GET /api/live, same `chart`/`since` trimming -- this
        changes the transport, not the contract, so the page can fall back to
        polling without the server caring.
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            self._json({"error": "expected a websocket upgrade"}, 400)
            return
        self.close_connection = True          # this socket leaves HTTP behind
        sock_lock = threading.Lock()
        sub = _Subscription()
        # the page names its market in the upgrade URL so the FIRST push is
        # already the right one, instead of a full state we then throw away
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sub.select((qs.get("chart") or [None])[0] or None)
        bot = (qs.get("bot") or [MAIN_BOT])[0] or MAIN_BOT
        paths = bot_paths(bot)
        if paths is None:
            self._json({"error": "unknown bot"}, 404)
            return
        state_path = paths[0]
        # permessage-deflate when the browser offers it; "patch=1": after the first frame, send only the top-level
        # parts of the state that changed (the page merges them) -- most of a 36 KB state is the same 10x a second
        deflate = ws.negotiate_deflate(self.headers.get("Sec-WebSocket-Extensions"))
        patch = (qs.get("patch") or ["0"])[0] == "1"
        sent: dict[str, str] = {}
        try:
            self.wfile.write(ws.handshake_response(key, deflate))
            self.wfile.flush()
        except OSError:
            return

        reader = threading.Thread(target=self._ws_reader, args=(sub, sock_lock),
                                  daemon=True)
        reader.start()

        last_mtime = -1.0
        last_ping = time.time()
        last_push = 0.0
        try:
            while not sub.closed:
                now = time.time()
                try:
                    mtime = state_path.stat().st_mtime
                except OSError:
                    mtime = -1.0
                dirty = sub.dirty
                if (mtime != last_mtime or dirty) and mtime > 0:
                    if not dirty and now - last_push < WS_MIN_PUSH_S:
                        time.sleep(WS_WATCH_S)        # throttled: the newest write goes out when the slot opens
                        continue
                    sub.dirty = False
                    chart, since = sub.snapshot()
                    payload = self._live_payload(chart, since, state_path=state_path, bot=bot)
                    if payload is None:
                        # caught the writer mid-file; leave last_mtime alone so
                        # this same write is retried rather than skipped
                        time.sleep(WS_WATCH_S)
                        continue
                    last_mtime = mtime
                    last_push = now
                    pts = (payload.get("price_history") or {}).get(chart or "")
                    if pts:
                        sub.advance(pts[-1].get("t"))
                    if patch:
                        enc = {k: json.dumps(v, separators=(",", ":")) for k, v in payload.items()}
                        if sent:
                            msg = {k: payload[k] for k, v in enc.items() if sent.get(k) != v}
                            gone = [k for k in sent if k not in enc]
                            msg.update({"_patch": 1, "bot": bot})
                            if gone:
                                msg["_del"] = gone
                        else:
                            msg = payload
                        sent = enc
                    else:
                        msg = payload
                    data = json.dumps(msg, separators=(",", ":")).encode()
                    if deflate and len(data) > 128:
                        frame = ws.encode_frame(ws.deflate_message(data), rsv1=True)
                    else:
                        frame = ws.encode_frame(data)
                    self._ws_send(sock_lock, frame)
                    last_ping = now
                elif now - last_ping >= WS_PING_S:
                    self._ws_send(sock_lock, ws.encode_frame(b"", ws.OP_PING))
                    last_ping = now
                else:
                    time.sleep(WS_WATCH_S)
        except (OSError, ValueError):
            pass
        finally:
            sub.closed = True
            with contextlib.suppress(OSError, ValueError):
                self._ws_send(sock_lock, ws.close_frame())

    @staticmethod
    def _live_payload(chart: str | None, since: int | None, state_path: Path | None = None,
                      bot: str = MAIN_BOT) -> dict | None:
        """The live state trimmed for one viewer, or None if it is unreadable.

        None specifically means "could not read it *this time*" -- a torn read
        while the bot rewrites the file, which at 10 Hz happens. Callers must
        not turn that into `running: false`: the page would blank a working
        dashboard for one frame every time it caught the writer mid-stride.
        """
        state_path = state_path or LIVE_STATE
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            mtime = state_path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["stale_s"] = round(time.time() - mtime, 1)
        # which bot this is: a frame from the socket of a bot the page has just
        # switched away from must not be drawn over the new one
        payload["bot"] = bot
        if "price_history" not in payload:
            # the bot keeps chart points out of the state (history_store.py):
            # read only the market this viewer draws
            pts = read_points(state_path, chart) if chart else None
            payload["price_history"] = {chart: pts} if pts is not None else {}
        return _filter_live(payload, chart, since)

    # ---------------------------------------------------------------- GET

    def do_GET(self) -> None:                              # noqa: N802
        path = self.path.split("?")[0]
        if path == "/login":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if self._session() is not None:                # already signed in
                self._redirect(self._safe_next((qs.get("next") or ["/"])[0]))
                return
            self._login_page(next_url=self._safe_next((qs.get("next") or ["/"])[0]))
            return
        if not self._require_session(path):
            return
        if path == "/api/session":
            sess = self._session() or {}
            self._json({"email": sess.get("email")})
        elif path == "/ws":
            self._ws_live()
        elif path in ("/", "/index.html"):
            self._static("index.html")
        elif path == "/api/schema":
            self._json(build_schema())
        elif path == "/api/bot":
            self._json(BOT.status())
        elif path == "/api/bots":
            # every bot with a state file on disk; the page's bot picker
            self._json({"bots": BOTS.list(), "main": MAIN_BOT})
        elif path == "/api/account":
            # read-only: this endpoint cannot place, cancel or modify an order
            self._json(ACCOUNT.get())
        elif path == "/api/history":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            page = _qs_int(qs, "page", 1)
            page_size = _qs_int(qs, "page_size", DEFAULT_HISTORY_PAGE_SIZE)
            paths = bot_paths((qs.get("bot") or [MAIN_BOT])[0])
            if paths is None:
                self._json({"error": "unknown bot"}, 404)
                return
            self._json(_history(page, page_size,
                                view=(qs.get("view") or ["trips"])[0],
                                mode=(qs.get("mode") or ["all"])[0], trade_log=paths[1]))
        elif path == "/api/orders":
            # the newest orders across every market (the market page's order list)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            out = recent_orders((qs.get("bot") or [MAIN_BOT])[0] or MAIN_BOT,
                                _qs_int(qs, "limit", DEFAULT_ORDERS))
            if out is None:
                self._json({"error": "unknown bot"}, 404)
            else:
                self._json(out)
        elif path == "/api/fills":
            # one market's fills, or the newest markets' (the market page's fill ledger)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            out = market_fills((qs.get("bot") or [MAIN_BOT])[0] or MAIN_BOT,
                               slug=(qs.get("slug") or [""])[0] or None,
                               markets=_qs_int(qs, "markets", DEFAULT_FILL_MARKETS))
            if out is None:
                self._json({"error": "unknown bot"}, 404)
            else:
                self._json(out)
        elif path == "/api/trade":
            # one window of one bot: price path, fills and orders (trade dialog)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            out = trade_detail((qs.get("slug") or [""])[0],
                               bot=(qs.get("bot") or [MAIN_BOT])[0] or MAIN_BOT,
                               since=_qs_int(qs, "since", 0) or None,
                               have=(qs.get("have") or [None])[0])
            if out is None:
                self._json({"error": "unknown trade"}, 404)
            else:
                self._json(out)
        elif path == "/api/live":
            # Written by the live paper trader (python -m troll_poly_bot).
            # Served read-only so the dashboard can show it without the two
            # processes needing to know about each other.
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            chart = (qs.get("chart") or [None])[0]
            try:
                since = int((qs.get("since") or [None])[0])
            except (TypeError, ValueError):
                since = None
            bot = (qs.get("bot") or [MAIN_BOT])[0] or MAIN_BOT
            paths = bot_paths(bot)
            if paths is None:
                self._json({"error": "unknown bot"}, 404)
                return
            # same builder the websocket uses, so the two transports cannot
            # drift apart in what they consider a payload
            self._json(self._live_payload(chart, since, state_path=paths[0], bot=bot)
                       or {"running": False, "bot": bot})
        elif path == "/api/earnings":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            period = (qs.get("period") or ["day"])[0]
            scope = (qs.get("scope") or ["all"])[0]
            span = _qs_int(qs, "span", 0) or None
            self._json(with_balances(EARNINGS.get(period, scope, span)))
        elif path == "/api/controls":
            self._json(_controls_payload())
        elif path == "/api/saved":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            one = lambda k: (qs.get(k) or [""])[0]            # noqa: E731
            rows = archive.filter_rows(ARCHIVE.all_rows(), asset=one("asset"),
                                       outcome=one("outcome"), traded=one("traded"),
                                       search=one("q"))
            out = archive.page(rows, _qs_int(qs, "page", 1),
                               _qs_int(qs, "page_size", archive.DEFAULT_PAGE_SIZE),
                               one("sort") or "recent")
            out["stats"] = archive.stats(rows)
            out["all_assets"] = archive.stats(ARCHIVE.all_rows())["assets"]
            self._json(out)
        elif path.startswith("/api/saved/"):
            # the whole recorded window, sample path and all, for one market
            src = ARCHIVE.one(urllib.parse.unquote(path[len("/api/saved/"):]))
            if src is None:
                self._json({"error": "unknown window"}, 404)
            else:
                self._raw(src.read_bytes(), "application/json")
        elif path.startswith("/charts/"):
            self._chart_png(urllib.parse.unquote(path[len("/charts/"):]))
        elif path.startswith("/api/job/"):
            job = MANAGER.get(path.rsplit("/", 1)[-1])
            self._json(job.snapshot() if job else {"error": "unknown job"},
                       200 if job else 404)
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        else:
            self._json({"error": "not found"}, 404)

    # --------------------------------------------------------------- POST

    def do_POST(self) -> None:                             # noqa: N802
        path = self.path.split("?")[0]
        if path == "/login":
            self._do_login()
            return
        if not self._require_session(path):
            return
        if path == "/api/logout":
            self._do_logout()
        elif path.startswith("/api/chart/"):
            self._save_chart(path[len("/api/chart/"):])
        elif path.startswith("/api/bot/"):
            action = path.rsplit("/", 1)[-1]
            body = self._body()
            bot = str(body.get("bot") or MAIN_BOT)
            if bot != MAIN_BOT:
                # a pm2 bot: started, stopped or restarted through pm2 by its data tag
                out = pm2_control(action, bot)
                self._json(out, 200 if out.get("ok") else 400)
            elif action == "start":
                self._json(BOT.start(body))
            elif action == "stop":
                self._json(BOT.stop())
            elif action == "restart":
                self._json(BOT.restart(body))
            else:
                self._json({"error": "unknown action"}, 404)
        elif path == "/api/sell":
            body = self._body()
            out = request_sell(str(body.get("bot") or MAIN_BOT), body, getattr(self, "session_user", ""))
            self._json(out, 400 if out.get("error") else 200)
        elif path == "/api/buy":
            body = self._body()
            out = request_buy(str(body.get("bot") or MAIN_BOT), body, getattr(self, "session_user", ""))
            self._json(out, 400 if out.get("error") else 200)
        elif path == "/api/controls":
            body = self._body()
            scope = str(body.get("scope") or "all")
            values = body.get("values") or {}
            if body.get("reset"):
                result = CONTROLS.clear_scope(scope)
            elif body.get("replace"):
                # the page sends the whole section, so clearing a field removes it
                result = CONTROLS.replace_scope(scope, values)
            else:
                result = CONTROLS.write_scope(scope, values)
            self._json({**result, **_controls_payload()},
                       200 if result.get("ok") else 400)
        elif path == "/api/kill":
            body = self._body()
            active = control.set_kill(bool(body.get("active")))
            self._json({"ok": True, "kill_active": active})
        elif path == "/api/run":
            body = self._body()
            job = MANAGER.submit(
                body.get("params") or {},
                body.get("profiles") or ["home_broadband"],
            )
            self._json({"id": job.id})
        elif path.startswith("/api/cancel/"):
            ok = MANAGER.cancel(path.rsplit("/", 1)[-1])
            self._json({"cancelled": ok})
        else:
            self._json({"error": "not found"}, 404)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Start the console.

    Refuses to start without an admin identity. That is deliberate: this
    server's POST routes start and stop the trader, rewrite the control file
    and create the kill file, so "no credentials configured" must fail closed
    rather than quietly serve an open console -- the failure mode that would
    be least visible is also the worst one.
    """
    global CREDENTIALS, COOKIE_SECURE
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )
    CREDENTIALS = auth.Credentials.from_env()               # raises AuthError
    COOKIE_SECURE = auth.cookie_secure_from_env()
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    log.info("dashboard on %s  (paper mode only -- no order-signing code exists)", url)
    log.info("sign in as %s", CREDENTIALS.email)
    if host not in ("127.0.0.1", "localhost", "::1") and not COOKIE_SECURE:
        log.warning(
            "reachable off this machine over plain HTTP: the password and the "
            "session cookie cross the network in clear. Put TLS in front and "
            "set TPB_ADMIN_COOKIE_SECURE=1, or bind 127.0.0.1 and use an SSH tunnel."
        )
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        httpd.server_close()
