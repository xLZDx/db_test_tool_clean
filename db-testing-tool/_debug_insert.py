"""Debug: check source table row count and privileges."""
import requests

BASE = "http://127.0.0.1:8550"
DS = 3

def query(sql, row_limit=20):
    r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": sql, "row_limit": row_limit}, timeout=60)
    r.raise_for_status()
    d = r.json()
    return d

# Check source table row count (use stats, not full scan)
res = query("SELECT NUM_ROWS, LAST_ANALYZED FROM ALL_TABLES WHERE OWNER='TRANSACTIONS_OWNER' AND TABLE_NAME='AVY_FACT_SIDE'")
print("Source table stats:", res['rows'])

# Try SELECT from source
res2 = query("SELECT COUNT(*) AS CNT FROM TRANSACTIONS_OWNER.AVY_FACT_SIDE WHERE ROWNUM <= 1")
print("Source SELECT COUNT:", res2['rows'])

# Try direct SELECT
res3 = query("SELECT TD, TXN_ID FROM TRANSACTIONS_OWNER.AVY_FACT_SIDE WHERE ROWNUM <= 3", row_limit=5)
print("Source sample rows:", res3['rows'])

# Check if IKOROSTELEV has INSERT priv
res4 = query("SELECT PRIVILEGE, TABLE_NAME FROM ALL_TAB_PRIVS WHERE TABLE_NAME='AVY_FACT_SIDE' AND GRANTEE='IKOROSTELEV'", row_limit=20)
print("IKOROSTELEV privs on AVY_FACT_SIDE:", res4['rows'])

# Check current user
res5 = query("SELECT USER FROM DUAL")
print("Current DB user:", res5['rows'])
