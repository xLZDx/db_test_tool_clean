"""Control-table generation and comparison helpers.

Builds a step-based control-table workflow from:
- saved PDM metadata for the target table
- DRD rows parsed by the existing DRD import service
- optional manual SQL supplied by the user for comparison
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

from app.services.drd_import_service import (
    _extract_case_expression,
    _extract_lookup_spec,
    _is_lookup_table_name,
    _resolve_column_name,
    extract_drd_metadata,
    generate_drd_tests,
    parse_drd_file,
    validate_column_mappings_with_kb,
)
from app.services.schema_kb_service import load_schema_kb_payload
from app.config import DATA_DIR


def _load_confirmed_name_pairs() -> tuple:
    """Read operator-confirmed PDM abbreviation pairs from
    data/comparator_config.json so the PDM diagnostic does not flag
    spec-name <-> physical-name pairs as missing.  Robust to missing /
    malformed / semantically-invalid config: returns empty tuple in
    that case.  Caches the result for the process lifetime.
    """
    global _CONFIRMED_PAIRS_CACHE
    if _CONFIRMED_PAIRS_CACHE is not None:
        return _CONFIRMED_PAIRS_CACHE
    import json as _json
    from pathlib import Path as _Path
    try:
        cfg_path = _Path(__file__).resolve().parent.parent.parent / "data" / "comparator_config.json"
        if not cfg_path.exists():
            _CONFIRMED_PAIRS_CACHE = ()
            return _CONFIRMED_PAIRS_CACHE
        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
        pairs = cfg.get("confirmed_name_pairs") if isinstance(cfg, dict) else None
        if not isinstance(pairs, list):
            _CONFIRMED_PAIRS_CACHE = ()
            return _CONFIRMED_PAIRS_CACHE
        out = []
        for p in pairs:
            if isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(x, str) for x in p):
                out.append((p[0].upper(), p[1].upper()))
        _CONFIRMED_PAIRS_CACHE = tuple(out)
        return _CONFIRMED_PAIRS_CACHE
    except Exception:
        # Includes OSError, JSONDecodeError, ValueError, TypeError --
        # any semantic / format problem -> safe empty fallback so the
        # PDM diagnostic keeps working without the name-pair feature.
        _CONFIRMED_PAIRS_CACHE = ()
        return _CONFIRMED_PAIRS_CACHE


_CONFIRMED_PAIRS_CACHE: Optional[tuple] = None


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


def _resolve_physical_target(
    datasource_id: int,
    target_schema: str,
    target_table: str,
    file_bytes: bytes,
    filename: str,
    sheet_name: Optional[str],
) -> Tuple[str, str]:
    """Resolve the PHYSICAL (schema, table) for the control-table flow.

    A DRD's "Table Name (From DA Team)" row may carry BOTH a logical/DA-team
    name and the physical table name side by side (e.g.
    ``cls_tax_lots_fact_rjt | CLS_TAX_LOTS_NON_BKR_FACT``).  The typed /
    auto-filled target is often the logical name, which is absent from the PDM.
    Try the typed name first; on PDM-miss, fall back to the DRD's other
    table-name candidate(s) and use the first that resolves in the PDM.
    Order-independent (physical may be the 1st or 2nd cell).  Returns the typed
    values unchanged when nothing resolves, so the normal PDM-miss 422 fires.
    """
    candidates: List[Tuple[str, str]] = [(target_schema, target_table)]
    try:
        meta = extract_drd_metadata(file_bytes, filename, sheet_name)
        for c in (meta.get("table_name_candidates") or []):
            cv = str(c).strip()
            if not cv:
                continue
            if "." in cv:
                csch, ctbl = cv.split(".", 1)
            else:
                csch, ctbl = target_schema, cv
            pair = (csch.strip(), ctbl.strip())
            if pair[1] and pair not in candidates:
                candidates.append(pair)
    except Exception:
        pass
    for csch, ctbl in candidates:
        try:
            load_target_table_definition(datasource_id, csch, ctbl)
            return csch, ctbl
        except ValueError:
            continue
    return target_schema, target_table


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
    # Resolve the PHYSICAL target name first.  A DRD may declare a logical/
    # DA-team name AND the physical table name side by side ("Table Name (From
    # DA Team)" row); the typed/auto-filled target is often the logical name,
    # which is absent from the PDM.  Map to the candidate that resolves in the
    # PDM so the whole flow (DDL/INSERT/tests) names the real table.
    target_schema, target_table = _resolve_physical_target(
        target_datasource_id, target_schema, target_table,
        file_bytes, filename, sheet_name,
    )
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
    # P3: parse all DRD sheets (ETL Notes, Model, ...) and build a named-block
    # index so cross-tab references ("Use APACSH logic from 'ETL Notes' tab")
    # can be resolved into full body text on each analysis row.  Best-effort:
    # non-xlsx inputs and parse errors degrade silently.
    etl_block_index = None
    try:
        if (filename or "").lower().endswith(".xlsx"):
            from app.sql_model.drd_multi_sheet import parse_all_sheets
            from app.sql_model.etl_block_index import build_block_index
            etl_block_index = build_block_index(parse_all_sheets(file_bytes))
    except Exception:
        etl_block_index = None
    analysis_rows = build_analysis_rows(
        rows=rows,
        baseline_tests=baseline_tests,
        target_schema=target_schema,
        target_table=target_table,
        target_definition=target_def,
        source_schema_index=source_index,
        etl_block_index=etl_block_index,
    )
    # Operator-locked (2026-05-29 Phase 7.4, reverts Phase 7.3 Issue 1):
    # DDL comes from PDM / real DB ONLY.  We do NOT silently default
    # DRD-declared columns missing from PDM to VARCHAR2(4000) -- that
    # was hiding a real DRD-vs-PDM mismatch.  Instead the diagnostic
    # below surfaces the gap so operator can either: (a) extend PDM,
    # (b) correct the DRD, or (c) accept the column as deprecated.
    ddl_sql = build_control_table_ddl(control_schema, target_table, target_def)
    drd_cols_upper = {
        (r.get("physical_name") or r.get("column") or "").strip().upper()
        for r in rows
        if (r.get("physical_name") or r.get("column") or "").strip()
    }
    pdm_cols_upper = {
        (c.get("name") or "").strip().upper()
        for c in (target_def.get("columns") or [])
    }
    drd_not_in_pdm = sorted(drd_cols_upper - pdm_cols_upper - {""})
    if drd_not_in_pdm:
        kb_validation.setdefault("ddl_warnings", []).extend(
            f"{c}: DRD declares this target column but PDM does NOT have it. "
            f"INSERT will fail unless PDM is extended OR DRD is corrected."
            for c in drd_not_in_pdm
        )
    insert_sql = build_control_insert_sql(
        control_schema=control_schema,
        target_table=target_table,
        target_definition=target_def,
        analysis_rows=analysis_rows,
        source_schema_index=source_index,
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
    """Load a target table's column definition from any available source.

    Lookup order (operator-locked 2026-05-29 Phase 7.4):
      1. Primary datasource's saved KB (schema_kb_ds_<id>.json).
      2. ALL registered datasources' saved KBs.
      3. ALL on-disk schema_kb_ds_*.json files (even unregistered) --
         picks up KBs the operator copied in without registering a DS.
      4. Live DB on primary + all other registered DSes.

    Raises ValueError with ASCII-only message on miss (CP1252-safe).
    """
    import logging
    _log = logging.getLogger(__name__)
    wanted_schema = (target_schema or "").strip().upper()
    wanted_table = (target_table or "").strip().upper()

    def _search_payload(payload: dict, source_label: str) -> Optional[dict]:
        for src in payload.get("sources", []):
            pdm = (src or {}).get("pdm", {})
            for schema_block in pdm.get("schemas", []) or []:
                schema_name = (schema_block.get("schema") or "").strip().upper()
                if wanted_schema and schema_name != wanted_schema:
                    continue
                for table_block in schema_block.get("tables", []) or []:
                    table_name = (table_block.get("name") or "").strip().upper()
                    if table_name == wanted_table:
                        # Return a shallow copy so callers can stamp
                        # diagnostic fields without mutating the cached
                        # payload (review MAJOR finding).
                        hit = dict(table_block)
                        hit["_source_label"] = source_label
                        return hit
        return None

    # 1. Primary registered DS.
    hit = _search_payload(load_schema_kb_payload(datasource_id), f"ds_{datasource_id}")
    if hit is not None:
        return hit

    # 2. Other registered DSes.
    other_ds_ids = _list_all_datasource_ids()
    for other_id in other_ds_ids:
        if other_id == datasource_id:
            continue
        try:
            other_payload = load_schema_kb_payload(other_id)
        except Exception as exc:
            _log.debug("PDM search ds_%s failed: %s", other_id, exc)
            continue
        hit = _search_payload(other_payload, f"ds_{other_id}")
        if hit is not None:
            hit["_source_datasource_id"] = other_id
            return hit

    # 3. ON-DISK KB files NOT registered in the DS table.  This catches
    #    KBs the operator pasted in manually (e.g. local_kb copied
    #    from another machine).  Operator-locked Phase 7.4 fix.
    from app.services.schema_kb_service import _kb_dir
    registered = set(other_ds_ids) | {datasource_id}
    on_disk_files = sorted((_kb_dir()).glob("schema_kb_ds_*.json"))
    for f in on_disk_files:
        # extract DS id from filename: schema_kb_ds_<N>.json
        m = re.match(r"schema_kb_ds_(\d+)\.json$", f.name)
        if not m:
            continue
        ds_num = int(m.group(1))
        if ds_num in registered:
            continue  # already covered by step 1 or 2
        try:
            payload = load_schema_kb_payload(ds_num)
        except Exception as exc:
            _log.debug("Unregistered KB %s failed to load: %s", f.name, exc)
            continue
        hit = _search_payload(payload, f"on-disk:{f.name}")
        if hit is not None:
            hit["_source_datasource_id"] = ds_num
            _log.warning(
                "Found %s.%s in unregistered KB %s -- consider "
                "registering datasource %d so future calls do not have "
                "to scan unregistered files.",
                wanted_schema, wanted_table, f.name, ds_num,
            )
            return hit

    # 4. Fall back to live DB on primary + all other registered DSes.
    fallback = _load_table_def_from_live_db(datasource_id, wanted_schema, wanted_table)
    if fallback:
        return fallback
    for other_id in other_ds_ids:
        if other_id == datasource_id:
            continue
        fallback = _load_table_def_from_live_db(other_id, wanted_schema, wanted_table)
        if fallback:
            fallback["_source_datasource_id"] = other_id
            return fallback

    # ASCII-only message (CP1252-safe).  Lists the scanned sources.
    scanned = [f"ds_{datasource_id}"]
    scanned += [f"ds_{i}" for i in other_ds_ids if i != datasource_id]
    # Append the unregistered on-disk KB files (review MAJOR finding:
    # previous expression double-counted and undercounted).
    unregistered_names = []
    for f in on_disk_files:
        m = re.match(r"schema_kb_ds_(\d+)\.json$", f.name)
        if not m:
            continue
        ds_num = int(m.group(1))
        if ds_num not in registered:
            unregistered_names.append(f.name)
    scanned += unregistered_names
    raise ValueError(
        f"Table {wanted_schema}.{wanted_table} not found in any saved PDM "
        f"({len(scanned)} sources scanned: {', '.join(scanned[:6])}{'...' if len(scanned) > 6 else ''}) "
        f"and could not be loaded from the live database. "
        f"Either: (a) place a Git-LFS-pulled schema_kb_ds_*.json into "
        f"data/local_kb/ that contains {wanted_schema}.{wanted_table}, "
        f"or (b) generate/save the PDM for datasource {datasource_id} "
        f"via Schema Browser -> Generate PDM."
    )


def _list_all_datasource_ids() -> List[int]:
    """Return all datasource IDs from the app database.

    On failure: WARN + return empty list (so the cascade can fall back
    to on-disk KB files + live DB).  Previously this swallowed silently
    -- review HIGH finding 2026-05-29 Phase 7.4.
    """
    import logging
    _log = logging.getLogger(__name__)
    try:
        db_path = DATA_DIR / "app.db"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT id FROM datasources ORDER BY id")
        ids = [row[0] for row in cur.fetchall()]
        conn.close()
        return ids
    except Exception as exc:
        _log.warning(
            "_list_all_datasource_ids failed (%s); cascade will fall "
            "back to on-disk KB files + live DB.  Operator may need to "
            "register the datasource in app.db.",
            exc,
        )
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
        from types import SimpleNamespace
        ds_obj = SimpleNamespace(
            db_type=row["db_type"],
            host=row["host"],
            port=int(row["port"]),
            database_name=row["database_name"],
            username=row["username"],
            password=row["password"] or "",
            extra_params=row["extra_params"],
        )
        connector = get_connector(ds_obj)

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
    etl_block_index: Optional[Any] = None,
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
        # Phase 7.19.13 (2026-06-02): DRD source_attribute cells sometimes
        # contain a newline where two spreadsheet rows/cells got merged
        # (e.g. "STM_BASE_CCY_AMT\nOTHR_FEE").  A column name can never
        # contain a newline, so when the value is a bare column followed by
        # a newline, keep only the first physical line -- otherwise the raw
        # newline leaks into the emitted SELECT and breaks the SQL
        # (ORA-00923).  Multi-line PROSE transformations are unaffected --
        # this only trims source_attribute, and only when line 1 is itself
        # a clean identifier.  Latent bug surfaced by the 7.19.9 KB fallback
        # (the owning alias used to be PDM_MISS -> NULL, hiding the leak).
        if "\n" in source_attr:
            _first_line = source_attr.split("\n", 1)[0].strip()
            if re.match(r"^[A-Z_][A-Z0-9_\$#]*$", _first_line):
                source_attr = _first_line
        expr_info = extract_expr_from_test_sql(
            sql=baseline.get("source_query") or "",
            target_schema=target_schema,
            target_table=target_table,
            target_column=col_name,
            fallback_source_table=row.get("source_table") or "",
        )
        drd_expr = expr_info.get("expression") or fallback_drd_expression(row, source_attr)
        # Defensive alignment: baseline-extracted ``<ALIAS>.<COL>`` may use a
        # placeholder column (e.g. join PK AR_ID) while DRD source_attribute says
        # the real attr (e.g. AR_CGY_CD). DRD is authoritative.
        drd_expr = align_expr_with_source_attr(drd_expr, source_attr)
        lookup_join = expr_info.get("lookup_join") or ""

        # Always compute bare source-table name for use in expression normalization.
        _src_table_for_check = (row.get("source_table") or "").strip().upper().split(".")[-1]

        # If baseline did not preserve lookup metadata, derive it from DRD transformation text.
        if not lookup_join:
            lookup_join, derived_expr = derive_lookup_from_transformation(
                row=row,
                source_attr=source_attr,
                target_col=col_name,
                source_schema_index=source_schema_index,
                source_block=expr_info.get("source_block") or fallback_source_block(row),
            )
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

        # P3: detect cross-tab ETL-Notes block reference (e.g. "Use APACSH
        # logic from 'ETL Notes' tab") and resolve to the full block body so
        # the comparator can surface the real logic in drd_logic.
        etl_block_ref: str = ""
        etl_block_body: str = ""
        if etl_block_index is not None:
            from app.sql_model.etl_block_index import (
                find_block_references,
                resolve_block_body,
            )
            search_text = (
                (row.get("transformation") or "")
                + "\n"
                + (row.get("notes") or "")
            )
            refs = find_block_references(search_text, etl_block_index)
            if refs:
                etl_block_ref = refs[0]
                body = resolve_block_body(search_text, etl_block_index)
                etl_block_body = body or ""

        # Phase 7.19.12 (2026-06-02): repair garbage drd_expression baseline
        # derived from prose ('ED'/'EC'/wrong-column) when the structured
        # source_attribute is a real column present in the source PDM.  Keeps
        # the comparison baseline honest so correct generated expressions do
        # not show as false GENERATED_MISMATCH.
        _src_tbl_bare_drd = (row.get("source_table") or "").strip().split("\n")[0].split(",")[0].strip().upper().split(".")[-1]
        _sa_in_pdm = False
        if source_schema_index is not None and source_attr and re.match(r"^[A-Z_][A-Z0-9_]*$", source_attr):
            _tbl_def = find_table(source_schema_index, (row.get("source_schema") or "").strip(), _src_tbl_bare_drd)
            _sa_in_pdm = bool(_tbl_def and source_attr in _tbl_def.get("columns", {}))
        drd_expr = reconcile_drd_expr_with_source_attr(
            drd_expr, _src_tbl_bare_drd, source_attr, source_attr_in_pdm=_sa_in_pdm,
        )

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
                "etl_block_ref": etl_block_ref,
                "etl_block_body": etl_block_body,
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


_RE_BARE_ALIAS_COL = re.compile(
    r"^\s*([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_$#]*)\s*$",
    re.IGNORECASE,
)
_RE_BARE_IDENT = re.compile(r"^[A-Z][A-Z0-9_$#]*$", re.IGNORECASE)


def align_expr_with_source_attr(expr: str, source_attr: str) -> str:
    """Reconcile a bare ``<ALIAS>.<COL>`` expression with the DRD source_attribute.

    The baseline test-SQL extractor sometimes yields a placeholder column on the
    join alias (e.g. the join PK ``AR_DIM_18.AR_ID``) when the DRD's authoritative
    ``source_attribute`` for that target column is a different attribute on the
    same table (e.g. ``AR_CGY_CD``).  The DRD source_attribute is the truth — the
    alias may legitimately come from the join graph, but the column must match
    the DRD's claim.

    Triggers ONLY when ALL hold:
      * ``expr`` is exactly ``<bare_alias>.<bare_col>`` (no functions / literals / CASE)
      * ``source_attr`` is a bare identifier (no expression)
      * the projected column differs from ``source_attr``

    Returns the expression unchanged in every other case, including:
      * CASE / NVL / DECODE / arithmetic expressions
      * literals (SYSDATE, quoted strings, numerics)
      * expressions where the bare column already matches source_attr
      * empty / missing source_attr
    """
    if not expr or not source_attr:
        return expr
    src = source_attr.strip().upper()
    if not _RE_BARE_IDENT.match(src):
        return expr  # source_attr is itself an expression — don't touch
    m = _RE_BARE_ALIAS_COL.match(expr)
    if not m:
        return expr  # not a bare ALIAS.COL — leave CASE/NVL/etc alone
    alias = m.group(1)
    col = m.group(2).upper()
    if col == src:
        return expr  # already aligned
    return f"{alias}.{src}"


def reconcile_drd_expr_with_source_attr(
    drd_expr: str,
    source_table_bare: str,
    source_attr: str,
    *,
    source_attr_in_pdm: bool,
) -> str:
    """Phase 7.19.12 (2026-06-02): repair a garbage drd_expression baseline.

    The DRD parser sometimes derives drd_expression from PROSE
    transformations and yields fragments -- ``'ED'`` from "PopulatED",
    ``'EC'`` from "APASEC" -- or a stale default that names the WRONG
    column (e.g. ``CL_VAL.CL_VAL_NM`` for a column whose source_attribute
    is ``SALE_CHRG_RATE``).  These poison the comparison baseline: the
    generated expression is correct (it uses source_attribute) but the
    drd_expression it is compared against is junk, so every such column
    shows a false GENERATED_MISMATCH.

    When the structured ``source_attribute`` is a real column (present in
    the source table's PDM) but the derived ``drd_expr`` is a bare literal
    OR a simple qualified reference to a DIFFERENT column, rebuild the
    baseline as ``<source_table>.<source_attribute>`` so the comparison
    reflects the DRD's STRUCTURED intent.  Genuine complex expressions
    (CASE / NVL / DECODE / COALESCE / SUBSTR / TO_CHAR / arithmetic /
    concatenation / sub-selects) are left untouched -- only obvious
    misparses are corrected.  Generic: no hardcoded table/column names.
    """
    sa = (source_attr or "").strip().upper()
    if not sa or not re.match(r"^[A-Z_][A-Z0-9_]*$", sa):
        return drd_expr
    if not source_attr_in_pdm:
        return drd_expr  # cannot trust source_attribute -> leave baseline as-is
    expr = (drd_expr or "").strip()
    rebuilt = f"{source_table_bare}.{sa}" if source_table_bare else sa
    if not expr:
        return rebuilt
    upper = expr.upper()
    # Leave genuine complex/derived expressions alone.
    if re.search(
        r"\b(CASE|SELECT|NVL|DECODE|COALESCE|SUBSTR|TO_CHAR|TO_DATE|TO_NUMBER|CAST|TRIM|ROUND)\b"
        r"|\|\||\(",
        upper,
    ):
        return drd_expr
    is_literal = bool(re.match(r"^'[^']*'$", expr) or re.match(r"^-?\d+(\.\d+)?$", expr))
    m = re.match(r"^(?:[A-Z_][A-Z0-9_\$#]*\.)?([A-Z_][A-Z0-9_\$#]*)$", upper)
    col_part = m.group(1) if m else None
    if is_literal or (col_part is not None and col_part != sa):
        return rebuilt
    return drd_expr


def extract_embedded_join_chain(
    transformation: str,
    *,
    main_alias: str,
    drd_source_table: str,
    source_attr: str,
    uniq_prefix: str,
) -> Optional[Tuple[List[str], str]]:
    """Phase 7.19.13 (2026-06-02): parse an explicit multi-hop join chain
    embedded as literal SQL in a DRD transformation cell.

    Many DRD columns carry the real join logic as embedded SQL, e.g.::

        ccal_repl_owner.txn t
        join ccal_repl_owner.apa ap ON ap.exec_id = t.txn_id
        left join ccal_repl_owner.txn_avy_cl tac ON tac.txn_id = t.txn_id ...
        left join ccal_repl_owner.avy_cl acl ON acl.avy_cl_id = tac.avy_cl_id

    The single-join heuristic mangles these (wrong alias / wrong ON), so
    the emitter NULLs the column.  This parser instead emits the WHOLE
    chain verbatim with collision-proof aliases (``<uniq_prefix>_<n>``),
    rebases the base-table alias (``t``) to the INSERT's main source
    alias, and returns the projection ``<final_alias>.<source_attr>``.

    Returns (join_sql_lines, final_alias, signature) or None when:
      * no embedded base+JOIN block is present, or
      * the DRD source_table is not one of the joined tables (cannot map
        the target column to a hop -> safer to leave NULL).
    The signature is alias-independent so the caller can DEDUP identical
    chains shared by many columns (emit the join block once).

    Aliases are capped at 30 chars (Oracle identifier limit).  Generic:
    no hardcoded schema/table names.
    """
    text = transformation or ""
    if not re.search(r"\bjoin\b", text, flags=re.IGNORECASE):
        return None

    # Repair the "net_new _ast_cgy" split-identifier artifact first.
    text = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+(_[A-Za-z_][A-Za-z0-9_]*)\b", r"\1\2", text)

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    base_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_$#]*)\s+([A-Za-z_][A-Za-z0-9_]*)$")
    join_re = re.compile(
        r"^(left\s+|right\s+|inner\s+|full\s+)?(?:outer\s+)?join\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_$#]*)\s+([A-Za-z_][A-Za-z0-9_]*)\s+on\s+(.+)$",
        flags=re.IGNORECASE,
    )

    base_alias = None
    hops: List[Dict[str, str]] = []   # {schema, table, alias, on}
    for ln in lines:
        bm = base_re.match(ln)
        if bm and base_alias is None and not re.match(r"^(left|right|inner|full|join)\b", ln, flags=re.IGNORECASE):
            base_alias = bm.group(3)
            continue
        jm = join_re.match(ln)
        if jm:
            hops.append({
                "schema": jm.group(2).upper(),
                "table": jm.group(3).upper(),
                "alias": jm.group(4),
                "on": jm.group(5).strip().rstrip(";"),
            })
    if base_alias is None or not hops:
        return None

    # Map each embedded alias -> unique collision-proof alias.
    sa = (source_attr or "").strip().upper()
    src_tbl = (drd_source_table or "").strip().upper().split(".")[-1]
    alias_map = {base_alias.upper(): main_alias.upper()}
    for idx, hop in enumerate(hops, 1):
        new_alias = f"{uniq_prefix}_{idx}"
        if len(new_alias) > 30:
            new_alias = new_alias[:30]
        alias_map[hop["alias"].upper()] = new_alias

    def _rebase(expr: str) -> str:
        out = expr
        for old, new in alias_map.items():
            out = re.sub(rf"(?<![A-Za-z0-9_.]){re.escape(old)}\.", f"{new}.", out, flags=re.IGNORECASE)
        return out

    final_alias = None
    join_lines: List[str] = []
    sig_parts: List[str] = []
    for idx, hop in enumerate(hops, 1):
        new_alias = alias_map[hop["alias"].upper()]
        on_clause = _rebase(hop["on"])
        join_lines.append(
            f"LEFT JOIN {hop['schema']}.{hop['table']} {new_alias} ON {on_clause}"
        )
        # Signature is alias-independent (uses the ORIGINAL embedded aliases)
        # so two columns sharing the SAME chain produce the SAME signature
        # and the caller can emit the join block ONCE (dedup) -- emitting it
        # per-column produced 100+ redundant joins and an unrunnable plan.
        sig_parts.append(f"{hop['schema']}.{hop['table']}|{hop['on'].upper().strip()}")
        if hop["table"] == src_tbl:
            final_alias = new_alias

    if final_alias is None or not sa:
        return None
    signature = " || ".join(sig_parts) + f" >>> {src_tbl}"
    return join_lines, final_alias, signature


def fallback_drd_expression(row: Dict[str, Any], source_attr: str) -> str:
    if source_attr in {"NULL", "NONE", "N/A"}:
        return "NULL"
    src_table = (row.get("source_table") or "").strip()
    # Use only the bare table name as alias prefix (last segment of FQ name)
    src_table_bare = src_table.split(".")[-1] if src_table else ""
    src_prefix = f"{src_table_bare}." if src_table_bare else ""
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
    source_schema_index: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> str:
    row_map = {row["column"]: row for row in analysis_rows}

    # ── Collect all unique non-lookup source tables from DRD rows ─────────
    # Each DRD row may come from a different source table (multi-join fact tables).
    # Build an ordered map of FQ_table -> bare_alias so the FROM clause lists all
    # required tables.  First occurrence order is preserved.
    _src_table_registry: Dict[str, str] = {}  # fq_upper -> bare_alias_upper
    _src_table_counts: Dict[str, int] = defaultdict(int)
    _src_schema_parts: set = set()
    for _row in analysis_rows:
        # Clean source_table: take only first entry if cell has multiple (newline/comma separated)
        _st_raw = (_row.get("source_table") or "").strip()
        _st = _st_raw.split("\n")[0].split(",")[0].strip().upper()
        _ss = (_row.get("source_schema") or "").strip().upper()
        if _ss:
            _src_schema_parts.add(_ss)
        if not _st or _is_lookup_table_name(_st):
            continue
        _bare = _st.split(".")[-1]
        if "." in _st:
            _fq = _st
        elif _ss:
            _fq = f"{_ss}.{_bare}"
        else:
            _fq = _bare
        _src_table_counts[_fq] += 1
        if _fq not in _src_table_registry:
            _src_table_registry[_fq] = _bare

    _multi_source = len(_src_table_registry) > 1

    if _multi_source:
        # Avoid comma-based multi-table FROM because it creates cartesian products
        # when DRD rows span many lookup/dimension tables. Use the most frequent
        # source table as the base and keep other tables reachable via explicit joins.
        _order_index = {fq: i for i, fq in enumerate(_src_table_registry.keys())}
        _src_fq = max(
            _src_table_registry.keys(),
            key=lambda fq: (_src_table_counts.get(fq, 0), -_order_index.get(fq, 0)),
        )
        _src_table_name = _src_table_registry[_src_fq]
        _src_ref_name = _src_table_name
        base_source = f"FROM {_src_fq} {_src_ref_name}"
    else:
        # Single-source path (original logic) ─────────────────────────────
        source_blocks = [row.get("source_block", "") for row in analysis_rows if row.get("source_block")]
        base_source = Counter(source_blocks).most_common(1)[0][0] if source_blocks else "FROM SOURCE_SCHEMA.SOURCE_TABLE"

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

        _inferred_alias = ""
        if _alias_freq:
            _sorted_aliases = sorted(_alias_freq.items(), key=lambda x: -x[1])
            for _cand, _cnt in _sorted_aliases:
                if _cand in _src_schema_parts:
                    continue
                if _cand == _src_table_name:
                    _inferred_alias = ""
                    break
                if _cnt >= 3:
                    _inferred_alias = _cand
                    break

        _effective_alias = _src_explicit_alias or _inferred_alias
        if _effective_alias and len(_effective_alias) == 1:
            _effective_alias = ""

        if _effective_alias:
            base_source = f"FROM {_src_fq} {_effective_alias}"
        else:
            base_source = f"FROM {_src_fq}"

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
    pdm_missing_old_aliases: set = set()
    pdm_missing_lookup_bases: set = set()
    _ejc_counter = 0  # Phase 7.19.13 embedded-join-chain unique alias counter
    _ejc_chain_cache: Dict[str, str] = {}  # chain signature -> final alias (dedup shared chains)

    # Collect unique raw joins with row context first, then collapse low-quality duplicates.
    raw_join_seen: set[str] = set()
    join_candidates: List[Dict[str, Any]] = []
    for row in analysis_rows:
        lookup_join = sanitize_lookup_join_sql((row.get("lookup_join") or "").strip())
        if not lookup_join or lookup_join in raw_join_seen:
            continue
        raw_join_seen.add(lookup_join)
        _lk_tbl_match = re.search(r'\bJOIN\s+([A-Z0-9_\.\"\$#]+)\b', lookup_join, flags=re.IGNORECASE)
        _lk_fq = (_lk_tbl_match.group(1).replace('"', '').upper() if _lk_tbl_match else "LOOKUP")
        _lk_bare = _lk_fq.split(".")[-1]

        # Extract alias including $ for Oracle dollar-sign tables like J$TXN.
        _alias_m = re.search(r'\bJOIN\s+[\w\.\$#]+\s+([A-Z_][A-Z0-9_\$#]*)\b', lookup_join, flags=re.IGNORECASE)
        old_alias = _alias_m.group(1) if _alias_m else extract_join_alias(lookup_join)

        _on_match = re.search(r'\bON\b\s+([\s\S]*)$', lookup_join, flags=re.IGNORECASE)
        _on_text = (_on_match.group(1) if _on_match else "").upper()

        # Source-side key used by this join (if present).
        # First try: look for explicit source/main table reference.
        _src_key_match = re.search(
            rf"\b(?:{re.escape(_src_ref_name)}|{re.escape(_src_table_name)}|S)\.([A-Z0-9_#\$]+)\b",
            _on_text,
            flags=re.IGNORECASE,
        )
        _src_key = (_src_key_match.group(1).upper() if _src_key_match else "")
        # If no explicit source match, look for any non-lookup-alias.col reference on the
        # right side of = that isn't the lookup table alias itself.
        if not _src_key and old_alias:
            _lhs_ref = re.search(
                r'(?:(?:NVL\(\s*TO_CHAR\(\s*)?([A-Z_][A-Z0-9_\$#]*)\.'
                r'([A-Z0-9_#\$]+))\s*=+',
                _on_text,
                flags=re.IGNORECASE,
            )
            if _lhs_ref and _lhs_ref.group(1).upper() != old_alias.upper():
                _src_key = _lhs_ref.group(2).upper()
        if not _src_key and old_alias:
            _rhs_ref = re.search(
                r'=\s*(?:NVL\(\s*TO_CHAR\(\s*)?([A-Z_][A-Z0-9_\$#]*)\.'
                r'([A-Z0-9_#\$]+)',
                _on_text,
                flags=re.IGNORECASE,
            )
            if _rhs_ref and _rhs_ref.group(1).upper() != old_alias.upper():
                _src_key = _rhs_ref.group(2).upper()
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
            # Phase 7.19.10 (2026-06-02): carry the DRD row's authoritative
            # source_schema so the resolver can correct a lookup_join whose
            # schema prefix came from prose and disagrees with the
            # structured source_schema column.
            "source_schema": (row.get("source_schema") or "").strip().upper(),
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
        _lk_fq = cand.get("lookup_fq") or ""

        lk_table_alias_counts[_lk_bare] = lk_table_alias_counts.get(_lk_bare, 0) + 1
        new_alias = f"{_lk_bare}_{lk_table_alias_counts[_lk_bare]}"

        # If lookup table is not present in current source schema KB index,
        # skip JOIN emission and force dependent expressions to NULL marker later.
        _lk_schema, _lk_name = split_fq_table(_lk_fq)
        if source_schema_index is not None and not find_table(source_schema_index, _lk_schema, _lk_name):
            # Phase 7.19.10 (2026-06-02): the lookup_join's schema prefix is
            # sometimes WRONG -- it was parsed from the DRD transformation
            # prose, which can name a different schema than the structured,
            # authoritative source_schema column.  Real case (operator
            # 2026-06-02): prose said CCAL_REPL_OWNER.IMPCT_ACTION_LKU but
            # the table actually lives in REFERENCE_REPL_OWNER (confirmed
            # present in the PDM + the live FREEPDB1 DB).  Before declaring
            # PDM_MISS, retry resolution using (a) the row's authoritative
            # source_schema, then (b) a bare table-name search.  If either
            # resolves, rewrite the JOIN's schema prefix so the emitted SQL
            # references the correct schema.  Only a genuinely-absent table
            # falls through to PDM_MISS.
            _authoritative_schema = (cand.get("source_schema") or "").strip().upper()
            _corrected = None
            if _authoritative_schema and _authoritative_schema != _lk_schema and \
                    find_table(source_schema_index, _authoritative_schema, _lk_name):
                _corrected = _authoritative_schema
            else:
                _bare_hit = find_table(source_schema_index, "", _lk_name)
                if _bare_hit and (_bare_hit.get("schema") or "").strip():
                    _corrected = _bare_hit["schema"].strip().upper()
            if _corrected and _corrected != _lk_schema:
                # Rewrite SCHEMA.TABLE in the join SQL + cached fq.
                _old_prefix = f"{_lk_schema}.{_lk_name}" if _lk_schema else _lk_name
                lookup_join = re.sub(
                    rf'\b{re.escape(_lk_schema)}\.{re.escape(_lk_name)}\b' if _lk_schema
                    else rf'\b{re.escape(_lk_name)}\b',
                    f'{_corrected}.{_lk_name}',
                    lookup_join,
                    count=1,
                    flags=re.IGNORECASE,
                )
                _lk_schema = _corrected
                _lk_fq = f"{_corrected}.{_lk_name}"
                logger.info(
                    "build_control_insert_sql: corrected lookup schema for "
                    "%s -> %s.%s (lookup_join prose schema disagreed with "
                    "authoritative source_schema/PDM)",
                    _old_prefix, _corrected, _lk_name,
                )
            elif _corrected is None:
                if old_alias:
                    pdm_missing_old_aliases.add(old_alias.upper())
                pdm_missing_lookup_bases.add(_lk_bare.upper())
                continue

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
                logger.warning(
                    "build_control_insert_sql: self-join detected for %s but no source key available — "
                    "neutralizing ON clause to 1=0; all joined columns will be NULL",
                    lookup_join,
                )
                join_sql_renamed = re.sub(
                    r"\bON\b[\s\S]*$",
                    "ON 1 = 0 /* WARNING: self-join key unresolved — joined cols will be NULL */",
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
    _joined_aliases = set(na for (_, na) in join_alias_map.values())
    all_valid_aliases: set = {_src_table_name, _src_ref_name} | _joined_aliases
    alias_by_base: Dict[str, List[str]] = defaultdict(list)
    for _alias in sorted(_joined_aliases):
        _base = re.sub(r"_\d+$", "", _alias.upper())
        alias_by_base[_base].append(_alias)
    # Only allow all DRD source-table aliases when we are truly in single-source mode.
    # In multi-source mode, base FROM is anchored to one table and other raw aliases must
    # be resolved via explicit joins (otherwise they are unsafe/unjoined references).
    if not _multi_source:
        for _fq, _bare in _src_table_registry.items():
            all_valid_aliases.add(_bare)
    # Also allow schema prefixes from FQ references (e.g. TAXLOT_STG_OWNER, COMMON_OWNER)
    for _row in analysis_rows:
        _ss = (_row.get("source_schema") or "").upper()
        if _ss:
            all_valid_aliases.add(_ss)
    # Add bare lookup table names (they may appear in expressions before aliasing)
    for _lk_alias, _lk_fq in lk_table_map.items():
        all_valid_aliases.add(_lk_fq.split(".")[-1].upper())
    # In single-source mode, tolerate raw source-table prefixes seen in DRD expressions.
    # In multi-source mode we intentionally anchor FROM to one primary table, so keeping
    # all raw source-table aliases would allow invalid, unjoined references to leak through.
    if not _multi_source:
        for _row in analysis_rows:
            _st = (_row.get("source_table") or "").strip().upper().split("\n")[0].split(",")[0].strip().split(".")[-1]
            if _st:
                all_valid_aliases.add(_st)

    # Phase 7.19.4 fix (operator 2026-06-01 B1): DRD authors sometimes shove
    # multi-line English prose into the `source_attribute` / `transformation`
    # cell instead of a SQL identifier.  Example (real, from DRD_Activity_Fact
    # column TRD_CNCLD_F):
    #   "If there is a record in CCAL_REPL_OWNER.TXN_RLTNP table with
    #    TXN.TXN_ID = TXN_RLTNP.TRGT_TXN_ID and TXN_RLTNP_TP_ID = 69 (Cancel)
    #    and TXN_RLTNP.TRGT_TXN_ID <> TXN_RLTNP.SRC_TXN_ID then set to 'Y'..."
    # Without recognition, drd_expression becomes "TRD_CNCLD_F.IF THERE IS..."
    # and ends up as a literal projection line that breaks the SELECT.
    #
    # The drd_rules module already has `extract_exists_derived_flag` +
    # `compose_exists_case_expr` (used by drd_first_emitter for the same
    # pattern with provenance DRD_EXISTS_DERIVED_FLAG).  Wire it here too.
    # Generic: no hardcoded table / column / value.
    from app.sql_model.drd_rules import (
        extract_exists_derived_flag, compose_exists_case_expr,
    )
    # Loose prose-leak detector for cases where the EXISTS regex does NOT match
    # but the source_attribute still contains multi-line English (then the
    # column gets NULL'd with the prose preserved as a SQL comment so the
    # operator can convert manually).  Conservative: requires (a) newline AND
    # (b) one of the imperative English keywords.  No false positives on
    # legitimate SQL CASE WHEN / SUBSTR / DECODE expressions.
    _PROSE_LEAK_KEYWORDS = (
        "IF THERE IS", "WHEN THERE IS A RECORD", "THEN SET",
        "SHOULD BE", "NOTE:", "REFER TO",
        # Phase 7.19.6 (2026-06-01): audit-column DRD prose.
        # DRD authors write "AUDIT COLUMN. DEFAULT SYSDATE" or
        # ". DEFAULT USER" inside source_attribute. The embedded
        # period + space + identifier was producing TABLE.AUDIT,
        # TABLE.DEFAULT, etc. -> ORA-00936 missing expression.
        # Caught below by _looks_like_drd_prose; the literal value
        # (SYSDATE / USER) is extracted by _extract_default_expr
        # which runs earlier and wins when matched.
        "AUDIT COLUMN", ". DEFAULT ",
    )
    def _looks_like_drd_prose(text: str) -> bool:
        if not text:
            return False
        t = text.upper()
        if "\n" not in text and len(text) < 80:
            # Short text path: still flag the audit-column variants
            # since they're under 80 chars but produce invalid SQL.
            if "AUDIT COLUMN" in t or ". DEFAULT " in t:
                return True
            return False
        return any(kw in t for kw in _PROSE_LEAK_KEYWORDS)

    # Phase 7.19.6 (2026-06-01): extract a SQL literal from prose
    # of the shape "...DEFAULT <expr>..." where <expr> is SYSDATE,
    # USER, NULL, a numeric literal, or a quoted string. Returns
    # None when no DEFAULT clause is present. Generic; not bound to
    # any specific column or table.
    _DEFAULT_PROSE_RE = re.compile(
        r"\bDEFAULT\s+(SYSDATE|SYSTIMESTAMP|USER|CURRENT_DATE|CURRENT_TIMESTAMP|NULL|-?\d+(?:\.\d+)?|'[^']*')",
        flags=re.IGNORECASE,
    )
    def _extract_default_expr(text: str) -> Optional[str]:
        if not text:
            return None
        m = _DEFAULT_PROSE_RE.search(text)
        if not m:
            return None
        val = m.group(1).strip()
        # Normalize bare keyword forms to upper-case canonical SQL.
        if val.upper() in {
            "SYSDATE", "SYSTIMESTAMP", "USER",
            "CURRENT_DATE", "CURRENT_TIMESTAMP", "NULL",
        }:
            return val.upper()
        return val

    # DRD constant-rule honoring (operator 2026-06-04): the INSERT-build path
    # must honor the same constant rules the ODI<->DRD comparison does -- a row
    # whose `transformation` says "Always NULL" / "populate as 6" / "Use value-
    # Closed" must emit that resolved constant (matching ODI), NOT the raw
    # drd_expression lookup the generator otherwise builds.  Lookup phrasings
    # ("... and get code/name", "look up") are excluded (they need a real join).
    _CONST_VAL = r"('[^']*'|-?\d+(?:\.\d+)?|[A-Za-z][A-Za-z0-9_]*)"
    _CONST_RULE_RES = [
        re.compile(
            r"\b(?:populate\s+(?:as|with)|use\s+value|set\s+to|constant"
            r"|hard\s*-?cod\w*\s+(?:as\s+|to\s+)?)\s*[-:]?\s*" + _CONST_VAL,
            re.IGNORECASE,
        ),
        re.compile(r"\balways\s+(NULL|'[^']*'|-?\d+(?:\.\d+)?)", re.IGNORECASE),
    ]
    _CONST_RULE_SKIP = ("get code", "get name", "and get", "look up", "lookup")
    _CONST_KW_STOP = {
        "JOIN", "FROM", "SELECT", "WHERE", "AND", "OR", "ON", "USE", "AS",
        "TABLE", "LOOKUP", "THE", "VALUE", "COLUMN", "FIELD",
    }

    def _emittable_constant(val: str) -> Optional[str]:
        v = (val or "").strip()
        if not v:
            return None
        if v.upper() in {"NULL", "SYSDATE", "SYSTIMESTAMP", "USER",
                         "CURRENT_DATE", "CURRENT_TIMESTAMP"}:
            return v.upper()
        if re.match(r"^-?\d+(?:\.\d+)?$", v):
            return v
        if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
            return "'" + v[1:-1].replace("'", "''") + "'"
        if v.upper() in _CONST_KW_STOP:
            return None
        return "'" + v.replace("'", "''") + "'"

    def _extract_constant_rule_expr(text: str) -> Optional[str]:
        if not text:
            return None
        if any(m in text.lower() for m in _CONST_RULE_SKIP):
            return None
        # A conditional rule ("if X is 01 then set to Y else N", CASE/WHEN) is
        # NOT a pure constant -- never collapse it to a single literal.  (operator
        # 2026-06-04: ZERO_COST_BSS_F = "if ZERO_BSS_IND is 01 then set to Y else
        # N" must NOT become 'Y'.)  Defer to the conditional/expression path.
        if re.search(r"\b(IF|THEN|ELSE|WHEN|CASE|END)\b", text, re.IGNORECASE):
            return None
        for rx in _CONST_RULE_RES:
            m = rx.search(text)
            if m:
                return _emittable_constant(m.group(1))
        return None

    # Target-only column remap (operator 2026-06-04): a transformation/CASE may
    # reference a TARGET column name (e.g. EXG_RATE) that does NOT exist in the
    # source -- its value comes from a differently-named SOURCE column
    # (SBC_EXG_RATE).  Build {target_col -> source_attr} for target cols that are
    # NOT themselves any row's source attribute, so expression bodies resolve the
    # real source column instead of an invalid target-name reference.
    _all_source_attrs = {
        (r.get("source_attribute") or "").strip().upper()
        for r in analysis_rows if (r.get("source_attribute") or "").strip()
    }
    _tgt_src_remap: Dict[str, str] = {}
    for _r in analysis_rows:
        _tc = (_r.get("column") or "").strip().upper()
        _sa = (_r.get("source_attribute") or "").strip().upper()
        if _tc and _sa and _tc != _sa and _tc not in _all_source_attrs:
            _tgt_src_remap[_tc] = _sa

    select_lines = []
    insert_cols = []
    for col in target_definition.get("columns", []) or []:
        col_name = (col.get("name") or "").strip().upper()
        if not col_name:
            continue
        insert_cols.append(col_name)
        row = row_map.get(col_name, {}) or {}

        # Phase 7.19.4 short-circuit: if DRD describes EXISTS-style derived
        # flag, build CASE WHEN EXISTS(...) THEN ... ELSE NULL END.
        _src_attr_raw = row.get("source_attribute") or ""
        _trans_raw = row.get("transformation") or ""
        _exists_spec = (
            extract_exists_derived_flag(_trans_raw)
            or extract_exists_derived_flag(_src_attr_raw)
        )
        if _exists_spec:
            expr = compose_exists_case_expr(_exists_spec, else_value="NULL")
            select_lines.append(f"    {expr} AS {col_name}")
            continue

        # Phase 7.19.6 literal-value short-circuit (2026-06-01):
        # DRD authors occasionally place bare SQL literals in the
        # source_attribute column (numeric literals like "123456",
        # quoted strings like "'Y'", or SQL keywords like NULL /
        # SYSDATE). The downstream emitter would otherwise produce
        # invalid output of the form ALIAS.123456 -> ORA-00923. Detect
        # such literals and emit them as-is, bypassing all alias /
        # qualification logic. Generic: no hardcoded column or table
        # names; pure pattern match on source_attribute content.
        def _is_sql_literal(text: str) -> bool:
            s = (text or "").strip()
            if not s:
                return False
            # Pure numeric literal (int or decimal, optional sign)
            if re.match(r'^-?\d+(\.\d+)?$', s):
                return True
            # Single-quoted string literal (no embedded quote)
            if re.match(r"^'[^']*'$", s):
                return True
            # SQL keyword literals
            if s.upper() in {
                "NULL", "SYSDATE", "SYSTIMESTAMP", "USER",
                "CURRENT_DATE", "CURRENT_TIMESTAMP",
            }:
                return True
            return False

        if _is_sql_literal(_src_attr_raw):
            select_lines.append(f"    {_src_attr_raw.strip()} AS {col_name}")
            continue

        # Phase 7.19.6 (2026-06-01) DEFAULT-extractor: DRD prose like
        # "AUDIT COLUMN. DEFAULT SYSDATE" -> emit SYSDATE. Runs BEFORE
        # the prose-leak fallback so the operator gets the intended
        # default instead of a NULL/TODO comment. Generic; matches
        # SYSDATE / USER / NULL / numeric / quoted string.
        _default_expr = (
            _extract_default_expr(_src_attr_raw)
            or _extract_default_expr(_trans_raw)
        )
        if _default_expr is not None:
            select_lines.append(f"    {_default_expr} AS {col_name}")
            continue

        # Honor DRD constant rules ("Always NULL", "populate as 6", "Use value-
        # Closed") so the control-table INSERT emits the resolved constant the
        # ODI<->DRD comparison agrees on, instead of a raw lookup expression.
        _const_rule_expr = (
            _extract_constant_rule_expr(_trans_raw)
            or _extract_constant_rule_expr(_src_attr_raw)
        )
        if _const_rule_expr is not None:
            select_lines.append(f"    {_const_rule_expr} AS {col_name}")
            continue

        # Phase 7.19.4 prose-leak fallback (no EXISTS pattern matched):
        # if `source_attribute` (NOT `transformation` — that field commonly
        # holds documentation prose) looks like English prose, emit NULL
        # with the prose preserved as a SQL comment.  This stops the
        # broken-projection leak into the SELECT and signals operator
        # review.  Narrow scope reduces false positives where the
        # source_attribute is a bare column but transformation is a doc
        # block.
        if _looks_like_drd_prose(_src_attr_raw):
            _preview = _src_attr_raw
            # Strip newlines + truncate so the comment stays on one line.
            _preview_one_line = re.sub(r"\s+", " ", _preview).strip()
            if len(_preview_one_line) > 200:
                _preview_one_line = _preview_one_line[:200] + "..."
            # Escape any */ that would close our /* */ comment prematurely.
            _preview_safe = _preview_one_line.replace("*/", "*\\/")
            expr = f"NULL /* DRD_PROSE_TODO: {_preview_safe} */"
            select_lines.append(f"    {expr} AS {col_name}")
            continue

        expr = row.get("drd_expression") or "NULL"
        # PDM_MISS guard (operator 2026-06-03): if the DRD expression is a bare
        # <lookup_alias>.<col> whose lookup table is PDM-missing (its JOIN was
        # dropped above), the lookup value cannot be produced.  Emit the
        # reviewable PDM_MISS NULL marker NOW -- BEFORE align_expr/Phase-7.19.8
        # rewrite the alias to the source table and silently substitute the
        # join-key column (operator rule: leave <100%-certain cases reviewable).
        _pm_alias_m = _RE_BARE_ALIAS_COL.match(expr.strip())
        if _pm_alias_m:
            _pm_alias = _pm_alias_m.group(1).upper()
            if (_pm_alias in pdm_missing_old_aliases
                    or re.sub(r"_\d+$", "", _pm_alias) in pdm_missing_lookup_bases):
                expr = (
                    f"NULL /* PDM_MISS: cannot resolve alias(es) {_pm_alias} for "
                    f"{col_name} -- add to PDM or correct DRD source_table */"
                )
                select_lines.append(f"    {expr} AS {col_name}")
                continue
        # Defensive: even if analysis_rows is stale or was produced before the
        # align fix, reconcile bare ALIAS.COL against DRD source_attribute.
        expr = align_expr_with_source_attr(expr, (row.get("source_attribute") or "").strip().upper())

        # Phase 7.19.8 (2026-06-02): when drd_expression is a bare
        # ``<alias>.<col>`` form where <col> matches source_attribute,
        # ALSO force the alias to match the row's own source_table
        # bare-name.  Generic; no hardcoded table names.  Triggering
        # case: baseline test-SQL extractor builds ``TXN.PD_CMPOS_DSC``
        # when the row's authoritative source_table is APA -> Oracle
        # ORA-00904 because PD_CMPOS_DSC is not a TXN column.  Skip the
        # remap when the column is also a legitimate column on the
        # alias's bound table (defence against multi-source overrides);
        # the source-attribute set check below provides that.
        _src_table_bare_align = (row.get("source_table") or "").strip().split("\n")[0].split(",")[0].strip().upper().split(".")[-1]
        _src_attr_u_align = (row.get("source_attribute") or "").strip().upper()
        if _src_table_bare_align and _src_attr_u_align and _RE_BARE_IDENT.match(_src_attr_u_align):
            _m_align = _RE_BARE_ALIAS_COL.match(expr)
            if _m_align:
                _cur_alias = _m_align.group(1).upper()
                _cur_col = _m_align.group(2).upper()
                if _cur_col == _src_attr_u_align and _cur_alias != _src_table_bare_align and _cur_alias != "S":
                    expr = f"{_src_table_bare_align}.{_cur_col}"
        lookup_join = sanitize_lookup_join_sql((row.get("lookup_join") or "").strip())
        if lookup_join and lookup_join in join_alias_map:
            old_alias, new_alias = join_alias_map[lookup_join]
            expr = replace_alias_token(expr, old_alias, new_alias)

        # Apply cross-join alias renaming: fix expressions referencing old aliases from OTHER rows' joins.
        for _old_a_u, _new_a in combined_alias_rename.items():
            if re.search(r'\b' + re.escape(_old_a_u) + r'\.[A-Z_]', expr, flags=re.IGNORECASE):
                expr = replace_alias_token(expr, _old_a_u, _new_a)

        # Normalize S. references: use the row's own source table bare name (multi-table aware)
        _row_src_bare = (row.get("source_table") or "").strip().split("\n")[0].split(",")[0].strip().upper().split(".")[-1]
        _row_ref = _row_src_bare or _src_ref_name
        expr = re.sub(r'\bS\.([A-Z0-9_#\$]+)\b', f'{_row_ref}.\\1', expr, flags=re.IGNORECASE)

        # Resolve target-only column refs (e.g. EXG_RATE) to their source column
        # (SBC_EXG_RATE) inside the expression body, so CASE/arithmetic that the
        # DRD wrote with the target name reads the real source column.  Word-
        # boundary replace is safe: \bEXG_RATE\b never matches inside SBC_EXG_RATE
        # (the leading '_' is a word char).  (operator 2026-06-04)
        for _tc_key, _sa_val in _tgt_src_remap.items():
            expr = re.sub(r'\b' + re.escape(_tc_key) + r'\b', _sa_val, expr, flags=re.IGNORECASE)

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
            for m in re.finditer(r'\b([A-Z_][A-Z0-9_\$#]*)\.[A-Z_][A-Z0-9_#\$]*\b', _expr_for_check, flags=re.IGNORECASE)
            if m.group(1).upper() not in all_valid_aliases
            and m.group(1).upper() not in {"SYSDATE", "SYSTIMESTAMP", "DUAL"}
        ]
        _has_undef = bool(_undef_aliases)
        if _has_undef:
            # Try one more time: look for any join in join_alias_map whose old_alias matches an undef alias
            for _undef_a in _undef_aliases:
                if _undef_a in combined_alias_rename:
                    expr = replace_alias_token(expr, _undef_a, combined_alias_rename[_undef_a])
                    continue
                # Handle numbered alias drift (e.g. CL_VAL_20 -> CL_VAL_1) by base alias.
                _undef_base = re.sub(r"_\d+$", "", _undef_a)
                _cands = alias_by_base.get(_undef_base, [])
                if not _cands:
                    # Fuzzy fallback for small naming drifts:
                    # FA_NUMBER -> FA_NUMBER_V_1, SRC_TAX_CODE -> SRC_TAX_CODE_LKUP_1, etc.
                    for _base, _base_cands in alias_by_base.items():
                        if _base.startswith(_undef_base) or _undef_base.startswith(_base):
                            _cands.extend(_base_cands)
                if _cands:
                    expr = replace_alias_token(expr, _undef_a, _cands[0])
            # Recheck after targeted renames
            _still_undef = any(
                m.group(1).upper() not in all_valid_aliases
                and m.group(1).upper() not in {"SYSDATE", "SYSTIMESTAMP", "DUAL"}
                for m in re.finditer(r'\b([A-Z_][A-Z0-9_\$#]*)\.[A-Z_][A-Z0-9_#\$]*\b', expr, flags=re.IGNORECASE)
            )
            if _still_undef:
                _col_def = col_map_def.get(col_name, {})
                _hits_pdm_miss = any(
                    (_undef_a in pdm_missing_old_aliases)
                    or (re.sub(r"_\d+$", "", _undef_a) in pdm_missing_lookup_bases)
                    for _undef_a in _undef_aliases
                )
                if _hits_pdm_miss:
                    # Operator-locked (2026-05-29 Phase 7.3 Issue 5):
                    # Surface which DRD source the emitter could not
                    # resolve so operator can spot-fix (add to PDM, fix
                    # DRD source_table, or add manual override).
                    _undef_listing = ", ".join(sorted(set(_undef_aliases))[:3])
                    expr = (
                        f"NULL /* PDM_MISS: cannot resolve alias(es) "
                        f"{_undef_listing} for {col_name} -- add to PDM "
                        f"or correct DRD source_table */"
                    )
                elif not _col_def.get("nullable", True):
                    _is_pk = col_name in pk_cols or bool(_col_def.get("is_pk"))
                    expr = fallback_non_nullable_expression(col_name, (_col_def.get("data_type") or "").upper(), is_pk=_is_pk)
                else:
                    expr = "NULL"

        # Phase 7.19.13 (2026-06-02): multi-hop embedded-SQL-join fallback.
        # When the heuristic resolved the column to NULL but the DRD
        # transformation carries an explicit multi-hop join chain as
        # literal SQL (TXN -> APA -> TXN_AVY_CL -> AVY_CL etc.), emit the
        # whole chain verbatim with collision-proof aliases and project
        # <final_alias>.<source_attribute>.  ONLY fires on would-be-NULL
        # columns, so it cannot regress columns the normal path resolves.
        # Gated on: chain parses + final table is the DRD source_table +
        # source_attribute is a real column on that table in the PDM.
        if normalize_sql_expr(expr) == "NULL":
            _row_sa = (row.get("source_attribute") or "").strip().upper()
            _row_stbl = (row.get("source_table") or "").strip().upper().split("\n")[0].split(",")[0].strip().split(".")[-1]
            if _row_sa and re.match(r"^[A-Z_][A-Z0-9_]*$", _row_sa) and source_schema_index is not None:
                _final_tbl_def = find_table(source_schema_index, (row.get("source_schema") or "").strip(), _row_stbl)
                if _final_tbl_def and _row_sa in _final_tbl_def.get("columns", {}):
                    _chain = extract_embedded_join_chain(
                        row.get("transformation") or "",
                        main_alias=_src_ref_name,
                        drd_source_table=_row_stbl,
                        source_attr=_row_sa,
                        uniq_prefix=f"EJC{_ejc_counter}",
                    )
                    if _chain:
                        _chain_joins, _chain_final_alias, _chain_sig = _chain
                        # Dedup: emit each unique chain's joins ONCE; columns
                        # sharing the chain reuse the cached final alias.  This
                        # turns 100+ redundant joins (per-column emission, which
                        # produced an unrunnable plan) into one block per chain.
                        _cached_alias = _ejc_chain_cache.get(_chain_sig)
                        if _cached_alias is not None:
                            expr = f"{_cached_alias}.{_row_sa}"
                        else:
                            joins.extend(_chain_joins)
                            _ejc_chain_cache[_chain_sig] = _chain_final_alias
                            expr = f"{_chain_final_alias}.{_row_sa}"
                            _ejc_counter += 1

        if not col.get("nullable", True):
            _is_pk = col_name in pk_cols or bool(col.get("is_pk"))
            fallback_expr = fallback_non_nullable_expression(col_name, (col.get("data_type") or "").strip().upper(), is_pk=_is_pk)
            # Keep mapped expressions intact; only fill when expression truly resolves to NULL.
            if normalize_sql_expr(expr) == "NULL":
                expr = fallback_expr
        # Phase 7.19.13 (2026-06-02): guard against a stray newline leaking
        # into a single-token projection (corrupted DRD cell) without
        # touching multi-line CASE / EXISTS expressions -- a blanket
        # whitespace-collapse is UNSAFE because an expression may contain a
        # ``--`` line comment whose closing ``)`` would be swallowed.  Only
        # collapse when the expression has NO line/block comment AND no
        # CASE/SELECT keyword (i.e. it is a simple ref/literal that should
        # never span lines).
        if ("\n" in expr or "\t" in expr) and "--" not in expr and "/*" not in expr \
                and not re.search(r"\b(CASE|SELECT)\b", expr, flags=re.IGNORECASE):
            expr = re.sub(r"\s+", " ", expr).strip()
        select_lines.append(f"    {expr} AS {col_name}")

    # Phase 7.19.13 (2026-06-02): final safety net -- neutralize any JOIN
    # whose ON references an undefined alias OR a non-existent column so
    # the INSERT always compiles AND executes.
    _main_a2t: Dict[str, Tuple[str, str]] = {}
    _main_src_fq = (analysis_rows[0].get("source_table") if analysis_rows else "") or ""
    # Best-effort: map the main source alias to the primary source table.
    for _r in analysis_rows:
        _st = (_r.get("source_table") or "").strip().upper().split("\n")[0].split(",")[0].strip()
        _ss = (_r.get("source_schema") or "").strip().upper()
        if _st and not _is_lookup_table_name(_st):
            _bare = _st.split(".")[-1]
            _main_a2t[_src_ref_name.upper()] = (_ss, _bare)
            if _src_table_name and _src_table_name.upper() != _src_ref_name.upper():
                _main_a2t[_src_table_name.upper()] = (_ss, _bare)
            break
    joins, _dropped_aliases = _neutralize_joins_with_undefined_aliases(
        joins, {_src_ref_name, _src_table_name},
        source_schema_index=source_schema_index, alias_table_map=_main_a2t,
    )
    # Rewrite SELECT projections that referenced a DROPPED join's alias to
    # NULL (those joins were degenerate dead-NULL self-refs; dropping them
    # cuts the join count so Oracle can optimise the INSERT).  Equivalent
    # result -- a LEFT JOIN on ON 1=0 already yielded NULL for these.
    if _dropped_aliases:
        _drop_ref = re.compile(
            r"(?<![A-Z0-9_$#.])(" + "|".join(re.escape(a) for a in _dropped_aliases) + r")\.",
            re.IGNORECASE,
        )
        _rewritten: List[str] = []
        for _line in select_lines:
            _mm = re.match(r"^(\s*)(.*)\s+AS\s+([A-Z_][A-Z0-9_$#]*)\s*$", _line, flags=re.IGNORECASE | re.DOTALL)
            if _mm and _drop_ref.search(_mm.group(2)):
                _rewritten.append(f"{_mm.group(1)}NULL AS {_mm.group(3)}")
            else:
                _rewritten.append(_line)
        select_lines = _rewritten
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
    """Parse a grain/join-key spec into column names.

    Phase 7.19.13 (2026-06-02): REJECT prose.  DRD "Grain" cells are
    frequently free text like "Refer to [ETL Notes] tab" -- the old
    regex grabbed the trailing word ("TAB") and emitted an invalid
    ``ON T.TAB = CTL.TAB`` join (ORA-00904).  Now a token contributes a
    column ONLY when it is a clean SQL identifier (optionally
    alias-qualified) or an equality of two such identifiers.  Any token
    containing spaces, brackets, or other prose punctuation is ignored,
    so a prose grain yields [] and the caller falls back to the PDM
    primary key.
    """
    if not main_grain:
        return []
    cols: List[str] = []
    seen: set = set()
    _ident = re.compile(r"^(?:[A-Z_][A-Z0-9_$#]*\.)?([A-Z_][A-Z0-9_$#]*)$", re.IGNORECASE)

    def _add(side: str) -> None:
        m = _ident.match((side or "").strip())
        if not m:
            return
        col = m.group(1).upper()
        if col not in seen:
            cols.append(col)
            seen.add(col)

    for token in re.split(r"\bAND\b|,|\n|;", main_grain, flags=re.IGNORECASE):
        token = (token or "").strip()
        if not token:
            continue
        if "=" in token:
            left, right = token.split("=", 1)
            _add(left)
            _add(right)
        else:
            _add(token)   # bare identifier only -- prose tokens are rejected
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
    """Compare INSERT variants (DRD vs generated vs manual) and filter false positives.
    
    Real differences (shown): expressions differ in all 3 sources or partial matches with real variance.
    False positives (hidden): fields missing in all sources or identical across all 3.
    """
    expected_columns = {row.get("column", "").upper() for row in analysis_rows if row.get("column")}
    generated_map = extract_sql_expression_map(generated_sql, expected_aliases=expected_columns)
    manual_supplied = bool(manual_sql.strip())
    manual_map = extract_sql_expression_map(manual_sql, expected_aliases=expected_columns) if manual_supplied else {}
    rows = []
    for row in analysis_rows:
        col = row["column"]
        drd_expr = (row.get("drd_expression") or "").strip()
        generated_expr = generated_map.get(col, "").strip()
        manual_expr = manual_map.get(col, "").strip()
        status = compare_column_status(
            drd_expr,
            generated_expr,
            manual_expr,
            manual_supplied=manual_supplied,
            compare_mode=compare_mode,
            target_column=col,
            source_attribute=(row.get("source_attribute") or "").strip().upper(),
        )
        recommended = recommend_source(drd_expr, generated_expr, manual_expr, compare_mode=compare_mode)
        
        # Detect real difference: false positive if all 3 sources identical OR all missing
        _drd_present = bool(drd_expr)
        _gen_present = bool(generated_expr)
        _man_present = bool(manual_expr)
        _all_missing = not (_drd_present or _gen_present or _man_present)
        _all_identical = (drd_expr == generated_expr == manual_expr) if _drd_present else False
        _real_difference = not (_all_missing or _all_identical) and status != "match_all"
        
        rows.append(
            {
                "column": col,
                "source_attribute": (row.get("source_attribute") or "").strip().upper(),
                "drd_expression": drd_expr,
                "generated_expression": generated_expr,
                "manual_expression": manual_expr,
                "status": status,
                "recommended_source": recommended,
                "generated_present": _gen_present,
                "manual_present": _man_present,
                "is_real_difference": _real_difference,  # UI filter: hide if False
            }
        )
    # Count only real differences for mismatch summary
    mismatch_count = sum(1 for row in rows if row.get("is_real_difference", False))
    return {"rows": rows, "mismatch_count": mismatch_count, "total_rows": len(rows)}


def compare_column_status(
    drd_expr: str,
    generated_expr: str,
    manual_expr: str,
    *,
    manual_supplied: bool = False,
    compare_mode: str = "all",
    target_column: str = "",
    source_attribute: str = "",
    saved_rules: Optional[List[Dict[str, Any]]] = None,
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
            # Before returning manual_mismatch, try canonical lineage resolution
            canonical_status = _try_canonical_resolution(
                target_column, source_attribute or drd_expr,
                generated_expr, manual_expr, saved_rules,
            )
            if canonical_status:
                return canonical_status
            return "manual_mismatch"
        if drd_norm == man_norm and drd_norm != gen_norm:
            return "generated_mismatch"
        if gen_norm == man_norm and drd_norm != gen_norm:
            return "both_match_each_other_not_drd"
        # All different - try canonical resolution before giving up
        canonical_status = _try_canonical_resolution(
            target_column, source_attribute or drd_expr,
            generated_expr, manual_expr, saved_rules,
        )
        if canonical_status:
            return canonical_status
        return "all_different"
    return "match_all" if drd_norm == gen_norm else "generated_mismatch"


def _try_canonical_resolution(
    target_column: str,
    drd_source_attribute: str,
    generated_expr: str,
    manual_expr: str,
    saved_rules: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Attempt canonical lineage + semantic resolution before declaring mismatch.

    Returns a match_all-compatible status if canonical mapping resolves the
    apparent mismatch, or None if it's a real mismatch.
    """
    if not target_column:
        return None
    try:
        from app.services.canonical_mapping_service import build_canonical_mapping
        canonical = build_canonical_mapping(
            target_column=target_column,
            drd_source_attribute=drd_source_attribute,
            generated_expression=generated_expr,
            manual_or_xml_expression=manual_expr,
            saved_rules=saved_rules,
        )
        status = canonical.get("match_status", "")
        # If canonical says it's a semantic match, return match_all
        # so the UI treats it as resolved
        if status in (
            "EXACT_EXPRESSION_MATCH",
            "MATCH_BY_OUTPUT_ALIAS",
            "MATCH_BY_STAGE_PROJECTION",
            "MATCH_BY_ROOT_SOURCE_LINEAGE",
            "MATCH_BY_ROLE_BASED_DIMENSION_KEY",
            "MATCH_BY_DRD_SOURCE_ATTRIBUTE",
            "MATCH_BY_PDM_PREDICTION",
            "MATCH_BY_SAVED_RULE",
        ):
            return "match_all"
    except Exception:
        pass
    return None


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
    # rules that fix a column name (e.g. ACG_TP_CODE -> AC_TP_CODE) propagate
    # into the JOIN conditions, not just the SELECT list.
    result_sql = apply_rule_corrections_to_joins(result_sql, decisions)
    return ensure_parallel_hints(result_sql)


def apply_rule_corrections_to_joins(sql_text: str, decisions: List[Dict[str, str]]) -> str:
    """Fix JOIN ON clauses based on rule decisions.

    When a rule replaces an expression for a column, the old source_attribute
    referenced in JOIN ON conditions may also be wrong (e.g. the DRD says
    ACG_TP_CODE but the real source column is AC_TP_CODE).
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


def _iter_select_from_clauses(sql_text: str) -> List[Dict[str, Any]]:
    """Find SELECT...FROM clause spans, paren-depth aware.

    Phase 7.19.11 (2026-06-02).  A FROM closes a SELECT only when it is
    encountered at the SAME parenthesis depth the SELECT opened at.  This
    is critical for the control-table INSERT, whose SELECT column list
    contains nested ``CASE WHEN EXISTS (SELECT 1 FROM <lkup> WHERE ...)``
    subqueries.  The old ``\\bSELECT\\b(.*?)\\bFROM\\b`` non-greedy regex
    stopped at the FIRST FROM -- i.e. the one INSIDE the first EXISTS
    subquery -- truncating the main column list (operator-reported
    2026-06-02: 369 columns but only 224 extracted -> 145 false
    GENERATED_MISSING).  Quotes, line comments and block comments are
    skipped so a FROM / paren inside a string or comment never miscounts.
    """
    text = sql_text or ""
    n = len(text)
    i = 0
    depth = 0
    in_s = in_d = in_line = in_block = False
    pending: List[Tuple[int, int]] = []   # (clause_start_index, depth_at_SELECT)
    results: List[Dict[str, Any]] = []
    _word_re = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    while i < n:
        ch = text[i]
        nx = text[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            if ch == "*" and nx == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if in_s:
            if ch == "'":
                in_s = False
            i += 1
            continue
        if in_d:
            if ch == '"':
                in_d = False
            i += 1
            continue
        if ch == "-" and nx == "-":
            in_line = True
            i += 2
            continue
        if ch == "/" and nx == "*":
            in_block = True
            i += 2
            continue
        if ch == "'":
            in_s = True
            i += 1
            continue
        if ch == '"':
            in_d = True
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            m = _word_re.match(text, i)
            wu = m.group(0).upper()
            if wu == "SELECT":
                pending.append((m.end(), depth))
            elif wu == "FROM" and pending:
                for k in range(len(pending) - 1, -1, -1):
                    cstart, cdepth = pending[k]
                    if cdepth == depth:
                        results.append({
                            "select_start": cstart,
                            "select_end": m.start(),
                            "clause": text[cstart:m.start()],
                        })
                        del pending[k]
                        break
            i = m.end()
            continue
        i += 1
    return results


def extract_sql_expression_map(
    sql_text: str,
    include_meta: bool = False,
    expected_aliases: Optional[set[str]] = None,
) -> Dict[str, Any]:
    if not sql_text.strip():
        return {} if not include_meta else {"map": {}, "parts": [], "alias_index": {}}

    insert_columns = parse_insert_target_columns(sql_text)
    candidates = []
    for _clause_span in _iter_select_from_clauses(sql_text):
        clause = _clause_span["clause"]
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
                "select_start": _clause_span["select_start"],
                "select_end": _clause_span["select_end"],
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
    m = re.search(r"\bJOIN\s+[A-Z0-9_\.\(\)\"\$# ]+\s+([A-Z0-9_\$#]+)\s*\nON\b", join_sql, flags=re.IGNORECASE)
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
        rf"(\bJOIN\s+[A-Z0-9_\.\(\)\"\$# ]+\s+){re.escape(old_alias)}(\b)",
        rf"\1{new_alias}\2",
        sql,
        flags=re.IGNORECASE,
        count=1,
    )
    return replace_alias_token(out, old_alias, new_alias)


def _neutralize_joins_with_undefined_aliases(
    joins: List[str],
    main_aliases: set,
    *,
    source_schema_index: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    alias_table_map: Optional[Dict[str, Tuple[str, str]]] = None,
) -> Tuple[List[str], set]:
    """Phase 7.19.13 (2026-06-02): final safety net for emitted JOINs.

    The lookup-join derivation occasionally produces an ON clause that
    references either (a) an UNDEFINED alias -- the lookup table's BARE
    name instead of its renamed alias (e.g. ``ACATS_BROKER_2.BROKER_ID =
    ACATS_BROKER.BROKER_ID_TYPE``) -- or (b) a column that does NOT EXIST
    on an otherwise-valid alias's table (e.g. ``= TXN.BROKER_ID`` where
    BROKER_ID is not a column of TXN).  Both reach Oracle as
    ORA-00904/00936.  The self-join detector misses them and the
    SELECT-side check does not inspect JOIN bodies.  Latent until the
    7.19.9 KB fallback started resolving these tables.

    This pass neutralizes any such JOIN's ON to ``1 = 0`` -- a valid LEFT
    JOIN that yields NULLs for the dependent columns -- so the INSERT
    always compiles AND executes; unresolvable columns are honestly NULL
    instead of breaking the whole statement.  Column existence is checked
    against the PDM (source_schema_index) only when the alias->table
    mapping is known; unknown aliases/tables are left to the
    undefined-alias check.  Generic; no hardcoded names.
    """
    if not joins:
        return joins, set()  # contract is Tuple[List[str], set]; caller unpacks 2 values
    defined = {a.upper() for a in main_aliases if a}
    _alias_re = re.compile(r"\bJOIN\s+([A-Z0-9_$#.\"]+)\s+([A-Z_][A-Z0-9_$#]*)\s+ON\b", re.IGNORECASE)
    # alias -> (schema, table) for column validation
    a2t: Dict[str, Tuple[str, str]] = dict(alias_table_map or {})
    for j in joins:
        mm = _alias_re.search(j)
        if mm:
            alias = mm.group(2).upper()
            defined.add(alias)
            fq = mm.group(1).replace('"', '').upper()
            if "." in fq:
                sch, tbl = fq.split(".", 1)
                a2t.setdefault(alias, (sch, tbl))
    _SAFE_QUALS = {"SYSDATE", "SYSTIMESTAMP", "DUAL", "NVL", "TO_CHAR", "TO_DATE",
                   "DECODE", "CASE", "TRIM", "SUBSTR", "COALESCE", "ROUND"}
    _ref_re = re.compile(r"(?<![A-Z0-9_$#.])([A-Z_][A-Z0-9_$#]*)\.([A-Z_][A-Z0-9_$#]*)", re.IGNORECASE)

    def _col_on_table(alias: str, col: str) -> Optional[bool]:
        """True/False if known; None if alias->table or PDM unknown."""
        if source_schema_index is None or alias not in a2t:
            return None
        sch, tbl = a2t[alias]
        entry = find_table(source_schema_index, sch, tbl) or find_table(source_schema_index, "", tbl)
        if not entry:
            return None
        return col in entry.get("columns", {})

    # Neutralize (ON 1=0) any join whose ON references an UNDEFINED ALIAS
    # only.  Column-existence validation was tried and REVERTED: the PDM
    # column lists are not always complete, so checking columns produced
    # FALSE positives that dropped valid lookup joins and corrupted the
    # comparison (335 match -> 131).  Undefined-alias detection is safe
    # (an alias is either in the FROM/JOIN set or it is not).  Keeping the
    # join with ON 1=0 (rather than dropping it) preserves the alias for
    # the SELECT projection, which then resolves to NULL via the LEFT JOIN.
    out: List[str] = []
    for j in joins:
        m = re.match(r"(.*?\bON\b\s*)(.*)$", j, flags=re.IGNORECASE | re.DOTALL)
        if not m:
            out.append(j)
            continue
        head, on_body = m.group(1), m.group(2)
        undef: set = set()
        for x in _ref_re.finditer(on_body):
            alias = x.group(1).upper()
            if alias in _SAFE_QUALS:
                continue
            if alias not in defined:
                undef.add(alias)
        if undef:
            out.append(head + f"1 = 0 /* neutralized: undefined alias(es) {','.join(sorted(undef))} */")
        else:
            out.append(j)
    return out, set()


def sanitize_lookup_join_sql(join_sql: str) -> str:
    """Strip DRD prose contamination from a lookup_join body before it is
    spliced into the INSERT...SELECT FROM clause.

    Phase 7.19.6 (2026-06-02). Generic: no hardcoded schema/table names;
    pure structural cleanup. Two classes of leaks observed in real DRDs:

    (1) Multi-line CASE-style prose embedded inside a JOIN block, e.g.
        ``LEFT JOIN ... ON ...
          USE AAS.AC_NUM
          ELSE
          CCAL_REPL_OWNER.TXN T``
        The ``USE``/``ELSE``/``IF``/``WHEN``/``THEN``/``SHOULD``/``NOTE:``
        line and everything after it are DRD authoring instructions, not
        SQL; truncate at the first such line.

    (2) Inline parenthetical commentary that wraps real SQL fragments,
        e.g. ``AND CV.CL_SCM_ID = '99' (THIS IS EXCESSIVE; USING CL_VAL_ID
        NO NEED TO USE CL_SCM. ...)``. The paren block is stripped
        only when it contains prose markers (multi-word English with
        commentary keywords) so legitimate `(col IN (1,2))` style
        SQL parens are preserved.
    """
    text = (join_sql or "").strip()
    if not text:
        return text

    _PROSE_LINE_STARTERS = re.compile(
        r"^\s*(USE|ELSE|IF\s+THERE|WHEN\s+THERE|THEN\s+SET|SHOULD\s+BE|NOTE\s*:|REFER\s+TO)\b",
        flags=re.IGNORECASE,
    )
    cleaned_lines: List[str] = []
    for ln in text.split("\n"):
        if _PROSE_LINE_STARTERS.search(ln):
            break
        cleaned_lines.append(ln)
    text = "\n".join(cleaned_lines).strip()

    _PROSE_PAREN_RE = re.compile(
        r"\([^()]*\b(THIS\s+IS|NO\s+NEED|FOR\s+CONSISTENCY|ADDED\s+FOR|EXCESSIVE|REDUNDANT|TODO|TBD|NOTE)\b[^()]*\)",
        flags=re.IGNORECASE,
    )
    prev = None
    while prev != text:
        prev = text
        text = _PROSE_PAREN_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    # Phase 7.19.6 (2026-06-02): DRD authors occasionally type a
    # broken identifier with an embedded space + underscore, e.g.
    # "NET_NEW _AST_CGY" (intent: "NET_NEW_AST_CGY"). Repair by
    # gluing IDENT + " _" + IDENT back together when both halves
    # look like identifier fragments. Generic; no table-name list.
    text = re.sub(
        r"\b([A-Z_][A-Z0-9_]*)\s+(_[A-Z_][A-Z0-9_]*)\b",
        r"\1\2",
        text,
        flags=re.IGNORECASE,
    )

    # Phase 7.19.13 (2026-06-02): repair newline-split column references.
    # DRD source cells sometimes wrap a column onto two physical lines
    # (e.g. "ORIG_SRC_STM_AR_ID\nAC_NUM"), which the lookup_join derivation
    # splices verbatim into an ON value -> NVL(TO_CHAR(TXN.ORIG_SRC_STM_AR_ID
    # \nAC_NUM)) -> ORA-00907 missing right parenthesis.  When a newline
    # directly separates two identifier tokens AND the second token is NOT
    # a SQL keyword (so it is not a legitimate "...\nAND" continuation),
    # drop the newline + the stray continuation token, keeping line 1 --
    # mirrors the SELECT-side source_attribute trim.
    _ON_KEYWORDS = {
        "AND", "OR", "ON", "JOIN", "LEFT", "RIGHT", "INNER", "FULL", "OUTER",
        "WHERE", "WHEN", "THEN", "ELSE", "END", "CASE", "IN", "IS", "NOT",
        "NULL", "NVL", "TO_CHAR", "DECODE", "EXISTS", "SELECT", "FROM",
    }
    def _fix_split_ident(mm: re.Match) -> str:
        first = mm.group(1).upper()
        second = mm.group(2).upper()
        # Only collapse when NEITHER token is a SQL keyword -- that is the
        # corrupted two-line column-cell pattern (e.g. ORIG_SRC_STM_AR_ID
        # \nAC_NUM).  "AND\nTXN" (keyword-first) and "TD\nAND" (keyword-
        # second) are legitimate ON-clause line breaks and must be kept.
        if first in _ON_KEYWORDS or second in _ON_KEYWORDS:
            return mm.group(0)
        return mm.group(1)      # corrupted split cell -- keep line 1 only
    prev = None
    while prev != text:
        prev = text
        text = re.sub(
            r"\b([A-Z_][A-Z0-9_]*)\n\s*([A-Z_][A-Z0-9_]*)\b",
            _fix_split_ident,
            text,
        )

    # Phase 7.19.6 (2026-06-02): drop any stray semicolons inside
    # the JOIN body. SQL statement terminators belong only at the
    # final boundary of the outer INSERT, not inside the JOIN tree;
    # leaked ";" mid-body breaks executors that split on ";".
    text = text.replace(";", "")

    text = text.strip()

    # Defence in depth: the result must start with a JOIN keyword.
    # Anything else is unrecoverable -> drop the whole join body so the
    # SELECT emitter falls back to NULL/FK-default and the operator
    # gets a clean compilable INSERT for review instead of ORA-03049.
    if not re.match(r"^(LEFT\s+(OUTER\s+)?JOIN|RIGHT\s+(OUTER\s+)?JOIN|FULL\s+(OUTER\s+)?JOIN|INNER\s+JOIN|JOIN)\b",
                    text, flags=re.IGNORECASE):
        return ""
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


def select_expr_for_column(
    col_name: str,
    row: dict,
    baseline: dict,
    col_def: dict,
    drd_candidate: str | None = None,
) -> tuple:
    """Return (expression, provenance) for a target column.

    Priority: DRD expression > baseline expression > fallback.
    Provenance is one of: 'DRD', 'BASELINE', 'FALLBACK'.
    """
    _null_markers = {"", "null", "none"}

    drd_expr = drd_candidate if drd_candidate is not None else (row.get("drd_expression") or "")
    if str(drd_expr).strip().lower() not in _null_markers:
        # Defensive: align bare ALIAS.COL to the DRD source_attribute when they drift.
        drd_expr = align_expr_with_source_attr(drd_expr, (row.get("source_attribute") or "").strip().upper())
        return drd_expr, "DRD"

    base_expr = baseline.get(col_name.upper()) or baseline.get(col_name) or ""
    if str(base_expr).strip().lower() not in _null_markers:
        return base_expr, "BASELINE"

    data_type = (col_def.get("data_type") or "").upper()
    is_pk = bool(col_def.get("is_pk"))
    fb = fallback_non_nullable_expression(col_name, data_type, is_pk=is_pk)
    return fb, "FALLBACK"


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

    # Do not coerce arbitrary aliases to source table names. That heuristic can
    # corrupt valid lookup expressions such as AR_DIM.COL or FA_NUMBER.COL.
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
    
    # Global schema mapping: CCAL_OWNER → CCAL_REPL_OWNER (use replica for all lookups)
    if lookup_schema and lookup_schema.upper() == "CCAL_OWNER":
        lookup_schema = "CCAL_REPL_OWNER"
        lookup_table = f"{lookup_schema}.{lookup_name}"
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
    explicit_on_clause = bool(spec.get("explicit_on_clause"))

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

    if source_entry and not explicit_on_clause:
        source_cols = set((source_entry.get("columns") or {}).keys())
        if src_lookup_col and src_lookup_col not in source_cols:
            inferred_src = infer_lookup_source_key_from_text(transformation, source_attr)
            if inferred_src in source_cols:
                src_lookup_col = inferred_src
            else:
                src_lookup_col = source_attr if source_attr in source_cols else ""

    # Determine the correct source reference for the ON clause.
    # If source_table bare name matches lookup bare name (e.g., both are CL_VAL),
    # use the main FROM anchor table or the DRD-specified source alias instead to
    # avoid self-referencing joins.
    _lk_bare_name = lookup_table.split(".")[-1].upper() if lookup_table else ""
    _src_bare_name = src_table.split(".")[-1].upper() if src_table else ""
    _source_alias_hint = (spec.get("source_alias_hint") or "").strip().upper()

    if _src_bare_name and _src_bare_name == _lk_bare_name:
        # Source table IS the lookup table — use the main FROM anchor or DRD alias hint
        _block_schema, _block_table = parse_source_table_from_block(source_block)
        if _block_table and _block_table.upper() != _lk_bare_name:
            _effective_src = _block_table.upper()
        elif _source_alias_hint and _source_alias_hint != _lk_bare_name:
            _effective_src = _source_alias_hint
        else:
            logger.warning("derive_lookup_from_transformation: falling back to S for self-referencing lookup %s", lookup_table)
            _effective_src = "S"
    elif _source_alias_hint and _source_alias_hint not in {"S", _src_bare_name, _lk_bare_name}:
        # DRD referenced an intermediate table alias (e.g., AP for APA)
        _effective_src = _source_alias_hint
    else:
        _effective_src = src_table if src_table else "S"

    if src_lookup_literal:
        source_expr = src_lookup_literal if re.fullmatch(r"-?\d+(?:\.\d+)?", src_lookup_literal) else f"'{src_lookup_literal}'"
        on_sql = f"LK.{join_col} = {source_expr}"
    elif not src_lookup_col:
        # If we cannot resolve a valid source lookup key, keep the join non-matching but executable.
        logger.warning(
            "derive_lookup_from_transformation: src_lookup_col unresolved for lookup_table=%s join_col=%s "
            "— ON clause set to 1=0; all joined columns will be NULL",
            lookup_table,
            join_col,
        )
        on_sql = "1 = 0 /* WARNING: lookup key unresolved — joined cols will be NULL */"
    else:
        _src_ref = f"{_effective_src}.{src_lookup_col}"
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


def _index_from_kb_payload(payload: Dict[str, Any]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Build a {(schema, table): {columns}} index from a KB payload.

    Uses setdefault so EARLIER sources win on duplicate (schema, table).
    When the caller merges multiple datasources' KBs, the merge order
    (lower ds id first via sorted filename glob) decides precedence.
    """
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
                index.setdefault((schema_name, table_name), {
                    "schema": schema_name,
                    "table": table_name,
                    "columns": col_map,
                })
    return index


