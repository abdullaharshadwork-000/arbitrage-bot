"""Wire agent ApprovedOrderRequest → live exchange via existing clients.

Operator path:
1. Dashboard live + real + ack
2. ARBICORE_AGENT_LIVE_EXEC=1 + HANDOFF=1
3. register_live_wire / maybe_register_from_engine
4. register_risk_context (or server auto)
5. Critic APPROVE → queue → process_handoff_queue
"""

from __future__ import annotations

from typing import Any, Optional

from .critic import CriticDecision
from .domain import OperatingMode, TradeProposal
from .execution_handoff import HandoffQueue, handoff, handoff_enabled, _default_queue
from .guards import LiveModeGuard
from .live_bridge import OrderIntentBridge
from .live_exec import LiveExecutor, LiveExecResult, live_exec_enabled
from .risk_adapter import ApprovedOrderRequest, RiskAdapter

_executor: Optional[LiveExecutor] = None
_last_results: list[dict[str, Any]] = []
_risk_manager: Any = None
_live_guard: Optional[LiveModeGuard] = None
_equity: float = 10_000.0


def register_live_wire(
    place_fn,
    *,
    live_guard: Optional[LiveModeGuard] = None,
    canary_fraction: float = 0.05,
    min_notional: float = 5.0,
) -> LiveExecutor:
    global _executor, _live_guard
    if live_guard is not None:
        _live_guard = live_guard
    _executor = LiveExecutor(
        place_fn,
        live_guard=_live_guard,
        canary_fraction=canary_fraction,
        min_notional=min_notional,
        require_flag=True,
    )
    return _executor


def register_risk_context(
    risk_manager: Any,
    *,
    equity: float = 10_000.0,
    live_guard: Optional[LiveModeGuard] = None,
) -> None:
    global _risk_manager, _equity, _live_guard
    _risk_manager = risk_manager
    _equity = float(equity)
    if live_guard is not None:
        _live_guard = live_guard


def get_live_executor() -> Optional[LiveExecutor]:
    return _executor


def maybe_queue_approved(
    proposal: TradeProposal,
    critique: CriticDecision,
    *,
    mode: OperatingMode = OperatingMode.LIVE,
) -> Optional[dict[str, Any]]:
    if not live_exec_enabled():
        return None
    if critique is None or critique.result != "APPROVE":
        return None
    if proposal is None or proposal.action in ("NO_TRADE", "HOLD"):
        return None
    if _risk_manager is None:
        return {"queued": False, "reason": "risk context not registered"}

    bridge = OrderIntentBridge(mode=mode).build(proposal, critique)
    if not bridge.accepted or bridge.intent is None:
        return {"queued": False, "reason": bridge.reject_reason}

    intent = bridge.intent
    adapter = RiskAdapter(
        _risk_manager,
        live_guard=_live_guard,
        equity=_equity,
        mode=OperatingMode.LIVE,
    )
    from dataclasses import replace

    try:
        live_intent = replace(intent, mode="live")
    except Exception:
        live_intent = intent

    risk = adapter.evaluate(live_intent)
    if not risk.allowed or risk.request is None:
        return {
            "queued": False,
            "reason": risk.reject_reason,
            "risk": risk.as_dict() if hasattr(risk, "as_dict") else None,
        }

    entry = handoff(risk.request)
    return {
        "queued": True,
        "request": risk.request.as_dict(),
        "handoff": entry,
    }


def process_approved_request(request: ApprovedOrderRequest) -> LiveExecResult:
    if _executor is None:
        return LiveExecResult(
            False,
            request_id=request.id,
            reject_reason="live wire not registered (call register_live_wire)",
        )
    result = _executor.execute(request)
    _last_results.append(result.as_dict())
    if len(_last_results) > 100:
        del _last_results[:-100]
    return result


def process_handoff_queue(
    queue: Optional[HandoffQueue] = None,
    *,
    max_items: int = 1,
) -> list[dict[str, Any]]:
    if not live_exec_enabled():
        return [{"executed": False, "reject_reason": "ARBICORE_AGENT_LIVE_EXEC off"}]
    if _executor is None:
        return [{"executed": False, "reject_reason": "live wire not registered"}]

    q = queue if queue is not None else _default_queue
    results: list[dict[str, Any]] = []
    for _ in range(max_items):
        if not q.items:
            break
        entry = q.items.popleft()
        req_dict = (entry or {}).get("request") or {}
        try:
            req = ApprovedOrderRequest(
                id=str(req_dict.get("id") or ""),
                intent_id=str(req_dict.get("intent_id") or ""),
                symbol=str(req_dict.get("symbol") or ""),
                side=str(req_dict.get("side") or ""),
                order_type=str(req_dict.get("order_type") or "market"),
                notional_usdt=float(req_dict.get("notional_usdt") or 0),
                risk_fraction=float(req_dict.get("risk_fraction") or 0),
                entry_price=req_dict.get("entry_price"),
                stop_loss=req_dict.get("stop_loss"),
                take_profit=req_dict.get("take_profit"),
                strategy_id=str(req_dict.get("strategy_id") or ""),
                strategy_version=str(req_dict.get("strategy_version") or ""),
                mode=str(req_dict.get("mode") or "live"),
                risk_reason=str(req_dict.get("risk_reason") or ""),
            )
        except Exception as exc:
            results.append({"executed": False, "reject_reason": f"bad request: {exc}"})
            continue
        results.append(process_approved_request(req).as_dict())
    return results


def live_wire_snapshot() -> dict[str, Any]:
    return {
        "live_exec_enabled": live_exec_enabled(),
        "handoff_enabled": handoff_enabled(),
        "wire_registered": _executor is not None,
        "risk_context": _risk_manager is not None,
        "equity": _equity,
        "canary_fraction": getattr(_executor, "canary_fraction", None),
        "recent": list(_last_results[-10:]),
    }


__all__ = [
    "register_live_wire",
    "register_risk_context",
    "get_live_executor",
    "maybe_queue_approved",
    "process_approved_request",
    "process_handoff_queue",
    "live_wire_snapshot",
]
