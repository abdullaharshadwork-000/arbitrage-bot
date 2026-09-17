"""Unit tests for arbicore.strategy_registry – Phase 5."""

from __future__ import annotations

import unittest

from arbicore.domain import StrategyStatus
from arbicore.strategy_registry import StrategyRegistry


class StrategyRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = StrategyRegistry()

    def test_create_and_get(self):
        sv = self.registry.create(
            name="cross_exchange",
            version="1.0.0",
            parameters={"min_profit_pct": 0.15},
            creation_reason="initial",
        )
        self.assertTrue(sv.id.startswith("strat_"))
        fetched = self.registry.get(sv.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.version, "1.0.0")
        self.assertEqual(fetched.status, StrategyStatus.DRAFT)

    def test_fork_creates_genealogy(self):
        parent = self.registry.create(name="cross_exchange", version="1.0.0")
        child = self.registry.fork(
            parent.id,
            "1.1.0",
            parameters={"min_profit_pct": 0.20},
            creation_reason="raised profit floor",
        )
        self.assertEqual(child.parent_version_id, parent.id)
        self.assertEqual(child.parameters["min_profit_pct"], 0.20)
        self.assertEqual(child.status, StrategyStatus.CANDIDATE)

        chain = self.registry.genealogy(child.id)
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].id, parent.id)
        self.assertEqual(chain[1].id, child.id)

    def test_set_status(self):
        sv = self.registry.create(name="triangular", version="0.1.0")
        updated = self.registry.set_status(sv.id, StrategyStatus.APPROVED)
        self.assertEqual(updated.status, StrategyStatus.APPROVED)
        self.assertEqual(self.registry.get(sv.id).status, StrategyStatus.APPROVED)

    def test_list_by_status(self):
        a = self.registry.create(name="a", version="1.0.0")
        self.registry.set_status(a.id, StrategyStatus.LIVE)
        b = self.registry.create(name="b", version="1.0.0")
        live = self.registry.list_by_status(StrategyStatus.LIVE)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].id, a.id)

    def test_latest(self):
        self.registry.create(name="x", version="1.0.0")
        self.registry.create(name="x", version="1.1.0")
        latest = self.registry.latest("x")
        self.assertEqual(latest.version, "1.1.0")


if __name__ == "__main__":
    unittest.main()
