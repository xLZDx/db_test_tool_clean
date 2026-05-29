"""SQL projection parser service.

Parses SELECT list from SQL statements into structured projection objects.
Handles:
- Simple column references: COL_A
- Qualified references: TABLE.COL_A
- Aliased expressions: TABLE.COL_A AS OUTPUT_COL
- CASE expressions: CASE WHEN ... END AS OUTPUT_COL
- Function calls: NVL(TABLE.COL, 0) AS OUTPUT_COL
- Aggregates: SUM(TABLE.COL) AS OUTPUT_COL
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


def parse_select_projections(sql: str) -> List[Dict[str, str]]:
    """Parse a SQL SELECT statement into projection objects.

    Returns list of:
        {
            "expression": "AR_DIM.EOD_AR_DIM_ID",
            "output_alias": "AR_DIM_ID",
            "raw_text": "AR_DIM.EOD_AR_DIM_ID AS AR_DIM_ID"
        }
    """
    if not sql or not sql.strip():
        return []

    # Find SELECT ... FROM boundary
    select_body = _extract_select_body(sql)
    if not select_body:
        return []

    # Split into individual projection items
    items = _split_select_items(select_body)

    projections = []
    for item in items:
        parsed = _parse_single_projection(item)
        if parsed:
            projections.append(parsed)

    return projections


def parse_insert_column_list(sql: str) -> List[str]:
    """Extract column list from INSERT INTO ... (...) SELECT ..."""
    m = re.search(
        r"INSERT\s+INTO\s+[\w.$\"]+\s*\(([^)]+)\)",
        sql,
        flags=re.IGNORECASE,
    )
    if not m:
        return []
    cols_str = m.group(1)
    return [c.strip().upper() for c in cols_str.split(",") if c.strip()]


def build_projection_map(sql: str) -> Dict[str, str]:
    """Build a mapping from output_alias → expression for a SQL statement.

    For INSERT ... SELECT, pairs insert columns with select expressions.
    For plain SELECT, uses AS aliases or derives from expression.
    """
    upper_sql = (sql or "").strip().upper()

    # Try INSERT INTO ... (...) SELECT ...
    insert_cols = parse_insert_column_list(sql)
    if insert_cols:
        # Extract the SELECT part after the INSERT column list
        m = re.search(r"\)\s*SELECT\s+", sql, flags=re.IGNORECASE)
        if m:
            select_sql = sql[m.start():]
            select_sql = re.sub(r"^\)\s*", "", select_sql, flags=re.IGNORECASE)
            projections = parse_select_projections(select_sql)
            result = {}
            for i, col in enumerate(insert_cols):
                if i < len(projections):
                    result[col] = projections[i]["expression"]
                else:
                    result[col] = ""
            return result

    # Plain SELECT
    projections = parse_select_projections(sql)
    result = {}
    for proj in projections:
        alias = proj["output_alias"]
        if alias:
            result[alias] = proj["expression"]
    return result


def extract_projection_edges(sql: str) -> List[Dict[str, str]]:
    """Extract lineage edges from SQL projections.

    Each edge represents: source_expression → output_alias
    """
    projections = parse_select_projections(sql)
    edges = []
    for proj in projections:
        expr = proj["expression"]
        alias = proj["output_alias"]
        if not alias:
            continue

        # Decompose expression into qualifier and column
        if "." in expr and not _is_function_call(expr):
            qualifier, column = expr.rsplit(".", 1)
        else:
            qualifier, column = "", expr

        edges.append({
            "from_expression": expr,
            "from_column": column,
            "from_alias": qualifier,
            "to_column": alias,
            "projection_type": "SELECT_ALIAS",
        })

    return edges


# ── Internal helpers ──────────────────────────────────────────────────────────


def _extract_select_body(sql: str) -> str:
    """Extract the body between SELECT and FROM (or end of statement)."""
    upper = sql.upper()

    # Find SELECT keyword (skip WITH ... AS (...) SELECT patterns)
    # Find the main SELECT
    select_positions = [m.start() for m in re.finditer(r"\bSELECT\b", upper)]
    if not select_positions:
        return ""

    # Use the last SELECT before FROM for CTE patterns,
    # or the first SELECT for simple queries
    # Strategy: find SELECT that isn't inside a WITH clause subquery
    start_pos = select_positions[0]
    for pos in select_positions:
        # Check if there's a FROM after this SELECT at the same nesting level
        remaining = upper[pos + 6:]
        if _find_from_at_level_0(remaining) is not None:
            start_pos = pos
            break

    body_start = start_pos + 6  # len("SELECT")

    # Skip DISTINCT, ALL, hints
    remaining = sql[body_start:].lstrip()
    hint_match = re.match(r"/\*[^*]*\*/\s*", remaining)
    if hint_match:
        body_start += len(sql[body_start:]) - len(remaining) + hint_match.end()
        remaining = sql[body_start:].lstrip()

    distinct_match = re.match(r"(?:DISTINCT|ALL|UNIQUE)\s+", remaining, flags=re.IGNORECASE)
    if distinct_match:
        body_start += len(sql[body_start:]) - len(remaining) + distinct_match.end()

    # Find FROM at nesting level 0
    from_pos = _find_from_at_level_0(sql[body_start:])
    if from_pos is not None:
        return sql[body_start:body_start + from_pos].strip()

    # No FROM found — return everything after SELECT
    return sql[body_start:].strip()


def _find_from_at_level_0(text: str) -> Optional[int]:
    """Find position of FROM keyword at parenthesis nesting level 0."""
    depth = 0
    upper = text.upper()
    i = 0
    while i < len(upper):
        if upper[i] == '(':
            depth += 1
        elif upper[i] == ')':
            depth -= 1
        elif depth == 0 and upper[i:i+4] == 'FROM' and (i == 0 or not upper[i-1].isalnum()) and (i + 4 >= len(upper) or not upper[i+4].isalnum()):
            return i
        i += 1
    return None


def _split_select_items(body: str) -> List[str]:
    """Split SELECT body by commas at nesting level 0."""
    items = []
    current = []
    depth = 0
    in_string = False
    prev_char = ''

    for ch in body:
        if ch == "'" and prev_char != '\\':
            in_string = not in_string
        elif not in_string:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif ch == ',' and depth == 0:
                items.append("".join(current).strip())
                current = []
                prev_char = ch
                continue
        current.append(ch)
        prev_char = ch

    if current:
        items.append("".join(current).strip())

    return [item for item in items if item]


def _parse_single_projection(item: str) -> Optional[Dict[str, str]]:
    """Parse a single SELECT item into expression + alias."""
    raw = item.strip()
    if not raw:
        return None

    # Check for AS alias (must be at level 0 parentheses)
    alias_match = _find_as_alias(raw)
    if alias_match:
        expression, alias = alias_match
        return {
            "expression": expression.strip().upper(),
            "output_alias": alias.strip().upper(),
            "raw_text": raw,
        }

    # No AS — try to derive alias from expression
    upper = raw.upper().strip()

    # TABLE.COLUMN → alias is COLUMN
    if "." in upper and not _is_function_call(upper) and "(" not in upper:
        parts = upper.rsplit(".", 1)
        return {
            "expression": upper,
            "output_alias": parts[1],
            "raw_text": raw,
        }

    # Plain column name
    if re.fullmatch(r"[A-Z_][A-Z0-9_$#]*", upper):
        return {
            "expression": upper,
            "output_alias": upper,
            "raw_text": raw,
        }

    # Complex expression without alias — use as-is
    return {
        "expression": upper,
        "output_alias": "",
        "raw_text": raw,
    }


def _find_as_alias(item: str) -> Optional[Tuple[str, str]]:
    """Find AS alias at parenthesis level 0, returning (expression, alias)."""
    # Search from the end for AS keyword at level 0
    upper = item.upper()
    depth = 0
    i = len(upper) - 1

    # First, find the last word (potential alias)
    # Pattern: ... AS ALIAS_NAME or ... AS "ALIAS NAME"
    # Look for AS at level 0
    positions = []
    depth = 0
    for idx in range(len(upper)):
        ch = upper[idx]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0 and upper[idx:idx+3] == ' AS' and (idx + 3 >= len(upper) or upper[idx+3] in (' ', '\t', '\n', '"')):
            # Verify it's word-bounded before
            if idx == 0 or not upper[idx-1].isalnum():
                positions.append(idx)

    if positions:
        # Use the last AS at level 0
        as_pos = positions[-1]
        expression = item[:as_pos].strip()
        alias = item[as_pos + 3:].strip().strip('"').strip()
        if alias and re.fullmatch(r"[A-Z_][A-Z0-9_$#]*", alias.upper()):
            return (expression, alias)

    # Also handle implicit alias: trailing identifier after a space at level 0
    # e.g., "FUNC(x) MY_ALIAS" — less common, skip for now

    return None


def _is_function_call(expr: str) -> bool:
    """Check if expression looks like a function call."""
    return bool(re.match(r"[A-Z_][A-Z0-9_]*\s*\(", expr.upper()))
