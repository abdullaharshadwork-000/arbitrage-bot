#!/usr/bin/env python3
"""Run a paper agent soak and record self-improvement evidence.

Does NOT place real orders. Use this to start building production evidence.

  python scripts/run_paper_soak.py --cycles 20
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ARBICORE_AGENT_LOOP", "1")
os.environ.setdefault("ARBICORE_AGENT_PAPER_EXEC", "1")
# Never force LIVE in this script
os.environ.pop("ARBICORE_AGENT_LIVE_EXEC", None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper agent soak + evidence")
    parser.add_argument("--cycles", type=int, default=10)
    args = parser.parse_args()

    from arbicore.evidence_loop import get_evidence_store
    from arbicore.scorecard import build_scorecard  # may vary; fallback below

    cycles = max(1, int(args.cycles))
    paper_fills = 0
    reflections = 0
    errors = []

    # Prefer agent loop if available
    try:
        from arbicore.agent_loop import AgentLoop

        loop = AgentLoop()
        for i in range(cycles):
            try:
                out = loop.run_once() if hasattr(loop, "run_once") else None
                if isinstance(out, dict):
                    paper_fills += int(out.get("paper_fills") or 0)
                    reflections += int(out.get("reflections") or 0)
            except Exception as exc:
                errors.append(str(exc))
    except Exception as exc:
        errors.append(f"agent_loop: {exc}")

    # Scorecard snapshot
    score = {"total_experiences": 0, "improving": False, "reason": "soak"}
    try:
        from arbicore.scan_hook import get_memory

        mem = get_memory()
        if mem is not None and hasattr(mem, "list_experiences"):
            exps = mem.list_experiences(limit=500)  # type: ignore
            score["total_experiences"] = len(exps or [])
        try:
            from arbicore.scorecard import ImprovementScorecard

            sc = ImprovementScorecard().compute()
            if hasattr(sc, "as_dict"):
                score = sc.as_dict()
            elif isinstance(sc, dict):
                score = sc
        except Exception:
            pass
    except Exception as exc:
        errors.append(f"scorecard: {exc}")

    store = get_evidence_store()
    store.record_scorecard(score, mode="paper_soak")
    soak_id = store.record_soak(
        {
            "cycles": cycles,
            "paper_fills": paper_fills,
            "reflections": reflections,
            "notes": "; ".join(errors) if errors else "ok",
            "scorecard": score,
        }
    )
    trend = store.improvement_trend()

    print("=== Paper soak complete ===")
    print(f"soak_id={soak_id}")
    print(f"cycles={cycles} paper_fills={paper_fills} reflections={reflections}")
    print(f"scorecard={score}")
    print(f"trend={trend}")
    if errors:
        print("warnings:")
        for e in errors:
            print(" -", e)
    print("LIVE was not enabled. Unrestricted always-on remains disabled by design.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
