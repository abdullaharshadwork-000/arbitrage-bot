"""Read-only agent API helpers.

Phases 22 + 27–30.
"""

from __future__ import annotations

from typing import Any, Optional

from .agent_loop import AgentObservationLoop, agent_loop_enabled, paper_exec_enabled
from .strategy_registry import StrategyRegistry


def _resolve_loop(loop: Optional[AgentObservationLoop]) -> Optional[AgentObservationLoop]:
    if loop is not None:
        return loop
    try:
        from .scan_hook import get_agent_loop

        return get_agent_loop()
    except Exception:
        return None


def build_agent_snapshot(loop: Optional[AgentObservationLoop] = None) -> dict[str, Any]:
    loop = _resolve_loop(loop)
    if loop is None:
        try:
            from .scan_hook import agent_snapshot as hook_snap

            base = hook_snap()
        except Exception:
            base = {"enabled": agent_loop_enabled(), "attached": False}
        return {
            "ok": True,
            "agent_loop_enabled": agent_loop_enabled(),
            "paper_exec_enabled": paper_exec_enabled(),
            "message": "observation loop not attached or flag off",
            "state": base,
            "recent": [],
        }

    snap = loop.state.snapshot()
    try:
        from .scan_hook import agent_snapshot as hook_snap

        extra = hook_snap()
        if "memory" in extra:
            snap["memory"] = extra["memory"]
        if "buffered_symbols" in extra:
            snap["buffered_symbols"] = extra["buffered_symbols"]
    except Exception:
        pass

    return {
        "ok": True,
        "agent_loop_enabled": loop.state.enabled,
        "paper_exec_enabled": loop.state.paper_exec_enabled,
        "state": snap,
        "recent": list(loop.state.history[-20:]),
    }


def build_registry_snapshot(registry: Optional[StrategyRegistry] = None) -> dict[str, Any]:
    if registry is None:
        loop = _resolve_loop(None)
        if loop is not None:
            registry = loop.registry
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
    loop = _resolve_loop(loop)
    lab = getattr(loop, "lab", None) if loop is not None else None
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

    loop = _resolve_loop(loop)
    memory = getattr(loop, "memory", None) if loop is not None else None
    if memory is None:
        try:
            from .scan_hook import get_memory

            memory = get_memory()
        except Exception:
            memory = None

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
    return {
        "ok": True,
        "reports": reports,
        "experience_count": len(experiences),
        "db": str(getattr(memory, "db_path", "")),
    }


def create_agent_blueprint(
    loop: Optional[AgentObservationLoop] = None,
    registry: Optional[StrategyRegistry] = None,
):
    try:
        from flask import Blueprint, jsonify, request
    except ImportError as exc:
        raise RuntimeError("Flask is required to create the agent blueprint") from exp

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
