"""Shadow book: record hypothetical trades from live data without placing orders.

Used for challenger evaluation before paper/canary.
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


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db() -> Path:
    override = os.environ.get("ARBICORE_SHADOW_DB")
    if override:
        return Path(override)
    main = os.environ.get("ARBICORE_DB")
    if main:
        return Path(main).with_name("arbicore_shadow.db")
    return Path.cwd() / "arbicore_shadow.db"


class ShadowBook:
    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path) if db_path else _db()
        self._ensure()

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_trades (
                    id TEXT PRIMARY KEY,
                    at TEXT NOT NULL,
                    strategy_id TEXT,
                    strategy_version TEXT,
                    symbol TEXT,
                    side TEXT,
                    entry REAL,
                    exit REAL,
                    qty REAL,
                    pnl REAL,
                    regime TEXT,
                    payload TEXT
                )
                """
            )

    def record(
        self,
        *,
        strategy_id: str,
        strategy_version: str = "",
        symbol: str,
        side: str,
        entry: float,
        exit_price: Optional[float] = None,
        qty: float = 0.0,
        pnl: Optional[float] = None,
        regime: str = "",
        extra: Optional[dict] = None,
    ) -> str:
        tid = f"sh_{uuid4().hex[:12]}"
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO shadow_trades (
                    id, at, strategy_id, strategy_version, symbol, side,
                    entry, exit, qty, pnl, regime, payload
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    tid,
                    _utc(),
                    strategy_id,
                    strategy_version,
                    symbol,
                    side,
                    float(entry),
                    float(exit_price) if exit_price is not None else None,
                    float(qty),
                    float(pnl) if pnl is not None else None,
                    regime,
                    json.dumps(extra or {}, default=str),
                ),
            )
        return tid

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM shadow_trades ORDER BY at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def snapshot(self) -> dict[str, Any]:
        rows = self.recent(100)
        pnls = [r["pnl"] for r in rows if r.get("pnl") is not None]
        return {
            "count": len(rows),
            "sum_pnl": sum(pnls) if pnls else 0.0,
            "recent": rows[:20],
        }


_book: Optional[ShadowBook] = None


def get_shadow_book() -> ShadowBook:
    global _book
    if _book is None:
        _book = ShadowBook()
    return _book


__all__ = ["ShadowBook", "get_shadow_book"]
