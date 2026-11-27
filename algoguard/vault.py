"""Encrypt exchange API keys at rest (Fernet: AES-128-CBC + HMAC-SHA256). Ported from IntelliStock."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    pass


class Vault:
    def __init__(self, secret_key: str, encryption_key: str | None = None):
        seed = encryption_key or ("exchange:" + secret_key)
        self._f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest()))

    def seal(self, text: str) -> str:
        return self._f.encrypt(text.encode()).decode()

    def unseal(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            raise VaultError("Stored exchange keys can no longer be read (the server's key changed). "
                             "Connect again.") from None
