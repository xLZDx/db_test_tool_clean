"""P6 -- Oracle XE Docker harness (OPTIONAL confirmatory only).

Design invariants (operator-locked, from CORRECT_ARCHITECTURE_2026-05-28.md):
  - xe_status in {'confirmed', 'unavailable'}
  - rows_affected from cursor.rowcount  (NOT len(result))
  - rows_affected == 0  ->  verdict = FAIL_ZERO_ROWS
  - XE_UNAVAILABLE must NEVER read as is_pass
  - Never flips STATIC_PASS -> FAIL  (static gate stays authoritative)
  - XE 21c lacks PARALLEL -> PARALLEL hints in SQL stay unexecuted (harmless)

Connection: oracledb thin mode (no Oracle Instant Client required).
  DSN:      env ORA_XE_DSN      (default: "localhost:1521/XE")
  User:     env ORA_XE_USER     (default: "system")
  Password: env ORA_XE_PASSWORD (default: "oracle")

Workflow per run:
  1. Connect.  Failure -> XeRunResult(xe_status='unavailable').
  2. For each source TableRef in the model: CREATE TABLE thin replica + INSERT
     synthetic rows (from KB column metadata).
  3. Execute the emitted INSERT SQL.
  4. Read cursor.rowcount -> rows_affected.
  5. ROLLBACK everything (test-only, never commit).
  6. Return XeRunResult.
"""
from __future__ import annotations

import json
import os
import random
import string
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from app.sql_model.types import ODIModel, TableRef, norm


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENV_DSN = "ORA_XE_DSN"
_ENV_USER = "ORA_XE_USER"
_ENV_PASS = "ORA_XE_PASSWORD"

_DEFAULT_DSN = "localhost:1521/XE"
_DEFAULT_USER = "system"
_DEFAULT_PASS = "oracle"

_DEFAULT_ROWS = 10


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class XeVerdict(Enum):
    CONFIRMED = "confirmed"           # INSERT ran, rows_affected > 0
    FAIL_ZERO_ROWS = "fail_zero_rows" # INSERT ran, rows_affected == 0
    ORA_ERROR = "ora_error"           # ORA-NNNNN during main INSERT
    XE_UNAVAILABLE = "xe_unavailable" # could not connect at all


@dataclass
class XeRunResult:
    """Full result of one XE test run."""
    xe_status: str                             # "confirmed" | "unavailable"
    verdict: XeVerdict
    rows_affected: int = 0
    ora_errors: list[str] = field(default_factory=list)
    synthetic_tables_created: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "xe_status": self.xe_status,
            "verdict": self.verdict.value,
            "rows_affected": self.rows_affected,
            "ora_errors": self.ora_errors,
            "synthetic_tables_created": self.synthetic_tables_created,
            "note": self.note,
            # Invariant: ONLY CONFIRMED is a pass.
            "is_pass": self.verdict == XeVerdict.CONFIRMED,
        }


# ---------------------------------------------------------------------------
# Synthetic data generator
# ---------------------------------------------------------------------------

