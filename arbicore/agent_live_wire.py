"""Wire agent ApprovedOrderRequest → live exchange via existing clients.

Also tracks open agent positions and can submit SL/TP exits through the
same LiveExecutor (still gated by LIVE_EXEC + wire registration).
"""

from __future__ import annotations

from typing import Any, Optional

from .agent_positions import get_position_book
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
_pending_levels: dict[str, dict[str, Any]] = {}


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

    req = risk.request
    _pending_levels[req.id] = {
        "stop_loss": req.stop_loss,
        "take_profit": req.take_profit,
        "strategy_id": req.strategy_id,
        "strategy_version": req.strategy_version,
    }
    entry = handoff(req)
    return {
        "queued": True,
        "request": req.as_dict(),
        "handoff": entry,
    }


def process_approved_request(request: ApprovedOrderRequest) -> LiveExecResult:
    if _executor is None:
        return LiveExecResult(
            False,
            request_id=request.id,
            reject_reason="live wire not registered (call register_live_wire)",
        )
    is_exit = str(request.intent_id or "").startswith("exit_")
    if is_exit:
        old_canary = _executor.canary_fraction
        _executor.canary_fraction = 1.0
        try:
            result = _executor.execute(request)
        finally:
            _executor.canary_fraction = old_canary
    else:
        result = _executor.execute(request)

    _last_results.append(result.as_dict())
    if len(_last_results) > 100:
        del _last_results[:-100]

    book = get_position_book()
    if result.executed and not is_exit:
        meta = _pending_levels.pop(request.id, {}) or {}
        book.open_from_fill(
            result,
            stop_loss=meta.get("stop_loss", request.stop_loss),
            take_profit=meta.get("take_profit", request.take_profit),
            strategy_id=str(meta.get("strategy_id") or request.strategy_id or ""),
            strategy_version=str(
                meta.get("strategy_version") or request.strategy_version or ""
            ),
        )
    elif result.executed and is_exit:
        pos_id = str(request.intent_id or "").replace("exit_", "", 1)
        closed = book.close(
            pos_id,
            exit_price=float(result.average_price or 0),
            reason="exit_fill",
        )
        if closed is not None:
            try:
                from .agent_learn import record_closed_position

                record_closed_position(closed)
            except Exception:
                pass

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


def process_position_exits(mid_prices: dict[str, float]) -> list[dict[str, Any]]:
    if not live_exec_enabled() or _executor is None:
        return []
    book = get_position_book()
    hits = book.check_exits(mid_prices or {})
    results: list[dict[str, Any]] = []
    for pos, reason in hits:
        book.mark_exit_pending(pos.id, reason)
        req = book.exit_request(pos)
        out = process_approved_request(req)
        results.append({"position_id": pos.id, "reason": reason, **out.as_dict()})
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
        "positions": get_position_book().snapshot(),
    }


__all__ = [
    "register_live_wire",
    "register_risk_context",
    "get_live_executor",
    "maybe_queue_approved",
    "process_approved_request",
    "process_handoff_queue",
    "process_position_exits",
    "live_wire_snapshot",
]
