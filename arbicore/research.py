"""Hypothesis & Experiment framework.

Phases 9–10.

A discovered pattern becomes a Hypothesis.
A Hypothesis is tested by an Experiment.
Nothing here deploys to LIVE or places orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex[:12]}"


@dataclass(frozen=True)
class Hypothesis:
    id: str
    statement: str
    supporting_evidence: tuple[str, ...] = ()
    sample_size: int = 0
    affected_strategies: tuple[str, ...] = ()
    applicable_regimes: tuple[str, ...] = ()
    expected_benefit: str = ""
    possible_downside: str = ""
    confidence: float = 0.0
    required_experiment: str = ""
    status: str = "open"          # open | testing | supported | rejected | retired
    created_at: datetime = field(default_factory=_utc_now)
    created_by: str = "system"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


@dataclass(frozen=True)
class Experiment:
    id: str
    hypothesis_id: str
    baseline_strategy_id: str
    candidate_strategy_id: str
    dataset_description: str = ""
    data_period: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    success_criteria: str = ""
    failure_criteria: str = ""
    fee_assumptions: str = ""
    slippage_assumptions: str = ""
    status: str = "planned"       # planned | running | completed | failed
    results: dict[str, Any] = field(default_factory=dict)
    conclusion: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    completed_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        if self.completed_at:
            data["completed_at"] = self.completed_at.isoformat()
        return data


class ResearchLab:
    """In-memory store for hypotheses and experiments."""

    def __init__(self):
        self._hypotheses: dict[str, Hypothesis] = {}
        self._experiments: dict[str, Experiment] = {}

    def create_hypothesis(
        self,
        statement: str,
        *,
        supporting_evidence: tuple[str, ...] = (),
        sample_size: int = 0,
        affected_strategies: tuple[str, ...] = (),
        applicable_regimes: tuple[str, ...] = (),
        expected_benefit: str = "",
        possible_downside: str = "",
        confidence: float = 0.0,
        required_experiment: str = "",
        created_by: str = "system",
    ) -> Hypothesis:
        h = Hypothesis(
            id=_new_id("hyp_"),
            statement=statement,
            supporting_evidence=supporting_evidence,
            sample_size=sample_size,
            affected_strategies=affected_strategies,
            applicable_regimes=applicable_regimes,
            expected_benefit=expected_benefit,
            possible_downside=possible_downside,
            confidence=confidence,
            required_experiment=required_experiment,
            created_by=created_by,
        )
        self._hypotheses[h.id] = h
        return h

    def create_experiment(
        self,
        hypothesis_id: str,
        baseline_strategy_id: str,
        candidate_strategy_id: str,
        *,
        dataset_description: str = "",
        data_period: str = "",
        parameters: Optional[dict[str, Any]] = None,
        success_criteria: str = "",
        failure_criteria: str = "",
    ) -> Experiment:
        if hypothesis_id not in self._hypotheses:
            raise ValueError(f"hypothesis {hypothesis_id!r} not found")
        exp = Experiment(
            id=_new_id("exp_"),
            hypothesis_id=hypothesis_id,
            baseline_strategy_id=baseline_strategy_id,
            candidate_strategy_id=candidate_strategy_id,
            dataset_description=dataset_description,
            data_period=data_period,
            parameters=dict(parameters or {}),
            success_criteria=success_criteria,
            failure_criteria=failure_criteria,
        )
        self._experiments[exp.id] = exp
        return exp

    def complete_experiment(
        self,
        experiment_id: str,
        *,
        results: Optional[dict[str, Any]] = None,
        conclusion: str = "",
        status: str = "completed",
    ) -> Experiment:
        current = self._experiments.get(experiment_id)
        if current is None:
            raise ValueError(f"experiment {experiment_id!r} not found")
        updated = Experiment(
            id=current.id,
            hypothesis_id=current.hypothesis_id,
            baseline_strategy_id=current.baseline_strategy_id,
            candidate_strategy_id=current.candidate_strategy_id,
            dataset_description=current.dataset_description,
            data_period=current.data_period,
            parameters=current.parameters,
            success_criteria=current.success_criteria,
            failure_criteria=current.failure_criteria,
            fee_assumptions=current.fee_assumptions,
            slippage_assumptions=current.slippage_assumptions,
            status=status,
            results=dict(results or {}),
            conclusion=conclusion,
            created_at=current.created_at,
            completed_at=_utc_now(),
        )
        self._experiments[experiment_id] = updated
        return updated

    def get_hypothesis(self, hypothesis_id: str) -> Optional[Hypothesis]:
        return self._hypotheses.get(hypothesis_id)

    def get_experiment(self, experiment_id: str) -> Optional[Experiment]:
        return self._experiments.get(experiment_id)

    def list_hypotheses(self, status: Optional[str] = None) -> list[Hypothesis]:
        items = list(self._hypotheses.values())
        if status:
            items = [h for h in items if h.status == status]
        return items

    def list_experiments(self, status: Optional[str] = None) -> list[Experiment]:
        items = list(self._experiments.values())
        if status:
            items = [e for e in items if e.status == status]
        return items


__all__ = ["Hypothesis", "Experiment", "ResearchLab"]
