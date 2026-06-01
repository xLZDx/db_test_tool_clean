"""Step 5: generate verification SQL between {snap_drd}
(populated via DRD-driven INSERT in Step 2) and IKOROSTELEV.AVY_FACT_SIDE_ODI
(populated via ODI-driven INSERT in Step 3), plus comparison against
TRANSACTIONS_OWNER.AVY_FACT (production target).

Tests generated:
  1. Row-count parity (DRD vs ODI)
  2. Per-column NULL count parity (DRD vs ODI)
  3. Per-column value drift (joined on PK = TXN_ID)
  4. Symmetric MINUS (DRD vs ODI)
  5. CT-vs-TARGET row count (DRD vs TRANSACTIONS_OWNER target)
  6. PK overlap (the 500 TXN_IDs we loaded exist in production target)

Each test is RUN live and the result captured.  Then saved as a new
test suite in `data/test_suites/AVY_FACT_SIDE_DRD_VS_ODI_LIVE.json`
in the standard tool format (the GUI can browse/re-run via the
existing Tests page).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ora_exec(conn, sql: str, *, commit: bool = False, fetch: bool = False):
    cur = conn.cursor()
    try:
        cur.execute(sql.rstrip().rstrip(";").rstrip())
        rc = cur.rowcount or 0
        rows = cur.fetchall() if fetch and cur.description else []
        if commit:
            conn.commit()
        return True, rc, rows, None
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return False, 0, [], str(e)
    finally:
        cur.close()


def main() -> int:
    # Phase 7.18 generic CLI + env config (see scripts/_step_config.py).
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _step_config import (
        add_common_args, parse_args_to_config, open_connection,
        print_config_banner,
    )
    parser = argparse.ArgumentParser(
        description="Step 5: DRD-vs-ODI verification suite (generic)."
    )
    add_common_args(parser)
    cfg = parse_args_to_config(parser)
    print_config_banner(cfg, "Step 5: verification suite")
    conn = open_connection(cfg)

    snap_schema = cfg.snapshot_schema
    snap_drd = f"{snap_schema}.{cfg.snapshot_table_drd}"
    snap_odi = f"{snap_schema}.{cfg.snapshot_table_odi}"

    # Discover PK column generically (same logic as step3)
    cur = conn.cursor()
    cur.execute(
        "SELECT cc.column_name FROM all_constraints c "
        "JOIN all_cons_columns cc ON c.owner=cc.owner AND c.constraint_name=cc.constraint_name "
        "WHERE c.owner=:o AND c.table_name=:t AND c.constraint_type='P' "
        "ORDER BY cc.position",
        o=cfg.target_schema, t=cfg.target_table,
    )
    pk_rows = cur.fetchall()
    cur.close()
    pk_col = pk_rows[0][0].upper() if pk_rows else None
    if pk_col is None:
        # Fall back to first column of snapshot table; warn loudly.
        print(f"   WARN: no PK on {cfg.target_schema}.{cfg.target_table}; "
              "using first column of snapshot for joins")
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM all_tab_columns WHERE owner=:o "
            "AND table_name=:t AND column_id=1",
            o=snap_schema, t=cfg.snapshot_table_drd,
        )
        r = cur.fetchone()
        cur.close()
        pk_col = r[0].upper() if r else "ROWID"

    tests: list[dict] = []
    tid = 0

    def add_test(category: str, title: str, sql: str, expected: str,
                 actual: str, passed: bool, note: str = "") -> None:
        nonlocal tid
        tid += 1
        tests.append({
            "id": tid,
            "category": category,
            "title": title,
            "description": note,
            "expected_result": expected,
            "actual_result": actual,
            "passed": passed,
            "sql_validation": sql,
        })

    # ── Test 1: row count parity ────────────────────────────────────────
    print("1) Row-count parity DRD vs ODI...")
    sql1 = (
        f"SELECT "
        f"(SELECT COUNT(*) FROM {snap_drd}) AS drd_count, "
        f"(SELECT COUNT(*) FROM {snap_odi}) AS odi_count "
        f"FROM dual"
    )
    ok, _, rows, err = _ora_exec(conn, sql1, fetch=True)
    if not ok:
        print(f"   FAIL: {err}")
        return 1
    drd_c, odi_c = rows[0]
    passed = (drd_c == odi_c == cfg.row_limit)
    print(f"   DRD={drd_c}, ODI={odi_c} -- {'PASS' if passed else 'FAIL'}")
    add_test(
        category="row count parity",
        title=f"DRD row count == ODI row count == {cfg.row_limit}",
        sql=sql1,
        expected=f"{cfg.row_limit} / {cfg.row_limit}",
        actual=f"{drd_c} / {odi_c}",
        passed=passed,
    )

    # ── Test 2: per-column NULL count parity ────────────────────────────
    print("\n2) Per-column NULL parity (top 10 mismatches)...")
    ok, _, cols, _ = _ora_exec(conn, (
        "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS "
        f"WHERE OWNER='{snap_schema}' AND TABLE_NAME='{cfg.snapshot_table_drd}' "
        "ORDER BY COLUMN_ID"
    ), fetch=True)
    col_names = [r[0] for r in cols]
    # Generic reserved-word quoting -- Oracle's full reserved set is large
    # but the column name comes from a real table so most are non-reserved.
    _ORA_RESERVED = {"CHECK", "ORDER", "GROUP", "SELECT", "FROM", "WHERE",
                     "TABLE", "INDEX", "LEVEL", "ROWNUM", "USER", "DATE",
                     "NUMBER", "VARCHAR", "VARCHAR2"}
    def _quote_if_reserved(c: str) -> str:
        return f'"{c}"' if c.upper() in _ORA_RESERVED else c
    union_parts = []
    for c in col_names:
        cq = _quote_if_reserved(c)
        union_parts.append(
            f"SELECT '{c}' AS col, "
            f"(SELECT COUNT(*) FROM {snap_drd} WHERE {cq} IS NULL) drd_null, "
            f"(SELECT COUNT(*) FROM {snap_odi} WHERE {cq} IS NULL) odi_null "
            "FROM dual"
        )
    null_parity_sql = (
        "SELECT col, drd_null, odi_null, ABS(drd_null - odi_null) AS diff FROM (\n"
        + "\nUNION ALL\n".join(union_parts)
        + "\n) WHERE drd_null <> odi_null ORDER BY diff DESC, col"
    )
    ok, _, rows, err = _ora_exec(conn, null_parity_sql, fetch=True)
    if not ok:
        print(f"   FAIL: {err[:200]}")
        return 1
    n_mismatches = len(rows)
    print(f"   {n_mismatches} columns differ in NULL count")
    for row in rows[:10]:
        print(f"     {row[0]}: DRD_NULL={row[1]} ODI_NULL={row[2]} diff={row[3]}")
    add_test(
        category="per-column NULL parity",
        title=f"NULL count per column equal between DRD and ODI ({n_mismatches} differ)",
        sql=null_parity_sql[:500] + "..." if len(null_parity_sql) > 500 else null_parity_sql,
        expected="0 columns with differing NULL count",
        actual=f"{n_mismatches} columns differ -- top: " + ", ".join(
            f"{r[0]}(diff={r[3]})" for r in rows[:5]
        ),
        passed=(n_mismatches == 0),
        note=f"When DRD-emitter and ODI-emitter project from the same {cfg.base_table} row set, NULL pattern should match.",
    )

    # ── Test 3: per-column value drift (joined on PK) ───────────────────
    print("\n3) Per-column value drift...")
    diff_parts = []
    for c in col_names:
        if c.upper() == pk_col.upper():
            continue
        cq = _quote_if_reserved(c)
        diff_parts.append(
            f"SELECT '{c}' AS col, COUNT(*) AS drift_rows "
            f"FROM {snap_drd} d "
            f"JOIN {snap_odi} o ON d.{pk_col} = o.{pk_col} "
            f"WHERE DECODE(d.{cq}, o.{cq}, 0, 1) = 1"
        )
    drift_sql = (
        "SELECT col, drift_rows FROM (\n"
        + "\nUNION ALL\n".join(diff_parts)
        + "\n) WHERE drift_rows > 0 ORDER BY drift_rows DESC, col"
    )
    ok, _, rows, err = _ora_exec(conn, drift_sql, fetch=True)
    if not ok:
        print(f"   FAIL: {err[:200]}")
        return 1
    n_drift = len(rows)
    print(f"   {n_drift} columns have any row-level drift")
    for row in rows[:10]:
        print(f"     {row[0]}: {row[1]} rows differ")
    add_test(
        category="per-column value drift",
        title=f"Per-column value equality DRD vs ODI ({n_drift} columns drift)",
        sql=drift_sql[:500] + "..." if len(drift_sql) > 500 else drift_sql,
        expected="0 columns with row-level drift",
        actual=f"{n_drift} columns drift -- top: " + ", ".join(
            f"{r[0]}({r[1]} rows)" for r in rows[:5]
        ),
        passed=(n_drift == 0),
        note=f"DECODE handles NULL==NULL as match.  Drift means DRD and ODI populate different values for this column on the same {pk_col}.",
    )

    # ── Test 4: symmetric MINUS (DRD - ODI / ODI - DRD) on PK ───────────
    print("\n4) Symmetric MINUS (PK only)...")
    sql4 = (
        f"SELECT 'DRD_NOT_IN_ODI' AS side, COUNT(*) AS n FROM ("
        f"SELECT {pk_col} FROM {snap_drd} MINUS "
        f"SELECT {pk_col} FROM {snap_odi}) "
        f"UNION ALL "
        f"SELECT 'ODI_NOT_IN_DRD', COUNT(*) FROM ("
        f"SELECT {pk_col} FROM {snap_odi} MINUS "
        f"SELECT {pk_col} FROM {snap_drd})"
    )
    ok, _, rows, err = _ora_exec(conn, sql4, fetch=True)
    if not ok:
        print(f"   FAIL: {err}")
        return 1
    minus_drd = rows[0][1]
    minus_odi = rows[1][1]
    passed = (minus_drd == 0 and minus_odi == 0)
    print(f"   DRD-ODI={minus_drd}, ODI-DRD={minus_odi} -- {'PASS' if passed else 'FAIL'}")
    add_test(
        category="PK overlap parity",
        title=f"DRD {pk_col} set == ODI {pk_col} set (symmetric MINUS)",
        sql=sql4,
        expected="0 / 0",
        actual=f"{minus_drd} / {minus_odi}",
        passed=passed,
    )

    # ── Test 5: PK overlap vs base table (data-existence sanity) ────────
    # Generic: check that the PKs we loaded actually exist in the base
    # production table (the source we emitted FROM).  Operator can override
    # the "production target" check via env DBT_PROD_TARGET_FQ (full qual).
    prod_target = os.environ.get("DBT_PROD_TARGET_FQ", f"{cfg.base_schema}.{cfg.base_table}")
    print(f"\n5) PK overlap vs {prod_target}...")
    sql5 = (
        f"SELECT COUNT(*) FROM {snap_drd} d "
        f"WHERE d.{pk_col} IN (SELECT {pk_col} FROM {prod_target})"
    )
    ok, _, rows, err = _ora_exec(conn, sql5, fetch=True)
    target_overlap = rows[0][0] if ok and rows else 0
    print(f"   {target_overlap} of {cfg.row_limit} PKs also present in {prod_target}")
    add_test(
        category="DRD vs TARGET",
        title=f"DRD {pk_col} present in {prod_target}",
        sql=sql5,
        expected=f"{cfg.row_limit} (all PKs should exist in target)",
        actual=f"{target_overlap}",
        passed=(target_overlap == cfg.row_limit),
        note=f"Production target stores rows by {pk_col}; our {cfg.row_limit} sample should all be present.",
    )

    # ── Save as test suite (standard tool format) ──────────────────────
    # Phase 7.18 generic: suite name + JSON filename derived from target
    # so this works for any (schema, table) pair, not just AVY_FACT_SIDE.
    suite_name = f"{cfg.target_table}_DRD_VS_ODI_LIVE"
    suite_file = f"{suite_name}.json"
    print(f"\n6) Save as test suite ({suite_name})...")
    suite = {
        "suite_name": suite_name,
        "pbi_id": "LIVE_TEST",
        "project": cfg.target_table,
        "description": (
            f"Live verification suite.  Compares {snap_drd} ({cfg.row_limit} "
            f"rows loaded via Step 2 DRD-driven INSERT) vs {snap_odi} (same "
            f"{cfg.row_limit} rows loaded via Step 3 ODI-driven INSERT, J$ "
            f"stripped, base from {cfg.base_schema}.{cfg.base_table}).  "
            "Generated by scripts/step5_verification_suite.py."
        ),
        "datasource": cfg.dsn,
        "datasource_user": cfg.user,
        "target_fq": f"{cfg.target_schema}.{cfg.target_table}",
        "base_fq": f"{cfg.base_schema}.{cfg.base_table}",
        "pk_col": pk_col,
        "row_limit": cfg.row_limit,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tests": tests,
        "_summary": {
            "total": len(tests),
            "passed": sum(1 for t in tests if t["passed"]),
            "failed": sum(1 for t in tests if not t["passed"]),
        },
    }
    out = ROOT / "data" / "test_suites" / suite_file
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(suite, indent=2, default=str), encoding="utf-8")
    print(f"   Saved {out.name}")
    print(f"   Tests: {suite['_summary']['total']} total, "
          f"{suite['_summary']['passed']} passed, "
          f"{suite['_summary']['failed']} failed")

    print("\n=== Step 5 DONE ===")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
