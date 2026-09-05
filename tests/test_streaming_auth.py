import unittest

from arbicore import auth
from arbicore.streaming import StreamingBookCache


class Clock:
    def __init__(self, value=100.0): self.value = value
    def __call__(self): return self.value


class StreamingCacheTests(unittest.TestCase):
    def test_fresh_complete_books_are_available(self):
        clock = Clock()
        cache = StreamingBookCache(clock=clock)
        self.assertTrue(cache.update("binance", "BTC/USDT", 100, 101, 1))
        self.assertTrue(cache.complete(["binance"], ["BTC/USDT"]))
        self.assertEqual(cache.snapshot(["binance"], ["BTC/USDT"])["BTC/USDT"]["binance"]["source"], "websocket")

    def test_stale_books_force_rest_fallback(self):
        clock = Clock()
        cache = StreamingBookCache(max_age_seconds=3, clock=clock)
        cache.update("binance", "BTC/USDT", 100, 101, 1)
        clock.value += 4
        self.assertFalse(cache.complete(["binance"], ["BTC/USDT"]))

    def test_sequence_gap_discards_the_book(self):
        cache = StreamingBookCache()
        cache.update("binance", "BTC/USDT", 100, 101, 10)
        self.assertFalse(cache.update("binance", "BTC/USDT", 100, 101, 12))
        self.assertEqual(cache.health()["sequence_gaps"], 1)


class TotpTests(unittest.TestCase):
    def test_rfc6238_code_verifies_only_near_its_time_window(self):
        secret = "JBSWY3DPEHPK3PXP"
        code = auth.totp(secret, moment=1_700_000_000)
        self.assertTrue(auth.verify_totp(secret, code, moment=1_700_000_000))
        self.assertFalse(auth.verify_totp(secret, code, moment=1_700_001_000))

    def test_recovery_tokens_are_stored_as_hashes(self):
        code = auth.recovery_codes(1)[0]
        self.assertNotEqual(code, auth.hash_token(code))


if __name__ == "__main__":
    unittest.main()
