"""Security helpers for high-risk local API endpoints."""
from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException, Request, status

from app.config import settings


def _configured_api_key() -> str:
    return (os.getenv("DBTOOL_API_KEY") or settings.DBTOOL_API_KEY or "").strip()


def _verify_api_key_value(value: Optional[str]) -> None:
    expected = _configured_api_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DBTOOL_API_KEY is required for this high-risk endpoint",
        )
    supplied = (value or "").strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-DBTOOL-API-Key",
        )


def require_api_key(x_dbtool_api_key: Optional[str] = Header(default=None)) -> None:
    _verify_api_key_value(x_dbtool_api_key)


def require_api_key_request(request: Request) -> None:
    _verify_api_key_value(request.headers.get("X-DBTOOL-API-Key"))
