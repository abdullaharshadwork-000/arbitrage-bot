"""Strategy / Research Lab API helpers.

Phase 21 + live wire status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canary import CanaryManager
from .execution_handoff import handoff_snapshot
from .ml_registry import ModelRegistry

_canary = CanaryManager()
_models = ModelRegistry()
_LAB_HTML = Path(__file__).resolve().parents[1] / "static" / "agent_lab.html"


def get_canary_manager() -> CanaryManager:
    return _canary


def get_model_registry() -> ModelRegistry:
    return _models


def build_lab_snapshot() -> dict[str, Any]:
    from .agent_api import (
        build_agent_snapshot,
        build_drift_snapshot,
        build_patterns_snapshot,
        build_registry_snapshot,
        build_scorecard_snapshot,
    )
    try:
        from .agent_live_wire import live_wire_snapshot
        live = live_wire_snapshot()
    except Exception as exc:
        live = {"error": str(exc)}

    return {
        "ok": True,
        "agent": build_agent_snapshot(),
        "strategies": build_registry_snapshot(),
        "drift": build_drift_snapshot(),
        "patterns": build_patterns_snapshot(),
        "scorecard": build_scorecard_snapshot(),
        "canary": _canary.snapshot(),
        "handoff": handoff_snapshot(),
        "models": _models.snapshot(),
        "live": live,
        "safety_note": (
            "Live agent orders require ARBICORE_AGENT_LIVE_EXEC=1, "
            "LiveModeGuard (dashboard live+real+ack), Risk Kernel approval, "
            "and register_live_wire(place_fn)."
        ),
    }


def create_lab_blueprint():
    try:
        from flask import Blueprint, Response, jsonify, request
    except ImportError as exc:
        raise RuntimeError("Flask required") from exc

    bp = Blueprint("arbicore_lab", __name__)

    @bp.route("/lab")
    def lab_page():
        if _LAB_HTML.is_file():
            return Response(_LAB_HTML.read_text(encoding="utf-8"), mimetype="text/html")
        return Response(
            "<h1>Strategy Lab</h1><p>static/agent_lab.html missing. "
            "Use GET /api/lab instead.</p>",
            mimetype="text/html",
        )

    @bp.route("/api/lab", methods=["GET"])
    def api_lab():
        return jsonify(build_lab_snapshot()), 200

    @bp.route("/api/lab/live", methods=["GET"])
    def api_live():
        try:
            from .agent_live_wire import live_wire_snapshot
            return jsonify(live_wire_snapshot()), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/api/lab/canary", methods=["GET"])
    def api_canary_list():
        return jsonify(_canary.snapshot()), 200

    @bp.route("/api/lab/canary/start", methods=["POST"])
    def api_canary_start():
        data = request.get_json(silent=True) or {}
        sid = str(data.get("strategy_id") or "").strip()
        ver = str(data.get("strategy_version") or "1.0.0").strip()
        if not sid:
            return jsonify({"ok": False, "error": "strategy_id required"}), 400
        dep = _canary.start(sid, ver, notes=str(data.get("notes") or ""))
        return jsonify({"ok": True, "deployment": dep.as_dict()}), 200

    @bp.route("/api/lab/canary/<dep_id>/advance", methods=["POST"])
    def api_canary_advance(dep_id: str):
        dep = _canary.advance(dep_id)
        if not dep:
            return jsonify({"ok": False, "error": "not found or not active"}), 404
        return jsonify({"ok": True, "deployment": dep.as_dict()}), 200

    @bp.route("/api/lab/canary/<dep_id>/rollback", methods=["POST"])
    def api_canary_rollback(dep_id: str):
        data = request.get_json(silent=True) or {}
        dep = _canary.rollback(dep_id, reason=str(data.get("reason") or ""))
        if not dep:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, "deployment": dep.as_dict()}), 200

    @bp.route("/api/lab/models", methods=["GET"])
    def api_models():
        return jsonify(_models.snapshot()), 200

    @bp.route("/api/lab/handoff", methods=["GET"])
    def api_handoff():
        return jsonify(handoff_snapshot()), 200

    return bp


__all__ = [
    "get_canary_manager",
    "get_model_registry",
    "build_lab_snapshot",
    "create_lab_blueprint",
]
