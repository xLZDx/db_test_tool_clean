"""Per-attribute test generator service.

Generates one test case per target column (e.g. 367 tests for 367 matched attributes).
Each test includes the full CTE/JOIN source query that validates a single attribute
from source to target using the resolved mapping.

Supports CT, DRD, and Chat flow test generation.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def generate_attribute_tests(
    analysis_rows: List[Dict[str, Any]],
    target_schema: str,
    target_table: str,
    source_schema: str = "",
    primary_source_table: str = "",
    generated_sql: str = "",
    join_sql: str = "",
    source_datasource_id: int = 0,
    target_datasource_id: int = 0,
    pbi_id: str = "",
    suite_prefix: str = "CT",
    grain_columns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Generate one test per attribute from comparison results.

    Args:
        analysis_rows: DRD rows with column, source_attribute, generated_expression, etc.
        target_schema: e.g. SSDS_TRANSACTIONS_OWNER
        target_table: e.g. AVY_FACT
        source_schema: e.g. CCAL_REPL_OWNER
        primary_source_table: e.g. TXN (main source alias B)
        generated_sql: full generated CTE/INSERT SQL
        join_sql: extracted JOIN block from generated SQL
        source_datasource_id: DS ID for source
        target_datasource_id: DS ID for target
        pbi_id: PBI reference (e.g. PBI2674782)
        suite_prefix: CT, DRD, or CHAT
        grain_columns: columns forming the business key (for join condition)

    Returns:
        List of test case dicts ready for POST /api/tests
    """
    target_schema_u = (target_schema or "").strip().upper()
    target_table_u = (target_table or "").strip().upper()
    source_schema_u = (source_schema or "").strip().upper()
    primary_src = (primary_source_table or "").strip().upper()
    grain_cols = [g.upper() for g in (grain_columns or ["TXN_ID"])]

    # Extract main FROM + JOIN block from generated SQL (skips CTEs)
    from_join_block = _extract_main_from_join_block(generated_sql or join_sql, target_schema_u, target_table_u)

    tests = []
    for i, row in enumerate(analysis_rows, start=1):
        col = (row.get("column") or row.get("physical_name") or "").strip().upper()
        if not col:
            continue

        source_attr = (row.get("source_attribute") or "").strip().upper()
        gen_expr = (row.get("generated_expression") or "").strip()
        source_table = (row.get("source_table") or primary_src).strip().upper()
        source_sch = (row.get("source_schema") or source_schema_u).strip().upper()
        transformation = (row.get("transformation") or "").strip()

        # Resolve the source expression — prefer generated, fall back to source_attr, then col name
        if gen_expr:
            src_expr = gen_expr
        elif source_attr:
            src_expr = source_attr
        else:
            src_expr = col

        # Build source-only query (NEVER joins to the target table)
        source_query = _build_attribute_source_query(
            target_column=col,
            source_expression=src_expr,
            from_join_block=from_join_block,
            source_schema=source_sch,
            source_table=source_table,
        )

        # Target query — single source: only the target table, counts non-null values
        target_query = (
            f"-- Target column: {target_schema_u}.{target_table_u}.{col}\n"
            f"SELECT COUNT(*) AS cnt\n"
            f"FROM {target_schema_u}.{target_table_u}\n"
            f"WHERE {col} IS NOT NULL"
        )

        # Description includes mapping lineage
        desc_parts = [f"{pbi_id}: " if pbi_id else ""]
        desc_parts.append(f"{suite_prefix} attribute validation [{i}] {col}")
        if source_attr and source_attr != col:
            desc_parts.append(f" | Source: {source_sch}.{source_table}.{source_attr}" if source_sch else f" | Source: {source_table}.{source_attr}")
        if transformation:
            desc_parts.append(f" | Transform: {transformation[:100]}")

        test = {
            "name": f"{suite_prefix}_{target_table_u}_{col}",
            "test_type": "value_match",
            "source_datasource_id": source_datasource_id or None,
            "target_datasource_id": target_datasource_id or None,
            "severity": "high" if col in grain_cols else "medium",
            "description": "".join(desc_parts),
            "source_query": source_query,
            "target_query": target_query,
            "expected_result": "0",
        }
        tests.append(test)

    return tests


