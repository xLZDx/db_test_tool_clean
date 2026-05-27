"""SQL pattern validation service."""
from __future__ import annotations
import logging
import re
from typing import List, Tuple

logger = logging.getLogger(__name__)

# DDL/DML statements that must not appear in user-supplied SQL fragments.
_DANGEROUS_KEYWORDS = re.compile(
    r"\b(DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|EXECUTE|EXEC|DELETE|UPDATE|INSERT|MERGE|CALL)\b",
    re.IGNORECASE,
)

# Sequences that indicate SQL injection attempts.
_INJECTION_PATTERNS = re.compile(
    r"(--|/\*|\*/|;|\bxp_|\bsp_|\bUNION\b)",
    re.IGNORECASE,
)

# A valid Oracle column/expression reference — very permissive; presence of SELECT is enough.
_HAS_SELECT = re.compile(r"\bSELECT\b", re.IGNORECASE)


def _check_balanced_parens(sql: str) -> str | None:
    """Return error message if parentheses are unbalanced, else None."""
    depth = 0
    for ch in sql:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth < 0:
            return "unbalanced parentheses: unexpected ')'"
    if depth != 0:
        return f"unbalanced parentheses: {depth} unclosed '('"
    return None


def validate_test_definition_sql(sql: str) -> bool:
    """Validate SQL in a test definition.

    Returns True when the SQL looks safe and well-formed, False otherwise.
    """
    if not sql or not sql.strip():
        logger.debug("validate_test_definition_sql: empty SQL -> invalid")
        return False

    errors = validate_sql_pattern(sql)
    if errors:
        logger.debug("validate_test_definition_sql: %d error(s): %s", len(errors), errors)
        return False
    return True


def validate_sql_pattern(sql: str) -> list:
    """Validate a SQL fragment and return a list of error strings.

    Returns an empty list when the SQL is acceptable.
    Checks performed:
    - Non-empty
    - Contains SELECT keyword
    - No dangerous DDL/DML keywords
    - No SQL injection sequences (--, /*, ;, UNION)
    - Balanced parentheses
    """
    if not sql or not sql.strip():
        return ["SQL is empty"]

    errors: List[str] = []

    # Must reference a SELECT to be a valid test query.
    if not _HAS_SELECT.search(sql):
        errors.append("SQL does not contain a SELECT statement")

    # No dangerous mutations.
    m = _DANGEROUS_KEYWORDS.search(sql)
    if m:
        errors.append(f"SQL contains forbidden keyword: {m.group(0).upper()}")

    # No injection signatures.
    m2 = _INJECTION_PATTERNS.search(sql)
    if m2:
        errors.append(f"SQL contains potentially unsafe pattern: {m2.group(0)!r}")

    # Balanced parentheses.
    paren_err = _check_balanced_parens(sql)
    if paren_err:
        errors.append(paren_err)

    return errors


def split_valid_invalid_test_defs(
    test_defs: List[dict],
) -> Tuple[List[dict], List[dict]]:
    """Split test definitions into valid and invalid based on pattern_errors field.

    A definition is invalid if it contains a non-empty ``pattern_errors`` dict
    with at least one non-empty list under the 'source' or 'target' key.
    All others are considered valid.
    """
    valid: List[dict] = []
    invalid: List[dict] = []
    for td in test_defs:
        errors = td.get("pattern_errors") or {}
        if isinstance(errors, dict):
            src_errs = errors.get("source") or []
            tgt_errs = errors.get("target") or []
            if src_errs or tgt_errs:
                invalid.append(td)
                continue
        valid.append(td)
    return valid, invalid
