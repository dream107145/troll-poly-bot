"""Email + password gate for the dashboard.

The console's API is not read-only: ``do_POST`` starts and stops the trader,
rewrites ``data/controls.json`` and creates the kill file. Until now the bind
address was the only access control there was, which is why the README told
you to keep it on loopback and tunnel in. This module is what lets it face a
network instead.

Stdlib only, like the rest of ``web/`` -- ``hashlib.pbkdf2_hmac`` for the
password, ``secrets`` for the session token, a dict for the session table.

Credentials come from the environment (``.env`` is loaded at startup)::

    TPB_ADMIN_EMAIL=you@example.com
    TPB_ADMIN_PASSWORD=a-long-random-passphrase

Prefer storing a hash instead of the password itself; it takes precedence when
both are set, and ``python scripts/hash_password.py`` prints one::

    TPB_ADMIN_PASSWORD_HASH=pbkdf2_sha256$600000$<salt>$<digest>

There is no user database and no registration: one admin, configured out of
band. That is the whole threat model -- keep one operator out of everyone
else's reach, not run a multi-tenant service.

Sessions live in memory, so restarting the server logs you out. That is a
feature for a process that pm2 restarts on a crash, not an oversight.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

log = logging.getLogger(__name__)

#: Cookie the browser carries. Session-scoped, HttpOnly, SameSite=Lax.
COOKIE = "tpb_session"

#: How long a session is good for, and how long it may sit idle.
SESSION_TTL_S = 12 * 3600
IDLE_TTL_S = 2 * 3600

#: Brute-force limiter: this many failures from one address inside the window
#: and that address is refused until the window rolls off.
MAX_FAILURES = 8
FAILURE_WINDOW_S = 300.0

#: A wrong password costs this long, so guessing is slow even under the limit.
FAILURE_DELAY_S = 0.25

#: Ceiling on the session table, so a flood of logins cannot grow it forever.
MAX_SESSIONS = 256

PBKDF2_ROUNDS = 600_000
HASH_PREFIX = "pbkdf2_sha256"


class AuthError(Exception):
    """Configuration is missing or unusable. Fatal at startup, never at runtime."""


# --------------------------------------------------------------- passwords


def hash_password(password: str, *, rounds: int = PBKDF2_ROUNDS,
                  salt: bytes | None = None) -> str:
    """``pbkdf2_sha256$rounds$salt$digest``, the value for TPB_ADMIN_PASSWORD_HASH."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    b64 = lambda raw: base64.b64encode(raw).decode()          # noqa: E731
    return f"{HASH_PREFIX}${rounds}${b64(salt)}${b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash, in constant time.

    A malformed hash is a failed verification rather than an exception: the
    login path must not turn a typo in ``.env`` into a 500 that leaks a stack
    trace to an unauthenticated caller.
    """
    try:
        prefix, rounds, salt_b64, digest_b64 = encoded.split("$")
        if prefix != HASH_PREFIX:
            return False
        want = base64.b64decode(digest_b64)
        got = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                  base64.b64decode(salt_b64), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


# ------------------------------------------------------------ credentials


class Credentials:
    """The one admin identity, read from the environment.

    ``password_hash`` is the only form kept in memory when a hash was
    supplied; a plain ``TPB_ADMIN_PASSWORD`` is hashed on load so the
    cleartext does not sit in the process for its whole life.
    """

    def __init__(self, email: str, password_hash: str) -> None:
        self.email = email
        self.password_hash = password_hash

    @classmethod
    def from_env(cls) -> "Credentials":
        email = (os.getenv("TPB_ADMIN_EMAIL") or "").strip().lower()
        hashed = (os.getenv("TPB_ADMIN_PASSWORD_HASH") or "").strip()
        plain = os.getenv("TPB_ADMIN_PASSWORD") or ""
        if not email:
            raise AuthError(
                "TPB_ADMIN_EMAIL is not set. The dashboard will not start without "
                "an admin identity -- its API can start and stop the trader. "
                "Put TPB_ADMIN_EMAIL and TPB_ADMIN_PASSWORD in .env "
                "(see .env.example)."
            )
        if "@" not in email:
            raise AuthError(f"TPB_ADMIN_EMAIL does not look like an email: {email!r}")
        if hashed:
            if not hashed.startswith(HASH_PREFIX + "$"):
                raise AuthError(
                    f"TPB_ADMIN_PASSWORD_HASH must start with {HASH_PREFIX}$ -- "
                    "generate it with `python scripts/hash_password.py`"
                )
            return cls(email, hashed)
        if not plain:
            raise AuthError(
                "TPB_ADMIN_PASSWORD is not set (and no TPB_ADMIN_PASSWORD_HASH). "
                "Put one in .env; see .env.example."
            )
        if len(plain) < 8:
            raise AuthError(
                "TPB_ADMIN_PASSWORD is shorter than 8 characters. This server "
                "may be reachable from a network -- use a real passphrase."
            )
        return cls(email, hash_password(plain))

    def check(self, email: str, password: str) -> bool:
        """True when both halves match. Both are compared even when the email
        is wrong, so a bad address cannot be told from a bad password by how
        long the answer took."""
        email_ok = hmac.compare_digest((email or "").strip().lower(), self.email)
        password_ok = verify_password(password or "", self.password_hash)
        return email_ok and password_ok


# --------------------------------------------------------------- sessions


class Sessions:
    """Token -> session, with absolute and idle expiry.

    Guarded by a lock because ``ThreadingHTTPServer`` gives every request its
    own thread, and the websocket push loop touches this too.
    """

    def __init__(self, ttl_s: float = SESSION_TTL_S, idle_s: float = IDLE_TTL_S) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self.ttl_s = ttl_s
        self.idle_s = idle_s

    def create(self, email: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._sessions) >= MAX_SESSIONS:
                # drop the least recently used rather than refuse the login
                oldest = min(self._sessions, key=lambda t: self._sessions[t]["seen"])
                self._sessions.pop(oldest, None)
            self._sessions[token] = {"email": email, "created": now, "seen": now}
        return token

    def get(self, token: str | None) -> dict | None:
        """The live session for a token, refreshing its idle clock."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._prune(now)
            sess = self._sessions.get(token)
            if sess is None:
                return None
            sess["seen"] = now
            return dict(sess)

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _prune(self, now: float) -> None:
        dead = [t for t, s in self._sessions.items()
                if now - s["created"] > self.ttl_s or now - s["seen"] > self.idle_s]
        for t in dead:
            self._sessions.pop(t, None)


