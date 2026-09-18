#!/usr/bin/env python3
"""Project health fix + verification.

Run from repo root:
  python scripts/fix_project.py
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
    # Critical: Lab routes + session wire on server.py
    notes.extend(_run_script("apply_server_patches.py"))
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
        "champion",
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
        from arbicore.agent_api import build_agent_snapshot, build_scorecard_snapshot, build_registry_snapshot
        from arbicore.lab_api import build_lab_snapshot

        a = build_agent_snapshot()
        s = build_scorecard_snapshot()
        r = build_registry_snapshot()
        lab = build_lab_snapshot()
        notes.append(f"agent snapshot ok={a.get('ok')}")
        notes.append(f"scorecard ok={s.get('ok')}")
        notes.append(f"registry count={r.get('count')}")
        notes.append(f"lab ok={lab.get('ok')}")
    except Exception as exp:
        notes.append(f"snapshot FAIL: {type(exp).__name__}: {exp}")
    return notes


def step_server_lab_marker() -> list[str]:
    server = ROOT / "server.py"
    text = server.read_text(encoding="utf-8")
    notes = []
    for label, needle in [
        ("create_agent_blueprint", "create_agent_blueprint"),
        ("create_lab_blueprint", "create_lab_blueprint"),
        ("notify_agent_mid", "notify_agent_mid"),
        ("wire_agent_session", "wire_agent_session"),
        ("theme-glass route", "theme-glass.css"),
        ("lab fallback /api/lab", "arbicore_api_lab_fallback"),
        ("lab fallback /lab", "arbicore_lab_page_fallback"),
    ]:
        notes.append(f"server {label}=" + str(needle in text))
    for rel in ("static/agent_lab.html", "static/agent_lab.js", "static/theme-glass.css"):
        notes.append(f"{rel}=" + str((ROOT / rel).is_file()))
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
    print("Windows:")
    print('  $env:ARBICORE_AGENT_LOOP = "1"')
    print('  $env:ARBICORE_AGENT_PAPER_EXEC = "1"')
    print("  python server.py")
    print("Lab: http://127.0.0.1:5050/lab")
    print("API: http://127.0.0.1:5050/api/lab")
    print("JS:  http://127.0.0.1:5050/agent_lab.js")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
