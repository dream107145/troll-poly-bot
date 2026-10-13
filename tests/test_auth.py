"""The dashboard's login gate.

The console's API starts and stops the trader, rewrites the control file and
creates the kill file, so "is this endpoint reachable without a session" is a
correctness question, not a nicety. These tests drive a real
``ThreadingHTTPServer`` on an ephemeral port rather than calling handler
methods directly, because the thing being tested is the HTTP behaviour --
status codes, cookies, redirects -- not the shape of a Python call.
"""
from __future__ import annotations

import http.client
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest

from troll_poly_bot.web import auth, server

EMAIL = "admin@example.com"
PASSWORD = "a-long-enough-passphrase"


# ------------------------------------------------------------------ hashing


def test_password_round_trip():
    encoded = auth.hash_password(PASSWORD, rounds=1000)
    assert auth.verify_password(PASSWORD, encoded)
    assert not auth.verify_password(PASSWORD + "x", encoded)


def test_hash_is_salted():
    """Two hashes of one password must differ, or the hash leaks reuse."""
    a = auth.hash_password(PASSWORD, rounds=1000)
    b = auth.hash_password(PASSWORD, rounds=1000)
    assert a != b


@pytest.mark.parametrize("junk", ["", "garbage", "pbkdf2_sha256$notanint$a$b",
                                  "md5$1$a$b", "a$b$c$d$e"])
def test_malformed_hash_is_a_failure_not_an_exception(junk):
    """A typo in .env must not become a 500 on an unauthenticated endpoint."""
    assert auth.verify_password(PASSWORD, junk) is False


# -------------------------------------------------------------- credentials


def test_credentials_require_an_email(monkeypatch):
    monkeypatch.delenv("TPB_ADMIN_EMAIL", raising=False)
    monkeypatch.setenv("TPB_ADMIN_PASSWORD", PASSWORD)
    with pytest.raises(auth.AuthError, match="TPB_ADMIN_EMAIL"):
        auth.Credentials.from_env()


