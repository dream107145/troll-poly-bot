"""Where is the money? Diagnose a --live setup without placing anything.

    python scripts/live_check.py

Reads the same .env the bot does and prints ONLY public addresses and
balances, never the key. It logs in through Polymarket's unified SDK, which
reports the account's wallet type itself (Deposit Wallet for every account
created or upgraded since May 2026, legacy proxy or Safe for older ones),
then reads the collateral balance, the exchange allowances and the public
Data API for the account wallet.

The signer address printed here is NOT the address on your Polymarket
profile, and that is expected: the key signs, the account wallet holds the
funds.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from dotenv import load_dotenv                         # noqa: E402

load_dotenv()

from troll_poly_bot.execution.polymarket import LiveConfigError, LiveCredentials, _parse_balance   # noqa: E402
from troll_poly_bot.history_store import read_points   # noqa: E402


def data_api(path: str, params: dict):
    url = f"https://data-api.polymarket.com{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "troll-poly-bot/0.1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _rows(resp):
    from troll_poly_bot.execution.polymarket import PolymarketExchange
    return PolymarketExchange._rows(resp)


def _g(row, *names):
    for n in names:
        v = row.get(n) if isinstance(row, dict) else getattr(row, n, None)
        if v is not None:
            return v
    return None


def clob_checks(client) -> None:
    """The questions the bot must answer before it sends anything."""
    print("\nCLOB account state:")
    try:
        pos = _rows(client.list_positions())
        print(f"  positions           {len(pos)}")
        for r in pos[:8]:
            print(f"    token ...{str(_g(r, 'asset_id', 'asset', 'token_id') or '')[-10:]}  size {_g(r, 'current_size', 'size')}  "
                  f"avg {_g(r, 'avg_price', 'avgPrice')}  value {_g(r, 'current_value', 'currentValue')}  "
                  f"{_g(r, 'slug', 'title') or ''}")
    except Exception as exc:                              # noqa: BLE001
        print(f"  positions           FAILED: {type(exc).__name__}: {str(exc)[:120]}")
    try:
        oo = _rows(client.list_open_orders())
        print(f"  open orders         {len(oo)}")
        for r in oo[:8]:
            print(f"    {str(_g(r, 'id', 'order_id') or '')[:12]}  {_g(r, 'side')}  {_g(r, 'price')} x {_g(r, 'original_size', 'size')}")
    except Exception as exc:                              # noqa: BLE001
        print(f"  open orders         FAILED: {type(exc).__name__}: {str(exc)[:120]}")
    try:
        tr = _rows(client.list_trades())
        print(f"  recent fills        {len(tr)} (venue's own record)")
        for r in tr[:5]:
            print(f"    {_g(r, 'timestamp')}  {_g(r, 'side')} {_g(r, 'size')} @ {_g(r, 'price')}  {_g(r, 'slug')}")
    except Exception as exc:                              # noqa: BLE001
        print(f"  recent fills        FAILED: {type(exc).__name__}: {str(exc)[:120]}")
    try:
        client.get_order(order_id="0x" + "0" * 64)
        print("  order status        endpoint answered (unexpectedly found a zero id)")
    except Exception as exc:                              # noqa: BLE001
        txt = str(exc).lower()
        ok = "404" in txt or "not found" in txt or "invalid" in txt
        print(f"  order status        {'endpoint reachable (clean not-found for a fake id)' if ok else 'FAILED: ' + type(exc).__name__ + ': ' + str(exc)[:100]}")

    print("\nmarkets the bot is watching (data/live_state.json) vs the venue's own book:")
    state_path = pathlib.Path("data/live_state.json")
    if not state_path.exists():
        print("  no state file: start the bot (any mode) to populate it")
        return
    try:
        state = json.load(state_path.open())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  state unreadable: {exc}")
        return
    hist = state.get("price_history", {})
    shown = 0
    for m in state.get("live_markets", []):
        slug = m["slug"]
        pts = hist.get(slug) or read_points(state_path, slug) or []
        last = pts[-1] if pts else {}
        ub, ua = last.get("ub"), last.get("ua")
        spread = None if (ub is None or ua is None) else round(ua - ub, 3)
        print(f"  {slug:26} {m['status']:9} {m['secs_left']:6.1f}s left  UP bid {ub} ask {ua} spread {spread}  "
              f"DOWN bid {last.get('db')} ask {last.get('da')}  local sample age {round((state.get('uptime_s', 0) and 0) or 0, 1)}")
        if shown < 2 and m["status"] == "live":
            shown += 1
            try:
                row = data_api_gamma(slug)
                tokens = json.loads(row.get("clobTokenIds") or "[]") if row else []
                if tokens:
                    ob = client.get_order_book(token_id=str(tokens[0]))
                    bids = sorted((float(_g(l, "price")) for l in _rows(getattr(ob, "bids", None) or (ob.get("bids") if isinstance(ob, dict) else []))), reverse=True)
                    asks = sorted(float(_g(l, "price")) for l in _rows(getattr(ob, "asks", None) or (ob.get("asks") if isinstance(ob, dict) else [])))
                    vb, va = (bids[0] if bids else None), (asks[0] if asks else None)
                    print(f"      venue REST book now: UP bid {vb} ask {va}   (local {ub}/{ua}; a persistent gap is the local lag)")
            except Exception as exc:                      # noqa: BLE001
                print(f"      venue book: {type(exc).__name__}: {str(exc)[:100]}")


def data_api_gamma(slug: str) -> dict | None:
    url = f"https://gamma-api.polymarket.com/markets?{urllib.parse.urlencode({'slug': slug})}"
    req = urllib.request.Request(url, headers={"User-Agent": "troll-poly-bot/0.1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        rows = json.load(r)
    return rows[0] if rows else None


def main() -> None:
    try:
        creds = LiveCredentials.from_env()
    except LiveConfigError as exc:
        print(f"config: {exc}")
        return
    print(f"configured wallet        {creds.wallet or '(unset: the SDK derives your Deposit Wallet)'}")
    print(f"relayer api key          {'given (the bot can redeem winners)' if creds.can_redeem else 'none (use Auto-Redeem in the app)'}")

    try:
        client = creds.build_client()
    except Exception as exc:                              # noqa: BLE001
        print(f"\nlogin FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        print("   The key must be the signer of the account whose address is TPB_POLY_WALLET.")
        print("   Email login: profile -> settings -> export private key.")
        return
    bal = 0.0
    try:
        wallet_type = str(client.wallet_type)
        wallet = str(client.wallet)
        print(f"\nvenue says: wallet type  {wallet_type}")
        print(f"            wallet       {wallet}")
        print(f"            signer       {client.signer}")
        bal, allow = _parse_balance(client.get_balance_allowance(asset_type="COLLATERAL"))
        allow_txt = "n/a" if allow is None else ("unlimited" if allow > 1e12 else f"{allow:.2f}")
        print(f"            collateral   {bal:.2f} USDC   exchange allowance {allow_txt}")
        try:
            state = client.get_trading_approvals_state()
            # the perpetuals deposit approval is not needed to trade the 5-minute markets
            perps = {"0xdca4af75705dbb50f62437045aff9921947917d2"}
            try:
                from polymarket.environments import PRODUCTION, get_environment_config
                perps.add(str(get_environment_config(PRODUCTION).perps_deposit_contract).lower())
            except Exception:                             # noqa: BLE001
                pass
            missing = [a.spender for a in state.missing.erc20 if str(a.spender).lower() not in perps]
            missing += [f"CTF for {a.operator}" for a in getattr(state.missing, "erc1155", ())]
            if not missing:
                print("            approvals    complete for CLOB trading")
            else:
                print(f"            approvals    MISSING for CLOB trading: {', '.join(map(str, missing))}")
                print("                         (trade once in the app, or give the bot a Relayer API key and run")
                print("                          setup_trading_approvals; with a plain wallet approve them yourself)")
        except Exception as exc:                          # noqa: BLE001
            print(f"            approvals    could not be read ({type(exc).__name__})")
        clob_checks(client)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    print("\npublic Data API (no key needed):")
    try:
        v = data_api("/v2/value", {"user": wallet})
        data = v.get("data") if isinstance(v, dict) else v
        if isinstance(data, list):
            data = data[0] if data else {}
        val = (data or {}).get("value")
        pos = data_api("/v2/positions", {"user": wallet, "limit": 5})
        rows = pos.get("data") if isinstance(pos, dict) else pos
        print(f"  {wallet}  portfolio value {val}  open positions {len(rows or [])}")
    except Exception as exc:                              # noqa: BLE001
        print(f"  {wallet}  error: {type(exc).__name__}: {str(exc)[:80]}")

    print()
    if bal >= 1.0:
        print(f"-> ready: {bal:.2f} USDC tradable from the {wallet_type}. Next: python -m troll_poly_bot --live")
    else:
        print("-> the account shows no collateral. Deposits made in the app take a minute or two to appear.")
        print("   Check that TPB_POLY_WALLET is the address on your profile and the key is that login's signer.")


if __name__ == "__main__":
    main()
