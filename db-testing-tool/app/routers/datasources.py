"""DataSource management endpoints."""
import asyncio
import json
import threading
import time
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.datasource import DataSource
from app.connectors.factory import get_connector
from app.config import settings
from app.security import require_api_key, require_api_key_request
from app.secret_store import (
    SecretStoreConfigError,
    encrypt_secret,
    encrypt_sensitive_extra_params,
)
from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime, timezone
import re
import difflib

router = APIRouter(prefix="/api/datasources", tags=["datasources"])


class DataSourceCreate(BaseModel):
    name: str
    db_type: str                    # oracle | redshift | sqlserver
    host: str
    port: Optional[int] = None
    database_name: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    extra_params: Optional[str] = None


class DataSourceOut(BaseModel):
    id: int
    name: str
    db_type: str
    host: str
    port: Optional[int]
    database_name: Optional[str]
    username: Optional[str]
    status: str
    last_tested_at: Optional[str]
    is_active: bool

    class Config:
        from_attributes = True


class QueryInput(BaseModel):
    sql: str
    row_limit: Optional[int] = None
    allow_writes: bool = False
    allow_ddl: bool = False
    allow_admin: bool = False


MAX_QUERY_ROWS = 50
MAX_QUERY_ROWS_UPPER_BOUND = 1000
DEFAULT_CONNECTOR_SESSION_MINUTES = settings.REDSHIFT_DEFAULT_SESSION_MINUTES

_CONNECTOR_CACHE_LOCK = threading.Lock()
_CONNECTOR_CACHE = {}
_QUERY_LOCKS = {}


def _ds_fingerprint(ds: DataSource) -> str:
    return "|".join([
        str(ds.id),
        str(ds.db_type or ""),
        str(ds.host or ""),
        str(ds.port or ""),
        str(ds.database_name or ""),
        str(ds.username or ""),
        str(ds.password or ""),
        str(ds.extra_params or ""),
    ])


def _session_ttl_seconds(ds: DataSource) -> int:
    ttl_minutes = DEFAULT_CONNECTOR_SESSION_MINUTES
    try:
        extras = json.loads(ds.extra_params) if ds.extra_params else {}
        if isinstance(extras, dict):
            configured = extras.get("session_minutes") or extras.get("auth_session_minutes")
            if configured is not None:
                ttl_minutes = max(5, min(720, int(configured)))
    except Exception:
        pass
    return ttl_minutes * 60


def _get_query_lock(ds_id: int) -> asyncio.Lock:
    lock = _QUERY_LOCKS.get(ds_id)
    if lock is None:
        lock = asyncio.Lock()
        _QUERY_LOCKS[ds_id] = lock
    return lock


def _pop_cached_connector(ds_id: int):
    with _CONNECTOR_CACHE_LOCK:
        entry = _CONNECTOR_CACHE.pop(ds_id, None)
    return entry


async def _close_cached_connector(ds_id: int):
    entry = _pop_cached_connector(ds_id)
    if not entry:
        return
    conn = entry.get("connector")
    if conn is None:
        return
    try:
        await asyncio.to_thread(conn.disconnect)
    except Exception:
        pass


async def _get_or_create_cached_connector(ds: DataSource):
    if (ds.db_type or "").lower().strip() != "redshift":
        conn = await asyncio.to_thread(get_connector, ds)
        return conn, False

    now = time.time()
    fingerprint = _ds_fingerprint(ds)
    ttl_sec = _session_ttl_seconds(ds)

    with _CONNECTOR_CACHE_LOCK:
        entry = _CONNECTOR_CACHE.get(ds.id)
        if entry and entry.get("fingerprint") == fingerprint and entry.get("expires_at", 0) > now:
            entry["expires_at"] = now + ttl_sec
            return entry.get("connector"), True

    if entry:
        stale_conn = entry.get("connector")
        if stale_conn is not None:
            try:
                await asyncio.to_thread(stale_conn.disconnect)
            except Exception:
                pass

    conn = await asyncio.to_thread(get_connector, ds)

    with _CONNECTOR_CACHE_LOCK:
        _CONNECTOR_CACHE[ds.id] = {
            "connector": conn,
            "fingerprint": fingerprint,
            "expires_at": now + ttl_sec,
        }
    return conn, False


