"""Runtime guards for live execution.

These controls deliberately contain no exchange calls.  The scan loop feeds
them observations and receives a deterministic allow/refuse decision, making
the dangerous boundary small and straightforward to test.
"""

import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from .money import D, ZERO


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reason: str = ""
    limit: str = ""

    def __bool__(self):
        return self.allowed


SAFE = SafetyDecision(True)


class ExecutionSafety:
    """Circuit breakers plus conservative, account-aware order sizing."""

    def __init__(self, clock=time.monotonic, max_quote_age_ms=3_000,
                 max_fetch_seconds=3.0, max_consecutive_feed_failures=3,
                 degradation_window=12, min_realized_edge_ratio="0.50"):
        self._clock = clock
        self.max_quote_age_ms = float(max_quote_age_ms)
        self.max_fetch_seconds = float(max_fetch_seconds)
        self.max_consecutive_feed_failures = int(max_consecutive_feed_failures)
        self.min_realized_edge_ratio = D(min_realized_edge_ratio)
        self.feed_failures = 0
        self.halted = False
        self.halt_limit = ""
        self.halt_reason = ""
        self.halted_at = None
        self.edge_history = deque(maxlen=int(degradation_window))
        self.last_observation = {}

    def halt(self, limit, reason):
        if not self.halted:
            self.halted = True
            self.halt_limit = str(limit)
            self.halt_reason = str(reason)
            self.halted_at = self._clock()
        return SafetyDecision(False, self.halt_reason, self.halt_limit)

    def resume(self):
        self.halted = False
        self.halt_limit = ""
        self.halt_reason = ""
        self.halted_at = None
        self.feed_failures = 0

    def observe_feed(self, quotes, fetch_seconds, required_routes=None):
        """Reject slow, empty, crossed, or stale quote snapshots."""
        ages = []
        quote_count = 0
        invalid = 0
        for venues in (quotes or {}).values():
            for quote in (venues or {}).values():
                quote_count += 1
                try:
                    bid = float(quote.get("bid") or 0)
                    ask = float(quote.get("ask") or 0)
                    age = float(quote.get("age_ms") or 0)
                except (TypeError, ValueError, AttributeError):
                    invalid += 1
                    continue
                ages.append(age)
                if bid <= 0 or ask <= 0 or ask < bid:
                    invalid += 1

        maximum_age = max(ages, default=0.0)
        self.last_observation = {
            "at": self._clock(), "quotes": quote_count, "invalid": invalid,
            "max_quote_age_ms": maximum_age,
            "fetch_seconds": float(fetch_seconds or 0.0),
        }
        viable_routes = 0
        for route in required_routes or []:
            symbols = route.get("symbols") or []
            exchange = route.get("exchange")
            minimum_venues = int(route.get("min_venues") or 1)
            if exchange:
                complete = all(exchange in (quotes or {}).get(symbol, {})
                               for symbol in symbols)
            else:
                complete = all(len((quotes or {}).get(symbol, {})) >= minimum_venues
                               for symbol in symbols)
            viable_routes += int(complete)
        self.last_observation["viable_routes"] = viable_routes
        reason = ""
        limit = ""
        if quote_count == 0:
            limit, reason = "empty_feed", "the live feed returned no usable quotes"
        elif invalid:
            limit, reason = "invalid_feed", f"the live feed returned {invalid} invalid quote(s)"
        elif required_routes and viable_routes == 0:
            limit, reason = "route_coverage", (
                "the live feed has no complete executable route")
        elif maximum_age > self.max_quote_age_ms:
            limit, reason = "stale_feed", (
                f"quote age {maximum_age:.0f}ms exceeds the "
                f"{self.max_quote_age_ms:.0f}ms execution limit")
        elif float(fetch_seconds or 0.0) > self.max_fetch_seconds:
            limit, reason = "feed_latency", (
                f"quote fetch took {float(fetch_seconds):.3f}s, above the "
                f"{self.max_fetch_seconds:.3f}s execution limit")

        if reason:
            self.feed_failures += 1
            if self.feed_failures >= self.max_consecutive_feed_failures:
                return self.halt(limit, reason)
            return SafetyDecision(False, reason, limit)
        self.feed_failures = 0
        return SAFE

    def dynamic_size(self, configured, free_quote, remaining_loss_budget,
                     visible_depth=None, volatility_pct=0,
                     worst_case_loss_pct="1.0"):
        """Return a size capped by cash, depth, loss budget, and volatility.

        This only reduces the operator's configured size.  It can never raise
        it, and reserves 2% of free quote currency for fees and rounding.
        """
        budget = D(remaining_loss_budget)
        # An exhausted or unknown loss allowance must prevent new exposure;
        # skipping this cap would restore the full size at the daily limit.
        if not budget.is_finite() or budget <= ZERO:
            return ZERO
        caps = [D(configured), D(free_quote) * Decimal("0.49")]
        if visible_depth is not None:
            caps.append(D(visible_depth) * Decimal("0.20"))
        loss_fraction = max(
            Decimal("0.0001"), D(worst_case_loss_pct) / Decimal("100"))
        caps.append(budget / loss_fraction)
        volatility = max(ZERO, D(volatility_pct))
        volatility_factor = Decimal("1") / (Decimal("1") + volatility / Decimal("2"))
        return max(ZERO, min(caps) * volatility_factor)

    def record_execution(self, expected_profit, realized_profit):
        expected = D(expected_profit)
        if expected <= ZERO:
            return SAFE
        ratio = D(realized_profit) / expected
        self.edge_history.append(ratio)
        if len(self.edge_history) < self.edge_history.maxlen:
            return SAFE
        average = sum(self.edge_history, ZERO) / len(self.edge_history)
        if average < self.min_realized_edge_ratio:
            return self.halt(
                "execution_degradation",
                f"realized edge averaged {float(average) * 100:.1f}% of expected "
                f"across {len(self.edge_history)} trades")
        return SAFE

    def snapshot(self):
        average = (sum(self.edge_history, ZERO) / len(self.edge_history)
                   if self.edge_history else None)
        return {
            "halted": self.halted,
            "halt_limit": self.halt_limit,
            "halt_reason": self.halt_reason,
            "halted_at": self.halted_at,
            "feed_failures": self.feed_failures,
            "max_quote_age_ms": self.max_quote_age_ms,
            "max_fetch_seconds": self.max_fetch_seconds,
            "realized_edge_ratio": float(average) if average is not None else None,
            "edge_samples": len(self.edge_history),
            "last_observation": dict(self.last_observation),
        }
