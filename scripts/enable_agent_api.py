#!/usr/bin/env python3
"""Idempotent patches for optional agent + lab + live wire integration in server.py.

Safe to run multiple times. Does not enable trading by itself.
Flags:
  ARBICORE_AGENT_LOOP=1
  ARBICORE_AGENT_PAPER_EXEC=1
  ARBICORE_AGENT_HANDOFF=1
  ARBICORE_AGENT_LIVE_EXEC=1
  ARBICORE_AGENT_CANARY=0.05
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"

API_IMPORTS = """from arbicore.agent_loop import AgentObservationLoop
from arbicore.agent_api import create_agent_blueprint
from arbicore.lab_api import create_lab_blueprint
from arbicore.strategy_registry import StrategyRegistry
from arbicore.server_live_hook import maybe_process_handoff, maybe_register_from_engine
"""

SCAN_IMPORT = "from arbicore.scan_hook import notify_agent_mid\n"
LIVE_IMPORT = (
    "from arbicore.server_live_hook import maybe_process_handoff, "
    "maybe_register_from_engine\n"
)

API_BLOCK = """
# Optional agent + lab APIs (read-only / operator lab). Never places orders alone.
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
            maybe_process_handoff(max_items=1, mid_prices=mid_prices)
        except Exception:
            pass

        found = []
"""

ENGINE_OLD = "        real_engine = bot.RealExecutionEngine(exchanges, credential_map)\n"
ENGINE_NEW = (
    "        real_engine = bot.RealExecutionEngine(exchanges, credential_map)\n"
    "        try:\n"
    "            maybe_register_from_engine(\n"
    "                real_engine,\n"
    "                risk_manager=risk_manager,\n"
    "                equity=float(getattr(risk_manager, \"equity\", 10000) or 10000),\n"
    "            )\n"
    "        except Exception:\n"
    "            pass\n"
)

ENGINE_SIMPLE = (
    "            maybe_register_from_engine(real_engine)\n"
)
ENGINE_RICH = (
    "            maybe_register_from_engine(\n"
    "                real_engine,\n"
    "                risk_manager=risk_manager,\n"
    "                equity=float(getattr(risk_manager, \"equity\", 10000) or 10000),\n"
    "            )\n"
)

HAND_SIMPLE = "            maybe_process_handoff(max_items=1)\n"
HAND_RICH = "            maybe_process_handoff(max_items=1, mid_prices=mid_prices)\n"


def _ensure_lab(text: str) -> tuple[str, bool]:
    if "create_lab_blueprint" in text:
        return text, False
    changed = False
    if "from arbicore.agent_api import create_agent_blueprint" in text:
        text = text.replace(
            "from arbicore.agent_api import create_agent_blueprint\n",
            "from arbicore.agent_api import create_agent_blueprint\n"
            "from arbicore.lab_api import create_lab_blueprint\n",
            1,
        )
        changed = True
    needle = "app.register_blueprint(create_agent_blueprint(_agent_loop, _agent_registry))\n"
    if needle in text and "create_lab_blueprint()" not in text:
        text = text.replace(
            needle,
            needle + "    app.register_blueprint(create_lab_blueprint())\n",
            1,
        )
        changed = True
    return text, changed


def _ensure_live_hooks(text: str) -> tuple[str, bool]:
    changed = False
    if "maybe_process_handoff" not in text:
        if "from arbicore.scan_hook import notify_agent_mid" in text:
            text = text.replace(
                "from arbicore.scan_hook import notify_agent_mid\n",
                "from arbicore.scan_hook import notify_agent_mid\n" + LIVE_IMPORT,
                1,
            )
            changed = True
        elif "from arbicore.agent_api import create_agent_blueprint" in text:
            text = text.replace(
                "from arbicore.agent_api import create_agent_blueprint\n",
                "from arbicore.agent_api import create_agent_blueprint\n" + LIVE_IMPORT,
                1,
            )
            changed = True

    old_notify = (
        "        try:\n"
        "            for _sym, _mid in (mid_prices or {}).items():\n"
        "                notify_agent_mid(_sym, _mid)\n"
        "        except Exception:\n"
        "            pass\n"
    )
    new_notify = (
        "        try:\n"
        "            for _sym, _mid in (mid_prices or {}).items():\n"
        "                notify_agent_mid(_sym, _mid)\n"
        "            maybe_process_handoff(max_items=1, mid_prices=mid_prices)\n"
        "        except Exception:\n"
        "            pass\n"
    )
    if old_notify in text and "maybe_process_handoff" not in text:
        text = text.replace(old_notify, new_notify, 1)
        changed = True

    if HAND_SIMPLE in text:
        text = text.replace(HAND_SIMPLE, HAND_RICH, 1)
        changed = True

    if ENGINE_SIMPLE in text:
        text = text.replace(ENGINE_SIMPLE, ENGINE_RICH, 1)
        changed = True
    elif ENGINE_OLD in text and "maybe_register_from_engine" not in text:
        text = text.replace(ENGINE_OLD, ENGINE_NEW, 1)
        changed = True

    return text, changed


def main():
    text = SERVER.read_text(encoding="utf-8")
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
        text, lab_changed = _ensure_lab(text)
        if lab_changed:
            changed = True
            print("added lab blueprint registration")
        else:
            print("agent/lab API already registered")

    if "notify_agent_mid" not in text:
        if "from arbicore.scan_hook import notify_agent_mid" not in text:
            text = text.replace(anchor_import, anchor_import + SCAN_IMPORT, 1)
        if SCAN_OLD not in text:
            print("scan_loop anchor not found; skip scan hook")
        else:
            text = text.replace(SCAN_OLD, SCAN_NEW, 1)
            changed = True
            print("wired notify_agent_mid + handoff into scan_loop")
    else:
        print("scan_loop agent hook already present")

    text, live_changed = _ensure_live_hooks(text)
    if live_changed:
        changed = True
        print("wired agent live handoff + engine registration hooks")
    else:
        print("live hooks already present or not applicable")

    if changed:
        SERVER.write_text(text, encoding="utf-8")
        print("server.py updated – restart the server")
    else:
        print("no changes needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
