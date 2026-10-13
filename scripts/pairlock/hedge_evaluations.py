"""Every hedge evaluation of tpb-pairlock-paper, from its own records (read-only).

The bot keeps no file of its evaluations: each one is a log line (pm2 error log) carrying the position at that moment,
the trigger and, on the outcome line, the best candidate hedge and why it was rejected. Repeated rejections of the same
kind inside 30 s are folded into a count on the next printed line (the engine's REPEAT_LOG_MS); they are reported as a
count, not itemised. v1 (2026-10-01 20:50 -> 10-02 01:39) evaluated silently every tick and logged only the hedges it
sent; those are listed with what v1 logged.

Market prices at each evaluation come from the 200 ms market record (data/charts/<slug>.json, else the live history),
the last sample at or before the end of the evaluation's second. The candidate's price is backed out exactly from the
engine's own numbers: hedge_EV = P(fill) * q * (P(opposite wins) - VWAP - fee(VWAP)), with P(UP) backed out of the
logged expected P&L, so the after-hedge figures are the engine's, not an approximation of its book walk.

usage: hedge_evaluations.py [--out results/pairlock_hedge_evaluations.csv]
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from bisect import bisect_right
from datetime import datetime, timezone

ROOT = "/root/troll-poly-bot"
LOG = os.path.expanduser("~/.pm2/logs/tpb-pairlock-paper-error.log")
MODEL = f"{ROOT}/data/replay/pairlock2_model.json"
FEE = 0.07
V2_START = "2026-10-02T01:39:04"


def fee(p: float) -> float:
    return FEE * p * (1.0 - p) if 0.0 < p < 1.0 else 0.0


def num(x):
    x = x.strip()
    return None if x in ("-", "None", "") else float(x)


RX_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d): .*?\[PAIRLOCK\] (\w+) (btc-updown-5m-\d+) \| (.*)$")
RX_POS = re.compile(r"UP ([\d.]+) DOWN ([\d.]+) matched ([\d.]+) unmatched UP ([\d.]+) DOWN ([\d.]+)")
RX_AVG = re.compile(r"avg UP (\S+) DOWN (\S+)")
RX_PAIR = re.compile(r"pair cost (\S+) edge (\S+) net (\S+) locked (\S+)")
RX_FEES = re.compile(r"fees (\S+) slip (\S+)")
RX_EXP = re.compile(r"expected (\S+) worst (\S+) MTM (\S+)")
RX_CAP = re.compile(r"capital (\S+)")
RX_BEST = re.compile(r"best candidate (\w+) ([\d.]+) sh VWAP (\S+) hedge_EV (\S+)")
RX_FOLD = re.compile(r"(\d+) similar rejected evaluations since the last line")
RX_REJ = re.compile(r"best hedge_EV ([+-][\d.]+) \((\w+) ([\d.]+) sh\) <= MIN_HEDGE_EV ([+-][\d.]+)")
RX_ACC = re.compile(r"(\w+): ([\d.]+) sh of (UP|DOWN) at VWAP ([\d.]+), hedge_EV ([+-][\d.]+)")
RX_FILL = re.compile(r"\[PAIRLOCK\] HEDGE_FILLED (btc-updown-5m-\d+) ([\d.]+) (UP|DOWN) @ ([\d.]+)")
RX_KILL = re.compile(r"\[PAIRLOCK\] HEDGE_NOT_FILLED (btc-updown-5m-\d+) \(([^)]*)\)")


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000.0


def parse_state(body: str) -> dict:
    parts = body.split(" | ")
    d = {"from": parts[0].split(" -> ")[0], "to": parts[0].split(" -> ")[1]}
    m = RX_POS.search(body)
    d.update(up=float(m[1]), down=float(m[2]), matched=float(m[3]), un_up=float(m[4]), un_down=float(m[5]))
    m = RX_AVG.search(body)
    d.update(avg_up=num(m[1]), avg_down=num(m[2]))
    m = RX_PAIR.search(body)
    d.update(pair_cost=num(m[1]), pair_edge=num(m[2]), net_edge=num(m[3]), locked=num(m[4]))
    m = RX_FEES.search(body)
    d.update(fees=num(m[1]), slip=num(m[2]))
    m = RX_EXP.search(body)
    d.update(expected=num(m[1]), worst=num(m[2]), mtm=num(m[3]))
    d["capital"] = num(RX_CAP.search(body)[1])
    if d["worst"] is not None:               # 3 decimals: worst = min(UP, DOWN) - total cost
        d["capital"] = round(min(d["up"], d["down"]) - d["worst"], 4)
    # the free-text reason is the field after "capital ..."
    idx = next(i for i, p in enumerate(parts) if p.startswith("capital "))
    d["reason"] = parts[idx + 1].strip() if idx + 1 < len(parts) else ""
    m = RX_BEST.search(body)
    d["best"] = None if m is None else {"label": m[1], "q": float(m[2]), "vwap": num(m[3]), "ev": float(m[4])}
    m = RX_FOLD.search(body)
    d["folded"] = int(m[1]) if m else 0
    return d


class Market:
    """The 200 ms record of one market: chart archive, else the live history file."""
    cache: dict = {}

    @classmethod
    def get(cls, slug: str):
        if slug in cls.cache:
            return cls.cache[slug]
        pts, outcome = [], None
        p = f"{ROOT}/data/charts/{slug}.json"
        if os.path.exists(p):
            d = json.load(open(p))
            pts, outcome = d.get("points") or [], (d.get("outcome") or "").upper() or None
        else:
            for h in (f"{ROOT}/data/live_state.pairlock-btc-paper.history/{slug}.jsonl",):
                if os.path.exists(h):
                    pts = [json.loads(x) for x in open(h)]
        m = cls()
        m.t = [x["t"] for x in pts]
        m.pts = pts
        m.outcome = outcome
        cls.cache[slug] = m
        return m

    def at(self, t_ms: float) -> dict | None:
        i = bisect_right(self.t, t_ms + 999.0) - 1
        return self.pts[i] if i >= 0 and t_ms + 999.0 - self.t[i] < 5000.0 else None


def book(pt: dict | None, side: str) -> tuple:
    """(bid, ask, ask depth within 2c) of a token in one sample. The books are mirrored: DOWN asks = UP bids."""
    if pt is None:
        return None, None, None
    if side == "UP":
        return pt.get("ub"), pt.get("ua"), pt.get("uad")
    return pt.get("db"), pt.get("da"), pt.get("dad") if pt.get("dad") is not None else pt.get("ubd")


def after_hedge(st: dict, light: str, q: float, v: float, p_up: float) -> dict:
    """The engine's post-hedge numbers (pairlock_book.evaluate_hedges) for q shares of ``light`` at VWAP v."""
    f = fee(v)
    up = st["up"] + (q if light == "UP" else 0.0)
    dn = st["down"] + (q if light == "DOWN" else 0.0)
    tc = st["capital"] + q * (v + f)
    m = min(up, dn)
    # gross and fees per side, fees allocated to the matched pairs pro rata (as the engine)
    g = {"UP": (st["avg_up"] or 0.0) * st["up"], "DOWN": (st["avg_down"] or 0.0) * st["down"]}
    fz = {"UP": 0.0, "DOWN": 0.0}
    heavy = "DOWN" if light == "UP" else "UP"
    fz[heavy] = st["capital"] - g["UP"] - g["DOWN"] if st["down"] == 0 or st["up"] == 0 else None
    if fz[heavy] is None:   # both sides already held: split the logged fees by each side's fee rate on its average
        fu = fee(st["avg_up"]) * st["up"]
        fd = fee(st["avg_down"]) * st["down"]
        tot = st["capital"] - g["UP"] - g["DOWN"]
        fz = {"UP": tot * fu / (fu + fd), "DOWN": tot * fd / (fu + fd)}
    g[light] += q * v
    fz[light] += q * f
    qq = {"UP": up, "DOWN": dn}
    pc = None
    if m > 1e-9:
        avg = {s: g[s] / qq[s] for s in qq}
        pc = avg["UP"] + avg["DOWN"] + sum(fz[s] * m / qq[s] for s in qq) / m
    e1 = p_up * up + (1.0 - p_up) * dn - tc
    return {"up": up, "down": dn, "matched": m, "unmatched": abs(up - dn), "pair_cost": pc,
            "pair_edge": None if pc is None else 1.0 - pc, "expected": e1, "worst": min(up, dn) - tc,
            "total_cost": tc, "fee_per_share": f}


