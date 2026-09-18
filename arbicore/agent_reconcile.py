"""Reconcile agent open positions after restart.

If local agent book and exchange state disagree, block new agent entries
until operator clears or positions are aligned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .agent_positions import get_position_book
from .kill_switch import get_kill_switch


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ReconcileReport:
    ok: bool
    open_local: int = 0
    notes: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "open_local": self.open_local,
            "notes": list(self.notes),
            "checked_at": self.checked_at.isoformat(),
        }


class AgentReconciler:
    """Best-effort local consistency check.

    Full exchange fetch is injected via optional exchange_positions callable
    so this module never imports ccxt directly.
    """

    def __init__(self, exchange_positions=None):
        self.exchange_positions = exchange_positions
        self.last: Optional[ReconcileReport] = None

    def run(self) -> ReconcileReport:
        book = get_position_book()
        notes: list[str] = []
        open_local = len(book.open)
        notes.append(f"local open agent positions: {open_local}")

        ok = True
        if self.exchange_positions is not None:
            try:
                remote = self.exchange_positions() or []
                notes.append(f"exchange positions fetched: {len(remote)}")
                # Soft check only – detailed symbol matching is operator-owned
                if open_local > 0 and len(remote) == 0:
                    notes.append(
                        "WARNING: local agent positions open but exchange reported none"
                    )
                    ok = False
            except Exception as exc:
                notes.append(f"exchange fetch failed: {exc}")
                ok = False
        else:
            notes.append("no exchange_positions callback – local-only check")

        report = ReconcileReport(ok=ok, open_local=open_local, notes=notes)
        self.last = report
        if not ok:
            get_kill_switch().trip(
                "agent_reconcile_failed",
                source="agent_reconcile",
            )
        return report


_reconciler: Optional[AgentReconciler] = None


def get_agent_reconciler() -> AgentReconciler:
    global _reconciler
    if _reconciler is None:
        _reconciler = AgentReconciler()
    return _reconciler


__all__ = ["ReconcileReport", "AgentReconciler", "get_agent_reconciler"]