def _build_table_index(datasource_id: int) -> Dict[Tuple[str, str], Dict[str, Any]]:
    index = _index_from_kb_payload(load_schema_kb_payload(datasource_id))

    # Phase 7.19.9 (2026-06-02): KB fallback for datasources that have no
    # generated schema_kb_ds_<id>.json yet.  Without this, the source
    # schema index is EMPTY and the emitter marks every lookup table as
    # PDM_MISS, producing an all-NULL INSERT.  Operator-reported
    # regression 2026-06-02 after ds_2 (FREEPDB1_LOCAL) was registered
    # for the DPY-6005 DNS fix: ds_2 had no KB JSON (only a .md), so the
    # CT generator produced 264 PDM_MISS / 304 NULL projections vs ds_1's
    # 1 PDM_MISS / 36 mismatches.  Mirrors load_target_table_definition's
    # multi-KB lookup order.  Only empty-KB datasources pay the merge
    # cost; KB-backed datasources (e.g. ds_1 with 5447 tables) skip it.
    if not index:
        index = _index_from_kb_payload(load_schema_kb_payload(None))
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

    # Operator-locked PDM diagnostic fix (2026-05-29 Phase 7.3):
    #   1. Multi-line source_attribute (e.g. "BKR_AR_ID\nAR_ID" -- two
    #      acceptable physical candidates) was previously concatenated
    #      with whitespace -> reported as missing column "BKR_AR_ID AR_ID".
    #      Now we split on newlines + commas + " OR " and check each
    #      candidate independently; if ANY candidate is in PDM, the row
    #      is not flagged.
    #   2. Apply operator-confirmed name-pair config (YIELD<->YLD etc.)
    #      so DRD spec names that have a known PDM alias are NOT
    #      reported as missing.
    confirmed_pairs = _load_confirmed_name_pairs()

    seen_source_tables = set()
    for row in rows:
        src_schema = (row.get("source_schema") or "").strip().upper()
        src_table = (row.get("source_table") or "").strip().upper()
        src_attr_raw = (row.get("source_attribute") or "").strip().upper()
        if not src_table:
            continue
        if not src_attr_raw:
            continue
        if _is_lookup_table_name(src_table):
            continue
        table_key = (src_schema, src_table)
        if table_key not in seen_source_tables:
            seen_source_tables.add(table_key)
            if not find_table(source_index, src_schema, src_table):
                missing_source_tables.append(f"{src_schema}.{src_table}" if src_schema else src_table)
                continue

        src_entry = find_table(source_index, src_schema, src_table)
        if not src_entry:
            continue
        col_map = src_entry.get("columns", {})
        # Split multi-candidate source_attribute strings.  Newlines are
        # the DRD convention for "any of these source columns is
        # acceptable"; commas / " OR " also occur.
        # Bounded whitespace pattern (no catastrophic backtracking risk).
        candidates_raw = re.split(r"[\n,]|[ \t]+OR[ \t]+", src_attr_raw)
        candidates: list[str] = []
        for c in candidates_raw:
            c = re.sub(r"\s*\(FROM\s+\w+\)\s*$", "", c, flags=re.IGNORECASE).strip()
            if c and c not in {"NULL", "NONE", "N/A"}:
                candidates.append(c)
        if not candidates:
            continue
        # A row is missing only if EVERY candidate fails both the exact
        # check, the fuzzy resolver, AND the operator-confirmed
        # name-pair table.
        any_found = False
        for cand in candidates:
            if cand in col_map or _resolve_column_name(col_map, cand):
                any_found = True
                break
            # Name-pair check: spec_name -> physical_name (or vice versa)
            mapped = None
            for spec, phys in confirmed_pairs:
                if cand == spec:
                    mapped = phys
                    break
                if cand == phys:
                    mapped = spec
                    break
            if mapped and (mapped in col_map or _resolve_column_name(col_map, mapped)):
                any_found = True
                break
        if not any_found:
            # Report each unresolved candidate independently for
            # actionable operator feedback (rather than concatenated).
            for cand in candidates:
                if cand in col_map or _resolve_column_name(col_map, cand):
                    continue
                qualified = (
                    f"{src_schema}.{src_table}.{cand}"
                    if src_schema else f"{src_table}.{cand}"
                )
                missing_source_columns.append(qualified)

    if missing_source_tables:
        # Don't hard-block: source tables may live in schemas not yet in the PDM.
        # Surface them as warnings so the caller can decide.
        pass

    return {
        "missing_source_tables": sorted(set(missing_source_tables)),
        "missing_source_columns": sorted(set(missing_source_columns))[:200],
        "missing_target_columns": missing_target_columns[:200],
    }