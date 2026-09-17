"""Signal engine skeleton.

Phase 23.

Turns FeatureSnapshot + RegimeDecision + selected strategy into a structured
TradeProposal. Does NOT place orders. Does NOT bypass Critic or Risk Kernel.

Conservative defaults:
* Low confidence → NO_TRADE
* Missing stops/targets for directional ideas are filled with transparent rules
* Conflicting signals are listed on the proposal for the Critic
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from .domain import TradeProposal
from .features import FeatureSnapshot
from .regime import RegimeDecision
from .selection import SelectionResult


@dataclass(frozen=True)
class SignalConfig:
    min_momentum: float = 0.012
    min_regime_confidence: float = 0.45
    min_trade_confidence: float = 0.50
    stop_pct: float = 0.008          # 0.8% stop
    target_pct: float = 0.016        # 1.6% target → ~2R
    risk_fraction: str = "0.005"     # 0.5% of equity requested (Risk Kernel may cut)


class SignalEngine:
    """Produce a TradeProposal from the current market context."""

    def __init__(self, config: Optional[SignalConfig] = None):
        self.config = config or SignalConfig()

    def propose(
        self,
        symbol: str,
        features: FeatureSnapshot,
        regime: RegimeDecision,
        selection: SelectionResult,
        *,
        last_price: Optional[float] = None,
    ) -> TradeProposal:
        price = float(last_price if last_price is not None else features.get("price", 0.0))
        mom = features.get("momentum_10", features.get("momentum_5", 0.0))
        vol = features.get("realized_vol", 0.0)

        supporting: list[str] = []
        conflicting: list[str] = []

        # Default: no trade
        action = "NO_TRADE"
        confidence = 0.0
        reason = "no actionable edge"

        if selection.action != "USE_STRATEGY":
            reason = selection.reason or "no strategy selected"
            return self._proposal(
                symbol, action, selection, regime, confidence, reason,
                supporting, conflicting, price,
            )

        if regime.confidence < self.config.min_regime_confidence:
            reason = f"regime confidence {regime.confidence:.2f} below minimum"
            conflicting.append(reason)
            return self._proposal(
                symbol, action, selection, regime, confidence, reason,
                supporting, conflicting, price,
            )

        if price <= 0:
            reason = "invalid price"
            return self._proposal(
                symbol, action, selection, regime, confidence, reason,
                supporting, conflicting, price,
            )

        # Directional rules (transparent, conservative)
        bullish = regime.regime in ("STRONG_BULL_TREND", "WEAK_BULL_TREND")
        bearish = regime.regime in ("STRONG_BEAR_TREND", "WEAK_BEAR_TREND")

        if bullish and mom >= self.config.min_momentum:
            action = "BUY"
            confidence = min(0.85, 0.40 + abs(mom) * 8 + regime.confidence * 0.25)
            supporting.append(f"bullish regime={regime.regime}")
            supporting.append(f"momentum={mom:.4f}")
            reason = "momentum aligned with bullish regime"
        elif bearish and mom <= -self.config.min_momentum:
            action = "SELL"
            confidence = min(0.85, 0.40 + abs(mom) * 8 + regime.confidence * 0.25)
            supporting.append(f"bearish regime={regime.regime}")
            supporting.append(f"momentum={mom:.4f}")
            reason = "momentum aligned with bearish regime"
        else:
            if abs(mom) < self.config.min_momentum:
                conflicting.append(f"momentum {mom:.4f} below threshold")
            if not (bullish or bearish):
                conflicting.append(f"regime {regime.regime} not directional")
            reason = "signals not aligned for entry"

        if vol > 0.025:
            conflicting.append(f"elevated volatility {vol:.4f}")
            confidence *= 0.85

        if action in ("BUY", "SELL") and confidence < self.config.min_trade_confidence:
            conflicting.append(
                f"confidence {confidence:.2f} below minimum "
                f"{self.config.min_trade_confidence:.2f}"
            )
            action = "NO_TRADE"
            reason = "confidence too low after adjustments"
            confidence = 0.0

        return self._proposal(
            symbol, action, selection, regime, confidence, reason,
            supporting, conflicting, price,
        )

    def _proposal(
        self,
        symbol: str,
        action: str,
        selection: SelectionResult,
        regime: RegimeDecision,
        confidence: float,
        reason: str,
        supporting: list[str],
        conflicting: list[str],
        price: float,
    ) -> TradeProposal:
        entry = Decimal(str(round(price, 8))) if price > 0 else None
        stop = take = None
        rr = None
        risk_frac = Decimal(self.config.risk_fraction)

        if action == "BUY" and entry is not None:
            stop = entry * (Decimal("1") - Decimal(str(self.config.stop_pct)))
            take = entry * (Decimal("1") + Decimal(str(self.config.target_pct)))
            rr = self.config.target_pct / self.config.stop_pct
        elif action == "SELL" and entry is not None:
            stop = entry * (Decimal("1") + Decimal(str(self.config.stop_pct)))
            take = entry * (Decimal("1") - Decimal(str(self.config.target_pct)))
            rr = self.config.target_pct / self.config.stop_pct

        if action == "NO_TRADE":
            risk_frac = Decimal("0")

        return TradeProposal(
            id=f"prop_{uuid4().hex[:12]}",
            symbol=symbol,
            action=action,
            strategy_id=selection.strategy_id or "none",
            strategy_version=selection.strategy_version or "none",
            market_regime=regime.regime,
            regime_confidence=regime.confidence,
            trade_confidence=round(float(confidence), 4),
            entry_type="MARKET",
            entry_price=entry,
            stop_loss=stop,
            take_profit=take,
            requested_risk_fraction=risk_frac,
            expected_reward_risk=rr,
            supporting_signals=tuple(supporting),
            conflicting_signals=tuple(conflicting),
            invalidation_conditions=(
                ("momentum flips", "regime leaves directional set")
                if action in ("BUY", "SELL")
                else ()
            ),
            reasoning_summary=reason,
        )


__all__ = ["SignalConfig", "SignalEngine"]