def solve_vwap(cost_per_share: float) -> float | None:
    """v such that v + fee(v) = c (fee 0.07 v (1-v)): the root in (0, 1)."""
    a, b, c = -FEE, 1.0 + FEE, -cost_per_share
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    for r in ((-b + disc ** 0.5) / (2 * a), (-b - disc ** 0.5) / (2 * a)):
        if 0.0 < r < 1.0:
            return r
    return None


def v2_rows(lines: list[str], ep: float) -> list[dict]:
    rows = []
    for i, line in enumerate(lines):
        m = RX_LINE.match(line)
        if not m or " -> HEDGE_EVALUATION |" not in m[4] or line[:19] < V2_START:
            continue
        t_str, _ev, slug, body = m[1], m[2], m[3], m[4]
        st = parse_state(body)
        out = None
        for j in range(i + 1, min(i + 40, len(lines))):
            mm = RX_LINE.match(lines[j])
            if mm and mm[3] == slug and mm[4].startswith("HEDGE_EVALUATION -> "):
                out = parse_state(mm[4])
                out["line"] = j
                break
        heavy = "UP" if st["up"] - st["down"] > 1e-9 else "DOWN"
        light = "DOWN" if heavy == "UP" else "UP"
        if abs(st["up"] - st["down"]) > 1e-9:
            p_up = (st["expected"] + st["capital"] - st["down"]) / (st["up"] - st["down"])
        else:
            p_up = None
        r = {"version": "v2", "time_utc": t_str.replace("T", " "), "market": slug, "trigger": st["reason"],
             "state_before": st["from"], "folded_similar_before": st["folded"],
             "initial_token": heavy, "initial_qty": st["up"] if heavy == "UP" else st["down"],
             "initial_avg_cost": st["avg_up"] if heavy == "UP" else st["avg_down"],
             "model_p_initial_wins": None if p_up is None else round(p_up if heavy == "UP" else 1 - p_up, 4),
             "before_up_qty": st["up"], "before_down_qty": st["down"], "before_total_cost": st["capital"],
             "before_expected_pnl": st["expected"], "before_worst_pnl": st["worst"]}
        mk = Market.get(slug)
        pt = mk.at(ts(t_str))
        hb, ha, _ = book(pt, heavy)
        ob, oa, od = book(pt, light)
        r.update(initial_token_bid=hb, initial_token_ask=ha, opposite_token=light, opposite_ask_record=oa,
                 opposite_depth_2c=od, opposite_depth_usable=None if od is None else round(od * 0.2, 1),
                 market_record="chart" if os.path.exists(f"{ROOT}/data/charts/{slug}.json") else (
                     "live history" if pt else "none"), settled_winner=mk.outcome)
        decision, q, v, hev, why, kind = None, None, None, None, "", ""
        if out is None:
            decision, why = "?", "outcome line not found"
        elif out["to"] == "NO_HEDGE":
            decision, why = "NO_HEDGE", out["reason"]
            mr = RX_REJ.search(why)
            if mr:
                hev, kind, q = float(mr[1]), mr[2], float(mr[3])
        else:
            decision = out["to"]
            ma = RX_ACC.search(out["reason"])
            if ma:
                kind, q, v, hev = ma[1], float(ma[2]), float(ma[4]), float(ma[5])
                if out.get("best") and out["best"]["vwap"] is not None and abs(out["best"]["q"] - q) < 1e-9:
                    v = out["best"]["vwap"]          # the 5-decimal VWAP of the chosen candidate
            why = ""
        if q and p_up is not None and v is None:
            p_light = p_up if light == "UP" else 1.0 - p_up
            v = solve_vwap(p_light - hev / (ep * q))
        r.update(decision=decision, candidate_label=kind, candidate_qty=q,
                 candidate_vwap=None if v is None else round(v, 4), hedge_ev=hev, reason_rejected=why)
        if q and v is not None and p_up is not None:
            a = after_hedge(st, light, q, v, p_up)
            r.update(after_up_qty=a["up"], after_down_qty=a["down"], after_matched_qty=a["matched"],
                     after_unmatched_qty=a["unmatched"],
                     after_pair_cost=None if a["pair_cost"] is None else round(a["pair_cost"], 4),
                     after_pair_edge=None if a["pair_edge"] is None else round(a["pair_edge"], 4),
                     after_expected_pnl=round(a["expected"], 4), after_worst_pnl=round(a["worst"], 4),
                     hedge_ev_check=round(ep * (a["expected"] - st["expected"]), 4))
            if mk.outcome in ("UP", "DOWN"):
                r["candidate_pnl_at_settlement"] = round(q * ((1.0 if mk.outcome == light else 0.0) - v - fee(v)), 4)
        # what happened to a sent hedge
        if decision in ("FULL_HEDGE", "PARTIAL_HEDGE"):
            fills, kill = [], None
            for j in range(out["line"] + 1, min(out["line"] + 400, len(lines))):
                mf = RX_FILL.search(lines[j])
                if mf and mf[1] == slug:
                    fills.append((float(mf[2]), float(mf[4])))
                mkill = RX_KILL.search(lines[j])
                if mkill and mkill[1] == slug:
                    kill = mkill[2]
                    break
                if fills and sum(x for x, _ in fills) >= (q or 0) - 1e-6:
                    break
            if fills:
                qf = sum(x for x, _ in fills)
                r["sent_result"] = f"filled {qf:g} sh @ {sum(x * y for x, y in fills) / qf:.4f}"
            else:
                r["sent_result"] = f"not filled ({kill})" if kill else "result not found"
        rows.append(r)
    return rows