def generate_ct_suite(
    analysis_rows: List[Dict[str, Any]],
    generated_sql: str,
    target_schema: str,
    target_table: str,
    source_datasource_id: int = 0,
    target_datasource_id: int = 0,
    pbi_id: str = "",
    grain_columns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Generate CT_TEST suite — per-attribute with full CTE/INSERT validation."""
    # Extract primary source from generated SQL
    source_schema, primary_table = _extract_primary_source(generated_sql)

    return generate_attribute_tests(
        analysis_rows=analysis_rows,
        target_schema=target_schema,
        target_table=target_table,
        source_schema=source_schema,
        primary_source_table=primary_table,
        generated_sql=generated_sql,
        source_datasource_id=source_datasource_id,
        target_datasource_id=target_datasource_id,
        pbi_id=pbi_id,
        suite_prefix="CT",
        grain_columns=grain_columns,
    )


def generate_drd_suite(
    analysis_rows: List[Dict[str, Any]],
    generated_sql: str,
    target_schema: str,
    target_table: str,
    source_datasource_id: int = 0,
    target_datasource_id: int = 0,
    pbi_id: str = "",
    grain_columns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Generate DRD_TEST suite — per-attribute with full join query."""
    source_schema, primary_table = _extract_primary_source(generated_sql)

    return generate_attribute_tests(
        analysis_rows=analysis_rows,
        target_schema=target_schema,
        target_table=target_table,
        source_schema=source_schema,
        primary_source_table=primary_table,
        generated_sql=generated_sql,
        source_datasource_id=source_datasource_id,
        target_datasource_id=target_datasource_id,
        pbi_id=pbi_id,
        suite_prefix="DRD",
        grain_columns=grain_columns,
    )


def generate_chat_suite(
    analysis_rows: List[Dict[str, Any]],
    generated_sql: str,
    target_schema: str,
    target_table: str,
    source_datasource_id: int = 0,
    target_datasource_id: int = 0,
    pbi_id: str = "",
    grain_columns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Generate CHAT_TEST suite — per-attribute with source validation."""
    source_schema, primary_table = _extract_primary_source(generated_sql)

    return generate_attribute_tests(
        analysis_rows=analysis_rows,
        target_schema=target_schema,
        target_table=target_table,
        source_schema=source_schema,
        primary_source_table=primary_table,
        generated_sql=generated_sql,
        source_datasource_id=source_datasource_id,
        target_datasource_id=target_datasource_id,
        pbi_id=pbi_id,
        suite_prefix="CHAT",
        grain_columns=grain_columns,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────


def _build_attribute_source_query(
    target_column: str,
    source_expression: str,
    from_join_block: str,
    source_schema: str = "",
    source_table: str = "",
) -> str:
    """Build a source-only validation query for one attribute.

    Rules:
    - NEVER joins to the target table.
    - NEVER uses ON 1=1.
    - Counts non-null values of the source expression.
    - Uses the sanitized FROM+JOIN block from the generated SQL (source tables only).
    """
    lines = [
        f"-- Source attribute: {target_column}",
        f"-- Expression: {source_expression}",
        f"SELECT COUNT(*) AS cnt",
    ]

    if from_join_block:
        lines.append(from_join_block)
    else:
        src_full = f"{source_schema}.{source_table}" if source_schema else source_table or "DUAL"
        lines.append(f"FROM {src_full}")

    lines.append(f"WHERE {source_expression} IS NOT NULL")
    return "\n".join(lines)


def _extract_main_from_join_block(sql: str, target_schema: str = "", target_table: str = "") -> str:
    """Extract the main query's FROM + JOIN block from SQL.

    Handles:
    - INSERT INTO ... SELECT ... FROM ...
    - WITH cte AS (...) SELECT ... FROM ...
    - Plain SELECT ... FROM ...

    Always strips:
    - Trailing semicolons
    - ON 1=1 joins (cartesian/bad joins)
    - Any JOIN that references the target table
    """
    if not sql:
        return ""

    # Strip trailing semicolons
    sql = sql.rstrip().rstrip(";").strip()

    main_sql = sql

    # For INSERT INTO ... SELECT: find the SELECT part after INSERT INTO
    insert_m = re.search(
        r"\bINSERT\b[^(]*?\bSELECT\b",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if insert_m:
        main_sql = sql[insert_m.end() - len("SELECT"):]
    elif re.match(r"\s*WITH\s+", sql, re.IGNORECASE):
        # CTEs: skip all CTE definitions, find the final SELECT after the last '\)'
        depth = 0
        pos = 0
        final_select_pos = 0
        i = 0
        n = len(sql)
        while i < n:
            ch = sql[i]
            if ch == "'":
                i += 1
                while i < n:
                    if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    if sql[i] == "'":
                        i += 1
                        break
                    i += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    # After this closing paren of a CTE, look for INSERT/SELECT at depth 0
                    rest = sql[i + 1:].lstrip()
                    upper_rest = rest.upper()
                    if upper_rest.startswith("SELECT") or upper_rest.startswith("INSERT"):
                        offset = len(sql[i + 1:]) - len(rest)
                        final_select_pos = i + 1 + offset
                        break
            i += 1
        if final_select_pos:
            candidate = sql[final_select_pos:]
            # If INSERT INTO follows CTE, recurse to get SELECT part
            ins2 = re.search(r"\bINSERT\b[^(]*?\bSELECT\b", candidate, re.IGNORECASE | re.DOTALL)
            if ins2:
                main_sql = candidate[ins2.end() - len("SELECT"):]
            else:
                main_sql = candidate

    # Now extract FROM ... [JOINs] up to WHERE / GROUP BY / ORDER BY / HAVING
    from_match = re.search(
        r"\bFROM\b([\s\S]+?)(?=\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|$)",
        main_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not from_match:
        return ""

    block = ("FROM" + from_match.group(1)).strip()

    # Limit to reasonable size
    if len(block) > 20000:
        block = block[:20000]

    # Sanitize: strip trailing semicolons
    block = block.rstrip(";")

    # Sanitize: remove ON 1=1 joins (cartesian cross-joins)
    block = re.sub(
        r"\s*(?:LEFT|RIGHT|INNER|OUTER|CROSS|FULL)?\s*(?:OUTER\s+)?JOIN\s+[\w.$\"]+(?:\s+\w+)?\s+ON\s+1\s*=\s*1[^\n]*",
        "",
        block,
        flags=re.IGNORECASE,
    )

    # Sanitize: remove any JOIN that references the target table (prevent source+target mixing)
    if target_schema and target_table:
        block = re.sub(
            rf"\s*(?:LEFT|RIGHT|INNER|OUTER|CROSS|FULL)?\s*(?:OUTER\s+)?JOIN\s+{re.escape(target_schema)}\.{re.escape(target_table)}\b[^\n]*",
            "",
            block,
            flags=re.IGNORECASE,
        )
        # Also remove bare target table name joins (without schema)
        block = re.sub(
            rf"\s*(?:LEFT|RIGHT|INNER|OUTER|CROSS|FULL)?\s*(?:OUTER\s+)?JOIN\s+{re.escape(target_table)}\s+",
            "",
            block,
            flags=re.IGNORECASE,
        )

    return block.strip()


def _extract_from_join_block(sql: str) -> str:
    """Backward-compat alias — delegates to the main extractor."""
    return _extract_main_from_join_block(sql)


# Backward-compat alias kept so existing callers don't break
_build_attribute_validation_query = _build_attribute_source_query


def _extract_primary_source(sql: str) -> tuple:
    """Extract primary source schema.table from SQL (first FROM table)."""
    if not sql:
        return ("", "")

    m = re.search(
        r"\bFROM\s+([\w$#]+)\.([\w$#]+)",
        sql,
        flags=re.IGNORECASE,
    )
    if m:
        return (m.group(1).upper(), m.group(2).upper())

    m = re.search(r"\bFROM\s+([\w$#]+)", sql, flags=re.IGNORECASE)
    if m:
        return ("", m.group(1).upper())

    return ("", "")
