"""Check IKOROSTELEV.AVY_FACT_SIDE state and column match."""
import requests, json

BASE = "http://127.0.0.1:8550"
DS = 3

def query(sql, row_limit=20):
    r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": sql, "row_limit": row_limit}, timeout=60)
    r.raise_for_status()
    return r.json()

# Check row count
res = query("SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE")
print(f"IKOROSTELEV.AVY_FACT_SIDE row count: {res['rows'][0]['CNT']}")

# Get IKOROSTELEV columns
res2 = query("SELECT COLUMN_NAME, COLUMN_ID FROM ALL_TAB_COLUMNS WHERE OWNER='IKOROSTELEV' AND TABLE_NAME='AVY_FACT_SIDE' ORDER BY COLUMN_ID", row_limit=500)
iko_cols = [r['COLUMN_NAME'] for r in res2['rows']]
print(f"IKOROSTELEV column count: {len(iko_cols)}")
print("First 10:", iko_cols[:10])
print("Last 10:", iko_cols[-10:])

# Get TRANSACTIONS_OWNER columns
res3 = query("SELECT COLUMN_NAME, COLUMN_ID FROM ALL_TAB_COLUMNS WHERE OWNER='TRANSACTIONS_OWNER' AND TABLE_NAME='AVY_FACT_SIDE' ORDER BY COLUMN_ID", row_limit=500)
src_cols = [r['COLUMN_NAME'] for r in res3['rows']]
print(f"\nTRANSACTIONS_OWNER column count: {len(src_cols)}")

# Check column compatibility
iko_set = set(iko_cols)
src_set = set(src_cols)
in_iko_not_src = iko_set - src_set
in_src_not_iko = src_set - iko_set
print(f"Cols in IKOROSTELEV but not TRANSACTIONS_OWNER: {in_iko_not_src}")
print(f"Cols in TRANSACTIONS_OWNER but not IKOROSTELEV (first 10): {list(in_src_not_iko)[:10]}")
print(f"Columns match: {iko_set == src_set}")
print(f"IKOROSTELEV is subset of source: {iko_set <= src_set}")
