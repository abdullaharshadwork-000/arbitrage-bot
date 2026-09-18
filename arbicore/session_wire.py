"""Wire agent observation + live execution into the running server session.

Call from the scan worker when a RealExecutionEngine (or paper) is available.
Never raises into the scan loop.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger("arbicore.session_wire")

_wired = False


def _env_true(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def wire_agent_session(
    *,
    engine: Any = None,
    risk_manager: Any = None,
    equity: float = 10_000.0,
    mode: str = "tutorial",
    execution_mode: str = "paper",
    real_trading_ack: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Attach agent loop (if flagged) and live wire (if LIVE_EXEC + real engine)."""
    global _wired
    result: dict[str, Any] = {
        "agent_loop": False,
        "live_wire": False,
        "risk": False,
        "notes": [],
    }
    try:
        # 1) Ensure scan_hook loop exists when agent loop flag is on
        if _env_true("ARBICORE_AGENT_LOOP"):
            try:
                from .scan_hook import get_agent_loop

                loop = get_agent_loop()
                result["agent_loop"] = loop is not None
                if loop is None:
                    result["notes"].append("agent loop flag on but loop is None")
            except Exception as exc:
                result["notes"].append(f"agent loop: {exc}")

        # 2) Build LiveModeGuard from session settings when possible
        live_guard = None
        try:
            from .config import Settings
            from .guards import LiveModeGuard

            settings = Settings(
                mode=str(mode or "tutorial"),
                execution_mode=str(execution_mode or "paper"),
                real_trading_ack=str(real_trading_ack or ""),
            )
            live_guard = LiveModeGuard(settings)
        except Exception as exc:
            result["notes"].append(f"live_guard: {exc}")

        # 3) Risk context for agent approvals
        if risk_manager is not None:
            try:
                from .agent_live_wire import register_risk_context

                register_risk_context(
                    risk_manager, equity=float(equity), live_guard=live_guard
                )
                result["risk"] = True
            except Exception as exc:
                result["notes"].append(f"risk context: {exc}")

        # 4) Live wire when LIVE_EXEC + engine present
        if engine is not None and (_env_true("ARBICORE_AGENT_LIVE_EXEC") or force):
            try:
                from .server_live_hook import maybe_register_from_engine

                ok = maybe_register_from_engine(
                    engine,
                    live_guard=live_guard,
                    risk_manager=risk_manager,
                    equity=float(equity),
                    force=force or not _wired,
                )
                result["live_wire"] = bool(ok)
                if not ok:
                    result["notes"].append("live wire registration returned False")
            except Exception as exc:
                result["notes"].append(f"live wire: {exc}")
        elif _env_true("ARBICORE_AGENT_LIVE_EXEC") and engine is None:
            result["notes"].append("LIVE_EXEC on but no engine yet")

        _wired = True
    except Exception as exc:
        result["notes"].append(f"wire failed: {exc}")
        log.warning("wire_agent_session failed: %s", exc)
    return result


def reset_wire_flag() -> None:
    global _wired
    _wired = False


__all__ = ["wire_agent_session", "reset_wire_flag"]
