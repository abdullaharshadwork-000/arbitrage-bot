"""Critic Agent – adversarial review of a TradeProposal.

Phase 7 + research scorer integration.

Purpose:
* Search for reasons the proposed trade may be wrong.
* Return APPROVE / WARN / REJECT with structured objections.
* Optionally consult HeuristicScorer (research signal only).
* Never executes trades.
* Never weakens the Risk Kernel.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from .domain import TradeProposal
from .features import FeatureSnapshot
from .regime import RegimeDecision


@dataclass(frozen=True)
class CriticObjection:
    code: str
    severity: str          # low | medium | high
    message: str


@dataclass(frozen=True)
class CriticDecision:
    result: str            # APPROVE | WARN | REJECT
    confidence: float
    objections: tuple[CriticObjection, ...] = ()
    recommended_modifications: tuple[str, ...] = ()
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["objections"] = [asdict(o) for o in self.objections]
        return data


class CriticAgent:
    """Review a TradeProposal against features, regime, and basic quality rules."""

    def __init__(
        self,
        *,
        min_reward_risk: float = 1.2,
        min_trade_confidence: float = 0.45,
        max_spread_pct: float = 0.40,
        use_heuristic_scorer: bool = True,
        scorer_weak_threshold: float = 0.35,
    ):
        self.min_reward_risk = float(min_reward_risk)
        self.min_trade_confidence = float(min_trade_confidence)
        self.max_spread_pct = float(max_spread_pct)
        self.use_heuristic_scorer = bool(use_heuristic_scorer)
        self.scorer_weak_threshold = float(scorer_weak_threshold)

    def review(
        self,
        proposal: TradeProposal,
        *,
        features: Optional[FeatureSnapshot] = None,
        regime: Optional[RegimeDecision] = None,
        extra_signals: Optional[Sequence[str]] = None,
    ) -> CriticDecision:
        objections: list[CriticObjection] = []
        modifications: list[str] = []
        scorer_meta: dict[str, Any] = {}

        if proposal.action in ("NO_TRADE", "HOLD"):
            return CriticDecision(
                result="APPROVE",
                confidence=1.0,
                reason="no position change requested",
            )

        if proposal.trade_confidence < self.min_trade_confidence:
            objections.append(CriticObjection(
                code="low_trade_confidence",
                severity="high",
                message=(
                    f"trade_confidence {proposal.trade_confidence:.2f} "
                    f"below minimum {self.min_trade_confidence:.2f}"
                ),
            ))

        if proposal.expected_reward_risk is not None:
            if proposal.expected_reward_risk < self.min_reward_risk:
                objections.append(CriticObjection(
                    code="poor_reward_risk",
                    severity="high",
                    message=(
                        f"expected R:R {proposal.expected_reward_risk:.2f} "
                        f"below minimum {self.min_reward_risk:.2f}"
                    ),
                ))
                modifications.append("increase target or tighten stop")

        if proposal.action in ("BUY", "SELL") and proposal.entry_price is not None:
            if proposal.stop_loss is None:
                objections.append(CriticObjection(
                    code="missing_stop",
                    severity="high",
                    message="directional trade has no stop_loss",
                ))
            if proposal.take_profit is None:
                objections.append(CriticObjection(
                    code="missing_take_profit",
                    severity="medium",
                    message="directional trade has no take_profit",
                ))

        if regime is not None:
            if regime.regime in ("UNCERTAIN", "WARMING_UP") and regime.confidence < 0.5:
                objections.append(CriticObjection(
                    code="uncertain_regime",
                    severity="medium",
                    message=f"regime is {regime.regime} with low confidence",
                ))
            if proposal.market_regime and proposal.market_regime != regime.regime:
                objections.append(CriticObjection(
                    code="regime_mismatch",
                    severity="medium",
                    message=(
                        f"proposal regime {proposal.market_regime} "
                        f"!= current {regime.regime}"
                    ),
                ))

        feat_map: dict[str, Any] = {}
        if features is not None:
            feat_map = dict(getattr(features, "features", None) or {})
            if not feat_map and hasattr(features, "get"):
                # FeatureSnapshot may expose get()
                try:
                    for key in ("realized_vol", "rel_volume_5", "momentum_10", "rsi_14", "atr_pct"):
                        feat_map[key] = features.get(key, 0.0)
                except Exception:
                    pass

            vol = float(feat_map.get("realized_vol") or features.get("realized_vol", 0.0) or 0.0)
            if vol > 0.03:
                objections.append(CriticObjection(
                    code="elevated_volatility",
                    severity="medium",
                    message=f"realized_vol {vol:.4f} is elevated",
                ))
                modifications.append("consider reduced size")

            rel_vol = float(feat_map.get("rel_volume_5") or features.get("rel_volume_5", 1.0) or 1.0)
            if rel_vol < 0.5 and proposal.action in ("BUY", "SELL"):
                objections.append(CriticObjection(
                    code="weak_volume",
                    severity="medium",
                    message=f"relative volume {rel_vol:.2f} is weak",
                ))

        # Research heuristic scorer – never sole authority; only adds objections
        if self.use_heuristic_scorer and proposal.action in ("BUY", "SELL"):
            try:
                from .ml_scorer import HeuristicScorer

                score = HeuristicScorer().score(feat_map, action=proposal.action)
                scorer_meta = score.as_dict()
                if score.score <= self.scorer_weak_threshold:
                    objections.append(CriticObjection(
                        code="weak_heuristic_score",
                        severity="medium",
                        message=(
                            f"heuristic score {score.score:.2f} ({score.label}); "
                            f"{', '.join(score.reasons) or 'no reasons'}"
                        ),
                    ))
                    modifications.append("wait for stronger feature alignment")
            except Exception as exc:
                scorer_meta = {"error": str(exc)}

        if proposal.conflicting_signals:
            objections.append(CriticObjection(
                code="proposer_conflicts",
                severity="low",
                message=f"proposer listed conflicts: {', '.join(proposal.conflicting_signals)}",
            ))

        if extra_signals:
            for sig in extra_signals:
                objections.append(CriticObjection(
                    code="extra_signal",
                    severity="low",
                    message=str(sig),
                ))

        high = sum(1 for o in objections if o.severity == "high")
        medium = sum(1 for o in objections if o.severity == "medium")

        if high >= 1:
            result = "REJECT"
            confidence = 0.85
            reason = "one or more high-severity objections"
        elif medium >= 2:
            result = "REJECT"
            confidence = 0.70
            reason = "multiple medium-severity objections"
        elif medium == 1 or (high == 0 and objections):
            result = "WARN"
            confidence = 0.55
            reason = "warnings present but not blocking"
        else:
            result = "APPROVE"
            confidence = 0.80
            reason = "no material objections"

        return CriticDecision(
            result=result,
            confidence=round(confidence, 4),
            objections=tuple(objections),
            recommended_modifications=tuple(modifications),
            reason=reason,
            meta={
                "high_count": high,
                "medium_count": medium,
                "low_count": len(objections) - high - medium,
                "heuristic_scorer": scorer_meta,
            },
        )


__all__ = ["CriticObjection", "CriticDecision", "CriticAgent"]