def _strip_leading_sql_comments(sql: str) -> str:
    text = (sql or "").strip()
    if not text:
        return ""
    # Remove any leading comment blocks/lines repeatedly before command check.
    while True:
        original = text
        text = re.sub(r"^\s*/\*.*?\*/\s*", "", text, flags=re.S)
        text = re.sub(r"^\s*--[^\n]*(?:\n|$)", "", text, flags=re.M)
        if text == original:
            break
    return text


def _is_resultset_sql(sql: str) -> bool:
    text = _strip_leading_sql_comments(sql)
    if not text:
        return False
    lowered = text.lower().lstrip()
    return lowered.startswith(("select", "with", "show", "describe", "desc", "explain"))


def _statement_type(sql: str) -> str:
    text = _strip_leading_sql_comments(sql)
    if not text:
        return "UNKNOWN"
    token = (text.split(None, 1)[0] if text.split(None, 1) else "").upper()
    return token or "UNKNOWN"

_DATA_WRITE_STATEMENTS = {"INSERT", "UPDATE", "DELETE", "MERGE", "CALL"}
_DDL_STATEMENTS = {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "COMMENT"}
_ADMIN_STATEMENTS = {"GRANT", "REVOKE", "PURGE", "FLASHBACK", "BEGIN", "DECLARE"}
_PRIVILEGED_ORACLE_MODES = {"SYSDBA", "SYSOPER", "SYSASM", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC"}
_SENSITIVE_EXTRA_PARAM_KEYS = {
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


def _parse_extra_params(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _enforce_datasource_privilege_policy(body: DataSourceCreate) -> None:
    db_kind = (body.db_type or "").strip().lower()
    if db_kind != "oracle":
        return

    username = (body.username or "").strip().upper()
    if username == "SYS":
        raise HTTPException(status_code=403, detail="Oracle SYS datasource credentials are not allowed")

    extras = _parse_extra_params(body.extra_params)
    for key in ("mode", "auth_mode", "oracle_mode", "privilege"):
        mode = str(extras.get(key) or "").strip().upper()
        if mode in _PRIVILEGED_ORACLE_MODES:
            raise HTTPException(status_code=403, detail=f"Oracle privileged mode {mode} is not allowed")


def _prepare_datasource_payload(body: DataSourceCreate) -> dict[str, Any]:
    payload = body.model_dump()
    try:
        payload["password"] = encrypt_secret(payload.get("password"))
        payload["extra_params"] = encrypt_sensitive_extra_params(payload.get("extra_params"))
    except SecretStoreConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return payload


def _redact_extra_params(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return "<redacted>"
    if not isinstance(parsed, dict):
        return "<redacted>"

    redacted: dict[str, Any] = {}
    for key, value in parsed.items():
        if any(marker in str(key).lower() for marker in _SENSITIVE_EXTRA_PARAM_KEYS):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return json.dumps(redacted, sort_keys=True)


def _enforce_query_statement_allowed(stmt_type: str, runs_as_resultset: bool, body: QueryInput) -> None:
    if runs_as_resultset:
        return
    if stmt_type in _DATA_WRITE_STATEMENTS and body.allow_writes:
        return
    if stmt_type in _DDL_STATEMENTS and body.allow_ddl:
        return
    if stmt_type in _ADMIN_STATEMENTS and body.allow_admin:
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"Statement type {stmt_type!r} is blocked by default. "
            "Set the matching allow_* flag and provide X-DBTOOL-API-Key."
        ),
    )


def _requires_api_key_for_statement(stmt_type: str, runs_as_resultset: bool, body: QueryInput) -> bool:
    if runs_as_resultset:
        return False
    if stmt_type in _DATA_WRITE_STATEMENTS and body.allow_writes:
        return True
    if stmt_type in _DDL_STATEMENTS and body.allow_ddl:
        return True
    if stmt_type in _ADMIN_STATEMENTS and body.allow_admin:
        return True
    return False


def _oracle_q_literal_end(text: str, start: int) -> Optional[int]:
    if start + 2 >= len(text):
        return None
    if text[start] not in {"q", "Q"} or text[start + 1] != "'":
        return None
    opener = text[start + 2]
    closer = {"[": "]", "(": ")", "{": "}", "<": ">"}.get(opener, opener)
    end_seq = closer + "'"
    pos = text.find(end_seq, start + 3)
    if pos < 0:
        return None
    return pos + len(end_seq)


def _split_sql_statements_with_lines(script: str) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    s = script or ""
    start_idx: Optional[int] = None
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    i = 0
    n = len(s)

    while i < n:
        ch = s[i]
        nxt = s[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if not in_single and not in_double:
            if ch == "-" and nxt == "-":
                in_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue

        if not in_single and not in_double:
            q_end = _oracle_q_literal_end(s, i)
            if q_end is not None:
                if start_idx is None:
                    start_idx = i
                i = q_end
                continue

        if ch == "'" and not in_double:
            if in_single and nxt == "'":
                i += 2
                continue
            in_single = not in_single
            if start_idx is None and not ch.isspace():
                start_idx = i
            i += 1
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            if start_idx is None and not ch.isspace():
                start_idx = i
            i += 1
            continue

        if start_idx is None and not ch.isspace() and ch != ';':
            start_idx = i

        if ch == ";" and not in_single and not in_double:
            if start_idx is not None:
                chunk = s[start_idx:i].strip()
                if chunk:
                    line, col = _line_col_from_index(s, start_idx)
                    statements.append({"sql": chunk, "start_idx": start_idx, "start_line": line, "start_column": col})
            start_idx = None
            i += 1
            continue

        i += 1

    if start_idx is not None:
        chunk = s[start_idx:].strip()
        if chunk:
            line, col = _line_col_from_index(s, start_idx)
            statements.append({"sql": chunk, "start_idx": start_idx, "start_line": line, "start_column": col})

    return statements


def _normalize_oracle_date_literals(sql: str) -> str:
    return re.sub(
        r"(>=|<=|=|>|<)\s*'(\d{4}-\d{2}-\d{2})'",
        r"\1 DATE '\2'",
        sql,
        flags=re.I,
    )


def _apply_row_cap(db_type: str, sql: str, max_rows: int) -> str:
    kind = (db_type or "").lower().strip()
    if kind == "oracle":
        # Always cap Oracle rows outside the original statement to preserve
        # query semantics for aggregates (COUNT/DISTINCT/SUM/etc.).
        return f"SELECT * FROM ({sql}) q WHERE ROWNUM <= {max_rows}"
    if kind == "sqlserver":
        return f"SELECT TOP {max_rows} * FROM ({sql}) q"
    return f"SELECT * FROM ({sql}) q LIMIT {max_rows}"


def _line_col_from_index(text: str, idx: int) -> tuple[int, int]:
    if idx < 0:
        return 1, 1
    prefix = (text or "")[:idx]
    line = prefix.count("\n") + 1
    last_nl = prefix.rfind("\n")
    col = idx + 1 if last_nl < 0 else (idx - last_nl)
    return line, col


def _extract_error_position(message: str, sql_text: str = "") -> tuple[Optional[int], Optional[int]]:
    text = str(message or "")
    m = re.search(r"position\s*[:=]?\s*(\d+)", text, flags=re.IGNORECASE)
    if m:
        pos = max(0, int(m.group(1)) - 1)
        return _line_col_from_index(sql_text, pos)
    m = re.search(r"at\s+character\s+(\d+)", text, flags=re.IGNORECASE)
    if m:
        pos = max(0, int(m.group(1)) - 1)
        return _line_col_from_index(sql_text, pos)
    m = re.search(r"ORA-06550:\s*line\s*(\d+)\s*,\s*column\s*(\d+)", text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"line\s+(\d+)\s*,\s*column\s+(\d+)", text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"line\s+(\d+)", text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1)), None
    return None, None


def _extract_table_aliases_with_pos(sql: str) -> list[dict]:
    refs: list[dict] = []
    patt = re.compile(r'\b(FROM|JOIN|INTO)\s+([A-Z0-9_\.\"]+)(?:\s+([A-Z][A-Z0-9_]*))?', flags=re.IGNORECASE)
    for m in patt.finditer(sql or ""):
        raw = (m.group(2) or "").strip()
        if not raw or raw.startswith("("):
            continue
        token = raw.replace('"', '')
        parts = token.split('.')
        schema = parts[-2].upper() if len(parts) >= 2 else ""
        table = parts[-1].upper()
        alias = (m.group(3) or "").strip().upper() or table
        line, col = _line_col_from_index(sql or "", m.start(2))
        refs.append({"schema": schema, "table": table, "alias": alias, "line": line, "column": col})
    return refs


def _analyze_sql_references(connector, sql: str) -> dict:
    refs = _extract_table_aliases_with_pos(sql)
    alias_map = {r["alias"]: (r["schema"], r["table"]) for r in refs if r.get("alias")}

    missing_tables: list[dict] = []
    for r in refs:
        schema = r.get("schema") or ""
        table = r.get("table") or ""
        if not schema or not table:
            continue
        exists: Optional[bool] = True
        if hasattr(connector, "table_exists"):
            try:
                exists = bool(connector.table_exists(schema, table))
            except Exception as exc:
                # Phase 7.16 silent-failure round 2 fix: was `exists = True`
                # which suppressed the missing-table warning when the probe
                # itself failed.  Now: log + set None so the missing-table
                # check is skipped (NOT silently assumed-True).
                import logging
                logging.getLogger(__name__).debug(
                    "table_exists probe failed for %s.%s: %s", schema, table, exc,
                )
                exists = None
        if exists is False:
            closest_table = None
            if hasattr(connector, "get_tables"):
                try:
                    known = [str(t.table_name).upper() for t in connector.get_tables(schema)]
                    matches = difflib.get_close_matches(table, known, n=1, cutoff=0.62)
                    if matches:
                        closest_table = matches[0]
                except Exception:
                    pass
            missing_tables.append({
                "schema": schema,
                "table": table,
                "line": r.get("line"),
                "column": r.get("column"),
                "closest_table": closest_table,
            })

    columns_cache: dict = {}
    missing_columns: list[dict] = []
    for m in re.finditer(r"\b([A-Z_][A-Z0-9_]*)\.([A-Z_][A-Z0-9_\$#]*)\b", (sql or ""), flags=re.IGNORECASE):
        alias = (m.group(1) or "").upper()
        col_name = (m.group(2) or "").upper()
        if alias not in alias_map:
            continue
        schema, table = alias_map[alias]
        if not schema or not table or not hasattr(connector, "get_columns"):
            continue
        cache_key = (schema, table)
        if cache_key not in columns_cache:
            try:
                cols = connector.get_columns(schema, table)
                columns_cache[cache_key] = {c.column_name.upper() for c in cols}
            except Exception as exc:
                # Phase 7.16 silent-failure round 2 fix: was `set()` which
                # silently skipped column-validation for the whole table
                # (an empty set tests as falsy in `if columns_cache[...]`).
                # Now: None distinguishes "probe failed" from "table has no
                # known columns".
                import logging
                logging.getLogger(__name__).debug(
                    "get_columns probe failed for %s.%s: %s", schema, table, exc,
                )
                columns_cache[cache_key] = None
        cols_set = columns_cache[cache_key]
        if cols_set and col_name not in cols_set:
            line, col = _line_col_from_index(sql or "", m.start(2))
            closest_column = None
            try:
                matches = difflib.get_close_matches(col_name, list(cols_set), n=1, cutoff=0.58)
                if matches:
                    closest_column = matches[0]
            except Exception:
                pass
            missing_columns.append({
                "schema": schema,
                "table": table,
                "column": col_name,
                "alias": alias,
                "line": line,
                "column_pos": col,
                "closest_column": closest_column,
            })

    unknown_aliases: list[dict] = []
    for m in re.finditer(r"\b([A-Z_][A-Z0-9_]*)\.([A-Z_][A-Z0-9_\$#]*)\b", (sql or ""), flags=re.IGNORECASE):
        alias = (m.group(1) or "").upper()
        if alias in alias_map or alias in {"SYS", "DUAL"}:
            continue
        line, col = _line_col_from_index(sql or "", m.start(1))
        unknown_aliases.append({"alias": alias, "column": (m.group(2) or "").upper(), "line": line, "column_pos": col})

    suggestions: list[str] = []
    for t in missing_tables:
        hint = f" Did you mean {t['schema']}.{t['closest_table']}?" if t.get("closest_table") else ""
        suggestions.append(
            f"Table not found at line {t.get('line')}: {t['schema']}.{t['table']}. Verify schema/table name or datasource.{hint}"
        )
    for c in missing_columns:
        hint = f" Closest column: {c['closest_column']}." if c.get("closest_column") else ""
        suggestions.append(
            f"Column not found at line {c.get('line')}: {c['schema']}.{c['table']}.{c['column']} (alias {c['alias']}). Fix table/column mapping.{hint}"
        )
    for a in unknown_aliases[:10]:
        suggestions.append(
            f"Unknown alias at line {a.get('line')}: {a['alias']}.{a['column']}. Define alias in FROM/JOIN or fix typo."
        )

    return {
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "unknown_aliases": unknown_aliases,
        "suggestions": suggestions,
    }


@router.get("")
async def list_datasources(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DataSource).order_by(DataSource.name))
    items = result.scalars().all()
    return [
        {
            "id": ds.id, "name": ds.name, "db_type": ds.db_type,
            "host": ds.host, "port": ds.port, "database_name": ds.database_name,
            "username": ds.username, "status": ds.status,
            "is_active": ds.is_active,
            "last_tested_at": str(ds.last_tested_at) if ds.last_tested_at else None,
        }
        for ds in items
    ]


@router.get("/{ds_id}")
async def get_datasource(ds_id: int, db: AsyncSession = Depends(get_db)):
    ds = await db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "DataSource not found")
    return {
        "id": ds.id,
        "name": ds.name,
        "db_type": ds.db_type,
        "host": ds.host,
        "port": ds.port,
        "database_name": ds.database_name,
        "username": ds.username,
        # SECURITY: Do not return plaintext password or raw extra_params.
        "extra_params": _redact_extra_params(ds.extra_params),
        "status": ds.status,
        "is_active": ds.is_active,
        "last_tested_at": str(ds.last_tested_at) if ds.last_tested_at else None,
    }