class SyntheticDataGenerator:
    """Generates typed fake rows for Oracle source tables from KB metadata."""

    def __init__(self, kb_path: Path) -> None:
        self._index: dict[str, list[dict]] = {}  # qualified key -> columns list
        self._table_index: dict[str, list[dict]] = {}  # unqualified -> columns list
        raw: dict = json.loads(kb_path.read_text(encoding="utf-8"))
        for s in raw.get("pdm", {}).get("schemas", []):
            schema = norm(s.get("schema", ""))
            for t in s.get("tables", []):
                table = norm(t.get("name", ""))
                if not table:
                    continue
                cols: list[dict] = t.get("columns", [])
                key = f"{schema}.{table}" if schema else table
                self._index[key] = cols
                if table not in self._table_index:
                    self._table_index[table] = cols

    def get_columns(self, ref: TableRef) -> list[dict]:
        key = f"{ref.schema}.{ref.table}" if ref.schema else ref.table
        cols = self._index.get(key)
        if cols is None:
            cols = self._table_index.get(ref.table, [])
        return cols

    @staticmethod
    def _fake_value(data_type: str, col_name: str, row_idx: int) -> str:
        """Return an Oracle SQL literal for the given column type."""
        dt = (data_type or "VARCHAR2").upper()
        if dt in ("NUMBER", "INTEGER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE"):
            return str(row_idx + 1)
        if dt.startswith("DATE"):
            year = 2020 + (row_idx % 5)
            month = (row_idx % 12) + 1
            return f"DATE '{year:04d}-{month:02d}-01'"
        if dt.startswith("TIMESTAMP"):
            year = 2020 + (row_idx % 5)
            month = (row_idx % 12) + 1
            return f"TIMESTAMP '{year:04d}-{month:02d}-01 00:00:00'"
        if dt.startswith("CHAR"):
            return f"'C{row_idx:03d}'"
        # VARCHAR2 / NVARCHAR2 / CLOB and anything else
        suffix = "".join(random.choices(string.ascii_uppercase, k=4))
        prefix = (col_name[:6].upper() if col_name else "COL")
        return f"'T_{prefix}_{suffix}'"

    @staticmethod
    def _oracle_ddl_type(data_type: str) -> str:
        """Map KB data_type to a simple Oracle XE-compatible DDL type."""
        dt = (data_type or "VARCHAR2").upper()
        if dt in ("NUMBER", "INTEGER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE"):
            return "NUMBER"
        if dt.startswith("DATE"):
            return "DATE"
        if dt.startswith("TIMESTAMP"):
            return "TIMESTAMP"
        if dt in ("CLOB", "NCLOB", "BLOB"):
            return "CLOB"
        # VARCHAR2, NVARCHAR2, CHAR, and everything else -> VARCHAR2(1000)
        return "VARCHAR2(1000)"

    def create_table_sql(self, ref: TableRef, scratch_schema: str = "") -> str:
        """Return a CREATE TABLE DDL (thin replica, no constraints)."""
        cols = self.get_columns(ref)
        if not cols:
            return ""
        target_name = f"{scratch_schema}.{ref.table}" if scratch_schema else ref.fq
        col_defs: list[str] = []
        for c in cols:
            cname = norm(c.get("name", ""))
            if not cname:
                continue
            ddl_type = self._oracle_ddl_type(c.get("data_type", "VARCHAR2"))
            col_defs.append(f"  {cname} {ddl_type}")
        if not col_defs:
            return ""
        return "CREATE TABLE " + target_name + " (\n" + ",\n".join(col_defs) + "\n)"

    def insert_rows_sql(
        self, ref: TableRef, scratch_schema: str = "", n: int = _DEFAULT_ROWS
    ) -> list[str]:
        """Return N INSERT statements with synthetic data."""
        cols = self.get_columns(ref)
        if not cols:
            return []
        target_name = f"{scratch_schema}.{ref.table}" if scratch_schema else ref.fq
        named_cols = [c for c in cols if norm(c.get("name", ""))]
        if not named_cols:
            return []
        col_names = [norm(c["name"]) for c in named_cols]
        stmts: list[str] = []
        for i in range(n):
            vals = [
                self._fake_value(c.get("data_type", "VARCHAR2"), norm(c.get("name", "")), i)
                for c in named_cols
            ]
            stmts.append(
                f"INSERT INTO {target_name} ({', '.join(col_names)}) "
                f"VALUES ({', '.join(vals)})"
            )
        return stmts


# ---------------------------------------------------------------------------
# XE executor
# ---------------------------------------------------------------------------

def _conn_params() -> dict:
    return {
        "dsn": os.environ.get(_ENV_DSN, _DEFAULT_DSN),
        "user": os.environ.get(_ENV_USER, _DEFAULT_USER),
        "password": os.environ.get(_ENV_PASS, _DEFAULT_PASS),
    }


def run_insert_on_xe(
    model: ODIModel,
    emit_sql: str,
    kb_path: Path,
    test_rows: int = _DEFAULT_ROWS,
    scratch_schema: str = "",
) -> XeRunResult:
    """Run the emitted INSERT SQL against a local Oracle XE instance.

    Returns XeRunResult.  Never raises.
    """
    try:
        import oracledb
    except ImportError:
        return XeRunResult(
            xe_status="unavailable",
            verdict=XeVerdict.XE_UNAVAILABLE,
            note="oracledb not installed",
        )

    params = _conn_params()
    try:
        conn = oracledb.connect(
            user=params["user"],
            password=params["password"],
            dsn=params["dsn"],
        )
    except Exception as exc:
        return XeRunResult(
            xe_status="unavailable",
            verdict=XeVerdict.XE_UNAVAILABLE,
            note=f"Connection failed: {exc}",
        )

    gen = SyntheticDataGenerator(kb_path)
    created: list[str] = []
    ora_errors: list[str] = []

    try:
        with conn.cursor() as cur:
            # Collect unique source tables from all staging steps
            source_refs: list[TableRef] = []
            seen: set[str] = set()
            for step in model.staging_steps:
                for binding in step.source_bindings:
                    key = binding.ref.fq
                    if key not in seen:
                        seen.add(key)
                        source_refs.append(binding.ref)
            # Also create the target table (needed for the INSERT INTO)
            target_key = model.target.fq
            if target_key not in seen:
                seen.add(target_key)
                source_refs.append(model.target)

            # CREATE thin replicas (ORA-00955 "already exists" is OK)
            for ref in source_refs:
                ddl = gen.create_table_sql(ref, scratch_schema)
                if not ddl:
                    continue
                try:
                    cur.execute(ddl)
                    created.append(ref.fq)
                except Exception as exc:
                    msg = str(exc)
                    if "ORA-00955" not in msg:
                        ora_errors.append(f"CREATE {ref.fq}: {msg}")

            # INSERT synthetic rows into all source tables (not the target)
            for ref in source_refs:
                if ref.fq == model.target.fq:
                    continue
                for stmt in gen.insert_rows_sql(ref, scratch_schema, test_rows):
                    try:
                        cur.execute(stmt)
                    except Exception as exc:
                        ora_errors.append(f"INSERT {ref.fq}: {exc}")
                        break  # stop rows for this table, continue to next

            # Execute the main INSERT SQL
            rows_affected = 0
            try:
                cur.execute(emit_sql)
                rows_affected = cur.rowcount  # from DML cursor, NOT len(result)
            except Exception as exc:
                ora_errors.append(f"MAIN INSERT: {exc}")

            # Always ROLLBACK -- this is a test run, never persist
            conn.rollback()

    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Determine verdict (connection succeeded -- xe_status = "confirmed")
    main_insert_failed = any(e.startswith("MAIN INSERT:") for e in ora_errors)
    if main_insert_failed:
        verdict = XeVerdict.ORA_ERROR
    elif rows_affected == 0:
        verdict = XeVerdict.FAIL_ZERO_ROWS
    else:
        verdict = XeVerdict.CONFIRMED

    return XeRunResult(
        xe_status="confirmed",   # we connected; result detail is in verdict
        verdict=verdict,
        rows_affected=rows_affected,
        ora_errors=ora_errors,
        synthetic_tables_created=created,
        note=(
            f"{len(created)} table(s) created; "
            f"{test_rows} synthetic row(s) per source table"
        ),
    )


