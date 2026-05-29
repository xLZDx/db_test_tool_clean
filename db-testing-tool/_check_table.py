"""Execute SQL queries against datasource and print results."""
import requests, json, sys

BASE = "http://127.0.0.1:8550"
DS = 3  # LH

def query(sql, row_limit=20):
    r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": sql, "row_limit": row_limit}, timeout=60)
    r.raise_for_status()
    return r.json()

# Check if IKOROSTELEV.AVY_FACT_SIDE exists
res = query("SELECT COUNT(*) AS CNT FROM ALL_TABLES WHERE OWNER='IKOROSTELEV' AND TABLE_NAME='AVY_FACT_SIDE'")
cnt = res["rows"][0]["CNT"]
print(f"IKOROSTELEV.AVY_FACT_SIDE exists: {cnt > 0} (count={cnt})")

# Check source column count
res2 = query("SELECT COUNT(*) AS COL_CNT FROM ALL_TAB_COLUMNS WHERE OWNER='TRANSACTIONS_OWNER' AND TABLE_NAME='AVY_FACT_SIDE'")
print(f"TRANSACTIONS_OWNER.AVY_FACT_SIDE column count: {res2['rows'][0]['COL_CNT']}")

# Get source columns list
res3 = query("SELECT COLUMN_NAME, DATA_TYPE, NULLABLE, COLUMN_ID FROM ALL_TAB_COLUMNS WHERE OWNER='TRANSACTIONS_OWNER' AND TABLE_NAME='AVY_FACT_SIDE' ORDER BY COLUMN_ID", row_limit=500)
print(f"\nSource columns ({len(res3['rows'])}):")
for row in res3['rows'][:20]:
    print(f"  {row['COLUMN_ID']:3}. {row['COLUMN_NAME']} {row['DATA_TYPE']} {'NULL' if row['NULLABLE']=='Y' else 'NOT NULL'}")
print("  ...")
