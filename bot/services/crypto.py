from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from bot.config import settings


def _fernet() -> Fernet:
    # MASTER_ENCRYPTION_KEY can be any secret string; derive a valid 32-byte
    # urlsafe-base64 Fernet key from it so users don't have to generate one themselves.
    digest = hashlib.sha256(settings.master_encryption_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
