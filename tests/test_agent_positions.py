"""Agent position book unit tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from arbicore.agent_positions import AgentPositionBook
from arbicore.live_exec import LiveExecResult


def _fill(**kwargs):
    base = dict(
        executed=True,
        request_id="r1",
        order_id="o1",
        symbol="BTC/USDT",
        side="buy",
        quantity=0.01,
        notional_usdt=500,
        average_price=50000.0,
        canary_fraction=0.05,
        at=datetime.now(timezone.utc),
    )
    base.update(kwargs)
    return LiveExecResult(**base)


class PositionBookTests(unittest.TestCase):
    def test_open_and_tp(self):
        book = AgentPositionBook()
        pos = book.open_from_fill(
            _fill(),
            stop_loss=49000.0,
            take_profit=52000.0,
            strategy_id="demo",
        )
        self.assertIsNotNone(pos)
        self.assertEqual(len(book.open), 1)
        hits = book.check_exits({"BTC/USDT": 52001.0})
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "take_profit")

    def test_stop_loss_long(self):
        book = AgentPositionBook()
        book.open_from_fill(_fill(), stop_loss=49000.0, take_profit=52000.0)
        hits = book.check_exits({"BTC/USDT": 48900.0})
        self.assertEqual(hits[0][1], "stop_loss")

    def test_exit_request_opposite_side(self):
        book = AgentPositionBook()
        pos = book.open_from_fill(_fill(), stop_loss=49000.0)
        req = book.exit_request(pos)
        self.assertEqual(req.side, "sell")
        self.assertTrue(req.intent_id.startswith("exit_"))


if __name__ == "__main__":
    unittest.main()
