"""Experience Memory – persistent long-term trading memory.

Phase 2.

Stores:
* Experience records (completed decision cycles)
* Rich AuditEvent records (from arbicore.domain)

Design rules:
* Additive only – never modifies or replaces the existing trades / audit_events tables.
* All writes are best-effort and never raise into the trading loop.
* Schema is created on first use via IF NOT EXISTS.
* Nothing here places orders or weakens risk limits.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .domain import AuditEvent, Experience, OperatingMode


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ExperienceMemory:
    """Thin SQLite-backed store for Experience and AuditEvent records."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._ensure_schema()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiences (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    strategy_id TEXT,
                    strategy_version TEXT,
                    regime TEXT,
                    regime_confidence REAL,
                    proposed_action TEXT,
                    final_action TEXT,
                    trade_confidence REAL,
                    requested_risk_fraction TEXT,
                    approved_risk_fraction TEXT,
                    entry_price TEXT,
                    actual_entry TEXT,
                    stop_loss TEXT,
                    take_profit TEXT,
                    position_size TEXT,
                    realized_pnl TEXT,
                    fees TEXT,
                    slippage TEXT,
                    holding_duration_seconds REAL,
                    exit_reason TEXT,
                    critic_result TEXT,
                    risk_result TEXT,
                    feature_snapshot TEXT,
                    reflection TEXT,
                    lessons TEXT,
                    audit_event_ids TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS experiences_symbol_time
                    ON experiences (symbol, timestamp);
                CREATE INDEX IF NOT EXISTS experiences_strategy
                    ON experiences (strategy_id, strategy_version);

                CREATE TABLE IF NOT EXISTS domain_audit_events (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    component TEXT,
                    strategy_id TEXT,
                    strategy_version TEXT,
                    symbol TEXT,
                    mode TEXT,
                    payload TEXT,
                    reason TEXT,
                    parent_event_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS domain_audit_events_time
                    ON domain_audit_events (timestamp);
                CREATE INDEX IF NOT EXISTS domain_audit_events_category
                    ON domain_audit_events (category, event_type);
                """
            )

    # ------------------------------------------------------------------ write

    def record_experience(self, experience: Experience) -> bool:
        """Persist one Experience. Returns True on success, False on failure.

        Never raises into the caller – memory must not break the trading loop.
        """
        try:
            data = experience.as_dict()
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO experiences (
                        id, timestamp, symbol, mode, strategy_id, strategy_version,
                        regime, regime_confidence, proposed_action, final_action,
                        trade_confidence, requested_risk_fraction, approved_risk_fraction,
                        entry_price, actual_entry, stop_loss, take_profit, position_size,
                        realized_pnl, fees, slippage, holding_duration_seconds,
                        exit_reason, critic_result, risk_result, feature_snapshot,
                        reflection, lessons, audit_event_ids, created_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        data["id"],
                        data["timestamp"],
                        data["symbol"],
                        data["mode"],
                        data.get("strategy_id"),
                        data.get("strategy_version"),
                        data.get("regime"),
                        data.get("regime_confidence"),
                        data.get("proposed_action"),
                        data.get("final_action"),
                        data.get("trade_confidence"),
                        data.get("requested_risk_fraction"),
                        data.get("approved_risk_fraction"),
                        data.get("entry_price"),
                        data.get("actual_entry"),
                        data.get("stop_loss"),
                        data.get("take_profit"),
                        data.get("position_size"),
                        data.get("realized_pnl"),
                        data.get("fees"),
                        data.get("slippage"),
                        data.get("holding_duration_seconds"),
                        data.get("exit_reason"),
                        data.get("critic_result"),
                        data.get("risk_result"),
                        json.dumps(data.get("feature_snapshot") or {}),
                        data.get("reflection"),
                        json.dumps(list(data.get("lessons") or [])),
                        json.dumps(list(data.get("audit_event_ids") or [])),
                        _utc_now_iso(),
                    ),
                )
            return True
        except Exception:
            return False

    def record_audit_event(self, event: AuditEvent) -> bool:
        """Persist one domain AuditEvent. Best-effort; never raises."""
        try:
            data = event.as_dict()
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO domain_audit_events (
                        id, category, event_type, timestamp, component,
                        strategy_id, strategy_version, symbol, mode,
                        payload, reason, parent_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data["id"],
                        data["category"],
                        data["event_type"],
                        data["timestamp"],
                        data.get("component"),
                        data.get("strategy_id"),
                        data.get("strategy_version"),
                        data.get("symbol"),
                        data.get("mode"),
                        json.dumps(data.get("payload") or {}),
                        data.get("reason"),
                        data.get("parent_event_id"),
                        _utc_now_iso(),
                    ),
                )
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ read

    def recent_experiences(
        self,
        *,
        symbol: Optional[str] = None,
        strategy_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if strategy_id:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM experiences
                {where}
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_audit_events(
        self,
        *,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM domain_audit_events
                {where}
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_experiences(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS n FROM experiences").fetchone()
        return int(row["n"] if row else 0)


__all__ = ["ExperienceMemory"]
