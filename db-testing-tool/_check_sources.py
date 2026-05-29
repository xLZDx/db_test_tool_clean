"""Check source table availability and get table structure for IKOROSTELEV.AVY_FACT_SIDE."""
import requests

BASE = "http://127.0.0.1:8550"
DS = 3

def qry(sql, label, timeout=30):
    r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": sql, "row_limit": 5}, timeout=timeout)
    if r.status_code == 500:
        detail = r.json().get("detail", {})
        return None, str(detail.get("error",""))[:150]
    d = r.json()
    return d.get("rows", []), None

# Check main source tables
tables = [
    "CCAL_REPL_OWNER.TXN",
    "CCAL_REPL_OWNER.APA",
    "CCAL_REPL_OWNER.TXN_RLTNP",
]
print("=== Source table row counts ===")
for tbl in tables:
    rows, err = qry(f"SELECT COUNT(*) AS CNT FROM {tbl}", f"count {tbl}")
    if err:
        print(f"  {tbl}: ERROR {err}")
    else:
        print(f"  {tbl}: {rows}")

# Get AVY_FACT_SIDE columns with nullable info  
print("\n=== IKOROSTELEV.AVY_FACT_SIDE column structure ===")
rows, err = qry("""
SELECT COLUMN_NAME, DATA_TYPE, NULLABLE, DATA_DEFAULT
FROM ALL_TAB_COLUMNS
WHERE OWNER = 'IKOROSTELEV' AND TABLE_NAME = 'AVY_FACT_SIDE'
ORDER BY COLUMN_ID
""", "col structure", timeout=30)
if err:
    print(f"ERROR: {err}")
else:
    not_null = [r['COLUMN_NAME'] for r in rows if r.get('NULLABLE') == 'N']
    nullable = [r['COLUMN_NAME'] for r in rows if r.get('NULLABLE') != 'N']
    print(f"Total columns: {len(rows)}")
    print(f"NOT NULL ({len(not_null)}): {not_null}")
    print(f"Nullable ({len(nullable)}): {nullable[:20]}...")

    # Save for use
    import json
    with open(r"C:\GIT_Repo\db-testing-tool\data\avyfactside_cols.json", "w") as f:
        json.dump(rows, f, indent=2)
    print("Saved to data/avyfactside_cols.json")

# Get sample row from TXN  
print("\n=== Sample TXN row ===")
rows2, err2 = qry("SELECT TXN_ID, TXN_SRC_KEY, TD, SD, AR_ID, ACTV_F FROM CCAL_REPL_OWNER.TXN WHERE ROWNUM <= 2", "txn sample")
if err2:
    print(f"ERROR: {err2}")
else:
    print(f"Sample TXN rows: {rows2}")
