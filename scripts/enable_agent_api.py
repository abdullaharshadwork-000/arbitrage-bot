#!/usr/bin/env python3
"""Idempotent patch: register read-only agent API routes in server.py.

Safe to run multiple times. Does not enable trading.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"

IMPORTS = """from arbicore.agent_loop import AgentObservationLoop
from arbicore.agent_api import create_agent_blueprint
from arbicore.strategy_registry import StrategyRegistry
"""

BLOCK = """
# Optional agent observation API (read-only). Disabled unless ARBICORE_AGENT_LOOP=1
# and never places orders. Failure here must not break the main bot.
try:
    _agent_registry = StrategyRegistry()
    _agent_loop = AgentObservationLoop(registry=_agent_registry)
    app.register_blueprint(create_agent_blueprint(_agent_loop, _agent_registry))
except Exception as _agent_exc:  # pragma: no cover - defensive
    import logging as _logging
    _logging.getLogger("arbicore").warning("agent API not registered: %s", _agent_exc)
"""


def main():
    text = SERVER.read_text()
    if "create_agent_blueprint" in text:
        print("agent API already registered in server.py")
        return 0
    anchor_import = (
        "from arbicore.performance import summarize as summarize_performance, "
        "scope_sql as performance_scope_sql\n"
    )
    if anchor_import not in text:
        print("import anchor not found; abort")
        return 1
    text = text.replace(anchor_import, anchor_import + IMPORTS, 1)
    app_anchor = "app = Flask(__name__, static_folder=None)\n"
    if app_anchor not in text:
        print("app anchor not found; abort")
        return 1
    text = text.replace(app_anchor, app_anchor + BLOCK, 1)
    SERVER.write_text(text)
    print("patched server.py – restart server to load GET /api/agent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
