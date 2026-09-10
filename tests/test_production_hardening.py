import unittest

from cryptography.fernet import Fernet

from arbicore.vault import CredentialVault


class CredentialVaultTests(unittest.TestCase):
    def test_credentials_round_trip_without_plaintext_ciphertext(self):
        vault = CredentialVault(Fernet.generate_key().decode("ascii"))
        ciphertext = vault.encrypt("public-key", "super-secret")

        self.assertNotIn("public-key", ciphertext)
        self.assertNotIn("super-secret", ciphertext)
        self.assertEqual(vault.decrypt(ciphertext), {
            "apiKey": "public-key",
            "secret": "super-secret",
        })

    def test_persistence_is_disabled_without_an_explicit_key(self):
        vault = CredentialVault("")
        self.assertFalse(vault.enabled)
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            vault.encrypt("key", "secret")

    def test_exchange_passphrase_is_encrypted_and_restored(self):
        vault = CredentialVault(Fernet.generate_key().decode("ascii"))
        ciphertext = vault.encrypt("public-key", "super-secret", "api-passphrase")

        self.assertNotIn("api-passphrase", ciphertext)
        self.assertEqual(vault.decrypt(ciphertext), {
            "apiKey": "public-key",
            "secret": "super-secret",
            "password": "api-passphrase",
        })
