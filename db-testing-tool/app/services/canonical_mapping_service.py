"""Canonical mapping service.

Builds a unified canonical mapping object for each target column that aligns:
- DRD source attribute
- Generated SQL expression + output alias
- XML/manual expression + output alias
- PDM source table/attribute
- Match status (resolved via lineage + role mapping + saved rules)

This is the single source of truth for comparison.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.services.stage_alias_normalizer_service import (
    normalize_stage_column,
    normalize_expression_for_comparison,
)
from app.services.sql_projection_parser_service import (
    build_projection_map,
    parse_select_projections,
)
from app.services.role_based_dimension_mapping_service import (
    try_role_based_match,
    load_dimension_role_rules,
)


# Match status constants (ordered by confidence, highest first)
MATCH_STATUSES = [
    "EXACT_EXPRESSION_MATCH",
    "MATCH_BY_OUTPUT_ALIAS",
    "MATCH_BY_STAGE_PROJECTION",
    "MATCH_BY_ROOT_SOURCE_LINEAGE",
    "MATCH_BY_ROLE_BASED_DIMENSION_KEY",
    "MATCH_BY_DRD_SOURCE_ATTRIBUTE",
    "MATCH_BY_PDM_PREDICTION",
    "MATCH_BY_SAVED_RULE",
    "REVIEW_REQUIRED_LOW_CONFIDENCE",
    "REAL_MISMATCH",
]


def build_canonical_mapping(
    target_column: str,
    drd_source_attribute: str,
    generated_expression: str,
    manual_or_xml_expression: str,
    pdm_source_table: str = "",
    pdm_source_attribute: str = "",
    saved_rules: Optional[List[Dict[str, Any]]] = None,
    source_tables: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a canonical mapping object for a single target column.

    Applies multi-level matching logic:
    1. Exact expression match
    2. Output alias match (generated alias == XML/manual column)
    3. Stage projection match (strip stage qualifier from XML/manual)
    4. Root source lineage match
    5. Role-based dimension key match
    6. DRD source attribute in generated expression
    7. PDM prediction
    8. Saved training rule
    """
    target_col = target_column.upper().strip()
    drd_src = (drd_source_attribute or "").upper().strip()
    gen_expr = (generated_expression or "").upper().strip()
    manual_expr = (manual_or_xml_expression or "").upper().strip()

    # Normalize generated expression
    gen_normalized = normalize_stage_column(gen_expr, source_tables)
    gen_output_alias = gen_normalized["canonical_output_column"]

    # Normalize manual/XML expression
    manual_normalized = normalize_stage_column(manual_expr, source_tables)
    manual_output_alias = manual_normalized["canonical_output_column"]

    # Determine match status using cascading rules
    match_status = _resolve_match_status(
        target_col=target_col,
        drd_src=drd_src,
        gen_expr=gen_expr,
        gen_output_alias=gen_output_alias,
        manual_expr=manual_expr,
        manual_output_alias=manual_output_alias,
        pdm_source_table=pdm_source_table,
        pdm_source_attribute=pdm_source_attribute,
        saved_rules=saved_rules,
    )

    return {
        "target_column": target_col,
        "drd_source_attribute": drd_src,
        "generated_expression": gen_expr,
        "generated_output_alias": gen_output_alias,
        "xml_expression": manual_expr,
        "xml_output_alias": manual_output_alias,
        "pdm_source_table": (pdm_source_table or "").upper().strip(),
        "pdm_source_attribute": (pdm_source_attribute or "").upper().strip(),
        "canonical_target": target_col,
        "canonical_source_attribute": drd_src,
        "match_status": match_status,
    }


