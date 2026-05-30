"""Regenerate the schema KB / PDM from live FREEPDB1 (operator's loaded
test DB).  Writes `data/local_kb/schema_kb_ds_99.json` in the same
shape as the existing PDM files so the comparator + emitter pick it
up via the standard `load_schema_kb_payload(99)` path.

Operator-locked rationale (2026-05-30 Phase 7.14):

  The existing `schema_kb_ds_3.json` (111 MB) was generated from a
  PRODUCTION Oracle (117 schemas, all views + objects).  Operator's
  Phase 3 loader populated FREEPDB1 with a 108-schema test subset --
  views and 6 FK-uniqueness-fail schemas didn't load.

  The emitter's PDM validation (Phase 7.13) uses the production PDM,
  which DISAGREES with the live FREEPDB1.  That mismatch surfaces as
  ORA-00942 (table missing) and ORA-00904 (column missing) at execute
  time, even though the PDM says everything exists.

  Regenerating the PDM from live FREEPDB1 eliminates the drift: the
  emitter's validation now matches reality.  JOINs that would fail at
  runtime are honestly downgraded to CROSS JOIN + TODO at emit time.

  Schema shape matches build_pdm_catalog() in
  app/services/schema_kb_service.py so the existing loader + comparator
  work without modification.

Run:
    python scripts/regen_pdm_from_freepdb1.py

Output:
    data/local_kb/schema_kb_ds_99.json  (one row per (schema, table))
    Counts printed at the end.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB_PATH = ROOT / "data" / "local_kb" / "schema_kb_ds_99.json"

# Datasource ID for the regenerated KB.  Picked 99 to avoid colliding
# with the registered DS 1 (production PDM, LFS-pointer-broken) and
# the on-disk DS 3 (production PDM with real data).
DS_ID = 99
DSN = "localhost:1521/FREEPDB1"
USER = "sys"
PASSWORD = "123456"

# Schemas we DON'T need (Oracle system + tooling).
EXCLUDED_SCHEMAS = {
    "SYS", "SYSTEM", "AUDSYS", "OUTLN", "GSMADMIN_INTERNAL",
    "XDB", "WMSYS", "DBSNMP", "DBSFWUSER", "REMOTE_SCHEDULER_AGENT",
    "ORDPLUGINS", "ORDSYS", "ORDDATA", "MDSYS", "OLAPSYS", "CTXSYS",
    "MDDATA", "APPQOSSYS", "OJVMSYS", "ANONYMOUS", "GGSYS",
    "APEX_PUBLIC_USER", "FLOWS_FILES", "ORACLE_OCM", "SI_INFORMTN_SCHEMA",
    "PUBLIC", "PDBADMIN", "REMOTE_SCHEDULER_AGENT", "LBACSYS",
    "DVSYS", "DVF",
}


def _fetch_all(cur, sql, **bind):
    cur.execute(sql, **bind)
    return cur.fetchall()


def main() -> int:
    import oracledb
    print("=== Regen PDM from live FREEPDB1 ===\n")
    t0 = time.perf_counter()
    conn = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN, mode=oracledb.SYSDBA)
    cur = conn.cursor()

    # 1. Discover user-defined schemas (exclude Oracle internals).
    print("1) Discover schemas...")
    rows = _fetch_all(cur, """
        SELECT username FROM dba_users
        WHERE oracle_maintained='N'
        ORDER BY username
    """)
    all_schemas = [r[0] for r in rows if r[0] not in EXCLUDED_SCHEMAS]
    print(f"   {len(all_schemas)} user schemas (after excluding internals)")

    # 2. For each schema: list all tables + views.  Bulk-query columns
    #    + PKs + FKs at the schema level (one round-trip per schema)
    #    so the regen finishes in a reasonable time even on the 4k-
    #    table FREEPDB1.
    schemas_payload = []
    relationships = []
    total_tables = 0
    for i, schema in enumerate(all_schemas, 1):
        rows = _fetch_all(cur, f"""
            SELECT object_name, object_type
              FROM dba_objects
             WHERE owner=:o AND object_type IN ('TABLE','VIEW','MATERIALIZED VIEW')
             ORDER BY object_name
        """, o=schema)
        tables_meta = [(r[0], r[1]) for r in rows]
        if not tables_meta:
            continue
        # Bulk columns for the schema
        cols_rows = _fetch_all(cur, """
            SELECT table_name, column_name, data_type, nullable,
                   column_id, data_length, data_precision, data_scale
              FROM dba_tab_columns
             WHERE owner=:o
             ORDER BY table_name, column_id
        """, o=schema)
        by_table_cols: dict = {}
        for r in cols_rows:
            tbl, name, dtype, null, ord_, dlen, dprec, dscale = r
            # Construct display data_type (NUMBER(p,s), VARCHAR2(n) etc.)
            disp = dtype
            if dtype in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR") and dlen:
                disp = f"{dtype}({dlen})"
            elif dtype == "NUMBER" and dprec is not None:
                disp = f"NUMBER({dprec},{dscale or 0})"
            by_table_cols.setdefault(tbl, []).append({
                "name": name,
                "data_type": disp,
                "nullable": (null == "Y"),
                "is_pk": False,  # filled below from PK constraints
                "ordinal_position": ord_,
            })
        # Bulk PKs
        pk_rows = _fetch_all(cur, """
            SELECT cc.table_name, cc.column_name
              FROM dba_constraints c
              JOIN dba_cons_columns cc
                ON c.owner=cc.owner
               AND c.constraint_name=cc.constraint_name
             WHERE c.owner=:o AND c.constraint_type='P'
             ORDER BY cc.table_name, cc.position
        """, o=schema)
        pk_by_table: dict = {}
        for tbl, col in pk_rows:
            pk_by_table.setdefault(tbl, []).append(col)
        # Mark is_pk
        for tbl, cols in by_table_cols.items():
            pks = set(pk_by_table.get(tbl, []))
            for c in cols:
                if c["name"] in pks:
                    c["is_pk"] = True
        # Bulk FKs
        fk_rows = _fetch_all(cur, """
            SELECT cc.table_name, cc.column_name, c.constraint_name,
                   r.owner AS ref_owner, rcc.table_name AS ref_table,
                   rcc.column_name AS ref_col
              FROM dba_constraints c
              JOIN dba_cons_columns cc
                ON c.owner=cc.owner AND c.constraint_name=cc.constraint_name
              JOIN dba_constraints r
                ON c.r_owner=r.owner AND c.r_constraint_name=r.constraint_name
              JOIN dba_cons_columns rcc
                ON r.owner=rcc.owner AND r.constraint_name=rcc.constraint_name
               AND cc.position=rcc.position
             WHERE c.owner=:o AND c.constraint_type='R'
             ORDER BY cc.table_name, c.constraint_name, cc.position
        """, o=schema)
        fk_by_table: dict = {}
        for tbl, col, cname, rsch, rtab, rcol in fk_rows:
            fk_by_table.setdefault(tbl, []).append({
                "constraint_name": cname,
                "column": col,
                "ref_schema": rsch,
                "ref_table": rtab,
                "ref_column": rcol,
            })
            relationships.append({
                "from_schema": schema, "from_table": tbl, "from_column": col,
                "to_schema": rsch, "to_table": rtab, "to_column": rcol,
                "constraint_name": cname,
            })
        # Assemble per-table dict
        table_payload = []
        for tbl_name, obj_type in tables_meta:
            cols = by_table_cols.get(tbl_name, [])
            table_payload.append({
                "schema": schema,
                "name": tbl_name,
                "type": obj_type,
                "columns": cols,
                "primary_keys": pk_by_table.get(tbl_name, []),
                "foreign_keys": fk_by_table.get(tbl_name, []),
                "indexes": [],     # deferred -- not needed by emitter
                "constraints": [],
                "view_sql": None,
            })
        total_tables += len(table_payload)
        schemas_payload.append({"schema": schema, "tables": table_payload})
        if i % 10 == 0 or i == len(all_schemas):
            print(f"   ... {i}/{len(all_schemas)} schemas processed, "
                  f"{total_tables} tables so far")

    cur.close()
    conn.close()
    elapsed = time.perf_counter() - t0
    print(f"\n2) Done in {elapsed:.1f}s -- {len(schemas_payload)} schemas / "
          f"{total_tables} tables / {len(relationships)} FK relationships")

    # 3. Wrap in the same envelope schema_kb_service.load_schema_kb_payload reads
    payload = {
        "datasource_id": DS_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pdm": {
            "datasource": {
                "id": DS_ID,
                "name": "FREEPDB1 (live regenerated by Phase 7.14)",
                "db_type": "oracle",
                "host": "localhost",
                "database": "FREEPDB1",
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "schemas": schemas_payload,
            "relationships": relationships,
        },
        "ldm": {},
    }
    KB_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    size_mb = KB_PATH.stat().st_size / (1024 * 1024)
    print(f"\n3) Wrote {KB_PATH} ({size_mb:.2f} MB)")

    # 4. Spot-check: critical tables for AVY_FACT_SIDE emitter.
    print("\n4) Spot-check critical tables...")
    critical = [
        ("CCAL_REPL_OWNER", "TXN"),
        ("CCAL_REPL_OWNER", "APA"),
        ("CCAL_REPL_OWNER", "FIP"),
        ("CCAL_REPL_OWNER", "CL_VAL"),
        ("CCSI_OWNER", "AR_DIM"),
        ("TRANSACTIONS_OWNER", "AVY_FACT"),
    ]
    table_index = {(s["schema"].upper(), t["name"].upper()): t
                   for s in schemas_payload for t in s["tables"]}
    for sch, tbl in critical:
        t = table_index.get((sch, tbl))
        if t:
            print(f"   OK  {sch}.{tbl}: {len(t['columns'])} cols, "
                  f"{len(t['primary_keys'])} PKs, {len(t['foreign_keys'])} FKs")
        else:
            print(f"   MISS {sch}.{tbl} -- not in FREEPDB1")

    return 0


if __name__ == "__main__":
    sys.exit(main())
