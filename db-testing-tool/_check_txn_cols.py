"""Check which TXN columns referenced in the generated SQL actually exist."""
import re, requests

BASE = "http://127.0.0.1:8550"

with open("data/drd_full_insert.sql", encoding="utf-8") as f:
    sql = f.read()

txn_cols = sorted(set(re.findall(r"\bTXN\.(\w+)", sql)))
print(f"TXN columns referenced ({len(txn_cols)}):")
for c in txn_cols:
    print(f"  {c}")

# Batch check in live DB
in_list = ",".join([f"'{c}'" for c in txn_cols])
check_sql = f"SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER='CCAL_REPL_OWNER' AND TABLE_NAME='TXN' AND COLUMN_NAME IN ({in_list})"
r = requests.post(f"{BASE}/api/datasources/3/query",
                  json={"sql": check_sql, "row_limit": 200}, timeout=30)
found = {row["COLUMN_NAME"] for row in r.json().get("rows", [])}
missing = [c for c in txn_cols if c not in found]
print(f"\nFound {len(found)}/{len(txn_cols)} in TXN")
print(f"MISSING ({len(missing)}): {missing}")
