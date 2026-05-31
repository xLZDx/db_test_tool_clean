"""Test execution service – run test cases against live databases."""
import asyncio
import os
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.test_case import TestCase, TestRun
from app.models.datasource import DataSource
from app.connectors.factory import get_connector_from_model
import json, time, uuid, logging
import re
import difflib
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DS_FAILURE_CACHE = {}
_DS_FAILURE_TTL_SECONDS = 120  # 2-min cool-down after DPY-6005 / hard timeout

# Only cache connection-level errors (not query-specific ORA errors)
_CACHEABLE_ERROR_PREFIXES = ("DPY-", "TNS-", "ORA-12", "ORA-01017", "ORA-28")


def _is_connection_error(error_msg: str) -> bool:
    """Return True if error is a connection/auth problem (should cache), False for query errors."""
    if not error_msg:
        return False
    return any(error_msg.strip().startswith(p) for p in _CACHEABLE_ERROR_PREFIXES)


def _execute_single(connector, sql: str) -> dict:
    """Run a query and return first-row dict or row count."""
    try:
        rows = connector.execute_query(sql)
        return {"rows": rows, "count": len(rows), "error": None}
    except Exception as e:
        return {"rows": [], "count": 0, "error": str(e)}


def _line_col_from_index(text: str, idx: int) -> tuple[int, int]:
    if idx < 0:
        return 1, 1
    prefix = (text or "")[:idx]
    line = prefix.count("\n") + 1
    last_nl = prefix.rfind("\n")
    col = idx + 1 if last_nl < 0 else (idx - last_nl)
    return line, col


def _analyze_sql_references(connector, sql: str) -> dict:
    refs: list[dict] = []
    patt = re.compile(r'\b(FROM|JOIN|INTO)\s+([A-Z0-9_\.\"]+)(?:\s+([A-Z][A-Z0-9_]*))?', flags=re.IGNORECASE)
    for m in patt.finditer(sql or ""):
        raw = (m.group(2) or "").strip()
        if not raw or raw.startswith("("):
            continue
        token = raw.replace('"', '')
        parts = token.split('.')
        schema = parts[-2].upper() if len(parts) >= 2 else ""
        table = parts[-1].upper()
        alias = (m.group(3) or "").strip().upper() or table
        line, col = _line_col_from_index(sql or "", m.start(2))
        refs.append({"schema": schema, "table": table, "alias": alias, "line": line, "column": col})

    alias_map = {r["alias"]: (r["schema"], r["table"]) for r in refs if r.get("alias")}
    missing_tables: list[dict] = []
    columns_cache: dict = {}
    missing_columns: list[dict] = []

    for r in refs:
        schema = r.get("schema") or ""
        table = r.get("table") or ""
        if not schema or not table:
            continue
        exists = True
        if hasattr(connector, "table_exists"):
            try:
                exists = bool(connector.table_exists(schema, table))
            except Exception:
                exists = True
        if not exists:
            closest_table = None
            if hasattr(connector, "get_tables"):
                try:
                    known = [str(t.table_name).upper() for t in connector.get_tables(schema)]
                    match = difflib.get_close_matches(table, known, n=1, cutoff=0.62)
                    if match:
                        closest_table = match[0]
                except Exception:
                    pass
            missing_tables.append({
                "schema": schema,
                "table": table,
                "line": r.get("line"),
                "column": r.get("column"),
                "closest_table": closest_table,
            })

    for m in re.finditer(r"\b([A-Z_][A-Z0-9_]*)\.([A-Z_][A-Z0-9_\$#]*)\b", (sql or ""), flags=re.IGNORECASE):
        alias = (m.group(1) or "").upper()
        col_name = (m.group(2) or "").upper()
        if alias not in alias_map:
            continue
        schema, table = alias_map[alias]
        if not schema or not table or not hasattr(connector, "get_columns"):
            continue
        key = (schema, table)
        if key not in columns_cache:
            try:
                cols = connector.get_columns(schema, table)
                columns_cache[key] = {c.column_name.upper() for c in cols}
            except Exception:
                columns_cache[key] = set()
        if columns_cache[key] and col_name not in columns_cache[key]:
            line, col = _line_col_from_index(sql or "", m.start(2))
            closest_column = None
            try:
                match = difflib.get_close_matches(col_name, list(columns_cache[key]), n=1, cutoff=0.58)
                if match:
                    closest_column = match[0]
            except Exception:
                pass
            missing_columns.append({
                "schema": schema,
                "table": table,
                "column": col_name,
                "alias": alias,
                "line": line,
                "column_pos": col,
                "closest_column": closest_column,
            })

    suggestions: list[str] = []
    for t in missing_tables:
        hint = f" Did you mean {t['schema']}.{t['closest_table']}?" if t.get("closest_table") else ""
        suggestions.append(f"Table not found at line {t.get('line')}: {t['schema']}.{t['table']}.{hint}")
    for c in missing_columns:
        hint = f" Closest column: {c['closest_column']}." if c.get("closest_column") else ""
        suggestions.append(f"Column not found at line {c.get('line')}: {c['schema']}.{c['table']}.{c['column']} (alias {c['alias']}).{hint}")

    return {
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "suggestions": suggestions,
    }