def build_canonical_mappings_from_comparison(
    analysis_rows: List[Dict[str, Any]],
    generated_sql: str = "",
    manual_sql: str = "",
    saved_rules: Optional[List[Dict[str, Any]]] = None,
    source_tables: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Build canonical mappings for all rows from a CT comparison.

    Uses the generated and manual SQL to extract projection maps,
    then builds canonical mapping for each target column.
    """
    # Build projection maps from SQL
    gen_map = build_projection_map(generated_sql) if generated_sql else {}
    manual_map = build_projection_map(manual_sql) if manual_sql else {}

    results = []
    for row in analysis_rows:
        col = (row.get("column") or row.get("physical_name") or "").upper().strip()
        if not col:
            continue

        drd_src = (row.get("source_attribute") or row.get("drd_expression") or "").upper().strip()
        gen_expr = gen_map.get(col, row.get("generated_expression", ""))
        manual_expr = manual_map.get(col, row.get("manual_expression", ""))

        canonical = build_canonical_mapping(
            target_column=col,
            drd_source_attribute=drd_src,
            generated_expression=gen_expr,
            manual_or_xml_expression=manual_expr,
            pdm_source_table=row.get("source_table", ""),
            pdm_source_attribute=drd_src,
            saved_rules=saved_rules,
            source_tables=source_tables,
        )
        results.append(canonical)

    return results


def _resolve_match_status(
    target_col: str,
    drd_src: str,
    gen_expr: str,
    gen_output_alias: str,
    manual_expr: str,
    manual_output_alias: str,
    pdm_source_table: str = "",
    pdm_source_attribute: str = "",
    saved_rules: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Resolve match status using cascading rules."""

    # If no manual/XML expression, can't compare
    if not manual_expr:
        if gen_output_alias == target_col:
            return "MATCH_BY_OUTPUT_ALIAS"
        if gen_expr:
            return "MATCH_BY_DRD_SOURCE_ATTRIBUTE"
        return "REVIEW_REQUIRED_LOW_CONFIDENCE"

    # If no generated expression
    if not gen_expr:
        return "REVIEW_REQUIRED_LOW_CONFIDENCE"

    # ── Rule 1: Exact expression match ──
    if _normalize_for_exact(gen_expr) == _normalize_for_exact(manual_expr):
        return "EXACT_EXPRESSION_MATCH"

    # ── Rule 2: Output alias match ──
    # Generated output alias == manual/XML output column
    if gen_output_alias and manual_output_alias and gen_output_alias == manual_output_alias:
        return "MATCH_BY_OUTPUT_ALIAS"

    # ── Rule 3: Stage projection match ──
    # Generated output alias == target column AND manual output alias == target column
    if gen_output_alias == target_col and manual_output_alias == target_col:
        return "MATCH_BY_STAGE_PROJECTION"

    # Generated output alias matches manual column (stage stripped)
    if gen_output_alias and gen_output_alias == manual_output_alias:
        return "MATCH_BY_STAGE_PROJECTION"

    # ── Rule 4: Root source lineage ──
    # DRD source attribute appears in generated expression
    if drd_src and drd_src in gen_expr:
        # And manual resolves to same target
        if manual_output_alias == target_col:
            return "MATCH_BY_ROOT_SOURCE_LINEAGE"

    # ── Rule 5: Role-based dimension key match ──
    rules = load_dimension_role_rules()
    if drd_src and target_col:
        role_match = try_role_based_match(target_col, drd_src, rules)
        if role_match and role_match.get("confidence", 0) >= 0.72:
            # Verify the manual/XML also targets the same column
            if manual_output_alias == target_col or not manual_expr:
                return "MATCH_BY_ROLE_BASED_DIMENSION_KEY"

    # ── Rule 6: DRD source attribute in generated ──
    if drd_src and drd_src in gen_expr:
        return "MATCH_BY_DRD_SOURCE_ATTRIBUTE"

    # ── Rule 7: PDM prediction ──
    if pdm_source_attribute and pdm_source_attribute in gen_expr:
        return "MATCH_BY_PDM_PREDICTION"

    # ── Rule 8: Saved training rule ──
    if saved_rules:
        for rule in saved_rules:
            if rule.get("target_column", "").upper() == target_col:
                if rule.get("decision") == "equivalent":
                    return "MATCH_BY_SAVED_RULE"

    # ── Default: Determine if it's really a mismatch ──
    # One more check: if both resolve to the same target column
    if gen_output_alias == target_col and manual_output_alias == target_col:
        return "MATCH_BY_STAGE_PROJECTION"

    # Check if source in generated matches source in manual (different paths to same data)
    gen_source_col = _extract_source_column(gen_expr)
    manual_source_col = _extract_source_column(manual_expr)
    if gen_source_col and manual_source_col:
        # If they reference the same source column, it's a lineage match
        if gen_source_col == manual_source_col:
            return "MATCH_BY_ROOT_SOURCE_LINEAGE"

    return "REAL_MISMATCH"


def _normalize_for_exact(expr: str) -> str:
    """Normalize for exact comparison — strip whitespace, qualifiers."""
    text = (expr or "").upper().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _extract_source_column(expr: str) -> str:
    """Extract the base column name from an expression (strip qualifiers)."""
    text = (expr or "").upper().strip()
    if "." in text:
        return text.rsplit(".", 1)[1]
    return text