@router.get("/export-env")
async def export_datasources_env(db: AsyncSession = Depends(get_db)):
    """REMOVED for security: This endpoint previously exported plaintext datasource passwords.
    
    Datasource credentials cannot be exported via HTTP endpoints.
    Use secure credential management patterns instead.
    """
    raise HTTPException(status_code=410, detail="This endpoint has been removed for security reasons. Plaintext datasource credentials cannot be exported.")


@router.post("")
async def create_datasource(body: DataSourceCreate, db: AsyncSession = Depends(get_db), _auth: None = Depends(require_api_key)):
    _enforce_datasource_privilege_policy(body)
    ds = DataSource(**_prepare_datasource_payload(body))
    db.add(ds)
    await db.commit()
    await db.refresh(ds)
    return {"id": ds.id, "name": ds.name, "status": "created"}


@router.post("/{ds_id}/test")
async def test_connection(ds_id: int, db: AsyncSession = Depends(get_db)):
    ds = await db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "DataSource not found")
    try:
        if (ds.db_type or "").lower().strip() == "redshift":
            conn = await asyncio.to_thread(get_connector, ds)
            result = await asyncio.to_thread(conn.test_connection)
        else:
            conn = get_connector(ds)
            result = conn.test_connection()
        ds.status = "ok" if result.success else "error"
        ds.last_tested_at = datetime.now(timezone.utc)
        await db.commit()
        return {"success": result.success, "message": result.message, "version": result.server_version}
    except Exception as e:
        ds.status = "error"
        await db.commit()
        return {"success": False, "message": str(e)}


