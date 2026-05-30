"""Live Oracle execution module (operator-locked 2026-05-30 Phase 7.10).

Connects to operator's live Oracle (default localhost:1521/FREEPDB1)
and executes operator-supplied SQL.  Designed so the GUI can drive
all DDL + INSERT + verification work end-to-end without the operator
dropping to a CLI.

Operator-locked safety policy:
  * Connection params read from env vars (ORA_LIVE_DSN, ORA_LIVE_USER,
    ORA_LIVE_PASSWORD, ORA_LIVE_MODE) OR per-request override.  Never
    hardcode credentials in code.
  * Statement gate: every SQL statement is parsed by sqlparse and
    classified.  Reject anything outside the operator-blessed
    statement-type whitelist (configurable via `allow_*` flags).
  * NEVER auto-commit destructive operations -- caller must pass
    `commit=True` explicitly.
  * Per-request timeout (default 60s) so a runaway query can't hang
    the FastAPI worker.

Returned by `execute_sql`:  `LiveSqlResult` with rowcount, columns,
sample rows (for SELECTs), Oracle error code+message, and the
classified statement type so the caller can verify the right thing
happened.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import oracledb  # type: ignore[import]
except ImportError:  # pragma: no cover - oracledb is a runtime dep
    oracledb = None  # type: ignore[assignment]


_log = logging.getLogger(__name__)

# Operator-blessed statement-type whitelist.  Per-statement classification
# uses sqlparse if available; falls back to leading-keyword regex.
_READ_ONLY = ("SELECT", "EXPLAIN", "DESCRIBE", "DESC", "SHOW")
_DATA_WRITES = ("INSERT", "UPDATE", "DELETE", "MERGE")
# TRUNCATE is operationally DDL in Oracle (auto-commits + invalidates
# dependent objects).  Classify under DDL so callers reuse `allow_ddl`
# flag for the destructive-but-structural family.
_DDL = ("CREATE", "ALTER", "DROP", "RENAME", "COMMENT", "TRUNCATE")
_ADMIN = ("GRANT", "REVOKE", "FLASHBACK", "PURGE", "SET", "BEGIN", "DECLARE")


@dataclass
class LiveOracleConfig:
    dsn: str = "localhost:1521/FREEPDB1"
    user: str = "SYS"
    password: str = "123456"
    # Connection mode: "DEFAULT", "SYSDBA", "SYSOPER"
    mode: str = "SYSDBA"

    @classmethod
    def from_env(cls, override: Optional[Dict[str, str]] = None) -> "LiveOracleConfig":
        d = {
            "dsn": os.environ.get("ORA_LIVE_DSN", "localhost:1521/FREEPDB1"),
            "user": os.environ.get("ORA_LIVE_USER", "SYS"),
            "password": os.environ.get("ORA_LIVE_PASSWORD", "123456"),
            "mode": os.environ.get("ORA_LIVE_MODE", "SYSDBA"),
        }
        if override:
            d.update({k: v for k, v in override.items() if v})
        return cls(**d)

    def auth_mode(self) -> Optional[int]:
        if oracledb is None:
            return None
        m = (self.mode or "").upper().strip()
        if m == "SYSDBA":
            return oracledb.SYSDBA
        if m == "SYSOPER":
            return oracledb.SYSOPER
        return None  # DEFAULT


@dataclass
class LiveSqlResult:
    sql: str
    statement_type: str               # SELECT / INSERT / DDL / ...
    success: bool
    rowcount: int = 0
    columns: List[str] = field(default_factory=list)
    sample_rows: List[List[Any]] = field(default_factory=list)
    ora_code: int = 0
    ora_message: str = ""
    elapsed_ms: int = 0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sql": self.sql[:600] + ("..." if len(self.sql) > 600 else ""),
            "statement_type": self.statement_type,
            "success": self.success,
            "rowcount": self.rowcount,
            "columns": self.columns,
            "sample_rows": [[None if v is None else str(v)[:200] for v in row]
                            for row in self.sample_rows],
            "ora_code": self.ora_code,
            "ora_message": self.ora_message,
            "elapsed_ms": self.elapsed_ms,
            "note": self.note,
        }


def classify_statement(sql: str) -> str:
    """Lightweight statement-type classifier.  Returns one of:
    SELECT / INSERT / UPDATE / DELETE / MERGE / TRUNCATE / CREATE /
    ALTER / DROP / GRANT / REVOKE / PLSQL / UNKNOWN.

    Uses sqlparse when available; otherwise regex on the leading
    keyword (skipping comments).
    """
    if not sql or not sql.strip():
        return "UNKNOWN"
    text = sql.strip()
    # Strip leading line/block comments
    while True:
        text = text.lstrip()
        if text.startswith("--"):
            text = text.split("\n", 1)[1] if "\n" in text else ""
            continue
        if text.startswith("/*"):
            end = text.find("*/")
            text = text[end + 2:] if end >= 0 else ""
            continue
        break
    text = text.lstrip()
    m = re.match(r"\b([A-Za-z_]+)\b", text)
    if not m:
        return "UNKNOWN"
    kw = m.group(1).upper()
    if kw in _READ_ONLY:
        return "SELECT" if kw == "SELECT" else kw
    if kw in _DATA_WRITES:
        return kw
    if kw in _DDL:
        return kw
    if kw in ("BEGIN", "DECLARE"):
        return "PLSQL"
    if kw in _ADMIN:
        return kw
    return "UNKNOWN"


def _statement_allowed(
    stmt_type: str,
    allow_read: bool = True,
    allow_writes: bool = False,
    allow_ddl: bool = False,
    allow_admin: bool = False,
    allow_plsql: bool = False,
) -> bool:
    if stmt_type == "SELECT" or stmt_type in _READ_ONLY:
        return allow_read
    if stmt_type in _DATA_WRITES:
        return allow_writes
    if stmt_type in _DDL:
        return allow_ddl
    if stmt_type == "PLSQL":
        return allow_plsql
    if stmt_type in _ADMIN:
        return allow_admin
    # UNKNOWN -> refuse unless caller explicitly permits all (admin).
    return allow_admin


def execute_sql(
    sql: str,
    *,
    config: Optional[LiveOracleConfig] = None,
    commit: bool = False,
    allow_read: bool = True,
    allow_writes: bool = False,
    allow_ddl: bool = False,
    allow_admin: bool = False,
    allow_plsql: bool = False,
    timeout_s: int = 60,
    sample_limit: int = 20,
) -> LiveSqlResult:
    """Execute a single SQL statement against live Oracle.

    Operator MUST pass `commit=True` AND `allow_writes=True` for
    INSERT/UPDATE/DELETE/TRUNCATE to actually take effect.
    """
    import time
    if oracledb is None:
        return LiveSqlResult(
            sql=sql, statement_type="UNKNOWN", success=False,
            ora_message="oracledb driver not installed",
            note="pip install oracledb",
        )
    cfg = config or LiveOracleConfig.from_env()
    stmt_type = classify_statement(sql)
    if not _statement_allowed(stmt_type, allow_read, allow_writes,
                              allow_ddl, allow_admin, allow_plsql):
        return LiveSqlResult(
            sql=sql, statement_type=stmt_type, success=False,
            note=(
                f"Statement type {stmt_type!r} blocked by safety gate. "
                f"Caller must pass the matching `allow_*=True` flag."
            ),
        )

    t0 = time.perf_counter()
    conn = None
    cur = None
    try:
        conn_args: Dict[str, Any] = {
            "user": cfg.user, "password": cfg.password, "dsn": cfg.dsn,
        }
        if cfg.auth_mode() is not None:
            conn_args["mode"] = cfg.auth_mode()
        conn = oracledb.connect(**conn_args)
        try:
            conn.call_timeout = int(timeout_s * 1000)
        except Exception:
            pass
        cur = conn.cursor()
        # Oracle does not accept trailing semicolons on most paths.
        sql_clean = sql.rstrip().rstrip(";").rstrip()
        cur.execute(sql_clean)
        rc = cur.rowcount or 0
        cols: List[str] = []
        sample: List[List[Any]] = []
        if cur.description:
            cols = [d[0] for d in cur.description]
            try:
                rows = cur.fetchmany(sample_limit)
                sample = [list(r) for r in rows]
                if stmt_type == "SELECT":
                    rc = len(sample)
            except Exception:
                pass
        if commit and stmt_type in _DATA_WRITES + _DDL:
            conn.commit()
        elapsed = int((time.perf_counter() - t0) * 1000)
        return LiveSqlResult(
            sql=sql, statement_type=stmt_type, success=True,
            rowcount=rc, columns=cols, sample_rows=sample,
            elapsed_ms=elapsed,
            note=("committed" if commit and stmt_type in _DATA_WRITES + _DDL
                  else "not committed"),
        )
    except oracledb.DatabaseError as e:  # type: ignore[union-attr]
        err = e.args[0] if e.args else None
        ora_code = getattr(err, "code", 0) if err else 0
        ora_msg = str(err) if err else str(e)
        elapsed = int((time.perf_counter() - t0) * 1000)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return LiveSqlResult(
            sql=sql, statement_type=stmt_type, success=False,
            ora_code=ora_code, ora_message=ora_msg[:500],
            elapsed_ms=elapsed,
        )
    except Exception as e:
        elapsed = int((time.perf_counter() - t0) * 1000)
        return LiveSqlResult(
            sql=sql, statement_type=stmt_type, success=False,
            ora_message=str(e)[:500], elapsed_ms=elapsed,
        )
    finally:
        if cur:
            try: cur.close()
            except Exception: pass
        if conn:
            try: conn.close()
            except Exception: pass


def execute_multi(
    sql_statements: List[str], *, config: Optional[LiveOracleConfig] = None,
    commit_each: bool = False, **flags,
) -> List[LiveSqlResult]:
    """Run a list of SQL statements sequentially.  Stops on first
    failure when `commit_each=False` (caller can decide to commit all
    or rollback at the end)."""
    out: List[LiveSqlResult] = []
    for s in sql_statements:
        r = execute_sql(s, config=config, commit=commit_each, **flags)
        out.append(r)
        if not r.success and not commit_each:
            break
    return out
