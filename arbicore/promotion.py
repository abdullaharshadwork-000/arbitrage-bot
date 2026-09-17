"""Champion / Challenger + Promotion engine.

Phases 16–17 foundation.

Default policy: HUMAN_APPROVAL required for promotion to LIVE.
AUTO_PROMOTE only when deliberately enabled and all gates pass.
Never weakens Risk Kernel limits.
Never places orders itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .domain import StrategyStatus, StrategyVersion
from .strategy_registry import StrategyRegistry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return f"promo_{uuid4().hex[:12]}"


@dataclass(frozen=True)
class PromotionDecision:
    id: str
    champion_id: str
    challenger_id: str
    result: str                    # PROMOTE | REJECT | DEFER | CANARY
    reason: str
    requires_human: bool = True
    metrics_comparison: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


class PromotionEngine:
    """Compare champion vs challenger and emit a PromotionDecision."""

    def __init__(
        self,
        registry: StrategyRegistry,
        *,
        auto_promote: bool = False,
        min_sharpe_improvement: float = 0.15,
        max_drawdown_increase: float = 0.02,
    ):
        self.registry = registry
        self.auto_promote = bool(auto_promote)
        self.min_sharpe_improvement = float(min_sharpe_improvement)
        self.max_drawdown_increase = float(max_drawdown_increase)
        self._history: list[PromotionDecision] = []

    def evaluate(
        self,
        champion_id: str,
        challenger_id: str,
        *,
        champion_metrics: Optional[dict[str, float]] = None,
        challenger_metrics: Optional[dict[str, float]] = None,
    ) -> PromotionDecision:
        champion = self.registry.get(champion_id)
        challenger = self.registry.get(challenger_id)
        if champion is None or challenger is None:
            decision = PromotionDecision(
                id=_new_id(),
                champion_id=champion_id,
                challenger_id=challenger_id,
                result="REJECT",
                reason="champion or challenger not found in registry",
                requires_human=True,
            )
            self._history.append(decision)
            return decision

        c_m = champion_metrics or {}
        h_m = challenger_metrics or {}

        if not c_m or not h_m:
            decision = PromotionDecision(
                id=_new_id(),
                champion_id=champion_id,
                challenger_id=challenger_id,
                result="DEFER",
                reason="insufficient metrics for both strategies",
                requires_human=True,
                metrics_comparison={"champion": c_m, "challenger": h_m},
            )
            self._history.append(decision)
            return decision

        sharpe_c = float(c_m.get("sharpe", 0))
        sharpe_h = float(h_m.get("sharpe", 0))
        dd_c = float(c_m.get("max_drawdown", 1))
        dd_h = float(h_m.get("max_drawdown", 1))
        pf_h = float(h_m.get("profit_factor", 0))

        reasons = []
        if sharpe_h < sharpe_c + self.min_sharpe_improvement:
            reasons.append(
                f"challenger Sharpe {sharpe_h:.2f} does not beat champion "
                f"{sharpe_c:.2f} by required {self.min_sharpe_improvement:.2f}"
            )
        if dd_h > dd_c + self.max_drawdown_increase:
            reasons.append(
                f"challenger drawdown {dd_h:.2%} exceeds champion "
                f"{dd_c:.2%} by more than allowed {self.max_drawdown_increase:.2%}"
            )
        if pf_h < 1.0:
            reasons.append(f"challenger profit factor {pf_h:.2f} < 1.0")

        if reasons:
            result = "REJECT"
            reason = "; ".join(reasons)
            requires_human = True
        else:
            if self.auto_promote:
                result = "CANARY"
                reason = "quantitative gates passed; auto-promote to canary only"
                requires_human = False
            else:
                result = "PROMOTE"
                reason = "quantitative gates passed; human approval required for LIVE"
                requires_human = True

        decision = PromotionDecision(
            id=_new_id(),
            champion_id=champion_id,
            challenger_id=challenger_id,
            result=result,
            reason=reason,
            requires_human=requires_human,
            metrics_comparison={
                "champion": c_m,
                "challenger": h_m,
                "sharpe_delta": sharpe_h - sharpe_c,
                "drawdown_delta": dd_h - dd_c,
            },
        )
        self._history.append(decision)
        return decision

    def apply_canary(self, decision: PromotionDecision) -> Optional[StrategyVersion]:
        """Move challenger to LIVE_CANARY status if decision allows."""
        if decision.result not in ("CANARY", "PROMOTE"):
            return None
        if decision.requires_human and decision.result == "PROMOTE":
            return None
        return self.registry.set_status(
            decision.challenger_id, StrategyStatus.LIVE_CANARY
        )

    def history(self) -> list[PromotionDecision]:
        return list(self._history)


__all__ = ["PromotionDecision", "PromotionEngine"]