def _build_oracle_error_payload(error_msg: str, sql: str, connector) -> dict:
    diagnostics = {}
    try:
        diagnostics = _analyze_sql_references(connector, sql)
    except Exception:
        diagnostics = {}

    suggestions = list((diagnostics or {}).get("suggestions") or [])
    err_u = str(error_msg or "").upper()
    if "ORA-00942" in err_u:
        suggestions.append("ORA-00942 detected: referenced table/view does not exist or is not accessible.")
    m = re.search(r'ORA-00904:\s*"?([A-Z0-9_\$#]+)"?: invalid identifier', str(error_msg or ""), flags=re.IGNORECASE)
    if m:
        bad = m.group(1).upper()
        closest = None
        for item in diagnostics.get("missing_columns", []) or []:
            if (item.get("column") or "").upper() == bad and item.get("closest_column"):
                closest = item.get("closest_column")
                break
        suffix = f" Closest match: {closest}." if closest else ""
        suggestions.append(f"ORA-00904 invalid identifier: {bad}.{suffix}")

    return {
        "error": str(error_msg or ""),
        "diagnostics": diagnostics,
        "suggestions": suggestions,
    }


def _compare_results(test: TestCase, src_res: dict, tgt_res: dict) -> tuple:
    """Returns (passed: bool, mismatch_count: int, detail: str)."""
    if src_res.get("error"):
        return False, 0, f"Source error: {src_res['error']}"
    if tgt_res.get("error"):
        return False, 0, f"Target error: {tgt_res['error']}"

    if test.test_type == "row_count":
        s_cnt = src_res["rows"][0].get("cnt", src_res["rows"][0].get("CNT", 0)) if src_res["rows"] else 0
        t_cnt = tgt_res["rows"][0].get("cnt", tgt_res["rows"][0].get("CNT", 0)) if tgt_res["rows"] else 0
        diff = abs(s_cnt - t_cnt)
        passed = diff <= (test.tolerance or 0)
        return passed, diff, f"Source={s_cnt}, Target={t_cnt}, Diff={diff}"

    if test.test_type in ("null_check", "uniqueness"):
        cnt = tgt_res["rows"][0].get("cnt", tgt_res["rows"][0].get("CNT", 0)) if tgt_res["rows"] else tgt_res["count"]
        if test.test_type == "uniqueness":
            cnt = tgt_res["count"]      # number of duplicate groups
        passed = cnt == 0
        return passed, cnt, f"Violations={cnt}"

    if test.test_type == "value_match":
        if not src_res["rows"] or not tgt_res["rows"]:
            return False, 0, "No data returned"
        mismatches = 0
        details = []
        sr = {k.lower(): v for k, v in src_res["rows"][0].items()}
        tr = {k.lower(): v for k, v in tgt_res["rows"][0].items()}
        for key in sr:
            sv, tv = sr[key], tr.get(key)
            if sv != tv:
                mismatches += 1
                details.append(f"{key}: src={sv} tgt={tv}")
        return mismatches == 0, mismatches, "; ".join(details) if details else "Match"

    if test.test_type == "freshness":
        if tgt_res["rows"]:
            last = tgt_res["rows"][0].get("last_update") or tgt_res["rows"][0].get("LAST_UPDATE")
            return True, 0, f"Last update: {last}"
        return False, 0, "No freshness data"

    # custom_sql / schema_drift – just check for rows
    # Pick whichever result set has data (single-DB mode: only src; dual-DB: prefer tgt)
    result_set = src_res if (src_res["rows"] and not tgt_res["rows"]) else tgt_res

    if test.expected_result:
        expected = json.loads(test.expected_result)
        if isinstance(expected, (int, float)):
            exp_val = expected
        elif isinstance(expected, dict):
            exp_val = expected.get("cnt", expected.get("count", expected.get("rows", None)))
        else:
            exp_val = None

        if exp_val is not None:
            # Extract actual value: first column of first row (for COUNT(*) etc.)
            actual_val = None
            if result_set["rows"]:
                first_row = result_set["rows"][0]
                # Get first column value from the row dict
                first_col_val = next(iter(first_row.values())) if first_row else None
                if isinstance(first_col_val, (int, float)):
                    actual_val = first_col_val
                else:
                    try:
                        actual_val = int(first_col_val) if first_col_val is not None else None
                    except (ValueError, TypeError):
                        actual_val = result_set["count"]
            else:
                actual_val = result_set["count"]

            if actual_val is not None:
                diff = abs(actual_val - exp_val)
                passed = diff <= (test.tolerance or 0)
                return passed, diff, f"Expected={exp_val}, Actual={actual_val}"

    # CORRECTNESS FIX: Unknown test types should fail, not pass
    # Previously this was returning True (pass) which masked invalid test type configurations
    return False, 0, f"Unknown or unhandled test type: {test.test_type}"


