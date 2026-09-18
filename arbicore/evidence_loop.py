"""Accumulate self-improvement *evidence* over time.

This does not invent live performance. It stores periodic snapshots so that
after paper/soak/canary runs the operator can answer:

  "Are later generations making better decisions?"

Unrestricted always-on LIVE is intentionally out of scope.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    override = os.environ.get("ARBICORE_EVIDENCE_DB")
    if override:
        return Path(override)
    main = os.environ.get("ARBICORE_DB")
    if main:
        return Path(main).with_name("arbicore_evidence.db")
    return Path.cwd() / "arbicore_evidence.db"


class EvidenceStore:
    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path) if db_path else _db_path()
        self._ensure()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure(self) -> None:
        with self._connect() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_snapshots (
                    id TEXT PRIMARY KEY,
                    at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    total_experiences INTEGER,
                    early_win_rate REAL,
                    late_win_rate REAL,
                    improving INTEGER,
                    reason TEXT,
                    payload TEXT
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS soak_runs (
                    id TEXT PRIMARY KEY,
                    at TEXT NOT NULL,
                    cycles INTEGER,
                    paper_fills INTEGER,
                    reflections INTEGER,
                    notes TEXT,
                    payload TEXT
                )
                """
            )

    def record_scorecard(
        self,
        scorecard: dict[str, Any],
        *,
        mode: str = "paper",
    ) -> str:
        sid = f"ev_{uuid4().hex[:12]}"
        sc = scorecard.get("scorecard") if "scorecard" in scorecard else scorecard
        with self._connect() as c:
            c.execute(
                """
                INSERT INTO evidence_snapshots (
                    id, at, mode, total_experiences, early_win_rate,
                    late_win_rate, improving, reason, payload
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    sid,
                    _utc_now(),
                    mode,
                    int(sc.get("total_experiences") or 0),
                    sc.get("early_win_rate"),
                    sc.get("late_win_rate"),
                    1 if sc.get("improving") else 0,
                    str(sc.get("reason") or ""),
                    json.dumps(scorecard, default=str),
                ),
            )
        return sid

    def record_soak(self, summary: dict[str, Any]) -> str:
        sid = f"soak_{uuid4().hex[:12]}"
        with self._connect() as c:
            c.execute(
                """
                INSERT INTO soak_runs (
                    id, at, cycles, paper_fills, reflections, notes, payload
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    sid,
                    _utc_now(),
                    int(summary.get("cycles") or 0),
                    int(summary.get("paper_fills") or 0),
                    int(summary.get("reflections") or 0),
                    str(summary.get("notes") or ""),
                    json.dumps(summary, default=str),
                ),
            )
        return sid

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM evidence_snapshots ORDER BY at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def soak_history(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM soak_runs ORDER BY at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def improvement_trend(self) -> dict[str, Any]:
        hist = list(reversed(self.history(limit=20)))
        if len(hist) < 2:
            return {
                "enough_data": False,
                "snapshots": len(hist),
                "message": "Need more soak/paper cycles to compare generations",
            }
        first = hist[0]
        last = hist[-1]
        e0 = first.get("early_win_rate")
        l1 = last.get("late_win_rate")
        improving_flags = sum(1 for h in hist if h.get("improving"))
        return {
            "enough_data": True,
            "snapshots": len(hist),
            "first_at": first.get("at"),
            "last_at": last.get("at"),
            "first_early_wr": e0,
            "last_late_wr": l1,
            "improving_snapshot_count": improving_flags,
            "trend_hint": (
                "late window stronger than first early window"
                if (l1 is not None and e0 is not None and l1 > e0)
                else "no clear improvement yet – keep soaking"
            ),
        }


_store: Optional[EvidenceStore] = None


def get_evidence_store() -> EvidenceStore:
    global _store
    if _store is None:
        _store = EvidenceStore()
    return _store


__all__ = ["EvidenceStore", "get_evidence_store"]
