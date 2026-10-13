"""Strategy simulator for the paired-exit study: structures A-F on one execution model (pe_lib.Exec).

    A  equal UP/DOWN shares bought at the signal, held to settlement
    B  today's triangle rule: entry model -> first token, resting pair BUY of the other side at
       floor(pair_target - fill) (strict), first leg held to settlement whether or not the pair fills
    C  first token on the fair-value EV gate -> independent stop / max hold / thesis / time exit, no pair
    D  C + take-profit ladder (resting asks, or taker sells)
    E  D + OPTIONAL resting pair buy of the other side: filled -> the remaining shares are locked and held,
       not filled -> the first leg's own exits run as in D (the pair never decides the first leg's fate)
    F  equal-share pair + directional inventory: sell the predicted loser down to a target ratio set by the
       model's confidence, then manage the winner as in D

Every leg is simulated forward from its fill, so the window's exposure book knows when each leg ends.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

import pe_lib as L

TICK = 0.01


def up_tick(x):
    return round(math.ceil(x / TICK - 1e-6) * TICK, 2)


def dn_tick(x):
    return round(math.floor(x / TICK + 1e-6) * TICK, 2)


@dataclass(frozen=True)
class Params:
    name: str = "C"
    structure: str = "C"
    # ---- entry
    entry: str = "fair"            # "fair" (the paired-exit gate) | "old" (today's triangle entry model)
    k_sig: float = 2.0             # |1 s spot z| that makes a candidate
    signal: str = "crypto"         # what makes a candidate: "crypto" (spot z) | "book" (Polymarket book model,
                                   # w.F["book"]) | "both" (crypto confirmed by the book) | "either" (first of the two)
    k_book: float = 1.0            # |book model| (predicted next-1 s UP mid move, cents) that makes a book candidate
    book_confirm: float = 0.0      # "both": the book toward the side >= this somewhere in the last confirm_ms
    confirm_ms: float = 1000.0
    min_agree: float = -1.0        # cross-exchange agreement toward the side (-1 = off)
    min_edge: float = 0.02         # fair - ask - fee - cost_c must be >= this
    min_conf: float = 0.0          # fair probability of the side >= this
    cost_c: float = 0.0            # slippage + adverse selection + exit cost per share (from TRAIN)
    lo: float = 0.15
    hi: float = 0.85
    min_left: float = 60.0
    max_left: float = 290.0
    late_left: float = 180.0       # under this many seconds left ...
    late_edge_add: float = 0.0     # ... the edge must be this much larger
    limit_off: float = 0.02
    shares: float = 5.0
    # ---- exposure (USDC at the order's limit)
    max_window_usdc: float = 10.0
    max_unpaired_usdc: float = 10.0
    max_dir_usdc: float = 10.0
    max_open: int = 1
    max_buys: int = 3
    # ---- exits (None = off)
    stop: float | None = 0.10      # bid <= fill - stop
    max_hold_s: float | None = None
    thesis: str | None = None      # "fair": fair < fill - thesis_drop | "mom": spot reversal with agreement | "both"
    thesis_drop: float = 0.10
    k_rev: float = 2.0
    time_exit_s: float | None = None   # left <= this -> sell ...
    settle_keep: float = 0.95          # ... unless the bid is >= this (a near-certain winner settles)
    stop_cross: float = 0.02
    tp_offsets: tuple = ()             # ladder: levels fill + offset
    tp_fracs: tuple = ()
    tp_mode: str = "maker"             # "maker" resting asks | "taker" sell at the bid at the level
    tp_fair: float | None = None       # dynamic: level = max(fill + offset, fair + tp_fair)
    # ---- pair (B, E)
    pair_target: float | None = None   # resting buy of the other side at floor(target - fill), strict
    # ---- F
    f_ratio: tuple = ((0.5, 1.0), (0.6, 0.6), (0.7, 0.2), (0.75, 0.05), (1.0, 0.0))
    # ---- B (today's rule)
    old_ev: float = 0.02
    old_cap_market: float = 50.0


def candidates(w, k_sig, min_left, max_left, signal="crypto", k_book=1.0, confirm=0.0, confirm_ms=1000.0):
    """(index, direction) of every trigger moment, one per direction per second. ``signal``: "crypto" |z1| >= k_sig;
    "book" |w.F["book"]| >= k_book; "both" a crypto trigger whose direction the book showed (>= confirm) within
    the last confirm_ms; "either" whichever fires."""
    z1 = w.F["z1"]
    win = (w.left >= min_left) & (w.left <= max_left)
    if signal == "crypto":
        x, m = z1, (np.abs(z1) >= k_sig) & win
    elif signal == "book":
        x = w.F["book"]
        m = (np.abs(np.nan_to_num(x)) >= k_book) & win
    elif signal == "both":
        bk = np.nan_to_num(w.F["book"])
        lo = np.searchsorted(w.T, w.T - confirm_ms, side="left")
        x = z1
        m = np.zeros(w.n, dtype=bool)
        for i in np.flatnonzero((np.abs(z1) >= k_sig) & win):
            d = 1.0 if z1[i] > 0 else -1.0
            m[i] = np.max(bk[lo[i]:i + 1] * d) >= confirm
    elif signal == "either":
        bk = np.nan_to_num(w.F["book"])
        zc = np.nan_to_num(z1)
        x = np.where(np.abs(zc) >= k_sig, zc, bk)
        m = ((np.abs(zc) >= k_sig) | (np.abs(bk) >= k_book)) & win
    else:
        raise ValueError(signal)
    out, last = [], {1: -1e18, -1: -1e18}
    for i in np.flatnonzero(m):
        d = 1 if x[i] > 0 else -1
        if w.T[i] - last[d] >= 1000.0:
            last[d] = w.T[i]
            out.append((int(i), d))
    return out


# ------------------------------------------------------------------------------- one leg's life


def first_true(mask, k0):
    if k0 >= len(mask):
        return None
    sub = mask[k0:]
    if not sub.any():
        return None
    return k0 + int(np.argmax(sub))


def leg_life(w, P, prm: Params, ex: L.Exec, side, px, S, t0, pair=None):
    """Exits of one leg of S shares bought at px, its result known at t0. ``pair``: (other side, price)
    of a resting buy placed at t0 (E / B). Returns a dict: exits, pnl, end time, flags."""
    d = 1 if side == "UP" else -1
    bid = ex.bid_arr(w, side)
    fair = P if d > 0 else 1.0 - P
    k0 = int(np.searchsorted(w.T, t0, side="left"))
    n = w.n
    rec = {"exits": [], "paired": False, "settled_shares": 0.0, "kind": None}
    remaining = S
    cash = -S * px - S * L.fee(px)
    # ---- the full-exit trigger (first of stop / thesis / max hold / time)
    triggers = []
    if prm.structure not in ("A", "B"):
        valid = ~np.isnan(bid)
        if prm.stop is not None:
            k = first_true(valid & (bid <= px - prm.stop + 1e-9), k0)
            if k is not None:
                triggers.append((w.T[k], "STOP_LOSS"))
        if prm.thesis in ("fair", "both"):
            k = first_true(fair < px - prm.thesis_drop, k0)
            if k is not None:
                triggers.append((w.T[k], "THESIS_INVALIDATED"))
        if prm.thesis in ("mom", "both"):
            ag = w.F["agree"]
            k = first_true((w.F["z1"] * d <= -prm.k_rev) & (np.nan_to_num(ag) * d <= -0.5), k0)
            if k is not None:
                triggers.append((w.T[k], "THESIS_INVALIDATED"))
        if prm.max_hold_s is not None:
            t = t0 + prm.max_hold_s * 1000.0
            if t < w.close - 1000.0:
                triggers.append((t, "TIME_EXIT"))
        if prm.time_exit_s is not None:
            k = first_true((w.left <= prm.time_exit_s) & valid & (bid < prm.settle_keep), k0)
            if k is not None:
                triggers.append((w.T[k], "TIME_EXIT"))
    t_exit, kind_exit = min(triggers) if triggers else (None, None)
    # ---- ladder fills before the trigger
    ladder = []
    if prm.tp_offsets:
        live_k = int(np.searchsorted(w.T, t0 + ex.lat, side="left"))
        fr = prm.tp_fracs or tuple(1.0 / len(prm.tp_offsets) for _ in prm.tp_offsets)
        alloc, left_sh = [], S
        for j, f in enumerate(fr):
            sh = S - sum(alloc) if j == len(fr) - 1 else round(S * f)
            alloc.append(sh)
        for off, sh in zip(prm.tp_offsets, alloc):
            if sh <= 0:
                continue
            lvl = np.full(n, up_tick(px + off))
            if prm.tp_fair is not None:
                lvl = np.maximum(lvl, np.ceil((fair + prm.tp_fair) / TICK - 1e-6) * TICK)
            lvl = np.minimum(lvl, 0.99)
            if prm.tp_mode == "maker":
                m = (bid >= lvl - 1e-9) if ex.touch_fill else (bid > lvl + 1e-9)
            else:
                m = bid >= lvl - 1e-9
            m &= ~np.isnan(bid)
            k = first_true(m, live_k)
            if k is not None:
                ladder.append((w.T[k], float(lvl[k]), sh, k))
        ladder.sort()
    # ---- the optional pair (resting buy of the other side)
    t_pair = pp = None
    if pair is not None:
        other, pp = pair
        if pp is not None and pp >= TICK - 1e-9:
            t_end = t_exit if t_exit is not None else w.close
            t_pair = ex.resting_buy_fill(w, other, t0 + ex.lat, pp, t_end)
    # ---- walk the events in time order
    events = [(t, "TP", lv, sh, k) for t, lv, sh, k in ladder]
    if t_pair is not None:
        events.append((t_pair, "PAIR", pp, None, None))
    if t_exit is not None:
        events.append((t_exit, kind_exit, None, None, None))
    events.sort(key=lambda e: e[0])
    t_end = None
    for t, kind, price, sh, _k in events:
        if remaining <= 1e-9:
            break
        if kind == "TP":
            if prm.tp_mode == "maker":
                q = min(sh, remaining)
                cash += q * price
                rec["exits"].append(("TAKE_PROFIT", q, price, 0.0, t))
                remaining -= q
            else:
                q = min(sh, remaining)
                fpx, known = ex.taker_sell(w, side, t, price - TICK)
                if fpx is None:
                    continue
                cash += q * fpx - q * L.fee(fpx)
                rec["exits"].append(("TAKE_PROFIT", q, fpx, q * L.fee(fpx), known))
                remaining -= q
            t_end = t
        elif kind == "PAIR":
            if remaining + 1e-9 < 5.0:
                continue                      # under the venue minimum: no pair possible
            q = remaining
            cash += q * 1.0 - q * price       # locked: pays 1 whichever side wins; maker fee 0
            rec["exits"].append(("PAIR_ORDER_FILLED", q, price, 0.0, t))
            rec["paired"] = True
            remaining = 0.0
            t_end = t
        else:
            fpx, known, _tries = L.stop_sell(ex, w, side, t, cross=prm.stop_cross)
            if fpx is not None:
                cash += remaining * fpx - remaining * L.fee(fpx)
                rec["exits"].append((kind, remaining, fpx, remaining * L.fee(fpx), known))
                remaining = 0.0
                t_end = known
            break
    if remaining > 1e-9:
        won = w.outcome == side
        cash += remaining * (1.0 if won else 0.0)
        rec["exits"].append(("SETTLE_WIN" if won else "SETTLE_LOSS", remaining, 1.0 if won else 0.0, 0.0, w.close))
        rec["settled_shares"] = remaining
        t_end = w.close
    rec["pnl"] = cash
    rec["t_end"] = t_end if t_end is not None else w.close
    rec["kind"] = rec["exits"][-1][0] if rec["exits"] else "SETTLE"
    rec["fees"] = S * L.fee(px) + sum(e[3] for e in rec["exits"])
    return rec


# ------------------------------------------------------------------------------- old entry (B)


def old_features(w, i, side, sigma):
    d = 1 if side == "UP" else -1
    lp = np.log(w.spot) * 1e4
    T = w.T
    j1 = w.ix(T[i] - 1000.0); j3 = w.ix(T[i] - 3000.0)
    if j1 < 0 or j3 < 0:
        return None
    r1, r3 = lp[i] - lp[j1], lp[i] - lp[j3]
    ask = (w.ua if d > 0 else w.da)[i]
    bid = (w.ub if d > 0 else w.db)[i]
    a_arr = w.ua if d > 0 else w.da
    a1, a3 = a_arr[j1], a_arr[j3]
    dep = (w.uad if d > 0 else w.dad)[i]
    left = w.left[i]
    te = max(left - 40.0, 1.0)
    fv_up = L.phi((lp[i] - math.log(w.strike) * 1e4) / (sigma * math.sqrt(te))) if w.strike > 0 else 0.5
    fv = fv_up if d > 0 else 1 - fv_up
    ua, da = w.ua[i], w.da[i]
    nz = lambda x: 0.0 if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)  # noqa: E731
    return {"z1": r1 / sigma * d, "z3": r3 / (sigma * math.sqrt(3.0)) * d, "accel": (r1 - r3 / 3.0) * d / sigma,
            "tfi15": nz(w.tfi[i]) * d, "obi": nz(w.obi[i]) * d, "ofi": nz(w.ofi[i]) * d,
            "tok_vel1": 0.0 if math.isnan(a1) else (ask - a1) * 100.0, "tok_vel3": 0.0 if math.isnan(a3) else (ask - a3) * 100.0,
            "depth_ask": math.log1p(nz(dep)), "spread": 0.0 if math.isnan(bid) else (ask - bid) * 100.0,
            "left_frac": left / 300.0, "ask": ask, "fv_gap": fv - ask,
            "pressure": 0.0 if math.isnan(ua) or math.isnan(da) else (ua + da - 1.0) * 100.0, "hz": 0.0}


def old_score(lock, f):
    mdl, feats = lock["model"], lock["features"]
    z = mdl["w"][0]
    for k, mu, sd, wt in zip(feats, mdl["mu"], mdl["sd"], mdl["w"][1:]):
        x = f.get(k)
        if x is None or x != x:
            x = mu
        z += wt * (x - mu) / sd
    p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
    e = min(f["ask"] + 0.01, 0.96)
    return p, p - e - L.fee(e)


# ------------------------------------------------------------------------------- one window


def run_window(w, P, prm: Params, ex: L.Exec, old_lock=None):
    """All legs of one window under ``prm``. Returns (window pnl, list of leg records)."""
    legs = []
    if prm.entry == "old":
        cands = candidates(w, 1.0, max(prm.min_left, 60.0), prm.max_left)
    else:
        cands = candidates(w, prm.k_sig, prm.min_left, prm.max_left, prm.signal, prm.k_book, prm.book_confirm,
                           prm.confirm_ms)
    busy = -1e18
    spent = 0.0
    buys = 0
    open_legs = []          # (t_end, side, usdc, paired_at)
    for i, d in cands:
        t = w.T[i]
        if t < busy:
            continue
        side = "UP" if d > 0 else "DOWN"
        other = "DOWN" if d > 0 else "UP"
        ask = ex.seen(w, ex.ask_arr(w, side), t)
        if ask is None or not (prm.lo <= ask <= prm.hi):
            continue
        fair = float(P[i] if d > 0 else 1.0 - P[i])
        left = w.left[i]
        if prm.entry == "old":
            f = old_features(w, i, side, w.sigma)
            if f is None:
                continue
            p_old, ev_old = old_score(old_lock, f)
            if ev_old < prm.old_ev:
                continue
            edge = ev_old
        else:
            ag = w.F["agree"][i]
            if prm.min_agree > -1.0 and (math.isnan(ag) or ag * d < prm.min_agree):
                continue
            need = prm.min_edge + (prm.late_edge_add if left < prm.late_left else 0.0)
            edge = fair - ask - L.fee(ask) - prm.cost_c
            if edge < need or fair < prm.min_conf:
                continue
        limit = min(round(ask + prm.limit_off, 2), 0.99)
        if prm.structure == "B" and prm.pair_target is not None:
            limit = min(limit, round(prm.pair_target - 2 * TICK, 2))
            if ask > limit + 1e-9:
                continue
        stake = prm.shares * limit * (2.0 if prm.structure in ("A", "F") else 1.0)
        open_now = [o for o in open_legs if o[0] > t]
        unpaired = sum(o[2] for o in open_now if not o[3] or o[3] > t)
        direction = sum((o[2] if o[1] == "UP" else -o[2]) for o in open_now if not o[3] or o[3] > t)
        if prm.structure == "B":
            if spent + stake > prm.old_cap_market + 1e-9:
                break
        else:
            if buys >= prm.max_buys or len(open_now) >= prm.max_open:
                continue
            if spent + stake > prm.max_window_usdc + 1e-9:
                continue
            if prm.structure not in ("A",) and unpaired + prm.shares * limit > prm.max_unpaired_usdc + 1e-9:
                continue
            new_dir = direction + (prm.shares * limit if d > 0 else -prm.shares * limit)
            if prm.structure not in ("A",) and abs(new_dir) > prm.max_dir_usdc + 1e-9 and abs(new_dir) > abs(direction):
                continue
        meta = {"asset": w.asset, "slug": w.slug, "split": w.split, "day": w.day, "side": side, "t": t,
                "left": float(left), "ask": float(ask), "fair": fair, "edge": float(edge), "z1": float(w.F["z1"][i]),
                "sigma": w.sigma, "agree": float(np.nan_to_num(w.F["agree"][i]))}
        if prm.structure in ("A", "F"):
            ask_o = ex.seen(w, ex.ask_arr(w, other), t)
            if ask_o is None:
                continue
            p1, k1 = ex.taker_buy(w, side, t, limit)
            p2, k2 = ex.taker_buy(w, other, t, min(round(ask_o + prm.limit_off, 2), 0.99))
            busy = max(k1, k2)
            buys += 1
            if p1 is None and p2 is None:
                continue
            S = prm.shares
            spent += S * ((p1 or 0) + (p2 or 0))
            if prm.structure == "A" or p1 is None or p2 is None:
                # A: held to settlement; F with one leg missing: the filled leg runs its own exits (C rules)
                for s_, p_, k_ in ((side, p1, k1), (other, p2, k2)):
                    if p_ is None:
                        continue
                    if prm.structure == "A":
                        won = w.outcome == s_
                        r = {"exits": [("SETTLE_WIN" if won else "SETTLE_LOSS", S, 1.0 if won else 0.0, 0.0, w.close)],
                             "pnl": S * ((1.0 if won else 0.0) - p_ - L.fee(p_)), "t_end": w.close,
                             "paired": p1 is not None and p2 is not None, "settled_shares": S,
                             "kind": "SETTLE_WIN" if won else "SETTLE_LOSS", "fees": S * L.fee(p_)}
                    else:
                        r = leg_life(w, P, replace(prm, structure="C"), ex, s_, p_, S, k_)
                    r.update(meta, side=s_, px=p_, shares=S, t_fill=k_, pair_trade=True)
                    legs.append(r)
                    open_legs.append((r["t_end"], s_, S * p_, r["t_end"] if r["paired"] else None))
                continue
            # F: both filled. Target losing-side ratio from the model's confidence in the predicted winner.
            xs, ys = zip(*prm.f_ratio)
            ratio = float(np.interp(fair, xs, ys))
            keep = 5.0 * round(S * ratio / 5.0)
            sell = S - keep
            cash_loser = -S * p2 - S * L.fee(p2)
            loser_exits = []
            if sell >= 5.0:
                fpx, known, _ = L.stop_sell(ex, w, other, k2, cross=prm.stop_cross)
                if fpx is not None:
                    cash_loser += sell * fpx - sell * L.fee(fpx)
                    loser_exits.append(("INVENTORY_SELL", sell, fpx, sell * L.fee(fpx), known))
                else:
                    keep = S
            # the winner: as D; the kept losing shares are locked against `keep` winner shares
            rwin = leg_life(w, P, replace(prm, structure="D"), ex, side, p1, S, k1)
            won_o = w.outcome == other
            cash_loser += keep * (1.0 if won_o else 0.0)
            if keep:
                loser_exits.append(("SETTLE_WIN" if won_o else "SETTLE_LOSS", keep, 1.0 if won_o else 0.0, 0.0, w.close))
            rl = {"exits": loser_exits, "pnl": cash_loser, "t_end": w.close, "paired": keep > 0,
                  "settled_shares": keep, "kind": loser_exits[-1][0] if loser_exits else "SETTLE", "fees": S * L.fee(p2)
                  + sum(e[3] for e in loser_exits)}
            for r, s_, p_, k_ in ((rwin, side, p1, k1), (rl, other, p2, k2)):
                r.update(meta, side=s_, px=p_, shares=S, t_fill=k_, pair_trade=True)
                legs.append(r)
            open_legs.append((rwin["t_end"], side, S * p1, None))
            continue
        # ---- single first token (B, C, D, E)
        px, known = ex.taker_buy(w, side, t, limit)
        busy = known
        buys += 1
        if px is None:
            legs.append({**meta, "filled": False})
            continue
        S = prm.shares
        spent += S * px
        pair = None
        if prm.pair_target is not None and prm.structure in ("B", "E"):
            pp = dn_tick(prm.pair_target - px - 1e-9) if dn_tick(prm.pair_target - px - 1e-9) + px < prm.pair_target - 1e-9 \
                else round(dn_tick(prm.pair_target - px - 1e-9) - TICK, 2)
            pair = (other, pp if pp >= TICK else None)
        r = leg_life(w, P, prm, ex, side, px, S, known, pair=pair)
        r.update(meta, px=px, shares=S, t_fill=known, filled=True, pair_trade=False)
        legs.append(r)
        t_pair = next((e[4] for e in r["exits"] if e[0] == "PAIR_ORDER_FILLED"), None)
        open_legs.append((r["t_end"], side, S * px, t_pair))
    pnl = sum(r.get("pnl", 0.0) for r in legs if r.get("filled", True))
    return pnl, legs
