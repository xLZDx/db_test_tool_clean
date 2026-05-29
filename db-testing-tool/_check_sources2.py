"""Quick checks with ROWNUM <= 1 to avoid timeouts on large tables."""
import requests, json

BASE = "http://127.0.0.1:8550"
DS = 3

def qry(sql, label, timeout=90):
    r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": sql, "row_limit": 5}, timeout=timeout)
    if r.status_code == 500:
        detail = r.json().get("detail", {})
        return None, str(detail.get("error",""))[:150]
    d = r.json()
    return d.get("rows", []), None

# Quick existence check
print("=== Quick row existence checks (ROWNUM <= 1) ===")
for tbl in ["CCAL_REPL_OWNER.TXN", "CCAL_REPL_OWNER.APA", "CCAL_REPL_OWNER.TXN_RLTNP"]:
    rows, err = qry(f"SELECT 1 AS X FROM {tbl} WHERE ROWNUM <= 1", f"exists {tbl}", timeout=90)
    if err:
        print(f"  {tbl}: ERROR {err}")
    else:
        print(f"  {tbl}: {'HAS ROWS' if rows else 'EMPTY'}")

# Get AVY_FACT_SIDE column structure (fast)
print("\n=== IKOROSTELEV.AVY_FACT_SIDE columns (ALL_TAB_COLUMNS) ===")
rows2, err2 = qry("""
SELECT COLUMN_NAME, DATA_TYPE, NULLABLE, DATA_DEFAULT, COLUMN_ID
FROM ALL_TAB_COLUMNS
WHERE OWNER = 'IKOROSTELEV' AND TABLE_NAME = 'AVY_FACT_SIDE'
ORDER BY COLUMN_ID
""", "col structure", timeout=30)
if err2:
    print(f"ERROR: {err2}")
else:
    print(f"Total columns: {len(rows2)}")
    not_null = [r['COLUMN_NAME'] for r in rows2 if r.get('NULLABLE') == 'N']
    nullable = [r['COLUMN_NAME'] for r in rows2 if r.get('NULLABLE') == 'Y']
    print(f"NOT NULL ({len(not_null)}): {not_null}")
    print(f"Nullable ({len(nullable)}): {nullable[:10]}...")
    with open(r"C:\GIT_Repo\db-testing-tool\data\avyfactside_cols.json", "w") as f:
        json.dump(rows2, f, indent=2)
    print("Saved to data/avyfactside_cols.json")

# Get sample TXN row
print("\n=== Sample TXN row (quick) ===")
rows3, err3 = qry(
    "SELECT TXN_ID, TD, SD, AR_ID, ACTV_F FROM CCAL_REPL_OWNER.TXN WHERE ROWNUM <= 2",
    "txn sample", timeout=90
)
if err3:
    print(f"ERROR: {err3}")
else:
    print(f"Sample TXN rows: {rows3}")
