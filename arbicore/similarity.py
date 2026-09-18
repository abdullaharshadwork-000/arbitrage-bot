"""Historical similarity engine.

Phase 19.

Finds past experiences whose feature snapshots are close to the current
market state. Supporting evidence only – never sole authority for orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence


DEFAULT_KEYS = (
    "momentum_10",
    "momentum_5",
    "realized_vol",
    "rsi_14",
    "atr_pct",
    "ema_slope",
)


@dataclass(frozen=True)
class SimilarState:
    experience_id: str
    distance: float
    symbol: str
    regime: Optional[str]
    strategy_id: Optional[str]
    final_action: Optional[str]
    realized_pnl: Optional[float]
    timestamp: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SimilarityReport:
    query_features: dict[str, float]
    matches: tuple[SimilarState, ...] = ()
    expectancy_by_action: dict[str, float] = field(default_factory=dict)
    sample_size: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query_features": dict(self.query_features),
            "matches": [m.as_dict() for m in self.matches],
            "expectancy_by_action": dict(self.expectancy_by_action),
            "sample_size": self.sample_size,
            "meta": dict(self.meta),
        }


def _feature_vector(snapshot: dict[str, Any], keys: Sequence[str]) -> list[float]:
    vec: list[float] = []
    for k in keys:
        v = snapshot.get(k, 0.0)
        try:
            vec.append(float(v) if v is not None else 0.0)
        except (TypeError, ValueError):
            vec.append(0.0)
    return vec


def _euclidean(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return float("inf")
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


class SimilarityEngine:
    """Nearest-neighbor search over experience feature snapshots."""

    def __init__(
        self,
        *,
        feature_keys: Sequence[str] = DEFAULT_KEYS,
        top_k: int = 20,
        max_distance: float = 5.0,
    ):
        self.feature_keys = tuple(feature_keys)
        self.top_k = int(top_k)
        self.max_distance = float(max_distance)

    def query(
        self,
        current_features: dict[str, Any],
        experiences: Sequence[Any],
        *,
        symbol: Optional[str] = None,
        regime: Optional[str] = None,
    ) -> SimilarityReport:
        q = _feature_vector(current_features, self.feature_keys)
        scored: list[SimilarState] = []

        for exp in experiences:
            if isinstance(exp, dict):
                sid = exp.get("symbol")
                reg = exp.get("regime")
                snap = exp.get("feature_snapshot") or {}
                if isinstance(snap, str):
                    try:
                        import json

                        snap = json.loads(snap)
                    except Exception:
                        snap = {}
                eid = str(exp.get("id", ""))
                strat = exp.get("strategy_id")
                action = exp.get("final_action")
                pnl_raw = exp.get("realized_pnl")
                ts = exp.get("timestamp")
            else:
                sid = getattr(exp, "symbol", None)
                reg = getattr(exp, "regime", None)
                snap = getattr(exp, "feature_snapshot", None) or {}
                eid = str(getattr(exp, "id", ""))
                strat = getattr(exp, "strategy_id", None)
                action = getattr(exp, "final_action", None)
                pnl_raw = getattr(exp, "realized_pnl", None)
                ts = getattr(exp, "timestamp", None)
                if hasattr(ts, "isoformat"):
                    ts = ts.isoformat()

            if symbol and sid and sid != symbol:
                continue
            if regime and reg and reg != regime:
                continue

            vec = _feature_vector(snap if isinstance(snap, dict) else {}, self.feature_keys)
            dist = _euclidean(q, vec)
            if dist > self.max_distance:
                continue

            pnl = None
            if pnl_raw is not None and pnl_raw != "":
                try:
                    pnl = float(pnl_raw)
                except (TypeError, ValueError):
                    pnl = None

            scored.append(
                SimilarState(
                    experience_id=eid,
                    distance=round(dist, 6),
                    symbol=str(sid or ""),
                    regime=reg,
                    strategy_id=strat,
                    final_action=action,
                    realized_pnl=pnl,
                    timestamp=str(ts) if ts else None,
                )
            )

        scored.sort(key=lambda s: s.distance)
        top = scored[: self.top_k]

        buckets: dict[str, list[float]] = {}
        for m in top:
            if m.final_action and m.realized_pnl is not None:
                buckets.setdefault(m.final_action, []).append(m.realized_pnl)
        expectancy = {
            act: round(sum(vals) / len(vals), 6) for act, vals in buckets.items() if vals
        }

        return SimilarityReport(
            query_features={k: float(current_features.get(k) or 0) for k in self.feature_keys},
            matches=tuple(top),
            expectancy_by_action=expectancy,
            sample_size=len(top),
            meta={"feature_keys": list(self.feature_keys), "max_distance": self.max_distance},
        )


__all__ = ["SimilarState", "SimilarityReport", "SimilarityEngine"]
