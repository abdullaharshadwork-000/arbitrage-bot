"""Live canary allocation stages.

Phase 23.

Staged deployment for newly promoted strategies. All stages remain inside
global Risk Kernel limits. Never places orders; only computes allowed
fraction of a strategy's allocation.

Stages (fraction of approved strategy allocation):
  1: 0.05
  2: 0.15
  3: 0.40
  4: 1.00 (full approved allocation)

Promotion between stages is operator-driven by default (HUMAN_APPROVAL).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


DEFAULT_STAGES = (0.05, 0.15, 0.40, 1.0)


@dataclass
class CanaryDeployment:
    id: str
    strategy_id: str
    strategy_version: str
    stage: int = 1  # 1..len(stages)
    stages: tuple[float, ...] = DEFAULT_STAGES
    status: str = "active"  # active | paused | rolled_back | completed
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    notes: str = ""

    @property
    def allocation_fraction(self) -> float:
        idx = max(0, min(self.stage, len(self.stages)) - 1)
        return float(self.stages[idx])

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "stage": self.stage,
            "stages": list(self.stages),
            "allocation_fraction": self.allocation_fraction,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "notes": self.notes,
        }


class CanaryManager:
    """Track canary deployments. Operator advances/rolls back stages."""

    def __init__(self, stages: tuple[float, ...] = DEFAULT_STAGES):
        self.stages = stages
        self._deployments: dict[str, CanaryDeployment] = {}

    def start(
        self,
        strategy_id: str,
        strategy_version: str,
        *,
        notes: str = "",
    ) -> CanaryDeployment:
        dep = CanaryDeployment(
            id=f"can_{uuid4().hex[:12]}",
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            stage=1,
            stages=self.stages,
            notes=notes or "canary started at stage 1",
        )
        self._deployments[dep.id] = dep
        return dep

    def advance(self, deployment_id: str) -> Optional[CanaryDeployment]:
        dep = self._deployments.get(deployment_id)
        if not dep or dep.status != "active":
            return None
        if dep.stage >= len(dep.stages):
            dep.status = "completed"
            dep.updated_at = _utc_now()
            return dep
        dep.stage += 1
        dep.updated_at = _utc_now()
        if dep.stage >= len(dep.stages):
            dep.status = "completed"
            dep.notes = (dep.notes + " | full allocation").strip()
        return dep

    def rollback(self, deployment_id: str, reason: str = "") -> Optional[CanaryDeployment]:
        dep = self._deployments.get(deployment_id)
        if not dep:
            return None
        dep.status = "rolled_back"
        dep.updated_at = _utc_now()
        dep.notes = (dep.notes + f" | rollback: {reason}").strip()
        return dep

    def pause(self, deployment_id: str) -> Optional[CanaryDeployment]:
        dep = self._deployments.get(deployment_id)
        if not dep:
            return None
        dep.status = "paused"
        dep.updated_at = _utc_now()
        return dep

    def allowed_fraction(self, strategy_id: str) -> float:
        """Max canary fraction for a strategy (0 if none active)."""
        fracs = [
            d.allocation_fraction
            for d in self._deployments.values()
            if d.strategy_id == strategy_id and d.status == "active"
        ]
        return max(fracs) if fracs else 0.0

    def list_deployments(self) -> list[CanaryDeployment]:
        return list(self._deployments.values())

    def snapshot(self) -> dict[str, Any]:
        deps = self.list_deployments()
        return {
            "count": len(deps),
            "deployments": [d.as_dict() for d in deps],
        }


__all__ = ["CanaryDeployment", "CanaryManager", "DEFAULT_STAGES"]
