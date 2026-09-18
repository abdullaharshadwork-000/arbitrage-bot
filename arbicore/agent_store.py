"""Persist agent open/closed positions to SQLite.

Separate from main trades table. Best-effort; never raises into the scan loop.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional


def _default_db() -> Path:
    override = os.environ.get("ARBICORE_AGENT_DB")
    if override:
        return Path(override)
    main_db = os.environ.get("ARBICORE_DB")
    if main_db:
        return Path(main_db).with_name("arbicore_agent.db")
    return Path.cwd() / "arbicore_agent.db"


class AgentStore:
    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path) if db_path else _default_db()
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
        try:
            with self._connect() as c:
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_positions (
                        id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quantity REAL NOT NULL,
                        entry_price REAL NOT NULL,
                        stop_loss REAL,
                        take_profit REAL,
                        strategy_id TEXT,
                        strategy_version TEXT,
                        request_id TEXT,
                        order_id TEXT,
                        status TEXT NOT NULL,
                        opened_at TEXT NOT NULL,
                        closed_at TEXT,
                        exit_price REAL,
                        exit_reason TEXT
                    )
                    """
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_agent_pos_status "
                    "ON agent_positions(status)"
                )
        except Exception:
            pass

    def upsert_position(self, data: dict[str, Any]) -> bool:
        try:
            with self._connect() as c:
                c.execute(
                    """
                    INSERT INTO agent_positions (
                        id, symbol, side, quantity, entry_price, stop_loss,
                        take_profit, strategy_id, strategy_version, request_id,
                        order_id, status, opened_at, closed_at, exit_price,
                        exit_reason
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        closed_at=excluded.closed_at,
                        exit_price=excluded.exit_price,
                        exit_reason=excluded.exit_reason,
                        stop_loss=excluded.stop_loss,
                        take_profit=excluded.take_profit
                    """,
                    (
                        data.get("id"),
                        data.get("symbol"),
                        data.get("side"),
                        float(data.get("quantity") or 0),
                        float(data.get("entry_price") or 0),
                        data.get("stop_loss"),
                        data.get("take_profit"),
                        data.get("strategy_id") or "",
                        data.get("strategy_version") or "",
                        data.get("request_id") or "",
                        data.get("order_id") or "",
                        data.get("status") or "open",
                        data.get("opened_at") or "",
                        data.get("closed_at"),
                        data.get("exit_price"),
                        data.get("exit_reason") or "",
                    ),
                )
            return True
        except Exception:
            return False

    def load_open(self) -> list[dict[str, Any]]:
        try:
            with self._connect() as c:
                rows = c.execute(
                    "SELECT * FROM agent_positions WHERE status IN ('open','exit_pending')"
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def load_closed(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            with self._connect() as c:
                rows = c.execute(
                    "SELECT * FROM agent_positions WHERE status='closed' "
                    "ORDER BY closed_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []


_store: Optional[AgentStore] = None


def get_agent_store() -> AgentStore:
    global _store
    if _store is None:
        _store = AgentStore()
    return _store


__all__ = ["AgentStore", "get_agent_store"]
