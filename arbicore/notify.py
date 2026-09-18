"""Operator notifications for critical events.

Default: structured logging. Optional webhook via ARBICORE_NOTIFY_WEBHOOK.
Never raises into trading path.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional
from urllib import request

log = logging.getLogger("arbicore.notify")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def notify(event: str, *,
           severity: str = "info",
           detail: Optional[dict[str, Any]] = None) -> bool:
    payload = {
        "event": event,
        "severity": severity,
        "detail": detail or {},
        "at": _utc_now(),
    }
    try:
        if severity in ("critical", "error"):
            log.error("notify %s %s", event, payload)
        elif severity == "warning":
            log.warning("notify %s %s", event, payload)
        else:
            log.info("notify %s %s", event, payload)
    except Exception:
        pass

    url = os.environ.get("ARBICORE_NOTIFY_WEBHOOK", "").strip()
    if not url:
        return True
    try:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception as exc:
        log.warning("webhook failed: %s", exc)
        return False


def notify_kill_switch(reason: str) -> None:
    notify("kill_switch", severity="critical", detail={"reason": reason})


def notify_reconcile_fail(notes: list) -> None:
    notify("reconcile_failed", severity="error", detail={"notes": notes})


def notify_drift(strategy_id: str, severity: str = "warning") -> None:
    notify(
        "strategy_drift",
        severity=severity,
        detail={"strategy_id": strategy_id},
    )


__all__ = [
    "notify",
    "notify_kill_switch",
    "notify_reconcile_fail",
    "notify_drift",
]
