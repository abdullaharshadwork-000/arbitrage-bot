#!/usr/bin/env python3
"""Idempotent patches for optional agent + lab integration in server.py.

Safe to run multiple times. Does not enable trading.
Flags:
  ARBICORE_AGENT_LOOP=1
  ARBICORE_AGENT_PAPER_EXEC=1
  ARBICORE_AGENT_HANDOFF=1
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"

API_IMPORTS = """from arbicore.agent_loop import AgentObservationLoop
from arbicore.agent_api import create_agent_blueprint
from arbicore.strategy_registry import StrategyRegistry
from arbicore.lab_api import create_lab_blueprint
"""

SCAN_IMPORT = "from arbicore.scan_hook import notify_agent_mid\n"

API_BLOCK = """
# Optional agent + lab APIs (read-only / operator lab). Never places orders.
try:
    _agent_registry = StrategyRegistry()
    _agent_loop = AgentObservationLoop(registry=_agent_registry)
    app.register_blueprint(create_agent_blueprint(_agent_loop, _agent_registry))
    app.register_blueprint(create_lab_blueprint())
except Exception as _agent_exc:  # pragma: no cover
    import logging as _logging
    _logging.getLogger("arbicore").warning("agent/lab API not registered: %s", _agent_exc)
"""

SCAN_OLD = """            state["quotes"] = quotes
            state["mid_prices"] = mid_prices
            state["feed_health"] = feed_health(quotes)
            state["intelligence"] = market_intelligence.snapshot()

        found = []
"""

SCAN_NEW = """            state["quotes"] = quotes
            state["mid_prices"] = mid_prices
            state["feed_health"] = feed_health(quotes)
            state["intelligence"] = market_intelligence.snapshot()

        # Optional agent observation (no-op unless ARBICORE_AGENT_LOOP=1; never raises)
        try:
            for _sym, _mid in (mid_prices or {}).items():
                notify_agent_mid(_sym, _mid)
        except Exception:
            pass

        found = []
"""


def main():
    text = SERVER.read_text()
    changed = False

    anchor_import = (
        "from arbicore.performance import summarize as summarize_performance, "
        "scope_sql as performance_scope_sql\n"
    )
    if anchor_import not in text:
        print("import anchor not found; abort")
        return 1

    if "create_agent_blueprint" not in text:
        text = text.replace(anchor_import, anchor_import + API_IMPORTS, 1)
        app_anchor = "app = Flask(__name__, static_folder=None)\n"
        if app_anchor not in text:
            print("app anchor not found; abort")
            return 1
        text = text.replace(app_anchor, app_anchor + API_BLOCK, 1)
        changed = True
        print("registered agent + lab API blueprints")
    else:
        if "create_lab_blueprint" not in text:
            # older patch without lab – add lab registration near agent block
            if "create_agent_blueprint" in text and "create_lab_blueprint" not in text:
                text = text.replace(
                    "from arbicore.agent_api import create_agent_blueprint\n",
                    "from arbicore.agent_api import create_agent_blueprint\n"
                    "from arbicore.lab_api import create_lab_blueprint\n",
                    1,
                )
                text = text.replace(
                    "app.register_blueprint(create_agent_blueprint(_agent_loop, _agent_registry))\n",
                    "app.register_blueprint(create_agent_blueprint(_agent_loop, _agent_registry))\n"
                    "    app.register_blueprint(create_lab_blueprint())\n",
                    1,
                )
                changed = True
                print("added lab blueprint registration")
        else:
            print("agent/lab API already registered")

    if "notify_agent_mid" not in text:
        if SCAN_IMPORT not in text:
            text = text.replace(anchor_import, anchor_import + SCAN_IMPORT, 1)
        if SCAN_OLD not in text:
            print("scan_loop anchor not found; abort")
            return 1
        text = text.replace(SCAN_OLD, SCAN_NEW, 1)
        changed = True
        print("wired notify_agent_mid into scan_loop")
    else:
        print("scan_loop agent hook already present")

    if changed:
        SERVER.write_text(text)
        print("server.py updated – restart the server")
    else:
        print("no changes needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
