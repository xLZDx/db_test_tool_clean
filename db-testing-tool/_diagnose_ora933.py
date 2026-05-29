"""Extract actual Oracle error from query endpoint 500 response."""
import requests

BASE = "http://127.0.0.1:8550"
DS = 3

with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

lines = sql.splitlines()
sel_start = next(i for i, l in enumerate(lines) if l.strip().upper().startswith("SELECT"))
select_sql = "\n".join(lines[sel_start:])

# Test 1: SELECT subquery wrapped in COUNT
count_sql = f"SELECT COUNT(*) AS CNT FROM (\n{select_sql}\n)"
r = requests.post(
    f"{BASE}/api/datasources/{DS}/query",
    json={"sql": count_sql, "row_limit": 1},
    timeout=120,
)
d = r.json()
print(f"COUNT wrapper - status={r.status_code}")
if r.status_code == 500:
    detail = d.get("detail", {})
    print(f"  error: {str(detail.get('error', ''))[:400]}")
    print(f"  line: {detail.get('line')}")
    print(f"  statement_preview: {str(detail.get('statement_preview', ''))[:200]}")
else:
    print(f"  rows: {d.get('rows', [])}")

# Test 2: Minimal INSERT to see if syntax is valid
minimal_insert = """INSERT INTO IKOROSTELEV.AVY_FACT_SIDE_MINI_TEST (ID)
SELECT 1 AS ID FROM DUAL WHERE ROWNUM <= 1"""
r2 = requests.post(
    f"{BASE}/api/datasources/{DS}/query",
    json={"sql": minimal_insert, "row_limit": 1},
    timeout=30,
)
d2 = r2.json()
print(f"\nMinimal INSERT test - status={r2.status_code}")
if r2.status_code == 500:
    detail2 = d2.get("detail", {})
    print(f"  error: {detail2.get('error', '')}")
else:
    print(f"  rows: {d2.get('rows', [])}, total_rows_affected: {d2.get('total_rows_affected',0)}")

# Test 3: EXPLAIN PLAN FOR the INSERT
explain_sql = f"EXPLAIN PLAN FOR\n{sql}"
r3 = requests.post(
    f"{BASE}/api/datasources/{DS}/query",
    json={"sql": explain_sql, "row_limit": 1},
    timeout=60,
)
d3 = r3.json()
print(f"\nEXPLAIN PLAN - status={r3.status_code}")
if r3.status_code == 500:
    detail3 = d3.get("detail", {})
    print(f"  error: {str(detail3.get('error', ''))[:500]}")
    print(f"  line: {detail3.get('line')}")
else:
    print(f"  rows: {d3.get('rows', [])}")

# Test 4: check what the server thinks of the SQL structure
# Test INSERT with just first 20 columns and minimal source
sample_sql = """INSERT INTO IKOROSTELEV.AVY_FACT_SIDE (
    EXG_DIM_ID,
    AR_DIM_ID
)
SELECT
    1 AS EXG_DIM_ID,
    2 AS AR_DIM_ID
FROM CCAL_REPL_OWNER.TXN TXN WHERE ROWNUM <= 1"""
r4 = requests.post(
    f"{BASE}/api/datasources/{DS}/query",
    json={"sql": sample_sql, "row_limit": 1},
    timeout=30,
)
d4 = r4.json()
print(f"\nSample INSERT (2 cols from TXN) - status={r4.status_code}")
if r4.status_code == 500:
    detail4 = d4.get("detail", {})
    print(f"  error: {detail4.get('error', '')}")
else:
    print(f"  rows: {d4.get('rows', [])}, total_rows_affected: {d4.get('total_rows_affected',0)}")
