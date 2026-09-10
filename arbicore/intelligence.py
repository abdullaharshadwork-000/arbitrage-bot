"""Transparent, conservative market intelligence for arbitrage decisions.

This module deliberately does not claim to know the future.  It estimates the
short-horizon uncertainty between observing an arbitrage edge and submitting
its orders.  The estimate is based only on recent, in-process quote changes and
is returned with its sample count and assumptions so an operator can audit it.

The decision layer is advisory in paper mode and fail-closed in real execution:
real candidates need enough observations, a non-stressed market regime, and an
edge that clears an adaptive volatility/latency buffer.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass


def _finite(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def _standard_deviation(values):
    values = list(values)
    return statistics.pstdev(values) if len(values) > 1 else 0.0


@dataclass(frozen=True)
class IntelligenceDecision:
    qualified: bool
    confidence: float
    observed_edge_pct: float
    predicted_edge_pct: float
    adaptive_floor_pct: float
    uncertainty_pct: float
    volatility_pct: float
    observations: int
    regime: str
    reason: str

    def as_dict(self):
        return asdict(self)


class OpportunityIntelligence:
    """Online next-scan forecast and risk-adjusted opportunity scorer.

    Returns are stored in percentage points (``0.10`` means 0.10%), matching
    the engine's profit and slippage settings.  Nothing here can place an order.
    """

    def __init__(self, max_history=180, min_observations=8,
                 stressed_volatility_pct=0.35, clock=time.monotonic):
        self.max_history = max(16, int(max_history))
        self.min_observations = max(3, int(min_observations))
        self.stressed_volatility_pct = max(
            0.01, float(stressed_volatility_pct))
        self._clock = clock
        self._last_mid = {}
        self._returns = defaultdict(lambda: deque(maxlen=self.max_history))
        self._spreads = defaultdict(lambda: deque(maxlen=self.max_history))
        self._fetch_seconds = 0.0
        self._observed_at = None
        self._regime = "warming_up"
        self._latest_decisions = []

    @staticmethod
    def market_key(exchange, symbol):
        return f"{str(exchange).lower()}:{str(symbol).upper()}"

    def reset(self):
        self._last_mid.clear()
        self._returns.clear()
        self._spreads.clear()
        self._fetch_seconds = 0.0
        self._observed_at = None
        self._regime = "warming_up"
        self._latest_decisions = []

    def observe(self, quotes, fetch_seconds=0.0):
        """Consume one validated quote snapshot and update next-scan forecasts."""
        for symbol, venues in (quotes or {}).items():
            for exchange, quote in (venues or {}).items():
                try:
                    bid = float(quote.get("bid"))
                    ask = float(quote.get("ask"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if (not math.isfinite(bid) or not math.isfinite(ask)
                        or bid <= 0 or ask <= 0 or ask < bid):
                    continue
                key = self.market_key(exchange, symbol)
                mid = (bid + ask) / 2.0
                spread_pct = (ask - bid) / mid * 100.0
                previous = self._last_mid.get(key)
                if previous and previous > 0:
                    change_pct = (mid - previous) / previous * 100.0
                    if math.isfinite(change_pct):
                        self._returns[key].append(change_pct)
                self._spreads[key].append(spread_pct)
                self._last_mid[key] = mid

        self._fetch_seconds = max(0.0, _finite(fetch_seconds))
        self._observed_at = self._clock()
        volatility = self.aggregate_volatility()
        observations = self.observation_count()
        momentum = self.aggregate_momentum()
        if observations < self.min_observations:
            self._regime = "warming_up"
        elif volatility >= self.stressed_volatility_pct or self._fetch_seconds > 3.0:
            self._regime = "stressed"
        elif volatility >= self.stressed_volatility_pct * 0.45:
            self._regime = "volatile"
        elif abs(momentum) >= max(0.04, volatility * 0.75):
            self._regime = "trending"
        else:
            self._regime = "stable"
        return self.snapshot()

    def observation_count(self, keys=None):
        selected = list(keys) if keys is not None else list(self._returns)
        counts = [len(self._returns[key]) for key in selected if key in self._returns]
        if keys is not None:
            return min(counts) if len(counts) == len(selected) and counts else 0
        return sum(counts)

    def aggregate_volatility(self, keys=None):
        selected = list(keys) if keys is not None else list(self._returns)
        deviations = [
            _standard_deviation(self._returns[key])
            for key in selected if self._returns.get(key)
        ]
        # A route can be hurt by movement in any leg; root-sum-square is more
        # conservative than averaging unrelated leg volatility away.
        squared = sum(value * value for value in deviations)
        if keys is None and deviations:
            return math.sqrt(squared / len(deviations))
        return math.sqrt(squared)

    def aggregate_momentum(self, keys=None):
        selected = list(keys) if keys is not None else list(self._returns)
        values = []
        for key in selected:
            recent = list(self._returns.get(key) or ())[-8:]
            if recent:
                # New observations receive more weight without an ML dependency
                # or an opaque fitted model.
                weights = range(1, len(recent) + 1)
                values.append(sum(v * w for v, w in zip(recent, weights))
                              / sum(weights))
        return statistics.fmean(values) if values else 0.0

    def _candidate_keys(self, candidate):
        cycle = candidate.get("cycle") or {}
        if cycle:
            exchange = candidate.get("buy_exchange")
            return [self.market_key(exchange, symbol)
                    for symbol in cycle.get("symbols") or ()]
        symbol = candidate.get("symbol")
        return [
            self.market_key(candidate.get("buy_exchange"), symbol),
            self.market_key(candidate.get("sell_exchange"), symbol),
        ]

    def evaluate(self, candidate, min_profit_pct, max_slippage_pct,
                 min_confidence=0.65):
        """Return an auditable estimate of whether an edge can survive a scan.

        ``qualified`` is intentionally conservative.  Paper execution may still
        collect the opportunity, but real execution should treat ``False`` as a
        veto rather than as a suggestion.
        """
        edge = _finite(candidate.get("net_pct"))
        keys = self._candidate_keys(candidate)
        observations = self.observation_count(keys)
        volatility = self.aggregate_volatility(keys)
        legs = max(1, int(candidate.get("legs") or len(keys) or 1))
        warmup_gap = max(0, self.min_observations - observations)

        # Recent price dispersion, slow collection, and an uncalibrated warm-up
        # all make the scan-time edge less trustworthy.  Cap the uncertainty at
        # the operator's explicit slippage tolerance per route.
        volatility_buffer = volatility * math.sqrt(legs)
        latency_buffer = volatility_buffer * min(2.0, self._fetch_seconds / 1.5)
        warmup_buffer = (warmup_gap / self.min_observations) * 0.05
        uncertainty = max(0.01, volatility_buffer + latency_buffer + warmup_buffer)
        uncertainty = min(max(0.01, float(max_slippage_pct) * legs), uncertainty)
        floor = float(min_profit_pct) + uncertainty
        predicted = edge - uncertainty

        scale = max(0.025, uncertainty)
        raw_confidence = 1.0 / (1.0 + math.exp(-_clamp(
            (edge - floor) / scale, -30.0, 30.0)))
        maturity = _clamp(observations / self.min_observations)
        confidence = 0.5 + (raw_confidence - 0.5) * maturity

        if observations < self.min_observations:
            candidate_regime = "warming_up"
        elif volatility >= self.stressed_volatility_pct or self._fetch_seconds > 3.0:
            candidate_regime = "stressed"
        elif volatility >= self.stressed_volatility_pct * 0.45:
            candidate_regime = "volatile"
        else:
            candidate_regime = "stable"

        reasons = []
        if observations < self.min_observations:
            reasons.append(
                f"forecast warming up ({observations}/{self.min_observations} observations)")
        if candidate_regime == "stressed":
            reasons.append("market regime is stressed")
        if edge < floor:
            reasons.append(
                f"{edge:.3f}% edge does not clear the {floor:.3f}% adaptive floor")
        if confidence < float(min_confidence):
            reasons.append(
                f"{confidence * 100:.1f}% confidence is below the "
                f"{float(min_confidence) * 100:.1f}% requirement")
        qualified = not reasons
        reason = "Qualified by the volatility and latency model." if qualified else "; ".join(reasons)
        decision = IntelligenceDecision(
            qualified=qualified,
            confidence=round(confidence, 6),
            observed_edge_pct=round(edge, 6),
            predicted_edge_pct=round(predicted, 6),
            adaptive_floor_pct=round(floor, 6),
            uncertainty_pct=round(uncertainty, 6),
            volatility_pct=round(volatility, 6),
            observations=observations,
            regime=candidate_regime,
            reason=reason,
        )
        self._latest_decisions.insert(0, decision.as_dict())
        self._latest_decisions = self._latest_decisions[:20]
        return decision

    def forecasts(self, limit=12):
        rows = []
        for key, values in self._returns.items():
            recent = list(values)[-20:]
            if not recent:
                continue
            mean = statistics.fmean(recent)
            deviation = _standard_deviation(recent)
            rows.append({
                "market": key,
                "observations": len(values),
                "next_scan_change_pct": round(mean, 6),
                "lower_95_pct": round(mean - 1.96 * deviation, 6),
                "upper_95_pct": round(mean + 1.96 * deviation, 6),
            })
        rows.sort(key=lambda row: abs(row["next_scan_change_pct"]), reverse=True)
        return rows[:max(1, int(limit))]

    def snapshot(self):
        observations = self.observation_count()
        return {
            "model": "transparent-ewma-v1",
            "purpose": "estimate whether an arbitrage edge survives until execution",
            "regime": self._regime,
            "ready": observations >= self.min_observations and self._regime != "stressed",
            "observations": observations,
            "minimum_observations": self.min_observations,
            "volatility_pct": round(self.aggregate_volatility(), 6),
            "momentum_pct": round(self.aggregate_momentum(), 6),
            "fetch_seconds": round(self._fetch_seconds, 6),
            "observed_at": self._observed_at,
            "forecasts": self.forecasts(),
            "latest_decisions": list(self._latest_decisions),
            "warning": "Probabilistic estimate only; it cannot guarantee profit.",
        }


def strategy_evidence(trades, minimum_samples=30):
    """Rank strategies by conservative realized expectancy.

    A strategy is recommended only when its 95% lower confidence estimate is
    positive.  This avoids calling a lucky handful of trades an AI discovery.
    """
    groups = defaultdict(list)
    for trade in trades or ():
        try:
            profit = float(trade.get("profit_usdt"))
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(profit):
            groups[str(trade.get("strategy") or "unknown")].append(profit)

    ranked = []
    for name, profits in groups.items():
        count = len(profits)
        average = statistics.fmean(profits)
        deviation = _standard_deviation(profits)
        standard_error = deviation / math.sqrt(count) if count else 0.0
        conservative = average - 1.96 * standard_error
        ranked.append({
            "strategy": name,
            "trades": count,
            "total_profit_usdt": round(sum(profits), 8),
            "average_profit_usdt": round(average, 8),
            "win_rate": round(sum(value > 0 for value in profits) / count * 100, 4),
            "lower_95_expectancy_usdt": round(conservative, 8),
            "qualified": count >= int(minimum_samples) and conservative > 0,
        })
    ranked.sort(key=lambda row: (
        row["qualified"], row["lower_95_expectancy_usdt"], row["trades"]), reverse=True)
    recommended = ranked[0]["strategy"] if ranked and ranked[0]["qualified"] else None
    return {
        "recommended_strategy": recommended,
        "minimum_samples": int(minimum_samples),
        "strategies": ranked,
        "message": (f"{recommended} has positive conservative expectancy."
                    if recommended else
                    "No strategy has enough statistically positive evidence yet."),
    }
