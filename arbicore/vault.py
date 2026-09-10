"""Encrypted exchange-credential storage.

The database never contains plaintext credentials. Persistence is deliberately
disabled unless the operator supplies a Fernet key through
``ARBICORE_MASTER_KEY``; silently inventing a new key would make credentials
unrecoverable after the next restart.
"""

import json
import os

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    def __init__(self, key=None):
        self._key = (os.environ.get("ARBICORE_MASTER_KEY", "").strip()
                     if key is None else str(key).strip())
        self._fernet = Fernet(self._key.encode("ascii")) if self._key else None

    @property
    def enabled(self):
        return self._fernet is not None

    def encrypt(self, api_key, api_secret, password=""):
        if not self._fernet:
            raise RuntimeError("Encrypted credential persistence is not configured.")
        values = {"apiKey": api_key, "secret": api_secret}
        if password:
            values["password"] = password
        payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def decrypt(self, ciphertext):
        if not self._fernet:
            raise RuntimeError("Encrypted credential persistence is not configured.")
        try:
            payload = self._fernet.decrypt(ciphertext.encode("ascii"))
        except InvalidToken as exc:
            raise RuntimeError("Stored exchange credentials cannot be decrypted.") from exc
        result = json.loads(payload.decode("utf-8"))
        values = {"apiKey": result["apiKey"], "secret": result["secret"]}
        if result.get("password"):
            values["password"] = result["password"]
        return values

    def encrypt_text(self, value):
        if not self.enabled:
            raise RuntimeError("Encrypted persistence is not configured.")
        return self._fernet.encrypt(str(value).encode("utf-8")).decode("ascii")

    def decrypt_text(self, ciphertext):
        if not self.enabled:
            raise RuntimeError("Encrypted persistence is not configured.")
        try:
            return self._fernet.decrypt(str(ciphertext).encode("ascii")).decode("utf-8")
        except Exception as exc:
            raise RuntimeError("Stored encrypted value cannot be decrypted.") from exc
