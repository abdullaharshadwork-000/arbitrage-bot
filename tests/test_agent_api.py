"""Unit tests for arbicore.agent_api – Phase 22."""

from __future__ import annotations

import unittest

from arbicore.agent_api import build_agent_snapshot, build_registry_snapshot
from arbicore.agent_loop import AgentObservationLoop
from arbicore.strategy_registry import StrategyRegistry


class AgentApiSnapshotTests(unittest.TestCase):
    def test_snapshot_without_loop(self):
        payload = build_agent_snapshot(None)
        self.assertTrue(payload["ok"])
        self.assertIn("agent_loop_enabled", payload)
        self.assertIn("state", payload)

    def test_snapshot_with_disabled_loop(self):
        loop = AgentObservationLoop(enabled=False)
        payload = build_agent_snapshot(loop)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["agent_loop_enabled"])
        self.assertEqual(payload["state"]["cycles"], 0)

    def test_snapshot_after_cycle(self):
        loop = AgentObservationLoop(enabled=True)
        prices = [100 + i * 0.4 for i in range(35)]
        loop.run_once("BTC/USDT", prices)
        payload = build_agent_snapshot(loop)
        self.assertEqual(payload["state"]["cycles"], 1)
        self.assertTrue(len(payload["recent"]) >= 1)

    def test_registry_snapshot(self):
        reg = StrategyRegistry()
        reg.create(name="demo", version="1.0.0")
        payload = build_registry_snapshot(reg)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["strategies"][0]["name"], "demo")


if __name__ == "__main__":
    unittest.main()
