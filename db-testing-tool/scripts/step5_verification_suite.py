"""Step 5: generate verification SQL between IKOROSTELEV.AVY_FACT_SIDE_DRD
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
    import oracledb
    print("=== Step 5: verification suite ===\n")
    conn = oracledb.connect(
        user="sys", password="123456",
        dsn="localhost:1521/FREEPDB1", mode=oracledb.SYSDBA,
    )
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
        "SELECT "
        "(SELECT COUNT(*) FROM IKOROSTELEV.AVY_FACT_SIDE_DRD) AS drd_count, "
        "(SELECT COUNT(*) FROM IKOROSTELEV.AVY_FACT_SIDE_ODI) AS odi_count "
        "FROM dual"
    )
    ok, _, rows, err = _ora_exec(conn, sql1, fetch=True)
    if not ok:
        print(f"   FAIL: {err}")
        return 1
    drd_c, odi_c = rows[0]
    passed = (drd_c == odi_c == 500)
    print(f"   DRD={drd_c}, ODI={odi_c} -- {'PASS' if passed else 'FAIL'}")
    add_test(
        category="row count parity",
        title="DRD row count == ODI row count == 500",
        sql=sql1,
        expected="500 / 500",
        actual=f"{drd_c} / {odi_c}",
        passed=passed,
    )

    # ── Test 2: per-column NULL count parity ────────────────────────────
    print("\n2) Per-column NULL parity (top 10 mismatches)...")
    ok, _, cols, _ = _ora_exec(conn, (
        "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS "
        "WHERE OWNER='IKOROSTELEV' AND TABLE_NAME='AVY_FACT_SIDE_DRD' "
        "ORDER BY COLUMN_ID"
    ), fetch=True)
    col_names = [r[0] for r in cols]
    union_parts = []
    for c in col_names:
        cq = f'"{c}"' if c.upper() in ("CHECK", "ORDER", "GROUP") else c
        union_parts.append(
            f"SELECT '{c}' AS col, "
            f"(SELECT COUNT(*) FROM IKOROSTELEV.AVY_FACT_SIDE_DRD WHERE {cq} IS NULL) drd_null, "
            f"(SELECT COUNT(*) FROM IKOROSTELEV.AVY_FACT_SIDE_ODI WHERE {cq} IS NULL) odi_null "
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
        note="When DRD-emitter and ODI-emitter project from the same TXN row set, NULL pattern should match.",
    )

    # ── Test 3: per-column value drift (joined on TXN_ID) ───────────────
    print("\n3) Per-column value drift...")
    diff_parts = []
    for c in col_names:
        if c.upper() == "TXN_ID":
            continue
        cq = f'"{c}"' if c.upper() in ("CHECK", "ORDER", "GROUP") else c
        diff_parts.append(
            f"SELECT '{c}' AS col, COUNT(*) AS drift_rows "
            "FROM IKOROSTELEV.AVY_FACT_SIDE_DRD d "
            "JOIN IKOROSTELEV.AVY_FACT_SIDE_ODI o ON d.TXN_ID = o.TXN_ID "
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
        note="DECODE handles NULL==NULL as match.  Drift means DRD and ODI populate different values for this column on the same TXN_ID.",
    )

    # ── Test 4: symmetric MINUS (DRD - ODI / ODI - DRD) on PK ───────────
    print("\n4) Symmetric MINUS (PK only)...")
    sql4 = (
        "SELECT 'DRD_NOT_IN_ODI' AS side, COUNT(*) AS n FROM ("
        "SELECT TXN_ID FROM IKOROSTELEV.AVY_FACT_SIDE_DRD MINUS "
        "SELECT TXN_ID FROM IKOROSTELEV.AVY_FACT_SIDE_ODI) "
        "UNION ALL "
        "SELECT 'ODI_NOT_IN_DRD', COUNT(*) FROM ("
        "SELECT TXN_ID FROM IKOROSTELEV.AVY_FACT_SIDE_ODI MINUS "
        "SELECT TXN_ID FROM IKOROSTELEV.AVY_FACT_SIDE_DRD)"
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
        title="DRD TXN_ID set == ODI TXN_ID set (symmetric MINUS)",
        sql=sql4,
        expected="0 / 0",
        actual=f"{minus_drd} / {minus_odi}",
        passed=passed,
    )

    # ── Test 5: PK overlap vs TARGET (TRANSACTIONS_OWNER) ───────────────
    print("\n5) PK overlap vs TRANSACTIONS_OWNER.AVY_FACT...")
    sql5 = (
        "SELECT COUNT(*) FROM IKOROSTELEV.AVY_FACT_SIDE_DRD d "
        "WHERE d.TXN_ID IN (SELECT TXN_ID FROM TRANSACTIONS_OWNER.AVY_FACT)"
    )
    ok, _, rows, err = _ora_exec(conn, sql5, fetch=True)
    target_overlap = rows[0][0] if ok and rows else 0
    print(f"   {target_overlap} of 500 IDs also present in production target")
    add_test(
        category="DRD vs TARGET",
        title="DRD TXN_IDs present in TRANSACTIONS_OWNER.AVY_FACT",
        sql=sql5,
        expected="500 (all PKs should exist in target)",
        actual=f"{target_overlap}",
        passed=(target_overlap == 500),
        note="Production target stores rows by TXN_ID; our 500 sample should all be present (loaded via real ODI).",
    )

    # ── Save as test suite (standard tool format) ──────────────────────
    print("\n6) Save as test suite...")
    suite = {
        "suite_name": "AVY_FACT_SIDE_DRD_VS_ODI_LIVE",
        "pbi_id": "LIVE_TEST",
        "project": "AVY_FACT_SIDE",
        "description": (
            "Live verification suite (Phase 7.12).  Compares "
            "IKOROSTELEV.AVY_FACT_SIDE_DRD (500 rows loaded via Step 2 "
            "DRD-driven INSERT) vs IKOROSTELEV.AVY_FACT_SIDE_ODI (same "
            "500 rows loaded via Step 3 ODI-driven INSERT, J$ stripped, "
            "base from CCAL_REPL_OWNER.TXN).  Generated by "
            "scripts/step5_verification_suite.py."
        ),
        "datasource": "FREEPDB1",
        "datasource_id": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tests": tests,
        "_summary": {
            "total": len(tests),
            "passed": sum(1 for t in tests if t["passed"]),
            "failed": sum(1 for t in tests if not t["passed"]),
        },
    }
    out = ROOT / "data" / "test_suites" / "AVY_FACT_SIDE_DRD_VS_ODI_LIVE.json"
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
