"""Execute the DRD full INSERT against LH (DS=3) via check-insert."""
import requests, json

BASE = "http://127.0.0.1:8550"
DS = 3

# Load generated SQL
with open("data/drd_full_insert.sql", encoding="utf-8") as f:
    insert_sql = f.read()

print(f"SQL: {len(insert_sql):,} chars, {insert_sql.count(chr(10))} lines")

# Step 1: TRUNCATE
print("\n[1] TRUNCATE IKOROSTELEV.AVY_FACT_SIDE …")
r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                  json={"sql": "TRUNCATE TABLE IKOROSTELEV.AVY_FACT_SIDE", "row_limit": 1},
                  timeout=30)
print(f"    status={r.status_code}")

cnt_rows, _ = None, None
r_cnt = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": "SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE", "row_limit": 1},
                      timeout=30)
cnt = (r_cnt.json().get("rows") or [{"CNT": -1}])[0].get("CNT", -1)
print(f"    post-truncate count = {cnt}")
assert int(cnt) == 0, "Table not empty after TRUNCATE"

# Step 2: Execute INSERT via check-insert
print("\n[2] Execute DRD INSERT via check-insert …")
r2 = requests.post(
    f"{BASE}/api/tests/control-table/check-insert",
    json={"target_datasource_id": DS, "sql": insert_sql, "execute": True},
    timeout=180,
)
d2 = r2.json()
print(f"    status={r2.status_code}, ok={d2.get('ok')}")
if not d2.get("ok"):
    err = d2.get("error", "")
    print(f"    ERROR: {str(err)[:400]}")
    # Print first 20 lines of SQL context around possible error
    if "ORA-" in str(err):
        import re
        line_match = re.search(r"line (\d+)", str(err), re.IGNORECASE)
        if line_match:
            err_line = int(line_match.group(1))
            lines = insert_sql.split("\n")
            print(f"\n    Context around line {err_line}:")
            for i in range(max(0, err_line-5), min(len(lines), err_line+5)):
                print(f"    {i+1:4}: {lines[i]}")
    raise SystemExit(1)

# Step 3: Verify count
print("\n[3] Verify row count …")
r3 = requests.post(f"{BASE}/api/datasources/{DS}/query",
                   json={"sql": "SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE", "row_limit": 1},
                   timeout=30)
cnt_after = (r3.json().get("rows") or [{"CNT": 0}])[0].get("CNT", 0)
print(f"    Rows inserted: {cnt_after}")
assert int(cnt_after) >= 1, f"Expected ≥1 rows, got {cnt_after}"

# Step 4: Sample rows
print("\n[4] Sample 3 rows (key columns) …")
r4 = requests.post(f"{BASE}/api/datasources/{DS}/query",
                   json={"sql": """SELECT TXN_ID, TD, AR_DIM_ID, CRT_USR_NM, ACTV_F,
                                          SHDW_TXN_TP_CD, IMP_SRC_ACTN_CD
                                   FROM IKOROSTELEV.AVY_FACT_SIDE
                                   WHERE ROWNUM <= 3""",
                         "row_limit": 3},
                   timeout=30)
for row in r4.json().get("rows", []):
    print(f"    {row}")

print(f"\n{'='*60}")
print(f"DRD FULL INSERT: {cnt_after} rows in IKOROSTELEV.AVY_FACT_SIDE")
print(f"{'='*60}")
