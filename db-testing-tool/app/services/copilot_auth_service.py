"""Copilot authentication service.

Supports three modes:
1) Static API key from environment (auto-connected)
2) Runtime token persisted to local data storage
3) GitHub OAuth device flow when GITHUB_OAUTH_CLIENT_ID is configured
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.config import BASE_DIR, settings

logger = logging.getLogger(__name__)

_SESSION_FILE = BASE_DIR / "data" / "copilot_session.json"


def _load_session() -> dict:
    try:
        if not _SESSION_FILE.exists():
            return {}
        return json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_session(payload: dict) -> None:
    try:
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    except Exception:
        logger.exception("Failed to save Copilot session state")


def _clear_session() -> None:
    try:
        if _SESSION_FILE.exists():
            _SESSION_FILE.unlink()
    except Exception:
        logger.exception("Failed to clear Copilot session state")


def get_runtime_copilot_token() -> Optional[str]:
    """Return active runtime token or static env API key."""
    static = (settings.GITHUBCOPILOT_API_KEY or settings.AI_API_KEY or "").strip()
    if static:
        return static
    sess = _load_session()
    token = (sess.get("access_token") or "").strip()
    return token or None


async def start_device_flow() -> Dict[str, Any]:
    """Start GitHub OAuth device-code flow.

    If API key is configured, returns connected immediately.
    """
    existing = get_runtime_copilot_token()
    if existing:
        return {
            "status": "already_connected",
            "connected": True,
            "authenticated": True,
            "message": "Copilot already connected via configured token.",
        }

    client_id = (settings.GITHUB_OAUTH_CLIENT_ID or "").strip()
    if not client_id:
        return {
            "status": "not_configured",
            "connected": False,
            "authenticated": False,
            "message": "GITHUB_OAUTH_CLIENT_ID is not configured. Set GITHUBCOPILOT_API_KEY for auto-connect or configure GitHub OAuth device flow.",
        }

    scope = (settings.GITHUB_OAUTH_SCOPE or "read:user copilot").strip()
    url = "https://github.com/login/device/code"
    headers = {"Accept": "application/json"}
    data = {"client_id": client_id, "scope": scope}
    try:
        async with httpx.AsyncClient(timeout=20, verify=settings.GITHUB_VERIFY_SSL) as client:
            resp = await client.post(url, data=data, headers=headers)
            payload = resp.json()
        if resp.status_code >= 300:
            return {
                "status": "error",
                "connected": False,
                "authenticated": False,
                "message": payload.get("error_description") or payload.get("error") or f"GitHub device flow start failed ({resp.status_code}).",
            }
        _save_session({
            "device_code": payload.get("device_code"),
            "user_code": payload.get("user_code"),
            "verification_uri": payload.get("verification_uri_complete") or payload.get("verification_uri"),
            "interval": payload.get("interval") or 5,
            "expires_at": int(time.time()) + int(payload.get("expires_in") or 900),
        })
        return {
            "status": "pending",
            "connected": False,
            "authenticated": False,
            "device_code": payload.get("device_code"),
            "user_code": payload.get("user_code"),
            "verification_uri": payload.get("verification_uri_complete") or payload.get("verification_uri"),
            "interval": payload.get("interval") or 5,
            "expires_in": payload.get("expires_in") or 900,
        }
    except Exception as exc:
        logger.exception("Failed to start GitHub device flow")
        return {
            "status": "error",
            "connected": False,
            "authenticated": False,
            "message": f"Failed to start device flow: {exc}",
        }


async def complete_device_flow(device_code: str) -> Dict[str, Any]:
    """Alias to poll_device_flow for backward compatibility."""
    return await poll_device_flow(device_code)


async def get_copilot_token(force_refresh: bool = False) -> Optional[str]:
    """Return current Copilot token.

    force_refresh currently re-checks stored session only.
    """
    if force_refresh:
        _ = _load_session()
    return get_runtime_copilot_token()


async def poll_device_flow(device_code: str) -> Dict[str, Any]:
    """Poll GitHub OAuth token endpoint using device code."""
    client_id = (settings.GITHUB_OAUTH_CLIENT_ID or "").strip()
    if not client_id:
        return {
            "status": "not_configured",
            "connected": False,
            "authenticated": False,
            "message": "GITHUB_OAUTH_CLIENT_ID is not configured.",
        }
    session = _load_session()
    if not (device_code or "").strip():
        device_code = (session.get("device_code") or "").strip()
    if not (device_code or "").strip():
        return {
            "status": "error",
            "connected": False,
            "authenticated": False,
            "message": "device_code is required",
        }

    url = "https://github.com/login/oauth/access_token"
    headers = {"Accept": "application/json"}
    data = {
        "client_id": client_id,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    try:
        async with httpx.AsyncClient(timeout=20, verify=settings.GITHUB_VERIFY_SSL) as client:
            resp = await client.post(url, data=data, headers=headers)
            payload = resp.json()

        if payload.get("error") in {"authorization_pending", "slow_down"}:
            return {
                "status": payload.get("error"),
                "connected": False,
                "authenticated": False,
                "message": payload.get("error_description") or payload.get("error"),
            }

        token = (payload.get("access_token") or "").strip()
        if not token:
            return {
                "status": "error",
                "connected": False,
                "authenticated": False,
                "message": payload.get("error_description") or payload.get("error") or "No access token returned.",
            }

        session = _load_session()
        session["access_token"] = token
        session["token_type"] = payload.get("token_type") or "bearer"
        session["scope"] = payload.get("scope") or ""
        session["connected_at"] = int(time.time())
        session.pop("device_code", None)
        _save_session(session)

        return {
            "status": "connected",
            "connected": True,
            "authenticated": True,
            "message": "Copilot connected.",
        }
    except Exception as exc:
        logger.exception("Failed to poll GitHub device flow")
        return {
            "status": "error",
            "connected": False,
            "authenticated": False,
            "message": f"Failed to poll device flow: {exc}",
        }


def get_copilot_status() -> Dict[str, Any]:
    """Return current Copilot auth status with frontend-compatible fields."""
    token = get_runtime_copilot_token()
    connected = bool(token)
    status = "connected" if connected else "disconnected"
    sess = _load_session()
    model = (settings.GITHUBCOPILOT_MODEL or "gpt-5mini").strip()
    out = {
        "connected": connected,
        "authenticated": connected,
        "status": status,
        "model": model,
        "using_env_key": bool((settings.GITHUBCOPILOT_API_KEY or settings.AI_API_KEY or "").strip()),
    }
    if not connected and sess.get("verification_uri") and sess.get("user_code"):
        out["pending_device_flow"] = {
            "verification_uri": sess.get("verification_uri"),
            "user_code": sess.get("user_code"),
            "device_code": sess.get("device_code"),
            "interval": sess.get("interval") or 5,
            "expires_at": sess.get("expires_at"),
        }
    return out


def logout_copilot() -> Dict[str, Any]:
    """Clear runtime Copilot session.

    Environment API keys are not removed.
    """
    _clear_session()
    if (settings.GITHUBCOPILOT_API_KEY or settings.AI_API_KEY or "").strip():
        return {
            "status": "ok",
            "connected": True,
            "authenticated": True,
            "message": "Runtime session cleared. Environment token is still active.",
        }
    return {"status": "ok", "connected": False, "authenticated": False, "message": "Copilot disconnected."}
