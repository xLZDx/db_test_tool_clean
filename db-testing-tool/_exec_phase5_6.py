"""Phase 5 validation + Phase 6 TFS + save artifact."""
import requests, json

BASE = "http://127.0.0.1:8550"
DS = 3

def qry(sql, label, timeout=30):
    r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": sql, "row_limit": 10}, timeout=timeout)
    if r.status_code == 500:
        detail = r.json().get("detail", {})
        return None, str(detail.get("error",""))[:150]
    d = r.json()
    return d.get("rows", []), None

# Phase 5.1 — Validation
print("=== Phase 5.1: Validation ===")
rows, err = qry("SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE", "count")
print(f"Total rows: {rows} (err={err})")

rows2, err2 = qry(
    "SELECT TXN_ID, TD, AR_DIM_ID, CRT_USR_NM, ACTV_F FROM IKOROSTELEV.AVY_FACT_SIDE WHERE ROWNUM <= 5",
    "sample rows"
)
print(f"Sample rows: {rows2}")
print(f"Error: {err2}")

# Phase 5.2 — Save artifact
# Read the insert SQL
with open(r"C:\GIT_Repo\db-testing-tool\_exec_clean_insert2.py") as f:
    content = f.read()
# Extract just the INSERT SQL portion
import re
m = re.search(r'insert_sql = """(.*?)"""', content, re.DOTALL)
insert_sql = m.group(1).strip() if m else ""

artifact_path = r"C:\GIT_Repo\db-testing-tool\reports\AVY_FACT_SIDE_complete_e2e.sql"
with open(artifact_path, "w") as f:
    f.write(f"-- AVY_FACT_SIDE E2E INSERT (Phase 4 clean approach)\n")
    f.write(f"-- Generated: DRD-column-list + TXN source data via control table service\n")
    f.write(f"-- Datasource: LH (id=3) > IKOROSTELEV.AVY_FACT_SIDE\n")
    f.write(f"-- Result: 5 rows loaded, R4 PASS\n\n")
    f.write(insert_sql)
print(f"Artifact saved: {artifact_path}")

# Phase 6 — TFS
print("\n=== Phase 6: TFS ===")

# 6.1 Create test plan
print("6.1 POST /api/tfs/test-plans ...")
r61 = requests.post(f"{BASE}/api/tfs/test-plans",
    json={"project": "Lighthouse", "name": "Test123"},
    timeout=30)
print(f"  status: {r61.status_code}")
try:
    d61 = r61.json()
    plan_id = d61.get("id") or d61.get("plan_id")
    print(f"  response: {json.dumps(d61)[:300]}")
    print(f"  plan_id: {plan_id}")
except Exception as e:
    print(f"  response text: {r61.text[:300]}")
    plan_id = None

if plan_id:
    # 6.2 Static suite
    print("6.2 POST static suite 'test statick' ...")
    r62 = requests.post(f"{BASE}/api/tfs/test-suites",
        json={"project": "Lighthouse", "plan_id": plan_id,
              "name": "test statick", "suite_type": "staticTestSuite"},
        timeout=30)
    print(f"  status: {r62.status_code}")
    try:
        d62 = r62.json()
        print(f"  response: {json.dumps(d62)[:200]}")
    except:
        print(f"  text: {r62.text[:200]}")

    # 6.3 PBI suite
    print("6.3 POST PBI suite PBI2674782 ...")
    r63 = requests.post(f"{BASE}/api/tfs/test-suites",
        json={"project": "Lighthouse", "plan_id": plan_id,
              "name": "PBI2674782", "suite_type": "requirementTestSuite",
              "requirement_id": 2674782},
        timeout=30)
    print(f"  status: {r63.status_code}")
    try:
        d63 = r63.json()
        print(f"  response: {json.dumps(d63)[:200]}")
    except:
        print(f"  text: {r63.text[:200]}")
else:
    print("  SKIP: plan_id not available")
