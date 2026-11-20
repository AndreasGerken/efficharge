"""Encryption of stored vendor credentials (tokens), with AES-256-GCM.

Format of a stored value: ``v1:<key id>:<base64(nonce ‖ ciphertext+tag)>``.

* The **associated data** binds a ciphertext to its row (``device_account:<id>:<kind>``).
  A value copied into another row cannot be decrypted there.
* **Key rotation:** ``EFFICHARGE_DATA_KEYS="k2:<new>,k1:<old>"``. New values use the
  first key; old values still decrypt with theirs. ``needs_rotation()`` tells the
  re-encrypt job which rows to rewrite.
* The keys live only in the server's environment file, never in the DB or backups.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"


class DecryptionError(Exception):
    pass


def generate_key() -> str:
    return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()


class Vault:
    def __init__(self, keys: dict[str, bytes], current: str) -> None:
        if current not in keys:
            raise ValueError("current key missing")
        for kid, key in keys.items():
            if len(key) != 32:
                raise ValueError(f"key {kid!r} must be 32 bytes")
            if ":" in kid:
                raise ValueError("key ids must not contain ':'")
        self._keys = keys
        self.current = current

    @classmethod
    def from_config(cls, data_keys: str) -> Vault:
        """Parse ``"kid:base64key,kid:base64key"``. The first entry is the current key."""
        keys: dict[str, bytes] = {}
        order: list[str] = []
        for part in filter(None, (p.strip() for p in data_keys.split(","))):
            kid, _, b64 = part.partition(":")
            if not b64:
                raise ValueError("EFFICHARGE_DATA_KEYS entries must look like 'kid:base64key'")
            keys[kid] = base64.urlsafe_b64decode(b64)
            order.append(kid)
        if not order:
            raise ValueError("EFFICHARGE_DATA_KEYS is empty")
        return cls(keys, order[0])

    def encrypt(self, data: dict[str, Any], associated: str) -> str:
        nonce = os.urandom(12)
        plaintext = json.dumps(data, separators=(",", ":")).encode()
        ct = AESGCM(self._keys[self.current]).encrypt(nonce, plaintext, associated.encode())
        return f"{VERSION}:{self.current}:{base64.urlsafe_b64encode(nonce + ct).decode()}"

    def decrypt(self, token: str, associated: str) -> dict[str, Any]:
        try:
            version, kid, b64 = token.split(":", 2)
            if version != VERSION:
                raise DecryptionError(f"unknown format {version!r}")
            raw = base64.urlsafe_b64decode(b64)
            key = self._keys[kid]
            plaintext = AESGCM(key).decrypt(raw[:12], raw[12:], associated.encode())
        except DecryptionError:
            raise
        except Exception as e:  # wrong key, tampering, wrong row, bad base64
            raise DecryptionError("cannot decrypt stored credentials") from e
        return json.loads(plaintext)

    def needs_rotation(self, token: str) -> bool:
        return token.split(":", 2)[1] != self.current


def account_aad(account_id: int, kind: str) -> str:
    return f"device_account:{account_id}:{kind}"