@router.delete("/{ds_id}")
async def delete_datasource(ds_id: int, db: AsyncSession = Depends(get_db), _auth: None = Depends(require_api_key)):
    ds = await db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "DataSource not found")
    await _close_cached_connector(ds_id)
    await db.delete(ds)
    await db.commit()
    return {"deleted": True}


@router.put("/{ds_id}")
async def update_datasource(ds_id: int, body: DataSourceCreate, db: AsyncSession = Depends(get_db), _auth: None = Depends(require_api_key)):
    _enforce_datasource_privilege_policy(body)
    payload = _prepare_datasource_payload(body)
    ds = await db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "DataSource not found")
    await _close_cached_connector(ds_id)
    for k, v in payload.items():
        if k == "password" and (v is None or v == ""):
            continue
        setattr(ds, k, v)
    await db.commit()
    return {"id": ds.id, "updated": True}


@router.post("/{ds_id}/query")
async def query_datasource(ds_id: int, body: QueryInput, request: Request, db: AsyncSession = Depends(get_db)):
    ds = await db.get(DataSource, ds_id)
    if not ds:
        raise HTTPException(404, "DataSource not found")

    sql = (body.sql or "").strip()
    if not sql:
        raise HTTPException(400, "SQL is required")
    requested_row_cap = body.row_limit if isinstance(body.row_limit, int) else MAX_QUERY_ROWS
    row_cap = max(1, min(MAX_QUERY_ROWS_UPPER_BOUND, int(requested_row_cap)))
    statements = _split_sql_statements_with_lines(sql)
    if not statements:
        raise HTTPException(400, "SQL is required")

    if ds.db_type.lower() in {"redshift", "sqlserver"}:
        for st in statements:
            st["sql"] = re.sub(r"\s+from\s+dual\s*$", "", st["sql"], flags=re.I)
    elif ds.db_type.lower() == "oracle":
        for st in statements:
            st["sql"] = _normalize_oracle_date_literals(st["sql"])

    query_lock = _get_query_lock(ds.id)
    async with query_lock:
        conn = None
        try:
            db_kind = (ds.db_type or "").lower().strip()
            if db_kind == "redshift":
                conn, from_cache = await _get_or_create_cached_connector(ds)
            else:
                conn = get_connector(ds)

            executions: list[dict[str, Any]] = []
            final_rows: list[dict[str, Any]] = []
            final_columns: list[str] = []
            total_rows_affected = 0

            for idx, st in enumerate(statements, start=1):
                stmt_sql = (st.get("sql") or "").strip()
                if not stmt_sql:
                    continue

                stmt_type = _statement_type(stmt_sql)
                runs_as_resultset = _is_resultset_sql(stmt_sql)
                _enforce_query_statement_allowed(stmt_type, runs_as_resultset, body)
                if _requires_api_key_for_statement(stmt_type, runs_as_resultset, body):
                    require_api_key_request(request)
                use_oracle_native_cap = db_kind == "oracle" and runs_as_resultset
                exec_sql = _apply_row_cap(ds.db_type, stmt_sql, row_cap) if (runs_as_resultset and not use_oracle_native_cap) else stmt_sql

                try:
                    if db_kind == "redshift":
                        try:
                            rows = await asyncio.to_thread(conn.execute_query, exec_sql)
                        except Exception:
                            if not from_cache:
                                raise
                            await _close_cached_connector(ds.id)
                            conn, _ = await _get_or_create_cached_connector(ds)
                            rows = await asyncio.to_thread(conn.execute_query, exec_sql)
                    elif use_oracle_native_cap:
                        rows = await asyncio.to_thread(conn.execute_query, exec_sql, None, row_cap)
                    else:
                        rows = await asyncio.to_thread(conn.execute_query, exec_sql)
                except Exception as stmt_exc:
                    stmt_err = str(stmt_exc)
                    local_line, local_col = _extract_error_position(stmt_err, stmt_sql)
                    start_line = int(st.get("start_line") or 1)
                    absolute_line = (start_line + local_line - 1) if local_line else None
                    diagnostics = {}
                    try:
                        diagnostics = _analyze_sql_references(conn, stmt_sql)
                    except Exception:
                        diagnostics = {}
                    suggestions = list((diagnostics or {}).get("suggestions") or [])
                    if "ORA-00942" in stmt_err.upper():
                        suggestions.append("ORA-00942 detected: one of referenced schema.table objects does not exist or is not accessible.")
                    if "ORA-00904" in stmt_err.upper():
                        suggestions.append("ORA-00904 detected: invalid identifier. Verify table alias and column spelling.")
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "error": stmt_err,
                            "line": absolute_line,
                            "column": local_col,
                            "statement_index": idx,
                            "statement_type": stmt_type,
                            "statement_start_line": start_line if local_line else None,
                            "statement_preview": stmt_sql[:200],
                            "sql": sql,
                            "diagnostics": diagnostics,
                            "suggestions": suggestions,
                        },
                    )

                rows = rows or []
                columns = list(rows[0].keys()) if rows else []
                rows_affected = 0
                if columns == ["ROWS_AFFECTED"]:
                    rows_affected = int((rows[0] or {}).get("ROWS_AFFECTED") or 0)
                    rows = []
                    columns = []
                total_rows_affected += rows_affected

                capped = bool(runs_as_resultset and len(rows) >= row_cap)
                executions.append({
                    "index": idx,
                    "statement": stmt_sql,
                    "statement_preview": stmt_sql[:200],
                    "statement_type": stmt_type,
                    "start_line": st.get("start_line"),
                    "start_column": st.get("start_column"),
                    "rows_affected": rows_affected,
                    "row_count": len(rows),
                    "columns": columns,
                    "capped": capped,
                })

                if columns or rows:
                    final_rows = rows
                    final_columns = columns

            has_rows = bool(final_rows or final_columns)
            if not has_rows and executions:
                final_message = f"Script executed successfully. Rows affected: {total_rows_affected}."
            elif len(statements) > 1:
                final_message = f"Script executed successfully ({len(executions)} statement(s))."
            else:
                final_message = "Statement executed successfully."

            return {
                "datasource_id": ds.id,
                "datasource_name": ds.name,
                "columns": final_columns,
                "rows": final_rows,
                "row_count": len(final_rows),
                "row_cap": row_cap,
                "capped": len(final_rows) >= row_cap,
                "executions": executions,
                "message": final_message,
                "total_rows_affected": total_rows_affected,
            }
        except HTTPException:
            raise
        except Exception as e:
            err = str(e)
            line, col = _extract_error_position(err, sql)
            diagnostics = {}
            try:
                diagnostics = _analyze_sql_references(conn, sql)
            except Exception:
                diagnostics = {}
            suggestions = list((diagnostics or {}).get("suggestions") or [])
            if "ORA-00942" in err.upper():
                suggestions.append("ORA-00942 detected: one of referenced schema.table objects does not exist or is not accessible.")
            if "ORA-00904" in err.upper():
                suggestions.append("ORA-00904 detected: invalid identifier. Verify table alias and column spelling.")
            raise HTTPException(
                status_code=500,
                detail={
                    "error": err,
                    "line": line,
                    "column": col,
                    "sql": sql,
                    "diagnostics": diagnostics,
                    "suggestions": suggestions,
                },
            )
        finally:
            if conn is not None and (ds.db_type or "").lower().strip() != "redshift":
                try:
                    conn.disconnect()
                except Exception:
                    pass
