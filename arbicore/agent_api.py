"""Read-only agent API helpers.

Phase 22.

Provides:
* snapshot payloads for the observation loop and research modules
* an optional Flask blueprint that can be registered on the existing app

Default behaviour of the main bot is unchanged until the blueprint is
explicitly registered and the agent loop is enabled.
"""

from __future__ import annotations

from typing import Any, Optional

from .agent_loop import AgentObservationLoop, agent_loop_enabled
from .strategy_registry import StrategyRegistry


def build_agent_snapshot(loop: Optional[AgentObservationLoop] = None) -> dict[str, Any]:
    """Pure snapshot for GET /api/agent (or equivalent)."""
    enabled = agent_loop_enabled()
    if loop is None:
        return {
            "ok": True,
            "agent_loop_enabled": enabled,
            "message": (
                "observation loop not attached; set ARBICORE_AGENT_LOOP=1 and "
                "register AgentObservationLoop to collect live cycles"
            ),
            "state": {"enabled": enabled, "cycles": 0},
        }
    snap = loop.state.snapshot()
    return {
        "ok": True,
        "agent_loop_enabled": loop.state.enabled,
        "state": snap,
        "recent": list(loop.state.history[-20:]),
    }


def build_registry_snapshot(registry: Optional[StrategyRegistry] = None) -> dict[str, Any]:
    """List registered strategy versions (read-only)."""
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


def create_agent_blueprint(loop: Optional[AgentObservationLoop] = None,
                           registry: Optional[StrategyRegistry] = None):
    """Optional Flask blueprint. Import only when Flask is available."""
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
