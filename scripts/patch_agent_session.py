#!/usr/bin/env python3
"""Idempotent patch: call wire_agent_session from supervised scan loop."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"

IMPORT = (
    "from arbicore.session_wire import wire_agent_session\n"
)

CALL_BLOCK = '''
        # Wire agent observation + optional live agent path (never raises)
        try:
            wire_agent_session(
                engine=locals().get("engine") or locals().get("real_engine"),
                risk_manager=locals().get("risk_manager"),
                equity=float((cfg or {}).get("trade_size") or 10000),
                mode=str((cfg or {}).get("mode") or bot.MODE),
                execution_mode=str((cfg or {}).get("execution_mode") or bot.EXECUTION_MODE),
                real_trading_ack=str((cfg or {}).get("real_trading_ack") or ""),
            )
        except Exception:
            pass
'''


def main() -> int:
    text = SERVER.read_text(encoding="utf-8")
    changed = False

    if "from arbicore.session_wire import wire_agent_session" not in text:
        anchor = "from arbicore.server_live_hook import maybe_process_handoff, maybe_register_from_engine\n"
        if anchor in text:
            text = text.replace(
                anchor,
                anchor + IMPORT,
                1,
            )
            changed = True
            print("added session_wire import")
        else:
            print("WARNING: import anchor not found")

    if "wire_agent_session(" not in text:
        # Insert before notify_agent_mid block
        needle = (
            "        # Optional agent observation (no-op unless ARBICORE_AGENT_LOOP=1; never raises)\n"
            "        try:\n"
            "            for _sym, _mid in (mid_prices or {}).items():\n"
            "                notify_agent_mid(_sym, _mid)\n"
        )
        if needle in text:
            text = text.replace(needle, CALL_BLOCK + "\n" + needle, 1)
            changed = True
            print("inserted wire_agent_session call in scan loop")
        else:
            # softer match
            soft = "            for _sym, _mid in (mid_prices or {}).items():\n                notify_agent_mid(_sym, _mid)\n"
            if soft in text:
                text = text.replace(
                    soft,
                    CALL_BLOCK + "\n" + soft,
                    1,
                )
                changed = True
                print("inserted wire_agent_session (soft match)")
            else:
                print("WARNING: scan hook block not found – add wire_agent_session manually")
                return 1

    if changed:
        SERVER.write_text(text, encoding="utf-8")
        print("server.py updated")
    else:
        print("no changes needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
