"""Stage alias normalizer service.

Detects stage/temporary table qualifiers in SQL expressions and normalizes
them to canonical output columns for comparison purposes.

Stage qualifiers (STG, STEP, RT, I$, C$, TMP, TEMP) are intermediate carriers
and should be stripped for final-target comparison.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# Tokens that indicate a qualifier is a stage/temporary reference
STAGE_TOKENS = {"STG", "STEP", "TMP", "TEMP", "RT"}
STAGE_PREFIXES = {"I$", "C$", "J$", "S$"}

# Alias categories for context-aware normalization
ALIAS_CATEGORIES = {
    "source": [],  # populated dynamically from DRD source tables
    "lookup": ["LK", "DIM", "REF", "MAP"],
    "stage": ["STG", "STEP", "RT", "TMP", "TEMP"],
    "target": ["T", "TARGET", "TGT"],
}


def is_stage_qualifier(qualifier: str) -> bool:
    """Check if a qualifier references a stage/temporary table."""
    if not qualifier:
        return False
    upper = qualifier.upper()
    # Check for stage prefix patterns (I$TABLE, C$TABLE, etc.)
    for prefix in STAGE_PREFIXES:
        if upper.startswith(prefix):
            return True
    # Check for stage tokens anywhere in the qualifier
    tokens = re.split(r"[_.]", upper)
    return bool(set(tokens) & STAGE_TOKENS)


def classify_qualifier(qualifier: str, source_tables: Optional[List[str]] = None) -> str:
    """Classify a qualifier into source/lookup/stage/target/unknown."""
    if not qualifier:
        return "unknown"
    upper = qualifier.upper()

    if is_stage_qualifier(upper):
        return "stage"

    # Check against known source tables
    if source_tables:
        for st in source_tables:
            if upper == st.upper() or upper.endswith(f".{st.upper()}"):
                return "source"

    # Check lookup tokens
    tokens = re.split(r"[_.]", upper)
    for tok in ALIAS_CATEGORIES["lookup"]:
        if tok in tokens:
            return "lookup"

    # Check target tokens
    for tok in ALIAS_CATEGORIES["target"]:
        if upper == tok:
            return "target"

    return "unknown"


def normalize_stage_column(expr: str, source_tables: Optional[List[str]] = None) -> Dict[str, str]:
    """Normalize a SQL expression that may reference a stage table.

    Returns a dict with:
        raw: original expression
        qualifier: table/alias part (before last dot)
        column: column part (after last dot)
        is_stage_reference: whether the qualifier is a stage table
        qualifier_category: source/lookup/stage/target/unknown
        canonical_output_column: the column to use for comparison
    """
    raw = (expr or "").strip().upper()
    if not raw:
        return {
            "raw": "",
            "qualifier": "",
            "column": "",
            "is_stage_reference": False,
            "qualifier_category": "unknown",
            "canonical_output_column": "",
        }

    # Handle table_or_alias.column patterns
    if "." in raw:
        # Split on last dot to handle schema.table.col
        qualifier, column = raw.rsplit(".", 1)
    else:
        qualifier, column = "", raw

    is_stage = is_stage_qualifier(qualifier)
    category = classify_qualifier(qualifier, source_tables)

    # For stage references, the canonical output is just the column name
    # For source/lookup references, preserve the full expression for lineage
    if is_stage:
        canonical = column
    elif category == "target":
        canonical = column
    else:
        canonical = column  # For final comparison, always use output column

    return {
        "raw": raw,
        "qualifier": qualifier,
        "column": column,
        "is_stage_reference": is_stage,
        "qualifier_category": category,
        "canonical_output_column": canonical,
    }


def normalize_expression_for_comparison(expr: str, level: int = 3) -> str:
    """Normalize expression at different levels.

    Level 1: raw expression (no change)
    Level 2: alias-normalized expression (expand abbreviations)
    Level 3: output projection (strip qualifier, return column only)
    Level 4: semantic role (expand to full semantic meaning)
    """
    raw = (expr or "").strip().upper()
    if not raw:
        return ""

    if level == 1:
        return raw

    if "." in raw:
        qualifier, column = raw.rsplit(".", 1)
    else:
        qualifier, column = "", raw

    if level == 2:
        # Keep qualifier but normalize
        return raw

    if level == 3:
        # Output projection — just the column
        return column

    if level == 4:
        # Semantic role — handled by role_based_dimension_mapping_service
        return column

    return raw


def batch_normalize(expressions: List[str], source_tables: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Normalize a batch of expressions."""
    return [normalize_stage_column(expr, source_tables) for expr in expressions]
