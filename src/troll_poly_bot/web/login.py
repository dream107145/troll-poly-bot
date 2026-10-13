"""The login page, as one self-contained document.

Deliberately not a file in ``static/``: everything under that directory is
behind the gate, and a login page that pulled its stylesheet from the other
side of the gate could not render. Inlining it also means an unauthenticated
caller is served exactly one thing -- no app bundle, no fonts from a third
party, nothing that says what is behind the door.

It follows the console's own tokens closely enough to look like the same
product, in both colour schemes, without importing its 30 KB of CSS.
"""
from __future__ import annotations

import html

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Sign in &middot; troll-poly-bot</title>
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<style>
  :root {{
    color-scheme: light;
    --bg: #f6f7f9;
    --card: #ffffff;
    --ink: #11161d;
    --muted: #5c6773;
    --line: #dfe3e8;
    --accent: #2f6fdb;
    --accent-ink: #ffffff;
    --error-bg: #fdecec;
    --error-ink: #96201c;
    --error-line: #f0bcba;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      color-scheme: dark;
      --bg: #0e1116;
      --card: #161b22;
      --ink: #e7edf3;
      --muted: #8b98a5;
      --line: #2a313a;
      --accent: #4b8bf0;
      --accent-ink: #0b0f14;
      --error-bg: #2a1618;
      --error-ink: #f2b8b5;
      --error-line: #5c2a2c;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    background: var(--bg);
    color: var(--ink);
    font: 400 14px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  main {{
    width: 100%;
    max-width: 380px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 28px;
  }}
  .mark {{
    width: 34px; height: 34px; border-radius: 9px;
    background: linear-gradient(135deg, var(--accent), #7d4bf0);
    margin-bottom: 16px;
  }}
  h1 {{ margin: 0 0 4px; font-size: 19px; font-weight: 600; letter-spacing: -0.01em; }}
  p.sub {{ margin: 0 0 22px; color: var(--muted); font-size: 13px; }}
  label {{ display: block; margin-bottom: 14px; }}
  label span {{
    display: block; margin-bottom: 6px;
    font-size: 12px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.04em;
  }}
  input {{
    width: 100%; padding: 10px 12px;
    border: 1px solid var(--line); border-radius: 8px;
    background: var(--bg); color: var(--ink);
    font: inherit;
  }}
  input:focus-visible {{
    outline: 2px solid var(--accent); outline-offset: 1px; border-color: transparent;
  }}
  button {{
    width: 100%; margin-top: 6px; padding: 11px 14px;
    border: 0; border-radius: 8px; cursor: pointer;
    background: var(--accent); color: var(--accent-ink);
    font: 600 14px/1 inherit; font-family: inherit;
  }}
  button:hover {{ filter: brightness(1.07); }}
  .error {{
    margin: 0 0 18px; padding: 10px 12px;
    background: var(--error-bg); color: var(--error-ink);
    border: 1px solid var(--error-line); border-radius: 8px;
    font-size: 13px;
  }}
  footer {{
    margin: 20px 0 0; padding-top: 16px; border-top: 1px solid var(--line);
    color: var(--muted); font-size: 12px;
  }}
</style>
</head>
<body>
<main>
  <div class="mark" aria-hidden="true"></div>
  <h1>Live console</h1>
  <p class="sub">Sign in to reach the trading dashboard.</p>
  {error}
  <form method="POST" action="/login">
    <input type="hidden" name="next" value="{next}">
    <label>
      <span>Email</span>
      <input type="email" name="email" autocomplete="username"
             required autofocus value="{email}">
    </label>
    <label>
      <span>Password</span>
      <input type="password" name="password" autocomplete="current-password" required>
    </label>
    <button type="submit">Sign in</button>
  </form>
  <footer>This console can start and stop the trader. Credentials are set in
  <code>.env</code> on the server.</footer>
</main>
</body>
</html>
"""


def render(error: str = "", *, email: str = "", next_url: str = "/") -> bytes:
    """The login page. Every interpolated value is escaped -- ``error`` and
    ``next`` both originate in a query string."""
    block = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    return PAGE.format(
        error=block,
        email=html.escape(email, quote=True),
        next=html.escape(next_url or "/", quote=True),
    ).encode()
