"""Phases 3-5 execution: CREATE TABLE, INSERT, Validation."""
import json, sys, os, requests

BASE = "http://127.0.0.1:8550"
DS   = 3
ANALYZE_RESULT = r"C:\GIT_Repo\db-testing-tool\data\phase2_analyze_result.json"
VALIDATION_SQL = r"C:\GIT_Repo\db-testing-tool\reports\avyfactside_validation.sql"
CREATE_SQL_FILE = r"C:\GIT_Repo\db-testing-tool\reports\avyfactside_create_ikorostelev.sql"

def query(sql, label="", row_limit=100, timeout=120):
    r = requests.post(
        f"{BASE}/api/datasources/{DS}/query",
        json={"sql": sql, "row_limit": row_limit},
        timeout=timeout,
    )
    data = r.json()
    err = data.get("error") or ""
    rows_affected = data.get("rows_affected", 0)
    rows = data.get("rows", [])
    row_count = data.get("row_count", len(rows))
    print(f"  [{label}] status={r.status_code} rows_affected={rows_affected} row_count={row_count} error={err[:200] if err else 'None'}")
    if rows:
        print(f"    sample row: {rows[0]}")
    return data

# ============================================================
# Phase 3.1 — Drop if exists
# ============================================================
print("\n=== Phase 3.1: DROP TABLE IF EXISTS ===")
query(
    "BEGIN EXECUTE IMMEDIATE 'DROP TABLE IKOROSTELEV.AVY_FACT_SIDE PURGE'; EXCEPTION WHEN OTHERS THEN NULL; END;",
    "3.1 DROP"
)

# ============================================================
# Phase 3.2 — CREATE TABLE via CTAS
# ============================================================
print("\n=== Phase 3.2: CREATE TABLE (CTAS) ===")
create_sql = "CREATE TABLE IKOROSTELEV.AVY_FACT_SIDE AS SELECT * FROM TRANSACTIONS_OWNER.AVY_FACT_SIDE WHERE 1!=1"
r32 = query(create_sql, "3.2 CREATE TABLE")
if r32.get("error"):
    print("  FATAL: CREATE TABLE failed — stopping Phase 3")
    sys.exit(2)

# ============================================================
# Phase 3.3 — Pre-flight: source table has rows
# ============================================================
print("\n=== Phase 3.3: Source row count pre-flight ===")
r33 = query(
    "SELECT COUNT(*) AS CNT FROM TRANSACTIONS_OWNER.AVY_FACT_SIDE WHERE ROWNUM <= 1",
    "3.3 SOURCE COUNT"
)
rows33 = r33.get("rows", [])
if not rows33 or int(rows33[0].get("CNT", 0)) == 0:
    print("  WARNING: Source table may be empty — R4 at risk, but continuing")
else:
    print("  PASS: Source table has rows")

# ============================================================
# Phase 3.4 — Verify target is empty
# ============================================================
print("\n=== Phase 3.4: Verify target table is empty ===")
r34 = query("SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE", "3.4 TARGET COUNT")
cnt34 = int((r34.get("rows") or [{"CNT": -1}])[0].get("CNT", -1))
print(f"  Target row count = {cnt34}")
if cnt34 == 0:
    print("  PASS: Table exists and is empty")
elif cnt34 > 0:
    print("  NOTE: Table already has rows — truncating")
    query("TRUNCATE TABLE IKOROSTELEV.AVY_FACT_SIDE", "3.4 TRUNCATE")
else:
    print("  ERROR: Could not verify table exists")
    sys.exit(2)

# ============================================================
# Phase 4.1 — Load generated_sql and inject ROWNUM inside SELECT
# ============================================================
print("\n=== Phase 4.1: Load and adapt generated INSERT SQL ===")
with open(ANALYZE_RESULT) as f:
    analyze_data = json.load(f)

gen_sql = analyze_data.get("generated_sql", "")
if not gen_sql or "INSERT" not in gen_sql.upper():
    print("  FATAL: No INSERT SQL in analyze result")
    sys.exit(3)

# Strip the TRUNCATE prefix (we already truncated)
if gen_sql.upper().startswith("TRUNCATE"):
    lines = gen_sql.split("\n")
    gen_sql = "\n".join(lines[1:]).lstrip()

print(f"  INSERT SQL length: {len(gen_sql)} chars")

# Inject ROWNUM <= 10 INSIDE the FROM clause (after last JOIN, before closing paren if subquery)
# Strategy: find the last WHERE clause in the SQL and add ROWNUM condition
import re
# Find the main WHERE clause (last WHERE in the SQL that's part of the outer SELECT)
# Approach: inject before ORDER BY or at end if no WHERE exists
if re.search(r'\bWHERE\b', gen_sql, re.IGNORECASE):
    # Add ROWNUM to the first WHERE clause
    insert_sql_limited = re.sub(
        r'\bWHERE\b',
        'WHERE ROWNUM <= 10 AND',
        gen_sql,
        count=1,
        flags=re.IGNORECASE
    )
