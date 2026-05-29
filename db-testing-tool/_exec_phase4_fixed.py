"""Fix generated SQL bugs and execute INSERT with ROWNUM <= 10."""
import json, re, requests

BASE = "http://127.0.0.1:8550"
DS = 3

with open(r"C:\GIT_Repo\db-testing-tool\data\phase2_analyze_result.json") as f:
    d = json.load(f)

sql = d["generated_sql"]

# Strip TRUNCATE prefix line
lines = sql.split("\n")
if lines[0].strip().upper().startswith("TRUNCATE"):
    lines = lines[1:]
sql = "\n".join(lines)

print(f"SQL before fixes: {len(sql)} chars")

# Fix 1: Rogue semicolon on line "AND T.TD < FA.END_DT;"
sql = sql.replace("AND T.TD < FA.END_DT;", "AND T.TD < FA.END_DT")
print(f"Fix 1 (rogue semicolon) applied")

# Fix 2: Inline comment on CL_SCM_ID = '99' line
# Replace the whole comment-bearing line with just the condition
sql = re.sub(
    r"AND CV\.CL_SCM_ID = '99'\s+\([^)]+\)",
    "AND CV.CL_SCM_ID = '99'",
    sql,
    flags=re.DOTALL
)
print("Fix 2 (inline comment) applied")

# Fix 3: Space in table name NET_NEW _AST_CGY
sql = sql.replace("CCAL_REPL_OWNER.NET_NEW _AST_CGY", "CCAL_REPL_OWNER.NET_NEW_AST_CGY")
print("Fix 3 (table name space) applied")

# Verify no more rogue semicolons (only the final one should remain)
semi_lines = [(i+1, l) for i, l in enumerate(sql.split("\n")) if ";" in l]
print(f"Remaining semicolons ({len(semi_lines)}):")
for ln, l in semi_lines:
    print(f"  line {ln}: {l[:80]}")

# Add ROWNUM <= 10 before the final semicolon
sql_limited = sql.rstrip()
if sql_limited.endswith(";"):
    sql_limited = sql_limited[:-1].rstrip()
sql_limited = sql_limited + "\nWHERE ROWNUM <= 10"

print(f"\nSQL after fixes: {len(sql_limited)} chars")
print(f"Last 100 chars: ...{sql_limited[-100:]!r}")

# Save the fixed SQL
fixed_path = r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql"
with open(fixed_path, "w") as f:
    f.write(sql_limited)
print(f"Fixed SQL saved to {fixed_path}")

# Execute via /api/datasources/3/query
print("\n=== Phase 4.2: Execute INSERT via check-insert ===")
r = requests.post(
    f"{BASE}/api/tests/control-table/check-insert",
    json={
        "target_datasource_id": DS,
        "sql": sql_limited,
        "execute": True,
    },
    timeout=120,
)
print(f"status: {r.status_code}")
d_resp = r.json()
print(f"keys: {list(d_resp.keys())}")
print(f"ok: {d_resp.get('ok')}")
print(f"error: {str(d_resp.get('error',''))[:400]}")
rows_affected = d_resp.get("rows_affected", 0)
print(f"rows_affected: {rows_affected}")
if d_resp.get("diagnostics"):
    diag = d_resp["diagnostics"]
    mc = diag.get("missing_columns", [])
    print(f"missing_columns ({len(mc)}): {[c['column'] for c in mc[:10]]}")
if d_resp.get("auto_fixed_sql"):
    print(f"auto_fixed_sql available ({len(d_resp['auto_fixed_sql'])} chars)")

# If check-insert failed, try raw query endpoint
if d_resp.get("error") and rows_affected == 0:
    print("\n=== Fallback: direct /api/datasources/3/query ===")
    r2 = requests.post(
        f"{BASE}/api/datasources/{DS}/query",
        json={"sql": sql_limited, "row_limit": 1},
        timeout=120,
    )
    print(f"status: {r2.status_code}")
    d2 = r2.json()
    print(f"error: {str(d2.get('error',''))[:400]}")
    print(f"rows_affected: {d2.get('total_rows_affected', d2.get('rows_affected', 0))}")

# Phase 4.3: Verify count
print("\n=== Phase 4.3: Verify row count ===")
r3 = requests.post(
    f"{BASE}/api/datasources/{DS}/query",
    json={"sql": "SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE", "row_limit": 1},
    timeout=30,
)
d3 = r3.json()
cnt = int((d3.get("rows") or [{"CNT": 0}])[0].get("CNT", 0))
print(f"COUNT(*) = {cnt}")
if cnt > 0:
    print("PASS R4: Real data rows loaded")
else:
    print("FAIL R4: 0 rows — check error above")
