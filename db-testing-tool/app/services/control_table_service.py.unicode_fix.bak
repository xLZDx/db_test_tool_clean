"""Control-table generation and comparison helpers.

Builds a step-based control-table workflow from:
- saved PDM metadata for the target table
- DRD rows parsed by the existing DRD import service
- optional manual SQL supplied by the user for comparison
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from app.services.drd_import_service import (
    _extract_case_expression,
    _extract_lookup_spec,
    _is_lookup_table_name,
    generate_drd_tests,
    parse_drd_file,
    validate_column_mappings_with_kb,
)
from app.services.schema_kb_service import load_schema_kb_payload
from app.config import DATA_DIR


DEFAULT_DRD_FIELDS = [
    "logical_name",
    "physical_name",
    "source_schema",
    "source_table",
    "source_attribute",
    "transformation",
    "notes",
    "target_datatype_oracle",
    "target_nullable_oracle",
]

AUDIT_COLUMNS = {
    "CRT_DTM",
    "CRT_USR_NM",
    "LAST_UDT_DTM",
    "LAST_UDT_USR_NM",
    "SESN_NUM",
    "IND_UPDATE",
}

NVL_NULL_SENTINEL = "-999"
PARALLEL_HINT_DEGREE = 32

_SQL_IDENTIFIER_ALLOWLIST = {
    "AND",
    "AS",
    "BETWEEN",
    "BY",
    "CASE",
    "CAST",
    "COALESCE",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "DECODE",
    "ELSE",
    "END",
    "FALSE",
    "FROM",
    "IN",
    "IS",
    "LIKE",
    "NOT",
    "NULL",
    "NVL",
    "ON",
    "OR",
    "REGEXP_REPLACE",
    "REPLACE",
    "SUBSTR",
    "THEN",
    "TO_CHAR",
    "TO_DATE",
    "TO_NUMBER",
    "TRIM",
    "TRUE",
    "WHEN",
}


def analyze_control_table(
    *,
    file_bytes: bytes,
    filename: str,
    target_schema: str,
    target_table: str,
    source_datasource_id: int,
    target_datasource_id: int,
    control_schema: str,
    main_grain: str = "",
    manual_sql: str = "",
    selected_fields: Optional[List[str]] = None,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    selected = selected_fields or list(DEFAULT_DRD_FIELDS)
    parse_result = parse_drd_file(
        file_bytes=file_bytes,
        filename=filename,
        selected_fields=selected,
        target_schema=target_schema,
        target_table=target_table,
        source_datasource_id=source_datasource_id,
        target_datasource_id=target_datasource_id,
        sheet_name=sheet_name,
    )

    normalized = validate_column_mappings_with_kb(
        column_mappings=parse_result.get("column_mappings", []),
        target_schema=target_schema,
        target_table=target_table,
        source_datasource_id=source_datasource_id,
        target_datasource_id=target_datasource_id,
    )
    rows = normalized.get("column_mappings", [])

    baseline = generate_drd_tests(
        column_mappings=rows,
        target_schema=target_schema,
        target_table=target_table,
        source_datasource_id=source_datasource_id,
        target_datasource_id=target_datasource_id,
        main_grain=main_grain,
        include_diagnostics=True,
    )
    baseline_tests = baseline.get("tests", []) if isinstance(baseline, dict) else baseline

    target_def = load_target_table_definition(target_datasource_id, target_schema, target_table)
    source_index = _build_table_index(source_datasource_id)
    target_index = _build_table_index(target_datasource_id)
    kb_validation = _validate_control_table_requirements(
        rows=rows,
        target_definition=target_def,
        target_schema=target_schema,
        target_table=target_table,
        source_index=source_index,
        target_index=target_index,
    )
    analysis_rows = build_analysis_rows(
        rows=rows,
        baseline_tests=baseline_tests,
        target_schema=target_schema,
        target_table=target_table,
        target_definition=target_def,
        source_schema_index=source_index,
    )
    ddl_sql = build_control_table_ddl(control_schema, target_table, target_def)
    insert_sql = build_control_insert_sql(
        control_schema=control_schema,
        target_table=target_table,
        target_definition=target_def,
        analysis_rows=analysis_rows,
    )
    comparison = compare_insert_variants(analysis_rows, insert_sql, manual_sql)
    suite_tests = build_control_table_test_defs(
        target_schema=target_schema,
        target_table=target_table,
        control_schema=control_schema,
        target_definition=target_def,
        main_grain=main_grain,
        ddl_sql=ddl_sql,
        insert_sql=insert_sql,
        analysis_rows=analysis_rows,
        source_datasource_id=source_datasource_id,
        target_datasource_id=target_datasource_id,
    )

    return {
        "parse_result": parse_result,
        "kb_validation": normalized,
        "control_validation": kb_validation,
        "analysis_rows": analysis_rows,
        "target_definition": target_def,
        "selected_fields": selected,
        "create_table_sql": ddl_sql,
        "generated_insert_sql": insert_sql,
        "comparison": comparison,
        "tests": suite_tests,
        "baseline_skipped_rows": baseline.get("skipped_rows", []) if isinstance(baseline, dict) else [],
    }


def load_target_table_definition(datasource_id: int, target_schema: str, target_table: str) -> Dict[str, Any]:
    payload = load_schema_kb_payload(datasource_id)
    wanted_schema = (target_schema or "").strip().upper()
    wanted_table = (target_table or "").strip().upper()
    for src in payload.get("sources", []):
        pdm = (src or {}).get("pdm", {})
        for schema_block in pdm.get("schemas", []) or []:
            schema_name = (schema_block.get("schema") or "").strip().upper()
            if wanted_schema and schema_name != wanted_schema:
                continue
            for table_block in schema_block.get("tables", []) or []:
                table_name = (table_block.get("name") or "").strip().upper()
                if table_name == wanted_table:
                    return table_block
    # ── PDM miss on primary DS: search ALL other saved PDMs ──────────
    other_ds_ids = _list_all_datasource_ids()
    for other_id in other_ds_ids:
        if other_id == datasource_id:
            continue
        try:
            other_payload = load_schema_kb_payload(other_id)
            for src in other_payload.get("sources", []):
                pdm = (src or {}).get("pdm", {})
                for schema_block in pdm.get("schemas", []) or []:
                    schema_name = (schema_block.get("schema") or "").strip().upper()
                    if wanted_schema and schema_name != wanted_schema:
                        continue
                    for table_block in schema_block.get("tables", []) or []:
                        table_name = (table_block.get("name") or "").strip().upper()
                        if table_name == wanted_table:
                            table_block["_source_datasource_id"] = other_id
                            return table_block
        except Exception:
            continue
    # ── Still not found: fall back to live database query ──────────
    fallback = _load_table_def_from_live_db(datasource_id, wanted_schema, wanted_table)
    if fallback:
        return fallback
    # Try all other Oracle datasources
    for other_id in other_ds_ids:
        if other_id == datasource_id:
            continue
        fallback = _load_table_def_from_live_db(other_id, wanted_schema, wanted_table)
        if fallback:
            fallback["_source_datasource_id"] = other_id
            return fallback
    raise ValueError(
        f"Table {wanted_schema}.{wanted_table} not found in saved PDM and could not be loaded "
        f"from the live database. Please generate/save the PDM for datasource {datasource_id} "
        f"(Schema Browser → Generate PDM) or check the schema/table name."
    )


def _list_all_datasource_ids() -> List[int]:
    """Return all datasource IDs from the app database."""
    try:
        db_path = DATA_DIR / "app.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT id FROM datasources ORDER BY id")
        ids = [row[0] for row in cur.fetchall()]
        conn.close()
        return ids
    except Exception:
        return []


def _load_table_def_from_live_db(datasource_id: int, schema: str, table: str) -> Optional[Dict[str, Any]]:
    """Query ALL_TAB_COLUMNS on the live Oracle DB to build a minimal table_block.
    Used when the saved PDM does not include the requested schema."""
    try:
        db_path = DATA_DIR / "app.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT db_type, host, port, database_name, username, password, extra_params "
            "FROM datasources WHERE id = ?",
            (datasource_id,),
        )
        row = cur.fetchone()
        conn.close()
        if not row or (row["db_type"] or "").lower() != "oracle":
            return None

        from app.connectors.factory import get_connector
        connector = get_connector(
            db_type=row["db_type"],
            host=row["host"],
            port=int(row["port"]),
            database=row["database_name"],
            username=row["username"],
            password=row["password"] or "",
            extra_params=row["extra_params"],
        )

        col_rows = connector.execute_query(
            "SELECT COLUMN_NAME, DATA_TYPE, NULLABLE, COLUMN_ID "
            "FROM ALL_TAB_COLUMNS "
            "WHERE OWNER = :s AND TABLE_NAME = :t ORDER BY COLUMN_ID",
            {"s": schema, "t": table},
        )
        if not col_rows:
            return None

        # Resolve primary key columns
        try:
            pk_rows = connector.execute_query(
                "SELECT cc.COLUMN_NAME FROM ALL_CONSTRAINTS c "
                "JOIN ALL_CONS_COLUMNS cc ON cc.OWNER=c.OWNER AND cc.CONSTRAINT_NAME=c.CONSTRAINT_NAME "
                "WHERE c.OWNER=:s AND c.TABLE_NAME=:t AND c.CONSTRAINT_TYPE='P' ORDER BY cc.POSITION",
                {"s": schema, "t": table},
            )
            pk_set = {(r.get("COLUMN_NAME") or "").upper() for r in pk_rows}
        except Exception:
            pk_set = set()

        columns = [
            {
                "name": (r.get("COLUMN_NAME") or "").upper(),
                "data_type": r.get("DATA_TYPE") or "VARCHAR2",
                "nullable": (r.get("NULLABLE") or "Y") == "Y",
                "is_pk": (r.get("COLUMN_NAME") or "").upper() in pk_set,
                "ordinal_position": int(r.get("COLUMN_ID") or 0),
            }
            for r in col_rows
        ]
        return {
            "schema": schema,
            "name": table,
            "type": "TABLE",
            "columns": columns,
            "primary_keys": sorted(pk_set),
            "foreign_keys": [],
            "indexes": [],
            "constraints": [],
            "_source": "live_db_fallback",
        }
    except Exception:
        return None


def build_analysis_rows(
    *,
    rows: List[Dict[str, Any]],
    baseline_tests: List[Dict[str, Any]],
    target_schema: str,
    target_table: str,
    target_definition: Dict[str, Any],
    source_schema_index: Dict[Tuple[str, str], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    baseline_by_target = {
        (t.get("target_field") or "").upper(): t
        for t in baseline_tests
        if (t.get("target_field") or "").strip()
    }
    row_by_target: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        target_col = ((row.get("physical_name") or "") or _logical_to_physical(row.get("logical_name") or "")).upper()
        if target_col and target_col not in row_by_target:
            row_by_target[target_col] = row

    analysis = []
    for col in target_definition.get("columns", []) or []:
        col_name = (col.get("name") or "").strip().upper()
        if not col_name:
            continue
        row = row_by_target.get(col_name, {})
        baseline = baseline_by_target.get(col_name, {})
        source_attr = (row.get("source_attribute") or "").strip().upper()
        expr_info = extract_expr_from_test_sql(
            sql=baseline.get("source_query") or "",
            target_schema=target_schema,
            target_table=target_table,
            target_column=col_name,
            fallback_source_table=row.get("source_table") or "",
        )
        drd_expr = expr_info.get("expression") or fallback_drd_expression(row, source_attr)
        lookup_join = expr_info.get("lookup_join") or ""

        # If baseline did not preserve lookup metadata, derive it from DRD transformation text.
        if not lookup_join:
            lookup_join, derived_expr = derive_lookup_from_transformation(
                row=row,
                source_attr=source_attr,
                target_col=col_name,
                source_schema_index=source_schema_index,
                source_block=expr_info.get("source_block") or fallback_source_block(row),
            )
            _src_table_for_check = (row.get("source_table") or "").strip().upper().split(".")[-1]
            _plain_ref_forms = {normalize_sql_expr(f"S.{source_attr}")}
            if _src_table_for_check:
                _plain_ref_forms.add(normalize_sql_expr(f"{_src_table_for_check}.{source_attr}"))
            if derived_expr and (not expr_info.get("expression") or normalize_sql_expr(drd_expr) in _plain_ref_forms):
                drd_expr = derived_expr

        transformed_expr = derive_transformation_expression(row, source_attr)
        _plain_xform_set: set[str] = {"", normalize_sql_expr(f"S.{source_attr}")}
        if _src_table_for_check:
            _plain_xform_set.add(normalize_sql_expr(f"{_src_table_for_check}.{source_attr}"))
        if transformed_expr and (not expr_info.get("expression") or normalize_sql_expr(drd_expr) in _plain_xform_set):
            drd_expr = transformed_expr

        analysis.append(
            {
                "column": col_name,
                "data_type": col.get("data_type") or row.get("target_datatype_oracle") or "VARCHAR2(4000)",
                "nullable": col.get("nullable", True),
                "source_schema": row.get("source_schema") or "",
                "source_table": row.get("source_table") or "",
                "source_attribute": source_attr,
                "logical_name": row.get("logical_name") or "",
                "transformation": row.get("transformation") or "",
                "notes": row.get("notes") or "",
                "drd_expression": drd_expr,
                "source_block": expr_info.get("source_block") or fallback_source_block(row),
                "lookup_join": lookup_join,
                "baseline_test_name": baseline.get("name") or "",
            }
        )
    return analysis


def fallback_source_block(row: Dict[str, Any]) -> str:
    src_schema = (row.get("source_schema") or "").strip()
    src_table = (row.get("source_table") or "").strip()
    if not src_table:
        return "FROM SOURCE_SCHEMA.SOURCE_TABLE"
    fq = f"{src_schema}.{src_table}" if src_schema else src_table
    return f"FROM {fq}"


def fallback_drd_expression(row: Dict[str, Any], source_attr: str) -> str:
    if source_attr in {"NULL", "NONE", "N/A"}:
        return "NULL"
    src_table = (row.get("source_table") or "").strip()
    src_prefix = f"{src_table}." if src_table else ""
    transformation = (row.get("transformation") or "").strip()
    notes = (row.get("notes") or "").strip()
    if transformation:
        if re.search(r"\bCASE\b", transformation, flags=re.IGNORECASE):
            case_expr = _extract_case_expression(transformation)
            if case_expr:
                return normalize_source_expression_aliases(
                    re.sub(r"\b(?:SRC|SOURCE|S)\.", src_prefix, case_expr, flags=re.IGNORECASE),
                    source_schema=(row.get("source_schema") or "").strip(),
                    source_table=src_table,
                )
        literal = extract_literal_expression(transformation)
        if literal:
            return literal
    literal_from_notes = extract_literal_expression(notes)
    if literal_from_notes:
        return literal_from_notes
    if source_attr:
        return f"{src_prefix}{source_attr}" if src_prefix else source_attr
    return "NULL"


def build_control_table_ddl(control_schema: str, target_table: str, target_definition: Dict[str, Any]) -> str:
    cols_sql = []
    for col in target_definition.get("columns", []) or []:
        col_name = (col.get("name") or "").strip().upper()
        if not col_name:
            continue
        data_type = normalize_control_column_type((col.get("data_type") or "VARCHAR2(4000)").strip())
        nullable = col.get("nullable", True)
        nullable_sql = "" if nullable else " NOT NULL"
        cols_sql.append(f"    {col_name} {data_type}{nullable_sql}")
    inner = ",\n".join(cols_sql) if cols_sql else "    ID NUMBER"
    return (
        f"BEGIN\n"
        f"    EXECUTE IMMEDIATE 'DROP TABLE {control_schema}.{target_table} PURGE';\n"
        f"EXCEPTION\n"
        f"    WHEN OTHERS THEN\n"
        f"        IF SQLCODE != -942 THEN\n"
        f"            RAISE;\n"
        f"        END IF;\n"
        f"END;\n"
        f"/\n"
        f"CREATE TABLE {control_schema}.{target_table} (\n{inner}\n);"
    )


def normalize_control_column_type(data_type: str) -> str:
    text = (data_type or "").strip().upper()
    if not text:
        return "VARCHAR2(4000)"
    if text == "VARCHAR2":
        return "VARCHAR2(4000)"
    if text == "NVARCHAR2":
        return "NVARCHAR2(2000)"
    return text


def build_control_insert_sql(
    *,
    control_schema: str,
    target_table: str,
    target_definition: Dict[str, Any],
    analysis_rows: List[Dict[str, Any]],
) -> str:
    row_map = {row["column"]: row for row in analysis_rows}
    source_blocks = [row.get("source_block", "") for row in analysis_rows if row.get("source_block")]
    base_source = Counter(source_blocks).most_common(1)[0][0] if source_blocks else "FROM SOURCE_SCHEMA.SOURCE_TABLE"

    # ── Detect source table alias from DRD expressions ────────────────────
    # DRD expressions frequently reference e.g. OPN_TAX_LOTS_NONBKR_TGT.COLUMN
    # where OPN_TAX_LOTS_NONBKR_TGT is the alias (or bare table name) used in the
    # original ETL query.  We must detect and preserve this in the FROM clause.
    _from_match = re.search(r'\bFROM\s+((?:[A-Z0-9_]+\.)?([A-Z0-9_]+))(?:\s+([A-Z][A-Z0-9_]*))?\s*$',
                            base_source, flags=re.IGNORECASE)
    _src_fq = _from_match.group(1).upper() if _from_match else "SOURCE_SCHEMA.SOURCE_TABLE"
    _src_table_name = _from_match.group(2).upper() if _from_match else "SOURCE_TABLE"
    _src_explicit_alias = (_from_match.group(3) or "").upper() if _from_match else ""

    # Count which prefix is most used in DRD expressions to infer the real alias
    _alias_freq: Dict[str, int] = {}
    for _row in analysis_rows:
        expr = (_row.get("drd_expression") or "").upper()
        for m in re.finditer(r'\b([A-Z][A-Z0-9_]*)\.[A-Z_][A-Z0-9_]*\b', expr):
            prefix = m.group(1)
            if prefix not in {"SYSDATE", "SYSTIMESTAMP", "DUAL", "NULL", "NVL", "CASE", "TO_CHAR", "TO_DATE", "TO_NUMBER"}:
                _alias_freq[prefix] = _alias_freq.get(prefix, 0) + 1

    # The most frequent non-lookup prefix that isn't the schema name is likely the source alias
    _src_schema_parts = set()
    for _r in analysis_rows:
        _ss = (_r.get("source_schema") or "").upper()
        if _ss:
            _src_schema_parts.add(_ss)
    _inferred_alias = ""
    if _alias_freq:
        _sorted_aliases = sorted(_alias_freq.items(), key=lambda x: -x[1])
        for _cand, _cnt in _sorted_aliases:
            if _cand in _src_schema_parts:
                continue
            if _cand == _src_table_name:
                # Expression uses bare table name — no alias needed
                _inferred_alias = ""
                break
            if _cnt >= 3:
                _inferred_alias = _cand
                break

    # Determine the effective alias to use in FROM clause
    _effective_alias = _src_explicit_alias or _inferred_alias
    # Single-letter aliases like S, T are generally from ODI and not useful
    if _effective_alias and len(_effective_alias) == 1:
        _effective_alias = ""

    # Rebuild base_source with alias if needed
    if _effective_alias:
        base_source = f"FROM {_src_fq} {_effective_alias}"
    else:
        base_source = f"FROM {_src_fq}"

    # The name expressions should use to reference source columns
    _src_ref_name = _effective_alias or _src_table_name

    # Build source-staging attribute set (non-lookup tables only) for alias validation.
    source_attr_set: set = set()
    for _row in analysis_rows:
        _src_tbl = (_row.get("source_table") or "").upper()
        _src_attr = (_row.get("source_attribute") or "").upper()
        if _src_attr and _src_tbl and not _is_lookup_table_name(_src_tbl):
            source_attr_set.add(_src_attr)
    col_to_analysis_row: Dict[str, Any] = {
        (r.get("column") or "").upper(): r for r in analysis_rows
    }
    pk_cols: set = {
        (pk or "").strip().upper()
        for pk in (target_definition.get("primary_keys", []) or [])
        if (pk or "").strip()
    }
    col_map_def: Dict[str, Any] = {
        (c.get("name") or "").strip().upper(): c
        for c in (target_definition.get("columns", []) or [])
        if (c.get("name") or "").strip()
    }

    joins = []
    join_alias_map: Dict[str, Tuple[str, str]] = {}
    lk_table_alias_counts: Dict[str, int] = {}  # Track per-table numbering

    # Collect unique raw joins with row context first, then collapse low-quality duplicates.
    raw_join_seen: set[str] = set()
    join_candidates: List[Dict[str, Any]] = []
    for row in analysis_rows:
        lookup_join = sanitize_lookup_join_sql((row.get("lookup_join") or "").strip())
        if not lookup_join or lookup_join in raw_join_seen:
            continue
        raw_join_seen.add(lookup_join)
        old_alias = extract_join_alias(lookup_join)
        _lk_tbl_match = re.search(r'\bJOIN\s+([A-Z0-9_\.\"\$#]+)\b', lookup_join, flags=re.IGNORECASE)
        _lk_fq = (_lk_tbl_match.group(1).replace('"', '').upper() if _lk_tbl_match else "LOOKUP")
        _lk_bare = _lk_fq.split(".")[-1]

        _on_match = re.search(r'\bON\b\s+([\s\S]*)$', lookup_join, flags=re.IGNORECASE)
        _on_text = (_on_match.group(1) if _on_match else "").upper()

        # Source-side key used by this join (if present).
        _src_key_match = re.search(
            rf"\b(?:{re.escape(_src_ref_name)}|{re.escape(_src_table_name)}|S)\.([A-Z0-9_#\$]+)\b",
            _on_text,
            flags=re.IGNORECASE,
        )
        _src_key = (_src_key_match.group(1).upper() if _src_key_match else "")
        _row_src_attr = (row.get("source_attribute") or "").strip().upper()

        # Quality scoring for duplicate collapse.
        _has_source_ref = bool(_src_key)
        _self_join_like = bool(old_alias and re.search(rf"=\s*(?:NVL\(\s*TO_CHAR\(\s*)?{re.escape(old_alias)}\.[A-Z0-9_#\$]+", _on_text, flags=re.IGNORECASE))
        _score = 0
        if _has_source_ref:
            _score += 5
        if _row_src_attr and _src_key and _row_src_attr == _src_key:
            _score += 4
        if re.search(r'\bCL_SCM_ID\b', _on_text, flags=re.IGNORECASE):
            _score += 2
        if not _self_join_like:
            _score += 1

        join_candidates.append({
            "lookup_join": lookup_join,
            "old_alias": old_alias,
            "lookup_fq": _lk_fq,
            "lookup_bare": _lk_bare,
            "src_key": _src_key,
            "has_source_ref": _has_source_ref,
            "score": _score,
        })

    # Collapse competing duplicates by lookup table + source key quality.
    grouped_by_table: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in join_candidates:
        grouped_by_table[c["lookup_bare"]].append(c)

    selected_candidates: List[Dict[str, Any]] = []
    for _lk_table, cands in grouped_by_table.items():
        if not cands:
            continue

        # Prefer joins that actually reference source-side key columns.
        with_source = [c for c in cands if c.get("has_source_ref")]
        pool = with_source if with_source else cands

        # Keep best join per source key to avoid duplicate aliases for same lookup purpose.
        by_src_key: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for c in pool:
            k = c.get("src_key") or "__NO_SRC_KEY__"
            by_src_key[k].append(c)

        for _k, k_cands in by_src_key.items():
            best = max(k_cands, key=lambda x: (x.get("score", 0), len(x.get("lookup_join", ""))))
            selected_candidates.append(best)

    # Stable order for deterministic SQL text.
    selected_candidates.sort(key=lambda x: (x.get("lookup_bare", ""), x.get("src_key", ""), x.get("lookup_join", "")))

    for cand in selected_candidates:
        lookup_join = cand["lookup_join"]
        old_alias = cand.get("old_alias") or ""
        _lk_bare = cand.get("lookup_bare") or "LOOKUP"

        lk_table_alias_counts[_lk_bare] = lk_table_alias_counts.get(_lk_bare, 0) + 1
        new_alias = f"{_lk_bare}_{lk_table_alias_counts[_lk_bare]}"
        join_sql_renamed = replace_join_alias(lookup_join, old_alias, new_alias) if old_alias else lookup_join
        # Normalize S. references in JOIN ON clause to use source ref name
        join_sql_renamed = re.sub(r'\bS\.([A-Z0-9_#\$]+)\b', f'{_src_ref_name}.\\1', join_sql_renamed, flags=re.IGNORECASE)
        # Also fix source table bare name references in JOIN ON
        if _src_table_name != _src_ref_name:
            join_sql_renamed = re.sub(
                r'\b' + re.escape(_src_table_name) + r'\.([A-Z0-9_#\$]+)\b',
                f'{_src_ref_name}.\\1',
                join_sql_renamed,
                flags=re.IGNORECASE,
            )

        # Hard fail-safe: never allow lookup-alias-to-same-lookup-alias ON conditions.
        # Example bad pattern: CL_VAL_1.CL_VAL_ID = CL_VAL_1.CL_VAL_CODE
        _left_alias_ref = re.search(
            rf"\bON\b[\s\S]*?\b{re.escape(new_alias)}\.[A-Z0-9_#\$]+\b",
            join_sql_renamed,
            flags=re.IGNORECASE,
        )
        _right_same_alias = re.search(
            rf"=\s*(?:NVL\(\s*TO_CHAR\(\s*)?{re.escape(new_alias)}\.[A-Z0-9_#\$]+",
            join_sql_renamed,
            flags=re.IGNORECASE,
        )
        _self_join_on = bool(_left_alias_ref and _right_same_alias)
        if _self_join_on:
            _src_key = (cand.get("src_key") or "").strip().upper()
            if _src_key:
                # Auto-rewrite right side to source key when known from DRD/training context.
                join_sql_renamed = re.sub(
                    rf"(=\s*NVL\(\s*TO_CHAR\(\s*){re.escape(new_alias)}\.[A-Z0-9_#\$]+",
                    rf"\1{_src_ref_name}.{_src_key}",
                    join_sql_renamed,
                    flags=re.IGNORECASE,
                    count=1,
                )
                join_sql_renamed = re.sub(
                    rf"(=\s*){re.escape(new_alias)}\.[A-Z0-9_#\$]+",
                    rf"\1{_src_ref_name}.{_src_key}",
                    join_sql_renamed,
                    flags=re.IGNORECASE,
                    count=1,
                )
            else:
                # If we cannot determine a safe source key, neutralize the join.
                join_sql_renamed = re.sub(
                    r"\bON\b[\s\S]*$",
                    "ON 1 = 0",
                    join_sql_renamed,
                    flags=re.IGNORECASE,
                )
        joins.append(join_sql_renamed)
        join_alias_map[lookup_join] = (old_alias, new_alias)

    # Build reverse map: new_alias → table name.
    lk_table_map: Dict[str, str] = {}
    for _jraw, (_old, _new) in join_alias_map.items():
        _tm = re.search(r'\bJOIN\s+([A-Z0-9_\.]+)\s', _jraw, flags=re.IGNORECASE)
        if _tm:
            lk_table_map[_new] = _tm.group(1).upper()

    # Combined alias rename map: old alias (upper) → new alias.
    combined_alias_rename: Dict[str, str] = {}
    for _raw_join, (_old_a, _new_a) in join_alias_map.items():
        if _old_a:
            combined_alias_rename[_old_a.upper()] = _new_a

    # All valid aliases that can appear in generated expressions.
    # Source table name (or alias) + all lookup aliases + fully-qualified schema.table parts
    all_valid_aliases: set = {_src_table_name, _src_ref_name} | set(na for (_, na) in join_alias_map.values())
    # Also allow schema prefixes from FQ references (e.g. TAXLOT_STG_OWNER, COMMON_OWNER)
    for _row in analysis_rows:
        _ss = (_row.get("source_schema") or "").upper()
        if _ss:
            all_valid_aliases.add(_ss)
    # Add bare lookup table names (they may appear in expressions before aliasing)
    for _lk_alias, _lk_fq in lk_table_map.items():
        all_valid_aliases.add(_lk_fq.split(".")[-1].upper())

    select_lines = []
    insert_cols = []
    for col in target_definition.get("columns", []) or []:
        col_name = (col.get("name") or "").strip().upper()
        if not col_name:
            continue
        insert_cols.append(col_name)
        row = row_map.get(col_name, {}) or {}
        expr = row.get("drd_expression") or "NULL"
        lookup_join = sanitize_lookup_join_sql((row.get("lookup_join") or "").strip())
        if lookup_join and lookup_join in join_alias_map:
            old_alias, new_alias = join_alias_map[lookup_join]
            expr = replace_alias_token(expr, old_alias, new_alias)

        # Apply cross-join alias renaming: fix expressions referencing old aliases from OTHER rows' joins.
        for _old_a_u, _new_a in combined_alias_rename.items():
            if re.search(r'\b' + re.escape(_old_a_u) + r'\.[A-Z_]', expr, flags=re.IGNORECASE):
                expr = replace_alias_token(expr, _old_a_u, _new_a)

        # Normalize S. references to use source table ref name (alias or bare table)
        expr = re.sub(r'\bS\.([A-Z0-9_#\$]+)\b', f'{_src_ref_name}.\\1', expr, flags=re.IGNORECASE)

        expr = sanitize_generated_expression(
            expr,
            (row.get("source_attribute") or "").strip().upper(),
            source_schema=(row.get("source_schema") or "").strip(),
            source_table=(row.get("source_table") or "").strip(),
        )

        # Fix TABLE.COL references where COL is not a staging attribute (it belongs to a DIM join).
        src_tbl_for_col = (row.get("source_table") or "").upper()
        if _is_lookup_table_name(src_tbl_for_col):
            def _replace_src_dim_ref(m: re.Match, _src_tbl=src_tbl_for_col, _lk_map=lk_table_map) -> str:
                col_ref = m.group(1).upper()
                if col_ref in source_attr_set:
                    return m.group(0)  # staging column — keep
                # Try to find a lookup alias whose table matches the DIM source.
                for _lk_alias, _lk_tbl in _lk_map.items():
                    _lk_bare = _lk_tbl.split(".")[-1]
                    if _lk_bare == _src_tbl or _lk_tbl == _src_tbl:
                        return f"{_lk_alias}.{m.group(1)}"
                return m.group(0)  # no match found – keep
            expr = re.sub(r'\b' + re.escape(_src_table_name) + r'\.([A-Z0-9_#\$]+)\b', _replace_src_dim_ref, expr, flags=re.IGNORECASE)

        # Detect leftover undefined alias references after all rename attempts.
        _expr_for_check = expr
        _undef_aliases = [
            m.group(1).upper()
            for m in re.finditer(r'\b([A-Z_][A-Z0-9_]*)\.[A-Z_][A-Z0-9_#\$]*\b', _expr_for_check, flags=re.IGNORECASE)
            if m.group(1).upper() not in all_valid_aliases
            and m.group(1).upper() not in {"SYSDATE", "SYSTIMESTAMP", "DUAL"}
        ]
        _has_undef = bool(_undef_aliases)
        if _has_undef:
            # Try one more time: look for any join in join_alias_map whose old_alias matches an undef alias
            for _undef_a in _undef_aliases:
                if _undef_a in combined_alias_rename:
                    expr = replace_alias_token(expr, _undef_a, combined_alias_rename[_undef_a])
            # Recheck after targeted renames
            _still_undef = any(
                m.group(1).upper() not in all_valid_aliases
                and m.group(1).upper() not in {"SYSDATE", "SYSTIMESTAMP", "DUAL"}
                for m in re.finditer(r'\b([A-Z_][A-Z0-9_]*)\.[A-Z_][A-Z0-9_#\$]*\b', expr, flags=re.IGNORECASE)
            )
            if _still_undef:
                _col_def = col_map_def.get(col_name, {})
                if not _col_def.get("nullable", True):
                    _is_pk = col_name in pk_cols or bool(_col_def.get("is_pk"))
                    expr = fallback_non_nullable_expression(col_name, (_col_def.get("data_type") or "").upper(), is_pk=_is_pk)
                else:
                    expr = "NULL"

        if not col.get("nullable", True):
            _is_pk = col_name in pk_cols or bool(col.get("is_pk"))
            fallback_expr = fallback_non_nullable_expression(col_name, (col.get("data_type") or "").strip().upper(), is_pk=_is_pk)
            # Keep mapped expressions intact; only fill when expression truly resolves to NULL.
            if normalize_sql_expr(expr) == "NULL":
                expr = fallback_expr
        select_lines.append(f"    {expr} AS {col_name}")

    join_sql = "\n" + "\n".join(joins) if joins else ""
    sql_text = (
        f"TRUNCATE TABLE {control_schema}.{target_table};\n"
        f"INSERT /*+ APPEND PARALLEL({PARALLEL_HINT_DEGREE}) */ INTO {control_schema}.{target_table} (\n    "
        + ",\n    ".join(insert_cols)
        + f"\n)\nSELECT /*+ PARALLEL({PARALLEL_HINT_DEGREE}) */\n"
        + ",\n".join(select_lines)
        + f"\n{base_source}{join_sql};"
    )
    sql_text = _enforce_not_null_in_insert_sql(sql_text, target_definition)
    return ensure_parallel_hints(sql_text)


def build_control_table_test_defs(
    *,
    target_schema: str,
    target_table: str,
    control_schema: str,
    target_definition: Dict[str, Any],
    main_grain: str,
    ddl_sql: str,
    insert_sql: str,
    analysis_rows: List[Dict[str, Any]],
    source_datasource_id: int,
    target_datasource_id: int,
) -> List[Dict[str, Any]]:
    target_fq = f"{target_schema}.{target_table}" if target_schema else target_table
    control_fq = f"{control_schema}.{target_table}"
    grain_cols = parse_grain_columns(main_grain) or [
        (pk or "").strip().upper() for pk in target_definition.get("primary_keys", []) or [] if (pk or "").strip()
    ]
    if not grain_cols:
        grain_cols = [
            (c.get("name") or "").strip().upper()
            for c in (target_definition.get("columns", []) or [])[:1]
            if (c.get("name") or "").strip()
        ]
    join_sql = "\n        AND ".join(f"T.{c} = CTL.{c}" for c in grain_cols)

    defs = [
        {
            "name": f"Setup: DROP/CREATE control table for {target_table}",
            "test_type": "custom_sql",
            "source_datasource_id": target_datasource_id,
            "target_datasource_id": None,
            "source_query": ddl_sql,
            "target_query": None,
            "expected_result": None,
            "severity": "low",
            "description": f"DDL helper for control table {control_fq}.",
            "is_active": False,
        },
        {
            "name": f"Setup: TRUNCATE/LOAD control table for {target_table}",
            "test_type": "custom_sql",
            "source_datasource_id": source_datasource_id,
            "target_datasource_id": None,
            "source_query": insert_sql,
            "target_query": None,
            "expected_result": None,
            "severity": "low",
            "description": f"Load helper for control table {control_fq}.",
            "is_active": False,
        },
    ]

    for row in analysis_rows:
        col = row["column"]
        if col in AUDIT_COLUMNS:
            continue
        defs.append(
            {
                "name": f"{target_table}: {col} control vs target",
                "test_type": "custom_sql",
                "source_datasource_id": target_datasource_id,
                "target_datasource_id": None,
                "source_query": (
                    f"SELECT /*+ PARALLEL({PARALLEL_HINT_DEGREE}) */ COUNT(*) AS mismatch_count\n"
                    f"FROM {target_fq} T\n"
                    f"JOIN {control_fq} CTL\n"
                    f"    ON {join_sql}\n"
                    f"WHERE NVL(TO_CHAR(T.{col}), '{NVL_NULL_SENTINEL}') <> NVL(TO_CHAR(CTL.{col}), '{NVL_NULL_SENTINEL}')"
                ),
                "target_query": None,
                "expected_result": "0",
                "severity": "high",
                "description": f"Direct attribute comparison for {col} between target and control tables.",
                "column_name": col,
                "is_active": True,
            }
        )
    return defs


def parse_grain_columns(main_grain: str) -> List[str]:
    if not main_grain:
        return []
    cols = []
    seen = set()
    for token in re.split(r"\bAND\b|,|\n|;", main_grain, flags=re.IGNORECASE):
        token = (token or "").strip()
        if not token:
            continue
        if "=" in token:
            left = token.split("=", 1)[0].strip()
            right = token.split("=", 1)[1].strip()
            for side in (left, right):
                match = re.search(r"([A-Z0-9_]+)$", side.upper())
                if match:
                    col = match.group(1)
                    if col not in seen:
                        cols.append(col)
                        seen.add(col)
            continue
        match = re.search(r"([A-Z0-9_]+)$", token.upper())
        if match:
            col = match.group(1)
            if col not in seen:
                cols.append(col)
                seen.add(col)
    return cols


def extract_expr_from_test_sql(
    *,
    sql: str,
    target_schema: str,
    target_table: str,
    target_column: str,
    fallback_source_table: str,
) -> Dict[str, str]:
    if not sql:
        return {"expression": "", "source_block": fallback_source_block({"source_table": fallback_source_table}), "lookup_join": ""}

    target_fq = re.escape(f"{target_schema}.{target_table}" if target_schema else target_table)
    source_block = ""
    # Match FROM ... LEFT JOIN target (with or without alias T)
    source_match = re.search(
        rf"\bFROM\s+(?P<source>.*?)\nLEFT\s+JOIN\s+{target_fq}(?:\s+T)?\b",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if source_match:
        source_block = f"FROM {source_match.group('source').strip()}"

    lookup_join = ""
    # Match lookup joins with various alias patterns (LK, LK1, CL_VAL_1, etc.)
    lookup_match = re.search(
        r"\n(?P<join_type>LEFT\s+JOIN|INNER\s+JOIN)\s+(?P<table>[A-Z0-9_\.\(\) ]+?)\s+(?P<alias>[A-Z_][A-Z0-9_]*)\nON\s+(?P<join_on>.*?)\nWHERE\s+",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if lookup_match:
        _lk_alias = lookup_match.group('alias').strip()
        lookup_join = (
            f"{lookup_match.group('join_type').strip()} {lookup_match.group('table').strip()} {_lk_alias}\n"
            f"ON {lookup_match.group('join_on').strip()}"
        )

    # Match WHERE clause expression (with T. prefix or bare target table name)
    _tgt_col_prefix = f"(?:T|{re.escape(target_table)})"
    rhs_match = re.search(
        rf"WHERE\s+NVL\(TO_CHAR\({_tgt_col_prefix}\.{re.escape(target_column)}\),\s*'[^']*'\)\s*<>\s*NVL\(TO_CHAR\((?P<rhs>.*?)\),\s*'[^']*'\)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    rhs = rhs_match.group("rhs").strip() if rhs_match else ""

    if re.fullmatch(r"[A-Z_][A-Z0-9_]*\.EXPECTEDVAL", rhs.upper()):
        return {
            "expression": rhs,
            "source_block": source_block,
            "lookup_join": lookup_join,
        }
    return {
        "expression": rhs,
        "source_block": source_block,
        "lookup_join": lookup_join,
    }


def compare_insert_variants(
    analysis_rows: List[Dict[str, Any]],
    generated_sql: str,
    manual_sql: str,
    compare_mode: str = "all",
) -> Dict[str, Any]:
    expected_columns = {row.get("column", "").upper() for row in analysis_rows if row.get("column")}
    generated_map = extract_sql_expression_map(generated_sql, expected_aliases=expected_columns)
    manual_supplied = bool(manual_sql.strip())
    manual_map = extract_sql_expression_map(manual_sql, expected_aliases=expected_columns) if manual_supplied else {}
    rows = []
    for row in analysis_rows:
        col = row["column"]
        drd_expr = row.get("drd_expression") or ""
        generated_expr = generated_map.get(col, "")
        manual_expr = manual_map.get(col, "")
        status = compare_column_status(
            drd_expr,
            generated_expr,
            manual_expr,
            manual_supplied=manual_supplied,
            compare_mode=compare_mode,
        )
        recommended = recommend_source(drd_expr, generated_expr, manual_expr, compare_mode=compare_mode)
        rows.append(
            {
                "column": col,
                "source_attribute": (row.get("source_attribute") or "").strip().upper(),
                "drd_expression": drd_expr,
                "generated_expression": generated_expr,
                "manual_expression": manual_expr,
                "status": status,
                "recommended_source": recommended,
                "generated_present": bool(generated_expr.strip()),
                "manual_present": bool(manual_expr.strip()),
            }
        )
    mismatch_count = sum(1 for row in rows if row["status"] != "match_all")
    return {"rows": rows, "mismatch_count": mismatch_count}


def compare_column_status(
    drd_expr: str,
    generated_expr: str,
    manual_expr: str,
    *,
    manual_supplied: bool = False,
    compare_mode: str = "all",
) -> str:
    drd_norm = normalize_sql_expr(drd_expr)
    gen_norm = normalize_sql_expr(generated_expr)
    man_norm = normalize_sql_expr(manual_expr)
    if not generated_expr:
        return "generated_missing"
    if manual_supplied and not manual_expr:
        return "manual_missing"

    manual_only = (compare_mode or "").strip().lower() in {"generated_manual", "manual_generated"}
    if manual_only:
        if manual_expr:
            return "match_all" if gen_norm == man_norm else "generated_mismatch"
        return "generated_missing"

    if manual_expr:
        if drd_norm == gen_norm == man_norm:
            return "match_all"
        if drd_norm == gen_norm and drd_norm != man_norm:
            return "manual_mismatch"
        if drd_norm == man_norm and drd_norm != gen_norm:
            return "generated_mismatch"
        if gen_norm == man_norm and drd_norm != gen_norm:
            return "both_match_each_other_not_drd"
        return "all_different"
    return "match_all" if drd_norm == gen_norm else "generated_mismatch"


def recommend_source(drd_expr: str, generated_expr: str, manual_expr: str, compare_mode: str = "all") -> str:
    drd_norm = normalize_sql_expr(drd_expr)
    gen_norm = normalize_sql_expr(generated_expr)
    man_norm = normalize_sql_expr(manual_expr)
    manual_only = (compare_mode or "").strip().lower() in {"generated_manual", "manual_generated"}
    if manual_only:
        if manual_expr and man_norm != gen_norm:
            return "manual"
        return "generated"
    if manual_expr and man_norm == drd_norm and gen_norm != drd_norm:
        return "manual"
    if gen_norm == drd_norm:
        return "generated"
    return "drd"


def apply_compare_decisions(base_sql: str, decisions: List[Dict[str, str]]) -> str:
    parsed = extract_sql_expression_map(base_sql, include_meta=True)
    select_parts = list(parsed.get("parts", []))
    alias_index = dict(parsed.get("alias_index", {}))
    table_alias_map = extract_sql_table_aliases(base_sql)
    for decision in decisions:
        column = (decision.get("column") or "").strip().upper()
        expr = (decision.get("expression") or "").strip()
        if not column or not expr or column not in alias_index:
            continue
        idx = alias_index[column]
        _, current_expr = parse_select_part(select_parts[idx])
        expr = align_expression_with_sql_aliases(expr, table_alias_map, current_expr=current_expr)
        select_parts[idx] = f"{expr} AS {column}"
    result_sql = rebuild_sql_with_select_parts(base_sql, parsed, select_parts)
    # Also apply column-reference corrections to JOIN ON clauses so that
    # rules that fix a column name (e.g. ACG_TP_CODE → AC_TP_CODE) propagate
    # into the JOIN conditions, not just the SELECT list.
    result_sql = apply_rule_corrections_to_joins(result_sql, decisions)
    return ensure_parallel_hints(result_sql)


def apply_rule_corrections_to_joins(sql_text: str, decisions: List[Dict[str, str]]) -> str:
    """Fix JOIN ON clauses based on rule decisions.

    When a rule replaces an expression for a column, the old *source_attribute*
    referenced in JOIN ON conditions may also be wrong (e.g. the DRD says
    ``ACG_TP_CODE`` but the real source column is ``AC_TP_CODE``).  This
    function extracts the column references from both old and new expressions
    and rewrites JOIN ON clauses accordingly.
    """
    if not decisions or not sql_text:
        return sql_text

    text = sql_text

    # Build column reference rename map from decisions that change a simple
    # column reference (ALIAS.OLD_COL → ALIAS.NEW_COL or bare OLD_COL → NEW_COL).
    col_renames: List[Tuple[str, str]] = []  # (old_ref, new_ref)
    # Hint map for joins: LOOKUP_ALIAS -> source attribute that should be used
    # on the source side of the ON clause when training has corrected it.
    alias_source_hints: Dict[str, set[str]] = {}

    # Resolve source reference used in INSERT ... FROM ...
    source_ref = ""
    _from_match = re.search(r"\bFROM\s+([A-Z0-9_\.\"]+)(?:\s+([A-Z_][A-Z0-9_]*))?", text, flags=re.IGNORECASE)
    if _from_match:
        raw_alias = (_from_match.group(2) or "").replace('"', '').strip().upper()
        if raw_alias in {"LEFT", "RIGHT", "INNER", "OUTER", "JOIN", "WHERE", "GROUP", "ORDER", "HAVING", "ON"}:
            raw_alias = ""
        source_ref = (raw_alias or _from_match.group(1) or "").replace('"', '').strip().upper()
        if "." in source_ref:
            source_ref = source_ref.split(".")[-1]

    for d in decisions:
        old_expr_raw = (d.get("old_expression") or "").strip()
        new_expr = (d.get("expression") or "").strip()
        src_attr = (d.get("source_attribute") or "").strip().upper()
        if not old_expr_raw or not new_expr:
            # Even when old/new expressions do not provide a rename pair,
            # keep source-attribute hints for join ON repair.
            if src_attr:
                new_refs_for_hint = re.findall(r'\b([A-Z_][A-Z0-9_]*)\.([A-Z_][A-Z0-9_]*)\b', new_expr, flags=re.IGNORECASE)
                if new_refs_for_hint:
                    alias_u = (new_refs_for_hint[0][0] or "").upper()
                    if alias_u:
                        alias_source_hints.setdefault(alias_u, set()).add(src_attr)
            continue
        # Extract simple column references: ALIAS.COL or bare COL
        old_refs = re.findall(r'\b([A-Z_][A-Z0-9_]*\.[A-Z_][A-Z0-9_]*)\b', old_expr_raw, flags=re.IGNORECASE)
        new_refs = re.findall(r'\b([A-Z_][A-Z0-9_]*\.[A-Z_][A-Z0-9_]*)\b', new_expr, flags=re.IGNORECASE)

        if src_attr and len(new_refs) >= 1:
            alias_u = new_refs[0].split(".")[0].upper()
            if alias_u:
                alias_source_hints.setdefault(alias_u, set()).add(src_attr)

        if len(old_refs) == 1 and len(new_refs) == 1:
            old_ref = old_refs[0].upper()
            new_ref = new_refs[0].upper()
            if old_ref != new_ref:
                col_renames.append((old_ref, new_ref))
                # Also add bare column name variants
                old_bare = old_ref.split(".")[-1]
                new_bare = new_ref.split(".")[-1]
                old_alias = old_ref.split(".")[0]
                new_alias = new_ref.split(".")[0]
                if old_bare != new_bare and old_alias == new_alias:
                    col_renames.append((old_bare, new_bare))

    # Apply renames/hints to JOIN ON clauses only (not SELECT)
    if col_renames or alias_source_hints:
        # Find all JOIN...ON... blocks
        def _replace_in_joins(match: re.Match) -> str:
            block = match.group(0)
            for old_ref, new_ref in col_renames:
                block = re.sub(r'\b' + re.escape(old_ref) + r'\b', new_ref, block, flags=re.IGNORECASE)

            # Join-level correction from training hints:
            # - fix self-join ON clauses like CL_VAL_X.COL1 = CL_VAL_X.COL2
            # - fix wrong source-side source_ref.BAD_COL to source_ref.CORRECT_COL
            jm = re.search(r"\bJOIN\s+[A-Z0-9_\.\"\$#]+\s+([A-Z_][A-Z0-9_]*)\b", block, flags=re.IGNORECASE)
            join_alias = (jm.group(1) if jm else "").upper()
            hinted_attrs = sorted(alias_source_hints.get(join_alias, set())) if join_alias else []
            hinted_src_attr = hinted_attrs[0] if hinted_attrs else ""
            if hinted_src_attr and source_ref:
                # If ON compares lookup alias to itself, force right side to source_ref.hinted_src_attr.
                left_alias_ref = re.search(
                    rf"\bON\b[\s\S]*?\b{re.escape(join_alias)}\.[A-Z0-9_]+\b",
                    block,
                    flags=re.IGNORECASE,
                )
                right_same_alias = re.search(
                    rf"=\s*(?:NVL\(\s*TO_CHAR\(\s*)?{re.escape(join_alias)}\.[A-Z0-9_]+",
                    block,
                    flags=re.IGNORECASE,
                )
                self_cmp = bool(left_alias_ref and right_same_alias)
                if self_cmp:
                    block = re.sub(
                        rf"(=\s*NVL\(\s*TO_CHAR\(\s*){re.escape(join_alias)}\.[A-Z0-9_]+",
                        rf"\1{source_ref}.{hinted_src_attr}",
                        block,
                        flags=re.IGNORECASE,
                        count=1,
                    )
                    block = re.sub(
                        rf"(=\s*){re.escape(join_alias)}\.[A-Z0-9_]+",
                        rf"\1{source_ref}.{hinted_src_attr}",
                        block,
                        flags=re.IGNORECASE,
                        count=1,
                    )

                # If source side already exists but uses a different column, align it to hinted source attr.
                block = re.sub(
                    rf"\b{re.escape(source_ref)}\.[A-Z0-9_]+\b",
                    f"{source_ref}.{hinted_src_attr}",
                    block,
                    flags=re.IGNORECASE,
                    count=1,
                )
            return block

        # Match JOIN ... ON ... up to the next JOIN or FROM or WHERE or ;
        text = re.sub(
            r'(\bJOIN\b.*?\bON\b.*?)(?=\bJOIN\b|\bWHERE\b|;|\Z)',
            _replace_in_joins,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

    return text


def apply_sql_variant_preserving_joins(base_sql: str, variant_sql: str) -> str:
    """Apply SELECT-list expressions from variant_sql onto base_sql, preserving base FROM/JOIN structure."""
    base_parsed = extract_sql_expression_map(base_sql, include_meta=True)
    variant_map = extract_sql_expression_map(variant_sql)
    if not base_parsed or not variant_map:
        return ensure_parallel_hints(base_sql)

    select_parts = list(base_parsed.get("parts", []))
    alias_index = dict(base_parsed.get("alias_index", {}))
    table_alias_map = extract_sql_table_aliases(base_sql)

    for column, expr in variant_map.items():
        col_u = (column or "").strip().upper()
        if not col_u or col_u not in alias_index:
            continue
        idx = alias_index[col_u]
        _, current_expr = parse_select_part(select_parts[idx])
        aligned = align_expression_with_sql_aliases(expr, table_alias_map, current_expr=current_expr)
        select_parts[idx] = f"{aligned} AS {col_u}"

    return ensure_parallel_hints(rebuild_sql_with_select_parts(base_sql, base_parsed, select_parts))


def ensure_parallel_hints(sql_text: str) -> str:
    text = str(sql_text or "")
    if not text.strip():
        return text

    if re.search(r"\bINSERT\b[^\n;]*?\bINTO\b", text, flags=re.IGNORECASE) and "PARALLEL(" not in text.upper():
        text = re.sub(
            r"\bINSERT\s+INTO\b",
            f"INSERT /*+ APPEND PARALLEL({PARALLEL_HINT_DEGREE}) */ INTO",
            text,
            flags=re.IGNORECASE,
            count=1,
        )

    if re.search(r"\bINSERT\b", text, flags=re.IGNORECASE):
        text = re.sub(
            r"\bSELECT\b(?!\s*/\*\+)",
            f"SELECT /*+ PARALLEL({PARALLEL_HINT_DEGREE}) */",
            text,
            flags=re.IGNORECASE,
            count=1,
        )
    return text


def extract_sql_table_aliases(sql_text: str) -> Dict[str, str]:
    """Return mapping of table names/FQNs to aliases used by the SQL.

    Handles both aliased (``FROM schema.table ALIAS``) and un-aliased
    (``FROM schema.table``) references.  For un-aliased tables the bare
    table name is used as both the key and the value so that downstream
    callers can treat it uniformly.
    """
    mapping: Dict[str, str] = {}
    # Match tables with explicit aliases
    for m in re.finditer(
        r"\b(?:FROM|JOIN)\s+([A-Z0-9_\.\"]+)\s+([A-Z][A-Z0-9_]*)\b",
        (sql_text or ""),
        flags=re.IGNORECASE,
    ):
        table_ref = (m.group(1) or "").strip().strip('"')
        alias = (m.group(2) or "").strip().upper()
        if not table_ref or not alias:
            continue
        # Skip if the "alias" is actually a SQL keyword (ON, WHERE, etc.)
        if alias in {"ON", "WHERE", "AND", "OR", "LEFT", "INNER", "RIGHT", "OUTER", "CROSS", "FULL", "SET", "GROUP", "ORDER", "HAVING", "UNION", "EXCEPT", "INTERSECT"}:
            # Treat as un-aliased; bare table name is the alias
            table_ref_u = table_ref.upper()
            bare = table_ref_u.split(".")[-1]
            if bare and bare not in mapping:
                mapping[table_ref_u] = bare
                mapping[bare] = bare
            continue
        table_ref_u = table_ref.upper()
        mapping[table_ref_u] = alias
        bare = table_ref_u.split(".")[-1]
        if bare and bare not in mapping:
            mapping[bare] = alias

    # Also capture un-aliased FROM/JOIN references (table followed by newline, ON, WHERE, etc.)
    for m in re.finditer(
        r"\b(?:FROM|JOIN)\s+([A-Z0-9_\.\"]+)\s*(?:\n|\r|$|(?=\bON\b)|(?=\bWHERE\b)|(?=\bLEFT\b)|(?=\bINNER\b)|(?=\bJOIN\b)|(?=\bGROUP\b)|(?=\bORDER\b))",
        (sql_text or ""),
        flags=re.IGNORECASE,
    ):
        table_ref = (m.group(1) or "").strip().strip('"')
        if not table_ref:
            continue
        table_ref_u = table_ref.upper()
        bare = table_ref_u.split(".")[-1]
        if bare and bare not in mapping:
            mapping[table_ref_u] = bare
            mapping[bare] = bare
    return mapping


def align_expression_with_sql_aliases(expr: str, table_alias_map: Dict[str, str], current_expr: str = "") -> str:
    """Rewrite table-qualified references to aliases that exist in current SQL."""
    text = (expr or "").strip()
    if not text:
        return text
    if not table_alias_map:
        return text

    # Detect preferred lookup alias from the current expression (TABLE_NAME_N or legacy LK# pattern)
    preferred_lookup_alias = ""
    m_pref = re.search(r"\b([A-Z_][A-Z0-9_]*_\d+)\.", (current_expr or ""), flags=re.IGNORECASE)
    if not m_pref:
        m_pref = re.search(r"\b(LK\d*)\.", (current_expr or ""), flags=re.IGNORECASE)
    if m_pref:
        preferred_lookup_alias = m_pref.group(1).upper()

    # Build set of known SQL aliases (values in the map) for later guard
    known_aliases = {v.upper() for v in table_alias_map.values() if v}

    keys = sorted(table_alias_map.keys(), key=len, reverse=True)
    for key in keys:
        alias = table_alias_map.get(key, "")
        if not alias:
            continue
        alias_u = alias.upper()
        # If preferred lookup alias matches the same lookup table family, use it
        is_lookup_alias = re.fullmatch(r"[A-Z_][A-Z0-9_]*_\d+", alias_u) or re.fullmatch(r"LK\d*", alias_u)
        target_alias = preferred_lookup_alias if (preferred_lookup_alias and is_lookup_alias) else alias
        text = re.sub(rf"\b{re.escape(key)}\.", f"{target_alias}.", text, flags=re.IGNORECASE)

    # If a preferred lookup alias exists, normalize any remaining non-source
    # qualifiers (that aren't already known aliases) to that lookup alias.
    if preferred_lookup_alias:
        def _rewrite_unknown(m: re.Match) -> str:
            ref = m.group(1).upper()
            if ref in known_aliases:
                return m.group(0)
            return f"{preferred_lookup_alias}."

        text = re.sub(
            r"\b([A-Z_][A-Z0-9_]*)\.",
            _rewrite_unknown,
            text,
            flags=re.IGNORECASE,
        )

    return text


def extract_sql_expression_map(
    sql_text: str,
    include_meta: bool = False,
    expected_aliases: Optional[set[str]] = None,
) -> Dict[str, Any]:
    if not sql_text.strip():
        return {} if not include_meta else {"map": {}, "parts": [], "alias_index": {}}

    insert_columns = parse_insert_target_columns(sql_text)
    candidates = []
    for match in re.finditer(r"\bSELECT\b(?P<select>.*?)\bFROM\b", sql_text, flags=re.IGNORECASE | re.DOTALL):
        clause = match.group("select")
        parts = split_sql_columns(clause)
        alias_map = {}
        alias_index = {}
        alias_count = 0
        for idx, part in enumerate(parts):
            alias, expr = parse_select_part(part)
            if alias:
                alias_count += 1
                alias_map[alias] = expr
                alias_index[alias] = idx
        if alias_map:
            alias_map = _expand_select_alias_expressions(alias_map)
        # Manual SQL often omits SELECT aliases in INSERT ... SELECT blocks.
        # Fall back to positional mapping using the INSERT target column list.
        if alias_count == 0 and insert_columns:
            for idx, column in enumerate(insert_columns):
                if idx >= len(parts):
                    break
                alias_map[column] = parts[idx].strip()
                alias_index[column] = idx
            alias_count = len(alias_map)
        candidates.append(
            {
                "select_start": match.start("select"),
                "select_end": match.end("select"),
                "parts": parts,
                "map": alias_map,
                "alias_index": alias_index,
                "score": (
                    len(set(alias_map.keys()) & {a.upper() for a in (expected_aliases or set())}),
                    alias_count,
                    len(parts),
                    len(clause),
                ),
            }
        )
    if not candidates:
        return {} if not include_meta else {"map": {}, "parts": [], "alias_index": {}}

    best = max(candidates, key=lambda item: item["score"])
    if include_meta:
        return best
    return best["map"]


def rebuild_sql_with_select_parts(sql_text: str, parsed: Dict[str, Any], select_parts: List[str]) -> str:
    select_sql = "\n    " + ",\n    ".join(part.strip() for part in select_parts) + "\n"
    return sql_text[: parsed["select_start"]] + select_sql + sql_text[parsed["select_end"] :]


def parse_select_part(part: str) -> Tuple[str, str]:
    text = (part or "").strip()
    if not text:
        return "", ""
    text = re.sub(r"^/\*\+.*?\*/\s*", "", text, flags=re.DOTALL).strip()
    match = re.search(r"\bAS\s+(?P<alias>\"?[A-Z0-9_]+\"?)\s*$", text, flags=re.IGNORECASE)
    if match:
        alias = match.group("alias").strip('"').upper()
        expr = text[: match.start()].strip()
        return alias, expr
    implicit = re.search(r"(?P<expr>.+?)\s+(?P<alias>[A-Z0-9_]+)\s*$", text, flags=re.IGNORECASE | re.DOTALL)
    if implicit:
        alias = implicit.group("alias").upper()
        expr = implicit.group("expr").strip()
        if alias not in {"END", "NULL"}:
            return alias, expr
    return "", text


def split_sql_columns(clause: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    in_single = False
    in_double = False
    for ch in clause:
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            continue
        if not in_single and not in_double:
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
                continue
        current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def parse_insert_target_columns(sql_text: str) -> List[str]:
    match = re.search(
        r"\bINSERT\b.*?\bINTO\b[^\(]*\((?P<cols>.*?)\)\s*\bSELECT\b",
        sql_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    raw_cols = split_sql_columns(match.group("cols"))
    columns: List[str] = []
    for col in raw_cols:
        token = re.sub(r"\s+", " ", col.strip())
        token = token.split(".")[-1].strip().strip('"').upper()
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", token):
            columns.append(token)
    return columns


def normalize_sql_expr(expr: str) -> str:
    text = (expr or "").strip().upper()
    text = strip_sql_qualifiers(text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([(),=+*/<>-])\s*", r"\1", text)
    text = normalize_multiplier_case_expr(text)
    text = normalize_case_expr(text)
    return text


def normalize_multiplier_case_expr(text: str) -> str:
    """Canonicalize value*CASE(...THEN 1 ELSE X END) into inline CASE form.

    This helps compare expressions where SQL factors repeated FX logic into
    a helper multiplier alias versus inline CASE branches.
    """
    raw = (text or "").strip()
    if not raw:
        return raw

    patt = re.compile(
        r"^(?P<lhs>.+?)\*\((?P<case>CASE\s+WHEN\s+(?P<cond>.+?)\s+THEN\s+1\s+ELSE\s+(?P<else_expr>.+?)\s+END)\)$",
        flags=re.IGNORECASE,
    )
    m = patt.match(raw)
    if m:
        lhs = m.group("lhs").strip()
        cond = m.group("cond").strip()
        else_expr = m.group("else_expr").strip()
        return f"CASE WHEN {cond} THEN {lhs} ELSE {lhs}*{else_expr} END"

    patt_rev = re.compile(
        r"^\((?P<case>CASE\s+WHEN\s+(?P<cond>.+?)\s+THEN\s+1\s+ELSE\s+(?P<else_expr>.+?)\s+END)\)\*(?P<rhs>.+)$",
        flags=re.IGNORECASE,
    )
    m2 = patt_rev.match(raw)
    if m2:
        rhs = m2.group("rhs").strip()
        cond = m2.group("cond").strip()
        else_expr = m2.group("else_expr").strip()
        return f"CASE WHEN {cond} THEN {rhs} ELSE {rhs}*{else_expr} END"

    return raw


def _expand_select_alias_expressions(alias_map: Dict[str, str]) -> Dict[str, str]:
    """Inline helper select aliases such as FX_MULTIPLIER for better comparison."""
    resolved: Dict[str, str] = {}
    visiting: set[str] = set()
    identifier_pattern = re.compile(r"(?<![\.\w\$#\"])\b([A-Z_][A-Z0-9_]*)\b(?!\s*\()(?!\.)")

    def resolve(alias: str) -> str:
        key = (alias or "").strip().upper()
        if not key or key not in alias_map:
            return ""
        if key in resolved:
            return resolved[key]
        if key in visiting:
            return alias_map[key]

        visiting.add(key)
        expr = alias_map.get(key, "")

        def repl(match: re.Match) -> str:
            token = (match.group(1) or "").upper()
            if not token or token == key:
                return match.group(0)
            if token in _SQL_IDENTIFIER_ALLOWLIST:
                return match.group(0)
            if token not in alias_map:
                return match.group(0)
            nested = resolve(token)
            if not nested:
                return match.group(0)
            return f"({nested})"

        expanded = re.sub(identifier_pattern, repl, expr)
        visiting.remove(key)
        resolved[key] = expanded
        return expanded

    for alias in list(alias_map.keys()):
        resolve(alias)
    return resolved


def normalize_case_expr(text: str) -> str:
    """Normalize a simple (non-nested) CASE expression by sorting WHEN arms.

    This makes order-insensitive comparison work: two CASE expressions with the
    same branches in different order will normalize to the same canonical string.
    Nested CASEs (more than one CASE keyword) are returned unchanged.
    """
    if not re.match(r"^CASE\b", text, re.IGNORECASE):
        return text
    # Bail out on nested CASEs – too complex for simple sorting
    if len(re.findall(r"\bCASE\b", text, re.IGNORECASE)) > 1:
        return text

    # Split on WHEN / THEN / ELSE keywords; re.split with a capturing group
    # preserves the separators in the result list.
    segments = re.split(r"\b(WHEN|THEN|ELSE)\b", text, flags=re.IGNORECASE)
    if len(segments) < 5:
        return text  # not enough structure

    arms: list[tuple[str, str]] = []
    else_val: str | None = None
    i = 0
    while i < len(segments):
        seg = (segments[i] or "").strip().upper()
        if seg == "WHEN":
            cond_raw = segments[i + 1].strip() if i + 1 < len(segments) else ""
            then_kw = (segments[i + 2] or "").strip().upper() if i + 2 < len(segments) else ""
            if then_kw != "THEN":
                return text  # unexpected structure
            val_raw = segments[i + 3].strip() if i + 3 < len(segments) else ""
            # Strip trailing END from the last THEN value
            val = re.sub(r"\bEND\s*$", "", val_raw, flags=re.IGNORECASE).strip()
            arms.append((cond_raw, val))
            i += 4
        elif seg == "ELSE":
            else_raw = segments[i + 1].strip() if i + 1 < len(segments) else ""
            else_val = re.sub(r"\bEND\s*$", "", else_raw, flags=re.IGNORECASE).strip()
            i += 2
        else:
            i += 1

    if not arms:
        return text

    # Sort arms for order-independent comparison
    sorted_arms = sorted(arms)
    result = "CASE " + " ".join(f"WHEN {w} THEN {t}" for w, t in sorted_arms)
    if else_val is not None:
        result += f" ELSE {else_val}"
    result += " END"
    return result


def strip_sql_qualifiers(text: str) -> str:
    out = text or ""
    pattern = re.compile(r"\b([A-Z_][A-Z0-9_\$#\"]*)\.(\"?[A-Z_][A-Z0-9_\$#]*\"?)\b")
    # Repeatedly collapse qualified references (schema.table.col -> col)
    # while leaving function calls and numeric literals intact.
    for _ in range(4):
        updated = re.sub(pattern, r"\2", out)
        if updated == out:
            break
        out = updated
    return out


def extract_literal_expression(text: str) -> Optional[str]:
    raw = (text or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"'.*'", raw, flags=re.DOTALL):
        return raw
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return raw
    return None


def _logical_to_physical(logical_name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (logical_name or "").upper()).strip("_")


def extract_join_alias(join_sql: str) -> str:
    m = re.search(r"\bJOIN\s+[A-Z0-9_\.\(\)\" ]+\s+([A-Z0-9_]+)\s*\nON\b", join_sql, flags=re.IGNORECASE)
    return (m.group(1) if m else "").upper()


def replace_alias_token(sql: str, old_alias: str, new_alias: str) -> str:
    if not old_alias or not new_alias or old_alias.upper() == new_alias.upper():
        return sql
    pattern = rf"\b{re.escape(old_alias)}\."
    return re.sub(pattern, f"{new_alias}.", sql, flags=re.IGNORECASE)


def replace_join_alias(sql: str, old_alias: str, new_alias: str) -> str:
    if not old_alias or not new_alias or old_alias.upper() == new_alias.upper():
        return sql
    out = re.sub(
        rf"(\bJOIN\s+[A-Z0-9_\.\(\)\" ]+\s+){re.escape(old_alias)}(\b)",
        rf"\1{new_alias}\2",
        sql,
        flags=re.IGNORECASE,
        count=1,
    )
    return replace_alias_token(out, old_alias, new_alias)


def sanitize_lookup_join_sql(join_sql: str) -> str:
    text = (join_sql or "").strip()
    return text


def dedupe_insert_join_blocks(sql_text: str) -> str:
    """Remove duplicate JOIN blocks in INSERT...SELECT SQL, preferring the last block per alias.

    This protects restored/edited SQL from repeated coach-apply artifacts where
    aliases like LK1..LK5/CL_VAL3 appear multiple times.
    """
    text = (sql_text or "").replace("\r\n", "\n")
    if not text.strip():
        return text

    lines = text.split("\n")
    start_re = re.compile(r"^\s*(LEFT(\s+OUTER)?\s+JOIN|INNER\s+JOIN|JOIN)\b", flags=re.IGNORECASE)
    cont_re = re.compile(r"^\s*(ON|AND|OR)\b", flags=re.IGNORECASE)

    blocks: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        if not start_re.search(lines[i] or ""):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and cont_re.search(lines[j] or ""):
            j += 1

        head = lines[i] or ""
        m = re.search(r"\bJOIN\s+([A-Z0-9_\.\"\$#]+)(?:\s+([A-Z][A-Z0-9_]*))?\b", head, flags=re.IGNORECASE)
        table = (m.group(1) if m else "").replace('"', '').upper()
        alias = (m.group(2) if m and m.group(2) else "").upper()
        key = f"ALIAS:{alias}" if alias else (f"TABLE:{table}" if table else "")

        blocks.append({"start": i, "end": j - 1, "key": key})
        i = j

    if not blocks:
        return text

    keep_ranges: set = set()
    seen_keys: set = set()
    for blk in reversed(blocks):
        k = blk["key"]
        sig = k or f"RANGE:{blk['start']}:{blk['end']}"
        if sig in seen_keys:
            continue
        seen_keys.add(sig)
        keep_ranges.add((blk["start"], blk["end"]))

    drop = [False] * len(lines)
    for blk in blocks:
        rng = (blk["start"], blk["end"])
        if rng in keep_ranges:
            continue
        for idx in range(blk["start"], blk["end"] + 1):
            drop[idx] = True

    out_lines = [line for idx, line in enumerate(lines) if not drop[idx]]
    return "\n".join(out_lines)


def validate_insert_join_aliases(sql_text: str) -> List[Dict[str, str]]:
    """Scan an INSERT...SELECT for alias references in the SELECT clause that are not defined in
    the FROM/JOIN section.  Returns a list of {alias, column} dicts for each violation."""
    if not sql_text:
        return []
    # Extract defined aliases from FROM / JOIN lines (both explicit aliases and bare table names)
    defined: set = set()
    for m in re.finditer(
        r'\b(?:FROM|JOIN)\s+(?:[A-Z0-9_]+\.)?([A-Z0-9_]+)(?:\s+([A-Z_][A-Z0-9_]*))?\b',
        sql_text, flags=re.IGNORECASE,
    ):
        bare_table = m.group(1).upper()
        alias = (m.group(2) or "").upper()
        if alias:
            defined.add(alias)
        defined.add(bare_table)  # Also allow referencing by bare table name
    # Skip constants / system names
    skip = {"SYSDATE", "SYSTIMESTAMP", "DUAL", "SYS", "NULL"}
    issues: List[Dict[str, str]] = []
    seen_issues: set = set()
    # Only scan the SELECT part (between SELECT and FROM)
    select_match = re.search(r'\bSELECT\b([\s\S]*?)\bFROM\b', sql_text, flags=re.IGNORECASE)
    scan_text = select_match.group(1) if select_match else sql_text
    for m in re.finditer(r'\b([A-Z_][A-Z0-9_]*)\.([A-Z_][A-Z0-9_\$#]*)\b', scan_text, flags=re.IGNORECASE):
        alias = m.group(1).upper()
        col = m.group(2).upper()
        if alias in defined or alias in skip:
            continue
        key = f"{alias}.{col}"
        if key not in seen_issues:
            seen_issues.add(key)
            issues.append({"alias": alias, "column": col})
    return issues


def fallback_non_nullable_expression(col_name: str, data_type: str, is_pk: bool = False) -> str:
    if is_pk:
        return "ROWNUM"
    if "DATE" in data_type or "TIMESTAMP" in data_type:
        return "SYSDATE"
    if any(token in data_type for token in ("CHAR", "CLOB", "TEXT", "VARCHAR")):
        return "'N/A'"
    return "0"


def _enforce_not_null_in_insert_sql(sql: str, target_definition: Dict[str, Any]) -> str:
    """Post-process any INSERT SQL: replace NULL AS <NOT_NULL_COL> with correct fallback.

    Handles both freshly generated and previously saved SQL strings so that
    stale states never produce ORA-01400 errors."""
    if not sql or not target_definition:
        return sql
    col_map: Dict[str, Any] = {
        (c.get("name") or "").strip().upper(): c
        for c in (target_definition.get("columns", []) or [])
        if (c.get("name") or "").strip()
    }
    pk_cols: set = {
        (pk or "").strip().upper()
        for pk in (target_definition.get("primary_keys", []) or [])
        if (pk or "").strip()
    }
    result = sql
    for col_name, col in col_map.items():
        if col.get("nullable", True):
            continue
        dtype = (col.get("data_type") or "").strip().upper()
        is_pk = col_name in pk_cols or bool(col.get("is_pk"))
        pattern = re.compile(
            r'\bNULL\s+AS\s+' + re.escape(col_name) + r'\b',
            flags=re.IGNORECASE,
        )
        fb = fallback_non_nullable_expression(col_name, dtype, is_pk=is_pk)
        result = pattern.sub(f"{fb} AS {col_name}", result)
    return result


def normalize_insert_source_target_aliases(sql_text: str) -> str:
    """Normalize INSERT SQL to avoid S/T aliases on source and target tables."""
    text = str(sql_text or "")
    if not text.strip():
        return text

    # Resolve source table from FROM clause.
    src_match = re.search(r"\bFROM\s+([A-Z0-9_\.\"\$#]+)", text, flags=re.IGNORECASE)
    src_ref = (src_match.group(1) or "").replace('"', '').strip() if src_match else ""
    src_table = src_ref.split(".")[-1].upper() if src_ref else ""

    # Resolve target table from INSERT INTO clause.
    tgt_match = re.search(r"\bINSERT\b.*?\bINTO\b\s+([A-Z0-9_\.\"\$#]+)", text, flags=re.IGNORECASE | re.DOTALL)
    tgt_ref = (tgt_match.group(1) or "").replace('"', '').strip() if tgt_match else ""
    tgt_table = tgt_ref.split(".")[-1].upper() if tgt_ref else ""

    # Drop explicit source/target aliases in FROM/JOIN clauses when they are S/T.
    if src_ref:
        text = re.sub(
            rf"(\bFROM\s+{re.escape(src_ref)})\s+S\b",
            r"\1",
            text,
            flags=re.IGNORECASE,
        )
    if tgt_ref:
        text = re.sub(
            rf"(\b(?:LEFT\s+JOIN|INNER\s+JOIN|JOIN)\s+{re.escape(tgt_ref)})\s+T\b",
            r"\1",
            text,
            flags=re.IGNORECASE,
        )

    # Rewrite lingering S./T. references to table-name qualifiers.
    if src_table:
        text = re.sub(r"\bS\.([A-Z0-9_\$#]+)\b", rf"{src_table}.\1", text, flags=re.IGNORECASE)
    if tgt_table:
        text = re.sub(r"\bT\.([A-Z0-9_\$#]+)\b", rf"{tgt_table}.\1", text, flags=re.IGNORECASE)

    return text


def parse_source_table_from_block(source_block: str) -> Tuple[str, str]:
    text = (source_block or "").strip()
    if not text:
        return "", ""
    # Try with alias S first, then without alias
    match = re.search(r"\bFROM\s+([A-Z0-9_\.]+)\s+S\b", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"\bFROM\s+([A-Z0-9_\.]+)\b", text, flags=re.IGNORECASE)
    if not match:
        return "", ""
    return split_fq_table(match.group(1))


def infer_lookup_source_key_from_text(transformation: str, fallback: str = "") -> str:
    upper = (transformation or "").upper()
    if not upper:
        return (fallback or "").strip().upper()

    explicit = re.search(r"\b(?:USE\s+)?([A-Z0-9_]*_ID)\s+(?:IN|UNDER)\s+CL_VAL(?:_ID)?\b", upper)
    if explicit:
        return explicit.group(1)

    eq_match = re.search(r"\bCL_VAL_ID\s*=\s*([A-Z0-9_]*_ID)\b", upper)
    if eq_match:
        return eq_match.group(1)
    eq_match_rev = re.search(r"\b([A-Z0-9_]*_ID)\s*=\s*CL_VAL_ID\b", upper)
    if eq_match_rev:
        return eq_match_rev.group(1)

    skip = {"CL_VAL_ID", "CL_SCM_ID", "SCM_ID"}
    for token in re.findall(r"\b([A-Z0-9_]*_ID)\b", upper):
        if token not in skip:
            return token
    return (fallback or "").strip().upper()


def derive_transformation_expression(row: Dict[str, Any], source_attr: str) -> str:
    transformation = (row.get("transformation") or "").strip()
    notes = (row.get("notes") or "").strip()
    source_table = (row.get("source_table") or "").strip()
    if transformation:
        case_expr = _extract_case_expression(transformation)
        if not case_expr and re.search(r"\bCASE\b", transformation, flags=re.IGNORECASE):
            loose = re.search(r"\bCASE\b[\s\S]*?\bEND\b", transformation, flags=re.IGNORECASE)
            case_expr = loose.group(0).strip() if loose else ""
        if case_expr:
            seed_alias_match = re.search(r"^\s*([A-Z0-9_]+)\s*,", transformation, flags=re.IGNORECASE)
            seed_alias = (seed_alias_match.group(1) if seed_alias_match else "").upper()
            case_expr = _normalize_case_expression_tokens(case_expr, source_attr=source_attr, seed_alias=seed_alias)
            src_prefix = f"{source_table}." if source_table else ""
            normalized = normalize_source_expression_aliases(
                re.sub(r"\b(?:SRC|SOURCE|S)\.", src_prefix, case_expr, flags=re.IGNORECASE),
                source_schema=(row.get("source_schema") or "").strip(),
                source_table=source_table,
            )
            return normalized if is_safe_transformation_expression(normalized, source_attr) else ""

    combined = f"{transformation} {notes}".upper()
    if "DEFAULT TO" in combined or "POPULATE AS" in combined:
        literal = extract_literal_expression(combined)
        if literal:
            return literal
        m = re.search(r"(?:DEFAULT\s+TO|POPULATE\s+AS)\s+([A-Z0-9_]+)", combined)
        if m:
            token = m.group(1)
            return token if re.fullmatch(r"-?\d+(?:\.\d+)?", token) else f"'{token}'"

    if source_attr in {"NULL", "NONE", "N/A"}:
        return "NULL"
    return ""


def sanitize_generated_expression(
    expr: str,
    source_attr: str,
    *,
    source_schema: str = "",
    source_table: str = "",
) -> str:
    text = (expr or "").strip()
    if not text:
        return "NULL"
    text = normalize_source_expression_aliases(text, source_schema=source_schema, source_table=source_table)
    if re.search(r"\bCASE\b", text, flags=re.IGNORECASE):
        return text
    if is_safe_transformation_expression(text, source_attr):
        return text
    if source_attr and source_attr not in {"NULL", "NONE", "N/A"}:
        src_prefix = f"{source_table}." if source_table else ""
        return f"{src_prefix}{source_attr}" if src_prefix else source_attr
    return "NULL"


def normalize_source_expression_aliases(expr: str, *, source_schema: str = "", source_table: str = "") -> str:
    """Normalize source table/alias references in expressions to use bare table name (no alias)."""
    text = (expr or "").strip()
    if not text:
        return text

    schema_u = (source_schema or "").strip().upper()
    table_u = (source_table or "").strip().upper()
    if table_u and "." in table_u:
        schema_part, table_part = split_fq_table(table_u)
        if table_part:
            table_u = table_part
        if not schema_u and schema_part:
            schema_u = schema_part

    # Replace fully qualified schema.table. references with bare table.
    if schema_u and table_u:
        text = re.sub(
            rf"\b{re.escape(schema_u)}\.{re.escape(table_u)}\.",
            f"{table_u}.",
            text,
            flags=re.IGNORECASE,
        )

    # Replace S. alias references with bare table name.
    if table_u:
        text = re.sub(r"\bS\.", f"{table_u}.", text, flags=re.IGNORECASE)

    # If a single unknown alias remains, replace it with table name.
    lk_pattern = re.compile(r'^(?:LK\d*|[A-Z_]+_\d+)$')
    aliases = {
        a.upper()
        for a in re.findall(r"\b([A-Z_][A-Z0-9_]*)\.", text, flags=re.IGNORECASE)
        if a and a.upper() != table_u and not lk_pattern.match(a.upper())
    }
    if len(aliases) == 1 and table_u:
        only_alias = next(iter(aliases))
        text = re.sub(rf"\b{re.escape(only_alias)}\.", f"{table_u}.", text, flags=re.IGNORECASE)
    return text


def _normalize_case_expression_tokens(case_expr: str, *, source_attr: str = "", seed_alias: str = "") -> str:
    """Normalize raw CASE expressions extracted from DRD free-text."""
    text = (case_expr or "").strip()
    if not text:
        return text

    # Common DRD shorthand/typos observed in tax-lot mappings.
    replacements = {
        "SBC_SBC_EXG_RATE": "SBC_EXG_RATE",
        "CCY_CODE": "NML_ISO_CCY_CODE",
    }
    for old, new in replacements.items():
        text = re.sub(rf"\b{old}\b", new, text, flags=re.IGNORECASE)

    if seed_alias and source_attr:
        text = re.sub(rf"(?<!\.)\b{re.escape(seed_alias)}\b(?!\s*\()", f"S.{source_attr}", text, flags=re.IGNORECASE)

    # Protect string literals so tokens like 'USD' are not rewritten.
    literals: List[str] = []

    def _stash_literal(match: re.Match) -> str:
        literals.append(match.group(0))
        return f"__LIT_{len(literals)-1}__"

    text_masked = re.sub(r"'(?:''|[^'])*'", _stash_literal, text)

    identifier_pattern = re.compile(r"(?<![\.\w\$#\"])\b([A-Z_][A-Z0-9_]*)\b(?!\s*\()(?!\.)")

    def repl(match: re.Match) -> str:
        token = (match.group(1) or "").upper()
        if token.startswith("__LIT_"):
            return match.group(0)
        if token in _SQL_IDENTIFIER_ALLOWLIST:
            return match.group(0)
        if token == (source_attr or "").strip().upper():
            return f"S.{token}"
        if re.fullmatch(r"LK\d*", token):
            return match.group(0)
        return f"S.{token}"

    normalized = re.sub(identifier_pattern, repl, text_masked)
    for idx, lit in enumerate(literals):
        normalized = normalized.replace(f"__LIT_{idx}__", lit)
    return normalized


def is_safe_transformation_expression(expr: str, source_attr: str) -> bool:
    text = (expr or "").strip().upper()
    if not text:
        return False
    # Reject known non-source aliases copied from prose transformations.
    if re.search(r"\bTAX_LOT_OPN_MSTR\.", text):
        return False
    # Only allow source alias S and generated lookup aliases LK/LK#.
    for alias in re.findall(r"\b([A-Z_][A-Z0-9_]*)\.", text):
        alias_u = alias.upper()
        # Allow: bare table names, LK-style aliases, and table_name_N aliases
        if alias_u == "S":
            continue
        if re.fullmatch(r"LK\d*", alias_u):
            continue
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*_\d+", alias_u):
            continue  # e.g. CL_VAL_1, ACG_TP_DIM_2
        # Allow uppercase identifiers that look like table names
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", alias_u):
            continue
        return False

    # CASE expressions with normalized aliases are acceptable even when they
    # include string literals that look like identifiers (e.g. 'USD').
    if re.search(r"\bCASE\b", text):
        return True

    bare_tokens = re.findall(r"(?<!\.)\b([A-Z_][A-Z0-9_]*)\b(?!\s*\()(?!\.)", text)
    source_attr_u = (source_attr or "").strip().upper()
    for token in bare_tokens:
        if token in _SQL_IDENTIFIER_ALLOWLIST:
            continue
        if source_attr_u and token == source_attr_u:
            continue
        if re.fullmatch(r"LK\d*", token):
            continue
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*_\d+", token):
            continue  # table_name_N aliases
        return False
    return True


def derive_lookup_from_transformation(
    *,
    row: Dict[str, Any],
    source_attr: str,
    target_col: str,
    source_schema_index: Dict[Tuple[str, str], Dict[str, Any]],
    source_block: str = "",
) -> Tuple[str, str]:
    transformation = (row.get("transformation") or "").strip()
    src_schema = (row.get("source_schema") or "").strip()
    src_table = (row.get("source_table") or "").strip()
    if not transformation:
        return "", ""

    spec = _extract_lookup_spec(
        transformation=transformation,
        src_attr_u=source_attr,
        target_col_u=target_col,
        src_schema=src_schema,
        src_table=src_table,
    )
    if not spec:
        return "", ""

    lookup_table = (spec.get("lookup_table") or "").strip().upper()
    if not lookup_table:
        return "", ""
    # Well-known schema-less lookup table names — do NOT inherit the source schema.
    _KNOWN_SCHEMA_LESS = {"CL_VAL", "SRC_STM_DIM", "ACG_TP_DIM", "CCAL_CIRD_PD_MAP"}
    lookup_bare = lookup_table.split(".")[-1] if "." in lookup_table else lookup_table
    if "." not in lookup_table and src_schema and lookup_bare not in _KNOWN_SCHEMA_LESS:
        lookup_table = f"{src_schema.upper()}.{lookup_table}"

    lookup_schema, lookup_name = split_fq_table(lookup_table)
    lookup_entry = find_table(source_schema_index, lookup_schema, lookup_name)
    if lookup_entry:
        lookup_table = f"{lookup_entry['schema']}.{lookup_entry['table']}"
    elif not lookup_schema:
        # Try well-known schema assignments for unqualified lookup names
        _WELL_KNOWN_SCHEMAS = {
            "CL_VAL": "CCAL_REPL_OWNER",
            "SRC_STM_DIM": "COMMON_OWNER",
            "ACG_TP_DIM": "COMMON_OWNER",
            "CCAL_CIRD_PD_MAP": "CCAL_REPL_OWNER",
        }
        if lookup_name in _WELL_KNOWN_SCHEMAS:
            lookup_table = f"{_WELL_KNOWN_SCHEMAS[lookup_name]}.{lookup_name}"
            lookup_schema = _WELL_KNOWN_SCHEMAS[lookup_name]

    source_block_schema, source_block_table = parse_source_table_from_block(source_block)
    source_entry = find_table(source_schema_index, source_block_schema, source_block_table)
    if not source_entry:
        source_entry = find_table(source_schema_index, src_schema, src_table)

    join_col = (spec.get("lookup_join_col") or source_attr or "").strip().upper().split(".")[-1]
    val_col = (spec.get("lookup_value_col") or target_col).strip().upper().split(".")[-1]
    src_lookup_col = (spec.get("source_lookup_col") or source_attr or "").strip().upper().split(".")[-1]
    src_lookup_literal = (spec.get("source_lookup_literal") or "").strip()
    extra_filter = (spec.get("extra_filter") or "").strip()

    if lookup_name == "CL_VAL" and (src_lookup_col in {"CL_VAL", "CL_VAL_NM", "CL_VAL_CD"} or src_lookup_col == source_attr):
        inferred_src = infer_lookup_source_key_from_text(transformation, source_attr)
        if inferred_src:
            src_lookup_col = inferred_src

    if lookup_entry:
        lookup_cols = set((lookup_entry.get("columns") or {}).keys())
        if join_col and join_col not in lookup_cols:
            if "CL_VAL_ID" in lookup_cols:
                join_col = "CL_VAL_ID"
            elif lookup_cols:
                join_col = sorted(lookup_cols)[0]
        if val_col and val_col not in lookup_cols:
            if "CL_VAL_NM" in lookup_cols:
                val_col = "CL_VAL_NM"
            elif lookup_cols:
                val_col = sorted(lookup_cols)[0]

    if source_entry:
        source_cols = set((source_entry.get("columns") or {}).keys())
        if src_lookup_col and src_lookup_col not in source_cols:
            inferred_src = infer_lookup_source_key_from_text(transformation, source_attr)
            if inferred_src in source_cols:
                src_lookup_col = inferred_src
            else:
                src_lookup_col = source_attr if source_attr in source_cols else ""

    if src_lookup_literal:
        source_expr = src_lookup_literal if re.fullmatch(r"-?\d+(?:\.\d+)?", src_lookup_literal) else f"'{src_lookup_literal}'"
        on_sql = f"LK.{join_col} = {source_expr}"
    elif not src_lookup_col:
        # If we cannot resolve a valid source lookup key, keep the join non-matching but executable.
        on_sql = "1 = 0"
    else:
        _src_ref = f"{src_table}.{src_lookup_col}" if src_table else f"S.{src_lookup_col}"
        on_sql = f"NVL(TO_CHAR(LK.{join_col}), '{NVL_NULL_SENTINEL}') = NVL(TO_CHAR({_src_ref}), '{NVL_NULL_SENTINEL}')"

    if extra_filter:
        # keep as-is, but normalize uppercase alias when present
        extra_filter = re.sub(r"\bLK\.", "LK.", extra_filter, flags=re.IGNORECASE)
        if not extra_filter.upper().startswith("AND"):
            extra_filter = "AND " + extra_filter

    # Use lookup table bare name as alias (will be renumbered by build_control_insert_sql).
    _lk_bare = lookup_table.split(".")[-1].upper() if lookup_table else "LK"
    join_sql = f"LEFT JOIN {lookup_table} {_lk_bare}\nON {on_sql}"
    # Replace LK. references in ON clause with the bare table name alias
    join_sql = re.sub(r"\bLK\.", f"{_lk_bare}.", join_sql, flags=re.IGNORECASE)
    if extra_filter:
        extra_filter = re.sub(r"\bLK\.", f"{_lk_bare}.", extra_filter, flags=re.IGNORECASE)
        join_sql += f"\n{extra_filter}"

    return join_sql, f"{_lk_bare}.{val_col}"


def _build_table_index(datasource_id: int) -> Dict[Tuple[str, str], Dict[str, Any]]:
    payload = load_schema_kb_payload(datasource_id)
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for source in payload.get("sources", []):
        pdm = (source or {}).get("pdm", {})
        for schema_block in pdm.get("schemas", []) or []:
            schema_name = (schema_block.get("schema") or "").strip().upper()
            if not schema_name:
                continue
            for table in schema_block.get("tables", []) or []:
                table_name = (table.get("name") or "").strip().upper()
                if not table_name:
                    continue
                col_map = {
                    (c.get("name") or "").strip().upper(): (c.get("name") or "").strip().upper()
                    for c in table.get("columns", []) or []
                    if (c.get("name") or "").strip()
                }
                index[(schema_name, table_name)] = {
                    "schema": schema_name,
                    "table": table_name,
                    "columns": col_map,
                }
    return index


def split_fq_table(fq: str) -> Tuple[str, str]:
    if "." in fq:
        parts = fq.split(".", 1)
        return parts[0].strip().upper(), parts[1].strip().upper()
    return "", (fq or "").strip().upper()


def find_table(index: Dict[Tuple[str, str], Dict[str, Any]], schema: str, table: str) -> Optional[Dict[str, Any]]:
    schema_u = (schema or "").strip().upper()
    table_u = (table or "").strip().upper()
    if schema_u and (schema_u, table_u) in index:
        return index[(schema_u, table_u)]
    if not schema_u:
        for (s, t), entry in index.items():
            if t == table_u:
                return entry
    return None


def _validate_control_table_requirements(
    *,
    rows: List[Dict[str, Any]],
    target_definition: Dict[str, Any],
    target_schema: str,
    target_table: str,
    source_index: Dict[Tuple[str, str], Dict[str, Any]],
    target_index: Dict[Tuple[str, str], Dict[str, Any]],
) -> Dict[str, Any]:
    target_schema_u = (target_schema or "").strip().upper()
    target_table_u = (target_table or "").strip().upper()
    target_entry = find_table(target_index, target_schema_u, target_table_u)
    # Accept target if it was loaded from the live-DB fallback (has columns) even
    # when the saved PDM/KB index doesn't include its schema yet.
    if not target_entry and not (target_definition or {}).get("columns"):
        raise ValueError(
            f"Target table {target_schema_u}.{target_table_u} was not found in saved PDM/KB. "
            "Generate the required PDM and KB first."
        )

    target_cols = {
        (c.get("name") or "").strip().upper()
        for c in target_definition.get("columns", []) or []
        if (c.get("name") or "").strip()
    }
    drd_target_cols = {
        (row.get("physical_name") or "").strip().upper()
        for row in rows
        if (row.get("physical_name") or "").strip()
    }

    missing_source_tables = []
    missing_source_columns = []
    missing_target_columns = sorted([c for c in drd_target_cols if c and c not in target_cols])

    seen_source_tables = set()
    for row in rows:
        src_schema = (row.get("source_schema") or "").strip().upper()
        src_table = (row.get("source_table") or "").strip().upper()
        src_attr = (row.get("source_attribute") or "").strip().upper()
        if not src_table:
            continue
        # Skip lookup / dimension tables in source column validation —
        # they are referenced for JOIN output only, not as staging input.
        if _is_lookup_table_name(src_table):
            continue
        table_key = (src_schema, src_table)
        if table_key not in seen_source_tables:
            seen_source_tables.add(table_key)
            if not find_table(source_index, src_schema, src_table):
                missing_source_tables.append(f"{src_schema}.{src_table}" if src_schema else src_table)
                continue

        src_entry = find_table(source_index, src_schema, src_table)
        if src_entry and src_attr and src_attr not in {"NULL", "NONE", "N/A"} and src_attr not in src_entry.get("columns", {}):
            missing_source_columns.append(f"{src_schema}.{src_table}.{src_attr}" if src_schema else f"{src_table}.{src_attr}")

    if missing_source_tables:
        # Don't hard-block: source tables may live in schemas not yet in the PDM.
        # Surface them as warnings so the caller can decide.
        pass

    return {
        "missing_source_tables": sorted(set(missing_source_tables)),
        "missing_source_columns": sorted(set(missing_source_columns))[:200],
        "missing_target_columns": missing_target_columns[:200],
    }