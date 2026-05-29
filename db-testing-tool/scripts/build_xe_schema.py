"""Generic Oracle XE bootstrap + synthetic seed.

Drives ANY DRD + PDM combo -- not specific to AVY_FACT_SIDE.  Walks
``data/local_kb/schema_kb_ds_*.json`` to discover every source schema /
table / column the DRD references, creates a thin replica in Oracle XE
under those exact owner names (so SQL emitted by the generator runs
unchanged), and seeds synthetic rows per dtype.

Workflow:
  1. `python scripts/build_xe_schema.py --ds 3 --rows 100`
     reads PDM datasource id=3, creates every owner/table, seeds 100 rows.
  2. The emitter's generated INSERT (CASE WHEN ... etc) can then run
     against the live XE; xe_harness.run_insert produces real rowcount.

Operator-locked invariants (2026-05-29):
  * 100% generic -- no hardcoded business / table / column names.
  * Idempotent -- safe to re-run; drops + recreates owned by DBTOOL.
  * Synthetic rows respect dtype + nullability + length constraints.
  * Never commits against any real Oracle DB; only the local XE container.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import string
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("build_xe_schema")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Default connection -- align with infra/oracle_xe/docker-compose.yml
DEFAULT_DSN = os.environ.get("XE_DSN", "localhost:1521/XEPDB1")
DEFAULT_USER = os.environ.get("XE_USER", "DBTOOL")
DEFAULT_PASS = os.environ.get("XE_PASSWORD", "dbtool")
DEFAULT_SYSTEM_USER = os.environ.get("XE_SYSTEM_USER", "system")
DEFAULT_SYSTEM_PASS = os.environ.get("XE_SYSTEM_PASSWORD", "oracle")


# ── PDM walker ────────────────────────────────────────────────────────────────

@dataclass
class ColumnDef:
    name: str
    data_type: str           # raw PDM type (VARCHAR2, NUMBER, DATE, TIMESTAMP)
    length: Optional[int]
    nullable: bool


@dataclass
class TableDef:
    schema: str
    name: str
    columns: List[ColumnDef]

    @property
    def fq(self) -> str:
        return f"{self.schema}.{self.name}"


def load_pdm(ds_id: int) -> List[TableDef]:
    """Walk ``data/local_kb/schema_kb_ds_<ds_id>.json`` and yield every table
    found.  Generic shape: ``sources[].pdm.schemas[].tables[].columns[]``.
    """
    p = ROOT / "data" / "local_kb" / f"schema_kb_ds_{ds_id}.json"
    if not p.exists():
        raise FileNotFoundError(p)
    payload = json.loads(p.read_text(encoding="utf-8"))
    tables: List[TableDef] = []
    for src in payload.get("sources", []):
        pdm = (src or {}).get("pdm") or {}
        for schema_block in pdm.get("schemas", []) or []:
            schema_name = (schema_block.get("schema") or "").strip().upper()
            for tbl in schema_block.get("tables", []) or []:
                tname = (tbl.get("name") or "").strip().upper()
                if not schema_name or not tname:
                    continue
                cols: List[ColumnDef] = []
                for c in tbl.get("columns", []) or []:
                    name = (c.get("name") or "").strip().upper()
                    dtype = (c.get("data_type") or "VARCHAR2").strip().upper()
                    length = c.get("data_length") or c.get("length")
                    try:
                        length = int(length) if length is not None else None
                    except Exception:
                        length = None
                    nullable = bool(c.get("nullable", True))
                    if name:
                        cols.append(ColumnDef(name, dtype, length, nullable))
                if cols:
                    tables.append(TableDef(schema_name, tname, cols))
    return tables


# ── DDL composition (generic) ─────────────────────────────────────────────────

def _format_oracle_dtype(name: str, dtype: str, length: Optional[int]) -> str:
    dt = dtype.upper().strip()
    if dt.startswith("VARCHAR2"):
        n = int(length) if length else 4000
        return f"VARCHAR2({min(n, 4000)})"
    if dt.startswith("NVARCHAR2"):
        n = int(length) if length else 2000
        return f"NVARCHAR2({min(n, 2000)})"
    if dt.startswith("CHAR"):
        n = int(length) if length else 1
        return f"CHAR({n})"
    if dt == "NUMBER":
        return "NUMBER"
    if dt.startswith("TIMESTAMP"):
        return dt
    if dt == "DATE":
        return "DATE"
    if dt == "CLOB":
        return "CLOB"
    return dt


def make_create_table(t: TableDef) -> str:
    cols_sql = []
    for c in t.columns:
        nullable = "NULL" if c.nullable else "NOT NULL"
        cols_sql.append(f"  {c.name} {_format_oracle_dtype(c.name, c.data_type, c.length)} {nullable}")
    return f"CREATE TABLE {t.fq} (\n" + ",\n".join(cols_sql) + "\n)"


def make_create_schema_user(schema: str, pwd: str = "thin") -> List[str]:
    """Each ``schema`` becomes an Oracle user.  Idempotent: ignore "already exists"."""
    return [
        f"CREATE USER {schema} IDENTIFIED BY {pwd} DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS",
        f"GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW TO {schema}",
    ]


# ── Synthetic row generator (generic dtype-driven) ───────────────────────────

def _rand_value(c: ColumnDef, row_idx: int) -> Any:
    dt = c.data_type.upper()
    if dt.startswith("VARCHAR2") or dt.startswith("NVARCHAR2") or dt.startswith("CHAR"):
        n = min(c.length or 8, 32)
        # Deterministic, readable seed per (col, row)
        seed = f"{c.name}_{row_idx}"
        return seed[:n]
    if dt == "NUMBER":
        return row_idx
    if dt.startswith("TIMESTAMP") or dt == "DATE":
        # use SYSDATE-N offset; let Oracle compute
        return None  # will be SYSDATE-? in SQL
    if dt == "CLOB":
        return f"clob_{row_idx}"
    return None


def make_insert_sql(t: TableDef, rows: int) -> List[Tuple[str, Dict[str, Any]]]:
    """Return list of (sql, binds) pairs.  Generic: each row uses bind vars
    named ``:c0, :c1, ...``  Skips date / timestamp via SYSDATE-N inline."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    col_names = [c.name for c in t.columns]
    for i in range(1, rows + 1):
        binds: Dict[str, Any] = {}
        value_exprs: List[str] = []
        for j, c in enumerate(t.columns):
            v = _rand_value(c, i)
            if c.data_type.startswith("TIMESTAMP") or c.data_type == "DATE":
                value_exprs.append(f"SYSDATE - {i % 30}")
            elif v is None and not c.nullable:
                value_exprs.append(_safe_not_null_literal(c))
            else:
                binds[f"c{j}"] = v
                value_exprs.append(f":c{j}")
        sql = (
            f"INSERT INTO {t.fq} (" + ", ".join(col_names) + ") "
            f"VALUES (" + ", ".join(value_exprs) + ")"
        )
        out.append((sql, binds))
    return out