async def run_test(db: AsyncSession, test_id: int, batch_id: Optional[str] = None) -> TestRun:
    """Execute a single test case and persist the result."""
    test = await db.get(TestCase, test_id)
    if not test:
        raise ValueError(f"TestCase {test_id} not found")

    batch_id = batch_id or str(uuid.uuid4())[:12]
    run = TestRun(test_case_id=test.id, batch_id=batch_id, status="running")
    db.add(run)
    await db.flush()

    connectors = {}
    try:
        start = time.time()

        # Source query – run blocking connector calls in a thread so the event loop stays
        # responsive (allows stop requests to be processed mid-execution).
        src_res = {"rows": [], "count": 0, "error": None}
        if test.source_query and test.source_datasource_id:
            src_ds = await db.get(DataSource, test.source_datasource_id)
            if src_ds:
                if (src_ds.db_type or "").strip().lower() == "redshift":
                    raise RuntimeError("Redshift testing is disabled. Use CDS or LH Oracle datasource.")
                now = time.time()
                src_key = f"{src_ds.id}:src"
                cached = _DS_FAILURE_CACHE.get(src_key)
                if cached and (now - cached.get("ts", 0)) < _DS_FAILURE_TTL_SECONDS:
                    src_res = {"rows": [], "count": 0, "error": cached.get("error")}
                else:
                    c = get_connector_from_model(src_ds)
                    await asyncio.to_thread(c.connect)
                    connectors["src"] = c
                    src_res = await asyncio.to_thread(_execute_single, c, test.source_query)
                    if src_res.get("error") and _is_connection_error(src_res["error"]):
                        _DS_FAILURE_CACHE[src_key] = {"error": src_res["error"], "ts": time.time()}
                    else:
                        _DS_FAILURE_CACHE.pop(src_key, None)

        # Target query
        tgt_res = {"rows": [], "count": 0, "error": None}
        if test.target_query and test.target_datasource_id:
            tgt_ds = await db.get(DataSource, test.target_datasource_id)
            if tgt_ds:
                if (tgt_ds.db_type or "").strip().lower() == "redshift":
                    raise RuntimeError("Redshift testing is disabled. Use CDS or LH Oracle datasource.")
                now = time.time()
                tgt_key = f"{tgt_ds.id}:tgt"
                cached = _DS_FAILURE_CACHE.get(tgt_key)
                if cached and (now - cached.get("ts", 0)) < _DS_FAILURE_TTL_SECONDS:
                    tgt_res = {"rows": [], "count": 0, "error": cached.get("error")}
                else:
                    c = get_connector_from_model(tgt_ds)
                    await asyncio.to_thread(c.connect)
                    connectors["tgt"] = c
                    tgt_res = await asyncio.to_thread(_execute_single, c, test.target_query)
                    if tgt_res.get("error") and _is_connection_error(tgt_res["error"]):
                        _DS_FAILURE_CACHE[tgt_key] = {"error": tgt_res["error"], "ts": time.time()}
                    else:
                        _DS_FAILURE_CACHE.pop(tgt_key, None)

        elapsed = int((time.time() - start) * 1000)

        passed, mismatches, detail = _compare_results(test, src_res, tgt_res)

        run.status = "passed" if passed else "failed"
        run.source_result = json.dumps(src_res["rows"][:10] if isinstance(src_res["rows"], list) else [],
                                        default=str) if src_res["rows"] else None
        run.target_result = json.dumps(tgt_res["rows"][:10] if isinstance(tgt_res["rows"], list) else [],
                                        default=str) if tgt_res["rows"] else None
        run.actual_result = json.dumps({"detail": detail}, default=str)
        run.mismatch_count = mismatches
        run.mismatch_sample = json.dumps(
            tgt_res["rows"][:5] if mismatches > 0 and tgt_res["rows"] else [],
            default=str
        )
        run.execution_time_ms = elapsed

        if src_res.get("error") or tgt_res.get("error"):
            run.status = "error"
            chosen_error = src_res.get("error") or tgt_res.get("error")
            run.error_message = chosen_error
            try:
                err_payload = {"detail": chosen_error}
                if src_res.get("error") and connectors.get("src") and test.source_query:
                    err_payload = _build_oracle_error_payload(src_res.get("error"), test.source_query, connectors.get("src"))
                elif tgt_res.get("error") and connectors.get("tgt") and test.target_query:
                    err_payload = _build_oracle_error_payload(tgt_res.get("error"), test.target_query, connectors.get("tgt"))
                run.actual_result = json.dumps(err_payload, default=str)
            except Exception:
                pass

    except asyncio.CancelledError:
        # task.cancel() was called (Stop Execution). Mark run as stopped and let the
        # batch loop's stopped-flag check terminate the batch cleanly.
        run.status = "error"
        run.error_message = "Execution stopped by user"
        # Do NOT re-raise – run_test returns normally so the batch loop可以 check the
        # stopped flag and break out cleanly.
    except Exception as e:
        run.status = "error"
        run.error_message = str(e)
        logger.exception("Test execution error")
    finally:
        for c in connectors.values():
            try:
                c.disconnect()
            except Exception:
                pass

    await db.commit()
    return run


