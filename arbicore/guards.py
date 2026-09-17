"""Explicit live-mode and real-trading guards.

Phase 1 addition.

These helpers sit above the existing Settings.real property and make the
rules that govern real-money trading impossible to miss or silently bypass.

Design rules:

* LIVE / real trading can never be enabled by an agent, an LLM, or a default.
* Only an explicit operator acknowledgement unlocks real orders.
* The Risk Kernel remains the final authority on every trade.
* Nothing in this module places orders or weakens risk limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import EXECUTION_REAL, MODE_LIVE, REAL_TRADING_ACK, Settings
from .domain import AuditCategory, AuditEvent, OperatingMode


@dataclass(frozen=True)
class GuardDecision:
    """Result of a live-mode or real-trading check."""

    allowed: bool
    reason: str = ""
    required_action: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class LiveModeGuard:
    """Deterministic gate that decides whether real orders are permitted.

    This class does not replace Settings.real — it makes the same rules
    explicit, auditable, and impossible for a future agent to ignore.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------ checks

    def can_use_live_data(self) -> GuardDecision:
        """May the system consume live public market data?"""
        if self.settings.mode != MODE_LIVE:
            return GuardDecision(
                False,
                f"mode is {self.settings.mode!r}; live data requires mode={MODE_LIVE!r}",
                f"Set MODE = {MODE_LIVE!r}",
            )
        return GuardDecision(True, "live market data is permitted")

    def can_place_real_orders(self) -> GuardDecision:
        """May the system send real orders to an exchange?

        All of the following must be true:
        1. mode == live
        2. execution_mode == real
        3. real_trading_ack is the exact acknowledgement string
        4. every configured exchange has complete credentials
        """
        if self.settings.mode != MODE_LIVE:
            return GuardDecision(
                False,
                f"real orders require mode={MODE_LIVE!r}, got {self.settings.mode!r}",
                f"Set MODE = {MODE_LIVE!r}",
            )
        if self.settings.execution_mode != EXECUTION_REAL:
            return GuardDecision(
                False,
                f"real orders require execution_mode={EXECUTION_REAL!r}, "
                f"got {self.settings.execution_mode!r}",
                f"Set EXECUTION_MODE = {EXECUTION_REAL!r}",
            )
        if self.settings.real_trading_ack != REAL_TRADING_ACK:
            return GuardDecision(
                False,
                "real orders require the exact acknowledgement string",
                f'Set REAL_TRADING_ACK = "{REAL_TRADING_ACK}"',
            )
        missing = []
        for exchange in self.settings.exchanges:
            gaps = self.settings.credential_for(exchange).missing()
            if gaps:
                missing.append(f"{exchange}: {', '.join(gaps)}")
        if missing:
            return GuardDecision(
                False,
                "credentials incomplete: " + "; ".join(missing),
                "Supply complete API credentials via environment variables",
            )
        return GuardDecision(True, "all real-trading gates are open")

    def operating_mode(self) -> OperatingMode:
        """Map current Settings onto the explicit OperatingMode enum."""
        if self.settings.real:
            return OperatingMode.LIVE
        if self.settings.mode == MODE_LIVE and self.settings.execution_mode != EXECUTION_REAL:
            # Live data + paper execution is the normal safe operating state.
            return OperatingMode.PAPER
        if self.settings.mode == MODE_LIVE:
            return OperatingMode.PAPER
        return OperatingMode.RESEARCH

    # ------------------------------------------------------------------ audit

    def audit_real_trading_check(self) -> AuditEvent:
        """Produce an AuditEvent for the current real-trading decision."""
        decision = self.can_place_real_orders()
        return AuditEvent.create(
            AuditCategory.SAFETY,
            "real_trading_check",
            component="LiveModeGuard",
            mode=self.operating_mode(),
            payload={
                "allowed": decision.allowed,
                "reason": decision.reason,
                "required_action": decision.required_action,
                "settings_mode": self.settings.mode,
                "settings_execution_mode": self.settings.execution_mode,
                "ack_present": self.settings.real_trading_ack == REAL_TRADING_ACK,
            },
            reason=decision.reason,
        )


def assert_real_trading_permitted(settings: Settings) -> None:
    """Raise RuntimeError if real orders are not fully unlocked.

    Call this immediately before any code path that can submit a real order.
    It is intentionally strict and does not attempt recovery.
    """
    guard = LiveModeGuard(settings)
    decision = guard.can_place_real_orders()
    if not decision:
        raise RuntimeError(
            f"Real trading is not permitted: {decision.reason}. "
            f"{decision.required_action}"
        )


__all__ = [
    "GuardDecision",
    "LiveModeGuard",
    "assert_real_trading_permitted",
]
