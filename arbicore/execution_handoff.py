"""Execution handoff for ApprovedOrderRequest.

Phase 22 completion.

When ARBICORE_AGENT_HANDOFF=1, approved risk-cleared requests are queued
for inspection / optional downstream use by existing execution code.

Hard rules:
* Never calls Binance / ccxt.
* Never places orders itself.
* LIVE handoff still requires LiveModeGuard + existing Settings.real path.
* Default is OFF.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .risk_adapter import ApprovedOrderRequest


def handoff_enabled() -> bool:
    return os.environ.get("ARBICORE_AGENT_HANDOFF", "").strip() in {"1", "true", "yes", "on"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class HandoffQueue:
    """In-process queue of risk-cleared requests (for operators / future bridge)."""

    max_size: int = 200
    items: deque = field(default_factory=lambda: deque(maxlen=200))

    def push(self, request: ApprovedOrderRequest) -> dict[str, Any]:
        entry = {
            "queued_at": _utc_now().isoformat(),
            "request": request.as_dict(),
            "status": "queued",
            "note": (
                "Queued only. Existing execution path must still apply "
                "LiveModeGuard and place the order if LIVE is enabled."
            ),
        }
        self.items.append(entry)
        return entry

    def snapshot(self, limit: int = 20) -> dict[str, Any]:
        recent = list(self.items)[-limit:]
        return {
            "enabled": handoff_enabled(),
            "queued": len(self.items),
            "recent": recent,
        }

    def clear(self) -> None:
        self.items.clear()


_default_queue = HandoffQueue()


def handoff(request: ApprovedOrderRequest, *,
            queue: Optional[HandoffQueue] = None) -> Optional[dict[str, Any]]:
    """Queue an approved request if handoff flag is on. Never places orders."""
    if not handoff_enabled():
        return None
    q = queue if queue is not None else _default_queue
    return q.push(request)


def handoff_snapshot() -> dict[str, Any]:
    return _default_queue.snapshot()


__all__ = [
    "handoff_enabled",
    "HandoffQueue",
    "handoff",
    "handoff_snapshot",
]