RX_V1_SENT = re.compile(r"^(\S+): .*\[PAIRLOCK\] (btc-updown-5m-\d+) (UP|DOWN) hedge sent: ([\d.]+) sh FOK <= ([\d.]+) "
                        r"\((\w+), state \w+, unmatched ([\d.]+)\)")
RX_V1_SIG = re.compile(r"PAIRLOCK HEDGE (\w+) (UP|DOWN) ([\d.]+) sh at ask ([\d.]+) \(limit ([\d.]+)\); P\(B\) ([\d.]+), "
                       r"pair cost after ([\d.]+), reversal ([+-][\d.]+)")
RX_V1_FILL = re.compile(r"\[PAIRLOCK\] (btc-updown-5m-\d+) hedge filled: ([\d.]+) sh (UP|DOWN) @ ([\d.]+) -> (\w+), hedge ratio "
                        r"([\d.]+), pair cost (\S+), worst case ([+-][\d.]+)")
RX_V1_KILL = re.compile(r"\[PAIRLOCK\] (btc-updown-5m-\d+) hedge NOT filled \(([^)]*)\)")
RX_V1_POS = re.compile(r"\[POSITION\] BTC \S+ (UP|DOWN) open: ([\d.]+) sh @ ([\d.]+)")


def v1_rows(lines: list[str]) -> list[dict]:
    rows = []
    for i, line in enumerate(lines):
        if line[:19] >= V2_START:
            break
        m = RX_V1_SENT.match(line)
        if not m:
            continue
        t_str, slug, light, q, limit, kind, unmatched = m[1], m[2], m[3], float(m[4]), float(m[5]), m[6], float(m[7])
        heavy = "DOWN" if light == "UP" else "UP"
        sig = None
        for j in range(max(0, i - 5), min(i + 5, len(lines))):
            ms = RX_V1_SIG.search(lines[j])
            if ms and ms[2] == light:
                sig = ms
                break
        # the first leg: the last [POSITION] ... open line of the heavy side in this market before the hedge
        o = datetime.fromtimestamp(int(slug.rsplit("-", 1)[1]), tz=timezone.utc)
        label = f"BTC {o:%H:%M}-"
        entry = None
        for j in range(i - 1, max(0, i - 4000), -1):
            mp = RX_V1_POS.search(lines[j])
            if mp and mp[1] == heavy and label in lines[j]:
                entry = (float(mp[2]), float(mp[3]))
                break
            if "SETTLE " + slug in lines[j]:
                break
        mk = Market.get(slug)
        pt = mk.at(ts(t_str))
        hb, ha, _ = book(pt, heavy)
        ob, oa, od = book(pt, light)
        res = None
        for j in range(i + 1, min(i + 200, len(lines))):
            mf = RX_V1_FILL.search(lines[j])
            if mf and mf[1] == slug:
                res = f"filled {mf[2]} sh @ {mf[4]} -> {mf[5]}, pair cost {mf[7]}, worst case {mf[8]}"
                break
            mkl = RX_V1_KILL.search(lines[j])
            if mkl and mkl[1] == slug:
                res = f"not filled ({mkl[2]})"
                break
        qa, pa = (entry if entry else (unmatched, None))
        r = {"version": "v1", "time_utc": t_str.replace("T", " "), "market": slug,
             "trigger": "confirmed reversal" if kind == "loss" else "lock opportunity", "state_before": "ADVERSE_MOVE/MONITOR",
             "initial_token": heavy, "initial_qty": qa, "initial_avg_cost": pa,
             "initial_token_bid": hb, "initial_token_ask": ha, "opposite_token": light, "opposite_ask_record": oa,
             "opposite_depth_2c": od, "opposite_depth_usable": None if od is None else round(od * 0.2, 1),
             "market_record": "chart" if os.path.exists(f"{ROOT}/data/charts/{slug}.json") else "none",
             "settled_winner": mk.outcome, "decision": "FULL_HEDGE" if q >= unmatched - 1e-9 else "PARTIAL_HEDGE",
             "candidate_label": kind, "candidate_qty": q, "reason_rejected": "", "sent_result": res or "result not found"}
        if sig is not None:
            p_b, ask_b = float(sig[6]), float(sig[4])
            r.update(candidate_vwap=ask_b, model_p_initial_wins=round(1 - p_b, 4), after_pair_cost=float(sig[7]),
                     after_pair_edge=round(1 - float(sig[7]), 4))
            if pa is not None:
                tc0 = qa * (pa + fee(pa))
                p_h = 1 - p_b
                e0 = p_h * qa - tc0
                tc1 = tc0 + q * (ask_b + fee(ask_b))
                up1 = qa if heavy == "UP" else q
                dn1 = q if heavy == "UP" else qa
                p_up = p_h if heavy == "UP" else p_b
                e1 = p_up * up1 + (1 - p_up) * dn1 - tc1
                r.update(before_up_qty=qa if heavy == "UP" else 0.0, before_down_qty=qa if heavy == "DOWN" else 0.0,
                         before_total_cost=round(tc0, 4), before_expected_pnl=round(e0, 4), before_worst_pnl=round(-tc0, 4),
                         after_up_qty=up1, after_down_qty=dn1, after_matched_qty=min(up1, dn1),
                         after_unmatched_qty=abs(up1 - dn1), after_expected_pnl=round(e1, 4),
                         after_worst_pnl=round(min(up1, dn1) - tc1, 4), hedge_ev=round(e1 - e0, 4))
                if mk.outcome in ("UP", "DOWN"):
                    r["candidate_pnl_at_settlement"] = round(q * ((1.0 if mk.outcome == light else 0.0) - ask_b - fee(ask_b)), 4)
        rows.append(r)
    return rows


