"""Validate the fixed SQL using check-insert without executing, to get EXPLAIN PLAN error."""
import requests

BASE = "http://127.0.0.1:8550"
DS = 3

with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

print(f"SQL chars: {len(sql)}, lines: {len(sql.splitlines())}")

# validate-only (no execute)
r = requests.post(
    f"{BASE}/api/tests/control-table/check-insert",
    json={
        "target_datasource_id": DS,
        "sql": sql,
        "execute": False,
    },
    timeout=60,
)
print(f"status: {r.status_code}")
d = r.json()
print(f"ok: {d.get('ok')}")
print(f"mode: {d.get('mode')}")
print(f"error: {str(d.get('error',''))[:600]}")
print(f"message: {d.get('message','')}")
if d.get("diagnostics"):
    diag = d["diagnostics"]
    mc = diag.get("missing_columns", [])
    mt = diag.get("missing_tables", [])
    print(f"missing_tables: {mt}")
    print(f"missing_columns ({len(mc)}): {mc}")

# Also try sending just the SELECT subquery to check if SELECT is valid
print("\n=== Test: wrap SELECT in COUNT to validate syntax ===")
# Extract just the SELECT clause from the INSERT
lines = sql.splitlines()
# Find the SELECT line
sel_start = None
for i, l in enumerate(lines):
    if l.strip().upper().startswith("SELECT"):
        sel_start = i
        break

if sel_start is not None:
    select_sql = "\n".join(lines[sel_start:])
    count_sql = f"SELECT COUNT(*) AS CNT FROM (\n{select_sql}\n) WHERE ROWNUM <= 1"
    print(f"COUNT wrapper SQL: {len(count_sql)} chars, first 200: {count_sql[:200]}")
    r2 = requests.post(
        f"{BASE}/api/datasources/{DS}/query",
        json={"sql": count_sql, "row_limit": 1},
        timeout=120,
    )
    print(f"status: {r2.status_code}")
    d2 = r2.json()
    print(f"error: {str(d2.get('error',''))[:400]}")
    print(f"rows: {d2.get('rows', [])}")
