"""Unit tests for arbicore.agent_loop – Phases 21 + 25."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arbicore.agent_loop import (
    AgentObservationLoop,
    agent_loop_enabled,
    paper_exec_enabled,
)
from arbicore.domain import OperatingMode, StrategyStatus
from arbicore.memory import ExperienceMemory
from arbicore.paper_exec import PaperExecutor
from arbicore.strategy_registry import StrategyRegistry


class AgentLoopFlagTests(unittest.TestCase):
    def test_default_disabled(self):
        self.assertFalse(agent_loop_enabled({}))
        self.assertFalse(paper_exec_enabled({}))

    def test_enabled_via_env(self):
        self.assertTrue(agent_loop_enabled({"ARBICORE_AGENT_LOOP": "1"}))
        self.assertTrue(paper_exec_enabled({"ARBICORE_AGENT_PAPER_EXEC": "true"}))


class AgentObservationLoopTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        loop = AgentObservationLoop(enabled=False)
        result = loop.run_once("BTC/USDT", [100 + i for i in range(30)])
        self.assertIsNone(result)
        self.assertEqual(loop.state.cycles, 0)

    def test_enabled_runs_cycle(self):
        loop = AgentObservationLoop(enabled=True, enable_paper_exec=False)
        prices = [100 + i * 0.5 for i in range(40)]
        result = loop.run_once("ETH/USDT", prices)
        self.assertIsNotNone(result)
        self.assertEqual(loop.state.cycles, 1)

    def test_cannot_use_live_mode(self):
        with self.assertRaises(ValueError):
            AgentObservationLoop(mode=OperatingMode.LIVE)

    def test_paper_exec_path(self):
        registry = StrategyRegistry()
        sv = registry.create(
            name="mom",
            version="1.0.0",
            intended_regimes=(
                "STRONG_BULL_TREND",
                "WEAK_BULL_TREND",
                "STRONG_BEAR_TREND",
                "WEAK_BEAR_TREND",
            ),
        )
        registry.set_status(sv.id, StrategyStatus.APPROVED)

        with tempfile.TemporaryDirectory() as tmp:
            memory = ExperienceMemory(Path(tmp) / "mem.db")
            paper = PaperExecutor(equity=10_000.0, mode=OperatingMode.PAPER)
            loop = AgentObservationLoop(
                registry=registry,
                memory=memory,
                paper_executor=paper,
                enabled=True,
                enable_paper_exec=True,
            )
            prices = [100 + i * 1.2 for i in range(50)]
            result = loop.run_once("BTC/USDT", prices)
            self.assertIsNotNone(result)
            self.assertIsNotNone(loop.state.last_paper)
            snap = loop.state.snapshot()
            self.assertTrue(snap["paper_exec_enabled"])


if __name__ == "__main__":
    unittest.main()
