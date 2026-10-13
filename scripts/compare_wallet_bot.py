"""Our paper bot vs the wallet dangerously-skip-permissions (0xc53375ff...) on the same BTC 5-minute markets.

The wallet's activity comes from data-api /activity (15-minute windows, split at the 500 cap), cached incrementally in
data/replay/wallet_dsp_activity.jsonl. Our bot's fills come from data/live_trades.<tag>.jsonl. Both are settled on the
gamma outcome (closed + resolved 1/0 only; cached in data/replay/btc5m_outcomes.json). Per market: shares per side,
cost (fees in: the wallet's usdcSize, our price x size + fee), P&L = winning shares - cost. Rebates are reported apart.

    .venv/bin/python scripts/compare_wallet_bot.py --since 2026-10-09T18:11:10 [--tag risingrecovery-paper] [--out FILE]
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import statistics as st
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WALLET = "0xc53375ff94e96100f2b30a4b5775db35218d69a9"
URL = "https://data-api.polymarket.com/activity?user={w}&limit=500&start={s}&end={e}&sortBy=TIMESTAMP&sortDirection=DESC"
CACHE = ROOT / "data" / "replay" / "wallet_dsp_activity.jsonl"
OUTCOMES = ROOT / "data" / "replay" / "btc5m_outcomes.json"
KEEP = ("timestamp", "type", "side", "slug", "outcome", "price", "size", "usdcSize", "transactionHash", "asset")


def _get(url: str):
    for k in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tpb-research"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except Exception:                                   # noqa: BLE001
            time.sleep(2 * (k + 1))
    raise RuntimeError(f"failed: {url}")


def _window(s: int, e: int) -> list:
    d = _get(URL.format(w=WALLET, s=s, e=e))
    if len(d) >= 500 and e - s > 1:
        m = (s + e) // 2
        return _window(s, m) + _window(m + 1, e)
    return d


def wallet_activity(since: int, until: int) -> list[dict]:
    """Cached records in [since, until]; the newest 15 minutes are always fetched again (late indexing)."""
    rows, have = {}, 0
    if CACHE.exists():
        for line in CACHE.open():
            x = json.loads(line)
            rows[(x["transactionHash"], x["asset"], x["type"], x["size"], x["price"], x["timestamp"], x.get("outcome"))] = x
            have = max(have, int(x["timestamp"]))
    start = max(since, have - 900) if have else since
    for s in range(start, until, 900):
        for x in _window(s, min(s + 899, until)):
            r = {k: x.get(k) for k in KEEP}
            rows[(r["transactionHash"], r["asset"], r["type"], r["size"], r["price"], r["timestamp"], r.get("outcome"))] = r
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("w") as f:
        for r in sorted(rows.values(), key=lambda r: r["timestamp"]):
            f.write(json.dumps(r) + "\n")
    return [r for r in rows.values() if since <= int(r["timestamp"]) <= until]


def outcomes(slugs: set[str]) -> dict[str, int]:
    known = json.loads(OUTCOMES.read_text()) if OUTCOMES.exists() else {}
    todo = sorted(s for s in slugs if s not in known)
    for i in range(0, len(todo), 40):
        url = "https://gamma-api.polymarket.com/markets?closed=true&limit=100&" + "&".join("slug=" + s for s in todo[i:i + 40])
        for m in _get(url):
            px, names = json.loads(m.get("outcomePrices") or "[]"), json.loads(m.get("outcomes") or "[]")
            if m.get("closed") and px and set(px) <= {"0", "1"}:
                known[m["slug"]] = int(dict(zip(names, map(float, px))).get("Up", 0) == 1.0)
    OUTCOMES.write_text(json.dumps(known))
    return known


def _blank():
    return {"UP": 0.0, "DOWN": 0.0, "cost": 0.0, "buys": 0, "t": [], "px": []}


def per_market_wallet(recs: list[dict]) -> tuple[dict, float]:
    mk, rebates = collections.defaultdict(_blank), 0.0
    for r in recs:
        if r["type"] in ("TAKER_REBATE", "MAKER_REBATE", "REWARD"):
            rebates += float(r["usdcSize"] or 0.0)
            continue
        if r["type"] != "TRADE" or not str(r["slug"]).startswith("btc-updown-5m-"):
            continue
        side = "UP" if str(r["outcome"]).lower() == "up" else "DOWN"
        q, cost = float(r["size"]), float(r["usdcSize"] or 0.0)
        m = mk[r["slug"]]
        sign = 1.0 if r["side"] == "BUY" else -1.0
        m[side] += sign * q
        m["cost"] += sign * cost
        m["buys"] += 1
        m["t"].append(int(r["timestamp"]) - int(r["slug"].rsplit("-", 1)[1]))
        m["px"].append(float(r["price"]))
    return mk, rebates


def per_market_bot(tag: str, since: int, until: int, archives: bool = False) -> dict:
    """Our fills in [since, until]; with ``archives`` also those of the runs archived under data/archive/<tag>_* (a
    restart archives the run, and a market closed but unresolved at the restart is dropped by the paper venue -- its
    fills still count here, settled on the real outcome)."""
    mk = collections.defaultdict(_blank)
    paths = [ROOT / "data" / f"live_trades.{tag}.jsonl"]
    if archives:
        paths += sorted((ROOT / "data" / "archive").glob(f"{tag}_*/live_trades.{tag}.jsonl"))
    seen = set()
    for path in paths:
        if path.exists():
            _read_fills(path, since, until, mk, seen)
    return mk


def _read_fills(path: Path, since: int, until: int, mk: dict, seen: set) -> None:
    for line in path.open():
        r = json.loads(line)
        if r.get("event") != "fill" or r.get("action") != "BUY":
            continue
        ts = r["ts"] / 1000.0
        if not since <= ts <= until:
            continue
        key = (r["slug"], r["ts"], r["side"], r["size"], r["price"])
        if key in seen:
            continue
        seen.add(key)
        m = mk[r["slug"]]
        q = float(r["size"])
        m[r["side"]] += q
        m["cost"] += q * float(r["price"]) + float(r.get("fee") or 0.0)
        m["buys"] += 1
        m["t"].append(int(ts) - int(r["slug"].rsplit("-", 1)[1]))
        m["px"].append(float(r["price"]))


def pnl(m: dict, up: int) -> float:
    return (m["UP"] if up == 1 else m["DOWN"]) - m["cost"]


def stats(v: list[float]) -> str:
    if not v:
        return "n 0"
    se = st.pstdev(v) / math.sqrt(len(v)) if len(v) > 1 else float("nan")
    return f"{st.mean(v):+.2f} / market (+-{se:.2f}, n {len(v)}, total {sum(v):+.2f})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="UTC, e.g. 2026-10-09T18:11:10")
    ap.add_argument("--until", default=None)
    ap.add_argument("--tag", default="risingrecovery-paper")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--include-archives", action="store_true", help="also the archived runs' fills (a watch across restarts)")
    a = ap.parse_args()
    since = int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp())
    until = int(datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc).timestamp()) if a.until else int(time.time())
    w, rebates = per_market_wallet(wallet_activity(since - 300, until))
    b = per_market_bot(a.tag, since, until, a.include_archives)
    w = {s: m for s, m in w.items() if int(s.rsplit("-", 1)[1]) >= since - 300}
    up = outcomes(set(w) | set(b))
    res_w = {s: pnl(m, up[s]) for s, m in w.items() if s in up}
    res_b = {s: pnl(m, up[s]) for s, m in b.items() if s in up}
    both = sorted(set(res_w) & set(res_b))
    fmt_t = lambda t: time.strftime("%m-%d %H:%M", time.gmtime(t))
    L = [f"# Paper bot ({a.tag}) vs dangerously-skip-permissions, {fmt_t(since)} -> {fmt_t(until)} UTC", "",
         f"Resolved BTC 5m markets: wallet {len(res_w)} (of {len(w)} traded), bot {len(res_b)} (of {len(b)} traded), both {len(both)}.", "",
         "| | P&L per market | spend per market | return on spend |", "|---|---|---:|---:|"]
    for name, res, mk in (("wallet (all its markets)", res_w, w), ("bot (all its markets)", res_b, b)):
        spend = [mk[s]["cost"] for s in res]
        L.append(f"| {name} | {stats(list(res.values()))} | {st.mean(spend) if spend else 0:.1f} | "
                 f"{100 * sum(res.values()) / sum(spend) if spend and sum(spend) else 0:+.2f} % |")
    for name, res, mk in (("wallet, markets both traded", res_w, w), ("bot, markets both traded", res_b, b)):
        v = [res[s] for s in both]
        spend = [mk[s]["cost"] for s in both]
        L.append(f"| {name} | {stats(v)} | {st.mean(spend) if spend else 0:.1f} | "
                 f"{100 * sum(v) / sum(spend) if spend and sum(spend) else 0:+.2f} % |")
    L += ["", f"Wallet rebates paid in the window: {rebates:+.2f} USDC (not in the P&L above).", "", "## How they trade", ""]
    for name, mk in (("wallet", w), ("bot", b)):
        ts = [x for m in mk.values() for x in m["t"]]
        px = [x for m in mk.values() for x in m["px"]]
        lean = [abs(m["UP"] - m["DOWN"]) / max(m["UP"], m["DOWN"]) for m in mk.values() if max(m["UP"], m["DOWN"]) > 0]
        twos = sum(1 for m in mk.values() if m["UP"] > 0 and m["DOWN"] > 0)
        if ts:
            L.append(f"- {name}: {len(mk)} markets, {sum(m['buys'] for m in mk.values())} buys; seconds after the open p10/median/p90 "
                     f"{sorted(ts)[len(ts) // 10]}/{int(st.median(ts))}/{sorted(ts)[9 * len(ts) // 10]}; price median {st.median(px):.2f}; "
                     f"both sides in {twos}/{len(mk)}; lean |U-D|/max median {st.median(lean):.2f}")
    agree = [((w[s]["UP"] > w[s]["DOWN"]) == (b[s]["UP"] > b[s]["DOWN"])) for s in both]
    if agree:
        L.append(f"- markets both traded: the bigger side agreed in {sum(agree)}/{len(agree)}")
    L += ["", "## Per market (both traded)", "", "| market (UTC) | won | wallet U/D, cost, P&L | bot U/D, cost, P&L |", "|---|---|---|---|"]
    for s in both[-40:]:
        L.append(f"| {fmt_t(int(s.rsplit('-', 1)[1]))} | {'UP' if up[s] == 1 else 'DOWN'} | {w[s]['UP']:.0f}/{w[s]['DOWN']:.0f}, "
                 f"{w[s]['cost']:.0f}, {res_w[s]:+.2f} | {b[s]['UP']:.1f}/{b[s]['DOWN']:.1f}, {b[s]['cost']:.2f}, {res_b[s]:+.2f} |")
    text = "\n".join(L)
    print(text)
    if a.out:
        a.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
