"""Unit tests for arbicore.features – Phase 3 Feature Engine."""

from __future__ import annotations

import unittest

from arbicore.features import FeatureEngine, FeatureSnapshot


class FeatureEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = FeatureEngine(min_bars=5)

    def test_insufficient_data_returns_empty_features(self):
        snap = self.engine.compute("BTC/USDT", [100.0, 101.0])
        self.assertEqual(snap.meta["status"], "insufficient_data")
        self.assertEqual(snap.features, {})

    def test_basic_returns_and_momentum(self):
        # Strictly increasing series
        prices = [100 + i for i in range(25)]
        snap = self.engine.compute("ETH/USDT", prices, as_of=1_700_000_000)
        self.assertEqual(snap.meta["status"], "ok")
        self.assertEqual(snap.symbol, "ETH/USDT")
        self.assertIn("price", snap.features)
        self.assertIn("return_1", snap.features)
        self.assertIn("momentum_5", snap.features)
        self.assertIn("sma_10", snap.features)
        self.assertIn("realized_vol", snap.features)
        self.assertGreater(snap.get("momentum_5"), 0)

    def test_no_lookahead_on_short_series(self):
        prices = [10.0, 10.5, 10.2, 10.8, 11.0]
        snap = self.engine.compute("SOL/USDT", prices)
        self.assertEqual(snap.meta["bars"], 5)
        self.assertIn("return_1", snap.features)

    def test_volume_features_when_supplied(self):
        prices = [100 + i * 0.1 for i in range(20)]
        volumes = [1000 + i * 10 for i in range(20)]
        snap = self.engine.compute("BTC/USDT", prices, volumes=volumes)
        self.assertIn("volume", snap.features)
        self.assertIn("rel_volume_5", snap.features)

    def test_snapshot_as_dict(self):
        snap = FeatureSnapshot(
            symbol="BTC/USDT",
            timestamp=123.0,
            features={"price": 50000.0},
            meta={"status": "ok"},
        )
        data = snap.as_dict()
        self.assertEqual(data["symbol"], "BTC/USDT")
        self.assertEqual(data["features"]["price"], 50000.0)


if __name__ == "__main__":
    unittest.main()
