"""Update test case 574 with the full 369-column DRD INSERT SQL."""
import requests, json

BASE = "http://127.0.0.1:8550"

with open("data/drd_full_insert.sql", encoding="utf-8") as f:
    insert_sql = f.read()

print(f"SQL: {len(insert_sql):,} chars")

# Check test 574 exists
r = requests.get(f"{BASE}/api/tests/574", timeout=10)
print(f"GET /api/tests/574: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    print(f"  name: {d.get('name')}, type: {d.get('test_type')}")

# Update test case 574
payload = {
    "name": "AVY_FACT_SIDE Insert E2E",
    "test_type": "custom_sql",
    "target_datasource_id": 3,
    "target_query": insert_sql,
    "description": (
        "Full 369-column DRD-based INSERT for IKOROSTELEV.AVY_FACT_SIDE. "
        "Sources: TXN direct (56 cols), LEFT JOIN lookups (75 cols, 22 joins), "
        "scalar subqueries for fan-out tables (APA, TXN_RLTNP, TXN_AVY_CL, AR_DIM, J$TXN), "
        "NOT NULL fallbacks (27 cols), NULL for multi-hop/complex transforms (211 cols). "
        "VARCHAR2 columns protected with SUBSTR to target column max length. "
        "Produces 5 rows (ROWNUM <= 5) from CCAL_REPL_OWNER.TXN WHERE ACTV_F = 'Y'."
    ),
    "severity": "critical",
}

r2 = requests.put(f"{BASE}/api/tests/574", json=payload, timeout=15)
print(f"PUT /api/tests/574: {r2.status_code}")
d2 = r2.json()
if r2.status_code in (200, 201):
    print(f"  Updated: {d2.get('name')}")
    print(f"  SQL length: {len(d2.get('target_query', '')):,}")
else:
    print(f"  ERROR: {d2}")
