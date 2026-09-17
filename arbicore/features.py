"""Unified Feature Engine – timestamp-safe market features.

Phase 3 foundation.

Goals:
* One implementation used by live, paper, shadow, and later backtests.
* Strictly causal: only data available at or before the decision time.
* No future leakage.
* Pure functions where possible so they are easy to test.

This module currently provides the core primitives and a small set of
price / return features. Later phases will expand indicators, volatility,
volume, and order-book features without changing the public contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator == 0 or not math.isfinite(denominator):
        return default
    result = numerator / denominator
    return result if math.isfinite(result) else default


@dataclass(frozen=True)
class FeatureSnapshot:
    """Immutable bag of features computed at a single decision time."""

    symbol: str
    timestamp: float                    # unix seconds (decision time)
    features: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: float = 0.0) -> float:
        return _finite(self.features.get(name, default), default)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "features": dict(self.features),
            "meta": dict(self.meta),
        }


class FeatureEngine:
    """Compute a FeatureSnapshot from recent price history.

    `prices` must be ordered oldest → newest and must only contain data
    that was available at or before `as_of`. The engine never looks ahead.
    """

    def __init__(self, min_bars: int = 5):
        self.min_bars = max(2, int(min_bars))

    def compute(
        self,
        symbol: str,
        prices: Sequence[float],
        *,
        as_of: Optional[float] = None,
        volumes: Optional[Sequence[float]] = None,
    ) -> FeatureSnapshot:
        """Return a FeatureSnapshot. Empty/short series produce zeroed features."""
        clean = [_finite(p) for p in prices if _finite(p) > 0]
        ts = float(as_of) if as_of is not None else 0.0
        features: dict[str, float] = {}
        meta: dict[str, Any] = {"bars": len(clean)}

        if len(clean) < self.min_bars:
            meta["status"] = "insufficient_data"
            return FeatureSnapshot(symbol=symbol, timestamp=ts, features=features, meta=meta)

        last = clean[-1]
        prev = clean[-2]
        features["price"] = last
        features["return_1"] = _safe_div(last - prev, prev)
        features["log_return_1"] = math.log(last / prev) if prev > 0 and last > 0 else 0.0

        # Short-horizon momentum (last 5 and last 10 bars when available)
        for window in (5, 10, 20):
            if len(clean) >= window:
                past = clean[-window]
                features[f"momentum_{window}"] = _safe_div(last - past, past)

        # Simple moving averages and distance from them
        for window in (5, 10, 20):
            if len(clean) >= window:
                window_slice = clean[-window:]
                sma = sum(window_slice) / window
                features[f"sma_{window}"] = sma
                features[f"dist_sma_{window}"] = _safe_div(last - sma, sma)

        # Realized volatility (std of simple returns over last N bars)
        if len(clean) >= 6:
            returns = [
                _safe_div(clean[i] - clean[i - 1], clean[i - 1])
                for i in range(1, len(clean))
            ]
            recent = returns[-20:] if len(returns) >= 20 else returns
            if len(recent) > 1:
                mean = sum(recent) / len(recent)
                var = sum((r - mean) ** 2 for r in recent) / len(recent)
                features["realized_vol"] = math.sqrt(var)

        # Volume features when supplied
        if volumes is not None:
            vols = [_finite(v) for v in volumes]
            if len(vols) == len(clean) and len(vols) >= 5:
                recent_vol = vols[-5:]
                avg_vol = sum(recent_vol) / len(recent_vol)
                features["volume"] = vols[-1]
                features["rel_volume_5"] = _safe_div(vols[-1], avg_vol, 1.0)

        meta["status"] = "ok"
        return FeatureSnapshot(symbol=symbol, timestamp=ts, features=features, meta=meta)


__all__ = ["FeatureSnapshot", "FeatureEngine"]