async def run_all_tests(db: AsyncSession, test_ids: Optional[List[int]] = None, max_concurrent: int = 2) -> dict:
    """Run multiple tests. Uses sequential DB writes to keep AsyncSession usage safe."""
    batch_id = str(uuid.uuid4())[:12]
    if test_ids:
        tests_r = await db.execute(select(TestCase).where(TestCase.id.in_(test_ids)))
        tests = tests_r.scalars().all()
    else:
        tests_r = await db.execute(select(TestCase).where(TestCase.is_active == True))
        all_tests = tests_r.scalars().all()
        executed_ids_r = await db.execute(select(TestRun.test_case_id).distinct())
        executed_ids = {row[0] for row in executed_ids_r.all() if row and row[0] is not None}
        tests = [t for t in all_tests if t.id not in executed_ids]

    summary = {"batch_id": batch_id, "total": len(tests), "passed": 0, "failed": 0, "error": 0, "skipped_executed": 0}
    if not test_ids:
        summary["skipped_executed"] = max(0, len(all_tests) - len(tests))

    # Phase 7.16 perf round-2 fix: hardcoded 3s inter-test sleep wasted
    # ~3*N seconds per batch with no throttle reason documented.  Made
    # configurable via env var `TEST_EXECUTOR_INTER_TEST_DELAY_S`
    # (default 0).  Operator can re-enable if a rate-limit scenario
    # surfaces.
    inter_test_delay_s = float(os.environ.get("TEST_EXECUTOR_INTER_TEST_DELAY_S", "0"))
    for idx, test in enumerate(tests):
        run = await run_test(db, test.id, batch_id)
        summary[run.status] = summary.get(run.status, 0) + 1
        if inter_test_delay_s > 0 and idx < len(tests) - 1:
            await asyncio.sleep(inter_test_delay_s)

    return summary
