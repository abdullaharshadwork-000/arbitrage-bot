"""Wire agent ApprovedOrderRequest → live exchange via existing clients.

Operator must:
1. Enable real trading in the dashboard (mode=live, execution=real, ack)
2. Set ARBICORE_AGENT_LIVE_EXEC=1
3. Call register_live_wire(place_fn, live_guard=..., canary_fraction=...)

Until register_live_wire runs, no live agent orders are possible.
"""

from __future__ import annotations

from typing import Any, Optional

from .execution_handoff import HandoffQueue, handoff_enabled, _default_queue
from .guards import LiveModeGuard
from .live_exec import LiveExecutor, LiveExecResult, live_exec_enabled
from .risk_adapter import ApprovedOrderRequest

_executor: Optional[LiveExecutor] = None
_last_results: list[dict[str, Any]] = []


def register_live_wire(
    place_fn,
    *,
    live_guard: Optional[LiveModeGuard] = None,
    canary_fraction: float = 0.05,
    min_notional: float = 5.0,
) -> LiveExecutor:
    """Register the only path agents can use to hit the exchange."""
    global _executor
    _executor = LiveExecutor(
        place_fn,
        live_guard=live_guard,
        canary_fraction=canary_fraction,
        min_notional=min_notional,
        require_flag=True,
    )
    return _executor


def get_live_executor() -> Optional[LiveExecutor]:
    return _executor


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
    """Drain up to max_items approved requests from the handoff queue."""
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
        "canary_fraction": getattr(_executor, "canary_fraction", None),
        "recent": list(_last_results[-10:]),
    }


__all__ = [
    "register_live_wire",
    "get_live_executor",
    "process_approved_request",
    "process_handoff_queue",
    "live_wire_snapshot",
]
