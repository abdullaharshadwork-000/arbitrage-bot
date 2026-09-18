"""Research-only reinforcement learning environment.

Phase 26 foundation.

Provides a minimal Gym-like interface over historical price series for
offline experimentation. NEVER connected to live execution or Binance.

Reward is NOT raw PnL alone – includes drawdown and cost penalties.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence


ACTIONS = ("HOLD", "BUY", "SELL", "CLOSE", "REDUCE")


@dataclass
class RLState:
    step: int
    price: float
    position: float  # -1..1 normalized
    equity: float
    peak_equity: float
    features: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "price": self.price,
            "position": self.position,
            "equity": self.equity,
            "peak_equity": self.peak_equity,
            "drawdown": self.peak_equity - self.equity,
            "features": dict(self.features),
        }


class ResearchRLEnv:
    """Offline RL env over a fixed price series. Research only."""

    def __init__(
        self,
        prices: Sequence[float],
        *,
        start_equity: float = 10_000.0,
        fee_rate: float = 0.001,
        drawdown_penalty: float = 0.5,
        cost_penalty: float = 1.0,
    ):
        self.prices = [float(p) for p in prices if float(p) > 0]
        if len(self.prices) < 2:
            raise ValueError("need at least 2 prices")
        self.start_equity = float(start_equity)
        self.fee_rate = float(fee_rate)
        self.drawdown_penalty = float(drawdown_penalty)
        self.cost_penalty = float(cost_penalty)
        self.reset()

    def reset(self) -> RLState:
        self._i = 0
        self._position = 0.0
        self._cash = self.start_equity
        self._equity = self.start_equity
        self._peak = self.start_equity
        return self._state()

    def _state(self) -> RLState:
        px = self.prices[self._i]
        return RLState(
            step=self._i,
            price=px,
            position=self._position,
            equity=self._equity,
            peak_equity=self._peak,
            features={
                "return_1": (
                    (px / self.prices[self._i - 1] - 1.0) if self._i > 0 else 0.0
                ),
            },
        )

    def step(self, action: str) -> tuple[RLState, float, bool, dict[str, Any]]:
        action = (action or "HOLD").upper()
        if action not in ACTIONS:
            action = "HOLD"

        px = self.prices[self._i]
        cost = 0.0
        prev_equity = self._equity

        if action == "BUY" and self._position <= 0:
            self._position = 1.0
            cost = abs(self._cash * self.fee_rate)
            self._cash -= cost
        elif action == "SELL" and self._position >= 0:
            self._position = -1.0
            cost = abs(self._cash * self.fee_rate)
            self._cash -= cost
        elif action == "CLOSE":
            cost = abs(self._cash * self.fee_rate * abs(self._position))
            self._cash -= cost
            self._position = 0.0
        elif action == "REDUCE":
            self._position *= 0.5
            cost = abs(self._cash * self.fee_rate * 0.5)
            self._cash -= cost

        self._i += 1
        done = self._i >= len(self.prices) - 1
        new_px = self.prices[min(self._i, len(self.prices) - 1)]

        # Mark equity
        ret = (new_px / px - 1.0) if px else 0.0
        self._equity = self._cash * (1.0 + self._position * ret)
        self._peak = max(self._peak, self._equity)
        dd = self._peak - self._equity

        raw = self._equity - prev_equity
        reward = raw - self.drawdown_penalty * max(0.0, dd / max(self._peak, 1.0)) - self.cost_penalty * cost

        info = {
            "action": action,
            "cost": cost,
            "raw_pnl": raw,
            "drawdown": dd,
            "research_only": True,
        }
        return self._state(), float(reward), done, info


__all__ = ["ACTIONS", "RLState", "ResearchRLEnv"]
