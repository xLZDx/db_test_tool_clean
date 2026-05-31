"""Small credential encryption helpers for datasource secrets."""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet

from app.config import settings

ENCRYPTED_PREFIX = "enc:v1:"
SENSITIVE_EXTRA_PARAM_KEYS = {
    "password",
    "pwd",
    "pass",
    "secret",
    "token",
    "api_key",
    "apikey",
    "key",
    "wallet_password",
}


class SecretStoreConfigError(RuntimeError):
    """Raised when a new secret must be encrypted but no key is configured."""


def is_sensitive_extra_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(marker in lowered for marker in SENSITIVE_EXTRA_PARAM_KEYS)


def _fernet() -> Fernet:
    secret = (settings.DBTOOL_SECRET_KEY or "").strip()
    if not secret:
        raise SecretStoreConfigError("DBTOOL_SECRET_KEY is required to store datasource secrets")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    if str(value).startswith(ENCRYPTED_PREFIX):
        return str(value)
    token = _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")
    return ENCRYPTED_PREFIX + token


def decrypt_secret_if_needed(value: str | None) -> str | None:
    if value is None or value == "":
        return value
    text = str(value)
    if not text.startswith(ENCRYPTED_PREFIX):
        return text
    token = text[len(ENCRYPTED_PREFIX):].encode("ascii")
    return _fernet().decrypt(token).decode("utf-8")


def encrypt_sensitive_extra_params(raw: str | None) -> str | None:
    if not raw:
        return raw
    try:
        parsed = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(parsed, dict):
        return raw

    changed = False
    encrypted: dict[str, Any] = {}
    for key, value in parsed.items():
        if is_sensitive_extra_key(key) and value not in (None, ""):
            encrypted[key] = encrypt_secret(str(value))
            changed = True
        else:
            encrypted[key] = value
    if not changed:
        return raw
    return json.dumps(encrypted, sort_keys=True)


def decrypt_sensitive_extra_params_dict(parsed: dict[str, Any]) -> dict[str, Any]:
    decrypted: dict[str, Any] = {}
    for key, value in (parsed or {}).items():
        if is_sensitive_extra_key(key) and isinstance(value, str):
            decrypted[key] = decrypt_secret_if_needed(value)
        else:
            decrypted[key] = value
    return decrypted
