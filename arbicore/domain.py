"""Foundational domain models for the ArbiCore agentic trading platform.

Phase 1 – pure data contracts only.

These types exist so that every later agent, the Risk Kernel, the execution
engine, and the audit trail all speak the same language.  Nothing in this
module places orders, weakens risk limits, or enables LIVE mode.

Design rules enforced here:

* All models are immutable (frozen dataclasses).
* Every critical decision can later be recorded as an AuditEvent.
* Strategy history is versioned; we never overwrite a previous version.
* Operating modes are explicit and ordered by risk.
* No model grants an agent the ability to call Binance directly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid4().hex[:16]}" if prefix else uuid4().hex


# ---------------------------------------------------------------------------
# Operating modes (ordered by increasing real-money risk)
# ---------------------------------------------------------------------------

class OperatingMode(str, Enum):
    """Explicit system mode. LIVE can never be entered silently."""

    RESEARCH = "research"      # historical analysis, no exchange orders
    SHADOW = "shadow"          # live data, hypothetical trades, no orders
    PAPER = "paper"            # persistent simulated capital
    TESTNET = "testnet"        # Binance testnet (or equivalent)
    LIVE = "live"              # real capital – requires explicit operator enablement

    @property
    def allows_real_orders(self) -> bool:
        return self is OperatingMode.LIVE

    @property
    def allows_exchange_data(self) -> bool:
        return self in {
            OperatingMode.SHADOW,
            OperatingMode.PAPER,
            OperatingMode.TESTNET,
            OperatingMode.LIVE,
        }


# ---------------------------------------------------------------------------
# Strategy versioning (never overwrite history)
# ---------------------------------------------------------------------------

class StrategyStatus(str, Enum):
    DRAFT = "draft"
    CANDIDATE = "candidate"
    BACKTESTING = "backtesting"
    FAILED_BACKTEST = "failed_backtest"
    VALIDATING = "validating"
    WALK_FORWARD = "walk_forward"
    SHADOW = "shadow"
    PAPER = "paper"
    TESTNET = "testnet"
    APPROVED = "approved"
    CHALLENGER = "challenger"
    LIVE_CANARY = "live_canary"
    LIVE = "live"
    PAUSED = "paused"
    RETIRED = "retired"
    REJECTED = "rejected"


@dataclass(frozen=True)
class StrategyVersion:
    """Immutable record of one version of a strategy.

    Any parameter change must produce a new StrategyVersion.
    The parent_version_id field creates the genealogy tree.
    """

    id: str
    name: str
    version: str
    parent_version_id: Optional[str] = None
    status: StrategyStatus = StrategyStatus.DRAFT
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    supported_symbols: tuple[str, ...] = ()
    supported_timeframes: tuple[str, ...] = ()
    intended_regimes: tuple[str, ...] = ()
    feature_dependencies: tuple[str, ...] = ()
    creation_reason: str = ""
    hypothesis_id: Optional[str] = None
    created_by: str = "system"
    created_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["created_at"] = self.created_at.isoformat()
        return data


# ---------------------------------------------------------------------------
# Audit trail – every important decision must be reconstructible
# ---------------------------------------------------------------------------

class AuditCategory(str, Enum):
    MARKET_DATA = "market_data"
    FEATURE = "feature"
    REGIME = "regime"
    STRATEGY_SELECTION = "strategy_selection"
    TRADE_PROPOSAL = "trade_proposal"
    CRITIC = "critic"
    RISK = "risk"
    EXECUTION = "execution"
    OUTCOME = "outcome"
    REFLECTION = "reflection"
    HYPOTHESIS = "hypothesis"
    EXPERIMENT = "experiment"
    PROMOTION = "promotion"
    SYSTEM = "system"
    SAFETY = "safety"


@dataclass(frozen=True)
class AuditEvent:
    """Immutable record of a single decision or observation.

    Designed so that later agents and operators can answer:
    WHAT happened? WHY? WHICH component decided? WHICH data was used?
    """

    id: str
    category: AuditCategory
    event_type: str
    timestamp: datetime = field(default_factory=_utc_now)
    component: str = ""
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    symbol: Optional[str] = None
    mode: Optional[OperatingMode] = None
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    parent_event_id: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        data["timestamp"] = self.timestamp.isoformat()
        if self.mode is not None:
            data["mode"] = self.mode.value
        return data

    @classmethod
    def create(
        cls,
        category: AuditCategory,
        event_type: str,
        *,
        component: str = "",
        strategy_id: Optional[str] = None,
        strategy_version: Optional[str] = None,
        symbol: Optional[str] = None,
        mode: Optional[OperatingMode] = None,
        payload: Optional[dict[str, Any]] = None,
        reason: str = "",
        parent_event_id: Optional[str] = None,
    ) -> "AuditEvent":
        return cls(
            id=_new_id("aud_"),
            category=category,
            event_type=event_type,
            component=component,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            symbol=symbol,
            mode=mode,
            payload=payload or {},
            reason=reason,
            parent_event_id=parent_event_id,
        )


# ---------------------------------------------------------------------------
# Experience – the seed of long-term memory
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Experience:
    """One completed decision cycle that can later be retrieved for learning.

    This is deliberately richer than a simple trade record so that reflection,
    similarity search, and hypothesis generation have enough context.
    """

    id: str
    timestamp: datetime
    symbol: str
    mode: OperatingMode
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    regime: Optional[str] = None
    regime_confidence: Optional[float] = None
    proposed_action: Optional[str] = None
    final_action: Optional[str] = None
    trade_confidence: Optional[float] = None
    requested_risk_fraction: Optional[Decimal] = None
    approved_risk_fraction: Optional[Decimal] = None
    entry_price: Optional[Decimal] = None
    actual_entry: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    position_size: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    fees: Optional[Decimal] = None
    slippage: Optional[Decimal] = None
    holding_duration_seconds: Optional[float] = None
    exit_reason: Optional[str] = None
    critic_result: Optional[str] = None
    risk_result: Optional[str] = None
    feature_snapshot: dict[str, Any] = field(default_factory=dict)
    reflection: Optional[str] = None
    lessons: tuple[str, ...] = ()
    audit_event_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mode"] = self.mode.value
        data["timestamp"] = self.timestamp.isoformat()
        for key in (
            "requested_risk_fraction",
            "approved_risk_fraction",
            "entry_price",
            "actual_entry",
            "stop_loss",
            "take_profit",
            "position_size",
            "realized_pnl",
            "fees",
            "slippage",
        ):
            value = getattr(self, key)
            if isinstance(value, Decimal):
                data[key] = str(value)
        return data


# ---------------------------------------------------------------------------
# Structured trade proposal (never raw natural language)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TradeProposal:
    """Validated structured output that a Trading Agent may emit.

    An invalid proposal must fail safely before it ever reaches the Risk Kernel.
    """

    id: str
    symbol: str
    action: str                     # BUY | SELL | HOLD | CLOSE | REDUCE_POSITION | NO_TRADE
    strategy_id: str
    strategy_version: str
    market_regime: str = ""
    regime_confidence: float = 0.0
    trade_confidence: float = 0.0
    entry_type: str = "MARKET"      # MARKET | LIMIT
    entry_price: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    requested_risk_fraction: Decimal = Decimal("0")
    expected_reward_risk: Optional[float] = None
    supporting_signals: tuple[str, ...] = ()
    conflicting_signals: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    reasoning_summary: str = ""
    created_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        for key in ("entry_price", "stop_loss", "take_profit", "requested_risk_fraction"):
            value = getattr(self, key)
            if isinstance(value, Decimal):
                data[key] = str(value)
        return data


__all__ = [
    "OperatingMode",
    "StrategyStatus",
    "StrategyVersion",
    "AuditCategory",
    "AuditEvent",
    "Experience",
    "TradeProposal",
]
