"""Hard kill switch for agent + engine live path.

Trip reasons are recorded. While active, LiveExecutor and handoff must refuse
new entries. Does not cancel exchange orders by itself (operator / engine does).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class KillState:
    active: bool = False
    reason: str = ""
    tripped_at: Optional[datetime] = None
    source: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "tripped_at": self.tripped_at.isoformat() if self.tripped_at else None,
            "source": self.source,
            "history": list(self.history[-20:]),
        }


class KillSwitch:
    def __init__(self) -> None:
        self.state = KillState()
        if os.environ.get("ARBICORE_KILL", "").strip() in ("1", "true", "yes"):
            self.trip("env ARBICORE_KILL", source="environment")

    def trip(self, reason: str, *,
             source: str = "system") -> KillState:
        self.state.active = True
        self.state.reason = str(reason or "unspecified")
        self.state.tripped_at = _utc_now()
        self.state.source = source
        self.state.history.append(
            {
                "event": "trip",
                "reason": self.state.reason,
                "source": source,
                "at": self.state.tripped_at.isoformat(),
            }
        )
        return self.state

    def clear(self, *,
              source: str = "operator") -> KillState:
        """Operator-only recovery. Never auto-clear on agent success."""
        self.state.history.append(
            {
                "event": "clear",
                "source": source,
                "at": _utc_now().isoformat(),
                "previous_reason": self.state.reason,
            }
        )
        self.state.active = False
        self.state.reason = ""
        self.state.tripped_at = None
        self.state.source = ""
        return self.state

    def is_active(self) -> bool:
        return bool(self.state.active)

    def block_reason(self) -> Optional[str]:
        if not self.state.active:
            return None
        return f"kill_switch: {self.state.reason}"


_switch: Optional[KillSwitch] = None


def get_kill_switch() -> KillSwitch:
    global _switch
    if _switch is None:
        _switch = KillSwitch()
    return _switch


__all__ = ["KillState", "KillSwitch", "get_kill_switch"]
