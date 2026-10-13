"""BOOK_MOMENTUM: what happened after the bot's own signals (data/signal_outcomes.<tag>.jsonl).

Every evaluated signal -- traded or skipped -- carries the token's bid / ask 5, 10, 20 and 45 s later. This measures,
per signal episode (one per token per 5 s):

    continuation   token mid 5 / 10 s later minus the mid at the signal (what the hold can catch)
    scalp          bid h s later - entry ask - both taker fees (the plan's short taker round trip)
    hold           settlement payout - entry ask - entry fee (the same buy held to the close)
    maker          a bid at the signal's bid that the ask came down to within 10 s: payout - bid (no fee)

Outcomes come from gamma (closed=true), cached next to the output. Not a replay of the strategy: the bot's own records.

    python scripts/bookmom_signal_study.py [--tag bookmom-paper] [--files a.jsonl b.jsonl] [--out results/x.md]
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics as st
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GAMMA = "https://gamma-api.polymarket.com/markets?closed=true&limit=100&"


def fee(p: float) -> float:
    return 0.07 * p * (1.0 - p)


def outcomes(slugs: set[str], cache: Path) -> dict[str, float]:
    known = json.loads(cache.read_text()) if cache.exists() else {}
    todo = sorted(s for s in slugs if s not in known)
    for i in range(0, len(todo), 40):
        url = GAMMA + "&".join("slug=" + s for s in todo[i:i + 40])
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        for m in json.load(urllib.request.urlopen(req, timeout=30)):
            px, names = json.loads(m.get("outcomePrices") or "[]"), json.loads(m.get("outcomes") or "[]")
            if m.get("closed") and px and set(px) <= {"0", "1"}:      # resolved 1/0 only, never a pre-resolution price
                known[m["slug"]] = dict(zip(names, map(float, px))).get("Up")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(known))
    return known


def episodes(files: list[Path]) -> list[dict]:
    last: dict[str, float] = {}
    rows = []
    for f in files:
        for line in f.open():
            r = json.loads(line)
            if r.get("event") != "signal" or r.get("bid") is None or r.get("ask") is None:
                continue
            if r["token_id"] in last and r["ts"] - last[r["token_id"]] < 5000:
                continue
            last[r["token_id"]] = r["ts"]
            r["entry"] = (r.get("arrival") or {}).get("ask") or r["ask"]
            rows.append(r)
    return rows


def mean_se(v: list[float]) -> str:
    if len(v) < 6:
        return "n/a"
    return f"{100 * st.mean(v):+.2f}c ±{100 * st.pstdev(v) / len(v) ** 0.5:.2f}"


def line(label: str, rs: list[dict], up: dict) -> str:
    cont, scalp, hold, maker = collections.defaultdict(list), collections.defaultdict(list), [], []
    for r in rs:
        h, mid0 = r.get("horizons") or {}, (r["ask"] + r["bid"]) / 2
        for k in ("5", "10"):
            x = h.get(k) or {}
            if x.get("bid") is not None and x.get("ask") is not None:
                cont[k].append((x["bid"] + x["ask"]) / 2 - mid0)
                scalp[k].append(x["bid"] - r["entry"] - fee(r["entry"]) - fee(x["bid"]))
        u = up.get(r["slug"])
        if u is None:
            continue
        win = u if r["side"] == "UP" else 1.0 - u
        hold.append(win - r["entry"] - fee(r["entry"]))
        if any((h.get(k) or {}).get("ask") is not None and h[k]["ask"] <= r["bid"] + 1e-9 for k in ("5", "10")):
            maker.append(win - r["bid"])
    return (f"| {label} | {len(rs)} | {mean_se(cont['5'])} | {mean_se(cont['10'])} | {mean_se(scalp['5'])} | "
            f"{mean_se(scalp['10'])} | {mean_se(hold)} | {mean_se(maker)} ({len(maker)}) |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="bookmom-paper")
    ap.add_argument("--files", nargs="*", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    files = a.files or [ROOT / "data" / f"signal_outcomes.{a.tag}.jsonl"]
    rows = episodes(files)
    up = outcomes({r["slug"] for r in rows}, ROOT / "data" / "replay" / "bookmom_outcomes.json")
    out = [f"# Book-momentum signal outcomes ({', '.join(f.name for f in files)})", "",
           f"{len(rows)} signal episodes (one per token per 5 s), {len({r['slug'] for r in rows})} markets, "
           f"{sum(r['slug'] in up for r in rows)} with a resolved outcome. Entry = the ask at order arrival.", "",
           "| slice | n | mid +5 s | mid +10 s | scalp 5 s | scalp 10 s | hold to close | maker bid filled, held (n) |",
           "|---|---:|---:|---:|---:|---:|---:|---:|", line("all", rows, up)]
    for lo, hi in ((0.015, 0.025), (0.025, 0.045), (0.045, 0.075), (0.075, 1.0)):
        out.append(line(f"rise {lo * 100:g}-{hi * 100:g}c", [r for r in rows if lo <= (r.get("momentum_z") or 0) / 100 < hi], up))
    for lo, hi in ((0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.0)):
        out.append(line(f"ask {lo:.2f}-{hi:.2f}", [r for r in rows if lo <= r["entry"] < hi], up))
    for lo, hi in ((30, 90), (90, 180), (180, 300)):
        out.append(line(f"{lo}-{hi} s left", [r for r in rows if lo <= r["secs_left"] < hi], up))
    out += ["", "± is one standard error per episode (episodes in one market are correlated: the real error is larger)."]
    text = "\n".join(out)
    print(text)
    if a.out:
        a.out.write_text(text + "\n")


if __name__ == "__main__":
    main()
