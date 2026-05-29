"""Shared v9 generation pipeline.

Single entry point used by BOTH the GUI endpoint
(``POST /api/odi/scenario/compare``) and any CLI / batch script.  Eliminates
input divergence (cached JSON vs raw xlsx) and guarantees byte-identical
output for identical inputs.

Operator-locked invariants (2026-05-29):
  * One pipeline, one code path, both directions.
  * Pure -- no random / time / dict-order-dependent state.
  * Inputs are raw bytes (DRD xlsx + ODI xml + target schema/table).
  * Output is a structured ``V9Result`` with .insert_sql + provenance +
    Oracle validation report + comparison results.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class V9Result:
    insert_sql: str
    provenance: Dict[str, int]
    column_count: int
    join_count: int
    cte_count: int
    oracle_validation: Dict[str, Any]
    comparison_summary: Dict[str, Any]
    comparison_rows: List[Dict[str, Any]] = field(default_factory=list)
    drd_row_count: int = 0
    drd_parse_errors: List[Any] = field(default_factory=list)
    augmented_drd_rows: List[Dict[str, Any]] = field(default_factory=list)


def generate_v9(
    *,
    drd_bytes: bytes,
    drd_filename: str,
    odi_xml_bytes: bytes,
    target_schema: str,
    target_table: str,
    source_datasource_id: int = 3,
    target_datasource_id: int = 3,
) -> V9Result:
    """End-to-end v9 generation.  Same inputs -> same SQL byte-for-byte."""
    # 1) Parse DRD xlsx (raw -> rows).  We parse TWICE so we can compute
    # the set of struck-through target columns (rows where Y/Z/AA have
    # strike-through font in Excel).  These are de-scoped by the DRD author
    # and must be DROPPED entirely from the emitted INSERT -- not kept as
    # NULL placeholders (operator-locked 2026-05-29).
    from app.services.drd_import_service import parse_drd_file
    _common_kwargs = dict(
        file_bytes=drd_bytes,
        filename=drd_filename or "drd.xlsx",
        selected_fields=[
            "logical_name", "physical_name", "source_schema", "source_table",
            "source_attribute", "transformation", "notes",
        ],
        target_schema=target_schema,
        target_table=target_table,
        source_datasource_id=source_datasource_id,
        target_datasource_id=target_datasource_id,
    )
    parse_result = parse_drd_file(**_common_kwargs, exclude_strikethrough=True)
    drd_rows = parse_result.get("column_mappings", [])
    drd_errors = parse_result.get("errors", [])
    # Compute the de-scoped target set (struck-through rows).
    _all_targets_result = parse_drd_file(**_common_kwargs, exclude_strikethrough=False)
    _all_targets = {
        (r.get("physical_name") or "").strip().upper()
        for r in _all_targets_result.get("column_mappings", [])
        if r.get("physical_name")
    }
    _kept_targets = {
        (r.get("physical_name") or "").strip().upper()
        for r in drd_rows
        if r.get("physical_name")
    }
    out_of_scope_targets = {t for t in (_all_targets - _kept_targets) if t}

    # 2) Parse ODI XML
    from app.sql_model.odi_parser import OdiXmlParser
    model = OdiXmlParser(target_schema=target_schema, target_table=target_table).parse_bytes(odi_xml_bytes)

    # 3) ETL Notes index + global haystack
    from app.sql_model.drd_multi_sheet import parse_all_sheets, SheetRole
    from app.sql_model.etl_block_index import (
        build_block_index, find_block_references, resolve_block_body,
    )
    ms = parse_all_sheets(drd_bytes)
    idx = build_block_index(ms)
    all_etl_text = "\n".join(
        r.description for r in ms.extracted_rules if r.role == SheetRole.ETL_NOTES
    )

    # 4) Augment rows -- canonical, deterministic, no override of generic defaults
    aug: List[Dict[str, Any]] = []
    for r in drd_rows:
        r2 = dict(r)
        col = r2.get("physical_name") or r2.get("column") or ""
        r2["column"] = col
        r2["physical_name"] = col
        r2["_all_etl_text"] = all_etl_text
        txt = (r.get("transformation") or "") + "\n" + (r.get("notes") or "")
        refs = find_block_references(txt, idx)
        if refs:
            r2["etl_block_ref"] = refs[0]
            r2["etl_block_body"] = resolve_block_body(txt, idx) or ""
        aug.append(r2)

    # 5) Target definition (PDM)
    from app.services.control_table_service import load_target_table_definition
    try:
        tdef = load_target_table_definition(target_datasource_id, target_schema, target_table)
    except Exception:
        # Synthetic minimal target_definition if PDM not available
        tdef = {"columns": [
            {"name": c, "data_type": "VARCHAR2(4000)", "nullable": True}
            for c in model.final_insert_columns
        ]}

    # 6) Comparator (shared rule engine)
    from app.sql_model.comparator import (
        compare_drd_rows_to_model, comparison_summary, ComparisonResult,
    )
    from app.sql_model.types import ComparisonVerdict, MismatchKind
    cmp_results = compare_drd_rows_to_model(aug, model)

    # 6b) ODI_EXTRA detection (operator 2026-05-29 Phase 7.3):
    # Surface columns that ODI projects into the final INSERT but DRD
    # has no rule for.  Set difference between final_insert_columns and
    # DRD target columns.  Each appended as a synthetic ComparisonResult
    # so the GUI summary + grid + filter dropdown all pick it up.
    drd_cols_upper = {
        (r.get("physical_name") or r.get("column") or "").strip().upper()
        for r in aug
    }
    drd_cols_upper.discard("")
    # Snapshot existing target_col set BEFORE the loop so we don't end up
    # scanning the list as it grows (Phase 7.3 review finding).
    existing_targets = {res.target_col for res in cmp_results}
    for odi_col in (model.final_insert_columns or []):
        c = (odi_col or "").strip().upper()
        if not c or c in drd_cols_upper or c in existing_targets:
            continue
        existing_targets.add(c)
        cmp_results.append(ComparisonResult(
            verdict=ComparisonVerdict.ODI_EXTRA,
            target_col=c,
            drd_schema="", drd_table="", drd_attr="",
            odi_schema=model.target.schema, odi_table=model.target.table,
            odi_col=c, odi_expr_sql="", odi_step=99,
            explanation=(
                f"ODI_EXTRA: ODI projects '{c}' into the final INSERT but DRD "
                "has no rule for it.  Decide: (a) add DRD rule, (b) remove "
                "from ODI, or (c) accept as known extra."
            ),
            mismatch_kind=MismatchKind.NONE,
            drd_logic="", odi_logic="",
        ))
    cmp_summary = comparison_summary(cmp_results)

    # 7) DRD-first emitter
    from app.sql_model.drd_first_emitter import emit_insert_drd_first
    from app.sql_model.oracle_validator import validate_oracle_sql
    gen = emit_insert_drd_first(
        target_schema=target_schema, target_table=target_table,
        target_definition=tdef, analysis_rows=aug,
        odi_model=model, comparison_results=cmp_results,
        all_etl_notes_text=all_etl_text,
        out_of_scope_targets=out_of_scope_targets,
        # NOTE: no etl_column_defaults override -- both paths use generic
        # DEFAULT_ETL_COLUMN_VALUES so the output is byte-identical regardless
        # of caller (operator-locked 2026-05-29).
    )
    val = validate_oracle_sql(gen.sql, run_live=False)

    return V9Result(
        insert_sql=gen.sql,
        provenance=gen.provenance_summary,
        column_count=gen.column_count,
        join_count=gen.join_count,
        cte_count=gen.cte_count,
        oracle_validation=val.to_dict(),
        comparison_summary=cmp_summary,
        comparison_rows=[r.to_dict() for r in cmp_results],
        drd_row_count=len(drd_rows),
        drd_parse_errors=drd_errors,
        augmented_drd_rows=aug,
    )
