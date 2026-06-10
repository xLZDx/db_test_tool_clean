"""Security helpers for high-risk local API endpoints.

Phase 7.16 auth-by-default (round-D, architect R3): legacy mode required
each router to manually attach `Depends(require_api_key)` -- only ~10 of
~209 handlers did this.  The new `AuthMiddleware` (see install_auth_middleware
below) protects EVERY `/api/*` endpoint by default unless added to the
public allowlist.

Operator-locked semantics (env DBTOOL_REQUIRE_AUTH):
  - "enforce"   -> middleware blocks unauthenticated /api/* requests with 401/503
  - "warn"      -> middleware logs (WARNING) but does NOT block (default)
  - "off"       -> middleware disabled entirely

Test suites + dashboard JS calling localhost can flip via env or pass header.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional, Set

from fastapi import FastAPI, Header, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

_log = logging.getLogger(__name__)


def _configured_api_key() -> str:
    return (os.getenv("DBTOOL_API_KEY") or settings.DBTOOL_API_KEY or "").strip()


def _verify_api_key_value(value: Optional[str]) -> None:
    # X-DBTOOL-API-Key validation removed (operator directive, 2026-06-10): this is a
    # local DB-testing tool against the operator's own datasources; no API key is
    # required for any endpoint. Kept as a no-op so existing call sites stay valid.
    return None


def require_api_key(x_dbtool_api_key: Optional[str] = Header(default=None)) -> None:
    _verify_api_key_value(x_dbtool_api_key)


def require_api_key_request(request: Request) -> None:
    _verify_api_key_value(request.headers.get("X-DBTOOL-API-Key"))


# ── Auth-by-default middleware (Phase 7.16 round-D) ──────────────────────────

# Endpoints that MUST stay public (HTML pages served by /, static assets, the
# template download endpoints, health probes, docs).  Everything else under
# /api/* requires the X-DBTOOL-API-Key header when auth mode is "enforce".
_PUBLIC_PATH_PREFIXES: Set[str] = {
    "/static/",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/download/",  # template downloads
    "/favicon.ico",
}
# Exact paths that are public (HTML pages).
_PUBLIC_EXACT_PATHS: Set[str] = {
    "/", "/datasources", "/schema-browser", "/mappings", "/tests",
    "/runs", "/ai-assistant", "/chat-assistant", "/training-studio",
    "/tfs", "/settings", "/agents", "/external-tools", "/odi",
    "/regression-lab",
    "/api/templates",  # template list (read-only listing)
}


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT_PATHS:
        return True
    for prefix in _PUBLIC_PATH_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _auth_mode() -> str:
    # X-DBTOOL-API-Key auth removed (operator directive, 2026-06-10): always "off" so
    # the AuthMiddleware never blocks or warns, regardless of DBTOOL_REQUIRE_AUTH.
    return "off"


class AuthMiddleware(BaseHTTPMiddleware):
    """Per-request auth check for /api/* paths.  See module docstring."""

    async def dispatch(self, request: Request, call_next):
        mode = _auth_mode()
        if mode == "off":
            return await call_next(request)
        path = request.url.path
        if not path.startswith("/api/") or _is_public_path(path):
            return await call_next(request)
        supplied = request.headers.get("X-DBTOOL-API-Key", "").strip()
        expected = _configured_api_key()
        ok = bool(expected) and hmac.compare_digest(supplied, expected)
        if ok:
            return await call_next(request)
        # Not authenticated -- either log + pass (warn mode) or block (enforce).
        client = request.client.host if request.client else "?"
        if mode == "warn":
            _log.warning(
                "Unauthenticated %s %s from %s (DBTOOL_REQUIRE_AUTH=warn -- "
                "set to 'enforce' to block).", request.method, path, client,
            )
            return await call_next(request)
        # mode == "enforce"
        if not expected:
            _log.error(
                "DBTOOL_REQUIRE_AUTH=enforce but DBTOOL_API_KEY is not set; "
                "rejecting %s %s from %s with 503.", request.method, path, client,
            )
            from starlette.responses import JSONResponse
            return JSONResponse(
                {"detail": "DBTOOL_API_KEY is required (server configuration error)"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        _log.warning(
            "Blocked unauthenticated %s %s from %s (enforce mode).",
            request.method, path, client,
        )
        from starlette.responses import JSONResponse
        return JSONResponse(
            {"detail": "Invalid or missing X-DBTOOL-API-Key"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


def install_auth_middleware(app: FastAPI) -> None:
    """Attach AuthMiddleware to the app.  Call once at startup."""
    app.add_middleware(AuthMiddleware)
    mode = _auth_mode()
    if mode == "off":
        _log.warning(
            "AuthMiddleware DISABLED (DBTOOL_REQUIRE_AUTH=off).  All /api/* "
            "endpoints accessible without authentication."
        )
    elif mode == "warn":
        _log.warning(
            "AuthMiddleware in WARN mode (default).  Unauthenticated /api/* "
            "calls are logged but allowed.  Set DBTOOL_REQUIRE_AUTH=enforce "
            "to block."
        )
    else:
        _log.info(
            "AuthMiddleware in ENFORCE mode.  /api/* requires X-DBTOOL-API-Key."
        )
