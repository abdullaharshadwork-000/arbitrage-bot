"""ML model registry.

Phases 24–25 foundation.

Stores model metadata and evaluation metrics. Models are RESEARCH-ONLY
until explicitly promoted through the same validation chain as strategies.
Never places orders. Never overwrites old versions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ModelVersion:
    id: str
    name: str
    version: str
    model_type: str  # classifier | regressor | ensemble | other
    features: tuple[str, ...] = ()
    target: str = ""
    training_period: str = ""
    hyperparameters: dict[str, Any] = field(default_factory=dict)
    training_metrics: dict[str, float] = field(default_factory=dict)
    validation_metrics: dict[str, float] = field(default_factory=dict)
    test_metrics: dict[str, float] = field(default_factory=dict)
    status: str = "draft"  # draft | trained | validated | rejected | research_only
    parent_version_id: Optional[str] = None
    created_at: datetime = field(default_factory=_utc_now)
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["features"] = list(self.features)
        data["created_at"] = self.created_at.isoformat()
        return data


class ModelRegistry:
    def __init__(self):
        self._models: dict[str, ModelVersion] = {}

    def register(
        self,
        name: str,
        version: str,
        model_type: str = "classifier",
        *,
        features: tuple[str, ...] = (),
        target: str = "",
        training_period: str = "",
        hyperparameters: Optional[dict[str, Any]] = None,
        parent_version_id: Optional[str] = None,
        notes: str = "",
    ) -> ModelVersion:
        mv = ModelVersion(
            id=f"mdl_{uuid4().hex[:12]}",
            name=name,
            version=version,
            model_type=model_type,
            features=features,
            target=target,
            training_period=training_period,
            hyperparameters=dict(hyperparameters or {}),
            parent_version_id=parent_version_id,
            status="draft",
            notes=notes or "research only – not connected to live execution",
        )
        self._models[mv.id] = mv
        return mv

    def set_metrics(
        self,
        model_id: str,
        *,
        training: Optional[dict[str, float]] = None,
        validation: Optional[dict[str, float]] = None,
        test: Optional[dict[str, float]] = None,
        status: Optional[str] = None,
    ) -> Optional[ModelVersion]:
        old = self._models.get(model_id)
        if not old:
            return None
        updated = ModelVersion(
            id=old.id,
            name=old.name,
            version=old.version,
            model_type=old.model_type,
            features=old.features,
            target=old.target,
            training_period=old.training_period,
            hyperparameters=old.hyperparameters,
            training_metrics=dict(training or old.training_metrics),
            validation_metrics=dict(validation or old.validation_metrics),
            test_metrics=dict(test or old.test_metrics),
            status=status or old.status,
            parent_version_id=old.parent_version_id,
            created_at=old.created_at,
            notes=old.notes,
        )
        self._models[model_id] = updated
        return updated

    def list_models(self) -> list[ModelVersion]:
        return list(self._models.values())

    def snapshot(self) -> dict[str, Any]:
        models = self.list_models()
        return {
            "count": len(models),
            "models": [m.as_dict() for m in models],
            "note": "All models are research-only until promotion policy allows otherwise",
        }


__all__ = ["ModelVersion", "ModelRegistry"]