COLS = [  # (csv header, row key)
    ("version", "version"), ("time (UTC)", "time_utc"), ("market", "market"), ("trigger", "trigger"),
    ("state before", "state_before"),
    ("initial token", "initial_token"), ("initial quantity", "initial_qty"),
    ("initial average cost (price, excl. fees)", "initial_avg_cost"),
    ("current token bid", "initial_token_bid"), ("current token ask", "initial_token_ask"),
    ("model P(initial token wins)", "model_p_initial_wins"),
    ("opposite token", "opposite_token"),
    ("opposite executable ask: engine VWAP for the candidate", "candidate_vwap"),
    ("opposite best ask (200 ms market record)", "opposite_ask_record"),
    ("opposite depth within 2c (shares)", "opposite_depth_2c"),
    ("opposite depth usable after the 80% haircut", "opposite_depth_usable"),
    ("candidate", "candidate_label"), ("candidate hedge quantity", "candidate_qty"),
    ("before: UP qty", "before_up_qty"), ("before: DOWN qty", "before_down_qty"),
    ("before: total cost (incl. fees)", "before_total_cost"), ("before: expected P&L", "before_expected_pnl"),
    ("before: worst-case P&L", "before_worst_pnl"),
    ("after: UP qty", "after_up_qty"), ("after: DOWN qty", "after_down_qty"), ("after: matched qty", "after_matched_qty"),
    ("after: unmatched qty", "after_unmatched_qty"), ("after: pair cost (incl. fees)", "after_pair_cost"),
    ("after: pair edge (1 - pair cost)", "after_pair_edge"), ("after: expected P&L", "after_expected_pnl"),
    ("after: worst-case P&L", "after_worst_pnl"),
    ("hedge EV (logged)", "hedge_ev"), ("hedge EV recomputed (check)", "hedge_ev_check"),
    ("decision", "decision"), ("reason rejected", "reason_rejected"), ("sent hedge result", "sent_result"),
    ("similar rejected evaluations folded into this line", "folded_similar_before"),
    ("settled winner", "settled_winner"), ("candidate hedge P&L at settlement", "candidate_pnl_at_settlement"),
]


def main():
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else f"{ROOT}/results/pairlock_hedge_evaluations.csv"
    ep = float(json.load(open(MODEL))["exec"]["hedge_fill_rate"])
    lines = open(LOG, errors="replace").read().splitlines()
    rows = v1_rows(lines) + v2_rows(lines, ep)
    rows.sort(key=lambda r: r["time_utc"])
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([h for h, _ in COLS])
        for r in rows:
            w.writerow(["" if r.get(k) is None else r.get(k) for _, k in COLS])
    json.dump(rows, open(out.replace(".csv", ".json"), "w"), indent=1, default=str)
    print(f"{len(rows)} rows -> {out}  (P(fill) used for hedge_EV: {ep:.4f})")


if __name__ == "__main__":
    main()
