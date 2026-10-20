"""Historical joint-outcome model with probability shrinkage.

Stores synchronized settlement counts per asset pair and produces calibrated
JointProbs. Uses a Dirichlet / Laplace prior so small samples shrink toward
independence (or a mild agreement prior), never hard-coding persistence.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .math import JointProbs, normalize_probs, wilson_halfwidth


@dataclass(slots=True)
class PairOutcomeCounts:
    asset_a: str
    asset_b: str
    n_uu: int = 0
    n_ud: int = 0
    n_du: int = 0
    n_dd: int = 0

    @property
    def n(self) -> int:
        return int(self.n_uu + self.n_ud + self.n_du + self.n_dd)

    def key(self) -> str:
        a, b = sorted((self.asset_a.upper(), self.asset_b.upper()))
        return f"{a}|{b}"

    def record(self, a_up: bool, b_up: bool) -> None:
        if a_up and b_up:
            self.n_uu += 1
        elif a_up and not b_up:
            self.n_ud += 1
        elif (not a_up) and b_up:
            self.n_du += 1
        else:
            self.n_dd += 1

    def as_dict(self) -> dict:
        return {
            "asset_a": self.asset_a, "asset_b": self.asset_b,
            "n_uu": self.n_uu, "n_ud": self.n_ud, "n_du": self.n_du, "n_dd": self.n_dd,
            "n": self.n,
        }


@dataclass(slots=True)
class JointOutcomeModel:
    """In-memory store of synchronized pair outcomes + shrinkage estimator."""
    prior_strength: float = 4.0
    #: Prior mass on agreement cells (UU, DD) vs disagreement; mild default.
    prior_agreement_bias: float = 0.55
    min_sample_size: int = 50
    counts: dict[str, PairOutcomeCounts] = field(default_factory=dict)

    def _pair_key(self, a: str, b: str) -> tuple[str, str, str]:
        aa, bb = a.upper(), b.upper()
        # Canonical storage key is sorted; caller still names A/B for direction.
        ka, kb = sorted((aa, bb))
        return aa, bb, f"{ka}|{kb}"

    def get_counts(self, asset_a: str, asset_b: str) -> PairOutcomeCounts:
        aa, bb, key = self._pair_key(asset_a, asset_b)
        row = self.counts.get(key)
        if row is None:
            row = PairOutcomeCounts(asset_a=aa if aa <= bb else bb,
                                    asset_b=bb if aa <= bb else aa)
            self.counts[key] = row
        return row

    def observe(self, asset_a: str, asset_b: str, a_up: bool, b_up: bool) -> None:
        """Record a synchronized settlement. Flips UD/DU if storage order swapped."""
        aa, bb, key = self._pair_key(asset_a, asset_b)
        row = self.get_counts(aa, bb)
        # Storage always uses lexicographic (asset_a, asset_b) on the row.
        sa, sb = row.asset_a, row.asset_b
        if aa == sa and bb == sb:
            row.record(a_up, b_up)
        else:
            # Caller A/B swapped vs storage — swap outcomes.
            row.record(b_up, a_up)

    def estimate(self, asset_a: str, asset_b: str) -> JointProbs:
        """Shrinkage estimator for P(outcomes) oriented to caller's A/B order."""
        aa, bb, _ = self._pair_key(asset_a, asset_b)
        row = self.get_counts(aa, bb)
        n = row.n
        # Mild agreement prior split across four cells.
        agree = float(self.prior_agreement_bias)
        disagree = 1.0 - agree
        alpha_uu = self.prior_strength * (agree / 2.0)
        alpha_dd = self.prior_strength * (agree / 2.0)
        alpha_ud = self.prior_strength * (disagree / 2.0)
        alpha_du = self.prior_strength * (disagree / 2.0)

        # Map storage counts into caller orientation.
        if row.asset_a == aa and row.asset_b == bb:
            c_uu, c_ud, c_du, c_dd = row.n_uu, row.n_ud, row.n_du, row.n_dd
        else:
            c_uu, c_ud, c_du, c_dd = row.n_uu, row.n_du, row.n_ud, row.n_dd

        total = n + self.prior_strength
        p_uu = (c_uu + alpha_uu) / total
        p_ud = (c_ud + alpha_ud) / total
        p_du = (c_du + alpha_du) / total
        p_dd = (c_dd + alpha_dd) / total
        p_uu, p_ud, p_du, p_dd = normalize_probs(p_uu, p_ud, p_du, p_dd)

        # Uncertainty from Wilson half-width on the larger disagreement rate cell.
        disagree_n = c_ud + c_du
        unc = wilson_halfwidth(disagree_n, max(n, 1))
        if n < self.min_sample_size:
            # Inflate uncertainty when under-sampled.
            unc = max(unc, 0.5 * (1.0 - n / max(self.min_sample_size, 1)))
        return JointProbs(p_uu, p_ud, p_du, p_dd, n=n, uncertainty=unc)

    def is_qualified(self, asset_a: str, asset_b: str) -> bool:
        return self.get_counts(asset_a, asset_b).n >= self.min_sample_size

    def load_json(self, path: str | Path) -> int:
        p = Path(path)
        if not p.is_file():
            return 0
        doc = json.loads(p.read_text(encoding="utf-8"))
        rows = doc.get("pairs") if isinstance(doc, dict) else doc
        n = 0
        for raw in rows or []:
            row = PairOutcomeCounts(
                asset_a=str(raw["asset_a"]).upper(),
                asset_b=str(raw["asset_b"]).upper(),
                n_uu=int(raw.get("n_uu", 0)),
                n_ud=int(raw.get("n_ud", 0)),
                n_du=int(raw.get("n_du", 0)),
                n_dd=int(raw.get("n_dd", 0)),
            )
            self.counts[row.key()] = row
            n += 1
        return n

    def dump_json(self, path: str | Path) -> None:
        payload = {"pairs": [c.as_dict() for c in self.counts.values()]}
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def ingest_synced(self, observations: Iterable[tuple[str, str, bool, bool]]) -> int:
        n = 0
        for a, b, a_up, b_up in observations:
            self.observe(a, b, a_up, b_up)
            n += 1
        return n

    def summary(self) -> dict:
        return {
            "n_pairs": len(self.counts),
            "pairs": {
                k: {
                    **v.as_dict(),
                    "agreement": (v.n_uu + v.n_dd) / v.n if v.n else None,
                    "disagreement_ud": v.n_ud / v.n if v.n else None,
                    "disagreement_du": v.n_du / v.n if v.n else None,
                }
                for k, v in self.counts.items()
            },
        }


def independence_baseline(p_a_up: float, p_b_up: float) -> JointProbs:
    """Baseline: markets independent given marginal UP probs."""
    pa = min(max(float(p_a_up), 1e-6), 1.0 - 1e-6)
    pb = min(max(float(p_b_up), 1e-6), 1.0 - 1e-6)
    return JointProbs(
        p_uu=pa * pb, p_ud=pa * (1 - pb), p_du=(1 - pa) * pb, p_dd=(1 - pa) * (1 - pb),
        n=0, uncertainty=1.0,
    )


def always_agree_baseline(p_agree: float = 0.9) -> JointProbs:
    """Naive baseline that almost always predicts agreement."""
    a = min(max(float(p_agree), 0.0), 1.0)
    d = (1.0 - a) / 2.0
    return JointProbs(a / 2.0, d, d, a / 2.0, n=0, uncertainty=1.0)


def log_score(probs: JointProbs, a_up: bool, b_up: bool) -> float:
    p = probs.normalized()
    if a_up and b_up:
        q = p.p_uu
    elif a_up and not b_up:
        q = p.p_ud
    elif (not a_up) and b_up:
        q = p.p_du
    else:
        q = p.p_dd
    return math.log(max(q, 1e-12))
