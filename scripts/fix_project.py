#!/usr/bin/env python3
"""Project health fix + verification.

Run from repo root:
  python scripts/fix_project.py

* Patches server.py for agent + Strategy Lab routes if missing
* Wires agent/live session into scan loop
* Applies glass theme routes
* Verifies arbicore package imports
* Does NOT enable unrestricted live trading or change risk limits
"""
from __future__ import annotations

import importlib
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run_script(name: str) -> list[str]:
    notes: list[str] = []
    path = ROOT / "scripts" / name
    if not path.is_file():
        notes.append(f"missing {name}")
        return notes
    try:
        ns = runpy.run_path(str(path))
        main = ns.get("main")
        if callable(main):
            code = main()
            notes.append(f"{name} exit={code}")
        else:
            notes.append(f"{name} has no main()")
    except Exception as exc:
        notes.append(f"{name} FAIL: {type(exc).__name__}: {exc}")
    return notes


def step_enable() -> list[str]:
    notes: list[str] = []
    notes.extend(_run_script("enable_agent_api.py"))
    notes.extend(_run_script("patch_agent_session.py"))
    notes.extend(_run_script("enable_glass_theme.py"))
    return notes


def step_imports() -> list[str]:
    notes: list[str] = []
    import arbicore

    failed = []
    for name in arbicore.__all__:
        try:
            importlib.import_module(f"arbicore.{name}")
        except Exception as exc:
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    if failed:
        notes.append(f"IMPORT FAILURES ({len(failed)}):")
        notes.extend(f"  - {f}" for f in failed)
    else:
        notes.append(f"imports ok ({len(arbicore.__all__)} modules)")
    for mod in (
        "session_wire",
        "lab_api",
        "agent_live_wire",
        "kill_switch",
        "evidence_loop",
    ):
        try:
            importlib.import_module(f"arbicore.{mod}")
            notes.append(f"import arbicore.{mod} ok")
        except Exception as exc:
            notes.append(f"import arbicore.{mod} FAIL: {exc}")
    return notes


def step_agent_snapshots() -> list[str]:
    notes: list[str] = []
    try:
        from arbicore.agent_api import (
            build_agent_snapshot,
            build_scorecard_snapshot,
            build_registry_snapshot,
        )
        from arbicore.lab_api import build_lab_snapshot

        a = build_agent_snapshot()
        s = build_scorecard_snapshot()
        r = build_registry_snapshot()
        lab = build_lab_snapshot()
        notes.append(f"agent snapshot ok={a.get('ok')}")
        notes.append(f"scorecard ok={s.get('ok')}")
        notes.append(f"registry count={r.get('count')}")
        notes.append(f"lab ok={lab.get('ok')}")
    except Exception as exc:
        notes.append(f"snapshot FAIL: {type(exc).__name__}: {exc}")
    return notes


def step_server_lab_marker() -> list[str]:
    server = ROOT / "server.py"
    text = server.read_text(encoding="utf-8")
    notes = []
    notes.append("server create_agent_blueprint=" + str("create_agent_blueprint" in text))
    notes.append("server create_lab_blueprint=" + str("create_lab_blueprint" in text))
    notes.append("server notify_agent_mid=" + str("notify_agent_mid" in text))
    notes.append("server wire_agent_session=" + str("wire_agent_session" in text))
    notes.append("server theme-glass route=" + str("theme-glass.css" in text))
    notes.append(
        "static/agent_lab.html=" + str((ROOT / "static" / "agent_lab.html").is_file())
    )
    notes.append(
        "static/agent_lab.js=" + str((ROOT / "static" / "agent_lab.js").is_file())
    )
    notes.append(
        "static/theme-glass.css=" + str((ROOT / "static" / "theme-glass.css").is_file())
    )
    return notes


def main() -> int:
    print("=== ArbiCore fix_project ===")
    for label, fn in [
        ("enable/patch", step_enable),
        ("server markers", step_server_lab_marker),
        ("imports", step_imports),
        ("snapshots", step_agent_snapshots),
    ]:
        print(f"\n-- {label} --")
        for line in fn():
            print(line)
    print("\nDone. Restart ONE server after patches.")
    print("Windows flags:")
    print('  $env:ARBICORE_AGENT_LOOP = "1"')
    print('  $env:ARBICORE_AGENT_PAPER_EXEC = "1"')
    print('  # LIVE only when intentional:')
    print('  # $env:ARBICORE_AGENT_LIVE_EXEC = "1"')
    print('  # $env:ARBICORE_AGENT_CANARY = "0.05"')
    print("Lab: http://127.0.0.1:5050/lab  API: /api/lab  JS: /agent_lab.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