else:
    # No WHERE — find FROM and add after the last table reference
    insert_sql_limited = gen_sql.rstrip().rstrip(";") + "\nWHERE ROWNUM <= 10"

print(f"  Limited SQL (first 300 chars): {insert_sql_limited[:300]}")
print(f"  Contains ROWNUM: {'ROWNUM' in insert_sql_limited.upper()}")

# ============================================================
# Phase 4.2 — Execute INSERT via check-insert endpoint
# ============================================================
print("\n=== Phase 4.2: Execute INSERT via /api/tests/control-table/check-insert ===")
r42 = requests.post(
    f"{BASE}/api/tests/control-table/check-insert",
    json={
        "target_datasource_id": DS,
        "sql": insert_sql_limited,
        "execute": True,
    },
    timeout=120,
)
print(f"  check-insert status: {r42.status_code}")
d42 = r42.json()
print(f"  check-insert keys: {list(d42.keys())}")
rows_affected = d42.get("rows_affected", d42.get("row_count", 0))
check_error = d42.get("error") or d42.get("errors") or ""
print(f"  rows_affected: {rows_affected}")
print(f"  error: {check_error[:300] if check_error else 'None'}")
if d42.get("diagnostics"):
    print(f"  diagnostics: {json.dumps(d42['diagnostics'], default=str)[:300]}")

# ============================================================
# Phase 4.3 — Verify COUNT > 0
# ============================================================
print("\n=== Phase 4.3: Verify INSERT loaded rows ===")
r43 = query("SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE", "4.3 COUNT CHECK")
cnt43 = int((r43.get("rows") or [{"CNT": 0}])[0].get("CNT", 0))
print(f"  Final row count in IKOROSTELEV.AVY_FACT_SIDE = {cnt43}")
if cnt43 > 0:
    print("  PASS R4: Real data rows loaded")
else:
    print("  FAIL R4: No rows loaded — check check-insert error above")

# ============================================================
# Phase 5.1 — Run validation queries
# ============================================================
print("\n=== Phase 5.1: Validation queries ===")
with open(VALIDATION_SQL) as f:
    val_sql = f.read()

# Run just the first meaningful SELECT from the validation file (COUNT check)
val_queries = [q.strip() for q in val_sql.split(";") if q.strip() and q.strip().upper().startswith("SELECT")]
for i, vq in enumerate(val_queries[:5], 1):
    print(f"\n  Validation query {i}: {vq[:80]}...")
    rv = query(vq, f"5.1 VAL-{i}", row_limit=5)

# ============================================================
# Phase 5.2 — Save combined SQL artifact
# ============================================================
print("\n=== Phase 5.2: Save combined e2e artifact ===")
combined_path = r"C:\GIT_Repo\db-testing-tool\reports\AVY_FACT_SIDE_complete_e2e.sql"
with open(combined_path, "w") as out:
    out.write("-- ============================================================\n")
    out.write("-- AVY_FACT_SIDE Complete E2E SQL\n")
    out.write(f"-- Generated: 2026-05-20\n")
    out.write("-- Target: IKOROSTELEV.AVY_FACT_SIDE on datasource LH (id=3)\n")
    out.write("-- DRD: DRD_Activity_Fact.xlsx sheet 'Table-View (2)'\n")
    out.write("-- ============================================================\n\n")
    out.write("-- === Phase 3: CREATE TABLE ===\n")
    out.write("BEGIN EXECUTE IMMEDIATE 'DROP TABLE IKOROSTELEV.AVY_FACT_SIDE PURGE'; EXCEPTION WHEN OTHERS THEN NULL; END;\n/\n\n")
    out.write(f"{create_sql};\n\n")
    out.write("-- === Phase 4: INSERT (with ROWNUM <= 10 for testing) ===\n")
    out.write(f"{insert_sql_limited};\n\n")
    out.write("-- === Phase 5: Validation ===\n")
    out.write(val_sql)

print(f"  Saved to {combined_path}")

print("\n=== Phases 3-5 COMPLETE ===")
print(f"  CREATE TABLE: OK")
print(f"  Source rows:  {'OK' if rows33 and int(rows33[0].get('CNT', 0)) > 0 else 'CHECK NEEDED'}")
print(f"  INSERT rows:  {cnt43}")
print(f"  R4 pass:      {'YES' if cnt43 > 0 else 'NO'}")
