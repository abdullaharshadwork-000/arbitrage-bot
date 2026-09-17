"""Reflection Agent – post-trade analysis.

Phase 8.

Distinguishes:
  GOOD DECISION + GOOD OUTCOME
  GOOD DECISION + BAD OUTCOME
  BAD DECISION + GOOD OUTCOME
  BAD DECISION + BAD OUTCOME

Profit alone does NOT determine decision quality.
Never places orders. Never weakens risk limits.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Optional

from .domain import Experience


@dataclass(frozen=True)
class ReflectionResult:
    experience_id: str
    decision_quality: str          # good | bad | uncertain
    outcome_quality: str           # good | bad | neutral
    classification: str            # e.g. GOOD_DECISION_BAD_OUTCOME
    lessons: tuple[str, ...] = ()
    summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReflectionAgent:
    """Produce a structured reflection for one Experience."""

    def reflect(self, experience: Experience) -> ReflectionResult:
        pnl = experience.realized_pnl
        if pnl is None:
            outcome = "neutral"
        elif pnl > 0:
            outcome = "good"
        elif pnl < 0:
            outcome = "bad"
        else:
            outcome = "neutral"

        # Decision quality heuristics (transparent, not ML)
        decision = "uncertain"
        lessons: list[str] = []

        if experience.critic_result == "REJECT":
            decision = "bad"
            lessons.append("critic rejected the setup; should not have traded")
        elif experience.critic_result == "WARN":
            decision = "uncertain"
            lessons.append("critic warned; review whether warnings were material")
        elif experience.risk_result and "reject" in str(experience.risk_result).lower():
            decision = "bad"
            lessons.append("risk kernel rejected; trade should not have reached execution")
        elif experience.trade_confidence is not None and experience.trade_confidence < 0.5:
            decision = "bad"
            lessons.append("low trade confidence at entry")
        elif experience.regime in ("UNCERTAIN", "WARMING_UP"):
            decision = "uncertain"
            lessons.append("traded during uncertain/warming-up regime")
        else:
            decision = "good"

        # Outcome-specific lessons
        if outcome == "bad" and decision == "good":
            lessons.append("good process, adverse outcome – ordinary variance or regime shift?")
        if outcome == "good" and decision == "bad":
            lessons.append("bad process, lucky outcome – do not reinforce the mistake")

        classification = f"{decision.upper()}_DECISION_{outcome.upper()}_OUTCOME"

        summary = (
            f"Decision={decision}, Outcome={outcome}. "
            + ("; ".join(lessons) if lessons else "No additional lessons.")
        )

        return ReflectionResult(
            experience_id=experience.id,
            decision_quality=decision,
            outcome_quality=outcome,
            classification=classification,
            lessons=tuple(lessons),
            summary=summary,
            meta={
                "pnl": str(pnl) if pnl is not None else None,
                "regime": experience.regime,
                "critic_result": experience.critic_result,
                "risk_result": experience.risk_result,
            },
        )


__all__ = ["ReflectionResult", "ReflectionAgent"]
