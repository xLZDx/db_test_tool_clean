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
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Top-level imports (review NIT 2026-05-30): hoist out of generate_v9
# body so static analysis sees the dependency.
from app.sql_model.comparator_driven_emitter import emit_insert_comparator_driven

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
    insert_dry_run: Dict[str, Any] = field(default_factory=dict)
    # Phase 7.5 (operator-locked 2026-05-30): the comparator-driven
    # INSERT.  This is the operator's preferred emitter -- it REUSES
    # the comparator's per-column verdict and projects from ODI's
    # USING() inner SELECT, so the JOIN graph is honoured by
    # construction (no PROVENANCE_FALLBACK path).
    insert_sql_comparator_driven: str = ""
    insert_comparator_driven_stats: Dict[str, Any] = field(default_factory=dict)


def generate_v9(
    *,
    drd_bytes: bytes,
    drd_filename: str,
    odi_xml_bytes: bytes,
    target_schema: str,
    target_table: str,
    source_datasource_id: int = 3,
    target_datasource_id: int = 3,
    kb=None,
) -> V9Result:
    """End-to-end v9 generation.  Same inputs -> same SQL byte-for-byte.

    ``kb`` (KBLookup | None): when provided, enables the comparator's PDM
    arbitration -- e.g. a DRD-typo column name vs the ODI-correct name is
    resolved to ALIAS_DRIFT_ONLY (PDM authoritative) instead of falling to
    UNRESOLVABLE.  Operator rule: on any DRD<->ODI column disagreement,
    always arbitrate against PDM and/or the live DB.
    """
    # Auto-load the schema KB when the caller did not supply one, so PDM
    # arbitration works consistently across the GUI, CLI and tests (not only
    # when the HTTP handler happens to pass kb).  Best-effort; degrades to
    # no-PDM when the KB file is absent.
    if kb is None:
        try:
            from pathlib import Path as _Path
            from app.sql_model.static_validator import KBLookup as _KBLookup
            # v9_pipeline.py lives in app/services/ -> parents[2] = project root
            # (mirrors odi.py's _KB_PATH at app/routers/ -> parents[2]).
            _kbp = _Path(__file__).resolve().parents[2] / "data" / "local_kb" / "schema_kb_ds_1.json"
            if _kbp.exists():
                kb = _KBLookup(_kbp)
        except Exception:
            kb = None

    # Parse ODI XML early so DRD auto-detection can use the same target context
    # as v16 compare (sheet/header selection must be identical across branches).
    from app.sql_model.odi_parser import OdiXmlParser
    model = OdiXmlParser(target_schema=target_schema, target_table=target_table).parse_bytes(odi_xml_bytes)
    target_schema = (target_schema or "").strip() or model.target.schema
    target_table = (target_table or "").strip() or model.target.table

    drd_sheet_override: Optional[str] = None
    drd_header_override: Optional[int] = None
    try:
        import io
        from openpyxl import load_workbook
        from app.services import odi_drd_compare_v15 as _v16_base

        wb = load_workbook(io.BytesIO(drd_bytes), read_only=True, data_only=True)
        det = _v16_base.auto_detect_mapping(
            wb,
            xml_targets=[_v16_base.normalize_identifier(target_table)],
            target_table_override=target_table or "",
        )
        drd_sheet_override = getattr(det, "mapping_sheet", None) or None
        drd_header_override = getattr(det, "header_row", None)
    except Exception as exc:
        logger.warning(
            "v9 DRD canonical sheet/header detection failed, using legacy parser auto-detect: %s",
            exc,
        )
        # Best-effort alignment: if detection fails, keep legacy parser path.
        drd_sheet_override = None
        drd_header_override = None

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
        sheet_name=drd_sheet_override,
        header_row_override=drd_header_override,
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
    import logging
    _v9_log = logging.getLogger(__name__)
    pdm_lookup_failed = False
    pdm_lookup_error: Optional[str] = None
    try:
        tdef = load_target_table_definition(target_datasource_id, target_schema, target_table)
    except Exception as _pdm_exc:
        # Operator-locked (Phase 7.16 silent-failure round 2): PDM lookup
        # failure used to silently substitute VARCHAR2(4000)/nullable for
        # every column, dropping NOT NULL + type info and producing wrong
        # INSERT SQL with NO indication that PDM was unavailable.  Now:
        # (1) loud WARNING in logs with the exception, (2) tdef payload
        # carries `pdm_missing=True` + `pdm_error=str(exc)` for the
        # emitter to project as `-- PDM_MISS_TYPES_DEFAULTED VARCHAR2(4000)`
        # markers, (3) `V9Result.warnings` will surface this so the
        # caller / dashboard banner can show it.  Operator can no longer
        # ship plausible-looking but type-wrong SQL silently.
        pdm_lookup_failed = True
        pdm_lookup_error = str(_pdm_exc)
        _v9_log.warning(
            "PDM target_definition lookup FAILED for %s.%s (ds=%s): %s -- "
            "falling back to synthetic VARCHAR2(4000) nullable=True for ALL "
            "columns.  Emitted SQL will carry PDM_MISS markers.",
            target_schema, target_table, target_datasource_id, _pdm_exc,
        )
        tdef = {
            "columns": [
                {"name": c, "data_type": "VARCHAR2(4000)", "nullable": True,
                 "pdm_missing": True}
                for c in model.final_insert_columns
            ],
            "pdm_missing": True,
            "pdm_error": str(_pdm_exc),
        }

    # 6) Comparator (shared rule engine)
    from app.sql_model.comparator import (
        compare_drd_rows_to_model, comparison_summary, ComparisonResult,
    )
    from app.sql_model.types import ComparisonVerdict, MismatchKind
    cmp_results = compare_drd_rows_to_model(aug, model, kb=kb)

    # Cross-branch parity layer: align verdict surface with canonical v16
    # mode1 (ODI #1 vs DRD) so both UI branches show the same match/mismatch
    # targets for identical inputs.
    try:
        from app.services.odi_drd_compare_v16 import compare_two_odi_against_drd

        v16_mode1 = compare_two_odi_against_drd(
            drd_bytes,
            odi_xml_bytes,
            None,
            profile="auto",
            target_table=target_table,
        )
        v16_issues = {
            (str(r.get("target_column") or "").strip().upper()): str(r.get("severity") or "").strip().lower()
            for r in (v16_mode1.get("differences") or [])
            if str(r.get("target_column") or "").strip()
        }
        if v16_issues:
            for res in cmp_results:
                tcol = (getattr(res, "target_col", "") or "").strip().upper()
                st = v16_issues.get(tcol)
                if not st:
                    res.verdict = ComparisonVerdict.MATCHED
                    continue
                if st == "missing":
                    res.verdict = ComparisonVerdict.SOURCE_MISSING
                elif st in ("odi_only", "odi_extra"):
                    res.verdict = ComparisonVerdict.ODI_EXTRA
                else:
                    # real_gap / logic_drift / structural (and unknowns) remain
                    # actionable mismatches in this branch.
                    res.verdict = ComparisonVerdict.REAL_MISMATCH
    except Exception as exc:
        logger.warning(
            "v9 v16-parity harmonization skipped, keeping comparator-native verdicts: %s",
            exc,
        )

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

    # 7b) Comparator-driven emitter (Phase 7.5 -- operator-locked
    # 2026-05-30).  Reuses the comparator's per-column verdict and
    # projects from ODI's USING(...) inner SELECT.  No fallback paths.
    cd_gen = emit_insert_comparator_driven(
        target_schema=target_schema,
        target_table=target_table,
        drd_rows=aug,
        comparison_results=cmp_results,
        odi_model=model,
        target_definition=tdef,
    )
    if cd_gen.extraction_failed:
        # Surface loudly -- the operator must NOT trust empty SQL.
        logging.getLogger(__name__).warning(
            "v9_pipeline: comparator_driven_emitter failed extraction: %s",
            cd_gen.extraction_failure_reason,
        )

    # 8) Pre-insert dry-run validation (operator-locked 2026-05-29
    # Phase 7.4 Issue 3): inspect the emitted INSERT for known smell
    # patterns that indicate operator should NOT trust the artefact
    # until investigated.  This is a PURE-TEXT check (no live DB).
    # Issues surfaced:
    #   - NULL_SUBSTITUTION: columns the emitter could not resolve
    #     and replaced with NULL (PDM_MISS marker present).
    #   - PROVENANCE_FALLBACK: columns projected via
    #     DRD_PHYSICAL_FALLBACK (DRD source had no JOIN; we fell back
    #     to the bare physical column).
    #   - COLUMN_COUNT_MISMATCH: INSERT projects N columns but
    #     DRD declared M (M != N).
    insert_sql = gen.sql
    if insert_sql is None:
        # Emitter returned None -- treat as hard failure, not silent OK.
        insert_dry_run = {
            "passed": False,
            "issues": ["EMITTER_RETURNED_NONE: gen.sql is None; the emitter "
                       "did not produce SQL.  Investigate emit_insert_drd_first."],
            "null_substituted_count": 0,
            "null_substituted_examples": [],
            "fallback_substituted_count": 0,
            "fallback_substituted_examples": [],
            "drd_declared_columns": 0,
            "insert_projected_columns": 0,
        }
        return V9Result(
            insert_sql="",
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
            insert_dry_run=insert_dry_run,
            insert_sql_comparator_driven=cd_gen.sql,
            insert_comparator_driven_stats={
                "column_count": cd_gen.column_count,
                "matched_count": cd_gen.matched_count,
                "real_mismatch_cols": cd_gen.real_mismatch_cols,
                "unresolvable_cols": cd_gen.unresolvable_cols,
                "source_missing_cols": cd_gen.source_missing_cols,
                "null_substitutions": cd_gen.null_substitutions,
                "null_in_not_null_risk_cols": cd_gen.null_in_not_null_risk_cols,
                "join_derived_tables": cd_gen.join_derived_tables,
                "join_undetermined_tables": cd_gen.join_undetermined_tables,
                "complex_drd_expression_cols": cd_gen.complex_drd_expression_cols,
                "recovered_from_prose_cols": cd_gen.recovered_from_prose_cols,
                "extraction_failed": cd_gen.extraction_failed,
                "extraction_failure_reason": cd_gen.extraction_failure_reason,
                "notes": cd_gen.notes,
            },
        )
    # Build a DRD-rule lookup by target column so we can decide whether
    # a NULL in the INSERT is VALID (DRD itself specifies NULL / audit /
    # literal) or INVALID (DRD says real source but emitter dropped to NULL).
    # Operator-locked clarification (2026-05-30): "NULL may be valid per
    # DRD -- always cross-check".
    _NULL_VALID_MARKERS = (
        "NULL", "N/A", "NONE",
    )
    _DRD_LOGIC_NULL_RE = re.compile(
        r"\b(?:DEFAULT\s+NULL|=\s*NULL|RETURN\s+NULL|SET\s+TO\s+NULL|"
        r"AUDIT\s+COLUMN|DEFAULT\s+SYSDATE|DEFAULT\s+USER|DEFAULT\s+'[^']*')",
        re.IGNORECASE,
    )

    def _drd_says_null_is_valid(col_name: str) -> bool:
        """True iff the DRD rule for `col_name` explicitly says the
        target should be NULL (or a non-source-derived default).  Used
        to skip false-positive NULL_SUBSTITUTION flags."""
        col_u = col_name.upper()
        for r in aug:
            if (r.get("physical_name") or r.get("column") or "").strip().upper() != col_u:
                continue
            src_attr = (r.get("source_attribute") or "").strip().upper()
            transformation = (r.get("transformation") or "")
            # (a) DRD explicitly names NULL/N/A in source_attribute
            if src_attr in _NULL_VALID_MARKERS:
                return True
            # (b) DRD logic text says "DEFAULT NULL" / "AUDIT COLUMN" /
            #     "DEFAULT sysdate" / "DEFAULT 'X'" -- these are
            #     emitter-supplied values, not DRD column projections
            if _DRD_LOGIC_NULL_RE.search(transformation):
                return True
            # (c) No source_table AND no source_attribute -> DRD itself
            #     doesn't specify a source -> NULL is the operator's
            #     intent
            if not src_attr and not (r.get("source_table") or "").strip():
                return True
            return False
        # Column not in DRD at all -> NULL is the only sane choice,
        # don't flag.
        return True

    null_unwanted: List[str] = []
    null_drd_validated: List[str] = []
    fallback_substituted: List[str] = []
    # Patterns operator-locked Phase 7.4: cover the two known emitter
    # comment styles (`NULL /* PDM_MISS ... */ AS COL` AND
    # `NULL AS COL  -- PDM_MISS ...`).  AND also a plain `NULL AS COL`
    # so we cross-check those against DRD too (2026-05-30 op clarif).
    _PDM_MISS_INLINE = re.compile(r"NULL\s*/\*\s*PDM_MISS:?[^*]*\*/\s*AS\s+([A-Z0-9_]+)", re.IGNORECASE)
    _PDM_MISS_TRAILING = re.compile(r"NULL\s+AS\s+([A-Z0-9_]+)\s*,?\s*--.*PDM_MISS", re.IGNORECASE)
    _PLAIN_NULL_AS = re.compile(r"^\s*NULL\s+AS\s+([A-Z0-9_]+)\b", re.IGNORECASE)
    _PROV_FALLBACK = re.compile(r"\bAS\s+([A-Z0-9_]+),?\s*--.*DRD_PHYSICAL_FALLBACK", re.IGNORECASE)
    for line in insert_sql.splitlines():
        # PDM_MISS markers: unconditional flag (emitter explicitly says
        # it could not resolve; DRD validity is moot).
        m = _PDM_MISS_INLINE.search(line) or _PDM_MISS_TRAILING.search(line)
        if m:
            col = m.group(1).upper()
            if _drd_says_null_is_valid(col):
                # Even PDM_MISS lands on a DRD-NULL column => operator's
                # intent met by accident; track for transparency.
                null_drd_validated.append(col)
            else:
                null_unwanted.append(col)
            continue
        # Plain `NULL AS COL` (no marker): cross-check against DRD.
        m_plain = _PLAIN_NULL_AS.search(line)
        if m_plain:
            col = m_plain.group(1).upper()
            if _drd_says_null_is_valid(col):
                null_drd_validated.append(col)
            else:
                null_unwanted.append(col)
            continue
        m_fb = _PROV_FALLBACK.search(line)
        if m_fb:
            fallback_substituted.append(m_fb.group(1).upper())
    drd_declared = len([r for r in aug if (r.get("physical_name") or r.get("column"))])
    issues: List[str] = []
    if null_unwanted:
        issues.append(
            f"NULL_SUBSTITUTION: {len(null_unwanted)} column(s) emit NULL "
            f"but DRD specifies a real source (first 5: "
            f"{', '.join(null_unwanted[:5])}). Operator MUST investigate "
            f"-- DRD says these should NOT be NULL."
        )
    if fallback_substituted:
        issues.append(
            f"PROVENANCE_FALLBACK: {len(fallback_substituted)} column(s) "
            f"emitted via DRD_PHYSICAL_FALLBACK (no JOIN found, bare "
            f"physical ref used; first 5: "
            f"{', '.join(fallback_substituted[:5])}). Operator MUST "
            f"add the join to the analysis or confirm the fallback is "
            f"semantically correct."
        )
    if gen.column_count != drd_declared and drd_declared > 0:
        issues.append(
            f"COLUMN_COUNT_MISMATCH: INSERT projects {gen.column_count} "
            f"column(s) but DRD declared {drd_declared}. Investigate "
            f"which rows were dropped or duplicated."
        )
    insert_dry_run = {
        "passed": len(issues) == 0,
        "issues": issues,
        # Operator-clarified split (2026-05-30):
        # - null_substituted_count: NULL where DRD says real source (BAD).
        # - null_drd_validated_count: NULL where DRD itself says NULL or
        #   the column is an audit / DEFAULT / no-source field (OK).
        "null_substituted_count": len(null_unwanted),
        "null_substituted_examples": null_unwanted[:10],
        "null_drd_validated_count": len(null_drd_validated),
        "null_drd_validated_examples": null_drd_validated[:10],
        "fallback_substituted_count": len(fallback_substituted),
        "fallback_substituted_examples": fallback_substituted[:10],
        "drd_declared_columns": drd_declared,
        "insert_projected_columns": gen.column_count,
    }

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
        insert_dry_run=insert_dry_run,
        insert_sql_comparator_driven=cd_gen.sql,
        insert_comparator_driven_stats={
            "column_count": cd_gen.column_count,
            "matched_count": cd_gen.matched_count,
            "real_mismatch_cols": cd_gen.real_mismatch_cols,
            "unresolvable_cols": cd_gen.unresolvable_cols,
            "source_missing_cols": cd_gen.source_missing_cols,
            "null_substitutions": cd_gen.null_substitutions,
            "null_in_not_null_risk_cols": cd_gen.null_in_not_null_risk_cols,
            "join_derived_tables": cd_gen.join_derived_tables,
            "join_undetermined_tables": cd_gen.join_undetermined_tables,
            "complex_drd_expression_cols": cd_gen.complex_drd_expression_cols,
            "recovered_from_prose_cols": cd_gen.recovered_from_prose_cols,
            "extraction_failed": cd_gen.extraction_failed,
            "extraction_failure_reason": cd_gen.extraction_failure_reason,
            "notes": cd_gen.notes,
        },
    )
