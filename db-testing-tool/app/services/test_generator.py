"""Test generation service – create test cases from mapping rules and schema metadata."""
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.mapping_rule import MappingRule
from app.models.test_case import TestCase
from app.models.schema_object import SchemaObject, ColumnProfile
from app.services.schema_kb_service import load_schema_kb_payload
from app.services.sql_pattern_validation import split_valid_invalid_test_defs
import json, logging, re
from rapidfuzz import fuzz, process as rfprocess

logger = logging.getLogger(__name__)


def _resolve_schema_table_from_kb(datasource_id: int | None, schema: str | None, table: str | None) -> tuple[str | None, str | None]:
    target_schema = (schema or "").strip()
    target_table = (table or "").strip()
    if not target_table:
        return target_schema, target_table

    payload = load_schema_kb_payload(datasource_id)
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    if not sources:
        return target_schema, target_table

    schema_map = {}
    for src in sources:
        pdm = (src or {}).get("pdm", {})
        for s in pdm.get("schemas", []) or []:
            sname = (s.get("schema") or "").strip()
            if not sname:
                continue
            schema_map.setdefault(sname.upper(), {})
            for t in s.get("tables", []) or []:
                tname = (t.get("name") or "").strip()
                if tname:
                    schema_map[sname.upper()][tname.upper()] = (sname, tname)

    if not schema_map:
        return target_schema, target_table

    schema_up = target_schema.upper() if target_schema else ""
    table_up = target_table.upper()

    if schema_up and schema_up in schema_map:
        candidates = schema_map[schema_up]
        if table_up in candidates:
            return candidates[table_up]
        hit = rfprocess.extractOne(table_up, list(candidates.keys()), scorer=fuzz.token_sort_ratio, score_cutoff=78)
        if hit:
            return candidates[hit[0]]

    # Fallback to global table match across all schemas.
    for sname_up, tables in schema_map.items():
        if table_up in tables:
            return tables[table_up]

    all_table_keys = []
    reverse = {}
    for tables in schema_map.values():
        for tkey, resolved in tables.items():
            all_table_keys.append(tkey)
            reverse[tkey] = resolved
    hit = rfprocess.extractOne(table_up, all_table_keys, scorer=fuzz.token_sort_ratio, score_cutoff=78)
    if hit:
        return reverse[hit[0]]

    return target_schema, target_table


def _load_table_columns_from_kb(datasource_id: int | None, schema: str | None, table: str | None) -> list[str]:
    if not table:
        return []

    payload = load_schema_kb_payload(datasource_id)
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    if not sources:
        return []

    schema_up = (schema or "").strip().upper()
    table_up = (table or "").strip().upper()
    for src in sources:
        pdm = (src or {}).get("pdm", {})
        for s in pdm.get("schemas", []) or []:
            sname = (s.get("schema") or "").strip()
            if schema_up and sname.upper() != schema_up:
                continue
            for t in s.get("tables", []) or []:
                tname = (t.get("name") or "").strip()
                if tname.upper() != table_up:
                    continue
                cols = []
                for c in t.get("columns", []) or []:
                    cname = (c.get("name") or "").strip()
                    if cname:
                        cols.append(cname)
                return cols
    return []


def _parse_mapping_columns(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, list):
            out = []
            for v in loaded:
                txt = str(v).strip().strip('"')
                if txt:
                    out.append(txt)
            return out
    except Exception:
        pass
    return []


def _resolve_column_against_kb(candidate: str, valid_columns: list[str]) -> str | None:
    col = (candidate or "").strip().strip('"')
    if not col:
        return None
    if not valid_columns:
        return col

    valid_map = {c.upper(): c for c in valid_columns if c}
    direct = valid_map.get(col.upper())
    if direct:
        return direct

    hit = rfprocess.extractOne(col.upper(), list(valid_map.keys()), scorer=fuzz.token_sort_ratio, score_cutoff=78)
    if hit:
        return valid_map[hit[0]]
    return None


