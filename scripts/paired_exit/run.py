"""Runner + metrics for the paired-exit study. Imported by the stage scripts (stage_*.py)."""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/root/troll-poly-bot/scripts/paired_exit")
import pe_lib as L   # noqa: E402
import sim as S      # noqa: E402

OLD_LOCK = json.load(open(f"{L.ROOT}/data/replay/triangle_entry_locked.json"))
SPLIT_NAMES = ("train", "validation", "test", "forward")


def load(asset, model=None):
    wins = L.prepare(L.load_asset(asset))
    model = model or json.load(open(f"{L.CACHE}/fair_{asset.lower()}.json"))
    for w in wins:
        w.fair_up = L.predict(model, L.design(w.F))
    return wins


def simulate(wins, prm, ex=None, splits=SPLIT_NAMES, feature_hook=None):
    """{split: (window pnl array in time order, legs, n windows)}"""
    ex = ex or L.Exec()
    out = {}
    for s in splits:
        pn, legs, n = [], [], 0
        for w in wins:
            if w.split != s:
                continue
            n += 1
            P = w.fair_up if feature_hook is None else feature_hook(w)
            p, lg = S.run_window(w, P, prm, ex, OLD_LOCK)
            pn.append(p)
            legs.extend(lg)
        out[s] = (np.array(pn), legs, n)
    return out


def metrics(pn, legs, n_windows):
    """Every figure the brief asks for (section 16), from window P&L and leg records."""
    att = [r for r in legs]
    fl = [r for r in legs if r.get("filled", True)]
    pnl = np.array([r["pnl"] for r in fl]) if fl else np.zeros(0)
    wins_ = pnl[pnl > 0]
    loss_ = pnl[pnl <= 0]
    eq = np.cumsum(pn) if len(pn) else np.zeros(1)
    dd = float(np.min(eq - np.maximum.accumulate(eq))) if len(pn) else 0.0
    kinds = defaultdict(int)
    for r in fl:
        kinds[r["kind"]] += 1
    nf = max(len(fl), 1)
    traded = pn[pn != 0] if len(pn) else pn
    std = float(np.std(pn)) if len(pn) > 1 else float("nan")
    se = std / math.sqrt(len(pn)) if len(pn) > 1 and std > 0 else float("nan")
    cvar = float(np.mean(np.sort(pn)[: max(1, len(pn) // 20)])) if len(pn) else 0.0
    trade_cvar = float(np.mean(np.sort(pnl)[: max(1, len(pnl) // 20)])) if len(pnl) else 0.0
    hold = [((r["t_end"] - r["t_fill"]) / 1000.0) for r in fl if r.get("t_fill") is not None]
    unp = [r for r in fl if not r.get("paired")]
    unp_loss = [r for r in unp if r["pnl"] < 0]
    pair_ok = [r for r in fl if r.get("paired")]
    fees = sum(r.get("fees", 0.0) for r in fl)
    slip = [r["px"] - r["ask"] for r in fl if r.get("px") is not None and r.get("ask") is not None]
    avg_w = float(wins_.mean()) if len(wins_) else 0.0
    avg_l = float(loss_.mean()) if len(loss_) else 0.0
    return {
        "windows": n_windows, "windows_traded": int((pn != 0).sum()) if len(pn) else 0,
        "entries": len(att), "fills": len(fl), "fill_rate": len(fl) / len(att) if att else float("nan"),
        "net": float(pn.sum()) if len(pn) else 0.0,
        "per_window": float(pn.mean()) if len(pn) else 0.0,
        "per_traded_window": float(traded.mean()) if len(traded) else 0.0,
        "per_hour": float(pn.sum()) / (n_windows * 5.0 / 60.0) if n_windows else 0.0,
        "t": float(pn.mean() / se) if se == se and se > 0 else float("nan"),
        "sharpe_day": float(pn.mean() / std * math.sqrt(288.0)) if std == std and std > 0 else float("nan"),
        "ev_per_entry": float(pnl.mean()) if len(pnl) else 0.0,
        "ev_per_attempt": float(pnl.sum() / len(att)) if att else 0.0,
        "median_trade": float(np.median(pnl)) if len(pnl) else 0.0,
        "avg_win": avg_w, "avg_loss": avg_l,
        "win_rate": len(wins_) / nf if fl else float("nan"),
        "largest_win": float(pnl.max()) if len(pnl) else 0.0, "largest_loss": float(pnl.min()) if len(pnl) else 0.0,
        "small_win_large_loss": (avg_w / abs(avg_l)) if avg_l else float("nan"),
        "wins_per_loss": (abs(avg_l) / avg_w) if avg_w else float("nan"),
        "profit_factor": float(wins_.sum() / abs(loss_.sum())) if len(loss_) and loss_.sum() != 0 else float("nan"),
        "max_dd": dd, "worst_window": float(pn.min()) if len(pn) else 0.0, "cvar5_window": cvar,
        "cvar5_trade": trade_cvar,
        "fees": fees, "slippage": float(np.mean(slip)) if slip else 0.0,
        "avg_hold_s": float(np.mean(hold)) if hold else 0.0,
        "max_exposure": max((r["shares"] * r["px"] for r in fl), default=0.0),
        "unpaired_positions": len(unp), "unpaired_losses": len(unp_loss),
        "unpaired_loss_avg": float(np.mean([r["pnl"] for r in unp_loss])) if unp_loss else 0.0,
        "pairs_completed": len(pair_ok),
        "pair_avg": float(np.mean([r["pnl"] for r in pair_ok])) if pair_ok else 0.0,
        "pct_settle": sum(v for k, v in kinds.items() if k.startswith("SETTLE")) / nf,
        "pct_stop": (kinds["STOP_LOSS"]) / nf, "pct_thesis": kinds["THESIS_INVALIDATED"] / nf,
        "pct_time": kinds["TIME_EXIT"] / nf, "pct_tp": kinds["TAKE_PROFIT"] / nf,
        "pct_pair": kinds["PAIR_ORDER_FILLED"] / nf,
    }


def row(label, split, m, cols=None):
    cols = cols or ("windows_traded", "fills", "fill_rate", "net", "per_window", "t", "ev_per_entry", "win_rate",
                    "avg_win", "avg_loss", "wins_per_loss", "largest_loss", "worst_window", "cvar5_window", "max_dd",
                    "pct_settle", "pct_stop", "pct_tp")
    f = []
    for c in cols:
        v = m[c]
        if isinstance(v, float) and math.isnan(v):
            f.append("—")
        elif c in ("fill_rate", "win_rate") or c.startswith("pct_"):
            f.append(f"{v:.0%}")
        elif c in ("windows_traded", "fills", "entries", "windows", "unpaired_positions", "unpaired_losses"):
            f.append(str(int(v)))
        elif c == "t":
            f.append(f"{v:+.1f}")
        elif c in ("ev_per_entry", "per_window", "avg_win", "avg_loss", "median_trade"):
            f.append(f"{v:+.3f}")
        else:
            f.append(f"{v:+.2f}")
    return f"| {label} | {split} | " + " | ".join(f) + " |"


HEAD_COLS = ("traded windows", "fills", "fill %", "net $", "$/window", "t", "EV/entry", "win %", "avg win", "avg loss",
             "wins per loss", "largest loss", "worst window", "5 % worst windows", "max DD", "settled", "stopped", "TP")


def head():
    return ("| strategy | split | " + " | ".join(HEAD_COLS) + " |\n|---|---|" + "---:|" * len(HEAD_COLS))
