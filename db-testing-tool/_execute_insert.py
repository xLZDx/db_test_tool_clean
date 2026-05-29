"""Execute INSERT into IKOROSTELEV.AVY_FACT_SIDE from source, then verify."""
import requests, json

BASE = "http://127.0.0.1:8550"
DS = 3

def query(sql, row_limit=20):
    r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": sql, "row_limit": row_limit}, timeout=120)
    r.raise_for_status()
    return r.json()

# Step 1: INSERT 10 rows
print("=== Executing INSERT (ROWNUM <= 10) ===")
insert_sql = """INSERT INTO IKOROSTELEV.AVY_FACT_SIDE
SELECT * FROM TRANSACTIONS_OWNER.AVY_FACT_SIDE
WHERE ROWNUM <= 10"""

try:
    res = query(insert_sql)
    print(f"Message: {res.get('message')}")
    print(f"Rows affected: {res.get('total_rows_affected')}")
    for ex in res.get('executions', []):
        print(f"  Exec: rows_affected={ex.get('rows_affected')}, type={ex.get('statement_type')}")
except Exception as e:
    print(f"INSERT error: {e}")

# Step 2: COMMIT
print("\n=== COMMIT ===")
try:
    res2 = query("COMMIT")
    print(f"Message: {res2.get('message')}")
except Exception as e:
    print(f"COMMIT error: {e}")

# Step 3: Verify COUNT
print("\n=== Verifying COUNT(*) ===")
res3 = query("SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE")
cnt = res3['rows'][0]['CNT']
print(f"COUNT(*) = {cnt}")
assert cnt > 0, f"Expected rows > 0, got {cnt}"
print("SUCCESS: Table has data!")
