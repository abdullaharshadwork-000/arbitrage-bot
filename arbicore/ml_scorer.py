"""Lightweight research trade-quality scorer.

Phase 24 foundation.

Pure heuristic over features (no sklearn dependency). Output is a score
in [0, 1] for research / ensemble input only – never places orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ScoreResult:
    score: float
    label: str  # weak | neutral | strong
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "label": self.label,
            "reasons": list(self.reasons),
        }


class HeuristicScorer:
    """Map common feature keys to a trade-quality score."""

    def score(self, features: dict[str, Any], *,
              action: str = "BUY") -> ScoreResult:
        reasons: list[str] = []
        s = 0.5

        def f(key: str, default: float = 0.0) -> float:
            try:
                return float(features.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        mom = f("momentum_10") or f("momentum_5")
        rsi = f("rsi_14", 50.0)
        vol = f("realized_vol") or f("atr_pct")

        if action.upper() == "BUY":
            if mom > 0:
                s += 0.1
                reasons.append("positive momentum")
            elif mom < 0:
                s -= 0.1
                reasons.append("negative momentum")
            if 40 <= rsi <= 65:
                s += 0.08
                reasons.append("rsi in constructive band")
            elif rsi > 75:
                s -= 0.12
                reasons.append("rsi overbought")
            elif rsi < 30:
                s += 0.05
                reasons.append("rsi oversold bounce candidate")
        elif action.upper() == "SELL":
            if mom < 0:
                s += 0.1
                reasons.append("negative momentum")
            if rsi > 70:
                s += 0.08
                reasons.append("rsi elevated")

        if vol > 0.05:
            s -= 0.08
            reasons.append("elevated volatility")

        s = max(0.0, min(1.0, s))
        if s >= 0.65:
            label = "strong"
        elif s <= 0.4:
            label = "weak"
        else:
            label = "neutral"
        return ScoreResult(score=round(s, 4), label=label, reasons=tuple(reasons))


__all__ = ["ScoreResult", "HeuristicScorer"]
