"""Role-based dimension mapping service.

Handles inference of role-based dimension key mappings using:
- Token normalization and abbreviation expansion
- PDM metadata validation
- Saved training rules
- Confidence scoring

Example: EOD_OFST_AR_DIM_ID → OFST_AR_DIM_ID (role match via shared tokens)
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from app.config import DATA_DIR

_RULES_PATH = os.path.join(DATA_DIR, "dimension_role_rules.json")


def load_dimension_role_rules() -> Dict[str, Any]:
    """Load dimension role rules from config file."""
    if os.path.exists(_RULES_PATH):
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"dimension_role_rules": [], "alias_categories": {}}


def parse_dimension_role(column_name: str, rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Decompose a column name into semantic parts.

    Returns:
        raw: original column name
        is_dimension_key: whether it ends with _DIM_ID
        role_tokens: tokens before DIM_ID suffix
        suffix: DIM_ID or similar
        normalized_tokens: expanded abbreviation tokens
        prefix_tokens: known ignore prefixes found
    """
    if rules is None:
        rules = load_dimension_role_rules()

    raw = (column_name or "").strip().upper()
    tokens = raw.split("_") if raw else []

    is_dim_key = raw.endswith("_DIM_ID")
    is_id_col = raw.endswith("_ID") and not is_dim_key

    if is_dim_key:
        role_tokens = tokens[:-2]  # everything before DIM_ID
        suffix = "DIM_ID"
    elif is_id_col:
        role_tokens = tokens[:-1]
        suffix = "ID"
    else:
        role_tokens = tokens
        suffix = ""

    # Identify known ignore prefixes
    ignore_prefixes = set()
    for rule in rules.get("dimension_role_rules", []):
        for pfx in rule.get("ignore_prefixes", []):
            ignore_prefixes.add(pfx.upper())

    prefix_tokens = []
    core_role_tokens = []
    prefix_done = False
    for tok in role_tokens:
        if not prefix_done and tok in ignore_prefixes:
            prefix_tokens.append(tok)
        else:
            prefix_done = True
            core_role_tokens.append(tok)

    # Expand abbreviations
    abbreviations = {}
    for rule in rules.get("dimension_role_rules", []):
        abbreviations.update({k.upper(): v.upper() for k, v in rule.get("abbreviations", {}).items()})

    normalized_tokens = [abbreviations.get(t, t) for t in tokens]

    return {
        "raw": raw,
        "is_dimension_key": is_dim_key,
        "is_id_column": is_id_col,
        "role_tokens": role_tokens,
        "core_role_tokens": core_role_tokens,
        "prefix_tokens": prefix_tokens,
        "suffix": suffix,
        "normalized_tokens": normalized_tokens,
    }


def compute_role_match_score(
    target_column: str,
    candidate_source: str,
    pdm_validated: bool = False,
    has_saved_rule: bool = False,
    rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute confidence score for a role-based mapping.

    Scoring:
        target suffix match:           20%
        dimension family match:         30%
        role token overlap:             30%
        PDM table/attribute validation: 15%
        saved training rule:             5%
    """
    if rules is None:
        rules = load_dimension_role_rules()

    target_parsed = parse_dimension_role(target_column, rules)
    source_parsed = parse_dimension_role(candidate_source, rules)

    # Suffix match (20%)
    suffix_match = target_parsed["suffix"] == source_parsed["suffix"]
    suffix_score = 1.0 if suffix_match else 0.0

    # Dimension family match (30%) — core role tokens minus prefixes
    target_core = set(target_parsed["core_role_tokens"])
    source_core = set(source_parsed["core_role_tokens"])

    if target_core and source_core:
        family_overlap = len(target_core & source_core) / max(len(target_core), len(source_core))
    elif not target_core and not source_core:
        family_overlap = 1.0
    else:
        family_overlap = 0.0

    # Identify dimension family
    dim_family = ""
    if target_core:
        # Use last meaningful token as family
        for tok in reversed(target_parsed["core_role_tokens"]):
            if tok not in ("DIM", "ID"):
                dim_family = tok
                break

    # Role token overlap (30%) — all role tokens
    target_role_set = set(target_parsed["role_tokens"])
    source_role_set = set(source_parsed["role_tokens"])
    # Remove known prefixes from source for overlap calc
    source_role_clean = source_role_set - set(source_parsed["prefix_tokens"])

    if target_role_set and source_role_clean:
        role_overlap = len(target_role_set & source_role_clean) / max(len(target_role_set), len(source_role_clean))
    elif not target_role_set and not source_role_clean:
        role_overlap = 1.0
    else:
        role_overlap = 0.0

    # PDM validation (15%)
    pdm_score = 1.0 if pdm_validated else 0.0

    # Saved rule (5%)
    rule_score = 1.0 if has_saved_rule else 0.0

    # Weighted total
    confidence = (
        suffix_score * 0.20
        + family_overlap * 0.30
        + role_overlap * 0.30
        + pdm_score * 0.15
        + rule_score * 0.05
    )

    # Determine status
    auto_accept = 0.88
    review_threshold = 0.72
    for rule in rules.get("dimension_role_rules", []):
        auto_accept = rule.get("auto_accept_threshold", 0.88)
        review_threshold = rule.get("review_threshold", 0.72)
        break

    if confidence >= auto_accept:
        status = "ROLE_MAPPING_AUTO_ACCEPT"
    elif confidence >= review_threshold:
        status = "ROLE_MAPPING_REVIEW"
    else:
        status = "ROLE_MAPPING_LOW_CONFIDENCE"

    return {
        "target_column": target_column.upper(),
        "candidate_source_attribute": candidate_source.upper(),
        "target_suffix_match": suffix_match,
        "dimension_family": dim_family,
        "dimension_family_match": family_overlap,
        "role_token_overlap": role_overlap,
        "pdm_validated": pdm_validated,
        "has_saved_rule": has_saved_rule,
        "confidence": round(confidence, 4),
        "status": status,
    }


def try_role_based_match(
    target_column: str,
    drd_source_attribute: str,
    rules: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Quick check: does the source attribute map to target via role-based logic?

    Returns match result if confidence >= review_threshold, else None.
    """
    if rules is None:
        rules = load_dimension_role_rules()

    target_upper = target_column.upper()
    source_upper = drd_source_attribute.upper()

    # Quick exit: if they're identical, it's exact match not role-based
    if target_upper == source_upper:
        return None

    # Check if source has a known prefix that, when stripped, yields target
    for rule in rules.get("dimension_role_rules", []):
        for prefix in rule.get("ignore_prefixes", []):
            stripped = re.sub(rf"^{prefix}_", "", source_upper)
            if stripped == target_upper:
                return {
                    "match_type": "MATCH_BY_ROLE_BASED_DIMENSION_KEY",
                    "target_column": target_upper,
                    "source_attribute": source_upper,
                    "stripped_prefix": prefix,
                    "confidence": 0.95,
                    "status": "ROLE_MAPPING_AUTO_ACCEPT",
                }

    # Full scoring
    result = compute_role_match_score(target_upper, source_upper, rules=rules)
    if result["confidence"] >= (rules.get("dimension_role_rules", [{}])[0].get("review_threshold", 0.72) if rules.get("dimension_role_rules") else 0.72):
        result["match_type"] = "MATCH_BY_ROLE_BASED_DIMENSION_KEY"
        return result

    return None
