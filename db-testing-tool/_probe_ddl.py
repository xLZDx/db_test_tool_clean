import requests, json

BASE = "http://127.0.0.1:8550"
DS = 3

def qry(sql, label, timeout=60):
    r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": sql, "row_limit": 5}, timeout=timeout)
    print(f"{label}: status={r.status_code}")
    try:
        d = r.json()
        print(f"  keys={list(d.keys())}")
        print(f"  error={str(d.get('error',''))[:200]}")
        print(f"  rows={d.get('rows', [])}")
        return d
    except Exception as e:
        print(f"  raw={r.text[:400]}")
        return {}

# Check if AVY_FACT_SIDE already exists
print("--- Table existence check ---")
qry("SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE", "table check")

# Check source
print("--- Source row count ---")
qry("SELECT COUNT(*) AS CNT FROM TRANSACTIONS_OWNER.AVY_FACT_SIDE", "source count")

# Test simple CREATE TABLE (using a temp name to avoid conflict)
print("--- Test DDL execution ---")
qry("BEGIN EXECUTE IMMEDIATE 'DROP TABLE IKOROSTELEV.AVY_FACT_SIDE_TMP'; EXCEPTION WHEN OTHERS THEN NULL; END;", "drop tmp")
d_ddl = qry("CREATE TABLE IKOROSTELEV.AVY_FACT_SIDE_TMP (ID NUMBER)", "create tmp table")
if not d_ddl.get("error"):
    print("  DDL WORKS - cleaning up")
    qry("DROP TABLE IKOROSTELEV.AVY_FACT_SIDE_TMP", "drop tmp cleanup")
else:
    print("  DDL FAILED")

# Also test a direct INSERT from source
print("--- Test simple INSERT ---")
d_ins = qry(
    "INSERT INTO IKOROSTELEV.AVY_FACT_SIDE SELECT * FROM TRANSACTIONS_OWNER.AVY_FACT_SIDE WHERE ROWNUM <= 5",
    "simple insert"
)
print(f"  rows_affected={d_ins.get('rows_affected', 0)}")