# ----------------------------------------------------------- rate limiting


class RateLimiter:
    """Per-address failure counter with a rolling window."""

    def __init__(self, max_failures: int = MAX_FAILURES,
                 window_s: float = FAILURE_WINDOW_S) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self.max_failures = max_failures
        self.window_s = window_s

    def blocked_for(self, addr: str) -> float:
        """Seconds until this address may try again; 0 when it may try now."""
        now = time.time()
        with self._lock:
            hits = self._trim(addr, now)
            if len(hits) < self.max_failures:
                return 0.0
            return max(0.0, self.window_s - (now - hits[0]))

    def record_failure(self, addr: str) -> None:
        now = time.time()
        with self._lock:
            self._trim(addr, now).append(now)

    def record_success(self, addr: str) -> None:
        with self._lock:
            self._fails.pop(addr, None)

    def clear(self) -> None:
        with self._lock:
            self._fails.clear()

    def _trim(self, addr: str, now: float) -> list[float]:
        hits = [t for t in self._fails.get(addr, []) if now - t < self.window_s]
        self._fails[addr] = hits
        return hits


# ------------------------------------------------------------ cookie glue


def parse_cookies(header: str | None) -> dict[str, str]:
    """Pull name=value pairs out of a Cookie header.

    Hand-rolled rather than ``http.cookies`` because that module raises on
    input this endpoint receives unauthenticated, and a malformed cookie
    should be an absent session, not a 500.
    """
    out: dict[str, str] = {}
    for chunk in (header or "").split(";"):
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip().strip('"')
    return out


def cookie_header(token: str, *, secure: bool, max_age: int | None = None) -> str:
    """Set-Cookie for a new session.

    ``Secure`` is conditional: the dashboard speaks plain HTTP unless there is
    a TLS proxy in front, and a Secure cookie on an http:// origin is dropped
    by the browser -- which would present as a login that never takes.
    """
    parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append(f"Max-Age={max_age}")
    return "; ".join(parts)


def clear_cookie_header(*, secure: bool) -> str:
    return cookie_header("", secure=secure, max_age=0)


def cookie_secure_from_env() -> bool:
    """Whether to mark the cookie Secure. Off by default: this server is HTTP.

    Set ``TPB_ADMIN_COOKIE_SECURE=1`` when a TLS-terminating proxy sits in
    front, so the session cookie never crosses a plaintext hop.
    """
    return (os.getenv("TPB_ADMIN_COOKIE_SECURE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
