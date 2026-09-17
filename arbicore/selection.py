"""Strategy Selection – choose among APPROVED strategies or NO_TRADE.

Phase 6 foundation.

Rules:
* Only strategies in APPROVED / LIVE / LIVE_CANARY / CHALLENGER status may be selected.
* NO_TRADE is always a valid intelligent outcome.
* Selection is deterministic given the same inputs.
* Nothing here places orders or changes risk limits.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from .domain import StrategyStatus, StrategyVersion
from .regime import RegimeDecision


SELECTABLE_STATUSES = {
    StrategyStatus.APPROVED,
    StrategyStatus.CHALLENGER,
    StrategyStatus.LIVE_CANARY,
    StrategyStatus.LIVE,
}


@dataclass(frozen=True)
class SelectionResult:
    """Outcome of one selection cycle."""

    action: str                          # USE_STRATEGY | NO_TRADE
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    strategy_name: Optional[str] = None
    regime: Optional[str] = None
    regime_confidence: float = 0.0
    reason: str = ""
    candidates_considered: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategySelector:
    """Select a strategy for the current regime or decide NO_TRADE."""

    def __init__(self, min_regime_confidence: float = 0.40):
        self.min_regime_confidence = float(min_regime_confidence)

    def select(
        self,
        candidates: Sequence[StrategyVersion],
        regime: RegimeDecision,
        *,
        symbol: Optional[str] = None,
    ) -> SelectionResult:
        """Return a SelectionResult.

        Ranking (simple, transparent):
        1. Filter to selectable statuses.
        2. Prefer strategies whose intended_regimes include the current regime.
        3. Prefer LIVE > LIVE_CANARY > CHALLENGER > APPROVED.
        4. If nothing qualifies → NO_TRADE.
        """
        selectable = [c for c in candidates if c.status in SELECTABLE_STATUSES]

        if regime.confidence < self.min_regime_confidence:
            return SelectionResult(
                action="NO_TRADE",
                regime=regime.regime,
                regime_confidence=regime.confidence,
                reason=(
                    f"regime confidence {regime.confidence:.2f} below "
                    f"minimum {self.min_regime_confidence:.2f}"
                ),
                candidates_considered=len(selectable),
            )

        if not selectable:
            return SelectionResult(
                action="NO_TRADE",
                regime=regime.regime,
                regime_confidence=regime.confidence,
                reason="no selectable (APPROVED/LIVE/…) strategies available",
                candidates_considered=0,
            )

        # Symbol filter when the strategy declares supported symbols
        if symbol:
            symbol_ok = [
                c for c in selectable
                if not c.supported_symbols or symbol in c.supported_symbols
            ]
            if symbol_ok:
                selectable = symbol_ok

        # Regime fit
        regime_fit = [
            c for c in selectable
            if not c.intended_regimes or regime.regime in c.intended_regimes
        ]
        pool = regime_fit if regime_fit else selectable

        # Status preference
        status_rank = {
            StrategyStatus.LIVE: 4,
            StrategyStatus.LIVE_CANARY: 3,
            StrategyStatus.CHALLENGER: 2,
            StrategyStatus.APPROVED: 1,
        }
        pool = sorted(
            pool,
            key=lambda c: status_rank.get(c.status, 0),
            reverse=True,
        )

        chosen = pool[0]
        fit_note = (
            "regime match"
            if chosen.intended_regimes and regime.regime in chosen.intended_regimes
            else "best available selectable strategy"
        )
        return SelectionResult(
            action="USE_STRATEGY",
            strategy_id=chosen.id,
            strategy_version=chosen.version,
            strategy_name=chosen.name,
            regime=regime.regime,
            regime_confidence=regime.confidence,
            reason=fit_note,
            candidates_considered=len(selectable),
            meta={"status": chosen.status.value},
        )


__all__ = ["SelectionResult", "StrategySelector", "SELECTABLE_STATUSES"]
