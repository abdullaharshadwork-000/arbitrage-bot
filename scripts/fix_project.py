#!/usr/bin/env python3
"""Project health fix + verification.

Run from repo root:
  python scripts/fix_project.py

* Patches server.py for agent + Strategy Lab routes if missing
* Verifies arbicore package imports
* Does NOT enable live trading or change risk limits
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def step_enable() -> list[str]:
    notes: list[str] = []
    try:
        from scripts import enable_agent_api as ena  # type: ignore
    except ImportError:
        # run as file
        import runpy
        path = ROOT / "scripts" / "enable_agent_api.py"
        ns = runpy.run_path(str(path))
        code = ns["main"]()
        notes.append(f"enable_agent_api exit={code}")
        return notes
    code = ena.main()
    notes.append(f"enable_agent_api exit={code}")
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
    notes.append("static/agent_lab.html=" + str((ROOT / "static" / "agent_lab.html").is_file()))
    return notes


def main() -> int:
    print("=== ArbiCore fix_project ===")
    for label, fn in [
        ("enable", step_enable),
        ("server markers", step_server_lab_marker),
        ("imports", step_imports),
        ("snapshots", step_agent_snapshots),
    ]:
        print(f"\n-- {label} --")
        for line in fn():
            print(line)
    print("\nDone. Restart server after enable patches.")
    print("Windows flags:")
    print('  $env:ARBICORE_AGENT_LOOP = "1"')
    print('  $env:ARBICORE_AGENT_PAPER_EXEC = "1"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
