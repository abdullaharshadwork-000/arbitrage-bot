"""Promotion gate: structured checklist before LIVE canary.

Does not place orders. Human approval still required by default.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class GateCriterion:
    name: str
    required: bool = True
    passed: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PromotionGateResult:
    strategy_id: str
    strategy_version: str
    eligible: bool
    criteria: list[GateCriterion] = field(default_factory=list)
    notes: str = ""
    evaluated_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "eligible": self.eligible,
            "criteria": [c.as_dict() for c in self.criteria],
            "notes": self.notes,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


class PromotionGate:
    """Evaluate whether a challenger may enter LIVE canary."""

    def evaluate(
        self,
        strategy_id: str,
        strategy_version: str,
        *,
        metrics: Optional[dict[str, Any]] = None,
        flags: Optional[dict[str, bool]] = None,
    ) -> PromotionGateResult:
        m = metrics or {}
        f = flags or {}

        criteria = [
            GateCriterion(
                "backtest_pass",
                passed=bool(f.get("backtest_pass", m.get("backtest_pass", False))),
                detail=str(m.get("backtest_note", "")),
            ),
            GateCriterion(
                "walk_forward_pass",
                passed=bool(f.get("walk_forward_pass", m.get("walk_forward_pass", False))),
                detail=str(m.get("walk_forward_note", "")),
            ),
            GateCriterion(
                "stress_pass",
                passed=bool(f.get("stress_pass", m.get("stress_pass", False))),
                detail=str(m.get("stress_note", "")),
            ),
            GateCriterion(
                "shadow_pass",
                passed=bool(f.get("shadow_pass", m.get("shadow_pass", False))),
                detail=str(m.get("shadow_note", "")),
            ),
            GateCriterion(
                "paper_pass",
                passed=bool(f.get("paper_pass", m.get("paper_pass", False))),
                detail=str(m.get("paper_note", "")),
            ),
            GateCriterion(
                "min_trades",
                passed=int(m.get("trade_count") or 0) >= int(m.get("min_trades") or 30),
                detail=f"trades={m.get('trade_count', 0)} min={m.get('min_trades', 30)}",
            ),
            GateCriterion(
                "max_drawdown",
                passed=float(m.get("max_drawdown_pct") or 100)
                <= float(m.get("max_drawdown_limit_pct") or 15),
                detail=f"dd={m.get('max_drawdown_pct', '?')}% limit={m.get('max_drawdown_limit_pct', 15)}%",
            ),
            GateCriterion(
                "positive_expectancy",
                passed=float(m.get("expectancy") or 0) > 0,
                detail=f"expectancy={m.get('expectancy', 0)}",
            ),
            GateCriterion(
                "human_approval",
                required=True,
                passed=bool(f.get("human_approval", False)),
                detail="operator must explicitly approve",
            ),
        ]

        required_ok = all(c.passed for c in criteria if c.required)
        notes = "eligible for LIVE canary" if required_ok else "blocked – incomplete checklist"
        return PromotionGateResult(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            eligible=required_ok,
            criteria=criteria,
            notes=notes,
        )


__all__ = ["GateCriterion", "PromotionGateResult", "PromotionGate"]
