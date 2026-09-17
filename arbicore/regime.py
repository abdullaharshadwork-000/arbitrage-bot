"""Market Regime Intelligence.

Phase 4 foundation.

Classifies the current market into a small set of regimes using only
measurable features.  No LLM is involved.  Output is always structured and
includes confidence + supporting / conflicting evidence.

Regimes are deliberately coarse so they are stable enough for strategy
selection and risk adjustment.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .features import FeatureSnapshot


# Canonical regime labels (stable contract for later strategy selection)
REGIME_STRONG_BULL = "STRONG_BULL_TREND"
REGIME_WEAK_BULL = "WEAK_BULL_TREND"
REGIME_STRONG_BEAR = "STRONG_BEAR_TREND"
REGIME_WEAK_BEAR = "WEAK_BEAR_TREND"
REGIME_SIDEWAYS_LOW_VOL = "SIDEWAYS_LOW_VOL"
REGIME_SIDEWAYS_HIGH_VOL = "SIDEWAYS_HIGH_VOL"
REGIME_BREAKOUT = "BREAKOUT"
REGIME_VOL_EXPANSION = "VOLATILITY_EXPANSION"
REGIME_VOL_COMPRESSION = "VOLATILITY_COMPRESSION"
REGIME_UNCERTAIN = "UNCERTAIN"
REGIME_WARMING_UP = "WARMING_UP"


@dataclass(frozen=True)
class RegimeDecision:
    regime: str
    confidence: float
    supporting: tuple[str, ...] = ()
    conflicting: tuple[str, ...] = ()
    previous_regime: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


class RegimeDetector:
    """Deterministic regime classifier driven by FeatureSnapshot."""

    def __init(
        self,
        *,
        vol_high: float = 0.012,
        vol_low: float = 0.004,
        momentum_strong: float = 0.025,
        momentum_weak: float = 0.008,
        min_bars: int = 10,
    ):
        self.vol_high = vol_high
        self.vol_low = vol_low
        self.momentum_strong = momentum_strong
        self.momentum_weak = momentum_weak
        self.min_bars = min_bars
        self._last_regime: Optional[str] = None

    def classify(self, snapshot: FeatureSnapshot) -> RegimeDecision:
        bars = int(snapshot.meta.get("bars") or 0)
        if bars < self.min_bars or snapshot.meta.get("status") != "ok":
            decision = RegimeDecision(
                regime=REGIME_WARMING_UP,
                confidence=0.0,
                supporting=("insufficient feature history",),
                previous_regime=self._last_regime,
                meta={"bars": bars},
            )
            self._last_regime = decision.regime
            return decision

        mom = snapshot.get("momentum_10", snapshot.get("momentum_5", 0.0))
        vol = snapshot.get("realized_vol", 0.0)
        dist_sma = snapshot.get("dist_sma_20", snapshot.get("dist_sma_10", 0.0))

        supporting: list[str] = []
        conflicting: list[str] = []

        # Volatility regime first
        if vol >= self.vol_high:
            supporting.append(f"realized_vol={vol:.4f} >= {self.vol_high}")
            high_vol = True
        elif vol <= self.vol_low:
            supporting.append(f"realized_vol={vol:.4f} <= {self.vol_low}")
            high_vol = False
        else:
            high_vol = None

        # Directional bias
        if mom >= self.momentum_strong and dist_sma > 0:
            regime = REGIME_STRONG_BULL if not high_vol else REGIME_VOL_EXPANSION
            supporting.append(f"strong positive momentum={mom:.4f}")
            confidence = min(0.95, 0.55 + abs(mom) * 8)
        elif mom <= -self.momentum_strong and dist_sma < 0:
            regime = REGIME_STRONG_BEAR if not high_vol else REGIME_VOL_EXPANSION
            supporting.append(f"strong negative momentum={mom:.4f}")
            confidence = min(0.95, 0.55 + abs(mom) * 8)
        elif abs(mom) >= self.momentum_weak:
            if mom > 0:
                regime = REGIME_WEAK_BULL
                supporting.append(f"weak positive momentum={mom:.4f}")
            else:
                regime = REGIME_WEAK_BEAR
                supporting.append(f"weak negative momentum={mom:.4f}")
            confidence = 0.45 + abs(mom) * 5
        else:
            # Sideways
            if high_vol is True:
                regime = REGIME_SIDEWAYS_HIGH_VOL
                supporting.append("low momentum + elevated volatility")
            elif high_vol is False:
                regime = REGIME_SIDEWAYS_LOW_VOL
                supporting.append("low momentum + compressed volatility")
            else:
                regime = REGIME_UNCERTAIN
                supporting.append("mixed signals")
            confidence = 0.40

        # Simple hysteresis: avoid rapid flips when confidence is marginal
        if (
            self._last_regime
            and self._last_regime != regime
            and confidence < 0.60
            and self._last_regime not in (REGIME_WARMING_UP, REGIME_UNCERTAIN)
        ):
            conflicting.append(
                f"holding previous regime {self._last_regime} (low confidence flip)"
            )
            regime = self._last_regime
            confidence = max(0.35, confidence - 0.10)

        decision = RegimeDecision(
            regime=regime,
            confidence=round(min(0.99, max(0.0, confidence)), 4),
            supporting=tuple(supporting),
            conflicting=tuple(conflicting),
            previous_regime=self._last_regime,
            meta={
                "momentum": mom,
                "realized_vol": vol,
                "dist_sma": dist_sma,
                "bars": bars,
            },
        )
        self._last_regime = decision.regime
        return decision


__all__ = [
    "REGIME_STRONG_BULL",
    "REGIME_WEAK_BULL",
    "REGIME_STRONG_BEAR",
    "REGIME_WEAK_BEAR",
    "REGIME_SIDEWAYS_LOW_VOL",
    "REGIME_SIDEWAYS_HIGH_VOL",
    "REGIME_BREAKOUT",
    "REGIME_VOL_EXPANSION",
    "REGIME_VOL_COMPRESSION",
    "REGIME_UNCERTAIN",
    "REGIME_WARMING_UP",
    "RegimeDecision",
    "RegimeDetector",
]
