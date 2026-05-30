"""Local schema KB and PDM/LDM extraction utilities."""
from __future__ import annotations

import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import json

logger = logging.getLogger(__name__)

from app.config import BASE_DIR
from app.models.datasource import DataSource
from app.connectors.factory import get_connector_from_model
from app.services.operation_control import ensure_not_stopped, set_total, advance_progress, add_notification


def _kb_dir() -> Path:
    # Keep KB inside the DB Testing Tool workspace data folder (not LocalAppData).
    path = BASE_DIR / "data" / "local_kb"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _kb_json_path(datasource_id: int) -> Path:
    return _kb_dir() / f"schema_kb_ds_{datasource_id}.json"


def _hint_index_path(datasource_id: int) -> Path:
    """Small {OWNER.TABLE:[cols]} index — fast to read, built from full KB."""
    return _kb_dir() / f"hint_index_ds_{datasource_id}.json"


def _build_hint_index_from_kb(datasource_id: int) -> dict:
    """Parse full KB and write a lean hint index file. Returns the tables dict."""
    json_path = _kb_json_path(datasource_id)
    if not json_path.exists():
        return {}
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        pdm = payload.get("pdm") if isinstance(payload, dict) else None
        if not pdm:
            pdm = payload
        tables: dict = {}
        for schema_block in (pdm or {}).get("schemas", []) if isinstance(pdm, dict) else []:
            owner = (schema_block.get("schema") or "").upper()
            for tbl in schema_block.get("tables", []):
                tname = (tbl.get("name") or tbl.get("table_name") or "").upper()
                if not tname:
                    continue
                cols = [c.get("name", "").upper() for c in tbl.get("columns", []) if c.get("name")]
                tables[f"{owner}.{tname}"] = cols
        # Write small index file
        index_path = _hint_index_path(datasource_id)
        index_path.write_text(json.dumps(tables, separators=(",", ":")), encoding="utf-8")
        return tables
    except Exception:
        return {}