def _validated_mapping_pairs(rule: MappingRule) -> list[tuple[str, str]]:
    src_schema, src_table = _resolve_schema_table_from_kb(
        rule.source_datasource_id,
        rule.source_schema,
        rule.source_table,
    )
    tgt_schema, tgt_table = _resolve_schema_table_from_kb(
        rule.target_datasource_id,
        rule.target_schema,
        rule.target_table,
    )

    src_schema = src_schema or rule.source_schema
    src_table = src_table or rule.source_table
    tgt_schema = tgt_schema or rule.target_schema
    tgt_table = tgt_table or rule.target_table

    src_kb_cols = _load_table_columns_from_kb(rule.source_datasource_id, src_schema, src_table)
    tgt_kb_cols = _load_table_columns_from_kb(rule.target_datasource_id, tgt_schema, tgt_table)

    src_cols = _parse_mapping_columns(rule.source_columns)
    tgt_cols = _parse_mapping_columns(rule.target_columns)

    pairs: list[tuple[str, str]] = []
    for src_col, tgt_col in zip(src_cols, tgt_cols):
        src_resolved = _resolve_column_against_kb(src_col, src_kb_cols)
        tgt_resolved = _resolve_column_against_kb(tgt_col, tgt_kb_cols)
        if src_resolved and tgt_resolved:
            pairs.append((src_resolved, tgt_resolved))
        else:
            logger.warning(
                "Skipping mapping pair due to KB validation miss: rule_id=%s source=%s target=%s",
                rule.id,
                src_col,
                tgt_col,
            )
    return pairs

# ── Template-based test generators ──────────────────────────────────────────

def _pretty_and_clause(expr: str) -> str:
    text = (expr or "").strip()
    if not text:
        return text
    return re.sub(r'\s+AND\s+', '\nAND ', text, flags=re.IGNORECASE)

