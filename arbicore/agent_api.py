"""Read-only agent API helpers + Strategy Lab routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .agent_loop import AgentObservationLoop, agent_loop_enabled, paper_exec_enabled
from .strategy_registry import StrategyRegistry

_LAB_HTML = Path(__file__).resolve().parents[1] / "static" / "agent_lab.html"


def _resolve_loop(loop: Optional[AgentObservationLoop]) -> Optional[AgentObservationLoop]:
    if loop is not None:
        return loop
    try:
        from .scan_hook import get_agent_loop

        return get_agent_loop()
    except Exception:
        return None


def _resolve_memory(loop: Optional[AgentObservationLoop] = None):
    loop = _resolve_loop(loop)
    memory = getattr(loop, "memory", None) if loop is not None else None
    if memory is None:
        try:
            from .scan_hook import get_memory

            memory = get_memory()
        except Exception:
            memory = None
    return memory


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
            "message": "research lab not attached",
        }
    hyps = [h.as_dict() for h in getattr(lab, "list_hypotheses", lambda: [])()]
    exps = [e.as_dict() for e in getattr(lab, "list_experiments", lambda: [])()]
    return {"ok": True, "hypotheses": hyps, "experiments": exps}


def build_drift_snapshot(
    strategy_id: Optional[str] = None,
    loop: Optional[AgentObservationLoop] = None,
) -> dict[str, Any]:
    from .drift import DriftMonitor

    memory = _resolve_memory(loop)
    if memory is None:
        return {"ok": True, "reports": [], "message": "no experience memory"}

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


def build_patterns_snapshot(loop: Optional[AgentObservationLoop] = None) -> dict[str, Any]:
    from .patterns import PatternDiscovery

    memory = _resolve_memory(loop)
    if memory is None:
        return {"ok": True, "message": "no experience memory", "report": None}
    try:
        experiences = memory.recent_experiences(limit=500)
    except Exception:
        experiences = []
    report = PatternDiscovery().analyze(experiences)
    return {"ok": True, "report": report.as_dict()}


def build_similarity_snapshot(
    *,
    symbol: Optional[str] = None,
    loop: Optional[AgentObservationLoop] = None,
) -> dict[str, Any]:
    from .similarity import SimilarityEngine

    memory = _resolve_memory(loop)
    if memory is None:
        return {"ok": True, "message": "no experience memory", "report": None}
    try:
        experiences = memory.recent_experiences(limit=500)
    except Exception:
        experiences = []

    features: dict[str, Any] = {}
    loop = _resolve_loop(loop)
    if loop and loop.state.last_result and loop.state.last_result.features:
        features = dict(loop.state.last_result.features.features or {})
    if not features and experiences:
        snap = experiences[0].get("feature_snapshot") if isinstance(experiences[0], dict) else {}
        if isinstance(snap, str):
            try:
                import json

                snap = json.loads(snap)
            except Exception:
                snap = {}
        features = snap if isinstance(snap, dict) else {}

    engine = SimilarityEngine()
    report = engine.query(features, experiences, symbol=symbol)
    return {"ok": True, "report": report.as_dict()}


def build_scorecard_snapshot(loop: Optional[AgentObservationLoop] = None) -> dict[str, Any]:
    from .scorecard import ImprovementScorecard

    memory = _resolve_memory(loop)
    if memory is None:
        return {"ok": True, "message": "no experience memory", "scorecard": None}
    try:
        experiences = memory.recent_experiences(limit=500)
    except Exception:
        experiences = []
    card = ImprovementScorecard().evaluate(experiences)
    return {"ok": True, "scorecard": card.as_dict()}


def build_lab_payload() -> dict[str, Any]:
    """Full lab aggregate – never raises."""
    try:
        from .lab_api import build_lab_snapshot

        return build_lab_snapshot()
    except Exception as exc:
        try:
            from .agent_live_wire import live_wire_snapshot

            live = live_wire_snapshot()
        except Exception as live_exc:
            live = {"error": str(live_exc)}
        return {
            "ok": True,
            "agent": build_agent_snapshot(),
            "strategies": build_registry_snapshot(),
            "drift": {"ok": True, "reports": []},
            "patterns": {"ok": True, "report": None},
            "scorecard": {"ok": True, "scorecard": None},
            "canary": {"deployments": []},
            "handoff": {"enabled": False, "queued": 0},
            "models": {},
            "live": live,
            "fallback_error": str(exc),
            "safety_note": "Lab fallback payload; some sections unavailable.",
        }


def create_agent_blueprint(
    loop: Optional[AgentObservationLoop] = None,
    registry: Optional[StrategyRegistry] = None,
):
    try:
        from flask import Blueprint, Response, jsonify, request
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

    @bp.route("/api/agent/patterns", methods=["GET"])
    def api_agent_patterns():
        return jsonify(build_patterns_snapshot(loop)), 200

    @bp.route("/api/agent/similarity", methods=["GET"])
    def api_agent_similarity():
        symbol = request.args.get("symbol")
        return jsonify(build_similarity_snapshot(symbol=symbol, loop=loop)), 200

    @bp.route("/api/agent/scorecard", methods=["GET"])
    def api_agent_scorecard():
        return jsonify(build_scorecard_snapshot(loop)), 200

    @bp.route("/lab")
    def lab_page():
        if _LAB_HTML.is_file():
            return Response(_LAB_HTML.read_text(encoding="utf-8"), mimetype="text/html")
        return Response(
            "<h1>Strategy Lab</h1><p>static/agent_lab.html missing.</p>",
            mimetype="text/html",
        )

    @bp.route("/api/lab", methods=["GET"])
    def api_lab():
        return jsonify(build_lab_payload()), 200

    @bp.route("/api/lab/live", methods=["GET"])
    def api_lab_live():
        try:
            from .agent_live_wire import live_wire_snapshot

            return jsonify(live_wire_snapshot()), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return bp


__all__ = [
    "build_agent_snapshot",
    "build_registry_snapshot",
    "build_research_snapshot",
    "build_drift_snapshot",
    "build_patterns_snapshot",
    "build_similarity_snapshot",
    "build_scorecard_snapshot",
    "build_lab_payload",
    "create_agent_blueprint",
]