def _load_hint_index(datasource_id: int) -> dict:
    """Load the small hint index if it exists (instant, <1 MB typically)."""
    p = _hint_index_path(datasource_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _kb_md_path(datasource_id: int) -> Path:
    return _kb_dir() / f"schema_kb_ds_{datasource_id}.md"


def _safe_upper(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def _pick_schemas(all_schemas: List[str], selected: Optional[List[str]]) -> List[str]:
    if not selected:
        return all_schemas
    wanted = {_safe_upper(s) for s in selected if _safe_upper(s)}
    return [s for s in all_schemas if _safe_upper(s) in wanted]


def _query_or_empty(connector, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    try:
        return connector.execute_query(sql, params or {})
    except Exception:
        return []


# ── Oracle bulk extractor (Phase 7.15) ───────────────────────────────────────
#
# Operator-locked rationale (2026-05-30): the legacy per-table FK extractor
# (`_oracle_foreign_keys` below) uses ALL_CONSTRAINTS which is grant-filtered.
# A user without SELECT_CATALOG_ROLE cannot see referenced PKs that live in
# other schemas -- result: 177 FKs captured on a DB that actually has 1375.
#
# This bulk extractor uses DBA_* views when available (one SELECT_CATALOG_ROLE
# grant is enough), falls back to ALL_* views otherwise.  Per-schema batching
# replaces N-tables-N-roundtrips with N-schemas-N-roundtrips (~40x fewer
# round-trips on the 4000-table FREEPDB1 sample).
#
# `oracle_bulk_extract_schema(connector, schema)` returns:
#   {
#     'tables': {table_name -> {'columns':..., 'primary_keys':...,
#                                'foreign_keys':..., 'object_type':...}},
#     'used_dba_views': bool,        # diagnostic
#   }

def _oracle_has_dba_access(connector) -> bool:
    """True iff the connecting user can SELECT from dba_users."""
    try:
        rows = connector.execute_query(
            "SELECT 1 FROM dba_users WHERE ROWNUM = 1", {},
        )
        return rows is not None
    except Exception:
        return False


def oracle_bulk_extract_schema(connector, schema: str) -> Dict[str, Any]:
    """Bulk-extract every (object_type, columns, PKs, FKs) for one
    Oracle schema in O(4) round-trips total.  Uses DBA_* views when
    available; falls back to ALL_* with a clear warning logged.
    """
    use_dba = _oracle_has_dba_access(connector)
    prefix = "dba" if use_dba else "all"
    schema_filter = "owner" if use_dba else "owner"
    s_upper = (schema or "").upper()

    # 1. Object list (tables + views)
    obj_sql = (
        f"SELECT object_name, object_type FROM {prefix}_objects "
        f"WHERE {schema_filter}=:o AND object_type IN "
        f"('TABLE','VIEW','MATERIALIZED VIEW') ORDER BY object_name"
    )
    obj_rows = _query_or_empty(connector, obj_sql, {"o": s_upper}) or []

    # 2. All columns for the schema in one round trip
    col_sql = (
        f"SELECT table_name, column_name, data_type, nullable, "
        f"column_id, data_length, data_precision, data_scale "
        f"FROM {prefix}_tab_columns WHERE {schema_filter}=:o "
        f"ORDER BY table_name, column_id"
    )
    col_rows = _query_or_empty(connector, col_sql, {"o": s_upper}) or []

    # 3. PK constraint columns
    pk_sql = (
        f"SELECT cc.table_name, cc.column_name "
        f"FROM {prefix}_constraints c "
        f"JOIN {prefix}_cons_columns cc "
        f"  ON c.owner=cc.owner AND c.constraint_name=cc.constraint_name "
        f"WHERE c.{schema_filter}=:o AND c.constraint_type='P' "
        f"ORDER BY cc.table_name, cc.position"
    )
    pk_rows = _query_or_empty(connector, pk_sql, {"o": s_upper}) or []

    # 4. FK constraint columns -- joins forward to the referenced PK
    #    via R_OWNER / R_CONSTRAINT_NAME (THIS is what the old per-table
    #    extractor did but with all_* visibility, which dropped cross-
    #    schema FKs when grants didn't extend).
    fk_sql = (
        f"SELECT cc.table_name, cc.column_name, c.constraint_name, "
        f"r.owner AS ref_owner, rcc.table_name AS ref_table, "
        f"rcc.column_name AS ref_col "
        f"FROM {prefix}_constraints c "
        f"JOIN {prefix}_cons_columns cc "
        f"  ON c.owner=cc.owner AND c.constraint_name=cc.constraint_name "
        f"JOIN {prefix}_constraints r "
        f"  ON c.r_owner=r.owner AND c.r_constraint_name=r.constraint_name "
        f"JOIN {prefix}_cons_columns rcc "
        f"  ON r.owner=rcc.owner AND r.constraint_name=rcc.constraint_name "
        f"  AND cc.position=rcc.position "
        f"WHERE c.{schema_filter}=:o AND c.constraint_type='R' "
        f"ORDER BY cc.table_name, c.constraint_name, cc.position"
    )
    fk_rows = _query_or_empty(connector, fk_sql, {"o": s_upper}) or []

    # Index everything by table_name
    def _norm(rec: Dict[str, Any], key: str) -> Any:
        # Connectors may return uppercase OR lowercase keys; normalize.
        return rec.get(key) or rec.get(key.upper()) or rec.get(key.lower())

    by_table_cols: Dict[str, list] = {}
    for r in col_rows:
        tbl = _norm(r, "table_name")
        if not tbl:
            continue
        dtype = _norm(r, "data_type") or "VARCHAR2"
        dlen = _norm(r, "data_length")
        dprec = _norm(r, "data_precision")
        dscale = _norm(r, "data_scale")
        # Construct display type (NUMBER(p,s), VARCHAR2(n), etc.)
        disp = dtype
        if dtype in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR") and dlen:
            disp = f"{dtype}({dlen})"
        elif dtype == "NUMBER" and dprec is not None:
            disp = f"NUMBER({dprec},{dscale or 0})"
        by_table_cols.setdefault(tbl, []).append({
            "name": _norm(r, "column_name"),
            "data_type": disp,
            "nullable": (_norm(r, "nullable") == "Y"),
            "is_pk": False,
            "ordinal_position": _norm(r, "column_id"),
        })
    pk_by_table: Dict[str, list] = {}
    for r in pk_rows:
        tbl = _norm(r, "table_name")
        col = _norm(r, "column_name")
        if tbl and col:
            pk_by_table.setdefault(tbl, []).append(col)
    for tbl, cols in by_table_cols.items():
        pks = set(pk_by_table.get(tbl, []))
        for c in cols:
            if c["name"] in pks:
                c["is_pk"] = True
    fk_by_table: Dict[str, list] = {}
    for r in fk_rows:
        tbl = _norm(r, "table_name")
        if not tbl:
            continue
        fk_by_table.setdefault(tbl, []).append({
            "constraint_name": _norm(r, "constraint_name"),
            "column": _norm(r, "column_name"),
            "ref_schema": _norm(r, "ref_owner"),
            "ref_table": _norm(r, "ref_table"),
            "ref_column": _norm(r, "ref_col"),
        })

    tables_out: Dict[str, Dict[str, Any]] = {}
    for r in obj_rows:
        name = _norm(r, "object_name")
        obj_type = _norm(r, "object_type")
        tables_out[name] = {
            "columns": by_table_cols.get(name, []),
            "primary_keys": pk_by_table.get(name, []),
            "foreign_keys": fk_by_table.get(name, []),
            "object_type": obj_type,
        }
    return {
        "tables": tables_out,
        "used_dba_views": use_dba,
    }


def _oracle_foreign_keys(connector, schema: str, table: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            a.column_name AS column_name,
            c_pk.owner AS referenced_schema,
            c_pk.table_name AS referenced_table,
            b.column_name AS referenced_column,
            c.constraint_name AS constraint_name
        FROM all_constraints c
        JOIN all_cons_columns a
            ON c.owner = a.owner AND c.constraint_name = a.constraint_name
        JOIN all_constraints c_pk
            ON c.r_owner = c_pk.owner AND c.r_constraint_name = c_pk.constraint_name
        JOIN all_cons_columns b
            ON c_pk.owner = b.owner AND c_pk.constraint_name = b.constraint_name
            AND a.position = b.position
        WHERE c.constraint_type = 'R'
          AND c.owner = :schema
          AND c.table_name = :table_name
        ORDER BY a.position
    """
    rows = _query_or_empty(connector, sql, {"schema": schema.upper(), "table_name": table.upper()})
    return [
        {
            "column": r.get("COLUMN_NAME"),
            "ref_schema": r.get("REFERENCED_SCHEMA"),
            "ref_table": r.get("REFERENCED_TABLE"),
            "ref_column": r.get("REFERENCED_COLUMN"),
            "constraint_name": r.get("CONSTRAINT_NAME"),
        }
        for r in rows
    ]


def _oracle_indexes(connector, schema: str, table: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT i.index_name, i.uniqueness, c.column_name, c.column_position
        FROM all_indexes i
        JOIN all_ind_columns c
          ON i.owner = c.index_owner
         AND i.index_name = c.index_name
        WHERE i.table_owner = :schema
          AND i.table_name = :table_name
        ORDER BY i.index_name, c.column_position
    """
    rows = _query_or_empty(connector, sql, {"schema": schema.upper(), "table_name": table.upper()})
    grouped: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        idx = r.get("INDEX_NAME")
        if idx not in grouped:
            grouped[idx] = {
                "name": idx,
                "unique": (r.get("UNIQUENESS") or "").upper() == "UNIQUE",
                "columns": [],
            }
        grouped[idx]["columns"].append(r.get("COLUMN_NAME"))
    return list(grouped.values())


def _oracle_constraints(connector, schema: str, table: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT constraint_name, constraint_type, search_condition
        FROM all_constraints
        WHERE owner = :schema
          AND table_name = :table_name
          AND constraint_type IN ('P','U','C','R')
        ORDER BY constraint_name
    """
    rows = _query_or_empty(connector, sql, {"schema": schema.upper(), "table_name": table.upper()})
    out = []
    for r in rows:
        ctype = (r.get("CONSTRAINT_TYPE") or "").upper()
        out.append({
            "name": r.get("CONSTRAINT_NAME"),
            "type": {"P": "PRIMARY KEY", "U": "UNIQUE", "C": "CHECK", "R": "FOREIGN KEY"}.get(ctype, ctype),
            "expression": r.get("SEARCH_CONDITION"),
        })
    return out


def _redshift_foreign_keys(connector, schema: str, table: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            kcu.column_name,
            ccu.table_schema AS ref_schema,
            ccu.table_name AS ref_table,
            ccu.column_name AS ref_column,
            tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = %(schema)s
          AND tc.table_name = %(table)s
        ORDER BY tc.constraint_name, kcu.ordinal_position
    """
    rows = _query_or_empty(connector, sql, {"schema": schema, "table": table})
    return [
        {
            "column": r.get("column_name"),
            "ref_schema": r.get("ref_schema"),
            "ref_table": r.get("ref_table"),
            "ref_column": r.get("ref_column"),
            "constraint_name": r.get("constraint_name"),
        }
        for r in rows
    ]


def _redshift_indexes(connector, schema: str, table: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = %(schema)s
          AND tablename = %(table)s
        ORDER BY indexname
    """
    rows = _query_or_empty(connector, sql, {"schema": schema, "table": table})
    return [
        {
            "name": r.get("indexname"),
            "unique": " unique " in (r.get("indexdef") or "").lower(),
            "definition": r.get("indexdef"),
            "columns": [],
        }
        for r in rows
    ]


def _redshift_constraints(connector, schema: str, table: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT tc.constraint_name, tc.constraint_type
        FROM information_schema.table_constraints tc
        WHERE tc.table_schema = %(schema)s
          AND tc.table_name = %(table)s
        ORDER BY tc.constraint_name
    """
    rows = _query_or_empty(connector, sql, {"schema": schema, "table": table})
    return [
        {
            "name": r.get("constraint_name"),
            "type": r.get("constraint_type"),
            "expression": None,
        }
        for r in rows
    ]


def _oracle_view_sql(connector, schema: str, view_name: str, object_type: str) -> Optional[str]:
    if (object_type or "").upper() == "MVIEW":
        rows = _query_or_empty(
            connector,
            "SELECT query FROM all_mviews WHERE owner = :schema AND mview_name = :name",
            {"schema": schema.upper(), "name": view_name.upper()},
        )
        if rows:
            return rows[0].get("QUERY")
    rows = _query_or_empty(
        connector,
        "SELECT text_vc FROM all_views WHERE owner = :schema AND view_name = :name",
        {"schema": schema.upper(), "name": view_name.upper()},
    )
    if rows and rows[0].get("TEXT_VC"):
        return rows[0].get("TEXT_VC")
    rows = _query_or_empty(
        connector,
        "SELECT text FROM all_views WHERE owner = :schema AND view_name = :name",
        {"schema": schema.upper(), "name": view_name.upper()},
    )
    if rows:
        return rows[0].get("TEXT")
    return None


def _redshift_view_sql(connector, schema: str, view_name: str) -> Optional[str]:
    rows = _query_or_empty(
        connector,
        "SELECT definition FROM pg_views WHERE schemaname = %(schema)s AND viewname = %(view)s",
        {"schema": schema, "view": view_name},
    )
    return rows[0].get("definition") if rows else None


def _sqlserver_view_sql(connector, schema: str, view_name: str) -> Optional[str]:
    rows = _query_or_empty(
        connector,
        """
        SELECT sm.definition
        FROM sys.views v
        JOIN sys.schemas s ON v.schema_id = s.schema_id
        JOIN sys.sql_modules sm ON v.object_id = sm.object_id
        WHERE s.name = ? AND v.name = ?
        """,
        {"schema": schema, "view": view_name},
    )
    return rows[0].get("definition") if rows else None


def _sqlserver_foreign_keys(connector, schema: str, table: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT
            parent_col.name AS column_name,
            ref_schema.name AS ref_schema,
            ref_table.name AS ref_table,
            ref_col.name AS ref_column,
            fk.name AS constraint_name
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fk.object_id = fkc.constraint_object_id
        JOIN sys.tables parent_table ON fk.parent_object_id = parent_table.object_id
        JOIN sys.schemas parent_schema ON parent_table.schema_id = parent_schema.schema_id
        JOIN sys.columns parent_col ON fkc.parent_object_id = parent_col.object_id AND fkc.parent_column_id = parent_col.column_id
        JOIN sys.tables ref_table ON fk.referenced_object_id = ref_table.object_id
        JOIN sys.schemas ref_schema ON ref_table.schema_id = ref_schema.schema_id
        JOIN sys.columns ref_col ON fkc.referenced_object_id = ref_col.object_id AND fkc.referenced_column_id = ref_col.column_id
        WHERE parent_schema.name = ?
          AND parent_table.name = ?
        ORDER BY fk.name, fkc.constraint_column_id
    """
    rows = _query_or_empty(connector, sql, {"schema": schema, "table": table})
    return [
        {
            "column": r.get("column_name"),
            "ref_schema": r.get("ref_schema"),
            "ref_table": r.get("ref_table"),
            "ref_column": r.get("ref_column"),
            "constraint_name": r.get("constraint_name"),
        }
        for r in rows
    ]


def _sqlserver_indexes(connector, schema: str, table: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT i.name AS index_name, i.is_unique, c.name AS column_name, ic.key_ordinal
        FROM sys.indexes i
        JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
        JOIN sys.columns c ON ic.object_id = c.object_id AND ic.column_id = c.column_id
        JOIN sys.tables t ON i.object_id = t.object_id
        JOIN sys.schemas s ON t.schema_id = s.schema_id
        WHERE s.name = ?
          AND t.name = ?
          AND i.is_hypothetical = 0
          AND i.name IS NOT NULL
        ORDER BY i.name, ic.key_ordinal
    """
    rows = _query_or_empty(connector, sql, {"schema": schema, "table": table})
    grouped: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        idx = r.get("index_name")
        if idx not in grouped:
            grouped[idx] = {
                "name": idx,
                "unique": bool(r.get("is_unique")),
                "columns": [],
            }
        grouped[idx]["columns"].append(r.get("column_name"))
    return list(grouped.values())


def _sqlserver_constraints(connector, schema: str, table: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        WHERE tc.TABLE_SCHEMA = ?
          AND tc.TABLE_NAME = ?
        ORDER BY tc.CONSTRAINT_NAME
    """
    rows = _query_or_empty(connector, sql, {"schema": schema, "table": table})
    return [
        {
            "name": r.get("CONSTRAINT_NAME"),
            "type": r.get("CONSTRAINT_TYPE"),
            "expression": None,
        }
        for r in rows
    ]


def _table_details(
    connector,
    db_type: str,
    schema: str,
    table: str,
    object_type: str,
    oracle_bulk: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    # Oracle bulk fast-path (Phase 7.15): if the caller pre-fetched the
    # schema-level columns/PKs/FKs via `oracle_bulk_extract_schema`,
    # serve columns + PKs + FKs from that cache instead of hitting the
    # connector per-table.  Indexes/constraints/view_sql still fall
    # through to per-table (kept small + DBA-view sensitive).
    if db_type == "oracle" and oracle_bulk is not None and table in oracle_bulk:
        bulk = oracle_bulk[table]
        columns = list(bulk.get("columns") or [])
        primary_keys = list(bulk.get("primary_keys") or [])
        foreign_keys = list(bulk.get("foreign_keys") or [])
        indexes = _oracle_indexes(connector, schema, table)
        constraints = _oracle_constraints(connector, schema, table)
        view_sql = _oracle_view_sql(connector, schema, table, object_type) if object_type in {"VIEW", "MVIEW"} else None
        return {
            "schema": schema,
            "name": table,
            "type": object_type,
            "columns": columns,
            "primary_keys": primary_keys,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
            "constraints": constraints,
            "view_sql": view_sql,
        }

    cols = connector.get_columns(schema, table)
    columns = [
        {
            "name": c.column_name,
            "data_type": c.data_type,
            "nullable": c.nullable,
            "is_pk": c.is_pk,
            "ordinal_position": c.ordinal_position,
        }
        for c in cols
    ]
    primary_keys = [c.column_name for c in cols if c.is_pk]

    if db_type == "oracle":
        foreign_keys = _oracle_foreign_keys(connector, schema, table)
        indexes = _oracle_indexes(connector, schema, table)
        constraints = _oracle_constraints(connector, schema, table)
        view_sql = _oracle_view_sql(connector, schema, table, object_type) if object_type in {"VIEW", "MVIEW"} else None
    elif db_type == "sqlserver":
        foreign_keys = _sqlserver_foreign_keys(connector, schema, table)
        indexes = _sqlserver_indexes(connector, schema, table)
        constraints = _sqlserver_constraints(connector, schema, table)
        view_sql = _sqlserver_view_sql(connector, schema, table) if object_type == "VIEW" else None
    else:
        foreign_keys = _redshift_foreign_keys(connector, schema, table)
        indexes = _redshift_indexes(connector, schema, table)
        constraints = _redshift_constraints(connector, schema, table)
        view_sql = _redshift_view_sql(connector, schema, table) if object_type == "VIEW" else None

    return {
        "schema": schema,
        "name": table,
        "type": object_type,
        "columns": columns,
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
        "indexes": indexes,
        "constraints": constraints,
        "view_sql": view_sql,
    }


def build_pdm_catalog(
    ds: DataSource,
    selected_schemas: Optional[List[str]] = None,
    operation_id: Optional[str] = None,
    skip_existing: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    connector = get_connector_from_model(ds)
    db_type = (ds.db_type or "").lower().strip()

    schemas_payload: List[Dict[str, Any]] = []
    relationships: List[Dict[str, Any]] = []

    try:
        connector.connect()
        all_schemas = connector.get_schemas()
        chosen = _pick_schemas(all_schemas, selected_schemas)
        add_notification(operation_id, f"PDM selected schemas: {len(chosen)}")

        schema_tables = {}
        total_units = len(chosen)
        for schema in chosen:
            ensure_not_stopped(operation_id)
            tables = connector.get_tables(schema)
            schema_tables[schema] = tables
            # Only count tables not already in the existing KB towards the progress total
            schema_existing_set = (skip_existing or {}).get(schema.upper(), set())
            new_count = sum(
                1 for t in tables
                if not (schema_existing_set and t.table_name.upper() in schema_existing_set)
            )
            total_units += new_count
        set_total(operation_id, total_units)

        for schema in chosen:
            ensure_not_stopped(operation_id)
            tables = schema_tables.get(schema, [])
            advance_progress(operation_id, 1, f"Building schema {schema} ({len(tables)} objects)")
            table_payload = []

            # Oracle bulk fast-path (Phase 7.15): one round-trip per
            # schema for columns + PKs + FKs.  Uses DBA_* views when
            # the connecting user has SELECT_CATALOG_ROLE so cross-
            # schema FKs are visible (legacy per-table ALL_* path
            # silently dropped them -- 177 vs 1375 FKs on FREEPDB1).
            oracle_bulk_cache: Optional[Dict[str, Dict[str, Any]]] = None
            if db_type == "oracle":
                try:
                    bulk = oracle_bulk_extract_schema(connector, schema)
                    oracle_bulk_cache = bulk.get("tables") or {}
                    if bulk.get("used_dba_views"):
                        add_notification(
                            operation_id,
                            f"  {schema}: bulk-extract via DBA views "
                            f"({len(oracle_bulk_cache)} objects)",
                        )
                    else:
                        add_notification(
                            operation_id,
                            f"  {schema}: bulk-extract via ALL views (no DBA "
                            f"role; cross-schema FKs may be undercount)",
                        )
                except Exception as exc:  # noqa: BLE001 - fall back to per-table
                    logger.warning(
                        "Oracle bulk extract failed for %s: %s -- "
                        "falling back to per-table queries", schema, exc,
                    )
                    oracle_bulk_cache = None

            for t in tables:
                ensure_not_stopped(operation_id)
                # Skip tables already in the KB — saves all the DB metadata queries
                schema_existing_set = (skip_existing or {}).get(schema.upper(), set())
                if schema_existing_set and t.table_name.upper() in schema_existing_set:
                    continue
                detail = _table_details(
                    connector, db_type, schema, t.table_name, t.table_type,
                    oracle_bulk=oracle_bulk_cache,
                )
                table_payload.append(detail)
                for fk in detail.get("foreign_keys", []):
                    relationships.append({
                        "from_schema": schema,
                        "from_table": t.table_name,
                        "from_column": fk.get("column"),
                        "to_schema": fk.get("ref_schema"),
                        "to_table": fk.get("ref_table"),
                        "to_column": fk.get("ref_column"),
                        "constraint_name": fk.get("constraint_name"),
                    })
                advance_progress(operation_id, 1)

            schemas_payload.append({
                "schema": schema,
                "tables": table_payload,
            })

    finally:
        connector.disconnect()

    return {
        "datasource": {
            "id": ds.id,
            "name": ds.name,
            "db_type": ds.db_type,
            "host": ds.host,
            "database": ds.database_name,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schemas": schemas_payload,
        "relationships": relationships,
    }


def build_ldm_from_pdm(pdm: Dict[str, Any]) -> Dict[str, Any]:
    entities = []
    relations = []
    for schema_block in pdm.get("schemas", []):
        schema_name = schema_block.get("schema")
        for tbl in schema_block.get("tables", []):
            entities.append({
                "entity": f"{schema_name}.{tbl.get('name')}",
                "schema": schema_name,
                "table": tbl.get("name"),
                "primary_keys": tbl.get("primary_keys", []),
                "attributes": [
                    {
                        "name": c.get("name"),
                        "type": c.get("data_type"),
                        "nullable": c.get("nullable", True),
                    }
                    for c in tbl.get("columns", [])
                ],
            })

    for r in pdm.get("relationships", []):
        relations.append({
            "from": f"{r.get('from_schema')}.{r.get('from_table')}.{r.get('from_column')}",
            "to": f"{r.get('to_schema')}.{r.get('to_table')}.{r.get('to_column')}",
            "name": r.get("constraint_name"),
        })

    return {
        "datasource": pdm.get("datasource", {}),
        "generated_at": pdm.get("generated_at"),
        "entities": entities,
        "relationships": relations,
    }


def _render_kb_markdown(pdm: Dict[str, Any], ldm: Dict[str, Any]) -> str:
    ds = pdm.get("datasource", {})
    lines = [
        f"# Local DB Knowledge Base: {ds.get('name', 'Data Source')}",
        "",
        f"- Generated At: {pdm.get('generated_at')}",
        f"- DB Type: {ds.get('db_type')}",
        f"- Host: {ds.get('host')}",
        f"- Database: {ds.get('database')}",
        f"- Schemas: {len(pdm.get('schemas', []))}",
        f"- Relationships: {len(pdm.get('relationships', []))}",
        "",
        "## PDM Summary",
    ]

    for schema_block in pdm.get("schemas", []):
        lines.append(f"### Schema {schema_block.get('schema')}")
        for tbl in schema_block.get("tables", []):
            lines.append(
                f"- {tbl.get('name')} ({tbl.get('type')}), "
                f"columns={len(tbl.get('columns', []))}, "
                f"pk={','.join(tbl.get('primary_keys', []) or ['-'])}, "
                f"fk={len(tbl.get('foreign_keys', []))}, "
                f"indexes={len(tbl.get('indexes', []))}, "
                f"constraints={len(tbl.get('constraints', []))}"
            )

    lines.append("")
    lines.append("## LDM Relationships")
    for rel in ldm.get("relationships", [])[:500]:
        lines.append(f"- {rel.get('from')} -> {rel.get('to')}")

    return "\n".join(lines) + "\n"


def _load_existing_payload(datasource_id: int) -> Dict[str, Any]:
    json_path = _kb_json_path(datasource_id)
    if not json_path.exists():
        return {}
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _merge_pdm_catalog(existing_pdm: Dict[str, Any], new_pdm: Dict[str, Any]) -> Dict[str, Any]:
    if not existing_pdm:
        return new_pdm

    merged = dict(existing_pdm)
    merged["generated_at"] = datetime.now(timezone.utc).isoformat()
    merged["datasource"] = new_pdm.get("datasource") or existing_pdm.get("datasource")

    schema_map: Dict[str, Dict[str, Any]] = {}
    for block in existing_pdm.get("schemas", []) or []:
        sname = block.get("schema")
        if not sname:
            continue
        table_map = {}
        for t in block.get("tables", []) or []:
            tname = t.get("name")
            if tname:
                table_map[tname.upper()] = t
        schema_map[sname.upper()] = {"schema": sname, "table_map": table_map}

    for block in new_pdm.get("schemas", []) or []:
        sname = block.get("schema")
        if not sname:
            continue
        key = sname.upper()
        if key not in schema_map:
            schema_map[key] = {"schema": sname, "table_map": {}}
        for t in block.get("tables", []) or []:
            tname = t.get("name")
            if tname:
                # Replace table metadata on repeated save to keep latest schema details.
                schema_map[key]["table_map"][tname.upper()] = t

    merged_schemas = []
    for key in sorted(schema_map.keys()):
        entry = schema_map[key]
        tables_sorted = [entry["table_map"][tkey] for tkey in sorted(entry["table_map"].keys())]
        merged_schemas.append({
            "schema": entry["schema"],
            "tables": tables_sorted,
        })
    merged["schemas"] = merged_schemas

    rel_map = {}
    for rel in (existing_pdm.get("relationships", []) or []) + (new_pdm.get("relationships", []) or []):
        rel_key = (
            (rel.get("from_schema") or ""),
            (rel.get("from_table") or ""),
            (rel.get("from_column") or ""),
            (rel.get("to_schema") or ""),
            (rel.get("to_table") or ""),
            (rel.get("to_column") or ""),
            (rel.get("constraint_name") or ""),
        )
        rel_map[rel_key] = rel
    merged["relationships"] = list(rel_map.values())

    return merged


def _existing_table_index(existing_pdm: Dict[str, Any]) -> Dict[str, set[str]]:
    index: Dict[str, set[str]] = {}
    for block in existing_pdm.get("schemas", []) or []:
        schema_name = (block.get("schema") or "").strip().upper()
        if not schema_name:
            continue
        tables = {
            (table.get("name") or "").strip().upper()
            for table in block.get("tables", []) or []
            if (table.get("name") or "").strip()
        }
        index[schema_name] = tables
    return index


def save_schema_kb(datasource_id: int, pdm: Dict[str, Any], ldm: Dict[str, Any]) -> Dict[str, str]:
    payload = {
        "datasource_id": datasource_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pdm": pdm,
        "ldm": ldm,
    }

    json_path = _kb_json_path(datasource_id)
    md_path = _kb_md_path(datasource_id)

    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_render_kb_markdown(pdm, ldm), encoding="utf-8")

    # Always rebuild the small hint index alongside the full KB
    try:
        tables: dict = {}
        for schema_block in (pdm or {}).get("schemas", []):
            owner = (schema_block.get("schema") or "").upper()
            for tbl in schema_block.get("tables", []):
                tname = (tbl.get("name") or tbl.get("table_name") or "").upper()
                if not tname:
                    continue
                cols = [c.get("name", "").upper() for c in tbl.get("columns", []) if c.get("name")]
                tables[f"{owner}.{tname}"] = cols
        _hint_index_path(datasource_id).write_text(
            json.dumps(tables, separators=(",", ":")), encoding="utf-8"
        )
    except Exception as _hint_err:
        logger.warning(
            "save_schema_kb: hint_index rebuild failed for datasource %s — %s",
            datasource_id,
            _hint_err,
        )

    return {"json_path": str(json_path), "markdown_path": str(md_path)}


def build_and_save_schema_kb(ds: DataSource, selected_schemas: Optional[List[str]] = None, operation_id: Optional[str] = None) -> Dict[str, Any]:
    existing_payload = _load_existing_payload(ds.id)
    existing_pdm = existing_payload.get("pdm", {}) if isinstance(existing_payload, dict) else {}
    existing_index = _existing_table_index(existing_pdm)
    pdm_new = build_pdm_catalog(ds, selected_schemas, operation_id, skip_existing=existing_index)

    filtered_schemas = []
    skipped_tables = 0
    for schema_block in pdm_new.get("schemas", []) or []:
        schema_name = (schema_block.get("schema") or "").strip()
        existing_tables = existing_index.get(schema_name.upper(), set())
        new_tables = []
        for table in schema_block.get("tables", []) or []:
            table_name = (table.get("name") or "").strip().upper()
            if table_name and table_name in existing_tables:
                skipped_tables += 1
                continue
            new_tables.append(table)
        if new_tables:
            filtered_schemas.append({
                "schema": schema_name,
                "tables": new_tables,
            })

    if filtered_schemas:
        pdm_new["schemas"] = filtered_schemas
        filtered_relationships = []
        kept_pairs = {
            ((schema.get("schema") or "").strip().upper(), (table.get("name") or "").strip().upper())
            for schema in filtered_schemas
            for table in schema.get("tables", []) or []
        }
        for rel in pdm_new.get("relationships", []) or []:
            from_pair = ((rel.get("from_schema") or "").strip().upper(), (rel.get("from_table") or "").strip().upper())
            if from_pair in kept_pairs:
                filtered_relationships.append(rel)
        pdm_new["relationships"] = filtered_relationships
    else:
        pdm_new["schemas"] = []
        pdm_new["relationships"] = []

    pdm_merged = _merge_pdm_catalog(existing_pdm, pdm_new)
    ldm = build_ldm_from_pdm(pdm_merged)
    paths = save_schema_kb(ds.id, pdm_merged, ldm)

    schema_count = len(pdm_merged.get("schemas", []))
    table_count = sum(len(s.get("tables", [])) for s in pdm_merged.get("schemas", []))
    column_count = sum(
        len(t.get("columns", []))
        for s in pdm_merged.get("schemas", [])
        for t in s.get("tables", [])
    )

    return {
        "pdm": pdm_merged,
        "ldm": ldm,
        "paths": paths,
        "stats": {
            "schemas": schema_count,
            "tables": table_count,
            "columns": column_count,
            "relationships": len(pdm_merged.get("relationships", [])),
            "skipped_existing_tables": skipped_tables,
        },
    }


def load_schema_kb_payload(datasource_id: Optional[int] = None) -> Dict[str, Any]:
    """Load KB payload(s) for one or all saved datasources.

    Operator-locked (2026-05-29 Phase 7.4): detect Git LFS pointer
    files (`version https://git-lfs.github.com/...`) and skip them
    with a warning so the silent-fail doesn't mask a missing
    `git lfs pull`.  Previously the JSON parse error was swallowed
    and the caller saw "table not found" with no hint that the KB
    file was an unfetched LFS pointer.
    """
    import logging
    _log = logging.getLogger(__name__)
    kb = _kb_dir()
    files = []
    if datasource_id is not None:
        p = _kb_json_path(datasource_id)
        if p.exists():
            files = [p]
    else:
        files = sorted(kb.glob("schema_kb_ds_*.json"))

    merged = {"sources": []}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
            # LFS pointer detection: first line is `version https://...`
            if text.lstrip().startswith("version https://git-lfs"):
                _log.warning(
                    "Schema KB %s is a Git LFS pointer (%d bytes), not real "
                    "content. Run `git lfs pull` to fetch.",
                    f.name, len(text),
                )
                continue
            payload = json.loads(text)
            merged["sources"].append(payload)
        except json.JSONDecodeError as exc:
            _log.warning("Schema KB %s is not valid JSON: %s", f.name, exc)
            continue
        except OSError as exc:
            _log.warning("Schema KB %s could not be read: %s", f.name, exc)
            continue
    return merged
