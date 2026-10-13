"""Real order execution on Polymarket, behind three separate locks.

This is the one module in the repo that can spend money. It is deliberately
small, boring and defensive.

The three locks
---------------
1. ``--live`` alone is a **dry run**: it authenticates against your account,
   reads the real balance and allowance, reconciles every 30 s, and signs
   every order the strategy wants to send -- and then does NOT post it. The
   order shows up in the ledger as ``DRY_RUN``. Run this first.
2. ``--live --armed`` posts real orders, and only if the environment carries
   ``TPB_LIVE_ACK=I_UNDERSTAND_REAL_MONEY``. The dashboard's Start button
   cannot arm the bot; only the command line can.
3. While armed, every order is checked against caps that are independent of
   the strategy's own risk config (``LiveCaps``): notional per order, open
   notional, orders per hour, a daily-loss halt, and a **kill file** -- touch
   ``data/KILL`` and nothing further is sent, no restart needed.

Entries are fill-or-kill by default, exactly like the paper engine, so a
stopped bot leaves nothing behind. With ``engine.maker_entry`` on, an entry
is instead a post-only GTC limit that RESTS at the model's price: the worker
polls it, books maker fills at our price (fee-free, the venue charges takers
only), cancels it after ``maker_ttl_s``, and cancels everything still open on
the way out; a venue-side expiration bounds a crash. There is no chase.

What the venue is trusted with
------------------------------
The account's cash balance is read from the venue and the bot's ``balance``
is the starting bankroll plus the venue's change since start. Fills are what
the venue's order response says was matched, not what we asked for. Fees
are an estimate from the market's schedule; the venue's real deduction
shows up in the next reconciliation.

Redemption
----------
A winning token pays 1 USDC only after it is **redeemed**. Smart-contract
accounts (Deposit Wallets and the older proxies) redeem through Polymarket's
relayer, which needs a **Relayer API key** made in the app (Settings -> API
Keys -> Relayer API Keys). Give it to the bot as ``TPB_POLY_RELAYER_API_KEY``
plus ``TPB_POLY_RELAYER_API_KEY_ADDRESS`` and every resolved winner is
redeemed from the worker thread, retried while the market is still settling
on-chain. Without it the bot does not redeem: turn on Auto-Redeem in the app
instead. Either way, until the cash lands the amount is carried here as
``pending_redemption`` and counted in equity.

Venue client
------------
Polymarket moved to CLOB V2 on 28 April 2026. The old ``py-clob-client``
signs V1 orders, which the venue now rejects, and it cannot sign for a
Deposit Wallet at all (signature type 3, ERC-1271). This module therefore
uses Polymarket's unified SDK, ``polymarket-client`` (``SecureClient``),
which detects the wallet type itself and binds the API key to the right
address. Orders are signed as fill-or-kill *market* orders with a price
bound, which is the V2 shape of the paper engine's FOK limit order.

Credentials
-----------
Read from the environment only (``.env`` is loaded by the CLI); never
logged, never written to the state file. ``TPB_POLY_PRIVATE_KEY`` is the
signer's key (email login: profile -> settings -> export private key).
``TPB_POLY_WALLET`` (older name ``TPB_POLY_FUNDER``) is the account wallet
shown under your Polymarket profile; it is a different address from the
signer, and that is expected. API credentials are derived from the key
unless ``TPB_POLY_API_KEY/SECRET/PASSPHRASE`` are given.
``TPB_POLY_SIGNATURE_TYPE`` is no longer needed; if set it is only compared
with what the venue reports.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import queue
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..types import Fill, Order, OrderState, Position, Side, TimeInForce
from .paper import EPS, FeeModel, OrderResult, RejectReason

log = logging.getLogger("live.exchange")

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
USDC_DECIMALS = 1_000_000.0
ACK_PHRASE = "I_UNDERSTAND_REAL_MONEY"
SIGNATURE_TYPE_OF = {"EOA": 0, "POLY_PROXY": 1, "GNOSIS_SAFE": 2, "DEPOSIT_WALLET": 3}
UNFILLED_CODES = {"unmatched", "fok_not_filled", "fak_not_filled"}
REDEEM_RETRY_S = 90.0
REDEEM_MAX_ATTEMPTS = 12
#: A resting order's venue-side expiry, the crash backstop: the SDK insists
#: on at least three minutes. The bot cancels at its own maker_ttl_s.
RESTING_BACKSTOP_S = 240
RESTING_POLL_S = 1.0
#: the strategy's cancels: looked at this often while one waits for its order to rest; given up after this long
CANCEL_RETRY_S = 0.1
CANCEL_GIVE_UP_S = 30.0
#: ping the order connection when it has been idle this long; the SDK's
#: HTTP client closes it at 30 s (see PolymarketExchange._keep_warm)
KEEP_WARM_S = 15.0
RESTING_MAX_POLL_ERRORS = 5
#: An ENTRY that has waited longer than this between the strategy's decision
#: and the POST is not sent: the worker thread may have been busy with a
#: 1-2 s reconciliation, and signing fetches market metadata the first time
#: a token is seen. The decision was about a book that no longer exists.
MAX_ENTRY_DECISION_AGE_MS = float(os.getenv("TPB_MAX_ENTRY_DECISION_AGE_MS", "1000") or 1000)


class LiveConfigError(Exception):
    """Something about the account setup is wrong. Always fatal at startup."""


@dataclass(slots=True)
class LiveCaps:
    """Hard limits that sit outside the strategy. Dollars are real here."""
    max_order_usdc: float = 5.0
    max_open_usdc: float = 25.0
    max_daily_loss_usdc: float = 10.0
    max_orders_per_hour: int = 60
    kill_file: str = "data/KILL"


@dataclass
class LiveCredentials:
    private_key: str
    wallet: str | None = None
    signature_type: int | None = None        # informational; the venue reports the real one
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    relayer_key: str | None = None
    relayer_address: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "LiveCredentials":
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            return (env.get(name) or "").strip() or None

        key = get("TPB_POLY_PRIVATE_KEY")
        if not key:
            raise LiveConfigError(
                "TPB_POLY_PRIVATE_KEY is not set. Put it in .env (never in the shell history). "
                "For an email login export it from Polymarket: profile -> settings -> export private key.")
        wallet = get("TPB_POLY_WALLET") or get("TPB_POLY_FUNDER")
        if wallet is not None and not (wallet.startswith("0x") and len(wallet) == 42):
            raise LiveConfigError(
                "TPB_POLY_WALLET must be the 0x-prefixed 40-hex-character address shown under your Polymarket profile")
        sig_txt = get("TPB_POLY_SIGNATURE_TYPE")
        sig: int | None = None
        if sig_txt is not None:
            if sig_txt not in ("0", "1", "2", "3"):
                raise LiveConfigError(
                    "TPB_POLY_SIGNATURE_TYPE, if set, must be 0, 1, 2 or 3. It is optional: the venue "
                    "reports the wallet type itself")
            sig = int(sig_txt)
        relayer_key, relayer_addr = get("TPB_POLY_RELAYER_API_KEY"), get("TPB_POLY_RELAYER_API_KEY_ADDRESS")
        if bool(relayer_key) != bool(relayer_addr):
            raise LiveConfigError(
                "TPB_POLY_RELAYER_API_KEY and TPB_POLY_RELAYER_API_KEY_ADDRESS go together "
                "(Polymarket app: Settings -> API Keys -> Relayer API Keys)")
        return cls(private_key=key, wallet=wallet, signature_type=sig,
                   api_key=get("TPB_POLY_API_KEY"), api_secret=get("TPB_POLY_API_SECRET"),
                   api_passphrase=get("TPB_POLY_API_PASSPHRASE"),
                   relayer_key=relayer_key, relayer_address=relayer_addr)

    @property
    def funder(self) -> str | None:           # the old name, still used by callers
        return self.wallet

    @property
    def can_redeem(self) -> bool:
        return self.relayer_key is not None

    def redacted(self) -> dict:
        return {"wallet": self.wallet, "signature_type": self.signature_type,
                "api_creds": "given" if self.api_key else "derived",
                "relayer_key": "given" if self.relayer_key else "none",
                "private_key": f"...{self.private_key[-4:]}"}

    def build_client(self):
        """An authenticated ``polymarket.SecureClient`` for this account.

        Creating it derives the CLOB API credentials (or validates the given
        ones) and asks the venue which wallet type the account is.
        """
        try:
            from polymarket import ApiKeyCreds, RelayerApiKey, SecureClient
        except ImportError as exc:                       # pragma: no cover
            raise LiveConfigError("polymarket-client is not installed: pip install -e '.[live]'") from exc
        creds = None
        if self.api_key and self.api_secret and self.api_passphrase:
            creds = ApiKeyCreds.model_validate(
                {"apiKey": self.api_key, "secret": self.api_secret, "passphrase": self.api_passphrase})
        relayer = None
        if self.relayer_key and self.relayer_address:
            relayer = RelayerApiKey(key=self.relayer_key, address=self.relayer_address)
        client = SecureClient.create(private_key=self.private_key, wallet=self.wallet,
                                     credentials=creds, api_key=relayer)
        detected = str(getattr(client, "wallet_type", "") or "")
        known = SIGNATURE_TYPE_OF.get(detected)
        if self.signature_type is not None and known is not None and known != self.signature_type:
            log.warning("TPB_POLY_SIGNATURE_TYPE=%d but the venue reports a %s (type %d); the venue wins",
                        self.signature_type, detected, known)
        return client


def _parse_balance(resp: Any) -> tuple[float, float | None]:
    """(collateral balance USDC, allowance USDC or None) from the venue reply.

    Takes the SDK's ``BalanceAllowance`` model (integer base units) or the
    raw dict the REST endpoint returns.
    """
    if hasattr(resp, "balance") and hasattr(resp, "allowances"):
        bal = float(resp.balance or 0) / USDC_DECIMALS
        values = [float(v or 0) for v in dict(resp.allowances or {}).values()]
        return bal, (max(values) / USDC_DECIMALS if values else None)
    if not isinstance(resp, dict):
        raise LiveConfigError(f"unexpected balance reply: {resp!r}")
    bal = float(resp.get("balance") or 0.0) / USDC_DECIMALS
    allow: float | None = None
    raw = resp.get("allowances")
    if isinstance(raw, dict) and raw:
        allow = max(float(v or 0.0) for v in raw.values()) / USDC_DECIMALS
    elif resp.get("allowance") is not None:
        allow = float(resp.get("allowance") or 0.0) / USDC_DECIMALS
    return bal, allow


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _epoch_s(value: Any) -> float | None:
    """Seconds since the epoch from a datetime, an epoch number (s or ms) or an ISO string."""
    if value is None:
        return None
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        try:
            return float(ts())
        except (TypeError, ValueError, OSError):
            return None
    try:
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _exc_is(exc: Exception, *names: str) -> bool:
    """Match SDK exceptions by class name so the tests need no SDK."""
    return any(cls.__name__ in names for cls in type(exc).__mro__)


def _kind(order: Order) -> str:
    """The order type as the venue knows it: an IOC entry is sent as FAK."""
    return "FAK" if order.tif is TimeInForce.IOC else "FOK"


def _is_fok_kill(exc: Exception) -> bool:
    """A fill-or-kill the venue killed, surfaced as an exception.

    polymarket-client raises ``RequestRejectedError`` for ANY non-success
    status, a killed FOK included, rather than returning a response with an
    unfilled code. ``code`` carries the venue's machine code when the body has
    one; otherwise only the message says what happened. A kill is the order's
    normal other outcome -- the same non-event the response path treats it as
    -- so it must never count toward the consecutive-error halt. Five ordinary
    kills in a row halted the live bot on 2026-09-22 because they did.
    """
    if _exc_is(exc, "InsufficientLiquidityError"):
        return True
    if not _exc_is(exc, "RequestRejectedError"):
        return False
    code = str(getattr(exc, "code", "") or "").lower()
    if code in UNFILLED_CODES:
        return True
    msg = str(exc).lower()
    # a fill-and-kill that found nothing to take is the same non-event
    return ("fully filled" in msg or "fok" in msg or "unmatched" in msg or "fak" in msg
            or "no orders found to match" in msg or "no match" in msg)


class PolymarketExchange:
    """Same surface as ``PaperExchange``, real venue behind it."""

    #: how often the venue's cash, positions and trades are re-read (TPB_LIVE_RECONCILE_S; 5 s on the account the
    #: user also trades by hand, so the books follow the account within seconds)
    RECONCILE_S = float(os.environ.get("TPB_LIVE_RECONCILE_S") or 30.0)
    MAX_CONSECUTIVE_ERRORS = 5
    #: the venue moves a fill's cash seconds after the match; until then its
    #: balance and ours differ by exactly that fill, which is timing, not drift
    CASH_SETTLE_GRACE_S = 60.0
    is_live = True

    def __init__(
        self,
        client,
        fees: FeeModel,
        bankroll: float,
        caps: LiveCaps | None = None,
        armed: bool = False,
        clock: Callable[[], float] | None = None,
        wallet: str | None = None,
        can_redeem: bool = False,
        order_lanes: int = 0,
    ) -> None:
        self.client = client
        self.fees = fees
        self.caps = caps or LiveCaps()
        self.armed = armed
        self.clock = clock or (lambda: time.time() * 1000.0)
        self.address = wallet
        self.can_redeem = can_redeem
        self.condition_of: dict[str, str] = {}     # token id -> condition id, for redemption
        self._redeem_pending: dict[str, tuple[int, float]] = {}   # condition -> (attempts, due monotonic s)
        self.redeemed: list[str] = []

        self.bankroll = bankroll
        self.starting_balance = bankroll
        self.balance = bankroll                  # bankroll + venue cash change since start
        self.venue_balance: float | None = None
        self.venue_balance_at_start: float | None = None
        self.allowance: float | None = None
        self.pending_redemption = 0.0            # USDC owed by unredeemed winners
        self.redeemed_ahead = 0.0                # USDC the venue paid for winners we have not settled yet (already in balance)
        self._local_cash_delta = 0.0             # what OUR fills moved the cash by
        self._last_fill_mono = 0.0               # time.monotonic() of the newest fill booked

        self.positions: dict[str, Position] = defaultdict(lambda: Position(""))
        self.fills: list[Fill] = []
        self.results: list[OrderResult] = []
        self.n_submitted = 0
        self.n_rejected: dict[RejectReason, int] = defaultdict(int)
        self.latency = None

        self.halted: str | None = None
        self.consecutive_errors = 0
        #: venue cash movement no fill or redemption explains (cumulative USDC)
        self.balance_drift = 0.0
        #: last reconcile_positions() report
        self.position_report: dict = {"unknown_positions": [], "missing_positions": [],
                                      "stray_orders": [], "legacy_positions": [], "checked": False}
        #: the bot sets this to the tokens of the markets it is trading, so
        #: venue positions elsewhere are reported as legacy, not as a missed fill
        self.watched_tokens: Callable[[], set] | None = None
        #: set by the bot: token -> shares in markets past their close that we have not settled yet. The venue
        #: redeems a resolved market's winners on its own, often before our outcome lookup has the result; that
        #: cash and the vanished position are a redemption ahead of our settlement, not drift or a missed fill
        self.settling_tokens: Callable[[], dict] | None = None
        self.redemptions_ahead = 0
        #: the user's own trading on polymarket.com (2026-10-05): a trade in a market this bot trades, or a venue
        #: position there that we do not hold, is ADOPTED into our books (and handed to the bot) instead of latching
        #: the guard; resting orders found at start are left alone
        self.adopt_external = False
        self.on_external_fill: Callable[[Order, Fill, dict], None] | None = None
        self.on_external_position: Callable[[str, float], float | None] | None = None
        self.external_trades: list[dict] = []
        self._adopted_recent: list[tuple[str, str, float, float]] = []   # (token, side, size, time) booked from positions
        self.external_cash = 0.0                 # cash the account moved by hand outside our markets (adopting: re-based, logged)
        self.venue_positions_last: dict[str, float] = {}
        #: a position difference is adopted only once it has persisted this long (the venue's position list runs a
        #: few seconds behind its fills: our own buy read as "ours > venue", then "venue > ours" -- 2026-10-05 20:44)
        self.ADOPT_CONFIRM_S = 20.0
        self._pos_diff_seen: dict[str, tuple[int, float]] = {}      # token -> (sign of the difference, first seen, monotonic)
        self._token_fill_mono: dict[str, float] = {}                # token -> monotonic time of our last booked fill
        #: resting (GTC) orders posted and not yet done, keyed by venue order id
        self.open_orders: dict[str, dict] = {}
        self._last_poll_mono = 0.0
        self.last_error = ""
        self.last_reconcile_ms = 0.0
        self.realised = 0.0
        self._queue: deque[Order] = deque()
        #: set when an order is queued: the worker wakes at once instead of on
        #: its next 50 ms idle tick (up to 50 ms, 25 on average, per order)
        self._wake = asyncio.Event()
        #: last time the order connection carried a request (keep_warm)
        self._last_clob_mono = time.monotonic()
        self._done: deque[OrderResult] = deque()
        self._sent_times: deque[float] = deque()
        self._rtts: list[float] = []
        #: the bot's measured-latency sink: every real POST round trip goes
        #: there, so paper and the strategy's own latency model learn the
        #: venue's real 230-660 ms instead of a 35 ms Gamma GET
        self.on_rtt: Callable[[float], None] | None = None
        #: set by the bot: runs in THIS worker thread right before a POST and
        #: returns why an entry must not go out any more (or None)
        self.pre_post_check: Callable[[Order], str | None] | None = None
        #: set by the bot: (order, avg price, shares, venue fill ts), called in
        #: THIS worker thread the moment a reply reports a match
        self.on_fill_event: Callable[[Order, float, float, float], None] | None = None
        self.max_entry_decision_age_ms = MAX_ENTRY_DECISION_AGE_MS
        #: tokens whose market metadata the SDK should fetch before we need it
        self._prewarm: deque[str] = deque()
        self._prewarmed: set[str] = set()
        self.prewarm_errors = 0
        #: every venue order id this process placed (foreign-activity check)
        self.own_venue_ids: set[str] = set()
        self.started_s = time.time()
        #: ORDER LANES (2026-09-29, docs/latency_2026-09-29.md). 0 = the original design: one
        #: asyncio worker posts orders one at a time and also runs reconciles, redemptions
        #: and keep-alives, so an order waits for the event loop, for any maintenance call
        #: in progress and for every order queued ahead of it (live: +20 ms median, +300 to
        #: +700 ms when several markets fire in the same shock). N > 0: N dedicated OS
        #: threads take fill-or-kill orders straight from submit() and sign and POST them in
        #: parallel over the SDK's shared HTTP/2 connection; maintenance stays on the worker.
        self.order_lanes = max(0, int(order_lanes or 0))
        self._lane_q: queue.Queue | None = queue.Queue() if self.order_lanes else None
        self._lanes: list[threading.Thread] = []
        #: guards the shared books (positions, cash, reservations, in-flight count)
        self._lock = threading.RLock()
        #: orders between their last look and their POST reply, and POSTs ever started:
        #: a venue read that overlaps either is ahead of our books and must not be judged
        self._inflight = 0
        self._post_epoch = 0
        self.reconcile_deferred = 0
        #: worst-case notional of entries that passed the exposure check and are being
        #: posted now (parallel lanes could otherwise all pass it before any fills)
        self._reservations: dict[str, float] = {}
        #: venue trades on this account since start that no order of ours explains
        self.foreign_trades: list[dict] = []
        self._seen_trade_ids: set[str] = set()
        self.trades_checked = 0
        #: the strategy's own cancels (a pair bid re-placed lower): order id -> when it was asked. The run worker
        #: pulls each at the venue, reads what matched by then and hands back ONE result (any last fills in it)
        self._cancel_wanted: dict[str, float] = {}
        self._last_cancel_mono = 0.0

    #: cancel() ends with the order's own result (live.py waits for it before re-placing a bid)
    cancel_reports_result = True

    # ------------------------------------------------------------- startup

    def authenticate(self) -> dict:
        """Read the account. Refuses to continue if it cannot be traded from."""
        from_venue = self._fetch_balance()
        bal, allow = from_venue
        self.venue_balance = self.venue_balance_at_start = bal
        self.allowance = allow
        if allow is not None and allow < min(bal, 1.0):
            raise LiveConfigError(
                f"the account holds {bal:.2f} USDC but the exchange contract has an allowance of "
                f"{allow:.2f}. Deposit or trade once through the Polymarket app, which sets the "
                "allowance, or approve the exchange contract for a plain wallet.")
        if bal < self.bankroll:
            log.warning("bankroll %.2f capped to the account's %.2f USDC", self.bankroll, bal)
            self.bankroll = self.starting_balance = self.balance = bal
        if bal < 1.0:
            raise LiveConfigError(f"the account holds only {bal:.2f} USDC; nothing to trade with")
        # After a restart the venue is the only record of what we hold and
        # what is still resting. Stray orders are cancelled; positions we do
        # not know are reported and block new entries through the guard.
        # A DRY RUN (shadow) is read-only: it reports strays and never
        # cancels, or a shadow process would cancel another bot's orders on
        # a shared account.
        if self.armed and not self.adopt_external:
            self.cancel_stray_orders()
        elif self.armed:
            rep = self.reconcile_positions()
            if rep.get("stray_orders"):
                log.warning("%d open venue order(s) this process did not place are LEFT ALONE (the account's manual "
                            "trading is synchronised, not cancelled)", len(rep["stray_orders"]))
        else:
            rep = self.reconcile_positions()
            if rep.get("stray_orders"):
                log.warning("dry run: %d open venue order(s) this process did not place; left alone",
                            len(rep["stray_orders"]))
        return self.snapshot()

    def _fetch_balance(self) -> tuple[float, float | None]:
        bal = _parse_balance(self.client.get_balance_allowance(asset_type="COLLATERAL"))
        self._last_clob_mono = time.monotonic()           # same authenticated connection
        return bal

    # -------------------------------------------------------------- submit

    def _open_notional(self) -> float:
        with self._lock:
            held = sum(p.cost_basis for p in list(self.positions.values()) if p.shares > EPS)
            # a resting buy is money committed the moment it fills
            reserved = sum(r['order'].price * r['order'].remaining for r in list(self.open_orders.values())
                           if r['order'].side is Side.BUY)
        return held + reserved

    def _orders_last_hour(self) -> int:
        cutoff = time.time() - 3600.0
        while self._sent_times and self._sent_times[0] < cutoff:
            self._sent_times.popleft()
        return len(self._sent_times)

    def _precheck(self, order: Order) -> tuple[RejectReason, str] | None:
        # Emergency policy: the kill file and a halt stop NEW risk. A sell
        # only reduces risk, so it still goes out -- a stop-loss that cannot
        # fire because the operator pressed "halt" is the wrong failure.
        # ``<kill_file>_ALL`` stops everything, exits included.
        kill_all = Path(str(self.caps.kill_file) + "_ALL")
        if kill_all.exists():
            return RejectReason.KILL_SWITCH, f"{kill_all} exists (all orders)"
        if order.side is Side.BUY and Path(self.caps.kill_file).exists():
            return RejectReason.KILL_SWITCH, f"{self.caps.kill_file} exists"
        if order.side is Side.BUY and self.halted:
            return RejectReason.HALTED, self.halted
        if not (0.0 < order.price < 1.0) or order.size <= 0.0:
            return RejectReason.TICK_SIZE, f"price {order.price} size {order.size}"
        if order.side is Side.BUY:
            notional = order.price * order.size
            if notional > self.caps.max_order_usdc + 1e-9:
                return RejectReason.LIVE_CAPS, f"order {notional:.2f} > max_order_usdc {self.caps.max_order_usdc:.2f}"
            if self._open_notional() + notional > self.caps.max_open_usdc + 1e-9:
                return RejectReason.LIVE_CAPS, f"open {self._open_notional():.2f} + {notional:.2f} > max_open_usdc"
            if notional > self.balance + 1e-9:
                return RejectReason.INSUFFICIENT_BALANCE, f"cash {self.balance:.2f}"
            # The hourly cap bounds what we ADD. A sell only reduces risk, and
            # refusing one because entries spent the budget is the wrong
            # failure: the position rides to the print with no way out. Exit
            # retries are throttled per market in live.py instead.
            if self._orders_last_hour() >= self.caps.max_orders_per_hour:
                return RejectReason.LIVE_CAPS, f"{self.caps.max_orders_per_hour} orders in the last hour"
        return None

    def submit(self, order: Order, now: float) -> str:
        order.decision_ts = now
        order.state = OrderState.IN_FLIGHT
        self.n_submitted += 1
        why = self._precheck(order)
        if why is not None:
            reason, detail = why
            log.warning("REFUSED %s %s %.2f @ %.2f: %s (%s)", order.side.value, order.token_id[:10],
                        order.size, order.price, reason.value, detail)
            self._finish(order, [], reason, now)
            return order.order_id
        if self._lane_q is not None and order.tif is not TimeInForce.GTC:
            # the position entry exists before a lane can fill it: the lane then only
            # mutates a Position, never inserts into the dict another thread may iterate
            pos = self.positions[order.token_id]
            if not pos.token_id:
                pos.token_id = order.token_id
            self._ensure_lanes()
            self._lane_q.put(order)
            return order.order_id
        self._queue.append(order)
        self._wake.set()
        return order.order_id

    # ----------------------------------------------------------------- lanes

    def _ensure_lanes(self) -> None:
        if self._lane_q is None or self._lanes:
            return
        for i in range(self.order_lanes):
            th = threading.Thread(target=self._lane_loop, name=f"order-lane-{i}", daemon=True)
            th.start()
            self._lanes.append(th)

    def _lane_loop(self) -> None:
        while True:
            order = self._lane_q.get()
            if order is None:
                return
            self._place_tracked(order)

    def stop_lanes(self, timeout: float = 15.0) -> None:
        """Let queued orders finish, then end the lane threads."""
        if self._lane_q is None or not self._lanes:
            return
        for _ in self._lanes:
            self._lane_q.put(None)
        deadline = time.monotonic() + timeout
        for th in self._lanes:
            th.join(max(0.0, deadline - time.monotonic()))
        self._lanes = []

    def _place_tracked(self, order: Order) -> None:
        """_place with the in-flight bookkeeping every path shares."""
        with self._lock:
            self._inflight += 1
            self._post_epoch += 1
        try:
            self._place(order)
        except Exception as exc:                          # noqa: BLE001 -- a lane must never die
            log.exception("order %s failed in the lane", order.order_id)
            self._api_error(exc)
            self._finish(order, [], RejectReason.API_ERROR, self.clock())
        finally:
            with self._lock:
                self._inflight -= 1
                self._reservations.pop(order.order_id, None)

    def _venue_quiet(self, epoch0: int) -> bool:
        """No POST started since ``epoch0`` and none is waiting for its reply."""
        with self._lock:
            return self._inflight == 0 and self._post_epoch == epoch0

    def _finish(self, order: Order, fills: list[Fill], reason: RejectReason, ts: float) -> None:
        if reason is not RejectReason.NONE:
            order.state = OrderState.REJECTED
            self.n_rejected[reason] += 1
        self._done.append(OrderResult(order, fills, reason, ts))

    def step(self, now: float, book_of=None, close_ts_of=None) -> list[OrderResult]:
        # popleft, not list() + clear(): a thread appending between the two would lose
        # that result -- the bot would never learn about a fill
        out = []
        while True:
            try:
                out.append(self._done.popleft())
            except IndexError:
                break
        for r in out:
            r.ack_ts = r.ack_ts or now
        self.results.extend(out)
        return out

    # ---------------------------------------------------------------- worker

    async def run(self, stop: asyncio.Event) -> None:
        """Post queued orders off the event loop; reconcile with the venue."""
        while not stop.is_set():
            if self._cancel_wanted and time.monotonic() - self._last_cancel_mono >= CANCEL_RETRY_S:
                self._last_cancel_mono = time.monotonic()
                self._drop_queued_cancels()
                if self._cancel_wanted:
                    await asyncio.to_thread(self._process_cancels)
                # no ``continue``: a cancel still waiting for its order must not hold up posting and polling
            if self._queue:
                order = self._queue.popleft()
                await asyncio.to_thread(self._place_tracked, order)
                continue
            if self.open_orders and time.monotonic() - self._last_poll_mono >= RESTING_POLL_S:
                self._last_poll_mono = time.monotonic()
                await asyncio.to_thread(self._poll_open_orders)
                continue
            if time.monotonic() * 1000.0 - self.last_reconcile_ms > self.RECONCILE_S * 1000.0:
                self.last_reconcile_ms = time.monotonic() * 1000.0
                await asyncio.to_thread(self.reconcile)
                continue
            due = self._redeem_due(time.monotonic())
            if due is not None:
                await asyncio.to_thread(self._redeem, due)
                continue
            if self._prewarm:
                await asyncio.to_thread(self._prewarm_one, self._prewarm.popleft())
                continue
            if time.monotonic() - self._last_clob_mono >= KEEP_WARM_S:
                await asyncio.to_thread(self._keep_warm)
                continue
            # idle: sleep until an order is queued (submit sets it), at most 50 ms
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=0.05)
        await asyncio.to_thread(self.stop_lanes)
        if self.open_orders:
            await asyncio.to_thread(self.cancel_open_orders)
        close = getattr(self.client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:                             # noqa: BLE001
                pass

    # ------------------------------------------------------------ redemption

    def _redeem_due(self, now_mono: float) -> str | None:
        for cond, (_, due) in self._redeem_pending.items():
            if due <= now_mono:
                return cond
        return None

    def _redeem(self, condition_id: str) -> None:
        """Ask the relayer to redeem one resolved market. Retried while it settles."""
        attempts, _ = self._redeem_pending.get(condition_id, (0, 0.0))
        try:
            self.client.redeem_positions(condition_id=condition_id)
        except Exception as exc:                          # noqa: BLE001
            attempts += 1
            if attempts >= REDEEM_MAX_ATTEMPTS:
                self._redeem_pending.pop(condition_id, None)
                log.warning("redeem %s gave up after %d attempts (stays in pending_redemption; "
                            "redeem it in the app): %s", condition_id[:10], attempts, exc)
            else:
                self._redeem_pending[condition_id] = (attempts, time.monotonic() + REDEEM_RETRY_S)
                log.info("redeem %s not yet possible (attempt %d): %s", condition_id[:10], attempts, str(exc)[:120])
            return
        self._redeem_pending.pop(condition_id, None)
        self.redeemed.append(condition_id)
        log.info("REDEEM submitted for %s; the cash shows up at the next reconcile", condition_id[:10])

    def _api_error(self, exc: Exception) -> None:
        self.consecutive_errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"[:200]
        log.error("venue API error (%d in a row): %s", self.consecutive_errors, self.last_error)
        if self.consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS and not self.halted:
            self.halt(f"{self.consecutive_errors} consecutive API errors")

    def halt(self, reason: str) -> None:
        self.halted = reason
        log.error("LIVE TRADING HALTED: %s", reason)

    def _sign(self, order: Order, price: float, size: float):
        """A signed fill-or-kill (or, for an IOC entry, fill-and-kill) market order with a
        price bound. Nothing is sent."""
        if order.side is Side.BUY:
            # 2026-10-08: a venue market BUY spends its whole USDC amount, so ``size x LIMIT`` dollars bought MORE shares
            # whenever the price was better than the limit (10 sh <= 0.56 at 0.54 = 10.37 sh; a pair at 0.39 under a 0.43
            # limit = 11.41 for 10.37): the two sides of a pair never matched. The amount is now ``size x the price the
            # order expects`` (the ask it was decided on), the limit still bounding it: at that price it buys ``size``
            # shares, to the cent.
            exp = order.expected_price
            basis = exp if exp is not None and 0.0 < exp <= price + 1e-9 else price
            amount = round(size * basis + 1e-9, 2)                  # USDC to spend, to the cent
            kind = "FAK" if order.tif is TimeInForce.IOC else "FOK"
            return self.client.create_market_order(token_id=order.token_id, side="BUY", amount=f"{amount:.2f}",
                                                   max_price=f"{price:.2f}", order_type=kind)
        return self.client.create_market_order(token_id=order.token_id, side="SELL", shares=f"{size:.2f}",
                                               min_price=f"{price:.2f}", order_type="FOK")

    # ------------------------------------------------------------- prewarm

    def _keep_warm(self) -> None:
        """A cheap GET on the connection orders are POSTed on.

        The SDK's HTTP client drops a connection idle for 30 s; orders come
        minutes apart, so each one paid a new TCP + TLS handshake with the
        venue's edge (measured from this VPS: 107 ms cold, 27 ms warm).
        ``/time`` has no side effect. Missing SDK internals just skip it."""
        self._last_clob_mono = time.monotonic()
        transport = getattr(getattr(self.client, "_ctx", None), "secure_clob", None)
        get_json = getattr(transport, "get_json", None)
        if not callable(get_json):
            return
        try:
            get_json("/time")
        except Exception:                                 # noqa: BLE001
            pass

    def prewarm(self, token_id: str) -> None:
        """Ask the SDK to fetch this token's market metadata while idle.

        Signing a price-bounded order resolves the market's tick size and
        exchange first -- a network call the first time a token is seen, and
        every 5-minute window brings new tokens. Paying it at discovery
        instead of between the decision and the POST takes that call off the
        critical path. Signing has no venue side effect; nothing is posted.
        """
        if token_id and token_id not in self._prewarmed:
            self._prewarmed.add(token_id)
            self._prewarm.append(token_id)

    def _prewarm_one(self, token_id: str) -> None:
        try:
            self.client.create_market_order(token_id=token_id, side="BUY", amount="1.00",
                                            max_price="0.50", order_type="FOK")
        except Exception:                                 # noqa: BLE001
            self.prewarm_errors += 1

    def _pre_post_refusal(self, order: Order) -> tuple[RejectReason, str] | None:
        """The last look, in the worker thread, immediately before a POST.

        The event loop's checks ran when the order was queued; since then the
        worker may have been signing, reconciling or redeeming. The kill file
        is read again here, an entry whose decision has gone stale is
        dropped, and the bot's freshest book gets a veto.
        """
        kill_all = Path(str(self.caps.kill_file) + "_ALL")
        if kill_all.exists():
            return RejectReason.KILL_SWITCH, f"{kill_all} exists (all orders)"
        if order.side is Side.BUY and Path(self.caps.kill_file).exists():
            return RejectReason.KILL_SWITCH, f"{self.caps.kill_file} exists"
        if order.side is Side.BUY:
            age = self.clock() - order.decision_ts if order.decision_ts else 0.0
            if age > self.max_entry_decision_age_ms:
                return RejectReason.STALE_DECISION, f"decided {age:.0f} ms ago (> {self.max_entry_decision_age_ms:.0f})"
            # the open-exposure cap again, now: buys for several markets can be queued
            # in one tick, each checked against the same book of positions when it was
            # queued; the ones ahead of this one may have filled since. Entries being
            # posted in parallel lanes are reserved, so they cannot all pass at once.
            notional = order.price * order.size
            with self._lock:
                committed = self._open_notional() + sum(self._reservations.values())
                if committed + notional > self.caps.max_open_usdc + 1e-9:
                    return RejectReason.LIVE_CAPS, (f"open {committed:.2f} + {notional:.2f} > max_open_usdc "
                                                    f"{self.caps.max_open_usdc:.2f} (re-checked before posting)")
                self._reservations[order.order_id] = notional
            if self.pre_post_check is not None:
                try:
                    why = self.pre_post_check(order)
                except Exception:                         # noqa: BLE001
                    why = None                            # a failed look is not a veto; the price cap still binds
                if why:
                    return RejectReason.PRICE_MOVED, why
        return None

    def _place(self, order: Order) -> None:
        if order.tif is TimeInForce.GTC:
            self._place_resting(order)
            return
        price = round(order.price, 2)
        size = math.floor(order.size * 100.0) / 100.0
        t_sign = time.perf_counter()
        try:
            signed = self._sign(order, price, size)
        except Exception as exc:                          # noqa: BLE001
            if _exc_is(exc, "InsufficientLiquidityError"):
                log.info("%s not fillable from the book: %s", _kind(order), exc)
                self._finish(order, [], RejectReason.FOK_UNFILLABLE, self.clock())
            elif _exc_is(exc, "UserInputError"):
                log.warning("order refused before signing: %s", exc)
                self._finish(order, [], RejectReason.TICK_SIZE, self.clock())
            else:
                self._api_error(exc)
                self._finish(order, [], RejectReason.API_ERROR, self.clock())
            return
        order.sign_ms = (time.perf_counter() - t_sign) * 1000.0
        # the last look happens in shadow too, so a dry run shows which
        # entries the real path would have dropped as stale
        refusal = self._pre_post_refusal(order)
        if refusal is not None:
            reason, detail = refusal
            log.warning("NOT POSTED %s %s %.2f @ %.2f: %s (%s)", order.side.value, order.token_id[:10],
                        order.size, order.price, reason.value, detail)
            self._finish(order, [], reason, self.clock())
            return
        if not self.armed:
            log.info("DRY RUN  would post %s %-4s %6.2f sh @ %.2f  (signed locally, not sent)",
                     order.side.value, order.tag[:4], size, price)
            self._finish(order, [], RejectReason.DRY_RUN, self.clock())
            return
        order.sent_ts = self.clock()
        t0 = time.perf_counter()
        try:
            resp = self.client.post_order(signed)
            self._last_clob_mono = time.monotonic()
        except Exception as exc:                          # noqa: BLE001
            order.venue_ack_ts = self.clock()
            self._record_rtt((time.perf_counter() - t0) * 1000.0)
            if _is_fok_kill(exc):
                # it reached the venue and was killed: not a fault, but it does
                # count toward the hourly cap exactly as a killed response would
                self.consecutive_errors = 0
                self._sent_times.append(time.time())
                log.info("%s not filled: %s", _kind(order), str(exc)[:160])
                self._finish(order, [], RejectReason.FOK_UNFILLABLE, self.clock())
            elif _exc_is(exc, "RequestRejectedError") and "invalid amount" in str(exc).lower():
                # the venue's notional floor ("invalid amount for a marketable
                # BUY order ($0.73)"): our sizing, not the venue's health
                self.consecutive_errors = 0
                log.warning("venue refused the size: %s", str(exc)[:160])
                self._finish(order, [], RejectReason.MIN_SIZE, self.clock())
            else:
                self._api_error(exc)
                self._finish(order, [], RejectReason.API_ERROR, self.clock())
            return
        rtt = (time.perf_counter() - t0) * 1000.0
        self._record_rtt(rtt)
        self._sent_times.append(time.time())
        now = self.clock()
        order.venue_ack_ts = now
        vid = str(getattr(resp, "order_id", "") or "")
        if vid:
            order.venue_order_id = vid
            self.own_venue_ids.add(vid)
        if not getattr(resp, "ok", False):
            code = str(getattr(resp, "code", "") or "")
            msg = str(getattr(resp, "message", "") or "")
            if code in UNFILLED_CODES:
                self.consecutive_errors = 0
                log.info("%s not filled: %s", _kind(order), msg or code)
                self._finish(order, [], RejectReason.FOK_UNFILLABLE, now)
            elif code == "not_enough_balance":
                self.consecutive_errors = 0
                log.warning("venue refused for balance: %s", msg)
                self._finish(order, [], RejectReason.INSUFFICIENT_BALANCE, now)
            elif code == "market_not_ready":
                self.consecutive_errors = 0
                self._finish(order, [], RejectReason.MARKET_CLOSED, now)
            else:                                         # auth, signature, unknown: count it
                self._api_error(RuntimeError(f"venue rejected the order: {code}: {msg}"))
                self._finish(order, [], RejectReason.API_ERROR, now)
            return
        self.consecutive_errors = 0
        shares, usdc = self._matched_amounts(resp, order)
        if shares <= EPS:
            self._finish(order, [], RejectReason.FOK_UNFILLABLE, now)
            return
        avg = usdc / shares
        # a fill-or-kill (or fill-and-kill) is matched inside the POST: the reply is the match
        order.venue_fill_ts = now
        fill = self._book_fill(order, avg, shares, now, maker=False)
        order.state = OrderState.FILLED if order.remaining <= EPS else OrderState.PARTIAL
        self._notify_fill(order, avg, shares, now)
        log.info("VENUE FILL %s %.2f sh @ %.4f  fee ~%.4f  rtt %.0fms  id %s",
                 order.side.value, shares, avg, fill.fee, rtt, str(getattr(resp, "order_id", ""))[:12])
        self._finish(order, [fill], RejectReason.NONE, now)

    def _notify_fill(self, order: Order, price: float, shares: float, ts: float) -> None:
        if self.on_fill_event is not None:
            try:
                self.on_fill_event(order, price, shares, ts)
            except Exception:                             # noqa: BLE001 -- telemetry never breaks a fill
                pass

    def _record_rtt(self, rtt_ms: float) -> None:
        """One real POST round trip: kept here, and handed to the bot's
        latency model so paper and the strategy stop assuming 35 ms."""
        self._rtts.append(rtt_ms)
        if len(self._rtts) > 5000:
            del self._rtts[:1000]
        if self.on_rtt is not None:
            try:
                self.on_rtt(rtt_ms)
            except Exception:                             # noqa: BLE001
                pass

    def _book_fill(self, order: Order, price: float, shares: float, now: float, *, maker: bool) -> Fill:
        """Record a fill everywhere it must be seen: order, position, cash."""
        with self._lock:
            return self._book_fill_locked(order, price, shares, now, maker=maker)

    def _book_fill_locked(self, order: Order, price: float, shares: float, now: float, *, maker: bool) -> Fill:
        usdc = price * shares
        fee = self.fees.charge(price, shares, order.token_id, maker=maker)
        fill = Fill(order_id=order.order_id, token_id=order.token_id, side=order.side, price=price,
                    size=shares, fee=fee, exchange_ts=now, ack_ts=now, decision_ts=order.decision_ts,
                    expected_price=order.expected_price, tag=order.tag)
        order.filled_size += shares
        order.filled_notional += usdc
        pos = self.positions[order.token_id]
        if not pos.token_id:
            pos.token_id = order.token_id
        pos.apply(fill)
        cash = -(usdc + fee) if order.side is Side.BUY else (usdc - fee)
        self.balance += cash
        self._local_cash_delta += cash
        self._last_fill_mono = time.monotonic()
        self._token_fill_mono[order.token_id] = self._last_fill_mono
        self.fills.append(fill)
        return fill

    # ------------------------------------------------------------- resting

    def _place_resting(self, order: Order) -> None:
        """Post a GTC limit that rests on the book, and start watching it.

        ``post_only``: an order that would cross is refused rather than
        filled -- a resting entry that turns into a taker pays exactly the
        fee and slippage it was chosen to avoid. The venue-side expiration is
        the crash backstop (the SDK insists on >= 3 minutes); the bot
        cancels at its own ``maker_ttl_s`` from ``_poll_open_orders``.
        """
        price = round(order.price, 2)
        size = math.floor(order.size * 100.0) / 100.0
        now = self.clock()
        try:
            signed = self.client.create_limit_order(
                token_id=order.token_id, price=f"{price:.2f}", size=f"{size:.2f}",
                side=order.side.value, post_only=True,
                expiration=int(time.time()) + RESTING_BACKSTOP_S)
        except Exception as exc:                          # noqa: BLE001
            if _exc_is(exc, "UserInputError"):
                log.warning("resting order refused before signing: %s", exc)
                self._finish(order, [], RejectReason.TICK_SIZE, now)
            else:
                self._api_error(exc)
                self._finish(order, [], RejectReason.API_ERROR, now)
            return
        refusal = self._pre_post_refusal(order)
        if refusal is not None:
            reason, detail = refusal
            log.warning("NOT POSTED (resting) %s %.2f @ %.2f: %s (%s)", order.side.value, size, price,
                        reason.value, detail)
            self._finish(order, [], reason, self.clock())
            return
        if not self.armed:
            log.info("DRY RUN  would rest %s %-4s %6.2f sh @ %.2f  (signed locally, not sent)",
                     order.side.value, order.tag[:4], size, price)
            self._finish(order, [], RejectReason.DRY_RUN, now)
            return
        order.sent_ts = self.clock()
        t0 = time.perf_counter()
        try:
            resp = self.client.post_order(signed)
            self._last_clob_mono = time.monotonic()
        except Exception as exc:                          # noqa: BLE001
            text = str(exc).lower()
            if _is_fok_kill(exc) or ("post" in text and "cross" in text):
                # the ask came down onto our price: it would have taken. Not a fault.
                self.consecutive_errors = 0
                self._sent_times.append(time.time())
                log.info("resting order not posted: %s", str(exc)[:160])
                self._finish(order, [], RejectReason.NO_LIQUIDITY, now)
            elif _exc_is(exc, "RequestRejectedError") and "invalid amount" in text:
                self.consecutive_errors = 0
                log.warning("venue refused the size: %s", str(exc)[:160])
                self._finish(order, [], RejectReason.MIN_SIZE, now)
            else:
                self._api_error(exc)
                self._finish(order, [], RejectReason.API_ERROR, now)
            return
        self._sent_times.append(time.time())
        self._record_rtt((time.perf_counter() - t0) * 1000.0)
        order.venue_ack_ts = self.clock()
        if not getattr(resp, "ok", False):
            code = str(getattr(resp, "code", "") or "")
            msg = str(getattr(resp, "message", "") or "")
            if code in UNFILLED_CODES:
                self.consecutive_errors = 0
                log.info("resting order refused: %s %s", code, msg)
                self._finish(order, [], RejectReason.NO_LIQUIDITY, now)
            else:
                self._api_error(RuntimeError(f"venue rejected the resting order: {code}: {msg}"))
                self._finish(order, [], RejectReason.API_ERROR, now)
            return
        self.consecutive_errors = 0
        venue_id = str(getattr(resp, "order_id", "") or "") or order.order_id
        order.venue_order_id = venue_id
        self.own_venue_ids.add(venue_id)
        order.state = OrderState.RESTING
        self.open_orders[venue_id] = {"order": order, "venue_id": venue_id, "matched": 0.0,
                                      "fills": [], "errors": 0}
        ttl = max(0.0, (order.expires_ts - now) / 1000.0) if order.expires_ts else 0.0
        log.info("RESTING %s %.2f sh @ %.2f  id %s  ttl %.0fs", order.side.value, size, price,
                 venue_id[:12], ttl)

    def _poll_open_orders(self) -> None:
        """What became of each resting order: book new fills, retire the done
        ones, cancel the ones that have waited long enough."""
        now = self.clock()
        for key, rec in list(self.open_orders.items()):
            order: Order = rec["order"]
            expired = bool(order.expires_ts) and now >= order.expires_ts
            try:
                oo = self.client.get_order(order_id=key)
            except Exception as exc:                      # noqa: BLE001
                if "404" in str(exc) or "not found" in str(exc).lower():
                    # gone from the venue; whatever filled is already booked
                    self._complete_open(key, now)
                    continue
                rec["errors"] += 1
                if _exc_is(exc, "UnexpectedResponseError"):
                    # the SDK could not parse the reply (seen for an unknown
                    # id): the venue answered, so this is not a connectivity
                    # fault and must not count toward the halt
                    log.warning("order %s status unreadable (%d): %s", key[:12], rec["errors"], str(exc)[:100])
                else:
                    self._api_error(exc)
                if rec["errors"] >= RESTING_MAX_POLL_ERRORS:
                    self._cancel_open(key, now)
                continue
            self.consecutive_errors = 0
            self._book_matched(key, rec, _num(getattr(oo, "size_matched", None)), now)
            status = str(getattr(oo, "status", "") or "").upper()
            if order.remaining <= EPS or status in ("MATCHED", "CANCELLED", "CANCELED", "EXPIRED"):
                self._complete_open(key, now)
            elif expired:
                self._cancel_open(key, now)

    def cancel(self, order_id: str, now: float) -> None:
        """Pull one of our resting orders (the strategy's own cancel). Non-blocking: the run worker sends it off the
        event loop; the order then ends with ONE result carrying whatever matched before the cancel landed."""
        with self._lock:
            self._cancel_wanted.setdefault(order_id, time.monotonic())
        self._wake.set()

    def _drop_queued_cancels(self) -> None:
        """An order to cancel that is still queued (never posted) is just dropped (event-loop side: the queue is its)."""
        for order in [o for o in self._queue if o.order_id in self._cancel_wanted]:
            self._queue.remove(order)
            with self._lock:
                self._cancel_wanted.pop(order.order_id, None)
            order.state = OrderState.CANCELLED
            log.info("CANCELLED before posting %s %.2f @ %.2f", order.side.value, order.size, order.price)
            self._finish(order, [], RejectReason.EXPIRED, self.clock())

    def _process_cancels(self) -> None:
        now = self.clock()
        with self._lock:
            wanted = dict(self._cancel_wanted)
        for oid, asked in wanted.items():
            key = next((k for k, r in self.open_orders.items() if r["order"].order_id == oid), None)
            if key is None:
                # still being posted (it rests in a moment), or already ended (its result went out): wait a little
                if time.monotonic() - asked > CANCEL_GIVE_UP_S:
                    with self._lock:
                        self._cancel_wanted.pop(oid, None)
                    log.warning("cancel %s: no resting order with that id (it already ended)", oid)
                continue
            with self._lock:
                self._cancel_wanted.pop(oid, None)
            self._cancel_settle(key, now)

    def _cancel_settle(self, key: str, now: float) -> None:
        """Cancel at the venue, then read the order once more: a fill between the last poll and the cancel is booked
        before the order's result goes out (otherwise those shares would be missing from our books)."""
        rec = self.open_orders.get(key)
        if rec is None:
            return
        try:
            self.client.cancel_order(order_id=key)
            self._last_clob_mono = time.monotonic()
            log.info("CANCELLED %s (resting)  id %s", rec["order"].side.value, key[:12])
        except Exception as exc:                          # noqa: BLE001
            log.warning("cancel %s failed (%s); reading its state", key[:12], str(exc)[:80])
        try:
            oo = self.client.get_order(order_id=key)
            self._book_matched(key, rec, _num(getattr(oo, "size_matched", None)), now)
        except Exception as exc:                          # noqa: BLE001
            log.warning("order %s state after the cancel unreadable (%s): reconciliation books any late fill",
                        key[:12], str(exc)[:80])
        self._complete_open(key, now)

    def _book_matched(self, key: str, rec: dict, matched: float, now: float) -> None:
        order: Order = rec["order"]
        new = matched - rec["matched"]
        if new > EPS:
            first = not order.venue_fill_ts
            if first:
                order.venue_fill_ts = now          # an upper bound: the poll that saw it
            rec["fills"].append(self._book_fill(order, order.price, new, now, maker=True))
            if first:
                self._notify_fill(order, order.price, new, now)
            rec["matched"] = matched
            log.info("VENUE FILL (maker) %s %.2f sh @ %.2f  id %s", order.side.value, new, order.price, key[:12])

    def _cancel_open(self, key: str, now: float) -> None:
        rec = self.open_orders.get(key)
        if rec is None:
            return
        try:
            self.client.cancel_order(order_id=key)
        except Exception as exc:                          # noqa: BLE001
            log.warning("cancel %s failed (%s); the venue-side expiry still bounds it",
                        key[:12], str(exc)[:80])
        self._complete_open(key, now)

    def _complete_open(self, key: str, now: float) -> None:
        """Hand the strategy ONE result per resting order, fills included."""
        rec = self.open_orders.pop(key, None)
        if rec is None:
            return
        order: Order = rec["order"]
        if rec["fills"]:
            order.state = OrderState.FILLED if order.remaining <= EPS else OrderState.PARTIAL
            self._finish(order, list(rec["fills"]), RejectReason.NONE, now)
        else:
            order.state = OrderState.CANCELLED
            self._finish(order, [], RejectReason.EXPIRED, now)

    def cancel_open_orders(self) -> None:
        """Leave nothing resting at the venue -- called on the way out."""
        for key in list(self.open_orders):
            self._cancel_open(key, self.clock())

    def _matched_amounts(self, resp: Any, order: Order) -> tuple[float, float]:
        """(shares, usdc) actually matched, from the reply or the order record.

        The venue reports ``making_amount``/``taking_amount`` in USDC and
        shares (BUY: making = USDC spent, taking = shares received).
        """
        making = _num(getattr(resp, "making_amount", None))
        taking = _num(getattr(resp, "taking_amount", None))
        shares, usdc = (taking, making) if order.side is Side.BUY else (making, taking)
        if shares > EPS and usdc > EPS:
            return shares, usdc
        oid = getattr(resp, "order_id", None)
        if not oid:
            return 0.0, 0.0
        for _ in range(3):
            try:
                rec = self.client.get_order(order_id=oid)
            except Exception as exc:                      # noqa: BLE001
                # a killed FOK is gone from the venue's books: not an API fault
                log.info("order %s not retrievable after posting: %s", str(oid)[:12], str(exc)[:120])
                return 0.0, 0.0
            matched = _num(getattr(rec, "size_matched", None))
            px = _num(getattr(rec, "price", None))
            if matched > EPS and px > 0.0:
                return matched, matched * px
            time.sleep(0.3)
        return 0.0, 0.0

    # ------------------------------------------------------------ settlement

    def settle_market(self, token_id: str, won: bool) -> float:
        pos = self.positions.get(token_id)
        if pos is None or abs(pos.shares) <= EPS:
            return 0.0
        payout = pos.shares if won else 0.0
        pnl = payout - pos.cost_basis - pos.fees_paid
        if won:
            # the USDC arrives when the token is redeemed; carry it until then -- unless the venue already paid
            # (reconcile's redemption-ahead credit), in which case that cash is this payout
            ahead = getattr(self, "redeemed_ahead", 0.0)
            take = min(payout, ahead)
            self.redeemed_ahead = ahead - take
            self.pending_redemption += payout - take
            cond = self.condition_of.get(token_id)
            if self.can_redeem and cond and cond not in self._redeem_pending:
                # the market resolves on-chain a little after the TWAP window; first try soon
                self._redeem_pending[cond] = (0, time.monotonic() + 20.0)
        pos.shares = 0.0
        pos.cost_basis = 0.0
        pos.fees_paid = 0.0
        self.realised += pnl
        self._check_daily_loss()
        return pnl

    def _check_daily_loss(self) -> None:
        if self.halted:
            return
        if -self.realised >= self.caps.max_daily_loss_usdc:
            self.halt(f"realised loss {self.realised:+.2f} reached max_daily_loss_usdc {self.caps.max_daily_loss_usdc:.2f}")

    def reconcile(self) -> None:
        """The venue's cash is the truth; ours is a running guess in between.

        Cash that moved which neither our fills nor a pending redemption
        explains is recorded as ``balance_drift`` for the guard (a deposit,
        a fee we mis-estimated, a fill we never saw). Positions and open
        orders are compared in ``reconcile_positions``.
        """
        with self._lock:
            epoch0, busy = self._post_epoch, self._inflight
        if busy:
            # an order is between its POST and its reply: the venue's cash, positions and
            # trade list are ahead of our books. Judging them now latches BALANCE_DRIFT /
            # UNKNOWN_POSITION / FOREIGN_TRADE on our own fill. Try again shortly.
            self.reconcile_deferred += 1
            self.last_reconcile_ms -= (self.RECONCILE_S - 2.0) * 1000.0
            return
        try:
            bal, allow = self._fetch_balance()
        except Exception as exc:                          # noqa: BLE001
            self._api_error(exc)
            return
        self.consecutive_errors = 0
        if not self._venue_quiet(epoch0):
            self.reconcile_deferred += 1
            self.last_reconcile_ms -= (self.RECONCILE_S - 2.0) * 1000.0
            return
        self.venue_balance, self.allowance = bal, allow
        if self.venue_balance_at_start is None:
            self.venue_balance_at_start = bal
        venue_delta = bal - self.venue_balance_at_start
        if self._last_fill_mono and time.monotonic() - self._last_fill_mono < self.CASH_SETTLE_GRACE_S:
            # A fill is still settling: the venue has not moved its cash yet, so
            # the gap to our figure is that fill, not an unexplained move. On
            # 2026-09-24 a check 3 s after a 2.05 USDC buy latched BALANCE_DRIFT
            # (+2.05, cancelled by the next check) and stopped entries for an hour.
            # The cash comparison waits for a quiet check and neither side is
            # re-based; positions and foreign trades are still checked now (trades first: an adopted trade must
            # not then be adopted again as an unknown position).
            self.check_foreign_trades(epoch0=epoch0)
            self.reconcile_positions(epoch0=epoch0)
            return

        # cash that moved which our own fills do not explain: redemptions
        # (or a deposit). Retire pending redemptions against it.
        unexplained = venue_delta - self._local_cash_delta
        if unexplained > 0.01:
            redeemed = min(unexplained, self.pending_redemption)
            self.pending_redemption = max(0.0, self.pending_redemption - redeemed)
            unexplained -= redeemed
        ahead = sum(self._settling().values())
        if 0.005 < unexplained <= ahead + 0.01:
            # the venue paid a resolved market we have not settled yet (it redeems about a minute after the close;
            # Gamma publishes the result minutes later). The cash is ours to trade with NOW: the balance follows it
            # here, and the payout settle_market books later is retired against it (redeemed_ahead) instead of
            # waiting for cash that has already come. 2026-10-06 01:50-01:56: the bot sat at "free 0.04" through two
            # opens with 26 USDC of winnings in the account, because this branch did not credit them.
            self.redemptions_ahead += 1
            self.redeemed_ahead = getattr(self, "redeemed_ahead", 0.0) + unexplained
            log.info("venue cash +%.4f USDC arrived with %.2f sh of unsettled winners outstanding: a redemption "
                     "ahead of our settlement, credited now (%.4f awaiting its settlement)", unexplained, ahead, self.redeemed_ahead)
            with self._lock:
                self._local_cash_delta = venue_delta
                self.balance = self.bankroll + venue_delta
            self.check_foreign_trades(epoch0=epoch0)
            self.reconcile_positions(epoch0=epoch0)
            return
        if abs(unexplained) > 0.005 and getattr(self, "adopt_external", False):
            # an account the user also trades by hand: cash moved outside our markets (a redemption of leftover
            # tokens, a trade elsewhere, a deposit) is the account's business -- followed, logged, never a latch
            self.external_cash = getattr(self, "external_cash", 0.0) + unexplained
            log.warning("venue cash moved %+.4f USDC by activity outside this bot's markets (cumulative %+.4f): "
                        "followed, not drift", unexplained, self.external_cash)
        elif abs(unexplained) > 0.005:
            self.balance_drift += unexplained
            log.warning("venue cash moved %+.4f USDC that no fill or redemption explains (cumulative %+.4f)",
                        unexplained, self.balance_drift)
        with self._lock:
            self._local_cash_delta = venue_delta
            self.balance = self.bankroll + venue_delta
        self.check_foreign_trades(epoch0=epoch0)
        self.reconcile_positions(epoch0=epoch0)

    def check_foreign_trades(self, epoch0: int | None = None) -> list[dict]:
        """Is anything else trading this account? Returns the NEW foreign trades.

        Every trade the venue lists for the account since this process started
        must belong to an order this process placed (``own_venue_ids``, taken
        from each POST reply before the next reconciliation can run). A trade
        that does not is another process, another bot or a human on the same
        account -- the 2026-09-23 12:59 fill of 6.33 shares that neither bot
        sent is exactly this. The guard latches on it: the bot's balance,
        positions and PnL can no longer be attributed.
        """
        fn = getattr(self.client, "list_account_trades", None)
        if not callable(fn):
            return []
        try:
            rows = self._rows(fn(after=str(int(self.started_s))), limit=500)
        except Exception as exc:                          # noqa: BLE001 -- a read, not an order: never a halt
            log.warning("could not list account trades: %s", str(exc)[:120])
            return []
        if epoch0 is not None and not self._venue_quiet(epoch0):
            self.reconcile_deferred += 1
            return []                                     # a POST overlapped the read: judge it next time
        self.trades_checked += 1
        new: list[dict] = []
        for r in rows:
            tid = str(self._field(r, "id", "trade_id") or "")
            if not tid or tid in self._seen_trade_ids:
                continue
            self._seen_trade_ids.add(tid)
            ts = _epoch_s(self._field(r, "matched_at", "match_time", "timestamp"))
            if ts is not None and ts < self.started_s - 1.0:
                continue                                  # before this process existed
            role = str(self._field(r, "trader_side") or "").upper()
            taker = str(self._field(r, "taker_order_id") or "")
            makers = [str(self._field(mo, "order_id") or "") for mo in (self._field(r, "maker_orders") or [])]
            if role == "TAKER":
                ours = taker in self.own_venue_ids
            elif role == "MAKER":
                ours = any(mid in self.own_venue_ids for mid in makers)
            else:
                ours = taker in self.own_venue_ids or any(mid in self.own_venue_ids for mid in makers)
            if not ours:
                row = {"trade_id": tid, "token_id": str(self._field(r, "asset_id", "token_id") or ""),
                       "side": str(self._field(r, "side") or ""), "size": _num(self._field(r, "size")),
                       "price": _num(self._field(r, "price")), "matched_at": ts, "role": role}
                if self._adopt_trade(row):
                    continue
                new.append(row)
        if new and self.adopt_external:
            # trades by hand in markets this bot does not trade: the account's business, listed, never a latch
            for row in new:
                self.external_trades.append({**row, "role": f"OTHER_MARKET/{row.get('role') or '?'}"})
            del self.external_trades[:-50]
            log.warning("%d trade(s) by hand outside this bot's markets: %s", len(new), new[:3])
            return []
        if new:
            self.foreign_trades.extend(new)
            del self.foreign_trades[:-50]
            log.error("FOREIGN TRADE(S) on this account that this process did not place: %s", new[:3])
        return new

    # ------------------------------------------------------- positions/orders

    @staticmethod
    def _rows(resp: Any, limit: int = 2000) -> list:
        """Items from whatever the SDK returned: a Paginator (iterate its
        items), a Page, a list, or a dict with a data list."""
        if resp is None:
            return []
        iter_items = getattr(resp, "iter_items", None)
        if callable(iter_items):                          # polymarket.pagination.Paginator
            out = []
            for it in iter_items():
                out.append(it)
                if len(out) >= limit:
                    break
            return out
        if isinstance(resp, dict):
            for key in ("data", "positions", "orders", "results", "items"):
                if isinstance(resp.get(key), (list, tuple)):
                    return list(resp[key])
            return []
        if isinstance(resp, (list, tuple)):
            return list(resp)
        for key in ("items", "data", "positions", "orders", "results"):
            rows = getattr(resp, key, None)
            if isinstance(rows, (list, tuple)):
                return list(rows)
        try:
            return list(resp)
        except TypeError:
            return []

    @staticmethod
    def _field(row: Any, *names: str) -> Any:
        for n in names:
            if isinstance(row, dict):
                if row.get(n) is not None:
                    return row[n]
            elif getattr(row, n, None) is not None:
                return getattr(row, n)
        return None

    def venue_positions(self) -> dict[str, float] | None:
        """token id -> shares the venue says we hold, or None if unsupported."""
        fn = getattr(self.client, "list_positions", None)
        if not callable(fn):
            return None
        try:
            resp = fn()
        except TypeError:
            resp = fn(user=self.address) if self.address else None
        out: dict[str, float] = {}
        for row in self._rows(resp):
            tok = self._field(row, "asset_id", "asset", "token_id", "tokenId")
            size = _num(self._field(row, "current_size", "size", "shares", "amount", "quantity"))
            if tok and size > EPS:
                out[str(tok)] = out.get(str(tok), 0.0) + size
        return out

    def venue_open_orders(self) -> list[str] | None:
        fn = getattr(self.client, "list_open_orders", None)
        if not callable(fn):
            return None
        rows = self._rows(fn())
        return [str(self._field(r, "id", "order_id", "orderId") or "") for r in rows]

    def venue_trades(self, limit: int = 200) -> list[dict]:
        """Recent fills the venue recorded for this account (newest first)."""
        fn = getattr(self.client, "list_trades", None)
        if not callable(fn):
            return []
        out = []
        for r in self._rows(fn(), limit=limit):
            out.append({"token_id": str(self._field(r, "asset_id", "asset") or ""),
                        "side": str(self._field(r, "side") or ""),
                        "size": _num(self._field(r, "size")), "price": _num(self._field(r, "price")),
                        "timestamp": str(self._field(r, "timestamp") or ""),
                        "slug": str(self._field(r, "slug") or "")})
        return out

    def reconcile_positions(self, epoch0: int | None = None) -> dict:
        """Compare the venue's positions and open orders with ours.

        Unknown venue positions are recorded, not adopted: we do not know
        their cost basis, and a position we did not buy means either a
        restart lost state or a fill we never saw. The guard stops new
        entries until a human has looked. Missing positions (we think we hold
        something the venue does not) are recorded the same way.
        """
        report = {"unknown_positions": [], "missing_positions": [], "stray_orders": [], "legacy_positions": [],
                  "checked": False}
        try:
            venue = self.venue_positions()
        except Exception as exc:                          # noqa: BLE001
            self._api_error(exc)
            return report
        if venue is None:
            return report
        if epoch0 is not None and not self._venue_quiet(epoch0):
            self.reconcile_deferred += 1
            return report                                 # a POST overlapped the read: not judged, not stored
        report["checked"] = True
        self.venue_positions_last = dict(venue)
        with self._lock:
            ours = {t: p.shares for t, p in list(self.positions.items()) if p.shares > EPS}
        # tokens of the markets this process is trading; a venue position in
        # any other market predates this run (an unredeemed winner, dust) and
        # is reported as legacy rather than as a fill we missed
        watched = set(self.watched_tokens()) if callable(self.watched_tokens) else None
        #: how many live-market tokens this check could see: 0 means it ran
        #: before discovery and proves nothing about the markets we trade
        report["watched_n"] = 0 if watched is None else len(watched)
        for tok, size in venue.items():
            have = ours.get(tok, 0.0)
            if size > have + 0.5:                       # the venue rounds to hundredths
                row = {"token_id": tok, "venue": round(size, 2), "ours": round(have, 2)}
                # only a position in a market THIS process is trading can be
                # a fill we missed; anything else (and everything before
                # discovery has run) is legacy: reported, never a trip
                if watched is not None and tok in watched:
                    if self._adopt_position(tok, size - have):
                        report.setdefault("adopted_positions", []).append(row)
                    elif self._adopting(tok):
                        report.setdefault("pending_adoption", []).append(row)   # waiting out ADOPT_CONFIRM_S: no trip
                    else:
                        report["unknown_positions"].append(row)
                else:
                    report["legacy_positions"].append(row)
        for tok in list(self._pos_diff_seen):
            if abs(venue.get(tok, 0.0) - ours.get(tok, 0.0)) <= 0.5:
                self._pos_diff_seen.pop(tok, None)
        settling = self._settling()
        for tok, have in ours.items():
            if have > venue.get(tok, 0.0) + 0.5:
                row = {"token_id": tok, "venue": round(venue.get(tok, 0.0), 2), "ours": round(have, 2)}
                if tok in settling:
                    report.setdefault("redeemed_pending", []).append(row)   # resolved and redeemed by the venue first
                elif watched is not None and tok in watched and self._adopt_position(tok, venue.get(tok, 0.0) - have):
                    report.setdefault("adopted_positions", []).append(row)   # sold outside the bot
                elif watched is not None and tok in watched and self._adopting(tok):
                    report.setdefault("pending_adoption", []).append(row)
                else:
                    report["missing_positions"].append(row)
        try:
            open_ids = self.venue_open_orders()
        except Exception as exc:                          # noqa: BLE001
            open_ids = None
            log.warning("could not list open orders: %s", str(exc)[:120])
        if open_ids is not None:
            report["stray_orders"] = [oid for oid in open_ids if oid and oid not in self.open_orders]
        self.position_report = report
        if report["unknown_positions"] or report["missing_positions"]:
            log.error("POSITION MISMATCH venue vs bot: unknown %s missing %s",
                      report["unknown_positions"], report["missing_positions"])
        return report

    def _settling(self) -> dict:
        try:
            return dict(self.settling_tokens()) if callable(self.settling_tokens) else {}
        except Exception:                                 # noqa: BLE001
            return {}

    def _adopt_trade(self, row: dict) -> bool:
        """A trade on this account that is not ours, in a market this bot trades: booked as our fill when
        adopting (position and cash move as for any fill; the bot is told). True when adopted."""
        if not self.adopt_external:
            return False
        watched = set(self.watched_tokens()) if callable(self.watched_tokens) else set()
        tok = row.get("token_id") or ""
        if tok not in watched or row["size"] <= EPS or not (0.0 < row["price"] < 1.0):
            return False
        side = Side.BUY if str(row["side"]).upper().startswith("B") else Side.SELL
        now = self.clock()
        for i, (atok, aside, aqty, at) in enumerate(self._adopted_recent):
            if atok == tok and aside == side.value and abs(aqty - row["size"]) <= 0.51 and now - at < 1_800_000.0:
                del self._adopted_recent[i]               # the position check booked it first (at the mark)
                log.info("venue trade %s matches a position already adopted: not booked twice", str(row["trade_id"])[:12])
                return True
        order = Order(token_id=tok, side=side, price=row["price"], size=row["size"], tif=TimeInForce.FOK,
                      expected_price=row["price"], tag=f"polymarket.com {row['side']} ({row.get('role') or '?'})")
        order.decision_ts = order.sent_ts = order.venue_ack_ts = order.venue_fill_ts = now
        order.venue_order_id = f"web-{row['trade_id']}"
        fill = self._book_fill(order, row["price"], row["size"], now, maker=str(row.get("role") or "").upper() == "MAKER")
        order.state = OrderState.FILLED
        self.external_trades.append({**row, "order_id": order.order_id, "fee": round(fill.fee, 6), "adopted_at": now})
        del self.external_trades[:-50]
        log.warning("ADOPTED a trade made outside this bot: %s %.2f %s @ %.4f (trade %s)", row["side"], row["size"],
                    tok[:10], row["price"], str(row["trade_id"])[:12])
        if self.on_external_fill is not None:
            try:
                self.on_external_fill(order, fill, row)
            except Exception:                             # noqa: BLE001 -- bookkeeping never breaks a reconcile
                log.exception("on_external_fill failed")
        return True

    def _adopting(self, tok: str) -> bool:
        """A synchronised account's watched-market difference is waiting out its confirmation (or our own fill on
        the token is recent): reported as pending, not as a mismatch for the guard."""
        return bool(self.adopt_external and self.on_external_position is not None
                    and (tok in self._pos_diff_seen or time.monotonic() - self._token_fill_mono.get(tok, -1e9)
                         < max(self.ADOPT_CONFIRM_S, self.CASH_SETTLE_GRACE_S)))

    def _confirmed_diff(self, tok: str, diff: float) -> bool:
        """Has this difference (same direction) been there for ADOPT_CONFIRM_S, with none of our own fills on the
        token inside that time? Otherwise it is most likely the venue's position list catching up with a fill."""
        now = time.monotonic()
        sign = 1 if diff > 0 else -1
        seen = self._pos_diff_seen.get(tok)
        if seen is None or seen[0] != sign:
            self._pos_diff_seen[tok] = (sign, now)
            return False
        if now - seen[1] < self.ADOPT_CONFIRM_S:
            return False
        if now - self._token_fill_mono.get(tok, -1e9) < max(self.ADOPT_CONFIRM_S, self.CASH_SETTLE_GRACE_S):
            return False
        return True

    def _adopt_position(self, tok: str, diff: float) -> bool:
        """The venue holds ``diff`` more (bought outside the bot) or fewer (sold outside the bot; diff < 0) shares of
        a watched market than we do, for ADOPT_CONFIRM_S and with no fill of ours on the token in that time: booked
        as a fill at the bot's mark -- the ask for a buy, the bid for a sale -- through the same path as any fill
        (position, cash, the bot's callback), so the books follow the account even when the venue's trade list does
        not deliver the trade (2026-10-05: it did not). True when adopted."""
        if not self.adopt_external or self.on_external_position is None or abs(diff) <= EPS:
            return False
        if not self._confirmed_diff(tok, diff):
            return False
        self._pos_diff_seen.pop(tok, None)
        try:
            px = self.on_external_position(tok, diff)
        except Exception:                                 # noqa: BLE001
            log.exception("on_external_position failed")
            return False
        if px is None or not (0.0 < px < 1.0):
            return False
        side = Side.BUY if diff > 0 else Side.SELL
        qty = round(abs(diff), 2)
        now = self.clock()
        order = Order(token_id=tok, side=side, price=px, size=qty, tif=TimeInForce.FOK, expected_price=px,
                      tag=f"polymarket.com {side.value} (from the venue's positions, at the mark)")
        order.decision_ts = order.sent_ts = order.venue_ack_ts = order.venue_fill_ts = now
        order.venue_order_id = f"pos-{tok[:8]}-{int(now)}"
        fill = self._book_fill(order, px, qty, now, maker=False)
        order.state = OrderState.FILLED
        self._adopted_recent.append((tok, side.value, qty, now))
        del self._adopted_recent[:-50]
        row = {"trade_id": order.venue_order_id, "token_id": tok, "side": side.value, "size": qty, "price": px,
               "matched_at": now / 1000.0, "role": "POSITION", "order_id": order.order_id, "fee": round(fill.fee, 6),
               "adopted_at": now}
        self.external_trades.append(row)
        del self.external_trades[:-50]
        log.warning("ADOPTED from the venue's positions: %s %.2f %s at the mark %.3f (traded outside this bot)",
                    side.value, qty, tok[:10], px)
        if self.on_external_fill is not None:
            try:
                self.on_external_fill(order, fill, row)
            except Exception:                             # noqa: BLE001
                log.exception("on_external_fill failed")
        return True

    def cancel_stray_orders(self) -> int:
        """Cancel venue orders this process did not place (after a restart)."""
        rep = self.reconcile_positions()
        n = 0
        for oid in rep.get("stray_orders", []):
            try:
                self.client.cancel_order(order_id=oid)
                n += 1
            except Exception as exc:                      # noqa: BLE001
                log.warning("cancel stray order %s failed: %s", oid[:12], str(exc)[:80])
        if n:
            log.warning("cancelled %d stray order(s) left at the venue by a previous run", n)
        return n

    # ------------------------------------------------------------- reporting

    def latency_report(self) -> dict[str, float]:
        filled = {f.order_id for f in self.fills}
        slips = [f.slippage for f in self.fills if f.expected_price is not None]
        return {
            "orders_submitted": float(self.n_submitted),
            "orders_filled": float(len(filled)),
            "fill_rate": len(filled) / self.n_submitted if self.n_submitted else 0.0,
            "rejected_fok_unfillable": float(self.n_rejected[RejectReason.FOK_UNFILLABLE]),
            "rejected_market_closed": float(self.n_rejected[RejectReason.MARKET_CLOSED]),
            "rejected_no_liquidity": float(self.n_rejected[RejectReason.NO_LIQUIDITY]),
            "mean_slippage_per_share": sum(slips) / len(slips) if slips else 0.0,
            "total_slippage_cost": sum(f.slippage * f.size for f in self.fills if f.expected_price is not None),
            "total_fees": sum(f.fee for f in self.fills),
            "mean_round_trip_ms": sum(self._rtts) / len(self._rtts) if self._rtts else 0.0,
            "p95_round_trip_ms": sorted(self._rtts)[int(len(self._rtts) * 0.95)] if len(self._rtts) >= 20 else 0.0,
            "filled_shares": sum(f.size for f in self.fills),
        }

    def equity(self, mark=None) -> float:
        eq = self.balance + self.pending_redemption
        if mark is not None:
            for token_id, pos in list(self.positions.items()):
                if abs(pos.shares) <= EPS:
                    continue
                m = mark(token_id)
                if m is not None:
                    eq += pos.shares * m
        return eq

    def snapshot(self) -> dict:
        return {
            "armed": self.armed,
            "address": self.address,
            "wallet_type": str(getattr(self.client, "wallet_type", "") or "") or None,
            "can_redeem": self.can_redeem,
            "redeem_pending": len(self._redeem_pending),
            "redeemed": len(self.redeemed),
            "bankroll": round(self.bankroll, 4),
            "venue_balance": None if self.venue_balance is None else round(self.venue_balance, 4),
            "venue_balance_at_start": None if self.venue_balance_at_start is None else round(self.venue_balance_at_start, 4),
            "allowance": None if self.allowance is None else round(self.allowance, 2),
            "pending_redemption": round(self.pending_redemption, 4),
            "redeemed_ahead": round(getattr(self, "redeemed_ahead", 0.0), 4),
            "realised": round(self.realised, 4),
            "halted": self.halted,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
            "balance_drift": round(self.balance_drift, 4),
            "external_cash": round(self.external_cash, 4),
            "adopt_external": self.adopt_external,
            "positions": self.position_report,
            "venue_positions": {k: round(v, 4) for k, v in self.venue_positions_last.items()},
            "external_trades": list(self.external_trades[-10:]),
            "foreign_trades": list(self.foreign_trades[-10:]),
            "trades_checked": self.trades_checked,
            "rtt_p95_ms": sorted(self._rtts)[int(len(self._rtts) * 0.95)] if len(self._rtts) >= 5 else None,
            "orders_last_hour": self._orders_last_hour(),
            "kill_file_present": Path(self.caps.kill_file).exists(),
            "caps": {"max_order_usdc": self.caps.max_order_usdc, "max_open_usdc": self.caps.max_open_usdc,
                     "max_daily_loss_usdc": self.caps.max_daily_loss_usdc,
                     "max_orders_per_hour": self.caps.max_orders_per_hour, "kill_file": self.caps.kill_file},
        }
