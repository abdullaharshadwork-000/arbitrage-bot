"""Pattern discovery over experiences.

Phase 9.

Aggregates outcomes by regime / action / strategy. Patterns become
hypotheses only – never automatic trading rules.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class PatternStat:
    key: str
    sample_size: int
    win_rate: float
    avg_pnl: float
    total_pnl: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PatternReport:
    by_regime: tuple[PatternStat, ...] = ()
    by_action: tuple[PatternStat, ...] = ()
    by_strategy: tuple[PatternStat, ...] = ()
    suggested_hypotheses: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "by_regime": [p.as_dict() for p in self.by_regime],
            "by_action": [p.as_dict() for p in self.by_action],
            "by_strategy": [p.as_dict() for p in self.by_strategy],
            "suggested_hypotheses": list(self.suggested_hypotheses),
            "meta": dict(self.meta),
        }


def _pnl(row: Any) -> Optional[float]:
    if isinstance(row, dict):
        v = row.get("realized_pnl")
    else:
        v = getattr(row, "realized_pnl", None)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _field(row: Any, name: str) -> Optional[str]:
    if isinstance(row, dict):
        v = row.get(name)
    else:
        v = getattr(row, name, None)
    return str(v) if v is not None else None


def _aggregate(rows: Sequence[Any], key_fn) -> list[PatternStat]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        p = _pnl(r)
        if k and p is not None:
            buckets[k].append(p)
    stats: list[PatternStat] = []
    for k, pnls in buckets.items():
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        stats.append(
            PatternStat(
                key=k,
                sample_size=len(pnls),
                win_rate=round(wins / len(pnls), 4),
                avg_pnl=round(sum(pnls) / len(pnls), 6),
                total_pnl=round(sum(pnls), 6),
            )
        )
    stats.sort(key=lambda s: s.sample_size, reverse=True)
    return stats


class PatternDiscovery:
    """Discover provisional patterns from experience rows."""

    def __init__(self, *,
                 min_sample: int = 5):
        self.min_sample = int(min_sample)

    def analyze(self, experiences: Sequence[Any]) -> PatternReport:
        by_regime = _aggregate(experiences, lambda r: _field(r, "regime"))
        by_action = _aggregate(experiences, lambda r: _field(r, "final_action"))
        by_strategy = _aggregate(experiences, lambda r: _field(r, "strategy_id"))

        hypotheses: list[str] = []
        for p in by_regime:
            if p.sample_size < self.min_sample:
                continue
            if p.win_rate < 0.4 and p.avg_pnl < 0:
                hypotheses.append(
                    f"Regime {p.key} shows weak results "
                    f"(win_rate={p.win_rate:.0%}, n={p.sample_size}); "
                    f"consider reducing allocation or tightening filters."
                )
            elif p.win_rate > 0.6 and p.avg_pnl > 0:
                hypotheses.append(
                    f"Regime {p.key} shows relative strength "
                    f"(win_rate={p.win_rate:.0%}, n={p.sample_size}); "
                    f"test increased selectivity only after validation."
                )

        return PatternReport(
            by_regime=tuple(by_regime),
            by_action=tuple(by_action),
            by_strategy=tuple(by_strategy),
            suggested_hypotheses=tuple(hypotheses[:10]),
            meta={"min_sample": self.min_sample, "rows": len(experiences)},
        )


__all__ = ["PatternStat", "PatternReport", "PatternDiscovery"]