def _extract_lookup_left_join(transformation_sql: str | None) -> str:
    if not transformation_sql:
        return ""
    m = re.search(
        r'(LEFT\s+JOIN\s+[\w\.\"]+\s+LK\s+ON\s+.+?)(?=\n\s*WHERE\b|\n\s*GROUP\b|\n\s*ORDER\b|$)',
        transformation_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    raw = re.sub(r'\s+', ' ', m.group(1)).strip()
    on_parts = re.split(r'\s+ON\s+', raw, maxsplit=1, flags=re.IGNORECASE)
    if len(on_parts) != 2:
        return "\n" + raw
    return f"\n{on_parts[0]}\nON {_pretty_and_clause(on_parts[1])}"


def _make_nvl_safe(expr: str) -> str:
    """Wrap column references like S.COL or T."COL" with NVL(TO_CHAR(...), '-999').

    Avoid double-wrapping if NVL or TO_CHAR already present nearby.
    This is a best-effort transformation to make JOIN and comparison keys null-safe.
    """
    if not expr:
        return expr

    # match patterns like S.COL or T."COL"; use a larger lookback to avoid double-wrap
    pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.(\"?)([A-Za-z0-9_]+)(\"?)\b")

    def _repl(m: re.Match) -> str:
        start = m.start()
        # guard: if NVL or TO_CHAR appears before the match, skip wrapping
        lookback = expr[max(0, start - 64):start].upper()
        if 'NVL(' in lookback or 'TO_CHAR(' in lookback:
            return m.group(0)
        alias = m.group(1)
        quote = m.group(2) or ''
        col = m.group(3)
        # Always use string '-999' as NVL default per requirement
        return f"NVL(TO_CHAR({alias}.{quote}{col}{quote}), '-999')"

    return pattern.sub(_repl, expr)


def _build_from_where_clause(rule: MappingRule) -> tuple[str, str]:
    src_schema, src_table = _resolve_schema_table_from_kb(
        rule.source_datasource_id,
        rule.source_schema,
        rule.source_table,
    )
    tgt_schema, tgt_table = _resolve_schema_table_from_kb(
        rule.target_datasource_id,
        rule.target_schema,
        rule.target_table,
    )

    src_schema = src_schema or rule.source_schema or ""
    src_table = src_table or rule.source_table or ""
    tgt_schema = tgt_schema or rule.target_schema or ""
    tgt_table = tgt_table or rule.target_table or ""

    src_fq = f'"{src_schema}"."{src_table}" S'
    tgt_fq = f'"{tgt_schema}"."{tgt_table}" T'
    join_on = _pretty_and_clause((rule.join_condition or '1=1').strip())
    # Make join keys null-safe by wrapping column refs with NVL/TO_CHAR
    join_on = _make_nvl_safe(join_on)
    lookup_left_join = _extract_lookup_left_join(rule.transformation_sql)
    lookup_left_join = _make_nvl_safe(lookup_left_join)
    from_clause = f"FROM {src_fq}\nLEFT JOIN {tgt_fq}\nON {join_on}{lookup_left_join}"
    where_clause = f"\nWHERE {_pretty_and_clause(rule.filter_condition.strip())}" if rule.filter_condition and rule.filter_condition.strip() else ""
    # Null-safe the where clause comparisons as well
    if where_clause:
        # strip leading \nWHERE to transform internals then reattach
        inner = where_clause[7:]
        inner = _make_nvl_safe(inner)
        where_clause = f"\nWHERE {_pretty_and_clause(inner)}"
    return from_clause, where_clause

def _row_count_test(rule: MappingRule) -> TestCase:
    """Generate row count test with Source LEFT JOIN Target (+ optional Lookup LEFT JOIN)."""
    from_clause, where_clause = _build_from_where_clause(rule)
    
    return TestCase(
        name=f"Row Count: {rule.source_table} → {rule.target_table}",
        test_type="row_count",
        mapping_rule_id=rule.id,
        source_datasource_id=rule.source_datasource_id or rule.target_datasource_id,
        target_datasource_id=rule.target_datasource_id or rule.source_datasource_id,
        source_query=f"SELECT /*+ PARALLEL(8) */\nCOUNT(*) AS cnt\n{from_clause}{where_clause}",
        target_query=f"SELECT /*+ PARALLEL(8) */\nCOUNT(*) AS cnt\n{from_clause}",
        severity="high",
        description="Verify row counts using source LEFT JOIN target pattern.",
    )


def _null_check_tests(rule: MappingRule, target_cols: List[dict]) -> List[TestCase]:
    """Generate nullable/PK checks using source LEFT JOIN target pattern."""
    tests = []
    from_clause, _ = _build_from_where_clause(rule)
    non_nullable = [c for c in target_cols if not c["nullable"] or c["is_pk"]]
    for col in non_nullable:
        tests.append(TestCase(
            name=f"Null Check: {rule.target_table}.{col['name']}",
            test_type="null_check",
            mapping_rule_id=rule.id,
            target_datasource_id=rule.target_datasource_id or rule.source_datasource_id,
            source_query=f"SELECT /*+ PARALLEL(8) */\nCOUNT(*) AS cnt\n{from_clause}\nWHERE T.\"{col['name']}\" IS NULL",
            target_query=f"SELECT /*+ PARALLEL(8) */\nCOUNT(*) AS cnt\n{from_clause}\nWHERE T.\"{col['name']}\" IS NULL",
            expected_result=json.dumps({"cnt": 0}),
            severity="critical" if col["is_pk"] else "high",
            description=f"Ensure no NULLs in {col['name']} (source LEFT JOIN target).",
        ))
    return tests


def _uniqueness_test(rule: MappingRule, pk_cols: List[str]) -> TestCase | None:
    """Generate uniqueness check using source LEFT JOIN target pattern."""
    if not pk_cols:
        return None
    from_clause, _ = _build_from_where_clause(rule)
    pk_list = ", ".join(f'T."{c}"' for c in pk_cols)
    return TestCase(
        name=f"Uniqueness: {rule.target_table} PK ({', '.join(pk_cols)})",
        test_type="uniqueness",
        mapping_rule_id=rule.id,
        target_datasource_id=rule.target_datasource_id or rule.source_datasource_id,
        target_query=(
            f"SELECT /*+ PARALLEL(8) */\n{pk_list}, COUNT(*) AS dup_cnt\n{from_clause}\n"
            f"GROUP BY {pk_list} HAVING COUNT(*) > 1"
        ),
        expected_result=json.dumps({"rows": 0}),
        severity="critical",
        description=f"Ensure PK uniqueness on {', '.join(pk_cols)} (source LEFT JOIN target).",
    )


def _value_match_tests(rule: MappingRule) -> List[TestCase]:
    """Generate aggregate comparison tests using source LEFT JOIN target pattern."""
    tests = []
    validated_pairs = _validated_mapping_pairs(rule)
    from_clause, where_clause = _build_from_where_clause(rule)

    for sc, tc in validated_pairs:
        tests.append(TestCase(
            name=f"Value Match: {rule.source_table}.{sc} → {rule.target_table}.{tc}",
            test_type="value_match",
            mapping_rule_id=rule.id,
            source_datasource_id=rule.source_datasource_id or rule.target_datasource_id,
            target_datasource_id=rule.target_datasource_id or rule.source_datasource_id,
            source_query=(
                f'SELECT /*+ PARALLEL(8) */\nSUM(S."{sc}") AS sum_val, MIN(S."{sc}") AS min_val, '
                f'MAX(S."{sc}") AS max_val\n{from_clause}{where_clause}'
            ),
            target_query=(
                f'SELECT /*+ PARALLEL(8) */\nSUM(T."{tc}") AS sum_val, MIN(T."{tc}") AS min_val, '
                f'MAX(T."{tc}") AS max_val\n{from_clause}'
            ),
            severity="high",
            description=f"Compare aggregate values for {sc} → {tc} (source LEFT JOIN target).",
        ))
    return tests


_FRESHNESS_CANDIDATES = [
    "UPDATED_AT", "UPDATE_DT", "LAST_UPDATE_DATE", "LAST_UPDATED",
    "MODIFIED_DATE", "MODIFIED_AT", "LAST_MOD_DT", "LOAD_DT",
    "UPDATE_DATE", "LAST_CHANGE_DATE", "LAST_MODIFIED", "MODIFIED_TS",
]


def _freshness_test(rule: MappingRule, target_cols: list | None = None) -> TestCase | None:
    """Generate freshness test on target table.

    Searches ``target_cols`` for a known audit timestamp column.  Returns None
    when no suitable column is found so callers can skip the test rather than
    emitting a guaranteed ORA-00904.
    """
    tgt_fq = f'"{rule.target_schema}"."{rule.target_table}"'

    ts_col: str | None = None
    if target_cols:
        col_names_upper = {(c.get("name") or "").upper() for c in target_cols}
        for candidate in _FRESHNESS_CANDIDATES:
            if candidate in col_names_upper:
                ts_col = candidate
                break

    if ts_col is None:
        return None

    return TestCase(
        name=f"Freshness: {rule.target_table}",
        test_type="freshness",
        mapping_rule_id=rule.id,
        target_datasource_id=rule.target_datasource_id,
        target_query=f"SELECT /*+ PARALLEL(8) */ MAX({ts_col}) AS last_update FROM {tgt_fq}",
        severity="medium",
        description=f"Check target table freshness via {ts_col}.",
    )


# ── Main generator ──────────────────────────────────────────────────────────

async def generate_tests_for_rule(db: AsyncSession, rule_id: int, connection_id: int = None) -> List[TestCase]:
    """Generate a standard test pack for a single mapping rule.
    
    Args:
        db: Database session
        rule_id: ID of the mapping rule  
        connection_id: Optional database connection ID for test context (future use)
    """
    rule = await db.get(MappingRule, rule_id)
    if not rule:
        raise ValueError(f"MappingRule {rule_id} not found")

    # Get target columns from metadata (if available)
    tgt_obj_r = await db.execute(
        select(SchemaObject).where(
            SchemaObject.datasource_id == rule.target_datasource_id,
            SchemaObject.schema_name == rule.target_schema,
            SchemaObject.object_name == rule.target_table,
        )
    )
    tgt_obj = tgt_obj_r.scalar_one_or_none()
    target_cols = []
    pk_cols = []
    if tgt_obj:
        cols_r = await db.execute(
            select(ColumnProfile).where(ColumnProfile.schema_object_id == tgt_obj.id)
        )
        for c in cols_r.scalars().all():
            target_cols.append({
                "name": c.column_name, "data_type": c.data_type,
                "nullable": c.nullable, "is_pk": c.is_pk,
            })
            if c.is_pk:
                pk_cols.append(c.column_name)

    tests: List[TestCase] = []

    # 1. Row count
    tests.append(_row_count_test(rule))

    # 2. Null checks on non-nullable / PK columns
    tests.extend(_null_check_tests(rule, target_cols))

    # 3. PK uniqueness
    uk = _uniqueness_test(rule, pk_cols)
    if uk:
        tests.append(uk)

    # 4. Value match (aggregate comparison)
    tests.extend(_value_match_tests(rule))

    # 5. Freshness (skipped when no suitable timestamp column is found)
    freshness = _freshness_test(rule, target_cols)
    if freshness is not None:
        tests.append(freshness)

    # Persist
    for t in tests:
        db.add(t)
    await db.commit()

    return tests


async def preview_tests_for_rule(db: AsyncSession, rule_id: int) -> List[dict]:
    """Preview test definitions for a mapping rule WITHOUT saving them."""
    rule = await db.get(MappingRule, rule_id)
    if not rule:
        raise ValueError(f"MappingRule {rule_id} not found")

    tgt_obj_r = await db.execute(
        select(SchemaObject).where(
            SchemaObject.datasource_id == rule.target_datasource_id,
            SchemaObject.schema_name == rule.target_schema,
            SchemaObject.object_name == rule.target_table,
        )
    )
    tgt_obj = tgt_obj_r.scalar_one_or_none()
    target_cols = []
    pk_cols = []
    if tgt_obj:
        cols_r = await db.execute(
            select(ColumnProfile).where(ColumnProfile.schema_object_id == tgt_obj.id)
        )
        for c in cols_r.scalars().all():
            target_cols.append({
                "name": c.column_name, "data_type": c.data_type,
                "nullable": c.nullable, "is_pk": c.is_pk,
            })
            if c.is_pk:
                pk_cols.append(c.column_name)

    tests: List[TestCase] = []
    tests.append(_row_count_test(rule))
    tests.extend(_null_check_tests(rule, target_cols))
    uk = _uniqueness_test(rule, pk_cols)
    if uk:
        tests.append(uk)
    tests.extend(_value_match_tests(rule))
    freshness = _freshness_test(rule, target_cols)
    if freshness is not None:
        tests.append(freshness)

    return [
        {
            "name": t.name,
            "test_type": t.test_type,
            "mapping_rule_id": t.mapping_rule_id,
            "source_datasource_id": t.source_datasource_id,
            "target_datasource_id": t.target_datasource_id,
            "source_query": t.source_query,
            "target_query": t.target_query,
            "expected_result": t.expected_result,
            "severity": t.severity,
            "description": t.description,
        }
        for t in tests
    ]


async def create_selected_tests(db: AsyncSession, test_defs: List[dict]) -> List[TestCase]:
    """Create only selected tests from preview definitions."""
    valid_defs, invalid_defs = split_valid_invalid_test_defs(test_defs)
    if invalid_defs:
        sample = invalid_defs[:8]
        details = []
        for bad in sample:
            name = bad.get("name") or f"index {bad.get('index')}"
            errs = (bad.get("pattern_errors", {}).get("source") or []) + (bad.get("pattern_errors", {}).get("target") or [])
            details.append(f"{name}: {', '.join(errs)}")
        raise ValueError(
            "SQL pattern validation failed for one or more tests. "
            + " | ".join(details)
        )

    created = []
    for td in valid_defs:
        tc = TestCase(
            name=td["name"],
            test_type=td["test_type"],
            mapping_rule_id=td.get("mapping_rule_id"),
            source_datasource_id=td.get("source_datasource_id"),
            target_datasource_id=td.get("target_datasource_id"),
            source_query=td.get("source_query"),
            target_query=td.get("target_query"),
            expected_result=td.get("expected_result"),
            severity=td.get("severity", "medium"),
            description=td.get("description"),
            is_active=td.get("is_active", True),
        )
        db.add(tc)
        created.append(tc)
    await db.commit()
    return created


async def generate_tests_for_all_rules(db: AsyncSession, connection_id: int = None) -> int:
    """Generate tests for every mapping rule. Returns total tests created.
    
    Args:
        db: Database session
        connection_id: Optional database connection ID for test context (future use)
    """
    rules_r = await db.execute(select(MappingRule))
    count = 0
    for rule in rules_r.scalars().all():
        ts = await generate_tests_for_rule(db, rule.id, connection_id)
        count += len(ts)
    return count
