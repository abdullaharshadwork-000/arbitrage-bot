"""Unit tests for arbicore.memory – Phase 2 Experience Memory."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arbicore.domain import (
    AuditCategory,
    AuditEvent,
    Experience,
    OperatingMode,
)
from arbicore.memory import ExperienceMemory


class ExperienceMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_memory.db"
        self.memory = ExperienceMemory(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_retrieve_experience(self):
        exp = Experience(
            id="exp_test_001",
            timestamp=datetime.now(timezone.utc),
            symbol="BTC/USDT",
            mode=OperatingMode.PAPER,
            strategy_id="cross_exchange",
            strategy_version="1.0.0",
            proposed_action="BUY",
            final_action="BUY",
            realized_pnl=Decimal("1.50"),
            fees=Decimal("0.30"),
            lessons=("edge survived latency",),
        )
        self.assertTrue(self.memory.record_experience(exp))
        self.assertEqual(self.memory.count_experiences(), 1)

        rows = self.memory.recent_experiences(symbol="BTC/USDT", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "exp_test_001")
        self.assertEqual(rows[0]["mode"], "paper")
        self.assertEqual(rows[0]["realized_pnl"], "1.50")

    def test_record_and_retrieve_audit_event(self):
        event = AuditEvent.create(
            AuditCategory.RISK,
            "trade_rejected",
            component="RiskManager",
            symbol="ETH/USDT",
            mode=OperatingMode.PAPER,
            reason="daily loss limit",
            payload={"loss": 55.0},
        )
        self.assertTrue(self.memory.record_audit_event(event))

        rows = self.memory.recent_audit_events(category="risk", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "trade_rejected")
        self.assertIn("daily loss limit", rows[0]["reason"])

    def test_empty_queries_return_empty_list(self):
        self.assertEqual(self.memory.recent_experiences(), [])
        self.assertEqual(self.memory.recent_audit_events(), [])
        self.assertEqual(self.memory.count_experiences(), 0)


if __name__ == "__main__":
    unittest.main()
