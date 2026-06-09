#!/usr/bin/env python3
"""Gate V6: complete the FREEPDB1 LH mirror from schema_kb_ds_3.json (create-missing).

Builds a STRUCTURAL mirror: every KB object that is absent from the live DB is
created as a TABLE with the KB's columns (no FK/PK/NOT-NULL -- columns only, so
loads never fail and EXPLAIN PLAN resolves names/types). Views in the KB are
mirrored as tables (a table with the right columns resolves identically in
EXPLAIN PLAN; the existing mirror already stores *_V views as TABLEs).

Default --mode dry-run: READ-ONLY. Diffs the KB against all_users/all_objects,
reports what is missing, writes a DDL preview .sql + md/csv summary. No writes.

--mode execute: runs the DDL. REQUIRES IKOROSTELEV to be a DBA -- run the V6.0
bootstrap first (as SYS):
    GRANT DBA TO IKOROSTELEV;
    GRANT UNLIMITED TABLESPACE TO IKOROSTELEV;
Refuses to execute (rc=2) if the connected user lacks CREATE ANY TABLE.

Usage:
  python -m tools.rebuild_freepdb1_from_kb                 # dry-run, ds 2
  python -m tools.rebuild_freepdb1_from_kb --mode execute  # after DBA bootstrap
  python -m tools.rebuild_freepdb1_from_kb --mode execute --limit 50  # staged
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
from pathlib import Path

from sqlalchemy import text

from app.database import sync_engine
from app.connectors.factory import get_connector
from app.models.datasource import DataSource

_REPO = Path(__file__).resolve().parents[1]
_KB = _REPO / "data" / "local_kb" / "schema_kb_ds_3.json"
_DEFAULT_TS = "LOADER_TS"

# Oracle types that CREATE TABLE cannot take verbatim, or that lack length info
# in the KB -> map to safe, resolvable defaults (structural mirror only).
_TYPE_MAP = {
    "NUMBER": "NUMBER", "FLOAT": "FLOAT", "DATE": "DATE",
    "VARCHAR2": "VARCHAR2(4000)", "VARCHAR": "VARCHAR2(4000)",
    "NVARCHAR2": "NVARCHAR2(2000)", "CHAR": "CHAR(255)", "NCHAR": "NCHAR(255)",
    "CLOB": "CLOB", "NCLOB": "NCLOB", "BLOB": "BLOB", "RAW": "RAW(2000)",
    "BINARY_FLOAT": "BINARY_FLOAT", "BINARY_DOUBLE": "BINARY_DOUBLE",
}


def oracle_type(dt: str) -> str:
    dt = (dt or "").upper().strip()
    if dt.startswith("TIMESTAMP"):  # keeps precision + WITH TIME ZONE
        return dt
    return _TYPE_MAP.get(dt, "VARCHAR2(4000)")  # internal/UNDEFINED/ANYDATA/AQ$/ROWID/LONG -> safe


def ddl_for(schema: str, table: str, columns: list) -> str:
    cols, seen = [], set()
    for c in sorted(columns, key=lambda x: x.get("ordinal_position", 0)):
        nm = (c.get("name") or "").strip().upper()
        if not nm or nm in seen:
            continue
        seen.add(nm)
        cols.append(f'  "{nm}" {oracle_type(c.get("data_type"))}')
    body = ",\n".join(cols) if cols else '  "DUMMY_COL" VARCHAR2(1)'
    return f'CREATE TABLE "{schema}"."{table}" (\n{body}\n)'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ds", type=int, default=2, help="datasource id (default 2 = FREEPDB1_LOCAL)")
    ap.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run")
    ap.add_argument("--limit", type=int, default=0, help="cap tables created in execute (0=all; for staged runs)")
    args = ap.parse_args()

    kb = json.loads(_KB.read_text(encoding="utf-8"))
    kb_objs, kb_schemas = {}, set()
    for s in kb.get("pdm", {}).get("schemas", []):
        sch = (s.get("schema") or "").strip().upper()
        if not sch:
            continue
        kb_schemas.add(sch)
        for t in s.get("tables", []):
            nm = (t.get("name") or "").strip().upper()
            if nm:
                kb_objs[(sch, nm)] = t.get("columns", [])

    with sync_engine.begin() as c:
        row = c.execute(text("SELECT * FROM datasources WHERE id=:i"), {"i": args.ds}).fetchone()
    if row is None:
        raise SystemExit(f"datasource id={args.ds} not found")
    ds = DataSource()
    for k, v in row._mapping.items():
        setattr(ds, k, v)
    conn = get_connector(ds)
    raw = conn._direct_connect()
    cur = raw.cursor()
    print(f"Connected ds={args.ds} as {conn.username}")

    cur.execute("SELECT username FROM all_users")
    existing_users = {r[0].upper() for r in cur.fetchall()}
    cur.execute("SELECT owner, object_name FROM all_objects WHERE object_type IN ('TABLE','VIEW')")
    existing_objs = {(r[0].upper(), r[1].upper()) for r in cur.fetchall()}
    cur.execute("SELECT privilege FROM session_privs")
    privs = {r[0] for r in cur.fetchall()}

    missing_schemas = sorted(kb_schemas - existing_users)
    missing_objs = sorted(k for k in kb_objs if k not in existing_objs)
    is_dba = "CREATE ANY TABLE" in privs

    print(f"KB: schemas={len(kb_schemas)} objects={len(kb_objs)}")
    print(f"Missing schemas (users): {len(missing_schemas)}")
    print(f"Missing objects (tables/views): {len(missing_objs)}")
    print(f"IKOROSTELEV is DBA (CREATE ANY TABLE): {is_dba}")

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sqlp = _REPO / "data" / f"v6_create_missing_{ts}.sql"
    sql_lines = []
    for sch in missing_schemas:
        sql_lines.append(f'CREATE USER "{sch}" IDENTIFIED BY "Mirror_{sch[:8]}_1" '
                         f'DEFAULT TABLESPACE {_DEFAULT_TS} QUOTA UNLIMITED ON {_DEFAULT_TS};')
    for (sch, nm) in missing_objs:
        sql_lines.append(ddl_for(sch, nm, kb_objs[(sch, nm)]) + ";")
    sqlp.write_text("\n".join(sql_lines) + "\n", encoding="utf-8")
    print(f"DDL preview ({len(sql_lines)} stmts): {sqlp}")

    avy = ("SSDS_DAL_OWNER", "ENTERPRISE_ENTITY_RISK_DIMENSION_V")
    if avy in set(missing_objs):
        print("\n--- DDL for the AVY-blocking view (mirrored as table) ---")
        print(ddl_for(*avy, kb_objs[avy]))

    created_u = created_t = 0
    failed = []
    if args.mode == "execute":
        if not is_dba:
            print("\nEXECUTE refused: connected user lacks DBA (CREATE ANY TABLE).")
            print("Run V6.0 bootstrap as SYS:")
            print("  GRANT DBA TO IKOROSTELEV;")
            print("  GRANT UNLIMITED TABLESPACE TO IKOROSTELEV;")
            cur.close(); raw.close()
            return 2
        for sch in missing_schemas:
            try:
                cur.execute(f'CREATE USER "{sch}" IDENTIFIED BY "Mirror_{sch[:8]}_1" '
                            f'DEFAULT TABLESPACE {_DEFAULT_TS} QUOTA UNLIMITED ON {_DEFAULT_TS}')
                raw.commit(); created_u += 1
            except Exception as e:  # noqa: BLE001
                failed.append(("USER", sch, str(e).splitlines()[0]))
        todo = missing_objs[:args.limit] if args.limit else missing_objs
        for (sch, nm) in todo:
            try:
                cur.execute(ddl_for(sch, nm, kb_objs[(sch, nm)]))
                raw.commit(); created_t += 1
            except Exception as e:  # noqa: BLE001
                failed.append(("TABLE", f"{sch}.{nm}", str(e).splitlines()[0]))
        print(f"\nExecuted: users_created={created_u} tables_created={created_t} failed={len(failed)}")
        for f in failed[:25]:
            print("  FAIL", f)

    cur.close(); raw.close()

    # md + csv twin summary
    md = _REPO / "data" / f"v6_rebuild_summary_{ts}.md"
    csvp = _REPO / "data" / f"v6_rebuild_summary_{ts}.csv"
    md.write_text(
        f"# V6 KB-mirror create-missing -- {ts}\n"
        f"mode={args.mode} ds={args.ds} dba={is_dba}\n\n"
        f"- KB schemas: {len(kb_schemas)} ; KB objects: {len(kb_objs)}\n"
        f"- Missing schemas: {len(missing_schemas)}\n"
        f"- Missing objects: {len(missing_objs)}\n"
        f"- Executed: users_created={created_u} tables_created={created_t} failed={len(failed)}\n"
        f"- DDL preview: {sqlp.name}\n", encoding="utf-8")
    with csvp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", "value"])
        for k, v in [("mode", args.mode), ("kb_schemas", len(kb_schemas)), ("kb_objects", len(kb_objs)),
                     ("missing_schemas", len(missing_schemas)), ("missing_objects", len(missing_objs)),
                     ("users_created", created_u), ("tables_created", created_t), ("failed", len(failed))]:
            w.writerow([k, v])
    print(f"Summary: {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
