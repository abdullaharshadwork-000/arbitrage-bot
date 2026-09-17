"""Read-only agent API helpers.

Phases 22 + 27–29.
"""

from __future__ import annotations

from typing import Any, Optional

from .agent_loop import AgentObservationLoop, agent_loop_enabled, paper_exec_enabled
from .strategy_registry import StrategyRegistry


def build_agent_snapshot(loop: Optional[AgentObservationLoop] = None) -> dict[str, Any]:
    if loop is None:
        try:
            from .scan_hook import get_agent_loop

            loop = get_agent_loop()
        except Exception:
            loop = None

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


def build_research_snapshot(loop: Optional[AgentObservationLoop] = None) -> dict[str, Any]:
    lab = None
    if loop is not None and hasattr(loop, "lab"):
        lab = getattr(loop, "lab", None)
    if lab is None:
        return {
            "ok": True,
            "hypotheses": [],
            "experiments": [],
            "message": "research lab not attached to observation loop",
        }
    hyps = [h.as_dict() for h in getattr(lab, "list_hypotheses", lambda: [])()]
    exps = [e.as_dict() for e in getattr(lab, "list_experiments", lambda: [])()]
    return {"ok": True, "hypotheses": hyps, "experiments": exps}


def build_drift_snapshot(
    strategy_id: Optional[str] = None,
    loop: Optional[AgentObservationLoop] = None,
) -> dict[str, Any]:
    from .drift import DriftMonitor

    memory = getattr(loop, "memory", None) if loop is not None else None
    if memory is None:
        return {
            "ok": True,
            "reports": [],
            "message": "no experience memory attached",
        }

    try:
        experiences = memory.recent_experiences(limit=500)
    except Exception:
        experiences = []

    monitor = DriftMonitor()
    strategy_ids: set[str] = set()
    if strategy_id:
        strategy_ids.add(strategy_id)
    else:
        for e in experiences:
            sid = e.get("strategy_id") if isinstance(e, dict) else getattr(e, "strategy_id", None)
            if sid:
                strategy_ids.add(sid)

    reports = [monitor.evaluate(sid, experiences).as_dict() for sid in sorted(strategy_ids)]
    return {"ok": True, "reports": reports, "experience_count": len(experiences)}


def create_agent_blueprint(
    loop: Optional[AgentObservationLoop] = None,
    registry: Optional[StrategyRegistry] = None,
):
    try:
        from flask import Blueprint, jsonify, request
    except ImportError as exc:
        raise RuntimeError("Flask is required to create the agent blueprint") from exc

    bp = Blueprint("arbicore_agent", __name__)

    @bp.route("/api/agent", methods=["GET"])
    def api_agent():
        return jsonify(build_agent_snapshot(loop)), 200

    @bp.route("/api/agent/strategies", methods=["GET"])
    def api_agent_strategies():
        return jsonify(build_registry_snapshot(registry)), 200

    @bp.route("/api/agent/research", methods=["GET"])
    def api_agent_research():
        return jsonify(build_research_snapshot(loop)), 200

    @bp.route("/api/agent/drift", methods=["GET"])
    def api_agent_drift():
        sid = request.args.get("strategy_id")
        return jsonify(build_drift_snapshot(strategy_id=sid, loop=loop)), 200

    return bp


__all__ = [
    "build_agent_snapshot",
    "build_registry_snapshot",
    "build_research_snapshot",
    "build_drift_snapshot",
    "create_agent_blueprint",
]
