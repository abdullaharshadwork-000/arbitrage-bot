"""Unit tests for arbicore.agent_loop – Phase 21."""

from __future__ import annotations

import unittest

from arbicore.agent_loop import AgentObservationLoop, agent_loop_enabled


class AgentLoopFlagTests(unittest.TestCase):
    def test_default_disabled(self):
        self.assertFalse(agent_loop_enabled({}))

    def test_enabled_via_env(self):
        self.assertTrue(agent_loop_enabled({"ARBICORE_AGENT_LOOP": "1"}))
        self.assertTrue(agent_loop_enabled({"ARBICORE_AGENT_LOOP": "true"}))
        self.assertFalse(agent_loop_enabled({"ARBICORE_AGENT_LOOP": "0"}))


class AgentObservationLoopTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        loop = AgentObservationLoop(enabled=False)
        result = loop.run_once("BTC/USDT", [100 + i for i in range(30)])
        self.assertIsNone(result)
        self.assertEqual(loop.state.cycles, 0)

    def test_enabled_runs_cycle(self):
        loop = AgentObservationLoop(enabled=True)
        prices = [100 + i * 0.5 for i in range(40)]
        result = loop.run_once("ETH/USDT", prices)
        self.assertIsNotNone(result)
        self.assertEqual(loop.state.cycles, 1)
        # Orchestrator must not invent real entries yet
        self.assertEqual(result.proposal.action, "NO_TRADE")

    def test_snapshot(self):
        loop = AgentObservationLoop(enabled=False)
        snap = loop.state.snapshot()
        self.assertIn("enabled", snap)
        self.assertFalse(snap["enabled"])


if __name__ == "__main__":
    unittest.main()
