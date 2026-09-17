"""Read-only agent API helpers.

Phases 22 + 27.
"""

from __future__ import annotations

from typing import Any, Optional

from .agent_loop import AgentObservationLoop, agent_loop_enabled, paper_exec_enabled
from .strategy_registry import StrategyRegistry


def build_agent_snapshot(loop: Optional[AgentObservationLoop] = None) -> dict[str, Any]:
    """Pure snapshot for GET /api/agent."""
    # Prefer process-wide scan_hook loop if present
    if loop is None:
        try:
            from .scan_hook import agent_snapshot as hook_snapshot, get_agent_loop

            hook_loop = get_agent_loop()
            if hook_loop is not None:
                loop = hook_loop
            else:
                base = hook_snapshot()
                return {
                    "ok": True,
                    "agent_loop_enabled": agent_loop_enabled(),
                    "paper_exec_enabled": paper_exec_enabled(),
                    "state": base,
                    "recent": [],
                }
        except Exception:
            pass

    if loop is None:
        return {
            "ok": True,
            "agent_loop_enabled": agent_loop_enabled(),
            "paper_exec_enabled": paper_exec_enabled(),
            "message": "observation loop not attached",
            "state": {"enabled": False, "cycles": 0},
            "recent": [],
        }

    snap = loop.state.snapshot()
    return {
        "ok": True,
        "agent_loop_enabled": loop.state.enabled,
        "paper_exec_enabled": loop.state.paper_exec_enabled,
        "state": snap,
        "recent": list(loop.state.history[-20:]),
    }


def build_registry_snapshot(registry: Optional[StrategyRegistry] = None) -> dict[str, Any]:
    if registry is None:
        try:
            from .scan_hook import get_agent_loop

            loop = get_agent_loop()
            if loop is not None:
                registry = loop.registry
        except Exception:
            registry = None
    if registry is None:
        return {"ok": True, "strategies": [], "count": 0}
    versions = registry.all_versions()
    return {
        "ok": True,
        "count": len(versions),
        "strategies": [
            {
                "id": v.id,
                "name": v.name,
                "version": v.version,
                "status": v.status.value,
                "parent_version_id": v.parent_version_id,
                "created_at": v.created_at.isoformat(),
            }
            for v in versions
        ],
    }


def create_agent_blueprint(
    loop: Optional[AgentObservationLoop] = None,
    registry: Optional[StrategyRegistry] = None,
):
    try:
        from flask import Blueprint, jsonify
    except ImportError as exc:
        raise RuntimeError("Flask is required to create the agent blueprint") from exc

    bp = Blueprint("arbicore_agent", __name__)

    @bp.route("/api/agent", methods=["GET"])
    def api_agent():
        return jsonify(build_agent_snapshot(loop)), 200

    @bp.route("/api/agent/strategies", methods=["GET"])
    def api_agent_strategies():
        return jsonify(build_registry_snapshot(registry)), 200

    return bp


__all__ = [
    "build_agent_snapshot",
    "build_registry_snapshot",
    "create_agent_blueprint",
]