def _safe_not_null_literal(c: ColumnDef) -> str:
    dt = c.data_type.upper()
    if dt.startswith("VARCHAR2") or dt.startswith("NVARCHAR2") or dt.startswith("CHAR"):
        return "'X'"
    if dt == "NUMBER":
        return "0"
    if dt.startswith("TIMESTAMP") or dt == "DATE":
        return "SYSDATE"
    return "NULL"


# ── Live execution against Oracle XE ──────────────────────────────────────────

def _connect_as_system(dsn: str) -> Any:
    import oracledb
    return oracledb.connect(user=DEFAULT_SYSTEM_USER, password=DEFAULT_SYSTEM_PASS, dsn=dsn)


def _connect_as_dbtool(dsn: str) -> Any:
    import oracledb
    return oracledb.connect(user=DEFAULT_USER, password=DEFAULT_PASS, dsn=dsn)


def ensure_schema_users(tables: List[TableDef], dsn: str) -> None:
    """Create one Oracle user per owner referenced by any table.  Idempotent."""
    owners = sorted({t.schema for t in tables if t.schema})
    if not owners:
        return
    conn = _connect_as_system(dsn)
    cur = conn.cursor()
    for owner in owners:
        for ddl in make_create_schema_user(owner):
            try:
                cur.execute(ddl)
                logger.info("OK: %s", ddl)
            except Exception as exc:
                msg = str(exc)
                if "ORA-01920" in msg or "ORA-01921" in msg or "already exists" in msg:
                    logger.info("skip (exists): %s", ddl.split(" IDENTIFIED")[0])
                    continue
                logger.warning("DDL failed: %s -- %s", ddl[:60], exc)
    conn.commit()
    cur.close()
    conn.close()


def create_tables(tables: List[TableDef], dsn: str, drop_first: bool = True) -> None:
    """Create every table under its actual schema.  Connects AS each owner
    via PROXY_USER (DBTOOL has CREATE ANY TABLE / DROP ANY TABLE)."""
    conn = _connect_as_dbtool(dsn)
    cur = conn.cursor()
    for t in tables:
        if drop_first:
            try:
                cur.execute(f"DROP TABLE {t.fq} CASCADE CONSTRAINTS PURGE")
                logger.info("dropped %s", t.fq)
            except Exception as exc:
                if "ORA-00942" in str(exc):
                    pass  # table didn't exist
                else:
                    logger.warning("drop %s failed: %s", t.fq, exc)
        ddl = make_create_table(t)
        try:
            cur.execute(ddl)
            logger.info("created %s (%d cols)", t.fq, len(t.columns))
        except Exception as exc:
            logger.warning("create %s failed: %s", t.fq, exc)
    conn.commit()
    cur.close()
    conn.close()


def seed_rows(tables: List[TableDef], rows: int, dsn: str) -> None:
    conn = _connect_as_dbtool(dsn)
    cur = conn.cursor()
    for t in tables:
        try:
            for sql, binds in make_insert_sql(t, rows):
                cur.execute(sql, binds)
            logger.info("seeded %d rows into %s", rows, t.fq)
        except Exception as exc:
            logger.warning("seed %s failed: %s", t.fq, exc)
            conn.rollback()
            continue
    conn.commit()
    cur.close()
    conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ds", type=int, default=3, help="Datasource id in PDM JSON")
    ap.add_argument("--rows", type=int, default=50, help="Synthetic rows per table")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--no-drop", action="store_true", help="Do not drop existing tables")
    ap.add_argument("--owners-only", nargs="*", help="Subset owner names")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    tables = load_pdm(args.ds)
    if args.owners_only:
        wanted = {o.upper() for o in args.owners_only}
        tables = [t for t in tables if t.schema.upper() in wanted]
    logger.info("PDM datasource %d -> %d tables", args.ds, len(tables))
    if not tables:
        logger.warning("no tables found in PDM")
        return 1

    ensure_schema_users(tables, args.dsn)
    create_tables(tables, args.dsn, drop_first=not args.no_drop)
    seed_rows(tables, args.rows, args.dsn)

    logger.info("Done.  Connect with: oracledb dsn=%s user=DBTOOL password=dbtool", args.dsn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