# ---------------------------------------------------------------------------
# DRD-based XE runner (no ODIModel needed)
# ---------------------------------------------------------------------------

@dataclass
class DrdXeRunResult:
    """Result of running DRD-generated scripts against Oracle XE."""
    xe_status: str                             # "confirmed" | "unavailable"
    verdict: XeVerdict
    rows_inserted: int = 0
    validation_results: list = field(default_factory=list)
    ora_errors: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "xe_status": self.xe_status,
            "verdict": self.verdict.value,
            "rows_inserted": self.rows_inserted,
            "validation_results": self.validation_results,
            "ora_errors": self.ora_errors,
            "note": self.note,
            "is_pass": self.verdict == XeVerdict.CONFIRMED,
        }


def run_drd_scripts_on_xe(
    create_table_sql: str,
    insert_statements: list[str],
    validation_sqls: list[dict],
) -> DrdXeRunResult:
    """Run DRD-generated CREATE/INSERT/validation scripts against Oracle XE.

    Design invariants (operator-locked):
    - xe_status in {'confirmed', 'unavailable'}
    - rows_inserted from cursor.rowcount per INSERT (NOT len(result))
    - rows_inserted == 0 -> verdict = FAIL_ZERO_ROWS
    - XE_UNAVAILABLE never reads as is_pass
    - Always ROLLBACK -- test only, never commit
    """
    try:
        import oracledb
    except ImportError:
        return DrdXeRunResult(
            xe_status="unavailable",
            verdict=XeVerdict.XE_UNAVAILABLE,
            note="oracledb not installed",
        )

    params = _conn_params()
    try:
        conn = oracledb.connect(
            user=params["user"],
            password=params["password"],
            dsn=params["dsn"],
        )
    except Exception as exc:
        return DrdXeRunResult(
            xe_status="unavailable",
            verdict=XeVerdict.XE_UNAVAILABLE,
            note=f"Connection failed: {exc}",
        )

    ora_errors: list[str] = []
    rows_inserted = 0
    validation_results: list[dict] = []

    try:
        with conn.cursor() as cur:
            # CREATE target table (ignore ORA-00955 = already exists)
            if create_table_sql:
                try:
                    cur.execute(create_table_sql)
                except Exception as exc:
                    msg = str(exc)
                    if "ORA-00955" not in msg:
                        ora_errors.append(f"CREATE TABLE: {msg}")

            # INSERT synthetic rows
            for stmt in insert_statements:
                stmt = stmt.rstrip(";")
                if not stmt.strip():
                    continue
                try:
                    cur.execute(stmt)
                    rows_inserted += cur.rowcount  # operator-locked: rowcount only
                except Exception as exc:
                    ora_errors.append(f"INSERT: {exc}")

            # Run validation queries
            for vq in validation_sqls:
                label = vq.get("label", "")
                sql = vq.get("sql", "").rstrip(";").strip()
                if not sql:
                    continue
                try:
                    cur.execute(sql)
                    row = cur.fetchone()
                    result_val = row[0] if row else None
                    validation_results.append({
                        "label": label,
                        "result": result_val,
                        "status": "ok",
                    })
                except Exception as exc:
                    ora_errors.append(f"VALIDATE {label}: {exc}")
                    validation_results.append({
                        "label": label,
                        "result": None,
                        "status": "error",
                        "error": str(exc),
                    })

            # Always ROLLBACK -- test only, never persist
            conn.rollback()

    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Verdict
    main_failed = any(e.startswith("INSERT:") for e in ora_errors)
    if main_failed:
        verdict = XeVerdict.ORA_ERROR
    elif rows_inserted == 0:
        verdict = XeVerdict.FAIL_ZERO_ROWS
    else:
        verdict = XeVerdict.CONFIRMED

    return DrdXeRunResult(
        xe_status="confirmed",
        verdict=verdict,
        rows_inserted=rows_inserted,
        validation_results=validation_results,
        ora_errors=ora_errors,
        note=f"{rows_inserted} row(s) inserted; {len(validation_results)} validation(s) run",
    )