def test_credentials_require_a_password(monkeypatch):
    monkeypatch.setenv("TPB_ADMIN_EMAIL", EMAIL)
    monkeypatch.delenv("TPB_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("TPB_ADMIN_PASSWORD_HASH", raising=False)
    with pytest.raises(auth.AuthError, match="TPB_ADMIN_PASSWORD"):
        auth.Credentials.from_env()


def test_short_password_is_refused(monkeypatch):
    monkeypatch.setenv("TPB_ADMIN_EMAIL", EMAIL)
    monkeypatch.setenv("TPB_ADMIN_PASSWORD", "short")
    monkeypatch.delenv("TPB_ADMIN_PASSWORD_HASH", raising=False)
    with pytest.raises(auth.AuthError, match="8 characters"):
        auth.Credentials.from_env()


def test_plain_password_is_not_kept_in_memory(monkeypatch):
    """from_env hashes immediately; the cleartext should not survive on the object."""
    monkeypatch.setenv("TPB_ADMIN_EMAIL", EMAIL)
    monkeypatch.setenv("TPB_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("TPB_ADMIN_PASSWORD_HASH", raising=False)
    creds = auth.Credentials.from_env()
    assert PASSWORD not in repr(vars(creds))
    assert creds.password_hash.startswith("pbkdf2_sha256$")


def test_hash_wins_over_plain_password(monkeypatch):
    monkeypatch.setenv("TPB_ADMIN_EMAIL", EMAIL)
    monkeypatch.setenv("TPB_ADMIN_PASSWORD", "the-wrong-one-entirely")
    monkeypatch.setenv("TPB_ADMIN_PASSWORD_HASH", auth.hash_password(PASSWORD, rounds=1000))
    creds = auth.Credentials.from_env()
    assert creds.check(EMAIL, PASSWORD)
    assert not creds.check(EMAIL, "the-wrong-one-entirely")


def test_email_match_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("TPB_ADMIN_EMAIL", "Admin@Example.COM")
    monkeypatch.setenv("TPB_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("TPB_ADMIN_PASSWORD_HASH", raising=False)
    creds = auth.Credentials.from_env()
    assert creds.check("ADMIN@example.com", PASSWORD)
    assert not creds.check("someone@else.com", PASSWORD)


# ----------------------------------------------------------------- sessions


def test_session_lifecycle():
    sessions = auth.Sessions()
    token = sessions.create(EMAIL)
    assert sessions.get(token)["email"] == EMAIL
    sessions.destroy(token)
    assert sessions.get(token) is None


def test_session_tokens_are_unique_and_long():
    sessions = auth.Sessions()
    tokens = {sessions.create(EMAIL) for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 32 for t in tokens)


def test_absolute_expiry():
    sessions = auth.Sessions(ttl_s=-1)
    assert sessions.get(sessions.create(EMAIL)) is None


def test_idle_expiry():
    sessions = auth.Sessions(idle_s=-1)
    assert sessions.get(sessions.create(EMAIL)) is None


def test_unknown_and_empty_tokens():
    sessions = auth.Sessions()
    assert sessions.get(None) is None
    assert sessions.get("") is None
    assert sessions.get("not-a-real-token") is None


def test_session_table_is_bounded():
    sessions = auth.Sessions()
    for _ in range(auth.MAX_SESSIONS + 20):
        sessions.create(EMAIL)
    assert len(sessions._sessions) <= auth.MAX_SESSIONS


# ------------------------------------------------------------- rate limiter


def test_limiter_blocks_after_max_failures():
    lim = auth.RateLimiter(max_failures=3, window_s=60)
    assert lim.blocked_for("1.2.3.4") == 0
    for _ in range(3):
        lim.record_failure("1.2.3.4")
    assert lim.blocked_for("1.2.3.4") > 0
    assert lim.blocked_for("5.6.7.8") == 0          # per address, not global


def test_success_clears_the_counter():
    lim = auth.RateLimiter(max_failures=2, window_s=60)
    lim.record_failure("1.2.3.4")
    lim.record_success("1.2.3.4")
    lim.record_failure("1.2.3.4")
    assert lim.blocked_for("1.2.3.4") == 0


def test_window_rolls_off():
    lim = auth.RateLimiter(max_failures=1, window_s=-1)
    lim.record_failure("1.2.3.4")
    assert lim.blocked_for("1.2.3.4") == 0


# -------------------------------------------------------------- cookie glue


@pytest.mark.parametrize("header,expected", [
    (None, {}),
    ("", {}),
    ("tpb_session=abc", {"tpb_session": "abc"}),
    ("a=1; tpb_session=abc; b=2", {"tpb_session": "abc", "a": "1", "b": "2"}),
    ('tpb_session="quoted"', {"tpb_session": "quoted"}),
    ("=nonsense;;;", {}),
])
def test_cookie_parsing_never_raises(header, expected):
    got = auth.parse_cookies(header)
    for k, v in expected.items():
        assert got[k] == v


def test_cookie_flags():
    plain = auth.cookie_header("tok", secure=False)
    assert "HttpOnly" in plain and "SameSite=Lax" in plain and "Secure" not in plain
    assert "Secure" in auth.cookie_header("tok", secure=True)
    assert "Max-Age=0" in auth.clear_cookie_header(secure=False)


# ------------------------------------------------------------ the live server


@pytest.fixture()
def console(monkeypatch):
    """A real console on an ephemeral port, with a known admin."""
    monkeypatch.setattr(server, "CREDENTIALS",
                        auth.Credentials(EMAIL, auth.hash_password(PASSWORD, rounds=1000)))
    monkeypatch.setattr(server, "SESSIONS", auth.Sessions())
    monkeypatch.setattr(server, "LOGIN_LIMITER", auth.RateLimiter())
    monkeypatch.setattr(auth, "FAILURE_DELAY_S", 0.0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()


def _request(addr, method, path, *, body=None, headers=None):
    conn = http.client.HTTPConnection(*addr, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _login(addr, email=EMAIL, password=PASSWORD):
    body = urllib.parse.urlencode({"email": email, "password": password})
    return _request(addr, "POST", "/login", body=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"})


def _token(headers):
    return auth.parse_cookies(headers.get("Set-Cookie", "")).get(auth.COOKIE)


API_PATHS = ["/api/live", "/api/bot", "/api/history", "/api/controls",
             "/api/saved", "/api/earnings", "/api/account", "/api/schema",
             "/api/session"]
WRITE_PATHS = ["/api/bot/start", "/api/bot/stop", "/api/bot/restart",
               "/api/controls", "/api/kill", "/api/run"]


@pytest.mark.parametrize("path", API_PATHS)
def test_api_is_closed_without_a_session(console, path):
    status, _, _ = _request(console, "GET", path)
    assert status == 401, f"{path} served an anonymous caller"


@pytest.mark.parametrize("path", WRITE_PATHS)
def test_writes_are_closed_without_a_session(console, path):
    """The reason this gate exists: these start the trader and write files."""
    status, _, _ = _request(console, "POST", path, body="{}",
                            headers={"Content-Type": "application/json"})
    assert status == 401, f"{path} accepted an anonymous write"


def test_static_bundle_is_closed(console):
    for path in ("/", "/index.html", "/static/app.js", "/static/styles.css"):
        status, _, _ = _request(console, "GET", path)
        assert status in (401, 303), f"{path} leaked to an anonymous caller"


def test_websocket_upgrade_is_closed(console):
    status, _, _ = _request(console, "GET", "/ws", headers={
        "Upgrade": "websocket", "Connection": "Upgrade",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13",
    })
    assert status == 401


def test_favicon_stays_public(console):
    """The login page references it, so it must render before there is a session."""
    status, _, _ = _request(console, "GET", "/static/favicon.svg")
    assert status == 200


def test_login_page_is_reachable_and_self_contained(console):
    status, _, body = _request(console, "GET", "/login")
    assert status == 200
    assert b"<form" in body and b"password" in body
    # no reference to the gated bundle, or the page cannot style itself
    assert b"/static/app.js" not in body and b"/static/styles.css" not in body


def test_browser_navigation_redirects_to_login(console):
    status, headers, _ = _request(console, "GET", "/",
                                  headers={"Accept": "text/html"})
    assert status == 303
    assert headers["Location"].startswith("/login?next=")


def test_good_credentials_open_everything(console):
    status, headers, _ = _login(console)
    assert status == 303
    token = _token(headers)
    assert token
    cookie = {"Cookie": f"{auth.COOKIE}={token}"}
    for path in API_PATHS:
        got, _, _ = _request(console, "GET", path, headers=cookie)
        assert got == 200, f"{path} refused a signed-in caller"
    got, _, _ = _request(console, "GET", "/", headers=cookie)
    assert got == 200


def test_session_cookie_is_httponly(console):
    _, headers, _ = _login(console)
    assert "HttpOnly" in headers["Set-Cookie"]
    assert "SameSite=Lax" in headers["Set-Cookie"]


def test_session_reports_the_signed_in_email(console):
    _, headers, _ = _login(console)
    cookie = {"Cookie": f"{auth.COOKIE}={_token(headers)}"}
    status, _, body = _request(console, "GET", "/api/session", headers=cookie)
    assert status == 200
    assert EMAIL in body.decode()


@pytest.mark.parametrize("email,password", [
    (EMAIL, "wrong-password-entirely"),
    ("someone@else.com", PASSWORD),
    ("someone@else.com", "wrong-password-entirely"),
    ("", ""),
])
def test_bad_credentials_are_refused(console, email, password):
    status, headers, body = _login(console, email, password)
    assert status == 401
    assert _token(headers) is None
    # the same message either way: which half was wrong is not public
    assert b"Wrong email or password" in body


def test_forged_cookie_is_not_a_session(console):
    status, _, _ = _request(console, "GET", "/api/live",
                            headers={"Cookie": f"{auth.COOKIE}=made-up-token"})
    assert status == 401


def test_logout_invalidates_the_session(console):
    _, headers, _ = _login(console)
    cookie = {"Cookie": f"{auth.COOKIE}={_token(headers)}"}
    assert _request(console, "GET", "/api/live", headers=cookie)[0] == 200
    assert _request(console, "POST", "/api/logout", headers=cookie)[0] == 200
    assert _request(console, "GET", "/api/live", headers=cookie)[0] == 401


def test_rate_limiter_stops_guessing(console):
    for _ in range(auth.MAX_FAILURES):
        _login(console, EMAIL, "wrong-password-entirely")
    status, _, body = _login(console, EMAIL, "wrong-password-entirely")
    assert status == 429
    assert b"Too many failed attempts" in body
    # and the throttle holds even once the password is right
    assert _login(console)[0] == 429


@pytest.mark.parametrize("target", [
    "https://evil.example.com/",
    "//evil.example.com/",
    "http://evil.example.com",
])
def test_login_will_not_redirect_off_site(console, target):
    """An open redirect here would make the login page a credible bounce."""
    body = urllib.parse.urlencode({"email": EMAIL, "password": PASSWORD,
                                   "next": target})
    status, headers, _ = _request(console, "POST", "/login", body=body,
                                  headers={"Content-Type":
                                           "application/x-www-form-urlencoded"})
    assert status == 303
    assert headers["Location"] == "/"


def test_login_honours_a_local_next(console):
    body = urllib.parse.urlencode({"email": EMAIL, "password": PASSWORD,
                                   "next": "/#ledger"})
    _, headers, _ = _request(console, "POST", "/login", body=body,
                             headers={"Content-Type":
                                      "application/x-www-form-urlencoded"})
    assert headers["Location"] == "/#ledger"


def test_login_page_escapes_what_it_echoes(console):
    """The email is reflected on a failure, so it is an injection point."""
    body = urllib.parse.urlencode({"email": '"><script>alert(1)</script>',
                                   "password": "nope-not-this-one"})
    _, _, page = _request(console, "POST", "/login", body=body,
                          headers={"Content-Type":
                                   "application/x-www-form-urlencoded"})
    assert b"<script>alert" not in page
    assert b"&lt;script&gt;" in page


def test_oversized_login_body_is_refused(console):
    body = urllib.parse.urlencode({"email": "a" * 20000, "password": PASSWORD})
    status, _, _ = _request(console, "POST", "/login", body=body,
                            headers={"Content-Type":
                                     "application/x-www-form-urlencoded"})
    assert status == 400
