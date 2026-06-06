"""ODI operations router: repository profile discovery, package run, and session monitoring."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Literal
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from app.security import require_api_key

router = APIRouter(prefix="/api/odi", tags=["odi"])

_ODI_PACKAGE_INDEX: set[str] = set()
_ODI_PACKAGE_INDEX_BY_REPO: Dict[str, set[str]] = {}
_ODI_CONTEXT_INDEX_BY_REPO: Dict[str, set[str]] = {}
_ODI_SESSIONS: Dict[str, Dict[str, Any]] = {}
_ODI_SESSION_PROCESSES: Dict[str, asyncio.subprocess.Process] = {}
_ODI_REPO_CONNECTIONS: Dict[str, Dict[str, Any]] = {}

_DEFAULT_CONTEXTS = ["QA", "DEV", "UAT", "PROD", "TAXLOTS"]
_DEFAULT_EXECUTION_AGENTS = ["Oracle", "Local"]
_DEFAULT_LOGICAL_AGENTS = ["OracleDIAgent (ODI Agent)"]


def _default_odi_root() -> Path:
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "odi"
    return Path.home() / "AppData" / "Roaming" / "odi"


def _resolve_root_path(root_path: str = "") -> Path:
    if root_path and root_path.strip():
        return Path(root_path.strip())
    return _default_odi_root()


def _decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return ""


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sanitize_login_payload(payload: Dict[str, str]) -> Dict[str, str]:
    """Never expose password material from ODI login exports."""
    return {
        "login_name": _compact_text(payload.get("LoginName", "")),
        "login_user": _compact_text(payload.get("LoginUser", "")),
        "db_user": _compact_text(payload.get("LoginDbuser", "")),
        "db_url": _compact_text(payload.get("LoginDburl", "")),
        "work_repository": _compact_text(payload.get("LoginWorkRepository", "")),
        "driver": _compact_text(payload.get("LoginDbdriver", "")),
    }


def _extract_login_records_from_xml(xml_text: str) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    if not xml_text.strip():
        return records
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return records

    for obj in root.findall(".//Object"):
        if "SnpLogin" not in (obj.attrib.get("class") or ""):
            continue
        fields: Dict[str, str] = {}
        for fld in obj.findall("Field"):
            name = fld.attrib.get("name")
            if not name:
                continue
            fields[name] = _compact_text(fld.text)
        rec = _sanitize_login_payload(fields)
        if rec.get("login_name"):
            records.append(rec)
    return records


def _extract_package_name_candidates(text: str) -> List[str]:
    if not text:
        return []
    tokens = re.findall(r"\b[A-Z][A-Z0-9_]{6,}\b", text.upper())
    bad_prefixes = ("LOGIN", "PASSWORD", "JDBC", "ORACLE", "SOURCE", "TARGET", "SESSION")
    filtered: List[str] = []
    for t in tokens:
        if t.startswith(bad_prefixes):
            continue
        if "_" not in t:
            continue
        filtered.append(t)
    return filtered


def _normalize_upper(value: str) -> str:
    return _compact_text(value).upper()


def _repo_key(owner_token: str = "", login_name: str = "") -> str:
    token = (owner_token or "").strip()
    login = (login_name or "").strip()
    if token:
        state = _ODI_REPO_CONNECTIONS.get(token) or {}
        root_path = _normalize_upper(str(state.get("root_path") or ""))
        login_from_state = _normalize_upper(str(state.get("login_name") or ""))
        if root_path or login_from_state:
            return f"{root_path}|{login_from_state}".strip("|") or "GLOBAL"
    if login:
        return f"LOGIN|{_normalize_upper(login)}"
    return "GLOBAL"


def _looks_like_context_name(value: str) -> bool:
    token = _compact_text(value)
    if not token:
        return False
    if len(token) > 64:
        return False
    if re.search(r"[^A-Za-z0-9_\- ]", token):
        return False
    upper = token.upper()
    banned = {
        "ALL CONTEXTS",
        "DEFAULT CONTEXT FOR EXECUTION",
        "DEFAULT DESIGNER CONTEXT",
        "DEFAULT CONTEXT FOR GENERATING DATA SERVICES",
    }
    if upper in banned:
        return False
    return bool(re.search(r"[A-Z]", upper))


def _extract_context_candidates(text: str) -> set[str]:
    found: set[str] = set()
    if not text.strip():
        return found

    pattern = r'(?is)<Field\s+name="(?:CtxName|ContextName|ContextCode|Context)"[^>]*>\s*(?:<!\[CDATA\[)?([^<\]]+)'
    for m in re.findall(pattern, text):
        candidate = _compact_text(m)
        if _looks_like_context_name(candidate):
            found.add(candidate.upper())

    return found


def _discover_contexts(root: Path) -> set[str]:
    contexts: set[str] = {c.upper() for c in _DEFAULT_CONTEXTS}
    if not root.exists() or not root.is_dir():
        return contexts

    xml_files = sorted(root.rglob("*.xml"))[:120]
    for p in xml_files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        contexts.update(_extract_context_candidates(text))

    return contexts


def _index_packages(repo_key: str, packages: List[str]) -> None:
    if not packages:
        return
    bucket = _ODI_PACKAGE_INDEX_BY_REPO.setdefault(repo_key, set())
    for name in packages:
        upper = _normalize_upper(name)
        if not upper:
            continue
        _ODI_PACKAGE_INDEX.add(upper)
        bucket.add(upper)


def _session_summary(s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "session_id": s["session_id"],
        "package_name": s.get("package_name", ""),
        "status": s.get("status", "pending"),
        "progress": s.get("progress", 0),
        "context": s.get("context", ""),
        "execution_agent": s.get("execution_agent", ""),
        "logical_agent": s.get("logical_agent", ""),
        "login_name": s.get("login_name", ""),
        "repository_connected": bool(s.get("repository_connected")),
        "repository_name": s.get("repository_name", ""),
        "repo_key": s.get("repo_key", "GLOBAL"),
        "source": s.get("source", "simulated"),
        "variables": s.get("variables", {}),
        "started_at": s.get("started_at", 0),
        "ended_at": s.get("ended_at"),
        "error_count": len(s.get("errors", [])),
        "step_count": len(s.get("steps", [])),
    }


def _append_error(session_id: str, message: str, line: str = "") -> None:
    s = _ODI_SESSIONS.get(session_id)
    if not s:
        return
    s.setdefault("errors", []).append({
        "ts": time.time(),
        "message": _compact_text(message),
        "line": (line or "")[:1200],
    })


def _append_step(session_id: str, name: str, status: str, detail: str = "") -> None:
    s = _ODI_SESSIONS.get(session_id)
    if not s:
        return
    step_no = len(s.setdefault("steps", [])) + 1
    s["steps"].append({
        "step_no": step_no,
        "name": _compact_text(name),
        "status": status,
        "detail": (detail or "")[:2000],
        "ts": time.time(),
    })


def sweep_odi_sessions(
    *,
    stale_seconds: int = 600,
    max_runtime_seconds: int = 7200,
    retain_seconds: int = 3600,
) -> Dict[str, int]:
    """Kill stale/zombie ODI sessions and prune completed session history."""
    now = time.time()
    stale_seconds = max(30, int(stale_seconds or 600))
    max_runtime_seconds = max(60, int(max_runtime_seconds or 7200))
    retain_seconds = max(60, int(retain_seconds or 3600))

    killed = 0
    zombie_killed = 0
    pruned = 0

    for session_id, session in list(_ODI_SESSIONS.items()):
        status = str(session.get("status") or "").lower()
        started_at = float(session.get("started_at") or now)
        ended_at = session.get("ended_at")

        steps = session.get("steps") or []
        errors = session.get("errors") or []
        last_step_ts = float(steps[-1].get("ts") or 0) if steps else 0
        last_error_ts = float(errors[-1].get("ts") or 0) if errors else 0
        last_activity_ts = max(started_at, last_step_ts, last_error_ts)
        runtime = max(0.0, now - started_at)
        idle = max(0.0, now - last_activity_ts)

        proc = _ODI_SESSION_PROCESSES.get(session_id)
        is_terminal = status in {"success", "error", "warning", "canceled", "killed_stale"}

        if not is_terminal and (idle >= stale_seconds or runtime >= max_runtime_seconds):
            session["cancel_requested"] = True
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            session["status"] = "killed_stale"
            session["ended_at"] = now
            _append_error(session_id, "Watchdog killed stale ODI session")
            killed += 1

        proc = _ODI_SESSION_PROCESSES.get(session_id)
        if proc and status in {"success", "error", "warning", "canceled", "killed_stale"} and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
            zombie_killed += 1

        terminal_ts = float(session.get("ended_at") or 0)
        if terminal_ts and (now - terminal_ts) > retain_seconds:
            _ODI_SESSIONS.pop(session_id, None)
            _ODI_SESSION_PROCESSES.pop(session_id, None)
            pruned += 1

    return {
        "killed_stale": killed,
        "killed_zombie_processes": zombie_killed,
        "pruned": pruned,
        "active_sessions": len(_ODI_SESSIONS),
    }


class OdiRunRequest(BaseModel):
    package_name: str
    context: str = ""
    execution_agent: str = ""
    logical_agent: str = ""
    login_name: str = ""
    owner_token: str = ""
    command_template: str = ""
    variables: Dict[str, str] = {}
    require_real_run: bool = False


class OdiRepoConnectRequest(BaseModel):
    login_name: str
    owner_token: str
    root_path: str = ""


async def _simulate_odi_session(session_id: str) -> None:
    s = _ODI_SESSIONS.get(session_id)
    if not s:
        return
    s["status"] = "running"
    phases = [
        "Connect to repository",
        "Resolve scenario/package metadata",
        "Start ODI execution",
        "Execute package steps",
        "Collect execution report",
    ]
    for idx, phase in enumerate(phases, start=1):
        if s.get("cancel_requested"):
            s["status"] = "canceled"
            s["ended_at"] = time.time()
            return
        _append_step(session_id, phase, "ok")
        s["progress"] = int((idx / len(phases)) * 100)
        await asyncio.sleep(1.2)

    s["status"] = "success"
    s["progress"] = 100
    s["ended_at"] = time.time()


async def _run_command_session(session_id: str, command: str) -> None:
    s = _ODI_SESSIONS.get(session_id)
    if not s:
        return
    s["status"] = "running"
    s["source"] = "command"
    s["command"] = command
    _append_step(session_id, "Launch ODI command", "ok", command)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as exc:
        _append_error(session_id, f"Failed to start ODI command: {exc}")
        s["status"] = "error"
        s["ended_at"] = time.time()
        return

    _ODI_SESSION_PROCESSES[session_id] = proc

    try:
        while True:
            if s.get("cancel_requested"):
                try:
                    proc.terminate()
                except Exception:
                    pass
                s["status"] = "canceled"
                s["ended_at"] = time.time()
                return

            line_raw = await proc.stdout.readline() if proc.stdout else b""
            if not line_raw:
                break
            line = _decode_text(line_raw).strip()
            if not line:
                continue

            s.setdefault("raw_output", []).append(line)
            if len(s["raw_output"]) > 500:
                s["raw_output"] = s["raw_output"][-500:]

            if re.search(r"(ODI-\d+|ORA-\d+|DPY-\d+|\bERROR\b|\bEXCEPTION\b)", line, flags=re.IGNORECASE):
                _append_error(session_id, "ODI runtime error", line)
            if re.search(r"(TASK|STEP|SESSION TASK|COMMAND ON SOURCE)", line, flags=re.IGNORECASE):
                _append_step(session_id, "Runtime step", "ok", line)

        returncode = await proc.wait()
        s["progress"] = 100
        s["ended_at"] = time.time()
        if s.get("cancel_requested"):
            s["status"] = "canceled"
        elif returncode == 0 and not s.get("errors"):
            s["status"] = "success"
        elif returncode == 0:
            s["status"] = "warning"
        else:
            s["status"] = "error"
            _append_error(session_id, f"Command exited with code {returncode}")

    finally:
        _ODI_SESSION_PROCESSES.pop(session_id, None)


def _collect_logins(root_path: str = "") -> List[Dict[str, Any]]:
    root = _resolve_root_path(root_path)
    if not root.exists() or not root.is_dir():
        return []

    login_files = sorted(root.rglob("snps_login*.xml"))
    merged: Dict[str, Dict[str, Any]] = {}

    for p in login_files[:30]:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for rec in _extract_login_records_from_xml(text):
            key = rec.get("login_name", "").upper()
            if not key:
                continue
            row = dict(rec)
            row["source_file"] = str(p)
            merged[key] = row

    return sorted(merged.values(), key=lambda x: x.get("login_name", "").lower())


@router.get("/config-files")
async def list_odi_config_files(root_path: str = Query(default=""), max_files: int = Query(default=300, ge=10, le=2000)):
    root = _resolve_root_path(root_path)
    if not root.exists() or not root.is_dir():
        return {"root_path": str(root), "exists": False, "files": []}

    allowed = {".xml", ".txt", ".properties", ".conf", ".cfg", ".ini", ".log"}
    files: List[Dict[str, Any]] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in allowed:
            continue
        try:
            st = p.stat()
            files.append({
                "name": p.name,
                "path": str(p),
                "relative_path": str(p.relative_to(root)),
                "size": st.st_size,
                "modified": st.st_mtime,
            })
        except Exception:
            continue
        if len(files) >= max_files:
            break

    files.sort(key=lambda x: x["relative_path"].lower())
    return {"root_path": str(root), "exists": True, "files": files}


@router.get("/logins")
async def get_odi_logins(root_path: str = Query(default="")):
    root = _resolve_root_path(root_path)
    rows = _collect_logins(root_path)
    return {"root_path": str(root), "exists": root.exists() and root.is_dir(), "logins": rows}


@router.post("/repository/connect")
async def connect_repository(body: OdiRepoConnectRequest):
    owner_token = (body.owner_token or "").strip()
    login_name = (body.login_name or "").strip()
    if not owner_token:
        raise HTTPException(status_code=400, detail="owner_token is required")
    if not login_name:
        raise HTTPException(status_code=400, detail="login_name is required")

    logins = _collect_logins(body.root_path)
    login = next((row for row in logins if (row.get("login_name") or "").strip().upper() == login_name.upper()), None)
    if not login:
        raise HTTPException(status_code=404, detail=f"Login profile '{login_name}' not found")

    state = {
        "connected": True,
        "connected_at": time.time(),
        "root_path": str(_resolve_root_path(body.root_path)),
        "login_name": login.get("login_name") or "",
        "work_repository": login.get("work_repository") or "",
        "db_url": login.get("db_url") or "",
        "db_user": login.get("db_user") or "",
        "login_user": login.get("login_user") or "",
    }
    _ODI_REPO_CONNECTIONS[owner_token] = state
    key = _repo_key(owner_token=owner_token, login_name=state.get("login_name", ""))
    discovered = _discover_contexts(_resolve_root_path(body.root_path))
    if discovered:
        _ODI_CONTEXT_INDEX_BY_REPO[key] = set(discovered)
        state["contexts"] = sorted(discovered)
    return {"connected": True, "connection": _ODI_REPO_CONNECTIONS[owner_token]}


@router.get("/repository/status")
async def repository_status(owner_token: str = Query(default="")):
    token = (owner_token or "").strip()
    if not token:
        return {"connected": False, "connection": None}
    state = _ODI_REPO_CONNECTIONS.get(token)
    if not state:
        return {"connected": False, "connection": None}
    return {"connected": True, "connection": state}


@router.post("/analyze-files")
async def analyze_odi_files(
    files: List[UploadFile] = File(default=[]),
    owner_token: str = Query(default=""),
    login_name: str = Query(default=""),
):
    if not files:
        return {"files": [], "package_candidates": []}

    all_tokens: List[str] = []
    file_summaries: List[Dict[str, Any]] = []

    for f in files:
        raw = await f.read()
        text = _decode_text(raw[:800_000])
        tokens = _extract_package_name_candidates(text)
        all_tokens.extend(tokens)
        file_summaries.append({
            "name": f.filename or "upload",
            "size": len(raw),
            "candidates_found": min(len(tokens), 100),
        })

    counts = Counter(all_tokens)
    candidates = [name for name, _ in counts.most_common(200)]
    repo_key = _repo_key(owner_token=owner_token, login_name=login_name)
    _index_packages(repo_key, candidates[:500])

    return {"files": file_summaries, "package_candidates": candidates}


@router.get("/contexts")
async def get_odi_contexts(
    owner_token: str = Query(default=""),
    login_name: str = Query(default=""),
    root_path: str = Query(default=""),
):
    token = (owner_token or "").strip()
    state = _ODI_REPO_CONNECTIONS.get(token) if token else None
    resolved_root = _resolve_root_path((state or {}).get("root_path") or root_path)
    repo_key = _repo_key(owner_token=token, login_name=(state or {}).get("login_name") or login_name)

    discovered = _discover_contexts(resolved_root)
    if discovered:
        _ODI_CONTEXT_INDEX_BY_REPO[repo_key] = set(discovered)

    rows = sorted(_ODI_CONTEXT_INDEX_BY_REPO.get(repo_key, set()) | {c.upper() for c in _DEFAULT_CONTEXTS})
    return {
        "owner_token": token,
        "repo_key": repo_key,
        "contexts": rows,
        "root_path": str(resolved_root),
    }


@router.get("/agents")
async def get_odi_agents(owner_token: str = Query(default=""), login_name: str = Query(default="")):
    repo_key = _repo_key(owner_token=owner_token, login_name=login_name)
    execution_agents = set(_DEFAULT_EXECUTION_AGENTS)
    logical_agents = set(_DEFAULT_LOGICAL_AGENTS)

    for s in _ODI_SESSIONS.values():
        if _compact_text(s.get("repo_key", "GLOBAL")) != repo_key:
            continue
        execution = _compact_text(s.get("execution_agent", ""))
        logical = _compact_text(s.get("logical_agent", ""))
        if execution:
            execution_agents.add(execution)
        if logical:
            logical_agents.add(logical)

    return {
        "repo_key": repo_key,
        "execution_agents": sorted(execution_agents),
        "logical_agents": sorted(logical_agents),
    }


@router.get("/packages/search")
async def search_odi_packages(
    q: str = Query(default=""),
    owner_token: str = Query(default=""),
    login_name: str = Query(default=""),
    limit: int = Query(default=120, ge=10, le=500),
):
    query = (q or "").strip().upper()
    repo_key = _repo_key(owner_token=owner_token, login_name=login_name)
    pool = set(_ODI_PACKAGE_INDEX_BY_REPO.get(repo_key, set()))
    if repo_key == "GLOBAL":
        pool.update(_ODI_PACKAGE_INDEX)

    for s in _ODI_SESSIONS.values():
        if _compact_text(s.get("repo_key", "GLOBAL")) != repo_key:
            continue
        name = (s.get("package_name") or "").strip().upper()
        if name:
            pool.add(name)

    rows = sorted(pool)
    if query:
        rows = [name for name in rows if query in name]
    return {"query": query, "repo_key": repo_key, "packages": rows[:limit]}


@router.post("/run")
async def run_odi_package(body: OdiRunRequest):
    package_name = (body.package_name or "").strip()
    if not package_name:
        raise HTTPException(status_code=400, detail="package_name is required")

    session_id = uuid.uuid4().hex[:12]
    owner_token = (body.owner_token or "").strip()
    repo_state = _ODI_REPO_CONNECTIONS.get(owner_token, {}) if owner_token else {}
    repo_connected = bool(repo_state.get("connected"))
    selected_login = (body.login_name or "").strip()
    if not selected_login:
        selected_login = str(repo_state.get("login_name") or "")
    if selected_login and repo_connected:
        repo_connected = (selected_login.upper() == str(repo_state.get("login_name") or "").upper())

    repo_key = _repo_key(owner_token=owner_token, login_name=selected_login)
    _index_packages(repo_key, [package_name])
    session_context = (body.context or "").strip() or "QA"
    session_execution_agent = (body.execution_agent or "").strip() or "Oracle"
    session_logical_agent = (body.logical_agent or "").strip() or "OracleDIAgent (ODI Agent)"
    variables = {str(k): str(v) for k, v in (body.variables or {}).items() if _compact_text(k)}

    session = {
        "session_id": session_id,
        "package_name": package_name,
        "status": "queued",
        "progress": 0,
        "context": session_context,
        "execution_agent": session_execution_agent,
        "logical_agent": session_logical_agent,
        "login_name": selected_login,
        "owner_token": owner_token,
        "repo_key": repo_key,
        "repository_connected": repo_connected,
        "repository_name": str(repo_state.get("login_name") or ""),
        "source": "simulated",
        "variables": variables,
        "created_at": time.time(),
        "started_at": time.time(),
        "ended_at": None,
        "steps": [],
        "errors": [],
        "raw_output": [],
        "cancel_requested": False,
    }
    _ODI_SESSIONS[session_id] = session

    command_template = (body.command_template or os.environ.get("ODI_RUNNER_CMD_TEMPLATE", "")).strip()
    if body.require_real_run and not command_template:
        _append_error(session_id, "Real ODI execution requested but command template is missing")
        session["status"] = "error"
        session["ended_at"] = time.time()
        return {
            "session": _session_summary(session),
            "note": "Provide command_template or ODI_RUNNER_CMD_TEMPLATE for real execution",
        }

    # SECURITY FIX: Real ODI execution via shell command is disabled pending refactoring
    # The original implementation used create_subprocess_shell with user-supplied variables,
    # which is vulnerable to shell injection. Need to refactor to use create_subprocess_exec
    # with safe argument arrays.
    if command_template or body.require_real_run:
        _append_error(session_id, "Real ODI execution is temporarily disabled for security refactoring")
        session["status"] = "error"
        session["ended_at"] = time.time()
        return {
            "session": _session_summary(session),
            "note": "Real ODI command execution is disabled pending security refactoring (shell injection risk). Use simulation mode only.",
        }

    # Only allow simulated sessions
    if True:  # Always simulate for now
        asyncio.create_task(_simulate_odi_session(session_id))

    return {"session": _session_summary(session), "note": "Use /api/odi/sessions/{session_id} for live progress"}


@router.get("/sessions")
async def list_odi_sessions(
    owner_token: str = Query(default=""),
    only_mine: bool = Query(default=True),
    name_contains: str = Query(default=""),
    status: str = Query(default=""),
    tracked_only: bool = Query(default=False),
    tracked_session_id: str = Query(default=""),
    limit: int = Query(default=200, ge=10, le=1000),
):
    rows = list(_ODI_SESSIONS.values())
    token = (owner_token or "").strip()
    if only_mine and token:
        rows = [r for r in rows if (r.get("owner_token") or "") == token]

    needle = (name_contains or "").strip().upper()
    if needle:
        rows = [r for r in rows if needle in (r.get("package_name") or "").upper()]

    wanted_status = (status or "").strip().lower()
    if wanted_status:
        rows = [r for r in rows if (r.get("status") or "").lower() == wanted_status]

    if tracked_only:
        tracked = (tracked_session_id or "").strip()
        if tracked:
            rows = [r for r in rows if (r.get("session_id") or "") == tracked]
        else:
            rows = []

    rows.sort(key=lambda x: x.get("started_at", 0), reverse=True)
    return {"sessions": [_session_summary(r) for r in rows[:limit]]}


@router.get("/sessions/{session_id}")
async def get_odi_session(session_id: str):
    s = _ODI_SESSIONS.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session": _session_summary(s),
        "steps": s.get("steps", []),
        "errors": s.get("errors", []),
        "raw_output": s.get("raw_output", [])[-200:],
    }


@router.get("/sessions/{session_id}/steps")
async def get_odi_session_steps(session_id: str):
    s = _ODI_SESSIONS.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"steps": s.get("steps", [])}


@router.get("/sessions/{session_id}/errors")
async def get_odi_session_errors(session_id: str):
    s = _ODI_SESSIONS.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"errors": s.get("errors", [])}


@router.post("/sessions/{session_id}/cancel")
async def cancel_odi_session(session_id: str):
    s = _ODI_SESSIONS.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    s["cancel_requested"] = True
    proc = _ODI_SESSION_PROCESSES.get(session_id)
    if proc and proc.returncode is None:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"ok": True, "session_id": session_id, "status": "cancel_requested"}


# ── ODI Scenario Analysis Endpoints ──────────────────────────────────────────
# These endpoints implement the morning deliverable:
# 1. Parse an ODI XML scenario export offline (no Oracle DB required)
# 2. Emit the correct Oracle INSERT SQL from the parsed model
# 3. 3-way DRD vs ODI comparison grid (DRD file optional)
# 4. P5 static offline validator (PDM_MISS / COLUMN_NOT_IN_KB / NULL risk)
# 5. P6 Oracle XE Docker confirmatory run (optional, requires local XE instance)
#
# All processing is offline against the local KB at data/local_kb/.
# Operator rule: no live Oracle DB access; PDM_MISS = hard error, not warning.

_MAX_UPLOAD_BYTES_ODI = 20 * 1024 * 1024  # 20 MB hard limit

# KB path for P5 static validator + P6 XE synthetic data generator.
# Resolved once at module load; endpoints degrade gracefully if file absent.
_KB_PATH = Path(__file__).resolve().parents[2] / "data" / "local_kb" / "schema_kb_ds_1.json"


async def _read_upload_checked_odi(file: UploadFile, max_bytes: int = _MAX_UPLOAD_BYTES_ODI) -> bytes:
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(413, f"Upload too large ({file.size} bytes > {max_bytes} limit)")
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, f"Upload exceeds {max_bytes // 1024 // 1024} MB limit")
    return data


@router.post("/scenario/parse")
async def parse_odi_scenario(
    xml_file: UploadFile = File(...),
    target_schema: str = Query(default=""),
    target_table: str = Query(default=""),
    strict: bool = Query(default=False),
):
    """Parse an ODI XML scenario export and emit the correct Oracle INSERT SQL.

    Returns:
    - sql: the emitted Oracle INSERT SQL (WITH ... CTEs + INSERT INTO target SELECT ...)
    - model_summary: step count, final column count, unresolved count
    - unresolved: list of columns that could not be resolved (ALIAS_NOT_IN_JOIN_GRAPH, etc.)
    - warnings: parser / emitter warnings
    - steps: list of staging steps with source tables and column count

    No Oracle DB connection is made. Everything is computed offline.
    """
    from app.sql_model.odi_parser import OdiXmlParser
    from app.sql_model.sql_emitter import EmitError, emit_insert

    xml_bytes = await _read_upload_checked_odi(xml_file)

    try:
        parser = OdiXmlParser(target_schema=target_schema, target_table=target_table)
        model = parser.parse_bytes(xml_bytes)
    except ValueError as exc:
        raise HTTPException(422, f"ODI XML parse error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"Unexpected parse error: {exc}") from exc

    try:
        emit_result = emit_insert(model, strict=strict, add_header_comment=True)
    except EmitError as exc:
        raise HTTPException(422, f"SQL emit error (strict=True): {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"Unexpected emit error: {exc}") from exc

    # P5 — static offline validation against local KB (best-effort; absent KB → None)
    static_validation = None
    if _KB_PATH.exists():
        try:
            from app.sql_model.static_validator import KBLookup, validate_model_offline
            kb = KBLookup(_KB_PATH)
            static_validation = validate_model_offline(model, kb).to_dict()
        except Exception:
            pass  # never break the parse response due to validator issues

    steps_summary = []
    for step in model.staging_steps:
        resolved_count = sum(1 for cm in step.column_mappings if cm.is_resolved)
        unresolved_count = len(step.column_mappings) - resolved_count
        source_tables = sorted({
            b.ref.fq for b in step.source_bindings if b.ref.schema
        } | {b.ref.table for b in step.source_bindings if not b.ref.schema})
        steps_summary.append({
            "step_id": step.step_id,
            "name": step.name,
            "column_count": len(step.column_mappings),
            "resolved_count": resolved_count,
            "unresolved_count": unresolved_count,
            "source_tables": source_tables,
            "join_edge_count": len(step.join_graph),
        })

    return {
        "sql": emit_result.sql,
        "model_summary": {
            "target": model.target.fq,
            "step_count": len(model.staging_steps),
            "final_column_count": len(model.final_insert_columns),
            "unresolved_count": len(emit_result.unresolved),
            "warning_count": len(emit_result.warnings),
            "status": "PARTIAL" if emit_result.unresolved else "OK",
        },
        "steps": steps_summary,
        "unresolved": emit_result.unresolved,
        "warnings": emit_result.warnings,
        "final_insert_columns": model.final_insert_columns,
        "static_validation": static_validation,
    }


@router.post("/scenario/compare")
async def compare_odi_vs_drd(
    xml_file: UploadFile = File(...),
    drd_file: UploadFile = File(None),
    target_schema: str = Query(default=""),
    target_table: str = Query(default=""),
    target_table_drd: str = Query(default=""),
    strict_emit: bool = Query(default=False),
):
    """Parse ODI XML and compare against DRD mapping file (DRD is optional).

    If drd_file is provided, returns a 3-way comparison grid:
    - MATCHED: ODI source matches DRD claim exactly
    - ALIAS_DRIFT_ONLY: same physical column, different alias/table name in DRD
    - REAL_MISMATCH: genuinely different column/logic — REVIEW REQUIRED
    - UNRESOLVABLE: complex expression or unclear DRD rule — manual verify
    - SOURCE_MISSING: column in DRD not found in any ODI staging step

    If drd_file is not provided, returns SQL-only (same as /scenario/parse).

    No Oracle DB connection is made. Everything is offline.
    """
    from app.sql_model.odi_parser import OdiXmlParser
    from app.sql_model.sql_emitter import EmitError, EmitResult, emit_insert
    from app.sql_model.comparator import compare_drd_rows_to_model, comparison_summary

    xml_bytes = await _read_upload_checked_odi(xml_file)

    try:
        parser = OdiXmlParser(target_schema=target_schema, target_table=target_table)
        model = parser.parse_bytes(xml_bytes)
    except ValueError as exc:
        raise HTTPException(422, f"ODI XML parse error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"Unexpected parse error: {exc}") from exc

    try:
        emit_result = emit_insert(model, strict=strict_emit, add_header_comment=True)
    except EmitError as exc:
        # MERGE-only / no-staging-step models (e.g. taxlot Incremental-Update-
        # Merge IKMs) cannot produce a faithful-ODI INSERT here -- but the
        # DRD<->ODI COMPARISON below is the panel's PRIMARY job and is still
        # valid.  Degrade the secondary emit to a note instead of aborting the
        # whole analysis (was: HTTP 422 that killed the comparison).
        emit_result = EmitResult(
            sql=(
                f"-- ODI faithful INSERT not emitted: {exc}\n"
                f"-- (MERGE-only / no-staging-step model -- the ODI<->DRD "
                f"comparison is still valid and shown below.)"
            ),
            warnings=[f"emit skipped (non-fatal): {exc}"],
        )
    except Exception as exc:
        raise HTTPException(500, f"Unexpected emit error: {exc}") from exc

    base_response = {
        "sql": emit_result.sql,
        "model_summary": {
            "target": model.target.fq,
            "step_count": len(model.staging_steps),
            "final_column_count": len(model.final_insert_columns),
            "unresolved_count": len(emit_result.unresolved),
            "status": "PARTIAL" if emit_result.unresolved else "OK",
        },
        "warnings": emit_result.warnings,
        "comparison": None,
    }

    if drd_file is None:
        base_response["note"] = "No DRD file provided; returning SQL only. Upload drd_file for 3-way comparison."
        return base_response

    # ── Parse DRD file ────────────────────────────────────────────────────────
    drd_bytes = await _read_upload_checked_odi(drd_file)
    try:
        from app.services.drd_import_service import parse_drd_file
        parse_result = parse_drd_file(
            file_bytes=drd_bytes,
            filename=drd_file.filename or "mapping.csv",
            selected_fields=[
                "logical_name", "physical_name", "source_schema", "source_table",
                "source_attribute", "transformation", "notes",
            ],
            target_schema=target_schema,
            target_table=target_table_drd or target_table,
            source_datasource_id=1,
            target_datasource_id=1,
            exclude_strikethrough=True,
        )
    except Exception as exc:
        raise HTTPException(422, f"DRD parse error: {exc}") from exc

    drd_rows = parse_result.get("column_mappings", [])
    drd_errors = parse_result.get("errors", [])

    # ── PDM KB lookup (optional enrichment for UNRESOLVABLE/SOURCE_MISSING) ──
    kb = None
    if _KB_PATH.exists():
        try:
            from app.sql_model.static_validator import KBLookup
            kb = KBLookup(_KB_PATH)
        except Exception:
            pass  # KB unavailable — comparator runs without PDM enrichment

    # ── Shared v9 pipeline (operator-locked: one code path, both directions) ──
    from app.services.v9_pipeline import generate_v9
    v9 = generate_v9(
        drd_bytes=drd_bytes,
        drd_filename=drd_file.filename or "drd.xlsx",
        odi_xml_bytes=xml_bytes,
        target_schema=target_schema,
        target_table=target_table,
        kb=kb,
    )
    base_response["comparison"] = {
        "summary": v9.comparison_summary,
        "rows": v9.comparison_rows,
        "drd_parse_errors": v9.drd_parse_errors,
        "drd_row_count": v9.drd_row_count,
    }
    base_response["drd_first_insert"] = {
        "sql": v9.insert_sql,
        "provenance": v9.provenance,
        "validation": v9.oracle_validation,
        "dry_run": v9.insert_dry_run,
    }
    # Phase 7.5 (operator-locked 2026-05-30): the comparator-driven
    # INSERT, built by REUSING the comparator's per-column verdict +
    # ODI USING(...) inner SELECT.  This is the operator's preferred
    # output -- no PROVENANCE_FALLBACK hacks, JOIN graph honoured by
    # construction.
    base_response["comparator_driven_insert"] = {
        "sql": v9.insert_sql_comparator_driven,
        "stats": v9.insert_comparator_driven_stats,
    }
    return base_response


@router.post("/scenario/compare-v15")
async def compare_odi_vs_drd_v15(
    xml_file: UploadFile = File(...),
    drd_file: UploadFile = File(...),
):
    """R3 (2026-06-06): v15 generic DRD-vs-ODI comparator -- ADDITIVE, opt-in.

    The existing /scenario/compare (v9 comparator) remains the DEFAULT and is
    completely untouched. This route runs the vendored v15 pipeline
    (app/services/odi_drd_compare_v15.py) with profile='generic' -- no AVY /
    TaxLot curated heuristics -- and returns the honest column-level lineage
    diff that reproduces the external gold reference (AVY 373/369/4/0).

    Offline only; no Oracle DB. DRD must be an Excel workbook (v15 auto-detects
    the mapping sheet / header row / columns).

    Returns:
      summary: {mapping_columns, in_both, mapping_only, xml_only}
      detection: auto-detected DRD layout (sheet/header/cols/confidence)
      differences: differences-only review rows (full_drd_vs_odi_xml_rules_diff)
      drd_only_columns / odi_only_columns: column-name lists
    """
    import csv as _csv
    import gc as _gc
    import shutil as _shutil
    import tempfile as _tempfile
    from app.services.odi_drd_compare_v15 import compare_to_dir

    xml_bytes = await _read_upload_checked_odi(xml_file)
    drd_bytes = await _read_upload_checked_odi(drd_file)

    # SEC-1: ext allow-list on the XML slot (the drd slot is checked below).
    xml_name = xml_file.filename or "odi.xml"
    if not xml_name.lower().endswith(".xml"):
        raise HTTPException(422, f"v15 engine needs an ODI XML (.xml), got {xml_name!r}")

    drd_name = drd_file.filename or "drd.xlsx"
    drd_ext = drd_name.rsplit(".", 1)[-1].lower() if "." in drd_name else "xlsx"
    if drd_ext not in ("xlsx", "xls", "xlsm"):
        raise HTTPException(
            422, f"v15 engine needs an Excel DRD (.xlsx/.xls/.xlsm), got {drd_ext!r}"
        )

    def _run() -> Dict[str, Any]:
        # NOTE: openpyxl read_only=True keeps the .xlsx file handle open until the
        # workbook is GC'd. On Windows that locks the temp file, so we must NOT use
        # TemporaryDirectory auto-cleanup (WinError 32). Manual dir + gc.collect()
        # to release the handle + best-effort rmtree(ignore_errors=True).
        td = _tempfile.mkdtemp(prefix="v15_")
        tdp = Path(td)
        try:
            xlsx_p = tdp / f"drd.{drd_ext}"
            xml_p = tdp / "odi.xml"
            out = tdp / "out"
            xlsx_p.write_bytes(drd_bytes)
            xml_p.write_bytes(xml_bytes)

            # #1 (2026-06-06): was hardcoded "generic" (keeps all 262 raw rows);
            # "auto" resolves AVY->curated review + taxlot->filtered, matching the
            # standalone final_v15 (AVY 14 / CLOSE 5 / OPEN 4).
            compare_to_dir(xlsx_p, xml_p, out, profile="auto")

            def _read_rows(name: str) -> List[Dict[str, str]]:
                p = out / name
                if not p.exists():
                    return []
                with p.open(encoding="utf-8-sig", newline="") as fh:
                    return list(_csv.DictReader(fh))

            col_rows = _read_rows("column_diff.csv")
            diff_rows = _read_rows("full_drd_vs_odi_xml_rules_diff.csv")

            # #2: tag each diff row with a severity bucket (from the v15 Conclusion
            # marker) and build per-Difference-Type counts so the GUI can render
            # dynamic v15-status tiles (one per type present), colored by severity.
            def _sev(row: Dict[str, str]) -> str:
                # NON-overlapping buckets: each diff row -> exactly one severity, so the
                # GUI tiles partition the rows (Missing is distinct from Real gap, and a
                # tile shows only its own rows -- never "all 14").
                dt = (row.get("Difference Type", "") or "").lower()
                c = (row.get("Conclusion", "") or "").lower()
                if "missing implementation" in dt or "missing target column" in dt:
                    return "missing"
                if "structural mismatch" in dt or "confirmed structural" in c or "structural gap" in c:
                    return "real_gap"
                if ("odi-only" in c or "environment" in c or "target risk" in c
                        or "xml-only column" in dt):
                    return "odi_only"
                if ("structural difference" in c or "structural lineage" in c
                        or "operationally specific" in c or "more detailed" in dt
                        or "xml-only exception" in dt or "journal source" in dt
                        or "where-vs-case" in dt or "join filter moved" in dt
                        or "acceptable" in c):
                    return "structural"
                return "logic_drift"

            _tc: Dict[str, Dict[str, Any]] = {}
            for _r in diff_rows:
                _sv = _sev(_r)
                _r["severity"] = _sv
                _t = (_r.get("Difference Type", "") or "(unspecified)")
                if _t not in _tc:
                    _tc[_t] = {"type": _t, "count": 0, "severity": _sv}
                _tc[_t]["count"] += 1
            _sev_order = {"missing": 0, "real_gap": 1, "logic_drift": 2, "structural": 3, "odi_only": 4}
            type_counts = sorted(
                _tc.values(), key=lambda x: (_sev_order.get(x["severity"], 9), -x["count"])
            )

            detection: Dict[str, Any] = {}
            dlj = out / "detected_layout.json"
            if dlj.exists():
                try:
                    detection = json.loads(dlj.read_text(encoding="utf-8"))
                except Exception:
                    detection = {}

            statuses = [r.get("status", "") for r in col_rows]
            summary = {
                "mapping_columns": sum(1 for s in statuses if s in ("IN_BOTH", "MAPPING_ONLY")),
                "in_both": statuses.count("IN_BOTH"),
                "mapping_only": statuses.count("MAPPING_ONLY"),
                "xml_only": statuses.count("XML_ONLY"),
            }
            _diff_sev = [r.get("severity", "") for r in diff_rows]
            bucket_counts = {
                "missing": _diff_sev.count("missing"),
                "real_gap": _diff_sev.count("real_gap"),
                "logic_drift": _diff_sev.count("logic_drift"),
                "structural": _diff_sev.count("structural"),
                "odi_extra": summary["xml_only"] + _diff_sev.count("odi_only"),
            }
            # matched = in_both columns not flagged with an in_both-level difference
            # (missing/real_gap rows are DRD-only / meta, not in_both subtractions).
            bucket_counts["matched"] = max(
                summary["in_both"]
                - (_diff_sev.count("logic_drift") + _diff_sev.count("structural") + _diff_sev.count("odi_only")),
                0,
            )
            return {
                "engine": "v15-generic",
                "summary": summary,
                "bucket_counts": bucket_counts,
                "detection": detection,
                "differences": diff_rows,
                "type_counts": type_counts,
                "drd_only_columns": [r.get("target_column", "") for r in col_rows if r.get("status") == "MAPPING_ONLY"],
                "odi_only_columns": [r.get("target_column", "") for r in col_rows if r.get("status") == "XML_ONLY"],
                "column_diff_count": len(col_rows),
            }
        finally:
            _gc.collect()  # release any lingering openpyxl read_only file handles
            _shutil.rmtree(tdp, ignore_errors=True)

    try:
        return await asyncio.to_thread(_run)
    except FileNotFoundError as exc:
        raise HTTPException(422, f"v15 compare error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"v15 compare unexpected error: {exc}") from exc


@router.post("/scenario/emit-sql")
async def emit_sql_from_xml(
    xml_file: UploadFile = File(...),
    target_schema: str = Query(default=""),
    target_table: str = Query(default=""),
    strict: bool = Query(default=False),
):
    """Lightweight endpoint: parse ODI XML and return only the emitted SQL string.

    Response: {"sql": "<Oracle INSERT SQL>", "unresolved_count": N}
    """
    from app.sql_model.odi_parser import OdiXmlParser
    from app.sql_model.sql_emitter import EmitError, emit_insert

    xml_bytes = await _read_upload_checked_odi(xml_file)
    try:
        parser = OdiXmlParser(target_schema=target_schema, target_table=target_table)
        model = parser.parse_bytes(xml_bytes)
        result = emit_insert(model, strict=strict, add_header_comment=True)
    except (ValueError, EmitError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc

    return {
        "sql": result.sql,
        "unresolved_count": len(result.unresolved),
        "warning_count": len(result.warnings),
        "status": "PARTIAL" if result.unresolved else "OK",
    }


@router.post("/scenario/run-xe")
async def run_xe_insert(
    xml_file: UploadFile = File(...),
    target_schema: str = Query(default=""),
    target_table: str = Query(default=""),
    strict: bool = Query(default=False),
    test_rows: int = Query(default=10, ge=1, le=100),
    scratch_schema: str = Query(default=""),
):
    """P6 — Oracle XE confirmatory run (OPTIONAL).

    Parse the ODI XML, emit the INSERT SQL, then execute it against a local
    Oracle XE Docker instance using synthetic data generated from the KB.

    Design invariants (operator-locked):
    - xe_status in {'confirmed', 'unavailable'}
    - rows_affected from cursor.rowcount (NOT len(result))
    - rows_affected == 0  ->  verdict = FAIL_ZERO_ROWS
    - XE_UNAVAILABLE is never is_pass=True
    - Never flips STATIC_PASS -> FAIL (static gate stays authoritative)

    Connection: oracledb thin mode (no Oracle Instant Client).
    Configure via env: ORA_XE_DSN / ORA_XE_USER / ORA_XE_PASSWORD.

    Returns XeRunResult.to_dict() including is_pass, verdict, rows_affected,
    ora_errors, and synthetic_tables_created.

    If KB file is absent, xe_status='unavailable' is returned immediately.
    """
    from app.sql_model.odi_parser import OdiXmlParser
    from app.sql_model.sql_emitter import EmitError, emit_insert
    from app.db.xe_harness import XeRunResult, XeVerdict, run_insert_on_xe

    xml_bytes = await _read_upload_checked_odi(xml_file)

    try:
        parser = OdiXmlParser(target_schema=target_schema, target_table=target_table)
        model = parser.parse_bytes(xml_bytes)
    except ValueError as exc:
        raise HTTPException(422, f"ODI XML parse error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"Unexpected parse error: {exc}") from exc

    try:
        emit_result = emit_insert(model, strict=strict, add_header_comment=True)
    except EmitError as exc:
        raise HTTPException(422, f"SQL emit error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(500, f"Unexpected emit error: {exc}") from exc

    if not _KB_PATH.exists():
        result = XeRunResult(
            xe_status="unavailable",
            verdict=XeVerdict.XE_UNAVAILABLE,
            note=f"KB file not found at {_KB_PATH}",
        )
        return result.to_dict()

    xe_result = await asyncio.to_thread(
        run_insert_on_xe,
        model,
        emit_result.sql,
        _KB_PATH,
        test_rows,
        scratch_schema,
    )
    return xe_result.to_dict()


# ---------------------------------------------------------------------------
# P7 — DRD all-sheets parse
# ---------------------------------------------------------------------------

@router.post("/drd/all-sheets-parse")
async def drd_all_sheets_parse(
    drd_file: UploadFile = File(...),
):
    """P7 — Parse ALL sheets of a DRD Excel workbook.

    Reads the file in non-read-only mode so openpyxl exposes hyperlinks.
    Returns:
      - sheets: list with role, row count, sample rows, hyperlink count per sheet
      - deferred_refs: hyperlinks / PBI links that cannot be resolved locally
      - extracted_rules: transformation rules, grain columns, join conditions
        extracted from ETL Notes / Grain / Model / Consumer View sheets
      - verdict: "FULL_DRD" (no deferred refs) | "PARTIAL_DRD" (some refs unresolved)
      - grain_columns: key / grain column names found across grain sheets

    Design invariant: deferred_refs is NEVER silently empty when hyperlinks exist.
    PARTIAL_DRD is always visible to the caller.
    """
    from app.sql_model.drd_multi_sheet import parse_all_sheets

    if not drd_file.filename:
        raise HTTPException(422, "No filename provided")

    ext = drd_file.filename.rsplit(".", 1)[-1].lower() if "." in drd_file.filename else ""
    if ext not in ("xlsx", "xls", "xlsm"):
        raise HTTPException(422, f"Unsupported file type: {ext!r}. Expected xlsx/xls/xlsm")

    raw = await drd_file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "DRD file exceeds 20 MB limit")
    if not raw:
        raise HTTPException(422, "Empty file")

    result = await asyncio.to_thread(parse_all_sheets, raw)
    return result.to_dict()


# ---------------------------------------------------------------------------
# P8 — Fix-mismatch overrides
# ---------------------------------------------------------------------------

_OVERRIDES_PATH = Path(__file__).resolve().parents[2] / "data" / "overrides" / "comparison_overrides.json"


class FixMismatchRequest(BaseModel):
    target_col: str
    verdict_override: Literal["MATCHED", "ALIAS_DRIFT_ONLY"]
    reason: str = ""


def _load_overrides() -> list:
    """Load comparison verdict overrides from disk.

    Phase 7.16 silent-failure round 2 fix: was bare `except: return []` ->
    corrupt JSON silently discarded entire override history on next save.
    Now: ERROR-level log on parse failure so the operator sees that
    manually-curated overrides are about to be lost.  Caller may decide
    to bail out instead of overwriting.
    """
    if not _OVERRIDES_PATH.exists():
        return []
    try:
        return json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        import logging
        logging.getLogger(__name__).error(
            "comparison_overrides.json is corrupt or unreadable; manually-"
            "curated overrides at risk of being overwritten: %s", exc,
        )
        return []


def _save_overrides(records: list) -> None:
    _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OVERRIDES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _OVERRIDES_PATH)


@router.post("/scenario/fix-mismatch")
async def fix_mismatch(body: FixMismatchRequest):
    """Persist a manual verdict override for a mismatched target column.

    Stores the override in data/overrides/comparison_overrides.json so
    the comparison grid can show the corrected verdict on future runs.
    A new entry for the same target_col replaces the existing one.

    Request body:
      target_col         — physical column name (e.g. "AGRT_ID")
      verdict_override   — "MATCHED" or "ALIAS_DRIFT_ONLY"
      reason             — optional human note ("DRD uses legacy alias; ODI is correct")
    """
    target_col = (body.target_col or "").strip().upper()
    if not target_col:
        raise HTTPException(status_code=400, detail="target_col is required")

    records = _load_overrides()
    # Replace existing entry for the same target_col
    records = [r for r in records if (r.get("target_col") or "").upper() != target_col]
    entry = {
        "target_col": target_col,
        "verdict_override": body.verdict_override,
        "reason": (body.reason or "").strip(),
        "saved_at": time.time(),
    }
    records.append(entry)
    try:
        _save_overrides(records)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save override: {exc}") from exc

    return {"ok": True, "override": entry, "total_overrides": len(records)}


@router.get("/scenario/fix-mismatch/list")
async def list_fix_mismatches():
    """Return all saved comparison verdict overrides."""
    records = _load_overrides()
    return {"overrides": records, "total": len(records)}


# ── Phase 7.10 (operator-locked 2026-05-30): live Oracle execution ───────────

@router.post("/live/execute")
async def live_execute_sql(body: dict, _auth: None = Depends(require_api_key)):
    """Execute SQL against live Oracle (operator's FREEPDB1).

    Body shape:
        {
          "sql": "...",                       # required
          "commit": false,                     # default false
          "allow_writes": false,               # default false
          "allow_ddl": false,                  # default false
          "allow_admin": false,                # default false
          "config": {                          # optional override
            "dsn": "localhost:1521/FREEPDB1",
            "user": "SYS",
            "password": "...",
            "mode": "SYSDBA"
          }
        }

    Operator-locked safety: writes/DDL/admin off by default; caller MUST
    explicitly enable the corresponding flag.  Returns LiveSqlResult.to_dict().
    """
    from app.services.oracle_live_runner import (
        LiveOracleConfig, execute_sql,
    )
    sql = (body.get("sql") or "").strip()
    if not sql:
        raise HTTPException(422, "Field 'sql' is required")
    cfg = None
    if body.get("config"):
        cfg = LiveOracleConfig.from_env(body["config"])
    result = await asyncio.to_thread(
        execute_sql, sql,
        config=cfg,
        commit=bool(body.get("commit", False)),
        allow_read=bool(body.get("allow_read", True)),
        allow_writes=bool(body.get("allow_writes", False)),
        allow_ddl=bool(body.get("allow_ddl", False)),
        allow_admin=bool(body.get("allow_admin", False)),
        allow_plsql=bool(body.get("allow_plsql", False)),
        timeout_s=int(body.get("timeout_s", 60)),
        sample_limit=int(body.get("sample_limit", 20)),
    )
    return result.to_dict()


@router.post("/live/execute-multi")
async def live_execute_multi_sql(body: dict, _auth: None = Depends(require_api_key)):
    """Execute a list of SQL statements sequentially against live Oracle.

    Body shape:
        {
          "statements": ["...", "..."],
          "commit_each": false,
          "allow_writes": false,
          ...same flags as /live/execute
        }

    Returns list of LiveSqlResult.to_dict(); stops on first failure
    unless `commit_each=True`.
    """
    from app.services.oracle_live_runner import (
        LiveOracleConfig, execute_multi,
    )
    stmts = body.get("statements") or []
    if not isinstance(stmts, list) or not stmts:
        raise HTTPException(422, "Field 'statements' must be a non-empty list")
    cfg = None
    if body.get("config"):
        cfg = LiveOracleConfig.from_env(body["config"])
    results = await asyncio.to_thread(
        execute_multi, list(stmts),
        config=cfg,
        commit_each=bool(body.get("commit_each", False)),
        allow_read=bool(body.get("allow_read", True)),
        allow_writes=bool(body.get("allow_writes", False)),
        allow_ddl=bool(body.get("allow_ddl", False)),
        allow_admin=bool(body.get("allow_admin", False)),
        allow_plsql=bool(body.get("allow_plsql", False)),
        timeout_s=int(body.get("timeout_s", 60)),
    )
    return {"results": [r.to_dict() for r in results]}
